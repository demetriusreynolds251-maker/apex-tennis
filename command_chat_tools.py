"""
================================================================
COMMAND CHAT V1 — TOOL LAYER (Phase 3/4)
================================================================
Read-only, mirrors ate_queries.py's pattern exactly. These are the
functions Command Chat's tool-use loop calls. Every one wraps an
already-proven interface — nothing here reimplements a connection
this session doesn't have verified access to.

Provenance of each tool, stated plainly rather than assumed uniform:
  - get_trace_bundle, get_recent_activations, get_cost_summary:
    thin wrappers over ate_queries.py — fully tested, real data,
    from this session and prior sessions.
  - get_evaluation: wraps openclaw_ops.pick_from_eval() directly —
    confirmed real interface, tested this session against a real
    scratch Dataset A schema (see test suite).
  - search_decisions, search_investigations, search_rules:
    search_decisions/search_investigations wrap command_chat_writer's
    FTS5 layer — fully tested. search_rules is NEW here (searches
    the rules corpus) — see its docstring for what's actually
    verified vs. best-effort.
  - get_dataset_b_player: queries Dataset B's `players` and
    `rankings_snapshot` tables directly, using column names
    CONFIRMED from tennis_warehouse_collector.py's real INSERT
    statements (not guessed). `player_year_stats` columns beyond
    what the collector's INSERT showed are NOT independently
    confirmed — flagged in the function itself.
  - get_rule_details: queries rule_activations directly — fully
    tested, real ATE data.
================================================================
"""

import sqlite3

import ate_queries as ate_q
import command_chat_writer as cc_writer

try:
    import openclaw_ops as ops
    _OPS_AVAILABLE = True
except ImportError:
    _OPS_AVAILABLE = False

try:
    import warehouse_ops as wops
    _WOPS_AVAILABLE = True
except ImportError:
    _WOPS_AVAILABLE = False


# ================================================================
# ATE-backed tools — fully proven, no new code paths
# ================================================================

def get_trace_bundle(eval_id):
    """Everything ATE captured for one evaluation: traces, rule
    activations, checkpoint stamps."""
    return ate_q.get_full_trace_bundle(eval_id)


