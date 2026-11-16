#!/usr/bin/env python3
"""APEX-EARLYDEMON-2026-11-13 (owner, "Go early demon board"): /earlydemon in the tennis server. NBA first Demons that look
likely to become the regular line, taken early. Built on the 11/13 study (~/openclaw/_backups/demon_moves_1113/predict/):
  - when the sportsbooks are already AT or ABOVE a Demon number, PrizePicks later made that number the regular line 35 to 52 of 100
    times (10 to 18 for same-height Demons) and those Demons won 52 and 48 of 100 (same height 42 and 41; newer half only 27 lines)
  - a teammate who plays 28+ minutes out or questionable: Demons won 36 and 42 of 100 (same height 32 and 33), both halves
  - 2TB archive, 88k NBA lines Feb 2026+: when the books' line went up 1.5+ before tip, the player went over the OLD number 59-63 of 100
Card: each NBA game's best first Demon (at most 1 above the regular line, MORE), ranked 📗 books at/above the Demon first, then 🏥
teammate out, then Apex; top 6. Injury-listed players skipped (own report AND the injury feed). Read only on NBA (same subprocess pattern
as /stardemon); nothing pulled from PrizePicks. Every showing logged (data/early_demon_log.jsonl), graded after the 4 AM NBA grading from the
NBA tracker, one #system line, running record on the card. Display only. Off: ON = False, restart. Undo: git checkout before-early-demon-board."""
import datetime as dt, json, os, sqlite3, subprocess
from zoneinfo import ZoneInfo
ON = True
HERE = os.path.dirname(os.path.abspath(__file__)); CT = ZoneInfo('America/Chicago')
LOG = os.path.join(HERE, 'data', 'early_demon_log.jsonl')
NBA = os.path.expanduser('~/ApexSports/basketball'); TRACKER = os.path.join(NBA, 'data', 'processed', 'bball_tracker.sqlite')
SIZE = 6; MAX_ABOVE = 1.0

