from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gamgui.core.activity import ActivityRegistry
from gamgui.core.classroom_access import (
    EntitlementService,
    EntitlementStore,
    LaunchAgentManager,
)
from gamgui.core.directory_index import Page
from gamgui.core.gam.models import GAMGroup, GAMUser, GroupMember
from gamgui.web.routes.classroom_access import router


class _Audit:
    def record(self, *_args, **_kwargs):
        return None


class _Connector:
    def __init__(self):
        self.audit = _Audit()
        self.groups = [
            GAMGroup("classroom_teachers@example.org", "Classroom Teachers"),
            GAMGroup("staff@example.org", "Staff"),
            GAMGroup("administrators@example.org", "Administrators"),
        ]
        self.members = {
            "classroom_teachers@example.org": [],
            "staff@example.org": [GroupMember("teacher@example.org")],
            "administrators@example.org": [GroupMember("admin@example.org")],
        }
        self.users = {
            "teacher@example.org": GAMUser("teacher@example.org"),
            "admin@example.org": GAMUser("admin@example.org"),
        }

    async def list_groups(self):
        return self.groups

    async def list_group_members(self, group):
        return list(self.members[group])

    async def list_oneroster_directory(self):
        return self.users

    async def add_group_member(self, group, email):
        self.members[group].append(GroupMember(email))
        return SimpleNamespace(ok=True, detail="added")

    async def remove_group_member(self, group, email):
        self.members[group] = [item for item in self.members[group] if item.email != email]
        return SimpleNamespace(ok=True, detail="removed")


class _Scheduler(LaunchAgentManager):
    def __init__(self):
        super().__init__(platform="darwin")
        self.installed = []
        self.disabled = []

    def install(self, policy):
        self.installed.append(policy.id)

    def disable(self, policy_id):
        self.disabled.append(policy_id)


def _client(tmp_path):
    connector = _Connector()
    store = EntitlementStore(tmp_path / "entitlements.db")
    scheduler = _Scheduler()
    registry = ActivityRegistry()
    state = SimpleNamespace(
        connector=connector,
        audit_domain="example.org",
        entitlement_store=store,
        entitlement_service=EntitlementService(connector, "example.org", store),
        entitlement_scheduler=scheduler,
        activity_registry=registry,
    )
    state.ensure_workspace_services = lambda: None

    async def directory_groups(**_kwargs):
        return Page(items=connector.groups, total=len(connector.groups))

    state.directory_groups = directory_groups
    app = FastAPI()
    app.state.gamgui = state
    app.include_router(router)
    return TestClient(app), state


def test_access_page_is_discoverable_and_has_safe_defaults(tmp_path):
    client, _state = _client(tmp_path)
    response = client.get("/classroom/access")
    assert response.status_code == 200
    assert "Classroom teacher access" in response.text
    assert "Next run" not in response.text
    assert "No membership changes occur until approval" in response.text
    assert "02:00" in response.text


def test_group_policy_preview_then_approve_and_schedule(tmp_path):
    client, state = _client(tmp_path)
    preview = client.post(
        "/classroom/access/save",
        data={
            "target_group": "classroom_teachers@example.org",
            "target_confirmed": "yes",
            "source_mode": "google_group",
            "source_groups": (
                "Staff@Example.org\n"
                "administrators@example.org\n"
                "staff@example.org"
            ),
            "csv_mode": "upload",
            "exception_users": "admin@example.org",
            "exception_groups": "",
            "schedule_enabled": "yes",
            "schedule_hour": "2",
            "schedule_minute": "0",
        },
    )
    assert preview.status_code == 200
    assert "Review and approve this exact membership plan" in preview.text
    policy = state.entitlement_store.policy_for_domain("example.org")
    plan = state.entitlement_store.get_plan(policy.pending_plan_id)
    assert policy.source_groups == (
        "administrators@example.org",
        "staff@example.org",
    )
    assert set(plan.adds) == {
        "administrators@example.org",
        "staff@example.org",
        "admin@example.org",
    }
    assert "administrators@example.org, staff@example.org" in preview.text

    applied = client.post(
        "/classroom/access/approve",
        data={"policy_id": policy.id, "plan_id": plan.id},
    )
    assert applied.status_code == 200
    assert "membership now matches the approved source" in applied.text
    assert "Next run" in applied.text
    assert state.entitlement_store.get_policy(policy.id).status == "active"
    assert state.entitlement_scheduler.installed == [policy.id]


