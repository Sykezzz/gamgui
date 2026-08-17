"""The Google Workspace connector — the only one implemented in the MVP.

It translates high-level operations into GAM commands (via :mod:`gamgui.core.gam.commands`), runs
them through the :class:`GAMRunner`, parses the output, and records mutations to the audit log.
The rest of the app talks to this object and never sees GAM syntax.
"""

from __future__ import annotations

import asyncio
import csv
import heapq
import inspect
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, Union

from ..audit import AuditLog
from ..classroom.index import CourseIndex
from ..classroom.models import (
    CourseDetail,
    CourseParticipant,
    CourseRosterSnapshot,
    CourseSummary,
)
from ..directory_index import DirectoryIndex, Page
from ..gam.commands import GAMCommands, SIGNATURE_USER_FIELDS, build_user_query
from ..gam.errors import GAMError, GAMErrorKind
from ..gam.models import (
    BatchExecutionReceipt,
    CalendarACL,
    CalendarEvent,
    GAMGroup,
    GAMUser,
    GroupMember,
    ResourceCalendar,
    UserCalendar,
    Vacation,
)
from ..calendar_index import IndexedCalendar
from ..gam.parser import iter_records_file, parse_one, parse_records
from ..gam.runner import (
    GAMRunner,
    StreamingGAMError,
    StreamingRunResult,
    await_secure_remove_private_file,
    secure_remove_private_file,
)
from ..secrets.ephemeral import private_runtime_spool_dir
from .base import (
    Capability,
    ChangePreview,
    ChangeResult,
    ConnectionStatus,
    Connector,
    ConnectorID,
    LifecycleAction,
    RiskLevel,
)
from .person import ConnectorAccount, Person

MAX_ONEROSTER_BULK_ROSTER_MEMBERS = 500_000
ONEROSTER_COURSE_SELECTOR_CHUNK_SIZE = 100
ONEROSTER_COURSE_SELECTOR_WORKERS = 2
ONEROSTER_RATE_LIMIT_RETRY_DELAYS = (60.0, 120.0, 300.0)
ONEROSTER_NATIVE_BATCH_WORKERS = 10
ONEROSTER_NATIVE_BATCH_TIMEOUT = 86400.0

ClassroomBatchCommandSink = Callable[[Sequence[str]], None]
ClassroomBatchCommandSource = Union[
    Iterable[Sequence[str]],
    Callable[[ClassroomBatchCommandSink], None],
]


@dataclass(frozen=True)
class NativeClassroomBatchProgress:
    """Sanitized dispatch count parsed from one native GAM progress line."""

    dispatched: int
    total: int
    raw_line: str


@dataclass(frozen=True)
class NativeClassroomBatchReceipt:
    """Receipt for a whole OneRoster phase submitted to one GAM process.

    ``progress_count`` is GAM's last observed dispatch count. It is not a child-command
    completion count; the successful process result is the authoritative completion signal.
    """

    submitted_count: int
    process_result: StreamingRunResult
    progress_count: int
    worker_count: int = ONEROSTER_NATIVE_BATCH_WORKERS
    outcome: str = "completed"

    @property
    def duration_seconds(self) -> float:
        return self.process_result.duration_seconds

    @property
    def returncode(self) -> int:
        return self.process_result.returncode


class NativeClassroomBatchError(GAMError):
    """A native GAM phase failed after its commands were durably submitted."""

    def __init__(self, cause: GAMError, *, submitted_count: int) -> None:
        self.submitted_count = int(submitted_count)
        self.process_result = (
            cause.result if isinstance(cause, StreamingGAMError) else None
        )
        super().__init__(
            kind=cause.kind,
            exit_code=cause.exit_code,
            stderr=cause.stderr,
            argv=GAMCommands.batch_file("<private-batch>", show_commands=False),
        )

    @property
    def error_code(self) -> str:
        return "GAM-NATIVE-BATCH-FAILED"


NativeClassroomBatchProgressCallback = Callable[
    [NativeClassroomBatchProgress],
    Optional[Awaitable[None]],
]

_NATIVE_BATCH_PROGRESS_PATTERN = re.compile(
    r"(?:^|,)\s*0\s*,\s*Processing item\s+(\d+)\s*/\s*(\d+)\s*$",
    re.IGNORECASE,
)


def _parse_signature(text: str) -> str:
    """Pull the signature body out of ``gam user X show signature`` text output.

    Format is ``Signature:`` followed by indented lines (or ``None`` when empty).
    """
    lines = (text or "").splitlines()
    body: List[str] = []
    capturing = False
    for ln in lines:
        if capturing:
            # stop at the next non-indented line (e.g. another "SendAs Address:")
            if ln.strip() and not ln.startswith(" "):
                break
            body.append(ln.strip())
        elif ln.strip().rstrip(":") == "Signature":
            capturing = True
    sig = "\n".join(body).strip()
    return "" if sig in ("", "None") else sig


def _usage_quota(row: dict) -> int:
    try:
        return int(float(row.get("accounts:used_quota_in_mb") or 0))
    except (TypeError, ValueError):
        return 0


def _bounded_usage_rows(path: Path, limit: int) -> List[dict]:
    """Read only the largest usage rows from a GAM CSV spool.

    ``gam report`` can print progress text before its CSV header, so scan to the allowlisted
    ``email`` header first. The heap keeps memory proportional to the UI bound, not tenant size.
    """

    cap = max(1, min(int(limit), 100))
    with Path(path).open("r", encoding="utf-8", errors="replace", newline="") as source:
        header = ""
        for line in source:
            if line.lstrip().lower().startswith("email,"):
                header = line
                break
        if not header:
            return []
        fieldnames = next(csv.reader([header]))
        rows = csv.DictReader(source, fieldnames=fieldnames)
        return heapq.nlargest(cap, rows, key=_usage_quota)


_GROUP_MEMBER_FIELDS = ("email", "role", "type", "status")
_GROUP_MEMBER_PAGE_SIZE = 50
_SIGNATURE_LARGE_SCOPES = {"company", "group", "ou", "department", "location"}


class _CourseRefreshCancelled(RuntimeError):
    """Internal signal used to roll back an in-flight streamed index refresh."""


def _course_summaries_from_spool(
    path: Path,
    cancelled: threading.Event,
) -> Iterator[CourseSummary]:
    for record in iter_records_file(path):
        if cancelled.is_set():
            raise _CourseRefreshCancelled
        yield CourseSummary.from_json(record)
    if cancelled.is_set():
        raise _CourseRefreshCancelled


def _replace_course_index_from_spool(
    index: CourseIndex,
    domain: str,
    path: Path,
    cancelled: threading.Event,
) -> int:
    return index.replace_all(
        domain,
        _course_summaries_from_spool(path, cancelled),
    )


def _bounded_text(value: object, limit: int) -> str:
    return str(value or "").strip()[: max(0, int(limit))]


def _safe_email(value: object) -> str:
    email = str(value or "").strip()
    if len(email) > 254 or "@" not in email or any(ch.isspace() for ch in email):
        return ""
    return email


def _bounded_group_member(record: dict) -> Optional[GroupMember]:
    member = GroupMember.from_json(record)
    email = _safe_email(member.email)
    if not email:
        return None
    return GroupMember(
        email=email,
        role=_bounded_text(member.role, 32).upper() or "MEMBER",
        member_type=_bounded_text(member.member_type, 32).upper() or "USER",
        status=_bounded_text(member.status, 64),
    )


def _is_allowed_classroom_batch_command(argv: Sequence[str]) -> bool:
    """Fail closed around the only mutation shapes OneRoster may batch."""
    command = tuple(str(argument) for argument in argv)
    if not command or any(any(ch in item for ch in "\r\n\x00") for item in command):
        return False
    if len(command) >= 8 and command[:2] == ("create", "course"):
        attributes = command[2:]
        if len(attributes) % 2:
            return False
        keys = attributes[::2]
        allowed = {
            "alias",
            "name",
            "teacher",
            "section",
            "room",
            "descriptionheading",
            "description",
            "state",
        }
        values = dict(zip(keys, attributes[1::2]))
        return (
            len(set(keys)) == len(keys)
            and set(keys) <= allowed
            and {"alias", "name", "teacher", "state"} <= set(keys)
            and values["state"].casefold() == "provisioned"
            and values["alias"].startswith("Section_")
            and _safe_batch_email(values["teacher"])
        )
    if len(command) >= 5 and command[:2] == ("update", "course"):
        if not command[2].startswith("d:Section_"):
            return False
        attributes = command[3:]
        if len(attributes) == 2 and attributes[0] == "state":
            return attributes[1].casefold() in {"active", "archived"}
        if len(attributes) == 2 and attributes[0] == "teacher":
            return _safe_batch_email(attributes[1])
        return (
            (len(attributes) == 6 and attributes[::2] == ("name", "section", "room"))
            or (
                len(attributes) == 10
                and attributes[::2]
                == ("name", "section", "room", "descriptionheading", "description")
            )
        )
    if (
        len(command) == 5
        and command[0] == "course"
        and command[1].startswith("d:Section_")
        and command[2] in {"add", "remove"}
        and command[3] in {"teachers", "students"}
        and _safe_batch_email(command[4])
    ):
        return True
    return False


