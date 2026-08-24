from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gamgui.components.oneroster import OneRosterService, ThresholdProfile
from gamgui.components.oneroster.executor import OneRosterExecutor
from gamgui.components.oneroster.models import (
    ManagedCourseDesired,
    ManagedCourseDirty,
    OneRosterError,
)
from gamgui.components.oneroster.semantic import student_hash, teacher_hash
from gamgui.components.oneroster.store import OneRosterStore
from gamgui.core.classroom.models import (
    CourseDetail,
    CourseParticipant,
    CourseRosterSnapshot,
)
from gamgui.core.gam.models import BatchExecutionReceipt, GAMUser
from tests.test_oneroster_helpers import valid_files, zip_bytes


class DeltaConnector:
    oneroster_audit_sample_size = 0
    oneroster_stabilization_delays = (0.0, 0.0)

    def __init__(self) -> None:
        self.users = {
            email: GAMUser(email, user_id=user_id)
            for email, user_id in {
                "teacher@example.org": "teacher-id",
                "teacher2@example.org": "teacher2-id",
                "student@example.org": "student-id",
                "student-2@example.org": "student-2-id",
            }.items()
        }
        detail = CourseDetail(
            id="1000",
            name="Algebra I – P1 (2026-27)",
            section="P1",
            room="101",
            owner_id="teacher-id",
            owner_email="teacher@example.org",
            course_state="ACTIVE",
            aliases=("d:Section_101",),
        )
        self.courses = {"Section_101": detail}
        self.teachers = {"1000": {"teacher@example.org"}}
        self.students = {"1000": {"student@example.org"}}
        self.metadata_calls: list[tuple[str, ...]] = []
        self.roster_calls: list[tuple[tuple[str, ...], str]] = []
        self.directory_calls = 0
        self.batches: list[tuple[tuple[str, ...], ...]] = []
        self.incomplete_rosters = False
        self.unrelated_courses: dict[str, CourseDetail] = {}

    def reset_course_reads(self) -> None:
        self.metadata_calls.clear()
        self.roster_calls.clear()

    async def list_oneroster_directory(self):
        self.directory_calls += 1
        return dict(self.users)

    async def list_oneroster_managed_courses(self, aliases):
        requested = tuple(str(alias) for alias in aliases)
        self.metadata_calls.append(requested)
        requested_keys = {alias.removeprefix("d:").casefold() for alias in requested}
        return [
            detail
            for alias, detail in self.courses.items()
            if alias.casefold() in requested_keys
        ]

    async def get_course(self, course_id: str, **_kwargs):
        alias = str(course_id).removeprefix("d:")
        try:
            return self.courses[alias]
        except KeyError:
            raise KeyError(alias) from None

    async def list_course_participants_many(self, course_ids, role="all"):
        requested = tuple(str(course_id) for course_id in course_ids)
        self.roster_calls.append((requested, str(role)))
        if self.incomplete_rosters:
            return CourseRosterSnapshot.empty()
        participants: list[CourseParticipant] = []
        for course_id in requested:
            if role in {"all", "teachers"}:
                participants.extend(
                    CourseParticipant(
                        course_id=course_id,
                        email=email,
                        role="teachers",
                    )
                    for email in sorted(self.teachers.get(course_id, set()))
                )
            if role in {"all", "students"}:
                participants.extend(
                    CourseParticipant(
                        course_id=course_id,
                        email=email,
                        role="students",
                    )
                    for email in sorted(self.students.get(course_id, set()))
                )
        return CourseRosterSnapshot.from_participants(participants, requested)

    async def list_course_participants(self, course_id: str, role: str):
        source = self.teachers if role == "teachers" else self.students
        return [
            CourseParticipant(course_id=course_id, email=email, role=role)
            for email in sorted(source.get(course_id, set()))
        ]

    async def run_classroom_batch(
        self,
        commands,
        *,
        max_commands=50,
        worker_count=5,
    ):
        assert 0 < len(commands) <= max_commands <= 50
        self.batches.append(tuple(tuple(command) for command in commands))
        for raw_command in commands:
            command = list(raw_command)
            alias = command[1].removeprefix("d:")
            detail = self.courses[alias]
            operation, role, email = command[2], command[3], command[4]
            members = (
                self.teachers[detail.id]
                if role == "teachers"
                else self.students[detail.id]
            )
            if operation == "add":
                members.add(email)
            else:
                members.discard(email)
        return BatchExecutionReceipt(
            duration_seconds=0.001,
            worker_count=worker_count,
            outcome="completed",
        )


def _service(tmp_path: Path, files: dict[str, str] | None = None):
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(zip_bytes(files or valid_files()))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    return service, snapshot.id


async def _baseline(tmp_path: Path):
    service, import_id = _service(tmp_path)
    connector = DeltaConnector()
    plan = await service.build_live_plan(connector, import_id)
    return service, connector, import_id, plan


