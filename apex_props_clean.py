#!/usr/bin/env python3
"""APEX-TENNISCLEAN-2026-11-12 (his "Go fix tennis data" after the 11/12 tennis audit).
Keeps the graded tennis lines in data/props_db.sqlite honest. Runs after every settle and is safe to run again.
  1. Two collectors, two results: a backfill row whose result differs from the live collector's score-backed result for the
     same player / match / stat takes the score-backed result.
  2. Retired matches: a backfill row graded on a match the warehouse shows as retired or a walkover (or a "Total Games"
     under 12, which no finished match can have) is thrown out, the same rule the live settler uses (VOID (RET)).
     Rows the live settler graded on purpose ("PP graded") are left alone.
  3. Late lines: a line first saved after the match started is thrown out.
  4. Copies: the same player / opponent / stat / type / line / day graded under two ids is counted once
     (live collector copy kept, then board-watch, then backfill).
Thrown-out rows are not deleted: result becomes 'void' and settle_note says why and what the result was.
Undo: ON = False (already-marked rows stay; restore the backup in _backups/tennis_fix_1112/ to bring them back)."""
import os, re, sqlite3, datetime as dt
ON = True
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "props_db.sqlite")
WH = os.path.join(HERE, "data", "warehouse", "tennis_warehouse.sqlite")
G = "result in ('more','less','push')"
RANK = {"partner_api": 0, "boardwatch_backfill": 1, "picklog_backfill": 2}

def _t(s):
    try:
        x = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None

def _void(c, pid, why, was):
    c.execute("update projections set result='void', more_won=null, less_won=null, settle_note=? where projection_id=?",
              ("%s (was %s)" % (why, was), pid))

def retired_matches(c):
    """{(frozenset(names), match_date)} of graded backfill matches that did not finish."""
    out = set()
    for p, o, d in c.execute("select distinct player, opponent, match_date from projections where category='total_games' "
                             "and actual < 12 and source='picklog_backfill' and " + G):
        out.add((frozenset((p, o)), d))
    if os.path.exists(WH):
        w = sqlite3.connect("file:%s?mode=ro" % WH, uri=True); W = {}
        for a, b, d, s in w.execute("select player_a, player_b, match_date, score from matches where match_date >= '2026-08-01' "
                                    "and score is not null"):
            if re.search(r"ret|w/o|def\b|\bwo\b", s.lower()): W[(frozenset((a, b)), d)] = s
        w.close()
        for p, o, d in c.execute("select distinct player, opponent, match_date from projections where source='picklog_backfill' and " + G):
            try: day = dt.date.fromisoformat(d)
            except Exception: continue
            for off in (0, -1, 1, -2, 2):
                if (frozenset((p, o)), (day + dt.timedelta(days=off)).isoformat()) in W: out.add((frozenset((p, o)), d)); break
    return out

def clean(c=None):
    if not ON: return {}
    own = c is None
    if own: c = sqlite3.connect(DB, timeout=60)
    n = dict(corrected=0, retired=0, late=0, copies=0)
    # 1. score-backed result wins
    live = {}
    for p, o, cat, d, a, st in c.execute("select player, opponent, category, match_date, actual, start_utc from projections "
                                         "where source='partner_api' and actual is not null and " + G):
        live.setdefault((p, o, cat, d), set()).add((a, (st or "")[:10]))
    for pid, p, o, cat, d, a, line, st, res in c.execute(
            "select projection_id, player, opponent, category, match_date, actual, coalesce(line_at_start, line_last), start_utc, result "
            "from projections where source != 'partner_api' and actual is not null and " + G).fetchall():
        v = live.get((p, o, cat, d))
        if not v or len(v) != 1: continue
        la, lday = next(iter(v))
        if la == a or line is None or (st and lday and st[:10] != lday): continue
        r = "push" if la == line else ("more" if la > line else "less")
        c.execute("update projections set actual=?, result=?, more_won=?, less_won=?, settle_note=? where projection_id=?",
                  (la, r, int(la > line), int(la < line), "result corrected to the score-backed collector (was %s, %s)" % (a, res), pid)); n["corrected"] += 1
    # 2. retired matches
    ret = retired_matches(c)
    for pid, p, o, d, res, note in c.execute("select projection_id, player, opponent, match_date, result, settle_note from projections "
                                             "where source='picklog_backfill' and " + G).fetchall():
        if (frozenset((p, o)), d) in ret and "PP graded" not in (note or ""):
            _void(c, pid, "VOID (RET, match did not finish)", res); n["retired"] += 1
    # 3. late lines
    for pid, st, fs, res in c.execute("select projection_id, start_utc, first_seen, result from projections where start_utc != '' and " + G).fetchall():
        a, b = _t(st), _t(fs)
        if a and b and b > a: _void(c, pid, "VOID (LATE LINE, first saved after the match started)", res); n["late"] += 1
    # 4. copies
    grp = {}
    for pid, p, o, cat, kind, line, d, src, res in c.execute(
            "select projection_id, player, opponent, category, odds_type, line_last, match_date, source, result from projections where " + G):
        grp.setdefault((p, o, cat, kind, line, d), []).append((RANK.get(src, 9), pid, res))
    for rows in grp.values():
        if len(rows) < 2: continue
        rows.sort()
        for _, pid, res in rows[1:]: _void(c, pid, "VOID (COPY of %s)" % rows[0][1], res); n["copies"] += 1
    c.commit()
    if own: c.close()
    return n

if __name__ == "__main__": print(clean())
