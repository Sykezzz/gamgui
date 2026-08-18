
# GamGUI

[![CI](https://github.com/Sykezzz/gamgui/actions/workflows/ci.yml/badge.svg?branch=district-main)](https://github.com/Sykezzz/gamgui/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Sykezzz/gamgui/actions/workflows/codeql.yml/badge.svg?branch=district-main)](https://github.com/Sykezzz/gamgui/actions/workflows/codeql.yml)
[![Post-merge validation](https://github.com/Sykezzz/gamgui/actions/workflows/post-merge-validation.yml/badge.svg?branch=district-main)](https://github.com/Sykezzz/gamgui/actions/workflows/post-merge-validation.yml)
[![Upstream sync](https://github.com/Sykezzz/gamgui/actions/workflows/upstream-sync.yml/badge.svg?branch=district-main)](https://github.com/Sykezzz/gamgui/actions/workflows/upstream-sync.yml)
[![Windows prerelease](https://github.com/Sykezzz/gamgui/actions/workflows/windows-prerelease.yml/badge.svg?branch=district-main)](https://github.com/Sykezzz/gamgui/actions/workflows/windows-prerelease.yml)

A free, local, open-source **macOS and Windows GUI for [GAM7](https://github.com/GAM-team/GAM)** — administer
Google Workspace (users, groups, signatures, delegates, vacation responders, reports, and more)
without memorizing CLI commands, with credentials kept in the operating system's user credential
store.

> GAM exposes far more of Google Workspace than the Admin Console surfaces (Gmail
> signatures/delegates/forwarding, advanced group settings, bulk operations, reporting). GamGUI
> puts a safe, native front end on top of it.

<!--
Screenshots: drop PNGs into docs/screenshots/ (see that folder's README for the recommended shots),
then uncomment these to show them here:
![Users](docs/screenshots/users.png)
![Signature designer](docs/screenshots/signatures.png)
![Calendars](docs/screenshots/calendars.png)
![Offboarding](docs/screenshots/lifecycle.png)
-->

## About This Fork

This repository began as a fork of [goetchstone/gamgui](https://github.com/goetchstone/gamgui), a
free macOS GUI for GAM7. My district-focused branch adds and maintains:

- Windows packaging, installation, updates, and rollback
- Google Classroom administration and roster reconciliation
- OneRoster ingestion, planning, manifests, gates, and recovery
- District-scale indexing and bounded execution controls
- Drive administration and ownership workflows
- Expanded auditing, activity tracking, security controls, and CI

See [MY-CONTRIBUTIONS.md](MY-CONTRIBUTIONS.md) for the full breakdown of what's upstream versus
what I built, and [DEVELOPMENT-PROCESS.md](DEVELOPMENT-PROCESS.md) for how changes (including
AI-assisted ones) get reviewed, tested, and authorized before touching a live tenant.

## Why this fork exists

- **171 commits ahead of upstream**, including a Windows platform (packaging, installer, updater)
  that did not exist before.
- **First-party OneRoster Import Studio** for district-scale (~25,000 user) Classroom provisioning,
  with immutable manifests, additions-only planning, and a student-enrollment release gate.
- **Every destructive action is guarded and audited** — preview → typed confirmation → audit log —
  with a documented, evidence-based [live verification status](docs/live-verification.md) per
  operation rather than a blanket "trust the tests" claim.
- **A fail-closed cross-platform local updater** with exact-SHA promotion gates, a checksum-pinned
  GAM binary, and automatic rollback on a failed activation health check.

## Status

Actively developed and used against live Google Workspace tenants. Working today:

- **Setup wizard** — either **import an existing GAM install** (it auto-detects `$GAMCFGDIR`,
  `~/.gam`, and its own setup dir, shows which credential files each one holds, and moves them into
  the Keychain) or follow the guided fresh GAM project / OAuth flow; then the manual
  domain-wide-delegation step and a verify.
- **Users** — fast list/search/detail (cached + paginated), profile editing (title/department —
  location is shown but not editable) with a bulk "assign store" tool, mailbox **delegates**,
  **vacation responders**, group membership, per-user calendar sharing (grant/revoke access to that
  person's own calendar), **sign out everywhere**, and a guarded **suspend**.
- **Gmail signatures** — a scoped designer with variables, saved templates, a live preview, and
  bulk apply with a live per-user ✓/✗ feed as each signature gets set.
- **Groups** — membership management, including a drag-and-drop board.
- **Calendars** — instant indexed search at district scale; list resources or a person's calendars,
  inspect and grant/revoke ACLs, subscribe an individual or every current group member with bounded
  background progress, search events, and remove a stray event or orphaned secondary calendar.
- **Classroom** — locally indexed course search, provisioned-course creation, metadata and lifecycle
  changes, guarded owner transfer, and exact-preview teacher/student roster reconciliation.
- **Drive** — bounded per-user file search, metadata and sharing administration, safe file previews,
  and exact-manifest ownership transfer for a file, folder, or supported Classroom claim.
- **Lifecycle** — guided onboarding runbooks and a guarded offboarding routine (reset password →
  delegate → auto-responder → transfer Drive and calendars → unsubscribe calendars → manager
  reminder), with previews and progress evidence.
- **Command Builder** — search the categorized GAM catalog; curated commands get typed slots,
  guarded preview/run, sequencing, bounded interactive results and CSV download, plus explicit
  Google Sheet export for complete results.
- **Reports and audit** — 2SV gaps, inactive or suspended accounts, admins, missing recovery,
  storage/mail usage, directory completeness, and an incrementally indexed local audit trail.
- **Cross-platform local updater** — an installed macOS or per-user Windows app prepares only an
  exact `district-main` commit that also owns `update-ready`. Both platforms must pass the same
  exact-SHA promotion gate. Each updater verifies its locally signed bundle and offline self-test
  without reading Workspace credentials, then rolls back the app and local databases if activation
  health fails.

You build and run it yourself; it is not yet notarized for distribution to other Macs.

> **Destructive actions are guarded — but check what has actually been proven live.** Suspend,
> account delete, calendar/event delete, data transfer, the offboarding routine, and bulk operations
> all run behind a *preview → typed confirmation → audit-logged* path. That guard is well covered by
> tests; what tests cannot prove is that a given GAM command behaves as expected against a real
> tenant. See [Live verification status](docs/live-verification.md) for which writes have been
> confirmed against a production domain and which have not — and run anything in the second list
> once on a **throwaway user/event/calendar** before you rely on it. Account deletion is reversible
> only within Google's ~20-day window. GamGUI is provided **as-is under the MIT License, with no
> warranty — use at your own risk**; you are responsible for what you run against your own tenant.

## Design goals

- **Local & native** — a bundled `.app` on macOS or per-user application directory on Windows; no
  cloud service. The
  UI is served by a loopback-only local server on a random port, gated by a per-launch token (see
  [Security model](#security-model)).
- **Secure** — secrets live in the operating system's user credential store; GAM's plaintext files are
  materialized into a locked-down temporary directory only for the duration of each `gam`
  invocation, then wiped. ([details](#security-model))
- **Easy but powerful** — form/table UI for the common painful tasks, full GAM power underneath.
- **Connector-ready** — built around a connector protocol, so the Google Workspace connector is
  cleanly isolated and other systems could be added later without touching the UI.

## Architecture

```
HTMX views → FastAPI routes → Services → Connector protocol → GAMConnector
                                              → GAMRunner (subprocess)
                                              → SecretsVault (Keychain) + EphemeralConfig (temp GAMCFGDIR)

Bounded selectors/reports → domain-isolated SQLite indexes → background snapshot refresh
Classroom/Drive writes   → live re-read → exact preview/manifest → guarded apply → audit
```

Wrapped in a `pywebview` native window (WKWebView). See [CONTRIBUTING.md](CONTRIBUTING.md) for the
layout and conventions, and [docs/builder-commands.md](docs/builder-commands.md) for the Builder
catalog.

## Security model

GAM stores credentials as plaintext files (`client_secrets.json`, `oauth2.txt`,
`oauth2service.json`) in its config dir. `oauth2service.json` can impersonate **any** user in the
domain and `oauth2.txt` is effectively an admin password, so GamGUI:

1. keeps the canonical copies in the **Keychain** (`keyring`, device-bound, not synced);
2. materializes them into a `chmod 700` temp dir (files `chmod 600`) set as `GAMCFGDIR` only for
   each `gam` call;
3. wipes that dir on completion (success or failure) — and, because "the app quit mid-call" is the
   case that actually strands plaintext credentials, also via an `atexit` hook, a graceful-shutdown
   timeout that lets in-flight calls unwind, and an owner-PID marker so a later run can collect a
   directory whose owning process is gone;
4. writes refreshed OAuth tokens back to the Keychain.

Beyond the credentials themselves:

- **The local server is not open to other local processes.** It binds loopback on a random port and
  requires a per-launch token — and because cookies are *not* port-scoped (so `SameSite` alone would
  treat every port on `127.0.0.1` as the same site), it also rejects cross-origin callers outright.
- **GAM is never invoked through a shell.** Every command is an explicit argv list built by
  `GAMCommands`; user input is always a single list element, never string-interpolated.
- **Every mutation is guarded and audited** — `guard.evaluate()` classifies risk and resolves the
  concrete affected set for a preview, and the write is appended to a local audit log.
- **The vendored `gam` binary is checksum-pinned and verified fail-closed.** A release asset with no
  committed pin is refused rather than installed, since a swapped binary would inherit
  domain-wide impersonation.

## Build from source

Requirements: **Python 3.10+** and **uv 0.11.7** on macOS or Windows (the test suite also runs on
Linux). No Google credentials are needed to build or test. The exact
Python dependency graph, including PyInstaller 6.20.0, is committed in `uv.lock`; `make setup`
fails rather than silently updating it.

```bash
git clone <repo-url> && cd gamgui
make setup     # create/sync .venv from the frozen uv.lock
make gam       # vendor the pinned GAM7 binary into gamgui/resources/gam7 (needs network)
make test      # offline test suite — uses a mock gam, no binary/credentials required
make run       # launch the app (native window; prints a browser URL if pywebview is absent)
```

`make help` lists all targets. The supported reproducible path is `make setup`; direct editable
`pip install` commands bypass `uv.lock` and are intended only for dependency development. After an
intentional dependency change, regenerate and review the lock with exactly `uv 0.11.7`, then run
`uv lock --check` and the complete offline suite.

The GAM7 binary is **not committed** (platform-specific, large) — `make gam` / `scripts/fetch_gam.sh`
fetches the pinned version (`v7.47.06`) from the official releases and records its checksum.

`make setup` auto-selects a usable Python 3.10+ when `PYTHON` is unset; set
`PYTHON=/path/to/python` to choose one explicitly.

### Build a standalone `.app` (macOS)

```bash
make app PROFILE=core                 # Core Classroom/Drive administration
make app PROFILE=classroom-oneroster  # Core plus OneRoster Import Studio
```

Each command builds `dist/GamGUI.app` from the selected sealed profile. For distribution to other
Macs, both profiles must be signed and notarized independently, including the embedded GAM binary;
running them on the managed development Mac uses the stable local signing identity described
below.

### Build a Windows bundle or Setup wizard

Windows supports the same fixed `core` and `classroom-oneroster` profiles. A developer build uses
the locked environment and checksum-pinned GAM payload:

```powershell
uv sync --frozen --python (Get-Command python).Source --extra dev --extra desktop --extra build
.\scripts\build_windows_release.ps1 -Profile core
.\scripts\build_windows_release.ps1 -Profile classroom-oneroster
```

The first-install deliverable is one native, offline Inno Setup wizard containing both profiles and
the committed, checksum-pinned MinGit and uv archives. Classroom + OneRoster is selected by default.
It installs the app under `%LOCALAPPDATA%\Programs\GamGUI\current`, keeps data under
`%LOCALAPPDATA%\GamGUI`, and copies `GamGUIUpdater.exe` outside the replaceable application
directory. The Setup executable is an unsigned manual trust boundary: verify its release SHA-256
before using SmartScreen's **More info > Run anyway** path. The wizard explains and requires
consent before it creates the ten-year, non-exportable RSA-3072 `GamGUI Local` identity or adds its
public certificate to the current user's Trusted Root and Trusted Publisher stores.

After Setup, updates are automatic local builds signed by that pinned identity. The updater
will not accept a changed certificate, unsigned executable, altered bundle manifest, stale SHA, or
backward/non-descendant revision. Do not delete or rotate `GamGUI Local` manually. Use the bundled
Apps & Features uninstall removes the app, helper, toolchain, shortcuts, and local certificate while
preserving application data unless **Also delete local application data** is explicitly selected.

First install is offline. Later update preparation needs GitHub, Python-package, and GAM-release
access. Allow enough disk space for the source checkout, build environment, current app, pending
app, rollback copy, and an additional 256 MiB safety reserve; 4 GiB free is a practical minimum. A
network, pin, signing, space, or self-test failure leaves the installed application unchanged. See
the complete [Windows setup, SmartScreen, GPO, recovery, and release guide](docs/windows-setup.md).

### Stop the Keychain prompts

By default the app is **ad-hoc signed**, so macOS treats each rebuild as a new identity and
re-prompts for the Keychain on every launch — and "Always Allow" never sticks. The fix (no Apple
Developer account needed — that's only for shipping the app to *other* people's Macs) is a **stable
self-signed code-signing cert** named `GamGUI Local`. Once it exists in your login keychain,
`scripts/build_app.sh` signs with it **automatically**, so your one-time **Always Allow** persists
across launches *and* rebuilds.

Create it once, either way:

- **GUI:** Keychain Access → *Certificate Assistant → Create a Certificate…* → name it `GamGUI
  Local`, Identity Type **Self-Signed Root**, Certificate Type **Code Signing**.
- **CLI:**
  ```bash
  D=$(mktemp -d)
  printf '[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n[dn]\nCN=GamGUI Local\n[v3]\nbasicConstraints=critical,CA:false\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=critical,codeSigning\n' > "$D/c.cnf"
  openssl req -x509 -newkey rsa:2048 -keyout "$D/k.pem" -out "$D/c.pem" -days 3650 -nodes -config "$D/c.cnf"
  openssl pkcs12 -export -inkey "$D/k.pem" -in "$D/c.pem" -out "$D/id.p12" -passout pass:gamgui-local -name "GamGUI Local"
  security import "$D/id.p12" -P gamgui-local -T /usr/bin/codesign && rm -rf "$D"
  ```

Then rebuild the desired profile. The first launch still asks once **per credential** — click
**Always Allow** on each — and you won't be prompted again, even after future rebuilds. (Override
the cert name with `CODESIGN_IDENTITY=…`. The cert is local and not trusted for distribution by
design — it only quiets your own Keychain.)

The app also caches the three secrets in-process for a sliding window (default 5 min) so a burst of
actions doesn't re-prompt; tune with `GAMGUI_SECRET_CACHE_TTL` (seconds; `0` disables).
On macOS, refreshed OAuth data updates the existing Keychain item in place so its per-item
**Always Allow** authorization survives token refreshes.

## Before you point this at a live tenant

Building, testing, and local development never touch Google and need no approval. **Before any
live Google-side authorization, OAuth/Domain-Wide Delegation change, updater canary, or mutation
against a real domain, read [docs/change-control.md](docs/change-control.md)** — it covers the
required administrator sign-off, the district setup and acceptance runbook, and the approval
checkpoints in order. Skipping it is how you end up authorizing scopes or running a canary without
the recorded sign-off this project expects.

## Detailed operations reference

The sections above are the overview; the following documents carry the depth:

- [docs/change-control.md](docs/change-control.md) — district change-control gate and the full
  setup/acceptance runbook.
- [docs/live-verification.md](docs/live-verification.md) — which mutations are confirmed live
  against a production tenant versus still unproven.
- [docs/district-operations.md](docs/district-operations.md) — performance and local-index
  behavior, Classroom/Drive administration detail, enforced bounds, the district branch topology,
  staying current with GAM, the fail-closed updater's internals, tests/CI, and email-signature
  hosting.
- [docs/windows-setup.md](docs/windows-setup.md) — Windows setup, SmartScreen, GPO, and recovery.
- [docs/classroom-oneroster.md](docs/classroom-oneroster.md) — guided OneRoster → Classroom flow.
- [docs/builder-commands.md](docs/builder-commands.md) — the Command Builder catalog.
- [docs/drive-integration.md](docs/drive-integration.md) — Drive administration detail.

## Where this is going

[ROADMAP.md](ROADMAP.md) — the ranked backlog, plus the trade-offs behind what it does *not* do yet.
[CONTRIBUTING.md](CONTRIBUTING.md) — layout, conventions, and how to add a Builder command.
[SECURITY.md](SECURITY.md) — threat model, the invariants the code is expected to hold, and how to
report a vulnerability privately.

## License

MIT — see [LICENSE](LICENSE).
