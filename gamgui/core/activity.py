"""Exclusive coordination for administrative and application lifecycle work.

Ordinary :class:`ActivityRegistry` instances remain process-local.  The module-level
registry adds a small owner-only lease on macOS so a second app process cannot begin a
component swap, application update, connector rebind, or administrative mutation while
the first is still working.  The durable record contains only privacy-safe process and
activity metadata.
"""

from __future__ import annotations

import json
import math
import os
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, Optional

from .processes import current_process_identity, process_lease_is_dead

DURABLE_ACTIVITY_FILENAME = "activity-lease.json"
_DURABLE_GUARD_SUFFIX = ".guard"
_DURABLE_PROCESS_LOCK_SUFFIX = ".process-lock"
_MAX_DURABLE_RECORD_BYTES = 4096
_MAX_ORPHAN_PROCESS_LOCKS = 8
_EXTERNAL_ACTIVITY_KIND = "external-activity"
_PROCESS_LOCK_PROTOCOL = 1


class ActivityBusyError(RuntimeError):
    """Raised when another exclusive activity is already in progress."""

    error_code = "CMP-ACTIVE-JOB"

    def __init__(self, active_kind: str) -> None:
        super().__init__(
            "Another administrative operation is active. Wait for it to finish and try again."
        )
        self.active_kind = active_kind


class ActivityPathUnavailableError(RuntimeError):
    """Raised when the durable activity lock path cannot be resolved safely."""

    error_code = "CMP-ACTIVITY-PATH-UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(
            "GamGUI could not resolve its durable administrative activity lock path. "
            "Check this account's home-folder configuration and try again."
        )


@dataclass(frozen=True)
class ActivitySnapshot:
    """Privacy-safe description of the currently held lease."""

    kind: str
    started_at: float


@dataclass(frozen=True)
class _DurableActivity:
    kind: str
    timestamp: float
    pid: int
    process_identity: str
    token: str
    lock_protocol: int = 0
    lock_device: int = 0
    lock_inode: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "timestamp": self.timestamp,
            "pid": self.pid,
            "process_identity": self.process_identity,
            "token": self.token,
            "lock_protocol": self.lock_protocol,
            "lock_device": self.lock_device,
            "lock_inode": self.lock_inode,
        }


@dataclass
class _DurableProcessLock:
    path: Path
    descriptor: int
    device: int
    inode: int
    closed: bool = False

    def validate_for_inheritance(self) -> None:
        if self.closed or self.descriptor < 0:
            raise OSError("Durable activity process lock is closed.")
        metadata = os.fstat(self.descriptor)
        current = os.lstat(self.path)
        if (
            metadata.st_dev != self.device
            or metadata.st_ino != self.inode
            or current.st_dev != self.device
            or current.st_ino != self.inode
        ):
            raise OSError("Durable activity process lock was replaced.")
        _require_owner_only_regular(metadata)

    def close(self, *, unlink: bool = False) -> None:
        if self.closed:
            return
        if unlink:
            try:
                current = os.lstat(self.path)
                if (
                    current.st_dev == self.device
                    and current.st_ino == self.inode
                ):
                    os.unlink(self.path)
            except FileNotFoundError:
                pass
            except OSError:
                # Do not follow or remove a replacement path.
                pass
        try:
            # Deliberately do not issue LOCK_UN. An inherited descriptor shares
            # this open file description, so closing the parent's descriptor
            # must leave the lock held until the last GAM child exits.
            os.close(self.descriptor)
        finally:
            self.descriptor = -1
            self.closed = True


