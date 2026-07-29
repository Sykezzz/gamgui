"""Persistent, domain-isolated directory summaries for bounded local search.

The Directory API can only produce the full user/group collection through GAM.  This index keeps
that expensive snapshot off the request path after the first refresh.  It stores only list/search
fields; detail-only data continues to come from a live ``info user`` call.
"""

from __future__ import annotations

import base64
import os
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Iterable, List, Optional, Set, TypeVar

from .gam.models import GAMGroup, GAMUser
from .paths import app_data_dir

T = TypeVar("T")

MAX_PAGE_SIZE = 50
DEFAULT_STALE_SECONDS = 15 * 60
_SCHEMA_VERSION = "1"
_KINDS = {"users", "groups"}
_MAX_EMAIL_CHARS = 254
_MAX_NAME_CHARS = 100
_MAX_ORG_UNIT_CHARS = 256
_MAX_PROFILE_SUMMARY_CHARS = 128
_MAX_TIMESTAMP_CHARS = 64


def default_index_path() -> Path:
    return app_data_dir() / "directory_index.db"


@dataclass(frozen=True)
class Page(Generic[T]):
    items: List[T]
    next_cursor: Optional[str]
    total: int
    snapshot_age_seconds: Optional[float]
    refreshing: bool


@dataclass(frozen=True)
class DirectoryStatus:
    domain: str
    users: int
    groups: int
    users_updated_at: Optional[float]
    groups_updated_at: Optional[float]


def _normalise_domain(domain: str) -> str:
    value = (domain or "").strip().lower()
    if not value:
        raise ValueError("DirectoryIndex requires a Workspace domain")
    return value


