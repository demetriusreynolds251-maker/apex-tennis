#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OPENCLAW MISSION CONTROL — PHASE 1: COMMAND DECK (v1.0, 2026-07-10)
Built exactly to OPENCLAW_MISSION_CONTROL_ARCHITECTURE.md §4/§8/§11/§12.

DISCOVERY-FIRST: every path, channel, schedule, and file location is
reused from OpenClaw itself by importing openclaw_core and reading its
own constants (DATA_DIR, ARCHIVE_DIR, PROPOSALS_FILE, log paths...).
Nothing already known to the Mac Mini is asked for again. The ONLY
manual data is what only the operator knows: slips, bankroll, goals —
and goals get sensible defaults written on first run.

READ-ONLY against pipeline state. Writes only to data/ops_center/
(its own config + slip ledger + outcome overrides). The one pipeline
action is approve/reject, which shells out to the EXISTING CLI:
    python3 openclaw_core.py --approve N | --reject N

Run locally or over Tailscale SSH (identical, 80x24-safe):
    python3 openclaw_ops.py
"""
import json, os, re, subprocess, sys, time, threading
from datetime import datetime, date, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# ── DISCOVERY: reuse OpenClaw's own knowledge of itself ─────────────
os.environ.setdefault("OPS_IMPORT", "1")
import openclaw_core as oc          # paths, schedules, structures — all reused

DATA_DIR   = oc.DATA_DIR
ARCHIVE    = oc.ARCHIVE_DIR
ML_ARCHIVE = getattr(oc, "ML_ARCHIVE_DIR", ARCHIVE)
PROPOSALS  = getattr(oc, "PROPOSALS_FILE", DATA_DIR / "proposals" / "pending_proposals.json")
LOG_FILE   = DATA_DIR / "logs" / "discord_bot.log"
VALID_DIR  = DATA_DIR / "validation"
OPS_DIR    = DATA_DIR / "ops_center"; OPS_DIR.mkdir(exist_ok=True)
CONFIG     = OPS_DIR / "ops_config.json"
SLIPS      = OPS_DIR / "slip_ledger.json"
OUTCOMES   = OPS_DIR / "outcome_overrides.json"   # manual HIT/BUST when not auto-derivable
APPROVAL_LOG_FILE   = getattr(oc, "APPROVAL_LOG_FILE", DATA_DIR / "proposals" / "approval_history.json")
SIGNAL_HISTORY_FILE = getattr(oc, "SIGNAL_HISTORY_FILE", DATA_DIR / "learning" / "signal_history.json")
ML_SIGNAL_HISTORY_FILE = getattr(oc, "ML_SIGNAL_HISTORY_FILE", DATA_DIR / "learning" / "ml_signal_history.json")
LAUNCH_LABEL = "com.apex.discordbot"           # discovered from the deploy runbook

DEFAULT_CONFIG = {
    "green_goal": 100, "green_target_hit_rate": 0.70,
    "slip_goal": 100, "ml_goal": 100, "ml_target_hit_rate": 0.70,
    "starting_bankroll": 500.0, "target_bankroll": 500000.0,
    "near_target_band": 0.05,
}

def _jload(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default

def load_config():
    cfg = {**DEFAULT_CONFIG, **_jload(CONFIG, {})}
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(cfg, indent=2))   # zero-setup first run
    return cfg

# ── DATA LAYER (pure functions — tested headlessly) ─────────────────
def iter_picks(kind="props"):
    src = ARCHIVE if kind == "props" else ML_ARCHIVE
    pat = "picks_*.json" if kind == "props" else "ml_picks_*.json"
    files = sorted(src.glob(pat))
    if kind == "ml" and not files:                    # ML may share naming
        files = sorted(src.glob("picks_*.json"))
    for f in files:
        for p in _jload(f, []):
            p["_date"] = f.stem.split("_", 1)[-1]
            yield p

def _okey(p):
    return f"{p.get('player','?')}|{p.get('stat_type', p.get('market','ML'))}|{p.get('match_date', p.get('_date',''))}"

def outcome_of(p, overrides):
    return p.get("outcome") or overrides.get(_okey(p))   # archive first, manual second

def tracker(kind, cfg):
    """GREEN or MONEYLINE tracker — auto-derived, never merged (arch §8).

    Real fix (this session): the old version read picks_*.json /
    ml_picks_*.json via iter_picks() -- the same dead archive format
    already found and fixed for Pick Results. That format stopped
    being written a while back, which is the real reason these tiles
    showed flat 0/100 regardless of real activity. Same root cause,
    same fix: query evaluations directly in dataset_a.sqlite, same
    pattern every other function in this file already uses via
    _warehouse_query(). Manual outcome_overrides.json is still merged
    in on top of evaluations.outcome -- record_settled_outcome() (open
    item, not yet wired) means evaluations.outcome is genuinely NULL
    for many settled picks today; dropping the override merge would
    silently regress real, currently-relied-on functionality.

    Confirmed real (2026-07-28): dataset_a.sqlite has 13 real rows,
    all evaluation_type='PROP' -- zero MONEYLINE rows, because no
    moneyline pick has been run through the system yet, not because
    of a bug. This function is written to work correctly for ML the
    moment real moneyline evaluations start landing; it will
    correctly show 0/0/None (status "⚪") until then, same as GREEN
    would if no props had ever run.
    """
    eval_type = "PROP" if kind == "green" else "MONEYLINE"
    goal, target = (cfg["green_goal"], cfg["green_target_hit_rate"]) if kind == "green" \
        else (cfg["ml_goal"], cfg["ml_target_hit_rate"])

    rows = _warehouse_query("""
        SELECT player, market, outcome, final_confidence, created_at
        FROM evaluations
        WHERE evaluation_type = ? AND final_grade = 'GREEN'
        ORDER BY created_at ASC
    """, (eval_type,))

    overrides = _jload(OUTCOMES, {})

    def _okey(r):
        # Best available real key from the evaluations schema: no
        # match_date column exists here, so the date portion of
        # created_at is used instead -- same spirit as the old
        # picks_*.json fallback to _date when match_date was absent.
        d = (r.get("created_at") or "")[:10]
        return f"{r.get('player','?')}|{r.get('market','?')}|{d}"

    def _outcome(r):
        return r.get("outcome") or overrides.get(_okey(r))  # Dataset A first, manual second

    done = [(r, _outcome(r)) for r in rows]
    done = [(r, o) for r, o in done if o in ("HIT", "BUST")]
    wins = sum(1 for _, o in done if o == "HIT")
    losses = len(done) - wins

    cur = 0; longest = 0; last = None
    for _, o in done:
        if o == last or last is None: cur += 1
        else: cur = 1
        last = o; longest = max(longest, cur if o == "HIT" else longest)
    streak = (("W" if last == "HIT" else "L") + str(cur)) if done else "-"

    hit = wins / len(done) if done else None
    avg_conf = (sum(r.get("final_confidence") or 0 for r in rows) / len(rows)) if rows else 0

    if hit is None: status = "⚪"
    elif hit >= target: status = "✅"
    elif hit >= target - cfg["near_target_band"]: status = "🟡"
    else: status = "🔴"

    return {"completed": len(done), "goal": goal, "wins": wins, "losses": losses,
            "hit": hit, "status": status, "streak": streak, "longest": longest,
            "avg_conf": avg_conf, "pending": len(rows) - len(done)}

def slips(cfg):
    entries = _jload(SLIPS, [])
    staked = sum(e.get("stake", 0) for e in entries)
    profit = sum(e.get("profit", 0) for e in entries)
    return {"entries": entries, "n": len(entries), "goal": cfg["slip_goal"],
            "six": sum(1 for e in entries if e.get("result") == "6/6"),
            "five": sum(1 for e in entries if e.get("result") == "5/6"),
            "loss": sum(1 for e in entries if e.get("result") == "LOSS"),
            "roi": (profit / staked) if staked else 0, "profit": profit,
            "bankroll": cfg["starting_bankroll"] + profit}

def _check_manual_auto_overlap(date_s, stake, legs, payout):
    """
    SLIP-AUTOMATION-2026-09-27 (safety pass, priority 2 of 4, ahead of
    screenshot auto-confirm). The manual Bankroll-tab ledger (this file,
    SLIPS/slip_ledger.json) and the automatic Discord/Slip-Review
    pipeline (oc._load_slip_log() -> slip_roi_report(), below) are two
    separate stores with no shared identifier -- neither knows the
    other exists. If the same real-world slip gets entered into both
    (typed by hand here AND auto-tracked through Discord), it is
    counted twice across the dashboard's two bankroll panels. There is
    no slip_id to join on across the two schemas, so this is a
    best-effort fingerprint match on (date, stake, payout, leg count)
    against slip_roi_report()'s already-resolved auto slips -- coarse,
    but a same-day exact match on all three dollar/count fields is a
    real, worth-blocking coincidence, not likely to be two genuinely
    different slips. Returns the matching auto slip_id, or None.
    """
    try:
        auto = slip_roi_report()
    except Exception:
        return None   # fail-open on the read side -- never block a manual
                       # entry because the auto pipeline itself errored
    for s in auto.get("slips", []):
        if s.get("legs") != legs:
            continue
        if abs((s.get("stake") or 0) - stake) > 0.01:
            continue
        if abs((s.get("potential_payout") or 0) - payout) > 0.01:
            continue
        return s.get("slip_id")
    return None


def manual_auto_overlap_report():
    """
    SLIP-AUTOMATION-2026-09-27 (safety pass, priority 2 of 4). Read-time
    companion to _check_manual_auto_overlap(): that function only stops
    NEW manual entries from colliding with an auto-tracked slip; this
    scans every entry ALREADY in the manual ledger against every
    already-resolved auto slip, so anything double-entered before this
    fix existed still surfaces as a visible warning rather than quietly
    inflating both bankroll panels forever. Same fingerprint match
    (date, stake, payout, leg count) as _check_manual_auto_overlap.
    """
    manual = _jload(SLIPS, [])
    try:
        auto = slip_roi_report()
    except Exception:
        return []
    overlaps = []
    for m in manual:
        for s in auto.get("slips", []):
            if s.get("legs") != m.get("legs"):
                continue
            if abs((s.get("stake") or 0) - (m.get("stake") or 0)) > 0.01:
                continue
            if abs((s.get("potential_payout") or 0) - (m.get("payout") or 0)) > 0.01:
                continue
            overlaps.append({"manual_id": m.get("id"), "manual_date": m.get("date"),
                              "auto_slip_id": s.get("slip_id"), "stake": m.get("stake"),
                              "payout": m.get("payout"), "legs": m.get("legs")})
    return overlaps


def add_slip(date_s, stake, legs, result, payout):
    dup = _check_manual_auto_overlap(date_s, stake, legs, payout)
    if dup:
        raise ValueError(
            f"This looks like it may already be tracked automatically (auto "
            f"slip_id {dup} — same stake, payout, and leg count today). Not "
            f"added to the manual ledger to avoid double-counting. If this "
            f"really is a separate slip, re-check the numbers or add it with "
            f"a slightly different value; if it's the same slip, no action "
            f"needed — it's already counted in the Auto ROI panel.")
    entries = _jload(SLIPS, [])
    profit = round(payout - stake, 2)
    entries.append({"v": 1, "id": len(entries) + 1, "date": date_s,
                    "stake": stake, "legs": legs, "result": result,
                    "payout": payout, "profit": profit})
    SLIPS.write_text(json.dumps(entries, indent=2))
    return profit

def set_outcome(key, outcome):
    o = _jload(OUTCOMES, {}); o[key] = outcome
    OUTCOMES.write_text(json.dumps(o, indent=2))

def bar(frac, width):
    frac = max(0.0, min(1.0, frac or 0))
    full = int(frac * width)
    return "█" * full + "░" * (width - full)

def today_str():
    return datetime.now(oc.CT).strftime("%Y-%m-%d") if hasattr(oc, "CT") else date.today().isoformat()

# ── AGENT LAYER (Mission Control Priority 2, approved) ───────────────
# Reads agent_heartbeat directly from Dataset A's warehouse — per the
# operator's approved architectural direction, this is a real query
# against structured state, not log-regex inference. Read-only, same
# as everything else in this file.
import sqlite3

WAREHOUSE_DB = oc.DATA_DIR / "warehouse" / "dataset_a.sqlite"

AGENT_ORDER = ["research", "evaluation", "compliance", "scoring",
               "posting", "learning", "validation", "ml_readiness"]
AGENT_LABELS = {
    "research": "Research Agent", "evaluation": "Evaluation Agent",
    "compliance": "Compliance Agent", "scoring": "Scoring Agent",
    "posting": "Posting Agent", "learning": "Learning Agent",
    "validation": "Validation Agent", "ml_readiness": "ML Readiness Agent",
}
# How long since last_heartbeat before a claimed WORKING/IDLE status is
# actually stale and should display as BLOCKED/OFFLINE instead. Two
# thresholds because "no heartbeat ever" (never run) is a different
# operator signal than "was working, now stuck."
STALE_BLOCKED_SECONDS = 10 * 60     # was WORKING, no update in 10 min -> BLOCKED
STALE_OFFLINE_SECONDS = 24 * 60 * 60  # nothing in 24h -> OFFLINE (agent hasn't run at all today)

def _parse_iso(ts):
    """dataset_a_capture.py writes timestamps as UTC via
    datetime.now(timezone.utc).isoformat() -- always tz-aware. Returns
    a tz-aware datetime, or None if unparseable, so the caller never
    has to guess whether it needs to attach a timezone."""
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # defensive: treat naive as UTC
        return dt
    except Exception:
        return None

def agent_status():
    """Returns one dict per agent, in AGENT_ORDER, always — agents that
    have never sent a heartbeat still appear, as OFFLINE with zero
    counts, so Mission Control never silently omits a row."""
    rows_by_name = {}
    if WAREHOUSE_DB.exists():
        try:
            conn = sqlite3.connect(f"file:{WAREHOUSE_DB}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            for r in conn.execute("SELECT * FROM agent_heartbeat"):
                rows_by_name[r["agent_name"]] = dict(r)
            conn.close()
        except Exception:
            pass  # Mission Control stays read-only-safe: a locked or
                  # missing warehouse file degrades to "all OFFLINE",
                  # it never raises into the dashboard.

    # Bug fix (caught in testing before this ever touched the Mini):
    # comparing a Central-Time "now" against dataset_a_capture's UTC
    # heartbeat timestamps after stripping tzinfo silently lost the
    # offset -- ages came back negative, and a heartbeat stale by 20
    # minutes displayed as freshly WORKING instead of BLOCKED. Fixed by
    # staying tz-aware (UTC) on both sides of the subtraction instead
    # of stripping and mixing timezones.
    now = datetime.now(timezone.utc)
    out = []
    for name in AGENT_ORDER:
        row = rows_by_name.get(name)
        if not row:
            out.append({"agent": name, "label": AGENT_LABELS[name], "status": "OFFLINE",
                        "current_task": None, "last_heartbeat": None, "age_seconds": None,
                        "success_count": 0, "failure_count": 0, "health": "⚪",
                        "note": "no heartbeat recorded yet"})
            continue
        ts = _parse_iso(row["last_heartbeat"])
        age = (now - ts).total_seconds() if ts else None
        display_status = row["status"]
        note = None
        if age is not None:
            if age > STALE_OFFLINE_SECONDS:
                display_status, note = "OFFLINE", f"no heartbeat in {int(age/3600)}h"
            elif display_status == "WORKING" and age > STALE_BLOCKED_SECONDS:
                display_status, note = "BLOCKED", f"stuck {int(age/60)}min on last task"
        health = {"IDLE": "🟢", "WORKING": "🟢", "BLOCKED": "🔴",
                   "FAILED": "🔴", "OFFLINE": "⚪"}.get(display_status, "⚪")
        out.append({"agent": name, "label": AGENT_LABELS[name], "status": display_status,
                    "current_task": row.get("current_task"), "last_heartbeat": row["last_heartbeat"],
                    "age_seconds": age, "success_count": row.get("success_count", 0),
                    "failure_count": row.get("failure_count", 0), "health": health, "note": note})
    return out

# ── AGENT CANVAS (Mission Control Priority 3, approved) ──────────────
# "Observed OpenClaw, not imagined OpenClaw" — every field below reads
# real Dataset A / existing-file state. Panels that can't yet be fully
# live (Learning, Validation) are labeled with exactly which future
# priority completes them, per the operator's explicit ruling: real
# partial data over empty placeholders, honestly labeled, never faked.

# lifecycle_state -> which agent currently holds the pick. The state
# name records what just finished, which is also who has it now.
LIFECYCLE_TO_AGENT = {
    "RESEARCH_STARTED":    "research",
    "RESEARCH_COMPLETE":   "evaluation",
    "EVALUATION_COMPLETE": "compliance",
    "COMPLIANCE_COMPLETE": "scoring",
    "SCORING_COMPLETE":    "posting",
}

def _warehouse_query(sql, params=()):
    """Shared read-only connector for every Canvas query. Same
    fail-safe contract as agent_status(): a locked or missing warehouse
    file degrades to an empty result, never raises into the dashboard."""
    if not WAREHOUSE_DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{WAREHOUSE_DB}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params)]
        conn.close()
        return rows
    except Exception:
        return []

def live_flow(limit=20):
    """Picks currently in flight, grouped by which agent has them.
    Returns {agent_name: [pick, ...]} so the Canvas can render one
    column per agent without doing the grouping itself."""
    rows = _warehouse_query("""
        SELECT eval_id, player, market, line, direction, lifecycle_state,
               lifecycle_updated_at
        FROM evaluations
        WHERE lifecycle_state IN
            ('RESEARCH_STARTED','RESEARCH_COMPLETE','EVALUATION_COMPLETE',
             'COMPLIANCE_COMPLETE','SCORING_COMPLETE')
        ORDER BY lifecycle_updated_at DESC LIMIT ?
    """, (limit,))
    by_agent = {a: [] for a in ("research", "evaluation", "compliance", "scoring", "posting")}
    for r in rows:
        agent = LIFECYCLE_TO_AGENT.get(r["lifecycle_state"])
        if agent:
            by_agent[agent].append(r)
    return by_agent

def rule_overlay_for(eval_id=None):
    """Structured rule state for one pick -- the eval_id the operator
    is currently looking at, or (if none selected) the single most
    recently updated evaluation, so the Canvas never shows an empty
    overlay when the pipeline is idle but has recent history."""
    if eval_id:
        rows = _warehouse_query(
            "SELECT * FROM evaluations WHERE eval_id = ?", (eval_id,))
    else:
        rows = _warehouse_query(
            "SELECT * FROM evaluations ORDER BY lifecycle_updated_at DESC LIMIT 1")
    if not rows:
        return None
    r = rows[0]
    echo = r.get("feature_echo_flags")
    echo_detected = bool(echo and echo != "[]" and echo != "null")
    return {
        "eval_id": r["eval_id"], "player": r.get("player"),
        "gate11": r.get("gate11_status") or "—",
        "gate11b": r.get("gate11b_dominance_key") or "—",
        "rule24": "ACTIVE" if r.get("rule24_triggered") else "—",
        "rule30": "ACTIVE" if r.get("rule30_triggered") else "—",
        "truth_lock": r.get("truth_lock_status") or "—",
        "dominance": r.get("dominance_level") or "—",
        "reqs": r.get("req_count"),
        "feature_echo": "DETECTED" if echo_detected else "clean",
    }

def learning_panel():
    """Real data only. Discoveries and rule-effectiveness are Priority
    5 scope (rule_discovery_check() currently only logs text, doesn't
    write structured rows) -- labeled as such, never fabricated."""
    agents = {a["agent"]: a for a in agent_status()}
    hb = agents.get("learning", {})
    pending = pending_proposals()
    last_proposal_ts = None
    for p in pending:
        for key in ("created_at", "date", "ts", "timestamp"):
            if p.get(key):
                last_proposal_ts = p[key]; break
        if last_proposal_ts:
            break
    return {
        "status": hb.get("status", "OFFLINE"), "health": hb.get("health", "⚪"),
        "current_task": hb.get("current_task"), "last_heartbeat": hb.get("last_heartbeat"),
        "success_count": hb.get("success_count", 0), "failure_count": hb.get("failure_count", 0),
        "pending_proposals": len(pending), "last_proposal": last_proposal_ts,
        "discoveries_note": "Not yet captured — rule_discovery_check() has no structured storage today, see Learning Center",
        "effectiveness_note": "See Learning Center for signal performance and trend",
    }

def validation_panel():
    """Real data only. Live per-pick validation tracking is Priority 8
    scope (green_validation_suite.py would need to call
    d1.record_validation() per replayed pick -- not wired). This shows
    the latest completed run's real numbers, clearly dated."""
    agents = {a["agent"]: a for a in agent_status()}
    hb = agents.get("validation", {})
    hist = _jload(VALID_DIR / "run_history.json", [])
    latest = hist[-1] if hist else None
    return {
        "status": hb.get("status", "OFFLINE"), "health": hb.get("health", "⚪"),
        "latest_run": latest,
        "live_tracking_note": "Live validation tracking scheduled for Validation Center (Priority 8)",
    }

