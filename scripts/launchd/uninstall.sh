#!/usr/bin/env bash
# Remove both gittrack LaunchAgents. The database is left untouched.
set -euo pipefail
for label in com.gittrack.ingest com.gittrack.enrich com.gittrack.dashboard com.gittrack.awake; do
  launchctl bootout "gui/$UID/$label" 2>/dev/null && echo "unloaded $label" || echo "$label was not loaded"
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
echo "done - gittrack.db and your config are untouched."
