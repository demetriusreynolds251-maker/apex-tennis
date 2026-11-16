"""
apex_sa365_perset_capture.py  (APEX-SA365-PERSET-2026-09-27, Phase 0/1)

Data-infrastructure layer for the SA365 field audit findings
(SA365_FIXTURES_FIELD_AUDIT_2026-09-27.md). This module ONLY captures and
stores data. It does not touch apex_score / apex_confidence / rank_score /
Best Bets / Slip Builder / any production model, and it never re-implements
the API-calling logic already vetted in apex_settle_sa365.py -- it reuses
find_event() and stat_sheet() as-is so the same rate limiting, caching to
data/sa365_sheets/*.json, and Ended-preference logic apply here too.

Storage: data/apex_sa365_stats_v1.db (new SQLite file, additive only).
  - captures        : one row per matched event (match-level metadata)
  - perset_stats     : one row per (event_id, side, set_label) with the
                       Phase-1/2/3 fields the user asked to store.

No writes happen anywhere else. This file makes no changes to any existing
production file.
"""

import json
import sqlite3
import time
import datetime as dt
from pathlib import Path

import apex_settle_sa365 as ss

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "apex_sa365_stats_v1.db"

SET_LABELS = ["1ST", "2ND", "3RD", "4TH", "5TH", "ALL"]


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = _conn()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS captures (
        event_id TEXT PRIMARY KEY,
        player1 TEXT, player2 TEXT,
        match_date TEXT, tour TEXT, surface TEXT, best_of INTEGER,
        league TEXT, status TEXT, score TEXT,
        picks_log_category TEXT,
        source TEXT,              -- 'cache' (pre-existing json) or 'api' (fetched this run)
        fetched_at TEXT,
        note TEXT
    );
    CREATE TABLE IF NOT EXISTS perset_stats (
        event_id TEXT, side INTEGER, set_label TEXT,
        games_won REAL, service_games_total REAL, return_games_total REAL,
        aces REAL, double_faults REAL,
        break_points_scored REAL,
        break_points_saved_value REAL, break_points_saved_total REAL, break_points_saved_pct REAL,
        first_serve_acc_value REAL, first_serve_acc_total REAL, first_serve_acc_pct REAL,
        second_serve_acc_value REAL, second_serve_acc_total REAL, second_serve_acc_pct REAL,
        first_serve_pts_value REAL, first_serve_pts_total REAL, first_serve_pts_pct REAL,
        second_serve_pts_value REAL, second_serve_pts_total REAL, second_serve_pts_pct REAL,
        tiebreaks REAL,
        PRIMARY KEY (event_id, side, set_label)
    );
    """)
    con.commit()
    con.close()


def _v(node, side):
    try:
        return node["player%d" % side]["value"]
    except Exception:
        return None


def _vtp(node, side):
    """-> (value, total, percentage) for a {total,value,percentage} leaf."""
    try:
        n = node["player%d" % side]
        return n.get("value"), n.get("total"), n.get("percentage")
    except Exception:
        return None, None, None


def flatten_statistics(raw):
    """raw = the JSON returned by stat_sheet() (has 'statistics' key).
    -> list of row dicts, one per (side, set_label) actually present."""
    stats = (raw or {}).get("statistics") or {}
    rows = []
    for set_label, sec in stats.items():
        if set_label not in SET_LABELS:
            continue  # unknown/unexpected key -- skip rather than guess
        for side in (1, 2):
            gw = _v((sec.get("games") or {}).get("gamesWon") or {}, side)
            sgt = _v((sec.get("service") or {}).get("serviceGamesTotal") or {}, side)
            rgt = _v((sec.get("return") or {}).get("returnGamesTotal") or {}, side)
            aces = _v((sec.get("service") or {}).get("aces") or {}, side)
            dfs = _v((sec.get("service") or {}).get("doubleFaults") or {}, side)
            bps = _v((sec.get("return") or {}).get("breakPointsScored") or {}, side)
            bpsv, bpst, bpsp = _vtp((sec.get("service") or {}).get("breakPointsSaved") or {}, side)
            f1v, f1t, f1p = _vtp((sec.get("service") or {}).get("firstServeAccuracy") or {}, side)
            f2v, f2t, f2p = _vtp((sec.get("service") or {}).get("secondServeAccuracy") or {}, side)
            fp1v, fp1t, fp1p = _vtp((sec.get("service") or {}).get("firstServePointsAccuracy") or {}, side)
            fp2v, fp2t, fp2p = _vtp((sec.get("service") or {}).get("secondServePointsAccuracy") or {}, side)
            tb = _v((sec.get("miscellaneous") or {}).get("tiebreaks") or {}, side)
            rows.append(dict(
                side=side, set_label=set_label,
                games_won=gw, service_games_total=sgt, return_games_total=rgt,
                aces=aces, double_faults=dfs, break_points_scored=bps,
                break_points_saved_value=bpsv, break_points_saved_total=bpst, break_points_saved_pct=bpsp,
                first_serve_acc_value=f1v, first_serve_acc_total=f1t, first_serve_acc_pct=f1p,
                second_serve_acc_value=f2v, second_serve_acc_total=f2t, second_serve_acc_pct=f2p,
                first_serve_pts_value=fp1v, first_serve_pts_total=fp1t, first_serve_pts_pct=fp1p,
                second_serve_pts_value=fp2v, second_serve_pts_total=fp2t, second_serve_pts_pct=fp2p,
                tiebreaks=tb,
            ))
    return rows


def capture_one(player, opponent, match_date, meta=None):
    """meta: optional dict with surface/best_of/tour/category from the picks log.
    -> (status, note, was_duplicate) where status in
       {'stored','no_event','no_statsheet','error'} and was_duplicate is True
       iff this event_id already had a captures row before this call
       (APEX-SA365-PERSET-2026-09-27 Phase 6: daily-job dedup tracking)."""
    meta = meta or {}
    ev, side, note = ss.find_event(player, opponent, match_date)
    if not ev:
        return "no_event", note, False
    eid = ev.get("id")
    con = _conn()
    was_duplicate = con.execute("SELECT 1 FROM captures WHERE event_id=?", (eid,)).fetchone() is not None
    con.close()
    sheet_file = ROOT / "data" / "sa365_sheets" / ("%s.json" % eid)
    source = "cache" if sheet_file.exists() else "api"
    res, err = ss.stat_sheet(ev)
    # APEX-SA365-PERSET-2026-09-27 Phase 6: tour was previously left blank
    # (meta.get("tour") only, and the picks log carries no tour column) --
    # the event record itself already carries tourType (atp/wta), confirmed
    # live in the field audit, so use it whenever meta doesn't override it.
    tour = meta.get("tour") or ev.get("tourType")
    statistics = (res or {}).get("statistics") or {}
    if not statistics:
        con = _conn()
        con.execute("""INSERT OR REPLACE INTO captures
            (event_id, player1, player2, match_date, tour, surface, best_of,
             league, status, score, picks_log_category, source, fetched_at, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, ev.get("participant1"), ev.get("participant2"), match_date,
             tour, meta.get("surface"), meta.get("best_of"),
             ev.get("league"), ev.get("status"), ev.get("score"),
             meta.get("category"), source, dt.datetime.now().isoformat(timespec="seconds"),
             "no stat sheet" + (" (%s)" % err if err else "")))
        con.commit(); con.close()
        return "no_statsheet", err or "empty statistics", was_duplicate
    rows = flatten_statistics(res)
    con = _conn()
    con.execute("""INSERT OR REPLACE INTO captures
        (event_id, player1, player2, match_date, tour, surface, best_of,
         league, status, score, picks_log_category, source, fetched_at, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (eid, ev.get("participant1"), ev.get("participant2"), match_date,
         tour, meta.get("surface"), meta.get("best_of"),
         ev.get("league"), ev.get("status"), ev.get("score"),
         meta.get("category"), source, dt.datetime.now().isoformat(timespec="seconds"), ""))
    for r in rows:
        con.execute("""INSERT OR REPLACE INTO perset_stats
            (event_id, side, set_label, games_won, service_games_total, return_games_total,
             aces, double_faults, break_points_scored,
             break_points_saved_value, break_points_saved_total, break_points_saved_pct,
             first_serve_acc_value, first_serve_acc_total, first_serve_acc_pct,
             second_serve_acc_value, second_serve_acc_total, second_serve_acc_pct,
             first_serve_pts_value, first_serve_pts_total, first_serve_pts_pct,
             second_serve_pts_value, second_serve_pts_total, second_serve_pts_pct,
             tiebreaks)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, r["side"], r["set_label"], r["games_won"], r["service_games_total"], r["return_games_total"],
             r["aces"], r["double_faults"], r["break_points_scored"],
             r["break_points_saved_value"], r["break_points_saved_total"], r["break_points_saved_pct"],
             r["first_serve_acc_value"], r["first_serve_acc_total"], r["first_serve_acc_pct"],
             r["second_serve_acc_value"], r["second_serve_acc_total"], r["second_serve_acc_pct"],
             r["first_serve_pts_value"], r["first_serve_pts_total"], r["first_serve_pts_pct"],
             r["second_serve_pts_value"], r["second_serve_pts_total"], r["second_serve_pts_pct"],
             r["tiebreaks"]))
    con.commit(); con.close()
    return "stored", "%d rows (%s)" % (len(rows), source), was_duplicate


if __name__ == "__main__":
    init_db()
    print("DB initialized at", DB_PATH)
