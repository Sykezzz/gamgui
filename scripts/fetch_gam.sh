#!/usr/bin/env bash
# Vendor the GAM7 binary into gamgui/resources/gam7/.
#
# Downloads the latest GAM7 macOS release matching this machine's architecture from the official
# GAM-team/GAM GitHub releases, records its SHA-256, extracts it, and copies the PyInstaller
# bundle (the `gam` executable plus its support files) into resources/gam7/. Records the version.
#
# Usage: scripts/fetch_gam.sh [--tag vX.Y.Z|latest]
#        (default: the pinned, tested version, verified against scripts/gam_checksums.txt)
set -euo pipefail

REPO="GAM-team/GAM"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/gamgui/resources/gam7"
DEST_PARENT="$(dirname "$DEST")"
CATALOG="$DEST/command_catalog.json"
# Pinned for reproducible builds. Override with `--tag latest` to grab the newest release.
TAG="v7.48.00"
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

case "$(uname -m)" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64) ARCH="x86_64" ;;
  *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac

if [ "$TAG" = "latest" ]; then
  API="https://api.github.com/repos/$REPO/releases/latest"
else
  API="https://api.github.com/repos/$REPO/releases/tags/$TAG"
fi

TMP="$(mktemp -d)"
INSTALL_STAGE=""
INSTALL_BACKUP=""
cleanup() {
  local status="$?"
  if [ -n "$INSTALL_STAGE" ] && [ -d "$INSTALL_STAGE" ]; then
    case "$INSTALL_STAGE" in
      "$DEST_PARENT"/.gam7-stage.*) rm -rf -- "$INSTALL_STAGE" ;;
      *) echo "Refusing to remove an unsafe GAM staging path." >&2; status=1 ;;
    esac
  fi
  if [ -n "$INSTALL_BACKUP" ] && [ -d "$INSTALL_BACKUP" ]; then
    case "$INSTALL_BACKUP" in
      "$DEST_PARENT"/.gam7-backup.*)
        if [ ! -e "$DEST" ]; then
          mv -- "$INSTALL_BACKUP" "$DEST" || status=1
        else
          rm -rf -- "$INSTALL_BACKUP" || status=1
        fi
        ;;
      *) echo "Refusing to remove an unsafe GAM backup path." >&2; status=1 ;;
    esac
  fi
  rm -rf -- "$TMP" || status=1
  return "$status"
}
trap cleanup EXIT

echo "==> Querying $REPO release ($TAG) for a macos/$ARCH asset..."
# Authenticate when a token is available: unauthenticated api.github.com is capped at 60 req/hr per
# IP, and shared CI runner IPs routinely blow past it -> HTTP 403 (curl exit 56). A token raises the
# limit to 5000/hr. Also retry to ride out transient rate-limit/network blips.
GH_API_AUTH=()
_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
if [ -n "$_TOKEN" ]; then
  GH_API_AUTH=(-H "Authorization: Bearer $_TOKEN")
fi
curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 \
  -H "Accept: application/vnd.github+json" ${GH_API_AUTH[@]+"${GH_API_AUTH[@]}"} "$API" -o "$TMP/release.json"

# Parse the release JSON from the file (no stdin, so a heredoc script is safe here). Print one
# line: "<tag> <asset-name> <download-url>". Prefer the highest macosNN build if several match.
PICK="$(python3 - "$TMP/release.json" "$ARCH" <<'PY'
import json, re, sys
data = json.load(open(sys.argv[1]))
arch = sys.argv[2]
cands = []
for a in data.get("assets", []):
    n = a["name"]
    if "macos" in n and arch in n and n.endswith(".tar.xz"):
        m = re.search(r"macos(\d+)", n)
        cands.append((int(m.group(1)) if m else 0, n, a["browser_download_url"]))
if not cands:
    sys.exit("no matching macOS asset found in release")
cands.sort()
_, name, url = cands[-1]
print(data.get("tag_name", ""), name, url)
PY
)"
read -r VERSION ASSET_NAME ASSET_URL <<<"$PICK"
echo "==> Selected: $ASSET_NAME (release $VERSION)"

