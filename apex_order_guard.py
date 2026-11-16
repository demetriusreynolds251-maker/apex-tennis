#!/usr/bin/env python3
"""
APEX-ORDERGUARD-2026-10-25 (owner: "I just want to be sure that things are in correct order
and that we aren't mixing up what we back test as better for what would be the lessor. We need
something to keep track and be sure that this doesn't break silently."):

ORDER CHECK -- runs on every board publish (PrizePicks from apex_board_history.log_publish,
ParlayPlay from log_pp_publish). CHECK ONLY: it never changes a board, a rank or a pick.

For every board (Top 3, Elite 6, Top 10, Best Goblin, Best Demon, Best Standard, ParlayPlay) it
re-ranks the board's own qualifier list on its own, from the live rules:
  rank = Win Prob + 5 points per net signal point (🏟 blowout counts double), minus the 17.5 push-down
         (Best Standard: Win Prob x its market's Standard record)
  holds (no book total, 17.5 skip, Fantasy LESS, DF after a match, low bet365 room) never take a spot
  one prop per match/player -- the one with the best rank (best line per match)
  spots #1-3 need 75% (Best Demon 62, Best Standard 68 adjusted); short of that, highest Win Prob
  floors: 70% Win Prob + Apex 67 (Best Demon 60% MORE, Best Standard its market rule, Best Goblin 72.1%)
then compares spot by spot with what was posted, BACKUP numbers included. Every switch the order
depends on is checked too (a switch off, or a signal data source erroring, counts as a mismatch).

  each mismatch -> one #alerts line, once per day:
     🚨 ORDER CHECK: Elite 6 #5 should be X (score 96.5) but Y (93.6) is there -- review
  the checker itself breaks -> 🚨 ORDER CHECK could not run: <reason>   (once per reason per day)
  9 AM #system -> ✅ Order check: N board updates yesterday, all in correct order   (apex_order_score.run)

Every check is logged in data/order_guard/checks.csv (one row per publish).

  python3 apex_order_guard.py            # re-check the newest stored publish (prints, posts nothing)
  python3 apex_order_guard.py --line     # print yesterday's 9 AM line
Undo: ORDER_GUARD_ON = False (no checks, no alerts; boards were never touched).
"""
import csv, json, os, sys, traceback
from datetime import datetime, date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "data", "order_guard")
CHECKS = os.path.join(DIR, "checks.csv")
STATE = os.path.join(DIR, "state.json")
ORDER_GUARD_ON = True
DRY = False                     # True = print instead of posting (tests, CLI)
TIE = 0.0005                    # rank scores this close are a tie, either order is right
POOL_MAX_AGE_MIN = 20           # the board's qualifier list must come from this publish
MAX_LINES_PER_BOARD = 3         # one bad spot shifts the rest; report the first few
BOARD_NAMES = {"top3": "Top 3", "elite6": "Elite 6", "top10": "Top 10", "bestgoblin": "Best Goblin",
               "bestdemon": "Best Demon", "beststandard": "Best Standard", "parlayplay": "ParlayPlay"}
CHECK_FIELDS = ["checked_at", "platform", "publish_id", "trigger", "boards", "spots_checked",
                "mismatches", "problems", "pool_source"]

