from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gamgui.core.activity import ActivityRegistry
from gamgui.core.components import (
    CORE_PROFILE,
    ONEROSTER_COMPONENT,
    ONEROSTER_PROFILE,
    ComponentError,
    EmbeddedProfile,
    build_profile_payload,
    write_artifact_sidecar,
    verify_bundle_artifact,
)
from gamgui.core.updater import (
    ACTIVATION_APP_UPDATE,
    ACTIVATION_COMPONENT_SWAP,
    ACTIVATION_VERIFIED_FILE,
    INSTALLED_SOURCE_EVIDENCE,
    VERIFIED_FILE_EVIDENCE,
    LocalUpdateBuilder,
    LocalUpdateInstaller,
    UpdateCandidate,
    UpdateCoordinator,
    UpdateState,
    UpdateStateStore,
    activation_evidence_valid,
    _enabled_components_after_activation,
    prepare_database_schemas,
)

SHA = "b" * 40


def _bundle(
    path: Path,
    profile: str,
    *,
    source_sha: str = SHA,
    version: str = "1",
    architecture: str = "arm64",
    minimum_macos_version: str = "12.0",
    packaging_revision: str = "1",
) -> Path:
    metadata = (
        path
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
                source_sha=source_sha,
                version=version,
                architecture=architecture,
                minimum_macos_version=minimum_macos_version,
                packaging_revision=packaging_revision,
            )
        ),
        encoding="utf-8",
    )
    executable = path / "Contents" / "MacOS" / "GamGUI"
    executable.parent.mkdir(parents=True)
    executable.write_text("candidate", encoding="utf-8")
    write_artifact_sidecar(
        path,
        signing_channel="local",
        signing_authority="GamGUI Local",
    )
    return path


def test_profile_state_round_trip_is_backward_compatible(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"installed_sha": SHA, "candidate_sha": ""}),
        encoding="utf-8",
    )
    state = UpdateStateStore(path).load()
    assert state.installed_profile == CORE_PROFILE
    assert state.desired_profile == CORE_PROFILE
    assert state.installed_components == []
    assert state.profile_blocklists == {}


def test_prepare_profile_stages_same_sha_without_tenant_canary(tmp_path):
    installed = verify_bundle_artifact(
        _bundle(tmp_path / "installed" / "GamGUI.app", CORE_PROFILE)
    ).artifact
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha=SHA,
            installed_profile=CORE_PROFILE,
            installed_artifact=installed,
            desired_profile=ONEROSTER_PROFILE,
            desired_components=[ONEROSTER_COMPONENT],
        )
    )
    calls = []

    class Builder:
        def prepare(self, candidate: UpdateCandidate, profile: str):
            calls.append((candidate.sha, profile))
            return _bundle(tmp_path / "built" / "GamGUI.app", profile)

        def run_canary(self, _pending):
            raise AssertionError("component profile preparation must not access the tenant")

    coordinator = UpdateCoordinator(
        store=store,
        builder=Builder(),
        activity_registry=ActivityRegistry(),
    )
    pending = coordinator.prepare_profile(ONEROSTER_PROFILE)
    state = store.load()
    assert pending is not None
    assert calls == [(SHA, ONEROSTER_PROFILE)]
    assert state.candidate_sha == state.installed_sha == SHA
    assert state.activation_kind == ACTIVATION_COMPONENT_SWAP
    assert state.required_check_evidence == [INSTALLED_SOURCE_EVIDENCE]
    assert state.candidate_artifact.profile == ONEROSTER_PROFILE
    assert activation_evidence_valid(state)


