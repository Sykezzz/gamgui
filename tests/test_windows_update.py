from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path

import pytest

from gamgui.core.components import CORE_PROFILE, build_profile_payload, verify_bundle_artifact, write_artifact_sidecar
from gamgui.core.update_platform import WindowsNamedMutex, windows_mutex_name
from gamgui.core.updater import (
    ACTIVATION_APP_UPDATE,
    LocalUpdateInstaller,
    UpdateCandidate,
    UpdateState,
    UpdateStateStore,
    _windows_directory_exchange,
)
from gamgui.core.windows_update import ToolchainAsset, WindowsLocalUpdateBuilder, WindowsToolchain

SHA = "a" * 40


def test_committed_windows_toolchain_manifest_is_exact_and_digestible(tmp_path):
    toolchain = WindowsToolchain(tmp_path / "toolchain")

    assert toolchain.revision == "windows-local-build-v1"
    assert len(toolchain.manifest_digest) == 64
    assert {asset.name for asset in toolchain.assets} == {"mingit", "uv"}
    assert {asset.version for asset in toolchain.assets} == {
        "2.55.0.windows.4",
        "0.11.7",
    }
    assert all(len(asset.sha256) == 64 for asset in toolchain.assets)
    assert all(asset.url.startswith("https://github.com/") for asset in toolchain.assets)


def test_windows_toolchain_download_retries_transient_disconnects_without_weakening_hash_pin(
    tmp_path, monkeypatch
):
    payload = b"checksum-pinned-toolchain"
    asset = ToolchainAsset(
        name="uv",
        version="1",
        archive="uv.zip",
        url="https://github.com/example/toolchain/uv.zip",
        sha256=hashlib.sha256(payload).hexdigest(),
        executable="uv.exe",
        version_argument="--version",
        version_contains="uv 1",
    )
    attempts = 0
    waits: list[int] = []

    def urlopen(_request, timeout):
        nonlocal attempts
        assert timeout == 120
        attempts += 1
        if attempts < 3:
            raise ConnectionError("remote closed early")
        return io.BytesIO(payload)

    monkeypatch.setattr("gamgui.core.windows_update.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("gamgui.core.windows_update.time.sleep", waits.append)
    destination = tmp_path / asset.archive

    WindowsToolchain._download(asset, destination)

    assert attempts == 3
    assert waits == [2, 4]
    assert destination.read_bytes() == payload
    assert not destination.with_suffix(".zip.download").exists()


def test_windows_builder_rejects_moving_update_ready_before_checkout(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv,
            0,
            f"{'b' * 40}\trefs/heads/district-main\n{'b' * 40}\trefs/heads/update-ready\n",
            "",
        )

    monkeypatch.setattr("gamgui.core.windows_update.sys.platform", "win32")
    builder = WindowsLocalUpdateBuilder(
        root=tmp_path / "updates",
        signer_thumbprint="c" * 64,
        run=run,
    )
    builder.toolchain.ensure = lambda: {
        "mingit": tmp_path / "git.exe",
        "uv": tmp_path / "uv.exe",
    }

    with pytest.raises(RuntimeError, match="no longer matches"):
        builder.prepare(UpdateCandidate(SHA, "url", ("update-ready",)), "core")

    assert len(calls) == 1
    assert calls[0][1] == "ls-remote"


def test_windows_signing_script_requires_nonexportable_rsa_and_detached_manifest():
    script = Path("scripts/windows_local_signing.ps1").read_text(encoding="utf-8")

    assert "-KeyLength 3072" in script
    assert "-KeyExportPolicy NonExportable" in script
    assert ".AddYears(10)" in script
    assert '@("Root", "TrustedPublisher")' in script
    assert "StoreLocation]::CurrentUser" in script
    assert "X509Store" in script and "OpenFlags]::ReadWrite" in script
    assert "FindBySubjectDistinguishedName" in script
    assert 'Get-ChildItem -LiteralPath "Cert:\\CurrentUser' not in script
    assert "Remove-LocalCertificates" in script
    assert "GamGui.RootTrustDialog" in script
    assert "GetDlgItem($process.MainWindowHandle, 6)" in script
    assert "protected current-user root-store operation exceeded 15 seconds" in script
    assert "FindByThumbprint" in script
    assert "Get-LocalCertificates $store $Certificate.Thumbprint" in script
    assert "Import-Certificate" not in script
    assert "if ($CiEphemeralCertificate)" in script
    assert "New-CiSigningCertificate" in script
    assert "CreateSelfSigned" in script and "AddDays(1)" in script
    assert "if ($TrustLocalCertificate) { Add-Trust $certificate }" in script
    assert "Get-EnhancedKeyUsageOids" in script
    assert "X509EnhancedKeyUsageExtension" in script
    assert "Test-SignatureStatus" in script
    assert '@("UnknownError", "NotTrusted")' in script
    assert "SignedCms" in script and "bundle-manifest.p7s" in script
    assert "Get-AuthenticodeSignature" in script
    assert 'Join-Path $Root.FullName "GamGUI.exe"' in script
    assert '@(".exe", ".dll", ".pyd", ".ps1")' not in script
    assert '"RemoveTrust"' in script


def _windows_bundle(path: Path, content: bytes, *, source_sha: str = SHA) -> Path:
    path.mkdir(parents=True)
    (path / "GamGUI.exe").write_bytes(content)
    metadata = path / "_internal" / "resources" / "components" / "profile.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            build_profile_payload(
                CORE_PROFILE,
                source_sha=source_sha,
                version="1",
                architecture="x86_64",
                minimum_macos_version="10.0",
                packaging_revision="2-windows-local",
                platform_name="windows",
                bundle_format="onedir",
                signer_thumbprint="c" * 64,
                toolchain_manifest_digest="d" * 64,
            )
        ),
        encoding="utf-8",
    )
    write_artifact_sidecar(
        path,
        signing_channel="local",
        signing_authority="GamGUI Local",
    )
    return path


