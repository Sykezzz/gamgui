from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gamgui.core.activity import ActivityRegistry
from gamgui.web.routes.classroom import (
    _schedule_refresh,
    router as classroom_router,
)
from gamgui.web.routes.drive import router as drive_router
from gamgui.web.routes.oneroster import router as oneroster_router
from gamgui.web.routes.setup import router as setup_router
from gamgui.web.routes.users import router as users_router


def _client(router, state: object, *, raise_server_exceptions: bool = True) -> TestClient:
    app = FastAPI()
    app.state.gamgui = state
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


class _NoCalls:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected mutation call: {name}")


class _CourseIndex:
    @staticmethod
    def status(_domain):
        return SimpleNamespace(count=0, age_seconds=None, stale=True)


class _ClassroomService(_NoCalls):
    def __init__(self, connector, index, manifests) -> None:
        self.connector = connector
        self.domain = "example.com"
        self.course_index = index
        self.manifests = manifests


class _ComponentManager:
    @staticmethod
    def status(_component_id):
        return {
            "state": "enabled",
            "installed": True,
            "enabled": True,
            "first_run_pending": False,
        }


def _oneroster_state(registry: ActivityRegistry, service: object) -> SimpleNamespace:
    return SimpleNamespace(
        activity_registry=registry,
        component_manager=_ComponentManager(),
        oneroster_service=service,
        oneroster_error_code="",
        audit_domain="example.com",
    )


def test_every_newly_covered_write_refuses_while_an_activity_is_active():
    registry = ActivityRegistry()

    users_state = SimpleNamespace(
        activity_registry=registry,
        connector=_NoCalls(),
        jobs={},
    )
    users = _client(users_router, users_state)

    connector = object()
    index = _CourseIndex()
    manifests = object()
    classroom_state = SimpleNamespace(
        activity_registry=registry,
        connector=connector,
        audit_domain="example.com",
        classroom_index=index,
        classroom_manifests=manifests,
        classroom_service=_ClassroomService(connector, index, manifests),
    )
    classroom = _client(classroom_router, classroom_state)

    drive_state = SimpleNamespace(
        activity_registry=registry,
        drive_service=_NoCalls(),
        jobs={},
    )
    drive = _client(drive_router, drive_state)
    setup = _client(
        setup_router,
        SimpleNamespace(activity_registry=registry),
    )
    oneroster = _client(
        oneroster_router,
        _oneroster_state(registry, _NoCalls()),
    )

    with registry.acquire("component-profile-swap"):
        responses = [
            users.post(
                "/users/signature",
                data={"email": "person@example.com", "signature": "District"},
            ),
            users.post("/users/signout", data={"email": "person@example.com"}),
            users.post(
                "/users/groups/add",
                data={
                    "email": "person@example.com",
                    "group": "teachers@example.com",
                },
            ),
            users.post(
                "/users/groups/remove",
                data={
                    "email": "person@example.com",
                    "group": "teachers@example.com",
                },
            ),
            users.post(
                "/users/delegate/add",
                data={
                    "email": "person@example.com",
                    "delegate": "delegate@example.com",
                },
            ),
            users.post(
                "/users/delegate/remove",
                data={
                    "email": "person@example.com",
                    "delegate": "delegate@example.com",
                },
            ),
            users.post(
                "/users/organization",
                data={"email": "person@example.com", "title": "Teacher"},
            ),
            users.post(
                "/users/calendar/add",
                data={
                    "email": "person@example.com",
                    "target": "viewer@example.com",
                },
            ),
            users.post(
                "/users/calendar/remove",
                data={
                    "email": "person@example.com",
                    "scope": "viewer@example.com",
                },
            ),
            users.post(
                "/users/delete/apply",
                data={
                    "email": "person@example.com",
                    "confirm": "person@example.com",
                },
            ),
            users.post(
                "/users/vacation/set",
                data={"email": "person@example.com", "subject": "Away"},
            ),
            users.post(
                "/users/vacation/off",
                data={"email": "person@example.com"},
            ),
            users.post(
                "/users/suspend/apply",
                data={"email": "person@example.com", "suspend": "on"},
            ),
            classroom.post("/classroom/index/refresh"),
            classroom.post(
                "/classroom/course",
                data={
                    "name": "English",
                    "owner_email": "teacher@example.com",
                },
            ),
            classroom.post(
                "/classroom/course/123/metadata",
                data={"name": "English"},
            ),
            classroom.post(
                "/classroom/course/123/state",
                data={"target_state": "ARCHIVED", "confirmed": "yes"},
            ),
            classroom.post(
                "/classroom/course/123/owner",
                data={
                    "target_email": "new@example.com",
                    "confirm_course_id": "123",
                    "confirm_target_email": "new@example.com",
                },
            ),
            classroom.post(
                "/classroom/course/123/roster/add",
                data={"role": "students", "email": "student@example.com"},
            ),
            classroom.post(
                "/classroom/course/123/roster/remove",
                data={"role": "students", "email": "student@example.com"},
            ),
            classroom.post(
                "/classroom/course/123/roster/preview",
                data={
                    "role": "students",
                    "csv_text": "email\nstudent@example.com\n",
                },
            ),
            classroom.post(
                "/classroom/roster/replan",
                data={"manifest_id": "manifest-1"},
            ),
            drive.post(
                "/drive/metadata",
                data={
                    "email": "owner@example.com",
                    "file_id": "file-1",
                    "name": "File",
                },
            ),
            drive.post(
                "/drive/permissions/add",
                data={
                    "email": "owner@example.com",
                    "file_id": "file-1",
                    "target": "viewer@example.com",
                },
            ),
            drive.post(
                "/drive/permissions/update",
                data={
                    "email": "owner@example.com",
                    "file_id": "file-1",
                    "permission_id": "permission-1",
                    "role": "writer",
                },
            ),
            drive.post(
                "/drive/permissions/remove",
                data={
                    "email": "owner@example.com",
                    "file_id": "file-1",
                    "permission_id": "permission-1",
                },
            ),
            drive.post(
                "/drive/manifest/folder",
                data={
                    "email": "owner@example.com",
                    "file_id": "folder-1",
                    "destination": "new@example.com",
                },
            ),
            drive.post(
                "/drive/manifest/classroom",
                data={
                    "teacher": "teacher@example.com",
                    "folder_id": "folder-1",
                },
            ),
            setup.post(
                "/setup/import",
                data={
                    "domain": "example.com",
                    "admin": "admin@example.com",
                    "config_dir": "/private/config",
                },
            ),
            oneroster.post(
                "/classroom/imports/upload",
                files={
                    "package": (
                        "district.zip",
                        b"PKfixture",
                        "application/zip",
                    )
                },
            ),
            oneroster.post(
                "/classroom/imports/import/import-1/validate"
            ),
            oneroster.post(
                "/classroom/imports/thresholds",
                data={"mode": "normal"},
            ),
            oneroster.post(
                "/classroom/imports/gate",
                data={"target_state": "CLOSED"},
            ),
        ]

    assert all(response.status_code == 200 for response in responses)
    missing = [
        f"{response.request.method} {response.request.url.path}: {response.text[:160]}"
        for response in responses
        if "CMP-ACTIVE-JOB" not in response.text
    ]
    assert not missing, "\n".join(missing)


