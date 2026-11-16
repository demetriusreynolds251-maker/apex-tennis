#!/usr/bin/env python3
"""APEX-BEST6-2026-11-11 (owner, "go best 6 ... call with tennis general but the name best6of6"): /best6of6.
One card across NFL, college football, NHL and NBA: the top 2 best-group Goblins of every sport (each sport's own current Top 10
numbered picks, which since before-room-plus-apex hold only best-group Goblins: proven room under the sportsbooks + a high Apex rating),
then the 6 with the highest numbers. If a sport has fewer than 2 the open spots go to the next best picks of the other sports; if fewer
than 6 qualify in all, the card is short. Since APEX-BEST6TENNIS (below): tennis top 2 are in and only games today or tomorrow. Read only: each sport's picks and cards come from that sport's OWN code, run
in its own process inside its own folder (same way as /lottery); nothing is pulled from PrizePicks and no pick or rating changes.
Turn off: ON = False, restart the tennis bot."""
import json, os, subprocess
ON = True
SIZE = 6; PER_SPORT = 2
# APEX-BEST6NUMBERED-2026-11-14 (owner, "go best 6 numbered"): Best 6 takes only picks that sit in a numbered spot of their sport's OWN
# posted Top 10 (the last scoring), not a board rebuilt from today's games alone (that let Micahi Danzy, Trenton Chaney on without ever being on
# the college board). Best Goblin LESS is unchanged. Undo: NUMBERED_ONLY = False, restart the tennis bot.
NUMBERED_ONLY = True
SPORTS = os.path.expanduser("~/ApexSports")
ROOTS = [("football/nfl", "🏈 NFL"), ("football/ncaaf", "🏈 College football"), ("hockey/nhl", "🏒 NHL"), ("basketball", "🏀 NBA")]
# APEX-COLLEGEOFFCARDS-2026-11-13 (owner, "Only off the all-sports cards"): college football picks leave /best6of6 (and both Best 6
# trackers from now on) and /bestless + /goblinless; each card says so in one plain line. The college server's own boards are untouched.
# Freshness checks / refreshes still cover every sport. Undo: CFB_OFF_CARDS = False, restart the tennis bot (or git checkout before-college-off-cards).
CFB_OFF_CARDS = False   # APEX-COLLEGETOP3-2026-11-14 (owner, "Alright ..."): college back on the cards, with the spot 1-4 rule below
CFB_OFF_LINE = "🏈 College football is off this card by your choice (its own server's boards still post)."


def _roots():
    return [(r, n) for r, n in ROOTS if not (CFB_OFF_CARDS and r == "football/ncaaf")]
_SNIP = r"""
import sys, os, json, datetime as dt
root = sys.argv[1]; os.chdir(root); out = []
BOARD = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else 'top10'
NUMBERED = len(sys.argv) > 4 and sys.argv[4] == '1'
now = dt.datetime.now(dt.timezone.utc); end = dt.datetime.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else None
def inwin(r):
    t = ts(r.get('start')); return bool(t and t > now and (end is None or t <= end))
def ts(s):
    try:
        t = dt.datetime.fromisoformat(str(s).replace('Z', '+00:00')); return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except Exception: return None
def gapd(c, line):
    try: return dict(line=float(line), book=float(c['book']), room=float(c['room']), steps=float(c['steps']), was=float(c['was']), books=int(c.get('n_books') or 0)) if isinstance(c, dict) and c.get('book') is not None else None
    except Exception: return None
if os.path.basename(root) == 'basketball':
    sys.path.insert(0, os.path.join(root, 'code')); sys.path.insert(0, os.path.join(root, 'discord'))
    import bball_pipeline as P, cards as K
    cur = P.current(); rows = [r for r in (cur['rows'] if cur else []) if inwin(r)]
    if NUMBERED and cur:   # APEX-BEST6NUMBERED-2026-11-14: the NBA board as posted (all games), then only its picks in the window
        _full = P.build_boards(cur['rows'], cur['bans'])[BOARD]['main']
        for _i, _r in enumerate(_full, 1): _r['_spot'] = _i   # APEX-COLLEGETOP3: spot on the posted board
        main = [r for r in _full if inwin(r)]
    else:
        main = P.build_boards(rows, cur['bans'])[BOARD]['main'] if cur else []
    for r in main: out.append(dict(player=r.get('player'), rn=(r.get('groom') or {}).get('n') if isinstance(r.get('groom'), dict) else None, spot=r.get('_spot'), gap=gapd(r.get('groom'), r['line']), num=r['win_prob'], start=r.get('start'), card=K.pick_card(r, 0, rows=rows), saved=cur.get('lines_saved_at'), key=P.T.pick_key(r), db=os.path.join(root, 'data', 'processed', 'bball_tracker.sqlite'), who='%s %s %g %s' % (r['player'], K.stat_name(r), r['line'], r.get('side') or 'MORE')))
else:
    sys.path.insert(0, root)
    import common as CM
    from database import db as DBM
    from picks import pipeline as P
    from rules import rules as RU
    K = CM.dmod('cards')
    s = CM.read_json(os.path.join(CM.CACHE, 'latest_scored.json'), {}) or {}
    rows = [r for r in (s.get('rows') or []) if inwin(r)]
    if rows:
        db = DBM.connect(); inj = P.latest_injuries(db); banned = {r[0] for r in db.execute('select player_norm from bans')}
        for r in rows: r['game_key'] = (frozenset([r.get('team') or '?', r.get('opponent') or '?']), r['game_date'])
        if NUMBERED:   # APEX-BEST6NUMBERED-2026-11-14: only picks in a numbered spot of the sport's own posted board (last scoring)
            keys = set((s.get('mains') or {}).get(BOARD) or []); _src = [r for r in rows if r.get('pick_key') in keys]
            _src.sort(key=lambda r: (s.get('mains') or {}).get(BOARD).index(r['pick_key']))
            for r in _src: r['_spot'] = (s.get('mains') or {}).get(BOARD).index(r['pick_key']) + 1   # APEX-COLLEGETOP3
        else:
            _src = RU.build_boards(rows, inj, banned)[BOARD]['main']
        for r in _src: out.append(dict(player=r.get('player'), rn=(r.get('room') or {}).get('n') if isinstance(r.get('room'), dict) else None, spot=r.get('_spot'), gap=gapd(r.get('room'), r['line']), num=r['confidence'], start=r.get('start'), card=K.pick_card(r, 0, rows=rows), saved=s.get('lines_saved_at'), key=r.get('pick_key'), db=os.path.join(root, 'database', 'history.db'), who='%s %s %g %s' % (r['player'], r.get('label') or r.get('stat'), r['line'], r.get('side') or 'MORE')))
print(json.dumps(out))
"""


