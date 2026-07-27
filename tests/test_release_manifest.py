import json
import plistlib
import shutil
import stat
import zipfile
from pathlib import Path

import pytest

from gamgui.core import release_manifest
from gamgui.core.components import (
    CORE_PROFILE,
    ONEROSTER_PROFILE,
    ComponentError,
    build_profile_payload,
    write_artifact_sidecar,
)
from gamgui.core.release_manifest import (
    OFFICIAL_BUNDLE_ID,
    ReleaseManifestError,
    build_release_manifest,
    create_release_evidence,
    extract_release_archive,
    main,
    preflight_release_archive,
    verify_release_artifact,
    verify_release_assets,
    write_manifest_and_checksum,
)
from gamgui.core.updater import _extract_verified_archive


SHA = "a" * 40
VERSION = "1.2.3"
AUTHORITY = "Developer ID Application: GamGUI Example (ABCDE12345)"


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _profile_evidence(
    tmp_path: Path,
    profile: str,
    *,
    authority: str = AUTHORITY,
) -> Path:
    profile_root = tmp_path / profile
    bundle = profile_root / "GamGUI.app"
    metadata = (
        bundle
        / "Contents"
        / "MacOS"
        / "_internal"
        / "resources"
        / "components"
        / "profile.json"
    )
    _write_json(
        metadata,
        build_profile_payload(
            profile,
            source_sha=SHA,
            version=VERSION,
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="7",
        ),
    )
    info = bundle / "Contents" / "Info.plist"
    info.parent.mkdir(parents=True, exist_ok=True)
    with info.open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": OFFICIAL_BUNDLE_ID,
                "LSMinimumSystemVersion": "12.0",
            },
            handle,
        )
    executable = bundle / "Contents" / "MacOS" / "GamGUI"
    executable.write_bytes(b"fake signed executable")

    generated_sidecar = write_artifact_sidecar(
        bundle,
        signing_channel="developer-id",
        signing_authority=authority,
    )
    asset_stem = f"GamGUI-{VERSION}-{profile}-arm64"
    sidecar = tmp_path / f"{asset_stem}.artifact.json"
    shutil.copy2(generated_sidecar, sidecar)
    archive = tmp_path / f"{asset_stem}.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(
            "GamGUI.app/Contents/release-fixture.txt",
            f"post-staple archive for {profile}",
        )
    notary = _write_json(
        profile_root / "notary-result.json",
        {"status": "Accepted", "id": f"submission-{profile}"},
    )
    self_test = _write_json(
        profile_root / "self-test.json",
        {"ok": True, "failures": []},
    )

    evidence = create_release_evidence(
        profile=profile,
        bundle=bundle,
        archive=archive,
        sidecar=sidecar,
        notary_result=notary,
        self_test_result=self_test,
        signing_authority=authority,
    )
    evidence_path = tmp_path / f"{asset_stem}.evidence.json"
    _write_json(evidence_path, evidence)
    return evidence_path


def _complete_release(tmp_path: Path):
    evidence = [
        _profile_evidence(tmp_path, CORE_PROFILE),
        _profile_evidence(tmp_path, ONEROSTER_PROFILE),
    ]
    manifest = build_release_manifest(
        evidence,
        source_sha=SHA,
        release_tag=f"v{VERSION}",
        generated_at="2026-07-25T12:00:00Z",
    )
    manifest_path = tmp_path / "release-manifest.json"
    checksum_path = tmp_path / "release-manifest.json.sha256"
    write_manifest_and_checksum(
        manifest,
        output=manifest_path,
        checksum_output=checksum_path,
    )
    return manifest, manifest_path, checksum_path


def test_manifest_binds_both_profiles_to_one_exact_source_and_platform(tmp_path):
    manifest, manifest_path, checksum_path = _complete_release(tmp_path)

    assert manifest["profiles"] == [CORE_PROFILE, ONEROSTER_PROFILE]
    assert manifest["source_sha"] == SHA
    assert manifest["application_version"] == VERSION
    assert manifest["architecture"] == "arm64"
    assert manifest["minimum_macos_version"] == "12.0"
    assert manifest["packaging_revision"] == "7"
    assert manifest["signing_channel"] == "developer-id"
    assert manifest["signing_authority"] == AUTHORITY
    assert manifest["team_id"] == "ABCDE12345"
    assert [item["profile"] for item in manifest["artifacts"]] == [
        CORE_PROFILE,
        ONEROSTER_PROFILE,
    ]
    assert all(
        item["notarization"]["status"] == "Accepted"
        and item["notarization"]["stapled"] is True
        and item["offline_self_test"] == {"ok": True}
        for item in manifest["artifacts"]
    )

    verified = verify_release_assets(
        manifest_path=manifest_path,
        checksum_path=checksum_path,
        asset_dir=tmp_path,
        expected_source_sha=SHA,
    )
    assert verified["source_sha"] == SHA


