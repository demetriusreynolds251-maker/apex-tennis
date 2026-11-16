#!/usr/bin/env python3
"""APEX-SETGAMES-2026-11-01 (owner, "Go set games board"). A separate first-set total games list, off Top 3 and
Elite. Strict rules: Goblin over 7.5 with favorite under 75; Goblin over 8.5 with book total 22+, favorite under 80, low
recent blowouts, both players averaging 9.5+ first-set games. Earlier matches 4,646-729 at fixed lines; real 60-11,
47-9 of them up at board lock (_backups/set_games_test_2026-11-01). Needs book odds, no fallback.
Undo: SET_GAMES_ON = False, restart."""
import csv, math
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SET_GAMES_ON = True
SET_GAMES_RANK = True   # APEX-RANKFIRSTSET-2026-11-01 ("Go rank first set"): strict picks compete in Ranking order on the boards
RANK_75, RANK_85 = 0.882, 0.812   # long-run 2026 record: over 7.5 3819 won 509 lost; strict over 8.5 1133 won 262 lost
# APEX-FIRSTSETLESS-2026-11-04 (owner, "Go first set less"): the one under that held up. Standard-line first-set
# LESS (9.5 or 8.5) when the book's match total is under 22: real PrizePicks 33 won, 12 lost 10/13-10/31 (15-5 / 18-7 by
# half); match total 22-23 is the opposite (9-19). Rule 2, ranked under both over rules. Undo: SET_GAMES_LESS = False.
SET_GAMES_LESS = True
RANK_LESS = 0.733
LESS_LINES = (8.5, 9.5)
LESS_TOTAL_MAX = 22.0
SET_GAMES_FILL = False   # was True; off with SET_GAMES_RANK, no fill-only spots # "Go set games fill": strict picks shown in empty Top 3 / Elite spots, never bumping a pick
HERE = os.path.dirname(os.path.abspath(__file__))
PROPS_DB = os.path.join(HERE, "data", "props_db.sqlite")
PICK_LOG = os.path.join(HERE, "apex_engine_picks_log_v1.csv")
LOG = os.path.join(HERE, "data", "logs", "set_games.json")
BOARD_DB = os.path.join(HERE, "data", "board_history.sqlite")
BOARD_SIZE = {"top3": 3, "elite6": 6}
BOARD_NAME = {"top3": "Top 3", "elite6": "Elite 6"}
CT = ZoneInfo("America/Chicago")
FAV_75, FAV_85, TOTAL_MIN, BLOW_MAX, AVG_MIN, N = 0.75, 0.80, 22.0, 0.20, 9.5, 10


def _load():
    try:
        with open(LOG) as fh:
            return json.load(fh)
    except Exception:
        return {"picks": {}, "shown": []}


def _save(d):
    tmp = LOG + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh)
    os.replace(tmp, LOG)


def _clean(n):
    return re.sub(r"\s*1st\s*set.*$", "", n or "", flags=re.I).strip()


def _history(name, before):
    """(first-set games, match games) for the player's last N finished matches before `before`."""
    import apex_player_history as ph
    con = sqlite3.connect("file:%s?mode=ro" % ph.path(), uri=True)
    try:
        rows = con.execute("select score,total_games,retired from matches where (player_a=? or player_b=?) and match_date<? "
                           "order by match_date desc limit 40", (name, name, before)).fetchall()
    finally:
        con.close()
    out = []
    for sc, tg, ret in rows:
        m = re.match(r"\s*(\d+)-(\d+)", sc or "")
        if ret or not m or not 6 <= int(m[1]) + int(m[2]) <= 13:
            continue
        games = sum(int(a) + int(b) for a, b in re.findall(r"(\d+)-(\d+)", sc) if max(int(a), int(b)) < 10)
        out.append((int(m[1]) + int(m[2]), tg or games))
        if len(out) >= N:
            break
    return out


_LOG_CACHE = {}