# APEX-BEST6TENNIS-2026-11-11 (owner: "Tennis pick should be in too ... I really like to play the day or the next day,
# waiting until Saturday is a long wait"): (1) SOON_ONLY: only games that start today or tomorrow (Central time) can be on the card.
# (2) TENNIS: the top 2 tennis picks are always on the card: the tennis Top 3 builder itself (same rules, bars, first-set picks) run
# on the matches that start today or tomorrow, the way /soon does it (apex_soon.soon_board). The other 4 spots go to the best of the
# other sports, 2 per sport at most. Tennis rules and tennis boards are not changed. Turn off: SOON_ONLY = False / TENNIS = False.
SOON_ONLY = True
SAME_DAY = True; SAME_DAY_HOUR = 19   # APEX-BEST6SAMEDAY-2026-11-12. Turn off: SAME_DAY = False
TENNIS = True
TENNIS_PICKS = 2
_TENNIS_LEGS = []


def _window_word():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return "today" if (SAME_DAY and datetime.now(ZoneInfo("America/Chicago")).hour < SAME_DAY_HOUR) else "today or tomorrow"


def _window_end():
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    n = datetime.now(ZoneInfo("America/Chicago"))
    # APEX-BEST6SAMEDAY-2026-11-12 (owner, "go best6 same day"): before 7 PM Central only games that play TODAY can be on the card,
    # so a game tomorrow never takes a spot from a good pick that plays today. From 7 PM on tomorrow's games are added (early lines).
    return (n + timedelta(days=1 if (not SAME_DAY or n.hour >= SAME_DAY_HOUR) else 0)).replace(hour=23, minute=59, second=0, microsecond=0).astimezone(timezone.utc)


def _tennis():
    """([card text], number of picks, or the error name as text): top TENNIS_PICKS of the tennis Top 3 builder on matches starting today or tomorrow."""
    try:
        from datetime import datetime, timezone
        import apex_soon as S, apex_edge_tracker as t
        now = datetime.now(timezone.utc); end = _window_end()
        picks, note = t.load_current_board()
        if note: return [], 0
        inside = []
        for p in picks:
            ct = S._ct(t._prop_start_utc(p))
            if ct is not None and now < ct <= end: inside.append(p)
        if not inside: return [], 0
        old = S.SOON_HOURS
        try:
            S.SOON_HOURS = (end - now).total_seconds() / 3600.0   # first-set picks inside the same today-or-tomorrow window
            scored, txt = S.soon_board("top3", inside, TENNIS_PICKS)
        finally:
            S.SOON_HOURS = old
        if not scored: return [], 0
        lines = txt.split("\n")
        hb = next((i for i, x in enumerate(lines) if x.startswith("━")), None)
        ti = next((i for i, x in enumerate(lines) if x.startswith("─")), len(lines))
        body = "\n".join(lines[(hb + 1 if hb is not None else 0):ti]).strip()
        global _TENNIS_LEGS
        _TENNIS_LEGS = []
        for e in scored:
            p = e.get("p") or {}
            nm = str(p.get("opponent")) if p.get("player") == "match" else "%s (v %s)" % (p.get("player"), p.get("opponent"))
            _TENNIS_LEGS.append(dict(sport="🎾 Tennis", tennis=True, player=p.get("player"), opp=p.get("opponent"), cat=p.get("category"),
                                     line=float(p.get("line")), side=str(p.get("direction") or "").upper(),
                                     who="%s %s %g %s" % (nm, str(p.get("category") or "").replace("_", " "), float(p.get("line")), str(p.get("direction") or "").upper()),
                                     start=str(t._prop_start_utc(p) or ""), result=None))
        return ([body] * 1 if body else []), (len(scored) if body else 0)
    except Exception as e:
        return [], type(e).__name__


