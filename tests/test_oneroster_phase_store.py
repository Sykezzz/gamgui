from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from gamgui.components.oneroster import OneRosterError, OneRosterService, ThresholdProfile
from gamgui.components.oneroster.models import ImportAction
from tests.test_oneroster_helpers import valid_files, zip_bytes


def _ready_service(tmp_path: Path) -> tuple[OneRosterService, str]:
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(zip_bytes(valid_files()))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    service.mark_scope_ready()
    return service, snapshot.id


def _claimed_run(service: OneRosterService, manifest, *, owner: str = "phase-owner"):
    service.store.confirm_manifest(manifest.id, manifest.import_id)
    service.store.claim_manifest(manifest.id, owner_id=owner)
    return service.store.start_execution_run(
        manifest.id,
        phase="phase-test",
        owner_id=owner,
    )


def test_phase_receipts_have_action_lookup_index(tmp_path: Path):
    service = OneRosterService("example.org", tmp_path / "component")

    with sqlite3.connect(service.store.state_path) as conn:
        columns = tuple(
            row[2]
            for row in conn.execute(
                "PRAGMA index_info(execution_batch_actions_action_receipt)"
            )
        )

    assert columns == ("action_id", "result_status", "batch_id")


def test_whole_phase_persists_submits_reconciles_and_retries(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    actions = tuple(
        ImportAction(
            id=f"student-{index:03d}",
            kind="student_add",
            subject="Section_101",
            target=f"student-{index:03d}@example.org",
        )
        for index in range(120)
    )
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=actions,
        limited_import=True,
        plan_kind="limited",
    )
    owner = "phase-owner"
    run = _claimed_run(service, manifest, owner=owner)

    phase = service.store.prepare_execution_phase(
        run.id,
        manifest.id,
        phase="student_add",
        kinds=("student_add",),
        owner_id=owner,
    )
    assert phase is not None
    assert phase.action_count == 120
    assert phase.action_ids == ()
    assert phase.execution_mode == "additions_first"
    with sqlite3.connect(service.store.state_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM execution_batch_actions WHERE batch_id = ?",
            (phase.id,),
        ).fetchone()[0] == 120

    started = service.store.mark_execution_phase_started(phase.id, owner_id=owner)
    assert started.status == "running"
    progress = service.store.record_execution_phase_progress(
        phase.id,
        37,
        120,
        owner_id=owner,
    )
    assert (progress.native_progress_count, progress.native_progress_total) == (37, 120)
    with pytest.raises(OneRosterError, match="non-monotonic"):
        service.store.record_execution_phase_progress(
            phase.id,
            36,
            120,
            owner_id=owner,
        )

    submitted = service.store.mark_execution_phase_submitted(
        phase.id,
        manifest.id,
        owner_id=owner,
        apply_seconds=2.5,
    )
    assert submitted.status == "submitted"
    assert service.store.execution_phase_action_counts(phase.id) == {
        "submitted": 120,
        "total": 120,
    }

    page = service.store.get_execution_phase_action_chunk(phase.id, limit=50)
    assert len(page) == 50
    second = service.store.get_execution_phase_action_chunk(
        phase.id,
        after_ordinal=page[-1][0],
        limit=100,
    )
    assert len(second) == 70
    ordered = (*page, *second)
    results = {
        action.id: (
            ("pending", "Retry after bounded reconciliation.")
            if ordinal == 119
            else ("applied", "Verified live.")
        )
        for ordinal, action in ordered
    }
    service.store.promote_submitted_phase_actions(
        phase.id,
        manifest.id,
        results,
        owner_id=owner,
    )
    finished = service.store.finish_execution_phase_reconciliation(
        phase.id,
        manifest.id,
        owner_id=owner,
    )
    assert finished.status == "reconciled"

    retry = service.store.prepare_execution_phase(
        run.id,
        manifest.id,
        phase="student_add",
        kinds=("student_add",),
        owner_id=owner,
    )
    assert retry is not None
    assert retry.action_count == 1


