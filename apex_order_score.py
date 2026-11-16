#!/usr/bin/env python3
"""
APEX-ORDERSCORE-2026-10-25 (owner: "... be sure that we aren't mixing up what we back test as
better for what would be the lessor"):

ORDER SCORECARD -- is the "better" rule really better? DATA ONLY: it never changes a board.

From data/board_history.sqlite, Top 3 and Elite 6 only (his betting boards), one board a day: the
official board = the last publish before that board's first match starts (same as the slips).
Every time the rules put a lower-Win-Prob pick ABOVE a higher-Win-Prob one, the pair is recorded:
the pick the rules moved up vs the higher-% pick they passed, with the reason:
  signals         the 🧮 signal points ranked it higher
  blowout double  the passed pick's 🏟 blowout risk counting double is what put it below
  17.5 push-down  the passed pick lost 17.5-check points and that is what put it below
  held            the passed pick was held to BACKUPS by a rule (no book, 17.5 skip, FS LESS, DF, room)
  best line       same match: the rules kept a different line of that match
  top 75          the 75% top-3 bar put a 75%+ pick above a pick the rank liked more (this one moves
                  a HIGHER-% pick up, so "passed" is the pick the rank alone would have played)
A pair counts as "took a spot" when it changes who is on the slip, else "order only" (both on it).
Both picks are graded when they settle; "took a spot" pairs also get the $ difference on the slip
($[amount] Top 3 Power / Elite 6 Flex payouts from apex_slips_v2).

Oct 10-24 boards are REPLAYED with today's rules on the stored rows (the blowout double and the
75% bar only went live 10/25; before that this shows what they WOULD have done). From Oct 25 on
the posted order itself is used.

Posts (scheduler, apex_order_score.run, every 15 min, posts only when due):
  9:00 AM daily, #system   ✅ Order check line (apex_order_guard) + 📊 Order score yesterday line
  Mon 9:10 AM, #system     weekly card, running score per reason since Oct 25
  any time, #alerts        ⚠️ ORDER SCORE: the <reason> rule is losing to plain Win Prob (once per
                           reason, after 20+ graded pairs since Oct 25). Nothing changes on its own.

  python3 apex_order_score.py              # backfill report by date range (prints)
  python3 apex_order_score.py --line       # yesterday's daily lines (prints)
  python3 apex_order_score.py --weekly     # the weekly card (prints)
  python3 apex_order_score.py --run        # what the scheduler runs
Files: data/order_guard/order_pairs.csv (every pair), data/order_guard/score_state.json.
Undo: ORDER_SCORE_ON = False.
"""
import csv, json, os, sqlite3, sys
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "board_history.sqlite")
DIR = os.path.join(HERE, "data", "order_guard")
PAIRS_CSV = os.path.join(DIR, "order_pairs.csv")
STATE = os.path.join(DIR, "score_state.json")
CT = ZoneInfo("America/Chicago")
ORDER_SCORE_ON = True
LIVE_FROM = "2026-10-25"        # running totals start here (posted order); earlier days are replays
FIRST_DAY = "2026-10-10"
BOARDS = ("top3", "elite6")
SIZES = {"top3": 3, "elite6": 6}
NAMES = {"top3": "Top 3", "elite6": "Elite 6"}
TOP_BAR = 0.75
TOP_N = 3
SIGNAL_PTS = 0.05               # one net signal point = 5 rank points
ALERT_MIN_PAIRS = 20
DAILY_AT = (9, 0)
WEEKLY_AT = (9, 10)
REASONS = ("signals", "blowout double", "17.5 push-down", "held", "best line", "top 75")
RANGES = (("Oct 10-18", "2026-10-10", "2026-10-18"), ("Oct 19-24", "2026-10-19", "2026-10-24"),
          ("Oct 25", "2026-10-25", "2026-10-25"))
PAIR_FIELDS = ["board_day", "board", "kind", "reason", "source", "up_spot", "up_pick", "up_win_prob", "up_rank",
               "up_result", "passed_spot", "passed_pick", "passed_win_prob", "passed_rank", "passed_result",
               "passed_why", "up_key", "passed_key"]


