#!/usr/bin/env python3
"""
APEX-SLIPGRADE-2026-10-11 (owner, approved 2026-10-11): grades logged
slips automatically from the results OpenClaw already settles, so wins and
losses never have to be typed. Run from the Discord bot every 30 minutes; it
posts one short line per finished slip to #slips-pnl.

- A PENDING prop leg is graded from the settled forward log / hand log: same
  stat, the leg's player (either player for Total Games / Total Sets), match
  date from the slip date to two days after it, and never a match that had
  already started when the slip was logged (data/logs/match_start_index.json). Exact line from
  the slip: MORE wins above, LESS wins below, equal = PUSH. A retired or
  void match has no settled value, so that leg simply stays pending.
- Each leg is tagged on_board = True if that exact prop was recommended on a
  published board (apex_board_status_log_v1.csv), for board vs off-board P&L.
- A slip is finished when every logged leg is graded. Power Play rules: any
  loss = lost (-entry), all wins = won (payout - entry). Pushes are flagged
  "check payout in the app" since PrizePicks re-prices those.
Moneyline legs are left for manual grading.
- APEX-DISCOUNT-2026-10-16: a leg with manual_grade (another sport, usually a
  PrizePicks discount play) is graded by the operator in #slips-pnl --
  "reese hit" / "reese miss" / "reese push" (or "#7 reese hit" for one slip)
  -> hand_grade(). It then counts like any other leg. Discount legs on tennis
  props grade automatically at the line played.

  python3 apex_slip_grader.py --dry-run   # show what would be graded
"""
import csv, json, os, re, sys, time, collections, threading
from datetime import date, datetime

_LOCK = threading.Lock()   # APEX-DISCOUNT-2026-10-16: run() and hand_grade() share the slip log

HERE = os.path.dirname(os.path.abspath(__file__))
SLIP_LOG = os.path.join(HERE, "data", "slips", "slip_log.json")
FWD_LOG = os.path.join(HERE, "apex_prop_sim_forward_log_v1.csv")
HAND_LOG = os.path.join(HERE, "apex_engine_hand_settled_v1.csv")
STATUS_LOG = os.path.join(HERE, "apex_board_status_log_v1.csv")
START_INDEX = os.path.join(HERE, "data", "logs", "match_start_index.json")
HOLDS = os.path.join(HERE, "apex_settle_holds.json")   # APEX-VOIDGRADE-2026-10-24
STUCK_HOURS = 8   # a leg whose match started this long ago with no result is handed to you to grade
STAT_TO_CAT = {"total_games": "total_games", "total_sets": "total_sets", "sets_won": "sets_won", "aces": "aces",
               "double_faults": "dfs", "break_points_won": "breaks", "games_won": "games_won",
               "fantasy_score": "fantasy_score",
               # APEX-FIRSTSETSLIPS-2026-11-01 (owner, "Fix first set slips"): results from props_db
               "1st_set_total_games": "1st_set_total_games", "first_set_total_games": "1st_set_total_games",
               "first_set_games": "1st_set_total_games"}
MATCH_LEVEL = {"total_games", "total_sets", "1st_set_total_games"}
DONE = {"WIN", "LOSS", "PUSH", "VOID"}
SKIP = {"REJECTED_CORRELATION", "DUPLICATE_SKIPPED"}


def _n(s):
    return str(s or "").strip().lower()