def test_uncertain_running_phase_marks_every_member_submitted(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=tuple(
            ImportAction(f"create-{index}", "course_create", f"Section_{index}", "")
            for index in range(3)
        ),
        limited_import=True,
        plan_kind="limited",
    )
    owner = "phase-owner"
    run = _claimed_run(service, manifest, owner=owner)
    phase = service.store.prepare_execution_phase(
        run.id,
        manifest.id,
        phase="course_create",
        kinds=("course_create",),
        owner_id=owner,
    )
    assert phase is not None
    service.store.mark_execution_phase_started(phase.id, owner_id=owner)

    reconciling = service.store.mark_execution_phase_reconciling(
        phase.id,
        error_code="OR-NATIVE-FAILED",
        owner_id=owner,
    )
    assert reconciling.status == "reconciling"
    assert service.store.execution_phase_action_counts(phase.id) == {
        "submitted": 3,
        "total": 3,
    }


def test_checkpoint_uses_per_membership_results_after_mixed_phase_retry(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    actions = tuple(
        ImportAction(
            f"create-{index}",
            "course_create",
            f"Section_{index}",
            "teacher@example.org",
            after=json.dumps(
                {
                    "alias": f"Section_{index}",
                    "name": f"Course {index}",
                    "owner_email": "teacher@example.org",
                },
                sort_keys=True,
            ),
        )
        for index in range(2)
    )
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=actions,
        limited_import=True,
        plan_kind="limited",
    )
    owner = "phase-owner"
    run = _claimed_run(service, manifest, owner=owner)
    first = service.store.prepare_execution_phase(
        run.id,
        manifest.id,
        phase="course_create",
        kinds=("course_create",),
        owner_id=owner,
    )
    assert first is not None
    service.store.mark_execution_phase_started(first.id, owner_id=owner)
    service.store.mark_execution_phase_submitted(
        first.id,
        manifest.id,
        owner_id=owner,
        apply_seconds=1.0,
    )
    service.store.promote_submitted_phase_actions(
        first.id,
        manifest.id,
        {
            "create-0": ("applied", "Verified live."),
            "create-1": ("pending", "Retry required."),
        },
        owner_id=owner,
    )
    assert service.store.finish_execution_phase_reconciliation(
        first.id,
        manifest.id,
        owner_id=owner,
    ).status == "reconciled"

    retry = service.store.prepare_execution_phase(
        run.id,
        manifest.id,
        phase="course_create",
        kinds=("course_create",),
        owner_id=owner,
    )
    assert retry is not None and retry.action_count == 1
    service.store.mark_execution_phase_started(retry.id, owner_id=owner)
    service.store.mark_execution_phase_submitted(
        retry.id,
        manifest.id,
        owner_id=owner,
        apply_seconds=0.5,
    )
    service.store.promote_submitted_phase_actions(
        retry.id,
        manifest.id,
        {"create-1": ("applied", "Verified on retry.")},
        owner_id=owner,
    )
    assert service.store.finish_execution_phase_reconciliation(
        retry.id,
        manifest.id,
        owner_id=owner,
    ).status == "completed"

    with sqlite3.connect(service.store.state_path) as conn:
        receipts = conn.execute(
            """
            SELECT batch_id, result_status FROM execution_batch_actions
            WHERE action_id = 'create-1' ORDER BY rowid
            """
        ).fetchall()
    assert receipts == [(first.id, "pending"), (retry.id, "applied")]

    service.store.request_execution_pause(manifest.id)
    service.store.finish_manifest(
        manifest.id,
        status="paused",
        error="OR-EXECUTION-PAUSED",
        load_actions=False,
        owner_id=owner,
    )
    service.store.finish_execution_run(
        run.id,
        status="paused",
        error_code="OR-EXECUTION-PAUSED",
        phase="paused",
    )
    checkpoint = service.store.validate_additions_first_checkpoint(manifest.id)
    assert checkpoint["action_counts"] == {"applied": 2, "total": 2}


