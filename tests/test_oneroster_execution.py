from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gamgui.components.oneroster import (
    GateState,
    OneRosterError,
    OneRosterService,
    ThresholdProfile,
)
from gamgui.components.oneroster.executor import OneRosterExecutor, _AdaptiveWorkerTuner
from gamgui.components.oneroster.models import ImportAction
from gamgui.core.audit import AuditLog
from gamgui.core.activity import ActivityRegistry
from gamgui.core.classroom.models import (
    CourseDetail,
    CourseParticipant,
    CourseRosterSnapshot,
)
from gamgui.core.connectors.gam_connector import (
    GAMConnector,
    _is_allowed_classroom_batch_command,
)
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.models import GAMUser
from tests.test_oneroster_helpers import valid_files, zip_bytes


class FakeClassroom:
    oneroster_stabilization_delays = (0.0, 0.0)

    def __init__(self) -> None:
        self.users = {
            "teacher@example.org": GAMUser(
                "teacher@example.org",
                user_id="teacher-id",
            ),
            "student@example.org": GAMUser(
                "student@example.org",
                user_id="student-id",
            ),
            "oldowner@example.org": GAMUser(
                "oldowner@example.org",
                user_id="oldowner-id",
            ),
            "obsolete@example.org": GAMUser(
                "obsolete@example.org",
                user_id="obsolete-id",
            ),
        }
        self.courses: dict[str, CourseDetail] = {}
        self.teachers: dict[str, set[str]] = {}
        self.students: dict[str, set[str]] = {}
        self.directory_reads: list[str] = []
        self.batches: list[list[list[str]]] = []

    async def get_user(self, email: str, fields=None):
        self.directory_reads.append(email)
        try:
            return self.users[email.casefold()]
        except KeyError:
            raise KeyError(email) from None

    async def get_course(self, course_id: str, **_kwargs):
        alias = course_id.removeprefix("d:")
        try:
            return self.courses[alias]
        except KeyError:
            raise KeyError(alias) from None

    async def list_course_participants(self, course_id: str, role: str):
        source = self.teachers if role == "teachers" else self.students
        return [
            CourseParticipant(course_id=course_id, email=email, role=role)
            for email in sorted(source.get(course_id, set()))
        ]

    async def list_course_participants_many(self, course_ids, role="all"):
        requested = tuple(course_ids)
        participants = []
        for course_id in requested:
            if role in {"all", "teachers"}:
                participants.extend(await self.list_course_participants(course_id, "teachers"))
            if role in {"all", "students"}:
                participants.extend(await self.list_course_participants(course_id, "students"))
        return CourseRosterSnapshot.from_participants(
            participants,
            requested,
        )

    async def run_classroom_batch(self, commands, *, max_commands=50):
        assert 0 < len(commands) <= max_commands <= 50
        self.batches.append([list(command) for command in commands])
        for command in commands:
            self._apply(list(command))

    def add_course(
        self,
        alias: str,
        *,
        owner: str,
        name: str = "Algebra I \u2013 P1 (2026-27)",
        section: str = "P1",
        room: str = "101",
        state: str = "ACTIVE",
    ) -> CourseDetail:
        course_id = str(1000 + len(self.courses))
        detail = CourseDetail(
            id=course_id,
            name=name,
            section=section,
            room=room,
            owner_id=self.users.get(owner, GAMUser(owner)).user_id,
            owner_email=owner,
            course_state=state,
            aliases=(f"d:{alias}",),
        )
        self.courses[alias] = detail
        self.teachers[course_id] = {owner}
        self.students[course_id] = set()
        return detail

    def _apply(self, command: list[str]) -> None:
        if command[:2] == ["create", "course"]:
            alias = command[command.index("alias") + 1]
            owner = command[command.index("teacher") + 1]
            detail = self.add_course(
                alias,
                owner=owner,
                name=command[command.index("name") + 1],
                section=command[command.index("section") + 1] if "section" in command else "",
                room=command[command.index("room") + 1] if "room" in command else "",
                state=command[command.index("state") + 1].upper(),
            )
            self.courses[alias] = detail
            return
        alias = command[2].removeprefix("d:") if command[:2] == ["update", "course"] else command[1].removeprefix("d:")
        detail = self.courses[alias]
        if command[:2] == ["update", "course"]:
            if "teacher" in command:
                owner = command[command.index("teacher") + 1]
                self.teachers[detail.id].add(owner)
                self.courses[alias] = _replace_detail(
                    detail,
                    owner_email=owner,
                    owner_id=self.users.get(owner, GAMUser(owner)).user_id,
                )
            elif "state" in command:
                state = command[command.index("state") + 1].upper()
                self.courses[alias] = _replace_detail(detail, course_state=state)
            else:
                self.courses[alias] = _replace_detail(
                    detail,
                    name=command[command.index("name") + 1],
                    section=command[command.index("section") + 1],
                    room=command[command.index("room") + 1],
                )
            return
        operation, role, email = command[2], command[3], command[4]
        members = self.teachers[detail.id] if role == "teachers" else self.students[detail.id]
        if operation == "add":
            members.add(email)
        else:
            members.discard(email)


class BulkExecutionClassroom(FakeClassroom):
    def __init__(self) -> None:
        super().__init__()
        self.directory_exports = 0

    async def list_oneroster_directory(self):
        self.directory_exports += 1
        return dict(self.users)

    async def list_oneroster_managed_courses(self, aliases):
        requested = {str(alias).removeprefix("d:").casefold() for alias in aliases}
        return [
            detail
            for alias, detail in self.courses.items()
            if alias.casefold() in requested
        ]


class BatchAndRosterFailure(FakeClassroom):
    def __init__(self) -> None:
        super().__init__()
        self.fail_verification_roster = False

    async def run_classroom_batch(self, commands, *, max_commands=50):
        assert 0 < len(commands) <= max_commands <= 50
        self.batches.append([list(command) for command in commands])
        self.fail_verification_roster = True
        raise RuntimeError("controlled batch failure")

    async def list_course_participants_many(self, course_ids, role="all"):
        if self.fail_verification_roster:
            raise RuntimeError("controlled roster reread failure")
        return await super().list_course_participants_many(course_ids, role)


class IncompleteVerificationRoster(FakeClassroom):
    def __init__(self) -> None:
        super().__init__()
        self.omit_verification_course = False

    async def run_classroom_batch(self, commands, *, max_commands=50):
        await super().run_classroom_batch(commands, max_commands=max_commands)
        self.omit_verification_course = True

    async def list_course_participants_many(self, course_ids, role="all"):
        if self.omit_verification_course:
            return CourseRosterSnapshot.empty()
        return await super().list_course_participants_many(course_ids, role)


def _replace_detail(detail: CourseDetail, **changes: str) -> CourseDetail:
    values = {
        "id": detail.id,
        "name": detail.name,
        "section": detail.section,
        "room": detail.room,
        "owner_id": detail.owner_id,
        "course_state": detail.course_state,
        "creation_time": detail.creation_time,
        "update_time": detail.update_time,
        "alternate_link": detail.alternate_link,
        "owner_email": detail.owner_email,
        "description_heading": detail.description_heading,
        "description": detail.description,
        "aliases": detail.aliases,
        "raw": detail.raw,
    }
    values.update(changes)
    return CourseDetail(**values)


def _ready_service(tmp_path: Path) -> tuple[OneRosterService, str]:
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(zip_bytes(valid_files()))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    service.mark_scope_ready()
    return service, snapshot.id


def _claim_for_executor(
    service: OneRosterService,
    manifest,
    executor: OneRosterExecutor,
) -> None:
    service.store.confirm_manifest(manifest.id, manifest.import_id)
    service.store.claim_manifest(
        manifest.id,
        owner_id=executor._operation_owner,
        owner_identity=executor._operation_identity,
    )


