"""APEX-HONEST-TENNIS-2026-11-07 (owner, "Go both"): tennis honest number by pick type.
How often past graded PrizePicks tennis props of the same category | Goblin/Demon/Standard | MORE/LESS | Win Prob band
really hit (data/props_db.sqlite, every graded prop with Apex's number). Needs 20+ graded at that exact key, else None
and the card keeps the old one-curve Honest Probability. Display + /lottery only: boards and rules still use
apex_edge_tracker.corrected_prob. Rebuilt from props_db every 6 hours, so new graded props count automatically.
Switch off: SHOW = False."""
import os, time, sqlite3
SHOW = True
MIN_N = 20
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "props_db.sqlite")
BANDS = [(0, 40), (40, 50), (50, 60), (60, 65), (65, 70), (70, 75), (75, 80), (80, 90), (90, 101)]
KIND = {"goblin": "Goblin", "demon": "Demon", "standard": "Standard"}
_C = {}


def band(pct):
    for lo, hi in BANDS:
        if lo <= pct < hi:
            return "90+" if lo == 90 else "%d-%d" % (lo, hi - 1)
    return "90+"


def _table():
    if _C.get("at", 0) > time.time() - 6 * 3600:
        return _C["t"]
    t = {}
    try:
        db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        rows = db.execute("select category, odds_type, engine_direction, coalesce(engine_prob_cal, engine_prob), result from projections "
                          "where result in ('more','less') and engine_prob is not null and engine_direction in ('MORE','LESS')").fetchall()
        db.close()
        for cat, kind, d, p, res in rows:
            p = float(p); kind = (kind or "standard").lower()
            for side in ("MORE", "LESS"):
                if kind in ("goblin", "demon") and side == "LESS":
                    continue
                ps = p if d == side else 1 - p
                if ps < 0.6:
                    continue
                x = t.setdefault("%s|%s|%s|%s" % (cat, kind, side, band(100 * ps)), [0, 0])
                x[0] += res == side.lower(); x[1] += 1
    except Exception:
        t = _C.get("t", {})
    _C.update(at=time.time(), t=t)
    return t


def honest(cat, kind, side, prob):
    """prob = the side's Win Probability (0-1 or 0-100). -> dict(pct, hits, n, kind) or None."""
    if not SHOW or prob in (None, ""):
        return None
    try:
        prob = float(prob)
    except (TypeError, ValueError):
        return None
    pct = prob * 100 if prob <= 1 else prob
    kind = str(kind or "standard").lower(); side = str(side or "").upper()
    if pct < 60 or side not in ("MORE", "LESS"):
        return None
    x = _table().get("%s|%s|%s|%s" % (cat, kind, side, band(pct)))
    if not x or x[1] < MIN_N:
        return None
    return dict(pct=round(100.0 * x[0] / x[1], 1), hits=x[0], n=x[1], kind=KIND.get(kind, kind.title()))


def card_line(cat, kind, side, prob):
    h = honest(cat, kind, side, prob)
    if not h:
        return None
    return "\U0001F9EE Honest Probability: %d%% _(past %s %s %s picks at this Win Prob really hit %s of %s)_" % (
        round(h["pct"]), h["kind"], str(cat).replace("_", " ").title(), str(side).upper(), format(h["hits"], ","), format(h["n"], ","))
