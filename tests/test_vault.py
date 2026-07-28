from __future__ import annotations

import ctypes
import os
import sys
from ctypes import c_long, c_void_p
from types import SimpleNamespace
from uuid import uuid4

import pytest
from keyring.errors import PasswordSetError

from gamgui.core.secrets import vault as vault_module
from gamgui.core.secrets.vault import (
    InMemoryBackend,
    SecretsVault,
    _DarwinSecurityAPI,
    _KeyringBackend,
    _set_darwin_password,
)


@pytest.fixture
def empty_vault() -> SecretsVault:
    return SecretsVault(backend=InMemoryBackend())


def test_set_get_roundtrip(empty_vault):
    empty_vault.set("a.com", "oauth2", "tok")
    assert empty_vault.get("a.com", "oauth2") == "tok"


def test_unknown_name_raises(empty_vault):
    with pytest.raises(ValueError):
        empty_vault.get("a.com", "not_a_credential")


def test_has_credentials_requires_service_and_oauth(empty_vault):
    assert empty_vault.has_credentials("a.com") is False
    empty_vault.set("a.com", "oauth2service", "{}")
    assert empty_vault.has_credentials("a.com") is False
    empty_vault.set("a.com", "oauth2", "tok")
    assert empty_vault.has_credentials("a.com") is True


def test_domain_index_and_clear(empty_vault):
    empty_vault.set("a.com", "oauth2", "t")
    empty_vault.set("b.com", "oauth2", "t")
    assert empty_vault.list_domains() == ["a.com", "b.com"]
    empty_vault.clear_domain("a.com")
    assert empty_vault.get("a.com", "oauth2") is None
    assert empty_vault.list_domains() == ["b.com"]


def test_get_all_returns_all_names(empty_vault):
    empty_vault.set("a.com", "oauth2", "t")
    allc = empty_vault.get_all("a.com")
    assert set(allc.keys()) == {"client_secrets", "oauth2", "oauth2service"}
    assert allc["oauth2"] == "t"
    assert allc["client_secrets"] is None


