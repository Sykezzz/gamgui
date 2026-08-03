"""Fail-closed local updater for the single managed GamGUI Mac."""

from __future__ import annotations

import json
import hashlib
import inspect
import os
import plistlib
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
import ctypes
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Optional

from .activation_lock import OwnerOnlyActivationLock
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
LOCAL_COMMAND_TIMEOUT_SECONDS = 30 * 60
DISK_SPACE_RESERVE_BYTES = 256 * 1024 * 1024
LOCAL_SIGNING_IDENTITY = "GamGUI Local"
INSTALLED_SOURCE_EVIDENCE = "installed-source"
VERIFIED_FILE_EVIDENCE = "verified-file"
ACTIVATION_APP_UPDATE = "app-update"
ACTIVATION_COMPONENT_SWAP = "component-swap"
ACTIVATION_VERIFIED_FILE = "verified-file"
ACTIVATION_TRANSACTION_ENV = "GAMGUI_ACTIVATION_TRANSACTION_ID"
ACTIVATION_PROBE_ENV = "GAMGUI_ACTIVATION_PROBE"
ACTIVATION_CURRENT_APP_ENV = "GAMGUI_UPDATE_CURRENT_APP"
ACTIVATION_RECOVERY_ENV = "GAMGUI_ACTIVATION_RECOVERY_PENDING"
ACTIVATION_PHASE_PREPARED = "prepared"
ACTIVATION_PHASE_SWAPPED = "swapped"
ACTIVATION_PHASE_HEALTH_PASSED = "health-passed"
ACTIVATION_PHASE_RECOVERY_REQUIRED = "recovery-required"
ACTIVATION_PHASES = frozenset(
    {
        ACTIVATION_PHASE_PREPARED,
        ACTIVATION_PHASE_SWAPPED,
        ACTIVATION_PHASE_HEALTH_PASSED,
        ACTIVATION_PHASE_RECOVERY_REQUIRED,
    }
)
HEALTH_STABILITY_SECONDS = 0.25


@dataclass(frozen=True)
class UpdateCandidate:
    sha: str
    html_url: str
    successful_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActivationJournal:
    """Durable paths needed to restore a pre-activation app and database."""

    transaction_id: str
    candidate_sha: str
    phase: str
    current_app: str
    pending_app: str
    incoming_app: str
    previous_app: str
    backup: str
    backup_app: str
    backup_sidecar: str
    candidate_sidecar: str
    database_snapshot: str
    health_marker: str

    @classmethod
    def from_json(cls, value: object) -> "ActivationJournal":
        if not isinstance(value, dict):
            raise ValueError("Activation journal is not an object.")

        def text(key: str, limit: int = 4096) -> str:
            raw = value.get(key, "")
            return raw[:limit] if isinstance(raw, str) else ""

        journal = cls(
            transaction_id=text("transaction_id", 32).lower(),
            candidate_sha=text("candidate_sha", 40).lower(),
            phase=text("phase", 32),
            current_app=text("current_app"),
            pending_app=text("pending_app"),
            incoming_app=text("incoming_app"),
            previous_app=text("previous_app"),
            backup=text("backup"),
            backup_app=text("backup_app"),
            backup_sidecar=text("backup_sidecar"),
            candidate_sidecar=text("candidate_sidecar"),
            database_snapshot=text("database_snapshot"),
            health_marker=text("health_marker"),
        )
        if (
            not re.fullmatch(r"[0-9a-f]{32}", journal.transaction_id)
            or not _valid_sha(journal.candidate_sha)
            or journal.phase not in ACTIVATION_PHASES
        ):
            raise ValueError("Activation journal identity is invalid.")
        path_values = (
            journal.current_app,
            journal.pending_app,
            journal.incoming_app,
            journal.previous_app,
            journal.backup,
            journal.backup_app,
            journal.backup_sidecar,
            journal.candidate_sidecar,
            journal.database_snapshot,
            journal.health_marker,
        )
        if any(not value or not Path(value).is_absolute() for value in path_values):
            raise ValueError("Activation journal paths are invalid.")
        return journal

    def with_phase(self, phase: str) -> "ActivationJournal":
        if phase not in ACTIVATION_PHASES:
            raise ValueError("Activation journal phase is invalid.")
        return replace(self, phase=phase)


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
    activation_transaction_id: str = ""
    activation_journal: Optional[ActivationJournal] = None
    activation_journal_invalid: bool = False

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
        activation_transaction_id = _text(
            "activation_transaction_id",
            32,
        ).lower()
        if not re.fullmatch(r"[0-9a-f]{32}", activation_transaction_id):
            activation_transaction_id = ""
        raw_journal = value.get("activation_journal")
        activation_journal: Optional[ActivationJournal] = None
        activation_journal_invalid = value.get("activation_journal_invalid") is True
        if raw_journal not in (None, {}):
            try:
                activation_journal = ActivationJournal.from_json(raw_journal)
            except (TypeError, ValueError):
                activation_journal_invalid = True
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
            component_prompt_answered=(
                value.get("component_prompt_answered", False) is True
            ),
            component_operation=component_operation,
            component_error_code=component_error_code,
            activation_transaction_id=(
                activation_transaction_id if candidate_sha else ""
            ),
            activation_journal=activation_journal,
            activation_journal_invalid=activation_journal_invalid,
        )


