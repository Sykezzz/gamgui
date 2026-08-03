"""First-party optional component administration.

The web layer deliberately treats the component manager as a local-only boundary:
rendering this page must never initialize GAM, read Workspace credentials, or make a
Google request.  The defensive adapter also lets a ``core`` build explain that the
OneRoster component is absent even when no component manager was registered.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Iterable

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core.updater import (
    ACTIVATION_PROBE_ENV,
    UpdateCoordinator,
    UpdateStateStore,
    activation_evidence_valid,
    candidate_is_blocked,
    installed_app_path,
)
from ..server import TEMPLATES

router = APIRouter(prefix="/components")
core_deep_link_router = APIRouter()

COMPONENT_ID = "classroom-oneroster"
_KNOWN_STATES = {
    "not_installed": "Not installed",
    "preparing": "Preparing",
    "restart_required": "Restart required",
    "installed_disabled": "Installed—disabled",
    "enabled": "Enabled",
    "update_available": "Update available",
    "unavailable": "Unavailable",
    "degraded": "Degraded",
}
_STATE_ALIASES = {
    "absent": "not_installed",
    "disabled": "installed_disabled",
    "installed": "enabled",
    "ready": "enabled",
    "pending": "preparing",
    "pending_restart": "restart_required",
    "restart-required": "restart_required",
    "update-ready": "update_available",
    "error": "degraded",
}


def _manager(request: Request) -> Any | None:
    return getattr(request.app.state.gamgui, "component_manager", None)


def _record(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: getattr(value, name)
            for name in value.__dataclass_fields__
            if not name.startswith("_")
        }
    data = getattr(value, "__dict__", None)
    return dict(data) if isinstance(data, dict) else {}


def _state_name(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    normalized = str(value or "not_installed").strip().casefold()
    normalized = normalized.replace(" ", "_").replace("-", "_").replace("—", "_")
    normalized = _STATE_ALIASES.get(normalized, normalized)
    return normalized if normalized in _KNOWN_STATES else "degraded"


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _call_method(
    method: Any,
    candidates: Iterable[tuple[tuple, dict]],
) -> Any:
    if inspect.iscoroutinefunction(method):
        return await _await(_compatible_call(method, candidates))
    value = await asyncio.to_thread(_compatible_call, method, candidates)
    return await _await(value)


def _compatible_call(method: Any, candidates: Iterable[tuple[tuple, dict]]) -> Any:
    """Call the first candidate compatible with a manager method's signature."""

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        signature = None
    for args, kwargs in candidates:
        if signature is not None:
            try:
                signature.bind(*args, **kwargs)
            except TypeError:
                continue
        return method(*args, **kwargs)
    raise TypeError("The installed component manager has an incompatible interface.")


async def _manager_status(manager: Any | None) -> Any:
    if manager is None:
        return None
    for name in ("status", "get_state", "component_state"):
        method = getattr(manager, name, None)
        if callable(method):
            return await _call_method(
                method,
                (
                    ((COMPONENT_ID,), {}),
                    ((), {"component_id": COMPONENT_ID}),
                    ((), {}),
                ),
            )
    value = getattr(manager, "state", None)
    if callable(value):
        return await _await(value())
    return value


