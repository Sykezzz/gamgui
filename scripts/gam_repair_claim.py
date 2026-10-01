"""Create-only two-attempt claim protocol for an ordinary isolated coding task.

No network, token, shell, service or account implementation. The caller supplies
existing authorized GitHub operations as REST-shaped snapshots. Default denies.
Only an atomic POST git/refs winner may receive a ticket; no resume/retry.
"""
from dataclasses import dataclass
import json
from datetime import datetime
import re

REPO = "Sykezzz/gamgui"
REPO_ID = 1309243040
WORKFLOW_ID = 318866333
BASE = "district-main"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX32 = re.compile(r"[0-9a-f]{32}")

class Hold(Exception):
    """Stop without diagnosis, fallback, ref reset or deletion."""

class Conflict(Exception):
    """Adapter maps ONLY atomic create-ref already-exists to this type."""

def need(value, message):
    if not value:
        raise Hold(message)

def sha(value):
    need(isinstance(value, str) and HEX40.fullmatch(value), "invalid SHA")
    return value

def repository(value):
    return isinstance(value, dict) and value.get("id") == REPO_ID and value.get("full_name") == REPO

@dataclass(frozen=True)
class Request:
    marker_number: int
    marker_sha: str
    incident_sha: str
    run_id: int
    run_attempt: int
    owner_id: int

    @classmethod
    def validated(cls, marker, metadata, files, source_run, current_default, owner_id):
        # Inputs must be re-fetched authoritative snapshots, never webhook body alone.
        need(type(owner_id) is int and owner_id > 0, "trusted repository owner identity")
        expected = {"schema", "kind", "repository_id", "workflow_id", "run_id", "run_attempt",
                    "head_sha", "base_branch", "max_repair_attempts"}
        need(isinstance(metadata, dict) and set(metadata) == expected, "marker schema")
        need(metadata["schema"] == 1 and metadata["kind"] == "repair-request"
             and metadata["repository_id"] == REPO_ID and metadata["workflow_id"] == WORKFLOW_ID
             and metadata["base_branch"] == BASE and metadata["max_repair_attempts"] == 2, "normal request only")
        incident = sha(metadata["head_sha"])
        need(type(metadata["run_id"]) is int and 0 < metadata["run_id"] <= 9223372036854775807
             and type(metadata["run_attempt"]) is int and 1 <= metadata["run_attempt"] <= 100, "run/attempt")
        need(type(marker.get("number")) is int and marker["number"] > 0
             and marker.get("state") == "open" and marker.get("draft") is True, "open draft marker")
        need(marker.get("user", {}).get("id") == 41898282
             and marker["user"].get("login") == "github-actions[bot]"
             and marker["user"].get("type") == "Bot", "marker provenance")
        need(repository(marker.get("head", {}).get("repo")) and repository(marker.get("base", {}).get("repo"))
             and marker["base"].get("ref") == BASE
             and marker["head"].get("ref") == "automation/gam-repair-request-" + incident
             and marker.get("title") == "[GAM bump repair request] " + incident, "marker identity")
        marker_sha = sha(marker["head"].get("sha"))
        path = ".github/repair-requests/gam-bump-" + incident + ".json"
        need(isinstance(files, list) and len(files) == 1 and files[0].get("filename") == path
             and files[0].get("status") == "added", "metadata-only marker")
        need(source_run.get("id") == metadata["run_id"] and source_run.get("run_attempt") == metadata["run_attempt"]
             and source_run.get("workflow_id") == WORKFLOW_ID
             and source_run.get("path") == ".github/workflows/gam-update.yml"
             and source_run.get("event") in ("schedule", "workflow_dispatch")
             and source_run.get("status") == "completed" and source_run.get("conclusion") == "failure"
             and source_run.get("head_branch") == BASE and source_run.get("head_sha") == incident
             and repository(source_run.get("head_repository")), "source no longer matches failure")
        need(sha(current_default) == incident, "default moved")
        return cls(marker["number"], marker_sha, incident, metadata["run_id"], metadata["run_attempt"], owner_id)

