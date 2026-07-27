"""Fail-closed preflight for persisted administrative-operation leases.

The application updater must not activate a different bundle while a previous
process may still be mutating Workspace state.  This module inspects only the
three allowlisted operation databases that already exist.  It never creates an
absent store, never exposes tenant or target identifiers, and only recovers a
row when the process helper proves that its lease owner is dead (or its PID was
definitely reused).
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .processes import process_lease_is_dead

CLASSROOM_ROSTER_ACTIVITY = "classroom-roster"
DRIVE_OWNERSHIP_ACTIVITY = "drive-ownership"
ONEROSTER_IMPORT_ACTIVITY = "oneroster-import"


@dataclass(frozen=True)
class PersistedActivityPreflight:
    """Privacy-safe activation decision for persisted operation stores."""

    blocked_kinds: tuple[str, ...] = ()
    recovered_kinds: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_kinds)

    @property
    def recovered(self) -> bool:
        return bool(self.recovered_kinds)

    @property
    def should_defer(self) -> bool:
        """A recovery also defers once so the interrupted state is observable."""

        return self.blocked or self.recovered

    @property
    def may_activate(self) -> bool:
        return not self.should_defer

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys((*self.blocked_kinds, *self.recovered_kinds))
        )


@dataclass(frozen=True)
class _StoreSpec:
    relative_path: tuple[str, ...]
    table: str
    kind: str
    base_columns: frozenset[str]
    allowed_statuses: frozenset[str]


_LEASE_COLUMNS = frozenset({"run_owner", "run_pid", "run_identity"})
_STORES = (
    _StoreSpec(
        ("classroom_roster_operations.db",),
        "roster_manifests",
        CLASSROOM_ROSTER_ACTIVITY,
        frozenset({"id", "status", "error", "updated_at"}),
        frozenset(
            {
                "planned",
                "running",
                "completed",
                "partial",
                "failed",
                "stale",
                "interrupted",
            }
        ),
    ),
    _StoreSpec(
        ("drive_operations.db",),
        "drive_operations",
        DRIVE_OWNERSHIP_ACTIVITY,
        frozenset({"id", "status", "updated_at"}),
        frozenset(
            {
                "planned",
                "running",
                "completed",
                "partial",
                "failed",
                "interrupted",
            }
        ),
    ),
    _StoreSpec(
        ("components", "classroom-oneroster", "state.db"),
        "manifests",
        ONEROSTER_IMPORT_ACTIVITY,
        frozenset({"id", "status", "error"}),
        frozenset(
            {
                "planned",
                "running",
                "completed",
                "partial",
                "failed",
                "stale",
                "interrupted",
                "awaiting_students",
            }
        ),
    ),
)


def inspect_persisted_operations(
    data_root: Path,
    *,
    oneroster_state_path: Optional[Path] = None,
    lease_is_dead: Callable[[int, str], bool] = process_lease_is_dead,
) -> PersistedActivityPreflight:
    """Inspect allowlisted existing stores before updater activation.

    Missing databases are intentionally ignored.  Any existing store that
    cannot be proven safe blocks activation.  Definitely dead leases are
    atomically changed to ``interrupted`` and make this invocation defer; the
    next launch may proceed after observing the persisted interruption.
    """

    root = Path(data_root).expanduser()
    blocked: list[str] = []
    recovered: list[str] = []
    for spec in _STORES:
        path = root.joinpath(*spec.relative_path)
        if spec.kind == ONEROSTER_IMPORT_ACTIVITY and oneroster_state_path is not None:
            path = Path(oneroster_state_path).expanduser()
        store_blocked, store_recovered = _inspect_store(
            root,
            path,
            spec,
            lease_is_dead=lease_is_dead,
        )
        if store_blocked:
            blocked.append(spec.kind)
        if store_recovered:
            recovered.append(spec.kind)
    return PersistedActivityPreflight(
        blocked_kinds=tuple(dict.fromkeys(blocked)),
        recovered_kinds=tuple(dict.fromkeys(recovered)),
    )


def _inspect_store(
    root: Path,
    path: Path,
    spec: _StoreSpec,
    *,
    lease_is_dead: Callable[[int, str], bool],
) -> tuple[bool, bool]:
    safety = _existing_store_safety(root, path)
    if safety == "missing":
        return False, False
    if safety != "safe":
        return True, False

    connection: Optional[sqlite3.Connection] = None
    descriptor: Optional[int] = None
    recovered = False
    blocked = False
    try:
        resolved = path.resolve(strict=True)
        flags = os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(resolved), flags)
        bound_metadata = os.fstat(descriptor)
        if not _same_file_identity(bound_metadata, path.lstat()):
            raise OSError("Operation database changed before it could be opened.")
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=rw",
            uri=True,
            timeout=0.0,
            isolation_level=None,
        )
        if not _bound_store_is_current(root, path, descriptor):
            raise OSError("Operation database changed while it was being opened.")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("BEGIN IMMEDIATE")
        check = connection.execute("PRAGMA quick_check(1)").fetchone()
        if check is None or str(check[0]).casefold() != "ok":
            raise sqlite3.DatabaseError("SQLite quick check failed.")
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (spec.table,),
        ).fetchone()
        if table is None:
            raise sqlite3.DatabaseError("Operation table is missing.")
        columns = {
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{spec.table}")')
        }
        if not spec.base_columns.issubset(columns):
            raise sqlite3.DatabaseError("Operation table schema is incomplete.")
        present_lease_columns = columns & _LEASE_COLUMNS
        if present_lease_columns not in {frozenset(), _LEASE_COLUMNS}:
            raise sqlite3.DatabaseError("Operation lease schema is incomplete.")

        statuses = connection.execute(
            f'SELECT DISTINCT status FROM "{spec.table}"'
        ).fetchall()
        if any(
            not isinstance(row[0], str) or row[0] not in spec.allowed_statuses
            for row in statuses
        ):
            raise sqlite3.DatabaseError("Operation table contains an unknown status.")

        if present_lease_columns == _LEASE_COLUMNS:
            running = connection.execute(
                f"""
                SELECT id, run_owner, run_pid, run_identity
                FROM "{spec.table}"
                WHERE status = 'running'
                """
            ).fetchall()
        else:
            legacy_running = connection.execute(
                f"""
                SELECT 1 FROM "{spec.table}"
                WHERE status = 'running' LIMIT 1
                """
            ).fetchone()
            if legacy_running is not None:
                blocked = True
            running = ()

        for row in running:
            owner = row["run_owner"]
            pid = row["run_pid"]
            identity = row["run_identity"]
            if (
                not isinstance(owner, str)
                or not owner
                or isinstance(pid, bool)
                or not isinstance(pid, int)
                or pid <= 0
                or not isinstance(identity, str)
                or len(identity) > 512
            ):
                blocked = True
                continue
            try:
                definitely_dead = bool(lease_is_dead(pid, identity))
            except (OSError, RuntimeError, TypeError, ValueError):
                definitely_dead = False
            if not definitely_dead:
                blocked = True
                continue
            if not _recover_row(connection, spec, row):
                blocked = True
                continue
            recovered = True

        if not _bound_store_is_current(root, path, descriptor):
            raise OSError("Operation database changed during inspection.")
        connection.commit()
        if not _bound_store_is_current(root, path, descriptor):
            raise OSError("Operation database changed while recovery was committed.")
    except (OSError, sqlite3.DatabaseError, UnicodeError, ValueError):
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.DatabaseError:
                pass
        return True, False
    finally:
        if connection is not None:
            connection.close()
        if descriptor is not None:
            os.close(descriptor)
    return blocked, recovered


def _recover_row(
    connection: sqlite3.Connection,
    spec: _StoreSpec,
    row: sqlite3.Row,
) -> bool:
    owner = str(row["run_owner"])
    pid = int(row["run_pid"])
    identity = str(row["run_identity"])
    if spec.kind == CLASSROOM_ROSTER_ACTIVITY:
        result = connection.execute(
            """
            UPDATE roster_manifests
            SET status = 'interrupted',
                error = CASE
                    WHEN error = ''
                    THEN 'The app stopped before this roster operation finished.'
                    ELSE error
                END,
                updated_at = ?, run_owner = '', run_pid = 0, run_identity = ''
            WHERE id = ? AND status = 'running'
              AND run_owner = ? AND run_pid = ? AND run_identity = ?
            """,
            (time.time(), row["id"], owner, pid, identity),
        )
    elif spec.kind == DRIVE_OWNERSHIP_ACTIVITY:
        result = connection.execute(
            """
            UPDATE drive_operations
            SET status = 'interrupted', updated_at = ?,
                run_owner = '', run_pid = 0, run_identity = ''
            WHERE id = ? AND status = 'running'
              AND run_owner = ? AND run_pid = ? AND run_identity = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                row["id"],
                owner,
                pid,
                identity,
            ),
        )
        if result.rowcount == 1:
            connection.execute(
                """
                UPDATE drive_operation_targets
                SET status = 'interrupted'
                WHERE operation_id = ? AND status = 'running'
                """,
                (row["id"],),
            )
    else:
        result = connection.execute(
            """
            UPDATE manifests
            SET status = 'interrupted', error = 'OR-EXECUTION-INTERRUPTED',
                run_owner = '', run_pid = 0, run_identity = ''
            WHERE id = ? AND status = 'running'
              AND run_owner = ? AND run_pid = ? AND run_identity = ?
            """,
            (row["id"], owner, pid, identity),
        )
    return result.rowcount == 1


