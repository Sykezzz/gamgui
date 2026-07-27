from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

import gamgui.components.oneroster.planner as planner_module
from gamgui.components.oneroster import (
    ActionLimit,
    OneRosterError,
    OneRosterService,
    ThresholdProfile,
)
from gamgui.core.classroom.models import (
    CourseDetail,
    CourseParticipant,
    CourseRosterSnapshot,
)
from gamgui.core.gam.models import GAMUser
from tests.test_oneroster_helpers import valid_files, zip_bytes


class BulkPlannerConnector:
    def __init__(self, courses: list[CourseDetail]) -> None:
        self.users = {
            "teacher@example.org": GAMUser("teacher@example.org"),
            "student@example.org": GAMUser("student@example.org"),
        }
        self.courses = courses
        self.calls: Counter[str] = Counter()

    async def list_oneroster_directory(self):
        self.calls["directory"] += 1
        return dict(self.users)

    async def list_oneroster_managed_courses(self, aliases):
        self.calls["courses"] += 1
        requested = {alias.casefold() for alias in aliases}
        return [
            course
            for course in self.courses
            if any(
                alias.removeprefix("d:").casefold() in requested
                for alias in course.aliases
            )
        ]

    async def list_course_participants_many(self, course_ids, role="all"):
        self.calls["rosters"] += 1
        assert role == "all"
        requested = set(course_ids)
        participants = []
        for course in self.courses:
            if course.id not in requested:
                continue
            participants.extend(
                [
                    CourseParticipant(
                        course_id=course.id,
                        email="teacher@example.org",
                        role="teachers",
                    ),
                    CourseParticipant(
                        course_id=course.id,
                        email="student@example.org",
                        role="students",
                    ),
                ]
            )
        return CourseRosterSnapshot.from_participants(
            participants,
            requested,
        )

    async def get_user(self, *_args, **_kwargs):
        self.calls["get_user"] += 1
        raise AssertionError("bulk planner must not perform per-user reads")

    async def get_course(self, *_args, **_kwargs):
        self.calls["get_course"] += 1
        raise AssertionError("bulk planner must not perform per-course reads")

    async def list_course_participants(self, *_args, **_kwargs):
        self.calls["roster_detail"] += 1
        raise AssertionError("bulk planner must not perform per-course roster reads")


def _ready_service(tmp_path: Path) -> tuple[OneRosterService, str]:
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(zip_bytes(valid_files()))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    service.mark_scope_ready()
    return service, snapshot.id


def _managed_course(
    *,
    course_id: str = "1000",
    alias: str = "d:Section_101",
    state: str = "ACTIVE",
) -> CourseDetail:
    return CourseDetail(
        id=course_id,
        name="Algebra I \u2013 P1 (2026-27)",
        section="P1",
        room="101",
        owner_email="teacher@example.org",
        course_state=state,
        aliases=(alias,),
    )


