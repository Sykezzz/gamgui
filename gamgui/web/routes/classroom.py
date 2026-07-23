"""Classroom course, lifecycle, owner, and roster administration."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from ...core.classroom.index import CourseIndex, default_course_index_path
from ...core.classroom.manifests import (
    RosterManifestStore,
    default_roster_manifest_path,
)
from ...core.classroom.models import COURSE_STATES, parse_desired_roster
from ...core.classroom.service import ClassroomService, ClassroomValidationError
from ...core.gam.errors import GAMError
from ..server import TEMPLATES

router = APIRouter(prefix="/classroom")

_NOT_CONNECTED = "Connect a Workspace domain before using Classroom."
_MAX_ROSTER_UPLOAD_BYTES = 1_000_000


def _connector(request: Request):
    return request.app.state.gamgui.connector


def _service(request: Request) -> Optional[ClassroomService]:
    state = request.app.state.gamgui
    connector = state.connector
    domain = (getattr(state, "audit_domain", "") or "").strip().casefold()
    if connector is None or not domain:
        return None

    course_index = getattr(state, "classroom_index", None)
    if course_index is None:
        course_index = CourseIndex(default_course_index_path())
        setattr(state, "classroom_index", course_index)
    manifests = getattr(state, "classroom_manifests", None)
    if manifests is None:
        manifests = RosterManifestStore(default_roster_manifest_path())
        setattr(state, "classroom_manifests", manifests)

    service = getattr(state, "classroom_service", None)
    if (
        service is None
        or service.connector is not connector
        or service.domain != domain
        or service.course_index is not course_index
        or service.manifests is not manifests
    ):
        service = ClassroomService(connector, domain, course_index, manifests)
        setattr(state, "classroom_service", service)
    return service


def _friendly(exc: Exception) -> str:
    if isinstance(exc, ClassroomValidationError):
        return str(exc)
    text = str(exc)
    lowered = text.casefold()
    classroom_messages = (
        (
            ("coursenotmodifiable", "course not modifiable"),
            "Google Classroom reports that this course cannot be modified in its current state.",
        ),
        (
            ("inactivecourseowner", "inactive course owner"),
            "The course owner is inactive. Restore or transfer the owner before changing this course.",
        ),
        (
            ("ineligibleowner", "ineligible owner"),
            "That account is not eligible to own this course.",
        ),
        (
            ("cannotremovecourseowner", "cannot remove course owner"),
            "The course owner cannot be removed from the teacher roster.",
        ),
        (
            ("cannotdirectadduser", "cannot direct add user"),
            "Google requires an invitation for that account; direct enrollment was not attempted.",
        ),
        (
            ("usergroupmembershiplimit", "coursememberlimit", "courseteacherlimit"),
            "This course or account has reached a Classroom membership limit.",
        ),
    )
    for needles, message in classroom_messages:
        if any(needle in lowered for needle in needles):
            return message
    if isinstance(exc, GAMError):
        return exc.remediation
    return "Classroom could not complete that request. No additional changes were attempted."


def _human_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    value = int(max(0, seconds))
    if value < 90:
        return "just now"
    if value < 5400:
        return f"{round(value / 60)} min ago"
    if value < 172800:
        return f"{round(value / 3600)} h ago"
    return f"{round(value / 86400)} d ago"


def _refreshing(request: Request) -> bool:
    task = getattr(request.app.state.gamgui, "classroom_refresh_task", None)
    return task is not None and not task.done()


def _schedule_refresh(request: Request, service: ClassroomService) -> None:
    state = request.app.state.gamgui
    existing = getattr(state, "classroom_refresh_task", None)
    if existing is not None and not existing.done():
        return

    async def run() -> None:
        setattr(state, "classroom_refresh_error", "")
        try:
            await service.refresh_index()
        except Exception as exc:  # noqa: BLE001 - rendered in the status partial
            setattr(state, "classroom_refresh_error", _friendly(exc))

    setattr(state, "classroom_refresh_task", asyncio.create_task(run()))


def _index_context(request: Request, service: ClassroomService) -> dict:
    status = service.course_index.status(service.domain)
    return {
        "count": status.count,
        "age": _human_age(status.age_seconds),
        "stale": status.stale,
        "refreshing": _refreshing(request),
        "error": getattr(request.app.state.gamgui, "classroom_refresh_error", ""),
    }


async def _course_list_response(
    request: Request,
    service: ClassroomService,
    *,
    query: str = "",
    state: str = "",
    cursor: str = "",
) -> HTMLResponse:
    page = await service.search(
        query=query.strip(),
        state=state.strip(),
        cursor=cursor or None,
        refreshing=_refreshing(request),
    )
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_courses.html",
        {
            "page": page,
            "query": query.strip(),
            "state_filter": state.strip().upper(),
        },
    )


@router.get("", response_class=HTMLResponse)
async def classroom_page(
    request: Request, q: str = "", state: str = ""
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return TEMPLATES.TemplateResponse(
            request, "classroom.html", {"connected": False}
        )
    status = service.course_index.status(service.domain)
    if status.stale:
        _schedule_refresh(request, service)
    page = await service.search(
        query=q,
        state=state,
        refreshing=_refreshing(request),
    )
    return TEMPLATES.TemplateResponse(
        request,
        "classroom.html",
        {
            "connected": True,
            "page": page,
            "query": q.strip(),
            "state_filter": state.strip().upper(),
            "states": COURSE_STATES,
            "index": _index_context(request, service),
        },
    )


@router.get("/courses", response_class=HTMLResponse)
async def courses(
    request: Request,
    q: str = "",
    state: str = "",
    cursor: str = "",
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    return await _course_list_response(
        request, service, query=q, state=state, cursor=cursor
    )


@router.post("/index/refresh", response_class=HTMLResponse)
async def refresh_index(request: Request) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    _schedule_refresh(request, service)
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_index_status.html",
        {"index": _index_context(request, service)},
    )


@router.get("/index/status", response_class=HTMLResponse)
async def refresh_status(request: Request) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_index_status.html",
        {"index": _index_context(request, service)},
    )


@router.post("/course", response_class=HTMLResponse)
async def create_course(
    request: Request,
    name: Annotated[str, Form()],
    owner_email: Annotated[str, Form()],
    alias: Annotated[str, Form()] = "",
    section: Annotated[str, Form()] = "",
    room: Annotated[str, Form()] = "",
    description_heading: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    try:
        result = await service.create_course(
            name=name,
            owner_email=owner_email,
            alias=alias,
            section=section,
            room=room,
            description_heading=description_heading,
            description=description,
        )
    except Exception as exc:
        return _action(request, False, _friendly(exc))
    if not result.ok:
        return _action(request, False, _friendly(RuntimeError(result.detail)))
    _schedule_refresh(request, service)
    return _action(
        request,
        True,
        "Provisioned course created. Refreshing the course index in the background.",
    )


async def _detail_response(
    request: Request,
    service: ClassroomService,
    course_id: str,
    *,
    notice: str = "",
    error: str = "",
) -> HTMLResponse:
    try:
        course = await service.connector.get_course(
            course_id,
            include_owner_email=True,
            include_aliases=True,
            best_effort_enrichment=True,
        )
    except Exception as exc:
        return _action(request, False, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_detail.html",
        {"course": course, "notice": notice, "error": error},
    )


@router.get("/course/{course_id}", response_class=HTMLResponse)
async def course_detail(request: Request, course_id: str) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    return await _detail_response(request, service, course_id)


@router.post("/course/{course_id}/metadata", response_class=HTMLResponse)
async def update_metadata(
    request: Request,
    course_id: str,
    name: Annotated[str, Form()],
    section: Annotated[str, Form()] = "",
    room: Annotated[str, Form()] = "",
    description_heading: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    try:
        result, _ = await service.update_metadata(
            course_id,
            name=name,
            section=section,
            room=room,
            description_heading=description_heading,
            description=description,
        )
    except Exception as exc:
        return await _detail_response(
            request, service, course_id, error=_friendly(exc)
        )
    return await _detail_response(
        request,
        service,
        course_id,
        notice="Course details saved." if result.ok else "",
        error="" if result.ok else _friendly(RuntimeError(result.detail)),
    )


@router.post("/course/{course_id}/state/preview", response_class=HTMLResponse)
async def state_preview(
    request: Request,
    course_id: str,
    target_state: Annotated[str, Form()],
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    try:
        course = await service.connector.get_course(course_id)
        target = target_state.strip().upper()
        if target not in COURSE_STATES:
            raise ClassroomValidationError("Choose a valid course state.")
    except Exception as exc:
        return _action(request, False, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_state_preview.html",
        {"course": course, "target_state": target},
    )


@router.post("/course/{course_id}/state", response_class=HTMLResponse)
async def update_state(
    request: Request,
    course_id: str,
    target_state: Annotated[str, Form()],
    confirmed: Annotated[str, Form()] = "",
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    if confirmed != "yes":
        return _action(request, False, "Review the state change before applying it.")
    try:
        result, _ = await service.transition_state(course_id, target_state)
    except Exception as exc:
        return await _detail_response(
            request, service, course_id, error=_friendly(exc)
        )
    verb = "Archived" if target_state.strip().upper() == "ARCHIVED" else "Activated"
    return await _detail_response(
        request,
        service,
        course_id,
        notice=f"{verb} course." if result.ok else "",
        error="" if result.ok else _friendly(RuntimeError(result.detail)),
    )


@router.post("/course/{course_id}/owner/preview", response_class=HTMLResponse)
async def owner_preview(
    request: Request,
    course_id: str,
    target_email: Annotated[str, Form()],
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    try:
        preview = await service.prepare_owner_transfer(course_id, target_email)
    except Exception as exc:
        return _action(request, False, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request, "_classroom_owner_preview.html", {"preview": preview}
    )


@router.post("/course/{course_id}/owner", response_class=HTMLResponse)
async def owner_transfer(
    request: Request,
    course_id: str,
    target_email: Annotated[str, Form()],
    confirm_course_id: Annotated[str, Form()] = "",
    confirm_target_email: Annotated[str, Form()] = "",
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    if confirm_course_id.strip() != course_id:
        return _action(request, False, "Type the exact course ID to confirm ownership transfer.")
    if confirm_target_email.strip().casefold() != target_email.strip().casefold():
        return _action(
            request, False, "Type the exact destination email to confirm ownership transfer."
        )
    try:
        result, _ = await service.transfer_owner(course_id, target_email)
    except Exception as exc:
        return await _detail_response(
            request, service, course_id, error=_friendly(exc)
        )
    return await _detail_response(
        request,
        service,
        course_id,
        notice=f"Course ownership transferred to {target_email.strip().casefold()}."
        if result.ok
        else "",
        error="" if result.ok else result.detail,
    )


async def _roster_response(
    request: Request,
    service: ClassroomService,
    course_id: str,
    role: str,
    *,
    notice: str = "",
    error: str = "",
) -> HTMLResponse:
    try:
        course, members = await service.roster(course_id, role)
    except Exception as exc:
        return _action(request, False, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_roster.html",
        {
            "course": course,
            "role": role.strip().lower(),
            "members": members,
            "notice": notice,
            "error": error,
        },
    )


@router.get("/course/{course_id}/roster", response_class=HTMLResponse)
async def roster(request: Request, course_id: str, role: str = "students") -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    return await _roster_response(request, service, course_id, role)


@router.post("/course/{course_id}/roster/add", response_class=HTMLResponse)
async def roster_add(
    request: Request,
    course_id: str,
    role: Annotated[str, Form()],
    email: Annotated[str, Form()],
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    try:
        result, _ = await service.add_member(course_id, role, email)
    except Exception as exc:
        return await _roster_response(
            request, service, course_id, role, error=_friendly(exc)
        )
    return await _roster_response(
        request,
        service,
        course_id,
        role,
        notice=f"Added {email.strip().casefold()}." if result.ok else "",
        error="" if result.ok else result.detail,
    )


@router.post("/course/{course_id}/roster/remove", response_class=HTMLResponse)
async def roster_remove(
    request: Request,
    course_id: str,
    role: Annotated[str, Form()],
    email: Annotated[str, Form()],
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    try:
        result, _ = await service.remove_member(course_id, role, email)
    except Exception as exc:
        return await _roster_response(
            request, service, course_id, role, error=_friendly(exc)
        )
    return await _roster_response(
        request,
        service,
        course_id,
        role,
        notice=f"Removed {email.strip().casefold()}." if result.ok else "",
        error="" if result.ok else result.detail,
    )


@router.post("/course/{course_id}/roster/preview", response_class=HTMLResponse)
async def roster_preview(
    request: Request,
    course_id: str,
    role: Annotated[str, Form()],
    csv_text: Annotated[str, Form()] = "",
    roster_file: Optional[UploadFile] = File(None),
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    source = csv_text or ""
    if roster_file is not None and roster_file.filename:
        payload = await roster_file.read(_MAX_ROSTER_UPLOAD_BYTES + 1)
        if len(payload) > _MAX_ROSTER_UPLOAD_BYTES:
            return _roster_preview_error(
                request, course_id, role, "Roster CSV must be 1 MB or smaller."
            )
        try:
            source = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            return _roster_preview_error(
                request, course_id, role, "Roster CSV must use UTF-8 text."
            )
    if not source.strip():
        return _roster_preview_error(
            request, course_id, role, "Paste a roster or choose a CSV file."
        )
    try:
        desired = parse_desired_roster(source)
        manifest = await service.plan_roster(course_id, role, desired)
    except Exception as exc:
        return _roster_preview_error(request, course_id, role, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_roster_preview.html",
        {"manifest": manifest, "error": ""},
    )


@router.post("/roster/apply", response_class=HTMLResponse)
async def roster_apply(
    request: Request,
    manifest_id: Annotated[str, Form()],
    confirm_course_id: Annotated[str, Form()] = "",
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    manifest = await asyncio.to_thread(service.manifests.get, manifest_id)
    if manifest is None or manifest.domain != service.domain:
        return _action(request, False, "That roster preview is no longer available.")
    if manifest.removes and confirm_course_id.strip() != manifest.course_id:
        return TEMPLATES.TemplateResponse(
            request,
            "_classroom_roster_preview.html",
            {
                "manifest": manifest,
                "error": "Type the exact course ID before applying roster removals.",
            },
        )
    state = request.app.state.gamgui
    tasks = getattr(state, "classroom_manifest_tasks", None)
    if tasks is None:
        tasks = {}
        setattr(state, "classroom_manifest_tasks", tasks)
    current = tasks.get(manifest.id)
    if current is None or current.done():
        errors = getattr(state, "classroom_manifest_errors", None)
        if errors is None:
            errors = {}
            setattr(state, "classroom_manifest_errors", errors)
        errors.pop(manifest.id, None)

        async def apply() -> None:
            try:
                await service.apply_manifest(manifest.id)
            except Exception as exc:  # noqa: BLE001 - surfaced by the polling partial
                errors[manifest.id] = _friendly(exc)

        tasks[manifest.id] = asyncio.create_task(apply())
    current_manifest = await asyncio.to_thread(service.manifests.get, manifest.id)
    return _manifest_response(request, current_manifest, "")


@router.get("/roster/status", response_class=HTMLResponse)
async def roster_status(request: Request, manifest: str = "") -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    current = await asyncio.to_thread(service.manifests.get, manifest)
    if current is None or current.domain != service.domain:
        return _action(request, False, "That roster operation is no longer available.")
    errors = getattr(request.app.state.gamgui, "classroom_manifest_errors", {})
    return _manifest_response(request, current, errors.get(manifest, ""))


@router.post("/roster/replan", response_class=HTMLResponse)
async def roster_replan(
    request: Request, manifest_id: Annotated[str, Form()]
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    try:
        manifest = await service.replan_manifest(manifest_id)
    except Exception as exc:
        return _action(request, False, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_roster_preview.html",
        {"manifest": manifest, "error": ""},
    )


@router.get("/user", response_class=HTMLResponse)
async def user_classroom(request: Request, email: str = "") -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _action(request, False, _NOT_CONNECTED)
    try:
        teaching, enrolled = await service.courses_for_user(email)
    except Exception as exc:
        return _action(request, False, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_user.html",
        {"email": email.strip().casefold(), "teaching": teaching, "enrolled": enrolled},
    )


def _manifest_response(
    request: Request, manifest, task_error: str
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_manifest.html",
        {"manifest": manifest, "task_error": task_error},
    )


def _roster_preview_error(
    request: Request, course_id: str, role: str, error: str
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_roster_preview.html",
        {
            "manifest": None,
            "course_id": course_id,
            "role": role,
            "error": error,
        },
    )


def _action(
    request: Request, ok: bool, message: str, details: str = ""
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_action.html",
        {"ok": ok, "message": message, "details": details},
    )
