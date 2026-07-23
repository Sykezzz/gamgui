"""Fail-closed local updater for the single managed GamGUI Mac."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .paths import APP_DATA_ENV, app_data_dir

UPDATE_REPOSITORY = "Sykezzz/gamgui"
UPDATE_BRANCH = "district-main"
READY_CHECK = "update-ready"
MAX_RETAINED_BACKUPS = 2
BACKUP_MAX_AGE_DAYS = 30
HEALTH_TIMEOUT_SECONDS = 45.0
LOCAL_SIGNING_IDENTITY = "GamGUI Local"


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
        )


def activation_evidence_valid(state: UpdateState) -> bool:
    """Return whether a staged bundle has both exact-SHA CI and canary evidence."""

    return (
        state.canary_result == "passed"
        and READY_CHECK in state.required_check_evidence
    )


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
    """Resolve one update-ready commit without trusting a moving branch after validation."""

    def __init__(
        self,
        repository: str = UPDATE_REPOSITORY,
        branch: str = UPDATE_BRANCH,
        ready_check: str = READY_CHECK,
        opener: Optional[Callable[..., object]] = None,
    ) -> None:
        self.repository = repository
        self.branch = branch
        self.ready_check = ready_check
        self._opener = opener or urllib.request.urlopen

    def discover(self, installed_sha: str = "", blocked_shas: Iterable[str] = ()) -> Optional[UpdateCandidate]:
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
        with self._opener(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))


class LocalUpdateBuilder:
    """Build and stage an exact commit using the admin Mac's local signing identity."""

    def __init__(
        self,
        root: Optional[Path] = None,
        repository_url: str = "https://github.com/Sykezzz/gamgui.git",
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.root = root or app_data_dir() / "updates"
        self.repository_url = repository_url
        self._run = run

    def prepare(self, candidate: UpdateCandidate) -> Path:
        if sys.platform != "darwin":
            raise RuntimeError("Automatic application builds are supported only on macOS.")
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
        self._command(["make", "app"], cwd=checkout, env=build_env)
        built = checkout / "dist" / "GamGUI.app"
        executable = built / "Contents" / "MacOS" / "GamGUI"
        if not executable.is_file():
            raise RuntimeError("The update build did not produce GamGUI.app.")
        self._command(["codesign", "--verify", "--deep", "--strict", str(built)])
        self._command([str(executable), "--self-test"], cwd=checkout)

        pending = self.root / "pending" / candidate.sha / "GamGUI.app"
        if pending.exists():
            _remove_tree(pending, self.root)
        pending.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(built, pending, symlinks=True)
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
    ) -> None:
        self.store = store or UpdateStateStore()
        self.source = source or GitHubUpdateSource()
        self.builder = builder or LocalUpdateBuilder()
        self.active_jobs = active_jobs

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
        try:
            if self.active_jobs():
                raise RuntimeError("An administrative operation is active; update preparation was deferred.")
            candidate = self.source.discover(state.installed_sha, state.blocked_shas)
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
            pending = self.builder.prepare(candidate)
            if self.active_jobs():
                raise RuntimeError("An administrative operation became active; the update canary was deferred.")
            self.builder.run_canary(pending)
            if self.active_jobs():
                raise RuntimeError("An administrative operation became active; update activation was deferred.")
            state.candidate_sha = candidate.sha
            state.pending_app = str(pending)
            state.canary_result = "passed"
            state.required_check_evidence = list(candidate.successful_checks)
            state.last_error = ""
            self.store.save(state)
            return pending
        except Exception as exc:
            # Preparation failures can be environmental or transient (network, toolchain,
            # certificate, or read-only canary). Only a failed activation/rollback blocklists a
            # SHA; otherwise the same validated commit may be retried after the environment is
            # repaired.
            state.last_error = str(exc)
            self.store.save(state)
            return None

    def mark_installed(self, sha: str) -> None:
        state = self.store.load()
        state.installed_sha = sha
        state.candidate_sha = ""
        state.pending_app = ""
        state.canary_result = ""
        state.required_check_evidence = []
        state.last_error = ""
        self.store.save(state)

    def block(self, sha: str, reason: str) -> None:
        state = self.store.load()
        if sha and sha not in state.blocked_shas:
            state.blocked_shas.append(sha)
        state.candidate_sha = ""
        state.pending_app = ""
        state.canary_result = "failed"
        state.required_check_evidence = []
        state.last_error = reason
        self.store.save(state)

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

        backup = self.root / "backups" / f"{int(self._clock())}-{sha[:12]}"
        database_snapshot = backup / "database"
        migration_copy = backup / "migration-copy"
        backup_app = backup / "GamGUI.app"
        marker = self.root / "health" / f"{sha}.ok"
        incoming = current_app.parent / f".{current_app.name}.{sha[:12]}.incoming"
        previous = current_app.parent / f".{current_app.name}.{sha[:12]}.previous"
        process = None
        swapped = False
        previous_moved = False
        snapshot_ready = False

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

            state = self.store.load()
            state.installed_sha = sha
            state.candidate_sha = ""
            state.pending_app = ""
            state.canary_result = ""
            state.schema_snapshot = str(database_snapshot)
            state.last_error = ""
            state.required_check_evidence = []
            state.retained_rollbacks.insert(0, str(backup))
            state.retained_rollbacks = state.retained_rollbacks[:MAX_RETAINED_BACKUPS]
            self.store.save(state)
            # State persistence is the activation commit point. Cleanup after this point is
            # best-effort and must never roll back a healthy application.
            swapped = False
            previous_moved = False
            self._best_effort_remove(previous, current_app.parent)
            self._best_effort_remove(pending_app, self.root)
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
        if sha in state.blocked_shas:
            raise ValueError("The candidate SHA is blocked.")
        if not activation_evidence_valid(state):
            raise ValueError("The candidate lacks required CI or canary evidence.")
        _require_within(pending_app, self.root)
        if not pending_app.is_dir() or not (pending_app / "Contents" / "MacOS" / "GamGUI").is_file():
            raise ValueError("The staged application bundle is incomplete.")
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

    def _block(
        self,
        sha: str,
        reason: str,
        database_snapshot: Optional[Path],
    ) -> None:
        state = self.store.load()
        if sha not in state.blocked_shas:
            state.blocked_shas.append(sha)
        state.candidate_sha = ""
        state.pending_app = ""
        state.canary_result = "failed"
        state.schema_snapshot = (
            str(database_snapshot)
            if database_snapshot is not None and database_snapshot.is_dir()
            else ""
        )
        state.required_check_evidence = []
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
    return paths


def bundle_self_test(data_root: Optional[Path] = None, *, require_gam: bool = True) -> dict[str, object]:
    """Offline bundle and copied-database integrity check without tenant access."""
    from ..web import server
    from .gam.runner import locate_gam_binary

    failures: list[str] = []
    for relative in ("templates/base.html", "templates/index.html", "static"):
        if not (server._WEB_DIR / relative).exists():
            failures.append(f"missing web asset: {relative}")
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


def _valid_sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdef" for char in value.lower())


def _owner_only(path: Path) -> None:
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _owner_only_directory(path: Path) -> None:
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


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
