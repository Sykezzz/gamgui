from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import time
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from gamgui.core.activation_lock import OwnerOnlyActivationLock
from gamgui.core.updater import (
    ACTIVATION_APP_UPDATE,
    ACTIVATION_PHASE_HEALTH_PASSED,
    ACTIVATION_PHASE_PREPARED,
    ACTIVATION_PHASE_SWAPPED,
    ACTIVATION_PROBE_ENV,
    ACTIVATION_TRANSACTION_ENV,
    ActivationJournal,
    _managed_mac_build_environment,
    _extract_verified_archive,
    GitHubUpdateSource,
    LocalUpdateBuilder,
    LocalUpdateInstaller,
    LOCAL_COMMAND_TIMEOUT_SECONDS,
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
from gamgui.core.components import (
    CORE_PROFILE,
    ComponentError,
    build_profile_payload,
    verify_bundle_artifact,
    write_artifact_sidecar,
)
from gamgui.core.canary import CANARY_CHECK_NAMES, CanaryConfigStore

SHA = "a" * 40
TRANSACTION = "1" * 32


def _signed_bundle_run(argv, **kwargs):
    """Portable codesign double for tests that pass bundle verification."""

    if argv[:3] == ["codesign", "--display", "--extract-certificates"]:
        Path(kwargs["cwd"], "codesign0.cer").write_bytes(b"test-signing-leaf")
    return subprocess.CompletedProcess(
        argv,
        0,
        "",
        "Authority=GamGUI Local\n",
    )


def test_managed_mac_build_environment_includes_gui_missing_tool_paths():
    environment = _managed_mac_build_environment(
        {"PATH": "/custom/bin"},
        home=Path("/Users/admin"),
    )
    paths = environment["PATH"].split(os.pathsep)

    assert paths[:3] == [
        "/Users/admin/.local/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    assert "/usr/bin" in paths
    assert paths[-1] == "/custom/bin"
    assert len(paths) == len(set(paths))


def _ready_state(pending: Path) -> UpdateState:
    envelope = verify_bundle_artifact(pending)
    return UpdateState(
        candidate_sha=SHA,
        pending_app=str(pending),
        canary_result="passed",
        required_check_evidence=["update-ready"],
        desired_profile=envelope.artifact.profile,
        candidate_artifact=envelope.artifact,
        candidate_signing_channel=envelope.signing_channel,
        candidate_signing_authority=envelope.signing_authority,
        activation_kind=ACTIVATION_APP_UPDATE,
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


def test_update_state_round_trips_durable_activation_journal(tmp_path):
    root = tmp_path / "updates"
    transaction = "2" * 32
    journal = ActivationJournal(
        transaction_id=transaction,
        candidate_sha=SHA,
        phase=ACTIVATION_PHASE_PREPARED,
        current_app=str((tmp_path / "Applications" / "GamGUI.app").resolve()),
        pending_app=str((root / "pending" / "GamGUI.app").resolve()),
        incoming_app=str((tmp_path / "Applications" / ".GamGUI.incoming").resolve()),
        previous_app=str((tmp_path / "Applications" / ".GamGUI.previous").resolve()),
        backup=str((root / "backups" / transaction).resolve()),
        backup_app=str((root / "backups" / transaction / "GamGUI.app").resolve()),
        backup_sidecar=str((root / "backups" / transaction / "old.json").resolve()),
        candidate_sidecar=str((root / "backups" / transaction / "new.json").resolve()),
        database_snapshot=str((root / "backups" / transaction / "database").resolve()),
        health_marker=str((root / "health" / f"{transaction}.json").resolve()),
    )
    store = UpdateStateStore(root / "state.json")
    state = UpdateState(
        candidate_sha=SHA,
        activation_transaction_id=transaction,
        activation_journal=journal,
    )

    store.save(state)

    assert store.load() == state


def test_corrupt_state_fails_closed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{", encoding="utf-8")
    state = UpdateStateStore(path).load()
    assert state.activation_journal_invalid
    assert state.component_error_code == "CMP-VERIFY-FAILED"
    assert "recovery mode" in state.last_error


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
                "component_prompt_answered": "false",
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
    assert state.component_prompt_answered is False


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


def test_private_fork_discovery_requires_validated_ref_to_equal_branch_head():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            (
                f"{SHA}\trefs/heads/district-main\n"
                f"{SHA}\trefs/heads/update-ready\n"
            ),
            "",
        )

    candidate = GitHubUpdateSource(run=run).discover()

    assert candidate == UpdateCandidate(
        SHA,
        f"https://github.com/Sykezzz/gamgui/commit/{SHA}",
        ("update-ready",),
    )
    argv, kwargs = calls[0]
    assert argv[:3] == ["git", "ls-remote", "--heads"]
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kwargs["timeout"] == 15


def test_private_fork_discovery_rejects_stale_validated_ref():
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            (
                f"{SHA}\trefs/heads/district-main\n"
                f"{'b' * 40}\trefs/heads/update-ready\n"
            ),
            "",
        )

    assert GitHubUpdateSource(run=run).discover() is None


def test_coordinator_prepares_and_records_canary(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")

    class Source:
        def discover(self, *_args):
            return UpdateCandidate(SHA, "url", ("update-ready",))

    class Builder:
        def prepare(self, candidate):
            assert candidate.sha == SHA
            return _app_bundle(tmp_path / "GamGUI.app", "candidate")

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


def test_official_install_never_silently_builds_a_local_update(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha="b" * 40,
            installed_signing_channel="developer-id",
            installed_signing_authority=(
                "Developer ID Application: District Admin (ABCDE12345)"
            ),
        )
    )

    class Source:
        def discover(self, *_args):
            raise AssertionError("official channel must not use the local-build source")

    class Builder:
        def prepare(self, *_args, **_kwargs):
            raise AssertionError("official channel must not build a local artifact")

    assert UpdateCoordinator(store, Source(), Builder()).check_and_prepare() is None
    assert "Official-channel updates" in store.load().last_error


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
            return _app_bundle(tmp_path / "GamGUI.app", "candidate")

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
        assert kwargs["timeout"] == LOCAL_COMMAND_TIMEOUT_SECONDS
        assert kwargs["env"]["GAMGUI_CANARY_DOMAIN"] == "example.edu"
        assert kwargs["env"]["GAMGUI_CANARY_SUBJECT"] == "admin@example.edu"
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


def test_verified_archive_rejects_content_nested_under_symlink(tmp_path):
    archive_path = tmp_path / "candidate.zip"
    link = zipfile.ZipInfo("GamGUI.app/Contents/linked")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(link, "Resources")
        archive.writestr(
            "GamGUI.app/Contents/linked/payload",
            b"must not follow the parent link",
        )

    with pytest.raises(ComponentError, match="beneath a symbolic link"):
        _extract_verified_archive(archive_path, tmp_path / "extract")

    assert not (tmp_path / "extract").exists()


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


def _app_bundle(
    path: Path,
    content: str,
    *,
    profile: str = CORE_PROFILE,
    source_sha: str = SHA,
) -> Path:
    executable = path / "Contents" / "MacOS" / "GamGUI"
    executable.parent.mkdir(parents=True)
    executable.write_text(content, encoding="utf-8")
    metadata = (
        path
        / "Contents"
        / "Resources"
        / "resources"
        / "components"
        / "profile.json"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            build_profile_payload(
                profile,
                source_sha=source_sha,
                version="1",
                architecture="arm64",
                minimum_macos_version="12.0",
                packaging_revision="1",
            )
        ),
        encoding="utf-8",
    )
    write_artifact_sidecar(
        path,
        signing_channel="local",
        signing_authority="GamGUI Local",
    )
    return path


def _write_candidate_health(
    environment: dict[str, str],
    pending: Path,
) -> None:
    artifact = verify_bundle_artifact(pending).artifact
    marker = Path(environment["GAMGUI_UPDATE_HEALTH_MARKER"])
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "ok": True,
                "transaction_id": environment[
                    "GAMGUI_ACTIVATION_TRANSACTION_ID"
                ],
                "sha": environment["GAMGUI_INSTALLED_SHA"],
                "profile": artifact.profile,
                "component_set_digest": artifact.component_set_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def test_update_notice_requires_activation_evidence(monkeypatch, tmp_path):
    from gamgui.web.server import _local_update_notice

    data_root = tmp_path / "data"
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(data_root))
    pending = _app_bundle(
        data_root / "updates" / "pending" / SHA / "GamGUI.app",
        "candidate",
    )
    store = UpdateStateStore()
    store.save(UpdateState(candidate_sha=SHA, pending_app=str(pending)))

    assert _local_update_notice() == ""

    store.save(_ready_state(pending))
    assert _local_update_notice() == (
        "A verified update is ready. Quit and reopen GamGUI to install it."
    )


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

    def run(argv, **kwargs):
        return _signed_bundle_run(argv, **kwargs)

    def popen(argv, env):
        launches.append((argv, env))
        if env.get("GAMGUI_UPDATE_HEALTH_MARKER"):
            _write_candidate_health(env, pending)
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
    assert len(launches) == 2
    assert launches[0][1]["GAMGUI_SKIP_UPDATE_ONCE"] == "1"
    assert "GAMGUI_UPDATE_HEALTH_MARKER" in launches[0][1]
    assert "GAMGUI_UPDATE_HEALTH_MARKER" not in launches[1][1]
    assert "GAMGUI_ACTIVATION_PROBE" not in launches[1][1]


