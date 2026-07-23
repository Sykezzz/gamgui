from __future__ import annotations

import os
import stat

import pytest

from gamgui.core.classroom.index import CourseIndex
from gamgui.core.classroom.models import CourseSummary


def _course(number: int, *, state: str = "ACTIVE", name: str = "") -> CourseSummary:
    return CourseSummary(
        id=str(number),
        name=name or f"Course {number:03d}",
        section=f"Section_{number:03d}",
        room=f"R{number % 10}",
        owner_id=f"owner-{number % 4}",
        course_state=state,
        update_time=f"2026-01-{(number % 28) + 1:02d}T00:00:00Z",
    )


def test_course_index_is_domain_scoped_and_persistent(tmp_path):
    path = tmp_path / "courses.db"
    index = CourseIndex(path)
    index.replace_all("one.example", [_course(1, name="Domain One")])
    index.replace_all("two.example", [_course(2, name="Domain Two")])

    assert [item.name for item in index.search("one.example").items] == ["Domain One"]
    assert [item.name for item in index.search("two.example").items] == ["Domain Two"]
    assert CourseIndex(path).search("one.example", "Domain").total == 1


def test_course_index_caps_pages_at_50_and_uses_bound_cursor(tmp_path):
    index = CourseIndex(tmp_path / "courses.db")
    index.replace_all("example.com", [_course(i) for i in range(125)])

    first = index.search("example.com", limit=500)
    assert len(first.items) == 50
    assert first.total == 125
    assert first.next_cursor

    second = index.search("example.com", cursor=first.next_cursor)
    assert len(second.items) == 50
    assert {item.id for item in first.items}.isdisjoint(item.id for item in second.items)

    # A cursor from another query cannot skip records in this query.
    expected = index.search("example.com", query="Course 1")
    rebound = index.search("example.com", query="Course 1", cursor=first.next_cursor)
    assert [item.id for item in rebound.items] == [item.id for item in expected.items]


def test_course_index_searches_summary_fields_and_state(tmp_path):
    index = CourseIndex(tmp_path / "courses.db")
    index.replace_all(
        "example.com",
        [
            _course(1, name="English Language Arts"),
            _course(2, name="Geometry", state="ARCHIVED"),
        ],
    )
    assert index.search("example.com", "English").items[0].id == "1"
    assert index.search("example.com", "Section_002").items[0].id == "2"
    assert index.search("example.com", "owner-2").items[0].id == "2"
    assert index.search("example.com", state="ARCHIVED").total == 1


def test_course_index_replace_is_atomic_and_upsert_patches_one_row(tmp_path):
    index = CourseIndex(tmp_path / "courses.db")
    index.replace_all("example.com", [_course(1, name="Old")])
    index.replace_all("example.com", [_course(2, name="New")])
    assert [item.name for item in index.search("example.com").items] == ["New"]

    index.upsert("example.com", _course(2, state="ARCHIVED", name="Renamed"))
    result = index.search("example.com", "Renamed")
    assert result.total == 1
    assert result.items[0].course_state == "ARCHIVED"


def test_course_index_like_wildcards_are_literal(tmp_path):
    index = CourseIndex(tmp_path / "courses.db")
    index.replace_all(
        "example.com",
        [_course(1, name="A_B"), _course(2, name="ACB")],
    )
    assert [item.name for item in index.search("example.com", "A_B").items] == ["A_B"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not meaningful on Windows")
def test_course_index_files_are_owner_only(tmp_path):
    index = CourseIndex(tmp_path / "private" / "courses.db")
    index.replace_all("example.com", [_course(1)])
    assert stat.S_IMODE(os.stat(index.path).st_mode) & 0o077 == 0
    assert stat.S_IMODE(os.stat(index.path.parent).st_mode) & 0o077 == 0
