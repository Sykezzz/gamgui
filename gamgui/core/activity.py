"""Process-wide exclusion for administrative and application lifecycle work.

The registry deliberately stores no tenant identifiers or operation payloads.  It is
only a small in-memory coordination primitive: a component swap, application update,
connector rebind, or administrative mutation must hold the one exclusive lease.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional


class ActivityBusyError(RuntimeError):
    """Raised when another exclusive activity is already in progress."""

    error_code = "CMP-ACTIVE-JOB"

    def __init__(self, active_kind: str) -> None:
        super().__init__(
            "Another administrative operation is active. Wait for it to finish and try again."
        )
        self.active_kind = active_kind


@dataclass(frozen=True)
class ActivitySnapshot:
    """Privacy-safe description of the currently held lease."""

    kind: str
    started_at: float


class ActivityLease:
    """Idempotent context-managed lease returned by :class:`ActivityRegistry`."""

    def __init__(self, registry: "ActivityRegistry", token: str, kind: str) -> None:
        self._registry = registry
        self._token = token
        self.kind = kind
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._registry._release(self._token)
        self._released = True

    def __enter__(self) -> "ActivityLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class ActivityRegistry:
    """Allow at most one mutation/lifecycle activity in this process."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._active_token = ""
        self._active_kind = ""
        self._started_at = 0.0

    def acquire(self, kind: str) -> ActivityLease:
        normalized = _normalize_kind(kind)
        token = uuid.uuid4().hex
        with self._lock:
            if self._active_token:
                raise ActivityBusyError(self._active_kind)
            self._active_token = token
            self._active_kind = normalized
            self._started_at = self._clock()
        return ActivityLease(self, token, normalized)

    def try_acquire(self, kind: str) -> Optional[ActivityLease]:
        try:
            return self.acquire(kind)
        except ActivityBusyError:
            return None

    def snapshot(self) -> Optional[ActivitySnapshot]:
        with self._lock:
            if not self._active_token:
                return None
            return ActivitySnapshot(
                kind=self._active_kind,
                started_at=self._started_at,
            )

    def is_active(self) -> bool:
        return self.snapshot() is not None

    def _release(self, token: str) -> None:
        with self._lock:
            if token != self._active_token:
                return
            self._active_token = ""
            self._active_kind = ""
            self._started_at = 0.0


def _normalize_kind(value: str) -> str:
    kind = str(value or "").strip().lower().replace("_", "-")
    if not kind or len(kind) > 64:
        raise ValueError("Activity kind must contain between 1 and 64 characters.")
    if not all(character.isalnum() or character in {"-", "."} for character in kind):
        raise ValueError("Activity kind contains unsupported characters.")
    return kind


activity_registry = ActivityRegistry()
