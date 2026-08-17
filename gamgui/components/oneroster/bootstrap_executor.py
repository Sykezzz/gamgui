"""Additions-first emergency execution for one strictly validated paused checkpoint."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence

from gamgui.core.activity import ActivityBusyError, ActivityRegistry
from gamgui.core.classroom.models import CourseRosterSnapshot
from gamgui.core.processes import current_process_identity

from .executor import _command_for, _has_alias, _payload, _text
from .models import (
    ClassroomImportManifest,
    ExecutionBatch,
    ExecutionSummary,
    GateState,
    ImportAction,
    OneRosterError,
    SnapshotState,
)
from .planner import planner_configuration_hash
from .store import OneRosterStore


BOOTSTRAP_PHASES = (
    "course_create",
    "student_add",
    "course_activate",
    "teacher_add",
)
BOOTSTRAP_HEARTBEAT_SECONDS = 5.0
BOOTSTRAP_RETRY_DELAYS = (2.0, 5.0)
BOOTSTRAP_ACTIVITY_WAIT_SECONDS = 30 * 60.0


class AdditionsFirstConnector(Protocol):
    async def run_classroom_phase_batch(
        self,
        commands: Iterable[Sequence[str]],
        *,
        progress_callback: Any = None,
    ) -> Any: ...

    async def snapshot_oneroster_managed_courses(
        self,
        aliases: Sequence[str],
    ) -> Sequence[Any]: ...

    async def list_oneroster_directory(self) -> Mapping[str, Any]: ...

    async def list_course_participants_many(
        self,
        course_ids: Sequence[str],
        role: str = "all",
    ) -> CourseRosterSnapshot: ...


@dataclass(frozen=True)
class _ReconciliationSnapshot:
    courses: Mapping[str, Any]
    directory: Mapping[str, Any]
    rosters: CourseRosterSnapshot
    create_actions: Mapping[str, ImportAction]


class AdditionsFirstBootstrapExecutor:
    """Resume an additive checkpoint without rebuilding its live plan."""

    def __init__(
        self,
        store: OneRosterStore,
        connector: AdditionsFirstConnector,
        *,
        activity_registry: Optional[ActivityRegistry] = None,
        retry_delays: Optional[Sequence[float]] = None,
        activity_wait_seconds: float = BOOTSTRAP_ACTIVITY_WAIT_SECONDS,
    ) -> None:
        self.store = store
        self.connector = connector
        self.activity_registry = activity_registry
        configured = (
            retry_delays
            if retry_delays is not None
            else getattr(connector, "oneroster_stabilization_delays", BOOTSTRAP_RETRY_DELAYS)
        )
        values = tuple(max(0.0, float(value)) for value in configured)
        self.retry_delays = (
            values[:2] if len(values) >= 2 else BOOTSTRAP_RETRY_DELAYS
        )
        self.activity_wait_seconds = max(0.0, float(activity_wait_seconds))
        self._operation_owner = f"{os.getpid()}:{secrets.token_urlsafe(12)}"
        self._operation_identity = current_process_identity()

    async def execute(
        self,
        manifest_id: str,
        *,
        import_id_ack: str,
        deferred_verification_ack: bool,
        now: Optional[datetime] = None,
    ) -> ExecutionSummary:
        manifest = self.store.get_manifest_header(manifest_id)
        if import_id_ack.strip() != manifest.import_id:
            raise OneRosterError(
                "OR-CONFIRMATION-MISMATCH",
                "The typed import ID does not match this immutable manifest.",
            )
        if deferred_verification_ack is not True:
            raise OneRosterError(
                "OR-DEFERRED-VERIFICATION-ACK-REQUIRED",
                "Acknowledge deferred verification before starting additions-first bootstrap.",
            )

        lease = await self._activity_lease("oneroster-additions-first")
        with lease:
            checkpoint = self.store.validate_additions_first_checkpoint(manifest_id)
            await self._validate_local_basis(manifest, now=now)
            if self.store.get_gate().state is not GateState.CLOSED:
                raise OneRosterError(
                    "OR-BOOTSTRAP-GATE-NOT-CLOSED",
                    "The student safety gate must remain closed for additions-first bootstrap.",
                )
            manifest = self.store.claim_manifest(
                manifest_id,
                allow_paused=True,
                owner_id=self._operation_owner,
                owner_pid=os.getpid(),
                owner_identity=self._operation_identity,
            )
            run = self.store.start_execution_run(
                manifest.id,
                phase="bootstrap",
                owner_id=self._operation_owner,
            )
            self.store.clear_bootstrap_missing_report(manifest.id)
            current_batch: Optional[ExecutionBatch] = None
            try:
                for batch_id in checkpoint.get("submitted_batch_ids", ()):
                    self.store.attach_paused_additions_first_reconciliation(
                        str(batch_id),
                        manifest.id,
                        run.id,
                        owner_id=self._operation_owner,
                    )

                for cycle in range(3):
                    if cycle:
                        await asyncio.sleep(self.retry_delays[cycle - 1])
                    for phase in BOOTSTRAP_PHASES:
                        if self.store.execution_stop_requested(run.id):
                            return self._pause(manifest.id, run.id)
                        self._require_claim(manifest.id)
                        current_batch = self.store.prepare_execution_phase(
                            run.id,
                            manifest.id,
                            phase=phase,
                            kinds=(phase,),
                            owner_id=self._operation_owner,
                        )
                        if current_batch is None:
                            continue
                        current_batch = self.store.mark_execution_phase_started(
                            current_batch.id,
                            owner_id=self._operation_owner,
                        )
                        await self._submit_phase(current_batch, manifest.id, run.id)
                        current_batch = None

                    if self.store.execution_stop_requested(run.id):
                        return self._pause(manifest.id, run.id)

                    batches = self.store.get_pending_additions_first_phase_batches(
                        manifest.id
                    )
                    if batches:
                        await self._reconcile_batches(
                            manifest,
                            batches,
                            run_id=run.id,
                            cycle=cycle,
                            terminal_missing=cycle == 2,
                        )
                    counts = self.store.manifest_action_counts(manifest.id)
                    if not counts.get("pending", 0) and not counts.get("submitted", 0):
                        return self._finish(manifest.id, run.id)

                return self._finish(manifest.id, run.id)
            except BaseException as exc:
                await self._enter_recovery(
                    manifest.id,
                    run.id,
                    current_batch,
                    exc,
                )
                raise

    async def reconcile_interrupted(self, manifest_id: str) -> ExecutionSummary:
        """Reconcile uncertain native phases without sending another mutation."""

        lease = await self._activity_lease("oneroster-bootstrap-recovery")
        with lease:
            manifest = self.store.get_manifest_header(manifest_id)
            if manifest.status != "recovery_required":
                raise OneRosterError(
                    "OR-RECOVERY-NOT-AVAILABLE",
                    "This manifest has no interrupted additions-first phase.",
                )
            manifest = self.store.claim_manifest(
                manifest_id,
                allow_recovery=True,
                owner_id=self._operation_owner,
                owner_pid=os.getpid(),
                owner_identity=self._operation_identity,
            )
            run = self.store.claim_execution_recovery(
                manifest.id,
                owner_id=self._operation_owner,
            )
            try:
                batches = self.store.get_reconciling_phase_batches(manifest.id)
                if not batches:
                    raise OneRosterError(
                        "OR-RECOVERY-EVIDENCE-MISSING",
                        "No additions-first phase is available for reconciliation.",
                    )
                snapshot = await self._read_reconciliation_snapshot(
                    manifest.id,
                    batches,
                    run.id,
                )
                await self._persist_reconciliation(
                    manifest,
                    batches,
                    snapshot,
                    cycle=0,
                    terminal_missing=False,
                )
                counts = self.store.manifest_action_counts(manifest.id)
                if counts.get("failed", 0) or counts.get("skipped", 0):
                    return self._finish(manifest.id, run.id)
                if counts.get("pending", 0):
                    self.store.request_execution_pause(manifest.id)
                    return self._pause(manifest.id, run.id)
                return self._finish(manifest.id, run.id)
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

    async def _activity_lease(self, operation: str):
        if self.activity_registry is None:
            return nullcontext()
        deadline = time.monotonic() + self.activity_wait_seconds
        while True:
            try:
                return self.activity_registry.acquire(operation)
            except ActivityBusyError as exc:
                if time.monotonic() >= deadline:
                    raise OneRosterError(
                        "OR-ACTIVE-JOB",
                        "Another updater, connector, or administrative job remained active.",
                    ) from exc
                await asyncio.sleep(min(1.0, max(0.05, self.activity_wait_seconds)))

    async def _validate_local_basis(
        self,
        manifest: ClassroomImportManifest,
        *,
        now: Optional[datetime],
    ) -> None:
        snapshot = self.store.get_import(manifest.import_id)
        if snapshot.state is not SnapshotState.READY:
            raise OneRosterError(
                "OR-IMPORT-NOT-READY",
                "The retained OneRoster source is no longer in its approved ready state.",
            )
        raw_path = self.store.raw_path(manifest.import_id)
        normalized_path = self.store.normalized_path(manifest.import_id)
        if not raw_path.is_file() or not normalized_path.is_file():
            raise OneRosterError(
                "OR-IMPORT-EXPIRED",
                "The retained source material for this checkpoint is unavailable.",
            )
        source_hash = await asyncio.to_thread(_sha256_file, raw_path)
        if source_hash != manifest.source_hash or snapshot.source_sha256 != manifest.source_hash:
            raise OneRosterError(
                "OR-SOURCE-DRIFT",
                "The retained OneRoster source no longer matches this checkpoint.",
            )
        profile = self.store.get_threshold_profile()
        schedule_scope = self.store.schedule_scope(manifest.import_id)
        config_hash = planner_configuration_hash(
            limited_import=True,
            course_name_template=snapshot.course_name_template,
            threshold_profile=profile.to_dict(),
            schedule_scope=schedule_scope,
        )
        if config_hash != manifest.config_hash:
            raise OneRosterError(
                "OR-CONFIG-DRIFT",
                "District import configuration changed after this checkpoint was approved.",
            )
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("Bootstrap validation requires a timezone-aware time.")
        if any(window.contains(moment) for window in profile.blackouts):
            raise OneRosterError(
                "OR-THRESHOLD-HOLD",
                "The additions-first bootstrap is inside a configured blackout period.",
            )

    async def _submit_phase(
        self,
        batch: ExecutionBatch,
        manifest_id: str,
        run_id: str,
    ) -> None:
        async def progress(item: Any) -> None:
            self.store.record_execution_phase_progress(
                batch.id,
                int(item.dispatched),
                int(item.total),
                owner_id=self._operation_owner,
            )

        heartbeat = asyncio.create_task(self._heartbeat(run_id, batch.phase))
        try:
            receipt = await self.connector.run_classroom_phase_batch(
                self._phase_commands(batch),
                progress_callback=progress,
            )
            if int(receipt.submitted_count) != batch.action_count:
                raise OneRosterError(
                    "OR-BATCH-ACTIONS-CHANGED",
                    "Native GAM did not receive every durable phase action.",
                )
            self.store.mark_execution_phase_submitted(
                batch.id,
                manifest_id,
                owner_id=self._operation_owner,
                apply_seconds=float(receipt.duration_seconds),
                worker_count=int(receipt.worker_count),
            )
        except BaseException as exc:
            with suppress(Exception):
                self.store.mark_execution_phase_reconciling(
                    batch.id,
                    error_code=str(
                        _error_code(exc, "OR-EXECUTION-INTERRUPTED")
                    ),
                    owner_id=self._operation_owner,
                )
            raise
        finally:
            await _stop_heartbeat(heartbeat)

    def _phase_commands(self, batch: ExecutionBatch) -> Iterable[Sequence[str]]:
        after = -1
        while page := self.store.get_execution_phase_action_chunk(
            batch.id,
            after_ordinal=after,
            limit=500,
        ):
            for ordinal, action in page:
                if action.kind != batch.phase or action.status != "pending":
                    raise OneRosterError(
                        "OR-BATCH-ACTIONS-CHANGED",
                        "Durable phase membership no longer matches pending actions.",
                    )
                yield _command_for(action)
                after = ordinal

    async def _heartbeat(self, run_id: str, phase: str) -> None:
        while True:
            await asyncio.sleep(BOOTSTRAP_HEARTBEAT_SECONDS)
            await asyncio.to_thread(
                self.store.heartbeat_execution_run,
                run_id,
                phase=phase,
            )

    async def _reconcile_batches(
        self,
        manifest: ClassroomImportManifest,
        batches: Sequence[ExecutionBatch],
        *,
        run_id: str,
        cycle: int,
        terminal_missing: bool,
    ) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(run_id, "reconciliation"))
        try:
            snapshot = await self._read_reconciliation_snapshot(
                manifest.id,
                batches,
                run_id,
            )
            await self._persist_reconciliation(
                manifest,
                batches,
                snapshot,
                cycle=cycle,
                terminal_missing=terminal_missing,
            )
        finally:
            await _stop_heartbeat(heartbeat)

    async def _read_reconciliation_snapshot(
        self,
        manifest_id: str,
        batches: Sequence[ExecutionBatch],
        run_id: str,
    ) -> _ReconciliationSnapshot:
        del run_id
        aliases: set[str] = set()
        for batch in batches:
            after = -1
            while page := self.store.get_execution_phase_action_chunk(
                batch.id,
                after_ordinal=after,
                limit=500,
                result_statuses=("submitted",),
            ):
                for ordinal, action in page:
                    aliases.add(action.subject)
                    after = ordinal
        create_actions: dict[str, ImportAction] = {}
        for chunk in self.store.iter_manifest_action_chunks(
            manifest_id,
            chunk_size=500,
            kinds=("course_create",),
        ):
            for action in chunk:
                key = action.subject.casefold()
                if key in create_actions:
                    raise OneRosterError(
                        "OR-ALIAS-AMBIGUOUS",
                        "The bootstrap manifest contains duplicate course identities.",
                    )
                create_actions[key] = action

        course_task = asyncio.create_task(
            self.connector.snapshot_oneroster_managed_courses(sorted(aliases))
        )
        directory_task = asyncio.create_task(self.connector.list_oneroster_directory())
        courses_raw, directory_raw = await asyncio.gather(course_task, directory_task)
        requested_aliases = {alias.casefold() for alias in aliases}
        courses: dict[str, Any] = {}
        for detail in courses_raw:
            matches = _matching_requested_aliases(detail, requested_aliases)
            if not matches:
                continue
            if len(matches) != 1 or next(iter(matches)) in courses:
                raise OneRosterError(
                    "OR-ALIAS-AMBIGUOUS",
                    "The tenant course inventory returned an ambiguous managed alias.",
                )
            courses[next(iter(matches))] = detail
        if not isinstance(directory_raw, Mapping):
            raise OneRosterError(
                "OR-DIRECTORY-READ",
                "The Directory snapshot was incomplete.",
            )
        directory = {
            str(key).casefold(): value for key, value in directory_raw.items()
        }
        roster_course_ids: set[str] = set()
        for batch in batches:
            if batch.phase not in {"teacher_add", "student_add"}:
                continue
            after = -1
            while page := self.store.get_execution_phase_action_chunk(
                batch.id,
                after_ordinal=after,
                limit=500,
                result_statuses=("submitted",),
            ):
                for ordinal, action in page:
                    detail = courses.get(action.subject.casefold())
                    course_id = _text(detail, "id") if detail is not None else ""
                    if course_id:
                        roster_course_ids.add(course_id)
                    after = ordinal
        requested_ids = tuple(sorted(roster_course_ids))
        rosters = (
            await self.connector.list_course_participants_many(requested_ids, "all")
            if requested_ids
            else CourseRosterSnapshot.empty()
        )
        if not isinstance(rosters, CourseRosterSnapshot) or not rosters.covers(requested_ids):
            raise OneRosterError(
                "OR-CLASSROOM-READ",
                "The bulk Classroom roster snapshot did not cover every requested course.",
            )
        return _ReconciliationSnapshot(
            courses=courses,
            directory=directory,
            rosters=rosters,
            create_actions=create_actions,
        )

    async def _persist_reconciliation(
        self,
        manifest: ClassroomImportManifest,
        batches: Sequence[ExecutionBatch],
        snapshot: _ReconciliationSnapshot,
        *,
        cycle: int,
        terminal_missing: bool,
    ) -> None:
        del manifest
        for batch in batches:
            started = time.perf_counter()
            after = -1
            attempts = cycle + 1
            while page := self.store.get_execution_phase_action_chunk(
                batch.id,
                after_ordinal=after,
                limit=500,
                result_statuses=("submitted",),
            ):
                results: dict[str, tuple[str, str]] = {}
                missing_rows: list[tuple[str, str, int]] = []
                for ordinal, action in page:
                    status, detail = _classify_action(
                        action,
                        snapshot,
                        terminal_missing=terminal_missing,
                    )
                    results[action.id] = (status, detail)
                    if status == "failed":
                        missing_rows.append((action.id, detail, cycle))
                    after = ordinal
                self.store.promote_submitted_phase_actions(
                    batch.id,
                    batch.manifest_id,
                    results,
                    owner_id=self._operation_owner,
                )
                if missing_rows:
                    self.store.upsert_bootstrap_missing_report(
                        batch.manifest_id,
                        missing_rows,
                    )
            self.store.finish_execution_phase_reconciliation(
                batch.id,
                batch.manifest_id,
                owner_id=self._operation_owner,
                verification_seconds=time.perf_counter() - started,
                verification_attempts=attempts,
                error_code="OR-BOOTSTRAP-MISSING",
            )

    async def _enter_recovery(
        self,
        manifest_id: str,
        run_id: str,
        batch: Optional[ExecutionBatch],
        exc: BaseException,
    ) -> None:
        code = _error_code(exc, "OR-RECOVERY-REQUIRED")
        if batch is not None:
            with suppress(Exception):
                self.store.mark_execution_phase_reconciling(
                    batch.id,
                    error_code=code,
                    owner_id=self._operation_owner,
                )
        for pending in self.store.get_pending_additions_first_phase_batches(manifest_id):
            with suppress(Exception):
                self.store.mark_execution_phase_reconciling(
                    pending.id,
                    error_code=code,
                    owner_id=self._operation_owner,
                )
        with suppress(Exception):
            self.store.finish_manifest(
                manifest_id,
                status="recovery_required",
                error="OR-RECOVERY-REQUIRED",
                load_actions=False,
                owner_id=self._operation_owner,
            )
        with suppress(Exception):
            self.store.finish_execution_run(
                run_id,
                status="recovery_required",
                error_code="OR-RECOVERY-REQUIRED",
                phase="reconciliation",
            )

    def _pause(self, manifest_id: str, run_id: str) -> ExecutionSummary:
        current = self.store.finish_manifest(
            manifest_id,
            status="paused",
            error="OR-BOOTSTRAP-PAUSED",
            load_actions=False,
            owner_id=self._operation_owner,
        )
        self.store.finish_execution_run(
            run_id,
            status="paused",
            error_code="OR-BOOTSTRAP-PAUSED",
            phase="paused",
        )
        return self._summary(current)

    def _finish(self, manifest_id: str, run_id: str) -> ExecutionSummary:
        counts = self.store.manifest_action_counts(manifest_id)
        unfinished = int(counts.get("pending", 0)) + int(counts.get("submitted", 0))
        failures = int(counts.get("failed", 0)) + int(counts.get("skipped", 0))
        applied = int(counts.get("applied", 0))
        if not unfinished and not failures:
            status, error, run_status = "completed", "", "completed"
        else:
            status = "partial" if applied or unfinished else "failed"
            error, run_status = "OR-BOOTSTRAP-MISSING", "failed"
            if unfinished:
                for chunk in self.store.iter_manifest_action_chunks(
                    manifest_id,
                    chunk_size=500,
                    statuses=("pending", "submitted"),
                ):
                    self.store.upsert_bootstrap_missing_report(
                        manifest_id,
                        (
                            (
                                action.id,
                                "OR-BOOTSTRAP-NOT-ATTEMPTED"
                                if action.status == "pending"
                                else "OR-BOOTSTRAP-RECONCILIATION-INCOMPLETE",
                                2,
                            )
                            for action in chunk
                        ),
                    )
        current = self.store.finish_manifest(
            manifest_id,
            status=status,
            error=error,
            load_actions=False,
            owner_id=self._operation_owner,
        )
        self.store.finish_execution_run(
            run_id,
            status=run_status,
            error_code=error,
            phase=status,
        )
        if status == "completed":
            self.store.maybe_mark_import_accepted(manifest_id)
        return self._summary(current)

    def _summary(self, manifest: ClassroomImportManifest) -> ExecutionSummary:
        counts = self.store.manifest_action_counts(manifest.id)
        return ExecutionSummary(
            manifest=manifest,
            applied=int(counts.get("applied", 0)),
            failed=int(counts.get("failed", 0)),
            skipped=int(counts.get("skipped", 0)),
            awaiting_students=False,
        )

    def _require_claim(self, manifest_id: str) -> None:
        if not self.store.owns_claim(manifest_id, self._operation_owner):
            raise OneRosterError(
                "OR-MANIFEST-LEASE-LOST",
                "The additions-first execution lease was lost.",
            )


def _classify_action(
    action: ImportAction,
    snapshot: _ReconciliationSnapshot,
    *,
    terminal_missing: bool,
) -> tuple[str, str]:
    alias = action.subject.casefold()
    detail = snapshot.courses.get(alias)
    create = snapshot.create_actions.get(alias)
    if create is None:
        return "failed", "OR-BOOTSTRAP-CREATE-EVIDENCE-MISSING"
    course_status, course_reason = _classify_course(create, detail, snapshot.directory)
    if course_status == "conflict":
        return "failed", course_reason
    if course_status == "missing":
        return _missing_result("OR-BOOTSTRAP-COURSE-MISSING", terminal_missing)
    if action.kind == "course_create":
        return "applied", "Verified course alias, name, and active owner."
    if action.kind == "course_activate":
        if _text(detail, "course_state", "courseState").upper() == "ACTIVE":
            return "applied", "Verified active Classroom state."
        return _missing_result("OR-BOOTSTRAP-ACTIVATION-MISSING", terminal_missing)
    if action.kind not in {"teacher_add", "student_add"}:
        return "failed", "OR-ACTION-UNSUPPORTED"
    identity = snapshot.directory.get(action.target.casefold())
    canonical = _directory_identity(identity)
    if not canonical or bool(getattr(identity, "suspended", False)):
        return "failed", "OR-BOOTSTRAP-DIRECTORY-IDENTITY"
    course_id = _text(detail, "id")
    teachers, students = snapshot.rosters.for_course(course_id)
    members = teachers if action.kind == "teacher_add" else students
    if canonical in members:
        return "applied", "Verified in the live Classroom roster."
    return _missing_result("OR-BOOTSTRAP-ROSTER-MISSING", terminal_missing)


def _classify_course(
    create: ImportAction,
    detail: Any,
    directory: Mapping[str, Any],
) -> tuple[str, str]:
    if detail is None:
        return "missing", "OR-BOOTSTRAP-COURSE-MISSING"
    if not _has_alias(detail, create.subject):
        return "conflict", "OR-ALIAS-CONFLICT"
    payload = _payload(create.after)
    if _text(detail, "name") != str(payload.get("name") or ""):
        return "conflict", "OR-ALIAS-CONFLICT-NAME"
    expected_owner = str(payload.get("owner_email") or "").casefold()
    identity = directory.get(expected_owner)
    canonical = _directory_identity(identity)
    if not canonical or bool(getattr(identity, "suspended", False)):
        return "conflict", "OR-BOOTSTRAP-OWNER-NOT-ACTIVE"
    expected_owner_id = _text(identity, "user_id", "id")
    actual_owner_id = _text(detail, "owner_id", "ownerId")
    actual_owner_email = _text(detail, "owner_email", "ownerEmail").casefold()
    if expected_owner_id:
        owner_matches = actual_owner_id == expected_owner_id
    else:
        owner_matches = actual_owner_email == canonical
    if not owner_matches:
        return "conflict", "OR-ALIAS-CONFLICT-OWNER"
    state = _text(detail, "course_state", "courseState").upper()
    if state not in {"PROVISIONED", "ACTIVE"}:
        return "conflict", "OR-ALIAS-CONFLICT-STATE"
    return "match", ""


def _directory_identity(value: Any) -> str:
    return _text(value, "primary_email", "primaryEmail", "email").casefold()


def _matching_requested_aliases(
    detail: Any,
    requested: set[str],
) -> set[str]:
    if isinstance(detail, Mapping):
        raw = detail.get("aliases", detail.get("alias", ()))
    else:
        raw = getattr(detail, "aliases", ())
    aliases = raw if isinstance(raw, Sequence) and not isinstance(raw, str) else str(raw).split(",")
    matches: set[str] = set()
    for value in aliases:
        normalized = str(value or "").strip().casefold()
        if normalized.startswith("d:"):
            normalized = normalized[2:]
        if normalized in requested:
            matches.add(normalized)
    return matches


def _error_code(exc: BaseException, default: str) -> str:
    return str(
        getattr(exc, "code", "")
        or getattr(exc, "error_code", "")
        or default
    )


async def _stop_heartbeat(task: asyncio.Task[Any]) -> None:
    task.cancel()
    # Phase persistence and claim checks remain authoritative. A background
    # heartbeat failure must never replace the native/reconciliation exception.
    with suppress(asyncio.CancelledError, Exception):
        await task


def _missing_result(reason: str, terminal: bool) -> tuple[str, str]:
    return ("failed", reason) if terminal else ("pending", reason)


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
