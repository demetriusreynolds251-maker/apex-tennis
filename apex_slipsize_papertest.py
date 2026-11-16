#!/usr/bin/env python3
"""
APEX-SLIPSIZE-2026-10-21 (owner: "Keep as a running record go"): paper
test of slip size and prop mix. Nothing is posted and no board or #slips
changes -- once a day, at the first /fetch or 5 PM publish, it builds the slips
below from the props on the board and records them; the daily status grades
them.

Leg pool: Win Probability 70%+, playable (no Goblin/Demon LESS), no Demons,
match not started, one leg per match and player, best Win Probability first.
Variants (each from the same pool, no leg reused inside a variant):
  legs 3 / 4 / 5 / 6
  rule "top" = best Win Probability, any category (how the boards rank today)
  rule "mix" = at most half the legs Total Games (max 1 in a 3-pick)
  slip 1 = the day's best slip, slip 2 = the next one

From the 10/21 backtest (Oct 10-21, 12 days): best slip a day hit all legs
3 legs 50%, 4 legs 42%, 5 legs 33%, 6 legs 33%; a second 6-leg slip 22%.

APEX-SLIPSIZE-SIGNALS-2026-10-21 (his "Go"): every logged leg is also graded
against two outside signals read from props_db at grade time -- bet365's Total
Games line for the match (games of cushion under it for a Total Games leg;
expected match length for every leg) and the player's chance to win the match
(Pinnacle). The daily status shows each signal's leg record.

  python3 apex_slipsize_papertest.py            # print the running record
  python3 apex_slipsize_papertest.py --log-now  # record today now if not yet logged
"""
import json, os, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "data", "logs", "slipsize_papertest.json")
START = "2026-10-21"
MIN_PROB = 0.70
LEGS = (3, 4, 5, 6)
RULES = ("top", "mix")
SLIPS_PER_DAY = 2
# Full-hit payouts from his own screenshots (10/11-10/21), Power except 6 (Flex).
# Estimates only -- PrizePicks prices every Goblin separately.
EST_PAYOUT = {3: 2.2, 4: 2.2, 5: 4.0, 6: 6.0}
FLEX6_ONE_MISS = 0.4
FINISH_DAYS = 21


def _load():
    try:
        with open(LOG) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _names(p):
    raw = str(p.get("opponent") or "").replace(" vs ", " v ").split(" v ") if p.get("player") == "match" \
        else [p.get("player"), p.get("opponent")]
    return frozenset(str(x or "").strip().lower() for x in raw if x)


def _leg(p, prob):
    return {k: p.get(k) for k in ("match_date", "player", "opponent", "category", "line", "direction", "prop_type")} \
        | {"prob": round(prob, 4)}


def pool(picks):
    import apex_edge_tracker as t
    out, seen = [], set()
    for p in picks:
        typ = str(p.get("prop_type") or "standard").lower()
        if typ == "demon" or t._is_unplayable(p) or t._pub_match_started(p):
            continue
        k = t._apex_prop_identity(p)
        if k in seen:
            continue
        seen.add(k)
        pr = t._board_prob(p)
        if pr >= MIN_PROB - 1e-9:
            out.append((pr, p))
    out.sort(key=lambda x: -x[0])
    return out


def _ok(rule, legs, k):
    if rule == "mix":
        return sum(1 for _, p in legs if p.get("category") == "total_games") <= k // 2
    return True


def build(cands, k, rule, nslips=SLIPS_PER_DAY):
    used, slips = set(), []
    for _ in range(nslips):
        s, names = [], frozenset()
        for i, (pr, p) in enumerate(cands):
            nm = _names(p)
            if i in used or nm & names or not _ok(rule, s + [(pr, p)], k):
                continue
            s.append((pr, p)); names |= nm; used.add(i)
            if len(s) == k:
                break
        if len(s) < k:
            break
        slips.append([_leg(p, pr) for pr, p in s])
    return slips


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
        cands = pool(picks)
        data[today] = {"logged_at": datetime.now().isoformat(timespec="seconds"), "source": source,
                       "slips": {"%d|%s" % (k, r): build(cands, k, r) for k in LEGS for r in RULES}}
        tmp = LOG + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1, default=str)
        os.replace(tmp, LOG)
        return True
    except Exception as e:
        print("[SlipSizePaperTest] log skipped (%s): %s" % (type(e).__name__, e))
        return False


def _signals(lg):
    """(bet365 line, games under it for a Total Games leg, player's win chance) from props_db; Nones if unknown."""
    try:
        import sqlite3
        con = sqlite3.connect(os.path.join(HERE, "data", "props_db.sqlite"))
        names = [x.strip() for x in str(lg.get("opponent") or "").replace(" vs ", " v ").split(" v ")] \
            if lg.get("player") == "match" else [lg.get("player")]
        row = None
        for nm in names:
            row = con.execute("select bet365_line, pinn_fav, player_is_fav from projections where category = ? and "
                              "line_last = ? and odds_type = ? and player = ? and match_date between date(?, '-1 day') "
                              "and date(?, '+1 day') order by bet365_line is null limit 1",
                              (lg.get("category"), float(lg.get("line")), str(lg.get("prop_type") or "standard").lower(),
                               nm, lg.get("match_date"), lg.get("match_date"))).fetchone()
            if row:
                break
        con.close()
        if not row:
            row = (None, None, None)
        book, fav, is_fav = row
        try:   # APEX-BET365-THREEPART-2026-10-22: bet365 total via the shared reader (names + match date)
            import apex_bet365_totals as b3
            nm2 = names if len(names) == 2 else [lg.get("player"), lg.get("opponent")]
            book = b3.total(nm2, lg.get("match_date"))[0]
        except Exception:
            pass
        gap = (book - float(lg.get("line"))) if (book is not None and lg.get("category") == "total_games") else None
        win = None
        if fav is not None and is_fav is not None and lg.get("player") != "match":
            win = fav if is_fav == 1 else 1 - fav
        return book, gap, win
    except Exception:
        return None, None, None


