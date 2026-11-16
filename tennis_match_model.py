"""
tennis_match_model.py

Deterministic tennis match simulation engine.

Purpose (per OpenClaw_Reasoning_Architecture_Final_Handoff.md, §6):
Everything downstream of a small set of true primitives (hold %, break %,
per-service-game rates) should be COMPUTED, not estimated by an LLM.
Expected games, expected sets, and match win probability follow exactly
from hold percentages via a standard Markov-chain tennis model — there is
no reasoning step here, only arithmetic.

This module owns exactly that derivation. It takes primitives (the only
things that should ever require LLM/data estimation, each with its own
uncertainty) and returns a MatchModel: the structured, deterministic
projection that the Prediction stage reads from.

Nothing in this file touches betting lines, MORE/LESS, or recommendations.
That split is intentional and load-bearing — see the handoff doc §0/§1.
"""

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional


# --------------------------------------------------------------------------
# Primitives: the only inputs that should ever come from LLM/data estimation.
# Everything else in this file is derived from these by exact computation.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PlayerPrimitives:
    """Per-player rates. Each should carry real uncertainty upstream —
    this dataclass just holds the point estimate used for simulation."""
    name: str
    hold_pct: float          # P(player wins own service game), 0-1
    ace_rate: float = 0.0    # expected aces per service game
    df_rate: float = 0.0     # expected double faults per service game

    def __post_init__(self):
        if not (0.0 <= self.hold_pct <= 1.0):
            raise ValueError(f"{self.name}: hold_pct must be in [0,1], got {self.hold_pct}")


@dataclass(frozen=True)
class MatchFormat:
    best_of: int = 3          # 3 or 5
    tiebreak_at: int = 6      # games-all score that triggers a tiebreak

    def sets_to_win(self) -> int:
        return 2 if self.best_of == 3 else 3


# --------------------------------------------------------------------------
# Tiebreak win probability.
# We don't have point-by-point serve data, only hold%, so this is a stated
# approximation: a tiebreak is treated as an alternating-serve sequence
# where each player's per-point serve-win probability is backed out from
# their hold% via the standard game-win-probability inversion. This is a
# real approximation, not a measurement — it should be replaced with true
# point-level data the moment that's available, and its own accuracy
# should be backtested independently once real tiebreak outcomes exist
# in Dataset A/B.
# --------------------------------------------------------------------------

def _point_win_prob_from_hold(hold_pct: float) -> float:
    """Invert the standard game-win formula to back out a per-point serve
    win probability p from a service-game hold probability.
    P(win game | p) = p^4 * (15 - 4p - 10p^2 + 20p^3 - 10p^4) / (1 - 2p(1-p))
      + [deuce term]. Rather than invert that closed form analytically
    (it isn't cleanly invertible), we solve it numerically via bisection --
    exact to float precision, no approximation beyond the model itself.
    """
    def game_win_prob(p: float) -> float:
        if p <= 0.0:
            return 0.0
        if p >= 1.0:
            return 1.0
        q = 1 - p
        # Win outright without reaching deuce (40-0, 40-15, 40-30 paths)
        win_before_deuce = p**4 + 4 * p**4 * q + 10 * p**4 * q**2
        # Probability of reaching deuce (3-3 in points)
        reach_deuce = 20 * (p**3) * (q**3)
        # From deuce, probability of eventually winning the game
        # (must win by 2): standard result = p^2 / (p^2 + q^2)
        win_from_deuce = (p**2) / (p**2 + q**2) if (p**2 + q**2) > 0 else 0.5
        return win_before_deuce + reach_deuce * win_from_deuce

    lo, hi = 0.0, 1.0
    for _ in range(60):  # bisection converges to well past float precision
        mid = (lo + hi) / 2
        if game_win_prob(mid) < hold_pct:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def tiebreak_win_prob(server_point_p: float, returner_point_p: float,
                       max_points_cap: int = 80) -> float:
    """P(player serving first wins a 7-point tiebreak), given each player's
    per-point win probability on their own serve. Uses exact recursive
    tiebreak scoring (first to 7, win by 2), serve alternating 1-2-2-2...

    Implemented as BOTTOM-UP iterative DP (not top-down recursion): a deep,
    evenly-matched tiebreak can require recursion depth far beyond Python's
    default limit (a genuine bug in an earlier version of this function --
    memoization does not bound recursion depth, only redundant computation).
    Processing states in decreasing order of total points played removes
    the recursion entirely. States beyond max_points_cap total points are
    given a neutral fallback (0.5); at any realistic point-win probability
    the mass reaching that many points is astronomically small (a fair
    tiebreak reaching 80-all has probability on the order of 1e-24), so the
    cap introduces no meaningful error.
    """
    def server_is_a_for_point(point_no: int) -> bool:
        # Standard tiebreak serve rotation: point 1 = A, points 2-3 = B,
        # points 4-5 = A, points 6-7 = B, ... (serve switches every 2
        # points after the first).
        if point_no == 1:
            return True
        block = (point_no - 2) // 2
        return (block % 2 == 1)

    # prob[a][b] = P(A wins the tiebreak from score a-b, A to have the
    # serve situation implied by point number a+b+1)
    M = max_points_cap
    prob = [[0.5] * (M + 2) for _ in range(M + 2)]

    for n in range(2 * M, -1, -1):  # n = a + b, process high to low
        for a in range(max(0, n - M), min(n, M) + 1):
            b = n - a
            if a > M or b > M:
                continue
            if a >= 7 and a - b >= 2:
                prob[a][b] = 1.0
                continue
            if b >= 7 and b - a >= 2:
                prob[a][b] = 0.0
                continue
            if a == M or b == M:
                continue  # leave at neutral fallback -- negligible mass
            point_no = a + b + 1
            a_serves = server_is_a_for_point(point_no)
            p = server_point_p if a_serves else (1 - returner_point_p)
            prob[a][b] = p * prob[a + 1][b] + (1 - p) * prob[a][b + 1]

    return prob[0][0]


