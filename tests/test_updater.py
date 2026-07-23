from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

import pytest

from gamgui.core.updater import (
    GitHubUpdateSource,
    LocalUpdateBuilder,
    LocalUpdateInstaller,
    UpdateCandidate,
    UpdateCoordinator,
    UpdateState,
    UpdateStateStore,
    bundle_self_test,
    installed_app_path,
    prepare_database_schemas,
    restore_databases,
    snapshot_databases,
    write_health_marker_from_environment,
)
from gamgui.core.canary import CANARY_CHECK_NAMES, CanaryConfigStore

SHA = "a" * 40


def _ready_state(pending: Path) -> UpdateState:
    return UpdateState(
        candidate_sha=SHA,
        pending_app=str(pending),
        canary_result="passed",
        required_check_evidence=["update-ready"],
    )


class _Response:
    def __init__(self, value):
        self._raw = io.BytesIO(json.dumps(value).encode())

    def read(self):
        return self._raw.read()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_update_state_round_trip(tmp_path):
    store = UpdateStateStore(tmp_path / "updates" / "state.json")
    state = UpdateState(installed_sha="c" * 40, candidate_sha=SHA, blocked_shas=["b" * 40])
    store.save(state)
    assert store.load() == state
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600


def test_corrupt_state_fails_closed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{", encoding="utf-8")
    assert UpdateStateStore(path).load() == UpdateState()


def test_type_corrupt_state_is_sanitized(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "installed_sha": ["not", "text"],
                "candidate_sha": "not-a-sha",
                "pending_app": {"outside": "path"},
                "blocked_shas": "a" * 40,
                "last_checked_at": "never",
                "canary_result": "maybe",
                "required_check_evidence": {"name": "update-ready"},
                "retained_rollbacks": [1, "one", "two", "three"],
            }
        ),
        encoding="utf-8",
    )

    state = UpdateStateStore(path).load()

    assert state.installed_sha == ""
    assert state.candidate_sha == ""
    assert state.pending_app == ""
    assert state.blocked_shas == []
    assert state.last_checked_at == 0.0
    assert state.canary_result == ""
    assert state.required_check_evidence == []
    assert state.retained_rollbacks == ["one", "two"]


def test_discover_requires_ready_check_on_exact_sha():
    calls = []

    def open_url(request, timeout):
        calls.append((request.full_url, timeout))
        if "/branches/" in request.full_url:
            return _Response({"commit": {"sha": SHA}})
        return _Response(
            {
                "check_runs": [
                    {"name": "test", "status": "completed", "conclusion": "success", "head_sha": SHA},
                    {"name": "update-ready", "status": "completed", "conclusion": "success", "head_sha": SHA},
                ]
            }
        )

    candidate = GitHubUpdateSource(opener=open_url).discover()
    assert candidate == UpdateCandidate(SHA, f"https://github.com/Sykezzz/gamgui/commit/{SHA}", ("test", "update-ready"))
    assert len(calls) == 2


@pytest.mark.parametrize(
    "run",
    [
        {"name": "update-ready", "status": "queued", "conclusion": None, "head_sha": SHA},
        {"name": "update-ready", "status": "completed", "conclusion": "failure", "head_sha": SHA},
        {"name": "update-ready", "status": "completed", "conclusion": "success", "head_sha": "b" * 40},
    ],
)
def test_discover_rejects_unready_commit(run):
    responses = iter([{"commit": {"sha": SHA}}, {"check_runs": [run]}])

    def open_url(_request, timeout):
        assert timeout == 15
        return _Response(next(responses))

    assert GitHubUpdateSource(opener=open_url).discover() is None


def test_discover_skips_installed_and_blocked_without_check_query():
    calls = []

    def open_url(_request, timeout):
        assert timeout == 15
        calls.append(1)
        return _Response({"commit": {"sha": SHA}})

    source = GitHubUpdateSource(opener=open_url)
    assert source.discover(installed_sha=SHA) is None
    assert source.discover(blocked_shas=[SHA]) is None
    assert len(calls) == 2


