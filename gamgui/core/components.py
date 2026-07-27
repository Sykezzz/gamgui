"""First-party optional component and immutable build-profile contracts.

GamGUI intentionally does not implement a general plug-in loader.  The only optional
component is compiled into the ``classroom-oneroster`` application profile and is
described by the code-owned allowlist below.  External files may describe and verify a
signed application artifact, but can never name Python code for the running process to
load.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .activity import ActivityBusyError, ActivityRegistry, activity_registry
from .paths import app_data_dir

CORE_PROFILE = "core"
ONEROSTER_PROFILE = "classroom-oneroster"
ONEROSTER_COMPONENT = "classroom-oneroster"
SUPPORTED_PROFILES = (CORE_PROFILE, ONEROSTER_PROFILE)
HOST_COMPONENT_API_VERSION = "1"
PROFILE_METADATA_RELATIVE = Path("resources") / "components" / "profile.json"
ARTIFACT_SIDECAR_SUFFIX = ".artifact.json"
DEFAULT_SNAPSHOT_RETENTION_DAYS = 30
COMPONENT_ERROR_CODES = frozenset(
    {
        "CMP-NOT-INSTALLED",
        "CMP-DISABLED",
        "CMP-AUTH-REQUIRED",
        "CMP-INCOMPATIBLE",
        "CMP-VERIFY-FAILED",
        "CMP-DOWNLOAD-FAILED",
        "CMP-ACTIVE-JOB",
        "CMP-RESTART-REQUIRED",
    }
)


class ComponentError(RuntimeError):
    """A privacy-safe, operator-facing component failure."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class ComponentStatusName(str, Enum):
    NOT_INSTALLED = "not-installed"
    PREPARING = "preparing"
    RESTART_REQUIRED = "restart-required"
    INSTALLED_DISABLED = "installed-disabled"
    ENABLED = "enabled"
    UPDATE_AVAILABLE = "update-available"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class ComponentManifest:
    """Allowlisted code and resources belonging to one first-party component."""

    component_id: str
    version: str
    host_api_version: str
    module_root: str
    route_prefixes: tuple[str, ...]
    assets: tuple[str, ...]
    migrations: tuple[str, ...]
    schema_compatibility: str
    required_capabilities: tuple[str, ...]

    @classmethod
    def from_json(cls, value: object) -> "ComponentManifest":
        if not isinstance(value, Mapping):
            raise ComponentError(
                "CMP-INCOMPATIBLE",
                "Embedded component manifest has an invalid shape.",
            )

        def text(key: str, limit: int = 256) -> str:
            raw = value.get(key, "")
            if isinstance(raw, (str, int)):
                return str(raw)[:limit]
            return ""

        def strings(key: str, limit: int = 64) -> tuple[str, ...]:
            raw = value.get(key, ())
            if not isinstance(raw, list) or len(raw) > limit:
                raise ComponentError(
                    "CMP-INCOMPATIBLE",
                    f"Embedded component manifest has invalid {key}.",
                )
            if not all(isinstance(item, str) and item for item in raw):
                raise ComponentError(
                    "CMP-INCOMPATIBLE",
                    f"Embedded component manifest has invalid {key}.",
                )
            return tuple(item[:512] for item in raw)

        manifest = cls(
            component_id=text("id", 64),
            version=text("version", 64),
            host_api_version=text("host_api_version", 32),
            module_root=text("module_root", 128),
            route_prefixes=strings("route_prefixes"),
            assets=strings("assets"),
            migrations=strings("migrations"),
            schema_compatibility=text("schema_compatibility", 32),
            required_capabilities=strings("required_capabilities"),
        )
        _validate_component_manifest(manifest)
        return manifest

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.component_id,
            "version": self.version,
            "host_api_version": self.host_api_version,
            "module_root": self.module_root,
            "route_prefixes": list(self.route_prefixes),
            "assets": list(self.assets),
            "migrations": list(self.migrations),
            "schema_compatibility": self.schema_compatibility,
            "required_capabilities": list(self.required_capabilities),
        }


ONEROSTER_MANIFEST = ComponentManifest(
    component_id=ONEROSTER_COMPONENT,
    version="1",
    host_api_version=HOST_COMPONENT_API_VERSION,
    module_root="gamgui.components.oneroster",
    route_prefixes=("/classroom/imports",),
    assets=(
        "templates/oneroster.html",
        "templates/_oneroster_*.html",
    ),
    migrations=("oneroster:v1",),
    schema_compatibility="1",
    required_capabilities=("CLASSROOM", "DIRECTORY"),
)


def _validate_component_manifest(manifest: ComponentManifest) -> None:
    """Accept compatible OneRoster evolution without accepting new code roots."""

    if (
        manifest.component_id != ONEROSTER_COMPONENT
        or not manifest.version
        or manifest.host_api_version != HOST_COMPONENT_API_VERSION
        or manifest.module_root != "gamgui.components.oneroster"
        or manifest.schema_compatibility != "1"
    ):
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "Embedded component manifest is not compatible with this host.",
        )
    if (
        not manifest.route_prefixes
        or manifest.route_prefixes[0] != "/classroom/imports"
        or any(
            prefix != "/classroom/imports"
            and not prefix.startswith("/classroom/imports/")
            for prefix in manifest.route_prefixes
        )
    ):
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "Embedded component routes are outside the allowlisted workspace.",
        )
    if set(manifest.required_capabilities) != {"CLASSROOM", "DIRECTORY"}:
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "Embedded component capabilities are not allowlisted.",
        )
    if (
        "templates/oneroster.html" not in manifest.assets
        or any(not _safe_component_asset(item) for item in manifest.assets)
    ):
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "Embedded component assets are outside the allowlisted component paths.",
        )
    if any(
        not item.startswith("oneroster:")
        or len(item) > 128
        or not all(character.isalnum() or character in {":", "-", "_", "."} for character in item)
        for item in manifest.migrations
    ):
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "Embedded component migrations are outside the allowlisted namespace.",
        )