# --------------------------------------------------------------------------
# Set-level Markov chain: exact given hold percentages.
# --------------------------------------------------------------------------

def _set_distribution(hold_a: float, hold_b: float, a_serves_first: bool,
                       tiebreak_at: int = 6):
    """Returns dict: {(games_a, games_b): probability} over all terminal
    set scores, plus overall P(A wins set) and E[games in set].
    Exact dynamic program over game scores; games alternate serve."""

    tb_p = tiebreak_win_prob(
        _point_win_prob_from_hold(hold_a if a_serves_first else hold_b),
        _point_win_prob_from_hold(hold_b if a_serves_first else hold_a),
    )

    @lru_cache(maxsize=None)
    def set_state(ga: int, gb: int, a_serves: bool) -> dict:
        """Returns {(final_ga, final_gb): prob} from this state onward."""
        # Terminal: someone already won the set
        if ga >= tiebreak_at and ga - gb >= 2:
            return {(ga, gb): 1.0}
        if gb >= tiebreak_at and gb - ga >= 2:
            return {(ga, gb): 1.0}
        if ga == tiebreak_at and gb == tiebreak_at:
            # tiebreak game decides it; whoever "a_serves" flag says serves first
            p_a_wins_tb = tb_p if a_serves else (1 - tb_p)
            return {(ga + 1, gb): p_a_wins_tb, (ga, gb + 1): 1 - p_a_wins_tb}

        p_hold = hold_a if a_serves else hold_b
        outcomes = {}
        # Player currently serving holds -> their game count +1
        for won, p in ((a_serves, p_hold), (not a_serves, 1 - p_hold)):
            if p <= 0:
                continue
            nga, ngb = (ga + 1, gb) if won else (ga, gb + 1)
            sub = set_state(nga, ngb, not a_serves)
            for k, v in sub.items():
                outcomes[k] = outcomes.get(k, 0.0) + p * v
        return outcomes

    dist = set_state(0, 0, a_serves_first)
    set_state.cache_clear()

    p_a_wins_set = sum(p for (ga, gb), p in dist.items() if ga > gb)
    expected_games = sum((ga + gb) * p for (ga, gb), p in dist.items())
    return dist, p_a_wins_set, expected_games


# --------------------------------------------------------------------------
# Match-level combination: best-of-3 or best-of-5, from set-level results.
# --------------------------------------------------------------------------

