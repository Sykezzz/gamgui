"""Safe OneRoster 1.1 ZIP ingestion and normalization.

ZIP members are never extracted to the filesystem. CSV records are decoded directly
from ``ZipFile.open`` and reduced to the fields needed by the Classroom import planner.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import zipfile
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable, Mapping, Optional, Sequence

from .models import (
    ImportIssue,
    IssueSeverity,
    SnapshotCounts,
    SnapshotState,
)
from .preview_index import rebuild_preview_index


REQUIRED_FILES = (
    "manifest.csv",
    "academicSessions.csv",
    "classes.csv",
    "courses.csv",
    "enrollments.csv",
    "orgs.csv",
    "users.csv",
)
OPTIONAL_FILES = (
    "categories.csv",
    "classResources.csv",
    "courseResources.csv",
    "demographics.csv",
    "lineItems.csv",
    "resources.csv",
    "results.csv",
)
ALLOWED_FILES = frozenset(name.casefold() for name in REQUIRED_FILES + OPTIONAL_FILES)
IGNORED_MACOS_ROOT_FILES = frozenset((".ds_store",))
MACOS_METADATA_DIRECTORY = "__macosx/"
MACOS_SIDECAR_PREFIX = "__macosx/._"
DEFAULT_MAX_CSV_LINE_CHARS = 4 * 1024 * 1024
DEFAULT_MAX_CSV_FIELD_CHARS = 1024 * 1024
DEFAULT_MAX_RECORDS_PER_FILE = 2_000_000
DEFAULT_MAX_MANIFEST_ROWS = 256
DEFAULT_MAX_ISSUES = 10_000
csv.field_size_limit(DEFAULT_MAX_CSV_FIELD_CHARS)

_CLASS_DERIVED_ISSUE_CODES = frozenset(
    {
        "OR-REFERENCE-TERM",
        "OR-REFERENCE-COURSE",
        "OR-REFERENCE-SCHOOL-YEAR",
        "OR-ALIAS-MISSING",
        "OR-ALIAS-COLLISION",
        "OR-ENROLLMENT-ROLE",
        "OR-OWNER-MISSING",
        "OR-OWNER-AMBIGUOUS",
        "OR-USER-MISSING",
        "OR-COURSE-NAME-INCOMPLETE",
        "OR-GAM-VALUE-INVALID",
    }
)

REQUIRED_HEADERS: Mapping[str, frozenset[str]] = {
    "manifest.csv": frozenset(("propertyName", "value")),
    "academicSessions.csv": frozenset(
        ("sourcedId", "status", "title", "type", "startDate", "endDate", "schoolYear")
    ),
    "classes.csv": frozenset(("sourcedId", "status", "title", "courseSourcedId")),
    "courses.csv": frozenset(("sourcedId", "status", "title", "schoolYearSourcedId")),
    "enrollments.csv": frozenset(
        ("sourcedId", "status", "classSourcedId", "userSourcedId", "role", "primary")
    ),
    "orgs.csv": frozenset(("sourcedId", "status", "name", "type")),
    "users.csv": frozenset(("sourcedId", "status", "email")),
}


@dataclass(frozen=True)
class SafetyLimits:
    max_archive_bytes: int = 512 * 1024 * 1024
    max_members: int = 32
    max_member_bytes: int = 512 * 1024 * 1024
    max_expanded_bytes: int = 1024 * 1024 * 1024
    max_compression_ratio: float = 250.0
    max_csv_line_chars: int = DEFAULT_MAX_CSV_LINE_CHARS
    max_csv_field_chars: int = DEFAULT_MAX_CSV_FIELD_CHARS
    max_records_per_file: int = DEFAULT_MAX_RECORDS_PER_FILE
    max_manifest_rows: int = DEFAULT_MAX_MANIFEST_ROWS
    max_issues: int = DEFAULT_MAX_ISSUES

    def __post_init__(self) -> None:
        if min(
            self.max_archive_bytes,
            self.max_members,
            self.max_member_bytes,
            self.max_expanded_bytes,
            self.max_csv_line_chars,
            self.max_csv_field_chars,
            self.max_records_per_file,
            self.max_manifest_rows,
            self.max_issues,
        ) <= 0:
            raise ValueError("OneRoster ZIP safety limits must be positive.")
        if self.max_compression_ratio <= 1:
            raise ValueError("Compression-ratio limit must be greater than one.")
        if self.max_issues < 2:
            raise ValueError("OneRoster issue limit must leave room for a summary.")


@dataclass(frozen=True)
class IngestionResult:
    source_sha256: str
    state: SnapshotState
    package_mode: str
    selected_session_id: str
    counts: SnapshotCounts
    issues: tuple[ImportIssue, ...]

    @property
    def blocking_issue_count(self) -> int:
        return sum(issue.blocking for issue in self.issues)


class _IssueCollector(list[ImportIssue]):
    """Bound hostile row-level error amplification while preserving a blocker."""

    def __init__(
        self,
        maximum: int,
        initial: Iterable[ImportIssue] = (),
    ) -> None:
        super().__init__()
        self.maximum = max(2, int(maximum))
        self._summarized = False
        for issue in initial:
            self.append(issue)

    def append(self, issue: ImportIssue) -> None:
        if len(self) < self.maximum - 1:
            super().append(issue)
            return
        if not self._summarized:
            super().append(
                ImportIssue(
                    code="OR-ISSUE-LIMIT",
                    severity=IssueSeverity.ERROR,
                    message=(
                        "The import produced more row-level issues than can be "
                        "retained safely; correct the source and upload it again."
                    ),
                    blocking=True,
                )
            )
            self._summarized = True


def copy_upload(
    source: BinaryIO | bytes | bytearray | Path | str,
    target: Path,
    *,
    limits: SafetyLimits,
) -> str:
    """Copy an upload to a private path while hashing and enforcing an archive cap."""

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _chmod(target.parent, 0o700)
    digest = hashlib.sha256()
    total = 0
    stream, close_stream = _open_source(source)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(target), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise TypeError("OneRoster uploads must be opened in binary mode.")
                total += len(chunk)
                if total > limits.max_archive_bytes:
                    raise ValueError("The OneRoster ZIP exceeds the configured upload limit.")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        if close_stream:
            stream.close()
    _chmod(target, 0o600)
    return digest.hexdigest()


def ingest_archive(
    archive_path: Path,
    normalized_path: Path,
    *,
    domain: str,
    source_sha256: str,
    limits: SafetyLimits = SafetyLimits(),
    today: Optional[date] = None,
) -> IngestionResult:
    """Validate and normalize a copied OneRoster ZIP into a private SQLite database."""

    issues = _IssueCollector(limits.max_issues)
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    _chmod(normalized_path.parent, 0o700)
    _create_normalized_db(normalized_path)
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (zipfile.BadZipFile, OSError):
        issues.append(
            _issue("OR-ZIP-INVALID", "The upload is not a readable ZIP archive.")
        )
        _replace_issues(normalized_path, issues, domain)
        return _result(source_sha256, "invalid", "", SnapshotCounts(), issues)

    with archive:
        members = _preflight_members(archive, limits, issues)
        if any(issue.blocking for issue in issues):
            _replace_issues(normalized_path, issues, domain)
            return _result(source_sha256, "invalid", "", SnapshotCounts(), issues)

        manifest = _read_manifest(
            archive,
            members["manifest.csv"],
            issues,
            limits,
        )
        package_mode = _manifest_mode(manifest, members, issues)
        if any(issue.code.startswith("OR-MANIFEST") for issue in issues):
            package_mode = "invalid"

        with closing(_normalized_conn(normalized_path)) as conn:
            with conn:
                _load_all_tables(
                    conn,
                    archive,
                    members,
                    domain,
                    issues,
                    limits,
                )
                _validate_references(conn, domain, issues)
                selected_session = _choose_current_session(
                    conn, domain, today or datetime.now(timezone.utc).date(), issues
                )
                _build_course_plans(conn, domain, selected_session, issues)
                _write_issues(conn, issues)
                rebuild_preview_index(conn, domain)
            counts = _snapshot_counts(conn, domain)

    _chmod_sqlite(normalized_path)
    return _result(
        source_sha256,
        package_mode,
        selected_session,
        counts,
        issues,
    )


def rebuild_course_plans(
    normalized_path: Path,
    *,
    domain: str,
    selected_session_id: str,
) -> tuple[SnapshotCounts, tuple[ImportIssue, ...]]:
    """Rebuild derived plans after an explicit operator term selection."""

    with closing(_normalized_conn(normalized_path)) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM academic_sessions
            WHERE domain = ? AND sourced_id = ? AND status != 'tobedeleted'
            """,
            (domain, selected_session_id),
        ).fetchone()
        if row is None:
            raise KeyError("Academic session not found in this import.")
        with conn:
            conn.execute(
                "DELETE FROM issues WHERE code = 'OR-TERM-AMBIGUOUS'"
            )
            conn.execute(
                "DELETE FROM issues WHERE code IN ({})".format(
                    ", ".join("?" for _ in _CLASS_DERIVED_ISSUE_CODES)
                ),
                tuple(_CLASS_DERIVED_ISSUE_CODES),
            )
            issues = _IssueCollector(
                DEFAULT_MAX_ISSUES,
                tuple(_issues_from_db(conn)),
            )
            _build_course_plans(conn, domain, selected_session_id, issues)
            _write_issues(conn, issues)
            rebuild_preview_index(conn, domain)
        counts = _snapshot_counts(conn, domain)
        updated_issues = tuple(_issues_from_db(conn))
    _chmod_sqlite(normalized_path)
    return counts, updated_issues