@pytest.mark.parametrize(
    "crash_phase",
    [
        ACTIVATION_PHASE_PREPARED,
        ACTIVATION_PHASE_SWAPPED,
        ACTIVATION_PHASE_HEALTH_PASSED,
    ],
)


def test_durable_journal_recovers_after_abrupt_exit_at_each_activation_phase(
    crash_phase,
    tmp_path,
):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    database = data_root / "directory.db"
    _database(database, "before")
    backing = UpdateStateStore(root / "state.json")
    backing.save(_ready_state(pending))

    class CrashAfterJournalPhase:
        crashed = False

        def load(self):
            return backing.load()

        def save(self, state):
            backing.save(state)
            journal = state.activation_journal
            if (
                not self.crashed
                and journal is not None
                and journal.phase == crash_phase
            ):
                self.crashed = True
                raise SystemExit(f"crash after {crash_phase}")

    def popen(_argv, env):
        _database(database, "candidate")
        _write_candidate_health(env, pending)
        return _Process()

    crashed = LocalUpdateInstaller(
        store=CrashAfterJournalPhase(),
        root=root,
        data_root=data_root,
        run=_signed_bundle_run,
        popen=popen,
    )
    with pytest.raises(SystemExit, match=crash_phase):
        crashed.install(SHA, pending, current, health_timeout=0.1)

    interrupted = backing.load()
    assert interrupted.activation_journal is not None
    assert interrupted.activation_journal.phase == crash_phase

    recovery = LocalUpdateInstaller(
        store=backing,
        root=root,
        data_root=data_root,
        run=_signed_bundle_run,
    )
    assert recovery.recover(
        current_app=current,
        transaction_id=interrupted.activation_journal.transaction_id,
    )
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    assert _database_value(database) == "before"
    recovered = backing.load()
    assert recovered.activation_journal is None
    assert recovered.candidate_sha == ""
    assert f"sha:{SHA}" in recovered.profile_blocklists[CORE_PROFILE]


