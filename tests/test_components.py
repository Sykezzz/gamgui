from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from gamgui.core.activity import ActivityRegistry
from gamgui.core.components import (
    CORE_PROFILE,
    ONEROSTER_COMPONENT,
    ONEROSTER_PROFILE,
    COMPONENT_ERROR_CODES,
    ComponentArtifactId,
    ComponentError,
    ComponentManager,
    EmbeddedProfile,
    artifact_sidecar_path,
    build_profile_payload,
    component_ids_for_profile,
    component_set_digest,
    load_embedded_profile,
    verify_bundle_artifact,
    write_artifact_sidecar,
)
from gamgui.core.updater import (
    ACTIVATION_CURRENT_APP_ENV,
    ACTIVATION_PROBE_ENV,
    ACTIVATION_TRANSACTION_ENV,
    INSTALLED_SOURCE_EVIDENCE,
    UpdateState,
    UpdateStateStore,
)

SHA = "a" * 40


def _embedded_bundle(tmp_path: Path, profile: str) -> Path:
    bundle = tmp_path / "GamGUI.app"
    metadata = (
        bundle
        / "Contents"
        / "Resources"
        / "resources"
        / "components"
        / "profile.json"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            build_profile_payload(
                profile,
                source_sha=SHA,
                version="1.2.3",
                architecture="arm64",
                minimum_macos_version="12.0",
                packaging_revision="7",
            )
        ),
        encoding="utf-8",
    )
    executable = bundle / "Contents" / "MacOS" / "GamGUI"
    executable.parent.mkdir(parents=True)
    executable.write_text("candidate", encoding="utf-8")
    return bundle


def test_profiles_are_fixed_and_have_distinct_digests():
    assert component_ids_for_profile(CORE_PROFILE) == ()
    assert component_ids_for_profile(ONEROSTER_PROFILE) == (ONEROSTER_COMPONENT,)
    assert component_set_digest(CORE_PROFILE) != component_set_digest(ONEROSTER_PROFILE)
    with pytest.raises(ComponentError) as caught:
        component_ids_for_profile("third-party")
    assert caught.value.error_code == "CMP-INCOMPATIBLE"
    assert {
        "CMP-NOT-INSTALLED",
        "CMP-DISABLED",
        "CMP-AUTH-REQUIRED",
        "CMP-INCOMPATIBLE",
        "CMP-VERIFY-FAILED",
        "CMP-DOWNLOAD-FAILED",
        "CMP-ACTIVE-JOB",
        "CMP-RESTART-REQUIRED",
    } <= COMPONENT_ERROR_CODES


def test_embedded_profile_rejects_manifest_injection(tmp_path):
    payload = build_profile_payload(
        ONEROSTER_PROFILE,
        source_sha=SHA,
        version="1",
        architecture="arm64",
        minimum_macos_version="12.0",
        packaging_revision="1",
    )
    payload["components"][0]["module_root"] = "evil.external.module"
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ComponentError) as caught:
        load_embedded_profile(path)
    assert caught.value.error_code == "CMP-INCOMPATIBLE"


def test_frozen_app_missing_profile_uses_stable_verify_error(monkeypatch, tmp_path):
    monkeypatch.setattr("gamgui.core.components.sys.frozen", True, raising=False)

    with pytest.raises(ComponentError) as caught:
        load_embedded_profile(tmp_path / "missing-profile.json")

    assert caught.value.error_code == "CMP-VERIFY-FAILED"
    assert caught.value.error_code in COMPONENT_ERROR_CODES


def test_artifact_sidecar_binds_profile_and_post_signing_bundle_hash(tmp_path):
    bundle = _embedded_bundle(tmp_path, ONEROSTER_PROFILE)
    sidecar = write_artifact_sidecar(
        bundle,
        signing_channel="local",
        signing_authority="GamGUI Local",
    )

    envelope = verify_bundle_artifact(bundle, expected_profile=ONEROSTER_PROFILE)
    assert envelope.artifact.source_sha == SHA
    assert envelope.artifact.artifact_sha256
    assert sidecar == artifact_sidecar_path(bundle)

    (bundle / "Contents" / "MacOS" / "GamGUI").write_text("tampered", encoding="utf-8")
    with pytest.raises(ComponentError) as caught:
        verify_bundle_artifact(bundle)
    assert caught.value.error_code == "CMP-VERIFY-FAILED"