def test_manifest_refuses_partial_profile_release(tmp_path):
    core = _profile_evidence(tmp_path, CORE_PROFILE)
    with pytest.raises(ReleaseManifestError, match="exactly two"):
        build_release_manifest(
            [core],
            source_sha=SHA,
            release_tag=f"v{VERSION}",
        )


def test_manifest_refuses_wrong_version_tag(tmp_path):
    evidence = [
        _profile_evidence(tmp_path, CORE_PROFILE),
        _profile_evidence(tmp_path, ONEROSTER_PROFILE),
    ]
    with pytest.raises(ReleaseManifestError, match="tag does not match"):
        build_release_manifest(
            evidence,
            source_sha=SHA,
            release_tag="v9.9.9",
        )


def test_manifest_refuses_profiles_with_different_signing_authorities(tmp_path):
    evidence = [
        _profile_evidence(tmp_path, CORE_PROFILE),
        _profile_evidence(
            tmp_path,
            ONEROSTER_PROFILE,
            authority="Developer ID Application: Other GamGUI (ZYXWV98765)",
        ),
    ]
    with pytest.raises(ReleaseManifestError, match="exactly one signing authority"):
        build_release_manifest(
            evidence,
            source_sha=SHA,
            release_tag=f"v{VERSION}",
        )


def test_evidence_refuses_local_signing_channel(tmp_path):
    profile = CORE_PROFILE
    profile_root = tmp_path / profile
    bundle = profile_root / "GamGUI.app"
    metadata = (
        bundle
        / "Contents"
        / "MacOS"
        / "_internal"
        / "resources"
        / "components"
        / "profile.json"
    )
    _write_json(
        metadata,
        build_profile_payload(
            profile,
            source_sha=SHA,
            version=VERSION,
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="7",
        ),
    )
    info = bundle / "Contents" / "Info.plist"
    info.parent.mkdir(parents=True, exist_ok=True)
    with info.open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": OFFICIAL_BUNDLE_ID,
                "LSMinimumSystemVersion": "12.0",
            },
            handle,
        )
    sidecar = write_artifact_sidecar(
        bundle,
        signing_channel="local",
        signing_authority="GamGUI Local",
    )
    archive = tmp_path / "core.zip"
    archive.write_bytes(b"archive")
    notary = _write_json(
        profile_root / "notary-result.json",
        {"status": "Accepted", "id": "submission"},
    )
    self_test = _write_json(
        profile_root / "self-test.json",
        {"ok": True, "failures": []},
    )

    with pytest.raises(ReleaseManifestError, match="official signing channel"):
        create_release_evidence(
            profile=profile,
            bundle=bundle,
            archive=archive,
            sidecar=sidecar,
            notary_result=notary,
            self_test_result=self_test,
            signing_authority=AUTHORITY,
        )


def test_asset_verification_detects_archive_and_manifest_tampering(tmp_path):
    manifest, manifest_path, checksum_path = _complete_release(tmp_path)
    archive = tmp_path / manifest["artifacts"][0]["filename"]
    archive.write_bytes(b"tampered")
    with pytest.raises(ReleaseManifestError, match="Release archive does not match"):
        verify_release_assets(
            manifest_path=manifest_path,
            checksum_path=checksum_path,
            asset_dir=tmp_path,
        )

    _complete_release(tmp_path)
    manifest_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ReleaseManifestError, match="checksum"):
        verify_release_assets(
            manifest_path=manifest_path,
            checksum_path=checksum_path,
            asset_dir=tmp_path,
        )


def test_verification_requires_manifest_profiles_to_share_one_team(tmp_path):
    manifest, manifest_path, checksum_path = _complete_release(tmp_path)
    manifest["artifacts"][1]["signing_authority"] = (
        "Developer ID Application: Other GamGUI (ZYXWV98765)"
    )
    write_manifest_and_checksum(
        manifest,
        output=manifest_path,
        checksum_output=checksum_path,
    )

    with pytest.raises(ReleaseManifestError, match="do not share"):
        verify_release_artifact(
            manifest_path=manifest_path,
            checksum_path=checksum_path,
            asset_dir=tmp_path,
            profile=CORE_PROFILE,
            expected_team_id="ABCDE12345",
        )


def test_verification_rejects_a_different_operator_team_id(tmp_path):
    _, manifest_path, checksum_path = _complete_release(tmp_path)
    with pytest.raises(ReleaseManifestError, match="expected Apple Team ID"):
        verify_release_assets(
            manifest_path=manifest_path,
            checksum_path=checksum_path,
            asset_dir=tmp_path,
            expected_team_id="ZYXWV98765",
        )
    with pytest.raises(ReleaseManifestError, match="uppercase"):
        verify_release_assets(
            manifest_path=manifest_path,
            checksum_path=checksum_path,
            asset_dir=tmp_path,
            expected_team_id="abcde12345",
        )


def test_verify_assets_cli_requires_independent_team_id():
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "verify-assets",
                "--manifest",
                "release-manifest.json",
                "--checksum",
                "release-manifest.json.sha256",
                "--asset-dir",
                ".",
            ]
        )
    assert exc.value.code == 2