def _read(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _actuals():
    """(category, name) -> list of (match_date, actual, opponent)."""
    idx = collections.defaultdict(list)
    for r in _read(FWD_LOG) + _read(HAND_LOG):
        a = r.get("actual")
        if a in (None, ""):
            continue
        try:
            a = float(a)
        except ValueError:
            continue
        cat = r.get("category")
        if r.get("player") == "match":
            pair = str(r.get("opponent") or "").split(" v ")
            for i, nm in enumerate(pair):
                opp = pair[1 - i] if len(pair) == 2 else ""
                idx[(cat, _n(nm))].append((r.get("match_date"), a, _n(opp)))
        else:
            idx[(cat, _n(r.get("player")))].append((r.get("match_date"), a, _n(r.get("opponent"))))
            if cat in MATCH_LEVEL:
                idx[(cat, _n(r.get("opponent")))].append((r.get("match_date"), a, _n(r.get("player"))))
    return idx


def _starts():
    """(player, opponent) -> latest known start time (UTC epoch seconds)."""
    out = {}
    try:
        with open(START_INDEX) as fh:
            players = json.load(fh).get("players", {})
    except Exception:
        return out
    for nm, rows in players.items():
        for r in rows or []:
            try:
                t = datetime.fromisoformat(r["start_utc"]).timestamp()
            except Exception:
                continue
            k = (_n(nm), _n(r.get("opponent")))
            out[k] = max(out.get(k, 0), t)
    return out


def _logged_ts(slip_id):
    """slip_id is '<date>_<epoch seconds when the slip was logged>'."""
    try:
        return float(str(slip_id).rsplit("_", 1)[1])
    except Exception:
        return None


def _find_actual(idx, cat, player, slip_date, logged_ts=None, starts=None):
    try:
        d0 = date.fromisoformat(str(slip_date)[:10])
    except Exception:
        return None
    best, opps = None, set()
    p = _n(player)
    for (c, nm), vals in idx.items():
        if c != cat or not nm or not (p == nm or p in nm or nm in p):
            continue
        for md, a, opp in vals:
            # APEX-SLIPDATE-2026-10-12: skip a match that had already started
            # when the slip was logged -- it can't be the match that was bet.
            st = (starts or {}).get((nm, opp))
            if logged_ts and st and st < logged_ts:
                continue
            try:
                dd = (date.fromisoformat(md) - d0).days
            except Exception:
                continue
            # Results only from the slip date on. The old one-day lookback graded
            # a 10/12 Hurkacz slip from his 10/11 match.
            if 0 <= dd <= 2:
                opps.add(opp)
                if best is None or dd < best[0]:
                    best = (abs(dd), a)
    # APEX-OPPONENTFIX-2026-10-25: no PrizePicks match to pin the leg to and the player has
    # results from two different opponents in the window -> don't guess which one was bet.
    if len(opps) > 1:
        return None
    return None if best is None else best[1]


# APEX-VOIDGRADE-2026-10-24 (owner' real slips #34-36: all three won but sat open
# into the next noon because Kawa's match was cancelled and nothing voided her leg). The
# hourly SA365 settle already writes what it could NOT settle to apex_settle_holds.json;
# a "void" hold (cancelled, walkover, retired) means PrizePicks voids that player's picks,
# so the leg is graded VOID and the slip settles on the legs that are left.
def _voids():
    """name -> list of (match_date, opponent) the settler marked void."""
    out = collections.defaultdict(list)
    try:
        with open(HOLDS) as fh:
            holds = json.load(fh).get("holds", [])
    except Exception:
        return out
    for h in holds:
        if h.get("reason") != "void":
            continue
        if h.get("player") == "match":
            pair = str(h.get("opponent") or "").split(" v ")
            for i, nm in enumerate(pair):
                out[_n(nm)].append((h.get("match_date"), _n(pair[1 - i]) if len(pair) == 2 else ""))
        else:
            out[_n(h.get("player"))].append((h.get("match_date"), _n(h.get("opponent"))))
            out[_n(h.get("opponent"))].append((h.get("match_date"), _n(h.get("player"))))
    return out


def _find_void(voids, player, slip_date, logged_ts=None, starts=None):
    """True when the leg's player had a void match (not started before the slip was logged)
    from the slip date to two days after it."""
    try:
        d0 = date.fromisoformat(str(slip_date)[:10])
    except Exception:
        return False
    p = _n(player)
    for nm, vals in voids.items():
        if not nm or not (p == nm or p in nm or nm in p):
            continue
        for md, opp in vals:
            st = (starts or {}).get((nm, opp))
            if logged_ts and st and st < logged_ts:
                continue
            try:
                if 0 <= (date.fromisoformat(str(md)[:10]) - d0).days <= 2:
                    return True
            except Exception:
                continue
    return False


def _stuck(player, logged_ts, starts, now_ts):
    """True when the leg's match started STUCK_HOURS+ ago and still has no result or void."""
    p = _n(player)
    for (nm, opp), st in (starts or {}).items():
        if nm and (p == nm or p in nm or nm in p) and (not logged_ts or st >= logged_ts):
            if now_ts - st >= STUCK_HOURS * 3600:
                return True
    return False


# APEX-OPPONENTFIX-2026-10-25 (owner' Siniakova TG MORE 17.5 / 18.5 legs, v Tararudee
# Sun 10 PM, were graded LOST at 2:07 AM from her Osaka final v Krueger, 13 games -- a logged
# leg has no opponent, so the grader took the first Siniakova match after the slip date). Each
# leg is now tied to the exact PrizePicks match first: same player (either side for match-level
# stats), stat, line and Goblin/Demon type in data/props_db.sqlite, the line already posted when
# the slip was logged and the match not yet started. One match -> the leg keeps that opponent
# and start time and is graded ONLY from that match. Two matches with the same line -> the leg
# waits for a hand grade instead of guessing. A grade that would have come from another match
# is caught and posted to #alerts once.
PROPS_DB = os.path.join(HERE, "data", "props_db.sqlite")
MATCH_DATE_SLACK = (-1, 2)   # SA365 match dates run up to 2 days after the PrizePicks start


def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _pp_rows():
    """category -> list of PrizePicks projections from the last two weeks."""
    out = collections.defaultdict(list)
    try:
        import sqlite3
        c = sqlite3.connect(PROPS_DB, timeout=30)
        c.row_factory = sqlite3.Row
        rows = c.execute("select pp_game_id, player, opponent, category, odds_type, line_first, line_last, "
                         "line_at_start, start_utc, first_seen, actual, result from projections "
                         "where start_utc >= datetime('now', '-14 days')").fetchall()
        c.close()
    except Exception:
        return out
    for r in rows:
        d = dict(r)
        d["opponent"] = re.sub(r"\s+1st Set$", "", str(d.get("opponent") or ""))
        d["start_ts"], d["seen_ts"] = _ts(d.get("start_utc")), _ts(d.get("first_seen"))
        out[d["category"]].append(d)
    return out


def _same(a, b):
    a, b = _n(a), _n(b)
    return bool(a and b) and (a == b or a in b or b in a)


def _pp_matches(pp, cat, e, logged_ts):
    """Distinct PrizePicks matches this leg can be: list of {opponent, start_ts, start_utc, game, rows}."""
    try:
        line = float(e["line"])
        d0 = date.fromisoformat(str(e.get("date"))[:10])
    except Exception:
        return []
    player, want_opp = e.get("player"), e.get("opponent")
    hits = []
    for r in pp.get(cat, []):
        if not any(r.get(k) is not None and abs(float(r[k]) - line) < 0.01
                   for k in ("line_last", "line_first", "line_at_start")):
            continue
        if _same(r["player"], player):
            opp = r["opponent"]
        elif cat in MATCH_LEVEL and _same(r["opponent"], player):
            opp = r["player"]
        else:
            continue
        if want_opp and not _same(opp, want_opp):
            continue
        st = r.get("start_ts")
        if st is None:
            continue
        if logged_ts and st < logged_ts:          # already started when the slip was logged
            continue
        if logged_ts and r.get("seen_ts") and r["seen_ts"] > logged_ts + 900:   # line not posted yet
            continue
        if not (-1 <= (datetime.utcfromtimestamp(st).date() - d0).days <= 3):
            continue
        hits.append(dict(r, leg_opp=opp))
    pt = str(e.get("prop_type") or "").replace("red_", "")
    if pt in ("goblin", "demon", "standard"):
        same = [r for r in hits if (r.get("odds_type") or "standard") == pt]
        hits = same or hits
    by = {}
    for r in hits:
        k = (_n(r["leg_opp"]), datetime.utcfromtimestamp(r["start_ts"]).date())
        m = by.setdefault(k, {"opponent": r["leg_opp"], "start_ts": r["start_ts"], "start_utc": r["start_utc"],
                              "game": r.get("pp_game_id"), "rows": []})
        m["start_ts"] = min(m["start_ts"], r["start_ts"])
        m["rows"].append(r)
    return list(by.values())


def _bound_actual(idx, cat, e, m):
    """Result for the leg from ITS match only: props_db's own settle first, then the
    forward / hand log with the same opponent and a match date near the PrizePicks start."""
    for r in m["rows"]:
        if r.get("result") in ("more", "less", "push") and r.get("actual") is not None:
            return float(r["actual"])
    if time.time() < m["start_ts"]:   # APEX-REMATCH-2026-11-01: a match not yet started has no result
        return None
    d0 = datetime.utcfromtimestamp(m["start_ts"]).date()
    p = _n(e.get("player"))
    for (c, nm), vals in idx.items():
        if c != cat or not nm or not (p == nm or p in nm or nm in p):
            continue
        for md, a, opp in vals:
            if not _same(opp, m["opponent"]):
                continue
            try:
                dd = (date.fromisoformat(md) - d0).days
            except Exception:
                continue
            if MATCH_DATE_SLACK[0] <= dd <= MATCH_DATE_SLACK[1]:
                return a
    return None


def _bound_void(voids, e, m):
    d0 = datetime.utcfromtimestamp(m["start_ts"]).date()
    p = _n(e.get("player"))
    for nm, vals in voids.items():
        if not nm or not (p == nm or p in nm or nm in p):
            continue
        for md, opp in vals:
            try:
                dd = (date.fromisoformat(str(md)[:10]) - d0).days
            except Exception:
                continue
            if _same(opp, m["opponent"]) and MATCH_DATE_SLACK[0] <= dd <= MATCH_DATE_SLACK[1]:
                return True
    return False


def _old_pick_opponent(idx, cat, player, slip_date, logged_ts, starts):
    """Opponent of the match the name-only lookup would grade from (for the mix-up alert)."""
    try:
        d0 = date.fromisoformat(str(slip_date)[:10])
    except Exception:
        return None
    best, p = None, _n(player)
    for (c, nm), vals in idx.items():
        if c != cat or not nm or not (p == nm or p in nm or nm in p):
            continue
        for md, a, opp in vals:
            st = (starts or {}).get((nm, opp))
            if logged_ts and st and st < logged_ts:
                continue
            try:
                dd = (date.fromisoformat(md) - d0).days
            except Exception:
                continue
            if 0 <= dd <= 2 and (best is None or dd < best[0]):
                best = (dd, opp)
    return None if best is None else best[1]


MIXUP_STATE = os.path.join(HERE, "data", "logs", "slip_mixup_alerted.json")


def _mixup_alert(e, right, wrong):
    """One #alerts line per player / right match / wrong match, ever (several slips with the
    same leg share it; dry runs don't repeat it)."""
    e["mixup_caught"] = "v %s, not v %s" % (right, str(wrong).title())
    key = "|".join((_n(e.get("player")), _n(right), _n(wrong)))
    try:
        with open(MIXUP_STATE) as fh:
            seen = json.load(fh)
    except Exception:
        seen = {}
    if key in seen:
        return
    seen[key] = datetime.now().isoformat(timespec="seconds")
    try:
        with open(MIXUP_STATE, "w") as fh:
            json.dump(seen, fh, indent=1)
    except Exception:
        pass
    msg = ("⚠️ Match mix-up caught: %s %s line is v %s but our data had v %s -- "
           "your slips are graded only from the v %s match" % (
               e.get("player"), STAT_NAMES.get(e.get("stat_type"), e.get("stat_type")),
               right, str(wrong).title(), right.split()[-1]))
    try:
        import apex_ops
        apex_ops.send_discord("alerts", msg)
    except Exception as ex:
        print("[SlipGrader] mix-up alert not sent: %s" % ex)
    print("[SlipGrader] " + msg)


def _board_props():
    """set of (category, name, line) recommended on any board."""
    out = set()
    for r in _read(STATUS_LOG):
        if r.get("status") != "recommended" or r.get("board") == "all":
            continue
        names = str(r.get("opponent") or "").split(" v ") if r.get("player") == "match" else [r.get("player")]
        for nm in names:
            try:
                out.add((r.get("category"), _n(nm), float(r.get("line"))))
            except (TypeError, ValueError):
                pass
    return out


# APEX-FLEXGRADE-2026-10-23 (real slips #34-36, two of them Flex): a Flex slip is
# not lost on one miss. All hit = the logged payout. Fewer hits than any Flex pays
# (PrizePicks: 3-pick 2, 4-pick 3, 5-pick 3, 6-pick 4; a push/void shrinks the
# slip) = lost. Anything in between pays a Goblin-priced partial we don't know, so
# it is held as CHECK_APP (no money counted) until the app amount is set by hand.
# A void on a Power Play re-prices it too, so it is held like a push.
FLEX_MIN_HITS = {1: 1, 2: 2, 3: 2, 4: 3, 5: 3, 6: 4}


def _is_flex(legs):
    return "flex" in str(legs[0].get("entry_type") or "").lower()


def _slip_result(legs, wins, losses, held):
    # APEX-VOIDGRADE-2026-10-24: a push/void drops the slip to the legs that are left
    # (PrizePicks re-prices it). Power stays all-or-nothing on those; a Flex with every
    # leg left hit is a win. Either way the new payout is only in the app, so a win with a
    # void/push is WON_WITH_PUSH: counted as won, payout confirmed from the app.
    if not _is_flex(legs):
        return "LOST" if losses else ("WON_WITH_PUSH" if held else "WON")
    if not losses:
        return "WON_WITH_PUSH" if held else "WON"
    need = FLEX_MIN_HITS.get(len(legs) - held)
    if need is not None and wins < need:
        return "LOST"
    return "CHECK_APP"


def _loss_note(legs):
    et = str(legs[0].get("entry_type") or "")
    if _is_flex(legs):
        return "this %s may still pay" % et
    if "power" in et.lower():
        return "this %s is lost" % et
    return "a Power Play is already lost; a Flex may still pay"


def grade(entries, now_label=None):
    """Grades PENDING legs in place. Returns (finished slip summaries, slips with
    newly settled legs that still have legs waiting)."""
    idx, onb, starts, voids = _actuals(), _board_props(), _starts(), _voids()
    pp = _pp_rows()   # APEX-OPPONENTFIX-2026-10-25
    now_ts = datetime.now().timestamp()
    touched = set()
    for e in entries:
        if e.get("status") != "PENDING" or e.get("bet_type") != "prop" or e.get("line") is None:
            continue
        cat = STAT_TO_CAT.get(e.get("stat_type"))
        if not cat:
            continue
        e.setdefault("on_board", (cat, _n(e.get("player")), float(e["line"])) in onb)
        lts = _logged_ts(e.get("slip_id"))
        ms = _pp_matches(pp, cat, e, lts)   # APEX-OPPONENTFIX-2026-10-25
        if len(ms) > 1:
            if not e.get("manual_grade"):
                e["manual_grade"] = True
                e["stuck_reason"] = "two matches with this line: " + " or ".join(
                    "v %s" % m["opponent"] for m in sorted(ms, key=lambda m: m["start_ts"]))
                print("[SlipGrader] %s %s %s: %s -- waiting for a hand grade"
                      % (e.get("player"), e.get("line"), e.get("stat_type"), e["stuck_reason"]))
            continue
        if ms:
            m = ms[0]
            e["opponent"], e["match_start_utc"], e["pp_game_id"] = m["opponent"], m["start_utc"], m["game"]
            a = _bound_actual(idx, cat, e, m)
            if a is None and time.time() < m["start_ts"]:
                wrong = _old_pick_opponent(idx, cat, e.get("player"), e.get("date"), lts, starts)
                if wrong and not _same(wrong, m["opponent"]):
                    _mixup_alert(e, m["opponent"], wrong)
            if a is None:
                if _bound_void(voids, e, m):
                    e["status"] = "VOID"
                    e["void_reason"] = "match cancelled, walkover or retirement (SA365) -- PrizePicks voids the pick"
                    e["resolved_at"] = now_label or datetime.now().strftime("%I:%M %p").lstrip("0")
                    e["auto_graded"] = True
                    touched.add(e.get("slip_id"))
                elif not e.get("manual_grade") and now_ts - m["start_ts"] >= STUCK_HOURS * 3600:
                    e["manual_grade"] = True
                    e["stuck_reason"] = "no result found %dh after the match started" % STUCK_HOURS
                continue
        else:
            a = _find_actual(idx, cat, e.get("player"), e.get("date"), lts, starts)
        if a is None:
            # APEX-VOIDGRADE-2026-10-24: cancelled / walkover / retired -> VOID
            if _find_void(voids, e.get("player"), e.get("date"), _logged_ts(e.get("slip_id")), starts):
                e["status"] = "VOID"
                e["void_reason"] = "match cancelled, walkover or retirement (SA365) -- PrizePicks voids the pick"
                e["resolved_at"] = now_label or datetime.now().strftime("%I:%M %p").lstrip("0")
                e["auto_graded"] = True
                touched.add(e.get("slip_id"))
            elif not e.get("manual_grade") and _stuck(e.get("player"), _logged_ts(e.get("slip_id")), starts, now_ts):
                # no result feed for this match (e.g. SA365 can't find it): ask for a hand grade
                # instead of leaving the slip open with no word -- "fernandez hit" in #slips-pnl
                e["manual_grade"] = True
                e["stuck_reason"] = "no result found %dh after the match started" % STUCK_HOURS
            continue
        line = float(e["line"])
        e["actual"] = a
        e["status"] = "PUSH" if a == line else ("WIN" if (a > line) == (e.get("direction") == "MORE") else "LOSS")
        e["resolved_at"] = now_label or datetime.now().strftime("%I:%M %p").lstrip("0")
        e["auto_graded"] = True
        touched.add(e.get("slip_id"))
    # APEX-SLIPLEGPOST-2026-10-12: every leg settled but not yet posted (new ones,
    # plus any graded before this change) is reported once, per slip.
    new_legs = collections.defaultdict(list)
    for e in entries:
        if (e.get("status") in DONE and (e.get("actual") is not None or e.get("hand_graded")
                                         or e.get("status") == "VOID")
                and not e.get("leg_posted")):   # APEX-DISCOUNT-2026-10-16: hand-graded legs too
            e["leg_posted"] = True
            new_legs[e.get("slip_id")].append(e)
            touched.add(e.get("slip_id"))
    finished = []
    by_slip = collections.defaultdict(list)
    for e in entries:
        if e.get("status") not in SKIP:
            by_slip[e.get("slip_id")].append(e)
    for sid in touched:
        legs = by_slip.get(sid, [])
        if not legs or any(l.get("status") not in DONE for l in legs) or any(l.get("slip_result") for l in legs):
            continue
        stake = legs[0].get("stake")
        payout = legs[0].get("potential_payout")
        losses = sum(1 for l in legs if l["status"] == "LOSS")
        pushes = sum(1 for l in legs if l["status"] == "PUSH")
        wins = sum(1 for l in legs if l["status"] == "WIN")
        voids = sum(1 for l in legs if l["status"] == "VOID")
        res = _slip_result(legs, wins, losses, pushes + voids)
        profit = None
        if stake is not None and res == "LOST":
            profit = -float(stake)
        elif stake is not None and payout is not None and res == "WON":
            profit = float(payout) - float(stake)
        all_board = all(l.get("on_board") for l in legs)
        for l in legs:
            l["slip_result"] = res
            l["slip_profit"] = profit
            l["slip_all_board"] = all_board
        finished.append({"slip_id": sid, "legs": legs, "result": res, "wins": wins, "losses": losses,
                         "pushes": pushes, "stake": stake, "payout": payout, "profit": profit, "all_board": all_board})
    done_ids = {f["slip_id"] for f in finished}
    progress = []
    for sid, nl in new_legs.items():
        legs = by_slip.get(sid, [])
        if sid in done_ids or not legs or any(l.get("slip_result") for l in legs):
            continue
        progress.append({"slip_id": sid, "new_legs": nl, "legs": legs})
    return finished, progress


def _leg_text(l):
    mark = {"WIN": "✅", "LOSS": "❌", "PUSH": "➖", "VOID": "⚪"}[l["status"]]
    return "%s %s %s %s %s (actual %s)%s" % (mark, l.get("player"), l.get("direction"), l.get("line"),
                                           l.get("stat_type"), ("%g" % l["actual"]) if l.get("actual") is not None else "?",
                                           " · board" if l.get("on_board") else "")


def progress_line(p):
    """One short update when legs settle on a slip that still has legs waiting."""
    legs = p["legs"]
    w = sum(1 for l in legs if l["status"] == "WIN")
    lo = sum(1 for l in legs if l["status"] == "LOSS")
    pu = sum(1 for l in legs if l["status"] == "PUSH")
    wait = sum(1 for l in legs if l["status"] not in DONE)
    stake = legs[0].get("stake")
    money = "" if stake in (None, "") else (", bonus entry" if float(stake) == 0 else ", $%g entry" % float(stake))
    head = "📋 **Slip update** (%s%s)" % (legs[0].get("date") or "?", money)
    tally = "So far: %d won, %d lost%s, %d still waiting" % (w, lo, (", %d push" % pu) if pu else "", wait)
    if lo:
        tally += " · " + _loss_note(legs)
    return "\n".join([head] + ["  " + _leg_text(l) for l in p["new_legs"]] + [tally])


def summary_line(f):
    icon = {"LOST": "❌", "WON": "✅", "WON_WITH_PUSH": "🟡", "CHECK_APP": "🟡"}[f["result"]]
    head = {"LOST": "Slip lost", "WON": "Slip won", "WON_WITH_PUSH": "Slip won with a push/void -- confirm the payout in the app",
            "CHECK_APP": "Flex paid part -- check the payout in the app"}[f["result"]]
    money = ""
    if f["profit"] is not None:
        if f["stake"] == 0 and f["result"] == "LOST":
            money = " · bonus entry, $0 of your money lost"
        else:
            money = " · %s$%.2f%s" % ("+" if f["profit"] >= 0 else "-", abs(f["profit"]), " (bonus)" if f["stake"] == 0 else "")
    lines = ["%s **%s** -- %d of %d%s%s" % (icon, head, f["wins"], len(f["legs"]), money,
                                           "" if f["all_board"] else " · includes off-board picks")]
    for l in f["legs"]:
        lines.append("  " + _leg_text(l))
    return "\n".join(lines)


# APEX-SLIPCARDS-2026-10-13 (owner: "separated to per slip and look more
# professional ... a lose emoji and a won"): each slip is its own Discord card --
# green when won, red when lost, gold for a won-with-push, blue for an update.
# Every leg is listed with its own mark so the card reads on a phone.
STAT_NAMES = {"total_games": "Total Games", "total_sets": "Total Sets", "sets_won": "Sets Won", "aces": "Aces",
              "double_faults": "Double Faults", "break_points_won": "Break Points Won", "games_won": "Games Won",
              "fantasy_score": "Fantasy Score"}
COLORS = {"WON": 0x2ECC71, "LOST": 0xE74C3C, "WON_WITH_PUSH": 0xF1C40F, "CHECK_APP": 0xF1C40F, "UPDATE": 0x3498DB, "UPDATE_LOSS": 0xE67E22}
LEG_MARK = {"WIN": "✅", "LOSS": "❌", "PUSH": "➖", "VOID": "⚪"}


def _money(x):
    return "%s$%.2f" % ("+" if x >= 0 else "-", abs(x))


def _entry_text(stake):
    if stake in (None, ""):
        return "entry not logged"
    return "Bonus entry" if float(stake) == 0 else "$%.2f entry" % float(stake)


def _leg_tags(l):
    """APEX-DISCOUNT-2026-10-16: 🟢 Goblin / 🔴 Demon (PrizePicks' colors) and 🏷️ Discount."""
    t = {"goblin": " · 🟢 Goblin", "red_demon": " · 🔴 Demon", "demon": " · 🔴 Demon"}.get(l.get("prop_type") or "", "")
    if l.get("discount"):
        t += " · 🏷️ Discount"
    return t


def _card_leg(l):
    mark = LEG_MARK.get(l.get("status"), "⏳")
    stat = l.get("stat_label") or STAT_NAMES.get(l.get("stat_type"), str(l.get("stat_type") or "").replace("_", " ").title())
    line = "%s **%s** · %s %s %s%s" % (mark, l.get("player"), stat, l.get("direction"), l.get("line"), _leg_tags(l))
    if l.get("actual") is not None and l.get("status") in DONE:
        line += " · finished **%g**" % l["actual"]
    elif l.get("status") == "VOID":
        line += " · void (did not play)"
    elif l.get("hand_graded") and l.get("status") in DONE:
        line += " · graded by you"
    elif l.get("status") not in DONE:
        line += " · waiting for your grade" if l.get("manual_grade") else " · waiting"
    return line


def slip_card(kind, legs, f=None):
    """Card dict: title, description, color, legs (lines), footer, text (plain fallback)."""
    w = sum(1 for l in legs if l["status"] == "WIN")
    lo = sum(1 for l in legs if l["status"] == "LOSS")
    pu = sum(1 for l in legs if l["status"] == "PUSH")
    wait = sum(1 for l in legs if l["status"] not in DONE)
    stake, date_ = legs[0].get("stake"), legs[0].get("date") or "?"
    if kind == "final":
        res = f["result"]
        if res == "LOST":
            title = "❌ SLIP LOST"
            money = "$0 of your money lost (bonus)" if f["stake"] == 0 else (_money(f["profit"]) if f["profit"] is not None else "")
        elif res == "WON":
            title = "✅ SLIP WON"
            money = (_money(f["profit"]) + (" (bonus)" if f["stake"] == 0 else "")) if f["profit"] is not None else ""
        elif res == "CHECK_APP":
            title = "🟡 FLEX PAID PART"
            money = "check the payout in the app"
        else:
            title = "🟡 SLIP WON WITH A PUSH/VOID"
            money = "won -- confirm the payout in the app"
        desc = "**%d of %d hit**%s\n%s · logged %s" % (w, len(legs), (" · " + money) if money else "", _entry_text(stake), date_)
        color = COLORS[res]
        footer = "All picks from the boards" if f["all_board"] else "Includes off-board picks"
    else:
        title = "📋 SLIP UPDATE"
        desc = "**%d won · %d lost%s · %d waiting**\n%s · logged %s" % (
            w, lo, (" · %d push" % pu) if pu else "", wait, _entry_text(stake), date_)
        if lo:
            desc += "\n⚠️ " + _loss_note(legs)[0].upper() + _loss_note(legs)[1:]
        color = COLORS["UPDATE_LOSS" if lo else "UPDATE"]
        footer = "Updates as each pick settles"
    rows = [_card_leg(l) for l in legs]
    text = "\n".join(["**%s**" % title, desc.replace("\n", " · ")] + rows)
    return {"title": title, "description": desc, "color": color, "legs": rows, "footer": footer, "text": text,
            "kind": kind, "day": legs[0].get("date")}   # APEX-PNLCHANNEL-2026-10-23: #pnl reposts on a final


def _save(entries):
    tmp = SLIP_LOG + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(entries, fh, indent=2, default=str)
    os.replace(tmp, SLIP_LOG)


_SEEN = {}


def sources_changed():
    """APEX-VOIDGRADE-2026-10-24: True when a result file (forward log, hand log, settle
    holds) changed since the last call, so the bot grades right after each hourly result
    ingest instead of waiting up to 30 minutes."""
    sig = []
    for p in (FWD_LOG, HAND_LOG, HOLDS):
        try:
            sig.append(os.path.getmtime(p))
        except OSError:
            sig.append(None)
    changed = _SEEN.get("sig") != sig
    _SEEN["sig"] = sig
    return changed


def run(save=True, cards=False):
    with _LOCK:
        return _run(save, cards)


def _run(save=True, cards=False):
    try:
        with open(SLIP_LOG) as fh:
            entries = json.load(fh)
    except Exception:
        return []
    finished, progress = grade(entries)
    if save:
        _save(entries)
    if cards:
        return [slip_card("update", p["legs"]) for p in progress] + [slip_card("final", f["legs"], f) for f in finished]
    return [progress_line(p) for p in progress] + [summary_line(f) for f in finished]


# APEX-DISCOUNT-2026-10-16 (owner: discount plays must count, hit or not):
# legs with no stats feed are graded by hand from #slips-pnl.
HAND_WORDS = {"hit": "WIN", "won": "WIN", "miss": "LOSS", "missed": "LOSS", "push": "PUSH", "void": "VOID"}
HAND_RE = re.compile(r"^\s*(?:#(\d+)\s+)?(?:grade\s+)?(.+?)\s+(hit|won|miss|missed|push|void)\s*$", re.IGNORECASE)


def _name_hit(query, player):
    q, pl = _n(query), _n(player)
    return bool(q) and (q == pl or q in pl.split() or (len(q) >= 4 and q in pl))


def short_name(player):
    """Last name for the 'reese hit' hint (drops a '(WNBA)' style suffix)."""
    return (re.sub(r"\(.*?\)", " ", str(player or "")).split() or ["name"])[-1].lower()


def hand_grade(query, word, slip_id=None, max_days=5):
    """Grade the operator's pending manual legs whose player matches `query`.
    Same player / stat / line / pick on several slips = one grade for all.
    Returns {"graded": [legs], "ask": text or None}."""
    status = HAND_WORDS.get(str(word).lower())
    if not status:
        return {"graded": [], "ask": None}
    with _LOCK:
        try:
            with open(SLIP_LOG) as fh:
                entries = json.load(fh)
        except Exception:
            return {"graded": [], "ask": None}
        cutoff = date.today().toordinal() - max_days
        cands = []
        for e in entries:
            if e.get("status") != "PENDING" or not e.get("manual_grade") or not _name_hit(query, e.get("player")):
                continue
            if slip_id and e.get("slip_id") != slip_id:
                continue
            try:
                if date.fromisoformat(str(e.get("date"))[:10]).toordinal() < cutoff:
                    continue
            except Exception:
                pass
            cands.append(e)
        if not cands:
            return {"graded": [], "ask": None}
        groups = {(_n(e.get("player")), e.get("stat_type"), e.get("line"), e.get("direction")) for e in cands}
        if len(groups) > 1:
            return {"graded": [], "ask": "More than one pick matches \"%s\": %s. Add the slip number, e.g. `#7 %s hit`."
                    % (query, "; ".join("%s %s %s %s" % (e.get("player"), e.get("direction"), e.get("line"),
                                                         e.get("stat_label") or e.get("stat_type")) for e in cands[:4]),
                       query)}
        now_label = datetime.now().strftime("%I:%M %p").lstrip("0")
        for e in cands:
            e["status"] = status
            e["hand_graded"] = True
            e["resolved_at"] = now_label
        _save(entries)
        return {"graded": cands, "ask": None}


def manual_reminders():
    """One reminder line per pending hand-graded pick whose slip date has passed
    (once per day per pick); marks them reminded."""
    with _LOCK:
        try:
            with open(SLIP_LOG) as fh:
                entries = json.load(fh)
        except Exception:
            return []
        today = date.today().isoformat()
        seen, out = set(), []
        for e in entries:
            if (e.get("status") != "PENDING" or not e.get("manual_grade") or str(e.get("date") or "9")[:10] >= today
                    or e.get("reminded_on") == today):
                continue
            e["reminded_on"] = today
            k = (_n(e.get("player")), e.get("stat_type"), e.get("line"), e.get("direction"))
            if k in seen:
                continue
            seen.add(k)
            nm = short_name(e.get("player"))
            out.append("🏷️ **Needs your grade:** %s %s %s %s (%s, logged %s) -- reply `%s hit`, `%s miss` or `%s push`."
                       % (e.get("player"), e.get("direction"), e.get("line"), e.get("stat_label") or e.get("stat_type"),
                          e.get("sport") or "other sport", e.get("date"), nm, nm, nm))
        if out:
            _save(entries)
        return out


def pnl_summary(days=7):
    """Short P&L lines for the daily status: last `days` days, board-only vs mixed."""
    try:
        with open(SLIP_LOG) as fh:
            entries = json.load(fh)
    except Exception:
        return []
    cutoff = (date.today().toordinal() - days)
    slips = {}
    for e in entries:
        if e.get("slip_result") and e.get("date"):
            try:
                if date.fromisoformat(str(e["date"])[:10]).toordinal() < cutoff:
                    continue
            except Exception:
                continue
            slips[e["slip_id"]] = e
    if not slips:
        return []
    def agg(xs):
        w = sum(1 for x in xs if x["slip_result"].startswith("WON")); l = len(xs) - w
        p = sum(x["slip_profit"] for x in xs if x.get("slip_profit") is not None)
        return "%d-%d, %s$%.2f" % (w, l, "+" if p >= 0 else "-", abs(p))
    allx = list(slips.values())
    board = [x for x in allx if x.get("slip_all_board")]
    mixed = [x for x in allx if not x.get("slip_all_board")]
    return ["Slips (last %d days): %s" % (days, agg(allx)),
            "Board-only slips: %s · With off-board picks: %s" % (agg(board) if board else "--", agg(mixed) if mixed else "--")]


if __name__ == "__main__":
    out = run(save="--dry-run" not in sys.argv)
    print("\n\n".join(out) if out else "nothing to grade")
