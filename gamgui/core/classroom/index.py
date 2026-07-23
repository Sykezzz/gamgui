"""Domain-isolated SQLite index for fast Classroom course search."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from ..paths import app_data_dir
from .models import CourseSummary

MAX_PAGE_SIZE = 50
SNAPSHOT_TTL_SECONDS = 15 * 60


def default_course_index_path() -> Path:
    return app_data_dir() / "classroom_courses.db"


@dataclass(frozen=True)
class CourseSnapshotStatus:
    domain: str
    count: int
    updated_at: Optional[float]

    @property
    def age_seconds(self) -> Optional[float]:
        if self.updated_at is None:
            return None
        return max(0.0, time.time() - self.updated_at)

    @property
    def stale(self) -> bool:
        age = self.age_seconds
        return age is None or age > SNAPSHOT_TTL_SECONDS


@dataclass(frozen=True)
class CoursePage:
    items: Tuple[CourseSummary, ...]
    next_cursor: Optional[str]
    total: int
    snapshot_age_seconds: Optional[float]
    refreshing: bool = False


class CourseIndex:
    """A rebuildable course-summary cache; every read is scoped by the active domain."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fts_enabled = False
        self._init()
        self._restrict_perms()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init(self) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS courses (
                    domain TEXT NOT NULL,
                    id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    section TEXT NOT NULL,
                    room TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    course_state TEXT NOT NULL,
                    creation_time TEXT NOT NULL,
                    update_time TEXT NOT NULL,
                    alternate_link TEXT NOT NULL,
                    PRIMARY KEY (domain, id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS courses_stage (
                    domain TEXT NOT NULL,
                    id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    section TEXT NOT NULL,
                    room TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    course_state TEXT NOT NULL,
                    creation_time TEXT NOT NULL,
                    update_time TEXT NOT NULL,
                    alternate_link TEXT NOT NULL,
                    PRIMARY KEY (domain, id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS courses_domain_state_name "
                "ON courses(domain, course_state, name COLLATE NOCASE)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS courses_domain_owner "
                "ON courses(domain, owner_id)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS course_snapshots "
                "(domain TEXT PRIMARY KEY, updated_at REAL NOT NULL)"
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS courses_fts USING fts5(
                        domain UNINDEXED,
                        course_id UNINDEXED,
                        name,
                        section,
                        room,
                        owner_identifier,
                        tokenize='unicode61 remove_diacritics 2'
                    )
                    """
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False

    def _restrict_perms(self) -> None:
        try:
            os.chmod(str(self.path.parent), 0o700)
        except OSError:
            pass
        for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                if candidate.exists():
                    os.chmod(str(candidate), 0o600)
            except OSError:
                pass

    def replace_all(self, domain: str, courses: Iterable[CourseSummary]) -> int:
        """Atomically replace one domain from a streaming course-summary iterable."""
        scoped_domain = _normalize_domain(domain)
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "DELETE FROM courses_stage WHERE domain = ?",
                (scoped_domain,),
            )
            for course in courses:
                if not course.id:
                    continue
                conn.execute(
                    """
                    INSERT INTO courses_stage (
                        domain, id, name, section, room, owner_id, course_state,
                        creation_time, update_time, alternate_link
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _course_row(scoped_domain, course),
                )
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM courses_stage WHERE domain = ?",
                    (scoped_domain,),
                ).fetchone()[0]
            )
            conn.execute("DELETE FROM courses WHERE domain = ?", (scoped_domain,))
            conn.execute(
                """
                INSERT INTO courses (
                    domain, id, name, section, room, owner_id, course_state,
                    creation_time, update_time, alternate_link
                )
                SELECT
                    domain, id, name, section, room, owner_id, course_state,
                    creation_time, update_time, alternate_link
                FROM courses_stage
                WHERE domain = ?
                """,
                (scoped_domain,),
            )
            if self._fts_enabled:
                conn.execute("DELETE FROM courses_fts WHERE domain = ?", (scoped_domain,))
                conn.execute(
                    """
                    INSERT INTO courses_fts (
                        domain, course_id, name, section, room, owner_identifier
                    )
                    SELECT domain, id, name, section, room, owner_id
                    FROM courses_stage
                    WHERE domain = ?
                    """,
                    (scoped_domain,),
                )
            conn.execute(
                "INSERT OR REPLACE INTO course_snapshots(domain, updated_at) VALUES (?, ?)",
                (scoped_domain, time.time()),
            )
            conn.execute(
                "DELETE FROM courses_stage WHERE domain = ?",
                (scoped_domain,),
            )
        self._restrict_perms()
        return count

    def upsert(self, domain: str, course: CourseSummary) -> None:
        if not course.id:
            return
        scoped_domain = _normalize_domain(domain)
        row = _course_row(scoped_domain, course)
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO courses (
                    domain, id, name, section, room, owner_id, course_state,
                    creation_time, update_time, alternate_link
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain, id) DO UPDATE SET
                    name=excluded.name,
                    section=excluded.section,
                    room=excluded.room,
                    owner_id=excluded.owner_id,
                    course_state=excluded.course_state,
                    creation_time=excluded.creation_time,
                    update_time=excluded.update_time,
                    alternate_link=excluded.alternate_link
                """,
                row,
            )
            if self._fts_enabled:
                conn.execute(
                    "DELETE FROM courses_fts WHERE domain = ? AND course_id = ?",
                    (scoped_domain, course.id),
                )
                conn.execute(
                    """
                    INSERT INTO courses_fts (
                        domain, course_id, name, section, room, owner_identifier
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (row[0], row[1], row[2], row[3], row[4], row[5]),
                )
        self._restrict_perms()

    def search(
        self,
        domain: str,
        query: str = "",
        state: str = "",
        cursor: Optional[str] = None,
        limit: int = MAX_PAGE_SIZE,
        refreshing: bool = False,
    ) -> CoursePage:
        scoped_domain = _normalize_domain(domain)
        q = (query or "").strip()
        normalized_state = (state or "").strip().upper()
        page_size = max(1, min(int(limit or MAX_PAGE_SIZE), MAX_PAGE_SIZE))
        offset = _decode_cursor(cursor, scoped_domain, q, normalized_state)

        with closing(self._conn()) as conn:
            if q and self._fts_enabled and _fts_query(q):
                match = _fts_query(q)
                where = "f.domain = ? AND courses_fts MATCH ?"
                params: List[object] = [scoped_domain, match]
                if normalized_state:
                    where += " AND c.course_state = ?"
                    params.append(normalized_state)
                from_sql = (
                    "courses_fts f JOIN courses c "
                    "ON c.domain = f.domain AND c.id = f.course_id"
                )
            else:
                where = "c.domain = ?"
                params = [scoped_domain]
                if q:
                    escaped = _escape_like(q)
                    like = f"%{escaped}%"
                    where += (
                        " AND (c.id LIKE ? ESCAPE '\\' OR c.name LIKE ? ESCAPE '\\' "
                        "OR c.section LIKE ? ESCAPE '\\' OR c.room LIKE ? ESCAPE '\\' "
                        "OR c.owner_id LIKE ? ESCAPE '\\')"
                    )
                    params.extend([like, like, like, like, like])
                if normalized_state:
                    where += " AND c.course_state = ?"
                    params.append(normalized_state)
                from_sql = "courses c"

            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {from_sql} WHERE {where}",
                    tuple(params),
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT c.* FROM {from_sql}
                WHERE {where}
                ORDER BY
                    CASE c.course_state
                        WHEN 'ACTIVE' THEN 0
                        WHEN 'PROVISIONED' THEN 1
                        WHEN 'ARCHIVED' THEN 2
                        ELSE 3
                    END,
                    c.name COLLATE NOCASE,
                    c.id
                LIMIT ? OFFSET ?
                """,
                tuple(params + [page_size, offset]),
            ).fetchall()

        items = tuple(_summary_from_row(row) for row in rows)
        next_cursor = None
        if offset + len(items) < total:
            next_cursor = _encode_cursor(offset + len(items), scoped_domain, q, normalized_state)
        status = self.status(scoped_domain)
        return CoursePage(
            items=items,
            next_cursor=next_cursor,
            total=total,
            snapshot_age_seconds=status.age_seconds,
            refreshing=refreshing,
        )

    def status(self, domain: str) -> CourseSnapshotStatus:
        scoped_domain = _normalize_domain(domain)
        with closing(self._conn()) as conn:
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM courses WHERE domain = ?", (scoped_domain,)
                ).fetchone()[0]
            )
            row = conn.execute(
                "SELECT updated_at FROM course_snapshots WHERE domain = ?", (scoped_domain,)
            ).fetchone()
        return CourseSnapshotStatus(
            domain=scoped_domain,
            count=count,
            updated_at=float(row[0]) if row else None,
        )


def _course_row(domain: str, course: CourseSummary) -> Tuple[str, ...]:
    return (
        domain,
        str(course.id),
        str(course.name or ""),
        str(course.section or ""),
        str(course.room or ""),
        str(course.owner_id or ""),
        str(course.course_state or "").upper(),
        str(course.creation_time or ""),
        str(course.update_time or ""),
        str(course.alternate_link or ""),
    )


def _summary_from_row(row: sqlite3.Row) -> CourseSummary:
    return CourseSummary(
        id=str(row["id"]),
        name=str(row["name"] or ""),
        section=str(row["section"] or ""),
        room=str(row["room"] or ""),
        owner_id=str(row["owner_id"] or ""),
        course_state=str(row["course_state"] or ""),
        creation_time=str(row["creation_time"] or ""),
        update_time=str(row["update_time"] or ""),
        alternate_link=str(row["alternate_link"] or ""),
    )


def _normalize_domain(domain: str) -> str:
    scoped = (domain or "").strip().casefold()
    if not scoped:
        raise ValueError("A Workspace domain is required for the Classroom index.")
    return scoped


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[\w@.:-]+", query, flags=re.UNICODE)
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"*' for token in tokens)


def _cursor_scope(domain: str, query: str, state: str) -> str:
    return hashlib.sha256(f"{domain}\0{query}\0{state}".encode("utf-8")).hexdigest()[:16]


def _encode_cursor(offset: int, domain: str, query: str, state: str) -> str:
    raw = json.dumps(
        {"offset": int(offset), "scope": _cursor_scope(domain, query, state)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Optional[str], domain: str, query: str, state: str) -> int:
    if not cursor:
        return 0
    try:
        padded = str(cursor) + "=" * (-len(str(cursor)) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if data.get("scope") != _cursor_scope(domain, query, state):
            return 0
        return max(0, int(data.get("offset", 0)))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return 0