@dataclass
class MatchModel:
    """The structured, deterministic projection. This is what the
    Prediction stage reads from -- nothing here mentions a betting line."""
    player_a: str
    player_b: str
    win_prob_a: float
    win_prob_b: float
    expected_games_total: float
    expected_sets_total: float
    expected_aces_a: Optional[float] = None
    expected_aces_b: Optional[float] = None
    expected_dfs_a: Optional[float] = None
    expected_dfs_b: Optional[float] = None
    notes: list = field(default_factory=list)


def simulate_match(a: PlayerPrimitives, b: PlayerPrimitives,
                    fmt: MatchFormat = MatchFormat(),
                    a_serves_first: bool = True) -> MatchModel:
    """Exact (given the primitives) derivation of win probability, expected
    games, and expected sets. This function contains zero judgment calls --
    every number here is fully determined by hold_a, hold_b, and the format."""

    set_dist, p_a_wins_set, exp_games_per_set = _set_distribution(
        a.hold_pct, b.hold_pct, a_serves_first, fmt.tiebreak_at
    )
    p_b_wins_set = 1 - p_a_wins_set

    sets_needed = fmt.sets_to_win()
    max_sets = fmt.best_of

    # Best-of-N match win probability: negative-binomial-style recursion
    # over sets won, exact given a single per-set win probability. (Serve
    # alternates who serves first each set in reality; using a fixed
    # p_a_wins_set per set is the stated simplification here -- accurate
    # to first order, and a clean place to extend later by alternating
    # a_serves_first per set if warranted.)
    @lru_cache(maxsize=None)
    def match_state(sa: int, sb: int) -> float:
        if sa == sets_needed:
            return 1.0
        if sb == sets_needed:
            return 0.0
        return p_a_wins_set * match_state(sa + 1, sb) + p_b_wins_set * match_state(sa, sb + 1)

    p_a_wins_match = match_state(0, 0)
    match_state.cache_clear()

    # Expected number of sets played: sum over all (sa, sb) terminal states
    @lru_cache(maxsize=None)
    def sets_played_dist(sa: int, sb: int) -> dict:
        if sa == sets_needed or sb == sets_needed:
            return {sa + sb: 1.0}
        out = {}
        for won, p in ((True, p_a_wins_set), (False, p_b_wins_set)):
            nsa, nsb = (sa + 1, sb) if won else (sa, sb + 1)
            for k, v in sets_played_dist(nsa, nsb).items():
                out[k] = out.get(k, 0.0) + p * v
        return out

    sets_dist = sets_played_dist(0, 0)
    sets_played_dist.cache_clear()
    expected_sets = sum(n * p for n, p in sets_dist.items())
    expected_games_total = expected_sets * exp_games_per_set

    model = MatchModel(
        player_a=a.name, player_b=b.name,
        win_prob_a=p_a_wins_match, win_prob_b=1 - p_a_wins_match,
        expected_games_total=expected_games_total,
        expected_sets_total=expected_sets,
    )

    # Simple derived counts: aces/DFs per service game x expected service
    # games. Expected service games for a player ~ expected_games_total / 2
    # (each player serves roughly half the games) -- a stated approximation,
    # not exact, since holds/breaks change who serves how often relative to
    # total games. Flagged in notes rather than silently treated as exact.
    if a.ace_rate or b.ace_rate:
        exp_service_games_each = expected_games_total / 2
        model.expected_aces_a = a.ace_rate * exp_service_games_each
        model.expected_aces_b = b.ace_rate * exp_service_games_each
        model.notes.append(
            "expected_aces_* assumes each player serves ~half of total games; "
            "a real break-adjusted split would use each player's actual "
            "service-game share, not a flat 50/50."
        )
    if a.df_rate or b.df_rate:
        exp_service_games_each = expected_games_total / 2
        model.expected_dfs_a = a.df_rate * exp_service_games_each
        model.expected_dfs_b = b.df_rate * exp_service_games_each

    model.notes.append(
        f"tiebreak model: point-win probabilities backed out from hold% via "
        f"bisection inversion of the standard game-win formula -- an approximation "
        f"until real point-level serve data is available for validation."
    )
    model.notes.append(
        "per-set win probability held constant across all sets in this match "
        "(does not vary who serves first each set) -- a stated simplification."
    )

    return model