def _payouts():
    try:
        import apex_slips_v2 as v2
        return v2.STAKE, v2.POWER3, v2.POWER3_FALLBACK, v2.FLEX6
    except Exception:
        return 20.0, {"GGG": 2.2, "GGS": 3.25, "GSS": 3.75, "SSS": 6.0}, 2.2, {6: 3.0, 5: 1.0, 4: 0.4}


# ------------------------------------------------------------------ reading the history
def _utc(local_iso):
    try:
        return datetime.fromisoformat(local_iso).replace(tzinfo=CT).astimezone(ZoneInfo("UTC")).isoformat()[:19]
    except Exception:
        return local_iso


def _names(r):
    if r.get("player") == "match":
        raw = str(r.get("opponent") or "").replace(" vs ", " v ").split(" v ")
    else:
        raw = [r.get("player"), r.get("opponent")]
    return {str(x).strip().casefold() for x in raw if x}


def _label(r):
    if r.get("player") == "match":
        who = " v ".join((str(x).split() or [""])[-1] for x in str(r.get("opponent") or "").split(" v "))
    else:
        who = (str(r.get("player") or "").split() or [""])[-1]
    cat = {"total_games": "TG", "fantasy_score": "Fantasy", "dfs": "DF", "games_won": "GW", "aces": "Aces",
           "breaks": "Breaks"}.get(r.get("category"), r.get("category"))
    ln = ("%g" % r["line"]) if r.get("line") is not None else ""
    return "%s %s %s%s" % (who, cat, ln, " LESS" if str(r.get("direction") or "").upper() == "LESS" else "")


def official_boards(since=FIRST_DAY):
    """{(board_day, board): (origin, rows)} -- the last publish before the board's first match."""
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    pubs = c.execute("SELECT publish_id, published_at, board_day, origin FROM publishes WHERE platform = 'prizepicks' "
                     "AND origin != 'backfill-membership-log' AND board_day >= ? ORDER BY published_at, publish_id",
                     (since,)).fetchall()
    rows = {}
    q = "SELECT * FROM history WHERE board IN ('top3', 'elite6') AND spot_kind IN ('main', 'backup', 'held') " \
        "AND publish_id >= ?"
    first = pubs[0]["publish_id"] if pubs else 0
    for r in c.execute(q, (first,)):
        rows.setdefault(r["publish_id"], []).append(dict(r))
    res = {}
    for r in c.execute("SELECT prop_key, match_start, result FROM history WHERE result IS NOT NULL"):
        res[(r[0], r[1])] = r[2]
    c.close()
    out = {}
    for p in pubs:
        at = _utc(p["published_at"])
        by = {}
        for r in rows.get(p["publish_id"], []):
            by.setdefault(r["board"], []).append(r)
        for b, rs in by.items():
            main = [r for r in rs if r["spot_kind"] == "main"]
            if not main:
                continue
            starts = [r["match_start"] for r in main if r.get("match_start")]
            if starts and at >= min(starts)[:19]:
                continue
            out[(p["board_day"], b)] = (p["origin"], rs)
    return out, res


# ------------------------------------------------------------------ the three orders
def _floor_held(r):
    """Held rows that are under a floor (not a rule hold): never on any arm."""
    why = str(r.get("reason") or "")
    return "under the 67 bar" in why or (r.get("win_prob") or 0) < 70 - 1e-9


def replay_hold(r):
    """Today's hold rules on a stored row (for the Oct 10-24 replays): no book total, 17.5 skip,
    under 3 games of bet365 room on Total Games MORE, Fantasy LESS, DF LESS after a match."""
    cat, d = r.get("category"), str(r.get("direction") or "").upper()
    if cat == "total_games" and r.get("bet365_total") is None:
        return "no book total"
    if r.get("line175") == "skip":
        return "17.5 check says skip"
    if cat == "total_games" and d == "MORE" and r.get("room") is not None and r["room"] < 3 - 1e-9 \
            and str(r.get("prop_type") or "") in ("goblin", "standard", "demon"):
        return "only %g games of room under bet365" % r["room"]
    if cat == "fantasy_score" and d == "LESS":
        return "Fantasy LESS"
    if cat == "dfs" and d == "LESS" and (r.get("sig_played") or 0) > 0:
        return "DF LESS after a match the day before"
    return None


