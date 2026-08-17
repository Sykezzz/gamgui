from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gamgui.components.oneroster import OneRosterError, OneRosterService, ThresholdProfile
from gamgui.components.oneroster.models import (
    GateState,
    ImportAction,
    StudentEnrollmentGate,
)
from gamgui.components.oneroster.planner import planner_configuration_hash
from gamgui.components.oneroster.bootstrap_executor import AdditionsFirstBootstrapExecutor
from gamgui.core.activity import ActivityRegistry
from gamgui.core.classroom.models import CourseDetail, CourseRosterSnapshot
from gamgui.core.connectors.gam_connector import NativeClassroomBatchProgress
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.models import GAMUser
from gamgui.core.processes import current_process_identity
from tests.test_oneroster_helpers import valid_files, zip_bytes


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


class BootstrapClassroom:
    oneroster_stabilization_delays = (0.0, 0.0)

    def __init__(self, *, suspended_student: bool = False) -> None:
        self.users = {
            "teacher@example.org": GAMUser(
                "teacher@example.org",
                user_id="teacher-id",
            ),
            "student@example.org": GAMUser(
                "student@example.org",
                user_id="student-id",
                suspended=suspended_student,
            ),
        }
        self.courses: dict[str, CourseDetail] = {}
        self.teachers: dict[str, set[str]] = {}
        self.students: dict[str, set[str]] = {}
        self.phase_calls: list[str] = []
        self.inventory_reads = 0
        self.directory_reads = 0
        self.roster_reads = 0
        self.fail_once_phase = ""
        self.omit_once_phase = ""
        self.reject_existing_create = False
        self.pause_once_phase = ""
        self.pause_callback = None

    async def run_classroom_phase_batch(self, commands, *, progress_callback=None):
        submitted = 0
        phase = ""
        for raw_command in commands:
            command = list(raw_command)
            command_phase = _command_phase(command)
            phase = phase or command_phase
            assert command_phase == phase
            if not (self.omit_once_phase == phase):
                if (
                    self.reject_existing_create
                    and phase == "course_create"
                    and command[command.index("alias") + 1].casefold() in self.courses
                ):
                    submitted += 1
                    continue
                self._apply(command)
            submitted += 1
        self.phase_calls.append(phase)
        if self.omit_once_phase == phase:
            self.omit_once_phase = ""
        if progress_callback is not None:
            await progress_callback(
                NativeClassroomBatchProgress(
                    dispatched=submitted,
                    total=submitted,
                    raw_line=f"0,Processing item {submitted}/{submitted}",
                )
            )
        if self.fail_once_phase == phase:
            self.fail_once_phase = ""
            raise RuntimeError("controlled native process failure")
        if self.reject_existing_create and phase == "course_create":
            raise RuntimeError("controlled alias collision")
        if self.pause_once_phase == phase:
            self.pause_once_phase = ""
            assert self.pause_callback is not None
            self.pause_callback()
        return SimpleNamespace(
            submitted_count=submitted,
            duration_seconds=0.01,
            worker_count=10,
        )

    async def snapshot_oneroster_managed_courses(self, aliases):
        self.inventory_reads += 1
        requested = {str(alias).casefold() for alias in aliases}
        return [
            detail
            for alias, detail in self.courses.items()
            if alias in requested
        ]

    async def list_oneroster_directory(self):
        self.directory_reads += 1
        return dict(self.users)

    async def list_course_participants_many(self, course_ids, role="all"):
        assert role == "all"
        self.roster_reads += 1
        requested = tuple(str(course_id) for course_id in course_ids)
        return CourseRosterSnapshot(
            {
                course_id: (
                    frozenset(self.teachers.get(course_id, set())),
                    frozenset(self.students.get(course_id, set())),
                )
                for course_id in requested
            },
            frozenset(requested),
        )

    def _apply(self, command: list[str]) -> None:
        if command[:2] == ["create", "course"]:
            alias = command[command.index("alias") + 1]
            key = alias.casefold()
            owner = command[command.index("teacher") + 1].casefold()
            course_id = f"course-{len(self.courses) + 1}"
            detail = CourseDetail(
                id=course_id,
                name=command[command.index("name") + 1],
                section=(
                    command[command.index("section") + 1]
                    if "section" in command
                    else ""
                ),
                room=command[command.index("room") + 1] if "room" in command else "",
                owner_id=self.users[owner].user_id,
                owner_email=owner,
                course_state=command[command.index("state") + 1].upper(),
                aliases=(f"d:{alias}",),
            )
            self.courses[key] = detail
            self.teachers[course_id] = {owner}
            self.students[course_id] = set()
            return
        if command[:2] == ["update", "course"]:
            key = command[2].removeprefix("d:").casefold()
            current = self.courses[key]
            self.courses[key] = CourseDetail(
                **{
                    **current.__dict__,
                    "course_state": command[command.index("state") + 1].upper(),
                }
            )
            return
        key = command[1].removeprefix("d:").casefold()
        detail = self.courses[key]
        target = command[4].casefold()
        members = self.teachers if command[3] == "teachers" else self.students
        members[detail.id].add(target)

    def add_course(
        self,
        *,
        alias: str = "Section_101",
        name: str = "Algebra I - P1 (2026-27)",
        owner: str = "teacher@example.org",
        state: str = "PROVISIONED",
    ) -> CourseDetail:
        self._apply(
            GAMCommands.create_course(
                name,
                owner,
                alias=alias,
                section="P1",
                room="101",
                state=state,
            )
        )
        return self.courses[alias.casefold()]


