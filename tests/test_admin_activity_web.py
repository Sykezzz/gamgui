from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gamgui.core.activity import ActivityBusyError, ActivityRegistry
from gamgui.core.classroom.index import CourseIndex
from gamgui.core.classroom.manifests import RosterManifestStore
from gamgui.core.classroom.service import ClassroomService
from gamgui.core.drive.models import TransferResult
from gamgui.web.jobs import BatchJob
from gamgui.web.routes.classroom import _run_roster_job, router as classroom_router
from gamgui.web.routes.drive import _run_manifest_job, router as drive_router
from gamgui.web.routes.lifecycle import _run_offboard, router as lifecycle_router
from gamgui.web.routes.setup import router as setup_router
from gamgui.web.routes.users import _run_bulk_store, router as users_router
from tests.classroom_fakes import FakeClassroomConnector


def _client(router, state: object) -> TestClient:
    app = FastAPI()
    app.state.gamgui = state
    app.include_router(router)
    return TestClient(app)


def test_drive_single_transfer_refuses_while_another_activity_is_active():
    class Service:
        calls = 0

        async def transfer_file_ownership(self, *args, **kwargs):
            self.calls += 1
            return TransferResult(True, "file-1", "new@example.com")

    registry = ActivityRegistry()
    service = Service()
    state = SimpleNamespace(
        activity_registry=registry,
        drive_service=service,
        jobs={},
    )
    client = _client(drive_router, state)

    with registry.acquire("app-update"):
        response = client.post(
            "/drive/ownership/apply",
            data={
                "email": "owner@example.com",
                "file_id": "file-1",
                "destination": "new@example.com",
                "confirmation": "new@example.com",
            },
        )

    assert response.status_code == 200
    assert "CMP-ACTIVE-JOB" in response.text
    assert service.calls == 0


def test_classroom_roster_refuses_while_another_activity_is_active(tmp_path):
    connector = FakeClassroomConnector()
    index = CourseIndex(tmp_path / "courses.db")
    manifests = RosterManifestStore(tmp_path / "rosters.db")
    service = ClassroomService(connector, "example.com", index, manifests)
    manifest = asyncio.run(
        service.plan_roster(
            "123",
            "students",
            [
                "student1@example.com",
                "student2@example.com",
                "student3@example.com",
            ],
        )
    )
    registry = ActivityRegistry()
    state = SimpleNamespace(
        activity_registry=registry,
        connector=connector,
        audit_domain="example.com",
        classroom_index=index,
        classroom_manifests=manifests,
        classroom_service=service,
    )
    client = _client(classroom_router, state)

    with registry.acquire("component-profile-swap"):
        response = client.post(
            "/classroom/roster/apply",
            data={"manifest_id": manifest.id},
        )

    assert response.status_code == 200
    assert "CMP-ACTIVE-JOB" in response.text
    assert manifests.get(manifest.id).status == "planned"


def test_lifecycle_and_user_bulk_jobs_refuse_concurrent_starts():
    class State:
        def __init__(self):
            self.activity_registry = ActivityRegistry()
            self.connector = SimpleNamespace()
            self.jobs = {}
            self.invalidations = 0

        async def users(self):
            return [
                SimpleNamespace(
                    primary_email="person@example.com",
                    full_name="Person",
                    title="Teacher",
                    suspended=False,
                )
            ]

        def invalidate_users(self):
            self.invalidations += 1

    state = State()
    lifecycle_client = _client(lifecycle_router, state)
    users_client = _client(users_router, state)

    with state.activity_registry.acquire("component-profile-build"):
        lifecycle_response = lifecycle_client.post(
            "/lifecycle/offboard/run",
            data={
                "user": "person@example.com",
                "manager": "manager@example.com",
            },
        )
        bulk_response = users_client.post(
            "/users/bulk/apply",
            data={
                "store": "North",
                "emails": "person@example.com",
            },
        )

    assert "CMP-ACTIVE-JOB" in lifecycle_response.text
    assert "CMP-ACTIVE-JOB" in bulk_response.text
    assert state.jobs == {}


def test_setup_maps_connector_rebind_conflict_to_stable_error(monkeypatch):
    class State:
        vault = object()
        runner = SimpleNamespace(base_dir=None)

        @staticmethod
        def has_active_admin_jobs():
            return False

        @staticmethod
        def activate_connector(_connector):
            try:
                raise ActivityBusyError("classroom-roster-apply")
            except ActivityBusyError as exc:
                raise RuntimeError(
                    "Finish or stop the active administrative operation "
                    "before reconnecting."
                ) from exc

    async def verified(*_args, **_kwargs):
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(
        "gamgui.web.routes.setup.SetupService.verify",
        verified,
    )
    monkeypatch.setattr(
        "gamgui.web.routes.setup.GAMConnector",
        lambda **_kwargs: object(),
    )
    client = _client(setup_router, State())

    response = client.post(
        "/setup/verify",
        data={"domain": "example.com", "admin": "admin@example.com"},
    )

    assert response.status_code == 200
    assert "CMP-ACTIVE-JOB" in response.text
    assert "classroom-roster-apply" not in response.text


@pytest.mark.asyncio
async def test_background_lease_releases_after_success_and_failure():
    registry = ActivityRegistry()
    target = SimpleNamespace(
        primary_email="person@example.com",
        title="Teacher",
    )
    state = SimpleNamespace(invalidate_users=lambda: None)

    class SuccessfulConnector:
        async def set_organization(self, *_args, **_kwargs):
            return SimpleNamespace(ok=True)

    success_job = BatchJob("success", 1)
    await _run_bulk_store(
        success_job,
        state,
        SuccessfulConnector(),
        [target],
        "North",
        registry.acquire("users-bulk-store"),
    )
    assert success_job.finished and success_job.applied == 1
    assert not registry.is_active()

    class FailingDriveService:
        async def apply_manifest(self, *_args, **_kwargs):
            raise RuntimeError("private upstream detail")

    failure_job = BatchJob("failure", 1)
    await _run_manifest_job(
        failure_job,
        FailingDriveService(),
        "manifest",
        "confirm",
        registry.acquire("drive-ownership-manifest"),
    )
    assert failure_job.finished and failure_job.error
    assert not registry.is_active()


@pytest.mark.asyncio
async def test_background_lease_releases_when_task_is_cancelled():
    registry = ActivityRegistry()
    started = asyncio.Event()

    class BlockingClassroomService:
        async def apply_manifest(self, _manifest_id):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        _run_roster_job(
            BlockingClassroomService(),
            "manifest",
            {},
            registry.acquire("classroom-roster-apply"),
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not registry.is_active()


@pytest.mark.asyncio
async def test_lifecycle_lease_releases_when_step_fails():
    registry = ActivityRegistry()

    async def fail(_connector):
        raise RuntimeError("private upstream detail")

    step = SimpleNamespace(label="Transfer data", action=fail)
    job = BatchJob("offboard", 1)
    await _run_offboard(
        job,
        object(),
        [step],
        registry.acquire("lifecycle-offboard"),
    )

    assert job.finished and job.failed == ["Transfer data"]
    assert not registry.is_active()
