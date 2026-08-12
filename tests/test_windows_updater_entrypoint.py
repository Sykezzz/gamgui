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


def test_detached_helper_launches_through_stable_entrypoint(monkeypatch):
    called = []
    monkeypatch.setattr(windows_updater, "_launch_installed", lambda: called.append(True) or 0)
    monkeypatch.setattr(
        windows_updater.sys,
        "argv",
        ["GamGUIUpdater.exe", "--launch-installed"],
    )

    assert windows_updater.main() == 0
    assert called == [True]


def test_detached_helper_promotes_only_to_validated_path_before_activation(monkeypatch):
    promoted = []
    called = []
    monkeypatch.setattr(windows_updater, "_promote_helper", promoted.append)
    monkeypatch.setattr("gamgui.app.main", lambda arguments: called.append(arguments) or 0)
    arguments = [
        "--apply-update-helper",
        "--promote-helper",
        "C:\\Users\\operator\\AppData\\Local\\GamGUI\\updater\\GamGUIUpdater.exe",
    ]
    monkeypatch.setattr(windows_updater.sys, "argv", ["GamGUIUpdater.exe", *arguments])

    assert windows_updater.main() == 0
    assert promoted == [arguments[-1]]
    assert called == [arguments]