# Every switch the board order depends on, and the value it should have. A switch that differs
# (or can't be read) is a mismatch. Change the expected value here when he says go to a change.
SWITCHES = [
    ("apex_edge_tracker", "NET_SIGNAL_POINTS", 0.05, "5 points per net signal"),
    ("apex_edge_tracker", "TOP3_NET_SIGNAL", True, "Top 3 ranks by signals too"),
    ("apex_edge_tracker", "RERANK_AFTER_TOP3", True, "boards re-ranked by signals"),
    ("apex_edge_tracker", "EMPTY_BOARD_FIX", True, "empty boards are written; first-set picks join an empty board"),
    ("apex_card_signals", "BLOWOUT_DOUBLE", True, "blowout double"),
    ("apex_edge_tracker", "TOP_SLOTS_MIN_BY_BOARD", {"bestdemon": 62, "beststandard": 68}, "Demon 62 / Standard 68 top bars"),
    ("apex_edge_tracker", "BEST_RANK_PER_MATCH", True, "best line per match"),
    ("apex_edge_tracker", "LINE70_RULE", True, "Fantasy line 4+ needs 70% to win"),
    ("apex_best_time", "BEST_TIME_TRACKER_ON", True, "best time tracker"),
    ("apex_board_counters", "BOARD_COUNTERS_ON", True, "board counters"),
    ("apex_player_history", "PLAYER_HISTORY_FILLED", True, "filled player history"),
    ("apex_card_signals", "ACES_SIGNALS_V2", True, "Aces signals v2"),
    ("apex_set_games", "SET_GAMES_ON", True, "first-set list"),
    ("apex_edge_tracker", "MIXED_IDS_FIX", True, "same match with mixed ids not retired"),
    ("apex_two_pulls", "ON", True, "line comes off only after two pulls"),
    ("apex_player_checks", "NAME_HOLD", True, "odds name not confirmed goes to backups"),
    ("apex_player_checks", "THIN_HOLD", True, "thin stats go to backups"),
    # APEX-FIXCHECKER-2026-11-02 (owner, "Go fix checker"): the rules that went live after the checker was written
    ("apex_player_checks", "THIN_DISQUALIFY", True, "lower-level players off every board"),
    ("apex_player_checks", "THIN_ALL", True, "thin rule covers every prop type, Total Games too"),
    ("apex_player_checks", "THIN_MIN", 5, "thin = under 5 matches on file"),
    ("apex_ban_list", "BAN_LIST_ON", True, "banned players list"),
    ("apex_edge_tracker", "LINE175_NODATA_TO_BACKUPS", True, "17.5 not-enough-data hold"),
    ("apex_edge_tracker", "SURFACE_HOLD", True, "surface hold"),
    ("apex_edge_tracker", "BEST_GOBLIN_SHOW_ALL_MIN_PROB", 0.75, "Best Goblin shows every 75%+"),
    ("apex_edge_tracker", "FS_BACKUPS", True, "first-set picks listed in BACKUPS"),
    ("apex_edge_tracker", "GOBLIN_SHOW_ALL_FIX", True, "Best Goblin shows every 75+"),
    ("apex_set_games", "FS_ENGINE_ON", True, "first-set engine breaks ties"),
    ("apex_set_games", "SET_GAMES_RANK", True, "first-set picks compete in Ranking order"),
    ("apex_set_games", "SET_GAMES_FILL", False, "first-set fill-only off"),
    ("apex_card_signals", "ACES_V2_PENALTIES", False, "Aces v2 penalties off"),
    ("apex_card_signals", "FAV_ROOM_RULE", True, "no favorite penalty with 4+ room"),
    ("apex_standard_paper", "STANDARD_LINE_PAPER", True, "Standard line paper test"),
    ("apex_slips_v2", "ELITE5_SLIP_ON", True, "Elite 5 slip card"),   # APEX-ELITE5-2026-10-31
    ("apex_edge_tracker", "FANTASY_RANK_POINTS", False, "Fantasy signals add no rank points"),
    ("apex_edge_tracker", "ONE_ROOM_RULE", True, "every Total Games over needs 4+ room"),
    ("apex_edge_tracker", "DF_MORE_HOLD", True, "Double Faults MORE held"),
    ("apex_edge_tracker", "CUSHION_FILL_ON", True, "75%+ favorite Total Games off, freed spots need 4+ room"),
    ("apex_edge_tracker", "FANTASY_TG_RULE", True, "Fantasy 75+ / Total Games 70+ keeps Total Games"),
    ("apex_edge_tracker", "BACKUP_BOARD_ORDER", True, "backups in board order"),
    ("apex_play_this", "PLAY_THIS_ON", True, "one PLAY THIS card a day"),   # APEX-PLAYTHIS-2026-11-04
    ("apex_edge_tracker", "HOLD_SLOT_MOVES_UP", True, "held pick's slot moves up"),
    ("apex_edge_tracker", "LOW_CUSHION_BACKUPS", True, "low bet365 room hold"),
    ("apex_edge_tracker", "NO_BOOK_TOTAL_TO_BACKUPS", True, "no book total hold"),
    ("apex_edge_tracker", "LINE175_SKIP_TO_BACKUPS", True, "17.5 skip hold"),
    ("apex_edge_tracker", "LINE175_NODATA_PENALTY", 0.05, "17.5 not-enough-data push-down"),
    ("apex_edge_tracker", "FANTASY_LESS_TO_BACKUPS", True, "Fantasy LESS hold"),
    ("apex_edge_tracker", "DF_LESS_PLAYED_YESTERDAY_HOLD", True, "DF after a match hold"),
    ("apex_edge_tracker", "BEST_STANDARD_MARKET_RANK", True, "Best Standard market rank"),
    ("apex_edge_tracker", "BOARD_MIN_PROB", 0.70, "70% floor"),
    ("apex_edge_tracker", "BOARD_MIN_SCORE", 67, "Apex 67 floor"),
    ("apex_edge_tracker", "DEMON_MIN_PROB", 0.60, "Best Demon 60% floor"),
    ("apex_edge_tracker", "_LINE175_RULES_OFF", False, "17.5 rules not left off by a test arm"),
    ("apex_edge_tracker", "NO_DOUBLE_POST", True, "timed runs do not post a board while a /fetch is running"),   # APEX-NODOUBLEPOST-2026-11-02
    ("apex_edge_tracker", "_DF_FS_HOLDS_OFF", False, "DF/Fantasy holds not left off by a test arm"),
    ("apex_fair_points_paper", "FAIR_POINTS_PAPER_ON", True, "fair points paper test (shadow only, never posted)"),
    ("apex_edge_tracker", "SCORE_DISPLAY", True, "board Score shown next to Win Prob (display only)"),
    ("apex_alert_chain", "CHAINS_ON", True, "#alerts chain cards edit in place (display only)"),
]
PP_SWITCHES = [("apex_parlayplay", "PP_BOARD_RULES", True, "ParlayPlay uses the PrizePicks board rules"),
               ("apex_parlayplay", "PP_THIN_OFF", True, "lower-level players off the ParlayPlay board")]   # APEX-FIXCHECKER-PP-2026-11-02


# ------------------------------------------------------------------ small helpers
def _t():
    import apex_edge_tracker as t
    return t


def _load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        json.dump(obj, fh, indent=1, default=str)
    os.replace(path + ".tmp", path)


# APEX-PPALERTS-2026-10-29 (owner, "I shouldn't have any parleyplay alerts in PrizePicks alerts
# channel" -> "Go pp alerts"): every ORDER CHECK card for the ParlayPlay board (problem, ✅ fixed, could
# not run) goes to ParlayPlay's own #pp-alerts through the ParlayPlay app's sender. PrizePicks cards
# stay in #alerts. Undo: PP_CARDS_TO_PP_ALERTS = False.
PP_CARDS_TO_PP_ALERTS = True


def _is_pp(platform):
    return PP_CARDS_TO_PP_ALERTS and str(platform or "").lower() == "parlayplay"


def _pp_post(embed, text):
    """Post one ParlayPlay ORDER CHECK card (as text) to #pp-alerts. Never raises."""
    body = text
    try:
        import apex_alert_cards as C
        if embed:
            body = C.to_text(embed)
    except Exception:
        pass
    if DRY:
        print("---- would post to #pp-alerts ----\n" + body)
        return True
    try:
        pp = sys.modules.get("apex_parlayplay")
        if pp is None:
            d = os.path.join(HERE, "ApexParlayPlay")
            if d not in sys.path:
                sys.path.insert(0, d)
            import apex_parlayplay as pp
        return pp.send("alerts", body)
    except Exception as ex:
        print("[OrderGuard] #pp-alerts send failed (%s): %s" % (type(ex).__name__, ex))
        return False


def _send(text):
    if DRY:
        print("---- would post to #alerts ----\n" + text)
        return True
    try:
        import apex_ops as ops
        return ops.send_discord("alerts", text)
    except Exception as ex:
        print("[OrderGuard] send failed (%s): %s" % (type(ex).__name__, ex))
        return False


def _ident(p):
    return tuple(_t()._apex_prop_identity(p))


def _short(p):
    import apex_board_history as bh
    d = str(p.get("direction") or "").upper()
    return bh._short(p) + (" LESS" if d == "LESS" else "")


def _sc(d):
    try:
        return float(d.get("sc", d.get("score")))
    except (TypeError, ValueError):
        return None


def _rank(flag, p):
    return _t().board_rank_of("pp" if flag == "parlayplay" else flag, p)


def _bar_value(flag, p):
    t = _t()
    return t._standard_rank_prob(p) if flag == "beststandard" else t._board_prob(p)