def section_alias(source_id: str) -> str:
    value = (source_id or "").strip()
    if value.casefold().startswith("section_"):
        value = value[len("Section_") :]
    return f"Section_{value}" if value else ""


def course_display_name(course_title: str, class_code: str, class_title: str, school_year: str) -> str:
    title = (course_title or "").strip()
    section = (class_code or "").strip()
    fallback = (class_title or "").strip()
    year = (school_year or "").strip()
    if not section:
        return fallback
    if not title or not section or not year:
        return ""
    return f"{title} \u2013 {section} ({year})"


def _result(
    source_hash: str,
    package_mode: str,
    selected_session_id: str,
    counts: SnapshotCounts,
    issues: Sequence[ImportIssue],
) -> IngestionResult:
    blocking = any(issue.blocking for issue in issues)
    state = (
        SnapshotState.READY
        if package_mode == "bulk" and selected_session_id and not blocking
        else SnapshotState.BLOCKED
    )
    return IngestionResult(
        source_sha256=source_hash,
        state=state,
        package_mode=package_mode,
        selected_session_id=selected_session_id,
        counts=counts,
        issues=tuple(issues),
    )


def _open_source(
    source: BinaryIO | bytes | bytearray | Path | str,
) -> tuple[BinaryIO, bool]:
    if isinstance(source, (bytes, bytearray)):
        return io.BytesIO(bytes(source)), True
    if isinstance(source, (str, Path)):
        return Path(source).open("rb"), True
    if not hasattr(source, "read"):
        raise TypeError("OneRoster upload must be bytes, a path, or a binary file object.")
    return source, False


def _preflight_members(
    archive: zipfile.ZipFile,
    limits: SafetyLimits,
    issues: list[ImportIssue],
) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > limits.max_members:
        issues.append(
            _issue(
                "OR-ZIP-MEMBER-LIMIT",
                f"The archive contains more than {limits.max_members} members.",
            )
        )
        return {}

    members: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        name = info.filename
        folded = name.casefold()
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        total += max(0, int(info.file_size))
        if info.file_size > limits.max_member_bytes:
            issues.append(
                _issue(
                    "OR-ZIP-MEMBER-SIZE",
                    f"ZIP member {name} exceeds the expanded-size limit.",
                )
            )
        ratio = info.file_size / max(1, info.compress_size)
        if ratio > limits.max_compression_ratio:
            issues.append(
                _issue(
                    "OR-ZIP-RATIO",
                    f"ZIP member {name} exceeds the compression-ratio limit.",
                )
            )
        unsafe_kind = False
        if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
            issues.append(_issue("OR-ZIP-SYMLINK", "ZIP symbolic links are not allowed."))
            unsafe_kind = True
        if info.flag_bits & 0x1:
            issues.append(_issue("OR-ZIP-ENCRYPTED", "Encrypted ZIP members are not supported."))
            unsafe_kind = True
        if unsafe_kind:
            continue
        if _is_ignorable_macos_metadata(info):
            # Finder-created archives can contain AppleDouble sidecars beneath
            # __MACOSX/ plus a root .DS_Store. These members are never opened or
            # extracted. Exact-name matching keeps every other nested member behind
            # the normal path boundary.
            continue
        if info.is_dir() or name.endswith(("/", "\\")):
            issues.append(_issue("OR-ZIP-NESTED", "ZIP directories are not allowed."))
            continue
        if (
            "/" in name
            or "\\" in name
            or Path(name).name != name
            or name in (".", "..")
            or "\x00" in name
        ):
            issues.append(
                _issue(
                    "OR-ZIP-TRAVERSAL",
                    "Every OneRoster CSV must be a root-level ZIP member.",
                )
            )
            continue
        if folded not in ALLOWED_FILES:
            issues.append(
                _issue(
                    "OR-ZIP-UNEXPECTED-MEMBER",
                    f"Unexpected root-level member: {name}.",
                )
            )
            continue
        if folded in members:
            issues.append(
                _issue(
                    "OR-ZIP-DUPLICATE-MEMBER",
                    f"The archive contains duplicate member names: {name}.",
                )
            )
            continue
        members[folded] = info
    if total > limits.max_expanded_bytes:
        issues.append(
            _issue(
                "OR-ZIP-EXPANDED-SIZE",
                "The archive exceeds the total expanded-size limit.",
            )
        )
    for required in REQUIRED_FILES:
        if required.casefold() not in members:
            issues.append(
                _issue(
                    "OR-MANIFEST-MISSING-FILE",
                    f"Required OneRoster file {required} is missing.",
                )
            )
    return members


