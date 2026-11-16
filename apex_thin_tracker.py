#!/usr/bin/env python3
"""APEX-THINDQ-2026-11-02 (owner, "keep on track and save all their lines data and performance in their own
folder ... for later testing, but I don't want them to mess up what we already have"): every line PrizePicks posts
on a thin player (fewer than 5 matches in the stats file), with Apex's number, odds and the graded result, kept in
data/thin_players/lines.csv. Reads the picks log and graded results; writes only its own folder. Monday 9 AM #system
line with last week's record. Nothing here feeds boards, records, learning or replays."""
import csv, json, os, sys
from datetime import date, datetime, timedelta
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DIR = os.path.join(HERE, "data", "thin_players")
LINES = os.path.join(DIR, "lines.csv")
STATE = os.path.join(DIR, "state.json")
FIELDS = ["match_date", "player", "opponent", "category", "line", "prop_type", "direction", "prob", "price_source",
          "pinn_fav", "book_total", "thin", "first_seen", "last_seen", "board_status", "actual", "result"]
STAT_CATS = ("aces", "dfs", "breaks", "fantasy_score", "1st_set_aces")


def _names(r):
    if str(r.get("player") or "").lower() == "match":
        return [x.strip() for x in str(r.get("opponent") or "").split(" v ")]
    return [r.get("player"), r.get("opponent")]


def _load():
    try:
        with open(LINES, newline="") as fh:
            return {tuple(r[k] for k in FIELDS[:6]): r for r in csv.DictReader(fh)}
    except FileNotFoundError:
        return {}


def run(post=True):
    import apex_player_checks as pc, apex_edge_tracker as t
    os.makedirs(DIR, exist_ok=True)
    rows = _load()
    since = (datetime.now() - timedelta(days=3)).isoformat()
    added = 0
    with open(t.PICK_LOG, newline="", errors="ignore") as fh:
        for r in csv.DictReader(fh):
            if (r.get("ts") or "") < since:
                continue
            thin = [n for n in _names(r) if n and (pc.matches_on_file(n) or 0) < pc.THIN_MIN
                    and pc.matches_on_file(n) is not None]
            if not thin:
                continue
            k = (r.get("match_date") or "", r.get("player") or "", r.get("opponent") or "", r.get("category") or "",
                 r.get("line") or "", r.get("prop_type") or "")
            old = rows.get(k)
            row = dict(old or {}, match_date=k[0], player=k[1], opponent=k[2], category=k[3], line=k[4],
                       prop_type=k[5], direction=r.get("direction"), prob=r.get("prob_cal") or r.get("prob"),
                       price_source=r.get("price_source"), pinn_fav=r.get("pinn_fav"),
                       book_total=r.get("main_total") or "", thin="; ".join("%s %d" % (n, pc.matches_on_file(n)) for n in thin),
                       last_seen=r.get("ts"))
            row.setdefault("first_seen", r.get("ts"))
            if not row.get("first_seen"):
                row["first_seen"] = r.get("ts")
            row["board_status"] = "off boards" if (pc.THIN_ALL or k[3] in STAT_CATS) else "normal rules"
            if old is None:
                added += 1
            rows[k] = row
    graded = 0
    try:
        res = {}
        for x in t.settled_picks():
            res[(str(x.get("match_date")), str(x.get("name")), x.get("category"), str(x.get("line")),
                 str(x.get("direction")).upper())] = x
        for row in rows.values():
            if row.get("result"):
                continue
            nm = row["opponent"] if row["player"] == "match" else row["player"]
            x = res.get((row["match_date"], nm, row["category"], row["line"], str(row["direction"]).upper()))
            if x and x.get("hit") is not None:
                row["actual"] = x.get("actual")
                row["result"] = "won" if x["hit"] else "lost"
                graded += 1
    except Exception:
        pass
    with open(LINES + ".tmp", "w", newline="") as fh:
        w = csv.DictWriter(fh, FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: (r["match_date"], r["player"])))
    os.replace(LINES + ".tmp", LINES)
    msg = "%d new thin-player lines, %d graded" % (added, graded)
    if post:
        _weekly(rows)
    return msg


def _weekly(rows):
    now = datetime.now()
    if now.weekday() != 0 or now.hour < 9:
        return
    try:
        with open(STATE) as fh:
            st = json.load(fh)
    except Exception:
        st = {}
    wk = now.date().isoformat()
    if st.get("posted") == wk:
        return
    lo, hi = (now.date() - timedelta(days=7)).isoformat(), now.date().isoformat()
    week = [r for r in rows.values() if lo <= r["match_date"] < hi and r.get("result")]
    def wl(rs):
        return "%d won %d lost" % (sum(r["result"] == "won" for r in rs), sum(r["result"] == "lost" for r in rs))
    stat = [r for r in week if r["category"] in STAT_CATS]
    tg = [r for r in week if r["category"] == "total_games"]
    line = ("\U0001F9EA Thin players last week (kept off every board): %d graded lines, %s"
            " · stat props %s · Total Games %s · file data/thin_players/lines.csv"
            % (len(week), wl(week), wl(stat), wl(tg)))
    try:
        import apex_ops as ops
        ops.send_discord("system", line)
    except Exception:
        return
    st["posted"] = wk
    with open(STATE, "w") as fh:
        json.dump(st, fh)


if __name__ == "__main__":
    print(run(post="--post" in sys.argv))
