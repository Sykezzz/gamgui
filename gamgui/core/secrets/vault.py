"""Secret storage.

The canonical home for GAM's credentials is the macOS Keychain. We keep three items per
Workspace domain:

* ``client_secrets`` → ``client_secrets.json`` (OAuth client)
* ``oauth2``         → ``oauth2.txt``          (admin refresh token; ≈ admin password)
* ``oauth2service``  → ``oauth2service.json``  (service-account key; can impersonate anyone)

The vault is backend-pluggable so tests (and headless CI) can use an in-memory store instead of
the real Keychain. The default backend uses ``keyring``, which maps to the macOS Keychain.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from typing import Dict, Optional, Protocol, Tuple

# Logical credential name -> the filename GAM expects inside GAMCFGDIR.
FILENAMES: Dict[str, str] = {
    "client_secrets": "client_secrets.json",
    "oauth2": "oauth2.txt",
    "oauth2service": "oauth2service.json",
}
CREDENTIAL_NAMES = tuple(FILENAMES.keys())

# Credentials required before GAM can act as the domain (service-account flow).
_REQUIRED = ("oauth2service", "oauth2")

_INDEX_SERVICE = "gamgui"
_INDEX_KEY = "_domains"


class VaultBackend(Protocol):
    """Minimal secret store interface (a subset of keyring's API)."""

    def get_password(self, service: str, username: str) -> Optional[str]: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class InMemoryBackend:
    """Backend for tests — keeps secrets in a dict. Never touches the OS Keychain."""

    def __init__(self) -> None:
        self._store: Dict[str, str] = {}

    @staticmethod
    def _k(service: str, username: str) -> str:
        return f"{service}\x00{username}"

    def get_password(self, service: str, username: str) -> Optional[str]:
        return self._store.get(self._k(service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[self._k(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._store.pop(self._k(service, username), None)


class _DarwinSecurityAPI:
    """Small, injectable Security.framework adapter for ACL-preserving password writes."""

    def __init__(self, macos_api=None, core_foundation=None) -> None:
        if macos_api is None:
            from keyring.backends.macOS import api as macos_api

        if core_foundation is None:
            core_foundation = macos_api._found

        self._api = macos_api
        self.item_not_found = macos_api.error.item_not_found
        self.duplicate_item = -25299  # errSecDuplicateItem
        self._update = macos_api._sec.SecItemUpdate
        self._update.restype = macos_api.OS_status
        self._update.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        self._create_data = core_foundation.CFDataCreate
        self._create_data.restype = ctypes.c_void_p
        self._create_data.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
        )
        self._release = core_foundation.CFRelease
        self._release.restype = None
        self._release.argtypes = (ctypes.c_void_p,)

    def _identity(self, service: str, username: str) -> dict:
        return {
            "kSecClass": self._api.k_("kSecClassGenericPassword"),
            "kSecAttrService": service,
            "kSecAttrAccount": username or "",
        }

    def _query_with_password(self, password: str, **attributes) -> object:
        encoded = password.encode("utf-8")
        buffer = ctypes.create_string_buffer(encoded)
        data = self._create_data(
            None,
            ctypes.cast(buffer, ctypes.c_void_p),
            len(encoded),
        )
        if not data:
            raise MemoryError("Unable to allocate Keychain password data")
        data_pointer = ctypes.c_void_p(data)
        try:
            # keyring's query uses CFType callbacks, so the dictionary retains the data.
            return self._api.create_query(
                **attributes,
                kSecValueData=data_pointer,
            )
        finally:
            self._release(data_pointer)

    def update_generic_password(self, service: str, username: str, password: str) -> int:
        query = self._api.create_query(**self._identity(service, username))
        attributes = None
        try:
            attributes = self._query_with_password(password)
            return int(self._update(query, attributes))
        finally:
            if attributes:
                self._release(attributes)
            if query:
                self._release(query)

    def add_generic_password(self, service: str, username: str, password: str) -> int:
        query = self._query_with_password(
            password,
            **self._identity(service, username),
        )
        try:
            return int(self._api.SecItemAdd(query, None))
        finally:
            if query:
                self._release(query)

    def raise_for_status(self, status: int) -> None:
        try:
            self._api.Error.raise_for_status(status)
        except self._api.KeychainDenied as exc:
            from keyring.errors import KeyringLocked

            raise KeyringLocked(f"Can't store password on keychain: {exc}") from exc
        except self._api.Error as exc:
            from keyring.errors import PasswordSetError

            raise PasswordSetError(f"Can't store password on keychain: {exc}") from exc


def _set_darwin_password(api, service: str, username: str, password: str) -> None:
    """Update an existing item in place so its macOS Keychain ACL remains attached."""

    status = api.update_generic_password(service, username, password)
    if status == api.item_not_found:
        status = api.add_generic_password(service, username, password)
        if status == api.duplicate_item:
            # Another app instance created the item between our update and add.
            status = api.update_generic_password(service, username, password)
    api.raise_for_status(status)


def _uses_native_macos_keyring(keyring_module) -> bool:
    """Return whether keyring selected its built-in macOS Keychain backend."""

    if sys.platform != "darwin":
        return False
    try:
        backend_type = type(keyring_module.get_keyring())
    except Exception:
        return False
    return (
        backend_type.__module__ == "keyring.backends.macOS"
        and backend_type.__name__ == "Keyring"
    )


class _KeyringBackend:
    """Default backend — lazily imports ``keyring`` so core tests don't require it installed."""

    def __init__(self, keyring_module=None, darwin_api=None) -> None:
        if keyring_module is None:
            import keyring as keyring_module

        self._keyring = keyring_module
        self._uses_darwin_api = _uses_native_macos_keyring(keyring_module)
        self._darwin_api = darwin_api if self._uses_darwin_api else None
        if self._uses_darwin_api and self._darwin_api is None:
            self._darwin_api = _DarwinSecurityAPI()

    def get_password(self, service: str, username: str) -> Optional[str]:
        return self._keyring.get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        if self._uses_darwin_api:
            _set_darwin_password(self._darwin_api, service, username, password)
            return
        self._keyring.set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        try:
            self._keyring.delete_password(service, username)
        except Exception:
            # keyring raises PasswordDeleteError if absent; deleting a missing item is a no-op.
            pass


class SecretsVault:
    # Default lifetime (seconds) for the in-process secret cache: a "session-reuse window" so a
    # burst of gam calls doesn't re-prompt the Keychain on every read. Sliding (extends on use).
    # Override with env GAMGUI_SECRET_CACHE_TTL; set to 0 to disable (re-read the Keychain every call).
    DEFAULT_CACHE_TTL = 300.0

    def __init__(self, backend: Optional[VaultBackend] = None, cache_ttl: Optional[float] = None) -> None:
        self.backend: VaultBackend = backend or _KeyringBackend()
        if cache_ttl is None:
            try:
                cache_ttl = float(os.environ.get("GAMGUI_SECRET_CACHE_TTL", self.DEFAULT_CACHE_TTL))
            except ValueError:
                cache_ttl = self.DEFAULT_CACHE_TTL
        self._cache_ttl = max(0.0, cache_ttl)
        self._cache: Dict[Tuple[str, str], Tuple[Optional[str], float]] = {}

    @staticmethod
    def _service(domain: str) -> str:
        return f"gamgui:{domain}"

    def clear_cache(self) -> None:
        """Forget cached secrets so the next read re-prompts the Keychain (an explicit 'lock')."""
        self._cache.clear()

    # --- single credential -------------------------------------------------------------
    def get(self, domain: str, name: str) -> Optional[str]:
        _check_name(name)
        key = (domain, name)
        if self._cache_ttl:
            now = time.monotonic()
            hit = self._cache.get(key)
            if hit is not None and hit[1] > now:
                self._cache[key] = (hit[0], now + self._cache_ttl)  # sliding: extend on use
                return hit[0]
        value = self.backend.get_password(self._service(domain), name)
        if self._cache_ttl:
            self._cache[key] = (value, time.monotonic() + self._cache_ttl)
        return value

    def set(self, domain: str, name: str, value: str) -> None:
        _check_name(name)
        self.backend.set_password(self._service(domain), name, value)
        if self._cache_ttl:
            self._cache[(domain, name)] = (value, time.monotonic() + self._cache_ttl)
        self._register_domain(domain)

    def delete(self, domain: str, name: str) -> None:
        _check_name(name)
        self.backend.delete_password(self._service(domain), name)
        self._cache.pop((domain, name), None)

    # --- whole credential set ----------------------------------------------------------
    def get_all(self, domain: str) -> Dict[str, Optional[str]]:
        return {name: self.get(domain, name) for name in CREDENTIAL_NAMES}

    def set_all(self, domain: str, creds: Dict[str, str]) -> None:
        for name, value in creds.items():
            if value is not None:
                self.set(domain, name, value)

    def has_credentials(self, domain: str) -> bool:
        return all(self.get(domain, name) for name in _REQUIRED)

    def clear_domain(self, domain: str) -> None:
        for name in CREDENTIAL_NAMES:
            self.delete(domain, name)
        self._unregister_domain(domain)

    # --- domain index ------------------------------------------------------------------
    def list_domains(self) -> list:
        raw = self.backend.get_password(_INDEX_SERVICE, _INDEX_KEY)
        try:
            return sorted(json.loads(raw)) if raw else []
        except ValueError:  # json.JSONDecodeError is a subclass of ValueError
            return []

    def _register_domain(self, domain: str) -> None:
        domains = set(self.list_domains())
        if domain not in domains:
            domains.add(domain)
            self.backend.set_password(_INDEX_SERVICE, _INDEX_KEY, json.dumps(sorted(domains)))

    def _unregister_domain(self, domain: str) -> None:
        domains = set(self.list_domains())
        if domain in domains:
            domains.discard(domain)
            self.backend.set_password(_INDEX_SERVICE, _INDEX_KEY, json.dumps(sorted(domains)))


def _check_name(name: str) -> None:
    if name not in CREDENTIAL_NAMES:
        raise ValueError(f"unknown credential name {name!r}; expected one of {CREDENTIAL_NAMES}")
