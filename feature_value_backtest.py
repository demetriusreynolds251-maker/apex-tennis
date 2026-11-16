#!/usr/bin/env python3
"""
FEATURE_VALUE_BACKTEST.PY — Phase C candidate-field evaluation
Real backtest, not a guess: tests whether the fields captured in the
2026-07-27 Phase A/B work carry genuine PREDICTIVE signal, and
compares them against the existing 5 Tier-1 fields (hold_pct,
break_pct, avg_aces, first_serve_pct, bp_save_pct) as a baseline, so
we know whether a candidate adds real lift or is redundant.

METHODOLOGY (important -- this is what makes it a fair test):
Per-match stats (unforced errors, winners, etc.) are only known AFTER
a match is played -- they cannot be inputs to predicting that same
match. So for every match, each player's feature value is their
ROLLING AVERAGE over their own PRIOR matches only (strict date
ordering, no lookahead). The signal tested is the DIFFERENTIAL between
the two players' rolling averages -- exactly how a real pre-match
feature would be used.

THREE TARGETS TESTED (per your instruction to test all three):
  1. Win/loss (did the player with the higher rolling-avg value win?)
  2. Total games in the match
  3. Game-margin dominance (winner's games minus loser's games)

USAGE:
    python3 feature_value_backtest.py

Reads DB_PATH below -- adjust if your warehouse lives elsewhere.
Prints a real results table, no LLM involved -- pure arithmetic over
your actual match history.
"""
import sqlite3
import math
from pathlib import Path
from collections import defaultdict

DB_PATH = Path(__file__).resolve().parent / "data" / "warehouse" / "tennis_warehouse.sqlite"
ROLLING_WINDOW = 8   # how many prior matches to average over per player
MIN_PRIOR_MATCHES = 3  # skip a match if either player has fewer than this many prior matches on file

# Candidate fields from the Phase A capture-gap fix, plus the existing
# 5 Tier-1 fields as the baseline comparison group.
CANDIDATE_FIELDS = {
    "unforced_errors":        ("player_a_unforced_errors", "player_b_unforced_errors"),
    "winners":                ("player_a_winners", "player_b_winners"),
    "break_points_converted": ("player_a_break_points_converted", "player_b_break_points_converted"),
    "winning_first_serve":    ("player_a_winning_first_serve", "player_b_winning_first_serve"),
    "winning_second_serve":   ("player_a_winning_second_serve", "player_b_winning_second_serve"),
    "fastest_serve":          ("player_a_fastest_serve", "player_b_fastest_serve"),
}
# Existing Tier-1 fields live in player_year_stats (year-level, not
# per-match) -- pulled separately below for the baseline comparison.
BASELINE_FIELDS = ["hold_pct", "break_pct", "avg_aces", "first_serve_pct", "bp_save_pct"]


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx == 0 or sy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sx * sy)


def auc_from_diffs(diffs, wins):
    """Rank-based AUC (Mann-Whitney U formulation) -- does a higher
    rolling-avg differential actually predict the win, more often than
    chance? 0.5 = no signal, 1.0 = perfect signal, <0.5 = inverted."""
    pos = [d for d, w in zip(diffs, wins) if w == 1]
    neg = [d for d, w in zip(diffs, wins) if w == 0]
    if not pos or not neg:
        return None
    wins_count = 0
    for p in pos:
        for ng in neg:
            if p > ng:
                wins_count += 1
            elif p == ng:
                wins_count += 0.5
    return wins_count / (len(pos) * len(neg))


def parse_h2h_pct(h2h_str, is_a):
    """h2h_at_match is stored as 'W-L' -- CAVEAT: which side W refers to
    (player_a or player_b) was not independently re-verified against
    known real head-to-heads before this script was written; sanity
    check a couple of real rows against a head-to-head you know by
    hand before trusting the sign of this result."""
    if not h2h_str or "-" not in h2h_str:
        return None
    try:
        w, l = h2h_str.split("-")
        w, l = int(w), int(l)
    except ValueError:
        return None
    if w + l == 0:
        return None
    pct = w / (w + l)
    return pct if is_a else (1 - pct)


def parse_odd(odd_str):
    if not odd_str:
        return None
    try:
        return float(odd_str)
    except ValueError:
        return None


