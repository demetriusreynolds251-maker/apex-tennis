#!/usr/bin/env python3
"""
APEX-ODDSMOVE-2026-10-15 (owner, "Go" 2026-10-15: "I need to know that
it moved"). Jodar Fantasy Score 3.5 slid 76% -> 73% because bet365 moved the
match (Jodar 57.5% -> 53.2%), and he only found out by asking.

Runs after each 30-minute auto-eval. A prop is re-scored (new pick-log row)
when its match price moves 2+ points -- by /fetch ("refresh:") or the
pre-match re-price ("reprice:"). This compares each re-scored prop's newest
row with the row before it and posts to #alerts-channel when:
  - its Win Probability moved 3+ points,
  - it is on a board or close to one (70%+ before or after... "close" = 65%+),
  - it is playable, its match hasn't started, and it isn't Breaks (off boards).
One post per match, each prop alerted once per re-score. Display only: no
board, ranking or pick rule changes.
"""
import csv, json, os
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PICK_LOG = os.path.join(HERE, "apex_engine_picks_log_v1.csv")
STATE = os.path.join(HERE, "data", "logs", "odds_move_alert_state.json")
MIN_MOVE = 0.03
NEAR_BOARD = 0.65
LOOKBACK_H = 8
LABEL = {"total_games": "Total Games", "games_won": "Games Won", "aces": "Aces", "dfs": "Double Faults",
         "fantasy_score": "Fantasy Score", "total_sets": "Total Sets", "tiebreaks": "Tiebreaks",
         "sets_won": "Sets Won", "breaks": "Breaks"}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _side_prob(row, direction):
    p = _f(row.get("prob"))
    if p is None:
        return None
    return p if row.get("direction") == direction else 1.0 - p


def moves(now=None, base=None):
    """Compare each prop's newest row with its baseline: the value last alerted
    (base[identity]) or, if never alerted, its first evaluation -- so a slow
    slide (76% -> 75% -> 73%) still alerts once it adds up to 3 points."""
    now = now or datetime.now(timezone.utc)
    base = base or {}
    hist = {}
    with open(PICK_LOG, newline="") as fh:
        for r in csv.DictReader(fh):
            k = (r.get("match_id"), r.get("player"), r.get("category"), r.get("line"), r.get("prop_type"))
            hist.setdefault(k, []).append(r)
    cutoff = (datetime.now() - timedelta(hours=LOOKBACK_H)).isoformat()
    out = []
    for k, rows in hist.items():
        if len(rows) < 2:
            continue
        ident = "|".join(str(x) for x in k)
        new = rows[-1]
        old = base.get(ident) or rows[0]
        if (new.get("ts") or "") < cutoff or new.get("category") == "breaks":
            continue
        if str(new.get("playable")) not in ("1", "True", "true"):
            continue
        try:
            if datetime.fromisoformat(new.get("slip_start_utc")) <= now:
                continue
        except Exception:
            continue
        a, b = _f(old.get("pinn_fav")), _f(new.get("pinn_fav"))
        if a is None or b is None or abs(a - b) < 0.005:
            continue          # not an odds move (surface fix, tour fix, etc.)
        d = new.get("direction")
        po, pn = _side_prob(old, d), _side_prob(new, d)
        if po is None or pn is None or abs(pn - po) < MIN_MOVE or max(po, pn) < NEAR_BOARD:
            continue
        out.append({"key": ident, "ts": new["ts"], "row": new, "old_p": po,
                    "new_p": pn, "old_fav": a, "new_fav": b})
    return out


def _fav_name(row):
    try:
        import apex_price_feed as pf
        if row.get("player") == "match":
            pa, pb = [x.strip() for x in str(row.get("opponent")).split(" v ")][:2]
        else:
            pa, pb = row.get("player"), row.get("opponent")
        r = pf.get_price(pa, pb) or {}
        return r.get("fav")
    except Exception:
        return None


DB_PATH = os.path.join(HERE, "data", "props_db.sqlite")
WTA_FIX_TS = "2026-10-15T13:05:00"


