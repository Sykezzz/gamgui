"""Fail-closed local updater for the single managed GamGUI Mac."""

from __future__ import annotations

import json
import hashlib
import inspect
import os
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Optional

from .activity import ActivityBusyError, ActivityRegistry, activity_registry
from .components import (
    CORE_PROFILE,
    ONEROSTER_COMPONENT,
    ONEROSTER_PROFILE,
    ArtifactEnvelope,
    ComponentArtifactId,
    ComponentError,
    artifact_sidecar_path,
    component_ids_for_profile,
    load_bundle_embedded_profile,
    normalize_profile,
    verify_bundle_artifact,
    verify_runtime_compatibility,
)
from .paths import APP_DATA_ENV, app_data_dir

UPDATE_REPOSITORY = "Sykezzz/gamgui"
UPDATE_BRANCH = "district-main"
READY_CHECK = "update-ready"
MAX_RETAINED_BACKUPS = 2
BACKUP_MAX_AGE_DAYS = 30
HEALTH_TIMEOUT_SECONDS = 45.0
DISK_SPACE_RESERVE_BYTES = 256 * 1024 * 1024
LOCAL_SIGNING_IDENTITY = "GamGUI Local"
INSTALLED_SOURCE_EVIDENCE = "installed-source"
VERIFIED_FILE_EVIDENCE = "verified-file"
ACTIVATION_APP_UPDATE = "app-update"
ACTIVATION_COMPONENT_SWAP = "component-swap"
ACTIVATION_VERIFIED_FILE = "verified-file"


@dataclass(frozen=True)
class UpdateCandidate:
    sha: str
    html_url: str
    successful_checks: tuple[str, ...] = ()


@dataclass
class UpdateState:
    installed_sha: str = ""
    candidate_sha: str = ""
    pending_app: str = ""
    blocked_shas: list[str] = field(default_factory=list)
    last_checked_at: float = 0.0
    last_error: str = ""
    canary_result: str = ""
    schema_snapshot: str = ""
    required_check_evidence: list[str] = field(default_factory=list)
    retained_rollbacks: list[str] = field(default_factory=list)
    installed_profile: str = CORE_PROFILE
    desired_profile: str = CORE_PROFILE
    installed_components: list[str] = field(default_factory=list)
    desired_components: list[str] = field(default_factory=list)
    enabled_components: list[str] = field(default_factory=list)
    installed_artifact: Optional[ComponentArtifactId] = None
    candidate_artifact: Optional[ComponentArtifactId] = None
    profile_blocklists: dict[str, list[str]] = field(default_factory=dict)
    retained_rollback_artifacts: list[dict[str, object]] = field(default_factory=list)
    activation_kind: str = ""
    installed_signing_channel: str = ""
    candidate_signing_channel: str = ""
    installed_signing_authority: str = ""
    candidate_signing_authority: str = ""
    candidate_migration_team_id: str = ""
    component_prompt_answered: bool = False
    component_operation: str = ""
    component_error_code: str = ""

    @classmethod
    def from_json(cls, value: object) -> "UpdateState":
        if not isinstance(value, dict):
            return cls()

        def _text(key: str, limit: int = 4096) -> str:
            raw = value.get(key, "")
            return raw[:limit] if isinstance(raw, str) else ""

        def _strings(key: str, limit: int = 100) -> list[str]:
            raw = value.get(key, ())
            if not isinstance(raw, list):
                return []
            result: list[str] = []
            for item in raw:
                if isinstance(item, str):
                    result.append(item[:4096])
                    if len(result) >= limit:
                        break
            return result

        installed_sha = _text("installed_sha", 40)
        candidate_sha = _text("candidate_sha", 40)
        installed_sha = installed_sha if _valid_sha(installed_sha) else ""
        candidate_sha = candidate_sha if _valid_sha(candidate_sha) else ""
        blocked_shas = [
            sha for sha in _strings("blocked_shas") if _valid_sha(sha)
        ]
        try:
            last_checked_at = float(value.get("last_checked_at", 0.0))
        except (TypeError, ValueError, OverflowError):
            last_checked_at = 0.0
        if (
            last_checked_at < 0
            or last_checked_at == float("inf")
            or last_checked_at != last_checked_at
        ):
            last_checked_at = 0.0
        canary_result = _text("canary_result", 16)
        if canary_result not in {"", "passed", "failed"}:
            canary_result = ""
        try:
            installed_profile = normalize_profile(
                _text("installed_profile", 64),
                default=CORE_PROFILE,
            )
        except ComponentError:
            installed_profile = CORE_PROFILE
        try:
            desired_profile = normalize_profile(
                _text("desired_profile", 64),
                default=installed_profile,
            )
        except ComponentError:
            desired_profile = installed_profile

        def _component_ids(key: str, profile: str) -> list[str]:
            allowed = set(component_ids_for_profile(profile))
            return [item for item in _strings(key, 10) if item in allowed]

        installed_components = _component_ids(
            "installed_components",
            installed_profile,
        )
        if not installed_components:
            installed_components = list(component_ids_for_profile(installed_profile))
        desired_components = _component_ids("desired_components", desired_profile)
        if not desired_components:
            desired_components = list(component_ids_for_profile(desired_profile))
        enabled_components = [
            item
            for item in _strings("enabled_components", 10)
            if item in installed_components
        ]
        installed_artifact = _artifact_from_state(value.get("installed_artifact"))
        candidate_artifact = _artifact_from_state(value.get("candidate_artifact"))
        profile_blocklists = _profile_blocklists(value.get("profile_blocklists"))
        raw_rollback_artifacts = value.get("retained_rollback_artifacts", [])
        rollback_artifacts = (
            [
                item
                for item in raw_rollback_artifacts
                if isinstance(item, dict)
            ][:MAX_RETAINED_BACKUPS]
            if isinstance(raw_rollback_artifacts, list)
            else []
        )
        activation_kind = _text("activation_kind", 32)
        if activation_kind not in {
            "",
            ACTIVATION_APP_UPDATE,
            ACTIVATION_COMPONENT_SWAP,
            ACTIVATION_VERIFIED_FILE,
        }:
            activation_kind = ""
        installed_signing_channel = _signing_channel(
            _text("installed_signing_channel", 32)
        )
        candidate_signing_channel = _signing_channel(
            _text("candidate_signing_channel", 32)
        )
        migration_team_id = _text(
            "candidate_migration_team_id",
            10,
        ).upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", migration_team_id):
            migration_team_id = ""
        component_operation = _text("component_operation", 32)
        if component_operation not in {"", "preparing"}:
            component_operation = ""
        component_error_code = _text("component_error_code", 64)
        return cls(
            installed_sha=installed_sha,
            candidate_sha=candidate_sha,
            pending_app=_text("pending_app") if candidate_sha else "",
            blocked_shas=blocked_shas,
            last_checked_at=last_checked_at,
            last_error=_text("last_error"),
            canary_result=canary_result,
            schema_snapshot=_text("schema_snapshot"),
            required_check_evidence=_strings("required_check_evidence"),
            retained_rollbacks=_strings(
                "retained_rollbacks",
                MAX_RETAINED_BACKUPS,
            ),
            installed_profile=installed_profile,
            desired_profile=desired_profile,
            installed_components=installed_components,
            desired_components=desired_components,
            enabled_components=enabled_components,
            installed_artifact=installed_artifact,
            candidate_artifact=candidate_artifact if candidate_sha else None,
            profile_blocklists=profile_blocklists,
            retained_rollback_artifacts=rollback_artifacts,
            activation_kind=activation_kind if candidate_sha else "",
            installed_signing_channel=installed_signing_channel,
            candidate_signing_channel=(
                candidate_signing_channel if candidate_sha else ""
            ),
            installed_signing_authority=_text(
                "installed_signing_authority",
                256,
            ),
            candidate_signing_authority=(
                _text("candidate_signing_authority", 256)
                if candidate_sha
                else ""
            ),
            candidate_migration_team_id=(
                migration_team_id if candidate_sha else ""
            ),
            component_prompt_answered=bool(
                value.get("component_prompt_answered", False)
            ),
            component_operation=component_operation,
            component_error_code=component_error_code,
        )


def activation_evidence_valid(state: UpdateState) -> bool:
    """Return whether a staged bundle has both exact-SHA CI and canary evidence."""

    if state.activation_kind == ACTIVATION_COMPONENT_SWAP:
        return (
            bool(state.installed_sha)
            and state.candidate_sha == state.installed_sha
            and INSTALLED_SOURCE_EVIDENCE in state.required_check_evidence
            and state.candidate_artifact is not None
            and state.candidate_artifact.profile == state.desired_profile
            and state.candidate_artifact.source_sha == state.candidate_sha
        )
    if state.activation_kind == ACTIVATION_VERIFIED_FILE:
        return (
            VERIFIED_FILE_EVIDENCE in state.required_check_evidence
            and state.candidate_artifact is not None
            and bool(state.candidate_artifact.artifact_sha256)
            and state.candidate_artifact.profile == state.desired_profile
            and state.candidate_artifact.source_sha == state.candidate_sha
            and (
                state.candidate_sha == state.installed_sha
                or state.canary_result == "passed"
            )
        )
    return (
        state.activation_kind == ACTIVATION_APP_UPDATE
        and state.canary_result == "passed"
        and READY_CHECK in state.required_check_evidence
        and state.candidate_artifact is not None
        and state.candidate_artifact.profile == state.desired_profile
        and state.candidate_artifact.source_sha == state.candidate_sha
    )


def candidate_is_blocked(state: UpdateState) -> bool:
    if not state.candidate_sha:
        return False
    profile = (
        state.candidate_artifact.profile
        if state.candidate_artifact is not None
        else state.desired_profile
    )
    return state.candidate_sha in _blocked_shas_for_profile(state, profile)


class UpdateStateStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or app_data_dir() / "updates" / "state.json"

    def load(self) -> UpdateState:
        try:
            return UpdateState.from_json(json.loads(self.path.read_text(encoding="utf-8")))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return UpdateState()

    def save(self, state: UpdateState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), sort_keys=True, indent=2) + "\n", encoding="utf-8")
        _owner_only(tmp)
        os.replace(tmp, self.path)
        _owner_only(self.path)


class GitHubUpdateSource:
    """Resolve one update-ready commit without trusting a moving branch after validation.

    The default path uses Git's configured credential helper, which works for a
    private fork without copying a GitHub token into application state. CI advances
    ``refs/heads/update-ready`` only after the complete exact-SHA matrix passes; the
    updater accepts it only while it equals ``district-main``. The injectable REST
    seam remains for legacy tests and public-repository integrations.
    """

    def __init__(
        self,
        repository: str = UPDATE_REPOSITORY,
        branch: str = UPDATE_BRANCH,
        ready_check: str = READY_CHECK,
        opener: Optional[Callable[..., object]] = None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.repository = repository
        self.branch = branch
        self.ready_check = ready_check
        self._opener = opener
        self._run = run

    def discover(self, installed_sha: str = "", blocked_shas: Iterable[str] = ()) -> Optional[UpdateCandidate]:
        if self._opener is None:
            return self._discover_validated_ref(installed_sha, blocked_shas)
        branch = self._get(f"repos/{self.repository}/branches/{self.branch}")
        commit = branch.get("commit", {}) if isinstance(branch, dict) else {}
        sha = str(commit.get("sha", ""))
        if not _valid_sha(sha) or sha == installed_sha or sha in set(blocked_shas):
            return None

        checks = self._get(f"repos/{self.repository}/commits/{sha}/check-runs?per_page=100")
        runs = checks.get("check_runs", []) if isinstance(checks, dict) else []
        passing = tuple(
            str(run.get("name", ""))
            for run in runs
            if isinstance(run, dict)
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and run.get("head_sha") == sha
        )
        if self.ready_check not in passing:
            return None
        return UpdateCandidate(
            sha=sha,
            html_url=f"https://github.com/{self.repository}/commit/{sha}",
            successful_checks=passing,
        )

    def _discover_validated_ref(
        self,
        installed_sha: str,
        blocked_shas: Iterable[str],
    ) -> Optional[UpdateCandidate]:
        repository_url = f"https://github.com/{self.repository}.git"
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        result = self._run(
            [
                "git",
                "ls-remote",
                "--heads",
                repository_url,
                f"refs/heads/{self.branch}",
                "refs/heads/update-ready",
            ],
            check=True,
            text=True,
            capture_output=True,
            timeout=15,
            env=environment,
        )
        refs: dict[str, str] = {}
        for line in str(result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and _valid_sha(parts[0]):
                refs[parts[1]] = parts[0].lower()
        sha = refs.get(f"refs/heads/{self.branch}", "")
        if (
            not sha
            or refs.get("refs/heads/update-ready") != sha
            or sha == installed_sha
            or sha in set(blocked_shas)
        ):
            return None
        return UpdateCandidate(
            sha=sha,
            html_url=f"https://github.com/{self.repository}/commit/{sha}",
            successful_checks=(self.ready_check,),
        )

    def _get(self, path: str) -> object:
        url = f"https://api.github.com/{path}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "GamGUI-Updater",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if self._opener is None:
            raise RuntimeError("The REST update source is not configured.")
        with self._opener(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))


class LocalUpdateBuilder:
    """Build and stage an exact commit using the admin Mac's local signing identity."""

    def __init__(
        self,
        root: Optional[Path] = None,
        repository_url: str = "https://github.com/Sykezzz/gamgui.git",
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        trusted_local_bundle: Optional[Path] = None,
    ) -> None:
        self.root = root or app_data_dir() / "updates"
        self.repository_url = repository_url
        self._run = run
        self.trusted_local_bundle = (
            Path(trusted_local_bundle)
            if trusted_local_bundle is not None
            else None
        )

    def prepare(
        self,
        candidate: UpdateCandidate,
        profile: str = CORE_PROFILE,
        installed_sha: str = "",
    ) -> Path:
        if sys.platform != "darwin":
            raise RuntimeError("Automatic application builds are supported only on macOS.")
        profile = normalize_profile(profile)
        checkout = self.root / "source" / candidate.sha
        checkout.parent.mkdir(parents=True, exist_ok=True)
        _require_within(checkout, self.root)
        if checkout.exists():
            _remove_tree(checkout, self.root)
        self._command(
            ["git", "clone", "--filter=blob:none", "--no-checkout", self.repository_url, str(checkout)]
        )
        self._command(["git", "-C", str(checkout), "fetch", "--depth", "1", "origin", candidate.sha])
        self._command(["git", "-C", str(checkout), "checkout", "--detach", candidate.sha])
        head = self._command(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture=True).stdout.strip()
        if head != candidate.sha:
            raise RuntimeError("Updater checkout did not resolve to the validated commit.")
        if (
            _valid_sha(installed_sha)
            and installed_sha != candidate.sha
        ):
            ancestry = self._command(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "merge-base",
                    "--is-ancestor",
                    installed_sha,
                    candidate.sha,
                ],
                capture=True,
                check=False,
            )
            if ancestry.returncode != 0:
                raise RuntimeError(
                    "The validated update is not a forward descendant of the installed commit."
                )

        self._command(["make", "setup"], cwd=checkout)
        self._command(["make", "gam"], cwd=checkout)
        identities = self._command(
            ["security", "find-identity", "-p", "codesigning", "-v"],
            capture=True,
        ).stdout
        if f'"{LOCAL_SIGNING_IDENTITY}"' not in identities:
            raise RuntimeError(
                f'The required local signing identity "{LOCAL_SIGNING_IDENTITY}" is unavailable.'
            )
        build_env = os.environ.copy()
        build_env["CODESIGN_IDENTITY"] = LOCAL_SIGNING_IDENTITY
        self._command(
            ["make", "app", f"PROFILE={profile}"],
            cwd=checkout,
            env=build_env,
        )
        built = checkout / "dist" / "GamGUI.app"
        executable = built / "Contents" / "MacOS" / "GamGUI"
        if not executable.is_file():
            raise RuntimeError("The update build did not produce GamGUI.app.")
        self._command(["codesign", "--verify", "--deep", "--strict", str(built)])
        self._command([str(executable), "--self-test"], cwd=checkout)
        envelope = verify_bundle_artifact(built, expected_profile=profile)
        if envelope.artifact.source_sha != candidate.sha:
            raise RuntimeError(
                "The built application profile did not contain the exact validated commit."
            )
        return self._stage_bundle(built, envelope)

    def prepare_verified_file(
        self,
        source_file: Path,
        expected_profile: str,
        *,
        installed_signing_channel: str = "",
        installed_signing_authority: str = "",
        official_team_id_confirmation: str = "",
        expected_source_sha: str = "",
        pre_execution_policy: Optional[
            Callable[[ArtifactEnvelope], None]
        ] = None,
    ) -> tuple[Path, ArtifactEnvelope]:
        """Validate and stage a signed, immutable local/offline artifact.

        ``pre_execution_policy`` runs after immutable identity, compatibility,
        signature, and notarization checks but before any executable content in
        the candidate is launched.  Update coordinators use this seam to reject
        blocked, replayed, or non-forward artifacts without granting them code
        execution.
        """

        expected_profile = normalize_profile(expected_profile)
        source = Path(source_file).expanduser().resolve()
        if not source.exists():
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "The selected application artifact does not exist.",
            )
        self.root.mkdir(parents=True, exist_ok=True)
        _owner_only_directory(self.root)
        scratch: Optional[tempfile.TemporaryDirectory[str]] = None
        official_record: Optional[object] = None
        try:
            if source.is_dir() and source.suffix == ".app":
                bundle = source
            elif source.is_file() and source.suffix.lower() == ".zip":
                scratch = tempfile.TemporaryDirectory(
                    prefix="verified-artifact-",
                    dir=self.root,
                )
                scratch_root = Path(scratch.name)
                _owner_only_directory(scratch_root)
                _extract_verified_archive(source, scratch_root)
                bundle = scratch_root / "GamGUI.app"
                manifest_path = source.parent / "release-manifest.json"
                checksum_path = source.parent / "release-manifest.json.sha256"
                if manifest_path.exists() or checksum_path.exists():
                    if not manifest_path.is_file() or not checksum_path.is_file():
                        raise ComponentError(
                            "CMP-VERIFY-FAILED",
                            "The official release manifest or its checksum is missing.",
                        )
                    try:
                        from .release_manifest import verify_release_artifact

                        official_record = verify_release_artifact(
                            manifest_path=manifest_path,
                            checksum_path=checksum_path,
                            asset_dir=source.parent,
                            profile=expected_profile,
                            archive_path=source,
                            expected_source_sha=(
                                expected_source_sha
                                if _valid_sha(expected_source_sha)
                                else ""
                            ),
                            expected_team_id=(
                                official_team_id_confirmation.strip().upper()
                                or None
                            ),
                        )
                    except Exception as exc:
                        raise ComponentError(
                            "CMP-VERIFY-FAILED",
                            "The official release manifest did not verify this artifact.",
                        ) from exc
                    sidecar_name = (
                        official_record.get("identity_filename")
                        if isinstance(official_record, dict)
                        else ""
                    )
                    sidecar_source = source.parent / str(sidecar_name or "")
                    if not sidecar_source.is_file():
                        raise ComponentError(
                            "CMP-VERIFY-FAILED",
                            "The verified artifact identity file is missing.",
                        )
                    shutil.copy2(
                        sidecar_source,
                        artifact_sidecar_path(bundle),
                    )
            else:
                raise ComponentError(
                    "CMP-INCOMPATIBLE",
                    "Choose a GamGUI.app bundle or a verified GamGUI ZIP archive.",
                )
            envelope = verify_bundle_artifact(
                bundle,
                expected_profile=expected_profile,
            )
            if (
                _valid_sha(expected_source_sha)
                and envelope.artifact.source_sha != expected_source_sha
            ):
                raise ComponentError(
                    "CMP-INCOMPATIBLE",
                    "The verified component profile does not match the installed source SHA.",
                )
            verify_runtime_compatibility(envelope.artifact)
            if envelope.signing_channel == "developer-id" and official_record is None:
                raise ComponentError(
                    "CMP-VERIFY-FAILED",
                    "Official artifacts require their release manifest, checksum, and identity file.",
                )
            official_team_id = official_team_id_confirmation.strip().upper()
            official_confirmation_valid = bool(
                re.fullmatch(r"[A-Z0-9]{10}", official_team_id)
                and envelope.signing_authority.endswith(
                    f"({official_team_id})"
                )
            )
            if (
                envelope.signing_channel == "developer-id"
                and not official_confirmation_valid
            ):
                raise ComponentError(
                    "CMP-INCOMPATIBLE",
                    "Confirm the official artifact with its trusted 10-character Apple Team ID.",
                )
            if (
                installed_signing_channel
                and envelope.signing_channel != installed_signing_channel
                and not (
                    official_confirmation_valid
                    and envelope.signing_channel == "developer-id"
                )
            ):
                raise ComponentError(
                    "CMP-INCOMPATIBLE",
                    "The artifact uses a different signing channel; an explicit channel migration is required.",
                )
            if (
                installed_signing_authority
                and envelope.signing_authority != installed_signing_authority
                and not (
                    official_confirmation_valid
                    and envelope.signing_channel == "developer-id"
                )
            ):
                raise ComponentError(
                    "CMP-INCOMPATIBLE",
                    "The artifact uses a different signing authority; confirm its trusted Apple Team ID.",
                )
            executable = bundle / "Contents" / "MacOS" / "GamGUI"
            if not executable.is_file():
                raise ComponentError(
                    "CMP-VERIFY-FAILED",
                    "The selected application bundle is incomplete.",
                )
            if sys.platform == "darwin":
                self._command(
                    ["codesign", "--verify", "--deep", "--strict", str(bundle)]
                )
                if envelope.signing_channel == "local":
                    trusted_bundle = (
                        self.trusted_local_bundle or installed_app_path()
                    )
                    if trusted_bundle is None or not trusted_bundle.is_dir():
                        raise ComponentError(
                            "CMP-INCOMPATIBLE",
                            "A local verified file requires the currently installed signed app as its trust anchor.",
                        )
                    candidate_certificate = _codesign_leaf_sha256(
                        bundle,
                        run=self._run,
                        scratch_root=self.root,
                    )
                    trusted_certificate = _codesign_leaf_sha256(
                        trusted_bundle,
                        run=self._run,
                        scratch_root=self.root,
                    )
                    if candidate_certificate != trusted_certificate:
                        raise ComponentError(
                            "CMP-VERIFY-FAILED",
                            "The local artifact was not signed by the installed GamGUI Local certificate.",
                        )
                authorities = self._command(
                    ["codesign", "-dv", "--verbose=4", str(bundle)],
                    capture=True,
                    check=False,
                )
                signature_output = f"{authorities.stdout}\n{authorities.stderr}"
                _validate_signing_authority(
                    envelope,
                    signature_output,
                    official_team_id=official_team_id,
                )
                if envelope.signing_channel == "developer-id":
                    _validate_official_bundle_metadata(bundle, signature_output)
                    self._command(
                        ["xcrun", "stapler", "validate", str(bundle)]
                    )
                    self._command(
                        [
                            "spctl",
                            "--assess",
                            "--type",
                            "execute",
                            "--verbose=4",
                            str(bundle),
                        ]
                    )
            if pre_execution_policy is not None:
                pre_execution_policy(envelope)
            self._command([str(executable), "--self-test"], cwd=bundle.parent)
            pending = self._stage_bundle(bundle, envelope)
            return pending, envelope
        except ComponentError:
            raise
        except (OSError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "The selected application artifact failed offline verification.",
            ) from exc
        finally:
            if scratch is not None:
                scratch.cleanup()

    def _stage_bundle(
        self,
        bundle: Path,
        envelope: ArtifactEnvelope,
    ) -> Path:
        artifact = envelope.artifact
        pending = (
            self.root
            / "pending"
            / artifact.source_sha
            / artifact.profile
            / "GamGUI.app"
        )
        if pending.exists():
            _remove_tree(pending, self.root)
        pending.parent.mkdir(parents=True, exist_ok=True)
        _owner_only_directory(pending.parent)
        _require_available_space(
            pending.parent,
            _tree_bytes(bundle) + DISK_SPACE_RESERVE_BYTES,
        )
        pending_sidecar = artifact_sidecar_path(pending)
        try:
            shutil.copytree(bundle, pending, symlinks=True)
            pending_sidecar.write_text(
                json.dumps(envelope.to_json(), sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            _owner_only(pending_sidecar)
            # Copying is followed by a second hash check so a partial/stale stage can
            # never become activation state.
            verify_bundle_artifact(
                pending,
                expected_profile=artifact.profile,
                expected_artifact=artifact,
            )
        except BaseException:
            if pending.exists():
                _remove_tree(pending, self.root)
            if pending_sidecar.is_file():
                pending_sidecar.unlink()
            raise
        return pending

    def run_canary(self, pending_app: Path) -> dict:
        from .canary import (
            CANARY_DOMAIN_ENV,
            CANARY_SUBJECT_ENV,
            CanaryConfigStore,
            CanaryResultStore,
            validate_canary_result,
        )

        config = CanaryConfigStore().load()
        if config is None:
            raise RuntimeError(
                "The read-only canary subject is not configured. Re-run Workspace setup."
            )
        executable = pending_app / "Contents" / "MacOS" / "GamGUI"
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="canary-",
            dir=self.root,
        ) as scratch:
            canary_env = os.environ.copy()
            canary_env[APP_DATA_ENV] = scratch
            canary_env[CANARY_DOMAIN_ENV] = config.domain
            canary_env[CANARY_SUBJECT_ENV] = config.subject
            result = self._command(
                [str(executable), "--canary", "--json"],
                capture=True,
                env=canary_env,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("The update canary process failed.")
            try:
                payload = json.loads(result.stdout)
            except ValueError as exc:
                raise RuntimeError("The update canary did not return valid JSON.") from exc
        try:
            validated = validate_canary_result(payload, require_success=True)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        CanaryResultStore().save(validated)
        return validated

    def _command(
        self,
        argv: list[str],
        *,
        cwd: Optional[Path] = None,
        capture: bool = False,
        env: Optional[dict[str, str]] = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        return self._run(
            argv,
            cwd=str(cwd) if cwd else None,
            check=check,
            text=True,
            capture_output=capture,
            env=env,
        )


class UpdateCoordinator:
    """Coordinate discovery, staging, canary, blocklisting, and backup retention."""

    def __init__(
        self,
        store: Optional[UpdateStateStore] = None,
        source: Optional[GitHubUpdateSource] = None,
        builder: Optional[LocalUpdateBuilder] = None,
        active_jobs: Callable[[], bool] = lambda: False,
        activity_registry: ActivityRegistry = activity_registry,
    ) -> None:
        self.store = store or UpdateStateStore()
        self.source = source or GitHubUpdateSource()
        self.builder = builder or LocalUpdateBuilder()
        self.active_jobs = active_jobs
        self.activity_registry = activity_registry

    def check_and_prepare(self) -> Optional[Path]:
        state = self.store.load()
        state.last_checked_at = time.time()
        existing = Path(state.pending_app) if state.pending_app else None
        if (
            existing is not None
            and state.candidate_sha
            and state.candidate_sha not in state.blocked_shas
            and existing.is_dir()
        ):
            if activation_evidence_valid(state):
                return existing
            self.block(
                state.candidate_sha,
                "The staged update lacked required CI or canary evidence.",
            )
            return None
        candidate: Optional[UpdateCandidate] = None
        lease = None
        try:
            if state.installed_signing_channel == "developer-id":
                raise RuntimeError(
                    "Official-channel updates must be installed from a verified "
                    "notarized release file; the installed app was not changed."
                )
            if self.active_jobs():
                raise RuntimeError("An administrative operation is active; update preparation was deferred.")
            lease = self.activity_registry.acquire("app-update")
            profile = normalize_profile(
                state.desired_profile,
                default=state.installed_profile or CORE_PROFILE,
            )
            candidate = self.source.discover(
                state.installed_sha,
                _blocked_shas_for_profile(state, profile),
            )
            if candidate is None:
                state.last_error = ""
                self.store.save(state)
                return None
            if READY_CHECK not in candidate.successful_checks:
                raise RuntimeError(
                    "The candidate did not include the required exact-SHA validation check."
                )
            if self.active_jobs():
                raise RuntimeError("An administrative operation became active; update preparation was deferred.")
            pending = _prepare_builder(
                self.builder,
                candidate,
                profile,
                installed_sha=state.installed_sha,
            )
            envelope = _require_staged_envelope(pending, profile)
            if envelope.artifact.source_sha != candidate.sha:
                raise ComponentError(
                    "CMP-VERIFY-FAILED",
                    "The built application did not match the validated candidate SHA.",
                )
            if self.active_jobs():
                raise RuntimeError("An administrative operation became active; the update canary was deferred.")
            self.builder.run_canary(pending)
            if self.active_jobs():
                raise RuntimeError("An administrative operation became active; update activation was deferred.")
            state.candidate_sha = candidate.sha
            state.pending_app = str(pending)
            state.canary_result = "passed"
            state.required_check_evidence = list(candidate.successful_checks)
            state.desired_profile = profile
            state.desired_components = list(component_ids_for_profile(profile))
            state.candidate_artifact = envelope.artifact
            state.candidate_signing_channel = envelope.signing_channel
            state.candidate_signing_authority = envelope.signing_authority
            state.candidate_migration_team_id = ""
            state.activation_kind = ACTIVATION_APP_UPDATE
            state.component_operation = ""
            state.component_error_code = ""
            state.last_error = ""
            self.store.save(state)
            return pending
        except ActivityBusyError as exc:
            state.last_error = str(exc)
            state.component_error_code = exc.error_code
            self.store.save(state)
            return None
        except Exception as exc:
            # Preparation failures can be environmental or transient (network, toolchain,
            # certificate, or read-only canary). Only a failed activation/rollback blocklists a
            # SHA; otherwise the same validated commit may be retried after the environment is
            # repaired.
            state.last_error = str(exc)
            self.store.save(state)
            return None
        finally:
            if lease is not None:
                lease.release()

    def prepare_verified_update_file(
        self,
        source_file: Path,
        *,
        official_team_id_confirmation: str = "",
    ) -> Optional[Path]:
        """Stage a newer verified release while preserving the installed profile.

        Unlike a component profile swap, this path accepts a different source
        SHA, requires the tenant canary, and retains the current enable/disable
        preference through activation.
        """

        state = self.store.load()
        state.last_checked_at = time.time()
        profile = normalize_profile(
            state.installed_profile,
            default=CORE_PROFILE,
        )
        lease = None
        try:
            if not _valid_sha(state.installed_sha):
                raise ComponentError(
                    "CMP-INCOMPATIBLE",
                    "The installed source SHA is unknown; the release update cannot be compared safely.",
                )
            if state.pending_app:
                raise ComponentError(
                    "CMP-RESTART-REQUIRED",
                    "Restart to finish the already staged application change first.",
                )
            if self.active_jobs():
                raise ActivityBusyError("administrative-operation")
            lease = self.activity_registry.acquire("app-update-verified-file")
            policy_applied = False

            def require_update_policy(envelope: ArtifactEnvelope) -> None:
                nonlocal policy_applied
                candidate_sha = envelope.artifact.source_sha
                if candidate_sha == state.installed_sha:
                    raise ComponentError(
                        "CMP-INCOMPATIBLE",
                        "The selected release is already installed.",
                    )
                if state.installed_artifact is None:
                    raise ComponentError(
                        "CMP-INCOMPATIBLE",
                        "The installed artifact identity is unavailable; release order cannot be verified.",
                    )
                _require_forward_artifact_version(
                    state.installed_artifact,
                    envelope.artifact,
                )
                blocked_shas = set(_blocked_shas_for_profile(state, profile))
                blocked_artifacts = set(
                    state.profile_blocklists.get(profile, ())
                )
                if (
                    candidate_sha in blocked_shas
                    or envelope.artifact.block_key in blocked_artifacts
                ):
                    raise ComponentError(
                        "CMP-VERIFY-FAILED",
                        "This profile-specific artifact is blocked after a prior failed activation.",
                    )
                policy_applied = True

            pending, envelope = self.builder.prepare_verified_file(
                Path(source_file),
                profile,
                installed_signing_channel=state.installed_signing_channel,
                installed_signing_authority=state.installed_signing_authority,
                official_team_id_confirmation=official_team_id_confirmation,
                expected_source_sha="",
                pre_execution_policy=require_update_policy,
            )
            # Test doubles and older custom builders may not implement the
            # pre-execution seam.  Reapply it after return for compatibility;
            # LocalUpdateBuilder always invokes it before candidate self-test.
            if not policy_applied:
                require_update_policy(envelope)
            candidate_sha = envelope.artifact.source_sha
            if self.active_jobs():
                raise ActivityBusyError("administrative-operation")
            self.builder.run_canary(pending)
            if self.active_jobs():
                raise ActivityBusyError("administrative-operation")
            state.candidate_sha = candidate_sha
            state.pending_app = str(pending)
            state.candidate_artifact = envelope.artifact
            state.candidate_signing_channel = envelope.signing_channel
            state.candidate_signing_authority = envelope.signing_authority
            state.candidate_migration_team_id = (
                official_team_id_confirmation.strip().upper()
                if envelope.signing_channel == "developer-id"
                else ""
            )
            state.canary_result = "passed"
            state.required_check_evidence = [VERIFIED_FILE_EVIDENCE]
            state.activation_kind = ACTIVATION_VERIFIED_FILE
            state.desired_profile = profile
            state.desired_components = list(component_ids_for_profile(profile))
            state.component_operation = ""
            state.component_error_code = ""
            state.last_error = ""
            self.store.save(state)
            return pending
        except ActivityBusyError as exc:
            state.component_operation = ""
            state.component_error_code = exc.error_code
            state.last_error = str(exc)
            self.store.save(state)
            return None
        except ComponentError as exc:
            state.component_operation = ""
            state.component_error_code = exc.error_code
            state.last_error = str(exc)
            self.store.save(state)
            return None
        except Exception as exc:
            state.component_operation = ""
            state.component_error_code = "CMP-VERIFY-FAILED"
            state.last_error = str(exc)
            self.store.save(state)
            return None
        finally:
            if lease is not None:
                lease.release()

    def prepare_profile(self, profile: str) -> Optional[Path]:
        """Build the other fixed profile from the already-installed exact source SHA.

        This is an offline application-profile operation.  It deliberately does not
        run the live tenant canary or load Workspace credentials.
        """

        profile = normalize_profile(profile)
        state = self.store.load()
        state.last_checked_at = time.time()
        if state.installed_signing_channel == "developer-id":
            state.component_operation = ""
            state.component_error_code = "CMP-DOWNLOAD-FAILED"
            state.last_error = (
                "This official installation requires the matching verified "
                "profile artifact; a locally signed profile was not substituted."
            )
            self.store.save(state)
            return None
        if not _valid_sha(state.installed_sha):
            state.component_operation = ""
            state.component_error_code = "CMP-DOWNLOAD-FAILED"
            state.last_error = (
                "The installed source SHA is unknown; install this component from a verified file."
            )
            self.store.save(state)
            return None
        if state.installed_profile == profile:
            state.desired_profile = profile
            state.desired_components = list(component_ids_for_profile(profile))
            state.component_operation = ""
            state.component_error_code = ""
            state.last_error = ""
            self.store.save(state)
            return None
        lease = None
        try:
            if self.active_jobs():
                raise ActivityBusyError("administrative-operation")
            lease = self.activity_registry.acquire("component-profile-build")
            candidate = UpdateCandidate(
                sha=state.installed_sha,
                html_url="",
                successful_checks=(INSTALLED_SOURCE_EVIDENCE,),
            )
            pending = _prepare_builder(self.builder, candidate, profile)
            envelope = _require_staged_envelope(pending, profile)
            if envelope.artifact.source_sha != state.installed_sha:
                raise ComponentError(
                    "CMP-VERIFY-FAILED",
                    "The built component profile did not match the installed source SHA.",
                )
            _require_paired_profile_identity(
                state.installed_artifact,
                envelope.artifact,
            )
            if (
                state.installed_signing_channel
                and envelope.signing_channel
                and state.installed_signing_channel != envelope.signing_channel
            ):
                raise ComponentError(
                    "CMP-INCOMPATIBLE",
                    "The profile build changed signing channels.",
                )
            if (
                state.installed_signing_authority
                and envelope.signing_authority
                and state.installed_signing_authority != envelope.signing_authority
            ):
                raise ComponentError(
                    "CMP-INCOMPATIBLE",
                    "The profile build changed signing authorities.",
                )
            state.candidate_sha = state.installed_sha
            state.pending_app = str(pending)
            state.candidate_artifact = envelope.artifact
            state.candidate_signing_channel = envelope.signing_channel
            state.candidate_signing_authority = envelope.signing_authority
            state.candidate_migration_team_id = ""
            state.canary_result = ""
            state.required_check_evidence = [INSTALLED_SOURCE_EVIDENCE]
            state.activation_kind = ACTIVATION_COMPONENT_SWAP
            state.desired_profile = profile
            state.desired_components = list(component_ids_for_profile(profile))
            state.component_operation = ""
            state.component_error_code = ""
            state.last_error = ""
            self.store.save(state)
            return pending
        except ActivityBusyError as exc:
            state.component_operation = ""
            state.component_error_code = exc.error_code
            state.last_error = str(exc)
            self.store.save(state)
            return None
        except ComponentError as exc:
            state.component_operation = ""
            state.component_error_code = exc.error_code
            state.last_error = str(exc)
            self.store.save(state)
            return None
        except Exception as exc:
            state.component_operation = ""
            state.component_error_code = "CMP-VERIFY-FAILED"
            state.last_error = str(exc)
            self.store.save(state)
            return None
        finally:
            if lease is not None:
                lease.release()

    def prepare_verified_file(
        self,
        source_file: Path,
        profile: str,
        *,
        official_team_id_confirmation: str = "",
    ) -> Optional[Path]:
        """Stage a local/offline artifact without accessing Google or Workspace secrets."""

        profile = normalize_profile(profile)
        state = self.store.load()
        lease = None
        try:
            if self.active_jobs():
                raise ActivityBusyError("administrative-operation")
            lease = self.activity_registry.acquire("component-verified-file")
            policy_applied = False

            def require_component_policy(envelope: ArtifactEnvelope) -> None:
                nonlocal policy_applied
                if (
                    _valid_sha(state.installed_sha)
                    and envelope.artifact.source_sha != state.installed_sha
                ):
                    raise ComponentError(
                        "CMP-INCOMPATIBLE",
                        "A component profile swap must use the exact installed source SHA. "
                        "Install application-version changes through the update workflow.",
                    )
                _require_paired_profile_identity(
                    state.installed_artifact,
                    envelope.artifact,
                )
                blocked_shas = set(_blocked_shas_for_profile(state, profile))
                blocked_artifacts = set(
                    state.profile_blocklists.get(profile, ())
                )
                if (
                    envelope.artifact.source_sha in blocked_shas
                    or envelope.artifact.block_key in blocked_artifacts
                ):
                    raise ComponentError(
                        "CMP-VERIFY-FAILED",
                        "This profile-specific artifact is blocked after a prior failed activation.",
                    )
                policy_applied = True

            pending, envelope = self.builder.prepare_verified_file(
                Path(source_file),
                profile,
                installed_signing_channel=state.installed_signing_channel,
                installed_signing_authority=state.installed_signing_authority,
                official_team_id_confirmation=official_team_id_confirmation,
                expected_source_sha=state.installed_sha,
                pre_execution_policy=require_component_policy,
            )
            if not policy_applied:
                require_component_policy(envelope)
            state.candidate_sha = envelope.artifact.source_sha
            state.pending_app = str(pending)
            state.candidate_artifact = envelope.artifact
            state.candidate_signing_channel = envelope.signing_channel
            state.candidate_signing_authority = envelope.signing_authority
            state.candidate_migration_team_id = (
                official_team_id_confirmation.strip().upper()
                if envelope.signing_channel == "developer-id"
                else ""
            )
            state.canary_result = ""
            state.required_check_evidence = [VERIFIED_FILE_EVIDENCE]
            state.activation_kind = ACTIVATION_VERIFIED_FILE
            state.desired_profile = profile
            state.desired_components = list(component_ids_for_profile(profile))
            state.component_operation = ""
            state.component_error_code = ""
            state.last_error = ""
            self.store.save(state)
            return pending
        except ActivityBusyError as exc:
            state.component_operation = ""
            state.component_error_code = exc.error_code
            state.last_error = str(exc)
            self.store.save(state)
            return None
        except ComponentError as exc:
            state.component_operation = ""
            state.component_error_code = exc.error_code
            state.last_error = str(exc)
            self.store.save(state)
            return None
        except Exception as exc:
            state.component_operation = ""
            state.component_error_code = "CMP-VERIFY-FAILED"
            state.last_error = str(exc)
            self.store.save(state)
            return None
        finally:
            if lease is not None:
                lease.release()

    def mark_installed(
        self,
        sha: str,
        *,
        profile: Optional[str] = None,
        artifact: Optional[ComponentArtifactId] = None,
        signing_channel: str = "",
        signing_authority: str = "",
    ) -> None:
        state = self.store.load()
        state.installed_sha = sha
        installed_profile = normalize_profile(
            profile or (artifact.profile if artifact else state.desired_profile),
            default=state.installed_profile or CORE_PROFILE,
        )
        state.installed_profile = installed_profile
        state.desired_profile = installed_profile
        state.installed_components = list(
            component_ids_for_profile(installed_profile)
        )
        state.desired_components = list(state.installed_components)
        state.enabled_components = list(state.installed_components)
        state.installed_artifact = artifact
        state.installed_signing_channel = _signing_channel(signing_channel)
        state.installed_signing_authority = str(signing_authority or "")[:256]
        state.candidate_sha = ""
        state.pending_app = ""
        state.candidate_artifact = None
        state.candidate_signing_channel = ""
        state.candidate_signing_authority = ""
        state.candidate_migration_team_id = ""
        state.canary_result = ""
        state.activation_kind = ""
        state.required_check_evidence = []
        state.component_operation = ""
        state.component_error_code = ""
        state.last_error = ""
        self.store.save(state)

    def block(self, sha: str, reason: str) -> None:
        state = self.store.load()
        _block_candidate(state, sha)
        state.candidate_sha = ""
        state.pending_app = ""
        state.candidate_artifact = None
        state.candidate_signing_channel = ""
        state.candidate_signing_authority = ""
        state.candidate_migration_team_id = ""
        state.canary_result = "failed"
        state.activation_kind = ""
        state.required_check_evidence = []
        state.component_operation = ""
        state.component_error_code = "CMP-VERIFY-FAILED"
        state.last_error = reason
        self.store.save(state)

    def recover_missing_pending(self, pending_app: Path) -> bool:
        """Clear a vanished staged bundle without permanently blocking its artifact."""

        state = self.store.load()
        recorded = Path(state.pending_app) if state.pending_app else None
        if (
            recorded is None
            or recorded != Path(pending_app)
            or recorded.is_dir()
        ):
            return False
        state.candidate_sha = ""
        state.pending_app = ""
        state.candidate_artifact = None
        state.candidate_signing_channel = ""
        state.candidate_signing_authority = ""
        state.candidate_migration_team_id = ""
        state.canary_result = ""
        state.activation_kind = ""
        state.required_check_evidence = []
        state.desired_profile = state.installed_profile or CORE_PROFILE
        state.desired_components = list(state.installed_components)
        state.component_operation = ""
        state.component_error_code = "CMP-VERIFY-FAILED"
        state.last_error = (
            "The staged application is missing. Choose or build the matching "
            "verified profile again."
        )
        self.store.save(state)
        return True

    def prune_backups(self, backups_dir: Optional[Path] = None, now: Optional[float] = None) -> list[Path]:
        root = backups_dir or app_data_dir() / "updates" / "backups"
        if not root.is_dir():
            return []
        cutoff = (now or time.time()) - BACKUP_MAX_AGE_DAYS * 86400
        entries = sorted(
            (entry for entry in root.iterdir() if entry.is_dir()),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True,
        )
        removed: list[Path] = []
        for index, entry in enumerate(entries):
            if index >= MAX_RETAINED_BACKUPS or entry.stat().st_mtime <= cutoff:
                _remove_tree(entry, root)
                removed.append(entry)
        return removed


class LocalUpdateInstaller:
    """Activate one staged build and restore both application and databases on failure."""

    def __init__(
        self,
        store: Optional[UpdateStateStore] = None,
        root: Optional[Path] = None,
        data_root: Optional[Path] = None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store or UpdateStateStore()
        self.root = root or app_data_dir() / "updates"
        self.data_root = data_root or app_data_dir()
        self._run = run
        self._popen = popen
        self._sleep = sleep
        self._clock = clock

    def install(
        self,
        sha: str,
        pending_app: Path,
        current_app: Path,
        *,
        health_timeout: float = HEALTH_TIMEOUT_SECONDS,
    ) -> bool:
        state = self.store.load()
        pending_app = Path(pending_app)
        current_app = Path(current_app)
        candidate_artifact = state.candidate_artifact
        candidate_signing_channel = state.candidate_signing_channel
        candidate_signing_authority = state.candidate_signing_authority
        activation_kind = state.activation_kind
        desired_profile = state.desired_profile
        desired_components = list(state.desired_components)
        previous_artifact = state.installed_artifact
        previous_sha = state.installed_sha
        previous_profile = state.installed_profile
        previous_signing_channel = state.installed_signing_channel
        previous_signing_authority = state.installed_signing_authority
        previous_enabled_components = list(state.enabled_components)

        backup = self.root / "backups" / f"{int(self._clock())}-{sha[:12]}"
        database_snapshot = backup / "database"
        migration_copy = backup / "migration-copy"
        backup_app = backup / "GamGUI.app"
        backup_sidecar = backup / "installed-artifact.json"
        installed_sidecar = artifact_sidecar_path(current_app)
        pending_sidecar = artifact_sidecar_path(pending_app)
        marker = self.root / "health" / f"{sha}.ok"
        incoming = current_app.parent / f".{current_app.name}.{sha[:12]}.incoming"
        previous = current_app.parent / f".{current_app.name}.{sha[:12]}.previous"
        process = None
        swapped = False
        previous_moved = False
        snapshot_ready = False

        try:
            database_bytes = sum(
                path.stat().st_size
                for path in _database_files(self.data_root)
                if path.is_file()
            )
            _require_available_space(
                self.root,
                _tree_bytes(current_app)
                + _tree_bytes(pending_app)
                + (database_bytes * 2)
                + DISK_SPACE_RESERVE_BYTES,
            )
        except (ComponentError, OSError) as exc:
            try:
                state.last_error = str(exc)
                state.component_error_code = "CMP-DOWNLOAD-FAILED"
                self.store.save(state)
            except Exception:
                pass
            self._launch_previous(current_app)
            return False

        try:
            self._validate_install_request(state, sha, pending_app, current_app)
            backup.mkdir(parents=True, exist_ok=False)
            _owner_only_directory(backup)
            snapshot_databases(self.data_root, database_snapshot)
            snapshot_ready = True
            if database_snapshot.is_dir():
                shutil.copytree(database_snapshot, migration_copy)
            else:
                migration_copy.mkdir(parents=True)
            _owner_only_directory(migration_copy)

            candidate_executable = pending_app / "Contents" / "MacOS" / "GamGUI"
            migration_env = os.environ.copy()
            migration_env[APP_DATA_ENV] = str(migration_copy)
            self._run(
                [str(candidate_executable), "--self-test"],
                check=True,
                text=True,
                capture_output=True,
                env=migration_env,
            )

            shutil.copytree(current_app, backup_app, symlinks=True)
            if installed_sidecar.is_file():
                shutil.copy2(installed_sidecar, backup_sidecar)
                _owner_only(backup_sidecar)
            if incoming.exists():
                _remove_tree(incoming, current_app.parent)
            if previous.exists():
                _remove_tree(previous, current_app.parent)
            shutil.copytree(pending_app, incoming, symlinks=True)
            os.replace(current_app, previous)
            previous_moved = True
            os.replace(incoming, current_app)
            swapped = True

            marker.parent.mkdir(parents=True, exist_ok=True)
            if marker.exists():
                marker.unlink()
            launch_env = os.environ.copy()
            launch_env["GAMGUI_UPDATE_HEALTH_MARKER"] = str(marker)
            launch_env["GAMGUI_INSTALLED_SHA"] = sha
            launch_env["GAMGUI_SKIP_UPDATE_ONCE"] = "1"
            process = self._popen(
                [str(current_app / "Contents" / "MacOS" / "GamGUI")],
                env=launch_env,
            )
            if not self._wait_for_health(marker, process, health_timeout):
                raise RuntimeError("The updated application did not report startup health in time.")

            if pending_sidecar.is_file():
                sidecar_tmp = installed_sidecar.with_suffix(".tmp")
                shutil.copy2(pending_sidecar, sidecar_tmp)
                os.replace(sidecar_tmp, installed_sidecar)
                _owner_only(installed_sidecar)
            state = self.store.load()
            state.installed_sha = sha
            installed_profile = (
                candidate_artifact.profile
                if candidate_artifact is not None
                else normalize_profile(desired_profile, default=previous_profile)
            )
            state.installed_profile = installed_profile
            state.desired_profile = installed_profile
            state.installed_components = list(
                component_ids_for_profile(installed_profile)
            )
            state.desired_components = [
                item
                for item in desired_components
                if item in state.installed_components
            ]
            state.enabled_components = _enabled_components_after_activation(
                activation_kind=activation_kind,
                candidate_sha=sha,
                previous_sha=previous_sha,
                desired_components=state.desired_components,
                previous_enabled_components=previous_enabled_components,
                installed_components=state.installed_components,
            )
            state.installed_artifact = candidate_artifact
            state.installed_signing_channel = (
                candidate_signing_channel or previous_signing_channel
            )
            state.installed_signing_authority = (
                candidate_signing_authority or previous_signing_authority
            )
            state.candidate_sha = ""
            state.pending_app = ""
            state.candidate_artifact = None
            state.candidate_signing_channel = ""
            state.candidate_signing_authority = ""
            state.candidate_migration_team_id = ""
            state.canary_result = ""
            state.activation_kind = ""
            state.schema_snapshot = str(database_snapshot)
            state.last_error = ""
            state.required_check_evidence = []
            state.component_operation = ""
            state.component_error_code = ""
            state.retained_rollbacks.insert(0, str(backup))
            state.retained_rollbacks = state.retained_rollbacks[:MAX_RETAINED_BACKUPS]
            state.retained_rollback_artifacts.insert(
                0,
                {
                    "path": str(backup),
                    "profile": previous_profile,
                    "artifact": (
                        previous_artifact.to_json()
                        if previous_artifact is not None
                        else None
                    ),
                },
            )
            state.retained_rollback_artifacts = state.retained_rollback_artifacts[
                :MAX_RETAINED_BACKUPS
            ]
            self.store.save(state)
            # State persistence is the activation commit point. Cleanup after this point is
            # best-effort and must never roll back a healthy application.
            swapped = False
            previous_moved = False
            self._best_effort_remove(previous, current_app.parent)
            self._best_effort_remove(pending_app, self.root)
            self._best_effort_unlink(pending_sidecar, self.root)
            try:
                removed = UpdateCoordinator(store=self.store).prune_backups(
                    self.root / "backups"
                )
                if removed:
                    state = self.store.load()
                    removed_values = {str(path) for path in removed}
                    state.retained_rollbacks = [
                        path
                        for path in state.retained_rollbacks
                        if path not in removed_values
                    ]
                    state.retained_rollback_artifacts = [
                        item
                        for item in state.retained_rollback_artifacts
                        if str(item.get("path", "")) not in removed_values
                    ]
                    self.store.save(state)
            except Exception:
                pass
            return True
        except Exception as exc:
            rollback_errors: list[str] = []
            process_stopped = False
            try:
                self._stop(process)
                process_stopped = True
            except Exception as rollback_exc:
                rollback_errors.append(f"process stop failed: {rollback_exc}")
            if process_stopped:
                try:
                    if previous_moved or swapped:
                        if current_app.exists():
                            _remove_tree(current_app, current_app.parent)
                        if previous.exists():
                            os.replace(previous, current_app)
                        elif backup_app.is_dir():
                            shutil.copytree(backup_app, current_app, symlinks=True)
                        if backup_sidecar.is_file():
                            shutil.copy2(backup_sidecar, installed_sidecar)
                        elif installed_sidecar.exists():
                            installed_sidecar.unlink()
                except Exception as rollback_exc:
                    rollback_errors.append(f"application restore failed: {rollback_exc}")
                try:
                    if snapshot_ready:
                        restore_databases(self.data_root, database_snapshot)
                except Exception as rollback_exc:
                    rollback_errors.append(f"database restore failed: {rollback_exc}")
            else:
                rollback_errors.append(
                    "application and database restore deferred because the updated process could not be confirmed stopped"
                )
            reason = str(exc)
            if rollback_errors:
                reason += " Rollback warning: " + "; ".join(rollback_errors)
            try:
                self._block(
                    sha,
                    reason,
                    database_snapshot if snapshot_ready else None,
                )
            except Exception:
                # A state-file failure must not prevent the restored application from
                # relaunching. The staged bundle is still removed below so it cannot loop.
                pass
            self._best_effort_remove(pending_app, self.root)
            self._best_effort_unlink(pending_sidecar, self.root)
            if process_stopped:
                self._launch_previous(current_app)
            return False
        finally:
            self._best_effort_remove(incoming, current_app.parent)

    def _validate_install_request(
        self,
        state: UpdateState,
        sha: str,
        pending_app: Path,
        current_app: Path,
    ) -> None:
        if not _valid_sha(sha):
            raise ValueError("The candidate SHA is invalid.")
        if state.candidate_sha != sha or Path(state.pending_app) != pending_app:
            raise ValueError("The install request does not match the staged update state.")
        if state.candidate_artifact is None:
            raise ValueError("The staged update has no verified artifact identity.")
        profile = state.candidate_artifact.profile
        if profile != normalize_profile(state.desired_profile):
            raise ValueError("The staged artifact profile does not match the requested profile.")
        if state.candidate_artifact.source_sha != sha:
            raise ValueError("The staged artifact source SHA does not match the candidate.")
        if sha in _blocked_shas_for_profile(state, profile):
            raise ValueError("The candidate SHA is blocked.")
        if not activation_evidence_valid(state):
            raise ValueError("The candidate lacks required CI or canary evidence.")
        _require_within(pending_app, self.root)
        if not pending_app.is_dir() or not (pending_app / "Contents" / "MacOS" / "GamGUI").is_file():
            raise ValueError("The staged application bundle is incomplete.")
        envelope = verify_bundle_artifact(
            pending_app,
            expected_profile=profile,
            expected_artifact=state.candidate_artifact,
        )
        if (
            state.candidate_signing_channel
            and envelope.signing_channel != state.candidate_signing_channel
        ):
            raise ValueError("The staged artifact signing channel changed.")
        if (
            state.candidate_signing_authority
            and envelope.signing_authority != state.candidate_signing_authority
        ):
            raise ValueError("The staged artifact signing authority changed.")
        channel_changed = bool(
            state.installed_signing_channel
            and envelope.signing_channel != state.installed_signing_channel
        )
        authority_changed = bool(
            state.installed_signing_authority
            and envelope.signing_authority != state.installed_signing_authority
        )
        migration_team_id = state.candidate_migration_team_id
        migration_approved = bool(
            state.activation_kind == ACTIVATION_VERIFIED_FILE
            and envelope.signing_channel == "developer-id"
            and re.fullmatch(r"[A-Z0-9]{10}", migration_team_id)
            and envelope.signing_authority.endswith(
                f"({migration_team_id})"
            )
        )
        if (channel_changed or authority_changed) and not migration_approved:
            raise ValueError(
                "The staged artifact changed signing identity without approval."
            )
        if sys.platform == "darwin":
            self._run(
                ["codesign", "--verify", "--deep", "--strict", str(pending_app)],
                check=True,
                text=True,
                capture_output=True,
            )
            if envelope.signing_channel == "local":
                if _codesign_leaf_sha256(
                    pending_app,
                    run=self._run,
                    scratch_root=self.root,
                ) != _codesign_leaf_sha256(
                    current_app,
                    run=self._run,
                    scratch_root=self.root,
                ):
                    raise ValueError(
                        "The staged local artifact signing certificate changed."
                    )
            details = self._run(
                ["codesign", "-dv", "--verbose=4", str(pending_app)],
                check=False,
                text=True,
                capture_output=True,
            )
            signature_output = f"{details.stdout}\n{details.stderr}"
            _validate_signing_authority(
                envelope,
                signature_output,
                official_team_id=(
                    migration_team_id
                    if envelope.signing_channel == "developer-id"
                    else ""
                ),
            )
            if envelope.signing_channel == "developer-id":
                _validate_official_bundle_metadata(
                    pending_app,
                    signature_output,
                )
                self._run(
                    ["xcrun", "stapler", "validate", str(pending_app)],
                    check=True,
                    text=True,
                    capture_output=True,
                )
                self._run(
                    [
                        "spctl",
                        "--assess",
                        "--type",
                        "execute",
                        "--verbose=4",
                        str(pending_app),
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                )
        if current_app.suffix != ".app" or not current_app.is_dir():
            raise ValueError("The installed application bundle could not be resolved.")

    def _wait_for_health(self, marker: Path, process: object, timeout: float) -> bool:
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if marker.is_file():
                return marker.read_text(encoding="utf-8").strip() == "ok"
            poll = getattr(process, "poll", None)
            if callable(poll) and poll() is not None:
                return False
            self._sleep(min(0.2, max(0.01, timeout)))
        return False

    def _stop(self, process: object) -> None:
        if process is None:
            return
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            return
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            terminate()
        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                wait(timeout=5)
            except Exception:
                kill = getattr(process, "kill", None)
                if not callable(kill):
                    raise RuntimeError("The updated application could not be stopped.")
                kill()
                try:
                    wait(timeout=5)
                except Exception as exc:
                    raise RuntimeError(
                        "The updated application could not be reaped after being killed."
                    ) from exc
        if not callable(poll) or poll() is None:
            raise RuntimeError("The updated application could not be confirmed stopped.")

    @staticmethod
    def _best_effort_remove(path: Path, root: Path) -> None:
        try:
            if path.exists():
                _remove_tree(path, root)
        except Exception:
            pass

    @staticmethod
    def _best_effort_unlink(path: Path, root: Path) -> None:
        try:
            _require_within(path, root)
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except Exception:
            pass

    def _block(
        self,
        sha: str,
        reason: str,
        database_snapshot: Optional[Path],
    ) -> None:
        state = self.store.load()
        _block_candidate(state, sha)
        state.candidate_sha = ""
        state.pending_app = ""
        state.candidate_artifact = None
        state.candidate_signing_channel = ""
        state.candidate_signing_authority = ""
        state.candidate_migration_team_id = ""
        state.canary_result = "failed"
        state.activation_kind = ""
        state.schema_snapshot = (
            str(database_snapshot)
            if database_snapshot is not None and database_snapshot.is_dir()
            else ""
        )
        state.required_check_evidence = []
        state.component_operation = ""
        state.component_error_code = "CMP-VERIFY-FAILED"
        state.last_error = reason
        self.store.save(state)

    def _launch_previous(self, current_app: Path) -> None:
        executable = current_app / "Contents" / "MacOS" / "GamGUI"
        if not executable.is_file():
            return
        env = os.environ.copy()
        env["GAMGUI_SKIP_UPDATE_ONCE"] = "1"
        try:
            self._popen([str(executable)], env=env)
        except OSError:
            pass


def snapshot_databases(data_root: Path, destination: Path) -> list[Path]:
    """Create consistent SQLite backups while excluding updater state.

    SQLite's backup API folds committed WAL pages into each snapshot and avoids the mixed DB/WAL
    copies that ordinary file copying can produce.
    """
    data_root = Path(data_root)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    _owner_only_directory(destination)
    copied: list[Path] = []
    for source in _database_files(data_root):
        if source.name.lower().endswith(("-wal", "-shm")):
            continue
        relative = source.relative_to(data_root)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(
            sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        ) as source_db, closing(sqlite3.connect(target)) as target_db:
            source_db.backup(target_db)
        _owner_only(target)
        copied.append(target)
    return copied


def restore_databases(data_root: Path, snapshot: Path) -> None:
    """Restore the matching pre-update database set after a failed startup."""
    data_root = Path(data_root)
    snapshot = Path(snapshot)
    for current in list(_database_files(data_root)):
        current.unlink()
    if not snapshot.is_dir():
        return
    for source in _database_files(snapshot, exclude_updates=False):
        relative = source.relative_to(snapshot)
        target = data_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        _owner_only(target)


def prepare_database_schemas(data_root: Path) -> list[Path]:
    """Open every persistent SQLite store so its migrations run on the supplied copy."""
    from .calendar_index import CalendarIndex
    from .classroom.index import CourseIndex
    from .classroom.manifests import RosterManifestStore
    from .directory_index import DirectoryIndex
    from .drive.operations import DriveOperationStore

    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    paths = [
        root / "directory_index.db",
        root / "calendar_index.db",
        root / "classroom_courses.db",
        root / "classroom_roster_operations.db",
        root / "drive_operations.db",
    ]
    DirectoryIndex(paths[0], "__migration_check__")
    CalendarIndex(paths[1])
    CourseIndex(paths[2])
    RosterManifestStore(paths[3])
    DriveOperationStore(paths[4])
    try:
        from .components import load_embedded_profile

        embedded = load_embedded_profile()
    except ComponentError:
        embedded = None
    if embedded is not None and embedded.artifact.profile == ONEROSTER_PROFILE:
        from gamgui.components.oneroster import OneRosterStore

        component_root = root / "components" / ONEROSTER_COMPONENT
        component_store = OneRosterStore("__migration_check__", component_root)
        paths.append(component_store.state_path)
    return paths


def bundle_self_test(data_root: Optional[Path] = None, *, require_gam: bool = True) -> dict[str, object]:
    """Offline bundle and copied-database integrity check without tenant access."""
    from ..web import server
    from .gam.runner import locate_gam_binary

    failures: list[str] = []
    try:
        from .components import load_embedded_profile

        profile = load_embedded_profile()
        if (
            profile.artifact.profile == ONEROSTER_PROFILE
            and ONEROSTER_COMPONENT
            not in {item.component_id for item in profile.components}
        ):
            failures.append("OneRoster profile is missing its allowlisted component")
    except ComponentError as exc:
        failures.append(f"component profile invalid: {exc.error_code}")
        profile = None
    for relative in ("templates/base.html", "templates/index.html", "static"):
        if not (server._WEB_DIR / relative).exists():
            failures.append(f"missing web asset: {relative}")
    if profile is not None and profile.artifact.profile == ONEROSTER_PROFILE:
        try:
            import gamgui.components.oneroster as oneroster_component
            from .components import ComponentManifest

            component = profile.components[0]
            manifest_path = Path(oneroster_component.__file__).with_name(
                "component.json"
            )
            component_payload = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            parsed_manifest = ComponentManifest.from_json(component_payload)
            if (
                component_payload != parsed_manifest.to_json()
                or parsed_manifest != component
            ):
                failures.append(
                    "OneRoster component.json does not match the embedded allowlist"
                )
            for pattern in component.assets:
                matches = tuple(server._WEB_DIR.glob(pattern))
                if not matches or any(not item.is_file() for item in matches):
                    failures.append(f"missing OneRoster asset pattern: {pattern}")
        except Exception:
            failures.append("OneRoster executable module could not be loaded")
    elif profile is not None and getattr(sys, "frozen", False):
        optional_assets = tuple(
            path
            for root in (
                server._WEB_DIR / "templates",
                server._WEB_DIR / "static",
            )
            if root.is_dir()
            for path in root.rglob("*")
            if "oneroster" in path.name.casefold()
        )
        if optional_assets:
            failures.append("Core profile contains OneRoster assets")
        try:
            import gamgui.components.oneroster  # noqa: F401
        except ImportError:
            pass
        else:
            failures.append("Core profile contains OneRoster executable modules")
        try:
            import gamgui.web.routes.oneroster  # noqa: F401
        except ImportError:
            pass
        else:
            failures.append("Core profile contains the OneRoster route module")
    if require_gam:
        from .gam.commands import EXPECTED_GAM_VERSION

        gam = locate_gam_binary()
        if not gam.is_file():
            failures.append("missing bundled GAM executable")
        elif os.name != "nt" and not os.access(gam, os.X_OK):
            failures.append("bundled GAM is not executable")
        else:
            try:
                result = subprocess.run(
                    [str(gam), "version"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if EXPECTED_GAM_VERSION not in result.stdout:
                    failures.append("bundled GAM version does not match the application pin")
            except (OSError, subprocess.SubprocessError):
                failures.append("bundled GAM version command failed")

    root = Path(data_root) if data_root is not None else app_data_dir()
    for database in _database_files(root):
        if database.name.endswith(("-wal", "-shm")):
            continue
        try:
            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                failures.append(f"database integrity failed: {database.name}")
        except sqlite3.DatabaseError:
            failures.append(f"database unreadable: {database.name}")
    return {"ok": not failures, "failures": failures}


def wait_for_process_exit(pid: int, timeout: float = 60.0) -> bool:
    """Wait for the launcher process to exit before replacing its application bundle."""
    if pid <= 0:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.1)
    return False


def installed_app_path(executable: Optional[Path] = None) -> Optional[Path]:
    path = Path(executable) if executable is not None else Path(sys.executable).resolve()
    for parent in path.parents:
        if parent.suffix == ".app":
            return parent
    return None


def write_health_marker_from_environment() -> None:
    marker = os.environ.get("GAMGUI_UPDATE_HEALTH_MARKER", "")
    if not marker:
        return
    path = Path(marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok\n", encoding="utf-8")
    _owner_only(path)


def _artifact_from_state(value: object) -> Optional[ComponentArtifactId]:
    if value in (None, {}):
        return None
    try:
        return ComponentArtifactId.from_json(
            value,
            require_hash=False,
            require_source_sha=True,
        )
    except ComponentError:
        return None


def _profile_blocklists(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for raw_profile, raw_values in value.items():
        try:
            profile = normalize_profile(raw_profile)
        except ComponentError:
            continue
        if not isinstance(raw_values, list):
            continue
        values = [
            item[:256]
            for item in raw_values
            if isinstance(item, str) and 0 < len(item) <= 256
        ][:100]
        if values:
            result[profile] = values
    return result


def _signing_channel(value: str) -> str:
    return value if value in {"", "local", "developer-id"} else ""


def _blocked_shas_for_profile(state: UpdateState, profile: str) -> list[str]:
    result = set(state.blocked_shas)
    for item in state.profile_blocklists.get(normalize_profile(profile), ()):
        if item.startswith("sha:") and _valid_sha(item[4:]):
            result.add(item[4:])
    return sorted(result)


def _require_paired_profile_identity(
    installed: Optional[ComponentArtifactId],
    candidate: ComponentArtifactId,
) -> None:
    """Require immutable build metadata to match for a same-version profile swap."""

    if installed is None:
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "The installed artifact identity is unavailable. Restart the sealed "
            "application once to establish it before changing profiles.",
        )
    paired_fields = (
        "source_sha",
        "version",
        "architecture",
        "minimum_macos_version",
        "packaging_revision",
    )
    mismatches = [
        field
        for field in paired_fields
        if getattr(installed, field) != getattr(candidate, field)
    ]
    if mismatches:
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "The selected profile is not the matching paired artifact for this "
            f"installation ({', '.join(mismatches)} mismatch).",
        )


def _require_forward_artifact_version(
    installed: ComponentArtifactId,
    candidate: ComponentArtifactId,
) -> None:
    """Reject a verified-file release that orders before the installed artifact."""

    installed_key = (
        _numeric_release_key(installed.version),
        _numeric_release_key(installed.packaging_revision),
    )
    candidate_key = (
        _numeric_release_key(candidate.version),
        _numeric_release_key(candidate.packaging_revision),
    )
    if any(not part for part in (*installed_key, *candidate_key)):
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "The installed or candidate release version is not comparable.",
        )
    if candidate_key <= installed_key:
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "The selected verified release is older than or equal to the installed application.",
        )


def _numeric_release_key(value: str) -> tuple[int, ...]:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)*", text):
        return ()
    parts = [int(part) for part in text.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _enabled_components_after_activation(
    *,
    activation_kind: str,
    candidate_sha: str,
    previous_sha: str,
    desired_components: Iterable[str],
    previous_enabled_components: Iterable[str],
    installed_components: Iterable[str],
) -> list[str]:
    """Apply desired state only for same-version profile swaps.

    App-version updates preserve an installed full profile's enabled/disabled
    preference instead of silently turning OneRoster back on.
    """

    installed = set(installed_components)
    if (
        activation_kind == ACTIVATION_COMPONENT_SWAP
        or (
            activation_kind == ACTIVATION_VERIFIED_FILE
            and candidate_sha == previous_sha
        )
    ):
        source = desired_components
    else:
        source = previous_enabled_components
    return sorted({item for item in source if item in installed})


def _block_candidate(state: UpdateState, sha: str) -> None:
    if state.candidate_artifact is None:
        if sha and sha not in state.blocked_shas:
            state.blocked_shas.append(sha)
        return
    profile = state.candidate_artifact.profile
    blocked = state.profile_blocklists.setdefault(profile, [])
    sha_key = f"sha:{sha}"
    if sha_key not in blocked:
        blocked.append(sha_key)
    if state.candidate_artifact.block_key not in blocked:
        blocked.append(state.candidate_artifact.block_key)


def _prepare_builder(
    builder: object,
    candidate: UpdateCandidate,
    profile: str,
    *,
    installed_sha: str = "",
) -> Path:
    method = getattr(builder, "prepare")
    parameters = inspect.signature(method).parameters
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, str] = {}
    if "profile" in parameters or accepts_kwargs:
        kwargs["profile"] = profile
    if "installed_sha" in parameters or accepts_kwargs:
        kwargs["installed_sha"] = installed_sha
    return Path(method(candidate, **kwargs))


def _load_staged_envelope(pending: Path) -> Optional[ArtifactEnvelope]:
    if not artifact_sidecar_path(pending).is_file():
        return None
    return verify_bundle_artifact(pending)


def _require_staged_envelope(pending: Path, profile: str) -> ArtifactEnvelope:
    envelope = _load_staged_envelope(pending)
    if envelope is None:
        raise ComponentError(
            "CMP-VERIFY-FAILED",
            "The built profile did not produce an artifact identity sidecar.",
        )
    if envelope.artifact.profile != normalize_profile(profile):
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "The built application contains the wrong component profile.",
        )
    return envelope


def _extract_verified_archive(source: Path, destination: Path) -> None:
    max_entries = 50_000
    max_expanded_bytes = 4 * 1024 * 1024 * 1024
    expanded = 0
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if not entries or len(entries) > max_entries:
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "The application archive has an invalid entry count.",
            )
        seen_paths: set[PurePosixPath] = set()
        symlink_paths: set[PurePosixPath] = set()
        for info in entries:
            name = info.filename
            pure = PurePosixPath(name.replace("\\", "/"))
            if (
                not name
                or name.startswith(("/", "\\"))
                or "\\" in name
                or any(part in {"", ".", ".."} for part in pure.parts)
                or pure.parts[0] not in {
                    "GamGUI.app",
                    "GamGUI.app.artifact.json",
                }
                or (
                    pure.parts[0] == "GamGUI.app.artifact.json"
                    and len(pure.parts) != 1
                )
            ):
                raise ComponentError(
                    "CMP-VERIFY-FAILED",
                    "The application archive contains an unsafe path.",
                )
            if pure in seen_paths:
                raise ComponentError(
                    "CMP-VERIFY-FAILED",
                    "The application archive contains a duplicate path.",
                )
            seen_paths.add(pure)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                symlink_paths.add(pure)
                if info.file_size > 4096:
                    raise ComponentError(
                        "CMP-VERIFY-FAILED",
                        "The application archive contains an invalid symbolic link.",
                    )
                try:
                    link_target = archive.read(info).decode("utf-8")
                except (UnicodeError, OSError) as exc:
                    raise ComponentError(
                        "CMP-VERIFY-FAILED",
                        "The application archive contains an invalid symbolic link.",
                    ) from exc
                link_path = destination / pure
                resolved_target = (link_path.parent / link_target).resolve()
                bundle_root = (destination / "GamGUI.app").resolve()
                try:
                    resolved_target.relative_to(bundle_root)
                except ValueError as exc:
                    raise ComponentError(
                        "CMP-VERIFY-FAILED",
                        "The application archive contains an escaping symbolic link.",
                    ) from exc
            expanded += info.file_size
            if expanded > max_expanded_bytes:
                raise ComponentError(
                    "CMP-VERIFY-FAILED",
                    "The application archive is too large.",
                )
        for pure in seen_paths:
            if any(parent in symlink_paths for parent in pure.parents):
                raise ComponentError(
                    "CMP-VERIFY-FAILED",
                    "The application archive nests content beneath a symbolic link.",
                )
        _require_available_space(
            destination,
            expanded + DISK_SPACE_RESERVE_BYTES,
        )
        regular_entries = [
            info
            for info in entries
            if ((info.external_attr >> 16) & 0o170000) != stat.S_IFLNK
        ]
        link_entries = [
            info
            for info in entries
            if ((info.external_attr >> 16) & 0o170000) == stat.S_IFLNK
        ]
        for info in (*regular_entries, *link_entries):
            relative = Path(info.filename)
            target = destination / relative
            _require_within(target, destination)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                if target.exists() or target.is_symlink():
                    raise ComponentError(
                        "CMP-VERIFY-FAILED",
                        "The application archive contains a conflicting symbolic link.",
                    )
                os.symlink(archive.read(info).decode("utf-8"), target)
                continue
            with archive.open(info) as source_handle, target.open("wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            permissions = (info.external_attr >> 16) & 0o777
            if permissions and os.name != "nt":
                target.chmod(permissions)


def _validate_signing_authority(
    envelope: ArtifactEnvelope,
    signature_output: str,
    *,
    official_team_id: str = "",
) -> None:
    if envelope.signing_channel == "local":
        if (
            envelope.signing_authority != LOCAL_SIGNING_IDENTITY
            or f"Authority={LOCAL_SIGNING_IDENTITY}" not in signature_output
        ):
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "The local artifact was not signed by the configured GamGUI identity.",
            )
    elif envelope.signing_channel == "developer-id":
        authority = envelope.signing_authority
        if (
            not authority.startswith("Developer ID Application: ")
            or f"Authority={authority}" not in signature_output
            or not official_team_id
            or f"TeamIdentifier={official_team_id}" not in signature_output
        ):
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "The official artifact signing authority did not match its release manifest.",
            )
    else:
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "The artifact does not declare a supported signing channel.",
        )


