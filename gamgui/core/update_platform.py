"""Platform-shaped paths and process primitives for the shared updater."""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Optional

MACOS = "macos"
WINDOWS = "windows"


def runtime_platform(value: Optional[str] = None) -> str:
    platform = sys.platform if value is None else value
    if platform == "darwin":
        return MACOS
    if platform == "win32":
        return WINDOWS
    return ""


def bundle_executable(bundle: Path, platform_name: str = "") -> Path:
    root = Path(bundle)
    selected = platform_name or infer_bundle_platform(root)
    if selected == WINDOWS:
        return root / "GamGUI.exe"
    return root / "Contents" / "MacOS" / "GamGUI"


def bundle_is_complete(bundle: Path, platform_name: str = "") -> bool:
    root = Path(bundle)
    selected = platform_name or infer_bundle_platform(root)
    if selected == MACOS and root.suffix != ".app":
        return False
    if selected == WINDOWS and root.name.lower() not in {"gamgui", "current"}:
        return False
    return (
        bool(selected)
        and root.is_dir()
        and not root.is_symlink()
        and bundle_executable(root, selected).is_file()
    )


def infer_bundle_platform(bundle: Path) -> str:
    root = Path(bundle)
    if root.suffix == ".app" and (root / "Contents" / "MacOS" / "GamGUI").is_file():
        return MACOS
    if (root / "GamGUI.exe").is_file():
        return WINDOWS
    return ""


def installed_bundle_path(
    executable: Optional[Path] = None,
    *,
    platform_name: str = "",
) -> Optional[Path]:
    path = Path(executable) if executable is not None else Path(sys.executable).resolve()
    selected = platform_name
    if not selected and executable is not None:
        if any(parent.suffix == ".app" for parent in path.parents):
            selected = MACOS
        elif path.name.lower() == "gamgui.exe":
            selected = WINDOWS
    selected = selected or runtime_platform()
    if selected == WINDOWS:
        for parent in (path.parent, *path.parents):
            if parent.name.lower() == "current" and (parent / "GamGUI.exe").is_file():
                return parent
        return None
    for parent in path.parents:
        if parent.suffix == ".app":
            return parent
    return None


def windows_installation_root(local_app_data: Optional[Path] = None) -> Path:
    base = (
        Path(local_app_data)
        if local_app_data is not None
        else Path(os.environ.get("LOCALAPPDATA", ""))
    )
    if not str(base):
        raise RuntimeError("LOCALAPPDATA is unavailable.")
    return base / "Programs" / "GamGUI"


def windows_updater_helper_path(data_root: Path) -> Path:
    return Path(data_root) / "updater" / "GamGUIUpdater.exe"


def windows_mutex_name(data_root: Path) -> str:
    digest = hashlib.sha256(str(Path(data_root).resolve()).lower().encode("utf-8")).hexdigest()
    return f"Local\\GamGUIUpdater-{digest[:24]}"


class WindowsNamedMutex:
    """Inheritable, per-user Windows mutex used across updater handoff."""

    WAIT_OBJECT_0 = 0
    WAIT_ABANDONED = 0x80

    def __init__(self, handle: int, name: str, *, owned: bool) -> None:
        self.handle = int(handle)
        self.name = name
        self.owned = owned

    @classmethod
    def acquire(cls, name: str) -> Optional["WindowsNamedMutex"]:
        if sys.platform != "win32" or not re.fullmatch(r"Local\\GamGUIUpdater-[0-9a-f]{24}", name):
            return None
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, True, name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Could not create updater mutex.")
        if ctypes.get_last_error() == 183:
            kernel32.CloseHandle(handle)
            return None
        if not kernel32.SetHandleInformation(handle, 1, 1):
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "Could not make updater mutex inheritable.")
        return cls(handle, name, owned=True)

    @classmethod
    def adopt(cls, handle: int, name: str) -> "WindowsNamedMutex":
        if sys.platform != "win32" or int(handle) <= 0:
            raise ValueError("The updater mutex handle is invalid.")
        mutex = cls(int(handle), name, owned=True)
        if not re.fullmatch(r"Local\\GamGUIUpdater-[0-9a-f]{24}", name):
            mutex.close()
            raise ValueError("The updater mutex name is invalid.")
        return mutex

    def close(self) -> None:
        if not self.handle:
            return
        kernel32 = ctypes.windll.kernel32
        if self.owned:
            kernel32.ReleaseMutex(self.handle)
        kernel32.CloseHandle(self.handle)
        self.handle = 0
        self.owned = False

    def __enter__(self) -> "WindowsNamedMutex":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