# ── CONFIDENCE AUDIT CENTER (Mission Control Priority 4, approved) ───
# Everything here reads evaluations.confidence_ledger / eval_confidence
# / final_confidence directly -- no log parsing, no keyword matching.

def confidence_calibration_report():
    """
    THE report requested after the numeric-weights research pass: for
    every settled evaluation (outcome HIT or BUST -- PUSH/VOID excluded,
    same convention as green_validation_suite.py's own hit_rate()),
    bucket by final_confidence using the SAME edges
    confidence_audit_aggregate() already uses (so the two screens read
    consistently), and compare predicted confidence against REAL hit
    rate in each bucket. This is what confidence_audit_aggregate()
    does NOT do -- that function tracks eval->final MOVEMENT (a ledger-
    integrity question); this tracks final_confidence against OUTCOME
    (a calibration question) -- genuinely different questions, both
    worth having, which is why this lives alongside it on the same
    screen rather than replacing it.

    Each bucket reports: how many settled, real hit rate, average
    predicted confidence within the bucket, and the gap between them
    (predicted minus real -- positive means overconfident, negative
    means underconfident). No verdict is rendered here on whether a
    gap is "acceptable" -- that's a judgment call for a human looking
    at real numbers, not something to silently threshold in code.
    """
    rows = _warehouse_query("""
        SELECT final_confidence, outcome, final_grade, market
        FROM evaluations
        WHERE outcome IN ('HIT','BUST') AND final_confidence IS NOT NULL
    """)
    edges  = [0, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
    labels = ["<60%", "60-65%", "65-70%", "70-75%", "75-80%", "80%+"]

    buckets = [{"label": labels[i], "n": 0, "hits": 0, "conf_sum": 0.0}
               for i in range(len(labels))]
    for r in rows:
        fc = r.get("final_confidence") or 0
        for i in range(len(edges) - 1):
            if edges[i] <= fc < edges[i + 1]:
                b = buckets[i]
                b["n"] += 1
                b["conf_sum"] += fc
                if r.get("outcome") == "HIT":
                    b["hits"] += 1
                break

    result = []
    for b in buckets:
        if b["n"] == 0:
            result.append({"label": b["label"], "n": 0, "real_hit_rate": None,
                           "avg_predicted": None, "gap": None})
            continue
        real_hit_rate = b["hits"] / b["n"]
        avg_predicted = b["conf_sum"] / b["n"]
        result.append({
            "label": b["label"], "n": b["n"],
            "real_hit_rate": real_hit_rate,
            "avg_predicted": avg_predicted,
            "gap": avg_predicted - real_hit_rate,
        })

    total_settled = sum(b["n"] for b in buckets)
    total_hits    = sum(b["hits"] for b in buckets)
    return {
        "buckets": result,
        "total_settled": total_settled,
        "overall_hit_rate": (total_hits / total_settled) if total_settled else None,
    }


def confidence_audit_aggregate():
    """Aggregate confidence view across every scored evaluation.
    Answers: on average, how much does confidence move between eval and
    final, broken out by grade and by compliance category -- and how
    often does the empty-ledger defect (moved score, no cited cause)
    actually fire."""
    rows = _warehouse_query("""
        SELECT final_grade, compliance_category, eval_confidence,
               final_confidence, ledger_empty_flag
        FROM evaluations
        WHERE final_confidence IS NOT NULL
    """)
    if not rows:
        return {"n": 0, "by_grade": [], "by_compliance": [], "defect_rate": None,
                "distribution": [], "avg_delta": None}

    n = len(rows)
    defects = sum(1 for r in rows if r.get("ledger_empty_flag"))

    def _bucket(rows_, key):
        agg = {}
        for r in rows_:
            k = r.get(key) or "—"
            b = agg.setdefault(k, {"n": 0, "eval_sum": 0.0, "final_sum": 0.0})
            b["n"] += 1
            b["eval_sum"] += r.get("eval_confidence") or 0
            b["final_sum"] += r.get("final_confidence") or 0
        out = []
        for k, b in sorted(agg.items(), key=lambda kv: -kv[1]["n"]):
            avg_eval = b["eval_sum"] / b["n"]
            avg_final = b["final_sum"] / b["n"]
            out.append({"key": k, "n": b["n"], "avg_eval": avg_eval,
                        "avg_final": avg_final, "avg_delta": avg_final - avg_eval})
        return out

    deltas = [(r.get("final_confidence") or 0) - (r.get("eval_confidence") or 0) for r in rows]
    avg_delta = sum(deltas) / n

    edges = [0, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
    labels = ["<60%", "60-65%", "65-70%", "70-75%", "75-80%", "80%+"]
    dist = [0] * (len(edges) - 1)
    for r in rows:
        fc = r.get("final_confidence") or 0
        for i in range(len(edges) - 1):
            if edges[i] <= fc < edges[i + 1]:
                dist[i] += 1; break
    distribution = [{"label": labels[i], "n": dist[i]} for i in range(len(labels))]

    return {"n": n, "avg_delta": avg_delta,
            "defect_rate": defects / n, "defect_count": defects,
            "by_grade": _bucket(rows, "final_grade"),
            "by_compliance": _bucket(rows, "compliance_category"),
            "distribution": distribution}

def recent_scored(limit=25):
    """Most recently scored evaluations for the per-pick list -- ordered
    by lifecycle_updated_at, includes eval_id so the frontend can open
    full detail straight from Dataset A (see pick_from_eval), not the
    archive-file lookup that can't see picks the archive never wrote."""
    return _warehouse_query("""
        SELECT eval_id, player, market, line, direction, eval_confidence,
               final_confidence, final_grade, ledger_empty_flag,
               compliance_category, lifecycle_updated_at
        FROM evaluations
        WHERE final_confidence IS NOT NULL
        ORDER BY lifecycle_updated_at DESC LIMIT ?
    """, (limit,))

def pick_from_eval(eval_id):
    """Full per-pick detail sourced directly from Dataset A by eval_id --
    works for ANY evaluation regardless of whether it was ever archived
    or posted (Rule-28-excluded GREENs included). Same output shape as
    the existing archive-based pick_detail() in openclaw_mission_control.py
    so the Inspector UI can render either source with the same code."""
    rows = _warehouse_query("SELECT * FROM evaluations WHERE eval_id = ?", (eval_id,))
    if not rows:
        return {"error": "evaluation not found"}
    r = rows[0]
    ledger = []
    if r.get("confidence_ledger"):
        try: ledger = json.loads(r["confidence_ledger"])
        except Exception: ledger = []
    flow = [f"eval {(r.get('eval_confidence') or 0):.0%}"]
    for item in ledger:
        if isinstance(item, dict):
            flow.append(f"{item.get('cause','?')} {item.get('delta',0):+.0%}" if isinstance(item.get('delta'), (int, float)) else str(item))
        else:
            flow.append(str(item))
    flow.append(f"FINAL {(r.get('final_confidence') or 0):.0%} {r.get('final_grade') or '?'}")
    alerts = []
    if r.get("ledger_empty_flag"):
        alerts.append("UNEXPLAINED confidence change — empty ledger — HIGH-SEVERITY defect")
    flags = []
    if r.get("compliance_notes"):
        try: flags = json.loads(r["compliance_notes"])
        except Exception: flags = []
    echo = r.get("feature_echo_flags")
    echo_detected = bool(echo and echo not in ("[]", "null", None))
    return {
        "pick": {"player": r.get("player"), "opponent": r.get("opponent"),
                 "line": r.get("line"), "stat_type": r.get("market"),
                 "direction": r.get("direction"), "confidence": r.get("final_confidence"),
                 "color": r.get("final_grade")},
        "flow": flow, "alerts": alerts, "flags": flags,
        "status": {"Gate 11": r.get("gate11_status") or "—",
                   "Truth Lock": r.get("truth_lock_status") or "—",
                   "Rule 24": "ACTIVE" if r.get("rule24_triggered") else "—",
                   "Rule 30": "ACTIVE" if r.get("rule30_triggered") else "—",
                   "Dominance": r.get("dominance_level") or "—",
                   "REQs": f"{r.get('req_count', '?')}/4",
                   "Feature Echo": "DETECTED" if echo_detected else "clean",
                   "Validation": r.get("validation_verdict") or "not yet validated",
                   "Outcome": r.get("outcome") or r.get("lifecycle_state") or "PENDING"},
        "reasoning": r.get("reasoning_text") or "",
        "source": "dataset_a",
    }

# ── RULE COMMAND CENTER (Mission Control Priority 5, approved) ───────
# Honest split: 4 of the 8 requested rules have real write sites in the
# current openclaw_core.py patch and get real aggregate stats below.
# The other 4 (Rule24, Rule30, Truth Lock, Feature Echo) are schema
# columns with zero write sites today -- confirmed by grep, not
# assumed. Rather than show fabricated zero-activation counts, those
# render with an explicit "not yet captured" status and the reason,
# per the operator's standing rule against faked live functionality.
CAPTURED_RULES = {
    "Gate 11": {"field": "gate11_status", "kind": "text",
                "note": "Only the unverified branch is currently written -- "
                        "the normal PASS path was never wired, so PASS rate "
                        "can't be distinguished from never-checked yet."},
    "Gate 11-B": {"field": "gate11b_dominance_key", "kind": "text", "note": None},
    "REQs": {"field": "req_count", "kind": "numeric", "note": None},
    "Compliance": {"field": "compliance_category", "kind": "text", "note": None},
    "Rule 24": {"field": "rule24_triggered", "kind": "bool",
                "note": "Captured as of this session -- deterministic "
                        "rank-gap check (_gap >= 100), recomputed "
                        "independently from research_data at RESEARCH_COMPLETE."},
    "Rule 30": {"field": "rule30_triggered", "kind": "bool",
                "note": "Captured after full trace report confirmed this is a "
                        "compliance flag (check #9), not the Moneyline S4 "
                        "turnaround calculation -- derived only from "
                        "compliance_flags rule=='Rule30', per operator's "
                        "explicit instruction to exclude any proxy signal."},
    "Truth Lock": {"field": "truth_lock_status", "kind": "text",
                   "note": "Captured as of this session -- reads the "
                           "confirmed 'Rule_TruthLock' compliance flag name directly."},
    "Feature Echo": {"field": "feature_echo_flags", "kind": "json_list",
                      "note": "Captured as of this session -- same keyword "
                              "detection Mission Control used to re-derive on "
                              "every read, now computed once at write-time."},
}
UNCAPTURED_RULES = {}

def slip_roi_report():
    """
    Real $ ROI (this session, closes the gap flagged since the original
    handoff: "no dollar amount is tracked anywhere in the slip log, for
    any market -- true $ ROI doesn't exist yet even in aggregate"). Now
    that stake/potential_payout are captured (Slip Review, earlier this
    session), this is the report that actually computes it.

    Groups oc._load_slip_log()'s flat per-leg entries by slip_id (a
    PrizePicks-style slip has one stake/payout across multiple legs,
    denormalized onto each leg's row -- same convention the capture
    layer already uses). A slip only counts toward real ROI if it can
    be cleanly resolved:
      - every leg has a real stake/potential_payout (older typed-entry
        slips, before this session, won't -- excluded, not guessed at)
      - every leg is a clean WIN or LOSS (no PENDING, PUSH, VOID, or
        REJECTED_CORRELATION leg in the slip)
    All legs WIN -> profit = potential_payout - stake (the slip paid
    out in full). Any leg LOSS -> loss = -stake (Power-Play-style
    all-or-nothing; this system has no data on PARTIAL/Flex payouts
    for a slip that hit some but not all legs, so a slip with a mixed
    WIN/LOSS result that isn't a clean full-loss is excluded rather
    than guessed at -- see excluded_reasons).
    """
    entries = oc._load_slip_log()
    by_slip = {}
    for e in entries:
        by_slip.setdefault(e.get("slip_id"), []).append(e)

    resolved_slips = []
    excluded = {"pending": 0, "missing_dollar_data": 0, "push_or_void": 0,
                "rejected_correlation": 0, "ambiguous_mixed_result": 0}

    for slip_id, legs in by_slip.items():
        if slip_id is None:
            continue
        statuses = [l.get("status") for l in legs]
        if any(s == "PENDING" for s in statuses):
            excluded["pending"] += 1
            continue
        if any(s == "REJECTED_CORRELATION" for s in statuses):
            excluded["rejected_correlation"] += 1
            continue
        if any(s in ("PUSH", "VOID") for s in statuses):
            excluded["push_or_void"] += 1
            continue
        if any(l.get("stake") is None or l.get("potential_payout") is None for l in legs):
            excluded["missing_dollar_data"] += 1
            continue
        wins  = sum(1 for s in statuses if s == "WIN")
        losses = sum(1 for s in statuses if s == "LOSS")
        if wins and losses:
            # A real mixed result on a slip this system logged as one
            # all-or-nothing unit -- can't cleanly resolve without
            # knowing this specific slip's partial-payout structure.
            excluded["ambiguous_mixed_result"] += 1
            continue
        stake   = legs[0].get("stake")
        payout  = legs[0].get("potential_payout")
        all_win = losses == 0
        profit  = (payout - stake) if all_win else -stake
        resolved_slips.append({
            "slip_id": slip_id, "legs": len(legs), "stake": stake,
            "potential_payout": payout, "result": "WIN" if all_win else "LOSS",
            "profit": profit,
        })

    total_staked = sum(s["stake"] for s in resolved_slips)
    total_profit = sum(s["profit"] for s in resolved_slips)
    wins = sum(1 for s in resolved_slips if s["result"] == "WIN")
    return {
        "slips": resolved_slips,
        "total_slips_resolved": len(resolved_slips),
        "total_staked": total_staked,
        "total_profit": total_profit,
        "roi_pct": (total_profit / total_staked) if total_staked else None,
        "win_count": wins,
        "loss_count": len(resolved_slips) - wins,
        "win_rate": (wins / len(resolved_slips)) if resolved_slips else None,
        "excluded": excluded,
        "total_excluded": sum(excluded.values()),
    }


def calibration_center():
    """
    Calibration Center — read-only view of the PROVISIONAL_RULES
    tracking oc.calibration_status_report() already builds (RBMD,
    LPAB_PSAR, Gate14 home-court lean, the 4 ML signal bands, the
    compliance per-flag penalty). Deliberately calls into
    openclaw_core directly rather than re-reading the calibration log
    file a second time -- unlike ate_queries.py's CT-constant
    duplication (justified there because ate_queries.py normally
    doesn't import openclaw_core at all), this module already does
    `import openclaw_core as oc` at the top for other reasons, so
    there's no dependency-weight argument for duplicating the logic
    here instead of calling it.

    Reshapes the rule_name-keyed dict into a sorted list (most-fired
    first) so the frontend can render a straightforward table instead
    of iterating dict keys, and separates out `_unregistered_rules_seen`
    (a rule that fired before anyone added it to PROVISIONAL_RULES) as
    its own top-level list rather than mixing it into the per-rule rows.
    """
    try:
        report = oc.calibration_status_report()
    except Exception as e:
        return {"error": str(e), "rules": [], "unregistered": [],
                "total_rules": 0, "ready_for_review": 0}
    unregistered = report.pop("_unregistered_rules_seen", [])
    rules = [{"rule_name": k, **v} for k, v in report.items()]
    rules.sort(key=lambda r: r["fired_count"], reverse=True)
    ready = sum(1 for r in rules if r["status"] == "READY_FOR_REVIEW")
    return {
        "rules": rules,
        "unregistered": unregistered,
        "total_rules": len(rules),
        "ready_for_review": ready,
    }

def rule_command_center():
    rows = _warehouse_query("""
        SELECT gate11_status, gate11b_dominance_key, req_count,
               compliance_category, rule24_triggered, rule30_triggered,
               truth_lock_status, feature_echo_flags, final_confidence,
               eval_confidence, outcome
        FROM evaluations WHERE final_confidence IS NOT NULL
    """)
    n = len(rows)

    def _display_value(field, raw):
        if raw is None:
            return "—"
        if field in ("rule24_triggered", "rule30_triggered"):
            return "ACTIVE" if raw else "—"
        if field == "feature_echo_flags":
            try:
                echoes = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                echoes = []
            return "DETECTED: " + ",".join(echoes) if echoes else "clean"
        return str(raw)

    def _rule_stats(field):
        agg = {}
        for r in rows:
            k = _display_value(field, r.get(field))
            b = agg.setdefault(k, {"n": 0, "hits": 0, "known": 0,
                                    "eval_sum": 0.0, "final_sum": 0.0})
            b["n"] += 1
            b["eval_sum"] += r.get("eval_confidence") or 0
            b["final_sum"] += r.get("final_confidence") or 0
            if r.get("outcome") in ("HIT", "BUST"):
                b["known"] += 1
                if r.get("outcome") == "HIT": b["hits"] += 1
        out = []
        for k, b in sorted(agg.items(), key=lambda kv: -kv[1]["n"]):
            out.append({
                "value": k, "n": b["n"],
                "hit_rate": (b["hits"] / b["known"]) if b["known"] else None,
                "known": b["known"],
                "avg_delta": (b["final_sum"] - b["eval_sum"]) / b["n"] if b["n"] else 0,
            })
        return out

    captured = []
    for label, spec in CAPTURED_RULES.items():
        captured.append({"label": label, "captured": True, "note": spec["note"],
                         "usage_count": n, "breakdown": _rule_stats(spec["field"])})
    uncaptured = [{"label": label, "captured": False, "note": note}
                  for label, note in UNCAPTURED_RULES.items()]
    return {"n": n, "captured": captured, "uncaptured": uncaptured}

# ── LEARNING CENTER (Mission Control Priority 6, approved) ───────────
# Built strictly on what the Learning System Trace confirmed as real,
# structured, already-persisted data: pending_proposals.json,
# approval_history.json, signal_history.json / ml_signal_history.json.
# Recent Discoveries is deliberately NOT built here -- rule_discovery_
# check() has zero storage today (confirmed by trace, no file write
# anywhere in that function). Fabricating a discoveries panel from log
# scraping would violate the same "structured truth over inference"
# standard applied to Rule 30.

def proposal_history(limit=50):
    """ALL proposals regardless of status -- pending_proposals()
    already existed and only returns PENDING; this reads the full file
    (PENDING/APPROVED/REJECTED) for the Learning Center's activity view."""
    all_p = _jload(PROPOSALS, [])
    if isinstance(all_p, dict) and "proposals" in all_p:
        all_p = all_p["proposals"]  # old single-batch format, same migration _load_proposals() does
    all_p = sorted(all_p, key=lambda p: p.get("id", 0), reverse=True)
    counts = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0}
    for p in all_p:
        counts[p.get("status", "PENDING")] = counts.get(p.get("status", "PENDING"), 0) + 1
    return {"proposals": all_p[:limit], "counts": counts, "total": len(all_p)}

def approval_log(limit=30):
    """Decision history from approval_history.json -- never read by
    anything in Mission Control before this."""
    hist = _jload(APPROVAL_LOG_FILE, [])
    return sorted(hist, key=lambda h: h.get("decided_at", ""), reverse=True)[:limit]

def signal_trend():
    """Per-signal hit rate from signal_history.json / ml_signal_history.json
    (confirmed real, GREEN-only gated per the trace) -- split into an
    earlier half and a recent half of each signal's entries so
    improving/degrading is an actual comparison, not a single aggregate
    number pretending to show a trend."""
    def _trend_for(path, kind):
        hist = _jload(path, [])
        by_signal = {}
        for h in hist:
            by_signal.setdefault(h.get("signal", "?"), []).append(h.get("outcome"))
        out = []
        for signal, outcomes in by_signal.items():
            outcomes = [o for o in outcomes if o in ("HIT", "BUST")]
            if not outcomes:
                continue
            n = len(outcomes)
            hits = outcomes.count("HIT")
            hit_rate = hits / n
            half = max(1, n // 2)
            earlier, recent = outcomes[:n - half] or outcomes[:1], outcomes[n - half:]
            early_rate = earlier.count("HIT") / len(earlier) if earlier else None
            recent_rate = recent.count("HIT") / len(recent) if recent else None
            trend = "—"
            if early_rate is not None and recent_rate is not None and n >= 4:
                delta = recent_rate - early_rate
                trend = "IMPROVING" if delta > 0.05 else ("DEGRADING" if delta < -0.05 else "STABLE")
            out.append({"signal": signal, "kind": kind, "n": n, "hit_rate": hit_rate,
                        "early_rate": early_rate, "recent_rate": recent_rate, "trend": trend})
        return out
    return sorted(
        _trend_for(SIGNAL_HISTORY_FILE, "prop") + _trend_for(ML_SIGNAL_HISTORY_FILE, "moneyline"),
        key=lambda s: -s["n"]
    )

# ── MONEYLINE INTAKE (Mission Control, approved after Dataset A trace) ─
# openclaw_intake.py's internals weren't part of this session's uploads,
# so rather than guess at its queue format, this is a dedicated, simple
# moneyline queue reusing the confirmed --moneyline CLI path (line
# ~9264 in openclaw_core.py) -- that path already mints an eval_id and
# prints parseable JSON to stdout, so this worker just shells out and
# reads the result back, same subprocess pattern decide_proposal()
# already uses successfully for --approve/--reject.
ML_INTAKE_FILE = OPS_DIR / "ml_intake_queue.json"
_ml_worker_started = False
_ml_worker_lock = threading.Lock()

def _ml_load():
    return _jload(ML_INTAKE_FILE, [])

def _ml_save(q):
    ML_INTAKE_FILE.write_text(json.dumps(q, indent=2, default=str))

def submit_moneyline(player_a, player_b, surface="hard", tour="atp"):
    """Queues a moneyline evaluation. Returns the intake id immediately;
    the actual evaluation runs in the background worker since it
    involves a live LLM call and can take longer than an HTTP request
    should block for."""
    q = _ml_load()
    next_id = max((e.get("id", 0) for e in q), default=0) + 1
    entry = {"id": next_id, "player_a": player_a, "player_b": player_b,
              "surface": surface or "hard", "tour": (tour or "atp").lower(),
              "status": "QUEUED", "queued_at": today_str(),
              "eval_id": None, "result_status": None, "error": None}
    q.append(entry)
    _ml_save(q)
    _ensure_ml_worker()
    return next_id

def _ml_worker_loop():
    while True:
        q = _ml_load()
        pending = [e for e in q if e["status"] == "QUEUED"]
        if not pending:
            time.sleep(2)
            continue
        entry = pending[0]
        entry["status"] = "RUNNING"
        for e in q:
            if e["id"] == entry["id"]:
                e["status"] = "RUNNING"
        _ml_save(q)
        raw = f"{entry['player_a']} | {entry['player_b']} | {entry['surface']} | {entry['tour']}"
        try:
            r = subprocess.run(
                [sys.executable, str(HERE / "openclaw_core.py"), "--moneyline", raw],
                capture_output=True, text=True, timeout=180
            )
            m = re.search(r"\{.*\}", r.stdout, re.DOTALL)
            parsed = json.loads(m.group(0)) if m else {}
            entry["eval_id"] = parsed.get("eval_id")
            entry["result_status"] = parsed.get("status")
            if r.returncode != 0 or parsed.get("status") in ("ERROR", "SYSTEM_FAILURE"):
                entry["status"] = "FAILED"
                entry["error"] = parsed.get("reason") or (r.stderr[-500:] if r.stderr else "unknown error")
            else:
                entry["status"] = "DONE"
        except subprocess.TimeoutExpired:
            entry["status"] = "FAILED"
            entry["error"] = "timed out after 180s"
        except Exception as e:
            entry["status"] = "FAILED"
            entry["error"] = str(e)
        q = _ml_load()
        for e in q:
            if e["id"] == entry["id"]:
                e.update(entry)
        _ml_save(q)

def _ensure_ml_worker():
    global _ml_worker_started
    with _ml_worker_lock:
        if not _ml_worker_started:
            threading.Thread(target=_ml_worker_loop, daemon=True).start()
            _ml_worker_started = True

def ml_intake_queue():
    """Adds a computed display_status per the operator's requested
    states (Queued/Running/Failed/NO_PLAY/Posted) -- DONE alone is
    ambiguous between a NO_PLAY and an actually-posted GREEN/YELLOW,
    so it's resolved here rather than left for the frontend to guess."""
    out = []
    for e in sorted(_ml_load(), key=lambda x: x["id"], reverse=True):
        if e["status"] in ("QUEUED", "RUNNING", "FAILED"):
            display = e["status"]
        elif e.get("result_status") == "NO_PLAY":
            display = "NO_PLAY"
        elif e.get("result_status") in ("GREEN", "YELLOW"):
            display = "POSTED"
        else:
            display = e["status"]
        out.append({**e, "display_status": display})
    return out

# ── VALIDATION CENTER EXPANSION (Mission Control, approved) ──────────
# green_validation_suite.py (the file that writes run_history.json)
# was not part of this session's uploads, so this works only from the
# real, confirmed field set already used by the existing Validation
# screen: ts, replay_green, green_known, green_hits, preserved, lost,
# created, win_demoted, lose_promoted. Two things this CANNOT honestly
# compute from that field set alone -- flagged rather than faked:
#   - Calibration (confidence-bucketed accuracy) -- no confidence-band
#     breakdown exists in run_history.json today.
#   - "Benign-direction" promotions/demotions -- only the concerning
#     movements (win_demoted, lose_promoted) are tracked; a winner
#     promoted further or a loser demoted further isn't separately
#     counted anywhere.
def validation_workspace():
    hist = _jload(VALID_DIR / "run_history.json", [])
    if not hist:
        return {"n_runs": 0, "runs": [], "trend": None, "latest": None,
                "calibration_note": "No validation runs recorded yet.",
                "promotion_note": None}

    runs = []
    for h in hist:
        known = h.get("green_known", 0) or 0
        hits = h.get("green_hits", 0) or 0
        runs.append({**h, "hit_rate": (hits / known) if known else None})

    # Trend: compare the most recent run against the average of all
    # prior runs -- same "earlier half vs recent half" honesty standard
    # used for signal_trend() in Learning Center, not a single number
    # dressed up as a trend.
    trend = None
    if len(runs) >= 2:
        rates = [r["hit_rate"] for r in runs if r["hit_rate"] is not None]
        if len(rates) >= 2:
            latest_rate = rates[-1]
            prior_avg = sum(rates[:-1]) / len(rates[:-1])
            delta = latest_rate - prior_avg
            trend = {"latest_rate": latest_rate, "prior_avg": prior_avg, "delta": delta,
                     "direction": "IMPROVING" if delta > 0.03 else ("DEGRADING" if delta < -0.03 else "STABLE")}

    total_preserved = sum(r.get("preserved", 0) or 0 for r in runs)
    total_lost = sum(r.get("lost", 0) or 0 for r in runs)
    total_created = sum(r.get("created", 0) or 0 for r in runs)
    total_win_demoted = sum(r.get("win_demoted", 0) or 0 for r in runs)
    total_lose_promoted = sum(r.get("lose_promoted", 0) or 0 for r in runs)

    # Real per-pick Dataset A verdicts (write-back approved this
    # session) -- separate from the run_history.json aggregate above,
    # which only ever had run-level counts. This is the actual
    # validation_verdict column, queried live. win_demoted/lose_promoted
    # here are DERIVED (verdict + outcome), never stored as their own
    # verdict value, per the approved normalized model.
    verdict_counts = _warehouse_query(
        "SELECT validation_verdict, COUNT(*) as n FROM evaluations "
        "WHERE validation_verdict IS NOT NULL GROUP BY validation_verdict")
    win_demoted_real = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE validation_verdict='LOST' AND outcome='HIT'")[0]["n"]
    lose_promoted_real = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE validation_verdict='CREATED' AND outcome='BUST'")[0]["n"]
    latest_run_id = _warehouse_query(
        "SELECT validation_run_id FROM evaluations WHERE validation_run_id IS NOT NULL "
        "ORDER BY lifecycle_updated_at DESC LIMIT 1")
    latest_run_id = latest_run_id[0]["validation_run_id"] if latest_run_id else None

    return {
        "n_runs": len(runs), "runs": list(reversed(runs))[:20], "trend": trend,
        "latest": runs[-1] if runs else None,
        "totals": {"preserved": total_preserved, "lost": total_lost, "created": total_created,
                   "win_demoted": total_win_demoted, "lose_promoted": total_lose_promoted},
        "dataset_a_verdicts": {
            "by_verdict": verdict_counts, "win_demoted": win_demoted_real,
            "lose_promoted": lose_promoted_real, "latest_run_id": latest_run_id,
        },
        "calibration_note": "Not computable from run_history.json today -- "
                             "no confidence-bucketed accuracy data exists. Would "
                             "need green_validation_suite.py to bucket outcomes "
                             "by confidence band per run.",
        "promotion_note": "Only the concerning direction is tracked: win_demoted "
                           "(a HIT that got demoted under new scoring) and "
                           "lose_promoted (a BUST that got promoted). The benign "
                           "directions aren't separately counted anywhere today.",
    }

# ── HEALTH CENTER (Mission Control, approved after trace) ────────────
# Reuses health() unchanged (the 5 existing checks are legitimate) and
# adds only signals confirmed real and observable today: Dataset A
# content counts, warehouse file health, and an agent_status() rollup.
# V8 is shown strictly as the boot-time capability flag it actually is
# -- never "online"/"responding", per the trace finding that
# _V8_AVAILABLE is never updated after process startup.

def _dataset_a_health():
    if not WAREHOUSE_DB.exists():
        return {"exists": False, "last_update": None, "eval_count": 0,
                "lifecycle_count": 0, "heartbeat_count": 0, "read_ok": False}
    try:
        conn = sqlite3.connect(f"file:{WAREHOUSE_DB}?mode=ro", uri=True, timeout=5)
        eval_count = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
        lifecycle_count = conn.execute("SELECT COUNT(*) FROM lifecycle_log").fetchone()[0]
        heartbeat_count = conn.execute("SELECT COUNT(*) FROM agent_heartbeat").fetchone()[0]
        last_update = conn.execute(
            "SELECT MAX(lifecycle_updated_at) FROM evaluations"
        ).fetchone()[0]
        conn.close()
        return {"exists": True, "last_update": last_update, "eval_count": eval_count,
                "lifecycle_count": lifecycle_count, "heartbeat_count": heartbeat_count,
                "read_ok": True}
    except Exception as e:
        return {"exists": True, "last_update": None, "eval_count": None,
                "lifecycle_count": None, "heartbeat_count": None,
                "read_ok": False, "error": str(e)}

def _warehouse_file_health():
    if not WAREHOUSE_DB.exists():
        return {"exists": False, "last_update": None, "row_counts": {}, "read_ok": False}
    try:
        stat = WAREHOUSE_DB.stat()
        conn = sqlite3.connect(f"file:{WAREHOUSE_DB}?mode=ro", uri=True, timeout=5)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        row_counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        conn.close()
        return {"exists": True, "last_update": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "row_counts": row_counts, "read_ok": True}
    except Exception as e:
        return {"exists": True, "last_update": None, "row_counts": {}, "read_ok": False, "error": str(e)}

def _agent_rollup():
    agents = agent_status()
    counts = {"WORKING": 0, "IDLE": 0, "BLOCKED": 0, "FAILED": 0, "OFFLINE": 0}
    problem_agents = []
    for a in agents:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
        if a["status"] in ("BLOCKED", "FAILED"):
            problem_agents.append({"agent": a["agent"], "status": a["status"], "note": a.get("note")})
    healthy = counts["WORKING"] + counts["IDLE"]
    return {"counts": counts, "healthy": healthy, "blocked": counts["BLOCKED"],
             "failed": counts["FAILED"], "offline": counts["OFFLINE"],
             "problem_agents": problem_agents}

def health_center():
    checks, base_score = health()  # existing 5 checks, unchanged, reused not duplicated
    ds_a = _dataset_a_health()
    warehouse = _warehouse_file_health()
    agents = _agent_rollup()
    v8_available = bool(getattr(oc, "_V8_AVAILABLE", False))

    # Extend the existing checks dict with the new real signals rather
    # than maintaining a second parallel scoring system.
    extended_checks = dict(checks)
    extended_checks["Dataset A"] = "🟢" if ds_a["read_ok"] else ("🟡" if ds_a["exists"] else "🔴")
    extended_checks["Warehouse"] = "🟢" if warehouse["read_ok"] else ("🟡" if warehouse["exists"] else "🔴")
    extended_checks["Agents"] = ("🔴" if agents["failed"] or agents["blocked"]
                                  else ("🟡" if agents["offline"] == len(agent_status()) else "🟢"))
    extended_checks["V8 Module"] = "🟢" if v8_available else "🔴"

    score = int(100 * sum(1 for v in extended_checks.values() if v == "🟢") / len(extended_checks))

    attention = []
    if extended_checks.get("Bot") == "🔴":
        attention.append("Bot process not found in launchctl — is the LaunchAgent loaded?")
    if extended_checks.get("Log") == "🔴":
        attention.append("Log file stale or missing — bot may not be running")
    if not v8_available:
        attention.append("V8 module was not present at last boot — evaluations run without V8 data")
    for pa in agents["problem_agents"]:
        attention.append(f"{pa['agent']} agent is {pa['status']}" + (f" — {pa['note']}" if pa.get("note") else ""))
    if not ds_a["read_ok"]:
        attention.append("Dataset A warehouse is not readable")

    return {
        "checks": extended_checks, "score": score,
        "dataset_a": ds_a, "warehouse": warehouse, "agents": agents,
        "v8_available": v8_available,
        "attention": attention,
        "intake_worker_note": "Not shown — openclaw_intake.py's internals aren't "
                               "confirmed to expose a reliable liveness signal. "
                               "A missing metric here is intentional, not an oversight.",
    }

# ── ML READINESS CENTER (Mission Control, approved after trace) ─────
# Real per-market counts, real tier thresholds (the exact numbers
# approved earlier this project), real math. Model Status is NOT
# STARTED everywhere -- confirmed by trace: no LightGBM, no training
# pipeline, no model storage exists anywhere in the codebase. Fantasy
# Score is BLOCKED BY DESIGN, not "0% collecting" -- confirmed at
# openclaw_core.py line ~1332, every fantasy_score prop is explicitly
# skipped before evaluation ever starts.

PROP_TIERS = [
    (0, "COLLECTING"), (100, "EXPLORATION"), (250, "PROTOTYPE READY"),
    (500, "CHALLENGER READY"), (1000, "ADVANCED"), (2500, "MATURE"),
]
ML_TIERS = [
    (0, "COLLECTING"), (1000, "PROTOTYPE READY"), (5000, "CHALLENGER READY"),
    (10000, "ADVANCED"), (25000, "MATURE"), (50000, "ELITE DATASET"),
]
# APEX-HOTFIX-2026-09-28 FIX #4: fantasy_score stopped being skipped on
# 2026-08-27 (see openclaw_core.py "APEX-WIRING-2026-08-27" comment) --
# it now belongs in the real-market list like everything else here, so
# both readiness functions below compute it from real Dataset A queries
# instead of a hardcoded BLOCKED BY DESIGN stub.
REAL_PROP_MARKETS = {
    "double_faults": "Double Faults", "aces": "Aces", "games_won": "Games Won",
    "break_points_won": "Break Points", "total_games": "Total Games",
    "total_sets": "Total Sets", "fantasy_score": "Fantasy Score",
}

def _tier_for(count, tiers):
    current = tiers[0][1]
    next_threshold = tiers[1][0] if len(tiers) > 1 else None
    for i, (threshold, label) in enumerate(tiers):
        if count >= threshold:
            current = label
            next_threshold = tiers[i + 1][0] if i + 1 < len(tiers) else None
        else:
            break
    return current, next_threshold

def ml_readiness_center():
    rows = _warehouse_query(
        "SELECT market, evaluation_type, COUNT(*) as n FROM evaluations "
        "GROUP BY market, evaluation_type"
    )
    prop_counts = {m: 0 for m in REAL_PROP_MARKETS}
    ml_count = 0
    for r in rows:
        if r["evaluation_type"] == "MONEYLINE":
            ml_count += r["n"]
        elif r["market"] in prop_counts:
            prop_counts[r["market"]] += r["n"]

    markets = []
    for market_key, label in REAL_PROP_MARKETS.items():
        count = prop_counts[market_key]
        tier, next_threshold = _tier_for(count, PROP_TIERS)
        readiness_pct = min(100, round(100 * count / next_threshold)) if next_threshold else 100
        markets.append({"market": label, "count": count, "tier": tier,
                        "next_threshold": next_threshold, "readiness_pct": readiness_pct,
                        "model_status": "NOT STARTED", "blocked": False})

    tier, next_threshold = _tier_for(ml_count, ML_TIERS)
    readiness_pct = min(100, round(100 * ml_count / next_threshold)) if next_threshold else 100
    markets.append({"market": "Moneyline", "count": ml_count, "tier": tier,
                    "next_threshold": next_threshold, "readiness_pct": readiness_pct,
                    "model_status": "NOT STARTED", "blocked": False})


    return {
        "markets": markets,
        "infrastructure_status": "ML model training not yet started (no LightGBM package or "
                                  "training pipeline built). Dataset B is collecting real data daily "
                                  "and is the foundation for future model training.",
        "dataset_b_status": "Running — tennis_warehouse.sqlite collecting since 2026-07-19. "
                             "1,812+ matches, 199 players, daily collector at 06:00 AM CT. "
                             "Use Warehouse Command to see full collector run history and stats.",
    }

# ── DATA WAREHOUSE CENTER (Mission Control, approved) ─────────────────
# "Show what OpenClaw KNOWS, not what OpenClaw is doing." Every number
# below is a real query against Dataset A. Per operator's explicit
# principle: NO classifications, rankings, effectiveness scores, or
# verdicts that rely on insufficient sample size. Raw counts only where
# the operator specifically asked for raw counts (Rule Activation).

REAL_MARKETS_FULL = {**REAL_PROP_MARKETS, "moneyline": "Moneyline"}

def data_sufficiency():
    """Top-of-page panel. Status text is fixed, not threshold-computed
    -- operator explicitly said not to guess a threshold yet."""
    total = _warehouse_query("SELECT COUNT(*) as n FROM evaluations")[0]["n"]
    settled = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE outcome IN ('HIT','BUST')")[0]["n"]
    return {
        "total_evaluations": total, "total_outcomes": settled,
        "outcome_completion_pct": (settled / total) if total else None,
        "rule_audit_status": "NOT YET STATISTICALLY MEANINGFUL",
    }

def knowledge_snapshot():
    total = _warehouse_query("SELECT COUNT(*) as n FROM evaluations")[0]["n"]
    by_type = {r["evaluation_type"]: r["n"] for r in _warehouse_query(
        "SELECT evaluation_type, COUNT(*) as n FROM evaluations GROUP BY evaluation_type")}
    outcomes = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE outcome IN ('HIT','BUST')")[0]["n"]
    pending = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE final_grade IS NOT NULL AND outcome IS NULL")[0]["n"]
    no_play = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE lifecycle_state='NO_PLAY'")[0]["n"]
    failed = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE lifecycle_state IN ('FAILED','REJECTED')")[0]["n"]
    validated = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE validation_verdict IS NOT NULL")[0]["n"]
    players = _warehouse_query(
        "SELECT COUNT(DISTINCT p) as n FROM (SELECT player as p FROM evaluations "
        "UNION SELECT opponent as p FROM evaluations) WHERE p IS NOT NULL AND p != ''")[0]["n"]
    markets = _warehouse_query(
        "SELECT COUNT(DISTINCT market) as n FROM evaluations WHERE market IS NOT NULL")[0]["n"]
    quality = _data_quality_pct()
    today = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE date(created_at)=date('now')")[0]["n"]
    week = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE date(created_at)>=date('now','-7 days')")[0]["n"]
    month = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE date(created_at)>=date('now','-30 days')")[0]["n"]
    learning_records = 0
    try:
        learning_records = (len(_jload(SIGNAL_HISTORY_FILE, [])) +
                            len(_jload(ML_SIGNAL_HISTORY_FILE, [])))
    except Exception:
        pass
    return {
        "total_evaluations": total, "total_props": by_type.get("PROP", 0),
        "total_moneylines": by_type.get("MONEYLINE", 0), "total_outcomes": outcomes,
        "total_pending": pending, "total_no_play": no_play, "total_failed": failed,
        "total_validation_records": validated, "total_learning_records": learning_records,
        "total_players": players, "total_markets": markets,
        "outcome_completion_pct": (outcomes / total) if total else None,
        "data_quality_pct": quality,
        "growth_today": today, "growth_week": week, "growth_month": month,
    }

def _data_quality_pct():
    """% of rows with no defects among the checks defined in
    data_quality_center() -- shared so knowledge_snapshot()'s headline
    number and the detail panel never disagree."""
    total = _warehouse_query("SELECT COUNT(*) as n FROM evaluations")[0]["n"]
    if not total:
        return None
    defects = _warehouse_query("""
        SELECT COUNT(*) as n FROM evaluations WHERE
            (evaluation_type='PROP' AND (opponent IS NULL OR opponent=''))
            OR surface IS NULL
            OR (evaluation_type='PROP' AND (market IS NULL OR market=''))
    """)[0]["n"]
    return max(0.0, 1 - (defects / total))

def market_coverage_full():
    out = []
    for market_key, label in REAL_PROP_MARKETS.items():
        rows = _warehouse_query(
            "SELECT COUNT(*) as n, "
            "SUM(CASE WHEN outcome IN ('HIT','BUST') THEN 1 ELSE 0 END) as with_outcome, "
            "MIN(created_at) as first_date, MAX(created_at) as last_date, "
            "COUNT(DISTINCT player) as players, COUNT(DISTINCT opponent) as opponents, "
            "COUNT(DISTINCT surface) as surfaces, COUNT(DISTINCT tour) as tours "
            "FROM evaluations WHERE market=?", (market_key,))[0]
        count = rows["n"] or 0
        with_outcome = rows["with_outcome"] or 0
        tier, next_threshold = _tier_for(count, PROP_TIERS)
        readiness = min(100, round(100 * count / next_threshold)) if next_threshold else 100
        out.append({
            "market": label, "count": count, "with_outcome": with_outcome,
            "pending": count - with_outcome,
            "outcome_completion_pct": (with_outcome / count) if count else None,
            "readiness_pct": readiness, "tier": tier,
            "first_date": rows["first_date"], "last_date": rows["last_date"],
            "players_covered": rows["players"] or 0, "opponents_covered": rows["opponents"] or 0,
            "surfaces": rows["surfaces"] or 0, "tours": rows["tours"] or 0,
            "model_status": "NOT STARTED", "blocked": False,
        })
    ml_rows = _warehouse_query(
        "SELECT COUNT(*) as n, "
        "SUM(CASE WHEN outcome IN ('HIT','BUST') THEN 1 ELSE 0 END) as with_outcome, "
        "MIN(created_at) as first_date, MAX(created_at) as last_date, "
        "COUNT(DISTINCT player) as players, COUNT(DISTINCT opponent) as opponents, "
        "COUNT(DISTINCT surface) as surfaces, COUNT(DISTINCT tour) as tours "
        "FROM evaluations WHERE evaluation_type='MONEYLINE'")[0]
    ml_count = ml_rows["n"] or 0
    ml_with_outcome = ml_rows["with_outcome"] or 0
    ml_tier, ml_next = _tier_for(ml_count, ML_TIERS)
    ml_readiness = min(100, round(100 * ml_count / ml_next)) if ml_next else 100
    out.append({
        "market": "Moneyline", "count": ml_count, "with_outcome": ml_with_outcome,
        "pending": ml_count - ml_with_outcome,
        "outcome_completion_pct": (ml_with_outcome / ml_count) if ml_count else None,
        "readiness_pct": ml_readiness, "tier": ml_tier,
        "first_date": ml_rows["first_date"], "last_date": ml_rows["last_date"],
        "players_covered": ml_rows["players"] or 0, "opponents_covered": ml_rows["opponents"] or 0,
        "surfaces": ml_rows["surfaces"] or 0, "tours": ml_rows["tours"] or 0,
        "model_status": "NOT STARTED", "blocked": False,
    })
    return sorted(out, key=lambda m: -m["count"])  # ranking = sort order, most→least collected

def player_coverage(limit=50):
    rows = _warehouse_query("""
        SELECT p as player, COUNT(*) as rows_n,
               SUM(CASE WHEN outcome='HIT' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='BUST' THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN final_grade IS NOT NULL AND outcome IS NULL THEN 1 ELSE 0 END) as pending,
               COUNT(DISTINCT market) as markets
        FROM (
            SELECT player as p, outcome, final_grade, market FROM evaluations WHERE player IS NOT NULL AND player != ''
            UNION ALL
            SELECT opponent as p, outcome, final_grade, market FROM evaluations WHERE opponent IS NOT NULL AND opponent != ''
        )
        GROUP BY p ORDER BY rows_n DESC LIMIT ?
    """, (limit,))
    out = []
    for r in rows:
        known = (r["wins"] or 0) + (r["losses"] or 0)
        out.append({**r, "outcome_completion_pct": (known / r["rows_n"]) if r["rows_n"] else None})
    return out

def player_detail(player_name):
    by_market = _warehouse_query("""
        SELECT market, COUNT(*) as n FROM evaluations
        WHERE (player=? OR opponent=?) AND market IS NOT NULL GROUP BY market
    """, (player_name, player_name))
    ml_rows = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE (player=? OR opponent=?) AND evaluation_type='MONEYLINE'",
        (player_name, player_name))[0]["n"]
    prop_rows = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE (player=? OR opponent=?) AND evaluation_type='PROP'",
        (player_name, player_name))[0]["n"]
    outcomes = _warehouse_query("""
        SELECT outcome, COUNT(*) as n FROM evaluations
        WHERE (player=? OR opponent=?) AND outcome IS NOT NULL GROUP BY outcome
    """, (player_name, player_name))
    surfaces = _warehouse_query("""
        SELECT surface, COUNT(*) as n FROM evaluations
        WHERE (player=? OR opponent=?) AND surface IS NOT NULL GROUP BY surface
    """, (player_name, player_name))
    tournaments = _warehouse_query("""
        SELECT tournament, COUNT(*) as n FROM evaluations
        WHERE (player=? OR opponent=?) AND tournament IS NOT NULL GROUP BY tournament
    """, (player_name, player_name))
    dates = _warehouse_query(
        "SELECT MIN(created_at) as first, MAX(created_at) as last FROM evaluations WHERE player=? OR opponent=?",
        (player_name, player_name))[0]
    total = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE player=? OR opponent=?",
        (player_name, player_name))[0]["n"]
    known = sum(o["n"] for o in outcomes if o["outcome"] in ("HIT", "BUST"))
    return {
        "player": player_name, "total_records": total, "moneyline_rows": ml_rows, "prop_rows": prop_rows,
        "by_market": by_market, "outcomes": outcomes, "surfaces": surfaces, "tournaments": tournaments,
        "date_range": dates, "outcome_completion_pct": (known / total) if total else None,
    }