async def component_context(request: Request) -> dict[str, Any]:
    """Return a stable template contract from manager versions old and new."""

    manager = _manager(request)
    raw = _record(await _manager_status(manager))
    raw_state = raw.get("state", raw.get("status", raw.get("lifecycle_state")))
    state = _state_name(raw_state)
    if manager is None:
        state = "unavailable"

    has_installed_component_contract = "installed_components" in raw
    installed_components = raw.get("installed_components", ())
    if isinstance(installed_components, str):
        installed_components = (installed_components,)
    if has_installed_component_contract:
        installed = COMPONENT_ID in installed_components
    else:
        installed = bool(
            raw.get("installed")
            or state
            in {
                "installed_disabled",
                "enabled",
                "update_available",
            }
        )
    enabled = bool(raw.get("enabled", state in {"enabled", "update_available"}))
    skipped = bool(
        raw.get("skipped", raw.get("first_run_skipped", raw.get("onboarding_skipped", False)))
    )
    pending: bool | None = None
    pending_method = getattr(manager, "first_run_choice_pending", None)
    if callable(pending_method):
        try:
            pending = bool(
                await _call_method(pending_method, (((), {}),))
            )
        except Exception:  # noqa: BLE001 - status remains usable when preference storage is degraded
            pending = None
    if pending is None:
        pending = bool(
            raw.get(
                "first_run_pending",
                raw.get("onboarding_pending", not installed and not skipped),
            )
        )
    error_code = str(raw.get("error_code", "") or "")
    runtime_error = str(
        getattr(request.app.state.gamgui, "oneroster_error_code", "") or ""
    )
    if manager is None:
        error_code = "CMP-NOT-INSTALLED"
    elif runtime_error == "CMP-INCOMPATIBLE" and installed and enabled:
        state = "degraded"
        error_code = runtime_error
    elif state == "degraded" and not error_code:
        error_code = "CMP-INCOMPATIBLE"

    return {
        "id": COMPONENT_ID,
        "name": "OneRoster Classroom",
        "state": state,
        "state_label": _KNOWN_STATES[state],
        "available": manager is not None and state != "unavailable",
        "installed": installed,
        "enabled": enabled,
        "first_run_pending": pending,
        "skipped": skipped,
        "restart_required": bool(
            raw.get("restart_required", state == "restart_required")
        ),
        "error_code": error_code,
        "error_message": str(raw.get("error_message", raw.get("message", "")) or ""),
        "profile": str(raw.get("profile", raw.get("installed_profile", "core")) or "core"),
        "desired_profile": str(raw.get("desired_profile", "") or ""),
        "version": str(raw.get("version", raw.get("application_version", "")) or ""),
        "download_size": str(raw.get("download_size", raw.get("artifact_size", "")) or ""),
        "source_sha": str(raw.get("source_sha", "") or ""),
        "channel": str(raw.get("channel", raw.get("signing_channel", "")) or ""),
    }


def _active_admin_job(request: Request) -> bool:
    checker = getattr(request.app.state.gamgui, "has_active_admin_jobs", None)
    return bool(checker()) if callable(checker) else False


def _active_admin_job_without_registry(state: Any) -> bool:
    """Ignore only the updater's own lease while retaining every other job check."""

    checker = getattr(state, "has_active_admin_jobs", None)
    if not callable(checker):
        return False
    try:
        signature = inspect.signature(checker)
        signature.bind(include_registry=False)
    except (TypeError, ValueError):
        return bool(checker())
    return bool(checker(include_registry=False))


def _update_store(request: Request) -> UpdateStateStore | None:
    manager = _manager(request)
    store = getattr(manager, "store", None)
    return store if isinstance(store, UpdateStateStore) else None


def _manual_update_supported() -> bool:
    return bool(
        sys.platform == "darwin"
        and installed_app_path() is not None
        and os.environ.get(ACTIVATION_PROBE_ENV) != "1"
        and not os.environ.get("GAMGUI_UPDATE_HEALTH_MARKER")
    )


def _update_thread(state: Any) -> threading.Thread | None:
    thread = getattr(state, "update_check_thread", None)
    return thread if isinstance(thread, threading.Thread) else None


def _update_check_in_progress(state: Any) -> bool:
    """Recognize the updater's own lease without admitting other activity kinds."""

    thread = _update_thread(state)
    if thread is not None and thread.is_alive():
        return True
    registry = getattr(state, "activity_registry", None)
    snapshot = getattr(registry, "snapshot", None)
    if not callable(snapshot):
        return False
    try:
        active = snapshot()
    except Exception:
        return False
    return bool(active is not None and getattr(active, "kind", "") == "app-update")


def _safe_update_error(code: str) -> str:
    return {
        "CMP-ACTIVE-JOB": (
            "The check was safely skipped because another administrative task is "
            "active. Try again after it finishes."
        ),
        "CMP-UPDATE-CHANNEL": (
            "This installation uses the official release channel. Install a verified "
            "notarized GamGUI release to update it."
        ),
        "CMP-UPDATE-SIGNING": (
            'The Mac could not access the required "GamGUI Local" signing identity. '
            "The current app was kept unchanged."
        ),
    }.get(
        code,
        "GamGUI could not prepare the update. The current version is still running normally.",
    )


