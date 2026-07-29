"""The local FastAPI app.

It is bound to loopback only and gated by a per-launch token (set as a cookie on first load), so
no other local process or user can drive it. The native window (``gamgui/app.py``) points a
WKWebView at it; in dev you can also open the printed URL in a browser.

This module exposes an app *factory* so tests can inject a mock-backed connector and run the whole
HTTP layer offline.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.calendar_index import CalendarIndex, default_index_path
from ..core.activity import (
    ActivityBusyError,
    ActivityRegistry,
    activity_registry as global_activity_registry,
)
from ..core.classroom.index import CourseIndex, default_course_index_path
from ..core.classroom.manifests import (
    RosterManifestStore,
    default_roster_manifest_path,
)
from ..core.classroom.service import ClassroomService
from ..core.classroom_access import (
    EntitlementService,
    EntitlementStore,
    LaunchAgentManager,
    default_entitlement_store_path,
)
from ..core.connectors.gam_connector import GAMConnector
from ..core.components import (
    CORE_PROFILE,
    ComponentError,
    ComponentManager,
    ONEROSTER_COMPONENT,
    ONEROSTER_PROFILE,
    component_ids_for_profile,
)
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
from ..core.secrets.vault import InMemoryBackend, SecretsVault
from ..core.updater import (
    ACTIVATION_APP_UPDATE,
    ACTIVATION_PROBE_ENV,
    ACTIVATION_TRANSACTION_ENV,
    UpdateStateStore,
    activation_evidence_valid,
    candidate_is_blocked,
)
from ..core.usercache import UserCache
from .limits import RequestBodyLimitMiddleware

_WEB_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
TOKEN_COOKIE = "gamgui_token"


def _local_update_notice() -> str:
    """Return a generic local updater notice without exposing paths or command output."""
    try:
        state = UpdateStateStore().load()
    except (OSError, RuntimeError):
        return ""
    if (
        state.pending_app
        and state.candidate_sha
        and activation_evidence_valid(state)
        and not candidate_is_blocked(state)
    ):
        return "A verified update is ready. Quit and reopen GamGUI to install it."
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
    entitlement_store: Optional[EntitlementStore] = None
    entitlement_service: Optional[EntitlementService] = None
    entitlement_scheduler: Optional[LaunchAgentManager] = None
    drive_service: Optional[DriveService] = None
    component_manager: Optional[ComponentManager] = None
    oneroster_service: object = None
    oneroster_error_code: str = ""
    oneroster_manifest_tasks: dict = field(default_factory=dict, repr=False)
    oneroster_manifest_errors: dict = field(default_factory=dict, repr=False)
    oneroster_gate_task: Optional[asyncio.Task] = field(default=None, repr=False)
    oneroster_gate_error: str = ""
    activation_probe_mode: bool = False
    activity_registry: ActivityRegistry = field(
        default_factory=lambda: global_activity_registry,
        repr=False,
    )
    cal_index_job_id: str = ""  # the in-flight index-rebuild job, if any (guards double-rebuilds)
    catalog: object = None  # the GAM command catalog (lazy-loaded by the Builder route)
    builder_sequence: list = field(default_factory=list)  # the working drag-built command sequence
    builder_last_result: Optional[dict] = None  # last read-command result set, for the CSV download
    runbooks: object = None  # onboarding role templates + welcome email (lazy-loaded by the route)
    sig_templates: object = None  # saved HTML signature templates (lazy-loaded by the signatures route)
    _directory_refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _directory_refresh_tasks: Dict[str, asyncio.Task] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.ensure_component_manager()
        if self.activation_probe_mode:
            # The activation page needs only sealed component metadata. Binding
            # optional services here would perform redundant projection work
            # before the page-loaded health decision.
            return
        if self.connector is not None:
            self.ensure_directory_index()
            self.ensure_workspace_services()
        self.ensure_component_services()

    def ensure_component_manager(self) -> ComponentManager:
        """Create the local-only component facade without reading Workspace secrets."""

        if self.component_manager is not None:
            return self.component_manager
        if self.runner.base_dir is not None:
            data_root = Path(self.runner.base_dir)
            store = UpdateStateStore(data_root / "updates" / "state.json")
        else:
            data_root = None
            store = UpdateStateStore()
        self.component_manager = ComponentManager(
            store=store,
            registry=self.activity_registry,
            data_root=data_root,
        )
        return self.component_manager

    def embedded_profile(self) -> str:
        manager = self.ensure_component_manager()
        embedded = getattr(manager, "embedded", None)
        artifact = getattr(embedded, "artifact", None)
        return str(getattr(artifact, "profile", "") or "core")

    def oneroster_enabled(self) -> bool:
        """Return the sealed-profile/local-preference decision only."""

        if self.embedded_profile() != ONEROSTER_PROFILE:
            return False
        status = self.ensure_component_manager().status()
        return bool(
            status.enabled
            and ONEROSTER_COMPONENT in status.installed_components
        )

    def update_activation_ready(self) -> bool:
        """Require the swapped candidate profile to initialize before health."""

        return self.update_activation_health_payload() is not None

    def update_activation_health_payload(self) -> Optional[dict[str, object]]:
        """Return transaction-bound health evidence, or ``None`` when unsafe."""

        marker_value = os.environ.get("GAMGUI_UPDATE_HEALTH_MARKER", "")
        if not marker_value:
            return None
        manager = self.ensure_component_manager()
        embedded = getattr(manager, "embedded", None)
        if embedded is None:
            return None
        projection = manager.runtime_projection()
        if projection is not None:
            activating = getattr(projection, "installed_artifact", None)
            if activating is None:
                return None
            installed_components = set(
                getattr(projection, "installed_components", ()) or ()
            )
            enabled_components = set(
                getattr(projection, "enabled_components", ()) or ()
            )
            if (
                embedded.artifact.profile != activating.profile
                or embedded.artifact.component_set_digest
                != activating.component_set_digest
                or installed_components
                != set(component_ids_for_profile(activating.profile))
            ):
                return None
            if activating.profile == ONEROSTER_PROFILE:
                if self.oneroster_error_code == "CMP-INCOMPATIBLE":
                    return None
                if (
                    ONEROSTER_COMPONENT in enabled_components
                    and self.audit_domain
                    and self.oneroster_service is None
                ):
                    return None
            return {
                "ok": True,
                "transaction_id": os.environ.get(
                    ACTIVATION_TRANSACTION_ENV,
                    "",
                ),
                "sha": activating.source_sha,
                "profile": activating.profile,
                "component_set_digest": activating.component_set_digest,
            }

        # The installed legacy updater predates transaction-bound profiles. It
        # may bootstrap Core only; any optional-profile transition requires the
        # validated projection above.
        state = manager.store.load()
        expected_sha = os.environ.get("GAMGUI_INSTALLED_SHA", "").lower()
        try:
            expected_marker = (
                Path(manager.store.path).parent
                / "health"
                / f"{expected_sha}.ok"
            ).resolve()
            marker_matches = Path(marker_value).resolve() == expected_marker
        except (AttributeError, OSError, ValueError):
            marker_matches = False
        if (
            os.environ.get("GAMGUI_SKIP_UPDATE_ONCE") != "1"
            or os.environ.get(ACTIVATION_PROBE_ENV)
            or embedded.artifact.profile != "core"
            or embedded.artifact.source_sha != expected_sha
            or state.candidate_sha != expected_sha
            or not marker_matches
            or (
                state.activation_kind == ACTIVATION_APP_UPDATE
                and not activation_evidence_valid(state)
            )
            or (
                not state.activation_kind
                and state.canary_result != "passed"
            )
            or "update-ready" not in state.required_check_evidence
            or candidate_is_blocked(state)
        ):
            return None
        return {
            "ok": True,
            "transaction_id": "",
            "sha": expected_sha,
            "profile": embedded.artifact.profile,
            "component_set_digest": embedded.artifact.component_set_digest,
        }

    def activation_probe_pending(self) -> bool:
        transaction_probe = os.environ.get(ACTIVATION_PROBE_ENV) == "1"
        legacy_health_start = bool(
            os.environ.get("GAMGUI_UPDATE_HEALTH_MARKER")
            and os.environ.get("GAMGUI_SKIP_UPDATE_ONCE") == "1"
        )
        if not transaction_probe and not legacy_health_start:
            return False
        expected_sha = os.environ.get("GAMGUI_INSTALLED_SHA", "").lower()
        manager = self.ensure_component_manager()
        state = manager.store.load()
        pending = bool(
            state.candidate_sha == expected_sha
            or state.installed_sha != expected_sha
        )
        if transaction_probe or legacy_health_start:
            pending = pending or not manager.committed_runtime_identity_ready(
                state
            )
        return pending

    async def _wait_for_activation_commit(self) -> None:
        """Enable background work only after the helper commits candidate state."""

        for _attempt in range(600):
            manager = self.ensure_component_manager()
            state = manager.store.load()
            expected_sha = os.environ.get("GAMGUI_INSTALLED_SHA", "").lower()
            marker_value = os.environ.get("GAMGUI_UPDATE_HEALTH_MARKER", "")
            embedded = getattr(manager, "embedded", None)
            embedded_artifact = getattr(embedded, "artifact", None)
            try:
                updates_root = Path(
                    os.path.abspath(Path(manager.store.path).parent)
                )
                marker_path = Path(os.path.abspath(marker_value))
                expected_marker = (
                    updates_root
                    / "health"
                    / f"{expected_sha}.ok"
                )
                marker_matches = (
                    marker_path == expected_marker
                    and not updates_root.is_symlink()
                    and not expected_marker.parent.is_symlink()
                    and not expected_marker.is_symlink()
                    and marker_path.is_file()
                    and marker_path.read_text(encoding="utf-8").strip() == "ok"
                )
            except (AttributeError, OSError, ValueError):
                marker_matches = False
            legacy_commit = bool(
                marker_matches
                and os.environ.get("GAMGUI_SKIP_UPDATE_ONCE") == "1"
                and not os.environ.get(ACTIVATION_PROBE_ENV)
                and not os.environ.get(ACTIVATION_TRANSACTION_ENV)
                and getattr(embedded_artifact, "profile", "") == CORE_PROFILE
                and getattr(embedded_artifact, "source_sha", "") == expected_sha
                and state.installed_sha == expected_sha
                and not state.candidate_sha
            )
            if (
                legacy_commit
                and not manager.committed_runtime_identity_ready(state)
            ):
                try:
                    reconciled = await asyncio.to_thread(
                        manager.reconcile_committed_runtime
                    )
                except (ComponentError, OSError, RuntimeError, ValueError):
                    return
                if not reconciled:
                    return
            if not self.activation_probe_pending():
                state = manager.store.load()
                if state.installed_sha == expected_sha and not state.candidate_sha:
                    if not manager.committed_runtime_identity_ready(state):
                        return
                    if self.activation_probe_mode:
                        if not await self._rehydrate_after_activation():
                            return
                    self._schedule_oneroster_gate()
                return
            await asyncio.sleep(0.1)

    async def _rehydrate_after_activation(self) -> bool:
        """Bind the committed app to Workspace only after activation is durable."""

        if not self.activation_probe_mode:
            return True
        try:
            hydrated = await asyncio.to_thread(
                type(self).create,
                token=self.token,
            )
        except Exception:
            self.oneroster_gate_error = "CMP-AUTH-REQUIRED"
            return False
        for descriptor in dataclass_fields(self):
            setattr(self, descriptor.name, getattr(hydrated, descriptor.name))
        return True

    def _schedule_activation_commit_watch(self) -> None:
        if not self.activation_probe_pending():
            self._schedule_oneroster_gate()
            return
        try:
            asyncio.get_running_loop().create_task(
                self._wait_for_activation_commit(),
                name="activation-commit-watch",
            )
        except RuntimeError:
            pass

    def ensure_component_services(self) -> None:
        """Bind optional local services without accessing GAM, Google, or Keychain."""

        if not self.oneroster_enabled():
            if isinstance(self.oneroster_gate_task, asyncio.Task):
                self.oneroster_gate_task.cancel()
            self.oneroster_gate_task = None
            self.oneroster_service = None
            self.oneroster_error_code = ""
            return
        domain = (self.audit_domain or "").strip().casefold()
        if not domain:
            self.oneroster_service = None
            self.oneroster_error_code = "CMP-AUTH-REQUIRED"
            return
        existing = self.oneroster_service
        if (
            existing is not None
            and str(getattr(existing, "domain", "")).casefold() == domain
        ):
            self.oneroster_error_code = ""
            return
        try:
            module = importlib.import_module("gamgui.components.oneroster")
            service_type = getattr(module, "OneRosterService")
            root = self.ensure_component_manager().component_data_root
            self.oneroster_service = service_type(
                domain,
                root,
                activity_registry=self.activity_registry,
            )
            self.oneroster_error_code = ""
            self._schedule_oneroster_gate()
        except Exception:
            self.oneroster_service = None
            self.oneroster_error_code = "CMP-INCOMPATIBLE"

    def rebind_component_services(self) -> None:
        """Discard optional service handles and recreate them from retained local state."""

        if isinstance(self.oneroster_gate_task, asyncio.Task):
            self.oneroster_gate_task.cancel()
        self.oneroster_gate_task = None
        self.oneroster_service = None
        self.oneroster_error_code = ""
        self.ensure_component_services()

    def _schedule_oneroster_gate(self) -> None:
        """Start the local scheduler without reading Workspace data at discovery time."""

        if self.activation_probe_pending():
            return
        if self.oneroster_service is None or self.connector is None:
            return
        current = self.oneroster_gate_task
        if isinstance(current, asyncio.Task) and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.oneroster_gate_task = loop.create_task(
            self._run_oneroster_gate_scheduler(),
            name="oneroster-student-release",
        )

    async def _run_oneroster_gate_scheduler(self) -> None:
        """Open and finish only a previously confirmed, due student manifest."""

        while True:
            try:
                await self._process_due_oneroster_gate()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.oneroster_gate_error = str(
                    getattr(exc, "code", "") or "OR-GATE-SCHEDULER"
                )
            await asyncio.sleep(30)

    async def _process_due_oneroster_gate(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        """Run one scheduler tick; exposed as a deterministic test seam."""

        service = self.oneroster_service
        connector = self.connector
        if service is None or connector is None:
            return False
        enabled_check = getattr(self, "oneroster_enabled", None)
        if callable(enabled_check) and not enabled_check():
            return False
        gate = service.get_gate()
        state = str(getattr(getattr(gate, "state", ""), "value", getattr(gate, "state", "")))
        manifest_id = str(getattr(gate, "manifest_id", "") or "")
        if not manifest_id:
            return False
        moment = now or datetime.now(timezone.utc)
        if state == "ARMED":
            from gamgui.components.oneroster.models import parse_aware_datetime

            release_at = parse_aware_datetime(str(getattr(gate, "release_at", "") or ""))
            if moment < release_at:
                return False
            try:
                await service.revalidate_scheduled_gate(
                    connector,
                    manifest_id,
                    now=moment,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if (
                    self.oneroster_service is not service
                    or self.connector is not connector
                    or (callable(enabled_check) and not enabled_check())
                ):
                    return False
                hold_failure = getattr(
                    service,
                    "hold_scheduled_gate_failure",
                    None,
                )
                if not callable(hold_failure):
                    raise
                held = hold_failure(manifest_id, exc, now=moment)
                self.oneroster_gate_error = str(
                    getattr(held, "hold_code", "")
                    or getattr(exc, "code", "")
                    or "OR-GATE-REVALIDATION-FAILED"
                )
                return False
            if (
                self.oneroster_service is not service
                or self.connector is not connector
                or (callable(enabled_check) and not enabled_check())
            ):
                return False
            gate = service.get_gate()
            state = str(
                getattr(getattr(gate, "state", ""), "value", getattr(gate, "state", ""))
            )
        if state != "OPEN":
            return False
        if (
            self.oneroster_service is not service
            or self.connector is not connector
            or (callable(enabled_check) and not enabled_check())
        ):
            return False
        manifest = service.get_manifest_header(manifest_id)
        if str(getattr(manifest, "status", "")) != "awaiting_students":
            return False
        await service.execute_manifest(connector, manifest_id, now=moment)
        self.oneroster_gate_error = ""
        return True

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
        try:
            lease = self.activity_registry.acquire("connector-rebind")
        except ActivityBusyError as exc:
            raise RuntimeError(
                "Finish or stop the active administrative operation before reconnecting."
            ) from exc
        try:
            self._activate_connector_unlocked(connector)
        finally:
            lease.release()

    def _activate_connector_unlocked(self, connector: GAMConnector) -> None:
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
            new_entitlement_store = self.entitlement_store or EntitlementStore(
                base_dir / "classroom_teacher_entitlements.db"
                if base_dir is not None
                else default_entitlement_store_path()
            )
            new_entitlement_service = EntitlementService(
                connector, domain, new_entitlement_store
            )
            new_entitlement_scheduler = (
                self.entitlement_scheduler or LaunchAgentManager()
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
        old_oneroster = self.oneroster_service
        old_refresh_tasks = list(self._directory_refresh_tasks.values())
        old_classroom_refresh = self.classroom_refresh_task
        (
            self.connector,
            self.audit_domain,
            self.directory_index,
            self.classroom_index,
            self.classroom_manifests,
            self.classroom_service,
            self.entitlement_store,
            self.entitlement_service,
            self.entitlement_scheduler,
            self.drive_service,
        ) = (
            connector,
            domain,
            new_directory_index,
            new_classroom_index,
            new_classroom_manifests,
            new_classroom_service,
            new_entitlement_store,
            new_entitlement_service,
            new_entitlement_scheduler,
            new_drive_service,
        )
        self.user_cache = UserCache()
        self.jobs.clear()
        self.builder_sequence.clear()
        self.classroom_manifest_tasks.clear()
        self.classroom_manifest_errors.clear()
        for task in self.oneroster_manifest_tasks.values():
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
        self.oneroster_manifest_tasks.clear()
        self.oneroster_manifest_errors.clear()
        if isinstance(self.oneroster_gate_task, asyncio.Task):
            self.oneroster_gate_task.cancel()
        self.oneroster_gate_task = None
        self.oneroster_gate_error = ""
        invalidator = getattr(old_oneroster, "invalidate_scope_readiness", None)
        if callable(invalidator):
            invalidator()
        self.classroom_refresh_task = None
        self.classroom_refresh_error = ""
        self._directory_refresh_tasks = {}
        for task in (*old_refresh_tasks, old_classroom_refresh):
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
        if old_drive is not None and old_drive is not self.drive_service:
            self._schedule_drive_close(old_drive)
        self.ensure_component_services()
        self._schedule_oneroster_gate()

    def has_active_admin_jobs(self) -> bool:
        """Return whether rebinding services could interrupt an administrative mutation."""

        if self.activity_registry.is_active():
            return True
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
        for task in self.oneroster_manifest_tasks.values():
            done = getattr(task, "done", None)
            if callable(done) and not done():
                return True
        stores = (
            self.classroom_manifests,
            self.entitlement_store,
            getattr(self.drive_service, "operations", None),
            getattr(self.oneroster_service, "store", None),
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
        if self.entitlement_store is None:
            self.entitlement_store = EntitlementStore(
                base_dir / "classroom_teacher_entitlements.db"
                if base_dir is not None
                else default_entitlement_store_path()
            )
        if (
            self.entitlement_service is None
            or self.entitlement_service.connector is not connector
            or self.entitlement_service.domain != domain
        ):
            self.entitlement_service = EntitlementService(
                connector, domain, self.entitlement_store
            )
        if self.entitlement_scheduler is None:
            self.entitlement_scheduler = LaunchAgentManager()
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
            for task in self.oneroster_manifest_tasks.values()
            if isinstance(task, asyncio.Task)
        )
        if isinstance(self.oneroster_gate_task, asyncio.Task):
            tasks.append(self.oneroster_gate_task)
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
        close_oneroster = getattr(self.oneroster_service, "close", None)
        if callable(close_oneroster):
            result = close_oneroster()
            if asyncio.iscoroutine(result):
                await result

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
        allow_first_run_workspace_access: bool = False,
    ) -> "AppState":
        sweep_stale_configs()  # clean up any credential temp dirs orphaned by a prior crash/kill
        vault = vault or SecretsVault()
        runner = GAMRunner(vault=vault)
        component_manager = ComponentManager(
            registry=global_activity_registry,
        )
        # First launch deliberately presents Optional Features before touching the
        # Workspace Keychain. Choosing Skip or staging the full profile records the
        # preference; subsequent launches may discover existing Workspace credentials.
        if (
            component_manager.first_run_choice_pending()
            and not allow_first_run_workspace_access
        ):
            domain = ""
        else:
            domains = vault.list_domains()
            requested = (preferred_domain or "").strip().casefold()
            domain = (
                requested
                if requested in {str(item).casefold() for item in domains}
                else ""
            )
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
            component_manager=component_manager,
            activity_registry=global_activity_registry,
        )

    @classmethod
    def create_activation_probe(
        cls,
        token: Optional[str] = None,
    ) -> "AppState":
        """Create a sealed-profile health state with no Keychain or GAM access."""

        vault = SecretsVault(backend=InMemoryBackend(), cache_ttl=0)
        runner = GAMRunner(vault=vault)
        component_manager = ComponentManager(
            registry=global_activity_registry,
        )
        return cls(
            vault=vault,
            runner=runner,
            token=token or secrets.token_urlsafe(24),
            component_manager=component_manager,
            activity_registry=global_activity_registry,
            activation_probe_mode=True,
        )


class TokenGateMiddleware(BaseHTTPMiddleware):
    """Reject cross-origin callers, allow static assets, else require the launch token
    (cookie, else ?token= which sets it).

    The origin check comes first because the cookie alone is not enough: cookies are not port-scoped
    (RFC 6265 §8.5), so SameSite=Strict treats *every* port on 127.0.0.1 as the same site. Without
    this, a page served by any other local process could fire a form POST at us — a simple request,
    so no preflight — and the browser would helpfully attach our token cookie.

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

    # Sec-Fetch-Site values that mean another origin initiated this. "same-site" is included on
    # purpose: that is exactly the other-port-on-loopback case. Anything else (including a missing
    # header, or a value a future browser invents) falls through to the Origin check below.
    CROSS_ORIGIN_FETCH_SITES = frozenset({"cross-site", "same-site"})

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    def _token_ok(self, candidate: "str | None") -> bool:
        # Constant-time compare (defence-in-depth vs. a local process timing the loopback auth).
        # Compared as UTF-8 bytes: compare_digest's str form rejects codepoints > 127 with a
        # TypeError, and a cookie or query string can carry those, which would turn a bad token
        # into a 500 instead of a 403.
        if candidate is None:
            return False
        return secrets.compare_digest(candidate.encode("utf-8"), self._token.encode("utf-8"))

    def _same_origin(self, request: Request) -> bool:
        if request.headers.get("sec-fetch-site", "").lower() in self.CROSS_ORIGIN_FETCH_SITES:
            return False
        origin = request.headers.get("origin")
        if origin is None:
            # Browsers always send Origin cross-origin, so absence means a same-origin navigation
            # or a non-browser client (the native WKWebView's initial load, curl, the test client).
            return True
        # The listening port is picked at runtime, so our own authority is whatever Host says.
        return origin.casefold() == f"{request.url.scheme}://{request.headers.get('host', '')}".casefold()

    def _secure(self, response):
        for key, value in self.SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    async def dispatch(self, request: Request, call_next):
        if not self._same_origin(request):
            return self._secure(JSONResponse({"error": "forbidden"}, status_code=403))

        if request.url.path.startswith("/static") or request.url.path == "/healthz":
            return self._secure(await call_next(request))

        if self._token_ok(request.cookies.get(TOKEN_COOKIE)):
            return self._secure(await call_next(request))

        if self._token_ok(request.query_params.get("token")):
            response = await call_next(request)
            # A session cookie: the token is regenerated every launch, so persisting it for a day
            # only widens the window in which it can be lifted out of the browser's cookie jar.
            response.set_cookie(TOKEN_COOKIE, self._token, httponly=True, samesite="strict")
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


