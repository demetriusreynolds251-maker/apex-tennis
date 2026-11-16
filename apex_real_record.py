"""APEX-REALRECORD-2026-10-31: '🧾 Real record' card line (display only).
How props of the same type (Standard/Goblin/Demon), category and side actually did at this win-chance band,
from data/board_history.sqlite (All Props board, last graded state per prop per day, since 10/10).
His words 10/30 10:42 PM CT: "It need to say it on the output". Switch REAL_RECORD_LINE in apex_edge_tracker.py."""
import json, os, sqlite3, time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "board_history.sqlite")
CACHE = os.path.join(HERE, "data", "real_record_cache.json")
SINCE = "2026-10-10"
TTL_SEC = 6 * 3600
BANDS = [(60, 65), (65, 70), (70, 75), (75, 80), (80, 101)]
MIN_GRADED = 5
NAMES = {"total_games": "Total Games", "fantasy_score": "Fantasy", "aces": "Aces", "dfs": "Double Faults",
         "breaks": "Breaks", "games_won": "Games Won", "tiebreaks": "Tiebreaks", "total_sets": "Total Sets"}
_MEM = {"t": 0, "c": None}


def _band(wp):
    for lo, hi in BANDS:
        if lo <= wp < hi:
            return lo
    return None


def _ptype(t):
    t = str(t or "standard").lower()
    return t if t in ("goblin", "demon") else "standard"


def build():
    db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    rows = db.execute("""select h.board_day, h.player, h.opponent, h.category, upper(h.direction), h.line, h.prop_type, h.win_prob, h.result
      from history h join publishes p using(publish_id) where p.platform='prizepicks' and h.board='allprops'
      and h.board_day >= ? and h.win_prob >= 60 and h.result in ('hit','miss')""", (SINCE,)).fetchall()
    db.close()
    last = {}
    for r in rows:
        last[r[:7]] = r
    c = {}
    for r in last.values():
        b = _band(r[7])
        if b is None:
            continue
        k = "%s|%s|%s|%d" % (_ptype(r[6]), r[3], r[4], b)
        x = c.setdefault(k, [0, 0])
        x[0] += r[8] == "hit"
        x[1] += 1
    out = {"built": time.time(), "since": SINCE, "c": c}
    try:
        with open(CACHE + ".tmp", "w") as fh:
            json.dump(out, fh)
        os.replace(CACHE + ".tmp", CACHE)
    except Exception:
        pass
    return out


def _table():
    now = time.time()
    if _MEM["c"] and now - _MEM["t"] < 600:
        return _MEM["c"]
    d = None
    try:
        with open(CACHE) as fh:
            d = json.load(fh)
        if now - d.get("built", 0) > TTL_SEC:
            d = None
    except Exception:
        d = None
    if d is None:
        d = build()
    _MEM.update(t=now, c=d)
    return d


def line(p, wp):
    """'🧾 Real record: Standard Double Faults LESS at 70-74% hit 5 of 8 (since 10/10)'. wp = 0-1 or 0-100. Never raises."""
    try:
        w = float(wp)
        w = w * 100 if w <= 1.0 else w
        b = _band(w)
        if b is None:
            return ""
        t, cat, d = _ptype(p.get("prop_type")), str(p.get("category") or ""), str(p.get("direction") or "").upper()
        hit, n = _table()["c"].get("%s|%s|%s|%d" % (t, cat, d, b), [0, 0])
        hi = dict(BANDS)[b]
        band = ("%d%%+" % b) if hi > 100 else ("%d-%d%%" % (b, hi - 1))
        what = "%s %s %s at %s" % (t.capitalize(), NAMES.get(cat, cat.replace("_", " ").title()), d, band)
        if n < MIN_GRADED:
            return "\U0001F9FE Real record: %s -- only %d graded so far, too few to judge" % (what, n)
        r = hit / float(n)
        tag = "✅" if r >= 0.75 else ("⚠️" if r < 0.6 else "")
        return ("\U0001F9FE Real record: %s hit %d of %d (since 10/10) %s" % (what, hit, n, tag)).rstrip()
    except Exception:
        return ""


if __name__ == "__main__":
    t0 = time.time(); d = build(); print("built %d groups in %.1fs" % (len(d["c"]), time.time() - t0))
    for p, w in [({"prop_type": "standard", "category": "dfs", "direction": "LESS"}, 72),
                 ({"prop_type": "goblin", "category": "fantasy_score", "direction": "MORE"}, 0.80),
                 ({"prop_type": "goblin", "category": "total_games", "direction": "MORE"}, 77),
                 ({"prop_type": "standard", "category": "breaks", "direction": "LESS"}, 72)]:
        print(line(p, w))
