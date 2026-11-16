#!/usr/bin/env python3
"""
APEX-OPPACES-2026-10-11 (owner, approved "go" 2026-10-11): how many aces
each player ALLOWS on return, relative to the tour average, on hard courts.

The simulator projects a server's aces from the server's own ace rate only;
the opponent's return was ignored for aces (it is already used for holds and
breaks). Tests that passed before this was built:
  - match history, rates learned on 2023-24 hard courts, tested on 2025:
    ATP aces error -7.4% (n=2,643); WTA got WORSE (-2.0%) -> ATP only
  - settled OpenClaw ATP Aces props Oct 1-10 (rates from matches before Oct 1):
    Brier 0.2116 -> 0.1988, playable picks 65.5% -> 71.1% (n=526)

factor(name) = shrunk aces-allowed rate / tour average (30 return games of
prior), from ATP hard-court matches in the current and previous calendar
year. Players with no history get 1.0. Scope: ATP, hard courts only (the
tested scope). Cache rebuilt when older than 7 days.
"""
import csv, json, os, re, unicodedata
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
HIST_DIR = os.path.join(HERE, "ATP & WTA, 1968-2026")
CACHE = os.path.join(HERE, "data", "opp_aces_allowed.json")
PRIOR_SVG = 30.0
MAX_AGE_DAYS = 7
_TABLE = None


def _nm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", s).strip()


def build():
    y = date.today().year
    files = [os.path.join(HIST_DIR, "%d.csv" % yy) for yy in (y - 1, y)] + [os.path.join(HERE, "%d.csv" % y)]
    conc, tot = {}, [0.0, 0.0]
    seen = set()
    for f in files:
        if not os.path.exists(f):
            continue
        with open(f, newline="", encoding="utf-8", errors="ignore") as fh:
            for r in csv.DictReader(fh):
                if r.get("surface") != "Hard":
                    continue
                k = (r.get("tourney_id"), r.get("match_num"), r.get("winner_name"), r.get("loser_name"))
                if k in seen:          # root 2026.csv and the history copy overlap
                    continue
                seen.add(k)
                for srv, ret in (("w", "loser_name"), ("l", "winner_name")):
                    try:
                        a = float(r[srv + "_ace"]); sv = float(r[srv + "_SvGms"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    c = conc.setdefault(_nm(r[ret]), [0.0, 0.0])
                    c[0] += a; c[1] += sv
                    tot[0] += a; tot[1] += sv
    avg = tot[0] / tot[1] if tot[1] else 0.5
    table = {n: round(((a + avg * PRIOR_SVG) / (s + PRIOR_SVG)) / avg, 4) for n, (a, s) in conc.items()}
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE + ".tmp", "w") as fh:
        json.dump({"built": datetime.now().isoformat(timespec="seconds"), "tour_avg_aces_per_svg": avg,
                   "players": table}, fh)
    os.replace(CACHE + ".tmp", CACHE)
    return table


def _load():
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    try:
        if (datetime.now().timestamp() - os.path.getmtime(CACHE)) / 86400.0 <= MAX_AGE_DAYS:
            with open(CACHE) as fh:
                _TABLE = json.load(fh)["players"]
                return _TABLE
    except Exception:
        pass
    _TABLE = build()
    return _TABLE


def factor(returner_name):
    """Multiplier for the SERVER's aces when facing this returner (1.0 if unknown)."""
    try:
        return float(_load().get(_nm(returner_name), 1.0))
    except Exception:
        return 1.0


if __name__ == "__main__":
    t = build()
    print("built %d players" % len(t))
    for n in ("Carlos Alcaraz", "Jannik Sinner", "Alex de Minaur", "Reilly Opelka"):
        print(n, factor(n))
