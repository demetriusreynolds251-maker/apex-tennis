#!/bin/zsh
# 2026-10-09: launchd wrapper (same pattern as apex_edge_tracker_daily.sh) so the 7 AM run can reach the Desktop folder.
cd /path/to/home/openclaw || exit 1
/usr/bin/python3 apex_daily_settlement_health.py