def _safe_component_asset(value: str) -> bool:
    if "\\" in value or value.startswith("/") or not value:
        return False
    path = Path(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        return False
    if path.parts[0] not in {"templates", "static"}:
        return False
    return "oneroster" in value.casefold()


_COMPONENTS_BY_ID = {ONEROSTER_COMPONENT: ONEROSTER_MANIFEST}
_PROFILE_COMPONENT_IDS = {
    CORE_PROFILE: (),
    ONEROSTER_PROFILE: (ONEROSTER_COMPONENT,),
}


def normalize_profile(value: object, *, default: str = CORE_PROFILE) -> str:
    profile = str(value or "").strip().lower()
    if not profile:
        profile = default
    if profile not in SUPPORTED_PROFILES:
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            f"Unsupported application profile: {profile[:64] or '(empty)'}.",
        )
    return profile


def manifests_for_profile(profile: str) -> tuple[ComponentManifest, ...]:
    normalized = normalize_profile(profile)
    return tuple(_COMPONENTS_BY_ID[item] for item in _PROFILE_COMPONENT_IDS[normalized])


def component_ids_for_profile(profile: str) -> tuple[str, ...]:
    return tuple(item.component_id for item in manifests_for_profile(profile))


def component_set_digest(profile: str) -> str:
    payload = [item.to_json() for item in manifests_for_profile(profile)]
    return _component_payload_digest(payload)


def _component_payload_digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ComponentArtifactId:
    """Identity of one exact application-profile artifact."""

    source_sha: str
    version: str
    profile: str
    component_set_digest: str
    architecture: str
    minimum_macos_version: str
    packaging_revision: str
    artifact_sha256: str = ""

    @classmethod
    def from_json(
        cls,
        value: object,
        *,
        require_hash: bool = False,
        require_source_sha: bool = True,
    ) -> "ComponentArtifactId":
        if not isinstance(value, Mapping):
            raise ComponentError("CMP-VERIFY-FAILED", "Artifact identity is missing.")

        def text(key: str, limit: int = 256) -> str:
            raw = value.get(key, "")
            return raw[:limit] if isinstance(raw, str) else ""

        identity = cls(
            source_sha=text("source_sha", 40).lower(),
            version=text("version", 64),
            profile=normalize_profile(text("profile")),
            component_set_digest=text("component_set_digest", 64).lower(),
            architecture=_normalize_architecture(text("architecture", 32)),
            minimum_macos_version=text("minimum_macos_version", 32),
            packaging_revision=text("packaging_revision", 64),
            artifact_sha256=text("artifact_sha256", 64).lower(),
        )
        identity.validate(
            require_hash=require_hash,
            require_source_sha=require_source_sha,
        )
        return identity

    def validate(
        self,
        *,
        require_hash: bool = False,
        require_source_sha: bool = True,
    ) -> None:
        if require_source_sha and not _valid_sha(self.source_sha):
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "Artifact source identity is invalid.",
            )
        if not self.version or not self.packaging_revision:
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "Artifact version metadata is incomplete.",
            )
        if not _valid_sha256(self.component_set_digest):
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "Artifact component-set digest is invalid.",
            )
        if not self.architecture or not self.minimum_macos_version:
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "Artifact platform metadata is incomplete.",
            )
        if self.artifact_sha256 and not _valid_sha256(self.artifact_sha256):
            raise ComponentError("CMP-VERIFY-FAILED", "Artifact hash is invalid.")
        if require_hash and not self.artifact_sha256:
            raise ComponentError("CMP-VERIFY-FAILED", "Artifact hash is missing.")

    @property
    def block_key(self) -> str:
        digest = self.artifact_sha256 or self.component_set_digest
        return f"{self.profile}:{self.source_sha}:{digest}"

    def to_json(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class EmbeddedProfile:
    artifact: ComponentArtifactId
    components: tuple[ComponentManifest, ...]

    @classmethod
    def from_json(
        cls,
        value: object,
        *,
        require_source_sha: bool = True,
    ) -> "EmbeddedProfile":
        if not isinstance(value, Mapping):
            raise ComponentError("CMP-VERIFY-FAILED", "Embedded profile metadata is missing.")
        artifact = ComponentArtifactId.from_json(
            value.get("artifact"),
            require_hash=False,
            require_source_sha=require_source_sha,
        )
        raw_components = value.get("components", [])
        if not isinstance(raw_components, list):
            raise ComponentError(
                "CMP-INCOMPATIBLE",
                "Embedded component manifest has an invalid shape.",
            )
        if artifact.profile == CORE_PROFILE:
            components: tuple[ComponentManifest, ...] = ()
            normalized_components: list[dict[str, object]] = []
        else:
            if len(raw_components) != 1:
                raise ComponentError(
                    "CMP-INCOMPATIBLE",
                    "The OneRoster profile must contain exactly one allowlisted component.",
                )
            component = ComponentManifest.from_json(raw_components[0])
            components = (component,)
            normalized_components = [component.to_json()]
        if raw_components != normalized_components:
            raise ComponentError(
                "CMP-INCOMPATIBLE",
                "Embedded component manifest is not canonical.",
            )
        if artifact.component_set_digest != _component_payload_digest(
            normalized_components
        ):
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "Embedded component manifest does not match its artifact digest.",
            )
        return cls(artifact=artifact, components=components)

    def to_json(self) -> dict[str, object]:
        return {
            "artifact": self.artifact.to_json(),
            "components": [item.to_json() for item in self.components],
        }


