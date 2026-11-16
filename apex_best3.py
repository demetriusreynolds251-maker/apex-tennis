#!/usr/bin/env python3
"""APEX-BEST3-2026-11-14 (owner, "I said fix all"; "go best 3"): /best3 in the tennis server. The best 3 picks taken only from each
sport's own numbered TOP 3 (NFL, NHL, NBA; college football off like Best 6; no tennis, soccer or college basketball), at most 2 per sport,
games today or tomorrow (same window as Best 6). Each pick shows its honest real number like Best 6. NEW, NOT PROVEN YET: the 11/5-11/13
replay won 6 of 9 days (older days tied with the Top 10 rule, newer days 4 of 4).
Tracker: the card is saved once a day at the first check at or after 4:30 PM Central (apex_alerts.check, every 30 min) to
data/best3_tracker.jsonl, graded from each sport's own results the way PrizePicks grades (a played 0 is a loss), one #system line when
saved and when graded, and the record shows on the card. Read only on every board. Undo: ON = False, restart the tennis bot."""
import json, os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ON = True
SIZE = 3
PER_SPORT = 2
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "data", "best3_tracker.jsonl")
CT = ZoneInfo("America/Chicago")
SAVE_AT = (16, 30)
LABEL = "🆕 NEW, NOT PROVEN YET (replay 11/5-11/13: won 6 of 9 days; older days tied the Top 10 rule, newer days 4 of 4)."


def build():
    import apex_best6 as B
    per, notes = {}, []
    for root, name in B._roots():
        per[name], err = B._sport(root, "top3")
        if err: notes.append("%s could not be read (%s), left out" % (name, err))
        per[name] = B._real80([x for x in (per[name] or []) if x.get('spot') is None or x['spot'] <= (B.TOP_SPOTS or 99)], name)   # APEX-BEST6REAL80-2026-11-14: real number 80+, ranked by it
    picks, rest = [], []
    for root, name in B._roots():
        for i, x in enumerate(per.get(name) or []):
            x["sport"] = name
            (picks if i < PER_SPORT else rest).append(x)
    picks.sort(key=B._rk); picks = picks[:SIZE]
    rest.sort(key=B._rk)
    while len(picks) < SIZE and rest: picks.append(rest.pop(0))
    picks.sort(key=B._rk)
    return picks, {name: len(v) for name, v in per.items()}, notes


def render():
    if not ON: return "🥉 BEST 3 is turned off."
    import apex_best6 as B
    picks, counts, notes = build()
    L = ["🥉 **BEST 3** · from each sport's own Top 3 only · best 3 kept · games %s only" % B._window_word(), LABEL]
    if B.CFB_OFF_CARDS: L.append(B.CFB_OFF_LINE)
    if B.REAL_MIN is not None: L.append("🎯 Only picks whose REAL win rate is %d+, ranked by it." % B.REAL_MIN)
    L.append("Qualified right now (numbered Top 3 picks in the window): " + " · ".join("%s %d" % (n, c) for n, c in counts.items()))
    if not picks: L.append("🚫 No Top 3 pick qualifies right now. Nothing to play from this card.")
    elif len(picks) < SIZE: L.append("⚠️ Short card: %d of 3 qualified. This is not a full 3-pick slip." % len(picks))
    try:
        import apex_strength as _ST
        _sl, _sh = _ST.block("best3", picks, SIZE)
    except Exception:
        _sl, _sh = [], {}
    L += _sl
    for n, x in enumerate(picks, 1):
        card = x["card"].replace("**#0 · ", "**")
        L += ["━━━━━━━━━━━━", "**%d.** %s" % (n, x["sport"]) + ((" · " + _sh[x.get("key")]) if _sh.get(x.get("key")) else "")] + ([B.gap_line(x)] if B.gap_line(x) else []) + [card]
    for t in notes: L.append("⚠️ " + t)
    L.append(record_line())
    L.append("Check every line in the app before you play. Same picks as each sport's own Top 3; nothing was re-pulled.")
    return "\n".join(L)


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


def _finish(r):
    import apex_best6 as B
    if r.get("result"): return False
    ch = False
    for l in r["legs"]:
        if l.get("result") is None:
            l["saved_day"] = r["day"]; g = B._grade(l)
            if g: l["result"] = g; ch = True
    res = [l.get("result") for l in r["legs"]]
    if not r["legs"]:
        r["result"] = "empty"; return True
    starts = []
    for l in r["legs"]:
        try:
            x = datetime.fromisoformat(str(l["start"]).replace("Z", "+00:00")); starts.append(x if x.tzinfo else x.replace(tzinfo=timezone.utc))
        except Exception:
            pass
    late = bool(starts) and datetime.now(timezone.utc) - max(starts) > timedelta(days=3)
    if all(res) or late:
        miss = [l["who"] for l in r["legs"] if l.get("result") == "loss"]
        r["result"] = "short" if len(r["legs"]) < SIZE else "unknown" if not all(res) else ("lost" if miss else "won")
        r["misses"] = miss; r["hits"] = sum(1 for x in res if x == "win"); ch = True
    return ch


def record_line():
    rows = _read()
    if not rows: return "📒 Best 3 tracker: starts with today's 4:30 PM card (saved and graded every day)."
    w = sum(1 for r in rows if r.get("result") == "won"); lo = sum(1 for r in rows if r.get("result") == "lost")
    sh = sum(1 for r in rows if r.get("result") == "short"); wt = sum(1 for r in rows if not r.get("result"))
    return "📒 Best 3 tracker since %s (the 4:30 PM card; all 3 must hit): won %d, lost %d%s%s" % (
        rows[0]["day"][5:].replace("-", "/"), w, lo, ", %d short" % sh if sh else "", ", %d waiting" % wt if wt else "")


def run(post=True):
    """Called from apex_alerts.check() every 30 min: save today's card once at 4:30 PM, grade open days. Never raises."""
    out = []
    if not ON: return out
    try:
        import apex_ops as ops
        say = (lambda t: ops.send_discord("system", t)) if post else (lambda t: print("---- would post to #system ----\n" + t))
        rows = _read(); now = datetime.now(CT); today = now.strftime("%Y-%m-%d"); ch = False
        if (now.hour, now.minute) >= SAVE_AT and not any(r["day"] == today for r in rows):
            picks, counts, notes = build()
            legs = [dict(sport=x.get("sport"), who=x.get("who"), key=x.get("key"), db=x.get("db"), num=x.get("num"), start=x.get("start"), result=None) for x in picks]
            rows.append(dict(day=today, saved=now.isoformat(timespec="seconds"), legs=legs)); ch = True
            t = "🥉 Best 3 tracker: saved today's card (%s): %s" % (now.strftime("%-I:%M %p CT"), " · ".join(l["who"] for l in legs) if legs else "no picks") + (" (short, will not count)" if len(legs) < SIZE else "")
            say(t); out.append(t)
        for r in rows:
            had = r.get("result")
            if _finish(r):
                ch = True
                if r.get("result") and not had:
                    t = "🥉 Best 3 result %s: %s" % (r["day"][5:].replace("-", "/"), "✅ won, all 3 hit" if r["result"] == "won" else
                                                    "❌ lost (missed: %s)" % ", ".join(r.get("misses") or []) if r["result"] == "lost" else "⚠️ %s, not counted" % r["result"])
                    say(t); out.append(t)
        if ch and post: _write(rows)
    except Exception as e:
        print("[best3] tracker skipped: %s: %s" % (type(e).__name__, e))
    return out


if __name__ == "__main__":
    print(render())