# APEX-BOARDREFRESH-2026-11-12 (owner: "Whenever I run a board I want everything refresh, I hate seeing picks that's not available"):
# before this card is built, every sport whose PrizePicks lines are older than REFRESH_AGE_MIN and has a game inside the card's window
# reads PrizePicks again and re-scores (that sport's own scheduler/refresh.py, which keeps the call spacing and the slow-down pause).
# NBA is not included: it reads PrizePicks on its own every 30 minutes. Turn off: REFRESH_ON = False.
REFRESH_ON = True
REFRESH_AGE_MIN = 15

def stale_sports():
    out = []
    if not REFRESH_ON: return out
    import time as _t
    from datetime import datetime as _dt
    for root, name in ROOTS:
        f = os.path.join(SPORTS, root, "scheduler", "refresh.py")
        if not os.path.exists(f): continue
        try:
            lp = (json.load(open(os.path.join(SPORTS, root, "cache", "scheduler_state.json"))) or {}).get("last_pp_ok")
            age = (_t.time() - _dt.fromisoformat(lp).timestamp()) / 60.0 if lp else None
        except Exception: age = None
        if age is None or age >= REFRESH_AGE_MIN: out.append((root, name, age))
    return out

def refresh_sports():
    """Reads PrizePicks again for each stale sport, one after the other. Returns (names refreshed, plain warning lines)."""
    did, notes = [], []
    for root, name, age in stale_sports():
        try:
            p = subprocess.run(["/usr/bin/python3", os.path.join(SPORTS, root, "scheduler", "refresh.py"), _window_end().isoformat() if SOON_ONLY else ""], capture_output=True, text=True, timeout=420, cwd=os.path.join(SPORTS, root))
            r = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception as e:
            r = dict(did=False, note="%s: refresh did not finish (%s), card uses its last saved lines." % (name, type(e).__name__))
        if r.get("did"): did.append(name)
        if r.get("note"): notes.append(r["note"])
    return did, notes


def _sport(root, board="top10", window=True):
    """That sport's best-group numbered picks, best first. Never raises: (picks, error or None)."""
    try:
        p = subprocess.run(["/usr/bin/python3", "-c", _SNIP, os.path.join(SPORTS, root), _window_end().isoformat() if (SOON_ONLY and window) else "", board,
                            "1" if (NUMBERED_ONLY and board in ("top10", "top3", "bestless") and not (TODAY_SPOTS and window)) else "0"], capture_output=True, text=True, timeout=90)
        v = json.loads(p.stdout.strip().splitlines()[-1])
        if TODAY_SPOTS and window:   # APEX-TODAYSPOTS-2026-11-14: spot = place among the card window's games, ties by the bigger real record
            _nm = dict(ROOTS).get(root)
            try:
                import apex_honest_cards as _HC
                for x in v: x["_r"] = _HC.honest(_nm, x.get("num"))[0]
            except Exception:
                pass
            v.sort(key=lambda x: (-round(x.get("num") or 0), -(x.get("_r") or 0), -(x.get("rn") or 0), -(x.get("num") or 0)))   # never random
            seen, keep = set(), []
            for x in v:   # one spot per player
                if x.get("player") and x["player"] in seen: continue
                seen.add(x.get("player")); keep.append(x)
            v = keep
            for i, x in enumerate(v, 1): x["spot"] = i
        return v, None
    except Exception as e:
        return [], type(e).__name__


# APEX-BEST6BOARDS-2026-11-11 (owner: "the full boards should be on the best6of6 for each sport with the new room gap with the
# clear explanation"): every pick shows its gap line (PrizePicks line, the sportsbooks' number, how far apart, Apex's score), and under
# the 6 picks the card lists each sport's FULL board of qualifying picks the same way. Display only. Turn off: FULL_BOARDS = False.
FULL_BOARDS = True
_PER = {}


def _g(v):
    return ("%.1f" % v).rstrip("0").rstrip(".")


def gap_line(x):
    g = x.get("gap")
    if not g: return ""
    kind = "Elite" if g["steps"] >= 3 else "Strong" if g["steps"] >= 2 else "Solid" if g["steps"] >= 1 else "Thin"
    return "📏 PrizePicks line %s · Sportsbook line %s · Gap +%s · Gap grade: %s · Apex score %d" % (_g(g["line"]), _g(g["book"]), _g(g["room"]), kind, round(g["was"]))


# APEX-BEST6REAL80-2026-11-14 (owner, "make 80 the minimum", about "77 weak real (Apex 90)"): Best 6 of 6, Best 3 and Best Goblin LESS
# only take picks whose REAL number (apex_honest_cards: how often that sport's picks with the same Apex band really won) is REAL_MIN or more,
# and rank by the real number (ties by Apex). A pick with no real number (not enough graded history) keeps the old rule. Sport boards are
# not changed. Undo: REAL_MIN = None, restart the tennis bot.
REAL_MIN = 80
# APEX-COLLEGETOP3-2026-11-14 (owner: "if it isn't in anything less than the number 4 spot of its own board I don't want to see it"):
# Read as spots 1-3: Best 6 of 6, Best 3 and Best Goblin LESS only take picks in spots #1-#3 of their sport's OWN posted board (its Top 10 /
# Best LESS / Top 3 numbering, the posted spot, not the spot among today's games). Undo: TOP_SPOTS = None.
# APEX-TODAYSPOTS-2026-11-14 (owner, "Go today spots"): spots are counted among the card window's games only (today's picks against today's,
# by the sport's own board rules), so tomorrow's games can't push tonight's picks off; picks tied on the number are ordered by the bigger real
# record (more past picks behind the number). Undo: TODAY_SPOTS = False.
TODAY_SPOTS = True
TOP_SPOTS = 4   # APEX-TOP4-2026-11-14 (owner, "I said anything after 4 so 4 is fine"): spots #1-#4
MAX_CFB = 2   # APEX-COLLEGETOP3-2026-11-14: never more than 2 college picks on a card, even when the card is short


