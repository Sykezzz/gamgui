"""Owner-only cross-process exclusion for application activation.

The managed-mac updater needs an operating-system lock that survives the handoff
from the running application to its updater helper.  ``ActivityRegistry`` is
intentionally process-local, so it cannot protect that boundary.

The parent passes :attr:`OwnerOnlyActivationLock.fileno` to ``subprocess.Popen``
via ``pass_fds``.  After a successful spawn it calls
:meth:`OwnerOnlyActivationLock.close_after_handoff`, which closes the parent's
descriptor *without* issuing an explicit unlock.  The inherited descriptor then
continues to hold the same POSIX ``flock`` until the helper adopts and releases it.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Optional, Protocol


class ActivationLockError(RuntimeError):
    """The activation lock could not be created, validated, or operated."""


class ActivationLockSecurityError(ActivationLockError):
    """The lock path or inherited descriptor failed a security check."""


class ActivationLockBackend(Protocol):
    """Small injectable seam for platform locking primitives."""

    def try_lock(self, fd: int) -> bool:
        """Acquire an exclusive lock without waiting, returning false when busy."""

    def unlock(self, fd: int) -> None:
        """Release the exclusive lock held by ``fd``."""


class _PosixFlockBackend:
    """Lazy ``fcntl`` adapter so importing this module remains safe on Windows."""

    def try_lock(self, fd: int) -> bool:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, PermissionError):
            return False
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise

    def unlock(self, fd: int) -> None:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class _WindowsByteRangeBackend:
    """Nonblocking one-byte lock used only by Windows-hosted tests/tools.

    Production activation runs on macOS and therefore uses ``flock``.  Providing
    a real Windows backend keeps imports and focused unit tests safe without
    weakening the production path or adding a third-party dependency.
    """

    def try_lock(self, fd: int) -> bool:
        import msvcrt

        if os.fstat(fd).st_size == 0:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
            } or getattr(exc, "winerror", None) in {32, 33, 36}:
                return False
            raise

    def unlock(self, fd: int) -> None:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _default_backend() -> ActivationLockBackend:
    return _PosixFlockBackend() if os.name == "posix" else _WindowsByteRangeBackend()


class OwnerOnlyActivationLock:
    """An exclusive, nonblocking, owner-only lock suitable for helper handoff."""

    def __init__(
        self,
        path: Path,
        fd: int,
        backend: ActivationLockBackend,
    ) -> None:
        self.path = Path(path)
        self._fd = int(fd)
        self._backend = backend
        self._closed = False

    @classmethod
    def try_acquire(
        cls,
        path: Path,
        *,
        backend: Optional[ActivationLockBackend] = None,
    ) -> Optional["OwnerOnlyActivationLock"]:
        """Acquire ``path`` exclusively without waiting.

        ``None`` means another process owns the lock.  Unsafe paths and operating
        system failures raise a stable lock exception instead of being mistaken
        for ordinary contention.
        """

        lock_path = Path(path)
        selected_backend = backend or _default_backend()
        _secure_lock_directory(lock_path.parent)
        fd = _open_owner_only_regular_file(lock_path)
        try:
            try:
                acquired = selected_backend.try_lock(fd)
            except OSError as exc:
                raise ActivationLockError(
                    "The application activation lock could not be acquired."
                ) from exc
            if not acquired:
                os.close(fd)
                return None
            return cls(lock_path, fd, selected_backend)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    @classmethod
    def adopt(
        cls,
        path: Path,
        fd: int,
        *,
        backend: Optional[ActivationLockBackend] = None,
    ) -> "OwnerOnlyActivationLock":
        """Adopt and validate an inherited lock descriptor.

        The descriptor is consumed by this call and is closed if validation
        fails. Re-locking an inherited POSIX open-file description is harmless
        and proves that an arbitrary, separately opened descriptor is not held
        by another activation process.
        """

        lock_path = Path(path)
        selected_backend = backend or _default_backend()
        inherited_fd = int(fd)
        try:
            _secure_lock_directory(lock_path.parent)
            _validate_open_lock_file(lock_path, inherited_fd)
            try:
                acquired = selected_backend.try_lock(inherited_fd)
            except OSError as exc:
                raise ActivationLockError(
                    "The inherited application activation lock could not be verified."
                ) from exc
            if not acquired:
                raise ActivationLockSecurityError(
                    "The inherited application activation descriptor does not own the lock."
                )
            return cls(lock_path, inherited_fd, selected_backend)
        except BaseException:
            try:
                os.close(inherited_fd)
            except OSError:
                pass
            raise

    @property
    def fileno(self) -> int:
        """Descriptor to include in ``Popen(pass_fds=(lock.fileno,))``."""

        if self._closed:
            raise ActivationLockError("The application activation lock is closed.")
        return self._fd

    @property
    def held(self) -> bool:
        return not self._closed

    def release(self) -> None:
        """Explicitly unlock and close this process's descriptor."""

        if self._closed:
            return
        fd = self._fd
        self._closed = True
        self._fd = -1
        unlock_error: Optional[BaseException] = None
        try:
            self._backend.unlock(fd)
        except BaseException as exc:
            unlock_error = exc
        finally:
            try:
                os.close(fd)
            except OSError as exc:
                if unlock_error is None:
                    unlock_error = exc
        if unlock_error is not None:
            raise ActivationLockError(
                "The application activation lock could not be released cleanly."
            ) from unlock_error

    def close_after_handoff(self) -> None:
        """Close the parent's descriptor without unlocking the child's copy.

        Call this only after ``Popen`` successfully inherited :attr:`fileno`.
        Calling ``LOCK_UN`` here would release the lock shared by the inherited
        POSIX open-file description and open a race before helper adoption.
        """

        if self._closed:
            return
        fd = self._fd
        self._closed = True
        self._fd = -1
        try:
            os.close(fd)
        except OSError as exc:
            raise ActivationLockError(
                "The parent activation descriptor could not be closed."
            ) from exc

    def __enter__(self) -> "OwnerOnlyActivationLock":
        if self._closed:
            raise ActivationLockError("The application activation lock is closed.")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def _secure_lock_directory(path: Path) -> None:
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = path.lstat()
    except OSError as exc:
        raise ActivationLockSecurityError(
            "The application activation lock directory is unavailable."
        ) from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise ActivationLockSecurityError(
            "The application activation lock directory is not a regular directory."
        )
    _require_current_owner(details, "directory")
    try:
        os.chmod(path, 0o700)
        secured = path.lstat()
    except OSError as exc:
        raise ActivationLockSecurityError(
            "The application activation lock directory could not be secured."
        ) from exc
    # Windows' ``stat`` emulates POSIX mode bits and cannot represent the ACL
    # that actually protects the file. Production activation is macOS-only;
    # retain the structural checks and byte-range lock on Windows so tests and
    # support tools remain usable without pretending its mode bits are ACLs.
    if os.name == "posix" and stat.S_IMODE(secured.st_mode) & 0o077:
        raise ActivationLockSecurityError(
            "The application activation lock directory is not owner-only."
        )