def activation_evidence_valid(state: UpdateState) -> bool:
    """Return whether a staged bundle has the required update evidence."""

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
        except FileNotFoundError:
            return UpdateState()
        except (AttributeError, OSError, TypeError, ValueError):
            return UpdateState(
                last_error=(
                    "The updater state is unreadable. GamGUI entered local "
                    "recovery mode and will not access Workspace."
                ),
                component_error_code="CMP-VERIFY-FAILED",
                activation_journal_invalid=True,
            )

    def save(self, state: UpdateState) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        _owner_only_directory(parent)
        encoded = (
            json.dumps(asdict(state), sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        descriptor, temporary_value = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=parent,
        )
        temporary = Path(temporary_value)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("The updater state could not be written.")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.path)
            _owner_only(self.path)
            _fsync_directory(parent)
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                if temporary.is_file() and not temporary.is_symlink():
                    temporary.unlink()
            except OSError:
                pass

    def load_runtime_projection(
        self,
        embedded: object,
        environment: Optional[dict[str, str]] = None,
    ) -> Optional[UpdateState]:
        """Return a validated, non-persistent candidate view during health startup."""

        state = self.load()
        env = os.environ if environment is None else environment
        transaction = str(env.get(ACTIVATION_TRANSACTION_ENV, "") or "").lower()
        expected_sha = str(env.get("GAMGUI_INSTALLED_SHA", "") or "").lower()
        marker_value = str(env.get("GAMGUI_UPDATE_HEALTH_MARKER", "") or "")
        current_value = str(env.get(ACTIVATION_CURRENT_APP_ENV, "") or "")
        candidate = state.candidate_artifact
        embedded_artifact = getattr(embedded, "artifact", None)
        if (
            env.get(ACTIVATION_PROBE_ENV) != "1"
            or not re.fullmatch(r"[0-9a-f]{32}", transaction)
            or transaction != state.activation_transaction_id
            or not _valid_sha(expected_sha)
            or expected_sha != state.candidate_sha
            or candidate is None
            or embedded_artifact is None
            or candidate_is_blocked(state)
            or not activation_evidence_valid(state)
        ):
            return None
        expected_marker = self.path.parent / "health" / f"{transaction}.json"
        try:
            marker = Path(marker_value).resolve()
            if marker != expected_marker.resolve():
                return None
            pending = Path(state.pending_app).resolve()
            pending.relative_to(self.path.parent.resolve())
            current_app = Path(current_value).resolve()
        except (OSError, ValueError):
            return None
        if (
            not pending.is_dir()
            or not (pending / "Contents" / "MacOS" / "GamGUI").is_file()
            or not current_app.is_dir()
            or not (current_app / "Contents" / "MacOS" / "GamGUI").is_file()
        ):
            return None
        if candidate.artifact_sha256 or getattr(sys, "frozen", False):
            try:
                from .components import bundle_sha256

                if (
                    not candidate.artifact_sha256
                    or bundle_sha256(current_app) != candidate.artifact_sha256
                ):
                    return None
            except (ComponentError, OSError):
                return None
        identity_fields = (
            "source_sha",
            "version",
            "profile",
            "component_set_digest",
            "architecture",
            "minimum_macos_version",
            "packaging_revision",
        )
        if any(
            getattr(candidate, field, None)
            != getattr(embedded_artifact, field, None)
            for field in identity_fields
        ):
            return None
        installed_components = list(component_ids_for_profile(candidate.profile))
        projected = replace(state)
        projected.installed_sha = candidate.source_sha
        projected.installed_profile = candidate.profile
        projected.installed_components = installed_components
        projected.installed_artifact = candidate
        projected.installed_signing_channel = (
            state.candidate_signing_channel or state.installed_signing_channel
        )
        projected.installed_signing_authority = (
            state.candidate_signing_authority
            or state.installed_signing_authority
        )
        projected.enabled_components = _enabled_components_after_activation(
            activation_kind=state.activation_kind,
            candidate_sha=state.candidate_sha,
            previous_sha=state.installed_sha,
            desired_components=state.desired_components,
            previous_enabled_components=state.enabled_components,
            installed_components=installed_components,
        )
        return projected


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


