"""Recognize upstream GAM macOS deployment targets and select a runnable asset."""
from __future__ import annotations

import json
import re
import sys

MAC_ASSET_RE = re.compile(
    r"^gam-(?P<version>[0-9.]+)-macos(?P<platform>[0-9]+(?:\.[0-9]+)*)-(?P<arch>arm64|x86_64)\.tar\.xz$"
)


def version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        raise ValueError(f"Invalid macOS version: {value!r}")
    parts = [int(part) for part in value.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def mac_assets(payload: dict):
    tag = str(payload.get("tag_name", "")).removeprefix("v")
    for asset in payload.get("assets", []):
        if not isinstance(asset, dict):
            continue
        match = MAC_ASSET_RE.fullmatch(str(asset.get("name", "")))
        if match and match.group("version") == tag:
            yield asset, match.group("arch"), version_tuple(match.group("platform"))


def select_mac_asset(payload: dict, arch: str, host_version: str) -> dict:
    host = version_tuple(host_version)
    candidates = [
        (target, asset["name"], asset)
        for asset, asset_arch, target in mac_assets(payload)
        if asset_arch == arch and target <= host
    ]
    if not candidates:
        raise ValueError(
            f"No compatible GAM macOS asset for {arch} on macOS {host_version}; "
            "refusing to select a newer deployment target."
        )
    return max(candidates, key=lambda item: item[:2])[2]


def effective_minimum_macos(asset_name: str, configured: str, host_version: str) -> str:
    match = MAC_ASSET_RE.fullmatch(asset_name)
    if not match:
        raise ValueError("Cannot determine bundled GAM deployment target")
    target = match.group("platform")
    minimum = max((configured, target), key=version_tuple)
    if version_tuple(minimum) > version_tuple(host_version):
        raise ValueError(f"Build requires macOS {minimum}, but host is {host_version}")
    return minimum


if __name__ == "__main__":
    try:
        with open(sys.argv[1], encoding="utf-8") as source:
            payload = json.load(source)
        asset = select_mac_asset(payload, sys.argv[2], sys.argv[3])
        print(payload["tag_name"], asset["name"], asset["browser_download_url"])
    except (ValueError, KeyError) as exc:
        sys.exit(str(exc))
