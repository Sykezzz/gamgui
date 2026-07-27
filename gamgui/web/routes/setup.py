"""Setup wizard routes.

A small HTMX flow: collect domain + admin, then either import an existing GAM config dir or follow
the guided fresh-setup commands; do the manual Domain-Wide Delegation step; verify. On a passing
verify the Google Workspace connector is activated on the app state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core.activity import ActivityBusyError
from ...core.canary import CanaryConfigStore
from ...core.components import ComponentError
from ...core.connectors.gam_connector import GAMConnector
from ...core.setup import SetupService
from ..activity import (
    ADMIN_ACTIVITY_BUSY_MESSAGE,
    activity_error_message,
    try_acquire_admin_activity,
)
from ..server import TEMPLATES

router = APIRouter(prefix="/setup")

SETUP_COMPONENT_GATE_MESSAGE = (
    "Finish the Optional Features choice and restart GamGUI if requested "
    "before connecting Google Workspace. No Workspace credentials or GAM "
    "commands were accessed."
)


def _service(request: Request) -> SetupService:
    st = request.app.state.gamgui
    return SetupService(st.vault, st.runner)


def _component_gate_response(request: Request) -> HTMLResponse | None:
    """Refuse setup mutations until the component choice is settled.

    The browser flow already renders Optional Features first, but this check is
    intentionally server-side so a direct or stale POST cannot trigger a GAM
    command or a Workspace Keychain read.
    """

    st = request.app.state.gamgui
    ensure_manager = getattr(st, "ensure_component_manager", None)
    manager = (
        ensure_manager()
        if callable(ensure_manager)
        else getattr(st, "component_manager", None)
    )
    if manager is None:
        # Minimal route test doubles predate the component host. Production
        # AppState always supplies ensure_component_manager().
        return None
    if manager.first_run_choice_pending() or manager.status().restart_required:
        return TEMPLATES.TemplateResponse(
            request,
            "_error.html",
            {"message": SETUP_COMPONENT_GATE_MESSAGE},
        )
    return None


@router.get("", response_class=HTMLResponse)
async def setup_page(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    manager = st.ensure_component_manager()
    component_status = manager.status()
    component_only = bool(
        manager.first_run_choice_pending()
        or component_status.restart_required
    )
    if component_only:
        # The optional-feature decision is deliberately rendered before any setup
        # service probes or credential discovery. Component preparation must not
        # touch Workspace Keychain state.
        return TEMPLATES.TemplateResponse(
            request,
            "setup.html",
            {
                "component_only": True,
                "gam_version": "",
                "gam_version_warning": "",
                "binary_present": st.runner.binary_exists(),
                "candidate_dirs": (),
            },
        )
    svc = _service(request)
    return TEMPLATES.TemplateResponse(
        request,
        "setup.html",
        {
            "component_only": False,
            "gam_version": await svc.engine_version(),
            "gam_version_warning": await svc.engine_version_warning(),
            "binary_present": st.runner.binary_exists(),
            "candidate_dirs": svc.candidate_dirs(),
        },
    )


@router.post("/import", response_class=HTMLResponse)
async def do_import(
    request: Request,
    domain: Annotated[str, Form()] = "",
    admin: Annotated[str, Form()] = "",
    config_dir: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if blocked := _component_gate_response(request):
        return blocked
    domain, admin, config_dir = domain.strip(), admin.strip(), config_dir.strip()
    if not domain or not admin or not config_dir:
        return TEMPLATES.TemplateResponse(
            request, "_error.html",
            {"message": "Enter the domain and super-admin email, then choose a credentials folder."},
        )
    st = request.app.state.gamgui
    lease = try_acquire_admin_activity(st, "setup-credential-import")
    if lease is None:
        return TEMPLATES.TemplateResponse(
            request,
            "_error.html",
            {"message": ADMIN_ACTIVITY_BUSY_MESSAGE},
        )
    svc = _service(request)
    try:
        # A scope proof belongs to the credential set that produced it.  Replacing
        # credentials for the same domain must make OneRoster fail closed until
        # setup verification succeeds again, even if the import itself fails or
        # the optional component is currently absent/disabled.
        try:
            st.ensure_component_manager().invalidate_scope_readiness(domain)
        except ComponentError as exc:
            return TEMPLATES.TemplateResponse(
                request,
                "_error.html",
                {"message": str(exc)},
            )
        try:
            imported = svc.import_dir(config_dir, domain)
        except (ValueError, OSError, RuntimeError) as exc:
            # A typo'd or non-directory path is operator error, not a crash — say which, and let
            # them correct it in place. Deliberately narrow: programming errors still surface.
            return TEMPLATES.TemplateResponse(
                request,
                "_error.html",
                {
                    "message": str(exc)
                    or "That folder could not be read — check the path and try again."
                },
            )
    finally:
        lease.release()
    return TEMPLATES.TemplateResponse(
        request, "_dwd.html",
        {
            "imported": imported,
            "ready": svc.is_ready(domain),
            "domain": domain,
            "admin": admin,
            "dwd": svc.dwd_details(domain),
        },
    )


@router.post("/fresh", response_class=HTMLResponse)
async def fresh(
    request: Request,
    domain: Annotated[str, Form()] = "",
    admin: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if blocked := _component_gate_response(request):
        return blocked
    svc = _service(request)
    info = svc.setup_commands(admin.strip() or "admin@yourdomain.com")
    return TEMPLATES.TemplateResponse(
        request, "_commands.html",
        {"info": info, "domain": domain.strip(), "admin": admin.strip()},
    )


@router.post("/verify", response_class=HTMLResponse)
async def verify(
    request: Request,
    domain: Annotated[str, Form()] = "",
    admin: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if blocked := _component_gate_response(request):
        return blocked
    domain, admin = domain.strip(), admin.strip()
    if not domain or not admin:
        return TEMPLATES.TemplateResponse(
            request, "_error.html", {"message": "Domain and super-admin email are required to verify."}
        )
    st = request.app.state.gamgui
    if st.has_active_admin_jobs():
        return TEMPLATES.TemplateResponse(
            request,
            "_error.html",
            {"message": ADMIN_ACTIVITY_BUSY_MESSAGE},
        )
    svc = _service(request)
    result = await svc.verify(domain, admin)
    if result.ok:
        try:
            st.activate_connector(GAMConnector(runner=st.runner, domain=domain))
        except ActivityBusyError:
            return TEMPLATES.TemplateResponse(
                request,
                "_error.html",
                {"message": ADMIN_ACTIVITY_BUSY_MESSAGE},
            )
        except RuntimeError as exc:
            return TEMPLATES.TemplateResponse(
                request,
                "_error.html",
                {"message": activity_error_message(exc)},
            )
        config_path = (
            Path(st.runner.base_dir) / "canary-config.json"
            if st.runner.base_dir is not None
            else None
        )
        lease = try_acquire_admin_activity(st, "setup-canary-save")
        if lease is None:
            return TEMPLATES.TemplateResponse(
                request,
                "_error.html",
                {"message": ADMIN_ACTIVITY_BUSY_MESSAGE},
            )
        try:
            CanaryConfigStore(config_path).save(domain, admin)
        finally:
            lease.release()
    return TEMPLATES.TemplateResponse(
        request, "_verify.html", {"result": result, "domain": domain, "admin": admin}
    )
