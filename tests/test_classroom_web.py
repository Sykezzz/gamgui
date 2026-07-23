from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gamgui.core.classroom.index import CourseIndex
from gamgui.core.classroom.manifests import RosterManifestStore
from gamgui.core.classroom.models import CourseSummary
from gamgui.web.routes.classroom import router
from tests.classroom_fakes import FakeClassroomConnector


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
    assert response.text.count(">Manage") == 50
    assert "Next 50 courses" in response.text
    assert not any(call[0] == "list_courses" for call in connector.calls)
    assert len(response.content) < 100_000


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
