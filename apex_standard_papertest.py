#!/usr/bin/env python3
"""
APEX-STDPAPER-2026-10-12 (owner, "Yes" 2026-10-12): paper test of a
revised Best Standard rule. Nothing is posted and no board changes -- once a
day, at the first /fetch or 5 PM publish, it records what the revised rule
would have picked, next to what the live Best Standard board picked, then
grades both from settled results for the daily status.

Revised rule (from the 9/24-10/11 study; 8/24-9/20 did not confirm it, hence
a paper test rather than a board change):
  - Games Won MORE with Win Probability 52-56% (above 56% went 15-17)
  - Double Faults LESS with Win Probability 50%+
  - Fantasy Score MORE with Win Probability 54%+
  Standard lines only, one per match, ranked by Win Probability, top 6.

  python3 apex_standard_papertest.py           # print the running record
  python3 apex_standard_papertest.py --log     # record today's picks now
"""
import json, os, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "data", "logs", "standard_papertest.json")
START = "2026-10-13"
RULES = {("games_won", "MORE"): (0.52, 0.56), ("dfs", "LESS"): (0.50, 1.01), ("fantasy_score", "MORE"): (0.54, 1.01)}
MARKET_NAMES = {("games_won", "MORE"): "Games Won MORE 52-56%", ("dfs", "LESS"): "Double Faults LESS 50%+",
                ("fantasy_score", "MORE"): "Fantasy Score MORE 54%+"}
LIMIT = 6


def _load():
    try:
        with open(LOG) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _leg(p, prob):
    return {k: p.get(k) for k in ("match_date", "player", "opponent", "category", "line", "direction")} | {"prob": round(prob, 4)}


def _match_key(p):
    names = str(p.get("opponent") or "").split(" v ") if p.get("player") == "match" else [p.get("player"), p.get("opponent")]
    return frozenset(str(x or "").strip().lower() for x in names)


def watch_picks(picks, cat="fantasy_score", direction="LESS", limit=6):
    """APEX-FSLESSWATCH-2026-10-13 (owner, "Yes"): a side watch list -- the
    top Standard props in one market, one per match, ranked by Win Probability.
    Fantasy Score LESS went 50% then 56% over 9/24-10/13; this tracks whether it holds."""
    import apex_edge_tracker as t
    cand = [(t._board_prob(p), p) for p in picks
            if str(p.get("prop_type") or "").lower() == "standard" and p.get("category") == cat
            and str(p.get("direction") or "").upper() == direction and not t._pub_match_started(p)]
    cand.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for pr, p in cand:
        k = _match_key(p)
        if k in seen:
            continue
        seen.add(k)
        out.append(_leg(p, pr))
        if len(out) >= limit:
            break
    return out


def revised_picks(picks):
    import apex_edge_tracker as t
    cand = []
    for p in picks:
        if str(p.get("prop_type") or "").lower() != "standard" or t._pub_match_started(p):
            continue
        band = RULES.get((p.get("category"), str(p.get("direction") or "").upper()))
        pr = t._board_prob(p)
        if band and band[0] - 1e-9 <= pr < band[1]:
            cand.append((pr, p))
    cand.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for pr, p in cand:
        k = _match_key(p)
        if k in seen:
            continue
        seen.add(k)
        out.append(_leg(p, pr))
        if len(out) >= LIMIT:
            break
    return out


def log_today(picks=None, source="", today=None):
    """Record once per day (first publish wins). Never raises."""
    try:
        import apex_edge_tracker as t
        today = today or datetime.now().date().isoformat()
        data = _load()
        if today in data or today < START:
            return False
        if picks is None:
            picks, _ = t.load_current_board()
        res = t.best_standard_detail(picks, return_scored=True)
        current = [_leg(d["p"], t._board_prob(d)) for d in (res.get("scored") or [])] if isinstance(res, dict) else []
        # APEX-STANDARDRANK-2026-10-22: the Win-Probability-only order, kept as its own arm
        _sw = getattr(t, "BEST_STANDARD_MARKET_RANK", False)
        try:
            t.BEST_STANDARD_MARKET_RANK = False
            r0 = t.best_standard_detail(picks, return_scored=True)
        finally:
            t.BEST_STANDARD_MARKET_RANK = _sw
        winprob = [_leg(d["p"], t._board_prob(d)) for d in (r0.get("scored") or [])] if isinstance(r0, dict) else []
        data[today] = {"logged_at": datetime.now().isoformat(timespec="seconds"), "source": source,
                       "revised": revised_picks(picks), "current": current, "winprob_only": winprob,
                       "fs_less": watch_picks(picks)}
        tmp = LOG + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1, default=str)
        os.replace(tmp, LOG)
        return True
    except Exception as e:
        print("[StdPaperTest] log skipped (%s): %s" % (type(e).__name__, e))
        return False


def _grade(S, leg):
    m = S.find(leg.get("match_date"), leg.get("player"), leg.get("opponent"), leg.get("category"),
               leg.get("line"), leg.get("direction"))
    return None if m is None else m.get("hit")


