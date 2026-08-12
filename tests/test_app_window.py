"""Window-sizing math: use the display but always fit it (incl. 13\" Macs)."""

import os
import re
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from gamgui.app import (
    _BackgroundServer,
    _active_admin_jobs,
    _arguments,
    _confirm_automatic_update,
    _fit_size,
    _handoff_pending_update,
    _run_helper,
    main,
)
from gamgui.core.activation_lock import OwnerOnlyActivationLock
from gamgui.core.activity import ActivityRegistry
from gamgui.core.components import ComponentArtifactId
from gamgui.core.updater import (
    ACTIVATION_APP_UPDATE,
    ACTIVATION_PHASE_RECOVERY_REQUIRED,
    ACTIVATION_PROBE_ENV,
    ACTIVATION_RECOVERY_ENV,
    ActivationJournal,
    UpdateState,
    UpdateStateStore,
)
from gamgui.web.jobs import BatchJob

# (screen_w, screen_h) for displays GamGUI runs on.
SCREENS = [
    (1440, 900),    # 13" MacBook Air (default scaled)
    (1280, 800),    # 13" "more space" off / older default
    (1512, 982),    # 14" MacBook Pro (default scaled)
    (1366, 768),    # small external / older laptop
    (1680, 1050),   # 15"/16" scaled
    (3840, 2160),   # large 4K external
    (2560, 1440),   # 27" external
]


