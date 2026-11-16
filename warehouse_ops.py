"""
warehouse_ops.py — Dataset B (Tennis Data Warehouse) operations layer
Real implementation backed by data/warehouse/tennis_warehouse.sqlite.
Replaces the stub (2026-07-24) now that the collector has been running
daily and has real data: 1,812 matches, 199 players, 16 runs.

All functions return dicts safe to JSON-serialize. Each function is
fail-open — if the database is inaccessible for any reason, it returns
a safe {"status": "offline"} dict rather than raising.
"""
from pathlib import Path
import sqlite3, json
from datetime import datetime, timezone

HERE     = Path(__file__).resolve().parent
DB_PATH  = HERE / "data" / "warehouse" / "tennis_warehouse.sqlite"
_OFFLINE = {"status": "offline", "message": "Warehouse database not accessible"}


# ── connection helper ────────────────────────────────────────────────
def _conn():
    if not DB_PATH.exists():
        return None
    try:
        c = sqlite3.connect(str(DB_PATH), timeout=5)
        c.row_factory = sqlite3.Row
        return c
    except Exception:
        return None


def _rows(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def _one(conn, sql, params=()):
    try:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def _scalar(conn, sql, params=()):
    try:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r else None
    except Exception:
        return None


# ── core functions called by /api/warehouse_cc ───────────────────────

def warehouse_snapshot():
    conn = _conn()
    if not conn:
        return _OFFLINE
    try:
        counts = {}
        for tbl in ["matches", "players", "player_year_stats",
                    "collector_runs", "player_seed_list"]:
            counts[tbl] = _scalar(conn, f"SELECT COUNT(*) FROM {tbl}") or 0

        last_run = _one(conn,
            "SELECT started_at, status, rows_added, rows_updated, duration_sec "
            "FROM collector_runs ORDER BY started_at DESC LIMIT 1")

        hb = _one(conn,
            "SELECT status, current_task, last_heartbeat, "
            "success_count, failure_count FROM collector_heartbeat WHERE id=1")

        size_mb = round(DB_PATH.stat().st_size / (1024 * 1024), 2)

        return {
            "status":    "ok",
            "counts":    counts,
            "size_mb":   size_mb,
            "last_run":  last_run,
            "heartbeat": hb,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def readiness_card():
    conn = _conn()
    if not conn:
        return _OFFLINE
    try:
        runs_total   = _scalar(conn, "SELECT COUNT(*) FROM collector_runs") or 0
        runs_success = _scalar(conn, "SELECT COUNT(*) FROM collector_runs WHERE status='SUCCESS'") or 0
        runs_partial = _scalar(conn, "SELECT COUNT(*) FROM collector_runs WHERE status='PARTIAL'") or 0
        runs_failed  = _scalar(conn, "SELECT COUNT(*) FROM collector_runs WHERE status='FAILED'") or 0

        players_with_stats = _scalar(conn,
            "SELECT COUNT(DISTINCT player) FROM player_year_stats") or 0

        surfaces = _rows(conn,
            "SELECT surface, COUNT(*) as n FROM matches "
            "WHERE surface IS NOT NULL GROUP BY surface ORDER BY n DESC")

        date_range = _one(conn,
            "SELECT MIN(match_date) as first, MAX(match_date) as last FROM matches")

        return {
            "status":             "ok",
            "runs_total":         runs_total,
            "runs_success":       runs_success,
            "runs_partial":       runs_partial,
            "runs_failed":        runs_failed,
            "players_with_stats": players_with_stats,
            "surfaces":           surfaces,
            "match_date_min":     date_range["first"] if date_range else None,
            "match_date_max":     date_range["last"]  if date_range else None,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def collection_status():
    conn = _conn()
    if not conn:
        return _OFFLINE
    try:
        runs = _rows(conn,
            "SELECT run_id, started_at, finished_at, status, "
            "rows_added, rows_updated, rows_failed, duration_sec "
            "FROM collector_runs ORDER BY started_at DESC LIMIT 10")

        hb = _one(conn,
            "SELECT status, current_task, last_heartbeat, "
            "success_count, failure_count FROM collector_heartbeat WHERE id=1")

        return {"status": "ok", "recent_runs": runs, "heartbeat": hb}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def data_source_quality():
    conn = _conn()
    if not conn:
        return _OFFLINE
    try:
        agg = _rows(conn,
            "SELECT data_source, COUNT(*) as n FROM player_year_stats GROUP BY data_source")

        total   = _scalar(conn, "SELECT COUNT(DISTINCT player) FROM player_year_stats") or 0
        full    = _scalar(conn,
            "SELECT COUNT(DISTINCT player) FROM player_year_stats "
            "WHERE hold_pct IS NOT NULL AND break_pct IS NOT NULL "
            "AND bp_save_pct IS NOT NULL") or 0

        return {
            "status":            "ok",
            "source_breakdown":  agg,
            "total_players":     total,
            "full_stats_players": full,
            "partial_players":   total - full,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def feature_health():
    conn = _conn()
    if not conn:
        return _OFFLINE
    try:
        # Try stored feature_coverage first
        rows = _rows(conn,
            "SELECT feature_name, population_pct, missing_pct, "
            "players_covered, ready_for_ml, quality_score, snapshot_date "
            "FROM feature_coverage ORDER BY snapshot_date DESC, feature_name")

        if rows:
            return {"status": "ok", "features": rows, "computed": False}

        # Compute on the fly from player_year_stats
        total = _scalar(conn, "SELECT COUNT(DISTINCT player) FROM player_year_stats") or 1
        features_cols = [
            ("hold_pct",         "hold_pct"),
            ("break_pct",        "break_pct"),
            ("avg_aces",         "avg_aces"),
            ("first_serve_pct",  "first_serve_pct"),
            ("bp_save_pct",      "bp_save_pct"),
        ]
        computed = []
        for fname, col in features_cols:
            covered = _scalar(conn,
                f"SELECT COUNT(DISTINCT player) FROM player_year_stats "
                f"WHERE {col} IS NOT NULL AND {col} > 0") or 0
            pct = round(covered / total * 100, 1)
            computed.append({
                "feature_name":   fname,
                "population_pct": pct,
                "missing_pct":    round(100 - pct, 1),
                "players_covered": covered,
                "ready_for_ml":   1 if pct >= 80 else 0,
            })
        return {"status": "ok", "features": computed, "computed": True}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def data_quality_center():
    conn = _conn()
    if not conn:
        return _OFFLINE
    try:
        total   = _scalar(conn, "SELECT COUNT(*) FROM matches") or 0
        missing_score   = _scalar(conn, "SELECT COUNT(*) FROM matches WHERE score IS NULL") or 0
        missing_winner  = _scalar(conn, "SELECT COUNT(*) FROM matches WHERE winner IS NULL") or 0
        missing_surface = _scalar(conn, "SELECT COUNT(*) FROM matches WHERE surface IS NULL") or 0

        return {
            "status":           "ok",
            "total_matches":    total,
            "missing_score":    missing_score,
            "missing_winner":   missing_winner,
            "missing_surface":  missing_surface,
            "completeness_pct": round((1 - missing_score / total) * 100, 1) if total > 0 else 0,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def year_coverage():
    conn = _conn()
    if not conn:
        return _OFFLINE
    try:
        rows = _rows(conn,
            "SELECT SUBSTR(match_date,1,4) as year, COUNT(*) as matches "
            "FROM matches WHERE match_date IS NOT NULL GROUP BY year ORDER BY year")
        return {"status": "ok", "years": rows}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def match_coverage():
    conn = _conn()
    if not conn:
        return _OFFLINE
    try:
        by_surface = _rows(conn,
            "SELECT COALESCE(surface,'unknown') as surface, COUNT(*) as n "
            "FROM matches GROUP BY surface ORDER BY n DESC")
        by_tour = _rows(conn,
            "SELECT COALESCE(tour,'unknown') as tour, COUNT(*) as n "
            "FROM matches GROUP BY tour")
        top_players = _rows(conn,
            "SELECT player, COUNT(*) as n FROM ("
            "  SELECT player_a as player FROM matches UNION ALL "
            "  SELECT player_b FROM matches"
            ") GROUP BY player ORDER BY n DESC LIMIT 20")
        return {
            "status":     "ok",
            "by_surface": by_surface,
            "by_tour":    by_tour,
            "top_players": top_players,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def collection_trends():
    conn = _conn()
    if not conn:
        return _OFFLINE
    try:
        daily = _rows(conn,
            "SELECT SUBSTR(started_at,1,10) as date, "
            "SUM(rows_added) as added, SUM(rows_updated) as updated, "
            "MAX(status) as status "
            "FROM collector_runs GROUP BY date ORDER BY date DESC LIMIT 14")
        return {"status": "ok", "daily": daily}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def warehouse_health_score():
    """0-100 score summarising warehouse health for the header tile."""
    conn = _conn()
    if not conn:
        return 0
    try:
        total    = _scalar(conn, "SELECT COUNT(*) FROM matches") or 0
        runs     = _scalar(conn, "SELECT COUNT(*) FROM collector_runs") or 0
        last_ok  = _scalar(conn,
            "SELECT COUNT(*) FROM collector_runs WHERE status IN ('SUCCESS','PARTIAL') "
            "AND started_at > datetime('now','-2 days')") or 0
        players  = _scalar(conn, "SELECT COUNT(*) FROM players") or 0

        score = 0
        score += min(40, int(total / 1812 * 40))   # up to 40pts for match coverage
        score += min(20, int(players / 199 * 20))   # up to 20pts for player coverage
        score += 30 if last_ok > 0 else 0            # 30pts if collected in last 48h
        score += 10 if runs >= 10 else int(runs)     # 10pts for run history depth
        return min(100, score)
    except Exception:
        return 0
    finally:
        conn.close()


# ── run history + detail ─────────────────────────────────────────────

def run_history():
    conn = _conn()
    if not conn:
        return []
    try:
        return _rows(conn,
            "SELECT run_id, started_at, finished_at, status, "
            "rows_added, rows_updated, rows_failed, duration_sec, "
            "api_errors, warnings, warehouse_size_after "
            "FROM collector_runs ORDER BY started_at DESC LIMIT 20")
    except Exception:
        return []
    finally:
        conn.close()


def run_detail(run_id):
    conn = _conn()
    if not conn:
        return {}
    try:
        row = _one(conn,
            "SELECT * FROM collector_runs WHERE run_id=?", (run_id,))
        if not row:
            return {"error": "run not found"}
        # Parse JSON fields
        for f in ("api_errors", "warnings"):
            if row.get(f):
                try:
                    row[f] = json.loads(row[f])
                except Exception:
                    pass
        return row
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


# ── player coverage ──────────────────────────────────────────────────

def player_coverage():
    conn = _conn()
    if not conn:
        return []
    try:
        return _rows(conn,
            "SELECT p.player, p.tour, p.current_ranking, "
            "p.last_seen_at, "
            "COUNT(DISTINCT pys.year) as years_on_file, "
            "COALESCE(m.match_count, 0) as match_count "
            "FROM players p "
            "LEFT JOIN player_year_stats pys ON pys.player=p.player "
            "LEFT JOIN ("
            "  SELECT player_a as player, COUNT(*) as match_count FROM matches GROUP BY player_a "
            "  UNION ALL "
            "  SELECT player_b, COUNT(*) FROM matches GROUP BY player_b"
            ") m ON m.player=p.player "
            "GROUP BY p.player ORDER BY p.current_ranking ASC NULLS LAST LIMIT 50")
    except Exception:
        return []
    finally:
        conn.close()


def player_detail(name):
    conn = _conn()
    if not conn:
        return {}
    try:
        player_row = _one(conn,
            "SELECT * FROM players WHERE player=?", (name,))
        years = _rows(conn,
            "SELECT year, hold_pct, break_pct, avg_aces, "
            "first_serve_pct, bp_save_pct, matches_count, data_source "
            "FROM player_year_stats WHERE player=? ORDER BY year DESC", (name,))
        match_count = _scalar(conn,
            "SELECT COUNT(*) FROM matches "
            "WHERE player_a=? OR player_b=?", (name, name)) or 0

        # v7.9.8: widened to surface the fields added in the 2026-07-27
        # capture-gap fix (unforced errors, winners, break points,
        # serve-win splits, h2h, tournament tier/id, game splits,
        # historical odds). player_a/player_b pulled in so the raw
        # per-side columns can be normalized below into a single
        # this-player-vs-opponent perspective -- the frontend renders
        # one shape regardless of which side `name` was on, rather than
        # re-deriving that on every render.
        raw_recent = _rows(conn,
            "SELECT match_date, player_a, player_b, surface, tournament, "
            "tournament_id, winner, score, set_count, h2h_at_match, "
            "total_games, player_a_games, player_b_games, "
            "player_a_unforced_errors, player_a_winners, "
            "player_a_break_points_converted, player_a_break_points_converted_of, "
            "player_a_first_serve_in, player_a_first_serve_of, "
            "player_a_fastest_serve, player_a_odd, "
            "player_b_unforced_errors, player_b_winners, "
            "player_b_break_points_converted, player_b_break_points_converted_of, "
            "player_b_first_serve_in, player_b_first_serve_of, "
            "player_b_fastest_serve, player_b_odd "
            "FROM matches WHERE player_a=? OR player_b=? "
            "ORDER BY match_date DESC LIMIT 10", (name, name))

        recent = []
        for r in raw_recent:
            is_a = (r["player_a"] == name)
            opponent = r["player_b"] if is_a else r["player_a"]
            won = (r["winner"] == name) if r["winner"] else None
            prefix, opp_prefix = ("player_a", "player_b") if is_a else ("player_b", "player_a")
            this_games = r["player_a_games"] if is_a else r["player_b_games"]
            opp_games  = r["player_b_games"] if is_a else r["player_a_games"]
            recent.append({
                "match_date":      r["match_date"],
                "opponent":        opponent,
                "won":             won,
                "score":           r["score"],
                "surface":         r["surface"],
                "tournament":      r["tournament"],
                "tournament_id":   r["tournament_id"],
                "set_count":       r["set_count"],
                "h2h_at_match":    r["h2h_at_match"],
                "total_games":     r["total_games"],
                "this_games":      this_games,
                "opponent_games":  opp_games,
                "this_unforced_errors":  r[f"{prefix}_unforced_errors"],
                "this_winners":          r[f"{prefix}_winners"],
                "this_bp_converted":     r[f"{prefix}_break_points_converted"],
                "this_bp_converted_of":  r[f"{prefix}_break_points_converted_of"],
                "this_first_serve_in":   r[f"{prefix}_first_serve_in"],
                "this_first_serve_of":   r[f"{prefix}_first_serve_of"],
                "this_fastest_serve":    r[f"{prefix}_fastest_serve"],
                "this_odd":              r[f"{prefix}_odd"],
                "opponent_unforced_errors": r[f"{opp_prefix}_unforced_errors"],
                "opponent_winners":         r[f"{opp_prefix}_winners"],
                "opponent_odd":             r[f"{opp_prefix}_odd"],
            })

        return {
            "player":      name,
            "info":        player_row or {},
            "years":       years,
            "match_count": match_count,
            "recent":      recent,
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()
