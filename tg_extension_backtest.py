"""
tg_extension_backtest.py
2026-08-07 — real test of the empirical core of props' TG (Total
Games) MORE-path logic, confirmed from DEMETRIUS_PROPS_DECISION_TREE.txt:
a MORE pick requires "BOTH players are CONFIRMED extenders — recent
matches actually going long (demonstrated behavior, cited)".

This tests that underlying claim directly: does each player's recent
"extend rate" (fraction of their last few matches that went the
maximum number of sets) predict how many REAL total games get played
in THIS match?

Different in kind from the moneyline signal backtests: the real
target here is CONTINUOUS (total games played), not binary (who won)
-- so this uses Pearson correlation, not AUC. Same walk-forward
discipline: each player's extend rate is built ONLY from their real
matches strictly before the one being evaluated.

Real total games = summed from the actual `score` string (both
players' games in every real set), reusing the same set-token
parsing already validated in historical_match_enrichment.py.
"""
import re

from elo_ops import _conn

MIN_SAMPLES_FOR_VERDICT = 30
SET_TOKEN = re.compile(r"^(\d{1,2})-(\d{1,2})(\(\d+\))?$")
RECENT_WINDOW = 5  # real matches to look back for each player's extend rate
MIN_PRIOR_MATCHES = 3  # need at least this many real prior matches to trust a rate


def _real_total_games(score):
    """Sums real games across every set in the score string. Returns
    None if unparseable -- never guessed."""
    if not score:
        return None
    total = 0
    found_any = False
    for tok in score.strip().split():
        m = SET_TOKEN.match(tok)
        if m:
            total += int(m.group(1)) + int(m.group(2))
            found_any = True
    return total if found_any else None


def _pearson_r(pairs):
    """Real Pearson correlation coefficient, no external dependency.
    Returns None if variance is zero on either side (undefined)."""
    n = len(pairs)
    if n < 2:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x ** 0.5 * var_y ** 0.5)


def _load_raw_matches(conn):
    rows = conn.execute(
        "SELECT player_a, player_b, winner, match_date, score, set_count, best_of "
        "FROM matches "
        "WHERE winner IS NOT NULL AND match_date IS NOT NULL AND score IS NOT NULL "
        "ORDER BY match_date ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def run_tg_extension_backtest():
    conn = _conn()
    if not conn:
        return {"status": "offline", "message": "Warehouse database not accessible"}
    try:
        raw = _load_raw_matches(conn)
    finally:
        conn.close()

    # player -> list of booleans (went the distance), most recent last
    extend_history: dict = {}

    pairs = []  # (combined_extend_rate, real_total_games)

    for row in raw:
        pa, pb = row["player_a"], row["player_b"]
        real_games = _real_total_games(row["score"])

        pa_hist = extend_history.get(pa, [])
        pb_hist = extend_history.get(pb, [])

        if real_games is not None and len(pa_hist) >= MIN_PRIOR_MATCHES and len(pb_hist) >= MIN_PRIOR_MATCHES:
            pa_recent = pa_hist[-RECENT_WINDOW:]
            pb_recent = pb_hist[-RECENT_WINDOW:]
            pa_rate = sum(pa_recent) / len(pa_recent)
            pb_rate = sum(pb_recent) / len(pb_recent)
            combined = pa_rate + pb_rate  # 0.0 (neither extends) to 2.0 (both always extend)
            pairs.append((combined, real_games))

        # Update history AFTER using it for this match's prediction
        went_the_distance = bool(row["best_of"] and row["set_count"]
                                  and row["set_count"] >= row["best_of"])
        extend_history.setdefault(pa, []).append(went_the_distance)
        extend_history.setdefault(pb, []).append(went_the_distance)

    result = {"status": "ok", "total_matches_seen": len(raw), "n_usable_pairs": len(pairs)}

    if len(pairs) < MIN_SAMPLES_FOR_VERDICT:
        result["pearson_r"] = None
        result["verdict"] = f"NOT ENOUGH DATA (n={len(pairs)})"
        return result

    r = _pearson_r(pairs)
    if r is None:
        result["pearson_r"] = None
        result["verdict"] = "UNDEFINED (zero variance)"
        return result

    result["pearson_r"] = round(r, 4)
    # Real, honest interpretation bands (standard, not invented for
    # this project): |r| < 0.1 negligible, 0.1-0.3 weak, 0.3-0.5
    # moderate, 0.5+ strong. No claim of "genuine signal" language
    # here since this isn't AUC/accuracy -- correlation strength
    # speaks for itself.
    abs_r = abs(r)
    if abs_r < 0.1:
        result["verdict"] = "NEGLIGIBLE — no meaningful real relationship"
    elif abs_r < 0.3:
        result["verdict"] = "WEAK real relationship"
    elif abs_r < 0.5:
        result["verdict"] = "MODERATE real relationship"
    else:
        result["verdict"] = "STRONG real relationship"

    # Real supporting numbers: average total games in the top vs
    # bottom quartile of combined extend rate, so the finding is
    # readable in plain terms (games), not just an r value.
    sorted_pairs = sorted(pairs, key=lambda p: p[0])
    q = max(1, len(sorted_pairs) // 4)
    low_quartile_games = [g for _, g in sorted_pairs[:q]]
    high_quartile_games = [g for _, g in sorted_pairs[-q:]]
    result["avg_total_games_low_extend_quartile"] = round(sum(low_quartile_games) / len(low_quartile_games), 2)
    result["avg_total_games_high_extend_quartile"] = round(sum(high_quartile_games) / len(high_quartile_games), 2)

    return result


if __name__ == "__main__":
    print(run_tg_extension_backtest())
