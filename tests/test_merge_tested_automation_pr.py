from scripts.merge_tested_automation_pr import dispatch_test_and_merge


HEAD = "a" * 40


class FakeGitHub:
    def __init__(
        self,
        *,
        conclusion: str = "success",
        state: str = "OPEN",
        pull_request_approval: bool = False,
    ):
        self.conclusion = conclusion
        self.state = state
        self.pull_request_approval = pull_request_approval
        self.approved = False
        self.list_calls = 0
        self.pr_view_calls = 0
        self.runs: list[list[str]] = []
        self.merge_payload = None

    def run(self, args):
        self.runs.append(args)
        if args[:3] == ["api", "--method", "POST"] and args[-1].endswith(
            "/approve"
        ):
            self.approved = True

    def json(self, args, payload=None):
        if args[:3] == ["run", "list", "--repo"]:
            event = args[args.index("--event") + 1]
            if event == "pull_request":
                if not self.pull_request_approval:
                    return []
                return [
                    {
                        "databaseId": 20,
                        "headSha": HEAD,
                        "status": "completed",
                        "conclusion": "success"
                        if self.approved
                        else "action_required",
                        "url": "https://example.test/pull-request",
                    }
                ]
            self.list_calls += 1
            old = {
                "databaseId": 10,
                "headSha": HEAD,
                "status": "completed",
                "conclusion": "success",
                "url": "https://example.test/old",
            }
            if self.list_calls == 1:
                return [old]
            new = {
                "databaseId": 11,
                "headSha": HEAD,
                "status": "completed",
                "conclusion": self.conclusion,
                "url": "https://example.test/new",
            }
            unrelated = {
                "databaseId": 12,
                "headSha": "b" * 40,
                "status": "completed",
                "conclusion": "success",
                "url": "https://example.test/unrelated",
            }
            return [old, new, unrelated]
        if args[:2] == ["pr", "view"]:
            self.pr_view_calls += 1
            if self.state == "AUTO_MERGE":
                state = "MERGED" if self.pr_view_calls > 1 else "OPEN"
            else:
                state = self.state
            return {
                "headRefOid": HEAD,
                "state": state,
                "mergeCommit": {"oid": "d" * 40}
                if state == "MERGED"
                else None,
            }
        if args[:2] == ["api", "--method"]:
            self.merge_payload = payload
            return {"merged": True, "sha": "c" * 40}
        raise AssertionError(args)


def test_dispatches_new_run_and_merges_the_exact_tested_sha():
    gh = FakeGitHub()
    result = dispatch_test_and_merge(
        repository="owner/repo",
        workflow="ci.yml",
        branch="automation/update",
        head_sha=HEAD,
        pull_request="https://github.com/owner/repo/pull/42",
        timeout_seconds=10,
        poll_seconds=1,
        gh=gh,
        sleep=lambda _: None,
        monotonic=lambda: 0,
    )

    assert gh.runs == [
        [
            "workflow",
            "run",
            "ci.yml",
            "--repo",
            "owner/repo",
            "--ref",
            "automation/update",
        ]
    ]
    assert gh.merge_payload == {"merge_method": "merge", "sha": HEAD}
    assert result == {
        "ci_run_url": "https://example.test/new",
        "merge_sha": "c" * 40,
    }


def test_does_not_merge_when_the_new_exact_sha_run_fails():
    gh = FakeGitHub(conclusion="failure")

    try:
        dispatch_test_and_merge(
            repository="owner/repo",
            workflow="ci.yml",
            branch="automation/update",
            head_sha=HEAD,
            pull_request="42",
            timeout_seconds=10,
            poll_seconds=1,
            gh=gh,
            sleep=lambda _: None,
            monotonic=lambda: 0,
        )
    except RuntimeError as exc:
        assert "did not pass" in str(exc)
    else:
        raise AssertionError("Expected failed CI to prevent the merge.")
    assert gh.merge_payload is None


def test_accepts_existing_auto_merge_of_the_same_exact_sha():
    gh = FakeGitHub(state="AUTO_MERGE")

    result = dispatch_test_and_merge(
        repository="owner/repo",
        workflow="ci.yml",
        branch="automation/update",
        head_sha=HEAD,
        pull_request="42",
        timeout_seconds=10,
        poll_seconds=1,
        gh=gh,
        sleep=lambda _: None,
        monotonic=lambda: 0,
    )

    assert gh.merge_payload is None
    assert result["merge_sha"] == "d" * 40


def test_approves_and_waits_for_the_exact_pull_request_run():
    gh = FakeGitHub(pull_request_approval=True)

    result = dispatch_test_and_merge(
        repository="owner/repo",
        workflow="ci.yml",
        branch="automation/update",
        head_sha=HEAD,
        pull_request="42",
        timeout_seconds=10,
        poll_seconds=1,
        gh=gh,
        sleep=lambda _: None,
        monotonic=lambda: 0,
    )

    assert gh.runs == [
        [
            "api",
            "--method",
            "POST",
            "repos/owner/repo/actions/runs/20/approve",
        ]
    ]
    assert result["ci_run_url"] == "https://example.test/pull-request"
    assert gh.merge_payload == {"merge_method": "merge", "sha": HEAD}
