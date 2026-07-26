from types import SimpleNamespace

from gamgui.app import _allow_window_close, _start_update_preparation


def _state(*, active: bool):
    task = SimpleNamespace(done=lambda: not active)
    return SimpleNamespace(
        jobs={},
        classroom_manifest_tasks={"job": task} if active else {},
        oneroster_manifest_tasks={},
        classroom_manifests=None,
        drive_service=None,
        oneroster_service=None,
    )


def test_window_close_is_refused_with_visible_notice_during_active_job():
    notices = []

    assert not _allow_window_close(_state(active=True), notices.append)
    assert notices and "administrative operation" in notices[0]


def test_window_close_is_allowed_when_all_jobs_are_terminal():
    assert _allow_window_close(_state(active=False))


def test_first_launch_component_choice_defers_automatic_update_and_keychain(
    monkeypatch,
    tmp_path,
):
    started = []
    manager = SimpleNamespace(
        first_run_choice_pending=lambda: True,
        store=SimpleNamespace(
            load=lambda: SimpleNamespace(component_operation="")
        ),
    )
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr(
        "gamgui.app.installed_app_path",
        lambda: tmp_path / "GamGUI.app",
    )
    monkeypatch.setattr(
        "gamgui.app.threading.Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs)),
    )

    _start_update_preparation(
        SimpleNamespace(component_manager=manager)
    )

    assert started == []


def test_component_preparation_defers_automatic_update(monkeypatch, tmp_path):
    started = []
    manager = SimpleNamespace(
        first_run_choice_pending=lambda: False,
        store=SimpleNamespace(
            load=lambda: SimpleNamespace(component_operation="preparing")
        ),
    )
    monkeypatch.setattr("gamgui.app.sys.platform", "darwin")
    monkeypatch.setattr(
        "gamgui.app.installed_app_path",
        lambda: tmp_path / "GamGUI.app",
    )
    monkeypatch.setattr(
        "gamgui.app.threading.Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs)),
    )

    _start_update_preparation(
        SimpleNamespace(component_manager=manager)
    )

    assert started == []
