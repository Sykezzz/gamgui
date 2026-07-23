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
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.calendar_index import CalendarIndex, default_index_path
from ..core.classroom.index import CourseIndex, default_course_index_path
from ..core.classroom.manifests import (
    RosterManifestStore,
    default_roster_manifest_path,
)
from ..core.classroom.service import ClassroomService
from ..core.connectors.gam_connector import GAMConnector
from ..core.directory_index import (
    DEFAULT_STALE_SECONDS,
    DirectoryIndex,
    Page,
    default_index_path as default_directory_index_path,
)
from ..core.gam.models import GAMGroup, GAMUser
from ..core.gam.runner import GAMRunner
from ..core.drive import (
    ConnectorDirectoryResolver,
    DriveAPIClient,
    DriveOperationStore,
    DriveService,
    ServiceAccountTokenProvider,
)
from ..core.secrets.ephemeral import sweep_stale_configs
from ..core.secrets.vault import SecretsVault
from ..core.updater import UpdateStateStore
from ..core.usercache import UserCache

_WEB_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
TOKEN_COOKIE = "gamgui_token"


def _local_update_notice() -> str:
    """Return a generic local updater notice without exposing paths or command output."""
    state = UpdateStateStore().load()
    if state.pending_app and state.candidate_sha:
        return "A verified update is ready and will install after the app closes."
    if state.last_error:
        return "The automatic update could not be prepared. This version is still running normally."
    return ""