def hold_reason(p):
    """Every rule that keeps a qualified prop off the numbered spots (our own copy of the list)."""
    t = _t()
    why = t.board_hold_reason(p)
    if why:
        return why
    try:   # APEX-FIXCHECKER-2026-11-02: a lower-level player's prop (Total Games too) is off every board
        import apex_player_checks as _pc
        if _pc.disqualified(p):
            return "lower-level player, under %d matches on file, off every board" % _pc.THIN_MIN
    except Exception:
        pass
    if t.LOW_CUSHION_BACKUPS:
        import apex_blowout_signal as bs
        return bs.low_cushion_reason(p)
    return None


def floor_problem(flag, p, sc):
    """Why a prop can't be on this board at all (None = passes the board's floor)."""
    t = _t()
    pr = t._board_prob(p)
    pt = str(p.get("prop_type") or "standard").lower()
    if flag == "bestdemon":
        if pt != "demon" or str(p.get("direction") or "").upper() != "MORE":
            return "not a Demon MORE"
        if pr < t.DEMON_MIN_PROB - 1e-9:
            return "under Best Demon's %d%%" % round(100 * t.DEMON_MIN_PROB)
        return None
    if flag == "beststandard":
        if pt != "standard":
            return "not a Standard"
        return None if t._passes_standard_rule(p) else "outside the Best Standard rule"
    if pr < t.BOARD_MIN_PROB - 1e-9:
        return "under %d%% Win Prob" % round(100 * t.BOARD_MIN_PROB)
    if sc is not None and sc < t.BOARD_MIN_SCORE:
        return "Apex %d, under %d" % (int(sc), t.BOARD_MIN_SCORE)
    if flag == "bestgoblin" and (pt != "goblin" or not t._best_eligible(p, "goblin")):
        return "under Best Goblin's Goblin floor"
    return None


def _usable(p):
    t = _t()
    try:
        return not (t._is_unplayable(p) or t._pub_match_started(p) or t._surface_hold(p))
    except Exception:
        return True


# ------------------------------------------------------------------ the independent re-rank
def expected_board(flag, cands, n):
    """cands: [(p, sc)] that pass the floor. Returns [(p, rank)] for n spots, by the live rules."""
    t = _t()
    rows = []
    for p, sc in cands:
        if hold_reason(p):
            continue
        rows.append({"p": p, "sc": sc or 0.0, "rank": _rank(flag, p), "wp": t._board_prob(p),
                     "bar": _bar_value(flag, p), "names": t._prop_names(p)})
    # best line per match: each match/player keeps its best-ranked prop
    key = (lambda r: (-r["rank"], -r["sc"])) if t.BEST_RANK_PER_MATCH else (lambda r: (-r["wp"], -r["sc"]))
    groups = []
    for r in sorted(rows, key=key):
        g = next((g for g in groups if g[0] & r["names"]), None)
        if g:
            g[0] |= r["names"]
        else:
            groups.append([set(r["names"]), r])
    reps = sorted((g[1] for g in groups), key=lambda r: (-r["rank"], -r["sc"]))
    bar = t.top_slots_bar(None if flag == "parlayplay" else flag)
    out = []
    top_n = min(t.TOP_SLOTS_COUNT, n)
    if bar and t.TOP3_NET_SIGNAL:
        out += [r for r in reps if r["bar"] >= bar / 100.0 - 1e-9][:top_n]
        if len(out) < top_n:   # short of bar picks: highest Win Prob fills the top spots
            out += sorted([r for r in reps if all(r is not o for o in out)],
                          key=lambda r: (-r["bar"], -r["rank"]))[:top_n - len(out)]
    out += [r for r in reps if all(r is not o for o in out)]
    return out[:n], reps


def _board_limit(flag, reps):
    t = _t()
    if flag == "parlayplay":
        pp = sys.modules.get("apex_parlayplay")
        return getattr(pp, "BOARD_SIZE", None)
    lim = t._BOARD_LIMITS.get(flag)
    if flag == "bestgoblin" and t.BEST_GOBLIN_SHOW_ALL_MIN_PROB:
        # APEX-ORDERCHECKFIX-2026-11-01: first-set picks don't widen the show-all count (false alarm 11/1 12:00)
        lim = max(lim, sum(1 for r in reps if r["wp"] >= t.BEST_GOBLIN_SHOW_ALL_MIN_PROB - 1e-9
                           and (r["p"] or {}).get("category") != "1st_set_total_games"))
    return lim


# APEX-KEEPSPOT-2026-10-26 (owner, 10/26 6:44 PM, "Fix this if it's a problem"): the 6:18 PM
# ParlayPlay ORDER CHECK was a false alarm. ParlayPlay pulled Sara Sorribes Tormo v Zeynep Sonmez
# TG 16.5 (#2) and the moved-line rule kept the pick at #2 on the new 17.5 line (78.5%), which ranks
# below #3-#4 by score. That is the rule working. The check now leaves a pick that filled a pulled
# line in its spot and compares the rest of the board in order. Undo: KEEP_SPOT_OK = False.
KEEP_SPOT_OK = True
# APEX-FIXCHECKER-2026-11-02 (owner, "Go fix checker"): a pick that qualifies and is not held must be posted
# somewhere, a numbered spot or a BACKUP. Rublev v Etcheverry 1st set 8.5 (81) vanished at 9:25 AM 11/2 and the
# checker, which only ordered the posted backups, never saw it. Undo: MISSING_BACKUP_CHECK = False.
MISSING_BACKUP_CHECK = True
START_EDGE_MIN = 3              # APEX-STARTCLOCK-2026-10-27: a posted pick whose match started this close to the
                                # check is "on the edge" (the board was built seconds earlier) -- it keeps its spot
                                # and is never the reason for an ORDER CHECK card; the next update drops it anyway


def _edge_started(p):
    """True when this pick's match started within START_EDGE_MIN minutes before now (the board
    builder ran seconds before the start; the next board update removes it)."""
    try:
        t = _t()
        s = t._current_start_utc(p)
        if not s:
            return False
        from datetime import timezone as _tz, timedelta as _td
        st = datetime.fromisoformat(s)
        if st.tzinfo is None:
            st = st.replace(tzinfo=_tz.utc)
        now = datetime.now(_tz.utc)
        return st <= now < st + _td(minutes=START_EDGE_MIN)
    except Exception:
        return False


