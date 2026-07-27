"""Fail-closed contracts for official GamGUI release artifacts.

The official release workflow records evidence only after macOS has verified the
Developer ID signature, hardened runtime, notarization ticket, Gatekeeper
assessment, and offline bundle self-test.  This module then binds that evidence to
the embedded component profile and to the exact post-staple bundle and archive
hashes.

It deliberately does not download artifacts, invoke Apple tooling, or publish a
release.  Those operations stay in the auditable shell/workflow boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Sequence

from .components import (
    ArtifactEnvelope,
    CORE_PROFILE,
    ONEROSTER_PROFILE,
    SUPPORTED_PROFILES,
    verify_bundle_artifact,
)

RELEASE_MANIFEST_SCHEMA = 1
RELEASE_EVIDENCE_SCHEMA = 1
OFFICIAL_BUNDLE_ID = "io.github.goetchstone.gamgui"
OFFICIAL_SIGNING_CHANNEL = "developer-id"
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")
_RELEASE_TAG = re.compile(
    r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)$"
)
MAX_RELEASE_ARCHIVE_ENTRIES = 50_000
MAX_RELEASE_ARCHIVE_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024


class ReleaseManifestError(RuntimeError):
    """A release input or verification failure safe to show in CI."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preflight_release_archive(path: Path) -> None:
    """Reject unsafe ZIP structure before any platform extractor sees the archive."""

    archive_path = _require_file(path, label="Release archive")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_RELEASE_ARCHIVE_ENTRIES:
                raise ReleaseManifestError(
                    "Release archive has an invalid entry count."
                )
            expanded = 0
            seen: set[str] = set()
            archive_paths: list[PurePosixPath] = []
            symlink_paths: set[str] = set()
            for info in entries:
                name = info.filename
                if (
                    not name
                    or name.startswith(("/", "\\"))
                    or "\\" in name
                    or "\x00" in name
                ):
                    raise ReleaseManifestError(
                        "Release archive contains an unsafe path."
                    )
                pure = PurePosixPath(name.rstrip("/"))
                if not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
                    raise ReleaseManifestError(
                        "Release archive contains an unsafe path."
                    )
                collision_key = unicodedata.normalize(
                    "NFC",
                    pure.as_posix(),
                ).casefold()
                if collision_key in seen:
                    raise ReleaseManifestError(
                        "Release archive contains a duplicate path."
                    )
                seen.add(collision_key)
                archive_paths.append(pure)
                if info.flag_bits & 0x1:
                    raise ReleaseManifestError(
                        "Release archive contains an encrypted entry."
                    )

                if pure.parts[0] != "GamGUI.app":
                    raise ReleaseManifestError(
                        "Release archive contains content outside GamGUI.app."
                    )

                mode = (info.external_attr >> 16) & 0o170000
                if mode not in {0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}:
                    raise ReleaseManifestError(
                        "Release archive contains a special filesystem entry."
                    )
                if mode == stat.S_IFLNK:
                    if pure.parts[0] != "GamGUI.app" or info.file_size > 4096:
                        raise ReleaseManifestError(
                            "Release archive contains an unsafe symbolic link."
                        )
                    try:
                        target = archive.read(info).decode("utf-8")
                    except (KeyError, OSError, UnicodeError) as exc:
                        raise ReleaseManifestError(
                            "Release archive contains an invalid symbolic link."
                        ) from exc
                    if not target or target.startswith(("/", "\\")) or "\\" in target:
                        raise ReleaseManifestError(
                            "Release archive contains an unsafe symbolic link."
                        )
                    resolved_parts: list[str] = list(pure.parent.parts)
                    for part in PurePosixPath(target).parts:
                        if part in {"", "."}:
                            continue
                        if part == "..":
                            if len(resolved_parts) <= 1:
                                raise ReleaseManifestError(
                                    "Release archive contains an escaping symbolic link."
                                )
                            resolved_parts.pop()
                        else:
                            resolved_parts.append(part)
                    if not resolved_parts or resolved_parts[0] != "GamGUI.app":
                        raise ReleaseManifestError(
                            "Release archive contains an escaping symbolic link."
                        )
                    symlink_paths.add(collision_key)

                expanded += info.file_size
                if expanded > MAX_RELEASE_ARCHIVE_EXPANDED_BYTES:
                    raise ReleaseManifestError(
                        "Release archive expands beyond the approved size limit."
                    )
            for pure in archive_paths:
                if any(
                    unicodedata.normalize("NFC", parent.as_posix()).casefold()
                    in symlink_paths
                    for parent in pure.parents
                ):
                    raise ReleaseManifestError(
                        "Release archive nests content beneath a symbolic link."
                    )
    except zipfile.BadZipFile as exc:
        raise ReleaseManifestError("Release archive is not a valid ZIP file.") from exc


