"""
elo_calculator.py
2026-08-07 — real ELO rating engine for Apex, scoped this session.

Standard ELO, applied to real historical tennis match results. Two
parallel ratings are tracked per player:
  - overall ELO   (every real match counted)
  - surface ELO   (separate running rating per surface: hard/clay/grass)

Deliberately kept surface ratings SEPARATE from overall rather than
blended — a player's skill genuinely varies by surface, and mixing
them would blur exactly the signal this is meant to add. Same
"don't collapse real information into one number" principle already
applied to S5 (dominance_score kept separate from the LOPSIDED/
COMPETITIVE label).

This module is pure computation — no DB reads/writes, no Apex
integration. That layer needs the real `matches` table schema from
Dataset B (tennis_warehouse.sqlite), which hasn't been confirmed
yet this session — see build_elo_history() below for exactly what's
blocked and why.

Nothing here touches live evaluation reasoning. Per the Phase C
precedent on this project, ELO does not get wired in as a real
signal (S6) until it clears a real backtest against actual settled
outcomes via feature_value_backtest.py — same bar H2H (S3) had to
clear, same bar unforced errors/serve splits/etc. failed to clear.
"""

from dataclasses import dataclass, field
from typing import Optional


DEFAULT_RATING = 1500.0
K_FACTOR = 32  # standard chess/ELO default; provisional, see note below

# PROVISIONAL: K_FACTOR controls how much a single result moves a
# rating. 32 is the standard default (used by FIDE for most players,
# and by most sports ELO implementations as a starting point). Tennis
# has more matches per player per year than chess does per rating
# period, so a lower K (16-24) may track true skill more smoothly
# without overreacting to a single upset. This should be tuned
# empirically against the real backtest results, not guessed — flag
# for revisit once build_elo_history() runs against real data.

SURFACES = {"hard", "clay", "grass"}


@dataclass
class PlayerRating:
    """Real running rating state for one player. `matches_played`
    is tracked because a rating built on 3 real matches carries far
    less confidence than one built on 300 — this needs to feed into
    the eventual S6 signal's None-vs-0 distinction, same as
    ML_S3_H2H_MIN_SAMPLE gates a thin H2H sample."""
    overall: float = DEFAULT_RATING
    overall_matches: int = 0
    surface: dict = field(default_factory=lambda: {s: DEFAULT_RATING for s in SURFACES})
    surface_matches: dict = field(default_factory=lambda: {s: 0 for s in SURFACES})


def expected_score(rating_a: float, rating_b: float) -> float:
    """Standard ELO expected-score formula: the probability rating_a
    is predicted to beat rating_b, given only the two ratings."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def update_pair(winner_rating: float, loser_rating: float, k: float = K_FACTOR) -> tuple:
    """Real post-match rating update for both players. Returns
    (new_winner_rating, new_loser_rating). The winner ALWAYS scored
    1, the loser ALWAYS scored 0 — actual/expected is what moves the
    rating, so a heavy favorite winning barely moves either rating,
    while an upset moves both a lot. This is the entire mechanism —
    no separate "upset bonus" needed, it falls out of the formula."""
    exp_winner = expected_score(winner_rating, loser_rating)
    exp_loser = 1.0 - exp_winner
    new_winner = winner_rating + k * (1.0 - exp_winner)
    new_loser = loser_rating + k * (0.0 - exp_loser)
    return new_winner, new_loser


def _normalize_surface(raw_surface: Optional[str]) -> Optional[str]:
    """Real surfaces vary in casing/naming across sources
    ('Hard', 'hard court', 'Clay', 'carpet'). Returns a normalized
    key from SURFACES, or None if unrecognized — an unrecognized
    surface updates overall ELO only, never guessed into a bucket."""
    if not raw_surface:
        return None
    s = raw_surface.strip().lower()
    if "hard" in s:
        return "hard"
    if "clay" in s:
        return "clay"
    if "grass" in s:
        return "grass"
    return None  # e.g. "carpet" — real surface, but not one of the three we track


def apply_match(ratings: dict, winner: str, loser: str, surface_raw: Optional[str],
                 k: float = K_FACTOR) -> None:
    """Mutates `ratings` (a dict of name -> PlayerRating) in place
    for one real match result. Both players get created with
    DEFAULT_RATING on first appearance. Always updates overall;
    updates surface ELO only if the surface normalizes to a known
    value — an unrecognized surface still counts toward overall
    skill, it just can't be attributed to a specific surface bucket."""
    w = ratings.setdefault(winner, PlayerRating())
    l = ratings.setdefault(loser, PlayerRating())

    new_w_overall, new_l_overall = update_pair(w.overall, l.overall, k)
    w.overall, l.overall = new_w_overall, new_l_overall
    w.overall_matches += 1
    l.overall_matches += 1

    surface = _normalize_surface(surface_raw)
    if surface:
        new_w_surf, new_l_surf = update_pair(w.surface[surface], l.surface[surface], k)
        w.surface[surface], l.surface[surface] = new_w_surf, new_l_surf
        w.surface_matches[surface] += 1
        l.surface_matches[surface] += 1