def _format_checked_at(value: float) -> str:
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(value).astimezone().strftime(
            "%Y-%m-%d %I:%M %p %Z"
        )
    except (OSError, OverflowError, ValueError):
        return ""


def application_update_context(request: Request) -> dict[str, Any]:
    state = request.app.state.gamgui
    store = _update_store(request)
    updater_state = store.load() if store is not None else None
    checking = _update_check_in_progress(state)
    supported = _manual_update_supported()
    ready = bool(
        updater_state is not None
        and updater_state.pending_app
        and updater_state.candidate_sha
        and activation_evidence_valid(updater_state)
        and not candidate_is_blocked(updater_state)
    )
    error_code = (
        str(updater_state.component_error_code or "CMP-UPDATE-PREPARE-FAILED")
        if updater_state is not None and updater_state.last_error
        else ""
    )
    if checking:
        status = "checking"
        status_label = "Checking"
        message = (
            "GamGUI is checking the validated release channel and will prepare a "
            "matching update in the background if one is available."
        )
    elif ready:
        status = "ready"
        status_label = "Update ready"
        message = (
            "A verified update is ready. Quit and reopen GamGUI to review the "
            "install-and-restart prompt."
        )
    elif error_code:
        status = "error"
        status_label = "Check needs attention"
        message = _safe_update_error(error_code)
    elif updater_state is not None and updater_state.last_checked_at:
        status = "current"
        status_label = "No validated update"
        message = (
            "No newer release has completed the exact-version update checks yet. "
            "GamGUI will keep using the current version."
        )
    elif not supported:
        status = "unavailable"
        status_label = "Installed app required"
        message = (
            "Manual update checks are available from the installed GamGUI app on macOS."
        )
    else:
        status = "idle"
        status_label = "Not checked"
        message = "Check the validated release channel without restarting GamGUI."

    installed_artifact = (
        updater_state.installed_artifact if updater_state is not None else None
    )
    candidate_artifact = (
        updater_state.candidate_artifact if updater_state is not None else None
    )
    return {
        "status": status,
        "status_label": status_label,
        "message": message,
        "checking": checking,
        "supported": supported and store is not None,
        "last_checked_at": _format_checked_at(
            updater_state.last_checked_at if updater_state is not None else 0.0
        ),
        "installed_version": str(
            getattr(installed_artifact, "version", "") or ""
        ),
        "candidate_version": str(
            getattr(candidate_artifact, "version", "") or ""
        ),
        "channel": str(
            updater_state.installed_signing_channel
            if updater_state is not None
            else ""
        ),
        "error_code": error_code,
    }


def _application_update_response(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "_application_update_status.html",
        {"update": application_update_context(request)},
    )


def _build_update_coordinator(request: Request) -> UpdateCoordinator:
    state = request.app.state.gamgui
    store = _update_store(request)
    if store is None:
        raise RuntimeError("The local update state is unavailable.")
    return UpdateCoordinator(
        store=store,
        active_jobs=lambda: _active_admin_job_without_registry(state),
        activity_registry=state.activity_registry,
    )


