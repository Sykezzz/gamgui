"""Small delegated Google Drive v3 client.

The client requests narrow metadata projections, never persists access tokens, and caps every list
page before sending it to Google. Binary content is accumulated only for an explicitly selected
file and is rejected once it exceeds the preview limit.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol, Sequence
from urllib.parse import quote

import httpx

from ..secrets.vault import SecretsVault
from .models import DriveFile, DrivePage, DrivePermission

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
API_ROOT = "https://www.googleapis.com/drive/v3"
MAX_PAGE_SIZE = 50
MAX_PREVIEW_BYTES = 10 * 1024 * 1024

FILE_FIELDS = (
    "id,name,mimeType,size,createdTime,modifiedTime,webViewLink,parents,driveId,shared,"
    "ownedByMe,owners(displayName,emailAddress,permissionId),"
    "capabilities(canDownload,canEdit,canShare,canAcceptOwnership),"
    "description,starred,resourceKey"
)
LIST_FIELDS = f"nextPageToken,incompleteSearch,files({FILE_FIELDS})"
PERMISSION_FIELDS = (
    "id,type,role,emailAddress,displayName,domain,allowFileDiscovery,expirationTime,deleted,"
    "permissionDetails(inherited,inheritedFrom,permissionType,role)"
)


class TokenProvider(Protocol):
    async def token_for(self, subject: str) -> str: ...


class ServiceAccountTokenProvider:
    """Mint delegated tokens from the existing GAM service-account JSON held in the vault."""

    def __init__(
        self,
        vault: SecretsVault,
        domain: str,
        scopes: Sequence[str] = (DRIVE_SCOPE,),
    ) -> None:
        self.vault = vault
        self.domain = domain
        self.scopes = tuple(scopes)
        self._credentials: "OrderedDict[str, Any]" = OrderedDict()
        self._lock = asyncio.Lock()

    def _credential_for(self, subject: str):
        raw = self.vault.get(self.domain, "oauth2service")
        if not raw:
            raise PermissionError(
                "The GAM service-account credential is missing. Re-run setup."
            )
        try:
            info = json.loads(raw)
        except ValueError as exc:
            raise PermissionError(
                "The GAM service-account credential is invalid."
            ) from exc
        try:
            from google.oauth2 import service_account
        except ImportError as exc:  # pragma: no cover - dependency installation failure
            raise RuntimeError(
                "google-auth is required for Drive administration."
            ) from exc
        return service_account.Credentials.from_service_account_info(
            info, scopes=self.scopes, subject=subject
        )

    @staticmethod
    def _refresh(credentials) -> str:
        from google.auth.transport.requests import Request

        credentials.refresh(Request())
        if not credentials.token:
            raise PermissionError("Google did not return a delegated access token.")
        return str(credentials.token)

    async def token_for(self, subject: str) -> str:
        subject = subject.strip().lower()
        if not subject:
            raise ValueError("A delegated subject is required.")
        async with self._lock:
            credentials = self._credentials.get(subject)
            if credentials is None:
                credentials = self._credential_for(subject)
                self._credentials[subject] = credentials
                while len(self._credentials) > 64:
                    self._credentials.popitem(last=False)
            else:
                self._credentials.move_to_end(subject)
            if credentials.valid and credentials.token:
                return str(credentials.token)
            return await asyncio.to_thread(self._refresh, credentials)


@dataclass
class DriveAPIError(Exception):
    status_code: int
    reason: str
    detail: str

    def __post_init__(self) -> None:
        super().__init__(self.user_message)

    @property
    def user_message(self) -> str:
        if self.status_code == 401:
            return (
                "Drive authorization expired. Re-run setup and verify delegated scopes."
            )
        if self.status_code == 403:
            if self.reason in {
                "rateLimitExceeded",
                "userRateLimitExceeded",
                "quotaExceeded",
            }:
                return (
                    "Google Drive is rate-limiting requests. Wait a moment and retry."
                )
            return "The delegated account cannot perform this Drive action. Check DWD scopes and admin access."
        if self.status_code == 404:
            return "That Drive file or permission no longer exists."
        if self.status_code == 429 or self.status_code >= 500:
            return "Google Drive is temporarily unavailable. Retry in a moment."
        return self.detail or f"Google Drive returned HTTP {self.status_code}."


def escape_query_literal(value: str) -> str:
    """Escape a Drive query string literal; it remains one HTTP parameter, never executable code."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _error_from_response(response: httpx.Response) -> DriveAPIError:
    reason = ""
    detail = ""
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
            errors = error.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                reason = str(errors[0].get("reason") or "")
            if not reason:
                details = error.get("details")
                if (
                    isinstance(details, list)
                    and details
                    and isinstance(details[0], dict)
                ):
                    reason = str(details[0].get("reason") or "")
    except (ValueError, TypeError):
        detail = ""
    return DriveAPIError(response.status_code, reason, detail)