def test_execution_batches_persist_exact_membership_and_progress(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(
            ImportAction("a-1", "student_add", "Section_101", "one@example.org"),
            ImportAction("a-2", "student_add", "Section_101", "two@example.org"),
        ),
    )
    executor = OneRosterExecutor(service.store, FakeClassroom())
    _claim_for_executor(service, manifest, executor)
    run = service.store.start_execution_run(
        manifest.id,
        phase="student_add",
        owner_id=executor._operation_owner,
        now=100.0,
    )

    batch = service.store.prepare_execution_batch(
        run.id,
        manifest.id,
        manifest.actions,
        phase="student_add",
        owner_id=executor._operation_owner,
        now=101.0,
    )
    started = service.store.mark_execution_batch_started(batch.id, now=102.0)

    assert started.status == "running"
    assert started.action_ids == ("a-1", "a-2")
    with sqlite3.connect(service.store.state_path) as conn:
        rows = conn.execute(
            """
            SELECT action_id, ordinal FROM execution_batch_actions
            WHERE batch_id = ? ORDER BY ordinal
            """,
            (batch.id,),
        ).fetchall()
    assert rows == [("a-1", 0), ("a-2", 1)]
    progress = service.store.get_execution_progress(manifest.id, now=112.0)
    assert progress.run is not None
    assert progress.run.current_batch_sequence == 1
    assert progress.pending == 2
    assert progress.percent == 0
    assert progress.heartbeat_age_seconds == 10
    assert progress.heartbeat_state == "current"
    assert progress.current_batch is not None
    assert progress.current_batch.course_count == 1


def test_execution_progress_projects_operator_phases_and_unknown_work(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    actions = (
        ImportAction("course", "course_update", "Section_101", "course"),
        ImportAction("teacher", "owner_transfer", "Section_102", "owner@example.org"),
        ImportAction("student", "student_add", "Section_103", "student@example.org"),
        ImportAction("other", "future_action", "Section_104", "safe-value"),
        ImportAction("waiting", "student_add", "Section_105", "waiting@example.org"),
    )
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=actions,
    )
    executor = OneRosterExecutor(service.store, FakeClassroom())
    _claim_for_executor(service, manifest, executor)
    run = service.store.start_execution_run(
        manifest.id,
        phase="course_update",
        owner_id=executor._operation_owner,
        now=100.0,
    )
    batch = service.store.prepare_execution_batch(
        run.id,
        manifest.id,
        actions[:4],
        phase="course_update",
        owner_id=executor._operation_owner,
        now=101.0,
    )
    service.store.mark_execution_batch_started(batch.id, now=102.0)
    service.store.complete_verified_batch(
        batch.id,
        manifest.id,
        {
            "course": ("applied", "verified"),
            "teacher": ("failed", "OR-BATCH-VERIFY-FAILED"),
            "student": ("applied", "verified"),
            "other": ("skipped", "not required"),
        },
        owner_id=executor._operation_owner,
        apply_seconds=2.0,
        verification_seconds=1.0,
        verification_attempts=2,
        worker_count=3,
        throttling_count=1,
        now=105.0,
    )

    progress = service.store.get_execution_progress(manifest.id, now=110.0)
    phases = {phase.key: phase for phase in progress.phases}

    assert progress.total == 5
    assert progress.course_count == 5
    assert progress.remaining_course_count == 1
    assert phases["classes"].applied == 1
    assert phases["teachers"].failed == 1
    assert phases["students"].applied == 1
    assert phases["students"].pending == 1
    assert phases["other"].skipped == 1
    assert progress.current_batch is not None
    assert progress.current_batch.phase_key == "classes"
    assert progress.current_batch.action_count == 4
    assert progress.current_batch.course_count == 4
    assert progress.current_batch.verification_attempts == 2
    assert progress.adaptive_state == "protecting_google"


def test_execution_progress_uses_two_stage_heartbeat_thresholds(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(ImportAction("a-1", "student_add", "Section_101", "one@example.org"),),
    )
    executor = OneRosterExecutor(service.store, FakeClassroom())
    _claim_for_executor(service, manifest, executor)
    service.store.start_execution_run(
        manifest.id,
        phase="student_add",
        owner_id=executor._operation_owner,
        now=100.0,
    )

    current = service.store.get_execution_progress(manifest.id, now=129.999)
    delayed = service.store.get_execution_progress(manifest.id, now=130.0)
    stale = service.store.get_execution_progress(manifest.id, now=280.0)

    assert current.heartbeat_state == "current"
    assert current.heartbeat_delayed is False
    assert delayed.heartbeat_state == "delayed"
    assert delayed.heartbeat_delayed is True
    assert delayed.heartbeat_stale is False
    assert stale.heartbeat_state == "stale"
    assert stale.heartbeat_stale is True


def test_execution_eta_waits_for_two_verified_batches(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    actions = tuple(
        ImportAction(
            f"a-{number}",
            "student_add",
            f"Section_10{number}",
            f"student-{number}@example.org",
        )
        for number in range(1, 4)
    )
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=actions,
    )
    executor = OneRosterExecutor(service.store, FakeClassroom())
    _claim_for_executor(service, manifest, executor)
    run = service.store.start_execution_run(
        manifest.id,
        phase="student_add",
        owner_id=executor._operation_owner,
        now=100.0,
    )
    for index, action in enumerate(actions[:2], start=1):
        batch = service.store.prepare_execution_batch(
            run.id,
            manifest.id,
            (action,),
            phase="student_add",
            owner_id=executor._operation_owner,
            now=100.0 + index,
        )
        service.store.mark_execution_batch_started(
            batch.id,
            now=102.0 + index,
        )
        service.store.complete_verified_batch(
            batch.id,
            manifest.id,
            {action.id: ("applied", "verified")},
            owner_id=executor._operation_owner,
            apply_seconds=1.0,
            verification_seconds=1.0,
            verification_attempts=1,
            worker_count=5,
            now=104.0 + index,
        )
        progress = service.store.get_execution_progress(
            manifest.id,
            now=105.0 + index,
        )
        if index == 1:
            assert progress.completed_batches == 1
            assert progress.eta_seconds is None

    assert progress.completed_batches == 2
    assert progress.eta_seconds is not None


def test_complete_verified_batch_is_atomic_and_records_sanitized_receipt(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(
            ImportAction("a-1", "student_add", "Section_102", "one@example.org"),
            ImportAction("a-2", "student_add", "Section_101", "two@example.org"),
        ),
    )
    executor = OneRosterExecutor(service.store, FakeClassroom())
    _claim_for_executor(service, manifest, executor)
    run = service.store.start_execution_run(
        manifest.id,
        phase="student_add",
        owner_id=executor._operation_owner,
    )
    packed = service.store.get_pending_action_batch(
        manifest.id,
        kinds=("student_add",),
        limit=50,
    )
    assert [action.subject for action in packed] == ["Section_101", "Section_102"]
    batch = service.store.prepare_execution_batch(
        run.id,
        manifest.id,
        packed,
        phase="student_add",
        owner_id=executor._operation_owner,
    )
    service.store.mark_execution_batch_started(batch.id, worker_count=8)

    completed = service.store.complete_verified_batch(
        batch.id,
        manifest.id,
        {
            "a-1": ("applied", "Verified against live Classroom state."),
            "a-2": ("failed", "OR-BATCH-VERIFY-FAILED"),
        },
        owner_id=executor._operation_owner,
        apply_seconds=1.25,
        verification_seconds=0.5,
        verification_attempts=2,
        worker_count=8,
    )

    assert completed.status == "failed"
    assert completed.worker_count == 8
    assert completed.verification_attempts == 2
    assert completed.apply_seconds == pytest.approx(1.25)
    assert completed.verification_seconds == pytest.approx(0.5)
    assert completed.persistence_seconds >= 0
    progress = service.store.get_execution_progress(manifest.id)
    assert progress.applied == 1
    assert progress.failed == 1
    assert progress.worker_count == 8
    assert progress.adaptive_state == "protecting_google"
    assert progress.actions_per_minute > 0


