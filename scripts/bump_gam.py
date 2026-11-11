#!/usr/bin/env python3
"""Refresh the pinned GAM release and every versioned contract in one reviewable change."""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import urllib.request
from pathlib import Path

import requests

if __package__:
    from .gam_assets import mac_assets
else:
    from gam_assets import mac_assets

ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")



def _replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"Expected one version marker in {path.name}; found {count}.")
    path.write_text(updated, encoding="utf-8")


def update_versioned_sources(root: Path, version: str) -> None:
    """Update the source pin, mock contract, and human runbook after a verified download."""
    _replace_once(
        root / "gamgui" / "core" / "gam" / "commands.py",
        r'^EXPECTED_GAM_VERSION = "[^"]+"$',
        f'EXPECTED_GAM_VERSION = "{version}"',
    )
    _replace_once(
        root / "scripts" / "fetch_gam.sh",
        r'^TAG="v[^"]+"$',
        f'TAG="v{version}"',
    )
    _replace_once(
        root / "scripts" / "fetch_gam_windows.ps1",
        r'^(\s*\[string\]\$Tag = )"v[^"]+"$',
        rf'\g<1>"v{version}"',
    )
    _replace_once(
        root / "tests" / "fixtures" / "mock_gam.sh",
        r'echo "GAM [0-9.]+ - mock"',
        f'echo "GAM {version} - mock"',
    )
    readme = root / "README.md"
    _replace_once(
        readme,
        r"(fetches the pinned version \(`v)[0-9.]+(`\))",
        rf"\g<1>{version}\g<2>",
    )
    _replace_once(
        readme,
        r"(The tested pin is currently \*\*GAM )[0-9.]+(\*\*)",
        rf"\g<1>{version}\g<2>",
    )


def record_checksum(root: Path) -> None:
    generated = root / "gamgui" / "resources" / "gam7" / "SHA256"
    line = generated.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{64}\s+\S+", line):
        raise RuntimeError("The downloaded GAM checksum record is invalid.")
    checksums = root / "scripts" / "gam_checksums.txt"
    text = checksums.read_text(encoding="utf-8")
    asset = line.split(maxsplit=1)[1]
    kept = [
        existing
        for existing in text.splitlines()
        if existing.startswith("#") or not existing.strip() or existing.split(maxsplit=1)[-1] != asset
    ]
    checksums.write_text("\n".join(kept).rstrip() + f"\n{line}\n", encoding="utf-8")


def release_checksums(tag: str, opener=urllib.request.urlopen) -> list[tuple[str, str]]:
    """Pin every Mac deployment target and Windows x86_64 from release metadata."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "GamGUI-GAM-Bump",
    }
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/GAM-team/GAM/releases/tags/{tag}",
        headers=headers,
    )
    # Python.org macOS interpreters do not always inherit the runner or system
    # keychain.  Use the CA bundle shipped with our direct requests dependency
    # so release metadata remains TLS-verified across supported Python builds.
    tls_context = ssl.create_default_context(cafile=requests.certs.where())
    with opener(request, timeout=30, context=tls_context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    selected: list[tuple[str, tuple[int, ...], str, str]] = []
    if not isinstance(payload, dict) or payload.get("tag_name", tag) != tag:
        raise RuntimeError("GAM release metadata does not match the requested tag.")
    payload["tag_name"] = tag
    windows_record: tuple[str, str] | None = None
    for asset in payload.get("assets", ()) if isinstance(payload, dict) else ():
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if name == f"gam-{tag.removeprefix('v')}-windows-x86_64.zip":
            digest = str(asset.get("digest") or "")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise RuntimeError(f"GitHub did not publish a SHA-256 digest for {name}.")
            windows_record = (digest.split(":", 1)[1], name)
            continue
    for asset, arch, target in mac_assets(payload):
        name = asset["name"]
        digest = str(asset.get("digest") or "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RuntimeError(f"GitHub did not publish a SHA-256 digest for {name}.")
        selected.append((arch, target, name, digest.split(":", 1)[1]))
    missing = {"arm64", "x86_64"} - {record[0] for record in selected}
    if missing:
        raise RuntimeError(
            "GAM release is missing supported macOS assets for: "
            + ", ".join(sorted(missing))
        )
    if windows_record is None:
        raise RuntimeError("GAM release is missing the supported Windows x86_64 asset.")
    return [(record[3], record[2]) for record in sorted(selected)] + [windows_record]


def record_release_checksums(
    root: Path, tag: str, records: list[tuple[str, str]]
) -> None:
    checksums = root / "scripts" / "gam_checksums.txt"
    version = tag.removeprefix("v")
    assets = {name for _digest, name in records}
    kept = [
        line
        for line in checksums.read_text(encoding="utf-8").splitlines()
        if not (
            line.strip()
            and not line.lstrip().startswith("#")
            and len(line.split()) >= 2
            and line.split()[1] in assets
        )
    ]
    additions = [f"{digest}  {name}" for digest, name in records]
    checksums.write_text(
        "\n".join(kept).rstrip() + "\n" + "\n".join(additions) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True, help="GAM release tag, for example v7.46.12")
    args = parser.parse_args()
    match = VERSION_RE.fullmatch(args.tag.strip())
    if not match:
        parser.error("--tag must be a semantic GAM release such as v7.46.12")
    version = match.group(1)
    tag = f"v{version}"

    # Pin all targets for both Mac architectures from GitHub's release metadata before the
    # downloader is allowed to execute either binary.  fetch_gam.sh deliberately
    # fails closed when the selected asset is not already present in the committed
    # checksum catalog.
    records = release_checksums(tag)
    record_release_checksums(ROOT, tag, records)
    subprocess.run([str(ROOT / "scripts" / "fetch_gam.sh"), "--tag", tag], cwd=ROOT, check=True)
    downloaded = (ROOT / "gamgui" / "resources" / "gam7" / "VERSION").read_text(
        encoding="utf-8"
    ).strip()
    if downloaded != tag:
        raise RuntimeError(f"Downloaded release {downloaded!r} does not match requested {tag!r}.")
    record_checksum(ROOT)
    update_versioned_sources(ROOT, version)
    subprocess.run(["python3", str(ROOT / "scripts" / "build_command_catalog.py")], cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
