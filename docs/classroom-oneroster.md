# Classroom OneRoster guided operations

The optional `classroom-oneroster` profile turns Classroom home into a guided import workspace.
It keeps the existing safety contract: fresh Directory and managed-course snapshots, immutable
manifests, explicit confirmation, batches of at most 50 actions, post-batch Classroom rereads, and
durable verified receipts. Monitoring and Recovery explain that contract; they do not bypass it.

## Guided flow

Only one guided import journey is active at a time. Completed steps remain reviewable, the current
step contains the available action, and later steps explain why they are locked. Starting another
journey requires reviewing the active saved work first.

The operator flow is:

1. Upload and validate a retained OneRoster source.
2. Review scope, class names, people, exclusions, and threshold holds.
3. Build a fresh live plan from concurrent Directory and managed-course snapshots.
4. Confirm the exact immutable manifest.
5. Apply, reread, and save batches of no more than 50 actions.
6. Review the final verified outcome or use protected Recovery when evidence is uncertain.

## Live monitoring

`/classroom/monitoring` is read-only. While an execution is active, its visible panel polls
`/classroom/monitoring/status` about every three seconds. The request reads only the local durable
SQLite record; it does not contact Google, start work, repeat work, or change a manifest. Overlapping
polls are dropped. Totals intentionally hold steady and then jump because progress advances only
after a batch has been checked and atomically saved.

The page leads with the current run's time forecast, verified pace, waiting changes, class count,
phase progress, and newest immutable batch. Operator-facing phases map actions as follows:

- **Class setup:** create, update, activate, and archive courses.
- **Teacher access:** add or remove teachers and transfer ownership.
- **Student roster:** add or remove students.
- **Other checked work:** future or unknown action kinds remain visible in the technical receipt.

A time estimate stays hidden as **Learning the pace…** until two verified batches establish a rate.
Completed runs show measured elapsed time. Failed, recovery-required, or stale runs hide the
estimate rather than presenting false precision.

The technical receipt is loaded on demand from `/classroom/monitoring/receipt`. It contains only
sanitized current-run timings, worker level, verification attempts, throttling count, and batch
statistics. It never includes tenant identifiers, action targets, raw stderr, or raw GAM output.
Worker level is automatic and is not an operator setting.

## Check-in and recovery states

An active worker refreshes its durable heartbeat during execution:

- Before 30 seconds, Monitoring shows the current saved progress without a reassurance banner.
- At 30 seconds, it shows a mild delayed-check-in notice and keeps polling.
- At three minutes, it hides ETA, warns against starting another import, and links directly to
  Recovery for reconciliation.

**Preview safe pause** is explanatory and read-only. Recovery owns the pause request. A safe pause
finishes and verifies the active batch, saves its exact stopping point, and pauses before the next
batch. Confirmed actions are not repeated.

Recovery does not promise instant transactional undo. Compensating rollback creates a new immutable
inverse plan only where current Classroom state still matches recorded evidence. Newly created
courses are archived rather than deleted. Drifted or ambiguous actions require manual review.
Checkpoint restoration is reserved for incidents and depends on retained pre-mutation affected-
course evidence.

## Release and cross-platform update boundary

Merge through a checked pull request to protected `district-main`; do not push directly to the
protected branch. A green pull request is source-control evidence, not installed-app evidence.
After merge, post-merge validation must publish the exact merged SHA to `update-ready`. The ref does
not advance unless macOS and Windows tests, both fixed profile builds, pinned GAM checks, offline
self-tests, activation, and rollback contracts pass for that same SHA. Only then can either local
updater discover and build the revision.

`update-ready` is the routine local-build channel. Official public macOS releases remain manual,
tag-based, signed, and notarized. Windows routine installs are per-user and use the pinned local
`GamGUI Local` certificate created during the first manual bootstrap; it provides user/machine
continuity, not public SmartScreen reputation. The bootstrap consent, actual installed SHA, selected
profile, bundled GAM, local signature, self-test, runtime health, and preserved data must still be
verified separately on each computer.

The local sketches under `.planning/sketches/` are disposable review artifacts and are not part of
the application or release.
