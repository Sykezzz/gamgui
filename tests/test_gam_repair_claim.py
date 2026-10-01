"""In-memory GitHub model: no network/files/credentials/processes created."""
import copy
import json
import threading
import unittest
from scripts.gam_repair_claim import claim, finish, Hold, Conflict, TERMINAL_PREFIX, candidate_paths

INCIDENT = "a" * 40
MARKER_SHA = "b" * 40
NONCE1, NONCE2 = "1" * 32, "2" * 32
REPO = {"id": 1309243040, "full_name": "Sykezzz/gamgui", "default_branch": "district-main", "owner": {"id": 81, "login": "Sykezzz"}}

class GitHub:
    def __init__(self):
        self.lock = threading.RLock(); self.barrier = None; self.writes = []
        self.refs = {"district-main": INCIDENT, "automation/gam-repair-request-" + INCIDENT: MARKER_SHA}
        self.metadata = {"schema": 1, "kind": "repair-request", "repository_id": REPO["id"],
                         "workflow_id": 318866333, "run_id": 123, "run_attempt": 1,
                         "head_sha": INCIDENT, "base_branch": "district-main", "max_repair_attempts": 2}
        self.source = {"id": 123, "run_attempt": 1, "workflow_id": 318866333,
                       "path": ".github/workflows/gam-update.yml", "event": "schedule", "status": "completed",
                       "conclusion": "failure", "head_branch": "district-main", "head_sha": INCIDENT,
                       "head_repository": REPO, "updated_at": "2026-10-01T10:00:00Z"}
        self.actor = {"id": 81, "login": "Sykezzz"}
        self.pulls = {700: {"number": 700, "state": "open", "draft": True,
             "title": "[GAM bump repair request] " + INCIDENT,
             "user": {"id": 41898282, "login": "github-actions[bot]", "type": "Bot"},
             "head": {"sha": MARKER_SHA, "ref": "automation/gam-repair-request-" + INCIDENT, "repo": REPO},
             "base": {"ref": "district-main", "repo": REPO}}}
        self.trees = {}; self.commits = {}; self.comments = {}; self.next_sha = 200
        self.fail = None; self.change_source = False; self.bad_diff = False; self.bad_receipt = False

    def sha(self):
        self.next_sha += 1; return f"{self.next_sha:040x}"

    def __call__(self, method, path, data=None, optional=False):
        if method == "POST" and path == "/git/refs" and self.barrier:
            self.barrier.wait(timeout=5)
        with self.lock:
            return copy.deepcopy(self.operate(method, path, data, optional))

    def operate(self, method, path, data, optional):
        if method == "GET":
            if path == "": return REPO
            if path == "/user": return self.actor
            if path == "/actions/workflows/318866333":
                return {"id": 318866333, "path": ".github/workflows/gam-update.yml", "name": "Update GAM7 pin", "state": "active"}
            if path.startswith("/actions/runs/"): return self.source
            if path.startswith("/actions/workflows/318866333/runs?"): return {"workflow_runs": [self.source]}
            if path.startswith("/git/ref/heads/"):
                name = path.removeprefix("/git/ref/heads/")
                return {"object": {"sha": self.refs[name]}} if name in self.refs else None
            if path.startswith("/pulls?"):
                branch = path.split("head=Sykezzz:")[1].split("&")[0]
                return [pull for pull in self.pulls.values() if pull["head"]["ref"] == branch]
            if path.startswith("/pulls/") and "/files?" in path:
                return [{"filename": ".github/repair-requests/gam-bump-" + INCIDENT + ".json", "status": "added"}]
            if path.startswith("/pulls/"): return self.pulls[int(path.split("/")[2])]
            if path.startswith("/issues/"):
                return self.comments.get(int(path.split("/")[2]), [])
            if path.startswith("/contents/"):
                ref = path.split("?ref=")[1]
                if ref == MARKER_SHA: return {"content_json": self.metadata}
                receipt = json.loads(self.commits[ref]["tree"]["tree"][0]["content"])
                if self.bad_receipt: receipt["nonce"] = "f" * 32
                return {"content_json": receipt}
            if path.startswith("/git/commits/"): return {"tree": {"sha": "c" * 40}}
            if path.startswith("/compare/"):
                before, after = path.removeprefix("/compare/").split("...")
                if before == after: return {"status": "identical", "behind_by": 0}
                if before in self.commits: return {"status": "ahead", "behind_by": 0}
                file = self.commits[after]["tree"]["tree"][0]["path"]
                return {"status": "ahead", "ahead_by": 1, "behind_by": 0,
                        "files": [{"filename": "evil.py" if self.bad_diff else file, "status": "added"}]}
            raise AssertionError(path)
        self.writes.append((method, path, copy.deepcopy(data)))
        if self.fail == path + ":before": raise TimeoutError("synthetic unknown result")
        if path == "/git/trees":
            sha = self.sha(); self.trees[sha] = data; return {"sha": sha}
        if path == "/git/commits":
            sha = self.sha(); self.commits[sha] = {"tree": self.trees[data["tree"]], "parents": data["parents"]}; return {"sha": sha}
        if path == "/git/refs":
            branch = data["ref"].removeprefix("refs/heads/")
            if branch in self.refs: raise Conflict("already exists")
            self.refs[branch] = data["sha"]
            if self.fail == path + ":after": raise TimeoutError("accepted ref response lost")
            return {"object": {"sha": data["sha"]}}
        if path == "/pulls":
            number = 800 + len(self.pulls)
            pull = {"number": number, "state": "open", "draft": data["draft"], "user": self.actor,
                    "head": {"ref": data["head"], "sha": self.refs[data["head"]], "repo": REPO},
                    "base": {"ref": data["base"], "repo": REPO}}
            self.pulls[number] = pull
            if self.change_source: self.source["conclusion"] = "success"
            if self.fail == path + ":after": raise TimeoutError("accepted PR response lost")
            return pull
        if path.startswith("/issues/") and method == "POST":
            number = int(path.split("/")[2]); comment = {"id": 100 + len(self.comments), "user": self.actor, "body": data["body"]}
            self.comments.setdefault(number, []).append(comment); return comment
        if path.startswith("/pulls/") and method == "PATCH":
            self.pulls[int(path.split("/")[2])]["state"] = data["state"]
            return self.pulls[int(path.split("/")[2])]
        raise AssertionError((method, path))

