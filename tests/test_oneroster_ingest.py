from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from gamgui.components.oneroster import (
    AUTOMATIC_SESSION_SCOPE,
    OneRosterError,
    OneRosterService,
    SnapshotState,
    course_display_name,
    normalize_course_name_template,
    render_course_display_name,
    section_alias,
)
from gamgui.components.oneroster import ingest as ingest_module
from tests.test_oneroster_helpers import csv_text, valid_files, zip_bytes


def test_valid_full_snapshot_normalizes_previews_and_exports(tmp_path: Path) -> None:
    service = OneRosterService("Example.ORG", tmp_path / "classroom-oneroster")
    snapshot = service.upload(io.BytesIO(zip_bytes(valid_files())), "district.zip")

    assert snapshot.state is SnapshotState.READY
    assert snapshot.ready_for_apply
    assert snapshot.domain == "example.org"
    assert snapshot.selected_session_id == AUTOMATIC_SESSION_SCOPE
    assert snapshot.counts.ready_courses == 1
    assert snapshot.counts.students == 1

    courses = service.preview(snapshot.id, "courses")
    assert courses.total == 1
    assert courses.items[0]["alias"] == "Section_101"
    assert courses.items[0]["name"] == "Algebra I \u2013 P1 (2026-27)"
    assert courses.items[0]["owner_email"] == "teacher@example.org"
    assert courses.items[0]["ready"] is True

    output = io.BytesIO()
    assert service.write_export(snapshot.id, "courses", output) == 1
    assert output.getvalue().decode().splitlines() == [
        "alias,name,section,room,teacher,status",
        "Section_101,Algebra I \u2013 P1 (2026-27),P1,101,teacher@example.org,PROVISIONED",
    ]
    students = io.StringIO()
    assert service.write_export(snapshot.id, "students.csv", students) == 1
    assert "student@example.org" in students.getvalue()


def test_duplicate_critical_header_blocks_apply_before_rows_are_loaded(
    tmp_path: Path,
) -> None:
    files = valid_files()
    lines = files["enrollments.csv"].splitlines()
    files["enrollments.csv"] = "\n".join(
        [f"{lines[0]},role", *(f"{line},teacher" for line in lines[1:]), ""]
    )
    service = OneRosterService("example.org", tmp_path / "duplicate-header")

    snapshot = service.upload(zip_bytes(files))
    issues = service.preview(
        snapshot.id,
        "issues",
        query="OR-HEADER-DUPLICATE",
    )

    assert snapshot.state is SnapshotState.BLOCKED
    assert not snapshot.ready_for_apply
    assert snapshot.counts.enrollments == 0
    assert any(item["blocking"] for item in issues.items)


def test_blank_header_blocks_apply_before_rows_are_loaded(tmp_path: Path) -> None:
    files = valid_files()
    lines = files["users.csv"].splitlines()
    files["users.csv"] = "\n".join(
        [f"{lines[0]},", *(f"{line},ignored" for line in lines[1:]), ""]
    )
    service = OneRosterService("example.org", tmp_path / "blank-header")

    snapshot = service.upload(zip_bytes(files))

    assert snapshot.state is SnapshotState.BLOCKED
    assert not snapshot.ready_for_apply
    assert snapshot.counts.users == 0
    assert service.preview(
        snapshot.id,
        "issues",
        query="OR-HEADER-BLANK",
    ).total == 1


def test_duplicate_manifest_header_is_rejected_in_manifest_pass(
    tmp_path: Path,
) -> None:
    files = valid_files()
    lines = files["manifest.csv"].splitlines()
    files["manifest.csv"] = "\n".join(
        [f"{lines[0]},value", *(f"{line},bulk" for line in lines[1:]), ""]
    )
    service = OneRosterService("example.org", tmp_path / "manifest-duplicate")

    snapshot = service.upload(zip_bytes(files))

    assert snapshot.state is SnapshotState.BLOCKED
    assert not snapshot.ready_for_apply
    assert service.preview(
        snapshot.id,
        "issues",
        query="OR-HEADER-DUPLICATE",
    ).total == 1


def test_ownerless_and_ambiguous_primary_are_quarantined_not_guessed(
    tmp_path: Path,
) -> None:
    ownerless = OneRosterService("example.org", tmp_path / "ownerless")
    first = ownerless.upload(zip_bytes(valid_files(include_teacher=False)))
    page = ownerless.preview(first.id, "courses")
    assert first.ready_for_apply
    assert first.counts.ready_courses == 0
    assert first.counts.quarantined_courses == 1
    assert "OR-OWNER-MISSING" in page.items[0]["quarantine_codes"]
    assert page.items[0]["owner_email"] == ""

    ambiguous = OneRosterService("example.org", tmp_path / "ambiguous")
    second = ambiguous.upload(zip_bytes(valid_files(primary="false")))
    course = ambiguous.preview(second.id, "courses").items[0]
    assert "OR-OWNER-AMBIGUOUS" in course["quarantine_codes"]
    assert course["owner_email"] == ""