class ActivationProbeGateMiddleware(BaseHTTPMiddleware):
    """Keep the health-start process read-only until activation is committed."""

    def __init__(self, app, state: AppState) -> None:
        super().__init__(app)
        self._state = state

    async def dispatch(self, request: Request, call_next):
        probe_active = (
            self._state.activation_probe_mode
            or self._state.activation_probe_pending()
        )
        method = request.method.upper()
        path = request.url.path
        if probe_active and not (
            method in {"GET", "HEAD", "OPTIONS"}
            and path in {"/", "/healthz"}
        ):
            return JSONResponse(
                {
                    "error": "The verified update is finishing activation.",
                    "code": "CMP-ACTIVE-JOB",
                },
                status_code=409,
            )
        return await call_next(request)


def create_app(state: AppState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state._schedule_activation_commit_watch()
        yield
        await state.aclose()

    app = FastAPI(
        title="GamGUI",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.gamgui = state
    # The request cap sits inside the token gate but outside FastAPI's multipart
    # parser, so unauthenticated requests are rejected before their body is read.
    app.add_middleware(
        RequestBodyLimitMiddleware,
        path="/classroom/imports/upload",
        maximum_bytes=(250 * 1024 * 1024) + (1024 * 1024),
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        path="/classroom/access/save",
        maximum_bytes=11 * 1024 * 1024,
    )
    app.add_middleware(PrivacyTimingMiddleware)
    app.add_middleware(ActivationProbeGateMiddleware, state=state)
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
    async def index(request: Request):
        st: AppState = request.app.state.gamgui
        if st.activation_probe_mode:
            return HTMLResponse(
                """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="2">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GamGUI update</title></head>
<body><main><h1>Finishing verified update</h1>
<p>GamGUI is validating the installed profile. Workspace access remains paused.</p>
</main></body></html>"""
            )
        manager = st.ensure_component_manager()
        component_status = manager.status()
        if (
            manager.first_run_choice_pending()
            or component_status.restart_required
        ):
            # First-launch optional-feature selection must precede every Workspace
            # Keychain read. The setup route has a component-only rendering mode.
            return RedirectResponse("/setup", status_code=303)
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
    from .routes.classroom_access import router as classroom_access_router
    from .routes.components import (
        core_deep_link_router,
        router as components_router,
    )
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
    app.include_router(classroom_access_router)
    app.include_router(components_router)
    if state.embedded_profile() == ONEROSTER_PROFILE:
        try:
            oneroster_module = importlib.import_module(
                "gamgui.web.routes.oneroster"
            )
            component = next(
                (
                    item
                    for item in state.ensure_component_manager().embedded.components
                    if item.component_id == ONEROSTER_COMPONENT
                ),
                None,
            )
            if component is None:
                raise RuntimeError("The embedded OneRoster manifest is missing.")
            registered_paths = tuple(
                str(getattr(route, "path", "") or "")
                for route in oneroster_module.router.routes
            )
            if not registered_paths or any(
                not any(
                    path == prefix or path.startswith(prefix + "/")
                    for prefix in component.route_prefixes
                )
                for path in registered_paths
            ):
                raise RuntimeError(
                    "The optional router registered a path outside its embedded allowlist."
                )
            app.include_router(oneroster_module.router)
        except Exception:
            # Optional-code corruption cannot prevent Core from starting. The
            # permanent Components page remains available for recovery.
            state.oneroster_service = None
            state.oneroster_error_code = "CMP-INCOMPATIBLE"
            app.include_router(core_deep_link_router)
    else:
        app.include_router(core_deep_link_router)
    app.include_router(drive_router)
    app.include_router(lifecycle_router)
    app.include_router(onboarding_router)
    app.include_router(builder_router)
    app.include_router(audit_router)
    return app
