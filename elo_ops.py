"""
elo_ops.py — real Dataset B (tennis_warehouse.sqlite) wrapper for ELO.
2026-08-07. Confirmed against the actual tennis_warehouse_schema.sql
and warehouse_ops.py this session — column names and connection
pattern below are the REAL ones, not guessed.

Real schema note: `matches` has no winner_name/loser_name columns —
it has `player_a`, `player_b`, `winner` (winner is the STRING VALUE
of whichever of player_a/player_b won, or NULL if unconfirmed). This
module does the winner/loser derivation once, here, so
elo_calculator.py can stay schema-agnostic (plain
{winner_name, loser_name, surface} dicts in).

Storage: a new `player_elo` table, since no ELO storage exists in
the current schema. Created via CREATE TABLE IF NOT EXISTS at
connection time (self-healing, same philosophy as
_ensure_moneyline_detail_columns() in dataset_a_capture.py) rather
than requiring a separate migration step the user has to run by hand.

Deliberately NOT wired into openclaw_core.py or evaluate_moneyline()
here. Per the Phase C precedent on this project, a signal doesn't
touch live evaluation reasoning until it clears a real backtest —
this module only builds and stores the ratings; signal_s6_elo() and
the actual backtest run are separate, later steps.
"""
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from elo_calculator import build_elo_history, elo_gap as _elo_gap_pure, DEFAULT_RATING, SURFACES

HERE     = Path(__file__).resolve().parent
DB_PATH  = HERE / "data" / "warehouse" / "tennis_warehouse.sqlite"
_OFFLINE = {"status": "offline", "message": "Warehouse database not accessible"}


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


def _scalar(conn, sql, params=()):
    try:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r else None
    except Exception:
        return None


def _ensure_elo_table(conn):
    """Self-healing table creation — same philosophy as
    _ensure_moneyline_detail_columns() in dataset_a_capture.py.
    One row per player. Surface columns kept flat (hard_rating,
    hard_matches, etc.) rather than a normalized child table —
    matches the flat style player_year_stats already uses for its
    own per-metric columns."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_elo (
            player           TEXT PRIMARY KEY,
            tour             TEXT CHECK (tour IN ('ATP','WTA')),
            overall_rating   REAL,
            overall_matches  INTEGER DEFAULT 0,
            hard_rating      REAL,
            hard_matches     INTEGER DEFAULT 0,
            clay_rating      REAL,
            clay_matches     INTEGER DEFAULT 0,
            grass_rating     REAL,
            grass_matches    INTEGER DEFAULT 0,
            last_updated     TEXT
        )
    """)
    conn.commit()


def load_match_rows_for_elo(conn):
    """
    Real query against the actual `matches` table. Only rows with a
    CONFIRMED winner and a real match_date are used — an unconfirmed
    winner (winner IS NULL) has nothing for ELO to update, and a
    missing match_date breaks the chronological-order requirement
    build_elo_history() depends on (it does not sort internally).

    Returns rows already sorted oldest-first, already reshaped into
    the {winner_name, loser_name, surface} dicts elo_calculator.py
    expects — the winner/loser derivation from player_a/player_b/
    winner happens here, once, rather than inside the pure engine.
    """
    raw = _rows(conn,
        "SELECT player_a, player_b, winner, surface, match_date, tour "
        "FROM matches "
        "WHERE winner IS NOT NULL AND match_date IS NOT NULL "
        "ORDER BY match_date ASC")

    out = []
    skipped_mismatched_winner = 0
    for r in raw:
        pa, pb, w = r["player_a"], r["player_b"], r["winner"]
        if w == pa:
            winner_name, loser_name = pa, pb
        elif w == pb:
            winner_name, loser_name = pb, pa
        else:
            # Real data-integrity case: winner string doesn't match
            # either player_a or player_b. Skip rather than guess
            # which side actually won.
            skipped_mismatched_winner += 1
            continue
        out.append({
            "winner_name": winner_name,
            "loser_name": loser_name,
            "surface": r["surface"],
            "tour": r["tour"],
        })
    return out, skipped_mismatched_winner


