
"""Application entry point.

Starts the local FastAPI server on a random loopback port and opens it in a native WKWebView
window via pywebview. If pywebview isn't installed (e.g. headless dev), it prints the tokenized URL
and keeps serving so you can open it in a browser.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

import uvicorn

from .core.activity import activity_registry
from .core.activation_lock import ActivationLockError, OwnerOnlyActivationLock
from .core.components import ComponentManager
from .core.paths import APP_DATA_ENV, app_data_dir
from .core.persisted_activity import inspect_persisted_operations
from .core.secrets.ephemeral import sweep_stale_configs, wipe_live_configs
from .core.updater import (
    ACTIVATION_APP_UPDATE,
    ACTIVATION_RECOVERY_ENV,
    ACTIVATION_PROBE_ENV,
    LocalUpdateInstaller,
    UpdateCoordinator,
    UpdateStateStore,
    activation_evidence_valid,
    bundle_self_test,
    candidate_is_blocked,
    installed_app_path,
    prepare_database_schemas,
    wait_for_process_exit,
    write_health_marker_from_environment,
)
from .web.server import AppState, create_app

# Seconds uvicorn may spend waiting for in-flight requests at shutdown. This has to be set: with no
# graceful-shutdown timeout uvicorn waits forever and never cancels, so a gam call still running
# when the window closes keeps its materialized credentials on disk. With it, the handler is
# cancelled, the `with EphemeralConfig(...)` block unwinds and the dir is wiped. Long enough for a
# quick call to finish, short enough that quitting still feels instant.
_GRACEFUL_SHUTDOWN_SECONDS = 3
_UPDATE_CONFIRM_TIMEOUT_SECONDS = 5 * 60


def _free_loopback_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _BackgroundServer:
    def __init__(self, app, host: str, port: int) -> None:
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS,
        )
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        # Block until uvicorn reports it has bound the socket.
        while not self.server.started:
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=_GRACEFUL_SHUTDOWN_SECONDS + 2)
        # Whatever the daemon thread was still holding is orphaned now: detached tasks (bulk
        # signature apply, Builder sequences) die with the loop without unwinding their context
        # managers. Wipe our own dirs first, then sweep anything a previous run left behind.
        wipe_live_configs()
        sweep_stale_configs(max_age_seconds=0)


def _fit_size(screen_w: int, screen_h: int) -> "tuple[int, int]":
    """A window size that uses most of the display but always fits it — including 13" Macs.

    Leaves room for the menu bar/dock, caps the size on large external monitors so it never opens
    absurdly wide, respects the 900×600 minimum, and never exceeds the screen.
    """
    w = max(900, min(screen_w - 40, 1600))
    h = max(600, min(screen_h - 90, 1000))
    return min(w, screen_w), min(h, screen_h)


def _arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--canary", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--apply-update-helper", action="store_true")
    parser.add_argument("--recover-update-helper", action="store_true")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--candidate-sha", default="")
    parser.add_argument("--pending-app", default="")
    parser.add_argument("--current-app", default="")
    parser.add_argument("--activation-lock-fd", type=int, default=-1)
    parser.add_argument("--activation-transaction", default="")
    parser.add_argument("--headless-task", default="")
    parser.add_argument("--policy-id", default="")
    parser.add_argument("--scheduled", action="store_true")
    # macOS may append process-serial arguments; they are intentionally ignored.
    args, _unknown = parser.parse_known_args(argv)
    return args


def _active_admin_jobs(state: AppState, *, include_registry: bool = True) -> bool:
    if include_registry and activity_registry.is_active():
        return True
    terminal = {"completed", "failed", "cancelled", "interrupted"}
    for job in state.jobs.values():
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
    for task in getattr(state, "classroom_manifest_tasks", {}).values():
        done = getattr(task, "done", None)
        if callable(done) and not done():
            return True
    for task in getattr(state, "oneroster_manifest_tasks", {}).values():
        done = getattr(task, "done", None)
        if callable(done) and not done():
            return True
    drive_service = getattr(state, "drive_service", None)
    oneroster_service = getattr(state, "oneroster_service", None)
    stores = (
        getattr(state, "classroom_manifests", None),
        getattr(drive_service, "operations", None),
        getattr(oneroster_service, "store", None),
    )
    for store in stores:
        checker = getattr(store, "has_active_jobs", None)
        if callable(checker) and checker():
            return True
    return False


def _start_update_preparation(state: AppState) -> None:
    if (
        sys.platform != "darwin"
        or installed_app_path() is None
        or os.environ.get(ACTIVATION_PROBE_ENV) == "1"
        or os.environ.get("GAMGUI_UPDATE_HEALTH_MARKER")
    ):
        return
    manager = getattr(state, "component_manager", None)
    first_run_pending = getattr(manager, "first_run_choice_pending", None)
    try:
        if callable(first_run_pending) and first_run_pending():
            return
        component_store = getattr(manager, "store", None)
        load_component_state = getattr(component_store, "load", None)
        if callable(load_component_state):
            component_state = load_component_state()
            if str(
                getattr(component_state, "component_operation", "") or ""
            ) == "preparing":
                return
    except (OSError, ValueError, RuntimeError):
        # A corrupt or unavailable local component state must not cause a
        # background updater to read Workspace credentials behind onboarding.
        return
    coordinator = UpdateCoordinator(
        active_jobs=lambda: _active_admin_jobs(state, include_registry=False),
        activity_registry=activity_registry,
    )
    threading.Thread(
        target=coordinator.check_and_prepare,
        name="gamgui-update-check",
        daemon=True,
    ).start()


def _allow_window_close(
    state: AppState,
    notify: Optional[Callable[[str], None]] = None,
) -> bool:
    """Refuse desktop shutdown while an administrative mutation is active."""

    if not _active_admin_jobs(state):
        return True
    if notify is not None:
        try:
            notify(
                "GamGUI is still completing an administrative operation. "
                "Wait for it to finish before closing the app."
            )
        except Exception:
            pass
    return False


def _confirm_automatic_update(state) -> bool:
    """Explain an automatic app activation before the launcher exits."""

    if getattr(state, "activation_kind", "") != ACTIVATION_APP_UPDATE:
        return True
    artifact = getattr(state, "candidate_artifact", None)
    version = str(getattr(artifact, "version", "") or "").strip()
    script = """
