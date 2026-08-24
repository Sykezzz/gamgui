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
import random
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Protocol, Sequence

from gamgui.core.classroom.models import CourseRosterSnapshot
from gamgui.core.connectors.gam_connector import (
    ONEROSTER_MANAGED_ALIAS_CHUNK_CAP,
    ONEROSTER_ROSTER_CHUNK_CAP,
)
from gamgui.core.gam.errors import GAMError, GAMErrorKind

from .ingest import district_roster_date
from .models import (
    ImportAction,
    ImportIssue,
    IssueSeverity,
    LivePlanningResult,
    ManagedCourseDesired,
    ManagedCourseDirty,
    ManagedCourseState,
    ManagedCourseVerification,
    OneRosterError,
    PlanningPerformanceReceipt,
    canonical_hash,
)
from .semantic import metadata_hash, student_hash, teacher_hash
from .store import OneRosterStore
from .thresholds import evaluate_thresholds


PLANNER_SCHEMA_VERSION = 3
DIRECTORY_CONCURRENCY = 12
COURSE_CONCURRENCY = 8
DEFAULT_AUDIT_SAMPLE_SIZE = 25
ONEROSTER_READ_MAX_CONCURRENCY = 4
ONEROSTER_READ_MAX_ATTEMPTS = 3
ONEROSTER_READ_BACKOFF_BASE_SECONDS = 1.0
ONEROSTER_READ_BACKOFF_CAP_SECONDS = 8.0
ONEROSTER_READ_BACKOFF_JITTER_RATIO = 0.25
ONEROSTER_READ_LATENCY_REGRESSION_FACTOR = 2.0
ONEROSTER_READ_LATENCY_REGRESSION_FLOOR_SECONDS = 1.0
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
    metadata_loaded: bool = True
    teachers_loaded: bool = True
    students_loaded: bool = True


@dataclass(frozen=True, slots=True)
class _CourseReadIntent:
    metadata: bool
    teachers: bool
    students: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ActionPayload:
    actions: tuple[ImportAction, ...]
    archive_actions: tuple[ImportAction, ...]
    ownership_actions: tuple[ImportAction, ...]
    issues: tuple[ImportIssue, ...]
    live_hash: str
    action_counts: Mapping[str, int]
    owner_ids: Mapping[str, str]
    live_evidence: Mapping[str, Any]


class _IncompleteReadCoverage(ValueError):
    """A bounded read returned data that cannot prove its requested scope."""


@dataclass(frozen=True, slots=True)
class _CompletedReadChunk:
    value: Any
    duration_seconds: float
    clean: bool


@dataclass(slots=True)
class _ReadTuner:
    maximum: int
    level: int = 1
    clean_streak: int = 0
    baseline_seconds: Optional[float] = None

    def penalize(self) -> None:
        self.level = max(1, self.level - 1)
        self.clean_streak = 0

    def completed_cleanly(self, duration_seconds: float) -> bool:
        duration = max(0.0, float(duration_seconds))
        regressed = bool(
            self.baseline_seconds is not None
            and duration
            > max(
                ONEROSTER_READ_LATENCY_REGRESSION_FLOOR_SECONDS,
                self.baseline_seconds * ONEROSTER_READ_LATENCY_REGRESSION_FACTOR,
            )
        )
        if regressed:
            self.penalize()
        else:
            self.clean_streak += 1
            if self.clean_streak >= 2 and self.level < self.maximum:
                self.level += 1
                self.clean_streak = 0
        self.baseline_seconds = (
            duration
            if self.baseline_seconds is None
            else (self.baseline_seconds * 0.75) + (duration * 0.25)
        )
        return regressed


