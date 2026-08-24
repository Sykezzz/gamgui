from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from gamgui.components.oneroster import OneRosterService, ThresholdProfile
from gamgui.components.oneroster.models import ManagedCourseDirty, OneRosterError
from gamgui.components.oneroster.planner import OneRosterPlanner
from gamgui.components.oneroster.store import OneRosterStore
from gamgui.core.classroom.models import CourseDetail, CourseRosterSnapshot
from gamgui.core.connectors.gam_connector import (
    GAMConnector,
    ONEROSTER_MANAGED_ALIAS_CHUNK_CAP,
    ONEROSTER_ROSTER_CHUNK_CAP,
)
from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.gam.models import GAMUser
from tests.test_oneroster_helpers import csv_text, valid_files, zip_bytes


def _alias(index: int) -> str:
    return f"Section_{100_000 + index:06d}"


def _course_id(index: int) -> str:
    return f"course-{index:06d}"


def _detail(index: int, *, course_id: str | None = None) -> CourseDetail:
    return CourseDetail(
        id=course_id or _course_id(index),
        name=f"Algebra I – P{index:04d} (2026-27)",
        section=f"P{index:04d}",
        room=f"R{index:04d}",
        owner_id="teacher-id",
        owner_email="teacher@example.org",
        course_state="ACTIVE",
        aliases=(f"d:{_alias(index)}",),
    )


def _files_for_courses(count: int) -> dict[str, str]:
    files = valid_files()
    classes = []
    enrollments = []
    for index in range(1, count + 1):
        class_id = str(100_000 + index)
        classes.append(
            {
                "sourcedId": class_id,
                "status": "active",
                "title": f"Algebra Section {index:04d}",
                "classCode": f"P{index:04d}",
                "location": f"R{index:04d}",
                "courseSourcedId": "course-1",
                "terms": "term-1",
                "schoolSourcedId": "school-1",
                "grades": "9",
            }
        )
        enrollments.extend(
            (
                {
                    "sourcedId": f"teacher-{index:06d}",
                    "status": "active",
                    "classSourcedId": class_id,
                    "schoolSourcedId": "school-1",
                    "userSourcedId": "teacher-1",
                    "role": "teacher",
                    "primary": "true",
                    "beginDate": "",
                    "endDate": "",
                },
                {
                    "sourcedId": f"student-{index:06d}",
                    "status": "active",
                    "classSourcedId": class_id,
                    "schoolSourcedId": "school-1",
                    "userSourcedId": "student-1",
                    "role": "student",
                    "primary": "false",
                    "beginDate": "",
                    "endDate": "",
                },
            )
        )
    files["classes.csv"] = csv_text(
        (
            "sourcedId",
            "status",
            "title",
            "classCode",
            "location",
            "courseSourcedId",
            "terms",
            "schoolSourcedId",
            "grades",
        ),
        classes,
    )
    files["enrollments.csv"] = csv_text(
        (
            "sourcedId",
            "status",
            "classSourcedId",
            "schoolSourcedId",
            "userSourcedId",
            "role",
            "primary",
            "beginDate",
            "endDate",
        ),
        enrollments,
    )
    return files


