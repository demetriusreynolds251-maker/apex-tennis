#!/usr/bin/env python3
"""APEX-BESTTIME-2026-10-31 (owner, "Go time trackers"): when to play. Top 3, Elite 5 and Elite 6 exactly as they stood at fixed
check-in times, only picks not started yet, each graded as a whole slip at $[amount]. "Last board" = last board before that slip's first match.
Real days count from START_DAY. The 10/12-10/30 replay is kept apart in data/best_time_replay.json. A time needs MIN_DAYS graded days
before it can be called best. Reads data/board_history.sqlite only; never touches boards, cards or slips.
/besttime and one line in the daily status. Undo: BEST_TIME_TRACKER_ON = False. Run: python3 apex_best_time.py"""
import json, os, sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BEST_TIME_TRACKER_ON = True
START_DAY = "2026-10-31"
MIN_DAYS = 5
STAKE = 20.0
SLOTS = [("8 AM", 8), ("11 AM", 11), ("1 PM", 13), ("3 PM", 15), ("5 PM", 17), ("8 PM", 20), ("11 PM", 23)]
LAST = "Last board"   # the last board before the first match
SLIP_NAMES = {"top3": "Top 3", "elite5": "Elite 5", "elite6": "Elite 6"}
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "board_history.sqlite")
REPLAY = os.path.join(HERE, "data", "best_time_replay.json")
CT = ZoneInfo("America/Chicago")


def _db():
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    return c


def _result(c, r):
    if r["result"]:
        return r["result"]
    if not r["match_start"]:
        return None
    x = c.execute("select result from history where prop_key=? and coalesce(result,'')!='' and substr(match_start,1,10)=? "
                  "order by id desc limit 1", (r["prop_key"], r["match_start"][:10])).fetchone()
    return x[0] if x else None


def _pay(slip, res):
    """Profit at $[amount] or None until every leg is graded (a lost slip is settled early)."""
    miss, void, wait = res.count("miss"), res.count("void"), res.count(None)
    if slip == "top3":
        if miss:
            return -STAKE
        return None if wait else (STAKE * 1.2 if not void else 0.0)   # 2.2x all Goblin; a void counted as money back
    if slip == "elite5":
        import apex_slips_v2 as v2
        if miss >= 2:
            return -STAKE
        if wait:
            return None
        if miss == 1:
            return STAKE * v2.ELITE5_ONE_MISS - STAKE
        return STAKE * v2.ELITE5_ALL_HIT.get(void, v2.ELITE5_ALL_HIT_MORE_VOIDS) - STAKE
    if miss >= 3:
        return -STAKE
    if wait:
        return None
    import apex_slips_v2 as v2
    return STAKE * v2.FLEX6.get(6 - miss, 0.0) - STAKE