def test_installer_reverifies_incoming_copy_before_exchange(monkeypatch, tmp_path):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    store = UpdateStateStore(root / "state.json")
    store.save(_ready_state(pending))
    original_copytree = shutil.copytree

    def tamper_incoming(source, destination, *args, **kwargs):
        result = original_copytree(source, destination, *args, **kwargs)
        target = Path(destination)
        if target.name.endswith(".incoming"):
            (target / "Contents" / "MacOS" / "GamGUI").write_text(
                "tampered",
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr("gamgui.core.updater.shutil.copytree", tamper_incoming)
    installer = LocalUpdateInstaller(
        store=store,
        root=root,
        data_root=data_root,
        run=_signed_bundle_run,
    )

    assert not installer.install(SHA, pending, current)
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    state = store.load()
    assert state.activation_journal is None
    assert f"sha:{SHA}" in state.profile_blocklists[CORE_PROFILE]


def test_installer_rejects_activation_lock_from_another_path(tmp_path):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    store = UpdateStateStore(root / "state.json")
    store.save(_ready_state(pending))
    wrong_lock = OwnerOnlyActivationLock.try_acquire(root / "other.lock")
    assert wrong_lock is not None
    try:
        installer = LocalUpdateInstaller(
            store=store,
            root=root,
            data_root=data_root,
        )
        assert not installer.install(
            SHA,
            pending,
            current,
            activation_lock=wrong_lock,
        )
    finally:
        wrong_lock.release()
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    assert store.load().candidate_sha == SHA


def test_health_marker_from_exited_process_is_rejected(tmp_path):
    marker = tmp_path / "health.json"
    expected = {"ok": True, "sha": SHA}
    marker.write_text(json.dumps(expected), encoding="utf-8")
    installer = LocalUpdateInstaller(root=tmp_path)

    assert not installer._wait_for_health(
        marker,
        _Process(returncode=0),
        0.1,
        expected,
    )


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

    def run(argv, **kwargs):
        return _signed_bundle_run(argv, **kwargs)

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
    assert f"sha:{SHA}" in state.profile_blocklists[CORE_PROFILE]
    assert "rolled back" in state.last_error
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


def test_insufficient_disk_keeps_candidate_retryable_and_relaunches_current_app(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    store = UpdateStateStore(root / "state.json")
    store.save(_ready_state(pending))
    launches = []

    monkeypatch.setattr(
        "gamgui.core.updater.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": 0})(),
    )

    def popen(argv, env=None, **_kwargs):
        launches.append((argv, env or {}))
        return _Process()

    installer = LocalUpdateInstaller(
        store=store,
        root=root,
        data_root=data_root,
        popen=popen,
    )

    assert not installer.install(SHA, pending, current)
    state = store.load()
    assert state.candidate_sha == SHA
    assert SHA not in state.blocked_shas
    assert state.component_error_code == "CMP-DOWNLOAD-FAILED"
    assert pending.is_dir()
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    assert launches and launches[0][1]["GAMGUI_SKIP_UPDATE_ONCE"] == "1"


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
            _write_candidate_health(env, pending)
        return _Process()

    installer = LocalUpdateInstaller(
        store=FailCommitStore(),
        root=root,
        data_root=data_root,
        run=_signed_bundle_run,
        popen=popen,
    )

    assert not installer.install(SHA, pending, current, health_timeout=0.1)
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    assert _database_value(database) == "before"
    assert f"sha:{SHA}" in backing.load().profile_blocklists[CORE_PROFILE]


