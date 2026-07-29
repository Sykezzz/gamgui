from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from gamgui.core import persisted_activity as persisted_activity_module
from gamgui.core.persisted_activity import (
    CLASSROOM_ROSTER_ACTIVITY,
    CLASSROOM_TEACHER_ENTITLEMENT_ACTIVITY,
    DRIVE_OWNERSHIP_ACTIVITY,
    ONEROSTER_IMPORT_ACTIVITY,
    inspect_persisted_operations,
)


def _write_database(path: Path, sql: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(sql)
    path.chmod(0o600)


def _running_stores(root: Path) -> tuple[Path, Path, Path]:
    roster = root / "classroom_roster_operations.db"
    _write_database(
        roster,
        """
        CREATE TABLE roster_manifests (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL,
            updated_at REAL NOT NULL, run_owner TEXT NOT NULL,
            run_pid INTEGER NOT NULL, run_identity TEXT NOT NULL
        );
        INSERT INTO roster_manifests
        VALUES ('private-roster-id', 'running', '', 1, 'owner-a', 321, 'start-a');
        """,
    )
    drive = root / "drive_operations.db"
    _write_database(
        drive,
        """
        CREATE TABLE drive_operations (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL,
            run_owner TEXT NOT NULL, run_pid INTEGER NOT NULL,
            run_identity TEXT NOT NULL
        );
        CREATE TABLE drive_operation_targets (
            operation_id TEXT NOT NULL, status TEXT NOT NULL
        );
        INSERT INTO drive_operations
        VALUES ('private-drive-id', 'running', 'before', 'owner-b', 322, 'start-b');
        INSERT INTO drive_operation_targets VALUES ('private-drive-id', 'running');
        """,
    )
    oneroster = root / "components" / "classroom-oneroster" / "state.db"
    _write_database(
        oneroster,
        """
        CREATE TABLE manifests (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL,
            run_owner TEXT NOT NULL, run_pid INTEGER NOT NULL,
            run_identity TEXT NOT NULL
        );
        INSERT INTO manifests
        VALUES ('private-import-id', 'running', '', 'owner-c', 323, 'start-c');
        """,
    )
    return roster, drive, oneroster


def test_preflight_does_not_create_absent_operation_databases(tmp_path: Path) -> None:
    root = tmp_path / "absent-data"

    result = inspect_persisted_operations(root)

    assert result.may_activate
    assert not result.kinds
    assert not root.exists()


def test_preflight_recovers_dead_entitlement_executor(tmp_path: Path) -> None:
    store = tmp_path / "classroom_teacher_entitlements.db"
    _write_database(
        store,
        """
        CREATE TABLE entitlement_plans (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL,
            updated_at REAL NOT NULL, run_owner TEXT NOT NULL,
            run_pid INTEGER NOT NULL, run_identity TEXT NOT NULL
        );
        INSERT INTO entitlement_plans
        VALUES ('private-policy-plan', 'running', '', 1, 'owner-e', 999, 'start-e');
        """,
    )

    result = inspect_persisted_operations(
        tmp_path,
        lease_is_dead=lambda pid, identity: (pid, identity) == (999, "start-e"),
    )

    assert result.recovered_kinds == (CLASSROOM_TEACHER_ENTITLEMENT_ACTIVITY,)
    with sqlite3.connect(store) as connection:
        status, error = connection.execute(
            "SELECT status, error FROM entitlement_plans"
        ).fetchone()
    assert status == "interrupted"
    assert "administrative operation" in error


def test_preflight_recovers_only_definitely_dead_leases_and_defers_once(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    roster, drive, oneroster = _running_stores(root)

    result = inspect_persisted_operations(
        root,
        lease_is_dead=lambda _pid, _identity: True,
    )

    assert result.recovered
    assert not result.blocked
    assert result.should_defer
    assert set(result.recovered_kinds) == {
        CLASSROOM_ROSTER_ACTIVITY,
        DRIVE_OWNERSHIP_ACTIVITY,
        ONEROSTER_IMPORT_ACTIVITY,
    }
    with sqlite3.connect(roster) as connection:
        assert connection.execute(
            "SELECT status, run_owner, run_pid, run_identity FROM roster_manifests"
        ).fetchone() == ("interrupted", "", 0, "")
    with sqlite3.connect(drive) as connection:
        assert connection.execute(
            "SELECT status, run_owner, run_pid, run_identity FROM drive_operations"
        ).fetchone() == ("interrupted", "", 0, "")
        assert connection.execute(
            "SELECT status FROM drive_operation_targets"
        ).fetchone()[0] == "interrupted"
    with sqlite3.connect(oneroster) as connection:
        assert connection.execute(
            "SELECT status, error, run_owner, run_pid, run_identity FROM manifests"
        ).fetchone() == (
            "interrupted",
            "OR-EXECUTION-INTERRUPTED",
            "",
            0,
            "",
        )

    next_launch = inspect_persisted_operations(
        root,
        lease_is_dead=lambda *_args: (_ for _ in ()).throw(
            AssertionError("terminal rows must not be probed")
        ),
    )
    assert next_launch.may_activate


def test_live_or_unknown_process_lease_blocks_without_exposing_row_data(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    roster, _drive, _oneroster = _running_stores(root)

    result = inspect_persisted_operations(
        root,
        lease_is_dead=lambda _pid, _identity: False,
    )

    assert result.blocked
    assert not result.recovered
    assert set(result.blocked_kinds) == {
        CLASSROOM_ROSTER_ACTIVITY,
        DRIVE_OWNERSHIP_ACTIVITY,
        ONEROSTER_IMPORT_ACTIVITY,
    }
    assert all("private" not in kind for kind in result.kinds)
    with sqlite3.connect(roster) as connection:
        assert connection.execute(
            "SELECT status FROM roster_manifests"
        ).fetchone()[0] == "running"


def test_legacy_running_row_without_process_metadata_stays_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    roster = root / "classroom_roster_operations.db"
    _write_database(
        roster,
        """
        CREATE TABLE roster_manifests (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO roster_manifests VALUES ('legacy', 'running', '', 1);
        """,
    )

    result = inspect_persisted_operations(root)

    assert result.blocked_kinds == (CLASSROOM_ROSTER_ACTIVITY,)
    with sqlite3.connect(roster) as connection:
        assert connection.execute(
            "SELECT status FROM roster_manifests"
        ).fetchone()[0] == "running"


def test_corrupt_locked_symlink_and_unreadable_stores_block(
    tmp_path: Path,
) -> None:
    corrupt_root = tmp_path / "corrupt"
    corrupt_root.mkdir()
    corrupt = corrupt_root / "classroom_roster_operations.db"
    corrupt.write_bytes(b"not a sqlite database")
    corrupt.chmod(0o600)
    assert inspect_persisted_operations(corrupt_root).blocked_kinds == (
        CLASSROOM_ROSTER_ACTIVITY,
    )

    locked_root = tmp_path / "locked"
    locked, _drive, _oneroster = _running_stores(locked_root)
    with sqlite3.connect(locked, isolation_level=None) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        assert CLASSROOM_ROSTER_ACTIVITY in inspect_persisted_operations(
            locked_root,
            lease_is_dead=lambda *_args: True,
        ).blocked_kinds

    unreadable_root = tmp_path / "unreadable"
    unreadable, _drive, _oneroster = _running_stores(unreadable_root)
    unreadable.chmod(0)
    try:
        assert CLASSROOM_ROSTER_ACTIVITY in inspect_persisted_operations(
            unreadable_root,
        ).blocked_kinds
    finally:
        unreadable.chmod(0o600)

    symlink_root = tmp_path / "symlink"
    symlink_root.mkdir()
    outside = tmp_path / "outside.db"
    _write_database(outside, "CREATE TABLE harmless (value TEXT);")
    try:
        (symlink_root / "drive_operations.db").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this test host.")
    assert inspect_persisted_operations(symlink_root).blocked_kinds == (
        DRIVE_OWNERSHIP_ACTIVITY,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner-only mode assertion")
def test_group_readable_operation_store_blocks_activation(tmp_path: Path) -> None:
    root = tmp_path / "data"
    roster = root / "classroom_roster_operations.db"
    _write_database(
        roster,
        """
        CREATE TABLE roster_manifests (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        """,
    )
    roster.chmod(0o640)

    result = inspect_persisted_operations(root)

    assert result.blocked_kinds == (CLASSROOM_ROSTER_ACTIVITY,)


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner-only mode assertion")
def test_owner_executable_operation_store_blocks_activation(tmp_path: Path) -> None:
    root = tmp_path / "data"
    roster = root / "classroom_roster_operations.db"
    _write_database(
        roster,
        """
        CREATE TABLE roster_manifests (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        """,
    )
    roster.chmod(0o700)

    result = inspect_persisted_operations(root)

    assert result.blocked_kinds == (CLASSROOM_ROSTER_ACTIVITY,)


def test_nonrunning_legacy_schema_remains_backward_compatible(tmp_path: Path) -> None:
    root = tmp_path / "data"
    roster = root / "classroom_roster_operations.db"
    _write_database(
        roster,
        """
        CREATE TABLE roster_manifests (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO roster_manifests VALUES ('legacy', 'completed', '', 1);
        """,
    )

    result = inspect_persisted_operations(root)

    assert result.may_activate
    with sqlite3.connect(roster) as connection:
        assert {
            row[1] for row in connection.execute("PRAGMA table_info(roster_manifests)")
        } == {"id", "status", "error", "updated_at"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX ancestor mode assertion")
def test_group_or_world_writable_data_ancestor_blocks_activation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    _running_stores(root)
    root.chmod(0o777)
    try:
        result = inspect_persisted_operations(root)
    finally:
        root.chmod(0o700)

    assert set(result.blocked_kinds) == {
        CLASSROOM_ROSTER_ACTIVITY,
        DRIVE_OWNERSHIP_ACTIVITY,
        ONEROSTER_IMPORT_ACTIVITY,
    }


def test_unknown_operation_statuses_block_without_probing_processes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    roster, drive, oneroster = _running_stores(root)
    for path, table in (
        (roster, "roster_manifests"),
        (drive, "drive_operations"),
        (oneroster, "manifests"),
    ):
        with sqlite3.connect(path) as connection:
            connection.execute(f'UPDATE "{table}" SET status = ?', ("unexpected",))

    result = inspect_persisted_operations(
        root,
        lease_is_dead=lambda *_args: (_ for _ in ()).throw(
            AssertionError("unknown statuses must block before lease recovery")
        ),
    )

    assert set(result.blocked_kinds) == {
        CLASSROOM_ROSTER_ACTIVITY,
        DRIVE_OWNERSHIP_ACTIVITY,
        ONEROSTER_IMPORT_ACTIVITY,
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX inode replacement semantics")
def test_database_path_swap_during_open_blocks_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "data"
    roster = root / "classroom_roster_operations.db"
    replacement = root / "replacement.db"
    schema = """
        CREATE TABLE roster_manifests (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL,
            updated_at REAL NOT NULL, run_owner TEXT NOT NULL,
            run_pid INTEGER NOT NULL, run_identity TEXT NOT NULL
        );
        INSERT INTO roster_manifests
        VALUES ('row', 'running', '', 1, 'owner', 321, 'identity');
    """
    _write_database(roster, schema)
    _write_database(replacement, schema)
    original_connect = persisted_activity_module.sqlite3.connect
    swapped = False

    def swap_after_open(*args, **kwargs):
        nonlocal swapped
        connection = original_connect(*args, **kwargs)
        if not swapped:
            os.replace(replacement, roster)
            swapped = True
        return connection

    monkeypatch.setattr(
        persisted_activity_module.sqlite3,
        "connect",
        swap_after_open,
    )

    result = inspect_persisted_operations(
        root,
        lease_is_dead=lambda *_args: True,
    )

    assert swapped
    assert result.blocked_kinds == (CLASSROOM_ROSTER_ACTIVITY,)
    assert not result.recovered_kinds
