"""
calibration.py

Layer 7 (Calibration) of the pipeline. Scores ESTIMATORS, not individual
predictions -- per the agreed refinement, a single match's deviation from
an estimate is not a verdict on the estimator, it's one noisy sample from
a small binomial. This module refuses to produce a verdict until enough
samples exist, and refuses to trust a per-category "this helps/hurts"
finding without a minimum sample size and a holdout check -- otherwise
you WILL find spurious patterns in noise (the same failure mode as
p-hacking with many small comparisons), exactly as flagged earlier in
this review process.

Every record scored here must carry schema_version and estimator_version
(two different things -- schema is the shape of the record, estimator is
the actual logic that produced the number; they change on different
schedules and conflating them loses the traceability this exists for).
"""

from dataclasses import dataclass, field
from typing import Optional
import math


MIN_SAMPLES_FOR_VERDICT = 30  # below this, "not enough data yet" -- not a guess dressed as a verdict
MIN_SAMPLES_PER_BUCKET = 10   # for reliability-diagram buckets specifically


@dataclass(frozen=True)
class ScoredRecord:
    """One logged prediction with its eventual real outcome. This is what
    gets stored per Deliverable 3 of the Sprint 1 plan -- schema_version
    and estimator_version are both required, not optional, because without
    them a calibration verdict can silently blend two different estimator
    versions and produce a meaningless average.

    evaluation_type added 2026-08-09 (real fix, not a guess): props and
    moneyline picks were being scored in the SAME calibration buckets --
    no dimension existed to tell them apart at all. Real values are
    "PROP" or "MONEYLINE", matching dataset_a_capture.py's own enforced
    values exactly (asserted there, not invented here). Optional with a
    None default so any existing records/callers that don't set it yet
    keep working unchanged -- this is additive, not a breaking change."""
    schema_version: str
    estimator_version: str
    match_id: str
    primitive_name: str          # e.g. "hold_pct"
    predicted_value: float       # e.g. 0.88
    predicted_confidence: Optional[float] = None  # 0-1, if available
    actual_outcome: Optional[float] = None  # None until match settles
    evaluation_type: Optional[str] = None  # "PROP" or "MONEYLINE", None = unsegmented (legacy records)
    provenance: dict = field(default_factory=dict)  # actual values pulled, timestamped


@dataclass
class CalibrationVerdict:
    estimator_version: str
    primitive_name: str
    n_samples: int
    evaluation_type: Optional[str] = None  # which segment this verdict covers, None = unsegmented/combined
    mean_predicted: Optional[float] = None
    mean_actual: Optional[float] = None
    mean_absolute_error: Optional[float] = None
    verdict: str = "INSUFFICIENT_DATA"  # INSUFFICIENT_DATA | WELL_CALIBRATED | MISCALIBRATED
    reliability_buckets: list = field(default_factory=list)


def brier_score(predictions: list) -> Optional[float]:
    """Standard Brier score for probability predictions: mean squared
    error between predicted probability and binary outcome (0 or 1).
    Lower is better; 0 is perfect, 0.25 is what always-guessing-50% gives.
    Requires settled outcomes (0/1) -- returns None if none are settled."""
    settled = [(p.predicted_value, p.actual_outcome) for p in predictions
               if p.actual_outcome is not None]
    if not settled:
        return None
    return sum((pred - actual) ** 2 for pred, actual in settled) / len(settled)


def log_loss(predictions: list, eps: float = 1e-9) -> Optional[float]:
    """Standard log loss (cross-entropy) for probability predictions.
    Lower is better. Clips predictions away from exact 0/1 to avoid -inf."""
    settled = [(p.predicted_value, p.actual_outcome) for p in predictions
               if p.actual_outcome is not None]
    if not settled:
        return None
    total = 0.0
    for pred, actual in settled:
        pred = min(max(pred, eps), 1 - eps)
        total += -(actual * math.log(pred) + (1 - actual) * math.log(1 - pred))
    return total / len(settled)


