"""Credential-backed OneRoster-to-Classroom live diff planner.

Nothing in this module runs during component discovery or installation.  A caller
must explicitly invoke :meth:`OneRosterPlanner.plan`, at which point source and
live identities are resolved through one live Directory snapshot and only exact
``Section_<SectionID>`` aliases are considered managed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence

from gamgui.core.classroom.models import CourseRosterSnapshot
from gamgui.core.gam.errors import GAMError, GAMErrorKind

from .ingest import district_roster_date
from .models import (
    ImportAction,
    ImportIssue,
    IssueSeverity,
    LivePlanningResult,
    OneRosterError,
    canonical_hash,
)
from .store import OneRosterStore
from .thresholds import evaluate_thresholds


PLANNER_SCHEMA_VERSION = 3
DIRECTORY_CONCURRENCY = 12
COURSE_CONCURRENCY = 8
# The current planner uses compact in-memory participant/action models. Keep
# district planning comfortably below the measured memory cliff; larger valid
# snapshots remain available for inspection and CSV export.
MAX_ONEROSTER_PLANNING_COURSES = 50_000
MAX_ONEROSTER_PLANNING_ENROLLMENTS = 300_000
MAX_ONEROSTER_PLANNING_ACTIONS = 300_000


class LiveClassroomConnector(Protocol):
    async def get_user(self, email: str, fields: Optional[Sequence[str]] = None) -> Any: ...

    async def get_course(
        self,
        course_id: str,
        *,
        include_owner_email: bool = False,
        include_aliases: bool = False,
        best_effort_enrichment: bool = False,
    ) -> Any: ...

    async def list_course_participants(self, course_id: str, role: str) -> Sequence[Any]: ...

    async def list_oneroster_managed_courses(
        self,
        aliases: Sequence[str],
    ) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class _Participant:
    source_user_id: str
    email: str
    role: str
    primary: bool


@dataclass(frozen=True, slots=True)
class _DesiredCourse:
    class_id: str
    alias: str
    name: str
    section: str
    room: str
    owner_email: str
    participants: tuple[_Participant, ...]


@dataclass(frozen=True, slots=True)
class _LiveCourse:
    detail: Any
    teachers: tuple[str, ...] = ()
    students: tuple[str, ...] = ()
    owner_email: str = ""
    unresolved_members: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ActionPayload:
    actions: tuple[ImportAction, ...]
    archive_actions: tuple[ImportAction, ...]
    ownership_actions: tuple[ImportAction, ...]
    issues: tuple[ImportIssue, ...]
    live_hash: str
    action_counts: Mapping[str, int]
    owner_ids: Mapping[str, str]


def planner_configuration_hash(
    *,
    limited_import: bool,
    course_name_template: str,
    threshold_profile: Mapping[str, Any],
    schedule_scope: Mapping[str, str],
) -> str:
    return canonical_hash(
        {
            "planner_schema": PLANNER_SCHEMA_VERSION,
            "limited_import": bool(limited_import),
            "course_name_template": course_name_template,
            "threshold_profile": dict(threshold_profile),
            "schedule_scope": dict(schedule_scope),
        }
    )


class OneRosterPlanner:
    """Build an immutable, deterministic diff from a normalized snapshot and live state."""

    def __init__(
        self,
        store: OneRosterStore,
        connector: LiveClassroomConnector,
        *,
        directory_concurrency: int = DIRECTORY_CONCURRENCY,
        course_concurrency: int = COURSE_CONCURRENCY,
    ) -> None:
        self.store = store
        self.connector = connector
        self.directory_concurrency = max(1, min(int(directory_concurrency), 32))
        self.course_concurrency = max(1, min(int(course_concurrency), 16))

    async def plan(
        self,
        import_id: str,
        *,
        limited_import: bool = False,
        now: Optional[datetime] = None,
    ) -> LivePlanningResult:
        snapshot = self.store.refresh_schedule_scope(
            import_id,
            today=district_roster_date(now),
        )
        if not snapshot.ready_for_apply:
            raise OneRosterError(
                "OR-IMPORT-BLOCKED",
                "Only a valid full snapshot can be planned.",
            )

        normalized_path = self.store.normalized_path(import_id)
        await asyncio.to_thread(
            _enforce_planning_source_limits,
            normalized_path,
            self.store.domain,
        )
        desired_result, protected_alias_values = await asyncio.gather(
            asyncio.to_thread(
                _read_desired_courses,
                normalized_path,
                self.store.domain,
            ),
            asyncio.to_thread(
                _read_protected_alias_values,
                normalized_path,
                self.store.domain,
            ),
        )
        desired, issues = desired_result
        protected_aliases = {
            alias.casefold() for alias in protected_alias_values
        }
        previous_id = self.store.previous_accepted_import_id(import_id)
        previous_aliases = self.store.previous_accepted_aliases(import_id)
        relevant_aliases = tuple(
            sorted(
                {*protected_alias_values, *previous_aliases},
                key=str.casefold,
            )
        )
        directory_snapshot = await self._read_directory_snapshot()
        managed_courses = await self._read_managed_course_snapshot(
            relevant_aliases
        )
        resolved, resolution_issues = await self._resolve_directory(
            desired,
            directory_snapshot,
        )
        issues.extend(resolution_issues)

        eligible, course_issues = await asyncio.to_thread(
            _canonicalize_eligible_courses,
            desired,
            resolved,
        )
        issues.extend(course_issues)

        live_courses = await self._read_live_courses(
            eligible,
            managed_courses,
            directory_snapshot,
        )
        live_courses, live_resolution_issues = await self._resolve_live_participants(
            live_courses,
            resolved,
            directory_snapshot,
        )
        issues.extend(live_resolution_issues)

        archive_actions, archive_basis, archive_issues = await self._archive_actions(
            import_id,
            protected_aliases,
            managed_courses,
        )

        profile = self.store.get_threshold_profile()
        effective_limited = bool(limited_import or profile.limited_import)
        action_payload = await asyncio.to_thread(
            _build_action_payload,
            eligible,
            live_courses,
            archive_actions,
            archive_basis,
            effective_limited,
            directory_snapshot,
        )
        issues.extend(action_payload.issues)
        issues.extend(archive_issues)
        schedule_scope = self.store.schedule_scope(import_id)
        config_hash = planner_configuration_hash(
            limited_import=effective_limited,
            course_name_template=snapshot.course_name_template,
            threshold_profile=profile.to_dict(),
            schedule_scope=schedule_scope,
        )
        counts = Counter(action_payload.action_counts)
        counts["record_rejected"] = await asyncio.to_thread(
            _rejected_record_count,
            issues,
        )
        threshold_evaluation = evaluate_thresholds(
            profile,
            counts,
            self.store.baseline_counts(import_id),
            now=now,
        )
        return LivePlanningResult(
            import_id=import_id,
            source_hash=snapshot.source_sha256,
            config_hash=config_hash,
            live_hash=action_payload.live_hash,
            actions=action_payload.actions,
            archive_actions=action_payload.archive_actions,
            ownership_actions=action_payload.ownership_actions,
            issues=tuple(issues),
            limited_import=effective_limited,
            threshold_evaluation=threshold_evaluation,
            owner_ids=action_payload.owner_ids,
            scope_hash=canonical_hash(schedule_scope),
        )
    async def _read_directory_snapshot(self) -> Optional[dict[str, Any]]:
        bulk = getattr(self.connector, "list_oneroster_directory", None)
        if not callable(bulk):
            return None
        try:
            raw = await bulk()
            return _normalize_directory_snapshot(raw)
        except OneRosterError:
            raise
        except Exception as exc:
            raise OneRosterError(
                "OR-DIRECTORY-READ",
                "Live Directory resolution failed; no import plan was created.",
            ) from exc

    async def _read_managed_course_snapshot(
        self,
        aliases: Sequence[str],
    ) -> Optional[dict[str, Any]]:
        bulk = getattr(self.connector, "list_oneroster_managed_courses", None)
        if not callable(bulk):
            return None
        try:
            raw = await bulk(aliases)
            return _index_managed_courses(raw)
        except GAMError:
            # The exact-set bulk command is an optimization. Fall back to
            # bounded per-alias reads, which distinguish absent new courses
            # from genuine authentication and permission failures.
            return None
        except OneRosterError:
            raise
        except Exception as exc:
            raise OneRosterError(
                "OR-CLASSROOM-READ",
                "Live Classroom course resolution failed; no import plan was created.",
            ) from exc

    async def _resolve_directory(
        self,
        courses: Sequence[_DesiredCourse],
        directory_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> tuple[dict[str, str], list[ImportIssue]]:
        source_emails = await asyncio.to_thread(
            _source_participant_emails,
            courses,
        )
        if directory_snapshot is not None:
            results = [
                _resolve_snapshot_user(email, directory_snapshot)
                for email in source_emails
            ]
        else:
            semaphore = asyncio.Semaphore(self.directory_concurrency)

            async def resolve(email: str) -> tuple[str, str, str]:
                async with semaphore:
                    try:
                        user = await self.connector.get_user(email)
                    except Exception as exc:  # missing users are quarantined
                        if _is_not_found(exc):
                            return email, "", "OR-USER-NOT-FOUND"
                        raise OneRosterError(
                            "OR-DIRECTORY-READ",
                            "Live Directory resolution failed; no import plan was created.",
                        ) from exc
                return _resolved_user(email, user, require_active=True)

            results = await asyncio.gather(*(resolve(email) for email in source_emails))
        resolved = {source: primary for source, primary, code in results if not code}
        codes = {source: code for source, _primary, code in results if code}
        issues = await asyncio.to_thread(
            _directory_resolution_issues,
            courses,
            codes,
        )
        return resolved, issues

    async def _read_live_courses(
        self,
        courses: Sequence[_DesiredCourse],
        managed_courses: Optional[Mapping[str, Any]] = None,
        directory_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Optional[_LiveCourse]]:
        if managed_courses is not None:
            details = {
                course.alias.casefold(): managed_courses.get(course.alias.casefold())
                for course in courses
            }
        else:
            semaphore = asyncio.Semaphore(self.course_concurrency)

            async def read(course: _DesiredCourse) -> tuple[str, Optional[Any]]:
                async with semaphore:
                    try:
                        detail = await self.connector.get_course(
                            _course_ref(course.alias),
                            include_owner_email=True,
                            include_aliases=True,
                            best_effort_enrichment=False,
                        )
                    except Exception as exc:
                        if _is_not_found(exc):
                            return course.alias.casefold(), None
                        raise OneRosterError(
                            "OR-CLASSROOM-READ",
                            "Live Classroom course resolution failed; no import plan was created.",
                        ) from exc
                return course.alias.casefold(), detail

            details = dict(await asyncio.gather(*(read(course) for course in courses)))
        present = [detail for detail in details.values() if detail is not None]
        rosters = await self._read_rosters(present)
        result: dict[str, Optional[_LiveCourse]] = {}
        for course in courses:
            key = course.alias.casefold()
            detail = details.get(key)
            if detail is None:
                result[key] = None
                continue
            course_id = _text(detail, "id")
            teachers, students = rosters.get(course_id, ((), ()))
            owner_email = _text(
                detail,
                "owner_email",
                "ownerEmail",
            ).casefold()
            if not owner_email and directory_snapshot is not None:
                owner_email = _snapshot_primary(
                    directory_snapshot.get(
                        _directory_id_key(
                            _text(detail, "owner_id", "ownerId")
                        )
                    )
                )
            live = _LiveCourse(
                detail,
                teachers,
                students,
                owner_email,
            )
            result[key] = live
        return result

    async def _resolve_live_participants(
        self,
        courses: Mapping[str, Optional[_LiveCourse]],
        already_resolved: Mapping[str, str],
        directory_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> tuple[dict[str, Optional[_LiveCourse]], list[ImportIssue]]:
        current = sorted(
            {
                email
                for live in courses.values()
                if live is not None
                for email in (*live.teachers, *live.students, live.owner_email)
                if email and email not in already_resolved
            }
        )
        mapping = dict(already_resolved)
        if directory_snapshot is not None:
            mapping.update(
                (
                    email,
                    _snapshot_primary(directory_snapshot.get(email)),
                )
                for email in current
            )
        else:
            semaphore = asyncio.Semaphore(self.directory_concurrency)

            async def resolve(email: str) -> tuple[str, str]:
                async with semaphore:
                    try:
                        user = await self.connector.get_user(email)
                    except Exception as exc:
                        if _is_not_found(exc):
                            return email, ""
                        raise OneRosterError(
                            "OR-DIRECTORY-READ",
                            "Live Directory resolution failed; no import plan was created.",
                        ) from exc
                return email, _snapshot_primary(user)

            mapping.update(await asyncio.gather(*(resolve(email) for email in current)))
        normalized: dict[str, Optional[_LiveCourse]] = {}
        issues: list[ImportIssue] = []
        for alias, live in courses.items():
            if live is None:
                normalized[alias] = None
                continue
            unresolved = tuple(
                sorted(
                    {
                        email
                        for email in (*live.teachers, *live.students)
                        if not mapping.get(email)
                    }
                )
            )
            if unresolved:
                issues.append(
                    _issue(
                        "OR-LIVE-PARTICIPANT-UNRESOLVED",
                        "A current Classroom member is not one managed Directory user and was excluded from removals.",
                        alias,
                    )
                )
            normalized[alias] = _LiveCourse(
                detail=live.detail,
                teachers=tuple(
                    sorted({mapping[email] for email in live.teachers if mapping.get(email)})
                ),
                students=tuple(
                    sorted({mapping[email] for email in live.students if mapping.get(email)})
                ),
                owner_email=mapping.get(live.owner_email, ""),
                unresolved_members=unresolved,
            )
        return normalized, issues

    async def _read_rosters(
        self,
        details: Sequence[Any],
    ) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
        if not details:
            return {}
        course_ids = [_text(detail, "id") for detail in details if _text(detail, "id")]
        bulk = getattr(self.connector, "list_course_participants_many", None)
        if callable(bulk):
            try:
                participants = await bulk(course_ids, "all")
                if (
                    not isinstance(participants, CourseRosterSnapshot)
                    or not participants.covers(course_ids)
                ):
                    raise ValueError(
                        "The Classroom roster snapshot did not prove complete "
                        "coverage of the requested courses."
                    )
            except Exception as exc:
                raise OneRosterError(
                    "OR-CLASSROOM-ROSTER-READ",
                    "Live Classroom rosters could not be read safely.",
                ) from exc
            return {
                course_id: (
                    tuple(sorted(participants.for_course(course_id)[0])),
                    tuple(sorted(participants.for_course(course_id)[1])),
                )
                for course_id in course_ids
            }

        semaphore = asyncio.Semaphore(self.course_concurrency)

        async def read(course_id: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
            async with semaphore:
                try:
                    teachers, students = await asyncio.gather(
                        self.connector.list_course_participants(course_id, "teachers"),
                        self.connector.list_course_participants(course_id, "students"),
                    )
                except Exception as exc:
                    raise OneRosterError(
                        "OR-CLASSROOM-ROSTER-READ",
                        "Live Classroom rosters could not be read safely.",
                    ) from exc
            return (
                course_id,
                tuple(sorted({_participant_email(item) for item in teachers if _participant_email(item)})),
                tuple(sorted({_participant_email(item) for item in students if _participant_email(item)})),
            )

        return {
            course_id: (teachers, students)
            for course_id, teachers, students in await asyncio.gather(
                *(read(course_id) for course_id in course_ids)
            )
        }

    async def _archive_actions(
        self,
        import_id: str,
        desired_aliases: set[str],
        managed_courses: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[ImportAction], list[dict[str, Any]], list[ImportIssue]]:
        previous_id = self.store.previous_accepted_import_id(import_id)
        if not previous_id:
            return [], [], []
        previous = self.store.previous_accepted_aliases(import_id)
        missing = sorted(alias for alias in previous if alias.casefold() not in desired_aliases)
        if not missing:
            return [], [], []
        if managed_courses is not None:
            return await asyncio.to_thread(
                _build_archive_payload_from_snapshot,
                missing,
                managed_courses,
            )

        semaphore = asyncio.Semaphore(self.course_concurrency)

        async def read(alias: str) -> tuple[str, Optional[Any], Optional[ImportIssue]]:
            async with semaphore:
                try:
                    detail = await self.connector.get_course(
                        _course_ref(alias),
                        include_owner_email=False,
                        include_aliases=True,
                        best_effort_enrichment=False,
                    )
                except Exception as exc:
                    if _is_not_found(exc):
                        return alias, None, None
                    return (
                        alias,
                        None,
                        _issue(
                            "OR-ARCHIVE-READ",
                            "A previously managed course could not be verified for archive planning.",
                            alias,
                        ),
                    )
            return alias, detail, None

        rows = await asyncio.gather(*(read(alias) for alias in missing))
        return await asyncio.to_thread(_build_archive_payload, rows)


def _normalize_directory_snapshot(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise OneRosterError(
            "OR-DIRECTORY-READ",
            "The live Directory snapshot had an invalid shape; no import plan was created.",
        )
    snapshot: dict[str, Any] = {}
    for source_key, user in raw.items():
        primary = _snapshot_primary(user)
        if not primary:
            raise OneRosterError(
                "OR-DIRECTORY-READ",
                "The live Directory snapshot contained an unresolved identity.",
            )
        identifiers = {
            _directory_identifier(source_key),
            _directory_identifier(primary),
            *(_directory_identifier(alias) for alias in _aliases(user)),
            _directory_id_key(_text(user, "user_id", "id", "userId")),
        }
        identifiers.discard("")
        for identifier in identifiers:
            existing = snapshot.get(identifier)
            if existing is not None and _snapshot_primary(existing) != primary:
                raise OneRosterError(
                    "OR-DIRECTORY-READ",
                    "The live Directory snapshot contained an ambiguous identity.",
                )
            snapshot[identifier] = user
    return snapshot


def _directory_identifier(value: Any) -> str:
    identifier = str(value or "").strip().casefold()
    if (
        len(identifier) < 3
        or len(identifier) > 254
        or "@" not in identifier
        or any(character.isspace() or ord(character) < 32 for character in identifier)
    ):
        return ""
    return identifier


def _directory_id_key(value: Any) -> str:
    identifier = str(value or "").strip()
    if (
        not identifier
        or len(identifier) > 256
        or any(character.isspace() or ord(character) < 32 for character in identifier)
    ):
        return ""
    return f"id:{identifier}"


def _snapshot_primary(user: Any) -> str:
    if user is None:
        return ""
    return _directory_identifier(
        _text(user, "primary_email", "primaryEmail", "email")
    )


def _owner_id_evidence(
    courses: Sequence[_DesiredCourse],
    directory_snapshot: Optional[Mapping[str, Any]],
) -> dict[str, str]:
    """Keep only owner IDs needed for post-mutation verification in this run."""

    if directory_snapshot is None:
        return {}
    result: dict[str, str] = {}
    for email in sorted({course.owner_email.casefold() for course in courses}):
        user = directory_snapshot.get(email)
        if _snapshot_primary(user) != email:
            continue
        user_id = _text(user, "user_id", "id", "userId")
        if user_id:
            result[email] = user_id
    return result


def _build_action_payload(
    courses: Sequence[_DesiredCourse],
    live_courses: Mapping[str, Optional[_LiveCourse]],
    archive_actions: list[ImportAction],
    archive_basis: Sequence[Mapping[str, Any]],
    limited_import: bool,
    directory_snapshot: Optional[Mapping[str, Any]],
) -> _ActionPayload:
    """Finalize a potentially district-sized plan away from the event loop.

    Lists are populated once, sorted in place, and immediately frozen into the
    public tuple contract.  Live-state hashing streams one course at a time so
    it does not retain a second full copy of every Classroom roster.
    """

    actions: list[ImportAction] = []
    ownership_actions: list[ImportAction] = []
    issues: list[ImportIssue] = []

    for course in courses:
        live = live_courses.get(course.alias.casefold())
        if live is None:
            _append_new_course_actions(course, actions)
            continue
        if not _text(live.detail, "id"):
            issues.append(
                _issue(
                    "OR-COURSE-ID-MISSING",
                    "The managed Classroom course returned no stable course ID.",
                    course.class_id,
                )
            )
            continue
        if not _has_exact_alias(live.detail, course.alias):
            issues.append(
                _issue(
                    "OR-ALIAS-COLLISION",
                    "The exact managed alias did not resolve to a verifiable Classroom alias.",
                    course.class_id,
                )
            )
            continue
        if not live.owner_email:
            issues.append(
                _issue(
                    "OR-OWNER-UNRESOLVED",
                    "The current Classroom owner could not be resolved safely.",
                    course.class_id,
                )
            )
            continue
        _append_existing_course_actions(
            course,
            live,
            actions,
            ownership_actions,
            limited_import=limited_import,
        )

    if limited_import:
        archive_actions.clear()
        ownership_actions.clear()

    counts = Counter(_threshold_action_kind(action.kind) for action in actions)
    counts.update(
        _threshold_action_kind(action.kind) for action in archive_actions
    )
    counts.update(
        (
            "owner_mismatch"
            if action.kind == "owner_transfer"
            else _threshold_action_kind(action.kind)
        )
        for action in ownership_actions
    )
    live_hash = _stream_live_hash(courses, live_courses, archive_basis)
    owner_ids = _owner_id_evidence(courses, directory_snapshot)

    return _ActionPayload(
        actions=_freeze_actions(actions),
        archive_actions=_freeze_actions(archive_actions),
        ownership_actions=_freeze_actions(ownership_actions),
        issues=tuple(issues),
        live_hash=live_hash,
        action_counts=dict(counts),
        owner_ids=owner_ids,
    )


def _freeze_actions(actions: list[ImportAction]) -> tuple[ImportAction, ...]:
    actions.sort(key=_action_sort_key)
    frozen = tuple(actions)
    actions.clear()
    return frozen


def _stream_live_hash(
    courses: Sequence[_DesiredCourse],
    live_courses: Mapping[str, Optional[_LiveCourse]],
    archive_basis: Sequence[Mapping[str, Any]],
) -> str:
    # Preserve the previous canonical JSON-array hash exactly, but retain only
    # lightweight source references plus one materialized course basis at once.
    sources: list[tuple[str, int, Any]] = [
        (course.alias, 0, course)
        for course in courses
    ]
    sources.extend(
        (str(item["alias"]), 1, item)
        for item in archive_basis
    )
    sources.sort(key=lambda item: item[0])

    digest = hashlib.sha256()
    digest.update(b"[")
    for index, (_alias, source_kind, source) in enumerate(sources):
        if index:
            digest.update(b",")
        if source_kind == 0:
            live = live_courses.get(source.alias.casefold())
            basis = (
                _live_basis(source.alias, live)
                if live is not None
                else {"alias": source.alias, "exists": False}
            )
        else:
            basis = source
        digest.update(_json(basis).encode("utf-8"))
    digest.update(b"]")
    return digest.hexdigest()


def _rejected_record_count(issues: Sequence[ImportIssue]) -> int:
    return len(
        {
            (issue.entity_kind, issue.source_id)
            for issue in issues
            if issue.source_id
        }
    )


def _build_archive_payload_from_snapshot(
    aliases: Sequence[str],
    managed_courses: Mapping[str, Any],
) -> tuple[list[ImportAction], list[dict[str, Any]], list[ImportIssue]]:
    return _build_archive_payload(
        (
            (alias, managed_courses.get(alias.casefold()), None)
            for alias in aliases
        )
    )


def _build_archive_payload(
    rows: Iterable[tuple[str, Optional[Any], Optional[ImportIssue]]],
) -> tuple[list[ImportAction], list[dict[str, Any]], list[ImportIssue]]:
    actions: list[ImportAction] = []
    basis: list[dict[str, Any]] = []
    issues: list[ImportIssue] = []
    for alias, detail, issue in rows:
        if issue:
            issues.append(issue)
            continue
        if detail is None:
            basis.append({"alias": alias, "exists": False, "prior": True})
            continue
        if not _has_exact_alias(detail, alias):
            issues.append(
                _issue(
                    "OR-ALIAS-COLLISION",
                    "A previously managed alias no longer resolves exactly; it was not touched.",
                    alias,
                )
            )
            continue
        state = _text(detail, "course_state", "courseState").upper()
        basis.append(
            {
                "alias": alias,
                "exists": True,
                "prior": True,
                "id": _text(detail, "id"),
                "state": state,
            }
        )
        if state != "ARCHIVED":
            _append_action(
                actions,
                _action(
                    "course_archive",
                    alias,
                    _text(detail, "id"),
                    state,
                    "ARCHIVED",
                )
            )
    return actions, basis, issues


def _resolved_user(
    source_email: str,
    user: Any,
    *,
    require_active: bool,
) -> tuple[str, str, str]:
    primary = _snapshot_primary(user)
    if not primary:
        return source_email, "", "OR-USER-NOT-FOUND"
    if require_active and bool(_value(user, "suspended", default=False)):
        return source_email, "", "OR-USER-INACTIVE"
    return source_email, primary, ""


def _resolve_snapshot_user(
    source_email: str,
    directory_snapshot: Mapping[str, Any],
) -> tuple[str, str, str]:
    return _resolved_user(
        source_email,
        directory_snapshot.get(source_email.casefold()),
        require_active=True,
    )


def _index_managed_courses(raw: Any) -> dict[str, Any]:
    if (
        isinstance(raw, (str, bytes, Mapping))
        or not isinstance(raw, Sequence)
    ):
        raise OneRosterError(
            "OR-CLASSROOM-READ",
            "The live Classroom snapshot had an invalid shape; no import plan was created.",
        )
    indexed: dict[str, Any] = {}
    course_ids: dict[str, str] = {}
    for detail in raw:
        aliases = {
            key
            for alias in _aliases(detail)
            if (key := _managed_alias_key(alias))
        }
        for key in aliases:
            if key in indexed:
                raise OneRosterError(
                    "OR-ALIAS-COLLISION",
                    "More than one live Classroom course claims the same managed alias.",
                )
            indexed[key] = detail
            course_id = _text(detail, "id")
            if course_id:
                existing_alias = course_ids.get(course_id)
                if existing_alias is not None and existing_alias != key:
                    raise OneRosterError(
                        "OR-ALIAS-COLLISION",
                        "More than one managed alias resolves to the same live Classroom course.",
                    )
                course_ids[course_id] = key
    return indexed


def _managed_alias_key(value: Any) -> str:
    alias = str(value or "").strip()
    if alias.startswith("d:"):
        alias = alias[2:]
    if (
        not alias.startswith("Section_")
        or len(alias) == len("Section_")
        or any(character in alias for character in "\r\n\x00")
    ):
        return ""
    return alias.casefold()


def _read_desired_courses(
    path: Any,
    domain: str,
) -> tuple[list[_DesiredCourse], list[ImportIssue]]:
    with closing(_snapshot_conn(path)) as conn:
        plans = conn.execute(
            """
            SELECT * FROM course_plans
            WHERE domain = ?
            ORDER BY alias COLLATE NOCASE, class_id
            """,
            (domain,),
        ).fetchall()
        participants_by_class: dict[str, list[_Participant]] = defaultdict(list)
        for row in conn.execute(
            """
            SELECT e.class_id, e.user_id, e.role, e.is_primary, u.email
            FROM enrollments e
            JOIN course_plans p
              ON p.domain = e.domain AND p.class_id = e.class_id
             AND p.selected = 1 AND p.ready = 1
            LEFT JOIN users u
              ON u.domain = e.domain AND u.sourced_id = e.user_id
             AND u.status != 'tobedeleted'
            WHERE e.domain = ? AND e.status != 'tobedeleted'
              AND e.in_scope = 1
              AND e.role IN ('teacher', 'student')
            ORDER BY e.class_id, e.role, e.sourced_id
            """,
            (domain,),
        ):
            participants_by_class[str(row["class_id"])].append(
                _Participant(
                    source_user_id=str(row["user_id"]),
                    email=str(row["email"] or "").strip().casefold(),
                    role=str(row["role"]),
                    primary=bool(row["is_primary"]),
                )
            )
        courses: list[_DesiredCourse] = []
        issues: list[ImportIssue] = []
        for plan in plans:
            codes = tuple(json.loads(str(plan["quarantine_json"] or "[]")))
            if not bool(plan["selected"]):
                for code in codes:
                    issues.append(
                        _issue(
                            str(code),
                            "This OneRoster class remains quarantined and was excluded from live planning.",
                            str(plan["class_id"]),
                        )
                    )
                continue
            if not bool(plan["ready"]):
                for code in codes or ("OR-COURSE-QUARANTINED",):
                    issues.append(
                        _issue(
                            str(code),
                            "This OneRoster class remains quarantined and was excluded from live planning.",
                            str(plan["class_id"]),
                        )
                    )
                continue
            class_id = str(plan["class_id"])
            participants = tuple(participants_by_class.pop(class_id, ()))
            courses.append(
                _DesiredCourse(
                    class_id=class_id,
                    alias=str(plan["alias"]),
                    name=str(plan["name"]),
                    section=str(plan["section"]),
                    room=str(plan["room"]),
                    owner_email=str(plan["owner_email"]).casefold(),
                    participants=participants,
                )
            )
    return courses, issues


def _enforce_planning_source_limits(path: Any, domain: str) -> None:
    """Reject oversized graphs before either planning loader allocates them."""

    with closing(_snapshot_conn(path)) as conn:
        course_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM course_plans WHERE domain = ?",
                (domain,),
            ).fetchone()[0]
        )
        enrollment_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM enrollments e
                JOIN course_plans p
                  ON p.domain = e.domain AND p.class_id = e.class_id
                 AND p.selected = 1 AND p.ready = 1
                WHERE e.domain = ? AND e.status != 'tobedeleted'
                  AND e.in_scope = 1
                  AND e.role IN ('teacher', 'student')
                """,
                (domain,),
            ).fetchone()[0]
        )
    if course_count > MAX_ONEROSTER_PLANNING_COURSES:
        raise OneRosterError(
            "OR-PLAN-SCALE-LIMIT",
            (
                "This snapshot exceeds the safe live-planning limit of "
                f"{MAX_ONEROSTER_PLANNING_COURSES:,} class records. It remains "
                "available for inspection and CSV export; split or filter the "
                "source before planning Google changes."
            ),
        )
    if enrollment_count > MAX_ONEROSTER_PLANNING_ENROLLMENTS:
        raise OneRosterError(
            "OR-PLAN-SCALE-LIMIT",
            (
                "This snapshot exceeds the safe live-planning limit of "
                f"{MAX_ONEROSTER_PLANNING_ENROLLMENTS:,} active enrollments. "
                "It remains available for inspection and CSV export; split or "
                "filter the source before planning Google changes."
            ),
        )


