from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Optional, Sequence

from gamgui.core.classroom.models import CourseDetail, CourseParticipant
from gamgui.core.connectors.base import (
    ChangePreview,
    ChangeResult,
    ConnectorID,
    RiskLevel,
)
from gamgui.core.gam.models import GAMUser


def course(
    course_id: str = "123",
    *,
    name: str = "English 1",
    state: str = "ACTIVE",
    owner_email: str = "teacher@example.com",
    owner_id: str = "owner-1",
) -> CourseDetail:
    return CourseDetail(
        id=course_id,
        name=name,
        section="CR S1",
        room="204",
        owner_id=owner_id,
        owner_email=owner_email,
        course_state=state,
        creation_time="2026-01-01T00:00:00Z",
        update_time="2026-07-20T00:00:00Z",
        alternate_link=f"https://classroom.google.com/c/{course_id}",
        description_heading="Welcome",
        description="Read first",
        aliases=(f"d:Section_{course_id}",),
    )


def member(
    email: str,
    role: str,
    *,
    course_id: str = "123",
    user_id: str = "",
) -> CourseParticipant:
    return CourseParticipant(
        course_id=course_id,
        email=email,
        user_id=user_id,
        role=role,
        full_name=email.split("@", 1)[0].replace(".", " ").title(),
    )


class FakeClassroomConnector:
    def __init__(self) -> None:
        self.domain = "example.com"
        self.courses: Dict[str, CourseDetail] = {"123": course()}
        self.rosters: Dict[tuple, List[CourseParticipant]] = {
            ("123", "teachers"): [
                member("teacher@example.com", "teachers", user_id="owner-1"),
                member("assistant@example.com", "teachers", user_id="user-2"),
            ],
            ("123", "students"): [
                member("student1@example.com", "students", user_id="student-1"),
                member("student2@example.com", "students", user_id="student-2"),
            ],
        }
        self.calls: List[tuple] = []
        self.fail_for: set = set()

    def _result(
        self,
        action: str,
        target: str,
        *,
        risk: RiskLevel = RiskLevel.LOW,
        ok: bool = True,
        detail: str = "",
    ) -> ChangeResult:
        preview = ChangePreview(
            connector_id=ConnectorID.GOOGLE_WORKSPACE,
            target=target,
            summary=action,
            risk=risk,
            argv=[action, target],
        )
        return ChangeResult(preview=preview, ok=ok, detail=detail)

    async def list_courses(
        self,
        states: Optional[Sequence[str]] = None,
        teacher: str = "",
        student: str = "",
        fields: Optional[Sequence[str]] = None,
    ) -> List[CourseDetail]:
        self.calls.append(("list_courses", teacher, student))
        values = list(self.courses.values())
        if states:
            allowed = {state.upper() for state in states}
            values = [item for item in values if item.course_state in allowed]
        if teacher:
            return values if teacher in {"teacher@example.com", "assistant@example.com"} else []
        if student:
            return values if student in {"student1@example.com", "student2@example.com"} else []
        return values

    async def get_course(self, course_id: str) -> CourseDetail:
        self.calls.append(("get_course", course_id))
        return self.courses[course_id]

    async def get_user(
        self, email: str, fields: Optional[Sequence[str]] = None
    ) -> GAMUser:
        self.calls.append(("get_user", email))
        if not email.endswith("@example.com") or email.startswith("missing"):
            raise RuntimeError("not found")
        return GAMUser(
            primary_email=email,
            suspended=email.startswith("suspended"),
        )

    async def list_course_participants(
        self, course_id: str, role: str
    ) -> List[CourseParticipant]:
        self.calls.append(("list_course_participants", course_id, role))
        return list(self.rosters.get((course_id, role), []))

    async def create_course(self, **kwargs) -> ChangeResult:
        self.calls.append(("create_course", kwargs))
        assert kwargs["state"] == "PROVISIONED"
        return self._result("create_course", kwargs["owner_email"])

    async def update_course(self, course_id: str, **kwargs) -> ChangeResult:
        self.calls.append(("update_course", course_id, kwargs))
        current = self.courses[course_id]
        self.courses[course_id] = replace(
            current,
            name=kwargs["name"],
            section=kwargs["section"],
            room=kwargs["room"],
            description_heading=kwargs["description_heading"],
            description=kwargs["description"],
        )
        return self._result("update_course", course_id)

    async def update_course_state(
        self, course_id: str, state: str
    ) -> ChangeResult:
        self.calls.append(("update_course_state", course_id, state))
        self.courses[course_id] = replace(
            self.courses[course_id], course_state=state.upper()
        )
        return self._result(
            "update_course_state",
            course_id,
            risk=RiskLevel.DESTRUCTIVE if state.upper() == "ARCHIVED" else RiskLevel.LOW,
        )

    async def transfer_course_owner(
        self, course_id: str, target_email: str
    ) -> ChangeResult:
        self.calls.append(("transfer_course_owner", course_id, target_email))
        self.courses[course_id] = replace(
            self.courses[course_id],
            owner_email=target_email,
            owner_id=f"id-{target_email}",
        )
        teachers = self.rosters.setdefault((course_id, "teachers"), [])
        if target_email not in {item.email for item in teachers}:
            teachers.append(
                member(target_email, "teachers", course_id=course_id, user_id=f"id-{target_email}")
            )
        return self._result(
            "transfer_course_owner",
            course_id,
            risk=RiskLevel.DESTRUCTIVE,
        )

    async def add_course_participant(
        self, course_id: str, role: str, email: str
    ) -> ChangeResult:
        self.calls.append(("add_course_participant", course_id, role, email))
        if email in self.fail_for:
            return self._result("add", email, ok=False, detail="refused")
        roster = self.rosters.setdefault((course_id, role), [])
        if email not in {item.email for item in roster}:
            roster.append(member(email, role, course_id=course_id, user_id=f"id-{email}"))
        return self._result("add", email)

    async def remove_course_participant(
        self, course_id: str, role: str, email: str
    ) -> ChangeResult:
        self.calls.append(("remove_course_participant", course_id, role, email))
        if email in self.fail_for:
            return self._result(
                "remove", email, risk=RiskLevel.DESTRUCTIVE, ok=False, detail="refused"
            )
        roster = self.rosters.setdefault((course_id, role), [])
        self.rosters[(course_id, role)] = [
            item for item in roster if item.email != email
        ]
        return self._result("remove", email, risk=RiskLevel.DESTRUCTIVE)