def _kept_spot(row):
    """True when this posted row filled a pulled line (moved-line / pulled-fill rules)."""
    try:
        if not KEEP_SPOT_OK or row.get("spot_kind") != "main":
            return False
        txt = ("%s %s" % (row.get("why_changed") or "", row.get("reason") or "")).lower()
        return "left the board" in txt or "kept at new line" in txt   # a backup that filled a pulled spot stays put
    except Exception:
        return False


def check_board(flag, main, backups, pool, keep=(), off_notes=None, held=()):
    """main/backups: [(p, sc)] in posted order; pool: [(p, sc)] the board's qualifier list.
    keep: prop identities allowed to stay in their posted spot (APEX-KEEPSPOT-2026-10-26).
    Returns [(dedupe key, alert text)]."""
    t = _t()
    name = BOARD_NAMES.get(flag, flag)
    out = []
    keep = set(keep or ())
    seen, cands = set(), []
    _blocked = t.cushion_blocked_idents("pp" if flag == "parlayplay" else flag)   # APEX-CUSHIONFILL-2026-11-01
    for p, sc in list(main) + list(pool):
        k = _ident(p)
        if k in seen or not _usable(p) or floor_problem(flag, p, sc) or k in _blocked:   # APEX-FILL75-2026-10-29
            continue
        seen.add(k)
        cands.append((p, sc))
    # posted picks that break a rule by themselves
    used = []
    for i, (p, sc) in enumerate(main, 1):
        why = floor_problem(flag, p, sc)
        hr = hold_reason(p)
        nm = t._prop_names(p)
        dup = next((j for j, x in used if x & nm), None)
        used.append((i, nm))
        if not (why or hr or dup) and p.get("category") == "total_games":   # APEX-ORDERCHECKFIX-2026-11-01
            try:
                import apex_card_signals as _cs
                if _cs.match_favorite(p) is None:
                    why = "no favorite data, the 75 rule could not run"
            except Exception:
                pass
        if why or hr or dup:
            what = ("is under the floor (%s)" % why) if why else (
                ("should be held (%s)" % hr) if hr else "is a second prop from the same match/player as #%d" % dup)
            out.append(("rule|%s|%d|%s" % (flag, i, "|".join(map(str, _ident(p)))),
                        "\U0001F6A8 ORDER CHECK: %s #%d %s (score %.1f) %s but it is on the board — review" % (
                            name, i, _short(p), 100 * _rank(flag, p), what),
                        {"flag": flag, "kind": "rule", "spot": "#%d" % i, "posted": p, "should": None, "detail": what}))
    exp, reps = expected_board(flag, cands, len(main))
    lim = _board_limit(flag, reps)
    want_n = min(lim, len(reps)) if lim else len(main)
    if len(main) < want_n:
        miss = [r for r in reps if all(_ident(r["p"]) != _ident(p) for p, _ in main)]
        if miss:
            r = miss[0]
            out.append(("short|%s|%d|%s" % (flag, len(main), "|".join(map(str, _ident(r["p"])))),
                        "\U0001F6A8 ORDER CHECK: %s posted %d picks but %d qualify — #%d should be %s (score %.1f) — review" % (
                            name, len(main), want_n, len(main) + 1, _short(r["p"]), 100 * r["rank"]),
                        {"flag": flag, "kind": "short", "spot": "#%d" % (len(main) + 1), "posted": None, "should": r["p"],
                         "detail": "posted %d picks but %d qualify" % (len(main), want_n)}))
    n_bad = 0
    if keep:   # APEX-KEEPSPOT-2026-10-26: kept picks stay put; the rest must be in rank order
        _main_c = [(i, p, sc) for i, (p, sc) in enumerate(main, 1) if _ident(p) not in keep]
        _exp_c = [r for r in exp if _ident(r["p"]) not in keep]
        _pairs = [(i, (p, sc), r) for (i, p, sc), r in zip(_main_c, _exp_c)]
    else:
        _pairs = [(i, ps, r) for i, (ps, r) in enumerate(zip(main, exp), 1)]
    for i, (p, sc), r in _pairs:
        if _ident(p) == _ident(r["p"]):
            continue
        mine = _rank(flag, p)
        bar = (t.top_slots_bar(None if flag == "parlayplay" else flag) or 0) / 100.0
        if abs(mine - r["rank"]) <= TIE and (r["bar"] >= bar - 1e-9) == (_bar_value(flag, p) >= bar - 1e-9):
            continue   # a tie: either order is right
        n_bad += 1
        if n_bad > MAX_LINES_PER_BOARD:
            break
        out.append(("spot|%s|%d|%s|%s" % (flag, i, "|".join(map(str, _ident(r["p"]))), "|".join(map(str, _ident(p)))),
                    "\U0001F6A8 ORDER CHECK: %s #%d should be %s (score %.1f) but %s (%.1f) is there — review" % (
                        name, i, _short(r["p"]), 100 * r["rank"], _short(p), 100 * mine),
                    {"flag": flag, "kind": "spot", "spot": "#%d" % i, "posted": p, "should": r["p"], "detail": ""}))
    # APEX-FIXCHECKER-2026-11-02: nothing that qualifies may vanish -- every unheld qualifier is a spot or a BACKUP
    if MISSING_BACKUP_CHECK and flag != "parlayplay":
        posted = {_ident(p) for p, _ in list(main) + list(backups)} | set(held or ()) | set(keep or ())
        lim_b = getattr(t, "BOARD_BACKUPS_LIMIT", 0) or 0
        last_b = min((_rank(flag, p) for p, _ in backups), default=None)
        for r in reps:
            k = _ident(r["p"])
            if k in posted or _edge_started(r["p"]):
                continue
            if "prizepicks" in str((off_notes or {}).get(k) or "").lower():
                continue   # PrizePicks pulled or moved the line: left off on purpose
            if lim_b and len(backups) >= lim_b and (last_b is None or r["rank"] <= last_b + TIE):
                continue   # BACKUPS list is full and this pick ranks below its last line
            out.append(("missing|%s|%s" % (flag, "|".join(map(str, k))),
                        "\U0001F6A8 ORDER CHECK: %s is missing %s (score %.1f) \u2014 it qualifies and is not held, so it belongs in a numbered spot or in BACKUPS \u2014 review" % (
                            name, _short(r["p"]), 100 * r["rank"]),
                        {"flag": flag, "kind": "missing", "spot": "BACKUPS", "posted": None, "should": r["p"],
                         "detail": "qualifies, not held, not posted anywhere"}))
            break
    # BACKUPS: numbered in the board's own rank order
    if t.BACKUP_BOARD_ORDER:
        bk = [(p, _rank(flag, p)) for p, _ in backups]
        for i in range(len(bk) - 1):
            (a, ra), (b, rb) = bk[i], bk[i + 1]
            if rb > ra + TIE:
                out.append(("backup|%s|%d|%s|%s" % (flag, i + 1, "|".join(map(str, _ident(b))), "|".join(map(str, _ident(a)))),
                            "\U0001F6A8 ORDER CHECK: %s BACKUP %d should be %s (score %.1f) but %s (%.1f) is there — review" % (
                                name, i + 1, _short(b), 100 * rb, _short(a), 100 * ra),
                            {"flag": flag, "kind": "backup", "spot": "Backup %d" % (i + 1), "posted": a, "should": b,
                             "detail": ""}))
                break
    return out


