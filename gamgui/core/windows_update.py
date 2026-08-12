"""Pinned Windows local-build and signing preparation for exact-SHA updates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from .components import (
    ArtifactEnvelope,
    ComponentError,
    artifact_sidecar_path,
    normalize_profile,
    verify_bundle_artifact,
)
from .paths import app_data_dir
from .update_platform import bundle_executable

WINDOWS_TOOLCHAIN_RELATIVE = Path("resources") / "updater" / "windows-toolchain.json"
WINDOWS_SIGNING_SCRIPT_RELATIVE = Path("resources") / "updater" / "windows_local_signing.ps1"
TOOLCHAIN_TIMEOUT_SECONDS = 30 * 60


def default_toolchain_manifest_path() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", "")
    if frozen_root:
        candidate = Path(frozen_root) / WINDOWS_TOOLCHAIN_RELATIVE
        if candidate.is_file():
            return candidate
    return Path(__file__).resolve().parents[1] / WINDOWS_TOOLCHAIN_RELATIVE


def bundled_toolchain_archive_root() -> Optional[Path]:
    frozen_root = getattr(sys, "_MEIPASS", "")
    if not frozen_root:
        return None
    candidate = Path(frozen_root) / "resources" / "updater" / "toolchain"
    return candidate if candidate.is_dir() else None


def default_windows_signing_script_path() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", "")
    if frozen_root:
        candidate = Path(frozen_root) / WINDOWS_SIGNING_SCRIPT_RELATIVE
        if candidate.is_file():
            return candidate
    return Path(__file__).resolve().parents[2] / "scripts" / "windows_local_signing.ps1"


def verify_windows_bundle(
    bundle: Path,
    certificate_sha256: str,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", certificate_sha256.lower()):
        raise RuntimeError("The pinned Windows signing certificate is invalid.")
    run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(default_windows_signing_script_path()),
            "-Action",
            "Verify",
            "-Path",
            str(Path(bundle)),
            "-CertificateSha256",
            certificate_sha256.lower(),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=TOOLCHAIN_TIMEOUT_SECONDS,
    )


def verify_windows_file(
    path: Path,
    certificate_sha256: str,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", certificate_sha256.lower()):
        raise RuntimeError("The pinned Windows signing certificate is invalid.")
    run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(default_windows_signing_script_path()),
            "-Action", "VerifyFile", "-Path", str(Path(path)),
            "-CertificateSha256", certificate_sha256.lower(),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=TOOLCHAIN_TIMEOUT_SECONDS,
    )


@dataclass(frozen=True)
class ToolchainAsset:
    name: str
    version: str
    archive: str
    url: str
    sha256: str
    executable: str
    version_argument: str
    version_contains: str


class WindowsToolchain:
    def __init__(
        self,
        root: Path,
        *,
        manifest_path: Optional[Path] = None,
        bundled_archives: Optional[Path] = None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.root = Path(root)
        self.manifest_path = Path(manifest_path or default_toolchain_manifest_path())
        self.bundled_archives = bundled_archives or bundled_toolchain_archive_root()
        self._run = run
        self.revision, self.assets, self.manifest_digest = self._load_manifest()

    def _load_manifest(self) -> tuple[str, tuple[ToolchainAsset, ...], str]:
        raw = self.manifest_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RuntimeError("The Windows toolchain manifest is invalid.")
        revision = value.get("revision", "")
        records = value.get("assets", [])
        if not isinstance(revision, str) or not revision or not isinstance(records, list):
            raise RuntimeError("The Windows toolchain manifest is incomplete.")
        assets: list[ToolchainAsset] = []
        for record in records:
            if not isinstance(record, dict):
                raise RuntimeError("The Windows toolchain asset record is invalid.")
            asset = ToolchainAsset(**{key: str(record.get(key, "")) for key in ToolchainAsset.__annotations__})
            if (
                not re.fullmatch(r"[a-z0-9-]{2,32}", asset.name)
                or not re.fullmatch(r"[0-9a-f]{64}", asset.sha256)
                or not asset.url.startswith("https://github.com/")
                or PurePosixPath(asset.executable).is_absolute()
                or ".." in PurePosixPath(asset.executable).parts
            ):
                raise RuntimeError("The Windows toolchain asset identity is invalid.")
            assets.append(asset)
        if {item.name for item in assets} != {"mingit", "uv"}:
            raise RuntimeError("The Windows toolchain must pin MinGit and uv exactly once.")
        return revision, tuple(assets), digest

    def ensure(self) -> dict[str, Path]:
        if sys.platform != "win32":
            raise RuntimeError("The Windows toolchain can only run on Windows.")
        revision_root = self.root / self.revision
        downloads = revision_root / "downloads"
        installs = revision_root / "installed"
        downloads.mkdir(parents=True, exist_ok=True)
        installs.mkdir(parents=True, exist_ok=True)
        result: dict[str, Path] = {}
        for asset in self.assets:
            archive = downloads / asset.archive
            if not archive.is_file() or _sha256_file(archive) != asset.sha256:
                archive.unlink(missing_ok=True)
                bundled = (
                    Path(self.bundled_archives) / asset.archive
                    if self.bundled_archives is not None
                    else None
                )
                if bundled is not None and bundled.is_file():
                    if _sha256_file(bundled) != asset.sha256:
                        raise RuntimeError(f"Bundled {asset.name} archive failed its committed SHA-256 pin.")
                    shutil.copy2(bundled, archive)
                else:
                    self._download(asset, archive)
            if _sha256_file(archive) != asset.sha256:
                raise RuntimeError(f"Downloaded {asset.name} archive failed its committed SHA-256 pin.")
            destination = installs / asset.name
            executable = destination / Path(asset.executable)
            if not executable.is_file():
                if destination.exists():
                    shutil.rmtree(destination)
                destination.mkdir(parents=True)
                _extract_zip(archive, destination)
            completed = self._run(
                [str(executable), asset.version_argument],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = f"{completed.stdout}\n{completed.stderr}"
            if asset.version_contains not in output:
                raise RuntimeError(f"Pinned {asset.name} reported an unexpected version.")
            result[asset.name] = executable
        return result

    @staticmethod
    def _download(asset: ToolchainAsset, destination: Path) -> None:
        temporary = destination.with_suffix(destination.suffix + ".download")
        temporary.unlink(missing_ok=True)
        try:
            request = urllib.request.Request(
                asset.url,
                headers={"User-Agent": "GamGUI exact-SHA updater"},
            )
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
            if _sha256_file(temporary) != asset.sha256:
                raise RuntimeError(f"Downloaded {asset.name} archive failed its committed SHA-256 pin.")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


class WindowsLocalUpdateBuilder:
    """Build, sign, verify, and stage one exact update-ready commit."""

    def __init__(
        self,
        root: Optional[Path] = None,
        repository_url: str = "https://github.com/Sykezzz/gamgui.git",
        *,
        signer_thumbprint: str,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        manifest_path: Optional[Path] = None,
        bundled_archives: Optional[Path] = None,
    ) -> None:
        self.root = Path(root or app_data_dir() / "updates")
        self.repository_url = repository_url
        self.signer_thumbprint = signer_thumbprint.lower()
        self._run = run
        self.toolchain = WindowsToolchain(
            self.root / "toolchain",
            manifest_path=manifest_path,
            bundled_archives=bundled_archives,
            run=run,
        )

    def prepare(self, candidate, profile: str, installed_sha: str = "") -> Path:
        if sys.platform != "win32":
            raise RuntimeError("Windows local builds can only run on Windows.")
        if not re.fullmatch(r"[0-9a-f]{64}", self.signer_thumbprint):
            raise RuntimeError("The pinned GamGUI Local certificate SHA-256 is unavailable.")
        profile = normalize_profile(profile)
        tools = self.toolchain.ensure()
        git = tools["mingit"]
        uv = tools["uv"]
        refs = self._command(
            [str(git), "ls-remote", self.repository_url, "refs/heads/district-main", "refs/heads/update-ready"],
            capture=True,
        ).stdout.splitlines()
        resolved = {line.split()[1]: line.split()[0].lower() for line in refs if len(line.split()) == 2}
        if resolved.get("refs/heads/district-main") != candidate.sha or resolved.get("refs/heads/update-ready") != candidate.sha:
            raise RuntimeError("The validated commit no longer matches district-main and update-ready.")

        checkout = self.root / "source" / candidate.sha
        if checkout.exists():
            shutil.rmtree(checkout)
        checkout.parent.mkdir(parents=True, exist_ok=True)
        self._command([str(git), "clone", "--filter=blob:none", "--no-checkout", self.repository_url, str(checkout)])
        self._command([str(git), "-C", str(checkout), "fetch", "--no-tags", "origin", candidate.sha])
        self._command([str(git), "-C", str(checkout), "checkout", "--detach", candidate.sha])
        head = self._command([str(git), "-C", str(checkout), "rev-parse", "HEAD"], capture=True).stdout.strip().lower()
        if head != candidate.sha:
            raise RuntimeError("Updater checkout did not resolve to the validated commit.")
        if re.fullmatch(r"[0-9a-f]{40}", installed_sha) and installed_sha != candidate.sha:
            ancestry = self._command(
                [str(git), "-C", str(checkout), "merge-base", "--is-ancestor", installed_sha, candidate.sha],
                check=False,
            )
            if ancestry.returncode != 0:
                raise RuntimeError("The validated update is not a forward descendant of the installed commit.")

        self._command([str(uv), "python", "install", "3.12"], cwd=checkout)
        self._command([str(uv), "sync", "--frozen", "--python", "3.12", "--extra", "desktop", "--extra", "build"], cwd=checkout)
        self._command([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(checkout / "scripts" / "fetch_gam_windows.ps1"),
        ], cwd=checkout)
        metadata_root = self.root / "metadata" / candidate.sha / profile
        metadata_root.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update({
            "GAMGUI_BUILD_PROFILE": profile,
            "GAMGUI_SOURCE_SHA": candidate.sha,
            "GAMGUI_BUILD_ARCH": "x86_64",
            "GAMGUI_BUILD_PLATFORM": "windows",
            "GAMGUI_BUNDLE_FORMAT": "onedir",
            "GAMGUI_MINIMUM_MACOS": "10.0",
            "GAMGUI_PACKAGING_REVISION": "2-windows-local",
            "GAMGUI_BUILD_METADATA_DIR": str(metadata_root),
            "GAMGUI_SIGNER_THUMBPRINT": self.signer_thumbprint,
            "GAMGUI_TOOLCHAIN_MANIFEST_DIGEST": self.toolchain.manifest_digest,
        })
        self._command([str(uv), "run", "--frozen", "--extra", "desktop", "--extra", "build", "python", "-m", "PyInstaller", "--noconfirm", "--clean", "gamgui.spec"], cwd=checkout, env=environment)
        bundle = checkout / "dist" / "GamGUI"
        if not bundle_executable(bundle, "windows").is_file():
            raise RuntimeError("The update build did not produce GamGUI.exe.")
        signing_script = checkout / "scripts" / "windows_local_signing.ps1"
        self._command([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(signing_script), "-Action", "Sign", "-Path", str(bundle),
            "-CertificateSha256", self.signer_thumbprint,
        ], cwd=checkout)
        self._command([
            str(uv), "run", "--frozen", "--extra", "desktop", "--extra", "build", "python", "-c",
            "import sys; from pathlib import Path; from gamgui.core.components import write_artifact_sidecar; write_artifact_sidecar(Path(sys.argv[1]), signing_channel='local', signing_authority='GamGUI Local')",
            str(bundle),
        ], cwd=checkout)
        self._command([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(signing_script), "-Action", "Verify", "-Path", str(bundle),
            "-CertificateSha256", self.signer_thumbprint,
        ], cwd=checkout)
        smoke_root = Path(tempfile.mkdtemp(prefix="gamgui-update-self-test-", dir=self.root))
        try:
            smoke_env = os.environ.copy()
            smoke_env["GAMGUI_APP_DATA_DIR"] = str(smoke_root / "data")
            self._command([str(bundle_executable(bundle, "windows")), "--self-test", "--json"], cwd=checkout, env=smoke_env)
        finally:
            shutil.rmtree(smoke_root, ignore_errors=True)
        envelope = verify_bundle_artifact(bundle, expected_profile=profile)
        if (
            envelope.artifact.source_sha != candidate.sha
            or envelope.artifact.platform != "windows"
            or envelope.artifact.signer_thumbprint != self.signer_thumbprint
            or envelope.artifact.toolchain_manifest_digest != self.toolchain.manifest_digest
        ):
            raise RuntimeError("The built Windows artifact identity did not match the validated inputs.")
        return self._stage(bundle, envelope)

    def _stage(self, bundle: Path, envelope: ArtifactEnvelope) -> Path:
        pending = self.root / "pending" / envelope.artifact.source_sha / envelope.artifact.profile / "GamGUI"
        if pending.exists():
            shutil.rmtree(pending)
        pending.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundle, pending)
        shutil.copy2(artifact_sidecar_path(bundle), artifact_sidecar_path(pending))
        verify_bundle_artifact(pending, expected_artifact=envelope.artifact)
        return pending

    def _command(self, argv: list[str], *, cwd: Optional[Path] = None, capture: bool = False, check: bool = True, env: Optional[dict[str, str]] = None):
        return self._run(argv, cwd=str(cwd) if cwd else None, check=check, capture_output=capture, text=True, env=env, timeout=TOOLCHAIN_TIMEOUT_SECONDS)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_zip(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        for item in archive.infolist():
            pure = PurePosixPath(item.filename.replace("\\", "/"))
            if pure.is_absolute() or not pure.parts or ".." in pure.parts:
                raise RuntimeError("The Windows toolchain archive contains an unsafe path.")
            target = destination.joinpath(*pure.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source_handle, target.open("wb") as output:
                shutil.copyfileobj(source_handle, output)
