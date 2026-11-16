#!/usr/bin/env python3
"""APEX-BOARDCOUNTERS-2026-10-31 (owner, "Go board counter"): one whole-slip record per board since START_DAY, today's rules only.
Slip = the board's top picks as numbered on its official board (last board before its own first pick), graded at $[amount]; a pick record too.
Payouts are the ones in the code or estimates: Goblin 6 Flex 3x/1x/0.4x (apex_slips_v2.FLEX6), Standard 3 Power 6x, Demon 3 Power 14x
(estimate from the ~41 of 100 a leg a Demon 3 needs); the app has the real number. Reads data/board_history.sqlite only; no board change.
Elite 5 has its own line in Slips v2. /counters and one line in the daily status. Undo: BOARD_COUNTERS_ON = False. Run: python3 apex_board_counters.py"""
from datetime import date, datetime, timedelta
import apex_best_time as bt

BOARD_COUNTERS_ON = True
START_DAY = "2026-10-31"
STAKE = 20.0
POWER3 = {"top3": 2.2, "beststandard": 6.0, "bestdemon": 14.0}
# board: (name, picks on the slip, play)
BOARDS = [("top3", "Top 3", 3, "Power"), ("elite6", "Elite 6", 6, "Flex"), ("top10", "Top 10", 6, "Flex"), ("bestgoblin", "Best Goblin", 6, "Flex"),
          ("beststandard", "Best Standard", 3, "Power"), ("bestdemon", "Best Demon", 3, "Power"), ("allprops", "All Props", 6, "Flex")]


def _legs(c, board, day):
    rows = bt._official(c, board, day)
    if board == "allprops":   # its numbered list also shows props that are held or under the floor
        rows = [r for r in rows if (r["win_prob"] or 0) >= 70 and (r["apex_score"] or 0) >= 67 and not str(r["reason"] or "").startswith("held")]
        used, out = [], []
        for r in sorted(rows, key=lambda x: x["spot_no"] or 99):
            n = bt_names(r)
            if any(n & u for u in used):
                continue
            used.append(n)
            out.append(r)
        return out
    return rows


def bt_names(r):
    t = r["opponent"] if r["player"] == "match" else "%s v %s" % (r["player"], r["opponent"])
    return frozenset(x.strip().lower() for x in (t or "").split(" v "))


def _profit(board, play, res):
    miss, void, wait = res.count("miss"), res.count("void"), res.count(None)
    if play == "Power":
        if miss:
            return -STAKE
        return None if wait else (STAKE * (POWER3[board] - 1) if not void else 0.0)
    return bt._pay("elite6", res)


def records():
    today = datetime.now(bt.CT).date()
    out = {}
    c = bt._db()
    try:
        for board, name, n, play in BOARDS:
            t = dict(name=name, n=n, play=play, days=0, won=0, short=0, wait=0, money=0.0, hit=0, miss=0)
            d = date.fromisoformat(START_DAY)
            while d <= today:
                rows = _legs(c, board, d)
                if rows:
                    legs = rows[:n]
                    res = [bt._result(c, r) for r in legs]
                    t["hit"] += res.count("hit")
                    t["miss"] += res.count("miss")
                    if len(legs) < n:
                        t["short"] += 1
                    else:
                        p = _profit(board, play, res)
                        if p is None:
                            t["wait"] += 1
                        else:
                            t["days"] += 1
                            t["won"] += res.count("miss") == 0
                            t["money"] += p
                d += timedelta(days=1)
            out[board] = t
    finally:
        c.close()
    return out


def report_text():
    if not BOARD_COUNTERS_ON:
        return "Board counters are off."
    L = ["\U0001F4CB **BOARD COUNTERS** since 10/31, today's rules, official board, $20 a slip", ""]
    for b, t in records().items():
        L.append("%s (%d-pick %s): won %d of %d day%s %s · picks %d-%d%s%s" % (t["name"], t["n"], t["play"], t["won"], t["days"], "" if t["days"] == 1 else "s",
                 bt._money(t["money"]), t["hit"], t["miss"], (" · %d waiting" % t["wait"]) if t["wait"] else "", (" · %d short" % t["short"]) if t["short"] else ""))
    L += ["", "Elite 5 has its own line in /slipsv2. Payouts are estimates where the app varies. Older records stay where they are and mix older rules."]
    return "\n".join(L)


def status_line():
    """One line for the daily status. Says so if the counters fail. Never raises."""
    if not BOARD_COUNTERS_ON:
        return None
    try:
        return "\U0001F4CB Boards since 10/31: " + " · ".join("%s %d of %d %s" % (t["name"], t["won"], t["days"], bt._money(t["money"])) for t in records().values()) + " (/counters)"
    except Exception as e:
        return "⚠️ Board counters failed: %s" % type(e).__name__


if __name__ == "__main__":
    print(report_text())
    print()
    print(status_line())
