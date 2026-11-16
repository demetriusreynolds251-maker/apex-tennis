"""
estimator_calibration_ops.py — Mission Control backend for estimator-level
calibration (Brier score / reliability diagrams on PRIMITIVES, not rules).

Distinct from the existing rule-level Calibration screen: that one scores
whether RULES (Gate14_Props_Procedural, RBMD_v1_2, etc) fire correctly.
This one scores whether numeric PRIMITIVE ESTIMATES (hold_pct, avg_aces,
etc) actually match reality, using calibration.py's Brier score / log
loss / reliability-diagram logic -- the same module already built and
tested this session, not reimplemented here.

Reads from the primitive_estimates table (dataset_a.sqlite) that
openclaw_core.py's evaluation_agent() now logs to via
d1.log_primitive_estimate(). Fail-open throughout, same convention as
warehouse_ops.py.
"""
from pathlib import Path
import sqlite3

import calibration as cal  # the module built and tested earlier this session

HERE    = Path(__file__).resolve().parent
DB_PATH = HERE / "data" / "warehouse" / "dataset_a.sqlite"

PRIMITIVES = ["hold_pct", "opp_hold_pct", "bp_save_pct", "break_pct",
              "avg_aces", "first_serve_pct"]

_OFFLINE = {"status": "offline", "message": "dataset_a.sqlite not accessible"}


def _conn():
    if not DB_PATH.exists():
        return None
    try:
        c = sqlite3.connect(str(DB_PATH), timeout=5)
        c.row_factory = sqlite3.Row
        return c
    except Exception:
        return None


