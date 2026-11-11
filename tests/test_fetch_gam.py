"""scripts/fetch_gam.sh must fail CLOSED on an asset with no committed SHA-256 pin.

The asset name comes out of the release JSON the script just downloaded, so "this name isn't in
gam_checksums.txt" is precisely what renaming an asset produces — installing it anyway would make
the pin decorative. Everything here runs offline: the script is copied into a throwaway root and
driven with a stub `curl` on PATH, so no test ever touches api.github.com or the real
gamgui/resources/gam7.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "fetch_gam.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The script maps uname -m onto GAM's asset naming; anything else exits before the checksum gate.
_ARCH = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64"}.get(platform.machine())
ASSET = f"gam-9.99.99-macos26.4-{_ARCH}.tar.xz"

# `find -perm +111` (the executable-lookup after extraction) is BSD-only, and this is a macOS-only
# vendoring script — on Linux we only assert on the gate itself, not on a completed install.
_INSTALLS = sys.platform == "darwin"

pytestmark = pytest.mark.skipif(
    _ARCH is None or shutil.which("shasum") is None,
    reason="fetch_gam.sh needs a macOS-style arch and shasum",
)


# --- hermetic harness --------------------------------------------------------------------

def _fake_tarball(path: Path, content: bytes = b'#!/bin/sh\necho "GAM 9.99.99"\n') -> str:
    """A real .tar.xz holding an executable `gam`, so the install path can run to completion."""
    gam = io.BytesIO(content)
    with tarfile.open(path, "w:xz") as tf:
        info = tarfile.TarInfo("gam-9.99.99/gam")
        info.size = len(gam.getvalue())
        info.mode = 0o755
        tf.addfile(info, gam)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stub_curl(bin_dir: Path) -> None:
    """Serve both curl calls (release JSON, then the asset) from local files instead of the net."""
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "out=''; url=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -o) out=\"$2\"; shift 2 ;;\n"
        "    -H) shift 2 ;;\n"
        "    -*) shift ;;\n"
        "    *) url=\"$1\"; shift ;;\n"
        "  esac\n"
        "done\n"
        "case \"$url\" in\n"
        "  *api.github.com*) cp \"$STUB_RELEASE_JSON\" \"$out\" ;;\n"
        "  *) cp \"$STUB_ASSET\" \"$out\" ;;\n"
        "esac\n"
    )
    curl.chmod(0o755)


def _make_root(tmp_path: Path, pinned: str | None) -> tuple[Path, str]:
    """Build a fake repo root around a copy of the script. `pinned` is the hash to commit, if any."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "fetch_gam.sh")
    shutil.copy2(REPO_ROOT / "scripts" / "gam_assets.py", root / "scripts" / "gam_assets.py")

    sha = _fake_tarball(tmp_path / "asset.tar.xz")
    (tmp_path / "release.json").write_text(
        '{"tag_name": "v9.99.99", "assets": ['
        f'{{"name": "{ASSET}", "browser_download_url": "https://example.invalid/{ASSET}"}}]}}'
    )
    lines = ["# Pinned SHA-256 checksums.\n"]
    if pinned is not None:
        lines.append(f"{pinned}  {ASSET}\n")
    (root / "scripts" / "gam_checksums.txt").write_text("".join(lines))
    return root, sha