def test_missing_user_and_alias_collision_quarantine_affected_classes(
    tmp_path: Path,
) -> None:
    files = valid_files()
    files["enrollments.csv"] += (
        "missing-ref,active,101,school-1,missing-user,student,false,,\n"
    )
    # Section_101 normalizes to the same alias as 101.
    files["classes.csv"] += (
        "Section_101,active,Collision,P2,102,course-1,term-1,school-1,9\n"
    )
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(zip_bytes(files))
    courses = service.preview(snapshot.id, "courses", limit=50)
    by_id = {item["class_id"]: item for item in courses.items}

    assert "OR-ALIAS-COLLISION" in by_id["101"]["quarantine_codes"]
    assert "OR-USER-MISSING" in by_id["101"]["quarantine_codes"]
    assert "OR-ALIAS-COLLISION" in by_id["Section_101"]["quarantine_codes"]
    issues = service.preview(snapshot.id, "issues", query="OR-REFERENCE")
    assert any(item["code"] == "OR-REFERENCE-ENROLLMENT" for item in issues.items)


def test_unsupported_enrollment_role_quarantines_exact_class(tmp_path: Path) -> None:
    files = valid_files()
    files["enrollments.csv"] = files["enrollments.csv"].replace(
        ",student,false,,",
        ",learner,false,,",
    )
    service = OneRosterService("example.org", tmp_path / "component")

    snapshot = service.upload(zip_bytes(files))
    course = service.preview(snapshot.id, "courses").items[0]

    assert snapshot.ready_for_apply
    assert course["ready"] is False
    assert "OR-ENROLLMENT-ROLE" in course["quarantine_codes"]
    initial_issues = service.preview(
        snapshot.id, "issues", query="OR-ENROLLMENT-ROLE"
    ).items
    assert any(item["entity_kind"] == "enrollment" for item in initial_issues)

    service.select_session(snapshot.id, "year-1")
    rebuilt_issues = service.preview(
        snapshot.id, "issues", query="OR-ENROLLMENT-ROLE"
    ).items
    assert any(item["entity_kind"] == "enrollment" for item in rebuilt_issues)
    assert any(item["entity_kind"] == "class" for item in rebuilt_issues)


def test_enrollment_with_missing_class_blocks_exact_state_apply(
    tmp_path: Path,
) -> None:
    files = valid_files()
    files["enrollments.csv"] = files["enrollments.csv"].replace(
        ",101,school-1,student-1,",
        ",missing-class,school-1,student-1,",
    )
    service = OneRosterService("example.org", tmp_path / "component")

    snapshot = service.upload(zip_bytes(files))
    issues = service.preview(snapshot.id, "issues", query="OR-REFERENCE-ENROLLMENT")

    assert snapshot.state is SnapshotState.BLOCKED
    assert not snapshot.ready_for_apply
    assert any(item["blocking"] for item in issues.items)


@pytest.mark.parametrize(
    ("entity_file", "active_marker", "deleted_marker", "expected_code", "blocked"),
    (
        (
            "users.csv",
            "student-1,active,",
            "student-1,tobedeleted,",
            "OR-USER-MISSING",
            False,
        ),
        (
            "courses.csv",
            "course-1,active,",
            "course-1,tobedeleted,",
            "OR-REFERENCE-COURSE",
            False,
        ),
        (
            "academicSessions.csv",
            "term-1,active,",
            "term-1,tobedeleted,",
            "",
            False,
        ),
    ),
)
def test_deleted_reference_targets_never_produce_ready_courses(
    tmp_path: Path,
    entity_file: str,
    active_marker: str,
    deleted_marker: str,
    expected_code: str,
    blocked: bool,
) -> None:
    files = valid_files()
    files[entity_file] = files[entity_file].replace(
        active_marker,
        deleted_marker,
        1,
    )
    service = OneRosterService(
        "example.org",
        tmp_path / (expected_code or "inactive-session"),
    )

    snapshot = service.upload(zip_bytes(files))
    course = service.preview(snapshot.id, "courses").items[0]

    assert course["ready"] is False
    if expected_code:
        assert expected_code in course["quarantine_codes"]
    else:
        assert course["selected"] is False
        assert course["quarantine_codes"] == ()
    assert (snapshot.state is SnapshotState.BLOCKED) is blocked


def test_overlapping_terms_are_applied_automatically(tmp_path: Path) -> None:
    files = valid_files(term_ids="term-1,term-2")
    files["academicSessions.csv"] += (
        "term-2,active,Second Term,term,2000-01-01,2100-12-31,year-1,2026-27\n"
    )
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(zip_bytes(files))
    assert snapshot.state is SnapshotState.READY
    assert snapshot.selected_session_id == AUTOMATIC_SESSION_SCOPE
    assert snapshot.ready_for_apply
    assert snapshot.counts.ready_courses == 1


