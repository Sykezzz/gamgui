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


def _write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _release_payload() -> dict[str, object]:
    return {
        "tag_name": "v2.3.4",
        "assets": [
            {
                "name": "gam-2.3.4-macos26.5-arm64.tar.xz",
                "digest": "sha256:" + "a" * 64,
            },
            {
                "name": "gam-2.3.4-macos26.6-x86_64.tar.xz",
                "digest": "sha256:" + "b" * 64,
            },
            {
                "name": "gam-2.3.4-windows-x86_64.zip",
                "digest": "sha256:" + "c" * 64,
            },
        ],
    }


def _opener_for(payload: dict[str, object], observed: dict[str, object] | None = None):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def opener(_request, *, timeout, context):
        if observed is not None:
            observed.update(timeout=timeout, context=context)
        return Response()

    return opener


def test_main_rejects_shell_metacharacters_before_any_release_call(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["bump_gam.py", "--tag", "v7.46.12$(touch should-not-exist)"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_release_checksums_returns_correct_multiplatform_records_with_verified_tls():
    observed: dict[str, object] = {}

    records = release_checksums(
        "v2.3.4",
        opener=_opener_for(_release_payload(), observed),
    )

    assert records == [
        ("a" * 64, "gam-2.3.4-macos26.5-arm64.tar.xz"),
        ("b" * 64, "gam-2.3.4-macos26.6-x86_64.tar.xz"),
        ("c" * 64, "gam-2.3.4-windows-x86_64.zip"),
    ]
    assert observed["timeout"] == 30
    context = observed["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_release_checksums_rejects_missing_windows_asset():
    payload = _release_payload()
    payload["assets"] = payload["assets"][:-1]

    with pytest.raises(RuntimeError, match="missing required Windows asset"):
        release_checksums("v2.3.4", opener=_opener_for(payload))


def test_release_checksums_rejects_duplicate_windows_asset():
    payload = _release_payload()
    payload["assets"].append(dict(payload["assets"][-1]))

    with pytest.raises(RuntimeError, match="2 copies of required Windows asset"):
        release_checksums("v2.3.4", opener=_opener_for(payload))


def test_release_checksums_rejects_missing_windows_digest():
    payload = _release_payload()
    payload["assets"][-1].pop("digest")

    with pytest.raises(RuntimeError, match="valid SHA-256 digest"):
        release_checksums("v2.3.4", opener=_opener_for(payload))


def test_release_checksums_rejects_malformed_digest():
    payload = _release_payload()
    payload["assets"][-1]["digest"] = "sha256:" + "A" * 64

    with pytest.raises(RuntimeError, match="valid SHA-256 digest"):
        release_checksums("v2.3.4", opener=_opener_for(payload))


def test_main_fails_before_source_markers_when_a_required_asset_is_missing(monkeypatch):
    source_update_called = False

    def missing_asset(_tag):
        raise RuntimeError("missing required Windows asset")

    def track_source_update(*_args):
        nonlocal source_update_called
        source_update_called = True

    monkeypatch.setattr("sys.argv", ["bump_gam.py", "--tag", "v2.3.4"])
    monkeypatch.setattr("scripts.bump_gam.release_checksums", missing_asset)
    monkeypatch.setattr("scripts.bump_gam.update_versioned_sources", track_source_update)

    with pytest.raises(RuntimeError, match="missing required Windows asset"):
        main()
    assert source_update_called is False


def test_update_versioned_sources_changes_every_contract(tmp_path):
    _write(tmp_path, "gamgui/core/gam/commands.py", 'EXPECTED_GAM_VERSION = "1.2.3"\n')
    _write(tmp_path, "scripts/fetch_gam.sh", 'TAG="v1.2.3"\n')
    _write(
        tmp_path,
        "scripts/fetch_gam_windows.ps1",
        '    [string]$Tag = "v1.2.3"\n',
    )
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
    windows_fetch = (tmp_path / "scripts/fetch_gam_windows.ps1").read_text()
    assert '[string]$Tag = "v2.3.4"' in windows_fetch
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


def test_record_release_checksums_replaces_all_current_platforms_idempotently(tmp_path):
    _write(
        tmp_path,
        "scripts/gam_checksums.txt",
        "# pins\n"
        + "a" * 64
        + "  gam-1.2.3-windows-x86_64.zip\n"
        + "d" * 64
        + "  gam-2.3.4-macos24-arm64.tar.xz\n"
        + "e" * 64
        + "  gam-2.3.4-windows-x86_64.zip\n",
    )
    records = [
        ("b" * 64, "gam-2.3.4-macos26.5-arm64.tar.xz"),
        ("c" * 64, "gam-2.3.4-macos26.6-x86_64.tar.xz"),
        ("f" * 64, "gam-2.3.4-windows-x86_64.zip"),
    ]

    record_release_checksums(tmp_path, "v2.3.4", records)
    first = (tmp_path / "scripts/gam_checksums.txt").read_text()
    record_release_checksums(tmp_path, "v2.3.4", records)
    second = (tmp_path / "scripts/gam_checksums.txt").read_text()

    assert first == second
    assert "gam-1.2.3-windows-x86_64.zip" in second
    assert "gam-2.3.4-macos24-arm64.tar.xz" not in second
    assert second.count("gam-2.3.4-macos26.5-arm64.tar.xz") == 1
    assert second.count("gam-2.3.4-macos26.6-x86_64.tar.xz") == 1
    assert second.count("gam-2.3.4-windows-x86_64.zip") == 1
