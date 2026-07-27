from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog, read_records
from gamgui.core.drive.client import (
    LIST_FIELDS,
    DriveAPIClient,
    DriveAPIError,
    escape_query_literal,
)
from gamgui.core.drive.models import (
    GOOGLE_FOLDER_MIME,
    DriveFile,
    DrivePage,
    DrivePermission,
    OperationManifest,
    OperationTarget,
    TransferResult,
)
from gamgui.core.drive import operations as operations_module
from gamgui.core.drive.operations import DriveOperationStore
from gamgui.core.drive.service import (
    DriveSafetyError,
    DriveService,
    InternalPrincipal,
)
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.processes import ProcessProbe, ProcessState
from gamgui.web.routes.drive import router as drive_router


@pytest.fixture
def anyio_backend():
    return "asyncio"


class StaticToken:
    async def token_for(self, subject: str) -> str:
        return "not-persisted"


def file_data(**overrides):
    data = {
        "id": "file-1",
        "name": "District plan.pdf",
        "mimeType": "application/pdf",
        "size": "1024",
        "createdTime": "2026-01-02T03:04:05Z",
        "modifiedTime": "2026-02-03T04:05:06Z",
        "webViewLink": "https://drive.google.com/open?id=file-1",
        "parents": ["root"],
        "shared": True,
        "ownedByMe": True,
        "owners": [{"emailAddress": "alice@example.com", "displayName": "Alice"}],
        "capabilities": {
            "canDownload": True,
            "canEdit": True,
            "canShare": True,
            "canAcceptOwnership": False,
        },
    }
    data.update(overrides)
    return data


def test_drive_models_tolerate_shared_drive_and_inherited_permission():
    file = DriveFile.from_api(
        file_data(owners=[], driveId="shared-123", size=None, ownedByMe=False)
    )
    assert file.owner_email == ""
    assert file.size is None
    assert file.is_shared_drive

    permission = DrivePermission.from_api(
        {
            "id": "perm-1",
            "type": "user",
            "role": "writer",
            "emailAddress": "person@example.com",
            "permissionDetails": [{"inherited": True, "inheritedFrom": "folder-1"}],
        }
    )
    assert permission.inherited
    assert permission.inherited_from == "folder-1"
    assert not permission.removable


def _contrast(foreground: str, background: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]

        def linear(channel: float) -> float:
            if channel <= 0.04045:
                return channel / 12.92
            return ((channel + 0.055) / 1.055) ** 2.4

        red, green, blue = (linear(channel) for channel in channels)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_drive_ui_uses_accessible_brand_roles():
    assert _contrast("#52647B", "#FFFFFF") >= 4.5  # brand-blueink text
    assert _contrast("#69829E", "#FFFFFF") >= 3.0  # control borders/focus
    template_dir = Path(__file__).parents[1] / "gamgui" / "web" / "templates"
    for path in template_dir.glob("_drive*.html"):
        source = path.read_text(encoding="utf-8")
        assert "text-brand-gray" not in source, path.name
        assert not re.search(r"\btext-brand-blue(?:\s|\"|$)", source), path.name
        assert not re.search(r"\bbg-brand-blue(?:\s|\"|$)", source), path.name


def test_drive_query_escape_handles_apostrophes_backslashes_and_shell_text():
    value = r"O'Brien\$(touch nope)"
    assert escape_query_literal(value) == r"O\'Brien\\$(touch nope)"


@pytest.mark.anyio
async def test_drive_list_is_bounded_uses_opaque_cursor_and_narrow_fields():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "files": [file_data()],
                "nextPageToken": "opaque+/token==",
                "incompleteSearch": False,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DriveAPIClient(StaticToken(), http=http)
        page = await client.list_owned_files(
            "alice@example.com",
            search=r"O'Brien\Docs",
            cursor="input+/cursor==",
            page_size=5000,
        )

    request = captured["request"]
    assert request.url.params["pageSize"] == "50"
    assert request.url.params["pageToken"] == "input+/cursor=="
    assert request.url.params["fields"] == LIST_FIELDS
    assert "'me' in owners" in request.url.params["q"]
    assert r"name contains 'O\'Brien\\Docs'" in request.url.params["q"]
    assert page.next_cursor == "opaque+/token=="
    assert len(page.items) == 1


