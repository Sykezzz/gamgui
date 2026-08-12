from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gamgui.core.classroom.index import CourseIndex
from gamgui.core.classroom.manifests import RosterManifestStore
from gamgui.core.classroom.models import CourseSummary
from gamgui.web.routes.classroom import _monitoring_view, router
from tests.classroom_fakes import FakeClassroomConnector


class FakeMonitoringRoster:
    def __init__(self, *, heartbeat_state: str = "current") -> None:
        self.heartbeat_state = heartbeat_state
        self.calls: list[tuple] = []

    def dashboard(self):
        self.calls.append(("dashboard",))
        return {"latest": {"id": "import-1", "state": "ready"}}

    def get_gate(self):
        self.calls.append(("get_gate",))
        return {"state": "CLOSED"}

    def latest_manifest_header(self, import_id=None):
        self.calls.append(("latest_manifest_header", import_id))
        return {
            "id": "manifest-1",
            "import_id": "import-1",
            "status": "running",
        }

    def get_execution_progress(self, manifest_id):
        self.calls.append(("get_execution_progress", manifest_id))
        return {
            "run": {
                "status": "running",
                "phase": "student_add",
                "started_at": 1_786_542_840.0,
                "current_batch_sequence": 4,
                "planning_seconds": 18.4,
            },
            "total": 432,
            "pending": 232,
            "applied": 200,
            "failed": 0,
            "skipped": 0,
            "percent": 46.3,
            "completed_batches": 1,
            "total_batches_estimate": 9,
            "elapsed_seconds": 420.0,
            "heartbeat_age_seconds": 190.0 if self.heartbeat_state == "stale" else 4.0,
            "heartbeat_stale": self.heartbeat_state == "stale",
            "heartbeat_delayed": self.heartbeat_state in {"delayed", "stale"},
            "heartbeat_state": self.heartbeat_state,
            "actions_per_minute": 112.0,
            "eta_seconds": None,
            "worker_count": 8,
            "adaptive_state": "normal",
            "course_count": 88,
            "remaining_course_count": 41,
            "phases": [
                {
                    "key": "classes",
                    "label": "Class setup",
                    "total": 88,
                    "finished": 88,
                    "applied": 88,
                    "failed": 0,
                    "skipped": 0,
                    "pending": 0,
                },
                {
                    "key": "teachers",
                    "label": "Teacher access",
                    "total": 84,
                    "finished": 84,
                    "applied": 84,
                    "failed": 0,
                    "skipped": 0,
                    "pending": 0,
                },
                {
                    "key": "students",
                    "label": "Student roster",
                    "total": 260,
                    "finished": 28,
                    "applied": 28,
                    "failed": 0,
                    "skipped": 0,
                    "pending": 232,
                },
                {
                    "key": "other",
                    "label": "Other checked work",
                    "total": 1,
                    "finished": 1,
                    "applied": 0,
                    "failed": 0,
                    "skipped": 1,
                    "pending": 0,
                },
            ],
            "current_batch": {
                "sequence_number": 4,
                "phase": "student_add",
                "phase_key": "students",
                "phase_label": "Student roster",
                "action_count": 50,
                "course_count": 11,
                "status": "running",
                "apply_seconds": 23.6,
                "verification_seconds": 12.0,
                "persistence_seconds": 0.08,
                "verification_attempts": 1,
                "worker_count": 8,
                "throttling_count": 0,
            },
        }


@pytest.mark.parametrize(
    (
        "manifest_status",
        "run_status",
        "heartbeat",
        "adaptive",
        "failed",
        "state",
        "polling",
    ),
    (
        ("running", "running", "current", "normal", 0, "running", True),
        ("running", "running", "delayed", "normal", 0, "delayed", True),
        ("running", "running", "current", "protecting_google", 0, "protecting", True),
        ("running", "pause_requested", "current", "normal", 0, "pausing", True),
        ("running", "running", "stale", "normal", 0, "stale", True),
        (
            "recovery_required",
            "recovery_required",
            "current",
            "normal",
            0,
            "recovery",
            False,
        ),
        ("completed", "completed", "current", "normal", 0, "completed", False),
        ("failed", "failed", "current", "normal", 0, "failed", False),
        ("running", "running", "current", "normal", 1, "issues", True),
        ("paused", "paused", "current", "normal", 0, "paused", False),
        ("planned", "", "current", "normal", 0, "planned", False),
    ),
)
def test_monitoring_view_projects_distinct_operator_states(
    manifest_status,
    run_status,
    heartbeat,
    adaptive,
    failed,
    state,
    polling,
):
    view = _monitoring_view(
        {
            "manifest": {"status": manifest_status},
            "progress": {
                "run": {"status": run_status, "started_at": 100.0},
                "failed": failed,
                "heartbeat_state": heartbeat,
                "adaptive_state": adaptive,
                "completed_batches": 1,
                "eta_seconds": None,
                "elapsed_seconds": 120.0,
                "percent": 25.0,
            },
        }
    )

    assert view["state"] == state
    assert view["polling"] is polling
    if state not in {"stale", "recovery", "failed", "completed"}:
        assert view["eta_caption"] == "Learning the pace…"


