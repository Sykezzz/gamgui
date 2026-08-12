#!/usr/bin/env bash
# One-time remote activation after district-main has been pushed.
# This script changes GitHub repository settings; run it only during an explicitly approved ship.
set -euo pipefail

REPO="${1:-Sykezzz/gamgui}"

gh repo view "$REPO" >/dev/null
gh repo edit "$REPO" \
  --enable-issues=true \
  --enable-auto-merge=true \
  --delete-branch-on-merge=true \
  --default-branch=district-main

gh api --method PUT "repos/$REPO/actions/permissions" \
  -F enabled=true \
  -f allowed_actions=all >/dev/null

gh api --method PUT "repos/$REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=true >/dev/null

gh api --method PUT "repos/$REPO/branches/district-main/protection" --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "gam-compat (pinned binary)",
      "macOS application build smoke",
      "Windows application build smoke",
      "test (ubuntu-latest, py3.10)",
      "test (ubuntu-latest, py3.12)",
      "test (ubuntu-latest, py3.14)",
      "test (macos-latest, py3.10)",
      "test (macos-latest, py3.12)",
      "test (macos-latest, py3.14)",
      "Windows test (py3.10)",
      "Windows test (py3.12)",
      "Windows test (py3.14)"
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "required_conversation_resolution": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON

echo "Configured $REPO: district-main is protected/default; Actions, Issues, and auto-merge are enabled."
