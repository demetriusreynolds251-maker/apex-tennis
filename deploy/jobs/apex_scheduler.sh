#!/bin/zsh
# 2026-10-10: launchd wrapper for com.apex.apexscheduler (same pattern as apex_edge_tracker_daily.sh)
# so the scheduler can reach the Desktop folder after a reboot. If a hand-started copy is running, wait for it to stop.
while pgrep -f "openclaw_core.py" >/dev/null; do sleep 30; done
cd /path/to/home/openclaw || exit 1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') scheduler starting via launchd ===" >> /path/to/home/openclaw/data/logs/apex_scheduler.log
exec /usr/bin/python3 -u openclaw_core.py >> /path/to/home/openclaw/data/logs/apex_scheduler.log 2>> /path/to/home/openclaw/data/logs/apex_scheduler_error.log
