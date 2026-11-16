#!/usr/bin/env python3
"""APEX-STARDEMON-2026-11-13 (owner: "I only want the most consistent star ... in their own board with the star demon card",
then "Go star demon board for tennis server"). /stardemon in the tennis server: NBA STAR DEMON card.

Only the most consistent stars: stars (top 1-2 on the team, 30+ minutes) whose own first-Demon record beat Demons of the same height
by 5+ per 100 in BOTH past seasons (15+ lines each). The list is built from real lines by
~/openclaw/_backups/star_demons_1113/margins/build_star_list.py and saved in data/star_demon_list.json (re-run that script to rebuild).
For each listed star in the NBA saved lines: his LOWEST Demon (MORE) on his best stat by his own record; a star whose lowest Demon is
more than 2 above his regular line is left off; one star per game; injury-listed players skipped. Read only on NBA (same subprocess
pattern as /bestless); nothing is pulled from PrizePicks.
Every showing is logged (data/star_demon_log.jsonl); after the 4 AM NBA grading the legs are graded from the NBA tracker's own results
(read only) and one line goes to #system; the board shows the running record. Display only: no other board, rating, floor or rule.
Turn off: ON = False, restart the tennis bot. Undo: git checkout before-star-demon-board (openclaw)."""
import datetime as dt, json, os, sqlite3, subprocess
from zoneinfo import ZoneInfo
ON = True
HERE = os.path.dirname(os.path.abspath(__file__)); CT = ZoneInfo('America/Chicago')
LIST = os.path.join(HERE, 'data', 'star_demon_list.json'); LOG = os.path.join(HERE, 'data', 'star_demon_log.jsonl')
NBA = os.path.expanduser('~/ApexSports/basketball'); TRACKER = os.path.join(NBA, 'data', 'processed', 'bball_tracker.sqlite')
MAX_ABOVE = 2.0
STAT_NAME = {'points': 'Points', 'rebounds': 'Rebounds', 'assists': 'Assists', 'threePointersMade': '3-PT Made', 'points+rebounds+assists': 'Pts+Rebs+Asts',
             'points+rebounds': 'Pts+Rebs', 'points+assists': 'Pts+Asts', 'rebounds+assists': 'Rebs+Asts'}

_SNIP = r"""
import sys, os, json
sys.path.insert(0, os.path.join(sys.argv[1], 'code')); sys.path.insert(0, os.path.join(sys.argv[1], 'discord')); os.chdir(os.path.join(sys.argv[1], 'code'))
import score_board as SB, bball_tracker as T, bball_pipeline as P, cards as K
sl = json.load(open(sys.argv[2])); MAX = float(sys.argv[3]); cur = P.current(); out = dict(saved=None, legs=[], far=[], hurt=[])
if cur:
    out['saved'] = K.saved_line(cur); rows = cur['rows']; games = {}
    for i, st in enumerate(sl['stars']):
        mine = [r for r in rows if SB.norm(r.get('player')) == st['norm'] and r.get('win_prob') is not None and not r.get('promo')]
        if not mine: continue
        if any(r.get('own_inj') or r.get('inj_hold') for r in mine): out['hurt'].append(st['name']); continue
        order = sorted(st['stats'], key=lambda s: -(st['stats'][s]['won'] / float(st['stats'][s]['lines']) - st['stats'][s]['normal'] / 100.0))
        order += [s for s in ('points+rebounds+assists', 'points+rebounds', 'points+assists', 'points', 'rebounds', 'assists', 'rebounds+assists', 'threePointersMade') if s not in order]
        pick = None; too_far = False
        for stat in order:
            reg = [r for r in mine if r['stat'] == stat and r['kind'] == 'standard' and 'gless' not in str(T.pick_key(r))]
            if not reg: continue
            reg = reg[0]; dem = sorted((r for r in mine if r['stat'] == stat and r['kind'] == 'demon' and r.get('side') == 'MORE' and float(r['line']) > float(reg['line'])), key=lambda r: float(r['line']))
            if not dem: continue
            if float(dem[0]['line']) - float(reg['line']) > MAX + 1e-9: too_far = True; continue
            pick = (stat, dem[0], reg); break
        if not pick:
            if too_far: out['far'].append(st['name'])
            continue
        stat, r, reg = pick
        leg = dict(i=i, name=st['name'], key=T.pick_key(r), player=r['player'], stat=stat, line=float(r['line']), regular=float(reg['line']), apex=r['win_prob'],
                   opp=SB.TEAMS.get(r.get('opponent'), (r.get('opponent') or '?',))[0], start=r['start'], game=r['game_key'], game_date=r['game_date'],
                   ladder=K.ladder_line(r, rows, K._bb_over, K._bb_honest), book=(r.get('market') or {}).get('line'), nbooks=(r.get('market') or {}).get('n'))
        games.setdefault(r['game_key'], []).append(leg)
    for g, legs in games.items():
        legs.sort(key=lambda x: x['i']); legs[0]['also'] = legs[1:]; out['legs'].append(legs[0])
    out['legs'].sort(key=lambda x: x['i']); out['ladder_key'] = K.LADDER_KEY
print(json.dumps(out))
"""


def _list():
    try: return json.load(open(LIST))
    except Exception: return None


def _read_log():
    try: return [json.loads(l) for l in open(LOG) if l.strip()]
    except Exception: return []


def record():
    w = l = 0
    for x in _read_log():
        if x.get('result') == 'win': w += 1
        elif x.get('result') == 'loss': l += 1
    return w, l


def _log(legs):
    """One row per leg, once (the same line shown again is not counted twice)."""
    have = {x.get('key') for x in _read_log()}; new = [x for x in legs if x['key'] not in have]
    if not new: return
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a') as f:
        for x in new: f.write(json.dumps(dict(key=x['key'], player=x['player'], stat=x['stat'], line=x['line'], regular=x['regular'], apex=x['apex'],
                                              game_date=x['game_date'], start=x['start'], shown=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), result=None)) + '\n')