def _managed_mac_build_environment(
    environment: Optional[dict[str, str]] = None,
    home: Optional[Path] = None,
) -> dict[str, str]:
    """Return a deterministic toolchain PATH for a Finder-launched managed app."""

    result = dict(os.environ if environment is None else environment)
    resolved_home = Path(home) if home is not None else Path.home()
    preferred = (
        resolved_home / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
    )
    current = [
        Path(item)
        for item in result.get("PATH", "").split(os.pathsep)
        if item
    ]
    ordered: list[str] = []
    for candidate in (*preferred, *current):
        value = str(candidate)
        if value not in ordered:
            ordered.append(value)
    result["PATH"] = os.pathsep.join(ordered)
    return result


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
        command_env = _managed_mac_build_environment()
        checkout = self.root / "source" / candidate.sha
        checkout.parent.mkdir(parents=True, exist_ok=True)
        _require_within(checkout, self.root)
        if checkout.exists():
            _remove_tree(checkout, self.root)
        self._command(
            ["git", "clone", "--filter=blob:none", "--no-checkout", self.repository_url, str(checkout)],
            env=command_env,
        )
        # Keep commit ancestry visible: a depth-one fetch marks the candidate as a
        # shallow root and makes the forward-only merge-base check reject valid updates.
        self._command(
            [
                "git",
                "-C",
                str(checkout),
                "fetch",
                "--no-tags",
                "origin",
                candidate.sha,
            ],
            env=command_env,
        )
        self._command(
            ["git", "-C", str(checkout), "checkout", "--detach", candidate.sha],
            env=command_env,
        )
        head = self._command(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture=True,
            env=command_env,
        ).stdout.strip()
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
                env=command_env,
            )
            if ancestry.returncode != 0:
                raise RuntimeError(
                    "The validated update is not a forward descendant of the installed commit."
                )

        self._command(["make", "setup"], cwd=checkout, env=command_env)
        self._command(["make", "gam"], cwd=checkout, env=command_env)
        identities = self._command(
            ["security", "find-identity", "-p", "codesigning", "-v"],
            capture=True,
            env=command_env,
        ).stdout
        if f'"{LOCAL_SIGNING_IDENTITY}"' not in identities:
            raise RuntimeError(
                f'The required local signing identity "{LOCAL_SIGNING_IDENTITY}" is unavailable.'
            )
        build_env = command_env.copy()
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
        self._command(
            ["codesign", "--verify", "--deep", "--strict", str(built)],
            env=command_env,
        )
        self._command(
            [str(executable), "--self-test"],
            cwd=checkout,
            env=command_env,
        )
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

        ``pre_execution_policy`` runs after immutable identity and compatibility
        checks but before platform signature commands or executable content in
        the candidate. Update coordinators use this rejection-only seam to stop
        blocked, replayed, or non-forward artifacts without granting them code
        execution or invoking unnecessary external verification.
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
            if pre_execution_policy is not None:
                pre_execution_policy(envelope)
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
            timeout=LOCAL_COMMAND_TIMEOUT_SECONDS,
        )


