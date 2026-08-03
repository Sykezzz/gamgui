#!/usr/bin/env python3
"""Approve or dispatch exact-SHA CI, then merge the unchanged automation PR."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


class GitHubCLI:
    def json(self, args: list[str], payload: dict[str, str] | None = None) -> Any:
        command = ["gh", *args]
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            input=json.dumps(payload) if payload is not None else None,
        )
        return json.loads(completed.stdout)

    def run(self, args: list[str]) -> None:
        subprocess.run(["gh", *args], check=True)


def _workflow_runs(
    gh: GitHubCLI,
    *,
    repository: str,
    workflow: str,
    branch: str,
    event: str,
) -> list[dict[str, Any]]:
    result = gh.json(
        [
            "run",
            "list",
            "--repo",
            repository,
            "--workflow",
            workflow,
            "--branch",
            branch,
            "--event",
            event,
            "--limit",
            "30",
            "--json",
            "databaseId,headSha,status,conclusion,url",
        ]
    )
    if not isinstance(result, list):
        raise RuntimeError("GitHub returned an invalid workflow-run response.")
    return result


def dispatch_test_and_merge(
    *,
    repository: str,
    workflow: str,
    branch: str,
    head_sha: str,
    pull_request: str,
    timeout_seconds: int,
    poll_seconds: int,
    gh: GitHubCLI,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, str]:
    match = re.search(r"(?:^|/)(\d+)$", pull_request)
    if not match:
        raise ValueError("Pull request must be a number or a GitHub pull request URL.")
    pull_number = match.group(1)
    details = gh.json(
        [
            "pr",
            "view",
            pull_number,
            "--repo",
            repository,
            "--json",
            "headRefOid,state,mergeCommit",
        ]
    )
    if details.get("headRefOid") != head_sha:
        raise RuntimeError("The pull request does not point to the expected head SHA.")
    if details.get("state") != "OPEN":
        raise RuntimeError("The automation pull request is not open.")

    pull_request_runs = [
        run
        for run in _workflow_runs(
            gh,
            repository=repository,
            workflow=workflow,
            branch=branch,
            event="pull_request",
        )
        if run.get("headSha") == head_sha
    ]
    selected_run_id: int | None = None
    if pull_request_runs:
        selected = max(
            pull_request_runs,
            key=lambda run: int(run["databaseId"]),
        )
        selected_run_id = int(selected["databaseId"])
        conclusion = selected.get("conclusion")
        if conclusion == "action_required":
            gh.run(
                [
                    "api",
                    "--method",
                    "POST",
                    f"repos/{repository}/actions/runs/{selected_run_id}/approve",
                ]
            )
        elif selected.get("status") == "completed" and conclusion != "success":
            gh.run(
                [
                    "run",
                    "rerun",
                    str(selected_run_id),
                    "--repo",
                    repository,
                ]
            )
    else:
        previous_run_ids = {
            int(run["databaseId"])
            for run in _workflow_runs(
                gh,
                repository=repository,
                workflow=workflow,
                branch=branch,
                event="workflow_dispatch",
            )
            if run.get("headSha") == head_sha
        }
        gh.run(
            [
                "workflow",
                "run",
                workflow,
                "--repo",
                repository,
                "--ref",
                branch,
            ]
        )

    deadline = monotonic() + timeout_seconds
    selected: dict[str, Any] | None = None
    while monotonic() < deadline:
        if selected_run_id is not None:
            candidates = [
                run
                for run in _workflow_runs(
                    gh,
                    repository=repository,
                    workflow=workflow,
                    branch=branch,
                    event="pull_request",
                )
                if int(run["databaseId"]) == selected_run_id
                and run.get("headSha") == head_sha
            ]
        else:
            candidates = [
                run
                for run in _workflow_runs(
                    gh,
                    repository=repository,
                    workflow=workflow,
                    branch=branch,
                    event="workflow_dispatch",
                )
                if run.get("headSha") == head_sha
                and int(run["databaseId"]) not in previous_run_ids
            ]
        if candidates:
            selected = max(candidates, key=lambda run: int(run["databaseId"]))
            if selected.get("status") == "completed":
                break
        sleep(poll_seconds)

    if selected is None or selected.get("status") != "completed":
        raise RuntimeError(
            f"Timed out waiting for an exact-SHA {workflow} run at {head_sha}."
        )
    if selected.get("conclusion") != "success":
        raise RuntimeError(
            f"Exact-SHA CI did not pass: {selected.get('url', 'unknown run')} "
            f"concluded {selected.get('conclusion') or 'without a conclusion'}."
        )

    details = gh.json(
        [
            "pr",
            "view",
            pull_number,
            "--repo",
            repository,
            "--json",
            "headRefOid,state,mergeCommit",
        ]
    )
    if details.get("headRefOid") != head_sha:
        raise RuntimeError("The pull request changed after its exact-SHA CI run passed.")
    if details.get("state") == "MERGED":
        merge_commit = details.get("mergeCommit") or {}
        return {
            "ci_run_url": str(selected.get("url", "")),
            "merge_sha": str(merge_commit.get("oid", "")),
        }
    if details.get("state") != "OPEN":
        raise RuntimeError("The automation pull request closed without merging.")

    merge = gh.json(
        [
            "api",
            "--method",
            "PUT",
            f"repos/{repository}/pulls/{pull_number}/merge",
            "--input",
            "-",
        ],
        payload={"merge_method": "merge", "sha": head_sha},
    )
    if not merge.get("merged"):
        raise RuntimeError(
            f"GitHub rejected the protected merge: {merge.get('message', 'unknown error')}."
        )
    return {
        "ci_run_url": str(selected.get("url", "")),
        "merge_sha": str(merge.get("sha", "")),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", default="ci.yml")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--pull-request", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1500)
    parser.add_argument("--poll-seconds", type=int, default=10)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", args.repository):
        raise SystemExit("Invalid owner/repository value.")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.head_sha):
        raise SystemExit("Head SHA must be a full 40-character commit SHA.")
    if args.timeout_seconds <= 0 or args.poll_seconds <= 0:
        raise SystemExit("Timeout and poll interval must be positive.")

    result = dispatch_test_and_merge(
        repository=args.repository,
        workflow=args.workflow,
        branch=args.branch,
        head_sha=args.head_sha.lower(),
        pull_request=args.pull_request,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        gh=GitHubCLI(),
    )
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            for key, value in result.items():
                output.write(f"{key}={value}\n")
    print(
        f"Merged exact tested revision {args.head_sha.lower()} as "
        f"{result['merge_sha']} ({result['ci_run_url']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