class ChunkConnector:
    oneroster_audit_sample_size = 0
    oneroster_read_max_concurrency = 4
    oneroster_read_max_attempts = 3
    oneroster_read_backoff_base_seconds = 1.0
    oneroster_read_backoff_cap_seconds = 8.0

    def __init__(self, count: int) -> None:
        self.users = {
            "teacher@example.org": GAMUser(
                "teacher@example.org",
                user_id="teacher-id",
            ),
            "student@example.org": GAMUser(
                "student@example.org",
                user_id="student-id",
            ),
        }
        self.courses = {_alias(index): _detail(index) for index in range(1, count + 1)}
        self.teachers = {
            _course_id(index): {"teacher@example.org"}
            for index in range(1, count + 1)
        }
        self.students = {
            _course_id(index): {"student@example.org"}
            for index in range(1, count + 1)
        }
        self.metadata_calls: list[tuple[str, ...]] = []
        self.roster_calls: list[tuple[tuple[str, ...], str]] = []
        self.get_course_calls = 0
        self.metadata_failures: dict[tuple[str, ...], list[BaseException]] = {}
        self.roster_failures: dict[
            tuple[tuple[str, ...], str],
            list[BaseException | CourseRosterSnapshot],
        ] = {}
        self.sleep_delays: list[float] = []
        self.active_reads = 0
        self.maximum_active_reads = 0
        self.unrelated_courses: dict[str, CourseDetail] = {}
        self.oneroster_read_sleep = self._record_sleep
        self.oneroster_read_jitter = lambda: 0.0

    async def _record_sleep(self, delay: float) -> None:
        self.sleep_delays.append(delay)
        await asyncio.sleep(0)

    async def _begin_read(self) -> None:
        self.active_reads += 1
        self.maximum_active_reads = max(self.maximum_active_reads, self.active_reads)
        await asyncio.sleep(0)

    def _finish_read(self) -> None:
        self.active_reads -= 1

    def reset_reads(self) -> None:
        self.metadata_calls.clear()
        self.roster_calls.clear()
        self.get_course_calls = 0
        self.sleep_delays.clear()
        self.active_reads = 0
        self.maximum_active_reads = 0

    async def list_oneroster_directory(self):
        return dict(self.users)

    async def get_user(self, email: str, **_kwargs):
        return self.users[email.casefold()]

    async def get_course(self, course_ref: str, **_kwargs):
        self.get_course_calls += 1
        return self.courses[course_ref.removeprefix("d:")]

    async def list_oneroster_managed_courses(self, aliases):
        requested = tuple(str(alias) for alias in aliases)
        self.metadata_calls.append(requested)
        await self._begin_read()
        try:
            failures = self.metadata_failures.get(requested)
            if failures:
                raise failures.pop(0)
            return [
                self.courses[alias.removeprefix("d:")]
                for alias in requested
                if alias.removeprefix("d:") in self.courses
            ]
        finally:
            self._finish_read()

    async def list_course_participants_many(self, course_ids, role="all"):
        requested = tuple(str(course_id) for course_id in course_ids)
        selected_role = str(role)
        key = (requested, selected_role)
        self.roster_calls.append(key)
        await self._begin_read()
        try:
            failures = self.roster_failures.get(key)
            if failures:
                outcome = failures.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
            return CourseRosterSnapshot(
                {
                    course_id: (
                        frozenset(self.teachers.get(course_id, set()))
                        if selected_role == "teachers"
                        else frozenset(),
                        frozenset(self.students.get(course_id, set()))
                        if selected_role == "students"
                        else frozenset(),
                    )
                    for course_id in requested
                },
                frozenset(requested),
            )
        finally:
            self._finish_read()

    async def list_course_participants(self, course_id: str, role: str):
        source = self.teachers if role == "teachers" else self.students
        return tuple(
            SimpleNamespace(email=email)
            for email in sorted(source.get(course_id, set()))
        )


def _ready_service(
    tmp_path: Path,
    count: int,
) -> tuple[OneRosterService, str, ChunkConnector]:
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(zip_bytes(_files_for_courses(count)))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    return service, snapshot.id, ChunkConnector(count)


def _dirty_students(service: OneRosterService, count: int, code: str = "TEST-DIRTY") -> None:
    service.store.mark_managed_course_dirty(
        tuple(
            ManagedCourseDirty(_alias(index), students=True)
            for index in range(1, count + 1)
        ),
        error_code=code,
    )


class EmptySpoolRunner:
    def __init__(self, root: Path) -> None:
        self.base_dir = root
        self.timeout = 120.0
        self.calls = 0

    @asynccontextmanager
    async def run_authenticated_to_file(self, *_args, **_kwargs):
        self.calls += 1
        fd, raw_path = tempfile.mkstemp(dir=str(self.base_dir))
        os.close(fd)
        path = Path(raw_path)
        try:
            yield SimpleNamespace(path=path)
        finally:
            path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_connector_caps_normalize_duplicates_and_keep_empty_reads_safe(
    tmp_path: Path,
):
    runner = EmptySpoolRunner(tmp_path)
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]

    assert await connector.list_oneroster_managed_courses([]) == []
    assert (await connector.list_course_participants_many([], "students")).covers([])
    assert await connector.list_oneroster_managed_courses(["Section_1"] * 1_001) == []
    assert runner.calls == 1

    with pytest.raises(ValueError, match="alias request exceeds"):
        await connector.list_oneroster_managed_courses(
            [f"Section_{index}" for index in range(ONEROSTER_MANAGED_ALIAS_CHUNK_CAP + 1)]
        )
    with pytest.raises(ValueError, match="roster request exceeds"):
        await connector.list_course_participants_many(
            [str(index) for index in range(ONEROSTER_ROSTER_CHUNK_CAP + 1)],
            "students",
        )
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_1001_aliases_use_deterministic_bounded_adaptive_metadata_chunks(
    tmp_path: Path,
):
    connector = ChunkConnector(1_001)
    planner = OneRosterPlanner(
        OneRosterStore("example.org", tmp_path / "component"),
        connector,
    )
    aliases = [_alias(index) for index in range(1_001, 0, -1)]

    indexed = await planner._read_managed_course_snapshot(aliases)

    expected = tuple(sorted(set(aliases), key=str.casefold))
    assert indexed is not None and len(indexed) == 1_001
    assert tuple(alias for call in connector.metadata_calls for alias in call) == expected
    assert [len(call) for call in connector.metadata_calls] == [200, 200, 200, 200, 200, 1]
    assert max(map(len, connector.metadata_calls)) == ONEROSTER_MANAGED_ALIAS_CHUNK_CAP
    assert planner._read_performance.metadata_chunk_count == 6
    assert planner._read_performance.largest_metadata_chunk == 200
    assert planner._read_performance.metadata_chunk_worker_levels == [1, 1, 2, 2, 3, 3]
    assert planner._read_performance.maximum_observed_read_concurrency == 2
    assert connector.maximum_active_reads == 2


