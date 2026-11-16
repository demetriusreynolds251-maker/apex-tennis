#!/usr/bin/env python3
"""APEX-STANDARDPAPER-2026-10-31 (owner, "Go standard line paper"). Paper only: never touches a board.
When a Goblin or Demon Total Games line first posts, the first PrizePicks Standard line is the total. Win chance is
the saved SGO odds (Pinnacle, else 2+ of Novig/Polymarket/Kalshi/Circa), else player strength. Runs the Total Games
signals, alerts #alerts, grades from props_db. Local files only, no API calls.
Test: _backups/own_line_test_2026-10-31/report.md. Undo: STANDARD_LINE_PAPER = False, restart scheduler."""
import glob
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

STANDARD_LINE_PAPER = True
START_UTC = "2026-10-31T20:45:00+00:00"   # lines first seen after this; earlier ones were not caught at first post
HERE = os.path.dirname(os.path.abspath(__file__))
PROPS_DB = os.path.join(HERE, "data", "props_db.sqlite")
SNAP_DIR = os.path.join(HERE, "data", "sgo_archive", "tennis_snapshots")
LOG = os.path.join(HERE, "data", "logs", "standard_line_paper.json")
CT = ZoneInfo("America/Chicago")
MIN_WIN, MIN_ROOM, SIGNAL_POINTS, SNAP_MAX_HOURS = 70.0, 3.0, 5, 3
FINISH_DAY, FINISH_GRADED, ODDS_END = "2026-11-14", 40, "2026-11-06"
BOOKS = ("novig", "polymarket", "kalshi", "circa")


def _load():
    try:
        with open(LOG) as fh:
            return json.load(fh)
    except Exception:
        return {"picks": {}}


def _save(d):
    tmp = LOG + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh)
    os.replace(tmp, LOG)


def _last(n):
    import apex_card_signals as cs
    t = cs._norm(n).split()
    return t[-1] if t else ""


def _prob(o):
    try:
        o = float(o)
    except (TypeError, ValueError):
        return None
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def _fair(h, a, book):
    x = _prob(((h.get("byBookmaker") or {}).get(book) or {}).get("odds"))
    y = _prob(((a.get("byBookmaker") or {}).get(book) or {}).get("odds"))
    return x / (x + y) if x and y else None


def _snapshot():
    """{frozenset(last names): (home last name, home win chance, source, league)} from the newest saved snapshot."""
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "*", "*.json")))
    if not files or datetime.now().timestamp() - os.path.getmtime(files[-1]) > SNAP_MAX_HOURS * 3600:
        return {}
    out = {}
    with open(files[-1]) as fh:
        events = json.load(fh).get("events", [])
    for e in events:
        s = e.get("status") or {}
        if s.get("started") or s.get("cancelled"):
            continue
        o = e.get("odds") or {}
        h, a = o.get("points-home-game-ml-home"), o.get("points-away-game-ml-away")
        if not h or not a:
            continue
        hn, an = e["teams"]["home"]["names"]["long"], e["teams"]["away"]["names"]["long"]
        p, src = _fair(h, a, "pinnacle"), "Pinnacle"
        if p is None:
            v = [x for x in (_fair(h, a, b) for b in BOOKS) if x is not None]
            p, src = (sum(v) / len(v), "2+ books") if len(v) >= 2 else (None, None)
        if p is not None:
            out[frozenset((_last(hn), _last(an)))] = (_last(hn), p, src, e.get("leagueID"))
    return out


def _strength(a, b):
    import apex_top3_no175_papertest as t3
    e = t3._rule_ctx()["elo"]
    x, y = e.get(t3._norm(a)), e.get(t3._norm(b))
    return 1 / (1 + 10 ** ((y - x) / 400.0)) if x and y else None


def _tour(a, b):
    import apex_blowout_signal as bs
    t = bs.tour(dict(player="match", opponent="%s v %s" % (a, b)))
    return str(t or "").upper() == "WTA"


