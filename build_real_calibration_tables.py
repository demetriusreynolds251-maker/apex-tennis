"""
build_real_calibration_tables.py
Run this ON THE MAC MINI, in the real project directory, with real
tennis_warehouse.sqlite present. This produces REAL calibration
tables for ELO / S5 / S1 from your actual 520K-row Dataset B -- no
synthetic data.

WHAT THIS REUSES (real, not reimplemented):
  - elo_calculator.py: PlayerRating, apply_match() -- the real ELO
    engine, used exactly as-is.
  - The exact real dominance_score formula from s5_backtest.py
    (0.35 serve + 0.25 return + 0.15 clutch + 0.25 form, all pp-scale).
  - The exact real surface-form gap logic from s1_backtest.py.

WHAT THIS DOES NOT INCLUDE:
  - H2H (S3). The real H2H backtest script wasn't available when this
    was written -- rather than guess at its raw-value logic (which
    depends on v8_get_h2h()'s real shape), H2H is left out entirely.
    Upload that script and this can be extended to cover it.
  - elo_ops.py's real _conn(). Not available either -- this connects
    directly to WAREHOUSE_PATH below instead. If that's not the real
    path on your machine, fix the one line and rerun.

USAGE:
    cd /path/to/home/openclaw\\ /   # mind the trailing space
    python3 build_real_calibration_tables.py

Paste the full printed output back into chat.
"""
import sqlite3
import os
import json
import sys

# ─── FIX THIS IF WRONG ────────────────────────────────────────────
WAREHOUSE_PATH = "data/warehouse/tennis_warehouse.sqlite"
# ───────────────────────────────────────────────────────────────────

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from elo_calculator import apply_match  # real, unmodified
from build_signal_calibration import build_calibration_table, print_table, MIN_SAMPLES_PER_BUCKET

MIN_SVGMS_FOR_RATE = 20
SERVE_WEIGHT, RETURN_WEIGHT, CLUTCH_WEIGHT, FORM_WEIGHT = 0.35, 0.25, 0.15, 0.25