_SNIP = r"""
import sys, os, json, glob, unicodedata, re
sys.path.insert(0, os.path.join(sys.argv[1], 'code')); sys.path.insert(0, os.path.join(sys.argv[1], 'discord')); os.chdir(os.path.join(sys.argv[1], 'code'))
import score_board as SB, bball_tracker as T, bball_pipeline as P, cards as K
MAX = float(sys.argv[2]); cur = P.current(); out = dict(saved=None, legs=[], hurt=[])
def nm(s): return ' '.join(re.sub(r'[^a-z ]', '', unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode().lower()).split())
import sqlite3, statistics as _st
_bx = {}
for _p, _d, _pl, _m in sqlite3.connect('file:%s?mode=ro' % os.path.join(sys.argv[1], 'data', 'processed', 'nba_box_full.sqlite'), uri=True).execute('select player, game_date, played, minutes from player_box'):
    if _pl and _m: _bx.setdefault(nm(_p), []).append((_d, _m))
def mins(p, day):
    v = sorted(x for x in _bx.get(nm(p), []) if x[0] < day)[-10:]
    return _st.mean(m for _, m in v) if len(v) >= 3 else 0
listed = {}
for fn in sorted(glob.glob(os.path.join(sys.argv[1], 'data', 'collected', 'injuries', '*.jsonl')))[-3:]:
    for l in open(fn):
        try: x = json.loads(l)
        except Exception: continue
        listed[nm(x.get('player'))] = x.get('status')
if cur:
    out['saved'] = K.saved_line(cur); rows = cur['rows']; std = {}
    for r in rows:
        if r['kind'] == 'standard' and r.get('win_prob') is not None and 'gless' not in str(T.pick_key(r)): std.setdefault((r['player'], r['stat'], r['game_key']), r)
    best = {}
    for r in rows:
        if r['kind'] != 'demon' or r.get('side') != 'MORE' or r.get('win_prob') is None or r.get('promo'): continue
        s = std.get((r['player'], r['stat'], r['game_key']))
        if not s: continue
        h = float(r['line']) - float(s['line'])
        if h <= 0 or h > MAX + 1e-9: continue
        st = listed.get(nm(r['player']))
        if r.get('own_inj') or r.get('inj_hold') or st in ('Out', 'Doubtful', 'Questionable', 'Day-To-Day'):
            if r['player'] not in out['hurt']: out['hurt'].append(r['player'])
            continue
        m = r.get('market') or {}; book = m.get('line'); nb = m.get('n') or 0
        at_book = book is not None and nb >= 2 and float(book) >= float(r['line']) - 1e-9
        f_ = r.get('facts') or {}
        outs = [o for o in list(f_.get('outs') or []) + list(f_.get('quest') or []) if mins(o, r['game_date']) >= 28]
        leg = dict(key=T.pick_key(r), player=r['player'], stat=r.get('stat_raw') or r['stat'], line=float(r['line']), regular=float(s['line']), apex=r['win_prob'],
                   opp=SB.TEAMS.get(r.get('opponent'), (r.get('opponent') or '?',))[0], start=r['start'], game=r['game_key'], game_date=r['game_date'],
                   book=book, nbooks=nb, at_book=at_book, outs=outs[:3], ladder=K.ladder_line(r, rows, K._bb_over, K._bb_honest))
        if not at_book and not outs: continue
        leg['signal'] = 'both' if at_book and outs else 'books' if at_book else 'teammate'
        leg['rank'] = (2 if (at_book and outs) else 1 if at_book else 0, r['win_prob'])
        g = r['game_key']
        if g not in best or leg['rank'] > best[g]['rank']: best[g] = leg
    out['legs'] = sorted(best.values(), key=lambda x: x['rank'], reverse=True)
    # already moved: this morning's first Demon (1 step or less, last pull at or before 10 AM CT) is now the regular line or below it
    import datetime as _dt
    from zoneinfo import ZoneInfo as _Z
    _ct = _Z('America/Chicago'); today = _dt.datetime.now(_ct).date().isoformat()
    ten = _dt.datetime.combine(_dt.date.fromisoformat(today), _dt.time(10, 0), _ct).astimezone(_dt.timezone.utc).isoformat()
    morning = {}
    fn = os.path.join(sys.argv[1], 'data', 'collected', today + '.jsonl')
    if os.path.exists(fn):
        snap = {}   # the collector saves only new or changed lines per pull: rebuild the 10 AM board line by line
        for l in open(fn):
            try: x = json.loads(l)
            except Exception: continue
            if x.get('league') != 'NBA' or x['seen'] > ten or x['start'][:10] != today: continue
            snap[x['projection_id']] = x
        if snap:
            last = list(snap.values()); std0 = {}; dem0 = {}
            for x in last:
                k = (x['player'], x['stat'])
                if x.get('odds_type') == 'standard': std0[k] = min(std0.get(k, 99999), float(x['line']))
            for x in last:
                k = (x['player'], x['stat'])
                if x.get('odds_type') == 'demon' and k in std0 and 0 < float(x['line']) - std0[k] <= MAX: dem0[k] = min(dem0.get(k, 99999), float(x['line']))
            nowstd = {}
            for r in rows:
                if r['kind'] == 'standard': nowstd[(r['player'], r.get('stat_raw'))] = float(r['line'])
            out['moved'] = ['%s %s %g' % (k[0], k[1], v) for k, v in dem0.items() if nowstd.get(k) is not None and nowstd[k] >= v - 1e-9]
    bf = os.path.join(sys.argv[1], 'data', 'collected', 'books', today + '.jsonl'); bt = None
    if os.path.exists(bf):
        for l in open(bf):
            try: s = json.loads(l).get('seen')
            except Exception: continue
            if s and (bt is None or s > bt): bt = s
    out['books_at'] = bt
print(json.dumps(out))
"""


def _read_log():
    try: return [json.loads(l) for l in open(LOG) if l.strip()]
    except Exception: return []


