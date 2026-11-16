#!/usr/bin/env python3
"""
APEX-BLOWOUT-2026-10-22 (owner, after Ann Li o17.5 ended 6-3 6-1 and
Siniakova o17.5 ended 4-6 1-6 on 10/22: "we want the matches to be
competitive", then "Go"): Total Games MORE picks lose to blowouts, not to close
misses -- 26 of the 31 board losses since Oct 10 ended in 17 games or fewer.

10/22 study, all 215 graded 70%+ Total Games Goblin MORE props since Oct 9:
  bet365 cushion 3+ games (and no blowout flag)  106-26 (80%)
  bet365 cushion under 3 games                     24-18 (57%)
  WTA, both players' last 10: over 25% blowouts    28-11 (72%)
  everything, no rule                             158-57 (73%)

Three things, all switchable:
  1. CARD LINE (display only): "🏟 Last 10 matches each ..." on every Total
     Games MORE card -- how many of each player's last 10 matches ended in 17
     games or fewer, in plain words, with the record in brackets.
  2. BACKUPS MOVE (apex_edge_tracker.LOW_CUSHION_BACKUPS): a Total Games Goblin
     MORE prop with under 3 games of room under bet365's line takes no numbered
     slot; it is listed under 🔁 BACKUPS / Held Back with the reason. /allprops
     still shows it. Nothing is hidden.
  3. PAPER TEST (background only): every /fetch and 5 PM publish records each
     70%+ Total Games Goblin MORE prop as KEEP (3+ games of room) or DROP (under
     3, or WTA with over 25% recent blowouts). The daily status grades it; after
     FINISH_GRADED graded props a TEST FINISHED card posts once to #system and
     #alerts.

  python3 apex_blowout_signal.py            # running record
  python3 apex_blowout_signal.py --log-now  # record today's props now
  python3 apex_blowout_signal.py --lines    # print the card lines for today's TG props
"""
import json, os, re, sqlite3, sys, time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
WAREHOUSE = os.path.join(HERE, "data", "warehouse", "tennis_warehouse.sqlite")
PROPS_DB = os.path.join(HERE, "data", "props_db.sqlite")
LOG = os.path.join(HERE, "data", "logs", "blowout_papertest.json")
START = "2026-10-22"
MIN_CUSHION = 3.0        # games of room under bet365's main line
BLOWOUT_GAMES = 17       # a match of 17 games or fewer = blowout (6-3 6-2 is 17)
RECENT_N = 10            # last N completed matches per player
RECENT_WARN = 0.25       # both players' average blowout share above this = ⚠️
FINISH_GRADED = 100
TTL = 900
_cache = {}


def _norm(s):
    import apex_card_signals as cs
    return cs._norm(s)


def _names(p):
    import apex_card_signals as cs
    return cs._names(p)


def _is_tg_more(p):
    return p.get("category") == "total_games" and str(p.get("direction") or "").upper() == "MORE"


def cushion(p):
    """(bet365 main line, games of room) for a Total Games prop, or None."""
    try:
        if p.get("category") != "total_games":
            return None
        import apex_card_signals as cs
        bk = cs.book_total(p)   # APEX-BET365-THREEPART-2026-10-22: names + match date, shared reader
        if not bk:
            return None
        return float(bk), float(bk) - float(p.get("line"))
    except Exception:
        return None


def _history():
    """APEX-PLAYERDATA-2026-10-31: the filled match history when its switch is on, else the warehouse."""
    try:
        import apex_player_history as _ph
        return _ph.path(WAREHOUSE)
    except Exception:
        return WAREHOUSE


def _games_from_score(score):
    t = 0
    for a, b in re.findall(r"(\d+)-(\d+)", score or ""):
        a, b = int(a), int(b)
        if max(a, b) >= 10:      # tiebreak points, not games
            continue
        t += a + b
    return t or None