def build_elo_history(match_rows, k: float = K_FACTOR) -> dict:
    """
    Replays real match rows IN CHRONOLOGICAL ORDER and returns the
    final {name: PlayerRating} state. This is the real entry point
    once match data is available in the expected shape.

    match_rows: iterable of dicts, each with real keys:
        winner_name (str), loser_name (str), surface (str or None),
        match_date (str or date, used ONLY for sort order upstream —
        this function assumes match_rows is ALREADY sorted oldest-
        first; it does not sort internally, since date parsing
        belongs with whatever reads the real source, not duplicated
        here)

    BLOCKED as of 2026-08-07: the real column names for Dataset B's
    `matches` table (tennis_warehouse.sqlite) haven't been confirmed
    against actual source this session — tennis_warehouse_schema.sql
    and warehouse_ops.py weren't available to read. Per this
    project's standing rule (read actual source before proposing
    integration points), the real DB-reading wrapper that queries
    `matches` and feeds rows into this function is NOT YET WRITTEN.
    This function itself is schema-agnostic (plain dicts in, plain
    dict out) so it doesn't need to change once that wrapper exists
    — only the wrapper needs the real column names.
    """
    ratings: dict = {}
    for row in match_rows:
        winner = row.get("winner_name")
        loser = row.get("loser_name")
        if not winner or not loser:
            continue  # real row, but missing an identity — skip rather than guess
        apply_match(ratings, winner, loser, row.get("surface"), k=k)
    return ratings


def elo_gap(ratings: dict, player_name: str, opponent_name: str,
            surface_raw: Optional[str] = None) -> Optional[dict]:
    """
    Real, current gap between two players' ratings — this is the
    raw material signal_s6_elo() (not yet built) would consume, same
    relationship signal_s3_h2h() has to v8_get_h2h()'s raw output.

    Returns None if either player has zero real matches on record
    (a DEFAULT_RATING with 0 matches carries no real information —
    same "None vs 0" discipline as every other signal in this file).

    Returns a dict with both the surface-specific gap (if the
    surface normalizes AND both players have surface-specific
    matches) and the overall gap as a fallback — the caller decides
    which to prefer, this function doesn't collapse them into one
    number.
    """
    p = ratings.get(player_name)
    o = ratings.get(opponent_name)
    if p is None or o is None or p.overall_matches == 0 or o.overall_matches == 0:
        return None

    result = {
        "overall_gap": p.overall - o.overall,
        "overall_matches": (p.overall_matches, o.overall_matches),
        "surface_gap": None,
        "surface_matches": None,
        "surface": None,
    }

    surface = _normalize_surface(surface_raw)
    if surface and p.surface_matches[surface] > 0 and o.surface_matches[surface] > 0:
        result["surface_gap"] = p.surface[surface] - o.surface[surface]
        result["surface_matches"] = (p.surface_matches[surface], o.surface_matches[surface])
        result["surface"] = surface

    return result
