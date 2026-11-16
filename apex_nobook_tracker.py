#!/usr/bin/env python3
"""
APEX-NOBOOK-2026-10-24 (owner, "Yes let's add a flag before the name and take them off the
boards only as a backup with a clear warning by the name ⚠️⚠️"): separate tracker for the
NO_BOOK_TOTAL_TO_BACKUPS rule (apex_edge_tracker). Every publish logs each Total Games pick the rule
held off a board -- board, the slot it had won, line -- and the pick that took that slot. The daily
status grades both sides:

  📊 NO-BOOK-TOTAL HOLDS (since 10/24): held X picks, they went W-L; the picks that took their slots went W-L

TEST FINISHED card (once) at FINISH_GRADED graded held picks or on FINISH_DATE.
Each pick counts once (the last publish before its match started), however many boards held it.

  python3 apex_nobook_tracker.py          # running record
  python3 apex_nobook_tracker.py --rows   # every held pick with its result
"""
import csv, os, sqlite3, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "data", "logs", "nobook_holds.csv")
WAREHOUSE = os.path.join(HERE, "data", "warehouse", "tennis_warehouse.sqlite")
START = "2026-10-24"
FINISH_GRADED = 30
FINISH_DATE = "2026-11-20"
TEST_ON = True
FIELDS = ["captured_at", "source", "match_date", "board", "slot", "player", "opponent", "category", "line",
          "direction", "prop_type", "prob", "start_utc",
          "took_player", "took_opponent", "took_category", "took_line", "took_direction", "took_prop_type"]


