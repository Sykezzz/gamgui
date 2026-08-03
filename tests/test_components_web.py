from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gamgui.core.activity import ActivityRegistry
from gamgui.core.updater import UpdateState, UpdateStateStore
from gamgui.web.routes.components import core_deep_link_router, router

TEMPLATES = Path(__file__).parents[1] / "gamgui" / "web" / "templates"


class FakeComponentManager:
    def __init__(self) -> None:
        self.data = {
            "state": "not_installed",
            "installed": False,
            "enabled": False,
            "first_run_pending": True,
            "profile": "core",
            "download_size": "18 MB",
        }
        self.calls: list[tuple] = []
        self.purged = False

    def status(self):
        self.calls.append(("status",))
        return dict(self.data)

    def first_run_choice_pending(self):
        return bool(self.data["first_run_pending"])

    def skip_first_run(self):
        self.calls.append(("skip",))
        self.data["first_run_pending"] = False
        self.data["skipped"] = True

    def prepare_install(self, source_file=None):
        self.calls.append(("install", source_file))
        self.data.update(
            state="restart_required",
            restart_required=True,
            first_run_pending=False,
        )

    def enable(self):
        self.calls.append(("enable",))
        self.data.update(state="enabled", enabled=True)

    def disable(self):
        self.calls.append(("disable",))
        self.data.update(state="installed_disabled", enabled=False)

    def prepare_remove(self):
        self.calls.append(("remove",))
        self.data.update(state="restart_required", restart_required=True)

    def prepare_update(
        self,
        source_file,
        *,
        signing_channel_confirmation="",
    ):
        self.calls.append(
            ("update", source_file, signing_channel_confirmation)
        )
        self.data.update(state="update_available", restart_required=True)

    def data_summary(self):
        self.calls.append(("data_summary",))
        return {"snapshots": 324, "files": 400, "bytes": 4096}

    def purge_data(self, confirm: str):
        self.calls.append(("purge", confirm))
        assert confirm == "OneRoster"
        self.purged = True


def _client(manager=None, *, active=False):
    state = SimpleNamespace(
        component_manager=manager,
        has_active_admin_jobs=lambda: active,
        activity_registry=ActivityRegistry(),
    )
    app = FastAPI()
    app.state.gamgui = state
    app.include_router(router)
    app.include_router(core_deep_link_router)
    return TestClient(app)


def test_components_page_is_permanent_local_settings_surface():
    manager = FakeComponentManager()
    response = _client(manager).get("/components")

    assert response.status_code == 200
    assert "Settings" in response.text
    assert "OneRoster Classroom" in response.text
    assert "Keep GamGUI current" in response.text
    assert "Check for updates" in response.text
    assert "Install OneRoster" in response.text
    assert "participant identifiers" in response.text
    assert "Purge OneRoster data" in response.text
    assert "never" in response.text.lower()
    assert manager.calls == [("status",)]


def test_core_build_degrades_to_install_explanation():
    response = _client().get("/components")

    assert response.status_code == 200
    assert "Unavailable" in response.text
    assert "CMP-NOT-INSTALLED" in response.text
    assert 'disabled aria-disabled="true"' in response.text


def test_core_profile_can_stage_a_same_profile_application_update():
    manager = FakeComponentManager()
    client = _client(manager)
    page = client.get("/components")
    assert "Update application from a verified release file" in page.text

    response = client.post(
        "/components/oneroster/update",
        data={
            "context": "settings",
            "source_path": "/tmp/GamGUI-core.zip",
            "signing_channel_confirmation": "ABCDE12345",
        },
    )

    assert response.status_code == 200
    assert (
        "update",
        Path("/tmp/GamGUI-core.zip"),
        "ABCDE12345",
    ) in manager.calls
    assert "staged" in response.text


def test_core_update_available_does_not_imply_oneroster_is_installed():
    class CurrentManager(FakeComponentManager):
        def status(self):
            return {
                "state": "update-available",
                "profile": "core",
                "installed_components": (),
                "enabled": False,
                "first_run_pending": False,
            }

    response = _client(CurrentManager()).get("/components")

    assert "Verify Core and prepare removal" not in response.text
    assert "Install from a verified local file" in response.text


def test_core_deep_link_uses_core_owned_install_explanation():
    response = _client(FakeComponentManager()).get("/classroom/imports")

    assert response.status_code == 200
    assert "OneRoster Import Studio is optional" in response.text
    assert "ordinary Classroom" in response.text
    assert "Install OneRoster" in response.text
    assert "_oneroster" not in response.text