def record(signal=None):
    xs = [x for x in _read_log() if signal is None or x.get('signal') == signal]
    return sum(1 for x in xs if x.get('result') == 'win'), sum(1 for x in xs if x.get('result') == 'loss')


def _log(legs):
    """Once per line per day (the same line shown again is not counted twice)."""
    have = {x.get('key') for x in _read_log()}; new = [x for x in legs if x['key'] not in have]
    if not new: return
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a') as f:
        for x in new: f.write(json.dumps(dict(key=x['key'], player=x['player'], stat=x['stat'], line=x['line'], regular=x['regular'], apex=x['apex'], signal=x['signal'], book=x.get('book'),
                                              teammates_out=x['outs'], game_date=x['game_date'], start=x['start'], shown=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), result=None)) + '\n')


SIG = {'both': 'sportsbooks already at %s AND teammate out: %s', 'books': 'sportsbooks already at %s', 'teammate': 'teammate out: %s'}


def _read_board():
    p = subprocess.run(['/usr/bin/python3', '-c', _SNIP, NBA, str(MAX_ABOVE)], capture_output=True, text=True, timeout=120)
    return json.loads(p.stdout.strip().splitlines()[-1])


def render(log=True):
    if not ON: return '🔴 EARLY DEMON board is turned off.'
    day = dt.datetime.now(CT).strftime('%a %-m/%-d')
    L = ['🔴 **EARLY DEMON** · NBA · %s' % day,
         'Demons 1 step or less above the regular line where the sportsbooks are already at that number, or a teammate who plays 28+ minutes is out or questionable. Take them early: once PrizePicks moves its line up, the edge is gone.',
         'Test record so far (promising, not proven): sportsbooks signal won 39 lost 36, then won 13 lost 14. Teammate out won 27 lost 49, then won 144 lost 195. Demons the same height won about 41 and 32 of 100.']
    try: d = _read_board()
    except Exception as e:
        return '\n'.join(L + ['⚠️ NBA lines could not be read right now (%s). Try again in a minute.' % type(e).__name__])
    if d.get('saved'): L.append(d['saved'])
    try: L.append('📚 Sportsbook lines last pulled %s.' % dt.datetime.fromisoformat(d['books_at']).astimezone(CT).strftime('%-I:%M %p CT') if d.get('books_at') else '📚 Sportsbook lines not pulled yet today (first pull about 9:15 AM CT), so only the teammate signal can fire.')
    except Exception: pass
    parts = []
    for s, lab in (('both', 'both signals'), ('books', 'sportsbooks'), ('teammate', 'teammate out')):
        w, l = record(s)
        if w or l: parts.append('%s won %d lost %d' % (lab, w, l))
    w, l = record(); L.append('📒 Early Demon board record: won %d, lost %d legs' % (w, l) + (' (%s)' % '; '.join(parts) if parts else ''))
    legs = d['legs'][:SIZE]
    if not legs: L.append('🚫 No NBA Demon 1 step or less above the regular line has either signal right now' + ('' if d.get('books_at') else ' (sportsbook lines not pulled yet today)') + '.')
    for n, x in enumerate(legs, 1):
        try: when = dt.datetime.fromisoformat(x['start'].replace('Z', '+00:00')).astimezone(CT).strftime('%a %-I:%M %p CT')
        except Exception: when = '?'
        sig = SIG[x['signal']] % ((('%g' % x['book']), ', '.join(x['outs'])) if x['signal'] == 'both' else ('%g' % x['book'],) if x['signal'] == 'books' else (', '.join(x['outs']),))
        L += ['━━━━━━━━━━━━', '🔴 **%d. %s** · %s · Demon **%g** MORE (regular line %g) · 🆚 %s · %s' % (n, x['player'], x['stat'], x['line'], x['regular'], x['opp'], when),
              '%s %s · 🎯 Apex %.0f' % ('📗' if x['signal'] != 'teammate' else '🏥', sig, x['apex'])]
        if x.get('ladder'): L.append(x['ladder'])
    if legs:
        L.append('━━━━━━━━━━━━')
        L.append('Honest chance: each leg is about 4 to 5 of 10, so all 6 hitting is about 1 slip in 100 or more. A 6-pick Flex still pays on 5 of 6.')
    if d.get('moved'): L.append('⏰ Already moved, too late (this morning\'s Demon is now the regular line): ' + ', '.join(d['moved'][:12]) + (' and %d more' % (len(d['moved']) - 12) if len(d['moved']) > 12 else ''))
    if d.get('hurt'): L.append('Left off (on the injury report): ' + ', '.join(d['hurt'][:12]) + (' and %d more' % (len(d['hurt']) - 12) if len(d['hurt']) > 12 else ''))
    L.append('Check every line in the app before you play. Display only: no other board changes.')
    if log: _log(legs)
    return '\n'.join(L)


