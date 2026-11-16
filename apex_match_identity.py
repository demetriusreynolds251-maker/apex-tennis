#!/usr/bin/env python3
"""APEX-MATCHID-2026-11-01 (owner, "fix everything tonight"): one match id per real match. The old id hashed
date + favorite + underdog, so a date move or a favorite flip split one match into two ids and live lines were
retired. Now the same two players within SAME_MATCH_DAYS keep the first id; a later meeting gets a new one. In the
picks log date moves are 1-2 days and rematches 5+. Old log rows are not rewritten. Undo: ON = False."""
import fcntl, json, os
from datetime import date
HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "data", "match_ids.json")
ON = True
SAME_MATCH_DAYS = 3


def _gap(a, b):
    return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)


def lookup(reg, match_date, a, b, new_id):
    """The id for this meeting; updates reg in place."""
    key = "|".join(sorted((str(a).strip().lower(), str(b).strip().lower())))
    eps = reg.setdefault(key, [])
    near = [e for e in eps if _gap(e[0], match_date) <= SAME_MATCH_DAYS]
    if near:
        e = min(near, key=lambda x: _gap(x[0], match_date))
        e[0] = str(match_date)[:10]
        return e[1]
    eps.append([str(match_date)[:10], new_id])
    return new_id


def resolve(match_date, a, b, new_id, path=None):
    """The stable id, or new_id if switched off or anything fails. Never raises."""
    if not ON:
        return new_id
    path = path or PATH
    try:
        with open(path + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                with open(path) as fh:
                    reg = json.load(fh)
            except FileNotFoundError:
                reg = {}
            mid = lookup(reg, match_date, a, b, new_id)
            with open(path + ".tmp", "w") as fh:
                json.dump(reg, fh, separators=(",", ":"))
            os.replace(path + ".tmp", path)
        return mid
    except Exception:
        return new_id
