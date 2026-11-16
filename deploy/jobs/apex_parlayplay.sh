#!/bin/zsh
# APEX-PARLAYPLAY-2026-10-15: Apex ParlayPlay read/score/grade/post, every 30 min, separate from the PrizePicks scheduler.
# APEX-PPTHROTTLE-2026-10-16: "scheduled" = every 30 min in the line-drop windows, hourly otherwise.
cd /path/to/home/openclaw/ApexParlayPlay || exit 1
/usr/bin/python3 -u apex_parlayplay.py scheduled >> /path/to/home/openclaw/ApexParlayPlay/logs/parlayplay.log 2>&1