def _is_ignorable_macos_metadata(info: zipfile.ZipInfo) -> bool:
    folded = info.filename.casefold()
    if folded in IGNORED_MACOS_ROOT_FILES:
        return True
    if folded == MACOS_METADATA_DIRECTORY:
        return info.is_dir()
    if not folded.startswith(MACOS_SIDECAR_PREFIX):
        return False
    sidecar_target = folded[len(MACOS_SIDECAR_PREFIX) :]
    return (
        sidecar_target in ALLOWED_FILES
        or sidecar_target in IGNORED_MACOS_ROOT_FILES
    )


def _read_manifest(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    issues: list[ImportIssue],
    limits: SafetyLimits,
) -> dict[str, str]:
    rows, headers = _read_rows(
        archive,
        info,
        "manifest.csv",
        issues,
        limits,
    )
    if not REQUIRED_HEADERS["manifest.csv"].issubset(headers):
        return {}
    result: dict[str, str] = {}
    for row_number, row in rows:
        key = (row.get("propertyName") or "").strip().casefold()
        value = (row.get("value") or "").strip()
        if not key:
            issues.append(
                _issue(
                    "OR-MANIFEST-ROW",
                    "A manifest property name is blank.",
                    "manifest",
                    row_number=row_number,
                )
            )
            continue
        if key in result:
            issues.append(
                _issue(
                    "OR-MANIFEST-DUPLICATE",
                    f"Manifest property {key} appears more than once.",
                    "manifest",
                    key,
                    row_number,
                )
            )
        else:
            result[key] = value
    manifest_version = result.get("manifest.version", "")
    if manifest_version != "1.0":
        issues.append(
            _issue(
                "OR-MANIFEST-FORMAT-VERSION",
                "Only OneRoster CSV manifest version 1.0 is supported.",
                "manifest",
            )
        )
    oneroster_version = result.get("oneroster.version", "")
    if oneroster_version != "1.1":
        issues.append(
            _issue(
                "OR-MANIFEST-VERSION",
                "Only OneRoster 1.1 packages are supported.",
                "manifest",
            )
        )
    return result


def _manifest_mode(
    manifest: Mapping[str, str],
    members: Mapping[str, zipfile.ZipInfo],
    issues: list[ImportIssue],
) -> str:
    modes: list[str] = []
    for filename in REQUIRED_FILES:
        if filename == "manifest.csv":
            continue
        property_name = f"file.{filename[:-4]}".casefold()
        declared = (manifest.get(property_name) or "").strip().casefold()
        if not declared:
            issues.append(
                _issue(
                    "OR-MANIFEST-MISMATCH",
                    f"Manifest does not declare {filename}.",
                    "manifest",
                    filename,
                )
            )
            continue
        modes.append(declared)
        if filename.casefold() in members and declared == "absent":
            issues.append(
                _issue(
                    "OR-MANIFEST-MISMATCH",
                    f"Manifest declares {filename} absent, but the file is present.",
                    "manifest",
                    filename,
                )
            )
    if "delta" in modes:
        issues.append(
            _issue(
                "OR-PACKAGE-DELTA",
                "Delta packages are preview-only; a full bulk snapshot is required to apply changes.",
            )
        )
        return "delta"
    if modes and all(mode == "bulk" for mode in modes):
        return "bulk"
    return "invalid"


class _CSVLimitError(ValueError):
    pass


class _BoundedTextLines:
    """Yield physical CSV lines without allowing unbounded TextIO buffering."""

    def __init__(self, stream: io.TextIOBase, maximum: int) -> None:
        self.stream = stream
        self.maximum = int(maximum)

    def __iter__(self) -> "_BoundedTextLines":
        return self

    def __next__(self) -> str:
        line = self.stream.readline(self.maximum + 1)
        if not line:
            raise StopIteration
        if len(line) > self.maximum:
            raise _CSVLimitError("CSV line exceeds the configured limit.")
        return line


def _require_bounded_fields(
    row: Mapping[str, str],
    limits: SafetyLimits,
) -> None:
    if any(len(value) > limits.max_csv_field_chars for value in row.values()):
        raise _CSVLimitError("CSV field exceeds the configured limit.")


def _validate_headers(
    fieldnames: Optional[Sequence[object]],
    canonical_name: str,
    issues: list[ImportIssue],
) -> tuple[frozenset[str], bool]:
    """Reject ambiguous CSV schemas before DictReader can collapse values."""

    normalized = [str(item or "").strip() for item in (fieldnames or ())]
    headers = frozenset(normalized)
    if any(not header for header in normalized):
        issues.append(
            _issue(
                "OR-HEADER-BLANK",
                f"{canonical_name} contains a blank CSV header.",
                canonical_name[:-4],
            )
        )
        return headers, False

    seen: set[str] = set()
    duplicates: set[str] = set()
    for header in normalized:
        key = header.casefold()
        if key in seen:
            duplicates.add(header)
        seen.add(key)
    if duplicates:
        issues.append(
            _issue(
                "OR-HEADER-DUPLICATE",
                (
                    f"{canonical_name} contains duplicate CSV headers: "
                    f"{', '.join(sorted(duplicates))}."
                ),
                canonical_name[:-4],
            )
        )
        return headers, False
    return headers, True


