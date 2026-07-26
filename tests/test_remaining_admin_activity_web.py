from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gamgui.core.activity import ActivityRegistry
from gamgui.core.catalog import load_catalog
from gamgui.core.gam.models import GAMUser
from gamgui.core.onboarding import RunbookStore
from gamgui.core.signatures import SignatureStore
from gamgui.web.jobs import BatchJob
from gamgui.web.routes.builder import _run_sequence, router as builder_router
from gamgui.web.routes.calendars import _build_index, router as calendars_router
from gamgui.web.routes.groups import router as groups_router
from gamgui.web.routes.onboarding import router as onboarding_router
from gamgui.web.routes.signatures import ApplyJob, _run_apply, router as signatures_router


def _client(router, state: object, *, raise_server_exceptions: bool = True) -> TestClient:
    app = FastAPI()
    app.state.gamgui = state
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


class _MutationConnector:
    def __init__(self) -> None:
        self.calls = 0

    async def apply(self, _previews):
        self.calls += 1
        return [SimpleNamespace(ok=True, detail="")]

    async def add_group_member(self, _group, _email):
        self.calls += 1
        return SimpleNamespace(ok=True, detail="")

    async def remove_group_member(self, _group, _email):
        self.calls += 1
        return SimpleNamespace(ok=True, detail="")

    async def delete_event(self, _calendar, _event):
        self.calls += 1
        return SimpleNamespace(ok=True, detail="")

    async def create_onboarding_runbook(self, _assignee, _title, _steps):
        self.calls += 1
        return SimpleNamespace(ok=True, detail="")

    async def get_user(self, _email, **_kwargs):
        self.calls += 1
        return GAMUser.from_json({"primaryEmail": "person@example.com"})


def test_owned_admin_mutations_refuse_a_concurrent_activity(tmp_path):
    registry = ActivityRegistry()
    connector = _MutationConnector()

    builder_state = SimpleNamespace(
        activity_registry=registry,
        connector=connector,
        catalog=load_catalog(),
        builder_sequence=[{"sentinel": True}],
        jobs={},
    )
    groups_state = SimpleNamespace(activity_registry=registry, connector=connector)
    calendars_state = SimpleNamespace(activity_registry=registry, connector=connector)

    runbooks = RunbookStore(tmp_path / "onboarding.json")
    runbooks.set_role("Teacher", ["Create account"])
    onboarding_state = SimpleNamespace(
        activity_registry=registry,
        connector=connector,
        runbooks=runbooks,
    )
    signature_store = SignatureStore(tmp_path / "signatures.json")
    signatures_state = SimpleNamespace(
        activity_registry=registry,
        connector=connector,
        sig_templates=signature_store,
        jobs={},
    )

    with registry.acquire("component-profile-swap"):
        responses = [
            _client(builder_router, builder_state).post(
                "/builder/run",
                data={
                    "cid": "build.set_signature",
                    "email": "person@example.com",
                    "signature": "District",
                },
            ),
            _client(builder_router, builder_state).post("/builder/sequence/clear"),
            _client(groups_router, groups_state).post(
                "/groups/members",
                data={
                    "group": "teachers@example.com",
                    "email": "person@example.com",
                    "op": "add",
                },
            ),
            _client(calendars_router, calendars_state).post(
                "/calendars/event/delete",
                data={"cal": "calendar@example.com", "event_id": "event-1"},
            ),
            _client(onboarding_router, onboarding_state).post(
                "/onboard/run",
                data={
                    "role": "Teacher",
                    "email": "person@example.com",
                    "assignee": "admin@example.com",
                },
            ),
            _client(onboarding_router, onboarding_state).post(
                "/onboard/role",
                data={"name": "Principal", "steps": "Create account"},
            ),
            _client(signatures_router, signatures_state).post(
                "/signatures/apply",
                data={
                    "template": "District",
                    "scope_type": "user",
                    "scope_value": "person@example.com",
                },
            ),
            _client(signatures_router, signatures_state).post(
                "/signatures/templates/save",
                data={"name": "District", "template": "District"},
            ),
        ]

    assert all(response.status_code == 200 for response in responses)
    assert all("CMP-ACTIVE-JOB" in response.text for response in responses)
    assert connector.calls == 0
    assert builder_state.builder_sequence == [{"sentinel": True}]
    assert "Principal" not in runbooks.role_names()
    assert "District" not in signature_store.names()


