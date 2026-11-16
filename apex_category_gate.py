#!/usr/bin/env python3
"""
APEX-CATGATE-2026-10-20 (owner, "Yes but need to automate something running in
the background to keep up with it stats", 2026-10-20): keeps a category + direction
off the Best boards (Best Goblin / Best Standard / Best Demon) while its settled
results are clearly below what Apex promised, and lets it back on once it recovers.

Every hour (scheduler, after the props DB settle) this reads the last 30 days of
settled, playable, engine-scored props from data/props_db.sqlite and, for each
category + direction, compares the hit rate with the average Honest Probability:

  BLOCK   n >= 40, hit rate 6+ points under Honest, and z <= -2 (not just bad luck)
  UNBLOCK (once blocked) hit rate back within 2 points of Honest, or z > -1

On 10/20 that blocks Breaks MORE (Honest 58.2%, hit 50.8%, n=394) and Double Faults
MORE (Honest 56.6%, hit 38.5%, n=65). Everything else passes.

The props still show on the Picks Channel, /allprops and every other board; only the
Best boards skip them. #alerts gets one line when the blocked list changes; the
boards are republished then. State: data/logs/category_gate.json.

  python3 apex_category_gate.py           # run once (writes state, alerts on change)
  python3 apex_category_gate.py --show    # print the table, change nothing
"""
import json, math, os, sqlite3, sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "props_db.sqlite")
STATE = os.path.join(HERE, "data", "logs", "category_gate.json")
WINDOW_DAYS = 30
MIN_N = 40
BLOCK_GAP, BLOCK_Z = 0.06, -2.0
UNBLOCK_GAP, UNBLOCK_Z = 0.02, -1.0
LABEL = {"aces": "Aces", "dfs": "Double Faults", "breaks": "Break Points Won", "games_won": "Games Won",
         "fantasy_score": "Fantasy Score", "total_games": "Total Games", "total_sets": "Total Sets",
         "tiebreaks": "Tiebreaks"}


def _read_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def compute(prev_blocked=()):
    sys.path.insert(0, HERE)
    import apex_edge_tracker as T
    since = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
    rows = con.execute("""SELECT category, engine_direction, engine_prob, more_won, less_won, player, opponent, line_last, match_date
                          FROM projections
                          WHERE engine_prob IS NOT NULL AND more_won IS NOT NULL AND NOT (more_won = 0 AND less_won = 0)
                          AND engine_playable = '1' AND match_date >= ?""", (since,)).fetchall()
    con.close()
    # APEX-SURFACE-EXCLUDE-2026-10-22: skip props scored on the wrong surface
    try:
        import json as _json
        _xs = {tuple(k) for k in _json.load(open(os.path.join(HERE, "apex_wrong_surface_exclusions.json"))).get("pdb_keys") or []}
    except Exception:
        _xs = set()
    g = {}
    for cat, d, p, mw, lw, pl, op, ln, md in rows:
        if _xs:
            _nm = T._norm(pl if pl != "match" else str(op or "").split(" v ")[0])
            _ln = str(ln)
            if (_nm, cat, _ln, md) in _xs or (_nm, cat, _ln.rstrip("0").rstrip(".") if "." in _ln else _ln, md) in _xs:
                continue
        hon = T.corrected_prob(p)
        if hon is None or d not in ("MORE", "LESS"):
            continue
        s = g.setdefault("%s|%s" % (cat, d), [0, 0.0, 0])
        s[0] += 1; s[1] += hon; s[2] += int(mw if d == "MORE" else lw)
    table, blocked = {}, []
    for k, (n, hs, hits) in sorted(g.items()):
        e, h = hs / n, hits / n
        z = (h - e) / math.sqrt(e * (1 - e) / n) if 0 < e < 1 else 0.0
        if k in prev_blocked:
            keep = n >= MIN_N and not (h >= e - UNBLOCK_GAP or z > UNBLOCK_Z)
        else:
            keep = n >= MIN_N and h <= e - BLOCK_GAP and z <= BLOCK_Z
        table[k] = {"n": n, "honest": round(e, 4), "hit": round(h, 4), "z": round(z, 2), "blocked": keep}
        if keep:
            blocked.append(k)
    return {"marker": "APEX-CATGATE-2026-10-20", "updated": datetime.now().isoformat(timespec="seconds"),
            "window_days": WINDOW_DAYS, "since": since, "blocked": blocked, "table": table}


def _name(k):
    cat, d = k.split("|")
    return "%s %s" % (LABEL.get(cat, cat), d)


def run(republish=True, alert=True):
    """Never raises. Returns a short status line."""
    try:
        prev = _read_state()
        out = compute(prev.get("blocked") or [])
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f, indent=1)
        os.replace(tmp, STATE)
        _cache["mtime"] = None
        old, new = set(prev.get("blocked") or []), set(out["blocked"])
        if old == new:
            return "category gate: no change (blocked: %s)" % (", ".join(_name(k) for k in sorted(new)) or "none")
        lines = []
        for k in sorted(new - old):
            t = out["table"][k]
            lines.append("⛔ %s off the Best boards: hit %.0f%% vs Honest %.0f%% (%d props, last %d days)"
                         % (_name(k), 100 * t["hit"], 100 * t["honest"], t["n"], WINDOW_DAYS))
        for k in sorted(old - new):
            t = out["table"].get(k) or {}
            lines.append("✅ %s back on the Best boards: hit %.0f%% vs Honest %.0f%% (%d props)"
                         % (_name(k), 100 * t.get("hit", 0), 100 * t.get("honest", 0), t.get("n", 0)))
        msg = "category gate: " + " | ".join(lines)
        _st = None
        if republish:
            try:
                import apex_board_status as S   # APEX-BOARDSTATUS-HEADER-2026-10-25
                _cap0 = S.latest_capture()
                import apex_edge_tracker as T
                T.republish_boards(source="category_gate")
                _st = S.from_captures(_cap0, S.latest_capture())
            except Exception:
                pass
        if alert:
            try:
                import apex_ops as ops
                import apex_board_status as S
                import apex_alert_cards as C   # APEX-ALERTCARDS-2026-10-25: a card, status first
                if C.ALERT_CARDS_ON:
                    S.post_alert(_st, "🚦 Best board category check", lines, event_color=C.ORANGE)
                else:
                    ops.send_discord("alerts", S.wrap_text(_st, "🚦 **Best board category check**\n" + "\n".join(lines)))
            except Exception:
                pass
        return msg
    except Exception as e:
        return "category gate failed (boards unchanged): %s: %s" % (type(e).__name__, e)


_cache = {"mtime": None, "blocked": frozenset()}


def is_blocked(category, direction):
    """Fast check for the board builders. Never raises; False when unsure."""
    try:
        mt = os.path.getmtime(STATE)
        if mt != _cache["mtime"]:
            _cache["blocked"] = frozenset(_read_state().get("blocked") or [])
            _cache["mtime"] = mt
        return ("%s|%s" % (category, str(direction or "").upper())) in _cache["blocked"]
    except Exception:
        return False


if __name__ == "__main__":
    if "--show" in sys.argv:
        out = compute(_read_state().get("blocked") or [])
        for k, t in out["table"].items():
            print("%-24s n=%4d Honest %5.1f%% hit %5.1f%% z %+5.1f %s" % (_name(k), t["n"], 100 * t["honest"], 100 * t["hit"], t["z"], "BLOCKED" if t["blocked"] else ""))
    else:
        print(run())