def summary():
    """Short lines for the daily status; [] until there is something graded."""
    data = _load()
    if not data:
        return []
    import apex_engine_validation as ev
    _t, _rep, S, _st = ev.load()
    rec = {"revised": [0, 0], "current": [0, 0]}
    fsl = [0, 0]
    mk = {k: [0, 0] for k in RULES}
    slips = {"revised": [0, 0, 0.0, 0.0], "current": [0, 0, 0.0, 0.0]}   # days, power wins, power net, flex net per $[amount]
    for day, v in sorted(data.items()):
        for side in ("revised", "current"):
            hits = [_grade(S, lg) for lg in v.get(side) or []]
            for lg, h in zip(v.get(side) or [], hits):
                if h is None:
                    continue
                rec[side][0 if h else 1] += 1
                if side == "revised":
                    k = (lg.get("category"), str(lg.get("direction") or "").upper())
                    if k in mk:
                        mk[k][0 if h else 1] += 1
            top3 = hits[:3]
            if len(top3) == 3 and None not in top3:
                w = sum(1 for h in top3 if h)
                s = slips[side]
                s[0] += 1
                s[1] += 1 if w == 3 else 0
                s[2] += (60 if w == 3 else 0) - 10
                s[3] += (30 if w == 3 else 10 if w == 2 else 0) - 10
    if not sum(rec["revised"]) and not sum(rec["current"]):
        return ["Standard paper test (since %s): no graded picks yet" % START]

    def r(wl):
        n = wl[0] + wl[1]
        return "%d-%d (%.0f%%)" % (wl[0], wl[1], 100.0 * wl[0] / n) if n else "--"

    def sl(s):
        return ("%d slip%s, Power won %d, $10 a day: Power %s$%.0f, Flex %s$%.0f"
                % (s[0], "" if s[0] == 1 else "s", s[1], "+" if s[2] >= 0 else "-", abs(s[2]),
                   "+" if s[3] >= 0 else "-", abs(s[3]))) if s[0] else "no full 3-pick slips graded yet"
    L = ["Standard paper test since %s (not posted; 55%% a pick breaks even on a 6x Power)" % START,
         "New rule: picks %s · %s" % (r(rec["revised"]), sl(slips["revised"])),
         "Current Best Standard: picks %s · %s" % (r(rec["current"]), sl(slips["current"])),
         "By market: " + " · ".join("%s %s" % (MARKET_NAMES[k], r(v)) for k, v in mk.items())]
    for day, v in data.items():
        for lg in v.get("fs_less") or []:
            h = _grade(S, lg)
            if h is not None:
                fsl[0 if h else 1] += 1
    L.append("Watch -- Fantasy Score LESS (top 6 a day): %s" % r(fsl))
    wp = [0, 0]   # APEX-STANDARDRANK-2026-10-22: Win-Prob-only order (since 10/22)
    for day, v in data.items():
        for lg in v.get("winprob_only") or []:
            h = _grade(S, lg)
            if h is not None:
                wp[0 if h else 1] += 1
    if sum(wp):
        L.append("Best Standard by Win Prob only (old order, not posted): picks %s" % r(wp))
    return L


FINISH_DAYS, FINISH_PICKS = 21, 60      # APEX-TESTDONE-2026-10-13


def finish_check():
    """(finished, card rows) once the test has 21+ logged days and 60+ graded
    new-rule picks; (False, None) before that."""
    data = _load()
    if len(data) < FINISH_DAYS:
        return False, None
    import apex_engine_validation as ev
    _t, _rep, S, _st = ev.load()
    rec = {"revised": [0, 0], "current": [0, 0], "fs_less": [0, 0]}
    for day, v in data.items():
        for side in rec:
            for lg in v.get(side) or []:
                h = _grade(S, lg)
                if h is not None:
                    rec[side][0 if h else 1] += 1
    n_new = sum(rec["revised"])
    if n_new < FINISH_PICKS:
        return False, None
    pn = rec["revised"][0] / n_new
    nc = sum(rec["current"])
    pc = rec["current"][0] / nc if nc else None
    beats = pc is None or pn > pc + 0.02
    verdict = ("PASSED -- the new rule clears 55%% and beats the current board" if pn >= 0.55 and beats else
               "FAILED -- the new rule is under 55%%" if pn < 0.55 else
               "MIXED -- clears 55%% but doesn't beat the current board")
    return True, [("Test", "Best Standard revised rule (paper test since %s)" % START),
                  ("New rule", "%d-%d (%.0f%%)" % (rec["revised"][0], rec["revised"][1], 100 * pn)),
                  ("Current Best Standard", ("%d-%d (%.0f%%)" % (rec["current"][0], rec["current"][1], 100 * pc)) if pc is not None else "no graded picks"),
                  ("Fantasy Score LESS watch", ("%d-%d (%.0f%%)" % (rec["fs_less"][0], rec["fs_less"][1], 100.0 * rec["fs_less"][0] / sum(rec["fs_less"])))
                   if sum(rec["fs_less"]) else "no graded picks"),
                  ("Verdict", verdict.replace("%%", "%")),
                  ("Decision", "switch Best Standard to the new rule / keep the current rule / keep testing")]


if __name__ == "__main__":
    if "--log" in sys.argv:
        print("logged" if log_today(source="manual-cli") else "already logged today (or skipped)")
    print("\n".join(summary()) or "nothing logged yet")