def _log_prices():
    """{frozenset(players): (favorite's chance, log match date)} newest row wins, from the pick log. Cached a minute."""
    now = datetime.now().timestamp()
    if _LOG_CACHE.get("at", 0) > now - 60:
        return _LOG_CACHE["data"]
    out = {}
    since = (date.today() - timedelta(days=3)).isoformat()
    with open(PICK_LOG, newline="", encoding="utf-8", errors="ignore") as fh:
        for r in csv.DictReader(fh):
            if (r.get("match_date") or "") < since or "bet365" not in (r.get("price_source") or "") + "" and "pinnacle" not in (r.get("price_source") or ""):
                continue
            try:
                f = float(r.get("pinn_fav"))
            except (TypeError, ValueError):
                continue
            names = [x.strip() for x in str(r.get("opponent") or "").split(" v ")] if r.get("player") == "match" else [r.get("player"), r.get("opponent")]
            if len(names) == 2:
                out[frozenset(names)] = (f, r.get("match_date"))
    _LOG_CACHE.update(at=now, data=out)
    return out


def _book(a, b):
    """(favorite's chance, book total) for the match, from what Apex priced with bet365. Nones if not priced."""
    v = _log_prices().get(frozenset((a, b)))
    if not v:
        return None, None
    tot = None
    try:
        import apex_bet365_totals as b3
        tot = b3.total([a, b], v[1])[0]
        if tot is None:   # APEX-BOOKDATE-2026-11-01: backup source, then a saved total from before a date move
            import apex_sgo_backup as _sb
            tot = _sb.total([a, b], v[1])[0]
        if tot is None:
            tot = b3.total_near([a, b], v[1])[0]
    except Exception:
        pass
    return v[0], tot


def _banned(a, b):
    try:
        with open(os.path.join(HERE, "data", "banned_players.json")) as fh:
            ban = {x["name"].split()[-1].lower() for x in json.load(fh).get("players", [])}
        return a.split()[-1].lower() in ban or b.split()[-1].lower() in ban
    except Exception:
        return False


def current():
    """(qualifying picks, count waiting on odds). One per match, 7.5 first, then start time."""
    pdb = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True)
    pdb.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    newest = pdb.execute("select max(last_seen) from projections").fetchone()[0]
    cut = (datetime.fromisoformat(newest) - timedelta(minutes=45)).isoformat()
    rows = pdb.execute("select * from projections where category='1st_set_total_games' and ((odds_type='goblin' and line_last in (7.5,8.5))"
                       " or (odds_type='standard' and line_last in (8.5,9.5))) and start_utc>? and last_seen>=?", (now, cut)).fetchall()
    best, waiting = {}, set()
    for r in rows:
        a, b = _clean(r["player"]), _clean(r["opponent"])
        key = frozenset((a, b))
        if _banned(a, b):
            continue
        fav, tot = _book(a, b)
        if r["odds_type"] == "standard":   # APEX-FIRSTSETLESS-2026-11-04: rule 2, standard LESS when the match total is under 22
            if not SET_GAMES_LESS or float(r["line_last"]) not in LESS_LINES:
                continue
            if not tot:
                waiting.add(key)
                continue
            if float(tot) >= LESS_TOTAL_MAX:
                continue
            rec = dict(log_day=(_log_prices().get(key) or (None, None))[1], pid=r["projection_id"], a=a, b=b, line=r["line_last"], start=r["start_utc"], day=r["match_date"],
                       fav=round(float(fav), 3) if fav is not None else None, total=tot, rule=2, direction="LESS")
            if key not in best or rec["rule"] < best[key]["rule"]:
                best[key] = rec
            continue
        if fav is None or (r["line_last"] == 8.5 and not tot):
            waiting.add(key)
            continue
        fav = float(fav)
        rec = dict(log_day=(_log_prices().get(key) or (None, None))[1], pid=r["projection_id"], a=a, b=b, line=r["line_last"], start=r["start_utc"], day=r["match_date"], fav=round(fav, 3), total=tot,
                   rule=0 if r["line_last"] == 7.5 else 1, direction="MORE")
        if r["line_last"] == 7.5:
            if fav >= FAV_75:
                continue
        else:
            if float(tot) < TOTAL_MIN or fav >= FAV_85:
                continue
            ha, hb = _history(a, r["match_date"]), _history(b, r["match_date"])
            if len(ha) < 5 or len(hb) < 5:
                continue
            al = ha + hb
            avg = sum(x[0] for x in al) / float(len(al))
            blow = sum(1 for x in al if x[1] <= 17) / float(len(al))
            if avg < AVG_MIN or blow >= BLOW_MAX:
                continue
            rec.update(avg=round(avg, 1), blow=round(blow, 2), blow_n=sum(1 for x in al if x[1] <= 17), n=len(al))
        if key not in best or rec["rule"] < best[key]["rule"]:
            best[key] = rec
    pdb.close()
    return sorted(best.values(), key=lambda x: (x["rule"], x["start"])), len(waiting)