def _safe_batch_email(value: str) -> bool:
    email = str(value or "")
    return (
        3 <= len(email) <= 254
        and "@" in email
        and not any(character.isspace() for character in email)
    )


def _remove_private_batch(path: Path) -> bool:
    """Overwrite then remove a temporary selector or roster batch file."""

    return secure_remove_private_file(path)


async def _await_private_batch_removal(path: Path) -> bool:
    """Finish private-file cleanup even if the caller is cancelled again."""

    return await await_secure_remove_private_file(path)


async def _parse_private_spool_off_loop(reader, *args):
    """Keep a runner-owned spool alive until its worker-thread parser has exited."""
    worker = asyncio.create_task(asyncio.to_thread(reader, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            worker.result()
        except Exception:
            # Cancellation is the caller-visible result. Retrieving a parser failure prevents an
            # unobserved-task warning before the runner securely removes its private spool.
            pass
        raise cancellation


def _read_group_member_spool(
    path: Path,
    limit: Optional[int],
) -> Tuple[List[GroupMember], int]:
    """Reduce a private GAM spool to bounded, display-safe membership summaries."""

    cap = None if limit is None else max(1, min(int(limit), _GROUP_MEMBER_PAGE_SIZE))
    items: List[GroupMember] = []
    total = 0
    for record in iter_records_file(path):
        member = _bounded_group_member(record)
        if member is None:
            continue
        total += 1
        if cap is None or len(items) < cap:
            items.append(member)
    return items, total


def _read_group_member_emails(path: Path) -> Set[str]:
    emails: Set[str] = set()
    for record in iter_records_file(path):
        member = _bounded_group_member(record)
        if member is not None:
            emails.add(member.email.casefold())
    return emails


def _bounded_signature_user(record: dict) -> Optional[GAMUser]:
    user = GAMUser.from_json(record)
    email = _safe_email(user.primary_email)
    if not email:
        return None
    # Drop ``raw`` and clamp every variable-bearing field. The source spool is already projected to
    # SIGNATURE_USER_FIELDS; these response/application bounds keep one malformed directory profile
    # from producing an oversized preview or mutation.
    return GAMUser(
        primary_email=email,
        given_name=_bounded_text(user.given_name, 100),
        family_name=_bounded_text(user.family_name, 100),
        suspended=bool(user.suspended),
        org_unit_path=_bounded_text(user.org_unit_path or "/", 512) or "/",
        title=_bounded_text(user.title, 128),
        department=_bounded_text(user.department, 128),
        location=_bounded_text(user.location, 256),
        phone=_bounded_text(user.phone, 64),
    )


def _read_signature_user_spool(
    path: Path,
    scope_type: str,
    scope_value: str,
    group_emails: Optional[Set[str]] = None,
) -> List[GAMUser]:
    value = (scope_value or "").strip()
    folded = value.casefold()
    ou_prefix = value.rstrip("/") + "/"
    membership = group_emails or set()
    users: List[GAMUser] = []
    for record in iter_records_file(path):
        user = _bounded_signature_user(record)
        if user is None or user.suspended:
            continue
        if scope_type == "group":
            matches = user.primary_email.casefold() in membership
        elif scope_type == "ou":
            matches = user.org_unit_path == value or user.org_unit_path.startswith(ou_prefix)
        elif scope_type == "department":
            matches = user.department.strip().casefold() == folded
        elif scope_type == "location":
            matches = user.location.strip().casefold() == folded
        else:
            matches = scope_type == "company"
        if matches:
            users.append(user)
    return users


def _read_oneroster_directory_spool(path: Path) -> dict[str, GAMUser]:
    """Build a primary-and-alias lookup without retaining full Directory records."""
    snapshot: dict[str, GAMUser] = {}
    for record in iter_records_file(path):
        parsed = GAMUser.from_json(record)
        primary = _safe_email(parsed.primary_email).casefold()
        if not primary:
            continue
        aliases = list(
            dict.fromkeys(
                alias
                for value in parsed.aliases
                if (alias := _safe_email(value).casefold()) and alias != primary
            )
        )
        canonical = GAMUser(
            primary_email=primary,
            suspended=bool(parsed.suspended),
            aliases=aliases,
            user_id=str(parsed.user_id or "").strip(),
        )
        for identifier in (primary, *aliases):
            existing = snapshot.get(identifier)
            if existing is not None and existing.primary_email != primary:
                raise ValueError(
                    "OneRoster directory snapshot contains an ambiguous email identifier."
                )
            snapshot[identifier] = canonical
    return snapshot


def _without_course_raw(
    detail: CourseDetail,
    aliases: Optional[Sequence[str]] = None,
) -> CourseDetail:
    """Retain planner fields while dropping the duplicate source-record dictionary."""
    return CourseDetail(
        id=detail.id,
        name=detail.name,
        section=detail.section,
        room=detail.room,
        owner_id=detail.owner_id,
        course_state=detail.course_state,
        creation_time=detail.creation_time,
        update_time=detail.update_time,
        alternate_link=detail.alternate_link,
        owner_email=detail.owner_email,
        description_heading=detail.description_heading,
        description=detail.description,
        aliases=tuple(detail.aliases if aliases is None else aliases),
    )


def _read_oneroster_course_aliases(record: dict) -> tuple[str, ...]:
    value = record.get("JSON-aliases")
    if value in (None, ""):
        value = record.get("aliases", record.get("Aliases", ()))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError(
                "Exact managed course lookup returned invalid alias data."
            ) from exc
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            "Exact managed course lookup returned invalid alias data."
        )
    aliases: list[str] = []
    for item in value:
        raw = item.get("alias", "") if isinstance(item, dict) else item
        alias = str(raw or "").strip()
        if alias.startswith("d:"):
            alias = alias[2:]
        if alias.startswith("Section_") and len(alias) > len("Section_"):
            aliases.append(f"d:{alias}")
    return tuple(dict.fromkeys(aliases))


def _read_oneroster_course_lookup_spool(
    path: Path,
    requested_aliases: Sequence[str],
) -> List[CourseDetail]:
    """Map sparse exact-set results by returned alias, never by row position."""

    requested_by_fold = {
        alias.casefold(): alias
        for alias in requested_aliases
    }
    if len(requested_by_fold) != len(requested_aliases):
        raise ValueError("managed Classroom aliases must be unique")
    courses_by_alias: dict[str, CourseDetail] = {}
    for record in iter_records_file(path):
        detail = _without_course_raw(CourseDetail.from_json(record), ())
        if not detail.id:
            continue
        matches = [
            requested_by_fold[alias.casefold()]
            for alias in _read_oneroster_course_aliases(record)
            if alias.casefold() in requested_by_fold
        ]
        if len(matches) != 1:
            raise ValueError(
                "Exact managed course lookup returned an ambiguous alias result."
            )
        matched = matches[0]
        key = matched.casefold()
        if key in courses_by_alias:
            raise ValueError(
                "Exact managed course lookup returned a duplicate alias result."
            )
        courses_by_alias[key] = _without_course_raw(detail, (matched,))
    return [
        courses_by_alias[alias.casefold()]
        for alias in requested_aliases
        if alias.casefold() in courses_by_alias
    ]


def _read_oneroster_course_snapshot_spool(
    path: Path,
    requested_aliases: Sequence[str],
) -> List[CourseDetail]:
    """Filter one tenant inventory while rejecting ambiguous managed identities."""

    requested_by_fold = {
        alias.casefold(): alias
        for alias in requested_aliases
    }
    if len(requested_by_fold) != len(requested_aliases):
        raise ValueError("managed Classroom aliases must be unique")

    courses_by_alias: dict[str, CourseDetail] = {}
    aliases_by_course_id: dict[str, str] = {}
    for record in iter_records_file(path):
        detail = _without_course_raw(CourseDetail.from_json(record), ())
        matches = [
            requested_by_fold[alias.casefold()]
            for alias in _read_oneroster_course_aliases(record)
            if alias.casefold() in requested_by_fold
        ]
        if not matches:
            continue
        if not detail.id:
            raise ValueError(
                "Managed course inventory returned a requested alias without a course ID."
            )
        if len(matches) != 1:
            raise ValueError(
                "Managed course inventory maps multiple requested aliases to one course."
            )

        matched = matches[0]
        alias_key = matched.casefold()
        if alias_key in courses_by_alias:
            raise ValueError(
                "Managed course inventory returned a duplicate or ambiguous alias."
            )
        previous_alias = aliases_by_course_id.get(detail.id)
        if previous_alias is not None:
            raise ValueError(
                "Managed course inventory returned a duplicate or ambiguous course ID."
            )
        aliases_by_course_id[detail.id] = alias_key
        courses_by_alias[alias_key] = _without_course_raw(detail, (matched,))

    return [
        courses_by_alias[alias.casefold()]
        for alias in requested_aliases
        if alias.casefold() in courses_by_alias
    ]


def _managed_course_alias(value: object) -> str:
    alias = str(value or "").strip()
    if alias.startswith("d:"):
        alias = alias[2:]
    if (
        not alias.startswith("Section_")
        or len(alias) == len("Section_")
        or len(alias) > 512
        or any(character in alias for character in "\r\n\x00")
    ):
        raise ValueError("aliases contain an invalid managed Classroom alias")
    return f"d:{alias}"


def _write_private_selector(
    values: Sequence[str],
    *,
    prefix: str,
    spool_dir: Optional[Path] = None,
) -> Path:
    fd, raw_path = tempfile.mkstemp(
        prefix=prefix,
        suffix=".txt",
        dir=str(spool_dir or private_runtime_spool_dir()),
    )
    path = Path(raw_path)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            fd = -1
            for value in values:
                stream.write(value)
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        return path
    except BaseException:
        if fd >= 0:
            os.close(fd)
        _remove_private_batch(path)
        raise


def _write_private_classroom_batch(
    commands: ClassroomBatchCommandSource,
    *,
    spool_dir: Path,
) -> tuple[Path, int]:
    """Validate and serialize a command source exactly once without retaining it."""

    fd, raw_path = tempfile.mkstemp(
        prefix="gamgui-oneroster-native-batch-",
        suffix=".txt",
        dir=str(spool_dir),
    )
    path = Path(raw_path)
    submitted_count = 0
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            fd = -1

            def emit(command: Sequence[str]) -> None:
                nonlocal submitted_count
                selected = tuple(str(argument) for argument in command)
                if not _is_allowed_classroom_batch_command(selected):
                    raise ValueError(
                        "Classroom batch contains a command outside the OneRoster allowlist."
                    )
                stream.write(GAMCommands.batch_line(selected))
                stream.write("\n")
                submitted_count += 1

            if callable(commands):
                commands(emit)
            else:
                for command in commands:
                    emit(command)
            if submitted_count == 0:
                raise ValueError("Classroom phase batch must contain at least one command.")
            stream.flush()
            os.fsync(stream.fileno())
        return path, submitted_count
    except BaseException:
        if fd >= 0:
            os.close(fd)
        _remove_private_batch(path)
        raise


def _native_batch_progress(
    line: str,
    submitted_count: int,
) -> Optional[NativeClassroomBatchProgress]:
    match = _NATIVE_BATCH_PROGRESS_PATTERN.search(line)
    if match is None:
        return None
    dispatched = int(match.group(1))
    total = int(match.group(2))
    if total != submitted_count or dispatched < 1 or dispatched > total:
        return None
    return NativeClassroomBatchProgress(
        dispatched=dispatched,
        total=total,
        raw_line=line,
    )


def _participant_role(record: dict, parsed: CourseParticipant) -> str:
    value = parsed.role
    if not value:
        for key in ("participantRole", "courseRole", "userRole", "type", "Type"):
            if record.get(key) not in (None, ""):
                value = str(record[key])
                break
    normalized = str(value or "").strip().casefold()
    if normalized in {"teacher", "teachers"}:
        return "teachers"
    if normalized in {"student", "students"}:
        return "students"
    return ""


def _participant_array(record: dict, key: str) -> List[dict]:
    """Decode one GAM ``formatjson`` roster cell without retaining the source row."""
    value = record.get(key)
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError(
                f"OneRoster roster snapshot contains invalid {key} data."
            ) from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"OneRoster roster snapshot contains invalid {key} data.")
    return value


