"""
================================================================
ATE V1 — READ QUERIES
================================================================
Read-only. Used by Evaluation Trace Center and Cost Intelligence
Center. These never write, never influence the pipeline, and are
safe to call from Mission Control or any other consumer without
touching evaluation flow.
================================================================
"""

import sqlite3
from datetime import datetime, timedelta, timezone
import pytz
from ate_db import ATE_DB_PATH

# Matches openclaw_core.py's CT constant exactly (confirmed: CT =
# pytz.timezone("America/Chicago"), line 239) — duplicated here rather
# than imported, to keep this module dependency-light (importing
# openclaw_core.py directly would pull in requests/schedule/pytz's
# full config-and-prompt-loading startup path just for one constant).
# ai_traces.created_at is stored via SQLite's datetime('now') (UTC) —
# every "today"/"this week" boundary below is computed by converting
# to CT first, not via SQLite's own date()/datetime() functions,
# which use the server's system timezone, not necessarily CT.
CT = pytz.timezone("America/Chicago")


def _rows(query, params, db_path=None):
    conn = sqlite3.connect(db_path or ATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    result = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()
    return result


def get_traces_for_evaluation(eval_id, db_path=None):
    return _rows(
        "SELECT * FROM ai_traces WHERE eval_id = ? ORDER BY created_at",
        (eval_id,), db_path)


def get_rule_activations_for_evaluation(eval_id, db_path=None):
    return _rows(
        "SELECT * FROM rule_activations WHERE eval_id = ? ORDER BY sequence_order",
        (eval_id,), db_path)


def get_checkpoint_stamps_for_evaluation(eval_id, db_path=None):
    return _rows(
        "SELECT * FROM checkpoint_stamps WHERE eval_id = ? ORDER BY sequence_order",
        (eval_id,), db_path)


def get_full_trace_bundle(eval_id, db_path=None):
    """Everything ATE has for one evaluation, in one call — the
    shape Evaluation Trace Center's screen consumes directly."""
    return {
        "eval_id": eval_id,
        "traces": get_traces_for_evaluation(eval_id, db_path),
        "rule_activations": get_rule_activations_for_evaluation(eval_id, db_path),
        "checkpoint_stamps": get_checkpoint_stamps_for_evaluation(eval_id, db_path),
    }


def confidence_integrity_check(eval_id, dataset_a_final_confidence, db_path=None):
    """
    Sums confidence_delta across rule_activations for this
    evaluation and compares to Dataset A's stored final confidence.
    Per the approved V1 design: most V1 rows will have
    confidence_delta = NULL (see ATE_V1_SCOPE_FINALIZED.md), so
    total will usually be 0.0 with zero contributing rows — this
    is EXPECTED, not an error, and the caller (Evaluation Trace
    Center's UI) should display "not available for this evaluation"
    rather than a fabricated-looking 0% delta whenever
    contributing_rows == 0.
    """
    activations = get_rule_activations_for_evaluation(eval_id, db_path)
    contributing = [a for a in activations if a.get("confidence_delta") is not None]
    total_delta = sum(a["confidence_delta"] for a in contributing)
    return {
        "eval_id": eval_id,
        "contributing_rows": len(contributing),
        "total_rule_activations": len(activations),
        "summed_delta": total_delta if contributing else None,
        "dataset_a_final_confidence": dataset_a_final_confidence,
        "available": len(contributing) > 0,
        "matches": (
            None if not contributing or dataset_a_final_confidence is None
            else abs(total_delta - dataset_a_final_confidence) < 0.005
        ),
    }


# ================================================================
# COST INTELLIGENCE CENTER — read-only aggregation queries.
# All against ai_traces only; no new tables, no new writers.
# ================================================================

def _connect(db_path=None):
    conn = sqlite3.connect(db_path or ATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ct_window_utc_bounds(days_ago=0, span_days=1):
    """
    Returns (start_utc_str, end_utc_str) in SQLite datetime('now')
    format ('YYYY-MM-DD HH:MM:SS') for a CT-calendar-day-aligned
    window. days_ago=0, span_days=1 -> "today" in CT. days_ago=0,
    span_days=7 -> the 7 CT calendar days ending today, inclusive.
    Computed via pytz in Python, not SQLite's date functions — see
    the module-level CT comment for why.
    """
    now_ct = datetime.now(CT)
    end_ct = (now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
              - timedelta(days=days_ago) + timedelta(days=1))
    start_ct = end_ct - timedelta(days=span_days)
    start_utc = start_ct.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = end_ct.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return start_utc, end_utc


def _to_ct_date(created_at_str):
    """Converts an ai_traces.created_at string (UTC) to a CT calendar
    date, for day-bucketing (daily_cost_history) where SQLite's own
    date functions can't be trusted to mean the same "day" the rest
    of Apex means (see openclaw_ops.today_str(), which uses the same
    CT convention)."""
    dt_utc = datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(CT).date()


def cost_summary(db_path=None):
    """SECTION 1 — Today / 7-day / 30-day / lifetime cost, real SQL
    SUM aggregation per window, CT-day-boundary aware."""
    conn = _connect(db_path)
    result = {}
    for key, span in (("today", 1), ("last_7_days", 7), ("last_30_days", 30)):
        start, end = _ct_window_utc_bounds(days_ago=0, span_days=span)
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM ai_traces "
            "WHERE created_at >= ? AND created_at < ?", (start, end)).fetchone()
        result[key] = round(row["total"] or 0.0, 4)
    lifetime = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM ai_traces").fetchone()
    result["lifetime"] = round(lifetime["total"] or 0.0, 4)
    conn.close()
    return result


def _eval_aggregates(conn, order_by, limit=None):
    """One row per eval_id, summed across every traced step
    (evaluation + compliance, etc.) belonging to it — this is the
    "evaluation" unit Sections 2/5/6 mean by "highest-cost
    evaluation", not a single raw ai_traces row."""
    sql = (
        "SELECT eval_id, "
        "SUM(cost_usd) AS total_cost, "
        "SUM(latency_ms) AS total_latency_ms, "
        "COUNT(*) AS trace_count, "
        "MIN(created_at) AS created_at "
        "FROM ai_traces GROUP BY eval_id ORDER BY " + order_by
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def evaluation_metrics(db_path=None):
    """SECTION 2 — evaluation-level metrics. "Evaluations Traced"
    counts distinct eval_id values; "traces_captured" counts raw
    ai_traces rows (an evaluation may have 2: evaluation + compliance)
    — exposed separately since conflating them would misrepresent
    both numbers."""
    conn = _connect(db_path)
    overall = conn.execute(
        "SELECT COUNT(*) AS traces, COUNT(DISTINCT eval_id) AS evals, "
        "AVG(latency_ms) AS avg_latency FROM ai_traces"
    ).fetchone()
    if not overall["evals"]:
        conn.close()
        return {"evaluations_traced": 0, "traces_captured": 0,
                "avg_cost_per_evaluation": None, "avg_latency_ms": None,
                "highest_cost": None, "lowest_cost": None,
                "slowest": None, "fastest": None}
    # Deliberately NOT AVG(cost_usd) over raw ai_traces rows — that
    # would average per TRACE (a caught bug during testing: an
    # evaluation with both an evaluation-step and a compliance-step
    # trace was silently pulling the average down, since it counts
    # as 2 cheap-and-expensive rows instead of 1 evaluation with a
    # combined cost). This averages the per-eval_id SUMMED totals,
    # matching what "per evaluation" actually means.
    avg_cost_row = conn.execute(
        "SELECT AVG(total_cost) AS avg_cost FROM "
        "(SELECT eval_id, SUM(cost_usd) AS total_cost FROM ai_traces GROUP BY eval_id)"
    ).fetchone()
    by_cost_desc = _eval_aggregates(conn, "total_cost DESC", limit=1)
    by_cost_asc = _eval_aggregates(conn, "total_cost ASC", limit=1)
    by_lat_desc = _eval_aggregates(conn, "total_latency_ms DESC", limit=1)
    by_lat_asc = _eval_aggregates(conn, "total_latency_ms ASC", limit=1)
    conn.close()
    return {
        "evaluations_traced": overall["evals"],
        "traces_captured": overall["traces"],
        "avg_cost_per_evaluation": round(avg_cost_row["avg_cost"] or 0.0, 4),
        "avg_latency_ms": round(overall["avg_latency"] or 0.0, 1),
        "highest_cost": by_cost_desc[0] if by_cost_desc else None,
        "lowest_cost": by_cost_asc[0] if by_cost_asc else None,
        "slowest": by_lat_desc[0] if by_lat_desc else None,
        "fastest": by_lat_asc[0] if by_lat_asc else None,
    }


def model_breakdown(db_path=None):
    """SECTION 3 — grouped by model, real SQL GROUP BY."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT model, COUNT(*) AS call_count, "
        "SUM(tokens_in) AS total_tokens_in, SUM(tokens_out) AS total_tokens_out, "
        "SUM(cost_usd) AS total_cost, AVG(cost_usd) AS avg_cost, "
        "AVG(latency_ms) AS avg_latency_ms "
        "FROM ai_traces GROUP BY model ORDER BY total_cost DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def step_type_breakdown(db_path=None):
    """SECTION 4 — grouped by step_type (evaluation / compliance /
    moneyline_evaluation), real SQL GROUP BY. Answers "which Apex
    subsystem consumes the most resources"."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT step_type, COUNT(*) AS call_count, "
        "SUM(cost_usd) AS total_cost, AVG(cost_usd) AS avg_cost, "
        "AVG(latency_ms) AS avg_latency_ms "
        "FROM ai_traces GROUP BY step_type ORDER BY total_cost DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def top_expensive_evaluations(n=10, db_path=None):
    """SECTION 5."""
    conn = _connect(db_path)
    result = _eval_aggregates(conn, "total_cost DESC", limit=n)
    conn.close()
    return result


def top_slowest_evaluations(n=10, db_path=None):
    """SECTION 6."""
    conn = _connect(db_path)
    result = _eval_aggregates(conn, "total_latency_ms DESC", limit=n)
    conn.close()
    return result


def daily_cost_history(db_path=None):
    """
    SECTION 7 — one row per CT calendar day. The one section that
    can't be pure SQL: SQLite has no IANA timezone support, so a
    CT-aware day boundary can only be computed in Python. Deliberately
    pulls only 3 columns (not SELECT *) to keep this the one
    necessarily-Python-side pass as light as it can be.
    """
    conn = _connect(db_path)
    rows = conn.execute("SELECT eval_id, cost_usd, created_at FROM ai_traces").fetchall()
    conn.close()
    by_day = {}
    for r in rows:
        d = _to_ct_date(r["created_at"]).isoformat()
        bucket = by_day.setdefault(d, {"date": d, "eval_ids": set(), "total_cost": 0.0})
        bucket["eval_ids"].add(r["eval_id"])
        bucket["total_cost"] += r["cost_usd"] or 0.0
    return [
        {"date": d, "evaluation_count": len(b["eval_ids"]), "total_cost": round(b["total_cost"], 4)}
        for d, b in sorted(by_day.items(), reverse=True)
    ]


def cost_intelligence_bundle(db_path=None):
    """Everything Cost Intelligence Center's screen needs, in one
    call — mirrors get_full_trace_bundle()'s pattern for Evaluation
    Trace Center."""
    return {
        "summary": cost_summary(db_path),
        "evaluation_metrics": evaluation_metrics(db_path),
        "model_breakdown": model_breakdown(db_path),
        "step_type_breakdown": step_type_breakdown(db_path),
        "top_expensive": top_expensive_evaluations(10, db_path),
        "top_slowest": top_slowest_evaluations(10, db_path),
        "daily_history": daily_cost_history(db_path),
    }