def _pick(cands, n, key, bar=False):
    groups = []
    for r in sorted(cands, key=key):
        g = next((g for g in groups if g[0] & _names(r)), None)
        if g:
            g[0] |= _names(r)
        else:
            groups.append([set(_names(r)), r])
    reps = [g[1] for g in groups]
    out = []
    if bar:
        out = [r for r in reps if (r["win_prob"] or 0) >= 100 * TOP_BAR - 1e-9][:min(TOP_N, n)]
        if len(out) < min(TOP_N, n):
            out += sorted([r for r in reps if all(r is not o for o in out)],
                          key=lambda r: -(r["win_prob"] or 0))[:min(TOP_N, n) - len(out)]
    out += [r for r in reps if all(r is not o for o in out)]
    return out[:n]


def _rk(r):
    return -(r["rank_score"] if r["rank_score"] is not None else (r["win_prob"] or 0)), -(r["apex_score"] or 0)


def arms(origin, rows, board):
    """(rule order, plain Win Prob order, rank-without-the-75-bar order) as row lists."""
    main = sorted([r for r in rows if r["spot_kind"] == "main"], key=lambda r: r["spot_no"] or 0)
    bk = [r for r in rows if r["spot_kind"] == "backup"]
    held = [r for r in rows if r["spot_kind"] == "held" and not _floor_held(r)]
    n = len(main)
    free = main + bk
    if origin != "live":   # replay: today's holds take stored picks off the numbered spots
        for r in free:
            why = replay_hold(r)
            if why:
                r["spot_kind"], r["reason"] = "held", "held back (today's rule): " + why
        held += [r for r in free if r["spot_kind"] == "held"]
        free = [r for r in free if r["spot_kind"] != "held"]
    rule = main if origin == "live" else _pick(free, n, _rk, bar=True)
    wp = _pick(free + held, n, lambda r: (-(r["win_prob"] or 0), -(r["apex_score"] or 0)))
    nobar = _pick(free, n, _rk)
    return rule, wp, nobar


def _why_rank(a, b):
    """Why the rules ranked a above b (a has the lower Win Prob)."""
    ra, rb = a["rank_score"] or 0, b["rank_score"] or 0
    if (b.get("sig_blowout") or 0) <= -2 and rb + 100 * SIGNAL_PTS >= ra - 1e-9:
        return "blowout double"
    pts = b.get("line175_points") or 0
    if pts < 0 and rb - pts >= ra - 1e-9:
        return "17.5 push-down"
    return "signals"