def test_group_mutation_releases_lease_when_connector_fails():
    class FailingConnector:
        async def add_group_member(self, _group, _email):
            raise RuntimeError("private connector detail")

    registry = ActivityRegistry()
    state = SimpleNamespace(activity_registry=registry, connector=FailingConnector())
    response = _client(
        groups_router, state, raise_server_exceptions=False
    ).post(
        "/groups/members",
        data={
            "group": "teachers@example.com",
            "email": "person@example.com",
            "op": "add",
        },
    )

    assert response.status_code == 500
    assert not registry.is_active()


def test_background_start_failures_release_and_remove_partial_jobs(monkeypatch):
    from gamgui.web.routes import builder as builder_routes
    from gamgui.web.routes import calendars as calendar_routes
    from gamgui.web.routes import signatures as signature_routes

    def fail_create_task(coroutine):
        coroutine.close()
        raise RuntimeError("task creation failed")

    registry = ActivityRegistry()

    builder_state = SimpleNamespace(
        activity_registry=registry,
        connector=object(),
        builder_sequence=[
            {
                "target": "person@example.com",
                "label": "Change",
                "argv": ["user", "person@example.com", "signature", "District"],
                "risk": 1,
            }
        ],
        jobs={},
    )
    monkeypatch.setattr(
        builder_routes,
        "asyncio",
        SimpleNamespace(create_task=fail_create_task),
    )
    with pytest.raises(RuntimeError, match="task creation failed"):
        _client(builder_router, builder_state).post("/builder/sequence/run")
    assert builder_state.jobs == {}
    assert not registry.is_active()

    class SignatureConnector:
        async def get_user(self, _email, **_kwargs):
            return GAMUser.from_json({"primaryEmail": "person@example.com"})

    signature_state = SimpleNamespace(
        activity_registry=registry,
        connector=SignatureConnector(),
        jobs={},
    )
    monkeypatch.setattr(
        signature_routes,
        "asyncio",
        SimpleNamespace(create_task=fail_create_task),
    )
    with pytest.raises(RuntimeError, match="task creation failed"):
        _client(signatures_router, signature_state).post(
            "/signatures/apply",
            data={
                "template": "District",
                "scope_type": "user",
                "scope_value": "person@example.com",
            },
        )
    assert signature_state.jobs == {}
    assert not registry.is_active()

    calendar_state = SimpleNamespace(
        activity_registry=registry,
        connector=object(),
        calendar_index=object(),
        audit_domain="example.com",
        jobs={},
        cal_index_job_id="",
    )
    monkeypatch.setattr(
        calendar_routes,
        "asyncio",
        SimpleNamespace(create_task=fail_create_task),
    )
    with pytest.raises(RuntimeError, match="task creation failed"):
        _client(calendars_router, calendar_state).post("/calendars/index/rebuild")
    assert calendar_state.jobs == {}
    assert calendar_state.cal_index_job_id == ""
    assert not registry.is_active()


@pytest.mark.asyncio
async def test_owned_background_jobs_release_on_failure_and_cancellation():
    registry = ActivityRegistry()

    class FailingScanner:
        async def scan_all_calendars(self):
            raise RuntimeError("private scan detail")

    calendar_job = BatchJob("calendar", 0)
    await _build_index(
        calendar_job,
        FailingScanner(),
        object(),
        "example.com",
        registry.acquire("calendar-index-rebuild"),
    )
    assert calendar_job.finished and calendar_job.error
    assert not registry.is_active()

    class BlockingSignatureConnector:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def set_signature(self, *_args, **_kwargs):
            self.started.set()
            await asyncio.Event().wait()

    connector = BlockingSignatureConnector()
    user = GAMUser.from_json({"primaryEmail": "person@example.com"})
    signature_job = ApplyJob("signature", 1)
    task = asyncio.create_task(
        _run_apply(
            signature_job,
            connector,
            [user],
            "District",
            registry.acquire("signature-batch-apply"),
        )
    )
    await connector.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert signature_job.finished
    assert not registry.is_active()

    sequence_job = BatchJob("sequence", 1)

    class FailingApplyConnector:
        async def apply(self, _previews):
            raise RuntimeError("private apply detail")

    preview = SimpleNamespace(summary="Change", target="person@example.com")
    await _run_sequence(
        sequence_job,
        FailingApplyConnector(),
        [preview],
        registry.acquire("builder-sequence-run"),
    )
    assert sequence_job.finished and sequence_job.failed
    assert not registry.is_active()