def test_automatic_update_requires_artifact_identity_sidecar(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")

    class Source:
        def discover(self, *_args):
            return UpdateCandidate(SHA, "url", ("update-ready",))

    class Builder:
        def prepare(self, _candidate, profile=CORE_PROFILE):
            executable = tmp_path / "missing-sidecar" / "GamGUI.app" / "Contents" / "MacOS" / "GamGUI"
            executable.parent.mkdir(parents=True)
            executable.write_text("candidate", encoding="utf-8")
            return executable.parents[2]

        def run_canary(self, _pending):
            raise AssertionError("an unidentified artifact must not run the canary")

    coordinator = UpdateCoordinator(
        store=store,
        source=Source(),
        builder=Builder(),
        activity_registry=ActivityRegistry(),
    )

    assert coordinator.check_and_prepare() is None
    state = store.load()
    assert state.candidate_artifact is None
    assert "identity sidecar" in state.last_error


def test_automatic_update_rejects_wrong_profile_before_canary(tmp_path):
    store = UpdateStateStore(tmp_path / "state.json")

    class Source:
        def discover(self, *_args):
            return UpdateCandidate(SHA, "url", ("update-ready",))

    class Builder:
        def prepare(self, _candidate, profile=CORE_PROFILE):
            assert profile == CORE_PROFILE
            return _bundle(
                tmp_path / "wrong-profile" / "GamGUI.app",
                ONEROSTER_PROFILE,
            )

        def run_canary(self, _pending):
            raise AssertionError("a wrong-profile artifact must not run the canary")

    coordinator = UpdateCoordinator(
        store=store,
        source=Source(),
        builder=Builder(),
        activity_registry=ActivityRegistry(),
    )

    assert coordinator.check_and_prepare() is None
    state = store.load()
    assert state.candidate_artifact is None
    assert "wrong component profile" in state.last_error


def test_app_update_activation_requires_exact_artifact_profile_and_sha(tmp_path):
    artifact = verify_bundle_artifact(
        _bundle(tmp_path / "candidate" / "GamGUI.app", CORE_PROFILE)
    ).artifact
    state = UpdateState(
        candidate_sha=SHA,
        desired_profile=CORE_PROFILE,
        candidate_artifact=artifact,
        activation_kind=ACTIVATION_APP_UPDATE,
        canary_result="passed",
        required_check_evidence=["update-ready"],
    )

    assert activation_evidence_valid(state)
    state.candidate_sha = "a" * 40
    assert not activation_evidence_valid(state)
    state.candidate_sha = SHA
    state.desired_profile = ONEROSTER_PROFILE
    assert not activation_evidence_valid(state)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("version", "2"),
        ("architecture", "x86_64"),
        ("minimum_macos_version", "13.0"),
        ("packaging_revision", "2"),
    ),
)
def test_prepare_profile_refuses_unpaired_build_metadata(
    tmp_path,
    field,
    value,
):
    installed = verify_bundle_artifact(
        _bundle(tmp_path / "installed" / "GamGUI.app", CORE_PROFILE)
    ).artifact
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha=SHA,
            installed_profile=CORE_PROFILE,
            installed_artifact=installed,
        )
    )

    class Builder:
        def prepare(self, _candidate: UpdateCandidate, profile: str):
            return _bundle(
                tmp_path / "built" / "GamGUI.app",
                profile,
                **{field: value},
            )

    coordinator = UpdateCoordinator(
        store=store,
        builder=Builder(),
        activity_registry=ActivityRegistry(),
    )

    assert coordinator.prepare_profile(ONEROSTER_PROFILE) is None
    state = store.load()
    assert state.component_error_code == "CMP-INCOMPATIBLE"
    assert field in state.last_error
    assert state.candidate_sha == ""


def test_component_verified_file_refuses_unpaired_platform_metadata(tmp_path):
    installed = verify_bundle_artifact(
        _bundle(tmp_path / "installed" / "GamGUI.app", CORE_PROFILE)
    ).artifact
    pending = _bundle(
        tmp_path / "pending" / SHA / ONEROSTER_PROFILE / "GamGUI.app",
        ONEROSTER_PROFILE,
        minimum_macos_version="13.0",
    )
    envelope = verify_bundle_artifact(pending)
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha=SHA,
            installed_profile=CORE_PROFILE,
            installed_artifact=installed,
        )
    )

    class Builder:
        def prepare_verified_file(self, *_args, **_kwargs):
            return pending, envelope

    coordinator = UpdateCoordinator(
        store=store,
        builder=Builder(),
        activity_registry=ActivityRegistry(),
    )

    assert (
        coordinator.prepare_verified_file(
            tmp_path / "profile.zip",
            ONEROSTER_PROFILE,
        )
        is None
    )
    state = store.load()
    assert state.component_error_code == "CMP-INCOMPATIBLE"
    assert "minimum_macos_version" in state.last_error
    assert state.candidate_sha == ""


