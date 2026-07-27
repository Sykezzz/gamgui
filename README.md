
# GamGUI

A free, local, open-source **macOS GUI for [GAM7](https://github.com/GAM-team/GAM)** — administer
Google Workspace (users, groups, signatures, delegates, vacation responders, reports, and more)
without memorizing CLI commands, with your credentials kept in the macOS **Keychain**.

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

## District change-control gate

Local source review, offline tests, documentation, and app builds do not contact Google and can be
done without a live-change approval. **Stop and obtain recorded administrator sign-off before any
live Google-side authorization, OAuth or Domain-Wide Delegation scope/policy change, updater
canary, pilot, or mutation.** The approval must identify the Workspace tenant, the delegated canary
subject (when applicable), the exact scopes or operation, the target set, and the approved window.
A read-only canary is still a live Google-side action and is included in this gate.

For an installed app, sign-off may explicitly authorize the recurring four-probe read-only canary
used for validated updates. Without that standing approval, do not complete the canary
configuration or run the local updater. An approval for setup or canary reads does not authorize
Classroom roster changes, Drive ownership/sharing changes, or any other mutation; obtain a separate
mutation or bounded-pilot approval.

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
- **Local updater** — on an installed macOS app, prepares only an exact `district-main` commit that
  has the `update-ready` check, runs bundle/self-tests plus the approved bounded canary, and rolls
  back the app and local databases if activation health fails.

You build and run it yourself; it is not yet notarized for distribution to other Macs.

> **Destructive actions are guarded — but check what has actually been proven live.** Suspend,
> account delete, calendar/event delete, data transfer, the offboarding routine, and bulk operations
> all run behind a *preview → typed confirmation → audit-logged* path. That guard is well covered by
> tests; what tests cannot prove is that a given GAM command behaves as expected against a real
> tenant. See [Live verification status](#live-verification-status) for which writes have been
> confirmed against a production domain and which have not — and run anything in the second list
> once on a **throwaway user/event/calendar** before you rely on it. Account deletion is reversible
> only within Google's ~20-day window. GamGUI is provided **as-is under the MIT License, with no
> warranty — use at your own risk**; you are responsible for what you run against your own tenant.

### Live verification status

Every write is audited, so this list is derived from real audit logs rather than memory. "Confirmed
live" means the operation has succeeded at least once against a production Google Workspace domain.

**Confirmed live:** calendar share (ACL) · calendar auto-subscribe (making a shared calendar appear
in someone's sidebar) · add calendar event · delete calendar · add delegate · remove group member ·
reset password · set organization fields · set signature · set vacation · transfer data · plus all
reads (a read-only pass over the parsers ships as `scripts/acceptance.py`).

**Not yet confirmed live** — treat as unproven and test on a throwaway target first: unshare a
calendar (remove ACL) · remove delegate · clear vacation · add group member · sign out everywhere ·
delete event · delete user · the group fan-out of a calendar share (the individual calls it makes —
ACL add and subscribe — are each confirmed live, but the group expansion itself is not) · and the two
offboarding repairs described below.

**Known-good repairs awaiting live re-run.** Two offboarding bugs were found in real audit logs and
fixed, but the fixes have not themselves been exercised live yet: Drive and calendar are now
transferred in a *single* data-transfer call (two separate calls collided with a `409 conflict`),
and "remove from everyone's calendars" now tolerates the `cannotChangeOwnAcl` error that used to
abort the sweep.

## Design goals

- **Local & native** — a single bundled `.app`; no cloud service, nothing leaves your machine. The
  UI is served by a loopback-only local server on a random port, gated by a per-launch token (see
  [Security model](#security-model)).
- **Secure** — secrets live in the macOS Keychain; GAM's plaintext credential files are
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

## Performance and local-index behavior

GamGUI avoids putting full-tenant payloads into ordinary pages:

- The user/group directory index is a persistent, domain-isolated SQLite snapshot containing only
  list/search fields. The first request builds the required snapshot; after 15 minutes it serves
  the existing snapshot while one background refresh runs. User detail remains a live exact lookup.
- The Classroom course index is also domain-isolated and persistent. Course pages search locally,
  return at most 50 rows, and schedule one background refresh when the 15-minute snapshot is stale.
  Course details, owners, and rosters are re-read live before a change.
- Drive does not cache a tenant-wide content listing. It queries one delegated user with narrow
  fields and cursor pagination, capped at 50 files per page.
- Directory and Classroom list pages are capped at 50. Builder pickers show at most 25 matches.
  A selected report finding returns at most 50 users. The audit page defaults to 25 entries and
  cannot request more than 50.
- Report summaries aggregate directly in SQLite. The audit index advances from the last indexed
  JSONL byte rather than re-reading the complete audit log on every page.

Indexes are derived local data, not credentials, but they can contain district identifiers. Keep
the app-data directory and backups owner-only. Deleting a directory or Classroom index is
recoverable—the next refresh rebuilds it from Google—but deleting the append-only audit JSONL is
not.

## Classroom and Drive administration

### Classroom

Classroom search is local and paginated; selection loads live detail and roster data. GamGUI can
create a course in `PROVISIONED`, edit writable course metadata, activate a provisioned course,
archive/reactivate an active course, transfer ownership to a verified active internal user, and
add/remove teachers or students. Direct enrollment is restricted to the connected domain.

Bulk roster input accepts pasted text or CSV, computes an exact add/remove diff, and persists a
restart-safe manifest. Apply re-reads the live roster and refuses a stale preview. A preview cannot
be reused; removals require typing the exact course ID. Owner transfer requires both the exact
course ID and exact destination email. The course owner cannot be removed from the teacher roster.

### Drive

Drive search is scoped to one delegated user and their owned, non-trashed My Drive files. GamGUI can
show narrow metadata and permissions, edit metadata, add/update/remove eligible internal-user or
internal-group permissions, preview supported content, and transfer ownership after a live
capability/owner check. Owner and inherited permissions are not editable here.

Single-file and recursive ownership transfers require the exact destination email. Recursive
folder transfers and Classroom ownership claims create persistent exact-file manifests, process
targets serially, and retain per-file status for retry/review. Shared Drive content is
organization-owned and is never transferred or claimed by these workflows.

### Enforced bounds and confirmation rules

| Surface | Enforced bound or confirmation |
| --- | --- |
| Directory, Classroom, and Drive pages | Maximum 50 results per page |
| Builder target picker / sequence | 25 picker matches; 25 command steps |
| Report detail / audit | 50 report rows; 25 audit rows by default and 50 maximum |
| Classroom roster upload | 1,000,000 bytes maximum |
| Classroom roster reconciliation | 200 total adds plus removals per preview |
| Drive content preview | 10 MiB maximum and only the supported safe MIME types |
| Drive folder/claim manifest | 500 exact files maximum |
| Generic action guard | Bulk starts at 10 targets; destructive bulk requires typed confirmation; over 200 is an explicit warning threshold, not an automatic refusal |

These controls reduce blast radius; they do not replace administrator approval, a reviewed target
list, a small pilot, or post-change verification.

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

## District setup and acceptance runbook

This is the handoff order for a new administrator. Do not skip the approval checkpoints.

1. **Prepare locally.** Install Python, clone the repository, run `make setup`, vendor the pinned
   GAM build with `make gam`, and run `make test`. These steps need no tenant credentials.
2. **Record setup approval.** Before opening a Google authorization page, get administrator
   sign-off for the tenant, super-admin/canary subject, GAM OAuth setup, and the exact feature scopes
   below. Decide whether the approval covers recurring read-only updater canaries.
3. **Run setup.** In the Setup screen, either import an existing GAM credential directory or use
   the fresh flow. The fresh flow provides these GAM7 commands in this order:

   ```bash
   gam create project <super-admin-email>
   gam oauth create
   gam create svcacct
   ```

   Use the commands rendered by the app so the bundled binary path and private `GAMCFGDIR` are
   correct. GamGUI imports the resulting credentials into Keychain.
4. **Authorize Domain-Wide Delegation.** Preserve the scopes already created by GAM. Add exactly
   these district feature scopes—do not replace the existing list with only these four:

   ```text
   https://www.googleapis.com/auth/classroom.courses
   https://www.googleapis.com/auth/classroom.rosters
   https://www.googleapis.com/auth/classroom.profile.emails
   https://www.googleapis.com/auth/drive
   ```

   The setup screen renders the same comma-separated value and the service-account client ID.
   Changing these scopes or any Admin Console policy requires the recorded sign-off from step 2.
5. **Verify setup.** Click **Verify access**. GamGUI first verifies GAM's existing service-account
   authorization, then checks those four feature scopes. A passing verification activates the
   connector and stores the approved canary subject locally for updater use.
6. **Approve and run the live acceptance pass.** A live canary requires explicit sign-off even
   though it is read-only:

   ```bash
   .venv/bin/python scripts/acceptance.py
   ```

   The fixed denominator is one exact user read with two projected fields, one Directory group page
   with `maxResults=1`, one Classroom course page with `pageSize=1`, and one Drive file page with
   `page_size=1`, plus a local GAM-version check. Output contains only fixed check names,
   PASS/FAIL, and elapsed milliseconds. Persisted canary evidence contains only check names,
   booleans, timings, and a timestamp—never tenant identifiers, full records, resource IDs, tokens,
   or exception text. Exit codes are `0` pass, `1` failed check, and `2` missing/incomplete canary
   setup.
7. **Approve a bounded pilot before mutations.** Use throwaway or specifically approved targets,
   start add-only where possible, review the exact preview/manifest, stay below the documented
   caps, and verify Google-side results before expanding. Setup or canary approval alone is not
   mutation approval.

## Build from source

Requirements: **Python 3.10+**, **uv 0.11.7**, and **macOS** (to run the native window; the test
suite itself runs on Linux too). No Google credentials are needed to build or test. The exact
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
fetches the pinned version (`v7.46.11`) from the official releases and records its checksum.

`make setup` auto-selects a usable Python 3.10+ when `PYTHON` is unset; set
`PYTHON=/path/to/python` to choose one explicitly.

### Build a standalone `.app` (macOS)

```bash
make app       # PyInstaller -> dist/GamGUI.app (bundles Python + the GAM7 binary)
```

For distribution to other Macs you must codesign + notarize the bundle (including the embedded gam
binary); running it yourself needs no signing.

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

Then rebuild (`make app`). The first launch still asks once **per credential** — click **Always
Allow** on each — and you won't be prompted again, even after future rebuilds. (Override the cert
name with `CODESIGN_IDENTITY=…`. The cert is local and not trusted for distribution by design — it
only quiets your own Keychain.)

The app also caches the three secrets in-process for a sliding window (default 5 min) so a burst of
actions doesn't re-prompt; tune with `GAMGUI_SECRET_CACHE_TTL` (seconds; `0` disables).
On macOS, refreshed OAuth data updates the existing Keychain item in place so its per-item
**Always Allow** authorization survives token refreshes.

### District branch topology

| Branch | Purpose |
| --- | --- |
| `main` | A no-force mirror of upstream `goetchstone/gamgui:main`. Do not land district-only code or deploy from this branch. |
| `district-main` | The protected default and deployment branch. It contains upstream plus the district upgrade and is the only branch the installed updater follows. |

`.github/workflows/upstream-sync.yml` advances the local `main` mirror, creates a temporary
integration branch from `district-main`, merges `main` there, and opens a PR back to
`district-main`. A conflict opens/updates an issue and leaves `district-main` unchanged. Upstream
sync and GAM-pin maintenance share the `district-maintenance` concurrency group, so those writers
run serially.

CI runs on integration PRs. A push to `district-main` dispatches post-merge validation for the exact
40-character commit SHA. That workflow rejects a moving/mismatched branch, runs the full Linux and
macOS test matrix, checks pinned/latest GAM command contracts, builds and self-tests the macOS app,
and publishes `update-ready` only when every required job passes. A green PR check alone is not
local-update evidence; the updater requires `update-ready` on the exact current
`district-main` SHA.

### Staying current with GAM (and not breaking on updates)

GamGUI pins a tested GAM7 version — `EXPECTED_GAM_VERSION` in `gamgui/core/gam/commands.py`, matched by
`scripts/fetch_gam.sh`. The tested pin is currently **GAM 7.46.11**. The running app never downloads
or substitutes an unpinned GAM binary:

- **Automated pin PR** (`.github/workflows/gam-update.yml`) compares the latest GAM release with the
  pin, runs `scripts/bump_gam.py`, downloads both supported macOS assets using GitHub-published
  SHA-256 metadata, regenerates the catalog/version contracts, runs focused tests, and opens an
  auto-merge PR to `district-main`. Branch protection and exact-SHA post-merge validation still
  gate deployment.
- **Compatibility checks** assert every GAM sub-command our builders use still exists in the
  vendored command reference. Post-merge validation checks both the pin and current latest release,
  so a renamed/removed command fails before an installed app can see `update-ready`.
- **Runtime self-check** — if the running `gam` differs from the tested version (e.g. a
  `GAMGUI_GAM_BINARY` override), the setup screen shows a soft warning. It never blocks.

**To perform or reproduce a GAM bump manually:**

1. Run `python3 scripts/bump_gam.py --tag vX.Y.Z`. It fails closed unless signed release metadata
   contains SHA-256 digests for both supported macOS architectures, then updates the checksum
   catalog, version constants, mock, README marker, vendored reference, and command catalog.
2. Review the diff and `gamgui/resources/gam7/GamUpdate.txt` for behavior or scope changes.
3. Run `pytest -q tests/test_command_contract.py tests/test_bump_gam.py`, then `make test`.
4. After separate administrator sign-off for a live canary, run
   `.venv/bin/python scripts/acceptance.py`.
5. Open the scoped PR to `district-main`; do not bypass protected-branch CI or exact-SHA post-merge
   validation.

### Fail-closed local updater

The updater runs only from an installed macOS `GamGUI.app`; a source checkout or headless
development server does not self-update.

1. At app startup, a background worker checks the head of `district-main`. It defers while an
   administrative job or Classroom/Drive manifest is active.
2. It accepts only a new, non-blocklisted 40-character SHA whose completed check runs include a
   successful `update-ready` for that same SHA.
3. It clones and checks out that exact SHA detached, requires exactly `uv 0.11.7`, synchronizes the
   committed frozen lock through `make setup`, then runs `make gam` and `make app`. It requires the
   local `GamGUI Local` signing identity, verifies the signature, and runs the bundled self-test.
4. It runs the four bounded, read-only canary probes in a disposable app-data directory. Only
   timing/status evidence is copied to the live updater state. This step contacts Google and
   requires prior administrator approval for the configured canary subject.
5. A passing build is staged while the current app keeps running. On the next launch, a helper
   snapshots the current app and all local SQLite databases, tests schema preparation on a copy,
   swaps the bundle, and requires a startup health marker within 45 seconds.
6. If the helper's migration-copy self-test, bundle swap, or startup health check fails, it restores
   the prior app and database snapshot, relaunches the old app, and blocklists that SHA. Successful
   activation keeps at most two rollback backups, and backups older than 30 days are pruned.

Network, toolchain, signing, or canary preparation failures leave the installed version untouched
and are retryable; they do not blocklist the SHA. The app shows only a generic local notice, not
paths, commands, or tenant data. The updater never changes OAuth/DWD scopes, Admin Console policy,
or tenant data, and it never treats a missing/failed canary as approval to proceed.

### Tests & CI

`pytest` is fully offline (mock gam + in-memory Keychain). CI runs it on Ubuntu and macOS across
Python 3.10, 3.12, and 3.14 — see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

**Static analysis.** CodeQL runs on every push and PR to `main`, plus weekly, over both the Python
code and the workflows themselves — configured in-tree so it is reviewable rather than hidden in
repository settings: [`.github/workflows/codeql.yml`](.github/workflows/codeql.yml) with
[`.github/codeql/codeql-config.yml`](.github/codeql/codeql-config.yml). It uses the broader
`security-extended` suite, and skips `tests/`, the vendored GAM release, and vendored browser
libraries — the config explains why for each.

## Email signatures

The **Signatures** screen designs one HTML signature with variables, previews it rendered for a real
person, and applies it in bulk — scoped to a single user (for testing), a group, an org unit, a
department, a location, or the whole company. Each user's current signature is also shown *rendered*
on their detail page.

**Template variables** (filled per user from the directory):
`{name}` `{first}` `{last}` `{email}` `{title}` (`{role}` is an alias) `{phone}` `{department}`
`{location}` `{ou}`. Wrap a fragment in `[[ … ]]` to drop it when a variable inside is empty — e.g.
`[[{title} · ]]` vanishes for people with no title, so one template can roll out before every profile
is filled in.

### Hosting signature images (logo, social icons)

Gmail does **not** allow inline/base64 images or Google Drive links in signatures — every image must
be a file at a **public HTTPS URL**. GamGUI is a local app and doesn't host images itself; you point
the template's `<img src="…">` at wherever you host them. Whatever host you choose, the URL must be:

- **HTTPS** and **anonymously reachable** — Gmail fetches images through its own proxy (no
  cookies/referer) and caches them. Test a URL in a private/incognito window; if it loads there,
  Gmail can fetch it.
- served with the correct **`Content-Type`** (`image/png`, …) and **no hotlink/referer protection**
  — referer-based protection is the usual cause of "the logo shows for me but not for recipients."
- **versioned by filename** when an image changes (`logo-2026.png`) — Gmail caches by URL, so
  overwriting the same name can keep serving the old one.

Size icons ~2× their display size and set explicit `width`/`height` on each `<img>`.

**Where to host — pick one:**

- **A web host you already have (simplest).** Drop the files in a public folder, e.g.
  `https://yourdomain.com/email/logo.png`. Done.
- **Google Cloud Storage** (Google-native; reuse the GCP project GAM created). Requires a **billing
  account** linked to the project — but small signature assets fall under the Always-Free tier, so
  the bill rounds to **$0**:
  1. Cloud Console → **Billing** → link a billing account to the project (if not already).
  2. **Cloud Storage → Create bucket** — globally-unique name, a US region, Standard class, Uniform
     bucket-level access.
  3. Make objects public: bucket **Permissions → Grant access → principal `allUsers` → role
     `Storage Object Viewer`**. (If your org enforces *Public access prevention*, allow it on this
     bucket.)
  4. Upload the images.
  5. Reference them at `https://storage.googleapis.com/<bucket>/<path>/logo.png`.
  (Pricing changes — confirm the current free-tier limits, but for a handful of small PNGs it is
  effectively free.)
- **GitHub + jsDelivr (free, no billing).** Commit the images to a public repo and serve them via the
  jsDelivr CDN: `https://cdn.jsdelivr.net/gh/<user>/<repo>@<branch>/path/logo.png`. CDN-fast, no card.
- **Cloudflare R2 / Amazon S3** — or any public-object store — also work.

## Where this is going

[ROADMAP.md](ROADMAP.md) — the ranked backlog, plus the trade-offs behind what it does *not* do yet.
[CONTRIBUTING.md](CONTRIBUTING.md) — layout, conventions, and how to add a Builder command.
[SECURITY.md](SECURITY.md) — threat model, the invariants the code is expected to hold, and how to
report a vulnerability privately.

## License

MIT — see [LICENSE](LICENSE).
