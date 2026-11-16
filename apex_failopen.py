#!/usr/bin/env python3
"""APEX-FAILLOG-2026-11-01 (owner, "Fix all"): counts errors in the checks that decide picks, where an
error used to mean the pick passed with no trace. Counts only; never changes a pick. Never raises."""
import json, os, time
from collections import Counter
from datetime import date
HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "data", "logs", "failopen_counts.jsonl")
_seen = {}


def note(name, err=None):
    """One line per check, per error type, per process every 10 minutes."""
    try:
        k = (name, type(err).__name__)
        if time.time() - _seen.get(k, 0) < 600:
            return
        _seen[k] = time.time()
        with open(PATH, "a") as fh:
            fh.write(json.dumps({"d": date.today().isoformat(), "check": name, "err": k[1], "msg": str(err)[:120]}) + "\n")
    except Exception:
        pass


def status_line(day=None):
    try:
        day = day or date.today().isoformat()
        c = Counter()
        if os.path.exists(PATH):
            for l in open(PATH):
                r = json.loads(l)
                if r.get("d") == day:
                    c[r["check"]] += 1
        if not c:
            return "✅ Pick checks: no errors today"
        return "⚠️ Pick checks had errors today: " + ", ".join("%s %d" % x for x in c.most_common(6))
    except Exception:
        return None