def test_background_server_configures_without_console_streams(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    background = _BackgroundServer(FastAPI(), "127.0.0.1", 0)

    assert background.server.config.log_config is None
    assert not background.server.config.access_log


@pytest.mark.parametrize("sw,sh", SCREENS)
def test_window_always_fits_the_screen(sw, sh):
    w, h = _fit_size(sw, sh)
    assert w <= sw and h <= sh                 # never larger than the display (fits 13" Macs)
    assert w >= 900 and h >= 600               # never below the usable minimum
    assert w <= 1600 and h <= 1000             # capped so huge externals don't open absurd


def test_uses_most_of_a_13in_display():
    # On a 13" MBA it should be noticeably bigger than the old fixed 1100×760, not a token bump.
    w, h = _fit_size(1440, 900)
    assert w >= 1300 and h >= 800


def test_caps_on_a_huge_external():
    assert _fit_size(3840, 2160) == (1600, 1000)


def test_cli_modes_are_parsed_without_rejecting_macos_arguments():
    args = _arguments(["--self-test", "--json", "-psn_0_12345"])
    assert args.self_test and args.json_output and not args.canary


def test_automatic_update_prompt_explains_restart_and_honors_choice(monkeypatch):
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    calls = []

    def run(arguments, **options):
        calls.append((arguments, options))
        return SimpleNamespace(returncode=0, stdout="Install and restart\n")

    monkeypatch.setattr("gamgui.app.subprocess.run", run)
    state = SimpleNamespace(
        activation_kind=ACTIVATION_APP_UPDATE,
        candidate_artifact=SimpleNamespace(version="2.4.0"),
    )

    assert _confirm_automatic_update(state)
    arguments, options = calls[0]
    assert arguments[:2] == ["/usr/bin/osascript", "-e"]
    assert arguments[-1] == "2.4.0"
    assert "reopen automatically" in arguments[2]
    assert options["timeout"] == 300


def test_automatic_update_prompt_defers_when_user_declines(monkeypatch):
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr(
        "gamgui.app.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    assert not _confirm_automatic_update(
        SimpleNamespace(
            activation_kind=ACTIVATION_APP_UPDATE,
            candidate_artifact=None,
        )
    )


def test_health_start_preserves_profile_projection_guard(monkeypatch, tmp_path):
    marker = tmp_path / "health" / "transaction.json"
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr(
        "gamgui.app.installed_app_path",
        lambda: tmp_path / "GamGUI.app",
    )
    monkeypatch.setenv("GAMGUI_SKIP_UPDATE_ONCE", "1")
    monkeypatch.setenv("GAMGUI_UPDATE_HEALTH_MARKER", str(marker))

    assert not _handoff_pending_update()
    assert os.environ["GAMGUI_SKIP_UPDATE_ONCE"] == "1"

    monkeypatch.delenv("GAMGUI_UPDATE_HEALTH_MARKER")
    assert not _handoff_pending_update()
    assert "GAMGUI_SKIP_UPDATE_ONCE" not in os.environ


def test_activation_health_is_written_only_after_window_page_loaded(
    monkeypatch,
    tmp_path,
):
    marker = tmp_path / "health.json"
    monkeypatch.setenv("GAMGUI_UPDATE_HEALTH_MARKER", str(marker))
    payload = {
        "ok": True,
        "transaction_id": "1" * 32,
        "sha": "a" * 40,
        "profile": "core",
        "component_set_digest": "b" * 64,
    }
    state = SimpleNamespace(
        token="probe",
        component_manager=SimpleNamespace(),
        update_activation_health_payload=lambda: payload,
    )
    writes = []

    class Event:
        handler = None

        def __iadd__(self, handler):
            self.handler = handler
            return self

    class Window:
        events = SimpleNamespace(closing=Event(), loaded=Event())

        def evaluate_js(self, _script):
            return None

        def resize(self, *_args):
            return None

        def move(self, *_args):
            return None

    class Server:
        def __init__(self, *_args):
            pass

        def start(self):
            return None

        def stop(self):
            return None

    window = Window()

    def start(callback):
        callback()
        assert writes == []
        assert window.events.loaded.handler is not None
        window.events.loaded.handler()
        window.events.loaded.handler()

    created = []

    def create_window(*_args, **kwargs):
        created.append(kwargs)
        return window

    webview = SimpleNamespace(
        settings={},
        screens=[],
        create_window=create_window,
        start=start,
    )
    monkeypatch.setitem(sys.modules, "webview", webview)
    monkeypatch.setattr("gamgui.app._handoff_pending_update", lambda: False)
    monkeypatch.setattr(
        "gamgui.app.AppState.create_activation_probe",
        lambda: state,
    )
    monkeypatch.setattr("gamgui.app.create_app", lambda _state: object())
    monkeypatch.setattr("gamgui.app._BackgroundServer", Server)
    monkeypatch.setattr("gamgui.app._start_update_preparation", lambda _state: None)
    monkeypatch.setattr(
        "gamgui.app.write_health_marker_from_environment",
        lambda value: writes.append(value),
    )

    assert main([]) == 0
    assert writes == [payload]
    assert created == [
        {
            "width": 1100,
            "height": 760,
            "min_size": (900, 600),
            "hidden": True,
            "focus": False,
        }
    ]


def test_active_job_detection_understands_terminal_and_running_states():
    class State:
        jobs = {"done": type("Job", (), {"status": "completed"})()}

    assert not _active_admin_jobs(State())
    State.jobs["running"] = type("Job", (), {"status": "running"})()
    assert _active_admin_jobs(State())


def test_active_job_detection_uses_real_finished_jobs_and_manifest_tasks():
    class State:
        jobs = {"done": BatchJob("done", 1, finished=True)}
        classroom_manifest_tasks = {}
        classroom_manifests = None
        drive_service = None

    assert not _active_admin_jobs(State())
    State.jobs["running"] = BatchJob("running", 1, finished=False)
    assert _active_admin_jobs(State())
    State.jobs.pop("running")

    class PendingTask:
        def done(self):
            return False

    State.classroom_manifest_tasks = {"manifest": PendingTask()}
    assert _active_admin_jobs(State())


def test_pending_update_hands_off_exact_staged_state(monkeypatch, tmp_path):
    data_root = tmp_path / "data"
    pending = data_root / "updates" / "pending" / ("a" * 40) / "GamGUI.app"
    pending_executable = pending / "Contents" / "MacOS" / "GamGUI"
    pending_executable.parent.mkdir(parents=True)
    pending_executable.write_text("candidate", encoding="utf-8")
    current = tmp_path / "Applications" / "GamGUI.app"
    current.mkdir(parents=True)
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(data_root))
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr("gamgui.app.installed_app_path", lambda: current)
    monkeypatch.setattr("gamgui.app._activation_must_defer", lambda: False)
    monkeypatch.setattr("gamgui.app._confirm_automatic_update", lambda _state: True)
    UpdateStateStore().save(
        UpdateState(
            candidate_sha="a" * 40,
            pending_app=str(pending),
            canary_result="",
            required_check_evidence=["update-ready"],
            activation_kind=ACTIVATION_APP_UPDATE,
            candidate_artifact=ComponentArtifactId(
                source_sha="a" * 40,
                version="0.0.1",
                profile="core",
                component_set_digest="b" * 64,
                architecture="arm64",
                minimum_macos_version="13.0",
                packaging_revision="1",
                artifact_sha256="c" * 64,
            ),
        )
    )
    launched = []
    monkeypatch.setattr("gamgui.app.subprocess.Popen", lambda argv, **kwargs: launched.append((argv, kwargs)))

    assert _handoff_pending_update()
    assert launched and "--apply-update-helper" in launched[0][0]
    assert str(pending) in launched[0][0]
    arguments, options = launched[0]
    transaction = arguments[arguments.index("--activation-transaction") + 1]
    descriptor = int(arguments[arguments.index("--activation-lock-fd") + 1])
    assert re.fullmatch(r"[0-9a-f]{32}", transaction)
    assert options["close_fds"] is True
    assert options["pass_fds"] == (descriptor,)
    assert UpdateStateStore().load().activation_transaction_id == transaction


