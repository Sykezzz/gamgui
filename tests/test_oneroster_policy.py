from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gamgui.components.oneroster import (
    ActionLimit,
    BlackoutWindow,
    GateState,
    ImportAction,
    OneRosterError,
    OneRosterService,
    ThresholdProfile,
    evaluate_thresholds,
)
from gamgui.components.oneroster.store import action_sequence_hash
from tests.test_oneroster_helpers import valid_files, zip_bytes


def test_thresholds_hold_on_count_percent_baseline_and_blackout() -> None:
    moment = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
    profile = ThresholdProfile(
        configured=True,
        limits={
            "student_remove": ActionLimit(max_count=10, max_percent=20),
            "teacher_remove": ActionLimit(max_percent=5),
        },
        blackouts=(
            BlackoutWindow(
                (moment - timedelta(hours=1)).isoformat(),
                (moment + timedelta(hours=1)).isoformat(),
                "opening day",
            ),
        ),
    )
    result = evaluate_thresholds(
        profile,
        {"student_remove": 25, "teacher_remove": 1},
        {"student_remove": 100, "teacher_remove": 0},
        now=moment,
    )
    assert result.held
    assert result.blackout
    assert {item.action for item in result.breaches} == {
        "student_remove",
        "teacher_remove",
        "blackout",
    }


def test_unconfigured_profile_holds_and_limited_profile_is_explicit() -> None:
    unconfigured = evaluate_thresholds(ThresholdProfile(), {}, {})
    assert unconfigured.held
    assert unconfigured.breaches[0].action == "profile"
    limited = evaluate_thresholds(
        ThresholdProfile(configured=True, limited_import=True),
        {"student_remove": 100},
        {"student_remove": 100},
    )
    assert not limited.held
    assert limited.limited_import


def test_service_accepts_web_profile_mapping_and_exposes_mode(tmp_path: Path) -> None:
    service = OneRosterService("example.org", tmp_path / "component")
    profile = service.save_threshold_profile(
        {
            "version": 1,
            "configured": True,
            "limited_import": True,
            "limits": {"student_remove": {"max_count": 5, "max_percent": 10}},
            "blackouts": [],
        }
    )
    assert profile.mode == "limited"
    assert service.get_threshold_profile().limits["student_remove"].max_count == 5


def _ready_service(tmp_path: Path) -> tuple[OneRosterService, str]:
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(zip_bytes(valid_files()))
    service.save_threshold_profile(
        ThresholdProfile(
            configured=True,
            limits={"student_remove": ActionLimit(max_count=0)},
        )
    )
    return service, snapshot.id


def test_held_manifest_requires_matching_reasoned_override(tmp_path: Path) -> None:
    service, import_id = _ready_service(tmp_path)
    evaluation = service.evaluate_thresholds(import_id, {"student_remove": 1})
    actions = (
        ImportAction("a1", "student_remove", "Section_101", "student@example.org"),
    )
    with pytest.raises(OneRosterError) as held:
        service.create_manifest(
            import_id,
            config_hash="config",
            live_hash="live",
            actions=actions,
            evaluation=evaluation,
        )
    assert held.value.code == "OR-THRESHOLD-HOLD"
    with pytest.raises(OneRosterError):
        service.record_override(import_id, evaluation, "expected rollover", "wrong")
    service.record_override(import_id, evaluation, "expected rollover", import_id)
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=actions,
        evaluation=evaluation,
    )
    assert manifest.actions == actions
    assert len(manifest.manifest_hash) == 64


def test_manifest_basis_is_immutable_results_are_resumable_and_limited_filters(
    tmp_path: Path,
) -> None:
    service, import_id = _ready_service(tmp_path)
    actions = (
        ImportAction("add", "student_add", "Section_101", "new@example.org"),
        ImportAction("remove", "student_remove", "Section_101", "old@example.org"),
        ImportAction("update", "course_update", "Section_101", "name"),
    )
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=actions,
        limited_import=True,
    )
    assert [item.id for item in manifest.actions] == ["add"]
    digest = manifest.manifest_hash
    updated = service.mark_action_result(
        manifest.id, "add", status="applied", detail="verified"
    )
    assert updated.manifest_hash == digest
    assert updated.actions[0].status == "applied"
    completed = service.finish_manifest(manifest.id, status="completed")
    assert completed.status == "completed"


