#!/usr/bin/env python3
"""
APEX-RERANKTEST-2026-10-22 (owner, "Go"): background test of re-ranking
board spots #4-10 with the card signals while the Top 3 stay exactly as they
are. Nothing is posted and no board changes. Once a day, at the first /fetch
or 5 PM publish, it records the board order two ways from the props that pass
the board bar (70% Win Probability, Apex 67), one per match/player:

  today   = Win Probability order (the live boards)
  signals = the same Top 3, then #4-10 by Win Probability adjusted by the
            card signals (weights from the 10/21 backtest):
              Total Games MORE: bet365 cushion 3+ +3 pts, under 3 -8 pts;
                ParlayPlay <=1.15x +4, >1.30x -6; favorite 65-80% -5, else +2
              any prop: re-priced +3, still on first price -2

The daily status grades #4-6, #7-10 and the second 3-pick (#4-6 all hit);
after FINISH_DAYS days a TEST FINISHED card posts once.
10/21 backtest (Oct 10-21): #4-6 79.4% -> 85.3%, #7-10 78.9% -> 71.1%,
second 3-pick perfect 7 -> 8 of 11 days.

APEX-NETSIGNAL-2026-10-22: the signals arm now uses the live rule (Win Prob +
NET_SIGNAL_POINTS per net signal point from apex_card_signals.net_score); the
nudges above (signal_adjusted) are kept for reference only.

  python3 apex_rerank_papertest.py            # print the running record
  python3 apex_rerank_papertest.py --log-now  # record today now if not yet logged
"""
import json, os, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "data", "logs", "rerank_papertest.json")
START = "2026-10-22"
DEPTH = 10
FINISH_DAYS = 21


def _load():
    try:
        with open(LOG) as fh:
            return json.load(fh)
    except Exception:
        return {}


def signal_adjusted(p, prob):
    """Win Probability nudged by the card signals. Never raises."""
    try:
        import apex_card_signals as cs
        adj = prob
        names = cs._names(p)
        pair = frozenset(cs._norm(x) for x in names)
        src, fav = cs._cached("picks", cs._pick_rows).get(
            (p.get("match_id"), p.get("category"), str(p.get("line")), str(p.get("prop_type") or "").lower()), ("", ""))
        if p.get("category") == "total_games" and str(p.get("direction") or "").upper() == "MORE":
            line = float(p.get("line"))
            bk = cs.book_total(p)   # APEX-BET365-THREEPART-2026-10-22: names + match date
            if bk:
                adj += 0.03 if bk - line >= 3 else -0.08
            m = cs._cached("pp", cs._pp_ladder).get((pair, line))
            if m:
                adj += 0.04 if m <= 1.15 else (-0.06 if m > 1.30 else 0.0)
            try:
                f = float(fav)
                adj += -0.05 if 0.65 <= f < 0.80 else 0.02
            except (TypeError, ValueError):
                pass
        if src.startswith(("refresh", "reprice")):
            adj += 0.03
        elif src:
            adj -= 0.02
        return adj
    except Exception:
        return prob


def _leg(p, prob):
    return {k: p.get(k) for k in ("match_date", "player", "opponent", "category", "line", "direction", "prop_type")} \
        | {"prob": round(prob, 4)}


def build(picks):
    import apex_edge_tracker as t
    rec = t.records()
    cand, seen = [], set()
    for p in picks:
        if t._is_unplayable(p) or t._pub_match_started(p):
            continue
        k = t._apex_prop_identity(p)
        if k in seen:
            continue
        seen.add(k)
        st, _why, _sc = t._pre_board_reason(p, rec)
        if st != "eligible":
            continue
        pr = t._board_prob(p)
        cand.append((pr, t.net_adjusted(p, pr), p))   # APEX-NETSIGNAL-2026-10-22: signals arm = net score

    def pick(order):
        out, used = [], set()
        for c in order:
            nm = t._prop_names(c[2])
            if nm & used:
                continue
            used |= nm
            out.append(c)
            if len(out) >= DEPTH:
                break
        return out
    today = pick(sorted(cand, key=lambda c: -c[0]))
    top3 = today[:3]
    used = set()
    for c in top3:
        used |= t._prop_names(c[2])
    rest = []
    for c in sorted([c for c in cand if c not in top3], key=lambda c: -c[1]):
        nm = t._prop_names(c[2])
        if nm & used:
            continue
        used |= nm
        rest.append(c)
        if len(top3) + len(rest) >= DEPTH:
            break
    return ([_leg(c[2], c[0]) for c in today],
            [_leg(c[2], c[0]) | {"adj": round(c[1], 4)} for c in top3 + rest])


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
        a, b = build(picks)
        data[today] = {"logged_at": datetime.now().isoformat(timespec="seconds"), "source": source,
                       "today": a, "signals": b,
                       # APEX-NETSIGNAL-2026-10-22: from 10/22 on the signals arm is Win Prob + 5 pts per net signal point
                       "signals_method": "net score x %g (apex_card_signals.net_score)" % t.NET_SIGNAL_POINTS}
        tmp = LOG + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1, default=str)
        os.replace(tmp, LOG)
        return True
    except Exception as e:
        print("[RerankPaperTest] log skipped (%s): %s" % (type(e).__name__, e))
        return False


