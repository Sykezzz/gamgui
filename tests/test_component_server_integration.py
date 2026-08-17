from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from gamgui.core.activity import ActivityRegistry
from gamgui.core.components import (
    CORE_PROFILE,
    ONEROSTER_COMPONENT,
    ONEROSTER_PROFILE,
    ComponentManager,
)
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.paths import APP_DATA_ENV
from gamgui.core.updater import (
    ACTIVATION_CURRENT_APP_ENV,
    ACTIVATION_PROBE_ENV,
    ACTIVATION_TRANSACTION_ENV,
    INSTALLED_SOURCE_EVIDENCE,
    UpdateState,
    UpdateStateStore,
    write_health_marker_from_environment,
)
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


def test_activation_probe_exposes_only_local_health_surface(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(APP_DATA_ENV, str(tmp_path))
    monkeypatch.setenv("GAMGUI_BUILD_PROFILE", CORE_PROFILE)
    monkeypatch.setenv("GAMGUI_SOURCE_SHA", "a" * 40)
    monkeypatch.setattr(
        "gamgui.web.server.sweep_stale_configs",
        lambda: (_ for _ in ()).throw(
            AssertionError("activation probe ran credential cleanup")
        ),
    )
    state = AppState.create_activation_probe(token="probe-token")

    def unexpected_access(*_args, **_kwargs):
        raise AssertionError("activation probe accessed Workspace or GAM")

    for name in ("list_domains", "has_credentials", "get", "get_all"):
        monkeypatch.setattr(state.vault, name, unexpected_access)
    monkeypatch.setattr(state.runner, "version", unexpected_access)
    monkeypatch.setattr(state.runner, "run_authenticated", unexpected_access)
    app = create_app(state)

    with TestClient(app) as client:
        page = client.get("/?token=probe-token")
        assert page.status_code == 200
        assert "Finishing verified update" in page.text
        assert "Workspace access remains paused" in page.text
        assert client.get("/healthz").json() == {"ok": True}
        blocked = client.get("/setup")
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "CMP-ACTIVE-JOB"


def test_explicit_canary_start_can_resolve_workspace_in_disposable_first_run(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(APP_DATA_ENV, str(tmp_path))
    vault = RecordingVault(("district.example",))

    state = AppState.create(
        vault=vault,
        token="canary-token",
        preferred_domain="district.example",
        allow_first_run_workspace_access=True,
    )

    assert state.component_manager.first_run_choice_pending()
    assert vault.list_calls == 1
    assert state.audit_domain == "district.example"
    assert state.connector is not None
    assert state.drive_service is not None


def test_activation_health_requires_enabled_oneroster_service(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(APP_DATA_ENV, str(tmp_path))
    monkeypatch.setenv("GAMGUI_BUILD_PROFILE", ONEROSTER_PROFILE)
    monkeypatch.setenv("GAMGUI_SOURCE_SHA", "a" * 40)
    manager = ComponentManager(
        store=UpdateStateStore(tmp_path / "updates" / "state.json"),
        registry=ActivityRegistry(),
        data_root=tmp_path,
    )
    candidate = manager.embedded.artifact
    transaction = "1" * 32
    bundle_name = "GamGUI" if candidate.platform == "windows" else "GamGUI.app"
    pending = (
        manager.store.path.parent
        / "pending"
        / candidate.source_sha
        / ONEROSTER_PROFILE
        / bundle_name
    )
    current = (
        tmp_path / "Applications" / "current"
        if candidate.platform == "windows"
        else tmp_path / "Applications" / "GamGUI.app"
    )
    for bundle in (pending, current):
        executable = (
            bundle / "GamGUI.exe"
            if candidate.platform == "windows"
            else bundle / "Contents" / "MacOS" / "GamGUI"
        )
        executable.parent.mkdir(parents=True)
        executable.write_text("binary", encoding="utf-8")
    manager.store.save(
        UpdateState(
            installed_sha=candidate.source_sha,
            candidate_sha=candidate.source_sha,
            pending_app=str(pending),
            installed_profile=CORE_PROFILE,
            desired_profile=ONEROSTER_PROFILE,
            desired_components=[ONEROSTER_COMPONENT],
            candidate_artifact=candidate,
            activation_kind="component-swap",
            component_prompt_answered=True,
            required_check_evidence=[INSTALLED_SOURCE_EVIDENCE],
            activation_transaction_id=transaction,
        )
    )
    monkeypatch.setenv("GAMGUI_SKIP_UPDATE_ONCE", "1")
    monkeypatch.setenv(ACTIVATION_PROBE_ENV, "1")
    monkeypatch.setenv(ACTIVATION_TRANSACTION_ENV, transaction)
    monkeypatch.setenv(ACTIVATION_CURRENT_APP_ENV, str(current))
    monkeypatch.setenv("GAMGUI_INSTALLED_SHA", candidate.source_sha)
    monkeypatch.setenv(
        "GAMGUI_UPDATE_HEALTH_MARKER",
        str(manager.store.path.parent / "health" / f"{transaction}.json"),
    )
    state = AppState(
        vault=RecordingVault(("district.example",)),
        runner=GAMRunner(
            vault=RecordingVault(("district.example",)),
            base_dir=tmp_path,
        ),
        audit_domain="district.example",
        connector=SimpleNamespace(domain="district.example"),
        component_manager=manager,
        token="activation",
    )

    assert state.oneroster_service is not None
    assert state.update_activation_ready()
    state.oneroster_service = None
    assert not state.update_activation_ready()
    state.oneroster_error_code = "CMP-INCOMPATIBLE"
    assert not state.update_activation_ready()


def test_activation_probe_blocks_mutations_and_scheduler_until_commit(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(APP_DATA_ENV, str(tmp_path))
    sha = "a" * 40
    monkeypatch.setenv("GAMGUI_SOURCE_SHA", sha)
    state = _state(tmp_path, monkeypatch, CORE_PROFILE)
    state.component_manager.store.save(
        UpdateState(
            installed_sha="b" * 40,
            candidate_sha=sha,
        )
    )
    monkeypatch.setenv(ACTIVATION_PROBE_ENV, "1")
    monkeypatch.setenv("GAMGUI_INSTALLED_SHA", sha)
    app = create_app(state)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        blocked = client.post("/setup/fresh?token=test-token")
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "CMP-ACTIVE-JOB"

    scheduled = []

    class Loop:
        def create_task(self, coroutine, *, name):
            scheduled.append(name)
            coroutine.close()
            return SimpleNamespace(done=lambda: False)

    state.oneroster_service = object()
    state.connector = SimpleNamespace(domain="district.example")
    monkeypatch.setattr(
        "gamgui.web.server.asyncio.get_running_loop",
        lambda: Loop(),
    )
    state._schedule_oneroster_gate()
    assert scheduled == []

    committed = state.component_manager.store.load()
    committed.installed_sha = sha
    committed.candidate_sha = ""
    committed.installed_profile = CORE_PROFILE
    committed.installed_components = []
    committed.installed_artifact = state.component_manager.embedded.artifact
    state.component_manager.store.save(committed)
    state._schedule_oneroster_gate()
    assert scheduled == ["oneroster-student-release"]


def test_legacy_installed_updater_can_bootstrap_core_health_once(
    tmp_path,
    monkeypatch,
):
    sha = "a" * 40
    monkeypatch.setenv(APP_DATA_ENV, str(tmp_path))
    monkeypatch.setenv("GAMGUI_BUILD_PROFILE", CORE_PROFILE)
    monkeypatch.setenv("GAMGUI_SOURCE_SHA", sha)
    monkeypatch.setenv("GAMGUI_SKIP_UPDATE_ONCE", "1")
    monkeypatch.setenv("GAMGUI_INSTALLED_SHA", sha)
    monkeypatch.delenv(ACTIVATION_PROBE_ENV, raising=False)
    monkeypatch.delenv(ACTIVATION_TRANSACTION_ENV, raising=False)
    store = UpdateStateStore(tmp_path / "updates" / "state.json")
    marker = store.path.parent / "health" / f"{sha}.ok"
    monkeypatch.setenv("GAMGUI_UPDATE_HEALTH_MARKER", str(marker))
    store.save(
        UpdateState(
            installed_sha="b" * 40,
            candidate_sha=sha,
            pending_app=str(store.path.parent / "pending" / sha / "GamGUI.app"),
            canary_result="passed",
            required_check_evidence=["update-ready"],
        )
    )
    manager = ComponentManager(
        store=store,
        registry=ActivityRegistry(),
        data_root=tmp_path,
    )
    vault = RecordingVault()
    state = AppState(
        vault=vault,
        runner=GAMRunner(vault=vault, base_dir=tmp_path),
        component_manager=manager,
        token="legacy",
    )

    payload = state.update_activation_health_payload()
    assert payload == {
        "ok": True,
        "transaction_id": "",
        "sha": sha,
        "profile": CORE_PROFILE,
        "component_set_digest": manager.embedded.artifact.component_set_digest,
    }
    write_health_marker_from_environment(payload)
    assert marker.read_text(encoding="utf-8") == "ok\n"
    monkeypatch.setenv("GAMGUI_SOURCE_SHA", "c" * 40)
    mismatched_manager = ComponentManager(
        store=store,
        registry=ActivityRegistry(),
        data_root=tmp_path,
    )
    state.component_manager = mismatched_manager
    assert state.update_activation_health_payload() is None
    state.component_manager = manager
    assert state.activation_probe_pending()
    committed = store.load()
    committed.installed_sha = sha
    committed.candidate_sha = ""
    store.save(committed)
    assert state.activation_probe_pending()
    assert manager.reconcile_committed_runtime()
    assert not state.activation_probe_pending()


@pytest.mark.asyncio
async def test_legacy_helper_commit_backfills_running_component_host_before_choice(
    tmp_path,
    monkeypatch,
):
    sha = "a" * 40
    monkeypatch.setenv(APP_DATA_ENV, str(tmp_path))
    monkeypatch.setenv("GAMGUI_BUILD_PROFILE", CORE_PROFILE)
    monkeypatch.setenv("GAMGUI_SOURCE_SHA", sha)
    monkeypatch.setenv("GAMGUI_SKIP_UPDATE_ONCE", "1")
    monkeypatch.setenv("GAMGUI_INSTALLED_SHA", sha)
    monkeypatch.delenv(ACTIVATION_PROBE_ENV, raising=False)
    # A legacy helper supplies the health marker but not the newer transaction
    # environment.  app.main therefore starts this exact process as a sealed
    # activation probe, even though the marker remains the legacy `.ok` form.
    state = AppState.create_activation_probe(token="legacy-running")
    manager = state.component_manager
    assert manager is not None
    store = manager.store
    marker = store.path.parent / "health" / f"{sha}.ok"
    pending = store.path.parent / "pending" / sha / "GamGUI.app"
    monkeypatch.setenv("GAMGUI_UPDATE_HEALTH_MARKER", str(marker))
    store.save(
        UpdateState(
            installed_sha="b" * 40,
            candidate_sha=sha,
            pending_app=str(pending),
            canary_result="passed",
            required_check_evidence=["update-ready"],
        )
    )
    rehydration_vault = RecordingVault(("district.example",))
    monkeypatch.setattr(
        "gamgui.web.server.SecretsVault",
        lambda *_args, **_kwargs: rehydration_vault,
    )

    async def unexpected_retry(_delay):
        raise AssertionError("legacy SHA-only commit was not reconciled")

    monkeypatch.setattr("gamgui.web.server.asyncio.sleep", unexpected_retry)
    payload = state.update_activation_health_payload()
    assert payload is not None
    write_health_marker_from_environment(payload)
    assert marker.read_text(encoding="utf-8") == "ok\n"

    # The pre-profile helper rewrites state using its older schema after it
    # accepts the marker, while this candidate process and manager stay alive.
    store.path.write_text(
        json.dumps({"installed_sha": sha}) + "\n",
        encoding="utf-8",
    )
    assert state.activation_probe_pending()

    await state._wait_for_activation_commit()

    committed = store.load()
    assert not state.activation_probe_mode
    assert not state.activation_probe_pending()
    assert not committed.candidate_sha
    assert not committed.pending_app
    assert committed.installed_profile == CORE_PROFILE
    assert committed.installed_components == []
    assert committed.installed_artifact is not None
    assert committed.installed_artifact.source_sha == sha
    assert (
        committed.installed_artifact.component_set_digest
        == manager.embedded.artifact.component_set_digest
    )
    assert manager.committed_runtime_identity_ready(committed)
    assert not committed.component_prompt_answered
    assert state.vault is rehydration_vault
    assert rehydration_vault.list_calls == 0

    app = create_app(state)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=True,
    ) as client:
        landing = await client.get("/?token=legacy-running")
        assert landing.status_code == 200
        assert "Choose optional features" in landing.text
        skipped = await client.post(
            "/components/oneroster/skip",
            data={"context": "setup"},
        )

    assert skipped.status_code == 200
    assert skipped.headers["HX-Redirect"] == "/setup"
    after_choice = store.load()
    assert after_choice.component_prompt_answered
    assert after_choice.installed_artifact == committed.installed_artifact


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "marker_text", "marker_case"),
    (
        (ONEROSTER_PROFILE, "ok\n", "expected"),
        (CORE_PROFILE, '{"ok":true}\n', "expected"),
        (CORE_PROFILE, "ok\n", "wrong"),
        (CORE_PROFILE, "ok\n", "final-symlink"),
        (CORE_PROFILE, "ok\n", "ancestor-symlink"),
    ),
)
async def test_legacy_commit_reconciliation_rejects_optional_profile_or_bad_marker(
    tmp_path,
    monkeypatch,
    profile,
    marker_text,
    marker_case,
):
    sha = "a" * 40
    monkeypatch.setenv(APP_DATA_ENV, str(tmp_path))
    monkeypatch.setenv("GAMGUI_BUILD_PROFILE", profile)
    monkeypatch.setenv("GAMGUI_SOURCE_SHA", sha)
    monkeypatch.setenv("GAMGUI_SKIP_UPDATE_ONCE", "1")
    monkeypatch.setenv("GAMGUI_INSTALLED_SHA", sha)
    monkeypatch.delenv(ACTIVATION_PROBE_ENV, raising=False)
    monkeypatch.delenv(ACTIVATION_TRANSACTION_ENV, raising=False)
    state = AppState.create_activation_probe(token="legacy-rejected")
    manager = state.component_manager
    assert manager is not None
    store = manager.store
    expected_marker = store.path.parent / "health" / f"{sha}.ok"
    if marker_case == "wrong":
        marker = tmp_path / "forged-health" / f"{sha}.ok"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(marker_text, encoding="utf-8")
    elif marker_case == "final-symlink":
        marker = expected_marker
        marker.parent.mkdir(parents=True, exist_ok=True)
        target = tmp_path / "forged-marker"
        target.write_text(marker_text, encoding="utf-8")
        try:
            marker.symlink_to(target)
        except OSError:
            pytest.skip("symbolic links are unavailable")
    elif marker_case == "ancestor-symlink":
        marker = expected_marker
        target = tmp_path / "forged-health"
        target.mkdir(parents=True, exist_ok=True)
        marker.parent.parent.mkdir(parents=True, exist_ok=True)
        try:
            marker.parent.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("directory symbolic links are unavailable")
        marker.write_text(marker_text, encoding="utf-8")
    else:
        marker = expected_marker
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(marker_text, encoding="utf-8")
    monkeypatch.setenv("GAMGUI_UPDATE_HEALTH_MARKER", str(marker))
    store.path.write_text(
        json.dumps({"installed_sha": sha}) + "\n",
        encoding="utf-8",
    )

    def unexpected_reconciliation():
        raise AssertionError("unsafe legacy state was reconciled")

    async def stop_after_first_poll(_delay):
        raise RuntimeError("legacy state remains gated")

    monkeypatch.setattr(
        manager,
        "reconcile_committed_runtime",
        unexpected_reconciliation,
    )
    monkeypatch.setattr(
        "gamgui.web.server.asyncio.sleep",
        stop_after_first_poll,
    )

    with pytest.raises(RuntimeError, match="legacy state remains gated"):
        await state._wait_for_activation_commit()

    assert state.activation_probe_mode
    assert state.activation_probe_pending()
    assert store.load().installed_artifact is None


@pytest.mark.asyncio
async def test_transaction_probe_sha_only_commit_never_rehydrates(
    tmp_path,
    monkeypatch,
):
    sha = "a" * 40
    transaction = "1" * 32
    monkeypatch.setenv(APP_DATA_ENV, str(tmp_path))
    monkeypatch.setenv("GAMGUI_BUILD_PROFILE", CORE_PROFILE)
    monkeypatch.setenv("GAMGUI_SOURCE_SHA", sha)
    monkeypatch.setenv("GAMGUI_SKIP_UPDATE_ONCE", "1")
    monkeypatch.setenv("GAMGUI_INSTALLED_SHA", sha)
    monkeypatch.setenv(ACTIVATION_PROBE_ENV, "1")
    monkeypatch.setenv(ACTIVATION_TRANSACTION_ENV, transaction)
    state = AppState.create_activation_probe(token="transaction-rejected")
    manager = state.component_manager
    assert manager is not None
    store = manager.store
    monkeypatch.setenv(
        "GAMGUI_UPDATE_HEALTH_MARKER",
        str(store.path.parent / "health" / f"{transaction}.json"),
    )
    store.path.write_text(
        json.dumps({"installed_sha": sha}) + "\n",
        encoding="utf-8",
    )

    async def unexpected_rehydration():
        raise AssertionError("transaction probe rehydrated without artifact identity")

    async def stop_after_first_poll(_delay):
        raise RuntimeError("transaction state remains gated")

    monkeypatch.setattr(
        state,
        "_rehydrate_after_activation",
        unexpected_rehydration,
    )
    monkeypatch.setattr(
        "gamgui.web.server.asyncio.sleep",
        stop_after_first_poll,
    )

    with pytest.raises(RuntimeError, match="transaction state remains gated"):
        await state._wait_for_activation_commit()

    assert state.activation_probe_mode
    assert state.activation_probe_pending()
    assert not manager.committed_runtime_identity_ready(store.load())


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
    assert "CMP-NOT-INSTALLED" in response.text
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
    assert "One safe roster import" in imports.text
    assert "Import Studio" in imports.text
    assert "CMP-INCOMPATIBLE" not in imports.text