def test_session_dates_scope_each_class_and_treat_end_as_exclusive(
    tmp_path: Path,
) -> None:
    today = date.today()
    files = valid_files(term_ids="term-current")
    files["academicSessions.csv"] = csv_text(
        (
            "sourcedId",
            "status",
            "title",
            "type",
            "startDate",
            "endDate",
            "parentSourcedId",
            "schoolYear",
        ),
        (
            {
                "sourcedId": "year-1",
                "status": "active",
                "title": "School Year",
                "type": "schoolYear",
                "startDate": (today - timedelta(days=30)).isoformat(),
                "endDate": (today + timedelta(days=30)).isoformat(),
                "parentSourcedId": "",
                "schoolYear": "2026-27",
            },
            {
                "sourcedId": "term-current",
                "status": "active",
                "title": "Current Term",
                "type": "term",
                "startDate": (today - timedelta(days=1)).isoformat(),
                "endDate": (today + timedelta(days=1)).isoformat(),
                "parentSourcedId": "year-1",
                "schoolYear": "2026-27",
            },
            {
                "sourcedId": "term-ended",
                "status": "active",
                "title": "Ended Term",
                "type": "term",
                "startDate": (today - timedelta(days=10)).isoformat(),
                "endDate": today.isoformat(),
                "parentSourcedId": "year-1",
                "schoolYear": "2026-27",
            },
        ),
    )
    files["classes.csv"] += (
        "ended-class,active,Ended Class,P2,,course-1,term-ended,school-1,9\n"
    )
    service = OneRosterService("example.org", tmp_path / "dated-sessions")

    snapshot = service.upload(zip_bytes(files))
    courses = {
        item["class_id"]: item
        for item in service.preview(snapshot.id, "courses").items
    }

    assert snapshot.ready_for_apply
    assert courses["101"]["selected"] is True
    assert courses["101"]["ready"] is True
    assert courses["ended-class"]["selected"] is False
    assert courses["ended-class"]["quarantine_codes"] == ()
    assert snapshot.counts.ready_courses == 1
    assert snapshot.counts.quarantined_courses == 0
    assert snapshot.counts.deferred_courses == 1


def test_future_sessions_are_ready_now_for_teacher_setup(
    tmp_path: Path,
) -> None:
    today = date.today()
    files = valid_files()
    files["academicSessions.csv"] = files["academicSessions.csv"].replace(
        "term-1,active,Current Term,term,2000-01-01,2100-12-31",
        (
            "term-1,active,Future Term,term,"
            f"{(today + timedelta(days=7)).isoformat()},"
            f"{(today + timedelta(days=180)).isoformat()}"
        ),
    )
    service = OneRosterService("example.org", tmp_path / "future-summer-import")

    snapshot = service.upload(zip_bytes(files))
    course = service.preview(snapshot.id, "courses").items[0]

    assert snapshot.state is SnapshotState.READY
    assert snapshot.ready_for_apply
    assert snapshot.blocking_issue_count == 0
    assert snapshot.counts.ready_courses == 1
    assert snapshot.counts.future_ready_courses == 1
    assert snapshot.counts.quarantined_courses == 0
    assert snapshot.counts.deferred_courses == 0
    assert course["selected"] is True
    assert course["ready"] is True
    assert course["scope_state"] == "future-ready"
    assert course["quarantine_codes"] == ()


def test_future_teacher_enrollment_is_immediate_but_future_student_waits(
    tmp_path: Path,
) -> None:
    today = date.today()
    start = today + timedelta(days=9)
    files = valid_files()
    files["academicSessions.csv"] = files["academicSessions.csv"].replace(
        "term-1,active,Current Term,term,2000-01-01,2100-12-31",
        (
            "term-1,active,Future Term,term,"
            f"{start.isoformat()},{(start + timedelta(days=120)).isoformat()}"
        ),
    )
    files["enrollments.csv"] = files["enrollments.csv"].replace(
        "teacher,true,,",
        f"teacher,true,{start.isoformat()},",
    ).replace(
        "student,false,,",
        f"student,false,{start.isoformat()},",
    )
    service = OneRosterService("example.org", tmp_path / "future-enrollments")

    snapshot = service.upload(zip_bytes(files))
    teachers = io.StringIO()
    students = io.StringIO()

    assert snapshot.counts.teachers == 1
    assert snapshot.counts.students == 0
    assert service.write_export(snapshot.id, "teachers.csv", teachers) == 1
    assert "teacher@example.org" in teachers.getvalue()
    assert service.write_export(snapshot.id, "students.csv", students) == 0

    effective = service.store.refresh_schedule_scope(snapshot.id, today=start)
    students = io.StringIO()
    assert effective.counts.students == 1
    assert service.write_export(snapshot.id, "students.csv", students) == 1
    assert "student@example.org" in students.getvalue()


