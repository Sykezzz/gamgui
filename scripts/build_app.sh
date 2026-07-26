#!/usr/bin/env bash
# Build the standalone GamGUI.app (macOS) with PyInstaller.
# Prereqs: `make setup` (a .venv with deps). Vendors GAM7 automatically if missing.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-.venv/bin/python}"
PROFILE="${PROFILE:-core}"
case "$PROFILE" in
  core|classroom-oneroster) ;;
  *)
    echo "Unsupported PROFILE '$PROFILE' (expected core or classroom-oneroster)." >&2
    exit 1
    ;;
esac
if [ ! -x "$PY" ]; then
  echo "No virtualenv at .venv — run 'make setup' first." >&2
  exit 1
fi

if [ "$(uname)" != "Darwin" ]; then
  echo "Note: this builds a macOS .app; on $(uname) PyInstaller will produce a plain bundle instead." >&2
fi

SOURCE_SHA="$(git rev-parse HEAD)"
PACKAGED_SOURCE_STATUS="$(
  git status --porcelain --untracked-files=all -- \
    gamgui \
    gamgui.spec \
    Makefile \
    pyproject.toml \
    uv.lock \
    scripts/build_app.sh \
    scripts/fetch_gam.sh \
    scripts/gam_checksums.txt
)"
if [ -n "$PACKAGED_SOURCE_STATUS" ] && [ "${GAMGUI_ALLOW_DIRTY_BUILD:-}" != "1" ]; then
  echo "Packaged source changes or untracked files are present:" >&2
  printf '%s\n' "$PACKAGED_SOURCE_STATUS" >&2
  echo "Commit them before building an exact-SHA app." >&2
  echo "Set GAMGUI_ALLOW_DIRTY_BUILD=1 only for a non-release developer build." >&2
  exit 1
fi

# The ignored GAM payload is executable and receives domain-wide delegated
# credentials at runtime. Never trust a pre-existing worktree copy: re-establish
# it from the committed release pin for every signed application build.
echo "==> Re-establishing pinned GAM payload..."
./scripts/fetch_gam.sh

echo "==> Verifying locked build dependencies..."
"$PY" -c "import PyInstaller, webview"

BUILD_METADATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gamgui-profile.XXXXXX")"
trap 'rm -rf "$BUILD_METADATA_DIR"' EXIT
export GAMGUI_BUILD_PROFILE="$PROFILE"
export GAMGUI_SOURCE_SHA="$SOURCE_SHA"
export GAMGUI_BUILD_ARCH="$(uname -m)"
export GAMGUI_MINIMUM_MACOS="${GAMGUI_MINIMUM_MACOS:-12.0}"
export GAMGUI_PACKAGING_REVISION="${GAMGUI_PACKAGING_REVISION:-1}"
export GAMGUI_BUILD_METADATA_DIR="$BUILD_METADATA_DIR"

echo "==> Building profile: $PROFILE"
"$PY" -m PyInstaller --noconfirm --clean gamgui.spec

APP="dist/GamGUI.app"
# Sign with a STABLE self-signed identity so macOS "Always Allow" sticks across rebuilds and the
# Keychain stops re-prompting. Create a free "Code Signing" cert (Keychain Access, or the CLI in the
# README) named "GamGUI Local" once; builds then pick it up automatically. Override with
# CODESIGN_IDENTITY=… ; leave it with no such cert to keep PyInstaller's ad-hoc signature.
if [ -z "${CODESIGN_IDENTITY:-}" ] && [ "$(uname)" = "Darwin" ] \
   && security find-identity -p codesigning 2>/dev/null | grep -q "GamGUI Local"; then
  CODESIGN_IDENTITY="GamGUI Local"  # auto-use the local signing cert if it exists
fi
SIGNING_CHANNEL=""
SIGNING_AUTHORITY=""
if [ -n "${CODESIGN_IDENTITY:-}" ] && [ "$(uname)" = "Darwin" ]; then
  echo "==> Codesigning with stable identity: $CODESIGN_IDENTITY"
  GAM_BIN="$(find "$APP" -type f -name gam -path '*resources/gam7/*' 2>/dev/null | head -1)"
  [ -n "$GAM_BIN" ] && codesign --force --sign "$CODESIGN_IDENTITY" "$GAM_BIN"
  codesign --force --deep --sign "$CODESIGN_IDENTITY" "$APP"
  codesign --verify --deep --strict "$APP" && echo "    signed + verified OK"
  SIGNING_AUTHORITY="$CODESIGN_IDENTITY"
  case "$CODESIGN_IDENTITY" in
    "GamGUI Local") SIGNING_CHANNEL="local" ;;
    "Developer ID Application:"*) SIGNING_CHANNEL="developer-id" ;;
  esac
else
  echo "==> No CODESIGN_IDENTITY set — keeping the ad-hoc signature."
  echo "    For a silent Keychain, make a self-signed Code Signing cert and re-run with"
  echo "    CODESIGN_IDENTITY set (see README → 'Stop the Keychain prompts')."
fi

echo "==> Recording exact artifact identity..."
"$PY" -c \
  'import sys; from pathlib import Path; from gamgui.core.components import write_artifact_sidecar; print(write_artifact_sidecar(Path(sys.argv[1]), signing_channel=sys.argv[2], signing_authority=sys.argv[3]))' \
  "$APP" "$SIGNING_CHANNEL" "$SIGNING_AUTHORITY"

echo "==> Done: $APP"
echo "    Profile: $PROFILE"
echo "    Identity: $APP.artifact.json"
echo "    For distribution to OTHER Macs you still need an Apple Developer ID + notarization."