def render(log=True):
    if not ON: return '🔴 STAR DEMON board is turned off.'
    sl = _list(); day = dt.datetime.now(CT).strftime('%a %-m/%-d')
    L = ['🔴 **STAR DEMON** · NBA · %s' % day,
         'Only the most consistent stars: they cleared their first Demon more often than normal Demons that height in BOTH past seasons. On the real lines 11/4 to 11/12 this was not confirmed yet. Expect about 4 to 5 of every 10 legs to win.']
    if not sl: return '\n'.join(L + ['⚠️ The star list file is missing, so there is nothing to show.'])
    try:
        p = subprocess.run(['/usr/bin/python3', '-c', _SNIP, NBA, LIST, str(MAX_ABOVE)], capture_output=True, text=True, timeout=120)
        d = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:
        return '\n'.join(L + ['⚠️ NBA lines could not be read right now (%s). Try again in a minute.' % type(e).__name__])
    if d.get('saved'): L.append(d['saved'])
    w, l = record(); L.append('📒 Star Demon board record: won %d, lost %d legs' % (w, l))
    L.append('⭐ Stars on the list: ' + ', '.join(s['name'] for s in sl['stars']))
    by = {s['name']: s for s in sl['stars']}
    if not d['legs']: L.append('🚫 None of the listed stars has a Demon line close to his regular line in the saved NBA lines right now (not playing, on the injury report, or lines not posted yet).')
    for n, x in enumerate(d['legs'], 1):
        st = by.get(x['name']) or {}; o = st.get('older') or {}; nw = st.get('newer') or {}; sr = (st.get('stats') or {}).get(x['stat'])
        try: when = dt.datetime.fromisoformat(x['start'].replace('Z', '+00:00')).astimezone(CT).strftime('%a %-I:%M %p CT')
        except Exception: when = '?'
        L += ['━━━━━━━━━━━━', '🔴 **%d. %s** · %s · Demon **%g** MORE (regular line %g) · 🆚 %s · %s' % (n, x['player'], STAT_NAME.get(x['stat'], x['stat']), x['line'], x['regular'], x['opp'], when),
              '📊 His first-Demon record: won %d, lost %d (2024-25) · won %d, lost %d (2025-26). Normal for Demons that height: about %d and %d of 100.'
              % (o.get('won', 0), o.get('lines', 0) - o.get('won', 0), nw.get('won', 0), nw.get('lines', 0) - nw.get('won', 0), o.get('normal', 0), nw.get('normal', 0))]
        if sr: L.append('📈 On %s: won %d, lost %d (normal about %d of 100)' % (STAT_NAME.get(x['stat'], x['stat']), sr['won'], sr['lines'] - sr['won'], sr['normal']))
        if x.get('book') is not None and x['line'] <= float(x['book']) + 1e-9: L.append('📗 This Demon is at or below the sportsbooks\' line (%g at %d books).' % (x['book'], x.get('nbooks') or 0))
        L.append('🎯 Apex: %.0f' % x['apex'])
        if x.get('ladder'): L.append(x['ladder'])
        for a in x.get('also') or []: L.append('   ↳ also in this game: %s %s Demon %g (regular %g)' % (a['player'], STAT_NAME.get(a['stat'], a['stat']), a['line'], a['regular']))
    if d['legs']: L.append('━━━━━━━━━━━━')
    if d.get('far'): L.append('Left off tonight (lowest Demon too far above his line): ' + ', '.join(d['far']))
    if d.get('hurt'): L.append('Left off (on the injury report): ' + ', '.join(d['hurt']))
    if any('🪜' in s for s in L) and d.get('ladder_key'): L.append(d['ladder_key'])
    L.append('Check every line in the app before you play. Display only: no other board changes.')
    if log: _log(d['legs'])
    return '\n'.join(L)


def grade():
    """Fills result from the NBA tracker (read only). Returns (newly graded, won, lost)."""
    rows = _read_log()
    open_ = [x for x in rows if x.get('result') is None]
    if not open_: return 0, 0, 0
    db = sqlite3.connect('file:%s?mode=ro' % TRACKER, uri=True)
    try:
        st = {k: (s, r) for k, s, r in db.execute('select pick_key, status, result from picks where pick_key in (%s)' % ','.join('?' * len(open_)), [x['key'] for x in open_])}
    finally: db.close()
    n = w = l = 0
    for x in open_:
        s = (st.get(x['key']) or (None, None))
        if s[0] in ('win', 'loss', 'void', 'push'):
            x['result'] = s[0]; x['actual'] = s[1]; n += 1; w += s[0] == 'win'; l += s[0] == 'loss'
    if n:
        with open(LOG + '.tmp', 'w') as f:
            for x in rows: f.write(json.dumps(x) + '\n')
        os.replace(LOG + '.tmp', LOG)
    return n, w, l


def run(post=True):
    """Called from apex_alerts.check (every 30 min): after 4:30 AM CT, grade any open legs once and post one #system line."""
    if not ON: return []
    now = dt.datetime.now(CT)
    if now.hour < 4 or (now.hour == 4 and now.minute < 30): return []
    n, w, l = grade()
    if not n: return []
    tw, tl = record()
    txt = '🔴 Star Demon board graded: %d leg%s (won %d, lost %d). Running record: won %d, lost %d.' % (n, '' if n == 1 else 's', w, l, tw, tl)
    if post:
        import apex_ops as ops
        ops.send_discord('system', txt)
    else: print('---- would post to #system ----\n' + txt)
    return [txt]


if __name__ == '__main__':
    print(render(log=False))
