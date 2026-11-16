#!/usr/bin/env python3
"""
APEX-DFFANTASY-2026-10-22 (owner, "1,2.3 now" on the 10/22 DF/Fantasy study):
background test of the two new BACKUPS holds -- Fantasy Score LESS, and Double Faults
LESS when the player played the day before -- in SLIP terms. Nothing is posted and no
board changes.

  with     = the Top 3 / Elite 6 as posted (apex_board_status_log_v1.csv, holds on)
  without  = the same boards built at the same publish with those two holds off
             (17.5 rules still on), logged by apex_edge_tracker._write_holds_off
             (apex_dffs_holds_off_log.csv)

One slip a day per arm: the 5 PM board (source "auto"), else the first board of the day.
Elite 6 = $[amount] 6-pick (Power 2.75x when all 6 hit; Flex 6x at 6/6, 0.4x at 5/6).
Top 3 = $[amount] 3-pick Power (2.2x all Goblins; 3.25 / 3.75 / 6x with 1 / 2 / 3 Standards).
TEST FINISHED after FINISH_DAYS days with a graded slip in both arms.

  python3 apex_dffs_holds_papertest.py          # print the running record
  python3 apex_dffs_holds_papertest.py --days   # day-by-day slips
"""
import csv, os, sys
from collections import OrderedDict, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_LOG = os.path.join(HERE, "apex_board_status_log_v1.csv")
OFF_LOG = os.path.join(HERE, "apex_dffs_holds_off_log.csv")
START = "2026-10-23"
FINISH_DAYS = 14
STAKE = 20
G6_POWER = 2.75
G6_FLEX = {6: 6.0, 5: 0.4}
POW3 = {0: 2.2, 1: 3.25, 2: 3.75, 3: 6.0}   # by number of non-Goblin legs (his screenshots)
BOARDS = (("elite6", "Elite 6", 6), ("top3", "Top 3", 3))
TEST_ON = True


def _key(r):
    return (r["match_date"], r["player"], r["opponent"], r["category"], str(r["line"]), str(r["direction"]).upper())


def _read(path, board_of, keep):
    """{board: {match_date: OrderedDict(captured_at -> {"source", "legs": [rows]})}}"""
    out = {b: defaultdict(OrderedDict) for b, _, _ in BOARDS}
    if not os.path.exists(path):
        return out
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            b = board_of(r)
            if b not in out or r["match_date"] < START or not keep(r):
                continue
            out[b][r["match_date"]].setdefault(r["captured_at"], {"source": r["source"], "legs": []})["legs"].append(r)
    for b in out:
        for md in out[b]:
            for c in out[b][md]:
                out[b][md][c]["legs"].sort(key=lambda r: float(r["position"] or 99))
    return out


def _arms():
    return {"with": _read(STATUS_LOG, lambda r: r["board"], lambda r: r["status"] == "recommended"),
            "without": _read(OFF_LOG, lambda r: r["board"], lambda r: True)}


def _grader():
    import apex_engine_validation as ev
    _t, _rep, S, _st = ev.load()
    memo = {}

    def hit(r):
        k = _key(r)
        if k not in memo:
            m = S.find(r["match_date"], r["player"], r["opponent"], r["category"], r["line"], r["direction"])
            memo[k] = None if m is None else m.get("hit")
        return memo[k]
    return hit


def _slip_board(caps):
    return next((cap for c, cap in caps.items() if cap["source"] == "auto"), next(iter(caps.values())))["legs"]