def test_complete_verified_batch_rejects_inexact_membership_without_partial_writes(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(
            ImportAction("a-1", "student_add", "Section_101", "one@example.org"),
            ImportAction("a-2", "student_add", "Section_101", "two@example.org"),
        ),
    )
    executor = OneRosterExecutor(service.store, FakeClassroom())
    _claim_for_executor(service, manifest, executor)
    run = service.store.start_execution_run(
        manifest.id,
        phase="student_add",
        owner_id=executor._operation_owner,
    )
    batch = service.store.prepare_execution_batch(
        run.id,
        manifest.id,
        manifest.actions,
        phase="student_add",
        owner_id=executor._operation_owner,
    )
    service.store.mark_execution_batch_started(batch.id)

    with pytest.raises(OneRosterError) as mismatch:
        service.store.complete_verified_batch(
            batch.id,
            manifest.id,
            {"a-1": ("applied", "verified")},
            owner_id=executor._operation_owner,
            apply_seconds=1,
            verification_seconds=1,
            verification_attempts=1,
            worker_count=5,
        )

    assert mismatch.value.code == "OR-BATCH-ACTIONS-CHANGED"
    assert service.store.get_execution_progress(manifest.id).pending == 2


@pytest.mark.asyncio
async def test_verification_retries_only_unresolved_actions(tmp_path: Path):
    executor = OneRosterExecutor(
        OneRosterService("example.org", tmp_path / "component").store,
        FakeClassroom(),
        stabilization_delays=(0.0, 0.0),
    )
    actions = (
        ImportAction("a-1", "student_add", "Section_101", "one@example.org"),
        ImportAction("a-2", "student_add", "Section_102", "two@example.org"),
    )
    calls: list[tuple[str, ...]] = []

    async def verify(selected, _owner_ids):
        calls.append(tuple(action.id for action in selected))
        return {
            action.id: action.id == "a-1" or len(calls) > 1
            for action in selected
        }

    executor._verify = verify  # type: ignore[method-assign]
    verified, attempts, _seconds = await executor._verify_with_retries(actions, {})

    assert verified == {"a-1": True, "a-2": True}
    assert attempts == 2
    assert calls == [("a-1", "a-2"), ("a-2",)]


def test_adaptive_workers_increase_after_clean_batches_and_back_off_on_risk():
    tuner = _AdaptiveWorkerTuner()
    assert tuner.worker_count == 5
    for _ in range(2):
        tuner.observe(
            action_count=50,
            duration_seconds=10,
            batch_failed=False,
            throttling_count=0,
            verification_attempts=1,
        )
    assert tuner.worker_count == 8
    tuner.observe(
        action_count=50,
        duration_seconds=10,
        batch_failed=False,
        throttling_count=1,
        verification_attempts=1,
    )
    assert tuner.worker_count == 5
    tuner.observe(
        action_count=50,
        duration_seconds=10,
        batch_failed=False,
        throttling_count=0,
        verification_attempts=2,
    )
    assert tuner.worker_count == 3


@pytest.mark.parametrize(
    ("profile", "batch_failed", "throttling_count", "attempts", "duration"),
    (
        ("high-latency", False, 0, 1, 30.0),
        ("throttled", False, 1, 1, 10.0),
        ("partial-failure", True, 0, 1, 10.0),
        ("delayed-consistency", False, 0, 2, 10.0),
    ),
)
def test_adaptive_workers_back_off_for_each_risk_profile(
    profile: str,
    batch_failed: bool,
    throttling_count: int,
    attempts: int,
    duration: float,
):
    tuner = _AdaptiveWorkerTuner()
    tuner.best_seconds_per_action = 0.2
    tuner.level_index = 2

    tuner.observe(
        action_count=50,
        duration_seconds=duration,
        batch_failed=batch_failed,
        throttling_count=throttling_count,
        verification_attempts=attempts,
    )

    assert tuner.worker_count == 5, profile


@pytest.mark.asyncio
async def test_execution_persists_fresh_planning_performance_receipt(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    manifest = service.persist_live_plan(
        await service.build_live_plan(connector, import_id)
    ).ordinary

    await service.execute_manifest(
        connector,
        manifest.id,
        typed_import_id=import_id,
    )
    progress = service.get_execution_progress(manifest.id)

    assert progress.run is not None
    assert progress.run.planning_seconds > 0
    assert progress.run.directory_snapshot_seconds >= 0
    assert progress.run.classroom_snapshot_seconds >= 0
    assert progress.actions_per_minute > 0


def test_latest_manifest_header_prefers_the_actionable_journey_step(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    ordinary = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(ImportAction("a-1", "course_create", "Section_101", ""),),
        plan_kind="ordinary",
    )
    service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(
            ImportAction(
                "owner-1",
                "owner_transfer",
                "Section_101",
                "teacher@example.org",
            ),
        ),
        plan_kind="ownership",
    )

    assert service.latest_manifest_header(import_id).id == ordinary.id
    service.store.confirm_manifest(ordinary.id, ordinary.import_id)
    service.store.claim_manifest(ordinary.id, owner_id="test-owner")

    latest = service.latest_manifest_header(import_id)
    assert latest is not None
    assert latest.id == ordinary.id
    assert latest.status == "running"


@pytest.mark.asyncio
async def test_dead_durable_batch_requires_reconciliation_instead_of_blind_retry(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(
            ImportAction("a-1", "student_add", "Section_101", "one@example.org"),
        ),
    )
    executor = OneRosterExecutor(service.store, FakeClassroom())
    service.store.confirm_manifest(manifest.id, manifest.import_id)
    service.store.claim_manifest(
        manifest.id,
        owner_id=executor._operation_owner,
        owner_pid=999_999_999,
        owner_identity="definitely-not-this-process",
    )
    run = service.store.start_execution_run(
        manifest.id,
        phase="student_add",
        owner_id=executor._operation_owner,
    )
    batch = service.store.prepare_execution_batch(
        run.id,
        manifest.id,
        manifest.actions,
        phase="student_add",
        owner_id=executor._operation_owner,
    )
    service.store.mark_execution_batch_started(batch.id)

    reopened = OneRosterService("example.org", tmp_path / "component")

    recovered = reopened.get_manifest_header(manifest.id)
    progress = reopened.get_execution_progress(manifest.id)
    assert recovered.status == "recovery_required"
    assert recovered.error == "OR-RECOVERY-REQUIRED"
    assert progress.run is not None
    assert progress.run.status == "recovery_required"
    with sqlite3.connect(reopened.store.state_path) as conn:
        status = conn.execute(
            "SELECT status FROM execution_batches WHERE id = ?",
            (batch.id,),
        ).fetchone()[0]
    assert status == "reconciling"

    connector = FakeClassroom()
    detail = connector.add_course("Section_101", owner="teacher@example.org")
    connector.students[detail.id].add("one@example.org")
    summary = await reopened.reconcile_interrupted_manifest(
        connector,
        manifest.id,
    )

    assert summary.applied == 1
    assert summary.manifest.status == "paused"
    assert reopened.get_manifest(manifest.id).actions[0].status == "applied"


