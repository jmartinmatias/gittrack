#!/usr/bin/env bash
# Install gittrack as two macOS LaunchAgents:
#   com.gittrack.ingest     - snapshots the tracked universe every hour
#   com.gittrack.dashboard  - keeps the local dashboard up on 127.0.0.1:8787
#
# Both are per-user agents in ~/Library/LaunchAgents. Remove with uninstall.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BIN="$ROOT/.venv/bin/gittrack"
AGENTS="$HOME/Library/LaunchAgents"
PORT="${GITTRACK_PORT:-8787}"
# Seconds between ingest runs. A run costs roughly one search call per 100 repos
# in the universe, so scale this with top_n: 3600 is fine at top_n<=2000,
# 7200 at 10000, 21600 (6h) at 30000+.
# Two schedules, because the two jobs have very different costs.
#   TRENDING_INTERVAL - snapshot the boards. No API quota, so it can run often.
#   ENRICH_INTERVAL   - the API pass (metadata, and history for repos that have
#                       dropped off every board). Rate-limit bound, so it runs
#                       rarely and catches up over several passes.
INTERVAL="${GITTRACK_INTERVAL:-3600}"
ENRICH_INTERVAL="${GITTRACK_ENRICH_INTERVAL:-21600}"

[ -x "$BIN" ] || { echo "error: $BIN not found. Run: uv venv && uv pip install -e ." >&2; exit 1; }
mkdir -p "$AGENTS"

# gh lives outside launchd's minimal PATH. Putting its directory on PATH here means
# that the moment you run `gh auth login`, the scheduled runs pick the token up -
# no edit, no reload.
GH_DIR="$(dirname "$(command -v gh 2>/dev/null || echo /opt/homebrew/bin/gh)")"
AGENT_PATH="$GH_DIR:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

write_plist() {
  local label="$1"; shift
  local plist="$AGENTS/$label.plist"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$AGENT_PATH</string></dict>
  <key>ProgramArguments</key>
  <array>
$(for a in "$@"; do echo "    <string>$a</string>"; done)
  </array>
$EXTRA
  <key>StandardOutPath</key><string>$ROOT/${label##*.}.log</string>
  <key>StandardErrorPath</key><string>$ROOT/${label##*.}.log</string>
</dict>
</plist>
PLIST
  echo "$plist"
}

# The trending sweep. This is the one that matters hourly: it snapshots each
# board and diffs it against the previous reading, and costs no API quota.
EXTRA="  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>"
write_plist com.gittrack.ingest "$BIN" run --trending-only >/dev/null

# The API pass, on its own slower clock so a rate limit can never stall the
# boards. It fills in metadata and keeps history for repos no board still lists.
EXTRA="  <key>StartInterval</key><integer>$ENRICH_INTERVAL</integer>
  <key>RunAtLoad</key><false/>"
write_plist com.gittrack.enrich "$BIN" run --no-trending >/dev/null

# Dashboard, restarted if it ever exits.
EXTRA='  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>'
write_plist com.gittrack.dashboard "$BIN" serve --no-browser --port "$PORT" >/dev/null

for label in com.gittrack.ingest com.gittrack.enrich com.gittrack.dashboard; do
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$AGENTS/$label.plist"
  echo "loaded $label"
done

echo
echo "ingest    trending boards every ${INTERVAL}s      -> $ROOT/ingest.log"
echo "enrich    API pass every ${ENRICH_INTERVAL}s          -> $ROOT/enrich.log"
echo "dashboard http://127.0.0.1:$PORT             -> $ROOT/dashboard.log"
