#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

if [ "$(uname)" != "Darwin" ]; then
  echo "Scheduled runs use launchd, which only exists on macOS." >&2
  echo "On other systems use the buttons in the app, or cron: scripts/run-daily.sh" >&2
  exit 1
fi

DEST="$HOME/Library/LaunchAgents"
PREFIX="com.classplanner"
mkdir -p "$DEST" logs

for old in "$DEST"/*classplanner.*.plist; do
  [ -e "$old" ] || continue
  grep -q "$ROOT/scripts/run-" "$old" || continue
  launchctl bootout "gui/$(id -u)/$(basename "${old%.plist}")" 2>/dev/null || true
  rm -f "$old"
done

write_plist () {
  local job="$1" when="$2" file="$DEST/$PREFIX.$1.plist"
  cat > "$file" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$PREFIX.$job</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$ROOT/scripts/run-$job.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>$when</dict>
  <!-- If the Mac was asleep at the scheduled time, launchd runs it on wake. -->
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$ROOT/logs/launchd-$job.out</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/launchd-$job.err</string>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin</string></dict>
</dict>
</plist>
PLIST
  launchctl bootstrap "gui/$(id -u)" "$file"
  echo "installed $PREFIX.$job"
}

write_plist daily  "<key>Hour</key><integer>6</integer><key>Minute</key><integer>30</integer>"
write_plist midday "<key>Hour</key><integer>14</integer><key>Minute</key><integer>0</integer>"
write_plist weekly "<key>Weekday</key><integer>0</integer><key>Hour</key><integer>17</integer><key>Minute</key><integer>0</integer>"

echo
echo "Run one now to test:  launchctl kickstart gui/$(id -u)/$PREFIX.daily"
echo "Logs:                 $ROOT/logs/"
echo "Remove:               scripts/uninstall-schedule.sh"