def recent(name, before):
    """[total games] of the player's last RECENT_N completed matches before `before`."""
    key = ("recent", name, before)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    out = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % _history(), uri=True)
        rows = con.execute(
            "select total_games, score, player_a, player_b, match_date, retired from matches "
            "where (player_a = ? or player_b = ?) and match_date < ? order by match_date desc limit 60",
            (name, name, before)).fetchall()
        con.close()
        seen = set()
        for tg, score, a, b, md, ret in rows:
            k = (frozenset([a, b]), md)
            if k in seen or ret:
                continue
            seen.add(k)
            tg = tg or _games_from_score(score)
            if tg and 12 <= tg <= 45:
                out.append(int(tg))
            if len(out) >= RECENT_N:
                break
    except Exception:
        out = []
    _cache[key] = (time.time(), out)
    return out


def _surname(n):
    w = str(n).split()
    if len(w) >= 3 and w[-2].lower() in ("van", "de", "di", "del", "da", "le", "der", "den"):
        return " ".join(w[-2:])
    return w[-1] if w else str(n)


def recent_blowouts(p):
    """[(surname, blowouts, matches)] for both players, or [] if either is unknown."""
    try:
        md = str(p.get("match_date") or "")[:10] or datetime.now().date().isoformat()
        out = []
        for n in _names(p):
            h = recent(n, md)
            if len(h) < 3:
                return []
            out.append((_surname(n), sum(1 for g in h if g <= BLOWOUT_GAMES), len(h)))
        return out
    except Exception:
        return []


def blowout_share(p):
    rb = recent_blowouts(p)
    if len(rb) != 2:
        return None
    return sum(b / float(n) for _, b, n in rb) / 2.0


def tour(p):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % _history(), uri=True)
        for n in _names(p):
            r = con.execute("select tour from matches where player_a = ? or player_b = ? order by match_date desc limit 1",
                            (n, n)).fetchone()
            if r and r[0]:
                con.close()
                return r[0]
        con.close()
    except Exception:
        pass
    return None


def verdict(p):
    """('KEEP' | 'DROP cushion' | 'DROP blowouts' | 'KEEP (no data)', games of room, blowout share)."""
    c = cushion(p)
    gap = c[1] if c else None
    sh = blowout_share(p)
    if gap is not None and gap < MIN_CUSHION:
        return "DROP cushion", gap, sh
    if sh is not None and sh > RECENT_WARN and tour(p) == "WTA":
        return "DROP blowouts", gap, sh
    return ("KEEP" if gap is not None else "KEEP (no data)"), gap, sh


# APEX-STANDARDHOLD-2026-10-23 (owner, "Put on best standards as well and all picks"):
# the hold covers Standard and Demon Total Games MORE too, so every board (Best Standard and
# Best Demon included) holds the same picks. The record shown is that type's own, since 10/10.
_LOW_CUSHION_RECORD = {
    "goblin": "under 3 games of room hit 57%",
    "parlayplay": "under 3 games of room hit 57%",
    "standard": "Best Standard Total Games MORE under 3 games of room hit 10 of 23 since 10/10",
    "demon": "Demon Total Games MORE under 3 games of room hit 2 of 10 since 10/10",
    "red_demon": "Demon Total Games MORE under 3 games of room hit 2 of 10 since 10/10",
}


def low_cushion_reason(p):
    """Why a Total Games MORE prop takes no numbered slot, or None. Never raises."""
    try:
        # APEX-PPMIRROR-B-2026-10-23: ParlayPlay picks (prop_type "parlayplay") take the same hold
        rec = _LOW_CUSHION_RECORD.get(str(p.get("prop_type") or "").lower())
        if not _is_tg_more(p) or not rec:
            return None
        c = cushion(p)
        if not c or c[1] >= MIN_CUSHION:
            return None
        who = "bet365's"   # APEX-SGOBACKUP-2026-10-31: say whose total it is
        try:
            import apex_card_signals as cs
            import apex_top3_no175_papertest as _t
            if _t.bet365_total(cs._names(p), str(p.get("match_date") or "")[:10])[1] == "sgo":
                who = "SportsGameOdds'"
        except Exception:
            pass
        if c[1] < 0:   # Demon lines can sit above bet365's
            return "line is %g game%s above %s %g -- blowout risk (%s)" % (
                -c[1], "" if c[1] == -1 else "s", who, c[0], rec)
        return "only %g game%s of room under %s %g -- blowout risk (%s)" % (
            c[1], "" if c[1] == 1 else "s", who, c[0], rec)
    except Exception:
        return None


