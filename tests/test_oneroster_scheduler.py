from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from gamgui.components.oneroster import (
    GateState,
    OneRosterError,
    OneRosterService,
    StudentEnrollmentGate,
)
from gamgui.web.server import AppState


class _ScheduledService:
    def __init__(self, release_at: datetime) -> None:
        self.gate = StudentEnrollmentGate(
            state=GateState.ARMED,
            timezone="America/Chicago",
            manifest_id="manifest-1",
            manifest_hash="a" * 64,
            release_at=release_at.isoformat(),
        )
        self.calls: list[str] = []

    def get_gate(self):
        return self.gate

    async def revalidate_scheduled_gate(self, connector, manifest_id, *, now):
        assert connector == "connector"
        assert manifest_id == "manifest-1"
        self.calls.append("revalidate")
        self.gate = StudentEnrollmentGate(
            state=GateState.OPEN,
            timezone="America/Chicago",
            manifest_id=manifest_id,
            manifest_hash="a" * 64,
            release_at=self.gate.release_at,
        )
        return self.gate

    def get_manifest_header(self, manifest_id):
        assert manifest_id == "manifest-1"
        return SimpleNamespace(status="awaiting_students")

    def get_manifest(self, manifest_id):
        raise AssertionError("scheduled release loaded every manifest action")

    async def execute_manifest(self, connector, manifest_id, *, now):
        assert connector == "connector"
        assert manifest_id == "manifest-1"
        self.calls.append("execute")


@pytest.mark.asyncio
async def test_scheduler_never_reads_live_state_before_armed_release_is_due():
    now = datetime.now(timezone.utc)
    service = _ScheduledService(now + timedelta(hours=1))
    state = SimpleNamespace(
        oneroster_service=service,
        connector="connector",
        oneroster_gate_error="",
    )

    changed = await AppState._process_due_oneroster_gate(state, now=now)

    assert changed is False
    assert service.calls == []


@pytest.mark.asyncio
async def test_scheduler_revalidates_then_executes_confirmed_student_remainder():
    now = datetime.now(timezone.utc)
    service = _ScheduledService(now - timedelta(seconds=1))
    state = SimpleNamespace(
        oneroster_service=service,
        connector="connector",
        oneroster_gate_error="stale",
    )

    changed = await AppState._process_due_oneroster_gate(state, now=now)

    assert changed is True
    assert service.calls == ["revalidate", "execute"]
    assert state.oneroster_gate_error == ""


@pytest.mark.asyncio
async def test_expired_scope_persists_visible_hold_and_stops_scheduler_retry(
    tmp_path,
):
    now = datetime.now(timezone.utc)
    release_at = now - timedelta(seconds=1)
    service = OneRosterService("example.org", tmp_path / "component")
    service.store.save_gate(
        StudentEnrollmentGate(
            state=GateState.ARMED,
            timezone="America/Chicago",
            manifest_id="manifest-scope",
            manifest_hash="b" * 64,
            release_at=release_at.isoformat(),
        )
    )
    state = SimpleNamespace(
        oneroster_service=service,
        connector="connector",
        oneroster_gate_error="",
    )

    changed = await AppState._process_due_oneroster_gate(state, now=now)

    assert changed is False
    held = OneRosterService("example.org", tmp_path / "component").get_gate()
    assert held.state is GateState.CLOSED
    assert held.hold_code == "CMP-AUTH-REQUIRED"
    assert "expired" in held.hold_detail
    assert held.manifest_id == "manifest-scope"
    assert held.manifest_hash == "b" * 64
    assert held.release_at == release_at.isoformat()
    assert state.oneroster_gate_error == "CMP-AUTH-REQUIRED"

    held_at = held.updated_at
    changed_again = await AppState._process_due_oneroster_gate(state, now=now)
    assert changed_again is False
    assert service.get_gate().updated_at == held_at


@pytest.mark.asyncio
async def test_unknown_scheduled_failure_is_redacted_and_persisted(tmp_path):
    now = datetime.now(timezone.utc)
    release_at = now - timedelta(seconds=1)
    service = OneRosterService("example.org", tmp_path / "component")
    service.mark_scope_ready(when=now.timestamp())
    service.store.save_gate(
        StudentEnrollmentGate(
            state=GateState.ARMED,
            timezone="America/Chicago",
            manifest_id="manifest-runtime",
            manifest_hash="c" * 64,
            release_at=release_at.isoformat(),
        )
    )
    calls = 0

    async def fail_revalidation(_connector, _manifest_id, *, now):
        nonlocal calls
        calls += 1
        raise RuntimeError("private tenant failure detail")

    service.revalidate_scheduled_gate = fail_revalidation
    state = SimpleNamespace(
        oneroster_service=service,
        connector="connector",
        oneroster_gate_error="",
    )

    changed = await AppState._process_due_oneroster_gate(state, now=now)

    assert changed is False
    held = service.get_gate()
    assert held.state is GateState.CLOSED
    assert held.hold_code == "OR-GATE-REVALIDATION-FAILED"
    assert "private tenant failure detail" not in held.hold_detail
    assert held.manifest_id == "manifest-runtime"
    assert calls == 1

    await AppState._process_due_oneroster_gate(state, now=now)
    assert calls == 1


def test_scheduled_hold_does_not_overwrite_concurrent_operator_close(tmp_path):
    service = OneRosterService("example.org", tmp_path / "component")
    service.close_gate()

    unchanged = service.hold_scheduled_gate_failure(
        "old-manifest",
        OneRosterError("CMP-AUTH-REQUIRED", "expired"),
    )

    assert unchanged.state is GateState.CLOSED
    assert unchanged.hold_code == ""