def test_component_manager_first_run_enable_disable_and_purge(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")
    embedded = EmbeddedProfile.from_json(
        build_profile_payload(
            ONEROSTER_PROFILE,
            source_sha=SHA,
            version="1",
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="1",
        )
    )
    store.save(
        UpdateState(
            installed_sha=SHA,
            installed_profile=ONEROSTER_PROFILE,
            desired_profile=ONEROSTER_PROFILE,
            installed_components=[ONEROSTER_COMPONENT],
            desired_components=[ONEROSTER_COMPONENT],
            enabled_components=[],
        )
    )
    manager = ComponentManager(
        store=store,
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=embedded,
    )
    assert manager.first_run_choice_pending()
    assert manager.status().state == "installed-disabled"
    assert manager.enable().state == "enabled"
    assert not manager.first_run_choice_pending()
    assert manager.disable().state == "installed-disabled"
    assert manager.skip_first_run().state == "installed-disabled"
    assert not manager.first_run_choice_pending()

    old = manager.component_data_root / "snapshots" / ("1" * 32)
    recent = manager.component_data_root / "snapshots" / ("2" * 32)
    old.mkdir(parents=True)
    recent.mkdir(parents=True)
    (old / "source.zip").write_bytes(b"old")
    (recent / "source.zip").write_bytes(b"new")
    now = time.time()
    os.utime(old, (now - 31 * 86400, now - 31 * 86400))
    assert manager.cleanup_expired_snapshots(now=now) == [old]
    assert recent.is_dir()

    with pytest.raises(ComponentError):
        manager.purge_data("oneroster")
    summary = manager.purge_data("OneRoster")
    assert summary["files"] == 1
    assert not manager.component_data_root.exists()


def test_candidate_profile_is_visible_only_during_exact_updater_health_start(
    tmp_path,
    monkeypatch,
):
    store = UpdateStateStore(tmp_path / "data" / "updates" / "state.json")
    embedded = EmbeddedProfile.from_json(
        build_profile_payload(
            ONEROSTER_PROFILE,
            source_sha=SHA,
            version="1",
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="1",
        )
    )
    candidate = embedded.artifact
    transaction = "1" * 32
    pending = (
        store.path.parent
        / "pending"
        / SHA
        / ONEROSTER_PROFILE
        / "GamGUI.app"
    )
    current = tmp_path / "Applications" / "GamGUI.app"
    for bundle in (pending, current):
        executable = bundle / "Contents" / "MacOS" / "GamGUI"
        executable.parent.mkdir(parents=True)
        executable.write_text("binary", encoding="utf-8")
    store.save(
        UpdateState(
            installed_sha=SHA,
            candidate_sha=SHA,
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
    manager = ComponentManager(
        store=store,
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=embedded,
    )

    assert manager.status().profile == CORE_PROFILE
    monkeypatch.setenv("GAMGUI_SKIP_UPDATE_ONCE", "1")
    monkeypatch.setenv(ACTIVATION_PROBE_ENV, "1")
    monkeypatch.setenv(ACTIVATION_TRANSACTION_ENV, transaction)
    monkeypatch.setenv(ACTIVATION_CURRENT_APP_ENV, str(current))
    monkeypatch.setenv("GAMGUI_INSTALLED_SHA", SHA)
    monkeypatch.setenv(
        "GAMGUI_UPDATE_HEALTH_MARKER",
        str(store.path.parent / "health" / f"{transaction}.json"),
    )
    activating = manager.status()
    assert activating.profile == ONEROSTER_PROFILE
    assert activating.enabled

    monkeypatch.setenv("GAMGUI_INSTALLED_SHA", "b" * 40)
    assert manager.status().profile == CORE_PROFILE


def test_runtime_projection_rehashes_the_live_candidate_bundle(tmp_path):
    store = UpdateStateStore(tmp_path / "data" / "updates" / "state.json")
    pending = _embedded_bundle(
        store.path.parent / "pending" / SHA / ONEROSTER_PROFILE,
        ONEROSTER_PROFILE,
    )
    envelope = verify_bundle_artifact(
        pending,
        sidecar=write_artifact_sidecar(
            pending,
            signing_channel="local",
            signing_authority="GamGUI Local",
        ),
    )
    current = tmp_path / "Applications" / "GamGUI.app"
    shutil.copytree(pending, current)
    embedded = EmbeddedProfile.from_json(
        build_profile_payload(
            ONEROSTER_PROFILE,
            source_sha=SHA,
            version="1.2.3",
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="7",
        )
    )
    transaction = "4" * 32
    store.save(
        UpdateState(
            installed_sha=SHA,
            candidate_sha=SHA,
            pending_app=str(pending),
            installed_profile=CORE_PROFILE,
            desired_profile=ONEROSTER_PROFILE,
            desired_components=[ONEROSTER_COMPONENT],
            candidate_artifact=envelope.artifact,
            activation_kind="component-swap",
            required_check_evidence=[INSTALLED_SOURCE_EVIDENCE],
            activation_transaction_id=transaction,
        )
    )
    environment = {
        "GAMGUI_SKIP_UPDATE_ONCE": "1",
        ACTIVATION_PROBE_ENV: "1",
        ACTIVATION_TRANSACTION_ENV: transaction,
        ACTIVATION_CURRENT_APP_ENV: str(current),
        "GAMGUI_INSTALLED_SHA": SHA,
        "GAMGUI_UPDATE_HEALTH_MARKER": str(
            store.path.parent / "health" / f"{transaction}.json"
        ),
    }

    assert store.load_runtime_projection(embedded, environment) is not None
    (current / "Contents" / "MacOS" / "GamGUI").write_text(
        "tampered",
        encoding="utf-8",
    )
    assert store.load_runtime_projection(embedded, environment) is None


def test_core_cleanup_marks_tracked_snapshot_expired(tmp_path):
    manager = ComponentManager(
        store=UpdateStateStore(tmp_path / "updates.json"),
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=EmbeddedProfile.from_json(
            build_profile_payload(
                CORE_PROFILE,
                source_sha=SHA,
                version="1",
                architecture="arm64",
                minimum_macos_version="12.0",
                packaging_revision="1",
            )
        ),
    )
    root = manager.component_data_root
    material = root / "snapshots" / ("1" * 32)
    material.mkdir(parents=True)
    (material / "normalized.db").write_bytes(b"material")
    state_path = root / "state.db"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TABLE imports (
                id TEXT PRIMARY KEY, expires_at REAL NOT NULL,
                state TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO imports VALUES (?, ?, 'ready')",
            ("1" * 32, 99.0),
        )
        connection.execute(
            "INSERT INTO imports VALUES (?, ?, 'ready')",
            ("2" * 32, 200.0),
        )
    unexpired = root / "snapshots" / ("2" * 32)
    unexpired.mkdir()
    (unexpired / "normalized.db").write_bytes(b"future")
    os.utime(unexpired, (1.0, 1.0))

    removed = manager.cleanup_expired_snapshots(now=100.0)

    assert removed == [material]
    assert not material.exists()
    assert unexpired.is_dir()
    with sqlite3.connect(state_path) as connection:
        assert connection.execute(
            "SELECT state FROM imports WHERE id = ?",
            ("1" * 32,),
        ).fetchone()[0] == "expired"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is privileged on Windows")
def test_core_cleanup_fails_closed_for_symlinked_state_database(tmp_path):
    manager = ComponentManager(
        store=UpdateStateStore(tmp_path / "updates.json"),
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=EmbeddedProfile.from_json(
            build_profile_payload(
                CORE_PROFILE,
                source_sha=SHA,
                version="1",
                architecture="arm64",
                minimum_macos_version="12.0",
                packaging_revision="1",
            )
        ),
    )
    snapshot = manager.component_data_root / "snapshots" / ("1" * 32)
    snapshot.mkdir(parents=True)
    os.utime(snapshot, (1.0, 1.0))
    target = tmp_path / "untrusted.db"
    target.write_bytes(b"not a trusted component database")
    (manager.component_data_root / "state.db").symlink_to(target)

    assert manager.cleanup_expired_snapshots(now=31 * 86400 + 2) == []
    assert snapshot.is_dir()


def test_retained_data_summary_counts_scope_and_denial_evidence(tmp_path):
    manager = ComponentManager(
        store=UpdateStateStore(tmp_path / "state.json"),
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=EmbeddedProfile.from_json(
            build_profile_payload(
                CORE_PROFILE,
                source_sha=SHA,
                version="1",
                architecture="arm64",
                minimum_macos_version="12.0",
                packaging_revision="1",
            )
        ),
    )
    manager.component_data_root.mkdir(parents=True)
    database = manager.component_data_root / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE scope_readiness(domain TEXT)")
        connection.execute("CREATE TABLE threshold_denials(domain TEXT)")
        connection.execute(
            "INSERT INTO scope_readiness(domain) VALUES ('example.org')"
        )
        connection.execute(
            "INSERT INTO threshold_denials(domain) VALUES ('example.org')"
        )

    summary = manager.data_summary()

    assert summary["records"] == 2
    assert summary["domains"] == 1


def test_pristine_direct_full_profile_is_registered_but_disabled(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")
    embedded = EmbeddedProfile.from_json(
        build_profile_payload(
            ONEROSTER_PROFILE,
            source_sha=SHA,
            version="1",
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="1",
        )
    )

    manager = ComponentManager(
        store=store,
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=embedded,
    )

    status = manager.status()
    saved = store.load()
    assert status.state == "installed-disabled"
    assert status.profile == ONEROSTER_PROFILE
    assert saved.installed_sha == SHA
    assert saved.installed_components == [ONEROSTER_COMPONENT]
    assert manager.first_run_choice_pending()


def test_legacy_sha_only_state_backfills_running_sealed_artifact(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha=SHA,
            installed_profile=CORE_PROFILE,
            desired_profile=CORE_PROFILE,
            component_prompt_answered=True,
        )
    )
    embedded = EmbeddedProfile.from_json(
        build_profile_payload(
            CORE_PROFILE,
            source_sha=SHA,
            version="1",
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="1",
        )
    )

    ComponentManager(
        store=store,
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=embedded,
    )

    saved = store.load()
    assert saved.installed_artifact == embedded.artifact
    assert saved.installed_profile == CORE_PROFILE
    assert saved.component_prompt_answered


def test_legacy_sha_mismatch_is_not_backfilled(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(UpdateState(installed_sha="b" * 40))
    embedded = EmbeddedProfile.from_json(
        build_profile_payload(
            CORE_PROFILE,
            source_sha=SHA,
            version="1",
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="1",
        )
    )

    ComponentManager(
        store=store,
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=embedded,
    )

    assert store.load().installed_artifact is None


def test_committed_runtime_identity_rejects_stale_packaging_metadata(tmp_path):
    embedded = EmbeddedProfile.from_json(
        build_profile_payload(
            CORE_PROFILE,
            source_sha=SHA,
            version="2",
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="2",
        )
    )
    stale = replace(
        embedded.artifact,
        version="1",
        packaging_revision="1",
    )
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha=SHA,
            installed_profile=CORE_PROFILE,
            installed_artifact=stale,
        )
    )
    manager = ComponentManager(
        store=store,
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=embedded,
    )

    assert not manager.committed_runtime_identity_ready()


def test_pristine_direct_install_records_detected_signing_identity(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "gamgui.core.components._runtime_signing_identity",
        lambda: (
            "developer-id",
            "Developer ID Application: District Admin (ABCDE12345)",
        ),
    )
    store = UpdateStateStore(tmp_path / "state.json")
    embedded = EmbeddedProfile.from_json(
        build_profile_payload(
            CORE_PROFILE,
            source_sha=SHA,
            version="1",
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="1",
        )
    )

    ComponentManager(
        store=store,
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=embedded,
    )

    state = store.load()
    assert state.installed_signing_channel == "developer-id"
    assert state.installed_signing_authority.endswith("(ABCDE12345)")


def test_pristine_frozen_install_records_exact_bundle_hash(
    tmp_path,
    monkeypatch,
):
    bundle = tmp_path / "GamGUI.app"
    bundle.mkdir()
    monkeypatch.setattr("gamgui.core.components.sys.frozen", True, raising=False)
    monkeypatch.setattr(
        "gamgui.core.components._runtime_bundle_path",
        lambda: bundle,
    )
    monkeypatch.setattr(
        "gamgui.core.components.bundle_sha256",
        lambda _bundle: "f" * 64,
    )
    monkeypatch.setattr(
        "gamgui.core.components._runtime_signing_identity",
        lambda: ("local", "GamGUI Local"),
    )
    store = UpdateStateStore(tmp_path / "state.json")
    embedded = EmbeddedProfile.from_json(
        build_profile_payload(
            CORE_PROFILE,
            source_sha=SHA,
            version="1",
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="1",
        )
    )

    ComponentManager(
        store=store,
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=embedded,
    )

    assert store.load().installed_artifact.artifact_sha256 == "f" * 64


def test_component_manager_surfaces_profile_update_candidate(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")
    embedded = EmbeddedProfile.from_json(
        build_profile_payload(
            ONEROSTER_PROFILE,
            source_sha=SHA,
            version="1",
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="1",
        )
    )
    candidate = ComponentArtifactId.from_json(
        {
            **embedded.artifact.to_json(),
            "source_sha": "b" * 40,
            "artifact_sha256": "c" * 64,
        },
        require_hash=True,
    )
    store.save(
        UpdateState(
            installed_sha=SHA,
            candidate_sha="b" * 40,
            installed_profile=ONEROSTER_PROFILE,
            desired_profile=ONEROSTER_PROFILE,
            installed_components=[ONEROSTER_COMPONENT],
            enabled_components=[ONEROSTER_COMPONENT],
            candidate_artifact=candidate,
        )
    )
    manager = ComponentManager(
        store=store,
        embedded=embedded,
        data_root=tmp_path / "data",
    )

    status = manager.status()

    assert status.state == "update-available"
    assert status.restart_required


def test_failed_first_run_install_keeps_choice_available(tmp_path):
    class FailedCoordinator:
        def prepare_profile(self, _profile):
            return None

    store = UpdateStateStore(tmp_path / "state.json")
    embedded = EmbeddedProfile.from_json(
        build_profile_payload(
            CORE_PROFILE,
            source_sha=SHA,
            version="1",
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="1",
        )
    )
    manager = ComponentManager(
        store=store,
        coordinator=FailedCoordinator(),
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=embedded,
    )

    manager.prepare_install()

    assert manager.first_run_choice_pending()
    assert manager.status().error_code == "CMP-DOWNLOAD-FAILED"
    assert store.load().desired_profile == CORE_PROFILE
    assert store.load().desired_components == []


def test_failed_remove_does_not_leave_core_as_future_update_intent(tmp_path):
    class FailedCoordinator:
        def prepare_profile(self, _profile):
            return None

    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha=SHA,
            installed_profile=ONEROSTER_PROFILE,
            desired_profile=ONEROSTER_PROFILE,
            installed_components=[ONEROSTER_COMPONENT],
            desired_components=[ONEROSTER_COMPONENT],
            enabled_components=[ONEROSTER_COMPONENT],
        )
    )
    embedded = EmbeddedProfile.from_json(
        build_profile_payload(
            ONEROSTER_PROFILE,
            source_sha=SHA,
            version="1",
            architecture="arm64",
            minimum_macos_version="12.0",
            packaging_revision="1",
        )
    )
    manager = ComponentManager(
        store=store,
        coordinator=FailedCoordinator(),
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
        embedded=embedded,
    )

    manager.prepare_remove()

    state = store.load()
    assert state.desired_profile == ONEROSTER_PROFILE
    assert state.desired_components == [ONEROSTER_COMPONENT]


def test_degraded_full_profile_preserves_recovery_actions(
    tmp_path,
    monkeypatch,
):
    calls = []

    class RepairCoordinator:
        def prepare_verified_file(
            self,
            source,
            profile,
            *,
            official_team_id_confirmation="",
        ):
            calls.append(
                (Path(source), profile, official_team_id_confirmation)
            )
            return tmp_path / "pending" / "GamGUI.app"

    def fail_embedded():
        raise ComponentError(
            "CMP-VERIFY-FAILED",
            "Embedded component manifest is corrupt.",
        )

    monkeypatch.setattr(
        "gamgui.core.components.load_embedded_profile",
        fail_embedded,
    )
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha=SHA,
            installed_profile=ONEROSTER_PROFILE,
            desired_profile=ONEROSTER_PROFILE,
            installed_components=[ONEROSTER_COMPONENT],
            desired_components=[ONEROSTER_COMPONENT],
            installed_signing_channel="developer-id",
        )
    )
    manager = ComponentManager(
        store=store,
        coordinator=RepairCoordinator(),
        registry=ActivityRegistry(),
        data_root=tmp_path / "data",
    )

    status = manager.status()
    assert status.state == "degraded"
    assert status.profile == ONEROSTER_PROFILE
    assert status.installed_components == (ONEROSTER_COMPONENT,)

    manager.prepare_install(
        tmp_path / "GamGUI-full.zip",
        signing_channel_confirmation="ABCDE12345",
    )
    assert calls == [
        (
            tmp_path / "GamGUI-full.zip",
            ONEROSTER_PROFILE,
            "ABCDE12345",
        )
    ]