def surface_coverage():
    total = _warehouse_query("SELECT COUNT(*) as n FROM evaluations")[0]["n"]
    rows = _warehouse_query(
        "SELECT COALESCE(surface,'Unknown') as surface, COUNT(*) as n FROM evaluations GROUP BY surface")
    return [{**r, "pct": (r["n"] / total) if total else None} for r in rows]

def tour_coverage():
    total = _warehouse_query("SELECT COUNT(*) as n FROM evaluations")[0]["n"]
    rows = _warehouse_query(
        "SELECT COALESCE(tour,'Unknown') as tour, COUNT(*) as n FROM evaluations GROUP BY tour")
    return [{**r, "pct": (r["n"] / total) if total else None} for r in rows]

def time_distribution():
    windows = {"today": "date(created_at)=date('now')",
               "week": "date(created_at)>=date('now','-7 days')",
               "month": "date(created_at)>=date('now','-30 days')",
               "last_90d": "date(created_at)>=date('now','-90 days')"}
    out = {}
    for label, clause in windows.items():
        out[label] = _warehouse_query(f"SELECT COUNT(*) as n FROM evaluations WHERE {clause}")[0]["n"]
    out["avg_per_day_30d"] = round(out["month"] / 30, 2)
    return out

def outcome_coverage_full():
    total = _warehouse_query("SELECT COUNT(*) as n FROM evaluations")[0]["n"]
    by_outcome = _warehouse_query(
        "SELECT COALESCE(outcome, lifecycle_state) as bucket, COUNT(*) as n "
        "FROM evaluations GROUP BY bucket")
    by_market = _warehouse_query(
        "SELECT market, outcome, COUNT(*) as n FROM evaluations "
        "WHERE market IS NOT NULL AND outcome IS NOT NULL GROUP BY market, outcome")
    by_type = _warehouse_query(
        "SELECT evaluation_type, outcome, COUNT(*) as n FROM evaluations "
        "WHERE outcome IS NOT NULL GROUP BY evaluation_type, outcome")
    return {"total": total,
            "by_outcome": [{**r, "pct": (r["n"] / total) if total else None} for r in by_outcome],
            "by_market": by_market, "by_type": by_type}

