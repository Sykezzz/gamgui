from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from gamgui.core.audit import AuditLog
from gamgui.core.classroom.index import CourseIndex
from gamgui.core.classroom.models import CourseSummary
from gamgui.core.connectors.base import RiskLevel
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import COURSE_INDEX_FIELDS

pytestmark = pytest.mark.asyncio


class FakeRunner:
    def __init__(self) -> None:
        self.calls = []

    async def run_authenticated(self, domain, argv, serialize=False):
        self.calls.append((domain, list(argv), serialize))
        if argv[:2] == ["print", "courses"]:
            return (
                '{"id":"123","name":"English 1","courseState":"ACTIVE","ownerId":"u1"}\n'
                '{"id":"456","name":"Geometry","courseState":"ARCHIVED","ownerId":"u2"}'
            )
        if argv[:2] == ["info", "course"]:
            return (
                '{"id":"123","name":"English 1","courseState":"ACTIVE",'
                '"ownerId":"u1","ownerEmail":"teacher@example.com","aliases":["d:Section_123"]}'
            )
        if argv[:2] == ["print", "course-participants"]:
            return (
                '{"courseId":"123","userId":"u1","profile":'
                '{"emailAddress":"teacher@example.com","name":{"fullName":"Teacher One"}}}'
            )
        return "ok"


class SpoolRunner:
    def __init__(self, path: Path, payload: str) -> None:
        self.path = path
        self.payload = payload
        self.calls = []

    @asynccontextmanager
    async def run_authenticated_to_file(self, domain, argv, **_kwargs):
        self.calls.append((domain, list(argv)))
        self.path.write_text(self.payload, encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            yield SimpleNamespace(
                path=self.path,
                stdout_bytes=self.path.stat().st_size,
            )
        finally:
            self.path.unlink(missing_ok=True)


async def test_connector_classroom_reads_and_mutations_are_audited(tmp_path: Path):
    runner = FakeRunner()
    connector = GAMConnector(
        runner=runner,
        domain="example.com",
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    courses = await connector.list_courses()
    assert [course.id for course in courses] == ["123", "456"]
    assert courses[0].name == "English 1"

    detail = await connector.get_course("123")
    assert detail.owner_email == "teacher@example.com"
    assert detail.aliases == ("d:Section_123",)

    members = await connector.list_course_participants("123", "teachers")
    assert members[0].email == "teacher@example.com"
    assert members[0].role == "teachers"

    removed = await connector.remove_course_participant(
        "123", "students", "student@example.com"
    )
    assert removed.ok
    assert removed.preview.risk is RiskLevel.DESTRUCTIVE
    assert runner.calls[-1][2] is True
    audit = connector.audit.tail()[-1]
    assert audit["action"] == "remove_course_participant"
    assert audit["target"] == "student@example.com"


async def test_course_refresh_streams_private_spool_off_event_loop(
    tmp_path: Path,
    monkeypatch,
):
    spool = tmp_path / "courses.ndjson"
    runner = SpoolRunner(
        spool,
        (
            '{"id":"123","name":"English 1","courseState":"ACTIVE",'
            '"ownerId":"u1","description":"must not persist"}\n'
            '{"id":"456","name":"Geometry","courseState":"ARCHIVED","ownerId":"u2"}'
        ),
    )
    connector = GAMConnector(runner=runner, domain="example.com")
    index = CourseIndex(tmp_path / "courses.db")
    caller_thread = threading.get_ident()
    worker_threads = []
    real_replace = index.replace_all

    def tracked_replace(domain, courses):
        worker_threads.append(threading.get_ident())
        return real_replace(domain, courses)

    monkeypatch.setattr(index, "replace_all", tracked_replace)

    assert await connector.refresh_course_index(index) == 2
    assert not spool.exists()
    assert worker_threads
    assert all(thread_id != caller_thread for thread_id in worker_threads)
    command = runner.calls[0][1]
    fields_position = command.index("fields")
    assert command[fields_position + 1] == ",".join(COURSE_INDEX_FIELDS)
    page = index.search("example.com")
    assert [item.id for item in page.items] == ["123", "456"]
    assert not hasattr(page.items[0], "description")


async def test_course_refresh_failure_cleans_spool_and_preserves_snapshot(tmp_path: Path):
    spool = tmp_path / "courses.ndjson"
    runner = SpoolRunner(
        spool,
        '{"id":"new","name":"Uncommitted","courseState":"ACTIVE"}\nnot-json',
    )
    connector = GAMConnector(runner=runner, domain="example.com")
    index = CourseIndex(tmp_path / "courses.db")
    index.replace_all(
        "example.com",
        [CourseSummary(id="stable", name="Stable", course_state="ACTIVE")],
    )

    with pytest.raises(ValueError, match="invalid newline-delimited JSON"):
        await connector.refresh_course_index(index)

    assert not spool.exists()
    assert [item.id for item in index.search("example.com").items] == ["stable"]


async def test_course_refresh_cancellation_waits_for_worker_before_spool_cleanup(
    tmp_path: Path,
    monkeypatch,
):
    import gamgui.core.connectors.gam_connector as connector_module

    spool = tmp_path / "courses.ndjson"
    runner = SpoolRunner(spool, '{"id":"123","name":"English"}')
    connector = GAMConnector(runner=runner, domain="example.com")
    index = CourseIndex(tmp_path / "courses.db")
    worker_started = threading.Event()

    def cancellable_worker(_index, _domain, _path, cancelled):
        worker_started.set()
        assert cancelled.wait(timeout=2)
        raise connector_module._CourseRefreshCancelled

    monkeypatch.setattr(
        connector_module,
        "_replace_course_index_from_spool",
        cancellable_worker,
    )
    task = asyncio.create_task(connector.refresh_course_index(index))
    assert await asyncio.to_thread(worker_started.wait, 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not spool.exists()
