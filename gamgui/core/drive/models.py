"""Drive metadata models.

Only metadata required by the UI is retained. Access tokens and file content never enter these
models or the durable operation store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIMES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _as_size(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: Optional[int] = None
    owner_email: str = ""
    owner_name: str = ""
    created_time: str = ""
    modified_time: str = ""
    web_view_link: str = ""
    parents: List[str] = field(default_factory=list)
    drive_id: str = ""
    shared: bool = False
    owned_by_me: bool = False
    description: str = ""
    starred: bool = False
    can_download: bool = False
    can_edit: bool = False
    can_share: bool = False
    can_accept_ownership: bool = False
    resource_key: str = ""

    @property
    def is_folder(self) -> bool:
        return self.mime_type == GOOGLE_FOLDER_MIME

    @property
    def is_google_doc(self) -> bool:
        return self.mime_type in GOOGLE_DOC_MIMES

    @property
    def is_shared_drive(self) -> bool:
        return bool(self.drive_id)

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "DriveFile":
        owners = data.get("owners") if isinstance(data.get("owners"), list) else []
        owner = owners[0] if owners and isinstance(owners[0], dict) else {}
        caps = (
            data.get("capabilities")
            if isinstance(data.get("capabilities"), dict)
            else {}
        )
        parents = data.get("parents") if isinstance(data.get("parents"), list) else []
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            mime_type=str(data.get("mimeType") or ""),
            size=_as_size(data.get("size")),
            owner_email=str(owner.get("emailAddress") or ""),
            owner_name=str(owner.get("displayName") or ""),
            created_time=str(data.get("createdTime") or ""),
            modified_time=str(data.get("modifiedTime") or ""),
            web_view_link=str(data.get("webViewLink") or ""),
            parents=[str(p) for p in parents if p],
            drive_id=str(data.get("driveId") or ""),
            shared=_as_bool(data.get("shared")),
            owned_by_me=_as_bool(data.get("ownedByMe")),
            description=str(data.get("description") or ""),
            starred=_as_bool(data.get("starred")),
            can_download=_as_bool(caps.get("canDownload")),
            can_edit=_as_bool(caps.get("canEdit")),
            can_share=_as_bool(caps.get("canShare")),
            can_accept_ownership=_as_bool(caps.get("canAcceptOwnership")),
            resource_key=str(data.get("resourceKey") or ""),
        )


@dataclass(frozen=True)
class DrivePermission:
    id: str
    type: str
    role: str
    email_address: str = ""
    display_name: str = ""
    domain: str = ""
    allow_file_discovery: bool = False
    expiration_time: str = ""
    deleted: bool = False
    inherited: bool = False
    inherited_from: str = ""

    @property
    def identity(self) -> str:
        if self.email_address:
            return self.email_address
        if self.domain:
            return self.domain
        if self.type == "anyone":
            return "Anyone with access"
        return self.display_name or self.id

    @property
    def removable(self) -> bool:
        return self.role not in {"owner", "organizer"} and not self.inherited

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "DrivePermission":
        details = data.get("permissionDetails")
        inherited = False
        inherited_from = ""
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                if _as_bool(detail.get("inherited")):
                    inherited = True
                    inherited_from = str(detail.get("inheritedFrom") or "")
                    break
        return cls(
            id=str(data.get("id") or ""),
            type=str(data.get("type") or ""),
            role=str(data.get("role") or ""),
            email_address=str(data.get("emailAddress") or ""),
            display_name=str(data.get("displayName") or ""),
            domain=str(data.get("domain") or ""),
            allow_file_discovery=_as_bool(data.get("allowFileDiscovery")),
            expiration_time=str(data.get("expirationTime") or ""),
            deleted=_as_bool(data.get("deleted")),
            inherited=inherited,
            inherited_from=inherited_from,
        )


@dataclass(frozen=True)
class DrivePage:
    items: List[DriveFile]
    next_cursor: Optional[str] = None
    incomplete_search: bool = False


@dataclass(frozen=True)
class PreviewStream:
    body: bytes
    media_type: str
    filename: str
    exported: bool = False


@dataclass
class OperationTarget:
    file_id: str
    name: str
    source_owner: str
    mime_type: str = ""
    status: str = "pending"
    error: str = ""
    residual_access: str = ""


@dataclass
class OperationManifest:
    id: str
    domain: str
    kind: str
    subject: str
    destination: str
    root_id: str
    target_hash: str
    targets: List[OperationTarget]
    created_at: str
    updated_at: str
    status: str = "planned"

    @property
    def remaining(self) -> int:
        return sum(t.status != "succeeded" for t in self.targets)

    @property
    def residual_count(self) -> int:
        return sum(bool(t.residual_access) for t in self.targets)


@dataclass(frozen=True)
class TransferResult:
    ok: bool
    file_id: str
    destination: str
    detail: str = ""
    residual_access: str = ""
