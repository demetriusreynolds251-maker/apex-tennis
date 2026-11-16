#!/usr/bin/env python3
"""
APEX-TOP3NO175-2026-10-22 (owner, "Track"): background test of the
Top 3 without over-17.5 Total Games picks. Nothing is posted and no board
changes. Why: the real Top 3's over 17.5 Total Games picks went 5-8 from
10/17 to 10/22 (every loss a straight-sets blowout), while the rest of the
Top 3 hit 9 in 10. A one-a-day replay could not show it, because the real
Top 3 is re-ordered 40-70 times a day -- so this test reads the boards
exactly as they were posted.

Read-only: it rebuilds everything from apex_board_status_log_v1.csv, which
logs every Top 3 / Top 10 the boards post (the Top 3 is always the first 3
of the Top 10). For every posted board it forms three Top 3s:

  real      = the Top 3 as posted
  no175     = Top 10 order, an over-17.5 Total Games pick swapped for the
              same match's over 18.5 when that one passed the board bar
              (70% / Apex 67) on the same board, else skipped
  nomatch   = Top 10 order, skipping every Total Games over on a match
              that had an over-17.5 pick passing the bar on that board

Record per arm = every different pick that was ever in that day's Top 3,
graded once. Slip per arm = the 5 PM Top 3 (or the first board of the day
if there was no 5 PM run), $[amount] 3-Goblin Power at 2.2x.
The test proper starts START; 10/17 to the day before is shown as a look back.
TEST FINISHED after FINISH_DAYS days or FINISH_PICKS graded picks in each arm.

  python3 apex_top3_no175_papertest.py          # print the running record
  python3 apex_top3_no175_papertest.py --days   # day-by-day detail
"""
import csv, os, sys
from collections import OrderedDict, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_LOG = os.path.join(HERE, "apex_board_status_log_v1.csv")
LOOKBACK = "2026-10-17"
START = "2026-10-23"
FINISH_DAYS = 7
FINISH_PICKS = 30
STAKE, POWER3 = 20, 2.2
RULE_ARM_ON = True   # on 10/22: owner "Track" + standing order that new rules run as background paper tests
ELO_CLOSE, ROOM_MIN = 100, 4   # line175_report.md: close in rating (Elo gap <= 100) OR bet365 4+ games of room
# APEX-175RANK-2026-10-22 (owner, "Go and still run the test"): from 10/22 the real Top 3
# has the 17.5 check push-down (over-17.5 picks lose 10 points on a skip, 5 on not enough data).
# The "nopenalty" arm is the Top 3 as it would be without it, logged at every publish by
# apex_edge_tracker._write_nopenalty_top3 (apex_top3_nopenalty_log.csv). Before that log has a
# board for a day, the arm equals the real Top 3 (the push-down did not exist yet).
# APEX-175BACKUPS-2026-10-22 ("Go back up"): from 10/22 a ❌ skip over 17.5 goes to BACKUPS instead
# of ranking; the real arm reads the boards as posted (rule included) and the "nopenalty" arm is
# logged with BOTH the push-down and the skip-to-BACKUPS rule off (edge tracker _LINE175_RULES_OFF).
# APEX-DFFANTASY-2026-10-22: the "nopenalty" arm is also built with the Fantasy LESS / DF-after-a-match
# BACKUPS holds off (edge tracker df_fs_hold_reason returns None while _LINE175_RULES_OFF is set).
NOPENALTY_ARM_ON = True
NOPENALTY_LOG = os.path.join(HERE, "apex_top3_nopenalty_log.csv")
ARMS = (("real", "Top 3 as posted"), ("no175", "No over 17.5"), ("nomatch", "No over on a 17.5 match")) + \
    ((("rule175", "17.5 only if close match or 4+ room"),) if RULE_ARM_ON else ()) + \
    ((("nopenalty", "Top 3 without the 17.5 push-down, skip-to-BACKUPS or Fantasy LESS / DF holds"),) if NOPENALTY_ARM_ON else ())


def _pair(r):
    if r["player"] == "match":
        return frozenset(x.strip() for x in r["opponent"].split(" v "))
    return frozenset([r["player"], r["opponent"]])


def _is_tg_over(r):
    return r["category"] == "total_games" and r["direction"].upper() == "MORE"


def _key(r):
    return (r["match_date"], r["player"], r["opponent"], r["category"], r["line"], r["direction"].upper())


