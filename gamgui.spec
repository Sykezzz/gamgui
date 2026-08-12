# PyInstaller spec — builds one immutable GamGUI.app profile (macOS).
# Use `make app PROFILE=core|classroom-oneroster`.
import json
import os
import platform
import subprocess
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

from gamgui import __version__
from gamgui.core.components import (
    CORE_PROFILE,
    ONEROSTER_PROFILE,
    ComponentManifest,
    build_profile_payload,
    manifests_for_profile,
    normalize_profile,
)


def source_sha():
    value = os.environ.get("GAMGUI_SOURCE_SHA", "").strip().lower()
    if not value:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
        ).strip().lower()
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise SystemExit("GAMGUI_SOURCE_SHA must be an exact 40-character Git SHA.")
    return value


def tree_datas(root, destination, *, exclude_oneroster=False):
    root_path = Path(root)
    result = []
    for path in sorted(item for item in root_path.rglob("*") if item.is_file()):
        relative = path.relative_to(root_path)
        if exclude_oneroster and (
            path.name.startswith("oneroster")
            or path.name.startswith("_oneroster_")
            or "oneroster" in {part.lower() for part in relative.parts}
        ):
            continue
        result.append(
            (
                str(path),
                str(Path(destination) / relative.parent),
            )
        )
    return result


profile = normalize_profile(os.environ.get("GAMGUI_BUILD_PROFILE"), default=CORE_PROFILE)
source = source_sha()
minimum_macos = os.environ.get("GAMGUI_MINIMUM_MACOS", "12.0")
packaging_revision = os.environ.get("GAMGUI_PACKAGING_REVISION", "1")
architecture = os.environ.get("GAMGUI_BUILD_ARCH", platform.machine())
artifact_platform = os.environ.get(
    "GAMGUI_BUILD_PLATFORM",
    "macos" if platform.system() == "Darwin" else "windows",
)
bundle_format = os.environ.get(
    "GAMGUI_BUNDLE_FORMAT",
    "app-bundle" if artifact_platform == "macos" else "onedir",
)
signer_thumbprint = os.environ.get("GAMGUI_SIGNER_THUMBPRINT", "")
toolchain_manifest_digest = os.environ.get(
    "GAMGUI_TOOLCHAIN_MANIFEST_DIGEST", ""
)
metadata_dir = Path(os.environ.get("GAMGUI_BUILD_METADATA_DIR", "build/profile-metadata"))
metadata_dir.mkdir(parents=True, exist_ok=True)
profile_metadata = metadata_dir / "profile.json"
profile_metadata.write_text(
    json.dumps(
        build_profile_payload(
            profile,
            source_sha=source,
            version=__version__,
            architecture=architecture,
            minimum_macos_version=minimum_macos,
            packaging_revision=packaging_revision,
            platform_name=artifact_platform,
            bundle_format=bundle_format,
            signer_thumbprint=signer_thumbprint,
            toolchain_manifest_digest=toolchain_manifest_digest,
        ),
        sort_keys=True,
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)

exclude_oneroster = profile == CORE_PROFILE
datas = tree_datas(
    "gamgui/web/templates",
    "gamgui/web/templates",
    exclude_oneroster=exclude_oneroster,
)
datas += tree_datas(
    "gamgui/web/static",
    "gamgui/web/static",
    exclude_oneroster=exclude_oneroster,
)
datas.append((str(profile_metadata), "resources/components"))
binaries = []
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("keyring.backends")
    + ["uvicorn.lifespan.on", "uvicorn.loops.auto", "uvicorn.protocols.http.auto"]
)
excludes = ["tkinter"]

if profile == ONEROSTER_PROFILE:
    hiddenimports += collect_submodules("gamgui.components.oneroster")
    hiddenimports.append("gamgui.web.routes.oneroster")
    component_json = Path("gamgui/components/oneroster/component.json")
    if not component_json.is_file():
        raise SystemExit("The OneRoster profile is missing its component.json manifest.")
    component_payload = json.loads(component_json.read_text(encoding="utf-8"))
    component_manifest = ComponentManifest.from_json(component_payload)
    if (
        component_payload != component_manifest.to_json()
        or (component_manifest,) != manifests_for_profile(profile)
    ):
        raise SystemExit(
            "The packaged OneRoster component.json does not match the code-owned manifest."
        )
    datas.append((str(component_json), "gamgui/components/oneroster"))
else:
    excludes.append("gamgui.components.oneroster")
    excludes.append("gamgui.web.routes.oneroster")

# pywebview + its macOS (Cocoa/WebKit via pyobjc) backend — collect everything it needs.
_wv_datas, _wv_binaries, _wv_hidden = collect_all("webview")
datas += _wv_datas
binaries += _wv_binaries
hiddenimports += _wv_hidden

# Bundle the vendored GAM7 binary (resolved at runtime via sys._MEIPASS/resources/gam7/gam).
gam_executable = "gam.exe" if os.name == "nt" else "gam"
if os.path.isdir("gamgui/resources/gam7") and os.path.exists(
    os.path.join("gamgui/resources/gam7", gam_executable)
):
    datas.append(("gamgui/resources/gam7", "resources/gam7"))

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="GamGUI", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="GamGUI")
if platform.system() == "Darwin":
    app = BUNDLE(
        coll,
        name="GamGUI.app",
        icon=None,
        bundle_identifier="io.github.goetchstone.gamgui",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": minimum_macos,
            "GamGUIBuildProfile": profile,
            "GamGUISourceSHA": source,
        },
    )