@pytest.mark.parametrize("block_kind", ["sha", "artifact"])
def test_component_verified_file_blocklist_rejects_before_self_test(
    tmp_path,
    block_kind,
):
    installed = verify_bundle_artifact(
        _bundle(tmp_path / "installed" / "GamGUI.app", CORE_PROFILE)
    ).artifact
    source = _bundle(
        tmp_path / "source" / "GamGUI.app",
        ONEROSTER_PROFILE,
    )
    candidate = verify_bundle_artifact(source).artifact
    blocked = (
        f"sha:{candidate.source_sha}"
        if block_kind == "sha"
        else candidate.block_key
    )
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha=SHA,
            installed_profile=CORE_PROFILE,
            installed_artifact=installed,
            profile_blocklists={ONEROSTER_PROFILE: [blocked]},
        )
    )
    commands: list[list[str]] = []

    def run(argv, **_kwargs):
        commands.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    coordinator = UpdateCoordinator(
        store=store,
        builder=LocalUpdateBuilder(root=tmp_path / "updates", run=run),
        activity_registry=ActivityRegistry(),
    )

    assert (
        coordinator.prepare_verified_file(source, ONEROSTER_PROFILE)
        is None
    )
    state = store.load()
    assert commands == []
    assert state.candidate_sha == ""
    assert state.component_error_code == "CMP-VERIFY-FAILED"
    assert "blocked" in state.last_error


def test_verified_release_update_preserves_full_profile_and_disabled_state(
    tmp_path,
):
    installed_sha = "a" * 40
    installed_artifact = verify_bundle_artifact(
        _bundle(
            tmp_path / "installed" / "GamGUI.app",
            ONEROSTER_PROFILE,
            source_sha=installed_sha,
            version="0.9",
        )
    ).artifact
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha=installed_sha,
            installed_profile=ONEROSTER_PROFILE,
            desired_profile=ONEROSTER_PROFILE,
            installed_components=[ONEROSTER_COMPONENT],
            desired_components=[ONEROSTER_COMPONENT],
            enabled_components=[],
            installed_artifact=installed_artifact,
            installed_signing_channel="local",
            installed_signing_authority="GamGUI Local",
        )
    )
    pending = _bundle(
        tmp_path / "pending" / SHA / ONEROSTER_PROFILE / "GamGUI.app",
        ONEROSTER_PROFILE,
    )
    envelope = verify_bundle_artifact(pending)
    calls = []

    class Builder:
        def prepare_verified_file(self, source_file, expected_profile, **kwargs):
            calls.append(("prepare", Path(source_file), expected_profile, kwargs))
            return pending, envelope

        def run_canary(self, staged):
            calls.append(("canary", staged))

    coordinator = UpdateCoordinator(
        store=store,
        builder=Builder(),
        activity_registry=ActivityRegistry(),
    )

    staged = coordinator.prepare_verified_update_file(tmp_path / "release.zip")
    state = store.load()

    assert staged == pending
    assert state.candidate_sha == SHA
    assert state.desired_profile == ONEROSTER_PROFILE
    assert state.enabled_components == []
    assert state.canary_result == "passed"
    assert state.activation_kind == ACTIVATION_VERIFIED_FILE
    assert activation_evidence_valid(state)
    assert calls[0][3]["expected_source_sha"] == ""
    assert calls[1] == ("canary", pending)
    assert _enabled_components_after_activation(
        activation_kind=state.activation_kind,
        candidate_sha=SHA,
        previous_sha=installed_sha,
        desired_components=state.desired_components,
        previous_enabled_components=[],
        installed_components=[ONEROSTER_COMPONENT],
    ) == []


@pytest.mark.parametrize(
    ("installed_version", "installed_revision", "candidate_version", "candidate_revision"),
    [
        ("2.0", "1", "1.9", "99"),
        ("2.0", "3", "2.0", "2"),
        ("2.0", "3", "2.0", "3"),
    ],
)
def test_verified_release_update_rejects_downgrade_before_canary(
    tmp_path,
    installed_version,
    installed_revision,
    candidate_version,
    candidate_revision,
):
    installed_sha = "a" * 40
    installed_artifact = verify_bundle_artifact(
        _bundle(
            tmp_path / "installed" / "GamGUI.app",
            ONEROSTER_PROFILE,
            source_sha=installed_sha,
            version=installed_version,
            packaging_revision=installed_revision,
        )
    ).artifact
    pending = _bundle(
        tmp_path / "pending" / "GamGUI.app",
        ONEROSTER_PROFILE,
        version=candidate_version,
        packaging_revision=candidate_revision,
    )
    envelope = verify_bundle_artifact(pending)
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha=installed_sha,
            installed_profile=ONEROSTER_PROFILE,
            desired_profile=ONEROSTER_PROFILE,
            installed_components=[ONEROSTER_COMPONENT],
            desired_components=[ONEROSTER_COMPONENT],
            installed_artifact=installed_artifact,
        )
    )

    class Builder:
        def prepare_verified_file(self, *_args, **_kwargs):
            return pending, envelope

        def run_canary(self, _pending):
            raise AssertionError("a downgrade must not run the canary")

    coordinator = UpdateCoordinator(
        store=store,
        builder=Builder(),
        activity_registry=ActivityRegistry(),
    )

    assert coordinator.prepare_verified_update_file(tmp_path / "release.zip") is None
    state = store.load()
    assert state.candidate_sha == ""
    assert state.component_error_code == "CMP-INCOMPATIBLE"
    assert "older" in state.last_error


