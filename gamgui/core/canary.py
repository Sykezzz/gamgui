"""Privacy-safe, bounded live checks used before activating a local update.

The configured admin subject is local updater configuration, not canary evidence.  Persisted
canary evidence contains only check names, pass/fail state, timings, and a timestamp; it never
contains tenant identifiers, returned resource identifiers, tokens, or exception text.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Protocol

import httpx

from .drive.client import ServiceAccountTokenProvider, TokenProvider
from .paths import app_data_dir

if TYPE_CHECKING:
    from ..web.server import AppState

CANARY_SUBJECT_ENV = "GAMGUI_CANARY_SUBJECT"
CANARY_DOMAIN_ENV = "GAMGUI_CANARY_DOMAIN"
GROUP_SCOPE = "https://www.googleapis.com/auth/admin.directory.group.readonly"
CLASSROOM_SCOPE = "https://www.googleapis.com/auth/classroom.courses"
CANARY_CHECK_NAMES = ("users", "groups", "classroom", "drive")


def _owner_only(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _owner_only(tmp)
    os.replace(tmp, path)
    _owner_only(path)


@dataclass(frozen=True)
class CanaryConfig:
    domain: str
    subject: str


class CanaryConfigStore:
    """Owner-only local configuration for the approved read-only canary subject."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or app_data_dir() / "updates" / "canary-config.json"

    def load(self) -> Optional[CanaryConfig]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        domain = str(payload.get("domain") or "").strip().casefold()
        subject = str(payload.get("subject") or "").strip().casefold()
        if not domain or not subject:
            return None
        return CanaryConfig(domain=domain, subject=subject)

    def save(self, domain: str, subject: str) -> CanaryConfig:
        config = CanaryConfig(
            domain=(domain or "").strip().casefold(),
            subject=(subject or "").strip().casefold(),
        )
        if not config.domain or not config.subject:
            raise ValueError("A domain and delegated canary subject are required.")
        _atomic_json(self.path, asdict(config))
        return config


@dataclass(frozen=True)
class CanaryCheck:
    name: str
    ok: bool
    duration_ms: float


