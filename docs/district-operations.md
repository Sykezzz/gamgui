# District operations reference

Detailed operational behavior: performance/local-index design, Classroom and Drive administration,
enforced bounds, the district branch topology, staying current with GAM, the fail-closed updater
internals, tests/CI, and email-signature hosting. This is the deep-dive reference behind the
top-level [README](../README.md) — start there for the overview.

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

Classroom Teacher Access is a separate Classroom page for the domain's existing special Classroom
Teachers group. Its live directory pickers can combine multiple AD/GCDS-synchronized Google groups
with active users from one or more organizational units, including child OUs. CSV remains an
explicit alternative source. Every source or exception change creates a fresh exact preview;
source membership is re-read before apply, and scheduled runs hold on drift or safety thresholds.

### Drive

Drive search is scoped to one delegated user and their owned, non-trashed My Drive files. GamGUI can
show narrow metadata and permissions, edit metadata, add/update/remove eligible internal-user or
internal-group permissions, preview supported content, and transfer ownership after a live
capability/owner check. Owner and inherited permissions are not editable here.

Single-file and recursive ownership transfers require the exact destination email. Recursive
folder transfers and Classroom ownership claims create persistent exact-file manifests, process
targets serially, and retain per-file status for retry/review. Shared Drive content is
organization-owned and is never transferred or claimed by these workflows.

### Optional OneRoster component

OneRoster Import Studio is a first-party optional application profile. The `core` profile keeps
all ordinary Classroom and Drive administration but omits the OneRoster executable package,
templates, and migrations. The `classroom-oneroster` profile adds validated OneRoster ZIP
ingestion, import thresholds, immutable district manifests, and the student-enrollment release
gate. Each retained import also has a local class-naming step with common schemes and a safe custom
template using `{course_title}`, `{class_code}`, `{class_title}`, and `{school_year}`. Naming
changes rebuild the bounded preview and GAM-ready exports without changing stable `Section_`
aliases or contacting Google.

New installations offer the component during first-run setup. It can also be installed, disabled,
enabled, or removed from **Settings → Components**. Installing or removing it stages a complete
matching GamGUI bundle and activates it on restart; GamGUI never loads downloaded Python from
Application Support. Component discovery and installation do not contact Google or read Workspace
credentials.

Removing the component preserves its protected audit and import state. Raw and normalized
OneRoster snapshots retain their 30-day expiry and are purged by Core. Permanent local data removal
is a separate typed-confirmation action.

See [Classroom OneRoster guided operations](classroom-oneroster.md) for the guided flow,
verified-step Monitoring behavior, heartbeat escalation, safe pause, Recovery, sanitized receipts,
and the cross-platform exact-SHA update boundary.

### Enforced bounds and confirmation rules

| Surface | Enforced bound or confirmation |
| --- | --- |
| Directory, Classroom, and Drive pages | Maximum 50 results per page |
| Builder target picker / sequence | 25 picker matches; 25 command steps |
| Report detail / audit | 50 report rows; 25 audit rows by default and 50 maximum |
| Classroom roster upload | 1,000,000 bytes maximum |
| Classroom roster reconciliation | 200 total adds plus removals per preview |
| OneRoster live planning | 50,000 classes, 300,000 active source enrollments, and 300,000 resulting actions; larger valid snapshots remain inspectable/exportable |
| OneRoster live roster snapshot | 500,000 deduplicated Classroom memberships |
| Drive content preview | 10 MiB maximum and only the supported safe MIME types |
| Drive folder/claim manifest | 500 exact files maximum |
| Generic action guard | Bulk starts at 10 targets; destructive bulk requires typed confirmation; over 200 is an explicit warning threshold, not an automatic refusal |