def pairs_for(day, board, origin, rows, res):
    rule, wp, nobar = arms(origin, rows, board)
    k = lambda r: r["prop_key"]
    in_rule, in_wp, in_nobar = {k(r) for r in rule}, {k(r) for r in wp}, {k(r) for r in nobar}
    spot = {k(r): i for i, r in enumerate(rule, 1)}
    out = []

    def add(kind, reason, a, b, a_spot, b_spot):
        out.append({"board_day": day, "board": board, "kind": kind, "reason": reason,
                    "source": "posted" if origin == "live" else "replay (today's rules)",
                    "up_spot": a_spot, "up_pick": _label(a), "up_win_prob": a["win_prob"], "up_rank": a["rank_score"],
                    "up_result": res.get((a["prop_key"], a["match_start"]), a.get("result")) or "",
                    "passed_spot": b_spot, "passed_pick": _label(b), "passed_win_prob": b["win_prob"],
                    "passed_rank": b["rank_score"],
                    "passed_result": res.get((b["prop_key"], b["match_start"]), b.get("result")) or "",
                    "passed_why": b.get("reason") or "", "up_key": a["prop_key"], "passed_key": b["prop_key"]})
    # (a) who is on the slip: rules vs plain Win Prob
    ins = [r for r in rule if k(r) not in in_wp]
    outs = [r for r in wp if k(r) not in in_rule]
    for b in list(outs):
        a = next((a for a in ins if _names(a) & _names(b)), None)
        if a:
            add("took a spot", "held" if b["spot_kind"] == "held" else "best line", a, b, "#%d" % spot[k(a)], "not on")
            ins.remove(a); outs.remove(b)
    ins.sort(key=lambda r: -spot[k(r)])   # the lowest rule pick took the best passed pick's spot
    for a, b in zip(ins, outs):
        if b["spot_kind"] == "held":
            reason = "held"
        elif (b["win_prob"] or 0) > (a["win_prob"] or 0):
            reason = _why_rank(a, b)
        else:
            continue
        add("took a spot", reason, a, b, "#%d" % spot[k(a)], "not on")
    # (b) order inside the board (both on the slip)
    both = [r for r in rule if k(r) in in_wp]
    for i, a in enumerate(both):
        for b in both[i + 1:]:
            if (a["win_prob"] or 0) < (b["win_prob"] or 0) - 1e-9:
                add("order only", _why_rank(a, b), a, b, "#%d" % spot[k(a)], "#%d" % spot[k(b)])
    # (c) the 75% top-3 bar: it moved a 75%+ pick above one the rank alone liked more
    for a in rule[:TOP_N]:
        if (a["win_prob"] or 0) < 100 * TOP_BAR - 1e-9:
            continue
        for b in nobar:
            if k(b) == k(a) or _names(a) & _names(b) or (b["win_prob"] or 0) >= 100 * TOP_BAR - 1e-9:
                continue
            if (b["rank_score"] or 0) <= (a["rank_score"] or 0):
                continue
            b_on = k(b) in in_rule
            if b_on and spot[k(b)] < spot[k(a)]:
                continue
            add("order only" if b_on else "took a spot", "top 75", a, b, "#%d" % spot[k(a)],
                ("#%d" % spot[k(b)]) if b_on else "not on")
    return out, rule


# ------------------------------------------------------------------ $ on the slip
def _slip_profit(board, legs, res_of):
    stake, power3, fb, flex6 = _payouts()
    rs = [res_of(l) for l in legs]
    if any(r not in ("hit", "miss") for r in rs):
        return None
    if board == "top3":
        if len(legs) != 3:
            return None
        code = "".join(sorted("G" if l["prop_type"] == "goblin" else ("S" if l["prop_type"] == "standard" else "D")
                              for l in legs))
        mult = (power3.get(code) or fb) if all(r == "hit" for r in rs) else 0.0
    else:
        if len(legs) != 6:
            return None
        mult = flex6.get(sum(r == "hit" for r in rs), 0.0)
    return stake * mult - stake


def build(since=FIRST_DAY, write=True):
    """All pairs since `since` + the $ difference per day/board/reason. Writes PAIRS_CSV."""
    boards, res = official_boards(since)
    pairs, money, seen_pairs, seen_slips = [], [], set(), set()
    for (day, board), (origin, rows) in sorted(boards.items()):
        if board not in BOARDS:
            continue
        ps, rule = pairs_for(day, board, origin, rows, res)
        # a board day can hold the same matches as the day before (any-date boards): count once
        ps = [p for p in ps if (board, p["up_key"], p["passed_key"]) not in seen_pairs]
        seen_pairs |= {(board, p["up_key"], p["passed_key"]) for p in ps}
        slip_key = (board, frozenset(r["prop_key"] for r in rule[:SIZES[board]]))
        dup_slip = slip_key in seen_slips
        seen_slips.add(slip_key)
        pairs += ps
        if dup_slip:
            continue
        by_key = {r["prop_key"]: r for r in rows}
        res_of = lambda l: res.get((l["prop_key"], l["match_start"]), l.get("result"))
        base = _slip_profit(board, rule[:SIZES[board]], res_of)
        for reason in REASONS:
            sw = [p for p in ps if p["reason"] == reason and p["kind"] == "took a spot"]
            if not sw:
                continue
            alt = list(rule[:SIZES[board]])
            for p in sw:
                alt = [by_key[p["passed_key"]] if l["prop_key"] == p["up_key"] else l for l in alt]
            other = _slip_profit(board, alt, res_of)
            money.append({"board_day": day, "board": board, "reason": reason,
                          "diff": None if base is None or other is None else base - other})
    if write:
        os.makedirs(DIR, exist_ok=True)
        with open(PAIRS_CSV + ".tmp", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=PAIR_FIELDS)
            w.writeheader()
            w.writerows(pairs)
        os.replace(PAIRS_CSV + ".tmp", PAIRS_CSV)
    return pairs, money


