"""
dataset_a_query.py -- read-only query layer for Dataset A (dataset_a.sqlite).

Deliberately a SEPARATE file from dataset_a_capture.py: that module's own
design contract (see its header, rule #2) is explicit -- "NEVER reads
pipeline state to make a decision. Write-only tap." This module is the
read half, kept apart on purpose so that contract stays true rather than
quietly violated for convenience. Mirrors the same read-only pattern
openclaw_ops.py already uses (mode=ro connection, fail-open to empty/None).

Real column names below confirmed against actual usage in openclaw_ops.py
(pick_from_eval(), the classification_dist query) -- not guessed.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "warehouse" / "dataset_a.sqlite"


def _connect_ro():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def find_matching_prop_classification(player, opponent, tournament, max_age_days=3):
    """
    Look for an already-completed PROP evaluation of the same real match,
    so moneyline's Phase 1 can reuse its classification (LOPSIDED/
    COMPETITIVE) instead of re-running the full 11-step process from
    scratch.

    Match-identity safeguard (the real gap flagged 2026-08-03): matching
    requires BOTH players (order-insensitive) AND the same tournament --
    never player names alone. Two players can meet more than once across
    different events; a same-named-but-different real match must never be
    silently reused. max_age_days additionally guards against reusing a
    stale evaluation from an earlier round at the same event (a Masters
    1000 can span two weeks under one tournament string) -- default 3
    days is intentionally tight, since this should only reuse a prop eval
    from THIS specific match, not an earlier meeting at the same event.

    Returns dict {eval_id, classification, reasoning_text, created_at} or
    None if no safe match is found. Never raises -- a missing DB, a
    locked file, or any query failure degrades to None (no reuse, caller
    falls back to a fresh 11-step run), never blocks or crashes the
    caller.
    """
    if not DB_PATH.exists():
        return None
    try:
        conn = _connect_ro()
        rows = conn.execute("""
            SELECT eval_id, classification, reasoning_text, created_at
            FROM evaluations
            WHERE evaluation_type = 'PROP'
              AND classification IS NOT NULL
              AND lifecycle_state NOT IN ('REJECTED', 'FAILED')
              AND (
                    (LOWER(player) = LOWER(?) AND LOWER(opponent) = LOWER(?))
                 OR (LOWER(player) = LOWER(?) AND LOWER(opponent) = LOWER(?))
              )
              AND LOWER(tournament) = LOWER(?)
              AND julianday('now') - julianday(created_at) <= ?
            ORDER BY created_at DESC
            LIMIT 1
        """, (player, opponent, opponent, player, tournament, max_age_days)).fetchall()
        conn.close()
    except Exception:
        return None
    if not rows:
        return None
    r = dict(rows[0])
    return {
        "eval_id": r["eval_id"],
        "classification": r["classification"],
        "reasoning_text": r.get("reasoning_text"),
        "created_at": r["created_at"],
    }

def find_pending_moneyline_picks(max_age_days=14):
    """
    Real, posted moneyline picks (GREEN or YELLOW -- a real graded pick)
    with no outcome recorded yet -- candidates for automatic settlement.

    Requires predicted_winner to be non-NULL: picks made before
    2026-08-04 never had it saved (see the systematic Dataset A audit
    that session) and would have nothing real to compare a settled
    result against -- those need a manual check, not a guess, so
    they're deliberately excluded rather than silently mishandled.

    max_age_days guards against reaching back into ancient unsettled
    picks that likely never will settle (data gap, match cancelled,
    etc.) -- default 14 days is generous for tennis' real turnaround.

    Returns a list of dicts: {eval_id, player, opponent, tournament,
    tour, predicted_winner, match_scheduled_time, created_at}. Never
    raises -- degrades to [] on any DB issue.
    """
    if not DB_PATH.exists():
        return []
    try:
        conn = _connect_ro()
        rows = conn.execute("""
            SELECT eval_id, player, opponent, tournament, tour,
                   predicted_winner, match_scheduled_time, created_at
            FROM evaluations
            WHERE evaluation_type = 'MONEYLINE'
              AND outcome IS NULL
              AND predicted_winner IS NOT NULL
              AND final_grade IN ('GREEN', 'YELLOW')
              AND julianday('now') - julianday(created_at) <= ?
            ORDER BY created_at ASC
        """, (max_age_days,)).fetchall()
        conn.close()
    except Exception:
        return []
    return [dict(r) for r in rows]

def find_confidence_for_leg(player, stat_label=None, line=None, max_age_days=3):
    """
    Given one leg extracted from a slip screenshot (player, and for a
    prop leg the stat label + line), find Apex's own real evaluation
    for that exact pick -- the screenshot tells us the market's
    structure (legs, payout table), not what Apex actually thinks;
    that has to come from Dataset A.

    For a prop leg: matches player (case-insensitive) + stat label
    (case-insensitive substring, since screenshot labels like "Total
    Games" and Dataset A's stored market field aren't guaranteed to be
    byte-identical) + the same line value. For a moneyline leg
    (stat_label/line both None), matches on player only.

    max_age_days keeps this to a genuinely recent evaluation of the
    same real pick, same reasoning as find_matching_prop_classification
    -- a much older evaluation of the same player/stat/line combo could
    be a different real match entirely.

    Returns dict {eval_id, final_confidence, final_grade, evaluation_type}
    or None if no safe match is found -- callers must treat None as
    "Apex has no real opinion on this leg" and either skip it from the
    EV calculation or flag it, never silently substitute a guessed
    confidence.
    """
    if not DB_PATH.exists():
        return None
    try:
        conn = _connect_ro()
        if stat_label is None and line is None:
            rows = conn.execute("""
                SELECT eval_id, final_confidence, final_grade, evaluation_type
                FROM evaluations
                WHERE evaluation_type = 'MONEYLINE'
                  AND final_confidence IS NOT NULL
                  AND LOWER(player) = LOWER(?)
                  AND julianday('now') - julianday(created_at) <= ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (player, max_age_days)).fetchall()
        else:
            rows = conn.execute("""
                SELECT eval_id, final_confidence, final_grade, evaluation_type
                FROM evaluations
                WHERE evaluation_type = 'PROP'
                  AND final_confidence IS NOT NULL
                  AND LOWER(player) = LOWER(?)
                  AND LOWER(market) LIKE '%' || LOWER(?) || '%'
                  AND line = ?
                  AND julianday('now') - julianday(created_at) <= ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (player, stat_label or "", line, max_age_days)).fetchall()
        conn.close()
    except Exception:
        return None
    if not rows:
        return None
    r = dict(rows[0])
    return {"eval_id": r["eval_id"], "final_confidence": r["final_confidence"],
            "final_grade": r["final_grade"], "evaluation_type": r["evaluation_type"]}

# (c) 2026 Demetrius Reynolds -- Project Apex Intelligence(tm).