def test_invalid_oneroster_upload_validates_before_acquiring_activity():
    registry = ActivityRegistry()
    client = _client(
        oneroster_router,
        _oneroster_state(registry, _NoCalls()),
    )

    with registry.acquire("component-profile-swap"):
        response = client.post(
            "/classroom/imports/upload",
            files={"package": ("roster.csv", b"users", "text/csv")},
        )

    assert "OR-ZIP-INVALID" in response.text
    assert "CMP-ACTIVE-JOB" not in response.text


def test_new_write_leases_release_when_the_operation_fails(monkeypatch):
    registry = ActivityRegistry()

    class FailingUserConnector:
        async def set_signature(self, *_args, **_kwargs):
            raise RuntimeError("private user failure")

    user_response = _client(
        users_router,
        SimpleNamespace(
            activity_registry=registry,
            connector=FailingUserConnector(),
        ),
        raise_server_exceptions=False,
    ).post(
        "/users/signature",
        data={"email": "person@example.com", "signature": "District"},
    )
    assert user_response.status_code == 500
    assert not registry.is_active()

    class FailingDriveService:
        async def update_metadata(self, *_args, **_kwargs):
            raise RuntimeError("private drive failure")

    drive_response = _client(
        drive_router,
        SimpleNamespace(
            activity_registry=registry,
            drive_service=FailingDriveService(),
        ),
    ).post(
        "/drive/metadata",
        data={
            "email": "owner@example.com",
            "file_id": "file-1",
            "name": "File",
        },
    )
    assert "Something went wrong talking to Google Drive" in drive_response.text
    assert not registry.is_active()

    connector = object()
    index = _CourseIndex()
    manifests = object()

    class FailingClassroomService(_ClassroomService):
        async def create_course(self, **_kwargs):
            raise RuntimeError("private Classroom failure")

    classroom_response = _client(
        classroom_router,
        SimpleNamespace(
            activity_registry=registry,
            connector=connector,
            audit_domain="example.com",
            classroom_index=index,
            classroom_manifests=manifests,
            classroom_service=FailingClassroomService(
                connector, index, manifests
            ),
        ),
    ).post(
        "/classroom/course",
        data={
            "name": "English",
            "owner_email": "teacher@example.com",
        },
    )
    assert "Classroom could not complete" in classroom_response.text
    assert not registry.is_active()

    class FailingOneRosterService:
        def upload(self, *_args, **_kwargs):
            raise RuntimeError("private OneRoster failure")

    oneroster_response = _client(
        oneroster_router,
        _oneroster_state(registry, FailingOneRosterService()),
    ).post(
        "/classroom/imports/upload",
        files={
            "package": (
                "district.zip",
                b"PKfixture",
                "application/zip",
            )
        },
    )
    assert "OR-ZIP-INVALID" in oneroster_response.text
    assert not registry.is_active()

    def fail_import(*_args, **_kwargs):
        raise RuntimeError("private import failure")

    monkeypatch.setattr(
        "gamgui.web.routes.setup.SetupService.import_dir",
        fail_import,
    )
    setup_response = _client(
        setup_router,
        SimpleNamespace(
            activity_registry=registry,
            vault=object(),
            runner=object(),
        ),
        raise_server_exceptions=False,
    ).post(
        "/setup/import",
        data={
            "domain": "example.com",
            "admin": "admin@example.com",
            "config_dir": "/private/config",
        },
    )
    assert setup_response.status_code == 500
    assert not registry.is_active()


@pytest.mark.asyncio
async def test_classroom_index_refresh_releases_when_cancelled():
    registry = ActivityRegistry()
    started = asyncio.Event()

    class BlockingService:
        async def refresh_index(self):
            started.set()
            await asyncio.Event().wait()

    state = SimpleNamespace(
        activity_registry=registry,
        classroom_refresh_task=None,
        classroom_refresh_error="",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(gamgui=state))
    )

    _schedule_refresh(request, BlockingService())
    await started.wait()
    assert registry.is_active()
    state.classroom_refresh_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await state.classroom_refresh_task
    assert not registry.is_active()
