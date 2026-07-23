from __future__ import annotations

import pytest

from gamgui.core.classroom.models import (
    CourseDetail,
    CourseParticipant,
    CourseSummary,
    RosterDiff,
    parse_desired_roster,
)


def test_course_models_tolerate_gam_key_variants():
    summary = CourseSummary.from_json(
        {
            "courseId": 123,
            "courseName": "English 1",
            "Section": "CR S1",
            "Room": "204",
            "ownerID": "999",
            "state": "active",
            "Creation Time": "2026-01-01T00:00:00Z",
        }
    )
    assert summary.id == "123"
    assert summary.name == "English 1"
    assert summary.section == "CR S1"
    assert summary.room == "204"
    assert summary.owner_id == "999"
    assert summary.course_state == "ACTIVE"

    detail = CourseDetail.from_json(
        {
            "course": {
                "id": "123",
                "name": "English 1",
                "courseState": "ARCHIVED",
                "ownerId": "999",
                "descriptionHeading": "Welcome",
                "description": "Read first",
            },
            "ownerEmail": "Teacher@Example.com",
            "aliases": ["d:Section_123", "p:legacy"],
        }
    )
    assert detail.owner_email == "teacher@example.com"
    assert detail.aliases == ("d:Section_123", "p:legacy")
    assert detail.description_heading == "Welcome"
    assert detail.roster_editable is False


def test_participant_reads_nested_profile_and_role_hint():
    member = CourseParticipant.from_json(
        {
            "courseId": "123",
            "userId": "u1",
            "profile": {
                "emailAddress": "Student@Example.com",
                "name": {"fullName": "Student One"},
                "photoUrl": "https://example.invalid/photo",
            },
        },
        role="students",
    )
    assert member.email == "student@example.com"
    assert member.full_name == "Student One"
    assert member.role == "students"
    assert member.label == "Student One"


def test_parse_desired_roster_accepts_csv_and_plain_lines():
    assert parse_desired_roster(
        "email,localId\nAlice@Example.com,1\nbob@example.com,2\nalice@example.com,3\n"
    ) == ["alice@example.com", "bob@example.com"]
    assert parse_desired_roster(
        "teacher@example.com\nassistant@example.com\n"
    ) == ["teacher@example.com", "assistant@example.com"]
    assert parse_desired_roster("email\n") == []


def test_parse_desired_roster_rejects_ambiguous_or_invalid_csv():
    with pytest.raises(ValueError, match="email column"):
        parse_desired_roster("name,localId\nAlice,1\n")
    with pytest.raises(ValueError, match="Invalid email"):
        parse_desired_roster("email\nnot-an-email\n")


def test_roster_diff_is_deterministic_and_exact():
    diff = RosterDiff.compute(
        "students",
        ["b@example.com", "c@example.com", "b@example.com"],
        ["a@example.com", "b@example.com"],
    )
    assert diff.adds == ("c@example.com",)
    assert diff.removes == ("a@example.com",)
    assert diff.unchanged == ("b@example.com",)
    assert diff.change_count == 2
    assert len(diff.desired_hash) == 64
    assert len(diff.basis_hash) == 64