def check_scores(flag, main_rows, props):
    """APEX-SCOREDISPLAY-2026-10-25: the '🏆 Score' every card shows must be the number this board
    ranked the pick by (board history rank_score). [(key, text, info)] for any pick that differs."""
    t = _t()
    out = []
    if not getattr(t, "SCORE_DISPLAY", False):
        return out
    name = BOARD_NAMES.get(flag, flag)
    for r in main_rows:
        p = (props.get(r["prop_key"]) or (None, None))[0]
        if p is None or r.get("rank_score") is None:
            continue
        shown = t.score_value("pp" if flag == "parlayplay" else flag, p)
        if abs(shown - float(r["rank_score"])) > 0.05:
            out.append(("score|%s|%s" % (flag, r["prop_key"]),
                        "\U0001F6A8 ORDER CHECK: %s #%s card shows Ranking # %.1f but the board ranked it at %.1f — review" % (
                            name, r.get("spot_no"), shown, float(r["rank_score"])),
                        {"flag": flag, "kind": "score", "spot": "#%s" % r.get("spot_no"), "detail": ""}))
    return out


# ------------------------------------------------------------------ switches + signal data
def check_switches(platform="prizepicks"):
    """[(dedupe key, alert text)] for every switch that is not as expected or can't be read."""
    out = []
    for mod, name, want, what in SWITCHES + (PP_SWITCHES if platform == "parlayplay" else []):
        try:
            m = sys.modules.get(mod) or __import__(mod)
            have = getattr(m, name)
            ok = (abs(float(have) - float(want)) < 1e-9) if isinstance(want, float) and not isinstance(want, bool) \
                else have == want
        except Exception as ex:
            out.append(("switch|%s" % name, "\U0001F6A8 ORDER CHECK: switch %s (%s) can't be read (%s) — review" % (
                name, what, type(ex).__name__)))
            continue
        if not ok:
            out.append(("switch|%s|%s" % (name, have), "\U0001F6A8 ORDER CHECK: switch %s (%s) is %r, should be %r — review" % (
                name, what, have, want)))
    return out


def check_signal_data(props):
    """The signal data the rank reads, called without the never-raise wrappers: an error here
    means signals silently count as 0. [(key, text)]."""
    out = []
    try:
        import apex_card_signals as cs
        import apex_blowout_signal as bs
    except Exception as ex:
        return [("signals|import", "\U0001F6A8 ORDER CHECK: signal code can't load (%s: %s) — ranking falls back to plain Win Prob — review" % (
            type(ex).__name__, str(ex)[:80]))]
    probes = [("ParlayPlay price data", lambda: cs._cached("pp", cs._pp_ladder)),
              ("pick data", lambda: cs._cached("picks", cs._pick_rows)),
              ("played-yesterday data", lambda: cs._cached("played", cs._played))]
    tg = [p for p in props if p.get("category") == "total_games" and str(p.get("direction") or "").upper() == "MORE"]
    for p in tg[:3]:
        probes.append(("bet365 total", lambda p=p: cs._book_for(p, cs._names(p))))
        probes.append(("blowout data", lambda p=p: bs.recent_blowouts(p)))
    errs = {}
    for label, fn in probes:
        try:
            fn()
        except Exception as ex:
            errs.setdefault(label, "%s: %s" % (type(ex).__name__, str(ex)[:60]))
    for label, why in errs.items():
        out.append(("signals|%s" % label, "\U0001F6A8 ORDER CHECK: %s is erroring (%s) — those signals count as 0 in the rank — review" % (
            label, why)))
    return out


# ------------------------------------------------------------------ one publish
def _pool(flag):
    t = _t()
    got = getattr(t, "LAST_RANK_POOLS", {}).get("pp" if flag == "parlayplay" else flag)
    if not got:
        return None
    ts, entries = got
    if (datetime.now() - ts).total_seconds() > 60 * POOL_MAX_AGE_MIN:
        return None
    out = []
    for d in entries:
        stack = [d]
        while stack:   # each slot's other lines too, so best line per match is checked
            x = stack.pop(0)
            p = x.get("p") or {}
            if p:
                out.append((p, _sc(x)))
            if flag not in ("bestdemon", "beststandard"):
                stack.extend(x.get("more") or [])
    return out


def check_publish(platform, publish_id, trigger, rows, props, post=True):
    """rows: the board history rows of one publish; props: prop_key -> (prop, apex).
    Never raises: a crash posts '🚨 ORDER CHECK could not run'. Returns the alert lines."""
    if not ORDER_GUARD_ON:
        return []
    try:
        return _check_publish(platform, publish_id, trigger, rows, props, post)
    except Exception as ex:
        _could_not_run("%s: %s" % (type(ex).__name__, str(ex)[:120]), platform, publish_id, trigger)
        traceback.print_exc()
        return None


