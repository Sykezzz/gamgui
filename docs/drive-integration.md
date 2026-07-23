# Drive module integration

The Drive lane deliberately does not edit `server.py`, `base.html`, or `user_detail.html`, because
those files are shared with the performance and Classroom lanes.

## Application state

Add `Capability.DRIVE` to the connector capability enum and the Google Workspace connector's
capability set. Add an optional `drive_service` field to `AppState`. When a domain is connected,
construct it from the credentials already held in the vault:

```python
token_provider = ServiceAccountTokenProvider(vault, domain)
drive_client = DriveAPIClient(token_provider)
drive_service = DriveService(
    drive_client,
    runner,
    domain,
    audit=connector.audit,
    resolver=ConnectorDirectoryResolver(connector),
)
```

Prefer the new `DirectoryIndex` as the injected resolver once that lane lands; the connector
resolver is a correctness fallback and its exact group check reads the group directory. Close the
Drive API client's owned `httpx.AsyncClient` during application shutdown.

Register `gamgui.web.routes.drive.router` in `create_app`.

## Lazy user tab

Add a `Drive` user-detail tab and an initially empty panel. On the first real tab activation only,
issue:

```text
GET /drive/user?email=<selected primary email>
```

Swap the response into the panel and mark it loaded so repeated tab selections do not repeat the
request. Do not use `hx-trigger="load"` on a hidden panel. The panel itself handles bounded search,
opaque cursor paging, file selection, permissions, preview, and ownership workflows.

## Delegated scopes and release verification

The client requests `https://www.googleapis.com/auth/drive` because the requested feature set
includes metadata edits, permission changes, downloads/exports, and ownership changes. Setup
should display that exact scope only when verification reports it missing.

Before production use, exercise read-only list/detail/permission/preview calls against throwaway
files, then separately approve mutation tests. The offline tests do not authorize or prove the
district tenant's DWD configuration.
