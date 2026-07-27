"""Drag-and-drop group membership board (/groups)."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core.gam.errors import GAMError
from ..activity import ADMIN_ACTIVITY_BUSY_MESSAGE, try_acquire_admin_activity
from ..server import TEMPLATES

_GROUPS_PAGE = "groups.html"
_BOARD_MEMBERS_PARTIAL = "_board_members.html"
_GROUP_OPTIONS_PARTIAL = "_group_search_options.html"
_PEOPLE_RESULTS_PARTIAL = "_people_search_results.html"
_DIRECTORY_LIMIT = 50
_MEMBER_LIMIT = 50

router = APIRouter(prefix="/groups")


def _friendly(exc: Exception) -> str:
    return exc.remediation if isinstance(exc, GAMError) else "Something went wrong talking to GAM."


@router.get("", response_class=HTMLResponse)
async def board(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _GROUPS_PAGE, {"connected": False})
    user_result, group_result = await asyncio.gather(
        st.directory_users(scope="active", limit=_DIRECTORY_LIMIT),
        st.directory_groups(limit=_DIRECTORY_LIMIT),
        return_exceptions=True,
    )
    errors = [
        _friendly(result)
        for result in (user_result, group_result)
        if isinstance(result, Exception)
    ]
    return TEMPLATES.TemplateResponse(
        request,
        _GROUPS_PAGE,
        {
            "connected": True,
            "people_page": None if isinstance(user_result, Exception) else user_result,
            "groups_page": None if isinstance(group_result, Exception) else group_result,
            "error": " ".join(errors),
        },
    )


@router.get("/search/users", response_class=HTMLResponse)
async def search_users(request: Request, q: str = "") -> HTMLResponse:
    """Return one bounded page of indexed people for the drag source."""
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(
            request,
            _PEOPLE_RESULTS_PARTIAL,
            {"page": None, "error": "Not connected."},
        )
    try:
        page = await st.directory_users(
            query=q,
            scope="active",
            limit=_DIRECTORY_LIMIT,
        )
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request,
            _PEOPLE_RESULTS_PARTIAL,
            {"page": None, "error": _friendly(exc)},
        )
    return TEMPLATES.TemplateResponse(
        request,
        _PEOPLE_RESULTS_PARTIAL,
        {"page": page, "query": q.strip()},
    )


@router.get("/search/groups", response_class=HTMLResponse)
async def search_groups(request: Request, q: str = "") -> HTMLResponse:
    """Return one bounded page of indexed groups for the board picker."""
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(
            request,
            _GROUP_OPTIONS_PARTIAL,
            {"page": None, "error": "Not connected."},
        )
    try:
        page = await st.directory_groups(query=q, limit=_DIRECTORY_LIMIT)
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request,
            _GROUP_OPTIONS_PARTIAL,
            {"page": None, "error": _friendly(exc)},
        )
    return TEMPLATES.TemplateResponse(
        request,
        _GROUP_OPTIONS_PARTIAL,
        {"page": page, "query": q.strip()},
    )


async def _members_partial(request: Request, conn, group: str, error: str = "") -> HTMLResponse:
    if not group:
        return TEMPLATES.TemplateResponse(
            request,
            _BOARD_MEMBERS_PARTIAL,
            {"group": "", "page": None, "empty": True},
        )
    try:
        page = await conn.list_group_members_page(group, limit=_MEMBER_LIMIT)
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request,
            _BOARD_MEMBERS_PARTIAL,
            {"group": group, "page": None, "error": _friendly(exc)},
        )
    return TEMPLATES.TemplateResponse(
        request,
        _BOARD_MEMBERS_PARTIAL,
        {"group": group, "page": page, "error": error},
    )


@router.get("/members", response_class=HTMLResponse)
async def members(request: Request, group: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(
            request,
            _BOARD_MEMBERS_PARTIAL,
            {"group": group, "page": None, "error": "Not connected."},
        )
    return await _members_partial(request, st.connector, group)


@router.post("/members", response_class=HTMLResponse)
async def members_mutate(
    request: Request,
    group: Annotated[str, Form()],
    email: Annotated[str, Form()],
    op: Annotated[str, Form()] = "add",
) -> HTMLResponse:
    st = request.app.state.gamgui
    conn = st.connector
    if conn is None:
        return TEMPLATES.TemplateResponse(
            request,
            _BOARD_MEMBERS_PARTIAL,
            {"group": group, "page": None, "error": "Not connected."},
        )
    lease = try_acquire_admin_activity(st, "group-membership-change")
    if lease is None:
        return TEMPLATES.TemplateResponse(
            request,
            _BOARD_MEMBERS_PARTIAL,
            {"group": group, "page": None, "error": ADMIN_ACTIVITY_BUSY_MESSAGE},
        )
    try:
        result = await (
            conn.remove_group_member(group, email)
            if op == "remove"
            else conn.add_group_member(group, email)
        )
        error = "" if result.ok else result.detail
        return await _members_partial(request, conn, group, error=error)
    finally:
        lease.release()
