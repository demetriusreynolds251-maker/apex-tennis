#!/usr/bin/env python3
"""APEX-PLAYERDATA-2026-10-31 (owner, "Go player data live"). A filled copy of the match history for the
blowout, form, head-to-head and 17.5 check signals. The warehouse only follows its seed list, so 94 of 224 board
players had nothing after 10/15. This adds saved PrizePicks settled results and the saved SGO archive, and joins
name variants only on clear evidence. Win Prob reads a separate player file and does not use this.
Report: _backups/player_data_test_2026-10-31/report.md. Undo: PLAYER_HISTORY_FILLED = False, restart."""
import glob
import json
import os
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

PLAYER_HISTORY_FILLED = True
HERE = os.path.dirname(os.path.abspath(__file__))
WAREHOUSE = os.path.join(HERE, "data", "warehouse", "tennis_warehouse.sqlite")
PROPS_DB = os.path.join(HERE, "data", "props_db.sqlite")
BOARD_DB = os.path.join(HERE, "data", "board_history.sqlite")
SGO_DIR = os.path.join(HERE, "data", "sgo_archive", "tennis")
FILLED = os.path.join(HERE, "data", "player_history_filled.sqlite")
SGO_CACHE = os.path.join(HERE, "data", "player_history_sgo.json")
DECISIONS = os.path.join(HERE, "data", "player_name_decisions.json")
STATE = os.path.join(HERE, "data", "logs", "player_history_state.json")
START = "2024-06-01"
RUN_AT = "08:30"          # after the morning grading
SAME_MATCH_DAYS = 2
SOURCE_DATE_DRIFT = 21    # the warehouse can date a match by its event start, weeks off


def path(fallback=WAREHOUSE):
    """The history file the signals should read."""
    return FILLED if PLAYER_HISTORY_FILLED and os.path.exists(FILLED) else fallback


def _ro(p):
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True)