def _real80(v, name):
    if REAL_MIN is None: return v
    try:
        import apex_honest_cards as _HC
    except Exception:
        return v
    out = []
    for x in v:
        r = _HC.honest(name, x.get("num"))[0]
        x["real"] = r
        if r is None or r >= REAL_MIN: out.append(x)
    out.sort(key=lambda x: (-(x["real"] if x.get("real") is not None else x["num"]), -x["num"]))
    return out


def _rk(x):
    return -(x.get("real") if x.get("real") is not None else x["num"]), -x["num"]


def build(size=SIZE):
    per, notes = {}, []
    for root, name in _roots():
        per[name], err = _sport(root)
        if err: notes.append("%s could not be read (%s), left out" % (name, err))
        per[name] = _real80([x for x in (per[name] or []) if x.get('spot') is None or x['spot'] <= TOP_SPOTS] if TOP_SPOTS else per[name], name)   # APEX-BEST6REAL80 + APEX-COLLEGETOP3: spots 1-4 only
    picks = []; rest = []
    for root, name in _roots():
        for i, x in enumerate(per[name]):
            x["sport"] = name
            (picks if i < PER_SPORT else rest).append(x)
    picks.sort(key=_rk); picks = picks[:size]
    rest.sort(key=_rk)
    while len(picks) < size and rest:   # a short sport's open spots go to the next best of the others
        x = rest.pop(0)
        if MAX_CFB is not None and x.get("sport") == "🏈 College football" and sum(1 for y in picks if y.get("sport") == x["sport"]) >= MAX_CFB: continue   # APEX-COLLEGETOP3: college stays max 2
        picks.append(x)
    picks.sort(key=_rk)
    global _PER
    _PER = per
    return picks, {name: len(v) for name, v in per.items()}, notes


def render():
    if not ON: return "⭐ BEST 6 OF 6 is turned off."
    tcards, tn = _tennis() if (TENNIS and TENNIS_ON_CARD) else ([], 0)
    terr = tn if isinstance(tn, str) else None
    if terr: tn = 0
    picks, counts, notes = build(SIZE - tn)
    if terr: notes.append("🎾 Tennis could not be read (%s), left out" % terr)
    L = ["⭐ **BEST 6 OF 6** · top 2 of every sport, best 6 kept" + ((" · games %s only" % _window_word() + (" (tomorrow's games join at 7 PM CT)" if _window_word() == "today" else "")) if SOON_ONLY else ""),
         ("🎾 Tennis: the top 2 of the tennis Top 3 rules. Other sports: best-group Goblins only (proven room under the sportsbooks + a high Apex rating)." if TENNIS_ON_CARD
          else "Best-group Goblins only: proven room under the sportsbooks + a high Apex rating. No tennis on this card (the tracker below follows it both ways).")]
    if CFB_OFF_CARDS: L.append(CFB_OFF_LINE)
    if REAL_MIN is not None: L.append("🎯 Only picks whose REAL win rate is %d+ (how often that sport's picks with the same Apex number really won), ranked by it." % REAL_MIN)
    L.append("Qualified right now: " + " · ".join("%s %d" % (n, c) for n, c in counts.items()) + (" · 🎾 Tennis %d" % tn if (TENNIS and TENNIS_ON_CARD) else ""))
    total = len(picks) + tn
    if not total:
        L.append("🚫 No pick qualifies right now. Nothing to play from this card.")
    elif total < SIZE:
        L.append("⚠️ Short card: fewer than 6 qualified. This is not a full 6-pick slip.")
    try:   # APEX-BOARDSTRENGTH-2026-11-12
        import apex_strength as _ST
        _sl, _sh = _ST.block("best6", picks, SIZE)
    except Exception:
        _sl, _sh = [], {}
    L += _sl
    if tcards:
        L += ["━━━━━━━━━━━━", "🎾 **Tennis**", tcards[0]]
    for n, x in enumerate(picks, 1):
        card = x["card"].replace("**#0 · ", "**")
        L += ["━━━━━━━━━━━━", "**%s** %s" % ("Next" if tcards else "%d." % n, x["sport"]) + ((" · " + _sh[x.get("key")]) if _sh.get(x.get("key")) else "")] + ([gap_line(x)] if (FULL_BOARDS and gap_line(x)) else []) + [card]
    if FULL_BOARDS:
        L += ["━━━━━━━━━━━━", "📋 **FULL BOARD OF EVERY SPORT** (every pick that qualifies, best first)",
              "Gap = sportsbook line minus the PrizePicks line (for a LESS pick: the PrizePicks line minus the sportsbook line). Gap grades: Solid (1 step), Strong (2 steps), Elite (3 or more steps). Apex score = Apex's own rating of the pick. The number in ( ) = how often picks with that gap and score won, out of 100."]
        for root, name in _roots():
            v = _PER.get(name) or []
            L.append("**%s** · %d qualify" % (name, len(v)) if v else "**%s** · 🚫 nothing qualifies for %s" % (name, _window_word()))
            for i, x in enumerate(v, 1):
                g = x.get("gap")
                L.append("%d. (%d) %s%s" % (i, round(x["num"]), x.get("who"), " · Sportsbook line %s · Gap +%s · Apex score %d" % (_g(g["book"]), _g(g["room"]), round(g["was"])) if g else ""))
    for t in notes: L.append("⚠️ " + t)
    try:
        _rl = record_line()
        if _rl: L.append(_rl)
        import apex_best6_times as _b6t   # APEX-BEST6TIMES-2026-11-13
        L.append(_b6t.record_line())
    except Exception:
        pass
    L.append("Check every line in the app before you play. Same picks as each sport's own boards; nothing was re-pulled.")
    return "\n".join(L)



