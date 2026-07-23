from __future__ import annotations

import time

from gamgui.core.classroom.index import CourseIndex
from gamgui.core.classroom.models import CourseSummary


def test_50k_course_search_stays_bounded_and_under_local_budget(tmp_path):
    index = CourseIndex(tmp_path / "courses.db")
    index.replace_all(
        "example.com",
        [
            CourseSummary(
                id=str(number),
                name=f"Course {number:05d}",
                section=f"Section_{number:05d}",
                room=f"R{number % 100}",
                owner_id=f"owner-{number % 500}",
                course_state="ACTIVE",
            )
            for number in range(50_000)
        ],
    )

    durations_ms = []
    for _ in range(20):
        started = time.perf_counter()
        page = index.search("example.com", "Section_4321")
        durations_ms.append((time.perf_counter() - started) * 1000)

    p95 = sorted(durations_ms)[18]
    assert p95 < 100
    assert len(page.items) <= 50
    assert index.status("example.com").count == 50_000