class CanaryResultStore:
    """Persist only non-identifying activation evidence."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or app_data_dir() / "updates" / "canary-last.json"

    def save(self, result: dict) -> None:
        checks_by_name: dict[str, dict] = {}
        for item in result.get("checks", ()):
            if isinstance(item, CanaryCheck):
                item = asdict(item)
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            ok = item.get("ok")
            try:
                duration_ms = float(item.get("duration_ms"))
            except (TypeError, ValueError, OverflowError):
                continue
            if (
                name not in CANARY_CHECK_NAMES
                or name in checks_by_name
                or type(ok) is not bool
                or not math.isfinite(duration_ms)
                or duration_ms < 0
            ):
                continue
            checks_by_name[name] = {
                "name": name,
                "ok": ok,
                "duration_ms": round(duration_ms, 3),
            }
        checks = [
            checks_by_name[name]
            for name in CANARY_CHECK_NAMES
            if name in checks_by_name
        ]
        try:
            checked_at = float(result.get("checked_at"))
        except (TypeError, ValueError, OverflowError):
            checked_at = time.time()
        if not math.isfinite(checked_at) or checked_at < 0:
            checked_at = time.time()
        payload = {
            "ok": (
                result.get("ok") is True
                and len(checks) == len(CANARY_CHECK_NAMES)
                and all(item["ok"] for item in checks)
            ),
            "checked_at": round(checked_at, 3),
            "checks": checks,
        }
        _atomic_json(self.path, payload)


def validate_canary_result(result: object, *, require_success: bool = False) -> dict:
    """Validate candidate-app output against the fixed, privacy-safe canary contract."""

    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise ValueError("The update canary returned an invalid result.")
    checks = result.get("checks")
    if not isinstance(checks, list) or len(checks) != len(CANARY_CHECK_NAMES):
        raise ValueError("The update canary did not return the required checks.")

    checks_by_name: dict[str, dict] = {}
    for item in checks:
        if not isinstance(item, dict):
            raise ValueError("The update canary returned an invalid check.")
        name = item.get("name")
        ok = item.get("ok")
        try:
            duration_ms = float(item.get("duration_ms"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("The update canary returned an invalid timing.") from exc
        if (
            name not in CANARY_CHECK_NAMES
            or name in checks_by_name
            or type(ok) is not bool
            or not math.isfinite(duration_ms)
            or duration_ms < 0
        ):
            raise ValueError("The update canary returned an invalid check.")
        checks_by_name[name] = {
            "name": name,
            "ok": ok,
            "duration_ms": round(duration_ms, 3),
        }

    if tuple(checks_by_name) != CANARY_CHECK_NAMES:
        raise ValueError("The update canary returned unexpected or reordered checks.")
    calculated_ok = all(item["ok"] for item in checks_by_name.values())
    if result["ok"] is not calculated_ok:
        raise ValueError("The update canary result did not match its checks.")
    if require_success and not calculated_ok:
        raise ValueError("The update canary failed.")

    try:
        checked_at = float(result.get("checked_at"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("The update canary returned an invalid timestamp.") from exc
    if not math.isfinite(checked_at) or checked_at < 0:
        raise ValueError("The update canary returned an invalid timestamp.")
    return {
        "ok": calculated_ok,
        "checked_at": checked_at,
        "checks": list(checks_by_name.values()),
    }


class CanaryPageProbe(Protocol):
    async def one_group_page(self, subject: str) -> object: ...

    async def one_course_page(self, subject: str) -> object: ...

    async def aclose(self) -> None: ...


class DelegatedCanaryPageProbe:
    """Issue true one-item Directory and Classroom API page reads."""

    def __init__(self, vault, domain: str, http: Optional[httpx.AsyncClient] = None) -> None:
        self.group_token_provider = ServiceAccountTokenProvider(
            vault,
            domain,
            scopes=(GROUP_SCOPE,),
        )
        self.classroom_token_provider = ServiceAccountTokenProvider(
            vault,
            domain,
            scopes=(CLASSROOM_SCOPE,),
        )
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        self._owns_http = http is None

    async def _get(
        self,
        token_provider: TokenProvider,
        subject: str,
        url: str,
        params: dict,
    ) -> object:
        token = await token_provider.token_for(subject)
        response = await self.http.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        return response.json()

    async def one_group_page(self, subject: str) -> object:
        return await self._get(
            self.group_token_provider,
            subject,
            "https://admin.googleapis.com/admin/directory/v1/groups",
            {
                "userKey": subject,
                "maxResults": 1,
                "fields": "groups(id),nextPageToken",
            },
        )

    async def one_course_page(self, subject: str) -> object:
        return await self._get(
            self.classroom_token_provider,
            subject,
            "https://classroom.googleapis.com/v1/courses",
            {
                "teacherId": subject,
                "pageSize": 1,
                "fields": "courses(id),nextPageToken",
            },
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()


async def _timed_check(name: str, call: Callable[[], Awaitable[object]]) -> CanaryCheck:
    started = time.perf_counter()
    ok = False
    try:
        await call()
        ok = True
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception:
        # Exception text can contain emails, file IDs, queries, or response bodies.  It is
        # deliberately discarded here and never enters updater state or persisted evidence.
        ok = False
    return CanaryCheck(
        name=name,
        ok=ok,
        duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
    )


async def run_live_canary(
    state: Optional["AppState"] = None,
    *,
    config_store: Optional[CanaryConfigStore] = None,
    result_store: Optional[CanaryResultStore] = None,
    page_probe: Optional[CanaryPageProbe] = None,
) -> dict:
    """Run four bounded, read-only tenant checks and return non-identifying evidence."""

    config_store = config_store or CanaryConfigStore()
    result_store = result_store or CanaryResultStore()
    config = config_store.load()
    environment_subject = os.environ.get(CANARY_SUBJECT_ENV, "").strip().casefold()
    environment_domain = os.environ.get(CANARY_DOMAIN_ENV, "").strip().casefold()
    if config is None and environment_subject:
        # The active AppState domain is validated below.  Environment use is intentionally
        # ephemeral and does not write the subject into the result store.
        config = CanaryConfig(domain=environment_domain, subject=environment_subject)

    owns_state = state is None
    if state is None:
        from ..web.server import AppState

        # The candidate runs against a disposable data root, where the ordinary
        # first-launch component choice is intentionally unanswered. This explicit
        # canary-only override allows the bounded read-only proof to resolve the
        # existing Keychain credentials without mutating that scratch component state.
        state = AppState.create(
            preferred_domain=config.domain if config else "",
            allow_first_run_workspace_access=True,
        )

    drive_service = None
    owned_probe: Optional[CanaryPageProbe] = None
    try:
        connector = getattr(state, "connector", None)
        drive_service = getattr(state, "drive_service", None)
        active_domain = str(getattr(state, "audit_domain", "") or "").strip().casefold()
        configured = bool(
            config
            and connector is not None
            and drive_service is not None
            and (not config.domain or config.domain == active_domain)
        )

        checks: list[CanaryCheck] = []
        if configured and config is not None:
            subject = config.subject
            if page_probe is None:
                owned_probe = DelegatedCanaryPageProbe(
                    getattr(state, "vault"),
                    active_domain,
                )
                page_probe = owned_probe
            checks = [
                await _timed_check(
                    CANARY_CHECK_NAMES[0],
                    lambda: connector.get_user(
                        subject,
                        fields=("primaryEmail", "suspended"),
                    ),
                ),
                await _timed_check(
                    CANARY_CHECK_NAMES[1],
                    lambda: page_probe.one_group_page(subject),
                ),
                await _timed_check(
                    CANARY_CHECK_NAMES[2],
                    lambda: page_probe.one_course_page(subject),
                ),
                await _timed_check(
                    CANARY_CHECK_NAMES[3],
                    lambda: drive_service.list_owned_files(subject, page_size=1),
                ),
            ]

        result = {
            "ok": (
                configured
                and len(checks) == len(CANARY_CHECK_NAMES)
                and all(check.ok for check in checks)
            ),
            "checked_at": time.time(),
            "checks": [asdict(check) for check in checks],
        }
        result_store.save(result)
        return result
    finally:
        if owned_probe is not None:
            await owned_probe.aclose()
        if owns_state:
            closer = getattr(state, "aclose", None)
            if callable(closer):
                await closer()
            elif drive_service is not None:
                client = getattr(drive_service, "client", None)
                close_client = getattr(client, "aclose", None)
                if callable(close_client):
                    await close_client()