def test_windows_builder_stages_the_signed_helper_with_the_candidate(tmp_path):
    bundle = _windows_bundle(tmp_path / "built" / "GamGUI", b"candidate")
    helper = tmp_path / "built" / "GamGUIUpdater.exe"
    helper.write_bytes(b"signed-helper")
    envelope = verify_bundle_artifact(bundle)
    builder = WindowsLocalUpdateBuilder(
        root=tmp_path / "updates",
        signer_thumbprint="c" * 64,
    )

    pending = builder._stage(bundle, helper, envelope)

    assert (pending.parent / "GamGUIUpdater.exe").read_bytes() == b"signed-helper"
    assert verify_bundle_artifact(pending).artifact == envelope.artifact


class _Process:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


def test_windows_installer_uses_shared_journal_and_exact_health_marker(tmp_path, monkeypatch):
    monkeypatch.setattr("gamgui.core.updater.runtime_platform", lambda: "windows")
    monkeypatch.setattr("gamgui.core.updater.sys.platform", "win32")
    data_root = tmp_path / "data"
    update_root = data_root / "updates"
    install_root = tmp_path / "Programs" / "GamGUI"
    current = _windows_bundle(install_root / "current", b"old")
    pending = _windows_bundle(update_root / "pending" / SHA / "core" / "GamGUI", b"new")
    candidate = verify_bundle_artifact(pending).artifact
    installed = verify_bundle_artifact(current).artifact
    store = UpdateStateStore(update_root / "state.json")
    store.save(
        UpdateState(
            installed_sha=SHA,
            candidate_sha=SHA,
            pending_app=str(pending),
            installed_profile=CORE_PROFILE,
            desired_profile=CORE_PROFILE,
            installed_artifact=installed,
            candidate_artifact=candidate,
            activation_kind=ACTIVATION_APP_UPDATE,
            required_check_evidence=["update-ready"],
            installed_signing_channel="local",
            candidate_signing_channel="local",
            installed_signing_authority="GamGUI Local",
            candidate_signing_authority="GamGUI Local",
            local_signer_thumbprint="c" * 64,
            windows_installation_root=str(install_root),
        )
    )
    verified: list[Path] = []
    monkeypatch.setattr(
        "gamgui.core.windows_update.verify_windows_bundle",
        lambda bundle, _thumbprint, **_kwargs: verified.append(Path(bundle)),
    )

    def popen(_argv, env):
        marker = env.get("GAMGUI_UPDATE_HEALTH_MARKER")
        if marker:
            Path(marker).parent.mkdir(parents=True, exist_ok=True)
            Path(marker).write_text(
                json.dumps(
                    {
                        "ok": True,
                        "transaction_id": env["GAMGUI_ACTIVATION_TRANSACTION_ID"],
                        "sha": env["GAMGUI_INSTALLED_SHA"],
                        "profile": candidate.profile,
                        "component_set_digest": candidate.component_set_digest,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
        return _Process()

    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    installer = LocalUpdateInstaller(
        store=store,
        root=update_root,
        data_root=data_root,
        run=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
        popen=popen,
        sleep=sleep,
        clock=lambda: now[0],
    )

    assert installer.install(SHA, pending, current, health_timeout=0.1)
    assert (current / "GamGUI.exe").read_bytes() == b"new"
    final = store.load()
    assert final.installed_platform == "windows"
    assert final.windows_installation_root == str(install_root)
    assert final.candidate_sha == "" and final.pending_bundle == ""
    assert verified


def test_windows_directory_exchange_retries_a_locked_current_directory(tmp_path, monkeypatch):
    left = tmp_path / "current"
    right = tmp_path / ".current.incoming"
    left.mkdir()
    right.mkdir()
    (left / "value").write_text("old", encoding="utf-8")
    (right / "value").write_text("new", encoding="utf-8")
    original = __import__("os").replace
    failures = 2

    def replace(source, destination):
        nonlocal failures
        if Path(source) == left and failures:
            failures -= 1
            raise PermissionError("locked")
        return original(source, destination)

    monkeypatch.setattr("gamgui.core.updater.os.replace", replace)
    _windows_directory_exchange(left, right, sleep=lambda _seconds: None)

    assert (left / "value").read_text(encoding="utf-8") == "new"
    assert (right / "value").read_text(encoding="utf-8") == "old"


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows mutex contract")
def test_windows_named_mutex_excludes_a_second_updater(tmp_path):
    name = windows_mutex_name(tmp_path)
    first = WindowsNamedMutex.acquire(name)
    assert first is not None
    try:
        assert WindowsNamedMutex.acquire(name) is None
    finally:
        first.close()
    second = WindowsNamedMutex.acquire(name)
    assert second is not None
    second.close()
