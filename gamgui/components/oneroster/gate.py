"""Student enrollment release-gate policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import GateState, OneRosterError, StudentEnrollmentGate, parse_aware_datetime


DISTRICT_TIMEZONE = "America/Chicago"


def closed_gate(*, now: Optional[datetime] = None) -> StudentEnrollmentGate:
    moment = _aware(now)
    return StudentEnrollmentGate(
        state=GateState.CLOSED,
        timezone=DISTRICT_TIMEZONE,
        updated_at=moment.timestamp(),
    )


def arm_gate(
    manifest_id: str,
    manifest_hash: str,
    release_at: str,
    *,
    now: Optional[datetime] = None,
) -> StudentEnrollmentGate:
    if not manifest_id.strip() or not manifest_hash.strip():
        raise OneRosterError(
            "OR-GATE-MANIFEST-REQUIRED",
            "An immutable import manifest is required before student release can be armed.",
        )
    # Compare timezone-aware instants directly. This avoids requiring the third-party
    # ``tzdata`` wheel on Windows while the persisted gate still declares the district
    # scheduler's authoritative IANA zone.
    release = parse_aware_datetime(release_at)
    moment = _aware(now)
    # Allow a small UI/SQLite round-trip skew for an explicitly manual "open now"
    # arm. Materially past schedules remain rejected.
    if release < moment - timedelta(seconds=5):
        raise OneRosterError(
            "OR-GATE-PAST",
            "The student release time cannot be in the past.",
        )
    return StudentEnrollmentGate(
        state=GateState.ARMED,
        timezone=DISTRICT_TIMEZONE,
        manifest_id=manifest_id.strip(),
        manifest_hash=manifest_hash.strip(),
        release_at=release.isoformat(),
        updated_at=moment.timestamp(),
    )


def open_gate(
    current: StudentEnrollmentGate,
    *,
    manifest_id: str,
    manifest_hash: str,
    current_manifest_hash: str,
    now: Optional[datetime] = None,
    allow_early: bool = False,
) -> StudentEnrollmentGate:
    moment = _aware(now)
    if current.state is not GateState.ARMED:
        raise OneRosterError(
            "OR-GATE-NOT-ARMED",
            "Student release must be armed against an exact manifest before it can open.",
        )
    if (
        manifest_id.strip() != current.manifest_id
        or manifest_hash.strip() != current.manifest_hash
        or current_manifest_hash.strip() != current.manifest_hash
    ):
        return StudentEnrollmentGate(
            state=GateState.HELD,
            timezone=DISTRICT_TIMEZONE,
            updated_at=moment.timestamp(),
            hold_code="OR-GATE-DRIFT",
            hold_detail="The approved manifest changed before student release.",
        )
    release = parse_aware_datetime(current.release_at)
    if moment < release and not allow_early:
        raise OneRosterError(
            "OR-GATE-NOT-DUE",
            "The approved student release time has not arrived.",
        )
    return StudentEnrollmentGate(
        state=GateState.OPEN,
        timezone=DISTRICT_TIMEZONE,
        manifest_id=current.manifest_id,
        manifest_hash=current.manifest_hash,
        release_at=current.release_at,
        updated_at=moment.timestamp(),
    )


def hold_gate(
    code: str,
    detail: str,
    *,
    now: Optional[datetime] = None,
) -> StudentEnrollmentGate:
    moment = _aware(now)
    return StudentEnrollmentGate(
        state=GateState.HELD,
        timezone=DISTRICT_TIMEZONE,
        updated_at=moment.timestamp(),
        hold_code=code,
        hold_detail=detail,
    )


def _aware(value: Optional[datetime]) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("Student gate operations require a timezone-aware time.")
    return moment
