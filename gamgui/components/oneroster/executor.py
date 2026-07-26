"""Explicit, resumability-safe execution of immutable OneRoster manifests."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence

from gamgui.core.activity import ActivityBusyError, ActivityRegistry
from gamgui.core.classroom.models import CourseRosterSnapshot
from gamgui.core.gam.commands import GAMCommands

from .models import (
    ClassroomImportManifest,
    ExecutionSummary,
    GateState,
    ImportAction,
    LivePlanningResult,
    OneRosterError,
    StudentEnrollmentGate,
    canonical_hash,
)
from .planner import OneRosterPlanner
from .store import OneRosterStore, action_sequence_hash


MAX_BATCH_COMMANDS = 50
STUDENT_ACTIONS = frozenset({"student_add", "student_remove"})
ACTION_PRIORITIES = {
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


class ExecutableClassroomConnector(Protocol):
    async def run_classroom_batch(
        self,
        commands: Sequence[Sequence[str]],
        *,
        max_commands: int = 50,
    ) -> None: ...

    async def get_course(self, course_id: str, **kwargs: Any) -> Any: ...

    async def list_course_participants(self, course_id: str, role: str) -> Sequence[Any]: ...

    async def list_oneroster_managed_courses(
        self,
        aliases: Sequence[str],
    ) -> Sequence[Any]: ...

    async def list_course_participants_many(
        self,
        course_ids: Sequence[str],
        role: str = "all",
    ) -> Sequence[Any]: ...

    async def list_oneroster_directory(self) -> Mapping[str, Any]: ...


class OneRosterExecutor:
    """Apply an explicitly confirmed manifest in bounded, verified GAM batches."""

    def __init__(
        self,
        store: OneRosterStore,
        connector: ExecutableClassroomConnector,
        *,
        activity_registry: Optional[ActivityRegistry] = None,
        batch_size: int = MAX_BATCH_COMMANDS,
    ) -> None:
        self.store = store
        self.connector = connector
        self.activity_registry = activity_registry
        self.batch_size = max(1, min(int(batch_size), MAX_BATCH_COMMANDS))

    async def execute(
        self,
        manifest_id: str,
        *,
        typed_import_id: str = "",
        now: Optional[datetime] = None,
    ) -> ExecutionSummary:
        manifest = self.store.get_manifest_header(manifest_id)
        if manifest.confirmed and typed_import_id and typed_import_id.strip() != manifest.import_id:
            raise OneRosterError(
                "OR-CONFIRMATION-MISMATCH",
                "The typed import ID does not match this immutable manifest.",
            )

        lease = nullcontext()
        if self.activity_registry is not None:
            try:
                lease = self.activity_registry.acquire("oneroster-import")
            except ActivityBusyError as exc:
                raise OneRosterError(
                    "OR-ACTIVE-JOB",
                    "Another updater, connector, or administrative job is active.",
                ) from exc

        with lease:
            if not manifest.confirmed:
                manifest = self.store.confirm_manifest(
                    manifest_id,
                    typed_import_id,
                )
            prepared_student_release = manifest.status == "awaiting_students"
            manifest = self.store.claim_manifest(
                manifest_id,
                allow_awaiting_students=prepared_student_release,
            )
            try:
                planning = await OneRosterPlanner(self.store, self.connector).plan(
                    manifest.import_id,
                    limited_import=manifest.plan_kind == "limited",
                    now=now,
                )
                await self._validate_preflight(
                    manifest,
                    planning,
                    prepared_student_release=prepared_student_release,
                )
                gate = self.store.get_gate()
                student_open = _gate_allows(gate, manifest)
                pending_before = self.store.pending_action_summary(manifest.id)
                blocked_students = (
                    pending_before["student"] > 0 and not student_open
                )
                applied, failed, skipped = await self._apply(
                    manifest.id,
                    owner_ids=planning.owner_ids,
                    include_students=student_open,
                )
                current = self.store.get_manifest_header(manifest.id)
                pending = self.store.pending_action_summary(manifest.id)
                if (
                    pending["total"] > 0
                    and pending["nonstudent"] == 0
                    and not failed
                    and not skipped
                ):
                    prepared_planning = await OneRosterPlanner(
                        self.store,
                        self.connector,
                    ).plan(
                        manifest.import_id,
                        limited_import=manifest.plan_kind == "limited",
                        now=now,
                    )
                    await self._validate_preflight(
                        current,
                        prepared_planning,
                        prepared_student_release=True,
                        establish_prepared_stage=True,
                    )
                    current = self.store.record_prepared_live_hash(
                        manifest.id,
                        prepared_planning.live_hash,
                    )
                    status = "awaiting_students"
                    error = "OR-STUDENT-GATE-CLOSED" if blocked_students else ""
                elif failed or skipped or pending["total"] > 0:
                    status = "partial" if applied else "failed"
                    error = "OR-ACTION-FAILED"
                else:
                    status = "completed"
                    error = ""
                current = self.store.finish_manifest(
                    manifest.id,
                    status=status,
                    error=error,
                    load_actions=False,
                )
                if current.status == "completed":
                    self.store.maybe_mark_import_accepted(current.id)
                return ExecutionSummary(
                    manifest=current,
                    applied=applied,
                    failed=failed,
                    skipped=skipped,
                    awaiting_students=current.status == "awaiting_students",
                )
            except asyncio.CancelledError:
                self.store.finish_manifest(
                    manifest.id,
                    status="interrupted",
                    error="OR-EXECUTION-INTERRUPTED",
                    load_actions=False,
                )
                raise
            except OneRosterError as exc:
                status = "stale" if exc.code in {
                    "OR-MANIFEST-DRIFT",
                    "OR-SOURCE-DRIFT",
                    "OR-CONFIG-DRIFT",
                    "OR-THRESHOLD-HOLD",
                } else "failed"
                self.store.finish_manifest(
                    manifest.id,
                    status=status,
                    error=exc.code,
                    load_actions=False,
                )
                raise
            except BaseException:
                self.store.finish_manifest(
                    manifest.id,
                    status="interrupted",
                    error="OR-EXECUTION-INTERRUPTED",
                    load_actions=False,
                )
                raise

    async def revalidate_scheduled_gate(
        self,
        manifest_id: str,
        *,
        now: Optional[datetime] = None,
        manual: bool = False,
    ) -> StudentEnrollmentGate:
        """Re-read source/config/live state before an armed scheduled release opens."""
        lease = nullcontext()
        if self.activity_registry is not None:
            try:
                lease = self.activity_registry.acquire("oneroster-gate-revalidation")
            except ActivityBusyError as exc:
                raise OneRosterError(
                    "OR-ACTIVE-JOB",
                    "Another updater, connector, or administrative job is active.",
                ) from exc

        with lease:
            manifest = self.store.get_manifest_header(manifest_id)
            gate = self.store.get_gate()
            if (
                gate.state is not GateState.ARMED
                or gate.manifest_id != manifest.id
                or gate.manifest_hash != manifest.manifest_hash
            ):
                raise OneRosterError(
                    "OR-GATE-NOT-ARMED",
                    "Student release is not armed for this exact manifest.",
                )
            try:
                planning = await OneRosterPlanner(self.store, self.connector).plan(
                    manifest.import_id,
                    limited_import=manifest.plan_kind == "limited",
                    now=now,
                )
                await self._validate_preflight(
                    manifest,
                    planning,
                    prepared_student_release=True,
                )
                return self.store.open_gate(
                    manifest.id,
                    manifest.manifest_hash,
                    manifest.manifest_hash,
                    now=now,
                    allow_early=manual,
                )
            except OneRosterError as exc:
                if exc.code not in {"OR-GATE-NOT-DUE", "OR-GATE-NOT-ARMED"}:
                    self.store.hold_gate(
                        "OR-GATE-DRIFT",
                        f"Scheduled release revalidation failed ({exc.code}).",
                    )
                raise

    async def _validate_preflight(
        self,
        manifest: ClassroomImportManifest,
        planning: LivePlanningResult,
        *,
        prepared_student_release: bool = False,
        establish_prepared_stage: bool = False,
    ) -> None:
        """Run district-sized evidence scans away from the server event loop."""

        await asyncio.to_thread(
            self._validate_preflight_sync,
            manifest,
            planning,
            prepared_student_release=prepared_student_release,
            establish_prepared_stage=establish_prepared_stage,
        )

    def _validate_preflight_sync(
        self,
        manifest: ClassroomImportManifest,
        planning: LivePlanningResult,
        *,
        prepared_student_release: bool = False,
        establish_prepared_stage: bool = False,
    ) -> None:
        if planning.source_hash != manifest.source_hash:
            raise OneRosterError(
                "OR-SOURCE-DRIFT",
                "The retained OneRoster source no longer matches the approved manifest.",
            )
        if planning.config_hash != manifest.config_hash:
            raise OneRosterError(
                "OR-CONFIG-DRIFT",
                "District import configuration changed after this manifest was approved.",
            )
        if prepared_student_release:
            if establish_prepared_stage:
                if manifest.prepared_live_hash:
                    raise OneRosterError(
                        "OR-MANIFEST-DRIFT",
                        "Post-preparation live-state evidence was already established.",
                    )
            elif (
                not manifest.prepared_live_hash
                or planning.live_hash != manifest.prepared_live_hash
            ):
                raise OneRosterError(
                    "OR-MANIFEST-DRIFT",
                    "Live Classroom state changed after teacher preparation; "
                    "replan and confirm the student release.",
                )
        elif planning.live_hash != manifest.live_hash:
            raise OneRosterError(
                "OR-MANIFEST-DRIFT",
                "Live Classroom identity or roster state changed; replan and "
                "confirm the remaining work.",
            )
        planned_actions = planning.actions_for(manifest.plan_kind)
        expected_hash, expected_count = self.store.pending_actions_hash(manifest.id)
        current_hash, current_count = action_sequence_hash(planned_actions)
        if (
            expected_count != current_count
            or expected_hash != current_hash
        ):
            raise OneRosterError(
                "OR-MANIFEST-DRIFT",
                "Live Classroom state changed; replan and confirm the remaining work.",
            )
        evaluation = planning.threshold_evaluation
        if manifest.threshold_evidence:
            current_thresholds = _threshold_drift_evidence(
                _threshold_evidence(evaluation)
            )
            approved_thresholds = _threshold_drift_evidence(
                manifest.threshold_evidence
            )
            if current_thresholds.get("profile_hash") != approved_thresholds.get(
                "profile_hash"
            ):
                raise OneRosterError(
                    "OR-MANIFEST-DRIFT",
                    "District threshold policy changed; rebuild and confirm the district manifest.",
                )
            # Teacher preparation intentionally changes the live action counts
            # before the student gate opens.  For an untouched plan, every
            # threshold input must still be identical.  At the gate, the exact
            # pending student actions are compared above and the freshly
            # evaluated hold/blackout result below is authoritative.
            if (
                not prepared_student_release
                and canonical_hash(current_thresholds)
                != canonical_hash(approved_thresholds)
            ):
                raise OneRosterError(
                    "OR-MANIFEST-DRIFT",
                    "Threshold evidence changed; rebuild and confirm the district manifest.",
                )
            if canonical_hash(_issue_evidence(planning.issues)) != canonical_hash(
                _issue_evidence(manifest.exclusions)
            ):
                raise OneRosterError(
                    "OR-MANIFEST-DRIFT",
                    "Quarantines or exclusions changed; rebuild and confirm the district manifest.",
                )
        if evaluation.blackout:
            raise OneRosterError(
                "OR-THRESHOLD-HOLD",
                "The scheduled run now falls inside a district blackout window.",
            )
        if (
            evaluation.held
            and manifest.plan_kind != "limited"
            and not self.store.has_override_hash(
                manifest.import_id,
                manifest.threshold_evaluation_hash,
            )
        ):
            raise OneRosterError(
                "OR-THRESHOLD-HOLD",
                "District thresholds hold this live plan and no matching override exists.",
            )

    async def _apply(
        self,
        manifest_id: str,
        actions: Optional[Sequence[ImportAction]] = None,
        owner_ids: Optional[Mapping[str, str]] = None,
        *,
        include_students: bool = True,
    ) -> tuple[int, int, int]:
        if actions is not None:
            return await self._apply_explicit(
                manifest_id,
                actions,
                owner_ids,
            )

        allowed_kinds = tuple(
            kind
            for kind in self.store.pending_action_kinds(manifest_id)
            if include_students or kind not in STUDENT_ACTIONS
        )
        applied = failed = skipped = 0
        blocked_courses: set[str] = set()
        priorities = sorted({_priority_kind(kind) for kind in allowed_kinds})
        for priority in priorities:
            stage_kinds = tuple(
                kind
                for kind in allowed_kinds
                if _priority_kind(kind) == priority
            )
            while True:
                chunk = self.store.get_pending_action_batch(
                    manifest_id,
                    kinds=stage_kinds,
                    limit=self.batch_size,
                )
                if not chunk:
                    break
                runnable: list[ImportAction] = []
                for action in chunk:
                    if action.subject.casefold() in blocked_courses:
                        self.store.mark_action_result(
                            manifest_id,
                            action.id,
                            status="skipped",
                            detail=(
                                "Skipped because a prerequisite action for "
                                "this course failed."
                            ),
                            load_manifest=False,
                        )
                        skipped += 1
                    else:
                        runnable.append(action)
                if runnable:
                    chunk_applied, chunk_failed = await self._apply_chunk(
                        manifest_id,
                        runnable,
                        owner_ids,
                        blocked_courses,
                    )
                    applied += chunk_applied
                    failed += chunk_failed
        return applied, failed, skipped

    async def _apply_explicit(
        self,
        manifest_id: str,
        actions: Sequence[ImportAction],
        owner_ids: Optional[Mapping[str, str]] = None,
    ) -> tuple[int, int, int]:
        applied = failed = skipped = 0
        blocked_courses: set[str] = set()
        for priority in sorted({_priority(action) for action in actions}):
            stage = [action for action in actions if _priority(action) == priority]
            runnable: list[ImportAction] = []
            for action in stage:
                if action.subject.casefold() in blocked_courses:
                    self.store.mark_action_result(
                        manifest_id,
                        action.id,
                        status="skipped",
                        detail="Skipped because a prerequisite action for this course failed.",
                        load_manifest=False,
                    )
                    skipped += 1
                else:
                    runnable.append(action)
            for offset in range(0, len(runnable), self.batch_size):
                chunk = runnable[offset : offset + self.batch_size]
                chunk_applied, chunk_failed = await self._apply_chunk(
                    manifest_id,
                    chunk,
                    owner_ids,
                    blocked_courses,
                )
                applied += chunk_applied
                failed += chunk_failed
        return applied, failed, skipped

    async def _apply_chunk(
        self,
        manifest_id: str,
        chunk: Sequence[ImportAction],
        owner_ids: Optional[Mapping[str, str]],
        blocked_courses: set[str],
    ) -> tuple[int, int]:
        commands = [_command_for(action) for action in chunk]
        batch_failed = False
        try:
            await self.connector.run_classroom_batch(
                commands,
                max_commands=self.batch_size,
            )
        except Exception:
            batch_failed = True
        verified = await self._verify(chunk, owner_ids)
        applied = failed = 0
        for action in chunk:
            ok = bool(verified.get(action.id))
            if ok:
                self.store.mark_action_result(
                    manifest_id,
                    action.id,
                    status="applied",
                    detail=(
                        "Verified live after GAM reported a batch error."
                        if batch_failed
                        else "Verified against live Classroom state."
                    ),
                    load_manifest=False,
                )
                applied += 1
            else:
                self.store.mark_action_result(
                    manifest_id,
                    action.id,
                    status="failed",
                    detail="OR-BATCH-VERIFY-FAILED",
                    load_manifest=False,
                )
                failed += 1
                if action.kind in {
                    "course_create",
                    "teacher_add",
                    "owner_transfer",
                    "course_activate",
                }:
                    blocked_courses.add(action.subject.casefold())
        return applied, failed

    async def _verify(
        self,
        actions: Sequence[ImportAction],
        owner_ids: Optional[Mapping[str, str]] = None,
    ) -> dict[str, bool]:
        aliases = sorted({action.subject for action in actions})
        bulk_courses = getattr(
            self.connector,
            "list_oneroster_managed_courses",
            None,
        )
        if callable(bulk_courses):
            try:
                rows = await bulk_courses(aliases)
            except Exception:
                rows = ()
            details: dict[str, Optional[Any]] = {
                alias.casefold(): None for alias in aliases
            }
            for detail in rows:
                matched = [
                    alias.casefold()
                    for alias in aliases
                    if _has_alias(detail, alias)
                ]
                if len(matched) != 1 or details[matched[0]] is not None:
                    continue
                details[matched[0]] = detail
        else:
            semaphore = asyncio.Semaphore(8)

            async def read(alias: str) -> tuple[str, Optional[Any]]:
                async with semaphore:
                    try:
                        detail = await self.connector.get_course(
                            _course_ref(alias),
                            include_owner_email=True,
                            include_aliases=True,
                            best_effort_enrichment=False,
                        )
                    except Exception:
                        return alias.casefold(), None
                return alias.casefold(), detail

            details = dict(await asyncio.gather(*(read(alias) for alias in aliases)))
        roster_aliases = {
            action.subject.casefold()
            for action in actions
            if action.kind in {"teacher_add", "teacher_remove", "student_add", "student_remove"}
        }
        rosters: dict[str, tuple[set[str], set[str]]] = {}
        roster_details = {
            alias: detail
            for alias in sorted(roster_aliases)
            if (detail := details.get(alias)) is not None
            and _text(detail, "id")
        }
        bulk_rosters = getattr(
            self.connector,
            "list_course_participants_many",
            None,
        )
        if roster_details and callable(bulk_rosters):
            requested_course_ids = [
                _text(detail, "id") for detail in roster_details.values()
            ]
            try:
                participants = await bulk_rosters(
                    requested_course_ids,
                    "all",
                )
                if (
                    not isinstance(participants, CourseRosterSnapshot)
                    or not participants.covers(requested_course_ids)
                ):
                    participants = None
            except Exception:
                participants = None
            if participants is not None:
                by_course = {
                    _text(detail, "id"): alias
                    for alias, detail in roster_details.items()
                }
                rosters.update(
                    {
                        alias: (
                            set(participants.for_course(course_id)[0]),
                            set(participants.for_course(course_id)[1]),
                        )
                        for course_id, alias in by_course.items()
                    }
                )
        elif roster_details:
            for alias, detail in roster_details.items():
                course_id = _text(detail, "id")
                try:
                    teachers, students = await asyncio.gather(
                        self.connector.list_course_participants(course_id, "teachers"),
                        self.connector.list_course_participants(course_id, "students"),
                    )
                except Exception:
                    continue
                rosters[alias] = (
                    {_participant_email(item) for item in teachers if _participant_email(item)},
                    {_participant_email(item) for item in students if _participant_email(item)},
                )
        return {
            action.id: _verify_action(
                action,
                details.get(action.subject.casefold()),
                rosters.get(action.subject.casefold()),
                owner_ids or {},
            )
            for action in actions
        }


def _command_for(action: ImportAction) -> list[str]:
    course = _course_ref(action.subject)
    if action.kind == "course_create":
        payload = _payload(action.after)
        return GAMCommands.create_course(
            str(payload["name"]),
            str(payload["owner_email"]),
            alias=str(payload["alias"]),
            section=str(payload.get("section") or ""),
            room=str(payload.get("room") or ""),
            state="PROVISIONED",
        )
    if action.kind == "course_update":
        payload = _payload(action.after)
        return GAMCommands.update_course_roster_metadata(
            course,
            name=str(payload["name"]),
            section=str(payload.get("section") or ""),
            room=str(payload.get("room") or ""),
        )
    if action.kind == "course_activate":
        return GAMCommands.update_course_state(course, "ACTIVE")
    if action.kind == "course_archive":
        return GAMCommands.update_course_state(course, "ARCHIVED")
    if action.kind == "owner_transfer":
        return GAMCommands.transfer_course_owner(course, action.target)
    if action.kind == "teacher_add":
        return GAMCommands.add_course_participant(course, "teachers", action.target)
    if action.kind == "teacher_remove":
        return GAMCommands.remove_course_participant(course, "teachers", action.target)
    if action.kind == "student_add":
        return GAMCommands.add_course_participant(course, "students", action.target)
    if action.kind == "student_remove":
        return GAMCommands.remove_course_participant(course, "students", action.target)
    raise OneRosterError(
        "OR-ACTION-UNSUPPORTED",
        "The immutable manifest contains an unsupported action.",
    )


def _verify_action(
    action: ImportAction,
    detail: Optional[Any],
    roster: Optional[tuple[set[str], set[str]]],
    owner_ids: Optional[Mapping[str, str]] = None,
) -> bool:
    owner_ids = owner_ids or {}
    if detail is None or not _has_alias(detail, action.subject):
        return False
    if action.kind == "course_create":
        payload = _payload(action.after)
        expected_owner = str(payload["owner_email"]).casefold()
        expected_owner_id = owner_ids.get(expected_owner, "")
        owner_matches = (
            _text(detail, "owner_id", "ownerId") == expected_owner_id
            if expected_owner_id
            else _text(detail, "owner_email", "ownerEmail").casefold()
            == expected_owner
        )
        return (
            _text(detail, "name") == str(payload["name"])
            and owner_matches
            and _text(detail, "course_state", "courseState").upper()
            in {"PROVISIONED", "ACTIVE"}
        )
    if action.kind == "course_update":
        payload = _payload(action.after)
        return all(
            _text(detail, key) == str(payload.get(key) or "")
            for key in ("name", "section", "room")
        )
    if action.kind == "course_activate":
        return _text(detail, "course_state", "courseState").upper() == "ACTIVE"
    if action.kind == "course_archive":
        return _text(detail, "course_state", "courseState").upper() == "ARCHIVED"
    if action.kind == "owner_transfer":
        expected_owner_id = owner_ids.get(action.target.casefold(), "")
        return bool(
            (
                _text(detail, "owner_id", "ownerId") == expected_owner_id
                if expected_owner_id
                else _text(detail, "owner_email", "ownerEmail").casefold()
                == action.target.casefold()
            )
        )
    if roster is None:
        return False
    teachers, students = roster
    target = action.target.casefold()
    if action.kind == "teacher_add":
        return target in teachers
    if action.kind == "teacher_remove":
        return target not in teachers
    if action.kind == "student_add":
        return target in students
    if action.kind == "student_remove":
        return target not in students
    return False


def _gate_allows(
    gate: StudentEnrollmentGate,
    manifest: ClassroomImportManifest,
) -> bool:
    return (
        gate.state is GateState.OPEN
        and gate.manifest_id == manifest.id
        and gate.manifest_hash == manifest.manifest_hash
    )


def _priority(action: ImportAction) -> int:
    return _priority_kind(action.kind)


def _priority_kind(kind: str) -> int:
    return ACTION_PRIORITIES.get(kind, 999)


def _course_ref(alias: str) -> str:
    value = str(alias or "").strip()
    if not value.startswith("Section_") or any(character in value for character in "\r\n\x00"):
        raise OneRosterError("OR-ALIAS-INVALID", "Invalid managed Classroom alias.")
    return f"d:{value}"


def _payload(value: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise OneRosterError(
            "OR-ACTION-PAYLOAD",
            "The immutable manifest action payload is invalid.",
        ) from exc
    if not isinstance(payload, Mapping):
        raise OneRosterError(
            "OR-ACTION-PAYLOAD",
            "The immutable manifest action payload is invalid.",
        )
    return payload


def _has_alias(detail: Any, alias: str) -> bool:
    expected = alias.casefold()
    raw = _value(detail, "aliases", default=())
    aliases = raw if isinstance(raw, Sequence) and not isinstance(raw, str) else str(raw).split(",")
    return any(
        str(item).strip().casefold() in {expected, f"d:{expected}"}
        for item in aliases
    )


def _participant_email(participant: Any) -> str:
    return _text(participant, "email", "emailAddress").casefold()


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


def _threshold_evidence(value: Any) -> dict[str, Any]:
    breaches = _value(value, "breaches", default=()) or ()
    return {
        "held": bool(_value(value, "held", default=False)),
        "limited_import": bool(
            _value(value, "limited_import", default=False)
        ),
        "blackout": bool(_value(value, "blackout", default=False)),
        "evaluated_at": float(
            _value(value, "evaluated_at", default=0.0) or 0.0
        ),
        "counts": {
            str(key): int(item)
            for key, item in dict(
                _value(value, "counts", default={}) or {}
            ).items()
        },
        "baselines": {
            str(key): int(item)
            for key, item in dict(
                _value(value, "baselines", default={}) or {}
            ).items()
        },
        "breaches": [
            {
                name: _value(item, name, default=None)
                for name in (
                    "action",
                    "actual_count",
                    "baseline_count",
                    "actual_percent",
                    "max_count",
                    "max_percent",
                    "reason",
                )
            }
            for item in breaches
        ],
        "profile_hash": _text(value, "profile_hash"),
    }


def _threshold_drift_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove observation time while retaining every policy and count input.

    Revalidation necessarily happens at a different instant.  The timestamp is
    immutable audit evidence, but it is not itself threshold drift.
    """

    return {
        key: item
        for key, item in dict(value).items()
        if key != "evaluated_at"
    }


def _issue_evidence(values: Sequence[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values:
        severity = _value(value, "severity", default="error")
        result.append(
            {
                "code": _text(value, "code"),
                "severity": str(getattr(severity, "value", severity)),
                "message": _text(value, "message"),
                "entity_kind": _text(value, "entity_kind"),
                "source_id": _text(value, "source_id"),
                "row_number": _value(value, "row_number", default=None),
                "blocking": bool(_value(value, "blocking", default=True)),
            }
        )
    return result