class _CountingBackend(InMemoryBackend):
    """In-memory backend that counts reads, to prove the cache avoids Keychain prompts."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def get_password(self, service: str, username: str):
        self.reads += 1
        return super().get_password(service, username)


def test_cache_avoids_repeat_backend_reads():
    backend = _CountingBackend()
    v = SecretsVault(backend=backend, cache_ttl=300)
    v.set("a.com", "oauth2", "tok")   # seeds the cache
    backend.reads = 0
    for _ in range(5):
        assert v.get("a.com", "oauth2") == "tok"
    assert backend.reads == 0          # all served from the session cache -> no repeat Keychain prompts


def test_clear_cache_forces_reread():
    backend = _CountingBackend()
    v = SecretsVault(backend=backend, cache_ttl=300)
    v.set("a.com", "oauth2", "tok")
    v.clear_cache()                    # explicit "lock"
    backend.reads = 0
    assert v.get("a.com", "oauth2") == "tok"
    assert backend.reads == 1          # re-locked: one fresh backend read


def test_cache_ttl_zero_disables_caching():
    backend = _CountingBackend()
    v = SecretsVault(backend=backend, cache_ttl=0)
    v.set("a.com", "oauth2", "tok")
    backend.reads = 0
    v.get("a.com", "oauth2")
    v.get("a.com", "oauth2")
    assert backend.reads == 2          # caching disabled: every read hits the backend


def test_delete_invalidates_cache():
    backend = _CountingBackend()
    v = SecretsVault(backend=backend, cache_ttl=300)
    v.set("a.com", "oauth2", "tok")
    v.delete("a.com", "oauth2")
    assert v.get("a.com", "oauth2") is None  # not a stale cached "tok"


class _SecurityStatusError(Exception):
    pass


class _FakeSecurityAPI:
    item_not_found = -25300
    duplicate_item = -25299

    def __init__(self, update_status=0, add_status=0):
        self.update_status = update_status
        self.add_status = add_status
        self.calls = []

    @staticmethod
    def _status(value):
        return value.pop(0) if isinstance(value, list) else value

    def update_generic_password(self, service, username, password):
        self.calls.append(("update", service, username, password))
        return self._status(self.update_status)

    def add_generic_password(self, service, username, password):
        self.calls.append(("add", service, username, password))
        return self._status(self.add_status)

    @staticmethod
    def raise_for_status(status):
        if status:
            raise _SecurityStatusError(status)


class _FakeNativeFunction:
    def __init__(self, implementation):
        self._implementation = implementation
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self._implementation(*args)


class _FakeCoreFoundation:
    def __init__(self):
        self.data = {}
        self.released = []
        self.CFDataCreate = _FakeNativeFunction(self._create_data)
        self.CFRelease = _FakeNativeFunction(self._release)

    def _create_data(self, _allocator, byte_pointer, length):
        reference = 0xC000 + len(self.data)
        self.data[reference] = ctypes.string_at(byte_pointer, length)
        return reference

    def _release(self, reference):
        self.released.append(
            reference.value if isinstance(reference, c_void_p) else reference
        )


class _FakeMacOSError(Exception):
    @classmethod
    def raise_for_status(cls, status):
        if status:
            raise cls(status, "Unknown Error")


class _FakeMacOSDenied(_FakeMacOSError):
    pass


class _FakeMacOSAPI:
    error = SimpleNamespace(item_not_found=-25300)
    OS_status = ctypes.c_int32
    Error = _FakeMacOSError
    KeychainDenied = _FakeMacOSDenied

    def __init__(self, update_status=0, add_status=0):
        self.update_status = update_status
        self.add_status = add_status
        self._sec = SimpleNamespace(
            SecItemUpdate=_FakeNativeFunction(
                lambda _query, _attributes: self.update_status
            )
        )
        self.SecItemAdd = _FakeNativeFunction(
            lambda _query, _result: self.add_status
        )

    @staticmethod
    def k_(name):
        return f"constant:{name}"

    @staticmethod
    def create_query(**attributes):
        return attributes


def test_darwin_password_updates_existing_item_in_place():
    api = _FakeSecurityAPI()

    _set_darwin_password(api, "gamgui:a.com", "oauth2", "new-token")

    assert api.calls == [("update", "gamgui:a.com", "oauth2", "new-token")]


def test_darwin_password_adds_only_when_item_is_absent():
    api = _FakeSecurityAPI(update_status=_FakeSecurityAPI.item_not_found)

    _set_darwin_password(api, "gamgui:a.com", "oauth2", "new-token")

    assert api.calls == [
        ("update", "gamgui:a.com", "oauth2", "new-token"),
        ("add", "gamgui:a.com", "oauth2", "new-token"),
    ]


def test_darwin_password_retries_update_after_concurrent_create():
    api = _FakeSecurityAPI(
        update_status=[_FakeSecurityAPI.item_not_found, 0],
        add_status=_FakeSecurityAPI.duplicate_item,
    )

    _set_darwin_password(api, "gamgui:a.com", "oauth2", "new-token")

    assert [call[0] for call in api.calls] == ["update", "add", "update"]


@pytest.mark.parametrize(
    ("update_status", "add_status", "expected_calls"),
    [
        (-50, 0, ["update"]),
        (_FakeSecurityAPI.item_not_found, -25293, ["update", "add"]),
    ],
)
def test_darwin_password_propagates_native_errors_without_destructive_fallback(
    update_status, add_status, expected_calls
):
    api = _FakeSecurityAPI(update_status=update_status, add_status=add_status)

    with pytest.raises(_SecurityStatusError):
        _set_darwin_password(api, "gamgui:a.com", "oauth2", "new-token")

    assert [call[0] for call in api.calls] == expected_calls


@pytest.mark.parametrize("operation", ["update_generic_password", "add_generic_password"])
def test_darwin_security_adapter_passes_utf8_cfdata_to_native_writes(operation):
    macos_api = _FakeMacOSAPI()
    core_foundation = _FakeCoreFoundation()
    api = _DarwinSecurityAPI(
        macos_api=macos_api,
        core_foundation=core_foundation,
    )
    value = "T\u00f8ken \U0001f510\0tail"

    status = getattr(api, operation)("gamgui:a.com", "oauth2", value)

    assert status == 0
    assert tuple(core_foundation.CFDataCreate.argtypes) == (
        c_void_p,
        c_void_p,
        c_long,
    )
    [(data_reference, encoded_value)] = core_foundation.data.items()
    assert encoded_value == value.encode("utf-8")
    identity = {
        "kSecClass": "constant:kSecClassGenericPassword",
        "kSecAttrService": "gamgui:a.com",
        "kSecAttrAccount": "oauth2",
    }
    if operation == "update_generic_password":
        [(actual_identity, value_attributes)] = macos_api._sec.SecItemUpdate.calls
        assert actual_identity == identity
        assert macos_api.SecItemAdd.calls == []
    else:
        assert macos_api._sec.SecItemUpdate.calls == []
        [(value_attributes, result_pointer)] = macos_api.SecItemAdd.calls
        assert result_pointer is None
        assert {
            key: value
            for key, value in value_attributes.items()
            if key != "kSecValueData"
        } == identity

    data_pointer = value_attributes["kSecValueData"]
    assert type(data_pointer) is c_void_p
    assert data_pointer.value == data_reference
    assert core_foundation.released[0] == data_reference
    if operation == "update_generic_password":
        assert core_foundation.released[1:] == [value_attributes, identity]
    else:
        assert core_foundation.released[1:] == [value_attributes]


def test_darwin_security_adapter_error_does_not_include_password():
    macos_api = _FakeMacOSAPI(update_status=-50)
    api = _DarwinSecurityAPI(
        macos_api=macos_api,
        core_foundation=_FakeCoreFoundation(),
    )
    value = "exception-message-canary"

    status = api.update_generic_password("gamgui:a.com", "oauth2", value)
    with pytest.raises(PasswordSetError) as exc_info:
        api.raise_for_status(status)

    assert value not in str(exc_info.value)


def _keyring_module(backend):
    calls = []
    return (
        SimpleNamespace(
            get_keyring=lambda: backend,
            set_password=lambda *args: calls.append(("set", *args)),
            get_password=lambda *args: calls.append(("get", *args)),
            delete_password=lambda *args: calls.append(("delete", *args)),
        ),
        calls,
    )


def test_keyring_backend_uses_acl_preserving_path_on_darwin(monkeypatch):
    native_type = type("Keyring", (), {})
    native_type.__module__ = "keyring.backends.macOS"
    keyring, keyring_calls = _keyring_module(native_type())
    api = _FakeSecurityAPI()
    monkeypatch.setattr(vault_module.sys, "platform", "darwin")
    backend = _KeyringBackend(keyring_module=keyring, darwin_api=api)

    backend.set_password("gamgui:a.com", "oauth2", "new-token")

    assert api.calls == [("update", "gamgui:a.com", "oauth2", "new-token")]
    assert keyring_calls == []


def test_keyring_backend_keeps_custom_backend_on_darwin(monkeypatch):
    custom_backend = object()
    keyring, keyring_calls = _keyring_module(custom_backend)
    api = _FakeSecurityAPI()
    monkeypatch.setattr(vault_module.sys, "platform", "darwin")
    backend = _KeyringBackend(keyring_module=keyring, darwin_api=api)

    backend.get_password("gamgui:a.com", "oauth2")
    backend.set_password("gamgui:a.com", "oauth2", "new-token")
    backend.delete_password("gamgui:a.com", "oauth2")

    assert keyring_calls == [
        ("get", "gamgui:a.com", "oauth2"),
        ("set", "gamgui:a.com", "oauth2", "new-token"),
        ("delete", "gamgui:a.com", "oauth2"),
    ]
    assert api.calls == []


def test_keyring_backend_keeps_keyring_write_path_off_darwin(monkeypatch):
    keyring, keyring_calls = _keyring_module(object())
    api = _FakeSecurityAPI()
    monkeypatch.setattr(vault_module.sys, "platform", "linux")
    backend = _KeyringBackend(keyring_module=keyring, darwin_api=api)

    backend.set_password("gamgui:a.com", "oauth2", "new-token")

    assert keyring_calls == [("set", "gamgui:a.com", "oauth2", "new-token")]
    assert api.calls == []


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Security.framework")
def test_darwin_security_adapter_contract():
    api = _DarwinSecurityAPI()

    assert api.item_not_found == -25300
    assert api.duplicate_item == -25299
    assert tuple(api._update.argtypes) == (c_void_p, c_void_p)
    assert tuple(api._create_data.argtypes) == (c_void_p, c_void_p, c_long)
    assert api._create_data.restype is c_void_p
    assert tuple(api._release.argtypes) == (c_void_p,)

    with pytest.raises(PasswordSetError):
        api.raise_for_status(-50)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Security.framework")
def test_darwin_security_adapter_keychain_roundtrip():
    api = _DarwinSecurityAPI()
    service = f"gamgui-contract:{uuid4()}"
    username = "contract-test"
    initial_value = "initial-T\u00f8ken-\U0001f510"
    updated_value = "updated-\u96ea-\U0001f510"
    unavailable_statuses = {
        api._api.error.keychain_denied,
        api._api.error.sec_interaction_not_allowed,
        -25294,  # errSecNoSuchKeychain
        -25291,  # errSecNotAvailable
    }
    created = False
    primary_failure = None
    try:
        add_status = api.add_generic_password(service, username, initial_value)
        if add_status in unavailable_statuses:
            if os.environ.get("CI"):
                pytest.fail(
                    "The required macOS Keychain round-trip could not start "
                    f"(OSStatus {add_status})."
                )
            pytest.skip(f"macOS Keychain unavailable (OSStatus {add_status})")
        created = add_status == 0
        api.raise_for_status(add_status)
        assert (
            api._api.find_generic_password(None, service, username)
            == initial_value
        )

        api.raise_for_status(
            api.update_generic_password(service, username, updated_value)
        )
        assert (
            api._api.find_generic_password(None, service, username)
            == updated_value
        )
    except BaseException as exc:
        primary_failure = exc
        raise
    finally:
        if created:
            try:
                api._api.delete_generic_password(None, service, username)
            except Exception:
                if primary_failure is None:
                    raise
