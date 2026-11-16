"""
build_signal_calibration.py
2026-08-11 — real calibration-table builder for the redesigned
confidence engine. This is the piece that replaces arbitrary
thresholds ("magnitude >= 8 means something") with real, historically
-earned probabilities ("in the real matches where this signal read
this value, the favored side actually won X% of the time").

Real design: this does NOT assume any formula shape (no logistic
curve, no fitted model). It bins each signal's real (raw_value, label)
pairs -- the SAME pairs each signal's own backtest script already
produces (elo_backtest.py, s1_backtest.py, s5_backtest.py, the H2H
test) -- into quantile buckets, and reports the REAL historical win
rate inside each bucket. This is the same real concept as
calibration.py's reliability_buckets(), applied to a raw signal value
instead of an already-computed probability.

Real, honest design choice: quantile bins (not fixed-width) so each
signal's table adapts to its own real value distribution -- an ELO
gap ranges roughly -400 to 400, S5's dominance score ranges roughly
-25 to 25, they should not share bin boundaries.

Buckets with fewer than MIN_SAMPLES_PER_BUCKET real settled examples
report win_rate=None rather than a fake-precise number -- same
"NOT ENOUGH DATA over a guess" discipline used everywhere else this
session.
"""
from dataclasses import dataclass, field

MIN_SAMPLES_PER_BUCKET = 30  # same real floor used throughout tonight


@dataclass
class CalibrationBucket:
    low: float
    high: float
    n: int
    win_rate: float = None  # None if n < MIN_SAMPLES_PER_BUCKET


def build_calibration_table(pairs, n_bins: int = 10):
    """
    pairs: list of (raw_signal_value, label) -- label is 1 if the side
    the signal favored (raw_value > 0, say) actually won, 0 otherwise.
    These are the SAME real (score, label) pairs each backtest script
    already generates walk-forward, no lookahead -- this function does
    not compute them, it only bins ones already computed.

    Returns a real, sorted list of CalibrationBucket objects covering
    the full observed range. Quantile-based: each bucket gets
    approximately n_total/n_bins real examples, so bucket WIDTH varies
    but bucket COUNT is roughly even -- this means every bucket's
    reported win_rate rests on a comparable, real sample size.
    """
    if not pairs:
        return []

    sorted_pairs = sorted(pairs, key=lambda p: p[0])
    n_total = len(sorted_pairs)
    real_n_bins = min(n_bins, max(1, n_total // MIN_SAMPLES_PER_BUCKET))
    if real_n_bins == 0:
        real_n_bins = 1

    bucket_size = n_total / real_n_bins
    buckets = []
    for i in range(real_n_bins):
        start = int(round(i * bucket_size))
        end = int(round((i + 1) * bucket_size)) if i < real_n_bins - 1 else n_total
        chunk = sorted_pairs[start:end]
        if not chunk:
            continue
        low = chunk[0][0]
        high = chunk[-1][0]
        n = len(chunk)
        win_rate = (sum(label for _, label in chunk) / n) if n >= MIN_SAMPLES_PER_BUCKET else None
        buckets.append(CalibrationBucket(low=low, high=high, n=n, win_rate=win_rate))

    return buckets


def calibrated_probability(raw_value, calibration_table):
    """
    Real lookup: finds which real bucket raw_value falls into, returns
    that bucket's real historical win rate. At the edges (a raw_value
    beyond the lowest/highest bucket ever observed), uses the nearest
    real bucket rather than extrapolating a guess.

    Returns None if the table is empty or the matching bucket has
    insufficient real samples -- never fabricates a probability.
    """
    if not calibration_table:
        return None

    for bucket in calibration_table:
        if bucket.low <= raw_value <= bucket.high:
            return bucket.win_rate

    # raw_value outside every real observed bucket -- use the nearest
    # real edge bucket rather than guessing an extrapolation
    if raw_value < calibration_table[0].low:
        return calibration_table[0].win_rate
    if raw_value > calibration_table[-1].high:
        return calibration_table[-1].win_rate
    return None


def print_table(calibration_table, label=""):
    print(f"\nCalibration table: {label}")
    print(f"{'RANGE':>20} {'N':>8} {'REAL WIN RATE':>15}")
    for b in calibration_table:
        rate_str = f"{b.win_rate:.1%}" if b.win_rate is not None else "NOT ENOUGH DATA"
        print(f"{b.low:>9.2f} to {b.high:>7.2f} {b.n:>8} {rate_str:>15}")