# APEX-BEST6TRACK-2026-11-11 (owner: "remove tennis from the best6of6 and ... create a tracker to track best6of6 with
# tennis and without"): TENNIS_ON_CARD = False takes tennis off the card. Once a day, at the first check at or after 4:30 PM
# Central (apex_alerts.check, every 30 min), BOTH versions of the card are saved to data/best6_tracker.jsonl:
#   "without" = the card as shown (best 6 best-group Goblins, 2 per sport at most)
#   "with"    = up to 2 tennis picks (tennis Top 3 rules) + the best of the other sports to make 6
# Each pick is graded from its own sport's results; a card WON only when every pick on it won. A card with fewer than 6 picks is
# saved as short and not counted. A line goes to #system when the day is saved and when it is graded. Read only on all other files.
TENNIS_ON_CARD = False
TRACK_ON = True
TRACK_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "best6_tracker.jsonl")
TRACK_AFTER = (16, 30)   # Central time
NAMES = {"without": "all 6 without tennis", "with": "all 6 with tennis", "without3": "top 3 without tennis", "with3": "top 3 with tennis"}
TRACK_TOP3 = True
PLAYED_ZERO = True   # APEX-BEST6GRADING-2026-11-14: a played 0 is a loss (MORE) the way PrizePicks grades it; see _grade


def _now_ct():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Chicago"))


def _leg(x):
    return dict(sport=x["sport"], who=x.get("who"), key=x.get("key"), db=x.get("db"), num=round(float(x.get("num") or 0), 1), start=str(x.get("start") or ""), result=None)


def versions():
    """{'without': [legs], 'with': [legs]} as of now."""
    tcards, tn = _tennis() if TENNIS else ([], 0)
    tl = list(_TENNIS_LEGS) if (tcards and not isinstance(tn, str)) else []
    wo, _, _ = build(SIZE)
    wi, _, _ = build(SIZE - len(tl))
    return {"without": [_leg(x) for x in wo], "with": [dict(l) for l in tl] + [_leg(x) for x in wi]}


def _read():
    out = []
    try:
        with open(TRACK_LOG) as fh:
            for ln in fh:
                if ln.strip(): out.append(json.loads(ln))
    except Exception:
        pass
    return out


def _write(rows):
    os.makedirs(os.path.dirname(TRACK_LOG), exist_ok=True)
    with open(TRACK_LOG + ".tmp", "w") as fh:
        for r in rows: fh.write(json.dumps(r) + "\n")
    os.replace(TRACK_LOG + ".tmp", TRACK_LOG)


def _grade(l):
    """'win' / 'loss' / 'void' or None (not graded yet). Never raises."""
    import sqlite3
    try:
        if l.get("tennis"):
            con = sqlite3.connect("file:%s?mode=ro" % os.path.join(os.path.dirname(TRACK_LOG), "board_history.sqlite"), uri=True)
            r = con.execute("select result from history where category=? and abs(line-?)<0.01 and upper(direction)=? and player=? and opponent=? "
                            "and result is not null and published_at>=? order by id desc limit 1",
                            (l.get("cat"), l.get("line"), l.get("side"), l.get("player"), l.get("opp"), str(l.get("saved_day") or "2026-11-11"))).fetchone()
            con.close()
            return {"hit": "win", "miss": "loss", "void": "void"}.get(str(r[0]).lower()) if r else None
        con = sqlite3.connect("file:%s?mode=ro" % l["db"], uri=True)
        r = con.execute("select status from picks where pick_key=?", (l["key"],)).fetchone()
        st = str((r[0] if r else "") or "")
        # APEX-BEST6GRADING-2026-11-14 (owner, "go fix best6"): grade like PrizePicks. A sport can void a pick when the player
        # is in the box score but has no stat line (Trenton Chaney 11/12: played, 0 catches). PrizePicks counts that as a played 0:
        # a MORE loses, a LESS wins. Only a player not in the box at all (true did-not-play) stays void. Undo: PLAYED_ZERO = False.
        if st == "void" and PLAYED_ZERO:
            try:
                q = con.execute("select player_norm, game_date, side from picks where pick_key=?", (l["key"],)).fetchone()
                if q and con.execute("select 1 from player_games where player_norm=? and game_date=? and stats_json like '%\"_played\": 1%' limit 1", (q[0], q[1])).fetchone():
                    st = "loss" if str(q[2]).upper() == "MORE" else "win"
            except Exception:
                pass
        con.close()
        return "void" if st == "push" else st if st in ("win", "loss", "void") else None
    except Exception:
        return None


