from scripts.merge_tested_automation_pr import dispatch_test_and_merge


HEAD = "a" * 40


class FakeGitHub:
    def __init__(self, *, conclusion: str = "success", state: str = "OPEN"):
        self.conclusion = conclusion
        self.state = state
        self.list_calls = 0
        self.runs: list[list[str]] = []
        self.merge_payload = None

    def run(self, args):
        self.runs.append(args)

    def json(self, args, payload=None):
        if args[:3] == ["run", "list", "--repo"]:
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
            return {
                "headRefOid": HEAD,
                "state": self.state,
                "mergeCommit": {"oid": "d" * 40}
                if self.state == "MERGED"
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
    gh = FakeGitHub(state="MERGED")

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