def get_pick_shadow_data(eval_id):
    """
    For the Mission Control pick-detail view (renderPickDetail /
    /api/pick_eval): fetches the shadow signals logged for ONE specific
    evaluation -- data completeness, the Match Model cross-check, and
    the Recommendation Engine's shadow read. Returns None (not an error
    dict) when nothing was logged for this eval_id, since that's the
    normal case for any evaluation that predates this feature -- the UI
    treats None as "not available for this pick," not an error.
    """
    if not eval_id:
        return None
    conn = _conn()
    if not conn:
        return None
    try:
        row = conn.execute(
            "SELECT data_completeness_pct, data_completeness_note, "
            "hold_pct, opp_hold_pct, bp_save_pct, break_pct, avg_aces, first_serve_pct, "
            "match_model_win_prob, match_model_expected_games, match_model_expected_sets, "
            "shadow_recommendation, shadow_reason_code, shadow_edge, "
            "goblin_conflict "
            "FROM primitive_estimates WHERE eval_id = ? "
            "ORDER BY logged_at DESC LIMIT 1", (eval_id,)
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None  # table doesn't exist yet -- same "not available" treatment
    except Exception:
        return None
    finally:
        conn.close()
def estimator_calibration_summary():
    """
    2026-08-09 FIX: this function's own `def` line was missing from
    the file as uploaded — its entire real body (everything below)
    was silently swallowed as unreachable dead code inside
    get_pick_shadow_data() above (that function returns in every
    branch of its try/except, so nothing after its `finally` could
    ever run). openclaw_mission_control.py calls
    `eco.estimator_calibration_summary()` directly (confirmed via the
    real caller, not guessed) — before this fix, that call would have
    raised a real AttributeError the moment Mission Control's
    Calibration screen tried to load. Real signature restored here.

    Also wires in the real evaluation_type segmentation fix from
    calibration.py (2026-08-09, earlier this session): primitive_estimates
    has NO evaluation_type column of its own (confirmed from the real
    SELECT below) -- evaluation_type lives on the `evaluations` table,
    joined here via the shared eval_id key, same real relationship
    dataset_a_capture.py's own d1.start(eval_id, evaluation_type, ...)
    establishes. Each ScoredRecord now carries the real evaluation_type
    pulled from that join, and per-primitive/per-version verdicts are
    now reported BOTH combined (as before, for backward compatibility)
    AND split real PROP vs real MONEYLINE (the whole reason that
    calibration.py fix existed -- until this wiring, it had nothing
    real to filter, since no caller ever populated evaluation_type on
    a ScoredRecord).

    One call, everything the Mission Control screen needs:
    - total rows logged (proves the logging pipeline is actually running)
    - total rows settled (have a real actual_outcome yet)
    - per-primitive verdicts (WELL_CALIBRATED / MISCALIBRATED / INSUFFICIENT_DATA),
      both combined and split by real evaluation_type
    - reliability-diagram bucket data for the primitive with the most
      settled samples (for the chart)
    - goblin-conflict-specific stats: how many logged predictions disagreed
      with a locked market direction, since those are free calibration
      data per the architecture review and worth surfacing separately
    """
    conn = _conn()
    if not conn:
        return _OFFLINE
    try:
        total_logged = conn.execute(
            "SELECT COUNT(*) FROM primitive_estimates").fetchone()
        if total_logged is None:
            return {"status": "ok", "table_exists": False, "total_logged": 0,
                    "message": "primitive_estimates table does not exist yet -- "
                               "logging has not been wired in or has not run."}
        total_logged = total_logged[0]

        total_settled = conn.execute(
            "SELECT COUNT(*) FROM primitive_estimates WHERE actual_outcome IS NOT NULL"
        ).fetchone()[0]

        goblin_conflicts = conn.execute(
            "SELECT COUNT(*) FROM primitive_estimates WHERE goblin_conflict = 1"
        ).fetchone()[0]
        goblin_conflicts_settled = conn.execute(
            "SELECT COUNT(*) FROM primitive_estimates "
            "WHERE goblin_conflict = 1 AND actual_outcome IS NOT NULL"
        ).fetchone()[0]

        per_primitive = []
        best_bucket_primitive = None
        best_bucket_n = -1
        for prim in PRIMITIVES:
            # Real JOIN to evaluations for the real evaluation_type --
            # primitive_estimates itself has no such column. LEFT JOIN
            # (not INNER) so a row whose evaluations record is somehow
            # missing still gets scored in the combined view -- it just
            # won't show up in either real PROP/MONEYLINE split, same
            # "unconfirmed segment, not a fabricated one" discipline
            # used everywhere else this session.
            rows = conn.execute(
                "SELECT pe.eval_id, pe.logged_at, pe.estimator_version, pe.schema_version, "
                f"pe.{prim} as val, pe.actual_outcome, e.evaluation_type as evaluation_type "
                "FROM primitive_estimates pe "
                "LEFT JOIN evaluations e ON pe.eval_id = e.eval_id "
                f"WHERE pe.{prim} IS NOT NULL",
            ).fetchall()

            # Group by estimator_version -- score_estimator() requires it,
            # and conflating versions would silently blend different logic
            # (see calibration.py docstring / the versioning guard agreed
            # earlier this session).
            by_version = {}
            for r in rows:
                by_version.setdefault(r["estimator_version"], []).append(r)

            version_verdicts = []
            for version, vrows in by_version.items():
                records = [
                    cal.ScoredRecord(
                        schema_version=r["schema_version"],
                        estimator_version=r["estimator_version"],
                        match_id=r["eval_id"] or "",
                        primitive_name=prim,
                        predicted_value=r["val"],
                        actual_outcome=r["actual_outcome"],
                        evaluation_type=r["evaluation_type"],
                    ) for r in vrows
                ]

                combined_verdict = cal.score_estimator(records, version, prim)
                prop_verdict = cal.score_estimator(records, version, prim, evaluation_type="PROP")
                moneyline_verdict = cal.score_estimator(records, version, prim, evaluation_type="MONEYLINE")

                def _verdict_dict(v):
                    return {
                        "n_samples": v.n_samples, "verdict": v.verdict,
                        "mean_predicted": v.mean_predicted, "mean_actual": v.mean_actual,
                        "mean_absolute_error": v.mean_absolute_error,
                        "reliability_buckets": v.reliability_buckets,
                    }

                version_verdicts.append({
                    "estimator_version": version,
                    "combined": _verdict_dict(combined_verdict),
                    "by_evaluation_type": {
                        "PROP": _verdict_dict(prop_verdict),
                        "MONEYLINE": _verdict_dict(moneyline_verdict),
                    },
                })
                settled_n = sum(1 for r in vrows if r["actual_outcome"] is not None)
                if settled_n > best_bucket_n:
                    best_bucket_n = settled_n
                    best_bucket_primitive = prim

            per_primitive.append({
                "primitive": prim,
                "total_logged": len(rows),
                "total_settled": sum(1 for r in rows if r["actual_outcome"] is not None),
                "versions": version_verdicts,
            })

        return {
            "status": "ok",
            "table_exists": True,
            "total_logged": total_logged,
            "total_settled": total_settled,
            "goblin_conflicts_logged": goblin_conflicts,
            "goblin_conflicts_settled": goblin_conflicts_settled,
            "min_samples_for_verdict": cal.MIN_SAMPLES_FOR_VERDICT,
            "per_primitive": per_primitive,
            "highlighted_primitive": best_bucket_primitive,
        }
    except sqlite3.OperationalError as e:
        # Most likely: table doesn't exist yet (logging never wired/run)
        return {"status": "ok", "table_exists": False, "total_logged": 0,
                "message": f"primitive_estimates not queryable yet: {e}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()