def test_state_failure_during_rollback_still_relaunches_previous_app(tmp_path):
    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    database = data_root / "directory.db"
    _database(database, "before")
    backing = UpdateStateStore(root / "state.json")
    ready = _ready_state(pending)
    ready.activation_transaction_id = TRANSACTION
    backing.save(ready)

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
        run=_signed_bundle_run,
        popen=popen,
    )

    assert not installer.install(
        SHA,
        pending,
        current,
        health_timeout=0.01,
        transaction_id=TRANSACTION,
    )
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    assert _database_value(database) == "before"
    assert len(launches) == 1
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
    envelope = verify_bundle_artifact(pending)
    state.desired_profile = envelope.artifact.profile
    state.candidate_artifact = envelope.artifact
    state.candidate_signing_channel = envelope.signing_channel
    state.candidate_signing_authority = envelope.signing_authority
    state.activation_kind = ACTIVATION_APP_UPDATE
    store = UpdateStateStore(root / "state.json")
    store.save(state)

    installer = LocalUpdateInstaller(store=store, root=root, data_root=data_root)
    assert not installer.install(SHA, pending, current)
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    blocked = store.load()
    assert f"sha:{SHA}" in blocked.profile_blocklists[CORE_PROFILE]
    assert "required CI or canary evidence" in blocked.last_error