def _finish(day):
    """Grade one saved day in place. True when something changed."""
    from datetime import datetime, timezone, timedelta
    ch = False
    for v, legs in day["versions"].items():
        if (day.get("results") or {}).get(v): continue
        for l in legs:
            if l.get("result") is None:
                l["saved_day"] = day["day"]
                g = _grade(l)
                if g: l["result"] = g; ch = True
        res = [l.get("result") for l in legs]
        starts = [datetime.fromisoformat(str(l["start"]).replace("Z", "+00:00")) for l in legs if l.get("start")]
        late = bool(starts) and datetime.now(timezone.utc) - max(x if x.tzinfo else x.replace(tzinfo=timezone.utc) for x in starts) > timedelta(days=3)
        if legs and (all(res) or late):
            miss = [l["who"] for l in legs if l.get("result") == "loss"]
            if len(legs) < SIZE: out = "short"
            elif not all(res): out = "unknown"
            else: out = "lost" if miss else "won"
            day.setdefault("results", {})[v] = dict(result=out, misses=miss, hits=sum(1 for r in res if r == "win"), n=len(legs)); ch = True
        elif not legs and not (day.get("results") or {}).get(v):
            day.setdefault("results", {})[v] = dict(result="short", misses=[], hits=0, n=0); ch = True
    # APEX-BEST6TOP3-2026-11-11 (owner, "have it grade that as well"): the top 3 picks of each card, graded as their own 3-pick slip
    if TRACK_TOP3:
        for v, legs in day["versions"].items():
            k = v + "3"
            if (day.get("results") or {}).get(k): continue
            l3 = legs[:3]; res = [l.get("result") for l in l3]
            if len(l3) < 3:
                if (day.get("results") or {}).get(v): day.setdefault("results", {})[k] = dict(result="short", misses=[], hits=sum(1 for r in res if r == "win"), n=len(l3)); ch = True
            elif all(res):
                miss = [l["who"] for l in l3 if l.get("result") == "loss"]
                day.setdefault("results", {})[k] = dict(result="lost" if miss else "won", misses=miss, hits=sum(1 for r in res if r == "win"), n=3); ch = True
            elif (day.get("results") or {}).get(v):
                day.setdefault("results", {})[k] = dict(result="unknown", misses=[], hits=sum(1 for r in res if r == "win"), n=3); ch = True
    return ch


def record_line():
    if not TRACK_ON: return ""
    rows = _read()
    if not rows: return "📒 Best 6 of 6 tracker: starts with today's 4:30 PM card. Both versions (with tennis, without tennis) are saved and graded every day."
    parts = []
    for v in ("without", "with") + (("without3", "with3") if TRACK_TOP3 else ()):
        rs = [(d.get("results") or {}).get(v) for d in rows]
        w = sum(1 for r in rs if r and r["result"] == "won"); lo = sum(1 for r in rs if r and r["result"] == "lost")
        sh = sum(1 for r in rs if r and r["result"] == "short"); wt = sum(1 for r in rs if not r)
        hits = sum(r["hits"] for r in rs if r); n = sum(r["n"] for r in rs if r and r["result"] in ("won", "lost", "short"))
        parts.append("%s: won %d, lost %d%s%s (picks %d of %d)" % (NAMES[v], w, lo, ", %d short" % sh if sh else "", ", %d waiting" % wt if wt else "", hits, n))
    return "📒 Best 6 of 6 tracker since %s (the 4:30 PM card; every pick must hit) · " % rows[0]["day"][5:].replace("-", "/") + " · ".join(parts)


def run(post=True):
    """Called from apex_alerts.check() every 30 min: save today's two cards once, grade open days. Never raises."""
    out = []
    if not (ON and TRACK_ON): return out
    try:
        import apex_ops as ops
        say = (lambda t: ops.send_discord("system", t)) if post else (lambda t: print("---- would post to #system ----\n" + t))
        rows = _read(); now = _now_ct(); today = now.strftime("%Y-%m-%d"); ch = False
        if (now.hour, now.minute) >= TRACK_AFTER and not any(d["day"] == today for d in rows):
            vs = versions()
            rows.append(dict(day=today, saved=now.isoformat(timespec="seconds"), versions=vs, results={})); ch = True
            t = "⭐ Best 6 of 6 tracker: saved today's two cards (%s). " % now.strftime("%-I:%M %p CT") + " · ".join(
                "%s %d picks%s" % (NAMES[v].replace("all 6 ", ""), len(vs[v]), "" if len(vs[v]) >= SIZE else " (short, will not count)") for v in ("without", "with")) + (". The top 3 of each card is graded as its own slip too." if TRACK_TOP3 else "")
            say(t); out.append(t)
        for d in rows:
            before = dict(d.get("results") or {})
            if _finish(d):
                ch = True
                for v in ("without", "with", "without3", "with3"):
                    r = (d.get("results") or {}).get(v)
                    if r and v not in before:
                        t = "⭐ Best 6 of 6 result %s, %s: %s" % (d["day"][5:].replace("-", "/"), NAMES[v],
                            "✅ won, all %d hit" % r["n"] if r["result"] == "won" else "❌ lost (%d of %d hit; missed: %s)" % (r["hits"], r["n"], ", ".join(r["misses"])) if r["result"] == "lost"
                            else "⚠️ short card (%d picks, %d hit), not counted" % (r["n"], r["hits"]) if r["result"] == "short" else "could not be graded")
                        say(t); out.append(t)
        if ch: _write(rows)
    except Exception as e:
        print("[best6] tracker skipped: %s: %s" % (type(e).__name__, e))
    return out


