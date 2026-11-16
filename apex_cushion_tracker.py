#!/usr/bin/env python3
"""APEX-CUSHIONFILL-2026-11-01: what the cushion fill rule kept off the boards, and how those picks did.
Logs each favorite-75+ Total Games pick and each low-room pick kept out of a freed spot, once per board,
day and pick. Graded from props_db. Read-only on every board; nothing here changes a pick."""
import csv, os, sqlite3
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "data", "logs", "cushion_fill_log.csv")
DB = os.path.join(HERE, "data", "props_db.sqlite")
FIELDS = ["day", "board", "kind", "player", "opponent", "line", "direction", "prop_type", "match_date"]


def _seen():
    try:
        return {(r["day"], r["board"], r["kind"], r["opponent"], r["line"]) for r in csv.DictReader(open(LOG, newline=""))}
    except Exception:
        return set()


def log_build(board, removed, blocked):
    day = date.today().isoformat()
    seen = _seen()
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for kind, ps in (("favorite", removed), ("low_room", blocked)):
            for p in ps:
                k = (day, board, kind, str(p.get("opponent")), str(p.get("line")))
                if k in seen:
                    continue
                seen.add(k)
                w.writerow({"day": day, "board": board, "kind": kind, "player": p.get("player"), "opponent": p.get("opponent"),
                            "line": p.get("line"), "direction": p.get("direction"), "prop_type": p.get("prop_type"),
                            "match_date": p.get("match_date")})


def record():
    """{kind: (would have won, would have lost)} over distinct picks."""
    out = {"favorite": [0, 0], "low_room": [0, 0]}
    try:
        rows = list(csv.DictReader(open(LOG, newline="")))
        c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        done = set()
        for r in rows:
            k = (r["kind"], r["opponent"], r["line"], r["match_date"])
            if k in done:
                continue
            done.add(k)
            a = r["opponent"].split(" v ")
            if len(a) != 2:
                continue
            q = c.execute("select more_won,less_won from projections where category='total_games' and line_last=? "
                          "and ((player like ? and opponent like ?) or (player like ? and opponent like ?)) and start_utc>=? "
                          "and (more_won+less_won)>0 limit 1",
                          (r["line"], a[0] + "%", a[1] + "%", a[1] + "%", a[0] + "%", r["day"])).fetchone()
            if q:
                out[r["kind"]][0 if q[0] else 1] += 1
        c.close()
    except Exception:
        pass
    return out


def status_line():
    r = record()
    return ("\U0001F6E1 Cushion fill since 11/1, picks kept off: favorites 75%%+ would have won %d, lost %d | "
            "low-room fills would have won %d, lost %d" % (r["favorite"][0], r["favorite"][1], r["low_room"][0], r["low_room"][1]))


if __name__ == "__main__":
    print(status_line())
