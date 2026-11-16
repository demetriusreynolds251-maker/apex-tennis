"""APEX-FRESH-LADDER-2026-11-09 (owner: "On all props I need to know how fresh the props is and on all props need
show the ladder system, not seen that on tennis"): two plain lines for every tennis card, read from data the bot already
keeps -- when PrizePicks was last seen offering this line, when PrizePicks last changed it, and the ladder of every line
PrizePicks offers on this player and stat with Apex's number for each. Never raises; a problem returns no lines."""
import os, sqlite3, datetime as dt
try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo('America/Chicago')
except Exception:
    CT = dt.timezone(dt.timedelta(hours=-6))
DB = os.path.expanduser('~/openclaw/data/props_db.sqlite')
BH = os.path.expanduser('~/openclaw/data/board_history.sqlite')
EMOJI = {'goblin': '\U0001F7E2', 'demon': '\U0001F534', 'red_demon': '\U0001F534', 'standard': '\u26aa'}

def _ct(s):
    if not s: return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if t.tzinfo is None: t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(CT).strftime('%-m/%-d %-I:%M %p CT')
    except Exception: return None

def _last(name):
    name = str(name or '').strip()
    return name.split()[-1].lower() if name else ''

def _rows(pname, opponent, cat, mdate):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    try:
        q = con.execute("select player, opponent, category, odds_type, line_last, line_changes, first_seen, last_seen, pp_updated_at from projections where category=? and match_date=?", (cat, mdate)).fetchall()
    finally: con.close()
    ln, lo = _last(pname), _last(str(opponent or '').split(' v ')[0])
    out = []
    for r in q:
        who = (r['player'] or '').lower(); opp = (r['opponent'] or '').lower()
        if who == 'match':
            if ln and ln in opp: out.append(r)
        elif ln and ln == _last(who): out.append(r)
        elif not ln and lo and lo in opp: out.append(r)
    return out

def _probs(pname, cat, mdate):
    """Apex's number per line from today's published all-props board (last publish)."""
    try:
        con = sqlite3.connect(BH)
        pid = con.execute("select max(publish_id) from history where board='allprops' and board_day=?", (mdate,)).fetchone()[0]
        if pid is None: return {}
        q = con.execute("select player, opponent, prop_type, line, win_prob, honest from history where board='allprops' and publish_id=? and category=?", (pid,)).fetchall() if False else \
            con.execute("select player, opponent, prop_type, line, win_prob, honest from history where board='allprops' and publish_id=? and category=?", (pid, cat)).fetchall()
        con.close()
    except Exception: return {}
    ln = _last(pname); d = {}
    for player, opp, ptype, line, wp, hon in q:
        who = (player or '').lower(); o = (opp or '').lower()
        if (who == 'match' and ln in o) or (who != 'match' and _last(who) == ln):
            d[(str(ptype or 'standard').lower().replace('red_demon', 'demon'), float(line))] = (wp, hon)
    return d

def lines(prop, t, pname=None):
    try:
        pname = pname or (t.get('player') if t.get('player') != 'match' else prop.get('player')) or prop.get('player') or ''
        cat = t.get('category'); line = float(t.get('line')); opponent = prop.get('opponent', '')
        mdate = str(prop.get('match_date') or prop.get('date') or dt.datetime.now(CT).date())[:10]
        ptype = str(prop.get('prop_type_classified') or 'standard').lower().replace('red_demon', 'demon')
        rows = _rows(pname, opponent, cat, mdate)
        if not rows: return []
        out = []
        me = [r for r in rows if abs(float(r['line_last'] or -99) - line) < 1e-6 and str(r['odds_type'] or 'standard').lower() == ptype] or \
             [r for r in rows if abs(float(r['line_last'] or -99) - line) < 1e-6] or rows
        r = me[0]
        seen = _ct(r['last_seen']); upd = _ct(r['pp_updated_at']); moves = int(r['line_changes'] or 0)
        bits = []
        if seen: bits.append('Seen on PrizePicks %s' % seen)
        if upd: bits.append('line set by PrizePicks %s' % upd)
        bits.append('moved %d time%s since first seen' % (moves, '' if moves == 1 else 's') if moves else 'never moved since first seen')
        out.append('\U0001F440 ' + ' \u00b7 '.join(bits))
        lad = ladder({'category': cat, 'line': line, 'player': pname, 'opponent': opponent, 'prop_type': ptype, 'match_date': mdate,
                      'direction': t.get('direction') or t.get('side'), '_key': _last(pname)})   # APEX-TENNISLADDER-2026-11-12: one ladder look everywhere
        if lad: out.append(lad)
        return out
    except Exception:
        return []


# APEX-LADDERALL-2026-11-12 (owner, "All props need a ladder ... I wish I would've caught the demon here" / "Go ladder all"):
# the 11/9 ladder only went on the engine card. Every tennis BOARD card (Top 3, Elite 5/6, Top 10, Best Goblin/Standard/Demon, Soon,
# the published board) is drawn by apex_edge_tracker._render_pick_card, which now calls ladder(pick). Display only; never raises.
# APEX-TENNISLADDER-2026-11-12 (owner, "Tennis ladder needs to show like all other ladders"): same look as the sport
# cards -- "🪜 🟢8.5 P81/HP75 · 🔴10.5 P40/HP–", every rung with Apex's number (P) and how often that really hits (HP),
# lowest line up to the first Demon, no key on the card. Numbers: the published boards first, then the engine's scoring log,
# then the first-set model. Display only; never raises.
LOGCSV = os.path.expanduser('~/openclaw/apex_engine_picks_log_v1.csv')
_LG = {}
def _englog():
    try:
        mt = os.path.getmtime(LOGCSV)
        if _LG.get('mt') == mt: return _LG['rows']
        import csv
        cut = str(dt.datetime.now(CT).date() - dt.timedelta(days=2)); rows = []
        with open(LOGCSV, newline='') as fh:
            for r in csv.DictReader(fh):
                if (r.get('match_date') or '') >= cut: rows.append(r)
        _LG.update(mt=mt, rows=rows); return rows
    except Exception: return []