def _bounded_oneroster_participant(
    record: dict,
    course_id: str,
    role: str,
) -> CourseParticipant:
    parsed = CourseParticipant.from_json(
        {**record, "courseId": course_id},
        role=role,
    )
    if not parsed.email:
        raise ValueError("OneRoster roster snapshot contains a participant without an email.")
    return CourseParticipant(
        course_id=course_id,
        email=parsed.email,
        user_id=parsed.user_id,
        role=role,
        full_name=parsed.full_name,
    )


def _read_oneroster_participants_spool(
    path: Path,
    course_ids: Set[str],
    role: str,
) -> CourseRosterSnapshot:
    grouped: dict[str, Tuple[Set[str], Set[str]]] = {}
    seen_course_ids: Set[str] = set()
    member_records = 0

    def retain(record: dict, course_id: str, parsed_role: str) -> None:
        nonlocal member_records
        member_records += 1
        if member_records > MAX_ONEROSTER_BULK_ROSTER_MEMBERS:
            raise ValueError(
                "OneRoster roster snapshot exceeds the safe membership limit."
            )
        participant = _bounded_oneroster_participant(
            record,
            course_id,
            parsed_role,
        )
        interned_email = sys.intern(participant.email)
        teachers, students = grouped.setdefault(
            course_id,
            (set(), set()),
        )
        if parsed_role == "teachers":
            teachers.add(interned_email)
        else:
            students.add(interned_email)

    for record in iter_records_file(path):
        course_id = sys.intern(
            str(
                record.get("courseId")
                or record.get("courseID")
                or record.get("Course ID")
                or ""
            ).strip()
        )
        if not course_id:
            raise ValueError("OneRoster roster snapshot contains a participant without a course ID.")
        if course_id not in course_ids:
            continue
        seen_course_ids.add(course_id)
        grouped.setdefault(course_id, (set(), set()))

        # GAM's `print course-participants ... formatjson` output is a streaming CSV
        # with one course per row and JSON arrays in these two columns. Keep support
        # for flat records as a compatibility boundary for older/mock GAM versions.
        nested = "JSON-teachers" in record or "JSON-students" in record
        if nested:
            for parsed_role, key in (
                ("teachers", "JSON-teachers"),
                ("students", "JSON-students"),
            ):
                if role != "all" and parsed_role != role:
                    continue
                for member in _participant_array(record, key):
                    retain(member, course_id, parsed_role)
            continue

        parsed = CourseParticipant.from_json(record)
        parsed_role = _participant_role(record, parsed)
        if not parsed_role:
            raise ValueError("OneRoster roster snapshot contains a participant without a valid role.")
        if role != "all" and parsed_role != role:
            continue
        retain(record, course_id, parsed_role)
    missing = course_ids - seen_course_ids
    if missing:
        raise ValueError(
            "OneRoster roster snapshot omitted one or more requested courses."
        )
    return CourseRosterSnapshot(
        rosters={
            course_id: (frozenset(teachers), frozenset(students))
            for course_id, (teachers, students) in grouped.items()
        },
        seen_course_ids=frozenset(seen_course_ids),
    )


