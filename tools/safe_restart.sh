#!/bin/zsh
# APEX-SCORINGCATCHUP-2026-11-01: restart the bot and scheduler only after the current board cycle
# has finished (up to 20 minutes), so a restart can't stop a scoring run.
ST="$HOME/openclaw/data/logs/board_cycle_state.json"
for i in {1..40}; do
  python3 - "$ST" <<'P' && break
import json,sys,time
try:
    s=json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
b,d=float(s.get('started',0)),float(s.get('finished',0))
sys.exit(1 if b>d and time.time()-b<1800 else 0)
P
  echo "scoring running, waiting"; sleep 30
done
# APEX-SAFERESTART-2026-11-01: a drop-alert scoring run doesn't mark the cycle state; also wait until the picks log is quiet.
for i in {1..40}; do
  python3 -c "import os,time,sys;sys.exit(0 if time.time()-os.path.getmtime(os.path.expanduser('~/openclaw/apex_engine_picks_log_v1.csv'))>150 else 1)" && break
  echo "picks still being scored, waiting"; sleep 30
done
launchctl kickstart -k gui/$UID/com.apex.discordbot
launchctl kickstart -k gui/$UID/com.apex.apexscheduler
launchctl list | grep -E "discordbot|apexscheduler"