def _read_rows(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    canonical_name: str,
    issues: list[ImportIssue],
    limits: SafetyLimits,
) -> tuple[list[tuple[int, dict[str, str]]], frozenset[str]]:
    try:
        raw = archive.open(info, "r")
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="strict", newline="")
        with raw, text:
            reader = csv.DictReader(
                _BoundedTextLines(text, limits.max_csv_line_chars)
            )
            headers, valid_headers = _validate_headers(
                reader.fieldnames,
                canonical_name,
                issues,
            )
            if not valid_headers:
                return [], headers
            missing = REQUIRED_HEADERS.get(canonical_name, frozenset()) - headers
            if canonical_name == "classes.csv" and not (
                {"terms", "termSourcedIds"} & headers
            ):
                missing = missing | {"terms"}
            if missing:
                issues.append(
                    _issue(
                        "OR-HEADER-MISSING",
                        f"{canonical_name} is missing required headers: {', '.join(sorted(missing))}.",
                        canonical_name[:-4],
                    )
                )
                return [], headers
            rows: list[tuple[int, dict[str, str]]] = []
            for number, row in enumerate(reader, start=2):
                if number - 1 > limits.max_manifest_rows:
                    issues.append(
                        _issue(
                            "OR-RECORD-LIMIT",
                            (
                                f"{canonical_name} exceeds the configured "
                                "record-count limit."
                            ),
                            canonical_name[:-4],
                            row_number=number,
                        )
                    )
                    return rows, headers
                if None in row:
                    issues.append(
                        _issue(
                            "OR-CSV-COLUMN-COUNT",
                            f"{canonical_name} row {number} has unexpected extra columns.",
                            canonical_name[:-4],
                            row_number=number,
                        )
                    )
                    continue
                normalized = {
                    str(key): str(value or "").strip()
                    for key, value in row.items()
                    if key is not None
                }
                _require_bounded_fields(normalized, limits)
                rows.append(
                    (
                        number,
                        normalized,
                    )
                )
            return rows, headers
    except _CSVLimitError:
        issues.append(
            _issue(
                "OR-CSV-LIMIT",
                f"{canonical_name} contains a line or field that exceeds the safe import limit.",
                canonical_name[:-4],
            )
        )
        return [], frozenset()
    except (UnicodeDecodeError, csv.Error, RuntimeError, zipfile.BadZipFile, OSError):
        issues.append(
            _issue(
                "OR-CSV-INVALID",
                f"{canonical_name} could not be decoded as UTF-8 CSV.",
                canonical_name[:-4],
            )
        )
        return [], frozenset()


def _load_all_tables(
    conn: sqlite3.Connection,
    archive: zipfile.ZipFile,
    members: Mapping[str, zipfile.ZipInfo],
    domain: str,
    issues: list[ImportIssue],
    limits: SafetyLimits,
) -> None:
    loaders = (
        ("academicSessions.csv", _insert_sessions),
        ("orgs.csv", _insert_orgs),
        ("users.csv", _insert_users),
        ("courses.csv", _insert_courses),
        ("classes.csv", _insert_classes),
        ("enrollments.csv", _insert_enrollments),
    )
    for name, loader in loaders:
        info = members.get(name.casefold())
        if info is None:
            continue
        _stream_table(
            conn,
            archive,
            info,
            name,
            domain,
            loader,
            issues,
            limits,
        )


def _stream_table(
    conn: sqlite3.Connection,
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    canonical_name: str,
    domain: str,
    loader: object,
    issues: list[ImportIssue],
    limits: SafetyLimits,
) -> None:
    """Decode one CSV incrementally and insert each accepted row immediately."""

    try:
        raw = archive.open(info, "r")
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="strict", newline="")
        with raw, text:
            reader = csv.DictReader(
                _BoundedTextLines(text, limits.max_csv_line_chars)
            )
            headers, valid_headers = _validate_headers(
                reader.fieldnames,
                canonical_name,
                issues,
            )
            if not valid_headers:
                return
            missing = REQUIRED_HEADERS.get(canonical_name, frozenset()) - headers
            if canonical_name == "classes.csv" and not (
                {"terms", "termSourcedIds"} & headers
            ):
                missing = missing | {"terms"}
            if missing:
                issues.append(
                    _issue(
                        "OR-HEADER-MISSING",
                        f"{canonical_name} is missing required headers: "
                        f"{', '.join(sorted(missing))}.",
                        canonical_name[:-4],
                    )
                )
                return
            for number, row in enumerate(reader, start=2):
                if number - 1 > limits.max_records_per_file:
                    issues.append(
                        _issue(
                            "OR-RECORD-LIMIT",
                            (
                                f"{canonical_name} exceeds the configured "
                                "record-count limit."
                            ),
                            canonical_name[:-4],
                            row_number=number,
                        )
                    )
                    return
                if None in row:
                    issues.append(
                        _issue(
                            "OR-CSV-COLUMN-COUNT",
                            f"{canonical_name} row {number} has unexpected extra columns.",
                            canonical_name[:-4],
                            row_number=number,
                        )
                    )
                    continue
                normalized = {
                    str(key): str(value or "").strip()
                    for key, value in row.items()
                    if key is not None
                }
                _require_bounded_fields(normalized, limits)
                if not _valid_status(
                    normalized,
                    canonical_name[:-4],
                    number,
                    issues,
                ):
                    continue
                loader(conn, domain, number, normalized, issues)  # type: ignore[operator]
    except _CSVLimitError:
        issues.append(
            _issue(
                "OR-CSV-LIMIT",
                f"{canonical_name} contains a line or field that exceeds the safe import limit.",
                canonical_name[:-4],
            )
        )
    except (UnicodeDecodeError, csv.Error, RuntimeError, zipfile.BadZipFile, OSError):
        issues.append(
            _issue(
                "OR-CSV-INVALID",
                f"{canonical_name} could not be decoded as UTF-8 CSV.",
                canonical_name[:-4],
            )
        )


