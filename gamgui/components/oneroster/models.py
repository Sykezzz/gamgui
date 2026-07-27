"""Public data contracts for the optional OneRoster Classroom component.

The component deliberately keeps these contracts independent from FastAPI and GAM.
Opening the component, inspecting imports, or changing local safety configuration must
never require Workspace credentials or make a Google request.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple


COMPONENT_ID = "classroom-oneroster"
MAX_PAGE_SIZE = 50
RETENTION_DAYS = 30
SCOPE_READINESS_TTL_SECONDS = 24 * 60 * 60


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class SnapshotState(str, Enum):
    PREPARING = "preparing"
    BLOCKED = "blocked"
    READY = "ready"
    EXPIRED = "expired"


class GateState(str, Enum):
    CLOSED = "CLOSED"
    ARMED = "ARMED"
    OPEN = "OPEN"


class OneRosterError(RuntimeError):
    """Privacy-safe error with a stable operator-facing code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


@dataclass(frozen=True)
class ImportIssue:
    code: str
    severity: IssueSeverity
    message: str
    entity_kind: str = ""
    source_id: str = ""
    row_number: Optional[int] = None
    blocking: bool = True


@dataclass(frozen=True)
class SnapshotCounts:
    users: int = 0
    classes: int = 0
    courses: int = 0
    enrollments: int = 0
    academic_sessions: int = 0
    orgs: int = 0
    ready_courses: int = 0
    quarantined_courses: int = 0
    teachers: int = 0
    students: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "users": self.users,
            "classes": self.classes,
            "courses": self.courses,
            "enrollments": self.enrollments,
            "academic_sessions": self.academic_sessions,
            "orgs": self.orgs,
            "ready_courses": self.ready_courses,
            "quarantined_courses": self.quarantined_courses,
            "teachers": self.teachers,
            "students": self.students,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SnapshotCounts":
        fields = cls.__dataclass_fields__
        return cls(
            **{
                name: max(0, int(value.get(name, 0) or 0))
                for name in fields
            }
        )


@dataclass(frozen=True)
class OneRosterSnapshot:
    id: str
    domain: str
    filename: str
    source_sha256: str
    state: SnapshotState
    package_mode: str
    selected_session_id: str
    imported_at: float
    expires_at: float
    counts: SnapshotCounts
    issue_count: int
    blocking_issue_count: int

    @property
    def ready_for_apply(self) -> bool:
        return (
            self.state is SnapshotState.READY
            and self.package_mode == "bulk"
            and self.blocking_issue_count == 0
            and bool(self.selected_session_id)
        )


@dataclass(frozen=True)
class PreviewPage:
    items: Tuple[Mapping[str, Any], ...]
    next_cursor: Optional[str]
    total: int
    limit: int
    total_exact: bool = True


@dataclass(frozen=True)
class DashboardStatus:
    latest: Optional[OneRosterSnapshot]
    import_count: int
    threshold_profile_configured: bool
    gate_state: GateState
    retained_bytes: int


@dataclass(frozen=True)
class PurgePreview:
    snapshot_count: int
    manifest_count: int
    retained_bytes: int
    oldest_import_at: Optional[float]
    newest_import_at: Optional[float]