@pytest.mark.parametrize(
    "rejection",
    ["already-installed", "downgrade", "equal-release", "blocklisted"],
)
def test_verified_release_policy_rejects_before_candidate_self_test(
    tmp_path,
    rejection,
):
    installed_sha = SHA if rejection == "already-installed" else "a" * 40
    installed_version = "2" if rejection == "downgrade" else "1"
    candidate_version = "1"
    installed = verify_bundle_artifact(
        _bundle(
            tmp_path / "installed" / "GamGUI.app",
            CORE_PROFILE,
            source_sha=installed_sha,
            version=installed_version,
        )
    ).artifact
    source = _bundle(
        tmp_path / "source" / "GamGUI.app",
        CORE_PROFILE,
        source_sha=SHA,
        version=candidate_version,
    )
    envelope = verify_bundle_artifact(source)
    blocklists = (
        {CORE_PROFILE: [envelope.artifact.block_key]}
        if rejection == "blocklisted"
        else {}
    )
    if rejection == "blocklisted":
        installed = verify_bundle_artifact(
            _bundle(
                tmp_path / "installed-blocked" / "GamGUI.app",
                CORE_PROFILE,
                source_sha=installed_sha,
                version="0.9",
            )
        ).artifact
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha=installed_sha,
            installed_profile=CORE_PROFILE,
            installed_artifact=installed,
            profile_blocklists=blocklists,
        )
    )
    commands: list[list[str]] = []

    def run(argv, **_kwargs):
        commands.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    coordinator = UpdateCoordinator(
        store=store,
        builder=LocalUpdateBuilder(root=tmp_path / "updates", run=run),
        activity_registry=ActivityRegistry(),
    )

    assert coordinator.prepare_verified_update_file(source) is None
    assert commands == []
    assert store.load().candidate_sha == ""


def test_verified_release_update_refuses_profile_blocklisted_artifact(tmp_path):
    installed_sha = "a" * 40
    pending = _bundle(
        tmp_path / "pending" / SHA / ONEROSTER_PROFILE / "GamGUI.app",
        ONEROSTER_PROFILE,
    )
    envelope = verify_bundle_artifact(pending)
    installed_artifact = verify_bundle_artifact(
        _bundle(
            tmp_path / "installed" / "GamGUI.app",
            ONEROSTER_PROFILE,
            source_sha=installed_sha,
            version="0.9",
        )
    ).artifact
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha=installed_sha,
            installed_profile=ONEROSTER_PROFILE,
            desired_profile=ONEROSTER_PROFILE,
            installed_components=[ONEROSTER_COMPONENT],
            enabled_components=[ONEROSTER_COMPONENT],
            installed_artifact=installed_artifact,
            profile_blocklists={
                ONEROSTER_PROFILE: [envelope.artifact.block_key]
            },
        )
    )

    class Builder:
        def prepare_verified_file(self, *_args, **_kwargs):
            return pending, envelope

        def run_canary(self, _pending):
            raise AssertionError("a blocked artifact must not run the canary")

    coordinator = UpdateCoordinator(
        store=store,
        builder=Builder(),
        activity_registry=ActivityRegistry(),
    )

    assert coordinator.prepare_verified_update_file(tmp_path / "release.zip") is None
    state = store.load()
    assert state.candidate_sha == ""
    assert state.component_error_code == "CMP-VERIFY-FAILED"
    assert "blocked" in state.last_error