def top_data_problems():
    total = _warehouse_query("SELECT COUNT(*) as n FROM evaluations")[0]["n"]
    checks = {
        "Missing Outcome (posted, settled long enough ago)": (
            "lifecycle_state='POSTED' AND outcome IS NULL AND "
            "lifecycle_updated_at < datetime('now','-3 days')"),
        "Missing Surface": "surface IS NULL",
        "Missing Opponent": "(opponent IS NULL OR opponent='')",
        "Missing Market (props)": "evaluation_type='PROP' AND (market IS NULL OR market='')",
        "Stalled Evaluations (>24h non-terminal)": (
            "lifecycle_state IN ('RESEARCH_STARTED','RESEARCH_COMPLETE','EVALUATION_COMPLETE',"
            "'COMPLIANCE_COMPLETE','SCORING_COMPLETE') AND lifecycle_updated_at < datetime('now','-1 day')"),
    }
    out = []
    for label, clause in checks.items():
        n = _warehouse_query(f"SELECT COUNT(*) as n FROM evaluations WHERE {clause}")[0]["n"]
        out.append({"problem": label, "count": n, "pct": (n / total) if total else None})
    return sorted(out, key=lambda p: -p["count"])

def data_quality_center():
    total = _warehouse_query("SELECT COUNT(*) as n FROM evaluations")[0]["n"]
    checks = {
        "missing_opponent": "(opponent IS NULL OR opponent='')",
        "missing_surface": "surface IS NULL",
        "missing_outcome": ("lifecycle_state='POSTED' AND outcome IS NULL AND "
                            "lifecycle_updated_at < datetime('now','-3 days')"),
        "missing_market": "evaluation_type='PROP' AND (market IS NULL OR market='')",
        "stalled_evaluations": (
            "lifecycle_state IN ('RESEARCH_STARTED','RESEARCH_COMPLETE','EVALUATION_COMPLETE',"
            "'COMPLIANCE_COMPLETE','SCORING_COMPLETE') AND lifecycle_updated_at < datetime('now','-1 day')"),
    }
    out = {}
    for key, clause in checks.items():
        n = _warehouse_query(f"SELECT COUNT(*) as n FROM evaluations WHERE {clause}")[0]["n"]
        out[key] = {"count": n, "pct": (n / total) if total else None}
    # near-duplicate detection: same player+opponent+market+line+date, more than one row
    dupes = _warehouse_query("""
        SELECT player, opponent, market, line, date(created_at) as d, COUNT(*) as n
        FROM evaluations WHERE evaluation_type='PROP'
        GROUP BY player, opponent, market, line, d HAVING COUNT(*) > 1
    """)
    out["duplicate_like"] = {"count": sum(d["n"] - 1 for d in dupes), "groups": len(dupes)}
    return out

