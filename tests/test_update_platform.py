from __future__ import annotations

from pathlib import Path

from gamgui.core.update_platform import (
    MACOS,
    WINDOWS,
    bundle_executable,
    bundle_is_complete,
    installed_bundle_path,
    windows_installation_root,
    windows_mutex_name,
)


def test_platform_bundle_shapes_and_installed_paths(tmp_path):
    mac = tmp_path / "Applications" / "GamGUI.app"
    mac_executable = mac / "Contents" / "MacOS" / "GamGUI"
    mac_executable.parent.mkdir(parents=True)
    mac_executable.write_text("app", encoding="utf-8")
    windows = tmp_path / "Local" / "Programs" / "GamGUI" / "current"
    windows_executable = windows / "GamGUI.exe"
    windows.mkdir(parents=True)
    windows_executable.write_bytes(b"app")

    assert bundle_executable(mac, MACOS) == mac_executable
    assert bundle_executable(windows, WINDOWS) == windows_executable
    assert bundle_is_complete(mac, MACOS)
    assert bundle_is_complete(windows, WINDOWS)
    assert installed_bundle_path(mac_executable, platform_name=MACOS) == mac
    assert installed_bundle_path(windows_executable, platform_name=WINDOWS) == windows


def test_windows_paths_are_per_user_and_mutex_is_stable(tmp_path):
    installation = windows_installation_root(tmp_path)
    assert installation == tmp_path / "Programs" / "GamGUI"
    assert windows_mutex_name(tmp_path) == windows_mutex_name(tmp_path)
    assert windows_mutex_name(tmp_path).startswith("Local\\GamGUIUpdater-")