class ClaimTests(unittest.TestCase):
    def setUp(self): self.api = GitHub()
    def take(self, nonce=NONCE1): return claim(700, nonce, self.api)

    def test_default_adapter_denies_without_writes(self):
        with self.assertRaises(Hold): claim(700, NONCE1)

    def test_ticket_requires_create_only_ref_and_durable_pr(self):
        ticket = self.take()
        self.assertEqual(ticket.attempt, 1); self.assertTrue(ticket.diagnosis_allowed); self.assertFalse(ticket.merge_allowed)
        self.assertIn(ticket.repair_pr, self.api.pulls)
        self.assertEqual([path for _, path, _ in self.api.writes], ["/git/trees", "/git/commits", "/git/refs", "/pulls"])
        receipt = json.loads(self.api.commits[ticket.receipt_sha]["tree"]["tree"][0]["content"])
        self.assertEqual(receipt["nonce"], NONCE1); self.assertEqual(receipt["actor_id"], self.api.actor["id"])

    def test_duplicate_event_never_resumes_active_slot(self):
        self.take(); before = len(self.api.writes)
        with self.assertRaises(Hold): self.take(NONCE2)
        self.assertEqual(len(self.api.writes), before)

    def test_two_concurrent_workers_only_one_ticket_no_slot_two(self):
        self.api.barrier = threading.Barrier(2); results = []; lock = threading.Lock()
        def worker(nonce):
            try: result = self.take(nonce)
            except Hold: result = "held"
            with lock: results.append(result)
        threads = [threading.Thread(target=worker, args=(nonce,)) for nonce in (NONCE1, NONCE2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=6)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(sum(item == "held" for item in results), 1)
        self.assertEqual(len([name for name in self.api.refs if name.startswith("automation/gam-repair-") and "attempt-" in name]), 1)
        self.assertEqual(len([pull for pull in self.api.pulls.values() if "attempt-" in pull["head"]["ref"]]), 1)

    def test_accepted_ref_lost_response_holds_orphan_and_never_advances(self):
        self.api.fail = "/git/refs:after"
        with self.assertRaises(Hold): self.take()
        self.api.fail = None; before = len(self.api.writes)
        with self.assertRaises(Hold): self.take(NONCE2)
        self.assertEqual(len(self.api.writes), before)

    def test_pr_failures_never_issue_ticket_or_resume(self):
        for failure in ("/pulls:before", "/pulls:after"):
            with self.subTest(failure=failure):
                self.api = GitHub(); self.api.fail = failure
                with self.assertRaises(Hold): self.take()
                self.api.fail = None; before = len(self.api.writes)
                with self.assertRaises(Hold): self.take(NONCE2)
                self.assertEqual(len(self.api.writes), before)

    def test_closed_pr_without_host_stop_record_is_held(self):
        ticket = self.take(); self.api.pulls[ticket.repair_pr]["state"] = "closed"
        before = len(self.api.writes)
        with self.assertRaises(Hold): self.take(NONCE2)
        self.assertEqual(len(self.api.writes), before)

    def test_finish_requires_stopped_task_before_any_write(self):
        ticket = self.take(); before = len(self.api.writes)
        with self.assertRaises(Hold): finish(ticket, "failed", api=self.api)
        self.assertEqual(len(self.api.writes), before)

    def test_two_stopped_failed_attempts_cap_survives_deleted_branches(self):
        first = self.take(); finish(first, "failed", task_stopped=True, api=self.api)
        del self.api.refs[first.branch]
        second = self.take(NONCE2); self.assertEqual(second.attempt, 2)
        finish(second, "rejected", task_stopped=True, api=self.api); del self.api.refs[second.branch]
        before = len(self.api.writes)
        with self.assertRaises(Hold): self.take("3" * 32)
        self.assertEqual(len(self.api.writes), before)

    def test_terminal_foreign_author_or_false_stop_is_rejected(self):
        for bad in ("author", "stop"):
            with self.subTest(bad=bad):
                self.api = GitHub(); ticket = self.take(); finish(ticket, "failed", task_stopped=True, api=self.api)
                comment = self.api.comments[ticket.repair_pr][0]
                if bad == "author": comment["user"] = {"id": 999}
                else:
                    value = json.loads(comment["body"][len(TERMINAL_PREFIX):]); value["task_stopped"] = False
                    comment["body"] = TERMINAL_PREFIX + json.dumps(value)
                with self.assertRaises(Hold): self.take(NONCE2)

    def test_forged_receipt_and_matching_foreign_comment_do_not_reset_budget(self):
        ticket = self.take(); finish(ticket, "failed", task_stopped=True, api=self.api)
        original = self.api.commits[ticket.receipt_sha]["tree"]["tree"][0]
        receipt = json.loads(original["content"]); receipt["actor_id"] = 999
        original["content"] = json.dumps(receipt)
        self.api.comments[ticket.repair_pr][0]["user"] = {"id": 999}
        with self.assertRaises(Hold): self.take(NONCE2)

    def test_foreign_authorized_actor_cannot_claim_owner_incident(self):
        self.api.actor = {"id": 999, "login": "other"}
        with self.assertRaises(Hold): self.take()
        self.assertEqual(self.api.writes, [])

    def test_terminal_replay_does_not_write_again(self):
        ticket = self.take(); finish(ticket, "failed", task_stopped=True, api=self.api)
        before = len(self.api.writes)
        with self.assertRaises(Hold): finish(ticket, "failed", task_stopped=True, api=self.api)
        self.assertEqual(len(self.api.writes), before)

    def test_merged_attempt_cannot_trigger_second(self):
        ticket = self.take(); self.api.pulls[ticket.repair_pr].update(state="closed", merged_at="fixture")
        before = len(self.api.writes)
        with self.assertRaises(Hold): self.take(NONCE2)
        self.assertEqual(len(self.api.writes), before)

    def test_normal_marker_forgery_and_delivery_test_never_claim(self):
        for field, bad in (("kind", "delivery-test"), ("max_repair_attempts", 99), ("repository_id", 5), ("workflow_id", 1)):
            with self.subTest(field=field):
                self.api = GitHub(); self.api.metadata[field] = bad
                with self.assertRaises(Hold): self.take()
                self.assertEqual(self.api.writes, [])

    def test_marker_author_and_source_branch_mismatch_hold_without_writes(self):
        self.api.pulls[700]["user"]["id"] = 999
        with self.assertRaises(Hold): self.take()
        self.assertEqual(self.api.writes, [])
        self.api = GitHub(); self.api.source["head_branch"] = "attacker"
        with self.assertRaises(Hold): self.take()
        self.assertEqual(self.api.writes, [])

    def test_source_resolves_during_claim_ticket_not_issued(self):
        self.api.change_source = True
        with self.assertRaises(Hold): self.take()
        self.assertEqual(len(self.api.pulls), 2)  # Durable consumed slot remains.

    def test_diff_or_receipt_tamper_never_issues_ticket(self):
        for field in ("bad_diff", "bad_receipt"):
            with self.subTest(field=field):
                self.api = GitHub(); setattr(self.api, field, True)
                with self.assertRaises(Hold): self.take()

    def test_final_diff_excludes_claim_metadata_and_security_files(self):
        self.assertEqual(candidate_paths(["scripts/bump_gam.py", "tests/test_bump_gam.py"]), ("scripts/bump_gam.py", "tests/test_bump_gam.py"))
        for path in (".github/repair-attempts/receipt.json", ".github/repair-requests/request.json", ".github/workflows/ci.yml", "unrelated.py"):
            with self.subTest(path=path):
                with self.assertRaises(Hold): candidate_paths(["scripts/bump_gam.py", path])
        with self.assertRaises(Hold): candidate_paths(["scripts/bump_gam.py", "scripts/bump_gam.py"])

    def test_helper_never_deletes_force_updates_or_merges(self):
        ticket = self.take(); finish(ticket, "failed", task_stopped=True, api=self.api)
        self.assertFalse(any(method == "DELETE" or "merge" in path or data.get("force") for method, path, data in self.api.writes))
        self.assertTrue(all(method != "PATCH" or data == {"state": "closed"} for method, _, data in self.api.writes))

if __name__ == "__main__": unittest.main()
