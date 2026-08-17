"""Explicit, resumability-safe execution of immutable OneRoster manifests."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import secrets
import time
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence

from gamgui.core.activity import ActivityBusyError, ActivityRegistry
from gamgui.core.classroom.models import CourseRosterSnapshot
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.gam.models import BatchExecutionReceipt
from gamgui.core.processes import current_process_identity

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
EXECUTION_HEARTBEAT_INTERVAL_SECONDS = 5.0
ADAPTIVE_WORKER_LEVELS = (3, 5, 8, 10)
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


@dataclass
class _AdaptiveWorkerTuner:
    level_index: int = 1
    clean_batches: int = 0
    best_seconds_per_action: float = 0.0

    @property
    def worker_count(self) -> int:
        return ADAPTIVE_WORKER_LEVELS[self.level_index]

    def observe(
        self,
        *,
        action_count: int,
        duration_seconds: float,
        batch_failed: bool,
        throttling_count: int,
        verification_attempts: int,
    ) -> None:
        per_action = max(0.0, duration_seconds) / max(1, int(action_count))
        latency_regressed = bool(
            self.best_seconds_per_action
            and per_action > self.best_seconds_per_action * 1.5
        )
        unhealthy = bool(
            batch_failed
            or throttling_count
            or verification_attempts > 1
            or latency_regressed
        )
        if unhealthy:
            self.level_index = max(0, self.level_index - 1)
            self.clean_batches = 0
            return
        if not self.best_seconds_per_action or per_action < self.best_seconds_per_action:
            self.best_seconds_per_action = per_action
        self.clean_batches += 1
        if self.clean_batches >= 2 and self.level_index < len(ADAPTIVE_WORKER_LEVELS) - 1:
            self.level_index += 1
            self.clean_batches = 0


class ExecutableClassroomConnector(Protocol):
    async def run_classroom_batch(
        self,
        commands: Sequence[Sequence[str]],
        *,
        max_commands: int = 50,
        worker_count: int = 5,
    ) -> BatchExecutionReceipt: ...

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
        stabilization_delays: Optional[Sequence[float]] = None,
    ) -> None:
        self.store = store
        self.connector = connector
        self.activity_registry = activity_registry
        self.batch_size = max(1, min(int(batch_size), MAX_BATCH_COMMANDS))
        configured_delays = (
            stabilization_delays
            if stabilization_delays is not None
            else getattr(connector, "oneroster_stabilization_delays", (2.0, 5.0))
        )
        normalized_delays = tuple(max(0.0, float(item)) for item in configured_delays)
        self.stabilization_delays = (
            normalized_delays[:2]
            if len(normalized_delays) >= 2
            else (2.0, 5.0)
        )
        self._operation_owner = f"{os.getpid()}:{secrets.token_urlsafe(12)}"
        self._operation_identity = current_process_identity()
        self._adaptive_workers = _AdaptiveWorkerTuner()

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
            resuming_after_pause = manifest.status == "paused"
            if prepared_student_release and not _gate_allows(
                self.store.get_gate(), manifest
            ):
                raise OneRosterError(
                    "OR-STUDENT-GATE-CLOSED",
                    "Open the student enrollment gate for this exact manifest before execution.",
                )
            manifest = self.store.claim_manifest(
                manifest_id,
                allow_awaiting_students=prepared_student_release,
                allow_paused=resuming_after_pause,
                owner_id=self._operation_owner,
                owner_pid=os.getpid(),
                owner_identity=self._operation_identity,
            )
            run = self.store.start_execution_run(
                manifest.id,
                phase="preflight",
                owner_id=self._operation_owner,
            )
            self._adaptive_workers = _AdaptiveWorkerTuner()
            try:
                planning = await self._plan_with_heartbeat(
                    manifest,
                    run_id=run.id,
                    now=now,
                )
                self.store.record_execution_planning_receipt(
                    run.id,
                    total_seconds=planning.performance.total_seconds,
                    directory_snapshot_seconds=(
                        planning.performance.directory_snapshot_seconds
                    ),
                    classroom_snapshot_seconds=(
                        planning.performance.classroom_snapshot_seconds
                    ),
                )
                self._record_performance_audit(
                    "oneroster_planning_performance",
                    target=f"{len(planning.actions_for(manifest.plan_kind))} actions",
                    extra={
                        "total_seconds": planning.performance.total_seconds,
                        "source_seconds": planning.performance.source_seconds,
                        "directory_snapshot_seconds": (
                            planning.performance.directory_snapshot_seconds
                        ),
                        "classroom_snapshot_seconds": (
                            planning.performance.classroom_snapshot_seconds
                        ),
                        "roster_snapshot_seconds": (
                            planning.performance.roster_snapshot_seconds
                        ),
                    },
                )
                await self._validate_preflight(
                    manifest,
                    planning,
                    prepared_student_release=prepared_student_release,
                    resuming_after_pause=resuming_after_pause,
                )
                gate = self.store.get_gate()
                student_open = _gate_allows(gate, manifest)
                pending_before = self.store.pending_action_summary(manifest.id)
                blocked_students = (
                    pending_before["student"] > 0 and not student_open
                )
                self.store.heartbeat_execution_run(run.id, phase="apply")
                applied, failed, skipped = await self._apply(
                    manifest.id,
                    owner_ids=planning.owner_ids,
                    include_students=student_open,
                    run_id=run.id,
                )
                paused = self.store.execution_stop_requested(run.id)
                if paused:
                    current = self.store.finish_manifest(
                        manifest.id,
                        status="paused",
                        error="OR-EXECUTION-PAUSED",
                        load_actions=False,
                        owner_id=self._operation_owner,
                    )
                    self.store.finish_execution_run(
                        run.id,
                        status="paused",
                        error_code="OR-EXECUTION-PAUSED",
                        phase="paused",
                    )
                    return ExecutionSummary(
                        manifest=current,
                        applied=applied,
                        failed=failed,
                        skipped=skipped,
                        awaiting_students=False,
                    )
                current = self.store.get_manifest_header(manifest.id)
                pending = self.store.pending_action_summary(manifest.id)
                if (
                    pending["total"] > 0
                    and pending["nonstudent"] == 0
                    and not failed
                    and not skipped
                ):
                    prepared_planning = await self._stabilized_planning(
                        manifest,
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
                        owner_id=self._operation_owner,
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
                    owner_id=self._operation_owner,
                )
                self.store.finish_execution_run(
                    run.id,
                    status="completed" if status in {"completed", "awaiting_students"} else "failed",
                    error_code=error,
                    phase=status,
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
                    status="recovery_required",
                    error="OR-RECOVERY-REQUIRED",
                    load_actions=False,
                    owner_id=self._operation_owner,
                )
                self.store.finish_execution_run(
                    run.id,
                    status="recovery_required",
                    error_code="OR-RECOVERY-REQUIRED",
                    phase="reconciliation",
                )
                raise

            except OneRosterError as exc:
                status = "stale" if exc.code in {
                    "OR-MANIFEST-DRIFT",
                    "OR-SOURCE-DRIFT",
                    "OR-CONFIG-DRIFT",
                    "OR-THRESHOLD-HOLD",
                    "OR-LIVE-NOT-STABLE",
                } else "failed"
                self.store.finish_manifest(
                    manifest.id,
                    status=status,
                    error=exc.code,
                    load_actions=False,
                    owner_id=self._operation_owner,
                )
                self.store.finish_execution_run(
                    run.id,
                    status="stale" if status == "stale" else "failed",
                    error_code=exc.code,
                    phase=status,
                )
                raise
            except BaseException:
                self.store.finish_manifest(
                    manifest.id,
                    status="recovery_required",
                    error="OR-RECOVERY-REQUIRED",
                    load_actions=False,
                    owner_id=self._operation_owner,
                )
                self.store.finish_execution_run(
                    run.id,
                    status="recovery_required",
                    error_code="OR-RECOVERY-REQUIRED",
                    phase="reconciliation",
                )
                raise

    async def _plan_with_heartbeat(
        self,
        manifest: ClassroomImportManifest,
        *,
        run_id: str,
        now: Optional[datetime],
    ) -> LivePlanningResult:
        """Keep the durable execution lease current during long live preflight reads."""

        async def pulse() -> None:
            while True:
                await asyncio.sleep(EXECUTION_HEARTBEAT_INTERVAL_SECONDS)
                await asyncio.to_thread(
                    self.store.heartbeat_execution_run,
                    run_id,
                    phase="preflight",
                )

        heartbeat_task = asyncio.create_task(pulse())
        try:
            return await OneRosterPlanner(self.store, self.connector).plan(
                manifest.import_id,
                limited_import=manifest.plan_kind == "limited",
                now=now,
            )
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _stabilized_planning(
        self,
        manifest: ClassroomImportManifest,
        *,
        now: Optional[datetime],
        require_claim: bool = True,
    ) -> LivePlanningResult:
        """Require two consecutive identical live observations after writes."""

        planner = OneRosterPlanner(self.store, self.connector)

        async def observe() -> LivePlanningResult:
            if require_claim:
                self._require_claim(manifest.id)
            return await planner.plan(
                manifest.import_id,
                limited_import=manifest.plan_kind == "limited",
                now=now,
            )

        first = await observe()
        await asyncio.sleep(self.stabilization_delays[0])
        second = await observe()
        if _planning_observation_hash(first) == _planning_observation_hash(second):
            return second
        await asyncio.sleep(self.stabilization_delays[1])
        third = await observe()
        if _planning_observation_hash(second) != _planning_observation_hash(third):
            raise OneRosterError(
                "OR-LIVE-NOT-STABLE",
                "Classroom reads did not stabilize after the preparation writes; retry later.",
            )
        return third

    async def retry_stabilization_read(
        self,
        manifest_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> LivePlanningResult:
        """Repeat only the bounded live observations for a stale unstable manifest."""

        manifest = self.store.get_manifest_header(manifest_id)
        if manifest.status != "stale" or manifest.error != "OR-LIVE-NOT-STABLE":
            raise OneRosterError(
                "OR-STABILIZATION-NOT-AVAILABLE",
                "Stabilization retry is available only for a manifest stopped by unstable live reads.",
            )
        return await self._stabilized_planning(
            manifest,
            now=now,
            require_claim=False,
        )

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

    async def reconcile_interrupted(
        self,
        manifest_id: str,
    ) -> ExecutionSummary:
        """Read back a possibly-sent batch before allowing any retry."""

        lease = nullcontext()
        if self.activity_registry is not None:
            try:
                lease = self.activity_registry.acquire("oneroster-recovery")
            except ActivityBusyError as exc:
                raise OneRosterError(
                    "OR-ACTIVE-JOB",
                    "Another updater, connector, or administrative job is active.",
                ) from exc
        with lease:
            manifest = self.store.get_manifest_header(manifest_id)
            if manifest.status != "recovery_required":
                raise OneRosterError(
                    "OR-RECOVERY-NOT-AVAILABLE",
                    "This manifest does not have an interrupted durable batch.",
                )
            manifest = self.store.claim_manifest(
                manifest_id,
                allow_recovery=True,
                owner_id=self._operation_owner,
                owner_pid=os.getpid(),
                owner_identity=self._operation_identity,
            )
            run = self.store.claim_execution_recovery(
                manifest_id,
                owner_id=self._operation_owner,
            )
            applied = 0
            try:
                for batch in self.store.get_reconciling_batches(manifest_id):
                    self._require_claim(manifest_id)
                    actions = self.store.get_manifest_actions_by_id(
                        manifest_id, batch.action_ids
                    )
                    verified = await self._verify(actions)
                    for action in actions:
                        if not verified.get(action.id, False):
                            continue
                        if action.status == "pending":
                            self.store.mark_action_result(
                                manifest_id,
                                action.id,
                                status="applied",
                                detail="Verified during interrupted-batch reconciliation.",
                                load_manifest=False,
                                owner_id=self._operation_owner,
                            )
                            applied += 1
                    self.store.finish_execution_batch(
                        batch.id,
                        status="reconciled",
                    )
                current = self.store.finish_manifest(
                    manifest_id,
                    status="paused",
                    error="OR-RECOVERY-RECONCILED",
                    load_actions=False,
                    owner_id=self._operation_owner,
                )
                self.store.finish_execution_run(
                    run.id,
                    status="paused",
                    error_code="OR-RECOVERY-RECONCILED",
                    phase="paused",
                )
                return ExecutionSummary(
                    manifest=current,
                    applied=applied,
                    failed=0,
                    skipped=0,
                    awaiting_students=False,
                )
            except BaseException:
                self.store.finish_manifest(
                    manifest_id,
                    status="recovery_required",
                    error="OR-RECOVERY-REQUIRED",
                    load_actions=False,
                    owner_id=self._operation_owner,
                )
                self.store.finish_execution_run(
                    run.id,
                    status="recovery_required",
                    error_code="OR-RECOVERY-REQUIRED",
                    phase="reconciliation",
                )
                raise

    async def _validate_preflight(
        self,
        manifest: ClassroomImportManifest,
        planning: LivePlanningResult,
        *,
        prepared_student_release: bool = False,
        establish_prepared_stage: bool = False,
        resuming_after_pause: bool = False,
    ) -> None:
        """Run district-sized evidence scans away from the server event loop."""

        await asyncio.to_thread(
            self._validate_preflight_sync,
            manifest,
            planning,
            prepared_student_release=prepared_student_release,
            establish_prepared_stage=establish_prepared_stage,
            resuming_after_pause=resuming_after_pause,
        )

    def _validate_preflight_sync(
        self,
        manifest: ClassroomImportManifest,
        planning: LivePlanningResult,
        *,
        prepared_student_release: bool = False,
        establish_prepared_stage: bool = False,
        resuming_after_pause: bool = False,
    ) -> None:
        if planning.source_hash != manifest.source_hash:
            self.store.record_manifest_drift(
                manifest.id,
                ({"category": "source changed", "field": "source_hash", "approved": manifest.source_hash, "current": planning.source_hash},),
            )
            raise OneRosterError(
                "OR-SOURCE-DRIFT",
                "The retained OneRoster source no longer matches the approved manifest.",
            )
        if planning.config_hash != manifest.config_hash:
            self.store.record_manifest_drift(
                manifest.id,
                ({"category": "configuration changed", "field": "config_hash", "approved": manifest.config_hash, "current": planning.config_hash},),
            )
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
                self._record_live_drift(manifest, planning)
                raise OneRosterError(
                    "OR-MANIFEST-DRIFT",
                    "Live Classroom state changed after teacher preparation; "
                    "replan and confirm the student release.",
                )
        elif not resuming_after_pause and planning.live_hash != manifest.live_hash:
            self._record_live_drift(manifest, planning)
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
            self.store.record_manifest_drift(
                manifest.id,
                ({
                    "category": "remaining-action sequence changed",
                    "field": "pending_actions",
                    "approved": f"{expected_count} actions / {expected_hash}",
                    "current": f"{current_count} actions / {current_hash}",
                },),
            )
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
                self.store.record_manifest_drift(
                    manifest.id,
                    ({"category": "threshold profile changed", "field": "profile_hash", "approved": approved_thresholds.get("profile_hash", ""), "current": current_thresholds.get("profile_hash", "")},),
                )
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
                and not resuming_after_pause
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
                self.store.record_manifest_drift(
                    manifest.id,
                    ({"category": "exclusions changed", "field": "exclusions", "approved": canonical_hash(_issue_evidence(manifest.exclusions)), "current": canonical_hash(_issue_evidence(planning.issues))},),
                )
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

    def _record_live_drift(
        self,
        manifest: ClassroomImportManifest,
        planning: LivePlanningResult,
    ) -> None:
        rows = _live_evidence_diff(
            manifest.live_evidence,
            planning.live_evidence,
        )
        if not rows:
            rows = (
                {
                    "category": "live state changed",
                    "field": "live_hash",
                    "approved": manifest.prepared_live_hash or manifest.live_hash,
                    "current": planning.live_hash,
                },
            )
        self.store.record_manifest_drift(manifest.id, rows)

    async def _apply(
        self,
        manifest_id: str,
        actions: Optional[Sequence[ImportAction]] = None,
        owner_ids: Optional[Mapping[str, str]] = None,
        *,
        include_students: bool = True,
        run_id: str = "",
    ) -> tuple[int, int, int]:
        if actions is not None:
            return await self._apply_explicit(
                manifest_id,
                actions,
                owner_ids,
                run_id=run_id,
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
                self._require_claim(manifest_id)
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
                            owner_id=self._operation_owner,
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
                        run_id=run_id,
                        phase=stage_kinds[0],
                    )
                    applied += chunk_applied
                    failed += chunk_failed
                if run_id and self.store.execution_stop_requested(run_id):
                    return applied, failed, skipped
        return applied, failed, skipped

    async def _apply_explicit(
        self,
        manifest_id: str,
        actions: Sequence[ImportAction],
        owner_ids: Optional[Mapping[str, str]] = None,
        *,
        run_id: str = "",
    ) -> tuple[int, int, int]:
        applied = failed = skipped = 0
        blocked_courses: set[str] = set()
        for priority in sorted({_priority(action) for action in actions}):
            self._require_claim(manifest_id)
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
                        owner_id=self._operation_owner,
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
                    run_id=run_id,
                    phase=chunk[0].kind,
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
        *,
        run_id: str = "",
        phase: str = "apply",
    ) -> tuple[int, int]:
        self._require_claim(manifest_id)
        guarded_chunk, guarded_applied, guarded_failed = await self._guard_course_creates(
            manifest_id,
            chunk,
            owner_ids,
            blocked_courses,
        )
        if not guarded_chunk:
            return guarded_applied, guarded_failed
        chunk = guarded_chunk
        durable_batch = None
        worker_count = self._adaptive_workers.worker_count
        if run_id:
            durable_batch = self.store.prepare_execution_batch(
                run_id,
                manifest_id,
                chunk,
                phase=phase,
                owner_id=self._operation_owner,
            )
            durable_batch = self.store.mark_execution_batch_started(
                durable_batch.id,
                worker_count=worker_count,
            )
        commands = [_command_for(action) for action in chunk]
        batch_failed = False
        throttling_count = 0
        apply_started = time.perf_counter()
        batch_receipt: Optional[BatchExecutionReceipt] = None
        heartbeat_task: Optional[asyncio.Task[None]] = None
        if run_id:
            async def pulse() -> None:
                while True:
                    await asyncio.sleep(EXECUTION_HEARTBEAT_INTERVAL_SECONDS)
                    await asyncio.to_thread(
                        self.store.heartbeat_execution_run,
                        run_id,
                        phase=phase,
                    )

            heartbeat_task = asyncio.create_task(pulse())
        try:
            batch_receipt = await self._run_classroom_batch(
                commands,
                worker_count=worker_count,
            )
        except asyncio.CancelledError:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            raise
        except Exception as exc:
            batch_failed = True
            throttling_count = int(
                isinstance(exc, GAMError) and exc.kind is GAMErrorKind.RATE_LIMITED
            )
        apply_seconds = (
            float(batch_receipt.duration_seconds)
            if batch_receipt is not None
            else time.perf_counter() - apply_started
        )
        if batch_receipt is not None:
            throttling_count = int(batch_receipt.throttling_count)
        try:
            verified, verification_attempts, verification_seconds = (
                await self._verify_with_retries(chunk, owner_ids)
            )
            applied = guarded_applied
            failed = guarded_failed
            results: dict[str, tuple[str, str]] = {}
            for action in chunk:
                ok = bool(verified.get(action.id))
                if ok:
                    results[action.id] = (
                        "applied",
                        (
                            "Verified live after GAM reported a batch error."
                            if batch_failed
                            else "Verified against live Classroom state."
                        ),
                    )
                    applied += 1
                else:
                    results[action.id] = ("failed", "OR-BATCH-VERIFY-FAILED")
                    failed += 1
                    if action.kind in {
                        "course_create",
                        "teacher_add",
                        "owner_transfer",
                        "course_activate",
                    }:
                        blocked_courses.add(action.subject.casefold())
            if durable_batch is not None:
                completed_batch = self.store.complete_verified_batch(
                    durable_batch.id,
                    manifest_id,
                    results,
                    owner_id=self._operation_owner,
                    apply_seconds=apply_seconds,
                    verification_seconds=verification_seconds,
                    verification_attempts=verification_attempts,
                    worker_count=worker_count,
                    throttling_count=throttling_count,
                )
                persistence_seconds = completed_batch.persistence_seconds
            else:
                persistence_started = time.perf_counter()
                for action_id, (status, detail) in results.items():
                    self.store.mark_action_result(
                        manifest_id,
                        action_id,
                        status=status,
                        detail=detail,
                        load_manifest=False,
                        owner_id=self._operation_owner,
                    )
                persistence_seconds = time.perf_counter() - persistence_started
            self._adaptive_workers.observe(
                action_count=len(chunk),
                duration_seconds=apply_seconds,
                batch_failed=batch_failed,
                throttling_count=throttling_count,
                verification_attempts=verification_attempts,
            )
            self._record_performance_audit(
                "oneroster_batch_performance",
                target=f"{len(chunk)} actions",
                extra={
                    "apply_seconds": apply_seconds,
                    "verification_seconds": verification_seconds,
                    "persistence_seconds": persistence_seconds,
                    "verification_attempts": verification_attempts,
                    "worker_count": worker_count,
                    "next_worker_count": self._adaptive_workers.worker_count,
                    "throttling_count": throttling_count,
                    "outcome": "failed" if failed else "completed",
                    "actions_per_minute": (
                        len(chunk)
                        * 60.0
                        / max(
                            0.001,
                            apply_seconds
                            + verification_seconds
                            + persistence_seconds,
                        )
                    ),
                },
            )
            return applied, failed
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _run_classroom_batch(
        self,
        commands: Sequence[Sequence[str]],
        *,
        worker_count: int,
    ) -> BatchExecutionReceipt:
        method = self.connector.run_classroom_batch
        supports_worker_count = False
        try:
            parameters = inspect.signature(method).parameters.values()
            supports_worker_count = any(
                parameter.name == "worker_count"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            pass
        started = time.perf_counter()
        kwargs: dict[str, Any] = {"max_commands": self.batch_size}
        if supports_worker_count:
            kwargs["worker_count"] = worker_count
        receipt = await method(commands, **kwargs)
        if isinstance(receipt, BatchExecutionReceipt):
            return receipt
        return BatchExecutionReceipt(
            duration_seconds=time.perf_counter() - started,
            worker_count=worker_count,
            outcome="completed",
        )

    async def _verify_with_retries(
        self,
        actions: Sequence[ImportAction],
        owner_ids: Optional[Mapping[str, str]],
    ) -> tuple[dict[str, bool], int, float]:
        started = time.perf_counter()
        remaining = list(actions)
        verified = {action.id: False for action in actions}
        attempts = 0
        retry_offsets = (0.0, *self.stabilization_delays)
        for retry_offset in retry_offsets:
            if not remaining:
                break
            wait_seconds = retry_offset - (time.perf_counter() - started)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            result = await self._verify(remaining, owner_ids)
            attempts += 1
            next_remaining: list[ImportAction] = []
            for action in remaining:
                if bool(result.get(action.id)):
                    verified[action.id] = True
                else:
                    next_remaining.append(action)
            remaining = next_remaining
        return verified, attempts, time.perf_counter() - started

    def _record_performance_audit(
        self,
        action: str,
        *,
        target: str,
        extra: Mapping[str, Any],
    ) -> None:
        audit = getattr(self.connector, "audit", None)
        record = getattr(audit, "record", None)
        if not callable(record):
            return
        sanitized = {
            str(key): round(value, 3) if isinstance(value, float) else value
            for key, value in extra.items()
            if isinstance(value, (str, int, float, bool))
        }
        record(
            action,
            target=target,
            argv=("oneroster", "performance"),
            ok=True,
            extra=sanitized,
        )

    async def _guard_course_creates(
        self,
        manifest_id: str,
        chunk: Sequence[ImportAction],
        owner_ids: Optional[Mapping[str, str]],
        blocked_courses: set[str],
    ) -> tuple[list[ImportAction], int, int]:
        """Perform the final exact-alias check immediately before course creation."""

        creates = [action for action in chunk if action.kind == "course_create"]
        if not creates:
            return list(chunk), 0, 0
        aliases = tuple(action.subject for action in creates)
        details: dict[str, Any] = {}
        bulk = getattr(self.connector, "list_oneroster_managed_courses", None)
        if callable(bulk):
            try:
                rows = await bulk(aliases)
            except Exception as exc:
                raise OneRosterError(
                    "OR-CLASSROOM-READ",
                    "Final exact-alias lookup failed before course creation.",
                ) from exc
            for detail in rows:
                matches = [alias for alias in aliases if _has_alias(detail, alias)]
                if len(matches) != 1 or matches[0].casefold() in details:
                    raise OneRosterError(
                        "OR-ALIAS-AMBIGUOUS",
                        "Final exact-alias lookup returned an ambiguous managed course.",
                    )
                details[matches[0].casefold()] = detail
        else:
            for alias in aliases:
                try:
                    detail = await self.connector.get_course(
                        _course_ref(alias),
                        include_owner_email=True,
                        include_aliases=True,
                        best_effort_enrichment=False,
                    )
                except KeyError:
                    continue
                except Exception as exc:
                    raise OneRosterError(
                        "OR-CLASSROOM-READ",
                        "Final exact-alias lookup failed before course creation.",
                    ) from exc
                if not _has_alias(detail, alias):
                    raise OneRosterError(
                        "OR-ALIAS-AMBIGUOUS",
                        "Course lookup did not prove the exact managed alias.",
                    )
                details[alias.casefold()] = detail

        runnable: list[ImportAction] = []
        applied = failed = 0
        for action in chunk:
            if action.kind != "course_create":
                runnable.append(action)
                continue
            detail = details.get(action.subject.casefold())
            if detail is None:
                runnable.append(action)
                continue
            if _verify_action(action, detail, None, owner_ids or {}):
                self.store.mark_action_result(
                    manifest_id,
                    action.id,
                    status="applied",
                    detail="Exact alias already existed and matched immediately before create.",
                    load_manifest=False,
                    owner_id=self._operation_owner,
                )
                applied += 1
            else:
                self.store.mark_action_result(
                    manifest_id,
                    action.id,
                    status="failed",
                    detail="OR-ALIAS-CONFLICT",
                    load_manifest=False,
                    owner_id=self._operation_owner,
                )
                blocked_courses.add(action.subject.casefold())
                failed += 1
        return runnable, applied, failed

    def _require_claim(self, manifest_id: str) -> None:
        if not self.store.owns_claim(manifest_id, self._operation_owner):
            raise OneRosterError(
                "OR-MANIFEST-LEASE-LOST",
                "The OneRoster execution lease was lost; no further changes were attempted.",
            )

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
                if any(
                    not bool(getattr(item, "identity_resolved", True))
                    for item in (*teachers, *students)
                ):
                    # A course-only GAM row is not proof of a complete roster.
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


def _planning_observation_hash(planning: LivePlanningResult) -> str:
    """Hash every decision-bearing field from one post-write observation."""

    return canonical_hash(
        {
            "source_hash": planning.source_hash,
            "config_hash": planning.config_hash,
            "live_hash": planning.live_hash,
            "actions": [action.basis_dict() for action in planning.actions],
            "archive_actions": [
                action.basis_dict() for action in planning.archive_actions
            ],
            "ownership_actions": [
                action.basis_dict() for action in planning.ownership_actions
            ],
            "issues": _issue_evidence(planning.issues),
            "thresholds": _threshold_drift_evidence(
                _threshold_evidence(planning.threshold_evaluation)
            ),
        }
    )


def _live_evidence_diff(
    approved: Mapping[str, Any],
    current: Mapping[str, Any],
) -> tuple[dict[str, str], ...]:
    categories = {
        "id": "course identity changed",
        "aliases": "course identity changed",
        "exists": "course identity changed",
        "name": "metadata changed",
        "section": "metadata changed",
        "room": "metadata changed",
        "owner_email": "owner changed",
        "teachers": "teacher roster changed",
        "students": "student roster changed",
        "state": "course state changed",
        "unresolved_members": "roster identity changed",
    }
    rows: list[dict[str, str]] = []
    aliases = sorted(
        (set(approved) | set(current)) - {"__truncated__"},
        key=str.casefold,
    )
    for alias in aliases:
        before = approved.get(alias, {})
        after = current.get(alias, {})
        if not isinstance(before, Mapping):
            before = {}
        if not isinstance(after, Mapping):
            after = {}
        for field in sorted(set(before) | set(after)):
            if field == "alias" or before.get(field) == after.get(field):
                continue
            rows.append(
                {
                    "category": categories.get(field, "live state changed"),
                    "course": alias,
                    "field": field,
                    "approved": json.dumps(before.get(field, ""), ensure_ascii=False),
                    "current": json.dumps(after.get(field, ""), ensure_ascii=False),
                }
            )
            if len(rows) >= 100:
                return tuple(rows)
    return tuple(rows)