@pytest.fixture
def web_client(tmp_path):
    connector = FakeClassroomConnector()
    index = CourseIndex(tmp_path / "courses.db")
    summaries = [
        CourseSummary(
            id=str(number),
            name=f"Course {number:03d}",
            section=f"Section_{number:03d}",
            room=f"R{number % 10}",
            owner_id=f"owner-{number % 4}",
            course_state="ACTIVE",
        )
        for number in range(75)
    ]
    # Preserve the live fake course as the first indexed record.
    summaries.append(connector.courses["123"])
    index.replace_all("example.com", summaries)
    manifests = RosterManifestStore(tmp_path / "operations.db")
    state = SimpleNamespace(
        connector=connector,
        audit_domain="example.com",
        classroom_index=index,
        classroom_manifests=manifests,
    )
    app = FastAPI()
    app.state.gamgui = state
    app.include_router(router)
    return TestClient(app), connector, state


def test_classroom_page_uses_bounded_index_without_live_gam(web_client):
    client, connector, _ = web_client
    response = client.get("/classroom")
    assert response.status_code == 200
    assert "Course administration" in response.text
    assert "Your district at a glance" in response.text
    assert response.text.count(">Manage") == 1
    courses = client.get("/classroom/courses/manage")
    assert courses.status_code == 200
    assert courses.text.count(">Manage") == 50
    assert "Next 50 courses" in courses.text
    assert not any(call[0] == "list_courses" for call in connector.calls)
    assert len(courses.content) < 100_000


def test_monitoring_renders_live_operations_without_connector_reads(web_client):
    client, connector, state = web_client
    roster = FakeMonitoringRoster()
    state.oneroster_service = roster

    response = client.get("/classroom/monitoring")

    assert response.status_code == 200
    assert "432 approved changes across 88 classes" in response.text
    assert "Learning the pace" in response.text
    assert "Class setup" in response.text
    assert "Student roster" in response.text
    assert "50 approved changes across 11 classes" in response.text
    assert "Classes still ahead" in response.text
    assert 'hx-get="/classroom/monitoring/status"' in response.text
    assert 'hx-sync="this:drop"' in response.text
    assert "Working normally" not in response.text
    assert connector.calls == []

    partial = client.get(
        "/classroom/monitoring/status",
        headers={"HX-Request": "true"},
    )
    assert partial.status_code == 200
    assert 'id="monitoring-live"' in partial.text
    assert "Live import operations" not in partial.text


