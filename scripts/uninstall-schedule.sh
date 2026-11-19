#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
found=0
for f in "$HOME/Library/LaunchAgents"/*classplanner.*.plist; do
  [ -e "$f" ] || continue
  grep -q "$ROOT/scripts/run-" "$f" || continue
  name="$(basename "${f%.plist}")"
  launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
  rm -f "$f"
  echo "removed $name"
  found=1
done
[ "$found" = 1 ] || echo "no planner timers were installed for $ROOT"