def _codesign_leaf_sha256(
    bundle: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess],
    scratch_root: Path,
) -> str:
    """Hash the leaf certificate embedded in a macOS code signature."""

    root = Path(scratch_root)
    root.mkdir(parents=True, exist_ok=True)
    _owner_only_directory(root)
    with tempfile.TemporaryDirectory(
        prefix="codesign-cert-",
        dir=root,
    ) as temporary:
        work = Path(temporary)
        _owner_only_directory(work)
        run(
            [
                "codesign",
                "--display",
                "--extract-certificates",
                str(Path(bundle)),
            ],
            cwd=str(work),
            check=True,
            text=True,
            capture_output=True,
        )
        leaf = next(
            (
                path
                for path in (work / "codesign0", work / "codesign0.cer")
                if path.is_file() and not path.is_symlink()
            ),
            None,
        )
        if leaf is None:
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "The application signing certificate could not be extracted.",
            )
        return hashlib.sha256(leaf.read_bytes()).hexdigest()


def _validate_official_bundle_metadata(
    bundle: Path,
    signature_output: str,
) -> None:
    """Require the official bundle ID, hardened runtime, and secure timestamp."""

    from .release_manifest import OFFICIAL_BUNDLE_ID

    info_path = Path(bundle) / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException) as exc:
        raise ComponentError(
            "CMP-VERIFY-FAILED",
            "The official application bundle metadata is invalid.",
        ) from exc
    if not isinstance(info, dict) or info.get("CFBundleIdentifier") != OFFICIAL_BUNDLE_ID:
        raise ComponentError(
            "CMP-VERIFY-FAILED",
            "The official application bundle identifier is not approved.",
        )
    if "(runtime)" not in signature_output or "Timestamp=" not in signature_output:
        raise ComponentError(
            "CMP-VERIFY-FAILED",
            "The official application is missing hardened-runtime or timestamp evidence.",
        )