@dataclass(slots=True)
class _ReadPerformance:
    metadata_chunk_count: int = 0
    teacher_roster_chunk_count: int = 0
    student_roster_chunk_count: int = 0
    completed_chunk_count: int = 0
    retried_chunk_count: int = 0
    failed_chunk_count: int = 0
    rate_limit_count: int = 0
    timeout_retry_count: int = 0
    incomplete_coverage_count: int = 0
    latency_regression_count: int = 0
    largest_metadata_chunk: int = 0
    largest_roster_chunk: int = 0
    active_chunks: int = 0
    maximum_observed_read_concurrency: int = 0
    metadata_final_read_concurrency: int = 1
    teacher_final_read_concurrency: int = 1
    student_final_read_concurrency: int = 1
    metadata_chunk_worker_levels: list[int] = field(default_factory=list)
    teacher_roster_chunk_worker_levels: list[int] = field(default_factory=list)
    student_roster_chunk_worker_levels: list[int] = field(default_factory=list)

    def scheduled(self, category: str, size: int, worker_level: int) -> None:
        if category == "metadata":
            self.metadata_chunk_count += 1
            self.largest_metadata_chunk = max(self.largest_metadata_chunk, size)
            self.metadata_chunk_worker_levels.append(worker_level)
        elif category == "teachers":
            self.teacher_roster_chunk_count += 1
            self.largest_roster_chunk = max(self.largest_roster_chunk, size)
            self.teacher_roster_chunk_worker_levels.append(worker_level)
        else:
            self.student_roster_chunk_count += 1
            self.largest_roster_chunk = max(self.largest_roster_chunk, size)
            self.student_roster_chunk_worker_levels.append(worker_level)

    def set_final_level(self, category: str, level: int) -> None:
        if category == "metadata":
            self.metadata_final_read_concurrency = level
        elif category == "teachers":
            self.teacher_final_read_concurrency = level
        else:
            self.student_final_read_concurrency = level

    def final_recommendation(self) -> int:
        levels: list[int] = []
        if self.metadata_chunk_count:
            levels.append(self.metadata_final_read_concurrency)
        if self.teacher_roster_chunk_count:
            levels.append(self.teacher_final_read_concurrency)
        if self.student_roster_chunk_count:
            levels.append(self.student_final_read_concurrency)
        return min(levels) if levels else 1


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
        audit_sample_size: Optional[int] = None,
        read_max_concurrency: Optional[int] = None,
        read_max_attempts: Optional[int] = None,
        read_backoff_base_seconds: Optional[float] = None,
        read_backoff_cap_seconds: Optional[float] = None,
        read_sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        read_jitter: Optional[Callable[[], float]] = None,
    ) -> None:
        self.store = store
        self.connector = connector
        self.directory_concurrency = max(1, min(int(directory_concurrency), 32))
        self.course_concurrency = max(1, min(int(course_concurrency), 16))
        configured_audit = (
            getattr(connector, "oneroster_audit_sample_size", DEFAULT_AUDIT_SAMPLE_SIZE)
            if audit_sample_size is None
            else audit_sample_size
        )
        self.audit_sample_size = max(0, min(int(configured_audit), 25))
        configured_read_concurrency = (
            getattr(
                connector,
                "oneroster_read_max_concurrency",
                ONEROSTER_READ_MAX_CONCURRENCY,
            )
            if read_max_concurrency is None
            else read_max_concurrency
        )
        configured_read_attempts = (
            getattr(
                connector,
                "oneroster_read_max_attempts",
                ONEROSTER_READ_MAX_ATTEMPTS,
            )
            if read_max_attempts is None
            else read_max_attempts
        )
        configured_backoff_base = (
            getattr(
                connector,
                "oneroster_read_backoff_base_seconds",
                ONEROSTER_READ_BACKOFF_BASE_SECONDS,
            )
            if read_backoff_base_seconds is None
            else read_backoff_base_seconds
        )
        configured_backoff_cap = (
            getattr(
                connector,
                "oneroster_read_backoff_cap_seconds",
                ONEROSTER_READ_BACKOFF_CAP_SECONDS,
            )
            if read_backoff_cap_seconds is None
            else read_backoff_cap_seconds
        )
        self.read_max_concurrency = max(
            1,
            min(int(configured_read_concurrency), ONEROSTER_READ_MAX_CONCURRENCY),
        )
        self.read_max_attempts = max(1, min(int(configured_read_attempts), 6))
        self.read_backoff_base_seconds = max(
            0.0,
            min(float(configured_backoff_base), 60.0),
        )
        self.read_backoff_cap_seconds = max(
            self.read_backoff_base_seconds,
            min(float(configured_backoff_cap), 120.0),
        )
        self._read_sleep = read_sleep or getattr(
            connector,
            "oneroster_read_sleep",
            asyncio.sleep,
        )
        self._read_jitter = read_jitter or getattr(
            connector,
            "oneroster_read_jitter",
            random.random,
        )
        self._read_performance = _ReadPerformance()
        self._checkpointed_scopes: set[tuple[str, str]] = set()

    async def plan(
        self,
        import_id: str,
        *,
        limited_import: bool = False,
        now: Optional[datetime] = None,
        required_metadata_aliases: Sequence[str] = (),
    ) -> LivePlanningResult:
        planning_started = time.perf_counter()
        self._read_performance = _ReadPerformance()
        self._checkpointed_scopes = set()
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
        source_started = time.perf_counter()
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
        source_seconds = time.perf_counter() - source_started
        protected_aliases = {
            alias.casefold() for alias in protected_alias_values
        }
        previous_aliases = self.store.previous_accepted_aliases(import_id)
        relevant_aliases = tuple(
            sorted(
                {*protected_alias_values, *previous_aliases},
                key=str.casefold,
            )
        )
        managed_states = self.store.get_managed_course_states(relevant_aliases)
        bootstrap_metadata_aliases = tuple(
            alias
            for alias in relevant_aliases
            if (state := managed_states.get(alias.casefold())) is None
            or not state.course_id
            or state.metadata_dirty
            or state.recovery_required
        )
        async def timed_directory_snapshot() -> tuple[Optional[dict[str, Any]], float]:
            started = time.perf_counter()
            value = await self._read_directory_snapshot()
            return value, time.perf_counter() - started

        async def timed_bootstrap_snapshot() -> tuple[Optional[dict[str, Any]], float]:
            started = time.perf_counter()
            value = await self._read_managed_course_snapshot(
                bootstrap_metadata_aliases
            )
            return value, time.perf_counter() - started

        bootstrap_read_concurrently = bool(
            0 < len(bootstrap_metadata_aliases) <= ONEROSTER_MANAGED_ALIAS_CHUNK_CAP
        )
        if bootstrap_read_concurrently:
            directory_result, bootstrap_result = await asyncio.gather(
                timed_directory_snapshot(),
                timed_bootstrap_snapshot(),
            )
            directory_snapshot, directory_snapshot_seconds = directory_result
            bootstrap_courses, bootstrap_snapshot_seconds = bootstrap_result
        else:
            directory_snapshot, directory_snapshot_seconds = (
                await timed_directory_snapshot()
            )
            bootstrap_courses = {}
            bootstrap_snapshot_seconds = 0.0
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

        desired_states = _desired_course_states(eligible, import_id)
        read_plan, unchanged_aliases = _build_course_read_plan(
            eligible,
            desired_states,
            managed_states,
            previous_aliases=previous_aliases,
            protected_aliases=protected_alias_values,
        )
        audit_aliases = self.store.select_managed_audit_aliases(
            tuple(
                course.alias
                for course in eligible
                if course.alias.casefold() in unchanged_aliases
            ),
            limit=self.audit_sample_size,
        )
        for alias in audit_aliases:
            key = alias.casefold()
            read_plan[key] = _CourseReadIntent(
                metadata=True,
                teachers=True,
                students=True,
                reasons=("bounded_audit",),
            )
            unchanged_aliases.discard(key)
        required_metadata_keys = {
            key
            for alias in required_metadata_aliases
            if (key := _managed_alias_key(alias))
        }
        for alias in relevant_aliases:
            key = alias.casefold()
            if key not in required_metadata_keys:
                continue
            current = read_plan.get(
                key,
                _CourseReadIntent(False, False, False, ()),
            )
            read_plan[key] = _CourseReadIntent(
                metadata=True,
                teachers=current.teachers,
                students=current.students,
                reasons=(*current.reasons, "execution_identity_guard"),
            )
            unchanged_aliases.discard(key)

        # Desired hashes establish intent, not live proof. New aliases enter as
        # dirty stubs until exact live evidence supplies a stable course ID.
        self.store.record_managed_course_desired(tuple(desired_states.values()))
        managed_states = self.store.get_managed_course_states(relevant_aliases)
        cached_members = self.store.verified_managed_members_many(
            tuple(course.alias for course in eligible)
        )

        metadata_aliases = tuple(
            alias
            for alias in relevant_aliases
            if (intent := read_plan.get(alias.casefold())) is not None
            and intent.metadata
        )
        eligible_by_alias = {course.alias.casefold(): course for course in eligible}
        aliases_by_key = {alias.casefold(): alias for alias in relevant_aliases}
        known_course_ids: dict[str, str] = {}
        if bootstrap_read_concurrently and bootstrap_courses is not None:
            await self._persist_metadata_chunk(
                import_id,
                bootstrap_metadata_aliases,
                bootstrap_courses,
                eligible_by_alias,
                directory_snapshot,
                required_metadata_keys,
            )
            known_course_ids.update(
                (_text(detail, "id"), key)
                for key, detail in bootstrap_courses.items()
                if _text(detail, "id")
            )

        async def checkpoint_metadata(
            chunk: tuple[str, ...],
            indexed: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            for key, detail in indexed.items():
                course_id = _text(detail, "id")
                previous_key = known_course_ids.get(course_id) if course_id else None
                if previous_key is not None and previous_key != key:
                    self.store.mark_managed_course_dirty(
                        tuple(
                            ManagedCourseDirty(aliases_by_key[item], metadata=True)
                            for item in (previous_key, key)
                        ),
                        error_code="OR-ALIAS-COLLISION",
                        recovery_required=True,
                    )
                    raise OneRosterError(
                        "OR-ALIAS-COLLISION",
                        "More than one managed alias resolves to the same live Classroom course.",
                    )
            await self._persist_metadata_chunk(
                import_id,
                chunk,
                indexed,
                eligible_by_alias,
                directory_snapshot,
                required_metadata_keys,
            )
            known_course_ids.update(
                (_text(detail, "id"), key)
                for key, detail in indexed.items()
                if _text(detail, "id")
            )
            return indexed

        bootstrap_keys = {
            alias.casefold() for alias in bootstrap_metadata_aliases
        } if bootstrap_read_concurrently else set()
        remaining_metadata_aliases = tuple(
            alias
            for alias in metadata_aliases
            if alias.casefold() not in bootstrap_keys
        )
        managed_courses = bootstrap_courses
        classroom_snapshot_seconds = bootstrap_snapshot_seconds
        if remaining_metadata_aliases and managed_courses is not None:
            classroom_started = time.perf_counter()
            additional_courses = await self._read_managed_course_snapshot(
                remaining_metadata_aliases,
                on_chunk=checkpoint_metadata,
            )
            classroom_snapshot_seconds += time.perf_counter() - classroom_started
            if additional_courses is None:
                managed_courses = None
            else:
                managed_courses = {**managed_courses, **additional_courses}
        roster_started = time.perf_counter()
        live_courses = await self._read_live_courses(
            import_id,
            eligible,
            managed_courses,
            directory_snapshot,
            read_plan=read_plan,
            managed_states=managed_states,
            cached_members=cached_members,
        )
        live_courses, live_resolution_issues = await self._resolve_live_participants(
            live_courses,
            resolved,
            directory_snapshot,
        )
        issues.extend(live_resolution_issues)
        roster_snapshot_seconds = time.perf_counter() - roster_started
        # Optional legacy connectors without exact bulk capabilities checkpoint
        # their bounded fallback reads here. Normal GAM bulk scopes were already
        # committed chunk-by-chunk and are skipped by this final compatibility pass.
        self._persist_live_verification(
            import_id,
            eligible,
            live_courses,
            read_plan,
        )

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
            live_evidence=action_payload.live_evidence,
            performance=PlanningPerformanceReceipt(
                total_seconds=time.perf_counter() - planning_started,
                source_seconds=source_seconds,
                directory_snapshot_seconds=directory_snapshot_seconds,
                classroom_snapshot_seconds=classroom_snapshot_seconds,
                roster_snapshot_seconds=roster_snapshot_seconds,
                total_managed_aliases=len(relevant_aliases),
                candidate_aliases=len(read_plan),
                unchanged_aliases=len(unchanged_aliases),
                metadata_reads_requested=len(metadata_aliases),
                teacher_rosters_requested=sum(
                    intent.teachers for intent in read_plan.values()
                ),
                student_rosters_requested=sum(
                    intent.students for intent in read_plan.values()
                ),
                cached_metadata_scopes=sum(
                    not bool(
                        (intent := read_plan.get(course.alias.casefold()))
                        and intent.metadata
                    )
                    for course in eligible
                ),
                cached_teacher_scopes=sum(
                    not bool(
                        (intent := read_plan.get(course.alias.casefold()))
                        and intent.teachers
                    )
                    for course in eligible
                ),
                cached_student_scopes=sum(
                    not bool(
                        (intent := read_plan.get(course.alias.casefold()))
                        and intent.students
                    )
                    for course in eligible
                ),
                audit_courses_requested=len(audit_aliases),
                metadata_chunk_count=self._read_performance.metadata_chunk_count,
                teacher_roster_chunk_count=(
                    self._read_performance.teacher_roster_chunk_count
                ),
                student_roster_chunk_count=(
                    self._read_performance.student_roster_chunk_count
                ),
                completed_chunk_count=self._read_performance.completed_chunk_count,
                retried_chunk_count=self._read_performance.retried_chunk_count,
                failed_chunk_count=self._read_performance.failed_chunk_count,
                rate_limit_count=self._read_performance.rate_limit_count,
                timeout_retry_count=self._read_performance.timeout_retry_count,
                incomplete_coverage_count=(
                    self._read_performance.incomplete_coverage_count
                ),
                latency_regression_count=(
                    self._read_performance.latency_regression_count
                ),
                largest_metadata_chunk=self._read_performance.largest_metadata_chunk,
                largest_roster_chunk=self._read_performance.largest_roster_chunk,
                maximum_observed_read_concurrency=(
                    self._read_performance.maximum_observed_read_concurrency
                ),
                final_recommended_read_concurrency=(
                    self._read_performance.final_recommendation()
                ),
                metadata_final_read_concurrency=(
                    self._read_performance.metadata_final_read_concurrency
                ),
                teacher_final_read_concurrency=(
                    self._read_performance.teacher_final_read_concurrency
                ),
                student_final_read_concurrency=(
                    self._read_performance.student_final_read_concurrency
                ),
                metadata_chunk_worker_levels=tuple(
                    self._read_performance.metadata_chunk_worker_levels
                ),
                teacher_roster_chunk_worker_levels=tuple(
                    self._read_performance.teacher_roster_chunk_worker_levels
                ),
                student_roster_chunk_worker_levels=tuple(
                    self._read_performance.student_roster_chunk_worker_levels
                ),
            ),
            desired_course_hashes=desired_states,
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
        *,
        on_chunk: Optional[
            Callable[
                [tuple[str, ...], Mapping[str, Any]],
                Awaitable[Mapping[str, Any]],
            ]
        ] = None,
    ) -> Optional[dict[str, Any]]:
        selected_by_key: dict[str, str] = {}
        for raw_alias in aliases:
            alias = str(raw_alias or "").strip()
            if alias.startswith("d:"):
                alias = alias[2:]
            key = _managed_alias_key(alias)
            if not key:
                raise OneRosterError(
                    "OR-ALIAS-INVALID",
                    "A managed Classroom alias must use the exact Section_<SectionID> form.",
                )
            selected_by_key.setdefault(key, alias)
        selected = tuple(sorted(selected_by_key.values(), key=str.casefold))
        if not selected:
            return {}
        bulk = getattr(self.connector, "list_oneroster_managed_courses", None)
        if not callable(bulk):
            return None
        chunks = tuple(
            tuple(selected[offset : offset + ONEROSTER_MANAGED_ALIAS_CHUNK_CAP])
            for offset in range(0, len(selected), ONEROSTER_MANAGED_ALIAS_CHUNK_CAP)
        )
        merged: dict[str, Any] = {}
        course_ids: dict[str, str] = {}

        async def read_chunk(chunk: tuple[str, ...]) -> Mapping[str, Any]:
            raw = await bulk(chunk)
            return _index_managed_courses(raw, requested_aliases=chunk)

        async def accept_chunk(
            chunk: tuple[str, ...],
            indexed: Mapping[str, Any],
        ) -> None:
            for alias_key, detail in indexed.items():
                course_id = _text(detail, "id")
                previous_alias = course_ids.get(course_id) if course_id else None
                if previous_alias is not None and previous_alias != alias_key:
                    affected = tuple(
                        ManagedCourseDirty(alias, metadata=True)
                        for alias in (
                            selected_by_key[previous_alias],
                            selected_by_key[alias_key],
                        )
                    )
                    self.store.mark_managed_course_dirty(
                        affected,
                        error_code="OR-ALIAS-COLLISION",
                        recovery_required=True,
                    )
                    raise OneRosterError(
                        "OR-ALIAS-COLLISION",
                        "More than one managed alias resolves to the same live Classroom course.",
                    )
                if course_id:
                    course_ids[course_id] = alias_key
            if on_chunk is not None:
                await on_chunk(chunk, indexed)
            merged.update(indexed)

        async def fail_chunk(chunk: tuple[str, ...], error_code: str) -> None:
            self.store.mark_managed_course_dirty(
                tuple(ManagedCourseDirty(alias, metadata=True) for alias in chunk),
                error_code=error_code,
            )

        await self._coordinate_read_chunks(
            chunks,
            category="metadata",
            read_chunk=read_chunk,
            accept_chunk=accept_chunk,
            fail_chunk=fail_chunk,
        )
        return merged

    async def _coordinate_read_chunks(
        self,
        chunks: Sequence[tuple[str, ...]],
        *,
        category: str,
        read_chunk: Callable[[tuple[str, ...]], Awaitable[Any]],
        accept_chunk: Callable[[tuple[str, ...], Any], Awaitable[None]],
        fail_chunk: Callable[[tuple[str, ...], str], Awaitable[None]],
    ) -> None:
        tuner = _ReadTuner(self.read_max_concurrency)
        offset = 0
        try:
            while offset < len(chunks):
                worker_level = tuner.level
                wave = tuple(chunks[offset : offset + worker_level])
                for chunk in wave:
                    self._read_performance.scheduled(
                        category,
                        len(chunk),
                        worker_level,
                    )
                outcomes = await asyncio.gather(
                    *(
                        self._attempt_bounded_read(chunk, read_chunk, tuner)
                        for chunk in wave
                    ),
                    return_exceptions=True,
                )
                first_failure: Optional[tuple[OneRosterError, BaseException]] = None
                for chunk, outcome in zip(wave, outcomes):
                    if isinstance(outcome, asyncio.CancelledError):
                        raise outcome
                    failure: Optional[BaseException] = (
                        outcome if isinstance(outcome, BaseException) else None
                    )
                    completed: Optional[_CompletedReadChunk] = (
                        outcome if isinstance(outcome, _CompletedReadChunk) else None
                    )
                    if completed is not None:
                        try:
                            await accept_chunk(chunk, completed.value)
                        except BaseException as exc:
                            if isinstance(exc, asyncio.CancelledError):
                                raise
                            failure = exc
                            if isinstance(exc, _IncompleteReadCoverage):
                                self._read_performance.incomplete_coverage_count += 1
                                tuner.penalize()
                    if failure is not None:
                        self._read_performance.failed_chunk_count += 1
                        safe_error = self._safe_read_error(failure, category)
                        await fail_chunk(chunk, safe_error.code)
                        if first_failure is None:
                            first_failure = (safe_error, failure)
                        continue
                    assert completed is not None
                    self._read_performance.completed_chunk_count += 1
                    if completed.clean and tuner.completed_cleanly(
                        completed.duration_seconds
                    ):
                        self._read_performance.latency_regression_count += 1
                if first_failure is not None:
                    safe_error, cause = first_failure
                    if safe_error is cause:
                        raise safe_error
                    raise safe_error from cause
                offset += len(wave)
        finally:
            self._read_performance.set_final_level(category, tuner.level)

    async def _attempt_bounded_read(
        self,
        chunk: tuple[str, ...],
        read_chunk: Callable[[tuple[str, ...]], Awaitable[Any]],
        tuner: _ReadTuner,
    ) -> _CompletedReadChunk:
        self._read_performance.active_chunks += 1
        self._read_performance.maximum_observed_read_concurrency = max(
            self._read_performance.maximum_observed_read_concurrency,
            self._read_performance.active_chunks,
        )
        retried = False
        try:
            for attempt in range(self.read_max_attempts):
                started = time.perf_counter()
                try:
                    value = await read_chunk(chunk)
                except _IncompleteReadCoverage:
                    self._read_performance.incomplete_coverage_count += 1
                    tuner.penalize()
                    raise
                except GAMError as exc:
                    retryable = exc.kind in {
                        GAMErrorKind.RATE_LIMITED,
                        GAMErrorKind.TIMEOUT,
                    }
                    if not retryable:
                        raise
                    tuner.penalize()
                    if exc.kind is GAMErrorKind.RATE_LIMITED:
                        self._read_performance.rate_limit_count += 1
                    if attempt + 1 >= self.read_max_attempts:
                        raise
                    if exc.kind is GAMErrorKind.TIMEOUT:
                        self._read_performance.timeout_retry_count += 1
                    if not retried:
                        retried = True
                        self._read_performance.retried_chunk_count += 1
                    await self._read_sleep(self._read_backoff_delay(attempt))
                    continue
                return _CompletedReadChunk(
                    value=value,
                    duration_seconds=time.perf_counter() - started,
                    clean=not retried,
                )
            raise RuntimeError("bounded OneRoster read exhausted unexpectedly")
        finally:
            self._read_performance.active_chunks -= 1

    def _read_backoff_delay(self, retry_index: int) -> float:
        jitter = max(0.0, min(float(self._read_jitter()), 1.0))
        exponential = self.read_backoff_base_seconds * (2 ** max(0, retry_index))
        return min(
            self.read_backoff_cap_seconds,
            exponential * (1.0 + (ONEROSTER_READ_BACKOFF_JITTER_RATIO * jitter)),
        )

    @staticmethod
    def _safe_read_error(error: BaseException, category: str) -> OneRosterError:
        if isinstance(error, OneRosterError):
            return error
        if isinstance(error, GAMError):
            if error.kind is GAMErrorKind.RATE_LIMITED:
                return OneRosterError(
                    "OR-ONEROSTER-READ-RATE-LIMITED",
                    "Google continued to rate-limit a bounded OneRoster read; completed chunks were preserved.",
                )
            if error.kind is GAMErrorKind.TIMEOUT:
                return OneRosterError(
                    "OR-ONEROSTER-READ-TIMEOUT",
                    "A bounded OneRoster read timed out after finite retries; completed chunks were preserved.",
                )
            if error.kind in {
                GAMErrorKind.AUTH_EXPIRED,
                GAMErrorKind.NOT_AUTHENTICATED,
            }:
                return OneRosterError(
                    "OR-ONEROSTER-READ-AUTH",
                    "OneRoster live reads require renewed Google authentication.",
                )
            if error.kind in {
                GAMErrorKind.PERMISSION_DENIED,
                GAMErrorKind.SCOPE_MISSING,
            }:
                return OneRosterError(
                    "OR-ONEROSTER-READ-PERMISSION",
                    "The authorized account cannot safely complete OneRoster live reads.",
                )
        if category == "metadata":
            return OneRosterError(
                "OR-CLASSROOM-READ",
                "Live Classroom course resolution failed; no import plan was created.",
            )
        return OneRosterError(
            "OR-CLASSROOM-ROSTER-READ",
            "Live Classroom rosters could not be read safely.",
        )

    async def _persist_metadata_chunk(
        self,
        import_id: str,
        chunk: Sequence[str],
        indexed: Mapping[str, Any],
        courses_by_alias: Mapping[str, _DesiredCourse],
        directory_snapshot: Optional[Mapping[str, Any]],
        manifest_guard_aliases: set[str],
    ) -> None:
        states = self.store.get_managed_course_states(chunk)
        aliases_by_key = {
            _managed_alias_key(alias): str(alias).removeprefix("d:")
            for alias in chunk
        }
        verifications: list[ManagedCourseVerification] = []
        verified_keys: list[str] = []
        for key, detail in indexed.items():
            alias = aliases_by_key.get(key, "")
            state = states.get(key)
            if state is None and key not in courses_by_alias:
                # A pre-registry accepted alias can still supply fresh archive
                # evidence, but there is no durable Branch 1 row to checkpoint.
                continue
            if not alias or state is None:
                raise _IncompleteReadCoverage(
                    "A returned managed course has no registered exact alias."
                )
            course_id = _text(detail, "id")
            if state.course_id and state.course_id != course_id:
                self.store.mark_managed_course_dirty(
                    (ManagedCourseDirty(alias, metadata=True),),
                    error_code="OR-MANAGED-COURSE-ID-DRIFT",
                    recovery_required=True,
                )
                if key in manifest_guard_aliases:
                    # Executor/gate revalidation must retain the fresh live basis
                    # so its manifest comparison can persist an exact drift report.
                    continue
                raise OneRosterError(
                    "OR-MANAGED-COURSE-ID-DRIFT",
                    "The exact managed alias resolved to a different Classroom course.",
                )
            owner_email = await self._normalize_live_identity(
                _text(detail, "owner_email", "ownerEmail"),
                _text(detail, "owner_id", "ownerId"),
                directory_snapshot,
            )
            course_state = _text(detail, "course_state", "courseState").upper()
            if not (
                course_id
                and _has_exact_alias(detail, alias)
                and owner_email
                and course_state
            ):
                raise _IncompleteReadCoverage(
                    "A managed-course metadata chunk returned incomplete exact coverage."
                )
            desired_course = courses_by_alias.get(key)
            verifications.append(
                ManagedCourseVerification(
                    alias=alias,
                    course_id=course_id,
                    source_class_id=(
                        desired_course.class_id
                        if desired_course is not None
                        else state.source_class_id
                    ),
                    last_seen_import_id=import_id,
                    metadata_hash=metadata_hash(
                        alias,
                        _text(detail, "name"),
                        owner_email,
                        _text(detail, "room"),
                        _text(detail, "section"),
                        course_state,
                    ),
                    course_state=course_state,
                )
            )
            verified_keys.append(key)
        self.store.record_managed_course_verifications(tuple(verifications))
        self._checkpointed_scopes.update(
            (key, "metadata") for key in verified_keys
        )

    async def _normalize_live_identity(
        self,
        email: str,
        user_id: str,
        directory_snapshot: Optional[Mapping[str, Any]],
    ) -> str:
        email_key = _directory_identifier(email)
        id_key = _directory_id_key(user_id)
        if directory_snapshot is not None:
            return _snapshot_primary(
                directory_snapshot.get(email_key)
                or directory_snapshot.get(id_key)
            )
        reference = email_key or str(user_id or "").strip()
        if not reference:
            return ""
        try:
            user = await self.connector.get_user(reference)
        except Exception as exc:
            if _is_not_found(exc):
                return ""
            raise OneRosterError(
                "OR-DIRECTORY-READ",
                "Live Directory resolution failed; no import plan was created.",
            ) from exc
        return _snapshot_primary(user)

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
        import_id: str,
        courses: Sequence[_DesiredCourse],
        managed_courses: Optional[Mapping[str, Any]] = None,
        directory_snapshot: Optional[Mapping[str, Any]] = None,
        *,
        read_plan: Optional[Mapping[str, _CourseReadIntent]] = None,
        managed_states: Optional[Mapping[str, ManagedCourseState]] = None,
        cached_members: Optional[
            Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]]
        ] = None,
    ) -> dict[str, Optional[_LiveCourse]]:
        intents = read_plan or {}
        states = managed_states or {}
        retained_members = cached_members or {}
        if managed_courses is not None:
            details = {
                course.alias.casefold(): (
                    managed_courses.get(course.alias.casefold())
                    if bool(
                        (intent := intents.get(course.alias.casefold()))
                        and intent.metadata
                    )
                    else _cached_course_detail(
                        course,
                        states.get(course.alias.casefold()),
                    )
                )
                for course in courses
            }
        else:
            semaphore = asyncio.Semaphore(self.course_concurrency)

            async def read(course: _DesiredCourse) -> tuple[str, Optional[Any]]:
                intent = intents.get(course.alias.casefold())
                if not intent or not intent.metadata:
                    return (
                        course.alias.casefold(),
                        _cached_course_detail(
                            course,
                            states.get(course.alias.casefold()),
                        ),
                    )
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
        rosters = await self._read_rosters(
            import_id,
            courses,
            details,
            intents,
            directory_snapshot,
        )
        result: dict[str, Optional[_LiveCourse]] = {}
        for course in courses:
            key = course.alias.casefold()
            detail = details.get(key)
            if detail is None:
                result[key] = None
                continue
            course_id = _text(detail, "id")
            cached_teachers, cached_students = retained_members.get(key, ((), ()))
            read_teachers, read_students, unresolved_members = rosters.get(
                course_id,
                (None, None, ()),
            )
            teachers = (
                tuple(read_teachers)
                if read_teachers is not None
                else tuple(cached_teachers)
            )
            students = (
                tuple(read_students)
                if read_students is not None
                else tuple(cached_students)
            )
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
                unresolved_members=tuple(unresolved_members),
                metadata_loaded=bool(
                    (intent := intents.get(key)) and intent.metadata
                ),
                teachers_loaded=bool(intent and intent.teachers),
                students_loaded=bool(intent and intent.students),
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
                    set(live.unresolved_members)
                    | {
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
                metadata_loaded=live.metadata_loaded,
                teachers_loaded=live.teachers_loaded,
                students_loaded=live.students_loaded,
            )
        return normalized, issues

    async def _read_rosters(
        self,
        import_id: str,
        courses: Sequence[_DesiredCourse],
        details: Mapping[str, Optional[Any]],
        read_plan: Mapping[str, _CourseReadIntent],
        directory_snapshot: Optional[Mapping[str, Any]],
    ) -> dict[
        str,
        tuple[
            Optional[tuple[str, ...]],
            Optional[tuple[str, ...]],
            tuple[str, ...],
        ],
    ]:
        courses_by_alias = {course.alias.casefold(): course for course in courses}
        manifest_guard_aliases = {
            alias
            for alias, intent in read_plan.items()
            if "execution_identity_guard" in intent.reasons
        }
        requested: dict[
            str,
            tuple[str, _DesiredCourse, Any, bool, bool],
        ] = {}
        for alias, detail in details.items():
            intent = read_plan.get(alias)
            course_id = _text(detail, "id") if detail is not None else ""
            if not intent or not course_id or not (intent.teachers or intent.students):
                continue
            course = courses_by_alias.get(alias)
            if course is None:
                continue
            previous = requested.get(course_id)
            if previous is not None and previous[0] != alias:
                self.store.mark_managed_course_dirty(
                    (
                        ManagedCourseDirty(
                            previous[1].alias,
                            teachers=previous[3],
                            students=previous[4],
                        ),
                        ManagedCourseDirty(
                            course.alias,
                            teachers=intent.teachers,
                            students=intent.students,
                        ),
                    ),
                    error_code="OR-ALIAS-COLLISION",
                    recovery_required=True,
                )
                raise OneRosterError(
                    "OR-ALIAS-COLLISION",
                    "More than one managed alias resolves to the same live Classroom course.",
                )
            requested[course_id] = (
                alias,
                course,
                detail,
                intent.teachers,
                intent.students,
            )
        if not requested:
            return {}
        teacher_ids = sorted(
            course_id
            for course_id, (_alias, _course, _detail, teachers, _students) in requested.items()
            if teachers
        )
        student_ids = sorted(
            course_id
            for course_id, (_alias, _course, _detail, _teachers, students) in requested.items()
            if students
        )
        result: dict[
            str,
            tuple[
                Optional[tuple[str, ...]],
                Optional[tuple[str, ...]],
                tuple[str, ...],
            ],
        ] = {}
        bulk = getattr(self.connector, "list_course_participants_many", None)
        if callable(bulk):
            for course_ids, role in (
                (teacher_ids, "teachers"),
                (student_ids, "students"),
            ):
                if not course_ids:
                    continue
                chunks = tuple(
                    tuple(course_ids[offset : offset + ONEROSTER_ROSTER_CHUNK_CAP])
                    for offset in range(0, len(course_ids), ONEROSTER_ROSTER_CHUNK_CAP)
                )

                async def read_chunk(
                    chunk: tuple[str, ...],
                    *,
                    selected_role: str = role,
                ) -> Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]]:
                    participants = await bulk(chunk, selected_role)
                    requested_ids = frozenset(chunk)
                    if (
                        not isinstance(participants, CourseRosterSnapshot)
                        or participants.seen_course_ids != requested_ids
                        or not set(participants.rosters) <= requested_ids
                    ):
                        raise _IncompleteReadCoverage(
                            "The Classroom roster snapshot did not prove exact complete coverage."
                        )
                    normalized: dict[
                        str,
                        tuple[tuple[str, ...], tuple[str, ...]],
                    ] = {}
                    for course_id in chunk:
                        teachers, students = participants.for_course(course_id)
                        selected_members = (
                            teachers if selected_role == "teachers" else students
                        )
                        unexpected_members = (
                            students if selected_role == "teachers" else teachers
                        )
                        if unexpected_members:
                            raise _IncompleteReadCoverage(
                                "A role-specific Classroom roster read returned another role."
                            )
                        normalized[course_id] = await self._normalize_roster_members(
                            selected_members,
                            directory_snapshot,
                        )
                    return normalized

                async def accept_chunk(
                    chunk: tuple[str, ...],
                    normalized: Mapping[
                        str,
                        tuple[tuple[str, ...], tuple[str, ...]],
                    ],
                    *,
                    selected_role: str = role,
                ) -> None:
                    self._persist_roster_chunk(
                        import_id,
                        selected_role,
                        chunk,
                        normalized,
                        requested,
                        manifest_guard_aliases,
                    )
                    for course_id in chunk:
                        members, unresolved = normalized[course_id]
                        old_teachers, old_students, old_unresolved = result.get(
                            course_id,
                            (None, None, ()),
                        )
                        result[course_id] = (
                            members if selected_role == "teachers" else old_teachers,
                            members if selected_role == "students" else old_students,
                            tuple(sorted({*old_unresolved, *unresolved})),
                        )

                async def fail_chunk(
                    chunk: tuple[str, ...],
                    error_code: str,
                    *,
                    selected_role: str = role,
                ) -> None:
                    self.store.mark_managed_course_dirty(
                        tuple(
                            ManagedCourseDirty(
                                requested[course_id][1].alias,
                                teachers=selected_role == "teachers",
                                students=selected_role == "students",
                            )
                            for course_id in chunk
                        ),
                        error_code=error_code,
                    )

                await self._coordinate_read_chunks(
                    chunks,
                    category=role,
                    read_chunk=read_chunk,
                    accept_chunk=accept_chunk,
                    fail_chunk=fail_chunk,
                )
            return result

        semaphore = asyncio.Semaphore(self.course_concurrency)

        async def read(
            course_id: str,
            teachers_needed: bool,
            students_needed: bool,
        ) -> tuple[
            str,
            Optional[tuple[str, ...]],
            Optional[tuple[str, ...]],
            tuple[str, ...],
        ]:
            async with semaphore:
                try:
                    teachers, students = await asyncio.gather(
                        self.connector.list_course_participants(course_id, "teachers")
                        if teachers_needed
                        else asyncio.sleep(0, result=()),
                        self.connector.list_course_participants(course_id, "students")
                        if students_needed
                        else asyncio.sleep(0, result=()),
                    )
                except Exception as exc:
                    raise OneRosterError(
                        "OR-CLASSROOM-ROSTER-READ",
                        "Live Classroom rosters could not be read safely.",
                    ) from exc
            normalized_teachers: Optional[tuple[str, ...]] = None
            normalized_students: Optional[tuple[str, ...]] = None
            unresolved: set[str] = set()
            if teachers_needed:
                normalized_teachers, missing = await self._normalize_roster_members(
                    tuple(
                        _participant_email(item)
                        for item in teachers
                        if _participant_email(item)
                    ),
                    directory_snapshot,
                )
                unresolved.update(missing)
                self._persist_roster_chunk(
                    import_id,
                    "teachers",
                    (course_id,),
                    {course_id: (normalized_teachers, missing)},
                    requested,
                    manifest_guard_aliases,
                )
            if students_needed:
                normalized_students, missing = await self._normalize_roster_members(
                    tuple(
                        _participant_email(item)
                        for item in students
                        if _participant_email(item)
                    ),
                    directory_snapshot,
                )
                unresolved.update(missing)
                self._persist_roster_chunk(
                    import_id,
                    "students",
                    (course_id,),
                    {course_id: (normalized_students, missing)},
                    requested,
                    manifest_guard_aliases,
                )
            return (
                course_id,
                normalized_teachers,
                normalized_students,
                tuple(sorted(unresolved)),
            )

        return {
            course_id: (teachers, students, unresolved)
            for course_id, teachers, students, unresolved in await asyncio.gather(
                *(
                    read(course_id, teachers, students)
                    for course_id, (
                        _alias,
                        _course,
                        _detail,
                        teachers,
                        students,
                    ) in requested.items()
                )
            )
        }

    async def _normalize_roster_members(
        self,
        members: Iterable[str],
        directory_snapshot: Optional[Mapping[str, Any]],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        normalized: set[str] = set()
        unresolved: set[str] = set()
        for raw_email in sorted(
            {_directory_identifier(email) for email in members} - {""}
        ):
            primary = await self._normalize_live_identity(
                raw_email,
                "",
                directory_snapshot,
            )
            if primary:
                normalized.add(primary)
            else:
                unresolved.add(raw_email)
        return tuple(sorted(normalized)), tuple(sorted(unresolved))

    def _persist_roster_chunk(
        self,
        import_id: str,
        role: str,
        chunk: Sequence[str],
        normalized: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]],
        requested: Mapping[
            str,
            tuple[str, _DesiredCourse, Any, bool, bool],
        ],
        manifest_guard_aliases: set[str],
    ) -> None:
        aliases = tuple(requested[course_id][1].alias for course_id in chunk)
        states = self.store.get_managed_course_states(aliases)
        verifications: list[ManagedCourseVerification] = []
        dirty: list[ManagedCourseDirty] = []
        verified_keys: list[str] = []
        for course_id in chunk:
            alias_key, course, detail, _teachers, _students = requested[course_id]
            state = states.get(alias_key)
            if (
                state is None
                or not _has_exact_alias(detail, course.alias)
                or state.course_id not in {"", course_id}
            ):
                if state is not None and state.course_id not in {"", course_id}:
                    self.store.mark_managed_course_dirty(
                        (
                            ManagedCourseDirty(
                                course.alias,
                                metadata=True,
                                teachers=role == "teachers",
                                students=role == "students",
                            ),
                        ),
                        error_code="OR-MANAGED-COURSE-ID-DRIFT",
                        recovery_required=True,
                    )
                    if alias_key in manifest_guard_aliases:
                        # Do not adopt the rebound ID. The executor still needs
                        # this live roster basis to produce OR-MANIFEST-DRIFT.
                        continue
                    raise OneRosterError(
                        "OR-MANAGED-COURSE-ID-DRIFT",
                        "The exact managed alias resolved to a different Classroom course.",
                    )
                raise _IncompleteReadCoverage(
                    "A roster chunk did not retain exact managed-course identity."
                )
            members, unresolved = normalized[course_id]
            if unresolved:
                dirty.append(
                    ManagedCourseDirty(
                        course.alias,
                        teachers=role == "teachers",
                        students=role == "students",
                    )
                )
                continue
            verifications.append(
                ManagedCourseVerification(
                    alias=course.alias,
                    course_id=course_id,
                    source_class_id=course.class_id,
                    last_seen_import_id=import_id,
                    teacher_hash=teacher_hash(members) if role == "teachers" else None,
                    student_hash=student_hash(members) if role == "students" else None,
                    teacher_members=members if role == "teachers" else None,
                    student_members=members if role == "students" else None,
                )
            )
            verified_keys.append(alias_key)
        self.store.record_managed_course_verifications(tuple(verifications))
        self.store.mark_managed_course_dirty(
            tuple(dirty),
            error_code="OR-LIVE-PARTICIPANT-UNRESOLVED",
        )
        self._checkpointed_scopes.update((key, role) for key in verified_keys)

    def _persist_live_verification(
        self,
        import_id: str,
        courses: Sequence[_DesiredCourse],
        live_courses: Mapping[str, Optional[_LiveCourse]],
        read_plan: Mapping[str, _CourseReadIntent],
    ) -> None:
        verified: list[ManagedCourseVerification] = []
        dirty: list[ManagedCourseDirty] = []
        identity_dirty: list[ManagedCourseDirty] = []
        known_states = self.store.get_managed_course_states(
            tuple(course.alias for course in courses)
        )
        for course in courses:
            key = course.alias.casefold()
            intent = read_plan.get(key)
            live = live_courses.get(key)
            if intent is None or live is None:
                continue
            metadata_requested = bool(
                intent.metadata and (key, "metadata") not in self._checkpointed_scopes
            )
            teachers_requested = bool(
                intent.teachers and (key, "teachers") not in self._checkpointed_scopes
            )
            students_requested = bool(
                intent.students and (key, "students") not in self._checkpointed_scopes
            )
            if not (metadata_requested or teachers_requested or students_requested):
                continue
            course_id = _text(live.detail, "id")
            known = known_states.get(key)
            if known is not None and known.course_id and course_id != known.course_id:
                identity_dirty.append(
                    ManagedCourseDirty(
                        course.alias,
                        metadata=metadata_requested,
                        teachers=teachers_requested,
                        students=students_requested,
                    )
                )
                continue
            exact_alias = _has_exact_alias(live.detail, course.alias)
            state = _text(live.detail, "course_state", "courseState").upper()
            metadata_covered = bool(
                metadata_requested
                and course_id
                and exact_alias
                and live.owner_email
                and state
            )
            roster_covered = bool(
                course_id and exact_alias and not live.unresolved_members
            )
            if metadata_requested and not metadata_covered:
                dirty.append(ManagedCourseDirty(course.alias, metadata=True))
            if teachers_requested and not roster_covered:
                dirty.append(ManagedCourseDirty(course.alias, teachers=True))
            if students_requested and not roster_covered:
                dirty.append(ManagedCourseDirty(course.alias, students=True))
            if not (
                metadata_covered
                or (teachers_requested and roster_covered)
                or (students_requested and roster_covered)
            ):
                continue
            verified.append(
                ManagedCourseVerification(
                    alias=course.alias,
                    course_id=course_id,
                    source_class_id=course.class_id,
                    last_seen_import_id=import_id,
                    metadata_hash=(
                        metadata_hash(
                            course.alias,
                            _text(live.detail, "name"),
                            live.owner_email,
                            _text(live.detail, "room"),
                            _text(live.detail, "section"),
                            state,
                        )
                        if metadata_covered
                        else None
                    ),
                    teacher_hash=(
                        teacher_hash(live.teachers)
                        if teachers_requested and roster_covered
                        else None
                    ),
                    student_hash=(
                        student_hash(live.students)
                        if students_requested and roster_covered
                        else None
                    ),
                    teacher_members=(
                        tuple(live.teachers)
                        if teachers_requested and roster_covered
                        else None
                    ),
                    student_members=(
                        tuple(live.students)
                        if students_requested and roster_covered
                        else None
                    ),
                    course_state=state if metadata_covered else None,
                )
            )
        self.store.record_managed_course_verifications(tuple(verified))
        self.store.mark_managed_course_dirty(
            tuple(dirty),
            error_code="OR-LIVE-READ-INCOMPLETE",
        )
        self.store.mark_managed_course_dirty(
            tuple(identity_dirty),
            error_code="OR-MANAGED-COURSE-ID-DRIFT",
            recovery_required=True,
        )

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

    frozen_actions = _freeze_actions(actions)
    frozen_archive_actions = _freeze_actions(archive_actions)
    frozen_ownership_actions = _freeze_actions(ownership_actions)
    affected_aliases = {
        action.subject
        for action in (
            *frozen_actions,
            *frozen_archive_actions,
            *frozen_ownership_actions,
        )
    }
    archive_by_alias = {
        str(item.get("alias", "")): dict(item)
        for item in archive_basis
        if str(item.get("alias", ""))
    }
    live_evidence = {}
    for alias in sorted(affected_aliases, key=str.casefold):
        live = live_courses.get(alias.casefold())
        if live is not None:
            live_evidence[alias] = _live_basis(alias, live)
        elif alias in archive_by_alias:
            live_evidence[alias] = archive_by_alias[alias]
        else:
            live_evidence[alias] = {"alias": alias, "exists": False}

    return _ActionPayload(
        actions=frozen_actions,
        archive_actions=frozen_archive_actions,
        ownership_actions=frozen_ownership_actions,
        issues=tuple(issues),
        live_hash=live_hash,
        action_counts=dict(counts),
        owner_ids=owner_ids,
        live_evidence=live_evidence,
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


def _index_managed_courses(
    raw: Any,
    *,
    requested_aliases: Sequence[str],
) -> dict[str, Any]:
    if (
        isinstance(raw, (str, bytes, Mapping))
        or not isinstance(raw, Sequence)
    ):
        raise OneRosterError(
            "OR-CLASSROOM-READ",
            "The live Classroom snapshot had an invalid shape; no import plan was created.",
        )
    requested = {
        _managed_alias_key(alias): str(alias).removeprefix("d:")
        for alias in requested_aliases
        if _managed_alias_key(alias)
    }
    if len(requested) != len(requested_aliases):
        raise OneRosterError(
            "OR-ALIAS-COLLISION",
            "The managed-course metadata chunk did not contain unique exact aliases.",
        )
    indexed: dict[str, Any] = {}
    course_ids: dict[str, str] = {}
    for detail in raw:
        matches = {
            key
            for alias in _aliases(detail)
            if (key := _managed_alias_key(alias))
            and key in requested
        }
        if len(matches) != 1:
            raise OneRosterError(
                "OR-ALIAS-COLLISION",
                "A live Classroom course did not map to exactly one requested managed alias.",
            )
        key = next(iter(matches))
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


def _desired_course_states(
    courses: Sequence[_DesiredCourse],
    import_id: str,
) -> dict[str, ManagedCourseDesired]:
    return {
        course.alias.casefold(): ManagedCourseDesired(
            alias=course.alias,
            import_id=import_id,
            source_class_id=course.class_id,
            metadata_hash=metadata_hash(
                course.alias,
                course.name,
                course.owner_email,
                course.room,
                course.section,
                "ACTIVE",
            ),
            teacher_hash=teacher_hash(_desired_members(course, "teacher")),
            student_hash=student_hash(_desired_members(course, "student")),
        )
        for course in courses
    }


def _build_course_read_plan(
    courses: Sequence[_DesiredCourse],
    desired: Mapping[str, ManagedCourseDesired],
    managed: Mapping[str, ManagedCourseState],
    *,
    previous_aliases: Sequence[str],
    protected_aliases: Sequence[str],
) -> tuple[dict[str, _CourseReadIntent], set[str]]:
    plan: dict[str, _CourseReadIntent] = {}
    unchanged: set[str] = set()
    for course in courses:
        key = course.alias.casefold()
        target = desired[key]
        state = managed.get(key)
        reasons: list[str] = []
        if state is None:
            plan[key] = _CourseReadIntent(
                metadata=True,
                teachers=True,
                students=True,
                reasons=("new_course",),
            )
            continue
        metadata_changed = target.metadata_hash != state.desired_metadata_hash
        teachers_changed = target.teacher_hash != state.desired_teacher_hash
        students_changed = target.student_hash != state.desired_student_hash
        metadata_dirty = bool(
            state.metadata_dirty
            or not state.course_id
            or target.metadata_hash != state.verified_metadata_hash
        )
        teachers_dirty = bool(
            state.teacher_roster_dirty
            or target.teacher_hash != state.verified_teacher_hash
        )
        students_dirty = bool(
            state.student_roster_dirty
            or target.student_hash != state.verified_student_hash
        )
        if metadata_changed:
            reasons.append("metadata_changed")
        if teachers_changed:
            reasons.append("teachers_changed")
        if students_changed:
            reasons.append("students_changed")
        if metadata_dirty and not metadata_changed:
            reasons.append("dirty_metadata")
        if teachers_dirty and not teachers_changed:
            reasons.append("dirty_teachers")
        if students_dirty and not students_changed:
            reasons.append("dirty_students")
        if state.recovery_required:
            reasons.append("recovery_required")
            metadata_dirty = teachers_dirty = students_dirty = True
        intent = _CourseReadIntent(
            metadata=metadata_changed or metadata_dirty,
            teachers=teachers_changed or teachers_dirty,
            students=students_changed or students_dirty,
            reasons=tuple(reasons),
        )
        if intent.metadata or intent.teachers or intent.students:
            plan[key] = intent
        else:
            unchanged.add(key)

    protected = {alias.casefold() for alias in protected_aliases}
    for alias in previous_aliases:
        key = alias.casefold()
        if key not in protected:
            plan[key] = _CourseReadIntent(
                metadata=True,
                teachers=False,
                students=False,
                reasons=("removed_from_source",),
            )
            unchanged.discard(key)
    return plan, unchanged


def _cached_course_detail(
    course: _DesiredCourse,
    state: Optional[ManagedCourseState],
) -> Optional[dict[str, Any]]:
    if state is None or not state.course_id:
        return None
    return {
        "id": state.course_id,
        "aliases": (course.alias, f"d:{course.alias}"),
        "name": course.name,
        "section": course.section,
        "room": course.room,
        "owner_email": course.owner_email,
        "course_state": state.verified_course_state or "ACTIVE",
    }


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
    if (
        live.metadata_loaded
        and not limited_import
        and current_metadata != desired_metadata
    ):
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
    if live.metadata_loaded and not limited_import and state != "ACTIVE":
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

    if live.teachers_loaded:
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
    if live.students_loaded:
        for student in sorted(desired_students - current_students):
            _append_action(ordinary, _action("student_add", course.alias, student))
        if not limited_import:
            for student in sorted(current_students - desired_students):
                _append_action(
                    ordinary,
                    _action("student_remove", course.alias, student),
                )

    if (
        live.metadata_loaded
        and not limited_import
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
        "aliases": [alias] if _has_exact_alias(detail, alias) else sorted(_aliases(detail)),
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