def rebuild_elo_ratings():
    """
    Real end-to-end build: read every confirmed match from Dataset B
    in chronological order, replay through the pure ELO engine, write
    final ratings into player_elo (full overwrite — ELO is a replay
    of the ENTIRE history each time, not an incremental append, since
    K-factor tuning or a data correction upstream should be able to
    change every rating consistently, not just new ones).
    """
    conn = _conn()
    if not conn:
        return _OFFLINE
    try:
        _ensure_elo_table(conn)
        rows, skipped = load_match_rows_for_elo(conn)
        if not rows:
            return {"status": "ok", "players_rated": 0, "matches_used": 0,
                    "skipped_mismatched_winner": skipped}

        ratings = build_elo_history(rows)

        # tour lookup per player: last real tour value seen for them
        # in the source rows (a player doesn't change tour, but the
        # `matches` rows don't carry a per-player tour column outside
        # this join, so derive it here rather than a second query)
        tour_of = {}
        for r in rows:
            tour_of[r["winner_name"]] = r.get("tour") or tour_of.get(r["winner_name"])
            tour_of[r["loser_name"]] = r.get("tour") or tour_of.get(r["loser_name"])

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("DELETE FROM player_elo")
        for name, pr in ratings.items():
            conn.execute(
                "INSERT INTO player_elo (player, tour, overall_rating, overall_matches, "
                "hard_rating, hard_matches, clay_rating, clay_matches, "
                "grass_rating, grass_matches, last_updated) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (name, tour_of.get(name), pr.overall, pr.overall_matches,
                 pr.surface["hard"], pr.surface_matches["hard"],
                 pr.surface["clay"], pr.surface_matches["clay"],
                 pr.surface["grass"], pr.surface_matches["grass"],
                 now)
            )
        conn.commit()
        return {"status": "ok", "players_rated": len(ratings),
                "matches_used": len(rows),
                "skipped_mismatched_winner": skipped}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def _resolve_elo_name(conn, name):
    """2026-08-14 fix (name-truncation bug): Discord cards truncate long
    names ("Masamichi Ima...", "Nicolai Budkov" for Budkov Kjaer),
    making exact `player=?` lookups fail even when a real rating
    exists. Resolution order:
      1. exact match (unchanged fast path)
      2. strip trailing truncation markers ("...", "\u2026"), retry exact
      3. prefix match (`LIKE stripped%`) — accepted ONLY if exactly
         one player matches. Two or more candidates = ambiguous =
         None, same honesty rule as everywhere else: an unambiguous
         truncation resolves, a guess between two real players never
         does.
    Returns the canonical stored name, or None."""
    if not name:
        return None
    row = conn.execute("SELECT player FROM player_elo WHERE player=?", (name,)).fetchone()
    if row:
        return row[0]
    stripped = name.strip()
    for marker in ("...", "\u2026"):
        if stripped.endswith(marker):
            stripped = stripped[: -len(marker)].strip()
    if stripped != name:
        row = conn.execute("SELECT player FROM player_elo WHERE player=?", (stripped,)).fetchone()
        if row:
            return row[0]
    if len(stripped) >= 5:  # too-short prefixes match half the tour
        rows = conn.execute(
            "SELECT player FROM player_elo WHERE player LIKE ? LIMIT 3",
            (stripped + "%",)).fetchall()
        if len(rows) == 1:
            return rows[0][0]
    # 2026-08-15 extension (middle-name gap, confirmed live on the clean
    # Djokovic re-run): "Thiago Tirante" is NOT a prefix of the stored
    # "Thiago Agustin Tirante", so prefix matching fails across an
    # inserted middle name. Token match: require the words we DO have
    # to appear in order, with anything (e.g. a middle name) allowed
    # between them. Same honesty rule: exactly one candidate or None.
    tokens = [t for t in stripped.split() if t]
    if len(tokens) >= 2 and len(stripped) >= 5:
        pattern = "%".join(tokens) + "%"
        rows = conn.execute(
            "SELECT player FROM player_elo WHERE player LIKE ? LIMIT 3",
            (pattern,)).fetchall()
        if len(rows) == 1:
            return rows[0][0]
    return None