def _signals(p, total, fav):
    """(net, facts) the way the card counts Total Games MORE, with the Standard line as the book total."""
    import apex_blowout_signal as bs
    import apex_card_signals as cs
    names = cs._names(p)
    good = bad = 0
    facts = []
    m = cs._cached("pp", cs._pp_ladder).get((frozenset(cs._norm(x) for x in names), float(p["line"])))
    if m:
        good += m <= 1.15
        bad += m > 1.30
        facts.append("price %.2fx" % m)
    room = total - float(p["line"])
    good += room >= MIN_ROOM
    bad += room < MIN_ROOM
    rb = bs.recent_blowouts(p)
    if len(rb) == 2:
        if sum(b / float(n) for _, b, n in rb) / 2.0 > bs.RECENT_WARN:
            bad += 2 if cs.BLOWOUT_DOUBLE else 1
            facts.append("blowout risk")
        else:
            good += 1
            facts.append("competitive lately")
    if fav:
        v = cs.fav_verdict(fav)
        if v == -1 and cs.FAV_ROOM_RULE and room >= cs.FAV_ROOM_MIN:   # APEX-FAVROOM-2026-10-31
            v = 0
        good += v == 1
        bad += v == -1
    played = cs._cached("played", cs._played)
    y = (datetime.fromisoformat(p["match_date"]) - timedelta(days=1)).date().isoformat()
    if any(played.get((cs._norm(n), y)) for n in names):
        good += 1
        facts.append("played yesterday")
    return good - bad, facts


def _banned(a, b):
    try:
        with open(os.path.join(HERE, "data", "banned_players.json")) as fh:
            ban = {_last(x["name"]) for x in json.load(fh).get("players", [])}
        return _last(a) in ban or _last(b) in ban
    except Exception:
        return False


def score_new(d, since=START_UTC):
    """Scores lines first seen since START_UTC that are not in the log. Returns the new picks that pass."""
    import apex_prop_engine as eng
    import apex_top3_no175_papertest as t3
    con = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True)
    con.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    rows = con.execute("select * from projections where category='total_games' and first_seen>=? and start_utc>? "
                       "and player is not null and opponent is not null", (since, now)).fetchall()
    std = {}
    for r in con.execute("select player,opponent,match_date,line_first,first_seen from projections where category='total_games' "
                         "and odds_type='standard' and start_utc>? order by first_seen desc", (now,)):
        std[(frozenset((_last(r["player"]), _last(r["opponent"]))), r["match_date"])] = r["line_first"]
    con.close()
    snap = _snapshot()
    out = []
    for r in rows:
        kind = (r["odds_type"] or "").lower()
        if kind not in ("goblin", "demon") or r["line_first"] is None:
            continue
        pair = frozenset((_last(r["player"]), _last(r["opponent"])))
        key = "%s|%s|%s|%g" % ("~".join(sorted(pair)), r["match_date"], kind, r["line_first"])
        if key in d["picks"]:
            continue
        total = std.get((pair, r["match_date"]))
        if total is None:
            continue   # waits for its Standard line; first_seen stays inside the window
        a, b, line = r["player"], r["opponent"], float(r["line_first"])
        s = snap.get(pair)
        if s:
            own, src = (s[1] if _last(a) == s[0] else 1 - s[1]), s[2]
            wta = s[3] == "WTA"
        else:
            own, src, wta = _strength(a, b), "player strength", _tour(a, b)
        rec = dict(key=key, pid=r["projection_id"], a=a, b=b, type=kind, line=line, total=total, room=total - line,
                   start=r["start_utc"], day=r["match_date"], first_seen=r["first_seen"], src=src, own=own,
                   scored=datetime.now(timezone.utc).isoformat(timespec="seconds"), surface=r["surface"], bet365=r["bet365_line"])
        if own is None or _banned(a, b):
            rec["status"] = "banned" if own is not None else "no win chance"
            d["picks"][key] = rec
            continue
        fav, dog, f = (a, b, own) if own >= 0.5 else (b, a, 1 - own)
        res = eng.evaluate_match(fav, dog, min(max(f, 0.5), 0.995), surface=(r["surface"] or "Hard").title(), wta=wta,
                                 date=r["match_date"], lines={"match|total_games": line})
        t = next((x for x in (res or {}).get("props", []) if x["category"] == "total_games"), None)
        if not t or not (float(t.get("p_over") or 0) + float(t.get("p_under") or 0)):
            rec["status"] = "engine declined"
            d["picks"][key] = rec
            continue
        wp = 100 * float(t["p_over"]) / (float(t["p_over"]) + float(t["p_under"]))
        p = dict(player="match", opponent="%s v %s" % (a, b), category="total_games", direction="MORE", line=line,
                 prop_type=kind, match_date=r["match_date"], match_id="")
        net, facts = _signals(p, total, f)
        hold = None
        if rec["room"] < MIN_ROOM:
            hold = "room"
        elif line == 17.5 and t3.rule175(["%s" % a, "%s" % b], r["match_date"], line, book=total)[0] == "skip":
            hold = "17.5 check"
        rec.update(wp=round(wp, 1), net=net, rank=round(wp + SIGNAL_POINTS * net, 1), facts=facts, hold=hold,
                   status="passes" if wp >= MIN_WIN and not hold else ("held" if wp >= MIN_WIN else "under 70"))
        d["picks"][key] = rec
        if rec["status"] == "passes":
            out.append(rec)
    return out


