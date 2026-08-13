from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from gamgui.core.classroom import manifests as manifests_module
from gamgui.core.classroom.manifests import RosterManifestStore
from gamgui.core.classroom.models import RosterDiff
from tests.windows_acl_assertions import assert_current_user_only_acl


def test_manifest_persists_exact_plan_and_target_results(tmp_path):
    store = RosterManifestStore(tmp_path / "ops.db")
    diff = RosterDiff.compute(
        "students",
        ["keep@example.com", "add@example.com"],
        ["keep@example.com", "remove@example.com"],
    )
    manifest = store.create("example.com", "123", diff)
    assert manifest.status == "planned"
    assert manifest.adds == ("add@example.com",)
    assert manifest.removes == ("remove@example.com",)
    assert manifest.unchanged == ("keep@example.com",)

    store.mark_running(manifest.id)
    store.mark_target(manifest.id, "add@example.com", "add", ok=True)
    store.mark_target(
        manifest.id, "remove@example.com", "remove", ok=False, detail="refused"
    )
    store.finish(
        manifest.id,
        status="partial",
        residual=["remove:remove@example.com"],
        error="Some changes failed.",
    )
    final = store.get(manifest.id)
    assert final is not None
    assert final.status == "partial"
    assert final.done_count == 2
    assert final.failed_count == 1
    assert final.residual == ("remove:remove@example.com",)


def test_running_manifest_becomes_interrupted_after_reopen(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    path = tmp_path / "ops.db"
    first = RosterManifestStore(path)
    manifest = first.create(
        "example.com",
        "123",
        RosterDiff.compute("students", ["a@example.com"], []),
    )
    first.mark_running(
        manifest.id,
        owner_id="dead-executor",
        owner_pid=child.pid,
        owner_identity="dead-process",
    )

    reopened = RosterManifestStore(path)
    recovered = reopened.get(manifest.id)
    assert recovered is not None
    assert recovered.status == "interrupted"
    assert "stopped" in recovered.error


def test_legacy_roster_claim_without_pid_stays_fail_closed(tmp_path):
    path = tmp_path / "ops.db"
    first = RosterManifestStore(path)
    manifest = first.create(
        "example.com",
        "123",
        RosterDiff.compute("students", ["a@example.com"], []),
    )
    assert first.mark_running(
        manifest.id,
        owner_id="legacy-executor",
        owner_pid=0,
        owner_identity="",
    )

    reopened = RosterManifestStore(path)

    assert reopened.get(manifest.id).status == "running"


def test_second_store_preserves_live_roster_claim_and_rejects_reclaim(tmp_path):
    path = tmp_path / "ops.db"
    first = RosterManifestStore(path)
    manifest = first.create(
        "example.com",
        "123",
        RosterDiff.compute("students", ["a@example.com"], []),
    )
    second = RosterManifestStore(path)
    barrier = Barrier(2)

    def claim(store, owner):
        barrier.wait()
        return store.mark_running(
            manifest.id,
            owner_id=owner,
            owner_pid=os.getpid(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda pair: claim(*pair),
                ((first, "executor-one"), (second, "executor-two")),
            )
        )

    assert sorted(outcomes) == [False, True]
    reopened = RosterManifestStore(path)
    assert reopened.get(manifest.id).status == "running"
    assert not reopened.mark_running(manifest.id)


def test_stale_roster_owner_cannot_write_target_or_terminal_status(tmp_path):
    store = RosterManifestStore(tmp_path / "ops.db")
    manifest = store.create(
        "example.com",
        "123",
        RosterDiff.compute("students", ["add@example.com"], []),
    )
    assert store.mark_running(
        manifest.id,
        owner_id="current-owner",
    )

    with pytest.raises(PermissionError, match="another executor"):
        store.mark_target(
            manifest.id,
            "add@example.com",
            "add",
            ok=True,
            owner_id="stale-owner",
        )
    with pytest.raises(PermissionError, match="another executor"):
        store.finish(
            manifest.id,
            status="completed",
            owner_id="stale-owner",
        )

    unchanged = store.get(manifest.id)
    assert unchanged.status == "running"
    assert unchanged.targets[0].status == "pending"


def test_roster_store_permission_failure_prevents_database_creation(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "private" / "ops.db"
    original_restrict = manifests_module.restrict_owner_only

    def fail_directory_restrict(candidate, *, directory=None):
        if Path(candidate) == path.parent:
            raise PermissionError("policy denied")
        return original_restrict(candidate, directory=directory)

    monkeypatch.setattr(
        manifests_module,
        "restrict_owner_only",
        fail_directory_restrict,
    )

    with pytest.raises(PermissionError, match="policy denied"):
        RosterManifestStore(path)

    assert not path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner-only mode assertion")
def test_roster_store_uses_owner_only_directory_and_database_modes(tmp_path):
    path = tmp_path / "private" / "ops.db"

    RosterManifestStore(path)

    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows protected-DACL assertion")
def test_roster_store_uses_current_user_only_windows_acls(tmp_path):
    path = tmp_path / "private" / "ops.db"

    RosterManifestStore(path)

    assert_current_user_only_acl(path.parent, directory=True)
    assert_current_user_only_acl(path)


def test_roster_store_tolerates_only_disappearing_sqlite_companions(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "ops.db"
    store = RosterManifestStore(path)
    original = manifests_module._secure_private_file

    def disappear_companions(candidate):
        if str(candidate).endswith(("-wal", "-shm")):
            raise FileNotFoundError(candidate)
        return original(candidate)

    monkeypatch.setattr(
        manifests_module,
        "_secure_private_file",
        disappear_companions,
    )

    store._restrict_perms()

    def reject_companion(candidate):
        if str(candidate).endswith("-wal"):
            raise PermissionError("unsafe companion")
        return original(candidate)

    monkeypatch.setattr(
        manifests_module,
        "_secure_private_file",
        reject_companion,
    )
    with pytest.raises(PermissionError, match="unsafe companion"):
        store._restrict_perms()


def test_roster_store_missing_main_database_stays_fail_closed(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "ops.db"
    store = RosterManifestStore(path)
    original = manifests_module._secure_private_file

    def disappear_main(candidate):
        if Path(candidate) == path:
            raise FileNotFoundError(candidate)
        return original(candidate)

    monkeypatch.setattr(
        manifests_module,
        "_secure_private_file",
        disappear_main,
    )

    with pytest.raises(FileNotFoundError):
        store._restrict_perms()
