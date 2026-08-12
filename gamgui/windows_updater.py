"""Standalone Windows activation helper entry point."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _argument_value(name: str) -> str:
    try:
        index = sys.argv.index(name)
        return sys.argv[index + 1]
    except (ValueError, IndexError):
        return ""


def _promote_helper(destination: str) -> None:
    from .core.paths import app_data_dir
    from .core.update_platform import windows_updater_helper_path

    expected = windows_updater_helper_path(app_data_dir()).resolve()
    target = Path(destination).resolve()
    if target != expected:
        raise ValueError("The updater helper promotion path is invalid.")
    incoming = target.with_name(f".{target.name}.{os.getpid()}.incoming")
    shutil.copy2(Path(sys.executable), incoming)
    os.replace(incoming, target)


def _launch_installed() -> int:
    if sys.platform != "win32":
        return 2
    from .core.activation_lock import OwnerOnlyActivationLock
    from .core.components import bundle_executable, bundle_is_complete
    from .core.paths import app_data_dir
    from .core.update_platform import (
        WindowsNamedMutex,
        windows_installation_root,
        windows_mutex_name,
    )
    from .core.updater import LocalUpdateInstaller, UpdateStateStore

    data_root = app_data_dir()
    store = UpdateStateStore()
    lock = OwnerOnlyActivationLock.try_acquire(data_root / "updates" / "activation.lock")
    if lock is None:
        return 1
    try:
        mutex = WindowsNamedMutex.acquire(windows_mutex_name(data_root))
    except OSError:
        lock.release()
        return 1
    if mutex is None:
        lock.release()
        return 1
    try:
        state = store.load()
        root = Path(state.windows_installation_root or windows_installation_root())
        current = root / "current"
        journal = state.activation_journal
        if journal is not None:
            if state.activation_journal_invalid:
                return 1
            if not LocalUpdateInstaller(store=store).recover(
                current_app=current,
                activation_lock=lock,
                transaction_id=journal.transaction_id,
            ):
                return 1
        if not bundle_is_complete(current, "windows"):
            return 1
        subprocess.Popen(
            [str(bundle_executable(current, "windows"))],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return 0
    finally:
        lock.release()
        mutex.close()


def main() -> int:
    if "--launch-installed" in sys.argv[1:]:
        return _launch_installed()
    if "--apply-update-helper" not in sys.argv[1:]:
        return 2
    promotion = _argument_value("--promote-helper")
    if promotion:
        try:
            _promote_helper(promotion)
        except (OSError, ValueError):
            return 1
    from .app import main as app_main

    return app_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
