from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gamgui.core.drive.models import OperationManifest, OperationTarget
from gamgui.core.drive.operations import DriveOperationStore
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(tmp_path) -> TestClient:
    vault = SecretsVault(backend=InMemoryBackend())
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    state = AppState(vault=vault, runner=runner, audit_domain="", connector=None, token="testtoken")
    return TestClient(create_app(state))


def test_healthz_is_open(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_root_requires_token(client):
    assert client.get("/").status_code == 403


def test_root_with_token_renders_and_sets_cookie(client):
    r = client.get("/?token=testtoken")
    assert r.status_code == 200
    assert "GamGUI" in r.text  # neutral product name
    assert "Not configured" in r.text  # no creds in the in-memory vault
    # cookie now set on the client -> a token-less follow-up is allowed
    assert client.get("/").status_code == 200


def test_wrong_token_forbidden(client):
    assert client.get("/?token=nope").status_code == 403


@pytest.mark.anyio
async def test_app_state_close_cancels_drive_job_tasks(tmp_path):
    vault = SecretsVault(backend=InMemoryBackend())
    runner = GAMRunner(
        vault=vault,
        gam_binary=FIXTURES / "mock_gam.sh",
        base_dir=tmp_path,
    )
    state = AppState(vault=vault, runner=runner)
    cancelled = asyncio.Event()

    async def drive_job():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(drive_job())
    state.jobs["drive"] = SimpleNamespace(task=task)
    await asyncio.sleep(0)
    await state.aclose()
    assert task.cancelled()
    assert cancelled.is_set()


@pytest.mark.anyio
async def test_connector_reactivation_cannot_interrupt_live_drive_manifest(tmp_path):
    vault = SecretsVault(backend=InMemoryBackend())
    runner = GAMRunner(
        vault=vault,
        gam_binary=FIXTURES / "mock_gam.sh",
        base_dir=tmp_path,
    )
    state = AppState(vault=vault, runner=runner)
    store = DriveOperationStore(tmp_path / "drive_operations.db")
    manifest = OperationManifest(
        id="live-manifest",
        domain="example.com",
        kind="folder_transfer",
        subject="alice@example.com",
        destination="bob@example.com",
        root_id="folder-1",
        target_hash="hash",
        targets=[
            OperationTarget("file-1", "One", "alice@example.com"),
        ],
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    store.create(manifest)
    assert store.claim_operation(
        manifest.id,
        manifest.domain,
        owner_id="blocked-drive-job",
        owner_pid=os.getpid(),
    )

    class Client:
        closed = False

        async def aclose(self):
            self.closed = True

    client = Client()
    old_connector = SimpleNamespace(domain="example.com")
    old_drive = SimpleNamespace(
        domain="example.com",
        operations=store,
        client=client,
    )
    state.connector = old_connector
    state.audit_domain = "example.com"
    state.drive_service = old_drive

    with pytest.raises(RuntimeError, match="active administrative operation"):
        state.activate_connector(SimpleNamespace(domain="other.example"))
    await asyncio.sleep(0)

    assert state.connector is old_connector
    assert state.drive_service is old_drive
    assert not client.closed
    assert store.get(manifest.id, manifest.domain).status == "running"


@pytest.mark.anyio
async def test_connector_activation_failure_preserves_complete_old_binding(
    tmp_path, monkeypatch
):
    vault = SecretsVault(backend=InMemoryBackend())
    runner = GAMRunner(
        vault=vault,
        gam_binary=FIXTURES / "mock_gam.sh",
        base_dir=tmp_path,
    )
    state = AppState(vault=vault, runner=runner)
    old_connector = SimpleNamespace(domain="example.com")
    old_directory = SimpleNamespace(
        domain="example.com",
        path=tmp_path / "directory.db",
    )
    old_course_index = object()
    old_manifests = SimpleNamespace(has_active_jobs=lambda: False)
    old_classroom = object()

    class Client:
        closed = False

        async def aclose(self):
            self.closed = True

    old_client = Client()
    old_drive = SimpleNamespace(
        domain="example.com",
        operations=SimpleNamespace(has_active_jobs=lambda: False),
        client=old_client,
    )
    state.connector = old_connector
    state.audit_domain = "example.com"
    state.directory_index = old_directory
    state.classroom_index = old_course_index
    state.classroom_manifests = old_manifests
    state.classroom_service = old_classroom
    state.drive_service = old_drive
    old_cache = state.user_cache

    def fail_store(*args, **kwargs):
        raise OSError("simulated staging failure")

    monkeypatch.setattr("gamgui.web.server.DriveOperationStore", fail_store)

    with pytest.raises(RuntimeError, match="existing connection remains active"):
        state.activate_connector(SimpleNamespace(domain="other.example"))
    await asyncio.sleep(0)

    assert state.connector is old_connector
    assert state.audit_domain == "example.com"
    assert state.directory_index is old_directory
    assert state.classroom_index is old_course_index
    assert state.classroom_manifests is old_manifests
    assert state.classroom_service is old_classroom
    assert state.drive_service is old_drive
    assert state.user_cache is old_cache
    assert not old_client.closed
