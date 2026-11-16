#!/usr/bin/env python3
"""APEX-HONESTCARDS-2026-11-14 (owner, "Fix all", after the calibration check: picks shown 90-94 really won about 83 of 100,
95+ about 81, 85-89 about 79). The honest number for a pick = how often that sport's graded picks in the same shown-number band really
won (since 11/5, one result per player-game-stat-side-band so ladder rungs don't repeat). Bands: 75-79, 80-84, 85-89, 90-94, 95+.
A sport-band cell needs 30+ graded picks, else the all-sports number for that band, else the shown number itself.
Rebuilt at most every 6 hours into data/honest_cards.json. Read only on every sport. Display only: used by apex_strength for the
cross-sport cards (Best 6 of 6, Best Goblin LESS); no pick, board or order changes. Undo: ON = False, restart the tennis bot."""
import json, os, sqlite3, collections, time

ON = True
MIN_N = 30
SINCE = "2026-11-05"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "honest_cards.json")
A = os.path.expanduser("~/ApexSports")
SPORT = {"NFL": "NFL", "College football": "NCAAF", "NHL": "NHL", "NBA": "NBA", "College basketball": "NCAAB", "Soccer": "Soccer", "Tennis": "Tennis"}
_C = {}


def band(n):
    n = float(n)
    return "95+" if n >= 95 else "90-94" if n >= 90 else "85-89" if n >= 85 else "80-84" if n >= 80 else "75-79" if n >= 75 else None


def _rows():
    R = []
    for sp, d in (("NFL", "football/nfl"), ("NCAAF", "football/ncaaf"), ("NHL", "hockey/nhl"), ("NCAAB", "basketball/ncaab"), ("Soccer", "soccer")):
        try:
            db = sqlite3.connect("file:%s/%s/database/history.db?mode=ro" % (A, d), uri=True)
            for pn, gd, stat, side, conf, st in db.execute("select player_norm, game_date, stat, side, confidence, status from picks where status in ('win','loss') and confidence>=75 and game_date>=?", (SINCE,)):
                R.append((sp, (pn, gd, stat, side), conf, st == "win"))
            db.close()
        except Exception:
            pass
    try:
        db = sqlite3.connect("file:%s/basketball/data/processed/bball_tracker.sqlite?mode=ro" % A, uri=True)
        for pl, gd, stat, side, wp, st in db.execute("select player, game_date, stat, side, win_prob, status from picks where league='NBA' and status in ('win','loss') and win_prob>=75 and game_date>=?", (SINCE,)):
            R.append(("NBA", (pl, gd, stat, side), wp, st == "win"))
        db.close()
    except Exception:
        pass
    try:
        db = sqlite3.connect("file:%s?mode=ro" % os.path.join(HERE, "data", "props_db.sqlite"), uri=True)
        for pl, md, stat, dr, pr, mw, lw in db.execute("select player, match_date, stat_type, engine_direction, engine_prob, more_won, less_won from projections where engine_prob>=0.75 and more_won in (0,1) and match_date>=?", (SINCE,)):
            won = mw if str(dr).upper() == "MORE" else lw
            if won in (0, 1):
                R.append(("Tennis", (pl, md, stat, dr), 100 * pr, won == 1))
        db.close()
    except Exception:
        pass
    return R


def build():
    g = collections.defaultdict(list)
    for sp, k, n, w in _rows():
        b = band(n)
        if b: g[(sp, k, b)].append((n, w))
    cells = collections.defaultdict(lambda: [0, 0])
    for (sp, k, b), v in g.items():
        n, w = sorted(v)[len(v) // 2]
        for s in (sp, "ALL"):
            cells["%s|%s" % (s, b)][0] += w; cells["%s|%s" % (s, b)][1] += 1
    t = dict(built=time.time(), since=SINCE, cells=dict(cells))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT + ".tmp", "w") as fh:
        json.dump(t, fh)
    os.replace(OUT + ".tmp", OUT)
    _C.clear()
    return t


def _table():
    if _C.get("t") and time.time() - _C.get("at", 0) < 600:
        return _C["t"]
    try:
        t = json.load(open(OUT))
        if time.time() - float(t.get("built") or 0) > 6 * 3600:
            t = build()
    except Exception:
        t = build()
    _C.update(t=t, at=time.time())
    return t


def sport_key(label):
    s = str(label or "")
    for k, v in SPORT.items():
        if k.lower() in s.lower():
            return v
    return None


def honest(sport_label, num):
    """(honest number, how many graded picks it comes from, which cell) or (None, 0, '') when it can't say."""
    if not ON or num is None:
        return None, 0, ""
    try:
        b = band(num)
        if not b:
            return None, 0, ""
        C = _table().get("cells") or {}
        sp = sport_key(sport_label)
        for k, lab in (("%s|%s" % (sp, b), "this sport"), ("ALL|%s" % b, "all sports")):
            if sp is None and k.startswith("None"):
                continue
            w, n = C.get(k, (0, 0))
            if n >= MIN_N:
                return 100.0 * w / n, n, lab
    except Exception:
        pass
    return None, 0, ""


if __name__ == "__main__":
    t = build()
    for k in sorted(t["cells"]):
        w, n = t["cells"][k]
        print("%-14s won %d of %d = %d" % (k, w, n, round(100 * w / n)))