def _read_managed_aliases(path: Any, domain: str) -> tuple[str, ...]:
    with closing(_snapshot_conn(path)) as conn:
        rows = conn.execute(
            """
            SELECT alias FROM course_plans
            WHERE domain = ? AND selected = 1 AND ready = 1
            ORDER BY alias COLLATE NOCASE
            """,
            (domain,),
        ).fetchall()
    return tuple(str(row["alias"]) for row in rows if str(row["alias"]).startswith("Section_"))


def _read_protected_alias_values(path: Any, domain: str) -> tuple[str, ...]:
    """Return every present exact alias, including out-of-scope or quarantined classes."""

    with closing(_snapshot_conn(path)) as conn:
        rows = conn.execute(
            """
            SELECT alias FROM course_plans
            WHERE domain = ?
            ORDER BY alias COLLATE NOCASE
            """,
            (domain,),
        ).fetchall()
    return tuple(
        alias
        for row in rows
        if (alias := str(row["alias"] or "")).startswith("Section_")
    )


def _snapshot_conn(path: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _source_participant_emails(
    courses: Sequence[_DesiredCourse],
) -> list[str]:
    return sorted(
        {
            participant.email.casefold()
            for course in courses
            for participant in course.participants
            if participant.email
        }
    )


def _directory_resolution_issues(
    courses: Sequence[_DesiredCourse],
    codes: Mapping[str, str],
) -> list[ImportIssue]:
    issues: list[ImportIssue] = []
    for course in courses:
        for participant in course.participants:
            code = codes.get(participant.email.casefold())
            if code:
                issues.append(
                    _issue(
                        code,
                        "A OneRoster participant did not resolve to one active live Directory user.",
                        course.class_id,
                        source_id=participant.source_user_id,
                    )
                )
    return _dedupe_issues(issues)


def _canonicalize_eligible_courses(
    courses: list[_DesiredCourse],
    resolved: Mapping[str, str],
) -> tuple[list[_DesiredCourse], list[ImportIssue]]:
    eligible: list[_DesiredCourse] = []
    issues: list[ImportIssue] = []
    for index, course in enumerate(courses):
        course_issues = _course_resolution_issues(course, resolved)
        if course_issues:
            issues.extend(course_issues)
            continue
        canonical = _canonicalize_course(course, resolved)
        courses[index] = canonical
        eligible.append(canonical)
    courses.clear()
    return eligible, issues


def _course_resolution_issues(
    course: _DesiredCourse,
    resolved: Mapping[str, str],
) -> list[ImportIssue]:
    issues: list[ImportIssue] = []
    by_primary: dict[str, set[str]] = defaultdict(set)
    for participant in course.participants:
        primary = resolved.get(participant.email.casefold(), "")
        if not primary:
            issues.append(
                _issue(
                    "OR-COURSE-DIRECTORY-BLOCKED",
                    "This class has a participant that did not resolve to one active live Directory user.",
                    course.class_id,
                    source_id=participant.source_user_id,
                )
            )
            continue
        by_primary[primary].add(participant.source_user_id)
    for source_ids in by_primary.values():
        if len(source_ids) > 1:
            issues.append(
                _issue(
                    "OR-USER-AMBIGUOUS",
                    "Multiple OneRoster identities resolve to the same live Directory user.",
                    course.class_id,
                )
            )
    owner = resolved.get(course.owner_email.casefold(), "")
    if not owner:
        issues.append(
            _issue(
                "OR-OWNER-NOT-FOUND",
                "The primary teacher did not resolve to one active live Directory user.",
                course.class_id,
            )
        )
    return _dedupe_issues(issues)


def _canonicalize_course(
    course: _DesiredCourse,
    resolved: Mapping[str, str],
) -> _DesiredCourse:
    return _DesiredCourse(
        class_id=course.class_id,
        alias=course.alias,
        name=course.name,
        section=course.section,
        room=course.room,
        owner_email=resolved[course.owner_email.casefold()],
        participants=tuple(
            _Participant(
                source_user_id=participant.source_user_id,
                email=resolved[participant.email.casefold()],
                role=participant.role,
                primary=participant.primary,
            )
            for participant in course.participants
        ),
    )


def _append_new_course_actions(
    course: _DesiredCourse,
    actions: list[ImportAction],
) -> None:
    payload = _json(
        {
            "alias": course.alias,
            "name": course.name,
            "section": course.section,
            "room": course.room,
            "owner_email": course.owner_email,
            "state": "PROVISIONED",
        }
    )
    _append_action(
        actions,
        _action("course_create", course.alias, course.owner_email, "", payload)
    )
    teachers = _desired_members(course, "teacher")
    students = _desired_members(course, "student")
    for teacher in teachers - {course.owner_email}:
        _append_action(actions, _action("teacher_add", course.alias, teacher))
    _append_action(
        actions,
        _action("course_activate", course.alias, "", "PROVISIONED", "ACTIVE"),
    )
    for student in students:
        _append_action(actions, _action("student_add", course.alias, student))


def _append_existing_course_actions(
    course: _DesiredCourse,
    live: _LiveCourse,
    ordinary: list[ImportAction],
    ownership: list[ImportAction],
    *,
    limited_import: bool = False,
) -> None:
    detail = live.detail
    current_metadata = {
        "name": _text(detail, "name"),
        "section": _text(detail, "section"),
        "room": _text(detail, "room"),
    }
    desired_metadata = {
        "name": course.name,
        "section": course.section,
        "room": course.room,
    }
    if not limited_import and current_metadata != desired_metadata:
        _append_action(
            ordinary,
            _action(
                "course_update",
                course.alias,
                _text(detail, "id"),
                _json(current_metadata),
                _json(desired_metadata),
            )
        )

    state = _text(detail, "course_state", "courseState").upper()
    if not limited_import and state != "ACTIVE":
        _append_action(
            ordinary,
            _action(
                "course_activate",
                course.alias,
                _text(detail, "id"),
                state,
                "ACTIVE",
            ),
        )

    desired_teachers = _desired_members(course, "teacher")
    desired_students = _desired_members(course, "student")
    current_teachers = set(live.teachers)
    current_students = set(live.students)
    current_owner = live.owner_email

    for teacher in sorted(desired_teachers - current_teachers):
        _append_action(ordinary, _action("teacher_add", course.alias, teacher))
    if not limited_import:
        for teacher in sorted(current_teachers - desired_teachers):
            if teacher == current_owner:
                continue
            _append_action(
                ordinary,
                _action("teacher_remove", course.alias, teacher),
            )
    for student in sorted(desired_students - current_students):
        _append_action(ordinary, _action("student_add", course.alias, student))
    if not limited_import:
        for student in sorted(current_students - desired_students):
            _append_action(
                ordinary,
                _action("student_remove", course.alias, student),
            )

    if (
        not limited_import
        and current_owner
        and current_owner != course.owner_email
    ):
        _append_action(
            ownership,
            _action(
                "owner_transfer",
                course.alias,
                course.owner_email,
                current_owner,
                course.owner_email,
            )
        )
        if current_owner not in desired_teachers:
            _append_action(
                ownership,
                _action(
                    "teacher_remove",
                    course.alias,
                    current_owner,
                    current_owner,
                    "",
                )
            )


def _append_action(actions: list[ImportAction], action: ImportAction) -> None:
    if len(actions) >= MAX_ONEROSTER_PLANNING_ACTIONS:
        raise OneRosterError(
            "OR-PLAN-SCALE-LIMIT",
            (
                "The live diff exceeds the safe planning limit of "
                f"{MAX_ONEROSTER_PLANNING_ACTIONS:,} actions. No manifest was "
                "created; narrow the source or create an additions-only plan."
            ),
        )
    actions.append(action)


def _desired_members(course: _DesiredCourse, role: str) -> set[str]:
    return {
        participant.email.casefold()
        for participant in course.participants
        if participant.role == role and participant.email
    }


def _threshold_action_kind(kind: str) -> str:
    # Activation mutates an existing course and must consume the existing
    # mandatory course-update threshold. This keeps legacy district profiles
    # protective without adding an unconfigured action family.
    return "course_update" if kind == "course_activate" else kind


def _live_basis(alias: str, live: _LiveCourse) -> dict[str, Any]:
    detail = live.detail
    return {
        "alias": alias,
        "exists": True,
        "id": _text(detail, "id"),
        "aliases": sorted(_aliases(detail)),
        "name": _text(detail, "name"),
        "section": _text(detail, "section"),
        "room": _text(detail, "room"),
        "owner_email": live.owner_email,
        "state": _text(detail, "course_state", "courseState").upper(),
        "teachers": list(live.teachers),
        "students": list(live.students),
        "unresolved_members": list(live.unresolved_members),
    }


def _has_exact_alias(detail: Any, alias: str) -> bool:
    expected = alias.casefold()
    return any(
        item.casefold() in {expected, f"d:{expected}"}
        for item in _aliases(detail)
    )


def _aliases(detail: Any) -> tuple[str, ...]:
    value = _value(detail, "aliases", default=())
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, Sequence):
        return tuple(str(part).strip() for part in value if str(part).strip())
    return ()