def _append(rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def _row(t, captured_at, source, date_str, board, slot, p, took):
    took = took or {}
    return {"captured_at": captured_at, "source": source, "match_date": str(p.get("match_date") or date_str)[:10],
            "board": board, "slot": slot or "", "player": p.get("player"), "opponent": p.get("opponent"),
            "category": p.get("category"), "line": p.get("line"), "direction": str(p.get("direction") or "").upper(),
            "prop_type": str(p.get("prop_type") or "").lower(), "prob": "%.4f" % t._board_prob(p),
            "start_utc": t._prop_start_utc(p) if hasattr(t, "_prop_start_utc") else "",
            "took_player": took.get("player", ""), "took_opponent": took.get("opponent", ""),
            "took_category": took.get("category", ""), "took_line": took.get("line", ""),
            "took_direction": str(took.get("direction") or "").upper(),
            "took_prop_type": str(took.get("prop_type") or "").lower()}


def log_publish(results, date_str, source="unknown"):
    """Called from apex_edge_tracker.write_board_status on every PrizePicks publish. Never raises."""
    try:
        import apex_edge_tracker as t
        if not TEST_ON or not t.NO_BOOK_TOTAL_TO_BACKUPS:
            return
        at = datetime.now().isoformat(timespec="seconds")
        rows = []
        for flag, res in (results or {}).items():
            if not isinstance(res, dict):
                continue
            scored = res.get("scored") or []
            for h in res.get("held_back") or []:
                if h.get("reason") != t.NO_BOOK_WHY:
                    continue
                slot = h.get("rank")
                took = (scored[slot - 1].get("p") if isinstance(slot, int) and 0 < slot <= len(scored) else None)
                rows.append(_row(t, at, source, date_str, flag, slot, h.get("p") or {}, took))
        _append(rows)
    except Exception as e:
        print("[NoBookTracker] log failed (%s): %s" % (type(e).__name__, e))


def log_pp(b):
    """Called from the ParlayPlay build with its board (b.held from _low_cushion_split). Never raises."""
    try:
        import apex_edge_tracker as t
        if not TEST_ON or not t.NO_BOOK_TOTAL_TO_BACKUPS:
            return
        at = datetime.now().isoformat(timespec="seconds")
        rows = []
        for h in getattr(b, "held", None) or []:
            if h.get("reason") != t.NO_BOOK_WHY:
                continue
            slot = h.get("rank")
            took = (b[slot - 1].get("p") if isinstance(slot, int) and 0 < slot <= len(b) else None)
            rows.append(_row(t, at, "parlayplay", "", "parlayplay", slot, h.get("p") or {}, took))
        _append(rows)
    except Exception as e:
        print("[NoBookTracker] pp log failed (%s): %s" % (type(e).__name__, e))


def _read():
    if not os.path.exists(LOG):
        return []
    with open(LOG, newline="") as fh:
        return [r for r in csv.DictReader(fh) if r["match_date"] >= START]


def _official(rows):
    """One row per (board, pick): the last publish before the match started (else the first one)."""
    best = {}
    for r in sorted(rows, key=lambda r: r["captured_at"]):
        k = (r["board"], r["match_date"], r["player"], r["opponent"], r["category"], r["line"], r["direction"])
        before = not r["start_utc"] or r["captured_at"] < _ct_naive(r["start_utc"])
        if k not in best or before:
            best[k] = r
    return list(best.values())


def _ct_naive(start_utc):
    """start_utc (ISO, UTC) as a naive Central-time ISO string, to compare with captured_at."""
    try:
        from zoneinfo import ZoneInfo
        d = datetime.fromisoformat(str(start_utc).replace("Z", "+00:00"))
        return d.astimezone(ZoneInfo("America/Chicago")).replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception:
        return "9999"


def _tg_result(md, player, opponent):
    """Total games of the match from the warehouse, or None."""
    try:
        import apex_card_signals as cs
        names = [cs._norm(x) for x in (str(opponent).split(" v ") if player == "match" else [player, opponent])]
        if len(names) != 2:
            return None
        con = sqlite3.connect("file:%s?mode=ro" % WAREHOUSE, uri=True)
        r = con.execute("select total_games, score, retired from matches where match_date between date(?, '-1 day') "
                        "and date(?, '+1 day') and ((player_a = ? and player_b = ?) or (player_a = ? and player_b = ?)) "
                        "order by match_date limit 1", (md, md, names[0], names[1], names[1], names[0])).fetchone()
        con.close()
        if not r or r[2]:
            return None
        if r[0]:
            return int(r[0])
        import apex_blowout_signal as bs
        return bs._games_from_score(r[1])
    except Exception:
        return None


def _grader():
    import apex_engine_validation as ev
    _t, _rep, S, _st = ev.load()
    memo = {}

    def hit(md, player, opponent, cat, line, direction):
        if not player or not cat:
            return None
        k = (md, player, opponent, cat, str(line), direction)
        if k not in memo:
            m = S.find(md, player, opponent, cat, line, direction)
            h = None if m is None else m.get("hit")
            if h is None and cat == "total_games":
                tg = _tg_result(md, player, opponent)
                if tg is not None:
                    h = (tg > float(line)) if direction == "MORE" else (tg < float(line))
            memo[k] = h
        return memo[k]
    return hit


def tally():
    """(held W, held L, took W, took L, graded rows) -- each pick counted once across boards."""
    hit = _grader()
    rows = _official(_read())
    held, took, out = {}, {}, []
    for r in rows:
        hk = (r["match_date"], r["player"], r["opponent"], r["category"], r["line"], r["direction"])
        h = hit(*hk)
        held.setdefault(hk, h)
        tk = (r["match_date"], r["took_player"], r["took_opponent"], r["took_category"], r["took_line"], r["took_direction"])
        th = hit(*tk) if r["took_player"] else None
        if r["took_player"]:
            took.setdefault(tk, th)
        out.append((r, h, th))
    hw = sum(1 for v in held.values() if v is True)
    hl = sum(1 for v in held.values() if v is False)
    tw = sum(1 for v in took.values() if v is True)
    tl = sum(1 for v in took.values() if v is False)
    return hw, hl, tw, tl, len(held), out


def summary():
    """Daily status line (apex_alerts). Never raises."""
    if not TEST_ON:
        return []
    try:
        hw, hl, tw, tl, n, _ = tally()
        return ["\U0001F4CA NO-BOOK-TOTAL HOLDS (since 10/24): held %d pick%s, they went %d-%d; "
                "the picks that took their slots went %d-%d" % (n, "" if n == 1 else "s", hw, hl, tw, tl)]
    except Exception as e:
        return ["\U0001F4CA NO-BOOK-TOTAL HOLDS: unavailable (%s)" % type(e).__name__]


def finish_check():
    if not TEST_ON:
        return False, None
    hw, hl, tw, tl, n, _ = tally()
    if hw + hl < FINISH_GRADED and datetime.now().date().isoformat() < FINISH_DATE:
        return False, None
    hr = (100.0 * hw / (hw + hl)) if hw + hl else 0
    tr = (100.0 * tw / (tw + tl)) if tw + tl else 0
    verdict = ("KEEP -- the held picks hit %.0f%%, the picks that took their slots hit %.0f%%" % (hr, tr) if tr >= hr else
               "THE HOLD COST US -- the held picks hit %.0f%%, the picks that took their slots only %.0f%%" % (hr, tr))
    rows = [("Test", "Total Games picks with no book total sent to BACKUPS (since 10/24, %d picks held)" % n),
            ("Held picks", "%d-%d" % (hw, hl)),
            ("Picks that took their slots", "%d-%d" % (tw, tl)),
            ("Verdict", verdict),
            ("Decision", "keep the hold / turn it off (apex_edge_tracker.NO_BOOK_TOTAL_TO_BACKUPS = False) / keep testing")]
    return True, rows


if __name__ == "__main__":
    print("\n".join(summary()))
    if "--rows" in sys.argv:
        for r, h, th in tally()[5]:
            f = {True: "W", False: "L", None: "?"}
            print(r["match_date"], r["board"], "#%s" % r["slot"], r["opponent"] if r["player"] == "match" else r["player"],
                  r["category"], r["line"], r["direction"], f[h], "| took:", r["took_player"], r["took_category"],
                  r["took_line"], f[th])