def test_selects_nearest_upcoming_school_year_during_summer(
    tmp_path: Path,
) -> None:
    today = date.today()
    year_start = today + timedelta(days=20)
    files = valid_files()
    files["academicSessions.csv"] = csv_text(
        (
            "sourcedId",
            "status",
            "title",
            "type",
            "startDate",
            "endDate",
            "parentSourcedId",
            "schoolYear",
        ),
        (
            {
                "sourcedId": "year-1",
                "status": "active",
                "title": "2026-27 School Year",
                "type": "schoolYear",
                "startDate": year_start.isoformat(),
                "endDate": (year_start + timedelta(days=300)).isoformat(),
                "parentSourcedId": "",
                "schoolYear": "2026-27",
            },
            {
                "sourcedId": "term-1",
                "status": "active",
                "title": "Fall Term",
                "type": "term",
                "startDate": (year_start + timedelta(days=10)).isoformat(),
                "endDate": (year_start + timedelta(days=130)).isoformat(),
                "parentSourcedId": "year-1",
                "schoolYear": "2026-27",
            },
        ),
    )
    service = OneRosterService("example.org", tmp_path / "summer-year")

    snapshot = service.upload(zip_bytes(files))
    course = service.preview(snapshot.id, "courses").items[0]

    assert snapshot.ready_for_apply
    assert snapshot.school_year_id == "year-1"
    assert snapshot.school_year_title == "2026-27 School Year"
    assert course["scope_state"] == "future-ready"
    assert course["ready"] is True


def test_inactive_future_sessions_do_not_block_automatic_scope(
    tmp_path: Path,
) -> None:
    today = date.today()
    year_start = today + timedelta(days=20)
    files = valid_files()
    files["academicSessions.csv"] = csv_text(
        (
            "sourcedId",
            "status",
            "title",
            "type",
            "startDate",
            "endDate",
            "parentSourcedId",
            "schoolYear",
        ),
        (
            {
                "sourcedId": "year-1",
                "status": "inactive",
                "title": "2026-27 School Year",
                "type": "schoolYear",
                "startDate": year_start.isoformat(),
                "endDate": (year_start + timedelta(days=300)).isoformat(),
                "parentSourcedId": "",
                "schoolYear": "2026-27",
            },
            {
                "sourcedId": "term-1",
                "status": "inactive",
                "title": "Fall Term",
                "type": "term",
                "startDate": (year_start + timedelta(days=10)).isoformat(),
                "endDate": (year_start + timedelta(days=130)).isoformat(),
                "parentSourcedId": "year-1",
                "schoolYear": "2026-27",
            },
        ),
    )
    service = OneRosterService("example.org", tmp_path / "inactive-future-year")

    snapshot = service.upload(zip_bytes(files))
    course = service.preview(snapshot.id, "courses").items[0]

    assert snapshot.state is SnapshotState.READY
    assert snapshot.blocking_issue_count == 0
    assert snapshot.school_year_id == "year-1"
    assert service.preview(snapshot.id, "issues", query="OR-STATUS-INVALID").total == 0
    assert course["scope_state"] == "future-ready"
    assert course["ready"] is True


def test_later_semester_is_ready_but_next_school_year_is_out_of_scope(
    tmp_path: Path,
) -> None:
    today = date.today()
    later_start = today + timedelta(days=90)
    files = valid_files()
    files["academicSessions.csv"] += (
        "term-later,active,Later Semester,term,"
        f"{later_start.isoformat()},{(later_start + timedelta(days=90)).isoformat()},"
        "year-1,2026-27\n"
        "year-2,active,Next School Year,schoolYear,2101-01-01,2200-12-31,,2027-28\n"
        "term-next,active,Next Fall,term,2101-08-01,2101-12-20,year-2,2027-28\n"
    )
    files["courses.csv"] += (
        "course-2,active,Geometry,year-1,school-1,9\n"
        "course-3,active,Physics,year-2,school-1,10\n"
    )
    files["classes.csv"] += (
        "202,active,Geometry Section,P2,202,course-2,term-later,school-1,9\n"
        "303,active,Physics Section,P3,303,course-3,term-next,school-1,10\n"
    )
    files["enrollments.csv"] += (
        "enrollment-teacher-202,active,202,school-1,teacher-1,teacher,true,,\n"
        "enrollment-teacher-303,active,303,school-1,teacher-1,teacher,true,,\n"
    )
    service = OneRosterService("example.org", tmp_path / "multi-year-scope")

    snapshot = service.upload(zip_bytes(files))
    courses = {
        item["class_id"]: item
        for item in service.preview(snapshot.id, "courses", limit=50).items
    }

    assert snapshot.ready_for_apply
    assert courses["202"]["scope_state"] == "future-ready"
    assert courses["202"]["ready"] is True
    assert courses["303"]["scope_state"] == "other-year"
    assert courses["303"]["selected"] is False
    assert courses["303"]["ready"] is False


def test_overlapping_current_school_years_hold_the_import(tmp_path: Path) -> None:
    files = valid_files()
    files["academicSessions.csv"] += (
        "year-2,active,Conflicting Year,schoolYear,2001-01-01,2099-12-31,,2027-28\n"
    )
    service = OneRosterService("example.org", tmp_path / "overlapping-years")

    snapshot = service.upload(zip_bytes(files))

    assert snapshot.state is SnapshotState.BLOCKED
    assert not snapshot.ready_for_apply
    assert service.preview(
        snapshot.id,
        "issues",
        query="OR-SCHOOL-YEAR-AMBIGUOUS",
    ).total == 1


