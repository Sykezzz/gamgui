"""Append-only local audit log (JSONL).

Every mutation (and optionally reads) is recorded with a redacted copy of the gam argument vector
so there is a durable, reviewable record of what the tool did. Secrets are never written — values
following sensitive keys (e.g. ``password``) are masked.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .paths import app_data_dir
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

# gam argument keys whose following value must be masked in the log.
_SENSITIVE_KEYS = {"password", "signature", "recoveryemail", "recoveryphone", "alternateemail"}
_MASK = "***redacted***"


def redact_argv(argv: Optional[Sequence[str]]) -> Optional[List[str]]:
    """Return a copy of ``argv`` with values after sensitive keys masked."""
    if argv is None:
        return None
    out: List[str] = []
    mask_next = False
    for tok in argv:
        if mask_next:
            out.append(_MASK)
            mask_next = False
            continue
        out.append(tok)
        if tok.lower() in _SENSITIVE_KEYS:
            mask_next = True
    return out


def default_audit_path() -> Path:
    base = app_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / "audit.jsonl"


AUDIT_PAGE_SIZE = 25
MAX_AUDIT_PAGE_SIZE = 50
MIN_AUDIT_QUERY_CHARS = 3
MAX_AUDIT_QUERY_CHARS = 200
_FINGERPRINT_BYTES = 4096
_INDEX_SCHEMA_VERSION = "2"
_MAX_AUDIT_ACTION_CHARS = 96
_MAX_AUDIT_TARGET_CHARS = 320
_MAX_AUDIT_ARG_CHARS = 256
_MAX_AUDIT_ARGV_CHARS = 4096
_MAX_AUDIT_ARGC = 32
_MAX_AUDIT_EXTRA_FIELDS = 16
_MAX_AUDIT_EXTRA_CHARS = 512


@dataclass(frozen=True)
class AuditSummary:
    total: int
    failures: int


@dataclass(frozen=True)
class AuditPage:
    rows: List[Dict[str, Any]]
    page: int
    pages: int
    total: int


def validate_audit_query(q: str) -> str:
    """Return a normalized indexed query or reject an unsafe broad query."""

    query = (q or "").strip()
    if query and len(query) < MIN_AUDIT_QUERY_CHARS:
        raise ValueError(
            f"Audit search requires at least {MIN_AUDIT_QUERY_CHARS} characters."
        )
    if len(query) > MAX_AUDIT_QUERY_CHARS:
        raise ValueError(
            f"Audit search is limited to {MAX_AUDIT_QUERY_CHARS} characters."
        )
    return query


def _bounded_text(value: Any, limit: int) -> str:
    return str(value or "")[: max(0, int(limit))]


def _bounded_argv(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        source = value
    elif value in (None, ""):
        source = ()
    else:
        source = (value,)
    bounded: List[str] = []
    remaining = _MAX_AUDIT_ARGV_CHARS
    for raw in source[:_MAX_AUDIT_ARGC]:
        if remaining <= 0:
            break
        arg = _bounded_text(raw, min(_MAX_AUDIT_ARG_CHARS, remaining))
        bounded.append(arg)
        remaining -= len(arg)
    return bounded


def _bounded_extra(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    bounded: Dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:_MAX_AUDIT_EXTRA_FIELDS]:
        key = _bounded_text(raw_key, 64)
        if not key:
            continue
        if raw_value is None or isinstance(raw_value, (bool, int, float)):
            bounded[key] = raw_value
        else:
            bounded[key] = _bounded_text(raw_value, _MAX_AUDIT_EXTRA_CHARS)
    return bounded


def _bounded_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Project an arbitrary source record to the bounded index/display contract."""

    exit_code = record.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, (bool, int, float)):
        exit_code = _bounded_text(exit_code, 32)
    ok = record.get("ok")
    if ok not in (True, False, None):
        ok = None
    bounded: Dict[str, Any] = {
        "ts": _bounded_text(record.get("ts"), 64),
        "connector": _bounded_text(record.get("connector"), 64),
        "action": _bounded_text(record.get("action"), _MAX_AUDIT_ACTION_CHARS),
        "target": _bounded_text(record.get("target"), _MAX_AUDIT_TARGET_CHARS),
        "argv": _bounded_argv(record.get("argv")),
        "exit_code": exit_code,
        "ok": ok,
        "actor": _bounded_text(record.get("actor"), 254),
    }
    extra = _bounded_extra(record.get("extra"))
    if extra:
        bounded["extra"] = extra
    return bounded


