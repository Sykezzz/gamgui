from __future__ import annotations

import json
import multiprocessing
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from gamgui.core import activity as activity_module
from gamgui.core.activity import (
    DURABLE_ACTIVITY_FILENAME,
    ActivityBusyError,
    ActivityRegistry,
)

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix",
    reason="Durable app activity leases use POSIX advisory file locks.",
)


def _race_for_durable_lease(
    path: str,
    ready,
    start,
    finish,
    results,
) -> None:
    registry = ActivityRegistry(durable_path=Path(path))
    ready.put(True)
    if not start.wait(15):
        results.put(False)
        return
    lease = registry.try_acquire("app-update")
    results.put(lease is not None)
    if lease is not None:
        finish.wait(15)
        lease.release()


def _write_durable_record(
    path: Path,
    *,
    token: str = "a" * 32,
    pid: int = 424242,
    identity: str = "old-process",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "kind": "app-update",
                "timestamp": 123.0,
                "pid": pid,
                "process_identity": identity,
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def test_activity_registry_is_exclusive_and_release_is_idempotent():
    registry = ActivityRegistry(clock=lambda: 123.0)
    lease = registry.acquire("component-profile-build")

    assert registry.snapshot().kind == "component-profile-build"
    assert registry.snapshot().started_at == 123.0
    with pytest.raises(ActivityBusyError) as caught:
        registry.acquire("app-update")
    assert caught.value.error_code == "CMP-ACTIVE-JOB"
    assert "profile" not in str(caught.value).lower()

    lease.release()
    lease.release()
    with registry.acquire("app-update"):
        assert registry.is_active()
    assert not registry.is_active()


@pytest.mark.parametrize("kind", ["", "has spaces", "../escape", "x" * 65])
def test_activity_registry_rejects_unbounded_or_unsafe_kinds(kind):
    with pytest.raises(ValueError):
        ActivityRegistry().acquire(kind)


@POSIX_ONLY
def test_durable_registry_observes_an_external_live_lease(tmp_path):
    path = tmp_path / "activity.json"
    holder = ActivityRegistry(
        clock=lambda: 321.0,
        durable_path=path,
    )
    observer = ActivityRegistry(durable_path=path)

    lease = holder.acquire("component-profile-build")

    assert observer.snapshot().kind == "component-profile-build"
    assert observer.snapshot().started_at == 321.0
    assert observer.is_active()
    assert observer.try_acquire("app-update") is None

    lease.release()
    assert observer.snapshot() is None
    assert not path.exists()


@POSIX_ONLY
def test_durable_atomic_create_allows_one_cross_process_winner(tmp_path):
    path = tmp_path / "activity.json"
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    finish = context.Event()
    processes = [
        context.Process(
            target=_race_for_durable_lease,
            args=(str(path), ready, start, finish, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=15)
        assert ready.get(timeout=15)
        start.set()
        outcomes = [results.get(timeout=15), results.get(timeout=15)]
        assert sum(outcomes) == 1
    finally:
        finish.set()
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert all(process.exitcode == 0 for process in processes)
    assert not path.exists()


@POSIX_ONLY
def test_durable_registry_recovers_only_a_proven_stale_lease(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "activity.json"
    _write_durable_record(path)
    observed = []

    def definitely_dead(pid, identity):
        observed.append((pid, identity))
        return True

    monkeypatch.setattr(
        activity_module,
        "process_lease_is_dead",
        definitely_dead,
    )
    registry = ActivityRegistry(durable_path=path)

    assert registry.snapshot() is None
    assert observed == [(424242, "old-process")]
    assert not path.exists()
    with registry.acquire("app-update"):
        assert registry.is_active()


@POSIX_ONLY
def test_durable_registry_keeps_unknown_or_live_lease_fail_closed(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "activity.json"
    _write_durable_record(path)
    monkeypatch.setattr(
        activity_module,
        "process_lease_is_dead",
        lambda _pid, _identity: False,
    )
    registry = ActivityRegistry(durable_path=path)

    snapshot = registry.snapshot()

    assert snapshot is not None
    assert snapshot.kind == "app-update"
    assert registry.is_active()
    assert registry.try_acquire("component-profile-build") is None
    assert path.exists()


@POSIX_ONLY
def test_durable_registry_keeps_corrupt_lease_fail_closed(tmp_path):
    path = tmp_path / "activity.json"
    path.write_text("{not-json", encoding="utf-8")
    registry = ActivityRegistry(durable_path=path)

    snapshot = registry.snapshot()

    assert snapshot is not None
    assert snapshot.kind == "external-activity"
    assert snapshot.started_at == 0.0
    assert registry.try_acquire("app-update") is None
    assert path.read_text(encoding="utf-8") == "{not-json"


@POSIX_ONLY
def test_durable_registry_rejects_explicit_legacy_lock_protocol(tmp_path):
    path = tmp_path / "activity.json"
    _write_durable_record(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["lock_protocol"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)

    snapshot = ActivityRegistry(durable_path=path).snapshot()

    assert snapshot is not None
    assert snapshot.kind == "external-activity"
    assert path.exists()


@POSIX_ONLY
def test_durable_registry_refuses_group_or_world_readable_lease(tmp_path):
    path = tmp_path / "activity.json"
    _write_durable_record(path)
    os.chmod(path, 0o644)
    registry = ActivityRegistry(durable_path=path)

    snapshot = registry.snapshot()

    assert snapshot is not None
    assert snapshot.kind == "external-activity"
    assert registry.try_acquire("app-update") is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@POSIX_ONLY
def test_durable_lease_and_guard_are_owner_only_and_privacy_bounded(tmp_path):
    path = tmp_path / "private" / "activity.json"
    registry = ActivityRegistry(
        clock=lambda: 456.0,
        durable_path=path,
        pid_provider=lambda: 123,
        identity_provider=lambda: "process-start",
    )

    lease = registry.acquire("app-update")

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    guard = path.with_name(f"{path.name}.guard")
    assert stat.S_IMODE(guard.stat().st_mode) == 0o600
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "kind",
        "lock_device",
        "lock_inode",
        "lock_protocol",
        "timestamp",
        "pid",
        "process_identity",
        "token",
    }
    assert payload["kind"] == "app-update"
    assert payload["timestamp"] == 456.0
    assert payload["pid"] == 123
    assert payload["process_identity"] == "process-start"
    assert payload["lock_protocol"] == 1
    assert payload["lock_device"] > 0
    assert payload["lock_inode"] > 0
    process_locks = list(
        path.parent.glob(f"{path.name}.*.process-lock")
    )
    assert len(process_locks) == 1
    assert stat.S_IMODE(process_locks[0].stat().st_mode) == 0o600
    with registry.subprocess_pass_fds() as pass_fds:
        assert pass_fds
        inherited_descriptor = pass_fds[0]
        os.fstat(inherited_descriptor)
    with pytest.raises(OSError):
        os.fstat(inherited_descriptor)

    lease.release()
    assert not path.exists()
    assert not process_locks[0].exists()


@POSIX_ONLY
def test_durable_stale_recovery_waits_for_inherited_child_lock(tmp_path):
    path = tmp_path / "activity.json"
    release = tmp_path / "release-child"
    ready = tmp_path / "child-ready"
    project_root = Path(__file__).resolve().parents[1]
    child_script = textwrap.dedent(
        """
        import os
        import sys
        import time
        from pathlib import Path

        os.fstat(int(sys.argv[1]))
        release = Path(sys.argv[2])
        Path(sys.argv[3]).write_text("ready", encoding="ascii")
        deadline = time.monotonic() + 30
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        """
    )
    holder_script = textwrap.dedent(
        f"""
        import os
        import subprocess
        import sys
        import time
        from pathlib import Path

        from gamgui.core.activity import ActivityRegistry

        path = Path(sys.argv[1])
        release = Path(sys.argv[2])
        ready = Path(sys.argv[3])
        registry = ActivityRegistry(durable_path=path)
        registry.acquire("oneroster-import")
        child_script = {child_script!r}
        with registry.subprocess_pass_fds() as pass_fds:
            descriptor = pass_fds[0]
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_script,
                    str(descriptor),
                    str(release),
                    str(ready),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=pass_fds,
            )
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            child.kill()
            child.wait()
            raise SystemExit(2)
        print(child.pid, flush=True)
        os._exit(0)
        """
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            holder_script,
            str(path),
            str(release),
            str(ready),
        ],
        cwd=project_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None
        child_pid = int(holder.stdout.readline().strip())
        assert child_pid > 0
        assert holder.wait(timeout=15) == 0
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["pid"] == holder.pid
        # A damaged/downgraded record must still honor the token-derived lock.
        payload.pop("lock_protocol")
        payload.pop("lock_device")
        payload.pop("lock_inode")
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, 0o600)

        observer = ActivityRegistry(durable_path=path)
        snapshot = observer.snapshot()
        assert snapshot is not None
        assert snapshot.kind == "oneroster-import"
        assert path.exists()

        release.write_text("release", encoding="ascii")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if observer.snapshot() is None:
                break
            time.sleep(0.02)
        else:
            pytest.fail("lease remained recoverable only after inherited child exited")
        assert not path.exists()
        assert not list(path.parent.glob(f"{path.name}.*.process-lock"))
    finally:
        release.write_text("release", encoding="ascii")
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


@POSIX_ONLY
def test_missing_record_cannot_hide_an_inherited_child_lock(tmp_path):
    path = tmp_path / "activity.json"
    release = tmp_path / "release-child"
    ready = tmp_path / "child-ready"
    child_script = textwrap.dedent(
        """
        import os
        import sys
        import time
        from pathlib import Path

        os.fstat(int(sys.argv[1]))
        release = Path(sys.argv[2])
        Path(sys.argv[3]).write_text("ready", encoding="ascii")
        deadline = time.monotonic() + 30
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        """
    )
    registry = ActivityRegistry(durable_path=path)
    lease = registry.acquire("app-update")
    child = None
    try:
        with registry.subprocess_pass_fds() as pass_fds:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_script,
                    str(pass_fds[0]),
                    str(release),
                    str(ready),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=pass_fds,
            )
        deadline = time.monotonic() + 10
        while (
            not ready.exists()
            and child.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready.exists()

        path.unlink()
        lease.release()
        observer = ActivityRegistry(durable_path=path)

        snapshot = observer.snapshot()
        assert snapshot is not None
        assert snapshot.kind == "external-activity"
        assert observer.try_acquire("component-profile-build") is None

        release.write_text("release", encoding="ascii")
        assert child.wait(timeout=15) == 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if observer.snapshot() is None:
                break
            time.sleep(0.02)
        else:
            pytest.fail("free orphan process lock was not recovered")
        assert not list(path.parent.glob(f"{path.name}.*.process-lock"))
    finally:
        release.write_text("release", encoding="ascii")
        lease.release()
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@POSIX_ONLY
def test_early_release_reaps_after_child_exit_without_app_restart(tmp_path):
    path = tmp_path / "activity.json"
    release = tmp_path / "release-child"
    ready = tmp_path / "child-ready"
    child_script = textwrap.dedent(
        """
        import os
        import sys
        import time
        from pathlib import Path

        os.fstat(int(sys.argv[1]))
        release = Path(sys.argv[2])
        Path(sys.argv[3]).write_text("ready", encoding="ascii")
        deadline = time.monotonic() + 30
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        """
    )
    registry = ActivityRegistry(durable_path=path)
    lease = registry.acquire("oneroster-import")
    child = None
    try:
        with registry.subprocess_pass_fds() as pass_fds:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_script,
                    str(pass_fds[0]),
                    str(release),
                    str(ready),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=pass_fds,
            )
        deadline = time.monotonic() + 10
        while (
            not ready.exists()
            and child.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready.exists()

        lease.release()
        snapshot = registry.snapshot()
        assert snapshot is not None
        assert snapshot.kind == "oneroster-import"
        assert registry.try_acquire("component-profile-build") is None

        release.write_text("release", encoding="ascii")
        assert child.wait(timeout=15) == 0
        next_lease = registry.acquire("component-profile-build")
        try:
            assert registry.snapshot().kind == "component-profile-build"
        finally:
            next_lease.release()
        assert registry.snapshot() is None
        assert not path.exists()
    finally:
        release.write_text("release", encoding="ascii")
        lease.release()
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@POSIX_ONLY
@pytest.mark.parametrize("closed_descriptor", [1, 2])
def test_spawn_lock_descriptor_avoids_closed_standard_streams(
    tmp_path,
    closed_descriptor,
):
    path = tmp_path / f"activity-{closed_descriptor}.json"
    result_path = tmp_path / f"result-{closed_descriptor}.json"
    project_root = Path(__file__).resolve().parents[1]
    child_script = (
        "import os,sys;"
        "metadata=os.fstat(int(sys.argv[1]));"
        "raise SystemExit(0 if metadata.st_ino == int(sys.argv[2]) else 3)"
    )
    holder_script = textwrap.dedent(
        f"""
        import json
        import os
        import subprocess
        import sys
        from pathlib import Path

        from gamgui.core.activity import ActivityRegistry

        os.close(int(sys.argv[3]))
        path = Path(sys.argv[1])
        result_path = Path(sys.argv[2])
        registry = ActivityRegistry(durable_path=path)
        lease = registry.acquire("app-update")
        payload = json.loads(path.read_text(encoding="utf-8"))
        with registry.subprocess_pass_fds() as pass_fds:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    {child_script!r},
                    str(pass_fds[0]),
                    str(payload["lock_inode"]),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                pass_fds=pass_fds,
            )
            process.communicate(timeout=10)
            result_path.write_text(
                json.dumps(
                    {{
                        "descriptor": pass_fds[0],
                        "returncode": process.returncode,
                    }}
                ),
                encoding="utf-8",
            )
        lease.release()
        """
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            holder_script,
            str(path),
            str(result_path),
            str(closed_descriptor),
        ],
        cwd=project_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )

    assert completed.returncode == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["descriptor"] >= 3
    assert result["returncode"] == 0


@POSIX_ONLY
def test_failed_inheritance_setup_closes_duplicated_descriptor(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "activity.json"
    registry = ActivityRegistry(durable_path=path)
    lease = registry.acquire("app-update")
    duplicated = []

    def fail_inheritable(descriptor, _inheritable):
        duplicated.append(descriptor)
        raise OSError("inheritable setup failed")

    monkeypatch.setattr(activity_module.os, "set_inheritable", fail_inheritable)

    with pytest.raises(
        RuntimeError,
        match="durable administrative activity lock is unavailable",
    ):
        with registry.subprocess_pass_fds():
            pytest.fail("failed descriptor setup yielded to the caller")
    assert len(duplicated) == 1
    with pytest.raises(OSError):
        os.fstat(duplicated[0])
    lease.release()


@POSIX_ONLY
@pytest.mark.parametrize("tamper", ["missing", "group-readable"])
def test_new_durable_lock_tampering_blocks_inheritance_and_stale_recovery(
    tmp_path,
    monkeypatch,
    tamper,
):
    path = tmp_path / "activity.json"
    registry = ActivityRegistry(durable_path=path)
    lease = registry.acquire("app-update")
    process_lock = next(path.parent.glob(f"{path.name}.*.process-lock"))
    if tamper == "missing":
        process_lock.unlink()
    else:
        os.chmod(process_lock, 0o640)
    monkeypatch.setattr(
        activity_module,
        "process_lease_is_dead",
        lambda _pid, _identity: True,
    )

    with pytest.raises(
        RuntimeError,
        match="durable administrative activity lock is unavailable",
    ):
        with registry.subprocess_pass_fds():
            pytest.fail("tampered durable lock yielded a descriptor")
    observer = ActivityRegistry(durable_path=path)
    snapshot = observer.snapshot()

    assert snapshot is not None
    assert snapshot.kind == "app-update"
    assert path.exists()
    lease.release()


@POSIX_ONLY
def test_owner_only_process_lock_replacement_cannot_bypass_record_identity(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "activity.json"
    registry = ActivityRegistry(durable_path=path)
    lease = registry.acquire("app-update")
    payload = json.loads(path.read_text(encoding="utf-8"))
    process_lock = next(path.parent.glob(f"{path.name}.*.process-lock"))
    process_lock.rename(path.parent / "held-original-lock")
    process_lock.write_bytes(b"replacement")
    os.chmod(process_lock, 0o600)
    assert process_lock.stat().st_ino != payload["lock_inode"]
    monkeypatch.setattr(
        activity_module,
        "process_lease_is_dead",
        lambda _pid, _identity: True,
    )

    observer = ActivityRegistry(durable_path=path)
    snapshot = observer.snapshot()

    assert snapshot is not None
    assert snapshot.kind == "app-update"
    assert observer.try_acquire("component-profile-build") is None
    assert path.exists()
    lease.release()


@POSIX_ONLY
def test_durable_release_never_unlinks_a_replacement_token(tmp_path):
    path = tmp_path / "activity.json"
    registry = ActivityRegistry(durable_path=path)
    lease = registry.acquire("app-update")
    original = json.loads(path.read_text(encoding="utf-8"))
    replacement_token = "f" * 32
    original["token"] = replacement_token
    path.write_text(json.dumps(original), encoding="utf-8")
    os.chmod(path, 0o600)

    lease.release()

    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == replacement_token
    assert registry.snapshot() is not None


@POSIX_ONLY
def test_module_registry_resolves_macos_app_data_path_lazily(
    tmp_path,
    monkeypatch,
):
    first = tmp_path / "first-launch"
    second = tmp_path / "canary"
    monkeypatch.setattr(activity_module.sys, "platform", "darwin")
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(first))

    first_lease = activity_module.activity_registry.acquire("app-update")
    assert (first / DURABLE_ACTIVITY_FILENAME).is_file()
    first_lease.release()

    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(second))
    second_lease = activity_module.activity_registry.acquire("app-update")
    assert (second / DURABLE_ACTIVITY_FILENAME).is_file()
    assert not (first / DURABLE_ACTIVITY_FILENAME).exists()
    second_lease.release()