class UpdateCoordinator:
    """Coordinate update discovery, offline staging, blocklisting, and backup retention."""

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
                state.last_error = ""
                state.component_error_code = ""
                self.store.save(state)
                return existing
            self.block(
                state.candidate_sha,
                "The staged update lacked required update evidence.",
            )
            return None
        candidate: Optional[UpdateCandidate] = None
        lease = None
        try:
            if state.installed_signing_channel == "developer-id":
                raise ComponentError(
                    "CMP-UPDATE-CHANNEL",
                    "Official-channel updates must be installed from a verified "
                    "notarized release file; the installed app was not changed."
                )
            if self.active_jobs():
                raise ComponentError(
                    "CMP-ACTIVE-JOB",
                    "An administrative operation is active; update preparation was deferred.",
                )
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
                state.component_error_code = ""
                self.store.save(state)
                return None
            if READY_CHECK not in candidate.successful_checks:
                raise RuntimeError(
                    "The candidate did not include the required exact-SHA validation check."
                )
            if self.active_jobs():
                raise ComponentError(
                    "CMP-ACTIVE-JOB",
                    "An administrative operation became active; update preparation was deferred.",
                )
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
                raise ComponentError(
                    "CMP-ACTIVE-JOB",
                    "An administrative operation became active; update activation was deferred.",
                )
            state.candidate_sha = candidate.sha
            state.pending_app = str(pending)
            # Exact-SHA CI, sealed artifact identity, and the staged bundle's
            # offline self-test establish automatic-update readiness. A live
            # Workspace canary would prompt for Keychain access during startup,
            # so it remains only on the user-initiated verified-file path.
            state.canary_result = ""
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
        except ComponentError as exc:
            state.last_error = str(exc)
            state.component_error_code = exc.error_code
            self.store.save(state)
            return None
        except Exception as exc:
            # Preparation failures can be environmental or transient (network, toolchain,
            # certificate, or offline self-test). Only a failed activation/rollback blocklists a
            # SHA; otherwise the same validated commit may be retried after the environment is
            # repaired.
            state.last_error = str(exc)
            state.component_error_code = (
                "CMP-UPDATE-SIGNING"
                if "signing identity" in state.last_error.casefold()
                else "CMP-UPDATE-PREPARE-FAILED"
            )
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
        state.activation_transaction_id = ""
        state.activation_journal = None
        state.activation_journal_invalid = False
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
        state.activation_transaction_id = ""
        state.activation_journal = None
        state.activation_journal_invalid = False
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
        state.activation_transaction_id = ""
        state.activation_journal = None
        state.activation_journal_invalid = False
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
        activation_lock: Optional[OwnerOnlyActivationLock] = None,
        transaction_id: str = "",
    ) -> bool:
        owned_lock = activation_lock is None
        lock = activation_lock
        if lock is None:
            lock = OwnerOnlyActivationLock.try_acquire(
                self.root / "activation.lock"
            )
            if lock is None:
                return False
        try:
            try:
                lock_matches = (
                    lock.path.resolve()
                    == (self.root / "activation.lock").resolve()
                )
            except OSError:
                lock_matches = False
            if not lock.held or not lock_matches:
                return False
            state = self.store.load()
            transaction = (
                transaction_id
                or state.activation_transaction_id
                or secrets.token_hex(16)
            ).lower()
            if not re.fullmatch(r"[0-9a-f]{32}", transaction):
                return False
            if state.activation_transaction_id not in {"", transaction}:
                return False
            if not state.activation_transaction_id:
                state.activation_transaction_id = transaction
                try:
                    self.store.save(state)
                except Exception:
                    self._launch_previous(Path(current_app))
                    return False
            return self._install_locked(
                sha,
                pending_app,
                current_app,
                health_timeout=health_timeout,
                transaction_id=transaction,
            )
        finally:
            if owned_lock:
                try:
                    lock.release()
                except Exception:
                    pass

    def _install_locked(
        self,
        sha: str,
        pending_app: Path,
        current_app: Path,
        *,
        health_timeout: float,
        transaction_id: str,
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

        backup = (
            self.root
            / "backups"
            / f"{int(self._clock())}-{sha[:12]}-{transaction_id}"
        )
        database_snapshot = backup / "database"
        migration_copy = backup / "migration-copy"
        backup_app = backup / "GamGUI.app"
        backup_sidecar = backup / "installed-artifact.json"
        candidate_sidecar = backup / "candidate-artifact.json"
        installed_sidecar = artifact_sidecar_path(current_app)
        pending_sidecar = artifact_sidecar_path(pending_app)
        marker = self.root / "health" / f"{transaction_id}.json"
        incoming = (
            current_app.parent
            / f".{current_app.name}.{transaction_id}.incoming"
        )
        previous = (
            current_app.parent
            / f".{current_app.name}.{transaction_id}.previous"
        )
        process = None
        snapshot_ready = False
        sidecar_tmp: Optional[Path] = None
        journal_saved = False
        recovery_completed = False
        activation_committed = False

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
                state.activation_transaction_id = ""
                self.store.save(state)
            except Exception:
                pass
            self._launch_previous(current_app)
            return False

        try:
            self._validate_install_request(
                state,
                sha,
                pending_app,
                current_app,
                transaction_id=transaction_id,
            )
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
                timeout=LOCAL_COMMAND_TIMEOUT_SECONDS,
            )
            _remove_tree(migration_copy, backup)

            shutil.copytree(current_app, backup_app, symlinks=True)
            if installed_sidecar.is_file():
                shutil.copy2(installed_sidecar, backup_sidecar)
                _owner_only(backup_sidecar)
            if incoming.exists() or previous.exists() or marker.exists():
                raise RuntimeError(
                    "The activation transaction paths already exist."
                )
            shutil.copytree(pending_app, incoming, symlinks=True)
            shutil.copy2(pending_sidecar, candidate_sidecar)
            _owner_only(candidate_sidecar)
            self._verify_candidate_bundle(
                state,
                incoming,
                current_app,
                sidecar=candidate_sidecar,
            )
            _fsync_tree(backup)
            _fsync_tree(incoming)

            journal = ActivationJournal(
                transaction_id=transaction_id,
                candidate_sha=sha,
                phase=ACTIVATION_PHASE_PREPARED,
                current_app=str(current_app.resolve()),
                pending_app=str(pending_app.resolve()),
                incoming_app=str(incoming.resolve()),
                previous_app=str(previous.resolve()),
                backup=str(backup.resolve()),
                backup_app=str(backup_app.resolve()),
                backup_sidecar=str(backup_sidecar.resolve()),
                candidate_sidecar=str(candidate_sidecar.resolve()),
                database_snapshot=str(database_snapshot.resolve()),
                health_marker=str(marker.resolve()),
            )
            state = self.store.load()
            if (
                state.activation_transaction_id != transaction_id
                or state.candidate_sha != sha
                or Path(state.pending_app) != pending_app
            ):
                raise RuntimeError(
                    "The activation transaction state changed before preparation."
                )
            state.activation_journal = journal
            state.activation_journal_invalid = False
            self.store.save(state)
            journal_saved = True

            _atomic_exchange(current_app, incoming)
            os.replace(incoming, previous)
            _fsync_directory(current_app.parent)
            journal = journal.with_phase(ACTIVATION_PHASE_SWAPPED)
            self._save_journal(journal)

            marker.parent.mkdir(parents=True, exist_ok=True)
            _owner_only_directory(marker.parent)
            launch_env = os.environ.copy()
            launch_env["GAMGUI_UPDATE_HEALTH_MARKER"] = str(marker)
            launch_env["GAMGUI_INSTALLED_SHA"] = sha
            launch_env["GAMGUI_SKIP_UPDATE_ONCE"] = "1"
            launch_env[ACTIVATION_TRANSACTION_ENV] = transaction_id
            launch_env[ACTIVATION_PROBE_ENV] = "1"
            launch_env[ACTIVATION_CURRENT_APP_ENV] = str(current_app)
            process = self._popen(
                [str(current_app / "Contents" / "MacOS" / "GamGUI")],
                env=launch_env,
            )
            expected_health = {
                "ok": True,
                "transaction_id": transaction_id,
                "sha": sha,
                "profile": candidate_artifact.profile,
                "component_set_digest": candidate_artifact.component_set_digest,
            }
            if not self._wait_for_health(
                marker,
                process,
                health_timeout,
                expected_health,
            ):
                raise RuntimeError("The updated application did not report startup health in time.")
            self._stop(process)
            process = None
            journal = journal.with_phase(ACTIVATION_PHASE_HEALTH_PASSED)
            self._save_journal(journal)

            sidecar_tmp = installed_sidecar.with_name(
                f"{installed_sidecar.name}.{transaction_id}.tmp"
            )
            if sidecar_tmp.exists():
                raise RuntimeError(
                    "The activation sidecar transaction path already exists."
                )
            shutil.copy2(candidate_sidecar, sidecar_tmp)
            _fsync_file(sidecar_tmp)
            os.replace(sidecar_tmp, installed_sidecar)
            _owner_only(installed_sidecar)
            _fsync_directory(installed_sidecar.parent)
            state = self.store.load()
            if (
                state.activation_transaction_id != transaction_id
                or state.activation_journal != journal
            ):
                raise RuntimeError(
                    "The activation transaction state changed before commit."
                )
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
            state.activation_transaction_id = ""
            state.activation_journal = None
            state.activation_journal_invalid = False
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
            activation_committed = True
            # State persistence is the activation commit point. Cleanup after this point is
            # best-effort and must never roll back a healthy application.
            self._best_effort_remove(previous, current_app.parent)
            self._best_effort_remove(pending_app, self.root)
            self._best_effort_unlink(pending_sidecar, self.root)
            self._best_effort_unlink(candidate_sidecar, self.root)
            self._best_effort_unlink(marker, self.root)
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
            self._launch_activated(current_app)
            return True
        except Exception as exc:
            if activation_committed:
                # The fsynced state transition is the commit point. Post-commit
                # cleanup or relaunch failure must not reinterpret the verified
                # candidate as an uncommitted transaction.
                return True
            process_stopped = False
            try:
                self._stop(process)
                process_stopped = True
            except Exception:
                process_stopped = False
            if journal_saved:
                try:
                    state = self.store.load()
                    active_journal = state.activation_journal
                    if (
                        active_journal is not None
                        and active_journal.transaction_id == transaction_id
                    ):
                        self._save_journal(
                            active_journal.with_phase(
                                ACTIVATION_PHASE_RECOVERY_REQUIRED
                            ),
                            error=(
                                "Activation recovery is pending. GamGUI will keep "
                                "Workspace administration paused until the previous "
                                "application and database are restored."
                            ),
                        )
                except Exception:
                    pass
            if journal_saved and process_stopped:
                try:
                    recovery_completed = self.recover(
                        current_app=current_app,
                        transaction_id=transaction_id,
                    )
                except Exception:
                    recovery_completed = False
                if recovery_completed:
                    self._launch_previous(current_app)
                return False
            if journal_saved:
                # The durable journal, backup, pending bundle, and database
                # snapshot intentionally remain untouched. A later launch
                # recovers them after the candidate process is provably gone.
                return False
            try:
                reason = str(exc)
                self._block(
                    sha,
                    reason,
                    database_snapshot if snapshot_ready else None,
                )
            except Exception:
                pass
            self._best_effort_remove(pending_app, self.root)
            self._best_effort_unlink(pending_sidecar, self.root)
            self._best_effort_remove(backup, self.root)
            self._launch_previous(current_app)
            return False
        finally:
            if activation_committed or recovery_completed or not journal_saved:
                self._best_effort_remove(incoming, current_app.parent)
                self._best_effort_unlink(marker, self.root)
            if sidecar_tmp is not None:
                self._best_effort_unlink(sidecar_tmp, current_app.parent)

    def _save_journal(
        self,
        journal: ActivationJournal,
        *,
        error: str = "",
    ) -> None:
        state = self.store.load()
        if (
            state.activation_transaction_id not in {
                "",
                journal.transaction_id,
            }
            or (
                state.candidate_sha
                and state.candidate_sha != journal.candidate_sha
            )
        ):
            raise RuntimeError("The durable activation journal changed.")
        state.activation_journal = journal
        state.activation_journal_invalid = False
        if error:
            state.component_error_code = "CMP-VERIFY-FAILED"
            state.last_error = error
        self.store.save(state)

    def recover(
        self,
        *,
        current_app: Optional[Path] = None,
        activation_lock: Optional[OwnerOnlyActivationLock] = None,
        transaction_id: str = "",
    ) -> bool:
        """Restore a journaled pre-activation bundle and database exactly once."""

        if activation_lock is not None:
            try:
                lock_matches = (
                    activation_lock.held
                    and activation_lock.path.resolve()
                    == (self.root / "activation.lock").resolve()
                )
            except OSError:
                lock_matches = False
            if not lock_matches:
                return False
        state = self.store.load()
        journal = state.activation_journal
        if journal is None or state.activation_journal_invalid:
            return False
        if transaction_id and journal.transaction_id != transaction_id:
            return False
        expected_current = Path(journal.current_app)
        if current_app is not None:
            try:
                if Path(current_app).resolve() != expected_current.resolve():
                    return False
            except OSError:
                return False
        self._validate_journal(journal)

        backup_app = Path(journal.backup_app)
        backup_sidecar = Path(journal.backup_sidecar)
        database_snapshot = Path(journal.database_snapshot)
        installed_sidecar = artifact_sidecar_path(expected_current)
        restore_copy = expected_current.parent / (
            f".{expected_current.name}.{journal.transaction_id}.restore"
        )
        if restore_copy.exists():
            _remove_tree(restore_copy, expected_current.parent)
        shutil.copytree(backup_app, restore_copy, symlinks=True)
        _fsync_tree(restore_copy)
        if not (
            restore_copy / "Contents" / "MacOS" / "GamGUI"
        ).is_file():
            raise RuntimeError("The rollback application snapshot is incomplete.")
        if state.installed_artifact is not None and backup_sidecar.is_file():
            verify_bundle_artifact(
                restore_copy,
                expected_profile=state.installed_artifact.profile,
                expected_artifact=state.installed_artifact,
                sidecar=backup_sidecar,
            )
        if sys.platform == "darwin":
            self._run(
                ["codesign", "--verify", "--deep", "--strict", str(restore_copy)],
                check=True,
                text=True,
                capture_output=True,
            )
        if expected_current.exists():
            _atomic_exchange(expected_current, restore_copy)
        else:
            os.replace(restore_copy, expected_current)
        if backup_sidecar.is_file():
            sidecar_restore = installed_sidecar.with_name(
                f"{installed_sidecar.name}.{journal.transaction_id}.restore"
            )
            shutil.copy2(backup_sidecar, sidecar_restore)
            _fsync_file(sidecar_restore)
            os.replace(sidecar_restore, installed_sidecar)
            _owner_only(installed_sidecar)
            _fsync_directory(installed_sidecar.parent)
        elif installed_sidecar.exists() and not installed_sidecar.is_symlink():
            installed_sidecar.unlink()
        restore_databases(self.data_root, database_snapshot)
        self._block(
            journal.candidate_sha,
            "The staged application failed activation and was rolled back.",
            database_snapshot,
        )

        self._best_effort_remove(restore_copy, expected_current.parent)
        self._best_effort_remove(Path(journal.incoming_app), expected_current.parent)
        self._best_effort_remove(Path(journal.previous_app), expected_current.parent)
        self._best_effort_remove(Path(journal.pending_app), self.root)
        self._best_effort_unlink(
            artifact_sidecar_path(Path(journal.pending_app)),
            self.root,
        )
        self._best_effort_unlink(Path(journal.candidate_sidecar), self.root)
        self._best_effort_unlink(Path(journal.health_marker), self.root)
        return True

    def _validate_journal(self, journal: ActivationJournal) -> None:
        current = Path(journal.current_app)
        pending = Path(journal.pending_app)
        incoming = Path(journal.incoming_app)
        previous = Path(journal.previous_app)
        backup = Path(journal.backup)
        if current.suffix != ".app":
            raise ValueError("The activation journal application path is invalid.")
        _require_within(pending, self.root)
        _require_within(backup, self.root / "backups")
        _require_within(Path(journal.backup_app), backup)
        _require_within(Path(journal.backup_sidecar), backup)
        _require_within(Path(journal.candidate_sidecar), backup)
        _require_within(Path(journal.database_snapshot), backup)
        _require_within(Path(journal.health_marker), self.root / "health")
        if (
            incoming.parent.resolve() != current.parent.resolve()
            or previous.parent.resolve() != current.parent.resolve()
            or incoming.name
            != f".{current.name}.{journal.transaction_id}.incoming"
            or previous.name
            != f".{current.name}.{journal.transaction_id}.previous"
        ):
            raise ValueError("The activation journal transaction paths are invalid.")
        if not Path(journal.backup_app).is_dir():
            raise ValueError("The activation rollback bundle is missing.")
        if not Path(journal.database_snapshot).is_dir():
            raise ValueError("The activation database snapshot is missing.")

    def _validate_install_request(
        self,
        state: UpdateState,
        sha: str,
        pending_app: Path,
        current_app: Path,
        *,
        transaction_id: str = "",
    ) -> None:
        if not _valid_sha(sha):
            raise ValueError("The candidate SHA is invalid.")
        if (
            not re.fullmatch(r"[0-9a-f]{32}", transaction_id)
            or state.activation_transaction_id != transaction_id
        ):
            raise ValueError("The activation transaction does not match staged state.")
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
            raise ValueError("The candidate lacks required update evidence.")
        _require_within(pending_app, self.root)
        if not pending_app.is_dir() or not (pending_app / "Contents" / "MacOS" / "GamGUI").is_file():
            raise ValueError("The staged application bundle is incomplete.")
        if current_app.suffix != ".app" or not current_app.is_dir():
            raise ValueError("The installed application bundle could not be resolved.")
        if pending_app.is_symlink() or current_app.is_symlink():
            raise ValueError("Application bundle symlinks are not accepted.")
        self._verify_candidate_bundle(
            state,
            pending_app,
            current_app,
            sidecar=artifact_sidecar_path(pending_app),
        )

    def _verify_candidate_bundle(
        self,
        state: UpdateState,
        bundle: Path,
        current_app: Path,
        *,
        sidecar: Path,
    ) -> ArtifactEnvelope:
        candidate = state.candidate_artifact
        if candidate is None:
            raise ValueError("The staged update has no verified artifact identity.")
        envelope = verify_bundle_artifact(
            bundle,
            expected_profile=candidate.profile,
            expected_artifact=candidate,
            sidecar=sidecar,
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
                ["codesign", "--verify", "--deep", "--strict", str(bundle)],
                check=True,
                text=True,
                capture_output=True,
            )
            if envelope.signing_channel == "local":
                if _codesign_leaf_sha256(
                    bundle,
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
                ["codesign", "-dv", "--verbose=4", str(bundle)],
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
                    bundle,
                    signature_output,
                )
                self._run(
                    ["xcrun", "stapler", "validate", str(bundle)],
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
                        str(bundle),
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                )
        return envelope

    def _wait_for_health(
        self,
        marker: Path,
        process: object,
        timeout: float,
        expected_payload: dict[str, object],
    ) -> bool:
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            poll = getattr(process, "poll", None)
            if callable(poll) and poll() is not None:
                return False
            if marker.is_file():
                try:
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    return False
                if not isinstance(payload, dict) or payload != expected_payload:
                    return False
                self._sleep(
                    min(
                        HEALTH_STABILITY_SECONDS,
                        max(0.01, deadline - self._clock()),
                    )
                )
                if callable(poll) and poll() is not None:
                    return False
                try:
                    stable_payload = json.loads(
                        marker.read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeError, json.JSONDecodeError):
                    return False
                return stable_payload == expected_payload
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
        state.activation_transaction_id = ""
        state.activation_journal = None
        state.activation_journal_invalid = False
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
        self._launch_bundle(current_app)

    def _launch_activated(self, current_app: Path) -> None:
        self._launch_bundle(current_app)

    def _launch_bundle(self, current_app: Path) -> None:
        executable = current_app / "Contents" / "MacOS" / "GamGUI"
        if not executable.is_file():
            return
        env = os.environ.copy()
        for key in (
            "GAMGUI_UPDATE_HEALTH_MARKER",
            "GAMGUI_INSTALLED_SHA",
            ACTIVATION_TRANSACTION_ENV,
            ACTIVATION_PROBE_ENV,
            ACTIVATION_CURRENT_APP_ENV,
            ACTIVATION_RECOVERY_ENV,
        ):
            env.pop(key, None)
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
        _owner_only_directory(target.parent)
        _prepare_owner_only_file(target)
        with closing(
            sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        ) as source_db, closing(sqlite3.connect(target)) as target_db:
            source_db.backup(target_db)
        _owner_only(target)
        _fsync_file(target)
        copied.append(target)
    _fsync_tree(destination)
    return copied


