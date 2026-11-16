#!/usr/bin/env python3
"""APEX-REPLAY-2026-11-04 (owner, "Go testing standard"): the one replay every rule test runs through.
Read-only. Official board = last board posted before the day's first match (a board saved more than a day
before the day it is labeled for is ignored: APEX-REPLAYDAY-2026-11-04, publish 1266 was labeled Nov 3 on Nov 1). Pool = every pick on a numbered or
BACKUP spot of Top 3 / Elite 6 / Top 10 in that board (held picks stay out). A rule file may define
    keep(pick) -> bool      drop picks from numbered spots (they go to BACKUPS)
    key(pick)  -> sort key  change the order (default: Ranking, then Win Prob)
Reports per-pick record, whole-board days (Top 3 / Elite 5 / Elite 6), both halves, and every day that changed.
Usage: python3 tools/apex_replay.py [--start 2026-10-13] [--end today] [--rule file.py] [--quiet]
"""
import sqlite3, re, unicodedata, datetime as dt, collections, sys, os, json, importlib.util
from zoneinfo import ZoneInfo
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CT = ZoneInfo('America/Chicago')
def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z ]', '', s.replace('-', ' ')).strip()
def ct(ms):
    try: return dt.datetime.fromisoformat(ms).astimezone(CT).replace(tzinfo=None)
    except Exception: return None
def name(r): return r['opponent'] if r['player'] == 'match' else r['player']
def matchkey(r):
    return frozenset(norm(x) for x in re.split(r'\s+v\s+|\s+vs\s+', r['opponent'])) if r['player'] == 'match' else frozenset([norm(r['player']), norm(r['opponent'] or '')])