@pytest.mark.asyncio
async def test_planner_reuses_each_bulk_snapshot_and_never_reads_details(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = BulkPlannerConnector([_managed_course()])

    plan = await service.build_live_plan(connector, import_id)

    assert plan.actions == ()
    assert connector.calls == Counter(
        {
            "directory": 1,
            "courses": 1,
            "rosters": 1,
        }
    )


@pytest.mark.asyncio
async def test_action_finalization_runs_off_loop_and_keeps_heartbeat_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service, import_id = _ready_service(tmp_path)
    connector = BulkPlannerConnector([_managed_course()])
    original = planner_module._build_action_payload
    worker_threads: list[int] = []

    def slow_district_sized_finalization(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        # Model the finalization latency of a district-sized action set. If the
        # helper regresses to an in-loop call, the heartbeat cannot run here.
        time.sleep(0.1)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        planner_module,
        "_build_action_payload",
        slow_district_sized_finalization,
    )
    main_thread = threading.get_ident()
    plan_task = asyncio.create_task(
        service.build_live_plan(connector, import_id)
    )
    heartbeat_count = 0

    async def heartbeat() -> None:
        nonlocal heartbeat_count
        while not plan_task.done():
            heartbeat_count += 1
            await asyncio.sleep(0.005)

    heartbeat_task = asyncio.create_task(heartbeat())
    plan = await plan_task
    await heartbeat_task

    assert plan.actions == ()
    assert worker_threads and worker_threads[0] != main_thread
    assert heartbeat_count >= 5


@pytest.mark.asyncio
async def test_planner_fails_closed_before_loading_oversized_enrollment_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service, import_id = _ready_service(tmp_path)
    connector = BulkPlannerConnector([_managed_course()])
    monkeypatch.setattr(
        planner_module,
        "MAX_ONEROSTER_PLANNING_ENROLLMENTS",
        1,
    )

    with pytest.raises(OneRosterError) as blocked:
        await service.build_live_plan(connector, import_id)

    assert blocked.value.code == "OR-PLAN-SCALE-LIMIT"
    assert "inspection and CSV export" in blocked.value.message
    assert connector.calls == Counter()


@pytest.mark.asyncio
async def test_planner_fails_closed_before_loading_oversized_course_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service, import_id = _ready_service(tmp_path)
    connector = BulkPlannerConnector([_managed_course()])
    monkeypatch.setattr(
        planner_module,
        "MAX_ONEROSTER_PLANNING_COURSES",
        0,
    )

    with pytest.raises(OneRosterError) as blocked:
        await service.build_live_plan(connector, import_id)

    assert blocked.value.code == "OR-PLAN-SCALE-LIMIT"
    assert "class records" in blocked.value.message
    assert connector.calls == Counter()


@pytest.mark.asyncio
async def test_planner_fails_closed_at_bounded_action_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service, import_id = _ready_service(tmp_path)
    connector = BulkPlannerConnector([])
    monkeypatch.setattr(
        planner_module,
        "MAX_ONEROSTER_PLANNING_ACTIONS",
        2,
    )

    with pytest.raises(OneRosterError) as blocked:
        await service.build_live_plan(connector, import_id)

    assert blocked.value.code == "OR-PLAN-SCALE-LIMIT"
    assert "No manifest was created" in blocked.value.message
    assert connector.calls == Counter({"directory": 1, "courses": 1})


@pytest.mark.asyncio
async def test_planner_rejects_bulk_snapshot_without_requested_course_proof(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)

    class IncompleteRosterConnector(BulkPlannerConnector):
        async def list_course_participants_many(self, course_ids, role="all"):
            self.calls["rosters"] += 1
            return CourseRosterSnapshot.empty()

    connector = IncompleteRosterConnector([_managed_course()])

    with pytest.raises(OneRosterError) as failure:
        await service.build_live_plan(connector, import_id)

    assert failure.value.code == "OR-CLASSROOM-ROSTER-READ"
    assert connector.calls["rosters"] == 1


@pytest.mark.asyncio
async def test_planner_fails_closed_on_duplicate_exact_managed_alias(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = BulkPlannerConnector(
        [
            _managed_course(course_id="1000"),
            _managed_course(course_id="2000"),
        ]
    )

    with pytest.raises(OneRosterError) as collision:
        await service.build_live_plan(connector, import_id)

    assert collision.value.code == "OR-ALIAS-COLLISION"
    assert connector.calls == Counter({"directory": 1, "courses": 1})


@pytest.mark.asyncio
async def test_limited_plan_does_not_reactivate_existing_archived_course(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    connector = BulkPlannerConnector([_managed_course(state="ARCHIVED")])

    plan = await service.build_live_plan(
        connector,
        import_id,
        limited_import=True,
    )

    assert plan.actions == ()
    assert not any(action.kind == "course_activate" for action in plan.actions)
    assert connector.calls == Counter(
        {
            "directory": 1,
            "courses": 1,
            "rosters": 1,
        }
    )


@pytest.mark.asyncio
async def test_course_activation_consumes_course_update_threshold(
    tmp_path: Path,
):
    service, import_id = _ready_service(tmp_path)
    service.save_threshold_profile(
        ThresholdProfile(
            configured=True,
            limits={"course_update": ActionLimit(max_count=0)},
        )
    )
    connector = BulkPlannerConnector(
        [_managed_course(state="PROVISIONED")]
    )

    plan = await service.build_live_plan(connector, import_id)

    assert [action.kind for action in plan.actions] == ["course_activate"]
    assert plan.threshold_evaluation.counts["course_update"] == 1
    assert plan.threshold_evaluation.held
    assert [item.action for item in plan.threshold_evaluation.breaches] == [
        "course_update"
    ]


@pytest.mark.asyncio
async def test_archive_planning_reuses_managed_course_snapshot(tmp_path: Path):
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
    connector = BulkPlannerConnector([_managed_course()])

    plan = await service.build_live_plan(connector, second.id)

    assert [action.kind for action in plan.archive_actions] == ["course_archive"]
    assert plan.archive_actions[0].subject == "Section_101"
    assert connector.calls == Counter({"directory": 1, "courses": 1})


@pytest.mark.asyncio
async def test_archive_baseline_survives_expired_snapshot_material(
    tmp_path: Path,
):
    service, first_id = _ready_service(tmp_path)
    first = service.get_import(first_id)
    service.mark_accepted(first_id)
    assert service.cleanup_expired(now=first.expires_at + 1) == 1
    assert not service.store.normalized_path(first_id).exists()

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
    connector = BulkPlannerConnector([_managed_course()])

    plan = await service.build_live_plan(connector, second.id)

    assert [action.kind for action in plan.archive_actions] == [
        "course_archive"
    ]
    assert plan.archive_actions[0].subject == "Section_101"


@pytest.mark.asyncio
async def test_malformed_role_protects_managed_course_from_removal_or_archive(
    tmp_path: Path,
):
    service, first_id = _ready_service(tmp_path)
    service.mark_accepted(first_id)
    files = valid_files()
    files["enrollments.csv"] = files["enrollments.csv"].replace(
        ",student,false,,",
        ",learner,false,,",
    )
    second = service.upload(zip_bytes(files))
    connector = BulkPlannerConnector([_managed_course()])

    plan = await service.build_live_plan(connector, second.id)

    assert plan.actions == ()
    assert plan.archive_actions == ()
    assert plan.ownership_actions == ()
    assert any(issue.code == "OR-ENROLLMENT-ROLE" for issue in plan.issues)


@pytest.mark.asyncio
async def test_invalid_term_protects_present_alias_from_archive(tmp_path: Path):
    files = valid_files()
    files["classes.csv"] += (
        "202,active,Geometry Section,P2,202,course-1,term-1,school-1,9\n"
    )
    files["enrollments.csv"] += (
        "enrollment-teacher-202,active,202,school-1,teacher-1,teacher,true,,\n"
        "enrollment-student-202,active,202,school-1,student-1,student,false,,\n"
    )
    service = OneRosterService("example.org", tmp_path / "component")
    first = service.upload(zip_bytes(files))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    service.mark_scope_ready()
    service.mark_accepted(first.id)

    next_files = dict(files)
    next_files["classes.csv"] = next_files["classes.csv"].replace(
        "202,active,Geometry Section,P2,202,course-1,term-1,school-1,9",
        "202,active,Geometry Section,P2,202,course-1,missing-term,school-1,9",
    )
    second = service.upload(zip_bytes(next_files))
    connector = BulkPlannerConnector(
        [
            _managed_course(),
            _managed_course(course_id="2000", alias="d:Section_202"),
        ]
    )

    plan = await service.build_live_plan(connector, second.id)

    assert second.ready_for_apply
    assert plan.archive_actions == ()
    assert any(issue.code == "OR-REFERENCE-TERM" for issue in plan.issues)