def ml_training_forecast():
    """Gated: only returns real forecasts once >=14 days of collection
    history exist. Below that, returns the honest 'not available' state
    rather than a forecast built on a handful of points."""
    history_days = _warehouse_query(
        "SELECT COUNT(DISTINCT date(created_at)) as n FROM evaluations")[0]["n"]
    if history_days < 14:
        return {"available": False,
                "reason": "No meaningful forecast available yet.",
                "days_of_history": history_days, "days_required": 14}
    forecasts = []
    for market_key, label in REAL_PROP_MARKETS.items():
        current = _warehouse_query(
            "SELECT COUNT(*) as n FROM evaluations WHERE market=?", (market_key,))[0]["n"]
        last_30 = _warehouse_query(
            "SELECT COUNT(*) as n FROM evaluations WHERE market=? AND "
            "date(created_at)>=date('now','-30 days')", (market_key,))[0]["n"]
        per_day = last_30 / 30
        tier, next_threshold = _tier_for(current, PROP_TIERS)
        if next_threshold and per_day > 0:
            days_to_target = round((next_threshold - current) / per_day)
        else:
            days_to_target = None
        forecasts.append({"market": label, "current": current, "target": next_threshold,
                          "per_day": round(per_day, 2), "days_to_target": days_to_target})
    return {"available": True, "forecasts": forecasts}