async def _invoke_action(
    manager: Any,
    action: str,
    *,
    source_path: str = "",
    signing_channel_confirmation: str = "",
) -> Any:
    names = {
        "install": ("prepare_install", "request_install", "stage_install", "install"),
        "skip": ("skip_first_run", "mark_skipped", "skip"),
        "enable": ("enable", "enable_component"),
        "disable": ("disable", "disable_component"),
        "remove": ("prepare_remove", "request_remove", "stage_remove", "remove"),
        "update": ("prepare_update", "request_update", "stage_update"),
    }[action]
    for name in names:
        method = getattr(manager, name, None)
        if not callable(method):
            continue
        common = (
            ((COMPONENT_ID,), {}),
            ((), {"component_id": COMPONENT_ID}),
            ((), {}),
        )
        if action in {"install", "remove", "update"}:
            source = Path(source_path).expanduser() if source_path else None
            common = (
                (
                    (COMPONENT_ID,),
                    {
                        "source_path": source,
                        "signing_channel_confirmation": signing_channel_confirmation,
                    },
                ),
                (
                    (),
                    {
                        "component_id": COMPONENT_ID,
                        "source_path": source,
                        "signing_channel_confirmation": signing_channel_confirmation,
                    },
                ),
                (
                    (source,),
                    {
                        "signing_channel_confirmation": signing_channel_confirmation,
                    },
                ),
                (
                    (),
                    {
                        "source_file": source,
                        "signing_channel_confirmation": signing_channel_confirmation,
                    },
                ),
                ((COMPONENT_ID,), {"source_path": source}),
                ((COMPONENT_ID, source), {}),
                ((), {"component_id": COMPONENT_ID, "source_path": source}),
                ((source,), {}),
                ((), {"source_path": source}),
                ((COMPONENT_ID,), {}),
                ((), {}),
            )
        return await _call_method(method, common)
    raise TypeError(f"The installed component manager cannot {action} components.")


def _allowed(action: str, component: dict[str, Any]) -> bool:
    state = component["state"]
    if action == "skip":
        return component["first_run_pending"]
    if action == "install":
        return (
            (not component["installed"] and state in {"not_installed", "unavailable"})
            or state == "degraded"
        )
    if action == "enable":
        return component["installed"] and not component["enabled"]
    if action == "disable":
        return component["installed"] and component["enabled"]
    if action == "remove":
        return component["installed"]
    if action == "update":
        # This is an application-profile update, not a OneRoster-only action.
        # Core and full profiles both have an installed application to update.
        return state not in {"preparing", "restart_required"}
    return False


def _safe_error(action: str, exc: Exception) -> tuple[str, str]:
    code = str(
        getattr(exc, "code", "")
        or getattr(exc, "error_code", "")
        or ""
    )
    if not code:
        code = {
            "install": "CMP-VERIFY-FAILED",
            "enable": "CMP-INCOMPATIBLE",
            "disable": "CMP-INCOMPATIBLE",
            "remove": "CMP-VERIFY-FAILED",
            "update": "CMP-VERIFY-FAILED",
            "skip": "CMP-INCOMPATIBLE",
        }.get(action, "CMP-INCOMPATIBLE")
    message = str(getattr(exc, "public_message", "") or "")
    if not message and getattr(exc, "error_code", None):
        message = str(exc)
    if not message:
        message = (
            "GamGUI could not prepare that component change. "
            "The installed application was not changed."
        )
    return code, message


def _component_response(
    request: Request,
    component: dict[str, Any],
    *,
    context: str,
    notice: str = "",
    error: str = "",
    error_code: str = "",
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "_component_card.html",
        {
            "component": component,
            "context": context,
            "notice": notice,
            "error": error,
            "error_code": error_code,
        },
    )


@router.get("", response_class=HTMLResponse)
async def components_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "components.html",
        {
            "component": await component_context(request),
            "update": application_update_context(request),
        },
    )


@router.get("/update/status", response_class=HTMLResponse)
async def application_update_status(request: Request) -> HTMLResponse:
    return _application_update_response(request)


@router.post("/update/check", response_class=HTMLResponse)
async def check_for_application_update(request: Request) -> HTMLResponse:
    state = request.app.state.gamgui
    if not _manual_update_supported() or _update_store(request) is None:
        return _application_update_response(request)
    if _update_check_in_progress(state):
        return _application_update_response(request)
    if _active_admin_job(request):
        store = _update_store(request)
        updater_state = store.load()
        updater_state.last_checked_at = datetime.now().timestamp()
        updater_state.last_error = (
            "An administrative operation is active; update preparation was deferred."
        )
        updater_state.component_error_code = "CMP-ACTIVE-JOB"
        store.save(updater_state)
        return _application_update_response(request)

    lock = getattr(state, "update_check_lock", None)
    if not isinstance(lock, type(threading.Lock())):
        lock = threading.Lock()
        setattr(state, "update_check_lock", lock)
    with lock:
        current = _update_thread(state)
        if current is None or not current.is_alive():
            coordinator = _build_update_coordinator(request)
            current = threading.Thread(
                target=coordinator.check_and_prepare,
                name="gamgui-manual-update-check",
                daemon=True,
            )
            setattr(state, "update_check_thread", current)
            current.start()
    return _application_update_response(request)


