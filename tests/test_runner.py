from __future__ import annotations

import asyncio
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from keyring.errors import PasswordSetError

from gamgui.core.gam.commands import EXPECTED_GAM_VERSION, GAMCommands
from gamgui.core.gam.errors import GAMError, GAMErrorKind, TokenPersistenceError
from gamgui.core.gam.runner import (
    GAMRunner,
    RunResult,
    locate_gam_binary,
    secure_remove_private_file,
)


def test_windows_bundle_resolves_gam_exe(monkeypatch, tmp_path):
    monkeypatch.setattr("gamgui.core.gam.runner.sys.platform", "win32")
    monkeypatch.setattr("gamgui.core.gam.runner.sys._MEIPASS", str(tmp_path), raising=False)
    assert locate_gam_binary() == tmp_path / "resources" / "gam7" / "gam.exe"


async def test_version(runner):
    assert EXPECTED_GAM_VERSION in await runner.version()


async def test_run_authenticated_reads_users(runner, domain):
    out = await runner.run_authenticated(domain, GAMCommands.print_users())
    assert "alice@example.com" in out


def test_gam_worker_override_is_scoped_to_child_environment(runner, tmp_path, monkeypatch):
    monkeypatch.delenv("GAM_THREADS", raising=False)
    env = runner._build_env(tmp_path, gam_threads=8)
    assert env["GAM_THREADS"] == "8"
    assert "GAM_THREADS" not in os.environ


def test_command_prefix_keeps_user_arguments_separate(vault, tmp_path):
    binary = tmp_path / "interpreter"
    script = tmp_path / "mock gam script"
    binary.write_text("mock", encoding="utf-8")
    runner = GAMRunner(
        vault=vault,
        gam_binary=binary,
        base_dir=tmp_path,
        command_prefix=(str(binary), str(script)),
    )

    assert runner._subprocess_command(["signature", "<div>A & B</div>"]) == [
        str(binary),
        str(script),
        "signature",
        "<div>A & B</div>",
    ]


@pytest.mark.asyncio
async def test_independent_authenticated_reads_use_isolated_configs_concurrently(
    runner,
    domain,
    monkeypatch,
):
    entered = asyncio.Event()
    cfgdirs: list[Path] = []

    async def overlapping_read(_argv, cfgdir, _timeout):
        cfgdirs.append(Path(cfgdir))
        if len(cfgdirs) == 2:
            entered.set()
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        return RunResult(stdout="[]", stderr="", returncode=0)

    monkeypatch.setattr(runner, "_exec", overlapping_read)
    await asyncio.gather(
        runner.run_authenticated(domain, ["read-one"]),
        runner.run_authenticated(domain, ["read-two"]),
    )

    assert len(set(cfgdirs)) == 2
    assert all(not path.exists() for path in cfgdirs)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("notfound", GAMErrorKind.NOT_FOUND),
        ("scope", GAMErrorKind.SCOPE_MISSING),
        ("rate", GAMErrorKind.RATE_LIMITED),
        ("auth", GAMErrorKind.AUTH_EXPIRED),
    ],
)
async def test_error_classification(runner, domain, kind, expected):
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, ["MOCKFAIL", kind])
    assert ei.value.kind == expected
    assert ei.value.remediation  # human guidance present


async def test_missing_binary_raises(vault, tmp_path):
    r = GAMRunner(vault=vault, gam_binary=tmp_path / "does-not-exist", base_dir=tmp_path)
    assert r.binary_exists() is False
    with pytest.raises(RuntimeError):
        await r.version()


async def test_oauth_token_write_back_through_a_real_run(runner, vault, domain, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_REFRESH", "1")
    before = vault.get(domain, "oauth2")
    await runner.run_authenticated(domain, GAMCommands.set_suspended("a@e.com", False), serialize=True)
    after = vault.get(domain, "oauth2")
    assert after != before
    assert "refreshed" in after


@pytest.mark.parametrize("streamed", [False, True], ids=["buffered", "streamed"])
@pytest.mark.parametrize(
    ("returncode", "expected_succeeded", "expected_kind"),
    [
        (0, True, None),
        (7, False, GAMErrorKind.AUTH_EXPIRED),
    ],
    ids=["command-success", "command-failure"],
)
async def test_authenticated_token_persistence_failure_preserves_command_outcome_and_cleanup(
    vault,
    tmp_path,
    domain,
    monkeypatch,
    streamed,
    returncode,
    expected_succeeded,
    expected_kind,
):
    refreshed_token = "refreshed-sensitive-token"
    backend_detail = f"Keychain failure for {domain}: OSStatus -25293 ({refreshed_token})"

    def fail_write_back(_domain: str, _name: str, _value: str) -> None:
        raise PasswordSetError(backend_detail)

    monkeypatch.setattr(vault, "set", fail_write_back)
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )
    script = (
        "import os,sys;"
        "from pathlib import Path;"
        f"Path(os.environ['GAMCFGDIR'],'oauth2.txt').write_text({refreshed_token!r});"
        f"sys.stderr.write('invalid_grant raw-stderr {domain}');"
        f"sys.exit({returncode})"
    )

    with pytest.raises(TokenPersistenceError) as caught:
        if streamed:
            async with runner.run_authenticated_to_file(domain, ["-c", script]):
                pass
        else:
            await runner.run_authenticated(domain, ["-c", script])

    error = caught.value
    assert error.error_code == "GAM-TOKEN-PERSISTENCE"
    assert error.command_succeeded is expected_succeeded
    assert error.gam_error_kind is expected_kind
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "operation was not finalized" in str(error)
    for private_text in (
        domain,
        refreshed_token,
        backend_detail,
        "raw-stderr",
        "OSStatus",
        "-25293",
    ):
        assert private_text not in str(error)
    assert list(tmp_path.glob("gamcfg-*")) == []


