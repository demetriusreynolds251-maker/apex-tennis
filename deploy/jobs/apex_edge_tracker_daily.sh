#!/bin/zsh
# APEX-EDGE-TRACKER-2026-08-27 daily: settle whatever the year files can settle, then post the tracker to Discord.
cd /path/to/home/openclaw || exit 1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> apex_edge_tracker_daily.log
/usr/bin/python3 apex_prop_sim_v1.py --settle-sackmann >> apex_edge_tracker_daily.log 2>&1
/usr/bin/python3 apex_settle_sa365.py --apply >> apex_edge_tracker_daily.log 2>&1
/usr/bin/python3 apex_settle_sa365.py --emit-hand --apply >> apex_edge_tracker_daily.log 2>&1
/usr/bin/python3 apex_edge_tracker.py --post --no-ledger >> apex_edge_tracker_daily.log 2>&1
/usr/bin/python3 /path/to/home/openclaw/apex_learning_engine.py --cycle --post >> apex_edge_tracker_daily.log 2>&1
