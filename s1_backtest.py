"""
s1_backtest.py
2026-08-07 — real walk-forward backtest of the LIVE S1 (surface form)
signal from openclaw_core.py, using the exact real thresholds already
in production:
    ML_S1_SURFACE_MODERATE_GAP_PP = 5
    ML_S1_SURFACE_STRONG_GAP_PP   = 15

Real logic replicated exactly from signal_s1_surface_form(): compare
each player's CURRENT-SEASON win% on the match's surface. Gap < 5pp =
signal 0 (no edge). 5-15pp = moderate (+/-1). 15pp+ = strong (+/-2).
"Current season" = same calendar year as the match, matching the real
production semantics.

Same walk-forward discipline as elo_backtest.py: for each match, uses
ONLY each player's win/loss record on this surface THIS YEAR, from
matches strictly BEFORE this one — no lookahead. A player with zero
confirmed matches on this surface this season (before this match)
returns None, exactly matching the real production function's
None-if-either-side-unconfirmed behavior.

Reports TWO real numbers:
  - auc: computed on the raw continuous win%-gap (before threshold
    bucketing) -- tests whether the underlying signal has real
    information at all, independent of where the /5pp//15pp
    thresholds happen to sit.
  - accuracy_pct: plain accuracy using the ACTUAL discrete production
    signal (only counting matches where the live thresholds would
    have produced a real, non-zero call) -- this is what the live
    system actually does today, thresholds and all.
"""
from elo_ops import _conn
from elo_backtest import _auc

MIN_SAMPLES_FOR_VERDICT = 30
PROMISING_AUC_THRESHOLD = 0.55

ML_S1_SURFACE_STRONG_GAP_PP = 15
ML_S1_SURFACE_MODERATE_GAP_PP = 5


def _load_raw_matches(conn):
    rows = conn.execute(
        "SELECT player_a, player_b, winner, surface, match_date "
        "FROM matches "
        "WHERE winner IS NOT NULL AND match_date IS NOT NULL AND surface IS NOT NULL "
        "ORDER BY match_date ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def run_s1_backtest():
    """
    Real walk-forward test. season_record[(player, year, surface)] =
    [wins, losses], updated AFTER each match is used for prediction.
    A match with no surface on file is skipped entirely (S1 requires
    a real surface value — same as the live signal, which is always
    called with a real surface argument).
    """
    conn = _conn()
    if not conn:
        return {"status": "offline", "message": "Warehouse database not accessible"}
    try:
        raw = _load_raw_matches(conn)
    finally:
        conn.close()

    season_record: dict = {}  # (player, year, surface) -> [wins, losses]
    gap_pairs = []            # (raw_gap_pp, label) for AUC -- continuous signal
    correct = 0
    total_predicted = 0       # only counts matches where the REAL production signal was non-zero
    skipped_no_prior_data = 0

    for row in raw:
        pa, pb, winner, surface, match_date = (
            row["player_a"], row["player_b"], row["winner"],
            row["surface"], row["match_date"]
        )
        if winner not in (pa, pb):
            continue
        year = match_date[:4]

        pa_rec = season_record.get((pa, year, surface))
        pb_rec = season_record.get((pb, year, surface))

        if pa_rec and pb_rec and (pa_rec[0] + pa_rec[1]) > 0 and (pb_rec[0] + pb_rec[1]) > 0:
            pa_rate = pa_rec[0] / (pa_rec[0] + pa_rec[1]) * 100
            pb_rate = pb_rec[0] / (pb_rec[0] + pb_rec[1]) * 100
            gap = pa_rate - pb_rate
            label = 1 if winner == pa else 0
            gap_pairs.append((gap, label))

            # Real production signal, exact same thresholds as live:
            if abs(gap) >= ML_S1_SURFACE_MODERATE_GAP_PP:
                predicted = pa if gap > 0 else pb
                total_predicted += 1
                if predicted == winner:
                    correct += 1
        else:
            skipped_no_prior_data += 1

        # Apply the real result AFTER recording the prediction
        for name, won in ((pa, winner == pa), (pb, winner == pb)):
            key = (name, year, surface)
            rec = season_record.setdefault(key, [0, 0])
            if won:
                rec[0] += 1
            else:
                rec[1] += 1

    result = {
        "status": "ok",
        "total_matches_seen": len(raw),
        "skipped_no_prior_data": skipped_no_prior_data,
        "n_gap_pairs": len(gap_pairs),
        "n_signal_fired": total_predicted,
    }

    if len(gap_pairs) < MIN_SAMPLES_FOR_VERDICT:
        result["auc"] = None
        result["auc_verdict"] = f"NOT ENOUGH DATA (n={len(gap_pairs)})"
    else:
        auc = _auc(gap_pairs)
        result["auc"] = round(auc, 4) if auc is not None else None
        result["auc_verdict"] = ("GENUINE SIGNAL" if auc and auc >= PROMISING_AUC_THRESHOLD
                                  else "NO REAL SIGNAL") if auc is not None else "UNDEFINED"

    if total_predicted < MIN_SAMPLES_FOR_VERDICT:
        result["accuracy_pct"] = None
        result["accuracy_verdict"] = f"NOT ENOUGH DATA (n={total_predicted})"
    else:
        result["accuracy_pct"] = round(correct / total_predicted * 100, 1)
        result["correct"] = correct

    return result


if __name__ == "__main__":
    print(run_s1_backtest())
