from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_make_and_build_script_expose_only_fixed_profiles():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "build_app.sh").read_text(encoding="utf-8")
    assert 'PROFILE="$(PROFILE)" ./scripts/build_app.sh' in makefile
    assert "core|classroom-oneroster" in script
    assert "GAMGUI_BUILD_PROFILE" in script
    assert "write_artifact_sidecar" in script


def test_windows_release_uses_pinned_gam_exact_sha_and_self_test():
    script = (ROOT / "scripts" / "build_windows_release.ps1").read_text(
        encoding="utf-8"
    )
    fetch = (ROOT / "scripts" / "fetch_gam_windows.ps1").read_text(
        encoding="utf-8"
    )
    checksums = (ROOT / "scripts" / "gam_checksums.txt").read_text(
        encoding="utf-8"
    )

    assert "Packaged source changes are present" in script
    assert "GAMGUI_SOURCE_SHA" in script
    assert "write_artifact_sidecar" in script
    assert "GAMGUI_SELF_TEST_OUTPUT" in script
    assert "Compress-Archive" in script
    assert "CertificateSha256" in script
    assert "gamgui-updater.spec" in script
    assert "GamGUIUpdater.exe" in script
    assert "bootstrap-manifest.json" in script
    assert "ToolchainBundleDir" in script
    assert "embedded Windows artifact identity" in script
    assert "0x8664" in script
    assert "helper accepted an ordinary application launch" in script
    assert "Get-FileHash" in fetch
    assert "Checksum mismatch" in fetch
    assert "gam.exe" in fetch
    assert "gam-7.47.02-windows-x86_64.zip" in checksums


def test_windows_bootstrap_requires_explicit_local_trust_and_preserves_data():
    install = (ROOT / "scripts" / "install_windows_bootstrap.ps1").read_text(
        encoding="utf-8"
    )
    uninstall = (ROOT / "scripts" / "uninstall_windows_bootstrap.ps1").read_text(
        encoding="utf-8"
    )

    assert "TRUST GAMGUI LOCAL" in install
    assert "-TrustLocalCertificate" in install
    assert "RemoveTrust" in install
    assert "$createdCertificate" in install
    assert "$installedCurrent" in install
    assert '"$current.artifact.json"' in install
    assert "A bootstrap file failed its SHA-256 receipt" in install
    assert "GamGUIUpdater.exe" in install
    assert "--write-artifact-sidecar" in install
    assert "--self-test" in install
    assert "RemoveData" in uninstall
    assert 'if ($RemoveData' in uninstall


def test_windows_updater_helper_is_built_outside_the_application_bundle():
    spec = (ROOT / "gamgui-updater.spec").read_text(encoding="utf-8")
    entrypoint = (ROOT / "gamgui" / "windows_updater.py").read_text(
        encoding="utf-8"
    )

    assert 'name="GamGUIUpdater"' in spec
    assert "windows-toolchain.json" in spec
    assert "windows_local_signing.ps1" in spec
    assert '"--apply-update-helper" not in sys.argv[1:]' in entrypoint


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
    assert 'path.name.startswith("oneroster")' in spec
    assert 'path.name.startswith("_oneroster_")' in spec
    assert '"resources/components"' in spec
    assert 'gam_executable = "gam.exe" if os.name == "nt" else "gam"' in spec
    assert 'if platform.system() == "Darwin":' in spec