@pytest.mark.parametrize(
    ("installed_profile", "desired_profile", "installed_components"),
    [
        (CORE_PROFILE, ONEROSTER_PROFILE, []),
        (
            ONEROSTER_PROFILE,
            ONEROSTER_PROFILE,
            [ONEROSTER_COMPONENT],
        ),
    ],
)
def test_missing_staged_bundle_recovers_profile_swap_and_app_update_for_retry(
    tmp_path,
    installed_profile,
    desired_profile,
    installed_components,
):
    missing = tmp_path / "pending" / "missing" / "GamGUI.app"
    store = UpdateStateStore(tmp_path / "state.json")
    store.save(
        UpdateState(
            installed_sha="a" * 40,
            candidate_sha=SHA,
            pending_app=str(missing),
            installed_profile=installed_profile,
            desired_profile=desired_profile,
            installed_components=installed_components,
            desired_components=[ONEROSTER_COMPONENT],
            activation_kind=(
                ACTIVATION_COMPONENT_SWAP
                if installed_profile != desired_profile
                else ACTIVATION_VERIFIED_FILE
            ),
            canary_result="passed",
            required_check_evidence=[VERIFIED_FILE_EVIDENCE],
        )
    )
    coordinator = UpdateCoordinator(
        store=store,
        activity_registry=ActivityRegistry(),
    )

    assert coordinator.recover_missing_pending(missing)
    state = store.load()

    assert state.pending_app == ""
    assert state.candidate_sha == ""
    assert state.desired_profile == installed_profile
    assert state.desired_components == installed_components
    assert state.profile_blocklists == {}
    assert state.component_error_code == "CMP-VERIFY-FAILED"
    assert "missing" in state.last_error
    assert not coordinator.recover_missing_pending(missing)


def test_verified_file_seam_rehashes_the_staged_copy(tmp_path):
    source = _bundle(tmp_path / "source" / "GamGUI.app", ONEROSTER_PROFILE)
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    builder = LocalUpdateBuilder(root=tmp_path / "updates", run=run)
    pending, envelope = builder.prepare_verified_file(
        source,
        ONEROSTER_PROFILE,
    )
    assert pending.is_dir()
    assert envelope.artifact.profile == ONEROSTER_PROFILE
    assert commands == [[str(source / "Contents" / "MacOS" / "GamGUI"), "--self-test"]]


def test_verified_file_policy_runs_before_candidate_self_test(tmp_path):
    source = _bundle(tmp_path / "source" / "GamGUI.app", ONEROSTER_PROFILE)
    events: list[object] = []

    def run(argv, **_kwargs):
        events.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    builder = LocalUpdateBuilder(root=tmp_path / "updates", run=run)
    pending, _envelope = builder.prepare_verified_file(
        source,
        ONEROSTER_PROFILE,
        pre_execution_policy=lambda _candidate: events.append("policy"),
    )

    assert pending.is_dir()
    assert events == [
        "policy",
        [str(source / "Contents" / "MacOS" / "GamGUI"), "--self-test"],
    ]


def test_local_update_builder_rejects_rewound_validated_commit(
    tmp_path,
    monkeypatch,
):
    installed_sha = "a" * 40
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        if argv[:2] == ["git", "clone"]:
            Path(argv[-1]).mkdir(parents=True)
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, SHA + "\n", "")
        if "merge-base" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("gamgui.core.updater.sys.platform", "darwin")
    builder = LocalUpdateBuilder(root=tmp_path / "updates", run=run)

    with pytest.raises(RuntimeError, match="not a forward descendant"):
        builder.prepare(
            UpdateCandidate(SHA, "url", ("update-ready",)),
            profile=CORE_PROFILE,
            installed_sha=installed_sha,
        )

    assert any("merge-base" in command for command in commands)
    assert not any(command[:2] == ["make", "setup"] for command in commands)


def test_local_verified_file_pins_installed_leaf_certificate(
    tmp_path,
    monkeypatch,
):
    source = _bundle(tmp_path / "source" / "GamGUI.app", ONEROSTER_PROFILE)
    trusted = _bundle(tmp_path / "installed" / "GamGUI.app", CORE_PROFILE)
    executed_candidate = False

    def run(argv, **kwargs):
        nonlocal executed_candidate
        if "--extract-certificates" in argv:
            certificate = (
                b"untrusted-leaf"
                if Path(argv[-1]) == source
                else b"trusted-leaf"
            )
            Path(kwargs["cwd"], "codesign0.cer").write_bytes(certificate)
        elif argv and Path(argv[0]) == source / "Contents" / "MacOS" / "GamGUI":
            executed_candidate = True
        return subprocess.CompletedProcess(
            argv,
            0,
            "",
            "Authority=GamGUI Local\n",
        )

    monkeypatch.setattr("gamgui.core.updater.sys.platform", "darwin")
    monkeypatch.setattr(
        "gamgui.core.updater.verify_runtime_compatibility",
        lambda _artifact: None,
    )
    builder = LocalUpdateBuilder(
        root=tmp_path / "updates",
        run=run,
        trusted_local_bundle=trusted,
    )

    with pytest.raises(ComponentError, match="installed GamGUI Local certificate"):
        builder.prepare_verified_file(source, ONEROSTER_PROFILE)

    assert not executed_candidate