def test_invalid_school_year_dates_hold_automatic_selection(tmp_path: Path) -> None:
    files = valid_files()
    files["academicSessions.csv"] = files["academicSessions.csv"].replace(
        "year-1,active,School Year,schoolYear,2000-01-01,2100-12-31",
        "year-1,active,School Year,schoolYear,not-a-date,2100-12-31",
    )
    service = OneRosterService("example.org", tmp_path / "invalid-year-dates")

    snapshot = service.upload(zip_bytes(files))

    assert snapshot.state is SnapshotState.BLOCKED
    assert not snapshot.school_year_id
    assert service.preview(
        snapshot.id,
        "issues",
        query="OR-SESSION-DATE",
    ).total >= 1


def test_term_with_missing_school_year_parent_holds_the_import(tmp_path: Path) -> None:
    files = valid_files()
    files["academicSessions.csv"] = files["academicSessions.csv"].replace(
        "term,2000-01-01,2100-12-31,year-1,2026-27",
        "term,2000-01-01,2100-12-31,missing-year,2026-27",
    )
    service = OneRosterService("example.org", tmp_path / "missing-year-parent")

    snapshot = service.upload(zip_bytes(files))
    course = service.preview(snapshot.id, "courses").items[0]

    assert snapshot.state is SnapshotState.BLOCKED
    assert course["scope_state"] == "uncertain"
    assert "OR-SCHOOL-YEAR-HIERARCHY" in course["quarantine_codes"]


def test_enrollment_dates_filter_current_roster_and_use_exclusive_end(
    tmp_path: Path,
) -> None:
    today = date.today()
    files = valid_files()
    files["enrollments.csv"] = files["enrollments.csv"].replace(
        ",student,false,,",
        (
            f",student,false,{(today - timedelta(days=10)).isoformat()},"
            f"{today.isoformat()}"
        ),
    )
    service = OneRosterService("example.org", tmp_path / "dated-enrollments")

    snapshot = service.upload(zip_bytes(files))
    students = io.StringIO()

    assert snapshot.ready_for_apply
    assert snapshot.counts.teachers == 1
    assert snapshot.counts.students == 0
    assert service.write_export(snapshot.id, "students.csv", students) == 0
    enrollment = service.preview(
        snapshot.id,
        "enrollments",
        query="enrollment-student-1",
    ).items[0]
    assert enrollment["in_scope"] is False


def test_ended_enrollment_does_not_block_on_stale_class_reference(
    tmp_path: Path,
) -> None:
    today = date.today()
    files = valid_files()
    files["enrollments.csv"] = files["enrollments.csv"].replace(
        "enrollment-student-1,active,101,",
        "enrollment-student-1,active,missing-old-class,",
    ).replace(
        ",student,false,,",
        (
            f",student,false,{(today - timedelta(days=10)).isoformat()},"
            f"{today.isoformat()}"
        ),
    )
    service = OneRosterService("example.org", tmp_path / "stale-ended-enrollment")

    snapshot = service.upload(zip_bytes(files))

    assert snapshot.ready_for_apply
    assert snapshot.blocking_issue_count == 0
    assert service.preview(
        snapshot.id,
        "issues",
        query="OR-REFERENCE-ENROLLMENT",
    ).total == 0


def test_parent_session_does_not_keep_an_ended_child_term_active(
    tmp_path: Path,
) -> None:
    today = date.today()
    files = valid_files(term_ids="year-1,term-1")
    files["academicSessions.csv"] = files["academicSessions.csv"].replace(
        "2000-01-01,2100-12-31,year-1,2026-27",
        (
            f"{(today - timedelta(days=10)).isoformat()},"
            f"{today.isoformat()},year-1,2026-27"
        ),
        1,
    )
    service = OneRosterService("example.org", tmp_path / "specific-session")

    snapshot = service.upload(zip_bytes(files))
    course = service.preview(snapshot.id, "courses").items[0]

    assert snapshot.ready_for_apply
    assert course["selected"] is False
    assert course["quarantine_codes"] == ()


def test_session_reselection_does_not_amplify_existing_inactive_term_issues(
    tmp_path: Path,
) -> None:
    files = valid_files()
    files["academicSessions.csv"] = files["academicSessions.csv"].replace(
        "term-1,active,",
        "term-1,tobedeleted,",
        1,
    )
    historical_classes = [
        f"historical-{number},active,Historical {number},P{number},,course-1,term-1,school-1,9"
        for number in range(10_000)
    ]
    files["classes.csv"] += "\n".join(historical_classes) + "\n"
    service = OneRosterService("example.org", tmp_path / "inactive-terms")

    snapshot = service.upload(zip_bytes(files))
    initial_codes = {
        item["code"] for item in service.preview(snapshot.id, "issues", limit=50).items
    }
    assert "OR-ISSUE-LIMIT" not in initial_codes
    assert snapshot.issue_count == 0

    rebuilt = service.select_session(snapshot.id, "year-1")
    rebuilt_codes = {
        item["code"] for item in service.preview(snapshot.id, "issues", limit=50).items
    }
    assert "OR-ISSUE-LIMIT" not in rebuilt_codes
    assert rebuilt.issue_count == 0