@dataclass(frozen=True)
class ArtifactEnvelope:
    artifact: ComponentArtifactId
    signing_channel: str = ""
    signing_authority: str = ""

    @classmethod
    def from_json(cls, value: object) -> "ArtifactEnvelope":
        if not isinstance(value, Mapping):
            raise ComponentError("CMP-VERIFY-FAILED", "Artifact sidecar is invalid.")
        channel = _safe_token(value.get("signing_channel", ""), 32)
        if channel not in {"", "local", "developer-id"}:
            raise ComponentError(
                "CMP-INCOMPATIBLE",
                "Artifact signing channel is not supported.",
            )
        authority = value.get("signing_authority", "")
        authority = authority[:256] if isinstance(authority, str) else ""
        return cls(
            artifact=ComponentArtifactId.from_json(
                value.get("artifact"),
                require_hash=True,
            ),
            signing_channel=channel,
            signing_authority=authority,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "artifact": self.artifact.to_json(),
            "signing_channel": self.signing_channel,
            "signing_authority": self.signing_authority,
        }


def build_profile_payload(
    profile: str,
    *,
    source_sha: str,
    version: str,
    architecture: str,
    minimum_macos_version: str,
    packaging_revision: str,
) -> dict[str, object]:
    identity = ComponentArtifactId(
        source_sha=str(source_sha).lower(),
        version=str(version),
        profile=normalize_profile(profile),
        component_set_digest=component_set_digest(profile),
        architecture=_normalize_architecture(architecture),
        minimum_macos_version=str(minimum_macos_version),
        packaging_revision=str(packaging_revision),
    )
    identity.validate()
    return EmbeddedProfile(
        artifact=identity,
        components=manifests_for_profile(profile),
    ).to_json()


def load_embedded_profile(path: Optional[Path] = None) -> EmbeddedProfile:
    """Load and validate the sealed build profile, or a source-tree development profile."""

    metadata = Path(path) if path is not None else _runtime_profile_metadata_path()
    if metadata is not None and metadata.is_file():
        try:
            value = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "The embedded application profile could not be read.",
            ) from exc
        return EmbeddedProfile.from_json(value)

    if getattr(sys, "frozen", False):
        raise ComponentError(
            "CMP-VERIFY-FAILED",
            "The signed application is missing its embedded component profile.",
        )

    # Source runs intentionally expose the full profile so optional-component tests and
    # development do not depend on a generated build artifact.  The environment may
    # select either fixed profile, but it can never supply module paths or code.
    from gamgui import __version__

    profile = normalize_profile(
        os.environ.get("GAMGUI_BUILD_PROFILE"),
        default=ONEROSTER_PROFILE,
    )
    source_sha = os.environ.get("GAMGUI_SOURCE_SHA", "").lower()
    identity = ComponentArtifactId(
        source_sha=source_sha if _valid_sha(source_sha) else "",
        version=__version__,
        profile=profile,
        component_set_digest=component_set_digest(profile),
        architecture=_normalize_architecture(platform.machine()),
        minimum_macos_version="12.0",
        packaging_revision="source",
    )
    identity.validate(require_source_sha=False)
    return EmbeddedProfile(
        artifact=identity,
        components=manifests_for_profile(profile),
    )


def bundle_sha256(bundle: Path) -> str:
    """Hash a bundle tree without following symlinks."""

    root = Path(bundle)
    if not root.is_dir() or root.is_symlink():
        raise ComponentError("CMP-VERIFY-FAILED", "Application bundle is missing.")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + relative + b"\0")
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            digest.update(str(path.stat().st_size).encode("ascii") + b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def write_artifact_sidecar(
    bundle: Path,
    *,
    signing_channel: str = "",
    signing_authority: str = "",
) -> Path:
    """Write the post-signing hash envelope next to a built application bundle."""

    embedded = load_bundle_embedded_profile(bundle)
    artifact = replace(
        embedded.artifact,
        artifact_sha256=bundle_sha256(bundle),
    )
    envelope = ArtifactEnvelope(
        artifact=artifact,
        signing_channel=signing_channel,
        signing_authority=signing_authority,
    )
    sidecar = artifact_sidecar_path(bundle)
    sidecar.write_text(
        json.dumps(envelope.to_json(), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return sidecar


def artifact_sidecar_path(bundle: Path) -> Path:
    path = Path(bundle)
    return path.with_name(path.name + ARTIFACT_SIDECAR_SUFFIX)


def load_bundle_embedded_profile(bundle: Path) -> EmbeddedProfile:
    root = Path(bundle)
    candidates = (
        root / "Contents" / "Resources" / PROFILE_METADATA_RELATIVE,
        root / "Contents" / "Frameworks" / PROFILE_METADATA_RELATIVE,
        root / "Contents" / "MacOS" / "_internal" / PROFILE_METADATA_RELATIVE,
    )
    for candidate in candidates:
        if candidate.is_file():
            return load_embedded_profile(candidate)
    raise ComponentError(
        "CMP-VERIFY-FAILED",
        "Application bundle has no embedded component profile.",
    )


def verify_bundle_artifact(
    bundle: Path,
    *,
    expected_profile: Optional[str] = None,
    expected_artifact: Optional[ComponentArtifactId] = None,
    sidecar: Optional[Path] = None,
) -> ArtifactEnvelope:
    """Verify the embedded allowlist and exact post-signing bundle hash."""

    embedded = load_bundle_embedded_profile(bundle)
    sidecar_path = Path(sidecar) if sidecar is not None else artifact_sidecar_path(bundle)
    try:
        raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ComponentError(
            "CMP-VERIFY-FAILED",
            "Application artifact sidecar is missing or invalid.",
        ) from exc
    envelope = ArtifactEnvelope.from_json(raw)
    actual_hash = bundle_sha256(bundle)
    if envelope.artifact.artifact_sha256 != actual_hash:
        raise ComponentError(
            "CMP-VERIFY-FAILED",
            "Application artifact hash did not match its verified sidecar.",
        )
    embedded_identity = embedded.artifact
    sidecar_identity = replace(envelope.artifact, artifact_sha256="")
    if embedded_identity != sidecar_identity:
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "Application artifact identity does not match its embedded profile.",
        )
    if expected_profile and envelope.artifact.profile != normalize_profile(expected_profile):
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "Application artifact contains the wrong component profile.",
        )
    if expected_artifact and envelope.artifact != expected_artifact:
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "Application artifact does not match the staged updater identity.",
        )
    return envelope