def dataset_a_coverage_audit():
    """Real population/null rate for every column -- exactly the field
    audit requested, no hardcoded subset."""
    total = _warehouse_query("SELECT COUNT(*) as n FROM evaluations")[0]["n"]
    if not WAREHOUSE_DB.exists():
        return {"total": 0, "fields": []}
    try:
        conn = sqlite3.connect(f"file:{WAREHOUSE_DB}?mode=ro", uri=True, timeout=5)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(evaluations)")]
        fields = []
        for c in cols:
            populated = conn.execute(
                f"SELECT COUNT(*) FROM evaluations WHERE {c} IS NOT NULL AND {c} != ''"
            ).fetchone()[0] if total else 0
            fields.append({
                "field": c, "populated": populated, "null": total - populated,
                "population_pct": (populated / total) if total else None,
            })
        conn.close()
    except Exception:
        fields = []
    highlight = {"compliance_cost", "pipeline_version", "classification", "dominance_level",
                "termination_reason", "termination_detail", "tournament", "round",
                "match_scheduled_time"}
    for f in fields:
        f["flagged_for_review"] = f["field"] in highlight
    return {"total": total, "fields": fields}

def rule_audit_infrastructure():
    """Objective counts ONLY -- no ACTIVE/RARE/DORMANT/DEAD labels, no
    verdicts, per operator's explicit instruction. Numbers speak for
    themselves; classification waits for real distributions."""
    def _rule_activation(field, label):
        rows = _warehouse_query(f"""
            SELECT {field} as val, COUNT(*) as n,
                   SUM(CASE WHEN outcome='HIT' THEN 1 ELSE 0 END) as hits,
                   SUM(CASE WHEN outcome IN ('HIT','BUST') THEN 1 ELSE 0 END) as known,
                   MAX(lifecycle_updated_at) as last_activation,
                   AVG(final_confidence - eval_confidence) as avg_delta
            FROM evaluations WHERE {field}=1 OR {field}='ACTIVE'
        """) if field in ("rule24_triggered", "rule30_triggered") else []
        if not rows or not rows[0]["n"]:
            return {"rule": label, "activation_count": 0, "known_outcomes": 0,
                    "hit_rate": None, "last_activation": None, "avg_confidence_delta": None}
        r = rows[0]
        return {"rule": label, "activation_count": r["n"], "known_outcomes": r["known"] or 0,
                "hit_rate": (r["hits"] / r["known"]) if r["known"] else None,
                "last_activation": r["last_activation"],
                "avg_confidence_delta": r["avg_delta"]}

    rule_activation = [
        _rule_activation("rule24_triggered", "Rule 24"),
        _rule_activation("rule30_triggered", "Rule 30"),
    ]
    tl = _warehouse_query("""
        SELECT truth_lock_status as val, COUNT(*) as n,
               SUM(CASE WHEN outcome='HIT' THEN 1 ELSE 0 END) as hits,
               SUM(CASE WHEN outcome IN ('HIT','BUST') THEN 1 ELSE 0 END) as known
        FROM evaluations WHERE truth_lock_status='FLAGGED'
    """)
    if tl and tl[0]["n"]:
        r = tl[0]
        rule_activation.append({"rule": "Truth Lock", "activation_count": r["n"],
                                "known_outcomes": r["known"] or 0,
                                "hit_rate": (r["hits"] / r["known"]) if r["known"] else None,
                                "last_activation": None, "avg_confidence_delta": None})
    else:
        rule_activation.append({"rule": "Truth Lock", "activation_count": 0, "known_outcomes": 0,
                                "hit_rate": None, "last_activation": None, "avg_confidence_delta": None})

    # Confidence conservation: empty-ledger defect rate, real, already
    # captured -- viewed historically here instead of per-pick.
    conf_rows = _warehouse_query(
        "SELECT COUNT(*) as n, SUM(ledger_empty_flag) as defects FROM evaluations "
        "WHERE final_confidence IS NOT NULL")[0]
    confidence_conservation = {
        "scored_evaluations": conf_rows["n"] or 0,
        "empty_ledger_defects": conf_rows["defects"] or 0,
        "defect_rate": ((conf_rows["defects"] or 0) / conf_rows["n"]) if conf_rows["n"] else None,
    }

    # Feature Echo frequency: real, real field.
    echo_rows = _warehouse_query(
        "SELECT feature_echo_flags FROM evaluations WHERE feature_echo_flags IS NOT NULL "
        "AND feature_echo_flags != '[]'")
    echo_count = len(echo_rows)
    total_scored = conf_rows["n"] or 0
    feature_echo = {"occurrences": echo_count, "scored_total": total_scored,
                    "rate": (echo_count / total_scored) if total_scored else None}

    # Rule interaction counts: how often Rule24 and Rule30 co-occur,
    # real, direct.
    interaction = _warehouse_query(
        "SELECT COUNT(*) as n FROM evaluations WHERE rule24_triggered=1 AND rule30_triggered=1")[0]["n"]

    # classification / dominance_level exposed now, per operator's
    # explicit "do not wait" instruction.
    classification_dist = _warehouse_query(
        "SELECT classification, COUNT(*) as n FROM evaluations WHERE classification IS NOT NULL "
        "GROUP BY classification")
    dominance_dist = _warehouse_query(
        "SELECT dominance_level, COUNT(*) as n FROM evaluations WHERE dominance_level IS NOT NULL "
        "GROUP BY dominance_level")

    return {
        "rule_activation": rule_activation,
        "confidence_conservation": confidence_conservation,
        "feature_echo": feature_echo,
        "rule24_and_rule30_cooccurrence": interaction,
        "classification_distribution": classification_dist,
        "dominance_distribution": dominance_dist,
    }

