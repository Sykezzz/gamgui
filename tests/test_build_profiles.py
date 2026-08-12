from pathlib import Path
import json


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


def test_windows_bootstrap_is_transactional_sanitized_and_preserves_data():
    install = (ROOT / "scripts" / "install_windows_bootstrap.ps1").read_text(
        encoding="utf-8"
    )
    uninstall = (ROOT / "scripts" / "uninstall_windows_bootstrap.ps1").read_text(
        encoding="utf-8"
    )

    assert 'ValidateSet("Interactive", "Pretrusted")' in install
    assert "PretrustedSignerSha256" in install
    assert "TrustApproved" in install
    assert "-TrustLocalCertificate" in install
    assert "RemoveTrust" in install
    assert "$script:createdCertificate" in install
    assert "$script:installedCurrent" in install
    assert "bootstrap-install.json" in install
    assert "Recover-IncompleteBootstrap" in install
    assert "Write-AtomicJson" in install
    assert "schema_version" in install
    assert "message_code" in install
    assert '"$current.artifact.json"' in install
    assert "A bootstrap file failed its SHA-256 receipt" in install
    assert "GamGUIUpdater.exe" in install
    assert '$shortcut.TargetPath = $helper' in install
    assert '$shortcut.Arguments = "--launch-installed"' in install
    assert "--write-artifact-sidecar" in install
    assert "--self-test" in install
    assert "AdditionalFileToSign" in install
    assert "outside the installer directory" in install
    assert install.index('$script:installedCurrent = $true') < install.index(
        "Move-Item -LiteralPath $incomingCurrent -Destination $current"
    )
    assert install.index('$script:installedHelper = $true') < install.index(
        'Copy-Item -LiteralPath (Join-Path $bootstrapRoot "updater\\GamGUIUpdater.exe")'
    )
    assert "Remove-Item -LiteralPath $statePath" in install
    assert "RemoveData" in uninstall
    assert 'if ($RemoveData' in uninstall
    assert '"updates"' in uninstall


def test_windows_updater_helper_is_built_outside_the_application_bundle():
    spec = (ROOT / "gamgui-updater.spec").read_text(encoding="utf-8")
    entrypoint = (ROOT / "gamgui" / "windows_updater.py").read_text(
        encoding="utf-8"
    )

    assert 'name="GamGUIUpdater"' in spec
    assert "windows-toolchain.json" in spec
    assert "windows_local_signing.ps1" in spec
    assert '"--apply-update-helper" not in sys.argv[1:]' in entrypoint


def test_windows_setup_wizard_is_native_offline_and_fail_closed():
    wizard = (ROOT / "scripts" / "windows_setup.iss").read_text(encoding="utf-8")

    assert "Inno Setup" not in wizard  # no compiler path or runtime download
    assert "PrivilegesRequired=lowest" in wizard
    assert "MinVersion=10.0.22000" in wizard
    assert "ArchitecturesAllowed=x64compatible" in wizard
    assert "Classroom + OneRoster (recommended)" in wizard
    assert "Trust and install" in wizard
    assert "TrustCheck.Checked := False" in wizard
    assert "desktopicon" in wizard and "Flags: unchecked" in wizard
    assert "Launch GamGUI" in wizard and "skipifsilent" in wizard
    assert "GamGUI is already installed" in wizard
    assert "PrepareToInstall" in wizard
    assert "/PINNEDSIGNERSHA256" in wizard
    assert "Silent setup never creates or trusts a certificate" in wizard
    assert "Google, GAM, Keychain, or tenant services" in wizard
    assert "setup-progress.json" in wizard
    assert "SETUP-RUNNING-SELF-TEST" in wizard
    assert "Also delete local application data" in wizard


def test_windows_setup_builder_pins_compiler_and_emits_unsigned_receipts():
    build = (ROOT / "scripts" / "build_windows_setup.ps1").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(
        (ROOT / "gamgui/resources/installer/windows-installer-toolchain.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["compiler"]["version"] == "7.0.2"
    assert len(manifest["compiler"]["sha256"]) == 64
    assert len(manifest["compiler"]["executable_sha256"]) == 64
    assert manifest["compiler"]["publisher"] == "Pyrsys B.V."
    assert "Get-AuthenticodeSignature" in build
    assert "The checkout does not match the requested exact SHA" in build
    assert 'foreach ($profile in @("core", "classroom-oneroster"))' in build
    assert "2GB" in build
    assert 'signing_status = "NotSigned"' in build
    assert "windows-bootstrap-manifest.json" in build
    assert "gamgui-windows-setup-release-v1" in build


def test_windows_prerelease_is_exact_sha_protected_and_exercises_setup():
    workflow = (ROOT / ".github/workflows/windows-prerelease.yml").read_text(
        encoding="utf-8"
    )
    exercise = (ROOT / "scripts/test_windows_setup.ps1").read_text(
        encoding="utf-8"
    )

    assert "v0.0.1-windows.1" in workflow
    assert "origin/district-main" in workflow
    assert "origin/update-ready" in workflow
    assert 'environment: windows-prerelease' in workflow
    assert 'signing_status -ne "NotSigned"' in workflow
    assert "actions/attest-build-provenance@" in workflow
    assert "--prerelease" in workflow
    assert "--verify-tag" in workflow
    assert "SmartScreen" in workflow
    assert "Windows offline suite (py${{ matrix.python }})" in workflow
    assert '["3.10", "3.12", "3.14"]' in workflow
    assert 'if (-not $env:CI)' in exercise
    assert "unsafe Setup invocation unexpectedly succeeded" in exercise
    assert 'Exercise-Profile "core"' in exercise
    assert 'Exercise-Profile "classroom-oneroster"' in exercise
    assert "Apps & Features" in exercise
    assert "tampered bootstrap was accepted" in exercise


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