@core_deep_link_router.get("/classroom/imports", response_class=HTMLResponse)
async def core_oneroster_deep_link(request: Request) -> HTMLResponse:
    """Core-profile explanation for a full-profile OneRoster URL.

    This handler intentionally lives with the Core component UI and renders no
    optional template or module. The application factory registers it only for a
    Core profile; the full profile registers ``routes.oneroster.router`` instead.
    """

    component = await component_context(request)
    component["error_code"] = (
        str(component.get("error_code", "") or "") or "CMP-NOT-INSTALLED"
    )
    return TEMPLATES.TemplateResponse(
        request,
        "components.html",
        {
            "component": component,
            "deep_link": True,
            "update": application_update_context(request),
        },
    )


@router.get("/status", response_class=HTMLResponse)
async def components_status(
    request: Request,
    context: str = "settings",
) -> HTMLResponse:
    if context not in {"settings", "setup", "classroom"}:
        context = "settings"
    return _component_response(
        request,
        await component_context(request),
        context=context,
    )


@router.post("/oneroster/{action}", response_class=HTMLResponse)
async def component_action(
    request: Request,
    action: str,
    source_path: Annotated[str, Form()] = "",
    signing_channel_confirmation: Annotated[str, Form()] = "",
    context: Annotated[str, Form()] = "settings",
) -> HTMLResponse:
    if context not in {"settings", "setup", "classroom"}:
        context = "settings"
    if action not in {"install", "skip", "enable", "disable", "remove", "update"}:
        component = await component_context(request)
        return _component_response(
            request,
            component,
            context=context,
            error="That component action is not supported.",
            error_code="CMP-INCOMPATIBLE",
        )
    component = await component_context(request)
    manager = _manager(request)
    if manager is None:
        return _component_response(
            request,
            component,
            context=context,
            error=(
                "This build cannot prepare component changes. Install a verified "
                "GamGUI profile that includes the component manager."
            ),
            error_code="CMP-NOT-INSTALLED",
        )
    if action != "skip" and _active_admin_job(request):
        return _component_response(
            request,
            component,
            context=context,
            error=(
                "Finish or stop the active administrative operation before "
                "changing installed components."
            ),
            error_code="CMP-ACTIVE-JOB",
        )
    if not _allowed(action, component):
        return _component_response(
            request,
            component,
            context=context,
            error="That action is not valid for the component's current state.",
            error_code="CMP-INCOMPATIBLE",
        )
    if action == "update" and not source_path.strip():
        return _component_response(
            request,
            component,
            context=context,
            error="Choose a verified same-profile release file.",
            error_code="CMP-VERIFY-FAILED",
        )
    try:
        await _invoke_action(
            manager,
            action,
            source_path=source_path.strip(),
            signing_channel_confirmation=signing_channel_confirmation.strip(),
        )
        if context == "setup" and action == "enable":
            await _invoke_action(manager, "skip")
        if action in {"install", "enable", "disable"}:
            refresh_services = getattr(
                request.app.state.gamgui,
                "ensure_component_services",
                None,
            )
            if callable(refresh_services):
                # Run on the request event-loop thread so an immediate enable can
                # create its scheduled student-release task.
                refresh_services()
                scheduler = getattr(
                    request.app.state.gamgui,
                    "_schedule_oneroster_gate",
                    None,
                )
                if callable(scheduler):
                    scheduler()
    except Exception as exc:  # noqa: BLE001 - converted to stable, privacy-safe UI
        code, message = _safe_error(action, exc)
        return _component_response(
            request,
            await component_context(request),
            context=context,
            error=message,
            error_code=code,
        )
    updated = await component_context(request)
    if updated["error_code"]:
        return _component_response(
            request,
            updated,
            context=context,
            error=(
                updated["error_message"]
                or "GamGUI could not prepare that component change."
            ),
            error_code=updated["error_code"],
        )
    notices = {
        "install": "OneRoster preparation started. GamGUI will ask for a restart when the verified profile is ready.",
        "skip": "OneRoster was skipped. You can add it later from Settings → Components.",
        "enable": "OneRoster is enabled.",
        "disable": "OneRoster is disabled. Its retained data was not removed.",
        "remove": "The Core profile is being prepared. OneRoster data will remain available for the retention period.",
        "update": "The verified same-profile release is staged. Restart to activate it after the canary and safety checks.",
    }
    response = _component_response(
        request,
        updated,
        context=context,
        notice=notices[action],
    )
    if context == "setup" and action in {"skip", "enable"}:
        response.headers["HX-Redirect"] = "/setup"
    return response


