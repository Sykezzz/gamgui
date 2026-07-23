"""Per-OS app-data directory — keeps GamGUI's on-disk footprint cross-platform."""

from pathlib import Path

from gamgui.core.paths import APP_DATA_ENV, APP_NAME, app_data_dir


def test_explicit_override(monkeypatch, tmp_path):
    root = tmp_path / "isolated"
    monkeypatch.setenv(APP_DATA_ENV, str(root))
    assert app_data_dir() == root


def test_macos(monkeypatch):
    monkeypatch.delenv(APP_DATA_ENV, raising=False)
    monkeypatch.setattr("sys.platform", "darwin")
    p = app_data_dir()
    assert p.name == APP_NAME and "Library/Application Support" in p.as_posix()


def test_windows(monkeypatch, tmp_path):
    monkeypatch.delenv(APP_DATA_ENV, raising=False)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    p = app_data_dir()
    assert p == tmp_path / "AppData" / "Local" / APP_NAME


def test_linux_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv(APP_DATA_ENV, raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert app_data_dir() == tmp_path / "xdg" / APP_NAME


def test_linux_default(monkeypatch):
    monkeypatch.delenv(APP_DATA_ENV, raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    p = app_data_dir()
    assert p == Path.home() / ".local" / "share" / APP_NAME
