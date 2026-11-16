"""APEX-KEYALERT-2026-10-29 (owner: "This should have been told to me!!!").

On 10/29 SportsAPI365 refused the key from 11:56 AM CT. Every 10-30 min scoring pass logged
"key refused -- skipped scoring" and nothing reached Discord; the 12:35 PM PrizePicks batch
(185 props) sat unscored for two hours with no word. This module posts ONE 🛑 line in #general
and #alerts the first time a scoring pass finds the key refused, a short reminder every
REMIND_MIN minutes while it stays refused, and one ✅ line when scoring works again.
State lives in data/sa365_alert_state.json so restarts don't repeat the first alert.
"""
import json
import os
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "data", "sa365_alert_state.json")
REMIND_MIN = 120
CHANNELS = ("general", "alerts")


def _load():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as fh:
            json.dump(st, fh)
    except Exception:
        pass


def _ct(ts=None):
    try:
        from zoneinfo import ZoneInfo
        d = datetime.fromtimestamp(ts or time.time(), ZoneInfo("America/Chicago"))
        return d.strftime("%-I:%M %p CT")
    except Exception:
        return datetime.fromtimestamp(ts or time.time()).strftime("%I:%M %p").lstrip("0")


def _send(core, text):
    for ch in CHANNELS:
        try:
            core.send_discord(ch, text)
        except Exception as e:
            try:
                core.log("KeyAlert", f"#{ch} post failed ({type(e).__name__})")
            except Exception:
                pass


def refused(core, waiting, why=""):
    """A scoring pass found the key refused. Posts once, then reminds every REMIND_MIN min."""
    st = _load()
    now = time.time()
    code = str(why or "").strip()
    if not st.get("dead_since"):
        st = {"dead_since": now, "last_post": now, "waiting": waiting, "code": code}
        _save(st)
        _send(core, "\U0001F6D1 **SportsAPI365 key refused** (%s) since %s — boards can't be scored, "
                    "%d new PrizePicks props are waiting. Fix the key in your SportsAPI365 account, then type "
                    "/setkey in Discord and pick SportsAPI365. I'll post here the moment scoring is back."
              % (code or "no answer", _ct(now), waiting))
        try:
            core.log("KeyAlert", f"first alert posted ({code}), {waiting} props waiting")
        except Exception:
            pass
        return True
    st["waiting"] = waiting
    if now - float(st.get("last_post") or 0) >= REMIND_MIN * 60:
        st["last_post"] = now
        _save(st)
        hrs = (now - float(st["dead_since"])) / 3600.0
        _send(core, "\U0001F6D1 **SportsAPI365 key still refused** (%.0f hr%s, since %s) — %d props waiting to be scored. "
                    "/setkey in Discord fixes it." % (hrs, "" if round(hrs) == 1 else "s", _ct(st["dead_since"]), waiting))
        return True
    _save(st)
    return False


def working(core, scored_hint=None):
    """A scoring pass got through. Posts the all-clear once if an outage was announced."""
    st = _load()
    if not st.get("dead_since"):
        return False
    waiting = int(st.get("waiting") or 0)
    hrs = (time.time() - float(st["dead_since"])) / 3600.0
    _save({})
    _send(core, "✅ **SportsAPI365 key working again** (was refused %.1f hrs, since %s) — scoring the %d waiting props now, "
                "boards update as they finish." % (hrs, _ct(st["dead_since"]), waiting))
    try:
        core.log("KeyAlert", "all-clear posted")
    except Exception:
        pass
    return True