@pytest.mark.asyncio
async def test_501_student_candidates_use_only_bounded_student_chunks(tmp_path: Path):
    service, import_id, connector = _ready_service(tmp_path, 501)
    await service.build_live_plan(connector, import_id)
    _dirty_students(service, 501)
    connector.reset_reads()

    plan = await service.build_live_plan(connector, import_id)

    assert connector.metadata_calls == []
    assert {role for _ids, role in connector.roster_calls} == {"students"}
    assert [len(ids) for ids, _role in connector.roster_calls] == [100, 100, 100, 100, 100, 1]
    assert max(len(ids) for ids, _role in connector.roster_calls) == ONEROSTER_ROSTER_CHUNK_CAP
    assert plan.performance.student_roster_chunk_count == 6
    assert plan.performance.teacher_roster_chunk_count == 0
    assert plan.performance.largest_roster_chunk == 100


@pytest.mark.asyncio
async def test_teacher_and_student_candidates_are_chunked_independently(tmp_path: Path):
    service, import_id, connector = _ready_service(tmp_path, 3)
    await service.build_live_plan(connector, import_id)
    service.store.mark_managed_course_dirty(
        (
            ManagedCourseDirty(_alias(1), teachers=True),
            ManagedCourseDirty(_alias(2), students=True),
            ManagedCourseDirty(_alias(3), teachers=True, students=True),
        ),
        error_code="TEST-DIRTY",
    )
    connector.reset_reads()

    await service.build_live_plan(connector, import_id)

    assert connector.roster_calls == [
        ((_course_id(1), _course_id(3)), "teachers"),
        ((_course_id(2), _course_id(3)), "students"),
    ]
    assert _course_id(2) not in connector.roster_calls[0][0]


@pytest.mark.asyncio
async def test_roster_failure_preserves_first_chunk_and_retry_skips_it(tmp_path: Path):
    service, import_id, connector = _ready_service(tmp_path, 201)
    await service.build_live_plan(connector, import_id)
    _dirty_students(service, 201)
    connector.reset_reads()
    ordered_ids = tuple(_course_id(index) for index in range(1, 202))
    failed_chunk = ordered_ids[100:200]
    rate_error = lambda: GAMError(
        GAMErrorKind.RATE_LIMITED,
        exit_code=1,
        stderr="429",
    )
    connector.roster_failures[(failed_chunk, "students")] = [
        rate_error(),
        rate_error(),
        rate_error(),
    ]

    with pytest.raises(OneRosterError) as failure:
        await service.build_live_plan(connector, import_id)

    assert failure.value.code == "OR-ONEROSTER-READ-RATE-LIMITED"
    assert connector.roster_calls == [
        (ordered_ids[:100], "students"),
        (failed_chunk, "students"),
        (failed_chunk, "students"),
        (failed_chunk, "students"),
    ]
    assert connector.sleep_delays == [1.0, 2.0]
    states = service.store.get_managed_course_states(
        (_alias(1), _alias(101), _alias(201))
    )
    assert not states[_alias(1).casefold()].student_roster_dirty
    assert states[_alias(101).casefold()].student_roster_dirty
    assert states[_alias(101).casefold()].last_error_code == (
        "OR-ONEROSTER-READ-RATE-LIMITED"
    )
    assert states[_alias(201).casefold()].last_error_code == "TEST-DIRTY"
    assert not states[_alias(101).casefold()].metadata_dirty
    assert not states[_alias(101).casefold()].teacher_roster_dirty

    connector.roster_failures.clear()
    connector.reset_reads()
    await service.build_live_plan(connector, import_id)

    retried_ids = tuple(
        course_id
        for ids, role in connector.roster_calls
        if role == "students"
        for course_id in ids
    )
    assert not set(ordered_ids[:100]) & set(retried_ids)
    assert retried_ids == ordered_ids[100:]
    assert [len(ids) for ids, _role in connector.roster_calls] == [100, 1]