def build_rolling_features(conn):
    """Walks all matches in date order, maintaining a per-player
    rolling history of each candidate field, and yields one row per
    match with the pre-match rolling-average DIFFERENTIAL for each
    field -- the actual feature a real prediction would have access to.

    v2 (2026-07-28): added three predictors that were missing from the
    first pass -- h2h_at_match, a real rolling GAME-MARGIN feature
    (distinct from total_games, which was only ever tested as a
    TARGET, never as an input), and historical odds -- closing a real
    gap flagged after the first backtest was reviewed."""
    cols = ["match_id", "match_date", "player_a", "player_b", "winner",
            "total_games", "player_a_games", "player_b_games",
            "h2h_at_match", "player_a_odd", "player_b_odd"]
    for field, (a_col, b_col) in CANDIDATE_FIELDS.items():
        cols += [a_col, b_col]
    query = f"SELECT {', '.join(cols)} FROM matches WHERE match_date IS NOT NULL ORDER BY match_date ASC"
    rows = conn.execute(query).fetchall()

    history = defaultdict(lambda: defaultdict(list))  # history[player][field] = [values...]
    margin_history = defaultdict(list)  # margin_history[player] = [signed game margins from their own matches]
    results = []

    for row in rows:
        r = dict(zip(cols, row))
        pa, pb, winner = r["player_a"], r["player_b"], r["winner"]
        if not winner or winner not in (pa, pb):
            continue  # unconfirmed winner -- skip, no target to test against

        margin = None
        if r["player_a_games"] is not None and r["player_b_games"] is not None:
            margin = r["player_a_games"] - r["player_b_games"]

        record = {
            "match_date": r["match_date"], "player_a": pa, "player_b": pb,
            "won_a": 1 if winner == pa else 0,
            "total_games": r["total_games"],
            "margin": margin,
        }

        # h2h and odds are pre-match-known values, not rolling averages
        # -- used directly, no history needed.
        h2h_a = parse_h2h_pct(r["h2h_at_match"], is_a=True)
        if h2h_a is not None:
            record["h2h_pct_diff"] = (2 * h2h_a - 1)  # rescale 0..1 -> -1..1, symmetric around "even" like other diffs

        odd_a, odd_b = parse_odd(r["player_a_odd"]), parse_odd(r["player_b_odd"])
        if odd_a and odd_b:
            record["odds_implied_prob_diff"] = (1 / odd_a) - (1 / odd_b)

        # rolling GAME-MARGIN feature: each player's own historical
        # average signed game-margin (their games minus opponent games,
        # from THEIR perspective across their own past matches) -- this
        # is different from total_games (which was only ever a TARGET
        # before, never an input) and different from h2h (opponent-
        # specific rather than a general dominance trend).
        hist_margin_a = margin_history[pa]
        hist_margin_b = margin_history[pb]
        if len(hist_margin_a) >= MIN_PRIOR_MATCHES and len(hist_margin_b) >= MIN_PRIOR_MATCHES:
            avg_margin_a = sum(hist_margin_a[-ROLLING_WINDOW:]) / len(hist_margin_a[-ROLLING_WINDOW:])
            avg_margin_b = sum(hist_margin_b[-ROLLING_WINDOW:]) / len(hist_margin_b[-ROLLING_WINDOW:])
            record["rolling_game_margin_diff"] = avg_margin_a - avg_margin_b
        if margin is not None:
            margin_history[pa].append(margin)
            margin_history[pb].append(-margin)  # from player_b's own perspective, sign flips

        for field, (a_col, b_col) in CANDIDATE_FIELDS.items():
            hist_a = history[pa][field]
            hist_b = history[pb][field]
            if len(hist_a) >= MIN_PRIOR_MATCHES and len(hist_b) >= MIN_PRIOR_MATCHES:
                avg_a = sum(hist_a[-ROLLING_WINDOW:]) / len(hist_a[-ROLLING_WINDOW:])
                avg_b = sum(hist_b[-ROLLING_WINDOW:]) / len(hist_b[-ROLLING_WINDOW:])
                record[f"{field}_diff"] = avg_a - avg_b

            val_a, val_b = r[a_col], r[b_col]
            if val_a is not None:
                hist_a.append(val_a)
            if val_b is not None:
                hist_b.append(val_b)

        results.append(record)

    return results