def test_first_run_skip_is_not_rendered_again():
    manager = FakeComponentManager()
    client = _client(manager)

    skipped = client.post(
        "/components/oneroster/skip",
        data={"context": "setup"},
    )
    assert skipped.status_code == 200
    assert skipped.headers["HX-Redirect"] == "/setup"
    assert "OneRoster Classroom" not in skipped.text

    later = client.get("/components/status", params={"context": "setup"})
    assert later.status_code == 200
    assert "Install OneRoster" not in later.text
    assert ("skip",) in manager.calls


def test_staged_update_requires_restart_instead_of_first_run_skip():
    manager = FakeComponentManager()
    manager.data.update(
        state="update_available",
        installed=True,
        enabled=True,
        first_run_pending=False,
        restart_required=True,
    )

    response = _client(manager).get("/components/status", params={"context": "setup"})

    assert response.status_code == 200
    assert (
        "Quit GamGUI normally, then reopen it to apply the verified update"
        in response.text
    )
    assert "Continue setup" not in response.text
    assert not any(call[0] == "skip" for call in manager.calls)


def test_component_change_refuses_active_admin_job_server_side():
    manager = FakeComponentManager()
    response = _client(manager, active=True).post(
        "/components/oneroster/install",
        data={"context": "settings"},
    )

    assert response.status_code == 200
    assert "CMP-ACTIVE-JOB" in response.text
    assert "active administrative operation" in response.text
    assert not any(call[0] == "install" for call in manager.calls)


def test_install_stages_restart_and_accepts_verified_file_path():
    manager = FakeComponentManager()
    response = _client(manager).post(
        "/components/oneroster/install",
        data={"context": "settings", "source_path": "/tmp/GamGUI.app"},
    )

    assert response.status_code == 200
    assert "Restart required" in response.text
    install = next(call for call in manager.calls if call[0] == "install")
    assert str(install[1]).endswith("GamGUI.app")


def test_enable_disable_and_remove_are_state_guarded():
    manager = FakeComponentManager()
    manager.data.update(
        state="installed_disabled",
        installed=True,
        enabled=False,
        first_run_pending=False,
    )
    client = _client(manager)

    enabled = client.post(
        "/components/oneroster/enable",
        data={"context": "settings"},
    )
    assert "OneRoster is enabled" in enabled.text
    disabled = client.post(
        "/components/oneroster/disable",
        data={"context": "settings"},
    )
    assert "OneRoster is disabled" in disabled.text
    removed = client.post(
        "/components/oneroster/remove",
        data={"context": "settings"},
    )
    assert "Core profile is being prepared" in removed.text


def test_installed_component_can_stage_verified_core_file_for_removal():
    class OfflineManager(FakeComponentManager):
        def prepare_remove(
            self,
            source_file=None,
            *,
            signing_channel_confirmation="",
        ):
            self.calls.append(
                ("verified-remove", source_file, signing_channel_confirmation)
            )
            self.data.update(state="restart_required", restart_required=True)

    manager = OfflineManager()
    manager.data.update(
        state="enabled",
        installed=True,
        enabled=True,
        first_run_pending=False,
    )

    response = _client(manager).post(
        "/components/oneroster/remove",
        data={
            "context": "settings",
            "source_path": "/tmp/GamGUI-core.zip",
            "signing_channel_confirmation": "ABCDE12345",
        },
    )

    assert response.status_code == 200
    assert (
        "verified-remove",
        Path("/tmp/GamGUI-core.zip"),
        "ABCDE12345",
    ) in manager.calls


def test_purge_requires_exact_confirmation_and_shows_impact():
    manager = FakeComponentManager()
    manager.data.update(state="enabled", installed=True, enabled=True)
    client = _client(manager)

    preview = client.get("/components/oneroster/data/purge")
    assert preview.status_code == 200
    assert "324" in preview.text
    assert "4096" in preview.text

    rejected = client.post(
        "/components/oneroster/data/purge",
        data={"confirmation": "oneroster"},
    )
    assert "Type OneRoster exactly" in rejected.text
    assert not manager.purged

    applied = client.post(
        "/components/oneroster/data/purge",
        data={"confirmation": "OneRoster"},
    )
    assert "permanently removed" in applied.text
    assert manager.purged


