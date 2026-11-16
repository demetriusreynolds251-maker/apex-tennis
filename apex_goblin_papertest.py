#!/usr/bin/env python3
"""
APEX-GOBPAPER-2026-10-13 (owner: "Don't make any changes to no board
or props ... Just let it test on the background"): paper test of a Goblin
80% rule for Top 3 and Best Goblin. Nothing is posted and no board changes --
once a day, at the first /fetch or 5 PM publish, it records what the 80% rule
would have picked next to what the live Top 3 and Best Goblin showed, then
grades both for the daily status.

Rule under test (from the 10/13 study: Goblin MORE at 80%+ Win Probability hit
76% on both 9/24-10/3 and 10/4-10/13; a Goblin 3-pick needs ~77% a pick):
  Goblin MORE, Win Probability 80%+, no Apex Score check, one per match and
  player, ranked by Win Probability. Top 3 = first 3, Best Goblin = first 6.

  python3 apex_goblin_papertest.py           # print the running record
"""
import json, os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "data", "logs", "goblin_papertest.json")
START = "2026-10-14"
MIN_PROB = 0.80
GOBLIN_3PICK_PAYOUT = 2.2      # plain 3-Goblin Power entry (10/11 screenshot)


def _load():
    try:
        with open(LOG) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _leg(p, prob):
    return {k: p.get(k) for k in ("match_date", "player", "opponent", "category", "line", "direction", "prop_type")} | {"prob": round(prob, 4)}


def _match_key(p):
    names = str(p.get("opponent") or "").split(" v ") if p.get("player") == "match" else [p.get("player"), p.get("opponent")]
    return frozenset(str(x or "").strip().lower() for x in names)


def rule_picks(picks, limit=6):
    import apex_edge_tracker as t
    cand = []
    for p in picks:
        if str(p.get("prop_type") or "").lower() != "goblin" or str(p.get("direction") or "").upper() != "MORE":
            continue
        if str(p.get("playable") or "") == "0" or t._pub_match_started(p):
            continue
        pr = t._board_prob(p)
        if pr >= MIN_PROB - 1e-9:
            cand.append((pr, p))
    cand.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for pr, p in cand:
        k = _match_key(p)
        if k & seen:
            continue
        seen |= k
        out.append(_leg(p, pr))
        if len(out) >= limit:
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
        snap = {}
        try:
            with open(t.PUBLISHED_BOARD_SNAPSHOT) as fh:
                snap = json.load(fh).get("boards") or {}
        except Exception:
            pass

        def live(b, n):
            return [_leg(e["p"], t._board_prob(e)) for e in ((snap.get(b) or {}).get("entries") or [])[:n]]
        data[today] = {"logged_at": datetime.now().isoformat(timespec="seconds"), "source": source,
                       "rule": rule_picks(picks), "top3": live("top3", 3), "bestgoblin": live("bestgoblin", 6)}
        tmp = LOG + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1, default=str)
        os.replace(tmp, LOG)
        return True
    except Exception as e:
        print("[GobPaperTest] log skipped (%s): %s" % (type(e).__name__, e))
        return False


def summary():
    data = _load()
    if not data:
        return []
    import apex_engine_validation as ev
    _t, _rep, S, _st = ev.load()

    def grade(leg):
        m = S.find(leg.get("match_date"), leg.get("player"), leg.get("opponent"), leg.get("category"),
                   leg.get("line"), leg.get("direction"))
        return None if m is None else m.get("hit")
    stats = {k: {"w": 0, "l": 0, "days": 0, "sweeps": 0, "net": 0.0} for k in ("rule3", "rule6", "top3", "bestgoblin")}
    for day, v in sorted(data.items()):
        for key, legs in (("rule3", (v.get("rule") or [])[:3]), ("rule6", v.get("rule") or []),
                          ("top3", v.get("top3") or []), ("bestgoblin", v.get("bestgoblin") or [])):
            hits = [grade(l) for l in legs]
            s = stats[key]
            s["w"] += sum(1 for h in hits if h is True)
            s["l"] += sum(1 for h in hits if h is False)
            top = hits[:3]
            if len(top) == 3 and None not in top:
                s["days"] += 1
                s["sweeps"] += 1 if all(top) else 0
                s["net"] += 10 * GOBLIN_3PICK_PAYOUT * (1 if all(top) else 0) - 10
    if not any(s["w"] + s["l"] for s in stats.values()):
        return ["Goblin 80%% paper test (since %s): no graded picks yet" % START]

    def line(label, s):
        n = s["w"] + s["l"]
        pct = ("%.0f%%" % (100.0 * s["w"] / n)) if n else "--"
        slip = (" · top-3 slip hit %d of %d days, $10/day %s$%.0f" % (s["sweeps"], s["days"], "+" if s["net"] >= 0 else "-", abs(s["net"]))) if s["days"] else ""
        return "%s: picks %d-%d (%s)%s" % (label, s["w"], s["l"], pct, slip)
    return ["Goblin 80%% paper test since %s (not posted; a Goblin 3-pick needs ~77%% a pick)" % START,
            line("80% rule Top 3", stats["rule3"]), line("Live Top 3", stats["top3"]),
            line("80% rule Best Goblin", stats["rule6"]), line("Live Best Goblin", stats["bestgoblin"])]


FINISH_DAYS, FINISH_PICKS = 21, 45      # APEX-TESTDONE-2026-10-13


def finish_check():
    """(finished, card rows) once 21+ days are logged and the 80% rule has 45+
    graded picks; (False, None) before that."""
    data = _load()
    if len(data) < FINISH_DAYS:
        return False, None
    import apex_engine_validation as ev
    _t, _rep, S, _st = ev.load()
    rec = {"rule": [0, 0], "top3": [0, 0], "bestgoblin": [0, 0]}
    for day, v in data.items():
        for side in rec:
            for lg in v.get(side) or []:
                m = S.find(lg.get("match_date"), lg.get("player"), lg.get("opponent"), lg.get("category"),
                           lg.get("line"), lg.get("direction"))
                if m is not None and m.get("hit") is not None:
                    rec[side][0 if m["hit"] else 1] += 1
    n = sum(rec["rule"])
    if n < FINISH_PICKS:
        return False, None
    pr = rec["rule"][0] / n

    def r(k):
        t = sum(rec[k])
        return ("%d-%d (%.0f%%)" % (rec[k][0], rec[k][1], 100.0 * rec[k][0] / t)) if t else "no graded picks"
    lt = sum(rec["top3"]) or 1
    beats = pr > rec["top3"][0] / lt + 0.02
    verdict = ("PASSED -- 77%%+ and better than the live Top 3" if pr >= 0.77 and beats else
               "FAILED -- under the 77%% a Goblin 3-pick needs" if pr < 0.77 else
               "MIXED -- 77%%+ but not better than the live Top 3")
    return True, [("Test", "Goblin 80%% rule for Top 3 / Best Goblin (paper test since %s)" % START),
                  ("80% rule", r("rule")), ("Live Top 3", r("top3")), ("Live Best Goblin", r("bestgoblin")),
                  ("Verdict", verdict.replace("%%", "%")),
                  ("Decision", "switch Top 3 / Best Goblin to 80% / keep the current boards / keep testing")]


if __name__ == "__main__":
    print("\n".join(summary()) or "nothing logged yet")
