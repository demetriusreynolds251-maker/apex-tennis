"""
elo_backtest.py
2026-08-07 — real walk-forward backtest for the ELO signal.

Standalone script, not yet merged with feature_value_backtest.py —
that file wasn't available to read this session, so rather than
guess its internals this reimplements the same real principle
already established for it (rolling pre-match state, no lookahead)
using elo_calculator.py's own functions directly. Flagged here
rather than silently diverging — align the two once the real file
is available.

THE CORE DISCIPLINE THIS SCRIPT EXISTS TO ENFORCE: you cannot use
rebuild_elo_ratings()'s final, fully-built ratings to "predict" old
matches — a rating built from a player's ENTIRE history already
encodes the outcome of every match in it, including ones after the
match being predicted. That's lookahead bias, and it would make any
AUC number meaningless. This script walks matches in chronological
order and, for each one, only uses whatever ratings existed the
moment BEFORE that match — exactly mirroring a real evaluation
happening the day before a real match, with no future information
available.

Real output format follows the same convention already established
in the Phase C verdict (2026-07-28): number of examples used, real
AUC, and a plain verdict — genuine signal vs no signal — using the
same MIN_SAMPLES philosophy calibration.py already applies elsewhere
on this project (a real number under 30 real examples doesn't get a
verdict at all, just "not enough data").
"""
from datetime import datetime

from elo_ops import _conn
from elo_calculator import (
    PlayerRating, apply_match, expected_score, _normalize_surface, K_FACTOR
)

MIN_SAMPLES_FOR_VERDICT = 30  # same floor calibration.py already uses elsewhere on this project
PROMISING_AUC_THRESHOLD = 0.55  # H2H (S3) cleared this at 0.584; ~0.50 = no real signal


def _load_raw_matches(conn):
    """Real query — keeps player_a/player_b/winner AS-IS (not
    winner/loser) since the backtest needs an outcome LABEL, and
    reusing elo_ops's winner/loser-oriented loader would bake the
    real outcome into the row identity itself, making the whole
    exercise circular."""
    rows = conn.execute(
        "SELECT player_a, player_b, winner, surface, match_date, tour "
        "FROM matches "
        "WHERE winner IS NOT NULL AND match_date IS NOT NULL "
        "ORDER BY match_date ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def run_elo_backtest(k: float = K_FACTOR):
    """
    Real walk-forward backtest. Returns a dict with results split
    three ways — overall ELO, surface-specific ELO (where available),
    and a combined view — since collapsing them into one number would
    hide whether surface-specific rating adds anything beyond overall
    (same "don't collapse real information" principle used everywhere
    else on this project).

    For each match, BEFORE updating ratings:
      - skip if either player has zero prior real matches (nothing to
        predict from yet — same None-not-0 discipline as every other
        signal here)
      - record (predicted_probability, actual_label) for overall ELO
      - if the surface normalizes AND both players have prior
        surface-specific matches, ALSO record a separate pair for
        surface ELO
    Then apply the real match result and continue.
    """
    conn = _conn()
    if not conn:
        return {"status": "offline", "message": "Warehouse database not accessible"}

    try:
        raw = _load_raw_matches(conn)
    finally:
        conn.close()

    ratings: dict = {}
    overall_pairs = []   # (predicted_prob_player_a_wins, actual_label)
    surface_pairs = []
    skipped_no_prior_data = 0

    for row in raw:
        pa, pb, winner = row["player_a"], row["player_b"], row["winner"]
        if winner not in (pa, pb):
            continue  # same real data-integrity skip as elo_ops.load_match_rows_for_elo

        ra = ratings.get(pa)
        rb = ratings.get(pb)
        label = 1 if winner == pa else 0

        if ra is not None and rb is not None and ra.overall_matches > 0 and rb.overall_matches > 0:
            prob = expected_score(ra.overall, rb.overall)
            overall_pairs.append((prob, label))

            surface = _normalize_surface(row.get("surface"))
            if surface and ra.surface_matches[surface] > 0 and rb.surface_matches[surface] > 0:
                s_prob = expected_score(ra.surface[surface], rb.surface[surface])
                surface_pairs.append((s_prob, label))
        else:
            skipped_no_prior_data += 1

        # NOW apply the real result — this must happen AFTER recording
        # the prediction above, or the prediction would already reflect
        # this match's own outcome (the exact lookahead bug this script
        # exists to avoid).
        winner_name = pa if winner == pa else pb
        loser_name = pb if winner == pa else pa
        apply_match(ratings, winner_name, loser_name, row.get("surface"), k=k)

    return {
        "status": "ok",
        "total_matches_seen": len(raw),
        "skipped_no_prior_data": skipped_no_prior_data,
        "overall": _verdict("overall ELO", overall_pairs),
        "surface": _verdict("surface-specific ELO", surface_pairs),
    }


def _auc(pairs):
    """
    Real AUC via the rank-sum (Mann-Whitney U) formula — no sklearn
    dependency needed. AUC = P(a randomly chosen positive example is
    ranked above a randomly chosen negative example).

    Returns None if all labels are the same (AUC undefined — can't
    measure discrimination with only one class present).
    """
    pos = [p for p, y in pairs if y == 1]
    neg = [p for p, y in pairs if y == 0]
    if not pos or not neg:
        return None

    all_scores = sorted(pairs, key=lambda x: x[0])
    ranks = {}
    i = 0
    r = 1
    while i < len(all_scores):
        j = i
        while j < len(all_scores) and all_scores[j][0] == all_scores[i][0]:
            j += 1
        avg_rank = (r + (r + (j - i) - 1)) / 2.0
        for idx in range(i, j):
            ranks[idx] = avg_rank
        r += (j - i)
        i = j

    rank_sum_pos = sum(ranks[idx] for idx, (p, y) in enumerate(all_scores) if y == 1)
    n_pos, n_neg = len(pos), len(neg)
    auc = (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return auc


def _verdict(label: str, pairs):
    """Real verdict, same format as the Phase C precedent — plain
    language, no fabricated confidence below the real sample floor."""
    n = len(pairs)
    if n < MIN_SAMPLES_FOR_VERDICT:
        return {
            "label": label, "n": n, "auc": None,
            "verdict": f"NOT ENOUGH DATA (n={n}, need {MIN_SAMPLES_FOR_VERDICT}+)"
        }
    auc = _auc(pairs)
    if auc is None:
        return {"label": label, "n": n, "auc": None, "verdict": "UNDEFINED (single-class sample)"}
    verdict = "GENUINE SIGNAL" if auc >= PROMISING_AUC_THRESHOLD else "NO REAL SIGNAL"
    return {"label": label, "n": n, "auc": round(auc, 4), "verdict": verdict}


if __name__ == "__main__":
    result = run_elo_backtest()
    print(result)
