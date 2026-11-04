#!/usr/bin/env python3
"""Wait for exact-SHA pull request CI, then merge the unchanged automation PR."""

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
            check=False,
            capture_output=True,
            text=True,
            input=json.dumps(payload) if payload is not None else None,
        )
        if completed.returncode:
            raise RuntimeError(_github_error(completed, payload))
        return json.loads(completed.stdout)

    def run(self, args: list[str]) -> None:
        subprocess.run(["gh", *args], check=True)


def _github_error(
    completed: subprocess.CompletedProcess[str], payload: dict[str, str] | None
) -> str:
    # Report the provider's message, never the full response, command, or request body.
    try:
        response = json.loads(completed.stdout)
    except (ValueError, TypeError):
        response = {}
    if not isinstance(response, dict):
        response = {}
    message = response.get("message")
    stderr = completed.stderr or ""
    status = str(response.get("status", ""))
    http = re.search(r"\(HTTP ([45]\d{2})\)", stderr)
    if not re.fullmatch(r"[45]\d{2}", status):
        status = http.group(1) if http else ""
    if not isinstance(message, str):
        match = re.search(r"^gh: (.+?)(?: \(HTTP [45]\d{2}\))?$", stderr, re.MULTILINE)
        message = match.group(1) if match else "No GitHub error message was returned."
    secrets = [
        os.environ.get(name, "")
        for name in (
            "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"
        )
    ]
    secrets.extend((payload or {}).values())
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        message = message.replace(secret, "[redacted]")
    message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message)
    message = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b",
        "[redacted]", message,
    )
    message = re.sub(
        r"\b(?:Bearer|Basic)\s+\S+", "[redacted]", message, flags=re.IGNORECASE
    )
    message = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", message).split())
    if len(message) > 500:
        message = message[:497] + "..."
    suffix = f", HTTP {status}" if status else ""
    return f"GitHub CLI failed (exit {completed.returncode}{suffix}): {message}"


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
            "databaseId,headSha,status,conclusion,url,attempt",
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
    deadline = monotonic() + timeout_seconds
    head_deadline = min(deadline, monotonic() + 60)
    details: dict[str, Any] = {}
    # Pull request metadata can briefly lag the branch push that refreshed it.
    while monotonic() < head_deadline:
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
        if details.get("state") != "OPEN":
            raise RuntimeError("The automation pull request is not open.")
        if details.get("headRefOid") == head_sha:
            break
        sleep(min(poll_seconds, max(0, head_deadline - monotonic())))
    else:
        raise RuntimeError(
            "Timed out waiting for the pull request head to update: "
            f"expected {head_sha}, observed {details.get('headRefOid') or 'unknown'}."
        )

    discovery_deadline = min(deadline, monotonic() + 60)
    selected: dict[str, Any] | None = None
    # Newly created pull request runs can take time to appear in GitHub's index.
    while monotonic() < discovery_deadline:
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
        if pull_request_runs:
            selected = max(
                pull_request_runs, key=lambda run: int(run["databaseId"])
            )
            break
        sleep(min(poll_seconds, max(0, discovery_deadline - monotonic())))
    if selected is None:
        raise RuntimeError(
            f"No eligible pull_request {workflow} run appeared at {head_sha}. "
            "Manually dispatched CI cannot satisfy required pull request checks."
        )

    selected_run_id = int(selected["databaseId"])
    minimum_attempt = int(selected["attempt"])
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
                "--failed",
                "--repo",
                repository,
            ]
        )
        minimum_attempt += 1

    selected = None
    while monotonic() < deadline:
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
            and int(run["attempt"]) >= minimum_attempt
            and run.get("conclusion") != "action_required"
        ]
        if candidates:
            selected = max(candidates, key=lambda run: int(run["databaseId"]))
            if selected.get("status") == "completed":
                break
        sleep(min(poll_seconds, max(0, deadline - monotonic())))

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
