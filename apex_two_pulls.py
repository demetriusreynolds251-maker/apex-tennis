#!/usr/bin/env python3
"""APEX-TWOPULLS-2026-11-01 (owner, "Go all three"): a line comes off only when it is missing from two pulls
at least 10 minutes apart. Missing from the newest pull only, it stays with a warning. Lines are matched on category,
both players, line and type. Undo: ON = False. Never raises."""
import json, os
from datetime import datetime, timedelta
HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "data", "logs", "two_pulls.json")
ON = True
MIN_APART_MIN = 10
MAX_APART_MIN = 120
WARNING = "⚠ not in PrizePicks' latest list, checking again"
_CACHE = {"mtime": None, "new": None, "old": None}


def record(ts, rows):
    """Save a pull: rows are [category, player, line, type, opponent]."""
    try:
        try:
            with open(PATH) as fh:
                pulls = json.load(fh)
        except Exception:
            pulls = []
        now = datetime.fromisoformat(ts)
        keep = [p for p in pulls if timedelta(0) <= now - datetime.fromisoformat(p["ts"]) <= timedelta(minutes=MAX_APART_MIN)]
        keep.append({"ts": ts, "rows": rows})
        slim, last = [], None    # enough older pulls to always have one 10+ minutes back
        for p in reversed(keep):
            t = datetime.fromisoformat(p["ts"])
            if last is None or len(slim) < 3 or last - t >= timedelta(minutes=MIN_APART_MIN):
                slim.append(p)
                last = t
        with open(PATH + ".tmp", "w") as fh:
            json.dump(list(reversed(slim)), fh, separators=(",", ":"))
        os.replace(PATH + ".tmp", PATH)
    except Exception:
        pass


def _key(cat, a, b, line, typ):
    t = str(typ or "standard").strip().lower()
    return (cat, frozenset((str(a).strip().lower(), str(b).strip().lower())), float(line), "demon" if t == "red_demon" else t)


def _sets():
    """(keys in the newest pull, keys in the pulls back to one 10+ minutes older), or (None, None)."""
    try:
        m = os.path.getmtime(PATH)
        if _CACHE["mtime"] != m:
            with open(PATH) as fh:
                pulls = json.load(fh)
            mk = lambda p: {_key(r[0], r[1], r[4], r[2], r[3]) for r in p["rows"]}
            new = pulls[-1]
            t = datetime.fromisoformat(new["ts"])
            old = set()   # every pull back to and including the first one 10+ minutes older
            for p in reversed(pulls[:-1]):
                old |= mk(p)
                if t - datetime.fromisoformat(p["ts"]) >= timedelta(minutes=MIN_APART_MIN):
                    break
            _CACHE.update(mtime=m, new=mk(new), old=old)
        return _CACHE["new"], _CACHE["old"]
    except Exception:
        return None, None


def _pkey(p):
    pl = str(p.get("player") or "").strip()
    a, b = ([x.strip() for x in str(p.get("opponent") or "").split(" v ")] + [""])[:2] if pl.lower() == "match" else (pl, p.get("opponent"))
    return _key(p.get("category"), a, b, p.get("line"), p.get("prop_type"))


def counted(p):
    """True while this line is in the newest pull or the one 10+ minutes before it."""
    if not ON:
        return False
    try:
        new, old = _sets()
        k = _pkey(p)
        return bool((new and k in new) or (old and k in old))
    except Exception:
        return False


def missing_once(p):
    """True when the newest pull lacks this line but the pull 10+ minutes before it had it."""
    if not ON:
        return False
    try:
        new, old = _sets()
        k = _pkey(p)
        return bool(new is not None and old and k not in new and k in old)
    except Exception:
        return False


def freshness_text(p):
    """Freshness from the saved pull for props the picks log doesn't time-stamp, or None if unknown."""
    try:
        with open(PATH) as fh:
            pulls = json.load(fh)
        new, _ = _sets()
        if missing_once(p):
            return "\u26a0 Freshness: " + WARNING
        if not new or _pkey(p) not in new:
            return None
        t = datetime.fromisoformat(pulls[-1]["ts"])
        return "\U0001F7E2 Freshness: in PrizePicks' list at %s CT" % t.strftime("%-I:%M %p")
    except Exception:
        return None