def _insert_sessions(
    conn: sqlite3.Connection,
    domain: str,
    row_number: int,
    row: Mapping[str, str],
    issues: list[ImportIssue],
) -> None:
    source_id = row.get("sourcedId", "")
    if not _valid_source_id(source_id, "academicSession", row_number, issues):
        return
    if not _insert_unique(
        conn,
        """
        INSERT INTO academic_sessions (
            domain, sourced_id, status, title, session_type, start_date,
            end_date, parent_id, school_year, row_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            domain,
            source_id,
            _status(row),
            row.get("title", ""),
            row.get("type", ""),
            row.get("startDate", ""),
            row.get("endDate", ""),
            row.get("parentSourcedId", ""),
            row.get("schoolYear", ""),
            row_number,
        ),
    ):
        _duplicate_issue("academicSession", source_id, row_number, issues)


def _insert_orgs(
    conn: sqlite3.Connection,
    domain: str,
    row_number: int,
    row: Mapping[str, str],
    issues: list[ImportIssue],
) -> None:
    source_id = row.get("sourcedId", "")
    if not _valid_source_id(source_id, "org", row_number, issues):
        return
    if not _insert_unique(
        conn,
        """
        INSERT INTO orgs (
            domain, sourced_id, status, name, org_type, parent_id, row_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            domain,
            source_id,
            _status(row),
            row.get("name", ""),
            row.get("type", ""),
            row.get("parentSourcedId", ""),
            row_number,
        ),
    ):
        _duplicate_issue("org", source_id, row_number, issues)


def _insert_users(
    conn: sqlite3.Connection,
    domain: str,
    row_number: int,
    row: Mapping[str, str],
    issues: list[ImportIssue],
) -> None:
    source_id = row.get("sourcedId", "")
    if not _valid_source_id(source_id, "user", row_number, issues):
        return
    if not _insert_unique(
        conn,
        """
        INSERT INTO users (
            domain, sourced_id, status, username, email, given_name,
            family_name, identifier, org_ids, row_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            domain,
            source_id,
            _status(row),
            row.get("username", ""),
            row.get("email", "").casefold(),
            row.get("givenName", ""),
            row.get("familyName", ""),
            row.get("identifier", ""),
            row.get("orgSourcedIds", ""),
            row_number,
        ),
    ):
        _duplicate_issue("user", source_id, row_number, issues)


def _insert_courses(
    conn: sqlite3.Connection,
    domain: str,
    row_number: int,
    row: Mapping[str, str],
    issues: list[ImportIssue],
) -> None:
    source_id = row.get("sourcedId", "")
    if not _valid_source_id(source_id, "course", row_number, issues):
        return
    if not _insert_unique(
        conn,
        """
        INSERT INTO courses (
            domain, sourced_id, status, title, school_year_id,
            org_id, grades, row_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            domain,
            source_id,
            _status(row),
            row.get("title", ""),
            row.get("schoolYearSourcedId", ""),
            row.get("orgSourcedId", ""),
            row.get("grades", ""),
            row_number,
        ),
    ):
        _duplicate_issue("course", source_id, row_number, issues)


def _insert_classes(
    conn: sqlite3.Connection,
    domain: str,
    row_number: int,
    row: Mapping[str, str],
    issues: list[ImportIssue],
) -> None:
    source_id = row.get("sourcedId", "")
    if not _valid_source_id(source_id, "class", row_number, issues):
        return
    terms = row.get("terms", "") or row.get("termSourcedIds", "")
    if not _insert_unique(
        conn,
        """
        INSERT INTO classes (
            domain, sourced_id, status, title, class_code, location,
            course_id, term_ids, school_id, grades, row_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            domain,
            source_id,
            _status(row),
            row.get("title", ""),
            row.get("classCode", ""),
            row.get("location", ""),
            row.get("courseSourcedId", ""),
            terms,
            row.get("schoolSourcedId", ""),
            row.get("grades", ""),
            row_number,
        ),
    ):
        _duplicate_issue("class", source_id, row_number, issues)


def _insert_enrollments(
    conn: sqlite3.Connection,
    domain: str,
    row_number: int,
    row: Mapping[str, str],
    issues: list[ImportIssue],
) -> None:
    source_id = row.get("sourcedId", "")
    if not _valid_source_id(source_id, "enrollment", row_number, issues):
        return
    role = row.get("role", "").casefold()
    if role not in {"teacher", "student"}:
        issues.append(
            _issue(
                "OR-ENROLLMENT-ROLE",
                "Enrollment role must be teacher or student.",
                "enrollment",
                source_id,
                row_number,
                blocking=False,
            )
        )
    if not _insert_unique(
        conn,
        """
        INSERT INTO enrollments (
            domain, sourced_id, status, class_id, school_id, user_id,
            role, is_primary, begin_date, end_date, row_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            domain,
            source_id,
            _status(row),
            row.get("classSourcedId", ""),
            row.get("schoolSourcedId", ""),
            row.get("userSourcedId", ""),
            role,
            1 if _truthy(row.get("primary", "")) else 0,
            row.get("beginDate", ""),
            row.get("endDate", ""),
            row_number,
        ),
    ):
        _duplicate_issue("enrollment", source_id, row_number, issues)


def _validate_references(
    conn: sqlite3.Connection,
    domain: str,
    issues: list[ImportIssue],
) -> None:
    for row in conn.execute(
        """
        SELECT e.sourced_id, e.class_id, e.user_id, e.row_number,
               c.sourced_id AS found_class, u.sourced_id AS found_user
        FROM enrollments e
        LEFT JOIN classes c ON c.domain = e.domain AND c.sourced_id = e.class_id
                           AND c.status != 'tobedeleted'
        LEFT JOIN users u ON u.domain = e.domain AND u.sourced_id = e.user_id
                         AND u.status != 'tobedeleted'
        WHERE e.domain = ? AND e.status != 'tobedeleted'
          AND (c.sourced_id IS NULL OR u.sourced_id IS NULL)
        """,
        (domain,),
    ):
        missing = "class" if row["found_class"] is None else "user"
        issues.append(
            _issue(
                "OR-REFERENCE-ENROLLMENT",
                f"Enrollment references a missing {missing}.",
                "enrollment",
                row["sourced_id"],
                row["row_number"],
                # A missing user can quarantine the class named by the row.
                # A missing class cannot be attributed safely, so exact-state
                # reconciliation must stop rather than infer removals elsewhere.
                blocking=row["found_class"] is None,
            )
        )
    for row in conn.execute(
        """
        SELECT sourced_id, row_number FROM users
        WHERE domain = ? AND status != 'tobedeleted' AND trim(email) = ''
        """,
        (domain,),
    ):
        issues.append(
            _issue(
                "OR-USER-EMAIL-MISSING",
                "Active OneRoster user has no email address.",
                "user",
                row["sourced_id"],
                row["row_number"],
                blocking=False,
            )
        )
