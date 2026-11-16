#!/bin/zsh
# APEX-OPS-2026-10-10: launchd wrapper for apex_daily_board_report.py (approved "go" 2026-10-10)
cd /path/to/home/openclaw || exit 1
/usr/bin/python3 apex_daily_board_report.py
