#!/usr/bin/env bash
# Offline verification for one downloaded official GamGUI profile.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MANIFEST="${1:-}"
CHECKSUM="${2:-}"
ASSET_DIR="${3:-}"
PROFILE="${4:-}"
case "$PROFILE" in
  core|classroom-oneroster) ;;
  *)
    echo "Usage: $0 MANIFEST CHECKSUM ASSET_DIR core|classroom-oneroster" >&2
    exit 2
    ;;
esac
if [ "$(uname)" != "Darwin" ]; then
  echo "macOS signature and notarization verification requires macOS." >&2
  exit 1
fi
: "${EXPECTED_TEAM_ID:?EXPECTED_TEAM_ID is required as an independent trust anchor}"
case "$EXPECTED_TEAM_ID" in
  *[!A-Z0-9]*|"")
    echo "EXPECTED_TEAM_ID must contain exactly 10 uppercase letters or digits." >&2
    exit 2
    ;;
esac
if [ "${#EXPECTED_TEAM_ID}" -ne 10 ]; then
  echo "EXPECTED_TEAM_ID must contain exactly 10 uppercase letters or digits." >&2
  exit 2
fi

PY="${PYTHON:-.venv/bin/python}"
test -x "$PY" || {
  echo "The locked verification environment is missing." >&2
  exit 1
}

VERIFY_ARGS=(
  --manifest "$MANIFEST"
  --checksum "$CHECKSUM"
  --asset-dir "$ASSET_DIR"
  --profile "$PROFILE"
  --expected-team-id "$EXPECTED_TEAM_ID"
)
if [ -n "${EXPECTED_SOURCE_SHA:-}" ]; then
  VERIFY_ARGS+=(--expected-source-sha "$EXPECTED_SOURCE_SHA")
fi
"$PY" scripts/release_manifest.py verify-artifact \
  "${VERIFY_ARGS[@]}"

ARCHIVE_NAME="$(
  "$PY" scripts/release_manifest.py show \
    --manifest "$MANIFEST" \
    --profile "$PROFILE" \
    --field filename
)"
SIDECAR_NAME="$(
  "$PY" scripts/release_manifest.py show \
    --manifest "$MANIFEST" \
    --profile "$PROFILE" \
    --field identity_filename
)"
SIGNING_AUTHORITY="$(
  "$PY" scripts/release_manifest.py show \
    --manifest "$MANIFEST" \
    --profile "$PROFILE" \
    --field signing_authority
)"
case "$SIGNING_AUTHORITY" in
  "Developer ID Application: "*" ($EXPECTED_TEAM_ID)") ;;
  *)
    echo "Release authority does not match EXPECTED_TEAM_ID." >&2
    exit 1
    ;;
esac

VERIFY_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/gamgui-official-verify.XXXXXX")"
cleanup() {
  rm -rf "$VERIFY_ROOT"
}
trap cleanup EXIT
chmod 700 "$VERIFY_ROOT"

"$PY" - "$ASSET_DIR/$ARCHIVE_NAME" "$VERIFY_ROOT" <<'PY'
import sys
from pathlib import Path

from gamgui.core.release_manifest import extract_release_archive

extract_release_archive(Path(sys.argv[1]), Path(sys.argv[2]))
PY
APP="$VERIFY_ROOT/GamGUI.app"
test -d "$APP" || {
  echo "Release archive did not contain GamGUI.app." >&2
  exit 1
}
cp "$ASSET_DIR/$SIDECAR_NAME" "$APP.artifact.json"

"$PY" - "$APP" "$PROFILE" <<'PY'
import sys
from pathlib import Path

from gamgui.core.components import verify_bundle_artifact, verify_runtime_compatibility

envelope = verify_bundle_artifact(Path(sys.argv[1]), expected_profile=sys.argv[2])
verify_runtime_compatibility(envelope.artifact)
PY

BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist")"
test "$BUNDLE_ID" = "io.github.goetchstone.gamgui"
codesign --verify --deep --strict --verbose=4 "$APP"
SIGNATURE_DETAILS="$(codesign --display --verbose=4 "$APP" 2>&1)"
printf '%s\n' "$SIGNATURE_DETAILS" | grep -F "Authority=$SIGNING_AUTHORITY" >/dev/null
test "$(
  printf '%s\n' "$SIGNATURE_DETAILS" |
    grep -Ec "^TeamIdentifier=$EXPECTED_TEAM_ID$"
)" = "1"
printf '%s\n' "$SIGNATURE_DETAILS" | grep -E 'flags=.*\(runtime\)' >/dev/null
printf '%s\n' "$SIGNATURE_DETAILS" | grep -E '^Timestamp=' >/dev/null
xcrun stapler validate "$APP"
spctl --assess --type execute --verbose=4 "$APP"

SELF_TEST_DATA="$VERIFY_ROOT/self-test-data"
mkdir -m 700 "$SELF_TEST_DATA"
SELF_TEST="$(
  GAMGUI_APP_DATA_DIR="$SELF_TEST_DATA" \
    "$APP/Contents/MacOS/GamGUI" --self-test --json
)"
"$PY" - "$SELF_TEST" <<'PY'
import json
import sys

result = json.loads(sys.argv[1])
if result.get("ok") is not True or result.get("failures") not in ([], None):
    raise SystemExit("Offline self-test failed.")
PY

echo "Verified official $PROFILE artifact without tenant access."
