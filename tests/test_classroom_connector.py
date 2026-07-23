from __future__ import annotations

from pathlib import Path

import pytest

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.base import RiskLevel
from gamgui.core.connectors.gam_connector import GAMConnector

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