@pytest.mark.skipif(sys.platform != "win32", reason="Windows helper handoff")
def test_windows_pending_update_hands_off_to_signed_detached_helper(
    monkeypatch,
    tmp_path,
):
    data_root = tmp_path / "data"
    sha = "a" * 40
    current = tmp_path / "Programs" / "GamGUI" / "current"
    current.mkdir(parents=True)
    (current / "GamGUI.exe").write_bytes(b"current")
    pending = data_root / "updates" / "pending" / sha / "core" / "GamGUI"
    pending.mkdir(parents=True)
    (pending / "GamGUI.exe").write_bytes(b"candidate")
    helper = data_root / "updater" / "GamGUIUpdater.exe"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"helper")
    staged_helper = pending.parent / "GamGUIUpdater.exe"
    staged_helper.write_bytes(b"staged-helper")
    artifact = ComponentArtifactId(
        source_sha=sha,
        version="0.0.1",
        profile="core",
        component_set_digest="b" * 64,
        architecture="x86_64",
        minimum_macos_version="10.0",
        packaging_revision="2-windows-local",
        platform="windows",
        bundle_format="onedir",
        signer_thumbprint="c" * 64,
        toolchain_manifest_digest="d" * 64,
        artifact_sha256="e" * 64,
    )
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(data_root))
    monkeypatch.setattr("gamgui.app.sys.platform", "win32")
    monkeypatch.setattr("gamgui.app.installed_app_path", lambda: current)
    monkeypatch.setattr("gamgui.app._activation_must_defer", lambda: False)
    monkeypatch.setattr("gamgui.app._confirm_automatic_update", lambda _state: True)
    verified = []
    monkeypatch.setattr(
        "gamgui.core.windows_update.verify_windows_file",
        lambda path, thumbprint: verified.append((path, thumbprint)),
    )

    class Mutex:
        handle = 1234
        name = "Local\\GamGUIUpdater-" + ("1" * 24)

        def close(self):
            self.handle = 0

    monkeypatch.setattr(
        "gamgui.app.WindowsNamedMutex.acquire",
        lambda _name: Mutex(),
    )
    UpdateStateStore().save(
        UpdateState(
            candidate_sha=sha,
            pending_app=str(pending),
            canary_result="passed",
            required_check_evidence=["update-ready"],
            activation_kind=ACTIVATION_APP_UPDATE,
            candidate_artifact=artifact,
            local_signer_thumbprint="c" * 64,
            windows_installation_root=str(current.parent),
        )
    )
    launched = []
    monkeypatch.setattr(
        "gamgui.app.subprocess.Popen",
        lambda argv, **kwargs: launched.append((argv, kwargs)),
    )

    assert _handoff_pending_update()
    arguments, options = launched[0]
    assert arguments[0] == str(staged_helper)
    assert arguments[arguments.index("--promote-helper") + 1] == str(helper)
    assert "--activation-mutex-handle" in arguments
    assert options["close_fds"] is True
    assert options["startupinfo"].lpAttributeList == {"handle_list": [1234]}
    assert verified == [(helper, "c" * 64), (staged_helper, "c" * 64)]


def test_corrupt_existing_update_state_enters_local_recovery_probe(
    monkeypatch,
    tmp_path,
):
    data_root = tmp_path / "data"
    state_path = data_root / "updates" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{", encoding="utf-8")
    current = tmp_path / "Applications" / "GamGUI.app"
    current.mkdir(parents=True)
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(data_root))
    # setenv records an undo even when the variables were initially absent;
    # delenv(..., raising=False) would not, so the app-written probe flags
    # could leak into later tests in the same process.
    monkeypatch.setenv(ACTIVATION_RECOVERY_ENV, "")
    monkeypatch.setenv(ACTIVATION_PROBE_ENV, "")
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr("gamgui.app.installed_app_path", lambda: current)

    assert not _handoff_pending_update()
    assert os.environ[ACTIVATION_RECOVERY_ENV] == "1"
    assert os.environ[ACTIVATION_PROBE_ENV] == "1"
    assert UpdateStateStore().load().activation_journal_invalid


