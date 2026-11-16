#!/usr/bin/env python3
"""APEX-REBUILD-2026-11-04 (owner: "back fill the ranking then run the correct test, make sure it's no bias").
Rebuilds every old day's board from ALL scored props (the All Props card data stored at each publish) using TODAY's
rules and TODAY's Ranking, so a rule test does not depend on which rules were live that day.
Official publish = last publish (with All Props rows) before the day's first match.
Today's rules applied to every prop: Win Prob 70+, Apex 67+, type floors (Demon 62 / Standard 68), Breaks off,
Total Games needs a book total and 4+ games of room, over-17.5 check must say play, Fantasy LESS held, Double Fault
overs held, no Total Games on 75+ favorites, Alycia Parks banned, players with fewer than 5 main-tour matches off,
one line per match. Ranking = Win Prob + 5 per net signal (blowout counts double), Fantasy signals display only,
Aces favorite +1 only. First-set props are NOT in the All Props store, so they are left out everywhere.
Usage: python3 tools/apex_rebuild.py [--start 2026-10-13] [--end today] [--rule file.py] [--bias] [--quiet]
"""
import sqlite3, re, unicodedata, datetime as dt, collections, sys, os, json, importlib.util, bisect
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apex_replay import Replay, norm, ct, name, matchkey
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANNED = {'alycia parks'}

