#!/bin/zsh
# APEX-ALERTS-2026-10-11: launchd wrapper for apex_alerts.py --check (integrity + system health, every 30 min)
cd /path/to/home/openclaw || exit 1
/usr/bin/python3 apex_alerts.py --check
