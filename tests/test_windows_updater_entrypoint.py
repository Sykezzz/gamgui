from gamgui import windows_updater


def test_detached_helper_rejects_normal_application_launch(monkeypatch):
    monkeypatch.setattr(windows_updater.sys, "argv", ["GamGUIUpdater.exe"])

    assert windows_updater.main() == 2


def test_detached_helper_delegates_only_activation_arguments(monkeypatch):
    called = []
    monkeypatch.setattr(
        "gamgui.app.main",
        lambda arguments: called.append(arguments) or 0,
    )
    arguments = [
        "--apply-update-helper",
        "--candidate-sha",
        "a" * 40,
    ]
    monkeypatch.setattr(
        windows_updater.sys,
        "argv",
        ["GamGUIUpdater.exe", *arguments],
    )

    assert windows_updater.main() == 0
    assert called == [arguments]
