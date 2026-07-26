from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from gamgui.core.activity import ActivityRegistry
from gamgui.core.components import ComponentManager
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.paths import APP_DATA_ENV
from gamgui.core.updater import UpdateStateStore
from gamgui.web.server import AppState, create_app


class RecordingVault:
    def __init__(self, domains=()) -> None:
        self.domains = list(domains)
        self.list_calls = 0

    def list_domains(self):
        self.list_calls += 1
        return list(self.domains)


def _state(tmp_path: Path, monkeypatch, profile: str) -> AppState:
    monkeypatch.setenv("GAMGUI_BUILD_PROFILE", profile)
    vault = RecordingVault()
    runner = GAMRunner(
        vault=vault,
        gam_binary=tmp_path / "missing-gam",
        base_dir=tmp_path,
    )
    manager = ComponentManager(
        store=UpdateStateStore(tmp_path / "updates" / "state.json"),
        registry=ActivityRegistry(),
        data_root=tmp_path,
    )
    return AppState(
        vault=vault,
        runner=runner,
        token="test-token",
        component_manager=manager,
        activity_registry=manager.registry,
    )


def test_first_launch_does_not_enumerate_workspace_keychain(tmp_path, monkeypatch):
    monkeypatch.setenv(APP_DATA_ENV, str(tmp_path))
    vault = RecordingVault(("district.example",))

    state = AppState.create(vault=vault, token="test-token")

    assert vault.list_calls == 0
    assert state.connector is None
    assert state.component_manager is not None
    assert state.component_manager.first_run_choice_pending()


def test_first_http_navigation_stays_component_only_without_keychain_reads(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(APP_DATA_ENV, str(tmp_path))
    vault = RecordingVault(("district.example",))
    state = AppState.create(vault=vault, token="test-token")
    client = TestClient(create_app(state))

    response = client.get("/?token=test-token")

    assert response.status_code == 200
    assert response.url.path == "/setup"
    assert "Choose optional features" in response.text
    assert "Connect Google Workspace" not in response.text
    assert vault.list_calls == 0


def test_component_choice_allows_workspace_discovery_on_next_launch(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(APP_DATA_ENV, str(tmp_path))
    first = AppState.create(vault=RecordingVault(), token="first")
    first.component_manager.skip_first_run()
    vault = RecordingVault(("district.example",))

    second = AppState.create(vault=vault, token="second")

    assert vault.list_calls == 1
    assert second.audit_domain == "district.example"
    assert second.connector is not None


def test_core_profile_registers_install_explanation_without_optional_route_module(
    tmp_path, monkeypatch
):
    state = _state(tmp_path, monkeypatch, "core")
    client = TestClient(create_app(state))

    response = client.get("/classroom/imports?token=test-token")

    assert response.status_code == 200
    assert "Install OneRoster" in response.text
    assert "Upload OneRoster ZIP" not in response.text


def test_full_profile_route_failure_degrades_only_optional_component(
    tmp_path, monkeypatch
):
    state = _state(tmp_path, monkeypatch, "classroom-oneroster")
    from gamgui.web import server

    original = server.importlib.import_module

    def fail_optional(name: str):
        if name == "gamgui.web.routes.oneroster":
            raise ImportError("deliberately corrupt optional route")
        return original(name)

    monkeypatch.setattr(server.importlib, "import_module", fail_optional)
    client = TestClient(create_app(state))

    health = client.get("/healthz")
    deep_link = client.get("/classroom/imports?token=test-token")

    assert health.status_code == 200
    assert health.json() == {"ok": True}
    assert deep_link.status_code == 200
    assert state.oneroster_error_code == "CMP-INCOMPATIBLE"
    assert "deliberately corrupt" not in deep_link.text


def test_corrupt_embedded_manifest_still_starts_core_recovery_surface(
    tmp_path, monkeypatch
):
    manifest = tmp_path / "profile.json"
    manifest.write_text('{"artifact": {"profile": "classroom-oneroster"}}')
    monkeypatch.setenv("GAMGUI_PROFILE_MANIFEST", str(manifest))
    vault = RecordingVault()
    runner = GAMRunner(
        vault=vault,
        gam_binary=tmp_path / "missing-gam",
        base_dir=tmp_path,
    )
    state = AppState(
        vault=vault,
        runner=runner,
        token="test-token",
        activity_registry=ActivityRegistry(),
    )
    client = TestClient(create_app(state))

    health = client.get("/healthz")
    components = client.get("/components?token=test-token")

    assert health.status_code == 200
    assert components.status_code == 200
    assert "Degraded" in components.text
    assert "CMP-VERIFY-FAILED" in components.text


def test_purge_rebinds_oneroster_service_without_restart(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch, "classroom-oneroster")
    state.audit_domain = "district.example"
    state.component_manager.enable()
    state.ensure_component_services()
    original = state.oneroster_service
    assert original is not None
    assert original.store.state_path.is_file()
    client = TestClient(create_app(state))

    purged = client.post(
        "/components/oneroster/data/purge?token=test-token",
        data={"confirmation": "OneRoster"},
    )

    assert purged.status_code == 200
    assert "permanently removed" in purged.text
    assert state.oneroster_service is not None
    assert state.oneroster_service is not original
    assert state.oneroster_service.store.state_path.is_file()

    imports = client.get("/classroom/imports?token=test-token")
    assert imports.status_code == 200
    assert "Classroom / OneRoster" in imports.text
    assert "Import Studio" in imports.text
    assert "CMP-INCOMPATIBLE" not in imports.text