def _boards():
    """{match_date: OrderedDict(captured_at -> {"source", "top10": [rows], "ok": [rows]})}"""
    days = defaultdict(OrderedDict)
    with open(STATUS_LOG, newline="") as fh:
        for r in csv.DictReader(fh):
            if r["match_date"] < LOOKBACK:
                continue
            b = r["board"]
            if b == "top10" and r["status"] == "recommended":
                cap = days[r["match_date"]].setdefault(r["captured_at"], {"source": r["source"], "top10": [], "ok": []})
                cap["top10"].append(r)
            elif b == "all" and r["status"] in ("eligible", "recommended") and _is_tg_over(r):
                cap = days[r["match_date"]].setdefault(r["captured_at"], {"source": r["source"], "top10": [], "ok": []})
                cap["ok"].append(r)
    for md in days:
        for c in list(days[md]):
            if not days[md][c]["top10"]:
                del days[md][c]
            else:
                days[md][c]["top10"].sort(key=lambda r: float(r["position"] or 99))
    return days


_ctx = {}


def _norm(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().replace("-", " ").strip()


CTX_TTL = 600   # the 🎯 card line runs inside the long-lived bot: reload Elo / bet365 lines every 10 min


def _rule_ctx():
    import sqlite3, time
    if _ctx.get("at") and time.time() - _ctx["at"] < CTX_TTL:
        return _ctx
    con = sqlite3.connect("file:%s?mode=ro" % os.path.join(HERE, "data", "warehouse", "tennis_warehouse.sqlite"), uri=True)
    elo = {_norm(p): e for p, e in con.execute("select player, overall_rating from player_elo")}
    con.close()
    try:   # APEX-PLAYERDATA-2026-10-31: name variants share one rating
        import apex_player_history as _ph
        elo = _ph.elo_variants(elo, _norm)
    except Exception:
        pass
    # APEX-BET365-THREEPART-2026-10-22: bet365 totals now come from apex_bet365_totals (names + date)
    _ctx.update(elo=elo, at=time.time())
    return _ctx


def bet365_total(names, match_date):
    """APEX-175BET365-2026-10-22: the one bet365 expected total shared by the 📏 room line, the 🎯
    17.5 check and this test's rule arm (via apex_bet365_totals).
    Returns (total or None, "props_db" / "log" / None). Never raises."""
    # APEX-BET365-THREEPART-2026-10-22 (owner, "Go bet365"): one shared reader for every
    # bet365 total -- log first for upcoming matches, props database first for past ones, matched
    # on both names (last name + first initial) AND the match date.
    try:
        import apex_bet365_totals as _b3
        b, _op, src = _b3.total(names, match_date)
        if b is not None:
            _age = _b3.log_age_hours(names, match_date)   # APEX-BOOKCHECK-2026-11-01
            if _age is None or _age <= _b3.BOOK_MAX_AGE_H:
                return float(b), src
            import apex_sgo_backup as _sb0
            if _sb0.total(names, match_date)[0] is None:
                return float(b), src
    except Exception:
        pass
    try:   # APEX-SGOBACKUP-2026-10-31: bet365 has none; never replaces a bet365 number
        import apex_sgo_backup as _sb
        b, _books = _sb.total(names, match_date)
        if b is not None:
            return float(b), "sgo"
    except Exception:
        pass
    try:   # APEX-BOOKDATE-2026-11-01: saved total from before a date move, last choice
        b, _op, src = _b3.total_near(names, match_date)
        if b is not None:
            return float(b), src
    except Exception:
        pass
    return None, None


def rule175(names, match_date, line, book=None):
    """APEX-LINE175CHECK-2026-10-22: the 17.5 rule's verdict, shared by this test and the 🎯 card line.
    Returns (verdict, elo_gap, room): verdict "close" (Elo gap <= ELO_CLOSE), "room" (bet365 total
    ROOM_MIN+ games over the line), "skip" (both known, neither true) or "nodata" (can't tell)."""
    c = _rule_ctx()
    names = [_norm(x) for x in names]
    e = [c["elo"].get(x) for x in names]
    gap = abs(e[0] - e[1]) if len(e) == 2 and None not in e else None
    b = bet365_total(names, match_date)[0]   # APEX-175BET365-2026-10-22: same number as the 📏 line
    if b is None and book is not None:   # APEX-PPMIRROR-B-2026-10-23: ParlayPlay's own stored bet365 total (only ParlayPlay passes it)
        b = book
    room = float(b) - float(line) if b is not None else None
    if gap is not None and gap <= ELO_CLOSE:
        return "close", gap, room
    if room is not None and room >= ROOM_MIN:
        return "room", gap, room
    if gap is None or room is None:
        return "nodata", gap, room
    return "skip", gap, room


def _rule_ok(r):
    """APEX-TOP3NO175-2026-10-22 rule arm: an over 17.5 stays only when the two players are close
    in rating (Elo gap <= ELO_CLOSE) or bet365's total is ROOM_MIN+ games over the line."""
    try:
        return rule175(_pair(r), r["match_date"], r["line"])[0] in ("close", "room")
    except Exception:
        return True


def _arms(cap):
    t10, ok = cap["top10"], cap["ok"]
    m175 = {_pair(r) for r in ok if r["line"] == "17.5"}
    real = t10[:3]
    no175, nomatch = [], []
    for r in t10:
        if _is_tg_over(r) and r["line"] == "17.5":
            sub = [x for x in ok if _pair(x) == _pair(r) and x["line"] == "18.5"]
            if sub and len(no175) < 3:
                no175.append(sub[0])
        elif len(no175) < 3:
            no175.append(r)
        if len(nomatch) < 3 and not (_is_tg_over(r) and _pair(r) in m175):
            nomatch.append(r)
    out = {"real": real, "no175": no175[:3], "nomatch": nomatch[:3]}
    if RULE_ARM_ON:
        out["rule175"] = [r for r in t10 if not (_is_tg_over(r) and r["line"] == "17.5" and not _rule_ok(r))][:3]
    return out


def _nopenalty_boards():
    """{match_date: OrderedDict(captured_at -> {"source", "top3": [rows]})} from NOPENALTY_LOG."""
    days = defaultdict(OrderedDict)
    if not NOPENALTY_ARM_ON or not os.path.exists(NOPENALTY_LOG):
        return days
    with open(NOPENALTY_LOG, newline="") as fh:
        for r in csv.DictReader(fh):
            if r["match_date"] < LOOKBACK:
                continue
            r["line"] = str(r["line"])
            days[r["match_date"]].setdefault(r["captured_at"], {"source": r["source"], "top3": []})["top3"].append(r)
    for md in days:
        for c in days[md]:
            days[md][c]["top3"].sort(key=lambda r: float(r["position"] or 99))
    return days


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


def tally(since=START, until=None):
    """{arm: {"w","l","open","slip_w","slip_l","money","days"}} plus per-day detail."""
    hit = _grader()
    T = {a: {"w": 0, "l": 0, "open": 0, "slip_w": 0, "slip_l": 0, "money": 0.0, "days": 0, "losers": []} for a, _ in ARMS}
    detail = []
    NP = _nopenalty_boards()
    for md, caps in sorted(_boards().items()):
        if md < since or (until and md > until):
            continue
        picks = {a: OrderedDict() for a, _ in ARMS}
        np_caps = NP.get(md) or OrderedDict()
        np_first = next(iter(np_caps), None)   # boards before the first no-penalty log had no push-down
        for c, cap in caps.items():
            for a, legs in _arms(cap).items():
                for r in legs:
                    picks[a].setdefault(_key(r), r)
            if NOPENALTY_ARM_ON and (np_first is None or c < np_first):
                for r in cap["top10"][:3]:
                    picks["nopenalty"].setdefault(_key(r), r)
        if NOPENALTY_ARM_ON:
            for c, cap in np_caps.items():
                for r in cap["top3"]:
                    picks["nopenalty"].setdefault(_key(r), r)
        slip_cap = next((cap for c, cap in caps.items() if cap["source"] == "auto"), next(iter(caps.values())))
        slip = _arms(slip_cap)
        if NOPENALTY_ARM_ON:
            np_slip = next((cap for c, cap in np_caps.items() if cap["source"] == "auto"), None)
            slip["nopenalty"] = np_slip["top3"] if np_slip else (slip["real"] if not np_caps else
                                                                 next(iter(np_caps.values()))["top3"])
        row = {"date": md}
        for a, _ in ARMS:
            T[a]["days"] += 1
            for k, r in picks[a].items():
                h = hit(r)
                if h is None:
                    T[a]["open"] += 1
                elif h:
                    T[a]["w"] += 1
                else:
                    T[a]["l"] += 1
                    T[a]["losers"].append("%s %s %s %s" % (md[5:], r["opponent"] if r["player"] == "match" else r["player"],
                                                          r["category"].replace("_", " "), r["line"]))
            hs = [hit(r) for r in slip.get(a, [])]
            if len(hs) == 3 and None not in hs:
                if all(hs):
                    T[a]["slip_w"] += 1
                    T[a]["money"] += STAKE * (POWER3 - 1)
                else:
                    T[a]["slip_l"] += 1
                    T[a]["money"] -= STAKE
            row[a] = ["%s %s %s" % (r["opponent"] if r["player"] == "match" else r["player"], r["line"],
                                    {True: "W", False: "L", None: "?"}[hit(r)]) for r in slip.get(a, [])]
        detail.append(row)
    return T, detail


def _line(label, x):
    n = x["w"] + x["l"]
    return "%s: picks %d-%d%s%s · 5 PM 3-pick %d of %d · $%+.0f" % (
        label, x["w"], x["l"], (" (%.0f%%)" % (100.0 * x["w"] / n)) if n else "",
        (" · %d open" % x["open"]) if x["open"] else "", x["slip_w"], x["slip_w"] + x["slip_l"], x["money"])


def summary():
    """Daily status lines (apex_alerts). Never raises."""
    try:
        L = []
        T, _ = tally(START)
        if T["real"]["days"]:
            L.append("Top 3 without over 17.5 since %s (%d days; not posted, boards unchanged)" % (START, T["real"]["days"]))
            L += [_line(lbl, T[a]) for a, lbl in ARMS]
            if NOPENALTY_ARM_ON:
                L.append(_pushdown_line(T))
        B, _ = tally(LOOKBACK, _prev(START))
        L.append("Look back %s to %s:" % (LOOKBACK[5:], _prev(START)[5:]))
        L += ["  " + _line(lbl, B[a]) for a, lbl in ARMS]
        return L
    except Exception as e:
        return ["Top 3 without 17.5 test: unavailable (%s)" % type(e).__name__]


def _pushdown_line(T):
    """APEX-175RANK-2026-10-22: plain words, e.g. "Top 3 with the 17.5 push-down: 21-6. Without it: 19-8." """
    r, n = T["real"], T["nopenalty"]
    return "Top 3 with the 17.5 push-down: %d-%d ($%+.0f). Without it: %d-%d ($%+.0f)." % (
        r["w"], r["l"], r["money"], n["w"], n["l"], n["money"])


def _prev(day):
    from datetime import date, timedelta
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def finish_check():
    T, _ = tally(START)
    graded = min(T[a]["w"] + T[a]["l"] for a, _ in ARMS)
    if T["real"]["days"] < FINISH_DAYS and graded < FINISH_PICKS:
        return False, None
    rate = {a: (T[a]["w"] / float(T[a]["w"] + T[a]["l"]) if T[a]["w"] + T[a]["l"] else 0) for a, _ in ARMS}
    best = max([a for a, _ in ARMS if a not in ("real", "nopenalty")], key=lambda a: (T[a]["money"], rate[a]))
    gain = T[best]["money"] - T["real"]["money"]
    verdict = ("PASSED -- %s made $%+.0f more than the real Top 3 (picks %.0f%% vs %.0f%%)"
               % (dict(ARMS)[best], gain, 100 * rate[best], 100 * rate["real"]) if gain > 0 and rate[best] >= rate["real"]
               else "FAILED -- taking over 17.5 out of the Top 3 did not do better")
    rows = [("Test", "Top 3 without over 17.5 Total Games (since %s, %d days)" % (START, T["real"]["days"]))]
    rows += [(lbl, _line("", T[a]).lstrip(": ")) for a, lbl in ARMS]
    rows += [("Verdict", verdict), ("Decision", "keep over 17.5 out of the Top 3 / keep the Top 3 as is / keep testing")]
    if NOPENALTY_ARM_ON:   # APEX-175RANK-2026-10-22
        better = T["real"]["money"] - T["nopenalty"]["money"]
        rows += [("17.5 push-down", _pushdown_line(T) + (" The push-down helped." if better > 0 else
                  (" The push-down hurt." if better < 0 else " No difference in money."))),
                 ("Push-down decision", "keep the 17.5 push-down / turn it off (both penalties to 0)")]
    return True, rows


if __name__ == "__main__":
    print("\n".join(summary()))
    if "--days" in sys.argv:
        _T, D = tally(LOOKBACK)
        for row in D:
            print(row["date"][5:], "| real:", ", ".join(row["real"]), "| no 17.5:", ", ".join(row["no175"]),
                  "| no over on match:", ", ".join(row["nomatch"]),
                  "| no push-down:", ", ".join(row.get("nopenalty", [])))