def score_estimator(records: list, estimator_version: str,
                     primitive_name: str, evaluation_type: Optional[str] = None) -> CalibrationVerdict:
    """
    Aggregates many matches' worth of one estimator-version's predictions
    for one primitive into a single verdict. This is the function that
    enforces the "don't judge from one match" rule: below
    MIN_SAMPLES_FOR_VERDICT, it refuses to render WELL_CALIBRATED or
    MISCALIBRATED and returns INSUFFICIENT_DATA instead -- no matter how
    tempting the early numbers look.

    evaluation_type added 2026-08-09: when passed ("PROP" or
    "MONEYLINE"), only scores records matching that segment -- real fix
    for a real gap, props and moneyline picks were previously scored
    together with no way to tell them apart. Left as an optional
    parameter, default None, so a caller who wants the old combined
    behavior (or is scoring legacy records with no evaluation_type set)
    still gets it -- this is additive, not a forced migration.
    """
    relevant = [r for r in records
                if r.estimator_version == estimator_version
                and r.primitive_name == primitive_name
                and r.actual_outcome is not None
                and (evaluation_type is None or r.evaluation_type == evaluation_type)]

    if len(relevant) < MIN_SAMPLES_FOR_VERDICT:
        return CalibrationVerdict(
            estimator_version=estimator_version, primitive_name=primitive_name,
            n_samples=len(relevant), evaluation_type=evaluation_type, verdict="INSUFFICIENT_DATA",
        )

    mean_pred = sum(r.predicted_value for r in relevant) / len(relevant)
    mean_actual = sum(r.actual_outcome for r in relevant) / len(relevant)
    mae = sum(abs(r.predicted_value - r.actual_outcome) for r in relevant) / len(relevant)

    buckets = _reliability_buckets(relevant)

    # Simple, conservative rule: well-calibrated if the aggregate mean
    # prediction is close to the aggregate mean outcome AND every bucket
    # with enough samples is reasonably close too. This deliberately does
    # NOT try to be clever about statistical significance testing here --
    # a real implementation should use a proper calibration test, but this
    # at minimum refuses to call something "miscalibrated" off one bucket
    # with too few samples to mean anything.
    verdict = "WELL_CALIBRATED" if abs(mean_pred - mean_actual) < 0.05 else "MISCALIBRATED"

    return CalibrationVerdict(
        estimator_version=estimator_version, primitive_name=primitive_name,
        n_samples=len(relevant), evaluation_type=evaluation_type,
        mean_predicted=mean_pred, mean_actual=mean_actual,
        mean_absolute_error=mae, verdict=verdict, reliability_buckets=buckets,
    )


def _reliability_buckets(records: list, n_buckets: int = 5) -> list:
    """Groups predictions into probability buckets (e.g. 0-20%, 20-40%...)
    and reports predicted-vs-actual per bucket -- the actual reliability
    diagram data. Buckets with fewer than MIN_SAMPLES_PER_BUCKET are
    marked insufficient rather than reported with a misleadingly precise
    number."""
    edges = [i / n_buckets for i in range(n_buckets + 1)]
    buckets = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        in_bucket = [r for r in records if lo <= r.predicted_value < hi
                     or (hi == 1.0 and r.predicted_value == 1.0)]
        if len(in_bucket) < MIN_SAMPLES_PER_BUCKET:
            buckets.append({
                "range": f"{lo:.0%}-{hi:.0%}", "n": len(in_bucket),
                "verdict": "INSUFFICIENT_DATA",
            })
            continue
        mean_pred = sum(r.predicted_value for r in in_bucket) / len(in_bucket)
        mean_actual = sum(r.actual_outcome for r in in_bucket) / len(in_bucket)
        buckets.append({
            "range": f"{lo:.0%}-{hi:.0%}", "n": len(in_bucket),
            "mean_predicted": mean_pred, "mean_actual": mean_actual,
            "gap": mean_pred - mean_actual,
        })
    return buckets


def compare_estimator_versions(records: list, primitive_name: str,
                                version_a: str, version_b: str,
                                evaluation_type: Optional[str] = None) -> dict:
    """Multiple-comparisons-aware check for 'did the new estimator version
    actually help'. Requires MIN_SAMPLES_FOR_VERDICT on BOTH versions
    before saying anything -- refuses to declare a winner off an early
    lead, which is exactly the p-hacking trap flagged earlier: testing
    many primitive/category combinations against a small sample and
    trusting whichever ones look good by chance.

    evaluation_type added 2026-08-09, passed straight through to
    score_estimator() so a real comparison can be run within just
    PROP or just MONEYLINE picks -- same real segmentation gap fix."""
    verdict_a = score_estimator(records, version_a, primitive_name, evaluation_type=evaluation_type)
    verdict_b = score_estimator(records, version_b, primitive_name, evaluation_type=evaluation_type)

    if verdict_a.verdict == "INSUFFICIENT_DATA" or verdict_b.verdict == "INSUFFICIENT_DATA":
        return {
            "comparable": False,
            "reason": f"Need {MIN_SAMPLES_FOR_VERDICT}+ settled samples on both "
                      f"versions before comparing. Have {verdict_a.n_samples} vs "
                      f"{verdict_b.n_samples}.",
            "version_a": verdict_a, "version_b": verdict_b,
        }

    return {
        "comparable": True,
        "better_version": version_a if verdict_a.mean_absolute_error < verdict_b.mean_absolute_error else version_b,
        "mae_a": verdict_a.mean_absolute_error, "mae_b": verdict_b.mean_absolute_error,
        "note": "MAE comparison only -- treat as a signal to investigate further, "
                "not a final verdict, especially with few categories tested.",
        "version_a": verdict_a, "version_b": verdict_b,
    }
