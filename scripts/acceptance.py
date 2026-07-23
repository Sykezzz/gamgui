#!/usr/bin/env python3
"""Privacy-safe, bounded live acceptance pass for a validated GamGUI build.

The live probes are read-only, but they still contact the configured Google Workspace tenant.
Run them only after administrator sign-off.  Output is deliberately restricted to fixed check
names, pass/fail state, and elapsed time; tenant identifiers, returned records, and exception text
are never printed or persisted by this script.

Prerequisites: the vendored binary, completed Workspace setup, and an approved canary subject.

    .venv/bin/python scripts/acceptance.py
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

# Direct execution sets sys.path[0] to ``scripts/``.  Add the repository root so the documented
# ``python scripts/acceptance.py`` command imports the source package without requiring installation.
_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[1])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from gamgui.core.canary import CanaryConfigStore, run_live_canary
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.drive import DriveAPIClient, ServiceAccountTokenProvider
from gamgui.core.gam.commands import EXPECTED_GAM_VERSION
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import SecretsVault

_LIVE_CHECKS = ("users", "groups", "classroom", "drive")
_DISPLAY_NAMES = {
    "gam_version": "GAM version",
    "users": "Directory user",
    "groups": "Directory groups",
    "classroom": "Classroom courses",
    "drive": "Drive files",
}


class _RejectingAudit:
    """Make an accidental mutation fail instead of writing data-bearing audit evidence."""

    def record(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Acceptance probes cannot record mutations.")


def _create_state(domain: str) -> Any:
    """Create only the clients needed by the canary; do not initialize app indexes/manifests."""

    vault = SecretsVault()
    configured_domains = {
        str(value).strip().casefold() for value in vault.list_domains()
    }
    if domain not in configured_domains or not vault.has_credentials(domain):
        return None
    runner = GAMRunner(vault=vault)
    connector = GAMConnector(
        runner=runner,
        domain=domain,
        audit=_RejectingAudit(),
    )
    drive_client = DriveAPIClient(ServiceAccountTokenProvider(vault, domain))
    return SimpleNamespace(
        vault=vault,
        runner=runner,
        audit_domain=domain,
        connector=connector,
        drive_service=drive_client,
    )


def _safe_duration(value: object) -> float:
    """Return a finite, non-negative duration without rendering arbitrary values."""

    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(duration):
        return 0.0
    return max(0.0, duration)


def _line(name: str, ok: bool, duration_ms: object) -> str:
    """Render only allow-listed labels and timing evidence."""

    label = _DISPLAY_NAMES[name]
    status = "PASS" if ok else "FAIL"
    return f"  {status:4}  {label:18} {_safe_duration(duration_ms):10.3f} ms"


async def _timed_version_check(state: Any) -> tuple[bool, float]:
    started = time.perf_counter()
    ok = False
    try:
        reported = await state.runner.version()
        ok = EXPECTED_GAM_VERSION in str(reported)
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception:
        # Version command output and exception text are intentionally discarded.
        ok = False
    return ok, (time.perf_counter() - started) * 1000.0


def _live_results(result: object) -> dict[str, tuple[bool, float]]:
    """Extract the fixed canary denominator and discard every other returned field."""

    if not isinstance(result, dict):
        return {}
    raw_checks = result.get("checks")
    if not isinstance(raw_checks, list):
        return {}

    checks: dict[str, tuple[bool, float]] = {}
    for item in raw_checks:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name not in _LIVE_CHECKS or name in checks:
            continue
        checks[name] = (
            item.get("ok") is True,
            _safe_duration(item.get("duration_ms")),
        )
    return checks


async def _close_state(state: Any) -> None:
    closer = getattr(state, "aclose", None)
    if not callable(closer):
        drive_service = getattr(state, "drive_service", None)
        closer = getattr(drive_service, "aclose", None)
    if not callable(closer):
        client = getattr(getattr(state, "drive_service", None), "client", None)
        closer = getattr(client, "aclose", None)
    if callable(closer):
        with suppress(Exception):
            await closer()


async def main() -> int:
    """Run the fixed live-check denominator without exposing tenant data."""

    print("GamGUI bounded acceptance (privacy-safe output)")
    config = CanaryConfigStore().load()
    if config is None:
        print("  SETUP REQUIRED  An approved canary is not configured; no live probes ran.")
        return 2

    state: Any = None
    try:
        # The configured domain is used only to select the local credential set.  It is never
        # included in output or acceptance evidence.
        state = _create_state(config.domain)
        if state is None:
            print("  SETUP REQUIRED  The approved local credentials are unavailable.")
            return 2
        version_ok, version_ms = await _timed_version_check(state)
        result = await run_live_canary(state=state)
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception:
        # A traceback or exception string can contain tenant identifiers, paths, queries, or
        # response bodies.  Fail closed with a fixed message instead.
        print("  FAIL  Acceptance could not complete; details were intentionally suppressed.")
        return 1
    finally:
        if state is not None:
            await _close_state(state)

    print(_line("gam_version", version_ok, version_ms))
    checks = _live_results(result)
    for name in _LIVE_CHECKS:
        ok, duration_ms = checks.get(name, (False, 0.0))
        print(_line(name, ok, duration_ms))

    if set(checks) != set(_LIVE_CHECKS):
        print("  SETUP REQUIRED  The fixed live-check denominator was unavailable.")
        return 2

    passed = (
        version_ok
        and result.get("ok") is True
        and all(ok for ok, _duration in checks.values())
    )
    print(
        "PASS: all bounded read-only checks passed."
        if passed
        else "FAIL: one or more bounded read-only checks failed."
    )
    return 0 if passed else 1


def cli(run: Callable[[], Awaitable[int]] = main) -> int:
    """Protect the command-line boundary from data-bearing exception tracebacks."""

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print("Acceptance interrupted; no record data was emitted.")
        return 130
    except Exception:
        print("Acceptance failed; details were intentionally suppressed.")
        return 1


if __name__ == "__main__":
    sys.exit(cli())
