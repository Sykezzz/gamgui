from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from keyring.errors import PasswordSetError

from gamgui.core.gam.errors import TokenPersistenceError
from gamgui.core.secrets.ephemeral import (
    _LIVE,
    _LIVE_PID_TRUST_SECONDS,
    _PID_FILENAME,
    EphemeralConfig,
    sweep_stale_configs,
    wipe_live_configs,
)
from gamgui.core.secrets.vault import FILENAMES, InMemoryBackend, SecretsVault
from tests.windows_acl_assertions import assert_current_user_only_acl

REPO_ROOT = Path(__file__).resolve().parent.parent


def _dead_pid() -> int:
    """A PID that is certainly not running (spawned, waited on, reaped)."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _make_cfgdir(base: Path, name: str, pid: int | None = None) -> Path:
    d = base / name
    d.mkdir()
    if pid is not None:
        (d / _PID_FILENAME).write_text(str(pid), encoding="utf-8")
    return d


def _backdate(d: Path, seconds: float) -> None:
    when = os.stat(d).st_mtime - seconds
    os.utime(d, (when, when))


def test_materialize_writes_files_with_restrictive_perms(vault, domain, tmp_path):
    with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
        if sys.platform == "win32":
            assert_current_user_only_acl(cfgdir, directory=True)
        else:
            assert (os.stat(cfgdir).st_mode & 0o777) == 0o700
        for fname in FILENAMES.values():
            f = cfgdir / fname
            assert f.exists(), f"{fname} not materialized"
            if sys.platform == "win32":
                assert_current_user_only_acl(f)
            else:
                assert (os.stat(f).st_mode & 0o777) == 0o600
        saved = cfgdir
    # dir wiped after the block
    assert not saved.exists()


def test_owner_pid_marker_is_written_and_does_not_disturb_the_lifecycle(vault, domain, tmp_path):
    with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
        marker = cfgdir / _PID_FILENAME
        assert marker.read_text(encoding="utf-8") == str(os.getpid())
        if sys.platform == "win32":
            assert_current_user_only_acl(marker)
        else:
            assert (os.stat(marker).st_mode & 0o777) == 0o600
        assert os.path.realpath(cfgdir) in _LIVE
        saved = cfgdir
    assert not saved.exists()
    assert os.path.realpath(saved) not in _LIVE


def test_missing_required_credentials_raises(tmp_path):
    empty = SecretsVault(backend=InMemoryBackend())
    with pytest.raises(PermissionError):
        with EphemeralConfig(empty, "x.com", base_dir=tmp_path):
            pass


def test_oauth2_token_write_back(vault, domain, tmp_path):
    # Simulate GAM refreshing the token by rewriting oauth2.txt inside the block.
    with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
        (cfgdir / FILENAMES["oauth2"]).write_text("refreshed-value", encoding="utf-8")
    assert vault.get(domain, "oauth2") == "refreshed-value"


def test_token_write_back_failure_is_typed_private_and_wipes(
    vault,
    domain,
    tmp_path,
    monkeypatch,
):
    refreshed_token = "refreshed-sensitive-token"
    backend_detail = f"Keychain failure for {domain}: OSStatus -25293 ({refreshed_token})"

    def fail_write_back(_domain: str, _name: str, _value: str) -> None:
        raise PasswordSetError(backend_detail)

    monkeypatch.setattr(vault, "set", fail_write_back)

    with pytest.raises(TokenPersistenceError) as caught:
        with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
            saved = cfgdir
            (cfgdir / FILENAMES["oauth2"]).write_text(
                refreshed_token,
                encoding="utf-8",
            )

    error = caught.value
    assert error.error_code == "GAM-TOKEN-PERSISTENCE"
    assert error.command_succeeded is True
    assert error.gam_error_kind is None
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "refreshed authorization could not be saved" in str(error)
    for private_text in (domain, refreshed_token, backend_detail, "OSStatus", "-25293"):
        assert private_text not in str(error)
    assert not saved.exists()
    assert list(tmp_path.glob("gamcfg-*")) == []


def test_token_read_failure_is_typed_private_and_wipes(
    vault,
    domain,
    tmp_path,
    monkeypatch,
):
    original_read_text = Path.read_text

    def fail_token_read(path: Path, *args, **kwargs) -> str:
        if path.name == FILENAMES["oauth2"]:
            raise PermissionError(f"private token read failure for {domain}")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_token_read)

    with pytest.raises(TokenPersistenceError) as caught:
        with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
            saved = cfgdir

    error = caught.value
    assert error.error_code == "GAM-TOKEN-PERSISTENCE"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert domain not in str(error)
    assert "PermissionError" not in str(error)
    assert not saved.exists()
    assert list(tmp_path.glob("gamcfg-*")) == []


def test_token_read_failure_never_masks_body_error(
    vault,
    domain,
    tmp_path,
    monkeypatch,
):
    original_read_text = Path.read_text

    def fail_token_read(path: Path, *args, **kwargs) -> str:
        if path.name == FILENAMES["oauth2"]:
            raise PermissionError("private token read failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_token_read)

    with pytest.raises(RuntimeError, match="body failure") as caught:
        with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
            saved = cfgdir
            raise RuntimeError("body failure")

    assert type(caught.value) is RuntimeError
    assert not saved.exists()
    assert list(tmp_path.glob("gamcfg-*")) == []


@pytest.mark.parametrize(
    "replacement",
    ["missing", "empty", "invalid-utf8"],
)
def test_unusable_token_file_fails_closed_and_wipes(
    vault,
    domain,
    tmp_path,
    replacement,
):
    original = vault.get(domain, "oauth2")

    with pytest.raises(TokenPersistenceError) as caught:
        with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
            saved = cfgdir
            token_file = cfgdir / FILENAMES["oauth2"]
            if replacement == "missing":
                token_file.unlink()
            elif replacement == "empty":
                token_file.write_text("", encoding="utf-8")
            else:
                token_file.write_bytes(b"\xff")

    assert caught.value.error_code == "GAM-TOKEN-PERSISTENCE"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert vault.get(domain, "oauth2") == original
    assert not saved.exists()
    assert list(tmp_path.glob("gamcfg-*")) == []


def test_optional_context_without_original_token_exits_cleanly(tmp_path):
    vault = SecretsVault(backend=InMemoryBackend())

    with EphemeralConfig(
        vault,
        "optional.example",
        require=False,
        base_dir=tmp_path,
    ) as cfgdir:
        saved = cfgdir
        assert not (cfgdir / FILENAMES["oauth2"]).exists()

    assert not saved.exists()
    assert list(tmp_path.glob("gamcfg-*")) == []


def test_optional_context_persists_a_new_token(tmp_path):
    vault = SecretsVault(backend=InMemoryBackend())

    with EphemeralConfig(
        vault,
        "optional.example",
        require=False,
        base_dir=tmp_path,
    ) as cfgdir:
        saved = cfgdir
        (cfgdir / FILENAMES["oauth2"]).write_text(
            "new-authorization",
            encoding="utf-8",
        )

    assert vault.get("optional.example", "oauth2") == "new-authorization"
    assert not saved.exists()
    assert list(tmp_path.glob("gamcfg-*")) == []


def test_token_write_back_failure_never_masks_body_error(
    vault,
    domain,
    tmp_path,
    monkeypatch,
):
    def fail_write_back(_domain: str, _name: str, _value: str) -> None:
        raise PasswordSetError("private backend failure: OSStatus -25293")

    monkeypatch.setattr(vault, "set", fail_write_back)

    with pytest.raises(RuntimeError, match="body failure") as caught:
        with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
            saved = cfgdir
            (cfgdir / FILENAMES["oauth2"]).write_text(
                "refreshed-sensitive-token",
                encoding="utf-8",
            )
            raise RuntimeError("body failure")

    assert type(caught.value) is RuntimeError
    assert not saved.exists()
    assert list(tmp_path.glob("gamcfg-*")) == []


def test_token_write_back_programming_error_surfaces_and_wipes(
    vault,
    domain,
    tmp_path,
    monkeypatch,
):
    def fail_write_back(_domain: str, _name: str, _value: str) -> None:
        raise TypeError("programming defect")

    monkeypatch.setattr(vault, "set", fail_write_back)

    with pytest.raises(TypeError, match="programming defect"):
        with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
            saved = cfgdir
            (cfgdir / FILENAMES["oauth2"]).write_text(
                "refreshed-sensitive-token",
                encoding="utf-8",
            )

    assert not saved.exists()
    assert list(tmp_path.glob("gamcfg-*")) == []


def test_sweep_stale_configs(tmp_path):
    old = _make_cfgdir(tmp_path, "gamcfg-old")
    fresh = _make_cfgdir(tmp_path, "gamcfg-fresh")
    other = tmp_path / "keep-me"
    other.mkdir()
    spool = tmp_path / "spool"
    spool.mkdir()
    stale_private = spool / "gamgui-oneroster-old.batch"
    stale_private.write_text("private", encoding="utf-8")
    fresh_private = spool / "gamgui-oneroster-fresh.txt"
    fresh_private.write_text("private", encoding="utf-8")
    unrelated_private = spool / "keep-private.txt"
    unrelated_private.write_text("keep", encoding="utf-8")
    past = os.stat(old).st_atime - 3600
    os.utime(old, (past, past))  # backdate so it looks orphaned
    os.utime(stale_private, (past, past))

    removed = sweep_stale_configs(base_dir=tmp_path, max_age_seconds=600)
    assert removed == 2
    assert not old.exists()      # orphaned -> swept
    assert fresh.exists()        # too recent, no owner recorded -> kept
    assert other.exists()        # not a gamcfg-* dir -> untouched
    assert not stale_private.exists()
    assert fresh_private.exists()
    assert unrelated_private.exists()


def test_sweep_removes_recent_dir_whose_owner_is_dead(tmp_path):
    dead = _make_cfgdir(tmp_path, "gamcfg-dead", pid=_dead_pid())
    (dead / FILENAMES["oauth2service"]).write_text("secret", encoding="utf-8")

    # Brand new (mtime = now), so only the PID check can catch it.
    assert sweep_stale_configs(base_dir=tmp_path, max_age_seconds=600) == 1
    assert not dead.exists()


def test_sweep_removes_dir_with_dead_owner_however_generous_the_age_rule(tmp_path):
    dead = _make_cfgdir(tmp_path, "gamcfg-dead", pid=_dead_pid())

    # mtime = now and a year-long grace period: only the "owner is gone" rule can justify removal.
    assert sweep_stale_configs(base_dir=tmp_path, max_age_seconds=365 * 86400) == 1
    assert not dead.exists()


def test_sweep_keeps_dir_owned_by_a_live_process(tmp_path):
    live = _make_cfgdir(tmp_path, "gamcfg-live", pid=os.getpid())
    past = os.stat(live).st_atime - 3600
    os.utime(live, (past, past))  # old, but still in use -> the PID wins over the age rule

    # max_age_seconds=0 is the shutdown sweep; it must not shred a concurrent instance's dir.
    assert sweep_stale_configs(base_dir=tmp_path, max_age_seconds=0) == 0
    assert live.exists()


def test_sweep_removes_live_owner_dir_past_the_trust_ceiling(tmp_path):
    """PIDs get recycled, so a live-looking owner only protects a dir for a bounded window."""
    stale = _make_cfgdir(tmp_path, "gamcfg-recycled-pid", pid=os.getpid())
    _backdate(stale, _LIVE_PID_TRUST_SECONDS + 3600)

    # Generous age rule: the ceiling is what removes this, not max_age_seconds.
    assert sweep_stale_configs(base_dir=tmp_path, max_age_seconds=365 * 86400) == 1
    assert not stale.exists()


def test_sweep_falls_back_to_the_age_rule_for_a_corrupt_marker(tmp_path):
    old = _make_cfgdir(tmp_path, "gamcfg-corrupt-old")
    (old / _PID_FILENAME).write_text("not-a-pid\n", encoding="utf-8")
    _backdate(old, 3600)
    fresh = _make_cfgdir(tmp_path, "gamcfg-corrupt-fresh")
    (fresh / _PID_FILENAME).write_text("", encoding="utf-8")

    assert sweep_stale_configs(base_dir=tmp_path, max_age_seconds=600) == 1
    assert not old.exists()
    assert fresh.exists()


def test_sweep_never_touches_a_dir_this_process_is_using(vault, domain, tmp_path):
    with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
        assert sweep_stale_configs(base_dir=tmp_path, max_age_seconds=0) == 0
        assert (cfgdir / FILENAMES["oauth2"]).exists()


def test_wipe_live_configs_cleans_up_when_exit_never_ran(vault, domain, tmp_path):
    cfg = EphemeralConfig(vault, domain, base_dir=tmp_path)
    cfgdir = cfg.__enter__()  # deliberately no __exit__: the daemon-thread-killed case
    assert (cfgdir / FILENAMES["oauth2service"]).exists()

    wipe_live_configs()  # what atexit runs
    assert not cfgdir.exists()
    assert os.path.realpath(cfgdir) not in _LIVE
    wipe_live_configs()  # idempotent


def test_atexit_hook_wipes_dir_on_interpreter_exit(tmp_path):
    """End-to-end: the process exits mid-call and the credentials must not survive it."""
    script = textwrap.dedent(
        """
        import sys
        from gamgui.core.secrets.ephemeral import EphemeralConfig
        from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault

        vault = SecretsVault(backend=InMemoryBackend())
        vault.set_all("example.com", {"oauth2": "tok", "oauth2service": "{}"})
        cfg = EphemeralConfig(vault, "example.com", base_dir=sys.argv[1])
        print(cfg.__enter__(), flush=True)  # never exited — atexit has to clean up
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    leaked = Path(proc.stdout.strip())
    assert leaked.name.startswith("gamcfg-")
    assert not leaked.exists(), "credentials survived interpreter exit"
    assert list(tmp_path.iterdir()) == []


def test_failed_enter_leaves_nothing_on_disk_or_registered(vault, domain, tmp_path, monkeypatch):
    """The caller never gets the path, so it can never call __exit__ — __enter__ must clean up."""
    real_write = EphemeralConfig._write_secret

    def exploding_write(target: Path, value: str) -> None:
        if target.name == FILENAMES["oauth2"]:
            raise KeyboardInterrupt("interrupted mid-materialization")
        real_write(target, value)

    monkeypatch.setattr(EphemeralConfig, "_write_secret", staticmethod(exploding_write))
    before = set(_LIVE)

    cfg = EphemeralConfig(vault, domain, base_dir=tmp_path)
    with pytest.raises(KeyboardInterrupt):
        cfg.__enter__()

    assert list(tmp_path.iterdir()) == []
    assert set(_LIVE) == before
    assert cfg.path is None


def test_dir_wiped_even_on_exception(vault, domain, tmp_path):
    captured = {}
    with pytest.raises(RuntimeError):
        with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
            captured["path"] = cfgdir
            assert cfgdir.exists()
            raise RuntimeError("boom")
    assert not captured["path"].exists()