def test_manual_session_compatibility_call_does_not_filter_automatic_scope(
    tmp_path: Path,
) -> None:
    files = valid_files(term_ids="term-1,term-2")
    files["academicSessions.csv"] += (
        "term-2,active,Second Term,term,2000-01-01,2100-12-31,year-1,2026-27\n"
    )
    files["classes.csv"] += (
        "term-2-ownerless,active,Historical,PT2,,course-1,term-2,school-1,9\n"
    )
    service = OneRosterService("example.org", tmp_path / "session-derived-issues")

    snapshot = service.upload(zip_bytes(files))
    initial_codes = {
        item["code"] for item in service.preview(snapshot.id, "issues", limit=50).items
    }
    assert "OR-TERM-AMBIGUOUS" not in initial_codes
    assert "OR-OWNER-MISSING" in initial_codes

    first = service.select_session(snapshot.id, "term-1")
    first_codes = {
        item["code"] for item in service.preview(first.id, "issues", limit=50).items
    }
    assert first.selected_session_id == AUTOMATIC_SESSION_SCOPE
    assert "OR-OWNER-MISSING" in first_codes

    second = service.select_session(snapshot.id, "term-2")
    second_codes = {
        item["code"] for item in service.preview(second.id, "issues", limit=50).items
    }
    assert "OR-OWNER-MISSING" in second_codes

    assert second.selected_session_id == AUTOMATIC_SESSION_SCOPE


def test_bounded_cursor_search_and_domain_isolation(tmp_path: Path) -> None:
    root = tmp_path / "component"
    first = OneRosterService("first.example", root)
    snapshot = first.upload(zip_bytes(valid_files(extra_users=70)))
    page1 = first.preview(snapshot.id, "users", limit=1000)
    assert len(page1.items) == 50
    assert page1.limit == 50
    assert page1.next_cursor
    page2 = first.preview(snapshot.id, "users", cursor=page1.next_cursor)
    assert len(page2.items) == 22
    assert {
        item["sourced_id"] for item in (*page1.items, *page2.items)
    } == {
        "teacher-1",
        "student-1",
        *(f"student-{number}" for number in range(2, 72)),
    }
    assert first.preview(snapshot.id, "users", query="student-70").total == 1
    cursor_value = page1.next_cursor or ""
    decoded = json.loads(
        base64.urlsafe_b64decode(
            (cursor_value + "=" * (-len(cursor_value) % 4)).encode()
        )
    )
    assert decoded["v"] == 2
    assert decoded["last_rowid"] > 0
    assert "offset" not in decoded
    reset = first.preview(snapshot.id, "users", cursor="not-a-valid-cursor")
    assert reset.items == page1.items

    other = OneRosterService("other.example", root)
    with pytest.raises(KeyError):
        other.get_import(snapshot.id)


def test_preview_search_uses_fts_keyset_and_caps_filtered_totals(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from gamgui.components.oneroster import preview_index, store as store_module

    service = OneRosterService("example.org", tmp_path / "indexed-preview")
    snapshot = service.upload(zip_bytes(valid_files(extra_users=6)))
    statements: list[str] = []
    original = store_module._snapshot_conn

    def traced(path):
        conn = original(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store_module, "_snapshot_conn", traced)
    monkeypatch.setattr(preview_index, "FILTERED_TOTAL_CAP", 2)

    page = service.preview(snapshot.id, "users", query="example", limit=2)

    assert page.total == 2
    assert not page.total_exact
    assert page.next_cursor
    normalized = [statement.upper() for statement in statements]
    assert any("PREVIEW_SEARCH MATCH" in statement for statement in normalized)
    assert not any(" LIKE " in statement for statement in normalized)
    assert not any(" OFFSET " in statement for statement in normalized)
    assert service.preview(snapshot.id, "users", query='") OR * NOT (').total == 0


def test_legacy_snapshot_preview_index_is_prepared_with_writable_migration(
    tmp_path: Path,
) -> None:
    service = OneRosterService("example.org", tmp_path / "legacy-preview")
    snapshot = service.upload(zip_bytes(valid_files()))
    path = service.store.normalized_path(snapshot.id)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE preview_search")
        conn.execute("DROP TABLE preview_totals")
        conn.execute("DROP TABLE preview_index_meta")

    page = service.preview(snapshot.id, "courses")

    assert page.total == 1
    assert page.items[0]["alias"] == "Section_101"


def test_legacy_selected_session_snapshot_migrates_to_automatic_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy-session-scope"
    service = OneRosterService("example.org", root)
    snapshot = service.upload(zip_bytes(valid_files()))
    normalized = service.store.normalized_path(snapshot.id)
    with sqlite3.connect(normalized) as conn:
        conn.execute("ALTER TABLE enrollments DROP COLUMN in_scope")
        conn.execute("UPDATE preview_index_meta SET version = 1")
    with sqlite3.connect(service.store.state_path) as conn:
        conn.execute(
            """
            UPDATE imports SET selected_session_id = ?
            WHERE domain = ? AND id = ?
            """,
            ("term-1", "example.org", snapshot.id),
        )

    reopened = OneRosterService("example.org", root)
    upgraded = reopened.get_import(snapshot.id)

    assert upgraded.selected_session_id == AUTOMATIC_SESSION_SCOPE
    assert upgraded.ready_for_apply
    with sqlite3.connect(normalized) as conn:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(enrollments)").fetchall()
        }
        preview_version = int(
            conn.execute("SELECT version FROM preview_index_meta").fetchone()[0]
        )
    assert "in_scope" in columns
    assert preview_version == 3