def _choose_current_session(
    conn: sqlite3.Connection,
    domain: str,
    today: date,
    issues: list[ImportIssue],
) -> str:
    referenced: set[str] = set()
    for row in conn.execute(
        "SELECT term_ids FROM classes WHERE domain = ? AND status != 'tobedeleted'",
        (domain,),
    ):
        referenced.update(_split_ids(row["term_ids"]))

    candidates: list[sqlite3.Row] = []
    for row in conn.execute(
        """
        SELECT * FROM academic_sessions
        WHERE domain = ? AND status != 'tobedeleted'
        """,
        (domain,),
    ):
        if row["sourced_id"] not in referenced:
            continue
        try:
            start = date.fromisoformat(str(row["start_date"]))
            end = date.fromisoformat(str(row["end_date"]))
        except ValueError:
            issues.append(
                _issue(
                    "OR-SESSION-DATE",
                    "Academic session has an invalid ISO date.",
                    "academicSession",
                    row["sourced_id"],
                    row["row_number"],
                )
            )
            continue
        if start <= today <= end:
            candidates.append(row)

    non_year = [
        row
        for row in candidates
        if str(row["session_type"]).casefold().replace(" ", "")
        not in {"schoolyear", "academicyear"}
    ]
    if non_year:
        candidates = non_year
    if len(candidates) == 1:
        return str(candidates[0]["sourced_id"])
    issues.append(
        _issue(
            "OR-TERM-AMBIGUOUS",
            "Select the academic session for this import before it can be applied.",
            "academicSession",
        )
    )
    return ""


def _build_course_plans(
    conn: sqlite3.Connection,
    domain: str,
    selected_session_id: str,
    issues: list[ImportIssue],
) -> None:
    conn.execute("DELETE FROM course_plans WHERE domain = ?", (domain,))
    aliases: dict[str, list[str]] = defaultdict(list)
    class_rows = conn.execute(
        """
        SELECT c.*, x.title AS course_title, x.school_year_id,
               y.school_year AS course_school_year,
               s.school_year AS selected_school_year
        FROM classes c
        LEFT JOIN courses x ON x.domain = c.domain AND x.sourced_id = c.course_id
                           AND x.status != 'tobedeleted'
        LEFT JOIN academic_sessions y
          ON y.domain = x.domain AND y.sourced_id = x.school_year_id
         AND y.status != 'tobedeleted'
        LEFT JOIN academic_sessions s
          ON s.domain = c.domain AND s.sourced_id = ?
         AND s.status != 'tobedeleted'
        WHERE c.domain = ? AND c.status != 'tobedeleted'
        ORDER BY c.sourced_id
        """,
        (selected_session_id, domain),
    ).fetchall()
    for row in class_rows:
        aliases[section_alias(row["sourced_id"]).casefold()].append(row["sourced_id"])
    known_sessions = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT sourced_id FROM academic_sessions
            WHERE domain = ? AND status != 'tobedeleted'
            """,
            (domain,),
        )
    }
    participant_stats = {
        str(row["class_id"]): row
        for row in conn.execute(
            """
            SELECT
                e.class_id,
                SUM(CASE WHEN e.role = 'teacher' THEN 1 ELSE 0 END) AS teacher_count,
                SUM(CASE WHEN e.role = 'student' THEN 1 ELSE 0 END) AS student_count,
                SUM(CASE WHEN e.role NOT IN ('teacher', 'student') THEN 1 ELSE 0 END)
                    AS unsupported_count,
                SUM(CASE WHEN trim(COALESCE(u.email, '')) = '' THEN 1 ELSE 0 END)
                    AS missing_user_count,
                SUM(
                    CASE
                        WHEN instr(COALESCE(u.email, ''), char(10)) > 0
                          OR instr(COALESCE(u.email, ''), char(13)) > 0
                          OR instr(COALESCE(u.email, ''), char(0)) > 0
                        THEN 1 ELSE 0
                    END
                ) AS invalid_gam_value_count,
                SUM(
                    CASE
                        WHEN e.role = 'teacher' AND e.is_primary = 1
                             AND trim(COALESCE(u.email, '')) != ''
                        THEN 1 ELSE 0
                    END
                ) AS primary_count,
                MAX(
                    CASE
                        WHEN e.role = 'teacher' AND e.is_primary = 1
                             AND trim(COALESCE(u.email, '')) != ''
                        THEN lower(trim(u.email)) ELSE ''
                    END
                ) AS primary_email
            FROM enrollments e
            LEFT JOIN users u
              ON u.domain = e.domain AND u.sourced_id = e.user_id
             AND u.status != 'tobedeleted'
            WHERE e.domain = ? AND e.status != 'tobedeleted'
            GROUP BY e.class_id
            """,
            (domain,),
        )
    }

    for row in class_rows:
        class_id = str(row["sourced_id"])
        alias = section_alias(class_id)
        term_ids = _split_ids(row["term_ids"])
        selected = bool(selected_session_id) and selected_session_id in term_ids
        codes: list[str] = []
        if not selected_session_id:
            codes.append("OR-TERM-AMBIGUOUS")
        if not term_ids or any(item not in known_sessions for item in term_ids):
            codes.append("OR-REFERENCE-TERM")
        if row["course_title"] is None:
            codes.append("OR-REFERENCE-COURSE")
        elif (
            str(row["school_year_id"] or "").strip()
            and row["course_school_year"] is None
        ):
            codes.append("OR-REFERENCE-SCHOOL-YEAR")
        if not alias:
            codes.append("OR-ALIAS-MISSING")
        elif len(aliases[alias.casefold()]) > 1:
            codes.append("OR-ALIAS-COLLISION")

        stats = participant_stats.get(class_id)
        teacher_count = int(stats["teacher_count"] or 0) if stats is not None else 0
        student_count = int(stats["student_count"] or 0) if stats is not None else 0
        primary_count = int(stats["primary_count"] or 0) if stats is not None else 0
        if stats is not None and int(stats["unsupported_count"] or 0):
            codes.append("OR-ENROLLMENT-ROLE")
        if not teacher_count:
            codes.append("OR-OWNER-MISSING")
        elif primary_count != 1:
            codes.append("OR-OWNER-AMBIGUOUS")
        if stats is not None and int(stats["missing_user_count"] or 0):
            codes.append("OR-USER-MISSING")

        school_year = str(
            row["course_school_year"] or row["selected_school_year"] or ""
        ).strip()
        name = course_display_name(
            str(row["course_title"] or ""),
            str(row["class_code"] or ""),
            str(row["title"] or ""),
            school_year,
        )
        if not name:
            codes.append("OR-COURSE-NAME-INCOMPLETE")
        owner_email = (
            str(stats["primary_email"] or "").casefold()
            if stats is not None and primary_count == 1
            else ""
        )
        if (
            any(
                _has_gam_control(value)
                for value in (
                    alias,
                    name,
                    str(row["class_code"] or ""),
                    str(row["location"] or ""),
                    owner_email,
                )
            )
            or (
                stats is not None
                and int(stats["invalid_gam_value_count"] or 0)
            )
        ):
            codes.append("OR-GAM-VALUE-INVALID")

        codes = sorted(set(codes))
        ready = int(selected and not codes)
        conn.execute(
            """
            INSERT INTO course_plans (
                domain, class_id, alias, name, section, room, owner_email,
                selected, ready, quarantine_json, teacher_count, student_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain,
                class_id,
                alias,
                name,
                str(row["class_code"] or ""),
                str(row["location"] or ""),
                owner_email,
                int(selected),
                ready,
                json.dumps(codes, separators=(",", ":")),
                teacher_count,
                student_count,
            ),
        )
        if selected:
            for code in codes:
                if code in {"OR-TERM-AMBIGUOUS"}:
                    continue
                issues.append(
                    _issue(
                        code,
                        _quarantine_message(code),
                        "class",
                        class_id,
                        int(row["row_number"]),
                        blocking=code == "OR-GAM-VALUE-INVALID",
                    )
                )