def test_checkpoint_accepts_legacy_blank_verified_receipt(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    action = ImportAction(
        "create",
        "course_create",
        "Section_101",
        "teacher@example.org",
        after=json.dumps(
            {
                "alias": "Section_101",
                "name": "Algebra",
                "owner_email": "teacher@example.org",
            },
            sort_keys=True,
        ),
    )
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(action,),
        limited_import=True,
        plan_kind="limited",
    )
    owner = "phase-owner"
    run = _claimed_run(service, manifest, owner=owner)
    batch = service.store.prepare_execution_batch(
        run.id,
        manifest.id,
        (action,),
        phase="course_create",
        owner_id=owner,
    )
    service.store.mark_execution_batch_started(batch.id)
    service.store.complete_verified_batch(
        batch.id,
        manifest.id,
        {"create": ("applied", "Verified live.")},
        owner_id=owner,
        apply_seconds=1.0,
        verification_seconds=0.5,
        verification_attempts=1,
        worker_count=1,
    )
    with sqlite3.connect(service.store.state_path) as conn:
        conn.execute(
            "UPDATE execution_batch_actions SET result_status = '' WHERE batch_id = ?",
            (batch.id,),
        )

    service.store.request_execution_pause(manifest.id)
    service.store.finish_manifest(
        manifest.id,
        status="paused",
        error="OR-EXECUTION-PAUSED",
        load_actions=False,
        owner_id=owner,
    )
    service.store.finish_execution_run(
        run.id,
        status="paused",
        error_code="OR-EXECUTION-PAUSED",
        phase="paused",
    )

    checkpoint = service.store.validate_additions_first_checkpoint(manifest.id)
    assert checkpoint["action_counts"] == {"applied": 1, "total": 1}


def test_checkpoint_integrity_and_streamed_missing_report(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    payload = json.dumps(
        {
            "alias": "Section_101",
            "name": "Algebra",
            "owner_email": "teacher@example.org",
        },
        sort_keys=True,
    )
    actions = (
        ImportAction(
            "create",
            "course_create",
            "Section_101",
            "teacher@example.org",
            after=payload,
        ),
        ImportAction(
            "student",
            "student_add",
            "Section_101",
            "student@example.org",
        ),
        ImportAction("activate", "course_activate", "Section_101", ""),
        ImportAction(
            "teacher",
            "teacher_add",
            "Section_101",
            "teacher@example.org",
        ),
    )
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=actions,
        limited_import=True,
        plan_kind="limited",
    )
    owner = "phase-owner"
    run = _claimed_run(service, manifest, owner=owner)
    phase = service.store.prepare_execution_phase(
        run.id,
        manifest.id,
        phase="bootstrap",
        kinds=("course_create", "student_add", "course_activate", "teacher_add"),
        owner_id=owner,
    )
    assert phase is not None
    service.store.mark_execution_phase_started(phase.id, owner_id=owner)
    service.store.mark_execution_phase_submitted(
        phase.id,
        manifest.id,
        owner_id=owner,
        apply_seconds=1.0,
    )
    phase_actions = service.store.get_execution_phase_action_chunk(phase.id)
    service.store.promote_submitted_phase_actions(
        phase.id,
        manifest.id,
        {
            action.id: ("applied", "Verified live.")
            for _ordinal, action in phase_actions
        },
        owner_id=owner,
    )
    service.store.finish_execution_phase_reconciliation(
        phase.id,
        manifest.id,
        owner_id=owner,
    )
    service.store.request_execution_pause(manifest.id)
    service.store.finish_manifest(
        manifest.id,
        status="paused",
        error="OR-EXECUTION-PAUSED",
        load_actions=False,
        owner_id=owner,
    )
    service.store.finish_execution_run(
        run.id,
        status="paused",
        error_code="OR-EXECUTION-PAUSED",
        phase="paused",
    )

    checkpoint = service.store.validate_additions_first_checkpoint(manifest.id)
    assert checkpoint["prior_run_id"] == run.id
    assert checkpoint["action_counts"] == {"applied": 4, "total": 4}

    assert service.store.replace_bootstrap_missing_report(
        manifest.id,
        (
            {"action_id": action.id, "reason": "Missing live state", "cycle": 2}
            for action in actions[:2]
        ),
        chunk_size=1,
    ) == 2
    assert service.store.bootstrap_missing_report_count(manifest.id) == 2
    output = io.StringIO()
    assert service.store.write_bootstrap_missing_report(manifest.id, output) == 2
    report = output.getvalue()
    assert "action_id,kind,subject,target,before,after,status,detail,reason,cycle" in report
    assert "Missing live state" in report
    assert service.store.get_manifest_header(manifest.id).status == "paused"


