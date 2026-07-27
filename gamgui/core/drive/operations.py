"""Durable Drive ownership-operation manifests."""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..paths import app_data_dir
from ..processes import current_process_identity, process_lease_is_dead
from .models import OperationManifest, OperationTarget

CLAIMABLE_OPERATION_STATUSES = ("planned", "interrupted", "partial")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_operation_path() -> Path:
    base = app_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / "drive_operations.db"


class DriveOperationStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_operation_path()
        _prepare_private_database(self.path)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        _prepare_private_database(self.path)
        conn = sqlite3.connect(str(self.path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._secure_files()
            return conn
        except BaseException:
            conn.close()
            raise

    def _secure_files(self) -> None:
        _secure_private_directory(self.path.parent)
        _secure_private_file(self.path)
        for companion in (
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                _secure_private_file(companion)
            except FileNotFoundError:
                # SQLite may delete an idle companion while permissions are
                # being tightened. Do not suppress any other safety failure.
                continue

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
                    updated_at TEXT NOT NULL,
                    run_owner TEXT NOT NULL DEFAULT '',
                    run_pid INTEGER NOT NULL DEFAULT 0,
                    run_identity TEXT NOT NULL DEFAULT ''
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
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(drive_operations)")
            }
            if "run_owner" not in columns:
                conn.execute(
                    "ALTER TABLE drive_operations "
                    "ADD COLUMN run_owner TEXT NOT NULL DEFAULT ''"
                )
            if "run_pid" not in columns:
                conn.execute(
                    "ALTER TABLE drive_operations "
                    "ADD COLUMN run_pid INTEGER NOT NULL DEFAULT 0"
                )
            if "run_identity" not in columns:
                conn.execute(
                    "ALTER TABLE drive_operations "
                    "ADD COLUMN run_identity TEXT NOT NULL DEFAULT ''"
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

    def set_operation_status(
        self,
        operation_id: str,
        domain: str,
        status: str,
        *,
        owner_id: str = "",
    ) -> None:
        owner = owner_id.strip() or f"pid:{os.getpid()}"
        with self._connect() as conn:
            if status == "running":
                raise ValueError(
                    "Running Drive operations must be acquired with claim_operation()."
                )
            result = conn.execute(
                """
                UPDATE drive_operations
                SET status = ?, updated_at = ?, run_owner = '', run_pid = 0,
                    run_identity = ''
                WHERE id = ? AND domain = ?
                  AND (status != 'running' OR run_owner = ?)
                """,
                (status, _now(), operation_id, domain, owner),
            )
            if result.rowcount != 1:
                exists = conn.execute(
                    "SELECT 1 FROM drive_operations WHERE id = ? AND domain = ?",
                    (operation_id, domain),
                ).fetchone()
                if exists is None:
                    raise KeyError("Drive operation not found for the active domain.")
                raise PermissionError(
                    "The Drive operation lease is owned by another executor."
                )

    def claim_operation(
        self,
        operation_id: str,
        domain: str,
        *,
        owner_id: str = "",
        owner_pid: Optional[int] = None,
        owner_identity: Optional[str] = None,
    ) -> bool:
        """Atomically claim one resumable manifest for a single executor."""

        pid = os.getpid() if owner_pid is None else int(owner_pid)
        owner = owner_id.strip() or f"pid:{pid}"
        identity = (
            current_process_identity()
            if owner_identity is None
            else owner_identity
        )
        placeholders = ",".join("?" for _ in CLAIMABLE_OPERATION_STATUSES)
        with self._connect() as conn:
            result = conn.execute(
                f"""
                UPDATE drive_operations
                SET status = 'running', updated_at = ?, run_owner = ?, run_pid = ?,
                    run_identity = ?
                WHERE id = ? AND domain = ? AND status IN ({placeholders})
                """,
                (
                    _now(),
                    owner,
                    pid,
                    identity,
                    operation_id,
                    domain,
                    *CLAIMABLE_OPERATION_STATUSES,
                ),
            )
        return result.rowcount == 1

    def owns_claim(self, operation_id: str, domain: str, owner_id: str) -> bool:
        """Return whether the exact executor token still owns the running lease."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM drive_operations
                WHERE id = ? AND domain = ? AND status = 'running'
                  AND run_owner = ?
                """,
                (operation_id, domain, owner_id),
            ).fetchone()
        return row is not None

    def has_active_jobs(self) -> bool:
        """Return whether an ownership operation is currently executing."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM drive_operations WHERE status = 'running' LIMIT 1"
            ).fetchone()
        return row is not None

    def set_target_status(
        self,
        operation_id: str,
        domain: str,
        file_id: str,
        status: str,
        *,
        error: str = "",
        residual_access: str = "",
        owner_id: str = "",
    ) -> None:
        owner = owner_id.strip() or f"pid:{os.getpid()}"
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM drive_operations WHERE id = ? AND domain = ?",
                (operation_id, domain),
            ).fetchone()
            if exists is None:
                raise KeyError("Drive operation not found for the active domain.")
            result = conn.execute(
                """
                UPDATE drive_operation_targets
                SET status = ?, error = ?, residual_access = ?
                WHERE operation_id = ? AND file_id = ?
                  AND EXISTS (
                      SELECT 1 FROM drive_operations
                      WHERE id = ? AND domain = ? AND status = 'running'
                        AND run_owner = ?
                  )
                """,
                (
                    status,
                    error,
                    residual_access,
                    operation_id,
                    file_id,
                    operation_id,
                    domain,
                    owner,
                ),
            )
            if result.rowcount != 1:
                raise PermissionError(
                    "The Drive operation lease is owned by another executor."
                )
            updated = conn.execute(
                """
                UPDATE drive_operations SET updated_at = ?
                WHERE id = ? AND domain = ? AND status = 'running'
                  AND run_owner = ?
                """,
                (_now(), operation_id, domain, owner),
            )
            if updated.rowcount != 1:
                raise PermissionError(
                    "The Drive operation lease is owned by another executor."
                )

    def recover_interrupted(self) -> None:
        """Recover only operations whose owning process is no longer alive.

        Constructing another store can happen during connector verification or in a second app
        process. A live executor's claim must remain untouched; otherwise the new store could make
        the same manifest claimable while the original task is still mutating Drive.
        """
        now = _now()
        with self._connect() as conn:
            running = conn.execute(
                """
                SELECT id, run_owner, run_pid, run_identity
                FROM drive_operations
                WHERE status = 'running'
                """
            ).fetchall()
            for operation in running:
                pid = int(operation["run_pid"] or 0)
                identity = str(operation["run_identity"] or "")
                if not process_lease_is_dead(pid, identity):
                    continue
                owner = str(operation["run_owner"] or "")
                result = conn.execute(
                    """
                    UPDATE drive_operations
                    SET status = 'interrupted', updated_at = ?,
                        run_owner = '', run_pid = 0, run_identity = ''
                    WHERE id = ? AND status = 'running'
                      AND run_owner = ? AND run_pid = ? AND run_identity = ?
                    """,
                    (now, operation["id"], owner, pid, identity),
                )
                if result.rowcount == 1:
                    conn.execute(
                        """
                        UPDATE drive_operation_targets
                        SET status = 'interrupted'
                        WHERE operation_id = ? AND status = 'running'
                        """,
                        (operation["id"],),
                    )


def _prepare_private_database(path: Path) -> None:
    """Create an owner-only SQLite file before any Drive identifiers are stored."""

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
        raise PermissionError("Drive operation data directory is not a private directory.")
    os.chmod(path, 0o700)
    _verify_owner_only(path, expected_mode=0o700, directory=True)


def _secure_private_file(path: Path) -> None:
    path = Path(path)
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise PermissionError("Drive operation data path is not a private file.")
    os.chmod(path, 0o600)
    _verify_owner_only(path, expected_mode=0o600, directory=False)


def _verify_owner_only(path: Path, *, expected_mode: int, directory: bool) -> None:
    metadata = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if path.is_symlink() or not expected_type(metadata.st_mode):
        raise PermissionError("Drive operation persistence changed type unexpectedly.")
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and int(metadata.st_uid) != int(getuid()):
        raise PermissionError("Drive operation persistence is not owned by this user.")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise PermissionError("Drive operation persistence is not owner-only.")
