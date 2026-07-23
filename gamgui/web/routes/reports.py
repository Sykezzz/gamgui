"""Reports / insights routes (read-only)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...core import reports as reports_mod
from ...core.gam.errors import GAMError
from ..server import TEMPLATES

router = APIRouter(prefix="/reports")

_REPORTS_PAGE = "reports.html"
_REPORT_BUCKET_PARTIAL = "_report_bucket.html"
_USAGE_REPORT_PARTIAL = "_usage_report.html"


def _friendly(exc: Exception) -> str:
    return exc.remediation if isinstance(exc, GAMError) else "Couldn't load report data."


@router.get("", response_class=HTMLResponse)
async def reports_page(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _REPORTS_PAGE, {"connected": False, "reports": []})
    try:
        # Ensure the summary snapshot exists, then aggregate directly in SQLite. No report user
        # objects are sent until an operator explicitly opens one finding.
        await st.directory_users(limit=1)
        index = st.ensure_directory_index()
        if index is None:
            raise RuntimeError("directory index unavailable")
        reports = await asyncio.to_thread(reports_mod.indexed_report_summaries, index)
        status = await asyncio.to_thread(index.status)
    except Exception as exc:
        msg = exc.remediation if isinstance(exc, GAMError) else "Couldn't load users."
        return TEMPLATES.TemplateResponse(
            request, _REPORTS_PAGE, {"connected": True, "reports": [], "error": msg, "total": 0}
        )
    return TEMPLATES.TemplateResponse(
        request,
        _REPORTS_PAGE,
        {
            "connected": True,
            "reports": reports,
            "total": status.users,
            "snapshot_age_seconds": index.snapshot_age("users"),
            "refreshing": index.is_refreshing("users"),
        },
    )


@router.get("/bucket", response_class=HTMLResponse)
async def report_bucket(
    request: Request,
    key: str,
    cursor: str = "",
) -> HTMLResponse:
    """Load one selected finding from the local index, never more than 50 users."""

    st = request.app.state.gamgui
    definition = reports_mod.indexed_report_definition(key)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(
            request,
            _REPORT_BUCKET_PARTIAL,
            {"report": definition, "page": None, "error": "Not connected."},
        )
    if definition is None:
        return TEMPLATES.TemplateResponse(
            request,
            _REPORT_BUCKET_PARTIAL,
            {"report": None, "page": None, "error": "Unknown report."},
            status_code=400,
        )
    try:
        await st.directory_users(limit=1)
        index = st.ensure_directory_index()
        if index is None:
            raise RuntimeError("directory index unavailable")
        page = await asyncio.to_thread(
            reports_mod.indexed_report_page,
            index,
            key,
            limit=50,
            cursor=cursor or None,
        )
    except ValueError:
        return TEMPLATES.TemplateResponse(
            request,
            _REPORT_BUCKET_PARTIAL,
            {"report": definition, "page": None, "error": "That report page is no longer valid."},
            status_code=400,
        )
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request,
            _REPORT_BUCKET_PARTIAL,
            {"report": definition, "page": None, "error": _friendly(exc)},
        )
    definition = reports_mod.ReportSummary(
        definition.key,
        definition.title,
        definition.description,
        page.total,
    )
    return TEMPLATES.TemplateResponse(
        request,
        _REPORT_BUCKET_PARTIAL,
        {"report": definition, "page": page, "error": ""},
    )


@router.get("/usage", response_class=HTMLResponse)
async def usage(request: Request) -> HTMLResponse:
    """Lazy-loaded storage/mail usage (a separate, slower Reports-API call)."""
    conn = request.app.state.gamgui.connector
    if conn is None:
        return TEMPLATES.TemplateResponse(request, _USAGE_REPORT_PARTIAL, {"rows": [], "date": "", "error": "Not connected."})
    try:
        data = await conn.usage_report(reports_mod.USAGE_PARAMS, limit=25)
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, _USAGE_REPORT_PARTIAL, {"rows": [], "date": "", "error": _friendly(exc)})
    rows = await asyncio.to_thread(reports_mod.parse_usage, data["rows"], limit=25)
    return TEMPLATES.TemplateResponse(request, _USAGE_REPORT_PARTIAL, {"rows": rows, "date": data["date"]})