def _teacher_change_files() -> dict[str, str]:
    files = valid_files()
    files["users.csv"] += (
        "teacher-2,active,teacher2,teacher2@example.org,Grace,Teacher,t2,school-1\n"
    )
    files["enrollments.csv"] += (
        "enrollment-teacher-2,active,101,school-1,teacher-2,teacher,false,,\n"
    )
    return files


def _metadata_change_files() -> dict[str, str]:
    files = valid_files()
    files["courses.csv"] = files["courses.csv"].replace("Algebra I", "Geometry")
    return files


@pytest.mark.asyncio
async def test_first_baseline_then_identical_plan_skips_live_course_reads(
    tmp_path: Path,
):
    service, connector, import_id, first = await _baseline(tmp_path)

    assert first.actions == ()
    assert connector.metadata_calls == [("Section_101",)]
    assert connector.roster_calls == [
        (("1000",), "teachers"),
        (("1000",), "students"),
    ]
    state = service.store.get_managed_course_states(["Section_101"])["section_101"]
    assert state.course_id == "1000"
    assert state.desired_metadata_hash == state.verified_metadata_hash
    assert state.desired_teacher_hash == state.verified_teacher_hash
    assert state.desired_student_hash == state.verified_student_hash
    assert not (
        state.metadata_dirty
        or state.teacher_roster_dirty
        or state.student_roster_dirty
        or state.recovery_required
    )
    assert service.store.verified_managed_members("Section_101", "teachers") == (
        "teacher@example.org",
    )
    assert service.store.verified_managed_members("Section_101", "students") == (
        "student@example.org",
    )

    connector.reset_course_reads()
    second = await service.build_live_plan(connector, import_id)

    assert second.actions == ()
    assert connector.metadata_calls == []
    assert connector.roster_calls == []
    assert second.performance.total_managed_aliases == 1
    assert second.performance.candidate_aliases == 0
    assert second.performance.unchanged_aliases == 1
    assert second.performance.metadata_reads_requested == 0
    assert second.performance.teacher_rosters_requested == 0
    assert second.performance.student_rosters_requested == 0
    assert second.performance.cached_metadata_scopes == 1
    assert second.performance.cached_teacher_scopes == 1
    assert second.performance.cached_student_scopes == 1
    assert second.performance.audit_courses_requested == 0


@pytest.mark.parametrize(
    ("changed_files", "expected_role", "expected_kind"),
    [
        (lambda: valid_files(extra_users=1), "students", "student_add"),
        (_teacher_change_files, "teachers", "teacher_add"),
        (_metadata_change_files, None, "course_update"),
    ],
    ids=("students", "teachers", "metadata"),
)
@pytest.mark.asyncio
async def test_changed_scope_requests_only_its_live_evidence(
    tmp_path: Path,
    changed_files,
    expected_role: str | None,
    expected_kind: str,
):
    service, connector, _first_id, _first = await _baseline(tmp_path)
    second = service.upload(zip_bytes(changed_files()))
    connector.reset_course_reads()

    plan = await service.build_live_plan(connector, second.id)

    if expected_role is None:
        assert connector.metadata_calls == [("Section_101",)]
        assert connector.roster_calls == []
    else:
        assert connector.metadata_calls == []
        assert connector.roster_calls == [(('1000',), expected_role)]
    assert expected_kind in {action.kind for action in plan.actions}


@pytest.mark.asyncio
async def test_dirty_student_scope_is_reread_without_other_live_scopes(tmp_path: Path):
    service, connector, import_id, _first = await _baseline(tmp_path)
    service.store.mark_managed_course_dirty(
        (ManagedCourseDirty("Section_101", students=True),),
        error_code="TEST-INCOMPLETE",
    )
    connector.reset_course_reads()

    await service.build_live_plan(connector, import_id)

    assert connector.metadata_calls == []
    assert connector.roster_calls == [(('1000',), "students")]
    state = service.store.get_managed_course_states(["Section_101"])["section_101"]
    assert not state.student_roster_dirty


@pytest.mark.asyncio
async def test_removed_managed_alias_gets_fresh_metadata_read(tmp_path: Path):
    service, connector, first_id, _first = await _baseline(tmp_path)
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
    connector.reset_course_reads()

    plan = await service.build_live_plan(connector, second.id)

    requested_aliases = {
        alias.casefold()
        for call in connector.metadata_calls
        for alias in call
    }
    assert "section_101" in requested_aliases
    assert connector.roster_calls == []
    assert [(action.kind, action.subject) for action in plan.archive_actions] == [
        ("course_archive", "Section_101")
    ]


