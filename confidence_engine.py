"""
confidence_engine.py
2026-08-11 — the real, redesigned confidence engine. Replaces the old
two-stage system (a hard Phase 1 magnitude cliff that could block a
play entirely, plus a Phase 2 layer that could only adjust an existing
play) with one real, continuous blend.

Real design, agreed with the user before building:
  1. ELO's own real win-probability formula as the real foundation
     (reused directly from elo_calculator.py's expected_score(), the
     same real hand-verified formula built and tested earlier tonight
     -- not reinvented here).
  2. S5/S1/H2H blended in as real corrections, each converted to a
     real probability via its own historically-earned calibration
     table (build_signal_calibration.py), not an arbitrary threshold.
  3. Weights are proportional to each signal's REAL tested AUC margin
     above a coin flip (0.50) -- ELO's 0.7223 (.2223 margin) carries
     roughly 2.5x the weight of H2H's 0.584 (.084 margin). These are
     the actual numbers from tonight's real signal audit, not guesses.
  4. NO hard cliff. Every match gets a real blended probability,
     always -- a near-50% match reports a near-50% number, it does
     not silently vanish into a blank NO PLAY.
  5. A "too close to call" advisory is a separate label alongside the
     real number, never a replacement for it.
  6. Real conflict detection: if the two most-weighted available
     signals point in genuinely opposite directions, that's flagged
     explicitly rather than silently averaged away -- this is the
     direct fix for the real Darderi TGW miss (dominance scoring said
     one thing, recent-pattern reasoning said another, and the old
     system just picked whichever sounded better instead of treating
     the disagreement itself as a real warning sign).
"""
from dataclasses import dataclass, field
from typing import Optional

from elo_calculator import expected_score
from build_signal_calibration import calibrated_probability

# Real weights, derived from tonight's actual backtested AUCs.
# weight_i = (AUC_i - 0.5) / sum_of_all(AUC_j - 0.5)
# Recomputed here explicitly (not hardcoded pre-normalized) so the
# real derivation stays visible and auditable.
_REAL_AUC = {"elo": 0.7223, "s5": 0.6157, "s1": 0.5892, "h2h": 0.584}
_MARGIN = {k: v - 0.5 for k, v in _REAL_AUC.items()}
_MARGIN_SUM = sum(_MARGIN.values())
REAL_WEIGHTS = {k: v / _MARGIN_SUM for k, v in _MARGIN.items()}

CONFLICT_THRESHOLD_HIGH = 0.55  # a signal reading above this = "favors player"
CONFLICT_THRESHOLD_LOW = 0.45   # a signal reading below this = "favors opponent"
TOO_CLOSE_LOW = 0.45
TOO_CLOSE_HIGH = 0.55

# 2026-08-15 single-signal cap support. Real live results (Cecchinato
# 75.7% GREEN on ELO alone, lost; Djokovic 79.6% GREEN on ELO 89% + a
# lukewarm S5 61%, lost) showed thin-signal numbers wearing GREEN. The
# engine now reports how thin the evidence is so evaluate_moneyline()
# can cap the BAND (not the number -- the number stays honest).
# Divergence thresholds are a first cut, NOT backtested -- they only
# produce a warning note, they never change the probability.
DIVERGENCE_STRONG = 0.75   # top-weighted signal reads at/above this
DIVERGENCE_LUKEWARM_LOW = 0.45
DIVERGENCE_LUKEWARM_HIGH = 0.62  # a second signal sitting in 45-62% = lukewarm


@dataclass
class SignalContribution:
    name: str
    raw_value: Optional[float]
    probability: Optional[float]
    weight: float
    included: bool
    reason_excluded: Optional[str] = None


@dataclass
class ConfidenceResult:
    win_probability: Optional[float]
    american_odds: Optional[int]
    advisory_label: Optional[str]
    conflict_detected: bool
    conflict_note: Optional[str]
    contributions: list = field(default_factory=list)
    # 2026-08-15 single-signal cap support
    included_signal_count: int = 0
    single_signal: bool = False        # True = blend rests on ONE signal only
    divergence_detected: bool = False  # strong top signal vs lukewarm second
    divergence_note: Optional[str] = None


def probability_to_american_odds(p: float) -> int:
    """
    Real, standard American-odds conversion -- not Apex-specific math,
    the actual real-world formula sportsbooks use. p is a real win
    probability (0-1, exclusive of exactly 0 or 1).
    """
    if p <= 0 or p >= 1:
        return None
    if p >= 0.5:
        return round(-100 * p / (1 - p))
    else:
        return round(100 * (1 - p) / p)


