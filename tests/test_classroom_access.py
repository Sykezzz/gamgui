from __future__ import annotations

import os
import plistlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gamgui.core.classroom_access import (
    CSVMode,
    EntitlementPolicy,
    EntitlementService,
    EntitlementStore,
    LaunchAgentManager,
    SourceMode,
)
from gamgui.core.classroom_access.models import parse_email_lines
from gamgui.core.activity import ActivityRegistry
from gamgui.core.gam.models import GAMGroup, GAMUser, GroupMember


class _Audit:
    def __init__(self) -> None:
        self.entries = []

    def record(self, action, **kwargs):
        self.entries.append((action, kwargs))


class _Connector:
    def __init__(self) -> None:
        self.groups = {
            "classroom_teachers@example.org": GAMGroup(
                "classroom_teachers@example.org", "Classroom Teachers"
            ),
            "staff@example.org": GAMGroup("staff@example.org", "Staff"),
            "exceptions@example.org": GAMGroup("exceptions@example.org", "Exceptions"),
        }
        self.members = {
            "classroom_teachers@example.org": [
                GroupMember("old@example.org", member_type="USER")
            ],
            "staff@example.org": [
                GroupMember("teacher@example.org", member_type="USER")
            ],
        }
        self.users = {
            email: GAMUser(email, suspended=suspended)
            for email, suspended in (
                ("teacher@example.org", False),
                ("admin@example.org", False),
                ("old@example.org", False),
                ("suspended@example.org", True),
            )
        }
        self.calls = []
        self.audit = _Audit()

    async def list_groups(self):
        return list(self.groups.values())

    async def list_group_members(self, group):
        return list(self.members.get(group, ()))

    async def list_oneroster_directory(self):
        return dict(self.users)

    async def add_group_member(self, group, email):
        self.calls.append(("add", email))
        self.members.setdefault(group, []).append(GroupMember(email))
        return SimpleNamespace(ok=True, detail="added")

    async def remove_group_member(self, group, email):
        self.calls.append(("remove", email))
        self.members[group] = [
            member for member in self.members.get(group, ()) if member.email != email
        ]
        return SimpleNamespace(ok=True, detail="removed")


def _store(tmp_path: Path) -> EntitlementStore:
    return EntitlementStore(tmp_path / "entitlements.db")


def _group_policy(store: EntitlementStore) -> EntitlementPolicy:
    return store.save_policy(
        EntitlementPolicy(
            id="",
            domain="example.org",
            target_group="classroom_teachers@example.org",
            source_mode=SourceMode.GOOGLE_GROUP.value,
            source_group="staff@example.org",
            exception_groups=("exceptions@example.org",),
        )
    )


def _csv_policy(
    store: EntitlementStore, emails=("teacher@example.org",)
) -> EntitlementPolicy:
    return store.save_policy(
        EntitlementPolicy(
            id="",
            domain="example.org",
            target_group="classroom_teachers@example.org",
            source_mode=SourceMode.CSV.value,
            csv_mode=CSVMode.UPLOAD.value,
            csv_emails=tuple(emails),
            exception_users=("admin@example.org",),
        )
    )


def test_parse_email_lines_reuses_narrow_roster_contract():
    assert parse_email_lines("email\nTeacher@Example.org\n") == (
        "teacher@example.org",
    )
    with pytest.raises(ValueError, match="email column"):
        parse_email_lines("name,id\nTeacher,1\n")


def test_store_is_restart_safe_and_owner_only(tmp_path):
    store = _store(tmp_path)
    policy = _group_policy(store)
    reopened = EntitlementStore(store.path)
    assert reopened.get_policy(policy.id) == policy
    if os.name == "posix":
        assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_group_source_plans_exact_direct_membership_and_requires_approval(tmp_path):
    connector = _Connector()
    store = _store(tmp_path)
    policy = _group_policy(store)
    plan = await EntitlementService(connector, "example.org", store).plan(
        policy, approval_required=True
    )
    assert plan.status == "held"
    assert plan.adds == ("exceptions@example.org", "staff@example.org")
    assert plan.removes == ("old@example.org",)
    assert "Review and approve" in plan.hold_reason