def test_manifest_store_streams_pending_hash_counts_and_bounded_batches(
    tmp_path: Path,
) -> None:
    service, import_id = _ready_service(tmp_path)
    actions = tuple(
        ImportAction(
            f"action-{index:03d}",
            "student_add" if index % 2 else "teacher_add",
            f"Section_{index // 4:03d}",
            f"user-{index:03d}@example.org",
        )
        for index in range(137)
    )
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=actions,
        limited_import=False,
    )

    assert service.store.pending_actions_hash(manifest.id) == action_sequence_hash(actions)
    assert service.store.pending_action_summary(manifest.id) == {
        "total": 137,
        "student": 68,
        "nonstudent": 69,
    }
    assert service.store.pending_action_kinds(manifest.id) == (
        "student_add",
        "teacher_add",
    )
    batch = service.store.get_pending_action_batch(
        manifest.id,
        kinds=("teacher_add",),
        limit=10_000,
    )
    assert len(batch) == 50
    assert all(action.kind == "teacher_add" for action in batch)

    for action in batch:
        service.store.mark_action_result(
            manifest.id,
            action.id,
            status="applied",
            load_manifest=False,
        )
    summary = service.store.pending_action_summary(manifest.id)
    assert summary == {"total": 87, "student": 68, "nonstudent": 19}


def test_manifest_lifecycle_metadata_paths_never_materialize_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, import_id = _ready_service(tmp_path)
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(
            ImportAction("add", "student_add", "Section_101", "new@example.org"),
        ),
    )

    def fail_full_load(_manifest_id: str):
        raise AssertionError("manifest lifecycle loaded every action")

    monkeypatch.setattr(service.store, "get_manifest", fail_full_load)
    confirmed = service.store.confirm_manifest(manifest.id, import_id)
    assert confirmed.confirmed and confirmed.actions == ()
    claimed = service.store.claim_manifest(manifest.id)
    assert claimed.status == "running" and claimed.actions == ()
    prepared = service.store.record_prepared_live_hash(manifest.id, "a" * 64)
    assert prepared.prepared_live_hash == "a" * 64
    finished = service.store.finish_manifest(
        manifest.id,
        status="awaiting_students",
        load_actions=False,
    )
    assert finished.status == "awaiting_students" and finished.actions == ()


def test_duplicate_manifest_action_ids_roll_back_the_manifest(tmp_path: Path) -> None:
    service, import_id = _ready_service(tmp_path)
    before = service.purge_preview().manifest_count

    with pytest.raises(ValueError, match="unique"):
        service.create_manifest(
            import_id,
            config_hash="config",
            live_hash="live",
            actions=(
                ImportAction("duplicate", "teacher_add", "Section_101", "a@example.org"),
                ImportAction("duplicate", "student_add", "Section_101", "b@example.org"),
            ),
        )

    assert service.purge_preview().manifest_count == before


def test_gate_arms_in_chicago_refuses_early_open_and_closes_on_drift(
    tmp_path: Path,
) -> None:
    service, import_id = _ready_service(tmp_path)
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(ImportAction("add", "student_add", "Section_101", "new@example.org"),),
    )
    now = datetime.now(timezone.utc)
    release = now + timedelta(minutes=10)
    service.store.confirm_manifest(manifest.id, import_id)
    service.finish_manifest(manifest.id, status="awaiting_students")
    armed = service.arm_gate(manifest.id, manifest.manifest_hash, release.isoformat())
    assert armed.state is GateState.ARMED
    assert armed.timezone == "America/Chicago"
    with pytest.raises(OneRosterError) as early:
        service.open_gate(
            manifest.id,
            manifest.manifest_hash,
            manifest.manifest_hash,
            now=now,
        )
    assert early.value.code == "OR-GATE-NOT-DUE"

    drifted = service.open_gate(
        manifest.id,
        manifest.manifest_hash,
        "different",
        now=release + timedelta(seconds=1),
    )
    assert drifted.state is GateState.CLOSED
    assert drifted.hold_code == "OR-GATE-DRIFT"


def test_gate_opens_only_for_exact_persisted_manifest(tmp_path: Path) -> None:
    service, import_id = _ready_service(tmp_path)
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(ImportAction("add", "student_add", "Section_101", "new@example.org"),),
    )
    now = datetime.now(timezone.utc)
    service.store.confirm_manifest(manifest.id, import_id)
    service.finish_manifest(manifest.id, status="awaiting_students")
    armed = service.arm_gate(manifest.id, manifest.manifest_hash, now.isoformat())
    opened = service.open_gate(
        manifest.id,
        manifest.manifest_hash,
        manifest.manifest_hash,
        now=now + timedelta(seconds=1),
    )
    assert opened.state is GateState.OPEN
    assert service.close_gate().state is GateState.CLOSED


def test_purge_requires_exact_confirmation_and_reinitializes_store(tmp_path: Path) -> None:
    service, _ = _ready_service(tmp_path)
    preview = service.purge_preview()
    assert preview.snapshot_count == 1
    with pytest.raises(OneRosterError):
        service.purge_data("oneroster")
    removed = service.purge_data("OneRoster")
    assert removed.snapshot_count == 1
    assert service.history() == ()
