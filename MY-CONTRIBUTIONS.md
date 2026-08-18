# My contributions

GamGUI began as [goetchstone/gamgui](https://github.com/goetchstone/gamgui), a free, open-source
macOS GUI for GAM7. This document is a credible accounting of what came from upstream versus what
I built and maintain on the `district-main` branch, so a reviewer doesn't have to take the fork
relationship on faith.

## Upstream baseline

The original project provided a native macOS app (pywebview + FastAPI/HTMX) for core Google
Workspace administration: users, groups, Gmail signatures, delegates, vacation responders, and
basic reporting, with credentials stored in the macOS Keychain. That baseline is still visible in
`main`, which is kept as a no-force mirror of upstream — district-only code never lands there.

## Major architectural additions

- **Windows platform.** Upstream was macOS-only. I added a full Windows build: an Inno Setup
  installer, a `%LOCALAPPDATA%`-based install/data layout, a bundled MinGit/uv toolchain, a
  ten-year self-signed `GamGUI Local` code-signing identity, and a fail-closed cross-platform local
  updater that both platforms must pass the same exact-SHA promotion gate to use.
- **OneRoster Import Studio.** A first-party optional application profile that ingests validated
  OneRoster ZIP exports, normalizes district data, preserves stable `Section_<SectionID>` aliases
  across renames, and generates bounded Classroom provisioning plans — separating teacher
  preparation from a gated, explicit student-enrollment release.
- **District-scale indexing.** Persistent, domain-isolated SQLite snapshots for the user/group
  directory and the Classroom course index (the upstream app queried Google live on every page),
  with capped page sizes and a background refresh cadence so the app stays usable at ~25,000 users.
- **Guarded mutation framework.** `guard.evaluate()` classifies risk and resolves the concrete
  affected set for every destructive action, driving a preview → typed confirmation → audit-logged
  path used consistently across Users, Groups, Classroom, and Drive — not just the actions upstream
  already guarded.
- **Classroom and Drive administration.** Neither existed upstream. I built course lifecycle
  management, exact-preview roster reconciliation, the Classroom Teacher Access group-membership
  workflow, and Drive metadata/sharing/ownership-transfer administration with per-file manifests.
- **District branch topology and CI.** The `main` / `district-main` split, the upstream-sync
  workflow that opens PRs from upstream `main` into a district integration branch, exact-SHA
  post-merge validation across Linux/macOS/Windows and three Python versions, CodeQL static
  analysis configured in-tree, and the automated GAM-version pin/bump workflow.

## Modules I own

`gamgui/core/gam/` (command builders and the GAM subprocess runner), the OneRoster ingestion and
planning pipeline, the guard/audit framework, the Windows installer and updater tooling
(`scripts/build_windows_release.ps1`, the updater helper), the SQLite index layer, and the CI/CodeQL/
release/upstream-sync/gam-update GitHub Actions workflows.

## Safety decisions

Every mutation is guarded and audited by design, not by convention — the guard framework resolves
the affected set before a single write happens. The vendored `gam` binary is checksum-pinned and
refused if unpinned. Credentials are materialized to a locked-down temp directory only for the
duration of a `gam` call and wiped on exit (including on crash, via an `atexit` hook and an
owner-PID marker). OneRoster planning is additions-only with a separate, explicit gate for anything
that changes student enrollment. See [docs/change-control.md](docs/change-control.md) for the
administrator sign-off process this project requires before any of that runs against a live tenant.

## Test strategy

The offline `pytest` suite (mock GAM, in-memory secret store) runs on Ubuntu, macOS, and Windows
across Python 3.10/3.12/3.14 on every PR. A separate, explicit live acceptance script
(`scripts/acceptance.py`) performs four bounded, read-only canary probes against a real tenant, and
is never run automatically — it requires recorded administrator sign-off. [Live verification
status](docs/live-verification.md) tracks, from real audit logs, which mutating operations have
actually been exercised against a production domain versus which are covered by tests but not yet
proven live.

## Current limitations

Not notarized for distribution to other Macs (you build and run it yourself). Several mutations
(remove delegate, clear vacation, add group member, sign-out-everywhere, delete event, delete user,
and the group fan-out of a calendar share) are tested but not yet confirmed against a live tenant —
see the live-verification doc before relying on them. The app is not yet packaged with a
demonstration/mock-data mode; that's tracked in [ROADMAP.md](ROADMAP.md).

## Contribution timeline

`district-main` is currently 171 commits ahead of the upstream comparison point. The branch history
and `.github/workflows/upstream-sync.yml` runs are the source of truth for exact dates; this
document describes the shape of the work rather than duplicating commit-by-commit history that's
already in `git log`.