@dataclass(frozen=True)
class ActionLimit:
    """Maximum acceptable change for one action family.

    A value of ``None`` disables that dimension. Percentage values use the
    previous accepted count as their denominator and are expressed from 0 to 100.
    """

    max_count: Optional[int] = None
    max_percent: Optional[float] = None

    def __post_init__(self) -> None:
        if self.max_count is not None and int(self.max_count) < 0:
            raise ValueError("Threshold counts cannot be negative.")
        if self.max_percent is not None and not 0 <= float(self.max_percent) <= 100:
            raise ValueError("Threshold percentages must be between 0 and 100.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_count": self.max_count,
            "max_percent": self.max_percent,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionLimit":
        count = value.get("max_count")
        percent = value.get("max_percent")
        return cls(
            max_count=None if count in (None, "") else int(count),
            max_percent=None if percent in (None, "") else float(percent),
        )


DEFAULT_THRESHOLD_ACTIONS = (
    "course_create",
    "course_update",
    "course_archive",
    "teacher_add",
    "teacher_remove",
    "student_add",
    "student_remove",
    "owner_mismatch",
    "record_rejected",
)


@dataclass(frozen=True)
class BlackoutWindow:
    starts_at: str
    ends_at: str
    label: str = ""

    def __post_init__(self) -> None:
        start = parse_aware_datetime(self.starts_at)
        end = parse_aware_datetime(self.ends_at)
        if end <= start:
            raise ValueError("A blackout window must end after it starts.")

    def contains(self, moment: datetime) -> bool:
        if moment.tzinfo is None:
            raise ValueError("Threshold evaluation requires a timezone-aware time.")
        return parse_aware_datetime(self.starts_at) <= moment <= parse_aware_datetime(
            self.ends_at
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "label": self.label,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BlackoutWindow":
        return cls(
            starts_at=str(value.get("starts_at") or ""),
            ends_at=str(value.get("ends_at") or ""),
            label=str(value.get("label") or ""),
        )


@dataclass(frozen=True)
class ThresholdProfile:
    version: int = 1
    configured: bool = False
    limited_import: bool = False
    limits: Mapping[str, ActionLimit] = field(default_factory=dict)
    blackouts: Tuple[BlackoutWindow, ...] = ()
    mode: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", "limited" if self.limited_import else "normal")
        unknown = set(self.limits) - set(DEFAULT_THRESHOLD_ACTIONS)
        if unknown:
            raise ValueError(f"Unknown threshold actions: {', '.join(sorted(unknown))}.")
        if int(self.version) < 1:
            raise ValueError("Threshold profile version must be positive.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": int(self.version),
            "configured": bool(self.configured),
            "limited_import": bool(self.limited_import),
            "limits": {
                key: value.to_dict()
                for key, value in sorted(self.limits.items())
            },
            "blackouts": [window.to_dict() for window in self.blackouts],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ThresholdProfile":
        limits = value.get("limits")
        blackouts = value.get("blackouts")
        return cls(
            version=int(value.get("version", 1) or 1),
            configured=bool(value.get("configured", False)),
            limited_import=bool(value.get("limited_import", False)),
            limits={
                str(key): ActionLimit.from_mapping(item)
                for key, item in (limits.items() if isinstance(limits, Mapping) else ())
                if isinstance(item, Mapping)
            },
            blackouts=tuple(
                BlackoutWindow.from_mapping(item)
                for item in (blackouts if isinstance(blackouts, Sequence) else ())
                if isinstance(item, Mapping)
            ),
        )


@dataclass(frozen=True)
class ThresholdBreach:
    action: str
    actual_count: int
    baseline_count: int
    actual_percent: Optional[float]
    max_count: Optional[int]
    max_percent: Optional[float]
    reason: str


@dataclass(frozen=True)
class ThresholdEvaluation:
    held: bool
    limited_import: bool
    blackout: bool
    evaluated_at: float
    counts: Mapping[str, int]
    baselines: Mapping[str, int]
    breaches: Tuple[ThresholdBreach, ...]
    profile_hash: str


@dataclass(frozen=True)
class ThresholdOverride:
    import_id: str
    evaluation_hash: str
    reason: str
    recorded_at: float


@dataclass(frozen=True)
class ThresholdDenial:
    import_id: str
    evaluation_hash: str
    reason: str
    recorded_at: float


@dataclass(frozen=True)
class ImportAction:
    id: str
    kind: str
    subject: str
    target: str
    before: str = ""
    after: str = ""
    status: str = "pending"
    detail: str = ""

    @property
    def destructive(self) -> bool:
        return self.kind.endswith("_remove") or self.kind in {
            "course_archive",
            "owner_transfer",
        }

    def basis_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "kind": self.kind,
            "subject": self.subject,
            "target": self.target,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class ClassroomImportManifest:
    id: str
    domain: str
    import_id: str
    source_hash: str
    config_hash: str
    live_hash: str
    manifest_hash: str
    status: str
    created_at: float
    actions: Tuple[ImportAction, ...]
    prepared_live_hash: str = ""
    threshold_evaluation_hash: str = ""
    error: str = ""
    plan_kind: str = "ordinary"
    confirmed_at: float = 0.0
    threshold_evidence: Mapping[str, Any] = field(default_factory=dict)
    exclusions: Tuple[ImportIssue, ...] = ()
    pilot_evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def complete_count(self) -> int:
        return sum(action.status in {"applied", "failed", "skipped"} for action in self.actions)

    @property
    def pending_actions(self) -> Tuple[ImportAction, ...]:
        return tuple(action for action in self.actions if action.status == "pending")

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at > 0


@dataclass(frozen=True)
class ManifestPage:
    """One bounded manifest-action page plus SQL-derived aggregate status."""

    manifest: ClassroomImportManifest
    total: int
    offset: int
    limit: int
    complete_count: int
    pending_count: int
    action_counts: Mapping[str, int]


@dataclass(frozen=True)
class ScopeReadiness:
    """Cached proof that the exact OneRoster DWD scope set passed verification."""

    ready: bool
    required_scope_hash: str
    verified_at: float = 0.0
    expires_at: float = 0.0


@dataclass(frozen=True)
class LivePlanningResult:
    """Credential-backed diff produced only by an explicit planning request."""

    import_id: str
    source_hash: str
    config_hash: str
    live_hash: str
    actions: Tuple[ImportAction, ...]
    archive_actions: Tuple[ImportAction, ...]
    ownership_actions: Tuple[ImportAction, ...]
    issues: Tuple[ImportIssue, ...]
    limited_import: bool
    threshold_evaluation: ThresholdEvaluation
    # Ephemeral verification evidence from the same live Directory preflight.
    # It is deliberately excluded from manifests, hashes, persistence, and repr.
    owner_ids: Mapping[str, str] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )

    def actions_for(self, plan_kind: str) -> Tuple[ImportAction, ...]:
        kind = str(plan_kind or "").strip().casefold()
        if kind in {"ordinary", "limited"}:
            return self.actions
        if kind == "archive":
            return self.archive_actions
        if kind == "ownership":
            return self.ownership_actions
        raise ValueError("Unknown OneRoster plan kind.")


@dataclass(frozen=True)
class PlannedManifestSet:
    ordinary: ClassroomImportManifest
    archive: Optional[ClassroomImportManifest] = None
    ownership: Optional[ClassroomImportManifest] = None
    issues: Tuple[ImportIssue, ...] = ()


@dataclass(frozen=True)
class ExecutionSummary:
    manifest: ClassroomImportManifest
    applied: int
    failed: int
    skipped: int
    awaiting_students: bool


@dataclass(frozen=True)
class StudentEnrollmentGate:
    state: GateState
    timezone: str
    manifest_id: str = ""
    manifest_hash: str = ""
    release_at: str = ""
    updated_at: float = 0.0
    hold_code: str = ""
    hold_detail: str = ""


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Date/time values must be ISO 8601.") from exc
    if parsed.tzinfo is None:
        raise ValueError("Date/time values must include a timezone.")
    return parsed