def _check_publish(platform, publish_id, trigger, rows, props, post):
    by = {}
    for r in rows:
        if r.get("board") in BOARD_NAMES:
            by.setdefault(r["board"], []).append(r)
    found, spots, pool_src, all_props = [], 0, [], []
    for flag, rs in by.items():
        def P(r):
            p, sc = props.get(r["prop_key"], (None, None))
            return p, (sc if sc is not None else r.get("apex_score"))
        main = [P(r) for r in sorted([r for r in rs if r["spot_kind"] == "main"], key=lambda r: r["spot_no"] or 0)]
        bks = [P(r) for r in sorted([r for r in rs if r["spot_kind"] == "backup"], key=lambda r: r["spot_no"] or 0)]
        if any(p is None for p, _ in main + bks):
            raise RuntimeError("%s: a posted prop is missing its data" % flag)
        if not main:
            continue
        pool = _pool(flag)
        if pool is None:   # no fresh qualifier list: the board's own spots + BACKUPS that weren't held
            pool = bks
            pool_src.append("%s=backups" % flag)
        spots += len(main) + len(bks)
        all_props += [p for p, _ in main]
        _keep = {_ident(P(r)[0]) for r in rs if r["spot_kind"] == "main" and _kept_spot(r)}   # APEX-KEEPSPOT-2026-10-26
        _keep |= {_ident(P(r)[0]) for r in rs if r["spot_kind"] == "main" and _edge_started(P(r)[0])}   # APEX-STARTCLOCK-2026-10-27
        # APEX-FIXCHECKER-2026-11-02: what the card itself said about picks it left off or held
        _off = {}
        _held = set()
        for r in rs:
            if r["spot_kind"] not in ("off", "held"):
                continue
            _p0 = P(r)[0]
            if _p0 is None:
                continue
            if r["spot_kind"] == "held":
                _held.add(_ident(_p0))
            else:
                _off[_ident(_p0)] = "%s %s" % (r.get("why_changed") or "", r.get("reason") or "")
        found += check_board(flag, main, bks, pool, keep=_keep, off_notes=_off, held=_held)
        found += check_scores(flag, [r for r in rs if r["spot_kind"] == "main"], props)
    if not by:
        raise RuntimeError("no board rows in this publish")
    found += [(k, t, {"flag": "switches", "kind": "switch", "detail": t}) for k, t in check_switches(platform)]
    found += [(k, t, {"flag": "switches", "kind": "signals", "detail": t}) for k, t in check_signal_data(all_props)]
    day = date.today().isoformat()
    # APEX-ALERTCHAIN-2026-10-25: a test re-check (post=False) never touches the open-problem list -- the
    # 6:26 PM 10/25 recheck read board-file props without their bet365 data, flagged 3 Best Standard
    # picks, and the next real publish (#701) then posted a "fixed" card for a problem never posted
    st = _load(STATE, {}) if post else json.loads(json.dumps(_load(STATE, {})))
    sent = st.setdefault("sent", {})
    new = []
    for k, text, info in found:
        kk = day + "|" + platform + "|" + k
        if kk in sent:
            continue
        sent[kk] = datetime.now().isoformat(timespec="seconds")
        new.append((k, text, info))
    # APEX-ALERTCARDS-2026-10-25: boards with an open problem that this check finds in order -> fixed
    if "open" not in st:   # first run with cards: the problems the last check today left open
        st["open"] = _seed_open(platform, day)
    opn = st["open"]
    now_bad = {}
    for k, text, info in found:
        now_bad.setdefault(platform + "|" + info["flag"], []).append(text)
    fixed = []
    for ok_key in [b for b in list(opn) if b.split("|")[0] == platform]:
        flag = ok_key.split("|", 1)[1]
        if ok_key in now_bad or (flag != "switches" and flag not in by):
            continue
        fixed.append((flag, opn.pop(ok_key)))
        pre = day + "|" + platform + "|"   # a later repeat of the same problem posts a new card
        for kk in [kk for kk in sent if kk.startswith(pre) and kk[len(pre):].split("|")[1:2] == [flag]]:
            sent.pop(kk, None)
    for b, texts in now_bad.items():
        if b not in opn:
            opn[b] = {"since": datetime.now().isoformat(timespec="seconds"), "trigger": trigger, "items": texts[:6]}
    cutoff = (date.today() - timedelta(days=3)).isoformat()
    st["sent"] = {k: v for k, v in sent.items() if k[:10] >= cutoff}
    st["last_check"] = {"at": datetime.now().isoformat(timespec="seconds"), "platform": platform,
                        "publish_id": publish_id, "mismatches": len(found)}
    if post:
        _save(STATE, st)
    _log(platform, publish_id, trigger, sorted(by), spots, len(found), " | ".join(t for _, t, _i in found)[:2000],
         ",".join(pool_src) or "rank pool")
    if post:
        groups = {}
        for k, text, info in new:
            groups.setdefault(info["flag"], []).append((text, info))
        for flag, items in groups.items():
            _cd = problem_card(flag, items, platform, publish_id, trigger)
            if _is_pp(platform):   # APEX-PPALERTS-2026-10-29: ParlayPlay cards -> #pp-alerts
                _pp_post(_cd, " \n".join(t for t, _i in items))
            elif not _chain_problem(platform, flag, _cd):   # APEX-ALERTCHAIN-2026-10-25
                _send_card(_cd, " \n".join(t for t, _i in items))
        for flag, was in fixed:
            _cd = fixed_card(flag, was, platform, publish_id, trigger)
            if _is_pp(platform):
                _pp_post(_cd, "✅ ORDER CHECK — %s fixed" % (BOARD_NAMES.get(flag, "switches")))
            elif not _chain_fixed(platform, flag, _cd):
                _send_card(_cd, "✅ ORDER CHECK — %s fixed" % (BOARD_NAMES.get(flag, "switches")))
    print("[OrderGuard] %s publish %s: %d spots checked, %d mismatch(es), %d new alert(s), %d fixed" % (
        platform, publish_id, spots, len(found), len(new), len(fixed)))
    return [t for _, t, _i in found]


# ------------------------------------------------------------------ APEX-ALERTCARDS-2026-10-25: the cards
def _seed_open(platform, day):
    out = {}
    try:
        with open(CHECKS, newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if r["platform"] == platform and r["checked_at"][:10] == day
                    and r.get("trigger") != "recheck"]
        last = rows[-1] if rows else None
        if not last or last["mismatches"] in ("0", "-1", ""):
            return out
        for txt in last["problems"].split(" | "):
            flag = next((f for f, n in BOARD_NAMES.items() if ("ORDER CHECK: %s " % n) in txt), None)
            if flag:
                out.setdefault(platform + "|" + flag, {"since": last["checked_at"], "trigger": last["trigger"],
                                                       "items": []})["items"].append(txt)
    except Exception:
        pass
    return out


NEXT_UPDATE_MIN = 30


def _score_words(flag, p):
    """'73.3% Win Prob + 20.0 from signals = 93.3' -- the rule math behind one pick's score."""
    try:
        t = _t()
        wp, rk = 100 * t._board_prob(p), 100 * _rank(flag, p)
        if flag == "beststandard":
            return "%s: %.1f%% Win Prob, market-adjusted score %.1f" % (_short(p), wp, rk)
        sig = rk - wp
        return "%s: %.1f%% Win Prob %s %.1f from signals = **%.1f**" % (_short(p), wp, "+" if sig >= 0 else "−", abs(sig), rk)
    except Exception:
        return _short(p)