TEMPLATES.env.globals["local_update_notice"] = _local_update_notice


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
    classroom_index: Optional[CourseIndex] = None
    classroom_manifests: Optional[RosterManifestStore] = None
    classroom_service: Optional[ClassroomService] = None
    classroom_refresh_task: Optional[asyncio.Task] = field(default=None, repr=False)
    classroom_refresh_error: str = ""
    classroom_manifest_tasks: dict = field(default_factory=dict, repr=False)
    classroom_manifest_errors: dict = field(default_factory=dict, repr=False)
    drive_service: Optional[DriveService] = None
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
            self.ensure_workspace_services()

    def ensure_directory_index(self) -> Optional[DirectoryIndex]:
        if self.connector is None:
            return None
        index = self._directory_index_for(self.connector)
        self.directory_index = index
        return index

    def _directory_index_for(self, connector: GAMConnector) -> DirectoryIndex:
        """Construct a domain-bound index without mutating the active state."""

        domain = connector.domain.strip().lower()
        if self.directory_index is not None and self.directory_index.domain == domain:
            return self.directory_index
        if self.directory_index is not None:
            path = self.directory_index.path
        elif self.runner.base_dir is not None:
            path = Path(self.runner.base_dir) / "directory_index.db"
        else:
            path = default_directory_index_path()
        return DirectoryIndex(path, domain)

    def activate_connector(self, connector: GAMConnector) -> None:
        """Stage a complete verified-domain binding, then swap it atomically."""
        if self.has_active_admin_jobs():
            raise RuntimeError(
                "Finish or stop the active administrative operation before reconnecting."
            )
        domain = connector.domain.strip().casefold()
        if not domain:
            raise RuntimeError("The verified Workspace domain was blank.")
        try:
            new_directory_index = self._directory_index_for(connector)
            base_dir = (
                Path(self.runner.base_dir)
                if self.runner.base_dir is not None
                else None
            )
            new_classroom_index = self.classroom_index or CourseIndex(
                base_dir / "classroom_courses.db"
                if base_dir is not None
                else default_course_index_path()
            )
            new_classroom_manifests = (
                self.classroom_manifests
                or RosterManifestStore(
                    base_dir / "classroom_roster_operations.db"
                    if base_dir is not None
                    else default_roster_manifest_path()
                )
            )
            new_classroom_service = ClassroomService(
                connector,
                domain,
                new_classroom_index,
                new_classroom_manifests,
            )
            operations = DriveOperationStore(
                base_dir / "drive_operations.db" if base_dir is not None else None
            )
            drive_client = DriveAPIClient(
                ServiceAccountTokenProvider(self.vault, domain)
            )
            new_drive_service = DriveService(
                drive_client,
                self.runner,
                domain,
                audit=getattr(connector, "audit", None),
                resolver=ConnectorDirectoryResolver(connector),
                operations=operations,
            )
        except Exception as exc:
            raise RuntimeError(
                "The verified connection could not be activated; "
                "the existing connection remains active."
            ) from exc

        old_drive = self.drive_service
        old_refresh_tasks = list(self._directory_refresh_tasks.values())
        old_classroom_refresh = self.classroom_refresh_task
        (
            self.connector,
            self.audit_domain,
            self.directory_index,
            self.classroom_index,
            self.classroom_manifests,
            self.classroom_service,
            self.drive_service,
        ) = (
            connector,
            domain,
            new_directory_index,
            new_classroom_index,
            new_classroom_manifests,
            new_classroom_service,
            new_drive_service,
        )
        self.user_cache = UserCache()
        self.jobs.clear()
        self.builder_sequence.clear()
        self.classroom_manifest_tasks.clear()
        self.classroom_manifest_errors.clear()
        self.classroom_refresh_task = None
        self.classroom_refresh_error = ""
        self._directory_refresh_tasks = {}
        for task in (*old_refresh_tasks, old_classroom_refresh):
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
        if old_drive is not None and old_drive is not self.drive_service:
            self._schedule_drive_close(old_drive)

    def has_active_admin_jobs(self) -> bool:
        """Return whether rebinding services could interrupt an administrative mutation."""

        terminal = {"completed", "failed", "cancelled", "interrupted"}
        for job in self.jobs.values():
            task = getattr(job, "task", None)
            done = getattr(task, "done", None)
            if callable(done) and not done():
                return True
            finished = getattr(job, "finished", None)
            if finished is not None:
                if not bool(finished):
                    return True
                continue
            status = getattr(job, "status", "")
            if hasattr(status, "value"):
                status = status.value
            if status and str(status).lower() not in terminal:
                return True
        for task in self.classroom_manifest_tasks.values():
            done = getattr(task, "done", None)
            if callable(done) and not done():
                return True
        stores = (
            self.classroom_manifests,
            getattr(self.drive_service, "operations", None),
        )
        for store in stores:
            checker = getattr(store, "has_active_jobs", None)
            if callable(checker) and checker():
                return True
        return False

    def ensure_workspace_services(self) -> None:
        """Bind Classroom and Drive services to the currently verified connector/domain."""
        connector = self.connector
        domain = (self.audit_domain or "").strip().casefold()
        if connector is None or not domain:
            return
        base_dir = Path(self.runner.base_dir) if self.runner.base_dir is not None else None
        if self.classroom_index is None:
            self.classroom_index = CourseIndex(
                base_dir / "classroom_courses.db"
                if base_dir is not None
                else default_course_index_path()
            )
        if self.classroom_manifests is None:
            self.classroom_manifests = RosterManifestStore(
                base_dir / "classroom_roster_operations.db"
                if base_dir is not None
                else default_roster_manifest_path()
            )
        if (
            self.classroom_service is None
            or self.classroom_service.connector is not connector
            or self.classroom_service.domain != domain
        ):
            self.classroom_service = ClassroomService(
                connector,
                domain,
                self.classroom_index,
                self.classroom_manifests,
            )
        if self.drive_service is None or self.drive_service.domain != domain:
            operations = DriveOperationStore(
                base_dir / "drive_operations.db" if base_dir is not None else None
            )
            drive_client = DriveAPIClient(
                ServiceAccountTokenProvider(self.vault, domain)
            )
            self.drive_service = DriveService(
                drive_client,
                self.runner,
                domain,
                audit=getattr(connector, "audit", None),
                resolver=ConnectorDirectoryResolver(connector),
                operations=operations,
            )

    @staticmethod
    def _schedule_drive_close(service: DriveService) -> None:
        close = getattr(getattr(service, "client", None), "aclose", None)
        if not callable(close):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(close())

    async def aclose(self) -> None:
        """Cancel local background work and close the delegated Drive HTTP client."""
        tasks = list(self._directory_refresh_tasks.values())
        if self.classroom_refresh_task is not None:
            tasks.append(self.classroom_refresh_task)
        tasks.extend(
            task
            for task in self.classroom_manifest_tasks.values()
            if isinstance(task, asyncio.Task)
        )
        tasks.extend(
            task
            for job in self.jobs.values()
            if isinstance((task := getattr(job, "task", None)), asyncio.Task)
        )
        tasks = list(dict.fromkeys(tasks))
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.drive_service is not None:
            close = getattr(self.drive_service.client, "aclose", None)
            if callable(close):
                await close()

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
    def create(
        cls,
        vault: Optional[SecretsVault] = None,
        token: Optional[str] = None,
        preferred_domain: str = "",
    ) -> "AppState":
        sweep_stale_configs()  # clean up any credential temp dirs orphaned by a prior crash/kill
        vault = vault or SecretsVault()
        runner = GAMRunner(vault=vault)
        domains = vault.list_domains()
        requested = (preferred_domain or "").strip().casefold()
        domain = requested if requested in {str(item).casefold() for item in domains} else ""
        if not domain:
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


class PrivacyTimingMiddleware(BaseHTTPMiddleware):
    """Expose local timing/size evidence without recording routes, searches, or identifiers."""

    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000.0
        response.headers["Server-Timing"] = f"app;dur={duration_ms:.3f}"
        length = response.headers.get("content-length")
        if length and length.isdigit():
            response.headers["X-GamGUI-Response-Bytes"] = length
        return response


def create_app(state: AppState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await state.aclose()

    app = FastAPI(
        title="GamGUI",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.gamgui = state
    app.add_middleware(PrivacyTimingMiddleware)
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
    from .routes.classroom import router as classroom_router
    from .routes.drive import router as drive_router
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
    app.include_router(classroom_router)
    app.include_router(drive_router)
    app.include_router(lifecycle_router)
    app.include_router(onboarding_router)
    app.include_router(builder_router)
    app.include_router(audit_router)
    return app
