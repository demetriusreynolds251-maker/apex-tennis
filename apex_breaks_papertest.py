#!/usr/bin/env python3
"""APEX-BREAKSPAPER-2026-10-31 (owner, "Go paper breaks"): paper test of PrizePicks Standard Breaks LESS at line 4+.
Backtest: _backups/breaks_test_2026-10-31/report.md. Reads props_db, board_history and the warehouse only; touches no board,
card, ranking, slip or Discord. Every run rebuilds data/logs/breaks_less_papertest.json from saved data, so grading is whatever
props_db knows at that moment. Headline counts from TEST_START; earlier rows are labeled before_test_start.
Finish: FINISH_GRADED graded picks or FINISH_DATE. Undo: BREAKS_PAPER_TEST = False.
Run: python3 apex_breaks_papertest.py"""
import collections, json, os, re, sqlite3, unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BREAKS_PAPER_TEST = True
TEST_START = "2026-10-31"
BACKFILL_FROM = "2026-10-12"
LINE_MIN = 4.0
FINISH_GRADED = 40
FINISH_DATE = "2026-11-21"
LOW_BREAKS = 2.5     # player's breaks per match, last 20, under this = rarely breaks
STRONG_SERVER = 2.5  # opponent's breaks conceded per match, last 20, under this = strong server
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "logs", "breaks_less_papertest.json")
CT = ZoneInfo("America/Chicago")


def _ro(path):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    c.row_factory = sqlite3.Row
    return c


def _last(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    x = re.sub(r"[^a-z ]", " ", s).split()
    return x[-1] if x else ""


MATCHES = {}   # APEX-BREAKSAUTO-2026-11-13: (frozenset of last names, date) -> {last name: breaks made}, for the warehouse grade


def _history():
    """Each player's (date, breaks made, breaks conceded), oldest first."""
    h = collections.defaultdict(list)
    w = _ro(os.path.join(HERE, "data", "warehouse", "tennis_warehouse.sqlite"))
    for r in w.execute("""select match_date, player_a, player_b, player_a_break_points_converted a, player_b_break_points_converted b
        from matches where match_date >= '2025-06-01' and player_a_break_points_converted is not null
        and player_b_break_points_converted is not null and coalesce(retired, 0) = 0 order by match_date"""):
        d = str(r["match_date"])[:10]
        h[_last(r["player_a"])].append((d, float(r["a"]), float(r["b"])))
        h[_last(r["player_b"])].append((d, float(r["b"]), float(r["a"])))
        MATCHES[(frozenset((_last(r["player_a"]), _last(r["player_b"]))), d)] = {_last(r["player_a"]): float(r["a"]), _last(r["player_b"]): float(r["b"])}
    w.close()
    return h


def _avg(h, name, day, ix):
    x = [m[ix] for m in h.get(_last(name), []) if m[0] < day][-20:]
    return round(sum(x) / len(x), 2) if len(x) >= 5 else None


def _win_chances():
    """Apex's last win chance for each Standard Breaks LESS prop on the All Props board."""
    h = _ro(os.path.join(HERE, "data", "board_history.sqlite"))
    out = {}
    for r in h.execute("""select h.player, h.opponent, h.line, h.win_prob from history h join publishes p using(publish_id)
        where p.platform='prizepicks' and h.board='allprops' and h.category='breaks' and lower(h.prop_type)='standard'
        and upper(h.direction)='LESS' and h.board_day >= ? order by h.id""", (BACKFILL_FROM,)):
        out[(r["player"], r["opponent"], float(r["line"]))] = r["win_prob"]
    h.close()
    return out


def build():
    db = _ro(os.path.join(HERE, "data", "props_db.sqlite"))
    rows = db.execute("""select player, opponent, line_last line, start_utc, more_won, less_won, actual, score, settle_note, player_is_fav, result raw_result
        from projections where category='breaks' and odds_type='standard' and line_last >= ? and coalesce(start_utc,'') >= ?
        and coalesce(player,'') != '' order by start_utc""", (LINE_MIN, BACKFILL_FROM)).fetchall()
    db.close()
    hist, wc, picks = _history(), _win_chances(), {}
    for r in rows:
        day = datetime.fromisoformat(r["start_utc"]).astimezone(CT).date().isoformat()
        note = r["settle_note"] or ""
        res = "void" if "VOID" in note else "won" if r["less_won"] else "lost" if r["more_won"] else "push" if r["raw_result"] == "push" else "pending"
        graded_from = "props_db" if res in ("won", "lost") else None; actual = r["actual"]
        if res == "pending":   # APEX-BREAKSAUTO-2026-11-13: Breaks are off the boards, so props_db rarely settles them: grade from the warehouse match (finished, not retired)
            for off in (0, -1, 1):
                dd = (datetime.fromisoformat(day) + __import__("datetime").timedelta(days=off)).date().isoformat()
                m = MATCHES.get((frozenset((_last(r["player"]), _last(r["opponent"]))), dd))
                if m and _last(r["player"]) in m:
                    actual = m[_last(r["player"])]
                    if actual != float(r["line"]): res = "won" if actual < float(r["line"]) else "lost"; graded_from = "warehouse"
                    break
        own, opp = _avg(hist, r["player"], day, 1), _avg(hist, r["opponent"], day, 2)
        key = "%s|%s|%s|%s" % (r["player"], r["opponent"], day, r["line"])
        if res == "pending" and key in picks and picks[key]["result"] != "pending": continue   # APEX-WAITINGBREAKS-2026-11-13: an ungraded copy never hides the graded row of the same line
        picks[key] = {
            "player": r["player"], "opponent": r["opponent"], "date": day, "start_utc": r["start_utc"], "line": r["line"],
            "apex_win_chance": wc.get((r["player"], r["opponent"], float(r["line"]))),
            "is_favorite": None if r["player_is_fav"] is None else bool(r["player_is_fav"]),
            "rarely_breaks": None if own is None else own < LOW_BREAKS, "own_breaks_avg": own,
            "opp_strong_server": None if opp is None else opp < STRONG_SERVER, "opp_conceded_avg": opp,
            "result": res, "actual": actual, "score": r["score"], "graded_from": graded_from, "before_test_start": day < TEST_START}
    return sorted(picks.values(), key=lambda p: p["start_utc"])


def _rec(picks):
    return sum(p["result"] == "won" for p in picks), sum(p["result"] == "lost" for p in picks)


def summary(picks):
    live = [p for p in picks if not p["before_test_start"]]
    w, l = _rec(live)
    fw, fl = _rec([p for p in live if p["is_favorite"]])
    bw, bl = _rec([p for p in picks if p["before_test_start"]])
    today = datetime.now(CT).date().isoformat()
    return {"won": w, "lost": l, "fav_won": fw, "fav_lost": fl, "pending": sum(p["result"] == "pending" for p in live),
            "void": sum(p["result"] == "void" for p in live), "before_won": bw, "before_lost": bl,
            "finished": w + l >= FINISH_GRADED or today >= FINISH_DATE, "updated": datetime.now(timezone.utc).isoformat()}


def status_line(s):
    return "\U0001F9EA Breaks LESS 4+ paper test (since 10/31): %d won, %d lost; favorites %d-%d" % (s["won"], s["lost"], s["fav_won"], s["fav_lost"])


def run():
    if not BREAKS_PAPER_TEST:
        return None
    picks = build()
    s = summary(picks)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT + ".tmp", "w") as f:
        json.dump({"summary": s, "picks": picks}, f, indent=1)
    os.replace(OUT + ".tmp", OUT)
    return s


