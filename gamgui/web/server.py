"""The local FastAPI app.

It is bound to loopback only and gated by a per-launch token (set as a cookie on first load), so
no other local process or user can drive it. The native window (``gamgui/app.py``) points a
WKWebView at it; in dev you can also open the printed URL in a browser.

This module exposes an app *factory* so tests can inject a mock-backed connector and run the whole
HTTP layer offline.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.calendar_index import CalendarIndex, default_index_path
from ..core.connectors.gam_connector import GAMConnector
from ..core.directory_index import (
    DEFAULT_STALE_SECONDS,
    DirectoryIndex,
    Page,
    default_index_path as default_directory_index_path,
)
from ..core.gam.models import GAMGroup, GAMUser
from ..core.gam.runner import GAMRunner
from ..core.secrets.ephemeral import sweep_stale_configs
from ..core.secrets.vault import SecretsVault
from ..core.usercache import UserCache

_WEB_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
TOKEN_COOKIE = "gamgui_token"


@dataclass
class AppState:
    vault: SecretsVault
    runner: GAMRunner
    audit_domain: str = ""              # the active Workspace domain, if configured
    connector: Optional[GAMConnector] = None
    token: str = ""
    user_cache: UserCache = field(default_factory=UserCache)
    jobs: dict = field(default_factory=dict)  # id -> ApplyJob, for polled progress on long batch ops
    calendar_index: Optional[CalendarIndex] = None  # persistent calendar name-search index (derived data)
    directory_index: Optional[DirectoryIndex] = None  # bounded user/group summary search
    cal_index_job_id: str = ""  # the in-flight index-rebuild job, if any (guards double-rebuilds)
    catalog: object = None  # the GAM command catalog (lazy-loaded by the Builder route)
    builder_sequence: list = field(default_factory=list)  # the working drag-built command sequence
    runbooks: object = None  # onboarding role templates + welcome email (lazy-loaded by the route)
    sig_templates: object = None  # saved HTML signature templates (lazy-loaded by the signatures route)
    _directory_refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _directory_refresh_tasks: Dict[str, asyncio.Task] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.connector is not None:
            self.ensure_directory_index()

    def ensure_directory_index(self) -> Optional[DirectoryIndex]:
        if self.connector is None:
            return None
        domain = self.connector.domain.strip().lower()
        if self.directory_index is not None and self.directory_index.domain == domain:
            return self.directory_index
        if self.directory_index is not None:
            path = self.directory_index.path
        elif self.runner.base_dir is not None:
            path = Path(self.runner.base_dir) / "directory_index.db"
        else:
            path = default_directory_index_path()
        self.directory_index = DirectoryIndex(path, domain)
        return self.directory_index

    def activate_connector(self, connector: GAMConnector) -> None:
        """Activate a verified domain and bind its isolated directory snapshot."""
        self.connector = connector
        self.audit_domain = connector.domain
        self.ensure_directory_index()

    async def users(self, force: bool = False) -> list:
        """The cached user list (one ``gam print users`` shared by the list + reports)."""
        if self.connector is None:
            return []
        from ..core.gam.commands import CACHE_FIELDS

        return await self.user_cache.get(
            lambda: self.connector.list_users(fields=CACHE_FIELDS), force=force
        )

    def invalidate_users(self) -> None:
        self.user_cache.invalidate()
        index = self.ensure_directory_index()
        if index is not None:
            index.mark_stale("users")

    async def directory_users(
        self,
        query: str = "",
        scope: str = "all",
        *,
        limit: int = 50,
        offset: int = 0,
        cursor: Optional[str] = None,
        refresh: bool = False,
    ) -> Page[GAMUser]:
        index = self.ensure_directory_index()
        if index is None:
            return Page([], None, 0, None, False)
        await self._ensure_directory_snapshot("users", refresh)
        return await asyncio.to_thread(
            index.search_users,
            query,
            scope,
            limit=limit,
            offset=offset,
            cursor=cursor,
        )

    async def directory_groups(
        self,
        query: str = "",
        *,
        limit: int = 50,
        offset: int = 0,
        cursor: Optional[str] = None,
        refresh: bool = False,
    ) -> Page[GAMGroup]:
        index = self.ensure_directory_index()
        if index is None:
            return Page([], None, 0, None, False)
        await self._ensure_directory_snapshot("groups", refresh)
        return await asyncio.to_thread(
            index.search_groups,
            query,
            limit=limit,
            offset=offset,
            cursor=cursor,
        )

    async def patch_directory_user(self, user: GAMUser) -> None:
        index = self.ensure_directory_index()
        if index is not None:
            await asyncio.to_thread(index.upsert_user, user)

    async def _ensure_directory_snapshot(self, kind: str, force: bool) -> None:
        index = self.ensure_directory_index()
        if index is None:
            return
        current = self._directory_refresh_tasks.get(kind)
        if force:
            if current is not None and not current.done():
                await current
            else:
                await self._refresh_directory(kind, force=True)
            return
        if not await asyncio.to_thread(index.has_snapshot, kind):
            await self._refresh_directory(kind, force=False)
            return
        if await asyncio.to_thread(index.is_stale, kind, DEFAULT_STALE_SECONDS):
            self._schedule_directory_refresh(kind)

    def _schedule_directory_refresh(self, kind: str) -> None:
        current = self._directory_refresh_tasks.get(kind)
        if current is not None and not current.done():
            return
        index = self.ensure_directory_index()
        if index is None:
            return
        index.set_refreshing(kind, True)
        task = asyncio.create_task(self._refresh_directory(kind, force=False))
        self._directory_refresh_tasks[kind] = task

        def _finish(done: asyncio.Task) -> None:
            if self._directory_refresh_tasks.get(kind) is done:
                self._directory_refresh_tasks.pop(kind, None)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                # Keep serving the previous snapshot. A manual refresh surfaces the live error.
                pass

        task.add_done_callback(_finish)

    async def _refresh_directory(self, kind: str, force: bool) -> None:
        index = self.ensure_directory_index()
        connector = self.connector
        if index is None or connector is None:
            return
        async with self._directory_refresh_lock:
            if not force and not await asyncio.to_thread(
                index.is_stale, kind, DEFAULT_STALE_SECONDS
            ):
                index.set_refreshing(kind, False)
                return
            index.set_refreshing(kind, True)
            try:
                if kind == "users":
                    await connector.refresh_directory_users(index)
                elif kind == "groups":
                    await connector.refresh_directory_groups(index)
                else:
                    raise ValueError(f"unknown directory snapshot kind: {kind}")
            finally:
                index.set_refreshing(kind, False)

    @classmethod
    def create(cls, vault: Optional[SecretsVault] = None, token: Optional[str] = None) -> "AppState":
        sweep_stale_configs()  # clean up any credential temp dirs orphaned by a prior crash/kill
        vault = vault or SecretsVault()
        runner = GAMRunner(vault=vault)
        domains = vault.list_domains()
        domain = domains[0] if domains else ""
        connector = GAMConnector(runner=runner, domain=domain) if domain else None
        return cls(
            vault=vault,
            runner=runner,
            audit_domain=domain,
            connector=connector,
            token=token or secrets.token_urlsafe(24),
            calendar_index=CalendarIndex(default_index_path()),
            directory_index=DirectoryIndex(default_directory_index_path(), domain) if domain else None,
        )


class TokenGateMiddleware(BaseHTTPMiddleware):
    """Allow static assets; otherwise require the launch token (cookie, else ?token= which sets it).

    Also stamps security headers on every response: a same-origin CSP, nosniff, and no-referrer.
    Inline script/style remains temporarily allowed for existing server-rendered handlers; remote
    code, fonts, frames, connections, and form targets remain denied."""

    SECURITY_HEADERS = {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; font-src 'self'; "
            "img-src 'self' data: blob:; connect-src 'self'; frame-src 'self' blob:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    def _token_ok(self, candidate: "str | None") -> bool:
        # Constant-time compare (defence-in-depth vs. a local process timing the loopback auth).
        return candidate is not None and secrets.compare_digest(candidate, self._token)

    def _secure(self, response):
        for key, value in self.SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/static") or request.url.path == "/healthz":
            return self._secure(await call_next(request))

        if self._token_ok(request.cookies.get(TOKEN_COOKIE)):
            return self._secure(await call_next(request))

        if self._token_ok(request.query_params.get("token")):
            response = await call_next(request)
            response.set_cookie(
                TOKEN_COOKIE, self._token, httponly=True, samesite="strict", max_age=86400
            )
            return self._secure(response)

        return self._secure(JSONResponse({"error": "forbidden"}, status_code=403))


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="GamGUI", docs_url=None, redoc_url=None)
    app.state.gamgui = state
    app.add_middleware(TokenGateMiddleware, token=state.token)
    # Ensure the dir exists before mounting — a fresh clone or a stripped bundle may lack it,
    # and StaticFiles raises on a missing directory.
    static_dir = _WEB_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        st: AppState = request.app.state.gamgui
        try:
            version = (await st.runner.version()).splitlines()[0] if st.runner.binary_exists() else ""
        except Exception:
            version = ""
        domains = st.vault.list_domains()
        configured = st.vault.has_credentials(st.audit_domain) if st.audit_domain else False
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "gam_version": version,
                "gam_binary": str(st.runner.gam_binary),
                "binary_present": st.runner.binary_exists(),
                "domains": domains,
                "active_domain": st.audit_domain,
                "configured": configured,
            },
        )

    # Imported here (not at module top) to avoid a cycle: routes import TEMPLATES from this module.
    from .routes.audit import router as audit_router
    from .routes.builder import router as builder_router
    from .routes.calendars import router as calendars_router
    from .routes.groups import router as groups_router
    from .routes.lifecycle import router as lifecycle_router
    from .routes.onboarding import router as onboarding_router
    from .routes.reports import router as reports_router
    from .routes.setup import router as setup_router
    from .routes.signatures import router as signatures_router
    from .routes.users import router as users_router

    app.include_router(setup_router)
    app.include_router(users_router)
    app.include_router(reports_router)
    app.include_router(groups_router)
    app.include_router(signatures_router)
    app.include_router(calendars_router)
    app.include_router(lifecycle_router)
    app.include_router(onboarding_router)
    app.include_router(builder_router)
    app.include_router(audit_router)
    return app