@dataclass(frozen=True)
class Ticket:
    attempt: int
    branch: str
    receipt_sha: str
    repair_pr: int
    nonce: str
    actor_id: int
    merge_allowed: bool = False
    diagnosis_allowed: bool = True


def denied(*args, **kwargs):
    raise Hold("authorized GitHub adapter not supplied; execution disabled")


def load_request(api, marker_number):
    need(type(marker_number) is int and 0 < marker_number <= 9223372036854775807, "marker number")
    repo = api("GET", "")
    need(repository(repo) and repo.get("default_branch") == BASE, "repository default")
    owner = repo.get("owner", {})
    need(type(owner.get("id")) is int and owner["id"] > 0 and owner.get("login") == "Sykezzz", "trusted owner policy")
    workflow = api("GET", f"/actions/workflows/{WORKFLOW_ID}")
    need(workflow.get("id") == WORKFLOW_ID and workflow.get("path") == ".github/workflows/gam-update.yml"
         and workflow.get("name") == "Update GAM7 pin" and workflow.get("state") == "active", "workflow changed")
    marker = api("GET", f"/pulls/{marker_number}")
    branch = marker.get("head", {}).get("ref", "")
    need(isinstance(branch, str) and branch.startswith("automation/gam-repair-request-"), "normal marker branch")
    incident = sha(branch.removeprefix("automation/gam-repair-request-"))
    head = sha(marker.get("head", {}).get("sha"))
    need(api("GET", "/git/ref/heads/" + branch)["object"]["sha"] == head, "marker head changed")
    path = ".github/repair-requests/gam-bump-" + incident + ".json"
    content = api("GET", f"/contents/{path}?ref={head}")
    metadata = content["content_json"]
    need(isinstance(metadata, dict) and type(metadata.get("run_id")) is int
         and 0 < metadata["run_id"] <= 9223372036854775807, "source run ID")
    need(len(json.dumps(metadata)) <= 4096, "bounded metadata")
    source = api("GET", f"/actions/runs/{metadata['run_id']}")
    default = api("GET", "/git/ref/heads/" + BASE)["object"]["sha"]
    files = api("GET", f"/pulls/{marker_number}/files?per_page=100")
    request = Request.validated(marker, metadata, files, source, default, owner["id"])
    runs = api("GET", f"/actions/workflows/{WORKFLOW_ID}/runs?branch={BASE}&head_sha={incident}&per_page=100")["workflow_runs"]
    need(isinstance(runs, list) and len(runs) < 100, "bounded incident runs")
    when = datetime.fromisoformat(source["updated_at"].replace("Z", "+00:00"))
    need(not any(item.get("status") == "completed" and item.get("conclusion") == "success"
                 and item.get("head_sha") == incident and item.get("head_branch") == BASE
                 and repository(item.get("head_repository")) and item.get("workflow_id") == WORKFLOW_ID
                 and item.get("event") in ("schedule", "workflow_dispatch")
                 and datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")) > when for item in runs), "newer success resolves incident")
    return request


TERMINAL_PREFIX = "<!-- gam-repair-terminal-v1 -->\n"


def terminal_record(api, pull, request, attempt):
    """Closed PR alone does not certify stop; require host-authored durable record."""
    comments = api("GET", f"/issues/{pull['number']}/comments?per_page=100")
    need(isinstance(comments, list) and len(comments) < 100, "bounded termination history")
    records = [comment for comment in comments if isinstance(comment.get("body"), str)
               and comment["body"].startswith(TERMINAL_PREFIX)]
    need(len(records) == 1, "one host stop record required; closure alone is not terminal")
    comment = records[0]
    need(len(comment["body"]) <= 4096, "bounded termination record")
    record = json.loads(comment["body"][len(TERMINAL_PREFIX):])
    keys = {"schema", "kind", "receipt_sha", "incident_sha", "repair_attempt", "nonce", "outcome", "task_stopped"}
    need(isinstance(record, dict) and set(record) == keys and record["schema"] == 1
         and record["kind"] == "repair-attempt-terminal" and record["incident_sha"] == request.incident_sha
         and record["repair_attempt"] == attempt and record["outcome"] in ("failed", "rejected")
         and record["task_stopped"] is True and isinstance(record["nonce"], str) and HEX32.fullmatch(record["nonce"]), "termination record identity")
    original = sha(record["receipt_sha"])
    path = f".github/repair-attempts/{request.incident_sha}-{attempt}.json"
    receipt = api("GET", f"/contents/{path}?ref={original}")["content_json"]
    need(receipt.get("schema") == 1 and receipt.get("kind") == "repair-attempt"
         and receipt.get("repository_id") == REPO_ID and receipt.get("workflow_id") == WORKFLOW_ID
         and receipt.get("marker_pr") == request.marker_number and receipt.get("incident_sha") == request.incident_sha
         and receipt.get("run_id") == request.run_id and receipt.get("run_attempt") == request.run_attempt
         and receipt.get("repair_attempt") == attempt and receipt.get("nonce") == record["nonce"]
         and type(receipt.get("actor_id")) is int and receipt["actor_id"] > 0
         and comment.get("user", {}).get("id") == receipt["actor_id"]
         and pull.get("user", {}).get("id") == receipt["actor_id"] == request.owner_id, "host receipt provenance")
    initial = api("GET", f"/compare/{request.incident_sha}...{original}")
    need(initial.get("status") == "ahead" and initial.get("ahead_by") == 1 and initial.get("behind_by") == 0
         and len(initial.get("files", [])) == 1 and initial["files"][0].get("filename") == path
         and initial["files"][0].get("status") == "added", "original receipt diff")
    head = sha(pull.get("head", {}).get("sha"))
    ancestry = api("GET", f"/compare/{original}...{head}")
    need(ancestry.get("status") in ("ahead", "identical") and ancestry.get("behind_by") == 0, "receipt not in repair history")
    return True


def finish(ticket, outcome, *, task_stopped=False, api=denied):
    """Trusted native coordinator calls only AFTER task/reviewer/tests have stopped.

    No close-on-timeout while a process still runs. Public author-id record is a
    trusted coordinator assertion, not cryptographic proof of process termination.
    No service/credential/security setup is needed. Ambiguity holds/no retry.
    """
    need(isinstance(ticket, Ticket) and type(ticket.attempt) is int and ticket.attempt in (1, 2)
         and type(ticket.repair_pr) is int and ticket.repair_pr > 0 and type(ticket.actor_id) is int and ticket.actor_id > 0
         and isinstance(ticket.nonce, str) and HEX32.fullmatch(ticket.nonce)
         and task_stopped is True and outcome in ("failed", "rejected"), "verified host stop required")
    try:
        actor = api("GET", "/user")
        repo = api("GET", "")
        need(repository(repo) and repo.get("owner", {}).get("id") == actor.get("id") == ticket.actor_id
             and actor.get("login") == "Sykezzz", "terminal trusted owner changed")
        pull = api("GET", f"/pulls/{ticket.repair_pr}")
        need(pull.get("state") == "open" and pull.get("head", {}).get("ref") == ticket.branch
             and repository(pull["head"].get("repo")) and repository(pull.get("base", {}).get("repo"))
             and pull["base"].get("ref") == BASE and pull.get("user", {}).get("id") == ticket.actor_id
             and not pull.get("merged_at") and not pull.get("merged"), "attempt PR no longer open")
        comments = api("GET", f"/issues/{ticket.repair_pr}/comments?per_page=100")
        need(isinstance(comments, list) and len(comments) < 100
             and not any(isinstance(c.get("body"), str) and c["body"].startswith(TERMINAL_PREFIX) for c in comments), "terminal already recorded; do not replay")
        incident = sha(ticket.branch.removeprefix("automation/gam-repair-").removesuffix(f"-attempt-{ticket.attempt}"))
        original = sha(ticket.receipt_sha)
        path = f".github/repair-attempts/{incident}-{ticket.attempt}.json"
        receipt = api("GET", f"/contents/{path}?ref={original}")["content_json"]
        need(receipt.get("nonce") == ticket.nonce and receipt.get("repair_attempt") == ticket.attempt
             and receipt.get("actor_id") == ticket.actor_id and receipt.get("incident_sha") == incident, "ticket receipt identity")
        record = {"schema": 1, "kind": "repair-attempt-terminal", "receipt_sha": original,
                  "incident_sha": incident, "repair_attempt": ticket.attempt, "nonce": ticket.nonce,
                  "outcome": outcome, "task_stopped": True}
        comment = api("POST", f"/issues/{ticket.repair_pr}/comments", {"body": TERMINAL_PREFIX + json.dumps(record, sort_keys=True, separators=(",", ":"))})
        need(comment.get("user", {}).get("id") == ticket.actor_id and type(comment.get("id")) is int, "durable terminal comment required")
        api("PATCH", f"/pulls/{ticket.repair_pr}", {"state": "closed"})
        need(api("GET", f"/pulls/{ticket.repair_pr}").get("state") == "closed", "closure ambiguous")
        return {"terminal": outcome, "repair_pr": ticket.repair_pr, "merged": False}
    except Hold:
        raise
    except Exception:
        raise Hold("terminal operation ambiguous; retain state, no retry") from None


def claim(marker_number, nonce, api=denied):
    """Reserve one slot, persist its draft PR, then issue one launch ticket.

    api MUST implement create-only POST/git/refs, no upsert, no retries, and map
    its already-exists error to Conflict. A transport timeout is ambiguous: HOLD,
    never select another slot. Native task executes no diagnosis before ticket.
    """
    need(isinstance(nonce, str) and HEX32.fullmatch(nonce), "host-owned opaque nonce required")
    # Public receipt nonce is a correlation identifier, never an auth credential.
    try:
        request = load_request(api, marker_number)
        actor = api("GET", "/user")
        need(actor.get("id") == request.owner_id and actor.get("login") == "Sykezzz", "trusted owner coordinator required")
        marker = api("GET", f"/pulls/{request.marker_number}")
        need(marker.get("state") == "open" and marker.get("draft") is True
             and marker.get("head", {}).get("sha") == request.marker_sha, "marker changed/closed")
        need(api("GET", "/git/ref/heads/" + BASE)["object"]["sha"] == request.incident_sha, "default changed")
        state = []
        for attempt in (1, 2):
            branch = f"automation/gam-repair-{request.incident_sha}-attempt-{attempt}"
            ref = api("GET", "/git/ref/heads/" + branch, optional=True)
            if ref is not None:
                sha(ref.get("object", {}).get("sha"))
            history = api("GET", f"/pulls?state=all&base={BASE}&head=Sykezzz:{branch}&per_page=100")
            need(isinstance(history, list) and len(history) <= 1, "ambiguous attempt history")
            for pull in history:
                need(repository(pull.get("head", {}).get("repo")) and pull["head"].get("ref") == branch
                     and repository(pull.get("base", {}).get("repo")) and pull["base"].get("ref") == BASE, "claim history identity")
                need(pull.get("state") in ("open", "closed") and type(pull.get("number")) is int, "claim history state")
                need(not pull.get("merged_at") and not pull.get("merged"), "incident already merged/resolved")
            occupied = ref is not None or bool(history)
            terminal = False
            if history and history[0]["state"] == "closed":
                terminal = terminal_record(api, history[0], request, attempt)
            state.append((branch, occupied, terminal))
        need(not state[1][1] or state[0][1], "out-of-order claim history")
        if state[0][1]:
            need(state[0][2], "first claim active/orphaned; held, not resumed")
            need(not state[1][1], "two attempts consumed")
            attempt = 2
        else:
            attempt = 1
        branch = state[attempt - 1][0]
        receipt = {"schema": 1, "kind": "repair-attempt", "repository_id": REPO_ID,
                   "workflow_id": WORKFLOW_ID, "incident_sha": request.incident_sha,
                   "marker_pr": request.marker_number, "run_id": request.run_id,
                   "run_attempt": request.run_attempt, "repair_attempt": attempt, "nonce": nonce, "actor_id": actor["id"]}
        path = f".github/repair-attempts/{request.incident_sha}-{attempt}.json"
        text = json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
        tree = sha(api("GET", "/git/commits/" + request.incident_sha)["tree"]["sha"])
        tree = sha(api("POST", "/git/trees", {"base_tree": tree, "tree": [{"path": path, "mode": "100644", "type": "blob", "content": text}]})["sha"])
        commit = sha(api("POST", "/git/commits", {"message": "chore: reserve bounded GAM repair attempt", "tree": tree, "parents": [request.incident_sha]})["sha"])
        need(api("GET", "/git/ref/heads/" + BASE)["object"]["sha"] == request.incident_sha, "default changed before claim")
        # Linearization point. Never update/reset a pre-existing ref.
        api("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": commit})
        need(api("GET", "/git/ref/heads/" + branch)["object"]["sha"] == commit, "claim ref changed")
        diff = api("GET", f"/compare/{request.incident_sha}...{commit}")
        need(diff.get("status") == "ahead" and diff.get("ahead_by") == 1 and diff.get("behind_by") == 0
             and len(diff.get("files", [])) == 1 and diff["files"][0].get("filename") == path
             and diff["files"][0].get("status") == "added", "receipt-only initial diff")
        pull = api("POST", "/pulls", {"head": branch, "base": BASE, "draft": True,
            "title": f"[GAM bump repair attempt {attempt}] {request.incident_sha}",
            "body": "Reserved bounded repair attempt; no diagnosis has run. Separate from request marker. "
                    "Keep this draft until independent exact-SHA review and full CI/protection checks. "
                    "On failure close without merge; retain receipt/history.\n\n```json\n" + text + "```\n"})
        number = pull.get("number")
        need(type(number) is int and number > 0, "durable repair PR required")
        final = api("GET", f"/pulls/{number}")
        need(final.get("state") == "open" and final.get("draft") is True
             and final.get("head", {}).get("sha") == commit and final["head"].get("ref") == branch
             and repository(final["head"].get("repo")) and final.get("base", {}).get("ref") == BASE
             and repository(final["base"].get("repo")) and final.get("user", {}).get("id") == actor["id"], "repair PR changed")
        need(api("GET", "/git/ref/heads/" + branch)["object"]["sha"] == commit
             and api("GET", "/git/ref/heads/" + BASE)["object"]["sha"] == request.incident_sha, "head/base changed before ticket")
        need(api("GET", f"/contents/{path}?ref={commit}")["content_json"] == receipt, "receipt content changed")
        need(load_request(api, request.marker_number) == request, "source/marker changed before ticket")
        # Only this successful fresh create-ref call returns. Duplicate events,
        # restart/orphaned claims and PR failures never resume or issue a ticket.
        return Ticket(attempt, branch, commit, number, nonce, actor["id"])
    except Conflict:
        raise Hold("atomic claim lost; stop, do not take next slot") from None
    except Hold:
        raise
    except Exception:
        raise Hold("GitHub state ambiguous/operation failed; retain any claim, no retry") from None


REPAIR_FILES = frozenset({
    "scripts/bump_gam.py", "scripts/fetch_gam.sh", "scripts/fetch_gam_windows.ps1",
    "scripts/gam_checksums.txt", "scripts/build_windows_release.ps1", "scripts/build_windows_setup.ps1",
    "tests/test_bump_gam.py", "tests/test_build_profiles.py", "tests/test_command_contract.py",
    "tests/test_acceptance_privacy.py", "tests/fixtures/mock_gam.sh", "gamgui/core/gam/commands.py",
    "gamgui/resources/gam7/VERSION", "gamgui/resources/gam7/command_catalog.json", "README.md",
})


def candidate_paths(paths):
    """Final base-to-candidate diff: remove receipt via normal descendant commit.

    Original receipt remains immutable/reachable in PR commit history for ledger
    proof. This gate grants no publication/merge permission; review/full CI and
    current branch protections remain separately required.
    """
    need(isinstance(paths, (list, tuple)) and 0 < len(paths) <= len(REPAIR_FILES), "bounded repair diff")
    need(all(isinstance(path, str) for path in paths) and len(set(paths)) == len(paths)
         and all(path in REPAIR_FILES for path in paths), "receipt/request/security/unrelated files outside final repair diff")
    return tuple(paths)