# APEX-BREAKSAUTO-2026-11-13 (owner, "Go breaks"): automatic again. apex_alerts.check calls auto() every 30 min: from 9 AM CT once a day it
# rebuilds the log and posts one line in #system; when the test reaches FINISH_GRADED or FINISH_DATE it posts one TEST FINISHED card in #system and
# #alerts (once). Paper only: no board, card or pick changes. Undo: AUTO_ON = False (or git checkout before-breaks-auto), restart.
AUTO_ON = True
STATE = os.path.join(HERE, "data", "logs", "breaks_less_papertest_state.json")


def auto(post=True):
    if not (AUTO_ON and BREAKS_PAPER_TEST):
        return []
    now = datetime.now(CT); today = now.date().isoformat()
    try:
        st = json.load(open(STATE))
    except Exception:
        st = {}
    if now.hour < 9 or st.get("daily") == today:
        return []
    s = run(); sent = []
    if not s:
        return []
    import apex_ops as ops
    say = (lambda ch, x: ops.send_discord(ch, x)) if post else (lambda ch, x: print("---- would post to #%s ----\n%s" % (ch, x)))
    line = status_line(s) + " · waiting %d · finishes at %d graded or %s" % (s["pending"], FINISH_GRADED, FINISH_DATE)
    say("system", line); sent.append(line)
    if s["finished"] and not st.get("finished"):
        w, l = s["won"], s["lost"]
        card = ("\U0001F9EA **TEST FINISHED -- DECISION NEEDED** · Breaks LESS 4+ paper test (since 10/31)\n"
                "Result: won %d, lost %d%s · favorites %d-%d · before the test (10/12-10/30): %d-%d\n"
                "PrizePicks Standard Breaks LESS at line 4+ needs about 55 of 100 to break even on a 6-pick.\n"
                "Decision: put Breaks LESS 4+ back on the boards / keep it off / keep testing." % (w, l, (" (%d of 100)" % round(100.0 * w / (w + l))) if w + l else "", s["fav_won"], s["fav_lost"], s["before_won"], s["before_lost"]))
        say("system", card); say("alerts", card); sent.append(card); st["finished"] = today
    st["daily"] = today
    with open(STATE, "w") as f:
        json.dump(st, f)
    return sent


if __name__ == "__main__":
    s = run()
    if s:
        print(status_line(s))
        print("pending %d, void %d | before test start (10/12 to 10/30): %d won, %d lost | finished: %s" % (s["pending"], s["void"], s["before_won"], s["before_lost"], s["finished"]))