_RANK_CACHE = {}


FS_ENGINE_ON = True   # APEX-FSHYBRID-2026-11-01: undo = False, ties go back to closest match
_FS_MODEL = {}
_TOURS = {}


def _tour_wta(name):
    """1 for a WTA player, 0 otherwise, from the warehouse Elo table."""
    if not _TOURS:
        _TOURS["_"] = 1
        try:
            con = sqlite3.connect("file:%s?mode=ro" % os.path.join(HERE, "data", "warehouse", "tennis_warehouse.sqlite"), uri=True)
            for pl, t in con.execute("select player, tour from player_elo"):
                _TOURS[pl] = 1 if str(t or "").upper() == "WTA" else 0
            con.close()
        except Exception:
            pass
    return _TOURS.get(name, 0)


def engine_number(x):
    """APEX-FSHYBRID-2026-11-01 (owner, "Go first set hybrid"): this match's first-set Win Prob from the
    tested model (data/first_set_model.json: favorite, match total, tour, players' recent first sets), 0-1.
    Orders picks with the same rule number and shows on the card; it never lets a pick on. None if unknown."""
    try:
        if not FS_ENGINE_ON:
            return None
        if not _FS_MODEL:
            with open(os.path.join(HERE, "data", "first_set_model.json")) as fh:
                _FS_MODEL.update(json.load(fh))
        fav = float(x.get("fav") or 0)
        tot = float(x.get("total") or 22.0)
        day = str(x.get("start") or "")[:10]
        ha, hb = _history(x["a"], day), _history(x["b"], day)
        if not fav:
            return None
        av = lambda h: sum(g[0] for g in h[:15]) / float(len(h[:15])) if h else 9.4
        f = [fav, fav * fav, tot, _tour_wta(x["a"]), (av(ha) + av(hb)) / 2.0, min(len(ha[:15]), len(hb[:15])) / 15.0]
        m = _FS_MODEL["lines"][str(float(x["line"]))]
        z = m["i"] + sum(c * (v - mu) / sd for c, v, mu, sd in zip(m["c"], f, _FS_MODEL["mu"], _FS_MODEL["sd"]))
        return 1.0 / (1.0 + math.exp(-z))
    except Exception as e:
        print("[SetGames] engine number skipped (%s: %s)" % (type(e).__name__, e))
        try:
            import apex_failopen as _fo
            _fo.note("first_set_engine", e)
        except Exception:
            pass
        return None


