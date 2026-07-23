"""Directory insight reports — the read-only "stuff the Admin Console buries" view.

The legacy pure classifier remains available for small callers. The web surface uses aggregate
and paged queries over :class:`DirectoryIndex`, so it never repeats the full tenant in the DOM.
"""

from __future__ import annotations

import base64
import binascii
import heapq
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .directory_index import MAX_PAGE_SIZE, DirectoryIndex, Page
from .gam.models import GAMUser

# Fields the report needs from `gam print users`.
REPORT_FIELDS = (
    "primaryEmail", "name", "suspended", "isAdmin", "isDelegatedAdmin",
    "isEnrolledIn2Sv", "lastLoginTime", "orgUnitPath", "recoveryEmail",
)

INACTIVE_DAYS = 90

# Usage-report parameters (Admin SDK reports API). Data lags ~2-3 days.
USAGE_PARAMS = (
    "accounts:used_quota_in_mb",
    "gmail:num_emails_received",
    "gmail:num_emails_sent",
    "drive:num_items_created",
)


@dataclass
class UsageRow:
    email: str
    quota_mb: int
    storage_gb: float
    received: int
    sent: int
    drive_created: int


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _usage_row(raw: Mapping[str, Any]) -> UsageRow:
    mb = _int(raw.get("accounts:used_quota_in_mb"))
    return UsageRow(
        email=str(raw.get("email", ""))[:320],
        quota_mb=mb,
        storage_gb=round(mb / 1024, 1),
        received=_int(raw.get("gmail:num_emails_received")),
        sent=_int(raw.get("gmail:num_emails_sent")),
        drive_created=_int(raw.get("drive:num_items_created")),
    )


def parse_usage(
    rows: Iterable[Mapping[str, Any]],
    *,
    limit: Optional[int] = None,
) -> List[UsageRow]:
    """Turn usage rows into a storage-ranked result without requiring a full-list sort.

    The web route supplies ``limit=25``. ``heapq.nlargest`` then retains only that bounded
    working set even when the connector is fed a district-wide report.
    """

    parsed = (_usage_row(row) for row in rows)
    if limit is not None:
        return heapq.nlargest(max(0, int(limit)), parsed, key=lambda row: row.quota_mb)
    return sorted(parsed, key=lambda row: row.quota_mb, reverse=True)


@dataclass
class Report:
    key: str
    title: str
    description: str
    users: List[GAMUser]

    @property
    def count(self) -> int:
        return len(self.users)


@dataclass(frozen=True)
class ReportSummary:
    """A count-only directory finding safe to render on the initial page."""

    key: str
    title: str
    description: str
    count: int


@dataclass(frozen=True)
class _IndexedReportDefinition:
    title: str
    description: str
    predicate: str


# These findings intentionally use only the minimal fields already held by DirectoryIndex.
# Recovery addresses, phone numbers, and locations remain live per-user detail fields and are
# deliberately not copied into the tenant-wide index.
_INDEXED_REPORTS: Dict[str, _IndexedReportDefinition] = {
    "no_2sv": _IndexedReportDefinition(
        "No 2-step verification",
        "Active users not enrolled in 2SV — a real security gap.",
        "u.suspended = 0 AND u.enrolled_2sv = 0",
    ),
    "inactive": _IndexedReportDefinition(
        f"Inactive ({INACTIVE_DAYS}+ days)",
        "Active users with no recent (or any) login.",
        "u.suspended = 0 AND COALESCE("
        "datetime(NULLIF(TRIM(u.last_login_time), '')), datetime('1970-01-01')) < datetime(?)",
    ),
    "admins": _IndexedReportDefinition(
        "Administrators",
        "Active accounts with super or delegated admin privileges.",
        "u.suspended = 0 AND (u.is_admin = 1 OR u.is_delegated_admin = 1)",
    ),
    "suspended": _IndexedReportDefinition(
        "Suspended",
        "Accounts currently suspended (sign-in blocked).",
        "u.suspended = 1",
    ),
    "no_title": _IndexedReportDefinition(
        "No job title",
        "Active users with no title set — needed for role-based signatures.",
        "u.suspended = 0 AND TRIM(u.title) = ''",
    ),
    "no_department": _IndexedReportDefinition(
        "No department",
        "Active users with no department set.",
        "u.suspended = 0 AND TRIM(u.department) = ''",
    ),
}