# ── LOG-DERIVED VIEWS: lanes, feed, health (arch §7/§11) ────────────
STAGES = ["Research", "Evaluation", "Compliance", "Scoring", "Post"]
_STAGE_PAT = {
    "Research":   re.compile(r"\[(Research|Gate ?11|V8)\]", re.I),
    "Evaluation": re.compile(r"\[Evaluation\]", re.I),
    "Compliance": re.compile(r"\[Compliance\]", re.I),
    "Scoring":    re.compile(r"\[Scoring\]", re.I),
    "Post":       re.compile(r"\[(Discord|Posting|Supervisor)\].*(post|card)", re.I),
}
_FAIL_PAT = re.compile(r"🛑|SYSTEM.FAILURE|ERROR|Traceback|failed", re.I)
_NAME_PAT = re.compile(r"—\s*([A-Z][a-z.'-]+(?:\s[A-Z][a-z.'-]+)+)")

def read_log_tail(n=400):
    if not LOG_FILE.exists():
        return []
    try:
        return LOG_FILE.read_text(errors="replace").splitlines()[-n:]
    except Exception:
        return []

def pipeline_lanes(lines):
    """Best-effort lanes from log phases: prop name -> stage states."""
    lanes = {}
    for ln in lines:
        m = _NAME_PAT.search(ln)
        if not m:
            continue
        name = m.group(1)[:22]
        lane = lanes.setdefault(name, {s: "⚪" for s in STAGES})
        for stage, pat in _STAGE_PAT.items():
            if pat.search(ln):
                for prev in STAGES[:STAGES.index(stage)]:
                    if lane[prev] == "⚪": lane[prev] = "🟢"
                lane[stage] = "🔴" if _FAIL_PAT.search(ln) else "🟡"
        if "posted" in ln.lower() or "✅" in ln:
            for s in STAGES: lane[s] = "🟢" if lane[s] != "🔴" else "🔴"
    return list(lanes.items())[-6:]