def test_coordinator_prepares_and_records_canary(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")

    class Source:
        def discover(self, *_args):
            return UpdateCandidate(SHA, "url", ("update-ready",))

    class Builder:
        def prepare(self, candidate):
            assert candidate.sha == SHA
            return tmp_path / "GamGUI.app"

        def run_canary(self, pending):
            assert pending.name == "GamGUI.app"

    pending = UpdateCoordinator(store, Source(), Builder()).check_and_prepare()
    state = store.load()
    assert pending == tmp_path / "GamGUI.app"
    assert state.candidate_sha == SHA and state.canary_result == "passed" and not state.last_error


def test_coordinator_fails_closed_when_job_active(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")
    coordinator = UpdateCoordinator(store=store, active_jobs=lambda: True)
    assert coordinator.check_and_prepare() is None
    assert "operation is active" in store.load().last_error


@pytest.mark.parametrize(
    ("active_states", "canary_called"),
    [
        ([False, False, True], False),
        ([False, False, False, True], True),
    ],
)
def test_coordinator_rechecks_active_jobs_before_canary_and_publish(
    tmp_path,
    active_states,
    canary_called,
):
    store = UpdateStateStore(tmp_path / "state.json")
    states = iter(active_states)
    calls = []

    class Source:
        def discover(self, *_args):
            return UpdateCandidate(SHA, "url", ("update-ready",))

    class Builder:
        def prepare(self, _candidate):
            return tmp_path / "GamGUI.app"

        def run_canary(self, _pending):
            calls.append("canary")

    coordinator = UpdateCoordinator(
        store,
        Source(),
        Builder(),
        active_jobs=lambda: next(states),
    )
    assert coordinator.check_and_prepare() is None
    state = store.load()
    assert bool(calls) is canary_called
    assert not state.candidate_sha and not state.pending_app
    assert "became active" in state.last_error


def test_preparation_failure_is_retryable_and_does_not_block_sha(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")

    class Source:
        def discover(self, *_args):
            return UpdateCandidate(SHA, "url", ("update-ready",))

    class Builder:
        def prepare(self, _candidate):
            raise RuntimeError("local signing identity is temporarily unavailable")

    assert UpdateCoordinator(store, Source(), Builder()).check_and_prepare() is None
    state = store.load()
    assert SHA not in state.blocked_shas
    assert "signing identity" in state.last_error


def test_coordinator_refuses_candidate_without_ready_check_evidence(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")

    class Source:
        def discover(self, *_args):
            return UpdateCandidate(SHA, "url", ("test",))

    class Builder:
        def prepare(self, _candidate):
            raise AssertionError("unready candidates must never build")

    assert UpdateCoordinator(store, Source(), Builder()).check_and_prepare() is None
    state = store.load()
    assert not state.candidate_sha
    assert "exact-SHA validation check" in state.last_error


def test_candidate_canary_uses_disposable_data_root(monkeypatch, tmp_path):
    live_data = tmp_path / "live"
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(live_data))
    CanaryConfigStore().save("example.edu", "admin@example.edu")
    live_database = live_data / "directory_index.db"
    _database(live_database, "unchanged")
    pending = _app_bundle(tmp_path / "pending" / "GamGUI.app", "candidate")
    seen_roots = []

    def run(_argv, **kwargs):
        scratch = Path(kwargs["env"]["GAMGUI_APP_DATA_DIR"])
        seen_roots.append(scratch)
        assert scratch != live_data
        _database(scratch / "directory_index.db", "candidate")
        return subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "ok": True,
                    "checked_at": 1.0,
                    "checks": [
                        {"name": name, "ok": True, "duration_ms": 1.0}
                        for name in ("users", "groups", "classroom", "drive")
                    ],
                }
            ),
            "",
        )

    payload = LocalUpdateBuilder(root=live_data / "updates", run=run).run_canary(
        pending
    )

    assert payload["ok"] is True
    assert _database_value(live_database) == "unchanged"
    assert seen_roots and not seen_roots[0].exists()
    persisted = (live_data / "updates" / "canary-last.json").read_text()
    assert "admin@example.edu" not in persisted


