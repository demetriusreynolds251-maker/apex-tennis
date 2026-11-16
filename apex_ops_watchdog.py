#!/usr/bin/env python3
"""
APEX-OPS-2026-10-10 (owner, approved "go" 2026-10-10): scheduler
watchdog. Runs every 15 minutes from its own LaunchAgent
(com.apex.schedwatchdog), OUTSIDE the scheduler process, so it still
fires when the scheduler is dead or stuck. Background: on Oct 9 the
scheduler was alive from 10:18 AM but never ran the 5 PM pipeline, and
nobody knew until the next day.

Alerts to Discord #alerts (each alert at most once per day per kind):
  1. heartbeat stale  -- no scheduler heartbeat for 45+ minutes
  2. missed 5 PM run  -- after 5:45 PM CT, no pipeline 'finish' today
                         (and a separate alert if the run recorded 'fail')
Read-only otherwise: it never restarts anything (recovery steps are in the
alert text).

  python3 apex_ops_watchdog.py           # normal check
  python3 apex_ops_watchdog.py --status  # print status, send nothing
"""
import sys
from datetime import datetime, timedelta
import apex_ops as ops

HEARTBEAT_STALE_MIN = 45
PIPELINE_DEADLINE = (17, 45)      # 5:45 PM CT


def _parse(ts):
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def check(now=None):
    now = now or ops.now_ct()
    today = now.date().isoformat()
    problems = []

    hb = ops._read_json(ops.HEARTBEAT_FILE, {})
    hb_ts = _parse(hb.get("ts", ""))
    if hb_ts is None:
        problems.append(("heartbeat", "No scheduler heartbeat on file at all."))
    else:
        age = (now - hb_ts.astimezone(now.tzinfo)).total_seconds() / 60.0
        if age > HEARTBEAT_STALE_MIN:
            problems.append(("heartbeat", "Last scheduler heartbeat was %d minutes ago (%s)."
                             % (age, ops.ct_str(hb_ts.astimezone(now.tzinfo)))))

    runs = ops._read_json(ops.PIPELINE_RUNS_FILE, [])
    todays = [r for r in runs if str(r.get("ts", ""))[:10] == today]
    finished = any(r.get("event") == "finish" for r in todays)
    failed = [r for r in todays if r.get("event") == "fail"]
    started = any(r.get("event") == "start" for r in todays)
    if (now.hour, now.minute) >= PIPELINE_DEADLINE and not finished:
        if failed:
            problems.append(("pipeline_fail", "Today's 5 PM pipeline FAILED: %s" % failed[-1].get("error", "?")))
        elif started:
            problems.append(("pipeline", "Today's 5 PM pipeline started but has not finished by 5:45 PM."))
        else:
            problems.append(("pipeline", "Today's 5 PM pipeline never started."))
    return problems, {"heartbeat": hb, "today_runs": todays}


RECOVERY = ("Recovery: run /fetch in Discord to publish today's boards, then restart the "
            "scheduler: launchctl kickstart -k gui/$(id -u)/com.apex.apexscheduler")


def main():
    problems, info = check()
    if "--status" in sys.argv:
        print("heartbeat:", info["heartbeat"])
        print("today's pipeline events:", info["today_runs"])
        print("problems:", problems or "none")
        return
    if not problems:
        return
    today = ops.now_ct().date().isoformat()
    state = ops._read_json(ops.WATCHDOG_STATE_FILE, {})
    sent = state.get(today, [])
    new = [(k, m) for k, m in problems if k not in sent]
    if not new:
        return
    msg = ["\U0001F6A8 **Scheduler watchdog** -- %s" % ops.ct_str()]
    msg += ["- %s" % m for _, m in new]
    msg.append(RECOVERY)
    if ops.send_discord("alerts", "\n".join(msg)):
        state = {today: sent + [k for k, _ in new]}   # keep only today's record
        ops._write_json(ops.WATCHDOG_STATE_FILE, state)
    print("\n".join(msg))


if __name__ == "__main__":
    main()
