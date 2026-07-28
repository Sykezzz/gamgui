from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest

from gamgui.components.oneroster import OneRosterService, SafetyLimits, SnapshotState
from gamgui.components.oneroster import store as store_module
from gamgui.components.oneroster.ingest import _preflight_members
from gamgui.components.oneroster.store import OneRosterStore
from tests.test_oneroster_helpers import valid_files, zip_bytes


class _InfoArchive:
    def __init__(self, infos: list[zipfile.ZipInfo]) -> None:
        self._infos = infos

    def infolist(self) -> list[zipfile.ZipInfo]:
        return self._infos


def test_traversal_member_blocks_without_extracting(tmp_path: Path) -> None:
    files = valid_files()
    files["../outside.csv"] = "unsafe"
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(zip_bytes(files))
    assert snapshot.state is SnapshotState.BLOCKED
    assert not (tmp_path / "outside.csv").exists()
    issues = service.preview(snapshot.id, "issues")
    assert any(item["code"] == "OR-ZIP-TRAVERSAL" for item in issues.items)


def test_duplicate_case_member_and_nested_member_are_rejected(tmp_path: Path) -> None:
    output = io.BytesIO()
    files = valid_files()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("USERS.CSV", files["users.csv"])
        archive.writestr("nested/file.csv", "x")
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(output.getvalue())
    codes = {item["code"] for item in service.preview(snapshot.id, "issues").items}
    assert "OR-ZIP-DUPLICATE-MEMBER" in codes
    assert "OR-ZIP-TRAVERSAL" in codes


def test_finder_macos_metadata_is_ignored_without_relaxing_csv_paths(
    tmp_path: Path,
) -> None:
    output = io.BytesIO()
    files = valid_files()
    files["demographics.csv"] = "sourcedId,status,birthDate,sex\n"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
            archive.writestr(f"__MACOSX/._{name}", "AppleDouble metadata")

    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(output.getvalue())
    issues = service.preview(snapshot.id, "issues")

    assert snapshot.state is SnapshotState.READY
    assert snapshot.ready_for_apply
    assert issues.items == ()


def test_macos_metadata_directories_and_ds_store_are_ignored(
    tmp_path: Path,
) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in valid_files().items():
            archive.writestr(name, data)
        archive.writestr("__MACOSX/", "")
        archive.writestr(".DS_Store", "Finder metadata")

    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(output.getvalue())

    assert snapshot.state is SnapshotState.READY
    assert snapshot.ready_for_apply
    assert service.preview(snapshot.id, "issues").items == ()


@pytest.mark.parametrize(
    ("member_name", "expected_code"),
    (
        ("__MACOSX/not-appledouble.bin", "OR-ZIP-TRAVERSAL"),
        ("__MACOSX/nested/", "OR-ZIP-NESTED"),
        ("__MACOSX/../../outside.csv", "OR-ZIP-TRAVERSAL"),
        ("__MACOSX-copy/._users.csv", "OR-ZIP-TRAVERSAL"),
    ),
)
def test_non_finder_macos_paths_remain_blocked(
    tmp_path: Path,
    member_name: str,
    expected_code: str,
) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in valid_files().items():
            archive.writestr(name, data)
        archive.writestr(member_name, "not trusted metadata")

    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(output.getvalue())
    codes = {item["code"] for item in service.preview(snapshot.id, "issues").items}

    assert snapshot.state is SnapshotState.BLOCKED
    assert expected_code in codes


def test_macos_sidecars_still_obey_symlink_and_encryption_checks() -> None:
    symlink = zipfile.ZipInfo("__MACOSX/._users.csv")
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    encrypted = zipfile.ZipInfo("__MACOSX/._classes.csv")
    encrypted.flag_bits |= 0x1
    issues = []

    _preflight_members(
        _InfoArchive([symlink, encrypted]),  # type: ignore[arg-type]
        SafetyLimits(),
        issues,
    )

    codes = {issue.code for issue in issues}
    assert "OR-ZIP-SYMLINK" in codes
    assert "OR-ZIP-ENCRYPTED" in codes


