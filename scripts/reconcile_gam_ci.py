#!/usr/bin/env python3
"""Resume GAM bump PRs from trusted default-branch code, never from PR code."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re

if __package__:
    from .macos_capacity import capacity_only, pages
    from .merge_tested_automation_pr import GitHubCLI, dispatch_test_and_merge
else:
    from macos_capacity import capacity_only, pages
    from merge_tested_automation_pr import GitHubCLI, dispatch_test_and_merge

REPOSITORY = "Sykezzz/gamgui"
REPOSITORY_ID = 1309243040
CI_WORKFLOW_ID = 318866180
BASE = "district-main"
MAX_ATTEMPTS = 3
BRANCH = re.compile(r"automation/gam-v\d+\.\d+\.\d+-[0-9a-f]{12}(?:-run-\d+)?")
SHA = re.compile(r"[0-9a-f]{40}")


def eligible(pr):
    head, base = pr.get("head", {}), pr.get("base", {})
    return (pr.get("state") == "open" and pr.get("draft") is False
            and type(pr.get("number")) is int and pr["number"] > 0
            and head.get("repo", {}).get("full_name") == REPOSITORY
            and base.get("repo", {}).get("full_name") == REPOSITORY
            and base.get("ref") == BASE
            and BRANCH.fullmatch(head.get("ref", ""))
            and SHA.fullmatch(head.get("sha", ""))
            and re.fullmatch(r"chore: bump GAM7 to v\d+\.\d+\.\d+", pr.get("title", "")))


def issue(gh, pr, run, reason):
    # A stable exact-head title avoids adding the same comment every scheduled check.
    category = "capacity exhausted" if reason.startswith("macOS hosted") else "CI failure"
    title = f"GAM {category}: PR #{pr['number']} at {pr['head']['sha']}"
    existing = pages(gh, f"repos/{REPOSITORY}/issues?state=all&per_page=100")
    if any(item.get("title") == title for item in existing):
        return
    gh.run(["issue", "create", "--repo", REPOSITORY, "--title", title, "--body",
            f"{reason}\n\nNo merge was requested by this recovery check. "
            f"Native checks and update-ready remain mandatory.\n"
            f"PR: https://github.com/{REPOSITORY}/pull/{pr['number']}\n"
            f"CI: https://github.com/{REPOSITORY}/actions/runs/{run['id']}"])


def newer_than_pin(gh, pr):
    content = gh.json(["api", f"repos/{REPOSITORY}/contents/gamgui/resources/gam7/VERSION?ref={BASE}"])
    if content.get("encoding") != "base64":
        raise RuntimeError("Unexpected pin encoding.")
    pin = base64.b64decode(content["content"]).decode().strip()
    target = pr["title"].removeprefix("chore: bump GAM7 to ")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", pin):
        raise RuntimeError("Invalid current GAM pin.")
    version = lambda value: tuple(int(part) for part in value[1:].split("."))
    return version(target) > version(pin)


def reconcile(gh, pr, *, expected_run=None):
    if not eligible(pr):
        return "ineligible"
    if not newer_than_pin(gh, pr):
        return "superseded by current pin"
    number, head = pr["number"], pr["head"]["sha"]
    branch = pr["head"]["ref"]
    workflow = gh.json(["api", f"repos/{REPOSITORY}/actions/workflows/ci.yml"])
    if workflow.get("path") != ".github/workflows/ci.yml" or workflow.get("state") != "active":
        raise RuntimeError("Unexpected CI workflow identity.")
    runs = pages(gh, f"repos/{REPOSITORY}/actions/workflows/{workflow['id']}/runs?event=pull_request&head_sha={head}&per_page=100", "workflow_runs")
    candidates = [run for run in runs if run.get("head_sha") == head
                  and run.get("event") == "pull_request" and run.get("head_branch") == branch
                  and run.get("workflow_id") == workflow["id"]
                  and run.get("path") == ".github/workflows/ci.yml"
                  and run.get("head_repository", {}).get("full_name") == REPOSITORY
                  and any(item.get("number") == number for item in run.get("pull_requests", []))]
    if not candidates:
        return "waiting for eligible CI"
    run = max(candidates, key=lambda item: item["id"])
    if expected_run is not None and any(run.get(key) != expected_run.get(key) for key in
            ("id", "head_sha", "workflow_id", "run_attempt", "status", "conclusion")):
        return "completion superseded; recovery held"
    if run.get("status") != "completed":
        return "verification running"

    def unchanged():
        current = gh.json(["api", f"repos/{REPOSITORY}/pulls/{number}"])
        if not newer_than_pin(gh, pr):
            raise RuntimeError("GAM pin advanced; recovery held.")
        latest = gh.json(["api", f"repos/{REPOSITORY}/actions/runs/{run['id']}"])
        if (not eligible(current) or current["head"]["sha"] != head
                or current["head"]["ref"] != branch
                or any(latest.get(key) != run.get(key) for key in
                       ("id", "head_sha", "workflow_id", "run_attempt", "status", "conclusion"))):
            raise RuntimeError("PR or CI changed; recovery held.")

    if run.get("conclusion") == "success":
        unchanged()
        result = dispatch_test_and_merge(
            repository=REPOSITORY, workflow="ci.yml", branch=branch, head_sha=head,
            pull_request=str(number), timeout_seconds=60, poll_seconds=5, gh=gh,
            defer_macos_capacity=True,
        )
        if not result.get("merge_sha"):
            return "deferred"
        gh.run(["workflow", "run", "post-merge-validation.yml", "--repo", REPOSITORY,
                "--ref", BASE, "-f", f"sha={result['merge_sha']}"])
        return "merged; exact-SHA release validation dispatched"
    if capacity_only(gh, REPOSITORY, run["id"], head):
        attempt = run.get("run_attempt")
        if type(attempt) is not int or attempt < 1:
            raise RuntimeError("Invalid CI attempt.")
        unchanged()
        if attempt >= MAX_ATTEMPTS:
            issue(gh, pr, run, "macOS hosted runner capacity recovery exhausted three CI attempts. "
                  "Verification is incomplete, not a demonstrated test failure.")
            return "capacity recovery exhausted"
        gh.run(["run", "rerun", str(run["id"]), "--failed", "--repo", REPOSITORY])
        return "capacity retry requested"
    unchanged()
    issue(gh, pr, run, "CI has an executed failure or an unclassified failure. "
          "It was not excused or automatically retried as runner capacity.")
    return "failure reported"


def resume_release_validation(gh):
    # A merge can succeed before dispatch fails. Recover from remote facts on the next tick.
    ref = gh.json(["api", f"repos/{REPOSITORY}/git/ref/heads/{BASE}"])
    head = ref.get("object", {}).get("sha", "")
    if not SHA.fullmatch(head):
        raise RuntimeError("Invalid district-main SHA.")
    merged = pages(gh, f"repos/{REPOSITORY}/pulls?state=closed&base={BASE}&per_page=100")
    current = [pr for pr in merged if pr.get("merged_at") and pr.get("merge_commit_sha") == head
               and eligible(pr | {"state": "open"})]
    if not current:
        return "no merged GAM bump at current head"
    workflow = gh.json(["api", f"repos/{REPOSITORY}/actions/workflows/post-merge-validation.yml"])
    if workflow.get("path") != ".github/workflows/post-merge-validation.yml" or workflow.get("state") != "active":
        raise RuntimeError("Unexpected validation workflow identity.")
    runs = pages(gh, f"repos/{REPOSITORY}/actions/workflows/{workflow['id']}/runs?head_sha={head}&per_page=100", "workflow_runs")
    if any(run.get("head_sha") == head and run.get("workflow_id") == workflow["id"]
           and run.get("path") == ".github/workflows/post-merge-validation.yml"
           and run.get("event") == "workflow_dispatch"
           and run.get("head_branch") == BASE
           and run.get("head_repository", {}).get("full_name") == REPOSITORY for run in runs):
        return "validation already dispatched"
    latest = gh.json(["api", f"repos/{REPOSITORY}/git/ref/heads/{BASE}"])
    if latest.get("object", {}).get("sha") != head:
        return "base changed; dispatch held"
    gh.run(["workflow", "run", "post-merge-validation.yml", "--repo", REPOSITORY,
            "--ref", BASE, "-f", f"sha={head}"])
    return "missing exact-SHA validation dispatched"


def _same_repository(value):
    return (isinstance(value, dict) and value.get("id") == REPOSITORY_ID
            and value.get("full_name") == REPOSITORY)


def _valid_completion(run):
    return (isinstance(run, dict) and type(run.get("id")) is int and 0 < run["id"] <= 9223372036854775807
            and run.get("workflow_id") == CI_WORKFLOW_ID
            and run.get("path") == ".github/workflows/ci.yml" and run.get("name") == "CI"
            and run.get("event") == "pull_request" and run.get("status") == "completed"
            and run.get("conclusion") in ("success", "failure", "cancelled", "timed_out")
            and type(run.get("run_attempt")) is int and 1 <= run["run_attempt"] <= 10000
            and isinstance(run.get("head_sha"), str) and SHA.fullmatch(run["head_sha"])
            and isinstance(run.get("head_branch"), str) and BRANCH.fullmatch(run["head_branch"])
            and _same_repository(run.get("head_repository")))


def ci_completion(gh, payload, *, repository, repository_id, ref, event_name):
    # workflow_run is privileged even for a fork. Reject before invoking any write path.
    if (event_name != "workflow_run" or repository != REPOSITORY
            or repository_id != str(REPOSITORY_ID) or ref != "refs/heads/" + BASE
            or not isinstance(payload, dict) or payload.get("action") != "completed"
            or not _same_repository(payload.get("repository"))):
        return "ignored unrelated completion context"
    source = payload.get("workflow_run")
    if not _valid_completion(source):
        return "ignored ineligible CI source"
    workflow = gh.json(["api", f"repos/{REPOSITORY}/actions/workflows/ci.yml"])
    if (workflow.get("id") != CI_WORKFLOW_ID or workflow.get("path") != source["path"]
            or workflow.get("name") != "CI" or workflow.get("state") != "active"):
        return "ignored unexpected CI workflow"
    current = gh.json(["api", f"repos/{REPOSITORY}/actions/runs/{source['id']}"])
    if (not _valid_completion(current)
            or any(current.get(key) != source.get(key) for key in
                   ("id", "head_sha", "head_branch", "workflow_id", "run_attempt", "status", "conclusion"))):
        return "ignored stale CI completion"
    pulls = current.get("pull_requests")
    if (not isinstance(pulls, list) or len(pulls) != 1 or not isinstance(pulls[0], dict)
            or type(pulls[0].get("number")) is not int or pulls[0]["number"] <= 0):
        return "ignored ambiguous CI pull request"
    number = pulls[0]["number"]
    pr = gh.json(["api", f"repos/{REPOSITORY}/pulls/{number}"])
    if (not eligible(pr) or not _same_repository(pr["head"].get("repo"))
            or not _same_repository(pr["base"].get("repo"))
            or pr["number"] != number or pr["head"]["sha"] != current["head_sha"]
            or pr["head"]["ref"] != current["head_branch"]):
        return "ignored changed or ineligible pull request"
    # Event handling is scoped to this exact PR/run, never a broad scheduled scan.
    return reconcile(gh, pr, expected_run=current)


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    gh = GitHubCLI()
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_run":
        with Path(os.environ["GITHUB_EVENT_PATH"]).open("rb") as event_file:
            raw = event_file.read(1048577)
        if len(raw) > 1048576:
            raise RuntimeError("Completion event exceeds size limit.")
        print(ci_completion(
            gh, json.loads(raw), repository=os.environ.get("GITHUB_REPOSITORY"),
            repository_id=os.environ.get("GITHUB_REPOSITORY_ID"),
            ref=os.environ.get("GITHUB_REF"), event_name=os.environ.get("GITHUB_EVENT_NAME"),
        ))
        return 0
    print(resume_release_validation(gh))
    prs = pages(gh, f"repos/{REPOSITORY}/pulls?state=open&base={BASE}&per_page=100")
    failed = False
    for pr in prs:
        if not eligible(pr):
            continue
        try:
            print(f"PR #{pr['number']}: {reconcile(gh, pr)}")
        except RuntimeError as error:
            # Don't continue writes for this PR after an uncertain API outcome.
            print(f"PR #{pr['number']}: recovery held: {error}")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