def test_paused_submitted_phase_attaches_to_new_attempt_for_reconciliation(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    actions = (
        ImportAction(
            "create",
            "course_create",
            "Section_101",
            "teacher@example.org",
            after=json.dumps(
                {
                    "alias": "Section_101",
                    "name": "Algebra",
                    "owner_email": "teacher@example.org",
                },
                sort_keys=True,
            ),
        ),
    )
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=actions,
        limited_import=True,
        plan_kind="limited",
    )
    owner = "phase-owner"
    first_run = _claimed_run(service, manifest, owner=owner)
    phase = service.store.prepare_execution_phase(
        first_run.id,
        manifest.id,
        phase="course_create",
        kinds=("course_create",),
        owner_id=owner,
    )
    assert phase is not None
    service.store.mark_execution_phase_started(phase.id, owner_id=owner)
    service.store.mark_execution_phase_submitted(
        phase.id,
        manifest.id,
        owner_id=owner,
        apply_seconds=1.0,
    )
    service.store.request_execution_pause(manifest.id)
    service.store.finish_manifest(
        manifest.id,
        status="paused",
        error="OR-BOOTSTRAP-PAUSED",
        load_actions=False,
        owner_id=owner,
    )
    service.store.finish_execution_run(
        first_run.id,
        status="paused",
        error_code="OR-BOOTSTRAP-PAUSED",
        phase="paused",
    )
    checkpoint = service.store.validate_additions_first_checkpoint(manifest.id)
    assert checkpoint["submitted_batch_ids"] == (phase.id,)

    service.store.claim_manifest(
        manifest.id,
        allow_paused=True,
        owner_id=owner,
    )
    second_run = service.store.start_execution_run(
        manifest.id,
        phase="resume",
        owner_id=owner,
    )
    attached = service.store.attach_paused_additions_first_reconciliation(
        phase.id,
        manifest.id,
        second_run.id,
        owner_id=owner,
    )
    assert attached.status == "reconciling"
    service.store.promote_submitted_phase_actions(
        phase.id,
        manifest.id,
        {"create": ("applied", "Verified after resume.")},
        owner_id=owner,
    )
    finished = service.store.finish_execution_phase_reconciliation(
        phase.id,
        manifest.id,
        owner_id=owner,
    )
    assert finished.status == "completed"