def live_feed(lines, n=8):
    keep = [l for l in lines if any(k in l for k in ("🛑", "⚠️", "✅", "🔔", "GREEN", "posted"))]
    return keep[-n:]

def health():
    checks = {}
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
        checks["Bot"] = "🟢" if LAUNCH_LABEL in out else "🔴"
    except Exception:
        checks["Bot"] = "⚪"
    if LOG_FILE.exists():
        age = datetime.now().timestamp() - LOG_FILE.stat().st_mtime
        checks["Log"] = "🟢" if age < 3600 else ("🟡" if age < 6 * 3600 else "🔴")
    else:
        checks["Log"] = "🔴"
    checks["Archives"] = "🟢" if ARCHIVE.exists() and os.access(ARCHIVE, os.R_OK) else "🔴"
    # webhook-key lint (the "systems" typo class, arch §11)
    try:
        src = (HERE / "openclaw_core.py").read_text(errors="replace")
        keys = set(re.findall(r'^\s+"(\w+)":\s+"https://discord', src, re.M))
        used = set(re.findall(r'send_discord\(\s*["\'](\w+)["\']', src))
        checks["Channels"] = "🟢" if used <= keys else "🔴"
    except Exception:
        checks["Channels"] = "⚪"
    checks["Validation"] = "🟢" if (VALID_DIR / "run_history.json").exists() else "🟡"
    score = int(100 * sum(1 for v in checks.values() if v == "🟢") / len(checks))
    return checks, score

def pending_proposals():
    return [p for p in _jload(PROPOSALS, []) if p.get("status") == "PENDING"]

def decide_proposal(pid, approve=True):
    flag = "--approve" if approve else "--reject"
    return subprocess.run([sys.executable, str(HERE / "openclaw_core.py"), flag, str(pid)],
                          capture_output=True, text=True, timeout=30)

# ── TUI (Textual) ───────────────────────────────────────────────────
from textual.app import App, ComposeResult
from textual.widgets import Static, Footer, Input
from textual.containers import Vertical

class Deck(Static):
    pass

class MissionControl(App):
    CSS = "Deck {height: auto; padding: 0 1;} #prompt {dock: bottom;}"
    BINDINGS = [("q", "quit", "quit"), ("r", "refresh", "refresh"),
                ("s", "slip", "add slip"), ("o", "outcome", "set outcome"),
                ("a", "approve", "approve #"), ("x", "reject", "reject #")]

    def compose(self) -> ComposeResult:
        yield Vertical(Deck(id="deck"))
        self.prompt = Input(placeholder="", id="prompt"); self.prompt.display = False
        yield self.prompt
        yield Footer()
        self._mode = None

    def on_mount(self):
        self.action_refresh()
        self.set_interval(5, self.action_refresh)

    def action_refresh(self):
        cfg = load_config()
        g, m, s = tracker("green", cfg), tracker("ml", cfg), slips(cfg)
        hs, score = health()
        lines = read_log_tail()
        W = 20
        g_hit = f"{g['hit']:.1%}" if g['hit'] is not None else " n/a "
        m_hit = f"{m['hit']:.1%}" if m['hit'] is not None else " n/a "
        rows = [f"[b]OPENCLAW MISSION CONTROL[/b]  {datetime.now().strftime('%H:%M:%S')}  health {score}%",
                "",
                f"GREENS  {g['completed']:>3}/{g['goal']:<4}{g['status']} {g_hit} "
                f"({g['wins']}-{g['losses']}) {bar(g['completed']/g['goal'], W)} "
                f"strk {g['streak']}  pend {g['pending']}  avg {g['avg_conf']:.0%}",
                f"SLIPS   {s['n']:>3}/{s['goal']:<4}ROI {s['roi']:+.0%}  net {s['profit']:+,.0f}  "
                f"{bar(s['n']/s['goal'], W)} 6/6:{s['six']} 5/6:{s['five']} L:{s['loss']}",
                f"MONEYLN {m['completed']:>3}/{m['goal']:<4}{m['status']} {m_hit} "
                f"({m['wins']}-{m['losses']}) {bar(m['completed']/m['goal'], W)} strk {m['streak']}",
                f"BANKROLL ${s['bankroll']:,.0f} / ${cfg['target_bankroll']:,.0f}  "
                f"{s['bankroll']/cfg['target_bankroll']:.2%} "
                f"{bar(s['bankroll']/cfg['target_bankroll'], 28)}",
                "", "[b]TONIGHT'S PIPELINE[/b]           R  E  C  S  P"]
        lanes = pipeline_lanes(lines)
        lane_rows = [f" {name:<26} " + "  ".join(state[s2] for s2 in STAGES)
                     for name, state in lanes]
        rows += lane_rows or [" (no active props in log window)"]
        rows += ["", "[b]LIVE FEED[/b]"]
        rows += [f" {l[-96:]}" for l in live_feed(lines)] or [" (quiet)"]
        pend = pending_proposals()
        rows += ["", f"[b]APPROVALS[/b]  🔔 {len(pend)} pending" if pend else "\n[b]APPROVALS[/b]  none pending"]
        rows += [f"  #{p['id']} {p.get('signal','?')} {p.get('current','?')}→{p.get('proposed','?')}"
                 for p in pend[:3]]
        rows += ["", "HEALTH  " + "  ".join(f"{k}{v}" for k, v in hs.items())]
        self.query_one("#deck", Deck).update("\n".join(rows))

    # ── operator inputs (the only manual data, arch §8) ─────────────
    def _ask(self, mode, placeholder):
        self._mode = mode
        self.prompt.placeholder = placeholder
        self.prompt.display = True
        self.prompt.focus()

    def action_slip(self):   self._ask("slip", "stake,legs,result(6/6|5/6|LOSS),payout  e.g. 20,6,6/6,520")
    def action_outcome(self): self._ask("outcome", "Player|stat_type|match_date|HIT or BUST")
    def action_approve(self): self._ask("approve", "proposal id to APPROVE")
    def action_reject(self):  self._ask("reject", "proposal id to REJECT")

    def on_input_submitted(self, ev):
        v, mode = ev.value.strip(), self._mode
        self.prompt.value = ""; self.prompt.display = False; self._mode = None
        try:
            if mode == "slip":
                stake, legs, result, payout = [x.strip() for x in v.split(",")]
                add_slip(today_str(), float(stake), int(legs), result.upper(), float(payout))
            elif mode == "outcome":
                player, stat, d, out = [x.strip() for x in v.split("|")]
                set_outcome(f"{player}|{stat}|{d}", out.upper())
            elif mode in ("approve", "reject"):
                decide_proposal(int(v), approve=(mode == "approve"))
        except Exception as e:
            self.notify(f"input error: {e}", severity="error")
        self.action_refresh()

if __name__ == "__main__":
    MissionControl().run()

# © 2026 Demetrius Reynolds — Project Apex Intelligence™. All rights reserved.