@pytest.mark.parametrize("streamed", [False, True], ids=["buffered", "streamed"])
async def test_token_persistence_failure_never_masks_runner_exception(
    vault,
    tmp_path,
    domain,
    monkeypatch,
    streamed,
):
    def fail_write_back(_domain: str, _name: str, _value: str) -> None:
        raise PasswordSetError("private backend failure: OSStatus -25293")

    monkeypatch.setattr(vault, "set", fail_write_back)
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )

    async def buffered_failure(_argv, cfgdir, _timeout, **_kwargs):
        (cfgdir / "oauth2.txt").write_text("refreshed-sensitive-token", encoding="utf-8")
        raise TypeError("runner programming defect")

    async def streamed_failure(_argv, cfgdir, _timeout, _stdout_path, **_kwargs):
        (cfgdir / "oauth2.txt").write_text("refreshed-sensitive-token", encoding="utf-8")
        raise TypeError("runner programming defect")

    monkeypatch.setattr(
        runner,
        "_exec_to_file" if streamed else "_exec",
        streamed_failure if streamed else buffered_failure,
    )

    with pytest.raises(TypeError, match="runner programming defect"):
        if streamed:
            async with runner.run_authenticated_to_file(domain, ["ignored"]):
                pytest.fail("a failed command must not reach its consumer")
        else:
            await runner.run_authenticated(domain, ["ignored"])

    assert list(tmp_path.glob("gamcfg-*")) == []


def test_strip_cfgdir_noise_removes_gam_init_banner():
    from pathlib import Path

    from gamgui.core.gam.runner import strip_cfgdir_noise

    cfg = Path("/var/run/gamcfg-xyz")
    out = (
        f"Created: {cfg}/gamcache\n"
        f"Config File: {cfg}/gam.cfg, Initialized\n"
        "User: x@e.com, Vacation:\n  Enabled: True\n"
    )
    cleaned = strip_cfgdir_noise(out, cfg)
    assert "gamcache" not in cleaned and "Initialized" not in cleaned
    assert cleaned == "User: x@e.com, Vacation:\n  Enabled: True"  # only the real data survives


def test_strip_cfgdir_noise_keeps_unrelated_output():
    from pathlib import Path

    from gamgui.core.gam.runner import strip_cfgdir_noise

    out = "primaryEmail\na@e.com\nb@e.com"
    assert strip_cfgdir_noise(out, Path("/var/run/gamcfg-xyz")) == out  # untouched when dir not present


async def test_authenticated_output_can_be_securely_spooled(vault, tmp_path, domain):
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )
    script = (
        "import os,sys;"
        "print('Created: '+os.environ['GAMCFGDIR']+'/gamcache');"
        "sys.stdout.write('x'*2000000)"
    )
    async with runner.run_authenticated_to_file(domain, ["-c", script]) as result:
        spool_path = result.path
        assert spool_path.exists()
        assert result.stdout_bytes == 2_000_000
        assert spool_path.stat().st_size == 2_000_000
        if os.name == "posix":
            assert stat.S_IMODE(spool_path.stat().st_mode) & 0o077 == 0
    assert not spool_path.exists()
    assert list(tmp_path.glob("gamcfg-*")) == []


async def test_spooled_read_can_accept_not_found_and_preserve_sparse_output(
    vault,
    tmp_path,
    domain,
):
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )
    script = (
        "import sys;"
        "print('id,JSON,JSON-aliases');"
        "print('123,{},[]');"
        "sys.stderr.write('Course: d:Section_missing, Does not exist');"
        "sys.exit(56)"
    )

    async with runner.run_authenticated_to_file(
        domain,
        ["-c", script],
        accepted_error_kinds=(GAMErrorKind.NOT_FOUND,),
    ) as result:
        spool_path = result.path
        assert "123,{},[]" in spool_path.read_text(encoding="utf-8")
        assert result.accepted_error_kind is GAMErrorKind.NOT_FOUND

    assert not spool_path.exists()
    assert list(tmp_path.glob("gamcfg-*")) == []