def board_entries():
    """Scored board entries for the strict first-set picks. Equal numbers: closest match first. Never raises."""
    try:
        if not (SET_GAMES_ON and SET_GAMES_RANK):
            return []
        now = datetime.now().timestamp()
        if _RANK_CACHE.get("at", 0) > now - 30:
            return list(_RANK_CACHE["data"])
        out = []
        rows = []
        for r in current()[0]:
            e = engine_number(r)
            rows.append(dict(r, _eng=(1 - e) if (e is not None and r["rule"] == 2) else e))   # APEX-FIRSTSETLESS: a LESS pick's own chance
        # APEX-FSHYBRID-2026-11-01: same rule number, higher engine number first; unknown last, closest match breaks the rest
        for x in sorted(rows, key=lambda r: (r["rule"], -(r["_eng"] or 0), r.get("fav") or 1.0, r["start"])):
            pr = {0: RANK_75, 1: RANK_85, 2: RANK_LESS}[x["rule"]]
            sc = int(round(100 * pr))
            p = {"match_id": "fs:%s" % x["pid"], "player": "match", "opponent": "%s v %s" % (x["a"], x["b"]),
                 "category": "1st_set_total_games", "line": x["line"], "direction": x.get("direction") or "MORE",
                 "prop_type": "standard" if x["rule"] == 2 else "goblin",
                 "prob": pr, "prob_cal": pr, "playable": "1", "slip_start_utc": x["start"], "pinn_fav": x.get("fav"),
                 "match_date": x.get("log_day") or str(x["start"])[:10], "stat_type": "1st Set Total Games", "bo5": False,
                 "fs_engine": x["_eng"]}
            out.append({"p": p, "sc": sc, "score": sc, "ev": "first-set record", "lvl": "MEDIUM", "up": [], "down": [],
                        "conf": sc, "cwhy": [], "rank": float(sc)})
        _RANK_CACHE.update(at=now, data=out)
        return list(out)
    except Exception as e:
        print("[SetGames] board entries skipped (%s: %s)" % (type(e).__name__, e))
        return []


def leg_actual(match_id, line):
    """Real first-set games for a board leg, the line itself for a void, None while open. Never raises."""
    try:
        pid = str(match_id or "")[3:]
        con = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True)
        r = con.execute("select actual,result from projections where projection_id=?", (pid,)).fetchone()
        con.close()
        if not r:
            return None
        if r[1] == "void":
            return float(line)
        return float(r[0]) if r[0] is not None else None
    except Exception:
        return None


def _when(s):
    t = datetime.fromisoformat(s).astimezone(CT)
    return t.strftime("%a %-I:%M %p")


def _fav_text(x):
    """'Cameron Norrie 64 to win', or 'Favorite 64 to win' when Apex doesn't say which player."""
    pct = round(x["fav"] * 100)
    try:
        import apex_card_signals as cs
        own = cs.own_win_chance(dict(player=x["a"], match_date=x.get("log_day") or x["day"]))
        if own is not None:
            return "%s %d to win" % (x["a"] if own >= 0.5 else x["b"], pct)
    except Exception:
        pass
    return "Favorite %d to win" % pct


def _line(i, x):
    """One pick's card, one line per signal with the record behind it."""
    if x.get("rule") == 2:   # APEX-FIRSTSETLESS-2026-11-04
        return "\n".join(["%d. **%s v %s**" % (i, x["a"], x["b"]),
                          "1st Set Total Games **LESS %g** \u00b7 Standard line \u00b7 %s CT" % (x["line"], _when(x["start"])),
                          "\U0001F4CF Book total %g games \u2705 short match expected (needs under 22; with it: 33 won, 12 lost; at 22-23: 9 won, 19 lost)" % float(x["total"]),
                          "\U0001F4CA Rule: 33 won, 12 lost on real props 10/13-10/31; 3,128 won, 2,319 lost at a fixed line on older matches (weaker there)"])
    L = ["%d. **%s v %s**" % (i, x["a"], x["b"]),
         "1st Set Total Games **MORE %g** \u00b7 Goblin \u00b7 %s CT" % (x["line"], _when(x["start"]))]
    if x["rule"] == 0:
        L.append("\u2696\ufe0f %s \u2705 not a mismatch (needs under 75; under 75: 16 won, 0 lost; 75+: 16 won, 6 lost)" % _fav_text(x))
        L.append("\U0001F4CA Rule: 16 won, 0 lost on real props; 3,513 won, 467 lost at a fixed line on older matches")
    else:
        L.append("\U0001F4CF Book total %g games \u2705 long match expected (needs 22+; with it: 64 won, 15 lost)" % float(x["total"]))
        L.append("\u2696\ufe0f %s \u2705 not a mismatch (needs under 80; at 80+: 8 won, 13 lost)" % _fav_text(x))
        L.append("\U0001F3DF\ufe0f Blowouts: %d of their last %d matches \u2705 competitive lately (under 20 of 100: 103 won, 36 lost; 20-40: 59 won, 34 lost)"
                 % (x.get("blow_n", 0), x.get("n", 0)))
        L.append("\u23F1\ufe0f First sets lately: %.1f games a set \u2705 long first sets (9.5+: 121 won, 44 lost; under: 45 won, 28 lost)" % x["avg"])
        L.append("\u2705 Passes all 4")
        L.append("\U0001F4CA Rule: 44 won, 11 lost on real props; 1,133 won, 262 lost at a fixed line on older matches")
    return "\n".join(L)


