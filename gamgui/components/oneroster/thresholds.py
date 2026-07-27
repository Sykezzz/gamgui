"""ClassLink-style local import thresholds.

This module only evaluates local counts. It neither calls GAM nor treats an override
as authorization to mutate Google; executor integration must independently verify the
immutable manifest before acting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Optional

from .models import (
    ActionLimit,
    ThresholdBreach,
    ThresholdEvaluation,
    ThresholdProfile,
    canonical_hash,
)


def evaluate_thresholds(
    profile: ThresholdProfile,
    counts: Mapping[str, int],
    baselines: Mapping[str, int],
    *,
    now: Optional[datetime] = None,
) -> ThresholdEvaluation:
    """Evaluate one proposed import against the configured profile.

    ``counts`` contains proposed action counts, while ``baselines`` contains the
    corresponding prior accepted population used as the percentage denominator.
    """

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("Threshold evaluation requires a timezone-aware time.")

    normalized_counts = {
        str(action): max(0, int(value or 0))
        for action, value in counts.items()
    }
    normalized_baselines = {
        str(action): max(0, int(value or 0))
        for action, value in baselines.items()
    }
    breaches: list[ThresholdBreach] = []

    blackout = any(window.contains(moment) for window in profile.blackouts)
    if not profile.configured:
        breaches.append(
            ThresholdBreach(
                action="profile",
                actual_count=0,
                baseline_count=0,
                actual_percent=None,
                max_count=None,
                max_percent=None,
                reason="District thresholds have not been configured.",
            )
        )

    for action, limit in sorted(profile.limits.items()):
        _evaluate_action(
            action,
            limit,
            normalized_counts.get(action, 0),
            normalized_baselines.get(action, 0),
            breaches,
        )

    if blackout:
        breaches.append(
            ThresholdBreach(
                action="blackout",
                actual_count=sum(normalized_counts.values()),
                baseline_count=0,
                actual_percent=None,
                max_count=0,
                max_percent=None,
                reason="The proposed run falls inside a configured blackout window.",
            )
        )

    return ThresholdEvaluation(
        held=bool(breaches),
        limited_import=profile.limited_import,
        blackout=blackout,
        evaluated_at=moment.timestamp(),
        counts=normalized_counts,
        baselines=normalized_baselines,
        breaches=tuple(breaches),
        profile_hash=canonical_hash(profile.to_dict()),
    )


def evaluation_hash(evaluation: ThresholdEvaluation) -> str:
    return canonical_hash(
        {
            "held": evaluation.held,
            "limited_import": evaluation.limited_import,
            "blackout": evaluation.blackout,
            "counts": dict(sorted(evaluation.counts.items())),
            "baselines": dict(sorted(evaluation.baselines.items())),
            "breaches": [
                {
                    "action": item.action,
                    "actual_count": item.actual_count,
                    "baseline_count": item.baseline_count,
                    "actual_percent": item.actual_percent,
                    "max_count": item.max_count,
                    "max_percent": item.max_percent,
                    "reason": item.reason,
                }
                for item in evaluation.breaches
            ],
            "profile_hash": evaluation.profile_hash,
        }
    )


def _evaluate_action(
    action: str,
    limit: ActionLimit,
    actual: int,
    baseline: int,
    breaches: list[ThresholdBreach],
) -> None:
    percent: Optional[float]
    if baseline > 0:
        percent = (actual / baseline) * 100.0
    elif actual:
        percent = None
    else:
        percent = 0.0

    reasons: list[str] = []
    if limit.max_count is not None and actual > limit.max_count:
        reasons.append(f"count {actual} exceeds {limit.max_count}")
    if limit.max_percent is not None:
        if actual > 0 and baseline == 0:
            reasons.append("percentage cannot be evaluated without a prior baseline")
        elif percent is not None and percent > limit.max_percent:
            reasons.append(f"percent {percent:.2f} exceeds {limit.max_percent:.2f}")

    if reasons:
        breaches.append(
            ThresholdBreach(
                action=action,
                actual_count=actual,
                baseline_count=baseline,
                actual_percent=percent,
                max_count=limit.max_count,
                max_percent=limit.max_percent,
                reason="; ".join(reasons),
            )
        )