def _open_owner_only_regular_file(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ActivationLockSecurityError(
            "The application activation lock file could not be opened safely."
        ) from exc
    try:
        _validate_open_lock_file(path, fd)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:  # pragma: no cover - Windows exposes chmod but not always fchmod.
            os.chmod(path, 0o600)
        secured = os.fstat(fd)
        if os.name == "posix" and stat.S_IMODE(secured.st_mode) & 0o077:
            raise ActivationLockSecurityError(
                "The application activation lock file is not owner-only."
            )
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _validate_open_lock_file(path: Path, fd: int) -> None:
    try:
        opened = os.fstat(fd)
        named = Path(path).lstat()
    except OSError as exc:
        raise ActivationLockSecurityError(
            "The application activation lock descriptor is invalid."
        ) from exc
    if (
        stat.S_ISLNK(named.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or not stat.S_ISREG(opened.st_mode)
    ):
        raise ActivationLockSecurityError(
            "The application activation lock must be a regular file."
        )
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise ActivationLockSecurityError(
            "The application activation lock path changed while it was opened."
        )
    _require_current_owner(opened, "file")


def _require_current_owner(details: os.stat_result, kind: str) -> None:
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        return
    if int(details.st_uid) != int(getuid()):
        raise ActivationLockSecurityError(
            f"The application activation lock {kind} is owned by another user."
        )
