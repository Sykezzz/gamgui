"""Policy boundary for Drive reads, edits, sharing, previews, and ownership operations."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Protocol

from ..audit import AuditLog
from ..gam.commands import GAMCommands
from ..gam.runner import GAMRunner
from .client import DriveAPIClient, MAX_PREVIEW_BYTES
from .models import (
    DriveFile,
    DrivePage,
    DrivePermission,
    OperationManifest,
    OperationTarget,
    PreviewStream,
    TransferResult,
)
from .operations import DriveOperationStore

PREVIEW_MIMES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "text/plain",
}
SHARE_ROLES = {"reader", "commenter", "writer"}
UPDATE_ROLES = {"reader", "commenter", "writer"}
MANIFEST_CAP = 500


class DriveSafetyError(ValueError):
    """A Drive action refused by a server-side safety rule."""


@dataclass(frozen=True)
class InternalPrincipal:
    email: str
    type: str
    active: bool = True


class DirectoryResolver(Protocol):
    async def resolve(
        self, email: str, principal_type: str
    ) -> Optional[InternalPrincipal]: ...


class ConnectorDirectoryResolver:
    """Live directory verification fallback.

    User resolution is one exact lookup. Group resolution uses the connector's existing bounded
    abstraction; a DirectoryIndex-backed resolver can be injected without changing DriveService.
    """

    def __init__(self, connector) -> None:
        self.connector = connector

    async def resolve(
        self, email: str, principal_type: str
    ) -> Optional[InternalPrincipal]:
        email = email.strip().lower()
        if principal_type == "user":
            try:
                user = await self.connector.get_user(email)
            except Exception:
                return None
            aliases = {str(a).lower() for a in getattr(user, "aliases", [])}
            primary = str(getattr(user, "primary_email", "")).lower()
            if email not in aliases and email != primary:
                return None
            return InternalPrincipal(
                email=primary or email,
                type="user",
                active=not bool(getattr(user, "suspended", False)),
            )
        if principal_type == "group":
            try:
                groups = await self.connector.list_groups()
            except Exception:
                return None
            group = next(
                (g for g in groups if str(getattr(g, "email", "")).lower() == email),
                None,
            )
            return InternalPrincipal(email=email, type="group") if group else None
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _manifest_hash(targets: list[OperationTarget]) -> str:
    rows = sorted(f"{t.file_id}\0{t.source_owner.lower()}" for t in targets)
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _clean_filename(name: str, extension: str = "") -> str:
    safe = "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip(" .")
    safe = safe or "preview"
    if extension and not safe.lower().endswith(extension):
        safe += extension
    return safe


class DriveService:
    def __init__(
        self,
        client: DriveAPIClient,
        runner: GAMRunner,
        domain: str,
        *,
        audit: Optional[AuditLog] = None,
        resolver: Optional[DirectoryResolver] = None,
        operations: Optional[DriveOperationStore] = None,
    ) -> None:
        self.client = client
        self.runner = runner
        self.domain = domain
        self.audit = audit or AuditLog()
        self.resolver = resolver
        self.operations = operations or DriveOperationStore()

    async def list_owned_files(
        self,
        subject: str,
        *,
        search: str = "",
        mime_type: str = "",
        modified_after: str = "",
        cursor: Optional[str] = None,
        page_size: int = 50,
    ) -> DrivePage:
        return await self.client.list_owned_files(
            subject,
            search=search,
            mime_type=mime_type,
            modified_after=modified_after,
            cursor=cursor,
            page_size=page_size,
        )

    async def get_file(self, subject: str, file_id: str) -> DriveFile:
        return await self.client.get_file(subject, file_id)

    async def list_permissions(
        self, subject: str, file_id: str
    ) -> list[DrivePermission]:
        return await self.client.list_permissions(subject, file_id)

    async def _internal(self, email: str, principal_type: str) -> InternalPrincipal:
        if self.resolver is None:
            raise DriveSafetyError(
                "Directory verification is unavailable; reconnect the domain."
            )
        if principal_type not in {"user", "group"}:
            raise DriveSafetyError(
                "Only internal users and groups can be granted new access."
            )
        principal = await self.resolver.resolve(email.strip(), principal_type)
        if principal is None:
            raise DriveSafetyError(
                "The recipient was not found in the active Workspace directory."
            )
        if principal_type == "user" and not principal.active:
            raise DriveSafetyError("The destination user is suspended.")
        return principal

    async def update_metadata(
        self,
        subject: str,
        file_id: str,
        *,
        name: str,
        description: str,
        starred: bool,
    ) -> DriveFile:
        live = await self.client.get_file(subject, file_id)
        if not live.can_edit:
            raise DriveSafetyError("This account cannot edit that file's metadata.")
        name = name.strip()
        if not name:
            raise DriveSafetyError("File name cannot be blank.")
        if len(name) > 255:
            raise DriveSafetyError("File name must be 255 characters or fewer.")
        if len(description) > 5000:
            raise DriveSafetyError("Description must be 5,000 characters or fewer.")
        try:
            updated = await self.client.update_file(
                subject,
                file_id,
                {"name": name, "description": description, "starred": starred},
            )
        except Exception as exc:
            self.audit.record(
                "drive_update_metadata",
                target=file_id,
                ok=False,
                actor=subject,
                extra={"error": str(exc)},
            )
            raise
        self.audit.record(
            "drive_update_metadata", target=file_id, ok=True, actor=subject
        )
        return updated

    async def add_permission(
        self,
        subject: str,
        file_id: str,
        *,
        email: str,
        principal_type: str,
        role: str,
    ) -> DrivePermission:
        live = await self.client.get_file(subject, file_id)
        if not live.can_share:
            raise DriveSafetyError("This account cannot change sharing for that file.")
        if role not in SHARE_ROLES:
            raise DriveSafetyError("Choose reader, commenter, or writer access.")
        principal = await self._internal(email, principal_type)
        try:
            permission = await self.client.create_permission(
                subject,
                file_id,
                principal_type=principal.type,
                email=principal.email,
                role=role,
            )
        except Exception as exc:
            self.audit.record(
                "drive_add_permission",
                target=file_id,
                ok=False,
                actor=subject,
                extra={"recipient": principal.email, "error": str(exc)},
            )
            raise
        self.audit.record(
            "drive_add_permission",
            target=file_id,
            ok=True,
            actor=subject,
            extra={"recipient": principal.email, "role": role},
        )
        return permission

    async def _mutable_permission(
        self, subject: str, file_id: str, permission_id: str
    ) -> DrivePermission:
        live = await self.client.get_file(subject, file_id)
        if not live.can_share:
            raise DriveSafetyError("This account cannot change sharing for that file.")
        permissions = await self.client.list_permissions(subject, file_id)
        permission = next((p for p in permissions if p.id == permission_id), None)
        if permission is None:
            raise DriveSafetyError("That permission no longer exists.")
        if not permission.removable:
            raise DriveSafetyError(
                "Owner and inherited permissions cannot be changed here."
            )
        return permission

    async def update_permission(
        self,
        subject: str,
        file_id: str,
        permission_id: str,
        role: str,
    ) -> DrivePermission:
        if role not in UPDATE_ROLES:
            raise DriveSafetyError("Choose reader, commenter, or writer access.")
        permission = await self._mutable_permission(subject, file_id, permission_id)
        try:
            updated = await self.client.update_permission(
                subject, file_id, permission_id, role
            )
        except Exception as exc:
            self.audit.record(
                "drive_update_permission",
                target=file_id,
                ok=False,
                actor=subject,
                extra={"permission": permission.id, "error": str(exc)},
            )
            raise
        self.audit.record(
            "drive_update_permission",
            target=file_id,
            ok=True,
            actor=subject,
            extra={"permission": permission.id, "role": role},
        )
        return updated

    async def remove_permission(
        self, subject: str, file_id: str, permission_id: str
    ) -> None:
        permission = await self._mutable_permission(subject, file_id, permission_id)
        try:
            await self.client.delete_permission(subject, file_id, permission_id)
        except Exception as exc:
            self.audit.record(
                "drive_remove_permission",
                target=file_id,
                ok=False,
                actor=subject,
                extra={"permission": permission.id, "error": str(exc)},
            )
            raise
        self.audit.record(
            "drive_remove_permission",
            target=file_id,
            ok=True,
            actor=subject,
            extra={"permission": permission.id, "identity": permission.identity},
        )

    async def preview(self, subject: str, file_id: str) -> PreviewStream:
        live = await self.client.get_file(subject, file_id)
        if not live.can_download:
            raise DriveSafetyError(
                "Google does not allow this account to download that file."
            )
        if live.is_google_doc:
            body = await self.client.download(
                subject,
                file_id,
                export_mime="application/pdf",
                max_bytes=MAX_PREVIEW_BYTES,
            )
            return PreviewStream(
                body=body,
                media_type="application/pdf",
                filename=_clean_filename(live.name, ".pdf"),
                exported=True,
            )
        if live.mime_type not in PREVIEW_MIMES:
            raise DriveSafetyError(
                "This file type is not safe to preview inside GamGUI."
            )
        if live.size is not None and live.size > MAX_PREVIEW_BYTES:
            raise DriveSafetyError("This file is larger than the 10 MB preview limit.")
        body = await self.client.download(subject, file_id, max_bytes=MAX_PREVIEW_BYTES)
        if live.mime_type == "text/plain":
            body = body.decode("utf-8", "replace").encode("utf-8")
            media_type = "text/plain; charset=utf-8"
        else:
            media_type = live.mime_type
        return PreviewStream(
            body=body,
            media_type=media_type,
            filename=_clean_filename(live.name),
        )

    async def validate_single_transfer(
        self, source: str, file_id: str, destination: str
    ) -> tuple[DriveFile, InternalPrincipal]:
        destination_user = await self._internal(destination, "user")
        if destination_user.email.lower() == source.strip().lower():
            raise DriveSafetyError("Choose a different destination owner.")
        live = await self.client.get_file(source, file_id)
        if live.is_shared_drive:
            raise DriveSafetyError(
                "Shared Drive files are organization-owned and cannot transfer to a user."
            )
        if not live.owned_by_me or live.owner_email.lower() != source.strip().lower():
            raise DriveSafetyError("The selected user no longer owns this file.")
        return live, destination_user

    async def _remove_previous_owner(
        self, destination: str, file_id: str, source: str
    ) -> str:
        try:
            permissions = await self.client.list_permissions(destination, file_id)
            previous = next(
                (
                    p
                    for p in permissions
                    if p.email_address.lower() == source.lower()
                    and p.role not in {"owner", "organizer"}
                ),
                None,
            )
            if previous is None:
                return ""
            if not previous.removable:
                return f"{source} still has inherited access."
            await self.client.delete_permission(destination, file_id, previous.id)
            remaining = await self.client.list_permissions(destination, file_id)
            if any(p.email_address.lower() == source.lower() for p in remaining):
                return f"{source} still has access."
            return ""
        except Exception:
            return f"Could not verify or remove {source}'s remaining access."

    async def transfer_file_ownership(
        self,
        source: str,
        file_id: str,
        destination: str,
        *,
        confirmation: str,
    ) -> TransferResult:
        live, destination_user = await self.validate_single_transfer(
            source, file_id, destination
        )
        if confirmation.strip().lower() != destination_user.email.lower():
            raise DriveSafetyError(
                "Type the exact destination email to confirm ownership transfer."
            )
        argv = GAMCommands.transfer_drive_ownership(
            source, live.id, destination_user.email
        )
        try:
            await self.runner.run_authenticated(self.domain, argv, serialize=True)
        except Exception as exc:
            self.audit.record(
                "drive_transfer_ownership",
                target=live.id,
                argv=argv,
                ok=False,
                actor=source,
                extra={"destination": destination_user.email, "error": str(exc)},
            )
            return TransferResult(
                ok=False,
                file_id=live.id,
                destination=destination_user.email,
                detail=str(exc),
            )
        verified = await self.client.get_file(destination_user.email, live.id)
        if verified.owner_email.lower() != destination_user.email.lower():
            detail = "GAM completed, but the new owner could not be verified."
            self.audit.record(
                "drive_transfer_ownership",
                target=live.id,
                argv=argv,
                ok=False,
                actor=source,
                extra={"destination": destination_user.email, "error": detail},
            )
            return TransferResult(
                ok=False,
                file_id=live.id,
                destination=destination_user.email,
                detail=detail,
            )
        residual = await self._remove_previous_owner(
            destination_user.email, live.id, source
        )
        self.audit.record(
            "drive_transfer_ownership",
            target=live.id,
            argv=argv,
            ok=not bool(residual),
            actor=source,
            extra={
                "destination": destination_user.email,
                "residual_access": residual,
            },
        )
        return TransferResult(
            ok=True,
            file_id=live.id,
            destination=destination_user.email,
            detail="Ownership transferred.",
            residual_access=residual,
        )

    async def _walk(
        self,
        subject: str,
        root: DriveFile,
        *,
        owned_only: bool,
        include_root: bool,
    ) -> list[DriveFile]:
        files = [root] if include_root else []
        queue = [root] if root.is_folder else []
        seen = {root.id}
        while queue:
            parent = queue.pop(0)
            cursor: Optional[str] = None
            while True:
                page = await self.client.list_children(
                    subject,
                    parent.id,
                    owned_only=owned_only,
                    cursor=cursor,
                    page_size=50,
                )
                for item in page.items:
                    if item.id in seen:
                        continue
                    seen.add(item.id)
                    files.append(item)
                    if item.is_folder:
                        queue.append(item)
                    if len(files) > MANIFEST_CAP:
                        raise DriveSafetyError(
                            "This operation exceeds the 500-file confirmation cap. "
                            "Choose a smaller subfolder and build a fresh manifest."
                        )
                cursor = page.next_cursor
                if not cursor:
                    break
        return files

    async def plan_folder_transfer(
        self, source: str, root_id: str, destination: str
    ) -> OperationManifest:
        root, destination_user = await self.validate_single_transfer(
            source, root_id, destination
        )
        if not root.is_folder:
            raise DriveSafetyError("Choose a folder for a recursive transfer manifest.")
        files = await self._walk(source, root, owned_only=True, include_root=True)
        if any(f.is_shared_drive for f in files):
            raise DriveSafetyError(
                "Shared Drive content cannot transfer to an individual."
            )
        # Child files are transferred before folders; the root folder is always last.
        files.sort(key=lambda f: (f.is_folder, f.id == root.id, f.name.lower()))
        targets = [
            OperationTarget(
                file_id=f.id,
                name=f.name,
                source_owner=source,
                mime_type=f.mime_type,
            )
            for f in files
        ]
        return self._create_manifest(
            kind="folder_transfer",
            subject=source,
            destination=destination_user.email,
            root_id=root.id,
            targets=targets,
        )

    async def plan_classroom_claim(
        self, teacher: str, folder_id: str
    ) -> OperationManifest:
        teacher_user = await self._internal(teacher, "user")
        root = await self.client.get_file(teacher_user.email, folder_id)
        if not root.is_folder:
            raise DriveSafetyError("Choose a Classroom folder.")
        if root.is_shared_drive:
            raise DriveSafetyError(
                "Classroom ownership claims are not supported in Shared Drives."
            )
        files = await self._walk(
            teacher_user.email, root, owned_only=False, include_root=False
        )
        targets = []
        for file in files:
            if file.is_shared_drive:
                raise DriveSafetyError("The manifest contains Shared Drive content.")
            if file.is_folder or not file.owner_email:
                continue
            if file.owner_email.lower() == teacher_user.email.lower():
                continue
            targets.append(
                OperationTarget(
                    file_id=file.id,
                    name=file.name,
                    source_owner=file.owner_email,
                    mime_type=file.mime_type,
                )
            )
        if not targets:
            raise DriveSafetyError("No student-owned files were found in that folder.")
        return self._create_manifest(
            kind="classroom_claim",
            subject=teacher_user.email,
            destination=teacher_user.email,
            root_id=root.id,
            targets=targets,
        )

    def _create_manifest(
        self,
        *,
        kind: str,
        subject: str,
        destination: str,
        root_id: str,
        targets: list[OperationTarget],
    ) -> OperationManifest:
        if not targets:
            raise DriveSafetyError("The manifest contains no files.")
        if len(targets) > MANIFEST_CAP:
            raise DriveSafetyError("A manifest cannot contain more than 500 files.")
        now = _now()
        manifest = OperationManifest(
            id=secrets.token_urlsafe(12),
            domain=self.domain,
            kind=kind,
            subject=subject,
            destination=destination,
            root_id=root_id,
            target_hash=_manifest_hash(targets),
            targets=targets,
            created_at=now,
            updated_at=now,
        )
        self.operations.create(manifest)
        return manifest

    async def _claim_one(self, target: OperationTarget, teacher: str) -> TransferResult:
        owner = await self._internal(target.source_owner, "user")
        live = await self.client.get_file(teacher, target.file_id)
        if live.is_shared_drive:
            raise DriveSafetyError("Shared Drive content cannot be claimed.")
        if live.owner_email.lower() != owner.email.lower():
            raise DriveSafetyError(
                "The file owner changed after the manifest was built."
            )
        if live.is_folder:
            raise DriveSafetyError(
                "Folder claims are not allowed in exact-file manifests."
            )
        argv = GAMCommands.claim_drive_ownership(teacher, live.id, owner.email)
        try:
            await self.runner.run_authenticated(self.domain, argv, serialize=True)
        except Exception as exc:
            self.audit.record(
                "drive_claim_ownership",
                target=live.id,
                argv=argv,
                ok=False,
                actor=teacher,
                extra={"previous_owner": owner.email, "error": str(exc)},
            )
            return TransferResult(False, live.id, teacher, detail=str(exc))
        verified = await self.client.get_file(teacher, live.id)
        if verified.owner_email.lower() != teacher.lower():
            return TransferResult(
                False,
                live.id,
                teacher,
                detail="GAM completed, but teacher ownership could not be verified.",
            )
        residual = await self._remove_previous_owner(teacher, live.id, owner.email)
        self.audit.record(
            "drive_claim_ownership",
            target=live.id,
            argv=argv,
            ok=not bool(residual),
            actor=teacher,
            extra={"previous_owner": owner.email, "residual_access": residual},
        )
        return TransferResult(
            True,
            live.id,
            teacher,
            detail="Ownership claimed.",
            residual_access=residual,
        )

    async def apply_manifest(
        self,
        operation_id: str,
        *,
        confirmation: str,
        progress: Optional[
            Callable[[int, int, OperationTarget], Awaitable[None]]
        ] = None,
    ) -> OperationManifest:
        manifest = self.operations.get(operation_id, self.domain)
        if manifest is None:
            raise DriveSafetyError(
                "That ownership manifest was not found for this domain."
            )
        if confirmation.strip().lower() != manifest.destination.lower():
            raise DriveSafetyError(
                "Type the exact destination email to confirm this manifest."
            )
        if len(manifest.targets) > MANIFEST_CAP:
            raise DriveSafetyError("The manifest exceeds the 500-file safety cap.")
        pending = [t for t in manifest.targets if t.status != "succeeded"]
        if not pending:
            return manifest
        self.operations.set_operation_status(operation_id, self.domain, "running")
        try:
            for index, target in enumerate(pending, start=1):
                self.operations.set_target_status(
                    operation_id, self.domain, target.file_id, "running"
                )
                try:
                    if manifest.kind == "folder_transfer":
                        result = await self.transfer_file_ownership(
                            target.source_owner,
                            target.file_id,
                            manifest.destination,
                            confirmation=manifest.destination,
                        )
                    elif manifest.kind == "classroom_claim":
                        result = await self._claim_one(target, manifest.destination)
                    else:
                        raise DriveSafetyError("Unknown Drive operation type.")
                    status = "succeeded" if result.ok else "failed"
                    self.operations.set_target_status(
                        operation_id,
                        self.domain,
                        target.file_id,
                        status,
                        error="" if result.ok else result.detail,
                        residual_access=result.residual_access,
                    )
                except asyncio.CancelledError:
                    self.operations.set_target_status(
                        operation_id,
                        self.domain,
                        target.file_id,
                        "interrupted",
                        error="Application stopped before this file completed.",
                    )
                    raise
                except Exception as exc:
                    self.operations.set_target_status(
                        operation_id,
                        self.domain,
                        target.file_id,
                        "failed",
                        error=str(exc),
                    )
                if progress is not None:
                    current = self.operations.get(operation_id, self.domain)
                    refreshed = next(
                        (t for t in current.targets if t.file_id == target.file_id),
                        target,
                    )
                    await progress(index, len(pending), refreshed)
        except asyncio.CancelledError:
            self.operations.set_operation_status(
                operation_id, self.domain, "interrupted"
            )
            raise
        final = self.operations.get(operation_id, self.domain)
        status = (
            "completed"
            if final is not None and all(t.status == "succeeded" for t in final.targets)
            else "partial"
        )
        self.operations.set_operation_status(operation_id, self.domain, status)
        return self.operations.get(operation_id, self.domain)