def build_baseline_features(conn, match_records):
    """Same rolling-differential idea, but for the existing 5 Tier-1
    fields, pulled from player_year_stats (year-level, most recent
    year at or before the match's year)."""
    stats_by_player_year = {}
    for row in conn.execute(
        "SELECT player, year, hold_pct, break_pct, avg_aces, first_serve_pct, bp_save_pct "
        "FROM player_year_stats"):
        player, year = row[0], row[1]
        stats_by_player_year[(player, year)] = dict(
            zip(BASELINE_FIELDS, row[2:]))

    for rec in match_records:
        year = int(rec["match_date"][:4])
        a_stats = stats_by_player_year.get((rec["player_a"], year)) or stats_by_player_year.get((rec["player_a"], year - 1))
        b_stats = stats_by_player_year.get((rec["player_b"], year)) or stats_by_player_year.get((rec["player_b"], year - 1))
        for field in BASELINE_FIELDS:
            if a_stats and b_stats and a_stats.get(field) is not None and b_stats.get(field) is not None:
                rec[f"baseline_{field}_diff"] = a_stats[field] - b_stats[field]
    return match_records


def run():
    if not DB_PATH.exists():
        print(f"FATAL: database not found at {DB_PATH} -- adjust DB_PATH at the top of this script.")
        return
    conn = sqlite3.connect(str(DB_PATH))
    print(f"Loading matches from {DB_PATH} ...")
    records = build_rolling_features(conn)
    records = build_baseline_features(conn, records)
    conn.close()

    print(f"\n{len(records)} matches usable after requiring {MIN_PRIOR_MATCHES}+ prior matches per player "
          f"(rolling window = last {ROLLING_WINDOW} matches).\n")

    all_fields = (["h2h_pct_diff", "rolling_game_margin_diff"]
                  + [f"{f}_diff" for f in CANDIDATE_FIELDS]
                  + [f"baseline_{f}_diff" for f in BASELINE_FIELDS]
                  + ["odds_implied_prob_diff"])

    header = f"{'FIELD':<32} {'N':>6} {'WIN AUC':>9} {'r vs GAMES':>12} {'r vs MARGIN':>12}"
    print(header)
    print("-" * len(header))

    for field in all_fields:
        subset = [r for r in records if field in r and r[field] is not None]
        n = len(subset)
        if n < 20:
            print(f"{field:<32} {n:>6}   (insufficient sample)")
            continue

        diffs = [r[field] for r in subset]
        wins  = [r["won_a"] for r in subset]
        auc = auc_from_diffs(diffs, wins)

        games_subset = [(r[field], r["total_games"]) for r in subset if r["total_games"] is not None]
        r_games = pearson([d for d, g in games_subset], [g for d, g in games_subset]) if len(games_subset) >= 20 else None

        margin_subset = [(r[field], r["margin"]) for r in subset if r["margin"] is not None]
        r_margin = pearson([d for d, m in margin_subset], [m for d, m in margin_subset]) if len(margin_subset) >= 20 else None

        auc_str = f"{auc:.3f}" if auc is not None else "n/a"
        rg_str = f"{r_games:+.3f}" if r_games is not None else "n/a"
        rm_str = f"{r_margin:+.3f}" if r_margin is not None else "n/a"
        print(f"{field:<32} {n:>6} {auc_str:>9} {rg_str:>12} {rm_str:>12}")

    print("\nReading the output:")
    print("  WIN AUC:     0.50 = no predictive signal, 1.00 = perfect. Compare each")
    print("               candidate field's AUC against the baseline_* fields' AUC --")
    print("               a candidate only earns a place in reasoning if it BEATS or")
    print("               meaningfully ADDS to what the baseline fields already predict.")
    print("  r vs GAMES:  Pearson correlation between the stat differential and total")
    print("               games in the match (relevant to Total Games props).")
    print("  r vs MARGIN: Pearson correlation between the stat differential and the")
    print("               signed game-margin (player_a games minus player_b games) --")
    print("               tests real dominance-margin predictive value (Steps 5-8).")
    print()
    print("NOTE on odds_implied_prob_diff: tested here for a COMPLETE empirical record")
    print("only. Regardless of how strong its signal comes back, it is NOT eligible to")
    print("become a live evaluation rule -- Rule 9 permanently blocks market/odds data")
    print("from entering prediction reasoning. A strong result here would only be useful")
    print("for a separate, different purpose (e.g. checking Apex's own picks against")
    print("closing-line value after the fact), never as a reasoning input.")
    print()
    print("NOTE on h2h_pct_diff: verify the win/loss ordering in h2h_at_match against a")
    print("real head-to-head you know by hand before trusting the SIGN of this result --")
    print("that ordering was not independently re-confirmed when this script was written.")


if __name__ == "__main__":
    run()