@pytest.mark.asyncio
async def test_ten_thousand_unrelated_tenant_courses_add_zero_managed_reads(
    tmp_path: Path,
):
    service, connector, import_id, _first = await _baseline(tmp_path)
    connector.unrelated_courses = {
        f"Unmanaged_{index}": CourseDetail(
            id=f"unrelated-{index}",
            name="Unmanaged",
            aliases=(f"d:Unmanaged_{index}",),
        )
        for index in range(10_000)
    }
    assert len(connector.unrelated_courses) == 10_000
    connector.reset_course_reads()

    await service.build_live_plan(connector, import_id)

    assert connector.metadata_calls == []
    assert connector.roster_calls == []


async def _student_execution_setup(tmp_path: Path):
    service, connector, _first_id, _first = await _baseline(tmp_path)
    second = service.upload(zip_bytes(valid_files(extra_users=1)))
    connector.reset_course_reads()
    planning = await service.build_live_plan(connector, second.id)
    assert [action.kind for action in planning.actions] == ["student_add"]
    manifest = service.persist_live_plan(planning).ordinary
    executor = OneRosterExecutor(
        service.store,
        connector,
        stabilization_delays=(0.0, 0.0),
    )
    service.store.confirm_manifest(manifest.id, manifest.import_id)
    service.store.claim_manifest(
        manifest.id,
        owner_id=executor._operation_owner,
        owner_identity=executor._operation_identity,
    )
    return service, connector, planning, manifest, executor


@pytest.mark.asyncio
async def test_executor_verification_updates_only_student_scope(tmp_path: Path):
    service, connector, planning, manifest, executor = await _student_execution_setup(
        tmp_path
    )
    before = service.store.get_managed_course_states(["Section_101"])["section_101"]
    connector.reset_course_reads()

    applied, failed = await executor._apply_chunk(
        manifest.id,
        manifest.actions,
        planning.owner_ids,
        set(),
    )

    after = service.store.get_managed_course_states(["Section_101"])["section_101"]
    assert (applied, failed) == (1, 0)
    assert connector.roster_calls == [(('1000',), "students")]
    assert after.verified_student_hash == after.desired_student_hash
    assert after.verified_student_hash != before.verified_student_hash
    assert after.verified_metadata_hash == before.verified_metadata_hash
    assert after.verified_teacher_hash == before.verified_teacher_hash
    assert after.last_metadata_verified_at == before.last_metadata_verified_at
    assert after.last_teacher_verified_at == before.last_teacher_verified_at
    assert not after.student_roster_dirty
    assert not after.recovery_required


@pytest.mark.asyncio
async def test_incomplete_executor_verification_dirties_only_student_scope(
    tmp_path: Path,
):
    service, connector, planning, manifest, executor = await _student_execution_setup(
        tmp_path
    )
    before = service.store.get_managed_course_states(["Section_101"])["section_101"]
    connector.incomplete_rosters = True
    connector.reset_course_reads()

    applied, failed = await executor._apply_chunk(
        manifest.id,
        manifest.actions,
        planning.owner_ids,
        set(),
    )

    after = service.store.get_managed_course_states(["Section_101"])["section_101"]
    assert (applied, failed) == (0, 1)
    assert connector.roster_calls == [
        (('1000',), "students"),
        (('1000',), "students"),
        (('1000',), "students"),
    ]
    assert after.student_roster_dirty
    assert after.recovery_required
    assert not after.metadata_dirty
    assert not after.teacher_roster_dirty
    assert after.verified_student_hash == before.verified_student_hash
    assert after.verified_metadata_hash == before.verified_metadata_hash
    assert after.verified_teacher_hash == before.verified_teacher_hash


def test_managed_state_migration_is_additive_and_idempotent(tmp_path: Path):
    root = tmp_path / "legacy-component"
    root.mkdir()
    state_path = root / "state.db"
    with sqlite3.connect(state_path) as conn:
        conn.execute("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO legacy_marker(id) VALUES (1)")

    first = OneRosterStore("example.org", root)
    second = OneRosterStore("example.org", root)

    assert first.state_path == second.state_path
    with sqlite3.connect(state_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(managed_course_state)")
        }
        assert conn.execute("SELECT id FROM legacy_marker").fetchone() == (1,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {"managed_course_state", "managed_course_members"} <= tables
    assert {
        "desired_metadata_hash",
        "verified_teacher_hash",
        "student_roster_dirty",
        "recovery_required",
    } <= columns

    digest = "0" * 64
    with pytest.raises(OneRosterError) as invalid:
        second.record_managed_course_desired(
            (
                ManagedCourseDesired(
                    "Arbitrary_1",
                    "import-id",
                    digest,
                    digest,
                    digest,
                ),
            )
        )
    assert invalid.value.code == "OR-ALIAS-INVALID"


def test_semantic_roster_hashes_are_role_specific_and_normalized():
    assert teacher_hash([" Teacher@Example.org ", "teacher@example.org"]) == (
        teacher_hash(["teacher@example.org"])
    )
    assert student_hash(["student@example.org"]) != teacher_hash(
        ["teacher@example.org"]
    )