def _snapshot_counts(conn: sqlite3.Connection, domain: str) -> SnapshotCounts:
    def count(table: str, where: str = "") -> int:
        suffix = f" AND {where}" if where else ""
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE domain = ?{suffix}",
                (domain,),
            ).fetchone()[0]
        )

    participant_counts = conn.execute(
        """
        SELECT
          SUM(CASE WHEN role = 'teacher' THEN 1 ELSE 0 END),
          SUM(CASE WHEN role = 'student' THEN 1 ELSE 0 END)
        FROM enrollments WHERE domain = ? AND status != 'tobedeleted'
        """,
        (domain,),
    ).fetchone()
    return SnapshotCounts(
        users=count("users"),
        classes=count("classes"),
        courses=count("courses"),
        enrollments=count("enrollments"),
        academic_sessions=count("academic_sessions"),
        orgs=count("orgs"),
        ready_courses=count("course_plans", "ready = 1"),
        quarantined_courses=count("course_plans", "selected = 1 AND ready = 0"),
        teachers=int(participant_counts[0] or 0),
        students=int(participant_counts[1] or 0),
    )


def _create_normalized_db(path: Path) -> None:
    path.unlink(missing_ok=True)
    _prepare_private_database(path)
    with closing(_normalized_conn(path)) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE academic_sessions (
                domain TEXT NOT NULL, sourced_id TEXT NOT NULL, status TEXT NOT NULL,
                title TEXT NOT NULL, session_type TEXT NOT NULL, start_date TEXT NOT NULL,
                end_date TEXT NOT NULL, parent_id TEXT NOT NULL, school_year TEXT NOT NULL,
                row_number INTEGER NOT NULL, PRIMARY KEY(domain, sourced_id)
            );
            CREATE TABLE orgs (
                domain TEXT NOT NULL, sourced_id TEXT NOT NULL, status TEXT NOT NULL,
                name TEXT NOT NULL, org_type TEXT NOT NULL, parent_id TEXT NOT NULL,
                row_number INTEGER NOT NULL, PRIMARY KEY(domain, sourced_id)
            );
            CREATE TABLE users (
                domain TEXT NOT NULL, sourced_id TEXT NOT NULL, status TEXT NOT NULL,
                username TEXT NOT NULL, email TEXT NOT NULL, given_name TEXT NOT NULL,
                family_name TEXT NOT NULL, identifier TEXT NOT NULL, org_ids TEXT NOT NULL,
                row_number INTEGER NOT NULL, PRIMARY KEY(domain, sourced_id)
            );
            CREATE TABLE courses (
                domain TEXT NOT NULL, sourced_id TEXT NOT NULL, status TEXT NOT NULL,
                title TEXT NOT NULL, school_year_id TEXT NOT NULL, org_id TEXT NOT NULL,
                grades TEXT NOT NULL, row_number INTEGER NOT NULL,
                PRIMARY KEY(domain, sourced_id)
            );
            CREATE TABLE classes (
                domain TEXT NOT NULL, sourced_id TEXT NOT NULL, status TEXT NOT NULL,
                title TEXT NOT NULL, class_code TEXT NOT NULL, location TEXT NOT NULL,
                course_id TEXT NOT NULL, term_ids TEXT NOT NULL, school_id TEXT NOT NULL,
                grades TEXT NOT NULL, row_number INTEGER NOT NULL,
                PRIMARY KEY(domain, sourced_id)
            );
            CREATE TABLE enrollments (
                domain TEXT NOT NULL, sourced_id TEXT NOT NULL, status TEXT NOT NULL,
                class_id TEXT NOT NULL, school_id TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT NOT NULL, is_primary INTEGER NOT NULL, begin_date TEXT NOT NULL,
                end_date TEXT NOT NULL, row_number INTEGER NOT NULL,
                PRIMARY KEY(domain, sourced_id)
            );
            CREATE TABLE course_plans (
                domain TEXT NOT NULL, class_id TEXT NOT NULL, alias TEXT NOT NULL,
                name TEXT NOT NULL, section TEXT NOT NULL, room TEXT NOT NULL,
                owner_email TEXT NOT NULL, selected INTEGER NOT NULL, ready INTEGER NOT NULL,
                quarantine_json TEXT NOT NULL, teacher_count INTEGER NOT NULL,
                student_count INTEGER NOT NULL, PRIMARY KEY(domain, class_id)
            );
            CREATE TABLE issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL,
                severity TEXT NOT NULL, message TEXT NOT NULL, entity_kind TEXT NOT NULL,
                source_id TEXT NOT NULL, row_number INTEGER, blocking INTEGER NOT NULL
            );
            CREATE INDEX enrollments_class_role ON enrollments(domain, class_id, role);
            CREATE INDEX users_domain_email ON users(domain, email COLLATE NOCASE);
            CREATE INDEX plans_search ON course_plans(domain, selected, ready, name COLLATE NOCASE);
            CREATE INDEX issues_search ON issues(code, entity_kind, source_id);
            """
        )
    _chmod_sqlite(path)


def _normalized_conn(path: Path) -> sqlite3.Connection:
    path = Path(path)
    _prepare_private_database(path)
    before = path.lstat()
    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        after = path.lstat()
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or not stat.S_ISREG(after.st_mode)
        ):
            raise PermissionError(
                "OneRoster normalized persistence changed during open."
            )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _chmod_sqlite(path)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn
    except BaseException:
        conn.close()
        raise


def _replace_issues(
    path: Path,
    issues: Sequence[ImportIssue],
    domain: str,
) -> None:
    with closing(_normalized_conn(path)) as conn, conn:
        _write_issues(conn, issues)
        rebuild_preview_index(conn, domain)
    _chmod_sqlite(path)


def _write_issues(conn: sqlite3.Connection, issues: Sequence[ImportIssue]) -> None:
    conn.execute("DELETE FROM issues")
    seen: set[tuple[object, ...]] = set()
    for issue in issues:
        key = (
            issue.code,
            issue.severity.value,
            issue.message,
            issue.entity_kind,
            issue.source_id,
            issue.row_number,
            issue.blocking,
        )
        if key in seen:
            continue
        seen.add(key)
        conn.execute(
            """
            INSERT INTO issues (
                code, severity, message, entity_kind, source_id, row_number, blocking
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue.code,
                issue.severity.value,
                issue.message,
                issue.entity_kind,
                issue.source_id,
                issue.row_number,
                int(issue.blocking),
            ),
        )