# APEX-BEST6ALERT-2026-11-11 (owner, "Go best6 alerts", after "how do I know when the cards are refreshed"): every 30 minutes
# (apex_alerts.check) the card is rebuilt and compared with the last one. When a pick came in, left, or its line moved, ONE message
# goes to tennis #general: what came in, what left and why, whether the card is a full 6, and the card in rank order. Quiet when
# nothing changed and between 11 PM and 8 AM Central (the first check after 8 AM reports the overnight change). Read only.
# Turn off: ALERT_ON = False, restart the tennis bot.
ALERT_ON = True
ALERT_CHANNEL = "general"
ALERT_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "best6_alert_state.json")
ALERT_HOURS = (8, 23)   # Central time, first hour on .. first hour off


def _akey(x):
    w = str(x.get("who") or "").rsplit(" ", 2)
    return "%s|%s" % (x.get("sport"), w[0])


def _aline(x, n=None):
    g = x.get("gap")
    t = "%s %s" % (x.get("sport"), x.get("who"))
    if g: t += " · Sportsbook line %s · Gap +%s · Apex score %d" % (_g(g["book"]), _g(g["room"]), round(g["was"]))
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        d = datetime.fromisoformat(str(x.get("start")).replace("Z", "+00:00")); d = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(ZoneInfo("America/Chicago"))
        t += " · starts %s" % d.strftime("%a %-I:%M %p CT")
    except Exception:
        pass
    return ("%d. " % n if n else "") + t


def alert(post=True):
    """Called from apex_alerts.check(): one #general message when the Best 6 of 6 card changed. Never raises. Returns the texts sent."""
    out = []
    if not (ON and ALERT_ON): return out
    try:
        from datetime import datetime, timezone
        now = _now_ct()
        if not (ALERT_HOURS[0] <= now.hour < ALERT_HOURS[1]): return out
        picks, counts, notes = build(SIZE)
        if notes: return out   # a sport could not be read: do not report its picks as gone
        cur = [dict(key=_akey(x), who=x.get("who"), sport=x.get("sport"), start=str(x.get("start") or ""), line=_aline(x)) for x in picks]
        try:
            with open(ALERT_STATE) as fh: st = json.load(fh)
        except Exception:
            st = None
        old = (st or {}).get("picks") or []
        ok = {o["key"]: o for o in old}; ck = {c["key"]: c for c in cur}
        came = [c for c in cur if c["key"] not in ok]
        left = [o for o in old if o["key"] not in ck]
        moved = [(ok[c["key"]], c) for c in cur if c["key"] in ok and ok[c["key"]].get("who") != c["who"]]
        today = now.strftime("%Y-%m-%d"); full = len(cur) >= SIZE
        first_full = full and (st or {}).get("full_day") != today
        if st is not None and not (came or left or moved or first_full):
            return out
        L = ["⭐ **BEST 6 OF 6 UPDATE** · %s · %s" % (now.strftime("%-I:%M %p CT"), "✅ full 6 ready" if full else "⚠️ short card, %d of 6" % len(cur))]
        if st is None: L.append("First check: this is the card right now.")
        for c in came if st is not None else []: L.append("➕ In: " + c["line"])
        for o, c in moved: L.append("🔁 Line moved: %s (was %s)" % (c["line"], o.get("who")))
        utc = datetime.now(timezone.utc)
        for o in left:
            why = "no longer qualifies (its line moved or was pulled, an injury listing, or a stronger pick took the spot)"
            try:
                t = datetime.fromisoformat(str(o.get("start")).replace("Z", "+00:00")); t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
                if t <= utc: why = "its game started"
            except Exception:
                pass
            L.append("➖ Out: %s %s, %s" % (o.get("sport"), o.get("who"), why))
        if cur:
            L.append("**The card now, best first:**")
            for n, x in enumerate(picks, 1): L.append(_aline(x, n))
        else:
            L.append("🚫 No pick qualifies right now.")
        L.append("Ranked by how often picks with that gap and Apex score won. Run /best6of6 for the full cards. Check every line in the app before you play.")
        txt = "\n".join(L)
        if post:
            import apex_ops as ops
            ops.send_discord(ALERT_CHANNEL, txt)
            os.makedirs(os.path.dirname(ALERT_STATE), exist_ok=True)
            with open(ALERT_STATE + ".tmp", "w") as fh: json.dump(dict(saved=now.isoformat(timespec="seconds"), picks=cur, full_day=today if full else (st or {}).get("full_day")), fh)
            os.replace(ALERT_STATE + ".tmp", ALERT_STATE)
        else:
            print("---- would post to #%s ----\n%s" % (ALERT_CHANNEL, txt))
        out.append(txt)
    except Exception as e:
        print("[best6] alert skipped: %s: %s" % (type(e).__name__, e))
    return out


if __name__ == "__main__":
    print(render())


# APEX-BESTLESSCARD-2026-11-12 (owner: "I want the best less command in the tennis general ... the best possible picks is less from
# the other sports just like we did best6of6"): /bestless (also /goblinless). One card across NFL, college football, NHL and NBA: each
# sport's own BEST GOBLIN LESS board (a full step or more above the sportsbooks and 70 of 100 or better), top 2 of every sport, best 6 kept,
# same game window as Best 6 of 6, then every sport's full Goblin LESS board as a list. Read only, same as /best6of6: each sport's own code
# builds its picks and cards; nothing is pulled from PrizePicks. Turn off: LESS_ON = False, restart the tennis bot.
LESS_ON = True