def test_installer_restores_old_app_when_atomic_exchange_fails(monkeypatch, tmp_path):
    from gamgui.core import updater as updater_module

    root = tmp_path / "data" / "updates"
    data_root = tmp_path / "data"
    current = _app_bundle(tmp_path / "Applications" / "GamGUI.app", "old")
    pending = _app_bundle(root / "pending" / SHA / "GamGUI.app", "new")
    store = UpdateStateStore(root / "state.json")
    store.save(_ready_state(pending))
    failed = False
    original_exchange = updater_module._atomic_exchange

    def fail_exchange(source, destination):
        nonlocal failed
        if not failed:
            assert Path(source) == current
            assert Path(destination).name.endswith(".incoming")
            failed = True
            raise OSError("simulated atomic exchange failure")
        return original_exchange(source, destination)

    monkeypatch.setattr("gamgui.core.updater._atomic_exchange", fail_exchange)
    installer = LocalUpdateInstaller(
        store=store,
        root=root,
        data_root=data_root,
        run=_signed_bundle_run,
    )

    assert not installer.install(SHA, pending, current)
    assert failed
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "old"
    assert f"sha:{SHA}" in store.load().profile_blocklists[CORE_PROFILE]


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
        run=_signed_bundle_run,
        popen=popen,
    )
    assert not installer.install(SHA, pending, current, health_timeout=0.01)
    assert process.killed
    assert (current / "Contents" / "MacOS" / "GamGUI").read_text() == "new"
    assert _database_value(database) == "candidate"
    state = store.load()
    assert "recovery is pending" in state.last_error
    assert state.candidate_sha == SHA
    assert state.pending_app == str(pending)
    assert state.activation_journal is not None


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


def test_prepare_database_schemas_initializes_every_core_store_on_copy(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GAMGUI_BUILD_PROFILE", "core")
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
    assert json.loads(marker.read_text(encoding="utf-8")) == {"ok": True}
    assert not list(marker.parent.glob("*.tmp"))


def test_transaction_health_marker_retains_bound_json(
    monkeypatch,
    tmp_path,
):
    marker = tmp_path / "health" / f"{TRANSACTION}.json"
    payload = {
        "ok": True,
        "transaction_id": TRANSACTION,
        "sha": SHA,
        "profile": CORE_PROFILE,
        "component_set_digest": "b" * 64,
    }
    monkeypatch.setenv("GAMGUI_UPDATE_HEALTH_MARKER", str(marker))
    monkeypatch.setenv("GAMGUI_INSTALLED_SHA", SHA)
    monkeypatch.setenv("GAMGUI_SKIP_UPDATE_ONCE", "1")
    monkeypatch.setenv(ACTIVATION_PROBE_ENV, "1")
    monkeypatch.setenv(ACTIVATION_TRANSACTION_ENV, TRANSACTION)

    write_health_marker_from_environment(payload)

    assert json.loads(marker.read_text(encoding="utf-8")) == payload
    assert not list(marker.parent.glob("*.tmp"))


def test_legacy_health_marker_refuses_invalid_component_digest(
    monkeypatch,
    tmp_path,
):
    marker = tmp_path / "health" / f"{SHA}.ok"
    payload = {
        "ok": True,
        "transaction_id": "",
        "sha": SHA,
        "profile": CORE_PROFILE,
        "component_set_digest": "B" * 64,
    }
    monkeypatch.setenv("GAMGUI_UPDATE_HEALTH_MARKER", str(marker))
    monkeypatch.setenv("GAMGUI_INSTALLED_SHA", SHA)
    monkeypatch.setenv("GAMGUI_SKIP_UPDATE_ONCE", "1")

    write_health_marker_from_environment(payload)

    assert json.loads(marker.read_text(encoding="utf-8")) == payload