def save_moves():
    """APEX-ODDSMOVE-DB-2026-10-15 (owner: "this information should be
    saved in the database as well"). Every price-driven re-score -- each time a
    prop's newest pick-log row carries a different bet365 price than the row
    before it -- goes into props_db.sqlite table odds_moves, alerted or not.
    Idempotent (primary key = prop identity + new row ts)."""
    import sqlite3
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("""CREATE TABLE IF NOT EXISTS odds_moves (
        move_id TEXT PRIMARY KEY, ts TEXT, prev_ts TEXT, match_id TEXT, player TEXT, opponent TEXT,
        category TEXT, line TEXT, prop_type TEXT, direction TEXT, prev_direction TEXT,
        prob_prev REAL, prob_new REAL, prob_change REAL, fav_prev REAL, fav_new REAL,
        price_source TEXT, slip_start_utc TEXT, playable TEXT, alerted INTEGER DEFAULT 0)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_om_match ON odds_moves(match_id, ts)")
    hist = {}
    with open(PICK_LOG, newline="") as fh:
        for r in csv.DictReader(fh):
            k = (r.get("match_id"), r.get("player"), r.get("category"), r.get("line"), r.get("prop_type"))
            hist.setdefault(k, []).append(r)
    n = 0
    for k, rows in hist.items():
        for old, new in zip(rows, rows[1:]):
            # Before the 10/15 1:05 PM WTA fix, auto-eval scored WTA props as ATP,
            # so a WTA row from before then isn't a fair "before" -- the change
            # would be the tour fix, not the odds. Skip those pairs.
            if "women" in str(old.get("format_label") or new.get("format_label") or "").lower() \
                    and str(old.get("ts") or "") < WTA_FIX_TS:
                continue
            a, b = _f(old.get("pinn_fav")), _f(new.get("pinn_fav"))
            if a is None or b is None or abs(a - b) < 0.005:
                continue
            d = new.get("direction")
            po, pn = _side_prob(old, d), _side_prob(new, d)
            mid = "|".join(str(x) for x in k) + "|" + str(new.get("ts"))
            cur = c.execute("INSERT OR IGNORE INTO odds_moves VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)", (
                mid, new.get("ts"), old.get("ts"), k[0], k[1], new.get("opponent"), k[2], k[3], k[4], d,
                old.get("direction"), po, pn, (pn - po) if None not in (po, pn) else None, a, b,
                new.get("price_source"), new.get("slip_start_utc"), new.get("playable")))
            n += cur.rowcount
    c.commit()
    c.close()
    return n


def _mark_alerted(rows):
    import sqlite3
    c = sqlite3.connect(DB_PATH, timeout=30)
    for r in rows:
        c.execute("UPDATE odds_moves SET alerted=1 WHERE match_id=? AND player=? AND category=? AND line=? "
                  "AND prop_type=? AND ts=?", (r.get("match_id"), r.get("player"), r.get("category"),
                                               r.get("line"), r.get("prop_type"), r.get("ts")))
    c.commit()
    c.close()


def run(core):
    try:
        saved = save_moves()
    except Exception as e:
        saved = f"db save failed ({type(e).__name__}: {e})"
    try:
        with open(STATE) as fh:
            st = json.load(fh)
    except Exception:
        st = {}
    base = {k: v["row"] for k, v in (st.get("alerted") or {}).items()}
    ms = moves(base=base)
    if not ms:
        return f"odds-move alert: nothing moved ({saved} new move(s) saved)"
    by_match = {}
    for m in ms:
        by_match.setdefault(m["row"].get("match_id"), []).append(m)
    sent = 0
    for mid, L in by_match.items():
        r0 = L[0]["row"]
        match = r0["opponent"] if r0.get("player") == "match" else f"{r0['player']} v {r0['opponent']}"
        fav = _fav_name(r0) or "the favorite"
        lines = ["\U0001F4C9 **Odds moved** -- %s" % match,
                 "bet365: %s's chance to win %.1f%% → %.1f%%" % (fav, 100 * L[0]["old_fav"], 100 * L[0]["new_fav"])]
        for m in sorted(L, key=lambda x: -x["new_p"]):
            r = m["row"]
            who = "" if r.get("player") == "match" else r["player"] + " "
            q = "clears the 70% bar" if m["new_p"] >= 0.70 else "under the 70% bar"
            lines.append("• %s%s %s %s (%s): %d%% → **%d%%** (%s)" % (
                who, LABEL.get(r["category"], r["category"]), r["line"], r["direction"],
                str(r.get("prop_type") or "standard").title(), round(100 * m["old_p"]), round(100 * m["new_p"]), q))
        _in_card = False
        try:   # APEX-ALERTCHAIN-2026-10-25: a re-price card from the last 45 min takes the lines
            import apex_alert_chain as AC
            _in_card = AC.CHAINS_ON and AC.add_section("reprice", "\U0001F4C9 Odds moved — %s" % match,
                                                       [x.replace("**", "") for x in lines[1:]], max_age_min=45)
        except Exception:
            _in_card = False
        if _in_card or core.send_discord("alerts", "\n".join(lines)):
            sent += 1
            try:
                _mark_alerted([m["row"] for m in L])
            except Exception:
                pass
            for m in L:
                st.setdefault("alerted", {})[m["key"]] = {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "row": {k: m["row"].get(k) for k in ("prob", "direction", "pinn_fav", "ts")}}
    cutoff = (datetime.now() - timedelta(days=3)).isoformat()
    st["alerted"] = {k: v for k, v in (st.get("alerted") or {}).items() if v.get("at", "") >= cutoff}
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE + ".tmp", "w") as fh:
        json.dump(st, fh)
    os.replace(STATE + ".tmp", STATE)
    return f"odds-move alert: {len(ms)} prop(s) moved, {sent} post(s), {saved} new move(s) saved"