def indexed_report_definition(key: str) -> Optional[ReportSummary]:
    """Return public metadata for an indexed report key, without querying the index."""

    definition = _INDEXED_REPORTS.get((key or "").strip())
    if definition is None:
        return None
    return ReportSummary(key, definition.title, definition.description, 0)


def _cutoff(now: Optional[datetime], inactive_days: int) -> str:
    value = (now or datetime.now(timezone.utc)) - timedelta(days=inactive_days)
    return value.astimezone(timezone.utc).isoformat()


def _predicate(
    key: str,
    *,
    now: Optional[datetime] = None,
    inactive_days: int = INACTIVE_DAYS,
) -> Tuple[str, Tuple[str, ...]]:
    definition = _INDEXED_REPORTS.get(key)
    if definition is None:
        raise ValueError("unknown report")
    params: Tuple[str, ...] = ()
    if key == "inactive":
        params = (_cutoff(now, inactive_days),)
    return definition.predicate, params


def _open_index(index: DirectoryIndex) -> sqlite3.Connection:
    conn = sqlite3.connect(index.path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def indexed_report_summaries(
    index: DirectoryIndex,
    *,
    now: Optional[datetime] = None,
    inactive_days: int = INACTIVE_DAYS,
) -> List[ReportSummary]:
    """Return counts without materialising a tenant-wide list of :class:`GAMUser` objects."""

    cutoff = _cutoff(now, inactive_days)
    expressions = []
    params: List[str] = []
    for key, definition in _INDEXED_REPORTS.items():
        predicate = definition.predicate
        if key == "inactive":
            params.append(cutoff)
        expressions.append(f"SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) AS {key}")
    sql = "SELECT " + ", ".join(expressions) + " FROM users u WHERE u.domain = ?"
    # SQL binds the aggregate expressions before the final domain predicate.
    params.append(index.domain)
    with closing(_open_index(index)) as conn:
        row = conn.execute(sql, params).fetchone()
    return [
        ReportSummary(
            key,
            definition.title
            if key != "inactive"
            else f"Inactive ({inactive_days}+ days)",
            definition.description,
            int(row[key] or 0),
        )
        for key, definition in _INDEXED_REPORTS.items()
    ]


def _encode_report_cursor(key: str, offset: int, generation: str) -> str:
    payload = json.dumps(
        {
            "v": 2,
            "report": key,
            "offset": max(0, int(offset)),
            "generation": generation,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_report_cursor(
    key: str,
    cursor: Optional[str],
    generation: str,
) -> int:
    if not cursor:
        return 0
    if len(cursor) > 512:
        raise ValueError("invalid report page cursor")
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 2
            or payload.get("report") != key
            or payload.get("generation") != generation
        ):
            raise ValueError
        offset = int(payload["offset"])
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("invalid report page cursor") from exc
    if offset < 0:
        raise ValueError("invalid report page cursor")
    return offset


def _snapshot_generation(conn: sqlite3.Connection, index: DirectoryIndex) -> str:
    row = conn.execute(
        "SELECT updated_at FROM snapshots WHERE domain = ? AND kind = 'users'",
        (index.domain,),
    ).fetchone()
    if row is None or float(row[0]) <= 0:
        raise ValueError("directory user snapshot is unavailable")
    return repr(float(row[0]))


def _user_from_index_row(row: sqlite3.Row) -> GAMUser:
    # Existing index files may predate ingestion bounds. Clamp again at this response boundary so
    # one hostile or malformed Directory value cannot break the 100 KB list-response contract.
    def text(name: str, limit: int) -> str:
        return str(row[name] or "")[:limit]

    return GAMUser(
        primary_email=text("primary_email", 254),
        given_name=text("given_name", 100),
        family_name=text("family_name", 100),
        suspended=bool(row["suspended"]),
        org_unit_path=text("org_unit_path", 256),
        is_admin=bool(row["is_admin"]),
        is_delegated_admin=bool(row["is_delegated_admin"]),
        enrolled_2sv=bool(row["enrolled_2sv"]),
        title=text("title", 128),
        department=text("department", 128),
        last_login_time=text("last_login_time", 64) or None,
    )


def indexed_report_page(
    index: DirectoryIndex,
    key: str,
    *,
    limit: int = MAX_PAGE_SIZE,
    cursor: Optional[str] = None,
    now: Optional[datetime] = None,
    inactive_days: int = INACTIVE_DAYS,
) -> Page[GAMUser]:
    """Return one hard-bounded report page from the local directory snapshot."""

    key = (key or "").strip()
    predicate, predicate_params = _predicate(
        key, now=now, inactive_days=inactive_days
    )
    limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    where_params = (index.domain, *predicate_params)
    with closing(_open_index(index)) as conn:
        # Pin the generation read, count, and page rows to one WAL snapshot. A refresh that commits
        # afterward gets a new generation and invalidates this page's continuation cursor.
        conn.execute("BEGIN")
        generation = _snapshot_generation(conn, index)
        offset = _decode_report_cursor(key, cursor, generation)
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM users u WHERE u.domain = ? AND ({predicate})",
                where_params,
            ).fetchone()[0]
        )
        if total and offset >= total:
            offset = ((total - 1) // limit) * limit
        rows = conn.execute(
            "SELECT u.* FROM users u WHERE u.domain = ? AND ("
            + predicate
            + ") ORDER BY u.family_name COLLATE NOCASE, u.given_name COLLATE NOCASE,"
            " u.primary_email COLLATE NOCASE LIMIT ? OFFSET ?",
            (*where_params, limit, offset),
        ).fetchall()
    items = [_user_from_index_row(row) for row in rows]
    next_cursor = (
        _encode_report_cursor(key, offset + limit, generation)
        if offset + len(items) < total
        else None
    )
    return Page(
        items=items,
        next_cursor=next_cursor,
        total=total,
        snapshot_age_seconds=index.snapshot_age("users"),
        refreshing=index.is_refreshing("users"),
    )


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def build_reports(users: List[GAMUser], now: Optional[datetime] = None, inactive_days: int = INACTIVE_DAYS) -> List[Report]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=inactive_days)

    no_2sv, admins, suspended, inactive, no_recovery = [], [], [], [], []
    no_title, no_dept, no_phone, no_location = [], [], [], []
    for u in users:
        if u.suspended:
            suspended.append(u)
            continue  # the buckets below describe *active* accounts
        if u.is_admin or u.is_delegated_admin:
            admins.append(u)
        if not u.enrolled_2sv:
            no_2sv.append(u)
        if not u.recovery_email:
            no_recovery.append(u)
        if not (u.title or "").strip():
            no_title.append(u)
        if not (u.department or "").strip():
            no_dept.append(u)
        if not (u.phone or "").strip():
            no_phone.append(u)
        if not (u.location or "").strip():
            no_location.append(u)
        last = _parse_dt(u.last_login_time)
        if last is None or last < cutoff:
            inactive.append(u)

    return [
        Report("no_2sv", "No 2-step verification", "Active users not enrolled in 2SV — a real security gap.", no_2sv),
        Report("inactive", f"Inactive ({inactive_days}+ days)", "Active users with no recent (or any) login.", inactive),
        Report("admins", "Administrators", "Accounts with super or delegated admin privileges.", admins),
        Report("no_recovery", "No recovery info", "Active users without a recovery email set.", no_recovery),
        Report("suspended", "Suspended", "Accounts currently suspended (sign-in blocked).", suspended),
        # Directory completeness — the worklist for filling profile data (e.g. before a signature rollout).
        Report("no_title", "No job title", "Active users with no title set — needed for role-based signatures.", no_title),
        Report("no_department", "No department", "Active users with no department set.", no_dept),
        Report("no_phone", "No phone", "Active users with no work phone set.", no_phone),
        Report("no_location", "No location", "Active users with no location/store set.", no_location),
    ]
