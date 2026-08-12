"""Standalone Windows updater helper built outside the replaceable app directory."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

datas = []
manifest = Path("gamgui/resources/updater/windows-toolchain.json")
signing = Path("scripts/windows_local_signing.ps1")
if manifest.is_file():
    datas.append((str(manifest), "resources/updater"))
if signing.is_file():
    datas.append((str(signing), "resources/updater"))

a = Analysis(
    ["gamgui/windows_updater.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("keyring.backends"),
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="GamGUIUpdater",
    console=False,
)
