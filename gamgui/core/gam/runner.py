"""The only place that spawns the ``gam`` binary.

Everything else goes through :class:`GAMRunner`, which handles binary location, the ephemeral
``GAMCFGDIR`` materialization, environment, timeouts, and error mapping. Output parsing lives in
``parser.py``; callers receive raw stdout and parse it.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional, Sequence

from ..activity import ActivityRegistry
from ..activity import activity_registry as global_activity_registry
from ..secrets.ephemeral import EphemeralConfig
from ..secrets.vault import SecretsVault
from .errors import GAMError, GAMErrorKind, TokenPersistenceError

# Env var that overrides binary discovery (used by tests with a mock gam, and power users).
GAM_BINARY_ENV = "GAMGUI_GAM_BINARY"

DEFAULT_TIMEOUT = 120.0
STREAM_DIAGNOSTIC_TAIL_LINES = 200
STREAM_DIAGNOSTIC_LINE_CHARS = 8192
STREAM_PROCESS_STOP_TIMEOUT = 5.0
STREAM_PIPE_DRAIN_TIMEOUT = 2.0

StreamingLineCallback = Callable[[str, str], Optional[Awaitable[None]]]


def _consume_future_exception(future: asyncio.Future) -> None:
    try:
        future.exception()
    except BaseException:
        pass


def strip_cfgdir_noise(stdout: str, cfgdir: Path) -> str:
    """Drop GAM's config-init banner from stdout.

    Because we hand GAM a brand-new ``GAMCFGDIR`` per call, it prints lines like
    ``Created: <dir>/gamcache`` and ``Config File: <dir>/gam.cfg, Initialized`` on stdout every time.
    Those reference our ephemeral dir and never appear in real data, so any line mentioning the dir
    is safe to remove — otherwise they leak into text parsers (e.g. the vacation message).
    """
    needle = str(cfgdir)
    if not needle or needle not in stdout:
        return stdout
    return "\n".join(line for line in stdout.splitlines() if needle not in line)


@dataclass
class RunResult:
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class SpooledRunResult:
    path: Path
    stdout_bytes: int
    accepted_error_kind: Optional[GAMErrorKind] = None


@dataclass(frozen=True)
class StreamingRunResult:
    """Bounded result metadata for a subprocess whose output was drained live."""

    returncode: int
    stdout_tail: tuple[str, ...]
    stderr_tail: tuple[str, ...]
    duration_seconds: float


class StreamingGAMError(GAMError):
    """A GAM failure that retains the bounded streaming process result."""

    def __init__(self, cause: GAMError, result: StreamingRunResult) -> None:
        self.result = result
        super().__init__(
            kind=cause.kind,
            exit_code=cause.exit_code,
            stderr=cause.stderr,
            argv=cause.argv,
        )


def _strip_cfgdir_noise_file(path: Path, cfgdir: Path) -> None:
    """Streaming file equivalent of :func:`strip_cfgdir_noise`."""
    needle = str(cfgdir).encode("utf-8")
    fd, clean_name = tempfile.mkstemp(prefix="gam-output-clean-", dir=str(path.parent))
    clean_path = Path(clean_name)
    os.chmod(clean_path, 0o600)
    failed = False
    try:
        with os.fdopen(fd, "wb") as target, path.open("rb") as source:
            for line in source:
                if needle not in line:
                    target.write(line)
        os.replace(clean_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        failed = True
        raise
    finally:
        cleaned = secure_remove_private_file(clean_path)
        if not cleaned and not failed:
            raise RuntimeError("Private GAM temporary output could not be removed.")


def secure_remove_private_file(path: Path, *, attempts: int = 3) -> bool:
    """Overwrite and unlink one known private file with bounded retries."""

    target = Path(path)
    for attempt in range(max(1, min(int(attempts), 5))):
        try:
            target.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            pass
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        try:
            if not target.is_symlink():
                size = target.stat().st_size
                with target.open("r+b", buffering=0) as stream:
                    zeroes = b"\x00" * min(max(size, 1), 1024 * 1024)
                    remaining = size
                    while remaining > 0:
                        amount = min(remaining, len(zeroes))
                        stream.write(zeroes[:amount])
                        remaining -= amount
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError:
            pass
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt + 1 < attempts:
                time.sleep(0.02 * (attempt + 1))
    return not target.exists()


async def await_secure_remove_private_file(path: Path) -> bool:
    """Wait for cleanup to finish even when the caller receives cancellation."""

    worker = asyncio.create_task(
        asyncio.to_thread(secure_remove_private_file, path)
    )
    cancellation: Optional[asyncio.CancelledError] = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            cancellation = exc
    cleaned = worker.result()
    if cancellation is not None:
        raise cancellation
    return cleaned


def locate_gam_binary() -> Path:
    """Resolve the bundled ``gam`` executable.

    Order: explicit env override → PyInstaller bundle (``sys._MEIPASS``) → repo source tree.
    """
    override = os.environ.get(GAM_BINARY_ENV)
    if override:
        return Path(override)

    executable_name = "gam.exe" if sys.platform == "win32" else "gam"
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:  # frozen .app
        return Path(meipass) / "resources" / "gam7" / executable_name

    # Source tree: gamgui/core/gam/runner.py -> gamgui/resources/gam7/gam
    return Path(__file__).resolve().parents[2] / "resources" / "gam7" / executable_name


class GAMRunner:
    def __init__(
        self,
        vault: SecretsVault,
        gam_binary: Optional[Path] = None,
        base_dir: Optional[Path] = None,
        timeout: float = DEFAULT_TIMEOUT,
        activity_registry: Optional[ActivityRegistry] = None,
        command_prefix: Optional[Sequence[str]] = None,
    ) -> None:
        self.vault = vault
        self.gam_binary = Path(gam_binary) if gam_binary else locate_gam_binary()
        self.base_dir = base_dir
        self.timeout = timeout
        self.activity_registry = (
            activity_registry
            if activity_registry is not None
            else global_activity_registry
        )
        self._command_prefix = (
            tuple(str(part) for part in command_prefix)
            if command_prefix is not None
            else (str(self.gam_binary),)
        )
        if not self._command_prefix or Path(self._command_prefix[0]) != self.gam_binary:
            raise ValueError("The GAM command prefix must begin with the checked GAM binary.")
        # Serializes mutating calls so two writes can't race the same ephemeral GAMCFGDIR.
        self._write_lock = asyncio.Lock()
        # Independent read processes use isolated GAMCFGDIRs. Only refreshed OAuth
        # token persistence is serialized so one read cannot overwrite another's
        # newer token while both exports still run concurrently.
        self._token_persistence_lock = asyncio.Lock()

    def binary_exists(self) -> bool:
        return self.gam_binary.exists()

    def _require_binary(self) -> None:
        if not self.binary_exists():
            raise RuntimeError(
                f"GAM binary not found at {self.gam_binary}. Run scripts/fetch_gam.sh to vendor it."
            )

    def _build_env(self, cfgdir: Path, *, gam_threads: Optional[int] = None) -> dict:
        env = os.environ.copy()
        env["GAMCFGDIR"] = str(cfgdir)
        # Keep GAM quiet/non-interactive where possible.
        env.setdefault("GAM_NO_UPDATE_CHECK", "1")
        if gam_threads is not None:
            env["GAM_THREADS"] = str(max(1, min(int(gam_threads), 1000)))
        return env

    @asynccontextmanager
    async def _authenticated_config(
        self,
        config: EphemeralConfig,
    ) -> AsyncIterator[Path]:
        cfgdir = config.__enter__()
        try:
            yield cfgdir
        except BaseException as exc:
            async with self._token_persistence_lock:
                config.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            async with self._token_persistence_lock:
                config.__exit__(None, None, None)

    def _subprocess_options(
        self,
        pass_fds: tuple[int, ...],
    ) -> dict[str, object]:
        if not pass_fds:
            return {}
        if os.name != "posix":
            raise RuntimeError(
                "Durable administrative activity descriptors cannot be "
                "inherited on this platform."
            )
        return {
            "close_fds": True,
            "pass_fds": pass_fds,
        }

    def _streaming_subprocess_options(
        self,
        pass_fds: tuple[int, ...],
    ) -> dict[str, object]:
        options = self._subprocess_options(pass_fds)
        if os.name == "posix":
            # A separate session lets timeout cleanup terminate GAM's worker pool as one group.
            options["start_new_session"] = True
        return options

    def _subprocess_command(self, argv: Sequence[str]) -> list[str]:
        """Return a directly executable command for this platform.

        A prefix is used only by controlled test harnesses that launch the
        cross-platform mock through an interpreter. User-controlled GAM
        arguments are still passed separately and are never shell-parsed.
        """

        return [*self._command_prefix, *argv]

    async def _exec(
        self,
        argv: Sequence[str],
        cfgdir: Path,
        timeout: float,
        *,
        gam_threads: Optional[int] = None,
    ) -> RunResult:
        self._require_binary()
        with self.activity_registry.subprocess_pass_fds() as pass_fds:
            proc = await asyncio.create_subprocess_exec(
                *self._subprocess_command(argv),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(cfgdir, gam_threads=gam_threads),
                **self._subprocess_options(pass_fds),
            )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise GAMError(GAMErrorKind.TIMEOUT, exit_code=None, stderr="command timed out", argv=list(argv))
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        return RunResult(
            stdout=(out or b"").decode("utf-8", "replace"),
            stderr=(err or b"").decode("utf-8", "replace"),
            returncode=proc.returncode if proc.returncode is not None else -1,
        )

    async def _exec_to_file(
        self,
        argv: Sequence[str],
        cfgdir: Path,
        timeout: float,
        stdout_path: Path,
    ) -> RunResult:
        self._require_binary()
        with stdout_path.open("wb", buffering=0) as stdout_file:
            with self.activity_registry.subprocess_pass_fds() as pass_fds:
                proc = await asyncio.create_subprocess_exec(
                    *self._subprocess_command(argv),
                    stdout=stdout_file,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._build_env(cfgdir),
                    **self._subprocess_options(pass_fds),
                )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise GAMError(
                    GAMErrorKind.TIMEOUT,
                    exit_code=None,
                    stderr="command timed out",
                    argv=list(argv),
                )
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                raise
        return RunResult(
            stdout="",
            stderr=(err or b"").decode("utf-8", "replace"),
            returncode=proc.returncode if proc.returncode is not None else -1,
        )

    async def _exec_streaming(
        self,
        argv: Sequence[str],
        cfgdir: Path,
        timeout: float,
        *,
        gam_threads: Optional[int] = None,
        line_callback: Optional[StreamingLineCallback] = None,
        tail_lines: int = STREAM_DIAGNOSTIC_TAIL_LINES,
    ) -> StreamingRunResult:
        """Drain both process pipes incrementally while retaining bounded tails."""

        self._require_binary()
        retained_lines = max(1, min(int(tail_lines), 1000))
        stdout_tail: deque[str] = deque(maxlen=retained_lines)
        stderr_tail: deque[str] = deque(maxlen=retained_lines)
        started = time.perf_counter()
        with self.activity_registry.subprocess_pass_fds() as pass_fds:
            proc = await asyncio.create_subprocess_exec(
                *self._subprocess_command(argv),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(cfgdir, gam_threads=gam_threads),
                **self._streaming_subprocess_options(pass_fds),
            )

        async def drain(
            reader: Optional[asyncio.StreamReader],
            stream_name: str,
            tail: deque[str],
        ) -> None:
            if reader is None:
                return
            cfgdir_text = str(cfgdir)
            while raw_line := await reader.readline():
                line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                if cfgdir_text and cfgdir_text in line:
                    continue
                bounded = line[-STREAM_DIAGNOSTIC_LINE_CHARS:]
                tail.append(bounded)
                if line_callback is not None:
                    callback_result = line_callback(stream_name, bounded)
                    if inspect.isawaitable(callback_result):
                        await callback_result

        drain_tasks = (
            asyncio.create_task(drain(proc.stdout, "stdout", stdout_tail)),
            asyncio.create_task(drain(proc.stderr, "stderr", stderr_tail)),
        )

        async def stop_windows_process_tree() -> None:
            system_root = os.environ.get("SystemRoot", "")
            taskkill = (
                str(Path(system_root) / "System32" / "taskkill.exe")
                if system_root
                else "taskkill.exe"
            )
            try:
                killer = await asyncio.create_subprocess_exec(
                    taskkill,
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except OSError:
                return
            try:
                await asyncio.wait_for(
                    killer.wait(),
                    timeout=STREAM_PROCESS_STOP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                try:
                    killer.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(killer.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

        async def stop_process() -> None:
            if proc.returncode is None:
                if os.name == "nt":
                    # GAM batch uses multiprocessing; kill its descendants while the parent PID
                    # still anchors the Windows process tree.
                    await stop_windows_process_tree()
                elif os.name == "posix":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

            try:
                await asyncio.wait_for(
                    asyncio.shield(proc.wait()),
                    timeout=STREAM_PROCESS_STOP_TIMEOUT,
                )
                return
            except asyncio.TimeoutError:
                pass

            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(
                    asyncio.shield(proc.wait()),
                    timeout=STREAM_PROCESS_STOP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                pass

        async def finish_pipe_drains() -> None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(completion),
                    timeout=STREAM_PIPE_DRAIN_TIMEOUT,
                )
                return
            except BaseException:
                completion.cancel()
                for task in drain_tasks:
                    task.cancel()
            _, pending = await asyncio.wait(
                (completion, *drain_tasks),
                timeout=STREAM_PIPE_DRAIN_TIMEOUT,
            )
            for task in pending:
                task.add_done_callback(_consume_future_exception)

        async def cleanup_after_cancellation() -> None:
            async def cleanup_process() -> None:
                await stop_process()
                await finish_pipe_drains()

            cleanup = asyncio.create_task(cleanup_process())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()

        completion = asyncio.gather(*drain_tasks, proc.wait())
        try:
            # Shield the pipe drains so timeout handling can kill the process and then consume
            # everything it already emitted instead of cancelling readers before the child exits.
            await asyncio.wait_for(asyncio.shield(completion), timeout=timeout)
        except asyncio.TimeoutError:
            await stop_process()
            await finish_pipe_drains()
            detail = "\n".join(stderr_tail) or "command timed out"
            raise GAMError(
                GAMErrorKind.TIMEOUT,
                exit_code=None,
                stderr=detail,
                argv=list(argv),
            ) from None
        except asyncio.CancelledError:
            await cleanup_after_cancellation()
            raise
        except BaseException:
            await stop_process()
            await finish_pipe_drains()
            raise

        return StreamingRunResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout_tail=tuple(stdout_tail),
            stderr_tail=tuple(stderr_tail),
            duration_seconds=time.perf_counter() - started,
        )

    async def run_authenticated(
        self,
        domain: str,
        argv: Sequence[str],
        timeout: Optional[float] = None,
        serialize: bool = False,
        gam_threads: Optional[int] = None,
    ) -> str:
        """Run a gam command as ``domain`` (credentials materialized from the vault).

        Returns stdout on success; raises :class:`GAMError` on failure.
        Set ``serialize=True`` for mutating commands.
        """
        argv = list(argv)
        timeout = timeout or self.timeout

        async def _do() -> str:
            result: Optional[RunResult] = None
            token_persistence_failed = False
            config = EphemeralConfig(self.vault, domain, base_dir=self.base_dir)
            try:
                async with self._authenticated_config(config) as cfgdir:
                    if gam_threads is None:
                        result = await self._exec(argv, cfgdir, timeout)
                    else:
                        result = await self._exec(
                            argv,
                            cfgdir,
                            timeout,
                            gam_threads=gam_threads,
                        )
            except TokenPersistenceError:
                if not config.token_persistence_error_raised:
                    raise
                token_persistence_failed = True
            if result is None:
                raise RuntimeError("GAM did not return a command result.")
            if token_persistence_failed:
                gam_error_kind = (
                    None
                    if result.returncode == 0
                    else GAMError.from_run(result.returncode, result.stderr, argv).kind
                )
                raise TokenPersistenceError(
                    command_succeeded=result.returncode == 0,
                    gam_error_kind=gam_error_kind,
                ) from None
            if result.returncode != 0:
                raise GAMError.from_run(result.returncode, result.stderr, argv)
            return strip_cfgdir_noise(result.stdout, cfgdir)

        if serialize:
            async with self._write_lock:
                return await _do()
        return await _do()

    async def run_authenticated_streaming(
        self,
        domain: str,
        argv: Sequence[str],
        *,
        timeout: float,
        serialize: bool = False,
        gam_threads: Optional[int] = None,
        line_callback: Optional[StreamingLineCallback] = None,
        tail_lines: int = STREAM_DIAGNOSTIC_TAIL_LINES,
    ) -> StreamingRunResult:
        """Run authenticated GAM while draining stdout and stderr without buffering them.

        Each decoded line is offered to ``line_callback`` on the event loop. Only bounded
        diagnostic tails survive process completion. Non-zero GAM exits raise
        :class:`StreamingGAMError`, a :class:`GAMError` subtype carrying that bounded result.
        """

        command = list(argv)
        command_timeout = float(timeout)
        if not math.isfinite(command_timeout) or command_timeout <= 0:
            raise ValueError("streaming GAM timeout must be a positive finite value")

        async def _do() -> StreamingRunResult:
            result: Optional[StreamingRunResult] = None
            token_persistence_failed = False
            config = EphemeralConfig(self.vault, domain, base_dir=self.base_dir)
            try:
                async with self._authenticated_config(config) as cfgdir:
                    result = await self._exec_streaming(
                        command,
                        cfgdir,
                        command_timeout,
                        gam_threads=gam_threads,
                        line_callback=line_callback,
                        tail_lines=tail_lines,
                    )
            except TokenPersistenceError:
                if not config.token_persistence_error_raised:
                    raise
                token_persistence_failed = True
            if result is None:
                raise RuntimeError("GAM did not return a command result.")
            error_text = "\n".join(result.stderr_tail)
            if token_persistence_failed:
                gam_error_kind = (
                    None
                    if result.returncode == 0
                    else GAMError.from_run(
                        result.returncode,
                        error_text,
                        command,
                    ).kind
                )
                raise TokenPersistenceError(
                    command_succeeded=result.returncode == 0,
                    gam_error_kind=gam_error_kind,
                ) from None
            if result.returncode != 0:
                cause = GAMError.from_run(
                    result.returncode,
                    error_text,
                    command,
                )
                raise StreamingGAMError(cause, result)
            return result

        if serialize:
            async with self._write_lock:
                return await _do()
        return await _do()

    @asynccontextmanager
    async def run_authenticated_to_file(
        self,
        domain: str,
        argv: Sequence[str],
        timeout: Optional[float] = None,
        serialize: bool = False,
        accepted_error_kinds: Sequence[GAMErrorKind] = (),
    ) -> AsyncIterator[SpooledRunResult]:
        """Stream authenticated GAM stdout to a private ``0600`` file.

        The file exists only inside the context and is overwritten then removed on success,
        command failure, consumer failure, or cancellation.  This path is for large exports that
        should not be retained as a bytes object and decoded on the event loop.
        """
        command = list(argv)
        command_timeout = timeout or self.timeout
        accepted_kinds = frozenset(accepted_error_kinds)
        if any(not isinstance(kind, GAMErrorKind) for kind in accepted_kinds):
            raise ValueError("accepted_error_kinds must contain GAMErrorKind values")

        @asynccontextmanager
        async def _do() -> AsyncIterator[SpooledRunResult]:
            result: Optional[RunResult] = None
            token_persistence_failed = False
            accepted_command_error: Optional[GAMError] = None
            config = EphemeralConfig(self.vault, domain, base_dir=self.base_dir)
            try:
                async with self._authenticated_config(config) as cfgdir:
                    fd, raw_path = tempfile.mkstemp(
                        prefix="gam-output-", suffix=".tmp", dir=str(cfgdir)
                    )
                    os.close(fd)
                    spool_path = Path(raw_path)
                    os.chmod(spool_path, 0o600)
                    failed = False
                    try:
                        result = await self._exec_to_file(
                            command, cfgdir, command_timeout, spool_path
                        )
                        command_error = (
                            GAMError.from_run(
                                result.returncode,
                                result.stderr,
                                command,
                            )
                            if result.returncode != 0
                            else None
                        )
                        if (
                            command_error is not None
                            and command_error.kind in accepted_kinds
                        ):
                            accepted_command_error = command_error
                        # Preserve the command failure if private-file cleanup also fails. The
                        # enclosing EphemeralConfig still wipes the whole private directory.
                        failed = (
                            command_error is not None
                            and accepted_command_error is None
                        )
                        if command_error is None or accepted_command_error is not None:
                            await asyncio.to_thread(
                                _strip_cfgdir_noise_file, spool_path, cfgdir
                            )
                            yield SpooledRunResult(
                                path=spool_path,
                                stdout_bytes=spool_path.stat().st_size,
                                accepted_error_kind=(
                                    accepted_command_error.kind
                                    if accepted_command_error is not None
                                    else None
                                ),
                            )
                    except BaseException:
                        failed = True
                        raise
                    finally:
                        cleaned = await await_secure_remove_private_file(spool_path)
                        if not cleaned and not failed:
                            raise RuntimeError(
                                "Private GAM output could not be removed securely."
                            )
            except TokenPersistenceError:
                if not config.token_persistence_error_raised:
                    raise
                token_persistence_failed = True
            if result is None:
                raise RuntimeError("GAM did not return a command result.")
            if token_persistence_failed:
                gam_error_kind = (
                    None
                    if result.returncode == 0
                    else GAMError.from_run(
                        result.returncode, result.stderr, command
                    ).kind
                )
                raise TokenPersistenceError(
                    command_succeeded=result.returncode == 0,
                    gam_error_kind=gam_error_kind,
                ) from None
            if result.returncode != 0 and accepted_command_error is None:
                raise GAMError.from_run(result.returncode, result.stderr, command)

        if serialize:
            async with self._write_lock:
                async with _do() as result:
                    yield result
            return
        async with _do() as result:
            yield result

    async def run_in_cfgdir(
        self,
        cfgdir: Path,
        argv: Sequence[str],
        timeout: Optional[float] = None,
    ) -> RunResult:
        """Run a gam command against an explicit, persistent ``GAMCFGDIR``.

        Used by the setup wizard, where credentials don't exist in the vault yet and the files GAM
        creates must persist long enough to be harvested. Returns the raw :class:`RunResult` (the
        wizard inspects exit code + output itself).
        """
        return await self._exec(list(argv), Path(cfgdir), timeout or self.timeout)

    async def version(self) -> str:
        """Return GAM's reported version (no credentials needed)."""
        from ..secrets.ephemeral import app_runtime_dir
        from .commands import GAMCommands

        cfgdir = self.base_dir or app_runtime_dir()
        res = await self._exec(GAMCommands.version(), Path(cfgdir), self.timeout)
        return res.stdout.strip()
