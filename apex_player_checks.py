#!/usr/bin/env python3
"""APEX-PLAYERCHECKS-2026-11-02 (owner, "Go both fixes correctly and properly"): two holds that send a prop to
BACKUPS instead of a numbered spot.
  name:  odds matched only by last name + first initial (full first and last name, middle names allowed, didn't fit)
  thin:  aces, double faults, breaks and Fantasy when a player has fewer than THIN_MIN matches in the stats file
         (Vaishnavi Adkar aces 0.5 at 91 on 11/2 came from one match). Total Games is priced from odds, so it is left out.
Every hold is logged to data/logs/player_checks.csv. Undo: NAME_HOLD = False / THIN_HOLD = False. Never raises."""
import csv, os, pickle
from datetime import date, datetime
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "apex_prop_state_v1.pkl")
LOG = os.path.join(HERE, "data", "logs", "player_checks.csv")
NAME_HOLD = True
THIN_HOLD = True
THIN_MIN = 5
THIN_DISQUALIFY = True
THIN_ALL = True   # APEX-THINALL-2026-11-02 (owner, "go thin track all"): every prop type, Total Games too   # APEX-THINDQ-2026-11-02: off every board, backups too; tracked in data/thin_players
THIN_CATS = {"aces": "player", "dfs": "player", "1st_set_aces": "player", "breaks": "both", "fantasy_score": "both"}
_C = {"mtime": None, "st": None, "n": {}}
_LOGGED = set()


def _state():
    m = os.path.getmtime(STATE)
    if _C["mtime"] != m:
        with open(STATE, "rb") as fh:
            _C.update(st=pickle.load(fh), mtime=m, n={})
    return _C["st"]


def matches_on_file(name):
    """Matches in the stats file's serve history for this player (capped at the engine window), or 0 if unknown."""
    try:
        st = _state()
        if name in _C["n"]:
            return _C["n"][name]
        import apex_prop_model_v1 as v1
        try:
            q = v1.resolve_name(name, st)
        except SystemExit:
            _C["n"][name] = 0
            return 0
        ps, _, _, _ = v1.live_features(st, q, "Hard", date.today())
        rb = v1.rate_bundle(ps, "Hard", max(v1.WINDOW_GRID), 10)
        n = int(rb["n_all"]) if rb else 0
        _C["n"][name] = n
        return n
    except Exception:
        return None   # can't tell -- don't hold on a read error


def _log(p, why):
    k = (p.get("player"), p.get("opponent"), p.get("category"), str(p.get("line")), why[:20])
    if k in _LOGGED:
        return
    _LOGGED.add(k)
    try:
        new = not os.path.exists(LOG)
        with open(LOG, "a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["logged_at", "player", "opponent", "category", "line", "reason"])
            w.writerow([datetime.now().isoformat(timespec="seconds")] + list(k[:4]) + [why])
    except Exception:
        pass


def name_reason(p):
    if not NAME_HOLD or "surname_initial" not in str(p.get("price_source") or ""):
        return None
    why = "⚠ name not confirmed: odds matched by last name + first initial only"
    _log(p, why)
    return why


def thin_reason(p):
    if not THIN_HOLD:
        return None
    side = THIN_CATS.get(p.get("category"))
    if not side or str(p.get("player") or "").lower() == "match":
        return None
    names = [p.get("player")] + ([p.get("opponent")] if side == "both" else [])
    thin = []
    for nm in names:
        n = matches_on_file(str(nm or ""))
        if n is not None and n < THIN_MIN:
            thin.append("%s %d" % (nm, n))
    if not thin:
        return None
    why = "⚠ not enough matches on file (%s)" % ", ".join(thin)
    _log(p, why)
    return why


def hold_reason(p):
    try:
        return name_reason(p) or thin_reason(p)
    except Exception:
        return None


def disqualified(p):
    """APEX-THINDQ-2026-11-02 (owner, "make them disqualified from being on boards but keep on track"):
    a thin player's aces / double faults / breaks / Fantasy prop never reaches any board. Never raises."""
    try:
        if not THIN_DISQUALIFY:
            return False
        if THIN_ALL:
            pl = str(p.get("player") or "").strip()
            names = [x.strip() for x in str(p.get("opponent") or "").split(" v ")] if pl.lower() == "match" \
                else [pl, str(p.get("opponent") or "").strip()]
            for nm in names:
                n = matches_on_file(nm) if nm else None
                if n is not None and n < THIN_MIN:
                    _log(p, "\u26a0 off boards: %s has %d matches on file" % (nm, n))
                    return True
            return False
        return bool(thin_reason(p))
    except Exception:
        return False
