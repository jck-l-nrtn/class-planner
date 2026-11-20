#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.."

PY="./.venv/bin/python"
LOG="logs/daily-$(date +%F).log"
exec >>"$LOG" 2>&1
echo "===== $(date '+%F %T') daily run ====="

step () { echo "--- $1 ---"; }

LS_STALE=0
if [ -d data/.browser-profile ]; then
  step "learningsuite refresh"
  ./plan ls-refresh --no-sync; rc=$?
  if [ "$rc" != 0 ] && [ "$rc" != 75 ]; then
    echo "LS refresh failed — session likely expired; continuing on cached dumps"
    LS_STALE=1
  fi
else
  echo "--- learningsuite refresh SKIPPED (no browser profile; run ./plan ls-login once) ---"
fi

step "sync";    ./plan sync    || echo "sync failed (continuing on cached data)"

step "today"
PLAN_OK=1
./plan today ${DAILY_MODEL:+--model "$DAILY_MODEL"} || { PLAN_OK=0; echo "daily agent failed"; }

echo "===== done $(date '+%T') ====="

TODAY_FILE="out/day-$(date +%F).json"
if [ "$PLAN_OK" = "1" ] && [ -f "$TODAY_FILE" ]; then
  SUMMARY=$($PY -c "import json,sys; \
print(json.load(open(sys.argv[1])).get('day_summary','plan ready')[:230])" \
    "$TODAY_FILE" 2>/dev/null)
  if [ -n "${SUMMARY:-}" ]; then
    /usr/bin/osascript -e "display notification \"${SUMMARY//\"/}\" with title \"Today's plan is ready\"" 2>/dev/null || true
  fi
else
  /usr/bin/osascript -e "display notification \"No plan for today — open Learning Cheat to see why\" with title \"Planning failed\" sound name \"Basso\"" 2>/dev/null || true
fi

if [ "$LS_STALE" = "1" ]; then
  /usr/bin/osascript -e 'display notification "Grades did not update — open Learning Cheat and click Sign in to LearningSuite" with title "LearningSuite needs re-login" sound name "Basso"' 2>/dev/null || true
fi
