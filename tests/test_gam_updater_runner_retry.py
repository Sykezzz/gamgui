"""Offline allocation-failure classification and bounded GitHub client tests."""

import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/gam-updater-runner-retry.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
SOURCE = TEXT.split("          python3 - <<'PY'\n", 1)[1].rsplit("          PY\n", 1)[0]
SOURCE = "\n".join(line[10:] for line in SOURCE.splitlines())
MODULE = {"__name__": "offline_runner_retry_test"}
exec(compile(SOURCE, str(WORKFLOW), "exec"), MODULE)
retry, Held, api_client = MODULE["retry"], MODULE["Held"], MODULE["api_client"]
REPO, REPO_ID, WF = MODULE["REPO"], MODULE["REPO_ID"], MODULE["WORKFLOW_ID"]
HEAD = "a" * 40


def repository():
    return {"id": REPO_ID, "full_name": REPO, "default_branch": "district-main"}


def run():
    return {"id": 123, "workflow_id": WF, "path": MODULE["WORKFLOW_PATH"],
            "event": "schedule", "status": "completed", "conclusion": "failure",
            "head_branch": "district-main", "head_repository": repository(),
            "head_sha": HEAD, "run_attempt": 1, "created_at": "2026-11-03T12:43:00Z",
            "updated_at": "2026-11-03T12:58:00Z"}


class API:
    def __init__(self):
        self.repo, self.run, self.head = repository(), run(), HEAD
        self.workflow = {"id": WF, "path": MODULE["WORKFLOW_PATH"],
                         "name": "Update GAM7 pin", "state": "active"}
        self.jobs = {"total_count": 1, "jobs": [{"id": 456, "run_id": 123,
                     "run_attempt": 1, "head_sha": HEAD, "name": "bump",
                     "labels": ["macos-latest"], "status": "completed",
                     "conclusion": "cancelled", "steps": [], "runner_id": 0}]}
        self.annotations = [{"annotation_level": "failure", "message": MODULE["ALLOCATION_ERROR"]}]
        self.runs = {"total_count": 1, "workflow_runs": [run()]}
        self.writes, self.reads = [], []
        self.move_head = self.new_attempt = self.new_run = False

    def __call__(self, method, path):
        if method == "POST":
            self.writes.append((method, path))
            self.run["run_attempt"] += 1
            self.run["status"] = "queued"
            self.run["conclusion"] = None
            return None
        self.reads.append(path)
        if path == "": return copy.deepcopy(self.repo)
        if path == f"/actions/workflows/{WF}": return copy.deepcopy(self.workflow)
        if path == "/actions/runs/123":
            value = copy.deepcopy(self.run)
            if self.new_attempt and self.reads.count(path) == 2:
                value["run_attempt"] += 1
            return value
        if path == "/git/ref/heads/district-main":
            return {"object": {"sha": "f" * 40 if self.move_head and self.reads.count(path) == 2 else self.head}}
        if path.startswith(f"/actions/workflows/{WF}/runs?"):
            value = copy.deepcopy(self.runs)
            if self.new_run and self.reads.count(path) == 2:
                newer = run(); newer.update(id=124, created_at="2026-11-03T13:00:00Z", updated_at="2026-11-03T13:00:00Z")
                value = {"total_count": 2, "workflow_runs": [newer, run()]}
            return value
        if "/attempts/" in path: return copy.deepcopy(self.jobs)
        if path == "/check-runs/456/annotations?per_page=100": return copy.deepcopy(self.annotations)
        raise AssertionError(path)


class RunnerRetryTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.payload = {"repository": repository(), "action": "completed", "workflow_run": run()}

    def emit(self, **kwargs):
        return retry(self.api, self.payload, kwargs.get("event", "workflow_run"),
                     kwargs.get("ref", "refs/heads/district-main"), kwargs.get("repository", REPO))

    def rejected(self, **kwargs):
        with self.assertRaises(Held): self.emit(**kwargs)
        self.assertEqual(self.api.writes, [])

    def test_allocation_failure_retries_only_failed_jobs_once(self):
        self.assertEqual(self.emit()["result"], "retry requested")
        self.assertEqual(self.api.writes, [("POST", "/actions/runs/123/rerun-failed-jobs")])

    def test_supported_intel_pool_allocation_failure_also_retried(self):
        self.api.jobs["jobs"][0]["labels"] = ["macos-26-intel"]
        self.assertEqual(self.emit()["result"], "retry requested")
        self.assertEqual(self.api.writes, [("POST", "/actions/runs/123/rerun-failed-jobs")])

    def test_mixed_or_unrelated_macos_pool_rejected(self):
        for labels in (["macos-latest", "macos-26-intel"], ["macos-15"], ["macos-26"]):
            with self.subTest(labels=labels):
                self.api = API(); self.api.jobs["jobs"][0]["labels"] = labels
                self.rejected()

    def test_second_attempt_allowed_but_third_held(self):
        for target in (self.payload["workflow_run"], self.api.run, self.api.jobs["jobs"][0]):
            target["run_attempt"] = 2
        self.emit()
        self.api.writes.clear()
        self.payload["workflow_run"] = copy.deepcopy(self.api.run)
        self.payload["workflow_run"].update(status="completed", conclusion="failure")
        self.api.run = copy.deepcopy(self.payload["workflow_run"])
        self.assertEqual(self.emit()["reason"], "two retries exhausted")
        self.assertEqual(self.api.writes, [])

    def test_duplicate_completion_after_accepted_rerun_rejected(self):
        self.emit(); self.api.writes.clear()
        self.rejected()

    def test_event_repository_ref_and_action_validated(self):
        self.rejected(event="workflow_dispatch")
        self.rejected(ref="refs/heads/main")
        self.rejected(repository="other/repo")
        self.payload["action"] = "requested"; self.rejected()

    def test_source_workflow_repository_event_and_status_validated(self):
        for field, bad in (("id", "123"), ("workflow_id", 1), ("path", "other.yml"),
                           ("event", "pull_request"), ("head_branch", "main"),
                           ("status", "in_progress"), ("conclusion", "cancelled"),
                           ("head_sha", "bad"), ("run_attempt", True)):
            with self.subTest(field=field):
                self.payload["workflow_run"] = run(); self.payload["workflow_run"][field] = bad
                self.rejected()
        self.payload["workflow_run"] = run()
        self.payload["workflow_run"]["head_repository"]["id"] = 999; self.rejected()

    def test_authoritative_source_and_workflow_rechecked(self):
        self.api.run["run_attempt"] = 2; self.rejected()
        self.api = API(); self.api.workflow["state"] = "disabled_manually"; self.rejected()
        self.api = API(); self.api.repo["default_branch"] = "main"; self.rejected()

    def test_default_move_or_new_attempt_during_checks_prevents_retry(self):
        for field in ("move_head", "new_attempt", "new_run"):
            with self.subTest(field=field):
                self.api = API(); setattr(self.api, field, True); self.rejected()

    def test_newer_run_or_rerun_of_older_run_prevents_stale_retry(self):
        for overrides in ({"id": 124, "created_at": "2026-11-03T13:00:00Z"},
                          {"id": 122, "created_at": "2026-11-02T12:00:00Z", "updated_at": "2026-11-03T13:00:00Z"}):
            with self.subTest(overrides=overrides):
                self.api = API(); other = run(); other.update(overrides)
                self.api.runs = {"total_count": 2, "workflow_runs": [other, run()]}
                self.rejected()

    def test_bounded_run_listing_and_source_presence_required(self):
        for listing in ({"total_count": 100, "workflow_runs": [run()] * 100},
                        {"total_count": 101, "workflow_runs": [run()]},
                        {"total_count": 0, "workflow_runs": []}):
            with self.subTest(listing_size=len(listing["workflow_runs"])):
                self.api = API(); self.api.runs = listing; self.rejected()

    def test_normal_job_failure_or_any_executed_step_never_retried(self):
        for field, bad in (("name", "test"), ("steps", [{"name": "Set up job"}]),
                           ("runner_id", 7), ("labels", ["ubuntu-24.04"]),
                           ("run_attempt", 2), ("head_sha", "b" * 40),
                           ("run_id", 999), ("status", "in_progress"), ("conclusion", "success")):
            with self.subTest(field=field):
                self.api = API(); self.api.jobs["jobs"][0][field] = bad; self.rejected()
        self.api = API(); self.api.jobs["total_count"] = 2; self.rejected()

    def test_exact_failure_annotation_required(self):
        for annotations in ([], [{"annotation_level": "failure", "message": "Tests failed"}],
                            [{"annotation_level": "warning", "message": MODULE["ALLOCATION_ERROR"]}],
                            [{"annotation_level": "failure", "message": MODULE["ALLOCATION_ERROR"] + " extra"}],
                            self.api.annotations * 100):
            with self.subTest(annotations=len(annotations)):
                self.api = API(); self.api.annotations = annotations; self.rejected()

    def test_uncertain_post_is_not_retried(self):
        real_api = self.api
        def uncertain(method, path):
            value = real_api(method, path)
            if method == "POST": raise Held("transport outcome uncertain")
            return value
        self.api = uncertain
        with self.assertRaises(Held): self.emit()
        self.assertEqual(real_api.writes, [("POST", "/actions/runs/123/rerun-failed-jobs")])

    def test_workflow_permissions_trigger_and_no_source_execution(self):
        self.assertIn("contents: read\n  checks: read\n  actions: write\n", TEXT)
        self.assertIn("runs-on: ubuntu-24.04", TEXT)
        self.assertIn("types: [completed]", TEXT)
        self.assertIn("group: gam-updater-runner-retry-${{ github.event.workflow_run.id }}", TEXT)
        self.assertNotIn("uses:", TEXT)
        self.assertNotIn("workflow_dispatch:", TEXT)
        self.assertNotIn("pull-requests:", TEXT)
        self.assertNotIn("secrets.", TEXT)