@router.get("/oneroster/data/purge", response_class=HTMLResponse)
async def purge_preview(request: Request) -> HTMLResponse:
    manager = _manager(request)
    preview: dict[str, Any] = {}
    if manager is not None:
        for name in ("purge_preview", "data_summary", "retained_data"):
            method = getattr(manager, name, None)
            if callable(method):
                try:
                    preview = _record(
                        await _call_method(
                            method,
                            (
                                ((COMPONENT_ID,), {}),
                                ((), {"component_id": COMPONENT_ID}),
                                ((), {}),
                            ),
                        )
                    )
                except Exception:  # noqa: BLE001 - preview remains safely unavailable
                    preview = {}
                break
    return TEMPLATES.TemplateResponse(
        request,
        "_component_status.html",
        {
            "mode": "purge",
            "component": await component_context(request),
            "preview": {
                "domains": preview.get("domains", 0),
                "records": preview.get(
                    "records",
                    preview.get("snapshots", preview.get("snapshot_count", 0)),
                ),
                "bytes": preview.get(
                    "bytes",
                    preview.get("retained_bytes", 0),
                ),
            },
        },
    )


@router.post("/oneroster/data/purge", response_class=HTMLResponse)
async def purge_data(
    request: Request,
    confirmation: Annotated[str, Form()] = "",
) -> HTMLResponse:
    component = await component_context(request)
    if confirmation != "OneRoster":
        return TEMPLATES.TemplateResponse(
            request,
            "_component_status.html",
            {
                "mode": "purge",
                "component": component,
                "preview": {},
                "error": "Type OneRoster exactly to confirm permanent removal.",
                "error_code": "CMP-INCOMPATIBLE",
            },
        )
    if _active_admin_job(request):
        return TEMPLATES.TemplateResponse(
            request,
            "_component_status.html",
            {
                "mode": "purge",
                "component": component,
                "preview": {},
                "error": (
                    "Finish or stop the active administrative operation before "
                    "purging retained OneRoster data."
                ),
                "error_code": "CMP-ACTIVE-JOB",
            },
        )
    manager = _manager(request)
    if manager is None:
        return TEMPLATES.TemplateResponse(
            request,
            "_component_status.html",
            {
                "mode": "purge",
                "component": component,
                "preview": {},
                "error": "The component manager is unavailable.",
                "error_code": "CMP-NOT-INSTALLED",
            },
        )
    try:
        for name in ("purge_data", "purge_component_data"):
            method = getattr(manager, name, None)
            if callable(method):
                await _call_method(
                    method,
                    (
                        ((COMPONENT_ID,), {"confirmation": confirmation}),
                        ((COMPONENT_ID, confirmation), {}),
                        ((), {"component_id": COMPONENT_ID, "confirmation": confirmation}),
                        ((confirmation,), {}),
                    ),
                )
                break
        else:
            raise TypeError("The installed component manager cannot purge component data.")
        rebind = getattr(
            request.app.state.gamgui,
            "rebind_component_services",
            None,
        )
        if callable(rebind):
            await _await(rebind())
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error("purge", exc)
        return TEMPLATES.TemplateResponse(
            request,
            "_component_status.html",
            {
                "mode": "purge",
                "component": await component_context(request),
                "preview": {},
                "error": message,
                "error_code": code,
            },
        )
    return TEMPLATES.TemplateResponse(
        request,
        "_component_status.html",
        {
            "mode": "purge",
            "component": await component_context(request),
            "preview": {},
            "notice": "Retained OneRoster imports and configuration were permanently removed.",
        },
    )