def test_macos_sidecars_count_toward_size_ratio_and_expansion_limits(
    tmp_path: Path,
) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in valid_files().items():
            archive.writestr(name, data)
        archive.writestr("__MACOSX/._users.csv", b"0" * 2048)

    service = OneRosterService(
        "example.org",
        tmp_path / "component",
        safety_limits=SafetyLimits(
            max_member_bytes=1024,
            max_expanded_bytes=16 * 1024,
            max_compression_ratio=2,
        ),
    )
    snapshot = service.upload(output.getvalue())
    codes = {item["code"] for item in service.preview(snapshot.id, "issues").items}

    assert snapshot.state is SnapshotState.BLOCKED
    assert "OR-ZIP-MEMBER-SIZE" in codes
    assert "OR-ZIP-RATIO" in codes

    expanded = zipfile.ZipInfo("__MACOSX/._users.csv")
    expanded.file_size = 2048
    expanded.compress_size = 2048
    expansion_issues = []
    _preflight_members(
        _InfoArchive([expanded]),  # type: ignore[arg-type]
        SafetyLimits(
            max_member_bytes=4096,
            max_expanded_bytes=1024,
        ),
        expansion_issues,
    )
    assert "OR-ZIP-EXPANDED-SIZE" in {
        issue.code for issue in expansion_issues
    }


def test_symlink_and_encrypted_flags_are_preflight_blockers() -> None:
    symlink = zipfile.ZipInfo("users.csv")
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    encrypted = zipfile.ZipInfo("classes.csv")
    encrypted.flag_bits |= 0x1
    issues = []
    _preflight_members(_InfoArchive([symlink, encrypted]), SafetyLimits(), issues)  # type: ignore[arg-type]
    codes = {issue.code for issue in issues}
    assert "OR-ZIP-SYMLINK" in codes
    assert "OR-ZIP-ENCRYPTED" in codes


def test_member_count_size_and_ratio_limits_block(tmp_path: Path) -> None:
    files = valid_files()
    service = OneRosterService(
        "example.org",
        tmp_path / "component",
        safety_limits=SafetyLimits(
            max_archive_bytes=1024 * 1024,
            max_members=4,
            max_member_bytes=64,
            max_expanded_bytes=128,
            max_compression_ratio=2,
        ),
    )
    snapshot = service.upload(zip_bytes(files))
    codes = {item["code"] for item in service.preview(snapshot.id, "issues").items}
    assert "OR-ZIP-MEMBER-LIMIT" in codes


def test_delta_manifest_and_missing_header_are_preview_only(tmp_path: Path) -> None:
    files = valid_files()
    files["manifest.csv"] = files["manifest.csv"].replace(
        "file.enrollments,bulk", "file.enrollments,delta"
    )
    files["users.csv"] = files["users.csv"].replace("email,", "mail,", 1)
    service = OneRosterService("example.org", tmp_path / "component")
    snapshot = service.upload(zip_bytes(files))
    assert snapshot.package_mode == "delta"
    assert not snapshot.ready_for_apply
    codes = {item["code"] for item in service.preview(snapshot.id, "issues").items}
    assert "OR-PACKAGE-DELTA" in codes
    assert "OR-HEADER-MISSING" in codes


def test_archive_upload_cap_fails_closed_and_leaves_no_partial_import(
    tmp_path: Path,
) -> None:
    service = OneRosterService(
        "example.org",
        tmp_path / "component",
        safety_limits=SafetyLimits(max_archive_bytes=32),
    )
    with pytest.raises(Exception) as error:
        service.upload(zip_bytes(valid_files()))
    assert getattr(error.value, "code", "") == "OR-ZIP-SIZE"
    assert service.history() == ()


def test_csv_line_and_field_limits_block_hostile_preview_payloads(
    tmp_path: Path,
) -> None:
    files = valid_files()
    files["courses.csv"] = files["courses.csv"].replace(
        "Algebra I",
        "A" * 256,
    )
    service = OneRosterService(
        "example.org",
        tmp_path / "component",
        safety_limits=SafetyLimits(
            max_csv_line_chars=512,
            max_csv_field_chars=64,
        ),
    )

    snapshot = service.upload(zip_bytes(files))
    issues = service.preview(snapshot.id, "issues")

    assert snapshot.state is SnapshotState.BLOCKED
    assert any(item["code"] == "OR-CSV-LIMIT" for item in issues.items)
    course_preview = service.preview(snapshot.id, "courses").items
    assert all(
        len(str(value)) <= 64
        for item in course_preview
        for value in item.values()
    )


