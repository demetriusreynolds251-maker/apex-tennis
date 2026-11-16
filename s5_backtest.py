"""
s5_backtest.py
2026-08-07 — real walk-forward backtest of the LIVE S5 (dominance/
classification confirmation) signal. Formula and thresholds confirmed
directly from source (compute_ml_dominance_score() and
signal_s5_classification_confirmation() in openclaw_core.py), not
guessed:

    dominance_score = 0.35*serve_edge + 0.25*return_edge
                     + 0.15*clutch_edge + 0.25*form_edge
    serve_edge  = player.hold_pct    - opponent.hold_pct
    return_edge = player.break_pct   - opponent.break_pct
    clutch_edge = player.bp_save_pct - opponent.bp_save_pct
    form_edge   = player.win_pct     - opponent.win_pct   (SURFACE-
                  specific season win rate — the SAME real quantity
                  S1 measures; S5 and S1 share this one input)

    ML_S5_CLASSIFICATION_MODERATE_MAGNITUDE = 8
    ML_S5_CLASSIFICATION_STRONG_MAGNITUDE   = 12

IMPORTANT SCALE DETAIL, verified against source before building this
(not assumed): hold_pct/break_pct/bp_save_pct/win_pct are on a
PERCENTAGE-POINT scale (0-100), matching S1's 5pp/15pp convention —
NOT the 0-1 fractions season_stats.py's engine returns internally.
This script's own accumulators output percentage-point scale
directly (×100 applied at the point of computation) so the real
weighted formula and its real 8/12 magnitude thresholds apply
correctly, unlike a raw 0-1 scale which would make every real
dominance_score trivially tiny compared to the thresholds.

compute_ml_dominance_score() marks status INCOMPLETE_DATA if EITHER
player is missing ANY of the four required fields — this backtest
requires all four for both players before including a match, same
real gate.

Real target: LABEL = did player_a win (binary) — same AUC-based
approach as ELO/S1/S3/S4 (not the Pearson-correlation style used for
the props stat-line tests, since S5 predicts match win, not a
continuous stat).
"""
from datetime import datetime

from elo_ops import _conn
from elo_backtest import _auc

MIN_SAMPLES_FOR_VERDICT = 30
PROMISING_AUC_THRESHOLD = 0.55
MIN_SVGMS_FOR_RATE = 20  # same real floor as season_stats.py

ML_S5_CLASSIFICATION_MODERATE_MAGNITUDE = 8
ML_S5_CLASSIFICATION_STRONG_MAGNITUDE = 12

SERVE_WEIGHT, RETURN_WEIGHT, CLUTCH_WEIGHT, FORM_WEIGHT = 0.35, 0.25, 0.15, 0.25


class _HoldAccum:
    __slots__ = ("svgms", "bp_faced", "bp_saved")
    def __init__(self):
        self.svgms = 0
        self.bp_faced = 0
        self.bp_saved = 0

    def hold_pct(self):
        if self.svgms < MIN_SVGMS_FOR_RATE:
            return None
        return (1.0 - ((self.bp_faced - self.bp_saved) / self.svgms)) * 100  # percentage-point scale


