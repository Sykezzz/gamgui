"""Audit Viewer routes (read-only).

Reads the local JSONL audit log through its incremental SQLite index (see ``core/audit.py``) — no
gam calls at all. Every guarded mutation elsewhere in the app appends a line to that log; this
screen just surfaces it, including the ok:false failures that otherwise sit silent in a file.
"""

from __future__ import annotations

import asyncio
import csv
import io
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse

from ...core.audit import (
    AUDIT_PAGE_SIZE,
    MAX_AUDIT_QUERY_CHARS,
    MIN_AUDIT_QUERY_CHARS,
    AuditPage,
    default_audit_path,
    get_audit_index,
    validate_audit_query,
)
from ..csvutil import csv_safe
from ..server import TEMPLATES

router = APIRouter(prefix="/audit")

PAGE_SIZE = AUDIT_PAGE_SIZE

_AUDIT_PAGE = "audit.html"
_AUDIT_ROWS = "_audit_rows.html"


def _audit_path(request: Request) -> Path:
    """The audit log path actually in use — the connected connector's own log if present.

    ``AppState`` doesn't keep a separate path field; the connector (when connected) owns the
    ``AuditLog`` instance that every mutation writes through, so its ``.path`` is the source of
    truth. Falls back to the default user-data-dir location when there's no connector (e.g. before
    setup), which mirrors what a freshly constructed ``AuditLog()`` would use anyway.
    """
    st = request.app.state.gamgui
    conn = getattr(st, "connector", None)
    audit = getattr(conn, "audit", None)
    path = getattr(audit, "path", None)
    return path if path is not None else default_audit_path()


def _flag(value: str) -> bool:
    """Parse checkbox-style query values without rejecting an empty pager parameter."""
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _display_row(record: dict) -> dict:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    error = str((extra or {}).get("error") or "")
    argv = record.get("argv") if isinstance(record.get("argv"), list) else []
    detail = error or " ".join(str(arg) for arg in argv)
    return {
        "ts": str(record.get("ts") or "")[:64],
        "action": str(record.get("action") or "")[:96],
        "target": str(record.get("target") or "")[:320],
        "ok": record.get("ok"),
        "detail": detail[:160],
        "detail_truncated": len(detail) > 160,
    }


def _indexed_rows_context(result: AuditPage, q: str, failed: bool) -> dict:
    return {
        "rows": [_display_row(record) for record in result.rows],
        "q": q,
        "failed": failed,
        "page": result.page,
        "pages": result.pages,
        "total": result.total,
    }


@router.get("", response_class=HTMLResponse)
async def audit_page(request: Request) -> HTMLResponse:
    index = await asyncio.to_thread(get_audit_index, _audit_path(request))

    def load():
        return index.summary(), index.page(page_size=PAGE_SIZE)

    summary, result = await asyncio.to_thread(load)
    ctx = {
        "total": summary.total,
        "failures": summary.failures,
        "query_min": MIN_AUDIT_QUERY_CHARS,
        "query_max": MAX_AUDIT_QUERY_CHARS,
    }
    ctx.update(_indexed_rows_context(result, "", False))
    return TEMPLATES.TemplateResponse(request, _AUDIT_PAGE, ctx)


@router.get("/rows", response_class=HTMLResponse)
async def audit_rows(request: Request, q: str = "", failed: str = "", page: int = 1) -> HTMLResponse:
    failed_only = _flag(failed)
    try:
        query = validate_audit_query(q)
    except ValueError as exc:
        return TEMPLATES.TemplateResponse(
            request,
            _AUDIT_ROWS,
            {
                "rows": [],
                "q": q.strip()[:MAX_AUDIT_QUERY_CHARS],
                "failed": failed_only,
                "page": 1,
                "pages": 1,
                "total": 0,
                "query_error": str(exc),
            },
            status_code=400,
        )
    index = await asyncio.to_thread(get_audit_index, _audit_path(request))
    result = await asyncio.to_thread(
        index.page,
        q=query,
        failed=failed_only,
        page=page,
        page_size=PAGE_SIZE,
    )
    ctx = _indexed_rows_context(result, query, failed_only)
    return TEMPLATES.TemplateResponse(request, _AUDIT_ROWS, ctx)


@router.get("/export.csv")
async def audit_export(request: Request, q: str = "", failed: str = ""):
    try:
        query = validate_audit_query(q)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    index = await asyncio.to_thread(get_audit_index, _audit_path(request))

    def rows() -> Iterator[str]:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ts", "action", "target", "ok", "exit_code", "error", "argv"])
        yield buf.getvalue()
        for record in index.iter_filtered(q=query, failed=_flag(failed)):
            buf.seek(0)
            buf.truncate(0)
            extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
            error = (extra or {}).get("error", "")
            argv = " ".join(str(arg) for arg in (record.get("argv") or []))
            writer.writerow(
                [
                    csv_safe(cell)
                    for cell in (
                        record.get("ts", ""),
                        record.get("action", ""),
                        record.get("target", ""),
                        record.get("ok"),
                        record.get("exit_code"),
                        error,
                        argv,
                    )
                ]
            )
            yield buf.getvalue()

    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-export.csv"},
    )