async def test_spool_is_removed_when_consumer_fails(vault, tmp_path, domain):
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )
    with pytest.raises(RuntimeError, match="consumer failed"):
        async with runner.run_authenticated_to_file(
            domain, ["-c", "print('private output')"]
        ) as result:
            spool_path = result.path
            raise RuntimeError("consumer failed")
    assert not spool_path.exists()
    assert list(tmp_path.glob("gamcfg-*")) == []


def test_secure_private_remove_retries_transient_unlink_failure(
    tmp_path,
    monkeypatch,
):
    private = tmp_path / "private.tmp"
    private.write_bytes(b"sensitive")
    original_unlink = Path.unlink
    calls = 0

    def transient(self, *args, **kwargs):
        nonlocal calls
        if self == private and calls < 2:
            calls += 1
            raise PermissionError("locked")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", transient)

    assert secure_remove_private_file(private)
    assert calls == 2
    assert not private.exists()


def test_secure_private_remove_reports_persistent_lock(tmp_path, monkeypatch):
    private = tmp_path / "private.tmp"
    private.write_bytes(b"sensitive")
    original_unlink = Path.unlink

    def locked(self, *args, **kwargs):
        if self == private:
            raise PermissionError("locked")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)

    assert not secure_remove_private_file(private, attempts=2)
    assert private.exists()


async def test_spool_cleanup_failure_never_masks_consumer_failure(
    vault,
    tmp_path,
    domain,
    monkeypatch,
):
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )

    async def cleanup_failed(_path):
        return False

    monkeypatch.setattr(
        "gamgui.core.gam.runner.await_secure_remove_private_file",
        cleanup_failed,
    )

    with pytest.raises(RuntimeError, match="consumer failed"):
        async with runner.run_authenticated_to_file(
            domain,
            ["-c", "print('private output')"],
        ):
            raise RuntimeError("consumer failed")


async def test_spool_cleanup_failure_surfaces_after_success(
    vault,
    tmp_path,
    domain,
    monkeypatch,
):
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )

    async def cleanup_failed(_path):
        return False

    monkeypatch.setattr(
        "gamgui.core.gam.runner.await_secure_remove_private_file",
        cleanup_failed,
    )

    with pytest.raises(RuntimeError, match="could not be removed securely"):
        async with runner.run_authenticated_to_file(
            domain,
            ["-c", "print('private output')"],
        ):
            pass


async def test_spool_cleanup_failure_never_masks_command_failure(
    vault,
    tmp_path,
    domain,
    monkeypatch,
):
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )

    async def cleanup_failed(_path):
        return False

    monkeypatch.setattr(
        "gamgui.core.gam.runner.await_secure_remove_private_file",
        cleanup_failed,
    )

    with pytest.raises(GAMError) as caught:
        async with runner.run_authenticated_to_file(
            domain,
            ["-c", "import sys; sys.stderr.write('invalid_grant'); sys.exit(7)"],
        ):
            pytest.fail("a failed command must not reach its consumer")

    assert caught.value.kind is GAMErrorKind.AUTH_EXPIRED


async def test_cancelling_spooled_command_stops_process_and_removes_files(
    vault, tmp_path, domain
):
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=30,
    )

    async def run_slow_export():
        async with runner.run_authenticated_to_file(
            domain,
            ["-c", "import sys,time; print('private', flush=True); time.sleep(30)"],
        ):
            raise AssertionError("cancelled export must not reach its consumer")

    task = asyncio.create_task(run_slow_export())
    for _ in range(100):
        if list(tmp_path.glob("gamcfg-*/gam-output-*.tmp")):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("spool file was not created")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.glob("gamcfg-*")) == []


async def test_cancelling_buffered_command_kills_and_reaps_process(
    vault,
    tmp_path,
    monkeypatch,
):
    started = asyncio.Event()

    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    process = Process()

    async def create_process(*_args, **_kwargs):
        return process

    binary = tmp_path / "gam"
    binary.write_text("mock", encoding="utf-8")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    runner = GAMRunner(vault=vault, gam_binary=binary, base_dir=tmp_path)
    cfgdir = tmp_path / "cfg"
    task = asyncio.create_task(runner._exec(["update", "user"], cfgdir, 30))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.waited and process.returncode == -9