def _tally():
    data = _load()
    if not data:
        return None, 0
    import apex_engine_validation as ev
    _t, _rep, S, _st = ev.load()

    def hit(lg):
        m = S.find(lg.get("match_date"), lg.get("player"), lg.get("opponent"), lg.get("category"),
                   lg.get("line"), lg.get("direction"))
        return None if m is None else m.get("hit")
    T = {k: {"4-6": [0, 0], "7-10": [0, 0], "slip2": [0, 0]} for k in ("today", "signals")}
    for day, v in sorted(data.items()):
        for k in ("today", "signals"):
            hs = [hit(lg) for lg in (v.get(k) or [])]
            for i, h in enumerate(hs, 1):
                if h is None or i <= 3:
                    continue
                g = "4-6" if i <= 6 else "7-10"
                T[k][g][0 if h else 1] += 1
            s2 = hs[3:6]
            if len(s2) == 3 and None not in s2:
                T[k]["slip2"][0 if all(s2) else 1] += 1
    return T, len(data)


def summary():
    T, days = _tally()
    if T is None:
        return []
    if not any(sum(v) for k in T for v in T[k].values()):
        return ["Re-rank test (since %s): %d day(s) logged, nothing graded yet" % (START, days)]

    def r(x):
        n = sum(x)
        return ("%d-%d (%.0f%%)" % (x[0], x[1], 100.0 * x[0] / n)) if n else "--"
    L = ["Re-rank test since %s (%d days; Top 3 unchanged, #4-10 by signals; not posted)" % (START, days)]
    for k, label in (("today", "Today's ranking"), ("signals", "Signals from #4")):
        L.append("%s: #4-6 %s · #7-10 %s · 2nd 3-pick perfect %d of %d days" % (
            label, r(T[k]["4-6"]), r(T[k]["7-10"]), T[k]["slip2"][0], sum(T[k]["slip2"])))
    return L


def finish_check():
    T, days = _tally()
    if not T or days < FINISH_DAYS:
        return False, None
    a, b = T["today"]["4-6"], T["signals"]["4-6"]
    ra = a[0] / float(sum(a)) if sum(a) else 0
    rb = b[0] / float(sum(b)) if sum(b) else 0
    verdict = ("PASSED -- signals made #4-6 better by %.0f pts" % (100 * (rb - ra)) if rb - ra >= 0.03 else
               "FAILED -- no real gain for #4-6")
    rows = [("Test", "Re-rank #4-10 by card signals, Top 3 unchanged (since %s, %d days)" % (START, days))]
    rows += [(label, "#4-6 %d-%d · #7-10 %d-%d · 2nd 3-pick perfect %d of %d" % (
        T[k]["4-6"][0], T[k]["4-6"][1], T[k]["7-10"][0], T[k]["7-10"][1], T[k]["slip2"][0], sum(T[k]["slip2"])))
             for k, label in (("today", "Today's ranking"), ("signals", "Signals from #4"))]
    rows += [("Verdict", verdict), ("Decision", "switch #4-10 on the boards to signal order / keep today's / keep testing")]
    return True, rows


if __name__ == "__main__":
    if "--log-now" in sys.argv:
        print("logged" if log_today(source="manual-cli") else "already logged today (or skipped)")
    print("\n".join(summary()) or "nothing logged yet")