def grade():
    rows = _read_log(); open_ = [x for x in rows if x.get('result') is None]
    if not open_: return []
    db = sqlite3.connect('file:%s?mode=ro' % TRACKER, uri=True)
    try: st = {k: (s, r) for k, s, r in db.execute('select pick_key, status, result from picks where pick_key in (%s)' % ','.join('?' * len(open_)), [x['key'] for x in open_])}
    finally: db.close()
    done = []
    for x in open_:
        s = st.get(x['key']) or (None, None)
        if s[0] in ('win', 'loss', 'void', 'push'): x['result'] = s[0]; x['actual'] = s[1]; done.append(x)
    if done:
        with open(LOG + '.tmp', 'w') as f:
            for x in rows: f.write(json.dumps(x) + '\n')
        os.replace(LOG + '.tmp', LOG)
    return done


STATE = os.path.join(HERE, 'data', 'early_demon_state.json')


def run(post=True):
    """From apex_alerts.check (every 30 min): 9:35-11 AM CT one 'board ready' line in #system; after 4:30 AM CT grade open legs, one #system line."""
    if not ON: return []
    import apex_ops as ops
    say = (lambda t: ops.send_discord('system', t)) if post else (lambda t: print('---- would post to #system ----\n' + t))
    now = dt.datetime.now(CT); today = now.date().isoformat(); sent = []
    try: st = json.load(open(STATE))
    except Exception: st = {}
    if (now.hour, now.minute) >= (9, 35) and now.hour < 11 and st.get('ready') != today:
        try:
            d = _read_board(); n = min(SIZE, len(d['legs']))
            t = ('🔴 Early Demon board ready: %d pick%s. Type /earlydemon' % (n, '' if n == 1 else 's')) if n else ('🔴 Early Demon board: no picks this morning (%s).' % ('sportsbook lines not pulled yet' if not d.get('books_at') else 'no Demon 1 step above the regular line has either signal'))
        except Exception as e: t = '🔴 Early Demon board could not read NBA lines this morning (%s).' % type(e).__name__
        say(t); sent.append(t); st['ready'] = today
        with open(STATE, 'w') as f: json.dump(st, f)
    if now.hour > 4 or (now.hour == 4 and now.minute >= 30):
        done = grade()
        if done:
            by = {}
            for x in done:
                if x['result'] in ('win', 'loss'): b = by.setdefault(x.get('signal'), [0, 0]); b[x['result'] == 'loss'] += 1
            w = sum(v[0] for v in by.values()); l = sum(v[1] for v in by.values()); tw, tl = record()
            t = '🔴 Early Demon board graded: %d leg%s (won %d, lost %d; %s). Running record: won %d, lost %d.' % (len(done), '' if len(done) == 1 else 's', w, l,
                 ', '.join('%s %d-%d' % ({'both': 'both signals', 'books': 'sportsbooks', 'teammate': 'teammate out'}.get(k, k), v[0], v[1]) for k, v in by.items()) or 'all void', tw, tl)
            say(t); sent.append(t)
    return sent


if __name__ == '__main__':
    print(render(log=False))