on run argv
    set candidateVersion to item 1 of argv
    set versionText to ""
    if candidateVersion is not "" then set versionText to " " & candidateVersion
    display dialog "GamGUI" & versionText & " is ready to install." & return & return & "GamGUI will close, verify the update in the background, and reopen automatically. Your Workspace data will not be changed during this check." with title "GamGUI update ready" buttons {"Not now", "Install and restart"} default button "Install and restart" cancel button "Not now" with icon note
    return button returned of result
end run
""".strip()
    try:
        completed = subprocess.run(
            ["/usr/bin/osascript", "-e", script, "--", version],
            check=False,
            capture_output=True,
            text=True,
            timeout=_UPDATE_CONFIRM_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (
        completed.returncode == 0
        and completed.stdout.strip() == "Install and restart"
    )


def _handoff_pending_update() -> bool:
    if sys.platform != "darwin":
        return False
    if os.environ.get("GAMGUI_SKIP_UPDATE_ONCE") == "1":
        # Candidate health startup must retain this flag until the component
        # manager has projected the exact staged profile. Ordinary rollback
        # relaunches have no health marker and consume the one-shot guard.
        if not os.environ.get("GAMGUI_UPDATE_HEALTH_MARKER"):
            os.environ.pop("GAMGUI_SKIP_UPDATE_ONCE", None)
        return False
    if installed_app_path() is None:
        return False
    current = installed_app_path()
    if current is None:
        return False
    store = UpdateStateStore()
    coordinator = UpdateCoordinator(store=store)
    lock_path = app_data_dir() / "updates" / "activation.lock"
    try:
        lock = OwnerOnlyActivationLock.try_acquire(lock_path)
    except ActivationLockError:
        try:
            recovery_state = store.load()
            recovery_pending = bool(
                recovery_state.activation_journal is not None
                or recovery_state.activation_journal_invalid
            )
        except Exception:
            recovery_pending = True
        _record_activation_deferred(
            store,
            code="CMP-VERIFY-FAILED",
            message=(
                "The owner-only activation lock could not be verified; "
                "the current version was kept."
            ),
        )
        if recovery_pending:
            os.environ[ACTIVATION_RECOVERY_ENV] = "1"
            os.environ[ACTIVATION_PROBE_ENV] = "1"
        return False
    if lock is None:
        # Another launcher/helper owns the complete activation transaction.
        return True

    handed_off = False
    try:
        state = store.load()
        if state.activation_journal_invalid:
            os.environ[ACTIVATION_RECOVERY_ENV] = "1"
            os.environ[ACTIVATION_PROBE_ENV] = "1"
            return False
        recovering = state.activation_journal is not None
        if recovering:
            journal = state.activation_journal
            if journal is None:
                return False
            try:
                app_matches = Path(journal.current_app).resolve() == current.resolve()
            except OSError:
                app_matches = False
            if not app_matches:
                os.environ[ACTIVATION_RECOVERY_ENV] = "1"
                os.environ[ACTIVATION_PROBE_ENV] = "1"
                return False
            transaction = journal.transaction_id
            expected_sha = journal.candidate_sha
            pending = Path(journal.pending_app)
        else:
            journal = None
            transaction = ""
            expected_sha = state.candidate_sha
            pending = Path(state.pending_app) if state.pending_app else None
        executable = (
            pending / "Contents" / "MacOS" / "GamGUI"
            if pending is not None
            else None
        )
        if not recovering and pending is not None and not pending.is_dir():
            coordinator.recover_missing_pending(pending)
            return False
        if not recovering and (
            pending is None
            or not state.candidate_sha
            or candidate_is_blocked(state)
        ):
            return False
        if not recovering and (
            executable is None or not executable.is_file()
        ):
            coordinator.block(
                state.candidate_sha,
                "The staged update bundle was incomplete; the current version was kept.",
            )
            return False
        if not recovering and not activation_evidence_valid(state):
            coordinator.block(
                state.candidate_sha,
                "The staged update lacked required update evidence; "
                "the current version was kept.",
            )
            return False
        if not recovering and _activation_must_defer():
            _record_activation_deferred(
                store,
                code="CMP-ACTIVE-JOB",
                message=(
                    "The update was deferred because persisted administrative "
                    "operation state is active or could not be verified."
                ),
            )
            return False
        if not recovering and not _confirm_automatic_update(state):
            return False
        if not recovering and _activation_must_defer():
            _record_activation_deferred(
                store,
                code="CMP-ACTIVE-JOB",
                message=(
                    "The update was deferred because an administrative "
                    "operation became active before restart."
                ),
            )
            return False

        if not recovering:
            transaction = secrets.token_hex(16)
            state = store.load()
            if (
                state.activation_journal is not None
                or state.activation_journal_invalid
                or state.candidate_sha != expected_sha
                or Path(state.pending_app) != pending
                or candidate_is_blocked(state)
                or not activation_evidence_valid(state)
            ):
                return False
            state.activation_transaction_id = transaction
            state.component_error_code = ""
            state.last_error = ""
            try:
                store.save(state)
            except (OSError, TypeError, ValueError):
                return False
        helper_arguments = [
            sys.executable,
            "--apply-update-helper",
        ]
        if recovering:
            helper_arguments.append("--recover-update-helper")
        helper_arguments.extend(
            [
                "--parent-pid",
                str(os.getpid()),
                "--candidate-sha",
                expected_sha,
                "--pending-app",
                str(pending),
                "--current-app",
                str(current),
                "--activation-lock-fd",
                str(lock.fileno),
                "--activation-transaction",
                transaction,
            ]
        )
        try:
            subprocess.Popen(
                helper_arguments,
                close_fds=True,
                pass_fds=(lock.fileno,),
            )
        except (OSError, TypeError, ValueError, subprocess.SubprocessError):
            _record_activation_deferred(
                store,
                transaction=transaction,
                code="CMP-VERIFY-FAILED",
                message=(
                    "The updater helper could not start; Workspace administration "
                    "remains paused until activation can be recovered."
                    if recovering
                    else "The updater helper could not start; the current version was kept."
                ),
            )
            if recovering:
                os.environ[ACTIVATION_RECOVERY_ENV] = "1"
                os.environ[ACTIVATION_PROBE_ENV] = "1"
            return False
        try:
            lock.close_after_handoff()
        except ActivationLockError:
            # The helper was already spawned with its inherited descriptor.
            # Exiting this launcher remains the only safe continuation.
            handed_off = True
            return True
        handed_off = True
        return True
    finally:
        if not handed_off and lock.held:
            try:
                lock.release()
            except ActivationLockError:
                pass


def _run_helper(args: argparse.Namespace) -> int:
    store = UpdateStateStore()
    lock_path = app_data_dir() / "updates" / "activation.lock"
    requested_app = Path(args.current_app)
    actual_app = installed_app_path()
    recovery_requested = bool(
        getattr(args, "recover_update_helper", False)
    )
    try:
        lock = OwnerOnlyActivationLock.adopt(
            lock_path,
            args.activation_lock_fd,
        )
    except (ActivationLockError, OSError, TypeError, ValueError):
        if actual_app is not None and wait_for_process_exit(args.parent_pid):
            _launch_existing_app(
                actual_app,
                recovery_pending=recovery_requested,
                candidate_sha=str(args.candidate_sha or ""),
            )
        return 1
    lease = None
    try:
        transaction = str(args.activation_transaction or "").lower()
        try:
            app_matches = (
                actual_app is not None
                and actual_app.resolve() == requested_app.resolve()
            )
        except OSError:
            app_matches = False
        if (
            not re.fullmatch(r"[0-9a-f]{32}", transaction)
            or not app_matches
        ):
            _record_activation_deferred(
                store,
                transaction=transaction,
                code="CMP-VERIFY-FAILED",
                message=(
                    "The updater helper bundle did not match the installed app."
                ),
            )
            if actual_app is not None and wait_for_process_exit(args.parent_pid):
                _launch_existing_app(
                    actual_app,
                    recovery_pending=recovery_requested,
                    candidate_sha=str(args.candidate_sha or ""),
                )
            return 1
        if not wait_for_process_exit(args.parent_pid):
            _record_activation_deferred(
                store,
                transaction=transaction,
                code="CMP-VERIFY-FAILED",
                message="The updater launcher did not exit in time.",
            )
            return 1

        try:
            lease = activity_registry.try_acquire("app-update-activation")
        except Exception:
            lease = None
        if lease is None:
            _record_activation_deferred(
                store,
                transaction=transaction,
                code="CMP-ACTIVE-JOB",
                message=(
                    "Activation recovery is waiting for an administrative "
                    "operation to finish."
                    if recovery_requested
                    else "The update was deferred because an administrative "
                    "operation is still active."
                ),
            )
            _launch_existing_app(
                requested_app,
                recovery_pending=recovery_requested,
                candidate_sha=str(args.candidate_sha or ""),
            )
            return 1
        if _persisted_activation_must_defer():
            _record_activation_deferred(
                store,
                transaction=transaction,
                code="CMP-ACTIVE-JOB",
                message=(
                    "Activation recovery is waiting for persisted administrative "
                    "operation state to become safe."
                    if recovery_requested
                    else "The update was deferred because persisted administrative "
                    "operation state is active or could not be verified."
                ),
            )
            _launch_existing_app(
                requested_app,
                recovery_pending=recovery_requested,
                candidate_sha=str(args.candidate_sha or ""),
            )
            return 1

        state = store.load()
        if recovery_requested:
            journal = state.activation_journal
            if (
                journal is None
                or state.activation_journal_invalid
                or journal.transaction_id != transaction
                or journal.candidate_sha != args.candidate_sha
                or Path(journal.current_app) != requested_app
                or Path(journal.pending_app) != Path(args.pending_app)
            ):
                _record_activation_deferred(
                    store,
                    transaction=transaction,
                    code="CMP-VERIFY-FAILED",
                    message=(
                        "The activation recovery transaction changed; Workspace "
                        "administration remains paused."
                    ),
                )
                _launch_existing_app(
                    requested_app,
                    recovery_pending=True,
                    candidate_sha=str(args.candidate_sha or ""),
                )
                return 1
            try:
                recovered = LocalUpdateInstaller(store=store).recover(
                    current_app=requested_app,
                    activation_lock=lock,
                    transaction_id=transaction,
                )
            except Exception:
                recovered = False
            _launch_existing_app(
                requested_app,
                recovery_pending=not recovered,
                candidate_sha=str(args.candidate_sha or ""),
            )
            return 0 if recovered else 1

        pending = Path(args.pending_app)
        if (
            state.activation_transaction_id != transaction
            or state.candidate_sha != args.candidate_sha
            or Path(state.pending_app) != pending
            or candidate_is_blocked(state)
            or not activation_evidence_valid(state)
        ):
            _record_activation_deferred(
                store,
                transaction=transaction,
                code="CMP-VERIFY-FAILED",
                message=(
                    "The staged updater transaction changed before activation; "
                    "the current version was kept."
                ),
            )
            _launch_existing_app(requested_app)
            return 1
        try:
            ok = LocalUpdateInstaller(store=store).install(
                args.candidate_sha,
                pending,
                requested_app,
                activation_lock=lock,
                transaction_id=transaction,
            )
        except Exception:
            current_state = store.load()
            if current_state.activation_journal is None:
                UpdateCoordinator(store=store).block(
                    args.candidate_sha,
                    "The updater helper could not validate the staged bundle.",
                )
                _launch_existing_app(requested_app)
            else:
                _launch_existing_app(
                    requested_app,
                    recovery_pending=True,
                    candidate_sha=str(args.candidate_sha or ""),
                )
            return 1
        return 0 if ok else 1
    finally:
        if lease is not None:
            try:
                lease.release()
            finally:
                try:
                    lock.release()
                except ActivationLockError:
                    pass
        else:
            try:
                lock.release()
            except ActivationLockError:
                pass


def _record_activation_deferred(
    store: UpdateStateStore,
    *,
    code: str,
    message: str,
    transaction: str = "",
) -> None:
    """Record a privacy-safe refusal without discarding a verified candidate."""

    try:
        state = store.load()
        if transaction and state.activation_transaction_id not in {"", transaction}:
            return
        if state.activation_journal is None:
            state.activation_transaction_id = ""
        state.component_error_code = code
        state.last_error = message
        store.save(state)
    except Exception:
        pass


def _activation_must_defer() -> bool:
    """Fail closed across both process-level and persisted operation leases."""

    try:
        if activity_registry.is_active():
            return True
        return inspect_persisted_operations(app_data_dir()).should_defer
    except Exception:
        return True


def _persisted_activation_must_defer() -> bool:
    """Inspect persisted jobs after the helper owns the global activity lease."""

    try:
        return inspect_persisted_operations(app_data_dir()).should_defer
    except Exception:
        return True


def _launch_existing_app(
    app_path: Path,
    *,
    recovery_pending: bool = False,
    candidate_sha: str = "",
) -> None:
    executable = Path(app_path) / "Contents" / "MacOS" / "GamGUI"
    if not executable.is_file():
        return
    env = os.environ.copy()
    for key in (
        "GAMGUI_UPDATE_HEALTH_MARKER",
        "GAMGUI_INSTALLED_SHA",
        "GAMGUI_ACTIVATION_TRANSACTION_ID",
        ACTIVATION_PROBE_ENV,
        "GAMGUI_UPDATE_CURRENT_APP",
        ACTIVATION_RECOVERY_ENV,
    ):
        env.pop(key, None)
    env["GAMGUI_SKIP_UPDATE_ONCE"] = "1"
    if recovery_pending:
        env[ACTIVATION_RECOVERY_ENV] = "1"
        env[ACTIVATION_PROBE_ENV] = "1"
        if re.fullmatch(r"[0-9a-f]{40}", candidate_sha.lower()):
            env["GAMGUI_INSTALLED_SHA"] = candidate_sha.lower()
    try:
        subprocess.Popen([str(executable)], env=env, close_fds=True)
    except OSError:
        pass


def _run_self_test(json_output: bool = False) -> int:
    if os.environ.get(APP_DATA_ENV):
        prepare_database_schemas(app_data_dir())
    result = bundle_self_test()
    if json_output or not result["ok"]:
        print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


def _run_canary(json_output: bool = False) -> int:
    from .core.canary import run_live_canary

    result = asyncio.run(run_live_canary())
    if json_output:
        print(json.dumps(result, sort_keys=True))
    else:
        print("Read-only update canary passed." if result["ok"] else "Read-only update canary failed.")
    return 0 if result["ok"] else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _arguments(argv)
    if args.headless_task:
        if args.headless_task != "classroom-teachers" or not args.policy_id:
            return 2
        from .agent import run_classroom_teachers

        return asyncio.run(
            run_classroom_teachers(args.policy_id, scheduled=args.scheduled)
        )
    if args.apply_update_helper:
        return _run_helper(args)
    if args.self_test:
        return _run_self_test(args.json_output)
    if args.canary:
        return _run_canary(args.json_output)
    if _handoff_pending_update():
        return 0

    activation_probe_start = bool(
        os.environ.get("GAMGUI_UPDATE_HEALTH_MARKER")
        or os.environ.get(ACTIVATION_RECOVERY_ENV) == "1"
    )
    state = (
        AppState.create_activation_probe()
        if activation_probe_start
        else AppState.create()
    )
    manager = getattr(state, "component_manager", None)
    if manager is None:
        manager = ComponentManager(registry=activity_registry)
        try:
            setattr(state, "component_manager", manager)
        except (AttributeError, TypeError):
            pass
    if not activation_probe_start:
        try:
            manager.cleanup_expired_snapshots()
        except (OSError, ValueError, RuntimeError):
            # Retention cleanup is best-effort at startup. It never blocks Core and
            # does not load optional component code or Workspace credentials.
            pass
    app = create_app(state)
    host, port = "127.0.0.1", _free_loopback_port()
    server = _BackgroundServer(app, host, port)
    server.start()
    _start_update_preparation(state)
    url = f"http://{host}:{port}/?token={state.token}"

    try:
        import webview  # pywebview (optional 'desktop' extra)
    except ImportError:
        print(f"[GamGUI] pywebview not installed — open this URL in a browser:\n  {url}")
        print("[GamGUI] (install the native window with: pip install '.[desktop]')  Ctrl-C to quit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
        return 0

    # WKWebView drops Content-Disposition downloads unless this is on — without it the CSV export
    # links (Audit viewer, Builder results) silently do nothing in the native window.
    webview.settings["ALLOW_DOWNLOADS"] = True

    window = webview.create_window(
        "GamGUI",
        url,
        width=1100,
        height=760,
        min_size=(900, 600),
        hidden=activation_probe_start,
        focus=not activation_probe_start,
    )

    def _notify_close_refusal(message: str) -> None:
        escaped = json.dumps(str(message))
        window.evaluate_js(f"window.alert({escaped})")

    def _guard_close() -> bool:
        return _allow_window_close(state, _notify_close_refusal)

    window.events.closing += _guard_close
    activation_health_written = False

    def _mark_activation_loaded(*_args: object) -> None:
        nonlocal activation_health_written
        if activation_health_written:
            return
        payload = state.update_activation_health_payload()
        if payload is None:
            return
        write_health_marker_from_environment(payload)
        activation_health_written = True

    window.events.loaded += _mark_activation_loaded

    def _fit_to_screen() -> None:
        if activation_probe_start:
            return
        # Once the GUI loop knows the display, grow the window to fit it (so the full-width screens
        # have room) and center it. Best-effort — falls back silently to the default 1100×760.
        try:
            screens = list(getattr(webview, "screens", None) or [])
            if screens:
                sw, sh = int(screens[0].width), int(screens[0].height)
                w, h = _fit_size(sw, sh)
                window.resize(w, h)
                window.move(max(0, (sw - w) // 2), max(20, (sh - h) // 3))
        except Exception:
            pass

    try:
        webview.start(_fit_to_screen)
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