def _participant_email(participant: Any) -> str:
    return _text(participant, "email", "emailAddress").casefold()


def _action(
    kind: str,
    subject: str,
    target: str,
    before: str = "",
    after: str = "",
) -> ImportAction:
    basis = {
        "kind": kind,
        "subject": subject,
        "target": target,
        "before": before,
        "after": after,
    }
    return ImportAction(
        id=canonical_hash(basis)[:32],
        kind=kind,
        subject=subject,
        target=target,
        before=before,
        after=after,
    )


def _action_sort_key(action: ImportAction) -> tuple[Any, ...]:
    priority = {
        "course_create": 10,
        "course_update": 20,
        "teacher_add": 30,
        "owner_transfer": 40,
        "teacher_remove": 50,
        "course_activate": 60,
        "student_add": 70,
        "student_remove": 71,
        "course_archive": 80,
    }
    return (priority.get(action.kind, 999), action.subject.casefold(), action.target.casefold())


def _course_ref(alias: str) -> str:
    value = str(alias or "").strip()
    if not value.startswith("Section_") or any(character in value for character in "\r\n\x00"):
        raise OneRosterError(
            "OR-ALIAS-INVALID",
            "A managed Classroom alias must use the exact Section_<SectionID> form.",
        )
    return f"d:{value}"


def _is_not_found(exc: Exception) -> bool:
    if isinstance(exc, KeyError):
        return True
    kind = getattr(exc, "kind", None)
    return kind is GAMErrorKind.NOT_FOUND or getattr(kind, "value", "") == "not_found"


def _value(value: Any, *names: str, default: Any = "") -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    raw = getattr(value, "raw", None)
    if isinstance(raw, Mapping):
        for name in names:
            if name in raw:
                return raw[name]
    return default


def _text(value: Any, *names: str) -> str:
    return str(_value(value, *names, default="") or "").strip()


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _issue(
    code: str,
    message: str,
    class_id: str,
    *,
    source_id: str = "",
) -> ImportIssue:
    return ImportIssue(
        code=code,
        severity=IssueSeverity.ERROR,
        message=message,
        entity_kind="class",
        source_id=source_id or class_id,
        blocking=False,
    )


def _dedupe_issues(issues: Sequence[ImportIssue]) -> list[ImportIssue]:
    seen: set[tuple[str, str, str]] = set()
    result: list[ImportIssue] = []
    for issue in issues:
        key = (issue.code, issue.entity_kind, issue.source_id)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