class GAMConnector(Connector):
    id = ConnectorID.GOOGLE_WORKSPACE
    capabilities = {
        Capability.DIRECTORY,
        Capability.GROUPS,
        Capability.MAIL,
        Capability.CLASSROOM,
        Capability.DRIVE,
    }

    def __init__(self, runner: GAMRunner, domain: str, audit: Optional[AuditLog] = None) -> None:
        self.runner = runner
        self.domain = domain
        self.audit = audit or AuditLog()

    # --- connection --------------------------------------------------------------------
    async def test(self) -> ConnectionStatus:
        version = ""
        try:
            version = await self.runner.version()
        except Exception as exc:  # binary missing, etc.
            return ConnectionStatus(ok=False, detail=str(exc))
        if not self.runner.vault.has_credentials(self.domain):
            return ConnectionStatus(ok=False, detail="Not configured — complete setup.", version=version)
        return ConnectionStatus(ok=True, detail="Ready.", version=version)

    # --- reads -------------------------------------------------------------------------
    async def list_users(
        self,
        search: str = "",
        include_suspended: bool = True,
        fields: Optional[Sequence[str]] = None,
    ) -> List[GAMUser]:
        query = build_user_query(search, include_suspended)
        argv = GAMCommands.print_users(query=query, fields=fields)
        stdout = await self.runner.run_authenticated(self.domain, argv)
        return [GAMUser.from_json(r) for r in parse_records(stdout)]

    async def list_oneroster_directory(self) -> dict[str, GAMUser]:
        """Return one alias-resolvable Directory snapshot from one private GAM spool."""
        argv = GAMCommands.print_oneroster_directory()
        async with self.runner.run_authenticated_to_file(self.domain, argv) as result:
            return await _parse_private_spool_off_loop(
                _read_oneroster_directory_spool,
                result.path,
            )

    async def get_user(self, email: str, fields: Optional[Sequence[str]] = None) -> GAMUser:
        argv = GAMCommands.info_user(email, fields=fields)
        stdout = await self.runner.run_authenticated(self.domain, argv)
        return GAMUser.from_json(parse_one(stdout))

    async def refresh_directory_users(self, index: DirectoryIndex) -> int:
        """Spool a full user export and atomically replace the local summary index."""
        from ..gam.commands import DIRECTORY_INDEX_FIELDS

        if index.domain != self.domain.strip().lower():
            raise ValueError("directory index domain does not match connector domain")
        argv = GAMCommands.print_users(fields=DIRECTORY_INDEX_FIELDS)
        async with self.runner.run_authenticated_to_file(self.domain, argv) as result:
            return await asyncio.to_thread(
                index.replace_users,
                (GAMUser.from_json(record) for record in iter_records_file(result.path)),
            )

    async def list_groups(self) -> List[GAMGroup]:
        argv = GAMCommands.print_groups()
        stdout = await self.runner.run_authenticated(self.domain, argv)
        return [GAMGroup.from_json(r) for r in parse_records(stdout)]

    async def refresh_directory_groups(self, index: DirectoryIndex) -> int:
        """Spool a full group export and atomically replace the local summary index."""
        if index.domain != self.domain.strip().lower():
            raise ValueError("directory index domain does not match connector domain")
        argv = GAMCommands.print_groups()
        async with self.runner.run_authenticated_to_file(self.domain, argv) as result:
            return await asyncio.to_thread(
                index.replace_groups,
                (GAMGroup.from_json(record) for record in iter_records_file(result.path)),
            )

    async def list_group_members(self, group: str) -> List[GroupMember]:
        """Return all members for non-UI workflows through a private streamed spool."""

        argv = GAMCommands.print_group_members(group, fields=_GROUP_MEMBER_FIELDS)
        async with self.runner.run_authenticated_to_file(self.domain, argv) as result:
            members, _ = await asyncio.to_thread(
                _read_group_member_spool,
                result.path,
                None,
            )
        return members

    async def list_group_members_page(
        self,
        group: str,
        limit: int = _GROUP_MEMBER_PAGE_SIZE,
    ) -> Page[GroupMember]:
        """Return only the first 50 members while counting the streamed source.

        The entire GAM response goes to the runner's owner-only temporary spool, and parsing runs
        off the event loop. Only bounded summaries survive cleanup or reach the template.
        """

        cap = max(1, min(int(limit), _GROUP_MEMBER_PAGE_SIZE))
        argv = GAMCommands.print_group_members(group, fields=_GROUP_MEMBER_FIELDS)
        async with self.runner.run_authenticated_to_file(self.domain, argv) as result:
            members, total = await asyncio.to_thread(
                _read_group_member_spool,
                result.path,
                cap,
            )
        return Page(
            items=members,
            next_cursor=None,
            total=total,
            snapshot_age_seconds=None,
            refreshing=False,
        )

    async def list_signature_scope_users(
        self,
        scope_type: str,
        scope_value: str = "",
    ) -> List[GAMUser]:
        """Resolve a non-single-user signature scope without buffering GAM stdout.

        Exact users intentionally use :meth:`get_user` in the route. Tenant-scale scopes spool only
        the fields the signature renderer consumes and perform all parsing/filtering in a worker
        thread. Group scope first reduces a separately spooled membership export to email keys.
        """

        scope = (scope_type or "").strip().lower()
        value = (scope_value or "").strip()
        if scope not in _SIGNATURE_LARGE_SCOPES:
            raise ValueError("unsupported signature scope")
        if scope != "company" and not value:
            raise ValueError("signature scope value is required")

        group_emails: Optional[Set[str]] = None
        if scope == "group":
            member_argv = GAMCommands.print_group_members(value, fields=("email",))
            async with self.runner.run_authenticated_to_file(
                self.domain,
                member_argv,
            ) as member_result:
                group_emails = await asyncio.to_thread(
                    _read_group_member_emails,
                    member_result.path,
                )

        user_argv = GAMCommands.print_users(fields=SIGNATURE_USER_FIELDS)
        async with self.runner.run_authenticated_to_file(
            self.domain,
            user_argv,
        ) as user_result:
            return await asyncio.to_thread(
                _read_signature_user_spool,
                user_result.path,
                scope,
                value,
                group_emails,
            )

    async def list_delegates(self, email: str) -> List[str]:
        """Return the email addresses delegated access to ``email``'s mailbox."""
        stdout = await self.runner.run_authenticated(self.domain, GAMCommands.print_delegates(email))
        out: List[str] = []
        for rec in parse_records(stdout):
            addr = rec.get("delegateAddress") or rec.get("delegate") or rec.get("Delegate Address")
            if addr:
                out.append(str(addr))
        return out

    # --- Classroom --------------------------------------------------------------------
    async def list_courses(
        self,
        states: Optional[Sequence[str]] = None,
        teacher: str = "",
        student: str = "",
        fields: Optional[Sequence[str]] = None,
    ) -> List[CourseSummary]:
        argv = GAMCommands.print_courses(
            states=states, teacher=teacher, student=student, fields=fields
        )
        stdout = await self.runner.run_authenticated(self.domain, argv)
        return [CourseDetail.from_json(record) for record in parse_records(stdout)]

    async def list_oneroster_managed_courses(
        self,
        aliases: Sequence[str],
    ) -> List[CourseDetail]:
        """Resolve an exact managed-alias set through bounded GAM selector shards."""

        selected: list[str] = []
        seen: set[str] = set()
        for raw in aliases:
            alias = _managed_course_alias(raw)
            key = alias.casefold()
            if key not in seen:
                seen.add(key)
                selected.append(alias)
        if not selected:
            return []

        chunks = [
            selected[index:index + ONEROSTER_COURSE_SELECTOR_CHUNK_SIZE]
            for index in range(0, len(selected), ONEROSTER_COURSE_SELECTOR_CHUNK_SIZE)
        ]

        async def read_shard(shard: Sequence[str]) -> List[CourseDetail]:
            selector_path = _write_private_selector(
                shard,
                prefix="gamgui-oneroster-course-selector-",
                spool_dir=private_runtime_spool_dir(
                    getattr(self.runner, "base_dir", None)
                ),
            )
            operation_succeeded = False
            try:
                timeout = min(
                    21600.0,
                    max(
                        float(getattr(self.runner, "timeout", 120.0)),
                        0.5 * len(shard),
                    ),
                )
                for attempt in range(len(ONEROSTER_RATE_LIMIT_RETRY_DELAYS) + 1):
                    try:
                        async with self.runner.run_authenticated_to_file(
                            self.domain,
                            GAMCommands.print_oneroster_courses_file(
                                str(selector_path)
                            ),
                            timeout=timeout,
                            serialize=False,
                            accepted_error_kinds=(GAMErrorKind.NOT_FOUND,),
                        ) as result:
                            courses = await _parse_private_spool_off_loop(
                                _read_oneroster_course_lookup_spool,
                                result.path,
                                shard,
                            )
                        break
                    except GAMError as exc:
                        if (
                            exc.kind is not GAMErrorKind.RATE_LIMITED
                            or attempt >= len(ONEROSTER_RATE_LIMIT_RETRY_DELAYS)
                        ):
                            raise
                        await asyncio.sleep(
                            ONEROSTER_RATE_LIMIT_RETRY_DELAYS[attempt]
                        )
                operation_succeeded = True
                return courses
            finally:
                cleaned = await _await_private_batch_removal(selector_path)
                if not cleaned and operation_succeeded:
                    raise RuntimeError(
                        "Private Classroom selector could not be removed securely."
                    )

        worker_limit = asyncio.Semaphore(ONEROSTER_COURSE_SELECTOR_WORKERS)

        async def read_chunk(chunk: Sequence[str]) -> List[CourseDetail]:
            async with worker_limit:
                return await read_shard(chunk)

        tasks = [asyncio.create_task(read_chunk(chunk)) for chunk in chunks]
        try:
            shard_results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        courses = [course for result in shard_results for course in result]
        selected_order = {alias.casefold(): index for index, alias in enumerate(selected)}
        courses.sort(
            key=lambda course: selected_order[course.aliases[0].casefold()]
        )

        course_ids: set[str] = set()
        for course in courses:
            if course.id in course_ids:
                raise ValueError(
                    "More than one managed alias resolves to the same Classroom course."
                )
            course_ids.add(course.id)
        return courses

    async def snapshot_oneroster_managed_courses(
        self,
        aliases: Sequence[str],
    ) -> List[CourseDetail]:
        """Filter requested managed aliases from one tenant-wide course inventory."""

        selected: list[str] = []
        seen: set[str] = set()
        for raw in aliases:
            alias = _managed_course_alias(raw)
            key = alias.casefold()
            if key not in seen:
                seen.add(key)
                selected.append(alias)
        if not selected:
            return []

        async with self.runner.run_authenticated_to_file(
            self.domain,
            GAMCommands.print_oneroster_courses_snapshot(),
            timeout=ONEROSTER_NATIVE_BATCH_TIMEOUT,
            serialize=False,
        ) as result:
            return await _parse_private_spool_off_loop(
                _read_oneroster_course_snapshot_spool,
                result.path,
                selected,
            )

    async def refresh_course_index(self, index: CourseIndex) -> int:
        """Stream the tenant-wide cheap course projection into ``index`` off-loop."""

        argv = GAMCommands.print_courses()
        async with self.runner.run_authenticated_to_file(self.domain, argv) as result:
            cancelled = threading.Event()
            worker = asyncio.create_task(
                asyncio.to_thread(
                    _replace_course_index_from_spool,
                    index,
                    self.domain,
                    result.path,
                    cancelled,
                )
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError as cancellation:
                cancelled.set()
                # Keep the runner's private spool alive until the worker closes it and SQLite has
                # rolled back. Repeated cancellation must not race secure spool cleanup on Windows.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        cancelled.set()
                    except Exception:
                        # The result is consumed below after the worker has closed the spool.
                        break
                try:
                    worker.result()
                except _CourseRefreshCancelled:
                    pass
                except Exception:
                    # Cancellation remains the public result; retrieving the exception prevents an
                    # unobserved-task warning while the transactional index keeps its old snapshot.
                    pass
                raise cancellation

    async def get_course(
        self,
        course_id: str,
        *,
        include_owner_email: bool = False,
        include_aliases: bool = False,
        best_effort_enrichment: bool = False,
    ) -> CourseDetail:
        attempts = [(include_owner_email, include_aliases)]
        if best_effort_enrichment:
            # A deleted legacy owner can make ``owneremail`` fail even though the
            # Classroom course itself is valid. Preserve aliases when possible,
            # then fall back to the core course resource.
            if include_owner_email:
                attempts.append((False, include_aliases))
            attempts.append((False, False))

        deduplicated = list(dict.fromkeys(attempts))
        last_detail = CourseDetail.from_json({})
        for index, (with_owner_email, with_aliases) in enumerate(deduplicated):
            try:
                stdout = await self.runner.run_authenticated(
                    self.domain,
                    GAMCommands.info_course(
                        course_id,
                        include_owner_email=with_owner_email,
                        include_aliases=with_aliases,
                    ),
                )
            except GAMError:
                if index == len(deduplicated) - 1:
                    raise
                continue
            last_detail = CourseDetail.from_json(parse_one(stdout))
            if last_detail.id or index == len(deduplicated) - 1:
                return last_detail
        return last_detail

    async def list_course_participants(
        self, course_id: str, role: str
    ) -> List[CourseParticipant]:
        stdout = await self.runner.run_authenticated(
            self.domain, GAMCommands.print_course_participants(course_id, role)
        )
        participants: List[CourseParticipant] = []
        for record in parse_records(stdout):
            nested = "JSON-teachers" in record or "JSON-students" in record
            if nested:
                normalized_role = str(role or "").strip().casefold()
                selected_roles = (
                    ("teachers", "students")
                    if normalized_role == "all"
                    else (normalized_role,)
                )
                for selected_role in selected_roles:
                    for member in _participant_array(
                        record, f"JSON-{selected_role}"
                    ):
                        participants.append(
                            CourseParticipant.from_json(
                                {**member, "courseId": course_id},
                                role=selected_role,
                            )
                        )
                continue
            parsed = CourseParticipant.from_json(
                {**record, "courseId": record.get("courseId") or course_id},
                role=role,
            )
            if (
                not parsed.email
                and not parsed.user_id
                and not isinstance(record.get("profile"), dict)
            ):
                # GAM may emit the course itself as the only row. Its `name`
                # is the course title, not a participant display name.
                parsed = CourseParticipant(
                    course_id=parsed.course_id or course_id,
                    email="",
                    user_id="",
                    role=parsed.role,
                    full_name="",
                    raw=dict(record),
                )
            participants.append(parsed)
        return participants

    async def list_course_participants_many(
        self,
        course_ids: Sequence[str],
        role: str = "all",
    ) -> CourseRosterSnapshot:
        """Read an exact course-roster set in one streamed GAM process."""

        selected = list(
            dict.fromkeys(
                str(course_id).strip()
                for course_id in course_ids
                if str(course_id).strip()
            )
        )
        normalized_role = str(role or "").strip().casefold()
        if normalized_role not in {"all", "teachers", "students"}:
            raise ValueError("invalid Classroom roster role")
        if not selected:
            return CourseRosterSnapshot.empty()
        if any(
            len(course_id) > 512
            or any(character in course_id for character in "\r\n\x00")
            for course_id in selected
        ):
            raise ValueError("course_ids contain an invalid Classroom course reference")

        selector_path = _write_private_selector(
            selected,
            prefix="gamgui-oneroster-roster-selector-",
            spool_dir=private_runtime_spool_dir(
                getattr(self.runner, "base_dir", None)
            ),
        )
        operation_succeeded = False
        try:
            timeout = min(
                21600.0,
                max(
                    float(getattr(self.runner, "timeout", 120.0)),
                    2.0 * len(selected),
                ),
            )
            async with self.runner.run_authenticated_to_file(
                self.domain,
                GAMCommands.print_course_participants_file(
                    str(selector_path),
                    normalized_role,
                ),
                timeout=timeout,
                serialize=True,
            ) as result:
                participants = await _parse_private_spool_off_loop(
                    _read_oneroster_participants_spool,
                    result.path,
                    set(selected),
                    normalized_role,
                )
            operation_succeeded = True
        finally:
            cleaned = await _await_private_batch_removal(selector_path)
            if not cleaned and operation_succeeded:
                raise RuntimeError(
                    "Private Classroom selector could not be removed securely."
                )
        return participants

    async def list_course_participants_many_bounded(
        self,
        course_ids: Sequence[str],
        role: str = "all",
    ) -> List[CourseParticipant]:
        """Retain the older exact-course subprocess behavior for non-OneRoster callers."""
        selected = [
            str(course_id).strip()
            for course_id in course_ids
            if str(course_id).strip()
        ]
        normalized_role = str(role or "").strip().casefold()
        roles = ("teachers", "students") if normalized_role == "all" else (normalized_role,)
        participants: List[CourseParticipant] = []
        for offset in range(0, len(selected), 50):
            chunk = selected[offset : offset + 50]
            for selected_role in roles:
                stdout = await self.runner.run_authenticated(
                    self.domain,
                    GAMCommands.print_course_participants_many(chunk, selected_role),
                )
                participants.extend(
                    CourseParticipant.from_json(record, role=selected_role)
                    for record in parse_records(stdout)
                )
        return participants

    async def run_classroom_batch(
        self,
        commands: Sequence[Sequence[str]],
        *,
        max_commands: int = 50,
        worker_count: int = 5,
    ) -> BatchExecutionReceipt:
        """Execute one bounded, allowlisted OneRoster mutation batch.

        The batch exists only as an owner-readable temporary file. GAM's opaque
        ``clear`` and ``sync`` operations are deliberately not accepted.
        Callers must verify every requested result against live Classroom state.
        """
        cap = max(1, min(int(max_commands), 50))
        selected = [list(command) for command in commands]
        if not selected or len(selected) > cap:
            raise ValueError(f"Classroom batch must contain between 1 and {cap} commands.")
        if not all(_is_allowed_classroom_batch_command(command) for command in selected):
            raise ValueError("Classroom batch contains a non-allowlisted GAM mutation.")
        workers = int(worker_count)
        if workers not in {3, 5, 8, 10}:
            raise ValueError("Classroom batch worker count must be 3, 5, 8, or 10.")

        fd, raw_path = tempfile.mkstemp(
            prefix="gamgui-oneroster-",
            suffix=".batch",
            dir=str(
                private_runtime_spool_dir(
                    getattr(self.runner, "base_dir", None)
                )
            ),
        )
        batch_path = Path(raw_path)
        operation_succeeded = False
        started = time.perf_counter()
        try:
            os.chmod(batch_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                for command in selected:
                    stream.write(GAMCommands.batch_line(command))
                    stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            fd = -1
            timeout = min(1800.0, max(float(self.runner.timeout), 30.0 * len(selected)))
            run_authenticated = self.runner.run_authenticated
            try:
                parameters = inspect.signature(run_authenticated).parameters.values()
                supports_threads = any(
                    parameter.name == "gam_threads"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )
            except (TypeError, ValueError):
                supports_threads = False
            kwargs = {"timeout": timeout, "serialize": True}
            if supports_threads:
                kwargs["gam_threads"] = workers
            await run_authenticated(
                self.domain,
                GAMCommands.batch_file(str(batch_path), show_commands=False),
                **kwargs,
            )
            receipt = BatchExecutionReceipt(
                duration_seconds=time.perf_counter() - started,
                worker_count=workers,
                outcome="completed",
            )
            self.audit.record(
                "oneroster_classroom_batch",
                target=f"{len(selected)} actions",
                argv=GAMCommands.batch_file("<private-batch>", show_commands=False),
                ok=True,
                extra={
                    "duration_seconds": round(receipt.duration_seconds, 3),
                    "worker_count": receipt.worker_count,
                    "outcome": receipt.outcome,
                    "retry_count": receipt.retry_count,
                    "throttling_count": receipt.throttling_count,
                },
            )
            operation_succeeded = True
        except Exception as exc:
            error_code = str(getattr(exc, "error_code", "GAM-BATCH-FAILED"))
            throttling_count = int(
                isinstance(exc, GAMError) and exc.kind is GAMErrorKind.RATE_LIMITED
            )
            self.audit.record(
                "oneroster_classroom_batch",
                target=f"{len(selected)} actions",
                argv=GAMCommands.batch_file("<private-batch>", show_commands=False),
                ok=False,
                extra={
                    "duration_seconds": round(time.perf_counter() - started, 3),
                    "worker_count": workers,
                    "outcome": "failed",
                    "retry_count": 0,
                    "throttling_count": throttling_count,
                    "error_code": error_code,
                },
            )
            raise
        finally:
            if fd >= 0:
                os.close(fd)
            cleaned = await _await_private_batch_removal(batch_path)
            if not cleaned and operation_succeeded:
                raise RuntimeError(
                    "Private Classroom batch could not be removed securely."
                )
        return receipt

    async def run_classroom_phase_batch(
        self,
        commands: ClassroomBatchCommandSource,
        *,
        timeout: float = ONEROSTER_NATIVE_BATCH_TIMEOUT,
        progress_callback: Optional[NativeClassroomBatchProgressCallback] = None,
    ) -> NativeClassroomBatchReceipt:
        """Execute one entire allowlisted OneRoster phase in one native GAM batch."""

        command_timeout = float(timeout)
        if not math.isfinite(command_timeout) or command_timeout <= 0:
            raise ValueError("Classroom phase timeout must be a positive finite value.")

        batch_path: Optional[Path] = None
        submitted_count = 0
        progress_count = 0
        operation_succeeded = False
        started = time.perf_counter()
        try:
            batch_path, submitted_count = await asyncio.to_thread(
                _write_private_classroom_batch,
                commands,
                spool_dir=private_runtime_spool_dir(
                    getattr(self.runner, "base_dir", None)
                ),
            )

            async def observe_progress(_stream_name: str, line: str) -> None:
                nonlocal progress_count
                progress = _native_batch_progress(line, submitted_count)
                if progress is None or progress.dispatched <= progress_count:
                    return
                progress_count = progress.dispatched
                if progress_callback is not None:
                    callback_result = progress_callback(progress)
                    if inspect.isawaitable(callback_result):
                        await callback_result

            process_result = await self.runner.run_authenticated_streaming(
                self.domain,
                GAMCommands.batch_file(str(batch_path), show_commands=False),
                timeout=command_timeout,
                serialize=True,
                gam_threads=ONEROSTER_NATIVE_BATCH_WORKERS,
                line_callback=observe_progress,
            )
            receipt = NativeClassroomBatchReceipt(
                submitted_count=submitted_count,
                process_result=process_result,
                progress_count=progress_count,
            )
            self.audit.record(
                "oneroster_classroom_phase_batch",
                target=f"{submitted_count} actions",
                argv=GAMCommands.batch_file(
                    "<private-batch>",
                    show_commands=False,
                ),
                ok=True,
                extra={
                    "duration_seconds": round(receipt.duration_seconds, 3),
                    "worker_count": receipt.worker_count,
                    "outcome": receipt.outcome,
                    "submitted_count": receipt.submitted_count,
                    "progress_count": receipt.progress_count,
                },
            )
            operation_succeeded = True
            return receipt
        except GAMError as exc:
            wrapped = NativeClassroomBatchError(
                exc,
                submitted_count=submitted_count,
            )
            self.audit.record(
                "oneroster_classroom_phase_batch",
                target=f"{submitted_count} actions",
                argv=GAMCommands.batch_file(
                    "<private-batch>",
                    show_commands=False,
                ),
                ok=False,
                extra={
                    "duration_seconds": round(time.perf_counter() - started, 3),
                    "worker_count": ONEROSTER_NATIVE_BATCH_WORKERS,
                    "outcome": "failed",
                    "submitted_count": submitted_count,
                    "progress_count": progress_count,
                    "error_code": wrapped.error_code,
                    "gam_error_kind": wrapped.kind.value,
                },
            )
            raise wrapped from exc
        finally:
            if batch_path is not None:
                cleaned = await _await_private_batch_removal(batch_path)
                if not cleaned and operation_succeeded:
                    raise RuntimeError(
                        "Private Classroom phase batch could not be removed securely."
                    )

    async def create_course(
        self,
        *,
        name: str,
        owner_email: str,
        alias: str = "",
        section: str = "",
        room: str = "",
        description_heading: str = "",
        description: str = "",
        state: str = "PROVISIONED",
    ) -> ChangeResult:
        argv = GAMCommands.create_course(
            name,
            owner_email,
            alias=alias,
            section=section,
            room=room,
            description_heading=description_heading,
            description=description,
            state=state,
        )
        return await self._run_write("create_course", owner_email, argv, RiskLevel.LOW)

    async def update_course(
        self,
        course_id: str,
        *,
        name: str,
        section: str = "",
        room: str = "",
        description_heading: str = "",
        description: str = "",
    ) -> ChangeResult:
        argv = GAMCommands.update_course(
            course_id,
            name=name,
            section=section,
            room=room,
            description_heading=description_heading,
            description=description,
        )
        return await self._run_write("update_course", course_id, argv, RiskLevel.LOW)

    async def update_course_state(self, course_id: str, state: str) -> ChangeResult:
        risk = RiskLevel.DESTRUCTIVE if state.strip().upper() == "ARCHIVED" else RiskLevel.LOW
        return await self._run_write(
            "update_course_state",
            course_id,
            GAMCommands.update_course_state(course_id, state),
            risk,
        )

    async def transfer_course_owner(
        self, course_id: str, target_email: str
    ) -> ChangeResult:
        return await self._run_write(
            "transfer_course_owner",
            course_id,
            GAMCommands.transfer_course_owner(course_id, target_email),
            RiskLevel.DESTRUCTIVE,
        )

    async def add_course_participant(
        self, course_id: str, role: str, email: str
    ) -> ChangeResult:
        return await self._run_write(
            "add_course_participant",
            email,
            GAMCommands.add_course_participant(course_id, role, email),
            RiskLevel.LOW,
        )

    async def remove_course_participant(
        self, course_id: str, role: str, email: str
    ) -> ChangeResult:
        return await self._run_write(
            "remove_course_participant",
            email,
            GAMCommands.remove_course_participant(course_id, role, email),
            RiskLevel.DESTRUCTIVE,
        )

    async def resolve(self, person: Person) -> Optional[ConnectorAccount]:
        try:
            user = await self.get_user(person.primary_email)
        except Exception:
            return None
        return ConnectorAccount(connector_id=self.id, native_id=user.primary_email, raw=user.raw)

    # --- low-risk mutations (run directly) ---------------------------------------------
    async def set_signature(self, email: str, signature: str, html: bool = True) -> ChangeResult:
        argv = GAMCommands.set_signature(email, signature, html=html)
        return await self._run_write("set_signature", email, argv, RiskLevel.LOW)

    async def get_signature(self, email: str) -> str:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.show_signature(email))
        return _parse_signature(out)

    async def list_user_groups(self, email: str) -> List[str]:
        """Group emails that ``email`` is a member of."""
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_groups_member(email))
        return [str(r.get("email")) for r in parse_records(out) if r.get("email") and "@" in str(r.get("email"))]

    async def usage_report(
        self,
        params: Sequence[str],
        max_lookback: int = 6,
        limit: int = 25,
    ) -> dict:
        """Return a bounded usage leaderboard from a private streamed GAM spool.

        Usage data lags roughly 2–3 days, so walk backward to the first date with data. GAM still
        produces a tenant report, but stdout is never buffered in the process and CSV reduction is
        performed off the event loop while the runner owns secure spool cleanup.
        """

        today = datetime.now(timezone.utc).date()
        for back in range(2, 2 + max_lookback):
            date = (today - timedelta(days=back)).isoformat()
            async with self.runner.run_authenticated_to_file(
                self.domain,
                GAMCommands.report_users(date, params),
            ) as result:
                rows = await asyncio.to_thread(_bounded_usage_rows, result.path, limit)
            if rows:
                return {"date": date, "rows": rows}
        return {"date": "", "rows": []}

    async def add_delegate(self, email: str, delegate: str) -> ChangeResult:
        argv = GAMCommands.add_delegate(email, delegate)
        return await self._run_write("add_delegate", email, argv, RiskLevel.LOW)

    async def remove_delegate(self, email: str, delegate: str) -> ChangeResult:
        argv = GAMCommands.remove_delegate(email, delegate)
        return await self._run_write("remove_delegate", email, argv, RiskLevel.LOW)

    async def signout_user(self, email: str) -> ChangeResult:
        return await self._run_write("signout_user", email, GAMCommands.signout_user(email), RiskLevel.LOW)

    # --- vacation / auto-responder -----------------------------------------------------
    async def get_vacation(self, email: str) -> Vacation:
        stdout = await self.runner.run_authenticated(self.domain, GAMCommands.show_vacation(email))
        return Vacation.from_show_text(stdout)

    async def set_vacation(
        self,
        email: str,
        subject: str,
        message: str,
        html: bool = True,
        start: Optional[str] = None,
        end: Optional[str] = None,
        contacts_only: bool = False,
        domain_only: bool = False,
    ) -> ChangeResult:
        argv = GAMCommands.set_vacation(
            email, subject, message, html=html, start=start, end=end,
            contacts_only=contacts_only, domain_only=domain_only,
        )
        return await self._run_write("set_vacation", email, argv, RiskLevel.LOW)

    async def clear_vacation(self, email: str) -> ChangeResult:
        return await self._run_write("clear_vacation", email, GAMCommands.vacation_off(email), RiskLevel.LOW)

    async def add_group_member(self, group: str, member: str, role: str = "member") -> ChangeResult:
        argv = GAMCommands.add_group_member(group, member, role=role)
        return await self._run_write("add_group_member", member, argv, RiskLevel.LOW, target_extra=group)

    async def remove_group_member(self, group: str, member: str) -> ChangeResult:
        argv = GAMCommands.remove_group_member(group, member)
        return await self._run_write("remove_group_member", member, argv, RiskLevel.LOW, target_extra=group)

    # --- directory profile (title = role, department = store) --------------------------
    async def set_organization(self, email: str, title: str = "", department: str = "") -> ChangeResult:
        argv = GAMCommands.update_organization(email, title=title, department=department)
        return await self._run_write("set_organization", email, argv, RiskLevel.LOW)

    # --- calendar access ---------------------------------------------------------------
    async def list_calendar_acls(self, email: str, calendar: str = "primary") -> List[CalendarACL]:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_calendar_acls(email, calendar))
        return [CalendarACL.from_json(r) for r in parse_records(out)]

    async def add_calendar_acl(self, email: str, target: str, role: str = "reader", calendar: str = "primary") -> ChangeResult:
        argv = GAMCommands.add_calendar_acl(email, target, role=role, calendar=calendar)
        return await self._run_write("add_calendar_acl", email, argv, RiskLevel.LOW, target_extra=target)

    async def remove_calendar_acl(self, email: str, scope: str, calendar: str = "primary") -> ChangeResult:
        argv = GAMCommands.delete_calendar_acl(email, scope, calendar=calendar)
        return await self._run_write("remove_calendar_acl", email, argv, RiskLevel.LOW, target_extra=scope)

    # --- calendars / resources / events ------------------------------------------------
    async def list_resources(self, query: str = "") -> List[ResourceCalendar]:
        # GAM's resource `query` is a structured filter — freeform text like "House Call Calendar"
        # fails with "Invalid Input: filter". Rooms are a small set, so fetch all and match locally.
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_resources())
        items = [ResourceCalendar.from_json(r) for r in parse_records(out)]
        q = query.strip().lower()
        if q:
            items = [r for r in items
                     if q in (r.name or "").lower() or q in (r.email or "").lower()
                     or q in (r.resource_id or "").lower()]
        return items

    async def list_user_calendars(self, email: str) -> List[UserCalendar]:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_user_calendars(email))
        return [UserCalendar.from_json(r) for r in parse_records(out)]

    async def scan_all_calendars(self) -> List[IndexedCalendar]:
        """The full domain scan that backs the calendar index (slow; run in the background).

        Walks every user's calendar list (one ``all users print calendars`` call) plus the room
        calendars, keeping only the discoverable shared calendars: **secondary** calendars
        (``…@group.calendar.google.com``) and **rooms**. For each secondary calendar it records the
        owner (the user whose row has accessRole=owner) and a subscriber count (how many users carry
        it). Each user's primary, holiday/system calendars are skipped — they aren't shared calendars
        you'd search for, and excluding them keeps the index small on large tenants.
        """
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_all_calendars())
        agg: dict = {}
        for row in parse_records(out):
            cid = str(row.get("id") or "").strip()
            low = cid.lower()
            if not cid or "#" in low or not low.endswith("@group.calendar.google.com"):
                continue  # skip primaries (email ids), holiday/system, imports
            summary = str(row.get("summary") or "")
            role = str(row.get("accessRole") or row.get("accessrole") or "")
            who = str(row.get("primaryEmail") or row.get("User") or row.get("user") or "")
            e = agg.setdefault(cid, {"summary": summary, "owner": "", "subs": 0})
            if summary and not e["summary"]:
                e["summary"] = summary
            e["subs"] += 1
            if role == "owner" and who and not e["owner"]:
                e["owner"] = who
        cals = [IndexedCalendar(id=cid, summary=v["summary"], owner=v["owner"],
                                kind="secondary", subscribers=v["subs"]) for cid, v in agg.items()]
        # Room / resource calendars (one fast admin call). Isolated: a tenant without the Resource
        # Calendar API shouldn't waste the expensive user scan we just completed — index without rooms.
        try:
            for r in await self.list_resources(""):
                if r.email:
                    cals.append(IndexedCalendar(id=r.email, summary=r.name or r.email, owner="",
                                                kind="room", subscribers=0))
        except Exception:  # noqa: BLE001 — rooms are a bonus; keep the secondary-calendar index
            pass
        return cals

    async def list_calendar_acls_for(self, calendar_id: str) -> List[CalendarACL]:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_calendar_acls_cal(calendar_id))
        return [CalendarACL.from_json(r) for r in parse_records(out)]

    async def add_calendar_acl_for(self, calendar_id: str, scope: str, role: str = "reader") -> ChangeResult:
        argv = GAMCommands.add_calendar_acl_cal(calendar_id, scope, role=role)
        return await self._run_write("add_calendar_acl_cal", calendar_id, argv, RiskLevel.LOW, target_extra=scope)

    async def remove_calendar_acl_for(self, calendar_id: str, scope: str) -> ChangeResult:
        argv = GAMCommands.delete_calendar_acl_cal(calendar_id, scope)
        return await self._run_write("remove_calendar_acl_cal", calendar_id, argv, RiskLevel.LOW, target_extra=scope)

    async def subscribe_calendar_for(self, email: str, calendar_id: str) -> ChangeResult:
        argv = GAMCommands.subscribe_calendar(email, calendar_id)
        return await self._run_write("subscribe_calendar", email, argv, RiskLevel.LOW, target_extra=calendar_id)

    async def search_events(
        self, calendar_id: str, query: str = "", after: str = "", before: str = "", cap: int = 200
    ) -> List[CalendarEvent]:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.print_events(calendar_id, query, after, before))
        events = [CalendarEvent.from_json(r) for r in parse_records(out)]
        return events[:cap]

    async def get_event(self, calendar_id: str, event_id: str) -> Optional[CalendarEvent]:
        out = await self.runner.run_authenticated(self.domain, GAMCommands.get_event(calendar_id, event_id))
        recs = parse_records(out)
        return CalendarEvent.from_json(recs[0]) if recs else None

    async def delete_event(self, calendar_id: str, event_id: str) -> ChangeResult:
        argv = GAMCommands.delete_event(calendar_id, event_id, doit=True)
        return await self._run_write("delete_event", calendar_id, argv, RiskLevel.DESTRUCTIVE, target_extra=event_id)

    async def delete_calendar(self, owner: str, calendar_id: str) -> ChangeResult:
        """PERMANENTLY delete a secondary calendar (for everyone) by impersonating an owner.

        GAM verb is `remove calendars` (= Calendars.delete); `delete calendars` would only
        unsubscribe. Verified against GAM7 source. Irreversible — no GAM-side undo.
        """
        argv = GAMCommands.remove_calendar(owner, calendar_id)
        return await self._run_write("delete_calendar", calendar_id, argv, RiskLevel.DESTRUCTIVE, target_extra=owner)

    # --- lifecycle (offboarding) -------------------------------------------------------
    async def reset_password(self, email: str) -> ChangeResult:
        res = await self._run_write("reset_password", email, GAMCommands.reset_password(email), RiskLevel.LOW)
        if res.ok:  # best-effort: end existing sessions too
            try:
                await self.runner.run_authenticated(self.domain, GAMCommands.signout_user(email), serialize=True)
            except Exception:
                pass
        return res

    async def transfer_data(self, old_owner: str, service: str, new_owner: str) -> ChangeResult:
        argv = GAMCommands.create_datatransfer(old_owner, service, new_owner)
        return await self._run_write("transfer_data", old_owner, argv, RiskLevel.LOW, target_extra=new_owner)

    async def create_onboarding_runbook(self, assignee: str, title: str, steps: List[str]) -> dict:
        """Create a Google Tasks list on ``assignee`` with one task per step; return a summary.

        Additive/low-risk, serialized + audited. The tasklist id comes back via ``returnidonly``;
        each step then becomes a task on it. A step that fails is reported, not fatal."""
        out = await self.runner.run_authenticated(
            self.domain, GAMCommands.create_tasklist(assignee, title), serialize=True)
        lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        tasklist_id = lines[-1] if lines else ""
        created, failed = 0, []
        if tasklist_id:
            for step in steps:
                try:
                    await self.runner.run_authenticated(
                        self.domain, GAMCommands.create_task(assignee, tasklist_id, step), serialize=True)
                    created += 1
                except Exception:  # noqa: BLE001 — report per-step, keep going
                    failed.append(step)
        self.audit.record("onboard_runbook", target=assignee,
                          argv=GAMCommands.create_tasklist(assignee, title), ok=bool(tasklist_id),
                          extra={"title": title, "tasks": created, "failed": failed})
        return {"tasklist_id": tasklist_id, "created": created, "failed": failed, "total": len(steps)}

    async def send_welcome_email(self, to: str, subject: str, body: str) -> ChangeResult:
        return await self._run_write(
            "send_welcome_email", to, GAMCommands.send_email(to, subject, body), RiskLevel.LOW)

    async def remove_from_all_calendars(self, email: str) -> ChangeResult:
        # Best-effort sweep across every user. Expected, harmless per-entity outcomes: NOT_FOUND
        # (that user never shared with the departing user) and PERMISSION_DENIED / cannotChangeOwnAcl
        # (the departing user's OWN primary calendar — you can't delete your own owner ACL, and it's
        # going away with the account anyway). Only a real auth/scope failure should fail this step.
        argv = GAMCommands.remove_all_calendar_acls(email)
        return await self._run_write(
            "remove_from_all_calendars", email, argv, RiskLevel.LOW,
            tolerate_kinds=(GAMErrorKind.NOT_FOUND, GAMErrorKind.PERMISSION_DENIED),
        )

    async def incomplete_transfers_for(self, email: str) -> List[dict]:
        """Data transfers FROM ``email`` that haven't reached 'completed' yet.

        Deleting an account before its Drive/calendar transfer completes permanently loses the
        un-transferred data, so the delete flow checks this first. Returns [] on any read error
        (never blocks deletion on an inability to check — just can't warn)."""
        try:
            out = await self.runner.run_authenticated(self.domain, GAMCommands.print_datatransfers(email))
        except Exception:
            return []
        pending = []
        for r in parse_records(out):
            status = str(r.get("overallTransferStatusCode") or r.get("status") or "")
            if status and status.lower() != "completed":
                pending.append({"application": str(r.get("application") or "data"), "status": status})
        return pending

    async def add_calendar_event(
        self, calendar: str, summary: str, start: str, end: str, description: str = "", attendee: str = ""
    ) -> ChangeResult:
        argv = GAMCommands.add_calendar_event(calendar, summary, start, end, description=description, attendee=attendee)
        return await self._run_write("add_calendar_event", calendar, argv, RiskLevel.LOW)

    async def delete_user(self, email: str) -> ChangeResult:
        return await self._run_write("delete_user", email, GAMCommands.delete_user(email), RiskLevel.DESTRUCTIVE)

    # --- destructive: plan (dry-run) then apply ----------------------------------------
    def plan_suspend(self, emails: Sequence[str], suspend: bool = True) -> List[ChangePreview]:
        """Build dry-run previews for (un)suspending a concrete set of users.

        GAM has no universal ``--dry-run``; the caller resolves the target set first (e.g. by
        expanding a query into emails), and this turns each into a previewable change.
        """
        risk = RiskLevel.DESTRUCTIVE if suspend else RiskLevel.LOW
        verb = "Suspend" if suspend else "Unsuspend"
        return [
            ChangePreview(
                connector_id=self.id,
                target=email,
                summary=f"{verb} {email}",
                risk=risk,
                argv=GAMCommands.set_suspended(email, suspend),
            )
            for email in emails
        ]

    async def plan(self, action: LifecycleAction, person: Person) -> List[ChangePreview]:
        if action == LifecycleAction.SUSPEND:
            return self.plan_suspend([person.primary_email], suspend=True)
        if action == LifecycleAction.UNSUSPEND:
            return self.plan_suspend([person.primary_email], suspend=False)
        # ONBOARD/OFFBOARD/UPDATE land in later phases.
        return []

    async def apply(self, changes: Sequence[ChangePreview]) -> List[ChangeResult]:
        results: List[ChangeResult] = []
        for change in changes:
            if change.connector_id != self.id or not change.argv:
                results.append(ChangeResult(preview=change, ok=False, detail="not applicable to this connector"))
                continue
            results.append(await self._run_write("apply", change.target, list(change.argv), change.risk))
        return results

    # --- internals ---------------------------------------------------------------------
    async def _run_write(
        self,
        action: str,
        target: str,
        argv: List[str],
        risk: RiskLevel,
        target_extra: Optional[str] = None,
        tolerate_kinds: tuple = (),
    ) -> ChangeResult:
        """Run a mutation; audit it. ``tolerate_kinds`` lists GAMErrorKinds that count as success for
        a *best-effort* bulk op (e.g. an all-users sweep where 'not found' / own-calendar are expected)."""
        preview = ChangePreview(connector_id=self.id, target=target, summary=action, risk=risk, argv=argv)
        try:
            await self.runner.run_authenticated(self.domain, argv, serialize=True)
        except Exception as exc:
            tolerated = bool(tolerate_kinds) and getattr(exc, "kind", None) in tolerate_kinds
            self.audit.record(
                action, target=target, argv=argv, ok=tolerated,
                extra={"error": str(exc), "tolerated": tolerated,
                       **({"group": target_extra} if target_extra else {})},
            )
            if tolerated:
                return ChangeResult(preview=preview, ok=True,
                                    detail="Completed (best-effort — per-entity 'not shared' / "
                                           "own-calendar notices are expected and were skipped).")
            return ChangeResult(preview=preview, ok=False, detail=str(exc))
        self.audit.record(
            action, target=target, argv=argv, ok=True,
            extra={"group": target_extra} if target_extra else None,
        )
        return ChangeResult(preview=preview, ok=True)
