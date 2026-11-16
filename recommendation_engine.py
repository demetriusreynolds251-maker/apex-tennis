"""
recommendation_engine.py

Layer 4 of the pipeline (Prediction -> Market Comparison -> Recommendation).

This module NEVER touches how a prediction was made. It takes a completed,
already-final prediction (from a MatchModel or, for props, any predicted
value + confidence) and decides ONLY: is this available to bet, and does it
clear the bar. Per the architecture handoff: recommendation must never
influence prediction. This file is the recommendation half only.

Market comparison is line-only (a number), never odds or implied
probability -- Rule 9 (market data permanently rejected) stays intact.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Recommendation(Enum):
    BET = "BET"
    PASS_DIRECTION_MISMATCH = "PASS_DIRECTION_MISMATCH"
    PASS_INSUFFICIENT_EDGE = "PASS_INSUFFICIENT_EDGE"
    PASS_LOW_CONFIDENCE = "PASS_LOW_CONFIDENCE"
    PASS_THIN_DATA = "PASS_THIN_DATA"


@dataclass(frozen=True)
class Prediction:
    """A completed, direction-agnostic prediction. Produced upstream by
    the Match Model or a primitive estimator -- this module does not
    produce or adjust this value in any way, only reads it."""
    predicted_value: float       # e.g. expected games, or win_prob_a
    direction: str                # "MORE", "LESS", "PLAYER_A", "PLAYER_B" -- whatever the market's two sides are called
    confidence: float             # 0-1, how sure the estimate itself is (data quality / evidence strength)
    evidence_category_count: int  # how many distinct evidence categories had verifiable data
    min_categories_required: int = 5  # threshold for PASS_THIN_DATA; caller-configurable per market


@dataclass(frozen=True)
class Market:
    """What the market actually offers. Line is a NUMBER (a stat threshold),
    never odds or implied probability -- see module docstring / Rule 9."""
    line: Optional[float] = None       # e.g. 22.5 games -- None for pure win/loss markets (moneyline)
    available_directions: tuple = ("MORE", "LESS")  # for goblin/demon, only one entry
    is_locked: bool = False            # True for goblin/demon (only one side biddable)


@dataclass
class RecommendationResult:
    recommendation: Recommendation
    prediction: Prediction
    market: Market
    edge: Optional[float] = None       # signed distance from line, in the predicted direction; None if N/A
    notes: list = field(default_factory=list)


def recommend(prediction: Prediction, market: Market,
              min_confidence: float = 0.55,
              min_edge_fraction_of_line: float = 0.02) -> RecommendationResult:
    """
    Decision order, matching the agreed architecture exactly:
    1. Direction mismatch check (goblin/demon lock) -- checked FIRST because
       it requires no judgment call, it's a fact about the market.
    2. Data sufficiency check -- can we trust this prediction at all.
    3. Confidence check -- is the estimate itself sure enough.
    4. Edge check (line markets only) -- is the predicted value far enough
       from the line to be worth betting, not just on the right side of it.

    Each PASS branch returns immediately with its own reason code -- no
    branch is allowed to silently fall through into another's territory,
    so a "why did we pass" query always has exactly one true answer.
    """
    notes = []

    # 1. Direction mismatch -- purely a market-availability fact, not a
    # judgment about prediction quality. Always checked first.
    if prediction.direction not in market.available_directions:
        notes.append(
            f"Predicted {prediction.direction}, but market only offers "
            f"{market.available_directions}."
        )
        return RecommendationResult(
            recommendation=Recommendation.PASS_DIRECTION_MISMATCH,
            prediction=prediction, market=market, notes=notes,
        )

    # 2. Data sufficiency -- before trusting confidence/edge numbers at all.
    if prediction.evidence_category_count < prediction.min_categories_required:
        notes.append(
            f"Only {prediction.evidence_category_count}/"
            f"{prediction.min_categories_required} evidence categories had "
            f"verifiable data."
        )
        return RecommendationResult(
            recommendation=Recommendation.PASS_THIN_DATA,
            prediction=prediction, market=market, notes=notes,
        )

    # 3. Confidence -- the estimate itself is too uncertain to act on,
    # independent of where the line sits.
    if prediction.confidence < min_confidence:
        notes.append(
            f"Confidence {prediction.confidence:.2f} below floor "
            f"{min_confidence:.2f}."
        )
        return RecommendationResult(
            recommendation=Recommendation.PASS_LOW_CONFIDENCE,
            prediction=prediction, market=market, notes=notes,
        )

    # 4. Edge -- only meaningful for line markets (props). Moneyline-style
    # markets (market.line is None) skip this check; direction + confidence
    # are the whole story there.
    edge = None
    if market.line is not None:
        edge = prediction.predicted_value - market.line
        # Sign of edge must agree with predicted direction, or something
        # upstream is inconsistent -- flag rather than silently proceeding.
        if (prediction.direction == "MORE" and edge <= 0) or \
           (prediction.direction == "LESS" and edge >= 0):
            notes.append(
                "Inconsistent inputs: predicted direction disagrees with "
                "sign of (predicted_value - line). Upstream bug, not a "
                "market condition -- do not recommend."
            )
            return RecommendationResult(
                recommendation=Recommendation.PASS_LOW_CONFIDENCE,
                prediction=prediction, market=market, edge=edge, notes=notes,
            )

        min_edge = abs(market.line) * min_edge_fraction_of_line
        if abs(edge) < min_edge:
            notes.append(
                f"Edge {edge:+.2f} smaller than minimum "
                f"{min_edge:.2f} ({min_edge_fraction_of_line:.0%} of line)."
            )
            return RecommendationResult(
                recommendation=Recommendation.PASS_INSUFFICIENT_EDGE,
                prediction=prediction, market=market, edge=edge, notes=notes,
            )

    notes.append("Cleared all checks: direction available, data sufficient, "
                  "confidence adequate" + (", edge adequate." if edge is not None else "."))
    return RecommendationResult(
        recommendation=Recommendation.BET,
        prediction=prediction, market=market, edge=edge, notes=notes,
    )