def tally(pairs, money, lo, hi, reason=None):
    ps = [p for p in pairs if lo <= p["board_day"] <= hi and (reason is None or p["reason"] == reason)]
    g = [p for p in ps if p["up_result"] in ("hit", "miss") and p["passed_result"] in ("hit", "miss")]
    ms = [m for m in money if lo <= m["board_day"] <= hi and (reason is None or m["reason"] == reason)
          and m["diff"] is not None]
    return {"pairs": len(ps), "graded": len(g), "up_won": sum(p["up_result"] == "hit" for p in g),
            "passed_won": sum(p["passed_result"] == "hit" for p in g),
            "dollars": {b: sum(m["diff"] for m in ms if m["board"] == b) for b in BOARDS},
            "slips": {b: sum(1 for m in ms if m["board"] == b) for b in BOARDS}}


def _money(x):
    return ("+$%.2f" % x) if x >= 0 else ("-$%.2f" % -x)


# ------------------------------------------------------------------ the lines and cards
def daily_line(pairs=None, money=None, day=None):
    if pairs is None:
        pairs, money = build()
    day = day or (date.today() - timedelta(days=1)).isoformat()
    y = tally(pairs, money, day, day)
    s = tally(pairs, money, LIVE_FROM, "9999")
    if not y["pairs"]:
        head = "\U0001F4CA Order score yesterday: the rules never put a lower-%% pick above a higher one"
    elif not y["graded"]:
        head = "\U0001F4CA Order score yesterday: %d pair%s, not graded yet" % (y["pairs"], "" if y["pairs"] == 1 else "s")
    else:
        head = ("\U0001F4CA Order score yesterday: picks the rules moved up won %d of %d · the higher-%% picks "
                "they passed won %d of %d") % (y["up_won"], y["graded"], y["passed_won"], y["graded"])
    run = " · since Oct 25: moved up %d of %d, passed %d of %d" % (s["up_won"], s["graded"], s["passed_won"], s["graded"]) \
        if s["graded"] else ""
    return head + run


def reason_line(reason, t):
    if not t["pairs"]:
        return "%s: no pairs" % reason.capitalize()
    d = " · ".join("%s %s (%d slip%s)" % (NAMES[b], _money(t["dollars"][b]), t["slips"][b], "" if t["slips"][b] == 1 else "s")
                        for b in BOARDS if t["slips"][b])
    if not t["graded"]:
        return "%s: %d pair%s, none graded yet" % (reason.capitalize(), t["pairs"], "" if t["pairs"] == 1 else "s")
    return "%s: moved-up won %d of %d, passed won %d of %d%s" % (
        reason.capitalize(), t["up_won"], t["graded"], t["passed_won"], t["graded"],
        (" · $ on the slips: " + d) if d else "")


def weekly_card(pairs=None, money=None):
    if pairs is None:
        pairs, money = build()
    L = ["\U0001F4CA **ORDER SCORE — week of %s**" % (date.today() - timedelta(days=7)).strftime("%-m/%-d"),
         "Every time the rules put a lower-Win-Prob pick above a higher one on Top 3 / Elite 6. "
         "\"Moved-up won\" vs \"passed won\": does the rule pick better than plain Win Prob? "
         "$ = rule slip minus the slip with the passed picks ($20, + means the rule made money).",
         "", "**Since Oct 25 (posted boards)**"]
    for r in REASONS:
        L.append("• " + reason_line(r, tally(pairs, money, LIVE_FROM, "9999", r)))
    wk_lo = (date.today() - timedelta(days=7)).isoformat()
    wk_hi = (date.today() - timedelta(days=1)).isoformat()
    t = tally(pairs, money, max(wk_lo, LIVE_FROM), wk_hi)
    L += ["", "**Last 7 days:** %s" % (("moved up won %d of %d, passed won %d of %d" % (
        t["up_won"], t["graded"], t["passed_won"], t["graded"])) if t["graded"] else "nothing graded")]
    old = tally(pairs, money, FIRST_DAY, "2026-10-24")
    if old["graded"]:
        L.append("Oct 10-24 replay (today's rules on stored boards): moved up won %d of %d, passed won %d of %d" % (
            old["up_won"], old["graded"], old["passed_won"], old["graded"]))
    L.append("_Check only — nothing on the boards changes from this card._")
    return "\n".join(L)