def test_component_controls_have_accessible_status_and_focus_contracts():
    response = _client(FakeComponentManager()).get("/components")

    assert 'role="status"' in response.text
    assert "focus-visible:ring-2" in response.text
    assert 'aria-labelledby="oneroster-component-heading"' in response.text
    assert '<form' in response.text and "<button" in response.text


def test_manual_update_check_runs_once_and_reports_no_validated_release(
    tmp_path,
    monkeypatch,
):
    manager = FakeComponentManager()
    manager.store = UpdateStateStore(tmp_path / "updates" / "state.json")
    client = _client(manager)
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Coordinator:
        def check_and_prepare(self):
            calls.append("check")
            started.set()
            assert release.wait(timeout=5)
            state = manager.store.load()
            state.last_checked_at = time.time()
            state.last_error = ""
            state.component_error_code = ""
            manager.store.save(state)

    monkeypatch.setattr(
        "gamgui.web.routes.components._manual_update_supported",
        lambda: True,
    )
    monkeypatch.setattr(
        "gamgui.web.routes.components._build_update_coordinator",
        lambda _request: Coordinator(),
    )

    response = client.post("/components/update/check")
    assert started.wait(timeout=2)
    assert response.status_code == 200
    assert "Checking for updates" in response.text
    assert 'hx-get="/components/update/status"' in response.text

    duplicate = client.post("/components/update/check")
    assert duplicate.status_code == 200
    assert calls == ["check"]

    release.set()
    for _attempt in range(50):
        status = client.get("/components/update/status")
        if "No validated update" in status.text:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("manual update status did not finish")

    assert "No newer release has completed" in status.text
    assert "Last checked" in status.text
    assert "Check for updates" in status.text


def test_manual_update_check_refuses_active_admin_work(tmp_path, monkeypatch):
    manager = FakeComponentManager()
    manager.store = UpdateStateStore(tmp_path / "updates" / "state.json")
    monkeypatch.setattr(
        "gamgui.web.routes.components._manual_update_supported",
        lambda: True,
    )

    response = _client(manager, active=True).post("/components/update/check")

    assert response.status_code == 200
    assert "Check needs attention" in response.text
    assert "CMP-ACTIVE-JOB" in response.text
    assert "safely skipped" in response.text


def test_manual_update_status_never_exposes_private_failure_detail(
    tmp_path,
    monkeypatch,
):
    manager = FakeComponentManager()
    manager.store = UpdateStateStore(tmp_path / "updates" / "state.json")
    manager.store.save(
        UpdateState(
            last_checked_at=time.time(),
            last_error="/Users/admin/private/signing/output",
            component_error_code="CMP-UPDATE-PREPARE-FAILED",
        )
    )
    monkeypatch.setattr(
        "gamgui.web.routes.components._manual_update_supported",
        lambda: True,
    )

    response = _client(manager).get("/components/update/status")

    assert response.status_code == 200
    assert "current version is still running normally" in response.text
    assert "CMP-UPDATE-PREPARE-FAILED" in response.text
    assert "/Users/admin" not in response.text


def test_manual_update_status_reports_a_verified_ready_candidate(
    tmp_path,
    monkeypatch,
):
    manager = FakeComponentManager()
    manager.store = UpdateStateStore(tmp_path / "updates" / "state.json")
    manager.store.save(
        UpdateState(
            candidate_sha="a" * 40,
            pending_app=str(tmp_path / "pending" / "GamGUI.app"),
            last_checked_at=time.time(),
        )
    )
    monkeypatch.setattr(
        "gamgui.web.routes.components._manual_update_supported",
        lambda: True,
    )
    monkeypatch.setattr(
        "gamgui.web.routes.components.activation_evidence_valid",
        lambda _state: True,
    )

    response = _client(manager).get("/components/update/status")

    assert response.status_code == 200
    assert "Update ready" in response.text
    assert "Quit and reopen GamGUI" in response.text
    assert "Check for updates" in response.text


def test_shared_navigation_and_discovery_cards_point_to_components():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    setup = (TEMPLATES / "setup.html").read_text(encoding="utf-8")
    classroom = (TEMPLATES / "classroom.html").read_text(encoding="utf-8")

    assert 'href="/components">Settings</a>' in base
    assert 'hx-get="/components/status?context=setup"' in setup
    assert 'hx-get="/components/status?context=classroom"' in classroom
    assert "OneRoster" not in base  # optional workspace is not a permanent Core nav item