@pytest.mark.asyncio
async def test_approved_plan_adds_before_removing_and_activates_policy(tmp_path):
    connector = _Connector()
    store = _store(tmp_path)
    policy = _group_policy(store)
    service = EntitlementService(connector, "example.org", store)
    plan = await service.plan(policy, approval_required=True)
    assert store.approve_plan(plan.id)

    result = await service.apply(plan.id)

    assert result.status == "completed"
    assert connector.calls == [
        ("add", "exceptions@example.org"),
        ("add", "staff@example.org"),
        ("remove", "old@example.org"),
    ]
    assert store.get_policy(policy.id).status == "active"


@pytest.mark.asyncio
async def test_interrupted_approved_plan_resumes_only_remaining_changes(tmp_path):
    connector = _Connector()
    store = _store(tmp_path)
    policy = _group_policy(store)
    service = EntitlementService(connector, "example.org", store)
    plan = await service.plan(policy, approval_required=True)
    assert store.approve_plan(plan.id)
    assert store.claim_plan(plan.id, "crashed-worker")
    connector.members[policy.target_group].append(
        GroupMember("exceptions@example.org", member_type="GROUP")
    )
    store.mark_target(
        plan.id,
        "exceptions@example.org",
        "add",
        ok=True,
        detail="added before crash",
        owner_id="crashed-worker",
    )
    store.finish_plan(
        plan.id,
        status="interrupted",
        error="simulated process exit",
        owner_id="crashed-worker",
    )

    result = await EntitlementService(
        connector, "example.org", store
    ).resume_interrupted(plan.id)

    assert result is not None
    assert result.status == "completed"
    assert connector.calls == [
        ("add", "staff@example.org"),
        ("remove", "old@example.org"),
    ]
    assert store.get_policy(policy.id).status == "active"


@pytest.mark.asyncio
async def test_interrupted_plan_does_not_inherit_approval_after_source_change(tmp_path):
    connector = _Connector()
    store = _store(tmp_path)
    policy = _group_policy(store)
    service = EntitlementService(connector, "example.org", store)
    plan = await service.plan(policy, approval_required=True)
    assert store.approve_plan(plan.id)
    assert store.claim_plan(plan.id, "crashed-worker")
    store.finish_plan(
        plan.id,
        status="interrupted",
        error="simulated process exit",
        owner_id="crashed-worker",
    )
    connector.members[policy.source_group].append(
        GroupMember("new-teacher@example.org")
    )

    result = await EntitlementService(
        connector, "example.org", store
    ).resume_interrupted(plan.id)

    assert result is None
    assert connector.calls == []
    assert store.get_plan(plan.id).status == "interrupted"


@pytest.mark.asyncio
async def test_approved_manifest_rejects_target_membership_drift(tmp_path):
    connector = _Connector()
    store = _store(tmp_path)
    policy = _group_policy(store)
    service = EntitlementService(connector, "example.org", store)
    plan = await service.plan(policy, approval_required=True)
    assert store.approve_plan(plan.id)
    connector.members[policy.target_group].append(GroupMember("rogue@example.org"))

    with pytest.raises(
        ValueError, match="live membership changed after preview"
    ):
        await service.apply(plan.id)

    assert connector.calls == []
    assert store.get_plan(plan.id).status == "stale"


@pytest.mark.asyncio
async def test_addition_failure_prevents_every_removal(tmp_path):
    connector = _Connector()
    store = _store(tmp_path)
    policy = _group_policy(store)
    service = EntitlementService(connector, "example.org", store)
    plan = await service.plan(policy, approval_required=True)
    assert store.approve_plan(plan.id)

    async def fail_first_add(group, email):
        connector.calls.append(("add", email))
        if email == "exceptions@example.org":
            return SimpleNamespace(ok=False, detail="simulated failure")
        connector.members.setdefault(group, []).append(GroupMember(email))
        return SimpleNamespace(ok=True, detail="added")

    connector.add_group_member = fail_first_add
    result = await service.apply(plan.id)

    assert result.status == "partial"
    assert ("remove", "old@example.org") not in connector.calls
    assert any(
        member.email == "old@example.org"
        for member in connector.members[policy.target_group]
    )