def restore_databases(data_root: Path, snapshot: Path) -> None:
    """Restore the matching pre-update database set after a failed startup."""
    data_root = Path(data_root)
    snapshot = Path(snapshot)
    if not snapshot.is_dir():
        return
    snapshot_files = tuple(
        source
        for source in _database_files(snapshot, exclude_updates=False)
        if not source.name.lower().endswith(("-wal", "-shm"))
    )
    restored_relatives = {
        source.relative_to(snapshot)
        for source in snapshot_files
    }
    for source in snapshot_files:
        relative = source.relative_to(snapshot)
        target = data_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _owner_only_directory(target.parent)
        for companion in (Path(f"{target}-wal"), Path(f"{target}-shm")):
            if companion.is_file() and not companion.is_symlink():
                companion.unlink()
        temporary = target.with_name(
            f".{target.name}.{secrets.token_hex(16)}.restore"
        )
        _prepare_owner_only_file(temporary)
        try:
            shutil.copyfile(source, temporary)
            _owner_only(temporary)
            _fsync_file(temporary)
            os.replace(temporary, target)
            _owner_only(target)
            _fsync_directory(target.parent)
        finally:
            try:
                if temporary.is_file() and not temporary.is_symlink():
                    temporary.unlink()
            except OSError:
                pass
    for current in tuple(_database_files(data_root)):
        if current.relative_to(data_root) not in restored_relatives:
            current.unlink()
            _fsync_directory(current.parent)