def _run(root: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _stub_curl(bin_dir)
    sw_vers = bin_dir / "sw_vers"
    sw_vers.write_text("#!/bin/sh\necho 26.7.2\n")
    sw_vers.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_RELEASE_JSON": str(tmp_path / "release.json"),
        "STUB_ASSET": str(tmp_path / "asset.tar.xz"),
    }
    return subprocess.run(
        [str(root / "scripts" / "fetch_gam.sh"), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _dest(root: Path) -> Path:
    return root / "gamgui" / "resources" / "gam7"


# --- the gate ----------------------------------------------------------------------------

def test_unpinned_asset_is_refused_by_default(tmp_path):
    root, _sha = _make_root(tmp_path, pinned=None)
    r = _run(root, tmp_path)

    assert r.returncode == 1, r.stdout + r.stderr
    assert "no pinned checksum" in r.stderr.lower() and "refusing to install" in r.stderr.lower()
    assert "automated bump must record every supported macOS architecture" in r.stderr
    assert not _dest(root).exists(), "refused install must not touch resources/gam7"


def test_unpinned_opt_in_is_not_available(tmp_path):
    root, _sha = _make_root(tmp_path, pinned=None)
    r = _run(root, tmp_path, "--allow-unpinned")

    assert r.returncode == 2
    assert "unknown arg: --allow-unpinned" in r.stderr
    assert not _dest(root).exists()


def test_matching_pin_installs_without_the_flag(tmp_path):
    root, sha = _make_root(tmp_path, pinned=None)
    (root / "scripts" / "gam_checksums.txt").write_text(f"{sha}  {ASSET}\n")
    r = _run(root, tmp_path)

    assert "Checksum verified" in r.stdout, r.stdout + r.stderr
    assert "WARNING" not in r.stderr
    if _INSTALLS:
        assert r.returncode == 0, r.stdout + r.stderr
        assert (_dest(root) / "VERSION").read_text().strip() == "v9.99.99"


def test_checksum_mismatch_always_refuses(tmp_path):
    root, _sha = _make_root(tmp_path, pinned="0" * 64)
    r = _run(root, tmp_path)

    assert r.returncode == 1, r.stdout + r.stderr
    assert "checksum mismatch" in r.stderr
    assert not _dest(root).exists()


def test_unknown_flag_still_rejected(tmp_path):
    root, _sha = _make_root(tmp_path, pinned=None)
    r = _run(root, tmp_path, "--allow-unpinned-please")
    assert r.returncode == 2 and "unknown arg" in r.stderr


# --- static checks (also meaningful where the script can't run) ---------------------------

def test_script_has_no_unpinned_install_escape_hatch():
    text = SCRIPT.read_text()
    assert "--allow-unpinned)" not in text
    assert "ALLOW_UNPINNED" not in text


def test_unpinned_branch_exits_nonzero():
    """The unpinned branch must end in exit 1, not fall through to the install."""
    text = SCRIPT.read_text()
    gate = text.split('if [ -n "$EXPECTED" ]', 1)[1].split("\nfi\n", 1)[0]
    refuse = gate.split("\nelse\n", 1)[1]
    assert "exit 1" in refuse


def test_ci_pins_latest_before_the_preview_download():
    text = CI_WORKFLOW.read_text()
    assert 'scripts/bump_gam.py --tag "$LATEST"' in text
    assert "--allow-unpinned" not in text
    # The pinned gam-compat job must keep failing closed.
    assert "run: ./scripts/fetch_gam.sh\n" in text


def test_pywebview_is_pinned():
    # Matched textually rather than with a TOML parser: tomllib is 3.11+, and the supported floor
    # is 3.10. pywebview hosts the WKWebView, so an unpinned floor would pull an unreviewed
    # version into the shipped .app.
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert re.search(r'^\s*"pywebview==[\d.]+"', text, re.MULTILINE), text

    # build_app.sh must consume the locked environment instead of installing a second version.
    build = (REPO_ROOT / "scripts" / "build_app.sh").read_text()
    assert "pywebview>=" not in build, build
    assert "pip install" not in build, build
    assert '"$PY" -c "import PyInstaller, webview"' in build, build


def _stub_bsd_find(tmp_path):
    # Let the complete macOS shell flow run hermetically on Linux too.
    if sys.platform == "darwin":
        return
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    find = bin_dir / "find"
    find.write_text(
        "#!/usr/bin/env bash\n"
        "args=(); for arg in \"$@\"; do \n"
        "  if [ \"$arg\" = '+111' ]; then args+=('/111'); else args+=(\"$arg\"); fi; done\n"
        "/usr/bin/find \"${args[@]}\"\n"
    )
    find.chmod(0o755)


def test_failed_runtime_probe_keeps_existing_payload(tmp_path):
    root, _sha = _make_root(tmp_path, pinned=None)
    sha = _fake_tarball(tmp_path / "asset.tar.xz", b'#!/bin/sh\necho "incompatible deployment target" >&2\nexit 9\n')
    (root / "scripts" / "gam_checksums.txt").write_text(f"{sha}  {ASSET}\n")
    _dest(root).mkdir(parents=True)
    (_dest(root) / "gam").write_text("previous working payload")
    _stub_bsd_find(tmp_path)
    result = _run(root, tmp_path)
    assert result.returncode != 0
    assert "runtime probe failed" in result.stderr
    assert "exit 9" in result.stderr
    assert "incompatible deployment target" in result.stderr
    assert (_dest(root) / "gam").read_text() == "previous working payload"
    assert not list(_dest(root).parent.glob(".gam7-stage.*"))


def test_runtime_version_mismatch_is_not_installed(tmp_path):
    root, _sha = _make_root(tmp_path, pinned=None)
    sha = _fake_tarball(tmp_path / "asset.tar.xz", b'#!/bin/sh\necho "GAM 1.0.0"\n')
    (root / "scripts" / "gam_checksums.txt").write_text(f"{sha}  {ASSET}\n")
    _stub_bsd_find(tmp_path)
    result = _run(root, tmp_path)
    assert result.returncode != 0
    assert "runtime probe failed" in result.stderr
    assert not _dest(root).exists()


def test_successful_runtime_probe_installs_payload(tmp_path):
    root, sha = _make_root(tmp_path, pinned=None)
    (root / "scripts" / "gam_checksums.txt").write_text(f"{sha}  {ASSET}\n")
    _stub_bsd_find(tmp_path)
    result = _run(root, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (_dest(root) / "VERSION").read_text().strip() == "v9.99.99"


@pytest.mark.parametrize("output", ["GAM 19.99.99", "GAM 9.99.990", "GAM 9.99.99beta", "Expected GAM 9.99.99"])
def test_runtime_probe_requires_exact_parsed_version(tmp_path, output):
    root, _sha = _make_root(tmp_path, pinned=None)
    sha = _fake_tarball(tmp_path / "asset.tar.xz", f'#!/bin/sh\necho "{output}"\n'.encode())
    (root / "scripts" / "gam_checksums.txt").write_text(f"{sha}  {ASSET}\n")
    _stub_bsd_find(tmp_path)
    result = _run(root, tmp_path)
    assert result.returncode != 0
    assert "runtime probe failed" in result.stderr
    assert not _dest(root).exists()
