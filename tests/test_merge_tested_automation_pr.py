import json
import subprocess

import pytest

from scripts.merge_tested_automation_pr import GitHubCLI, dispatch_test_and_merge


HEAD = "a" * 40


def test_cli_preserves_successful_json_and_request_input(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '{"merged": true}', "")

    monkeypatch.setattr(subprocess, "run", run)
    payload = {"merge_method": "merge", "sha": HEAD}

    assert GitHubCLI().json(["api", "--input", "-"], payload) == {"merged": True}
    assert calls[0][0] == ["gh", "api", "--input", "-"]
    assert json.loads(calls[0][1]["input"]) == payload


def test_cli_reports_provider_message_and_http_status_without_response_body(monkeypatch):
    response = {
        "message": "Required status check is expected.",
        "status": "405",
        "request_body": "private request data",
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(
            command, 1, json.dumps(response), ""
        ),
    )

    with pytest.raises(RuntimeError) as error:
        GitHubCLI().json(["api", "private-command-argument"], {"sha": HEAD})

    diagnostic = str(error.value)
    assert "HTTP 405" in diagnostic
    assert response["message"] in diagnostic
    assert "private request data" not in diagnostic
    assert "private-command-argument" not in diagnostic
    assert HEAD not in diagnostic
    assert error.value.__context__ is None


def test_cli_sanitizes_and_bounds_stderr_diagnostics(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "private-environment-token")
    payload = {"sha": HEAD}
    stderr = (
        "gh: \x1b[31mMerge blocked: private-environment-token "
        f"{HEAD} github_pat_othercredential Bearer other-auth "
        + "x" * 1000
        + " (HTTP 403)\nprivate trailing stderr"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(command, 1, "not JSON", stderr),
    )

    with pytest.raises(RuntimeError) as error:
        GitHubCLI().json(["api"], payload)

    diagnostic = str(error.value)
    assert "Merge blocked" in diagnostic
    assert "HTTP 403" in diagnostic
    assert "[redacted]" in diagnostic
    assert len(diagnostic) < 550
    assert diagnostic.endswith("...")
    for sensitive in (
        "private-environment-token", HEAD, "github_pat_othercredential",
        "other-auth", "private trailing stderr", "\x1b",
    ):
        assert sensitive not in diagnostic


def test_cli_omits_unstructured_output_that_could_contain_request_data(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(
            command, 1, '{"partial request": "secret', "raw request: secret"
        ),
    )

    with pytest.raises(RuntimeError) as error:
        GitHubCLI().json(["api"])

    assert str(error.value) == (
        "GitHub CLI failed (exit 1): No GitHub error message was returned."
    )


def _run(*, conclusion="success", status="completed", attempt=1, head=HEAD, number=20):
    return {
        "databaseId": number,
        "headSha": head,
        "status": status,
        "conclusion": conclusion,
        "attempt": attempt,
        "url": "https://example.test/pull-request",
    }


class FakeClock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeGitHub:
    def __init__(self, batches=None, *, state="OPEN", changed_head=False):
        self.batches = [[_run()]] if batches is None else batches
        self.state = state
        self.changed_head = changed_head
        self.list_calls = 0
        self.pr_view_calls = 0
        self.runs = []
        self.merge_payload = None

    def run(self, args):
        self.runs.append(args)

    def json(self, args, payload=None):
        if args[:3] == ["run", "list", "--repo"]:
            assert args[args.index("--event") + 1] == "pull_request"
            assert "attempt" in args[-1].split(",")
            batch = self.batches[min(self.list_calls, len(self.batches) - 1)]
            self.list_calls += 1
            return batch
        if args[:2] == ["pr", "view"]:
            self.pr_view_calls += 1
            if self.state == "AUTO_MERGE":
                state = "MERGED" if self.pr_view_calls > 1 else "OPEN"
            else:
                state = self.state
            return {
                "headRefOid": "b" * 40
                if self.changed_head and self.pr_view_calls > 1 else HEAD,
                "state": state,
                "mergeCommit": {"oid": "d" * 40} if state == "MERGED" else None,
            }
        if args[:2] == ["api", "--method"]:
            self.merge_payload = payload
            return {"merged": True, "sha": "c" * 40}
        raise AssertionError(args)


