"""APEX-BEST6TIMES-2026-11-13 (owner: "We need a tracker when the best card is", chose "Both" on 11/13 6:13 AM CT):
the Best 6 of 6 card (without tennis, the one shown) is saved at 9 AM, 12 PM, 4:30 PM and 7 PM Central, each with its strength
(STRONG / OK / WEAK, same rule as the card's strength line). Every saved card is graded from each sport's own results.
A card WON when every pick that played hit (a void does not lose it). Short cards are graded too and show as short.
Answers two questions: which time of day the card wins most, and whether STRONG cards really win more than OK and WEAK.
Read only on every other file. Log: data/best6_times.jsonl. Lines to #system when saved and when graded. Called from
apex_alerts.check() every 30 min. Turn off: ON = False."""
import json, os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ON = True
SLOTS = [((9, 0), "9 AM"), ((12, 0), "12 PM"), ((16, 30), "4:30 PM"), ((19, 0), "7 PM")]
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "best6_times.jsonl")
CT = ZoneInfo("America/Chicago")


def strength(nums, size):
    import apex_strength as S
    if not nums or len(nums) < size or min(nums) < S.SOLID: return "WEAK"
    return "STRONG" if min(nums) >= S.STRONG else "OK"


def _read():
    try:
        return [json.loads(l) for l in open(LOG) if l.strip()]
    except Exception:
        return []


def _write(rows):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG + ".tmp", "w") as fh:
        for r in rows: fh.write(json.dumps(r) + "\n")
    os.replace(LOG + ".tmp", LOG)


def _due(now, rows):
    """The slot to save now, or None: the latest slot whose time has come, not saved today, and the next slot not reached yet."""
    day = now.strftime("%Y-%m-%d"); hm = (now.hour, now.minute)
    for i, (t, name) in enumerate(SLOTS):
        nxt = SLOTS[i + 1][0] if i + 1 < len(SLOTS) else (24, 0)
        if t <= hm < nxt and not any(r["day"] == day and r["slot"] == name for r in rows): return name
    return None


def _finish(r):
    import apex_best6 as B
    if r.get("result"): return False
    ch = False
    for l in r["legs"]:
        if l.get("result") is None:
            l["saved_day"] = r["day"]; g = B._grade(l)
            if g: l["result"] = g; ch = True
    res = [l.get("result") for l in r["legs"]]
    starts = []
    for l in r["legs"]:
        try:
            x = datetime.fromisoformat(str(l["start"]).replace("Z", "+00:00")); starts.append(x if x.tzinfo else x.replace(tzinfo=timezone.utc))
        except Exception:
            pass
    late = bool(starts) and datetime.now(timezone.utc) - max(starts) > timedelta(days=3)
    if not r["legs"]:
        r["result"] = "empty"; return True
    if all(res) or late:
        miss = [l["who"] for l in r["legs"] if l.get("result") == "loss"]
        r["result"] = "unknown" if not all(res) else ("lost" if miss else "won")
        r["misses"] = miss; r["hits"] = sum(1 for x in res if x == "win"); ch = True
    return ch


def record_line():
    rows = [r for r in _read() if r.get("result") in ("won", "lost")]
    pend = sum(1 for r in _read() if not r.get("result"))
    if not rows:
        return "🕒 Best 6 by time of day: saving the card at 9 AM, 12 PM, 4:30 PM and 7 PM with its strength. First results after tonight's games."
    def rec(sel):
        w = sum(1 for r in sel if r["result"] == "won"); return "won %d of %d" % (w, len(sel)) if sel else "none yet"
    full = [r for r in rows if len(r["legs"]) >= 6]
    t = " · ".join("%s %s" % (name, rec([r for r in full if r["slot"] == name])) for _, name in SLOTS)
    s = " · ".join("%s %s" % (k, rec([r for r in rows if r["strength"] == k])) for k in ("💪 STRONG", "✅ OK", "⚠️ WEAK"))
    return "🕒 Best 6 by time of day (full 6-pick cards, every pick must hit): %s\n📊 By strength (all saved cards): %s%s" % (t, s, " · %d waiting on games" % pend if pend else "")


def run(post=True):
    out = []
    if not ON: return out
    try:
        import apex_best6 as B, apex_ops as ops
        say = (lambda t: ops.send_discord("system", t)) if post else (lambda t: print("---- would post to #system ----\n" + t))
        rows = _read(); now = datetime.now(CT); ch = False
        slot = _due(now, rows)
        if slot:
            picks, _, _ = B.build(B.SIZE)
            legs = [B._leg(x) for x in picks]
            st = strength([l["num"] for l in legs], B.SIZE)
            word = {"STRONG": "💪 STRONG", "OK": "✅ OK", "WEAK": "⚠️ WEAK"}[st]
            rows.append(dict(day=now.strftime("%Y-%m-%d"), slot=slot, saved=now.isoformat(timespec="seconds"), strength=word, legs=legs)); ch = True
            t = "🕒 Best 6 saved (%s card, %s): %d picks, %s%s" % (slot, now.strftime("%-m/%-d"), len(legs), word,
                ("; weakest %s (%d)" % (min(legs, key=lambda l: l["num"])["who"], round(min(l["num"] for l in legs))) if legs else ""))
            say(t); out.append(t)
        for r in rows:
            if _finish(r):
                ch = True
                if r["result"] in ("won", "lost"):
                    t = "🕒 Best 6 %s card %s (%s, %d picks): %s" % (r["slot"], r["day"][5:].replace("-", "/"), r["strength"], len(r["legs"]),
                        "✅ won, every pick that played hit" if r["result"] == "won" else "❌ lost (%d hit; missed: %s)" % (r["hits"], ", ".join(r["misses"])))
                    say(t); out.append(t)
        if ch: _write(rows)
    except Exception as e:
        print("[best6 times] skipped: %s: %s" % (type(e).__name__, e))
    return out
