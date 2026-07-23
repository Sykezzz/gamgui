"""Small cross-platform process-liveness helpers for durable operation leases."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ProcessState(str, Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProcessProbe:
    state: ProcessState
    identity: str = ""


def probe_process(pid: int) -> ProcessProbe:
    """Probe liveness and process-start identity without treating uncertainty as death."""

    if pid <= 0:
        # Legacy rows created before lease metadata existed have no trustworthy PID. Recovering
        # them automatically could overlap an older still-running app, so leave them fail-closed
        # for explicit reconciliation.
        return ProcessProbe(ProcessState.UNKNOWN)
    if sys.platform == "win32":
        return _probe_windows_process(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessProbe(ProcessState.DEAD)
    except PermissionError:
        return ProcessProbe(ProcessState.ALIVE, _posix_process_identity(pid))
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return ProcessProbe(ProcessState.DEAD)
        if exc.errno == errno.EPERM:
            return ProcessProbe(ProcessState.ALIVE, _posix_process_identity(pid))
        return ProcessProbe(ProcessState.UNKNOWN)
    except (OverflowError, ValueError):
        return ProcessProbe(ProcessState.UNKNOWN)
    return ProcessProbe(ProcessState.ALIVE, _posix_process_identity(pid))


def current_process_identity() -> str:
    """Return a stable identity for this OS process lifetime when the OS exposes one."""

    probe = probe_process(os.getpid())
    return probe.identity if probe.state is ProcessState.ALIVE else ""


def process_lease_is_dead(pid: int, expected_identity: str = "") -> bool:
    """Recover only definite death or definite PID reuse; uncertainty stays fail-closed."""

    probe = probe_process(pid)
    if probe.state is ProcessState.DEAD:
        return True
    if probe.state is not ProcessState.ALIVE:
        return False
    return bool(
        expected_identity
        and probe.identity
        and probe.identity != expected_identity
    )


def _posix_process_identity(pid: int) -> str:
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            fields = stat[stat.rfind(")") + 2 :].split()
            start_ticks = fields[19]
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="ascii"
            ).strip()
            return f"linux:{boot_id}:{start_ticks}"
        except (OSError, IndexError, ValueError):
            return ""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    started = result.stdout.strip() if result.returncode == 0 else ""
    return f"posix:{started}" if started else ""


def _probe_windows_process(pid: int) -> ProcessProbe:
    """Query Windows liveness and creation time without using destructive ``os.kill``."""

    try:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            error = ctypes.get_last_error()
            if error in {87, 1168}:  # invalid parameter / not found
                return ProcessProbe(ProcessState.DEAD)
            if error == 5:  # access denied still proves the PID exists
                return ProcessProbe(ProcessState.ALIVE)
            return ProcessProbe(ProcessState.UNKNOWN)
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return ProcessProbe(ProcessState.UNKNOWN)
            if int(exit_code.value) != still_active:
                return ProcessProbe(ProcessState.DEAD)
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            identity = ""
            if kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                ticks = (int(created.dwHighDateTime) << 32) | int(
                    created.dwLowDateTime
                )
                identity = f"windows:{ticks}"
            return ProcessProbe(ProcessState.ALIVE, identity)
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, OverflowError, ValueError):
        return ProcessProbe(ProcessState.UNKNOWN)
