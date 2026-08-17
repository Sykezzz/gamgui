# OneRoster performance work — handoff

Working note for the `oneroster/additions-first-bootstrap` branch. Delete this file when the
branch merges; it describes in-flight state, not durable behaviour.

## What this branch is

Two things stacked together:

1. **Preserved work** that was sitting uncommitted in a detached HEAD at `a8ccc14` — the
   additions-first phase engine, GAM streaming/native batch, Windows credential + component
   support, and the OneRoster web surface. Not written in this session; committed so it could not
   be lost.
2. **New work**: the bulk roster read is now concurrent and reports progress.

Full offline suite is green on the branch: **1,365 passed, 71 skipped, 0 failures**.

## Setting up on another machine

Requires **Python 3.10+** and **uv 0.11.7** exactly (`make setup` refuses other versions rather
than silently updating `uv.lock`).

```powershell
git clone https://github.com/Sykezzz/gamgui.git
cd gamgui
git switch oneroster/additions-first-bootstrap

uv sync --frozen --python (Get-Command python).Source --extra dev --extra desktop --extra build
.\scripts\fetch_gam_windows.ps1        # GAM7 binary is NOT in git — must be fetched
.venv\Scripts\python.exe -m pytest -q  # expect 1365 passed, 71 skipped
.venv\Scripts\python.exe -m gamgui.app # launch
```

**The GAM7 binary is gitignored** (`gamgui/resources/gam7/*`, platform-specific and large). Cloning
alone is not enough — `fetch_gam_windows.ps1` pins `v7.47.02` and records its checksum. Only
`VERSION` and `command_catalog.json` are tracked.

Credentials live in the OS credential store, not the repo, so they come across separately.

### Note on Windows credentials

Windows Credential Manager rejects blobs above 2560 bytes. This branch splits oversized secrets
across chunk entries behind a manifest with a SHA-256 check (`gamgui/core/secrets/vault.py`). If
importing credentials from another machine, that path is what handles the large ones — a failure
there surfaces as a vault read returning `None` rather than a partial value.

## The immediate next step: benchmark before a full run

The roster read now defaults to **20 workers** (was effectively 5, inherited from `gam.cfg`'s
`num_threads`). That 5→20 change is *expected* to be roughly linear, but this is unverified —
scaling could flatten at any point, and validating on the full ~9,189 courses costs ~3 hours per
attempt.

**Benchmark on a subset instead.** The worker count is env-overridable specifically for this:

```powershell
$env:GAMGUI_ONEROSTER_ROSTER_READ_WORKERS = "10"
```

Run a few hundred courses at 5 / 10 / 20 / 40 and compare wall clock. Minutes instead of hours,
and it gives a real scaling curve to pick from. Invalid or out-of-range values fall back to the
default rather than breaking the read.

Rationale for going higher on reads than writes: all three observed `GAM-RATE-LIMITED` failures
were on **write** phases (`student_add`, `course_activate`, `teacher_add`). No read phase has been
throttled. Read quotas are generally more generous — but this is inference, not measurement, so
watch for throttling as the count rises.

## Tenant state — read before running anything

Manifest `83d17c7b…` is in `recovery_required`, from `state.db`:

| status | meaning | count |
|---|---|---|
| `applied` | confirmed in Google | 4,350 |
| `submitted` | dispatched, unconfirmed | 134,290 |
| `pending` | never sent | 157,227 |

Measured per-phase throughput (`apply_seconds ÷ action_count`): `course_create` 3.81/s,
`course_activate` 9.34/s, `student_add` 40.5/s, `teacher_add` 2.68/s. The pending work alone is
**~2 hours**; re-sending the submitted actions would add ~1.5 more. Reconciling first is the
cheaper path and avoids 134k redundant calls into the rate limiter.

**The app does not auto-resume this on launch.** Every entry point is a route handler
(`web/routes/oneroster.py`); the FastAPI `lifespan` hook does nothing OneRoster-related. Launching
the app is safe. Triggering the bootstrap/reconcile action from the UI is what starts a multi-hour
job — don't, until the benchmark is done.

## Progress reporting

The read now persists its own counter to `execution_runs`
(`read_progress_count` / `read_progress_total` / `read_progress_updated_at`), added through the
existing additive `_ensure_column` migration, so an existing 148 MB `state.db` upgrades in place
with no manual step.

While a read is in flight the manifest panel shows a dedicated bar and an `n / total courses`
counter. Each write also renews `last_heartbeat_at`, so a moving read can never trip the
stale-heartbeat alarm. Writes are throttled to one every two seconds; the final one always lands.

If persisting fails it is swallowed — observability must not be able to kill the read it is
observing.

## The one thing that is still unverified

**The progress regex is a best guess at GAM's stderr format** for `print course-participants`
(a trailing `(n/total)`). It has never been seen against real GAM output — that could not be tested
without a live tenant.

If the counter stays at zero during the benchmark, the read is still fine; an unmatched line is
simply ignored. To fix it, capture a few stderr lines and adjust `_ROSTER_READ_PROGRESS_PATTERN`
in `gamgui/core/connectors/gam_connector.py`. The pattern is deliberately narrow — it also requires
the total to equal the requested course count, so a counter for some other unit of work is ignored
rather than displayed as a wrong total.

Independent of the UI, liveness can always be confirmed from the process itself:

```powershell
Get-CimInstance Win32_Process -Filter "Name='gam.exe'" |
  Select-Object ProcessId, ReadTransferCount, WriteTransferCount
```

A climbing `WriteTransferCount` means GAM is working. That is how the 3h15m read was confirmed
alive before any of this existed.

Remaining plan (verification cost removal, adaptive throttling, cached drift detection via
`gam print courses ... countsonly`, removal phases, two-lane activity lease) is in the approved
plan file outside the repo.