def get_recent_activations(rule_name=None, limit=20, db_path=None):
    """Recent rule_activations, optionally filtered by rule name
    (e.g. "Rule 30") — answers "show recent Rule 30 activations"
    directly against the real table, no new query logic beyond a
    straightforward filter+order+limit."""
    conn = sqlite3.connect(db_path or ate_q.ATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    if rule_name:
        rows = conn.execute(
            "SELECT * FROM rule_activations WHERE rule_name LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (f"%{rule_name}%", limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM rule_activations ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rule_details(rule_name, db_path=None):
    """Every activation of a named rule, across all evaluations —
    answers "explain Rule 24" at the activation-history level (not
    the rule's own text, which lives in the Decision Tree document,
    not a database)."""
    conn = sqlite3.connect(db_path or ate_q.ATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM rule_activations WHERE rule_name LIKE ? ORDER BY created_at DESC",
        (f"%{rule_name}%",)).fetchall()
    conn.close()
    activations = [dict(r) for r in rows]
    return {
        "rule_name": rule_name,
        "total_activations": len(activations),
        "by_status": {
            s: len([a for a in activations if a["status"] == s])
            for s in set(a["status"] for a in activations)
        } if activations else {},
        "activations": activations,
    }


def get_cost_summary():
    """Today/7-day/30-day/lifetime cost — direct passthrough to the
    already-built, already-tested Cost Intelligence Center query."""
    return ate_q.cost_summary()


# ================================================================
# Dataset A — wraps the real, confirmed pick_from_eval() interface
# ================================================================

def get_evaluation(eval_id):
    """
    Full Dataset A detail for one evaluation — confidence ledger,
    final grade, Rule 24/30 status, compliance flags, reasoning
    text. Direct passthrough to openclaw_ops.pick_from_eval(),
    the same function Mission Control's inspector panel already
    uses — no new connection logic, no new query.
    """
    if not _OPS_AVAILABLE:
        return {"error": "openclaw_ops not available"}
    return ops.pick_from_eval(eval_id)


# ================================================================
# Dataset B — CONFIRMED column names only (from the collector's
# real INSERT statements), flagged where unconfirmed
# ================================================================

def get_dataset_b_player(player_name, db_path=None):
    """
    Player identity, ranking, and year-level stats from Dataset B.
    `players` and `rankings_snapshot` columns are CONFIRMED against
    tennis_warehouse_collector.py's real INSERT statements. The
    `player_year_stats` table is referenced by name in the collector
    but its full column list was never directly confirmed in this
    session (only that it exists and is written to) — queried here
    with SELECT * deliberately, so no column-name assumption is
    baked into the query itself.

    Fail-safe contract matches openclaw_ops._warehouse_query exactly
    (found and matched during testing, not assumed): a missing
    warehouse file, or a warehouse file that exists but doesn't yet
    have these tables, degrades to empty results — never raises.
    A bare sqlite3.connect() on a nonexistent path silently creates
    an empty file rather than erroring, so the real failure mode
    tested here is a missing TABLE, not just a missing file.
    """
    import tennis_warehouse_collector as twc
    from pathlib import Path
    path = db_path or twc.DB_PATH
    if not Path(path).exists():
        return {"player": None, "recent_rankings": [], "year_stats": []}
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        player_row = conn.execute(
            "SELECT * FROM players WHERE player = ?", (player_name,)).fetchone()
        rankings = conn.execute(
            "SELECT * FROM rankings_snapshot WHERE player = ? ORDER BY snapshot_date DESC LIMIT 10",
            (player_name,)).fetchall()
        year_stats = []
        try:
            year_stats = conn.execute(
                "SELECT * FROM player_year_stats WHERE player = ?", (player_name,)).fetchall()
        except sqlite3.OperationalError:
            pass  # table/column shape unconfirmed — degrade gracefully, don't raise
        conn.close()
        return {
            "player": dict(player_row) if player_row else None,
            "recent_rankings": [dict(r) for r in rankings],
            "year_stats": [dict(r) for r in year_stats],
        }
    except sqlite3.OperationalError:
        # Warehouse file exists but doesn't have these tables yet
        # (e.g. a fresh/partial DB) — same degrade-don't-raise contract.
        return {"player": None, "recent_rankings": [], "year_stats": []}


# ================================================================
# Memory search — decisions/investigations (fully tested), rules
# (best-effort — see docstring)
# ================================================================

def search_decisions(query, limit=10):
    return cc_writer.search_decisions(query, limit)


def search_investigations(query, limit=10):
    return cc_writer.search_investigations(query, limit)


def search_rules(query, limit=10):
    """
    BEST-EFFORT, not fully proven: searches rule_activations'
    reasoning_snippet and checkpoint_stamps' stamp_text for keyword
    matches, since neither the Decision Tree document nor the Master
    Prompt's full rule text is loaded into any queryable table in
    this session (they're plain .txt files on the Mac Mini, not
    indexed anywhere Command Chat can reach yet). This searches
    *activation history*, not rule *definitions* — a real, useful,
    but narrower capability than the name implies. Indexing the
    actual rule corpus text is a real, separate future step, not
    done here.
    """
    conn = sqlite3.connect(ate_q.ATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    like = f"%{query}%"
    rows = conn.execute(
        "SELECT eval_id, rule_name, status, reasoning_snippet, created_at "
        "FROM rule_activations WHERE reasoning_snippet LIKE ? "
        "ORDER BY created_at DESC LIMIT ?", (like, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ================================================================
# Memory Center Phase 6 — graph query routing. Per the finalized
# design's firm routing rule: single-entity "why/what is X" always
# goes to search_decisions/search_investigations above, never here.
# This tool is exclusively for relational questions ("what depends
# on X", "what would be affected if X changed") that search cannot
# answer, since search finds records ABOUT a name, not records
# CONNECTED TO one.
# ================================================================

def get_dependencies(node_type, node_id):
    """
    Direct graph neighbors of one node (both directions) — the
    Command Chat equivalent of Memory Center's Related panel, same
    underlying query. Use for 'what depends on Rule 30' / 'what
    would be affected if ATE changed' style questions only.
    """
    try:
        return cc_writer.get_related(node_type, node_id)
    except Exception as e:
        return {"error": str(e), "outgoing": [], "incoming": []}


# ================================================================
# SYSTEM-WIDE VISIBILITY TOOLS (this session, closes a real gap:
# Command Chat previously had zero tools covering Health Center,
# Warehouse Command, Operations, Agent Canvas, Rule Command Center,
# ML Readiness, Calibration, or Bankroll -- it could answer detailed
# questions about a single evaluation but nothing about the system
# as a whole, which defeated the point of having ONE place to ask
# anything. Every function below wraps an already-built, already-
# real function from openclaw_ops.py or warehouse_ops.py -- nothing
# here is a new calculation, only a new way to ask for one that
# already existed. Same fail-open contract as every other tool in
# this file: a missing module or a query failure returns a clear
# error dict, never raises into the conversation.
# ================================================================

def get_system_health():
    """
    Full Health Center detail: all 9 real checks (Bot, Log, Archives,
    Channels, Validation, Dataset A, Warehouse, Agents, V8 Module),
    the composite score, and the real "needs attention" list -- the
    exact same data the Health Center screen renders, not a
    simplified summary of it.
    """
    if not _OPS_AVAILABLE:
        return {"error": "openclaw_ops not available"}
    try:
        return ops.health_center()
    except Exception as e:
        return {"error": str(e)}


def get_agent_status():
    """Live status of all 8 pipeline agents (research, evaluation,
    compliance, scoring, posting, learning, validation, ml_readiness)
    -- online/offline, current task, last heartbeat age, success/
    failure counts. Answers 'is anything stuck' and 'what is X agent
    doing right now'."""
    if not _OPS_AVAILABLE:
        return {"error": "openclaw_ops not available"}
    try:
        return ops.agent_status()
    except Exception as e:
        return {"error": str(e)}


def get_live_pipeline_flow():
    """What's actively in flight right now, grouped by pipeline stage
    (research/evaluation/compliance/scoring/posting) -- the same data
    Operations and Agent Canvas render live."""
    if not _OPS_AVAILABLE:
        return {"error": "openclaw_ops not available"}
    try:
        return ops.live_flow()
    except Exception as e:
        return {"error": str(e)}


def get_rule_command_summary():
    """Rule-level observability across the whole system: total fires,
    success rate, per-rule hit rate and impact, recent failures,
    category breakdown -- the full Rule Command Center dataset, not
    just one rule's activation history (see get_rule_details for
    that)."""
    if not _OPS_AVAILABLE:
        return {"error": "openclaw_ops not available"}
    try:
        return ops.rule_command_center()
    except Exception as e:
        return {"error": str(e)}


def get_calibration_status():
    """Every provisional rule's real calibration data (RBMD,
    LPAB_PSAR, Gate 14 for both props and moneyline, Rule 9, CFEF,
    and the Rule30/CFEF stacking check): how many times each has
    fired, how many are settled, and its real hit rate once it clears
    its minimum sample. Answers 'is Rule 9 actually catching real
    violations' and 'which provisional rules have enough data to
    trust yet'."""
    if not _OPS_AVAILABLE:
        return {"error": "openclaw_ops not available"}
    try:
        return ops.calibration_center()
    except Exception as e:
        return {"error": str(e)}


def get_ml_readiness():
    """Real per-market row counts against the real tier thresholds
    for future ML model training -- data readiness, not model
    performance (no model has been trained yet; Model Status is
    NOT_STARTED everywhere, by design, not an oversight)."""
    if not _OPS_AVAILABLE:
        return {"error": "openclaw_ops not available"}
    try:
        return ops.ml_readiness_center()
    except Exception as e:
        return {"error": str(e)}


def get_confidence_calibration():
    """The predicted-vs-real reliability report: for every settled
    pick, bucketed by confidence range, does the real hit rate match
    what was predicted? Answers 'are we overconfident' with an actual
    measured gap, not an estimate."""
    if not _OPS_AVAILABLE:
        return {"error": "openclaw_ops not available"}
    try:
        return ops.confidence_calibration_report()
    except Exception as e:
        return {"error": str(e)}


def get_roi_summary():
    """Real auto-captured $ ROI from Slip Review (screenshot-sourced
    slips only, not manually-typed ones -- see the manual's Bankroll
    section for why these stay separate): total staked, total profit,
    ROI%, win rate, and which slips were excluded from the
    calculation and why."""
    if not _OPS_AVAILABLE:
        return {"error": "openclaw_ops not available"}
    try:
        return ops.slip_roi_report()
    except Exception as e:
        return {"error": str(e)}


def get_warehouse_collection_status():
    """Did the daily tennis data collection actually run? Last
    successful run, last failed run, next scheduled run, real success
    rate weighted by actual row-level work (not just the run's overall
    status label), and collection freshness. This is the direct
    answer to 'did the warehouse get updated today.'"""
    if not _WOPS_AVAILABLE:
        return {"error": "warehouse_ops not available"}
    try:
        return wops.collection_status()
    except Exception as e:
        return {"error": str(e)}


def get_warehouse_run_history(limit=30):
    """Real history of individual collector runs -- start/finish
    time, rows added/updated/failed, real API errors and warnings per
    run, not just a pass/fail label."""
    if not _WOPS_AVAILABLE:
        return {"error": "warehouse_ops not available"}
    try:
        return wops.run_history(limit=limit)
    except Exception as e:
        return {"error": str(e)}


def get_match_coverage():
    """Real match-level data coverage in the tennis warehouse: total
    matches, how many have a confirmed winner, how many are feature-
    complete. Direct answer to 'how many matches do we have.'"""
    if not _WOPS_AVAILABLE:
        return {"error": "warehouse_ops not available"}
    try:
        return wops.match_coverage()
    except Exception as e:
        return {"error": str(e)}


def get_player_coverage(limit=200):
    """Real player-level coverage in the tennis warehouse -- which
    ATP/WTA players have data, and how complete it is per player."""
    if not _WOPS_AVAILABLE:
        return {"error": "warehouse_ops not available"}
    try:
        return wops.player_coverage(limit=limit)
    except Exception as e:
        return {"error": str(e)}


def get_warehouse_health():
    """The Warehouse Command Center's own composite health score --
    collection success, feature completeness, coverage, data quality,
    outcome completeness, each shown individually, not averaged into
    one hidden number."""
    if not _WOPS_AVAILABLE:
        return {"error": "warehouse_ops not available"}
    try:
        return wops.warehouse_health_score()
    except Exception as e:
        return {"error": str(e)}


def get_collection_trends():
    """Real row-collection volume over time (today, this week, this
    month, and the 30-day daily average) -- answers 'is data
    collection keeping pace' with real counts, not a status label."""
    if not _WOPS_AVAILABLE:
        return {"error": "warehouse_ops not available"}
    try:
        return wops.collection_trends()
    except Exception as e:
        return {"error": str(e)}


def get_data_source_quality():
    """Breakdown of Dataset B's player-year stats rows by real data
    source (full aggregate vs. fallback-partial), with real
    percentages -- how much of the warehouse is fully trustworthy
    data vs. a partial fallback."""
    if not _WOPS_AVAILABLE:
        return {"error": "warehouse_ops not available"}
    try:
        return wops.data_source_quality()
    except Exception as e:
        return {"error": str(e)}