def _tokens(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().replace("-", " ")
    return re.sub(r"[^a-z ]", " ", s).split()


def _games(score):
    t = 0
    for a, b in re.findall(r"(\d+)-(\d+)", score or ""):
        a, b = int(a), int(b)
        if max(a, b) >= 10:
            continue
        t += a + b
    return t or None


def _sgo_rows():
    """Finished matches from the saved SGO archive, cached. The archive stops growing when the trial ends."""
    newest = 0.0
    for d in glob.glob(os.path.join(SGO_DIR, "*", "*")):
        newest = max(newest, os.path.getmtime(d))
    if os.path.exists(SGO_CACHE) and os.path.getmtime(SGO_CACHE) >= newest:
        with open(SGO_CACHE) as fh:
            return json.load(fh)
    out = []
    for f in glob.glob(os.path.join(SGO_DIR, "*", "*", "*.json")):
        try:
            with open(f) as fh:
                e = json.load(fh)
            s = e["status"]
            if not s.get("finalized") or s.get("cancelled") or s["startsAt"][:10] < START:
                continue
            r = e.get("results") or {}
            g = r.get("game") or {}
            hs, aw = (g.get("home") or {}).get("points"), (g.get("away") or {}).get("points")
            if hs is None or aw is None or max(hs, aw) < 2:
                continue
            parts, tot = [], 0
            for i in range(1, 6):
                x = r.get("%ds" % i) or {}
                if not x:
                    continue
                a, b = (x.get("home") or {}).get("games"), (x.get("away") or {}).get("games")
                if a is None or b is None:
                    parts = []
                    break
                parts.append("%d-%d" % (a, b))
                tot += a + b
            if not parts:
                continue
            h, w = e["teams"]["home"]["names"]["long"], e["teams"]["away"]["names"]["long"]
            out.append(["sgo", h, w, h if hs > aw else w, s["startsAt"][:10], " ".join(parts), tot, 0, e.get("leagueID"), None])
        except Exception:
            continue
    with open(SGO_CACHE, "w") as fh:
        json.dump(out, fh)
    return out


def _load_rows():
    M = []
    con = _ro(WAREHOUSE)
    for a, b, w, d, sc, tg, ret, tour, tn in con.execute(
            "select player_a,player_b,winner,match_date,score,total_games,retired,tour,tournament from matches where match_date>=?", (START,)):
        M.append(["wh", a, b, w, d[:10], sc, tg, ret, tour, tn])
    con.close()
    M += _sgo_rows()
    con = _ro(PROPS_DB)
    seen = set()
    for pl, op, day, sc in con.execute("select player,opponent,match_date,score from projections where coalesce(score,'')!='' "
                                       "and player is not null and opponent is not null and category not like '1st%'"):
        k = (frozenset((pl, op)), day)
        if k in seen or not day:
            continue
        seen.add(k)
        sw = sl = 0
        for x, y in re.findall(r"(\d+)-(\d+)", sc):
            x, y = int(x), int(y)
            if max(x, y) >= 10:
                continue
            sw += x > y
            sl += y > x
        if max(sw, sl) < 2:
            continue
        M.append(["pdb", pl, op, pl if sw > sl else op, day[:10], sc, _games(sc), 0, None, None])
    con.close()
    return M


def _decisions():
    try:
        with open(DECISIONS) as fh:
            d = json.load(fh)
    except Exception:
        d = {}
    return {frozenset(x) for x in d.get("merge", [])}, {frozenset(x) for x in d.get("separate", [])}


def _name_groups(M):
    """Same spelling (accents, order) always joins. A longer and shorter name join only when both list the same
    match; two names in different events on one date stay apart; no evidence goes to owner."""
    names = Counter()
    played = defaultdict(list)
    for m in M:
        names[m[1]] += 1
        names[m[2]] += 1
        played[m[1]].append((m[4], m[2], m[9]))
        played[m[2]].append((m[4], m[1], m[9]))
    par = {n: n for n in names}

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    def join(a, b):
        par[find(a)] = find(b)

    same = defaultdict(list)
    for n in names:
        same[" ".join(sorted(_tokens(n)))].append(n)
    for v in same.values():
        for x in v[1:]:
            join(v[0], x)
    full = {" ".join(_tokens(n)): n for n in names}
    merge, separate = _decisions()
    unsure, joined, kept = [], [], []
    for key, n in sorted(full.items()):
        t = key.split()
        if len(t) < 3:
            continue
        for short in {" ".join(t[:2]), t[0] + " " + t[-1]}:
            o = full.get(short)
            if not o or find(o) == find(n):
                continue
            pair = frozenset((n, o))
            if pair in separate:
                kept.append(sorted(pair))
                continue
            if pair in merge:
                join(n, o)
                joined.append(sorted(pair))
                continue
            A, B = played[n], played[o]
            clash = any(d1 == d2 and t1 and t2 and not set(_tokens(t1)) & set(_tokens(t2))
                        and _tokens(o1)[-1:] != _tokens(o2)[-1:] for d1, o1, t1 in A for d2, o2, t2 in B)
            shared = any(abs((date.fromisoformat(d1) - date.fromisoformat(d2)).days) <= SOURCE_DATE_DRIFT
                         and (_tokens(o1) or ["?"])[-1] == (_tokens(o2) or ["!"])[-1]
                         for d1, o1, _ in A for d2, o2, _ in B)
            if clash:
                kept.append(sorted(pair))
            elif shared:
                join(n, o)
                joined.append(sorted(pair))
            else:
                unsure.append(sorted(pair))
    groups = defaultdict(set)
    for n in names:
        groups[find(n)].add(n)
    return find, groups, joined, kept, unsure


def build():
    """Rewrites the filled copy in one step. Returns a summary dict. Never touches the warehouse."""
    M = _load_rows()
    find, groups, joined, kept, unsure = _name_groups(M)
    order = {"wh": 0, "pdb": 1, "sgo": 2}
    M.sort(key=lambda m: order[m[0]])
    seen = defaultdict(list)
    keep = []
    for m in M:
        k = frozenset((find(m[1]), find(m[2])))
        d = date.fromisoformat(m[4])
        tg = m[6] or _games(m[5])
        dup = False
        for od, otg in seen[k]:
            gap = abs((d - od).days)
            if gap <= SAME_MATCH_DAYS or (gap <= SOURCE_DATE_DRIFT and tg and otg == tg):
                dup = True
                break
        if dup:   # APEX-HISTDEDUPE-2026-11-01: warehouse duplicates too (Tabilo v Zverev 09-05 and 09-06)
            continue
        seen[k].append((d, tg))
        keep.append(m)
    rows = []
    for s, a, b, w, d, sc, tg, ret, tour, tn in keep:
        ca, cb = find(a), find(b)
        wg = find(w) if w else None
        # One row per match for any name asked about: the main row, plus a row for each other spelling
        # with the opponent marked "~" so the opponent's own lookup does not see the match twice.
        rows.append((ca, cb, (ca if wg == ca else cb) if wg else None, d, sc, tg, ret, tour, tn, s))
        for x, opp, side in [(v, "~" + cb, ca) for v in groups[ca] if v != ca] + [(v, "~" + ca, cb) for v in groups[cb] if v != cb]:
            rows.append((x, opp, (x if wg == side else opp) if wg else None, d, sc, tg, ret, tour, tn, s))
    tmp = FILLED + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    o = sqlite3.connect(tmp)
    o.execute("create table matches(player_a,player_b,winner,match_date,score,total_games,retired,tour,tournament,src)")
    o.execute("create table variants(name,grp)")
    o.executemany("insert into matches values(?,?,?,?,?,?,?,?,?,?)", rows)
    o.executemany("insert into variants values(?,?)", [(n, g) for g, v in groups.items() for n in v])
    o.execute("create index ma on matches(player_a,match_date)")
    o.execute("create index mb on matches(player_b,match_date)")
    o.execute("create index vn on variants(name)")
    o.commit()
    o.close()
    os.replace(tmp, FILLED)
    return {"matches": len(keep), "added": sum(m[0] != "wh" for m in keep), "joined": joined, "kept": kept, "unsure": unsure}


def elo_variants(elo, norm):
    """Gives name variants one rating for the 17.5 check. Returns a new dict."""
    if not PLAYER_HISTORY_FILLED or not os.path.exists(FILLED):
        return elo
    out = dict(elo)
    con = _ro(FILLED)
    groups = defaultdict(list)
    for n, g in con.execute("select name,grp from variants"):
        groups[g].append(n)
    con.close()
    for ns in groups.values():
        v = [elo.get(norm(n)) for n in ns if elo.get(norm(n)) is not None]
        if v:
            for n in ns:
                out.setdefault(norm(n), v[0])
    return out


def _board_players():
    """(names on today's newest Top 3 / Elite 6, names on those boards the last 3 days)."""
    con = _ro(BOARD_DB)
    p = con.execute("select max(publish_id) from publishes where platform='prizepicks'").fetchone()[0]
    since = (date.today() - timedelta(days=3)).isoformat()
    def names(rows):
        out = set()
        for a, o in rows:
            out.update([x.strip() for x in (o or "").split(" v ")] if a == "match" else [a, o])
        return {x for x in out if x}
    q = "select distinct player,opponent from history where board in('top3','elite6') and spot_kind='main' and "
    now = names(con.execute(q + "publish_id=?", (p,)))
    recent = names(con.execute(q + "board_day>=?", (since,)))
    con.close()
    return now, recent


def _missing(players):
    """{player: newest graded PrizePicks match date not in the filled history}. Complete matches only."""
    if not os.path.exists(FILLED):
        return {n: "no file" for n in players}
    pdb, f = _ro(PROPS_DB), _ro(FILLED)
    since = (date.today() - timedelta(days=14)).isoformat()
    out = {}
    for n in players:
        r = pdb.execute("select max(match_date) from projections where (player=? or opponent=?) and coalesce(score,'')!='' "
                        "and more_won is not null and match_date>=?", (n, n, since)).fetchone()[0]
        if not r:
            continue
        lo = (date.fromisoformat(r) - timedelta(days=SOURCE_DATE_DRIFT)).isoformat()
        if not f.execute("select 1 from matches where (player_a=? or player_b=?) and match_date>=? limit 1", (n, n, lo)).fetchone():
            out[n] = r
    pdb.close()
    f.close()
    return out


def status_line():
    """Player history: X of Y board players up to date. Never raises."""
    try:
        if not PLAYER_HISTORY_FILLED:
            return None
        now, recent = _board_players()
        allp = now | recent
        miss = _missing(allp)
        line = "\U0001F4C7 Player history: %d of %d board players up to date" % (len(allp) - len(miss), len(allp))
        return line + (" -- missing: " + ", ".join(sorted(miss)) if miss else "")
    except Exception as e:
        return "⚠️ Player history check failed: %s" % type(e).__name__


def _state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _alert(title, desc):
    import apex_alert_cards as C
    C.post(C.make(title, color=C.ORANGE, desc=desc))


def run(force=False):
    """Daily job: rebuild after morning grading, one #system line, change-only #alerts. Catch-up safe. Never raises."""
    try:
        if not PLAYER_HISTORY_FILLED:
            return "off"
        st = _state()
        today = date.today().isoformat()
        if not force and (st.get("day") == today or datetime.now().strftime("%H:%M") < RUN_AT):
            return "nothing due"
        before = st.get("matches") or 0
        s = build()
        import apex_ops as ops
        if before and s["matches"] < before * 0.95:
            _alert("⚠️ Player history shrank", "Matches went from %d to %d. Check the sources." % (before, s["matches"]))
        now, recent = _board_players()
        miss = _missing(now | recent)
        old = set(st.get("missing") or [])
        new = sorted(set(miss) - old)
        if new:
            _alert("⚠️ Player history missing a graded match", "\n".join("%s -- graded match %s" % (n, miss[n]) for n in new))
        asked = {tuple(x) for x in (st.get("asked") or [])}
        ask = [x for x in s["unsure"] if tuple(x) not in asked]
        if ask:
            _alert("❓ Same player? Name check", "Kept apart until you say:\n" + "\n".join("%s / %s" % tuple(x) for x in ask[:25]))
        st.update(day=today, matches=s["matches"], missing=sorted(miss), asked=sorted(asked | {tuple(x) for x in ask}),
                  ran=datetime.now().isoformat(timespec="seconds"))
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as fh:
            json.dump(st, fh)
        added = s["matches"] - before if before else 0
        allp = now | recent
        ops.send_discord("system", "\U0001F4C7 Player history updated: %d new matches, %d of %d board players up to date"
                         % (added, len(allp) - len(miss), len(allp)))
        return "updated"
    except Exception as e:
        try:
            _alert("⚠️ Player history job failed", "%s: %s" % (type(e).__name__, e))
        except Exception:
            pass
        return "failed %s" % type(e).__name__


if __name__ == "__main__":
    b = build()
    print({k: (v if not isinstance(v, list) else len(v)) for k, v in b.items()})
    for k in ("joined", "kept", "unsure"):
        print(k, b[k][:80])
    print(status_line())