def test_release_archive_preflight_rejects_traversal_and_escaping_symlink(tmp_path):
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as handle:
        handle.writestr("../outside", "bad")
    with pytest.raises(ReleaseManifestError, match="unsafe path"):
        preflight_release_archive(traversal)

    symlink = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("GamGUI.app/Contents/escape")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(symlink, "w") as handle:
        handle.writestr(info, "../../../outside")
    with pytest.raises(ReleaseManifestError, match="escaping symbolic link"):
        preflight_release_archive(symlink)

    chained_parent = tmp_path / "chained-parent.zip"
    parent = zipfile.ZipInfo("GamGUI.app/Contents/link")
    parent.create_system = 3
    parent.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(chained_parent, "w") as handle:
        handle.writestr(parent, "real")
        handle.writestr("GamGUI.app/Contents/link/payload", "unsafe overwrite")
    with pytest.raises(ReleaseManifestError, match="beneath a symbolic link"):
        preflight_release_archive(chained_parent)


def test_release_archive_preflight_bounds_entry_count_and_expanded_size(
    tmp_path,
    monkeypatch,
):
    archive = tmp_path / "bounded.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("GamGUI.app/Contents/one", "1")
        handle.writestr("GamGUI.app/Contents/two", "22")

    monkeypatch.setattr(release_manifest, "MAX_RELEASE_ARCHIVE_ENTRIES", 1)
    with pytest.raises(ReleaseManifestError, match="entry count"):
        preflight_release_archive(archive)

    monkeypatch.setattr(release_manifest, "MAX_RELEASE_ARCHIVE_ENTRIES", 10)
    monkeypatch.setattr(release_manifest, "MAX_RELEASE_ARCHIVE_EXPANDED_BYTES", 2)
    with pytest.raises(ReleaseManifestError, match="size limit"):
        preflight_release_archive(archive)

    monkeypatch.setattr(
        release_manifest,
        "MAX_RELEASE_ARCHIVE_EXPANDED_BYTES",
        1024,
    )
    duplicate = tmp_path / "normalized-duplicate.zip"
    with zipfile.ZipFile(duplicate, "w") as handle:
        handle.writestr("GamGUI.app/Contents/Resource", "one")
        handle.writestr("GamGUI.app/Contents/resource", "two")
    with pytest.raises(ReleaseManifestError, match="duplicate path"):
        preflight_release_archive(duplicate)


def test_official_release_archive_matches_safe_installer_extractor(tmp_path):
    archive = tmp_path / "valid.zip"
    info = zipfile.ZipInfo("GamGUI.app/Contents/MacOS/GamGUI")
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o755) << 16
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(info, b"executable")

    verifier_destination = tmp_path / "verifier-extract"
    verifier_destination.mkdir()
    bundle = extract_release_archive(archive, verifier_destination)

    assert bundle == verifier_destination / "GamGUI.app"
    assert (bundle / "Contents" / "MacOS" / "GamGUI").read_bytes() == b"executable"

    installer_destination = tmp_path / "installer-extract"
    installer_destination.mkdir()
    _extract_verified_archive(archive, installer_destination)
    assert (
        installer_destination / "GamGUI.app" / "Contents" / "MacOS" / "GamGUI"
    ).read_bytes() == b"executable"


def test_release_archive_rejects_appledouble_metadata_for_installer_compatibility(
    tmp_path,
):
    archive = tmp_path / "appledouble.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("GamGUI.app/Contents/MacOS/GamGUI", b"executable")
        handle.writestr(
            "__MACOSX/GamGUI.app/Contents/MacOS/._GamGUI",
            b"metadata",
        )

    with pytest.raises(ReleaseManifestError, match="outside GamGUI.app"):
        preflight_release_archive(archive)

    destination = tmp_path / "installer-extract"
    destination.mkdir()
    with pytest.raises(ComponentError, match="unsafe path"):
        _extract_verified_archive(archive, destination)


def test_selected_profile_verification_does_not_require_other_profile_download(
    tmp_path,
):
    manifest, manifest_path, checksum_path = _complete_release(tmp_path)
    selected = manifest["artifacts"][1]
    other = manifest["artifacts"][0]
    (tmp_path / other["filename"]).unlink()
    (tmp_path / other["identity_filename"]).unlink()

    verified = verify_release_artifact(
        manifest_path=manifest_path,
        checksum_path=checksum_path,
        asset_dir=tmp_path,
        profile=ONEROSTER_PROFILE,
        archive_path=tmp_path / selected["filename"],
        expected_source_sha=SHA,
    )

    assert verified["profile"] == ONEROSTER_PROFILE
    with pytest.raises(ReleaseManifestError, match="Selected archive"):
        verify_release_artifact(
            manifest_path=manifest_path,
            checksum_path=checksum_path,
            asset_dir=tmp_path,
            profile=ONEROSTER_PROFILE,
            archive_path=tmp_path / "different.zip",
        )
