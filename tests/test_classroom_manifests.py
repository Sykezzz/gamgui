from __future__ import annotations

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
    path = tmp_path / "ops.db"
    first = RosterManifestStore(path)
    manifest = first.create(
        "example.com",
        "123",
        RosterDiff.compute("students", ["a@example.com"], []),
    )
    first.mark_running(manifest.id)

    reopened = RosterManifestStore(path)
    recovered = reopened.get(manifest.id)
    assert recovered is not None
    assert recovered.status == "interrupted"
    assert "stopped" in recovered.error