def backfill_report(pairs=None, money=None):
    if pairs is None:
        pairs, money = build()
    L = ["ORDER SCORE BACKFILL (Top 3 + Elite 6, one official board a day)"]
    for label, lo, hi in RANGES:
        t = tally(pairs, money, lo, hi)
        L.append("")
        L.append("%s%s: %d pairs, %d graded -- moved up won %d, passed won %d" % (
            label, " (replay with today's rules)" if hi < LIVE_FROM else " (posted)", t["pairs"], t["graded"],
            t["up_won"], t["passed_won"]))
        for r in REASONS:
            L.append("  " + reason_line(r, tally(pairs, money, lo, hi, r)))
    return "\n".join(L)


def losing_alerts(pairs, money, st):
    out = []
    sent = st.setdefault("losing_sent", {})
    for r in REASONS:
        t = tally(pairs, money, LIVE_FROM, "9999", r)
        if t["graded"] >= ALERT_MIN_PAIRS and t["passed_won"] > t["up_won"] and r not in sent:
            sent[r] = datetime.now().isoformat(timespec="seconds")
            out.append("⚠️ ORDER SCORE: the %s rule is losing to plain Win Prob (%d of %d vs %d of %d). Review it." % (
                r, t["up_won"], t["graded"], t["passed_won"], t["graded"]))
    return out


def _load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(path + ".tmp", path)


def run(now=None, post=True):
    """Scheduler entry (every 15 min): 9 AM daily lines, Monday 9:10 weekly card, the 20-pair
    warning. Posts each once. Returns what it did."""
    if not ORDER_SCORE_ON:
        return "off"
    now = now or datetime.now(CT)
    import apex_ops as ops
    send = ops.send_discord if post else (lambda ch, m: print("---- #%s ----\n%s" % (ch, m)) or True)
    st = _load(STATE, {})
    today = now.date().isoformat()
    if not st and (now.hour, now.minute) >= (DAILY_AT[0] + 1, 0):
        st["daily"] = today   # first start after 10 AM: the first 9 AM lines go out tomorrow
    did = []
    try:
        pairs, money = build()
    except Exception as ex:
        if st.get("build_fail") != today:
            st["build_fail"] = today
            _save(STATE, st)
            send("alerts", "\U0001F6A8 ORDER CHECK could not run: the order score failed (%s: %s)" % (
                type(ex).__name__, str(ex)[:100]))
        return "build failed: %s" % ex
    if (now.hour, now.minute) >= DAILY_AT and st.get("daily") != today:
        try:
            import apex_order_guard as og
            g = og.daily_line()
        except Exception as ex:
            g = "\U0001F6A8 Order check: the 9 AM line failed (%s)" % type(ex).__name__
        send("system", g + "\n" + daily_line(pairs, money))
        st["daily"] = today
        did.append("daily lines")
    if now.weekday() == 0 and (now.hour, now.minute) >= WEEKLY_AT and st.get("weekly") != today:
        send("system", weekly_card(pairs, money))
        st["weekly"] = today
        did.append("weekly card")
    for m in losing_alerts(pairs, money, st):
        send("alerts", m)
        did.append("losing alert")
    _save(STATE, st)
    return "%d pairs; %s" % (len(pairs), ", ".join(did) or "nothing due")


if __name__ == "__main__":
    if "--run" in sys.argv:
        print(run())
    elif "--line" in sys.argv:
        import apex_order_guard as og
        print(og.daily_line())
        print(daily_line())
    elif "--weekly" in sys.argv:
        print(weekly_card())
    else:
        print(backfill_report())