@pytest.mark.parametrize(
    ("returncode", "mutate", "message"),
    [
        (3, lambda value: value, "process failed"),
        (
            0,
            lambda value: {**value, "checks": value["checks"][:-1]},
            "required checks",
        ),
        (
            0,
            lambda value: {
                **value,
                "checks": [
                    *value["checks"][:-1],
                    {"name": "arbitrary", "ok": True, "duration_ms": 1.0},
                ],
            },
            "invalid check",
        ),
        (
            0,
            lambda value: {
                **value,
                "checks": [
                    {**value["checks"][0], "duration_ms": float("inf")},
                    *value["checks"][1:],
                ],
            },
            "invalid check",
        ),
    ],
)
def test_candidate_canary_requires_successful_fixed_contract(
    monkeypatch,
    tmp_path,
    returncode,
    mutate,
    message,
):
    live_data = tmp_path / "live"
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(live_data))
    CanaryConfigStore().save("example.edu", "admin@example.edu")
    payload = {
        "ok": True,
        "checked_at": 1.0,
        "checks": [
            {"name": name, "ok": True, "duration_ms": 1.0}
            for name in CANARY_CHECK_NAMES
        ],
    }

    def run(_argv, **_kwargs):
        return subprocess.CompletedProcess(
            [],
            returncode,
            json.dumps(mutate(payload)),
            "",
        )

    with pytest.raises(RuntimeError, match=message):
        LocalUpdateBuilder(root=live_data / "updates", run=run).run_canary(
            _app_bundle(tmp_path / "pending" / "GamGUI.app", "candidate")
        )


def test_backup_retention_keeps_two_newest_and_recent_old(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    now = time.time()
    for index, age_days in enumerate([1, 2, 40, 50]):
        entry = backups / str(index)
        entry.mkdir()
        stamp = now - age_days * 86400
        os.utime(entry, (stamp, stamp))
    removed = UpdateCoordinator().prune_backups(backups, now=now)
    assert {path.name for path in removed} == {"2", "3"}
    assert {path.name for path in backups.iterdir()} == {"0", "1"}


def _app_bundle(path: Path, content: str) -> Path:
    executable = path / "Contents" / "MacOS" / "GamGUI"
    executable.parent.mkdir(parents=True)
    executable.write_text(content, encoding="utf-8")
    return path


def _database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS sample (value TEXT)")
        connection.execute("DELETE FROM sample")
        connection.execute("INSERT INTO sample VALUES (?)", (value,))
        connection.commit()


def _database_value(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT value FROM sample").fetchone()[0]


class _Process:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


def test_installer_activates_exact_candidate_and_records_snapshot(tmp_path):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    database = data_root / "directory.db"
    _database(database, "before")
    store = UpdateStateStore(root / "state.json")
    store.save(_ready_state(pending))
    launches = []

    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, "", "")

    def popen(argv, env):
        launches.append((argv, env))
        marker = Path(env["GAMGUI_UPDATE_HEALTH_MARKER"])
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok\n", encoding="utf-8")
        return _Process()

    installer = LocalUpdateInstaller(
        store=store,
        root=root,
        data_root=data_root,
        run=run,
        popen=popen,
    )
    assert installer.install(SHA, pending, current, health_timeout=0.1)
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text(encoding="utf-8") == "new"
    state = store.load()
    assert state.installed_sha == SHA and not state.candidate_sha and not state.pending_app
    assert Path(state.schema_snapshot, "directory.db").is_file()
    assert len(state.retained_rollbacks) == 1
    assert len(launches) == 1
    assert launches[0][1]["GAMGUI_SKIP_UPDATE_ONCE"] == "1"


def test_installer_restores_app_and_database_when_health_fails(tmp_path):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    database = data_root / "directory.db"
    _database(database, "before")
    store = UpdateStateStore(root / "state.json")
    store.save(_ready_state(pending))
    launches = []

    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, "", "")

    def popen(argv, env):
        launches.append((argv, env))
        if len(launches) == 1:
            _database(database, "migrated")
            return _Process(returncode=1)
        assert env["GAMGUI_SKIP_UPDATE_ONCE"] == "1"
        return _Process()

    installer = LocalUpdateInstaller(
        store=store,
        root=root,
        data_root=data_root,
        run=run,
        popen=popen,
    )
    assert not installer.install(SHA, pending, current, health_timeout=0.01)
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text(encoding="utf-8") == "old"
    assert _database_value(database) == "before"
    state = store.load()
    assert SHA in state.blocked_shas and "startup health" in state.last_error
    assert len(launches) == 2