class GitHubClientTests(unittest.TestCase):
    def setUp(self):
        self.opener = MagicMock()
        self.response = MagicMock()
        self.opener.open.return_value.__enter__.return_value = self.response

    def client(self):
        with patch.dict(MODULE["urllib"].request.__dict__, {"build_opener": lambda *args: self.opener}):
            return api_client("test-secret")

    def test_fixed_origin_and_no_redirect_handler(self):
        self.response.read.return_value = b'{"ok":true}'
        self.assertEqual(self.client()("GET", ""), {"ok": True})
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/Sykezzz/gamgui")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 15)
        self.assertIsNone(MODULE["NoRedirect"]().redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_unlisted_or_injected_endpoint_stops_before_request(self):
        api = self.client()
        for method, path in (("POST", "/pulls/117/merge"), ("GET", "/actions/runs/123/../secrets"),
                             ("POST", "/actions/runs/123/rerun-failed-jobs?extra=1")):
            with self.assertRaises(Held): api(method, path)
        self.opener.open.assert_not_called()

    def test_oversized_response_and_invalid_json_stop(self):
        api = self.client()
        self.response.read.return_value = b"x" * (MODULE["MAX_BYTES"] + 1)
        with self.assertRaises(Held): api("GET", "")
        self.assertEqual(self.response.read.call_args.args[0], MODULE["MAX_BYTES"] + 1)
        self.response.read.return_value = b"invalid JSON"
        with self.assertRaises(ValueError): api("GET", "")

    def test_http_and_network_errors_are_redacted_and_not_retried(self):
        api = self.client()
        for error in (OSError("test-secret"), urllib.error.HTTPError("url", 403, "test-secret", {}, io.BytesIO(b"test-secret"))):
            with self.subTest(error=type(error).__name__):
                self.opener.open.reset_mock(); self.opener.open.side_effect = error
                with self.assertRaises(Held) as caught: api("POST", "/actions/runs/123/rerun-failed-jobs")
                self.assertNotIn("test-secret", str(caught.exception))
                self.assertEqual(self.opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