def _signal_lines(data, hit):
    B = {}
    seen = set()
    for day, v in sorted(data.items()):
        for slips in (v.get("slips") or {}).values():
            for legs in slips:
                for lg in legs:
                    k = (day, lg.get("player"), lg.get("opponent"), lg.get("category"), lg.get("line"), lg.get("prop_type"))
                    if k in seen:
                        continue
                    seen.add(k)
                    h = hit(lg)
                    if h is None:
                        continue
                    book, gap, win = _signals(lg)
                    tags = []
                    if gap is not None:
                        tags.append(("Total Games cushion", "3+ games under bet365" if gap >= 3 else "under 3 games"))
                    if book is not None:
                        tags.append(("Match length (bet365)", "long 22.5+" if book >= 22.5 else "medium 21-22" if book >= 21 else "short under 21"))
                    if win is not None:
                        tags.append(("Player win chance", "75%+" if win >= 0.75 else "60-75%" if win >= 0.60 else "45-60%" if win >= 0.45 else "under 45%"))
                    for t in tags:
                        r = B.setdefault(t, [0, 0]); r[0 if h else 1] += 1
    out = []
    for group in ("Total Games cushion", "Match length (bet365)", "Player win chance"):
        parts = ["%s %d-%d" % (b, w, l) for (g, b), (w, l) in sorted(B.items()) if g == group]
        if parts:
            out.append("%s: %s" % (group, " · ".join(parts)))
    return out


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
    T = {"_signals": _signal_lines(data, hit)}
    for day, v in sorted(data.items()):
        for key, slips in (v.get("slips") or {}).items():
            k = int(key.split("|")[0])
            for n, legs in enumerate(slips, 1):
                hs = [hit(l) for l in legs]
                if None in hs:
                    continue
                miss = sum(1 for h in hs if not h)
                pay = EST_PAYOUT[k] if miss == 0 else (FLEX6_ONE_MISS if (k == 6 and miss == 1) else 0.0)
                for tag in ("%s|best" % key, "%s|all" % key):
                    if tag.endswith("best") and n > 1:
                        continue
                    s = T.setdefault(tag, {"n": 0, "perfect": 0, "one": 0, "legs_w": 0, "legs_n": 0, "net": 0.0})
                    s["n"] += 1; s["perfect"] += miss == 0; s["one"] += miss == 1
                    s["legs_w"] += k - miss; s["legs_n"] += k; s["net"] += 10 * pay - 10
    return T, len(data)


def summary():
    T, days = _tally()
    if T is None:
        return []
    sig = T.pop("_signals", [])
    if not T:
        return ["Slip size paper test (since %s): %d day(s) logged, nothing graded yet" % (START, days)]
    L = ["Slip size paper test since %s (%d days; not posted; $10 slips, est. payouts 3L 2.2x, 4L 2.2x, 5L 4x, 6L Flex 6x)"
         % (START, days)]
    for k in LEGS:
        for r in RULES:
            for scope, label in (("best", "best slip"), ("all", "2 a day")):
                s = T.get("%d|%s|%s" % (k, r, scope))
                if not s:
                    continue
                L.append("%dL %s %s: perfect %d/%d (%.0f%%) · one miss %d · legs %.0f%% · break-even %s · $%+.0f" % (
                    k, "today's ranking" if r == "top" else "max half TG", label, s["perfect"], s["n"],
                    100.0 * s["perfect"] / s["n"], s["one"], 100.0 * s["legs_w"] / max(s["legs_n"], 1),
                    ("%.1fx" % (s["n"] / float(s["perfect"]))) if s["perfect"] else "n/a", s["net"]))
    if sig:
        L.append("Leg signals (each leg once):")
        L += sig
    return L


def finish_check():
    """(True, card rows) once FINISH_DAYS days are logged; (False, None) before."""
    T, days = _tally()
    if T:
        T.pop("_signals", None)
    if not T or days < FINISH_DAYS:
        return False, None
    rows = [("Test", "Slip size + prop mix paper test (since %s, %d days)" % (START, days))]
    for k in LEGS:
        for r in RULES:
            s = T.get("%d|%s|best" % (k, r))
            if s:
                rows.append(("%d legs, %s" % (k, "today's ranking" if r == "top" else "max half TG"),
                             "best slip perfect %d/%d (%.0f%%), $%+.0f" % (
                                 s["perfect"], s["n"], 100.0 * s["perfect"] / s["n"], s["net"])))
    rows.append(("Decision", "pick a slip size / mix for #slips, or keep testing"))
    return True, rows


if __name__ == "__main__":
    if "--log-now" in sys.argv:
        print("logged" if log_today(source="manual-cli") else "already logged today (or skipped)")
    print("\n".join(summary()) or "nothing logged yet")
