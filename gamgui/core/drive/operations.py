"""Durable Drive ownership-operation manifests."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..paths import app_data_dir
from .models import OperationManifest, OperationTarget


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_operation_path() -> Path:
    base = app_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / "drive_operations.db"


class DriveOperationStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_operation_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._secure_files()
        return conn

    def _secure_files(self) -> None:
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                if candidate.exists():
                    os.chmod(candidate, 0o600)
            except OSError:
                pass

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS drive_operations (
                    id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    root_id TEXT NOT NULL,
                    target_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drive_operation_targets (
                    operation_id TEXT NOT NULL REFERENCES drive_operations(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    source_owner TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL,
                    residual_access TEXT NOT NULL,
                    PRIMARY KEY (operation_id, file_id)
                );
                CREATE INDEX IF NOT EXISTS drive_operations_domain_status
                    ON drive_operations(domain, status, updated_at);
                """
            )
        self._secure_files()
        self.recover_interrupted()

    def create(self, manifest: OperationManifest) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO drive_operations
                    (id, domain, kind, subject, destination, root_id, target_hash,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.id,
                    manifest.domain,
                    manifest.kind,
                    manifest.subject,
                    manifest.destination,
                    manifest.root_id,
                    manifest.target_hash,
                    manifest.status,
                    manifest.created_at,
                    manifest.updated_at,
                ),
            )
            conn.executemany(
                """
                INSERT INTO drive_operation_targets
                    (operation_id, ordinal, file_id, name, source_owner, mime_type,
                     status, error, residual_access)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        manifest.id,
                        ordinal,
                        target.file_id,
                        target.name,
                        target.source_owner,
                        target.mime_type,
                        target.status,
                        target.error,
                        target.residual_access,
                    )
                    for ordinal, target in enumerate(manifest.targets)
                ],
            )

    def get(self, operation_id: str, domain: str) -> Optional[OperationManifest]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM drive_operations WHERE id = ? AND domain = ?",
                (operation_id, domain),
            ).fetchone()
            if row is None:
                return None
            target_rows = conn.execute(
                """
                SELECT * FROM drive_operation_targets
                WHERE operation_id = ? ORDER BY ordinal
                """,
                (operation_id,),
            ).fetchall()
        return OperationManifest(
            id=row["id"],
            domain=row["domain"],
            kind=row["kind"],
            subject=row["subject"],
            destination=row["destination"],
            root_id=row["root_id"],
            target_hash=row["target_hash"],
            targets=[
                OperationTarget(
                    file_id=t["file_id"],
                    name=t["name"],
                    source_owner=t["source_owner"],
                    mime_type=t["mime_type"],
                    status=t["status"],
                    error=t["error"],
                    residual_access=t["residual_access"],
                )
                for t in target_rows
            ],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=row["status"],
        )

    def set_operation_status(self, operation_id: str, domain: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE drive_operations SET status = ?, updated_at = ?
                WHERE id = ? AND domain = ?
                """,
                (status, _now(), operation_id, domain),
            )

    def set_target_status(
        self,
        operation_id: str,
        domain: str,
        file_id: str,
        status: str,
        *,
        error: str = "",
        residual_access: str = "",
    ) -> None:
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM drive_operations WHERE id = ? AND domain = ?",
                (operation_id, domain),
            ).fetchone()
            if exists is None:
                raise KeyError("Drive operation not found for the active domain.")
            conn.execute(
                """
                UPDATE drive_operation_targets
                SET status = ?, error = ?, residual_access = ?
                WHERE operation_id = ? AND file_id = ?
                """,
                (status, error, residual_access, operation_id, file_id),
            )
            conn.execute(
                "UPDATE drive_operations SET updated_at = ? WHERE id = ?",
                (_now(), operation_id),
            )

    def recover_interrupted(self) -> None:
        """Crash recovery is fail-closed: running targets require an explicit resume."""
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE drive_operation_targets SET status = 'interrupted' WHERE status = 'running'"
            )
            conn.execute(
                """
                UPDATE drive_operations SET status = 'interrupted', updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