def lines(p):
    """The 🏟 card line for a Total Games MORE prop. Never raises; [] if unknown."""
    try:
        if not _is_tg_more(p):
            return []
        rb = recent_blowouts(p)
        if len(rb) != 2:
            return []
        sh = sum(b / float(n) for _, b, n in rb) / 2.0
        who = ", ".join("%s %d" % (s, b) for s, b, _ in rb)
        if sh > RECENT_WARN:
            tail = "\u26a0\ufe0f blowout risk (over 25% between them hit 72%; 25% or less hit 80%)"
            try:   # APEX-BLOWOUTDOUBLE-2026-10-25
                import apex_card_signals as _cs
                if _cs.BLOWOUT_DOUBLE:
                    tail += " -- counts double: \u221210 rank points"
            except Exception:
                pass
        else:
            tail = "\u2705 competitive lately (25% or less between them hit 80%)"
        return ["\U0001F3DF\ufe0f Blowouts in their last %d matches each (%d games or fewer): %s -- %d%% between them %s" % (
            rb[0][2], BLOWOUT_GAMES, who, round(100 * sh), tail)]
    except Exception:
        return []


# ----------------------------------------------------------------------------- paper test
def _load():
    try:
        with open(LOG) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _key(p):
    import apex_card_signals as cs
    pair = "|".join(sorted(cs._norm(x) for x in cs._names(p)))
    return "%s|%s|%s" % (str(p.get("match_date") or "")[:10], pair, p.get("line"))


def log_today(picks=None, source="", today=None):
    """Adds today's 70%+ Total Games Goblin MORE props that pass the board bar
    (first sighting wins). Never raises."""
    try:
        import apex_edge_tracker as t
        today = today or datetime.now().date().isoformat()
        if today < START:
            return 0
        if picks is None:
            picks, _ = t.load_current_board()
        rec = t.records()
        data = _load()
        day = data.setdefault(today, {})
        added = 0
        for p in picks:
            if not _is_tg_more(p) or str(p.get("prop_type") or "").lower() != "goblin":
                continue
            if t._is_unplayable(p) or t._pub_match_started(p):
                continue
            st, _why, _sc = t._pre_board_reason(p, rec)
            if st != "eligible":
                continue
            k = _key(p)
            if k in day:
                continue
            v, gap, sh = verdict(p)
            day[k] = {"logged_at": datetime.now().isoformat(timespec="seconds"), "source": source,
                      "match_date": str(p.get("match_date") or "")[:10], "names": _names(p),
                      "line": float(p.get("line")), "prob": round(float(t._board_prob(p)), 4),
                      "room": None if gap is None else round(gap, 1),
                      "blowout_share": None if sh is None else round(sh, 3), "verdict": v}
            added += 1
        if added:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            tmp = LOG + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=1, default=str)
            os.replace(tmp, LOG)
        return added
    except Exception as e:
        print("[BlowoutPaperTest] log skipped (%s): %s" % (type(e).__name__, e))
        return 0


