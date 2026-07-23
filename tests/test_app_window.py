"""Window-sizing math: use the display but always fit it (incl. 13\" Macs)."""

import pytest

from gamgui.app import _active_admin_jobs, _arguments, _fit_size, _handoff_pending_update
from gamgui.core.updater import UpdateState, UpdateStateStore
from gamgui.web.jobs import BatchJob

# (screen_w, screen_h) for displays GamGUI runs on.
SCREENS = [
    (1440, 900),    # 13" MacBook Air (default scaled)
    (1280, 800),    # 13" "more space" off / older default
    (1512, 982),    # 14" MacBook Pro (default scaled)
    (1366, 768),    # small external / older laptop
    (1680, 1050),   # 15"/16" scaled
    (3840, 2160),   # large 4K external
    (2560, 1440),   # 27" external
]


@pytest.mark.parametrize("sw,sh", SCREENS)
def test_window_always_fits_the_screen(sw, sh):
    w, h = _fit_size(sw, sh)
    assert w <= sw and h <= sh                 # never larger than the display (fits 13" Macs)
    assert w >= 900 and h >= 600               # never below the usable minimum
    assert w <= 1600 and h <= 1000             # capped so huge externals don't open absurd


def test_uses_most_of_a_13in_display():
    # On a 13" MBA it should be noticeably bigger than the old fixed 1100×760, not a token bump.
    w, h = _fit_size(1440, 900)
    assert w >= 1300 and h >= 800


def test_caps_on_a_huge_external():
    assert _fit_size(3840, 2160) == (1600, 1000)


def test_cli_modes_are_parsed_without_rejecting_macos_arguments():
    args = _arguments(["--self-test", "--json", "-psn_0_12345"])
    assert args.self_test and args.json_output and not args.canary


def test_active_job_detection_understands_terminal_and_running_states():
    class State:
        jobs = {"done": type("Job", (), {"status": "completed"})()}

    assert not _active_admin_jobs(State())
    State.jobs["running"] = type("Job", (), {"status": "running"})()
    assert _active_admin_jobs(State())


def test_active_job_detection_uses_real_finished_jobs_and_manifest_tasks():
    class State:
        jobs = {"done": BatchJob("done", 1, finished=True)}
        classroom_manifest_tasks = {}
        classroom_manifests = None
        drive_service = None

    assert not _active_admin_jobs(State())
    State.jobs["running"] = BatchJob("running", 1, finished=False)
    assert _active_admin_jobs(State())
    State.jobs.pop("running")

    class PendingTask:
        def done(self):
            return False

    State.classroom_manifest_tasks = {"manifest": PendingTask()}
    assert _active_admin_jobs(State())


def test_pending_update_hands_off_exact_staged_state(monkeypatch, tmp_path):
    data_root = tmp_path / "data"
    pending = data_root / "updates" / "pending" / ("a" * 40) / "GamGUI.app"
    pending_executable = pending / "Contents" / "MacOS" / "GamGUI"
    pending_executable.parent.mkdir(parents=True)
    pending_executable.write_text("candidate", encoding="utf-8")
    current = tmp_path / "Applications" / "GamGUI.app"
    current.mkdir(parents=True)
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(data_root))
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr("gamgui.app.installed_app_path", lambda: current)
    UpdateStateStore().save(
        UpdateState(
            candidate_sha="a" * 40,
            pending_app=str(pending),
            canary_result="passed",
            required_check_evidence=["update-ready"],
        )
    )
    launched = []
    monkeypatch.setattr("gamgui.app.subprocess.Popen", lambda argv, **kwargs: launched.append((argv, kwargs)))

    assert _handoff_pending_update()
    assert launched and "--apply-update-helper" in launched[0][0]
    assert str(pending) in launched[0][0]


def test_incomplete_pending_update_is_blocked_without_handoff(monkeypatch, tmp_path):
    data_root = tmp_path / "data"
    sha = "a" * 40
    pending = data_root / "updates" / "pending" / sha / "GamGUI.app"
    pending.mkdir(parents=True)
    current = tmp_path / "Applications" / "GamGUI.app"
    current.mkdir(parents=True)
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(data_root))
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr("gamgui.app.installed_app_path", lambda: current)
    UpdateStateStore().save(UpdateState(candidate_sha=sha, pending_app=str(pending)))
    launched = []
    monkeypatch.setattr(
        "gamgui.app.subprocess.Popen",
        lambda argv, **kwargs: launched.append((argv, kwargs)),
    )

    assert not _handoff_pending_update()
    state = UpdateStateStore().load()
    assert sha in state.blocked_shas
    assert not state.pending_app and not state.candidate_sha
    assert launched == []


def test_pending_update_without_activation_evidence_is_blocked(monkeypatch, tmp_path):
    data_root = tmp_path / "data"
    sha = "a" * 40
    pending = data_root / "updates" / "pending" / sha / "GamGUI.app"
    executable = pending / "Contents" / "MacOS" / "GamGUI"
    executable.parent.mkdir(parents=True)
    executable.write_text("candidate", encoding="utf-8")
    current = tmp_path / "Applications" / "GamGUI.app"
    current.mkdir(parents=True)
    monkeypatch.setenv("GAMGUI_APP_DATA_DIR", str(data_root))
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr("gamgui.app.installed_app_path", lambda: current)
    UpdateStateStore().save(
        UpdateState(
            candidate_sha=sha,
            pending_app=str(pending),
            canary_result="passed",
        )
    )
    launched = []
    monkeypatch.setattr(
        "gamgui.app.subprocess.Popen",
        lambda argv, **kwargs: launched.append((argv, kwargs)),
    )

    assert not _handoff_pending_update()
    state = UpdateStateStore().load()
    assert sha in state.blocked_shas
    assert "CI or canary evidence" in state.last_error
    assert launched == []
