#!/usr/bin/env bash
# Vendor the executable browser dependency and rebuild static CSS/fonts.
# The UI loads no remote scripts, stylesheets, or fonts at runtime.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/gamgui/web/static/vendor"
mkdir -p "$DEST"

HTMX_VER="1.9.12"
HTMX_SRI="sha384-ujb1lZYygJmzgSwoxRggbCHcjc0rB2XoQrxeTUQyRjrOnlCoYta87iKBWq3EsdM2"
echo "==> htmx ${HTMX_VER}"
curl -fsSL "https://unpkg.com/htmx.org@${HTMX_VER}/dist/htmx.min.js" -o "$DEST/htmx-${HTMX_VER}.min.js"
GOT="sha384-$(openssl dgst -sha384 -binary "$DEST/htmx-${HTMX_VER}.min.js" | openssl base64 -A)"
if [ "$GOT" != "$HTMX_SRI" ]; then
  echo "ERROR: htmx SRI mismatch — refusing to vendor a tampered file." >&2
  echo "  expected: $HTMX_SRI" >&2
  echo "  got:      $GOT" >&2
  exit 1
fi
echo "    verified SRI $GOT"

echo "==> Static Tailwind CSS + fonts"
if [ ! -d "$ROOT/node_modules" ]; then
  (cd "$ROOT" && npm ci)
fi
(cd "$ROOT" && npm run build:css)

echo "==> Done. If the htmx filename changed, update gamgui/web/templates/base.html."
