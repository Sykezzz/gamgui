"""Bounded directory selector contracts for Groups, Builder, and Signatures."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.directory_index import Page
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import SIGNATURE_USER_FIELDS
from gamgui.core.gam.models import GAMGroup, GAMUser, GroupMember
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"


@pytest.fixture
def bounded_client(tmp_path: Path):
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(
        DOMAIN,
        {
            "client_secrets": "{}",
            "oauth2": "token",
            "oauth2service": '{"client_id": "test"}',
        },
    )
    runner = GAMRunner(
        vault=vault,
        gam_binary=FIXTURES / "mock_gam.sh",
        base_dir=tmp_path,
    )
    connector = GAMConnector(
        runner=runner,
        domain=DOMAIN,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    state = AppState(
        vault=vault,
        runner=runner,
        audit_domain=DOMAIN,
        connector=connector,
        token="test-token",
    )
    users = [
        GAMUser.from_json(
            {
                "primaryEmail": f"user{i:04d}@{DOMAIN}",
                "name": {"givenName": f"User{i:04d}", "familyName": "Scale"},
                "orgUnitPath": f"/School-{i % 3}",
                "organizations": [
                    {"department": f"Department-{i % 4}", "primary": True}
                ],
            }
        )
        for i in range(120)
    ]
    groups = [
        GAMGroup(
            email=f"group{i:04d}@{DOMAIN}",
            name=f"Group {i:04d}",
        )
        for i in range(120)
    ]
    state.directory_index.replace_users(users)
    state.directory_index.replace_groups(groups)

    with TestClient(create_app(state)) as client:
        client.get("/?token=test-token")
        yield client


def test_groups_page_and_search_results_are_hard_bounded(bounded_client):
    response = bounded_client.get("/groups")

    assert response.status_code == 200
    assert response.text.count('class="ucard ') == 50
    assert response.text.count("data-group=") == 50
    assert "Showing 50 of 120" in response.text
    assert "delay:300ms" in response.text
    assert response.text.count('hx-sync="this:replace"') == 2
    assert len(response.content) < 100_000

    people = bounded_client.get(
        "/groups/search/users",
        params={"q": "user0119@example.com"},
    )
    groups = bounded_client.get(
        "/groups/search/groups",
        params={"q": "group0119@example.com"},
    )
    assert people.text.count('class="ucard ') == 1
    assert "user0119@example.com" in people.text
    assert groups.text.count("data-group=") == 1
    assert "group0119@example.com" in groups.text


def test_builder_picker_uses_index_and_replaces_stale_requests(
    bounded_client,
    monkeypatch,
):
    state = bounded_client.app.state.gamgui

    async def forbidden(*args, **kwargs):
        raise AssertionError("builder picker must not fetch the full tenant")

    monkeypatch.setattr(state, "users", forbidden)
    monkeypatch.setattr(state.connector, "list_groups", forbidden)

    users = bounded_client.get("/builder/pick", params={"kind": "users", "q": "user"})
    groups = bounded_client.get("/builder/pick", params={"kind": "groups", "q": "group"})
    slot_named_query = bounded_client.get(
        "/builder/pick",
        params={"kind": "users", "email": "user0119@example.com"},
    )
    form = bounded_client.get("/builder/command/build.add_delegate")
    page = bounded_client.get("/builder")

    assert users.status_code == 200 and groups.status_code == 200
    assert users.text.count('class="upick-opt ') == 25
    assert groups.text.count('class="upick-opt ') == 25
    assert slot_named_query.text.count('class="upick-opt ') == 1
    assert "Showing the first 25 of 120" in users.text
    assert 'hx-trigger="input changed delay:300ms, focus"' in form.text
    assert 'hx-sync="this:replace"' in form.text
    assert 'fetch("/builder/pick' not in page.text


def test_signature_page_defers_directory_data_to_bounded_scope_search(
    bounded_client,
    monkeypatch,
):
    state = bounded_client.app.state.gamgui

    async def forbidden(*args, **kwargs):
        raise AssertionError("signature page must not load a full tenant")

    monkeypatch.setattr(state, "users", forbidden)
    monkeypatch.setattr(state.connector, "list_groups", forbidden)

    page = bounded_client.get("/signatures")
    users = bounded_client.get(
        "/signatures/scopes",
        params={"scope_type": "user", "q": "user"},
    )
    groups = bounded_client.get(
        "/signatures/scopes",
        params={"scope_type": "group", "q": "group"},
    )

    assert page.status_code == 200
    assert "SIG_USERS" not in page.text and "SIG_GROUPS" not in page.text
    assert 'hx-get="/signatures/scopes"' in page.text
    assert "delay:300ms" in page.text
    assert 'hx-sync="this:replace"' in page.text
    assert users.text.count('class="sig-scope-option ') == 50
    assert groups.text.count('class="sig-scope-option ') == 50
    assert len(users.content) < 100_000 and len(groups.content) < 100_000


def test_signature_structural_scopes_remain_searchable(bounded_client):
    ous = bounded_client.get(
        "/signatures/scopes",
        params={"scope_type": "ou", "q": "School-1"},
    )
    departments = bounded_client.get(
        "/signatures/scopes",
        params={"scope_type": "department", "q": "Department-2"},
    )
    location = bounded_client.get(
        "/signatures/scopes",
        params={"scope_type": "location", "q": "North Campus"},
    )

    assert "/School-1" in ous.text
    assert "Department-2" in departments.text
    assert "Enter the exact value used in the directory." in location.text


def test_signature_preview_and_apply_reject_unallowlisted_or_empty_scopes_before_reads(
    bounded_client,
    monkeypatch,
):
    state = bounded_client.app.state.gamgui

    async def forbidden(*args, **kwargs):
        raise AssertionError("invalid signature scopes must fail before a directory read")

    monkeypatch.setattr(state, "users", forbidden)
    monkeypatch.setattr(state.connector, "get_user", forbidden)
    monkeypatch.setattr(state.connector, "list_signature_scope_users", forbidden)

    cases = [
        ("all-users", ""),
        ("user", ""),
        ("group", ""),
        ("ou", ""),
        ("department", ""),
        ("location", ""),
        ("user", "not-an-email"),
        ("ou", "School"),
        ("company", "forged-value"),
    ]
    for endpoint in ("preview", "apply"):
        for scope_type, scope_value in cases:
            response = bounded_client.post(
                f"/signatures/{endpoint}",
                data={
                    "template": "{name}",
                    "scope_type": scope_type,
                    "scope_value": scope_value,
                },
            )
            assert response.status_code == 200
            assert "apply/status" not in response.text
            assert any(
                phrase in response.text
                for phrase in (
                    "supported signature scope",
                    "No active users match this scope",
                    "valid user email",
                    "valid org unit path",
                    "does not accept a scope value",
                )
            )


def test_signature_exact_user_uses_direct_projection_for_preview_and_apply(
    bounded_client,
    monkeypatch,
):
    state = bounded_client.app.state.gamgui
    calls = []

    async def forbidden_users(*args, **kwargs):
        raise AssertionError("signature flows must not call AppState.users")

    async def get_user(email, fields=None):
        calls.append((email, tuple(fields or ())))
        return GAMUser(
            primary_email=email,
            given_name="Exact",
            family_name="User",
        )

    async def forbidden_large(*args, **kwargs):
        raise AssertionError("an exact user must not run a tenant-scale export")

    async def set_signature(*args, **kwargs):
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(state, "users", forbidden_users)
    monkeypatch.setattr(state.connector, "get_user", get_user)
    monkeypatch.setattr(
        state.connector,
        "list_signature_scope_users",
        forbidden_large,
    )
    monkeypatch.setattr(state.connector, "set_signature", set_signature)

    body = {
        "template": "{name}",
        "scope_type": "user",
        "scope_value": "exact@example.com",
    }
    preview = bounded_client.post("/signatures/preview", data=body)
    apply = bounded_client.post("/signatures/apply", data=body)

    assert preview.status_code == 200 and "Exact User" in preview.text
    assert apply.status_code == 200 and "apply/status" in apply.text
    assert calls == [
        ("exact@example.com", SIGNATURE_USER_FIELDS),
        ("exact@example.com", SIGNATURE_USER_FIELDS),
    ]


def test_signature_company_scope_uses_private_spool_and_worker_thread(
    tmp_path,
    monkeypatch,
):
    spool = tmp_path / "signature-users.ndjson"
    spool.write_text(
        "\n".join(
            json.dumps(
                {
                    "primaryEmail": f"user{i}@example.com",
                    "name": {"givenName": f"User{i}", "familyName": "District"},
                    "suspended": i == 2,
                    "organizations": [{"title": "Teacher", "primary": True}],
                }
            )
            for i in range(3)
        )
        + "\n",
        encoding="utf-8",
    )

    class SpoolRunner:
        def __init__(self):
            self.calls = []

        @asynccontextmanager
        async def run_authenticated_to_file(self, domain, argv, **kwargs):
            self.calls.append((domain, list(argv)))
            yield SimpleNamespace(path=spool, stdout_bytes=spool.stat().st_size)

        async def run_authenticated(self, *args, **kwargs):
            raise AssertionError("large signature scopes must not buffer stdout")

    runner = SpoolRunner()
    connector = GAMConnector(
        runner=runner,
        domain=DOMAIN,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    import gamgui.core.connectors.gam_connector as connector_mod

    real_reduce = connector_mod._read_signature_user_spool
    reduced_off_loop = {"value": False}

    def guarded_reduce(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            reduced_off_loop["value"] = True
        else:
            raise AssertionError("signature spool parsing ran on the event loop")
        return real_reduce(*args, **kwargs)

    monkeypatch.setattr(connector_mod, "_read_signature_user_spool", guarded_reduce)
    users = asyncio.run(connector.list_signature_scope_users("company"))

    assert [user.primary_email for user in users] == [
        "user0@example.com",
        "user1@example.com",
    ]
    assert reduced_off_loop["value"]
    assert runner.calls == [
        (
            DOMAIN,
            [
                "print",
                "users",
                "fields",
                ",".join(SIGNATURE_USER_FIELDS),
                "formatjson",
            ],
        )
    ]


def test_group_member_detail_spools_off_loop_and_hard_limits_to_50(
    tmp_path,
    monkeypatch,
):
    spool = tmp_path / "group-members.ndjson"
    spool.write_text(
        "\n".join(
            json.dumps(
                {
                    "email": f"member{i:03d}@example.com",
                    "role": "R" * 5_000,
                    "type": "USER",
                    "status": "ACTIVE",
                }
            )
            for i in range(75)
        )
        + "\n",
        encoding="utf-8",
    )

    class SpoolRunner:
        def __init__(self):
            self.calls = []

        @asynccontextmanager
        async def run_authenticated_to_file(self, domain, argv, **kwargs):
            self.calls.append(list(argv))
            yield SimpleNamespace(path=spool, stdout_bytes=spool.stat().st_size)

    connector = GAMConnector(
        runner=SpoolRunner(),
        domain=DOMAIN,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    import gamgui.core.connectors.gam_connector as connector_mod

    real_reduce = connector_mod._read_group_member_spool
    reduced_off_loop = {"value": False}

    def guarded_reduce(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            reduced_off_loop["value"] = True
        else:
            raise AssertionError("group membership parsing ran on the event loop")
        return real_reduce(*args, **kwargs)

    monkeypatch.setattr(connector_mod, "_read_group_member_spool", guarded_reduce)
    page = asyncio.run(connector.list_group_members_page("large@example.com", limit=500))

    assert page.total == 75
    assert len(page.items) == 50
    assert all(len(member.role) <= 32 and not member.raw for member in page.items)
    assert reduced_off_loop["value"]
    assert connector.runner.calls[0] == [
        "print",
        "group-members",
        "group",
        "large@example.com",
        "fields",
        "email,role,type,status",
        "formatjson",
    ]


def test_group_member_partial_is_explicit_and_under_100kb(
    bounded_client,
    monkeypatch,
):
    members = [
        GroupMember(email=f"member{i:03d}@example.com")
        for i in range(50)
    ]

    async def member_page(group, limit=50):
        assert limit == 50
        return Page(members, None, 5_000, None, False)

    monkeypatch.setattr(
        bounded_client.app.state.gamgui.connector,
        "list_group_members_page",
        member_page,
    )
    response = bounded_client.get(
        "/groups/members",
        params={"group": "large@example.com"},
    )

    assert response.status_code == 200
    assert response.text.count("/users/detail?email=") == 50
    assert "Partial view: showing the first 50 of 5000" in response.text
    assert "hard-limited to 50" in response.text
    assert len(response.content) < 100_000