def _audit_haystack(record: Dict[str, Any]) -> str:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    return "\n".join(
        (
            str(record.get("action") or ""),
            str(record.get("target") or ""),
            str((extra or {}).get("error") or ""),
            " ".join(str(value) for value in (record.get("argv") or [])),
        )
    )


def _decode_record(line: bytes) -> Optional[Dict[str, Any]]:
    try:
        record = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


class AuditIndex:
    """Incremental, owner-only SQLite index over the append-only JSONL audit log.

    The source file remains authoritative. The index records how many source bytes it has
    consumed, verifies a small prefix/tail fingerprint, and parses only newly appended lines.
    A truncation, replacement, or changed fingerprint triggers one atomic rebuild.
    """

    def __init__(self, source_path: Path, index_path: Optional[Path] = None) -> None:
        self.source_path = Path(source_path)
        self.index_path = (
            Path(index_path)
            if index_path is not None
            else self.source_path.with_name(self.source_path.name + ".index.sqlite3")
        )
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            try:
                fd = os.open(
                    str(self.index_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self._lock = threading.RLock()
        self._fts_available = False
        self._init()
        self._restrict_perms()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.index_path, timeout=10.0)
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
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ok INTEGER,
                    haystack TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS records_ok_id ON records (ok, id DESC);
                """
            )
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS audit_fts "
                    "USING fts5(haystack, tokenize='trigram')"
                )
            except sqlite3.OperationalError:
                self._fts_available = False
            else:
                self._fts_available = True
                records_count = int(
                    conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
                )
                fts_count = int(
                    conn.execute("SELECT COUNT(*) FROM audit_fts").fetchone()[0]
                )
                if records_count != fts_count:
                    conn.execute("DELETE FROM audit_fts")
                    conn.execute(
                        "INSERT INTO audit_fts (rowid, haystack) "
                        "SELECT id, haystack FROM records"
                    )
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if version is not None and version["value"] != _INDEX_SCHEMA_VERSION:
                self._clear(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (_INDEX_SCHEMA_VERSION,),
            )

    def _restrict_perms(self) -> None:
        for path in (
            self.index_path,
            Path(str(self.index_path) + "-wal"),
            Path(str(self.index_path) + "-shm"),
        ):
            try:
                if path.exists():
                    os.chmod(path, 0o600)
            except OSError:
                pass

    @staticmethod
    def _meta(conn: sqlite3.Connection) -> Dict[str, str]:
        return {
            str(row["key"]): str(row["value"])
            for row in conn.execute("SELECT key, value FROM meta").fetchall()
        }

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, values: Dict[str, Any]) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [(str(key), str(value)) for key, value in values.items()],
        )

    @staticmethod
    def _digest_range(path: Path, start: int, length: int) -> str:
        if length <= 0:
            return ""
        try:
            with open(path, "rb") as source:
                source.seek(max(0, start))
                data = source.read(max(0, length))
        except OSError:
            return ""
        return hashlib.sha256(data).hexdigest()

    def _source_is_append_only(
        self,
        meta: Dict[str, str],
        stat: os.stat_result,
    ) -> bool:
        try:
            prior_size = int(meta["source_size"])
            prior_device = int(meta["source_device"])
            prior_inode = int(meta["source_inode"])
            prior_mtime_ns = int(meta["source_mtime_ns"])
            prefix_len = int(meta.get("prefix_len", "0"))
            tail_start = int(meta.get("tail_start", "0"))
            tail_len = int(meta.get("tail_len", "0"))
            middle_start = int(meta.get("middle_start", "0"))
            middle_len = int(meta.get("middle_len", "0"))
        except (KeyError, TypeError, ValueError):
            return False
        if stat.st_size < prior_size:
            return False
        if prior_device != int(stat.st_dev) or prior_inode != int(stat.st_ino):
            return False
        # A same-size mtime change cannot be a pure append. This catches in-place rewrites outside
        # the sampled ranges; the middle sample remains a defense when timestamps are preserved.
        if stat.st_size == prior_size and int(stat.st_mtime_ns) != prior_mtime_ns:
            return False
        if self._digest_range(self.source_path, 0, prefix_len) != meta.get(
            "prefix_hash", ""
        ):
            return False
        if self._digest_range(self.source_path, tail_start, tail_len) != meta.get(
            "tail_hash", ""
        ):
            return False
        if self._digest_range(self.source_path, middle_start, middle_len) != meta.get(
            "middle_hash", ""
        ):
            return False
        return True

    def _clear(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM records")
        if self._fts_available:
            conn.execute("DELETE FROM audit_fts")
        conn.execute("DELETE FROM meta WHERE key != 'schema_version'")

    def _insert(self, conn: sqlite3.Connection, record: Dict[str, Any]) -> None:
        record = _bounded_record(record)
        ok = record.get("ok")
        ok_value = 0 if ok is False else 1 if ok is True else None
        haystack = _audit_haystack(record)
        cursor = conn.execute(
            "INSERT INTO records (ok, haystack, payload) VALUES (?, ?, ?)",
            (
                ok_value,
                haystack,
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        if self._fts_available:
            conn.execute(
                "INSERT INTO audit_fts (rowid, haystack) VALUES (?, ?)",
                (cursor.lastrowid, haystack),
            )

    def _ingest(self, conn: sqlite3.Connection, start: int) -> int:
        try:
            source = open(self.source_path, "rb")
        except OSError:
            return 0
        with source:
            source.seek(max(0, start))
            while True:
                line_start = source.tell()
                line = source.readline()
                if not line:
                    return source.tell()
                complete = line.endswith(b"\n")
                candidate = line.rstrip(b"\r\n")
                if not candidate:
                    continue
                record = _decode_record(candidate)
                if record is not None:
                    self._insert(conn, record)
                    continue
                if not complete:
                    # A writer may be between bytes. Retry this line on the next sync.
                    return line_start
                # Malformed complete lines preserve read_records' tolerant skip semantics.

    def sync(self) -> None:
        """Bring the index current without re-reading already indexed JSONL bytes."""

        with self._lock:
            try:
                stat = self.source_path.stat()
            except OSError:
                stat = None
            with closing(self._conn()) as conn, conn:
                meta = self._meta(conn)
                if stat is None:
                    if int(meta.get("source_size", "0") or 0) or conn.execute(
                        "SELECT 1 FROM records LIMIT 1"
                    ).fetchone():
                        self._clear(conn)
                    self._set_meta(
                        conn,
                        {
                            "source_size": 0,
                            "source_device": 0,
                            "source_inode": 0,
                            "source_mtime_ns": 0,
                            "prefix_len": 0,
                            "prefix_hash": "",
                            "tail_start": 0,
                            "tail_len": 0,
                            "tail_hash": "",
                            "middle_start": 0,
                            "middle_len": 0,
                            "middle_hash": "",
                        },
                    )
                    return

                append_only = self._source_is_append_only(meta, stat)
                start = int(meta.get("source_size", "0")) if append_only else 0
                if not append_only:
                    self._clear(conn)
                consumed = self._ingest(conn, start)
                prefix_len = min(consumed, _FINGERPRINT_BYTES)
                tail_start = max(0, consumed - _FINGERPRINT_BYTES)
                tail_len = max(0, consumed - tail_start)
                middle_len = min(consumed, _FINGERPRINT_BYTES)
                middle_start = max(0, (consumed - middle_len) // 2)
                self._set_meta(
                    conn,
                    {
                        "source_size": consumed,
                        "source_device": int(stat.st_dev),
                        "source_inode": int(stat.st_ino),
                        "source_mtime_ns": int(stat.st_mtime_ns),
                        "prefix_len": prefix_len,
                        "prefix_hash": self._digest_range(
                            self.source_path, 0, prefix_len
                        ),
                        "tail_start": tail_start,
                        "tail_len": tail_len,
                        "tail_hash": self._digest_range(
                            self.source_path, tail_start, tail_len
                        ),
                        "middle_start": middle_start,
                        "middle_len": middle_len,
                        "middle_hash": self._digest_range(
                            self.source_path, middle_start, middle_len
                        ),
                    },
                )
            self._restrict_perms()

    @staticmethod
    def _fts_query(q: str) -> str:
        return '"' + q.replace('"', '""') + '"'

    def _filters(
        self,
        q: str,
        failed: bool,
        *,
        force_like: bool = False,
    ) -> Tuple[str, str, List[Any]]:
        joins = ""
        where: List[str] = []
        params: List[Any] = []
        if failed:
            where.append("r.ok = 0")
        query = validate_audit_query(q)
        if query:
            if self._fts_available and not force_like:
                joins = " JOIN audit_fts ON audit_fts.rowid = r.id"
                where.append("audit_fts MATCH ?")
                params.append(self._fts_query(query))
            else:
                where.append("instr(lower(r.haystack), lower(?)) > 0")
                params.append(query)
        clause = " WHERE " + " AND ".join(where) if where else ""
        return joins, clause, params

    def summary(self) -> AuditSummary:
        self.sync()
        with self._lock, closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures FROM records"
            ).fetchone()
        return AuditSummary(int(row["total"]), int(row["failures"] or 0))

    def page(
        self,
        *,
        q: str = "",
        failed: bool = False,
        page: int = 1,
        page_size: int = AUDIT_PAGE_SIZE,
    ) -> AuditPage:
        q = validate_audit_query(q)
        self.sync()
        page_size = max(1, min(int(page_size), MAX_AUDIT_PAGE_SIZE))
        with self._lock, closing(self._conn()) as conn:
            joins, clause, params = self._filters(q, failed)
            try:
                total = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM records r" + joins + clause, params
                    ).fetchone()[0]
                )
            except sqlite3.OperationalError:
                joins, clause, params = self._filters(q, failed, force_like=True)
                total = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM records r" + joins + clause, params
                    ).fetchone()[0]
                )
            pages = max(1, math.ceil(total / page_size))
            page = max(1, min(int(page), pages))
            offset = (page - 1) * page_size
            rows = conn.execute(
                "SELECT r.payload FROM records r"
                + joins
                + clause
                + " ORDER BY r.id DESC LIMIT ? OFFSET ?",
                (*params, page_size, offset),
            ).fetchall()
        return AuditPage(
            rows=[json.loads(row["payload"]) for row in rows],
            page=page,
            pages=pages,
            total=total,
        )

    def latest(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        self.sync()
        with self._lock, closing(self._conn()) as conn:
            if limit is None:
                rows = conn.execute(
                    "SELECT payload FROM records ORDER BY id DESC"
                ).fetchall()
            else:
                limit = max(0, int(limit))
                if not limit:
                    return []
                rows = conn.execute(
                    "SELECT payload FROM records ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def iter_filtered(
        self,
        *,
        q: str = "",
        failed: bool = False,
    ) -> Iterator[Dict[str, Any]]:
        """Stream matching records newest-first without materialising the export."""

        q = validate_audit_query(q)
        self.sync()
        # A WAL read transaction is already a consistent snapshot. Do not retain the Python
        # writer/sync lock while a slow HTTP client consumes a potentially large CSV export.
        with closing(self._conn()) as conn:
            joins, clause, params = self._filters(q, failed)
            try:
                cursor = conn.execute(
                    "SELECT r.payload FROM records r"
                    + joins
                    + clause
                    + " ORDER BY r.id DESC",
                    params,
                )
            except sqlite3.OperationalError:
                joins, clause, params = self._filters(q, failed, force_like=True)
                cursor = conn.execute(
                    "SELECT r.payload FROM records r"
                    + joins
                    + clause
                    + " ORDER BY r.id DESC",
                    params,
                )
            for row in cursor:
                yield json.loads(row["payload"])


_INDEX_CACHE: Dict[Path, AuditIndex] = {}
_INDEX_CACHE_LOCK = threading.Lock()


def get_audit_index(path: Optional[Path] = None) -> AuditIndex:
    source = (Path(path) if path else default_audit_path()).resolve()
    with _INDEX_CACHE_LOCK:
        index = _INDEX_CACHE.get(source)
        if index is None:
            index = AuditIndex(source)
            _INDEX_CACHE[source] = index
        return index


class AuditLog:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_audit_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        try:
            if self.path.exists():
                os.chmod(self.path, 0o600)
        except OSError:
            pass

    def record(
        self,
        action: str,
        *,
        connector: str = "google_workspace",
        target: Optional[str] = None,
        argv: Optional[Sequence[str]] = None,
        exit_code: Optional[int] = None,
        ok: Optional[bool] = None,
        actor: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "connector": connector,
            "action": action,
            "target": target,
            "argv": redact_argv(argv),
            "exit_code": exit_code,
            "ok": ok,
            "actor": actor,
        }
        if extra:
            entry["extra"] = extra
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            # 0600 — the log can reveal who was changed, even without secrets.
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                payload = (line + "\n").encode("utf-8")
                written = 0
                while written < len(payload):
                    count = os.write(fd, payload[written:])
                    if count <= 0:
                        raise OSError("audit write made no progress")
                    written += count
            finally:
                os.close(fd)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        return entry

    def tail(self, limit: int = 100) -> List[Dict[str, Any]]:
        # Historical callers expect oldest-to-newest order within the tail.
        return list(reversed(get_audit_index(self.path).latest(limit)))


def iter_records(
    path: Optional[Path] = None,
    *,
    limit: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    """Iterate indexed records newest-first without a legacy fixed cap."""
    yield from get_audit_index(path).latest(limit)


def read_records(
    path: Optional[Path] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Read JSONL records through the rebuildable local index (no source writes or gam calls).

    Tolerant of a missing file and of malformed/blank lines (skipped rather than raised).
    Returns the most-recent-written-first, optionally capped at ``limit`` entries.
    """
    return get_audit_index(path).latest(limit)