def grade(d):
    con = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True)
    for rec in d["picks"].values():
        if rec.get("result"):
            continue
        r = con.execute("select actual,more_won from projections where projection_id=?", (rec["pid"],)).fetchone()
        if r and r[0] is not None and r[1] is not None:
            rec["result"] = _result(rec, r[0])
    con.close()


def _result(rec, actual):
    """hit/miss/void for the pick's own direction (LESS picks win under the line). APEX-FIRSTSETLESS-2026-11-04."""
    if actual == rec["line"]:
        return "void"
    over = actual > rec["line"]
    return "hit" if over == (str(rec.get("direction") or "MORE").upper() == "MORE") else "miss"


def record(d=None):
    d = d or _load()
    v = [x for x in d["picks"].values() if x.get("result") in ("hit", "miss")]
    w = sum(x["result"] == "hit" for x in v)
    return w, len(v) - w


HEADER = ("\U0001F3BE **First-set games list**\nGoblin first-set overs that pass every check, plus Standard-line LESS 9.5 when the book expects a short match (total under 22). Separate from Top 3 and Elite. "
          "No Win Prob or Ranking # here: a pick passes all its checks or it's off the list.")


def card_text():
    """The /setgames reply. Never raises."""
    try:
        if not SET_GAMES_ON:
            return "First-set list is off."
        picks, waiting = current()
        w, l = record()
        body = "\n\n".join(_line(i + 1, x) for i, x in enumerate(picks)) if picks else "No qualifying picks right now."
        return "%s\nRecord since 11/1: %d won, %d lost\n\n%s\n\n%d match%s waiting on book odds. Check the line in the app. Records come from the same weeks the rules were picked from." % (
            HEADER, w, l, body, waiting, "" if waiting == 1 else "es")
    except Exception as e:
        return "\u26A0\ufe0f First-set list failed: %s" % type(e).__name__


def chunks(text, limit=1900):
    """Splits on blank lines so a pick's card stays whole, for Discord's message limit."""
    out, cur = [], ""
    for part in text.split("\n\n"):
        if cur and len(cur) + len(part) + 2 > limit:
            out.append(cur)
            cur = part
        else:
            cur = cur + "\n\n" + part if cur else part
    return out + ([cur] if cur else [])


def post_cards(picks, title):
    """#alerts, up to 6 picks a card."""
    import apex_alert_cards as C
    for n in range(0, len(picks), 6):
        part = picks[n:n + 6]
        C.post(C.make(title, color=C.BLUE, desc=HEADER + "\n\n" + "\n\n".join(_line(n + i + 1, x) for i, x in enumerate(part)) + "\n\nCheck the line in the app."))


def _board_now(flag):
    """[frozenset(players)] for the newest published board, matches not yet started."""
    con = sqlite3.connect("file:%s?mode=ro" % BOARD_DB, uri=True)
    try:
        pid = con.execute("select max(publish_id) from publishes where platform='prizepicks'").fetchone()[0]
        out = []
        now = datetime.now(timezone.utc)
        for pl, op, st in con.execute("select player,opponent,match_start from history where publish_id=? and board=? and spot_kind='main' "
                                      "order by spot_no", (pid, flag)):
            try:
                if st and datetime.fromisoformat(st) <= now:
                    continue
            except ValueError:
                pass
            out.append(frozenset(x.strip() for x in str(op).split(" v ")) if pl == "match" else frozenset((pl, op)))
        return out
    finally:
        con.close()