def extract_release_archive(path: Path, destination: Path) -> Path:
    """Extract an already verified archive without creating links until the end."""

    archive_path = _require_file(path, label="Release archive")
    root = Path(destination)
    if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
        raise ReleaseManifestError(
            "Release extraction destination must be an empty real directory."
        )
    preflight_release_archive(archive_path)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = [
                info
                for info in archive.infolist()
                if PurePosixPath(info.filename.rstrip("/")).parts[0] == "GamGUI.app"
            ]
            directory_entries = [
                info
                for info in entries
                if info.is_dir()
                or ((info.external_attr >> 16) & 0o170000) == stat.S_IFDIR
            ]
            regular_entries = [
                info
                for info in entries
                if not info.is_dir()
                and ((info.external_attr >> 16) & 0o170000) != stat.S_IFDIR
                and ((info.external_attr >> 16) & 0o170000) != stat.S_IFLNK
            ]
            link_entries = [
                info
                for info in entries
                if ((info.external_attr >> 16) & 0o170000) == stat.S_IFLNK
            ]
            written = 0
            root_resolved = root.resolve()
            for info in (*directory_entries, *regular_entries, *link_entries):
                pure = PurePosixPath(info.filename.rstrip("/"))
                target = root.joinpath(*pure.parts)
                try:
                    target.resolve(strict=False).relative_to(root_resolved)
                except ValueError as exc:
                    raise ReleaseManifestError(
                        "Release archive extraction path escaped its destination."
                    ) from exc
                mode = (info.external_attr >> 16) & 0o170000
                if info.is_dir() or mode == stat.S_IFDIR:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    raise ReleaseManifestError(
                        "Release archive contains a conflicting extraction path."
                    )
                if mode == stat.S_IFLNK:
                    os.symlink(archive.read(info).decode("utf-8"), target)
                    continue
                with archive.open(info) as source, target.open("xb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_RELEASE_ARCHIVE_EXPANDED_BYTES:
                            raise ReleaseManifestError(
                                "Release archive exceeded its expanded size limit."
                            )
                        output.write(chunk)
                permissions = (info.external_attr >> 16) & 0o777
                if permissions and os.name != "nt":
                    target.chmod(permissions)
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ReleaseManifestError("Release archive extraction failed.") from exc

    bundle = root / "GamGUI.app"
    if not bundle.is_dir() or bundle.is_symlink():
        raise ReleaseManifestError(
            "Release archive did not contain one valid GamGUI.app bundle."
        )
    return bundle


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ReleaseManifestError(f"Invalid JSON evidence file: {Path(path).name}") from exc
    if not isinstance(value, Mapping):
        raise ReleaseManifestError(f"JSON evidence must be an object: {Path(path).name}")
    return value


def _safe_filename(value: object, *, label: str) -> str:
    filename = value if isinstance(value, str) else ""
    if not _SAFE_FILENAME.fullmatch(filename) or Path(filename).name != filename:
        raise ReleaseManifestError(f"{label} has an unsafe filename.")
    return filename


def _required_text(
    value: Mapping[str, Any],
    key: str,
    *,
    label: str,
    limit: int = 512,
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or len(item) > limit:
        raise ReleaseManifestError(f"{label} is missing {key}.")
    return item


def _require_file(path: Path, *, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        raise ReleaseManifestError(f"{label} is missing or is not a regular file.")
    return candidate


def _require_bundle(path: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_dir() or candidate.is_symlink():
        raise ReleaseManifestError("Application bundle is missing or invalid.")
    return candidate


def _bundle_metadata(bundle: Path) -> tuple[str, str]:
    info_plist = bundle / "Contents" / "Info.plist"
    try:
        with info_plist.open("rb") as handle:
            value = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError) as exc:
        raise ReleaseManifestError("Application bundle has no valid Info.plist.") from exc
    bundle_id = value.get("CFBundleIdentifier") if isinstance(value, Mapping) else None
    if bundle_id != OFFICIAL_BUNDLE_ID:
        raise ReleaseManifestError("Application bundle identifier is not approved.")
    minimum_macos = (
        value.get("LSMinimumSystemVersion") if isinstance(value, Mapping) else None
    )
    if not isinstance(minimum_macos, str) or not minimum_macos:
        raise ReleaseManifestError("Application bundle minimum macOS version is missing.")
    return bundle_id, minimum_macos


def _accepted_notary_result(path: Path) -> tuple[str, str]:
    result = _read_json(_require_file(path, label="Notarization result"))
    status = result.get("status")
    submission_id = result.get("id")
    if status != "Accepted" or not isinstance(submission_id, str) or not submission_id:
        raise ReleaseManifestError("Apple notarization did not return Accepted evidence.")
    return status, submission_id[:128]


def _accepted_self_test(path: Path) -> None:
    result = _read_json(_require_file(path, label="Self-test result"))
    failures = result.get("failures")
    if result.get("ok") is not True or failures not in ([], None):
        raise ReleaseManifestError("Offline bundle self-test did not pass.")


def _validate_team_id(team_id: str) -> str:
    value = str(team_id or "").strip()
    if not _TEAM_ID.fullmatch(value):
        raise ReleaseManifestError(
            "Expected Apple Team ID must contain exactly 10 uppercase letters or digits."
        )
    return value


def _validate_authority(authority: str) -> str:
    if (
        not authority.startswith("Developer ID Application: ")
        or len(authority) > 256
        or "\n" in authority
        or "\r" in authority
    ):
        raise ReleaseManifestError("Signing authority is not a Developer ID Application identity.")
    match = re.search(r"\(([A-Z0-9]{10})\)$", authority)
    if match is None:
        raise ReleaseManifestError(
            "Signing authority does not end with an Apple Team ID."
        )
    return match.group(1)


def create_release_evidence(
    *,
    profile: str,
    bundle: Path,
    archive: Path,
    sidecar: Path,
    notary_result: Path,
    self_test_result: Path,
    signing_authority: str,
) -> dict[str, Any]:
    """Create evidence after the macOS verification commands have succeeded."""

    if profile not in SUPPORTED_PROFILES:
        raise ReleaseManifestError("Release evidence requested an unsupported profile.")
    _validate_authority(signing_authority)
    bundle = _require_bundle(bundle).resolve()
    archive = _require_file(archive, label="Release archive").resolve()
    sidecar = _require_file(sidecar, label="Artifact identity").resolve()
    notary_result = _require_file(
        notary_result,
        label="Notarization result",
    ).resolve()
    self_test_result = _require_file(
        self_test_result,
        label="Self-test result",
    ).resolve()

    envelope = verify_bundle_artifact(
        bundle,
        expected_profile=profile,
        sidecar=sidecar,
    )
    if envelope.signing_channel != OFFICIAL_SIGNING_CHANNEL:
        raise ReleaseManifestError("Artifact identity is not in the official signing channel.")
    if envelope.signing_authority != signing_authority:
        raise ReleaseManifestError("Artifact identity does not match the verified signing authority.")
    bundle_id, minimum_macos = _bundle_metadata(bundle)
    if minimum_macos != envelope.artifact.minimum_macos_version:
        raise ReleaseManifestError("Bundle and artifact minimum macOS versions disagree.")
    status, submission_id = _accepted_notary_result(notary_result)
    _accepted_self_test(self_test_result)

    return {
        "schema_version": RELEASE_EVIDENCE_SCHEMA,
        "profile": profile,
        "bundle_path": str(bundle),
        "archive_path": str(archive),
        "sidecar_path": str(sidecar),
        "notary_result_path": str(notary_result),
        "self_test_result_path": str(self_test_result),
        "bundle_id": bundle_id,
        "signing_authority": signing_authority,
        "artifact": envelope.artifact.to_json(),
        "checks": {
            "strict_signature": True,
            "hardened_runtime": True,
            "secure_timestamp": True,
            "notarization": status,
            "notary_submission_id": submission_id,
            "staple_validated": True,
            "gatekeeper_accepted": True,
            "offline_self_test": True,
        },
    }


def _artifact_from_evidence(
    evidence_path: Path,
    *,
    expected_source_sha: str,
) -> dict[str, Any]:
    evidence = _read_json(evidence_path)
    if evidence.get("schema_version") != RELEASE_EVIDENCE_SCHEMA:
        raise ReleaseManifestError("Release evidence schema is unsupported.")
    profile = evidence.get("profile")
    if profile not in SUPPORTED_PROFILES:
        raise ReleaseManifestError("Release evidence contains an unsupported profile.")
    authority = _required_text(
        evidence,
        "signing_authority",
        label="Release evidence",
        limit=256,
    )
    _validate_authority(authority)

    checks = evidence.get("checks")
    required_checks = {
        "strict_signature": True,
        "hardened_runtime": True,
        "secure_timestamp": True,
        "notarization": "Accepted",
        "staple_validated": True,
        "gatekeeper_accepted": True,
        "offline_self_test": True,
    }
    if not isinstance(checks, Mapping) or any(
        checks.get(key) != expected for key, expected in required_checks.items()
    ):
        raise ReleaseManifestError("Release evidence is missing a required macOS verification.")
    submission_id = checks.get("notary_submission_id")
    if not isinstance(submission_id, str) or not submission_id:
        raise ReleaseManifestError("Release evidence has no notarization submission ID.")

    bundle = _require_bundle(
        Path(_required_text(evidence, "bundle_path", label="Release evidence"))
    )
    archive = _require_file(
        Path(_required_text(evidence, "archive_path", label="Release evidence")),
        label="Release archive",
    )
    sidecar = _require_file(
        Path(_required_text(evidence, "sidecar_path", label="Release evidence")),
        label="Artifact identity",
    )
    notary_result = Path(
        _required_text(evidence, "notary_result_path", label="Release evidence")
    )
    self_test_result = Path(
        _required_text(evidence, "self_test_result_path", label="Release evidence")
    )
    status, live_submission_id = _accepted_notary_result(notary_result)
    _accepted_self_test(self_test_result)
    if submission_id != live_submission_id:
        raise ReleaseManifestError("Notarization evidence changed after it was recorded.")

    envelope = verify_bundle_artifact(
        bundle,
        expected_profile=profile,
        sidecar=sidecar,
    )
    if envelope.signing_channel != OFFICIAL_SIGNING_CHANNEL:
        raise ReleaseManifestError("Release artifact is not signed for the official channel.")
    if envelope.signing_authority != authority:
        raise ReleaseManifestError("Release artifact signing authority changed.")
    if envelope.artifact.source_sha != expected_source_sha:
        raise ReleaseManifestError("Release artifact was built from a different source SHA.")
    if evidence.get("artifact") != envelope.artifact.to_json():
        raise ReleaseManifestError("Embedded artifact identity changed after verification.")
    live_bundle_id, minimum_macos = _bundle_metadata(bundle)
    if evidence.get("bundle_id") != live_bundle_id:
        raise ReleaseManifestError("Bundle identifier evidence changed.")
    if minimum_macos != envelope.artifact.minimum_macos_version:
        raise ReleaseManifestError("Bundle and artifact minimum macOS versions disagree.")

    archive_filename = _safe_filename(archive.name, label="Release archive")
    sidecar_filename = _safe_filename(sidecar.name, label="Artifact identity")
    return {
        "profile": profile,
        "filename": archive_filename,
        "sha256": sha256_file(archive),
        "size_bytes": archive.stat().st_size,
        "identity_filename": sidecar_filename,
        "identity_sha256": sha256_file(sidecar),
        "identity_size_bytes": sidecar.stat().st_size,
        "artifact": envelope.artifact.to_json(),
        "bundle_id": OFFICIAL_BUNDLE_ID,
        "signing_channel": OFFICIAL_SIGNING_CHANNEL,
        "signing_authority": authority,
        "notarization": {
            "status": status,
            "submission_id": submission_id[:128],
            "stapled": True,
            "gatekeeper_accepted": True,
        },
        "offline_self_test": {"ok": True},
    }


def build_release_manifest(
    evidence_paths: Sequence[Path],
    *,
    source_sha: str,
    release_tag: str,
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    """Build the two-profile manifest only when every identity and check agrees."""

    source_sha = source_sha.strip().lower()
    if not _GIT_SHA.fullmatch(source_sha):
        raise ReleaseManifestError("Release source SHA must be an exact Git commit.")
    match = _RELEASE_TAG.fullmatch(release_tag.strip())
    if not match:
        raise ReleaseManifestError("Official release tag must be a version tag such as v1.2.3.")
    if len(evidence_paths) != len(SUPPORTED_PROFILES):
        raise ReleaseManifestError("Official releases require exactly two profile artifacts.")

    artifacts = [
        _artifact_from_evidence(Path(path), expected_source_sha=source_sha)
        for path in evidence_paths
    ]
    by_profile = {item["profile"]: item for item in artifacts}
    if len(by_profile) != len(artifacts) or set(by_profile) != set(SUPPORTED_PROFILES):
        raise ReleaseManifestError("Official releases require Core and Core + OneRoster.")

    ordered = [by_profile[CORE_PROFILE], by_profile[ONEROSTER_PROFILE]]
    authorities = {item["signing_authority"] for item in ordered}
    team_ids = {_validate_authority(item["signing_authority"]) for item in ordered}
    if len(authorities) != 1 or len(team_ids) != 1:
        raise ReleaseManifestError(
            "Core and Core + OneRoster must share exactly one signing authority and Team ID."
        )
    signing_authority = authorities.pop()
    team_id = team_ids.pop()
    common_keys = (
        "version",
        "source_sha",
        "architecture",
        "minimum_macos_version",
        "packaging_revision",
    )
    common: dict[str, str] = {}
    for key in common_keys:
        values = {item["artifact"].get(key) for item in ordered}
        if len(values) != 1 or not all(isinstance(value, str) and value for value in values):
            raise ReleaseManifestError(f"Profile artifacts disagree on {key}.")
        common[key] = values.pop()
    if common["source_sha"] != source_sha:
        raise ReleaseManifestError("Release manifest source SHA does not match its artifacts.")
    if match.group("version") != common["version"]:
        raise ReleaseManifestError("Release tag does not match the application version.")

    filenames = [
        item[key]
        for item in ordered
        for key in ("filename", "identity_filename")
    ]
    if len(set(filenames)) != len(filenames):
        raise ReleaseManifestError("Release asset filenames must be unique.")

    timestamp = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(timestamp, str) or not timestamp:
        raise ReleaseManifestError("Release generation timestamp is missing.")
    return {
        "schema_version": RELEASE_MANIFEST_SCHEMA,
        "release_tag": release_tag,
        "source_sha": source_sha,
        "application_version": common["version"],
        "bundle_id": OFFICIAL_BUNDLE_ID,
        "architecture": common["architecture"],
        "minimum_macos_version": common["minimum_macos_version"],
        "packaging_revision": common["packaging_revision"],
        "signing_channel": OFFICIAL_SIGNING_CHANNEL,
        "signing_authority": signing_authority,
        "team_id": team_id,
        "generated_at": timestamp,
        "profiles": list(SUPPORTED_PROFILES),
        "artifacts": ordered,
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def write_manifest_and_checksum(
    manifest: Mapping[str, Any],
    *,
    output: Path,
    checksum_output: Path,
) -> None:
    output = Path(output)
    checksum_output = Path(checksum_output)
    _atomic_json(output, manifest)
    digest = sha256_file(output)
    checksum_output.write_text(f"{digest}  {output.name}\n", encoding="ascii")


def _verified_manifest_header(
    *,
    manifest_path: Path,
    checksum_path: Path,
    expected_source_sha: Optional[str] = None,
    expected_team_id: Optional[str] = None,
) -> Mapping[str, Any]:
    """Verify the release manifest checksum and its fixed compatibility contract."""

    manifest_path = _require_file(manifest_path, label="Release manifest")
    checksum_path = _require_file(checksum_path, label="Manifest checksum")
    checksum_line = checksum_path.read_text(encoding="ascii").strip()
    expected_line = f"{sha256_file(manifest_path)}  {manifest_path.name}"
    if checksum_line != expected_line:
        raise ReleaseManifestError("Release manifest checksum did not match.")

    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != RELEASE_MANIFEST_SCHEMA:
        raise ReleaseManifestError("Release manifest schema is unsupported.")
    source_sha = manifest.get("source_sha")
    if not isinstance(source_sha, str) or not _GIT_SHA.fullmatch(source_sha):
        raise ReleaseManifestError("Release manifest source SHA is invalid.")
    if expected_source_sha and source_sha != expected_source_sha.lower():
        raise ReleaseManifestError("Release manifest source SHA is not the requested commit.")
    if manifest.get("bundle_id") != OFFICIAL_BUNDLE_ID:
        raise ReleaseManifestError("Release manifest bundle identifier is not approved.")
    if manifest.get("signing_channel") != OFFICIAL_SIGNING_CHANNEL:
        raise ReleaseManifestError("Release manifest is not for the official signing channel.")
    signing_authority = _required_text(
        manifest,
        "signing_authority",
        label="Release manifest",
        limit=256,
    )
    authority_team_id = _validate_authority(signing_authority)
    manifest_team_id = manifest.get("team_id")
    if manifest_team_id != authority_team_id:
        raise ReleaseManifestError(
            "Release manifest signing authority and Team ID disagree."
        )
    if expected_team_id is not None:
        trusted_team_id = _validate_team_id(expected_team_id)
        if manifest_team_id != trusted_team_id:
            raise ReleaseManifestError(
                "Release manifest was not signed by the expected Apple Team ID."
            )
    if manifest.get("profiles") != list(SUPPORTED_PROFILES):
        raise ReleaseManifestError("Release manifest profile set is incomplete.")
    release_tag = manifest.get("release_tag")
    version = manifest.get("application_version")
    match = _RELEASE_TAG.fullmatch(release_tag) if isinstance(release_tag, str) else None
    if not match or not isinstance(version, str) or match.group("version") != version:
        raise ReleaseManifestError("Release manifest tag and application version disagree.")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(SUPPORTED_PROFILES):
        raise ReleaseManifestError("Release manifest must contain exactly two artifacts.")
    for item in artifacts:
        item_authority = (
            item.get("signing_authority") if isinstance(item, Mapping) else None
        )
        if not isinstance(item_authority, str) or item_authority != signing_authority:
            raise ReleaseManifestError(
                "Release profiles do not share the manifest signing authority and Team ID."
            )
        if _validate_authority(item_authority) != manifest_team_id:
            raise ReleaseManifestError(
                "Release profiles do not share the manifest signing authority and Team ID."
            )
    return manifest


def _verify_release_artifact_record(
    item: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    asset_dir: Path,
) -> Mapping[str, Any]:
    profile = item.get("profile")
    if profile not in SUPPORTED_PROFILES:
        raise ReleaseManifestError("Release artifact profile is invalid.")
    if item.get("bundle_id") != OFFICIAL_BUNDLE_ID:
        raise ReleaseManifestError("Release artifact bundle identifier is not approved.")
    if item.get("signing_channel") != OFFICIAL_SIGNING_CHANNEL:
        raise ReleaseManifestError("Release artifact is not in the official signing channel.")
    authority = item.get("signing_authority")
    if not isinstance(authority, str):
        raise ReleaseManifestError("Release artifact signing authority is missing.")
    _validate_authority(authority)
    notarization = item.get("notarization")
    if not isinstance(notarization, Mapping) or (
        notarization.get("status") != "Accepted"
        or notarization.get("stapled") is not True
        or notarization.get("gatekeeper_accepted") is not True
    ):
        raise ReleaseManifestError("Release artifact notarization evidence is incomplete.")
    if item.get("offline_self_test") != {"ok": True}:
        raise ReleaseManifestError("Release artifact self-test evidence is incomplete.")

    for filename_key, hash_key, size_key, label in (
        ("filename", "sha256", "size_bytes", "Release archive"),
        (
            "identity_filename",
            "identity_sha256",
            "identity_size_bytes",
            "Artifact identity",
        ),
    ):
        filename = _safe_filename(item.get(filename_key), label=label)
        path = _require_file(asset_dir / filename, label=label)
        expected_hash = item.get(hash_key)
        if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            raise ReleaseManifestError(f"{label} checksum is invalid.")
        if sha256_file(path) != expected_hash or path.stat().st_size != item.get(size_key):
            raise ReleaseManifestError(f"{label} does not match the release manifest.")
        if filename_key == "filename":
            preflight_release_archive(path)

    sidecar_value = _read_json(asset_dir / str(item["identity_filename"]))
    try:
        envelope = ArtifactEnvelope.from_json(sidecar_value)
    except Exception as exc:
        raise ReleaseManifestError("Artifact identity sidecar is invalid.") from exc
    if envelope.to_json().get("artifact") != item.get("artifact"):
        raise ReleaseManifestError("Artifact identity does not match the release manifest.")
    shared_identity = (
        ("version", "application_version"),
        ("architecture", "architecture"),
        ("minimum_macos_version", "minimum_macos_version"),
        ("packaging_revision", "packaging_revision"),
    )
    if any(
        getattr(envelope.artifact, artifact_key) != manifest.get(manifest_key)
        for artifact_key, manifest_key in shared_identity
    ):
        raise ReleaseManifestError("Artifact identity disagrees with release metadata.")
    if (
        envelope.signing_channel != OFFICIAL_SIGNING_CHANNEL
        or envelope.signing_authority != authority
        or envelope.artifact.profile != profile
        or envelope.artifact.source_sha != manifest.get("source_sha")
    ):
        raise ReleaseManifestError("Artifact identity is incompatible with the release manifest.")
    return item


def verify_release_artifact(
    *,
    manifest_path: Path,
    checksum_path: Path,
    asset_dir: Path,
    profile: str,
    archive_path: Optional[Path] = None,
    expected_source_sha: Optional[str] = None,
    expected_team_id: Optional[str] = None,
) -> Mapping[str, Any]:
    """Verify one downloadable profile and its sidecar for offline installation."""

    manifest = _verified_manifest_header(
        manifest_path=manifest_path,
        checksum_path=checksum_path,
        expected_source_sha=expected_source_sha,
        expected_team_id=expected_team_id,
    )
    asset_dir = Path(asset_dir)
    if not asset_dir.is_dir() or asset_dir.is_symlink():
        raise ReleaseManifestError("Release asset directory is missing.")
    item = select_artifact(manifest, profile)
    _verify_release_artifact_record(item, manifest=manifest, asset_dir=asset_dir)
    if archive_path is not None:
        expected_archive = (asset_dir / str(item["filename"])).resolve()
        if Path(archive_path).resolve() != expected_archive:
            raise ReleaseManifestError(
                "Selected archive does not match the requested release profile."
            )
    return item


def verify_release_assets(
    *,
    manifest_path: Path,
    checksum_path: Path,
    asset_dir: Path,
    expected_source_sha: Optional[str] = None,
    expected_team_id: Optional[str] = None,
) -> Mapping[str, Any]:
    """Verify manifest integrity and every downloadable file without network access."""

    manifest = _verified_manifest_header(
        manifest_path=manifest_path,
        checksum_path=checksum_path,
        expected_source_sha=expected_source_sha,
        expected_team_id=expected_team_id,
    )
    asset_dir = Path(asset_dir)
    if not asset_dir.is_dir() or asset_dir.is_symlink():
        raise ReleaseManifestError("Release asset directory is missing.")
    artifacts = manifest["artifacts"]
    seen_profiles: set[str] = set()
    seen_filenames: set[str] = set()
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise ReleaseManifestError("Release artifact record is invalid.")
        profile = item.get("profile")
        if profile not in SUPPORTED_PROFILES or profile in seen_profiles:
            raise ReleaseManifestError("Release artifact profile is invalid or duplicated.")
        seen_profiles.add(profile)
        for filename_key, label in (
            ("filename", "Release archive"),
            ("identity_filename", "Artifact identity"),
        ):
            filename = _safe_filename(item.get(filename_key), label=label)
            if filename in seen_filenames:
                raise ReleaseManifestError("Release asset filename is duplicated.")
            seen_filenames.add(filename)
        _verify_release_artifact_record(item, manifest=manifest, asset_dir=asset_dir)

    if seen_profiles != set(SUPPORTED_PROFILES):
        raise ReleaseManifestError("Release manifest profile set is incomplete.")
    return manifest


def select_artifact(manifest: Mapping[str, Any], profile: str) -> Mapping[str, Any]:
    if profile not in SUPPORTED_PROFILES:
        raise ReleaseManifestError("Requested release profile is unsupported.")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseManifestError("Release manifest artifact list is missing.")
    matches = [
        item
        for item in artifacts
        if isinstance(item, Mapping) and item.get("profile") == profile
    ]
    if len(matches) != 1:
        raise ReleaseManifestError("Requested release profile is missing or duplicated.")
    return matches[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="record one verified profile")
    record.add_argument("--profile", required=True, choices=SUPPORTED_PROFILES)
    record.add_argument("--bundle", required=True, type=Path)
    record.add_argument("--archive", required=True, type=Path)
    record.add_argument("--sidecar", required=True, type=Path)
    record.add_argument("--notary-result", required=True, type=Path)
    record.add_argument("--self-test-result", required=True, type=Path)
    record.add_argument("--signing-authority", required=True)
    record.add_argument("--output", required=True, type=Path)

    build = subparsers.add_parser("build", help="build the two-profile manifest")
    build.add_argument("--source-sha", required=True)
    build.add_argument("--release-tag", required=True)
    build.add_argument(
        "--artifact-evidence",
        required=True,
        action="append",
        type=Path,
    )
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--checksum-output", required=True, type=Path)
    build.add_argument("--generated-at")

    for command, help_text in (
        ("verify-assets", "verify all release assets"),
        ("verify-artifact", "verify one selected release artifact"),
    ):
        verify = subparsers.add_parser(command, help=help_text)
        verify.add_argument("--manifest", required=True, type=Path)
        verify.add_argument("--checksum", required=True, type=Path)
        verify.add_argument("--asset-dir", required=True, type=Path)
        verify.add_argument("--expected-source-sha")
        verify.add_argument("--expected-team-id", required=True)
        if command == "verify-artifact":
            verify.add_argument("--profile", required=True, choices=SUPPORTED_PROFILES)
            verify.add_argument("--archive", type=Path)

    show = subparsers.add_parser("show", help="print one allowlisted artifact field")
    show.add_argument("--manifest", required=True, type=Path)
    show.add_argument("--profile", required=True, choices=SUPPORTED_PROFILES)
    show.add_argument(
        "--field",
        required=True,
        choices=("filename", "identity_filename", "signing_authority"),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "record":
            evidence = create_release_evidence(
                profile=args.profile,
                bundle=args.bundle,
                archive=args.archive,
                sidecar=args.sidecar,
                notary_result=args.notary_result,
                self_test_result=args.self_test_result,
                signing_authority=args.signing_authority,
            )
            _atomic_json(args.output, evidence)
        elif args.command == "build":
            manifest = build_release_manifest(
                args.artifact_evidence,
                source_sha=args.source_sha,
                release_tag=args.release_tag,
                generated_at=args.generated_at,
            )
            write_manifest_and_checksum(
                manifest,
                output=args.output,
                checksum_output=args.checksum_output,
            )
        elif args.command == "verify-assets":
            verify_release_assets(
                manifest_path=args.manifest,
                checksum_path=args.checksum,
                asset_dir=args.asset_dir,
                expected_source_sha=args.expected_source_sha,
                expected_team_id=args.expected_team_id,
            )
        elif args.command == "verify-artifact":
            verify_release_artifact(
                manifest_path=args.manifest,
                checksum_path=args.checksum,
                asset_dir=args.asset_dir,
                profile=args.profile,
                archive_path=args.archive,
                expected_source_sha=args.expected_source_sha,
                expected_team_id=args.expected_team_id,
            )
        else:
            manifest = _read_json(args.manifest)
            artifact = select_artifact(manifest, args.profile)
            value = artifact.get(args.field)
            if not isinstance(value, str) or not value:
                raise ReleaseManifestError("Requested artifact field is missing.")
            print(value)
    except ReleaseManifestError as exc:
        parser = _parser()
        parser.error(str(exc))
    return 0


__all__ = [
    "OFFICIAL_BUNDLE_ID",
    "OFFICIAL_SIGNING_CHANNEL",
    "RELEASE_MANIFEST_SCHEMA",
    "ReleaseManifestError",
    "build_release_manifest",
    "create_release_evidence",
    "extract_release_archive",
    "main",
    "preflight_release_archive",
    "select_artifact",
    "sha256_file",
    "verify_release_artifact",
    "verify_release_assets",
    "write_manifest_and_checksum",
]
