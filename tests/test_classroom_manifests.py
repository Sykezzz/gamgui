from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from gamgui.core.classroom.manifests import RosterManifestStore
from gamgui.core.classroom.models import RosterDiff


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
