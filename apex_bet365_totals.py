#!/usr/bin/env python3
"""
APEX-BET365-THREEPART-2026-10-22 (owner, "Go bet365"): the ONE place every bet365
total-games number is read from.

  total(names, match_date) -> (book_line, book_over_prob, source)   source "log" / "props_db" / None

Two stores hold bet365's main Over/Under total for a match:
  - apex_book_lines_log_v1.csv  (written at every publish, so it has upcoming matches)
  - data/props_db.sqlite projections.bet365_line / bet365_over_prob (filled hourly from the log)
Upcoming matches (today or later) read the log first; past matches read the props database
first; either way the other store is the fallback.

A match is BOTH players' names AND the match date -- a total is never returned for another
date (rematches used to pick up an old total). Names match on last name + first initial
("Leylah Annie Fernandez" == "Leylah Fernandez"), with a second try on the full set of name
words ("Xiyu Wang" == "Wang Xiyu"). Never raises.
"""
import csv, os, sqlite3, time, unicodedata
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "apex_book_lines_log_v1.csv")
DB = os.path.join(HERE, "data", "props_db.sqlite")
RECHECK = 30   # seconds between "did a file change?" checks inside the long-lived bot

_idx = {"at": 0, "sig": None, "log": {}, "db": {}}


def _clean(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    for ch in "-.,'’":
        s = s.replace(ch, " ")
    return [w for w in s.split() if w]


def name_key(s):
    """'leylah annie fernandez' -> 'fernandez l' (last name + first initial)."""
    w = _clean(s)
    return ("%s %s" % (w[-1], w[0][0])) if w else ""


def _words(s):
    return " ".join(sorted(_clean(s)))


def _keys(names, match_date):
    names = [n for n in (names or []) if str(n or "").strip()][:2]
    if len(names) != 2:
        return []
    d = str(match_date or "")[:10]
    return [(frozenset(name_key(n) for n in names), d), (frozenset("w:" + _words(n) for n in names), d)]


def row_dates(match_date, start_utc=None):
    """The dates one scheduled match goes by: its match_date and the UTC date of its start time.
    The props database dates evening US matches by Central time (10/22, 9:30 PM CT) while the
    board and the bet365 log use the next day (10/23) -- same match, never a different day."""
    out = [str(match_date or "")[:10]]
    s = str(start_utc or "")[:10]
    if len(s) == 10 and s not in out:
        out.append(s)
    return [d for d in out if d]


def pair_of(r):
    """Both player names of a log / props row ('match' rows carry 'A v B' in opponent)."""
    if r.get("player") == "match":
        xs = [x.strip() for x in str(r.get("opponent") or "").replace(" vs ", " v ").split(" v ")]
        return (xs + [""])[:2]
    return [r.get("player"), r.get("opponent")]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _sig():
    out = []
    for p in (LOG, DB, DB + "-wal"):
        try:
            out.append(os.path.getmtime(p))
        except OSError:
            out.append(0)
    return tuple(out)


def _load():
    if time.time() - _idx["at"] < RECHECK:
        return _idx
    _idx["at"] = time.time()
    sig = _sig()
    if sig == _idx["sig"]:
        return _idx
    log, db = {}, {}
    try:
        with open(LOG, newline="") as fh:
            for r in csv.DictReader(fh):
                bl, op = _f(r.get("book_line")), _f(r.get("book_over_prob"))
                if bl is None:
                    continue
                v = (bl, op, r.get("captured_at") or "", (r.get("book") or "bet365").lower())   # APEX-FONBET-2026-11-07: book kept
                for k in _keys(pair_of(r), r.get("match_date")):
                    if k not in log or v[2] >= log[k][2]:   # newest capture for that match + date wins
                        log[k] = v
    except Exception:
        pass
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
        for pl, opp, md, st, bl, op in con.execute(
                "select player, opponent, match_date, start_utc, bet365_line, bet365_over_prob from projections "
                "where bet365_line is not null and category = 'total_games'"):
            for d in row_dates(md, st):
                for k in _keys(pair_of({"player": pl, "opponent": opp}), d):
                    db.setdefault(k, (float(bl), _f(op), "props_db"))
        con.close()
    except Exception:
        pass
    _idx.update(sig=sig, log=log, db=db)
    return _idx


BOOK_DATE_SLACK = 2


def total(names, match_date):
    """(bet365 main total, bet365 no-vig over chance or None, 'log' / 'props_db') for this match
    on this date, else (None, None, None). Never raises."""
    try:
        d = str(match_date or "")[:10]
        if not d:
            return None, None, None
        ix = _load()
        order = (("log", ix["log"]), ("props_db", ix["db"])) if d >= date.today().isoformat() \
            else (("props_db", ix["db"]), ("log", ix["log"]))
        for src, store in order:
            for k in _keys(names, d):
                v = store.get(k)
                if v is not None:
                    return v[0], v[1], src
    except Exception:
        pass
    return None, None, None


BOOK_MAX_AGE_H = 12   # APEX-BOOKCHECK-2026-11-01: an older bet365 number gives way to a fresh backup source


def log_age_hours(names, match_date):
    """Hours since this match's bet365 number was logged, None if unknown. Never raises."""
    try:
        ix = _load()
        for k in _keys(names, str(match_date or "")[:10]):
            v = ix["log"].get(k)
            if v and v[2]:
                return (datetime.now() - datetime.fromisoformat(str(v[2])[:19])).total_seconds() / 3600.0
    except Exception:
        pass
    return None


def total_near(names, match_date):
    """APEX-BOOKDATE-2026-11-01 (owner, "Go book date fix", "Go fresh total first"): the same two
    players up to BOOK_DATE_SLACK days either side, nearest first, for a match whose date moved. Callers
    use it last, after total() and the backup source, so a saved number never beats a fresher one.
    Undo: BOOK_DATE_SLACK = 0. Never raises."""
    try:
        d = str(match_date or "")[:10]
        if not d:
            return None, None, None
        for n in range(1, BOOK_DATE_SLACK + 1):
            for off in (n, -n):
                r = total(names, (date.fromisoformat(d) + timedelta(days=off)).isoformat())
                if r[0] is not None:
                    return r
    except Exception:
        pass
    return None, None, None


def book_of(names, match_date):
    """APEX-FONBET-2026-11-07: which book gave the logged total ('bet365' / 'fonbet'), else None. Never raises."""
    try:
        ix = _load()
        for k in _keys(names, str(match_date or "")[:10]):
            v = ix["log"].get(k)
            if v is not None:
                return v[3] if len(v) > 3 else "bet365"
    except Exception:
        pass
    return None


def log_total(names, match_date):
    """bet365 total from the log only (used to fill the props database). (line, over_prob) or (None, None)."""
    try:
        ix = _load()
        for k in _keys(names, match_date):
            v = ix["log"].get(k)
            if v is not None:
                return v[0], v[1]
    except Exception:
        pass
    return None, None


def log_has_any(max_age_hours=48):
    """True when the log has any row captured in the last max_age_hours (backup-mode check)."""
    try:
        from datetime import datetime
        cutoff = time.time() - max_age_hours * 3600
        return any(datetime.fromisoformat(v[2]).timestamp() >= cutoff for v in _load()["log"].values() if v[2])
    except Exception:
        return False


def refresh():
    """Force a reload on the next read (after the hourly fill writes the props database)."""
    _idx["at"] = 0
    _idx["sig"] = None