def _valid_sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdef" for char in value.lower())


def _owner_only(path: Path) -> None:
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _owner_only_directory(path: Path) -> None:
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def _tree_bytes(root: Path) -> int:
    path = Path(root)
    if path.is_file() and not path.is_symlink():
        return int(path.stat().st_size)
    if not path.is_dir() or path.is_symlink():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            total += int(item.stat().st_size)
    return total


def _require_available_space(path: Path, required_bytes: int) -> None:
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        available = int(shutil.disk_usage(target).free)
    except OSError as exc:
        raise ComponentError(
            "CMP-DOWNLOAD-FAILED",
            "Available disk space could not be verified safely.",
        ) from exc
    if available < max(0, int(required_bytes)):
        raise ComponentError(
            "CMP-DOWNLOAD-FAILED",
            "There is not enough free disk space to stage and roll back this application profile.",
        )


def _database_files(root: Path, *, exclude_updates: bool = True) -> Iterable[Path]:
    root = Path(root)
    if not root.is_dir():
        return ()
    results: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if exclude_updates and relative.parts and relative.parts[0] == "updates":
            continue
        name = path.name.lower()
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or name.endswith(("-wal", "-shm")):
            results.append(path)
    return results


def _require_within(path: Path, root: Path) -> None:
    resolved = Path(path).resolve()
    resolved_root = Path(root).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Refusing an updater file operation outside {resolved_root}.") from exc


def _remove_tree(path: Path, root: Path) -> None:
    _require_within(path, root)
    if Path(path).resolve() == Path(root).resolve():
        raise ValueError("Refusing to remove the updater root.")
    shutil.rmtree(path)