def test_approving_disabled_schedule_unloads_existing_launch_agent(tmp_path):
    client, state = _client(tmp_path)
    preview = client.post(
        "/classroom/access/save",
        data={
            "target_group": "classroom_teachers@example.org",
            "target_confirmed": "yes",
            "source_mode": "google_group",
            "source_group": "staff@example.org",
            "csv_mode": "upload",
            "schedule_hour": "2",
            "schedule_minute": "0",
        },
    )
    policy = state.entitlement_store.policy_for_domain("example.org")
    plan = state.entitlement_store.get_plan(policy.pending_plan_id)

    applied = client.post(
        "/classroom/access/approve",
        data={"policy_id": policy.id, "plan_id": plan.id},
    )

    assert preview.status_code == 200
    assert applied.status_code == 200
    assert state.entitlement_scheduler.disabled == [policy.id]
    assert state.entitlement_scheduler.installed == []


def test_legacy_single_source_form_field_still_previews(tmp_path):
    client, state = _client(tmp_path)

    preview = client.post(
        "/classroom/access/save",
        data={
            "target_group": "classroom_teachers@example.org",
            "target_confirmed": "yes",
            "source_mode": "google_group",
            "source_group": "staff@example.org",
            "schedule_hour": "2",
            "schedule_minute": "0",
        },
    )

    assert preview.status_code == 200
    policy = state.entitlement_store.policy_for_domain("example.org")
    assert policy.source_groups == ("staff@example.org",)
    assert "Review and approve this exact membership plan" in preview.text


def test_csv_upload_is_normalized_and_busy_apply_does_not_mutate(tmp_path):
    client, state = _client(tmp_path)
    preview = client.post(
        "/classroom/access/save",
        data={
            "target_group": "classroom_teachers@example.org",
            "target_confirmed": "yes",
            "source_mode": "csv",
            "csv_mode": "upload",
            "schedule_hour": "2",
            "schedule_minute": "0",
        },
        files={"csv_file": ("staff.csv", b"email\nTeacher@Example.org\n", "text/csv")},
    )
    assert preview.status_code == 200
    policy = state.entitlement_store.policy_for_domain("example.org")
    assert policy.csv_emails == ("teacher@example.org",)
    plan = state.entitlement_store.get_plan(policy.pending_plan_id)
    before = list(state.connector.members["classroom_teachers@example.org"])
    with state.activity_registry.acquire("another-job"):
        response = client.post(
            "/classroom/access/approve",
            data={"policy_id": policy.id, "plan_id": plan.id},
        )
    assert response.status_code == 200
    assert "Another active administrative operation is in progress" in response.text
    assert state.connector.members["classroom_teachers@example.org"] == before


def test_invalid_target_confirmation_and_empty_csv_are_inline_errors(tmp_path):
    client, _state = _client(tmp_path)
    unconfirmed = client.post(
        "/classroom/access/save",
        data={
            "target_group": "classroom_teachers@example.org",
            "source_mode": "google_group",
            "source_group": "staff@example.org",
        },
    )
    assert unconfirmed.status_code == 200
    assert "Confirm that the selected target" in unconfirmed.text

    empty = client.post(
        "/classroom/access/save",
        data={
            "target_group": "classroom_teachers@example.org",
            "target_confirmed": "yes",
            "source_mode": "csv",
            "csv_mode": "upload",
        },
    )
    assert empty.status_code == 200
    assert "Choose a CSV file" in empty.text
