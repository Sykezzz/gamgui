"""Tolerant Classroom models decoded from GAM ``formatjson`` output."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

COURSE_STATES = ("ACTIVE", "ARCHIVED", "PROVISIONED", "DECLINED", "SUSPENDED")
ROSTER_ROLES = ("teachers", "students")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


def _get(data: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def _course_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    nested = data.get("course")
    if isinstance(nested, dict):
        merged = dict(data)
        merged.update(nested)
        return merged
    return data


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [part.strip() for part in re.split(r"[\n,]+", text) if part.strip()]
    return []


def normalize_email(value: str) -> str:
    return (value or "").strip().casefold()


def valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(normalize_email(value)))


def normalize_role(role: str) -> str:
    value = (role or "").strip().lower()
    if value not in ROSTER_ROLES:
        raise ValueError(f"invalid Classroom roster role: {role!r}")
    return value


def normalize_state(state: str) -> str:
    value = (state or "").strip().upper()
    if value not in COURSE_STATES:
        raise ValueError(f"invalid Classroom course state: {state!r}")
    return value


@dataclass(frozen=True)
class CourseSummary:
    id: str
    name: str
    section: str = ""
    room: str = ""
    owner_id: str = ""
    course_state: str = ""
    creation_time: str = ""
    update_time: str = ""
    alternate_link: str = ""

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "CourseSummary":
        d = _course_payload(data)
        state = str(_get(d, "courseState", "state", "Course State", default="")).upper()
        return cls(
            id=str(_get(d, "id", "courseId", "courseID", "Course ID", default="")),
            name=str(_get(d, "name", "courseName", "Name", default="")),
            section=str(_get(d, "section", "Section", default="")),
            room=str(_get(d, "room", "Room", default="")),
            owner_id=str(_get(d, "ownerId", "ownerID", "Owner ID", default="")),
            course_state=state,
            creation_time=str(_get(d, "creationTime", "Creation Time", default="")),
            update_time=str(_get(d, "updateTime", "Update Time", default="")),
            alternate_link=str(_get(d, "alternateLink", "Alternate Link", default="")),
        )

    @property
    def state_label(self) -> str:
        return self.course_state.replace("_", " ").title() or "Unknown"


@dataclass(frozen=True)
class CourseDetail(CourseSummary):
    owner_email: str = ""
    description_heading: str = ""
    description: str = ""
    aliases: Tuple[str, ...] = ()
    raw: Dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "CourseDetail":
        d = _course_payload(data)
        summary = CourseSummary.from_json(d)
        aliases = _as_list(_get(d, "aliases", "alias", "Aliases", default=[]))
        return cls(
            id=summary.id,
            name=summary.name,
            section=summary.section,
            room=summary.room,
            owner_id=summary.owner_id,
            course_state=summary.course_state,
            creation_time=summary.creation_time,
            update_time=summary.update_time,
            alternate_link=summary.alternate_link,
            owner_email=str(
                _get(d, "ownerEmail", "owneremail", "Owner Email", "teacherEmail", default="")
            ).casefold(),
            description_heading=str(
                _get(d, "descriptionHeading", "heading", "Description Heading", default="")
            ),
            description=str(_get(d, "description", "Description", default="")),
            aliases=tuple(aliases),
            raw=dict(d),
        )

    @property
    def read_only(self) -> bool:
        return self.course_state in ("DECLINED", "SUSPENDED")

    @property
    def roster_editable(self) -> bool:
        return self.course_state == "ACTIVE"


@dataclass(frozen=True)
class CourseParticipant:
    course_id: str
    email: str
    user_id: str = ""
    role: str = ""
    full_name: str = ""
    photo_url: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_json(cls, data: Dict[str, Any], role: str = "") -> "CourseParticipant":
        profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        name_obj = profile.get("name") if isinstance(profile.get("name"), dict) else {}
        email = str(
            _get(
                profile,
                "emailAddress",
                "email",
                default=_get(data, "emailAddress", "email", "Email", default=""),
            )
        )
        full_name = str(
            _get(
                name_obj,
                "fullName",
                default=_get(profile, "fullName", default=_get(data, "name", "Name", default="")),
            )
        )
        parsed_role = str(_get(data, "role", "participantType", "Role", default=role)).lower()
        if parsed_role in ("teacher", "student"):
            parsed_role += "s"
        return cls(
            course_id=str(_get(data, "courseId", "courseID", "Course ID", default="")),
            email=normalize_email(email),
            user_id=str(_get(data, "userId", "userID", "User ID", default=profile.get("id", ""))),
            role=parsed_role,
            full_name=full_name,
            photo_url=str(_get(profile, "photoUrl", default=_get(data, "photoUrl", default=""))),
            raw=dict(data),
        )

    @property
    def label(self) -> str:
        return self.full_name or self.email or self.user_id


@dataclass(frozen=True)
class CourseRosterSnapshot(Sequence[CourseParticipant]):
    """Compact rosters plus proof that every requested course was represented.

    GAM's bulk roster export emits one row per course, including an explicit row
    with empty participant arrays.  Keeping that coverage evidence separate from
    the flattened participants prevents an omitted course from being mistaken for
    a genuinely empty roster.  Only deduplicated emails are retained; participant
    objects are generated lazily for compatibility so million-member imports do
    not keep a dataclass and raw mapping per membership in memory.
    """

    rosters: Mapping[str, Tuple[frozenset[str], frozenset[str]]]
    seen_course_ids: frozenset[str]

    def __post_init__(self) -> None:
        compact: Dict[str, Tuple[frozenset[str], frozenset[str]]] = {}
        for raw_course_id, roles in self.rosters.items():
            course_id = str(raw_course_id).strip()
            if not course_id or not isinstance(roles, tuple) or len(roles) != 2:
                raise ValueError("invalid compact Classroom roster snapshot")
            compact[course_id] = (
                frozenset(normalize_email(email) for email in roles[0] if normalize_email(email)),
                frozenset(normalize_email(email) for email in roles[1] if normalize_email(email)),
            )
        object.__setattr__(self, "rosters", MappingProxyType(compact))
        object.__setattr__(
            self,
            "seen_course_ids",
            frozenset(
                str(course_id).strip()
                for course_id in self.seen_course_ids
                if str(course_id).strip()
            ),
        )

    @classmethod
    def empty(cls, course_ids: Iterable[str] = ()) -> "CourseRosterSnapshot":
        seen = frozenset(
            str(course_id).strip()
            for course_id in course_ids
            if str(course_id).strip()
        )
        return cls(
            {course_id: (frozenset(), frozenset()) for course_id in seen},
            seen,
        )

    @classmethod
    def from_participants(
        cls,
        participants: Iterable[CourseParticipant],
        seen_course_ids: Iterable[str],
    ) -> "CourseRosterSnapshot":
        grouped: Dict[str, Tuple[set[str], set[str]]] = {}
        for participant in participants:
            course_id = str(participant.course_id).strip()
            email = normalize_email(participant.email)
            if not course_id or not email:
                continue
            teachers, students = grouped.setdefault(course_id, (set(), set()))
            role = str(participant.role).strip().casefold()
            if role in {"teacher", "teachers"}:
                teachers.add(email)
            elif role in {"student", "students"}:
                students.add(email)
        return cls(
            {
                course_id: (frozenset(teachers), frozenset(students))
                for course_id, (teachers, students) in grouped.items()
            },
            frozenset(seen_course_ids),
        )

    def for_course(self, course_id: str) -> Tuple[frozenset[str], frozenset[str]]:
        return self.rosters.get(
            str(course_id).strip(),
            (frozenset(), frozenset()),
        )

    def __iter__(self) -> Iterator[CourseParticipant]:
        for course_id in sorted(self.rosters):
            teachers, students = self.rosters[course_id]
            for email in sorted(teachers):
                yield CourseParticipant(
                    course_id=course_id,
                    email=email,
                    role="teachers",
                )
            for email in sorted(students):
                yield CourseParticipant(
                    course_id=course_id,
                    email=email,
                    role="students",
                )

    def __len__(self) -> int:
        return sum(
            len(teachers) + len(students)
            for teachers, students in self.rosters.values()
        )

    def __getitem__(
        self,
        index: int | slice,
    ) -> CourseParticipant | Tuple[CourseParticipant, ...]:
        return tuple(self)[index]

    def covers(self, course_ids: Iterable[str]) -> bool:
        requested = {
            str(course_id).strip()
            for course_id in course_ids
            if str(course_id).strip()
        }
        return requested <= self.seen_course_ids


@dataclass(frozen=True)
class RosterDiff:
    role: str
    desired: Tuple[str, ...]
    current: Tuple[str, ...]
    adds: Tuple[str, ...]
    removes: Tuple[str, ...]
    unchanged: Tuple[str, ...]

    @classmethod
    def compute(
        cls,
        role: str,
        desired: Iterable[str],
        current: Iterable[str],
    ) -> "RosterDiff":
        normalized_role = normalize_role(role)
        desired_set = {normalize_email(item) for item in desired if normalize_email(item)}
        current_set = {normalize_email(item) for item in current if normalize_email(item)}
        return cls(
            role=normalized_role,
            desired=tuple(sorted(desired_set)),
            current=tuple(sorted(current_set)),
            adds=tuple(sorted(desired_set - current_set)),
            removes=tuple(sorted(current_set - desired_set)),
            unchanged=tuple(sorted(current_set & desired_set)),
        )

    @property
    def change_count(self) -> int:
        return len(self.adds) + len(self.removes)

    @property
    def has_removals(self) -> bool:
        return bool(self.removes)

    @property
    def desired_hash(self) -> str:
        payload = "\n".join((self.role,) + self.desired)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def basis_hash(self) -> str:
        payload = "\n".join(
            (self.role, "ADD") + self.adds + ("REMOVE",) + self.removes
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_desired_roster(text: str) -> List[str]:
    """Parse an email column CSV or a one-email-per-line roster.

    Accepted headers are intentionally narrow. Unknown multi-column CSVs are rejected instead of
    guessing which column could trigger removals.
    """

    source = (text or "").lstrip("\ufeff")
    if not source.strip():
        return []
    lines = [line for line in source.splitlines() if line.strip()]
    if not lines:
        return []

    first = lines[0]
    if "," not in first:
        header = first.strip().casefold()
        if header in ("email", "emailaddress", "studentemail", "teacheremail"):
            values = lines[1:]
        else:
            values = lines
        return _validated_emails(values)

    reader = csv.DictReader(io.StringIO(source))
    fields = {str(field).strip().casefold(): field for field in (reader.fieldnames or []) if field}
    selected = next(
        (
            fields[key]
            for key in ("email", "emailaddress", "studentemail", "teacheremail", "primaryemail")
            if key in fields
        ),
        None,
    )
    if selected is None:
        raise ValueError("CSV must include an email column.")
    return _validated_emails(str(row.get(selected) or "") for row in reader)


def _validated_emails(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    invalid: List[str] = []
    for raw in values:
        email = normalize_email(str(raw))
        if not email:
            continue
        if not valid_email(email):
            invalid.append(email)
            continue
        if email not in seen:
            seen.add(email)
            result.append(email)
    if invalid:
        sample = ", ".join(invalid[:3])
        suffix = "" if len(invalid) <= 3 else f" and {len(invalid) - 3} more"
        raise ValueError(f"Invalid email address: {sample}{suffix}.")
    return result


def participant_emails(participants: Sequence[CourseParticipant]) -> List[str]:
    return [participant.email for participant in participants if participant.email]
