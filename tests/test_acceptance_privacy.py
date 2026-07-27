from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from gamgui.core.gam.commands import EXPECTED_GAM_VERSION


def _module():
    path = Path(__file__).parents[1] / "scripts" / "acceptance.py"
    spec = importlib.util.spec_from_file_location("gamgui_acceptance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Runner:
    async def version(self):
        # The suffix is intentionally sensitive-looking and must never be echoed.
        return f"GAM {EXPECTED_GAM_VERSION} tenant=private.example.edu"


class _State:
    def __init__(self):
        self.runner = _Runner()
        self.closed = False

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_acceptance_emits_only_fixed_status_and_timing_fields(
    monkeypatch,
    capsys,
):
    acceptance = _module()
    state = _State()
    private_values = (
        "private.example.edu",
        "admin@private.example.edu",
        "private-user-id",
        "private-drive-id",
    )

    monkeypatch.setattr(
        acceptance,
        "CanaryConfigStore",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                domain=private_values[0],
                subject=private_values[1],
            )
        ),
    )
    monkeypatch.setattr(acceptance, "_create_state", lambda _domain: state)

    async def canary(**_kwargs):
        return {
            "ok": True,
            "checked_at": 1_999_999_999.0,
            "tenant": private_values[0],
            "record": {"id": private_values[2]},
            "checks": [
                {
                    "name": "users",
                    "ok": True,
                    "duration_ms": 1.25,
                    "record": private_values[2],
                },
                {
                    "name": "groups",
                    "ok": True,
                    "duration_ms": 2.5,
                    "subject": private_values[1],
                },
                {
                    "name": "classroom",
                    "ok": True,
                    "duration_ms": 3.75,
                    "course_id": "private-course-id",
                },
                {
                    "name": "drive",
                    "ok": True,
                    "duration_ms": 5,
                    "file_id": private_values[3],
                },
            ],
        }

    monkeypatch.setattr(acceptance, "run_live_canary", canary)

    assert await acceptance.main() == 0
    output = capsys.readouterr().out

    assert state.closed
    assert "PASS: all bounded read-only checks passed." in output
    assert all(label in output for label in acceptance._DISPLAY_NAMES.values())
    assert all(value not in output for value in private_values)
    assert "private-course-id" not in output
    assert "checked_at" not in output


@pytest.mark.asyncio
async def test_acceptance_suppresses_data_bearing_exception_text(
    monkeypatch,
    capsys,
):
    acceptance = _module()
    state = _State()
    secret = "admin@private.example.edu response={full tenant record}"

    monkeypatch.setattr(
        acceptance,
        "CanaryConfigStore",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                domain="private.example.edu",
                subject="admin@private.example.edu",
            )
        ),
    )
    monkeypatch.setattr(acceptance, "_create_state", lambda _domain: state)

    async def failing_canary(**_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(acceptance, "run_live_canary", failing_canary)

    assert await acceptance.main() == 1
    output = capsys.readouterr().out
    assert state.closed
    assert "details were intentionally suppressed" in output
    assert secret not in output
    assert "private.example.edu" not in output


@pytest.mark.asyncio
async def test_acceptance_requires_config_before_creating_live_state(
    monkeypatch,
    capsys,
):
    acceptance = _module()
    created = False

    monkeypatch.setattr(
        acceptance,
        "CanaryConfigStore",
        lambda: SimpleNamespace(load=lambda: None),
    )

    def create(**_kwargs):
        nonlocal created
        created = True
        return _State()

    monkeypatch.setattr(acceptance, "_create_state", create)

    assert await acceptance.main() == 2
    assert not created
    assert "no live probes ran" in capsys.readouterr().out


def test_cli_suppresses_unhandled_exception_text(capsys):
    acceptance = _module()

    async def failing():
        raise RuntimeError("private.example.edu full-record")

    assert acceptance.cli(failing) == 1
    output = capsys.readouterr().out
    assert "details were intentionally suppressed" in output
    assert "private.example.edu" not in output
    assert "full-record" not in output


def test_documented_direct_command_bootstraps_the_source_tree(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "acceptance.py"
    env = os.environ.copy()
    env["GAMGUI_APP_DATA_DIR"] = str(tmp_path / "unconfigured")

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "SETUP REQUIRED" in result.stdout
    assert "ModuleNotFoundError" not in result.stderr