def _existing_store_safety(root: Path, path: Path) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "blocked"
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        return "blocked"
    permissions = stat.S_IMODE(metadata.st_mode)
    if (
        not permissions & stat.S_IRUSR
        or not permissions & stat.S_IWUSR
        or (os.name == "posix" and permissions != 0o600)
        or not _owned_by_current_user(metadata)
    ):
        return "blocked"

    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return "blocked"
    current = path.parent
    while True:
        try:
            parent_metadata = current.lstat()
        except OSError:
            return "blocked"
        if not stat.S_ISDIR(parent_metadata.st_mode) or current.is_symlink():
            return "blocked"
        if not _owned_by_current_user(parent_metadata):
            return "blocked"
        if (
            os.name == "posix"
            and stat.S_IMODE(parent_metadata.st_mode) & 0o022
        ):
            return "blocked"
        if current == root:
            break
        if current == current.parent:
            return "blocked"
        current = current.parent

    for suffix in ("-wal", "-shm", "-journal"):
        companion = Path(f"{path}{suffix}")
        try:
            companion_metadata = companion.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return "blocked"
        companion_permissions = stat.S_IMODE(companion_metadata.st_mode)
        if (
            not stat.S_ISREG(companion_metadata.st_mode)
            or companion.is_symlink()
            or not companion_permissions & stat.S_IRUSR
            or not companion_permissions & stat.S_IWUSR
            or (os.name == "posix" and companion_permissions != 0o600)
            or not _owned_by_current_user(companion_metadata)
        ):
            return "blocked"
    return "safe"


def _owned_by_current_user(metadata: os.stat_result) -> bool:
    getuid = getattr(os, "getuid", None)
    return not callable(getuid) or int(metadata.st_uid) == int(getuid())


def _same_file_identity(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return (int(first.st_dev), int(first.st_ino)) == (
        int(second.st_dev),
        int(second.st_ino),
    )


def _bound_store_is_current(root: Path, path: Path, descriptor: int) -> bool:
    """Revalidate that SQLite's pathname still resolves to the pinned file."""

    if _existing_store_safety(root, path) != "safe":
        return False
    try:
        return _same_file_identity(os.fstat(descriptor), path.lstat())
    except OSError:
        return False


__all__ = [
    "CLASSROOM_ROSTER_ACTIVITY",
    "DRIVE_OWNERSHIP_ACTIVITY",
    "ONEROSTER_IMPORT_ACTIVITY",
    "PersistedActivityPreflight",
    "inspect_persisted_operations",
]
