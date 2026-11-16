#!/usr/bin/env python3
"""APEX-SGOBACKUP-2026-10-31 (owner, "Go sgo backup"): when bet365 has no total for a match, use the
SportsGameOdds pre-match total from the newest saved snapshot (data/sgo_archive/tennis_snapshots). Reads local
files only: no API calls, no key. Pre-match only: the event must not have started, its start must be ahead,
and the snapshot under MAX_AGE_MIN old. Total = median of Pinnacle, Novig, Polymarket, Circa; needs Pinnacle or
2+ of them; never PrizePicks' own line. Never replaces a bet365 number. ATP/WTA only, since SGO has no ITF.
Auto-off at OFF_AT, the end of the trial, and whenever snapshots stop. Undo: SGO_BACKUP_TOTALS = False."""
import glob, json, os, re, statistics, unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SGO_BACKUP_TOTALS = True
CT = ZoneInfo("America/Chicago")
OFF_AT = datetime(2026, 12, 7, 23, 59, tzinfo=CT)
MAX_AGE_MIN = 90
BOOKS = ("pinnacle", "novig", "polymarket", "circa")
HERE = os.path.dirname(os.path.abspath(__file__))
SNAPS = os.path.join(HERE, "data", "sgo_archive", "tennis_snapshots")
STATE = os.path.join(HERE, "data", "logs", "sgo_backup_state.json")
_MEM = {"file": None, "rows": []}
_SEEN = {}


def _name(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    w = re.sub(r"[^a-z ]", " ", s).split()
    return (w[-1], w[0][0]) if w else ("", "")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _rows():
    files = sorted(glob.glob(os.path.join(SNAPS, "*", "*.json")))
    if not files:
        return []
    f = files[-1]
    if _MEM["file"] == f:
        return _MEM["rows"]
    with open(f) as fh:
        d = json.load(fh)
    taken = datetime.fromisoformat(d["taken_at"])
    rows = []
    for e in d.get("events") or []:
        s = e.get("status") or {}
        o = (e.get("odds") or {}).get("games-all-game-ou-over") or {}
        bb = o.get("byBookmaker") or {}
        got = {b: _f((bb.get(b) or {}).get("overUnder")) for b in BOOKS if bb.get(b)}
        got = {b: v for b, v in got.items() if v is not None}
        if s.get("started") or s.get("live") or s.get("cancelled") or not s.get("startsAt"):
            continue
        if not got or not ("pinnacle" in got or len(got) >= 2):
            continue
        t = e.get("teams") or {}
        rows.append({"a": _name(t["home"]["names"]["long"]), "b": _name(t["away"]["names"]["long"]),
                     "start": datetime.fromisoformat(s["startsAt"].replace("Z", "+00:00")),
                     "total": statistics.median(got.values()), "books": sorted(got), "taken": taken})
    _MEM.update(file=f, rows=rows)
    return rows


def _note(names, day, value, books):
    """Remembers once per match per day, for the DATA CHECK line. Never raises."""
    key = "%s|%s" % (day, " v ".join(sorted(str(n) for n in names)))
    if _SEEN.get(key) is not None or (key in _SEEN and value is None):
        return
    _SEEN[key] = value
    try:
        try:
            with open(STATE) as fh:
                st = json.load(fh)
        except Exception:
            st = {}
        if st.get(key, {}).get("total") is None or value is not None:
            st[key] = {"total": value, "books": books}
            with open(STATE + ".tmp", "w") as fh:
                json.dump(st, fh, indent=0)
            os.replace(STATE + ".tmp", STATE)
    except Exception:
        pass


def total(names, match_date):
    """(total, [books]) or (None, None). Call only after bet365 came back empty. Never raises."""
    try:
        now = datetime.now(timezone.utc)
        if not SGO_BACKUP_TOTALS or now >= OFF_AT.astimezone(timezone.utc) or len(names) != 2:
            return None, None
        x, y = _name(names[0]), _name(names[1])
        day = str(match_date or "")[:10]
        base = datetime.fromisoformat(day).date() if day else None
        val, books = None, None
        for r in _rows():
            if now - r["taken"] > timedelta(minutes=MAX_AGE_MIN) or r["start"] <= now:
                continue
            if {x, y} != {r["a"], r["b"]}:
                continue
            if base and abs((r["start"].astimezone(CT).date() - base).days) > 1:
                continue
            val, books = r["total"], r["books"]
            break
        _note(names, day, val, books)
        return val, books
    except Exception:
        return None, None


def data_check_line():
    """'SGO backup filled N of M no-book-total matches' for today, or None. Local files only. Never raises."""
    try:
        if not SGO_BACKUP_TOTALS or datetime.now(timezone.utc) >= OFF_AT.astimezone(timezone.utc):
            return None
        today = datetime.now(CT).date().isoformat()
        with open(STATE) as fh:
            st = json.load(fh)
        rows = [v for k, v in st.items() if k[:10] >= today]
        if not rows:
            return None
        return "\U0001F4CF SGO backup filled %d of %d no-book-total matches" % (sum(v.get("total") is not None for v in rows), len(rows))
    except Exception:
        return None
