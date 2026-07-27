"""Lazy user-Drive administration routes."""

from __future__ import annotations

import asyncio
import webbrowser
from typing import Annotated, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from ...core.drive import DriveAPIError, DriveSafetyError
from ...core.drive.models import OperationTarget
from ..activity import ADMIN_ACTIVITY_BUSY_MESSAGE, try_acquire_admin_activity
from ..jobs import start_job
from ..server import TEMPLATES

router = APIRouter(prefix="/drive")

_NOT_CONNECTED = "Drive administration is unavailable. Reconnect the Workspace domain."


def _service(request: Request):
    return getattr(request.app.state.gamgui, "drive_service", None)


def _friendly(exc: Exception) -> str:
    if isinstance(exc, DriveAPIError):
        return exc.user_message
    if isinstance(exc, (DriveSafetyError, ValueError, PermissionError)):
        return str(exc)
    return "Something went wrong talking to Google Drive."


def _error(request: Request, message: str) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "_action_result.html", {"ok": False, "message": message}
    )


def _rows(
    request: Request,
    *,
    email: str,
    page=None,
    q: str = "",
    mime_type: str = "",
    modified_after: str = "",
    error: str = "",
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "_drive_rows.html",
        {
            "email": email,
            "page": page,
            "q": q,
            "mime_type": mime_type,
            "modified_after": modified_after,
            "error": error,
        },
    )


async def _list(
    service,
    email: str,
    *,
    q: str = "",
    mime_type: str = "",
    modified_after: str = "",
    cursor: Optional[str] = None,
):
    return await service.list_owned_files(
        email,
        search=q.strip(),
        mime_type=mime_type.strip(),
        modified_after=modified_after.strip(),
        cursor=cursor,
        page_size=50,
    )


@router.get("/user", response_class=HTMLResponse)
async def user_panel(request: Request, email: str) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    email = email.strip()
    try:
        page = await _list(service, email)
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request,
            "_drive_panel.html",
            {"email": email, "page": None, "error": _friendly(exc)},
        )
    return TEMPLATES.TemplateResponse(
        request, "_drive_panel.html", {"email": email, "page": page, "error": ""}
    )