def test_component_verified_file_refuses_cross_sha_replay_before_execution(
    tmp_path,
    monkeypatch,
):
    source = _bundle(tmp_path / "source" / "GamGUI.app", ONEROSTER_PROFILE)
    commands = []
    monkeypatch.setattr(
        "gamgui.core.updater.verify_runtime_compatibility",
        lambda _artifact: None,
    )
    builder = LocalUpdateBuilder(
        root=tmp_path / "updates",
        run=lambda argv, **_kwargs: (
            commands.append(argv)
            or subprocess.CompletedProcess(argv, 0, "", "")
        ),
    )

    with pytest.raises(ComponentError, match="installed source SHA"):
        builder.prepare_verified_file(
            source,
            ONEROSTER_PROFILE,
            expected_source_sha="a" * 40,
        )

    assert commands == []


def test_interrupted_stage_removes_partial_bundle_and_sidecar(
    tmp_path,
    monkeypatch,
):
    source = _bundle(tmp_path / "source" / "GamGUI.app", ONEROSTER_PROFILE)
    envelope = verify_bundle_artifact(source)
    builder = LocalUpdateBuilder(root=tmp_path / "updates")
    expected = (
        tmp_path
        / "updates"
        / "pending"
        / SHA
        / ONEROSTER_PROFILE
        / "GamGUI.app"
    )

    def interrupted_copy(_source, destination, **_kwargs):
        destination = Path(destination)
        destination.mkdir(parents=True)
        (destination / "partial").write_text("partial", encoding="utf-8")
        raise OSError("copy interrupted")

    monkeypatch.setattr(shutil, "copytree", interrupted_copy)

    with pytest.raises(OSError, match="copy interrupted"):
        builder._stage_bundle(source, envelope)

    assert not expected.exists()
    assert not expected.with_suffix(".app.artifact.json").exists()


def test_schema_preparation_is_profile_aware(tmp_path, monkeypatch):
    monkeypatch.setenv("GAMGUI_BUILD_PROFILE", CORE_PROFILE)
    core_root = tmp_path / "core"
    core_paths = prepare_database_schemas(core_root)
    assert not any("classroom-oneroster" in str(path) for path in core_paths)
    assert not (core_root / "components" / ONEROSTER_COMPONENT).exists()

    monkeypatch.setenv("GAMGUI_BUILD_PROFILE", ONEROSTER_PROFILE)
    full_root = tmp_path / "full"
    full_paths = prepare_database_schemas(full_root)
    component_state = (
        full_root / "components" / ONEROSTER_COMPONENT / "state.db"
    )
    assert component_state in full_paths
    assert component_state.is_file()


def test_verified_file_signing_migration_requires_durable_team_id(tmp_path):
    current = _bundle(tmp_path / "current" / "GamGUI.app", CORE_PROFILE)
    pending = _bundle(
        tmp_path / "updates" / "pending" / SHA / "GamGUI.app",
        ONEROSTER_PROFILE,
    )
    write_artifact_sidecar(
        pending,
        signing_channel="developer-id",
        signing_authority="Developer ID Application: District Admin (ABCDE12345)",
    )
    envelope = verify_bundle_artifact(pending)
    state = UpdateState(
        installed_sha="a" * 40,
        candidate_sha=SHA,
        pending_app=str(pending),
        installed_profile=CORE_PROFILE,
        desired_profile=ONEROSTER_PROFILE,
        desired_components=[ONEROSTER_COMPONENT],
        candidate_artifact=envelope.artifact,
        activation_kind=ACTIVATION_VERIFIED_FILE,
        required_check_evidence=[VERIFIED_FILE_EVIDENCE],
        installed_signing_channel="local",
        installed_signing_authority="GamGUI Local",
        candidate_signing_channel="developer-id",
        candidate_signing_authority=envelope.signing_authority,
        canary_result="passed",
    )
    installer = LocalUpdateInstaller(root=tmp_path / "updates")

    with pytest.raises(ValueError, match="without approval"):
        installer._validate_install_request(state, SHA, pending, current)

    state.candidate_migration_team_id = "ABCDE12345"
    installer._validate_install_request(state, SHA, pending, current)