def test_legacy_snapshot_rebuilds_persisted_course_scope_before_preview(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy-course-scope"
    service = OneRosterService("example.org", root)
    snapshot = service.upload(zip_bytes(valid_files()))
    normalized = service.store.normalized_path(snapshot.id)
    with sqlite3.connect(normalized) as conn:
        conn.execute("DROP TABLE schedule_scope")
        for column in (
            "scope_state",
            "scope_date",
            "school_year_id",
            "term_start",
            "term_end",
        ):
            conn.execute(f"ALTER TABLE course_plans DROP COLUMN {column}")
        conn.execute("UPDATE preview_index_meta SET version = 1")
    with sqlite3.connect(service.store.state_path) as conn:
        conn.execute(
            """
            UPDATE imports SET scope_date = '', school_year_id = '',
                school_year_title = ''
            WHERE domain = ? AND id = ?
            """,
            ("example.org", snapshot.id),
        )

    reopened = OneRosterService("example.org", root)
    course = reopened.preview(snapshot.id, "courses").items[0]
    upgraded = reopened.get_import(snapshot.id)

    assert upgraded.scope_date == date.today().isoformat()
    assert upgraded.school_year_id == "year-1"
    assert course["scope_state"] == "current"
    assert course["school_year_id"] == "year-1"


def test_retention_removes_material_but_keeps_history(tmp_path: Path) -> None:
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(zip_bytes(valid_files()))
    assert service.store.raw_path(snapshot.id).exists()
    assert service.cleanup_expired(now=snapshot.expires_at + 1) == 1
    expired = service.get_import(snapshot.id)
    assert expired.state is SnapshotState.EXPIRED
    assert not service.store.snapshot_dir(snapshot.id).exists()
    with pytest.raises(OneRosterError) as error:
        service.preview(snapshot.id, "courses")
    assert error.value.code == "OR-IMPORT-EXPIRED"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are meaningful on macOS/Linux")
def test_component_material_is_owner_only(tmp_path: Path) -> None:
    root = tmp_path / "classroom-oneroster"
    service = OneRosterService("example.org", root)
    snapshot = service.upload(zip_bytes(valid_files()))
    assert root.stat().st_mode & 0o777 == 0o700
    assert service.store.snapshots_root.stat().st_mode & 0o777 == 0o700
    assert service.store.snapshot_dir(snapshot.id).stat().st_mode & 0o777 == 0o700
    assert service.store.state_path.stat().st_mode & 0o777 == 0o600
    assert service.store.raw_path(snapshot.id).stat().st_mode & 0o777 == 0o600
    assert service.store.normalized_path(snapshot.id).stat().st_mode & 0o777 == 0o600


def test_ingest_permission_failure_stops_before_upload_pii_is_written(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "snapshot" / "raw.zip"

    def fail_chmod(_path, _mode):
        raise PermissionError("simulated owner-only failure")

    monkeypatch.setattr(ingest_module.os, "chmod", fail_chmod)

    with pytest.raises(PermissionError, match="owner-only"):
        ingest_module.copy_upload(
            b"private roster bytes",
            target,
            limits=ingest_module.SafetyLimits(),
        )
    assert not target.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are meaningful")
def test_normalized_database_is_owner_only_before_sqlite_opens(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "snapshot" / "normalized.db"
    observed_modes = []

    def stop_at_connect(value, **_kwargs):
        candidate = Path(value)
        observed_modes.append(candidate.stat().st_mode & 0o777)
        raise RuntimeError("stop after private pre-create")

    monkeypatch.setattr(ingest_module.sqlite3, "connect", stop_at_connect)

    with pytest.raises(RuntimeError, match="private pre-create"):
        ingest_module._create_normalized_db(path)
    assert observed_modes == [0o600]
    assert path.stat().st_mode & 0o777 == 0o600


def test_naming_and_alias_are_deterministic() -> None:
    assert section_alias("42") == "Section_42"
    assert section_alias("Section_42") == "Section_42"
    assert section_alias("section_42") == "Section_42"
    assert course_display_name("Math", "", "Section A", "2026") == (
        "Section A"
    )
    assert course_display_name("Math", "P1", "Section A", "2026") == (
        "Math \u2013 P1 (2026)"
    )
    assert render_course_display_name(
        "{course_title}[[ \u2013 {class_code}]][[ ({school_year})]]",
        "Math",
        "",
        "Section A",
        "2026",
    ) == "Math (2026)"


@pytest.mark.parametrize(
    "template",
    (
        "{unknown}",
        "{course_title.__class__}",
        "{course_title!r}",
        "{course_title:>20}",
        "[[{course_title}]",
        "[[literal]] {course_title}",
        "literal only",
        "{course_title}\n{class_title}",
    ),
)
def test_course_naming_template_rejects_unsafe_or_ambiguous_syntax(
    template: str,
) -> None:
    with pytest.raises(ValueError):
        normalize_course_name_template(template)


def test_course_naming_template_rebuilds_and_persists_per_import(
    tmp_path: Path,
) -> None:
    root = tmp_path / "custom-course-names"
    service = OneRosterService("example.org", root)
    snapshot = service.upload(zip_bytes(valid_files()))

    configured = service.configure_course_naming(
        snapshot.id,
        "{class_title}[[ ({school_year})]]",
    )
    course = service.preview(snapshot.id, "courses").items[0]

    assert configured.ready_for_apply
    assert configured.course_name_template == (
        "{class_title}[[ ({school_year})]]"
    )
    assert course["name"] == "Algebra Section (2026-27)"

    reopened = OneRosterService("example.org", root)
    persisted = reopened.get_import(snapshot.id)
    assert persisted.course_name_template == configured.course_name_template
    assert reopened.preview(snapshot.id, "courses").items[0]["name"] == (
        "Algebra Section (2026-27)"
    )


def test_rendered_course_name_over_google_limit_is_quarantined(
    tmp_path: Path,
) -> None:
    files = valid_files()
    files["courses.csv"] = files["courses.csv"].replace(
        "Algebra I",
        "A" * 751,
    )
    service = OneRosterService("example.org", tmp_path / "long-course-name")

    snapshot = service.upload(zip_bytes(files))
    courses = service.preview(snapshot.id, "courses").items
    issues = service.preview(snapshot.id, "issues").items

    assert snapshot.counts.quarantined_courses == 1
    assert courses[0]["ready"] is False
    assert "OR-COURSE-NAME-LENGTH" in courses[0]["quarantine_codes"]
    assert any(issue["code"] == "OR-COURSE-NAME-LENGTH" for issue in issues)


def test_blank_class_code_uses_class_title_only(tmp_path: Path) -> None:
    files = valid_files()
    files["classes.csv"] = files["classes.csv"].replace(
        "Algebra Section,P1,101",
        "Algebra Section,,101",
    )
    service = OneRosterService("example.org", tmp_path / "blank-class-code")

    snapshot = service.upload(zip_bytes(files))
    course = service.preview(snapshot.id, "courses").items[0]

    assert snapshot.ready_for_apply
    assert course["name"] == "Algebra Section"


def test_classroom_bound_control_character_quarantines_before_planning(
    tmp_path: Path,
) -> None:
    files = valid_files()
    files["courses.csv"] = files["courses.csv"].replace(
        "Algebra I",
        '"Algebra\nInjected"',
    )
    service = OneRosterService("example.org", tmp_path / "control-value")

    snapshot = service.upload(zip_bytes(files))
    course = service.preview(snapshot.id, "courses").items[0]
    issue_codes = {
        issue["code"] for issue in service.preview(snapshot.id, "issues").items
    }

    assert not snapshot.ready_for_apply
    assert not course["ready"]
    assert "OR-GAM-VALUE-INVALID" in course["quarantine_codes"]
    assert "OR-GAM-VALUE-INVALID" in issue_codes


@pytest.mark.parametrize(
    ("replacement", "expected_code"),
    [
        (
            "manifest.version,1.0\n",
            "OR-MANIFEST-FORMAT-VERSION",
        ),
        (
            "manifest.version,2.0\n",
            "OR-MANIFEST-FORMAT-VERSION",
        ),
        (
            "manifest.version,1.0\nmanifest.version,1.0\n",
            "OR-MANIFEST-DUPLICATE",
        ),
    ],
)
def test_manifest_version_is_required_exact_and_unique(
    tmp_path: Path,
    replacement: str,
    expected_code: str,
) -> None:
    files = valid_files()
    if replacement == "manifest.version,1.0\n":
        files["manifest.csv"] = files["manifest.csv"].replace(
            replacement,
            "",
            1,
        )
    else:
        files["manifest.csv"] = files["manifest.csv"].replace(
            "manifest.version,1.0\n",
            replacement,
            1,
        )

    service = OneRosterService("example.org", tmp_path / expected_code)
    snapshot = service.upload(zip_bytes(files))

    assert snapshot.state is SnapshotState.BLOCKED
    assert expected_code in {
        issue["code"] for issue in service.preview(snapshot.id, "issues").items
    }