def _less_head(x):
    g = x.get("gap")
    if not g: return ""
    kind = "Elite" if g["steps"] >= 3 else "Strong" if g["steps"] >= 2 else "Solid" if g["steps"] >= 1 - 1e-9 else "Thin"
    return "📏 PrizePicks line %s · Sportsbook line %s · Gap +%s above · Gap grade: %s · Apex score on LESS %d" % (_g(g["line"]), _g(g["book"]), _g(g["room"]), kind, round(g["was"]))


def render_less():
    if not LESS_ON: return "🟢 BEST GOBLIN LESS is turned off."
    from datetime import datetime
    from zoneinfo import ZoneInfo
    per, later, notes = {}, {}, []
    for root, name in _roots():
        per[name], err = _sport(root, "bestless")
        if err: notes.append("%s could not be read (%s), left out" % (name, err)); continue
        per[name] = _real80([x for x in (per[name] or []) if x.get('spot') is None or x['spot'] <= TOP_SPOTS] if TOP_SPOTS else per[name], name)   # APEX-BEST6REAL80 + APEX-COLLEGETOP3
        allp, err2 = _sport(root, "bestless", window=False)
        keys = {x.get("key") for x in per[name]}
        later[name] = [x for x in (allp or []) if x.get("key") not in keys]
    picks, rest = [], []
    for root, name in _roots():
        for i, x in enumerate(per.get(name) or []):
            x["sport"] = name
            (picks if i < PER_SPORT else rest).append(x)
    picks.sort(key=_rk); picks = picks[:SIZE]; rest.sort(key=_rk)
    while len(picks) < SIZE and rest: picks.append(rest.pop(0))
    picks.sort(key=_rk)
    L = ["🟢 **BEST GOBLIN LESS** · all sports · top 2 of every sport, best 6 kept" + ((" · games %s only" % _window_word() + (" (tomorrow's games join at 7 PM CT)" if _window_word() == "today" else "")) if SOON_ONLY else ""),
         "Goblin LESS = a line where PrizePicks sells LESS as a Goblin. Best of the best only: a full step or more above the sportsbooks, 80 of 100 or better with that gap and Apex score, and Apex 75 or higher on LESS. Short cards are normal. No tennis on this card.",
         "Qualified right now: " + " · ".join("%s %d" % (name, len(per.get(name) or [])) for root, name in _roots())]
    if CFB_OFF_CARDS: L.append(CFB_OFF_LINE)
    if REAL_MIN is not None: L.append("🎯 Only picks whose REAL win rate is %d+, ranked by it." % REAL_MIN)
    if not picks: L.append("🚫 No Goblin LESS pick qualifies for %s. Nothing to play from this card." % _window_word())
    elif len(picks) < SIZE: L.append("⚠️ Short card: %d qualified. This is not a full 6-pick slip." % len(picks))
    try:   # APEX-BOARDSTRENGTH-2026-11-12
        import apex_strength as _ST
        _sl, _sh = _ST.block("bestless", picks, SIZE)
    except Exception:
        _sl, _sh = [], {}
    L += _sl
    for n, x in enumerate(picks, 1):
        card = x["card"].replace("**#0 · ", "**")
        L += ["━━━━━━━━━━━━", "**%d.** %s" % (n, x["sport"]) + ((" · " + _sh[x.get("key")]) if _sh.get(x.get("key")) else "")] + ([_less_head(x)] if _less_head(x) else []) + [card]
    L += ["━━━━━━━━━━━━", "📋 **FULL GOBLIN LESS BOARD OF EVERY SPORT** (best first)",
          "Gap = the PrizePicks line minus the sportsbook line. For a LESS pick, the higher the line sits above the sportsbooks, the better. Gap grades: Solid (1 step), Strong (2 steps), Elite (3 or more steps). Apex score = Apex's own rating of LESS. The number in ( ) = how often LESS picks with that gap and score won, out of 100.",
          "🪜 On a LESS card the ladder numbers are for the LESS side: P = Apex chance the player stays under that line, HP = how often that really happened."]
    def row(i, x):
        g = x.get("gap")
        return "%d. (%d) %s%s" % (i, round(x["num"]), x.get("who"), " · Sportsbook line %s · Gap +%s · Apex score %d" % (_g(g["book"]), _g(g["room"]), round(g["was"])) if g else "")
    for root, name in _roots():
        v = per.get(name) or []
        L.append("**%s** · %d qualify" % (name, len(v)) if v else "**%s** · 🚫 nothing qualifies for %s" % (name, _window_word()))
        for i, x in enumerate(v, 1): L.append(row(i, x))
        lt = later.get(name) or []
        if lt:
            L.append("⏳ %s later games (not on the card yet): %d" % (name, len(lt)))
            for i, x in enumerate(lt, 1):
                try: d = datetime.fromisoformat(str(x.get("start")).replace("Z", "+00:00")).astimezone(ZoneInfo("America/Chicago")).strftime("%a %-m/%-d")
                except Exception: d = "?"
                L.append("   " + row(i, x) + " · plays %s" % d)
    for t in notes: L.append("⚠️ " + t)
    L.append("Numbers come from past games. The first real Goblin LESS picks are graded 11/13. Check every line in the app before you play. Same picks as each sport's own /…-bestless board; nothing was re-pulled.")
    return "\n".join(L)