class DriveAPIClient:
    def __init__(
        self,
        token_provider: TokenProvider,
        http: Optional[httpx.AsyncClient] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.token_provider = token_provider
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._owns_http = http is None
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def _request(
        self,
        subject: str,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        token = await self.token_provider.token_for(subject)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        response: Optional[httpx.Response] = None
        for attempt in range(3):
            response = await self.http.request(
                method,
                f"{API_ROOT}{path}",
                params=params,
                json=json_body,
                headers=headers,
            )
            if response.status_code < 400:
                if response.status_code == 204 or not response.content:
                    return {}
                payload = response.json()
                return payload if isinstance(payload, dict) else {}
            error = _error_from_response(response)
            retryable = (
                response.status_code == 429
                or response.status_code >= 500
                or error.reason in {"rateLimitExceeded", "userRateLimitExceeded"}
            )
            if not retryable or attempt == 2:
                raise error
            await self._sleep(0.25 * (2**attempt))
        raise _error_from_response(
            response
        )  # pragma: no cover - loop always returns/raises

    async def list_owned_files(
        self,
        subject: str,
        *,
        search: str = "",
        mime_type: str = "",
        modified_after: str = "",
        cursor: Optional[str] = None,
        page_size: int = MAX_PAGE_SIZE,
    ) -> DrivePage:
        page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        clauses = ["'me' in owners", "trashed = false"]
        if search.strip():
            clauses.append(f"name contains '{escape_query_literal(search.strip())}'")
        if mime_type.strip():
            clauses.append(f"mimeType = '{escape_query_literal(mime_type.strip())}'")
        if modified_after:
            try:
                parsed = date.fromisoformat(modified_after)
            except ValueError as exc:
                raise ValueError("Modified-after date must use YYYY-MM-DD.") from exc
            clauses.append(f"modifiedTime >= '{parsed.isoformat()}T00:00:00Z'")
        params: Dict[str, Any] = {
            "q": " and ".join(clauses),
            "pageSize": page_size,
            "fields": LIST_FIELDS,
            "spaces": "drive",
            "corpora": "user",
            "orderBy": "modifiedTime desc,name_natural",
            "includeItemsFromAllDrives": "false",
            "supportsAllDrives": "true",
        }
        if cursor:
            params["pageToken"] = cursor
        payload = await self._request(subject, "GET", "/files", params=params)
        raw_files = (
            payload.get("files") if isinstance(payload.get("files"), list) else []
        )
        return DrivePage(
            items=[
                DriveFile.from_api(item) for item in raw_files if isinstance(item, dict)
            ],
            next_cursor=str(payload.get("nextPageToken") or "") or None,
            incomplete_search=bool(payload.get("incompleteSearch")),
        )

    async def list_children(
        self,
        subject: str,
        parent_id: str,
        *,
        owned_only: bool = False,
        cursor: Optional[str] = None,
        page_size: int = MAX_PAGE_SIZE,
    ) -> DrivePage:
        page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        clauses = [
            f"'{escape_query_literal(parent_id)}' in parents",
            "trashed = false",
        ]
        if owned_only:
            clauses.append("'me' in owners")
        params: Dict[str, Any] = {
            "q": " and ".join(clauses),
            "pageSize": page_size,
            "fields": LIST_FIELDS,
            "spaces": "drive",
            "corpora": "user",
            "orderBy": "folder,name_natural",
            "includeItemsFromAllDrives": "false",
            "supportsAllDrives": "true",
        }
        if cursor:
            params["pageToken"] = cursor
        payload = await self._request(subject, "GET", "/files", params=params)
        raw_files = (
            payload.get("files") if isinstance(payload.get("files"), list) else []
        )
        return DrivePage(
            items=[
                DriveFile.from_api(item) for item in raw_files if isinstance(item, dict)
            ],
            next_cursor=str(payload.get("nextPageToken") or "") or None,
            incomplete_search=bool(payload.get("incompleteSearch")),
        )

    async def get_file(self, subject: str, file_id: str) -> DriveFile:
        payload = await self._request(
            subject,
            "GET",
            f"/files/{quote(file_id, safe='')}",
            params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
        )
        return DriveFile.from_api(payload)

    async def list_permissions(
        self, subject: str, file_id: str
    ) -> list[DrivePermission]:
        payload = await self._request(
            subject,
            "GET",
            f"/files/{quote(file_id, safe='')}/permissions",
            params={
                "fields": f"permissions({PERMISSION_FIELDS})",
                "supportsAllDrives": "true",
                "useDomainAdminAccess": "false",
            },
        )
        raw = (
            payload.get("permissions")
            if isinstance(payload.get("permissions"), list)
            else []
        )
        return [
            DrivePermission.from_api(item) for item in raw if isinstance(item, dict)
        ]

    async def update_file(
        self, subject: str, file_id: str, changes: Dict[str, Any]
    ) -> DriveFile:
        allowed = {
            k: changes[k] for k in ("name", "description", "starred") if k in changes
        }
        if not allowed:
            raise ValueError("No supported Drive metadata changes were supplied.")
        payload = await self._request(
            subject,
            "PATCH",
            f"/files/{quote(file_id, safe='')}",
            params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
            json_body=allowed,
        )
        return DriveFile.from_api(payload)

    async def create_permission(
        self,
        subject: str,
        file_id: str,
        *,
        principal_type: str,
        email: str,
        role: str,
    ) -> DrivePermission:
        payload = await self._request(
            subject,
            "POST",
            f"/files/{quote(file_id, safe='')}/permissions",
            params={
                "fields": PERMISSION_FIELDS,
                "supportsAllDrives": "true",
                "sendNotificationEmail": "false",
            },
            json_body={"type": principal_type, "emailAddress": email, "role": role},
        )
        return DrivePermission.from_api(payload)

    async def update_permission(
        self, subject: str, file_id: str, permission_id: str, role: str
    ) -> DrivePermission:
        payload = await self._request(
            subject,
            "PATCH",
            f"/files/{quote(file_id, safe='')}/permissions/{quote(permission_id, safe='')}",
            params={"fields": PERMISSION_FIELDS, "supportsAllDrives": "true"},
            json_body={"role": role},
        )
        return DrivePermission.from_api(payload)

    async def delete_permission(
        self, subject: str, file_id: str, permission_id: str
    ) -> None:
        await self._request(
            subject,
            "DELETE",
            f"/files/{quote(file_id, safe='')}/permissions/{quote(permission_id, safe='')}",
            params={"supportsAllDrives": "true"},
        )

    async def download(
        self,
        subject: str,
        file_id: str,
        *,
        export_mime: str = "",
        max_bytes: int = MAX_PREVIEW_BYTES,
    ) -> bytes:
        token = await self.token_provider.token_for(subject)
        path = (
            f"/files/{quote(file_id, safe='')}/export"
            if export_mime
            else f"/files/{quote(file_id, safe='')}"
        )
        params = {"mimeType": export_mime} if export_mime else {"alt": "media"}
        headers = {"Authorization": f"Bearer {token}", "Accept": export_mime or "*/*"}
        async with self.http.stream(
            "GET", f"{API_ROOT}{path}", params=params, headers=headers
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise _error_from_response(response)
            length = response.headers.get("content-length")
            if length and int(length) > max_bytes:
                raise ValueError("This file is larger than the 10 MB preview limit.")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ValueError(
                        "This file is larger than the 10 MB preview limit."
                    )
            return bytes(body)
