import json
import ssl
from pathlib import Path

import pytest

from scripts.bump_gam import (
    main,
    record_checksum,
    record_release_checksums,
    release_checksums,
    update_versioned_sources,
)


def test_main_rejects_shell_metacharacters_before_any_release_call(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["bump_gam.py", "--tag", "v7.46.12$(touch should-not-exist)"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_release_checksums_uses_a_verifying_tls_context():
    payload = {
        "assets": [
            {
                "name": "gam-7.47.02-macos26.4-arm64.tar.xz",
                "digest": "sha256:" + "a" * 64,
            },
            {
                "name": "gam-7.47.02-macos26.4-x86_64.tar.xz",
                "digest": "sha256:" + "b" * 64,
            },
            {
                "name": "gam-7.47.02-windows-x86_64.zip",
                "digest": "sha256:" + "c" * 64,
            },
        ]
    }
    observed: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def opener(_request, *, timeout, context):
        observed.update(timeout=timeout, context=context)
        return Response()

    records = release_checksums("v7.47.02", opener=opener)

    assert records == [
        ("a" * 64, "gam-7.47.02-macos26.4-arm64.tar.xz"),
        ("b" * 64, "gam-7.47.02-macos26.4-x86_64.tar.xz"),
        ("c" * 64, "gam-7.47.02-windows-x86_64.zip"),
    ]
    assert observed["timeout"] == 30
    context = observed["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def _write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_update_versioned_sources_changes_every_contract(tmp_path):
    _write(tmp_path, "gamgui/core/gam/commands.py", 'EXPECTED_GAM_VERSION = "1.2.3"\n')
    _write(tmp_path, "scripts/fetch_gam.sh", 'TAG="v1.2.3"\n')
    _write(tmp_path, "scripts/fetch_gam_windows.ps1", '    [string]$Tag = "v1.2.3"\n')
    _write(tmp_path, "tests/fixtures/mock_gam.sh", 'echo "GAM 1.2.3 - mock"\n')
    _write(
        tmp_path,
        "README.md",
        "fetches the pinned version (`v1.2.3`) from releases.\n"
        "The tested pin is currently **GAM 1.2.3**.\n",
    )

    update_versioned_sources(tmp_path, "2.3.4")

    assert '"2.3.4"' in (tmp_path / "gamgui/core/gam/commands.py").read_text()
    assert 'TAG="v2.3.4"' in (tmp_path / "scripts/fetch_gam.sh").read_text()
    assert '$Tag = "v2.3.4"' in (tmp_path / "scripts/fetch_gam_windows.ps1").read_text()
    assert "GAM 2.3.4 - mock" in (tmp_path / "tests/fixtures/mock_gam.sh").read_text()
    readme = (tmp_path / "README.md").read_text()
    assert "`v2.3.4`" in readme
    assert "**GAM 2.3.4**" in readme


def test_update_versioned_sources_fails_when_marker_drifts(tmp_path):
    _write(tmp_path, "gamgui/core/gam/commands.py", "missing\n")
    with pytest.raises(RuntimeError, match="Expected one version marker"):
        update_versioned_sources(tmp_path, "2.3.4")


def test_record_checksum_replaces_same_asset(tmp_path):
    _write(
        tmp_path,
        "scripts/gam_checksums.txt",
        "# pins\n" + "a" * 64 + "  gam-2.3.4-macos-arm64.tar.xz\n",
    )
    _write(
        tmp_path,
        "gamgui/resources/gam7/SHA256",
        "b" * 64 + "  gam-2.3.4-macos-arm64.tar.xz\n",
    )
    record_checksum(tmp_path)
    result = (tmp_path / "scripts/gam_checksums.txt").read_text()
    assert "a" * 64 not in result
    assert result.count("gam-2.3.4-macos-arm64.tar.xz") == 1


def test_record_release_checksums_pins_both_supported_architectures(tmp_path):
    _write(
        tmp_path,
        "scripts/gam_checksums.txt",
        "# pins\n" + "a" * 64 + "  gam-1.2.3-macos15-arm64.tar.xz\n",
    )
    record_release_checksums(
        tmp_path,
        "v2.3.4",
        [
            ("b" * 64, "gam-2.3.4-macos26.4-arm64.tar.xz"),
            ("c" * 64, "gam-2.3.4-macos26.4-x86_64.tar.xz"),
        ],
    )
    result = (tmp_path / "scripts/gam_checksums.txt").read_text()
    assert "gam-2.3.4-macos26.4-arm64.tar.xz" in result
    assert "gam-2.3.4-macos26.4-x86_64.tar.xz" in result

def test_release_checksums_requires_valid_windows_digest():
    assets = [
        {"name": "gam-2.3.4-macos26-arm64.tar.xz", "digest": "sha256:" + "a" * 64},
        {"name": "gam-2.3.4-macos26-x86_64.tar.xz", "digest": "sha256:" + "b" * 64},
    ]
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return json.dumps({"assets": assets}).encode()
    def opener(*_args, **_kwargs): return Response()
    with pytest.raises(RuntimeError, match="missing.*Windows"):
        release_checksums("v2.3.4", opener=opener)
    assets.append({"name": "gam-2.3.4-windows-x86_64.zip", "digest": "invalid"})
    with pytest.raises(RuntimeError, match="SHA-256 digest"):
        release_checksums("v2.3.4", opener=opener)


def test_record_release_checksums_replaces_windows_pin_preserving_other_releases(tmp_path):
    name = "gam-2.3.4-windows-x86_64.zip"
    old = "gam-1.2.3-windows-x86_64.zip"
    _write(tmp_path, "scripts/gam_checksums.txt", "a" * 64 + "  " + name + "\n" + "b" * 64 + "  " + old + "\n")
    record_release_checksums(tmp_path, "v2.3.4", [("c" * 64, name)])
    text = (tmp_path / "scripts/gam_checksums.txt").read_text()
    assert text.count(name) == 1
    assert "c" * 64 + "  " + name in text
    assert "b" * 64 + "  " + old in text
