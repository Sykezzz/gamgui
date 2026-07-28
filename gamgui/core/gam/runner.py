"""The only place that spawns the ``gam`` binary.

Everything else goes through :class:`GAMRunner`, which handles binary location, the ephemeral
``GAMCFGDIR`` materialization, environment, timeouts, and error mapping. Output parsing lives in
``parser.py``; callers receive raw stdout and parse it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional, Sequence

from ..activity import ActivityRegistry
from ..activity import activity_registry as global_activity_registry
from ..secrets.ephemeral import EphemeralConfig
from ..secrets.vault import SecretsVault
from .errors import GAMError, GAMErrorKind, TokenPersistenceError

# Env var that overrides binary discovery (used by tests with a mock gam, and power users).
GAM_BINARY_ENV = "GAMGUI_GAM_BINARY"

DEFAULT_TIMEOUT = 120.0


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

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:  # frozen .app
        return Path(meipass) / "resources" / "gam7" / "gam"

    # Source tree: gamgui/core/gam/runner.py -> gamgui/resources/gam7/gam
    return Path(__file__).resolve().parents[2] / "resources" / "gam7" / "gam"


class GAMRunner:
    def __init__(
        self,
        vault: SecretsVault,
        gam_binary: Optional[Path] = None,
        base_dir: Optional[Path] = None,
        timeout: float = DEFAULT_TIMEOUT,
        activity_registry: Optional[ActivityRegistry] = None,
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
        # Serializes mutating calls so two writes can't race the same ephemeral GAMCFGDIR.
        self._write_lock = asyncio.Lock()

    def binary_exists(self) -> bool:
        return self.gam_binary.exists()

    def _require_binary(self) -> None:
        if not self.binary_exists():
            raise RuntimeError(
                f"GAM binary not found at {self.gam_binary}. Run scripts/fetch_gam.sh to vendor it."
            )

    def _build_env(self, cfgdir: Path) -> dict:
        env = os.environ.copy()
        env["GAMCFGDIR"] = str(cfgdir)
        # Keep GAM quiet/non-interactive where possible.
        env.setdefault("GAM_NO_UPDATE_CHECK", "1")
        return env

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

    async def _exec(self, argv: Sequence[str], cfgdir: Path, timeout: float) -> RunResult:
        self._require_binary()
        with self.activity_registry.subprocess_pass_fds() as pass_fds:
            proc = await asyncio.create_subprocess_exec(
                str(self.gam_binary),
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(cfgdir),
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
                    str(self.gam_binary),
                    *argv,
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

    async def run_authenticated(
        self,
        domain: str,
        argv: Sequence[str],
        timeout: Optional[float] = None,
        serialize: bool = False,
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
                with config as cfgdir:
                    result = await self._exec(argv, cfgdir, timeout)
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

    @asynccontextmanager
    async def run_authenticated_to_file(
        self,
        domain: str,
        argv: Sequence[str],
        timeout: Optional[float] = None,
        serialize: bool = False,
    ) -> AsyncIterator[SpooledRunResult]:
        """Stream authenticated GAM stdout to a private ``0600`` file.

        The file exists only inside the context and is overwritten then removed on success,
        command failure, consumer failure, or cancellation.  This path is for large exports that
        should not be retained as a bytes object and decoded on the event loop.
        """
        command = list(argv)
        command_timeout = timeout or self.timeout

        @asynccontextmanager
        async def _do() -> AsyncIterator[SpooledRunResult]:
            result: Optional[RunResult] = None
            token_persistence_failed = False
            config = EphemeralConfig(self.vault, domain, base_dir=self.base_dir)
            try:
                with config as cfgdir:
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
                        # Preserve the command failure if private-file cleanup also fails. The
                        # enclosing EphemeralConfig still wipes the whole private directory.
                        failed = result.returncode != 0
                        if result.returncode == 0:
                            await asyncio.to_thread(
                                _strip_cfgdir_noise_file, spool_path, cfgdir
                            )
                            yield SpooledRunResult(
                                path=spool_path,
                                stdout_bytes=spool_path.stat().st_size,
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
            if result.returncode != 0:
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
