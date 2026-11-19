#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.."
LOG="logs/weekly-$(date +%F).log"
exec >>"$LOG" 2>&1
echo "===== $(date '+%F %T') weekly run ====="

if [ -d data/.browser-profile ]; then
  echo "--- learningsuite refresh ---"
  ./plan ls-refresh --no-sync; rc=$?
  if [ "$rc" != 0 ] && [ "$rc" != 75 ]; then
    echo "LS refresh failed — session likely expired; re-run with --login"
  fi
else
  echo "--- learningsuite refresh SKIPPED (no browser profile; run --login once) ---"
fi

./plan sync    || echo "sync failed"
./plan enrich  || echo "enrich failed"
./plan estimate --batch 20 ${WEEKLY_MODEL:+--model "$WEEKLY_MODEL"} || echo "estimate failed"
echo "===== done $(date '+%T') ====="