def test_snapshot_failure_never_deletes_live_database(monkeypatch, tmp_path):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    database = data_root / "directory.db"
    _database(database, "before")
    store = UpdateStateStore(root / "state.json")
    store.save(_ready_state(pending))

    def fail_snapshot(*_args, **_kwargs):
        raise OSError("snapshot disk full")

    monkeypatch.setattr("gamgui.core.updater.snapshot_databases", fail_snapshot)
    installer = LocalUpdateInstaller(
        store=store,
        root=root,
        data_root=data_root,
        popen=lambda *_args, **_kwargs: _Process(),
    )

    assert not installer.install(SHA, pending, current)
    assert _database_value(database) == "before"
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    assert not store.load().schema_snapshot


def test_state_commit_failure_restores_previous_app_and_database(tmp_path):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    database = data_root / "directory.db"
    _database(database, "before")
    backing = UpdateStateStore(root / "state.json")
    backing.save(_ready_state(pending))

    class FailCommitStore:
        failed = False

        def load(self):
            return backing.load()

        def save(self, state):
            if state.installed_sha == SHA and not self.failed:
                self.failed = True
                raise OSError("state disk full")
            backing.save(state)

    def popen(_argv, env):
        marker = env.get("GAMGUI_UPDATE_HEALTH_MARKER")
        if marker:
            marker = Path(marker)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ok\n", encoding="utf-8")
        return _Process()

    installer = LocalUpdateInstaller(
        store=FailCommitStore(),
        root=root,
        data_root=data_root,
        run=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
        popen=popen,
    )

    assert not installer.install(SHA, pending, current, health_timeout=0.1)
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    assert _database_value(database) == "before"
    assert SHA in backing.load().blocked_shas


def test_state_failure_during_rollback_still_relaunches_previous_app(tmp_path):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    database = data_root / "directory.db"
    _database(database, "before")
    backing = UpdateStateStore(root / "state.json")
    backing.save(_ready_state(pending))

    class FailEverySaveStore:
        def load(self):
            return backing.load()

        def save(self, _state):
            raise OSError("state storage unavailable")

    launches = []

    def popen(argv, env):
        launches.append((argv, env))
        if len(launches) == 1:
            return _Process(returncode=1)
        return _Process()

    installer = LocalUpdateInstaller(
        store=FailEverySaveStore(),
        root=root,
        data_root=data_root,
        run=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
        popen=popen,
    )

    assert not installer.install(SHA, pending, current, health_timeout=0.01)
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    assert _database_value(database) == "before"
    assert len(launches) == 2
    assert launches[-1][1]["GAMGUI_SKIP_UPDATE_ONCE"] == "1"


@pytest.mark.parametrize(
    "state",
    [
        UpdateState(
            candidate_sha=SHA,
            canary_result="passed",
            required_check_evidence=[],
        ),
        UpdateState(
            candidate_sha=SHA,
            canary_result="failed",
            required_check_evidence=["update-ready"],
        ),
    ],
)
def test_installer_rejects_staged_state_without_both_activation_evidences(
    tmp_path,
    state,
):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    state.pending_app = str(pending)
    store = UpdateStateStore(root / "state.json")
    store.save(state)

    installer = LocalUpdateInstaller(store=store, root=root, data_root=data_root)
    assert not installer.install(SHA, pending, current)
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    blocked = store.load()
    assert SHA in blocked.blocked_shas
    assert "required CI or canary evidence" in blocked.last_error