@pytest.mark.asyncio
async def test_rate_limited_metadata_retries_same_chunk_without_alias_fanout(
    tmp_path: Path,
):
    connector = ChunkConnector(1_001)
    planner = OneRosterPlanner(
        OneRosterStore("example.org", tmp_path / "component"),
        connector,
    )
    aliases = tuple(_alias(index) for index in range(1, 1_002))
    ordered = tuple(sorted(aliases, key=str.casefold))
    throttled = ordered[400:600]
    connector.metadata_failures[throttled] = [
        GAMError(GAMErrorKind.RATE_LIMITED, exit_code=1, stderr="429")
    ]

    await planner._read_managed_course_snapshot(aliases)

    assert connector.metadata_calls.count(throttled) == 2
    assert connector.get_course_calls == 0
    assert connector.sleep_delays == [1.0]
    assert planner._read_performance.rate_limit_count == 1
    assert planner._read_performance.retried_chunk_count == 1
    assert planner._read_performance.metadata_chunk_worker_levels == [1, 1, 2, 2, 1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (GAMErrorKind.AUTH_EXPIRED, "OR-ONEROSTER-READ-AUTH"),
        (GAMErrorKind.PERMISSION_DENIED, "OR-ONEROSTER-READ-PERMISSION"),
    ],
)
async def test_auth_and_permission_failures_never_fan_out_to_alias_reads(
    tmp_path: Path,
    kind: GAMErrorKind,
    code: str,
):
    connector = ChunkConnector(1)
    planner = OneRosterPlanner(
        OneRosterStore("example.org", tmp_path / kind.value),
        connector,
    )
    chunk = (_alias(1),)
    connector.metadata_failures[chunk] = [
        GAMError(kind, exit_code=1, stderr=kind.value)
    ]

    with pytest.raises(OneRosterError) as failure:
        await planner._read_managed_course_snapshot(chunk)

    assert failure.value.code == code
    assert connector.metadata_calls == [chunk]
    assert connector.get_course_calls == 0


@pytest.mark.asyncio
async def test_incomplete_roster_coverage_is_dirty_and_not_verified(tmp_path: Path):
    service, import_id, connector = _ready_service(tmp_path, 1)
    await service.build_live_plan(connector, import_id)
    before = service.store.get_managed_course_states((_alias(1),))[
        _alias(1).casefold()
    ]
    _dirty_students(service, 1)
    connector.reset_reads()
    key = ((_course_id(1),), "students")
    connector.roster_failures[key] = [CourseRosterSnapshot.empty()]

    with pytest.raises(OneRosterError) as failure:
        await service.build_live_plan(connector, import_id)

    after = service.store.get_managed_course_states((_alias(1),))[
        _alias(1).casefold()
    ]
    assert failure.value.code == "OR-CLASSROOM-ROSTER-READ"
    assert after.student_roster_dirty
    assert after.verified_student_hash == before.verified_student_hash
    assert after.last_student_verified_at == before.last_student_verified_at
    assert not after.metadata_dirty
    assert not after.teacher_roster_dirty


@pytest.mark.asyncio
async def test_duplicate_course_id_mapping_across_metadata_chunks_fails_closed(
    tmp_path: Path,
):
    connector = ChunkConnector(201)
    connector.courses[_alias(201)] = _detail(201, course_id=_course_id(1))
    planner = OneRosterPlanner(
        OneRosterStore("example.org", tmp_path / "component"),
        connector,
    )

    with pytest.raises(OneRosterError) as failure:
        await planner._read_managed_course_snapshot(
            tuple(_alias(index) for index in range(1, 202))
        )

    assert failure.value.code == "OR-ALIAS-COLLISION"
    assert connector.get_course_calls == 0


@pytest.mark.asyncio
async def test_ten_thousand_unrelated_courses_add_zero_managed_reads(tmp_path: Path):
    service, import_id, connector = _ready_service(tmp_path, 1)
    await service.build_live_plan(connector, import_id)
    connector.unrelated_courses = {
        f"unrelated-{index}": CourseDetail(
            id=f"unrelated-{index}",
            name="Unmanaged",
            aliases=(f"d:Other_{index}",),
        )
        for index in range(10_000)
    }
    connector.reset_reads()

    await service.build_live_plan(connector, import_id)

    assert len(connector.unrelated_courses) == 10_000
    assert connector.metadata_calls == []
    assert connector.roster_calls == []
