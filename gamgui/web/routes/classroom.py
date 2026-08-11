"""Classroom course, lifecycle, owner, and roster administration."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Annotated, Any, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from ...core.classroom.index import CourseIndex, default_course_index_path
from ...core.classroom.manifests import (
    RosterManifestStore,
    default_roster_manifest_path,
)
from ...core.classroom.models import (
    COURSE_STATES,
    CourseParticipant,
    parse_desired_roster,
)
from ...core.classroom.service import ClassroomService, ClassroomValidationError
from ...core.gam.errors import GAMError
from ..activity import ADMIN_ACTIVITY_BUSY_MESSAGE, try_acquire_admin_activity
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
    lease = try_acquire_admin_activity(state, "classroom-index-refresh")
    if lease is None:
        setattr(state, "classroom_refresh_error", ADMIN_ACTIVITY_BUSY_MESSAGE)
        return

    async def run() -> None:
        setattr(state, "classroom_refresh_error", "")
        try:
            await service.refresh_index()
        except Exception as exc:  # noqa: BLE001 - rendered in the status partial
            setattr(state, "classroom_refresh_error", _friendly(exc))
        finally:
            lease.release()

    try:
        setattr(state, "classroom_refresh_task", asyncio.create_task(run()))
    except Exception:
        lease.release()
        raise


def _index_context(request: Request, service: ClassroomService) -> dict:
    status = service.course_index.status(service.domain)
    return {
        "count": status.count,
        "age": _human_age(status.age_seconds),
        "stale": status.stale,
        "refreshing": _refreshing(request),
        "error": getattr(request.app.state.gamgui, "classroom_refresh_error", ""),
    }


def _plain_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _plain_value(asdict(value))
    if isinstance(value, dict):
        return {key: _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    return value


def _plain(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if is_dataclass(value) and not isinstance(value, type):
        return _plain_value(asdict(value))
    if isinstance(value, dict):
        return _plain_value(value)
    return _plain_value({
        name: getattr(value, name)
        for name in dir(value)
        if not name.startswith("_") and not callable(getattr(value, name, None))
    })


async def _classroom_workspace_context(request: Request) -> dict[str, Any]:
    """Project durable local state into the friendly Classroom workspaces."""

    state = request.app.state.gamgui
    roster = getattr(state, "oneroster_service", None)
    context: dict[str, Any] = {
        "available": roster is not None,
        "snapshot": {},
        "manifest": {},
        "progress": {},
        "gate": {"state": "CLOSED"},
        "phase": "setup",
        "risk_tone": "quiet",
        "risk_label": "No active import",
        "next_title": "Start a guided import",
        "next_detail": "GamGUI will ask one small question at a time before anything can change.",
        "next_href": "/classroom/imports",
        "next_label": "Begin guided setup",
    }
    if roster is None:
        context.update(
            risk_label="Guided imports are not available",
            next_title="Turn on OneRoster Classroom",
            next_detail="Install or enable the Classroom OneRoster component first.",
            next_href="/components",
            next_label="Open component settings",
        )
        return context

    dashboard_method = getattr(roster, "dashboard", None)
    dashboard = _plain(await asyncio.to_thread(dashboard_method)) if callable(dashboard_method) else {}
    snapshot = _plain(dashboard.get("latest"))
    context["snapshot"] = snapshot
    gate_method = getattr(roster, "get_gate", None)
    if callable(gate_method):
        context["gate"] = _plain(await asyncio.to_thread(gate_method))

    manifest_method = getattr(roster, "latest_manifest_header", None)
    manifest = {}
    if callable(manifest_method):
        value = await asyncio.to_thread(
            manifest_method,
            str(snapshot.get("id", "") or "") or None,
        )
        manifest = _plain(value)
    context["manifest"] = manifest
    manifest_id = str(manifest.get("id", "") or "")
    progress_method = getattr(roster, "get_execution_progress", None)
    if manifest_id and callable(progress_method):
        try:
            context["progress"] = _plain(
                await asyncio.to_thread(progress_method, manifest_id)
            )
        except (KeyError, TypeError):
            pass

    snapshot_state = str(snapshot.get("state", "") or "").casefold()
    manifest_status = str(manifest.get("status", "") or "").casefold()
    if manifest_status in {"running", "pause_requested", "paused"}:
        context.update(
            phase="in_progress",
            risk_tone="warn" if manifest_status != "running" else "good",
            risk_label=(
                "Pausing after the current checked batch"
                if manifest_status == "pause_requested"
                else "Import paused safely"
                if manifest_status == "paused"
                else "Import is running"
            ),
            next_title="Watch the checked batches",
            next_detail="GamGUI finishes and verifies each small batch before moving on.",
            next_href=f"/classroom/imports/manifest/{manifest_id}",
            next_label="Open live progress",
        )
    elif manifest_status in {"recovery_required", "interrupted", "failed", "stale"}:
        context.update(
            phase="results",
            risk_tone="danger",
            risk_label="A person needs to review what happened",
            next_title="Open the protected recovery workspace",
            next_detail="GamGUI kept the evidence and will not repeat uncertain work.",
            next_href="/classroom/recovery",
            next_label="Review recovery",
        )
    elif manifest_status == "completed":
        context.update(
            phase="results",
            risk_tone="good",
            risk_label="Last import finished and was checked",
            next_title="Read the final results",
            next_detail="See what changed, what was skipped, and whether anything needs follow-up.",
            next_href=f"/classroom/imports/manifest/{manifest_id}",
            next_label="See final results",
        )
    elif manifest_status in {"planned", "awaiting_students"}:
        context.update(
            phase="review",
            risk_tone="warn" if manifest_status == "awaiting_students" else "good",
            risk_label=(
                "Student changes are safely held"
                if manifest_status == "awaiting_students"
                else "The checked plan is ready for your review"
            ),
            next_title="Review the exact impact before starting",
            next_detail="Nothing changes until you confirm the saved plan.",
            next_href=f"/classroom/imports/manifest/{manifest_id}",
            next_label="Review and start",
        )
    elif snapshot:
        blocked = int(snapshot.get("blocking_issue_count", 0) or 0)
        context.update(
            phase="setup",
            risk_tone="danger" if blocked else "quiet",
            risk_label=(
                f"{blocked} source problem{'s' if blocked != 1 else ''} need help"
                if blocked
                else "Setup is waiting for you"
            ),
            next_title="Answer the next setup question",
            next_detail="The source stays local while you review names, dates, and people.",
            next_href=f"/classroom/imports/import/{snapshot.get('id', '')}",
            next_label="Continue guided setup",
        )
    return context


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
            request, "classroom_dashboard.html", {"connected": False}
        )
    status = service.course_index.status(service.domain)
    if status.stale:
        _schedule_refresh(request, service)
    return TEMPLATES.TemplateResponse(
        request,
        "classroom_dashboard.html",
        {
            "connected": True,
            "index": _index_context(request, service),
            "workspace": await _classroom_workspace_context(request),
        },
    )


@router.get("/courses/manage", response_class=HTMLResponse)
async def course_admin_page(
    request: Request, q: str = "", state: str = ""
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return TEMPLATES.TemplateResponse(request, "classroom.html", {"connected": False})
    status = service.course_index.status(service.domain)
    if status.stale:
        _schedule_refresh(request, service)
    page = await service.search(query=q, state=state, refreshing=_refreshing(request))
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


@router.get("/monitoring", response_class=HTMLResponse)
async def classroom_monitoring(request: Request) -> HTMLResponse:
    service = _service(request)
    return TEMPLATES.TemplateResponse(
        request,
        "classroom_monitoring.html",
        {
            "connected": service is not None,
            "workspace": await _classroom_workspace_context(request),
        },
    )


@router.get("/recovery", response_class=HTMLResponse)
async def classroom_recovery(request: Request) -> HTMLResponse:
    service = _service(request)
    return TEMPLATES.TemplateResponse(
        request,
        "classroom_recovery.html",
        {
            "connected": service is not None,
            "workspace": await _classroom_workspace_context(request),
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
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "classroom-course-create")
    if lease is None:
        return _action(request, False, ADMIN_ACTIVITY_BUSY_MESSAGE)
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
    finally:
        lease.release()
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
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "classroom-course-metadata")
    if lease is None:
        return _action(request, False, ADMIN_ACTIVITY_BUSY_MESSAGE)
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
    finally:
        lease.release()
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
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "classroom-course-state")
    if lease is None:
        return _action(request, False, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        result, _ = await service.transition_state(course_id, target_state)
    except Exception as exc:
        return await _detail_response(
            request, service, course_id, error=_friendly(exc)
        )
    finally:
        lease.release()
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
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "classroom-course-owner")
    if lease is None:
        return _action(request, False, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        result, _ = await service.transfer_owner(course_id, target_email)
    except Exception as exc:
        return await _detail_response(
            request, service, course_id, error=_friendly(exc)
        )
    finally:
        lease.release()
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
    unresolved_member_count = sum(
        not bool(getattr(member, "identity_resolved", member.label))
        for member in members
    )
    visible_members = [
        member
        for member in members
        if bool(getattr(member, "identity_resolved", member.label))
    ]
    owner_from_course_details = False
    if role.strip().casefold() == "teachers" and (course.owner_id or course.owner_email):
        owner_present = any(
            (course.owner_id and member.user_id == course.owner_id)
            or (course.owner_email and member.email == course.owner_email)
            for member in visible_members
        )
        if not owner_present:
            visible_members.insert(
                0,
                CourseParticipant(
                    course_id=course.id,
                    email=course.owner_email,
                    user_id=course.owner_id,
                    role="teachers",
                    full_name="Course owner",
                    raw={"owner_from_course_details": True},
                ),
            )
            owner_from_course_details = True
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_roster.html",
        {
            "course": course,
            "role": role.strip().lower(),
            "members": visible_members,
            "unresolved_member_count": unresolved_member_count,
            "owner_from_course_details": owner_from_course_details,
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
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "classroom-roster-member")
    if lease is None:
        return _action(request, False, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        result, _ = await service.add_member(course_id, role, email)
    except Exception as exc:
        return await _roster_response(
            request, service, course_id, role, error=_friendly(exc)
        )
    finally:
        lease.release()
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
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "classroom-roster-member")
    if lease is None:
        return _action(request, False, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        result, _ = await service.remove_member(course_id, role, email)
    except Exception as exc:
        return await _roster_response(
            request, service, course_id, role, error=_friendly(exc)
        )
    finally:
        lease.release()
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
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "classroom-roster-plan")
    if lease is None:
        return _roster_preview_error(
            request, course_id, role, ADMIN_ACTIVITY_BUSY_MESSAGE
        )
    try:
        desired = parse_desired_roster(source)
        manifest = await service.plan_roster(course_id, role, desired)
    except Exception as exc:
        return _roster_preview_error(request, course_id, role, _friendly(exc))
    finally:
        lease.release()
    return TEMPLATES.TemplateResponse(
        request,
        "_classroom_roster_preview.html",
        {"manifest": manifest, "error": ""},
    )


async def _run_roster_job(
    service: ClassroomService,
    manifest_id: str,
    errors: dict[str, str],
    lease,
) -> None:
    try:
        await service.apply_manifest(manifest_id)
    except Exception as exc:  # noqa: BLE001 - surfaced by the polling partial
        errors[manifest_id] = _friendly(exc)
    finally:
        lease.release()


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
        lease = try_acquire_admin_activity(state, "classroom-roster-apply")
        if lease is None:
            return _action(request, False, ADMIN_ACTIVITY_BUSY_MESSAGE)
        errors = getattr(state, "classroom_manifest_errors", None)
        if errors is None:
            errors = {}
            setattr(state, "classroom_manifest_errors", errors)
        errors.pop(manifest.id, None)

        try:
            tasks[manifest.id] = asyncio.create_task(
                _run_roster_job(service, manifest.id, errors, lease)
            )
        except Exception:
            lease.release()
            raise
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
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "classroom-roster-plan")
    if lease is None:
        return _action(request, False, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        manifest = await service.replan_manifest(manifest_id)
    except Exception as exc:
        return _action(request, False, _friendly(exc))
    finally:
        lease.release()
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