def _command_phase(command: list[str]) -> str:
    if command[:2] == ["create", "course"]:
        return "course_create"
    if command[:2] == ["update", "course"] and "state" in command:
        return "course_activate"
    if command[:3] == ["course", command[1], "add"]:
        return "teacher_add" if command[3] == "teachers" else "student_add"
    raise AssertionError(f"unexpected bootstrap command: {command!r}")


def _paused_checkpoint(
    tmp_path: Path,
    *,
    preverified_connector: BootstrapClassroom | None = None,
) -> tuple[OneRosterService, object]:
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(zip_bytes(valid_files()))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    service.mark_scope_ready()
    profile = service.store.get_threshold_profile()
    config_hash = planner_configuration_hash(
        limited_import=True,
        course_name_template=snapshot.course_name_template,
        threshold_profile=profile.to_dict(),
        schedule_scope=service.store.schedule_scope(snapshot.id),
    )
    create_payload = json.dumps(
        {
            "alias": "Section_101",
            "name": "Algebra I - P1 (2026-27)",
            "owner_email": "teacher@example.org",
            "section": "P1",
            "room": "101",
        },
        sort_keys=True,
    )
    manifest = service.create_manifest(
        snapshot.id,
        config_hash=config_hash,
        live_hash="bootstrap-local-checkpoint",
        actions=(
            ImportAction(
                "create-101",
                "course_create",
                "Section_101",
                "teacher@example.org",
                after=create_payload,
            ),
            ImportAction(
                "student-101",
                "student_add",
                "Section_101",
                "student@example.org",
            ),
            ImportAction(
                "activate-101",
                "course_activate",
                "Section_101",
                "",
            ),
            ImportAction(
                "teacher-101",
                "teacher_add",
                "Section_101",
                "teacher@example.org",
            ),
        ),
        limited_import=True,
        plan_kind="limited",
    )
    service.store.confirm_manifest(manifest.id, manifest.import_id)
    owner = "verified-pause-fixture"
    service.store.claim_manifest(
        manifest.id,
        owner_id=owner,
        owner_pid=os.getpid(),
        owner_identity=current_process_identity(),
    )
    run = service.store.start_execution_run(
        manifest.id,
        phase="preflight",
        owner_id=owner,
    )
    if preverified_connector is not None:
        create = next(action for action in manifest.actions if action.kind == "course_create")
        preverified_connector.add_course()
        batch = service.store.prepare_execution_batch(
            run.id,
            manifest.id,
            (create,),
            phase="course_create",
            owner_id=owner,
        )
        service.store.mark_execution_batch_started(batch.id)
        service.store.complete_verified_batch(
            batch.id,
            manifest.id,
            {create.id: ("applied", "Verified before the clean pause.")},
            owner_id=owner,
            apply_seconds=0.01,
            verification_seconds=0.01,
            verification_attempts=1,
            worker_count=5,
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
    return service, manifest


@pytest.mark.asyncio
async def test_additions_first_executes_one_native_process_per_phase(tmp_path: Path):
    service, manifest = _paused_checkpoint(tmp_path)
    connector = BootstrapClassroom()

    summary = await service.execute_additions_first_bootstrap(
        connector,
        manifest.id,
        import_id_ack=manifest.import_id,
        deferred_verification_ack=True,
        now=NOW,
    )

    assert summary.manifest.status == "completed"
    assert (summary.applied, summary.failed, summary.skipped) == (4, 0, 0)
    assert connector.phase_calls == [
        "course_create",
        "student_add",
        "course_activate",
        "teacher_add",
    ]
    assert (
        connector.inventory_reads,
        connector.directory_reads,
        connector.roster_reads,
    ) == (1, 1, 1)


@pytest.mark.asyncio
async def test_suspended_student_does_not_quarantine_course(tmp_path: Path):
    service, manifest = _paused_checkpoint(tmp_path)
    connector = BootstrapClassroom(suspended_student=True)

    summary = await service.execute_additions_first_bootstrap(
        connector,
        manifest.id,
        import_id_ack=manifest.import_id,
        deferred_verification_ack=True,
        now=NOW,
    )

    assert summary.manifest.status == "partial"
    assert (summary.applied, summary.failed) == (3, 1)
    assert service.store.bootstrap_missing_report_count(manifest.id) == 1
    assert connector.courses["section_101"].course_state == "ACTIVE"
    assert "teacher@example.org" in connector.teachers["course-1"]


@pytest.mark.asyncio
async def test_native_failure_requires_recovery_before_more_mutation(tmp_path: Path):
    service, manifest = _paused_checkpoint(tmp_path)
    connector = BootstrapClassroom()
    connector.fail_once_phase = "course_create"

    with pytest.raises(RuntimeError, match="controlled native process failure"):
        await service.execute_additions_first_bootstrap(
            connector,
            manifest.id,
            import_id_ack=manifest.import_id,
            deferred_verification_ack=True,
            now=NOW,
        )

    assert service.store.get_manifest_header(manifest.id).status == "recovery_required"
    assert connector.phase_calls == ["course_create"]

    recovered = await service.reconcile_interrupted_manifest(connector, manifest.id)
    assert recovered.manifest.status == "paused"
    assert connector.phase_calls == ["course_create"]

    finished = await service.execute_additions_first_bootstrap(
        connector,
        manifest.id,
        import_id_ack=manifest.import_id,
        deferred_verification_ack=True,
        now=NOW,
    )
    assert finished.manifest.status == "completed"
    assert connector.phase_calls == [
        "course_create",
        "student_add",
        "course_activate",
        "teacher_add",
    ]


@pytest.mark.asyncio
async def test_nonclosed_student_gate_rejects_before_gam(tmp_path: Path):
    service, manifest = _paused_checkpoint(tmp_path)
    connector = BootstrapClassroom()
    service.store.save_gate(
        StudentEnrollmentGate(
            state=GateState.ARMED,
            timezone="America/Chicago",
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            release_at="2099-01-01T00:00:00-06:00",
        )
    )

    with pytest.raises(OneRosterError) as caught:
        await service.execute_additions_first_bootstrap(
            connector,
            manifest.id,
            import_id_ack=manifest.import_id,
            deferred_verification_ack=True,
            now=NOW,
        )

    assert caught.value.code == "OR-BOOTSTRAP-GATE-NOT-CLOSED"
    assert service.store.get_manifest_header(manifest.id).status == "paused"
    assert connector.phase_calls == []


@pytest.mark.asyncio
async def test_verified_create_is_never_resent_on_resume(tmp_path: Path):
    connector = BootstrapClassroom()
    service, manifest = _paused_checkpoint(
        tmp_path,
        preverified_connector=connector,
    )

    summary = await service.execute_additions_first_bootstrap(
        connector,
        manifest.id,
        import_id_ack=manifest.import_id,
        deferred_verification_ack=True,
        now=NOW,
    )

    assert summary.manifest.status == "completed"
    assert connector.phase_calls == [
        "student_add",
        "course_activate",
        "teacher_add",
    ]
    assert len(connector.courses) == 1


@pytest.mark.asyncio
async def test_missing_student_is_retried_once_and_then_verified(tmp_path: Path):
    service, manifest = _paused_checkpoint(tmp_path)
    connector = BootstrapClassroom()
    connector.omit_once_phase = "student_add"

    summary = await service.execute_additions_first_bootstrap(
        connector,
        manifest.id,
        import_id_ack=manifest.import_id,
        deferred_verification_ack=True,
        now=NOW,
    )

    assert summary.manifest.status == "completed"
    assert connector.phase_calls.count("student_add") == 2
    assert connector.phase_calls.count("course_create") == 1
    assert connector.inventory_reads == 2


@pytest.mark.asyncio
async def test_alias_name_conflict_is_reported_without_overwrite(tmp_path: Path):
    service, manifest = _paused_checkpoint(tmp_path)
    connector = BootstrapClassroom()
    existing = connector.add_course(name="Unrelated existing course")
    connector.reject_existing_create = True

    with pytest.raises(RuntimeError, match="controlled alias collision"):
        await service.execute_additions_first_bootstrap(
            connector,
            manifest.id,
            import_id_ack=manifest.import_id,
            deferred_verification_ack=True,
            now=NOW,
        )
    summary = await service.reconcile_interrupted_manifest(connector, manifest.id)

    assert summary.manifest.status == "partial"
    assert connector.phase_calls == ["course_create"]
    assert connector.courses["section_101"].name == existing.name
    assert service.store.bootstrap_missing_report_count(manifest.id) == 4


@pytest.mark.asyncio
async def test_pause_between_phases_preserves_submitted_create(tmp_path: Path):
    service, manifest = _paused_checkpoint(tmp_path)
    connector = BootstrapClassroom()
    connector.pause_once_phase = "course_create"
    connector.pause_callback = lambda: service.request_execution_pause(manifest.id)

    paused = await service.execute_additions_first_bootstrap(
        connector,
        manifest.id,
        import_id_ack=manifest.import_id,
        deferred_verification_ack=True,
        now=NOW,
    )
    assert paused.manifest.status == "paused"
    assert connector.phase_calls == ["course_create"]

    finished = await service.execute_additions_first_bootstrap(
        connector,
        manifest.id,
        import_id_ack=manifest.import_id,
        deferred_verification_ack=True,
        now=NOW,
    )
    assert finished.manifest.status == "completed"
    assert connector.phase_calls == [
        "course_create",
        "student_add",
        "course_activate",
        "teacher_add",
    ]


@pytest.mark.asyncio
async def test_config_drift_rejects_before_gam(tmp_path: Path):
    service, manifest = _paused_checkpoint(tmp_path)
    connector = BootstrapClassroom()
    service.save_threshold_profile(
        ThresholdProfile(configured=True, limited_import=True)
    )

    with pytest.raises(OneRosterError) as caught:
        await service.execute_additions_first_bootstrap(
            connector,
            manifest.id,
            import_id_ack=manifest.import_id,
            deferred_verification_ack=True,
            now=NOW,
        )

    assert caught.value.code == "OR-CONFIG-DRIFT"
    assert service.store.get_manifest_header(manifest.id).status == "paused"
    assert connector.phase_calls == []


@pytest.mark.asyncio
async def test_bootstrap_waits_for_existing_admin_activity(tmp_path: Path):
    service, manifest = _paused_checkpoint(tmp_path)
    connector = BootstrapClassroom()
    registry = ActivityRegistry()
    existing = registry.acquire("classroom-index-refresh")

    async def release_existing() -> None:
        await asyncio.sleep(0.05)
        existing.release()

    release_task = asyncio.create_task(release_existing())
    summary = await AdditionsFirstBootstrapExecutor(
        service.store,
        connector,
        activity_registry=registry,
        activity_wait_seconds=1.0,
    ).execute(
        manifest.id,
        import_id_ack=manifest.import_id,
        deferred_verification_ack=True,
        now=NOW,
    )
    await release_task

    assert summary.manifest.status == "completed"
    assert connector.phase_calls == [
        "course_create",
        "student_add",
        "course_activate",
        "teacher_add",
    ]