def test_pending_update_defers_for_persisted_running_admin_work(
    monkeypatch,
    tmp_path,
):
    data_root = tmp_path / "data"
    sha = "a" * 40
    pending = data_root / "updates" / "pending" / sha / "GamGUI.app"
    executable = pending / "Contents" / "MacOS" / "GamGUI"
    executable.parent.mkdir(parents=True)
    executable.write_text("candidate", encoding="utf-8")
    current = tmp_path / "Applications" / "GamGUI.app"
    current.mkdir(parents=True)
    operation_db = data_root / "classroom_roster_operations.db"
    with sqlite3.connect(operation_db) as connection:
        connection.executescript(
            """
            CREATE TABLE roster_manifests (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            INSERT INTO roster_manifests VALUES ('private-id', 'running', '', 1);
            """
        )
    operation_db.chmod(0o600)
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(data_root))
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr("gamgui.app.installed_app_path", lambda: current)
    UpdateStateStore().save(
        UpdateState(
            candidate_sha=sha,
            pending_app=str(pending),
            canary_result="passed",
            required_check_evidence=["update-ready"],
            activation_kind=ACTIVATION_APP_UPDATE,
            candidate_artifact=ComponentArtifactId(
                source_sha=sha,
                version="0.0.1",
                profile="core",
                component_set_digest="b" * 64,
                architecture="arm64",
                minimum_macos_version="13.0",
                packaging_revision="1",
                artifact_sha256="c" * 64,
            ),
        )
    )
    launched = []
    monkeypatch.setattr(
        "gamgui.app.subprocess.Popen",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )

    assert not _handoff_pending_update()
    state = UpdateStateStore().load()
    assert state.candidate_sha == sha
    assert not state.activation_transaction_id
    assert state.component_error_code == "CMP-ACTIVE-JOB"
    assert launched == []


@pytest.mark.skipif(os.name != "posix", reason="descriptor handoff requires POSIX")
def test_helper_adopts_lock_and_revalidates_exact_transaction(
    monkeypatch,
    tmp_path,
):
    data_root = tmp_path / "data"
    current = tmp_path / "Applications" / "GamGUI.app"
    current.mkdir(parents=True)
    pending = data_root / "updates" / "pending" / ("a" * 40) / "GamGUI.app"
    pending.mkdir(parents=True)
    transaction = "1" * 32
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(data_root))
    monkeypatch.setattr("gamgui.app.installed_app_path", lambda: current)
    monkeypatch.setattr("gamgui.app.wait_for_process_exit", lambda _pid: True)
    monkeypatch.setattr(
        "gamgui.app._persisted_activation_must_defer",
        lambda: False,
    )
    registry = ActivityRegistry()
    monkeypatch.setattr("gamgui.app.activity_registry", registry)
    store = UpdateStateStore()
    store.save(
        UpdateState(
            candidate_sha="a" * 40,
            pending_app=str(pending),
            canary_result="passed",
            required_check_evidence=["update-ready"],
            activation_kind=ACTIVATION_APP_UPDATE,
            activation_transaction_id=transaction,
            candidate_artifact=ComponentArtifactId(
                source_sha="a" * 40,
                version="0.0.1",
                profile="core",
                component_set_digest="b" * 64,
                architecture="arm64",
                minimum_macos_version="13.0",
                packaging_revision="1",
                artifact_sha256="c" * 64,
            ),
        )
    )
    observed = []

    class Installer:
        def __init__(self, *, store):
            self.store = store

        def install(self, sha, pending_app, current_app, **options):
            observed.append((sha, pending_app, current_app, options))
            assert options["activation_lock"].held
            assert registry.is_active()
            assert registry.try_acquire("concurrent-admin") is None
            return True

    monkeypatch.setattr("gamgui.app.LocalUpdateInstaller", Installer)
    parent_lock = OwnerOnlyActivationLock.try_acquire(
        data_root / "updates" / "activation.lock"
    )
    assert parent_lock is not None
    inherited_fd = os.dup(parent_lock.fileno)
    parent_lock.close_after_handoff()
    args = SimpleNamespace(
        activation_lock_fd=inherited_fd,
        activation_transaction=transaction,
        current_app=str(current),
        pending_app=str(pending),
        candidate_sha="a" * 40,
        parent_pid=123,
        recover_update_helper=False,
    )

    assert _run_helper(args) == 0
    assert observed and observed[0][3]["transaction_id"] == transaction
    assert not registry.is_active()
    next_lock = OwnerOnlyActivationLock.try_acquire(
        data_root / "updates" / "activation.lock"
    )
    assert next_lock is not None
    next_lock.release()