def test_dispatch_boundary_pauses_without_resending_undispatched_suffix(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    course_payload = json.dumps(
        {
            "alias": "Section_101",
            "name": "Algebra",
            "owner_email": "teacher@example.org",
        },
        sort_keys=True,
    )
    actions = (
        ImportAction(
            "create",
            "course_create",
            "Section_101",
            "teacher@example.org",
            after=course_payload,
        ),
        *(
            ImportAction(
                f"student-{index}",
                "student_add",
                "Section_101",
                f"student-{index}@example.org",
            )
            for index in range(5)
        ),
    )
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=actions,
        limited_import=True,
        plan_kind="limited",
    )
    owner = "phase-owner"
    first_run = _claimed_run(service, manifest, owner=owner)

    course_phase = service.store.prepare_execution_phase(
        first_run.id,
        manifest.id,
        phase="course_create",
        kinds=("course_create",),
        owner_id=owner,
    )
    assert course_phase is not None
    service.store.mark_execution_phase_started(course_phase.id, owner_id=owner)
    service.store.mark_execution_phase_submitted(
        course_phase.id,
        manifest.id,
        owner_id=owner,
        apply_seconds=1.0,
    )
    service.store.mark_execution_phase_reconciling(
        course_phase.id,
        error_code="OR-EXECUTION-INTERRUPTED",
        owner_id=owner,
    )

    student_phase = service.store.prepare_execution_phase(
        first_run.id,
        manifest.id,
        phase="student_add",
        kinds=("student_add",),
        owner_id=owner,
    )
    assert student_phase is not None and student_phase.action_count == 5
    service.store.mark_execution_phase_started(student_phase.id, owner_id=owner)
    service.store.record_execution_phase_progress(
        student_phase.id,
        3,
        5,
        owner_id=owner,
    )
    recovered_phase = service.store.mark_execution_phase_reconciling(
        student_phase.id,
        error_code="OR-EXECUTION-INTERRUPTED",
        owner_id=owner,
    )
    assert recovered_phase.status == "reconciling"
    original_hash = recovered_phase.action_ids_hash
    service.store.finish_manifest(
        manifest.id,
        status="recovery_required",
        error="OR-RECOVERY-REQUIRED",
        load_actions=False,
        owner_id=owner,
    )
    service.store.finish_execution_run(
        first_run.id,
        status="recovery_required",
        error_code="OR-RECOVERY-REQUIRED",
        phase="reconciliation",
    )

    with pytest.raises(OneRosterError) as wrong_boundary:
        service.pause_additions_first_at_dispatch_boundary(
            manifest.id,
            student_phase.id,
            2,
        )
    assert wrong_boundary.value.code == "OR-PHASE-PROGRESS-INVALID"

    boundary = service.pause_additions_first_at_dispatch_boundary(
        manifest.id,
        student_phase.id,
        3,
    )
    assert boundary == {
        "manifest_id": manifest.id,
        "run_id": first_run.id,
        "batch_id": student_phase.id,
        "action_count": 5,
        "dispatched_count": 3,
        "pending_count": 2,
        "idempotent": False,
    }
    repeated = service.pause_additions_first_at_dispatch_boundary(
        manifest.id,
        student_phase.id,
        3,
    )
    assert repeated["idempotent"] is True

    with sqlite3.connect(service.store.state_path) as conn:
        student_receipts = conn.execute(
            """
            SELECT membership.ordinal, membership.result_status, action.status
            FROM execution_batch_actions AS membership
            JOIN manifest_actions AS action
              ON action.manifest_id = ?
             AND action.action_id = membership.action_id
            WHERE membership.batch_id = ? ORDER BY membership.ordinal
            """,
            (manifest.id, student_phase.id),
        ).fetchall()
        stored_student_phase = conn.execute(
            """
            SELECT action_count, action_ids_hash, status
            FROM execution_batches WHERE id = ?
            """,
            (student_phase.id,),
        ).fetchone()
        stored_course_phase = conn.execute(
            """
            SELECT status, action_count, action_ids_hash
            FROM execution_batches WHERE id = ?
            """,
            (course_phase.id,),
        ).fetchone()
        stored_run = conn.execute(
            """
            SELECT status, phase, stop_requested, last_error_code
            FROM execution_runs WHERE id = ?
            """,
            (first_run.id,),
        ).fetchone()
    assert student_receipts == [
        (0, "submitted", "submitted"),
        (1, "submitted", "submitted"),
        (2, "submitted", "submitted"),
        (3, "pending", "pending"),
        (4, "pending", "pending"),
    ]
    assert stored_student_phase == (5, original_hash, "reconciling")
    assert stored_course_phase == (
        "reconciling",
        course_phase.action_count,
        course_phase.action_ids_hash,
    )
    assert stored_run == ("paused", "paused", 1, "OR-BOOTSTRAP-PAUSED")
    checkpoint = service.store.validate_additions_first_checkpoint(manifest.id)
    assert checkpoint["action_counts"] == {
        "pending": 2,
        "submitted": 4,
        "total": 6,
    }
    assert checkpoint["submitted_batch_ids"] == (
        course_phase.id,
        student_phase.id,
    )

    resumed_owner = "resumed-owner"
    service.store.claim_manifest(
        manifest.id,
        allow_paused=True,
        owner_id=resumed_owner,
    )
    resumed_run = service.store.start_execution_run(
        manifest.id,
        phase="bootstrap",
        owner_id=resumed_owner,
    )
    for batch_id in checkpoint["submitted_batch_ids"]:
        service.store.attach_paused_additions_first_reconciliation(
            batch_id,
            manifest.id,
            resumed_run.id,
            owner_id=resumed_owner,
        )
    submitted_prefix = service.store.get_execution_phase_action_chunk(
        student_phase.id,
        result_statuses=("submitted",),
    )
    assert [action.id for _ordinal, action in submitted_prefix] == [
        "student-0",
        "student-1",
        "student-2",
    ]
    retry = service.store.prepare_execution_phase(
        resumed_run.id,
        manifest.id,
        phase="student_add",
        kinds=("student_add",),
        owner_id=resumed_owner,
    )
    assert retry is not None and retry.action_count == 2
    retry_actions = service.store.get_execution_phase_action_chunk(retry.id)
    assert [action.id for _ordinal, action in retry_actions] == [
        "student-3",
        "student-4",
    ]

    service.store.mark_execution_phase_started(retry.id, owner_id=resumed_owner)
    service.store.mark_execution_phase_submitted(
        retry.id,
        manifest.id,
        owner_id=resumed_owner,
        apply_seconds=0.5,
    )
    service.store.promote_submitted_phase_actions(
        student_phase.id,
        manifest.id,
        {
            action.id: ("applied", "Verified after boundary recovery.")
            for _ordinal, action in submitted_prefix
        },
        owner_id=resumed_owner,
    )
    reconciled = service.store.finish_execution_phase_reconciliation(
        student_phase.id,
        manifest.id,
        owner_id=resumed_owner,
    )
    assert reconciled.status == "reconciled"
    assert reconciled.action_count == 5
    assert reconciled.action_ids_hash == original_hash