@pytest.mark.asyncio
async def test_closed_student_gate_rejects_before_live_planning(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(
            ImportAction(
                "a-1",
                "student_add",
                "Section_101",
                "student@example.org",
            ),
        ),
    )
    service.store.confirm_manifest(manifest.id, import_id)
    service.finish_manifest(manifest.id, status="awaiting_students")

    with pytest.raises(OneRosterError) as blocked:
        await service.execute_manifest(connector, manifest.id)

    assert blocked.value.code == "OR-STUDENT-GATE-CLOSED"
    assert connector.directory_reads == []
    assert connector.batches == []
    assert service.get_manifest_header(manifest.id).status == "awaiting_students"


@pytest.mark.asyncio
async def test_pause_request_stops_only_after_current_batch_is_verified(tmp_path: Path):
    class BlockingClassroom(FakeClassroom):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def run_classroom_batch(self, commands, *, max_commands=50):
            self.entered.set()
            await self.release.wait()
            await super().run_classroom_batch(commands, max_commands=max_commands)

    service, import_id = _ready_service(tmp_path)
    actions = tuple(
        ImportAction(
            id=f"create-{index}",
            kind="course_create",
            subject=f"Section_{index}",
            target="teacher@example.org",
            after=json.dumps(
                {
                    "alias": f"Section_{index}",
                    "name": f"Course {index}",
                    "owner_email": "teacher@example.org",
                    "room": "",
                    "section": "",
                },
                sort_keys=True,
            ),
        )
        for index in range(75)
    )
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=actions,
        limited_import=True,
    )
    connector = BlockingClassroom()
    executor = OneRosterExecutor(service.store, connector)
    _claim_for_executor(service, manifest, executor)
    run = service.store.start_execution_run(
        manifest.id,
        phase="course_create",
        owner_id=executor._operation_owner,
    )

    task = asyncio.create_task(
        executor._apply(
            manifest.id,
            owner_ids={"teacher@example.org": "teacher-id"},
            run_id=run.id,
        )
    )
    await connector.entered.wait()
    service.request_execution_pause(manifest.id)
    connector.release.set()
    applied, failed, skipped = await task

    assert (applied, failed, skipped) == (50, 0, 0)
    assert len(connector.batches) == 1
    assert service.store.pending_action_summary(manifest.id)["total"] == 25