def fills(flag, picks=None):
    """[(spot number, pick)] for this board's empty spots, rule order, no match already on it. Never raises."""
    try:
        if not (SET_GAMES_ON and SET_GAMES_FILL) or flag not in BOARD_SIZE:
            return []
        board = _board_now(flag)
        need = BOARD_SIZE[flag] - len(board)
        if need <= 0:
            return []
        picks = current()[0] if picks is None else picks
        free = [x for x in picks if frozenset((x["a"], x["b"])) not in board]
        return [(len(board) + i + 1, x) for i, x in enumerate(free[:need])]
    except Exception:
        return []


def fill_text(flag):
    """Lines to add under a board command, or ''. Never raises."""
    f = fills(flag)
    if not f:
        return ""
    head = "\n\n\U0001F3BE **First-set fill: spot was empty.** Not a regular board pick, and not counted on the slip record. Steps aside when a regular pick qualifies."
    return head + "\n\n" + "\n\n".join(_line(n, x) for n, x in f)


def fill_record(d=None):
    d = d or _load()
    v = [x for x in (d.get("fills") or {}).values() if x.get("result") in ("hit", "miss")]
    w = sum(x["result"] == "hit" for x in v)
    return w, len(v) - w


def _run_fills(d, picks, post):
    """Log fills, mark ones that stepped aside, change-only #alerts card."""
    log = d.setdefault("fills", {})
    now = datetime.now(timezone.utc)
    on, new = set(), []
    for flag in BOARD_SIZE:
        for n, x in fills(flag, picks):
            k = "%s|%s" % (flag, x["pid"])
            on.add(k)
            if k not in log:
                log[k] = dict(x, board=flag, spot=n, listed=now.isoformat(timespec="seconds"), active=True)
                new.append((flag, n, x))
            log[k]["active"] = True
    for k, rec in log.items():
        if k not in on and rec.get("active") and datetime.fromisoformat(rec["start"]) > now:
            rec["active"] = False   # stepped aside before its match; its result doesn't count
    con = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True)
    for rec in log.values():
        if rec.get("active") and not rec.get("result"):
            r = con.execute("select actual,more_won from projections where projection_id=?", (rec["pid"],)).fetchone()
            if r and r[0] is not None and r[1] is not None:
                rec["result"] = _result(rec, r[0])
    con.close()
    if new and post:
        import apex_alert_cards as C
        body = "\n\n".join("**%s #%d**\n%s" % (BOARD_NAME[f], n, _line(n, x)) for f, n, x in new)
        C.post(C.make("\U0001F3BE First-set fill: spot was empty", color=C.BLUE, desc=body + "\n\nNot on the slip record. Check the line in the app."))
    return len(new)


def status_line():
    """One line for the 9 AM status. Never raises."""
    try:
        if not SET_GAMES_ON:
            return None
        w, l = record()
        fw, fl = fill_record()
        return "\U0001F3BE First-set list since 11/1: %d won, %d lost | Fills in empty board spots: %d won, %d lost" % (w, l, fw, fl)
    except Exception as e:
        return "⚠️ First-set list failed: %s" % type(e).__name__


def run(post=True):
    """Scheduler job: log new picks, grade, one #alerts card when the list gains a pick. Never raises."""
    try:
        if not SET_GAMES_ON:
            return "off"
        d = _load()
        picks, waiting = current()
        new = []
        for x in picks:
            k = "%s|%s|%g" % (x["pid"], x["day"], x["line"])
            if k not in d["picks"]:
                d["picks"][k] = dict(x, listed=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                new.append(x)
        grade(d)
        nf = _run_fills(d, picks, post) if SET_GAMES_FILL else 0
        _save(d)
        if new and post:
            post_cards(new, "\U0001F3BE First-set list: %d new" % len(new))
        return "%d new, %d on list, %d waiting, %d fills" % (len(new), len(picks), waiting, nf)
    except Exception as e:
        return "failed %s: %s" % (type(e).__name__, e)