def get_elo_gap(player_name, opponent_name, surface=None):
    """
    Real read from the STORED player_elo table (post-rebuild) —
    this is what a future signal_s6_elo() would call, mirroring
    signal_s3_h2h()'s relationship to v8_get_h2h(). Deliberately a
    thin DB read, not a recompute — rebuild_elo_ratings() owns
    recomputation, this function only reads the current snapshot.

    Returns None if either player has zero matches on file, same
    discipline as elo_calculator.elo_gap().
    """
    conn = _conn()
    if not conn:
        return None
    try:
        p_name = _resolve_elo_name(conn, player_name)
        o_name = _resolve_elo_name(conn, opponent_name)
        if not p_name or not o_name:
            return None
        p = _rows(conn, "SELECT * FROM player_elo WHERE player=?", (p_name,))
        o = _rows(conn, "SELECT * FROM player_elo WHERE player=?", (o_name,))
        if not p or not o or p[0]["overall_matches"] == 0 or o[0]["overall_matches"] == 0:
            return None
        p, o = p[0], o[0]

        result = {
            "overall_gap": p["overall_rating"] - o["overall_rating"],
            "overall_matches": (p["overall_matches"], o["overall_matches"]),
            "surface_gap": None,
            "surface_matches": None,
            "surface": None,
        }
        if surface and surface.lower() in SURFACES:
            s = surface.lower()
            p_sm = p[f"{s}_matches"] or 0
            o_sm = o[f"{s}_matches"] or 0
            if p_sm > 0 and o_sm > 0:
                result["surface_gap"] = p[f"{s}_rating"] - o[f"{s}_rating"]
                result["surface_matches"] = (p_sm, o_sm)
                result["surface"] = s
        return result
    except Exception:
        return None
    finally:
        conn.close()


def elo_coverage_stats():
    """
    Real stats for the Mission Control ELO panel — match rows used,
    players rated, coverage % (rated players / all real players ever
    seen in `matches`), split by tour. Coverage denominator is
    players who've actually appeared in a match, NOT the raw
    known_player_names.json count — a name-reference entry with zero
    real matches on file can never get a real rating, so counting it
    in the denominator would make coverage look artificially low for
    a reason that isn't fixable by rebuilding ELO.
    """
    conn = _conn()
    if not conn:
        return _OFFLINE
    try:
        total_real_players = _scalar(conn,
            "SELECT COUNT(*) FROM (SELECT player_a AS player FROM matches "
            "WHERE winner IS NOT NULL UNION SELECT player_b FROM matches "
            "WHERE winner IS NOT NULL)") or 0
        rated = _scalar(conn, "SELECT COUNT(*) FROM player_elo WHERE overall_matches > 0") or 0
        # Same definition load_match_rows_for_elo() uses (excludes
        # winner/player_a/player_b mismatches, not just NULL checks)
        # so this number always matches rebuild_elo_ratings()'s
        # "matches_used" — showing two different match counts for the
        # same dataset on the same panel would be confusing, caught
        # in testing before this went anywhere near Mission Control.
        usable_rows, _ = load_match_rows_for_elo(conn)
        matches_used = len(usable_rows)
        last_updated = _scalar(conn, "SELECT MAX(last_updated) FROM player_elo")

        by_tour = _rows(conn,
            "SELECT tour, COUNT(*) AS players, "
            "SUM(CASE WHEN hard_matches>0 THEN 1 ELSE 0 END) AS hard_covered, "
            "SUM(CASE WHEN clay_matches>0 THEN 1 ELSE 0 END) AS clay_covered, "
            "SUM(CASE WHEN grass_matches>0 THEN 1 ELSE 0 END) AS grass_covered "
            "FROM player_elo WHERE overall_matches>0 GROUP BY tour")

        coverage_pct = round(rated / total_real_players * 100, 1) if total_real_players else 0.0
        return {
            "status": "ok",
            "match_rows": matches_used,
            "players_rated": rated,
            "total_real_players": total_real_players,
            "coverage_pct": coverage_pct,
            "by_tour": by_tour,
            "last_updated": last_updated,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()