@dataclass(frozen=True)
class _DeferredDurableRelease:
    path: Path
    token: str
    device: int
    inode: int
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
    """Allow at most one mutation/lifecycle activity.

    ``durable_path`` is opt-in. It may be a path or a resolver called for every
    new acquisition/observation, which lets the macOS app honor a per-launch
    ``GAMGUI_APP_DATA_DIR`` without resolving Application Support at import time.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        *,
        durable_path: (
            Path
            | str
            | Callable[[], Path | str | None]
            | None
        ) = None,
        pid_provider: Callable[[], int] = os.getpid,
        identity_provider: Callable[[], str] = current_process_identity,
    ) -> None:
        self._clock = clock
        self._durable_path = durable_path
        self._pid_provider = pid_provider
        self._identity_provider = identity_provider
        self._lock = threading.Lock()
        self._active_token = ""
        self._active_kind = ""
        self._started_at = 0.0
        self._active_durable_path: Optional[Path] = None
        self._active_process_lock: Optional[_DurableProcessLock] = None
        self._deferred_release: Optional[_DeferredDurableRelease] = None

    def acquire(self, kind: str) -> ActivityLease:
        normalized = _normalize_kind(kind)
        token = uuid.uuid4().hex
        with self._lock:
            self._reap_deferred_release()
            if self._deferred_release is not None:
                raise ActivityBusyError(self._deferred_release.kind)
            if self._active_token:
                raise ActivityBusyError(self._active_kind)
            started_at = float(self._clock())
            if not math.isfinite(started_at) or started_at < 0:
                raise ValueError("Activity clock returned an invalid timestamp.")
            durable_path = self._resolve_durable_path()
            process_lock: Optional[_DurableProcessLock] = None
            if durable_path is not None:
                pid = int(self._pid_provider())
                if pid <= 0:
                    raise ValueError("Activity PID must be positive.")
                record = _DurableActivity(
                    kind=normalized,
                    timestamp=started_at,
                    pid=pid,
                    process_identity=str(self._identity_provider() or "")[:512],
                    token=token,
                    lock_protocol=_PROCESS_LOCK_PROTOCOL,
                )
                process_lock = self._acquire_durable(durable_path, record)
            self._active_token = token
            self._active_kind = normalized
            self._started_at = started_at
            self._active_durable_path = durable_path
            self._active_process_lock = process_lock
        return ActivityLease(self, token, normalized)

    def try_acquire(self, kind: str) -> Optional[ActivityLease]:
        try:
            return self.acquire(kind)
        except ActivityBusyError:
            return None

    def snapshot(self) -> Optional[ActivitySnapshot]:
        with self._lock:
            self._reap_deferred_release()
            if self._active_token:
                return ActivitySnapshot(
                    kind=self._active_kind,
                    started_at=self._started_at,
                )
            if self._deferred_release is not None:
                return ActivitySnapshot(
                    kind=self._deferred_release.kind,
                    started_at=self._deferred_release.started_at,
                )
            try:
                durable_path = self._resolve_durable_path()
            except ActivityPathUnavailableError:
                # If the process cannot locate the cross-process lock, it cannot
                # prove that another process is idle. Observation therefore
                # remains blocked rather than degrading to a process-local view.
                return _blocked_snapshot()
            if durable_path is None:
                return None
            try:
                with _durable_guard(durable_path):
                    return _observe_durable(durable_path, recover_stale=True)
            except OSError:
                return _blocked_snapshot()

    def is_active(self) -> bool:
        return self.snapshot() is not None

    @contextmanager
    def subprocess_pass_fds(self) -> Iterator[tuple[int, ...]]:
        """Yield a duplicate durable descriptor for one child-process spawn.

        Process-local registries return an empty tuple. For a durable lease,
        validation is intentionally repeated immediately before every spawn;
        a missing, closed, or replaced lock fails closed instead of launching
        an unprotected GAM process. The duplicate prevents descriptor-reuse
        races if lease teardown overlaps subprocess creation.
        """

        inherited_descriptor = -1
        with self._lock:
            if not self._active_token or self._active_durable_path is None:
                pass_fds: tuple[int, ...] = ()
            else:
                process_lock = self._active_process_lock
                if process_lock is None:
                    raise RuntimeError(
                        "The durable administrative activity lock is unavailable."
                    )
                try:
                    process_lock.validate_for_inheritance()
                    inherited_descriptor = _duplicate_process_lock_descriptor(
                        process_lock.descriptor
                    )
                    duplicate_metadata = os.fstat(inherited_descriptor)
                    if (
                        duplicate_metadata.st_dev != process_lock.device
                        or duplicate_metadata.st_ino != process_lock.inode
                    ):
                        raise OSError(
                            "Durable activity process lock duplication failed."
                        )
                except OSError as exc:
                    if inherited_descriptor >= 0:
                        os.close(inherited_descriptor)
                    raise RuntimeError(
                        "The durable administrative activity lock is unavailable."
                    ) from exc
                pass_fds = (inherited_descriptor,)
        try:
            yield pass_fds
        finally:
            if inherited_descriptor >= 0:
                os.close(inherited_descriptor)

    def _release(self, token: str) -> None:
        with self._lock:
            if token != self._active_token:
                return
            durable_path = self._active_durable_path
            process_lock = self._active_process_lock
            if durable_path is not None:
                # Close only our descriptor first. If a GAM subprocess still
                # has the inherited descriptor, the advisory lock remains
                # held and the durable record stays fail-closed.
                if process_lock is not None:
                    process_lock.close()
                device = process_lock.device if process_lock is not None else 0
                inode = process_lock.inode if process_lock is not None else 0
                released = self._finalize_durable_release(
                    durable_path,
                    token,
                    expected_device=device,
                    expected_inode=inode,
                )
                if not released:
                    self._deferred_release = _DeferredDurableRelease(
                        path=durable_path,
                        token=token,
                        device=device,
                        inode=inode,
                        kind=self._active_kind,
                        started_at=self._started_at,
                    )
            self._active_token = ""
            self._active_kind = ""
            self._started_at = 0.0
            self._active_durable_path = None
            self._active_process_lock = None

    def _reap_deferred_release(self) -> bool:
        deferred = self._deferred_release
        if deferred is None:
            return True
        released = self._finalize_durable_release(
            deferred.path,
            deferred.token,
            expected_device=deferred.device,
            expected_inode=deferred.inode,
        )
        if released:
            self._deferred_release = None
        return released

    @staticmethod
    def _finalize_durable_release(
        path: Path,
        token: str,
        *,
        expected_device: int,
        expected_inode: int,
    ) -> bool:
        try:
            with _durable_guard(path):
                lock_state, recovery_lock = _claim_process_lock_for_recovery(
                    path,
                    token,
                    required=True,
                    expected_device=expected_device,
                    expected_inode=expected_inode,
                )
                if lock_state != "acquired" or recovery_lock is None:
                    return False
                removed = _unlink_if_token(
                    path,
                    token,
                    expected_device=expected_device,
                    expected_inode=expected_inode,
                )
                recovery_lock.close(unlink=removed)
                return removed
        except OSError:
            # A release must never remove a lease it cannot prove it owns.
            return False

    def _resolve_durable_path(self) -> Optional[Path]:
        value = self._durable_path
        try:
            if callable(value):
                value = value()
            if value is None:
                return None
            path = Path(value).expanduser()
        except (OSError, RuntimeError, ValueError) as exc:
            # A mutating operation must never silently fall back to the
            # process-local registry when the configured durable path cannot
            # be resolved. Normalize expected environment/path failures so
            # callers can render a corrective refusal without exposing local
            # filesystem detail.
            raise ActivityPathUnavailableError() from exc
        if not path.name:
            raise ValueError("Durable activity path must name a file.")
        return path

    def _acquire_durable(
        self,
        path: Path,
        record: _DurableActivity,
    ) -> _DurableProcessLock:
        process_lock: Optional[_DurableProcessLock] = None
        try:
            with _durable_guard(path):
                for _attempt in range(2):
                    active = _observe_durable(path, recover_stale=True)
                    if active is not None:
                        raise ActivityBusyError(active.kind)
                    process_lock = _create_process_lock(path, record.token)
                    try:
                        _create_durable(
                            path,
                            replace(
                                record,
                                lock_device=process_lock.device,
                                lock_inode=process_lock.inode,
                            ),
                        )
                        return process_lock
                    except FileExistsError:
                        # An uncoordinated writer raced the atomic create.
                        # Observe once more and remain fail-closed if it does
                        # not disappear as a definitely stale lease.
                        process_lock.close(unlink=True)
                        process_lock = None
                        continue
        except ActivityBusyError:
            if process_lock is not None:
                process_lock.close(unlink=True)
            raise
        except (OSError, ValueError, TypeError):
            if process_lock is not None:
                process_lock.close(unlink=True)
            raise ActivityBusyError(_EXTERNAL_ACTIVITY_KIND) from None
        raise ActivityBusyError(_EXTERNAL_ACTIVITY_KIND)


def _normalize_kind(value: str) -> str:
    kind = str(value or "").strip().lower().replace("_", "-")
    if not kind or len(kind) > 64:
        raise ValueError("Activity kind must contain between 1 and 64 characters.")
    if not all(character.isalnum() or character in {"-", "."} for character in kind):
        raise ValueError("Activity kind contains unsupported characters.")
    return kind


def _duplicate_process_lock_descriptor(descriptor: int) -> int:
    """Duplicate outside stdin/stdout/stderr so child redirection cannot replace it."""

    try:
        import fcntl
    except ImportError as exc:
        raise OSError("Process descriptor duplication is unavailable.") from exc
    duplicate_command = getattr(
        fcntl,
        "F_DUPFD_CLOEXEC",
        fcntl.F_DUPFD,
    )
    duplicate = int(fcntl.fcntl(descriptor, duplicate_command, 3))
    try:
        if duplicate < 3:
            raise OSError(
                "Process descriptor duplication returned an unsafe descriptor."
            )
        os.set_inheritable(duplicate, False)
        return duplicate
    except BaseException:
        if duplicate >= 0:
            os.close(duplicate)
        raise


def _blocked_snapshot() -> ActivitySnapshot:
    return ActivitySnapshot(kind=_EXTERNAL_ACTIVITY_KIND, started_at=0.0)


def _read_durable(path: Path) -> tuple[str, Optional[_DurableActivity]]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "blocked", None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return "blocked", None
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            return "blocked", None
        if metadata.st_size <= 0 or metadata.st_size > _MAX_DURABLE_RECORD_BYTES:
            return "blocked", None
        content = bytearray()
        while len(content) <= _MAX_DURABLE_RECORD_BYTES:
            chunk = os.read(
                descriptor,
                min(1024, _MAX_DURABLE_RECORD_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_DURABLE_RECORD_BYTES:
            return "blocked", None
    except OSError:
        return "blocked", None
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "blocked", None
    if not isinstance(payload, dict):
        return "blocked", None
    base_fields = {
        "kind",
        "timestamp",
        "pid",
        "process_identity",
        "token",
    }
    fields = frozenset(payload)
    lock_fields = {
        "lock_protocol",
        "lock_device",
        "lock_inode",
    }
    if fields not in {
        frozenset(base_fields),
        frozenset(base_fields | lock_fields),
    }:
        return "blocked", None
    timestamp = payload.get("timestamp")
    pid = payload.get("pid")
    identity = payload.get("process_identity")
    token = payload.get("token")
    has_lock_protocol = "lock_protocol" in payload
    lock_protocol = payload.get("lock_protocol", 0)
    lock_device = payload.get("lock_device", 0)
    lock_inode = payload.get("lock_inode", 0)
    try:
        kind = _normalize_kind(payload.get("kind", ""))
    except ValueError:
        return "blocked", None
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(float(timestamp))
        or float(timestamp) < 0
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(identity, str)
        or len(identity) > 512
        or not isinstance(token, str)
        or len(token) != 32
        or any(character not in "0123456789abcdef" for character in token)
        or isinstance(lock_protocol, bool)
        or not isinstance(lock_protocol, int)
        or (
            lock_protocol
            != (_PROCESS_LOCK_PROTOCOL if has_lock_protocol else 0)
        )
        or isinstance(lock_device, bool)
        or not isinstance(lock_device, int)
        or lock_device < (1 if has_lock_protocol else 0)
        or isinstance(lock_inode, bool)
        or not isinstance(lock_inode, int)
        or lock_inode < (1 if has_lock_protocol else 0)
    ):
        return "blocked", None
    return (
        "valid",
        _DurableActivity(
            kind=kind,
            timestamp=float(timestamp),
            pid=pid,
            process_identity=identity,
            token=token,
            lock_protocol=lock_protocol,
            lock_device=lock_device,
            lock_inode=lock_inode,
        ),
    )


def _observe_durable(
    path: Path,
    *,
    recover_stale: bool,
) -> Optional[ActivitySnapshot]:
    state, record = _read_durable(path)
    if state == "missing":
        return None if _clear_free_orphan_process_locks(path) else _blocked_snapshot()
    if state != "valid" or record is None:
        return _blocked_snapshot()
    definitely_dead = False
    if recover_stale:
        try:
            definitely_dead = process_lease_is_dead(
                record.pid,
                record.process_identity,
            )
        except (OSError, RuntimeError, ValueError):
            definitely_dead = False
    if definitely_dead:
        lock_required = record.lock_protocol == _PROCESS_LOCK_PROTOCOL
        lock_state, recovery_lock = _claim_process_lock_for_recovery(
            path,
            record.token,
            required=lock_required,
            expected_device=record.lock_device,
            expected_inode=record.lock_inode,
        )
        if lock_state not in {"missing", "acquired"} or (
            lock_required and lock_state != "acquired"
        ):
            # A live inherited GAM descriptor, a missing lock promised by the
            # record, or unsafe lock metadata all remain fail-closed. Legacy
            # records may have no process lock, but if a token-matching lock
            # exists we always honor it so deleting the protocol field cannot
            # downgrade an active new lease.
            return ActivitySnapshot(
                kind=record.kind,
                started_at=record.timestamp,
            )
        removed = _unlink_if_token(
            path,
            record.token,
            expected_device=record.lock_device,
            expected_inode=record.lock_inode,
        )
        if recovery_lock is not None:
            recovery_lock.close(unlink=removed)
        if removed:
            return None
        # The record changed between observation and recovery. Do not make a
        # second liveness assumption about the replacement.
        state, replacement = _read_durable(path)
        if state == "missing":
            return None
        if state == "valid" and replacement is not None:
            return ActivitySnapshot(
                kind=replacement.kind,
                started_at=replacement.timestamp,
            )
        return _blocked_snapshot()
    return ActivitySnapshot(kind=record.kind, started_at=record.timestamp)


def _process_lock_path(path: Path, token: str) -> Path:
    return path.with_name(
        f"{path.name}.{token}{_DURABLE_PROCESS_LOCK_SUFFIX}"
    )


def _clear_free_orphan_process_locks(path: Path) -> bool:
    """Remove only provably unlocked token locks when their record is absent."""

    prefix = f"{path.name}."
    suffix = _DURABLE_PROCESS_LOCK_SUFFIX
    tokens: list[str] = []
    try:
        with os.scandir(path.parent) as entries:
            for entry in entries:
                name = entry.name
                if not name.startswith(prefix) or not name.endswith(suffix):
                    continue
                token = name[len(prefix) : -len(suffix)]
                if (
                    len(token) != 32
                    or any(
                        character not in "0123456789abcdef"
                        for character in token
                    )
                ):
                    return False
                tokens.append(token)
                if len(tokens) > _MAX_ORPHAN_PROCESS_LOCKS:
                    return False
    except OSError:
        return False
    for token in sorted(tokens):
        state, recovery_lock = _claim_process_lock_for_recovery(
            path,
            token,
            required=True,
        )
        if state != "acquired" or recovery_lock is None:
            return False
        lock_path = recovery_lock.path
        recovery_lock.close(unlink=True)
        try:
            os.lstat(lock_path)
        except FileNotFoundError:
            continue
        except OSError:
            return False
        return False
    return True


def _require_owner_only_regular(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("Durable activity process lock is not a regular file.")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise OSError("Durable activity process lock is not owner-only.")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise OSError("Durable activity process lock has a different owner.")


def _create_process_lock(path: Path, token: str) -> _DurableProcessLock:
    lock_path = _process_lock_path(path, token)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        current = os.lstat(lock_path)
        _require_owner_only_regular(metadata)
        if (
            current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
        ):
            raise OSError("Durable activity process lock was replaced.")
        try:
            import fcntl
        except ImportError as exc:
            raise OSError("Process locks are unavailable.") from exc
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _DurableProcessLock(
            path=lock_path,
            descriptor=descriptor,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
    except BaseException:
        try:
            current = os.lstat(lock_path)
            metadata = os.fstat(descriptor)
            if (
                current.st_dev == metadata.st_dev
                and current.st_ino == metadata.st_ino
            ):
                os.unlink(lock_path)
        except OSError:
            pass
        os.close(descriptor)
        raise


def _claim_process_lock_for_recovery(
    path: Path,
    token: str,
    *,
    required: bool,
    expected_device: int = 0,
    expected_inode: int = 0,
) -> tuple[str, Optional[_DurableProcessLock]]:
    lock_path = _process_lock_path(path, token)
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags)
    except FileNotFoundError:
        return ("blocked" if required else "missing"), None
    except OSError:
        return "blocked", None
    try:
        metadata = os.fstat(descriptor)
        current = os.lstat(lock_path)
        _require_owner_only_regular(metadata)
        if (
            current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
            or (
                expected_device > 0
                and metadata.st_dev != expected_device
            )
            or (
                expected_inode > 0
                and metadata.st_ino != expected_inode
            )
        ):
            raise OSError("Durable activity process lock was replaced.")
        try:
            import fcntl
        except ImportError as exc:
            raise OSError("Process locks are unavailable.") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return "held", None
        return (
            "acquired",
            _DurableProcessLock(
                path=lock_path,
                descriptor=descriptor,
                device=metadata.st_dev,
                inode=metadata.st_ino,
            ),
        )
    except OSError:
        os.close(descriptor)
        return "blocked", None


def _create_durable(path: Path, record: _DurableActivity) -> None:
    encoded = json.dumps(
        record.to_json(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_DURABLE_RECORD_BYTES:
        raise ValueError("Durable activity record is too large.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    metadata: Optional[os.stat_result] = None
    try:
        metadata = os.fstat(descriptor)
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("Could not write durable activity lease.")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        try:
            current = os.lstat(path)
            if (
                metadata is not None
                and current.st_dev == metadata.st_dev
                and current.st_ino == metadata.st_ino
            ):
                os.unlink(path)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def _unlink_if_token(
    path: Path,
    token: str,
    *,
    expected_device: int = 0,
    expected_inode: int = 0,
) -> bool:
    state, record = _read_durable(path)
    if state == "missing":
        return True
    if (
        state != "valid"
        or record is None
        or record.token != token
        or (
            expected_device > 0
            and record.lock_device != expected_device
        )
        or (
            expected_inode > 0
            and record.lock_inode != expected_inode
        )
    ):
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


@contextmanager
def _durable_guard(path: Path) -> Iterator[None]:
    parent = path.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise OSError("Unsafe durable activity directory.")
    os.chmod(parent, 0o700)
    guard = path.with_name(f"{path.name}{_DURABLE_GUARD_SUFFIX}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(guard, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            import fcntl
        except ImportError as exc:
            raise OSError("Cross-process file locks are unavailable.") from exc
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _macos_durable_activity_path() -> Optional[Path]:
    if sys.platform != "darwin":
        return None
    # Resolve lazily so first-launch tests, canaries, and alternate app-data
    # roots never inherit the importing process's Application Support path.
    from .paths import app_data_dir

    return app_data_dir() / DURABLE_ACTIVITY_FILENAME


activity_registry = ActivityRegistry(durable_path=_macos_durable_activity_path)