def test_installer_restores_old_app_when_second_rename_fails(monkeypatch, tmp_path):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    store = UpdateStateStore(root / "state.json")
    store.save(_ready_state(pending))
    original_replace = os.replace
    failed = False

    def fail_second_swap(source, destination):
        nonlocal failed
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not failed
            and source_path.name.endswith(".incoming")
            and destination_path == current
        ):
            failed = True
            raise OSError("simulated second rename failure")
        return original_replace(source, destination)

    monkeypatch.setattr("gamgui.core.updater.os.replace", fail_second_swap)
    installer = LocalUpdateInstaller(
        store=store,
        root=root,
        data_root=data_root,
        run=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )

    assert not installer.install(SHA, pending, current)
    assert failed
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    assert SHA in store.load().blocked_shas


def test_stop_waits_again_after_kill(tmp_path):
    class Process:
        returncode = None
        killed = False
        waits = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            return None

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("GamGUI", timeout)
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = Process()
    LocalUpdateInstaller(root=tmp_path)._stop(process)
    assert process.killed and process.waits == 2 and process.poll() == -9


def test_installer_does_not_restore_while_updated_process_may_be_running(tmp_path):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    database = data_root / "directory.db"
    _database(database, "before")
    store = UpdateStateStore(root / "state.json")
    store.save(_ready_state(pending))

    class StubbornProcess:
        killed = False

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("GamGUI", timeout)

        def kill(self):
            self.killed = True

    process = StubbornProcess()

    def popen(_argv, _env=None, **_kwargs):
        _database(database, "candidate")
        return process

    installer = LocalUpdateInstaller(
        store=store,
        root=root,
        data_root=data_root,
        run=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
        popen=popen,
    )
    assert not installer.install(SHA, pending, current, health_timeout=0.01)
    assert process.killed
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "new"
    assert _database_value(database) == "candidate"
    state = store.load()
    assert "restore deferred" in state.last_error


def test_database_snapshot_excludes_updater_state(tmp_path):
    data_root = tmp_path / "data"
    _database(data_root / "directory.db", "one")
    _database(data_root / "nested" / "course.sqlite", "two")
    _database(data_root / "updates" / "ignored.db", "secret")
    destination = tmp_path / "snapshot"
    copied = snapshot_databases(data_root, destination)
    assert {path.relative_to(destination).as_posix() for path in copied} == {
        "directory.db",
        "nested/course.sqlite",
    }

    _database(data_root / "directory.db", "changed")
    restore_databases(data_root, destination)
    assert _database_value(data_root / "directory.db") == "one"


def test_prepare_database_schemas_initializes_every_store_on_copy(tmp_path):
    paths = prepare_database_schemas(tmp_path)
    assert {path.name for path in paths} == {
        "directory_index.db",
        "calendar_index.db",
        "classroom_courses.db",
        "classroom_roster_operations.db",
        "drive_operations.db",
    }
    assert all(path.is_file() for path in paths)
    assert bundle_self_test(tmp_path, require_gam=False)["ok"] is True


def test_bundle_self_test_checks_sqlite_integrity(tmp_path):
    _database(tmp_path / "valid.db", "ok")
    assert bundle_self_test(tmp_path, require_gam=False)["ok"] is True
    (tmp_path / "broken.db").write_bytes(b"not sqlite")
    result = bundle_self_test(tmp_path, require_gam=False)
    assert result["ok"] is False
    assert any("database unreadable" in item for item in result["failures"])


def test_installed_app_path():
    path = Path("/Applications/GamGUI.app/Contents/MacOS/GamGUI")
    assert installed_app_path(path) == Path("/Applications/GamGUI.app")
    assert installed_app_path(Path("/usr/local/bin/python")) is None


def test_health_marker(monkeypatch, tmp_path):
    marker = tmp_path / "health" / "ok"
    monkeypatch.setenv("GAMGUI_UPDATE_HEALTH_MARKER", str(marker))
    write_health_marker_from_environment()
    assert marker.read_text(encoding="utf-8") == "ok\n"
