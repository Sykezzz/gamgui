"""Persistent, restart-safe manifests for exact Classroom roster reconciliation."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ..paths import app_data_dir
from .models import RosterDiff


def default_roster_manifest_path() -> Path:
    return app_data_dir() / "classroom_roster_operations.db"


@dataclass(frozen=True)
class ManifestTarget:
    email: str
    action: str
    status: str = "pending"
    detail: str = ""


@dataclass(frozen=True)
class RosterManifest:
    id: str
    domain: str
    course_id: str
    role: str
    desired: Tuple[str, ...]
    unchanged: Tuple[str, ...]
    desired_hash: str
    basis_hash: str
    status: str
    created_at: float
    updated_at: float
    targets: Tuple[ManifestTarget, ...]
    residual: Tuple[str, ...] = ()
    error: str = ""

    @property
    def adds(self) -> Tuple[str, ...]:
        return tuple(target.email for target in self.targets if target.action == "add")

    @property
    def removes(self) -> Tuple[str, ...]:
        return tuple(target.email for target in self.targets if target.action == "remove")

    @property
    def change_count(self) -> int:
        return len(self.targets)

    @property
    def done_count(self) -> int:
        return sum(target.status in ("applied", "failed") for target in self.targets)

    @property
    def failed_count(self) -> int:
        return sum(target.status == "failed" for target in self.targets)


class RosterManifestStore:
    """SQLite store that converts in-flight operations to interrupted on process restart."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()
        self._restrict_perms()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init(self) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS roster_manifests (
                    id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    course_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    desired_json TEXT NOT NULL,
                    unchanged_json TEXT NOT NULL,
                    desired_hash TEXT NOT NULL,
                    basis_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    residual_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS roster_targets (
                    manifest_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    PRIMARY KEY (manifest_id, email, action),
                    FOREIGN KEY (manifest_id) REFERENCES roster_manifests(id) ON DELETE CASCADE
                )
                """
            )
            now = time.time()
            conn.execute(
                """
                UPDATE roster_manifests
                SET status = 'interrupted',
                    error = CASE
                        WHEN error = '' THEN 'The app stopped before this roster operation finished.'
                        ELSE error
                    END,
                    updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )

    def _restrict_perms(self) -> None:
        try:
            os.chmod(str(self.path.parent), 0o700)
        except OSError:
            pass
        for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                if candidate.exists():
                    os.chmod(str(candidate), 0o600)
            except OSError:
                pass

    def create(self, domain: str, course_id: str, diff: RosterDiff) -> RosterManifest:
        manifest_id = secrets.token_urlsafe(12)
        now = time.time()
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO roster_manifests (
                    id, domain, course_id, role, desired_json, unchanged_json,
                    desired_hash, basis_hash, status, residual_json, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', '[]', '', ?, ?)
                """,
                (
                    manifest_id,
                    domain.casefold(),
                    course_id,
                    diff.role,
                    json.dumps(diff.desired),
                    json.dumps(diff.unchanged),
                    diff.desired_hash,
                    diff.basis_hash,
                    now,
                    now,
                ),
            )
            conn.executemany(
                """
                INSERT INTO roster_targets (
                    manifest_id, email, action, status, detail
                ) VALUES (?, ?, ?, 'pending', '')
                """,
                [
                    (manifest_id, email, "add")
                    for email in diff.adds
                ]
                + [
                    (manifest_id, email, "remove")
                    for email in diff.removes
                ],
            )
        self._restrict_perms()
        manifest = self.get(manifest_id)
        if manifest is None:  # pragma: no cover - an immediately committed row must be readable
            raise RuntimeError("Roster manifest was not persisted.")
        return manifest

    def get(self, manifest_id: str) -> Optional[RosterManifest]:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM roster_manifests WHERE id = ?", (manifest_id,)
            ).fetchone()
            if row is None:
                return None
            targets = conn.execute(
                """
                SELECT email, action, status, detail
                FROM roster_targets
                WHERE manifest_id = ?
                ORDER BY CASE action WHEN 'add' THEN 0 ELSE 1 END, email
                """,
                (manifest_id,),
            ).fetchall()
        return RosterManifest(
            id=str(row["id"]),
            domain=str(row["domain"]),
            course_id=str(row["course_id"]),
            role=str(row["role"]),
            desired=tuple(json.loads(row["desired_json"] or "[]")),
            unchanged=tuple(json.loads(row["unchanged_json"] or "[]")),
            desired_hash=str(row["desired_hash"]),
            basis_hash=str(row["basis_hash"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            targets=tuple(
                ManifestTarget(
                    email=str(target["email"]),
                    action=str(target["action"]),
                    status=str(target["status"]),
                    detail=str(target["detail"] or ""),
                )
                for target in targets
            ),
            residual=tuple(json.loads(row["residual_json"] or "[]")),
            error=str(row["error"] or ""),
        )

    def mark_running(self, manifest_id: str) -> None:
        self._set_manifest(manifest_id, status="running", error="", residual=())

    def mark_target(
        self,
        manifest_id: str,
        email: str,
        action: str,
        *,
        ok: bool,
        detail: str = "",
    ) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                UPDATE roster_targets
                SET status = ?, detail = ?
                WHERE manifest_id = ? AND email = ? AND action = ?
                """,
                ("applied" if ok else "failed", detail, manifest_id, email, action),
            )
            conn.execute(
                "UPDATE roster_manifests SET updated_at = ? WHERE id = ?",
                (time.time(), manifest_id),
            )

    def finish(
        self,
        manifest_id: str,
        *,
        status: str,
        residual: Sequence[str] = (),
        error: str = "",
    ) -> None:
        if status not in ("completed", "partial", "failed", "stale", "interrupted"):
            raise ValueError(f"invalid manifest terminal status: {status!r}")
        self._set_manifest(manifest_id, status=status, error=error, residual=residual)

    def _set_manifest(
        self,
        manifest_id: str,
        *,
        status: str,
        error: str,
        residual: Sequence[str],
    ) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                UPDATE roster_manifests
                SET status = ?, residual_json = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, json.dumps(list(residual)), error, time.time(), manifest_id),
            )
        self._restrict_perms()