def _results():
    out = {}
    try:
        import apex_card_signals as cs
        con = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True)
        for pl, opp, md, line, res, actual in con.execute(
                "select player, opponent, match_date, line_last, result, actual from projections "
                "where category = 'total_games' and odds_type = 'goblin' and result in ('more', 'less') and match_date >= ?",
                (START,)):
            names = [x.strip() for x in str(opp).replace(" vs ", " v ").split(" v ")] if pl == "match" else [pl, opp]
            pair = "|".join(sorted(cs._norm(x) for x in names))
            out["%s|%s|%s" % (md, pair, float(line))] = (res == "more", actual)
        con.close()
    except Exception:
        pass
    return out


def _tally():
    data = _load()
    if not data:
        return None, 0, 0
    R = _results()
    T = {}
    total = graded = 0
    for day, props in data.items():
        for k, v in props.items():
            total += 1
            r = R.get("%s|%s|%s" % (v["match_date"], k.split("|", 1)[1].rsplit("|", 1)[0], float(v["line"])))
            if r is None:
                continue
            graded += 1
            g = v["verdict"].replace(" (no data)", "")
            T.setdefault(g, [0, 0, 0])
            T[g][0 if r[0] else 1] += 1
            if not r[0] and r[1] is not None and float(r[1]) <= BLOWOUT_GAMES:
                T[g][2] += 1
    return T, total, graded


def _r(x):
    n = x[0] + x[1]
    return ("%d-%d (%.0f%%)" % (x[0], x[1], 100.0 * x[0] / n)) if n else "--"


def summary():
    T, total, graded = _tally()
    if T is None:
        return []
    L = ["Blowout test since %s: %d Total Games Goblin MORE props logged, %d graded (card line + backups move live; 3+ room rule not live)"
         % (START, total, graded)]
    if not graded:
        return L
    for g, label in (("KEEP", "KEEP: 3+ games of room, no blowout flag"), ("DROP cushion", "DROP: under 3 games of room"),
                     ("DROP blowouts", "DROP: WTA, over 25% recent blowouts")):
        if g in T:
            L.append("%s: %s · %d of the losses were blowouts" % (label, _r(T[g]), T[g][2]))
    return L


def finish_check():
    T, total, graded = _tally()
    if not T or graded < FINISH_GRADED:
        return False, None
    keep = T.get("KEEP", [0, 0, 0]); dc = T.get("DROP cushion", [0, 0, 0]); db = T.get("DROP blowouts", [0, 0, 0])
    rk = keep[0] / float(keep[0] + keep[1]) if keep[0] + keep[1] else 0
    drop = [dc[0] + db[0], dc[1] + db[1]]
    rd = drop[0] / float(sum(drop)) if sum(drop) else 0
    if sum(drop) < 15:
        verdict_ = "INCONCLUSIVE -- only %d dropped props graded" % sum(drop)
    elif rk - rd >= 0.05:
        verdict_ = "PASSED -- kept props beat dropped props by %.0f pts" % (100 * (rk - rd))
    else:
        verdict_ = "FAILED -- no real gap between kept and dropped"
    rows = [("Test", "TG Goblins 3+ games of room only (since %s, %d graded props)" % (START, graded)),
            ("KEEP: 3+ room, no blowout flag", _r(keep)), ("DROP: under 3 games of room", _r(dc)),
            ("DROP: WTA recent blowouts over 25%", _r(db)), ("Verdict", verdict_),
            ("Decision", "make 3+ games of room a rule for Total Games Goblins / keep it as a card line and backups only / keep testing")]
    return True, rows


if __name__ == "__main__":
    if "--log-now" in sys.argv:
        print("added %d" % log_today(source="manual-cli"))
    if "--lines" in sys.argv:
        import apex_edge_tracker as t
        picks, _ = t.load_current_board()
        for p in picks:
            if _is_tg_more(p) and t._board_prob(p) >= 0.70:
                print(p.get("opponent"), p.get("line"), p.get("prop_type"), verdict(p))
                for x in lines(p):
                    print("   " + x)
                lc = low_cushion_reason(p)
                if lc:
                    print("   held back: " + lc)
    print("\n".join(summary()) or "nothing logged yet")