echo "==> Downloading..."
curl -fSL --retry 5 --retry-all-errors --retry-delay 3 --progress-bar "$ASSET_URL" -o "$TMP/gam.tar.xz"

SHA="$(shasum -a 256 "$TMP/gam.tar.xz" | awk '{print $1}')"
echo "==> SHA-256: $SHA"

# Verify against the committed pin BEFORE extracting — refuse a tampered / MITM'd / unexpected
# binary (the bundled gam can impersonate any user via DWD, so a swapped binary is game over).
CHECKSUMS="$ROOT/scripts/gam_checksums.txt"
EXPECTED="$(awk -v a="$ASSET_NAME" '$1!~/^#/ && $2==a {print $1}' "$CHECKSUMS" 2>/dev/null | head -1)"
if [ -n "$EXPECTED" ]; then
  if [ "$SHA" != "$EXPECTED" ]; then
    echo "ERROR: checksum mismatch for $ASSET_NAME" >&2
    echo "  expected (pinned): $EXPECTED" >&2
    echo "  downloaded:        $SHA" >&2
    echo "  Refusing to install — the binary differs from the committed pin (possible tampering)." >&2
    exit 1
  fi
  echo "==> Checksum verified against committed pin (scripts/gam_checksums.txt)."
else
  echo "ERROR: no pinned checksum for '$ASSET_NAME'." >&2
  echo "  Refusing to install an unpinned GAM binary on this architecture." >&2
  echo "  The automated bump must record every supported macOS architecture first." >&2
  exit 1
fi

echo "==> Extracting..."
tar -xJf "$TMP/gam.tar.xz" -C "$TMP"

GAM_BIN="$(find "$TMP" -type f -name gam -perm +111 | head -1 || true)"
if [ -z "$GAM_BIN" ]; then
  echo "could not locate the gam executable inside the archive" >&2
  exit 1
fi
GAM_DIR="$(dirname "$GAM_BIN")"

echo "==> Installing into $DEST"
# ``command_catalog.json`` is an application resource committed alongside the
# downloaded payload. Keep it while replacing the payload so a routine
# ``make gam`` does not make the command browser unusable. GAM bumps regenerate
# the catalog explicitly after this script completes.
if [ -f "$CATALOG" ]; then
  cp "$CATALOG" "$TMP/command_catalog.json"
fi
mkdir -p "$DEST_PARENT"
INSTALL_STAGE="$(mktemp -d "$DEST_PARENT/.gam7-stage.XXXXXX")"
cp -R "$GAM_DIR"/. "$INSTALL_STAGE"/
chmod +x "$INSTALL_STAGE/gam"

printf '%s\n' "$VERSION" > "$INSTALL_STAGE/VERSION"
printf '%s  %s\n' "$SHA" "$ASSET_NAME" > "$INSTALL_STAGE/SHA256"
if [ -f "$TMP/command_catalog.json" ]; then
  cp "$TMP/command_catalog.json" "$INSTALL_STAGE/command_catalog.json"
fi

# Stage completely before replacing the executable payload. If activation fails,
# the EXIT trap restores the prior directory.
if [ -e "$DEST" ]; then
  if [ -L "$DEST" ] || [ ! -d "$DEST" ]; then
    echo "ERROR: refusing to replace an unsafe GAM destination." >&2
    exit 1
  fi
  INSTALL_BACKUP="$(mktemp -d "$DEST_PARENT/.gam7-backup.XXXXXX")"
  rmdir "$INSTALL_BACKUP"
  mv -- "$DEST" "$INSTALL_BACKUP"
fi
mv -- "$INSTALL_STAGE" "$DEST"
INSTALL_STAGE=""
if [ -n "$INSTALL_BACKUP" ]; then
  rm -rf -- "$INSTALL_BACKUP"
  INSTALL_BACKUP=""
fi

echo "==> Done. Vendored GAM $VERSION."
"$DEST/gam" version 2>/dev/null | head -2 || echo "(could not run gam version yet — may need Gatekeeper approval)"