def test_manifest_and_table_record_caps_stop_row_amplification(
    tmp_path: Path,
) -> None:
    files = valid_files()
    files["manifest.csv"] += "".join(
        f"extension.property.{number},value\n" for number in range(20)
    )
    service = OneRosterService(
        "example.org",
        tmp_path / "manifest-cap",
        safety_limits=SafetyLimits(max_manifest_rows=8),
    )
    manifest_snapshot = service.upload(zip_bytes(files))
    manifest_codes = {
        item["code"]
        for item in service.preview(manifest_snapshot.id, "issues").items
    }
    assert manifest_snapshot.state is SnapshotState.BLOCKED
    assert "OR-RECORD-LIMIT" in manifest_codes

    table_service = OneRosterService(
        "example.org",
        tmp_path / "table-cap",
        safety_limits=SafetyLimits(max_records_per_file=1),
    )
    table_snapshot = table_service.upload(zip_bytes(valid_files()))
    table_codes = {
        item["code"]
        for item in table_service.preview(table_snapshot.id, "issues").items
    }
    assert table_snapshot.state is SnapshotState.BLOCKED
    assert "OR-RECORD-LIMIT" in table_codes
    assert table_snapshot.counts.users <= 1


def test_row_level_issue_amplification_is_summarized_and_bounded(
    tmp_path: Path,
) -> None:
    files = valid_files()
    files["users.csv"] += "".join(
        (
            f"bad-{number},invalid,bad-{number},bad-{number}@example.org,"
            "Bad,Row,id,school-1\n"
        )
        for number in range(100)
    )
    service = OneRosterService(
        "example.org",
        tmp_path / "issue-cap",
        safety_limits=SafetyLimits(max_issues=5),
    )

    snapshot = service.upload(zip_bytes(files))
    issues = service.preview(snapshot.id, "issues", limit=50)

    assert snapshot.state is SnapshotState.BLOCKED
    assert snapshot.issue_count <= 5
    assert issues.total <= 5
    assert any(item["code"] == "OR-ISSUE-LIMIT" for item in issues.items)


def test_oneroster_store_permission_failure_prevents_state_database_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "component"
    original_chmod = store_module.os.chmod

    def fail_root_chmod(candidate, mode):
        if Path(candidate) == root:
            raise PermissionError("policy denied")
        return original_chmod(candidate, mode)

    monkeypatch.setattr(store_module.os, "chmod", fail_root_chmod)

    with pytest.raises(PermissionError, match="policy denied"):
        OneRosterStore("example.org", root)

    assert not (root / "state.db").exists()


def test_oneroster_store_tolerates_only_disappearing_sqlite_companions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "component"
    store = OneRosterStore("example.org", root)
    original = store_module._chmod

    def disappear_companions(candidate, mode):
        if str(candidate).endswith(("-wal", "-shm")):
            raise FileNotFoundError(candidate)
        return original(candidate, mode)

    monkeypatch.setattr(store_module, "_chmod", disappear_companions)
    store._restrict_state_perms()

    normalized = root / "normalized.db"
    normalized.touch()
    store_module._secure_sqlite_files(normalized)

    def reject_companion(candidate, mode):
        if str(candidate).endswith("-wal"):
            raise PermissionError("unsafe companion")
        return original(candidate, mode)

    monkeypatch.setattr(store_module, "_chmod", reject_companion)
    with pytest.raises(PermissionError, match="unsafe companion"):
        store._restrict_state_perms()


def test_oneroster_missing_main_database_stays_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = OneRosterStore("example.org", tmp_path / "component")
    original = store_module._chmod

    def disappear_main(candidate, mode):
        if Path(candidate) == store.state_path:
            raise FileNotFoundError(candidate)
        return original(candidate, mode)

    monkeypatch.setattr(store_module, "_chmod", disappear_main)
    with pytest.raises(FileNotFoundError):
        store._restrict_state_perms()
