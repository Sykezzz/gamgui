"""Credential-free application facade for OneRoster Import Studio."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Optional, Sequence, TextIO

from gamgui.core.activity import ActivityRegistry
from gamgui.core.setup import ONEROSTER_DWD_SCOPES

from .ingest import SafetyLimits, copy_upload, ingest_archive
from .models import (
    ClassroomImportManifest,
    DashboardStatus,
    GateState,
    ImportAction,
    ExecutionSummary,
    LivePlanningResult,
    MAX_PAGE_SIZE,
    ManifestPage,
    OneRosterError,
    OneRosterSnapshot,
    PreviewPage,
    PurgePreview,
    PlannedManifestSet,
    SCOPE_READINESS_TTL_SECONDS,
    ScopeReadiness,
    StudentEnrollmentGate,
    ThresholdEvaluation,
    ThresholdDenial,
    ThresholdOverride,
    ThresholdProfile,
    canonical_hash,
)
from .store import OneRosterStore
from .thresholds import evaluate_thresholds as run_threshold_evaluation
from .thresholds import evaluation_hash


class OneRosterService:
    """Local-only OneRoster workflow API.

    Construction and every status/configuration method are deliberately free of GAM,
    Google, Directory, and Keychain access. Later executor integration must resolve
    normalized emails through the live Directory before creating an apply manifest.
    """

    def __init__(
        self,
        domain: str,
        root: Optional[Path] = None,
        *,
        safety_limits: SafetyLimits = SafetyLimits(),
        activity_registry: Optional[ActivityRegistry] = None,
    ) -> None:
        self.store = OneRosterStore(domain, root)
        self.safety_limits = safety_limits
        self.activity_registry = activity_registry
        self.store.mark_running_interrupted()

    @property
    def domain(self) -> str:
        return self.store.domain

    def dashboard(self) -> DashboardStatus:
        return self.store.dashboard()

    def upload(
        self,
        source: BinaryIO | bytes | bytearray | Path | str,
        filename: str = "OneRoster.zip",
    ) -> OneRosterSnapshot:
        """Copy, validate, and normalize an uploaded package eagerly."""

        import_id, snapshot_dir = self.store.new_import(filename)
        try:
            try:
                source_hash = copy_upload(
                    source,
                    self.store.raw_path(import_id),
                    limits=self.safety_limits,
                )
            except ValueError as exc:
                raise OneRosterError("OR-ZIP-SIZE", str(exc)) from exc
            result = ingest_archive(
                self.store.raw_path(import_id),
                self.store.normalized_path(import_id),
                domain=self.domain,
                source_sha256=source_hash,
                limits=self.safety_limits,
            )
            return self.store.complete_import(
                import_id,
                source_sha256=result.source_sha256,
                state=result.state,
                package_mode=result.package_mode,
                selected_session_id=result.selected_session_id,
                counts=result.counts,
                issue_count=len(result.issues),
                blocking_issue_count=result.blocking_issue_count,
            )
        except OneRosterError:
            self.store.discard_import(import_id)
            raise
        except Exception as exc:
            self.store.discard_import(import_id)
            raise OneRosterError(
                "OR-INGEST-FAILED",
                "The OneRoster package could not be normalized safely.",
            ) from exc
        except BaseException:
            if snapshot_dir.exists():
                self.store.discard_import(import_id)
            raise

    def get_import(self, import_id: str) -> OneRosterSnapshot:
        return self.store.get_import(import_id)

    def history(self, limit: int = MAX_PAGE_SIZE) -> tuple[OneRosterSnapshot, ...]:
        return self.store.history(limit)

    def select_session(self, import_id: str, session_id: str) -> OneRosterSnapshot:
        return self.store.select_session(import_id, session_id)

    def preview(
        self,
        import_id: str,
        kind: str,
        query: str = "",
        cursor: Optional[str] = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> PreviewPage:
        return self.store.preview(import_id, kind, query, cursor, limit)

    def write_export(
        self,
        import_id: str,
        kind: str,
        stream: BinaryIO | TextIO,
    ) -> int:
        return self.store.write_export(import_id, kind, stream)

    def get_threshold_profile(self) -> ThresholdProfile:
        return self.store.get_threshold_profile()

    def save_threshold_profile(
        self,
        profile: ThresholdProfile | Mapping[str, object],
    ) -> ThresholdProfile:
        normalized = (
            profile
            if isinstance(profile, ThresholdProfile)
            else ThresholdProfile.from_mapping(profile)
        )
        return self.store.save_threshold_profile(normalized)

    def scope_readiness(self, *, now: Optional[float] = None) -> ScopeReadiness:
        return self.store.get_scope_readiness(
            canonical_hash(sorted(ONEROSTER_DWD_SCOPES)),
            now=now,
            ttl_seconds=SCOPE_READINESS_TTL_SECONDS,
        )

    def mark_scope_ready(self, *, when: Optional[float] = None) -> ScopeReadiness:
        return self.store.mark_scope_ready(
            canonical_hash(sorted(ONEROSTER_DWD_SCOPES)),
            when=when,
            ttl_seconds=SCOPE_READINESS_TTL_SECONDS,
        )

    def invalidate_scope_readiness(self) -> None:
        self.store.invalidate_scope_readiness()

    def require_scope_ready(self, *, now: Optional[float] = None) -> None:
        if not self.scope_readiness(now=now).ready:
            raise OneRosterError(
                "CMP-AUTH-REQUIRED",
                "Verify the exact OneRoster Directory and Classroom scopes before reading live state.",
            )

    def evaluate_thresholds(
        self,
        import_id: str,
        current_counts: Mapping[str, int],
        *,
        now: Optional[datetime] = None,
    ) -> ThresholdEvaluation:
        self.store.get_import(import_id)
        return run_threshold_evaluation(
            self.store.get_threshold_profile(),
            current_counts,
            self.store.baseline_counts(import_id),
            now=now,
        )

    def record_override(
        self,
        import_id: str,
        evaluation: ThresholdEvaluation,
        reason: str,
        typed_import_id: str,
    ) -> ThresholdOverride:
        return self.store.record_override(
            import_id, evaluation, reason, typed_import_id
        )

    def record_denial(
        self,
        import_id: str,
        evaluation: ThresholdEvaluation,
        reason: str,
        typed_import_id: str,
    ) -> ThresholdDenial:
        return self.store.record_denial(
            import_id, evaluation, reason, typed_import_id
        )

    def get_denial(
        self,
        import_id: str,
        evaluation: ThresholdEvaluation,
    ) -> Optional[ThresholdDenial]:
        return self.store.get_denial(import_id, evaluation)

    def create_manifest(
        self,
        import_id: str,
        *,
        config_hash: str,
        live_hash: str,
        actions: Sequence[ImportAction],
        evaluation: Optional[ThresholdEvaluation] = None,
        limited_import: bool = False,
        plan_kind: str = "ordinary",
        exclusions: Sequence[Any] = (),
        pilot_evidence: str = "",
    ) -> ClassroomImportManifest:
        profile = self.store.get_threshold_profile()
        limited_mode = bool(limited_import or profile.limited_import)
        effective_actions = (
            _limited_actions(actions) if limited_mode else actions
        )
        if not profile.configured and not limited_mode:
            raise OneRosterError(
                "OR-THRESHOLD-UNCONFIGURED",
                "District thresholds must be configured before a full apply manifest.",
            )
        if evaluation is None and profile.configured:
            evaluation = self.evaluate_thresholds(
                import_id,
                Counter(action.kind for action in effective_actions),
            )
        if evaluation is not None and evaluation.held and not self.store.has_override(
            import_id, evaluation
        ):
            raise OneRosterError(
                "OR-THRESHOLD-HOLD",
                "The import is held by district thresholds and has no matching override.",
            )
        return self.store.create_manifest(
            import_id,
            config_hash=config_hash,
            live_hash=live_hash,
            actions=effective_actions,
            threshold_evaluation_hash=(
                evaluation_hash(evaluation) if evaluation is not None else ""
            ),
            # This service already produced the exact limited action sequence
            # used for threshold evaluation. Avoid filtering/copying it again.
            limited_import=False,
            plan_kind=plan_kind,
            threshold_evidence=_threshold_evidence(evaluation),
            exclusions=exclusions,
            pilot_evidence=_pilot_evidence(
                pilot_evidence,
                prior_accepted_import_id=self.store.previous_accepted_import_id(import_id),
                limited_import=limited_mode,
            ),
        )

    async def build_live_plan(
        self,
        connector: Any,
        import_id: str,
        *,
        limited_import: bool = False,
        now: Optional[datetime] = None,
    ) -> LivePlanningResult:
        """Explicitly read Directory/Classroom and produce a non-mutating live diff."""
        from .planner import OneRosterPlanner

        self.require_scope_ready()
        return await OneRosterPlanner(self.store, connector).plan(
            import_id,
            limited_import=limited_import,
            now=now,
        )

    def persist_live_plan(
        self,
        planning: LivePlanningResult,
        *,
        pilot_evidence: str = "",
    ) -> PlannedManifestSet:
        """Persist one district plan plus separately approvable archive/owner plans."""
        snapshot = self.store.get_import(planning.import_id)
        if snapshot.source_sha256 != planning.source_hash:
            raise OneRosterError(
                "OR-SOURCE-DRIFT",
                "The retained OneRoster source changed after live planning.",
            )
        evaluation = planning.threshold_evaluation
        profile = self.store.get_threshold_profile()
        if evaluation.profile_hash != canonical_hash(profile.to_dict()):
            raise OneRosterError(
                "OR-CONFIG-DRIFT",
                "District import configuration changed after live planning.",
            )
        if (
            evaluation.held
            and not planning.limited_import
            and not self.store.has_override(planning.import_id, evaluation)
        ):
            raise OneRosterError(
                "OR-THRESHOLD-HOLD",
                "The import is held by district thresholds and has no matching override.",
            )
        digest = evaluation_hash(evaluation)
        threshold_evidence = _threshold_evidence(evaluation)
        pilot_record = _pilot_evidence(
            pilot_evidence,
            prior_accepted_import_id=self.store.previous_accepted_import_id(
                planning.import_id
            ),
            limited_import=planning.limited_import,
        )
        ordinary = self.store.create_manifest(
            planning.import_id,
            config_hash=planning.config_hash,
            live_hash=planning.live_hash,
            actions=planning.actions,
            threshold_evaluation_hash=digest,
            # The planner already emitted the exact limited sequence.
            limited_import=False,
            plan_kind="limited" if planning.limited_import else "ordinary",
            threshold_evidence=threshold_evidence,
            exclusions=planning.issues,
            pilot_evidence=pilot_record,
        )
        archive = (
            self.store.create_manifest(
                planning.import_id,
                config_hash=planning.config_hash,
                live_hash=planning.live_hash,
                actions=planning.archive_actions,
                threshold_evaluation_hash=digest,
                plan_kind="archive",
                threshold_evidence=threshold_evidence,
                exclusions=planning.issues,
                pilot_evidence=pilot_record,
            )
            if planning.archive_actions
            else None
        )
        ownership = (
            self.store.create_manifest(
                planning.import_id,
                config_hash=planning.config_hash,
                live_hash=planning.live_hash,
                actions=planning.ownership_actions,
                threshold_evaluation_hash=digest,
                plan_kind="ownership",
                threshold_evidence=threshold_evidence,
                exclusions=planning.issues,
                pilot_evidence=pilot_record,
            )
            if planning.ownership_actions
            else None
        )
        return PlannedManifestSet(
            ordinary=ordinary,
            archive=archive,
            ownership=ownership,
            issues=planning.issues,
        )

    async def execute_manifest(
        self,
        connector: Any,
        manifest_id: str,
        *,
        typed_import_id: str = "",
        now: Optional[datetime] = None,
    ) -> ExecutionSummary:
        from .executor import OneRosterExecutor

        self.require_scope_ready()
        return await OneRosterExecutor(
            self.store,
            connector,
            activity_registry=self.activity_registry,
        ).execute(
            manifest_id,
            typed_import_id=typed_import_id,
            now=now,
        )

    async def revalidate_scheduled_gate(
        self,
        connector: Any,
        manifest_id: str,
        *,
        now: Optional[datetime] = None,
        manual: bool = False,
    ) -> StudentEnrollmentGate:
        from .executor import OneRosterExecutor

        self.require_scope_ready()
        return await OneRosterExecutor(
            self.store,
            connector,
            activity_registry=self.activity_registry,
        ).revalidate_scheduled_gate(
            manifest_id,
            now=now,
            manual=manual,
        )

    def hold_scheduled_gate_failure(
        self,
        manifest_id: str,
        error: BaseException,
        *,
        now: Optional[datetime] = None,
    ) -> StudentEnrollmentGate:
        """Persist a privacy-safe hold after an automatic release check fails.

        The scheduler calls this only after a due ARMED gate fails
        revalidation.  A concurrent operator close/re-arm wins: this method
        never replaces a gate that is no longer armed for the same manifest.
        """

        current = self.store.get_gate()
        if (
            current.state is not GateState.ARMED
            or current.manifest_id != str(manifest_id or "").strip()
        ):
            return current

        error_code = (
            str(getattr(error, "code", "") or "").strip()
            if isinstance(error, OneRosterError)
            else ""
        )
        if error_code == "CMP-AUTH-REQUIRED":
            hold_code = "CMP-AUTH-REQUIRED"
            hold_detail = (
                "OneRoster live-access verification expired before the scheduled "
                "student release. Verify access again, review the manifest, and re-arm."
            )
        elif error_code:
            hold_code = error_code
            hold_detail = (
                f"Scheduled student-release revalidation failed ({error_code}). "
                "Review the hold, rebuild the plan if required, and re-arm."
            )
        else:
            hold_code = "OR-GATE-REVALIDATION-FAILED"
            hold_detail = (
                "Scheduled student-release revalidation failed. Review local "
                "diagnostics and the manifest before re-arming."
            )
        return self.store.hold_gate(
            hold_code,
            hold_detail,
            now=now,
            preserve_manifest=True,
        )

    def get_manifest(self, manifest_id: str) -> ClassroomImportManifest:
        return self.store.get_manifest(manifest_id)

    def get_manifest_header(self, manifest_id: str) -> ClassroomImportManifest:
        """Read manifest status/evidence without materializing action rows."""

        return self.store.get_manifest_header(manifest_id)

    def get_manifest_page(
        self,
        manifest_id: str,
        *,
        offset: int = 0,
        limit: int = MAX_PAGE_SIZE,
    ) -> ManifestPage:
        return self.store.get_manifest_page(
            manifest_id,
            offset=offset,
            limit=limit,
        )

    def mark_action_result(
        self,
        manifest_id: str,
        action_id: str,
        *,
        status: str,
        detail: str = "",
    ) -> ClassroomImportManifest:
        return self.store.mark_action_result(
            manifest_id, action_id, status=status, detail=detail
        )

    def finish_manifest(
        self,
        manifest_id: str,
        *,
        status: str,
        error: str = "",
    ) -> ClassroomImportManifest:
        return self.store.finish_manifest(manifest_id, status=status, error=error)

    def mark_accepted(self, import_id: str) -> None:
        self.store.mark_accepted(import_id)

    def get_gate(self) -> StudentEnrollmentGate:
        return self.store.get_gate()

    def close_gate(self) -> StudentEnrollmentGate:
        return self.store.close_gate()

    def arm_gate(
        self,
        manifest_id: str,
        manifest_hash: str,
        release_at: str,
    ) -> StudentEnrollmentGate:
        return self.store.arm_gate(manifest_id, manifest_hash, release_at)

    def open_gate(
        self,
        manifest_id: str,
        manifest_hash: str,
        current_manifest_hash: str,
        *,
        now: Optional[datetime] = None,
    ) -> StudentEnrollmentGate:
        return self.store.open_gate(
            manifest_id,
            manifest_hash,
            current_manifest_hash,
            now=now,
        )

    def cleanup_expired(self, *, now: Optional[float] = None) -> int:
        return self.store.cleanup_expired(now=now)

    def purge_preview(self) -> PurgePreview:
        return self.store.purge_preview()

    def purge_data(self, confirmation: str) -> PurgePreview:
        return self.store.purge_data(confirmation)


def _limited_actions(actions: Sequence[ImportAction]) -> tuple[ImportAction, ...]:
    created = {
        action.subject.casefold()
        for action in actions
        if action.kind == "course_create"
    }
    return tuple(
        action
        for action in actions
        if action.kind in {"course_create", "teacher_add", "student_add"}
        or (
            action.kind == "course_activate"
            and action.subject.casefold() in created
        )
    )


def _threshold_evidence(
    evaluation: Optional[ThresholdEvaluation],
) -> dict[str, Any]:
    if evaluation is None:
        return {}
    raw = asdict(evaluation) if is_dataclass(evaluation) else dict(evaluation)
    return {
        "held": bool(raw.get("held", False)),
        "limited_import": bool(raw.get("limited_import", False)),
        "blackout": bool(raw.get("blackout", False)),
        "evaluated_at": float(raw.get("evaluated_at", 0.0) or 0.0),
        "counts": {
            str(key): int(value)
            for key, value in dict(raw.get("counts", {}) or {}).items()
        },
        "baselines": {
            str(key): int(value)
            for key, value in dict(raw.get("baselines", {}) or {}).items()
        },
        "breaches": [
            dict(item) if isinstance(item, Mapping) else asdict(item)
            for item in (raw.get("breaches", ()) or ())
        ],
        "profile_hash": str(raw.get("profile_hash", "") or ""),
    }


def _pilot_evidence(
    note: str | Mapping[str, Any],
    *,
    prior_accepted_import_id: str,
    limited_import: bool,
) -> dict[str, Any]:
    if isinstance(note, Mapping):
        operator_note = str(note.get("operator_note", "") or "").strip()
    else:
        operator_note = str(note or "").strip()
    if len(operator_note) > 1000:
        raise OneRosterError(
            "OR-PILOT-EVIDENCE",
            "Pilot evidence must be 1,000 characters or fewer.",
        )
    return {
        "operator_note": operator_note,
        "prior_accepted_import_id": str(prior_accepted_import_id or ""),
        "mode": "limited" if limited_import else "full",
    }
