# District change-control gate and setup/acceptance runbook

This document is the full change-control policy for running GamGUI against a **live, production
Google Workspace tenant**. Local source review, offline tests, documentation, and app builds do not
contact Google and do not require anything on this page — it only applies once you point the app at
a real domain.

## District change-control gate

Local source review, offline tests, documentation, and app builds do not contact Google and can be
done without a live-change approval. **Stop and obtain recorded administrator sign-off before any
live Google-side authorization, OAuth or Domain-Wide Delegation scope/policy change, updater
canary, pilot, or mutation.** The approval must identify the Workspace tenant, the delegated canary
subject (when applicable), the exact scopes or operation, the target set, and the approved window.
A read-only canary is still a live Google-side action and is included in this gate.

For an installed app, sign-off may explicitly authorize the four-probe read-only canary for manual
acceptance or a user-initiated verified-file update. The automatic startup updater does not run that
canary or read Workspace credentials. Without that standing approval, do not complete the canary
configuration or initiate a verified-file update. An approval for setup or canary reads does not
authorize Classroom roster changes, Drive ownership/sharing changes, or any other mutation; obtain a
separate mutation or bounded-pilot approval.

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
   these district feature scopes—do not replace the existing list with only these six:

   ```text
   https://www.googleapis.com/auth/admin.directory.user.readonly
   https://www.googleapis.com/auth/classroom.courses
   https://www.googleapis.com/auth/classroom.rosters
   https://www.googleapis.com/auth/classroom.profile.emails
   https://www.googleapis.com/auth/admin.directory.group.readonly
   https://www.googleapis.com/auth/drive
   ```

   The setup screen renders the same comma-separated value and the service-account client ID.
   Changing these scopes or any Admin Console policy requires the recorded sign-off from step 2.
5. **Verify setup.** Click **Verify access**. GamGUI first verifies GAM's existing service-account
   authorization, then checks those six feature scopes. A passing verification activates the
   connector and stores the approved canary subject locally for manual acceptance or a
   user-initiated verified-file update.
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