def _load_raw_rows(conn):
    rows = conn.execute(
        "SELECT player_a, player_b, winner, surface, match_date, "
        "player_a_svgms, player_a_bp_faced, player_a_bp_saved, "
        "player_b_svgms, player_b_bp_faced, player_b_bp_saved "
        "FROM matches "
        "WHERE winner IS NOT NULL AND match_date IS NOT NULL AND surface IS NOT NULL "
        "AND player_a_svgms IS NOT NULL AND player_a_bp_faced IS NOT NULL AND player_a_bp_saved IS NOT NULL "
        "AND player_b_svgms IS NOT NULL AND player_b_bp_faced IS NOT NULL AND player_b_bp_saved IS NOT NULL "
        "ORDER BY match_date ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def run_s5_backtest():
    conn = _conn()
    if not conn:
        return {"status": "offline", "message": "Warehouse database not accessible"}
    try:
        raw = _load_raw_rows(conn)
    finally:
        conn.close()

    hold_accum: dict = {}     # (player, year) -> _HoldAccum  (serve_edge, clutch_edge)
    return_accum: dict = {}   # (player, year) -> [breaks_achieved, return_games_faced]  (return_edge)
    season_record: dict = {}  # (player, year, surface) -> [wins, losses]  (form_edge, same as S1)

    gap_pairs = []       # (dominance_score, label) for AUC
    correct = 0
    total_predicted = 0  # matches where the real live 8/12 thresholds would have fired
    skipped_incomplete_data = 0

    for row in raw:
        pa, pb, winner, surface, date = row["player_a"], row["player_b"], row["winner"], row["surface"], row["match_date"]
        if winner not in (pa, pb):
            continue
        year = date[:4]

        pa_hold_acc = hold_accum.get((pa, year))
        pb_hold_acc = hold_accum.get((pb, year))
        pa_hold = pa_hold_acc.hold_pct() if pa_hold_acc else None
        pb_hold = pb_hold_acc.hold_pct() if pb_hold_acc else None

        pa_bp_save = (pa_hold_acc.bp_saved / pa_hold_acc.bp_faced * 100) if pa_hold_acc and pa_hold_acc.bp_faced >= 5 else None
        pb_bp_save = (pb_hold_acc.bp_saved / pb_hold_acc.bp_faced * 100) if pb_hold_acc and pb_hold_acc.bp_faced >= 5 else None

        pa_r = return_accum.get((pa, year))
        pb_r = return_accum.get((pb, year))
        pa_return = (pa_r[0] / pa_r[1] * 100) if pa_r and pa_r[1] >= MIN_SVGMS_FOR_RATE else None
        pb_return = (pb_r[0] / pb_r[1] * 100) if pb_r and pb_r[1] >= MIN_SVGMS_FOR_RATE else None

        pa_rec = season_record.get((pa, year, surface))
        pb_rec = season_record.get((pb, year, surface))
        pa_win_pct = (pa_rec[0] / (pa_rec[0] + pa_rec[1]) * 100) if pa_rec and (pa_rec[0] + pa_rec[1]) > 0 else None
        pb_win_pct = (pb_rec[0] / (pb_rec[0] + pb_rec[1]) * 100) if pb_rec and (pb_rec[0] + pb_rec[1]) > 0 else None

        all_present = all(v is not None for v in
                           (pa_hold, pb_hold, pa_return, pb_return, pa_bp_save, pb_bp_save, pa_win_pct, pb_win_pct))

        if all_present:
            serve_edge = pa_hold - pb_hold
            return_edge = pa_return - pb_return
            clutch_edge = pa_bp_save - pb_bp_save
            form_edge = pa_win_pct - pb_win_pct
            dominance_score = (SERVE_WEIGHT * serve_edge + RETURN_WEIGHT * return_edge
                                + CLUTCH_WEIGHT * clutch_edge + FORM_WEIGHT * form_edge)
            label = 1 if winner == pa else 0
            gap_pairs.append((dominance_score, label))

            magnitude = abs(dominance_score)
            if magnitude >= ML_S5_CLASSIFICATION_MODERATE_MAGNITUDE:
                predicted = pa if dominance_score > 0 else pb
                total_predicted += 1
                if predicted == winner:
                    correct += 1
        else:
            skipped_incomplete_data += 1

        # Update state AFTER using it -- real values for this match
        pa_sv, pa_bpf, pa_bps = row["player_a_svgms"], row["player_a_bp_faced"], row["player_a_bp_saved"]
        pb_sv, pb_bpf, pb_bps = row["player_b_svgms"], row["player_b_bp_faced"], row["player_b_bp_saved"]

        pa_acc = hold_accum.setdefault((pa, year), _HoldAccum())
        pa_acc.svgms += pa_sv; pa_acc.bp_faced += pa_bpf; pa_acc.bp_saved += pa_bps
        pb_acc = hold_accum.setdefault((pb, year), _HoldAccum())
        pb_acc.svgms += pb_sv; pb_acc.bp_faced += pb_bpf; pb_acc.bp_saved += pb_bps

        # pa's return dominance this match: pb's real (bp_faced - bp_saved)
        # -- breaks pa achieved -- over pb's real svgms (pa's return games faced)
        pa_r_acc = return_accum.setdefault((pa, year), [0, 0])
        pa_r_acc[0] += (pb_bpf - pb_bps)
        pa_r_acc[1] += pb_sv
        pb_r_acc = return_accum.setdefault((pb, year), [0, 0])
        pb_r_acc[0] += (pa_bpf - pa_bps)
        pb_r_acc[1] += pa_sv

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
        "skipped_incomplete_data": skipped_incomplete_data,
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
    print(run_s5_backtest())