@pytest.mark.asyncio
async def test_final_alias_guard_recognizes_matching_course_without_duplicate_create(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = BulkExecutionClassroom()
    connector.add_course(
        "Section_101",
        owner="teacher@example.org",
        state="ACTIVE",
    )
    action = ImportAction(
        id="create-1",
        kind="course_create",
        subject="Section_101",
        target="teacher@example.org",
        after=json.dumps(
            {
                "alias": "Section_101",
                "name": "Algebra I – P1 (2026-27)",
                "owner_email": "teacher@example.org",
                "room": "101",
                "section": "P1",
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
    )
    executor = OneRosterExecutor(service.store, connector)
    _claim_for_executor(service, manifest, executor)

    applied, failed, skipped = await executor._apply(
        manifest.id,
        (action,),
        {"teacher@example.org": "teacher-id"},
    )

    assert (applied, failed, skipped) == (1, 0, 0)
    assert connector.batches == []
    assert service.get_manifest(manifest.id).actions[0].status == "applied"


@pytest.mark.asyncio
async def test_executor_refuses_gam_when_exact_manifest_claim_is_missing(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(
            ImportAction(
                "create",
                "course_create",
                "Section_999",
                "teacher@example.org",
                after=json.dumps(
                    {
                        "alias": "Section_999",
                        "name": "Lease test",
                        "owner_email": "teacher@example.org",
                    },
                    sort_keys=True,
                ),
            ),
        ),
    )
    executor = OneRosterExecutor(service.store, connector)

    with pytest.raises(OneRosterError) as lost:
        await executor._apply(
            manifest.id,
            owner_ids={"teacher@example.org": "teacher-id"},
        )

    assert lost.value.code == "OR-MANIFEST-LEASE-LOST"
    assert connector.batches == []


@pytest.mark.asyncio
async def test_live_plan_resolves_directory_and_never_guesses_missing_users(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    connector.users.pop("student@example.org")

    plan = await service.build_live_plan(connector, import_id)

    assert set(connector.directory_reads) == {
        "teacher@example.org",
        "student@example.org",
    }
    assert plan.actions == ()
    assert {issue.code for issue in plan.issues} >= {"OR-USER-NOT-FOUND"}
    assert connector.batches == []


@pytest.mark.asyncio
async def test_course_naming_change_invalidates_live_plan_configuration(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()

    standard = await service.build_live_plan(connector, import_id)
    service.configure_course_naming(import_id, "{class_title}")
    renamed = await service.build_live_plan(connector, import_id)

    assert standard.config_hash != renamed.config_hash
    standard_create = next(
        action for action in standard.actions if action.kind == "course_create"
    )
    renamed_create = next(
        action for action in renamed.actions if action.kind == "course_create"
    )
    assert json.loads(standard_create.after)["name"] == (
        "Algebra I \u2013 P1 (2026-27)"
    )
    assert json.loads(renamed_create.after)["name"] == "Algebra Section"


@pytest.mark.asyncio
async def test_future_class_creates_with_all_teachers_but_defers_dated_student(
    tmp_path: Path,
):
    today = datetime.now(timezone.utc).date()
    start = today + timedelta(days=9)
    files = valid_files()
    files["academicSessions.csv"] = files["academicSessions.csv"].replace(
        "term-1,active,Current Term,term,2000-01-01,2100-12-31",
        (
            "term-1,active,Future Term,term,"
            f"{start.isoformat()},{(start + timedelta(days=120)).isoformat()}"
        ),
    )
    files["users.csv"] += (
        "teacher-2,active,teacher2,teacher2@example.org,Tess,Teacher,t2,school-1\n"
    )
    files["enrollments.csv"] = files["enrollments.csv"].replace(
        "teacher,true,,",
        f"teacher,true,{start.isoformat()},",
    ).replace(
        "student,false,,",
        f"student,false,{start.isoformat()},",
    )
    files["enrollments.csv"] += (
        "enrollment-teacher-2,active,101,school-1,teacher-2,teacher,false,"
        f"{start.isoformat()},\n"
    )
    service = OneRosterService("example.org", tmp_path / "future-class")
    snapshot = service.upload(zip_bytes(files))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    service.mark_scope_ready()
    connector = FakeClassroom()
    connector.users["teacher2@example.org"] = GAMUser(
        "teacher2@example.org",
        user_id="teacher2-id",
    )

    plan = await service.build_live_plan(connector, snapshot.id)

    assert any(action.kind == "course_create" for action in plan.actions)
    assert any(
        action.kind == "teacher_add"
        and action.target == "teacher2@example.org"
        for action in plan.actions
    )
    assert not any(action.kind == "student_add" for action in plan.actions)


@pytest.mark.asyncio
async def test_crossing_roster_date_invalidates_unpersisted_live_plan(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    planned_at = datetime(2026, 8, 3, 23, 59, tzinfo=timezone.utc)
    plan = await service.build_live_plan(connector, import_id, now=planned_at)

    with pytest.raises(OneRosterError) as stale:
        service.persist_live_plan(
            plan,
            now=planned_at + timedelta(days=1),
        )

    assert stale.value.code == "OR-SCOPE-DRIFT"
    assert connector.batches == []


@pytest.mark.asyncio
async def test_teacher_prep_then_exact_manifest_student_release(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    plan = await service.build_live_plan(connector, import_id)
    assert [action.kind for action in plan.actions] == [
        "course_create",
        "course_activate",
        "student_add",
    ]
    manifests = service.persist_live_plan(plan)

    prep = await service.execute_manifest(
        connector,
        manifests.ordinary.id,
        typed_import_id=import_id,
    )
    assert prep.awaiting_students
    assert prep.manifest.status == "awaiting_students"
    assert len(prep.manifest.prepared_live_hash) == 64
    assert connector.students[connector.courses["Section_101"].id] == set()
    assert all(
        "sync" not in command and "clear" not in command
        for batch in connector.batches
        for command in batch
    )

    now = datetime.now(timezone.utc)
    armed = service.arm_gate(
        prep.manifest.id,
        prep.manifest.manifest_hash,
        now.isoformat(),
    )
    assert armed.state is GateState.ARMED
    opened = await service.revalidate_scheduled_gate(
        connector,
        prep.manifest.id,
        now=now + timedelta(seconds=1),
    )
    assert opened.state is GateState.OPEN
    finished = await service.execute_manifest(connector, prep.manifest.id)
    assert finished.manifest.status == "completed"
    assert connector.students[connector.courses["Section_101"].id] == {
        "student@example.org"
    }


@pytest.mark.asyncio
async def test_execution_reuses_preflight_owner_ids_without_second_directory_export(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = BulkExecutionClassroom()
    plan = await service.build_live_plan(connector, import_id)
    assert plan.owner_ids == {"teacher@example.org": "teacher-id"}
    manifest = service.persist_live_plan(plan).ordinary

    prepared = await service.execute_manifest(
        connector,
        manifest.id,
        typed_import_id=import_id,
    )

    assert prepared.awaiting_students
    # One export builds the preview, one validates before apply, and two
    # consecutive matching observations stabilize post-preparation live state.
    # Batch application/verification itself still reuses preflight owner IDs.
    assert connector.directory_exports == 4


@pytest.mark.asyncio
async def test_execute_and_gate_revalidation_never_load_full_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    manifest = service.persist_live_plan(
        await service.build_live_plan(connector, import_id)
    ).ordinary

    def reject_full_manifest(*_args, **_kwargs):
        raise AssertionError("executor loaded the full manifest action set")

    monkeypatch.setattr(service.store, "get_manifest", reject_full_manifest)
    prepared = await service.execute_manifest(
        connector,
        manifest.id,
        typed_import_id=import_id,
    )
    assert prepared.awaiting_students
    assert prepared.manifest.actions == ()

    now = datetime.now(timezone.utc)
    service.arm_gate(
        prepared.manifest.id,
        prepared.manifest.manifest_hash,
        now.isoformat(),
    )
    opened = await service.revalidate_scheduled_gate(
        connector,
        prepared.manifest.id,
        now=now + timedelta(seconds=1),
    )
    assert opened.state is GateState.OPEN


@pytest.mark.asyncio
async def test_manifest_preflight_hashing_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    planning = await service.build_live_plan(connector, import_id)
    manifest = service.persist_live_plan(planning).ordinary
    executor = OneRosterExecutor(service.store, connector)
    original = service.store.pending_actions_hash
    worker_threads: list[int] = []

    def slow_hash(manifest_id):
        worker_threads.append(threading.get_ident())
        time.sleep(0.05)
        return original(manifest_id)

    monkeypatch.setattr(service.store, "pending_actions_hash", slow_hash)
    main_thread = threading.get_ident()
    preflight = asyncio.create_task(
        executor._validate_preflight(manifest, planning)
    )
    heartbeat = 0
    while not preflight.done():
        heartbeat += 1
        await asyncio.sleep(0.005)
    await preflight

    assert worker_threads and worker_threads[0] != main_thread
    # Thread identity proves the blocking hash was offloaded. Windows timer
    # granularity can coalesce several of the nominal 5 ms heartbeats.
    assert heartbeat >= 1


@pytest.mark.asyncio
async def test_persisted_apply_streams_at_most_fifty_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    actions = tuple(
        ImportAction(
            id=f"action-{index}",
            kind="course_create",
            subject=f"Section_{index}",
            target="teacher@example.org",
            after=json.dumps(
                {
                    "alias": f"Section_{index}",
                    "name": f"Course {index}",
                    "owner_email": "teacher@example.org",
                    "room": "",
                    "section": "",
                },
                sort_keys=True,
            ),
        )
        for index in range(120)
    )
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=actions,
        limited_import=True,
    )
    original_batch = service.store.get_pending_action_batch
    batch_receipts: list[tuple[int, int]] = []

    def bounded_batch(manifest_id, *, kinds, limit=50):
        assert 0 < limit <= 50
        batch = original_batch(
            manifest_id,
            kinds=kinds,
            limit=limit,
        )
        assert len(batch) <= 50
        batch_receipts.append((limit, len(batch)))
        return batch

    def reject_full_manifest(*_args, **_kwargs):
        raise AssertionError("bounded apply loaded the full manifest action set")

    monkeypatch.setattr(
        service.store,
        "get_pending_action_batch",
        bounded_batch,
    )
    monkeypatch.setattr(service.store, "get_manifest", reject_full_manifest)
    executor = OneRosterExecutor(service.store, connector, batch_size=500)
    _claim_for_executor(service, manifest, executor)

    applied, failed, skipped = await executor._apply(
        manifest.id,
        owner_ids={"teacher@example.org": "teacher-id"},
    )

    assert (applied, failed, skipped) == (120, 0, 0)
    assert [size for _, size in batch_receipts] == [50, 50, 20, 0]
    assert service.store.pending_action_summary(manifest.id)["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action_kind", ("teacher_remove", "student_remove"))
async def test_failed_batch_and_failed_roster_reread_never_verify_removal(
    tmp_path: Path,
    action_kind: str,
):
    service, import_id = _ready_service(tmp_path)
    connector = BatchAndRosterFailure()
    if action_kind == "teacher_remove":
        detail = connector.add_course(
            "Section_101",
            owner="oldowner@example.org",
        )
        plan = await service.build_live_plan(connector, import_id)
        manifest = service.persist_live_plan(plan).ownership
        assert manifest is not None
    else:
        detail = connector.add_course(
            "Section_101",
            owner="teacher@example.org",
        )
        connector.students[detail.id].add("obsolete@example.org")
        plan = await service.build_live_plan(connector, import_id)
        manifest = service.persist_live_plan(plan).ordinary

    action = next(item for item in manifest.actions if item.kind == action_kind)
    executor = OneRosterExecutor(service.store, connector)
    _claim_for_executor(service, manifest, executor)
    applied, failed, skipped = await executor._apply(
        manifest.id,
        (action,),
        plan.owner_ids,
    )

    assert (applied, failed, skipped) == (0, 1, 0)
    recorded = next(
        item
        for item in service.get_manifest(manifest.id).actions
        if item.id == action.id
    )
    assert recorded.status == "failed"
    assert recorded.detail == "OR-BATCH-VERIFY-FAILED"


@pytest.mark.asyncio
async def test_applied_batch_with_omitted_verification_course_is_failed(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = IncompleteVerificationRoster()
    detail = connector.add_course(
        "Section_101",
        owner="teacher@example.org",
    )
    plan = await service.build_live_plan(connector, import_id)
    manifest = service.persist_live_plan(plan).ordinary
    action = next(item for item in manifest.actions if item.kind == "student_add")
    executor = OneRosterExecutor(service.store, connector)
    _claim_for_executor(service, manifest, executor)

    applied, failed, skipped = await executor._apply(
        manifest.id,
        (action,),
        plan.owner_ids,
    )

    assert connector.students[detail.id] == {"student@example.org"}
    assert (applied, failed, skipped) == (0, 1, 0)
    recorded = next(
        item
        for item in service.get_manifest(manifest.id).actions
        if item.id == action.id
    )
    assert recorded.status == "failed"
    assert recorded.detail == "OR-BATCH-VERIFY-FAILED"


@pytest.mark.asyncio
async def test_manual_open_revalidates_live_state_but_may_open_before_schedule(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    manifest = service.persist_live_plan(
        await service.build_live_plan(connector, import_id)
    ).ordinary
    prep = await service.execute_manifest(
        connector,
        manifest.id,
        typed_import_id=import_id,
    )
    now = datetime.now(timezone.utc)
    service.arm_gate(
        prep.manifest.id,
        prep.manifest.manifest_hash,
        (now + timedelta(hours=1)).isoformat(),
    )

    with pytest.raises(OneRosterError) as not_due:
        await service.revalidate_scheduled_gate(
            connector,
            prep.manifest.id,
            now=now,
        )
    assert not_due.value.code == "OR-GATE-NOT-DUE"

    opened = await service.revalidate_scheduled_gate(
        connector,
        prep.manifest.id,
        now=now,
        manual=True,
    )
    assert opened.state is GateState.OPEN


@pytest.mark.asyncio
async def test_live_drift_stales_manifest_before_any_mutation(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    plan = await service.build_live_plan(connector, import_id)
    manifest = service.persist_live_plan(plan).ordinary
    connector.add_course(
        "Section_101",
        owner="teacher@example.org",
        state="ACTIVE",
    )
    connector.students[connector.courses["Section_101"].id].add("student@example.org")

    with pytest.raises(OneRosterError) as stale:
        await service.execute_manifest(
            connector,
            manifest.id,
            typed_import_id=import_id,
        )
    assert stale.value.code == "OR-MANIFEST-DRIFT"
    assert service.get_manifest(manifest.id).status == "stale"
    assert connector.batches == []


@pytest.mark.asyncio
async def test_alias_rebind_with_same_action_diff_stales_initial_manifest(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    original = connector.add_course(
        "Section_101",
        owner="teacher@example.org",
    )
    plan = await service.build_live_plan(connector, import_id)
    manifest = service.persist_live_plan(plan).ordinary
    replacement = _replace_detail(original, id="replacement-course")
    connector.courses["Section_101"] = replacement
    connector.teachers[replacement.id] = {"teacher@example.org"}
    connector.students[replacement.id] = set()

    with pytest.raises(OneRosterError) as stale:
        await service.execute_manifest(
            connector,
            manifest.id,
            typed_import_id=import_id,
        )

    assert stale.value.code == "OR-MANIFEST-DRIFT"
    assert service.get_manifest(manifest.id).status == "stale"
    assert connector.batches == []


@pytest.mark.asyncio
async def test_alias_rebind_after_teacher_prep_holds_student_release(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    manifest = service.persist_live_plan(
        await service.build_live_plan(connector, import_id)
    ).ordinary
    prepared = await service.execute_manifest(
        connector,
        manifest.id,
        typed_import_id=import_id,
    )
    detail = connector.courses["Section_101"]
    replacement = _replace_detail(detail, id="replacement-course")
    connector.courses["Section_101"] = replacement
    connector.teachers[replacement.id] = {"teacher@example.org"}
    connector.students[replacement.id] = set()
    now = datetime.now(timezone.utc)
    service.arm_gate(
        prepared.manifest.id,
        prepared.manifest.manifest_hash,
        now.isoformat(),
    )

    with pytest.raises(OneRosterError) as stale:
        await service.revalidate_scheduled_gate(
            connector,
            prepared.manifest.id,
            now=now + timedelta(seconds=1),
        )

    assert stale.value.code == "OR-MANIFEST-DRIFT"
    gate = service.get_gate()
    assert gate.state is GateState.HELD
    assert gate.hold_code == "OR-GATE-DRIFT"
    report = service.get_manifest_header(prepared.manifest.id).drift_report
    assert any(
        row["course"] == "Section_101"
        and row["field"] == "id"
        and row["category"] == "course identity changed"
        for row in report
    )
    assert connector.batches


@pytest.mark.asyncio
async def test_config_drift_refuses_manifest_persistence(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    plan = await service.build_live_plan(connector, import_id)
    service.save_threshold_profile(
        ThresholdProfile(configured=True, limited_import=True)
    )

    with pytest.raises(OneRosterError) as stale:
        service.persist_live_plan(plan)

    assert stale.value.code == "OR-CONFIG-DRIFT"
    assert connector.batches == []


@pytest.mark.asyncio
async def test_current_non_directory_member_is_never_a_removal_target(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    detail = connector.add_course("Section_101", owner="teacher@example.org")
    connector.students[detail.id].add("external@outside.example")

    plan = await service.build_live_plan(connector, import_id)

    assert not any(
        action.kind == "student_remove"
        and action.target == "external@outside.example"
        for action in plan.actions
    )
    assert "OR-LIVE-PARTICIPANT-UNRESOLVED" in {
        issue.code for issue in plan.issues
    }


@pytest.mark.asyncio
async def test_unresolved_live_owner_quarantines_course(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    connector.add_course("Section_101", owner="deleted-owner@example.org")

    plan = await service.build_live_plan(connector, import_id)

    assert plan.actions == ()
    assert plan.ownership_actions == ()
    assert "OR-OWNER-UNRESOLVED" in {issue.code for issue in plan.issues}
    assert connector.batches == []


@pytest.mark.asyncio
async def test_ownership_is_a_separate_typed_plan_and_limited_is_additions_only(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    detail = connector.add_course("Section_101", owner="oldowner@example.org")
    connector.students[detail.id].add("obsolete@example.org")

    plan = await service.build_live_plan(connector, import_id)
    assert [action.kind for action in plan.ownership_actions] == [
        "owner_transfer",
        "teacher_remove",
    ]
    assert "student_remove" in {action.kind for action in plan.actions}
    manifests = service.persist_live_plan(plan)
    assert manifests.ownership is not None
    assert manifests.ownership.plan_kind == "ownership"

    limited = await service.build_live_plan(
        connector,
        import_id,
        limited_import=True,
    )
    assert {action.kind for action in limited.actions} <= {
        "course_create",
        "course_activate",
        "teacher_add",
        "student_add",
    }
    assert limited.ownership_actions == ()
    assert limited.archive_actions == ()


@pytest.mark.asyncio
async def test_missing_prior_managed_alias_creates_separate_archive_plan(tmp_path: Path):
    service, first_id = _ready_service(tmp_path)
    service.mark_accepted(first_id)
    files = valid_files()
    files["classes.csv"] = files["classes.csv"].replace(
        "101,active,Algebra Section",
        "202,active,Algebra Section",
    )
    files["enrollments.csv"] = files["enrollments.csv"].replace(
        ",101,school-1,",
        ",202,school-1,",
    )
    second = service.upload(zip_bytes(files))
    connector = FakeClassroom()
    connector.add_course("Section_101", owner="teacher@example.org")

    plan = await service.build_live_plan(connector, second.id)

    assert [action.kind for action in plan.archive_actions] == ["course_archive"]
    assert plan.archive_actions[0].subject == "Section_101"
    manifests = service.persist_live_plan(plan)
    assert manifests.archive is not None
    assert manifests.archive.plan_kind == "archive"
    assert all(action.kind == "course_archive" for action in manifests.archive.actions)


@pytest.mark.asyncio
async def test_interrupted_manifest_never_auto_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "component"
    service = OneRosterService("example.org", root)
    snapshot = service.upload(zip_bytes(valid_files()))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    service.mark_scope_ready()
    connector = FakeClassroom()
    plan = await service.build_live_plan(connector, snapshot.id)
    manifest = service.persist_live_plan(plan).ordinary
    service.store.confirm_manifest(manifest.id, snapshot.id)
    service.store.claim_manifest(manifest.id)

    monkeypatch.setattr(
        "gamgui.components.oneroster.store.process_lease_is_dead",
        lambda _pid, _identity: True,
    )
    restarted = OneRosterService("example.org", root)
    restarted.mark_scope_ready()
    recovered = restarted.get_manifest(manifest.id)
    assert recovered.status == "interrupted"
    with pytest.raises(OneRosterError) as stopped:
        await restarted.execute_manifest(
            connector,
            manifest.id,
            typed_import_id=snapshot.id,
        )
    assert stopped.value.code == "OR-MANIFEST-INTERRUPTED"
    assert connector.batches == []

    replacement = restarted.persist_live_plan(
        await restarted.build_live_plan(connector, snapshot.id)
    ).ordinary
    assert replacement.id != manifest.id
    assert replacement.manifest_hash != manifest.manifest_hash
    assert replacement.status == "planned"
    assert not replacement.confirmed


def test_live_manifest_claim_is_preserved_and_owner_checked(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    manifest = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(
            ImportAction(
                "action",
                "student_add",
                "Section_101",
                "student@example.org",
            ),
        ),
    )
    service.store.confirm_manifest(manifest.id, import_id)
    service.store.claim_manifest(manifest.id, owner_id="executor-a")

    reopened = OneRosterService("example.org", service.store.root)
    assert reopened.get_manifest_header(manifest.id).status == "running"
    with pytest.raises(PermissionError, match="another executor"):
        reopened.store.mark_action_result(
            manifest.id,
            "action",
            status="applied",
            owner_id="executor-b",
        )
    with pytest.raises(PermissionError, match="another executor"):
        reopened.store.finish_manifest(
            manifest.id,
            status="interrupted",
            owner_id="executor-b",
        )

    updated = service.store.mark_action_result(
        manifest.id,
        "action",
        status="applied",
        owner_id="executor-a",
    )
    assert updated is not None and updated.actions[0].status == "applied"
    finished = service.store.finish_manifest(
        manifest.id,
        status="completed",
        owner_id="executor-a",
    )
    assert finished.status == "completed"


def test_legacy_manifest_schema_migrates_without_recovering_unknown_run(
    tmp_path: Path,
):
    root = tmp_path / "legacy-component"
    root.mkdir()
    state = root / "state.db"
    with sqlite3.connect(state) as connection:
        connection.execute(
            """
            CREATE TABLE manifests (
                id TEXT PRIMARY KEY, domain TEXT NOT NULL, import_id TEXT NOT NULL,
                source_hash TEXT NOT NULL, config_hash TEXT NOT NULL,
                live_hash TEXT NOT NULL, manifest_hash TEXT NOT NULL UNIQUE,
                threshold_evaluation_hash TEXT NOT NULL, status TEXT NOT NULL,
                created_at REAL NOT NULL, error TEXT NOT NULL,
                plan_kind TEXT NOT NULL DEFAULT 'ordinary',
                confirmed_at REAL NOT NULL DEFAULT 0,
                threshold_evidence_json TEXT NOT NULL DEFAULT '{}',
                exclusions_json TEXT NOT NULL DEFAULT '[]',
                pilot_evidence_json TEXT NOT NULL DEFAULT '{}',
                prepared_live_hash TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            INSERT INTO manifests VALUES (
                'legacy-manifest', 'example.org', 'legacy-import',
                'source', 'config', 'live', ?, 'threshold', 'running',
                1, '', 'ordinary', 1, '{}', '[]', '{}', ''
            )
            """,
            ("a" * 64,),
        )

    store = OneRosterService("example.org", root).store

    with sqlite3.connect(state) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(manifests)")
        }
        row = connection.execute(
            """
            SELECT status, run_owner, run_pid, run_identity
            FROM manifests WHERE id = 'legacy-manifest'
            """
        ).fetchone()
    assert {"run_owner", "run_pid", "run_identity"}.issubset(columns)
    assert row == ("running", "", 0, "")
    assert store.has_active_jobs()


@pytest.mark.asyncio
async def test_store_active_job_contract_tracks_only_running(tmp_path: Path):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    manifest = service.persist_live_plan(
        await service.build_live_plan(connector, import_id)
    ).ordinary

    assert not service.store.has_active_jobs()
    service.store.confirm_manifest(manifest.id, import_id)
    assert not service.store.has_active_jobs()
    service.store.claim_manifest(manifest.id)
    assert service.store.has_active_jobs()
    service.store.finish_manifest(manifest.id, status="interrupted")
    assert not service.store.has_active_jobs()


@pytest.mark.asyncio
async def test_gate_requires_confirmed_awaiting_manifest_with_pending_students(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = FakeClassroom()
    manifest = service.persist_live_plan(
        await service.build_live_plan(connector, import_id)
    ).ordinary
    release = datetime.now(timezone.utc) + timedelta(minutes=1)

    with pytest.raises(OneRosterError) as unconfirmed:
        service.arm_gate(manifest.id, manifest.manifest_hash, release.isoformat())
    assert unconfirmed.value.code == "OR-GATE-MANIFEST-NOT-READY"

    service.store.confirm_manifest(manifest.id, import_id)
    with pytest.raises(OneRosterError) as not_prepared:
        service.arm_gate(manifest.id, manifest.manifest_hash, release.isoformat())
    assert not_prepared.value.code == "OR-GATE-MANIFEST-NOT-READY"

    prepared = await service.execute_manifest(connector, manifest.id)
    assert prepared.awaiting_students
    armed = service.arm_gate(
        manifest.id,
        manifest.manifest_hash,
        release.isoformat(),
    )
    assert armed.state is GateState.ARMED

    no_students = service.create_manifest(
        import_id,
        config_hash="config",
        live_hash="live",
        actions=(),
        limited_import=True,
    )
    service.store.confirm_manifest(no_students.id, import_id)
    service.store.finish_manifest(no_students.id, status="awaiting_students")
    with pytest.raises(OneRosterError) as empty:
        service.arm_gate(
            no_students.id,
            no_students.manifest_hash,
            release.isoformat(),
        )
    assert empty.value.code == "OR-GATE-NO-STUDENT-ACTIONS"


@pytest.mark.asyncio
async def test_execution_refuses_shared_activity_registry_conflict(tmp_path: Path):
    registry = ActivityRegistry()
    service = OneRosterService(
        "example.org",
        tmp_path / "component",
        activity_registry=registry,
    )
    snapshot = service.upload(zip_bytes(valid_files()))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    service.mark_scope_ready()
    connector = FakeClassroom()
    manifest = service.persist_live_plan(
        await service.build_live_plan(connector, snapshot.id)
    ).ordinary

    with registry.acquire("application-update"):
        with pytest.raises(OneRosterError) as busy:
            await service.execute_manifest(
                connector,
                manifest.id,
                typed_import_id=snapshot.id,
            )
    assert busy.value.code == "OR-ACTIVE-JOB"
    blocked_manifest = service.get_manifest(manifest.id)
    assert blocked_manifest.status == "planned"
    assert not blocked_manifest.confirmed
    assert connector.batches == []


@pytest.mark.asyncio
async def test_gate_revalidation_refuses_shared_activity_conflict(tmp_path: Path):
    registry = ActivityRegistry()
    service = OneRosterService(
        "example.org",
        tmp_path / "component",
        activity_registry=registry,
    )
    snapshot = service.upload(zip_bytes(valid_files()))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    service.mark_scope_ready()
    connector = FakeClassroom()
    manifest = service.persist_live_plan(
        await service.build_live_plan(connector, snapshot.id)
    ).ordinary
    prepared = await service.execute_manifest(
        connector,
        manifest.id,
        typed_import_id=snapshot.id,
    )
    assert prepared.awaiting_students
    connector.batches.clear()
    now = datetime.now(timezone.utc)
    service.arm_gate(manifest.id, manifest.manifest_hash, now.isoformat())

    with registry.acquire("application-update"):
        with pytest.raises(OneRosterError) as busy:
            await service.revalidate_scheduled_gate(
                connector,
                manifest.id,
                now=now + timedelta(seconds=1),
            )

    assert busy.value.code == "OR-ACTIVE-JOB"
    assert service.get_gate().state is GateState.ARMED
    assert connector.batches == []


def test_batch_builders_bound_size_and_reject_line_injection():
    argv = GAMCommands.print_course_participants_many(["1", "2"], "all")
    assert argv == [
        "print",
        "course-participants",
        "course",
        "1",
        "course",
        "2",
        "show",
        "all",
        "formatjson",
    ]
    line = GAMCommands.batch_line(
        GAMCommands.add_course_participant(
            "d:Section_1",
            "students",
            "student+tag@example.org",
        )
    )
    assert line.startswith("gam course ")
    with pytest.raises(ValueError):
        GAMCommands.batch_line(["course", "d:Section_1\n", "add", "students", "x"])
    with pytest.raises(ValueError):
        GAMCommands.print_course_participants_many([str(i) for i in range(51)])
    assert _is_allowed_classroom_batch_command(
        GAMCommands.update_course_state("d:Section_1", "ACTIVE")
    )
    assert _is_allowed_classroom_batch_command(
        GAMCommands.create_course(
            "Course",
            "teacher@example.org",
            alias="Section_1",
            state="PROVISIONED",
        )
    )
    assert _is_allowed_classroom_batch_command(
        GAMCommands.update_course_roster_metadata(
            "d:Section_1",
            name="Course",
            section="P1",
            room="101",
        )
    )
    assert "description" not in GAMCommands.update_course_roster_metadata(
        "d:Section_1",
        name="Course",
    )
    assert not _is_allowed_classroom_batch_command(
        ["update", "course", "d:Section_1", "state", "ACTIVE", "delete", "all"]
    )
    assert not _is_allowed_classroom_batch_command(
        ["course", "d:Section_1", "sync", "students", "all"]
    )
    assert _is_allowed_classroom_batch_command(
        GAMCommands.create_course(
            "state",
            "teacher@example.org",
            alias="Section_2",
            state="PROVISIONED",
        )
    )


@pytest.mark.asyncio
async def test_connector_batch_is_private_bounded_and_removed(tmp_path: Path):
    class Runner:
        timeout = 120.0

        def __init__(self):
            self.base_dir = tmp_path
            self.argv = []
            self.lines = []
            self.batch_path = None

        async def run_authenticated(
            self,
            _domain,
            argv,
            timeout=None,
            serialize=False,
        ):
            self.argv = list(argv)
            self.batch_path = Path(argv[1])
            self.lines = self.batch_path.read_text(encoding="utf-8").splitlines()
            assert serialize is True
            assert timeout <= 1800
            return ""

    runner = Runner()
    connector = GAMConnector(
        runner,  # type: ignore[arg-type]
        "example.org",
        AuditLog(tmp_path / "audit.jsonl"),
    )
    receipt = await connector.run_classroom_batch(
        [
            GAMCommands.add_course_participant(
                "d:Section_1",
                "students",
                "student@example.org",
            )
        ]
    )
    assert runner.argv[0] == "batch"
    assert runner.lines == [
        "gam course d:Section_1 add students student@example.org"
    ]
    assert runner.batch_path is not None
    assert runner.batch_path.parent == tmp_path / "spool"
    assert not runner.batch_path.exists()
    assert receipt.worker_count == 5
    assert receipt.outcome == "completed"
    assert receipt.duration_seconds >= 0


@pytest.mark.asyncio
async def test_connector_passes_adaptive_workers_only_to_the_child_gam_call(tmp_path: Path):
    class Runner:
        timeout = 120.0
        base_dir = tmp_path

        def __init__(self):
            self.gam_threads = None

        async def run_authenticated(
            self,
            _domain,
            _argv,
            *,
            timeout=None,
            serialize=False,
            gam_threads=None,
        ):
            assert serialize is True
            self.gam_threads = gam_threads
            return ""

    runner = Runner()
    connector = GAMConnector(
        runner,  # type: ignore[arg-type]
        "example.org",
        AuditLog(tmp_path / "audit.jsonl"),
    )
    receipt = await connector.run_classroom_batch(
        [
            GAMCommands.add_course_participant(
                "d:Section_1",
                "students",
                "student@example.org",
            )
        ],
        worker_count=8,
    )

    assert runner.gam_threads == 8
    assert receipt.worker_count == 8
    assert "GAM_THREADS" not in os.environ


@pytest.mark.asyncio
async def test_connector_bulk_rosters_assign_role_without_trusting_output(tmp_path: Path):
    class Runner:
        timeout = 120.0

        def __init__(self):
            self.roles = []

        async def run_authenticated(self, _domain, argv, **_kwargs):
            role = argv[argv.index("show") + 1]
            self.roles.append(role)
            email = "teacher@example.org" if role == "teachers" else "student@example.org"
            return (
                '[{"courseId":"123","profile":{"emailAddress":"'
                + email
                + '"}}]'
            )

    runner = Runner()
    connector = GAMConnector(
        runner,  # type: ignore[arg-type]
        "example.org",
        AuditLog(tmp_path / "audit.jsonl"),
    )

    participants = await connector.list_course_participants_many_bounded(["123"], "all")

    assert runner.roles == ["teachers", "students"]
    assert {(item.email, item.role) for item in participants} == {
        ("teacher@example.org", "teachers"),
        ("student@example.org", "students"),
    }


@pytest.mark.asyncio
async def test_single_course_roster_decodes_nested_formatjson_rows(tmp_path: Path):
    class Runner:
        timeout = 120.0

        async def run_authenticated(self, _domain, _argv, **_kwargs):
            return json.dumps(
                [
                    {
                        "courseId": "123",
                        "JSON-teachers": json.dumps(
                            [
                                {
                                    "profile": {
                                        "id": "teacher-id",
                                        "emailAddress": "teacher@example.org",
                                    }
                                }
                            ]
                        ),
                        "JSON-students": "[]",
                    }
                ]
            )

    connector = GAMConnector(
        Runner(),  # type: ignore[arg-type]
        "example.org",
        AuditLog(tmp_path / "audit.jsonl"),
    )

    participants = await connector.list_course_participants("123", "teachers")

    assert [(item.email, item.user_id, item.role) for item in participants] == [
        ("teacher@example.org", "teacher-id", "teachers")
    ]


@pytest.mark.asyncio
async def test_single_course_roster_preserves_course_only_row_as_unresolved_evidence(
    tmp_path: Path,
):
    class Runner:
        timeout = 120.0

        async def run_authenticated(self, _domain, _argv, **_kwargs):
            return '[{"courseId":"123","name":"Algebra"}]'

    connector = GAMConnector(
        Runner(),  # type: ignore[arg-type]
        "example.org",
        AuditLog(tmp_path / "audit.jsonl"),
    )

    participants = await connector.list_course_participants("123", "teachers")

    assert len(participants) == 1
    assert participants[0].label == ""
    assert not participants[0].identity_resolved


@pytest.mark.asyncio
async def test_connector_cancellation_waits_for_private_batch_removal(tmp_path: Path):
    class Runner:
        timeout = 120.0

        def __init__(self):
            self.base_dir = tmp_path
            self.entered = asyncio.Event()
            self.batch_path = None

        async def run_authenticated(self, _domain, argv, **_kwargs):
            self.batch_path = Path(argv[1])
            self.entered.set()
            await asyncio.Event().wait()

    runner = Runner()
    connector = GAMConnector(
        runner,  # type: ignore[arg-type]
        "example.org",
        AuditLog(tmp_path / "audit.jsonl"),
    )
    task = asyncio.create_task(
        connector.run_classroom_batch(
            [
                GAMCommands.add_course_participant(
                    "d:Section_1",
                    "students",
                    "student@example.org",
                )
            ]
        )
    )
    await runner.entered.wait()
    assert runner.batch_path is not None and runner.batch_path.exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not runner.batch_path.exists()