class Rebuild(Replay):
    def __init__(self, start, end):
        super().__init__(start, end)
        p = sqlite3.connect('file:%s?mode=ro' % os.path.join(HERE, 'data', 'props_db.sqlite'), uri=True)
        self.fav = {}
        for pl, op, md, fv in p.execute("select player, opponent, match_date, pinn_fav from projections where pinn_fav is not null and match_date >= ?", (start,)):
            self.fav.setdefault((frozenset((norm(pl), norm(op))), md), float(fv))
        h = sqlite3.connect('file:%s?mode=ro' % os.path.join(HERE, 'data', 'player_history_filled.sqlite'), uri=True)
        self.hist = collections.defaultdict(list)
        for a, b, d in h.execute("select player_a, player_b, match_date from matches where tour in ('ATP','WTA')"):
            self.hist[norm(a)].append(d[:10]); self.hist[norm(b)].append(d[:10])
        for k in self.hist: self.hist[k].sort()
    def main_tour_matches(self, player, day):
        """matches on file for the thin-player rule: the live checker's own count when it can be loaded, else main-tour history"""
        try:
            if not hasattr(self, '_pc'):
                sys.path.insert(0, HERE); import apex_player_checks as _pc; self._pc = _pc
            n = self._pc.matches_on_file(player)
            if n: return n
        except Exception:
            pass
        v = self.hist.get(norm(player)) or []
        return bisect.bisect_left(v, day)
    def names(self, r):
        return [x.strip() for x in re.split(r'\s+v\s+|\s+vs\s+', r['opponent'])] if r['player'] == 'match' else [r['player']]
    def favorite(self, r, day):
        if r['favorite_pct'] is not None: return float(r['favorite_pct'])
        ns = [norm(x) for x in self.names(r)]
        if r['player'] != 'match': ns.append(norm(r['opponent'] or ''))
        for d in (day, (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat(), (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()):
            f = self.fav.get((frozenset(ns), d))
            if f is not None: return 100 * max(f, 1 - f)
        return None
    def hold(self, r, day):
        """today's rules -> None if eligible for a numbered spot, else the reason (held = BACKUPS, off = not eligible at all)"""
        wp = r['win_prob'] or 0; cat = r['category']; pt = str(r['prop_type'] or '').lower(); dr = str(r['direction'] or '').upper()
        rs = str(r['reason'] or '')
        if wp < 70: return 'off: under 70'
        if r['apex_score'] is not None and r['apex_score'] < 67: return 'off: Apex under 67'
        if 'Apex Score under' in rs: return 'off: Apex under 67'
        if cat in ('breaks',): return 'off: breaks'
        if 'below category minimum' in rs or 'kept off boards' in rs: return 'off: type floor'
        if pt == 'demon' and wp < 62: return 'off: demon floor'
        if pt == 'standard' and wp < 68: return 'off: standard floor'
        ns = self.names(r)
        if any(norm(x) in BANNED for x in ns + ([r['opponent']] if r['player'] != 'match' else [])): return 'off: banned'
        if any(self.main_tour_matches(x, day) < 5 for x in ns): return 'off: lower-level player'
        if cat == 'total_games' and dr == 'MORE':
            if r['bet365_total'] is None: return 'held: no book total'
            if float(r['bet365_total']) - float(r['line']) < 4 - 1e-9: return 'held: under 4 games of room'
            if float(r['line']) == 17.5 and (str(r['line175'] or '') == 'skip' or r['not_enough_data'] not in (None, '')): return 'held: 17.5 check'
            f = self.favorite(r, day)
            if f is not None and f >= 75: return 'held: 75+ favorite'
        if cat == 'fantasy_score' and dr == 'LESS': return 'held: Fantasy LESS'
        if cat in ('dfs', 'double_faults') and dr == 'MORE': return 'held: Double Fault over'
        return None
    def rank_today(self, r):
        wp = float(r['win_prob'] or 0); cat = r['category']; dr = str(r['direction'] or '').upper()
        if cat == 'total_games' and dr == 'MORE':
            sig = [r['sig_pp'], r['sig_room'], r['sig_blowout'], r['sig_fav'], r['sig_played']]
            net = sum((-2 if (k == 2 and v is not None and v < 0) else (v or 0)) for k, v in enumerate(sig))
            return wp + 5 * net
        if cat == 'aces' and dr == 'MORE':
            return wp + 5 * (1 if (r['sig_fav'] or 0) > 0 else 0)
        return wp
    def official(self, day):
        starts = [ct(x[0]) for x in self.c.execute("select distinct match_start from history h join publishes p using(publish_id) where p.board_day=? and date(p.published_at) >= date(?, '-1 day') and h.board in ('top3','elite6') and h.spot like '#%' and h.match_start is not null", (day, day))]
        starts = [s for s in starts if s and s.date() >= dt.date.fromisoformat(day) - dt.timedelta(days=1)]
        if not starts: return None
        pub = self.c.execute("select p.publish_id, p.published_at from publishes p where p.board_day=? and date(p.published_at) >= date(?, '-1 day') and p.published_at<? and exists(select 1 from history h where h.publish_id=p.publish_id and h.board='allprops') order by p.published_at desc limit 1", (day, day, min(starts).isoformat())).fetchone()
        return pub
    def days(self):
        out = []
        for (day,) in self.c.execute("select distinct board_day from publishes where board_day between ? and ? order by 1", (self.start, self.end)):
            pub = self.official(day)
            if not pub: continue
            rows = self.c.execute("select * from history where publish_id=? and board in ('top3','elite6') and spot_kind='main'", (pub['publish_id'],)).fetchall()
            posted = {b: sorted([r for r in rows if r['board'] == b], key=lambda r: r['spot_no'] or 0) for b in ('top3', 'elite6')}
            allp = self.c.execute("select * from history where publish_id=? and board='allprops' and win_prob is not null", (pub['publish_id'],)).fetchall()
            pool = []
            seen = set()
            for r in allp:
                k = (norm(name(r)), r['category'], float(r['line']), r['direction'])
                if k in seen: continue
                seen.add(k)
                if self.hold(r, day) is None: pool.append(r)
            if not pool: continue
            if not self.keep_posted:   # tests compare against the rebuilt (today's rules) board, not the old posted one
                base = self.build(pool, None)
                posted = {'top3': base[:3], 'elite6': base[:6]}
            out.append((day, pub['published_at'], posted, pool))
        return out
    def build(self, pool, base, keep=None, key=None):
        key = key or (lambda r: (-self.rank_today(r), -r['win_prob']))
        cand = sorted([r for r in pool if (keep is None or keep(r))], key=key)
        new, used = [], set()
        for r in cand:
            mk = matchkey(r)
            if mk in used: continue
            used.add(mk); new.append(r)
            if len(new) >= 6: break
        return new
    keep_posted = False
    def bias(self):
        """rebuilt board vs the real posted board, day by day"""
        self.keep_posted = True
        for day, pub_at, posted, pool in self.days():
            new = self.build(pool, None)
            post6 = [name(r) + ' ' + str(r['line']) for r in posted['elite6'] if r['category'] != '1st_set_total_games']
            reb6 = [name(r) + ' ' + str(r['line']) for r in new[:6]]
            same = len(set(post6) & set(reb6))
            print('%s posted(%s): %s' % (day, pub_at[11:16], ' | '.join(post6)))
            print('%s rebuilt      : %s   -> %d of %d match' % (day, ' | '.join(reb6), same, len(post6)))

if __name__ == '__main__':
    a = sys.argv[1:]
    start = a[a.index('--start') + 1] if '--start' in a else '2026-10-13'
    end = a[a.index('--end') + 1] if '--end' in a else dt.date.today().isoformat()
    R = Rebuild(start, end)
    if '--bias' in a:
        R.bias(); sys.exit()
    keep = key = None
    if '--rule' in a:
        spec = importlib.util.spec_from_file_location('rule', a[a.index('--rule') + 1]); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        keep = getattr(mod, 'keep', None); key = getattr(mod, 'key', None)
        print('RULE:', (mod.__doc__ or '').strip())
    R.run(keep, key, quiet='--quiet' in a)