@pytest.mark.anyio
async def test_drive_client_retries_rate_limit_without_leaking_token():
    calls = []

    async def no_sleep(_seconds: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "message": "slow down",
                        "errors": [{"reason": "userRateLimitExceeded"}],
                    }
                },
            )
        return httpx.Response(200, json={"files": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DriveAPIClient(StaticToken(), http=http, sleep=no_sleep)
        await client.list_owned_files("alice@example.com")
    assert len(calls) == 2
    assert calls[0].headers["authorization"] == "Bearer not-persisted"


@pytest.mark.anyio
async def test_drive_client_maps_permission_error_to_friendly_message():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {"message": "forbidden", "errors": [{"reason": "forbidden"}]}
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DriveAPIClient(StaticToken(), http=http)
        with pytest.raises(DriveAPIError) as error:
            await client.get_file("alice@example.com", "x")
    assert "delegated account" in error.value.user_message


class FakeRunner:
    def __init__(self):
        self.calls = []

    async def run_authenticated(self, domain, argv, serialize=False):
        self.calls.append((domain, argv, serialize))
        return ""


class FakeResolver:
    def __init__(self, active=True):
        self.active = active
        self.calls = []

    async def resolve(self, email, principal_type):
        self.calls.append((email, principal_type))
        if email.startswith("external"):
            return None
        return InternalPrincipal(
            email=email.lower(), type=principal_type, active=self.active
        )


class FakeDriveClient:
    def __init__(self):
        self.files = {
            ("alice@example.com", "file-1"): DriveFile.from_api(file_data()),
            (
                "bob@example.com",
                "file-1",
            ): DriveFile.from_api(
                file_data(
                    owners=[{"emailAddress": "bob@example.com", "displayName": "Bob"}]
                )
            ),
        }
        self.permissions = [
            DrivePermission(
                id="owner",
                type="user",
                role="owner",
                email_address="alice@example.com",
            ),
            DrivePermission(
                id="writer",
                type="user",
                role="writer",
                email_address="bob@example.com",
            ),
            DrivePermission(
                id="inherited",
                type="group",
                role="reader",
                email_address="all@example.com",
                inherited=True,
            ),
        ]
        self.download_calls = []
        self.deleted = []
        self.created = []
        self.updated_files = []
        self.list_calls = []

    async def list_owned_files(self, subject, **kwargs):
        self.list_calls.append((subject, kwargs))
        return DrivePage([self.files[("alice@example.com", "file-1")]], "next-token")

    async def get_file(self, subject, file_id):
        return (
            self.files.get((subject, file_id))
            or self.files[("alice@example.com", file_id)]
        )

    async def list_permissions(self, subject, file_id):
        return list(self.permissions)

    async def delete_permission(self, subject, file_id, permission_id):
        self.deleted.append((subject, file_id, permission_id))
        self.permissions = [p for p in self.permissions if p.id != permission_id]

    async def create_permission(self, subject, file_id, *, principal_type, email, role):
        self.created.append((subject, file_id, principal_type, email, role))
        return DrivePermission("new", principal_type, role, email_address=email)

    async def update_permission(self, subject, file_id, permission_id, role):
        return DrivePermission(
            permission_id, "user", role, email_address="bob@example.com"
        )

    async def update_file(self, subject, file_id, changes):
        self.updated_files.append((subject, file_id, changes))
        return self.files[("alice@example.com", "file-1")]

    async def download(self, subject, file_id, **kwargs):
        self.download_calls.append((subject, file_id, kwargs))
        return b"%PDF-safe"

    async def list_children(self, subject, parent_id, **kwargs):
        return DrivePage([])


def drive_service(tmp_path: Path, client=None, resolver=None):
    return DriveService(
        client or FakeDriveClient(),
        FakeRunner(),
        "example.com",
        audit=AuditLog(tmp_path / "audit.jsonl"),
        resolver=resolver or FakeResolver(),
        operations=DriveOperationStore(tmp_path / "drive_operations.db"),
    )


@pytest.mark.anyio
async def test_new_shares_require_directory_resolved_internal_principal(tmp_path):
    service = drive_service(tmp_path)
    permission = await service.add_permission(
        "alice@example.com",
        "file-1",
        email="teacher@example.com",
        principal_type="user",
        role="writer",
    )
    assert permission.email_address == "teacher@example.com"
    with pytest.raises(DriveSafetyError, match="not found"):
        await service.add_permission(
            "alice@example.com",
            "file-1",
            email="external@partner.org",
            principal_type="user",
            role="reader",
        )


@pytest.mark.anyio
async def test_owner_and_inherited_permissions_are_protected(tmp_path):
    service = drive_service(tmp_path)
    with pytest.raises(DriveSafetyError, match="Owner and inherited"):
        await service.remove_permission("alice@example.com", "file-1", "owner")
    with pytest.raises(DriveSafetyError, match="Owner and inherited"):
        await service.remove_permission("alice@example.com", "file-1", "inherited")
    await service.remove_permission("alice@example.com", "file-1", "writer")
    assert service.client.deleted == [("alice@example.com", "file-1", "writer")]


@pytest.mark.anyio
async def test_preview_allowlist_export_and_size_guard(tmp_path):
    client = FakeDriveClient()
    service = drive_service(tmp_path, client=client)
    preview = await service.preview("alice@example.com", "file-1")
    assert preview.media_type == "application/pdf"
    assert preview.body == b"%PDF-safe"

    client.files[("alice@example.com", "file-1")] = DriveFile.from_api(
        file_data(mimeType="text/html")
    )
    with pytest.raises(DriveSafetyError, match="not safe"):
        await service.preview("alice@example.com", "file-1")
    assert len(client.download_calls) == 1

    client.files[("alice@example.com", "file-1")] = DriveFile.from_api(
        file_data(size=str(11 * 1024 * 1024))
    )
    with pytest.raises(DriveSafetyError, match="10 MB"):
        await service.preview("alice@example.com", "file-1")


@pytest.mark.anyio
async def test_google_doc_preview_exports_pdf(tmp_path):
    client = FakeDriveClient()
    client.files[("alice@example.com", "file-1")] = DriveFile.from_api(
        file_data(mimeType="application/vnd.google-apps.document", size=None)
    )
    service = drive_service(tmp_path, client=client)
    preview = await service.preview("alice@example.com", "file-1")
    assert preview.exported
    assert preview.filename.endswith(".pdf")
    assert client.download_calls[0][2]["export_mime"] == "application/pdf"


def test_transfer_and_claim_command_shapes_are_exact():
    assert GAMCommands.transfer_drive_ownership(
        "a@example.com", "abc", "b@example.com"
    ) == [
        "user",
        "a@example.com",
        "transfer",
        "ownership",
        "id:abc",
        "b@example.com",
        "norecursion",
    ]
    assert GAMCommands.claim_drive_ownership(
        "teacher@example.com", "abc", "student@example.com"
    ) == [
        "user",
        "teacher@example.com",
        "claim",
        "ownership",
        "id:abc",
        "onlyusers",
        "student@example.com",
        "retainrole",
        "none",
    ]


@pytest.mark.anyio
async def test_single_transfer_requires_confirmation_norecursion_and_verifies_owner(
    tmp_path,
):
    client = FakeDriveClient()
    service = drive_service(tmp_path, client=client)
    with pytest.raises(DriveSafetyError, match="exact destination"):
        await service.transfer_file_ownership(
            "alice@example.com", "file-1", "bob@example.com", confirmation="wrong"
        )
    result = await service.transfer_file_ownership(
        "alice@example.com",
        "file-1",
        "bob@example.com",
        confirmation="bob@example.com",
    )
    assert result.ok
    assert service.runner.calls[0][1][-1] == "norecursion"
    assert service.runner.calls[0][2] is True
    # The previous owner's writer permission is absent in this fixture, so no residual warning.
    assert not result.residual_access


@pytest.mark.anyio
async def test_single_transfer_rejects_shared_drive(tmp_path):
    client = FakeDriveClient()
    client.files[("alice@example.com", "file-1")] = DriveFile.from_api(
        file_data(driveId="shared-1")
    )
    service = drive_service(tmp_path, client=client)
    with pytest.raises(DriveSafetyError, match="organization-owned"):
        await service.transfer_file_ownership(
            "alice@example.com",
            "file-1",
            "bob@example.com",
            confirmation="bob@example.com",
        )
    assert not service.runner.calls


def sample_manifest() -> OperationManifest:
    return OperationManifest(
        id="manifest-1",
        domain="example.com",
        kind="folder_transfer",
        subject="alice@example.com",
        destination="bob@example.com",
        root_id="folder-1",
        target_hash="hash",
        targets=[
            OperationTarget("file-1", "One", "alice@example.com"),
            OperationTarget("file-2", "Two", "alice@example.com"),
        ],
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def test_operation_store_is_domain_isolated_and_recovers_running_targets(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    path = tmp_path / "ops.db"
    store = DriveOperationStore(path)
    store.create(sample_manifest())
    assert store.get("manifest-1", "other.example") is None
    assert store.claim_operation(
        "manifest-1",
        "example.com",
        owner_id="dead-executor",
        owner_pid=child.pid,
        owner_identity="dead-process",
    )
    store.set_target_status(
        "manifest-1",
        "example.com",
        "file-1",
        "running",
        owner_id="dead-executor",
    )
    recovered = DriveOperationStore(path)
    manifest = recovered.get("manifest-1", "example.com")
    assert manifest.status == "interrupted"
    assert manifest.targets[0].status == "interrupted"


def test_legacy_drive_claim_without_pid_stays_fail_closed(tmp_path):
    path = tmp_path / "ops.db"
    first = DriveOperationStore(path)
    first.create(sample_manifest())
    assert first.claim_operation(
        "manifest-1",
        "example.com",
        owner_id="legacy-executor",
        owner_pid=0,
        owner_identity="",
    )

    reopened = DriveOperationStore(path)

    assert reopened.get("manifest-1", "example.com").status == "running"


def test_second_store_preserves_a_live_operation_claim(tmp_path):
    path = tmp_path / "ops.db"
    first = DriveOperationStore(path)
    first.create(sample_manifest())
    assert first.claim_operation(
        "manifest-1",
        "example.com",
        owner_id="live-executor",
        owner_pid=os.getpid(),
    )
    first.set_target_status(
        "manifest-1",
        "example.com",
        "file-1",
        "running",
        owner_id="live-executor",
    )

    second = DriveOperationStore(path)
    manifest = second.get("manifest-1", "example.com")

    assert manifest.status == "running"
    assert manifest.targets[0].status == "running"
    assert not second.claim_operation("manifest-1", "example.com")


def test_unknown_process_probe_never_recovers_a_running_operation(
    tmp_path, monkeypatch
):
    path = tmp_path / "ops.db"
    first = DriveOperationStore(path)
    first.create(sample_manifest())
    assert first.claim_operation(
        "manifest-1",
        "example.com",
        owner_id="uncertain-executor",
        owner_pid=424242,
        owner_identity="known-start",
    )
    monkeypatch.setattr(
        "gamgui.core.processes.probe_process",
        lambda _pid: ProcessProbe(ProcessState.UNKNOWN),
    )

    reopened = DriveOperationStore(path)

    assert reopened.get("manifest-1", "example.com").status == "running"


def test_process_identity_mismatch_recovers_reused_pid(tmp_path, monkeypatch):
    path = tmp_path / "ops.db"
    first = DriveOperationStore(path)
    first.create(sample_manifest())
    assert first.claim_operation(
        "manifest-1",
        "example.com",
        owner_id="old-executor",
        owner_pid=12345,
        owner_identity="old-start",
    )
    monkeypatch.setattr(
        "gamgui.core.processes.probe_process",
        lambda _pid: ProcessProbe(ProcessState.ALIVE, "new-start"),
    )

    reopened = DriveOperationStore(path)

    assert reopened.get("manifest-1", "example.com").status == "interrupted"


def test_dead_child_process_lease_is_recovered(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    path = tmp_path / "ops.db"
    first = DriveOperationStore(path)
    first.create(sample_manifest())
    assert first.claim_operation(
        "manifest-1",
        "example.com",
        owner_id="dead-child",
        owner_pid=child.pid,
        owner_identity="child-start",
    )

    reopened = DriveOperationStore(path)

    assert reopened.get("manifest-1", "example.com").status == "interrupted"


def test_stale_drive_owner_cannot_write_target_or_terminal_status(tmp_path):
    store = DriveOperationStore(tmp_path / "ops.db")
    store.create(sample_manifest())
    assert store.claim_operation(
        "manifest-1",
        "example.com",
        owner_id="current-owner",
    )

    with pytest.raises(PermissionError, match="another executor"):
        store.set_target_status(
            "manifest-1",
            "example.com",
            "file-1",
            "running",
            owner_id="stale-owner",
        )
    with pytest.raises(PermissionError, match="another executor"):
        store.set_operation_status(
            "manifest-1",
            "example.com",
            "completed",
            owner_id="stale-owner",
        )

    unchanged = store.get("manifest-1", "example.com")
    assert unchanged.status == "running"
    assert unchanged.targets[0].status == "pending"


def test_drive_store_permission_failure_prevents_database_creation(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "private" / "ops.db"
    original_chmod = operations_module.os.chmod

    def fail_directory_chmod(candidate, mode):
        if Path(candidate) == path.parent:
            raise PermissionError("policy denied")
        return original_chmod(candidate, mode)

    monkeypatch.setattr(operations_module.os, "chmod", fail_directory_chmod)

    with pytest.raises(PermissionError, match="policy denied"):
        DriveOperationStore(path)

    assert not path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner-only mode assertion")
def test_drive_store_uses_owner_only_directory_and_database_modes(tmp_path):
    path = tmp_path / "private" / "ops.db"

    DriveOperationStore(path)

    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_drive_store_tolerates_only_disappearing_sqlite_companions(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "ops.db"
    store = DriveOperationStore(path)
    original = operations_module._secure_private_file

    def disappear_companions(candidate):
        if str(candidate).endswith(("-wal", "-shm")):
            raise FileNotFoundError(candidate)
        return original(candidate)

    monkeypatch.setattr(
        operations_module,
        "_secure_private_file",
        disappear_companions,
    )
    store._secure_files()

    def reject_companion(candidate):
        if str(candidate).endswith("-shm"):
            raise PermissionError("unsafe companion")
        return original(candidate)

    monkeypatch.setattr(
        operations_module,
        "_secure_private_file",
        reject_companion,
    )
    with pytest.raises(PermissionError, match="unsafe companion"):
        store._secure_files()


def test_drive_store_missing_main_database_stays_fail_closed(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "ops.db"
    store = DriveOperationStore(path)
    original = operations_module._secure_private_file

    def disappear_main(candidate):
        if Path(candidate) == path:
            raise FileNotFoundError(candidate)
        return original(candidate)

    monkeypatch.setattr(
        operations_module,
        "_secure_private_file",
        disappear_main,
    )
    with pytest.raises(FileNotFoundError):
        store._secure_files()


class TooLargeTreeClient(FakeDriveClient):
    def __init__(self):
        super().__init__()
        self.files[("alice@example.com", "file-1")] = DriveFile.from_api(
            file_data(
                id="file-1",
                name="Root",
                mimeType=GOOGLE_FOLDER_MIME,
            )
        )

    async def list_children(self, subject, parent_id, **kwargs):
        return DrivePage(
            [
                DriveFile.from_api(
                    file_data(id=f"child-{i}", name=f"Child {i}", size="1")
                )
                for i in range(500)
            ]
        )


@pytest.mark.anyio
async def test_folder_manifest_hard_caps_at_500(tmp_path):
    service = drive_service(tmp_path, client=TooLargeTreeClient())
    with pytest.raises(DriveSafetyError, match="500-file"):
        await service.plan_folder_transfer(
            "alice@example.com", "file-1", "bob@example.com"
        )


@pytest.mark.anyio
async def test_manifest_rejects_incomplete_recursive_drive_listing(tmp_path):
    class IncompleteTreeClient(FakeDriveClient):
        def __init__(self):
            super().__init__()
            self.files[("alice@example.com", "file-1")] = DriveFile.from_api(
                file_data(mimeType=GOOGLE_FOLDER_MIME)
            )

        async def list_children(self, subject, parent_id, **kwargs):
            return DrivePage([], incomplete_search=True)

    service = drive_service(tmp_path, client=IncompleteTreeClient())
    with pytest.raises(DriveSafetyError, match="incomplete folder listing"):
        await service.plan_folder_transfer(
            "alice@example.com",
            "file-1",
            "bob@example.com",
        )


@pytest.mark.anyio
async def test_manifest_apply_persists_per_file_completion(tmp_path):
    service = drive_service(tmp_path)
    manifest = service._create_manifest(
        kind="folder_transfer",
        subject="alice@example.com",
        destination="bob@example.com",
        root_id="file-1",
        targets=[OperationTarget("file-1", "District plan.pdf", "alice@example.com")],
    )
    result = await service.apply_manifest(manifest.id, confirmation="bob@example.com")
    assert result.status == "completed"
    assert result.targets[0].status == "succeeded"
    persisted = service.operations.get(manifest.id, "example.com")
    assert persisted.targets[0].status == "succeeded"


@pytest.mark.anyio
async def test_manifest_apply_atomically_rejects_concurrent_executor(tmp_path):
    service = drive_service(tmp_path)
    manifest = service._create_manifest(
        kind="folder_transfer",
        subject="alice@example.com",
        destination="bob@example.com",
        root_id="file-1",
        targets=[OperationTarget("file-1", "District plan.pdf", "alice@example.com")],
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def slow_transfer(source, file_id, destination, *, confirmation):
        calls.append((source, file_id, destination, confirmation))
        started.set()
        await release.wait()
        return TransferResult(True, file_id, destination, "transferred")

    service.transfer_file_ownership = slow_transfer
    first = asyncio.create_task(
        service.apply_manifest(manifest.id, confirmation="bob@example.com")
    )
    await started.wait()
    try:
        with pytest.raises(DriveSafetyError, match="already running"):
            await service.apply_manifest(
                manifest.id,
                confirmation="bob@example.com",
            )
    finally:
        release.set()
    result = await first
    assert result.status == "completed"
    assert result.targets[0].status == "succeeded"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_classroom_claim_allows_suspended_internal_source_owner(tmp_path):
    class SourceAwareResolver:
        calls = []

        async def resolve(self, email, principal_type):
            self.calls.append((email, principal_type))
            return InternalPrincipal(
                email=email.lower(),
                type=principal_type,
                active=not email.startswith("suspended"),
            )

    class ClaimClient(FakeDriveClient):
        def __init__(self):
            super().__init__()
            self.reads = 0

        async def get_file(self, subject, file_id):
            self.reads += 1
            owner = (
                "suspended-student@example.com"
                if self.reads == 1
                else "teacher@example.com"
            )
            return DriveFile.from_api(
                file_data(
                    id=file_id,
                    owners=[{"emailAddress": owner}],
                )
            )

    resolver = SourceAwareResolver()
    service = drive_service(
        tmp_path,
        client=ClaimClient(),
        resolver=resolver,
    )
    result = await service._claim_one(
        OperationTarget(
            "file-1",
            "Student work",
            "suspended-student@example.com",
        ),
        "teacher@example.com",
    )
    assert result.ok
    assert (
        "suspended-student@example.com",
        "user",
    ) in resolver.calls
    assert service.runner.calls


@pytest.mark.anyio
async def test_failed_claim_post_verification_is_audited(tmp_path):
    class UnchangedOwnerClient(FakeDriveClient):
        async def get_file(self, subject, file_id):
            return DriveFile.from_api(
                file_data(
                    id=file_id,
                    owners=[{"emailAddress": "student@example.com"}],
                )
            )

    service = drive_service(tmp_path, client=UnchangedOwnerClient())
    result = await service._claim_one(
        OperationTarget(
            "file-1",
            "Student work",
            "student@example.com",
        ),
        "teacher@example.com",
    )
    assert not result.ok
    record = read_records(service.audit.path)[-1]
    assert record["action"] == "drive_claim_ownership"
    assert record["ok"] is False
    assert "verification failed" in record["extra"]["error"].lower()


@pytest.mark.anyio
async def test_single_transfer_audits_unavailable_verification_after_gam(tmp_path):
    class VerificationUnavailableClient(FakeDriveClient):
        async def get_file(self, subject, file_id):
            if subject == "bob@example.com":
                raise RuntimeError("private upstream response")
            return await super().get_file(subject, file_id)

    service = drive_service(tmp_path, client=VerificationUnavailableClient())
    result = await service.transfer_file_ownership(
        "alice@example.com",
        "file-1",
        "bob@example.com",
        confirmation="bob@example.com",
    )
    assert not result.ok
    assert "verification was unavailable" in result.detail
    record = read_records(service.audit.path)[-1]
    assert record["action"] == "drive_transfer_ownership"
    assert record["ok"] is False
    assert record["extra"]["error"] == "Verification unavailable after GAM success."
    assert "private upstream response" not in str(record)


@pytest.mark.anyio
async def test_claim_audits_unavailable_verification_after_gam(tmp_path):
    class VerificationUnavailableClient(FakeDriveClient):
        def __init__(self):
            super().__init__()
            self.reads = 0

        async def get_file(self, subject, file_id):
            self.reads += 1
            if self.reads > 1:
                raise RuntimeError("private upstream response")
            return DriveFile.from_api(
                file_data(
                    id=file_id,
                    owners=[{"emailAddress": "student@example.com"}],
                )
            )

    service = drive_service(tmp_path, client=VerificationUnavailableClient())
    result = await service._claim_one(
        OperationTarget(
            "file-1",
            "Student work",
            "student@example.com",
        ),
        "teacher@example.com",
    )
    assert not result.ok
    assert "verification was unavailable" in result.detail
    record = read_records(service.audit.path)[-1]
    assert record["action"] == "drive_claim_ownership"
    assert record["ok"] is False
    assert record["extra"]["error"] == "Verification unavailable after GAM success."
    assert "private upstream response" not in str(record)


class FakeWebService:
    domain = "example.com"

    def __init__(self, tmp_path):
        self.calls = []
        self.file = DriveFile.from_api(file_data())
        self.operations = DriveOperationStore(tmp_path / "web_ops.db")

    async def list_owned_files(self, email, **kwargs):
        self.calls.append(("list", email, kwargs))
        return DrivePage([self.file], "opaque-next")

    async def get_file(self, email, file_id):
        self.calls.append(("file", email, file_id))
        return self.file

    async def list_permissions(self, email, file_id):
        return [
            DrivePermission(
                "owner", "user", "owner", email_address="alice@example.com"
            ),
            DrivePermission(
                "inherited",
                "domain",
                "reader",
                domain="example.com",
                inherited=True,
            ),
            DrivePermission(
                "writer", "user", "writer", email_address="bob@example.com"
            ),
        ]

    async def preview(self, email, file_id):
        from gamgui.core.drive.models import PreviewStream

        return PreviewStream(b"%PDF", "application/pdf", "safe.pdf")

    async def update_metadata(self, email, file_id, *, name, description, starred):
        self.file = DriveFile.from_api(
            file_data(name=name, description=description, starred=starred)
        )
        return self.file

    async def add_permission(
        self, subject, file_id, *, email: str, principal_type, role
    ):
        return DrivePermission("new", principal_type, role, email_address=email)

    async def update_permission(self, email, file_id, permission_id, role):
        return DrivePermission(
            permission_id, "user", role, email_address="bob@example.com"
        )

    async def remove_permission(self, email, file_id, permission_id):
        return None

    async def validate_single_transfer(self, email, file_id, destination):
        return self.file, InternalPrincipal(destination, "user")

    async def transfer_file_ownership(
        self, email, file_id, destination, *, confirmation
    ):
        if confirmation != destination:
            raise DriveSafetyError("Type the exact destination email.")
        return TransferResult(
            True, file_id, destination, detail="Ownership transferred."
        )

    def claim_manifest(self, operation_id, *, confirmation):
        manifest = self.operations.get(operation_id, self.domain)
        if manifest is None:
            raise DriveSafetyError("That ownership manifest was not found.")
        if confirmation != manifest.destination:
            raise DriveSafetyError("Type the exact destination email to confirm.")
        if manifest.remaining == 0:
            self.operations.set_operation_status(
                operation_id,
                self.domain,
                "completed",
            )
            return self.operations.get(operation_id, self.domain)
        if not self.operations.claim_operation(operation_id, self.domain):
            raise DriveSafetyError("That ownership manifest is already running.")
        return self.operations.get(operation_id, self.domain)

    async def apply_manifest(
        self,
        operation_id,
        *,
        confirmation,
        claimed=False,
        progress=None,
    ):
        assert claimed
        self.operations.set_operation_status(operation_id, self.domain, "completed")
        return self.operations.get(operation_id, self.domain)


@pytest.fixture
def drive_web_client(tmp_path):
    service = FakeWebService(tmp_path)
    app = FastAPI()
    app.state.gamgui = SimpleNamespace(drive_service=service, jobs={})
    app.include_router(drive_router)
    return TestClient(app), service


def test_drive_panel_fetches_one_bounded_page_and_defers_acl(drive_web_client):
    client, service = drive_web_client
    response = client.get("/drive/user", params={"email": "alice@example.com"})
    assert response.status_code == 200
    assert "District plan.pdf" in response.text
    assert "Next 50 files" in response.text
    assert service.calls == [
        (
            "list",
            "alice@example.com",
            {
                "search": "",
                "mime_type": "",
                "modified_after": "",
                "cursor": None,
                "page_size": 50,
            },
        )
    ]
    assert "/drive/permissions" not in response.text


def test_drive_manage_exposes_and_focuses_visible_detail_region(drive_web_client):
    client, _service = drive_web_client

    response = client.get("/drive/user", params={"email": "alice@example.com"})

    assert response.status_code == 200
    assert 'aria-label="Selected file management"' in response.text
    assert 'tabindex="-1"' in response.text
    assert "Select Manage beside a file" in response.text
    assert 'aria-controls="drive-detail"' in response.text
    assert 'hx-swap="innerHTML show:#drive-detail:top"' in response.text
    assert (
        'hx-on::after-swap="if (event.detail.target === this) '
        'this.focus({preventScroll:true})"'
    ) in response.text
    assert 'Manage<span class="sr-only"> District plan.pdf</span>' in response.text


def test_drive_detail_loads_acl_only_after_file_selection(drive_web_client):
    client, service = drive_web_client
    response = client.get(
        "/drive/file",
        params={"email": "alice@example.com", "file_id": "file-1"},
    )
    assert response.status_code == 200
    assert 'hx-get="/drive/permissions"' in response.text
    permissions = client.get(
        "/drive/permissions",
        params={"email": "alice@example.com", "file_id": "file-1"},
    )
    assert permissions.status_code == 200
    # Only the mutable writer has a remove control.
    assert permissions.text.count('hx-post="/drive/permissions/remove"') == 1
    assert "Protected" in permissions.text
    assert "Inherited" in permissions.text


def test_preview_content_sets_no_store_and_nosniff(drive_web_client):
    client, _service = drive_web_client
    response = client.get(
        "/drive/preview/content",
        params={"email": "alice@example.com", "file_id": "file-1"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("sandbox")
    assert response.content == b"%PDF"


def test_preview_frame_rejects_hostile_mime(drive_web_client):
    client, service = drive_web_client
    service.file = DriveFile.from_api(file_data(mimeType="image/svg+xml"))
    response = client.get(
        "/drive/preview",
        params={"email": "alice@example.com", "file_id": "file-1"},
    )
    assert response.status_code == 200
    assert "not safe to preview" in response.text
    assert "<iframe" not in response.text


def test_drive_metadata_and_permission_routes_rerender(drive_web_client):
    client, _service = drive_web_client
    metadata = client.post(
        "/drive/metadata",
        data={
            "email": "alice@example.com",
            "file_id": "file-1",
            "name": "Renamed plan.pdf",
            "description": "Board copy",
            "starred": "on",
        },
    )
    assert metadata.status_code == 200
    assert "Renamed plan.pdf" in metadata.text
    assert "File details updated" in metadata.text

    shared = client.post(
        "/drive/permissions/add",
        data={
            "email": "alice@example.com",
            "file_id": "file-1",
            "target": "teacher@example.com",
            "principal_type": "user",
            "role": "reader",
        },
    )
    assert shared.status_code == 200
    assert "Shared with teacher@example.com" in shared.text
    updated = client.post(
        "/drive/permissions/update",
        data={
            "email": "alice@example.com",
            "file_id": "file-1",
            "permission_id": "writer",
            "role": "reader",
        },
    )
    assert updated.status_code == 200
    assert "Access level updated" in updated.text
    removed = client.post(
        "/drive/permissions/remove",
        data={
            "email": "alice@example.com",
            "file_id": "file-1",
            "permission_id": "writer",
        },
    )
    assert removed.status_code == 200
    assert "Access removed" in removed.text


def test_drive_ownership_preview_and_apply_are_typed(drive_web_client):
    client, _service = drive_web_client
    preview = client.post(
        "/drive/ownership/preview",
        data={
            "email": "alice@example.com",
            "file_id": "file-1",
            "destination": "bob@example.com",
        },
    )
    assert preview.status_code == 200
    assert "mandatory" in preview.text
    assert "norecursion" in preview.text
    refused = client.post(
        "/drive/ownership/apply",
        data={
            "email": "alice@example.com",
            "file_id": "file-1",
            "destination": "bob@example.com",
            "confirmation": "wrong@example.com",
        },
    )
    assert "exact destination" in refused.text
    applied = client.post(
        "/drive/ownership/apply",
        data={
            "email": "alice@example.com",
            "file_id": "file-1",
            "destination": "bob@example.com",
            "confirmation": "bob@example.com",
        },
    )
    assert "Ownership transferred" in applied.text


def test_manifest_apply_rejects_an_already_running_submission(drive_web_client):
    client, service = drive_web_client
    manifest = sample_manifest()
    service.operations.create(manifest)
    assert service.operations.claim_operation(
        manifest.id,
        service.domain,
    )

    response = client.post(
        "/drive/manifest/apply",
        data={
            "manifest_id": manifest.id,
            "confirmation": manifest.destination,
        },
    )
    assert response.status_code == 200
    assert "already running" in response.text
    assert not client.app.state.gamgui.jobs


def test_manifest_apply_returns_completed_without_starting_empty_job(
    drive_web_client,
):
    client, service = drive_web_client
    manifest = sample_manifest()
    manifest.status = "interrupted"
    for target in manifest.targets:
        target.status = "succeeded"
    service.operations.create(manifest)

    response = client.post(
        "/drive/manifest/apply",
        data={
            "manifest_id": manifest.id,
            "confirmation": manifest.destination,
        },
    )
    assert response.status_code == 200
    assert "completed" in response.text
    assert not client.app.state.gamgui.jobs
    assert service.operations.get(manifest.id, service.domain).status == "completed"


def test_open_in_drive_uses_system_browser(drive_web_client, monkeypatch):
    client, _service = drive_web_client
    opened = []
    monkeypatch.setattr(
        "gamgui.web.routes.drive.webbrowser.open",
        lambda url, new: opened.append((url, new)) or True,
    )
    response = client.post(
        "/drive/open",
        data={"email": "alice@example.com", "file_id": "file-1"},
    )
    assert response.status_code == 200
    assert "Opened in Google Drive" in response.text
    assert opened == [("https://drive.google.com/open?id=file-1", 2)]
