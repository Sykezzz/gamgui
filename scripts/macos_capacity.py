"""Recognize provider-confirmed allocation failures without excusing executed tests."""

from __future__ import annotations

from typing import Any

ALLOCATION_ERROR = "The job was not acquired by Runner of type hosted even after multiple attempts"
MACOS_JOBS = {
    "gam-compat (pinned binary)", "gam-compat (latest preview)",
    "macOS application build smoke (core)",
    "macOS application build smoke (classroom-oneroster)",
    *(f"test (macos-latest, py{version})" for version in ("3.10", "3.12", "3.14")),
}
SKIPPABLE_JOBS = {"dispatch exact-SHA validation", "report automated integration failure"}


def pages(gh: Any, endpoint: str, key: str | None = None) -> list:
    result = gh.json(["api", "--paginate", "--slurp", endpoint])
    if not isinstance(result, list):
        raise RuntimeError("Invalid paginated GitHub response.")
    items = []
    for page in result:
        values = page.get(key) if key and isinstance(page, dict) else page
        if not isinstance(values, list):
            raise RuntimeError("Invalid GitHub response page.")
        items.extend(values)
    return items


def capacity_only(gh: Any, repository: str, run_id: int, head_sha: str) -> bool:
    jobs = pages(gh, f"repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100", "jobs")
    if not jobs or len({job.get("id") for job in jobs}) != len(jobs):
        return False
    allocation = set()
    aggregates = []
    for job in jobs:
        if (job.get("run_id") != run_id or job.get("head_sha") != head_sha):
            return False
        name, conclusion = job.get("name"), job.get("conclusion")
        if conclusion == "success" and job.get("status") == "completed":
            continue
        if conclusion == "skipped" and name in SKIPPABLE_JOBS:
            continue
        if name == "macOS application build smoke":
            aggregates.append(job)
            continue
        if (name not in MACOS_JOBS or job.get("status") != "completed" or conclusion not in ("cancelled", "failure")
                or job.get("steps") != [] or job.get("runner_id") != 0
                or job.get("labels") != ["macos-latest"]):
            return False
        job_id = job.get("id")
        if type(job_id) is not int or job_id <= 0:
            return False
        annotations = pages(gh, f"repos/{repository}/check-runs/{job_id}/annotations?per_page=100")
        if not any(item.get("annotation_level") == "failure"
                   and item.get("message") == ALLOCATION_ERROR for item in annotations):
            return False
        allocation.add(name)
    for job in aggregates:
        # This Ubuntu gate only fails as a consequence of the two native profile builds.
        profiles = {"macOS application build smoke (core)",
                    "macOS application build smoke (classroom-oneroster)"}
        failed_steps = [step.get("name") for step in job.get("steps", [])
                        if step.get("conclusion") not in ("success", "skipped")]
        if (allocation.intersection(profiles) and job.get("status") == "queued"
                and job.get("steps") == [] and job.get("runner_id") == 0
                and job.get("labels") == ["ubuntu-latest"]):
            continue
        if (not allocation.intersection(profiles) or job.get("conclusion") != "failure"
                or failed_steps != ["Require every application profile build"]):
            return False
    return bool(allocation)


def verification_pending(gh: Any, repository: str, run_id: int, head_sha: str) -> bool:
    jobs = pages(gh, f"repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100", "jobs")
    return bool(jobs) and all(
        job.get("run_id") == run_id and job.get("head_sha") == head_sha
        and ((job.get("status") in ("queued", "in_progress", "waiting") and job.get("conclusion") is None)
             or (job.get("status") == "completed" and job.get("conclusion") == "success")
             or (job.get("conclusion") == "skipped" and job.get("name") in SKIPPABLE_JOBS))
        for job in jobs
    )