def _hp(cat, kind, side, pct):
    try:
        import apex_tennis_honest as H
        x = H._table().get('%s|%s|%s|%s' % (cat, kind, side, H.band(pct)))
        if not x or x[1] < H.MIN_N: return None
        return 100.0 * x[0] / x[1]
    except Exception: return None

def ladder(p):
    try:
        import re
        cat = p.get('category'); line = float(p.get('line')); is_match = str(p.get('player') or '').lower() == 'match'
        ptype = str(p.get('prop_type') or p.get('prop_type_classified') or p.get('odds_type') or 'standard').lower().replace('red_demon', 'demon')
        opp = str(p.get('opponent') or '')
        key = p.get('_key') or (_last(re.split(r'\s+(?:v|vs|vs\.)\s+', opp)[0]) if is_match else _last(p.get('player')))
        if p.get('_key'): is_match = True
        if not key or not cat: return None
        less = str(p.get('direction') or p.get('side') or 'MORE').upper() == 'LESS'
        today = dt.datetime.now(CT).date(); days = []
        for d in (str(p.get('match_date') or p.get('date') or '')[:10], str(today), str(today + dt.timedelta(days=1)), str(today - dt.timedelta(days=1))):
            if d and d not in days: days.append(d)
        def mine(player, opponent):
            who = (player or '').lower()
            if who != 'match' and _last(who) == key: return True
            return is_match and key in (who + ' ' + (opponent or '').lower())
        norm = lambda k: str(k or 'standard').lower().replace('red_demon', 'demon')
        rows, mdate = [], None
        con = sqlite3.connect(DB)
        try:
            for d in days:
                q = con.execute("select player, opponent, odds_type, line_last from projections where category=? and match_date=?", (cat, d)).fetchall()
                rows = [r for r in q if mine(r[0], r[1]) and r[3] is not None]
                if rows and any(abs(float(r[3]) - line) < 1e-6 for r in rows): mdate = d; break
                rows = []
        finally: con.close()
        if not rows: return None
        more, hon_own = {}, {}                      # (kind, line) -> Apex MORE number 0-100
        for r in _englog():                         # engine scoring log, oldest first so the newest score wins
            try:
                if r.get('match_date') != mdate or r.get('category') != cat or not mine(r.get('player'), r.get('opponent')): continue
                pr = float(r.get('prob_cal') or r.get('prob')); pr = pr * 100 if pr <= 1 else pr
                more[(norm(r.get('prop_type')), float(r['line']))] = pr if str(r.get('direction')).upper() == 'MORE' else 100 - pr
            except Exception: continue
        try:                                        # what the boards really published wins over the log
            bh = sqlite3.connect(BH)
            for player, o, pt, ln, dr, wp, hon in bh.execute("select player, opponent, prop_type, line, direction, win_prob, honest from history where board_day=? and category=? order by publish_id", (mdate, cat)):
                if not mine(player, o) or ln is None or wp is None: continue
                k = (norm(pt), float(ln)); isl = str(dr or 'MORE').upper() == 'LESS'
                more[k] = 100 - float(wp) if isl else float(wp)
                if hon is not None and isl == less: hon_own[k] = float(hon)
            bh.close()
        except Exception: pass
        if cat == '1st_set_total_games':            # first-set model fills the rungs nothing else scored
            try:
                import apex_set_games as SG
                a, b = SG._clean(rows[0][0]), SG._clean(rows[0][1]); fav, tot = SG._book(a, b)
                for player, o, ot, L in rows:
                    k = (norm(ot), float(L))
                    if k in more or not fav: continue
                    n = SG.engine_number(dict(a=a, b=b, line=float(L), fav=fav, total=tot, start=mdate))
                    if n is not None: more[k] = 100.0 * n
            except Exception: pass
        same = {}
        for player, o, ot, L in rows:
            ot = norm(ot); L = float(L)
            if L not in same or ot == ptype or (same[L] != ptype and ot == 'standard'): same[L] = ot
        if abs(line - round(line * 2) / 2.0) < 1e-6 and line in same and ptype in [norm(r[2]) for r in rows if abs(float(r[3]) - line) < 1e-6]: same[line] = ptype
        demons = [L for L, k in same.items() if k == 'demon']
        top = max(min(demons), line) if demons else max(same)
        steps = [L for L in sorted(same) if L <= top]
        steps = steps[-7:] if (less or line not in steps[:7]) else steps[:7]
        out = []
        for L in steps:
            kind = same[L]; m = more.get((kind, L)); icon = EMOJI.get(kind, '⚪')
            if m is None: txt = '%s%g P–/HP–' % (icon, L)
            else:
                pv = 100 - m if less else m
                hp = hon_own.get((kind, L))          # the honest number the boards published for this line, so every card agrees
                if hp is None: hp = _hp(cat, kind, 'LESS' if less else 'MORE', pv)
                txt = '%s%g P%.0f/HP%s' % (icon, L, pv, ('%.0f' % hp) if hp is not None else '–')
            out.append('**%s**' % txt if abs(L - line) < 1e-6 else txt)
        if len(out) == 1: return '\U0001FA9C %s · the only line PrizePicks offers on this stat' % out[0]
        return ('\U0001FA9C LESS side: ' if less else '\U0001FA9C ') + ' · '.join(out)
    except Exception:
        return None