def _normalise_term(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(re.sub(r"[^\w@.+-]+", " ", text).split())


def _terms(*values: str) -> Set[str]:
    terms: Set[str] = set()
    for value in values:
        phrase = _normalise_term(value)
        if not phrase:
            continue
        terms.add(phrase)
        terms.update(part for part in phrase.split() if part)
        if "@" in phrase:
            terms.add(phrase.split("@", 1)[0])
    return terms


def _summary_text(value: object, limit: int) -> str:
    """Bound low-sensitivity list fields before they enter the persistent summary index."""

    return str(value or "")[:limit]


def _encode_cursor(offset: int) -> str:
    raw = str(max(0, offset)).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Optional[str], offset: int) -> int:
    if not cursor:
        return max(0, int(offset))
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        value = int(base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii"))
    except (ValueError, UnicodeError, base64.binascii.Error) as exc:
        raise ValueError("invalid directory page cursor") from exc
    if value < 0:
        raise ValueError("invalid directory page cursor")
    return value


class DirectoryIndex:
    """SQLite directory cache bound to exactly one Workspace domain."""

    def __init__(self, path: Path, domain: str) -> None:
        self.path = Path(path)
        self.domain = _normalise_domain(domain)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._refreshing: Set[str] = set()
        self._state_lock = threading.Lock()
        self._init()
        self._restrict_perms()

    def _restrict_perms(self) -> None:
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                if path.exists():
                    os.chmod(path, 0o600)
            except OSError:
                pass

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init(self) -> None:
        with closing(self._conn()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    domain TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (domain, kind)
                );
                CREATE TABLE IF NOT EXISTS users (
                    domain TEXT NOT NULL,
                    primary_email TEXT NOT NULL,
                    given_name TEXT NOT NULL,
                    family_name TEXT NOT NULL,
                    suspended INTEGER NOT NULL,
                    org_unit_path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    department TEXT NOT NULL,
                    is_admin INTEGER NOT NULL,
                    is_delegated_admin INTEGER NOT NULL,
                    enrolled_2sv INTEGER NOT NULL,
                    last_login_time TEXT,
                    PRIMARY KEY (domain, primary_email)
                );
                CREATE TABLE IF NOT EXISTS users_stage (
                    domain TEXT NOT NULL,
                    primary_email TEXT NOT NULL,
                    given_name TEXT NOT NULL,
                    family_name TEXT NOT NULL,
                    suspended INTEGER NOT NULL,
                    org_unit_path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    department TEXT NOT NULL,
                    is_admin INTEGER NOT NULL,
                    is_delegated_admin INTEGER NOT NULL,
                    enrolled_2sv INTEGER NOT NULL,
                    last_login_time TEXT,
                    PRIMARY KEY (domain, primary_email)
                );
                CREATE TABLE IF NOT EXISTS user_search_terms (
                    domain TEXT NOT NULL,
                    primary_email TEXT NOT NULL,
                    term TEXT NOT NULL,
                    PRIMARY KEY (domain, primary_email, term)
                );
                CREATE TABLE IF NOT EXISTS user_search_terms_stage (
                    domain TEXT NOT NULL,
                    primary_email TEXT NOT NULL,
                    term TEXT NOT NULL,
                    PRIMARY KEY (domain, primary_email, term)
                );
                CREATE TABLE IF NOT EXISTS groups (
                    domain TEXT NOT NULL,
                    email TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    members_count INTEGER,
                    PRIMARY KEY (domain, email)
                );
                CREATE TABLE IF NOT EXISTS groups_stage (
                    domain TEXT NOT NULL,
                    email TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    members_count INTEGER,
                    PRIMARY KEY (domain, email)
                );
                CREATE TABLE IF NOT EXISTS group_search_terms (
                    domain TEXT NOT NULL,
                    email TEXT NOT NULL,
                    term TEXT NOT NULL,
                    PRIMARY KEY (domain, email, term)
                );
                CREATE TABLE IF NOT EXISTS group_search_terms_stage (
                    domain TEXT NOT NULL,
                    email TEXT NOT NULL,
                    term TEXT NOT NULL,
                    PRIMARY KEY (domain, email, term)
                );
                CREATE INDEX IF NOT EXISTS users_order
                    ON users (domain, suspended, family_name COLLATE NOCASE,
                              given_name COLLATE NOCASE, primary_email COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS user_terms_prefix
                    ON user_search_terms (domain, term, primary_email);
                CREATE INDEX IF NOT EXISTS groups_order
                    ON groups (domain, name COLLATE NOCASE, email COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS group_terms_prefix
                    ON group_search_terms (domain, term, email);
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )

    @staticmethod
    def _user_row(domain: str, user: GAMUser) -> tuple:
        return (
            domain,
            _summary_text(user.primary_email, _MAX_EMAIL_CHARS).strip().lower(),
            _summary_text(user.given_name, _MAX_NAME_CHARS),
            _summary_text(user.family_name, _MAX_NAME_CHARS),
            int(bool(user.suspended)),
            _summary_text(user.org_unit_path or "/", _MAX_ORG_UNIT_CHARS),
            _summary_text(user.title, _MAX_PROFILE_SUMMARY_CHARS),
            _summary_text(user.department, _MAX_PROFILE_SUMMARY_CHARS),
            int(bool(user.is_admin)),
            int(bool(user.is_delegated_admin)),
            int(bool(user.enrolled_2sv)),
            _summary_text(user.last_login_time, _MAX_TIMESTAMP_CHARS) or None,
        )

    @staticmethod
    def _group_row(domain: str, group: GAMGroup) -> tuple:
        return (
            domain,
            group.email.strip().lower(),
            group.name or "",
            group.description or "",
            group.members_count,
        )

    def replace_users(self, users: Iterable[GAMUser]) -> int:
        """Atomically replace this domain's user snapshot through staging tables."""
        with closing(self._conn()) as conn, conn:
            conn.execute("DELETE FROM users_stage WHERE domain = ?", (self.domain,))
            conn.execute("DELETE FROM user_search_terms_stage WHERE domain = ?", (self.domain,))
            for user in users:
                row = self._user_row(self.domain, user)
                email = row[1]
                if not email:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO users_stage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO user_search_terms_stage VALUES (?,?,?)",
                    [
                        (self.domain, email, term)
                        for term in _terms(
                            email,
                            row[2],
                            row[3],
                            f"{row[2]} {row[3]}".strip(),
                            row[6],
                            row[7],
                            row[5],
                        )
                    ],
                )
            count = conn.execute(
                "SELECT COUNT(*) FROM users_stage WHERE domain = ?", (self.domain,)
            ).fetchone()[0]
            conn.execute("DELETE FROM users WHERE domain = ?", (self.domain,))
            conn.execute(
                "INSERT INTO users SELECT * FROM users_stage WHERE domain = ?", (self.domain,)
            )
            conn.execute("DELETE FROM user_search_terms WHERE domain = ?", (self.domain,))
            conn.execute(
                "INSERT INTO user_search_terms "
                "SELECT * FROM user_search_terms_stage WHERE domain = ?",
                (self.domain,),
            )
            conn.execute("DELETE FROM users_stage WHERE domain = ?", (self.domain,))
            conn.execute("DELETE FROM user_search_terms_stage WHERE domain = ?", (self.domain,))
            self._stamp(conn, "users")
        self._restrict_perms()
        return int(count)

    def replace_groups(self, groups: Iterable[GAMGroup]) -> int:
        """Atomically replace this domain's group snapshot through staging tables."""
        with closing(self._conn()) as conn, conn:
            conn.execute("DELETE FROM groups_stage WHERE domain = ?", (self.domain,))
            conn.execute("DELETE FROM group_search_terms_stage WHERE domain = ?", (self.domain,))
            for group in groups:
                row = self._group_row(self.domain, group)
                email = row[1]
                if not email:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO groups_stage VALUES (?,?,?,?,?)",
                    row,
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO group_search_terms_stage VALUES (?,?,?)",
                    [
                        (self.domain, email, term)
                        for term in _terms(email, group.name, group.description)
                    ],
                )
            count = conn.execute(
                "SELECT COUNT(*) FROM groups_stage WHERE domain = ?", (self.domain,)
            ).fetchone()[0]
            conn.execute("DELETE FROM groups WHERE domain = ?", (self.domain,))
            conn.execute(
                "INSERT INTO groups SELECT * FROM groups_stage WHERE domain = ?", (self.domain,)
            )
            conn.execute("DELETE FROM group_search_terms WHERE domain = ?", (self.domain,))
            conn.execute(
                "INSERT INTO group_search_terms "
                "SELECT * FROM group_search_terms_stage WHERE domain = ?",
                (self.domain,),
            )
            conn.execute("DELETE FROM groups_stage WHERE domain = ?", (self.domain,))
            conn.execute("DELETE FROM group_search_terms_stage WHERE domain = ?", (self.domain,))
            self._stamp(conn, "groups")
        self._restrict_perms()
        return int(count)

    def upsert_user(self, user: GAMUser) -> None:
        row = self._user_row(self.domain, user)
        email = row[1]
        if not email:
            return
        with closing(self._conn()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
            conn.execute(
                "DELETE FROM user_search_terms WHERE domain = ? AND primary_email = ?",
                (self.domain, email),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO user_search_terms VALUES (?,?,?)",
                [
                    (self.domain, email, term)
                    for term in _terms(
                        email,
                        row[2],
                        row[3],
                        f"{row[2]} {row[3]}".strip(),
                        row[6],
                        row[7],
                        row[5],
                    )
                ],
            )
        self._restrict_perms()

    def remove_user(self, email: str) -> None:
        key = (email or "").strip().lower()
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "DELETE FROM users WHERE domain = ? AND primary_email = ?",
                (self.domain, key),
            )
            conn.execute(
                "DELETE FROM user_search_terms WHERE domain = ? AND primary_email = ?",
                (self.domain, key),
            )

    def upsert_group(self, group: GAMGroup) -> None:
        row = self._group_row(self.domain, group)
        email = row[1]
        if not email:
            return
        with closing(self._conn()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO groups VALUES (?,?,?,?,?)", row)
            conn.execute(
                "DELETE FROM group_search_terms WHERE domain = ? AND email = ?",
                (self.domain, email),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO group_search_terms VALUES (?,?,?)",
                [
                    (self.domain, email, term)
                    for term in _terms(email, group.name, group.description)
                ],
            )
        self._restrict_perms()

    def remove_group(self, email: str) -> None:
        key = (email or "").strip().lower()
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "DELETE FROM groups WHERE domain = ? AND email = ?", (self.domain, key)
            )
            conn.execute(
                "DELETE FROM group_search_terms WHERE domain = ? AND email = ?",
                (self.domain, key),
            )

    def _stamp(self, conn: sqlite3.Connection, kind: str) -> None:
        conn.execute(
            "INSERT INTO snapshots (domain, kind, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(domain, kind) DO UPDATE SET updated_at=excluded.updated_at",
            (self.domain, kind, time.time()),
        )

    def mark_stale(self, kind: str) -> None:
        self._require_kind(kind)
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT INTO snapshots (domain, kind, updated_at) VALUES (?,?,0) "
                "ON CONFLICT(domain, kind) DO UPDATE SET updated_at=0",
                (self.domain, kind),
            )

    def set_refreshing(self, kind: str, refreshing: bool) -> None:
        self._require_kind(kind)
        with self._state_lock:
            if refreshing:
                self._refreshing.add(kind)
            else:
                self._refreshing.discard(kind)

    def is_refreshing(self, kind: str) -> bool:
        self._require_kind(kind)
        with self._state_lock:
            return kind in self._refreshing

    @staticmethod
    def _require_kind(kind: str) -> None:
        if kind not in _KINDS:
            raise ValueError(f"unknown directory snapshot kind: {kind}")

    def snapshot_age(self, kind: str) -> Optional[float]:
        self._require_kind(kind)
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT updated_at FROM snapshots WHERE domain = ? AND kind = ?",
                (self.domain, kind),
            ).fetchone()
        if row is None or float(row[0]) <= 0:
            return None
        return max(0.0, time.time() - float(row[0]))

    def is_stale(self, kind: str, max_age_seconds: float = DEFAULT_STALE_SECONDS) -> bool:
        age = self.snapshot_age(kind)
        return age is None or age > max(0.0, max_age_seconds)

    def has_snapshot(self, kind: str) -> bool:
        """Return whether a refresh has completed, including a valid empty snapshot."""
        self._require_kind(kind)
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT 1 FROM snapshots WHERE domain = ? AND kind = ?",
                (self.domain, kind),
            ).fetchone()
        return row is not None

    def is_empty(self, kind: str) -> bool:
        self._require_kind(kind)
        table = "users" if kind == "users" else "groups"
        with closing(self._conn()) as conn:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE domain = ?", (self.domain,)
            ).fetchone()[0]
        return int(count) == 0

    def status(self) -> DirectoryStatus:
        with closing(self._conn()) as conn:
            users = conn.execute(
                "SELECT COUNT(*) FROM users WHERE domain = ?", (self.domain,)
            ).fetchone()[0]
            groups = conn.execute(
                "SELECT COUNT(*) FROM groups WHERE domain = ?", (self.domain,)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT kind, updated_at FROM snapshots WHERE domain = ?", (self.domain,)
            ).fetchall()
        updated = {str(row["kind"]): float(row["updated_at"]) for row in rows}
        return DirectoryStatus(
            domain=self.domain,
            users=int(users),
            groups=int(groups),
            users_updated_at=updated.get("users"),
            groups_updated_at=updated.get("groups"),
        )

    def search_users(
        self,
        query: str = "",
        scope: str = "all",
        *,
        limit: int = MAX_PAGE_SIZE,
        cursor: Optional[str] = None,
        offset: int = 0,
    ) -> Page[GAMUser]:
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        offset = _decode_cursor(cursor, offset)
        scope_sql = ""
        if scope == "active":
            scope_sql = " AND u.suspended = 0"
        elif scope == "suspended":
            scope_sql = " AND u.suspended = 1"
        elif scope != "all":
            raise ValueError("scope must be all, active, or suspended")

        raw_query = (query or "").strip()
        term = _normalise_term(raw_query)
        if raw_query and not term:
            return Page([], None, 0, self.snapshot_age("users"), self.is_refreshing("users"))

        with closing(self._conn()) as conn:
            if term:
                lower, upper = term, term + "\U0010ffff"
                count_sql = (
                    "WITH matched AS ("
                    " SELECT DISTINCT primary_email FROM user_search_terms"
                    " WHERE domain = ? AND term >= ? AND term < ?"
                    ") SELECT COUNT(*) FROM users u"
                    " JOIN matched m ON m.primary_email = u.primary_email"
                    " WHERE u.domain = ?" + scope_sql
                )
                total = int(
                    conn.execute(
                        count_sql, (self.domain, lower, upper, self.domain)
                    ).fetchone()[0]
                )
                offset = self._clamp_offset(offset, limit, total)
                rows = conn.execute(
                    "WITH matched AS ("
                    " SELECT DISTINCT primary_email FROM user_search_terms"
                    " WHERE domain = ? AND term >= ? AND term < ?"
                    ") SELECT u.* FROM users u"
                    " JOIN matched m ON m.primary_email = u.primary_email"
                    " WHERE u.domain = ?"
                    + scope_sql
                    + " ORDER BY u.family_name COLLATE NOCASE, u.given_name COLLATE NOCASE,"
                    " u.primary_email COLLATE NOCASE LIMIT ? OFFSET ?",
                    (self.domain, lower, upper, self.domain, limit, offset),
                ).fetchall()
            else:
                total = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM users u WHERE u.domain = ?" + scope_sql,
                        (self.domain,),
                    ).fetchone()[0]
                )
                offset = self._clamp_offset(offset, limit, total)
                rows = conn.execute(
                    "SELECT u.* FROM users u WHERE u.domain = ?"
                    + scope_sql
                    + " ORDER BY u.family_name COLLATE NOCASE, u.given_name COLLATE NOCASE,"
                    " u.primary_email COLLATE NOCASE LIMIT ? OFFSET ?",
                    (self.domain, limit, offset),
                ).fetchall()
        items = [self._user_from_row(row) for row in rows]
        next_cursor = _encode_cursor(offset + limit) if offset + len(items) < total else None
        return Page(
            items,
            next_cursor,
            total,
            self.snapshot_age("users"),
            self.is_refreshing("users"),
        )

    def search_groups(
        self,
        query: str = "",
        *,
        limit: int = MAX_PAGE_SIZE,
        cursor: Optional[str] = None,
        offset: int = 0,
    ) -> Page[GAMGroup]:
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        offset = _decode_cursor(cursor, offset)
        raw_query = (query or "").strip()
        term = _normalise_term(raw_query)
        if raw_query and not term:
            return Page([], None, 0, self.snapshot_age("groups"), self.is_refreshing("groups"))

        with closing(self._conn()) as conn:
            if term:
                lower, upper = term, term + "\U0010ffff"
                total = int(
                    conn.execute(
                        "WITH matched AS ("
                        " SELECT DISTINCT email FROM group_search_terms"
                        " WHERE domain = ? AND term >= ? AND term < ?"
                        ") SELECT COUNT(*) FROM groups g"
                        " JOIN matched m ON m.email = g.email WHERE g.domain = ?",
                        (self.domain, lower, upper, self.domain),
                    ).fetchone()[0]
                )
                offset = self._clamp_offset(offset, limit, total)
                rows = conn.execute(
                    "WITH matched AS ("
                    " SELECT DISTINCT email FROM group_search_terms"
                    " WHERE domain = ? AND term >= ? AND term < ?"
                    ") SELECT g.* FROM groups g"
                    " JOIN matched m ON m.email = g.email WHERE g.domain = ?"
                    " ORDER BY g.name COLLATE NOCASE, g.email COLLATE NOCASE LIMIT ? OFFSET ?",
                    (self.domain, lower, upper, self.domain, limit, offset),
                ).fetchall()
            else:
                total = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM groups WHERE domain = ?", (self.domain,)
                    ).fetchone()[0]
                )
                offset = self._clamp_offset(offset, limit, total)
                rows = conn.execute(
                    "SELECT * FROM groups WHERE domain = ?"
                    " ORDER BY name COLLATE NOCASE, email COLLATE NOCASE LIMIT ? OFFSET ?",
                    (self.domain, limit, offset),
                ).fetchall()
        items = [self._group_from_row(row) for row in rows]
        next_cursor = _encode_cursor(offset + limit) if offset + len(items) < total else None
        return Page(
            items,
            next_cursor,
            total,
            self.snapshot_age("groups"),
            self.is_refreshing("groups"),
        )

    def search_org_units(
        self,
        query: str = "",
        *,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> Page[str]:
        """Return bounded distinct OU paths from the indexed user snapshot."""

        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        offset = max(0, int(offset))
        term = unicodedata.normalize("NFKC", str(query or "")).strip().casefold()
        if any(ord(character) < 32 for character in term):
            return Page(
                [], None, 0, self.snapshot_age("users"), self.is_refreshing("users")
            )
        predicate = (
            "domain = ? AND org_unit_path <> ''"
            + (" AND instr(lower(org_unit_path), ?) > 0" if term else "")
        )
        params: tuple[object, ...] = (self.domain, term) if term else (self.domain,)
        with closing(self._conn()) as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM (SELECT DISTINCT org_unit_path "
                    f"FROM users WHERE {predicate})",
                    params,
                ).fetchone()[0]
            )
            offset = self._clamp_offset(offset, limit, total)
            rows = conn.execute(
                f"SELECT DISTINCT org_unit_path FROM users WHERE {predicate} "
                "ORDER BY org_unit_path COLLATE NOCASE LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        items = [
            _summary_text(row["org_unit_path"], _MAX_ORG_UNIT_CHARS)
            for row in rows
        ]
        next_cursor = _encode_cursor(offset + limit) if offset + len(items) < total else None
        return Page(
            items,
            next_cursor,
            total,
            self.snapshot_age("users"),
            self.is_refreshing("users"),
        )

    @staticmethod
    def _clamp_offset(offset: int, limit: int, total: int) -> int:
        if total and offset >= total:
            return ((total - 1) // limit) * limit
        return offset

    @staticmethod
    def _user_from_row(row: sqlite3.Row) -> GAMUser:
        return GAMUser(
            primary_email=_summary_text(row["primary_email"], _MAX_EMAIL_CHARS),
            given_name=_summary_text(row["given_name"], _MAX_NAME_CHARS),
            family_name=_summary_text(row["family_name"], _MAX_NAME_CHARS),
            suspended=bool(row["suspended"]),
            org_unit_path=_summary_text(row["org_unit_path"], _MAX_ORG_UNIT_CHARS),
            is_admin=bool(row["is_admin"]),
            is_delegated_admin=bool(row["is_delegated_admin"]),
            enrolled_2sv=bool(row["enrolled_2sv"]),
            title=_summary_text(row["title"], _MAX_PROFILE_SUMMARY_CHARS),
            department=_summary_text(row["department"], _MAX_PROFILE_SUMMARY_CHARS),
            last_login_time=_summary_text(
                row["last_login_time"], _MAX_TIMESTAMP_CHARS
            )
            or None,
        )

    @staticmethod
    def _group_from_row(row: sqlite3.Row) -> GAMGroup:
        return GAMGroup(
            email=str(row["email"]),
            name=str(row["name"]),
            description=str(row["description"]),
            members_count=row["members_count"],
        )
