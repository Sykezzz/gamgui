"""Persistent, restart-safe manifests for exact Classroom roster reconciliation."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ..paths import app_data_dir
from ..processes import current_process_identity, process_lease_is_dead
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
        _prepare_private_database(self.path)
        self._init()
        self._restrict_perms()

    def _conn(self) -> sqlite3.Connection:
        _prepare_private_database(self.path)
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            self._restrict_perms()
            return conn
        except BaseException:
            conn.close()
            raise

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
                    updated_at REAL NOT NULL,
                    run_owner TEXT NOT NULL DEFAULT '',
                    run_pid INTEGER NOT NULL DEFAULT 0,
                    run_identity TEXT NOT NULL DEFAULT ''
                )
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(roster_manifests)")
            }
            if "run_owner" not in columns:
                conn.execute(
                    "ALTER TABLE roster_manifests "
                    "ADD COLUMN run_owner TEXT NOT NULL DEFAULT ''"
                )
            if "run_pid" not in columns:
                conn.execute(
                    "ALTER TABLE roster_manifests "
                    "ADD COLUMN run_pid INTEGER NOT NULL DEFAULT 0"
                )
            if "run_identity" not in columns:
                conn.execute(
                    "ALTER TABLE roster_manifests "
                    "ADD COLUMN run_identity TEXT NOT NULL DEFAULT ''"
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
            running = conn.execute(
                """
                SELECT id, run_owner, run_pid, run_identity
                FROM roster_manifests
                WHERE status = 'running'
                """
            ).fetchall()
            for manifest in running:
                pid = int(manifest["run_pid"] or 0)
                identity = str(manifest["run_identity"] or "")
                if not process_lease_is_dead(pid, identity):
                    continue
                conn.execute(
                    """
                    UPDATE roster_manifests
                    SET status = 'interrupted',
                        error = CASE
                            WHEN error = ''
                            THEN 'The app stopped before this roster operation finished.'
                            ELSE error
                        END,
                        updated_at = ?, run_owner = '', run_pid = 0,
                        run_identity = ''
                    WHERE id = ? AND status = 'running'
                      AND run_owner = ? AND run_pid = ? AND run_identity = ?
                    """,
                    (
                        time.time(),
                        manifest["id"],
                        str(manifest["run_owner"] or ""),
                        pid,
                        identity,
                    ),
                )

    def _restrict_perms(self) -> None:
        _secure_private_directory(self.path.parent)
        for candidate in (
            self.path,
            Path(str(self.path) + "-wal"),
            Path(str(self.path) + "-shm"),
        ):
            if candidate.is_symlink() or candidate.exists():
                _secure_private_file(candidate)

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

    def mark_running(
        self,
        manifest_id: str,
        *,
        owner_id: str = "",
        owner_pid: Optional[int] = None,
        owner_identity: Optional[str] = None,
    ) -> bool:
        """Atomically claim one fresh roster preview for exactly one executor."""

        pid = os.getpid() if owner_pid is None else int(owner_pid)
        owner = owner_id.strip() or f"pid:{pid}"
        identity = (
            current_process_identity()
            if owner_identity is None
            else owner_identity
        )
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE roster_manifests
                SET status = 'running', residual_json = '[]', error = '',
                    updated_at = ?, run_owner = ?, run_pid = ?,
                    run_identity = ?
                WHERE id = ? AND status = 'planned'
                """,
                (time.time(), owner, pid, identity, manifest_id),
            )
        self._restrict_perms()
        return result.rowcount == 1

    def owns_claim(self, manifest_id: str, owner_id: str) -> bool:
        """Return whether the exact executor token still owns the running lease."""

        with closing(self._conn()) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM roster_manifests
                WHERE id = ? AND status = 'running' AND run_owner = ?
                """,
                (manifest_id, owner_id),
            ).fetchone()
        return row is not None

    def has_active_jobs(self) -> bool:
        """Return whether a roster mutation is currently executing."""
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT 1 FROM roster_manifests WHERE status = 'running' LIMIT 1"
            ).fetchone()
        return row is not None

    def mark_target(
        self,
        manifest_id: str,
        email: str,
        action: str,
        *,
        ok: bool,
        detail: str = "",
        owner_id: str = "",
    ) -> None:
        owner = owner_id.strip() or f"pid:{os.getpid()}"
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE roster_targets
                SET status = ?, detail = ?
                WHERE manifest_id = ? AND email = ? AND action = ?
                  AND EXISTS (
                      SELECT 1 FROM roster_manifests
                      WHERE id = ? AND status = 'running' AND run_owner = ?
                  )
                """,
                (
                    "applied" if ok else "failed",
                    detail,
                    manifest_id,
                    email,
                    action,
                    manifest_id,
                    owner,
                ),
            )
            if result.rowcount != 1:
                raise PermissionError(
                    "The Classroom roster lease is owned by another executor."
                )
            updated = conn.execute(
                """
                UPDATE roster_manifests SET updated_at = ?
                WHERE id = ? AND status = 'running' AND run_owner = ?
                """,
                (time.time(), manifest_id, owner),
            )
            if updated.rowcount != 1:
                raise PermissionError(
                    "The Classroom roster lease is owned by another executor."
                )

    def finish(
        self,
        manifest_id: str,
        *,
        status: str,
        residual: Sequence[str] = (),
        error: str = "",
        owner_id: str = "",
    ) -> None:
        if status not in ("completed", "partial", "failed", "stale", "interrupted"):
            raise ValueError(f"invalid manifest terminal status: {status!r}")
        self._set_manifest(
            manifest_id,
            status=status,
            error=error,
            residual=residual,
            owner_id=owner_id,
        )

    def _set_manifest(
        self,
        manifest_id: str,
        *,
        status: str,
        error: str,
        residual: Sequence[str],
        owner_id: str = "",
    ) -> None:
        owner = owner_id.strip() or f"pid:{os.getpid()}"
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE roster_manifests
                SET status = ?, residual_json = ?, error = ?, updated_at = ?,
                    run_owner = '', run_pid = 0, run_identity = ''
                WHERE id = ?
                  AND (status != 'running' OR run_owner = ?)
                """,
                (
                    status,
                    json.dumps(list(residual)),
                    error,
                    time.time(),
                    manifest_id,
                    owner,
                ),
            )
            if result.rowcount != 1:
                exists = conn.execute(
                    "SELECT 1 FROM roster_manifests WHERE id = ?",
                    (manifest_id,),
                ).fetchone()
                if exists is None:
                    raise KeyError("Roster manifest not found.")
                raise PermissionError(
                    "The Classroom roster lease is owned by another executor."
                )
        self._restrict_perms()


def _prepare_private_database(path: Path) -> None:
    """Create an owner-only SQLite file before any tenant data can be written."""

    path = Path(path)
    _secure_private_directory(path.parent, create=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except FileExistsError:
        _secure_private_file(path)
        return
    try:
        fchmod = getattr(os, "fchmod", None)
        if callable(fchmod):
            fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    _secure_private_file(path)


def _secure_private_directory(path: Path, *, create: bool = False) -> None:
    path = Path(path)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError("Classroom operation data directory is not a private directory.")
    os.chmod(path, 0o700)
    _verify_owner_only(path, expected_mode=0o700, directory=True)


def _secure_private_file(path: Path) -> None:
    path = Path(path)
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise PermissionError("Classroom operation data path is not a private file.")
    os.chmod(path, 0o600)
    _verify_owner_only(path, expected_mode=0o600, directory=False)


def _verify_owner_only(path: Path, *, expected_mode: int, directory: bool) -> None:
    metadata = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if path.is_symlink() or not expected_type(metadata.st_mode):
        raise PermissionError("Classroom operation persistence changed type unexpectedly.")
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and int(metadata.st_uid) != int(getuid()):
        raise PermissionError("Classroom operation persistence is not owned by this user.")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise PermissionError("Classroom operation persistence is not owner-only.")