@pytest.mark.asyncio
async def test_scheduled_plan_holds_above_total_change_cap(tmp_path):
    connector = _Connector()
    emails = tuple(f"teacher{i}@example.org" for i in range(201))
    connector.users.update({email: GAMUser(email) for email in emails})
    connector.members["classroom_teachers@example.org"] = []
    store = _store(tmp_path)
    policy = store.save_policy(
        EntitlementPolicy(
            id="",
            domain="example.org",
            target_group="classroom_teachers@example.org",
            source_mode=SourceMode.CSV.value,
            csv_mode=CSVMode.UPLOAD.value,
            csv_emails=emails,
        )
    )

    plan = await EntitlementService(connector, "example.org", store).plan(
        policy, approval_required=False
    )

    assert plan.status == "held"
    assert "201 changes" in plan.hold_reason


@pytest.mark.asyncio
async def test_connector_identity_change_holds_before_live_reads(tmp_path):
    class Vault:
        def __init__(self, client_email):
            self.client_email = client_email

        def get(self, domain, name):
            if name == "oauth2service":
                return (
                    '{"client_email":"'
                    + self.client_email
                    + '","private_key_id":"key-1"}'
                )
            return '{"client_id":"client-1"}'

    first = _Connector()
    first.runner = SimpleNamespace(vault=Vault("gam-one@example.org"))
    store = _store(tmp_path)
    first_service = EntitlementService(first, "example.org", store)
    policy = store.save_policy(
        replace(
            _group_policy(store),
            connector_identity=first_service.connector_identity,
        )
    )
    second = _Connector()
    second.runner = SimpleNamespace(vault=Vault("gam-two@example.org"))

    plan = await EntitlementService(second, "example.org", store).plan(
        policy, approval_required=False
    )

    assert plan.status == "held"
    assert "connector identity changed" in plan.hold_reason
    assert second.calls == []


@pytest.mark.asyncio
async def test_csv_validates_active_users_and_holds_large_removal(tmp_path):
    connector = _Connector()
    connector.members["classroom_teachers@example.org"] = [
        GroupMember(f"person{i}@example.org") for i in range(30)
    ]
    store = _store(tmp_path)
    policy = _csv_policy(store)
    plan = await EntitlementService(connector, "example.org", store).plan(
        policy, approval_required=False
    )
    assert plan.status == "held"
    assert "removes 30 members" in plan.hold_reason


@pytest.mark.asyncio
async def test_empty_or_invalid_csv_holds_without_mutating(tmp_path):
    connector = _Connector()
    store = _store(tmp_path)
    policy = _csv_policy(store, ())
    service = EntitlementService(connector, "example.org", store)
    plan = await service.plan(policy, approval_required=False)
    assert plan.status == "held"
    assert "empty" in plan.hold_reason
    assert connector.calls == []