def _cause(flag, items):
    out = []
    for text, i in items:
        k = i["kind"]
        if k == "short":
            out.append("a pick left the board (pulled, started or held) and no backup moved up into the open spot"
                       + (" (Best Goblin shows every 75%+ Goblin, so it can run past 6)" if flag == "bestgoblin" else ""))
        elif k == "spot":
            try:
                gap = 100 * (_rank(flag, i["should"]) - _rank(flag, i["posted"]))
            except Exception:
                gap = None
            if gap is not None and gap < 1:
                out.append("the two scores are only %.1f apart — a small odds move between building the board and this check" % gap)
            else:
                out.append("the board was ranked with older scores — a Win Prob or a signal changed after the board was built")
        elif k == "backup":
            out.append("the BACKUPS list is not in score order — a score changed after the list was built")
        elif k == "rule":
            out.append("the posted pick breaks a rule now (%s) — its data likely changed after the board was built" % i["detail"])
        elif k == "switch":
            out.append("a ranking switch is not set the way the rules expect")
        elif k == "signals":
            out.append("a signal data source is erroring, so those signals count as 0")
        elif k == "score":   # APEX-SCOREDISPLAY-2026-10-25
            out.append("the Score shown on the card is figured differently from the number the board ranked by (display only)")
    seen = []
    for c in out:
        if c not in seen:
            seen.append(c)
    return seen


def problem_card(flag, items, platform, publish_id, trigger):
    import apex_alert_cards as C
    name = BOARD_NAMES.get(flag, "Ranking switches" if flag == "switches" else flag)
    posted, should, scores = [], [], []
    for text, i in items[:MAX_LINES_PER_BOARD + 2]:
        if i["kind"] in ("switch", "signals", "score"):   # APEX-SCOREDISPLAY-2026-10-25: score
            posted.append("❌ " + text.replace("\U0001F6A8 ORDER CHECK: ", "").replace(" — review", ""))
            continue
        sp = i.get("spot") or ""
        if i["kind"] == "short":
            posted.append("%s: nothing there — %s" % (sp, i["detail"]))
        elif i["kind"] == "rule":
            posted.append("%s: %s — %s" % (sp, _short(i["posted"]), i["detail"]))
        else:
            posted.append("%s: %s" % (sp, _short(i["posted"])))
        if i.get("should") is not None:
            should.append("%s: %s" % (sp, _short(i["should"])))
        elif i["kind"] == "rule":
            should.append("%s: a pick that passes every rule" % sp)
        for p in (i.get("should"), i.get("posted")):
            if p is not None:
                w = _score_words(flag, p)
                if w not in scores:
                    scores.append(w)
    nxt = "Boards update about every %d min — this usually fixes itself on the next update. " \
          "I'll post ✅ ORDER CHECK — %s fixed when it does." % (NEXT_UPDATE_MIN, name)
    if flag == "switches":
        nxt = "This does not fix itself — a switch or data source needs a look. I'll post ✅ when it reads right again."
    fields = [("❌ Posted", posted), ("✅ Should be", should or ["—"]), ("\U0001F4CA Scores (rule math)", scores or ["—"]),
              ("\U0001F50E Likely cause", ["• " + c for c in _cause(flag, items)] or ["—"]),
              ("\U0001F6E0 What happens next", nxt)]
    desc = "The posted %s is not in the order the live rules give." % name if flag != "switches" else \
        "Something the board order depends on is off."
    desc += "\nChecked after the %s publish (#%s)." % (trigger, publish_id)
    return C.make("\U0001F6A8 ORDER CHECK — %s" % name, C.RED, desc, fields,
                  foot=C.footer("Order Guard · %s" % ("ParlayPlay" if platform == "parlayplay" else "PrizePicks")))


def fixed_card(flag, was, platform, publish_id, trigger):
    import apex_alert_cards as C
    name = BOARD_NAMES.get(flag, "Ranking switches" if flag == "switches" else flag)
    since = C.clock((was or {}).get("since")) if (was or {}).get("since") else "earlier"
    items = [x.replace("\U0001F6A8 ORDER CHECK: ", "").replace(" — review", "") for x in (was or {}).get("items") or []]
    return C.make("✅ ORDER CHECK — %s fixed" % name, C.GREEN,
                  "The %s update (#%s) has every %s spot in the right order again." % (trigger, publish_id, name),
                  [("\U0001F552 Problem first seen", since), ("\U0001F4DD What was wrong", items[:4] or ["—"])],
                  foot=C.footer("Order Guard · %s" % ("ParlayPlay" if platform == "parlayplay" else "PrizePicks")))


def _chain_key(platform, flag):
    return "order|%s|%s|%s" % (date.today().isoformat(), platform, flag)


def _chain_problem(platform, flag, card):
    """APEX-ALERTCHAIN-2026-10-25: the problem card is the start of a chain -- its 'what happens next'
    says when the next update is, and the fix (✅ ORDER CHECK — fixed) edits this same card."""
    if DRY:
        return False
    try:
        import apex_alert_chain as AC
        if not AC.CHAINS_ON:
            return False
        e = dict(card)
        e["fields"] = [f for f in (card.get("fields") or []) if not f["name"].startswith("\U0001F6E0")]
        at, _w = AC.next_board_check()
        if flag == "switches":
            nxt = "This does not fix itself — a switch or data source needs a look. This card turns ✅ when it reads right again."
        else:
            nxt = ("Boards update about every %d min (next about %s) — this usually fixes itself then, and this card "
                   "turns ✅ ORDER CHECK — %s fixed by itself." % (NEXT_UPDATE_MIN, AC.hm(at), BOARD_NAMES.get(flag, flag)))
        k = _chain_key(platform, flag)
        if AC.get(k):
            return AC.replace(k, e, nxt, now="Still off after the %s update" % AC.hm())
        return AC.soft_start(k, e, nxt, check_mute=False)
    except Exception as ex:
        print("[OrderGuard] chain card failed (%s), posting it" % type(ex).__name__)
        return False


def _chain_fixed(platform, flag, card):
    if DRY:
        return False
    try:
        import apex_alert_chain as AC
        k = _chain_key(platform, flag)
        if not AC.CHAINS_ON or not AC.get(k):
            return False
        return AC.finish(k, card, note="fixed %s" % AC.hm())
    except Exception as ex:
        print("[OrderGuard] chain fix failed (%s), posting it" % type(ex).__name__)
        return False


