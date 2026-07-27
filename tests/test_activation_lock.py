from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from gamgui.core.activation_lock import (
    ActivationLockSecurityError,
    OwnerOnlyActivationLock,
)


class RecordingBackend:
    def __init__(self, *, acquired: bool = True) -> None:
        self.acquired = acquired
        self.lock_calls: list[int] = []
        self.unlock_calls: list[int] = []

    def try_lock(self, fd: int) -> bool:
        self.lock_calls.append(fd)
        return self.acquired

    def unlock(self, fd: int) -> None:
        self.unlock_calls.append(fd)


def test_injectable_backend_keeps_import_and_basic_use_cross_platform(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend()
    path = tmp_path / "updates" / "activation.lock"

    lock = OwnerOnlyActivationLock.try_acquire(path, backend=backend)

    assert lock is not None and lock.held
    assert len(backend.lock_calls) == 1
    lock.release()
    assert not lock.held
    assert backend.unlock_calls == backend.lock_calls


def test_busy_backend_is_nonblocking_and_returns_none(tmp_path: Path) -> None:
    backend = RecordingBackend(acquired=False)
    path = tmp_path / "updates" / "activation.lock"

    assert OwnerOnlyActivationLock.try_acquire(path, backend=backend) is None
    assert len(backend.lock_calls) == 1
    assert backend.unlock_calls == []


def test_close_after_handoff_closes_without_explicit_unlock(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend()
    path = tmp_path / "updates" / "activation.lock"
    lock = OwnerOnlyActivationLock.try_acquire(path, backend=backend)
    assert lock is not None
    fd = lock.fileno

    lock.close_after_handoff()

    assert not lock.held
    assert backend.unlock_calls == []
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.skipif(os.name != "posix", reason="POSIX flock contract")
def test_posix_lock_is_owner_only_nonblocking_and_reacquirable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "updates" / "activation.lock"
    first = OwnerOnlyActivationLock.try_acquire(path)
    assert first is not None

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert OwnerOnlyActivationLock.try_acquire(path) is None

    first.release()
    second = OwnerOnlyActivationLock.try_acquire(path)
    assert second is not None
    second.release()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor inheritance contract")
def test_adopted_duplicate_keeps_lock_after_parent_detach(
    tmp_path: Path,
) -> None:
    path = tmp_path / "updates" / "activation.lock"
    parent = OwnerOnlyActivationLock.try_acquire(path)
    assert parent is not None
    inherited_fd = os.dup(parent.fileno)

    parent.close_after_handoff()
    helper = OwnerOnlyActivationLock.adopt(path, inherited_fd)

    assert helper.held
    assert OwnerOnlyActivationLock.try_acquire(path) is None
    helper.release()
    replacement = OwnerOnlyActivationLock.try_acquire(path)
    assert replacement is not None
    replacement.release()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor inheritance contract")
def test_real_subprocess_inherits_lock_until_helper_releases(
    tmp_path: Path,
) -> None:
    path = tmp_path / "updates" / "activation.lock"
    parent = OwnerOnlyActivationLock.try_acquire(path)
    assert parent is not None
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from gamgui.core.activation_lock import OwnerOnlyActivationLock\n"
        "lock = OwnerOnlyActivationLock.adopt(Path(sys.argv[1]), int(sys.argv[2]))\n"
        "print('ready', flush=True)\n"
        "sys.stdin.readline()\n"
        "lock.release()\n"
    )
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(path),
            str(parent.fileno),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True,
        pass_fds=(parent.fileno,),
    )
    parent.close_after_handoff()
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "ready"
    assert OwnerOnlyActivationLock.try_acquire(path) is None

    assert child.stdin is not None
    child.stdin.write("\n")
    child.stdin.flush()
    assert child.wait(timeout=10) == 0
    replacement = OwnerOnlyActivationLock.try_acquire(path)
    assert replacement is not None
    replacement.release()


def test_adopt_rejects_descriptor_for_another_inode(tmp_path: Path) -> None:
    path = tmp_path / "updates" / "activation.lock"
    lock = OwnerOnlyActivationLock.try_acquire(
        path,
        backend=RecordingBackend(),
    )
    assert lock is not None
    other = tmp_path / "updates" / "other.lock"
    other.write_bytes(b"")
    wrong_fd = os.open(other, os.O_RDWR)

    with pytest.raises(ActivationLockSecurityError, match="path changed"):
        OwnerOnlyActivationLock.adopt(
            path,
            wrong_fd,
            backend=RecordingBackend(),
        )
    with pytest.raises(OSError):
        os.fstat(wrong_fd)
    lock.release()


@pytest.mark.skipif(os.name != "posix", reason="reliable symlink semantics")
def test_symlink_lock_file_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "updates"
    directory.mkdir()
    target = directory / "target"
    target.write_bytes(b"")
    link = directory / "activation.lock"
    link.symlink_to(target.name)

    with pytest.raises(ActivationLockSecurityError):
        OwnerOnlyActivationLock.try_acquire(link)


@pytest.mark.skipif(os.name != "posix", reason="reliable symlink semantics")
def test_symlink_lock_directory_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(ActivationLockSecurityError, match="regular directory"):
        OwnerOnlyActivationLock.try_acquire(linked / "activation.lock")