def _issues_from_db(conn: sqlite3.Connection) -> Iterable[ImportIssue]:
    for row in conn.execute("SELECT * FROM issues ORDER BY id"):
        yield ImportIssue(
            code=str(row["code"]),
            severity=IssueSeverity(str(row["severity"])),
            message=str(row["message"]),
            entity_kind=str(row["entity_kind"]),
            source_id=str(row["source_id"]),
            row_number=int(row["row_number"]) if row["row_number"] is not None else None,
            blocking=bool(row["blocking"]),
        )


def _insert_unique(conn: sqlite3.Connection, sql: str, params: Sequence[object]) -> bool:
    try:
        conn.execute(sql, tuple(params))
        return True
    except sqlite3.IntegrityError:
        return False


def _valid_source_id(
    source_id: str,
    kind: str,
    row_number: int,
    issues: list[ImportIssue],
) -> bool:
    if source_id.strip():
        return True
    issues.append(
        _issue(
            "OR-SOURCED-ID-MISSING",
            f"{kind} row has no sourcedId.",
            kind,
            row_number=row_number,
        )
    )
    return False


def _duplicate_issue(
    kind: str,
    source_id: str,
    row_number: int,
    issues: list[ImportIssue],
) -> None:
    issues.append(
        _issue(
            "OR-SOURCED-ID-DUPLICATE",
            f"Duplicate {kind} sourcedId.",
            kind,
            source_id,
            row_number,
        )
    )


def _status(row: Mapping[str, str]) -> str:
    return str(row.get("status") or "").strip().casefold()


def _valid_status(
    row: Mapping[str, str],
    entity_kind: str,
    row_number: int,
    issues: list[ImportIssue],
) -> bool:
    status = _status(row)
    if status in {"active", "tobedeleted"}:
        return True
    issues.append(
        _issue(
            "OR-STATUS-INVALID",
            "OneRoster status must be active or tobedeleted.",
            entity_kind,
            str(row.get("sourcedId", "") or ""),
            row_number,
        )
    )
    return False


def _truthy(value: str) -> bool:
    return (value or "").strip().casefold() in {"true", "1", "yes"}


def _split_ids(value: str) -> tuple[str, ...]:
    return tuple(
        part.strip()
        for part in str(value or "").replace(";", ",").split(",")
        if part.strip()
    )


def _has_gam_control(value: object) -> bool:
    return any(character in str(value or "") for character in "\r\n\x00")


def _issue(
    code: str,
    message: str,
    entity_kind: str = "",
    source_id: str = "",
    row_number: Optional[int] = None,
    *,
    blocking: bool = True,
) -> ImportIssue:
    return ImportIssue(
        code=code,
        severity=IssueSeverity.ERROR if blocking else IssueSeverity.WARNING,
        message=message,
        entity_kind=entity_kind,
        source_id=source_id,
        row_number=row_number,
        blocking=blocking,
    )


def _quarantine_message(code: str) -> str:
    messages = {
        "OR-REFERENCE-COURSE": "Course reference is missing; class is quarantined.",
        "OR-ALIAS-MISSING": "Class has no stable Section_ alias.",
        "OR-ALIAS-COLLISION": "More than one class resolves to the same Section_ alias.",
        "OR-OWNER-MISSING": "Class has no teacher enrollment and no safe owner.",
        "OR-OWNER-AMBIGUOUS": "Class does not have exactly one resolvable primary teacher.",
        "OR-USER-MISSING": "At least one class enrollment has no resolvable user email.",
        "OR-COURSE-NAME-INCOMPLETE": "Course name cannot be built from the required OneRoster fields.",
        "OR-GAM-VALUE-INVALID": "A Classroom-bound value contains an unsafe control character.",
    }
    return messages.get(code, "Class cannot be applied until its source data is corrected.")


def _chmod(path: Path, mode: int) -> None:
    path = Path(path)
    if mode not in {0o600, 0o700}:
        raise ValueError("OneRoster ingestion mode must be owner-only.")
    metadata = path.lstat()
    expected_type = stat.S_ISDIR if mode == 0o700 else stat.S_ISREG
    if path.is_symlink() or not expected_type(metadata.st_mode):
        raise PermissionError("OneRoster ingestion path has an unsafe type.")
    os.chmod(path, mode)
    verified = path.lstat()
    getuid = getattr(os, "getuid", None)
    if path.is_symlink() or not expected_type(verified.st_mode):
        raise PermissionError("OneRoster ingestion path changed type.")
    if callable(getuid) and int(verified.st_uid) != int(getuid()):
        raise PermissionError("OneRoster ingestion path is not owned by this user.")
    if os.name == "posix" and stat.S_IMODE(verified.st_mode) != mode:
        raise PermissionError("OneRoster ingestion path is not owner-only.")


def _prepare_private_database(path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod(path.parent, 0o700)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except FileExistsError:
        _chmod(path, 0o600)
        return
    try:
        fchmod = getattr(os, "fchmod", None)
        if callable(fchmod):
            fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    _chmod(path, 0o600)


def _chmod_sqlite(path: Path) -> None:
    _chmod(path.parent, 0o700)
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.is_symlink() or candidate.exists():
            _chmod(candidate, 0o600)