def _send_card(embed, text):
    if DRY:
        import apex_alert_cards as C
        print("---- would post to #alerts ----\n" + C.to_text(embed))
        return True
    try:
        import apex_alert_cards as C
        if C.ALERT_CARDS_ON:
            return C.post(embed, "alerts")
    except Exception as ex:
        print("[OrderGuard] card failed (%s), sending text" % type(ex).__name__)
    return _send(text)


def _log(platform, publish_id, trigger, boards, spots, n, problems, pool_src):
    os.makedirs(DIR, exist_ok=True)
    new = not os.path.exists(CHECKS)
    with open(CHECKS, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CHECK_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"checked_at": datetime.now().isoformat(timespec="seconds"), "platform": platform,
                    "publish_id": publish_id, "trigger": trigger, "boards": " ".join(boards),
                    "spots_checked": spots, "mismatches": n, "problems": problems, "pool_source": pool_src})


EMPTY_OK = True   # APEX-REALSKIP-2026-11-14 (owner, "Go real reason"): an empty board is not a checker failure. Undo: False


def _could_not_run(reason, platform="", publish_id="", trigger=""):
    # APEX-REALSKIP-2026-11-14: a publish with no board rows (every board empty) has nothing to check. It is logged as
    # "board empty, nothing to check" and says so once a day in plain words -- no 🚨 "could not run".
    empty = EMPTY_OK and str(reason).startswith("no board rows")
    try:
        _log(platform, publish_id, trigger, [], 0, 0 if empty else -1, "board empty, nothing to check" if empty else "could not run: " + reason, "")
    except Exception:
        pass
    day = date.today().isoformat()
    st = _load(STATE, {})
    key = day + ("|empty" if empty else "|crash|" + reason[:60])
    if empty:
        if key in st.setdefault("sent", {}):
            return
        st["sent"][key] = datetime.now().isoformat(timespec="seconds")
        try:
            _save(STATE, st)
        except Exception:
            pass
        _msg = "ORDER CHECK: board empty, nothing to check (%s publish). Nothing is wrong with the order." % (platform or "board")
        if _is_pp(platform):
            _pp_post(None, _msg)
        else:
            _send(_msg)
        return
    if key in st.setdefault("sent", {}):
        return
    st["sent"][key] = datetime.now().isoformat(timespec="seconds")
    try:
        _save(STATE, st)
    except Exception:
        pass
    _msg = "\U0001F6A8 ORDER CHECK could not run: %s (%s publish) — the board order was not checked" % (
        reason, platform or "board")
    if _is_pp(platform):   # APEX-PPALERTS-2026-10-29
        _pp_post(None, _msg)
    else:
        _send(_msg)


# ------------------------------------------------------------------ 9 AM line
def daily_line(day=None):
    """'✅ Order check: N board updates yesterday, all in correct order' (or the mismatch count)."""
    day = day or (date.today() - timedelta(days=1)).isoformat()
    rows = []
    try:
        with open(CHECKS, newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if r["checked_at"][:10] == day]
    except FileNotFoundError:
        pass
    ok = [r for r in rows if r["mismatches"] not in ("-1", "")]
    crashed = len(rows) - len(ok)
    bad = [r for r in ok if int(r["mismatches"] or 0) > 0]
    # publishes the checker never saw (it should see every one)
    unchecked = 0
    try:
        import sqlite3
        with open(CHECKS, newline="") as fh:
            since = min((r["checked_at"] for r in csv.DictReader(fh)), default="9999")
        c = sqlite3.connect(os.path.join(HERE, "data", "board_history.sqlite"), timeout=30)
        pubs = {str(x[0]) for x in c.execute(   # only publishes after the checker went live
            "SELECT publish_id FROM publishes WHERE origin = 'live' AND platform IN ('prizepicks', 'parlayplay') "
            "AND substr(published_at, 1, 10) = ? AND published_at >= ?", (day, since[:19]))}
        c.close()
        unchecked = len(pubs - {r["publish_id"] for r in rows})
    except Exception:
        pass
    if not rows and not unchecked:
        return "ℹ️ Order check: no board updates yesterday"
    tail = ""
    if crashed:
        tail += " · \U0001F6A8 could not run %d time%s" % (crashed, "" if crashed == 1 else "s")
    if unchecked:
        tail += " · ⚠️ %d update%s not checked" % (unchecked, "" if unchecked == 1 else "s")
    if not bad:
        return "✅ Order check: %d board update%s yesterday, all in correct order%s" % (
            len(ok), "" if len(ok) == 1 else "s", tail)
    n = sum(int(r["mismatches"]) for r in bad)
    return "\U0001F6A8 Order check: %d board update%s yesterday, %d had a problem (%d mismatch%s, see #alerts)%s" % (
        len(ok), "" if len(ok) == 1 else "s", len(bad), n, "" if n == 1 else "es", tail)


# ------------------------------------------------------------------ CLI: re-check a stored publish
def recheck_stored(publish_id=None):
    """Re-check a stored publish from board_history rows, using the props as they are scored NOW
    (current board file). For testing only: prints, posts nothing."""
    import sqlite3
    t = _t()
    import apex_board_history as bh
    c = sqlite3.connect(bh.DB)
    c.row_factory = sqlite3.Row
    if publish_id is None:
        publish_id = c.execute("SELECT max(publish_id) FROM publishes WHERE origin='live' AND platform='prizepicks'").fetchone()[0]
    rows = [dict(r) for r in c.execute("SELECT * FROM history WHERE publish_id = ?", (publish_id,))]
    c.close()
    picks, _ = t.load_current_board()
    props = {}
    rec = t.records()
    for p in picks:
        try:
            sc = t.apex_score(p.get("prob_cal", p.get("prob")), p["category"], bool(p.get("bo5")), rec)[0]
        except Exception:
            sc = None
        props[bh.prop_key(p)] = (p, sc)
    rows = [r for r in rows if r["prop_key"] in props or r["spot_kind"] in ("held", "off")]
    return _check_publish("prizepicks", publish_id, "recheck", rows, props, post=False)


if __name__ == "__main__":
    if "--line" in sys.argv:
        print(daily_line())
    else:
        DRY = True
        out = recheck_stored(int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None)
        print("\n".join(out or []) or "all in correct order")
