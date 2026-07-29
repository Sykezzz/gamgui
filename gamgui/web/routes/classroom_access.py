"""Guided Classroom Teachers entitlement policy UI."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from ...core.classroom.models import normalize_email
from ...core.classroom_access import (
    CSVMode,
    EntitlementPlan,
    EntitlementPolicy,
    EntitlementValidationError,
    PolicyStatus,
    SourceMode,
    parse_email_lines,
)
from ..activity import ADMIN_ACTIVITY_BUSY_MESSAGE, try_acquire_admin_activity
from ..server import TEMPLATES

router = APIRouter(prefix="/classroom/access")
_PAGE = "classroom_access.html"
_CSV_UPLOAD_MAX = 10 * 1024 * 1024


async def _context(
    request: Request,
    *,
    policy: Optional[EntitlementPolicy] = None,
    plan: Optional[EntitlementPlan] = None,
    error: str = "",
    notice: str = "",
) -> dict:
    state = request.app.state.gamgui
    connected = state.connector is not None and bool(state.audit_domain)
    groups = ()
    if connected:
        state.ensure_workspace_services()
        if policy is None and state.entitlement_store is not None:
            policy = await asyncio.to_thread(
                state.entitlement_store.policy_for_domain, state.audit_domain
            )
        try:
            page = await state.directory_groups(limit=50)
            groups = tuple(page.items)
        except Exception:
            groups = ()
    if policy is not None and plan is None and policy.pending_plan_id:
        plan = await asyncio.to_thread(
            state.entitlement_store.get_plan, policy.pending_plan_id
        )
    last_run = "Not yet"
    next_run = "Disabled"
    if policy is not None:
        if policy.last_run_at:
            last_run = datetime.fromtimestamp(policy.last_run_at).strftime(
                "%Y-%m-%d %H:%M"
            )
        if policy.schedule_enabled and policy.status == PolicyStatus.ACTIVE.value:
            local_now = datetime.now()
            scheduled = local_now.replace(
                hour=policy.schedule_hour,
                minute=policy.schedule_minute,
                second=0,
                microsecond=0,
            )
            if scheduled <= local_now:
                scheduled += timedelta(days=1)
            next_run = scheduled.strftime("%Y-%m-%d %H:%M")
    return {
        "connected": connected,
        "domain": state.audit_domain,
        "policy": policy,
        "plan": plan,
        "groups": groups,
        "error": error,
        "notice": notice,
        "last_run": last_run,
        "next_run": next_run,
        "source_group": SourceMode.GOOGLE_GROUP.value,
        "source_csv": SourceMode.CSV.value,
        "csv_upload": CSVMode.UPLOAD.value,
        "csv_watch": CSVMode.WATCH.value,
        "schedule_available": getattr(
            state.entitlement_scheduler, "platform", ""
        )
        == "darwin",
    }


async def _render(request: Request, **kwargs) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, _PAGE, await _context(request, **kwargs)
    )


@router.get("", response_class=HTMLResponse)
async def access_page(request: Request) -> HTMLResponse:
    return await _render(request)


@router.post("/save", response_class=HTMLResponse)
async def save_and_preview(
    request: Request,
    target_group: Annotated[str, Form()],
    source_mode: Annotated[str, Form()],
    source_group: Annotated[str, Form()] = "",
    csv_mode: Annotated[str, Form()] = CSVMode.UPLOAD.value,
    watched_path: Annotated[str, Form()] = "",
    exception_users: Annotated[str, Form()] = "",
    exception_groups: Annotated[str, Form()] = "",
    schedule_hour: Annotated[int, Form()] = 2,
    schedule_minute: Annotated[int, Form()] = 0,
    target_confirmed: Annotated[str, Form()] = "",
    schedule_enabled: Annotated[str, Form()] = "",
    csv_file: Annotated[Optional[UploadFile], File()] = None,
) -> HTMLResponse:
    state = request.app.state.gamgui
    if state.connector is None or not state.audit_domain:
        return await _render(request, error="Connect a Workspace domain first.")
    state.ensure_workspace_services()
    store = state.entitlement_store
    service = state.entitlement_service
    existing = await asyncio.to_thread(
        store.policy_for_domain, state.audit_domain
    )
    try:
        if target_confirmed != "yes":
            raise ValueError(
                "Confirm that the selected target is the domain's special Classroom Teachers group."
            )
        target = normalize_email(target_group)
        source = normalize_email(source_group)
        if source_mode not in {mode.value for mode in SourceMode}:
            raise ValueError("Choose a synchronized group or CSV source.")
        if csv_mode not in {mode.value for mode in CSVMode}:
            raise ValueError("Choose an uploaded snapshot or watched CSV path.")
        if not 0 <= int(schedule_hour) <= 23 or not 0 <= int(schedule_minute) <= 59:
            raise ValueError("Schedule time must be a valid local time.")

        csv_emails = existing.csv_emails if existing is not None else ()
        if source_mode == SourceMode.CSV.value and csv_mode == CSVMode.UPLOAD.value:
            if csv_file is not None and csv_file.filename:
                content = await csv_file.read(_CSV_UPLOAD_MAX + 1)
                if len(content) > _CSV_UPLOAD_MAX:
                    raise ValueError("The CSV upload is larger than 10 MB.")
                try:
                    text = content.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise ValueError("The CSV must be UTF-8 text.") from exc
                csv_emails = parse_email_lines(text)
            elif not csv_emails:
                raise ValueError("Choose a CSV file to preview.")
        if source_mode == SourceMode.CSV.value and csv_mode == CSVMode.WATCH.value:
            csv_emails = existing.csv_emails if existing is not None else ()
            if not watched_path.strip():
                raise ValueError("Enter the absolute path of the watched CSV export.")

        candidate = EntitlementPolicy(
            id=existing.id if existing is not None else "",
            domain=state.audit_domain,
            target_group=target,
            source_mode=source_mode,
            source_group=source if source_mode == SourceMode.GOOGLE_GROUP.value else "",
            csv_mode=csv_mode,
            csv_emails=tuple(csv_emails),
            watch_path=(
                watched_path.strip()
                if source_mode == SourceMode.CSV.value
                and csv_mode == CSVMode.WATCH.value
                else ""
            ),
            exception_users=parse_email_lines(exception_users),
            exception_groups=parse_email_lines(exception_groups),
            connector_identity=service.connector_identity,
            status=existing.status if existing is not None else PolicyStatus.DRAFT.value,
            schedule_enabled=schedule_enabled == "yes",
            schedule_hour=int(schedule_hour),
            schedule_minute=int(schedule_minute),
            approved_config_hash=(
                existing.approved_config_hash if existing is not None else ""
            ),
            approved_source_hash=(
                existing.approved_source_hash if existing is not None else ""
            ),
            last_run_status=existing.last_run_status if existing is not None else "",
            last_run_message=existing.last_run_message if existing is not None else "",
            last_run_at=existing.last_run_at if existing is not None else 0.0,
            created_at=existing.created_at if existing is not None else 0.0,
        )
        policy = await asyncio.to_thread(store.save_policy, candidate)
        plan = await service.plan(policy, approval_required=True)
        policy = await asyncio.to_thread(store.get_policy, policy.id)
        return await _render(
            request,
            policy=policy,
            plan=plan,
            notice="Policy saved. Review the exact live membership plan below.",
        )
    except (EntitlementValidationError, OSError, ValueError) as exc:
        draft = existing
        if existing is not None:
            draft = replace(existing)
        return await _render(request, policy=draft, error=str(exc))
    finally:
        if csv_file is not None:
            await csv_file.close()


@router.post("/approve", response_class=HTMLResponse)
async def approve_plan(
    request: Request,
    policy_id: Annotated[str, Form()],
    plan_id: Annotated[str, Form()],
) -> HTMLResponse:
    state = request.app.state.gamgui
    if state.connector is None:
        return await _render(request, error="Connect a Workspace domain first.")
    state.ensure_workspace_services()
    store = state.entitlement_store
    policy = await asyncio.to_thread(store.get_policy, policy_id)
    if policy is None or policy.domain != state.audit_domain:
        return await _render(request, error="That policy is no longer available.")
    lease = try_acquire_admin_activity(state, "classroom-teacher-entitlement")
    if lease is None:
        return await _render(
            request,
            policy=policy,
            error=ADMIN_ACTIVITY_BUSY_MESSAGE,
        )
    try:
        selected_plan = await asyncio.to_thread(store.get_plan, plan_id)
        if (
            selected_plan is None
            or selected_plan.policy_id != policy.id
            or selected_plan.domain != state.audit_domain
        ):
            raise EntitlementValidationError(
                "That preview does not belong to the active policy."
            )
        if selected_plan.status == "interrupted":
            result = await state.entitlement_service.resume_interrupted(plan_id)
            if result is None:
                raise EntitlementValidationError(
                    "The source or live membership changed after interruption. "
                    "Create a fresh preview."
                )
        else:
            if not await asyncio.to_thread(store.approve_plan, plan_id):
                raise EntitlementValidationError(
                    "That preview cannot be reused. Create a fresh preview."
                )
            result = await state.entitlement_service.apply(plan_id)
        policy = await asyncio.to_thread(store.get_policy, policy_id)
        notice = (
            "Classroom Teachers membership now matches the approved source."
            if result.status == "completed"
            else "The operation finished with residual changes that require review."
        )
        if result.status == "completed" and policy.schedule_enabled:
            try:
                await asyncio.to_thread(state.entitlement_scheduler.install, policy)
            except RuntimeError as exc:
                policy = await asyncio.to_thread(
                    store.record_policy_result,
                    policy.id,
                    status="held",
                    message=str(exc),
                )
                return await _render(
                    request,
                    policy=policy,
                    plan=result,
                    error=(
                        "Membership was reconciled, but the background schedule "
                        f"was not installed: {exc}"
                    ),
                )
        elif result.status == "completed":
            try:
                await asyncio.to_thread(
                    state.entitlement_scheduler.disable, policy.id
                )
            except OSError as exc:
                return await _render(
                    request,
                    policy=policy,
                    plan=result,
                    error=(
                        "Membership was reconciled, but the previous background "
                        f"schedule could not be removed: {exc}"
                    ),
                )
        return await _render(
            request, policy=policy, plan=result, notice=notice
        )
    except (EntitlementValidationError, OSError, RuntimeError) as exc:
        policy = await asyncio.to_thread(store.get_policy, policy_id)
        plan = await asyncio.to_thread(store.get_plan, plan_id)
        return await _render(request, policy=policy, plan=plan, error=str(exc))
    finally:
        lease.release()


@router.post("/run", response_class=HTMLResponse)
async def run_now(
    request: Request,
    policy_id: Annotated[str, Form()],
) -> HTMLResponse:
    state = request.app.state.gamgui
    if state.connector is None:
        return await _render(request, error="Connect a Workspace domain first.")
    state.ensure_workspace_services()
    store = state.entitlement_store
    policy = await asyncio.to_thread(store.get_policy, policy_id)
    if policy is None or policy.domain != state.audit_domain:
        return await _render(request, error="That policy is no longer available.")
    if (
        policy.status != PolicyStatus.ACTIVE.value
        or policy.configuration_hash != policy.approved_config_hash
    ):
        return await _render(
            request,
            policy=policy,
            error="Preview and approve the current policy before running it.",
        )
    lease = try_acquire_admin_activity(state, "classroom-teacher-entitlement")
    if lease is None:
        return await _render(
            request, policy=policy, error=ADMIN_ACTIVITY_BUSY_MESSAGE
        )
    try:
        if policy.pending_plan_id:
            pending = await asyncio.to_thread(
                store.get_plan, policy.pending_plan_id
            )
            if pending is not None and pending.status == "interrupted":
                resumed = await state.entitlement_service.resume_interrupted(
                    pending.id
                )
                if resumed is not None:
                    policy = await asyncio.to_thread(store.get_policy, policy.id)
                    return await _render(
                        request,
                        policy=policy,
                        plan=resumed,
                        notice="The interrupted approved reconciliation resumed and completed.",
                    )
        plan = await state.entitlement_service.plan(
            policy, approval_required=False
        )
        if plan.status == "held":
            policy = await asyncio.to_thread(store.get_policy, policy.id)
            return await _render(
                request,
                policy=policy,
                plan=plan,
                error=plan.hold_reason,
            )
        result = await state.entitlement_service.apply(plan.id)
        policy = await asyncio.to_thread(store.get_policy, policy.id)
        return await _render(
            request,
            policy=policy,
            plan=result,
            notice="The on-demand reconciliation completed.",
        )
    except (EntitlementValidationError, OSError, RuntimeError) as exc:
        policy = await asyncio.to_thread(store.get_policy, policy.id)
        return await _render(request, policy=policy, error=str(exc))
    finally:
        lease.release()


@router.post("/disable")
async def disable_policy(
    request: Request,
    policy_id: Annotated[str, Form()],
) -> RedirectResponse:
    state = request.app.state.gamgui
    state.ensure_workspace_services()
    existing = await asyncio.to_thread(
        state.entitlement_store.get_policy, policy_id
    )
    if existing is None or existing.domain != state.audit_domain:
        return RedirectResponse("/classroom/access", status_code=303)
    policy = await asyncio.to_thread(
        state.entitlement_store.disable_policy, policy_id
    )
    try:
        await asyncio.to_thread(state.entitlement_scheduler.disable, policy.id)
    except OSError:
        pass
    return RedirectResponse("/classroom/access", status_code=303)
