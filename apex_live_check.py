#!/usr/bin/env python3
"""APEX-LIVECHECK-2026-11-01 (owner, "Go live check"): compares PrizePicks' latest full pull with what Apex
kept. Posts one change-only #alerts line naming live lines dropped with no reason, and counts lines whose match is
priced but which were never scored. Changes no pick. Never raises."""
import csv, json, os
from datetime import datetime, timedelta
HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "data", "logs", "fetch_live_board.json")
PICKS = os.path.join(HERE, "apex_engine_picks_log_v1.csv")
STATE = os.path.join(HERE, "data", "logs", "live_check_state.json")
MAIN = ("total_games", "games_won", "aces", "fantasy_score", "dfs", "breaks", "total_sets")
MAX_AGE_MIN = 60
UNSCORED_STEP = 15
# APEX-LIVECHECKDAY-2026-11-06 (owner, "Go live check day"): only compare lines for the same board day as the
# published picks, so tomorrow's matches are not called "dropped". Set False to undo.
LIVE_CHECK_SAME_DAY = True


def live_keys():
    """{(category, name, line, type)} from the newest full pull, or None if missing or old."""
    try:
        with open(LIVE) as fh:
            d = json.load(fh)
        if datetime.now() - datetime.fromisoformat(d["ts"]) > timedelta(minutes=MAX_AGE_MIN):
            return None
        return {(c, n, float(l), t) for c, n, l, t in d["props"]}
    except Exception:
        return None


def _names(p):
    pl = str(p.get("player") or "").strip().lower()
    return [x.strip() for x in str(p.get("opponent") or "").lower().split(" v ")] if pl == "match" else [pl]


def is_live(p, keys=None):
    """True when PrizePicks' latest pull still offers this exact line and type. False if unknown."""
    keys = live_keys() if keys is None else keys
    if not keys:
        return False
    try:
        t = str(p.get("prop_type") or "standard").strip().lower()
        t = "demon" if t == "red_demon" else t
        return any((p.get("category"), n, float(p.get("line")), t) in keys for n in _names(p))
    except Exception:
        return False


def _exact_live(r):
    """True unless the saved pull (with opponents) shows this pair isn't offering this line. Never raises."""
    try:
        import apex_two_pulls as _tp
        new, _ = _tp._sets()
        return new is None or _tp._pkey(r) in new
    except Exception:
        return True


def compare(kept):
    """(live lines scored 70+ that are missing from kept, count of unscored lines on priced matches)."""
    keys = live_keys()
    if not keys:
        return [], 0, []
    since = (datetime.now() - timedelta(days=4)).isoformat()
    seen, priced, best = set(), set(), {}
    with open(PICKS, newline="", errors="ignore") as fh:
        for r in csv.DictReader(fh):
            if (r.get("ts") or "") < since:
                continue
            ns = _names(r)
            try:
                line = float(r.get("line"))
            except (TypeError, ValueError):
                continue
            t = str(r.get("prop_type") or "standard").lower()
            for n in ns:
                seen.add((r.get("category"), n, line, t))
                priced.add(n)
                best[(r.get("category"), n, line, t)] = r
    keep = set()
    for p in kept:
        t = str(p.get("prop_type") or "standard").lower()
        for n in _names(p):
            keep.add((p.get("category"), n, float(p.get("line")), t))
    days = {str(p.get("match_date"))[:10] for p in kept if p.get("match_date")} if LIVE_CHECK_SAME_DAY else set()
    dropped, unscored, ex = [], 0, []
    done = set()
    for k in keys:
        if k[0] not in MAIN:
            continue
        r = best.get(k)
        if r is None:
            if k[1] in priced:
                unscored += 1
                if len(ex) < 6:
                    ex.append("%s %s %g" % (k[1].title(), k[0].replace("_", " "), k[2]))
            continue
        if days and str(r.get("match_date") or "")[:10] not in days:   # APEX-LIVECHECKDAY-2026-11-06
            continue
        try:
            pr = float(r.get("prob_cal") or r.get("prob") or 0)
        except ValueError:
            pr = 0
        # APEX-LIVECHECKFIX-2026-11-01: the pull list has names only; the same player's old match with the same line
        # is not a dropped line. Only the exact two players count.
        if not _exact_live(r):
            continue
        try:   # APEX-LIVECHECKSTARTED-2026-11-02: a started match leaves the boards on purpose
            from datetime import timezone as _tz
            _st = str(r.get("slip_start_utc") or "")
            if _st and datetime.fromisoformat(_st.replace("Z", "+00:00")) <= datetime.now(_tz.utc):
                continue
        except Exception:
            pass
        try:   # APEX-THINDQ-2026-11-02: thin players' stat props are off on purpose
            import apex_player_checks as _pcq
            if _pcq.disqualified(r):
                continue
        except Exception:
            pass
        try:   # a banned player's match is off on purpose
            import apex_ban_list as _abl
            if _abl.is_banned(r):
                continue
        except Exception:
            pass
        if k not in keep and pr >= 0.70 and str(r.get("playable") or "1") != "0" and id(r) not in done:
            done.add(id(r))
            dropped.append("%s %s %g" % (str(r.get("opponent")) if r.get("player") == "match" else r.get("player"),
                                         k[0].replace("_", " "), k[2]))
    return sorted(set(dropped)), unscored, ex


def run(kept):
    """Called after each publish. Never raises."""
    try:
        dropped, unscored, ex = compare(kept)
        try:
            prev = json.load(open(STATE))
        except Exception:
            prev = {}
        msg = []
        if dropped and set(dropped) != set(prev.get("dropped") or []):
            msg.append("🚨 LIVE CHECK: %d line(s) PrizePicks still offers at 70%%+ were dropped with no reason: %s"
                       % (len(dropped), "; ".join(dropped[:8])))
        if abs(unscored - int(prev.get("unscored") or 0)) >= UNSCORED_STEP:
            msg.append("🔎 LIVE CHECK: %d line(s) on priced matches are not scored yet%s"
                       % (unscored, (" (like %s)" % ", ".join(ex[:4])) if ex else ""))
        with open(STATE + ".tmp", "w") as fh:
            json.dump({"dropped": dropped, "unscored": unscored if msg or "unscored" not in prev else prev["unscored"],
                       "at": datetime.now().isoformat(timespec="seconds")}, fh)
        os.replace(STATE + ".tmp", STATE)
        if msg:
            import apex_ops
            apex_ops.send_discord("alerts", "\n".join(msg))
        return dropped, unscored
    except Exception as e:
        print("[LiveCheck] skipped (%s: %s)" % (type(e).__name__, e))
        return [], 0