@pytest.mark.skipif(
    os.name != "posix",
    reason="pass_fds is a POSIX subprocess contract",
)
async def test_runner_inherits_active_durable_descriptor_for_all_spawn_paths(
    vault,
    tmp_path,
    monkeypatch,
):
    class Registry:
        def __init__(self):
            self.calls = 0

        @contextmanager
        def subprocess_pass_fds(self):
            self.calls += 1
            yield (91,)

    class Process:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    spawned = []

    async def create_process(*args, **kwargs):
        spawned.append((args, kwargs))
        return Process()

    binary = tmp_path / "gam"
    binary.write_text("mock", encoding="utf-8")
    registry = Registry()
    runner = GAMRunner(
        vault=vault,
        gam_binary=binary,
        base_dir=tmp_path,
        activity_registry=registry,
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    await runner._exec(["version"], tmp_path, 15)
    await runner._exec_to_file(
        ["print", "users"],
        tmp_path,
        15,
        tmp_path / "stdout.tmp",
    )

    assert registry.calls == 2
    assert len(spawned) == 2
    for _args, options in spawned:
        assert options["close_fds"] is True
        assert options["pass_fds"] == (91,)


async def test_runner_does_not_add_posix_spawn_options_without_durable_lease(
    vault,
    tmp_path,
    monkeypatch,
):
    class Registry:
        @contextmanager
        def subprocess_pass_fds(self):
            yield ()

    class Process:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    spawned = []

    async def create_process(*args, **kwargs):
        spawned.append(kwargs)
        return Process()

    binary = tmp_path / "gam"
    binary.write_text("mock", encoding="utf-8")
    runner = GAMRunner(
        vault=vault,
        gam_binary=binary,
        base_dir=tmp_path,
        activity_registry=Registry(),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    await runner._exec(["version"], tmp_path, 15)

    assert len(spawned) == 1
    assert "pass_fds" not in spawned[0]


async def test_runner_refuses_spawn_when_durable_descriptor_validation_fails(
    vault,
    tmp_path,
    monkeypatch,
):
    class Registry:
        @contextmanager
        def subprocess_pass_fds(self):
            raise RuntimeError("durable lock unavailable")
            yield ()

    called = False

    async def create_process(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("unprotected GAM must not be spawned")

    binary = tmp_path / "gam"
    binary.write_text("mock", encoding="utf-8")
    runner = GAMRunner(
        vault=vault,
        gam_binary=binary,
        base_dir=tmp_path,
        activity_registry=Registry(),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(RuntimeError, match="durable lock unavailable"):
        await runner._exec(["version"], tmp_path, 15)
    assert not called


async def test_exec_to_file_sets_gam_threads_for_child(vault, tmp_path):
    """The spool path must honour per-command concurrency, not gam.cfg's global default."""
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )
    script = "import os, sys; sys.stdout.write(os.environ.get('GAM_THREADS', 'unset'))"
    spool = tmp_path / "threads.tmp"

    result = await runner._exec_to_file(
        ["-c", script],
        tmp_path,
        15.0,
        spool,
        gam_threads=20,
    )

    assert result.returncode == 0
    assert spool.read_text(encoding="utf-8").strip() == "20"


async def test_exec_to_file_streams_stderr_lines_to_callback(vault, tmp_path):
    """Progress must be observable while stdout still goes straight to the spool file."""
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )
    script = (
        "import sys\n"
        "print('payload-line')\n"
        "for n in (1, 2, 3):\n"
        "    print('Getting participants (%d/3)' % n, file=sys.stderr, flush=True)\n"
    )
    spool = tmp_path / "streamed.tmp"
    seen: list[tuple[str, str]] = []

    async def observe(stream_name: str, line: str) -> None:
        seen.append((stream_name, line))

    result = await runner._exec_to_file(
        ["-c", script],
        tmp_path,
        15.0,
        spool,
        line_callback=observe,
    )

    assert result.returncode == 0
    # stdout is untouched by the callback path.
    assert spool.read_text(encoding="utf-8").strip() == "payload-line"
    assert {name for name, _ in seen} == {"stderr"}
    assert [line for _, line in seen] == [
        "Getting participants (1/3)",
        "Getting participants (2/3)",
        "Getting participants (3/3)",
    ]


async def test_exec_to_file_without_callback_keeps_buffered_stderr(vault, tmp_path):
    """Opting out must leave the original buffered behaviour exactly as it was."""
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )
    script = (
        "import sys\n"
        "print('first', file=sys.stderr)\n"
        "print('second', file=sys.stderr)\n"
    )
    spool = tmp_path / "buffered.tmp"

    result = await runner._exec_to_file(
        ["-c", script],
        tmp_path,
        15.0,
        spool,
    )

    assert result.returncode == 0
    assert result.stderr.splitlines() == ["first", "second"]
