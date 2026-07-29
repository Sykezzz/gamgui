"""Per-user macOS LaunchAgent management for entitlement reconciliation."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..paths import APP_DATA_ENV, app_data_dir
from .models import EntitlementPolicy

_LABEL_PREFIX = "org.gamgui.classroom-teachers"


class LaunchAgentManager:
    def __init__(
        self,
        *,
        launch_agents_dir: Optional[Path] = None,
        executable: Optional[Path] = None,
        platform: Optional[str] = None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.platform = platform or sys.platform
        self.launch_agents_dir = (
            Path(launch_agents_dir)
            if launch_agents_dir is not None
            else Path.home() / "Library" / "LaunchAgents"
        )
        self.executable = Path(executable) if executable is not None else Path(sys.executable)
        self._run = run

    def install(self, policy: EntitlementPolicy) -> Path:
        if self.platform != "darwin":
            raise RuntimeError("Background scheduling is available in the macOS app.")
        if not policy.schedule_enabled:
            return self.disable(policy.id)
        path = self.path_for(policy.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.payload(policy)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent)
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        target = f"gui/{os.getuid()}"
        self._run(
            ["launchctl", "bootout", target, str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        result = self._run(
            ["launchctl", "bootstrap", target, str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError("macOS could not load the Classroom teacher background schedule.")
        return path

    def disable(self, policy_id: str) -> Path:
        path = self.path_for(policy_id)
        if self.platform == "darwin" and path.exists():
            self._run(
                ["launchctl", "bootout", f"gui/{os.getuid()}", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            path.unlink()
        return path

    def path_for(self, policy_id: str) -> Path:
        return self.launch_agents_dir / f"{self.label_for(policy_id)}.plist"

    def label_for(self, policy_id: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9.-]", "-", policy_id).strip(".-")
        if not safe:
            raise ValueError("Policy ID cannot produce a LaunchAgent label.")
        return f"{_LABEL_PREFIX}.{safe}"

    def payload(self, policy: EntitlementPolicy) -> dict:
        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        if os.name == "posix":
            os.chmod(log_dir, 0o700)
        stdout_path = log_dir / "classroom-teachers-agent.log"
        stderr_path = log_dir / "classroom-teachers-agent.error.log"
        for log_path in (stdout_path, stderr_path):
            descriptor = os.open(
                str(log_path),
                os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            os.close(descriptor)
            if os.name == "posix":
                os.chmod(log_path, 0o600)
        arguments: Sequence[str]
        if getattr(sys, "frozen", False):
            arguments = (
                str(self.executable),
                "--headless-task",
                "classroom-teachers",
                "--policy-id",
                policy.id,
                "--scheduled",
            )
        else:
            arguments = (
                str(self.executable),
                "-m",
                "gamgui.app",
                "--headless-task",
                "classroom-teachers",
                "--policy-id",
                policy.id,
                "--scheduled",
            )
        return {
            "Label": self.label_for(policy.id),
            "ProgramArguments": list(arguments),
            "RunAtLoad": False,
            "ProcessType": "Background",
            "StartCalendarInterval": {
                "Hour": int(policy.schedule_hour),
                "Minute": int(policy.schedule_minute),
            },
            "EnvironmentVariables": {APP_DATA_ENV: str(app_data_dir())},
            "StandardOutPath": str(stdout_path),
            "StandardErrorPath": str(stderr_path),
        }