@pytest.mark.skipif(os.name != "posix", reason="descriptor handoff requires POSIX")
def test_journaled_activation_hands_off_to_recovery_helper(monkeypatch, tmp_path):
    data_root = tmp_path / "data"
    updates = data_root / "updates"
    current = tmp_path / "Applications" / "GamGUI.app"
    (current / "Contents" / "MacOS").mkdir(parents=True)
    (current / "Contents" / "MacOS" / "GamGUI").write_text(
        "candidate",
        encoding="utf-8",
    )
    pending = updates / "pending" / ("a" * 40) / "GamGUI.app"
    pending.mkdir(parents=True)
    transaction = "3" * 32
    backup = updates / "backups" / transaction
    journal = ActivationJournal(
        transaction_id=transaction,
        candidate_sha="a" * 40,
        phase=ACTIVATION_PHASE_RECOVERY_REQUIRED,
        current_app=str(current.resolve()),
        pending_app=str(pending.resolve()),
        incoming_app=str(
            (current.parent / f".{current.name}.{transaction}.incoming").resolve()
        ),
        previous_app=str(
            (current.parent / f".{current.name}.{transaction}.previous").resolve()
        ),
        backup=str(backup.resolve()),
        backup_app=str((backup / "GamGUI.app").resolve()),
        backup_sidecar=str((backup / "installed-artifact.json").resolve()),
        candidate_sidecar=str((backup / "candidate-artifact.json").resolve()),
        database_snapshot=str((backup / "database").resolve()),
        health_marker=str((updates / "health" / f"{transaction}.json").resolve()),
    )
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(data_root))
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr("gamgui.app.installed_app_path", lambda: current)
    UpdateStateStore().save(
        UpdateState(
            candidate_sha="a" * 40,
            pending_app=str(pending),
            activation_transaction_id=transaction,
            activation_journal=journal,
        )
    )
    launched = []
    monkeypatch.setattr(
        "gamgui.app.subprocess.Popen",
        lambda argv, **kwargs: launched.append((argv, kwargs)),
    )

    assert _handoff_pending_update()
    arguments, options = launched[0]
    assert "--recover-update-helper" in arguments
    assert arguments[arguments.index("--activation-transaction") + 1] == transaction
    assert options["close_fds"] is True
    assert options["pass_fds"]


def test_incomplete_pending_update_is_blocked_without_handoff(monkeypatch, tmp_path):
    data_root = tmp_path / "data"
    sha = "a" * 40
    pending = data_root / "updates" / "pending" / sha / "GamGUI.app"
    pending.mkdir(parents=True)
    current = tmp_path / "Applications" / "GamGUI.app"
    current.mkdir(parents=True)
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(data_root))
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr("gamgui.app.installed_app_path", lambda: current)
    UpdateStateStore().save(UpdateState(candidate_sha=sha, pending_app=str(pending)))
    launched = []
    monkeypatch.setattr(
        "gamgui.app.subprocess.Popen",
        lambda argv, **kwargs: launched.append((argv, kwargs)),
    )

    assert not _handoff_pending_update()
    state = UpdateStateStore().load()
    assert sha in state.blocked_shas
    assert not state.pending_app and not state.candidate_sha
    assert launched == []


def test_pending_update_without_activation_evidence_is_blocked(monkeypatch, tmp_path):
    data_root = tmp_path / "data"
    sha = "a" * 40
    pending = data_root / "updates" / "pending" / sha / "GamGUI.app"
    executable = pending / "Contents" / "MacOS" / "GamGUI"
    executable.parent.mkdir(parents=True)
    executable.write_text("candidate", encoding="utf-8")
    current = tmp_path / "Applications" / "GamGUI.app"
    current.mkdir(parents=True)
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(data_root))
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr("gamgui.app.installed_app_path", lambda: current)
    UpdateStateStore().save(
        UpdateState(
            candidate_sha=sha,
            pending_app=str(pending),
            canary_result="passed",
        )
    )
    launched = []
    monkeypatch.setattr(
        "gamgui.app.subprocess.Popen",
        lambda argv, **kwargs: launched.append((argv, kwargs)),
    )

    assert not _handoff_pending_update()
    state = UpdateStateStore().load()
    assert sha in state.blocked_shas
    assert "required update evidence" in state.last_error
    assert launched == []