@router.get("/files", response_class=HTMLResponse)
async def files(
    request: Request,
    email: str,
    q: str = "",
    mime_type: str = "",
    modified_after: str = "",
    cursor: str = "",
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _rows(request, email=email, error=_NOT_CONNECTED)
    try:
        page = await _list(
            service,
            email,
            q=q,
            mime_type=mime_type,
            modified_after=modified_after,
            cursor=cursor or None,
        )
    except Exception as exc:
        return _rows(
            request,
            email=email,
            q=q,
            mime_type=mime_type,
            modified_after=modified_after,
            error=_friendly(exc),
        )
    return _rows(
        request,
        email=email,
        page=page,
        q=q,
        mime_type=mime_type,
        modified_after=modified_after,
    )


async def _detail_context(request: Request, email: str, file_id: str, notice: str = ""):
    service = _service(request)
    file = await service.get_file(email, file_id)
    return {
        "email": email,
        "file": file,
        "notice": notice,
        "error": "",
    }


@router.get("/file", response_class=HTMLResponse)
async def file_detail(request: Request, email: str, file_id: str) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    try:
        ctx = await _detail_context(request, email.strip(), file_id.strip())
    except Exception as exc:
        return _error(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_drive_detail.html", ctx)


@router.post("/metadata", response_class=HTMLResponse)
async def update_metadata(
    request: Request,
    email: Annotated[str, Form()],
    file_id: Annotated[str, Form()],
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    starred: Annotated[str, Form()] = "",
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "drive-metadata-update")
    if lease is None:
        return _error(request, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        await service.update_metadata(
            email.strip(),
            file_id.strip(),
            name=name,
            description=description,
            starred=starred == "on",
        )
        ctx = await _detail_context(
            request, email.strip(), file_id.strip(), "File details updated."
        )
    except Exception as exc:
        return _error(request, _friendly(exc))
    finally:
        lease.release()
    return TEMPLATES.TemplateResponse(request, "_drive_detail.html", ctx)


async def _permission_context(
    request: Request, email: str, file_id: str, notice: str = "", error: str = ""
):
    service = _service(request)
    permissions = await service.list_permissions(email, file_id)
    return {
        "email": email,
        "file_id": file_id,
        "permissions": permissions,
        "notice": notice,
        "error": error,
    }


@router.get("/permissions", response_class=HTMLResponse)
async def permissions(request: Request, email: str, file_id: str) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    try:
        ctx = await _permission_context(request, email.strip(), file_id.strip())
    except Exception as exc:
        return _error(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(request, "_drive_permissions.html", ctx)


@router.post("/permissions/add", response_class=HTMLResponse)
async def add_permission(
    request: Request,
    email: Annotated[str, Form()],
    file_id: Annotated[str, Form()],
    target: Annotated[str, Form()],
    principal_type: Annotated[str, Form()] = "user",
    role: Annotated[str, Form()] = "reader",
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "drive-permission-add")
    if lease is None:
        return _error(request, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        await service.add_permission(
            email.strip(),
            file_id.strip(),
            email=target.strip(),
            principal_type=principal_type,
            role=role,
        )
        ctx = await _permission_context(
            request, email.strip(), file_id.strip(), f"Shared with {target.strip()}."
        )
    except Exception as exc:
        try:
            ctx = await _permission_context(
                request,
                email.strip(),
                file_id.strip(),
                error=_friendly(exc),
            )
        except Exception:
            return _error(request, _friendly(exc))
    finally:
        lease.release()
    return TEMPLATES.TemplateResponse(request, "_drive_permissions.html", ctx)


@router.post("/permissions/update", response_class=HTMLResponse)
async def update_permission(
    request: Request,
    email: Annotated[str, Form()],
    file_id: Annotated[str, Form()],
    permission_id: Annotated[str, Form()],
    role: Annotated[str, Form()],
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "drive-permission-update")
    if lease is None:
        return _error(request, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        await service.update_permission(
            email.strip(), file_id.strip(), permission_id.strip(), role
        )
        ctx = await _permission_context(
            request, email.strip(), file_id.strip(), "Access level updated."
        )
    except Exception as exc:
        try:
            ctx = await _permission_context(
                request,
                email.strip(),
                file_id.strip(),
                error=_friendly(exc),
            )
        except Exception:
            return _error(request, _friendly(exc))
    finally:
        lease.release()
    return TEMPLATES.TemplateResponse(request, "_drive_permissions.html", ctx)


@router.post("/permissions/remove", response_class=HTMLResponse)
async def remove_permission(
    request: Request,
    email: Annotated[str, Form()],
    file_id: Annotated[str, Form()],
    permission_id: Annotated[str, Form()],
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "drive-permission-remove")
    if lease is None:
        return _error(request, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        await service.remove_permission(
            email.strip(), file_id.strip(), permission_id.strip()
        )
        ctx = await _permission_context(
            request, email.strip(), file_id.strip(), "Access removed."
        )
    except Exception as exc:
        try:
            ctx = await _permission_context(
                request,
                email.strip(),
                file_id.strip(),
                error=_friendly(exc),
            )
        except Exception:
            return _error(request, _friendly(exc))
    finally:
        lease.release()
    return TEMPLATES.TemplateResponse(request, "_drive_permissions.html", ctx)


@router.get("/preview", response_class=HTMLResponse)
async def preview_frame(request: Request, email: str, file_id: str) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    try:
        file = await service.get_file(email.strip(), file_id.strip())
        # Validate before rendering an iframe so unsafe types never become inline candidates.
        if not file.is_google_doc and file.mime_type not in {
            "application/pdf",
            "image/png",
            "image/jpeg",
            "image/webp",
            "text/plain",
        }:
            raise DriveSafetyError(
                "This file type is not safe to preview inside GamGUI."
            )
        if not file.can_download:
            raise DriveSafetyError(
                "Google does not allow this account to download that file."
            )
    except Exception as exc:
        return _error(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request, "_drive_preview.html", {"email": email, "file": file}
    )


@router.get("/preview/content")
async def preview_content(request: Request, email: str, file_id: str) -> Response:
    service = _service(request)
    if service is None:
        return Response(_NOT_CONNECTED, status_code=503, media_type="text/plain")
    try:
        preview = await service.preview(email.strip(), file_id.strip())
    except Exception as exc:
        return Response(
            _friendly(exc),
            status_code=422,
            media_type="text/plain",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="{preview.filename}"',
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:",
    }
    return Response(preview.body, media_type=preview.media_type, headers=headers)


@router.post("/open", response_class=HTMLResponse)
async def open_in_drive(
    request: Request,
    email: Annotated[str, Form()],
    file_id: Annotated[str, Form()],
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    try:
        file = await service.get_file(email.strip(), file_id.strip())
        parsed = urlparse(file.web_view_link)
        if parsed.scheme != "https" or not (
            parsed.hostname == "google.com"
            or str(parsed.hostname or "").endswith(".google.com")
        ):
            raise DriveSafetyError(
                "Google did not return a safe edit link for this file."
            )
        opened = await asyncio.to_thread(webbrowser.open, file.web_view_link, 2)
        if not opened:
            raise DriveSafetyError("The system browser could not be opened.")
    except Exception as exc:
        return _error(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request,
        "_action_result.html",
        {"ok": True, "message": "Opened in Google Drive."},
    )


@router.post("/ownership/preview", response_class=HTMLResponse)
async def ownership_preview(
    request: Request,
    email: Annotated[str, Form()],
    file_id: Annotated[str, Form()],
    destination: Annotated[str, Form()],
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    try:
        file, principal = await service.validate_single_transfer(
            email.strip(), file_id.strip(), destination.strip()
        )
    except Exception as exc:
        return _error(request, _friendly(exc))
    return TEMPLATES.TemplateResponse(
        request,
        "_drive_transfer_confirm.html",
        {
            "email": email.strip(),
            "file": file,
            "destination": principal.email,
            "error": "",
        },
    )


@router.post("/ownership/apply", response_class=HTMLResponse)
async def ownership_apply(
    request: Request,
    email: Annotated[str, Form()],
    file_id: Annotated[str, Form()],
    destination: Annotated[str, Form()],
    confirmation: Annotated[str, Form()],
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    lease = try_acquire_admin_activity(
        request.app.state.gamgui, "drive-single-owner-transfer"
    )
    if lease is None:
        return _error(request, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        result = await service.transfer_file_ownership(
            email.strip(),
            file_id.strip(),
            destination.strip(),
            confirmation=confirmation,
        )
    except Exception as exc:
        return _error(request, _friendly(exc))
    finally:
        lease.release()
    message = result.detail
    if result.residual_access:
        message += f" Warning: {result.residual_access}"
    return TEMPLATES.TemplateResponse(
        request,
        "_action_result.html",
        {"ok": result.ok, "message": message},
    )


@router.post("/manifest/folder", response_class=HTMLResponse)
async def folder_manifest(
    request: Request,
    email: Annotated[str, Form()],
    file_id: Annotated[str, Form()],
    destination: Annotated[str, Form()],
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "drive-folder-manifest-plan")
    if lease is None:
        return _error(request, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        manifest = await service.plan_folder_transfer(
            email.strip(), file_id.strip(), destination.strip()
        )
    except Exception as exc:
        return _error(request, _friendly(exc))
    finally:
        lease.release()
    return TEMPLATES.TemplateResponse(
        request, "_drive_manifest.html", {"manifest": manifest, "error": ""}
    )


@router.post("/manifest/classroom", response_class=HTMLResponse)
async def classroom_manifest(
    request: Request,
    teacher: Annotated[str, Form()],
    folder_id: Annotated[str, Form()],
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "drive-classroom-manifest-plan")
    if lease is None:
        return _error(request, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        manifest = await service.plan_classroom_claim(
            teacher.strip(), folder_id.strip()
        )
    except Exception as exc:
        return _error(request, _friendly(exc))
    finally:
        lease.release()
    return TEMPLATES.TemplateResponse(
        request, "_drive_manifest.html", {"manifest": manifest, "error": ""}
    )


async def _run_manifest_job(
    job, service, manifest_id: str, confirmation: str, lease=None
) -> None:
    async def progress(done: int, total: int, target: OperationTarget) -> None:
        job.done = done
        job.current = target.name or target.file_id
        if target.status == "succeeded":
            job.applied += 1
        elif target.status == "failed":
            job.failed.append(target.name or target.file_id)

    try:
        await service.apply_manifest(
            manifest_id,
            confirmation=confirmation,
            claimed=True,
            progress=progress,
        )
    except Exception as exc:
        job.error = str(exc)
    finally:
        job.current = ""
        job.finished = True
        if lease is not None:
            lease.release()


@router.post("/manifest/apply", response_class=HTMLResponse)
async def manifest_apply(
    request: Request,
    manifest_id: Annotated[str, Form()],
    confirmation: Annotated[str, Form()],
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "drive-ownership-manifest")
    if lease is None:
        return _error(request, ADMIN_ACTIVITY_BUSY_MESSAGE)
    try:
        manifest = service.claim_manifest(
            manifest_id.strip(),
            confirmation=confirmation.strip(),
        )
    except Exception as exc:
        lease.release()
        manifest = service.operations.get(manifest_id.strip(), service.domain)
        if manifest is None:
            return _error(request, _friendly(exc))
        return TEMPLATES.TemplateResponse(
            request,
            "_drive_manifest.html",
            {"manifest": manifest, "error": _friendly(exc)},
        )
    if manifest.status == "completed" or manifest.remaining == 0:
        lease.release()
        return TEMPLATES.TemplateResponse(
            request,
            "_drive_job.html",
            {"job": None, "manifest": manifest},
        )
    try:
        job = start_job(state.jobs, manifest.remaining)
        job.task = asyncio.create_task(
            _run_manifest_job(
                job, service, manifest.id, confirmation.strip(), lease
            )
        )
    except Exception:
        service.interrupt_manifest_claim(manifest.id)
        lease.release()
        if "job" in locals():
            state.jobs.pop(job.id, None)
        raise
    return TEMPLATES.TemplateResponse(
        request, "_drive_job.html", {"job": job, "manifest": manifest}
    )


@router.get("/manifest/status", response_class=HTMLResponse)
async def manifest_status(
    request: Request, manifest_id: str, job: str = ""
) -> HTMLResponse:
    service = _service(request)
    if service is None:
        return _error(request, _NOT_CONNECTED)
    manifest = service.operations.get(manifest_id.strip(), service.domain)
    if manifest is None:
        return _error(request, "That ownership manifest was not found for this domain.")
    running = request.app.state.gamgui.jobs.get(job) if job else None
    return TEMPLATES.TemplateResponse(
        request, "_drive_job.html", {"job": running, "manifest": manifest}
    )