These controls reduce blast radius; they do not replace administrator approval, a reviewed target
list, a small pilot, or post-change verification.

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
40-character commit SHA. That workflow rejects a moving/mismatched branch, runs Linux, macOS, and
Windows tests on Python 3.10/3.12/3.14, checks GAM contracts, and builds/self-tests both profiles on
macOS and Windows. Windows validation uses an ephemeral runner certificate and labels its bundles
validation-only. It publishes `update-ready` only when every platform gate passes. A green PR check
alone is not local-update evidence; the updater requires `update-ready` on the exact current
`district-main` SHA.

### Staying current with GAM (and not breaking on updates)

GamGUI pins a tested GAM7 version — `EXPECTED_GAM_VERSION` in `gamgui/core/gam/commands.py`, matched by
`scripts/fetch_gam.sh`. The tested pin is currently **GAM 7.47.06**. The running app never downloads
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

### Fail-closed cross-platform local updater

The updater runs only from an installed macOS `GamGUI.app` or Windows
`%LOCALAPPDATA%\Programs\GamGUI\current`; a source checkout or headless development server does not
self-update.

1. At app startup, a background worker checks the head of `district-main`. It defers while an
   administrative job or Classroom/Drive manifest is active.
2. It accepts only a new, non-blocklisted 40-character SHA whose completed check runs include a
   successful `update-ready` for that same SHA.
3. It clones and checks out that exact SHA detached, requires exactly `uv 0.11.7`, synchronizes the
   committed frozen lock, fetches checksum-pinned GAM, and builds the selected profile. Windows uses
   the bundled, version-checked MinGit and uv toolchain; macOS uses its existing local builder. It
   never falls back from `classroom-oneroster` to `core`. It requires the pinned local `GamGUI Local`
   signing identity, verifies the signature and complete bundle manifest, and runs the offline
   self-test.
4. Exact-SHA CI, the sealed artifact identity, local signing verification, and the bundled offline
   self-test establish automatic-update readiness. The automatic startup path does not run the live
   Workspace canary or read operating-system credentials.
5. A passing build is staged while the current app keeps running. On the next launch, a native
   dialog explains the restart and lets the administrator install now or defer. If accepted, a
   helper snapshots the current app and all local SQLite databases, tests schema preparation on a
   copy, swaps the bundle, and obtains a startup health marker from a hidden verification window
   within 45 seconds before reopening the normal app. Windows uses a per-user named mutex and a
   detached helper so no process inside `current` replaces itself.
6. If the helper's migration-copy self-test, bundle swap, or startup health check fails, it restores
   the prior app and database snapshot, relaunches the old app, and blocklists that SHA. Successful
   activation keeps at most two rollback backups, and backups older than 30 days are pruned.

Network, toolchain, signing, or self-test preparation failures leave the installed version untouched
and are retryable; they do not blocklist the SHA. The app shows only a generic local notice, not
paths, commands, or tenant data. A user-initiated verified-file update separately runs the four
bounded, read-only canary probes in a disposable app-data directory and requires prior administrator
approval for the configured canary subject. The updater never changes OAuth/DWD scopes, Admin Console
policy, or tenant data, and it never treats a missing/failed required canary as approval to proceed.

After `update-ready` advances, verify an actual computer separately. On Windows, inspect
`%LOCALAPPDATA%\GamGUI\updates\state.json` and `%LOCALAPPDATA%\Programs\GamGUI\current.artifact.json`,
run `GamGUI.exe --self-test --json`, confirm `gam.exe version`, and use `Get-AuthenticodeSignature`
plus the pinned signer SHA-256. On macOS, inspect the corresponding Application Support state and
artifact receipt, run the bundle self-test and bundled GAM version, and verify `codesign`. In both
cases the installed source SHA, profile, component digest, architecture, GAM version, signature,
self-test, runtime health, and preserved application data must agree before calling the machine
updated.

### Tests & CI

`pytest` is fully offline (mock GAM + in-memory secret store). CI runs it on Ubuntu, macOS, and
Windows across Python 3.10, 3.12, and 3.14 — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

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
