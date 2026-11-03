# Hosted runner allocation retries

`gam-updater-runner-retry.yml` retries only the GAM updater's verified failure to
acquire a hosted macOS runner. It runs on Ubuntu without checking out or executing
repository source, artifacts, caches, or external actions. It uses the existing
workflow token with Contents:read, Checks:read (for failure annotations), and Actions:write.

The updater uses the supported `macos-26-intel` pool to avoid depending on the
capacity-constrained arm64 pool for update generation. The existing fetcher
selects the pinned macOS 26.6 x86_64 asset. All arm64 CI, application build, and
promotion gates remain in place. This mitigates one pool dependency; it does not
guarantee Intel runner availability.

The handler re-fetches the fixed repository and updater workflow, failed run and
attempt, current default SHA, bounded same-head run history, and the single `bump`
job. That job must request only `macos-26-intel` (or the prior `macos-latest`
configuration), have no steps and no runner, and
carry the exact GitHub failure annotation:

> The job was not acquired by Runner of type hosted even after multiple attempts

A moved default, newer run, already advanced attempt, ordinary job failure,
missing annotation, or ambiguous/truncated response stops the handler. It checks
the default and run history again, then re-fetches the source attempt immediately
before its single `rerun-failed-jobs` request. Per-run concurrency and the current
attempt check suppress duplicate completion deliveries. GitHub has no atomic
compare-and-rerun endpoint, so an external rerun or new run can still race the final
read. Failed or uncertain requests are never repeated by the handler.

Attempts 1 and 2 may be retried; attempt 3 stops. Thus each original run receives
at most two requested retries through this workflow. Persistent capacity failures
still need investigation. The updater's CI, protected merge, and post-merge
validation remain responsible for any work once a runner starts. The existing
metadata repair relay and native repair coordinator are unchanged.

The workflow becomes active after an ordinary protected merge to `district-main`;
it does not retrospectively retry earlier completion events. Tests execute the
inline handler against synthetic GitHub snapshots and mocked bounded HTTP calls:

```console
python -m unittest discover -s tests -p test_gam_updater_runner_retry.py -q
```

GitHub documents that [reruns preserve the original SHA, ref and triggering actor](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs).

## Pull request CI and merge errors

The merge helper waits up to 60 seconds for GitHub to register an exact-head
`pull_request` CI run. It approves workflow execution when required, waits past a
stale approval response, and requires a new attempt when retrying failed jobs.
It never substitutes manually dispatched CI for that run: GitHub
[does not evaluate dispatched job checks for a pull request's required checks](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks#checks-from-some-workflow-jobs-are-not-evaluated).
Missing eligible CI, failed tests, or a changed head stop the merge. API failures
include a bounded provider message and HTTP status with credentials and request
values removed. Branch protections and post-merge validation still apply.