def _slip_at(c, board, t_utc, t_ct):
    """Numbered picks not started at t, from the last board published at or before t."""
    p = c.execute("select publish_id from history where board=? and published_at<=? order by id desc limit 1",
                  (board, t_ct.strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
    if not p:
        return []
    rows = c.execute("select * from history where publish_id=? and board=? and spot_kind='main' order by spot_no", (p[0], board)).fetchall()
    out = []
    for r in rows:
        if r["match_start"] and datetime.fromisoformat(r["match_start"]).astimezone(timezone.utc) <= t_utc:
            continue
        out.append(r)
    return out


def _official(c, board, day):
    """Numbered picks on the last board before that board's own first pick started, for this board day."""
    pubs = c.execute("select publish_id, min(published_at) t from history where board_day=? and board=? group by publish_id order by publish_id",
                     (day.isoformat(), board)).fetchall()
    pt = lambda x: datetime.fromisoformat(x).replace(tzinfo=CT).astimezone(timezone.utc)
    for i, p in enumerate(pubs):
        rows = c.execute("select * from history where publish_id=? and board=? and spot_kind='main' order by spot_no", (p["publish_id"], board)).fetchall()
        st = [datetime.fromisoformat(r["match_start"]).astimezone(timezone.utc) for r in rows if r["match_start"]]
        if not st or pt(p["t"]) >= min(st):
            continue
        if i + 1 == len(pubs) or pt(pubs[i + 1]["t"]) >= min(st):
            return rows
    return []


def _build(c, day):
    """{slot: {slip: dict(legs, result, profit, key)}} for one calendar day. A time still ahead is left out."""
    out = {}
    now = datetime.now(CT)
    times = [(n, datetime(day.year, day.month, day.day, h, tzinfo=CT)) for n, h in SLOTS]
    for name, t in times + [(LAST, None)]:
        if t is not None and t > now:
            continue
        if t is None:
            e6, t3 = _official(c, "elite6", day), _official(c, "top3", day)
        else:
            tu = t.astimezone(timezone.utc)
            e6, t3 = _slip_at(c, "elite6", tu, t), _slip_at(c, "top3", tu, t)
        slot = {}
        for slip, legs, n in (("top3", t3[:3], 3), ("elite5", e6[:5], 5), ("elite6", e6[:6], 6)):
            if len(legs) < n:
                slot[slip] = {"short": True, "n": len(legs)}
                continue
            res = [_result(c, r) for r in legs]
            slot[slip] = {"short": False, "profit": _pay(slip, res), "won": res.count("miss") == 0 and None not in res,
                          "res": res, "key": tuple(sorted(r["prop_key"] or "" for r in legs))}
        out[name] = slot
    return out


def real_days():
    today = datetime.now(CT).date()
    d, days = date.fromisoformat(START_DAY), {}
    c = _db()
    try:
        while d <= today:
            days[d.isoformat()] = _build(c, d)
            d += timedelta(days=1)
    finally:
        c.close()
    return days


def _tally(days):
    T = {n: {s: [0, 0, 0.0, 0] for s in SLIP_NAMES} for n in [x for x, _ in SLOTS] + [LAST]}   # won, graded, profit, short
    for v in days.values():
        for name, slot in v.items():
            for s, r in slot.items():
                t = T[name][s]
                if r.get("short"):
                    t[3] += 1
                elif r.get("profit") is not None:
                    t[0] += bool(r["won"])
                    t[1] += 1
                    t[2] += r["profit"]
    return T


def _money(x):
    return ("+$%.0f" % x) if x >= 0 else ("-$%.0f" % -x)


def _replay():
    with open(REPLAY) as fh:
        return json.load(fh)["slots"]


def _best_replay(rp, s):
    return max(rp, key=lambda n: rp[n][s][2])


def changed_lines(days, n=3):
    """'Sat: Elite 5 changed at 3 PM, helped' for recent days. Never raises."""
    out = []
    for d in sorted(days, reverse=True)[:n + 1]:
        v, prev, notes = days[d], None, []
        for name, _ in SLOTS:
            r = v.get(name, {}).get("elite5") or {}
            if not r or r.get("short"):
                continue
            if prev and r["key"] != prev["key"]:
                a, b = prev.get("profit"), r.get("profit")
                notes.append("%s %s" % (name, "pending" if a is None or b is None else "helped" if b > a else "hurt" if b < a else "same result"))
            prev = r
        if notes:
            out.append("%s Elite 5 changed: %s" % (date.fromisoformat(d).strftime("%a %-m/%-d"), ", ".join(notes)))
    return out[:n]


def report_text():
    if not BEST_TIME_TRACKER_ON:
        return "Best time tracker is off."
    days, rp = real_days(), _replay()
    T = _tally(days)
    L = ["⏰ **BEST TIME TO PLAY** -- slip as it stood at that time, only picks not started, $20", "",
         "**Real days since 10/31**"]
    for name in [x for x, _ in SLOTS] + [LAST]:
        t = T[name]
        L.append("%s: %s" % (name, " · ".join("%s %s" % (SLIP_NAMES[s], ("%d of %d %s" % (t[s][0], t[s][1], _money(t[s][2]))) if t[s][1] else "no graded day") for s in SLIP_NAMES)))
    enough = max(T[n]["elite5"][1] for n, _ in SLOTS)
    L.append("Best time so far: too few days (needs %d graded days at a time)." % MIN_DAYS if enough < MIN_DAYS else
             "Best time so far: " + " · ".join("%s %s" % (SLIP_NAMES[s], max((n for n, _ in SLOTS if T[n][s][1] >= MIN_DAYS), key=lambda n: T[n][s][2], default="too few days")) for s in SLIP_NAMES))
    L += ["", "**Replay 10/12-10/30** (today's rules on past days, days with an ungraded leg left out)"]
    for name, _ in SLOTS:
        L.append("%s: %s" % (name, " · ".join("%s %d of %d %s" % (SLIP_NAMES[s], rp[name][s][0], rp[name][s][1], _money(rp[name][s][2])) for s in SLIP_NAMES)))
    L.append("Best in replay: " + " · ".join("%s %s" % (SLIP_NAMES[s], _best_replay(rp, s)) for s in ("elite5", "elite6")) + " · Top 3 about even 1 PM to 8 PM")
    ch = changed_lines(days)
    if ch:
        L += ["", "**When the slip changed**"] + ch
    return "\n".join(L)


def status_line():
    """One line for the daily status. Says so if the tracker fails. Never raises."""
    if not BEST_TIME_TRACKER_ON:
        return None
    try:
        T, rp = _tally(real_days()), _replay()
        real = " · ".join("%s %d of %d %s" % (n, T[n]["elite5"][0], T[n]["elite5"][1], _money(T[n]["elite5"][2])) for n in [x for x, _ in SLOTS] + [LAST] if T[n]["elite5"][1])
        return "⏰ Best time: Elite 5 real days since 10/31: %s. Replay best: Elite 5 %s, Elite 6 %s (/besttime)" % (
            real or "none graded yet", _best_replay(rp, "elite5"), _best_replay(rp, "elite6"))
    except Exception as e:
        return "⚠️ Best time tracker failed: %s" % type(e).__name__


if __name__ == "__main__":
    print(report_text())
    print()
    print(status_line())
