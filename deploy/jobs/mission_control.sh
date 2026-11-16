#!/bin/zsh
# 2026-10-10: launchd wrapper for com.apex.openclawmissioncontrol (same pattern as apex_edge_tracker_daily.sh).
# If a hand-started copy is running, wait for it to stop instead of starting a second.
while pgrep -f "openclaw_mission_control.py" >/dev/null; do sleep 30; done
cd /path/to/home/openclaw || exit 1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') mission control starting via launchd ===" >> /path/to/home/openclaw/data/logs/mission_control.log
exec /usr/bin/python3 openclaw_mission_control.py 8788 >> /path/to/home/openclaw/data/logs/mission_control.log 2>&1