class Replay:
    def __init__(self, start, end):
        self.c = sqlite3.connect('file:%s?mode=ro' % os.path.join(HERE, 'data', 'board_history.sqlite'), uri=True); self.c.row_factory = sqlite3.Row
        p = sqlite3.connect('file:%s?mode=ro' % os.path.join(HERE, 'data', 'props_db.sqlite'), uri=True)
        self.res = {}
        for pl, op, md, cat, line, result in p.execute("select player, opponent, match_date, category, line_last, result from projections where result in ('more','less','void','push') and match_date >= ?", ((dt.date.fromisoformat(start) - dt.timedelta(days=2)).isoformat(),)):
            self.res[(frozenset((norm(pl), norm(op))), cat, float(line), md)] = result
            self.res[(norm(pl), cat, float(line), md)] = result
        hp = os.path.join(HERE, 'tools', 'replay_hand_grades.json')
        self.hand = {}
        if os.path.exists(hp):
            for k, v in json.load(open(hp)).items():
                n, cat, line = k.split('|'); self.hand[(norm(n), cat, float(line))] = v
        self.start, self.end = start, end
    def grade1(self, r):
        if r['result'] in ('hit', 'miss', 'void', 'push'): return r['result']
        day = (r['match_start'] or '')[:10]
        if not day: return None
        for d in (day, (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat(), (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()):
            key = (frozenset(norm(x) for x in re.split(r'\s+v\s+|\s+vs\s+', r['opponent'])), r['category'], float(r['line']), d) if r['player'] == 'match' else (norm(r['player']), r['category'], float(r['line']), d)
            rr = self.res.get(key)
            if rr in ('void', 'push'): return rr
            if rr: return 'hit' if (rr == 'more') == (r['direction'] == 'MORE') else 'miss'
        return self.hand.get((norm(name(r)), r['category'], float(r['line'])))
    def grade(self, legs):
        g = [self.grade1(r) for r in legs]
        if any(x == 'miss' for x in g): return 'lost'
        if any(x is None for x in g): return 'open'
        return 'won'
    def days(self):
        out = []
        for (day,) in self.c.execute("select distinct board_day from publishes where board_day between ? and ? order by 1", (self.start, self.end)):
            starts = [ct(r[0]) for r in self.c.execute("select distinct match_start from history h join publishes p using(publish_id) where p.board_day=? and date(p.published_at) >= date(?, '-1 day') and h.board in ('top3','elite6') and h.spot like '#%' and h.match_start is not null", (day, day))]
            starts = [s for s in starts if s and s.date() >= dt.date.fromisoformat(day) - dt.timedelta(days=1)]
            if not starts: continue
            pub = self.c.execute("select publish_id, published_at from publishes p where board_day=? and date(published_at) >= date(?, '-1 day') and published_at<? and exists(select 1 from history h where h.publish_id=p.publish_id and h.board='top3' and h.spot='#1') order by published_at desc limit 1", (day, day, min(starts).isoformat())).fetchone()
            if not pub: continue
            rows = self.c.execute("select * from history where publish_id=? and board in ('top3','elite6','top10') and spot_kind in ('main','backup') and win_prob is not null", (pub['publish_id'],)).fetchall()
            posted = {b: sorted([r for r in rows if r['board'] == b and r['spot_kind'] == 'main'], key=lambda r: r['spot_no'] or 0) for b in ('top3', 'elite6')}
            if not posted['top3'] and not posted['elite6']: continue
            pool = {}
            for r in rows:
                k = (norm(name(r)), r['category'], float(r['line']), r['direction'])
                pool.setdefault(k, r)
            out.append((day, pub['published_at'], posted, list(pool.values())))
        return out
    def build(self, pool, base, keep=None, key=None):
        """key given: rebuild the whole board from the pool in that order (then keep()).
        keep only: start from the posted board, drop what fails keep(), fill from the pool in Ranking order."""
        dkey = lambda r: (-(r['rank_score'] or 0), -r['win_prob'])
        if key is not None:
            cand = sorted([r for r in pool if (keep is None or keep(r))], key=key)
            new, used = [], set()
        else:
            new = [r for r in base if (keep is None or keep(r))]
            used = {matchkey(r) for r in base}          # never refill with another line of a dropped match
            cand = sorted([r for r in pool if (keep is None or keep(r))], key=dkey)
        for r in cand:
            mk = matchkey(r)
            if mk in used or any(r is x for x in new): continue
            used.add(mk); new.append(r)
            if len(new) >= 6: break
        return new
    def run(self, keep=None, key=None, quiet=False):
        days = self.days()
        if not days: print('no days'); return
        mid = days[len(days) // 2][0]
        half = lambda d: 'older' if d < mid else 'newer'
        tot = {b: collections.defaultdict(collections.Counter) for b in ('top3', 'elite5', 'elite6')}
        picks = collections.defaultdict(collections.Counter)
        changed = []
        for day, pub_at, posted, pool in days:
            for b, n in (('top3', 3), ('elite5', 5), ('elite6', 6)):
                old = posted['top3'] if b == 'top3' else posted['elite6'][:n]
                if not old: continue
                new = self.build(pool, posted['top3'] if b == 'top3' else posted['elite6'], keep, key)
                m = min(n, len(old))
                bo, bn = self.grade(old), (self.grade(new[:m]) if len(new) >= m else 'short')
                for tag, legs in (('before', old), ('after', new[:m])):
                    for r in legs:
                        g = self.grade1(r)
                        if g in ('hit', 'miss'): picks[(b, tag, half(day))][g] += 1
                tot[b][half(day)]['before ' + bo] += 1
                tot[b][half(day)]['after ' + bn] += 1
                if bo != bn:
                    changed.append('%s %-6s %s -> %s | out: %s | in: %s' % (day, b, bo, bn,
                        ', '.join('%s %s %s' % (name(r), r['line'], self.grade1(r)) for r in old if not any(name(r) == name(x) and r['line'] == x['line'] for x in new[:m])),
                        ', '.join('%s %s WP%.0f %s' % (name(r), r['line'], r['win_prob'], self.grade1(r)) for r in new[:m] if not any(name(r) == name(x) and r['line'] == x['line'] for x in old))))
        print('REPLAY %s to %s, %d days, halves split at %s (official board = last board before first match)' % (self.start, self.end, len(days), mid))
        for b in ('top3', 'elite5', 'elite6'):
            for tag in ('before', 'after'):
                s = ['%s %-6s %-6s' % (b.upper() if tag == 'before' else '', tag.upper(), '')]
                for h in ('older', 'newer', 'all'):
                    c = tot[b]['older'] + tot[b]['newer'] if h == 'all' else tot[b][h]
                    pk = picks[(b, tag, 'older')] + picks[(b, tag, 'newer')] if h == 'all' else picks[(b, tag, h)]
                    s.append('%s: days won %d lost %d open %d short %d | picks %d-%d' % (h, c[tag + ' won'], c[tag + ' lost'], c[tag + ' open'], c[tag + ' short'], pk['hit'], pk['miss']))
                print('  '.join(s))
        if not quiet:
            print('\nDAYS THAT CHANGED:'); print('\n'.join(changed) if changed else '  none')

if __name__ == '__main__':
    a = sys.argv[1:]
    start = a[a.index('--start') + 1] if '--start' in a else '2026-10-13'
    end = a[a.index('--end') + 1] if '--end' in a else dt.date.today().isoformat()
    keep = key = None
    if '--rule' in a:
        spec = importlib.util.spec_from_file_location('rule', a[a.index('--rule') + 1]); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        keep = getattr(mod, 'keep', None); key = getattr(mod, 'key', None)
        print('RULE:', (mod.__doc__ or '').strip())
    Replay(start, end).run(keep, key, quiet='--quiet' in a)
