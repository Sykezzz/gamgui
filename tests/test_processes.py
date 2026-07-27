from __future__ import annotations

from types import SimpleNamespace

from gamgui.core import processes


def test_nonlinux_process_identity_forces_stable_locale_and_timezone(monkeypatch):
    observed = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["env"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout="Thu Jul 23 05:29:03 2026\n",
        )

    monkeypatch.setattr(processes.sys, "platform", "darwin")
    monkeypatch.setattr(processes.subprocess, "run", fake_run)

    identity = processes._posix_process_identity(123)

    assert identity == "posix:Thu Jul 23 05:29:03 2026"
    assert observed["argv"] == ["/bin/ps", "-o", "lstart=", "-p", "123"]
    assert observed["env"]["LC_ALL"] == "C"
    assert observed["env"]["TZ"] == "UTC0"
