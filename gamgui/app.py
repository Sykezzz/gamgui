
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
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

import uvicorn

from .core.paths import APP_DATA_ENV, app_data_dir
from .core.secrets.ephemeral import sweep_stale_configs, wipe_live_configs
from .core.updater import (
    LocalUpdateInstaller,
    UpdateCoordinator,
    UpdateStateStore,
    activation_evidence_valid,
    bundle_self_test,
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
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--candidate-sha", default="")
    parser.add_argument("--pending-app", default="")
    parser.add_argument("--current-app", default="")
    # macOS may append process-serial arguments; they are intentionally ignored.
    args, _unknown = parser.parse_known_args(argv)
    return args


def _active_admin_jobs(state: AppState) -> bool:
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
    drive_service = getattr(state, "drive_service", None)
    stores = (
        getattr(state, "classroom_manifests", None),
        getattr(drive_service, "operations", None),
    )
    for store in stores:
        checker = getattr(store, "has_active_jobs", None)
        if callable(checker) and checker():
            return True
    return False


def _start_update_preparation(state: AppState) -> None:
    if sys.platform != "darwin" or installed_app_path() is None:
        return
    coordinator = UpdateCoordinator(active_jobs=lambda: _active_admin_jobs(state))
    threading.Thread(
        target=coordinator.check_and_prepare,
        name="gamgui-update-check",
        daemon=True,
    ).start()


def _handoff_pending_update() -> bool:
    if (
        sys.platform != "darwin"
        or os.environ.pop("GAMGUI_SKIP_UPDATE_ONCE", "") == "1"
        or installed_app_path() is None
    ):
        return False
    state = UpdateStateStore().load()
    pending = Path(state.pending_app) if state.pending_app else None
    current = installed_app_path()
    executable = pending / "Contents" / "MacOS" / "GamGUI" if pending else None
    if (
        pending is None
        or current is None
        or not state.candidate_sha
        or state.candidate_sha in state.blocked_shas
        or not pending.is_dir()
    ):
        return False
    if executable is None or not executable.is_file():
        UpdateCoordinator().block(
            state.candidate_sha,
            "The staged update bundle was incomplete; the current version was kept.",
        )
        return False
    if not activation_evidence_valid(state):
        UpdateCoordinator().block(
            state.candidate_sha,
            "The staged update lacked required CI or canary evidence; the current version was kept.",
        )
        return False
    subprocess.Popen(
        [
            sys.executable,
            "--apply-update-helper",
            "--parent-pid",
            str(os.getpid()),
            "--candidate-sha",
            state.candidate_sha,
            "--pending-app",
            str(pending),
            "--current-app",
            str(current),
        ],
        close_fds=True,
    )
    return True


def _run_helper(args: argparse.Namespace) -> int:
    actual_app = installed_app_path()
    requested_app = Path(args.current_app)
    if actual_app is None or actual_app.resolve() != requested_app.resolve():
        UpdateCoordinator().block(args.candidate_sha, "The updater helper bundle did not match the installed app.")
        if actual_app is not None and wait_for_process_exit(args.parent_pid):
            _launch_existing_app(actual_app)
        return 1
    if not wait_for_process_exit(args.parent_pid):
        UpdateCoordinator().block(args.candidate_sha, "The updater launcher did not exit in time.")
        return 1
    try:
        ok = LocalUpdateInstaller().install(
            args.candidate_sha,
            Path(args.pending_app),
            requested_app,
        )
    except Exception:
        UpdateCoordinator().block(
            args.candidate_sha,
            "The updater helper could not validate the staged bundle.",
        )
        _launch_existing_app(requested_app)
        return 1
    return 0 if ok else 1


def _launch_existing_app(app_path: Path) -> None:
    executable = Path(app_path) / "Contents" / "MacOS" / "GamGUI"
    if not executable.is_file():
        return
    env = os.environ.copy()
    env["GAMGUI_SKIP_UPDATE_ONCE"] = "1"
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
    if args.apply_update_helper:
        return _run_helper(args)
    if args.self_test:
        return _run_self_test(args.json_output)
    if args.canary:
        return _run_canary(args.json_output)
    if _handoff_pending_update():
        return 0

    state = AppState.create()
    app = create_app(state)
    host, port = "127.0.0.1", _free_loopback_port()
    server = _BackgroundServer(app, host, port)
    server.start()
    write_health_marker_from_environment()
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

    window = webview.create_window("GamGUI", url, width=1100, height=760, min_size=(900, 600))

    def _fit_to_screen() -> None:
        # Once the GUI loop knows the display, grow the window to fit it (so the full-width screens
        # have room) and center it. Best-effort — falls back silently to the default 1100×760.
        try:
            screens = list(getattr(webview, "screens", None) or [])
            if not screens:
                return
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