def verify_runtime_compatibility(artifact: ComponentArtifactId) -> None:
    """Fail closed when a macOS artifact targets another architecture or OS."""

    if sys.platform != "darwin":
        return
    current_architecture = _normalize_architecture(platform.machine())
    if artifact.architecture != current_architecture:
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "Application artifact architecture does not match this Mac.",
        )
    current_version = _version_tuple(platform.mac_ver()[0])
    minimum_version = _version_tuple(artifact.minimum_macos_version)
    if not current_version or not minimum_version or current_version < minimum_version:
        raise ComponentError(
            "CMP-INCOMPATIBLE",
            "Application artifact requires a newer version of macOS.",
        )


@dataclass(frozen=True)
class ComponentStatus:
    state: str
    profile: str
    enabled: bool
    error_code: str = ""
    error_message: str = ""
    restart_required: bool = False
    artifact: Optional[dict[str, str]] = None
    installed_components: tuple[str, ...] = field(default_factory=tuple)
    signing_channel: str = ""
    download_size: str = "Allow at least 1 GB of temporary free space"


class ComponentManager:
    """Preference and profile-swap facade used by the core Components UI."""

    def __init__(
        self,
        store: Optional[object] = None,
        coordinator: Optional[object] = None,
        *,
        registry: ActivityRegistry = activity_registry,
        data_root: Optional[Path] = None,
        embedded: Optional[EmbeddedProfile] = None,
    ) -> None:
        if store is None:
            from .updater import UpdateStateStore

            store = UpdateStateStore()
        self.store = store
        self._coordinator = coordinator
        self.registry = registry
        self.data_root = Path(data_root) if data_root is not None else app_data_dir()
        try:
            self.embedded = embedded or load_embedded_profile()
            self._embedded_error: Optional[ComponentError] = None
        except ComponentError as exc:
            self.embedded = None
            self._embedded_error = exc
        self._reconcile_pristine_install()
        self._recover_interrupted_preparation()

    def _recover_interrupted_preparation(self) -> None:
        """Convert a crash-left preparing marker into a retryable stable state."""

        state = self.store.load()
        if (
            str(getattr(state, "component_operation", "") or "") != "preparing"
            or str(getattr(state, "pending_app", "") or "")
        ):
            return
        state.component_operation = ""
        state.component_error_code = "CMP-VERIFY-FAILED"
        state.last_error = (
            "A previous component preparation was interrupted. Retry from "
            "Settings > Components; the installed application was not changed."
        )
        self.store.save(state)

    def _reconcile_pristine_install(self) -> None:
        """Seed updater state when a profile was installed outside the updater.

        A first official/manual installation has no updater state yet. The sealed
        embedded profile is authoritative in that one pristine case; once any
        installed or pending artifact evidence exists, the updater remains the
        sole owner of profile transitions.
        """

        if self.embedded is None or self._embedded_error is not None:
            return
        state = self.store.load()
        installed_sha = str(getattr(state, "installed_sha", "") or "")
        pristine = (
            not installed_sha
            and getattr(state, "installed_artifact", None) is None
            and not getattr(state, "pending_app", "")
        )
        legacy_sha_only = (
            bool(installed_sha)
            and getattr(state, "installed_artifact", None) is None
            and not getattr(state, "pending_app", "")
            and installed_sha == self.embedded.artifact.source_sha
        )
        if not pristine and not legacy_sha_only:
            return
        artifact = self.embedded.artifact
        runtime_bundle = _runtime_bundle_path()
        if getattr(sys, "frozen", False):
            if runtime_bundle is None:
                self._embedded_error = ComponentError(
                    "CMP-VERIFY-FAILED",
                    "The installed application bundle identity could not be resolved.",
                )
                return
            try:
                artifact = replace(
                    artifact,
                    artifact_sha256=bundle_sha256(runtime_bundle),
                )
                artifact.validate(require_source_sha=True, require_hash=True)
            except (ComponentError, OSError):
                self._embedded_error = ComponentError(
                    "CMP-VERIFY-FAILED",
                    "The installed application bundle identity could not be verified.",
                )
                return
        signing_channel, signing_authority = _runtime_signing_identity()
        if getattr(sys, "frozen", False) and (
            not signing_channel or not signing_authority
        ):
            self._embedded_error = ComponentError(
                "CMP-VERIFY-FAILED",
                "The installed application signing identity could not be verified.",
            )
            return
        state.installed_sha = artifact.source_sha
        state.installed_profile = artifact.profile
        state.desired_profile = artifact.profile
        state.installed_components = list(component_ids_for_profile(artifact.profile))
        state.desired_components = list(state.installed_components)
        state.installed_artifact = artifact
        state.installed_signing_channel = signing_channel
        state.installed_signing_authority = signing_authority
        if pristine:
            # A directly installed full profile starts disabled until the
            # first-launch choice explicitly enables it. Core remains unchanged.
            state.enabled_components = []
        else:
            # Older updater state recorded only a SHA. Backfill identity from the
            # currently running sealed bundle without changing the operator's
            # enabled/disabled preference.
            installed = set(state.installed_components)
            state.enabled_components = [
                component
                for component in state.enabled_components
                if component in installed
            ]
        self.store.save(state)

    def committed_runtime_identity_ready(
        self,
        state: Optional[object] = None,
    ) -> bool:
        """Return whether persisted installed identity matches the sealed runtime."""

        if self.embedded is None or self._embedded_error is not None:
            return False
        current = state if state is not None else self.store.load()
        artifact = getattr(current, "installed_artifact", None)
        embedded = self.embedded.artifact
        return bool(
            isinstance(artifact, ComponentArtifactId)
            and getattr(current, "installed_sha", "") == embedded.source_sha
            and getattr(current, "installed_profile", "") == embedded.profile
            and set(getattr(current, "installed_components", ()) or ())
            == set(component_ids_for_profile(embedded.profile))
            and replace(artifact, artifact_sha256="")
            == replace(embedded, artifact_sha256="")
        )

    def reconcile_committed_runtime(self) -> bool:
        """Backfill a legacy updater's SHA-only commit from the sealed bundle."""

        self._reconcile_pristine_install()
        return self.committed_runtime_identity_ready()

    def runtime_projection(self) -> Optional[object]:
        """Return the updater's validated candidate view without persisting it."""

        if (
            self.embedded is None
            or self._embedded_error is not None
            or os.environ.get("GAMGUI_SKIP_UPDATE_ONCE") != "1"
        ):
            return None
        loader = getattr(self.store, "load_runtime_projection", None)
        if not callable(loader):
            return None
        if getattr(sys, "frozen", False):
            runtime = _runtime_bundle_path()
            requested = os.environ.get("GAMGUI_UPDATE_CURRENT_APP", "")
            try:
                if (
                    runtime is None
                    or not requested
                    or runtime.resolve() != Path(requested).resolve()
                ):
                    return None
            except OSError:
                return None
        try:
            return loader(self.embedded, os.environ)
        except (ComponentError, OSError, RuntimeError, ValueError):
            return None

    def activating_artifact(self) -> Optional[ComponentArtifactId]:
        projection = self.runtime_projection()
        return (
            getattr(projection, "installed_artifact", None)
            if projection is not None
            else None
        )

    def status(self) -> ComponentStatus:
        state = self.store.load()
        if self._embedded_error is not None:
            try:
                installed_profile = normalize_profile(
                    getattr(state, "installed_profile", ""),
                    default=CORE_PROFILE,
                )
            except ComponentError:
                installed_profile = CORE_PROFILE
            installed = tuple(
                getattr(state, "installed_components", ()) or ()
            )
            if not installed:
                installed = component_ids_for_profile(installed_profile)
            return ComponentStatus(
                state=ComponentStatusName.DEGRADED.value,
                profile=installed_profile,
                enabled=False,
                error_code=self._embedded_error.error_code,
                error_message=str(self._embedded_error),
                installed_components=installed,
                signing_channel=str(
                    getattr(state, "installed_signing_channel", "") or ""
                ),
            )
        projection = self.runtime_projection()
        activating = (
            getattr(projection, "installed_artifact", None)
            if projection is not None
            else None
        )
        if activating is not None:
            installed = tuple(
                getattr(projection, "installed_components", ()) or ()
            )
            enabled_items = tuple(
                getattr(projection, "enabled_components", ()) or ()
            )
            enabled = (
                ONEROSTER_COMPONENT in installed
                and ONEROSTER_COMPONENT in enabled_items
            )
            if ONEROSTER_COMPONENT not in installed:
                name = ComponentStatusName.NOT_INSTALLED
            elif enabled:
                name = ComponentStatusName.ENABLED
            else:
                name = ComponentStatusName.INSTALLED_DISABLED
            return ComponentStatus(
                state=name.value,
                profile=activating.profile,
                enabled=enabled,
                artifact=activating.to_json(),
                installed_components=installed,
                signing_channel=str(
                    getattr(state, "candidate_signing_channel", "") or ""
                ),
            )
        installed_profile = normalize_profile(
            getattr(state, "installed_profile", ""),
            default=self.embedded.artifact.profile if self.embedded else CORE_PROFILE,
        )
        desired_profile = normalize_profile(
            getattr(state, "desired_profile", ""),
            default=installed_profile,
        )
        installed = tuple(getattr(state, "installed_components", ()) or ())
        if not installed:
            installed = component_ids_for_profile(installed_profile)
        enabled_items = tuple(getattr(state, "enabled_components", ()) or ())
        enabled = ONEROSTER_COMPONENT in installed and ONEROSTER_COMPONENT in enabled_items
        pending_app = str(getattr(state, "pending_app", "") or "")
        candidate_artifact = getattr(state, "candidate_artifact", None)
        operation = str(getattr(state, "component_operation", "") or "")
        error_message = str(getattr(state, "last_error", "") or "")
        error_code = str(getattr(state, "component_error_code", "") or "")

        if pending_app and desired_profile != installed_profile:
            name = ComponentStatusName.RESTART_REQUIRED
        elif operation == "preparing":
            name = ComponentStatusName.PREPARING
        elif error_code:
            name = (
                ComponentStatusName.DEGRADED
                if ONEROSTER_COMPONENT in installed
                else ComponentStatusName.UNAVAILABLE
            )
        elif (
            candidate_artifact is not None
            and candidate_artifact.profile == installed_profile
            and candidate_artifact.source_sha
            != str(getattr(state, "installed_sha", "") or "")
        ):
            name = ComponentStatusName.UPDATE_AVAILABLE
        elif ONEROSTER_COMPONENT not in installed:
            name = ComponentStatusName.NOT_INSTALLED
        elif enabled:
            name = ComponentStatusName.ENABLED
        else:
            name = ComponentStatusName.INSTALLED_DISABLED
        artifact = getattr(state, "installed_artifact", None)
        return ComponentStatus(
            state=name.value,
            profile=installed_profile,
            enabled=enabled,
            error_code=error_code,
            error_message=error_message if error_code else "",
            restart_required=name
            in {
                ComponentStatusName.RESTART_REQUIRED,
                ComponentStatusName.UPDATE_AVAILABLE,
            },
            artifact=artifact.to_json() if artifact is not None else None,
            installed_components=installed,
            signing_channel=str(
                getattr(state, "installed_signing_channel", "") or ""
            ),
        )

    def first_run_choice_pending(self) -> bool:
        return not bool(getattr(self.store.load(), "component_prompt_answered", False))

    def skip_first_run(self) -> ComponentStatus:
        with self.registry.acquire("component-preference"):
            state = self.store.load()
            state.component_prompt_answered = True
            if not getattr(state, "desired_profile", ""):
                state.desired_profile = getattr(state, "installed_profile", CORE_PROFILE)
            state.component_error_code = ""
            state.component_operation = ""
            self.store.save(state)
        return self.status()

    def prepare_install(
        self,
        source_file: Optional[Path] = None,
        *,
        signing_channel_confirmation: str = "",
    ) -> ComponentStatus:
        state = self.store.load()
        if (
            source_file is None
            and self._embedded_error is None
            and normalize_profile(
                getattr(state, "installed_profile", CORE_PROFILE),
                default=CORE_PROFILE,
            )
            == ONEROSTER_PROFILE
            and not getattr(state, "pending_app", "")
        ):
            state.component_prompt_answered = True
            state.desired_profile = ONEROSTER_PROFILE
            state.desired_components = [ONEROSTER_COMPONENT]
            state.enabled_components = [ONEROSTER_COMPONENT]
            state.component_operation = ""
            state.component_error_code = ""
            state.last_error = ""
            self.store.save(state)
            return self.status()
        state.component_operation = "preparing"
        state.component_error_code = ""
        state.last_error = ""
        self.store.save(state)
        try:
            coordinator = self._get_coordinator()
            if source_file is None:
                pending = coordinator.prepare_profile(ONEROSTER_PROFILE)
            else:
                pending = coordinator.prepare_verified_file(
                    Path(source_file),
                    ONEROSTER_PROFILE,
                    official_team_id_confirmation=signing_channel_confirmation,
                )
            if pending is None:
                self._record_coordinator_error()
            else:
                state = self.store.load()
                state.component_prompt_answered = True
                self.store.save(state)
        except ActivityBusyError as exc:
            self._record_error(exc.error_code, str(exc))
        except ComponentError as exc:
            self._record_error(exc.error_code, str(exc))
        except Exception:
            self._record_error(
                "CMP-VERIFY-FAILED",
                "The OneRoster application profile could not be prepared.",
            )
        return self.status()

    def prepare_remove(
        self,
        source_file: Optional[Path] = None,
        *,
        signing_channel_confirmation: str = "",
    ) -> ComponentStatus:
        state = self.store.load()
        if (
            normalize_profile(
                getattr(state, "installed_profile", CORE_PROFILE),
                default=CORE_PROFILE,
            )
            == CORE_PROFILE
            and not getattr(state, "pending_app", "")
        ):
            state.component_prompt_answered = True
            state.desired_profile = CORE_PROFILE
            state.desired_components = []
            state.enabled_components = []
            state.component_operation = ""
            state.component_error_code = ""
            state.last_error = ""
            self.store.save(state)
            return self.status()
        state.component_prompt_answered = True
        state.component_operation = "preparing"
        state.component_error_code = ""
        state.last_error = ""
        self.store.save(state)
        try:
            coordinator = self._get_coordinator()
            if source_file is None:
                pending = coordinator.prepare_profile(CORE_PROFILE)
            else:
                pending = coordinator.prepare_verified_file(
                    Path(source_file),
                    CORE_PROFILE,
                    official_team_id_confirmation=signing_channel_confirmation,
                )
            if pending is None:
                self._record_coordinator_error()
        except ActivityBusyError as exc:
            self._record_error(exc.error_code, str(exc))
        except ComponentError as exc:
            self._record_error(exc.error_code, str(exc))
        except Exception:
            self._record_error(
                "CMP-VERIFY-FAILED",
                "The Core application profile could not be prepared.",
            )
        return self.status()

    def prepare_update(
        self,
        source_file: Path,
        *,
        signing_channel_confirmation: str = "",
    ) -> ComponentStatus:
        """Stage a newer verified release without changing the selected profile."""

        state = self.store.load()
        state.component_operation = "preparing"
        state.component_error_code = ""
        state.last_error = ""
        self.store.save(state)
        try:
            pending = self._get_coordinator().prepare_verified_update_file(
                Path(source_file),
                official_team_id_confirmation=signing_channel_confirmation,
            )
            if pending is None:
                self._record_coordinator_error()
        except ActivityBusyError as exc:
            self._record_error(exc.error_code, str(exc))
        except ComponentError as exc:
            self._record_error(exc.error_code, str(exc))
        except Exception:
            self._record_error(
                "CMP-VERIFY-FAILED",
                "The verified application release could not be prepared.",
            )
        return self.status()

    def enable(self) -> ComponentStatus:
        with self.registry.acquire("component-preference"):
            state = self.store.load()
            installed = set(getattr(state, "installed_components", ()) or ())
            if ONEROSTER_COMPONENT not in installed:
                raise ComponentError(
                    "CMP-NOT-INSTALLED",
                    "Install the OneRoster component before enabling it.",
                )
            state.enabled_components = sorted(
                set(getattr(state, "enabled_components", ()) or ())
                | {ONEROSTER_COMPONENT}
            )
            state.desired_components = [ONEROSTER_COMPONENT]
            state.component_prompt_answered = True
            state.component_error_code = ""
            state.last_error = ""
            self.store.save(state)
        return self.status()

    def disable(self) -> ComponentStatus:
        with self.registry.acquire("component-preference"):
            state = self.store.load()
            state.enabled_components = [
                item
                for item in getattr(state, "enabled_components", ())
                if item != ONEROSTER_COMPONENT
            ]
            state.component_error_code = ""
            state.last_error = ""
            self.store.save(state)
        return self.status()

    def invalidate_scope_readiness(self, domain: str) -> bool:
        """Delete retained OneRoster scope proof without loading optional code.

        Credential replacement is owned by Core and must fail closed even while
        the OneRoster component is absent or disabled.  The retained component
        database is therefore invalidated through this narrow, host-owned schema
        contract instead of relying on a live optional service instance.
        """

        normalized = (domain or "").strip().casefold()
        if not normalized:
            return False
        state_path = self.component_data_root / "state.db"
        _require_component_path(state_path, self.data_root)
        if not state_path.exists():
            return False
        if state_path.is_symlink() or not state_path.is_file():
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "Retained OneRoster access evidence could not be invalidated; "
                "Workspace credentials were not changed.",
            )
        try:
            with closing(sqlite3.connect(str(state_path))) as connection, connection:
                table = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'scope_readiness'
                    """
                ).fetchone()
                if table is None:
                    return False
                result = connection.execute(
                    "DELETE FROM scope_readiness WHERE domain = ?",
                    (normalized,),
                )
        except (OSError, sqlite3.DatabaseError) as exc:
            raise ComponentError(
                "CMP-VERIFY-FAILED",
                "Retained OneRoster access evidence could not be invalidated; "
                "Workspace credentials were not changed.",
            ) from exc
        return result.rowcount > 0

    def data_summary(self) -> dict[str, int]:
        root = self.component_data_root
        files = 0
        total_bytes = 0
        snapshots = 0
        records = 0
        domains: set[str] = set()
        if not root.is_dir() or root.is_symlink():
            return {
                "files": 0,
                "bytes": 0,
                "snapshots": 0,
                "records": 0,
                "domains": 0,
            }
        snapshots_root = root / "snapshots"
        if snapshots_root.is_dir() and not snapshots_root.is_symlink():
            snapshots = sum(1 for item in snapshots_root.iterdir() if item.is_dir())
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                files += 1
                total_bytes += path.stat().st_size
        databases = [root / "state.db"]
        if snapshots_root.is_dir() and not snapshots_root.is_symlink():
            databases.extend(
                path
                for path in snapshots_root.glob("*/normalized.db")
                if path.is_file() and not path.is_symlink()
            )
        allowed_tables = {
            "imports",
            "scope_readiness",
            "threshold_profiles",
            "threshold_overrides",
            "threshold_denials",
            "manifests",
            "manifest_actions",
            "student_gate",
            "accepted_managed_aliases",
            "academic_sessions",
            "orgs",
            "users",
            "courses",
            "classes",
            "enrollments",
            "issues",
            "course_plans",
        }
        for database in databases:
            if not database.is_file() or database.is_symlink():
                continue
            try:
                with closing(
                    sqlite3.connect(f"file:{database}?mode=ro", uri=True)
                ) as connection:
                    tables = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    for table in sorted(tables & allowed_tables):
                        records += int(
                            connection.execute(
                                f'SELECT COUNT(*) FROM "{table}"'
                            ).fetchone()[0]
                        )
                        columns = {
                            str(row[1])
                            for row in connection.execute(
                                f'PRAGMA table_info("{table}")'
                            )
                        }
                        if "domain" in columns:
                            domains.update(
                                str(row[0])
                                for row in connection.execute(
                                    f'SELECT DISTINCT domain FROM "{table}" '
                                    "WHERE domain != ''"
                                )
                            )
            except (OSError, sqlite3.DatabaseError):
                continue
        return {
            "files": files,
            "bytes": total_bytes,
            "snapshots": snapshots,
            "records": records,
            "domains": len(domains),
        }

    def purge_data(self, confirm: str) -> dict[str, int]:
        if confirm != "OneRoster":
            raise ComponentError(
                "CMP-CONFIRMATION-REQUIRED",
                'Type "OneRoster" exactly to purge retained component data.',
            )
        with self.registry.acquire("component-data-purge"):
            summary = self.data_summary()
            root = self.component_data_root
            _require_component_path(root, self.data_root)
            if root.exists():
                if root.is_symlink() or not root.is_dir():
                    raise ComponentError(
                        "CMP-VERIFY-FAILED",
                        "The OneRoster data location is unsafe.",
                    )
                shutil.rmtree(root)
            return summary

    def cleanup_expired_snapshots(
        self,
        *,
        now: Optional[float] = None,
        retention_days: int = DEFAULT_SNAPSHOT_RETENTION_DAYS,
    ) -> list[Path]:
        if retention_days <= 0:
            raise ValueError("Snapshot retention must be positive.")
        snapshots = self.component_data_root / "snapshots"
        _require_component_path(snapshots, self.data_root)
        if not snapshots.is_dir() or snapshots.is_symlink():
            return []
        current = time.time() if now is None else float(now)
        cutoff = current - retention_days * 86400
        removed: list[Path] = []
        known_ids: set[str] = set()
        expired_ids: set[str] = set()
        state_path = self.component_data_root / "state.db"
        if state_path.exists() or state_path.is_symlink():
            if state_path.is_symlink() or not state_path.is_file():
                # An unsafe state path cannot prove which snapshots are tracked.
                # Treat it like an unreadable database and retain everything.
                return []
            try:
                with sqlite3.connect(str(state_path)) as connection:
                    tables = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    if "imports" in tables:
                        known_ids = {
                            str(row[0])
                            for row in connection.execute(
                                "SELECT id FROM imports"
                            )
                            if _safe_snapshot_id(str(row[0]))
                        }
                        expired_ids = {
                            str(row[0])
                            for row in connection.execute(
                                """
                                SELECT id FROM imports
                                WHERE expires_at <= ?
                                """,
                                (current,),
                            )
                            if _safe_snapshot_id(str(row[0]))
                        }
                        connection.executemany(
                            """
                            UPDATE imports SET state = 'expired'
                            WHERE id = ?
                            """,
                            [(import_id,) for import_id in sorted(expired_ids)],
                        )
                        connection.commit()
            except sqlite3.DatabaseError:
                # A failed state read is fail-closed: without a trustworthy list
                # of tracked imports, do not infer expiration from directory age.
                return []
        for import_id in sorted(expired_ids):
            entry = snapshots / import_id
            if entry.is_dir() and not entry.is_symlink():
                try:
                    shutil.rmtree(entry)
                except OSError:
                    # The database already records the snapshot as expired. Leave
                    # material in place so a later host-owned cleanup can retry.
                    continue
                removed.append(entry)
        for entry in snapshots.iterdir():
            if (
                entry.is_dir()
                and not entry.is_symlink()
                and _safe_snapshot_id(entry.name)
                and entry.name not in known_ids
                and entry.stat().st_mtime <= cutoff
            ):
                try:
                    shutil.rmtree(entry)
                except OSError:
                    continue
                removed.append(entry)
        return removed

    @property
    def component_data_root(self) -> Path:
        return self.data_root / "components" / ONEROSTER_COMPONENT

    def _get_coordinator(self) -> object:
        if self._coordinator is None:
            from .updater import UpdateCoordinator

            self._coordinator = UpdateCoordinator(
                store=self.store,
                activity_registry=self.registry,
            )
        return self._coordinator

    def _record_coordinator_error(self) -> None:
        state = self.store.load()
        if getattr(state, "pending_app", ""):
            return
        code = getattr(state, "component_error_code", "") or "CMP-DOWNLOAD-FAILED"
        message = getattr(state, "last_error", "") or (
            "The requested application profile is not currently available."
        )
        self._record_error(code, message)

    def _record_error(self, code: str, message: str) -> None:
        state = self.store.load()
        state.component_operation = ""
        state.component_error_code = str(code)[:64]
        state.last_error = str(message)[:4096]
        self.store.save(state)


def _runtime_profile_metadata_path() -> Optional[Path]:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / PROFILE_METADATA_RELATIVE
    override = os.environ.get("GAMGUI_PROFILE_MANIFEST", "")
    if override and not getattr(sys, "frozen", False):
        return Path(override)
    return None


def _normalize_architecture(value: object) -> str:
    architecture = str(value or "").strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }
    architecture = aliases.get(architecture, architecture)
    if architecture and not all(
        character.isalnum() or character in {"_", "-"}
        for character in architecture
    ):
        return ""
    return architecture[:32]


def _safe_token(value: object, limit: int) -> str:
    token = value[:limit] if isinstance(value, str) else ""
    if not all(character.isalnum() or character in {"_", "-", "."} for character in token):
        return ""
    return token


def _valid_sha(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _safe_snapshot_id(value: str) -> bool:
    return len(value) == 32 and all(
        character in "0123456789abcdef" for character in value
    )


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split(".") if part != "")
    except ValueError:
        return ()


def _runtime_signing_identity() -> tuple[str, str]:
    """Read the sealed running bundle's leaf identity without opening Keychain."""

    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return "", ""
    try:
        bundle = _runtime_bundle_path()
        if bundle is None:
            return "", ""
        verified = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(bundle)],
            check=False,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if verified.returncode != 0:
            return "", ""
        result = subprocess.run(
            ["codesign", "-dv", "--verbose=4", str(bundle)],
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
        output = f"{result.stdout}\n{result.stderr}"
        authority = next(
            (
                line.removeprefix("Authority=").strip()
                for line in output.splitlines()
                if line.startswith("Authority=")
            ),
            "",
        )
        if authority == "GamGUI Local":
            return "local", authority
        if authority.startswith("Developer ID Application: "):
            return "developer-id", authority[:256]
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return "", ""


def _runtime_bundle_path() -> Optional[Path]:
    if not getattr(sys, "frozen", False):
        return None
    try:
        executable = Path(sys.executable).resolve()
        bundle = executable.parents[2]
    except IndexError:
        return None
    return bundle if bundle.suffix == ".app" and bundle.is_dir() else None


def _require_component_path(path: Path, data_root: Path) -> None:
    resolved = Path(path).resolve()
    component_parent = (Path(data_root) / "components").resolve()
    try:
        relative = resolved.relative_to(component_parent)
    except ValueError as exc:
        raise ComponentError(
            "CMP-VERIFY-FAILED",
            "Refusing a component data operation outside GamGUI storage.",
        ) from exc
    if not relative.parts:
        raise ComponentError(
            "CMP-VERIFY-FAILED",
            "Refusing to operate on the component storage root.",
        )