def grade(d):
    con = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True)
    n = 0
    for rec in d["picks"].values():
        if "wp" not in rec:
            continue
        r = con.execute("select actual,more_won from projections where projection_id=?", (rec["pid"],)).fetchone()
        if r and r[0] is not None and r[1] is not None:   # graded at the first-posted line, not the line at start
            res = "hit" if r[0] > rec["line"] else ("miss" if r[0] < rec["line"] else "void")
            n += rec.get("result") != res
            rec["result"] = res
    con.close()
    return n


def _card(new):
    import apex_alert_cards as C
    new = sorted(new, key=lambda x: -x["rank"])
    L = []
    for x in new[:12]:
        st = datetime.fromisoformat(x["start"]).astimezone(CT).strftime("%a %-I:%M %p")
        L.append("**%s v %s** over %g %s | Win %.0f | Rank %.0f (%+d signals) | %g games of room under Standard %g | %s | %s"
                 % (x["a"], x["b"], x["line"], x["type"].title(), x["wp"], x["rank"], x["net"], x["room"], x["total"], x["src"], st))
    C.post(C.make("\U0001F4DD PAPER: new line at first post", color=C.BLUE,
                  desc="\n".join(L) + "\nPaper test, not on the boards. Check the line in the app."))


def record():
    d = _load()
    def rc(f):
        v = [x for x in d["picks"].values() if f(x) and x.get("result") in ("hit", "miss")]
        w = sum(x["result"] == "hit" for x in v)
        return "%d won, %d lost" % (w, len(v) - w)
    return rc(lambda x: x.get("status") == "passes"), rc(lambda x: x.get("status") == "held" and x.get("hold") == "room")


def status_line():
    """One line for the 9 AM status. Never raises."""
    try:
        if not STANDARD_LINE_PAPER:
            return None
        p, h = record()
        d = _load()
        y = (datetime.now(CT).date() - timedelta(days=1)).isoformat()
        n = sum(1 for x in d["picks"].values() if x.get("status") == "passes"
                and datetime.fromisoformat(x["scored"]).astimezone(CT).date().isoformat() == y)
        odds = " | new API odds stopped 11/6, player strength only" if datetime.now(CT).date().isoformat() > ODDS_END else ""
        return "\U0001F4DD Standard line paper since 10/31: passes %s | under 3 games of room %s | %s yesterday%s" % (
            p, h, ("%d alert%s" % (n, "" if n == 1 else "s")) if n else "no alerts", odds)
    except Exception as e:
        return "⚠️ Standard line paper failed: %s" % type(e).__name__


def _finish(d):
    """TEST FINISHED card once, at 11/14 or 40 graded."""
    if d.get("finished"):
        return
    n = sum(1 for x in d["picks"].values() if x.get("status") == "passes" and x.get("result") in ("hit", "miss"))
    if n < FINISH_GRADED and datetime.now(CT).date().isoformat() < FINISH_DAY:
        return
    import apex_alert_cards as C
    p, h = record()
    C.post(C.make("\U0001F3C1 TEST FINISHED: Standard line paper", color=C.BLUE,
                  desc="Passes: %s\nUnder 3 games of room: %s\nPaper test, not on the boards. Your call what happens next." % (p, h)))
    d["finished"] = datetime.now(CT).isoformat(timespec="seconds")
    _save(d)


def run(post=True):
    """Scheduler job. Never raises."""
    try:
        if not STANDARD_LINE_PAPER:
            return "off"
        d = _load()
        new = score_new(d)
        g = grade(d)
        _save(d)
        if new and post:
            _card(new)
        if post:
            _finish(d)
        return "%d new, %d graded" % (len(new), g)
    except Exception as e:
        return "failed %s: %s" % (type(e).__name__, e)
