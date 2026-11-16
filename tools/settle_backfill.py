#!/usr/bin/env python3
"""APEX-SETTLE-FALLBACK-2026-11-04: one-off backfill. Grades still-open score-only props (Total Games, Games Won, sets,
first-set) since --since from the warehouse final score, no stats-site calls. --apply writes; default is a dry run."""
import sys, os, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import apex_props_db as P
since = sys.argv[sys.argv.index('--since') + 1] if '--since' in sys.argv else '2026-10-13'
apply = '--apply' in sys.argv
c = P._conn()
rows = c.execute("""SELECT projection_id, player, opponent, category, stat_slug, COALESCE(line_at_start, line_last), match_date, start_utc
                    FROM projections WHERE settled_ts IS NULL AND match_date >= ? AND match_date < date('now') AND player NOT LIKE '%/%'
                    AND source IN ('partner_api','boardwatch_backfill')""", (since,)).fetchall()
print('open props since', since, ':', len(rows))
n = v = 0; seen = {}
now = P._now().isoformat(timespec='seconds')
for pid, pl, op, cat, slug, line, md, st in rows:
    opp = P._real_opponent(op)
    key = (P._norm(pl), P._norm(opp), cat, slug, md)
    if key not in seen:
        seen[key] = P._wh_value(cat, slug, pl, opp, md, '3')
    val, why, text = seen[key]
    if val is None and not str(why).startswith('VOID'):
        continue
    if val is None:
        res, mw, lw = 'void', None, None; v += 1
    else:
        res = 'push' if val == line else ('more' if val > line else 'less'); mw, lw = int(val > line), int(val < line); n += 1
    if n + v <= 40 or '--all' in sys.argv:
        print(' ', md, pl if cat not in P.MATCH_LEVEL else '', opp, cat, line, '->', val, res, why, text)
    if apply:
        c.execute("UPDATE projections SET actual=?, score=?, result=?, more_won=?, less_won=?, settle_note=?, settled_ts=? WHERE projection_id=?",
                  (val, text, res, mw, lw, why, now, pid))
if apply:
    c.commit()
c.close()
print('graded %d, void %d%s' % (n, v, '' if apply else '  (dry run, nothing written)'))