def _conn():
    if not os.path.exists(WAREHOUSE_PATH):
        print(f"ERROR: no file found at WAREHOUSE_PATH = '{WAREHOUSE_PATH}'")
        print("Edit WAREHOUSE_PATH at the top of this script to the real path on this machine, then rerun.")
        return None
    conn = sqlite3.connect(WAREHOUSE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


class _HoldAccum:
    __slots__ = ("svgms", "bp_faced", "bp_saved")
    def __init__(self):
        self.svgms = 0
        self.bp_faced = 0
        self.bp_saved = 0

    def hold_pct(self):
        if self.svgms < MIN_SVGMS_FOR_RATE:
            return None
        return (1.0 - ((self.bp_faced - self.bp_saved) / self.svgms)) * 100


def build_elo_pairs(conn):
    """Real walk-forward ELO gap pairs. Same discipline as every other
    backtest here: use ratings state BEFORE this match to record the
    pair, then update state AFTER, using apply_match() from the real
    elo_calculator.py directly -- not reimplemented."""
    rows = conn.execute(
        "SELECT player_a, player_b, winner, surface, match_date FROM matches "
        "WHERE winner IS NOT NULL AND match_date IS NOT NULL ORDER BY match_date ASC"
    ).fetchall()

    ratings = {}
    pairs = []
    skipped_no_prior = 0
    for row in rows:
        pa, pb, winner, surface = row["player_a"], row["player_b"], row["winner"], row["surface"]
        if winner not in (pa, pb):
            continue
        p = ratings.get(pa)
        o = ratings.get(pb)
        if p is not None and o is not None and p.overall_matches > 0 and o.overall_matches > 0:
            gap = p.overall - o.overall
            label = 1 if winner == pa else 0
            pairs.append((gap, label))
        else:
            skipped_no_prior += 1
        loser = pb if winner == pa else pa
        apply_match(ratings, winner, loser, surface)
    return pairs, skipped_no_prior, len(rows)


def build_s5_pairs(conn):
    """Exact real dominance_score formula and accumulation logic,
    copied from s5_backtest.py -- produces the full continuous
    (dominance_score, label) distribution, same pairs that script's
    own AUC test consumes."""
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

    hold_accum, return_accum, season_record = {}, {}, {}
    pairs = []
    skipped_incomplete = 0

    for row in rows:
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
            pairs.append((dominance_score, label))
        else:
            skipped_incomplete += 1

        pa_sv, pa_bpf, pa_bps = row["player_a_svgms"], row["player_a_bp_faced"], row["player_a_bp_saved"]
        pb_sv, pb_bpf, pb_bps = row["player_b_svgms"], row["player_b_bp_faced"], row["player_b_bp_saved"]

        pa_acc = hold_accum.setdefault((pa, year), _HoldAccum())
        pa_acc.svgms += pa_sv; pa_acc.bp_faced += pa_bpf; pa_acc.bp_saved += pa_bps
        pb_acc = hold_accum.setdefault((pb, year), _HoldAccum())
        pb_acc.svgms += pb_sv; pb_acc.bp_faced += pb_bpf; pb_acc.bp_saved += pb_bps

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

    return pairs, skipped_incomplete, len(rows)


MIN_H2H_SAMPLE = 3  # lower than the field-based 10 -- derived H2H has real per-match
                     # granularity to work with, not just whatever the API happened to stamp,
                     # so a real 3+ prior meeting is still a meaningful sample here


def build_h2h_pairs(conn):
    """Real H2H pairs, DERIVED directly from Dataset B's own match
    history -- NOT from the h2h_at_match field. That field is
    populated on only ~0.3% of real rows (confirmed this session: the
    uploaded historical CSVs used to bulk-import much of Dataset B are
    in the standard Sackmann format, which has no head-to-head column
    at all -- h2h_at_match most likely only gets written by the live
    V8 collector for real-time matches, not backfilled history).

    Same real walk-forward discipline as everything else here: for
    each match, look at the real prior meeting record between these
    two specific players BEFORE this match (built up match by match,
    in chronological order), THEN record it as a pair, THEN update
    the record with this match's real result.

    A pair key is order-independent (frozenset of the two names) so
    "Player A vs Player B" and "Player B vs Player A" accumulate into
    the same real head-to-head record regardless of which one is
    listed as player_a in a given row.
    """
    rows = conn.execute(
        "SELECT player_a, player_b, winner, match_date FROM matches "
        "WHERE winner IS NOT NULL AND match_date IS NOT NULL ORDER BY match_date ASC"
    ).fetchall()

    h2h_record = {}  # frozenset({p1,p2}) -> {p1: wins_vs_p2, p2: wins_vs_p1}
    pairs = []
    skipped_thin = 0

    for row in rows:
        pa, pb, winner = row["player_a"], row["player_b"], row["winner"]
        if winner not in (pa, pb):
            continue

        key = frozenset((pa, pb))
        rec = h2h_record.get(key)
        if rec is not None:
            pa_wins = rec.get(pa, 0)
            pb_wins = rec.get(pb, 0)
            total = pa_wins + pb_wins
            if total >= MIN_H2H_SAMPLE:
                pct_a = pa_wins / total
                raw = 2 * pct_a - 1  # rescale 0..1 -> -1..1, symmetric around "even"
                label = 1 if winner == pa else 0
                pairs.append((raw, label))
            else:
                skipped_thin += 1
        else:
            skipped_thin += 1

        rec = h2h_record.setdefault(key, {})
        rec[winner] = rec.get(winner, 0) + 1

    return pairs, skipped_thin, len(rows)


def build_s1_pairs(conn):
    """Exact real surface-form gap logic, copied from s1_backtest.py."""
    rows = conn.execute(
        "SELECT player_a, player_b, winner, surface, match_date "
        "FROM matches "
        "WHERE winner IS NOT NULL AND match_date IS NOT NULL AND surface IS NOT NULL "
        "ORDER BY match_date ASC"
    ).fetchall()

    season_record = {}
    pairs = []
    skipped_no_prior = 0

    for row in rows:
        pa, pb, winner, surface, match_date = (
            row["player_a"], row["player_b"], row["winner"], row["surface"], row["match_date"]
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
            pairs.append((gap, label))
        else:
            skipped_no_prior += 1

        for name, won in ((pa, winner == pa), (pb, winner == pb)):
            key = (name, year, surface)
            rec = season_record.setdefault(key, [0, 0])
            if won:
                rec[0] += 1
            else:
                rec[1] += 1

    return pairs, skipped_no_prior, len(rows)


def main():
    conn = _conn()
    if not conn:
        return
    try:
        print("Building real ELO gap pairs (walk-forward, no lookahead)...")
        elo_pairs, elo_skipped, elo_total = build_elo_pairs(conn)
        print(f"  {len(elo_pairs)} real pairs, {elo_skipped} skipped (no prior real ELO for one/both players), {elo_total} rows scanned")

        print("Building real S5 (dominance) pairs...")
        s5_pairs, s5_skipped, s5_total = build_s5_pairs(conn)
        print(f"  {len(s5_pairs)} real pairs, {s5_skipped} skipped (incomplete real data), {s5_total} rows scanned")

        print("Building real S1 (surface form) pairs...")
        s1_pairs, s1_skipped, s1_total = build_s1_pairs(conn)
        print(f"  {len(s1_pairs)} real pairs, {s1_skipped} skipped (no prior real season data), {s1_total} rows scanned")

        print("Building real H2H pairs (derived from match history, min sample = 3 prior meetings)...")
        h2h_pairs, h2h_skipped, h2h_total = build_h2h_pairs(conn)
        print(f"  {len(h2h_pairs)} real pairs, {h2h_skipped} skipped (fewer than 3 prior real meetings), {h2h_total} rows scanned")
    finally:
        conn.close()

    elo_table = build_calibration_table(elo_pairs, n_bins=10)
    s5_table = build_calibration_table(s5_pairs, n_bins=10)
    s1_table = build_calibration_table(s1_pairs, n_bins=10)
    h2h_table = build_calibration_table(h2h_pairs, n_bins=10)

    print_table(elo_table, "ELO gap (real Dataset B)")
    print_table(s5_table, "S5 dominance_score (real Dataset B)")
    print_table(s1_table, "S1 surface-form gap pp (real Dataset B)")
    print_table(h2h_table, "H2H pct diff, -1..1 scale (real Dataset B)")

    print("\nH2H note: this table is DERIVED directly from real match history (who beat whom,")
    print("how many times, before this match) -- it does NOT use the h2h_at_match field, so")
    print("the earlier 'W' ordering ambiguity doesn't apply here.")

    # Save real tables for reuse (e.g. wiring into confidence_engine.py later)
    out = {
        "elo": [{"low": b.low, "high": b.high, "n": b.n, "win_rate": b.win_rate} for b in elo_table],
        "s5": [{"low": b.low, "high": b.high, "n": b.n, "win_rate": b.win_rate} for b in s5_table],
        "s1": [{"low": b.low, "high": b.high, "n": b.n, "win_rate": b.win_rate} for b in s1_table],
        "h2h": [{"low": b.low, "high": b.high, "n": b.n, "win_rate": b.win_rate} for b in h2h_table],
    }
    with open("real_calibration_tables.json", "w") as f:
        json.dump(out, f, indent=2)
    print("\nSaved real tables to real_calibration_tables.json")


if __name__ == "__main__":
    main()
