#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.."
LOG="logs/midday-$(date +%F).log"
exec >>"$LOG" 2>&1
echo "===== $(date '+%F %T') midday grade refresh ====="

LS_STALE=0
if [ -d data/.browser-profile ]; then
  echo "--- learningsuite refresh ---"
  ./plan ls-refresh --no-sync; rc=$?
  if [ "$rc" != 0 ] && [ "$rc" != 75 ]; then
    echo "LS refresh failed — session likely expired"; LS_STALE=1
  fi
else
  echo "--- refresh SKIPPED (no browser profile) ---"
fi

./plan sync || echo "sync failed (continuing on cached data)"
echo "===== done $(date '+%T') ====="

if [ "$LS_STALE" = "1" ]; then
  /usr/bin/osascript -e 'display notification "Grades still not updating — open Learning Cheat and click Sign in to LearningSuite" with title "LearningSuite needs re-login" sound name "Basso"' 2>/dev/null || true
fi