@pytest.mark.asyncio
async def test_watched_csv_detects_mid_read_change(tmp_path, monkeypatch):
    source = tmp_path / "staff.csv"
    source.write_text("email\nteacher@example.org\n", encoding="utf-8")
    connector = _Connector()
    store = _store(tmp_path)
    policy = store.save_policy(
        EntitlementPolicy(
            id="",
            domain="example.org",
            target_group="classroom_teachers@example.org",
            source_mode=SourceMode.CSV.value,
            csv_mode=CSVMode.WATCH.value,
            watch_path=str(source),
        )
    )
    original = Path.read_text

    def changing_read(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        source.write_text(value + "\n", encoding="utf-8")
        return value

    monkeypatch.setattr(Path, "read_text", changing_read)
    plan = await EntitlementService(connector, "example.org", store).plan(
        policy, approval_required=False
    )
    assert plan.status == "held"
    assert "changed while" in plan.hold_reason


@pytest.mark.asyncio
async def test_missing_watched_csv_holds_without_exposing_the_path(tmp_path):
    missing = tmp_path / "private" / "staff.csv"
    connector = _Connector()
    store = _store(tmp_path)
    policy = store.save_policy(
        EntitlementPolicy(
            id="",
            domain="example.org",
            target_group="classroom_teachers@example.org",
            source_mode=SourceMode.CSV.value,
            csv_mode=CSVMode.WATCH.value,
            watch_path=str(missing),
        )
    )

    plan = await EntitlementService(connector, "example.org", store).plan(
        policy, approval_required=False
    )

    assert plan.status == "held"
    assert plan.hold_reason == "The watched CSV was not found."
    assert str(missing) not in plan.hold_reason
    assert connector.calls == []


def test_launch_agent_payload_and_install_are_deterministic(tmp_path, monkeypatch):
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(tmp_path / "data"))
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    manager = LaunchAgentManager(
        launch_agents_dir=tmp_path / "LaunchAgents",
        executable=Path("/Applications/GamGUI.app/Contents/MacOS/GamGUI"),
        platform="darwin",
        run=run,
    )
    policy = EntitlementPolicy(
        id="policy-1",
        domain="example.org",
        target_group="classroom_teachers@example.org",
        source_mode=SourceMode.GOOGLE_GROUP.value,
        source_group="staff@example.org",
        schedule_hour=2,
        schedule_minute=15,
    )
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)
    path = manager.install(policy)
    payload = manager.payload(policy)
    assert path.exists()
    assert payload["StartCalendarInterval"] == {"Hour": 2, "Minute": 15}
    assert "--headless-task" in payload["ProgramArguments"]
    assert calls[-1][:3] == ["launchctl", "bootstrap", "gui/501"]
    manager.install(replace(policy, schedule_hour=3, schedule_minute=30))
    updated = plistlib.loads(path.read_bytes())
    assert updated["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}
    assert sum(command[1] == "bootstrap" for command in calls) == 2
    manager.disable(policy.id)
    assert not path.exists()
    assert calls[-1][:3] == ["launchctl", "bootout", "gui/501"]


@pytest.mark.asyncio
async def test_headless_agent_skips_busy_without_deactivating_policy(
    tmp_path, monkeypatch
):
    from gamgui import agent

    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(tmp_path))
    store = EntitlementStore()
    policy = _group_policy(store)
    policy = store.record_policy_result(
        policy.id,
        status="completed",
        message="activated",
        source_hash="source",
        activate=True,
    )
    registry = ActivityRegistry()
    monkeypatch.setattr(agent, "activity_registry", registry)
    with registry.acquire("another-job"):
        code = await agent.run_classroom_teachers(policy.id, scheduled=True)
    updated = store.get_policy(policy.id)
    assert code == 0
    assert updated.status == "active"
    assert updated.last_run_status == "skipped-busy"


@pytest.mark.asyncio
async def test_post_apply_verification_failure_closes_running_manifest(tmp_path):
    connector = _Connector()
    store = _store(tmp_path)
    policy = _group_policy(store)
    service = EntitlementService(connector, "example.org", store)
    plan = await service.plan(policy, approval_required=True)
    assert store.approve_plan(plan.id)
    original = connector.list_group_members
    target_reads = 0

    async def fail_final_read(group):
        nonlocal target_reads
        if group == policy.target_group:
            target_reads += 1
            if target_reads >= 2:
                raise RuntimeError("verification unavailable")
        return await original(group)

    connector.list_group_members = fail_final_read
    with pytest.raises(RuntimeError, match="verification unavailable"):
        await service.apply(plan.id)
    assert store.get_plan(plan.id).status == "failed"
    assert store.has_active_jobs() is False


@pytest.mark.asyncio
async def test_headless_agent_preserves_safety_held_plan_for_approval(
    tmp_path, monkeypatch
):
    from gamgui import agent

    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(tmp_path))
    store = EntitlementStore()
    policy = _group_policy(store)
    policy = store.record_policy_result(
        policy.id,
        status="completed",
        message="activated",
        source_hash="source",
        activate=True,
    )
    connector = _Connector()
    connector.members["classroom_teachers@example.org"] = [
        GroupMember(f"person{i}@example.org") for i in range(30)
    ]

    class Vault:
        pass

    class Runner:
        def __init__(self, vault):
            self.vault = vault

    class ConnectorFactory:
        def __new__(cls, runner, domain):
            connector.test = lambda: _async_value(
                SimpleNamespace(ok=True, detail="ready")
            )
            return connector

    async def _async_value(value):
        return value

    monkeypatch.setattr(agent, "SecretsVault", Vault)
    monkeypatch.setattr(agent, "GAMRunner", Runner)
    monkeypatch.setattr(agent, "GAMConnector", ConnectorFactory)
    monkeypatch.setattr(agent, "activity_registry", ActivityRegistry())
    code = await agent.run_classroom_teachers(policy.id, scheduled=True)
    updated = store.get_policy(policy.id)
    assert code == 2
    assert updated.status == "held"
    assert updated.pending_plan_id
    assert store.get_plan(updated.pending_plan_id).status == "held"
