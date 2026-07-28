from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from gamgui.core.canary import (
    CANARY_CHECK_NAMES,
    CLASSROOM_SCOPE,
    GROUP_SCOPE,
    CanaryConfigStore,
    CanaryResultStore,
    DelegatedCanaryPageProbe,
    run_live_canary,
)


class FakeConnector:
    async def get_user(self, email, fields=None):
        return {"primaryEmail": email, "returned_id": "user-secret"}

class FakeDrive:
    async def list_owned_files(self, email, page_size=50):
        assert page_size == 1
        return {"files": [{"id": "private-drive-id", "owner": email}]}


class FakePageProbe:
    async def one_group_page(self, subject):
        return {"groups": [{"id": "private-group-id", "subject": subject}]}

    async def one_course_page(self, subject):
        return {"courses": [{"id": "private-course-id", "subject": subject}]}

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_delegated_page_probe_hard_bounds_group_and_course_reads():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    class Tokens:
        def __init__(self, token):
            self.token = token

        async def token_for(self, _subject):
            return self.token

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = DelegatedCanaryPageProbe(object(), "example.edu", http=http)
    assert probe.group_token_provider is not probe.classroom_token_provider
    assert probe.group_token_provider.scopes == (GROUP_SCOPE,)
    assert probe.classroom_token_provider.scopes == (CLASSROOM_SCOPE,)
    assert (GROUP_SCOPE, CLASSROOM_SCOPE) == (
        "https://www.googleapis.com/auth/admin.directory.group.readonly",
        "https://www.googleapis.com/auth/classroom.courses",
    )
    probe.group_token_provider = Tokens("group-token")
    probe.classroom_token_provider = Tokens("classroom-token")
    await probe.one_group_page("admin@example.edu")
    await probe.one_course_page("admin@example.edu")
    await http.aclose()

    assert requests[0].url.params["maxResults"] == "1"
    assert requests[1].url.params["pageSize"] == "1"
    assert requests[0].headers["Authorization"] == "Bearer group-token"
    assert requests[1].headers["Authorization"] == "Bearer classroom-token"


@pytest.mark.asyncio
async def test_group_authorization_failure_does_not_poison_classroom_token():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    class MissingGroupAuthorization:
        async def token_for(self, _subject):
            raise PermissionError("group scope is not authorized")

    class ClassroomTokens:
        async def token_for(self, _subject):
            return "classroom-token"

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = DelegatedCanaryPageProbe(object(), "example.edu", http=http)
    probe.group_token_provider = MissingGroupAuthorization()
    probe.classroom_token_provider = ClassroomTokens()

    with pytest.raises(PermissionError):
        await probe.one_group_page("admin@example.edu")
    await probe.one_course_page("admin@example.edu")
    await http.aclose()

    assert len(requests) == 1
    assert requests[0].url.params["pageSize"] == "1"
    assert requests[0].headers["Authorization"] == "Bearer classroom-token"


@pytest.mark.asyncio
async def test_live_canary_runs_bounded_checks_without_persisting_identifiers(tmp_path):
    config = CanaryConfigStore(tmp_path / "config.json")
    results = CanaryResultStore(tmp_path / "result.json")
    config.save("example.edu", "admin@example.edu")
    state = SimpleNamespace(
        connector=FakeConnector(),
        drive_service=FakeDrive(),
        audit_domain="example.edu",
    )

    result = await run_live_canary(
        state,
        config_store=config,
        result_store=results,
        page_probe=FakePageProbe(),
    )

    assert result["ok"]
    assert [check["name"] for check in result["checks"]] == [
        "users",
        "groups",
        "classroom",
        "drive",
    ]
    persisted = results.path.read_text(encoding="utf-8")
    assert "admin@example.edu" not in persisted
    assert "private-" not in persisted
    assert set(json.loads(persisted)) == {"ok", "checked_at", "checks"}


@pytest.mark.asyncio
async def test_canary_fails_closed_when_subject_is_not_configured(tmp_path):
    results = CanaryResultStore(tmp_path / "result.json")
    state = SimpleNamespace(
        connector=FakeConnector(),
        drive_service=FakeDrive(),
        audit_domain="example.edu",
    )

    result = await run_live_canary(
        state,
        config_store=CanaryConfigStore(tmp_path / "missing.json"),
        result_store=results,
        page_probe=FakePageProbe(),
    )

    assert not result["ok"]
    assert result["checks"] == []
    assert json.loads(results.path.read_text(encoding="utf-8"))["checks"] == []


@pytest.mark.asyncio
async def test_canary_discards_exception_text(tmp_path):
    class FailingConnector(FakeConnector):
        pass

    class FailingProbe(FakePageProbe):
        async def one_group_page(self, subject):
            raise RuntimeError(f"failed for {subject} with file private-id")

    config = CanaryConfigStore(tmp_path / "config.json")
    results = CanaryResultStore(tmp_path / "result.json")
    config.save("example.edu", "admin@example.edu")
    state = SimpleNamespace(
        connector=FailingConnector(),
        drive_service=FakeDrive(),
        audit_domain="example.edu",
    )

    result = await run_live_canary(
        state,
        config_store=config,
        result_store=results,
        page_probe=FailingProbe(),
    )

    assert not result["ok"]
    assert not next(check for check in result["checks"] if check["name"] == "groups")["ok"]
    assert next(check for check in result["checks"] if check["name"] == "classroom")["ok"]
    persisted = results.path.read_text(encoding="utf-8")
    assert "admin@example.edu" not in persisted
    assert "private-id" not in persisted


def test_result_store_drops_unknown_duplicate_and_invalid_checks(tmp_path):
    store = CanaryResultStore(tmp_path / "result.json")
    store.save(
        {
            "ok": True,
            "checked_at": 1.0,
            "checks": [
                {"name": "users", "ok": True, "duration_ms": 1.0},
                {"name": "users", "ok": True, "duration_ms": 2.0},
                {"name": "attacker-controlled", "ok": True, "duration_ms": 1.0},
                {"name": "groups", "ok": "yes", "duration_ms": 1.0},
                {"name": "classroom", "ok": True, "duration_ms": float("inf")},
            ],
        }
    )

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert [item["name"] for item in payload["checks"]] == ["users"]
    assert set(CANARY_CHECK_NAMES) == {"users", "groups", "classroom", "drive"}