def _merge(gh, *, timeout=10, clock=None):
    clock = clock or FakeClock()
    return dispatch_test_and_merge(
        repository="owner/repo",
        workflow="ci.yml",
        branch="automation/update",
        head_sha=HEAD,
        pull_request="https://github.com/owner/repo/pull/42",
        timeout_seconds=timeout,
        poll_seconds=1,
        gh=gh,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


def test_merges_the_exact_sha_after_eligible_pull_request_ci_passes():
    gh = FakeGitHub()

    assert _merge(gh) == {
        "ci_run_url": "https://example.test/pull-request",
        "merge_sha": "c" * 40,
    }
    assert gh.runs == []
    assert gh.merge_payload == {"merge_method": "merge", "sha": HEAD}


def test_waits_for_delayed_pull_request_indexing_and_ignores_other_heads():
    clock = FakeClock()
    gh = FakeGitHub([[], [_run(head="b" * 40)], [_run()]])

    _merge(gh, clock=clock)

    assert clock.now == 2
    assert gh.runs == []
    assert gh.merge_payload == {"merge_method": "merge", "sha": HEAD}


@pytest.mark.parametrize("timeout,expected_elapsed", [(100, 60), (5, 5)])
def test_missing_pull_request_ci_fails_closed_with_bounded_discovery(timeout, expected_elapsed):
    clock = FakeClock()
    gh = FakeGitHub([[]])

    with pytest.raises(RuntimeError, match="No eligible pull_request"):
        _merge(gh, timeout=timeout, clock=clock)

    assert clock.now == expected_elapsed
    assert gh.runs == []
    assert gh.merge_payload is None


def test_does_not_merge_when_the_exact_sha_pull_request_run_fails():
    gh = FakeGitHub([
        [_run(status="in_progress", conclusion=None)],
        [_run(conclusion="failure")],
    ])

    with pytest.raises(RuntimeError, match="did not pass"):
        _merge(gh)

    assert gh.merge_payload is None


def test_accepts_existing_auto_merge_of_the_same_exact_sha():
    gh = FakeGitHub(state="AUTO_MERGE")

    assert _merge(gh)["merge_sha"] == "d" * 40
    assert gh.merge_payload is None


def test_approves_workflow_and_waits_past_stale_approval_required_result():
    clock = FakeClock()
    pending = [_run(conclusion="action_required")]
    gh = FakeGitHub([pending, pending, [_run(status="queued", conclusion=None)], [_run()]])

    result = _merge(gh, clock=clock)

    assert gh.runs == [[
        "api", "--method", "POST", "repos/owner/repo/actions/runs/20/approve"
    ]]
    assert clock.now == 2
    assert result["ci_run_url"] == "https://example.test/pull-request"
    assert gh.merge_payload == {"merge_method": "merge", "sha": HEAD}


def test_reruns_only_failed_jobs_and_waits_for_the_new_attempt():
    clock = FakeClock()
    failed = [_run(conclusion="failure")]
    gh = FakeGitHub([
        failed, failed,
        [_run(attempt=2, status="queued", conclusion=None)],
        [_run(attempt=2)],
    ])

    _merge(gh, clock=clock)

    assert gh.runs == [["run", "rerun", "20", "--failed", "--repo", "owner/repo"]]
    assert clock.now == 2
    assert gh.merge_payload == {"merge_method": "merge", "sha": HEAD}


def test_stale_failed_attempt_times_out_without_merging():
    clock = FakeClock()
    gh = FakeGitHub([[_run(conclusion="failure")]])

    with pytest.raises(RuntimeError, match="Timed out"):
        _merge(gh, timeout=5, clock=clock)

    assert clock.now == 5
    assert len(gh.runs) == 1
    assert gh.merge_payload is None


def test_does_not_merge_when_head_changes_after_ci():
    gh = FakeGitHub(changed_head=True)

    with pytest.raises(RuntimeError, match="changed after"):
        _merge(gh)

    assert gh.merge_payload is None