def _run_for_read_progress(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(
            ImportAction(
                id="student-000",
                kind="student_add",
                subject="Section_101",
                target="student-000@example.org",
            ),
        ),
        limited_import=True,
        plan_kind="limited",
    )
    return service, manifest, _claimed_run(service, manifest)


def _stored_run(service, manifest):
    return service.store.get_execution_progress(manifest.id).run


def test_read_progress_persists_and_renews_heartbeat(tmp_path: Path):
    """A long bulk read must be visibly distinguishable from a hang."""
    service, manifest, run = _run_for_read_progress(tmp_path)

    service.store.record_read_progress(run.id, 250, 9189, now=5_000.0)
    stored = _stored_run(service, manifest)

    assert stored.read_progress_count == 250
    assert stored.read_progress_total == 9189
    assert stored.read_progress_updated_at == 5_000.0
    # The same write proves liveness, so a moving read can never look stale.
    assert stored.last_heartbeat_at == 5_000.0


def test_read_progress_is_monotonic_within_one_read(tmp_path: Path):
    service, manifest, run = _run_for_read_progress(tmp_path)

    service.store.record_read_progress(run.id, 500, 9189, now=5_000.0)
    service.store.record_read_progress(run.id, 120, 9189, now=5_010.0)

    assert _stored_run(service, manifest).read_progress_count == 500


def test_read_progress_restarts_when_the_total_changes(tmp_path: Path):
    """A different total is a different read, so the counter starts over."""
    service, manifest, run = _run_for_read_progress(tmp_path)

    service.store.record_read_progress(run.id, 500, 9189, now=5_000.0)
    service.store.record_read_progress(run.id, 10, 40, now=5_010.0)

    stored = _stored_run(service, manifest)
    assert (stored.read_progress_count, stored.read_progress_total) == (10, 40)


def test_read_progress_clamps_overrun_and_ignores_unknown_runs(tmp_path: Path):
    service, manifest, run = _run_for_read_progress(tmp_path)

    service.store.record_read_progress(run.id, 99_999, 9189, now=5_000.0)
    assert _stored_run(service, manifest).read_progress_count == 9189

    # An unknown run is a no-op rather than an error; observability must not raise
    # into the read path it is observing.
    service.store.record_read_progress("no-such-run", 5, 10, now=5_020.0)


def test_clear_read_progress_stops_a_finished_read_looking_in_flight(tmp_path: Path):
    service, manifest, run = _run_for_read_progress(tmp_path)

    service.store.record_read_progress(run.id, 9189, 9189, now=5_000.0)
    service.store.clear_read_progress(run.id, now=5_030.0)

    stored = _stored_run(service, manifest)
    assert stored.read_progress_count == 0
    assert stored.read_progress_total == 0