def prepare_database_schemas(data_root: Path) -> list[Path]:
    """Open every persistent SQLite store so its migrations run on the supplied copy."""
    from .calendar_index import CalendarIndex
    from .classroom.index import CourseIndex
    from .classroom.manifests import RosterManifestStore
    from .classroom_access import EntitlementStore
    from .directory_index import DirectoryIndex
    from .drive.operations import DriveOperationStore

    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    paths = [
        root / "directory_index.db",
        root / "calendar_index.db",
        root / "classroom_courses.db",
        root / "classroom_roster_operations.db",
        root / "classroom_teacher_entitlements.db",
        root / "drive_operations.db",
    ]
    DirectoryIndex(paths[0], "__migration_check__")
    CalendarIndex(paths[1])
    CourseIndex(paths[2])
    RosterManifestStore(paths[3])
    EntitlementStore(paths[4])
    DriveOperationStore(paths[5])
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


def write_health_marker_from_environment(
    payload: Optional[dict[str, object]] = None,
) -> None:
    marker = os.environ.get("GAMGUI_UPDATE_HEALTH_MARKER", "")
    if not marker:
        return
    path = Path(marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    evidence = dict(payload or {"ok": True})
    legacy_sha = os.environ.get("GAMGUI_INSTALLED_SHA", "").lower()
    legacy_contract = (
        os.environ.get("GAMGUI_SKIP_UPDATE_ONCE") == "1"
        and not os.environ.get(ACTIVATION_PROBE_ENV)
        and not os.environ.get(ACTIVATION_TRANSACTION_ENV)
        and _valid_sha(legacy_sha)
        and path.name == f"{legacy_sha}.ok"
        and evidence.get("ok") is True
        and evidence.get("transaction_id") == ""
        and evidence.get("sha") == legacy_sha
        and evidence.get("profile") == CORE_PROFILE
        and isinstance(evidence.get("component_set_digest"), str)
        and re.fullmatch(
            r"[0-9a-f]{64}",
            str(evidence.get("component_set_digest")),
        )
        is not None
    )
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    descriptor = -1
    try:
        if legacy_contract:
            encoded = b"ok\n"
        else:
            encoded = (
                json.dumps(evidence, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("The activation health receipt could not be written.")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _owner_only(path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            if temporary.is_file() and not temporary.is_symlink():
                temporary.unlink()
        except OSError:
            pass


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


def _prepare_owner_only_file(path: Path) -> None:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _owner_only(path)


def _fsync_file(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes on POSIX activation filesystems."""

    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(path: Path) -> None:
    """Flush regular files and directory entries before journaling their paths."""

    root = Path(path)
    if os.name != "posix":
        return
    directories = [root]
    for item in root.rglob("*"):
        if item.is_symlink():
            continue
        if item.is_file():
            _fsync_file(item)
        elif item.is_dir():
            directories.append(item)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _atomic_exchange(first: Path, second: Path) -> None:
    """Atomically swap two bundle paths on supported production filesystems."""

    left = Path(first)
    right = Path(second)
    if (
        left.parent.resolve() != right.parent.resolve()
        or not left.is_dir()
        or not right.is_dir()
        or left.is_symlink()
        or right.is_symlink()
    ):
        raise ValueError("Activation bundles must be real directories on one filesystem.")

    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renameatx_np = getattr(libc, "renameatx_np", None)
        if renameatx_np is None:
            raise OSError("Atomic application bundle exchange is unavailable.")
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        result = renameatx_np(
            -2,
            os.fsencode(left),
            -2,
            os.fsencode(right),
            0x00000002,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(
                error_number,
                os.strerror(error_number),
                f"{left} <-> {right}",
            )
        _fsync_directory(left.parent)
        return

    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100,
                os.fsencode(left),
                -100,
                os.fsencode(right),
                0x00000002,
            )
            if result == 0:
                _fsync_directory(left.parent)
                return
            error_number = ctypes.get_errno()
            raise OSError(
                error_number,
                os.strerror(error_number),
                f"{left} <-> {right}",
            )

    # Windows is not a supported deployment target. This fallback exists so
    # local tooling can exercise recovery semantics; macOS never reaches it.
    temporary = left.parent / (
        f".{left.name}.{secrets.token_hex(16)}.exchange"
    )
    os.replace(left, temporary)
    try:
        os.replace(right, left)
        os.replace(temporary, right)
    except BaseException:
        if not left.exists() and temporary.exists():
            os.replace(temporary, left)
        raise


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
