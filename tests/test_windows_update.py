from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gamgui.core.updater import UpdateCandidate
from gamgui.core.windows_update import WindowsLocalUpdateBuilder, WindowsToolchain

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
    assert "TrustedPublisher" in script and "CurrentUser\\Root" in script
    assert "SignedCms" in script and "bundle-manifest.p7s" in script
    assert "Get-AuthenticodeSignature" in script
