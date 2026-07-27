from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_make_and_build_script_expose_only_fixed_profiles():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "build_app.sh").read_text(encoding="utf-8")
    assert 'PROFILE="$(PROFILE)" ./scripts/build_app.sh' in makefile
    assert "core|classroom-oneroster" in script
    assert "GAMGUI_BUILD_PROFILE" in script
    assert "write_artifact_sidecar" in script


def test_exact_sha_build_rejects_untracked_packaged_source():
    script = (ROOT / "scripts" / "build_app.sh").read_text(encoding="utf-8")
    assert "git status --porcelain --untracked-files=all" in script
    assert "PACKAGED_SOURCE_STATUS" in script
    assert "main.py" in script
    assert "gamgui.spec" in script
    assert "scripts/build_app.sh" in script
    assert "scripts/fetch_gam.sh" in script
    assert "scripts/gam_checksums.txt" in script
    assert "Packaged source changes or untracked files are present" in script
    assert 'GAMGUI_ALLOW_DIRTY_BUILD:-}" != "1"' in script


def test_app_build_always_reestablishes_pinned_gam_payload_atomically():
    build = (ROOT / "scripts" / "build_app.sh").read_text(encoding="utf-8")
    fetch = (ROOT / "scripts" / "fetch_gam.sh").read_text(encoding="utf-8")

    assert 'echo "==> Re-establishing pinned GAM payload..."' in build
    assert "./scripts/fetch_gam.sh" in build
    assert 'if [ ! -x "gamgui/resources/gam7/gam" ]' not in build
    assert "INSTALL_STAGE" in fetch
    assert "INSTALL_BACKUP" in fetch
    assert 'mktemp -d "$DEST_PARENT/.gam7-stage.XXXXXX"' in fetch
    assert 'mv -- "$INSTALL_STAGE" "$DEST"' in fetch
    assert "checksum mismatch" in fetch
    assert "no pinned checksum" in fetch


def test_pyinstaller_profile_excludes_optional_code_and_assets_from_core():
    spec = (ROOT / "gamgui.spec").read_text(encoding="utf-8")
    assert 'excludes.append("gamgui.components.oneroster")' in spec
    assert 'collect_submodules("gamgui.components.oneroster")' in spec
    assert 'hiddenimports.append("gamgui.web.routes.oneroster")' in spec
    assert 'excludes.append("gamgui.web.routes.oneroster")' in spec
    assert 'path.name.startswith("_oneroster_")' in spec
    assert '"resources/components"' in spec
