"""Offline security tests of the author-owned inline relay; no GitHub calls."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/gam-repair-request.yml"
text = WORKFLOW.read_text(encoding="utf-8")
source = text.split("          python3 - <<'PY'\n", 1)[1].rsplit("          PY\n", 1)[0]
source = "\n".join(line[10:] for line in source.splitlines())
module = {"__name__": "offline_relay_test"}
exec(compile(source, str(WORKFLOW), "exec"), module)
relay, Rejected = module["relay"], module["Rejected"]
REPO, REPO_ID, WF = module["REPO"], module["REPO_ID"], module["WORKFLOW_ID"]
BASE_SHA = "a" * 40


def repository():
    return {"id": REPO_ID, "full_name": REPO, "default_branch": "district-main"}


def run():
    return {"id": 123, "workflow_id": WF, "path": module["WORKFLOW_PATH"], "event": "schedule",
            "status": "completed", "conclusion": "failure", "head_branch": "district-main",
            "head_repository": repository(), "head_sha": BASE_SHA, "run_attempt": 1,
            "updated_at": "2026-10-01T10:00:00Z"}


class API:
    def __init__(self):
        self.repo = repository(); self.run = run(); self.base = BASE_SHA
        self.runs = [copy.deepcopy(self.run)]; self.refs = {}; self.pulls = {}
        self.writes = []; self.file = None; self.tamper = False; self.bad_diff = False
        self.bad_returned_pull = False; self.default_reads = 0; self.move_base = False
        self.workflow = {"id": WF, "path": module["WORKFLOW_PATH"], "name": "Update GAM7 pin", "state": "active"}

    def __call__(self, method, path, data=None, optional=False):
        if method == "GET":
            if path == "": return copy.deepcopy(self.repo)
            if path == f"/actions/workflows/{WF}": return self.workflow
            if path.startswith("/actions/runs/"): return copy.deepcopy(self.run)
            if path.startswith(f"/actions/workflows/{WF}/runs?"): return {"workflow_runs": self.runs}
            if path == "/git/ref/heads/district-main":
                self.default_reads += 1
                return {"object": {"sha": "f" * 40 if self.move_base and self.default_reads > 1 else self.base}}
            if path.startswith("/git/ref/heads/"):
                name = path.removeprefix("/git/ref/heads/")
                if self.tamper and name.startswith("automation/gam-repair-request-") and name in self.refs:
                    return {"object": {"sha": "e" * 40}}
                return self.refs.get(name)
            if path.startswith("/pulls?"):
                branch = path.split("head=Sykezzz:")[1].split("&")[0]
                return copy.deepcopy(self.pulls.get(branch, []))
            if path.startswith("/git/commits/"): return {"tree": {"sha": "b" * 40}}
            if path.startswith("/compare/"):
                return {"status": "ahead", "ahead_by": 1, "behind_by": 0,
                        "files": [{"filename": "evil.py" if self.bad_diff else self.file, "status": "added"}]}
            raise AssertionError(path)
        self.writes.append((method, path, copy.deepcopy(data)))
        if path == "/git/trees":
            self.file = data["tree"][0]["path"]; return {"sha": "c" * 40}
        if path == "/git/commits": return {"sha": "d" * 40}
        if path == "/git/refs":
            self.refs[data["ref"].removeprefix("refs/heads/")] = {"object": {"sha": data["sha"]}}
            return {}
        if path == "/pulls":
            pull = {"number": 100, "draft": data["draft"], "state": "open",
                    "user": {"login": "github-actions[bot]", "id": 41898282, "type": "Bot"},
                    "head": {"ref": data["head"], "sha": "e" * 40 if self.bad_returned_pull else "d" * 40, "repo": repository()},
                    "base": {"ref": data["base"], "repo": repository()}}
            self.pulls[data["head"]] = [copy.deepcopy(pull)]
            return pull
        raise AssertionError(path)


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.payload = {"repository": repository(), "action": "completed", "workflow_run": run()}

    def emit(self, **kwargs):
        return relay(self.api, self.payload, kwargs.get("event", "workflow_run"),
                     kwargs.get("ref", "refs/heads/district-main"), kwargs.get("repository", REPO))

    def rejected_without_writes(self, **kwargs):
        with self.assertRaises(Rejected): self.emit(**kwargs)
        self.assertEqual(self.api.writes, [])

    def test_valid_request_writes_one_metadata_file_and_draft_only(self):
        result = self.emit(); self.assertEqual(result["result"], "created")
        self.assertEqual([item[1] for item in self.api.writes], ["/git/trees", "/git/commits", "/git/refs", "/pulls"])
        tree = self.api.writes[0][2]; self.assertEqual(len(tree["tree"]), 1)
        metadata = json.loads(tree["tree"][0]["content"])
        self.assertEqual(metadata["max_repair_attempts"], 2)
        self.assertEqual(metadata["kind"], "repair-request")
        self.assertEqual(metadata["run_attempt"], 1)
        self.assertTrue(self.api.writes[-1][2]["draft"])
        self.assertFalse(any("merge" in endpoint for _, endpoint, _ in self.api.writes))

    def test_wrong_repository_ref_and_source_repository_rejected(self):
        self.rejected_without_writes(repository="other/repo")
        self.rejected_without_writes(ref="refs/heads/main")
        self.payload["workflow_run"]["head_repository"]["id"] = 999
        self.rejected_without_writes()

    def test_other_workflow_path_event_branch_status_rejected(self):
        for field, bad in (("workflow_id", 1), ("path", "evil.yml"), ("event", "pull_request"),
                           ("head_branch", "attacker"), ("status", "in_progress"), ("conclusion", "success")):
            with self.subTest(field=field):
                self.payload["workflow_run"] = run(); self.payload["workflow_run"][field] = bad
                self.rejected_without_writes()

    def test_non_completed_event_rejected(self):
        self.payload["action"] = "requested"; self.rejected_without_writes()

    def test_api_run_attempt_mismatch_rejected(self):
        self.api.run["run_attempt"] = 2; self.rejected_without_writes()

    def test_api_workflow_identity_rechecked(self):
        self.api.workflow["path"] = "other.yml"; self.rejected_without_writes()

    def test_authoritative_default_rechecked(self):
        self.api.repo["default_branch"] = "main"; self.rejected_without_writes()

    def test_moved_default_suppresses_stale_failure(self):
        self.api.base = "f" * 40
        self.assertEqual(self.emit()["result"], "suppressed"); self.assertEqual(self.api.writes, [])

    def test_newer_success_including_rerun_of_older_run_suppresses(self):
        success = run(); success.update(id=122, conclusion="success", updated_at="2026-10-01T11:00:00Z")
        self.api.runs.append(success)
        self.assertEqual(self.emit()["result"], "suppressed"); self.assertEqual(self.api.writes, [])

    def test_two_closed_repair_prs_cap_even_when_branches_deleted(self):
        for n in (1, 2):
            branch = f"automation/gam-repair-{BASE_SHA}-attempt-{n}"
            self.api.pulls[branch] = [{"state": "closed", "head": {"ref": branch, "repo": repository()}, "base": {"ref": "district-main"}}]
        self.assertEqual(self.emit()["reason"], "two repair claims exhausted"); self.assertEqual(self.api.writes, [])

    def test_malformed_claim_is_held(self):
        self.api.refs[f"automation/gam-repair-{BASE_SHA}-attempt-1"] = {"object": {"sha": "bad"}}
        self.rejected_without_writes()

    def test_duplicate_run_and_rerun_coalesce_without_writes(self):
        self.emit(); self.api.writes.clear()
        self.assertEqual(self.emit()["result"], "deduplicated")
        self.api.run.update(id=124, run_attempt=2); self.payload["workflow_run"] = copy.deepcopy(self.api.run)
        self.assertEqual(self.emit()["result"], "deduplicated"); self.assertEqual(self.api.writes, [])

    def test_closed_marker_never_reopened(self):
        self.emit(); self.api.writes.clear()
        for pulls in self.api.pulls.values():
            for pull in pulls: pull["state"] = "closed"
        self.assertEqual(self.emit()["result"], "deduplicated"); self.assertEqual(self.api.writes, [])

    def test_stranded_branch_held_not_adopted(self):
        self.api.refs["automation/gam-repair-request-" + BASE_SHA] = {"object": {"sha": "d" * 40}}
        self.assertEqual(self.emit()["result"], "held"); self.assertEqual(self.api.writes, [])

    def test_base_change_before_write_rejected(self):
        self.api.move_base = True; self.rejected_without_writes()

    def test_branch_tamper_and_nonmetadata_diff_stop_before_pr(self):
        for field in ("tamper", "bad_diff"):
            with self.subTest(field=field):
                self.api = API(); setattr(self.api, field, True)
                with self.assertRaises(Rejected): self.emit()
                self.assertFalse(any(endpoint == "/pulls" for _, endpoint, _ in self.api.writes))

    def test_returned_pr_tamper_rejected_for_receiver_hold(self):
        self.api.bad_returned_pull = True
        with self.assertRaises(Rejected): self.emit()

    def test_historical_delivery_test_is_fixed_and_zero_repair(self):
        self.api.run["id"] = module["HISTORICAL_RUN"]; self.api.run["head_sha"] = "f" * 40
        self.payload = {"repository": repository(), "inputs": {"mode": "historical-delivery-test", "run_id": "attacker-ignored"}}
        self.assertEqual(self.emit(event="workflow_dispatch")["kind"], "delivery-test")
        marker = json.loads(self.api.writes[0][2]["tree"][0]["content"])
        self.assertEqual(marker["run_id"], module["HISTORICAL_RUN"]); self.assertEqual(marker["max_repair_attempts"], 0)

    def test_manual_inputs_cannot_enable_live_repair(self):
        self.payload = {"repository": repository(), "inputs": {"mode": "repair"}}
        self.rejected_without_writes(event="workflow_dispatch")

    def test_real_api_client_allows_exact_compare_but_rejects_traversal_and_other_endpoints(self):
        opener = MagicMock(); response = MagicMock()
        response.read.return_value = b'{"status":"ahead"}'
        opener.open.return_value.__enter__.return_value = response
        with patch.object(module["urllib"].request, "build_opener", return_value=opener):
            client = module["api_client"]("synthetic-token")
            suffix = "/compare/" + "a" * 40 + "..." + "b" * 40
            self.assertEqual(client("GET", suffix)["status"], "ahead")
            self.assertEqual(opener.open.call_args.args[0].full_url, "https://api.github.com/repos/" + REPO + suffix)
            for method, bad in (("GET", "/git/ref/heads/../../evil"), ("GET", suffix + "?extra=1"),
                                ("POST", suffix), ("PUT", "/pulls"), ("GET", "/actions/runs/0"),
                                ("GET", "/actions/runs/1#fragment")):
                with self.subTest(method=method, bad=bad):
                    with self.assertRaises(Rejected): client(method, bad)
            self.assertEqual(opener.open.call_count, 1)

    def test_workflow_has_minimum_permissions_no_checkout_artifacts_or_interpolation(self):
        self.assertIn("contents: write\n  pull-requests: write\n  actions: read", text)
        self.assertNotIn("uses:", text); self.assertNotIn("actions/checkout", text)
        self.assertNotIn("${{", source); self.assertNotIn("secrets.", text)
        self.assertIn("github.event.workflow_run.head_sha", text)
        self.assertNotIn("cancel-in-progress: true", text)


if __name__ == "__main__":
    unittest.main()
