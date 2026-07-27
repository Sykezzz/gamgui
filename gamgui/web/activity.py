"""Web-facing helpers for the process-wide administrative activity lease."""

from __future__ import annotations

from typing import Optional

from ..core.activity import (
    ActivityBusyError,
    ActivityLease,
    activity_registry,
)


ADMIN_ACTIVITY_BUSY_MESSAGE = (
    "CMP-ACTIVE-JOB: Another active administrative operation is in progress. "
    "Wait for it to finish and try again."
)


def try_acquire_admin_activity(state: object, kind: str) -> Optional[ActivityLease]:
    """Acquire the state registry, falling back to the process-wide singleton.

    The UI intentionally does not expose the active operation kind. Keeping the
    refusal generic avoids leaking subjects or other administrative context.
    """

    registry = getattr(state, "activity_registry", activity_registry)
    try:
        return registry.acquire(kind)
    except ActivityBusyError:
        return None


def activity_error_message(exc: Exception) -> str:
    """Return a stable, privacy-safe connector-rebind refusal."""

    cause = getattr(exc, "__cause__", None)
    if (
        isinstance(exc, ActivityBusyError)
        or isinstance(cause, ActivityBusyError)
        or (
            isinstance(exc, RuntimeError)
            and "active administrative operation" in str(exc).casefold()
        )
    ):
        return ADMIN_ACTIVITY_BUSY_MESSAGE
    return str(exc)