def blend_probabilities(contributions: list) -> tuple:
    """
    contributions: list of SignalContribution with included=True and a
    real probability set. Real weighted average, weights RENORMALIZED
    to sum to 1 across only the signals actually available for this
    match (a match with no real H2H data doesn't get penalized by an
    H2H weight that has nothing to contribute to).

    Real conflict check: compares the two HIGHEST-weighted available
    signals. If one reads clearly above 0.55 and the other clearly
    below 0.45 (genuinely opposite leans, not just noise around 50%),
    flags a real conflict -- this does not change the blended number
    itself, it surfaces the disagreement so a human (or a future rule)
    can see it rather than have it silently averaged away.
    """
    included = [c for c in contributions if c.included and c.probability is not None]
    if not included:
        return None, False, "No real signals available for this match."

    total_weight = sum(c.weight for c in included)
    if total_weight == 0:
        return None, False, "Available signals have zero real combined weight."

    blended = sum(c.probability * c.weight for c in included) / total_weight

    conflict = False
    conflict_note = None
    if len(included) >= 2:
        ranked = sorted(included, key=lambda c: c.weight, reverse=True)
        top, second = ranked[0], ranked[1]
        top_high = top.probability >= CONFLICT_THRESHOLD_HIGH
        top_low = top.probability <= CONFLICT_THRESHOLD_LOW
        second_high = second.probability >= CONFLICT_THRESHOLD_HIGH
        second_low = second.probability <= CONFLICT_THRESHOLD_LOW
        if (top_high and second_low) or (top_low and second_high):
            conflict = True
            conflict_note = (
                f"{top.name} ({top.probability:.1%}) and {second.name} "
                f"({second.probability:.1%}) point in genuinely opposite "
                f"directions -- real disagreement between two of the "
                f"strongest available signals, not just noise."
            )

    return blended, conflict, conflict_note


def detect_divergence(contributions: list) -> tuple:
    """
    2026-08-15. Softer than blend_probabilities()' conflict check.
    Conflict = two top signals point OPPOSITE ways (>=55% vs <=45%).
    Divergence = the top-weighted signal is STRONG (>=75%) while any
    other included signal is only LUKEWARM (45-62%) on the same side or
    near coin-flip. Real case: Djokovic ELO 89.2% / S5 61.1% -> 79.6%
    GREEN, lost. Conflict flag was False there; this catches it.
    Returns (detected, note). Never changes the blended number.
    """
    included = [c for c in contributions if c.included and c.probability is not None]
    if len(included) < 2:
        return False, None
    ranked = sorted(included, key=lambda c: c.weight, reverse=True)
    top = ranked[0]
    top_strong_high = top.probability >= DIVERGENCE_STRONG
    top_strong_low = top.probability <= (1.0 - DIVERGENCE_STRONG)
    if not (top_strong_high or top_strong_low):
        return False, None
    lukewarm = []
    for c in ranked[1:]:
        p = c.probability
        # mirror the band when the top signal favors the opponent side
        q = p if top_strong_high else (1.0 - p)
        if DIVERGENCE_LUKEWARM_LOW <= q <= DIVERGENCE_LUKEWARM_HIGH:
            lukewarm.append(c)
    if not lukewarm:
        return False, None
    names = ", ".join(f"{c.name} ({c.probability:.1%})" for c in lukewarm)
    note = (
        f"{top.name} reads strong ({top.probability:.1%}) but {names} "
        f"only lukewarm -- the blended number is being carried mostly by "
        f"one signal. Treat as a warning even though no outright conflict."
    )
    return True, note


def compute_moneyline_confidence(
    elo_gap: Optional[float],
    s5_raw: Optional[float], s5_table: list,
    s1_raw: Optional[float], s1_table: list,
    h2h_raw: Optional[float], h2h_table: list,
) -> ConfidenceResult:
    """
    Real, end-to-end orchestration. Every input can be None (signal
    unavailable/unconfirmed for this match) -- the blend renormalizes
    around whatever real signals ARE available. ELO is the one signal
    treated as foundational (its real win-probability formula, not a
    calibration-table lookup) since it has its own exact real math,
    not just a historically-observed relationship.
    """
    contributions = []

    if elo_gap is not None:
        # expected_score() takes two ratings; a gap is equivalent to
        # comparing (1500+gap) vs 1500 -- the real formula only cares
        # about the difference, so this is mathematically identical
        # to passing the two real ratings directly.
        p_elo = expected_score(1500 + elo_gap, 1500)
        contributions.append(SignalContribution("ELO", elo_gap, p_elo, REAL_WEIGHTS["elo"], True))
    else:
        contributions.append(SignalContribution("ELO", None, None, REAL_WEIGHTS["elo"], False, "no real ELO rating for one or both players"))

    for name, raw, table in (("S5", s5_raw, s5_table), ("S1", s1_raw, s1_table), ("H2H", h2h_raw, h2h_table)):
        if raw is not None and table:
            p = calibrated_probability(raw, table)
            if p is not None:
                contributions.append(SignalContribution(name, raw, p, REAL_WEIGHTS[name.lower()], True))
            else:
                contributions.append(SignalContribution(name, raw, None, REAL_WEIGHTS[name.lower()], False, "raw value fell in a real bucket with insufficient sample"))
        else:
            contributions.append(SignalContribution(name, raw, None, REAL_WEIGHTS[name.lower()], False, "no real confirmed data for this match"))

    blended, conflict, conflict_note = blend_probabilities(contributions)

    advisory = None
    if blended is not None and TOO_CLOSE_LOW <= blended <= TOO_CLOSE_HIGH:
        advisory = "TOO CLOSE TO CALL — real signals land near a coin flip for this match"

    odds = probability_to_american_odds(blended) if blended is not None else None

    # 2026-08-15 single-signal cap support
    included_count = sum(1 for c in contributions if c.included and c.probability is not None)
    single_signal = included_count == 1
    divergence, divergence_note = detect_divergence(contributions)

    return ConfidenceResult(
        win_probability=blended,
        american_odds=odds,
        advisory_label=advisory,
        conflict_detected=conflict,
        conflict_note=conflict_note,
        contributions=contributions,
        included_signal_count=included_count,
        single_signal=single_signal,
        divergence_detected=divergence,
        divergence_note=divergence_note,
    )