def test_monitoring_stale_state_links_to_recovery_and_receipt_is_sanitized(
    web_client,
):
    client, _connector, state = web_client
    state.oneroster_service = FakeMonitoringRoster(heartbeat_state="stale")

    response = client.get("/classroom/monitoring")
    receipt = client.get(
        "/classroom/monitoring/receipt",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "has not checked in for three minutes" in response.text
    assert 'href="/classroom/recovery"' in response.text
    assert 'role="alert"' in response.text
    assert "Estimate unavailable" in response.text
    assert receipt.status_code == 200
    assert "Automatic workers" in receipt.text
    assert "Other checked work" in receipt.text
    assert "manifest-1" not in receipt.text
    assert "example.org" not in receipt.text
    assert "raw GAM output" in receipt.text


def test_course_search_is_indexed_and_state_filtered(web_client):
    client, connector, _ = web_client
    response = client.get(
        "/classroom/courses", params={"q": "Section_010", "state": "ACTIVE"}
    )
    assert response.status_code == 200
    assert "Course 010" in response.text
    assert "Course 011" not in response.text
    assert not connector.calls


def test_course_detail_does_not_load_hidden_rosters(web_client):
    client, connector, _ = web_client
    response = client.get("/classroom/course/123")
    assert response.status_code == 200
    assert "English 1" in response.text
    assert "teacher@example.com" in response.text
    assert "d:Section_123" in response.text
    assert "Teachers" in response.text and "Students" in response.text
    assert (
        "get_course",
        "123",
        True,
        True,
        True,
    ) in connector.calls
    assert not any(call[0] == "list_course_participants" for call in connector.calls)

    roster = client.get("/classroom/course/123/roster", params={"role": "students"})
    assert roster.status_code == 200
    assert "student1@example.com" in roster.text
    assert any(call[0] == "list_course_participants" for call in connector.calls)


def test_teacher_roster_marks_owner_by_stable_user_id(web_client):
    client, _connector, _ = web_client

    roster = client.get("/classroom/course/123/roster", params={"role": "teachers"})

    assert roster.status_code == 200
    assert roster.text.count(">Owner<") == 1
    assert roster.text.count(">Remove<") == 1


def test_create_course_is_always_provisioned(web_client):
    client, connector, _ = web_client
    response = client.post(
        "/classroom/course",
        data={
            "name": "Geometry",
            "owner_email": "teacher@example.com",
            "alias": "Section_456",
        },
    )
    assert response.status_code == 200
    assert "Provisioned course created" in response.text
    call = next(call for call in connector.calls if call[0] == "create_course")
    assert call[1]["state"] == "PROVISIONED"


def test_metadata_edit_and_state_confirmation(web_client):
    client, connector, _ = web_client
    updated = client.post(
        "/classroom/course/123/metadata",
        data={
            "name": "English I",
            "section": "S2",
            "room": "301",
            "description_heading": "Start",
            "description": "Updated",
        },
    )
    assert updated.status_code == 200
    assert "Course details saved" in updated.text
    assert "English I" in updated.text

    forged = client.post(
        "/classroom/course/123/state",
        data={"target_state": "ARCHIVED", "confirmed": ""},
    )
    assert "Review the state change" in forged.text

    preview = client.post(
        "/classroom/course/123/state/preview",
        data={"target_state": "ARCHIVED"},
    )
    assert "Confirm state change" in preview.text
    applied = client.post(
        "/classroom/course/123/state",
        data={"target_state": "ARCHIVED", "confirmed": "yes"},
    )
    assert "Archived course" in applied.text
    assert connector.courses["123"].course_state == "ARCHIVED"


def test_owner_transfer_requires_two_exact_typed_values(web_client):
    client, connector, _ = web_client
    preview = client.post(
        "/classroom/course/123/owner/preview",
        data={"target_email": "newowner@example.com"},
    )
    assert "Confirm owner transfer" in preview.text
    assert "newowner@example.com" in preview.text

    wrong = client.post(
        "/classroom/course/123/owner",
        data={
            "target_email": "newowner@example.com",
            "confirm_course_id": "wrong",
            "confirm_target_email": "newowner@example.com",
        },
    )
    assert "exact course ID" in wrong.text
    assert connector.courses["123"].owner_email == "teacher@example.com"

    applied = client.post(
        "/classroom/course/123/owner",
        data={
            "target_email": "newowner@example.com",
            "confirm_course_id": "123",
            "confirm_target_email": "newowner@example.com",
        },
    )
    assert "ownership transferred" in applied.text
    assert connector.courses["123"].owner_email == "newowner@example.com"


def test_owner_teacher_removal_is_rejected_server_side(web_client):
    client, connector, _ = web_client
    response = client.post(
        "/classroom/course/123/roster/remove",
        data={"role": "teachers", "email": "teacher@example.com"},
    )
    assert "owner cannot be removed" in response.text
    assert not any(call[0] == "remove_course_participant" for call in connector.calls)


def test_exact_roster_preview_shows_add_remove_and_requires_course_id(web_client):
    client, _, _ = web_client
    response = client.post(
        "/classroom/course/123/roster/preview",
        data={
            "role": "students",
            "csv_text": "email\nstudent2@example.com\nstudent3@example.com\n",
        },
    )
    assert response.status_code == 200
    assert "Exact roster preview" in response.text
    assert "student3@example.com" in response.text
    assert "student1@example.com" in response.text
    assert "Type course ID 123" in response.text

    manifest_id = response.text.split('name="manifest_id" value="', 1)[1].split('"', 1)[0]
    rejected = client.post(
        "/classroom/roster/apply",
        data={"manifest_id": manifest_id, "confirm_course_id": "wrong"},
    )
    assert "exact course ID" in rejected.text


def test_user_classroom_partial_lists_teaching_and_enrolled(web_client):
    client, _, _ = web_client
    response = client.get(
        "/classroom/user", params={"email": "teacher@example.com"}
    )
    assert response.status_code == 200
    assert "Teaching" in response.text
    assert "English 1" in response.text
    assert "Enrolled" in response.text
