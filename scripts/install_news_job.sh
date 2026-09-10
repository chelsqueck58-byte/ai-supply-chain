#!/bin/bash
# Install the daily news refresh as a launchd job on the Mac mini.
#
# Run once, on the mini, from the repo checkout:
#     bash scripts/install_news_job.sh
#
# Idempotent: unloads any previous copy before reloading, so re-running after a
# change to this file just picks up the new version. Does NOT touch
# orchestrate.py - the news refresh runs on its own timer and publishes through
# publish.py, which only ever stages generated data.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.chels.ai-supply-chain.news"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/ai-supply-chain-news.log"
HOUR=7
MINUTE=30

fail() { echo "error: $*" >&2; exit 1; }

# Preflight. Every one of these has bitten a scheduled job on this machine
# before, and launchd failures are close to silent - better to stop here.
[ -f "$REPO/scripts/news_refresh.py" ] || fail "news_refresh.py not found - run this from the repo checkout"
[ -f "$REPO/scripts/publish.py" ]      || fail "publish.py not found - run this from the repo checkout"
PY="$REPO/.venv/bin/python3"
[ -x "$PY" ] || fail "no venv at $PY - create one with: uv venv --python 3.12 .venv"

CLAUDE="$(command -v claude || true)"
[ -n "$CLAUDE" ] || fail "claude CLI not on PATH - install it, or set CLAUDE_BIN in the plist below"
echo "repo:   $REPO"
echo "python: $PY"
echo "claude: $CLAUDE"

mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"

# CLAUDE_BIN is written in explicitly: launchd jobs do not get a login shell, so
# an interactive-only PATH entry would leave `claude` unfindable at 07:30.
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CLAUDE_BIN</key><string>$CLAUDE</string>
    <key>PATH</key><string>$(dirname "$CLAUDE"):/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>$PY scripts/news_refresh.py &amp;&amp; $PY scripts/publish.py -m "news refresh"</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST_EOF

plutil -lint "$PLIST" >/dev/null || fail "generated plist is malformed: $PLIST"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

printf '\ninstalled %s\n' "$LABEL"
printf 'schedule  daily %02d:%02d HKT (launchd uses local time - no UTC conversion)\n' "$HOUR" "$MINUTE"
printf 'log       %s\n\n' "$LOG"
printf 'run it now:  launchctl start %s && tail -f %s\n' "$LABEL" "$LOG"
printf 'remove it:   launchctl unload %s && rm %s\n' "$PLIST" "$PLIST"