def tally(since=START, until=None):
    """{(board, arm): {"days", "won", "power", "flex", "k": {hits: days}}} plus day detail."""
    hit = _grader()
    A = _arms()
    T, detail = {}, []
    for b, _lbl, n in BOARDS:
        days = sorted(set(A["with"][b]) & set(A["without"][b]))
        for arm in ("with", "without"):
            x = T.setdefault((b, arm), {"days": 0, "won": 0, "power": 0.0, "flex": 0.0, "k": defaultdict(int)})
            for md in days:
                if md < since or (until and md > until):
                    continue
                legs = _slip_board(A[arm][b][md])[:n]
                hs = [hit(r) for r in legs]
                detail.append((md, b, arm, ["%s %s %s %s" % (r["opponent"] if r["player"] == "match" else r["player"],
                                                             r["category"].replace("_", " "), r["line"],
                                                             {True: "W", False: "L", None: "?"}[h]) for r, h in zip(legs, hs)]))
                if len(legs) < n or None in hs:
                    continue
                k = sum(1 for h in hs if h)
                x["days"] += 1
                x["k"][k] += 1
                if b == "elite6":
                    x["won"] += k == 6
                    x["power"] += STAKE * G6_POWER - STAKE if k == 6 else -STAKE
                    x["flex"] += STAKE * G6_FLEX.get(k, 0) - STAKE
                else:
                    ns = sum(1 for r in legs if str(r.get("prop_type") or "").lower() != "goblin")
                    x["won"] += k == 3
                    x["power"] += STAKE * POW3.get(ns, 2.2) - STAKE if k == 3 else -STAKE
    return T, detail


def _arm_text(b, x):
    if b == "elite6":
        return "days all 6 hit %d of %d ($%+.0f Power, $%+.0f Flex; 5 of 6 on %d days)" % (
            x["won"], x["days"], x["power"], x["flex"], x["k"].get(5, 0))
    return "days won %d of %d ($%+.0f)" % (x["won"], x["days"], x["power"])


def _lines(T):
    L = []
    for b, lbl, _n in BOARDS:
        w, o = T[(b, "with")], T[(b, "without")]
        L.append("%s with the Fantasy LESS / DF holds: %s. Without: %s." % (lbl, _arm_text(b, w), _arm_text(b, o)))
    return L


def summary():
    """Daily #system status lines (apex_alerts). Never raises."""
    if not TEST_ON:
        return []
    try:
        T, _ = tally(START)
        if not T[("elite6", "with")]["days"] and not T[("top3", "with")]["days"]:
            return ["Fantasy LESS / DF holds test starts %s (no graded slip yet; not posted, boards as posted)" % START]
        return ["Fantasy LESS / DF holds test since %s (one slip a day, $%d; not posted)" % (START, STAKE)] + _lines(T)
    except Exception as e:
        return ["Fantasy LESS / DF holds test: unavailable (%s)" % type(e).__name__]


def finish_check():
    if not TEST_ON:
        return False, None
    T, _ = tally(START)
    if min(T[(b, a)]["days"] for b, _, _ in BOARDS for a in ("with", "without")) < FINISH_DAYS:
        return False, None
    gain = sum(T[(b, "with")]["power"] - T[(b, "without")]["power"] for b, _, _ in BOARDS) + \
        (T[("elite6", "with")]["flex"] - T[("elite6", "without")]["flex"])
    verdict = ("PASSED -- the holds made $%+.0f more across the Top 3 and Elite 6 slips" % gain if gain > 0 else
               ("FAILED -- the holds made $%+.0f across the Top 3 and Elite 6 slips" % gain if gain < 0 else
                "NO DIFFERENCE -- the holds did not change the money"))
    rows = [("Test", "Fantasy LESS and DF-after-a-match holds to BACKUPS (since %s, %d days)" % (
        START, T[("elite6", "with")]["days"]))]
    rows += [(lbl, l) for (_, lbl, _), l in zip(BOARDS, _lines(T))]
    rows += [("Verdict", verdict),
             ("Decision", "keep the holds / turn one off (FANTASY_LESS_TO_BACKUPS or DF_LESS_PLAYED_YESTERDAY_HOLD = False) / keep testing")]
    return True, rows


if __name__ == "__main__":
    print("\n".join(summary()))
    if "--days" in sys.argv:
        _T, D = tally(START)
        for md, b, arm, legs in D:
            print(md[5:], b, arm, "|", ", ".join(legs))
