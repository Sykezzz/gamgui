"""Indexed, keyset-paginated OneRoster snapshot previews.

The normalized snapshot is immutable between explicit term-selection rebuilds, so
preview search can be materialized once without consulting Google or GAM.  FTS
rowids provide a stable cursor order; source rows remain authoritative and are
loaded in one bounded query per page.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence

PREVIEW_INDEX_VERSION = 1
FILTERED_TOTAL_CAP = 10_000
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class PreviewSource:
    sql: str
    search_columns: tuple[str, ...]
    order_by: str


_SOURCES = {
    "courses": PreviewSource(
        "SELECT p.class_id AS _entity_key, p.* "
        "FROM course_plans p WHERE p.domain = ?",
        ("name", "alias", "owner_email", "section", "class_id"),
        "selected DESC, ready DESC, name COLLATE NOCASE, class_id",
    ),
    "excluded": PreviewSource(
        "SELECT p.class_id AS _entity_key, p.* FROM course_plans p "
        "WHERE p.domain = ? AND p.selected = 1 AND p.ready = 0",
        ("name", "alias", "owner_email", "section", "class_id", "quarantine_json"),
        "name COLLATE NOCASE, class_id",
    ),
    "users": PreviewSource(
        "SELECT u.sourced_id AS _entity_key, u.sourced_id, u.status, u.username, "
        "u.email, u.given_name, u.family_name, u.identifier, u.org_ids "
        "FROM users u WHERE u.domain = ?",
        ("sourced_id", "username", "email", "given_name", "family_name", "identifier"),
        "family_name COLLATE NOCASE, given_name COLLATE NOCASE, sourced_id",
    ),
    "enrollments": PreviewSource(
        "SELECT e.sourced_id AS _entity_key, e.sourced_id, e.status, e.class_id, "
        "e.user_id, e.role, e.is_primary, u.email FROM enrollments e "
        "LEFT JOIN users u ON u.domain = e.domain AND u.sourced_id = e.user_id "
        "WHERE e.domain = ?",
        ("sourced_id", "class_id", "user_id", "role", "email"),
        "e.class_id, e.role, u.email COLLATE NOCASE, e.sourced_id",
    ),
    "teachers": PreviewSource(
        "SELECT e.sourced_id AS _entity_key, p.alias, e.class_id, e.user_id, "
        "u.email, e.is_primary FROM enrollments e "
        "JOIN course_plans p ON p.domain = e.domain AND p.class_id = e.class_id "
        "LEFT JOIN users u ON u.domain = e.domain AND u.sourced_id = e.user_id "
        "WHERE e.domain = ? AND e.role = 'teacher' AND e.status != 'tobedeleted'",
        ("alias", "class_id", "user_id", "email"),
        "alias, is_primary DESC, email COLLATE NOCASE, _entity_key",
    ),
    "students": PreviewSource(
        "SELECT e.sourced_id AS _entity_key, p.alias, e.class_id, e.user_id, "
        "u.email FROM enrollments e "
        "JOIN course_plans p ON p.domain = e.domain AND p.class_id = e.class_id "
        "LEFT JOIN users u ON u.domain = e.domain AND u.sourced_id = e.user_id "
        "WHERE e.domain = ? AND e.role = 'student' AND e.status != 'tobedeleted'",
        ("alias", "class_id", "user_id", "email"),
        "alias, email COLLATE NOCASE, _entity_key",
    ),
    "issues": PreviewSource(
        "SELECT CAST(i.id AS TEXT) AS _entity_key, i.* FROM issues i WHERE 1 = ?",
        ("code", "entity_kind", "source_id", "message"),
        "blocking DESC, code, entity_kind, source_id, id",
    ),
    "sessions": PreviewSource(
        "SELECT s.sourced_id AS _entity_key, s.sourced_id, s.status, s.title, "
        "s.session_type, s.start_date, s.end_date, s.parent_id, s.school_year "
        "FROM academic_sessions s WHERE s.domain = ?",
        ("sourced_id", "title", "session_type", "school_year"),
        "start_date DESC, title COLLATE NOCASE, sourced_id",
    ),
    "orgs": PreviewSource(
        "SELECT o.sourced_id AS _entity_key, o.sourced_id, o.status, o.name, "
        "o.org_type, o.parent_id FROM orgs o WHERE o.domain = ?",
        ("sourced_id", "name", "org_type"),
        "name COLLATE NOCASE, sourced_id",
    ),
}
PREVIEW_KINDS = frozenset(_SOURCES)


def rebuild_preview_index(conn: sqlite3.Connection, domain: str) -> None:
    """Atomically replace the FTS preview index for one normalized snapshot."""

    conn.execute("DROP TABLE IF EXISTS preview_search")
    conn.execute("DROP TABLE IF EXISTS preview_totals")
    conn.execute("DROP TABLE IF EXISTS preview_index_meta")
    conn.execute(
        """
        CREATE VIRTUAL TABLE preview_search USING fts5(
            kind,
            entity_key UNINDEXED,
            content,
            tokenize = 'unicode61 remove_diacritics 2'
        )
        """
    )
    conn.execute(
        "CREATE TABLE preview_totals (kind TEXT PRIMARY KEY, total INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE preview_index_meta (version INTEGER NOT NULL)"
    )
    for kind, source in _SOURCES.items():
        parameter: object = 1 if kind == "issues" else domain
        cursor = conn.execute(
            f"{source.sql} ORDER BY {source.order_by}",
            (parameter,),
        )
        batch: list[tuple[str, str, str]] = []
        total = 0
        for row in cursor:
            key = str(row["_entity_key"])
            content = " ".join(
                str(row[column] or "")
                for column in source.search_columns
            )
            batch.append((kind, key, content))
            total += 1
            if len(batch) >= 1_000:
                conn.executemany(
                    "INSERT INTO preview_search(kind, entity_key, content) VALUES (?, ?, ?)",
                    batch,
                )
                batch.clear()
        if batch:
            conn.executemany(
                "INSERT INTO preview_search(kind, entity_key, content) VALUES (?, ?, ?)",
                batch,
            )
        conn.execute(
            "INSERT INTO preview_totals(kind, total) VALUES (?, ?)",
            (kind, total),
        )
    conn.execute(
        "INSERT INTO preview_index_meta(version) VALUES (?)",
        (PREVIEW_INDEX_VERSION,),
    )


def ensure_preview_index(conn: sqlite3.Connection, domain: str) -> None:
    """Prepare legacy snapshots once; new snapshots are indexed during ingestion."""

    if preview_index_ready(conn):
        return
    with conn:
        rebuild_preview_index(conn, domain)


def preview_index_ready(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT version FROM preview_index_meta LIMIT 1"
        ).fetchone()
        totals = int(
            conn.execute("SELECT COUNT(*) FROM preview_totals").fetchone()[0]
        )
    except sqlite3.DatabaseError:
        return False
    return (
        row is not None
        and int(row[0]) == PREVIEW_INDEX_VERSION
        and totals == len(_SOURCES)
    )


def preview_keys(
    conn: sqlite3.Connection,
    kind: str,
    query: str,
    *,
    after_rowid: int,
    limit: int,
) -> list[tuple[int, str]]:
    match = _match_expression(kind, query)
    if match is None:
        return []
    rows = conn.execute(
        """
        SELECT rowid, entity_key FROM preview_search
        WHERE preview_search MATCH ? AND rowid > ?
        ORDER BY rowid
        LIMIT ?
        """,
        (match, max(0, int(after_rowid)), max(1, int(limit))),
    ).fetchall()
    return [(int(row["rowid"]), str(row["entity_key"])) for row in rows]


def preview_total(
    conn: sqlite3.Connection,
    kind: str,
    query: str,
) -> tuple[int, bool]:
    if not query.strip():
        row = conn.execute(
            "SELECT total FROM preview_totals WHERE kind = ?",
            (kind,),
        ).fetchone()
        return (int(row["total"]) if row is not None else 0, True)
    match = _match_expression(kind, query)
    if match is None:
        return 0, True
    count = int(
        conn.execute(
            """
            SELECT COUNT(*) AS matched FROM (
                SELECT rowid FROM preview_search
                WHERE preview_search MATCH ?
                LIMIT ?
            )
            """,
            (match, FILTERED_TOTAL_CAP + 1),
        ).fetchone()["matched"]
    )
    if count > FILTERED_TOTAL_CAP:
        return FILTERED_TOTAL_CAP, False
    return count, True


def source_rows(
    conn: sqlite3.Connection,
    kind: str,
    domain: str,
    entity_keys: Sequence[str],
) -> list[sqlite3.Row]:
    if not entity_keys:
        return []
    source = _source(kind)
    placeholders = ",".join("?" for _ in entity_keys)
    parameter: object = 1 if kind == "issues" else domain
    rows = conn.execute(
        f"SELECT * FROM ({source.sql}) AS source "
        f"WHERE source._entity_key IN ({placeholders})",
        (parameter, *entity_keys),
    ).fetchall()
    by_key = {str(row["_entity_key"]): row for row in rows}
    return [by_key[key] for key in entity_keys if key in by_key]


def _source(kind: str) -> PreviewSource:
    try:
        return _SOURCES[kind]
    except KeyError:
        raise ValueError("Unknown OneRoster preview kind.") from None


def _match_expression(kind: str, query: str) -> str | None:
    _source(kind)
    pieces = [token.casefold()[:64] for token in _TOKEN_RE.findall(query)[:12]]
    if query.strip() and not pieces:
        return None
    expression = f'kind:"{kind}"'
    for token in pieces:
        escaped = token.replace('"', '""')
        expression += f' AND content:"{escaped}"*'
    return expression


def available_kinds() -> Iterable[str]:
    return _SOURCES.keys()
