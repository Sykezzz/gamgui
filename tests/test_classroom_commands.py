from __future__ import annotations

import pytest

from gamgui.core.gam.commands import COURSE_INDEX_FIELDS, GAMCommands


def test_classroom_index_command_is_bounded_to_cheap_fields():
    argv = GAMCommands.print_courses()
    assert argv == [
        "print",
        "courses",
        "fields",
        ",".join(COURSE_INDEX_FIELDS),
        "formatjson",
    ]
    lowered = [token.lower() for token in argv]
    assert "owneremail" not in lowered
    assert "aliases" not in lowered
    assert "show" not in lowered


def test_classroom_filtered_course_commands():
    assert GAMCommands.print_courses(
        states=["ACTIVE", "ARCHIVED"], teacher="teacher@example.com"
    )[:6] == [
        "print",
        "courses",
        "teacher",
        "teacher@example.com",
        "states",
        "active,archived",
    ]
    assert GAMCommands.print_courses(student="student@example.com")[2:4] == [
        "student",
        "student@example.com",
    ]
    with pytest.raises(ValueError, match="mutually exclusive"):
        GAMCommands.print_courses(teacher="t@example.com", student="s@example.com")


def test_classroom_course_create_update_and_lifecycle_argv():
    assert GAMCommands.create_course(
        "English 1",
        "teacher@example.com",
        alias="Section_123",
        section="CR S1",
        room="204",
        description_heading="Welcome",
        description="Read first",
        state="PROVISIONED",
    ) == [
        "create",
        "course",
        "alias",
        "Section_123",
        "name",
        "English 1",
        "teacher",
        "teacher@example.com",
        "section",
        "CR S1",
        "room",
        "204",
        "descriptionheading",
        "Welcome",
        "description",
        "Read first",
        "state",
        "provisioned",
    ]
    assert GAMCommands.update_course(
        "123",
        name="English 1",
        section="S1",
        room="",
        description_heading="",
        description="Updated",
    ) == [
        "update",
        "course",
        "123",
        "name",
        "English 1",
        "section",
        "S1",
        "room",
        "",
        "descriptionheading",
        "",
        "description",
        "Updated",
    ]
    assert GAMCommands.update_course_state("123", "ARCHIVED") == [
        "update",
        "course",
        "123",
        "state",
        "archived",
    ]
    assert GAMCommands.transfer_course_owner("123", "new@example.com") == [
        "update",
        "course",
        "123",
        "teacher",
        "new@example.com",
    ]


def test_classroom_roster_argv_never_uses_clear_or_sync():
    assert GAMCommands.print_course_participants("123", "teachers") == [
        "print",
        "course-participants",
        "course",
        "123",
        "show",
        "teachers",
        "formatjson",
    ]
    assert GAMCommands.add_course_participant(
        "123", "students", "student@example.com"
    ) == ["course", "123", "add", "students", "student@example.com"]
    assert GAMCommands.remove_course_participant(
        "123", "teachers", "teacher@example.com"
    ) == ["course", "123", "remove", "teachers", "teacher@example.com"]
    for argv in (
        GAMCommands.add_course_participant("123", "students", "x@example.com"),
        GAMCommands.remove_course_participant("123", "students", "x@example.com"),
    ):
        assert "clear" not in argv and "sync" not in argv


def test_classroom_values_remain_single_argv_elements():
    hostile = "English; rm -rf / `whoami`"
    argv = GAMCommands.create_course(hostile, "teacher@example.com")
    assert argv[argv.index("name") + 1] == hostile
    assert len([token for token in argv if token == hostile]) == 1

    with pytest.raises(ValueError):
        GAMCommands.update_course_state("123", "deleted")
    with pytest.raises(ValueError):
        GAMCommands.add_course_participant("123", "guardians", "x@example.com")
