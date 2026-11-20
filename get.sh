#!/usr/bin/env bash
set -euo pipefail
DEST="${PLANNER_DIR:-$HOME/learning-cheat}"
REPO="jck-l-nrtn/learning-cheat"

# Download to a temp folder first so a failed download changes nothing.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
[ -t 1 ] && printf '  Downloading Learning Cheat...'
SHA="$(curl -fsSL -H 'Accept: application/vnd.github.sha' \
  "https://api.github.com/repos/$REPO/commits/main" 2>/dev/null || true)"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || SHA=""
curl -fsSL "https://github.com/$REPO/archive/${SHA:-refs/heads/main}.tar.gz" \
  | tar -xz -C "$TMP" --strip-components 1
[ -t 1 ] && printf '\r\033[K'
if [ ! -f "$TMP/src/cli.py" ]; then
  echo "The download did not finish. Check the internet connection and paste the same line again." >&2
  exit 1
fi

[ -f "$DEST/config/courses.yaml" ] && UPDATE=1 || UPDATE=""
mkdir -p "$DEST"
cp -R "$TMP/." "$DEST/"
cd "$DEST"
PLANNER_FROM_GET=1 ./install ${PLANNER_INSTALL_ARGS:-} </dev/null
if [ -n "$SHA" ]; then
  ./.venv/bin/python -m src.updater record "$SHA" "$TMP" >/dev/null 2>&1 || true
fi

if [ -n "${PLANNER_NO_SETUP:-}" ]; then
  echo "  Done. Open it with:  cd \"$DEST\" && ./plan app"
  exit 0
fi
if [ -n "$UPDATE" ]; then
  echo "  Updated. Your classes and settings are as you left them."
else
  echo "  Done. The planner is opening in your browser for setup."
fi
echo "  It runs in this window, so leave the window open while you use it."
echo "  Next time, double-click \"Learning Cheat\" in ${DEST/#$HOME/~}."
echo
exec ./plan app
