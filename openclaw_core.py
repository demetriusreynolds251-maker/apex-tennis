"""
================================================================
OPENCLAW CORE v7.1
DEMETRIUS Tennis Prop Evaluation System
================================================================
Complete pipeline — all agents — all gates — all rules
Discord only. CT time only. Tomorrow's props always.
Learning on today's completed matches only.

PROP FETCHING:
Primary:  PrizePicks unofficial API (Scenario B)
Fallback: Playwright browser automation (Option A)
Manual:   Screenshot via Discord #general (Option C)

WEB SEARCH: Claude native tool inside agent sessions
Research verification: validate_research_source()
Format verification:   format_verification_check()
Rule discovery:        rule_discovery_check()
================================================================
"""

import json
import os
import re
import time
import base64
import difflib
import requests
import schedule
import pytz
from datetime import datetime, timedelta, date, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================================================================ APEX-FETCHCANCEL-2026-10-12
# owner, 2026-10-12: "Cancel this fetch, then create a cancel button".
# A cooperative stop flag for the running /fetch. The scrape checks it
# between PrizePicks tabs and the evaluation loop checks it between props,
# so a cancel lands within a few seconds instead of only after the whole
# run. Nothing is published once it is set; the active board is untouched.
import threading as _fc_threading
FETCH_CANCEL = _fc_threading.Event()

def request_fetch_cancel():
    """Ask the running /fetch to stop at its next checkpoint."""
    FETCH_CANCEL.set()

def clear_fetch_cancel():
    """Called at the start of every /fetch so an old cancel never carries over."""
    FETCH_CANCEL.clear()

def fetch_cancel_requested():
    return FETCH_CANCEL.is_set()
from concurrent.futures import TimeoutError as FuturesTimeoutError
from itertools import product

# Dataset A — write-only evaluation-history capture. Never reads pipeline
# state, never raises, never blocks a pick from posting. See
# dataset_a_capture.py for the full contract.
import dataset_a_capture as d1
import elo_ops  # 2026-08-07: real Dataset B ELO ratings, for signal_s6_elo()
import confidence_engine  # 2026-08-12: real blended moneyline engine, replaces Phase1/Phase2 gate+adjustment
from build_signal_calibration import CalibrationBucket  # for real calibration table loading

# Read-only companion to Dataset A (see its own module docstring for why
# it's deliberately separate from dataset_a_capture.py's write-only
# contract). Same optional-import, fail-open pattern as ATE below.
_DAQ_AVAILABLE = False
try:
    import dataset_a_query as d1q
    _DAQ_AVAILABLE = True
except ImportError as _daq_err:
    print(f"[DatasetA] Query module not found — moneyline settlement/reuse features unavailable ({_daq_err})")

# ATE (Apex Trace Engine) — write-only reasoning-trail capture, same
# contract as Dataset A above: never reads pipeline state, never raises,
# never blocks a pick. Approved V1 scope: ai_traces, rule_activations,
# checkpoint_stamps (see ATE_V1_SCOPE_FINALIZED.md). Optional — the
# pipeline must run identically with or without this module present.
_ATE_AVAILABLE = False
try:
    import ate_writer as ate
    _ATE_AVAILABLE = True
    print("[ATE] Apex Trace Engine capture layer loaded successfully")
except ImportError as _ate_err:
    print(f"[ATE] capture layer not found — running without ATE tracing ({_ate_err})")

# CORE_VERSION (approved, this session): the prerequisite pipeline_version
# capture needed -- nothing existed to write into that Dataset A column
# before this. Bump manually on future sessions; not auto-derived from
# git or file hash, since this file isn't in a git repo on the Mini and
# a SHA changes on every edit including ones that don't affect behavior.
CORE_VERSION = "v7.9.8-dataset_a"

# ================================================================
# V8 DATA FOUNDATION — TENNIS STATS API (ms-profile)
# ================================================================
# Live API-backed historical stats via Tennis Stats API on RapidAPI.
# Confirmed working endpoints:
#   - profile/search/{name}/{tour}     → name resolution
#   - profile/{name}/surface-summary   → surface records
#   - profile/{name}/match-stat/{year} → hold%, break%, aces
# Falls back silently if openclaw_v8_api.py is not present.
# ================================================================

_V8_AVAILABLE = False
try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.resolve()))
    from openclaw_v8_api import (
        get_tennis_packet   as build_tennis_packet,
        format_packet_for_eval,
        v8_validation       as v8_validation_agent,
        get_recent_matches  as v8_get_recent_matches,
        resolve_player_name as v8_resolve_player_name,
        get_current_season_surface_winpct as v8_current_season_winpct,
        get_home_away_split as v8_get_home_away_split,
        get_h2h              as v8_get_h2h,
        _strip_accents        as v8_strip_accents,
    )
    _V8_AVAILABLE = True
    print("[V8] Tennis Stats API module loaded successfully")
except ImportError as _v8_err:
    print(f"[V8] API module not found — running without V8 data ({_v8_err})")

# ================================================================
# CONFIGURATION
# ================================================================

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DISCORD_WEBHOOKS = {
    "picks":     os.environ.get("DISCORD_WEBHOOK_PICKS", ""),
    "alerts":    os.environ.get("DISCORD_WEBHOOK_ALERTS", ""),
    "general":   os.environ.get("DISCORD_WEBHOOK_GENERAL", ""),
    "learning":  os.environ.get("DISCORD_WEBHOOK_LEARNING", ""),
    "system":    os.environ.get("DISCORD_WEBHOOK_SYSTEM", ""),
    "moneyline": os.environ.get("DISCORD_WEBHOOK_MONEYLINE", "")
}

# APEX-ALIS-CHANNEL-2026-08-31: dedicated ALIS reporting channel. The URL
# lives in apex_alis_webhook.txt, not in source. If the file is absent or
# unreadable the entry is simply not added -- send_discord() then logs
# "#alis not configured" and returns True, so ALIS still writes its
# markdown report and never raises.
try:
    _alis_wh = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "apex_alis_webhook.txt")
    if os.path.exists(_alis_wh):
        with open(_alis_wh) as _f:
            _u = _f.read().strip()
        if _u.startswith("https://"):
            DISCORD_WEBHOOKS["alis"] = _u
except Exception:
    pass

# Load from config.json if available
CONFIG_FILE = Path("/path/to/home/openclaw/config.json")
if CONFIG_FILE.exists():
    with open(CONFIG_FILE) as f:
        _cfg = json.load(f)
        ANTHROPIC_API_KEY = _cfg.get("anthropic", {}).get("api_key", ANTHROPIC_API_KEY)
        for ch, url in _cfg.get("discord", {}).items():
            DISCORD_WEBHOOKS[ch] = url

PRIZEPICKS_API    = "https://api.prizepicks.com/projections"
# APEX-PARTNERAPI-2026-10-01 (API-First Fetch): api.prizepicks.com above is
# DataDome-walled -- every logged attempt from this machine has returned
# 403 (confirmed across multiple sessions/dates, never once succeeded).
# partner-api.prizepicks.com serves the identical /projections JSON:API
# shape (same league_id/per_page/single_stat params, same data+included
# structure) WITHOUT that wall -- live-tested repeatedly during the
# 2026-10-01 Scraper Architecture Audit and Fetch Performance Overhaul
# audit (HTTP 200 in 0.38-0.56s, full board, all three tiers). It does
# have its own light Cloudflare bot-management layer (a rapid 3rd request
# in testing returned 429), so this is called once per fetch, not polled.
PARTNER_API       = "https://partner-api.prizepicks.com/projections"
PLAYWRIGHT_PORT   = 9222   # Chrome remote debugging port -- retired by
                           # APEX-FETCHPIPE-2026-09-30 (kept only so old
                           # references/comments elsewhere still resolve).
CHROME_PRIZEPICKS_PROFILE = str(Path.home() / ".chrome-prizepicks")  # APEX-FETCHPIPE-2026-09-30

DATA_DIR    = Path("./data");    DATA_DIR.mkdir(exist_ok=True)

# v7.9.6: reorganized into labeled subfolders (Finder-searchable by
# typing a word or date) instead of one flat data/ directory. Run
# migrate_data_layout.py once before this version runs for the first
# time — it safely moves existing files into these locations.
ARCHIVE_DIR = DATA_DIR / "picks_archive"; ARCHIVE_DIR.mkdir(exist_ok=True)

# v7.9.7 (moneyline learning system): moneyline evaluations previously
# had no persistent record of the numeric signal breakdown that drove
# a pick — only the transient in-memory dict during the single
# request. Without that, no later classification could distinguish
# "the data was wrong" from "the reasoning misweighted good data" from
# genuine variance. Archived here at evaluation time, same pattern as
# props' picks_archive.
ML_ARCHIVE_DIR = DATA_DIR / "ml_picks_archive"; ML_ARCHIVE_DIR.mkdir(exist_ok=True)

# APEX-FETCHPIPE-2026-09-30: every failed /fetch used to overwrite the
# same /tmp/prizepicks_debug.png -- by the time anyone looked, it was
# whatever the LAST failure looked like, not THIS one (confirmed live:
# the 2026-09-30 5:29pm failure investigation found a screenshot from
# 2026-09-28 9:52pm, two days stale). Each failure now gets its own
# timestamped screenshot + HTML snapshot here, never overwritten.
FETCH_DEBUG_DIR = DATA_DIR / "fetch_debug"; FETCH_DEBUG_DIR.mkdir(exist_ok=True)

WEIGHTS_FILE    = DATA_DIR / "weights" / "weights.json"
PROPOSALS_FILE  = DATA_DIR / "proposals" / "pending_proposals.json"
APPROVAL_LOG_FILE = DATA_DIR / "proposals" / "approval_history.json"
SLIP_LOG_FILE   = DATA_DIR / "slips" / "slip_log.json"
PENDING_FILE    = DATA_DIR / "learning" / "pending_results.json"
MANUAL_REVIEW_FILE = DATA_DIR / "learning" / "manual_review.json"
QUARANTINE_FILE = DATA_DIR / "learning" / "quarantine.json"
SIGNAL_HISTORY_FILE = DATA_DIR / "learning" / "signal_history.json"

# v7.9.7 (moneyline learning system): separate history file from props'
# signal_history.json — moneyline signals (ml_serve_weight etc.) are a
# different set of weights with different, lower-frequency volume, and
# mixing the two pools would make the sample-size math meaningless for
# both.
ML_SIGNAL_HISTORY_FILE = DATA_DIR / "learning" / "ml_signal_history.json"

# v7.9.6: persistent player profile store — one JSON file per player
# per surface, keyed by player name + surface. Research agent reads
# here first before hitting the V8 API. Written back after every
# successful fresh fetch. TTL controlled by player_profile_ttl_hours
# in weights.json (default 24). Covers both props and moneyline.
PROFILE_DIR = DATA_DIR / "players"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

for _f in (WEIGHTS_FILE, PROPOSALS_FILE, APPROVAL_LOG_FILE, SLIP_LOG_FILE,
           PENDING_FILE, QUARANTINE_FILE, SIGNAL_HISTORY_FILE, ML_SIGNAL_HISTORY_FILE):
    _f.parent.mkdir(parents=True, exist_ok=True)

# v7.9.7 (items 8 & 9): raised from 5 to 20. Operator reviewed real
# volume — roughly 10-15 matches evaluated per day, 6 highest-confidence
# GREEN picks selected per slip — and confirmed GREEN samples accumulate
# steadily across prop types at that volume, not just for high-frequency
# ones. 20 gives meaningfully more protection against a short correlated
# run (a hot streak, a soft slate) being mistaken for a real signal,
# without starving the learning loop the way a threshold with no
# realistic path to being reached would. Applies uniformly to all prop
# types for now — revisit per-type thresholds (e.g. DF/Aces separately)
# once real accumulation-rate data exists at this new threshold.
#
# This threshold is evaluated CUMULATIVELY across the rolling
# last-N-picks window (SIGNAL_LOOKBACK_LAST_N below), not within a
# single night. A single night's picks rarely produce 10 occurrences
# of the same signal — pooling the most recent graded results makes
# this realistically achievable while staying fast-moving evidence.
MIN_PROPOSAL_SAMPLE = 10
# Lookback change (this session, operator-approved): the rolling
# 14-DAY window is replaced with a rolling last-10-PICKS-per-signal
# window, for BOTH props and moneyline. Time windows made the sample
# size depend on slate volume -- a quiet week starved the loop while a
# Grand Slam week flooded it with one tournament's conditions. A fixed
# pick-count window means every proposal is always judged on the same
# amount of evidence: the 10 most recent graded results for that
# signal, however many days they span. MIN_PROPOSAL_SAMPLE drops
# 20 -> 10 to match (a threshold above the window size would be
# unreachable by construction). SIGNAL_LOOKBACK_DAYS retired.
SIGNAL_LOOKBACK_LAST_N = 10

# v7.9.7 (moneyline learning system): moneyline gets its own, lower
# threshold rather than sharing props' 20. Moneyline candidates are
# inherently lower-volume than props (roughly one legitimate
# winner-pick per match vs. multiple prop lines per match), and the
# pool feeding proposals is further narrowed by the attribution
# classifier below (only losses classified as a genuine weighting
# problem count — not data failures, not variance). Stacking the
# props-level 20 on top of an already smaller, already-filtered pool
# risked the same "threshold with no realistic path to being reached"
# problem already rejected for props. 10 keeps real statistical
# protection while staying reachable given moneyline's lower volume.
MIN_ML_PROPOSAL_SAMPLE = 10

# ══════════════════════════════════════════════════════════════
# CALIBRATION REQUIREMENT — provisional rule registry (this session)
# ══════════════════════════════════════════════════════════════
# Real gap this closes: _build_weight_proposals above already forces
# real evidence (MIN_PROPOSAL_SAMPLE) before an EXISTING weight gets
# tuned. But a brand-new RULE (RBMD, LPAB, a new confidence cap, a new
# band cutoff) has never gone through anything like that — it ships
# as soon as a session writes it, marked "PROVISIONAL" in a comment,
# and that comment is the entire tracking mechanism. Nothing queries
# it, nothing counts how many real picks it's fired on, nothing tells
# the operator "this one now has enough settled results to actually
# review." A text comment that nothing reads is not calibration
# discipline, just a note to self.
#
# This registry is that missing tracking layer — NOT a replacement
# for the weight-proposal system above, which stays exactly as-is.
# A rule entered here just becomes queryable: how many times has it
# fired, how many of those are now settled (WIN/LOSS), and has anyone
# looked. record_provisional_rule_fire() and calibration_status_report()
# below do the actual work.
#
# HONEST SCOPE NOTE: seeded here with the provisional constants this
# session could clearly identify (grep for "PROVISIONAL" across the
# file). A full audit of every unbacktested threshold in this ~9,700
# line file is a separate, larger pass — do not read this list as
# exhaustive. New rules should be added here going forward as they're
# written, same discipline as adding a weight to weights.json.
PROVISIONAL_RULES = {
    "ML_S1_Surface_Bands":        {"introduced": "2026-07-09", "min_sample": MIN_ML_PROPOSAL_SAMPLE},
    "ML_S2_Tournament_Bands":     {"introduced": "2026-07-09", "min_sample": MIN_ML_PROPOSAL_SAMPLE},
    "ML_S4_Physical_Bands":       {"introduced": "2026-07-09", "min_sample": MIN_ML_PROPOSAL_SAMPLE},
    "ML_S5_Classification_Bands": {"introduced": "2026-07-09", "min_sample": MIN_ML_PROPOSAL_SAMPLE},
    "ML_Gate14_HomeCourtLean":    {"introduced": "2026-07-09", "min_sample": 10},
    "Compliance_PerFlag_Penalty": {"introduced": "2026-07-09", "min_sample": 10},
    "RBMD_v1_2":                  {"introduced": "2026-06-13", "min_sample": 10},
    "LPAB_PSAR_v1_0":             {"introduced": "2026-06-14", "min_sample": 10},
    "Rule9_MarketExclusion":      {"introduced": "2026-07-19", "min_sample": 10},
    "Gate14_Props_Procedural":    {"introduced": "2026-07-19", "min_sample": 10},
    "CFEF_Trigger6":              {"introduced": "2026-07-19", "min_sample": 10},
    "Rule30_CFEF_Stacking":       {"introduced": "2026-07-19", "min_sample": 10},
}

CALIBRATION_LOG_FILE = DATA_DIR / "calibration" / "rule_firings.json"


def _load_calibration_log():
    if CALIBRATION_LOG_FILE.exists():
        with open(CALIBRATION_LOG_FILE) as f:
            return json.load(f)
    return []


def _save_calibration_log(entries):
    CALIBRATION_LOG_FILE.parent.mkdir(exist_ok=True)
    with open(CALIBRATION_LOG_FILE, "w") as f:
        json.dump(entries, f, indent=2, default=str)


def record_provisional_rule_fire(rule_name: str, eval_id: str, player: str = None,
                                  fired_at: str = None) -> None:
    """
    Call this at the point a provisional rule actually fires on a real
    evaluation (e.g. wherever RBMD applies its friction adjustment, or
    Gate 14 applies its home-court lean). Deliberately fail-open — a
    logging problem must never block or alter a real pick, same
    principle already used for ate.capture_rule_activations().
    Unknown rule_name is logged anyway (not silently dropped) so a
    rule fired before being registered still shows up once someone
    checks calibration_status_report() and adds it to the registry.
    """
    try:
        entries = _load_calibration_log()
        entries.append({
            "rule_name": rule_name,
            "eval_id":   eval_id,
            "player":    player,
            "fired_at":  fired_at,
            "outcome":   None,  # filled in later by mark_calibration_outcome()
        })
        _save_calibration_log(entries)
    except Exception:
        pass  # fail-open, never blocks scoring


def mark_calibration_outcome(eval_id: str, outcome: str) -> None:
    """
    Call when a slip leg tied to eval_id settles (WIN/LOSS/PUSH/VOID).
    Updates every calibration-log row for that eval_id — a single
    evaluation can have multiple provisional rules fire on it.
    """
    try:
        entries = _load_calibration_log()
        changed = False
        for e in entries:
            if e.get("eval_id") == eval_id and e.get("outcome") is None:
                e["outcome"] = outcome
                changed = True
        if changed:
            _save_calibration_log(entries)
    except Exception:
        pass


def calibration_status_report() -> dict:
    """
    The actual answer to "which of my rules are still unproven."
    For every registered provisional rule: how many times it's fired,
    how many of those are settled, and — once settled count reaches
    that rule's min_sample — a real hit rate the operator can look at
    and decide: keep, tune, or retire. Rules below their min_sample
    are explicitly marked NOT_ENOUGH_DATA rather than given a
    misleadingly precise-looking rate off 2 or 3 picks.
    """
    entries = _load_calibration_log()
    report = {}
    for rule_name, meta in PROVISIONAL_RULES.items():
        rule_entries = [e for e in entries if e.get("rule_name") == rule_name]
        settled = [e for e in rule_entries if e.get("outcome") in ("WIN", "LOSS")]
        fired = len(rule_entries)
        settled_n = len(settled)
        min_sample = meta.get("min_sample", 10)
        if settled_n >= min_sample:
            wins = sum(1 for e in settled if e["outcome"] == "WIN")
            status = "READY_FOR_REVIEW"
            hit_rate = round(wins / settled_n, 4)
        else:
            status = "NOT_ENOUGH_DATA"
            hit_rate = None
        report[rule_name] = {
            "introduced":   meta.get("introduced"),
            "fired_count":  fired,
            "settled_count": settled_n,
            "min_sample":   min_sample,
            "status":       status,
            "hit_rate":     hit_rate,
        }
    # Also surface anything that fired but was never registered —
    # exactly the case the "unknown rule_name" note above anticipates.
    unregistered = sorted({e.get("rule_name") for e in entries
                            if e.get("rule_name") not in PROVISIONAL_RULES})
    if unregistered:
        report["_unregistered_rules_seen"] = unregistered
    return report


# ══════════════════════════════════════════════════════════════
# MONEYLINE v1.4 — GRADUATED PHASE 2 SIGNAL BANDS
# ══════════════════════════════════════════════════════════════
# Locked spec (this session), per operator + reviewer agreement.
# Signal scale: None (unconfirmed) / -2,-1,0,+1,+2 (confirmed,
# graduated). None != 0 — 0 means confirmed real data showing no
# meaningful edge; None means genuinely couldn't confirm at all.
# Each signal has its OWN independent band definition below — a
# surface win% gap, a tournament round gap, and a fatigue-factor
# count are not comparable on the same numeric scale, so they are
# NOT forced to share one set of cutoffs.
#
# ⚠️ PROVISIONAL — NOT YET CALIBRATED ON HISTORICAL RESULTS.
# These are starting hypotheses, not locked/validated rules — same
# status as any other unbacktested threshold in this system. Revisit
# once real outcome data exists to check them against.

ML_S1_SURFACE_STRONG_GAP_PP   = 15   # percentage points, current-season surface win%
ML_S1_SURFACE_MODERATE_GAP_PP = 5

ML_S2_TOURNAMENT_STRONG_ROUND_GAP   = 2   # rounds won in this event
ML_S2_TOURNAMENT_MODERATE_ROUND_GAP = 1

ML_S4_PHYSICAL_STRONG_FACTORS   = 2   # both went-the-distance AND short turnaround
ML_S4_PHYSICAL_MODERATE_FACTORS = 1   # only one of the two factors present

ML_S5_CLASSIFICATION_STRONG_MAGNITUDE   = 12  # dominance score magnitude
ML_S5_CLASSIFICATION_MODERATE_MAGNITUDE = 8   # existing LOPSIDED-confirmation floor

# S6 (ELO) — added 2026-08-07 after a real walk-forward backtest
# (elo_backtest.py, n=500,072 real matches post player_a/b labeling
# fix) confirmed overall ELO gap at AUC 0.7223, Apex's strongest
# tested signal, beating H2H's 0.584. Uses OVERALL elo_gap, not
# surface-specific — surface tested weaker at real scale (0.7093
# vs 0.7223), the opposite of what a small early sample suggested.
# Thresholds are PROVISIONAL but not arbitrary: based on the standard,
# widely-published ELO win-probability relationship (a 100-point gap
# ≈ 64% expected win probability, 200-point gap ≈ 76%) rather than a
# guess — real empirical tuning against Apex's own outcomes should
# still happen once enough live picks accumulate, same as every other
# signal here.
ML_S6_ELO_STRONG_GAP   = 200   # ELO rating points, overall gap
ML_S6_ELO_MODERATE_GAP = 100
ML_S6_ELO_MIN_MATCHES  = 5     # real matches required per player before trusting the rating at all

# S3 (H2H) — PROVISIONAL, same status as the other four signals' band
# cutoffs above. Derived from 26 real matches with a head-to-head
# sample of 10+ meetings: median gap 38.5pp, 75th pct 60pp, 90th pct
# 68.4pp. ML_S3_H2H_MIN_SAMPLE=10 matches that same real dataset —
# below that, one match can swing the whole percentage, not a stable
# signal (same "unconfirmed-is-NO" discipline used everywhere else).
ML_S3_H2H_STRONG_GAP_PP   = 62   # percentage points, head-to-head win-rate gap
ML_S3_H2H_MODERATE_GAP_PP = 38
ML_S3_H2H_MIN_SAMPLE      = 10   # real H2H meetings required to trust this signal at all

# Phase 2 aggregation bands (sum of all confirmed signal values).
# FIXED 2026-08-07 (real fix, not a guess): the real signal count is
# back to 4 (S1/S3/S5/S6, after S2/S4 removal + S6 addition earlier
# today), so the real max |phase2_total| is back to ±8 -- the exact
# same count/range this codebase already used ONCE before, prior to
# S3 ever being added. The comments below (now removed) literally
# documented that earlier state: "rescaled from 2 (max 8, 4 signals)
# -> 3 (max 10, 5 signals)" for LEAN, "4 -> 5" for YELLOW, "6 -> 8,
# rounded UP" for GREEN_CAND. Reverting to those real, already-
# established pre-S3 values rather than inventing new ones -- this is
# a faithful count-based rescale (same arithmetic basis the ORIGINAL
# rescale used), not a fresh guess. Real caveat: the specific 4
# signals are DIFFERENT this time (S1/S3/S5/S6, not S1/S2/S4/S5) --
# the count/max arithmetic transfers correctly regardless of which
# four signals they are, but these values are still PROVISIONAL and
# worth real backtesting once enough live S6-era picks accumulate.
ML_PHASE2_LEAN_MIN        = 2   # reverted to the real pre-S3 4-signal/±8-max value
ML_PHASE2_YELLOW_MIN      = 4   # reverted to the real pre-S3 4-signal/±8-max value
ML_PHASE2_GREEN_CAND_MIN  = 6   # reverted to the real pre-S3 4-signal/±8-max value

CT = pytz.timezone("America/Chicago")

# ================================================================
# MODELS
# ================================================================

MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"

# ================================================================
# VERIFIED RESEARCH SOURCES
# ================================================================

VERIFIED_SOURCES = [
    "atptour.com",
    "wtatennis.com",
    "tennisabstract.com",
    "flashscore.com",
    "ultimatetennisstatistics.com",
    "espn.com",
    "rolandgarros.com",
    "wimbledon.com",
    "usopen.org",
    "ausopen.com",
]

# ================================================================
# DATE SYSTEM — ONE SOURCE OF TRUTH
# ================================================================

def get_system_dates():
    now      = datetime.now(CT)
    today    = now.date()
    tomorrow = today + timedelta(days=1)
    return {
        "pipeline_run_date": str(today),
        "match_date":        str(tomorrow),
        "learn_on_date":     str(today),
        "archive_date":      str(today),
        "display_day":       tomorrow.strftime("%A, %B %d %Y"),
        "ct_time":           now.strftime("%I:%M %p CT").lstrip("0"),
    }

# ================================================================
# LOGGING
# ================================================================

def log(agent, message):
    # Bug fix (this session, confirmed live): stdout redirected to a file
    # via launchctl's StandardOutPath uses block buffering by default, not
    # line buffering. On a long-running process, log lines can sit
    # unwritten in memory for a long time. Confirmed live: a real
    # compliance parse failure's diagnostic output (the raw malformed JSON
    # the fix earlier this session specifically added to help diagnose
    # this exact failure mode) was lost entirely because the process was
    # killed on a routine restart before that block ever flushed to disk
    # -- the log line existed in memory, was never written, and is now
    # unrecoverable. flush=True forces every log line to disk immediately,
    # at a small, worthwhile I/O cost, so a restart (or crash) can no
    # longer silently erase diagnostics that were supposedly captured.
    #
    # APEX-FETCHRELIABILITY-2026-10-01: root-cause fix. log() is called
    # from EVERY stage of EVERY pipeline (fetch, Playwright, Discord
    # sends, scoring, evaluation) -- hundreds of call sites -- and until
    # now this print() was the one unguarded I/O call in the whole
    # codebase. If this process's stdout fd ever becomes a broken pipe
    # (the terminal/pty it was started from closes, an SSH session drops,
    # a piped viewer like `tail`/`less` exits while attached) the VERY
    # NEXT log() call anywhere -- not just in Discord/network code, which
    # was already fully try/excepted -- raises BrokenPipeError straight
    # out of whatever function happened to be running. That's confirmed
    # to be the actual cause of the raw "BrokenPipeError [Errno 32]" a
    # user-triggered /fetch surfaced: Partner API and Playwright already
    # catch every failure internally and never raise, so the only
    # remaining unguarded write in that whole call path was this one.
    # Fail-open here: a lost log line is cheap, a crashed /fetch is not.
    now = datetime.now(CT).strftime("%I:%M %p CT").lstrip("0")
    try:
        print(f"[{now}] [{agent}] {message}", flush=True)
    except (BrokenPipeError, OSError, ValueError):
        # stdout is gone (broken pipe / closed fd) -- best-effort fallback
        # to a file so the line isn't silently lost, but never let a
        # logging failure take down the caller.
        try:
            _fallback_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs")
            with open(os.path.join(_fallback_dir, "stdout_fallback.log"),
                      "a", encoding="utf-8") as _f:
                _f.write(f"[{now}] [{agent}] {message}\n")
        except Exception:
            pass

# ================================================================
# DISCORD DELIVERY
# ================================================================

# v7.9.7 fix (item 15): parallel prop evaluation (ThreadPoolExecutor)
# means multiple threads can now call send_discord() at the same time.
# The old per-call time.sleep(0.5) only paced a single thread's own
# sequential chunks — it did nothing to stop 4 threads all posting to
# the same webhook simultaneously, which is exactly what risks tripping
# Discord's rate limit rather than preventing it. A single shared lock
# plus a last-sent timestamp PER WEBHOOK means every thread funnels
# through one real throttle instead of four independent, ineffective ones.
import threading as _threading

# ── API USAGE + RETRY TRACKING (this session, approved) ─────────
# Every API call flows through call_model, which already receives the
# real usage object (input/cached/output tokens) and previously
# discarded it after one log line. Accumulate it so cost stops being
# a ceiling-multiplication estimate and becomes a measured number
# from this system's own calls. Retry stats feed the Sonnet-retry
# keep-or-delete decision with real clear-rate evidence instead of
# guesswork (operator decision: measure first, delete only if the
# data says retries mostly rediscover the same problem).
_usage_lock = _threading.Lock()
_usage_totals = {"input_tokens": 0, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 0, "output_tokens": 0, "calls": 0}
_retry_stats = {"retries_attempted": 0, "retries_cleared": 0,
                "retries_still_flagged": 0, "b_only_no_retry": 0}

# Per-MTok pricing used ONLY for the estimated-$ line in the usage
# report. OPERATOR-EDITABLE — verify against the current published
# Anthropic pricing page before trusting the dollar estimate; the
# token counts themselves are exact regardless of these numbers.
USAGE_PRICES_PER_MTOK = {
    "input":       3.00,   # fresh input
    "cache_write": 3.75,   # cache_creation_input_tokens
    "cache_read":  0.30,   # cache_read_input_tokens
    "output":     15.00,
}

def post_usage_report(dates, props_posted):
    """Posts cumulative-since-bot-start token usage, estimated spend,
    and retry clear-rate to #systems-channel after each batch."""
    with _usage_lock:
        u = dict(_usage_totals)
        r = dict(_retry_stats)
    if not u["calls"]:
        return
    est = (u["input_tokens"]                * USAGE_PRICES_PER_MTOK["input"] +
           u["cache_creation_input_tokens"] * USAGE_PRICES_PER_MTOK["cache_write"] +
           u["cache_read_input_tokens"]     * USAGE_PRICES_PER_MTOK["cache_read"] +
           u["output_tokens"]               * USAGE_PRICES_PER_MTOK["output"]) / 1_000_000
    attempted = r["retries_attempted"]
    clear_rate = (f"{r['retries_cleared']}/{attempted} cleared "
                  f"({r['retries_cleared']/attempted:.0%})" if attempted else "none yet")
    # Bug fix (this session): this was "systems" (plural). The real
    # DISCORD_WEBHOOKS dict only has "system" (singular) -- meaning this
    # usage report has never actually posted to Discord, only logged
    # locally and silently discarded, since before tonight.
    send_discord("system",
        f"📊 **API USAGE — since bot start** (batch just posted: {props_posted} pick(s))\n"
        f"Calls: {u['calls']} | Fresh in: {u['input_tokens']:,} tok | "
        f"Cache write: {u['cache_creation_input_tokens']:,} | "
        f"Cache read: {u['cache_read_input_tokens']:,} | "
        f"Out: {u['output_tokens']:,}\n"
        f"Est. spend: ${est:.2f} (verify unit prices in USAGE_PRICES_PER_MTOK)\n"
        f"Sonnet retries: {clear_rate} | still-flagged after retry: "
        f"{r['retries_still_flagged']} | B-only accepted w/o retry (cost saved): "
        f"{r['b_only_no_retry']}"
    )
_discord_lock = _threading.Lock()
_discord_last_sent = {}  # webhook url -> last-sent monotonic timestamp
_DISCORD_MIN_INTERVAL = 0.5  # seconds between sends to the SAME webhook
_DISCORD_MAX_RETRIES = 4     # on 429: up to 4 retries (5 attempts total) per chunk
_DISCORD_MAX_BACKOFF = 10.0  # seconds -- cap so one stuck chunk can't block a fetch for long

# APEX-DISCORDREPAIR-2026-09-30: fetch-scoped suppression for the #alerts
# per-prop spam (confirmed real on 2026-09-30 -- ~240 individual #alerts
# sends across one fetch's evaluation loop, all hit 429, which stalled the
# run past Discord's 15-minute slash-interaction window and killed the
# completion message). This does NOT touch any of the 30+ call sites that
# decide WHEN to alert (unchanged, still evaluate/decide exactly as
# before) -- it only changes what happens at the delivery sink, and only
# for #alerts, and only while a /fetch run has turned it on. Outside an
# active /fetch (scheduled jobs, manual commands, other channels), alerts
# behave exactly as before.
_DISCORD_FETCH_SUPPRESS = {"active": False, "suppressed_alerts": 0}

LAST_DISCORD_DELIVERY_STATS = {
    "attempted": 0, "sent": 0, "rate_limited_429": 0, "retries": 0, "final_failures": 0,
}

def begin_fetch_discord_suppression():
    """Call at the start of a /fetch run: silences (but still counts)
    individual #alerts sends for the duration, and resets the delivery
    counters used by the fetch summary. Does not affect #picks, #system,
    or any other channel."""
    global LAST_DISCORD_DELIVERY_STATS
    _DISCORD_FETCH_SUPPRESS["active"] = True
    _DISCORD_FETCH_SUPPRESS["suppressed_alerts"] = 0
    LAST_DISCORD_DELIVERY_STATS = {
        "attempted": 0, "sent": 0, "rate_limited_429": 0, "retries": 0, "final_failures": 0,
    }

def end_fetch_discord_suppression():
    """Turns #alerts delivery back on and returns how many were
    suppressed during the run, for the fetch summary's warnings line."""
    _DISCORD_FETCH_SUPPRESS["active"] = False
    return _DISCORD_FETCH_SUPPRESS["suppressed_alerts"]

def send_discord(channel, message):
    """
    Routes to correct Discord channel by name.
    CT 12-hour time only. Never Telegram. Never military time.
    Channels: picks | alerts | general | learning | system

    Thread-safe: a shared lock serializes sends to the same webhook
    across all threads, so parallel prop evaluation can't fire
    simultaneous requests at one channel.

    Returns True/False (this session -- closes a real gap found during
    a full-system audit: every caller ignored this function's outcome
    entirely, because there wasn't one to check -- it always returned
    None. Three call sites were marking a pick POSTED=1 in Dataset A
    with NO verification the Discord send actually succeeded; one of
    them did it BEFORE even calling send_discord. A webhook failure
    (rate limit, revoked token, network blip) meant a real GREEN pick
    could silently vanish -- never reaching Discord, while Dataset A
    confidently recorded it as posted. "Not configured" (dev/test mode,
    already an intentional, logged no-op) returns True -- that's not a
    failure, it's the function working as designed. A genuine send
    failure -- bad status code, or an exception -- returns False.

    APEX-DISCORDREPAIR-2026-09-30: two additions, both delivery-layer only
    -- no caller's decision logic changes.
    1. #alerts sends are silently (but countably) dropped while a /fetch
       run has suppression on (see begin_/end_fetch_discord_suppression).
    2. A 429 now backs off and retries (bounded) instead of being logged
       once and moved past -- respects Discord's own retry_after when it
       gives one.
    """
    if channel == "alerts" and _DISCORD_FETCH_SUPPRESS["active"]:
        _DISCORD_FETCH_SUPPRESS["suppressed_alerts"] += 1
        return True

    webhook = DISCORD_WEBHOOKS.get(channel)
    if not webhook or "YOUR_TOKEN" in webhook:
        log("Discord", f"#{channel} not configured — logged only: {message[:80]}")
        return True

    if channel == "alerts":   # APEX-ALERTCARDS-2026-10-25: every #alerts message is a card
        try:
            import apex_alert_cards as _C
            if _C.ALERT_CARDS_ON:
                return bool(_C.post_text(message))
        except Exception as _e:
            log("Discord", f"#alerts card failed ({type(_e).__name__}), sending text")

    chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
    all_ok = True
    for chunk in chunks:
        with _discord_lock:
            last = _discord_last_sent.get(webhook, 0)
            wait = _DISCORD_MIN_INTERVAL - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            LAST_DISCORD_DELIVERY_STATS["attempted"] += 1
            _attempt = 0
            while True:
                _attempt += 1
                try:
                    resp = requests.post(webhook, json={"content": chunk}, timeout=10)
                except Exception as e:
                    log("Discord", f"Error #{channel}: {e}")
                    all_ok = False
                    LAST_DISCORD_DELIVERY_STATS["final_failures"] += 1
                    break
                if resp.status_code in (200, 204):
                    LAST_DISCORD_DELIVERY_STATS["sent"] += 1
                    break
                if resp.status_code == 429 and _attempt <= _DISCORD_MAX_RETRIES:
                    LAST_DISCORD_DELIVERY_STATS["rate_limited_429"] += 1
                    LAST_DISCORD_DELIVERY_STATS["retries"] += 1
                    _retry_after = None
                    try:
                        _retry_after = float(resp.json().get("retry_after"))
                    except Exception:
                        try:
                            _retry_after = float(resp.headers.get("Retry-After"))
                        except Exception:
                            _retry_after = None
                    _backoff = _retry_after if _retry_after is not None else (2 ** (_attempt - 1)) * 0.5
                    _backoff = min(max(_backoff, 0.5), _DISCORD_MAX_BACKOFF)
                    log("Discord", f"#{channel} 429 — retry {_attempt}/{_DISCORD_MAX_RETRIES} after {_backoff:.1f}s")
                    time.sleep(_backoff)
                    continue
                log("Discord", f"Send failed #{channel}: {resp.status_code}")
                all_ok = False
                LAST_DISCORD_DELIVERY_STATS["final_failures"] += 1
                break
            _discord_last_sent[webhook] = time.monotonic()
    return all_ok

def send_discord_tracked(channel, message):
    """Like send_discord(), but for a NEW message where the caller needs
    the message_id back (so it can be edited later via
    edit_discord_message()) -- used for the persistent /fetch progress
    message, which must survive and be updatable for the whole run,
    independent of Discord's 15-minute slash-interaction window. Posts
    only the first chunk if the message is multi-chunk (progress/
    completion text is kept short by design; this is not used for the
    full picks dump). Returns (ok, message_id or None)."""
    webhook = DISCORD_WEBHOOKS.get(channel)
    if not webhook or "YOUR_TOKEN" in webhook:
        log("Discord", f"#{channel} not configured — logged only: {message[:80]}")
        return True, None
    if channel == "alerts":   # APEX-ALERTCARDS-2026-10-25: a card (fetch slow / stalled)
        try:
            import apex_alert_cards as _C
            if _C.ALERT_CARDS_ON:
                return _C.post_text(message, wait=True)
        except Exception as _e:
            log("Discord", f"#alerts card failed ({type(_e).__name__}), sending text")
    try:
        resp = requests.post(webhook + "?wait=true", json={"content": message[:1900]}, timeout=10)
        if resp.status_code in (200, 201):
            return True, resp.json().get("id")
        log("Discord", f"send_discord_tracked failed #{channel}: {resp.status_code}")
        return False, None
    except Exception as e:
        log("Discord", f"send_discord_tracked error #{channel}: {e}")
        return False, None

def edit_discord_message(channel, message_id, new_text):
    """Edits a message previously created by send_discord_tracked(). Uses
    the channel's regular (long-lived) incoming webhook -- never a
    slash-command interaction token, so this keeps working no matter how
    long the fetch has been running. Bounded retry on 429, same as
    send_discord(). Returns True/False."""
    webhook = DISCORD_WEBHOOKS.get(channel)
    if not webhook or "YOUR_TOKEN" in webhook or not message_id:
        return False
    url = f"{webhook}/messages/{message_id}"
    _attempt = 0
    while True:
        _attempt += 1
        try:
            resp = requests.patch(url, json={"content": new_text[:1900]}, timeout=10)
        except Exception as e:
            log("Discord", f"edit_discord_message error: {e}")
            return False
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 429 and _attempt <= _DISCORD_MAX_RETRIES:
            try:
                _backoff = float(resp.json().get("retry_after"))
            except Exception:
                _backoff = (2 ** (_attempt - 1)) * 0.5
            time.sleep(min(max(_backoff, 0.5), _DISCORD_MAX_BACKOFF))
            continue
        log("Discord", f"edit_discord_message failed: {resp.status_code}")
        return False

# ================================================================
# ANTHROPIC API
# ================================================================

# APEX-SETKEY-ANTHROPIC-2026-10-27 (owner: "we need the api key, I'll send an updated one
# tomorrow"): the Anthropic key can now be replaced from Discord (/setkey -> Anthropic). It is
# saved in anthropic_key.txt (mode 600) and read here per call, so no restart is needed. The
# file wins; otherwise config.json / the environment as before. Never logged or echoed.
ANTHROPIC_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anthropic_key.txt")
_ANTHROPIC_KEY_CACHE = {"mtime": None, "key": ""}


def anthropic_key():
    try:
        mt = os.path.getmtime(ANTHROPIC_KEY_FILE)
        if _ANTHROPIC_KEY_CACHE["mtime"] != mt:
            with open(ANTHROPIC_KEY_FILE) as fh:
                _ANTHROPIC_KEY_CACHE.update(mtime=mt, key=fh.read().strip())
        if _ANTHROPIC_KEY_CACHE["key"]:
            return _ANTHROPIC_KEY_CACHE["key"]
    except Exception:
        pass
    return ANTHROPIC_API_KEY


def call_model(model, system_prompt, user_content,
               temperature=0.1, max_tokens=2000, enable_cache=False,
               return_stop_reason=False, eval_id=None, step_type=None,
               trace_id_out=None, error_out=None):
    """
    enable_cache=True marks the system_prompt as cacheable (Anthropic
    prompt caching). Use this for calls that resend the same large,
    mostly-static system prompt on every call — e.g. the main evaluation
    call, which resends the ~29k-token master prompt/decision tree/
    session-start-check bundle on every single prop, unchanged, at full
    price every time (confirmed 0% cache read ratio for Sonnet in the
    Console's Caching tab vs 84% for Haiku, which already benefits from
    caching elsewhere). A cache hit costs ~10% of normal input price for
    the cached portion — no change to model behavior, output, or rules,
    only to how the repeated system prompt is billed.

    eval_id / step_type (ATE, optional): when step_type is provided,
    this call is traced to ate.db after it completes (success or
    failure) — see ate_writer.write_trace(). Callers that don't pass
    step_type are never traced; this keeps utility/parsing call sites
    (name resolution, classification, health checks) untouched and
    untraced by construction, not by omission. Tracing is fail-open —
    an ATE write failure can never affect this function's return value.

    trace_id_out (ATE, optional): a list passed by the caller to
    receive the new ai_traces.trace_id on success (trace_id_out.append(...)),
    so rule_activations/checkpoint_stamps captured afterward by the
    caller can be linked back to the exact trace row that produced
    them. Deliberately NOT a return-value change — call_model() has
    two established return shapes across its 13 call sites (bare
    value, or a 3-tuple with return_stop_reason=True) and adding a
    4th element to either would touch every existing caller's
    unpacking. An output-parameter list has zero effect on any call
    site that doesn't pass one. Left empty (or unset) on any failure
    path or when ATE is unavailable — callers must treat a missing
    trace_id_out entry as "no linkage available," not an error.

    error_out (2026-08-04, same non-breaking output-parameter pattern
    as trace_id_out): a list passed by the caller to receive the REAL
    failure reason on any error path (error_out.append(...)) — the
    actual HTTP status + API response body, or the actual exception
    text. Real gap this closes: on failure this function already logs
    the real reason and posts it to #system, but every caller only
    ever saw a bare None back — meaning a caller like
    prepare_batch_prop() had no way to raise anything more specific
    than a hardcoded "could not read the screenshot" no matter what
    actually happened (a genuine credit/billing error, a malformed
    base64 payload, a timeout, anything). That generic message was
    all Mission Control's queue ever showed too, since it can only
    display what the raised error actually says. error_out lets a
    caller surface the SAME real detail Discord already gets, in the
    UI too, instead of the real reason existing only in the Discord
    log. Left untouched (still whatever the caller initialized it to)
    on success.
    """
    _key = anthropic_key()   # APEX-SETKEY-ANTHROPIC-2026-10-27: file first, then config/env
    if not _key:
        log("API", "No API key configured")
        return (None, None, {}) if return_stop_reason else None
    headers = {
        "x-api-key": _key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if enable_cache:
        # Required for the 1-hour cache TTL below -- without this beta
        # header, ttl:"1h" is silently ignored and every call falls back
        # to Anthropic's default 5-minute ephemeral cache, which most
        # real-world evaluation spacing (more than a few minutes apart)
        # would miss entirely. (2026-07-26: this was the actual gap --
        # cache_control existed already, just without this header/ttl,
        # so it was rarely if ever actually hitting cache.)
        headers["anthropic-beta"] = "extended-cache-ttl-2025-04-11"

    if enable_cache:
        # System prompt as a content-block list with a cache breakpoint
        # on the (large, static) prompt text. Anthropic caches everything
        # up to and including the marked block for reuse on later calls.
        system_payload = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ]
    else:
        system_payload = system_prompt

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system_payload,
        "messages": [{"role": "user", "content": user_content}],
    }
    try:
        _ate_start = time.monotonic()
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=payload, timeout=240
        )
        _ate_latency_ms = int((time.monotonic() - _ate_start) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            usage = data.get("usage", {})
            if usage:
                with _usage_lock:
                    _usage_totals["calls"] += 1
                    for _k in ("input_tokens", "cache_creation_input_tokens",
                               "cache_read_input_tokens", "output_tokens"):
                        _usage_totals[_k] += usage.get(_k, 0) or 0
            if enable_cache and usage:
                log("API", f"Cache — created: {usage.get('cache_creation_input_tokens', 0)} | "
                    f"read: {usage.get('cache_read_input_tokens', 0)} | "
                    f"fresh input: {usage.get('input_tokens', 0)}")
            # Bug fix (this session): stop_reason was fetched by the API
            # but discarded here -- meaning there was no way to tell
            # whether an incomplete/truncated response (like the
            # Kostyuk attempt-1 evaluation that "cut off at Checkpoint
            # 4" per two separate compliance flags) hit max_tokens or
            # stopped for some other reason. Now logs loudly whenever
            # length was the actual cause, so truncation is diagnosable
            # from the log directly instead of only showing up
            # indirectly as a downstream compliance flag and a
            # confidence penalty with no visible cause. return_stop_
            # reason=True (opt-in, evaluation_agent only) lets the
            # caller act on it directly instead of just reading a log.
            stop_reason = data.get("stop_reason")
            if stop_reason == "max_tokens":
                log("API", f"⚠️ TRUNCATED — response hit max_tokens={max_tokens} "
                    f"for model {model} (output_tokens used: {usage.get('output_tokens', '?')}). "
                    f"Output was cut off mid-generation, not a deliberate stop. If this "
                    f"is evaluation_agent, an 'incomplete evaluation' compliance flag "
                    f"on this pick is a truncation artifact, not a reasoning failure.")
            text = data["content"][0]["text"]
            # ATE capture (fail-open, only when a caller opted in via
            # step_type — see docstring). Reuses the same per-MTok rates
            # already defined in USAGE_PRICES_PER_MTOK above for the
            # cumulative usage report, so there is one pricing source of
            # truth, not two.
            if _ATE_AVAILABLE and step_type is not None:
                try:
                    _ate_cost = (
                        (usage.get("input_tokens", 0) or 0) * USAGE_PRICES_PER_MTOK["input"] +
                        (usage.get("cache_creation_input_tokens", 0) or 0) * USAGE_PRICES_PER_MTOK["cache_write"] +
                        (usage.get("cache_read_input_tokens", 0) or 0) * USAGE_PRICES_PER_MTOK["cache_read"] +
                        (usage.get("output_tokens", 0) or 0) * USAGE_PRICES_PER_MTOK["output"]
                    ) / 1_000_000
                    _new_trace_id = ate.write_trace(
                        eval_id=eval_id, step_type=step_type, model=model,
                        prompt=str(user_content), response=text, success=True,
                        stop_reason=stop_reason, latency_ms=_ate_latency_ms,
                        tokens_in=usage.get("input_tokens"),
                        tokens_out=usage.get("output_tokens"),
                        cache_creation_tokens=usage.get("cache_creation_input_tokens"),
                        cache_read_tokens=usage.get("cache_read_input_tokens"),
                        cost_usd=_ate_cost,
                    )
                    if trace_id_out is not None and _new_trace_id is not None:
                        trace_id_out.append(_new_trace_id)
                except Exception:
                    pass  # ate.write_trace already fail-opens internally;
                          # this outer guard only protects the cost-math
                          # above it, which is new code in this call site
            return (text, stop_reason, usage) if return_stop_reason else text
        log("API", f"Error {resp.status_code}: {resp.text[:800]}")
        if error_out is not None:
            error_out.append(f"HTTP {resp.status_code}: {resp.text[:500]}")
        # Diagnostic fix (this session): the caller only ever saw a bare
        # None on failure, which evaluate_manual_prop turned into a
        # generic "could not read the screenshot" message no matter what
        # actually went wrong (oversized image, bad media_type, API
        # outage, malformed request). That made every screenshot failure
        # look identical from the user's side regardless of cause. Now
        # pushes the real status code + response body to #systems-channel
        # so the actual cause is visible without digging through the log
        # file after the fact.
        try:
            # Bug fix (this session, found immediately after adding this
            # alert): typo'd as "systems" (plural) -- the real webhook
            # dict key is "system" (singular), so this alert was silently
            # never reaching Discord since the moment it was written.
            send_discord("system",
                f"⚠️ API call failed — model={model}, status={resp.status_code}\n"
                f"```{resp.text[:500]}```")
        except Exception:
            pass
        if _ATE_AVAILABLE and step_type is not None:
            try:
                ate.write_trace(
                    eval_id=eval_id, step_type=step_type, model=model,
                    prompt=str(user_content), response="", success=False,
                    error_message=f"HTTP {resp.status_code}: {resp.text[:500]}",
                    latency_ms=_ate_latency_ms,
                )
            except Exception:
                pass
        return (None, None, {}) if return_stop_reason else None
    except Exception as e:
        log("API", f"Call failed: {e}")
        if error_out is not None:
            error_out.append(str(e))
        try:
            send_discord("system", f"⚠️ API call exception — model={model}: {e}")
        except Exception:
            pass
        if _ATE_AVAILABLE and step_type is not None:
            try:
                ate.write_trace(
                    eval_id=eval_id, step_type=step_type, model=model,
                    prompt=str(user_content), response="", success=False,
                    error_message=str(e),
                )
            except Exception:
                pass
        return (None, None, {}) if return_stop_reason else None

# ================================================================
# WEIGHTS
# ================================================================

DEFAULT_WEIGHTS = {
    # DEAD WEIGHTS STRIPPED (this session, approved): 24 prop-signal
    # entries removed (underdog_gw_less, lopsided_tg_less,
    # bp_strong_server, aces_surface_match, trend_confirm_last3,
    # favored_more_low_line, stage_competitive_more, line_buffer_3plus,
    # history_4of5, home_player_more_supports, trap_line_detected,
    # favored_gw_less_trap, bp_more_no_weak_server, bp_three_set_risk,
    # stage_competitive_tg_less, high_variance_confirmed,
    # surface_specialist_wrong, home_player_penalty_less,
    # home_player_more_unrelated, rule30_prev_round,
    # young_home_grand_slam, revenge_match_14days,
    # format_contamination, clustering_less, rule31_failure_mode).
    # Verified: each had exactly ONE reference in this file -- its own
    # definition. The v7.9.4 scoring rewrite replaced prop signal math
    # with hard caps; these numbers have influenced nothing since, but
    # the learning loop could still propose adjustments to them --
    # a wasted approval cycle that LOOKED like system improvement.
    # Stripping them also auto-gates proposals: the proposal math
    # requires current > 0, and a stripped key resolves to 0.
    # v7.9.6 — Moneyline Dominance Score weights (Step 8). Starting
    # defaults, not backtested — serve strength weighted highest since
    # it's historically the most predictive single serve/return factor
    # in tennis. These feed the SAME 5-in-14-days learning loop as prop
    # weights, so they calibrate from real moneyline results over time
    # rather than staying fixed at these initial guesses.
    "ml_serve_weight":           0.35,   # hold% differential
    "ml_return_weight":          0.25,   # break% differential
    "ml_clutch_weight":          0.15,   # BP save% differential
    "ml_form_weight":            0.25,   # current-season surface win% differential
    # (negative prop-signal weights removed here -- see the tombstone
    # comment at the top of this dict for the full stripped list.)
    # v7.9.6: persistent player profile TTL — how many hours of confirmed
    # V8 data to reuse before fetching fresh. Applies to both props and
    # moneyline evaluations. Default 24 hours matches the V8 API cache TTL.
    # Lower to get fresher data more often; raise during heavy multi-day
    # tournament use to reduce API call volume.
    "player_profile_ttl_hours":  24,

    # v7.9.7 (item 16): shorter TTL used specifically for Grand Slam
    # props (is_grand_slam flag), where matches happen daily and a
    # player's real stats update every day the tournament is live.
    # Falls back to player_profile_ttl_hours for all non-Grand-Slam
    # props. Tune lower for fresher in-major data, higher to reduce
    # API calls if 210k/month volume ever becomes a real constraint.
    "active_tournament_ttl_hours": 6,
}

def load_weights():
    if not WEIGHTS_FILE.exists():
        save_weights(DEFAULT_WEIGHTS)
        return DEFAULT_WEIGHTS.copy()
    with open(WEIGHTS_FILE) as f:
        loaded = json.load(f)
    merged = DEFAULT_WEIGHTS.copy()
    merged.update({k: v for k, v in loaded.items() if not k.startswith("_")})
    return merged

def save_weights(weights):
    weights["_last_updated"] = datetime.now(CT).isoformat()
    WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WEIGHTS_FILE, "w") as f:
        json.dump(weights, f, indent=2)

# ================================================================
# RESEARCH CACHE
# ================================================================

_research_cache = {}

def cache_key(player, surface, tournament):
    return f"{player.lower().strip()}_{surface}_{tournament}"

# ================================================================
# STAT_TYPE NORMALIZATION — structural fix (this session)
# ================================================================
# stat_type is created in at least 5 different places in this file:
# the PrizePicks API pull, the Playwright DOM scrape, screenshot
# vision intake, manual text intake, and test fixtures. Only test
# fixtures ever produced an already-normalized slug like
# "total_games_won" -- the vision extraction schema explicitly
# instructs the model to store "exact stat label as shown" (i.e.
# "Total Games Won", raw, capitalized, spaced), and the scrape/API
# paths were never audited for format either. Every direct
# stat_type == "some_slug" comparison anywhere else in this file
# (compliance rules, grading, direction logic, V8-coverage checks)
# was silently broken for any prop that didn't happen to already be
# lowercase-underscored -- confirmed live this session with a
# "Total Games Won" prop never matching the "total_games_won" key
# it needed to. This normalizes stat_type to one consistent format
# at every point a prop dict is actually constructed, so nothing
# downstream needs to guess or re-normalize.
def normalize_stat_type(raw):
    return (raw or "").strip().lower().replace(" ", "_")

# APEX-GATE11-OPPSUFFIX-2026-09-28 (hoisted to module level
# 2026-10-01 -- API-FIRST-FETCH): originally a local helper inside
# fetch_props_playwright() only, fixing PrizePicks' "vs {Opponent}
# 1st Set {countdown}" subtitle on the two 1st-Set tabs (158/158 Gate
# 11 rejections confirmed live 2026-09-28 before this fix). The 2026-
# 10-01 Fetch Performance Overhaul audit confirmed partner-api.
# prizepicks.com has the IDENTICAL quirk ("Pablo Carreno Busta 1st
# Set" instead of "Pablo Carreno Busta" in the projection's
# `description` field for 1st-Set markets -- 55/398 live props, 100%
# of them this one pattern). Hoisted here so fetch_props_partner_api()
# below reuses the SAME regex/function Playwright already relies on,
# rather than a second copy that could drift. fetch_props_playwright()
# now references this module-level version instead of defining its
# own (identical logic, nothing behavioral changed there).
_OPPONENT_SET_SUFFIX_RE = re.compile(
    r"\s+(?:1st|2nd|3rd|first|second|third)\s+set\s*$", re.IGNORECASE)

def _clean_scraped_opponent(_name):
    if not _name:
        return _name
    return _OPPONENT_SET_SUFFIX_RE.sub("", _name).strip()

# ================================================================
# *** NEW *** RESEARCH SOURCE VALIDATOR
# ================================================================

def validate_research_source(search_result_text, player, stat_requested, dates):
    """
    Checks whether research result came from a verified source.
    Returns: (is_verified, source_found, confidence_cap)

    Bug fix (this session): this used to send a Discord alert directly,
    once per failed query, with zero awareness of whether V8 had
    already confirmed the exact stat being asked about. Confirmed live:
    a player whose Aces/Break Points Won/Total Games Won/Total Games
    were ALL already V8-confirmed (Tier 1) still generated a wall of
    "UNVERIFIED RESEARCH SOURCE... capped at YELLOW" alerts, because
    this function's generic web-search queries (mostly about ranking/
    injury/general results, not the specific prop stat) failed to find
    a source and treated that as gospel about a stat V8 already had.
    It no longer sends anything itself -- it just reports what it
    found. research_player() below decides whether an alert is
    actually warranted, after checking V8 coverage, and sends at most
    one summary alert per player per evaluation instead of one per
    query.
    """
    if not search_result_text:
        return False, None, "YELLOW"

    result_lower = search_result_text.lower()
    source_found = None

    for source in VERIFIED_SOURCES:
        if source in result_lower:
            source_found = source
            break

    if not source_found:
        log("Research", f"Unverified source for {player} {stat_requested}")
        return False, None, "YELLOW"

    log("Research", f"Verified source: {source_found} for {player}")
    return True, source_found, None

# ================================================================
# *** NEW *** FORMAT VERIFICATION (best-of-3 vs best-of-5 mixing)
# ================================================================

def format_verification_check(prop, last_5_results, dates):
    """
    CRITICAL: Prevents mixing best-of-3 and best-of-5 data.
    For Grand Slam props — only uses best-of-5 results in average.
    If fewer than 3 best-of-5 results exist → cap YELLOW immediately.

    This fixes the Svajda aces bust where best-of-3 averages (2.5)
    were applied to a best-of-5 Grand Slam line (8.5).

    v7.9.7 fix (item 1): previously ran a fresh web search per result
    to confirm best-of-3 vs best-of-5 — even though V8's own
    get_recent_matches() already returns a "best_of" field directly on
    each match object (confirmed: this exact field is already read
    elsewhere in this file, e.g. line ~2645).

    IMPORTANT LIMITATION, stated honestly: last_5_results here comes
    from recent_stat_results, which is built by the web-search-based
    research extraction and is genuinely just [number, number, ...] —
    no date, no opponent, nothing to positively match against a V8
    match record. That data was thrown away upstream long before this
    function ever sees it, and fixing that fully would require
    restructuring research_agent's data flow — out of scope for this
    fix. So this function pulls the player's most recent N real V8
    matches directly and pairs them BY POSITION (most recent V8 match
    <-> most recent stat result, and so on) as a reasonable proxy for
    "probably the same matches" — not a guaranteed exact match. If V8
    doesn't have enough matches to pair, or a specific position's
    best_of comes back null, falls through to web search for that one
    result only, same as the original behavior.
    """
    is_grand_slam = prop.get("is_grand_slam", False)

    if not is_grand_slam:
        # Not a Grand Slam — no format check needed
        return last_5_results, False

    player = prop.get("player", "")

    # Pull the player's own most recent real V8 matches, in order, to
    # pair positionally against last_5_results (see limitation note above).
    v8_recent_ordered = []
    if _V8_AVAILABLE:
        try:
            tour = "wta" if prop.get("tour", "atp").lower() == "wta" else "atp"
            canonical = v8_resolve_player_name(player, tour)
            if canonical:
                v8_recent_ordered = v8_get_recent_matches(canonical, limit=len(last_5_results))
        except Exception as e:
            log("Format", f"V8 lookup for format check failed (non-fatal): {e}")

    verified_bo5 = []
    verified_bo3 = []

    for i, result in enumerate(last_5_results):
        opponent   = result.get("opponent", "Unknown")
        match_date = result.get("date", "")

        # ── Try V8's real best_of field first, paired by position ──
        v8_match = v8_recent_ordered[i] if i < len(v8_recent_ordered) else None
        best_of  = v8_match.get("best_of") if v8_match else None

        if best_of == 5:
            result["confirmed_format"] = "best_of_5"
            verified_bo5.append(result)
            continue
        elif best_of == 3:
            result["confirmed_format"] = "best_of_3"
            verified_bo3.append(result)
            continue

        # ── Fallback: web search, only when V8 didn't have a usable
        # best_of for this position (no match at that index, or
        # best_of was null in the V8 response itself) ──
        search_text = web_search(
            f"{player} vs {opponent} {match_date} sets played grand slam ATP WTA"
        )

        text_lower = search_text.lower() if search_text else ""

        if any(kw in text_lower for kw in
               ["grand slam", "best of 5", "australian open", "roland garros",
                "french open", "wimbledon", "us open", "5 sets"]):
            result["confirmed_format"] = "best_of_5"
            verified_bo5.append(result)
        elif any(kw in text_lower for kw in
                 ["best of 3", "atp 250", "atp 500", "atp 1000", "wta 500",
                  "wta 1000", "challenger", "3 sets"]):
            result["confirmed_format"] = "best_of_3"
            verified_bo3.append(result)
        else:
            result["confirmed_format"] = "unknown"
            verified_bo3.append(result)  # treat unknown as bo3 (conservative)

    contaminated = len(verified_bo3) > 0
    bo5_count    = len(verified_bo5)

    if contaminated:
        log("Format", f"Mixed formats detected for {prop.get('player')}: "
                      f"{bo5_count} bo5, {len(verified_bo3)} bo3/unknown")

    if bo5_count < 3:
        send_discord("alerts",
            f"⚠️ **FORMAT CONTAMINATION** — {dates['ct_time']}\n"
            f"Player: {prop.get('player')} | Stat: {prop.get('stat_type')}\n"
            f"Grand Slam prop — only {bo5_count} of 5 results are best-of-5\n"
            f"Remaining results are best-of-3 (different format)\n"
            f"Mixed average would be MISLEADING\n"
            f"**Prop capped at YELLOW — insufficient Grand Slam data**\n"
            f"Best-of-5 results found: {bo5_count}/5"
        )
        prop["format_contamination"]  = True
        prop["confidence_cap"]        = "YELLOW"
        prop["bo5_results_available"] = bo5_count

    return verified_bo5 if bo5_count >= 3 else last_5_results, contaminated

# ================================================================
# *** NEW *** RULE DISCOVERY ENGINE
# ================================================================

def rule_discovery_check(session_results, dates):
    """
    After each session — looks for patterns in busts that suggest
    a new structural rule rather than just a weight adjustment.
    Flags candidates to #learning for approval.

    Cannot update rules automatically — only surfaces candidates.
    You reply IMPLEMENT or MONITOR to each candidate.
    """
    if not session_results:
        return

    busts = [r for r in session_results if r.get("outcome") == "BUST"]
    if len(busts) < 2:
        return

    # Pattern detectors
    patterns = []

    # Pattern: format contamination appeared in multiple busts
    format_busts = [b for b in busts if b.get("format_contamination")]
    if len(format_busts) >= 2:
        patterns.append({
            "name":        "format_contamination_repeat",
            "description": "Best-of-3 data applied to best-of-5 Grand Slam lines",
            "count":       len(format_busts),
            "players":     [b.get("player") for b in format_busts],
            "action":      "Enforce format_verification_check() on all Grand Slam props",
            "priority":    "HIGH"
        })

    # Pattern: young player (under 19) + home tournament produced bust
    young_home_busts = [
        b for b in busts
        if b.get("opponent_age", 99) < 19
        and b.get("home_crowd_opponent", False)
    ]
    if len(young_home_busts) >= 1:
        patterns.append({
            "name":        "young_home_player_variance",
            "description": "Opponent under 19 at home Grand Slam — exceeded ranking prediction",
            "count":       len(young_home_busts),
            "players":     [b.get("player") for b in young_home_busts],
            "action":      "Additional -6% when opponent is under 19 at home Grand Slam",
            "priority":    "MEDIUM"
        })

    # Pattern: revenge match within 14 days produced bust on MORE
    revenge_busts = [
        b for b in busts
        if b.get("revenge_match_flag", False)
        and b.get("direction") == "MORE"
    ]
    if len(revenge_busts) >= 1:
        patterns.append({
            "name":        "revenge_match_more_bust",
            "description": "Goblin MORE bust where opponent had revenge motivation (met within 14 days)",
            "count":       len(revenge_busts),
            "players":     [b.get("player") for b in revenge_busts],
            "action":      "Apply -8% to goblin MORE when opponent lost to player within 14 days",
            "priority":    "MEDIUM"
        })

    # Pattern: fatigue did not suppress BP as expected
    fatigue_bp_busts = [
        b for b in busts
        if b.get("fatigue_flag", False)
        and b.get("stat_type") == "break_points_won"
        and b.get("direction") == "LESS"
    ]
    if len(fatigue_bp_busts) >= 1:
        patterns.append({
            "name":        "fatigue_neutral_on_bp",
            "description": "Fatigue signal used to confirm BP LESS — busted because tired returners fight harder",
            "count":       len(fatigue_bp_busts),
            "players":     [b.get("player") for b in fatigue_bp_busts],
            "action":      "Remove fatigue as positive signal for BP LESS — treat as neutral",
            "priority":    "MEDIUM"
        })

    if not patterns:
        send_discord("system",
            f"🔍 Rule discovery: No new structural patterns found in {len(busts)} busts"
        )
        return

    # Send each pattern as a separate approval request
    for p in patterns:
        send_discord("learning",
            f"🔍 **RULE DISCOVERY CANDIDATE** — {dates['ct_time']}\n"
            f"Pattern: `{p['name']}`\n"
            f"Description: {p['description']}\n"
            f"Appeared in: {p['count']} bust(s) today\n"
            f"Players affected: {', '.join(p['players'])}\n"
            f"Suggested action: {p['action']}\n"
            f"Priority: {p['priority']}\n\n"
            f"Reply **IMPLEMENT** → creates GitHub PR with code change\n"
            f"Reply **MONITOR** → tracks pattern for 30 days before committing\n"
            f"Reply **REJECT** → dismiss this pattern"
        )

# ================================================================
# *** NEW *** PROP FETCHING — API + PLAYWRIGHT FALLBACK
# ================================================================

NON_COMPLETION_KEYWORDS = [
    "retired", "ret.", "walkover", "w/o", "withdrawal", "injured",
    "defaulted", "postponed", "suspended", "rain", "weather",
    "abandoned", "not completed", "early retirement", "retires"
]

# ================================================================
# APEX-GOBLIN-CLASSIFY-DIAG-2026-09-28 -- diagnostics-only evidence log.
# Appends one row per fetched prop, capturing the raw discount_percentage /
# is_promo values from PrizePicks and the reason is_goblin/is_demon came out
# the way it did. Never read by, and never influences, classification,
# filtering, ranking, or the engine -- write-only, fail-open, purely so the
# evidence exists to later investigate whether is_promo-without-discount
# props are real Goblin markets or something else. Directive: "gather
# sufficient evidence before changing classification logic."
# ================================================================
import csv as _apex_csv_diag
_APEX_GOBLIN_DIAG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "apex_goblin_classification_diag_v1.csv")
_APEX_GOBLIN_DIAG_FIELDS = ["ts", "match_date", "player", "opponent", "stat_type", "line",
                            "discount_percentage", "is_promo", "is_demon", "is_goblin", "reason"]


def _apex_log_goblin_classify_diag(player, opponent, stat_type, line, match_date,
                                    discount_percentage, is_promo, is_demon, is_goblin, reason):
    row = {"ts": datetime.now().isoformat(timespec="seconds"),
           "match_date": match_date, "player": player, "opponent": opponent,
           "stat_type": stat_type, "line": line, "discount_percentage": discount_percentage,
           "is_promo": is_promo, "is_demon": is_demon, "is_goblin": is_goblin, "reason": reason}
    _is_new = not os.path.exists(_APEX_GOBLIN_DIAG_LOG)
    with open(_APEX_GOBLIN_DIAG_LOG, "a", newline="", encoding="utf-8") as fh:
        w = _apex_csv_diag.DictWriter(fh, fieldnames=_APEX_GOBLIN_DIAG_FIELDS)
        if _is_new:
            w.writeheader()
        w.writerow(row)

def fetch_prizepicks_api(match_date):
    """
    PRIMARY: PrizePicks unofficial API — Scenario B.
    Returns props for match_date (tomorrow).
    No authentication needed for public props.
    """
    try:
        resp = requests.get(
            PRIZEPICKS_API,
            params={
                "league_id":   9,
                "per_page":    250,
                "single_stat": True,
                "in_game":     False,
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Referer":    "https://app.prizepicks.com/",
                "Origin":     "https://app.prizepicks.com",
            },
            timeout=15
        )

        if resp.status_code != 200:
            log("Fetch", f"API status {resp.status_code}")
            return []

        data  = resp.json()
        props = []

        for proj in data.get("data", []):
            attrs      = proj.get("attributes", {})
            start_time = attrs.get("start_time", "")

            if str(match_date) not in start_time:
                continue

            league = attrs.get("league", "").upper()
            if league not in ("TENNIS", "ATP", "WTA"):
                continue

            # Classify prop type
            is_demon  = attrs.get("is_promo", False) and attrs.get("discount_percentage", 0) < 0
            is_goblin = (attrs.get("discount_percentage", 0) > 0 or
                         attrs.get("is_promo", False)) and not is_demon

            # APEX-GOBLIN-CLASSIFY-DIAG-2026-09-28: diagnostics only, no
            # behavior change. discount_percentage/is_promo were computed
            # above but never carried past this point or logged anywhere --
            # there was no evidence trail to investigate the classifier
            # with. This just keeps the raw values (as new, additional
            # dict keys -- nothing existing is altered) and records why
            # is_goblin came out the way it did, one row per fetched prop.
            _dp = attrs.get("discount_percentage", 0)
            _ip = attrs.get("is_promo", False)
            if is_demon:
                _reason = "is_demon (is_promo + discount<0)"
            elif _dp and float(_dp) > 0:
                _reason = "discount_percentage>0 (%.4g)" % float(_dp)
            elif _ip and (not _dp or float(_dp) == 0):
                _reason = "is_promo_true_discount_zero"
            elif _ip:
                _reason = "is_promo_true_discount_other (%.4g)" % float(_dp or 0)
            else:
                _reason = "neither_flag (standard)"
            try:
                _apex_log_goblin_classify_diag(
                    player=attrs.get("name", ""), opponent=attrs.get("description", ""),
                    stat_type=attrs.get("stat_type", ""), line=attrs.get("line_score", 0),
                    match_date=start_time[:10], discount_percentage=_dp, is_promo=_ip,
                    is_demon=is_demon, is_goblin=is_goblin, reason=_reason)
            except Exception as _e:
                log("Fetch", f"goblin classify diag log failed (non-fatal): {_e}")

            props.append({
                "id":                        proj.get("id"),
                "player":                    attrs.get("name", ""),
                "opponent":                  (attrs.get("description", "")
                                              .split(" vs ")[-1]
                                              if " vs " in attrs.get("description", "")
                                              else ""),
                "stat_type":                 normalize_stat_type(attrs.get("stat_type", "")),
                "line":                      float(attrs.get("line_score", 0)),
                "tournament":                attrs.get("league", "Tennis"),
                "surface":                   attrs.get("surface", "clay"),
                "tournament_stage":          attrs.get("tournament_stage", 1),
                "round":                     attrs.get("round", ""),
                "is_grand_slam":             attrs.get("is_grand_slam", False),
                "both_directions_available": not attrs.get("is_promo", False),
                "is_goblin":                 is_goblin,
                "is_demon":                  is_demon,
                "match_date":                start_time[:10],
                "same_day_3set_count":       0,
                "discount_percentage":       _dp,          # APEX-GOBLIN-CLASSIFY-DIAG-2026-09-28
                "is_promo_raw":              _ip,          # new field, additive only
                "goblin_classify_reason":    _reason,      # new field, additive only
            })

        log("Fetch", f"API success: {len(props)} props for {match_date}")
        return props

    except Exception as e:
        log("Fetch", f"API failed: {e}")
        return []

# APEX-DISCORD-FETCH-2026-09-28: last-scrape stats, stashed here so a
# caller (e.g. the /fetch Discord command) can read per-tab and raw/
# deduped counts without changing fetch_props_playwright()'s existing
# return signature (still just the props list, exactly as every other
# caller already expects).
LAST_PLAYWRIGHT_SCRAPE_STATS = {}
LAST_FETCH_TAB_DIAGNOSTICS = {}  # APEX-FETCH-DIAG-2026-09-28: temporary per-tab click/element diagnostics
LAST_TAB_RETRY_REPORT = {}  # APEX-TABRETRY-2026-09-30: tabs_processed/first_try_ok/retry_recovered/failed_final
LAST_FULLTIER_API_STATS = {}  # APEX-FULLTIER-2026-09-29: captured-response full-tier ingestion stats
LAST_FETCH_FAILURE = {}  # APEX-FETCHPIPE-2026-09-30: see _record_fetch_failure()/describe_last_fetch_failure()


def _record_fetch_failure(stage, headline, exc=None, page=None):
    """APEX-FETCHPIPE-2026-09-30: replaces the old single hardcoded guess
    ("Most likely Chrome isn't running...") that discord_bot.py used to
    show on every /fetch failure regardless of what actually happened --
    confirmed live on 2026-09-30 that the guess was flat wrong (Chrome WAS
    running) and its referenced screenshot was 2 days stale. Every failure
    now gets: a specific stage, the real exception text, and (when a page
    object exists) its own timestamped screenshot + HTML snapshot -- never
    overwriting a prior failure's evidence."""
    global LAST_FETCH_FAILURE
    ts = datetime.now(CT).strftime("%Y%m%d_%H%M%S")
    rec = {
        "stage": stage,
        "headline": headline,
        "exception": ("%s: %s" % (type(exc).__name__, exc)) if exc is not None else None,
        "timestamp": datetime.now(CT).isoformat(),
        "screenshot": None,
        "html": None,
    }
    if page is not None:
        try:
            shot_path = FETCH_DEBUG_DIR / ("fetch_fail_%s_%s.png" % (ts, stage))
            page.screenshot(path=str(shot_path))
            rec["screenshot"] = str(shot_path)
        except Exception as _e:
            log("Playwright", "Debug screenshot capture failed (non-fatal): %s" % _e)
        try:
            html_path = FETCH_DEBUG_DIR / ("fetch_fail_%s_%s.html" % (ts, stage))
            html_path.write_text(page.content(), encoding="utf-8")
            rec["html"] = str(html_path)
        except Exception as _e:
            log("Playwright", "Debug HTML capture failed (non-fatal): %s" % _e)
    LAST_FETCH_FAILURE = rec
    log("Playwright", "FETCH FAILURE [%s] %s%s" % (
        stage, headline, (" -- %s" % rec["exception"]) if rec["exception"] else ""))


def describe_last_fetch_failure():
    """APEX-FETCHPIPE-2026-09-30: human-readable version of LAST_FETCH_FAILURE
    for discord_bot.py to show instead of a generic guess."""
    f = LAST_FETCH_FAILURE
    if not f:
        return ("Scrape returned 0 props, but no diagnostic detail was captured "
                "for this attempt (unexpected -- please report this).")
    lines = [f["headline"], "Stage: %s" % f["stage"]]
    if f.get("exception"):
        lines.append("Exception: %s" % f["exception"])
    if f.get("screenshot"):
        lines.append("Screenshot: %s" % f["screenshot"])
    if f.get("html"):
        lines.append("HTML snapshot: %s" % f["html"])
    lines.append("Captured at: %s" % f["timestamp"])
    return "\n".join(lines)


def fetch_props_playwright(match_date):
    """
    FALLBACK: Playwright browser automation — Option A.

    APEX-FETCHPIPE-2026-09-30: previously attached to an already-running,
    manually-launched Chrome via connect_over_cdp() on port 9222. Confirmed
    live on 2026-09-30 that this architecture is broken against the current
    Chrome build: the websocket connects fine, but Chrome's own CDP handler
    rejects Playwright's browser-level Browser.setDownloadBehavior handshake
    call with "Browser context management is not supported" -- a documented,
    currently-unfixed Playwright limitation (microsoft/playwright #15370,
    #35131), not a config problem on this machine. No manually-launched
    Chrome or open port is needed any more, or checked for -- Playwright now
    launches and owns its own Chrome instance against the SAME persistent
    profile (CHROME_PRIZEPICKS_PROFILE), which never needs that handshake.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("Playwright", "Not installed — run: pip install playwright && playwright install chromium")
        return []

    global LAST_FETCH_FAILURE
    LAST_FETCH_FAILURE = {}   # APEX-FETCHPIPE-2026-09-30: clear stale state from any prior attempt

    props = []
    try:
        with sync_playwright() as p:
            log("Playwright", "Launching Playwright-owned Chrome (persistent PrizePicks profile)...")

            try:
                context = p.chromium.launch_persistent_context(
                    CHROME_PRIZEPICKS_PROFILE,
                    channel="chrome",
                    headless=False,
                    args=["--no-first-run", "--no-default-browser-check"],
                )
            except Exception as e:
                _record_fetch_failure("chrome_launch", "Chrome unavailable", e)
                return []

            # APEX-FETCH-STRAYTABS-2026-09-28: real incident this
            # session -- repeated manual test/validation runs (and any
            # run that hit an unhandled exception before reaching this
            # function's page.close() below) had each left their own
            # PrizePicks board tab open in this shared debug Chrome
            # profile. By tonight, 8 duplicate board tabs plus ~26
            # leftover fingerprinting iframes/worker blobs had piled up
            # in the SAME renderer process, and confirmed live: every
            # tab-switch click below started timing out mid-action
            # ("element visible/enabled/stable... performing click
            # action" then a bare 4000ms timeout with no completion) --
            # classic symptom of a resource-starved renderer, not a
            # selector bug. A live diagnostic run showed only 1-2 of 11
            # tabs succeeding, a DIFFERENT 1-2 each time (Popular+Fantasy
            # Score once, Total Games+Double Faults the next) -- proving
            # it was contention, not something deterministic about any
            # one tab. Closing the stray tabs took the browser from 34
            # open targets to 6 and brought it straight back to 11/11
            # tabs clicking and extracting cleanly. Doing this
            # automatically here, before opening this run's own page,
            # so the fix doesn't depend on remembering to clean up by
            # hand before every scrape.
            try:
                _stray = list(context.pages)
                if len(_stray) > 1:
                    for _p in _stray:
                        try:
                            _p.close()
                        except Exception:
                            pass
                    log("Playwright",
                        f"Closed {len(_stray)} stray tab(s) from earlier "
                        f"runs before starting this scrape.")
            except Exception as _e:
                log("Playwright", f"Stray-tab cleanup skipped (non-fatal): {_e}")

            page = context.new_page()

            # APEX-FULLTIER-2026-09-29: PrizePicks' desktop board only
            # ever RENDERS one card per (player, market) -- but the
            # live api.prizepicks.com/projections response the page
            # itself downloads to build that board carries every tier
            # (odds_type demon/goblin/standard at every line) for the
            # same player+market. A live audit confirmed the rendered
            # board misses ~42% of real, playable props this way. Per
            # explicit instruction: do NOT hit this API directly (it is
            # behind DataDome and returns 403/429 to any request that
            # didn't originate from the real page) -- only listen for
            # the response the browser already receives on its own.
            # Attached before navigation so it catches the very first
            # load's request too, not just later tab clicks.
            _api_captures = []

            def _capture_projections_response(_resp):
                try:
                    # APEX-FULLTIER-LEAGUEFILTER-2026-09-29: the board can
                    # briefly be on a different sport's default league
                    # (e.g. NFL) before the Tennis filter click below takes
                    # effect, firing its own projections response first.
                    # Only league_id=5 (Tennis) is kept -- confirmed live
                    # that an unfiltered capture pulled in stray NFL
                    # records (e.g. "AJ Barner vs LAC") that would have
                    # gone straight into the tennis pipeline as garbage.
                    if ("api.prizepicks.com/projections" in _resp.url
                            and "league_id=5" in _resp.url
                            and _resp.status == 200):
                        _api_captures.append(_resp.json())
                except Exception:
                    pass

            page.on("response", _capture_projections_response)

            # APEX-PLAYWRIGHT-GEOLOC-2026-09-28: PrizePicks shows a
            # blocking "Where are you?" location-eligibility modal over
            # the board for any context with no geolocation permission
            # granted -- the same DFS state-eligibility gate a real
            # visitor sees, unrelated to bot detection. It sits on top
            # of every filter button, so the click-selector loop below
            # was clicking the modal backdrop and silently finding
            # nothing (confirmed via before/after debug screenshots).
            # Granting geolocation up front makes the site skip it.
            try:
                context.grant_permissions(
                    ["geolocation"], origin="https://app.prizepicks.com")
                context.set_geolocation(
                    {"latitude": 30.6944, "longitude": -88.0431})  # Mobile, AL
            except Exception as _e:
                log("Playwright", f"geolocation permission grant failed (non-fatal): {_e}")

            # Navigate to PrizePicks tennis
            log("Playwright", "Navigating to PrizePicks tennis...")
            try:
                page.goto("https://app.prizepicks.com/board", timeout=30000)
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception as e:
                # APEX-FETCHPIPE-2026-09-30: browser launched fine, but the
                # site itself didn't load in time -- a genuinely different
                # failure from "Chrome unavailable" above, so it gets its
                # own stage/message and its own screenshot+HTML capture.
                _record_fetch_failure("navigation", "PrizePicks unreachable", e, page=page)
                try:
                    context.close()
                except Exception:
                    pass
                return []

            # Wait for content to load
            page.wait_for_timeout(3000)

            # Try to click tennis/ATP filter
            for selector in [
                '[data-sport="Tennis"]',
                'button:has-text("Tennis")',
                'button:has-text("ATP")',
                '[aria-label="Tennis"]',
            ]:
                try:
                    page.click(selector, timeout=3000)
                    page.wait_for_timeout(2000)
                    log("Playwright", f"Clicked tennis filter: {selector}")
                    break
                except:
                    continue

            # APEX-PLAYWRIGHT-EXTRACT-2026-09-28: the old selectors
            # ('[class*="projection"]', '[class*="prop-card"]', etc.)
            # matched nothing against the live site -- PrizePicks' real
            # markup uses Tailwind utility classes with no "projection"/
            # "prop-card" substring anywhere. Card boundary is
            # '#scroll-proj-container li' (confirmed against a live page
            # dump 2026-09-28); PrizePicks itself annotates the fields
            # that matter with test ids (#test-player-name,
            # #test-team-position), which are far more stable than class
            # names a redesign will change. Also reads the Demon/Goblin
            # badge image's alt text -- ground truth from PrizePicks' own
            # UI -- so playwright-sourced props get real is_goblin/
            # is_demon flags instead of the previous hardcoded False/False.
            _EXTRACT_CARDS_JS = """
                () => {
                    const results = [];
                    const cards = document.querySelectorAll('#scroll-proj-container li');
                    cards.forEach(card => {
                        try {
                            const player = card.querySelector('#test-player-name, [id="test-player-name"]')
                                ?.textContent?.trim();
                            const timeEl = card.querySelector('time');
                            const opponent = timeEl?.querySelector('span')?.textContent?.trim() || '';
                            const statValueEl = card.querySelector('.heading-md span, [class*="heading-md"] span');
                            const line = statValueEl?.textContent?.trim() || '';
                            const statLabelEl = card.querySelector('.break-words, [class*="break-words"]');
                            const stat = statLabelEl?.textContent?.trim() || '';
                            const badgeImg = card.querySelector('img[alt="Demon"], img[alt="Goblin"]');
                            const badge = badgeImg ? badgeImg.getAttribute('alt') : null;
                            if (player && line) {
                                results.push({ player, line, stat, opponent, badge });
                            }
                        } catch(e) {}
                    });
                    return results;
                }
            """

            # APEX-PLAYWRIGHT-ALLTABS-2026-09-28: PrizePicks splits Tennis
            # into 10 separate stat-category tabs (Popular is an 11th,
            # curated/overlapping "trending" view) -- clicking "Tennis"
            # alone only ever lands on whichever one is the default
            # (Popular), so the scraper was silently seeing ~20 of the
            # ~489 tennis props actually on the board (confirmed via a
            # live audit 2026-09-28: 156 Demon + 130 Goblin + 203
            # Standard = 489 total, vs. 20 captured). Iterating every tab
            # and deduping the union fixes that; Popular is included for
            # completeness even though it's expected to be pure overlap.
            _TENNIS_TABS = [
                "Popular", "Total Games", "Total Games Won", "Fantasy Score",
                "Total Sets", "Aces", "Break Points Won", "Total Tie Breaks",
                "1st Set Total Games", "1st Set Total Games Won", "Double Faults",
            ]

            # APEX-GATE11-OPPSUFFIX-2026-09-28: PrizePicks renders the
            # "1st Set Total Games"/"1st Set Total Games Won" tabs'
            # opponent subtitle as "vs {Opponent} 1st Set {countdown}",
            # so the raw scrape was capturing "{Opponent} 1st Set" as the
            # opponent field verbatim. That contaminated string never
            # matches a real player name, so Gate 11 rejected 100% of
            # props from these two tabs (158/158 confirmed via a live
            # forensic audit 2026-09-28 -- every rejection this run had
            # this exact suffix, and every one resolved cleanly in V8
            # once stripped). Stripped here, immediately after each
            # tab's raw extraction, so it's clean before the dedup key
            # is built AND before it reaches Gate 11/V8 lookups anywhere
            # downstream. _OPPONENT_SET_SUFFIX_RE / _clean_scraped_opponent
            # moved to module level 2026-10-01 (API-FIRST-FETCH) so
            # fetch_props_partner_api() can reuse the identical logic --
            # behavior here is unchanged, just no longer redefined locally.

            # APEX-FETCH-DIAG-2026-09-28: temporary richer per-tab
            # diagnostics (click success, raw <li> element count right
            # after the click/scroll, and a screenshot when a tab comes
            # back empty) -- added to investigate a real live /fetch
            # result showing only Popular + Fantasy Score returning data
            # tonight, with every other tab at 0. Purely additive
            # logging; the extraction/dedup behavior below is unchanged.
            _per_tab_raw = {}
            _per_tab_diag = {}
            _all_raw = []
            _run_ts = datetime.now(CT).strftime("%Y%m%d_%H%M%S")   # APEX-FETCHPIPE-2026-09-30
            # APEX-TABRETRY-2026-09-30: tab-click reliability pass (audit-
            # driven -- 2026-09-30 Tab Contention audit found the same 3
            # tabs failing on a single 4s click attempt with zero retries).
            # Up to 3 click attempts per tab (1s backoff after attempt 1,
            # 2s backoff after attempt 2), plus a short best-effort
            # networkidle wait before each attempt to let the prior tab's
            # DOM swap settle. Only the CLICK is retried -- the scroll/
            # extraction/dedup logic below is byte-for-byte unchanged, and
            # nothing here touches Apex Score, ranking, confidence, ALIS,
            # or the freshness system.
            _tabs_first_try_ok = 0
            _tabs_retry_recovered = 0
            _tabs_failed_final = 0
            for _tab_name in _TENNIS_TABS:
                if fetch_cancel_requested():   # APEX-FETCHCANCEL-2026-10-12
                    log("Playwright", f"Cancel requested -- stopping before tab '{_tab_name}'")
                    break
                _tab_start_t = time.time()
                _click_ok = False
                _li_count_post_click = None
                _attempt_used = 0
                _last_click_exc = None
                for _attempt in range(1, 4):   # attempts 1, 2, 3 -- max 3
                    _attempt_used = _attempt
                    if _attempt > 1:
                        page.wait_for_timeout(1000 if _attempt == 2 else 2000)
                    try:
                        # Pre-click stabilization: let the page settle.
                        # Short timeout, continue anyway if it doesn't idle.
                        page.wait_for_load_state("networkidle", timeout=1500)
                    except Exception:
                        pass
                    try:
                        page.click(f'button:has-text("{_tab_name}")', timeout=4000)
                        _click_ok = True
                        break
                    except Exception as _click_e:
                        _last_click_exc = _click_e
                        continue

                _tab_raw = []
                if _click_ok:
                    try:
                        page.wait_for_timeout(1200)
                        try:
                            page.evaluate(
                                "() => { const c = document.querySelector('#scroll-proj-container'); "
                                "if (c) c.scrollTop = c.scrollHeight; }"
                            )
                            page.wait_for_timeout(600)
                        except Exception:
                            pass
                        try:
                            _li_count_post_click = page.evaluate(
                                "() => document.querySelectorAll('#scroll-proj-container li').length"
                            )
                        except Exception:
                            _li_count_post_click = None
                        _tab_raw = page.evaluate(_EXTRACT_CARDS_JS)
                        for _rec in _tab_raw:
                            _rec["opponent"] = _clean_scraped_opponent(_rec.get("opponent", ""))
                    except Exception as _tab_e:
                        log("Playwright", f"Tab '{_tab_name}' post-click scrape failed (non-fatal): {_tab_e}")
                        _tab_raw = []
                else:
                    log("Playwright",
                        f"Tab '{_tab_name}' scrape failed (non-fatal) after {_attempt_used} attempt(s): {_last_click_exc}")

                _tab_elapsed = round(time.time() - _tab_start_t, 2)

                # Post-click validation: did expected content actually show
                # up? A click that "succeeded" but produced zero <li>
                # elements counts the same as a click failure below -- no
                # real content appeared either way.
                _content_appeared = _click_ok and (_li_count_post_click or 0) > 0
                if _content_appeared and _attempt_used == 1:
                    _tabs_first_try_ok += 1
                elif _content_appeared and _attempt_used > 1:
                    _tabs_retry_recovered += 1
                else:
                    _tabs_failed_final += 1

                log("Playwright",
                    f"DIAG tab='{_tab_name}' click_ok={_click_ok} attempt={_attempt_used} "
                    f"li_elements_post_click={_li_count_post_click} "
                    f"props_extracted={len(_tab_raw)} elapsed={_tab_elapsed}s")
                if _click_ok and len(_tab_raw) == 0:
                    try:
                        _safe_name = _tab_name.replace(' ', '_')
                        # APEX-FETCHPIPE-2026-09-30: timestamped, never overwritten
                        page.screenshot(path=str(
                            FETCH_DEBUG_DIR / f"fetch_diag_{_run_ts}_{_safe_name}.png"))
                    except Exception:
                        pass
                _per_tab_diag[_tab_name] = {
                    "click_ok": _click_ok,
                    "attempt": _attempt_used,
                    "li_elements_post_click": _li_count_post_click,
                    "props_extracted": len(_tab_raw),
                    "elapsed_s": _tab_elapsed,
                }
                _per_tab_raw[_tab_name] = len(_tab_raw)
                _all_raw.extend(_tab_raw)

            global LAST_FETCH_TAB_DIAGNOSTICS
            LAST_FETCH_TAB_DIAGNOSTICS = _per_tab_diag

            log("Playwright", "Per-tab raw counts: " +
                ", ".join(f"{k}={v}" for k, v in _per_tab_raw.items()))

            # APEX-TABRETRY-2026-09-30: failed-tab summary report
            log("Playwright",
                f"TAB REPORT -- Tabs Processed: {len(_TENNIS_TABS)} | "
                f"Successful First Try: {_tabs_first_try_ok} | "
                f"Recovered By Retry: {_tabs_retry_recovered} | "
                f"Failed After Retries: {_tabs_failed_final}")
            global LAST_TAB_RETRY_REPORT
            LAST_TAB_RETRY_REPORT = {
                "tabs_processed": len(_TENNIS_TABS),
                "first_try_ok": _tabs_first_try_ok,
                "retry_recovered": _tabs_retry_recovered,
                "failed_final": _tabs_failed_final,
            }

            # APEX-FETCHPIPE-2026-09-30: all tabs clicked/loaded fine but
            # extraction came back empty across the board -- a real,
            # distinct failure mode ("D" in the fetch-repair spec), not a
            # launch or navigation problem, so it gets its own stage and
            # its own timestamped screenshot+HTML capture.
            if not _all_raw:
                _record_fetch_failure("extraction", "Connected successfully but extracted 0 props",
                                       None, page=page)

            # Dedup on (player, stat_type, opponent, line) -- the same
            # match/stat can legitimately render on more than one tab
            # (e.g. Popular always overlaps with whichever category a
            # trending prop belongs to); first occurrence wins.
            _seen_keys = set()
            raw_props = []
            for r in _all_raw:
                try:
                    _dedup_stat = normalize_stat_type(r.get("stat", ""))
                    _dedup_line = float(re.sub(r"[^\d.]", "", r.get("line", "0")) or 0)
                    _dedup_key = (
                        (r.get("player") or "").strip().lower(),
                        _dedup_stat,
                        (r.get("opponent") or "").strip().lower(),
                        _dedup_line,
                    )
                except Exception:
                    _dedup_key = None
                if _dedup_key is not None:
                    if _dedup_key in _seen_keys:
                        continue
                    _seen_keys.add(_dedup_key)
                raw_props.append(r)

            log("Playwright", f"Raw {len(_all_raw)} across {len(_TENNIS_TABS)} tabs -> "
                              f"deduped {len(raw_props)}")

            global LAST_PLAYWRIGHT_SCRAPE_STATS
            LAST_PLAYWRIGHT_SCRAPE_STATS = {
                "per_tab":       dict(_per_tab_raw),
                "raw_total":     len(_all_raw),
                "deduped_total": len(raw_props),
            }

            for r in raw_props:
                try:
                    _badge = (r.get("badge") or "").strip().lower()
                    _is_demon  = _badge == "demon"
                    _is_goblin = _badge == "goblin"
                    props.append({
                        "player":                    r.get("player", ""),
                        "opponent":                  r.get("opponent", ""),
                        "stat_type":                 normalize_stat_type(r.get("stat", "")),
                        "line":                      float(re.sub(r"[^\d.]", "", r.get("line", "0")) or 0),
                        "tournament":                "Tennis",
                        "surface":                   "clay",
                        "tournament_stage":          1,
                        "round":                     "",
                        "is_grand_slam":             False,
                        "both_directions_available": _badge == "",  # APEX-PLAYWRIGHT-EXTRACT-2026-09-28
                        "is_goblin":                 _is_goblin,     # APEX-PLAYWRIGHT-EXTRACT-2026-09-28
                        "is_demon":                  _is_demon,      # APEX-PLAYWRIGHT-EXTRACT-2026-09-28
                        "match_date":                str(match_date),
                        "same_day_3set_count":       0,
                        "source":                    "playwright",
                    })
                except Exception as e:
                    log("Playwright", f"Parse error for {r}: {e}")

            # APEX-FULLTIER-2026-09-29: TASK 1/2 -- parse every real
            # api.prizepicks.com/projections response this page itself
            # received (captured above via page.on("response", ...),
            # never a direct HTTP call) into one prop record per unique
            # player+opponent+market+line+tier. The DOM scrape above
            # only ever captures ONE tile per (player, market) -- this
            # adds every alternate Demon/Goblin/Standard tier the
            # dedup above would otherwise never see, without touching
            # or removing anything the DOM pass already found.
            _api_added = 0
            _api_skipped_dup = 0
            _api_skipped_no_opp = 0
            try:
                for _body in _api_captures:
                    _data = _body.get("data", []) if isinstance(_body, dict) else []
                    _included = _body.get("included", []) if isinstance(_body, dict) else []
                    if not _data:
                        continue
                    _players_by_id = {}
                    _player_team_by_id = {}
                    _games_by_id = {}
                    _teams_by_id = {}
                    for _inc in _included:
                        _itype = _inc.get("type")
                        if _itype == "new_player":
                            _players_by_id[_inc["id"]] = _inc.get("attributes", {})
                            _player_team_by_id[_inc["id"]] = (
                                _inc.get("relationships", {})
                                    .get("team_data", {})
                                    .get("data", {})
                                    .get("id")
                            )
                        elif _itype == "game":
                            _games_by_id[_inc["id"]] = _inc
                        elif _itype == "team":
                            _teams_by_id[_inc["id"]] = _inc.get("attributes", {})

                    for _rec in _data:
                        try:
                            _a = _rec.get("attributes", {}) or {}
                            _rel = _rec.get("relationships", {}) or {}
                            _stat_name = _a.get("stat_display_name") or _a.get("stat_type") or ""
                            _line_val = _a.get("line_score")
                            if not _stat_name or _line_val is None:
                                continue
                            _pid = _rel.get("new_player", {}).get("data", {}).get("id")
                            _pattrs = _players_by_id.get(_pid, {})
                            _player_name = _pattrs.get("name") or _pattrs.get("display_name") or ""
                            if not _player_name:
                                continue

                            _opponent_name = ""
                            _gid = _rel.get("game", {}).get("data", {}).get("id")
                            _game = _games_by_id.get(_gid)
                            if _game:
                                _home_id = (_game.get("relationships", {})
                                            .get("home_team_data", {}).get("data", {}).get("id"))
                                _away_id = (_game.get("relationships", {})
                                            .get("away_team_data", {}).get("data", {}).get("id"))
                                _own_team_id = _player_team_by_id.get(_pid)
                                _opp_team_id = None
                                if _own_team_id == _home_id:
                                    _opp_team_id = _away_id
                                elif _own_team_id == _away_id:
                                    _opp_team_id = _home_id
                                if _opp_team_id:
                                    _opponent_name = (
                                        _teams_by_id.get(_opp_team_id, {}).get("abbreviation") or ""
                                    )
                            if not _opponent_name:
                                _api_skipped_no_opp += 1
                                continue

                            _api_stat_type = normalize_stat_type(_stat_name)
                            _api_line = float(_line_val)
                            _api_key = (
                                _player_name.strip().lower(),
                                _api_stat_type,
                                _opponent_name.strip().lower(),
                                _api_line,
                            )
                            if _api_key in _seen_keys:
                                _api_skipped_dup += 1
                                continue
                            _seen_keys.add(_api_key)

                            _odds_type = (_a.get("odds_type") or "standard").strip().lower()
                            _api_is_demon = _odds_type == "demon"
                            _api_is_goblin = _odds_type == "goblin"

                            props.append({
                                "player":                    _player_name,
                                "opponent":                  _opponent_name,
                                "stat_type":                 _api_stat_type,
                                "line":                      _api_line,
                                "tournament":                "Tennis",
                                "surface":                   "clay",
                                "tournament_stage":          1,
                                "round":                     "",
                                "is_grand_slam":             False,
                                "both_directions_available": _odds_type == "standard",
                                "is_goblin":                 _api_is_goblin,
                                "is_demon":                  _api_is_demon,
                                "match_date":                str(match_date),
                                "same_day_3set_count":       0,
                                "source":                    "playwright_api",
                            })
                            _api_added += 1
                        except Exception as _rec_e:
                            log("Playwright", f"Full-tier parse error for record: {_rec_e}")
            except Exception as _api_e:
                log("Playwright", f"Full-tier API parsing skipped (non-fatal): {_api_e}")

            log("Playwright",
                f"Full-tier API: {len(_api_captures)} response(s) captured, "
                f"+{_api_added} additional tier(s) added "
                f"({_api_skipped_dup} already covered by DOM scrape, "
                f"{_api_skipped_no_opp} skipped — no opponent resolved)")

            global LAST_FULLTIER_API_STATS
            LAST_FULLTIER_API_STATS = {
                "responses_captured": len(_api_captures),
                "added":              _api_added,
                "skipped_dup":        _api_skipped_dup,
                "skipped_no_opp":     _api_skipped_no_opp,
            }

            # APEX-FETCHPIPE-2026-09-30: this context is now Playwright's
            # own browser (launch_persistent_context), not someone's real
            # Chrome window someone else is using -- close it fully at the
            # end of every run instead of leaving a Chrome process behind.
            try:
                context.close()
            except Exception as _e:
                log("Playwright", f"Context close failed (non-fatal): {_e}")
            log("Playwright", f"Browser success: {len(props)} props")
            return props

    except Exception as e:
        # APEX-FETCHPIPE-2026-09-30: catch-all for anything not already
        # classified above (chrome_launch / navigation / extraction). Shows
        # the real exception instead of a generic guess. A CDP protocol
        # error surfacing here (rather than at launch) still gets labeled
        # as a handshake rejection since that's what the text means.
        _msg = str(e)
        if "setDownloadBehavior" in _msg or "context management is not supported" in _msg or "Protocol error" in _msg:
            _record_fetch_failure("handshake_rejection", "Chrome connected but rejected Playwright session", e)
        else:
            _record_fetch_failure("unexpected", "Unexpected Playwright error", e)
        log("Playwright", f"Automation failed: {e}")
        return []

# APEX-PARTNERAPI-2026-10-01 (API-First Fetch -- IMPLEMENTATION APPROVED):
# Primary data-acquisition path. Hits partner-api.prizepicks.com (see the
# PARTNER_API constant above for why: api.prizepicks.com is DataDome-walled,
# this mirror isn't). Live-tested repeatedly during the 2026-10-01 Scraper
# Architecture Audit and Fetch Performance Overhaul: HTTP 200 in 0.3-0.6s,
# full tennis board, all three tiers via the `odds_type` attribute.
#
# FIELD MAPPING (confirmed live against real responses, not guessed):
#   player    -> relationships.new_player.data.id, resolved against
#                included[type=="new_player"].attributes.display_name
#                (falls back to .name if display_name is absent)
#   opponent  -> attributes.description, run through the SAME
#                _clean_scraped_opponent() Playwright already uses.
#                Confirmed live (singles AND doubles-team entries):
#                description is the bare opponent (or opponent team)
#                name, e.g. player="Daniil Medvedev" had
#                description="Pablo Carreno Busta". The only
#                contamination found was the "{Opponent} 1st Set"
#                suffix on the two 1st-Set markets (55/398 live props,
#                100% of all raw mismatches found during validation) --
#                exactly what _clean_scraped_opponent() already strips.
#   stat_type -> attributes.stat_type, through the SAME
#                normalize_stat_type() Playwright already uses.
#   line      -> attributes.line_score
#   tier      -> attributes.odds_type ("standard"/"goblin"/"demon"),
#                converted to the is_goblin/is_demon boolean pair the
#                rest of Apex already expects (matches the shape
#                fetch_props_playwright()'s full-tier API parsing uses).
#   Game ID / Projection ID / Start Time -- NOT carried by the existing
#                Playwright path at all. Added here as new, additive
#                fields only (game_id, projection_id, start_time_raw) --
#                nothing existing is removed or renamed, so every
#                downstream consumer of the old field names keeps working
#                unchanged.
#
# FAILURE HANDLING: every failure mode in the approved design (403, 429,
# timeout, invalid/missing schema, empty response) returns [] and logs a
# distinct reason -- never raises -- so fetch_props_with_fallback()'s
# existing "if props: ... else: try next step" chain continues to work
# exactly as it already does for the old API-first step.
def fetch_props_partner_api(match_date, max_age=120, caller="core"):
    """
    PRIMARY (API-First Fetch, 2026-10-01): partner-api.prizepicks.com.
    Returns props for match_date in the same shape fetch_props_playwright()
    produces, plus additive game_id/projection_id/start_time_raw fields.
    Returns [] (never raises) on any failure so the caller falls back to
    Playwright automatically.
    """
    try:
        # APEX-PPTHROTTLE-2026-10-16: the request goes through the shared
        # PrizePicks gate (apex_ops.pp_partner_board): one pull reused for
        # 2 min across jobs, one request at a time, 15-min pause on 429/403.
        import apex_ops as _ppg
        try:
            body = _ppg.pp_partner_board(max_age=max_age, caller=caller)
        except _ppg.PPCooldown as _cd:
            log("Fetch", f"Partner API paused: {_cd} -- no request made")
            return []
        _pp_src = _ppg.PP_LAST.get("source")
        _pp_note = "" if _pp_src == "network" else f", shared pull {_ppg.PP_LAST.get('age')}s old" + (" (paused)" if _pp_src == "stale" else "")

        raw = body.get("data") or []
        if not raw:
            log("Fetch", "Partner API returned empty board -- falling back")
            return []

        included = body.get("included") or []
        players_by_id = {}
        for inc in included:
            if inc.get("type") == "new_player":
                _pa = inc.get("attributes", {}) or {}
                players_by_id[inc.get("id")] = (
                    _pa.get("display_name") or _pa.get("name") or ""
                )

        props = []
        skipped_no_player = 0
        skipped_no_opponent = 0

        # APEX-PARTNERAPI-2026-10-01: NOT filtered to a single calendar
        # date. Live-tested 2026-10-01: the board carries BOTH today's and
        # tomorrow's matches at once (314 + 83 of 397), and
        # fetch_props_playwright() captures that same whole live board
        # without a date filter either -- it just stamps every prop with
        # the match_date argument regardless of the prop's real date (see
        # "match_date": str(match_date) below, matching that existing
        # behavior exactly). Filtering here to only match_date would make
        # this function return a SMALLER board than Playwright's
        # equivalent call for no accuracy benefit, and would contradict
        # the Fetch Performance Overhaul audit's own counts (Playwright
        # 406 vs partner-api 398 -- the whole board both times, not a
        # date-filtered subset of either).
        for proj in raw:
            try:
                attrs = proj.get("attributes", {}) or {}
                rel   = proj.get("relationships", {}) or {}
                start_time = attrs.get("start_time") or ""

                pid = (rel.get("new_player", {}) or {}).get("data", {}).get("id")
                player_name = players_by_id.get(pid, "")
                if not player_name:
                    skipped_no_player += 1
                    continue

                opponent_name = _clean_scraped_opponent(attrs.get("description", ""))
                if not opponent_name:
                    skipped_no_opponent += 1
                    continue

                stat_name = attrs.get("stat_type") or attrs.get("stat_display_name") or ""
                line_val  = attrs.get("line_score")
                if not stat_name or line_val is None:
                    continue

                odds_type = (attrs.get("odds_type") or "standard").strip().lower()
                is_demon  = odds_type == "demon"
                is_goblin = odds_type == "goblin"

                props.append({
                    "id":                        proj.get("id"),
                    "player":                    player_name,
                    "opponent":                  opponent_name,
                    "stat_type":                 normalize_stat_type(stat_name),
                    "line":                      float(line_val),
                    "tournament":                "Tennis",
                    "surface":                   "clay",
                    "tournament_stage":          1,
                    "round":                     "",
                    "is_grand_slam":             False,
                    "both_directions_available": odds_type == "standard",
                    "is_goblin":                 is_goblin,
                    "is_demon":                  is_demon,
                    "match_date":                str(match_date),
                    "same_day_3set_count":       0,
                    "source":                    "partner_api",
                    # APEX-PARTNERAPI-2026-10-01: additive-only fields, not
                    # produced by the Playwright path -- nothing existing
                    # changes shape or name for callers that don't use these.
                    "game_id":                   attrs.get("game_id"),
                    "projection_id":             proj.get("id"),
                    "start_time_raw":            start_time,
                })
            except Exception as _rec_e:
                log("Fetch", f"Partner API record parse error (non-fatal, skipped): {_rec_e}")

        log("Fetch",
            f"Partner API success: {len(props)} props (full live board, "
            f"raw {len(raw)}, skipped: {skipped_no_player} no-player, "
            f"{skipped_no_opponent} no-opponent{_pp_note})")
        _apex_write_live_board(props, len(raw))   # APEX-STALELINE-2026-10-14
        return props

    except requests.exceptions.Timeout:
        log("Fetch", "Partner API timed out -- falling back")
        return []
    except Exception as e:
        log("Fetch", f"Partner API failed ({e}) -- falling back")
        return []

# APEX-FETCHRELIABILITY-2026-10-01: retry/backoff wrapper used by both
# fetch stages below. A fetch should "almost never fail outright" given
# the existing API + Playwright chain -- the gap was that neither step
# was ever retried; a single blip (timeout, transient 403/429, a
# Playwright/Chrome hiccup) sent the whole run straight to the next
# stage (or to a hard failure) on the first try. This gives each stage
# real retries with exponential backoff before giving up on it, and
# calls `on_retry(attempt, max_attempts)` between attempts so the caller
# can post a friendly, non-technical status update instead of nothing.
def _fetch_with_retries(fetch_fn, match_date, max_attempts, base_delay, label, on_retry=None):
    props = []
    for attempt in range(1, max_attempts + 1):
        try:
            props = fetch_fn(match_date)
        except Exception as e:
            # fetch_props_partner_api / fetch_props_playwright already
            # catch everything internally and return [] -- this except
            # is a second, outer safety net only, so a future change to
            # either function can never turn a retry loop into an
            # unhandled crash.
            log("Fetch", f"{label} attempt {attempt}/{max_attempts} raised "
                          f"{type(e).__name__}: {e} -- treating as empty result")
            props = []
        if props:
            if attempt > 1:
                log("Fetch", f"{label} succeeded on attempt {attempt}/{max_attempts}")
            return props
        # APEX-PPTHROTTLE-2026-10-16: a 429/403 paused PrizePicks reads --
        # retrying now would only push harder, so stop here.
        try:
            import apex_ops as _ppg
            if _ppg.pp_cooldown_until():
                log("Fetch", f"{label}: PrizePicks asked us to slow down -- not retrying")
                return []
        except Exception:
            pass
        if attempt < max_attempts:
            delay = base_delay * (2 ** (attempt - 1))  # exponential backoff
            log("Fetch", f"{label} attempt {attempt}/{max_attempts} returned nothing -- "
                         f"retrying in {delay:.1f}s")
            if on_retry:
                try:
                    on_retry(attempt, max_attempts)
                except Exception:
                    pass
            time.sleep(delay)
    return []


def _ppfetch_chain(stage, text, nxt=None):
    """APEX-ALERTCHAIN-2026-10-25: the props-fetch retry alerts are one #alerts card (retrying ->
    browser -> found / failed). Without the chain each line posts as before."""
    try:
        import apex_alert_chain as _AC
        import apex_alert_cards as _C
        if _AC.CHAINS_ON:
            open_ = _AC.get("ppfetch", max_age_min=30)
            e = _C.from_text(text)
            if stage == "step":
                if open_:
                    _AC.step("ppfetch", text.replace("**", "").split("\n")[0], nxt, wait_min=3)
                else:
                    _AC.start("ppfetch", e, nxt or "Retrying — this card shows how it ends.", wait_min=3)
                return True
            if not open_:   # no retry card open: a clean first try posts nothing extra
                if stage == "ok":
                    return True
                return bool(send_discord("alerts", text))
            if stage == "ok":
                e["color"] = _C.GREEN
            return _AC.finish("ppfetch", e)
    except Exception as _e:
        log("AlertChain", f"props fetch card failed ({type(_e).__name__}), posting the line")
    if stage == "ok" and "PrizePicks API answered" in text:
        return True
    return send_discord("alerts", text)


def fetch_props_with_fallback(match_date, dates):
    """
    Master prop fetcher.
    Step 1: Try partner-api.prizepicks.com, 3 attempts w/ exponential
            backoff (primary -- API-First Fetch, 2026-10-01)
    Step 2: Try Playwright browser automation, 2 attempts w/ exponential
            backoff (fallback -- Option A)
    Step 3: Alert Discord — manual screenshot via #general (Option C)

    Returns (props, source) where source is one of:
      "partner_api" | "playwright" | "failed"

    APEX-FETCHRELIABILITY-2026-10-01: a user-triggered /fetch surfaced a
    raw "BrokenPipeError [Errno 32]" to Discord. Root cause (see log()'s
    own comment for the full writeup): that error came from the logging
    layer, not from either fetch stage below -- fetch_props_partner_api()
    and fetch_props_playwright() already caught every one of their own
    failure modes and returned [] without raising, so this chain was
    never actually the thing that crashed. Two real gaps DID exist
    though, and are fixed here: (1) neither stage was ever retried, so
    one transient blip skipped straight past a step that would often
    have worked a second later, and (2) every message shown to the user
    on failure was either silent or a raw technical string -- replaced
    below with plain-English status updates at each transition.
    ONLY FAIL IF: Partner API failed AND Playwright failed AND every
    retry on both is exhausted -- steps 1 and 2 below are that, exactly.
    """
    log("Fetch", f"Fetching props for {match_date}")

    # Step 1 — Partner API: 3 attempts, exponential backoff
    def _partner_retry_notice(attempt, max_attempts):
        _ppfetch_chain("step",
            f"⚠️ **Partner API unavailable** (attempt {attempt}/{max_attempts}) — "
            f"{dates['ct_time']}\nRetrying...",
            f"Retrying the PrizePicks API (attempt {attempt + 1} of {max_attempts}); if all {max_attempts} fail, Apex "
            f"tries the browser next. This card shows how it ends.")
    props = _fetch_with_retries(
        fetch_props_partner_api, match_date,
        max_attempts=3, base_delay=2.0, label="Partner API",
        on_retry=_partner_retry_notice,
    )
    if props:
        send_discord("system",
            f"✅ Props fetched via Partner API: {len(props)} tennis props for {match_date}"
        )
        _ppfetch_chain("ok", f"✅ **PrizePicks API answered** — {len(props)} props found after a retry")
        return props, "partner_api"

    # APEX-PPTHROTTLE-2026-10-16: never open the PrizePicks website in an
    # automated browser right after PrizePicks told us to slow down. The
    # browser fallback stays for real outages only.
    try:
        import apex_ops as _ppg
        _pp_until = _ppg.pp_cooldown_until()
    except Exception:
        _pp_until = 0
    if _pp_until:
        _pp_when = datetime.fromtimestamp(_pp_until, CT).strftime("%I:%M %p").lstrip("0")
        _record_fetch_failure("partner_api_paused",
            f"PrizePicks asked us to slow down -- PrizePicks reads are paused until {_pp_when} CT "
            f"and there is no recent pull to reuse. Try /fetch again after {_pp_when} CT.")
        log("Fetch", f"Partner API paused until {_pp_when} CT -- skipping the browser fallback")
        return [], "paused"

    # Step 2 — Playwright: 2 attempts, exponential backoff
    log("Fetch", "Partner API exhausted all retries — switching to Playwright fallback")
    _ppfetch_chain("step",
        f"⚠️ **Switching to Playwright fallback** — {dates['ct_time']}\n"
        f"Partner API unavailable after 3 attempts.",
        "Reading the PrizePicks website in the browser (2 tries, about 2 min). This card shows how it ends.")

    def _playwright_retry_notice(attempt, max_attempts):
        _ppfetch_chain("step",
            f"⚠️ **Playwright attempt {attempt}/{max_attempts} failed** — retrying...",
            f"Browser try {attempt + 1} of {max_attempts} next; if it fails too, you'll be asked for a screenshot here.")
    props = _fetch_with_retries(
        fetch_props_playwright, match_date,
        max_attempts=2, base_delay=3.0, label="Playwright",
        on_retry=_playwright_retry_notice,
    )
    if props:
        _ppfetch_chain("ok",
            f"✅ **Browser fallback success** — {len(props)} props found\n"
            f"Source: Playwright automation"
        )
        return props, "playwright"

    # Step 3 — Alert for manual entry (Partner API AND Playwright both
    # failed, all retries on both exhausted)
    log("Fetch", "Both API and browser failed after all retries — requesting manual entry")
    _ppfetch_chain("fail",
        f"🚨 **PROPS FETCH FAILED** — {dates['ct_time']}\n"
        f"Partner API (3 attempts) and Playwright (2 attempts) both failed\n\n"
        f"**Action required:**\n"
        f"Open PrizePicks → Tennis section → take screenshot\n"
        f"Send screenshot to #general\n"
        f"OpenClaw will evaluate manually from your screenshot\n\n"
        f"Or type props directly in #general:\n"
        f"`PlayerName LESS/MORE LINE STAT_TYPE`\n"
        f"Example: `Rublev LESS 22.5 total_games`"
    )
    return [], "failed"

# ================================================================
# WEB SEARCH — Claude native tool stub
# ================================================================
# APEX-SERPERDISABLE-2026-10-05 (owner, explicit instruction):
# Serper is permanently out of credits on the vendor's side and is not
# being renewed, so this is now a clean, permanent no-op rather than a
# live call that always fails. No HTTP request is made, no "Serper
# error 400" log spam, and no Discord warning (the warning this stub
# used to post — APEX-SERPERALERT-2026-10-05 — is removed along with
# the calls it was warning about; there is nothing left to warn about).
# Every caller (learning_agent, moneyline_settlement_agent, the pending
# retry engine) already treats an empty string as "couldn't resolve"
# and falls through correctly, so nothing downstream needed to change.
# To bring Serper back later: restore from
# openclaw_core.py.bak_<timestamp>_serperdisable and supply a funded
# SERPER_API_KEY.
# ================================================================

def web_search(query):
    return ""


def fetch_tennisratio_hold(player):
    """
    Fetch serve hold % from tennisratio.com — static HTML, no JS required.
    Returns float (e.g. 0.792) or None.
    URL pattern: https://www.tennisratio.com/players/FirstLast.html
    Page contains: "he holds at XX.X% in service games won ratio"
    """
    import re as _re
    slug = player.replace(" ", "")
    urls_to_try = [
        f"https://www.tennisratio.com/players/{slug}.html",
        # WTA players sometimes use hyphen format: First-Last.html
        f"https://www.tennisratio.com/players/{player.replace(' ', '-')}.html",
    ]
    for url in urls_to_try:
      try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            log("Research", f"TennisRatio fetch {resp.status_code} for {player} at {url}")
            continue
        text = resp.text
        # Pattern 1: "holds at XX.X% in service games won ratio"
        m = _re.search(r"holds?\s+at\s+(\d{2,3}\.?\d*)\s*%\s+in\s+service\s+games?\s+won", text, _re.IGNORECASE)
        if m:
            val = float(m.group(1)) / 100.0
            if 0.50 <= val <= 0.99:
                log("Research", f"TennisRatio hold (p1): {player} {val:.3f}")
                return val
        # Pattern 2: "service games won ratio: XX.X%"
        m = _re.search(r"service\s+games?\s+won\s+ratio[:\s]+(\d{2,3}\.?\d*)\s*%", text, _re.IGNORECASE)
        if m:
            val = float(m.group(1)) / 100.0
            if 0.50 <= val <= 0.99:
                log("Research", f"TennisRatio hold (p2): {player} {val:.3f}")
                return val
        # Pattern 3: any XX.X% near "service games won"
        m = _re.search(r"service\s+games?\s+won[^%]{0,50}(\d{2,3}\.?\d*)\s*%", text, _re.IGNORECASE)
        if m:
            val = float(m.group(1)) / 100.0
            if 0.50 <= val <= 0.99:
                log("Research", f"TennisRatio hold (p3): {player} {val:.3f}")
                return val
        log("Research", f"TennisRatio: no hold pattern for {player} at {url}")
        # continue to next URL slug
      except Exception as e:
        log("Research", f"TennisRatio fetch error {player} at {url}: {e}")
    # All URLs exhausted
    return None


# ================================================================
# SURFACE LOOKUP — never silently defaults to clay
# ================================================================

_SURFACE_HINTS = {
    "wimbledon": "grass", "halle": "grass", "queen": "grass",
    "nottingham": "grass", "stuttgart": "grass", "hertogenbosch": "grass",
    "eastbourne": "grass", "mallorca": "grass", "bad homburg": "grass",
    "birmingham": "grass", "ilkley": "grass", "berlin": "grass",
    "roland garros": "clay", "french open": "clay", "madrid": "clay",
    "rome": "clay", "monte carlo": "clay", "hamburg": "clay",
    "barcelona": "clay", "geneva": "clay", "kitzbuhel": "clay",
    "australian open": "hard", "us open": "hard", "indian wells": "hard",
    "miami": "hard", "cincinnati": "hard", "shanghai": "hard",
    "dubai": "hard", "acapulco": "hard", "tokyo": "hard",
}
_surface_cache = {}

def research_surface(tournament, dates=None):
    """
    Determine court surface from tournament name.
    Never silently defaults to clay — logs a warning if unconfirmed.
    Order: cache -> hint table -> web_search -> 'hard' last resort.
    """
    if not tournament:
        return "hard"
    key = tournament.lower().strip()
    if key in _surface_cache:
        return _surface_cache[key]
    for name, surf in _SURFACE_HINTS.items():
        if name in key:
            _surface_cache[key] = surf
            return surf
    # web search fallback
    result = web_search(f"{tournament} tennis tournament court surface grass clay hard 2026")
    surf = None
    if result:
        rl = result.lower()
        if "grass" in rl:   surf = "grass"
        elif "clay" in rl:  surf = "clay"
        elif "hard" in rl:  surf = "hard"
    if surf:
        _surface_cache[key] = surf
        log("Surface", f"{tournament} -> {surf} (researched)")
        return surf
    log("Surface", f"UNCONFIRMED surface for {tournament} — defaulting hard")
    _surface_cache[key] = "hard"
    return "hard"


# ================================================================
# PROP TYPE CLASSIFIER
# ================================================================

def classify_prop_type(prop) -> str:
    """
    Returns one of: 'standard' | 'goblin' | 'red_demon' | 'demon'

    STANDARD   — Both MORE and LESS available
    GOBLIN     — MORE only, standard Goblin payout (😈 green icon)
    RED_DEMON  — MORE only, higher payout (🔴😈 red demon icon)
    DEMON      — MORE only, PrizePicks' plain demon icon (🔴 red icon
                 without 😈). APEX-DEMON-EVAL-2026-09-04 / APEX-DEMONPARITY-
                 2026-09-29: evaluated everywhere in this codebase exactly
                 like the other prop types — never skipped based on type.

    Detection priority:
    1. Explicit prop_type field in prop dict (from gateway screenshot read)
    2. is_red_demon flag
    3. is_demon flag
    4. is_goblin flag
    5. both_directions_available field
    6. Default = standard
    """
    # Explicit override from input string or gateway
    explicit = prop.get("prop_type", "").lower().strip()
    if explicit in ("red_demon", "red demon", "reddemon"):
        return "red_demon"
    if explicit in ("goblin",):
        return "goblin"
    if explicit in ("demon",):
        return "demon"
    if explicit in ("standard",):
        return "standard"

    # Flag-based detection
    if prop.get("is_red_demon", False):
        return "red_demon"
    if prop.get("is_demon", False):
        return "demon"
    if prop.get("is_goblin", False):
        return "goblin"
    if prop.get("both_directions_available", True):
        return "standard"

    # One direction only without explicit type = goblin
    return "goblin"

def filter_props(props, dates):
    valid   = []
    skipped = 0
    for prop in props:
        ptype = classify_prop_type(prop)
        prop["prop_type_classified"] = ptype

        # APEX-WIRING-2026-08-27: fantasy score is no longer skipped here.
        # The engine prices it (simulator category fantasy_score); if the engine
        # cannot, it declines like any other prop.

        # APEX-DEMONLOCK-2026-09-29: demon, goblin, and red_demon MUST share
        # one if/elif/else chain so the lock set for one type can never be
        # wiped out by a fallthrough "else" written for a different type.
        # Root cause of the prior bug: "demon" was locked in its own separate
        # `if` block, then unconditionally fell into a second, unrelated
        # if/elif/else (goblin / red_demon / else-standard) whose else branch
        # reset direction_locked back to None -- erasing the demon lock two
        # lines after it was set. Live audit (2026-09-29): 427 of 437 Demon
        # picks on the board were LESS recommendations, which PrizePicks does
        # not even offer. Goblin and red_demon were never affected, because
        # they were already handled directly inside this same chain.

        # DEMON 🔴 — MORE only, engine prices it honestly, _apex_forward_log_row
        # stamps playable=0 on a LESS verdict.
        if ptype == "demon":
            prop["direction_locked"] = "MORE"
            prop["both_directions_available"] = False
            log("Filter", f"Demon 🔴 flagged: {prop.get('player')} "
                f"{prop.get('line')} — MORE only, engine evaluates")

        # GOBLIN 😈 — MORE only, flag it
        elif ptype == "goblin":
            prop["direction_locked"] = "MORE"
            prop["both_directions_available"] = False
            log("Filter", f"Goblin 😈 flagged: {prop.get('player')} "
                f"{prop.get('line')} — MORE only, GREEN required")

        # RED DEMON 🔴😈 — MORE only, higher payout, flag it
        elif ptype == "red_demon":
            prop["direction_locked"] = "MORE"
            prop["both_directions_available"] = False
            log("Filter", f"Red Demon 🔴😈 flagged: {prop.get('player')} "
                f"{prop.get('line')} — MORE only, GREEN required, "
                f"stronger justification needed")

        # STANDARD — both directions available
        else:
            prop["direction_locked"] = None
            prop["both_directions_available"] = True

        valid.append(prop)

    log("Filter", f"{len(props)} total → {len(valid)} valid → {skipped} skipped")
    return valid

# ================================================================
# GATE 11
# ================================================================

SCORE_PATTERN = re.compile(r'\b[0-7]-[0-7]\b[\s,]+\b[0-7]-[0-7]\b')

def gate_11_check(prop, dates):
    """
    v7.9.7 rewrite (item 13): Gate 11 previously ran entirely on web
    search before V8 data existed anywhere in the pipeline, meaning it
    could pass a match based on whatever search happened to return —
    and had no reliable way to catch a match that was actually already
    completed, retired, or not real.

    New approach: check existence directly against V8 (structured data,
    not a search guess), retrying both ATP and WTA if the stated tour
    doesn't resolve — this directly guards against the 1F-style bug
    (correct player name, wrong tour endpoint queried). Then check V8's
    own recent-match record for a completed result against this exact
    opponent on/near the stated match date — if found, the match already
    happened and Gate 11 rejects.

    No web search fallback. If V8 doesn't have the player at all, that's
    an honest "cannot verify" — reject and let owner check
    manually, rather than trusting a search result for a factual
    existence/scheduling question a structured API should answer.
    """
    player     = prop.get("player", "")
    opponent   = prop.get("opponent", "")
    match_date = prop.get("match_date") or dates.get("match_date")

    # APEX-GATE11-SA365-2026-09-23: check SportsAPI365's match lists first. The old
    # stats service is gone and its local name list only knows established players,
    # so Challenger/ITF pairs were rejected as "not found". If SportsAPI365 lists this
    # pair, the match is real: verified, and its tour is used. A listed match that has
    # already started is NOT rejected here (ITF matches often start late; pricing
    # decides). Not listed -> the old check below, unchanged.
    try:
        import apex_price_feed as _apf_g11
        _fc11 = _apf_g11.fixture_context(player, opponent)
    except Exception as _e:
        _fc11 = None
        log("Gate11", f"SportsAPI365 match-list check unavailable (non-fatal): {_e}")
    if _fc11:
        _t11 = str(_fc11.get("tour") or "").lower()
        _st11 = (prop.get("tour") or "atp").lower().strip()
        if _t11 in ("atp", "wta") and _t11 != _st11:
            prop["tour"] = _t11
            log("Gate11", f"Tour corrected: {_st11} -> {_t11} (SportsAPI365)")
            send_discord("alerts",
                f"⚠️ **Gate 11 TOUR CORRECTED** — {dates['ct_time']}\n"
                f"Match: {player} vs {opponent}\n"
                f"Input said: **{_st11.upper()}** | SportsAPI365 lists: **{_t11.upper()}**\n"
                f"Evaluation proceeding with corrected tour."
            )
        log("Gate11", f"VERIFIED — {player} vs {opponent} — SportsAPI365 lists it: "
            f"{_fc11.get('tournament')} | {_t11 or '?'} | starts {_fc11.get('start_time')} "
            f"| started={_fc11.get('started')} ({_fc11.get('match_method')})")
        return {"status": "PASSED", "verified": True}

    if not _V8_AVAILABLE:
        # Fix (2026-07-23, approved): every Gate 11 REJECTED/UNVERIFIED
        # outcome previously only reached send_discord() -- the log file
        # (what every terminal diagnostic and grep this project uses
        # actually reads) never recorded WHY a match was gated, only that
        # a tour got corrected or a recent-match lookup failed. Confirmed
        # live: a real debugging session grepped the log for "Gate 11"
        # and "REJECTED" and found nothing, despite real rejections
        # sitting in #alerts the whole time. Logging alongside every
        # send_discord() call here closes that blind spot for good.
        log("Gate11", f"UNVERIFIED — {player} vs {opponent} — V8 API unavailable")
        send_discord("alerts",
            f"⚠️ **Gate 11 UNVERIFIED** — {dates['ct_time']}\n"
            f"Match: {player} vs {opponent} — V8 API unavailable, cannot verify existence"
        )
        return {"status": "PASSED", "verified": False,
                "flag": "Gate 11 unverified — V8 unavailable"}

    stated_tour  = (prop.get("tour") or "atp").lower().strip()
    tours_to_try = [stated_tour] + [t for t in ("atp", "wta") if t != stated_tour]

    canonical_player = canonical_opponent = None
    resolved_tour    = None
    # 2026-08-04 real fix: track each name's own best resolution
    # independently across every tour tried, regardless of whether the
    # OTHER name also resolved on that exact same attempt. The original
    # loop only ever set canonical_player/canonical_opponent when BOTH
    # resolved together on the identical tour_attempt -- meaning if
    # opponent never resolved on any tour, canonical_player stayed None
    # too, even when player genuinely DID resolve fine on its own. Real
    # dry-run test caught this: Aliona Falei correctly resolves, but
    # with the old logic the rejection message would have called her
    # unresolved as well, purely because her opponent never paired with
    # her on a shared tour attempt -- exactly the kind of misleading
    # diagnostic the fix above was meant to eliminate.
    player_resolved_any = opponent_resolved_any = None
    for tour_attempt in tours_to_try:
        cp = v8_resolve_player_name(player, tour_attempt)
        co = v8_resolve_player_name(opponent, tour_attempt)
        if cp and player_resolved_any is None:
            player_resolved_any = cp
        if co and opponent_resolved_any is None:
            opponent_resolved_any = co
        if cp and co:
            canonical_player, canonical_opponent, resolved_tour = cp, co, tour_attempt
            break

    if not (canonical_player and canonical_opponent):
        # 2026-08-04 real fix: this used to just say "not found in V8"
        # for the whole match, forcing a manual web-search investigation
        # every time to find out WHICH of the two names actually failed
        # -- confirmed live on a real rejection (Aliona Falei vs Carol
        # Young Suh Lee) where Falei herself was a real, established,
        # correctly-resolvable player and only her opponent's unusual
        # multi-part name was the actual problem. Now says so directly,
        # using the honest per-name tracking above rather than the
        # paired canonical_player/canonical_opponent variables (which
        # can both be None even when one name resolved fine, if the
        # other name never resolved on the same tour attempt).
        missing = []
        if not player_resolved_any:
            missing.append(player)
        if not opponent_resolved_any:
            missing.append(opponent)
        cross_tour_note = ""
        if not missing:
            # Real, distinct edge case: both names resolved individually,
            # just never on the same tour attempt together -- genuinely
            # suspicious for a real match (a WTA player vs an ATP player
            # isn't a real pairing), worth flagging differently from a
            # true "name not found" case rather than folding it into the
            # same generic message.
            missing = [f"{player} vs {opponent}"]
            cross_tour_note = " Both names resolved individually, but never on the same tour -- check the stated tour/gender is correct."
        one_side_only = len(missing) == 1 and missing[0] in (player, opponent)
        log("Gate11", f"REJECTED — {player} vs {opponent} — not found in V8 "
            f"for any tour tried ({', '.join(tours_to_try)}) — "
            f"specifically: {', '.join(missing)}")
        send_discord("alerts",
            f"⛔ **Gate 11 REJECTED** — {dates['ct_time']}\n"
            f"Match: {player} vs {opponent}\n"
            f"Could not resolve: **{', '.join(missing)}** "
            f"(tried tours: {', '.join(tours_to_try)}).{cross_tour_note} "
            f"{'The other player resolved fine — ' if one_side_only else ''}"
            f"Evaluate manually or wait for V8 coverage before betting."
        )
        return {"status": "REJECTED"}

    if resolved_tour != stated_tour:
        prop["tour"] = resolved_tour
        log("Gate11", f"Tour corrected: {stated_tour} -> {resolved_tour}")
        send_discord("alerts",
            f"⚠️ **Gate 11 TOUR CORRECTED** — {dates['ct_time']}\n"
            f"Match: {player} vs {opponent}\n"
            f"Input said: **{stated_tour.upper()}** | V8 confirmed: **{resolved_tour.upper()}**\n"
            f"Evaluation proceeding with corrected tour."
        )

    # ── Completed-match check — structured data, not a search guess ──
    try:
        recent = v8_get_recent_matches(canonical_player, limit=5)
    except Exception as _e:
        recent = []
        log("Gate11", f"get_recent_matches lookup failed (non-fatal): {_e}")

    opponent_last = canonical_opponent.split()[-1].lower()
    for m in recent:
        m_opp = (m.get("opponent") or "").lower()
        if opponent_last in m_opp and m.get("date") == match_date:
            log("Gate11", f"REJECTED — {player} vs {opponent} — V8 shows this "
                f"match already completed on {m.get('date')} (score: {m.get('score')})")
            send_discord("alerts",
                f"⛔ **Gate 11 REJECTED** — {dates['ct_time']}\n"
                f"Match: {player} vs {opponent}\n"
                f"V8 shows this match already completed on {m.get('date')} "
                f"(score: {m.get('score')})."
            )
            return {"status": "REJECTED"}

    return {"status": "PASSED", "verified": True}


# ================================================================
# RESEARCH AGENT
# ================================================================

RESEARCH_EXTRACTION_PROMPT = """Extract tennis player data from search result snippets.
Return ONLY valid JSON. No markdown. No explanation. No text before or after the JSON.

REJECT ATP OVERVIEW TEMPLATE DATA:
The ATP/WTA overview page shows identical fake stats for every player.
If you see ANY of these fingerprints, treat ALL serve stats from that source as null:
- "Aces: 1584" (template career ace total on overview pages)
- "Service Games Won: 85%" with no season or surface qualifier shown nearby
- Any serve stat with no "2026", "last 52 weeks", "grass/clay/hard" qualifier nearby
Only accept serve_hold_pct from tennisabstract.com with a qualifier (e.g. "76.3%", "last 52 weeks"),
or from tennisratio.com (e.g. "holds at 79.2% in service games won ratio"),
or ATP/WTA stats with explicit season AND surface filter, or derived from match box scores.
Template data = null. No exceptions.

CRITICAL — serve_hold_pct STAT IDENTITY (the most common extraction error):
serve_hold_pct MUST come ONLY from a true hold statistic labeled:
"Service Games Won %", "Service Games Won", "Hold %", or "% of service games held"
These look-alike stats are NOT hold% — if that is all you see, serve_hold_pct = null:
  - "1st Serve %" / "First Serve In %" = how often serve lands in — NOT hold%
  - "1st Serve Points Won %" = a POINT stat — NOT a game stat
  - "Service Points Won %" = points won — NOT games won
  - "Break Points Saved %" = related but NOT hold% — do not substitute
Never put a look-alike into serve_hold_pct because the number "looks about right."

HOW TO OBTAIN serve_hold_pct (in priority order):
1. PUBLISHED AVERAGE: Tennis Abstract or ATP/WTA stats WITH season/surface qualifier shown.
   Set serve_hold_method = "published", serve_hold_matches = sample size if stated.
  Set serve_hold_surface = "grass"/"clay"/"hard" if the source explicitly filters by surface,
  or "all" if it is an overall/cross-surface average.
2. BOX SCORE DERIVATION: hold% = service_games_won / service_games_played across 3-4 matches.
   Recaps state "broken X times" -> games won = served minus broken.
   Example: serves 10, broken twice -> held 8/10 = 0.80. Sum 3-4 matches, show math.
   Set serve_hold_method = "derived", serve_hold_matches = number of matches used.
3. If neither: serve_hold_pct = null. One match is NOT a confirmed average.

Extraction rules:
- ranking: look for "rank", "No.", "#", "world No", "ATP No", "current rank" followed by a number. Extract the integer.
- surface_record: look for win-loss records like "5-2", "13-6", "0-1" near surface names (grass/clay/hard). Format as "W-L".
  CRITICAL: Prefer ATP/WTA tour-level records over combined ATP+Challenger records. If you see both "ATP level: 6-6" and "all levels: 10-5", use the ATP-level figure. Never use Challenger-only or ITF records as the surface_record — those inflate the number and misrepresent tour-level performance. If only a combined figure is available, note it but flag it as mixed-level.
- recent_form_last3: look for recent match results. W=win, L=loss. Return array like ["W","W","L"].
- recent_match_scores: look for ACTUAL match scorelines (e.g. "6-3 6-4", "7-5 6-4", "6-4 3-6 6-3").
  Return list of up to 5 entries in format "won/lost X-X X-X vs Opponent at Tournament Year".
  EXTRACT ONLY if real game scores with digit-dash-digit patterns are visible in the text.
  A 3-set score (e.g. "6-4 3-6 6-2") or tiebreak set (e.g. "7-6") is a CONFIRMED EXTENDER.
  Do NOT infer or estimate. If no actual scores appear in the snippets, return empty list.
- primary_surface: infer from "clay specialist", "grass court player", "hard court" mentions. Return "clay", "hard", or "grass".
- serve_hold_pct: ONLY from tennisabstract.com with qualifier OR box score math. Convert to decimal: "76.3%" -> 0.763. Reject any "85%" from ATP overview pages.
- ace_avg_this_surface: look for "aces per match", "ace average". Return float.
- confirmed_playing: true unless "withdrew", "injured", "retirement", "out" mentioned.
- source_quality: "tier1" for atptour.com/wtatennis.com, "tier2" for tennisabstract.com/flashscore.com/espn.com, "tier3" for others.
- source_url: domain of the main source used.
- If a field genuinely cannot be found in the text, use null. Do NOT leave null if the value is clearly stated.

V8 API FIELDS — ALREADY CONFIRMED TIER 1 DATA (do not re-search for these):
The following fields may already be populated in research_data from the V8 Tennis Stats API
before this extraction runs. If present and non-null, treat them as CONFIRMED Tier 1 data
and use them directly — do not override with web search extractions unless the web source
is explicitly more specific (e.g. surface-filtered vs cross-surface):
- avg_double_faults: confirmed DF average per match from V8 box scores
- avg_aces_v8 / ace_avg_this_surface: confirmed ace average from V8
- bp_save_pct_v8: confirmed break point save percentage from V8
- break_pct_v8: confirmed break percentage from V8
- first_serve_pct_v8: confirmed first serve percentage from V8
- serve_hold_pct: confirmed hold percentage injected from V8 (already decimal)

{
  "ranking": integer or null,
  "surface_win_pct": float 0-1 or null,
  "surface_record": "W-L" or null,
  "recent_form_last3": ["W","L","W"] or null,
  "recent_match_scores": ["won 6-3 6-4 vs Player at Tournament 2026"] or [],
  "recent_stat_results": [numbers] or null,
  "primary_surface": "clay" or "hard" or "grass" or null,
  "nationality": "country" or null,
  "serve_hold_pct": float 0-1 or null,
  "ace_avg_this_surface": float or null,
  "avg_double_faults": float or null,
  "avg_aces_v8": float or null,
  "bp_save_pct_v8": float or null,
  "break_pct_v8": float or null,
  "first_serve_pct_v8": float or null,
  "this_tournament_matches": integer or null,
  "this_tournament_3set_count": integer or null,
  "serve_hold_method": "published" or "derived" or null,
  "serve_hold_matches": integer or null,
  "serve_hold_surface": "grass" or "clay" or "hard" or "all" or null,
  "recent_surface_title": true or false,
  "typical_gw_when_winning": integer or null,
  "confirmed_playing": true or false — CONFIRMED_PLAYING DEFINITION: set to false ONLY if there is direct evidence the player has withdrawn, retired from the CURRENT tournament, or is officially unavailable for the upcoming match being evaluated. A player winning or losing a PAST match via retirement (their own retirement in a prior round, or an opponent retiring against them) does NOT make confirmed_playing false — that is normal historical match data, not a withdrawal signal for the player being evaluated now. If no evidence of withdrawal/unavailability exists, default to true.
  "source_quality": "tier1" or "tier2" or "tier3",
  "source_url": "domain.com" or null
}"""

def research_player(player, surface, tournament, stat_type, year, dates,
                     v8_covers_stat=False, v8_has_player_data=False):
    ck = cache_key(player, surface, tournament)
    if ck in _research_cache:
        log("Research", f"Cache hit: {player}")
        return _research_cache[ck]

    # Bug fix (this session): the v7.9.7 "TRIM" comment below claimed
    # results/matches/ranking queries were already removed because V8
    # covers them -- they weren't. The ATP ranking query ran every
    # single time regardless of V8 status, and results/recent-matches
    # queries duplicated V8's own recent_matches data every time too.
    # Confirmed live: running these against a player V8 already had
    # Tier 1 data for burned real search calls and wall-clock time
    # finding nothing new, every single evaluation, for every prop on
    # the same player. Web search's actual job is to fill what V8
    # structurally can't provide -- injury/withdrawal status and
    # tournament-progress narrative -- not re-derive numbers V8
    # already gave us. Only the genuinely V8-blind query always runs;
    # everything else is now conditional on V8 actually lacking it.
    queries = [f"{player} injury withdrawal status {year} tennis"]

    if not v8_has_player_data:
        # V8 has nothing for this player at all -- fall back to the
        # full manual sweep, same as the pre-V8 protocol.
        queries = [
            f'"{player}" results scores {year} {surface} -site:espn.com',
            f'"{player}" recent matches {year} site:flashscore.com',
            f'"{player}" {tournament} {year} results sets -site:wikipedia.org',
            f"{player} ATP ranking {year} tennis -site:wikipedia.org",
        ] + queries

    # FIX 2 — Aces-specific stat V8 does not cover (ace_avg_this_surface)
    # Only run this if V8 does NOT already have this exact stat --
    # v8_covers_stat is computed by the caller from the real V8 packet.
    # Bug fix (this session): stat_type here can be a raw display
    # string ("Aces", "Games Won") not a slug -- normalize before
    # comparing, same fix as the caller's v8_covers_stat computation.
    _stat_norm = (stat_type or "").lower().strip().replace(" ", "_")
    if not v8_covers_stat:
        if _stat_norm == "aces":
            queries.append(f"{player} aces per match {surface} {year} tennis -site:wikipedia.org")
        elif _stat_norm == "games_won":
            queries.append(f"{player} typical winning score {surface} {year}")

    raw_texts      = []
    missing_fields = []
    verified_count = 0

    # Cap trimmed from 11-14 down to the length of the trimmed list above —
    # V8 covers what the removed queries used to chase, so there is no
    # longer a need to burn extra Serper credits on that data.
    query_cap = len(queries)
    for query in queries[:query_cap]:
        for _attempt in range(1, 3):
            result = web_search(query)
            if result:
                is_verified, _source, _cap = validate_research_source(
                    result, player, stat_type, dates
                )
                raw_texts.append(result[:1500])
                if is_verified:
                    verified_count += 1
                break

    if verified_count == 0 and raw_texts:
        if v8_covers_stat:
            # Bug fix (this session): V8 already confirmed this exact
            # stat (Tier 1) -- this function's queries are mostly
            # generic (ranking/injury/results), not stat-specific, so
            # them failing to find a "verified source" says nothing
            # about whether the actual stat is trustworthy. No alert,
            # no missing_fields flag: V8 data doesn't need web
            # corroboration, same principle already applied to hold%.
            log("Research", f"{player} {stat_type}: web verification queries "
                f"found no listed source, but V8 already confirmed this stat "
                f"(Tier 1) -- not flagging, not alerting.")
        else:
            missing_fields.append("unverified_source")
            # Bug fix (this session, found live): this alert used to
            # unconditionally claim "ranking/injury/current-form" were
            # searched and that the prop is "capped at YELLOW" -- both
            # false in the common case. When v8_has_player_data is True,
            # only the single injury query actually runs (ranking/
            # results/matches are V8-owned and were never searched this
            # pass) -- the old text described three categories when at
            # most one was checked. Worse: a downstream reconciliation
            # step (in research_agent, right after this returns) clears
            # this exact "unverified_source" flag whenever V8 provided
            # ANY confirmed data for the player -- meaning the "capped"
            # claim below was frequently false by the time real scoring
            # ran, and the alert never told you that. Now both are
            # accurate: what was actually searched, and whether a cap
            # will genuinely stick or is about to be cleared.
            if v8_has_player_data:
                send_discord("alerts",
                    f"⚠️ **UNVERIFIED GENERAL CONTEXT (informational)** — {dates['ct_time']}\n"
                    f"Player: {player} | Evaluating: {stat_type}\n"
                    f"V8 has Tier 1 data for this player. Only the injury/withdrawal "
                    f"check was run (ranking/results/recent-matches are V8-owned and "
                    f"were not re-searched this pass) — it found no source from the "
                    f"verified list. This does NOT cap the prop: V8's confirmed data "
                    f"for this player clears this flag automatically before scoring. "
                    f"Shown for visibility only, not as a grading concern.\n"
                    f"Sources checked: {', '.join(VERIFIED_SOURCES[:4])}"
                )
            else:
                send_discord("alerts",
                    f"⚠️ **UNVERIFIED GENERAL CONTEXT** — {dates['ct_time']}\n"
                    f"Player: {player} | Evaluating: {stat_type}\n"
                    f"V8 has no confirmed data for this player. Full manual search "
                    f"(results, recent matches, tournament results, ranking, "
                    f"injury/withdrawal) found no source from the verified list. "
                    f"This genuinely caps the prop at YELLOW — no Tier 1 or Tier 2 "
                    f"data exists to clear it.\n"
                    f"Sources checked: {', '.join(VERIFIED_SOURCES[:4])}\n"
                    f"**Prop capped at YELLOW — no confirmed data from any source**"
                )

    if not raw_texts:
        missing_fields.append("all_data")
        return _empty_player_profile(player, missing_fields)

    combined = "\n\n".join(raw_texts)
    extraction_user = (
        f"Player: {player}\nSurface: {surface}\n"
        f"Tournament: {tournament}\n\nData:\n{combined}"
    )

    result_text = call_model(
        MODEL_HAIKU, RESEARCH_EXTRACTION_PROMPT,
        extraction_user, temperature=0.1, max_tokens=500
    )

    if result_text:
        try:
            clean = re.sub(r"```json|```", "", result_text).strip()
            data  = json.loads(clean)
            data["name"]           = player
            data["missing_fields"] = missing_fields

            # TENNISRATIO FALLBACK — if hold data still null, fetch directly
            if not data.get("serve_hold_pct"):
                tr_hold = fetch_tennisratio_hold(player)
                if tr_hold:
                    data["serve_hold_pct"] = tr_hold
                    log("Research", f"TennisRatio fallback hold: {player} {tr_hold:.3f}")

            _research_cache[ck]    = data
            log("Research", f"Done: {player} #{data.get('ranking','?')}")
            return data
        except json.JSONDecodeError:
            log("Research", f"JSON parse failed for {player}")

    return _empty_player_profile(player, ["parse_failure"])

def _empty_player_profile(name, missing_fields=None):
    return {
        "name": name, "ranking": None, "surface_win_pct": None,
        "surface_record": None, "recent_form_last3": None,
        "recent_match_scores": [],
        "recent_stat_results": None, "primary_surface": None,
        "nationality": None, "serve_hold_pct": None,
        "ace_avg_this_surface": None, "this_tournament_matches": None,
        "this_tournament_3set_count": None, "typical_gw_when_winning": None,
        "confirmed_playing": True,
        "recent_surface_title": False,
        "serve_hold_method": None,
        "serve_hold_matches": None,
        "serve_hold_surface": None,
        "missing_fields": missing_fields or ["all_data"],
    }

# ================================================================
# PLAYER PROFILE STORE — persistent 24hr data cache
# ================================================================

def _profile_key(player_name: str, surface: str) -> Path:
    """Returns the path for a player's persistent profile file."""
    safe = re.sub(r"[^a-zA-Z0-9]", "_", player_name)[:40]
    surf = re.sub(r"[^a-zA-Z0-9]", "_", (surface or "all").lower())
    return PROFILE_DIR / f"{safe}_{surf}.json"


def load_player_profile(player_name: str, surface: str,
                        ttl_hours: float = 24) -> dict:
    """
    Loads a player's persistent profile if it exists and is fresh.

    Returns the profile dict if data was fetched within ttl_hours,
    or None if the profile is missing, stale, or unreadable — in
    which case the caller should fetch fresh from the V8 API.

    ttl_hours is read from weights.json by the caller and passed in
    so this function stays pure and testable without touching weights.
    """
    path = _profile_key(player_name, surface)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            profile = json.load(f)
        fetched_at_str = profile.get("fetched_at")
        if not fetched_at_str:
            return None
        fetched_at = datetime.fromisoformat(fetched_at_str)
        # Make fetched_at timezone-aware if naive
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
        if age_hours > ttl_hours:
            log("Profile", f"Stale ({age_hours:.1f}h > {ttl_hours}h) — "
                f"will refresh: {player_name} | {surface}")
            return None
        # Bug fix (this session): defense-in-depth alongside the
        # save-time fix in save_player_profile -- if any profile with
        # resolution_failed=True still exists on disk (e.g. saved
        # before that fix was deployed), don't trust it just because
        # it's within TTL. A failed resolution is not valid data
        # regardless of how it got persisted.
        if profile.get("resolution_failed"):
            log("Profile", f"Ignoring cached profile for {player_name} | "
                f"{surface} -- resolution_failed=True on disk (stale from "
                f"before this fix, or an edge case that slipped through). "
                f"Forcing a fresh fetch instead.")
            return None
        log("Profile", f"Cache hit ({age_hours:.1f}h old) — "
            f"reusing: {player_name} | {surface}")
        return profile
    except Exception as e:
        log("Profile", f"Profile read failed for {player_name}: {e}")
        return None


def save_player_profile(player_name: str, surface: str,
                        v8_stats: dict) -> None:
    """
    Writes a player's V8 stats to their persistent profile file,
    stamped with the current UTC time as fetched_at.

    Called after every successful fresh V8 fetch so the next
    evaluation within the TTL window can skip the API call entirely.
    """
    # Bug fix (this session, confirmed live on a real pick): this used
    # to save WHATEVER it was given, unconditionally -- including a
    # genuinely failed resolution (resolution_failed=True, from
    # _empty_stats in openclaw_v8_api.py). Confirmed live: Katie
    # Volynets (a real, well-documented WTA #101 player, 69 real hard-
    # court matches on file) was queried under the WRONG tour (atp) on
    # the very first evaluation of the night, before Gate 11's later,
    # per-prop tour correction ever ran. That failed lookup got saved
    # to disk as if it were her real profile -- and because this cache
    # is checked BEFORE Gate 11 even runs (and has no idea what tour
    # was used), every subsequent evaluation kept loading the same
    # poisoned, empty profile for the full TTL window (up to 24h),
    # even after Gate 11 correctly identified and fixed the tour each
    # time. A single wrong-tour or transient lookup failure should
    # never get to silently lock out real data for hours. Deliberately
    # scoped to resolution_failed specifically, NOT data_tier ==
    # "MISSING" broadly -- after this session's earlier fixes,
    # "MISSING" can also mean a genuinely resolved player with real
    # data but zero surface-specific history (e.g. Curmi), which IS
    # legitimate, useful information worth caching.
    if v8_stats.get("resolution_failed"):
        log("Profile", f"NOT saving profile for {player_name} | {surface} -- "
            f"resolution_failed=True (this lookup found nothing, possibly "
            f"due to a wrong tour or transient failure). Caching a failure "
            f"would lock out real data for the full TTL window; leaving "
            f"unsaved so the next evaluation retries fresh instead.")
        return

    path = _profile_key(player_name, surface)
    try:
        profile = {
            **v8_stats,
            "player_name": player_name,
            "surface":     surface,
            "fetched_at":  datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w") as f:
            json.dump(profile, f, indent=2, default=str)
        log("Profile", f"Saved: {player_name} | {surface} → {path.name}")
    except Exception as e:
        log("Profile", f"Profile save failed for {player_name}: {e}")


# v7.9.7 (Evidence Strength Score, this session): weighted, stat-type-aware
# field importance for ESS computation. Each prop stat_type has its own
# weighting reflecting which fields actually matter most for that prop --
# flat field-counting was explicitly rejected because it would let a DF
# prop score "75% complete" while missing the single most important
# datapoint (avg_double_faults) for that specific stat. Weights sum to
# 100 within each stat_type's mapping.
ESS_FIELD_WEIGHTS = {
    "double_faults": {
        "avg_double_faults":   40,
        "first_serve_pct_v8":  20,
        "serve_hold_pct":      15,
        "recent_form_last3":   15,
        "ranking":             10,
    },
    "aces": {
        "avg_aces_v8":         40,
        "serve_hold_pct":      20,
        "first_serve_pct_v8":  15,
        "surface_record":      15,
        "ranking":             10,
    },
    "total_games": {
        "serve_hold_pct":      25,
        "break_pct_v8":        25,
        "surface_record":      20,
        "recent_form_last3":   15,
        "ranking":             15,
    },
    "games_won": {
        "serve_hold_pct":      25,
        "break_pct_v8":        25,
        "surface_record":      20,
        "recent_form_last3":   15,
        "ranking":             15,
    },
    "break_points_won": {
        "bp_save_pct_v8":      35,
        "break_pct_v8":        25,
        "serve_hold_pct":      20,
        "recent_form_last3":   10,
        "ranking":             10,
    },
    # Fallback weighting for any stat_type not explicitly mapped above --
    # a reasonable, general-purpose evidence mix rather than a crash or
    # an unweighted flat count.
    "_default": {
        "serve_hold_pct":      25,
        "surface_record":      20,
        "recent_form_last3":   20,
        "ranking":             20,
        "break_pct_v8":        15,
    },
}


def _compute_evidence_strength_score(prop) -> None:
    """
    Evidence Strength Score (ESS) — locked spec, this session.
    Informational only. Answers "how complete is the evidence package
    that supported this prop evaluation?" — a DIFFERENT question from
    confidence, which answers "how sure is the pick." A thin-evidence
    prop and a rich-evidence prop can land at the same confidence %
    today with no way to tell them apart; ESS makes that visible
    without touching confidence, grading, direction, or any weight.

    Scope: per-prop, combining BOTH players' data together (not two
    separate per-player scores) — the real question is whether the
    evaluation as a whole had what it needed, not how each player
    individually scored.

    Method: weighted completeness, using ESS_FIELD_WEIGHTS for this
    prop's stat_type (falling back to "_default" if unmapped). Each
    weighted field is checked against BOTH player_data and
    opponent_data — a field counts as present only if confirmed for
    BOTH players, since a prop needs both sides' data to be genuinely
    well-evidenced.

    Bands (per locked spec — deliberately NOT named THIN/MODERATE/
    STRONG, since those read as confidence judgments; ESS measures
    evidence availability, a different concept):
      0-40   = LIMITED
      41-70  = ADEQUATE
      71-100 = ROBUST

    Stores ess_score, ess_band, and critical_data_missing (any
    weighted field missing for either player) directly on the prop
    dict. Explicitly does NOT modify eval_confidence, color grade, any
    deduction, or any weight anywhere in the pipeline. Per the locked
    plan: observe archived outcomes first; only consider promoting
    this to a real scoring signal if evidence later shows ESS actually
    correlates with pick accuracy.
    """
    stat_type = (prop.get("stat_type") or "").lower().replace(" ", "_")
    weights = ESS_FIELD_WEIGHTS.get(stat_type, ESS_FIELD_WEIGHTS["_default"])

    player_data   = prop.get("research_data", {}).get("player", {}) or {}
    opponent_data = prop.get("research_data", {}).get("opponent", {}) or {}

    earned = 0
    total_possible = sum(weights.values())
    critical_missing = []

    for field, weight in weights.items():
        p_present = player_data.get(field) is not None
        o_present = opponent_data.get(field) is not None
        if p_present and o_present:
            earned += weight
        else:
            critical_missing.append(field)

    ess_score = round((earned / total_possible) * 100, 1) if total_possible > 0 else 0.0

    if ess_score <= 40:
        ess_band = "LIMITED"
    elif ess_score <= 70:
        ess_band = "ADEQUATE"
    else:
        ess_band = "ROBUST"

    prop["ess_score"] = ess_score
    prop["ess_band"] = ess_band
    prop["critical_data_missing"] = critical_missing

    log("Evidence", f"ESS for {prop.get('player')} {prop.get('stat_type')}: "
        f"{ess_score} ({ess_band}) — missing: {critical_missing or 'none'}")


def _compute_expected_service_games(prop, p_v8: dict, o_v8: dict) -> None:
    """
    Expected Service Games (ESG) — locked spec, this session.
    Informational only. Estimates OPPORTUNITY VOLUME — "how many
    service games is this player likely to serve in a typical version
    of this match?" — NOT outcome probability, NOT a prediction of
    aces/DFs/wins. Deliberately excludes ranking, per explicit
    instruction: ESG is a match-environment/opportunity metric, not a
    player-strength metric. Ranking already influences other parts of
    this system; ESG stays in its own lane.

    Stored in research_data.match_context (a match-environment metric,
    not a per-player stat, per locked preference) — NOT research_data.
    player/opponent.

    Formula:
      baseline = 10 (BO3) or 16 (BO5), from is_grand_slam
      combined_extender_rate = AVERAGE (not MAX) of both players'
        extender_rate — average was explicitly chosen over max because
        max lets one player's high extender rate dominate the estimate
        even when the other player has never extended recently; average
        gives a more balanced match-environment read, consistent with
        estimating match opportunity rather than one player's tendency.
        >= 0.50      -> +2
        0.25 - 0.49  -> 0
        < 0.25       -> -2
      combined_hold = player_hold_pct + opponent_hold_pct (decimal scale)
        >= 1.65      -> +1
        1.45 - 1.64  -> 0
        < 1.45       -> -1
      ESG = baseline + extender_adjustment + hold_adjustment

    Bands are FORMAT-SPECIFIC (locked correction, this session): a
    single band set applied to both formats made BO5 matches land in
    HIGH_OPPORTUNITY almost unconditionally (16 baseline +/- adjustments
    never drops below 13), making the band uninformative for BO5. Two
    separate band sets let BO5 matches actually move between categories.

    BO3: 0-8 LOW_OPPORTUNITY | 9-13 NORMAL_OPPORTUNITY | 14+ HIGH_OPPORTUNITY
    BO5: 0-14 LOW_OPPORTUNITY | 15-17 NORMAL_OPPORTUNITY | 18+ HIGH_OPPORTUNITY

    Explicitly does NOT modify eval_confidence, color grade, any
    deduction, direction, or any weight anywhere in the pipeline. Same
    phased discipline as SPI and ESS: derive, store, display, observe
    real outcomes, decide on scoring influence later only if evidence
    supports it.
    """
    is_bo5 = bool(prop.get("is_grand_slam"))
    baseline = 16 if is_bo5 else 10

    p_ext_rate = p_v8.get("extender_rate")
    o_ext_rate = o_v8.get("extender_rate")
    if p_ext_rate is not None and o_ext_rate is not None:
        combined_extender = (p_ext_rate + o_ext_rate) / 2
    elif p_ext_rate is not None:
        combined_extender = p_ext_rate
    elif o_ext_rate is not None:
        combined_extender = o_ext_rate
    else:
        combined_extender = None

    if combined_extender is None:
        extender_adj = 0
        extender_note = "unavailable — treated as neutral"
    elif combined_extender >= 0.50:
        extender_adj = 2
        extender_note = f"strong extender profile ({combined_extender:.2f})"
    elif combined_extender >= 0.25:
        extender_adj = 0
        extender_note = f"neutral ({combined_extender:.2f})"
    else:
        extender_adj = -2
        extender_note = f"compression profile ({combined_extender:.2f})"

    p_hold = p_v8.get("hold_pct")
    o_hold = o_v8.get("hold_pct")
    if p_hold is not None and o_hold is not None:
        # hold_pct from v8_api is stored as a raw percentage (e.g. 86.0),
        # matching the 1.65/1.45 thresholds' scale when summed as
        # decimals (0.86 + 0.80 = 1.66) -- convert here for the combined
        # check per the locked spec's decimal-scale thresholds.
        combined_hold = (p_hold / 100.0) + (o_hold / 100.0)
    else:
        combined_hold = None

    if combined_hold is None:
        hold_adj = 0
        hold_note = "unavailable — treated as neutral"
    elif combined_hold >= 1.65:
        hold_adj = 1
        hold_note = f"strong hold environment ({combined_hold:.2f})"
    elif combined_hold >= 1.45:
        hold_adj = 0
        hold_note = f"neutral hold environment ({combined_hold:.2f})"
    else:
        hold_adj = -1
        hold_note = f"weak hold environment ({combined_hold:.2f})"

    esg = baseline + extender_adj + hold_adj

    if is_bo5:
        if esg <= 14:
            esg_band = "LOW_OPPORTUNITY"
        elif esg <= 17:
            esg_band = "NORMAL_OPPORTUNITY"
        else:
            esg_band = "HIGH_OPPORTUNITY"
    else:
        if esg <= 8:
            esg_band = "LOW_OPPORTUNITY"
        elif esg <= 13:
            esg_band = "NORMAL_OPPORTUNITY"
        else:
            esg_band = "HIGH_OPPORTUNITY"

    if "match_context" not in prop.get("research_data", {}):
        prop.setdefault("research_data", {})["match_context"] = {}

    prop["research_data"]["match_context"]["expected_service_games"] = esg
    prop["research_data"]["match_context"]["esg_band"] = esg_band
    prop["research_data"]["match_context"]["esg_source"] = "derived_v8"

    log("Evidence", f"ESG for {prop.get('player')} vs {prop.get('opponent')}: "
        f"{esg} ({esg_band}) — format={'BO5' if is_bo5 else 'BO3'} baseline={baseline}, "
        f"extender={extender_note}, hold={hold_note}")


# Bug fix (this session, confirmed live): the V8 DATA QUALITY ALERT below
# fires from inside research_agent, which runs once per PROP -- but the
# fact it's reporting (a player's V8 data tier) is true of the MATCH, not
# any individual prop. Confirmed live: the identical alert fired 5 times
# in a row for one match (Putintseva vs Radivojevic) because all 5
# evaluable props on that card independently re-discovered and re-alerted
# the same underlying fact. This set tracks which (player, opponent)
# match-ups have already fired this alert during the current process
# lifetime, so it fires once per match instead of once per prop.
_v8_quality_alerted_matches = set()

def research_agent(prop, dates):
    player     = prop.get("player", "")
    opponent   = prop.get("opponent", "")
    surface    = prop.get("surface", "clay")
    tournament = prop.get("tournament", "")
    stat_type  = prop.get("stat_type", "")
    # Bug fix (this session, found live): stat_type from screenshot
    # intake is often a raw display string ("Total Games Won", "Break
    # Points Won") not a normalized slug -- confirmed live, an alert
    # printed "Evaluating: Total Games Won" verbatim. The _v8_covers
    # mapping below (and research_player's aces/games_won special-case)
    # compare against lowercase-underscore keys like "total_games_won"
    # -- those never matched a raw display string, so v8_covers_stat
    # was silently always False even when V8 genuinely had the stat,
    # and the fix from earlier tonight never actually engaged for any
    # screenshot-sourced prop. This normalizes ONLY for matching
    # purposes; stat_type itself (used for display/logging) is
    # untouched.
    stat_type_norm = (stat_type or "").lower().strip().replace(" ", "_")
    year       = datetime.now(CT).year

    # ── V8 TENNIS STATS API LAYER — RUNS FIRST ──────────────────
    # v7.9.7 fix: V8 is the Tier 1 confirmed data source and must be
    # checked BEFORE web search. Previously web search ran first and
    # could flag a stat "unverified" even when V8 already had the
    # confirmed number for it (e.g. total_games), firing a false
    # RESEARCH GAP alert and wrongly capping the prop at YELLOW.
    # Falls back silently — pipeline continues without it if unavailable.
    v8_packet  = None
    p_hold_v8 = o_hold_v8 = None
    p_recent  = o_recent  = []
    p_surf_rec = o_surf_rec = None
    p_rank_v8  = o_rank_v8  = None
    p_df_v8    = o_df_v8    = None

    if _V8_AVAILABLE:
        try:
            tour = "wta" if prop.get("tour", "atp").lower() == "wta" else "atp"

            # ── Player profile TTL from weights.json ──────────────
            # v7.9.7 fix (item 16): the flat 24hr TTL doesn't account
            # for active tournaments where matches happen daily and a
            # player's stats update every single day (e.g. Wimbledon
            # Day 1 -> Day 2). A profile fetched at 11 PM on Day 1 was
            # being reused through 11 PM on Day 2 even though that
            # day's matches had already updated the real numbers.
            # Confirmed: the system did NOT self-correct for this
            # before this fix. Grand Slam props (is_grand_slam flag,
            # already set upstream) now use a shorter TTL to stay
            # fresh during peak daily-update periods; everything else
            # keeps the normal weights.json-configured default.
            weights_for_ttl = load_weights()
            base_ttl_hours = float(weights_for_ttl.get("player_profile_ttl_hours", 24))
            active_tournament_ttl_hours = float(weights_for_ttl.get("active_tournament_ttl_hours", 6))
            if prop.get("is_grand_slam"):
                ttl_hours = active_tournament_ttl_hours
                log("Profile", f"Active tournament TTL applied: {ttl_hours}h "
                    f"(Grand Slam prop, {player} vs {opponent})")
            else:
                ttl_hours = base_ttl_hours

            # ── Check persistent player profiles first ────────────
            # v7.9.6: before hitting the V8 API, check whether we
            # already have fresh confirmed data for both players saved
            # in data/players/. If both profiles are within TTL, skip
            # the API entirely and reconstruct the packet from profiles.
            # This covers both props AND moneyline evaluations since
            # both call research_agent under the hood.
            p_profile = load_player_profile(player, surface, ttl_hours)
            o_profile = load_player_profile(opponent, surface, ttl_hours)

            if p_profile and o_profile:
                # Both players have fresh confirmed data — reconstruct
                # the packet from profiles without touching the API
                log("Profile", f"Both profiles fresh — skipping API for "
                    f"{player} vs {opponent}")
                _p_tier = p_profile.get("data_tier", "Tier2")
                _o_tier = o_profile.get("data_tier", "Tier2")

                # Bug fix (this session): this branch used to hardcode
                # h2h to UNAVAILABLE unconditionally, even when a real,
                # correctly-fetched H2H (via get_h2h(), often CONFIRMED)
                # was already sitting in prop["_v8_packet_prefetched"]
                # from the once-per-card prefetch step. Confirmed live:
                # a genuine CONFIRMED 0-0 H2H was fetched, then silently
                # discarded and replaced with UNAVAILABLE purely because
                # both players' individual stat profiles also happened
                # to be cached and fresh -- a condition that has nothing
                # to do with whether H2H itself is actually available.
                # Now checks the prefetched packet for real H2H data
                # first and reuses it; only falls back to UNAVAILABLE if
                # no prefetched H2H exists or it wasn't CONFIRMED either.
                # Bug fix (this session): this salvage branch reused
                # the prefetched h2h with NO tour check, while the main
                # packet-reuse branch below correctly requires
                # _prefetch_tour == tour before trusting the prefetch.
                # Same stale-prefetch failure class as the compliance
                # ground-truth bug fixed this session: if the prefetch
                # ran under the wrong tour and still came back with a
                # CONFIRMED h2h, that wrong-context h2h would be spliced
                # into an otherwise-correct packet built from cached
                # profiles. Guard it with the exact same condition the
                # branch below uses.
                _prefetched_pkt = prop.get("_v8_packet_prefetched", {}) or {}
                _prefetched_h2h = _prefetched_pkt.get("h2h")
                if _prefetched_h2h and _prefetched_h2h.get("status") == "CONFIRMED" \
                        and _prefetched_pkt.get("_prefetch_tour", tour) == tour:
                    _h2h_for_packet = _prefetched_h2h
                    log("Profile", f"Reusing CONFIRMED H2H from prefetched packet "
                        f"for {player} vs {opponent} instead of discarding it")
                else:
                    _h2h_for_packet = {"status": "UNAVAILABLE"}

                v8_packet = {
                    "player":       p_profile,
                    "opponent":     o_profile,
                    "h2h":          _h2h_for_packet,
                    "data_quality": {
                        "player_tier":   _p_tier,
                        "opponent_tier": _o_tier,
                        # Bug fix (this session): confidence_cap was
                        # missing entirely from this manually-built
                        # data_quality dict -- every OTHER packet-
                        # construction path in this system includes it,
                        # but this specific "load from cached profiles"
                        # branch never did. v8_validation() unconditionally
                        # does dq["confidence_cap"] (bracket access, not
                        # .get()), so any evaluation that hit this exact
                        # path threw a real KeyError, silently caught by
                        # the outer try/except and logged as "Packet
                        # injection failed (non-fatal): 'confidence_cap'"
                        # -- confirmed live in tonight's logs. Using the
                        # same DATA_TIER_CONFIDENCE_CAP mapping the rest
                        # of this file already uses for consistency.
                        "confidence_cap": min(
                            DATA_TIER_CONFIDENCE_CAP.get(_p_tier, 65),
                            DATA_TIER_CONFIDENCE_CAP.get(_o_tier, 65),
                        ),
                        "notes":         ["Loaded from persistent player profiles"],
                    },
                    "surface": surface,
                    "tour":    tour,
                }
            # ── Pre-fetched packet from evaluate_manual_prop ──────
            elif prop.get("_v8_packet_prefetched") and \
                 prop["_v8_packet_prefetched"].get("_prefetch_tour", tour) == tour:
                v8_packet = prop["_v8_packet_prefetched"]
                log("V8", "Reusing pre-fetched packet — skipping duplicate API call")
                # Save both player stats to profiles after the pre-fetch
                # so subsequent evaluations can skip the API entirely
                save_player_profile(player, surface,
                                    v8_packet.get("player", {}))
                save_player_profile(opponent, surface,
                                    v8_packet.get("opponent", {}))
            else:
                # Fresh API fetch — save results to persistent profiles.
                # Bug fix (this session): this path is now also reached
                # when a prefetched packet existed but was built for a
                # DIFFERENT tour than Gate 11 later corrected to (see
                # the elif above) -- distinguishing that case in the log
                # from "no prefetch ever existed" so a future session
                # doesn't have to re-derive this from scratch again.
                if prop.get("_v8_packet_prefetched"):
                    log("V8", f"Prefetched packet was built for tour="
                        f"{prop['_v8_packet_prefetched'].get('_prefetch_tour', '?')} "
                        f"but Gate 11 corrected to tour={tour} -- discarding "
                        f"stale packet, fetching fresh.")
                v8_packet = build_tennis_packet(player, opponent, surface, tour)
                save_player_profile(player, surface,
                                    v8_packet.get("player", {}))
                save_player_profile(opponent, surface,
                                    v8_packet.get("opponent", {}))
                log("Profile", f"Profiles saved after fresh fetch: "
                    f"{player} | {opponent} | {surface}")

            p_v8 = v8_packet.get("player", {})
            o_v8 = v8_packet.get("opponent", {})
            p_hold_v8  = p_v8.get("hold_pct")
            o_hold_v8  = o_v8.get("hold_pct")
            p_recent   = p_v8.get("recent_matches", [])
            o_recent   = o_v8.get("recent_matches", [])
            p_surf_rec = p_v8.get("surface_record")
            o_surf_rec = o_v8.get("surface_record")
            p_rank_v8  = p_v8.get("ranking")
            o_rank_v8  = o_v8.get("ranking")
            p_df_v8    = p_v8.get("avg_double_faults")
            o_df_v8    = o_v8.get("avg_double_faults")
            p_win_pct_v8 = p_v8.get("win_pct")
            o_win_pct_v8 = o_v8.get("win_pct")

            log("V8", f"Hold% confirmed \u2014 {player}: {p_hold_v8}% | {opponent}: {o_hold_v8}%")
            log("V8", f"Avg DF confirmed \u2014 {player}: {p_df_v8} | {opponent}: {o_df_v8}")

        except Exception as _v8e:
            log("V8", f"Packet build failed (non-fatal): {_v8e}")
            v8_packet = None
    # ────────────────────────────────────────────────────────────

    # ── DOES V8 ALREADY COVER THIS EXACT PROP STAT? ─────────────
    # Reuses the same stat-name correspondences as the v8_confirmed_fields
    # reconciliation below, computed here so research_player() can know
    # BEFORE it runs whether a failed generic web search actually means
    # anything for this stat, instead of finding out too late (after
    # already sending an alert) the way the old code did.
    def _v8_covers(stat, p_pack):
        if not p_pack:
            return False
        mapping = {
            "aces":              p_pack.get("avg_aces"),
            "double_faults":     p_pack.get("avg_double_faults"),
            "break_points_won":  p_pack.get("bp_save_pct") if p_pack.get("bp_save_pct") is not None else p_pack.get("break_pct"),
            "total_games":       p_pack.get("hold_pct"),
            "total_games_won":   p_pack.get("hold_pct"),
            "games_won":         p_pack.get("hold_pct"),
        }
        return mapping.get(stat) is not None

    p_v8_covers_this = _v8_covers(stat_type_norm, p_v8 if v8_packet else None)
    o_v8_covers_this = _v8_covers(stat_type_norm, o_v8 if v8_packet else None)
    p_v8_has_data = bool(v8_packet and p_v8 and p_v8.get("hold_pct") is not None)
    o_v8_has_data = bool(v8_packet and o_v8 and o_v8.get("hold_pct") is not None)

    # ── WEB SEARCH — runs second, fills what V8 doesn't cover ───
    player_data   = research_player(player, surface, tournament, stat_type, year, dates,
                                     v8_covers_stat=p_v8_covers_this,
                                     v8_has_player_data=p_v8_has_data)
    opponent_data = research_player(opponent, surface, tournament, stat_type, year, dates,
                                     v8_covers_stat=o_v8_covers_this,
                                     v8_has_player_data=o_v8_has_data)

    # ── RECONCILE MISSING FIELDS AGAINST WHAT V8 ACTUALLY CONFIRMED ──
    # v7.9.7 fix: research_player() flags missing_fields from web search
    # alone. Since V8 is the Tier 1 source, any field it actually
    # populated should be cleared here BEFORE the gap alert is computed
    # — this is what stops the false "RESEARCH GAP" alert and the wrong
    # YELLOW cap on stats V8 already confirmed (e.g. total_games).
    v8_confirmed_fields = {
        "serve_hold_pct":    (p_hold_v8, o_hold_v8),
        "recent_form_last3": (p_recent, o_recent),
        "surface_record":    (p_surf_rec, o_surf_rec),
        "ranking":           (p_rank_v8, o_rank_v8),
        "avg_double_faults": (p_df_v8, o_df_v8),
        "ace_avg_this_surface": (p_v8.get("avg_aces") if v8_packet else None,
                                 o_v8.get("avg_aces") if v8_packet else None),
        "bp_save_pct_v8":    (p_v8.get("bp_save_pct") if v8_packet else None,
                              o_v8.get("bp_save_pct") if v8_packet else None),
        "break_pct_v8":      (p_v8.get("break_pct") if v8_packet else None,
                              o_v8.get("break_pct") if v8_packet else None),
        "first_serve_pct_v8":(p_v8.get("first_serve") if v8_packet else None,
                              o_v8.get("first_serve") if v8_packet else None),
    }
    for field, (p_val, o_val) in v8_confirmed_fields.items():
        if p_val and field in player_data.get("missing_fields", []):
            player_data["missing_fields"].remove(field)
            log("V8", f"Cleared missing-field flag ({field}) for {player} — V8 confirmed")
        if o_val and field in opponent_data.get("missing_fields", []):
            opponent_data["missing_fields"].remove(field)
            log("V8", f"Cleared missing-field flag ({field}) for {opponent} — V8 confirmed")

    # Also clear the generic "unverified_source" flag if V8 gave us
    # ANY confirmed data for that player — the source IS verified,
    # it just isn't a web-search source.
    if any([p_hold_v8, p_recent, p_surf_rec, p_rank_v8, p_df_v8]):
        if "unverified_source" in player_data.get("missing_fields", []):
            player_data["missing_fields"].remove("unverified_source")
            log("V8", f"Cleared unverified_source flag for {player} — V8 provided confirmed data")
    if any([o_hold_v8, o_recent, o_surf_rec, o_rank_v8, o_df_v8]):
        if "unverified_source" in opponent_data.get("missing_fields", []):
            opponent_data["missing_fields"].remove("unverified_source")
            log("V8", f"Cleared unverified_source flag for {opponent} — V8 provided confirmed data")

    has_gaps = (len(player_data.get("missing_fields", [])) +
                len(opponent_data.get("missing_fields", []))) > 0

    if has_gaps:
        gap_details = []
        if player_data.get("missing_fields"):
            gap_details.append(f"{player}: {player_data['missing_fields']}")
        if opponent_data.get("missing_fields"):
            gap_details.append(f"{opponent}: {opponent_data['missing_fields']}")
        send_discord("alerts",
            f"⚠️ **RESEARCH GAP** — {dates['ct_time']}\n"
            + "\n".join(gap_details)
            + "\nImpact: Prop capped at YELLOW"
        )

    prop["research_data"] = {
        "player":   player_data,
        "opponent": opponent_data,
        "match_context": {
            "tournament":           tournament,
            "surface":              surface,
            "stage":                prop.get("tournament_stage", 1),
            "round":                prop.get("round", ""),
            "is_grand_slam":        prop.get("is_grand_slam", False),
            "same_day_3set_count":  prop.get("same_day_3set_count", 0),
            "match_date":           dates["match_date"],
        },
        "has_gaps": has_gaps,
    }

    # ── ATTACH V8 PACKET + INJECT CONFIRMED FIELDS ──────────────
    if v8_packet is not None:
        try:
            v8_valid  = v8_validation_agent(player, opponent, stat_type, v8_packet)
            prop["research_data"]["v8_packet"]     = v8_packet
            prop["research_data"]["v8_validation"] = v8_valid

            # Log data quality to alerts if thin
            p_tier = v8_packet["data_quality"]["player_tier"]
            o_tier = v8_packet["data_quality"]["opponent_tier"]
            cap    = v8_valid.get("confidence_cap")

            # Bug fix (this session, confirmed live on a real pick):
            # "MISSING" is returned by TWO genuinely different situations
            # in openclaw_v8_api.py's _data_tier() / _empty_stats() --
            # (1) the player could not be resolved/found in V8 at all
            # (resolution_failed=True), and (2) the player resolved fine
            # and has real data (ranking, hold%, break%, DFs, a real
            # match on file) but simply has zero PRIOR matches on this
            # specific surface -- e.g. a grass debutante (resolution_
            # failed=False). Confirmed live: Radivojevic showed
            # data_tier="MISSING" while her packet contained a real
            # ranking (#148), real hold/break/DF/serve stats, and a full
            # detailed recent match -- she was not missing, she simply
            # had zero prior grass matches on record. Both cases were
            # previously reported identically as "MISSING", misleading
            # anyone reading the alert into thinking V8 had nothing on
            # the player. resolution_failed already exists on both
            # p_stats/o_stats (added in an earlier session's H2H fix)
            # and is read here for the first time to make this alert
            # accurate. Cap and dedup behavior are unchanged -- only the
            # message text is more precise now.
            p_resolution_failed = (v8_packet.get("player", {}) or {}).get("resolution_failed", False)
            o_resolution_failed = (v8_packet.get("opponent", {}) or {}).get("resolution_failed", False)

            def _tier_desc(name, tier, res_failed):
                if tier != "MISSING":
                    return tier
                if res_failed:
                    return "MISSING (player could not be resolved/found in V8 at all)"
                return "MISSING (real player, real data on file -- zero PRIOR matches on this surface specifically, e.g. a surface debut; not a missing player)"

            _match_key = tuple(sorted([player, opponent]))
            if ("MISSING" in [p_tier, o_tier] or "Tier3" in [p_tier, o_tier]) \
               and _match_key not in _v8_quality_alerted_matches:
                _v8_quality_alerted_matches.add(_match_key)
                send_discord("alerts",
                    f"\u26a0\ufe0f **V8 DATA QUALITY ALERT** \u2014 {player} vs {opponent}\n"
                    f"Player tier: {_tier_desc(player, p_tier, p_resolution_failed)}\n"
                    f"Opponent tier: {_tier_desc(opponent, o_tier, o_resolution_failed)}\n"
                    f"Confidence capped at {cap}%.\n"
                    f"Research agent must confirm missing fields via web search."
                )
            log("V8", f"Packet attached \u2014 {player}: {p_tier} | {opponent}: {o_tier} | Cap: {cap}%")

            # Log REQ1 extender status
            p_ext = v8_packet["player"].get("extender_confirmed", False)
            o_ext = v8_packet["opponent"].get("extender_confirmed", False)
            log("V8", f"REQ1 extender \u2014 {player}: {'YES' if p_ext else 'NO'} | {opponent}: {'YES' if o_ext else 'NO'}")

            # ── FEED V8 HOLD% INTO RESEARCH DATA ──────────────────
            # Push confirmed V8 hold% into serve_hold_pct so the
            # scoring agent hold gate sees confirmed data.
            # V8 hold% is Tier 1 grass-specific — always preferred
            # over web search extraction.
            if p_hold_v8 and "player" in prop.get("research_data", {}):
                p_hold_decimal = p_hold_v8 / 100.0
                prop["research_data"]["player"]["serve_hold_pct"] = p_hold_decimal
                prop["research_data"]["player"]["serve_hold_surface"] = surface
                prop["research_data"]["player"]["serve_hold_source"] = "V8 Tennis Stats API (Tier 1)"
                log("V8", f"Hold% injected into research — {player}: {p_hold_v8}% → {p_hold_decimal}")
            if o_hold_v8 and "opponent" in prop.get("research_data", {}):
                o_hold_decimal = o_hold_v8 / 100.0
                prop["research_data"]["opponent"]["serve_hold_pct"] = o_hold_decimal
                prop["research_data"]["opponent"]["serve_hold_surface"] = surface
                prop["research_data"]["opponent"]["serve_hold_source"] = "V8 Tennis Stats API (Tier 1)"
                log("V8", f"Hold% injected into research — {opponent}: {o_hold_v8}% → {o_hold_decimal}")

            # ── FEED V8 RECENT FORM + SURFACE RECORD + RANKING ───
            # v7.9.6 fix: pick card displays recent_form_last3 and
            # surface_record from research_data, but these were only
            # populated via web search extraction. V8 packet has
            # confirmed match results — inject them here so pick card
            # always shows real data, not None when cache is cleared.
            if p_recent and "player" in prop.get("research_data", {}):
                p_form = ["W" if m.get("won") else "L" for m in p_recent[:3]]
                if p_form:
                    prop["research_data"]["player"]["recent_form_last3"] = p_form
                    log("V8", f"Recent form injected — {player}: {p_form}")
            if o_recent and "opponent" in prop.get("research_data", {}):
                o_form = ["W" if m.get("won") else "L" for m in o_recent[:3]]
                if o_form:
                    prop["research_data"]["opponent"]["recent_form_last3"] = o_form
                    log("V8", f"Recent form injected — {opponent}: {o_form}")

            # ── FEED V8 THIS-TOURNAMENT MATCH/3-SET COUNTS (fix, this session) ──
            # Bug fix: this_tournament_matches and this_tournament_3set_count
            # were defined in the web-search extraction schema and defaulted
            # to None, but were NEVER added to v8_confirmed_fields or given
            # an injection path here -- unlike every other V8-owned field
            # (hold%, DF, ranking, break%, etc.), which all get overridden
            # by confirmed V8 data above. Confirmed real: a match log with
            # two genuine 3-set matches this tournament (both recent_matches
            # entries with set_count == 3) still showed
            # this_tournament_3set_count == 1 from the unreconciled web-search
            # guess. V8's own recent_matches array (already fetched, already
            # in memory as p_recent/o_recent) is filtered to this tournament
            # and counted directly here, the same way recent_form_last3 just
            # was above. Tournament match is a case-insensitive substring
            # check since V8 match records use full names (e.g. "Wimbledon -
            # London") while the prop's tournament field is often a short
            # slug (e.g. "wimbledon").
            _tourn_key = (tournament or "").lower().strip()
            if p_recent and "player" in prop.get("research_data", {}):
                p_this_tourn = [m for m in p_recent
                                 if _tourn_key and _tourn_key in (m.get("tournament") or "").lower()]
                prop["research_data"]["player"]["this_tournament_matches"] = len(p_this_tourn)
                prop["research_data"]["player"]["this_tournament_3set_count"] = sum(
                    1 for m in p_this_tourn if m.get("set_count") == 3)
                prop["research_data"]["player"]["this_tournament_source"] = "V8 Tennis Stats API (Tier 1, derived)"
                log("V8", f"Tournament match/3-set counts injected — {player}: "
                    f"{len(p_this_tourn)} matches, "
                    f"{prop['research_data']['player']['this_tournament_3set_count']} went 3 sets")
            if o_recent and "opponent" in prop.get("research_data", {}):
                o_this_tourn = [m for m in o_recent
                                 if _tourn_key and _tourn_key in (m.get("tournament") or "").lower()]
                prop["research_data"]["opponent"]["this_tournament_matches"] = len(o_this_tourn)
                prop["research_data"]["opponent"]["this_tournament_3set_count"] = sum(
                    1 for m in o_this_tourn if m.get("set_count") == 3)
                prop["research_data"]["opponent"]["this_tournament_source"] = "V8 Tennis Stats API (Tier 1, derived)"
                log("V8", f"Tournament match/3-set counts injected — {opponent}: "
                    f"{len(o_this_tourn)} matches, "
                    f"{prop['research_data']['opponent']['this_tournament_3set_count']} went 3 sets")

            # ── FEED REAL PER-PLAYER HOME/AWAY SPLIT (Gate 14) ─────
            # Fix (this session, closes a gap found early in a full-
            # system alignment audit): v8_get_home_away_split() has
            # existed and been imported since v7.9.6, accumulating real
            # per-player home/away results the whole time -- but was
            # never actually CALLED anywhere in this file. Gate 14's
            # home-crowd lean has been running purely on the flat,
            # conservative default the Calibration Notes always
            # documented as a known, temporary limitation ("no per-
            # player home/away split data existed anywhere in the
            # system"). It exists now; this is where it starts being
            # used. min_sample defaults to 5 (matches
            # MIN_PROPOSAL_SAMPLE discipline elsewhere) -- a THIN_SAMPLE
            # or NO_DATA result is real information (not yet trustworthy
            # for this specific player) and is surfaced as such, not
            # silently treated as "no effect."
            p_home_split = v8_get_home_away_split(player)
            if "player" in prop.get("research_data", {}):
                prop["research_data"]["player"]["home_away_split"] = p_home_split
                if p_home_split.get("status") == "CONFIRMED":
                    log("V8", f"Home/away split injected — {player}: "
                        f"home {p_home_split['home_win_pct']}% ({p_home_split['home_sample']}), "
                        f"away {p_home_split['away_win_pct']}% ({p_home_split['away_sample']})")
            o_home_split = v8_get_home_away_split(opponent)
            if "opponent" in prop.get("research_data", {}):
                prop["research_data"]["opponent"]["home_away_split"] = o_home_split
                if o_home_split.get("status") == "CONFIRMED":
                    log("V8", f"Home/away split injected — {opponent}: "
                        f"home {o_home_split['home_win_pct']}% ({o_home_split['home_sample']}), "
                        f"away {o_home_split['away_win_pct']}% ({o_home_split['away_sample']})")

            # Surface record
            if p_surf_rec and p_surf_rec != "N/A" and "player" in prop.get("research_data", {}):
                prop["research_data"]["player"]["surface_record"] = p_surf_rec
                log("V8", f"Surface record injected — {player}: {p_surf_rec}")
            if o_surf_rec and o_surf_rec != "N/A" and "opponent" in prop.get("research_data", {}):
                prop["research_data"]["opponent"]["surface_record"] = o_surf_rec
                log("V8", f"Surface record injected — {opponent}: {o_surf_rec}")

            # v7.9.7 fix (this session): surface_win_pct was never
            # populated anywhere in this file despite V8 always computing
            # the equivalent value as win_pct (get_surface_summary's
            # wins/total calculation). Two different field names for the
            # same underlying concept, with no mapping between them --
            # confirmed real: a player could show win_pct: 59.5 and
            # surface_win_pct: null simultaneously in the same archived
            # pick. Same propagation-gap pattern as the ranking and DF
            # fixes earlier tonight. Schema comment states surface_win_pct
            # is expected on a 0-1 decimal scale, so converting from V8's
            # percentage scale here, same conversion already used for
            # serve_hold_pct.
            if p_win_pct_v8 is not None and "player" in prop.get("research_data", {}):
                prop["research_data"]["player"]["surface_win_pct"] = round(p_win_pct_v8 / 100.0, 3)
                log("V8", f"Surface win% injected — {player}: {p_win_pct_v8}% → {round(p_win_pct_v8/100.0, 3)}")
            if o_win_pct_v8 is not None and "opponent" in prop.get("research_data", {}):
                prop["research_data"]["opponent"]["surface_win_pct"] = round(o_win_pct_v8 / 100.0, 3)
                log("V8", f"Surface win% injected — {opponent}: {o_win_pct_v8}% → {round(o_win_pct_v8/100.0, 3)}")

            # v7.9.7 fix (ranking precedence bug, this session): previously
            # only injected V8's ranking if web search hadn't already found
            # ANY ranking value — meaning a stale/wrong web-search ranking
            # found first would silently block the correct, current V8
            # ranking from ever overwriting it. This was the ONE field in
            # this entire injection block using "web search wins by
            # default" logic while every other V8 field here (hold%, DF,
            # break%, BP save%, first serve%) unconditionally overwrites
            # with V8's value. Confirmed as a real inconsistency, not an
            # intentional design choice — the surrounding code and comments
            # explicitly establish V8 as the Tier 1 authoritative source.
            # Fixed to match that same "V8 always wins" precedence used
            # everywhere else, and now records which source the final
            # ranking actually came from for audit visibility.
            if p_rank_v8 and "player" in prop.get("research_data", {}):
                prop["research_data"]["player"]["ranking"] = p_rank_v8
                prop["research_data"]["player"]["ranking_source"] = "V8 Tennis Stats API (Tier 1)"
                log("V8", f"Ranking injected — {player}: #{p_rank_v8}")
            elif "player" in prop.get("research_data", {}) and prop["research_data"]["player"].get("ranking"):
                prop["research_data"]["player"]["ranking_source"] = "web_search (V8 unavailable for this field)"

            if o_rank_v8 and "opponent" in prop.get("research_data", {}):
                prop["research_data"]["opponent"]["ranking"] = o_rank_v8
                prop["research_data"]["opponent"]["ranking_source"] = "V8 Tennis Stats API (Tier 1)"
                log("V8", f"Ranking injected — {opponent}: #{o_rank_v8}")
            elif "opponent" in prop.get("research_data", {}) and prop["research_data"]["opponent"].get("ranking"):
                prop["research_data"]["opponent"]["ranking_source"] = "web_search (V8 unavailable for this field)"

            # ── FEED V8 AVG DOUBLE FAULTS (v7.9.7) ────────────────
            if p_df_v8 is not None and "player" in prop.get("research_data", {}):
                prop["research_data"]["player"]["avg_double_faults"] = p_df_v8
                prop["research_data"]["player"]["df_source"] = "V8 Tennis Stats API (Tier 1, derived)"
                log("V8", f"Avg DF injected — {player}: {p_df_v8}")
            if o_df_v8 is not None and "opponent" in prop.get("research_data", {}):
                prop["research_data"]["opponent"]["avg_double_faults"] = o_df_v8
                prop["research_data"]["opponent"]["df_source"] = "V8 Tennis Stats API (Tier 1, derived)"
                log("V8", f"Avg DF injected — {opponent}: {o_df_v8}")

            # ── FEED V8 DF SAMPLE SIZE (fix, this session) ────────
            # Bug fix: df_match_count (how many matches avg_double_faults
            # is actually derived from) was present in the raw V8 packet
            # the whole time but never referenced anywhere else in this
            # file -- never injected into research_data, never surfaced
            # in the evaluation prompt, never used in ESS. That meant a
            # DF average from 4 matches (one of them a heavy outlier) and
            # a DF average from 30 matches looked structurally identical
            # to the evaluation model. This does not add any confidence
            # deduction or scoring weight (same "informational only, no
            # invented thresholds" discipline as SPI/ESS/ESG) -- it just
            # makes the existing sample size visible next to the average
            # it belongs to, so the model (and a human reading the
            # reasoning file) can see how many matches back the number.
            p_df_n = p_v8.get("df_match_count") if v8_packet else None
            o_df_n = o_v8.get("df_match_count") if v8_packet else None
            if p_df_n is not None and "player" in prop.get("research_data", {}):
                prop["research_data"]["player"]["df_match_count"] = p_df_n
                log("V8", f"DF sample size injected — {player}: {p_df_n} matches")
            if o_df_n is not None and "opponent" in prop.get("research_data", {}):
                prop["research_data"]["opponent"]["df_match_count"] = o_df_n
                log("V8", f"DF sample size injected — {opponent}: {o_df_n} matches")

            # ── FEED V8 AVG ACES, BP SAVE, BREAK PCT, FIRST SERVE (v7.9.7) ──
            # All four were confirmed present in V8 packet but had no
            # injection path into research_data, so the evaluation model
            # couldn't see them even when V8 had real confirmed numbers.
            # Same pattern as avg_double_faults above. All four are always
            # injected (not just for specific stat types) so the model has
            # the full serve profile available for any prop type that needs it.
            p_avg_aces  = p_v8.get("avg_aces")
            o_avg_aces  = o_v8.get("avg_aces")
            p_bp_save   = p_v8.get("bp_save_pct")
            o_bp_save   = o_v8.get("bp_save_pct")
            p_break_pct = p_v8.get("break_pct")
            o_break_pct = o_v8.get("break_pct")
            p_first_srv = p_v8.get("first_serve")
            o_first_srv = o_v8.get("first_serve")

            if p_avg_aces is not None and "player" in prop.get("research_data", {}):
                prop["research_data"]["player"]["avg_aces_v8"] = p_avg_aces
                prop["research_data"]["player"]["ace_avg_this_surface"] = p_avg_aces
                prop["research_data"]["player"]["avg_aces_source"] = "V8 Tennis Stats API (Tier 1)"
                log("V8", f"Avg aces injected — {player}: {p_avg_aces}")
            if o_avg_aces is not None and "opponent" in prop.get("research_data", {}):
                prop["research_data"]["opponent"]["avg_aces_v8"] = o_avg_aces
                prop["research_data"]["opponent"]["ace_avg_this_surface"] = o_avg_aces
                prop["research_data"]["opponent"]["avg_aces_source"] = "V8 Tennis Stats API (Tier 1)"
                log("V8", f"Avg aces injected — {opponent}: {o_avg_aces}")

            if p_bp_save is not None and "player" in prop.get("research_data", {}):
                prop["research_data"]["player"]["bp_save_pct_v8"] = p_bp_save
                prop["research_data"]["player"]["bp_save_pct_source"] = "V8 Tennis Stats API (Tier 1)"
                log("V8", f"BP save % injected — {player}: {p_bp_save}%")
            if o_bp_save is not None and "opponent" in prop.get("research_data", {}):
                prop["research_data"]["opponent"]["bp_save_pct_v8"] = o_bp_save
                prop["research_data"]["opponent"]["bp_save_pct_source"] = "V8 Tennis Stats API (Tier 1)"
                log("V8", f"BP save % injected — {opponent}: {o_bp_save}%")

            if p_break_pct is not None and "player" in prop.get("research_data", {}):
                prop["research_data"]["player"]["break_pct_v8"] = p_break_pct
                prop["research_data"]["player"]["break_pct_source"] = "V8 Tennis Stats API (Tier 1)"
                log("V8", f"Break % injected — {player}: {p_break_pct}%")
            if o_break_pct is not None and "opponent" in prop.get("research_data", {}):
                prop["research_data"]["opponent"]["break_pct_v8"] = o_break_pct
                prop["research_data"]["opponent"]["break_pct_source"] = "V8 Tennis Stats API (Tier 1)"
                log("V8", f"Break % injected — {opponent}: {o_break_pct}%")

            if p_first_srv is not None and "player" in prop.get("research_data", {}):
                prop["research_data"]["player"]["first_serve_pct_v8"] = p_first_srv
                prop["research_data"]["player"]["first_serve_pct_source"] = "V8 Tennis Stats API (Tier 1)"
                log("V8", f"First serve % injected — {player}: {p_first_srv}%")
            if o_first_srv is not None and "opponent" in prop.get("research_data", {}):
                prop["research_data"]["opponent"]["first_serve_pct_v8"] = o_first_srv
                prop["research_data"]["opponent"]["first_serve_pct_source"] = "V8 Tennis Stats API (Tier 1)"
                log("V8", f"First serve % injected — {opponent}: {o_first_srv}%")
            # ─────────────────────────────────────────────────────

            # ── SERVICE PRESSURE INDEX (SPI) — EXPERIMENTAL, INFORMATIONAL ONLY ──
            # Locked spec (this session, operator + reviewer agreed):
            # SPI = hold_pct - break_pct, per player, using existing
            # confirmed V8 fields only (no new data source). Computed,
            # stored, and displayed — explicitly NOT wired into scoring,
            # confidence, or direction anywhere. Same phased discipline
            # as Moneyline v1.4's provisional thresholds: observe real
            # outcomes first, decide on scoring influence later, only
            # if the archived results actually show a real relationship
            # between SPI and match/prop outcomes (more aces, fewer
            # breaks, different TG distributions, etc. for high-SPI
            # matches). Until that's demonstrated, this field carries
            # zero weight anywhere in the pipeline.
            #
            # Classification bands are deliberately left undefined here
            # beyond a simple three-way split — no "high"/"low" numeric
            # thresholds are locked, per the explicit instruction not to
            # invent unvalidated cutoffs the way M1/M2 were avoided for
            # moneyline. SERVE_LEANING/RETURN_LEANING/MIXED is a
            # provisional, coarse label only.
            if p_hold_v8 is not None and p_break_pct is not None and "player" in prop.get("research_data", {}):
                p_spi = round(p_hold_v8 - p_break_pct, 1)
                prop["research_data"]["player"]["spi"] = p_spi
                prop["research_data"]["player"]["spi_source"] = "derived_v8"
                prop["research_data"]["player"]["spi_components"] = {
                    "hold_pct": p_hold_v8, "break_pct": p_break_pct,
                }
                log("V8", f"SPI computed — {player}: {p_spi} (hold {p_hold_v8}% - break {p_break_pct}%)")

            if o_hold_v8 is not None and o_break_pct is not None and "opponent" in prop.get("research_data", {}):
                o_spi = round(o_hold_v8 - o_break_pct, 1)
                prop["research_data"]["opponent"]["spi"] = o_spi
                prop["research_data"]["opponent"]["spi_source"] = "derived_v8"
                prop["research_data"]["opponent"]["spi_components"] = {
                    "hold_pct": o_hold_v8, "break_pct": o_break_pct,
                }
                log("V8", f"SPI computed — {opponent}: {o_spi} (hold {o_hold_v8}% - break {o_break_pct}%)")

            # Match-level classification — provisional, coarse, informational
            p_spi_val = prop["research_data"].get("player", {}).get("spi")
            o_spi_val = prop["research_data"].get("opponent", {}).get("spi")
            if p_spi_val is not None and o_spi_val is not None:
                # No numeric thresholds locked — simple sign-based split
                # only, per explicit instruction to avoid inventing
                # unvalidated cutoffs. Both positive = both hold more
                # than they get broken = serve-leaning. Both negative =
                # return-leaning. Mixed signs = mixed.
                if p_spi_val > 0 and o_spi_val > 0:
                    match_classification = "SERVE_LEANING"
                elif p_spi_val < 0 and o_spi_val < 0:
                    match_classification = "RETURN_LEANING"
                else:
                    match_classification = "MIXED"
                prop["research_data"]["match_context"]["match_spi"] = {
                    "player_spi":    p_spi_val,
                    "opponent_spi":  o_spi_val,
                    "classification": match_classification,
                }
                log("V8", f"Match SPI classification — {player} vs {opponent}: "
                    f"{match_classification} (player {p_spi_val}, opponent {o_spi_val})")
            # ─────────────────────────────────────────────────────

            _compute_expected_service_games(prop, p_v8, o_v8)

        except Exception as _v8e:
            log("V8", f"Packet injection failed (non-fatal): {_v8e}")
    # ────────────────────────────────────────────────────────────

    _compute_evidence_strength_score(prop)

    return prop


# ================================================================
# EVALUATION AGENT
# ================================================================

def load_master_prompt():
    base = "/path/to/home/openclaw"

    # Primary files in load order — handoff supersedes V71
    # Bug fix (this session): the loader required one exact literal
    # filename ("DEMETRIUS_v7.9.6_HANDOFF.txt", dots) and silently
    # skipped that file entirely if it didn't match -- confirmed live
    # this session: the real file on disk was named with underscores
    # ("DEMETRIUS_v7_9_6_HANDOFF.txt") and was never being found by
    # either candidate path, meaning the handoff had a real chance of
    # silently dropping out of combined_parts with no error, no log
    # warning distinguishing it from "file genuinely doesn't exist."
    # Now checks both dot and underscore variants for the two files
    # most likely to drift (handoff version number, V71) so a rename
    # mismatch like this can't silently recur.
    file_order = [
        (base + "DEMETRIUS_v7.9.6_HANDOFF.txt",          "Handoff v7.9.6"),
        (base + "DEMETRIUS_v7_9_6_HANDOFF.txt",          "Handoff v7.9.6 (underscore variant)"),
        (base + "DEMETRIUS_PROPS_DECISION_TREE.txt",      "Decision Tree"),
        (base + "DEMETRIUS_SESSION_START_CHECK.txt",       "Session Start Check"),
        (base + "OPENCLAW_MASTER_PROMPT_V71.txt",         "Master Prompt V71"),
        ("DEMETRIUS_v7.9.6_HANDOFF.txt",                  "Handoff v7.9.6 (local)"),
        ("OPENCLAW_MASTER_PROMPT_V71.txt",                "Master Prompt V71 (local)"),
    ]

    # Try to load all three primary files and combine them
    # Each file has two candidates: absolute path first, local fallback second
    primary_file_candidates = [
        [base + "DEMETRIUS_v7.9.6_HANDOFF.txt",
         base + "DEMETRIUS_v7_9_6_HANDOFF.txt",
         "DEMETRIUS_v7.9.6_HANDOFF.txt",
         "DEMETRIUS_v7_9_6_HANDOFF.txt"],
        [base + "DEMETRIUS_PROPS_DECISION_TREE.txt",     "DEMETRIUS_PROPS_DECISION_TREE.txt"],
        [base + "DEMETRIUS_SESSION_START_CHECK.txt",      "DEMETRIUS_SESSION_START_CHECK.txt"],
    ]

    combined_parts = []
    for candidates in primary_file_candidates:
        for path in candidates:
            if Path(path).exists():
                with open(path) as f:
                    text = f.read()
                    combined_parts.append(text)
                    log("Prompt", f"Loaded: {Path(path).name} ({len(text)} chars)")
                break  # found this file, move to next
        else:
            # Bug fix (this session): previously silent -- if NONE of a
            # file's candidate names existed, that document just quietly
            # never made it into combined_parts, with no distinguishable
            # log line telling you a whole document was missing versus
            # everything working normally. Now logs it loudly.
            log("Prompt", f"WARNING — none of these candidate filenames found, "
                f"this document is MISSING from the live prompt: {candidates}")

    if combined_parts:
        combined = "\n\n" + ("="*80) + "\n\n".join(combined_parts)
        log("Prompt", f"Master prompt assembled: {len(combined)} chars from {len(combined_parts)} files")
        return combined

    # Fallback: try V71 alone
    for path, label in file_order:
        if Path(path).exists():
            with open(path) as f:
                text = f.read()
                log("Prompt", f"Fallback prompt loaded: {label} ({len(text)} chars)")
                return text

    log("Prompt", "WARNING — No prompt files found. Evaluations will be degraded.")
    return ("You are OpenClaw. Evaluate tennis props using 12 checkpoints. "
            "Research both players first. Every checkpoint produces written output.")

MASTER_PROMPT = load_master_prompt()

def build_evaluation_content(prop):
    rd              = prop.get("research_data", {})
    player_data     = rd.get("player", {})
    opponent_data   = rd.get("opponent", {})
    context         = rd.get("match_context", {})
    compliance_flags= prop.get("compliance_flags", [])
    format_contam   = prop.get("format_contamination", False)
    surface         = prop.get("surface", "hard")

    content = f"""================================================================
DEMETRIUS SYSTEM — CORE BELIEFS (read before every evaluation)
================================================================
TWO PILLARS — NON-NEGOTIABLE:

PILLAR 1 — FAIR EVALUATION
Every prop evaluated using confirmed real data about BOTH players.
No direction assumed by default. No bias toward favorite or underdog.
Rules validate thinking — they never replace it.

PILLAR 2 — RESEARCH FIRST
Before evaluating anything, confirm:
  • Current rankings (from live search data provided below)
  • Surface-specific win records this season
  • Recent form (last 3-5 matches with scores)
  • Tournament stage, set counts
  • Serve hold % (BP and TG props)
  • Ace averages on surface (aces props)
  • Head-to-head history on surface
If any of these are missing from the research block below, name them
explicitly at Checkpoint 1 and cap confidence accordingly.

THE CORE PROMISE:
The pick must win in MOST realistic outcomes — not one script.
"The number shows the danger — the matchup decides the direction —
the score paths prove the pick."

HOW IT WORKS:
  • 12-checkpoint evaluation (run every checkpoint, produce written output)
  • Gate system screens unfavorable conditions before direction is set
  • 31 active rules validate thinking — do not skip rules to reach a conclusion
  • Confidence caps at YELLOW if data is missing — never inflate to GREEN on thin data
  • Direction must be challenged by rules before the final call is locked
================================================================

PROP TO EVALUATE:
Player: {prop.get('player')} | Opponent: {prop.get('opponent')}
Stat: {prop.get('stat_type')} | Line: {prop.get('line')}
Tournament: {context.get('tournament')} | Surface: {context.get('surface')}
Stage: {context.get('stage')} | Round: {context.get('round') or 'UNCONFIRMED — do not infer or guess the round; state plainly that round is unconfirmed rather than reasoning through match history to estimate it'}
Grand Slam (Best of 5): {context.get('is_grand_slam', False)}
Same-day 3-set count: {"UNCONFIRMED — this field is not currently computed from real tournament-wide match data anywhere in the pipeline; it is always 0 by construction, not a genuine observation. Do NOT treat it as evidence of a quiet or heavy match day." if not context.get("same_day_3set_count") else context.get("same_day_3set_count")}
Prop type: {prop.get('prop_type_classified', 'standard').upper()}
{f"⚠️ GOBLIN 😈 PROP — MORE DIRECTION ONLY. Evaluate MORE direction exclusively. GREEN minimum required before slip inclusion. Approval required." if prop.get('prop_type_classified') == 'goblin' else ""}
{f"⚠️ RED DEMON 🔴😈 PROP — MORE DIRECTION ONLY. Higher payout because book prices MORE as unlikely. Evaluate MORE on structural merit ONLY. Payout never influences evaluation. GREEN minimum required. Stronger structural justification required than Goblin. Approval required." if prop.get('prop_type_classified') == 'red_demon' else ""}
Direction locked: {prop.get('direction_locked') or 'No — evaluate both directions'}

EVIDENCE STRENGTH SCORE (ESS, EXPERIMENTAL — informational only, do NOT use to set direction or confidence, do NOT treat as a confidence cap):
ESS: {prop.get('ess_score', 'NOT COMPUTED')} ({prop.get('ess_band', 'UNKNOWN')})
Critical fields missing for this stat type: {prop.get('critical_data_missing', [])}

EXPECTED SERVICE GAMES (ESG, EXPERIMENTAL — informational only, opportunity-volume estimate, do NOT use to set direction, confidence, or any deduction):
ESG: {context.get('expected_service_games', 'NOT COMPUTED')} ({context.get('esg_band', 'UNKNOWN')})

FORMAT CONTAMINATION FLAG: {format_contam}
{'⚠️ GRAND SLAM PROP: Use only best-of-5 format results in average calculations.' if format_contam else ''}

RESEARCH — PLAYER ({prop.get('player')}):
Ranking: #{player_data.get('ranking', 'NOT FOUND')} (source: {player_data.get('ranking_source', 'unknown')})
Surface record: {player_data.get('surface_record', 'NOT FOUND')}
Recent form last 3: {player_data.get('recent_form_last3', 'NOT FOUND')}
Recent match scores: {player_data.get('recent_match_scores') or 'NOT FOUND — REQ1 extender detection requires actual scorelines'}
Primary surface: {player_data.get('primary_surface', 'NOT FOUND')}
Tournament 3-set count: {player_data.get('this_tournament_3set_count', 'NOT FOUND')}
Serve hold %: {player_data.get('serve_hold_pct', 'NOT FOUND')} (surface: {player_data.get('serve_hold_surface', 'unknown')})
Hold surface note: {
    'GRASS-SPECIFIC — use as-is' if player_data.get('serve_hold_surface') == surface else
    'ALL-SURFACE AVERAGE — apply surface adjustment if primary_surface != match surface' if player_data.get('serve_hold_surface') == 'all' else
    'SURFACE UNKNOWN — treat as Tier 3'
}
Primary surface: {player_data.get('primary_surface', 'NOT FOUND')}
Surface adjustment note: {
    'Clay primary on grass — estimated grass hold = hold_pct x 0.92' if player_data.get('primary_surface') == 'clay' and surface == 'grass' else
    'Hard primary on grass — estimated grass hold = hold_pct x 0.97' if player_data.get('primary_surface') == 'hard' and surface == 'grass' else
    'Grass primary on grass — use hold rate as-is' if player_data.get('primary_surface') == 'grass' and surface == 'grass' else
    'No adjustment needed or surface match confirmed'
}
Ace avg this surface: {player_data.get('ace_avg_this_surface', 'NOT FOUND')}
GW when winning: {player_data.get('typical_gw_when_winning', 'NOT FOUND')}
Avg double faults (V8, Tier 1): {player_data.get('avg_double_faults', 'NOT FOUND')} (from {player_data.get('df_match_count', 'an unknown number of')} matches — IMPORTANT: 5 is the V8 API window maximum, identical for every player on every prop; a count of 5 means "the 5 most recent matches", NOT thin data. Do NOT apply a small-sample confidence deduction merely because the count is 5 — a deduction that fires on every prop identically carries no information. Deduct for sample instability ONLY if the count is below 5, or if the underlying per-match figures visibly show one outlier match dominating the average)
BP save % (V8, Tier 1): {player_data.get('bp_save_pct_v8', 'NOT FOUND')}
Break % (V8, Tier 1): {player_data.get('break_pct_v8', 'NOT FOUND')}
First serve % (V8, Tier 1): {player_data.get('first_serve_pct_v8', 'NOT FOUND')}
Service Pressure Index (SPI, EXPERIMENTAL — informational only, do NOT use to set direction or confidence): {player_data.get('spi', 'NOT FOUND')}
Home/away split (Gate 14 reference — real accumulated data, this session): {
    f"HOME {player_data.get('home_away_split',{}).get('home_win_pct')}% ({player_data.get('home_away_split',{}).get('home_sample')} matches) / AWAY {player_data.get('home_away_split',{}).get('away_win_pct')}% ({player_data.get('home_away_split',{}).get('away_sample')} matches)"
    if player_data.get('home_away_split',{}).get('status') == 'CONFIRMED' else
    f"THIN SAMPLE — {player_data.get('home_away_split',{}).get('home_sample','?')} home / {player_data.get('home_away_split',{}).get('away_sample','?')} away, below the 5-match minimum each side — real data, not yet trustworthy, do not use for a per-player lean"
    if player_data.get('home_away_split',{}).get('status') == 'THIN_SAMPLE' else
    "NO DATA — apply Gate 14's flat default only, no per-player lean available for this player yet"
}
Missing: {player_data.get('missing_fields', [])}

RESEARCH — OPPONENT ({prop.get('opponent')}):
Ranking: #{opponent_data.get('ranking', 'NOT FOUND')} (source: {opponent_data.get('ranking_source', 'unknown')})
Surface record: {opponent_data.get('surface_record', 'NOT FOUND')}
Recent form last 3: {opponent_data.get('recent_form_last3', 'NOT FOUND')}
Recent match scores: {opponent_data.get('recent_match_scores') or 'NOT FOUND — REQ1 extender detection requires actual scorelines'}
Primary surface: {opponent_data.get('primary_surface', 'NOT FOUND')}
Tournament 3-set count: {opponent_data.get('this_tournament_3set_count', 'NOT FOUND')}
Serve hold %: {opponent_data.get('serve_hold_pct', 'NOT FOUND')} (surface: {opponent_data.get('serve_hold_surface', 'unknown')})
Hold surface note: {
    'GRASS-SPECIFIC — use as-is' if opponent_data.get('serve_hold_surface') == surface else
    'ALL-SURFACE AVERAGE — apply surface adjustment if primary_surface != match surface' if opponent_data.get('serve_hold_surface') == 'all' else
    'SURFACE UNKNOWN — treat as Tier 3'
}
Primary surface: {opponent_data.get('primary_surface', 'NOT FOUND')}
Surface adjustment note: {
    'Clay primary on grass — estimated grass hold = hold_pct x 0.92' if opponent_data.get('primary_surface') == 'clay' and surface == 'grass' else
    'Hard primary on grass — estimated grass hold = hold_pct x 0.97' if opponent_data.get('primary_surface') == 'hard' and surface == 'grass' else
    'Grass primary on grass — use hold rate as-is' if opponent_data.get('primary_surface') == 'grass' and surface == 'grass' else
    'No adjustment needed or surface match confirmed'
}
Avg double faults (V8, Tier 1): {opponent_data.get('avg_double_faults', 'NOT FOUND')} (from {opponent_data.get('df_match_count', 'an unknown number of')} matches — IMPORTANT: 5 is the V8 API window maximum, identical for every player on every prop; a count of 5 means "the 5 most recent matches", NOT thin data. Do NOT apply a small-sample confidence deduction merely because the count is 5 — a deduction that fires on every prop identically carries no information. Deduct for sample instability ONLY if the count is below 5, or if the underlying per-match figures visibly show one outlier match dominating the average)
BP save % (V8, Tier 1): {opponent_data.get('bp_save_pct_v8', 'NOT FOUND')}
Break % (V8, Tier 1): {opponent_data.get('break_pct_v8', 'NOT FOUND')}
First serve % (V8, Tier 1): {opponent_data.get('first_serve_pct_v8', 'NOT FOUND')}
Service Pressure Index (SPI, EXPERIMENTAL — informational only, do NOT use to set direction or confidence): {opponent_data.get('spi', 'NOT FOUND')}
Home/away split (Gate 14 reference — real accumulated data, this session): {
    f"HOME {opponent_data.get('home_away_split',{}).get('home_win_pct')}% ({opponent_data.get('home_away_split',{}).get('home_sample')} matches) / AWAY {opponent_data.get('home_away_split',{}).get('away_win_pct')}% ({opponent_data.get('home_away_split',{}).get('away_sample')} matches)"
    if opponent_data.get('home_away_split',{}).get('status') == 'CONFIRMED' else
    f"THIN SAMPLE — {opponent_data.get('home_away_split',{}).get('home_sample','?')} home / {opponent_data.get('home_away_split',{}).get('away_sample','?')} away, below the 5-match minimum each side — real data, not yet trustworthy, do not use for a per-player lean"
    if opponent_data.get('home_away_split',{}).get('status') == 'THIN_SAMPLE' else
    "NO DATA — apply Gate 14's flat default only, no per-player lean available for this player yet"
}
Missing: {opponent_data.get('missing_fields', [])}"""

    if compliance_flags:
        content += "\n\nCOMPLIANCE FLAGS:\n"
        for f in compliance_flags:
            content += f"VIOLATION — {f.get('rule')}: {f.get('violation')} | FIX: {f.get('required')}\n"
        content += (
            "\nFor EACH flag above, resolve it using this exact structure — do NOT "
            "defer the evidence to a later checkpoint, even if that checkpoint will "
            "also cover it in more detail:\n"
            "  1. State the rule being addressed.\n"
            "  2. Show the actual evidence/numbers immediately, inline, right here — "
            "not a promise that it will appear later.\n"
            "  3. State the conclusion this evidence supports, immediately.\n"
            "  4. THEN note that the relevant checkpoint below will provide the full, "
            "detailed treatment — the checkpoint structure stays intact, but the "
            "compliance auditor needs to see real evidence and a real conclusion at "
            "the point of resolution, not a forward reference.\n"
            "Example of what's REQUIRED: 'Rule 4 Resolution: Struff break% = 16.8%, "
            "Sinner BP save% = 72.7%. Result: return pressure is limited, lopsidedness "
            "confirmed. Checkpoint D below will provide full dominance scoring.'\n"
            "Example of what's NOT sufficient: 'Rule 4 will be addressed at Checkpoint D.'"
        )

    # Direction-drivers requirement (this session, approved): direction
    # previously came out of the evaluation with no structured statement
    # of WHY -- making direction integrity unauditable. With this block
    # required, the compliance auditor and post-mortems can check
    # drivers-vs-direction consistency mechanically, catching the
    # "everything points LESS, MORE chosen anyway" class.
    content += (
        "\n\nMANDATORY OUTPUT SECTION — DIRECTION DRIVERS: after your final "
        "direction call, list exactly the top 3 evidence items that drove "
        "the MORE/LESS decision, strongest first, each tied to its source "
        "(V8 field, checkpoint, or confirmed web fact). Each driver must "
        "genuinely support the direction chosen — if you cannot produce 3 "
        "drivers that support your direction, the direction is not "
        "adequately supported and your confidence must reflect that."
    )

    if prop.get("is_re_evaluation"):
        content += "\n\nNOTE: RE-EVALUATION — address every compliance flag above using the structure specified."

    # ── V8 TENNIS STATS API DATA BLOCK ───────────────────────────
    # Injects confirmed historical stats into evaluation prompt.
    # These are CONFIRMED STRUCTURAL FACTS that SET direction.
    # Narratives may only adjust confidence — never set direction.
    v8_packet = rd.get("v8_packet")
    v8_valid  = rd.get("v8_validation", {})
    if v8_packet and _V8_AVAILABLE:
        try:
            hist_block = format_packet_for_eval(
                prop.get("player", ""), prop.get("opponent", ""), v8_packet
            )
            cap      = v8_valid.get("confidence_cap")
            warnings = v8_valid.get("warnings", [])
            missing  = v8_valid.get("missing", [])

            content += f"\n\n{hist_block}"

            if cap:
                content += (
                    f"\n\nV8 CONFIDENCE CAP: {cap}% maximum based on data tier."
                    f"\nPlayer tier: {v8_valid.get('player_tier')} | "
                    f"Opponent tier: {v8_valid.get('opponent_tier')}"
                )
            if warnings:
                content += "\n\nV8 WARNINGS (apply these caps):\n"
                content += "\n".join(f"  ⚠️ {w}" for w in warnings)
            if missing:
                content += "\n\nV8 MISSING DATA (research agent must confirm via web):\n"
                content += "\n".join(f"  ❌ {m}" for m in missing)

        except Exception as _e:
            content += f"\n\n[V8 data unavailable: {_e}]"
    # ────────────────────────────────────────────────────────────

    # ── GATE 11-B FORMAT BLOCK ───────────────────────────────────
    # Injects confirmed match format before 12 checkpoints run.
    # Prevents evaluation agent from assuming wrong format.
    if prop.get("format_label"):
        content += f"\n\nFORMAT LOCKED: {prop['format_label']}"
    if prop.get("format_warning"):
        content += f"\n{prop['format_warning']}"
    # ────────────────────────────────────────────────────────────

    # ── 5D: WIMBLEDON DAY 1 CORRECTIONS — PROMPT INJECTION ──────
    _rd        = prop.get("research_data", {})
    _p_data    = _rd.get("player", {})
    _o_data    = _rd.get("opponent", {})
    _stat_type = prop.get("stat_type", "")
    _ptype     = prop.get("prop_type_classified", "standard")

    try:
        _p_rank = int(str(_p_data.get("ranking","0")).replace("#","").strip() or "0")
        _o_rank = int(str(_o_data.get("ranking","0")).replace("#","").strip() or "0")
        _gap    = abs(_p_rank - _o_rank) if _p_rank and _o_rank else 0
    except Exception:
        _p_rank = _o_rank = _gap = 0

    # Correction 24 — Category 9 Finding 3 (approved, Outcome A: code
    # drifted, LOCKED rule is correct). The locked June 29 spec fires at
    # "ranking gap >= ~100 spots AND opponent is ITF/qualifier/wildcard".
    # This trigger had widened to >=50 OR cross-tier asymmetry with no
    # opponent-status condition and no documented amendment -- injecting
    # class-gap framing onto ordinary 50-90-spot tour-level matchups.
    # Numeric trigger tightened to the locked >=100; the opponent-status
    # condition (not machine-readable from rankings alone) is enforced
    # inside the injected instruction text below.
    # Duplication fix (this session, confirmed real by comparing the
    # actual injected text, not just suspected): Correction 24 and the
    # Competition-Context Filter were TWO SEPARATE blocks injected
    # back-to-back whenever gap>=100, asking the model the functionally
    # IDENTICAL three questions (who caused the struggles, is today's
    # opponent comparable level/style) under two different names in the
    # same prompt. This is exactly the "rank gap feeds three rules, one
    # fact" echo risk the Rule Roadmap's duplication watchlist flagged
    # as its highest-priority candidate -- confirmed here, not merely
    # suspected. Merged into ONE injected block: the Competition-Context
    # Filter's Step 1/2/3 structure runs every time (unchanged,
    # always-active), and Correction 24's LOCKED class-gap-specific
    # conclusion (competition hierarchy, "direction = class gap",
    # volatility-cannot-flip) is appended to that SAME block only when
    # gap>=100 -- one instruction, one place the model reasons through
    # it, instead of the same question asked twice under different
    # names. The third leg of the same roadmap concern (dominance
    # classification using rank gap directly) is separately covered by
    # the new Rule 9 compliance check added this session -- the
    # decision tree already bans that explicitly ("if the dominance
    # read leans on the ranking gap, it is invalid").
    content += """

COMPETITION-CONTEXT FILTER (ALWAYS ACTIVE)
When applying surface struggle narratives, confirm:
Step 1: Who specifically caused the struggles? Their ranking and style?
Step 2: Is today's opponent at a comparable level AND similar playing style?
Step 3: Apply if similar. REJECT if materially lower level or different style.
Example: Top-10 losses on grass do NOT predict vulnerability vs a qualifier.
Direction comes from the STRUCTURAL MATCHUP between these two specific players."""

    if _gap >= 100:
        content += """

CLASS-GAP CONCLUSION (locked spec, applies when the ranking gap is
>=100 spots AND the lower-ranked player is ITF/qualifier/wildcard-level
opposition -- if today's opponent is an established tour-level player
despite the ranking gap, state that and do NOT apply this conclusion):
If Step 1 above found the struggles came from stronger/different
opponents than today's -> those form stats are NOT predictive.
Direction = class gap (LESS/UNDER). Volatility flags may lower
confidence but CANNOT flip LESS→MORE.
Competition hierarchy: Grand Slam > Masters 1000 > 500 > 250 > Challenger > ITF
If struggles came from level N and today's opponent is N-2 lower → reject narrative."""

    # Volatility Cap Rule — always active
    content += """

VOLATILITY CAP RULE (ALWAYS ACTIVE)
Volatility flags / upset history / "complicated surface" / poor recent form:
→ Affect CONFIDENCE LEVEL ONLY. NEVER set direction toward MORE/length.
→ LOPSIDED + VOLATILE = cap at YELLOW max. Direction still from structural facts.
→ If class gap says LESS → VOLATILE lowers confidence, does NOT flip to MORE.
→ Volatility = less predictable. A volatile player can also produce a blowout."""

    # Source Weight Discipline — always active
    content += """

SOURCE WEIGHT DISCIPLINE (ALWAYS ACTIVE)
Narrative predictions ("X in three sets," "could go long," "complicated surface"):
→ Context only. Max -1% to -2% confidence adjustment.
→ CANNOT set or flip direction. CANNOT override structural data.
Direction must come from: confirmed hold%, break%, surface record, H2H, model %.
"Could go three" from a preview = context. It is NEVER a direction signal."""

    # Total Sets MORE Gate — fires on total_sets props
    if _stat_type == "total_sets":
        _ts_extra = ""
        if _ptype == "red_demon":
            _ts_extra = ("RED DEMON: Book prices MORE as unlikely. "
                        "Require BOTH Condition 1 AND confirmed extender evidence.")
        content += f"""

TOTAL SETS MORE ELIGIBILITY GATE (ACTIVE — total_sets prop)
Prop: {_ptype.upper()}. {_ts_extra}
Total Sets MORE VALID only when:
CONDITION 1: Genuine coin-flip (model ≤60%, OR ranking gap <10 with similar records)
CONDITION 2: Confirmed quantitative model explicitly flags underdog winning a set
             (Dimers/Stats Insider model % — NOT a preview article or VOLATILE tag)
REJECTED when: Favorite is 70%+ without model-backed underdog flag.
REJECTED when: Only support is narrative or VOLATILE tag.
DEFAULT for LOPSIDED (70%+): Total Sets UNDER is structural base case."""
    # ────────────────────────────────────────────────────────────

    content += """

Run all 12 checkpoints. Every checkpoint produces written output.

MANDATORY STRUCTURED OUTPUT — append this block at the very end of your evaluation,
after the FINAL OUTPUT section. Fill in YES or NO for each:

PROVEN_COMPETITIVE_REQS:
REQ1_EXTENDERS: [YES if both players have confirmed recent matches going long / 3-set history. NO if form data missing or matches short]
REQ2_TWO_SIDED_HOLD: [YES if both players have confirmed hold % from a real source. NO if either is unconfirmed]
REQ3_LINE_BEATABLE: [YES if realistic score paths weighted by behavior show MORE clearing the line. NO if coin-flip or LESS-leaning]
REQ4_NOT_THIN_DATA: [YES if both players have ranking + surface record + hold data confirmed. NO if thin data / debut / qualifier]
RESULT: [count]/4 MET → [GREEN ELIGIBLE if 4/4, else YELLOW MAX]

ECL CERTIFICATION (Evidence Chain Lock, mandatory — added this session, closes
the gap where a checkpoint gets scored, self-acknowledged as not the primary
driver, and then silently never used: confirmed live on the Zverev/Fery DF 2.5
card, where dominance scored 5/5 HARD and never appeared in DIRECTION DRIVERS,
and on the Waltert/Romero Gormaz DF card, where direction and confidence
initially drew from two different, uncross-referenced evidence sets before a
re-evaluation caught and fixed it. This block makes that check explicit and
required on every card, not something a re-evaluation has to catch after the
fact). Append immediately after DIRECTION DRIVERS above. A checkmark with no
supporting detail is invalid — show the actual evidence for each line, not
just the word PASSED. If any stage fails, every stage after it is NOT REACHED,
never PASSED — do not certify a number you were not entitled to generate:

ECL CERTIFICATION:
Evidence Verification: [PASSED — every value cited as "confirmed" appears in
  the V8/ground-truth data actually provided above, none inferred or assumed.
  OR FAILED — name the value cited as confirmed that is not actually present.]
Rule Status Resolution: [PASSED — every rule/checkpoint run is either applied
  with real evidence, or stated as not applicable, with nothing marked passed
  that wasn't actually checked. OR FAILED — name what was marked passed
  without being checked.]
Direction Drivers Integration: [PASSED — every checkpoint that scored a
  strong result (HARD dominance, 4-5/5 traits, a confirmed hold/break/DF
  number) either appears in DIRECTION DRIVERS above or was explicitly
  redirected as context-only with the real driver named instead — nothing
  strong was scored and left unmentioned. OR FAILED — name the checkpoint
  that was scored and silently dropped.]
Confidence Traceability: [PASSED — every deduction/addition in the confidence
  math traces to a fact in DIRECTION DRIVERS, or is explicitly disclosed as a
  separate axis not used to set direction. OR FAILED — name the deduction
  that introduces a fact absent from DIRECTION DRIVERS with no disclosure.]
Reconstructability: [PASSED — show the actual arithmetic: ceiling minus each
  named deduction equals the final %, using only facts shown above. OR
  NOT REACHED if an earlier stage failed.]
RESULT: [EVIDENCE CHAIN INTACT if all five PASSED, else EVIDENCE CHAIN BROKEN]
"""
    return content

def evaluation_agent(prop, _precomputed_eval_text=None,
                     _precomputed_stop_reason=None, _precomputed_usage=None):
    """
    _precomputed_eval_text / _precomputed_stop_reason / _precomputed_usage:
    Batch API path only (v1.1, 2026-07-23). When _precomputed_eval_text
    is provided, the call_model() step is skipped entirely and the
    supplied text is used as the model response. All downstream logic
    (extractions, ECL check, rule calibration, ATE rule_activations and
    checkpoint_stamps) runs identically to the live path. Zero behavior
    change when these params are None (default) — every existing call
    site is unaffected.
    ATE note: the per-call trace_id is not available from batch results
    (no synchronous API response to extract it from), so _eval_trace_id_box
    stays empty on the batch path. ate.capture_rule_activations /
    ate.capture_checkpoint_stamps still run but with trace_id=None,
    which both functions already handle gracefully (documented as
    'no linkage available, not an error').
    """
    log("Evaluation",
        f"Evaluating: {prop.get('player')} {prop.get('stat_type')} {prop.get('line')}")
    content = build_evaluation_content(prop)

    log("Evaluation", f"MASTER PROMPT SIZE: {len(MASTER_PROMPT)}")
    log("Evaluation", f"EVALUATION CONTENT SIZE: {len(content)}")
    log("Evaluation", f"TOTAL PAYLOAD SIZE: {len(MASTER_PROMPT) + len(content)}")

    # Bug fix (this session, confirmed live on a real pick): 10000 was
    # confirmed genuinely insufficient -- Total Games Won hit exactly
    # 10000/10000 output tokens and truncated before reaching a
    # conclusion, no pick generated. Total Games/Games Won props require
    # more output than DF/Aces props (exhaustive path-counting across a
    # wider outcome range per the "list ALL realistic outcomes" rule),
    # which is very likely why this specific prop type is the one
    # hitting the ceiling -- real DF evaluations tonight completed at
    # ~6000-6300 tokens, well under budget. 16000 gives real headroom
    # above the confirmed failure point without being unbounded.
    EVAL_MAX_TOKENS = 16000
    _eval_trace_id_box = []

    if _precomputed_eval_text is not None:
        # Batch path: evaluation model call was submitted via the Batch API;
        # caller provides the response text and stop_reason directly.
        result      = _precomputed_eval_text
        stop_reason = _precomputed_stop_reason or "end_turn"
        usage       = _precomputed_usage or {}
        log("Evaluation",
            f"[BATCH] Using precomputed eval text "
            f"({len(result)} chars, stop_reason={stop_reason})")
    else:
        result, stop_reason, usage = call_model(MODEL_SONNET, MASTER_PROMPT, content,
                             temperature=0.2, max_tokens=EVAL_MAX_TOKENS, enable_cache=True,
                             return_stop_reason=True,
                             eval_id=prop.get("eval_id"), step_type="evaluation",
                             trace_id_out=_eval_trace_id_box)
    if not result:
        return None

    evaluated                        = prop.copy()
    evaluated["evaluation_output"]   = result
    # Fix (this session, operator correction): a truncated evaluation
    # used to trigger a retry, on the theory that truncation might be
    # a transient issue. It isn't -- same prompt, same model, same
    # max_tokens budget every time, so a retry mostly just repeats the
    # same structural failure at the cost of a second API call, while
    # delaying visibility into the actual problem. Truncation is now
    # treated as an immediate, non-retried system failure, with full
    # diagnostics captured here for investigation: how big the prompt
    # was, how many output tokens were actually generated before the
    # cutoff, and the exact stop_reason.
    evaluated["_truncated"] = (stop_reason == "max_tokens")
    if evaluated["_truncated"]:
        evaluated["_truncation_diagnostics"] = {
            "master_prompt_chars": len(MASTER_PROMPT),
            "content_chars": len(content),
            "total_payload_chars": len(MASTER_PROMPT) + len(content),
            "output_tokens_used": usage.get("output_tokens"),
            "max_tokens_limit": EVAL_MAX_TOKENS,
            "stop_reason": stop_reason,
        }
        log("Evaluation", f"⚠️ Evaluation truncated (hit max_tokens={EVAL_MAX_TOKENS}, "
            f"output_tokens_used={usage.get('output_tokens', '?')}) for "
            f"{prop.get('player')} {prop.get('stat_type')} — treated as an "
            f"immediate system failure, no retry.")
    evaluated["direction"]           = _extract_direction(result)
    evaluated["match_type"]          = _extract_match_type(result)
    evaluated["is_favorite"]         = _extract_is_favorite(result, prop)
    evaluated["is_underdog"]         = not evaluated["is_favorite"]
    evaluated["rules_confirmed"]     = _extract_rules(result, "confirmed")
    evaluated["rules_challenged"]    = _extract_rules(result, "challenged")

    # Calibration tracking (this session): RBMD and Gate14 have no
    # discrete Python trigger point -- both are reasoning-level checks
    # applied inside the evaluation model's own text and (per the
    # citation-format fix already deployed) now cited as RULE: markers.
    # The citation itself IS the fire event; a rule cited in EITHER
    # rules_confirmed or rules_challenged was genuinely invoked -- the
    # ✅/⚡ marker distinguishes clean-pass vs flagged-adjustment, not
    # applied vs not-applied (same "any non-None signal fired"
    # philosophy already used for the ML S1/S2/S4/S5 loop). Deduped per
    # evaluation via set() so a rule cited twice in one eval still only
    # counts as one fire. LPAB_PSAR is deliberately NOT tracked here
    # even though the decision tree lists it as a citable name -- it has
    # its own real, mechanical Python trigger in scoring_agent()'s
    # serve-stat gate, which is the authoritative fire event; counting
    # the text citation too would double-fire the same real-world event
    # and corrupt the calibration sample with a duplicate.
    #
    # Mislabel fix (found during this session's full-system audit): this
    # function is evaluation_agent(), the PROPS pipeline only -- a
    # "Gate14" citation reaching this exact code path is necessarily the
    # PROPS-side spec (direction-aware, 4-case, see DEMETRIUS_v7_9_6_
    # HANDOFF.txt's "GATE 14 -- HOME PLAYER" section), never the
    # moneyline one (small competitive-only lean, a separate spec in the
    # same handoff). This was previously firing "ML_Gate14_HomeCourtLean"
    # -- the wrong registry entry, moneyline-prefixed but fed from props
    # data -- for as long as this hook has existed. Now correctly routes
    # to "Gate14_Props_Procedural" (registered alongside the Rule 9/CFEF
    # compliance checks). ML_Gate14_HomeCourtLean is fed from its real
    # source instead -- moneyline's own compliance audit, see the
    # equivalent hook in moneyline_evaluation_agent().
    _cited_rule_names = set(evaluated["rules_confirmed"]) | set(evaluated["rules_challenged"])
    _citation_to_registry = {"RBMD": "RBMD_v1_2", "Gate14": "Gate14_Props_Procedural"}
    for _cited_name, _registry_key in _citation_to_registry.items():
        if _cited_name in _cited_rule_names:
            record_provisional_rule_fire(_registry_key, prop.get("eval_id"), player=prop.get("player"))

    # ECL certification check (this session): a cheap, mechanical,
    # fast pre-check -- parsed the same way PROVEN_COMPETITIVE_REQS
    # already is. This does NOT gate the pipeline by itself; it only
    # catches the one thing worth catching mechanically (the block is
    # missing entirely) and logs diagnostics. The real judgment --
    # whether a PASSED claim actually holds up, whether a FAILED
    # certification is honest -- is check 14 in COMPLIANCE_PROMPT
    # below, which already has ground truth, the full evaluation text,
    # and the existing retry/penalty/Category-A-vs-B machinery checks
    # 12 and 13 use. Same division of labor as _truncated (mechanical)
    # vs Rule_TruthLock (judged) two levels up in this same function.
    ecl = _extract_ecl_certification(result)
    evaluated["_ecl_intact"]     = ecl["intact"]
    evaluated["_ecl_violation"]  = ecl["violation"]
    evaluated["_ecl_failed_stage"] = ecl["failed_stage"]
    if ecl["intact"] is not True:
        log("Evaluation", f"⚠️ ECL certification {'missing' if ecl['intact'] is None else 'BROKEN'} "
            f"for {prop.get('player')} {prop.get('stat_type')}: {ecl['violation']}")

    if _ATE_AVAILABLE:
        try:
            _eval_trace_id = _eval_trace_id_box[0] if _eval_trace_id_box else None
            ate.capture_rule_activations(
                eval_id=prop.get("eval_id"), trace_id=_eval_trace_id,
                rules_confirmed=evaluated["rules_confirmed"],
                rules_challenged=evaluated["rules_challenged"])
            ate.capture_checkpoint_stamps(
                eval_id=prop.get("eval_id"), trace_id=_eval_trace_id, response_text=result)
        except Exception:
            pass  # both capture_* functions already fail-open internally;
                  # belt-and-suspenders only
    evaluated["one_sentence"]        = _extract_one_sentence(result)
    evaluated["eval_confidence"]     = _extract_eval_confidence(result)
    evaluated["dominance_level"]     = _extract_dominance(result)
    evaluated["competitive_reqs"]    = _extract_competitive_reqs(result)

    # Goblin lock enforcement
    if prop.get("direction_locked") and prop.get("locked_direction"):
        locked = prop["locked_direction"].upper()
        if evaluated["direction"] != locked:
            # 2026-07-25: preserve the genuine pre-override prediction before
            # it gets overwritten below. Previously this was discarded --
            # the alert fired but nothing kept what the model actually
            # thought would happen, which is real, free calibration data
            # (see OpenClaw_Reasoning_Architecture_Final_Handoff.md §5).
            # This line is pure addition -- does not change what gets
            # posted or how the pick is scored. That's a separate,
            # deliberately NOT-yet-made decision -- see PENDING_DECISION
            # note below.
            evaluated["genuine_direction_pre_lock"] = evaluated["direction"]
            evaluated["genuine_confidence_pre_lock"] = evaluated.get("eval_confidence")

            send_discord("alerts",
                f"⚠️ **GOBLIN CONFLICT** — {prop.get('player')} {prop.get('line')}\n"
                f"Locked: {locked} | Data supports: {evaluated['direction']}\n"
                f"Direction locked — included with YELLOW cap"
            )
            # RESOLVED (2026-08-04, user-confirmed): per the reasoning
            # architecture review, PASS_DIRECTION_MISMATCH (skip posting
            # entirely) was considered and explicitly rejected -- v7.7's
            # locked "every match MUST produce MORE or LESS, NO PLAY as
            # a default is BANNED" rule stays intact. Instead: still post
            # a real MORE/LESS pick (the locked direction, since Goblin
            # is structurally MORE-only), but the disagreement is now
            # surfaced ON THE ACTUAL PICK CARD (see format_pick_card's
            # goblin_conflict note) instead of only reaching a separate
            # #alerts channel. No longer silent -- just not a NO_PLAY.
            evaluated["direction"]      = locked
            evaluated["goblin_conflict"] = True

    # --- Shadow computations for calibration + the new pick-detail view ---
    # (2026-07-25, new). Everything in this block is READ-ONLY with respect
    # to the live pick: it computes and LOGS additional signals but never
    # writes back into `evaluated`, never changes direction/confidence/
    # grade, and never affects compliance, scoring, or posting. This is
    # deliberate -- these two modules are tested and correct in isolation,
    # but haven't yet earned the right to control a real, money-affecting
    # decision. Running them in shadow mode is how that gets earned: once
    # enough settled outcomes exist to compare shadow_recommendation
    # against what actually happened, that's a real, data-backed decision
    # about whether to let them drive posting -- not a guess.
    _primitives = {
        "hold_pct":       (prop.get("research_data", {}).get("player", {}) or {}).get("serve_hold_pct"),
        "opp_hold_pct":   (prop.get("research_data", {}).get("opponent", {}) or {}).get("serve_hold_pct"),
        "bp_save_pct":    (prop.get("research_data", {}).get("player", {}) or {}).get("bp_save_pct_v8"),
        "break_pct":      (prop.get("research_data", {}).get("player", {}) or {}).get("break_pct_v8"),
        "avg_aces":       (prop.get("research_data", {}).get("player", {}) or {}).get("avg_aces_v8"),
        "first_serve_pct": (prop.get("research_data", {}).get("player", {}) or {}).get("first_serve_pct_v8"),
    }

    # Data completeness: how many of the 6 tracked primitives did V8
    # actually confirm for THIS specific pick. A plain, honest fraction --
    # not a compliance grade, not tied into GREEN/YELLOW/RED at all.
    _confirmed = sum(1 for v in _primitives.values() if v is not None)
    _data_completeness_pct = round(_confirmed / len(_primitives) * 100, 1)
    _ENOUGH_DATA_THRESHOLD_PCT = 50.0  # starting point -- 3+ of 6 primitives confirmed.
                                        # Deliberately not tied to any existing compliance
                                        # threshold; revisit once calibration data shows
                                        # whether this number actually predicts anything.
    _data_completeness_note = (
        f"{_confirmed}/{len(_primitives)} primitives confirmed by V8"
        + (" — ENOUGH DATA" if _data_completeness_pct >= _ENOUGH_DATA_THRESHOLD_PCT
           else " — THIN DATA, treat this pick's confidence with extra caution")
    )

    # Match Model cross-check: exact math from tennisematch_model.py, only
    # runs when both players' hold% are actually confirmed (it has nothing
    # honest to compute otherwise). Shadow value only -- see block docstring.
    _mm_win_prob = _mm_games = _mm_sets = None
    if _primitives["hold_pct"] is not None and _primitives["opp_hold_pct"] is not None:
        try:
            from tennis_match_model import PlayerPrimitives, MatchFormat, simulate_match
            _p_hold = _primitives["hold_pct"] / 100 if _primitives["hold_pct"] > 1 else _primitives["hold_pct"]
            _o_hold = _primitives["opp_hold_pct"] / 100 if _primitives["opp_hold_pct"] > 1 else _primitives["opp_hold_pct"]
            _mm = simulate_match(
                PlayerPrimitives(prop.get("player") or "Player", hold_pct=_p_hold),
                PlayerPrimitives(prop.get("opponent") or "Opponent", hold_pct=_o_hold),
                MatchFormat(best_of=prop.get("best_of", 3)),
            )
            _mm_win_prob, _mm_games, _mm_sets = _mm.win_prob_a, _mm.expected_games_total, _mm.expected_sets_total
        except Exception as _mm_err:
            log("Calibration", f"Match Model shadow computation skipped (non-fatal): {_mm_err}")

    # Recommendation Engine shadow read: what BET/PASS this pick would get
    # under the new reason-coded logic, for comparison against what
    # actually happened. Shadow value only -- see block docstring.
    _shadow_rec = _shadow_reason = _shadow_edge = None
    try:
        from recommendation_engine import Prediction, Market, recommend
        _pred = Prediction(
            predicted_value=(_mm_games if _mm_games is not None else prop.get("line", 0)),
            direction=evaluated.get("genuine_direction_pre_lock", evaluated["direction"]),
            confidence=(evaluated.get("eval_confidence") or 0) / 100 if (evaluated.get("eval_confidence") or 0) > 1 else (evaluated.get("eval_confidence") or 0),
            evidence_category_count=_confirmed,
            min_categories_required=3,
        )
        _mkt = Market(
            line=prop.get("line"),
            available_directions=(("MORE",) if prop.get("locked_direction", "").upper() == "MORE"
                                   else ("LESS",) if prop.get("locked_direction", "").upper() == "LESS"
                                   else ("MORE", "LESS")),
            is_locked=bool(prop.get("direction_locked")),
        )
        _rec_result = recommend(_pred, _mkt)
        _shadow_rec = _rec_result.recommendation.value
        _shadow_reason = "; ".join(_rec_result.notes)[:500]
        _shadow_edge = _rec_result.edge
    except Exception as _rec_err:
        log("Calibration", f"Recommendation Engine shadow computation skipped (non-fatal): {_rec_err}")

    # Primitive-estimate logging for calibration (2026-07-25, extended).
    # Pure addition -- logs what V8 gave us, what the model predicted, and
    # the two shadow signals above, for later scoring against the real
    # match outcome. Does not affect evaluation, compliance, scoring, or
    # posting in any way. Fail-open: a logging failure here must never
    # break a live evaluation.
    try:
        d1.log_primitive_estimate(
            eval_id=prop.get("eval_id"),
            player=prop.get("player"),
            opponent=prop.get("opponent"),
            stat_type=prop.get("stat_type"),
            line=prop.get("line"),
            primitives=_primitives,
            predicted_direction=evaluated.get("genuine_direction_pre_lock", evaluated["direction"]),
            predicted_confidence=evaluated.get("eval_confidence"),
            goblin_conflict=evaluated.get("goblin_conflict", False),
            schema_version="1.0",
            estimator_version=CORE_VERSION,
            data_completeness_pct=_data_completeness_pct,
            data_completeness_note=_data_completeness_note,
            match_model_win_prob=_mm_win_prob,
            match_model_expected_games=_mm_games,
            match_model_expected_sets=_mm_sets,
            shadow_recommendation=_shadow_rec,
            shadow_reason_code=_shadow_reason,
            shadow_edge=_shadow_edge,
        )
    except Exception as _log_err:
        log("Calibration", f"primitive-estimate logging failed (non-fatal): {_log_err}")

    return evaluated

def _extract_direction(text):
    """Extract final direction from evaluation.
    Priority: find the LAST explicit direction assignment in the text,
    which represents the final resolved direction after all checkpoints."""
    import re as _re

    # Search the final 40% for the resolved direction conclusion
    conclusion = text[int(len(text) * 0.60):]

    # Priority 1: STAMP lines (e.g. "STAMP I: ⬇ LESS" or "STAMP I: MORE")
    stamps = _re.findall(r"STAMP\s+[A-Z\d]+[:\s]+.*?(MORE|LESS)", conclusion, _re.IGNORECASE)
    if stamps:
        return stamps[-1].upper()

    # Priority 2: "Direction: LESS/MORE" in conclusion
    m = _re.search(r"Direction[:\s]+(?:\⬇\s*)?(LESS)|Direction[:\s]+(?:\⬆\s*)?(MORE)", conclusion, _re.IGNORECASE)
    if m:
        return "LESS" if m.group(1) else "MORE"

    # Priority 3: "Final direction: X" anywhere in conclusion
    m = _re.search(r"Final\s+direction[:\s]+(MORE|LESS)", conclusion, _re.IGNORECASE)
    if m:
        return m.group(1).upper()

    # Priority 4: Arrow symbols in conclusion — use the LAST one found
    arrows = _re.findall(r"(⬆|⬇|\u2b06|\u2b07)\s*\*{0,2}(MORE|LESS)\*{0,2}", conclusion)
    if arrows:
        return arrows[-1][1].upper()

    # Priority 5: Grade line pattern "MORE — 🟡" or "LESS — 🟢" in conclusion
    m = _re.search(r"(MORE|LESS)\s*[—-]\s*(?:🟢|🟡|🔴)", conclusion)
    if m:
        return m.group(1).upper()

    # Fallback: scan full text for FINAL DIRECTION marker
    m = _re.search(r"FINAL\s+DIRECTION[:\s]+(MORE|LESS)", text, _re.IGNORECASE)
    if m:
        return m.group(1).upper()

    # Last resort: last arrow symbol in full text
    arrows = _re.findall(r"(⬆|⬇)\s*\*{0,2}(MORE|LESS)\*{0,2}", text)
    if arrows:
        return arrows[-1][1].upper()

    return "LESS"

def _extract_match_type(text):
    """
    Extracts the LOPSIDED/COMPETITIVE/HIGH_VARIANCE classification --
    NOT the same concept as dominance_level (HARD/EDGE/SOFT/UNSTABLE,
    see _extract_dominance below). These are two intentionally
    separate axes in this system's Two-Paths confidence framework
    (DEMETRIUS_PROPS_DECISION_TREE.txt): match_type is the mechanical
    classification (rank-gap-based, confirmed/overridden by narrative
    judgment), dominance_level is the per-trait magnitude read. A
    COMPETITIVE match CAN legitimately have HARD dominance (confirmed
    by the system's own test fixture at the bottom of this file) --
    that combination is valid, not a contradiction to "fix" by making
    this function recognize HARD.

    v7.9.7 fix (this session): previously scanned the ENTIRE text for
    any occurrence of "LOPSIDED", which could false-positive on a
    narrative mention early in the reasoning (e.g. a checkpoint
    discussing why a match is NOT lopsided) before the model reaches
    its actual conclusion. Now reads only the final 25% of the text,
    same discipline _extract_dominance already uses, so only the
    evaluation's actual concluding classification is read.
    """
    # Read only the final 25% to avoid catching narrative mentions of
    # LOPSIDED/COMPETITIVE mid-evaluation, before the model's actual
    # concluding classification.
    conclusion = text[int(len(text) * 0.75):]
    t = conclusion.upper()
    if "CLASSIFIED AS LOPSIDED" in t or ("LOPSIDED" in t and "NOT LOPSIDED" not in t):
        return "LOPSIDED"
    if "HIGH VARIANCE" in t:
        return "HIGH_VARIANCE"
    return "COMPETITIVE"

def _extract_is_favorite(text, prop):
    rd     = prop.get("research_data", {})
    p_rank = rd.get("player", {}).get("ranking", 999)
    o_rank = rd.get("opponent", {}).get("ranking", 999)
    if p_rank and o_rank:
        return p_rank < o_rank
    return False

def _extract_rules(text, tag):
    """
    Fix (this session, confirmed root cause): the old pattern
    (r"[checkmark] Rule (word-chars)" -> "Rule" + match) assumed every
    citation was a bare number after the word "Rule" (e.g. "Rule 30"
    -> "Rule30"). That breaks for RBMD, Gate14, LPAB_PSAR, and
    FlipRule, which are real named rules with NO "Rule" prefix in the
    compliance auditor's own naming (COMPLIANCE_PROMPT cites them as
    "RBMD", "Gate14", etc. directly) -- the old code would have
    mangled these into "RuleRBMD", "RuleGate14", never matching
    compliance-side names. Moot until now because real evaluation
    output had ZERO citations of ANY kind (confirmed live, 2026-07-19:
    ai_traces got a row, rule_activations stayed at 0 on the same real
    evaluation) -- this was a latent bug, not yet an active one, caught
    while fixing the missing-instruction gap that made it moot in the
    first place.

    New format: a literal "RULE:" label, name copied verbatim, no
    prepend logic. Requires the literal "RULE:" token (not just a bare
    checkmark) specifically to avoid false-positive matches on the
    model's own generic, decorative checkmark usage elsewhere in an
    evaluation (e.g. "[checkmark] opponent data confirmed") -- tested
    against exactly that case, zero false positives.
    """
    pattern = r"✅ RULE:\s*(\S+)" if tag == "confirmed" else r"⚡ RULE:\s*(\S+)"
    return re.findall(pattern, text)

def _extract_one_sentence(text):
    for line in reversed(text.split("\n")):
        line = line.strip()
        if len(line) > 30 and "because" in line.lower():
            return line[:200]
    return ""

def _extract_dominance(text):
    """Extract dominance level from evaluation conclusion only."""
    # Read only the final 25% of the eval to avoid catching
    # narrative mentions of dominance levels mid-evaluation
    conclusion = text[int(len(text) * 0.75):]
    t = conclusion.upper()
    if "HARD DOMINANCE" in t and "NOT HARD" not in t:
        return "HARD"
    if "EDGE DOMINANCE" in t or "DOMINANCE: EDGE" in t or "= EDGE" in t:
        return "EDGE"
    if "SOFT DOMINANCE" in t and "NOT SOFT" not in t:
        return "SOFT"
    if "UNSTABLE" in t:
        return "UNSTABLE"
    # Fallback: scan full text for explicit conclusion markers only
    full = text.upper()
    if "DOMINANCE = UNSTABLE" in full or "DOMINANCE: UNSTABLE" in full:
        return "UNSTABLE"
    if "DOMINANCE = HARD" in full or "DOMINANCE: HARD" in full:
        return "HARD"
    if "DOMINANCE = EDGE" in full or "DOMINANCE: EDGE" in full:
        return "EDGE"
    if "DOMINANCE = SOFT" in full or "DOMINANCE: SOFT" in full:
        return "SOFT"
    return "UNKNOWN"

def _extract_eval_confidence(text):
    """Extract the final confidence % the eval agent assigned.
    Searches the final 40% of the eval for numeric % patterns,
    returns the last credible one found (closest to end = final answer)."""
    import re as _re
    conclusion = text[int(len(text) * 0.60):]
    candidates = []

    # P1: "Final confidence: 66%" or "Final: 66%" (colon now optional, v7.9.6 fix)
    for m in _re.finditer(r"final\s*[:\s]\s*(?:confidence\s*[:\s]\s*)?\**\s*(\d{2,3})\s*%", conclusion, _re.IGNORECASE):
        val = float(m.group(1)) / 100.0
        if 0.50 <= val <= 0.95:
            candidates.append((m.start(), val))

    # P2: "YELLOW 66%" or "GREEN (76%" (parenthesis now allowed, v7.9.6 fix)
    for m in _re.finditer(r"(?:yellow|green|amber|red)\s*[\(\s]\s*\**\s*(\d{2,3})\s*%", conclusion, _re.IGNORECASE):
        val = float(m.group(1)) / 100.0
        if 0.50 <= val <= 0.95:
            candidates.append((m.start(), val))

    # P3: "Confidence: 66%" or "Grade: 66%"
    for m in _re.finditer(r"(?:confidence|grade)[:\s]+(?:yellow|green|red|amber)?\s*(\d{2,3})\s*%", conclusion, _re.IGNORECASE):
        val = float(m.group(1)) / 100.0
        if 0.50 <= val <= 0.95:
            candidates.append((m.start(), val))

    # P4: "= **66%**" or "-> 66%" or "→ 66%" (Unicode arrow added, v7.9.6 fix)
    for m in _re.finditer(r"(?:=|->|\u2192|to)\s*\*{0,2}(\d{2,3})\s*%\*{0,2}", conclusion, _re.IGNORECASE):
        val = float(m.group(1)) / 100.0
        if 0.50 <= val <= 0.95:
            candidates.append((m.start(), val))

    # P5: "**66% YELLOW**" bold pattern
    for m in _re.finditer(r"\*{1,2}(\d{2,3})\s*%\s*(?:yellow|green|amber|red)?\*{0,2}", conclusion, _re.IGNORECASE):
        val = float(m.group(1)) / 100.0
        if 0.50 <= val <= 0.95:
            candidates.append((m.start(), val))

    # P6: "YELLOW 66" no percent sign (truncated evals)
    for m in _re.finditer(r"(?:yellow|green|amber)\s+(\d{2,3})(?!\s*%)", conclusion, _re.IGNORECASE):
        val = float(m.group(1)) / 100.0
        if 0.50 <= val <= 0.95:
            candidates.append((m.start(), val))

    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[-1][1]
    return None
def _extract_ecl_certification(text):
    """Parse the ECL CERTIFICATION block from eval output.

    Mirrors _extract_competitive_reqs's approach deliberately: same
    conclusion window, same structured-block-first strategy, same
    graceful degradation if the model didn't format it exactly right.

    Returns a dict: {"intact": bool or None, "violation": str or None,
    "failed_stage": str or None}. intact=None means the block could not
    be found at all -- treated as a failure by the caller (a missing
    certification is itself an ECL failure, not a pass by default).
    """
    import re as _re

    conclusion = text[int(len(text) * 0.55):]

    block_match = _re.search(
        r"ECL CERTIFICATION:.*?RESULT:\s*(EVIDENCE CHAIN INTACT|EVIDENCE CHAIN BROKEN)",
        conclusion, _re.IGNORECASE | _re.DOTALL
    )
    if not block_match:
        # Also check the full text in case the 0.55 window missed it
        # on an unusually long evaluation (mirrors the fallback logic
        # already used elsewhere in this parser style).
        block_match = _re.search(
            r"ECL CERTIFICATION:.*?RESULT:\s*(EVIDENCE CHAIN INTACT|EVIDENCE CHAIN BROKEN)",
            text, _re.IGNORECASE | _re.DOTALL
        )
    if not block_match:
        return {"intact": None, "violation": "ECL CERTIFICATION block missing entirely",
                "failed_stage": None}

    result_text = block_match.group(1).upper()
    intact = (result_text == "EVIDENCE CHAIN INTACT")

    if intact:
        return {"intact": True, "violation": None, "failed_stage": None}

    # Broken -- find which stage failed and why, for the alert/retry text.
    block_text = block_match.group(0)
    failed_stage = None
    violation = None
    for stage_name, stage_key in [
        ("Evidence Verification", "Evidence Verification"),
        ("Rule Status Resolution", "Rule Status Resolution"),
        ("Direction Drivers Integration", "Direction Drivers Integration"),
        ("Confidence Traceability", "Confidence Traceability"),
        ("Reconstructability", "Reconstructability"),
    ]:
        stage_match = _re.search(
            rf"{_re.escape(stage_key)}:\s*\[?FAILED[^\]\n]*",
            block_text, _re.IGNORECASE
        )
        if stage_match:
            failed_stage = stage_name
            violation = stage_match.group(0)
            break

    return {"intact": False,
            "violation": violation or "EVIDENCE CHAIN BROKEN, specific stage not parseable from output",
            "failed_stage": failed_stage}


def _extract_competitive_reqs(text):
    """Parse proven-competitive MORE requirements from eval output.

    Looks for a structured PROVEN_COMPETITIVE_REQS block first (reliable).
    Falls back to TYPE B confirmation as a proxy.
    Returns int 0-4.

    The eval agent is instructed to output this block:
        PROVEN_COMPETITIVE_REQS:
        REQ1_EXTENDERS: YES/NO
        REQ2_TWO_SIDED_HOLD: YES/NO
        REQ3_LINE_BEATABLE: YES/NO
        REQ4_NOT_THIN_DATA: YES/NO
        RESULT: X/4 MET
    """
    import re as _re

    conclusion = text[int(len(text) * 0.55):]

    # Priority 1: structured block — most reliable
    block_match = _re.search(
        r"PROVEN_COMPETITIVE_REQS.*?RESULT[:\s]+(\d)\s*/?\s*4",
        conclusion, _re.IGNORECASE | _re.DOTALL
    )
    if block_match:
        return min(int(block_match.group(1)), 4)

    # Priority 2: "X/4 requirements met" or "X of 4 reqs met"
    for region in [conclusion, text.lower()]:
        m = _re.search(r"(\d)\s*/\s*4\s+(?:competitive\s+)?req", region, _re.IGNORECASE)
        if m:
            return min(int(m.group(1)), 4)
        m = _re.search(r"(\d)\s+of\s+4\s+(?:competitive\s+)?req", region, _re.IGNORECASE)
        if m:
            return min(int(m.group(1)), 4)
        m = _re.search(r"all\s+4\s+(?:competitive\s+)?req.*?met", region, _re.IGNORECASE)
        if m:
            return 4

    # Priority 3: count explicit YES answers in structured block
    req_markers = [
        r"REQ1_EXTENDERS[:\s]+YES",
        r"REQ2_TWO_SIDED_HOLD[:\s]+YES",
        r"REQ3_LINE_BEATABLE[:\s]+YES",
        r"REQ4_NOT_THIN_DATA[:\s]+YES",
    ]
    count = sum(1 for m in req_markers if _re.search(m, conclusion, _re.IGNORECASE))
    if count > 0:
        return count

    # Priority 4: TYPE B confirmed is sufficient for REQ2+REQ3
    # (both hold = two-sided confirmed; TYPE B = line structurally beatable)
    # Still need REQ1 (extenders confirmed) and REQ4 (not thin data)
    type_b = any(x in conclusion.lower() for x in [
        "type b confirmed", "structure: type b", "type b structure confirmed",
        "type b →", "type b lock"
    ])
    not_thin = "not thin" in conclusion.lower() or (
        "thin" not in conclusion.lower() and
        "data gap" not in conclusion.lower()
    )
    extenders = any(x in conclusion.lower() for x in [
        "confirmed extender", "both extender", "extender confirmed",
        "demonstrated extended", "3-set", "went three"
    ])

    if type_b and not_thin and extenders:
        return 4
    if type_b and not_thin:
        # Check CFEF section for previous tournament extender evidence
        cfef_section = ""
        cfef_idx = text.lower().find("cfef")
        if cfef_idx > 0:
            cfef_section = text[cfef_idx:cfef_idx+1500].lower()
        extender_cfef = any(x in cfef_section for x in [
            "3 sets", "3-set", "three sets", "tiebreak", "tiebreaks",
            "match points", "extended", "previous tournament",
            "trigger 6", "friction", "queen", "halle", "eastbourne",
            "collignon", "medjedovic", "bublik",
        ])
        if extender_cfef:
            return 4  # CFEF section confirms extender evidence
        return 3  # Missing extender confirmation
    if type_b:
        return 2  # TYPE B only

    return 0


# ================================================================
# BATCH API HELPERS (v1.1, 2026-07-23)
# These two functions split process_single_prop's pipeline at the
# evaluation model call so openclaw_intake's batch poller can submit
# just that one call to Anthropic's /v1/messages/batches (50% off)
# while running research + gates synchronously (cheap) and running
# compliance + scoring + posting when results return hours later.
#
# prepare_batch_prop  — everything UP TO evaluation_agent()
# finalize_batch_evaluation — everything FROM evaluation_agent() onward
#
# Both are additive new functions. No existing code is changed except
# the three optional params added to evaluation_agent() above.
# ================================================================

def prepare_batch_prop(kind, payload, dates, weights):
    """
    Pre-evaluation phase for a batch-submitted prop. Handles 'text'
    and 'image' kinds. Runs: parse → tournament context → V8 prefetch
    → Gate 11 → Gate 11-B → research → build_evaluation_content.

    Returns (prop, content_str) where:
      prop         — fully populated with eval_id and research_data
      content_str  — the user message to send in the Batch API request

    Raises ValueError with a human-readable reason on any gate
    rejection, parse failure, or research failure — caller marks FAILED.

    Called from openclaw_intake._process_batch_submit().
    """
    import re as _re

    prop = None

    # ── Parse input into a prop dict ─────────────────────────────────
    if kind == "image":
        b64        = payload["b64"]
        media_type = payload.get("media_type", "image/png")
        _err = []
        parse_result = call_model(
            MODEL_SONNET, SCREENSHOT_PARSE_PROMPT,
            [{"type": "image",
              "source": {"type": "base64", "media_type": media_type,
                         "data": b64}},
             {"type": "text",
              "text": "Extract all props from this PrizePicks screenshot."}],
            temperature=0.1, max_tokens=800, error_out=_err,
        )
        if not parse_result:
            # 2026-08-04: real reason (billing, malformed base64, a
            # timeout, etc.) now reaches the same place Discord's
            # #system alert already gets it, instead of always
            # showing this same generic line no matter what happened.
            reason = _err[0] if _err else "no response from the model"
            raise ValueError(f"could not read the screenshot — {reason}")
        try:
            parsed = json.loads(_re.sub(r"```json|```", "", parse_result).strip())
        except Exception:
            raise ValueError("screenshot could not be parsed as JSON — send a clearer image")
        if not parsed:
            raise ValueError("no props found in the screenshot")

        first_player   = parsed[0].get("player")
        first_opponent = parsed[0].get("opponent")
        ctx = resolve_tournament_context(first_player, first_opponent) if first_player else None
        if not ctx:
            raise ValueError(
                f"could not confirm tournament or surface for {first_player}")

        surface    = ctx["surface"]
        tour       = ctx["tour"]
        tournament = ctx["tournament"]
        is_gs = any(gs in tournament.lower() for gs in
                    ("wimbledon", "us open", "australian open",
                     "roland garros", "french open"))

        # Batch processes one prop at a time — take the first prop from
        # the card. APEX-WIRING-2026-08-27: fantasy score is evaluable
        # (engine category).
        # APEX-DEMONPARITY-2026-09-29: this used to hard-fail the whole
        # batch submission if the first prop on the card was Demon-typed
        # (a stale "DEMON skip rule" from before the engine could price
        # Demon props). Demon props are evaluated normally everywhere
        # else now, so they're no longer special-cased or rejected here.
        for p in parsed:
            stat = normalize_stat_type(p.get("stat_type", ""))
            prop = {
                **p,
                "stat_type":                 stat,
                "tournament":                tournament,
                "surface":                   surface,
                "tour":                      tour,
                "tournament_stage":          3 if is_gs else 1,
                "is_grand_slam":             is_gs,
                "both_directions_available": p.get("prop_type") == "standard",
                "match_date":                dates["match_date"],
                "same_day_3set_count":       0,
            }
            break
        if not prop:
            raise ValueError("no evaluable props found in screenshot")

    elif kind == "text":
        user_input = payload.get("text", "").strip()
        parts      = user_input.split()
        if len(parts) < 3:
            raise ValueError("format: Player MORE/LESS LINE STAT")
        first_player = " ".join(parts[:-3]) if len(parts) > 4 else parts[0]
        ctx        = resolve_tournament_context(first_player, None)
        surface    = ctx["surface"]    if ctx else "hard"
        tour       = ctx["tour"]       if ctx else "atp"
        tournament = ctx["tournament"] if ctx else "Current tournament"
        is_gs = any(gs in tournament.lower() for gs in
                    ("wimbledon", "us open", "australian open",
                     "roland garros", "french open")) if ctx else False
        stat = normalize_stat_type(parts[-1])
        # APEX-WIRING-2026-08-27: fantasy score is evaluable (engine category).
        prop = {
            "player":                    first_player,
            "opponent":                  "Research required",
            "stat_type":                 stat,
            "line":                      (float(parts[-2])
                                          if parts[-2].replace(".", "").isdigit()
                                          else 0),
            "direction":                 (parts[-3].upper()
                                          if parts[-3].upper() in ("LESS", "MORE")
                                          else "LESS"),
            "tournament":                tournament,
            "surface":                   surface,
            "tour":                      tour,
            "tournament_stage":          3 if is_gs else 1,
            "is_grand_slam":             is_gs,
            "both_directions_available": True,
            "prop_type_classified":      "standard",
            "match_date":                dates["match_date"],
            "same_day_3set_count":       0,
        }
    else:
        raise ValueError(f"unsupported batch kind: {kind!r} "
                         f"(supported: 'text', 'image')")

    # ── Mint eval_id + start Dataset A lifecycle ──────────────────────
    eval_id    = d1.new_eval_id()
    prop["eval_id"] = eval_id
    d1.start(eval_id, "PROP",
             prop.get("player", ""), prop.get("opponent", ""),
             tour=prop.get("tour"), tournament=prop.get("tournament"),
             round_=prop.get("round"), surface=prop.get("surface"),
             match_scheduled_time=prop.get("match_date"),
             market=prop.get("stat_type"), line=prop.get("line"),
             pipeline_version=CORE_VERSION, _logger=log)

    # ── V8 pre-fetch (same as evaluate_manual_prop) ───────────────────
    if _V8_AVAILABLE:
        try:
            pkt = build_tennis_packet(
                prop.get("player", ""), prop.get("opponent", ""),
                prop.get("surface", "hard"), prop.get("tour", "atp")
            )
            if pkt:
                pkt["_prefetch_tour"] = prop.get("tour", "atp")
                prop["_v8_packet_prefetched"] = pkt
        except Exception as e:
            log("BatchPrep", f"V8 pre-fetch failed (non-fatal): {e}")

    # ── Gate 11 ───────────────────────────────────────────────────────
    gate_result = gate_11_check(prop, dates)
    if gate_result["status"] == "REJECTED":
        d1.terminate(eval_id, "GATE11_REJECTED", _logger=log)
        raise ValueError(
            f"Gate 11 rejected — {gate_result.get('reason','not found in V8')}")
    if gate_result.get("verified") is False:
        prop["gate11_unverified"] = True

    # ── Gate 11-B ─────────────────────────────────────────────────────
    prop = gate_11b_format_detection(prop)
    d1.transition(eval_id, "RESEARCH_STARTED", _logger=log)

    # ── Research ──────────────────────────────────────────────────────
    d1.heartbeat("research", "WORKING", current_task=prop.get("player"), _logger=log)
    try:
        prop = research_agent(prop, dates)
    except Exception as e:
        d1.terminate(eval_id, "RESEARCH_FAILURE", str(e), _logger=log)
        d1.heartbeat("research", "FAILED", detail=str(e),
                     increment_failure=True, _logger=log)
        raise ValueError(f"Research failed: {e}")

    d1.transition(eval_id, "RESEARCH_COMPLETE", _logger=log)
    d1.heartbeat("research", "IDLE", increment_success=True, _logger=log)

    # ── Build evaluation content (the user message for the API call) ──
    content = build_evaluation_content(prop)
    log("BatchPrep",
        f"Prepared {prop.get('player')} {prop.get('stat_type')} "
        f"eval_id={eval_id} content={len(content)} chars — ready for batch submit")
    return prop, content


def prepare_batch_ml_prop(payload, dates):
    """
    Pre-evaluation phase for a batch moneyline screenshot. Parses the
    screenshot synchronously (cheap Sonnet vision call), resolves
    tournament context, builds evaluation content via
    build_moneyline_evaluation_content(). Mints eval_id and starts
    Dataset A.

    Returns (batch_state, content_str) where batch_state is a dict
    containing everything finalize_batch_ml_evaluation() needs, and
    content_str is the user message to submit to the Batch API.
    Raises ValueError with a human-readable reason on any failure.

    Called from openclaw_intake._process_batch_submit() for ml_image
    batch items.
    """
    import re as _re
    b64        = payload["b64"]
    media_type = payload.get("media_type", "image/png")

    # ── Parse screenshot (sync vision call — cheap, ~800 tokens) ─────
    _err = []
    parse_result = call_model(
        MODEL_SONNET, SCREENSHOT_PARSE_PROMPT,
        [{"type": "image",
          "source": {"type": "base64", "media_type": media_type, "data": b64}},
         {"type": "text",
          "text": "Extract player names, opponent names, and lines from this screenshot."}],
        temperature=0.1, max_tokens=800, error_out=_err,
    )
    if not parse_result:
        reason = _err[0] if _err else "no response from the model"
        raise ValueError(f"could not read the screenshot — {reason}")
    try:
        parsed = json.loads(_re.sub(r"```json|```", "", parse_result).strip())
    except Exception:
        raise ValueError("screenshot could not be parsed as JSON — send a clearer image")

    from discord_message_classifier import classify_image_content
    if classify_image_content(parsed) != "moneyline":
        raise ValueError(
            "this looks like a props board, not a moneyline screen — "
            "use the PrizePicks screenshot input above instead")

    player   = parsed.get("player", "")
    opponent = parsed.get("opponent", "")
    player_odds   = parsed.get("player_odds", "")
    opponent_odds = parsed.get("opponent_odds", "")

    if not player or not opponent:
        raise ValueError("could not read both player names from the moneyline screen")

    # ── Doubles guard ──────────────────────────────────────────────────
    if _is_doubles_entry(player) or _is_doubles_entry(opponent):
        raise ValueError(
            f"Doubles not supported — {player} vs {opponent}. "
            f"This system only evaluates singles matches.")

    # ── Tournament context ─────────────────────────────────────────────
    ctx = resolve_tournament_context(player, opponent)
    if not ctx:
        raise ValueError(
            f"could not confirm tournament for {player} vs {opponent}")

    surface       = ctx["surface"]
    tour          = ctx["tour"]
    tournament    = ctx["tournament"]
    tournament_id = ctx.get("tournament_id")
    tour_confirmed = ctx.get("tour_confirmed", True)

    if not tour_confirmed:
        log("BatchML",
            f"Tour unconfirmed for {player} vs {opponent} — "
            f"proceeding with tour='{tour}' as best guess")

    # ── Build evaluation content ────────────────────────────────────────
    content, precomputed = build_moneyline_evaluation_content(
        player, opponent, surface, tournament_id, tour,
        tournament_name=tournament)
    if content is None:
        raise ValueError(
            "V8 API unavailable — cannot build moneyline evaluation content")

    # ── Mint eval_id + start Dataset A lifecycle ───────────────────────
    eval_id = d1.new_eval_id()
    d1.start(eval_id, "MONEYLINE", player, opponent,
             tour=tour.upper() if tour else None,
             surface=surface, pipeline_version=CORE_VERSION, _logger=log)
    d1.heartbeat("research", "WORKING",
                 current_task=f"{player} vs {opponent}", _logger=log)
    d1.transition(eval_id, "RESEARCH_COMPLETE", _logger=log)
    d1.heartbeat("research", "IDLE", increment_success=True, _logger=log)

    batch_state = {
        "player":        player,
        "opponent":      opponent,
        "surface":       surface,
        "tour":          tour,
        "tournament":    tournament,
        "tournament_id": tournament_id,
        "player_odds":   player_odds,
        "opponent_odds": opponent_odds,
        "precomputed":   precomputed,
        "eval_id":       eval_id,
        "tour_confirmed": tour_confirmed,
    }

    log("BatchML",
        f"Prepared {player} vs {opponent} eval_id={eval_id} — ready for batch")
    return batch_state, content



# ---------------------------------------------------------------------------
# APEX-FRAGILITY-WIRE-2026-08-21 -- Fragility V3 informational display layer.
# Display + logging ONLY. Never alters winner, confidence, recommendations,
# Dataset A, compliance, or cross-check logic. Fail-open by contract: any
# problem returns "" and the Discord post is exactly what it was before.
# ---------------------------------------------------------------------------
def _fragility_american_to_prob(odds_str):
    s = str(odds_str).strip().replace("\u2212", "-")
    if not s or s in ("--", "-", "\u2014"):
        return None
    v = float(s.replace("+", ""))
    if v == 0:
        return None
    return (-v) / ((-v) + 100.0) if v < 0 else 100.0 / (v + 100.0)


def _fragility_block(player, opponent, player_odds, opponent_odds,
                     surface, tour, tournament):
    """APEX-FRAGILITY-WIRE-2026-08-21: returns the informational block or ""."""
    try:
        pa = _fragility_american_to_prob(player_odds)
        pb = _fragility_american_to_prob(opponent_odds)
        if pa is None or pb is None or (pa + pb) <= 0:
            return ""
        fair_a = pa / (pa + pb)              # de-vig: normalize the two sides
        if fair_a >= 0.5:
            fav, dog, fav_p = player, opponent, fair_a
        else:
            fav, dog, fav_p = opponent, player, 1.0 - fair_a
        import fragility_live
        r = fragility_live.score_for_apex(
            fav, dog, fav_p,
            surface=str(surface).title(), tour=str(tour),
            tournament=str(tournament), round_="",
            price_source="devig_entered")
        if not r:
            return ""
        return ("\u2014\u2014\u2014 Fragility (informational) \u2014\u2014\u2014\n"
                + r["block"].replace("\\n", "\n"))
    except (Exception, SystemExit):
        return ""



# ---------------------------------------------------------------------------
# APEX-ENGINE-PRIMARY-2026-08-23 -- frozen prop engine as the PRIMARY prop
# source, with the legacy path as an automatic fallback.
#
# On engine success this path deliberately skips research_agent,
# evaluation_agent, compliance_agent, scoring_agent and the correlation check --
# that LLM chain is the cost this replaces. Gate 11, Gate 11-B and Dataset A
# tracking all still run. The tier system + frozen-model + holdout validation
# stand in for the compliance/scoring layer on engine picks, by explicit
# operator decision (2026-08-23).
#
# Fail-open by contract: ANY problem returns None and the legacy path runs
# exactly as it did before. No silence, no hard failure.
# ---------------------------------------------------------------------------

# PrizePicks stat slug -> engine category. Confirmed against the slugs this file
# actually branches on. Anything unmapped -> engine declines -> legacy runs.
# APEX-WIRING-2026-08-27: normalize_stat_type() lowercases the label PrizePicks
# shows and swaps spaces for underscores -- it does not translate. So the keys
# below must be the labels as displayed ("Sets Won" -> sets_won), plus aliases.
_APEX_STAT_MAP = {
    "aces":               "aces",
    "double_faults":      "dfs",
    "total_games":        "total_games",       # match-level
    "games_won":          "games_won",         # player-level
    "total_games_won":    "games_won",         # player-level (operator-confirmed)
    "total_sets":         "total_sets",        # match-level
    "sets":               "sets_won",          # player-level
    "sets_won":           "sets_won",          # player-level ("Sets Won" card)
    "break_points_won":   "breaks",            # player-level
    "fantasy_score":      "fantasy_score",
    "total_tie_breaks":   "tiebreaks",         # match-level  (APEX-ENGINE-ONLY-2026-08-24)
    "tie_breaks":         "tiebreaks",         # match-level
    "tiebreaks":          "tiebreaks",         # match-level
    "first_set_games":    "first_set_games",   # match-level
    "first_set_total_games": "first_set_games",
    "any_tiebreak":       "any_tiebreak",      # match-level, binary
    "any_tie_break":      "any_tiebreak",
    "any_tie_breaks":     "any_tiebreak",
    "first_set_winner":   "first_set_fav_wins",  # match-level, binary (dog flip below)
    "win_first_set":      "first_set_fav_wins",
    "goes_3_sets":        "goes_3_sets",       # match-level, binary
    "match_goes_3_sets":  "goes_3_sets",
    "3_sets":             "goes_3_sets",
    "goes_5_sets":        "goes_5_sets",       # match-level, binary
    "match_goes_5_sets":  "goes_5_sets",
    "5_sets":             "goes_5_sets",
}
# APEX-FSG-COLLECT-WIRE-2026-09-04: first-set slugs are RESEARCH COLLECTION.
# Intercepted in _apex_engine_pick before the price feed; never mapped into the
# engine (no validated projection method). Collector: apex_firstset_collect.py
_APEX_FIRSTSET_SLUGS = {"1st_set_total_games", "1st_set_total_games_won",
                        "1st_set_games_won", "1st_set_aces", "1st_set_double_faults",
                        "1st_set_breaks", "1st_set_winner"}
_APEX_MATCH_LEVEL = {"total_games", "total_sets", "tiebreaks", "first_set_games",
                     "any_tiebreak", "goes_3_sets", "goes_5_sets", "first_set_fav_wins"}
# Binary categories: the simulator returns 0/1 per sim, so P(over 0.5) is the
# event probability. The line is always 0.5 regardless of what the card says.
_APEX_BINARY = {"any_tiebreak", "goes_3_sets", "goes_5_sets", "first_set_fav_wins"}

# ── APEX-ENGINE-ONLY-2026-08-24: legacy is OFF. An engine decline is a defect,
# not a fallback. Set env APEX_ENGINE_ONLY=0 to re-enable legacy for a run.
import os as _apex_os
APEX_ENGINE_ONLY = _apex_os.environ.get("APEX_ENGINE_ONLY", "1").strip() != "0"
_APEX_DECLINE_LOG = "apex_engine_declines_log_v1.csv"


# APEX-ACESCAL-2026-09-09: measured bias on aces LESS (see patch docstring). Applied
# downstream of the frozen simulator; never changes the simulator's own output.
_ACESCAL = {1: 0.020, 0: 0.035, -1: 0.035}   # is_fav 1 = favourite, 0 = underdog, -1 = match-level

def _apex_prob_cal(prob, cat, direction, is_fav):
    """Calibrated probability: raw, minus the measured aces-LESS bias. Other
    categories and MORE picks return the raw value unchanged. Never raises."""
    try:
        p = float(prob)
        if str(cat) != "aces" or str(direction).upper() != "LESS":
            return p
        return max(0.50, min(p, p - _ACESCAL.get(int(is_fav), 0.035)))
    except Exception:
        return prob


def _apex_decline(prop, msg):
    """Log an engine decline AND stamp the reason on the prop so the caller can
    post it. If the reason is the price feed's, append the feed's own last words."""
    try:
        reason = str(msg)
        if "no usable odds" in reason:
            try:
                import apex_price_feed as _apf
                fr = _apf.last_reason() if hasattr(_apf, "last_reason") else ""
                if fr:
                    reason = f"{reason} [{fr}]"
            except Exception:
                pass
        reason = reason.replace(" -> legacy", "").strip()
        if isinstance(prop, dict):
            prop["apex_decline_reason"] = reason
    except Exception:
        reason = str(msg)
    try:
        log("ApexEngine", str(msg))
    except Exception:
        pass


def _apex_record_decline(prop, reason, eval_id=None):
    """Append one line to the declines log. Never raises."""
    try:
        import csv
        fields = ["ts", "eval_id", "player", "opponent", "stat_type", "line", "reason"]
        wh = not _apex_os.path.exists(_APEX_DECLINE_LOG)
        with open(_APEX_DECLINE_LOG, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            if wh:
                w.writeheader()
            w.writerow({"ts": datetime.now().isoformat(timespec="seconds"), "eval_id": eval_id or "",
                        "player": prop.get("player", ""), "opponent": prop.get("opponent", ""),
                        "stat_type": prop.get("stat_type", ""), "line": prop.get("line", ""),
                        "reason": reason})
    except Exception:
        pass


def _apex_engine_pick(prop, dates, eval_id):
    """APEX-ENGINE-PRIMARY-2026-08-23: returns a scored-shaped dict or None.

    None means "engine declined" for ANY reason -- the caller falls through to
    the legacy path untouched."""
    try:
        # --- APEX-FSG-COLLECT-WIRE-2026-09-04: first-set research collection.
        # Runs BEFORE the price feed so unpriced matches are still collected.
        try:
            _fs = str(prop.get("stat_type", "")).strip().lower().replace(" ", "_")
            if _fs in _APEX_FIRSTSET_SLUGS:
                import apex_firstset_collect as _afc
                _fr = _afc.record(
                    prop,
                    match_date=prop.get("match_date") or prop.get("date") or "",
                    tournament=prop.get("tournament", ""),
                    best_of=5 if str(prop.get("match_format", "")).lower() == "bo5" else 3,
                    wta=1 if str(prop.get("tour", "")).lower() == "wta" else 0,
                    surface=prop.get("surface", ""))
                _apex_decline(prop, f"declined ({prop.get('player', '?')}): "
                              f"first-set research collection (no projection method yet) [{_fr}]")
                return None
        except Exception as _fe:
            log("ApexEngine", f"first-set collect error ({type(_fe).__name__}: {_fe}) -> falling through")
        # --- APEX-PRICE-AUTO-2026-08-24: names first -- the price feed needs the pair,
        # and a prop we cannot evaluate should never cost an API credit.
        player   = prop.get("player", "")
        opponent = prop.get("opponent", "")
        if not player or not opponent:
            _apex_decline(prop, "declined: missing player/opponent -> legacy")
            return None

        # APEX-SLIP-TIME-2026-08-24: the slip says when the match is. A match
        # already under way per the slip is declined here, before any price call.
        _slip_utc = prop.get("slip_start_utc")
        if _slip_utc:
            try:
                from datetime import datetime as _sdt, timezone as _stz
                _st = _sdt.fromisoformat(str(_slip_utc))
                _now = _sdt.now(_stz.utc)
                log("ApexEngine", f"slip start {prop.get('slip_start')} -> {prop.get('slip_start_ct')} "
                                  f"({(_st - _now).total_seconds() / 60:+.0f} min from now)")
                if (_st - _now).total_seconds() < -120:
                    _apex_decline(prop, f"declined ({player}): match already started per slip "
                                        f"({prop.get('slip_start')} = {prop.get('slip_start_ct')}) -> legacy")
                    return None
            except Exception:
                pass

        fav = dog = None
        fav_p = None
        price_source = None

        # 1) operator-typed odds win outright, and cost nothing.
        # APEX-PRICE-AUTO-FIX-2026-08-24: intake stamps the STRING "None" when no odds
        # were typed. It is truthy, so it survives `or`, and _fragility_american_to_prob
        # does a bare float() that raises ValueError on it -- which killed this function
        # before the automatic feed below could ever run. Treat sentinel strings as
        # absent, try BOTH field names, and never let a junk stamp raise past here.
        _APEX_ODDS_SENTINELS = ("none", "null", "nan", "n/a", "na", "--", "-",
                                "\u2014", "?", "tbd")

        def _apex_typed_odds(*vals):
            for v in vals:
                if v is None:
                    continue
                s = str(v).strip()
                if not s or s.lower() in _APEX_ODDS_SENTINELS:
                    continue
                try:
                    p = _fragility_american_to_prob(s)
                except Exception:
                    continue
                if p is not None:
                    return p
            return None

        pa = _apex_typed_odds(prop.get("apex_player_odds"), prop.get("player_odds"))
        pb = _apex_typed_odds(prop.get("apex_opponent_odds"), prop.get("opponent_odds"))
        if pa is not None and pb is not None and (pa + pb) > 0:
            fair_a = pa / (pa + pb)
            if fair_a >= 0.5:
                fav, dog, fav_p = player, opponent, fair_a
            else:
                fav, dog, fav_p = opponent, player, 1.0 - fair_a
            price_source = "typed"

        # 2) nothing typed -> automatic de-vigged Pinnacle. Fail-open: any problem at
        # all leaves fav_p None and we fall through to the same decline as before.
        if fav_p is None:
            _pf = None
            try:
                import apex_price_feed
                _pf = apex_price_feed.get_price(player, opponent)
            except Exception as _e:
                log("ApexEngine",
                    f"price feed unavailable ({type(_e).__name__}: {_e}) -> legacy")
            try:
                if _pf and _pf.get("ok"):
                    fav   = _pf["fav"]
                    dog   = _pf["dog"]
                    fav_p = float(_pf["pinn_fav"])
                    price_source = "auto:%s/%s" % (_pf.get("book", "?"),
                                                   _pf.get("match_method", "?"))
                    log("ApexEngine",
                        f"auto price {fav} {fav_p:.4f} ({_pf.get('sport_title','?')}, "
                        f"vig {_pf.get('vig_pct','?')}%, "
                        f"{_pf.get('credits_spent',0)} credit(s), "
                        f"{_pf.get('quota_remaining','?')} left)")
            except Exception as _e:
                fav = dog = None
                fav_p = None
                log("ApexEngine",
                    f"price feed result unusable ({type(_e).__name__}: {_e}) -> legacy")

        # 3) refuse to pass a bad number downstream
        if fav_p is None or not fav or not dog or not (0.0 < fav_p < 1.0):
            _apex_decline(prop, f"declined ({player}): no usable odds -> legacy")
            # APEX-CARRYFORWARD-2026-10-11 (owner, approved "yes, keep"
            # 2026-10-11): the prop IS still offered on PrizePicks -- only this
            # fetch's price lookup failed (SportsAPI365 listing gap / stale bet365).
            # Record the sighting so the board keeps its last good evaluation instead
            # of dropping it (Oct 11: Alcaraz v Bergs 16.5 MORE 83.5%, the day's #1
            # prop, vanished this way). Its card says the price wasn't refreshed, and
            # the refetch report lists it. Started matches and lines PrizePicks
            # removed still leave the board.
            if not prop.get("apex_reprice_job"):
                try:
                    _cf_cat = _APEX_STAT_MAP.get(str(prop.get("stat_type", "")).strip().lower().replace(" ", "_"))
                    _cf_line = "%.1f" % float(prop.get("line"))
                    _cf_ts = _dt_cf.now().isoformat(timespec="seconds")
                    _pa, _pb = str(prop.get("player") or ""), str(prop.get("opponent") or "")
                    if _cf_cat in _APEX_MATCH_LEVEL:
                        _apex_mark_seen("match", f"{_pa} v {_pb}", _cf_cat, _cf_line, _cf_ts, noprice=True)
                        _apex_mark_seen("match", f"{_pb} v {_pa}", _cf_cat, _cf_line, _cf_ts, noprice=True)
                    elif _cf_cat:
                        _apex_mark_seen(_pa, _pb, _cf_cat, _cf_line, _cf_ts, noprice=True)
                except Exception:
                    pass
            return None
        prop["apex_price_source"] = price_source

        # --- stat mapping
        slug = str(prop.get("stat_type", "")).strip().lower().replace(" ", "_")
        cat  = _APEX_STAT_MAP.get(slug)
        if not cat:
            _apex_decline(prop, f"declined ({player}): unmapped stat_type {slug!r} -> legacy")
            return None
        # APEX-WIRING-2026-08-27: binary categories always evaluate at 0.5.
        if cat in _APEX_BINARY:
            line = 0.5
        else:
            try:
                line = float(prop.get("line"))
            except (TypeError, ValueError):
                _apex_decline(prop, f"declined ({player}): unusable line -> legacy")
                return None

        key   = ("match|" + cat) if cat in _APEX_MATCH_LEVEL else (player + "|" + cat)
        bo5   = str(prop.get("match_format", "")).lower() == "bo5"
        wta   = str(prop.get("tour", "")).lower() == "wta"
        # APEX-EDGE-ZONE-2026-08-27: a five-setter cannot happen in a best-of-3.
        if cat == "goes_5_sets" and not bo5:
            _apex_decline(prop, f"declined ({player}): goes_5_sets on a best-of-3 match -> legacy")
            return None
        # APEX-SURFACEMAP-2026-10-10 (owner, approved 2026-10-11): surface
        # now comes from the tournament (match-history map -> old table -> hard +
        # flagged), not the fetch's hard-wired "clay". Re-simulating 4,139 settled
        # props on the correct surface: Brier 0.2332 -> 0.2289, 70%+ props
        # 67.6% -> 69.5%. Written back to prop["surface"] so logs show it.
        try:
            import apex_surface_map as _asm
            surf, _indoor, _ssrc = _asm.lookup(prop.get("apex_fixture_tournament") or prop.get("tournament") or "")
            if prop.get("surface") and str(prop.get("surface")).title() != surf and "UNMAPPED" not in _ssrc:
                _surf_note = (prop.get("apex_fixture_tournament") or prop.get("tournament"), str(prop.get("surface")), surf)
                if _surf_note not in _APEX_SURFACE_SEEN:
                    _APEX_SURFACE_SEEN.add(_surf_note)
                    log("Surface", f"{_surf_note[0]}: fetch said {_surf_note[1]}, using {surf} ({_ssrc})")
            prop["surface"] = surf.lower()
            prop["apex_indoor"] = _indoor
        except Exception as _se:
            surf = "Hard"
            log("Surface", f"surface lookup failed, using Hard (non-fatal): {_se}")

        import apex_prop_engine
        res = apex_prop_engine.evaluate_match(
            fav, dog, fav_p, surface=surf, bo5=bo5, wta=wta,
            lines={key: line}, indoor=prop.get("apex_indoor"))   # APEX-INDOOR-2026-10-11
        if not res or not res.get("ok"):
            # APEX-HOTFIX-2026-09-28 FIX #5: this branch used to call log()
            # directly and never stamped prop["apex_decline_reason"] -- the
            # caller only ever reads that stamped field, so every decline
            # through here showed up in Dataset A as the generic "engine
            # declined (no reason recorded)" placeholder even though a real
            # reason was available right here. _apex_decline() logs the
            # identical message AND stamps the reason, so this now matches
            # every other decline path in this function.
            _apex_decline(prop, f"declined ({player}): "
                          f"{(res or {}).get('error', 'no result')} -> legacy")
            return None

        target = None
        for p in res["props"]:
            if p["category"] != cat:
                continue
            if cat in _APEX_MATCH_LEVEL or _fragility_name_match(p["player"], player):
                target = p
                break
        if target is None or target.get("line") is None:
            _apex_decline(prop, f"declined ({player}): no {cat} row with a line -> legacy")
            return None

        # APEX-WIRING-2026-08-27: first_set_fav_wins is simulated from the
        # FAVORITE's side. When the card names the underdog, present the
        # underdog's side on the card and in the pick direction. The forward
        # log row is written from the unflipped target so it stays
        # favorite-relative, which is what --settle-sackmann compares against.
        _apex_flip = (cat == "first_set_fav_wins"
                      and _fragility_name_match(player, res.get("dog", ""))
                      and not _fragility_name_match(player, res.get("fav", "")))
        if _apex_flip:
            _ct = dict(target)
            _ct["p_over"], _ct["p_under"] = target.get("p_under"), target.get("p_over")
            try:
                _ct["mean"] = 1.0 - float(target.get("mean"))
                _ct["edge"] = _ct["mean"] - 0.5
                _ct["p10"], _ct["p90"] = 1.0 - float(target.get("p90")), 1.0 - float(target.get("p10"))
                _ct["p50"] = 1.0 - float(target.get("p50"))
            except (TypeError, ValueError):
                pass
            _ct["reasons"] = ["shown from the underdog's side (simulated as P(favorite wins set 1))"] + list(target.get("reasons") or [])[:2]
            card = _apex_engine_card(res, _ct, prop)
        else:
            card = _apex_engine_card(res, target, prop)

        # --- Dataset A: engine picks are tracked, and distinguishable
        try:
            d1.transition(eval_id, "ENGINE_PICK",
                           classification="ENGINE_PICK", _logger=log)
        except Exception:
            pass
        try:
            d1.heartbeat("evaluation", "IDLE", increment_success=True, _logger=log)
        except Exception:
            pass

        # APEX-ENGINE-ONLY-2026-08-24: take the LARGER side, not "over if >= 0.5".
        # On a whole-number line a push eats probability from both sides and the
        # old rule could pick the weaker one (Squire 22.0: over .495 / under .429
        # -> LESS 43%). PrizePicks refunds a push, so the reported probability is
        # among decided outcomes.
        _po = float(target.get("p_over") or 0.0)
        _pu = float(target.get("p_under") or 0.0)
        _push = max(0.0, 1.0 - _po - _pu)
        direction = "MORE" if _po >= _pu else "LESS"
        _raw      = _po if direction == "MORE" else _pu
        prob      = (_raw / (_po + _pu)) if (_po + _pu) > 0 else _raw
        if _apex_flip:
            # APEX-WIRING-2026-08-27: same probability, opposite label for the dog
            direction = "LESS" if direction == "MORE" else "MORE"
        target["push_prob"]  = round(_push, 4)
        target["p_pick_raw"] = round(_raw, 4)
        log("ApexEngine",
            f"ENGINE_PICK {player} {slug} {line} -> {direction} "
            f"p={prob:.3f} tier={target['tier']} (sim {res['sim_sha'][:8]})")

        # APEX-FLOOR-TO-ELIGIBILITY-2026-09-28: Goblin (72.1%) and Standard
        # (70.0%) floors no longer block the pick from being made, logged,
        # or published -- per directive, that was rejecting props from the
        # whole system, which was not the intended behavior. Every prop
        # still becomes a real, published pick with normal probability /
        # confidence / ranking. These two booleans (also written to the
        # picks log -- see _apex_forward_log_row) are read ONLY by Best
        # Goblin / Best Standard generation and headline recommendations
        # (apex_edge_tracker.py); Picks Channel, logging, historical
        # tracking, calibration tracking, and research monitoring never
        # filter on them and see every prop regardless of eligibility.
        _GOBLIN_BREAKEVEN_ENGINE = 0.721
        _STANDARD_MIN_WIN_PROB   = 0.70
        _ptype_elig = str(prop.get("prop_type_classified") or "")
        best_goblin_eligible   = not (_ptype_elig == "goblin"   and prob < _GOBLIN_BREAKEVEN_ENGINE)
        best_standard_eligible = not (_ptype_elig == "standard" and prob < _STANDARD_MIN_WIN_PROB)
        # APEX-FRESHSTATE-2026-10-20: a player who played this week but whose newest match
        # in the player file is 7+ days old gets a card line and stays off Best Standard.
        _stale_note = None
        try:
            import apex_state_topup as _ast
            _stale_note = _ast.stale_note(player, dates.get("match_date"))
        except Exception:
            _stale_note = None
        if _stale_note:
            best_standard_eligible = False
        _eligibility_note = None
        if _ptype_elig == "goblin" and not best_goblin_eligible:
            _eligibility_note = (f"⚠️ Below Best Goblin Threshold ({prob:.1%}) — "
                                  f"still tracked for research purposes.")
            log("ApexEngine", f"GOBLIN below Best-Goblin threshold (still published) — "
                              f"{player} {slug} {line} p={prob:.1%} < {_GOBLIN_BREAKEVEN_ENGINE:.1%}")
        elif _ptype_elig == "standard" and not best_standard_eligible:
            _eligibility_note = (f"⚠️ Below Best Standard Threshold ({prob:.1%}) — "
                                  f"still tracked for research purposes.")
            log("ApexEngine", f"STANDARD below Best-Standard threshold (still published) — "
                              f"{player} {slug} {line} p={prob:.1%} < {_STANDARD_MIN_WIN_PROB:.1%}")

        # APEX-PICKSCHANNEL-TIER-2026-09-28: cosmetic scan-tier line for the
        # Picks Channel card itself (every prop still gets one, floor or no
        # floor) + the below-floor research note when it applies. Tier is
        # purely a display label over the same real probability -- does not
        # change confidence, score, rank, or eligibility.
        if prob >= 0.70:
            _tier_line = f"🟢 Elite Pick ({prob:.1%})"
        elif prob >= 0.65:
            _tier_line = f"✅ Strong Pick ({prob:.1%})"
        elif prob >= 0.60:
            _tier_line = f"🟡 Lean ({prob:.1%})"
        elif prob >= 0.55:
            _tier_line = f"⚠️ Watchlist ({prob:.1%})"
        else:
            _tier_line = f"🔴 Below Edge Threshold ({prob:.1%})"
        card = f"{card}\n{_tier_line}"
        if _eligibility_note:
            card = f"{card}\n{_eligibility_note}"
        if _stale_note:
            card = f"{card}\n{_stale_note}"

        # ── APEX-FORWARD-LOG-2026-08-24: this pick exists outside Discord now
        try:
            _apex_forward_log_row(res, target, prop, player, opponent, cat, line, direction, prob, slug=slug)
        except Exception as _e:
            log("ApexEngine", f"forward log write failed (non-fatal): {_e}")
        return {
            "_apex_engine_card":   card,
            "_apex_engine":        True,
            "player":              player,
            "opponent":            opponent,
            "stat_type":           prop.get("stat_type", ""),
            "line":                line,
            "direction":           direction,
            "confidence":          round(float(prob), 4),
            "emoji":               {"elite": "🟢", "good": "🟡",
                                    "caution": "🟠", "low": "🔴"}.get(target["tier"], "🟡"),
            "match_type":          "ENGINE · FORWARD TEST",
            "one_sentence":        (target["reasons"] or [""])[0],
            "rules_confirmed":     [],
            "prop_type_classified": prop.get("prop_type_classified", "standard"),
            "compliance_penalty":  False,
            "compliance_flags":    [],
            "pipeline_flags":      ["ENGINE_PICK — legacy evaluation chain skipped"],
            "research_data":       {"player": {}, "opponent": {}},
            "evaluation":          card,
            "eval_id":             eval_id,
            "match_date":          dates.get("match_date"),
            "pipeline_run_date":   dates.get("pipeline_run_date"),
            "apex_sim_sha":        res["sim_sha"],
            "apex_v1_sha":         res["v1_sha"],
            "apex_tier":           target["tier"],
            "apex_best_of":        res.get("best_of", ""),
            "best_goblin_eligible":   best_goblin_eligible,     # APEX-FLOOR-TO-ELIGIBILITY-2026-09-28
            "best_standard_eligible": best_standard_eligible,   # APEX-FLOOR-TO-ELIGIBILITY-2026-09-28
            "eligibility_note":       _eligibility_note,        # APEX-FLOOR-TO-ELIGIBILITY-2026-09-28
        }
    except (Exception, SystemExit) as e:
        try:
            _apex_decline(prop, f"declined (exception: {type(e).__name__}: {e}) -> legacy")
        except Exception:
            pass
        return None


def _fragility_name_match(a, b):
    """APEX-ENGINE-PRIMARY-2026-08-23: loose name comparison, never raises."""
    try:
        import re as _re
        norm = lambda s: _re.sub(r"[^a-z ]", "", str(s).lower()).strip()
        na, nb = norm(a), norm(b)
        if na == nb:
            return True
        sa, sb = set(na.split()), set(nb.split())
        return bool(sa) and bool(sb) and (sa <= sb or sb <= sa)
    except Exception:
        return False


# ── APEX-FORWARD-LOG-2026-08-24: write every bot engine pick to the forward logs
_APEX_FWD_LOCK = None
_APEX_SIM_LOG  = "apex_prop_sim_forward_log_v1.csv"
_APEX_PICK_LOG = "apex_engine_picks_log_v1.csv"


_APEX_SEEN_FILE = os.path.join("data", "logs", "fetch_seen.json")
from datetime import datetime as _dt_cf   # APEX-CARRYFORWARD-2026-10-11


def _apex_mark_seen(player, opponent, cat, line, ts, noprice=False):
    """APEX-FETCHSEEN-2026-10-11: a fetch that finds a prop unchanged doesn't
    rewrite it, so the board's "was it in the latest scrape?" check used to
    miss it and retire a prop PrizePicks still offers. Record the sighting
    (identity -> ts) so load_current_board() can count it. Called under the
    forward-log lock. Never raises."""
    try:
        import json as _js
        try:
            with open(_APEX_SEEN_FILE) as fh:
                d = _js.load(fh)
        except Exception:
            d = {}
        k = "|".join([str(player or "").strip().lower(), str(opponent or "").strip().lower(),
                      str(cat or ""), str(line or "")])
        d[k] = str(ts)
        if len(d) > 6000:   # keep the newest sightings only
            d = dict(sorted(d.items(), key=lambda kv: kv[1])[-4000:])
        with open(_APEX_SEEN_FILE + ".tmp", "w") as fh:
            _js.dump(d, fh)
        os.replace(_APEX_SEEN_FILE + ".tmp", _APEX_SEEN_FILE)
        if noprice:   # APEX-CARRYFORWARD-2026-10-11: seen, but this fetch could not price it
            _np = _APEX_SEEN_FILE.replace("fetch_seen", "fetch_noprice")
            try:
                with open(_np) as fh:
                    n = _js.load(fh)
            except Exception:
                n = {}
            n[k] = str(ts)
            if len(n) > 3000:
                n = dict(sorted(n.items(), key=lambda kv: kv[1])[-2000:])
            with open(_np + ".tmp", "w") as fh:
                _js.dump(n, fh)
            os.replace(_np + ".tmp", _np)
    except Exception:
        pass


_APEX_LIVE_BOARD_FILE = os.path.join("data", "logs", "fetch_live_board.json")


def _apex_write_live_board(props, raw_n):
    """APEX-STALELINE-2026-10-14 (owner, "Go" 2026-10-14): record every
    prop the newest full partner-API pull offered, as (category, player,
    line, odds type). load_current_board() keeps a pick only if this list has
    its exact line and type. Before this, "latest fetch" was guessed from
    45-minute gaps between rows; fetches 30 min apart (5:00, 5:37, 6:09 PM
    on 10/14) chained into one run and Sakkari Fantasy Score 4.5 Goblin stayed
    on four boards after PrizePicks moved it to 6.5. Never raises."""
    try:
        import json as _js
        rows = []
        for p in props:
            cat = _APEX_STAT_MAP.get(p.get("stat_type"), p.get("stat_type"))
            ot = "goblin" if p.get("is_goblin") else "demon" if p.get("is_demon") else "standard"
            rows.append([cat, str(p.get("player") or "").strip().lower(), float(p.get("line")), ot])
        d = {"ts": _dt_cf.now().isoformat(timespec="seconds"), "count": len(rows), "raw": int(raw_n),
             "props": rows}
        with open(_APEX_LIVE_BOARD_FILE + ".tmp", "w") as fh:
            _js.dump(d, fh)
        os.replace(_APEX_LIVE_BOARD_FILE + ".tmp", _APEX_LIVE_BOARD_FILE)
        # the saved copy also names the opponent, so a line is traced to its exact match
        _apex_save_pull(dict(d, props=[r + [str(p.get("opponent") or "").strip().lower()] for r, p in zip(rows, props)]))
        import apex_two_pulls as _tp
        _tp.record(d["ts"], [r + [str(p.get("opponent") or "").strip().lower()] for r, p in zip(rows, props)])
    except Exception:
        pass


_APEX_PULL_DIR = os.path.join("data", "logs", "pull_history")
APEX_PULL_KEEP_DAYS = 45


def _apex_save_pull(d):
    """APEX-FEEDCHECK-2026-11-01 (owner, "Go feed check"): keep every pull's line list, one gzip file a
    day, so a dropped line can be traced pull by pull. Files older than 45 days go. Never raises."""
    try:
        import gzip as _gz, json as _js, glob as _gl
        os.makedirs(_APEX_PULL_DIR, exist_ok=True)
        day = d["ts"][:10]
        path = os.path.join(_APEX_PULL_DIR, day + ".jsonl.gz")
        fresh = not os.path.exists(path)
        with _gz.open(path, "at") as fh:
            fh.write(_js.dumps(d, separators=(",", ":")) + "\n")
        if fresh:
            cut = (_dt_cf.now() - timedelta(days=APEX_PULL_KEEP_DAYS)).strftime("%Y-%m-%d")
            for f in _gl.glob(os.path.join(_APEX_PULL_DIR, "*.jsonl.gz")):
                if os.path.basename(f)[:10] < cut:
                    os.remove(f)
    except Exception:
        pass


_APEX_SURFACE_SEEN = set()
APEX_REPRICE_MIN_MOVE = 0.02   # APEX-REPRICE-2026-10-11: price move (favorite prob) that triggers a re-score   # APEX-SURFACEMAP-2026-10-10: log each tournament's correction once


def _apex_start_utc(raw):
    """APEX-OPS-2026-10-10: PrizePicks start_time (e.g. 2026-10-10T04:00:00-04:00)
    -> UTC ISO string for slip_start_utc, which was never filled before, so the
    published board's match-started removal and closing-line capture had no
    start time to work from. '' if missing/unparseable."""
    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        from datetime import datetime as _dtu, timezone as _tzu
        _d = _dtu.fromisoformat(s.replace("Z", "+00:00"))
        if _d.tzinfo is None:
            _d = _d.replace(tzinfo=_tzu.utc)
        return _d.astimezone(_tzu.utc).isoformat(timespec="seconds")
    except Exception:
        return ""
_APEX_SIM_FIELDS = ["ts", "match_id", "match_date", "player", "opponent", "is_fav", "pinn_fav", "surface", "best_of",
                    "category", "mean", "sd", "p10", "p50", "p90", "fragility", "hold_fav", "hold_dog",
                    "book", "line", "p_over", "p_under", "actual", "settled_ts", "sim_sha", "v1_sha"]
_APEX_PICK_FIELDS = ["ts", "match_id", "match_date", "player", "opponent", "category", "stat_type", "line",
                     "direction", "prob", "tier", "label", "edge", "confidence", "surface", "best_of",
                     "tournament", "price_source", "main_total", "format_label", "pinn_fav", "sim_sha", "v1_sha",
                     "playable", "slip_start_utc",   # APEX-PLAYABLE-2026-08-31
                     "prop_type",                    # APEX-PROPTYPE-2026-09-08
                     "prob_cal",                      # APEX-ACESCAL-2026-09-09
                     "best_goblin_eligible", "best_standard_eligible"]  # APEX-FLOOR-TO-ELIGIBILITY-2026-09-28


def _apex_forward_log_row(res, t, prop, player, opponent, cat, line, direction, prob, slug=""):
    """Append one engine pick to the simulator's forward log (exact schema) and to
    the pick log. Same duplicate rule as apex_prop_sim_v1 --project: one row per
    (match_id, player, category). Never raises to the caller."""
    global _APEX_FWD_LOCK
    import csv, hashlib, threading
    from datetime import datetime as _dt, date as _date
    if _APEX_FWD_LOCK is None:
        _APEX_FWD_LOCK = threading.Lock()
    try:
        import apex_prop_model_v1 as _v1
        _norm = _v1.norm_name
    except Exception:
        _norm = lambda s: " ".join(str(s or "").lower().split())

    fav, dog = str(res.get("fav") or ""), str(res.get("dog") or "")
    match_date = _date.today().isoformat()
    tourn = str(prop.get("apex_fixture_tournament") or "")
    try:
        import apex_price_feed as _apf
        _fc = _apf.fixture_context(player, opponent, allow_spend=False)   # cache only, no call
        if _fc and _fc.get("start_time") and not prop.get("slip_start_ct"):
            match_date = str(_fc["start_time"])[:10]
        if _fc:
            tourn = tourn or str(_fc.get("tournament") or "")
    except Exception:
        pass
    # APEX-SLIP-TIME-2026-08-24: the slip's Central date is the match day
    try:
        if prop.get("slip_start_ct"):
            match_date = str(prop["slip_start_ct"])[:10]
    except Exception:
        pass
    match_id = hashlib.sha256("|".join([match_date, _norm(fav), _norm(dog)]).encode("utf-8")).hexdigest()[:12]
    try:   # APEX-MATCHID-2026-11-01: same two players, same meeting = one id through date moves and favorite flips
        import apex_match_identity as _mi
        match_id = _mi.resolve(match_date, _norm(fav), _norm(dog), match_id)
    except Exception:
        pass

    is_match   = (str(t.get("player") or "") == "match")
    row_player = "match" if is_match else str(t.get("player") or player)
    if is_match:
        row_opp, is_fav = f"{fav} v {dog}", -1
    else:
        on_fav = _norm(row_player) == _norm(fav)
        row_opp, is_fav = (dog if on_fav else fav), (1 if on_fav else 0)
    q = "%.1f" if cat == "fantasy_score" else "%.0f"
    def f(v, fmt):
        try:
            return fmt % float(v)
        except (TypeError, ValueError):
            return ""
    ts = _dt.now().isoformat(timespec="seconds")
    sim_row = {"ts": ts, "match_id": match_id, "match_date": match_date, "player": row_player, "opponent": row_opp,
               "is_fav": is_fav, "pinn_fav": f(res.get("pinn_fav"), "%.4f"), "surface": str(res.get("surface") or ""),
               "best_of": res.get("best_of", ""), "category": cat,
               "mean": f(t.get("mean"), "%.3f"), "sd": f(t.get("sd"), "%.3f"),
               "p10": f(t.get("p10"), q), "p50": f(t.get("p50"), q), "p90": f(t.get("p90"), q),
               "fragility": f(res.get("fragility"), "%.1f"), "hold_fav": f(res.get("hold_fav"), "%.4f"),
               "hold_dog": f(res.get("hold_dog"), "%.4f"),
               "book": "prizepicks", "line": f(line, "%.1f"), "p_over": f(t.get("p_over"), "%.3f"),
               "p_under": f(t.get("p_under"), "%.3f"), "actual": "", "settled_ts": "",
               "sim_sha": str(res.get("sim_sha") or ""), "v1_sha": str(res.get("v1_sha") or "")}
    # APEX-PLAYABLE-2026-08-31: honest direction is kept; playability is a
    # separate fact about what the book offers. "" = prop was never classified
    # (pre-classification evaluation) -- unknown, never guessed.
    _ptype = str(prop.get("prop_type_classified") or "")
    _lock  = str(prop.get("direction_locked") or "")
    if _ptype and _lock:
        _playable = "1" if direction == _lock.upper() else "0"
    elif _ptype:
        _playable = "1"
    else:
        _playable = ""
    if _playable == "0":
        log("ApexEngine", f"UNPLAYABLE: {row_player} {cat} {f(line, '%.1f')} -- "
                          f"{_ptype} is {_lock}-only, engine says {direction}. "
                          f"Honest pick kept, excluded from bettable ranking.")
    # APEX-FLOOR-TO-ELIGIBILITY-2026-09-28: recorded for Best Goblin / Best
    # Standard generation to read later -- never used to filter this log,
    # historical tracking, or calibration, all of which see every row.
    _pf = None
    try:
        _pf = float(prob)
    except (TypeError, ValueError):
        pass
    _best_goblin_eligible   = not (_ptype == "goblin"   and _pf is not None and _pf < 0.721)
    _best_standard_eligible = not (_ptype == "standard" and _pf is not None and _pf < 0.70)
    try:   # APEX-FRESHSTATE-2026-10-20: stale player file -> off Best Standard (card says why)
        import apex_state_topup as _ast
        if _ast.stale_note(row_player, match_date):
            _best_standard_eligible = False
    except Exception:
        pass
    pick_row = {"playable": _playable,
                "prop_type": _ptype,                     # APEX-PROPTYPE-2026-09-08
                "best_goblin_eligible":   _best_goblin_eligible,     # APEX-FLOOR-TO-ELIGIBILITY-2026-09-28
                "best_standard_eligible": _best_standard_eligible,   # APEX-FLOOR-TO-ELIGIBILITY-2026-09-28
                "slip_start_utc": str(prop.get("slip_start_utc") or _apex_start_utc(prop.get("start_time_raw")) or ""),   # APEX-OPS-2026-10-10: was always blank
                "ts": ts, "match_id": match_id, "match_date": match_date, "player": row_player, "opponent": row_opp,
                "category": cat, "stat_type": str(slug or prop.get("stat_type") or ""), "line": f(line, "%.1f"),
                "direction": direction, "prob": f(prob, "%.4f"),
                "prob_cal": f(_apex_prob_cal(prob, cat, direction, is_fav), "%.4f"),   # APEX-ACESCAL-2026-09-09
                "tier": str(t.get("tier") or ""),
                "label": str(t.get("label") or ""), "edge": f(t.get("edge"), "%.2f"),
                "confidence": str(t.get("confidence") or ""), "surface": str(res.get("surface") or ""),
                "best_of": res.get("best_of", ""), "tournament": tourn,
                "price_source": str(prop.get("apex_price_source") or ""),
                "main_total": "" if prop.get("apex_main_total") is None else prop.get("apex_main_total"),
                "format_label": str(prop.get("format_label") or ""), "pinn_fav": f(res.get("pinn_fav"), "%.4f"),
                "sim_sha": str(res.get("sim_sha") or ""), "v1_sha": str(res.get("v1_sha") or "")}

    with _APEX_FWD_LOCK:
        # APEX-FULLTIER-LOGGING-2026-09-29: this used to dedup on
        # (match_id, player, category) alone -- which made every
        # alternate line/tier for the same player+market collide on the
        # exact same key. Live audit: 603 of 622 full-tier evaluations
        # this run hit "duplicate ... not rewritten" and never reached
        # the pick log, because a Goblin at one line and a Demon at
        # another line for the same player+category looked identical to
        # this check. line and prop_type are now part of the identity --
        # Games Won 12.5 Goblin and Games Won 13.5 Demon are different
        # picks and both get written. Reads from _APEX_PICK_LOG (not
        # _APEX_SIM_LOG) because it's the file that actually carries
        # line and prop_type; both logs are still written together
        # below, atomically, under this same lock.
        # APEX-REPRICE-2026-10-11 (owner, approved "go" 2026-10-11): a prop
        # already in the log is re-written ONLY when its surface changed
        # (APEX-SURFACEMAP fix) or its match price moved 2+ points since the last
        # logged row (re-pricing test: Brier 0.2320 -> 0.2295 on moved matches).
        # A re-write appends to the PICK log only -- the forward log keeps its one
        # row per prop, so settlement never counts the same prop twice; boards
        # and settled records read the newest pick-log row.
        existing = {}
        if os.path.exists(_APEX_PICK_LOG):
            with open(_APEX_PICK_LOG, newline="") as fh:
                for r in csv.DictReader(fh):
                    # background re-scores ("reprice:") don't count -- a real fetch
                    # must still confirm the prop, or the board would retire it
                    if str(r.get("price_source") or "").startswith("reprice:") and not prop.get("apex_reprice_job"):
                        continue
                    existing[(r.get("match_id"), r.get("player"), r.get("category"),
                              r.get("line"), r.get("prop_type"))] = (
                        str(r.get("surface") or "").title(), r.get("pinn_fav"), str(r.get("match_date") or ""))
        _dedup_line = f(line, "%.1f")
        key = (match_id, row_player, cat, _dedup_line, _ptype)
        _refresh = False
        # APEX-DATEMOVE-2026-10-25: a prop re-scored because PrizePicks moved its match gets a
        # new match_id (it hashes the date) -- pick log only, so the forward log keeps one row
        # per prop and settlement never counts it twice.
        if prop.get("apex_date_moved") and key not in existing:
            _refresh = True
            pick_row["price_source"] = "refresh:" + str(pick_row.get("price_source") or "")
            log("ApexEngine", f"pick refresh: {row_player} {cat} {_dedup_line} {_ptype} (match moved to {match_date})")
        # APEX-MATCHID-2026-11-01: a moved match keeps its id, so a new date on the same key is a re-write.
        if key in existing and existing[key][2] != str(match_date):
            _refresh = True
            pick_row["price_source"] = ("reprice:" if prop.get("apex_reprice_job") else "refresh:") + str(pick_row.get("price_source") or "")
            log("ApexEngine", f"pick refresh: {row_player} {cat} {_dedup_line} {_ptype} (match moved to {match_date})")
        elif key in existing:
            _old_surf, _old_pinn = existing[key][:2]
            _new_surf = str(res.get("surface") or "").title()
            try:
                _moved = abs(float(res.get("pinn_fav")) - float(_old_pinn)) >= APEX_REPRICE_MIN_MOVE
            except (TypeError, ValueError):
                _moved = False
            # APEX-AUTOEVAL-TOUR-2026-10-15: the one-time WTA re-score sets
            # apex_force_refresh so a corrected tour is written even when the
            # price and surface are unchanged.
            if _old_surf == _new_surf and not _moved and not prop.get("apex_force_refresh"):
                log("ApexEngine",
                    f"forward log: duplicate {match_id} {row_player} {cat} "
                    f"{_dedup_line} {_ptype} — not rewritten")
                prop["apex_forward_dup"] = True   # APEX-FIXDUP-2026-11-02: tells auto-eval this prop is already scored
                if not prop.get("apex_reprice_job"):
                    _apex_mark_seen(row_player, row_opp, cat, _dedup_line, ts)
                return False
            _refresh = True
            log("ApexEngine", f"pick refresh: {row_player} {cat} {_dedup_line} {_ptype} "
                              f"({'surface ' + _old_surf + '->' + _new_surf if _old_surf != _new_surf else ''}"
                              f"{' price ' + str(_old_pinn) + '->' + str(res.get('pinn_fav')) if _moved else ''}"
                              f"{' tour fix' if prop.get('apex_force_refresh') else ''})")
            # "reprice:" = re-scored by the background job (PrizePicks not re-checked);
            # "refresh:" = re-scored during a real fetch. Freshness ignores "reprice:".
            pick_row["price_source"] = ("reprice:" if prop.get("apex_reprice_job") else "refresh:") + str(pick_row.get("price_source") or "")
        if not _refresh:
            wh = not os.path.exists(_APEX_SIM_LOG)
            with open(_APEX_SIM_LOG, "a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=_APEX_SIM_FIELDS)
                if wh:
                    w.writeheader()
                w.writerow(sim_row)
        wh2 = not os.path.exists(_APEX_PICK_LOG)
        with open(_APEX_PICK_LOG, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_APEX_PICK_FIELDS)
            if wh2:
                w.writeheader()
            w.writerow(pick_row)
    log("ApexEngine", f"forward log: wrote {match_id} {row_player} {cat} {sim_row['line']} {direction} "
                      f"p={sim_row['p_over'] if direction == 'MORE' else sim_row['p_under']} -> {_APEX_SIM_LOG}")
    return True


def _apex_engine_card(res, t, prop):
    """APEX-ENGINE-PRIMARY-2026-08-23: the Discord card. Never raises.

    APEX-PICKSCHANNEL-REORDER-2026-10-05 (Phase 7, owner): reordered
    per spec -- Board Type -> Match/Market/Line/Direction -> Apex Score ->
    Confidence -> Win Probability -> Board Rank -> Why Apex Likes It (max 5
    bullets) -> Historical Performance -> Risk; projection/engine-ID/SHA
    data (the old top-of-card content) moves to the bottom, unchanged in
    substance. Apex Score/Confidence are now the SAME apex_edge_tracker
    apex_score()/apex_confidence() computation Top10/Elite6/Best-[Type]
    use (rec = records(), decoupled from today's board -- historical
    settled-picks only, so this never needs the rest of today's fetch to
    exist first). Board Rank is honestly NOT computable here: a single
    prop is scored in isolation, before the rest of today's board exists,
    so this says so plainly and points to the board commands instead of
    faking a number. Wrapped so a failure in the new Apex Score lookup
    degrades to the old card content, never to a blank/broken post."""
    try:
        badge = {"elite": "\U0001F7E2 ELITE", "good": "\U0001F7E1 GOOD",
                 "caution": "\U0001F7E0 CAUTION", "low": "\U0001F534 LOW CONFIDENCE"}.get(t["tier"], "\U0001F7E1 GOOD")
        _po = float(t.get("p_over") or 0.0)
        _pu = float(t.get("p_under") or 0.0)
        _push = max(0.0, 1.0 - _po - _pu)
        over  = _po >= _pu
        side  = "MORE \u2b06" if over else "LESS \u2b07"
        _raw  = _po if over else _pu
        prob  = (_raw / (_po + _pu)) if (_po + _pu) > 0 else _raw
        L = []
        _aet = None
        try:
            import apex_edge_tracker as _aet
            _po, _pu = float(t.get("p_over") or 0), float(t.get("p_under") or 0)
            _hdr = _aet.discord_header(t.get("category"), prob=((max(_po, _pu) / (_po + _pu)) if (_po + _pu) > 0 else None), bo5=(str(res.get("best_of") or "") == "5"))
            if _hdr:
                L.append(_hdr)
        except Exception:
            pass
        L.append("**EXPERIMENTAL \u00b7 FORWARD TEST ACTIVE**")

        # 1. BOARD TYPE
        _ptype = str(prop.get("prop_type_classified") or "standard").lower()
        _ptype_tag = {"red_demon": "\U0001F534\U0001F608 RED DEMON", "demon": "\U0001F534 DEMON",
                      "goblin": "\U0001F608 GOBLIN"}.get(_ptype, "\u26aa STANDARD")
        L.append("\U0001F3F7\ufe0f Board Type: " + _ptype_tag)

        # 2. MATCH / MARKET / LINE / DIRECTION
        _pname = t['player'] if t['player'] != 'match' else prop.get('player', '')
        L.append(badge + " \u00b7 " + t['label'])
        L.append("**%s** vs %s \u00b7 %s \u00b7 Bo%s" % (_pname, prop.get('opponent',''), res['surface'], res['best_of']))
        L.append("`%s %s \u2014 %s`" % (t['line'], prop.get('stat_type',''), side))

        # 3-6. APEX SCORE / CONFIDENCE / WIN PROBABILITY / BOARD RANK
        _apex_sc = _apex_cf = _apex_cwhy = None
        _hist_line = None
        if _aet is not None:
            try:
                _rec = _aet.records()
                _cat = t.get("category")
                _bo5 = (str(res.get("best_of") or "") == "5")
                _apex_sc, _ev, _lvl, _up, _down = _aet.apex_score(prob, _cat, _bo5, _rec)
                try:
                    import apex_card_index as _CI
                    _mdate = prop.get("match_date") or prop.get("date") or ""
                    if str(_pname).lower() == "match" or not _pname:
                        _card = _CI.lookup_match(str(prop.get('opponent') or ""), _cat, str(_mdate))[0]
                    else:
                        _card = _CI.lookup(_pname, _cat, str(_mdate))[0]
                except Exception:
                    _card = None
                _apex_cf, _ctier, _apex_cwhy = _aet.apex_confidence(
                    _cat, _bo5, _rec, score=_apex_sc, prob=prob, evidence=_ev,
                    attention=_lvl, warnings=_down, card_present=bool(_card))
                _cs = _rec["cats"].get(_cat) or {"w": 0, "l": 0, "n": 0, "rate": None}
                if _cs["n"]:
                    _cat_label = _aet.LABEL.get(_cat, _cat) if hasattr(_aet, "LABEL") else _cat
                    _hist_line = "%s: %d-%d (%.1f%%, n=%d)" % (_cat_label, _cs['w'], _cs['l'], (_cs['rate'] or 0) * 100, _cs['n'])
                else:
                    _hist_line = "no settled picks yet for this category"
            except Exception:
                _apex_sc = None
        if _apex_sc is not None:
            L.append("\U0001F3C6 Apex Score: %d" % round(_apex_sc))
            L.append("\u2b50 Confidence: %s/100" % (round(_apex_cf) if _apex_cf is not None else "?"))
            try:
                _floor_ptype = "demon" if _ptype == "red_demon" else _ptype
                _pc_floor = _aet.QUALITY_FLOOR_BY_TYPE.get(_floor_ptype, _aet.DEFAULT_QUALITY_FLOOR)
                if _apex_sc >= _pc_floor:
                    L.append("\u2705 Clears %s Quality Floor (%d+)" % (_floor_ptype.capitalize(), _pc_floor))
                else:
                    L.append("\U0001F6A6 Below %s Quality Floor (%d+) -- published for tracking, won't appear on the Best %s list" % (_floor_ptype.capitalize(), _pc_floor, _floor_ptype.capitalize()))
            except Exception:
                pass
        else:
            L.append("\U0001F3C6 Apex Score: unavailable this card (see probability below)")
        _pcal = _apex_prob_cal(prob, t.get("category"), side, res.get("is_fav", -1))
        if _pcal >= prob - 1e-9:
            L.append("\U0001F4CA Win Probability: **%.0f%%**" % (100 * prob))
        else:
            L.append("\U0001F4CA Win Probability: **%.0f%%** \u00b7 calibrated **%.0f%%**" % (100 * prob, 100 * _pcal))
        L.append("\U0001F4CD Board Rank: not available on a single-prop card -- this prop is scored "
                 "before today's full board exists. See /top10, /elite6, or the Fetch "
                 "Completion Report once this fetch finishes.")

        # 7. WHY APEX LIKES IT (max 5 bullets)
        _why_bullets = list((t.get("reasons") or [])[:3])
        if _apex_cwhy:
            for w in _apex_cwhy:
                if w not in _why_bullets:
                    _why_bullets.append(w)
        if _why_bullets:
            L.append("\U0001F9E0 Why Apex Likes It:")
            for why in _why_bullets[:5]:
                L.append("  \u2022 " + str(why))

        # 8. HISTORICAL PERFORMANCE
        if _hist_line:
            L.append("\U0001F4DA Historical Performance: " + _hist_line)

        # 9. RISK
        _risk_bits = ["Fragility %s" % res['fragility_text']]
        if _pcal < prob - 1e-9:
            _risk_bits.append("aces LESS runs %.1f pts under its stated probability "
                               "(measured on 11,341 holdout matches) -- the "
                               "calibrated figure is what break-even, APEX and RANK use" % (100 * (prob - _pcal)))
        if _push >= 0.02:
            _risk_bits.append("push %.0f%% at %s (refunded) \u2014 probability "
                               "is among decided outcomes" % (100 * _push, t['line']))
        L.append("\u26a0\ufe0f Risk: " + " \u00b7 ".join(_risk_bits))

        try:   # APEX-FRESH-LADDER-2026-11-09 (owner): how fresh the line is + the PrizePicks ladder, like the sports cards
            import importlib as _il, apex_card_extras as _ACE
            for _x in _ACE.lines(prop, t, _pname): L.append(_x)
        except Exception:
            pass
        # ---- technical / projection / engine-ID data, moved to the bottom ----
        L.append("\u2500" * 30)
        L.append("Projection **%.1f**  (10-90: %.1f\u2013%.1f)   Edge %+.1f   Confidence %s"
                 % (t['mean'], t['p10'], t['p90'], t['edge'], t['confidence']))
        L.append("_Break-even \u224854% (6-pick flex) / 58% (power play)_")
        L.append("`engine sim %s / V1 %s`" % (res['sim_sha'][:12], res['v1_sha'][:12]))
        return "\n".join(L)
    except Exception:
        return ""


def finalize_batch_ml_evaluation(batch_state, eval_text, stop_reason, dates):
    """
    Post-evaluation phase for a batch moneyline evaluation. Takes the
    batch_state produced by prepare_batch_ml_prop() and the model
    response text from the Batch API, and runs the full post-model
    pipeline: cross-check, compliance, Dataset A, posting.

    Mirrors evaluate_moneyline_screenshot() + the post-model section
    of moneyline_evaluation_agent() exactly. Posts with ⏳ prefix so
    the operator knows it came from the batch path.

    Called from openclaw_intake._batch_poller() for ml_image batch items.
    """
    player        = batch_state["player"]
    opponent      = batch_state["opponent"]
    surface       = batch_state["surface"]
    tour          = batch_state.get("tour", "atp")
    tournament    = batch_state.get("tournament", "")
    tournament_id = batch_state.get("tournament_id")
    player_odds   = batch_state.get("player_odds", "")
    opponent_odds = batch_state.get("opponent_odds", "")
    precomputed   = batch_state.get("precomputed", {})
    eval_id       = batch_state["eval_id"]

    if not batch_state.get("tour_confirmed", True):
        send_discord("alerts",
            f"⚠️ **TOUR UNCONFIRMED (batch)** — {player} vs {opponent}\n"
            f"Processed with tour='{tour.upper()}' as best guess.")

    # ── Truncation check ───────────────────────────────────────────────
    if stop_reason == "max_tokens":
        diag = {"output_tokens_used": "?", "max_tokens_limit": 6000}
        log("BatchML",
            f"SYSTEM FAILURE — {player} vs {opponent} truncated. No pick.")
        send_discord("alerts",
            f"🛑 **BATCH MONEYLINE TRUNCATED** — {player} vs {opponent}\n"
            f"Evaluation hit max_tokens before reaching a conclusion. No pick.")
        d1.terminate(eval_id, "EVAL_TRUNCATED", str(diag), _logger=log)
        return None

    d1.heartbeat("evaluation", "WORKING",
                 current_task=f"{player} vs {opponent}", _logger=log)

    # ── Cross-check: model vs pre-computed ────────────────────────────
    model_says_no_play = bool(
        re.search(r"NO\s*PLAY",
                  eval_text[int(len(eval_text) * 0.6):], re.IGNORECASE))
    model_winner_match = re.search(
        r"([A-Za-z .\-']+)\s*—\s*MONEYLINE WIN",
        eval_text[int(len(eval_text) * 0.6):]
    )
    model_winner = (model_winner_match.group(1).strip()
                    if model_winner_match else None)

    final = dict(precomputed)
    final["model_output"]      = eval_text
    final["model_winner"]      = model_winner
    final["model_said_no_play"] = model_says_no_play
    final["eval_id"]           = eval_id

    # Calibration signal tracking (same as moneyline_evaluation_agent)
    # S2/S4 removed 2026-08-07 -- retired from live signals, see the
    # real backtest evidence in evaluate_moneyline()'s comment block.
    # S6 added same day after its own real backtest (AUC 0.7223).
    _signal_to_rule = {
        "s1": "ML_S1_Surface_Bands", "s5": "ML_S5_Classification_Bands",
        "s6": "ML_S6_ELO_Bands",
    }
    for _sig_key, _rule_name in _signal_to_rule.items():
        if final.get("signals", {}).get(_sig_key) is not None:
            record_provisional_rule_fire(_rule_name, eval_id, player=player)

    precomputed_winner = precomputed.get("winner")
    if precomputed["status"] == "NO_PLAY" or model_says_no_play:
        final["status"] = "NO_PLAY"
        if precomputed["status"] != "NO_PLAY":
            final["reason"] = ("Model's own reasoning concluded NO PLAY "
                                "despite Phase 2 agreement")
    elif (model_winner and precomputed_winner
          and model_winner.lower() != precomputed_winner.lower()):
        final["status"] = "NO_PLAY"
        final["reason"] = (
            f"Cross-check failure: model said {model_winner}, "
            f"pre-computed said {precomputed_winner} — disagreement forces NO PLAY")
        log("BatchML", f"⚠️ CROSS-CHECK DISAGREEMENT — {final['reason']}")

    if final["status"] == "NO_PLAY":
        d1.no_play(eval_id, final.get("reason", "no play"),
                   advisory=final.get("advisory"),
                   suggested_winner=final.get("suggested_winner"),
                   suggested_confidence=final.get("suggested_confidence"),
                   signals=final.get("signals"),
                   dominance=final.get("dominance"),
                   phase2_band=final.get("phase2_band"),
                   phase2_confirmation_status=final.get("phase2_confirmation_status"),
                   confirmed_signal_count=final.get("confirmed_signal_count"),
                   _logger=log)
        d1.heartbeat("evaluation", "IDLE", increment_success=True, _logger=log)
    else:
        d1.transition(eval_id, "EVALUATION_COMPLETE",
                       eval_confidence=(final["confidence"] / 100.0
                                        if final.get("confidence") is not None
                                        else None),
                       reasoning_text=eval_text[:20000],
                       signals=final.get("signals"),
                       dominance=final.get("dominance"),
                       phase2_band=final.get("phase2_band"),
                       phase2_confirmation_status=final.get("phase2_confirmation_status"),
                       confirmed_signal_count=final.get("confirmed_signal_count"),
                       player_odds=player_odds, opponent_odds=opponent_odds,
                       _logger=log)
        d1.heartbeat("evaluation", "IDLE", increment_success=True, _logger=log)

    # ── Compliance (same logic as moneyline_evaluation_agent) ──────────
    if final.get("status") not in ("ERROR", "NO_PLAY"):
        compliance_input = {
            "player":            player,
            "stat_type":         "moneyline",
            "line":              None,
            "direction":         final.get("winner") or final.get("model_winner"),
            "match_type":        final.get("match_type", ""),
            "is_favorite":       final.get("is_favorite"),
            "evaluation_output": eval_text,
            "research_data":     final.get("research_data", {}),
        }
        d1.heartbeat("compliance", "WORKING",
                     current_task=f"{player} vs {opponent}", _logger=log)
        audit = compliance_agent(compliance_input, attempt=1)
        final["compliance_status"] = audit["status"]
        final["compliance_flags"]  = audit.get("flags", [])

        if audit["status"] == "FLAGGED_FOR_MANUAL_REVIEW":
            final["status"] = "SYSTEM_FAILURE"
            final["confidence"] = None
            final["winner"] = None
            send_discord("alerts",
                f"🛑 **BATCH MONEYLINE COMPLIANCE FAILED** — {player} vs {opponent}\n"
                f"Compliance audit could not be parsed. No pick generated.")
            d1.terminate(eval_id, "COMPLIANCE_UNPARSEABLE", _logger=log)
            d1.heartbeat("compliance", "FAILED", detail="unparseable",
                         increment_failure=True, _logger=log)
            return None
        elif audit["status"] not in ("APPROVED",):
            a_flags = [f for f in (audit.get("flags") or [])
                       if f.get("category", "A") == "A"]
            penalty_pts = min(len(a_flags) * 3, 15)
            if final.get("confidence") is not None:
                final["confidence"] = max(0, final["confidence"] - penalty_pts)
            final["compliance_note"] = (
                f"⚠️ Compliance penalty: -{penalty_pts}pp "
                f"({len(a_flags)} Category-A flag(s))"
                if a_flags else "")
        d1.transition(eval_id, "COMPLIANCE_COMPLETE",
                       compliance_category=(
                           "A" if any(f.get("category", "A") == "A"
                                      for f in (final.get("compliance_flags") or []))
                           else ("B" if final.get("compliance_flags") else None)
                       ), _logger=log)
        d1.heartbeat("compliance", "IDLE", increment_success=True, _logger=log)

    # ── Save reasoning ─────────────────────────────────────────────────
    save_evaluation_reasoning(player, opponent, "moneyline",
                               eval_text, dates, target_dir=ML_ARCHIVE_DIR)

    # ── Post — ⏳ prefix so operator knows it's a batch result ─────────
    status = final.get("status", "NO_PLAY")
    if status == "SYSTEM_FAILURE":
        return final

    if status in ("GREEN", "YELLOW"):
        color  = "\U0001f7e2" if status == "GREEN" else "\U0001f7e1"
        winner = final.get("winner", player)
        conf   = final.get("confidence", "--")
        implied = format_american_odds(confidence_to_american_odds(conf))
        context_lines = moneyline_context_lines(player, opponent, final.get("research_data", {}))
        fav_line = favorite_underdog_line(player, player_odds, opponent, opponent_odds)
        one_sentence  = _extract_one_sentence(eval_text)
        compact = (
            f"⏳ {color} **{winner} -- MONEYLINE WIN** (BATCH)\n"
            f"Confidence: {conf}% -- {status}\n"
            f"Apex implied fair odds: {implied} (derived from confidence, not a market price)\n"
            f"Surface: {surface.title()} | {tournament.title()}\n"
            f"Odds: {player} {player_odds} / {opponent} {opponent_odds}"
            + (f"\n{fav_line}" if fav_line else "")
            + (f"\n{context_lines}" if context_lines else "")
            + (f"\n*{one_sentence}*" if one_sentence else "")
        )
        if final.get("compliance_note"):
            compact += f"\n{final['compliance_note']}"
        # APEX-FRAGILITY-WIRE-2026-08-21: informational block, appended last
        _frag_block = _fragility_block(player, opponent, player_odds,
                                       opponent_odds, surface, tour, tournament)
        if _frag_block:
            compact += "\n" + _frag_block
        _post_ok = send_discord("moneyline", compact)
        d1.heartbeat("posting", "WORKING",
                     detail=f"{player} vs {opponent}", _logger=log)
        if _post_ok:
            d1.transition(eval_id, "POSTED", posted=1, _logger=log)
            d1.heartbeat("posting", "IDLE", increment_success=True, _logger=log)
        else:
            d1.transition(eval_id, "SCORING_COMPLETE",
                           detail="Discord post failed — pick never reached Discord",
                           posted=0, predicted_winner=final.get("winner"), _logger=log)
            d1.heartbeat("posting", "IDLE", increment_failure=True, _logger=log)
    else:
        reason  = final.get("reason", "Insufficient signal agreement")
        if final.get("advisory"):
            sug_conf = final.get("suggested_confidence")
            implied  = format_american_odds(confidence_to_american_odds(sug_conf))
            compact = (
                f"NO PLAY (SUGGESTED) -- {player} vs {opponent}\n"
                f"Surface: {surface.title()} | {tournament.title()}\n"
                f"Reason: {reason}\n"
                f"Phase 1 leaned {final.get('suggested_winner')} at {sug_conf}% "
                f"(implied {implied}) -- confirmation signals disagreed, use your own judgment"
            )
        else:
            compact = (
                f"NO PLAY -- {player} vs {opponent}\n"
                f"Surface: {surface.title()} | {tournament.title()}\n"
                f"Reason: {reason}"
            )
        send_discord("moneyline", compact)

    dom       = final.get("dominance", {})
    breakdown = dom.get("breakdown", {})
    summary_lines = [
        f"⏳ Batch Moneyline — {player} vs {opponent}",
        f"Tournament: {tournament.title()} | Surface: {surface.title()} | Tour: {tour.upper()}",
        f"Phase 1: {dom.get('dominance_score','--')} → {dom.get('predicted_side','--')}",
    ]
    if breakdown:
        summary_lines.append(
            f"Serve: {breakdown.get('serve_edge','--')} | "
            f"Return: {breakdown.get('return_edge','--')} | "
            f"Clutch: {breakdown.get('clutch_edge','--')} | "
            f"Form: {breakdown.get('form_edge','--')}"
        )
    summary_lines.append(f"Result: {status} | Reasoning saved to moneyline archive.")
    send_discord("system", "\n".join(summary_lines))
    send_discord("general",
        f"✅ ⏳ Batch moneyline complete — {player} vs {opponent}. "
        f"See #moneyline-channel.")
    return final


def finalize_batch_evaluation(prop, eval_text, stop_reason, dates, weights):
    """
    Post-evaluation phase for a batch prop. Called by the batch poller
    in openclaw_intake after the Anthropic Batch API returns the
    evaluation model's response.

    Runs: evaluation_agent (using precomputed text) → compliance loop
    → scoring → Discord post → reasoning save.

    Same integrity guarantees as process_single_prop's live path:
    identical compliance logic, identical scoring_agent call, identical
    Category-A retry decision, identical posting format. The only
    differences are (a) the model call already happened via batch API,
    and (b) the pick card has a ⏳ BATCH prefix in Discord so the
    operator knows it came from a batch evaluation.

    prop must have been produced by prepare_batch_prop (has eval_id,
    research_data, gate state, and Dataset A already started).
    """
    eval_id        = prop.get("eval_id")
    pipeline_flags = prop.get("_pipeline_flags", [])

    # ── Run evaluation_agent with precomputed batch text ─────────────
    d1.heartbeat("evaluation", "WORKING", current_task=prop.get("player"), _logger=log)
    evaluated = evaluation_agent(
        prop,
        _precomputed_eval_text=eval_text,
        _precomputed_stop_reason=stop_reason,
        _precomputed_usage={},
    )
    if not evaluated:
        d1.terminate(eval_id, "EVAL_ATTEMPTS_EXHAUSTED", _logger=log)
        d1.heartbeat("evaluation", "FAILED",
                     detail="evaluation_agent returned None from batch result",
                     increment_failure=True, _logger=log)
        send_discord("alerts",
            f"🛑 **BATCH EVAL FAILED** — "
            f"{prop.get('player')} {prop.get('stat_type')} {prop.get('line')}\n"
            f"evaluation_agent returned None processing the batch result.")
        return None

    if evaluated.get("_truncated"):
        diag = evaluated.get("_truncation_diagnostics", {})
        d1.terminate(eval_id, "EVAL_TRUNCATED", str(diag), _logger=log)
        d1.heartbeat("evaluation", "FAILED", detail="truncated",
                     increment_failure=True, _logger=log)
        send_discord("alerts",
            f"🛑 **BATCH EVAL TRUNCATED** — "
            f"{prop.get('player')} {prop.get('stat_type')}\n"
            f"Batch response hit max_tokens before reaching a conclusion. "
            f"No pick generated.")
        return None

    d1.transition(eval_id, "EVALUATION_COMPLETE",
                   direction=evaluated.get("direction"),
                   dominance_level=evaluated.get("dominance_level"),
                   gate11b_dominance_key=evaluated.get("dominance_level"),
                   classification=evaluated.get("match_type"),
                   eval_confidence=evaluated.get("eval_confidence"),
                   req_count=evaluated.get("competitive_reqs"),
                   reasoning_text=evaluated.get("evaluation_output"),
                   _logger=log)
    d1.heartbeat("evaluation", "IDLE", increment_success=True, _logger=log)

    # ── Compliance loop — mirrors process_single_prop exactly ─────────
    compliance_penalty = False
    compliance_flags   = []
    audit              = None

    for attempt in range(1, 3):
        d1.heartbeat("compliance", "WORKING",
                     current_task=prop.get("player"), _logger=log)
        audit = compliance_agent(evaluated, attempt)

        if audit["status"] == "APPROVED":
            break

        if audit["status"] == "ACCEPTED_WITH_PENALTY":
            compliance_penalty = True
            compliance_flags   = audit.get("flags", [])
            break

        if audit["status"] == "FLAGGED_FOR_MANUAL_REVIEW":
            d1.terminate(eval_id, "COMPLIANCE_UNPARSEABLE", _logger=log)
            d1.heartbeat("compliance", "FAILED", detail="unparseable",
                         increment_failure=True, _logger=log)
            send_discord("alerts",
                f"🛑 **BATCH COMPLIANCE FAILED** — "
                f"{evaluated.get('player')} {prop.get('stat_type')}\n"
                f"Compliance audit could not be parsed. No pick generated.")
            return None

        if audit["status"] == "FLAGGED" and attempt == 1:
            _a_only = [f for f in (audit.get("flags") or [])
                       if f.get("category", "A") != "B"]
            if not _a_only:
                # B-only flags — accept with note, no retry
                compliance_penalty = True
                compliance_flags   = audit.get("flags", [])
                break
            # Category-A flag on attempt 1 — re-evaluate synchronously
            # (compliance retry on a batch prop is live, not batch: the
            # retry is rare and we need it fast, not in 8 hours).
            prop["compliance_flags"]  = audit.get("flags", [])
            prop["is_re_evaluation"]  = True
            evaluated = evaluation_agent(prop)  # live call
            if not evaluated:
                break
        else:
            compliance_penalty = True
            compliance_flags   = audit.get("flags", [])
            break

    if not evaluated:
        d1.terminate(eval_id, "EVAL_ATTEMPTS_EXHAUSTED", _logger=log)
        return None

    # Calibration tracking — same as process_single_prop
    _truth_lock = "FLAGGED" if any(
        "truthlock" in str(f.get("rule", "")).lower()
        for f in (compliance_flags or [])
    ) else "PASS"
    _rule30_triggered = any(
        f.get("rule") == "Rule30" for f in (compliance_flags or []))
    d1.transition(eval_id, "COMPLIANCE_COMPLETE",
                   compliance_category=(
                       "A" if any(f.get("category", "A") == "A"
                                  for f in (compliance_flags or []))
                       else ("B" if compliance_flags else None)
                   ),
                   compliance_notes=(json.dumps(compliance_flags, default=str)
                                     if compliance_flags else None),
                   truth_lock_status=_truth_lock,
                   rule30_triggered=int(_rule30_triggered),
                   _logger=log)
    d1.heartbeat("compliance", "IDLE", increment_success=True, _logger=log)

    # ── Scoring ───────────────────────────────────────────────────────
    scored = scoring_agent(evaluated, weights, compliance_penalty,
                            compliance_flags, eval_id=eval_id)
    if scored.get("confidence") is None:
        # scoring_agent already called d1.terminate() and sent an alert
        return None

    scored.update({
        "eval_id":             eval_id,
        "pipeline_flags":      pipeline_flags,
        "compliance_flags":    compliance_flags,
        "compliance_penalty":  compliance_penalty,
        "evaluation_attempts": 1,
        "match_date":          dates["match_date"],
        "pipeline_run_date":   dates["pipeline_run_date"],
    })

    # ── Post — identical format to live path, ⏳ prefix to distinguish ─
    pick_card = format_pick_card(scored, 1)
    send_discord("general",   # APEX-PICKSCLEAN-2026-10-24: was #picks
        f"⏳ **BATCH RESULT** — {dates['ct_time']}\n\n{pick_card}")

    p_name    = scored.get("player", prop.get("player", "Unknown"))
    opp       = scored.get("opponent", prop.get("opponent", "opponent"))
    reasoning = scored.get("evaluation",
                            json.dumps(scored, indent=2, default=str))

    c_flags = scored.get("compliance_flags", [])
    if scored.get("compliance_penalty") and c_flags:
        comp_lines = ["\n\n" + "=" * 60,
                      "COMPLIANCE AUDIT RESULT (batch evaluation — "
                      "attached separately from the model's evaluation)",
                      "=" * 60]
        for flag in c_flags:
            comp_lines.append(f"\nRule: {flag.get('rule', 'unknown')}")
            comp_lines.append(f"Violation: {flag.get('violation', '')}")
            comp_lines.append(f"Required: {flag.get('required', '')}")
        reasoning = reasoning + "\n".join(comp_lines)

    save_evaluation_reasoning(p_name, opp, "props", reasoning, dates,
                              pick_tag="%s_%s" % (prop.get("stat_type") or "", prop.get("line") or ""))
    log("Batch",
        f"finalize_batch_evaluation complete: {p_name} {prop.get('stat_type')} "
        f"eval_id={eval_id} confidence={scored.get('confidence')}")
    return scored


# ================================================================
# COMPLIANCE AGENT
# ================================================================

COMPLIANCE_PROMPT = """Audit tennis prop evaluation for rule compliance.
Do NOT re-evaluate. Only check whether 18 rules were applied.

*** RULE CITATION FORMAT (fixed this session, confirmed live bug) ***
The list below is numbered 1-14 as CHECKLIST POSITIONS ONLY -- those
numbers are NOT rule names and must NEVER appear in your "rule" field.
Each check's description names the REAL rule it's checking (e.g. check
9 checks "Rule30 rest-day awareness" -- cite it as "Rule30", never
"Rule9"; check 3 checks "Rule19 GW math" -- cite it as "Rule19", never
"Rule3"; check 7 checks "Rule26 surface specialist" -- cite it as
"Rule26", never "Rule7"). Confirmed live: the auditor correctly found
a genuine violation on check 9 but cited it as "Rule9" instead of
"Rule30" -- correct substance, wrong label. Before writing any "rule"
field, find the actual rule number/name INSIDE that check's own
description text and use that, not the checklist position it happens
to sit at.

Return ONLY valid JSON:
{
  "status": "APPROVED or FLAGGED",
  "checks_passed": ["Rule26", "Rule18"],
  "checks_failed": ["Rule19"],
  "flags": [{"rule":"Rule19","violation":"math not shown","required":"show 6-3 6-3=12 GW","category":"B"}],
  "note": "summary"
}

*** CATEGORY CLASSIFICATION (added this session, required on every flag) ***
Every flag needs a "category" field: "A" or "B". This is a judgment call
about the VIOLATION CONTENT, not the rule number -- the same rule can be
either category depending on what actually went wrong.
Category A (real reasoning failure, affects prediction reliability):
  the evaluation ignored, contradicted, or fabricated a real fact --
  a confirmed V8 value treated as unknown, a date invented instead of
  read from the prop card, a direction that contradicts its own stated
  classification, an internal logical contradiction. These make the
  pick's actual probability estimate less trustworthy.
Category B (wording/completeness only, does NOT affect prediction
  reliability): the underlying number or conclusion was already
  correct, but a required sentence, buffer-check phrase, or template
  line wasn't explicitly written out. Example: hold% was correctly
  cited and used, but the specific "buffer check: hold% vs stage
  minimum" sentence wasn't spelled out -- the math was right, the
  citation was incomplete. A prop is not less likely to hit because
  a required phrase was missing from the writeup.
*** CATEGORY A NARROWING (added this session, approved) ***
Category A is EXACTLY two things and nothing else:
  (1) the evaluation used, ignored, contradicted, or fabricated real
      data (a confirmed V8 value treated as unknown, an invented date,
      a wrong number used in the math), or
  (2) the direction contradicts the evaluation's own stated evidence.
Everything procedural -- section placement, ordering, formatting,
template completeness, phrasing, how a correctly-computed deduction
was described -- is Category B, always. If a violation does not
clearly meet condition (1) or (2), it is B. Do not default
procedural findings to A out of caution: mispriced wording flags
have directly cost real picks real confidence.
CONFIDENCE-MATH STRUCTURE findings are Category B (confirmed live,
2026-07-09, Djokovic DF pick): a critique of HOW the confidence number
was assembled -- double-counting a constraint already embedded in the
path anchor, conflating an anchor with a ceiling, where a deduction was
placed or documented -- is NOT condition (1) or (2) when the direction
and the underlying data are correct. It is procedural, therefore B.
Note the direction of such errors: double-counting makes the score TOO
LOW -- charging a Category-A penalty on top of it compounds the exact
error being flagged.
WRONG-RULE FILING (confirmed live, 2026-07-09): if your own violation
text says or implies the violation is not actually in the rule you are
citing ("the violation is not in RuleX itself but...") then you are
filing under the wrong rule. Do NOT emit that flag under that rule
name -- either identify the rule the finding actually violates and
file it there, or if no listed rule covers it, put the observation in
"note" only, never in "flags".
*** RETRACTION RULE (added this session, confirmed live bug) ***
If, while writing a flag's violation text, you conclude the violation
is NOT actually present -- do NOT emit that flag object at all. A flag
whose own text ends in a retraction still counts as a flag and still
costs confidence: confirmed live, a self-retracted Rule30 flag
("RETRACT THIS FLAG") was charged as Category A (-3%) on a pick whose
Rule30 handling was correct. Reason FIRST, then write the flags array
containing ONLY violations you actually stand behind. Never write a
flag as a draft to argue with.
Rules: 1=Rule_DataPresence (both players real data), 2=Rule_SurfaceClass
(surface data classification),
3=Rule19 GW math visible -- ONLY APPLIES to Total Games, Games Won,
  or other games-count-based props (check the "Prop:" stat_type given
  above). Does NOT apply to Double Faults, Aces, Break Points, or any
  other non-games-count stat -- there is no meaningful "games won"
  math to show for those prop types. If stat_type is not games-related,
  this check passes automatically (not applicable, not a violation),
4=Rule32 only when lopsided confirmed (renamed from Rule9 this session
  -- Rule9 is reserved for market/seed exclusion; the underdog
  statistical ceiling rule this check verifies is now Rule32 in the
  handoff. If the evaluation text cites "Rule9" for an underdog
  ceiling claim, that is itself a Rule32 violation -- stale numbering,
  flag it),
5=Rule6 serve hold for BP, 6=Rule17 buffer vs stage minimum,
7=Rule26 surface specialist checked, 8=Rule18 last 3 checked,
9=RULE30 RETIRED 2026-08-07 (real evidence, not a guess) -- rest-day
  turnaround was tested directly against Apex's own real match history
  (n=511,679 for aces, n=1,010,473 for total games) and found NO real
  correlation with actual prop outcomes at any turnaround length
  (r=-0.0422 and r=+0.0202, both negligible). The scaled deduction
  table (24-48hr=-4%, 48hr+=0%, under-24hr=CFEF suppression) no longer
  exists and MUST NOT be applied by the evaluation. This check now
  ALWAYS PASSES AUTOMATICALLY -- do not flag "Rule30" for ANY reason,
  including an evaluation correctly showing NO rest-day deduction at
  all. An evaluation that applies no Rule30 deduction is behaving
  CORRECTLY, not incompletely. Confirmed live 2026-08-09: this check's
  stale pre-retirement wording caused a real false Category-A penalty
  (-3%, compounded with Rule30_CFEF_Stacking and Rule_ECL_False
  Certification to -9% total) on a pick whose real handling was
  correct -- exactly the kind of false-positive check 9 must never
  produce again,
10=Flip Rule — direction was assigned as MORE or LESS even when
   compression/dominance was initially ambiguous; "direction
   unclear/ambiguous" language is a violation, system must always
   commit to a direction,
11=RBMD — on LOW/MEDIUM total_games lines only, friction (resisting
   player hold rate, dominant player break conversion) was checked
   before allowing a MORE call driven by dominance alone,
12=TRUTH LOCK (added this session, real audit not just an instruction
   -- confirmed live bugs this session: match_date ignored despite being
   present, and DF per-match figures ignored despite being present, both
   costing real confidence points). Scan the evaluation text for any of:
   "unknown," "uncertain," "cannot determine," "cannot compute," or
   "UNCONFIRMED." For each instance found, check it against the
   "CONFIRMED V8 GROUND TRUTH" block given above. If a real, non-null
   value exists there for the exact thing the evaluation called unknown
   (a MISSING marker in the ground truth block does NOT count as a real
   value -- that's legitimately unconfirmed, not a violation), this is a
   TRUTH LOCK FAILURE: flag it with rule "Rule_TruthLock", violation
   describing what was called unknown, required describing the real V8
   value that was ignored. This is the single highest-value check on
   this list -- confirmed to have directly cost a real pick its GREEN
   grade tonight.
13=DIRECTION INTEGRITY (added this session): the evaluation is required
   to end with a "DIRECTION DRIVERS" section listing the top 3 evidence
   items driving the MORE/LESS call, strongest first, each tied to its
   source. Three sub-checks, with DIFFERENT categories:
   (a) If the DIRECTION DRIVERS section is missing entirely, flag rule
   "Rule_DirectionDrivers" as category "B" -- a formatting omission,
   not evidence the direction is wrong.
   (b) If the section exists but one or more listed drivers actually
   argues AGAINST the direction chosen (e.g. direction is MORE but a
   driver describes evidence for fewer games/aces/BPs), flag rule
   "Rule_DirectionIntegrity" as category "A" -- the direction came from
   framework momentum, not the stated evidence. Quote the contradicting
   driver in the violation field.
   (c) If the stated confidence is clearly built on evidence NOT among
   the three drivers (confidence reasoning cites different facts than
   the direction reasoning), flag rule "Rule_DriverConfidenceSplit" as
   category "A" -- direction from one evidence set while confidence
   comes from another is exactly the drift this check exists to catch.
   Do not flag (b) or (c) for drivers that merely add nuance or note a
   risk while still supporting the direction -- only flag genuine
   contradiction or a genuine evidence-set split.
   BINARY-MARKET RULE (added this session, from a live over-trigger):
   MORE/LESS is a binary market -- evidence that forecloses one
   direction IS affirmative support for the other. A driver phrased
   negatively ("the only path to MORE is unavailable", "the 3-set
   scenario is not supported") COUNTS as a supporting LESS driver.
   Only flag a driver under (b) if it supports the OPPOSITE direction
   from the one chosen -- never for being phrased as evidence against
   the alternative.
14=ECL CERTIFICATION (added this session, closes a real gap checks 10-13
   don't cover: a checkpoint can be scored strongly, self-acknowledged
   as not the primary driver, and simply never used -- with nothing
   CONTRADICTING anything, so check 13 never fires. Confirmed live on
   the Zverev/Fery DF 2.5 card: dominance scored 5/5 HARD, the card
   itself called it "context only," and dominance never appeared in
   DIRECTION DRIVERS -- no contradiction, so check 13(b)/(c) both pass
   clean while the actual integration failure goes uncaught.) The
   evaluation is required to end with an "ECL CERTIFICATION" block,
   five stages (Evidence Verification, Rule Status Resolution,
   Direction Drivers Integration, Confidence Traceability,
   Reconstructability), each PASSED or FAILED with real supporting
   detail, ending in RESULT: EVIDENCE CHAIN INTACT or BROKEN. Three
   sub-checks, with DIFFERENT categories -- mirrors check 13's own
   structure deliberately:
   (a) If the ECL CERTIFICATION block is missing entirely, flag rule
   "Rule_ECL_Missing" as category "B" -- a formatting omission, not by
   itself evidence the reasoning is unsound.
   (b) If the block is present and every stage reads PASSED, verify it
   yourself against the actual evaluation text above -- do not take a
   PASSED claim at face value. Specifically check "Direction Drivers
   Integration": did any checkpoint score a strong result (HARD
   dominance, 4-5/5 traits, a clearly confirmed number) that does NOT
   appear anywhere in DIRECTION DRIVERS and was NOT explicitly
   redirected as context-only with a named real driver in its place?
   If you find one, the self-certification was false -- flag rule
   "Rule_ECL_FalseCertification" as category "A". A PASSED claim that
   doesn't hold up under your own check is worse than an honest FAILED,
   because it asserts integrity that isn't there.
   (c) If the block itself reports RESULT: EVIDENCE CHAIN BROKEN, do
   not add a separate penalty for the same finding the block already
   disclosed -- an evaluation that catches and reports its own break is
   functioning correctly. Flag rule "Rule_ECL_SelfReportedBreak" as
   category "B" (the break itself should already be captured by
   whichever specific rule failed -- checks 1-13 above -- this entry
   just confirms the self-report was honest, it does not double-charge
   the underlying issue).

15=RULE 9 — MARKET/SEED EXCLUSION (added this session, closes a real
   gap: Rule 9 has existed in the master prompt this whole time with
   zero compliance verification -- confirmed by reading every one of
   the 14 checks above, none named it). Scan the evaluation text for
   odds, betting lines, market movement, "the market/books/sportsbooks
   favor X," or seeding being used AS EVIDENCE for a direction or
   dominance conclusion. Rule 9 permits mentioning these in passing
   (e.g., noting the line exists) but forbids using them to JUSTIFY a
   structural conclusion -- direction must come from on-court traits
   (hold%, break%, surface record, H2H, confirmed form), never from
   what the market's own guess implies. If odds/market movement/
   seeding is cited as a REASON a direction or dominance level was
   chosen, flag "Rule9" category "A" -- this is circular reasoning,
   exactly what Rule 9 exists to prevent. Incidental mentions with no
   evidentiary weight given to them pass this check clean.

16=GATE 14 — HOME CROWD (upgraded this session: real per-player
   home/away data now reaches this pipeline via the research block's
   "Home/away split" line -- v8_get_home_away_split() was imported but
   never called until this session's fix). If the evaluation text
   cites home-crowd/Gate14/"playing at home" at all, confirm:
   (a) it was applied as a confidence-only lean, never used to set or
       flip DIRECTION,
   (b) it was not treated as a primary driver -- a small, named,
       secondary mention, not the deciding factor,
   (c) it was not applied against a player already holding a confirmed
       HARD dominance call (Gate 14 never overrides HARD dominance,
       per its own spec),
   (d) NEW -- if the research block's home/away split for the home
       player reads CONFIRMED, the evaluation's stated deduction must
       match the PER-PLAYER CALIBRATION tiers (real gap >=15pp -> -10%,
       5-15pp -> -8%, <5pp -> -4%), not blindly the flat -8% regardless
       of what the real data shows for that specific player. If the
       split reads THIN_SAMPLE or NO DATA, the flat -8% is correct and
       this sub-check passes automatically -- there's nothing to
       calibrate against yet.
   Any violation of (a)-(d) -> flag "Gate14" category "A". Gate 14 not
   mentioned at all passes this check automatically -- citing it is
   optional context, not every match has a clear home player.

17=CFEF TRIGGERS (added this session, closes a real gap: CFEF is
   extensively specified in the master prompt with zero compliance
   verification before now). The ground truth above already gives you
   each player's this_tournament_matches / this_tournament_3set_count.
   If either player's 3set_count is 2 or more (multiple extended
   matches already this tournament -- the master prompt's own CFEF
   Trigger 6 condition) AND the evaluation's confidence math makes no
   mention of CFEF, fatigue, or extender count anywhere, flag "CFEF"
   category "A" -- real, available extender evidence was silently
   ignored, the same class of violation Rule 12 (Truth Lock) already
   catches for other fields. If the evaluation addresses CFEF at all
   -- applies the trigger, OR explicitly reasons why it doesn't apply
   this time (e.g. Week 1-2 seasonal exemption) -- this check passes
   regardless of which way it concluded; it verifies the evidence was
   CONSIDERED, not that a deduction was applied.

18=RULE30/CFEF DOUBLE-CHARGE CHECK -- RETIRED 2026-08-07 alongside
   Rule30 itself (see check 9). This check existed to catch a real
   Rule 30 deduction and a real CFEF deduction both being charged for
   the same grueling match without reconciliation. Since Rule 30
   deductions no longer exist at all, a Rule 30 deduction can never
   legitimately appear in the confidence math again -- there is
   nothing left for this check to reconcile. This check now ALWAYS
   PASSES AUTOMATICALLY. Do NOT flag "Rule30_CFEF_Stacking" for any
   reason. If a future evaluation somehow still shows a real Rule 30
   deduction in its math, that is itself a Rule30 violation (check 9)
   to flag there -- not a stacking question for this check to weigh in on."""

def compliance_agent(evaluated, attempt=1):
    rd      = evaluated.get("research_data", {})
    p_rd    = rd.get("player", {})
    o_rd    = rd.get("opponent", {})
    # Bug fix (this session, confirmed live on the Arseneault/Volynets
    # rerun): this used to read evaluated["_v8_packet_prefetched"] --
    # the RAW once-per-card prefetch, taken BEFORE Gate 11's tour
    # correction and BEFORE wrong-player/name-resolution corrections
    # engage. Whenever any of those correction paths fires, the
    # prefetch is stale relative to research_data["v8_packet"] (the
    # corrected packet the evaluation itself reasons from). Confirmed
    # effect: compliance built its "CONFIRMED V8 GROUND TRUTH" from
    # Nicolas Arseneault's data (wrong player, last match 2026-07-07)
    # while the evaluation correctly used Ariana Arseneault's
    # (2026-06-29) -- producing a FALSE Rule30 Category-A penalty on a
    # pick whose reasoning was right. The auditor and the audited must
    # see the SAME facts: source ground truth from the corrected
    # packet in research_data, same place p_rd/o_rd below already
    # come from. If no corrected packet exists (V8 injection failed),
    # this is {} and dates honestly report MISSING -- same behavior
    # the old code had in that situation.
    v8pkt   = rd.get("v8_packet", {}) or {}
    p_last_match = (v8pkt.get("player", {}).get("recent_matches") or [{}])[0].get("date")
    o_last_match = (v8pkt.get("opponent", {}).get("recent_matches") or [{}])[0].get("date")

    # Bug fix (this session): the ground-truth data needed for a real
    # Rule 12 Truth Lock check (was this field actually confirmed by
    # V8, or genuinely unavailable) was never given to the compliance
    # auditor -- it only ever saw the evaluation's own text. A "check
    # for unknown/uncertain language" instruction with nothing to
    # compare it against is not an audit, it's another unenforced rule.
    # This block gives the auditor the same confirmed facts research_
    # agent already established, so it can actually catch a real
    # contradiction instead of trusting the evaluation's self-report.
    ground_truth = f"""CONFIRMED V8 GROUND TRUTH (compare "unknown/uncertain/cannot compute" claims in the evaluation text against this -- a real value here that the text called unknown is a Rule12 violation):
match_date (confirmed play date): {evaluated.get('match_date', 'MISSING')}
Player's most recent confirmed match date: {p_last_match or 'MISSING'}
Opponent's most recent confirmed match date: {o_last_match or 'MISSING'}
Player avg_double_faults: {p_rd.get('avg_double_faults', 'MISSING')} (from {p_rd.get('df_match_count', 'MISSING')} matches)
Opponent avg_double_faults: {o_rd.get('avg_double_faults', 'MISSING')} (from {o_rd.get('df_match_count', 'MISSING')} matches)
Player this_tournament_matches / 3set_count: {p_rd.get('this_tournament_matches', 'MISSING')} / {p_rd.get('this_tournament_3set_count', 'MISSING')}
Opponent this_tournament_matches / 3set_count: {o_rd.get('this_tournament_matches', 'MISSING')} / {o_rd.get('this_tournament_3set_count', 'MISSING')}
Player serve_hold_pct: {p_rd.get('serve_hold_pct', 'MISSING')}
Opponent serve_hold_pct: {o_rd.get('serve_hold_pct', 'MISSING')}"""

    # Bug fix (this session, confirmed live on a real pick): this slice
    # was previously [:7000], which silently cut compliance off partway
    # through Checkpoint C on a normal-length evaluation (23,297 chars
    # total) -- meaning compliance NEVER saw Checkpoints D through J,
    # Gate 4's GREEN justification, Rule 30's rest-day math, RBMD, score
    # paths, the direction decision, or the confidence math for that
    # pick, or very likely any pick following the mandated A-J checkpoint
    # format. It then correctly reported the (artificially truncated)
    # text it received as incomplete, producing a fabricated
    # Rule_TruthLock Category-A penalty on a pick that was actually
    # complete and legitimate. evaluation_agent's own max_tokens=16000
    # output cap puts a hard ceiling on how long evaluation_output can
    # ever be -- 50000 sits above that ceiling with buffer, so this can
    # no longer clip a legitimately-completed evaluation.
    content = f"""Prop: {evaluated.get('player')} {evaluated.get('stat_type')} {evaluated.get('line')}
Direction: {evaluated.get('direction')} | Match: {evaluated.get('match_type')}
Stage: {rd.get('match_context',{}).get('stage',1)}
Is favorite: {evaluated.get('is_favorite')}
{ground_truth}
Evaluation output:
{evaluated.get('evaluation_output','')[:50000]}
Audit all 18 checks. JSON only."""

    # Bug fix (this session, evidence from real (older-session) log
    # entries): three real PARSE FAILUREs all broke at char 1413, 1435,
    # and 1500 respectively -- clustered right around the old
    # max_tokens=1400 output ceiling (roughly 350-375 tokens at typical
    # density), and all three raw outputs were still actively LISTING
    # MORE FLAGS when they cut off (checks_failed showed 6-7 items with
    # only some of the corresponding flags array entries written before
    # truncation). This is the same class of bug as the evaluation-side
    # truncation, just on compliance's OUTPUT instead of its input: a
    # heavily-flagged evaluation can legitimately need more than 1400
    # tokens to write out every flag's rule/violation/required fields as
    # valid JSON, and a truncated JSON response fails to parse entirely
    # -- producing FLAGGED_FOR_MANUAL_REVIEW (or, before this session's
    # earlier fix, a silent flat cap) instead of the real, itemized
    # Category A/B verdict that should have been possible. 3000 is a
    # reasoned buffer (roughly 2x the ceiling that was confirmed
    # insufficient), not a hard mathematical bound the way 50000 was for
    # the input slice above -- PROVISIONAL, watch for further parse
    # failures and raise again if they recur at the new ceiling.
    _compliance_trace_id_box = []
    result = call_model(MODEL_HAIKU, COMPLIANCE_PROMPT, content,
                        temperature=0.1, max_tokens=3000,
                        eval_id=evaluated.get("eval_id"), step_type="compliance",
                        trace_id_out=_compliance_trace_id_box)
    if not result:
        log("Compliance", "⚠️ CALL FAILURE — call_model returned no result at all "
            "(API call itself failed, not a parse issue)")
    if result:
        try:
            clean = re.sub(r"```json|```", "", result).strip()
            audit = json.loads(clean)
            flags = audit.get("flags", [])
            status = audit.get("status", "APPROVED")

            if _ATE_AVAILABLE:
                try:
                    _compliance_trace_id = _compliance_trace_id_box[0] if _compliance_trace_id_box else None
                    ate.capture_compliance_flag_activations(
                        eval_id=evaluated.get("eval_id"), trace_id=_compliance_trace_id, flags=flags)
                except Exception:
                    pass  # capture_compliance_flag_activations already
                          # fail-opens internally; belt-and-suspenders only

            # Retraction safety net (this session, approved; confirmed
            # live on the Maria/Sebov pick): the auditor can reason
            # itself out of a violation INSIDE the violation text and
            # still emit the flag object -- and a flag in the array is
            # a flag, whatever its text says. That self-retracted
            # Rule30 flag was charged as Category A (-3%) on a pick
            # whose Rule30 handling was correct. The prompt now forbids
            # emitting retracted flags; this net catches any that slip
            # through anyway. Conservative explicit markers only -- a
            # flag that merely hedges is kept, a flag that states its
            # own violation isn't present is dropped, loudly.
            _RETRACTION_MARKERS = ("retract this flag", "retract the flag",
                                   "flag is retracted",
                                   "violation is not present",
                                   "no violation is present",
                                   "violation was not present")
            _kept_flags, _retracted_flags = [], []
            for _f in flags:
                _vtext = str(_f.get("violation", "")).lower()
                if any(_m in _vtext for _m in _RETRACTION_MARKERS):
                    _retracted_flags.append(_f)
                else:
                    _kept_flags.append(_f)
            if _retracted_flags:
                _r_rules = ", ".join(str(_f.get("rule", "?")) for _f in _retracted_flags)
                log("Compliance", f"RETRACTION NET — dropped {len(_retracted_flags)} "
                    f"self-retracted flag(s) [{_r_rules}] the auditor emitted "
                    f"despite concluding no violation was present")
                send_discord("alerts",
                    f"⚠️ **AUDITOR RETRACTION CAUGHT** — "
                    f"{evaluated.get('player')} {evaluated.get('line')} "
                    f"{evaluated.get('stat_type')}\n"
                    f"The auditor wrote {len(_retracted_flags)} flag(s) "
                    f"[{_r_rules}] whose own text retracts the violation. "
                    f"Dropped before penalty math — no confidence was charged "
                    f"for them."
                )
                flags = _kept_flags
                if not flags and status == "FLAGGED":
                    status = "APPROVED"
                    log("Compliance", "All flags were self-retractions — "
                        "status corrected to APPROVED")

            # Category-by-definition normalization (this session,
            # root-caused live: Zverev DF 2.5, 2026-07-09 -- the auditor
            # emitted rule "Rule_DirectionDrivers" without a valid
            # category field and the scorer's default-to-A charged -3%
            # for what check 13(a) itself defines as Category B, ALWAYS:
            # a missing/incomplete DIRECTION DRIVERS section is a
            # formatting omission, never evidence the direction is
            # wrong. A rule whose category is fixed by its own
            # definition must not depend on the auditor remembering to
            # write the field. Genuine driver contradictions carry
            # DIFFERENT rule names (Rule_DirectionIntegrity,
            # Rule_DriverConfidenceSplit) and keep the default-to-A.
            # Applies to BOTH pipelines: props (scoring_agent) and
            # moneyline both consume the flags returned here.
            _B_BY_DEFINITION = ("rule_directiondrivers",)
            for _f in flags:
                _rname = str(_f.get("rule", "")).strip().lower()
                if _rname in _B_BY_DEFINITION and _f.get("category") != "B":
                    log("Compliance", f"CATEGORY NORMALIZED — {_f.get('rule')} "
                        f"is Category B by definition (check 13a); auditor "
                        f"wrote {_f.get('category', 'no category')!r} — forced "
                        f"to B, zero confidence cost")
                    _f["category"] = "B"

            if status == "APPROVED":
                return {"status": "APPROVED", "flags": []}

            if attempt >= 2:
                return {"status": "ACCEPTED_WITH_PENALTY",
                        "flags": flags, "note": "Flagged 2x — accepted with penalty"}

            return {"status": "FLAGGED", "flags": flags}
        except Exception as _parse_err:
            # v7.9.7 fix (this session): this was previously a bare
            # `except: pass` — meaning every compliance parse failure
            # was completely invisible, with zero record of WHY it
            # failed (malformed JSON from the model, an unexpected
            # response shape, etc.). This was confirmed as the actual
            # reason MANUAL REVIEW caps were firing on every single
            # prop tonight with no way to diagnose the real cause —
            # the downstream "compliance audit failed to run" message
            # was the only visible trace, with nothing showing what
            # actually broke. Logging the real exception AND the raw
            # model output (trimmed) that failed to parse, so the next
            # failure is actually diagnosable instead of silent.
            log("Compliance", f"⚠️ PARSE FAILURE — {type(_parse_err).__name__}: {_parse_err}")
            log("Compliance", f"Raw model output that failed to parse (first 500 chars): {result[:500]!r}")
            pass

    # v7.9.7 fix (item 2): previously returned "APPROVED" here on any
    # parse/call failure — a crashed or malformed compliance audit gave
    # a free pass instead of being caught. Now flagged for manual review
    # instead, with a dedicated confidence cap (62%) applied downstream
    # in scoring_agent — stricter than the generic compliance-penalty
    # cap (66%), since this path means compliance never actually ran.
    return {"status": "FLAGGED_FOR_MANUAL_REVIEW", "flags": [],
            "note": "Parse error — flagged for manual review, not auto-approved"}

# ================================================================
# MONEYLINE DOMINANCE SCORE (v7.9.6, Step 8)
# ================================================================
# Feeds Phase 1's "who wins in most realistic outcomes" step with a
# real, numeric, weighted differential instead of a purely verbal
# judgment call — using the same confirmed structural fields (hold%,
# break%, BP save%, current-season surface win%) already flowing
# through the V8 packet for props. Weights are tunable via weights.json
# and calibrate through the same 5-in-14-days learning loop as every
# other signal in this system — the starting values are a reasonable
# default, not a locked, backtested truth.

DATA_TIER_CONFIDENCE_CAP = {
    "Tier1":   88,
    "Tier2":   80,
    "Tier3":   72,
    "MISSING": 65,
}

def confidence_to_american_odds(confidence_pct):
    """
    Converts a 0-100 confidence percentage into its equivalent FAIR
    American odds price (no vig/juice) -- this is Apex's own implied
    price derived from its own confidence, NOT a real market price,
    and must always be labeled and shown separately from real captured
    sportsbook odds (Rule 9: market/odds data never enters reasoning,
    comparison-only, post-prediction only).

    Standard conversion, round-trip verified:
      p=0.50 -> -100  (even money)
      p=0.63 -> -170
      p=0.78 -> -355
      p=0.90 -> -900

    Returns None for anything not a real 0-100 number (0 and 100
    themselves are excluded too -- a 0% or 100% "price" is undefined/
    infinite, not a real number to display).
    """
    try:
        p = float(confidence_pct) / 100
    except (TypeError, ValueError):
        return None
    if p <= 0 or p >= 1:
        return None
    if p >= 0.5:
        odds = -100 * (p / (1 - p))
    else:
        odds = 100 * ((1 - p) / p)
    return round(odds)

def format_american_odds(odds):
    """+150 style for positive, -355 style for negative/zero. None -> '--'."""
    if odds is None:
        return "--"
    return f"+{odds}" if odds > 0 else f"{odds}"

def compute_ml_dominance_score(player_stats: dict, opponent_stats: dict, weights: dict) -> dict:
    """
    Computes a weighted structural dominance differential between two
    players for a moneyline pick, using only confirmed V8 packet
    fields: hold_pct, break_pct, bp_save_pct, win_pct (current-season
    surface win rate).

    Returns a dict with the predicted side ("player" or "opponent"),
    the raw dominance score, a confidence ceiling derived from the
    score's magnitude (capped further by data tier), and the full
    per-signal breakdown for "show your math" transparency — never a
    single opaque number with no way to audit how it was reached.

    If either player is missing any of the four required fields, the
    computation is honestly marked INCOMPLETE_DATA rather than
    silently computed from a partial set — consistent with the
    unconfirmed-is-NO discipline used everywhere else in this system.
    """
    required = ["hold_pct", "break_pct", "bp_save_pct", "win_pct"]
    missing = [f for f in required
               if player_stats.get(f) is None or opponent_stats.get(f) is None]
    if missing:
        return {
            "status": "INCOMPLETE_DATA",
            "missing_fields": missing,
            "predicted_side": None,
            "dominance_score": None,
            "confidence_ceiling": None,
            "breakdown": {},
        }

    serve_edge  = player_stats["hold_pct"]    - opponent_stats["hold_pct"]
    return_edge = player_stats["break_pct"]   - opponent_stats["break_pct"]
    clutch_edge = player_stats["bp_save_pct"] - opponent_stats["bp_save_pct"]
    form_edge   = player_stats["win_pct"]     - opponent_stats["win_pct"]

    dominance_score = (
        weights.get("ml_serve_weight",  0.35) * serve_edge +
        weights.get("ml_return_weight", 0.25) * return_edge +
        weights.get("ml_clutch_weight", 0.15) * clutch_edge +
        weights.get("ml_form_weight",   0.25) * form_edge
    )

    magnitude = abs(dominance_score)
    if magnitude >= 15:
        confidence_ceiling = 78
    elif magnitude >= 8:
        confidence_ceiling = 68
    else:
        confidence_ceiling = None  # too close — Phase 1 alone can't call it

    # Data-tier honesty: cap by whichever player has the thinner data,
    # same principle as everywhere else — missing/thin data never lets
    # a confident number through unchecked.
    p_tier = player_stats.get("data_tier", "MISSING")
    o_tier = opponent_stats.get("data_tier", "MISSING")
    tier_cap = min(
        DATA_TIER_CONFIDENCE_CAP.get(p_tier, 65),
        DATA_TIER_CONFIDENCE_CAP.get(o_tier, 65),
    )
    if confidence_ceiling is not None:
        confidence_ceiling = min(confidence_ceiling, tier_cap)

    predicted_side = "player" if dominance_score > 0 else ("opponent" if dominance_score < 0 else None)

    return {
        "status": "COMPUTED",
        "predicted_side": predicted_side,
        "dominance_score": round(dominance_score, 2),
        "confidence_ceiling": confidence_ceiling,
        "data_tier_cap_applied": tier_cap,
        "breakdown": {
            "serve_edge":  round(serve_edge, 2),
            "return_edge": round(return_edge, 2),
            "clutch_edge": round(clutch_edge, 2),
            "form_edge":   round(form_edge, 2),
        },
    }

# ================================================================
# MONEYLINE PHASE 2 — FIVE-SIGNAL CONFIRMATION (v7.9.6, Step 8)
# ================================================================
# Each function returns a graduated None/-2/-1/0/+1/+2 value (never
# the legacy "player"/"opponent" strings). S3 (H2H) wired in below —
# openclaw_v8_api.get_h2h()'s real endpoint fix confirmed live-tested
# working (Djokovic vs Alcaraz -> CONFIRMED, 5-5); the old "pending
# vendor fix" note here was stale and has been removed.

def favorite_underdog_line(player: str, player_odds: str, opponent: str, opponent_odds: str) -> str:
    """
    2026-08-04: a real, visible favorite/underdog tag using odds that
    are ALREADY being captured automatically by the screenshot vision
    parse (SCREENSHOT_PARSE_PROMPT already extracts player_odds/
    opponent_odds from a real sportsbook screenshot -- no manual entry
    involved). This is display-only, computed strictly AFTER the real
    prediction exists -- never touches evaluation reasoning (Rule 9).

    Standard American odds convention: negative = favorite, positive =
    underdog. Deliberately simple and literal -- no interpretation
    beyond the sign, so there's nothing here that could second-guess a
    real prediction.

    Returns an empty string if either odds value is missing or doesn't
    parse as a real number, rather than guessing.
    """
    def _to_int(o):
        try:
            return int(str(o).strip().replace("+", ""))
        except (TypeError, ValueError):
            return None

    p_val = _to_int(player_odds)
    o_val = _to_int(opponent_odds)
    if p_val is None or o_val is None:
        return ""

    if p_val < o_val:
        return f"Favorite: {player} ({player_odds}) | Underdog: {opponent} ({opponent_odds})"
    elif o_val < p_val:
        return f"Favorite: {opponent} ({opponent_odds}) | Underdog: {player} ({player_odds})"
    else:
        return f"Even money: {player} ({player_odds}) / {opponent} ({opponent_odds})"


def moneyline_context_lines(player: str, opponent: str, research_data: dict) -> str:
    """
    Real ranking/surface-record/recent-form context for a moneyline
    Discord post, matching what props' pick card already shows. Props
    gets this from the V8 packet plus a mechanical override at research
    time (recent_form_last3 computed from real match results, not web
    search/LLM guessing -- see the v7.9.6 fix comment near where props
    injects it). Moneyline never had any of this in its posted message
    at all.

    ranking/surface_record come from research_data (already fetched by
    build_moneyline_evaluation_content(), no new call needed).
    recent_form is computed fresh here the same real way props does --
    W/L from v8_get_recent_matches()'s real "winner" field, not a guess.

    Fails open: any missing data or API issue just omits that piece,
    never blocks or breaks the post itself.
    """
    if not _V8_AVAILABLE:
        return ""
    p = research_data.get("player", {}) or {}
    o = research_data.get("opponent", {}) or {}

    def _form(name):
        try:
            canonical = v8_resolve_player_name(name, "atp") or v8_resolve_player_name(name, "wta")
            if not canonical:
                return "?"
            recent = v8_get_recent_matches(canonical, limit=3)
            form = ["W" if m.get("winner") else "L" for m in recent[:3]]
            return "".join(form) if form else "?"
        except Exception:
            return "?"

    p_line = f"#{p.get('ranking') or '?'} | {p.get('surface_record') or '?'} | Last 3: {_form(player)}"
    o_line = f"#{o.get('ranking') or '?'} | {o.get('surface_record') or '?'} | Last 3: {_form(opponent)}"
    return f"{player}: {p_line}\n{opponent}: {o_line}"


def signal_s1_surface_form(player_name: str, opponent_name: str, surface: str):
    """
    Moneyline v1.4 (locked spec, this session): current-season surface
    win rate comparison, graduated on a None/-2/-1/0/+1/+2 scale based
    on the SIZE of the gap between players, not just its direction.

    Return type is intentionally restricted to exactly these six
    values — None, -2, -1, 0, 1, 2 — and NEVER the legacy "player"/
    "opponent" strings. Mixing the old binary vocabulary back in here
    would silently recreate the exact information-loss problem this
    redesign exists to fix (see Osaka/Muchova review: real edges like
    a 15pp surface gap were being treated identically to a 1pp gap).

    None = genuinely unconfirmed (missing win% for either player) —
           NOT the same as 0, which means confirmed real data showing
           no meaningful edge either way.
    0    = confirmed data, gap below ML_S1_SURFACE_MODERATE_GAP_PP —
           real information, just not a meaningful edge.
    +/-1 = moderate gap (ML_S1_SURFACE_MODERATE_GAP_PP to just under
           ML_S1_SURFACE_STRONG_GAP_PP).
    +/-2 = strong gap (>= ML_S1_SURFACE_STRONG_GAP_PP).
    Positive favors player, negative favors opponent.

    Band cutoffs (ML_S1_SURFACE_*_GAP_PP) are PROVISIONAL — see the
    config block where they're defined for the full caveat.
    """
    if not _V8_AVAILABLE:
        return None
    p_rate = v8_current_season_winpct(player_name, surface)
    o_rate = v8_current_season_winpct(opponent_name, surface)
    if p_rate is None or o_rate is None:
        return None

    gap = p_rate - o_rate
    if abs(gap) < ML_S1_SURFACE_MODERATE_GAP_PP:
        return 0
    magnitude = 2 if abs(gap) >= ML_S1_SURFACE_STRONG_GAP_PP else 1
    return magnitude if gap > 0 else -magnitude

def signal_s2_tournament_form(player_name: str, opponent_name: str, tournament_id):
    """
    RETIRED 2026-08-07 -- no longer called from evaluate_moneyline().
    Real walk-forward backtest against Dataset B's full history
    (n=257,068 real matches) found AUC 0.486 (worse than a coin flip)
    for the original form below. Three research-motivated refinements
    (closely-matched-pairs-only, at 100/150/200 ELO-point thresholds)
    were also tested and all landed at ~0.50 (n=117K-190K each) --
    real "hot hand"/momentum literature broadly does not support
    cross-match momentum either, consistent with this result. Left
    intact for reference / possible future reuse under a genuinely
    different framing, not because it's still trusted as-is.

    Moneyline v1.4 (locked spec, this session): tournament form
    (Rule F8 — supporting only, never primary), graduated on the SIZE
    of the round-count gap between players in this event, not just
    who's advanced further.

    Return type restricted to None/-2/-1/0/1/2 only — never legacy
    "player"/"opponent" strings, same principle as S1.

    Important correction from v1.3: a TIED round count (e.g. both
    reached the semifinal) is now 0 — confirmed real data showing no
    gap — not None. v1.3 conflated "tied" with "neither has played
    yet," which is exactly the None-vs-0 collapse this whole redesign
    exists to fix. Only "neither player has any confirmed rounds in
    this event yet" remains None (genuinely nothing to measure).

    None = neither player has confirmed rounds in this event yet, OR
           tournament_id/V8 unavailable — nothing to measure at all.
    0    = confirmed rounds for at least one side, but the gap is
           below ML_S2_TOURNAMENT_MODERATE_ROUND_GAP (e.g. tied, or a
           1-round difference below the threshold).
    +/-1 = moderate round gap.
    +/-2 = strong round gap (>= ML_S2_TOURNAMENT_STRONG_ROUND_GAP).
    Positive favors player, negative favors opponent.

    Band cutoffs (ML_S2_TOURNAMENT_*_ROUND_GAP) are PROVISIONAL — see
    the config block where they're defined for the full caveat.
    """
    if not _V8_AVAILABLE or not tournament_id:
        return None
    p_matches = v8_get_recent_matches(player_name, limit=20)
    o_matches = v8_get_recent_matches(opponent_name, limit=20)
    p_rounds = sum(1 for m in p_matches
                   if m.get("tournament_id") == tournament_id and m.get("won") is True)
    o_rounds = sum(1 for m in o_matches
                   if m.get("tournament_id") == tournament_id and m.get("won") is True)
    if p_rounds == 0 and o_rounds == 0:
        return None  # neither has confirmed rounds in this event yet — genuinely nothing to measure

    gap = p_rounds - o_rounds
    if abs(gap) < ML_S2_TOURNAMENT_MODERATE_ROUND_GAP:
        return 0  # confirmed data (tied or near-tied) — real info, no meaningful edge
    magnitude = 2 if abs(gap) >= ML_S2_TOURNAMENT_STRONG_ROUND_GAP else 1
    return magnitude if gap > 0 else -magnitude

def signal_s3_h2h(player_name: str, opponent_name: str, tour: str = "atp"):
    """
    Moneyline v1.4 — H2H signal, graduated None/-2/-1/0/+1/+2, same
    pattern as S1/S2/S4/S5. Gap is the head-to-head win-rate
    differential between the two players, in percentage points:
        gap = (p1_wins - p2_wins) / total * 100

    Deliberately NOT surface-filtered — the real 26-match dataset the
    thresholds were derived from used overall H2H, not surface-split,
    since splitting further would push most pairs below
    ML_S3_H2H_MIN_SAMPLE. A surface-aware H2H signal would need its
    own separate derivation, not a change to this one.

    None = get_h2h() returned UNAVAILABLE, OR total real meetings is
           below ML_S3_H2H_MIN_SAMPLE — real data, just not enough of
           it to trust (a 1-0 head-to-head is not a real 100pp gap).
    0    = confirmed sample, gap below ML_S3_H2H_MODERATE_GAP_PP —
           real information, no meaningful edge.
    +/-1 = moderate gap (ML_S3_H2H_MODERATE_GAP_PP to just under
           ML_S3_H2H_STRONG_GAP_PP).
    +/-2 = strong gap (>= ML_S3_H2H_STRONG_GAP_PP).
    Positive favors player, negative favors opponent.

    Band cutoffs (ML_S3_H2H_*_GAP_PP) are PROVISIONAL — see the
    config block where they're defined for the full caveat.
    """
    if not _V8_AVAILABLE:
        return None

    h2h = v8_get_h2h(player_name, opponent_name, tour=tour)
    if h2h.get("status") != "CONFIRMED":
        return None

    total = h2h.get("total")
    if not total or total < ML_S3_H2H_MIN_SAMPLE:
        return None

    p1_wins = h2h.get("p1_wins", 0) or 0
    p2_wins = h2h.get("p2_wins", 0) or 0
    gap = (p1_wins - p2_wins) / total * 100

    if abs(gap) < ML_S3_H2H_MODERATE_GAP_PP:
        return 0
    magnitude = 2 if abs(gap) >= ML_S3_H2H_STRONG_GAP_PP else 1
    return magnitude if gap > 0 else -magnitude

def signal_s4_physical_state(player_name: str, opponent_name: str):
    """
    RETIRED 2026-08-07 -- no longer called from evaluate_moneyline().
    Real walk-forward backtest against Dataset B's full history
    (n=500,072 real matches) found AUC 0.511 (essentially a coin flip)
    for the original 2-factor form below. Three research-motivated
    refinements (continuous turnaround gap; cumulative match load over
    7-day and 14-day windows) were also tested and all landed AT OR
    BELOW a coin flip (0.451/0.447/0.412) -- worse than the original,
    not better. Independent tennis-fatigue research corroborates this:
    outcomes are "likely more influenced by the skill level of the
    opponent" than recent match length. Left intact for reference /
    possible future reuse under a genuinely different framing, not
    because it's still trusted as-is.

    Moneyline v1.4 (locked spec, this session): physical state, tied
    to Rule F9 fatigue logic, graduated on the NUMBER of fatigue
    factors present for each player, not a single binary flag.

    Return type restricted to None/-2/-1/0/1/2 only — never legacy
    "player"/"opponent" strings, same principle as S1/S2.

    Two independent fatigue factors are checked per player:
      1. Most recent match went the distance (best_of == set_count)
      2. Turnaround since that match is under 48 hours
    A player's fatigue_count is 0, 1, or 2 depending on how many of
    these are confirmed true. The SIGNAL is the gap between the two
    players' fatigue_counts — "went the distance AND short turnaround"
    is a stronger fatigue read than just one factor alone, per the
    explicit guidance: "back-to-back 3-setters + short turnaround
    = stronger fatigue" should outweigh "any fatigue = -1".

    Important correction from v1.3: "both fatigued the same amount"
    (including "neither fatigued at all," fatigue_count 0-0) is now 0
    — confirmed real data showing no fatigue-based edge — not None.
    v1.3's "both or neither fatigued -> None" conflated a real,
    confirmed tie with a genuine data gap. Only an actual failure to
    confirm turnaround/match data for either player remains None.

    None = could not confirm match/turnaround data for either player.
    0    = confirmed data, fatigue_count gap is 0 (equally fatigued,
           possibly both fully rested).
    +/-1 = one-factor gap (ML_S4_PHYSICAL_MODERATE_FACTORS).
    +/-2 = two-factor gap (ML_S4_PHYSICAL_STRONG_FACTORS) — the more
           fatigued player has BOTH factors against them while the
           other has none.
    Positive favors player (opponent more fatigued), negative favors
    opponent (player more fatigued).

    Band cutoffs (ML_S4_PHYSICAL_*_FACTORS) are PROVISIONAL — see the
    config block where they're defined for the full caveat.
    """
    if not _V8_AVAILABLE:
        return None

    def _fatigue_count(name):
        """Returns 0, 1, or 2 confirmed fatigue factors, or None if
        match/turnaround data couldn't be confirmed at all."""
        matches = v8_get_recent_matches(name, limit=1)
        if not matches:
            return None
        last = matches[0]
        best_of = last.get("best_of")
        set_count = last.get("set_count")
        went_the_distance = bool(best_of and set_count and set_count >= best_of)
        try:
            last_date = datetime.strptime(last["date"], "%Y-%m-%d")
            turnaround_hours = (datetime.now(CT).replace(tzinfo=None) - last_date).total_seconds() / 3600
            short_turnaround = turnaround_hours < 48
        except Exception:
            # Can't confirm turnaround at all. If we ALSO can't confirm
            # went_the_distance, this player's fatigue state is fully
            # unconfirmable -- None. If went_the_distance IS confirmed,
            # we at least have partial information (1 factor known).
            if not (best_of and set_count):
                return None
            short_turnaround = False  # unconfirmed turnaround treated as not-short, not assumed fatigued

        count = int(went_the_distance) + int(short_turnaround)
        return count

    p_count = _fatigue_count(player_name)
    o_count = _fatigue_count(opponent_name)
    if p_count is None or o_count is None:
        return None

    gap = o_count - p_count  # positive gap = opponent MORE fatigued = favors player
    if gap == 0:
        return 0  # confirmed data, equally fatigued (possibly both fine) — no edge
    magnitude = ML_S4_PHYSICAL_STRONG_FACTORS if abs(gap) >= ML_S4_PHYSICAL_STRONG_FACTORS else ML_S4_PHYSICAL_MODERATE_FACTORS
    return magnitude if gap > 0 else -magnitude

def signal_s5_classification_confirmation(dominance_result: dict, classification: str):
    """
    Moneyline v1.4 (locked spec, this session): does the underlying
    structural data (hold%/break% gap, via the same dominance_result
    Step 8 already computed) show a real, measurable edge — graded on
    the SIZE of dominance_score's magnitude directly, not gated by the
    LOPSIDED/COMPETITIVE classification label.

    Return type restricted to None/-2/-1/0/1/2 only — never legacy
    "player"/"opponent" strings, same principle as S1/S2/S4.

    Important correction from v1.3: the old version gated entirely on
    `classification == "LOPSIDED"` before even looking at magnitude —
    meaning a COMPETITIVE match with a real, measurable dominance gap
    got silently converted to None regardless of its actual size. This
    is exactly the "LOPSIDED=+2, COMPETITIVE=0" collapse explicitly
    flagged as wrong: it recreates magnitude-loss by hiding it behind
    a label instead of a raw score. classification is a separate
    mechanical proxy computed elsewhere (rank-gap based) — S5's job is
    to measure the REAL structural gap directly, independent of that
    label, still explicitly rejecting ranking/seeding-only
    justification (the whole original point of S5) since
    dominance_score itself is never ranking-derived.

    None = dominance_result wasn't successfully computed at all
           (status != "COMPUTED") — nothing to measure.
    0    = confirmed dominance_score, magnitude below
           ML_S5_CLASSIFICATION_MODERATE_MAGNITUDE — real computed
           data, no meaningful structural edge.
    +/-1 = moderate magnitude (ML_S5_CLASSIFICATION_MODERATE_MAGNITUDE
           to just under ML_S5_CLASSIFICATION_STRONG_MAGNITUDE).
    +/-2 = strong magnitude (>= ML_S5_CLASSIFICATION_STRONG_MAGNITUDE).
    Positive favors player, negative favors opponent.

    Band cutoffs (ML_S5_CLASSIFICATION_*_MAGNITUDE) are PROVISIONAL —
    see the config block where they're defined for the full caveat.
    """
    if dominance_result.get("status") != "COMPUTED":
        return None

    score = dominance_result.get("dominance_score")
    if score is None:
        return None

    magnitude = abs(score)
    if magnitude < ML_S5_CLASSIFICATION_MODERATE_MAGNITUDE:
        return 0  # confirmed data, real gap measured, just not a meaningful edge
    band = 2 if magnitude >= ML_S5_CLASSIFICATION_STRONG_MAGNITUDE else 1
    return band if score > 0 else -band

def signal_s6_elo(player_name: str, opponent_name: str):
    """
    Moneyline v1.4 (added 2026-08-07 after a real walk-forward
    backtest, not a guess): overall ELO rating gap, graduated
    None/-2/-1/0/+1/+2, same shape as every other Phase 2 signal.

    Real result that earned this signal a place here: elo_backtest.py
    ran walk-forward (no lookahead) against Dataset B's full real
    history (519,864 matches, post the player_a/player_b labeling-bug
    fix) and found overall ELO gap at AUC 0.7223 — Apex's strongest
    tested signal, beating H2H's 0.584 and S1's 0.589. Surface-specific
    ELO tested weaker at real scale (0.7093) — the OPPOSITE of what a
    much smaller early sample suggested — so this deliberately reads
    OVERALL elo_gap, not surface, per that real finding.

    None = elo_ops.get_elo_gap() returned None (one or both players
           have zero real matches on file — nothing to measure), OR
           either player's real match count is below
           ML_S6_ELO_MIN_MATCHES — a rating built on a handful of
           matches hasn't converged enough to trust, same
           unconfirmed-is-NO discipline as every other signal here.
    0    = confirmed ratings for both players, gap below
           ML_S6_ELO_MODERATE_GAP — real information, no meaningful edge.
    +/-1 = moderate gap (ML_S6_ELO_MODERATE_GAP to just under
           ML_S6_ELO_STRONG_GAP).
    +/-2 = strong gap (>= ML_S6_ELO_STRONG_GAP).
    Positive favors player, negative favors opponent.

    Band cutoffs (ML_S6_ELO_*_GAP) are PROVISIONAL — see the config
    block where they're defined for the full caveat.
    """
    gap_info = elo_ops.get_elo_gap(player_name, opponent_name)
    if gap_info is None:
        return None

    p_matches, o_matches = gap_info["overall_matches"]
    if p_matches < ML_S6_ELO_MIN_MATCHES or o_matches < ML_S6_ELO_MIN_MATCHES:
        return None

    gap = gap_info["overall_gap"]
    if abs(gap) < ML_S6_ELO_MODERATE_GAP:
        return 0
    magnitude = 2 if abs(gap) >= ML_S6_ELO_STRONG_GAP else 1
    return magnitude if gap > 0 else -magnitude


def _clean_player_name(name):
    """2026-08-14 fix (#3, name truncation): Discord cards truncate long
    names with a literal '...' or '\u2026' ('Masamichi Ima...'), which then
    fails V8 resolution and ELO lookups on the raw string. Strip the
    truncation markers here at the entry point; deeper resolution
    (unambiguous prefix matching against player_elo) lives in
    elo_ops._resolve_elo_name()."""
    if not name:
        return name
    cleaned = name.strip()
    for marker in ("...", "\u2026"):
        if cleaned.endswith(marker):
            cleaned = cleaned[: -len(marker)].strip()
    return cleaned


# ── REAL calibration table loading (2026-08-12) ─────────────────────
# real_calibration_tables.json is built by build_real_calibration_tables.py
# against the real 519,864-match Dataset B history -- see that script
# for how ELO/S5/S1/H2H's real (raw_value, outcome) pairs were bucketed
# into real historical win rates. Loaded once, cached at module level
# (matches restart cleanly picks up a rebuilt file; no live reload).
REAL_CALIBRATION_TABLES_FILE = Path(__file__).resolve().parent / "real_calibration_tables.json"
_REAL_CALIBRATION_TABLES = None


def _load_real_calibration_tables():
    global _REAL_CALIBRATION_TABLES
    if _REAL_CALIBRATION_TABLES is not None:
        return _REAL_CALIBRATION_TABLES
    if not REAL_CALIBRATION_TABLES_FILE.exists():
        # Real, honest failure mode: no fabricated tables. Every S5/S1/H2H
        # lookup will come back None (excluded, not guessed) until the
        # real file is present -- ELO alone can still produce a real
        # blended result on its own real formula.
        _REAL_CALIBRATION_TABLES = {"s5": [], "s1": [], "h2h": []}
        return _REAL_CALIBRATION_TABLES
    with open(REAL_CALIBRATION_TABLES_FILE) as f:
        raw = json.load(f)
    _REAL_CALIBRATION_TABLES = {
        name: [CalibrationBucket(low=b["low"], high=b["high"], n=b["n"], win_rate=b["win_rate"])
               for b in buckets]
        for name, buckets in raw.items()
    }
    return _REAL_CALIBRATION_TABLES


def _raw_s1_gap(player_name: str, opponent_name: str, surface: str):
    """Real raw surface-form gap in percentage points -- same real
    source as signal_s1_surface_form(), WITHOUT banding into -2..2.
    The new engine needs the raw continuous number, not a graduated one."""
    if not _V8_AVAILABLE:
        return None
    p_rate = v8_current_season_winpct(player_name, surface)
    o_rate = v8_current_season_winpct(opponent_name, surface)
    if p_rate is None or o_rate is None:
        return None
    return p_rate - o_rate


def _raw_h2h_gap(player_name: str, opponent_name: str, tour: str = "atp"):
    """Real raw H2H gap, RESCALED to -1..1 to match the real
    calibration table's scale (build_real_calibration_tables.py's
    derived-H2H convention: 2*(wins/total)-1) -- NOT the same -100..100pp
    scale signal_s3_h2h() bands into -2..2.

    Min sample = 3 prior meetings (2026-08-12 fix, caught on
    post-deploy verification): the real calibration table AND the
    380K-match backtest were both built on a 3-meeting floor -- using
    the old signal's ML_S3_H2H_MIN_SAMPLE (10) here would silently
    exclude H2H from most matches the tested numbers included it on,
    shifting live blends away from tested conditions."""
    H2H_MIN_SAMPLE_CALIBRATED = 3  # matches build_real_calibration_tables.py exactly
    if not _V8_AVAILABLE:
        return None
    h2h = v8_get_h2h(player_name, opponent_name, tour=tour)
    if h2h.get("status") != "CONFIRMED":
        return None
    total = h2h.get("total")
    if not total or total < H2H_MIN_SAMPLE_CALIBRATED:
        return None
    p1_wins = h2h.get("p1_wins", 0) or 0
    p2_wins = h2h.get("p2_wins", 0) or 0
    return (p1_wins - p2_wins) / total  # -1..1 scale, matches the real calibration table


def _raw_elo_gap(player_name: str, opponent_name: str):
    """Real raw ELO gap from elo_ops.get_elo_gap() -- same real source
    signal_s6_elo() already uses, returned raw instead of banded into
    -2..2.

    Min matches = ANY rated match (2026-08-12 fix, caught on
    post-deploy verification): the real 380K-match backtest included
    every player with overall_matches > 0 -- get_elo_gap() itself
    already returns None for zero-match players, so no extra floor is
    applied here. Using the old signal's ML_S6_ELO_MIN_MATCHES (5)
    would exclude ELO from matches the tested numbers included it on."""
    gap_info = elo_ops.get_elo_gap(player_name, opponent_name)
    if gap_info is None:
        return None
    return gap_info["overall_gap"]


def evaluate_moneyline(player_name: str, opponent_name: str, player_stats: dict,
                        opponent_stats: dict, classification: str, surface: str,
                        tournament_id, weights: dict, tour: str = "atp") -> dict:
    """
    Moneyline v2.0 (2026-08-12) — REAL BLENDED ENGINE, replaces the old
    Phase 1 hard gate + Phase 2 adjustment system entirely.

    Real, full walk-forward backtest against Dataset B (519,864 real
    matches): old system fired on 69,102 matches at 64.6% accuracy
    (AUC 0.6155). New blended engine fires on 380,102 matches (5.5x
    the real volume) at 69.8% accuracy overall (AUC 0.7199), including
    321,341 real matches the old hard gate would have blocked
    entirely, at the SAME 69.8% accuracy on those specifically — not
    diluted. Every confidence band from 55% up clears the real,
    independently-verified PrizePicks 6-pick Flex breakeven (54.2%/leg,
    re-derived directly from the live payout table — not the earlier
    65.83% figure this system used to be checked against).

    Uses confidence_engine.compute_moneyline_confidence(): ELO's own
    real win-probability formula as the foundation (43.5% weight, its
    real tested AUC margin above 0.50), S5/S1/H2H blended in via real
    historically-earned calibration tables built from Dataset B — not
    arbitrary thresholds. No hard cliff: every match with any real
    signal gets a real probability, always. "Too close to call"
    (45-55%) is a separate honest label, never a silent forced
    NO_PLAY reason with the real number thrown away. Real conflict
    detection between the top two weighted signals — the direct fix
    for the Darderi-style problem (two real signals disagreeing,
    previously resolved by silently picking the more optimistic story
    instead of flagging the disagreement).
    """
    tables = _load_real_calibration_tables()
    player_name = _clean_player_name(player_name)
    opponent_name = _clean_player_name(opponent_name)

    # Real dominance/S5 computation reused unchanged — same real
    # formula as before, still the source of S5's raw value. NOTE
    # (2026-08-12 fix, caught on post-deploy verification): missing
    # S5 stats NO LONGER short-circuit to NO_PLAY — the backtest that
    # produced the real 380,102-match/69.8% result ran the engine with
    # s5_raw=None on every match missing S5 data and let it
    # renormalize around ELO/S1/H2H (only ~177K of those 500K matches
    # had complete S5 data). An early return here structurally
    # recreates the old hard-gate problem and makes the tested
    # numbers unreachable. If NO real signal exists at all, the
    # engine itself returns None and the honest NO_PLAY below fires.
    dominance = compute_ml_dominance_score(player_stats, opponent_stats, weights)
    if dominance.get("status") == "INCOMPLETE_DATA":
        s5_raw = None  # engine renormalizes around whatever IS real
    else:
        s5_raw = dominance.get("dominance_score")

    elo_gap = _raw_elo_gap(player_name, opponent_name)
    s1_raw = _raw_s1_gap(player_name, opponent_name, surface)
    h2h_raw = _raw_h2h_gap(player_name, opponent_name, tour=tour)

    result = confidence_engine.compute_moneyline_confidence(
        elo_gap=elo_gap,
        s5_raw=s5_raw, s5_table=tables["s5"],
        s1_raw=s1_raw, s1_table=tables["s1"],
        h2h_raw=h2h_raw, h2h_table=tables["h2h"],
    )

    signals_debug = {
        c.name: {"raw_value": c.raw_value, "probability": c.probability,
                 "weight": round(c.weight, 4), "included": c.included,
                 "reason_excluded": c.reason_excluded}
        for c in result.contributions
    }

    # No real signal available at all — honest NO_PLAY, never a fabricated read.
    if result.win_probability is None:
        return {
            "status": "NO_PLAY",
            "reason": "No real signals available for this match",
            "phase1_winner": None,
            "phase2_winner": None,
            "signals": signals_debug,
            "dominance": dominance,
        }

    predicted_side = "player" if result.win_probability > 0.5 else "opponent"
    confidence_pct = round(max(result.win_probability, 1 - result.win_probability) * 100, 1)

    # 2026-08-14 fix (#5): result.american_odds is computed from
    # player_a's perspective. When the pick is the OPPONENT, showing
    # player_a's odds (e.g. +240) for a -240 favorite pick reads
    # backwards. Report odds for the side actually being picked.
    pick_probability = result.win_probability if predicted_side == "player" else (1 - result.win_probability)
    pick_odds = confidence_engine.probability_to_american_odds(pick_probability)

    # Too close to call (45-55%) — honest label, real number still
    # carried through (same spirit as the old Flow D disagreement case:
    # never silently discard the real read).
    if result.advisory_label is not None:
        return {
            "status": "NO_PLAY",
            "reason": "Too close to call — real blended signals land near a coin flip",
            "advisory": True,
            "suggested_winner": predicted_side,
            "suggested_confidence": confidence_pct,
            "win_probability": round(result.win_probability, 4),
            "american_odds": pick_odds,
            "phase1_winner": predicted_side,
            "phase2_winner": predicted_side,
            "conflict_detected": result.conflict_detected,
            "conflict_note": result.conflict_note,
            "signals": signals_debug,
            "dominance": dominance,
        }

    winner_name = player_name if predicted_side == "player" else opponent_name
    color = "GREEN" if confidence_pct >= 70 else "YELLOW"

    # 2026-08-15 single-signal cap. Real live results: Cecchinato 75.7%
    # GREEN on ELO alone (lost). A number resting on ONE signal has no
    # corroboration -- the probability stays honest and unchanged, but
    # the BAND cannot be GREEN. Divergence (strong top signal, lukewarm
    # second -- Djokovic 89%/61% -> 79.6% GREEN, lost) is surfaced as a
    # warning note only; it does not change the band (thresholds are a
    # first cut, not backtested).
    single_signal = bool(getattr(result, "single_signal", False))
    divergence_detected = bool(getattr(result, "divergence_detected", False))
    divergence_note = getattr(result, "divergence_note", None)
    cap_note = None
    if single_signal and color == "GREEN":
        color = "YELLOW"
        cap_note = ("SINGLE-SIGNAL CAP: only one real signal contributed -- "
                    "band capped at YELLOW (probability unchanged)")

    notes = []
    if result.conflict_detected:
        notes.append("Real conflict between top signals — see conflict_note")
    if cap_note:
        notes.append(cap_note)
    if divergence_detected:
        notes.append("Signal divergence — see divergence_note")

    return {
        "status": color,
        "winner": winner_name,
        "confidence": confidence_pct,
        "win_probability": round(result.win_probability, 4),
        "american_odds": pick_odds,
        "signal_agreement": f"{sum(1 for c in result.contributions if c.included)}/4",
        "confirmed_signal_count": sum(1 for c in result.contributions if c.included),
        "signals": signals_debug,
        "dominance": dominance,
        "conflict_detected": result.conflict_detected,
        "conflict_note": result.conflict_note,
        "single_signal": single_signal,
        "single_signal_cap_applied": cap_note is not None,
        "divergence_detected": divergence_detected,
        "divergence_note": divergence_note,
        "note": " | ".join(notes) if notes else None,
        "phase1_winner": predicted_side,
        "phase2_winner": predicted_side,
        # phase2_total / phase2_direction / phase2_band / phase2_confirmation_status
        # deliberately NOT included — those were specific to the old Phase 2
        # five-signal math, which no longer exists. Faking values into
        # those fields for old dashboard/archive compatibility would be
        # fabricating numbers with no real meaning. Any downstream display
        # or archive code still reading them should be updated to use
        # win_probability / conflict_detected / confirmed_signal_count instead.
    }


def _OLD_evaluate_moneyline_RETIRED(player_name: str, opponent_name: str, player_stats: dict,
                        opponent_stats: dict, classification: str, surface: str,
                        tournament_id, weights: dict, tour: str = "atp") -> dict:
    """
    RETIRED 2026-08-12 — kept only for reference/rollback, no longer
    called from anywhere. Real replacement above uses
    confidence_engine.py's blended engine instead.

    Moneyline v1.4 (locked spec, this session) — full aggregation and
    agreement rewrite. Phase 1 (numeric dominance score) remains the
    PRIMARY decision engine; Phase 2 (four-signal graduated
    confirmation, S1/S3/S5/S6 as of 2026-08-07 — S2/S4 retired after
    real backtesting showed no genuine signal, ELO/S6 added after
    real backtesting showed AUC 0.7223) can only
    confirm, reduce confidence, or invalidate Phase 1's call. Phase 2
    can never independently create a play Phase 1 didn't already find,
    and can never upgrade Phase 1's confidence beyond its own ceiling.

    This directly fixes the flaw exposed by the Osaka/Muchova review:
    real per-signal edges (a 15pp surface gap, a genuine tournament
    round gap, etc.) were previously collapsed into simple yes/no
    votes, discarding magnitude. Signals now return a graduated
    None/-2/-1/0/+1/+2 value (see signal_s1_surface_form docstring for
    the full None-vs-0 distinction), summed here into a single
    phase2_total, banded by strength, and read separately for
    direction — so a weak +1 lean is never confused with a genuine 0
    neutral read.

    AGREEMENT FLOW (locked spec section 5):
      A. Phase 1 shows no meaningful edge (confidence_ceiling is None,
         i.e. dominance magnitude < 8) -> NO_PLAY. Phase 2 cannot
         override a Phase 1 that found nothing.
      B. Phase 1 has an edge, Phase 2 direction is neutral (total==0)
         -> retain Phase 1's side, apply the largest reduction (-8%).
      C. Phase 1 has an edge, Phase 2 direction agrees (including a
         weak |total|==1 lean, per the locked edge-case ruling) ->
         retain Phase 1's side, reduction scales with Phase 2's band
         strength per the fixed table below.
      D. Phase 1 has an edge, Phase 2 direction is OPPOSITE -> NO_PLAY,
         hard gate, no override, same discipline as the original design.

    S3 (H2H) is now a real fifth signal, same graduated shape as the
    other four (see signal_s3_h2h docstring) — fully summed into
    phase2_total below, no longer excluded.
    """
    # Phase 1 — primary decision engine, unchanged mechanically
    dominance = compute_ml_dominance_score(player_stats, opponent_stats, weights)
    phase1_winner = dominance.get("predicted_side")
    phase1_ceiling = dominance.get("confidence_ceiling")

    # Phase 2 — real graduated signals (None/-2/-1/0/1/2 each).
    # RETIRED 2026-08-07 (real, backtested decision, not a guess):
    # S2 (tournament form) and S4 (physical fatigue) removed from live
    # aggregation. Both were walk-forward backtested against Dataset B's
    # full real history (519,864 matches, post player_a/b labeling-bug
    # fix) in their original live form AND in 3 research-motivated
    # refinements each (7 total variants) -- EVERY variant of both
    # signals came back at or below a coin flip (best S2 result: AUC
    # 0.506; best S4 result: AUC 0.511). Independent published research
    # on both topics (tennis fatigue-vs-outcome studies; cross-match
    # "hot hand"/momentum literature) broadly corroborates these null
    # results rather than contradicting them. Real functions
    # (signal_s2_tournament_form, signal_s4_physical_state) are left
    # intact below/elsewhere in this file for reference and possible
    # future reuse if a genuinely different framing is ever proposed --
    # they are just no longer called here. Real remaining Phase 2
    # signals: S1 (AUC 0.589, real), S3/H2H (AUC 0.584, real), S5.
    s1 = signal_s1_surface_form(player_name, opponent_name, surface)
    s3 = signal_s3_h2h(player_name, opponent_name, tour=tour)
    s5 = signal_s5_classification_confirmation(dominance, classification)
    s6 = signal_s6_elo(player_name, opponent_name)

    signals = {"s1": s1, "s3": s3, "s5": s5, "s6": s6}
    confirmed_values = [v for v in (s1, s3, s5, s6) if v is not None]
    confirmed_signal_count = len(confirmed_values)
    phase2_total = sum(confirmed_values) if confirmed_values else 0

    # Direction: derived purely from sign (locked spec section 2)
    if phase2_total > 0:
        phase2_direction = "player"
    elif phase2_total < 0:
        phase2_direction = "opponent"
    else:
        phase2_direction = "neutral"

    # Band: derived purely from magnitude (locked spec section 3)
    phase2_magnitude = abs(phase2_total)
    if phase2_magnitude >= ML_PHASE2_GREEN_CAND_MIN:
        phase2_band = "GREEN_CANDIDATE"
    elif phase2_magnitude >= ML_PHASE2_YELLOW_MIN:
        phase2_band = "YELLOW"
    elif phase2_magnitude >= ML_PHASE2_LEAN_MIN:
        phase2_band = "LEAN"
    else:
        phase2_band = "NONE"

    # phase2_confirmation_status (locked spec section 8) — derived by
    # comparing phase2_direction against phase1_winner, independent of
    # band strength, so a weak +1 that agrees is still logged as AGREE.
    if phase1_winner is None or phase2_direction == "neutral":
        phase2_confirmation_status = "NEUTRAL"
    elif phase2_direction == phase1_winner:
        phase2_confirmation_status = "AGREE"
    else:
        phase2_confirmation_status = "DISAGREE"

    debug_fields = {
        "phase2_total":              phase2_total,
        "phase2_direction":          phase2_direction,
        "phase2_band":               phase2_band,
        "phase2_confirmation_status": phase2_confirmation_status,
        "confirmed_signal_count":    confirmed_signal_count,
    }

    # ── FLOW A: Phase 1 shows no meaningful edge — Phase 2 cannot override ──
    if phase1_winner is None or phase1_ceiling is None:
        # Bug fix: this branch used to hardcode "dominance magnitude too
        # low" regardless of WHY phase1_winner/ceiling came back None —
        # collapsing a genuinely close matchup (real data, computed,
        # magnitude just under 8) and missing input data entirely
        # (INCOMPLETE_DATA, required stat fields absent, no magnitude
        # was ever computed) into the same false reason. Same principle
        # already applied elsewhere in this codebase to SYSTEM_FAILURE
        # ("must never be presented as a NO_PLAY judgment") — a missing-
        # data NO_PLAY must say so honestly, not claim a close matchup
        # that was never actually measured.
        if dominance.get("status") == "INCOMPLETE_DATA":
            missing = ", ".join(dominance.get("missing_fields", [])) or "required stats"
            reason = f"Missing required stat data ({missing}) — could not compute a dominance score at all"
        else:
            reason = "Phase 1 shows no meaningful structural edge (dominance magnitude too low)"
        return {
            "status": "NO_PLAY",
            "reason": reason,
            "phase1_winner": phase1_winner,
            "phase2_winner": phase2_direction if phase2_direction != "neutral" else None,
            "signals": signals,
            "dominance": dominance,
            **debug_fields,
        }

    # ── FLOW D: Phase 1 has an edge, Phase 2 direction is OPPOSITE ──
    # 2026-08-04, user-approved: Phase 1's real winner/confidence is no
    # longer silently thrown away here. status stays "NO_PLAY" (so every
    # existing d1.no_play()/skip-posting check downstream keeps working
    # unchanged), but "advisory": True plus suggested_winner/confidence
    # now carry the real Phase 1 read through to the message-formatting
    # layer, so the user sees the actual number and can decide for
    # themselves rather than it being fully discarded.
    if phase2_direction != "neutral" and phase2_direction != phase1_winner:
        return {
            "status": "NO_PLAY",
            "reason": "Phase 1 / Phase 2 direction disagreement",
            "advisory": True,
            "suggested_winner": phase1_winner,
            "suggested_confidence": phase1_ceiling,
            "phase1_winner": phase1_winner,
            "phase2_winner": phase2_direction,
            "signals": signals,
            "dominance": dominance,
            **debug_fields,
        }

    # ── FLOW B/C: Phase 1 has an edge, Phase 2 is neutral or agrees ──
    # Confidence reduction table (locked spec section 7) — Phase 2 can
    # only ever pull Phase 1's own ceiling DOWN, never add to it.
    if phase2_confirmation_status == "NEUTRAL":
        reduction = 8  # Flow B — weakest confirmation state
    elif phase2_band in ("GREEN_CANDIDATE", "YELLOW"):
        reduction = 0
    elif phase2_band == "LEAN":
        reduction = 3
    else:  # NONE band, but a real (non-zero) directional agreement
        reduction = 5

    confidence = max(0, phase1_ceiling - reduction)
    confidence = min(confidence, dominance.get("data_tier_cap_applied", confidence))

    color = "GREEN" if confidence >= 70 else "YELLOW"

    winner_name = player_name if phase1_winner == "player" else opponent_name
    return {
        "status": color,
        "winner": winner_name,
        "confidence": confidence,
        "signal_agreement": f"{confirmed_signal_count}/5",
        "signals": signals,
        "dominance": dominance,
        "note": "S3 (H2H) unavailable this match — insufficient sample or vendor data, excluded from this Phase 2 total" if signals["s3"] is None else None,
        **debug_fields,
    }

def build_moneyline_evaluation_content(player: str, opponent: str, surface: str,
                                        tournament_id=None, tour: str = "atp",
                                        tournament_name: str = None) -> tuple:
    """
    v7.9.6 (Step 8). Fetches real V8 data, computes the Step 8 numeric
    dominance score and all five Phase 2 signals in Python, then
    formats everything into content for
    the LLM to reason over for Steps 1-7 and 9-12 — the same pattern
    props already use: real numbers computed and injected, not left
    for the model to estimate on its own.

    tournament_name (2026-08-04, wired in): when a matching PROP
    evaluation of this exact real match already exists (same players,
    same tournament, recent), its classification is reused instead of
    the mechanical rank-gap proxy below — see the reuse block right
    before `classification` gets set. tournament_id alone isn't enough
    for that lookup since Dataset A stores tournament by name, not ID.

    Returns (content_string, precomputed_result_dict) — the caller
    needs the precomputed result separately for the post-response
    cross-check.
    """
    if not _V8_AVAILABLE:
        return None, None

    packet = build_tennis_packet(player, opponent, surface, tour)
    packet_text = format_packet_for_eval(player, opponent, packet)

    p_stats = packet.get("player", {})
    o_stats = packet.get("opponent", {})

    # Reuse a matching PROP evaluation's classification when a safe,
    # real match exists (2026-08-04) -- checked FIRST, before falling
    # back to the mechanical rank-gap proxy. Real match-identity
    # safeguard already built into find_matching_prop_classification()
    # (same players AND same tournament, tight recency window) --
    # never just player names. Fails open to the proxy on any miss.
    reused_from = None
    classification = None
    if _DAQ_AVAILABLE and tournament_name:
        reuse = d1q.find_matching_prop_classification(player, opponent, tournament_name)
        if reuse and reuse.get("classification") in ("LOPSIDED", "COMPETITIVE"):
            classification = reuse["classification"]
            reused_from = reuse["eval_id"]

    p_rank = p_stats.get("ranking")
    o_rank = o_stats.get("ranking")
    rank_gap = abs(p_rank - o_rank) if p_rank and o_rank else 0

    if classification is None:
        # Mechanical classification proxy for the Python-side computation
        # only — the LLM independently reaches its own classification in
        # Step 5 narratively; this is just what Step 8/Phase 2 need as an
        # input to run before the LLM response exists.
        classification = "LOPSIDED" if rank_gap >= 25 else "COMPETITIVE"

    weights = load_weights()
    precomputed = evaluate_moneyline(
        player, opponent, p_stats, o_stats, classification,
        surface, tournament_id, weights, tour=tour
    )
    if precomputed is not None:
        precomputed["classification_reused_from"] = reused_from

    # Bug fix (this session): evaluate_moneyline's return dicts carry
    # no research_data at all, and this function used to discard
    # `packet` after formatting it into the prompt text. Downstream,
    # moneyline_evaluation_agent builds compliance_input with
    # final.get("research_data", {}) -- which was therefore ALWAYS {},
    # so compliance_agent's entire "CONFIRMED V8 GROUND TRUTH" block
    # (match dates, DF averages, hold pcts, tournament counts) read
    # MISSING on every moneyline audit ever run, regardless of how much
    # real data existed. The Truth Lock and Rule30 checks were
    # structurally blind for moneyline -- not a data-quality problem, a
    # missing wire. Attach the packet in the same shape the props path
    # uses (research_data.v8_packet + player/opponent stat dicts) so
    # the SAME compliance ground-truth fix made this session (sourcing
    # from rd["v8_packet"]) serves both pipelines with no special
    # casing. Both moneyline entry paths (CLI and screenshot intake)
    # converge on moneyline_evaluation_agent, so this single wire
    # covers both.
    precomputed["research_data"] = {
        "player":    p_stats,
        "opponent":  o_stats,
        "v8_packet": packet,
    }

    # Bug fix (this session): the props path fires a V8 DATA QUALITY
    # ALERT to #alerts-channel whenever a player's tier is MISSING or
    # Tier3, with resolution_failed-aware wording -- moneyline had NO
    # data-quality signal of any kind. A moneyline pick built on a
    # failed resolution or thin data ran completely silently. Mirror
    # the props alert exactly: same dedup set (so a props alert and a
    # moneyline alert for the same match don't double-post), same
    # tier-description logic distinguishing "player not found at all"
    # from "real player, zero prior matches on this surface".
    _dq      = packet.get("data_quality", {}) or {}
    _ml_p_tier = _dq.get("player_tier", "MISSING")
    _ml_o_tier = _dq.get("opponent_tier", "MISSING")
    if "MISSING" in (_ml_p_tier, _ml_o_tier) or "Tier3" in (_ml_p_tier, _ml_o_tier):
        _p_res_failed = (packet.get("player", {}) or {}).get("resolution_failed", False)
        _o_res_failed = (packet.get("opponent", {}) or {}).get("resolution_failed", False)

        def _ml_tier_desc(tier, res_failed):
            if tier != "MISSING":
                return tier
            if res_failed:
                return "MISSING (player could not be resolved/found in V8 at all)"
            return ("MISSING (real player, real data on file -- zero PRIOR "
                    "matches on this surface specifically, e.g. a surface "
                    "debut; not a missing player)")

        _ml_match_key = tuple(sorted([player, opponent]))
        if _ml_match_key not in _v8_quality_alerted_matches:
            _v8_quality_alerted_matches.add(_ml_match_key)
            send_discord("alerts",
                f"\u26a0\ufe0f **V8 DATA QUALITY ALERT (moneyline)** \u2014 {player} vs {opponent}\n"
                f"Player tier: {_ml_tier_desc(_ml_p_tier, _p_res_failed)}\n"
                f"Opponent tier: {_ml_tier_desc(_ml_o_tier, _o_res_failed)}\n"
                # 2026-08-14 fix: no cap claim here anymore — data-tier caps
                # are PROPS-ONLY as of the v2.0 engine (missing data is
                # charged inside the moneyline blend via signal exclusion
                # + renormalization, never as a stacked cap).
                f"Missing data is handled inside the blended engine (affected "
                f"signals excluded and shown as such in the evaluation).\n"
                f"Verify this matchup's data manually before trusting the pick."
            )

    signals_lines = "\n".join(
        f"  {name}: raw={info['raw_value']}, probability={info['probability']}, "
        f"included={info['included']}"
        + (f", excluded because: {info['reason_excluded']}" if not info['included'] else "")
        for name, info in precomputed.get("signals", {}).items()
    ) or "  (no signal detail available)"

    content = f"""
MONEYLINE EVALUATION REQUEST
Player: {player} | Opponent: {opponent} | Surface: {surface} | Tour: {tour.upper()}

{packet_text}

═══════════════════════════════════════════════════
PRE-COMPUTED REAL BLENDED RESULT (Python, real data — 2026-08-12 engine)
═══════════════════════════════════════════════════
IMPORTANT — READ THIS BEFORE REASONING: this evaluation now uses a
NEW engine (confidence_engine.py), not the old Phase 1 gate / Phase 2
five-signal system your general instructions describe. The old
"Agreement Rule" and "S1/S3/S5/S6 graduated signal" framework
referenced elsewhere in your instructions is RETIRED as of today —
do NOT apply it here, do NOT require your own narrative conclusion to
"agree" with a Phase 1/Phase 2 result, and do NOT force NO_PLAY just
because a signal below is excluded. The real result below is final —
your job is to explain WHY it makes sense using the real stats shown,
not to independently gate, override, or second-guess it.

Real blended win probability: {precomputed.get('win_probability')}
Real American odds equivalent: {precomputed.get('american_odds')}
Real signal contributions (ELO/S5/S1/H2H, weighted by real tested AUC):
{signals_lines}

Real conflict between top signals detected: {precomputed.get('conflict_detected')}
{"Conflict detail: " + str(precomputed.get('conflict_note')) if precomputed.get('conflict_detected') else ""}
{"SINGLE-SIGNAL CAP APPLIED (2026-08-15): only ONE real signal contributed. The probability is honest but uncorroborated -- band is capped at YELLOW. Say so plainly in your write-up." if precomputed.get('single_signal_cap_applied') else ""}
{"Signal divergence: " + str(precomputed.get('divergence_note')) if precomputed.get('divergence_detected') else ""}

Pre-computed overall result: {precomputed['status']}
{"Winner (pre-computed): " + precomputed.get('winner', '') if precomputed.get('winner') else ""}
{"Confidence (pre-computed): " + str(precomputed.get('confidence', '')) + "%" if precomputed.get('confidence') else ""}
{"Reason for NO_PLAY: " + precomputed.get('reason', '') if precomputed['status'] == 'NO_PLAY' else ""}

Explain the real stats behind this result narratively (Steps 1-7 and
9-12 of your usual process still apply for research/context), but
the final grade and winner above are not yours to change.

Output your final answer in this exact format:
Player — MONEYLINE WIN — [WIN emoji] — Color Grade
or, if NO PLAY:
NO PLAY — [one sentence reason]
"""
    return content, precomputed

def _is_doubles_entry(name: str) -> bool:
    """
    v7.9.7 fix (item 2 — doubles rejection). The master prompt already
    says "Doubles: Any doubles = SKIP" (confirmed in real reasoning
    output), but nothing in code enforced it — a doubles slip like
    "Noskova L / Sra..." was falling through into singles-shaped V8
    lookups, where truncated/team-style names either resolved
    incorrectly or failed resolution outright (see item 5's
    resolution_failed fix) and produced a slow, confusing NO PLAY
    instead of an immediate, clear rejection.

    Detects the team-name pattern doubles players are shown in on
    sportsbook screenshots: two names joined by "/", e.g.
    "Noskova L / Sra..." or "Piter K / Siskova...". A single name never
    legitimately contains "/", so this pattern is a reliable signal —
    much more reliable than trying to detect the word "doubles" in
    surrounding text, which may not always be present in a manual or
    partially-parsed entry.
    """
    if not name:
        return False
    return "/" in name


def moneyline_evaluation_agent(player: str, opponent: str, surface: str,
                                tournament_id=None, tour: str = "atp",
                                tournament_name: str = None) -> dict:
    """
    Full moneyline pipeline: builds content (V8 data + pre-computed
    Step 8/Phase 2), calls the live model for Steps 1-7/9-12
    narrative reasoning, then cross-checks the model's stated
    conclusion against the pre-computed Python result. Disagreement
    between the two is NOT resolved by trusting the model's prose —
    it forces NO_PLAY, same defense-in-depth pattern used elsewhere
    in this system (e.g. Gate 11-B format contamination checks run
    both in the prompt AND as a hard Python check afterward).
    """
    # Dataset A (Priority: Moneyline Integration, approved after full
    # trace). eval_id minted here, at the true single entry point both
    # callers (CLI --moneyline and screenshot intake) funnel through --
    # moneyline has no supervisor_agent/process_single_prop equivalent,
    # so this function IS the orchestrator for capture purposes.
    _ml_eval_id = d1.new_eval_id()
    d1.start(_ml_eval_id, "MONEYLINE", player, opponent, tour=tour.upper() if tour else None,
             surface=surface, pipeline_version=CORE_VERSION, _logger=log)
    d1.heartbeat("research", "WORKING", current_task=f"{player} vs {opponent}", _logger=log)

    # v7.9.7 fix (item 2): doubles check runs FIRST, before any V8
    # lookup, research, or model call — the whole point is to reject
    # cleanly and immediately rather than let a doubles slip wander
    # into singles-shaped resolution attempts.
    if _is_doubles_entry(player) or _is_doubles_entry(opponent):
        log("Moneyline", f"Doubles entry rejected: {player} vs {opponent} — doubles not supported")
        send_discord("moneyline",
            f"🚫 **DOUBLES NOT SUPPORTED** — {player} vs {opponent}\n"
            f"This system only evaluates singles matches. Doubles slip skipped."
        )
        d1.terminate(_ml_eval_id, "MANUAL_REJECT", "doubles not supported — singles only", _logger=log)
        d1.heartbeat("research", "IDLE", _logger=log)
        return {"status": "ERROR", "reason": "Doubles not supported — singles only",
                "eval_id": _ml_eval_id}

    content, precomputed = build_moneyline_evaluation_content(
        player, opponent, surface, tournament_id, tour,
        tournament_name=tournament_name
    )
    if content is None:
        d1.terminate(_ml_eval_id, "RESEARCH_FAILURE", "V8 API unavailable", _logger=log)
        d1.heartbeat("research", "FAILED", detail="V8 API unavailable",
                     increment_failure=True, _logger=log)
        return {"status": "ERROR", "reason": "V8 API unavailable", "eval_id": _ml_eval_id}
    d1.transition(_ml_eval_id, "RESEARCH_COMPLETE", _logger=log)
    d1.heartbeat("research", "IDLE", increment_success=True, _logger=log)

    log("Moneyline", f"Evaluating: {player} vs {opponent} ({surface})")
    d1.heartbeat("evaluation", "WORKING", current_task=f"{player} vs {opponent}", _logger=log)
    result_text, stop_reason, usage = call_model(MODEL_SONNET, MASTER_PROMPT, content,
                              temperature=0.2, max_tokens=6000, enable_cache=True,
                              return_stop_reason=True,
                              eval_id=_ml_eval_id, step_type="moneyline_evaluation")
    if not result_text:
        d1.terminate(_ml_eval_id, "EVAL_ATTEMPTS_EXHAUSTED", "model call failed", _logger=log)
        d1.heartbeat("evaluation", "FAILED", detail="model call failed",
                     increment_failure=True, _logger=log)
        return {"status": "ERROR", "reason": "Model call failed", "precomputed": precomputed,
                "eval_id": _ml_eval_id}


    # Fix (this session, consistency with the props path): a truncated
    # moneyline evaluation used to flow straight into the cross-check
    # logic below as if it were a complete response -- the model_says_
    # no_play / model_winner regex searches would run against an
    # incomplete narrative that never reached its actual conclusion.
    # Same principle as props: this is a structural failure (same
    # prompt, same model, same budget), not a transient one, so it
    # fails immediately with full diagnostics rather than being scored
    # or cross-checked as if real reasoning happened.
    if stop_reason == "max_tokens":
        diag = {
            "master_prompt_chars": len(MASTER_PROMPT),
            "content_chars": len(content),
            "output_tokens_used": usage.get("output_tokens"),
            "max_tokens_limit": 6000,
        }
        log("Moneyline", f"SYSTEM FAILURE — {player} vs {opponent} truncated "
            f"(output_tokens_used={usage.get('output_tokens', '?')}). No pick generated. "
            f"Diagnostics: {diag}")
        send_discord("alerts",
            f"🛑 **SYSTEM FAILURE — NO PICK GENERATED (moneyline)** — "
            f"{player} vs {opponent}\n"
            f"CAUSE: Evaluation hit max_tokens before reaching a conclusion.\n"
            f"DIAGNOSTICS — output tokens used: {usage.get('output_tokens', '?')} of "
            f"6000 limit.\n"
            f"No retry attempted (structural failure, not transient)."
        )
        d1.terminate(_ml_eval_id, "EVAL_TRUNCATED", str(diag), _logger=log)
        d1.heartbeat("evaluation", "FAILED", detail="truncated", increment_failure=True, _logger=log)
        return {"status": "SYSTEM_FAILURE", "reason": "Evaluation truncated (max_tokens)",
                "precomputed": precomputed, "diagnostics": diag, "eval_id": _ml_eval_id}

    model_says_no_play = bool(re.search(r"NO\s*PLAY", result_text[int(len(result_text)*0.6):], re.IGNORECASE))
    model_winner_match = re.search(
        r"([A-Za-z .\-']+)\s*—\s*MONEYLINE WIN", result_text[int(len(result_text)*0.6):]
    )
    model_winner = model_winner_match.group(1).strip() if model_winner_match else None

    final = dict(precomputed)
    final["model_output"] = result_text
    final["model_winner"] = model_winner
    final["model_said_no_play"] = model_says_no_play
    final["eval_id"] = _ml_eval_id

    # Calibration tracking (this session): log which graduated ML
    # signal bands actually fired on this real evaluation, tied to
    # eval_id, so calibration_status_report() can eventually report a
    # real hit rate instead of these staying permanently "PROVISIONAL"
    # in a comment nobody queries. Only signals that returned a
    # non-None value fired (None means genuinely unconfirmed, not a
    # band decision — see each signal_sN function's own docstring).
    # Fail-open by construction (record_provisional_rule_fire already
    # swallows its own exceptions) — never allowed to affect a pick.
    # S2/S4 removed 2026-08-07 -- retired from live signals, see the
    # real backtest evidence in evaluate_moneyline()'s comment block.
    # S6 added same day after its own real backtest (AUC 0.7223).
    _signal_to_rule = {
        "s1": "ML_S1_Surface_Bands",
        "s5": "ML_S5_Classification_Bands",
        "s6": "ML_S6_ELO_Bands",
    }
    for _sig_key, _rule_name in _signal_to_rule.items():
        if final.get("signals", {}).get(_sig_key) is not None:
            record_provisional_rule_fire(_rule_name, _ml_eval_id, player=player)

    # ── Cross-check: model's narrative conclusion vs. Python's pre-computed result ──
    precomputed_winner = precomputed.get("winner")
    if precomputed["status"] == "NO_PLAY" or model_says_no_play:
        final["status"] = "NO_PLAY"
        if precomputed["status"] != "NO_PLAY":
            final["reason"] = "Model's own reasoning concluded NO PLAY despite Phase 2 agreement"
    elif model_winner and precomputed_winner and model_winner.lower() != precomputed_winner.lower():
        final["status"] = "NO_PLAY"
        final["reason"] = (f"Cross-check failure: model said {model_winner}, "
                            f"pre-computed Phase 1/2 said {precomputed_winner} — "
                            f"disagreement forces NO PLAY")
        log("Moneyline", f"⚠️ CROSS-CHECK DISAGREEMENT — {final['reason']}")

    # Dataset A: NO_PLAY here means compliance never runs at all (see the
    # `if final.get("status") not in ("ERROR", "NO_PLAY")` gate right
    # below) -- this is the ONLY place to capture it, and it's a
    # successful evaluation that found no edge, not a failure. Kept on
    # the dedicated no_play() path per the same NO_PLAY philosophy
    # established for props/moneyline earlier this session.
    if final["status"] == "NO_PLAY":
        d1.no_play(_ml_eval_id, final.get("reason", "no play"),
                   advisory=final.get("advisory"),
                   suggested_winner=final.get("suggested_winner"),
                   suggested_confidence=final.get("suggested_confidence"),
                   signals=final.get("signals"),
                   dominance=final.get("dominance"),
                   phase2_band=final.get("phase2_band"),
                   phase2_confirmation_status=final.get("phase2_confirmation_status"),
                   confirmed_signal_count=final.get("confirmed_signal_count"),
                   _logger=log)
        d1.heartbeat("evaluation", "IDLE", increment_success=True, _logger=log)
    else:
        d1.transition(_ml_eval_id, "EVALUATION_COMPLETE",
                       eval_confidence=(final["confidence"] / 100.0
                                        if final.get("confidence") is not None else None),
                       reasoning_text=result_text[:20000] if result_text else None,
                       signals=final.get("signals"),
                       dominance=final.get("dominance"),
                       phase2_band=final.get("phase2_band"),
                       phase2_confirmation_status=final.get("phase2_confirmation_status"),
                       confirmed_signal_count=final.get("confirmed_signal_count"),
                       _logger=log)
        d1.heartbeat("evaluation", "IDLE", increment_success=True, _logger=log)

    # ── COMPLIANCE PASS (v7.9.7, item 14) ────────────────────────
    # Props already run every evaluation through compliance_agent before
    # a pick posts. Moneyline previously skipped this entirely — went
    # straight from research to evaluate_moneyline_from_signals with no
    # independent audit at all. This adapts moneyline's result shape
    # into what compliance_agent expects (it was built around prop
    # fields) and applies the same status handling used in
    # process_single_prop, rather than duplicating that logic separately.
    if final.get("status") not in ("ERROR", "NO_PLAY"):
        compliance_input = {
            "player":            player,
            "stat_type":         "moneyline",
            "line":              None,
            "direction":         final.get("winner") or final.get("model_winner"),
            "match_type":        final.get("match_type", ""),
            "is_favorite":       final.get("is_favorite"),
            "evaluation_output": result_text,
            "research_data":     final.get("research_data", {}),
        }
        d1.heartbeat("compliance", "WORKING", current_task=f"{player} vs {opponent}", _logger=log)
        audit = compliance_agent(compliance_input, attempt=1)
        final["compliance_status"] = audit["status"]
        final["compliance_flags"]  = audit.get("flags", [])

        if audit["status"] == "FLAGGED_FOR_MANUAL_REVIEW":
            # Architectural fix (this session, operator correction):
            # compliance audit failing to run (API/parse error) means
            # no real judgment was ever produced for this pick -- same
            # principle as a truncated props evaluation. Previously
            # this scored a pick at a capped confidence; now it's an
            # explicit SYSTEM_FAILURE with no confidence/winner data,
            # so a capped number can never be mistaken for a real
            # (if weak) judgment that actually happened.
            final["status"] = "SYSTEM_FAILURE"
            final["confidence"] = None
            final["winner"] = None
            final.pop("manual_review_cap", None)
            send_discord("alerts",
                f"🛑 **SYSTEM FAILURE — NO PICK GENERATED (moneyline)** — "
                f"{player} vs {opponent}\n"
                f"CAUSE: Compliance audit could not be parsed/run (API or parse error).\n"
                f"This is not a low-confidence pick -- no compliance judgment "
                f"was ever produced. Skipped, not graded."
            )
            d1.terminate(_ml_eval_id, "COMPLIANCE_UNPARSEABLE", _logger=log)
            d1.heartbeat("compliance", "FAILED", detail="unparseable",
                         increment_failure=True, _logger=log)
        elif audit["status"] not in ("APPROVED",):
            # Bug fix (this session): this used to set a bare boolean
            # (final["compliance_penalty"] = True) that NOTHING
            # downstream ever read -- the pick posted to Discord at its
            # original, unpenalized confidence with no visible mention
            # of the flag. Compliance existed for moneyline in name
            # only. Apply the SAME itemized Category A/B model
            # scoring_agent already uses for props, in this pipeline's
            # units (integer percentage points, vs props' fractions):
            # 3 points per Category-A flag, capped at 15; Category-B
            # (wording-only) flags cost nothing; a non-APPROVED status
            # with NO itemized flags at all is Category C (malformed
            # audit) and is routed to SYSTEM_FAILURE below, never
            # scored. Unclassified flags default to A
            # -- a missed real issue is worse than an over-cautious
            # flag. Recolor after the deduction so a penalized GREEN
            # can honestly drop to YELLOW.
            final["compliance_penalty"] = True  # kept for backward compat (archives/readers)
            all_flags = audit.get("flags", []) or []
            # Fix (this session, approved, mirrors the props-side
            # change): non-APPROVED with ZERO parseable flags is a
            # malformed audit output -- Category C, never scored. Route
            # it to SYSTEM_FAILURE like the existing parse-failure path
            # instead of fabricating a penalty.
            if not all_flags:
                final["compliance_penalty"] = False
                final["status"] = "SYSTEM_FAILURE"
                final["error"] = ("compliance returned non-APPROVED with no "
                                  "parseable flags (Category C — malformed audit)")
                send_discord("alerts",
                    f"🔴 **MALFORMED MONEYLINE AUDIT — no pick produced** — "
                    f"{player} vs {opponent}\n"
                    f"Compliance returned {audit.get('status')} with zero parseable "
                    f"flags. Category C system failure — held for manual review."
                )
                log("Moneyline", "Compliance non-APPROVED with no flags — "
                    "Category C, SYSTEM_FAILURE (no fabricated penalty)")
                d1.terminate(_ml_eval_id, "AUDIT_MALFORMED", _logger=log)
                d1.heartbeat("compliance", "FAILED", detail="AUDIT_MALFORMED",
                             increment_failure=True, _logger=log)
                # No early return: the end-of-function archive block
                # already runs for SYSTEM_FAILURE status and builds its
                # own dates/weights — fall through to it naturally.
            else:
                _a_flags = [f for f in all_flags if f.get("category", "A") != "B"]
                _b_flags = [f for f in all_flags if f.get("category") == "B"]
                _PER_FLAG_PTS = 3
                _MAX_PENALTY_PTS = 15
                penalty_pts = min(len(_a_flags) * _PER_FLAG_PTS, _MAX_PENALTY_PTS)
                final["compliance_category_a_penalty_pts"] = penalty_pts
                if penalty_pts and final.get("confidence") is not None:
                    pre = final["confidence"]
                    final["confidence"] = max(0, pre - penalty_pts)
                    final["status"] = "GREEN" if final["confidence"] >= 70 else "YELLOW"
                    final["compliance_note"] = (
                        f"Compliance: {len(_a_flags)} Category-A flag(s) "
                        f"[{', '.join(f.get('rule', '?') for f in _a_flags) or 'unspecified'}] "
                        f"— confidence {pre}% → {final['confidence']}%"
                    )
                elif _b_flags and not _a_flags:
                    final["compliance_note"] = (
                        f"Compliance: {len(_b_flags)} Category-B (wording only, "
                        f"no cost) flag(s) [{', '.join(f.get('rule', '?') for f in _b_flags)}]"
                    )
                log("Moneyline", f"Compliance flagged ({len(_a_flags)} Cat-A / "
                    f"{len(_b_flags)} Cat-B, -{penalty_pts} pts): {audit.get('flags')}")

        # Unified success-path capture: only reached when compliance
        # didn't already terminate this eval_id above (both failure
        # branches call d1.terminate() themselves). Moneyline has no
        # separate scoring_agent, so COMPLIANCE_COMPLETE and
        # SCORING_COMPLETE both land here, back to back, from the same
        # `final` dict that already holds the (possibly penalty-adjusted)
        # confidence and status.
        if final.get("status") not in ("SYSTEM_FAILURE",):
            _ml_flags = final.get("compliance_flags", []) or []
            _ml_compliance_category = (
                "A" if any(f.get("category", "A") == "A" for f in _ml_flags)
                else ("B" if _ml_flags else None)
            )
            # Calibration tracking (this session, closes the gap found
            # during the full-system audit): props' equivalent hook
            # reads rules_confirmed/rules_challenged from
            # _extract_rules() -- moneyline never calls that function at
            # all, so this reads the SAME information from its actual
            # source here: compliance_agent's own checks_passed/flags,
            # audit is already in scope from the compliance call above.
            # This is also the correct, real source for
            # ML_Gate14_HomeCourtLean -- previously fed (mistakenly)
            # from props' citations; now fed from an actual moneyline
            # evaluation's own compliance audit for the first time.
            # Rule30_CFEF_Stacking and CFEF_Trigger6 are included for
            # completeness but will likely fire rarely-to-never here:
            # this_tournament_3set_count (what CFEF's check reads) is
            # only ever populated by research_agent(), the PROPS
            # research path -- confirmed absent from moneyline's own
            # build_moneyline_evaluation_content() data. Left wired
            # rather than special-cased out: if that data gap ever
            # closes, this starts working with no further edit needed.
            _ml_audit_rule_names = set(audit.get("checks_passed") or []) | \
                                    {f.get("rule") for f in _ml_flags if f.get("rule")}
            _ml_compliance_check_to_registry = {
                "Rule9": "Rule9_MarketExclusion",
                "Gate14": "ML_Gate14_HomeCourtLean",
                "CFEF": "CFEF_Trigger6",
                "Rule30_CFEF_Stacking": "Rule30_CFEF_Stacking",
            }
            for _ml_cited_name, _ml_registry_key in _ml_compliance_check_to_registry.items():
                if _ml_cited_name in _ml_audit_rule_names:
                    record_provisional_rule_fire(_ml_registry_key, _ml_eval_id, player=player)
            d1.transition(_ml_eval_id, "COMPLIANCE_COMPLETE",
                           compliance_category=_ml_compliance_category,
                           compliance_notes=json.dumps(_ml_flags, default=str) if _ml_flags else None,
                           _logger=log)
            d1.heartbeat("compliance", "IDLE", increment_success=True, _logger=log)
            d1.heartbeat("scoring", "WORKING", current_task=f"{player} vs {opponent}", _logger=log)
            d1.transition(_ml_eval_id, "SCORING_COMPLETE",
                           final_confidence=(final["confidence"] / 100.0
                                             if final.get("confidence") is not None else None),
                           final_grade=final.get("status"),
                           direction="WIN" if final.get("status") in ("GREEN", "YELLOW") else None,
                           predicted_winner=final.get("winner"),
                           # compliance_cost capture (approved, this session):
                           # moneyline stores its penalty as integer points
                           # (0-15), props as a fraction (0.0-0.15) -- normalized
                           # to the fraction convention here so the two evaluation
                           # types share one consistent unit in Dataset A.
                           compliance_cost=final.get("compliance_category_a_penalty_pts", 0) / 100.0,
                           _logger=log)
            d1.heartbeat("scoring", "IDLE", increment_success=True, _logger=log)
    # ────────────────────────────────────────────────────────────

    # ── ARCHIVE FOR LEARNING (moneyline learning system) ─────────
    # Persist the signal breakdown/reasoning now, while it exists, so
    # the attribution classifier at slip-result time has something
    # real to compare the outcome against. Skip pure errors — nothing
    # meaningful to attribute if the call itself failed.
    if final.get("status") != "ERROR":
        try:
            _dates_for_archive   = get_system_dates()
            _weights_for_archive = load_weights()
            _archive_ml_pick(player, opponent, final, _weights_for_archive, _dates_for_archive)
        except Exception as _e:
            log("Moneyline", f"ML archive write failed (non-fatal): {_e}")
    # ────────────────────────────────────────────────────────────

    return final

def scoring_agent(evaluated, weights, compliance_penalty=False, compliance_flags=None,
                   eval_id=None):
    """
    Scoring agent — decision tree aligned (v7.9.4).

    Architecture:
    - Start from eval agent's concluded confidence (eval_confidence)
    - Apply hard caps from the decision tree
    - Direction-aware ceiling enforcement:
        LESS path:  HARD=GREEN eligible | EDGE=YELLOW max 69% | SOFT=YELLOW max 66% | UNSTABLE=YELLOW max 62%
        MORE path:  All 4 proven-competitive reqs met = GREEN eligible | any missing = YELLOW max 69%
    - Final color assignment

    What was removed (not in decision tree):
    - get_base_confidence() base table
    - surface_specialist_wrong penalty
    - underdog_gw_less, lopsided_tg_less, stage_competitive_tg_less signal math
    - favored_gw_less_trap, revenge_match, young_home_grand_slam signals
    - graduated competitive_reqs caps (0/1/2/3/4 tiers)
    """
    rd          = evaluated.get("research_data", {})
    context     = rd.get("match_context", {})
    player_data = rd.get("player", {})
    opp_data    = rd.get("opponent", {})
    direction   = evaluated.get("direction", "LESS")
    stat_type   = evaluated.get("stat_type", "")
    d1.heartbeat("scoring", "WORKING", current_task=evaluated.get("player"), _logger=log)
    has_gaps    = rd.get("has_gaps", False)

    # ── STEP 1: Start from eval agent's concluded confidence ──────────────────
    eval_conf = evaluated.get("eval_confidence")
    if eval_conf and 0.50 <= eval_conf <= 0.95:
        confidence = eval_conf
        # v7.9.7 (item 5): baseline captured here so we can show, at the
        # end of scoring, whether and why the final number moved away
        # from what the model itself concluded — previously a silent
        # disagreement between eval_confidence and the scored output.
        _eval_baseline = eval_conf
        log("Scoring", f"Eval confidence: {confidence:.0%}")
    else:
        # v7.9.7 fix (item 1): previously silently defaulted to 0.60
        # YELLOW and continued — meaning a completely broken/unreadable
        # evaluation still produced a postable pick card. Now the
        # evaluation is rejected outright rather than guessed at, and
        # flagged to #alerts so it's visible instead of silent.
        log("Scoring", "Eval confidence unreadable — evaluation REJECTED (no fallback confidence)")
        send_discord("alerts",
            f"🔴 **BROKEN EVAL — no pick produced** — {evaluated.get('player')} "
            f"{evaluated.get('line')} {stat_type}\n"
            f"eval_confidence was missing or unreadable. Evaluation discarded "
            f"rather than defaulted to a guessed confidence."
        )
        d1.terminate(eval_id, "BROKEN_EVAL",
                     "eval_confidence missing/unreadable", _logger=log)
        d1.heartbeat("scoring", "FAILED", detail="BROKEN_EVAL", increment_failure=True, _logger=log)
        return {**evaluated, "status": "BROKEN_EVAL", "confidence": None,
                "color": "RED", "emoji": "🔴",
                "note": "eval_confidence missing/unreadable — no pick produced"}

    # ── CONFIDENCE CONSERVATION LEDGER (this session, approved) ─────────────
    # Root cause (confirmed live, Djokovic DF 2.5, 2026-07-09): the scoring
    # override note only ever printed the COMPLIANCE penalty breakdown --
    # every cap-driven move (serve-stat gate, dominance ceilings, BP rule,
    # tier caps, floor) produced a bare "eval 72% -> scored 69%" with no
    # cause shown anywhere. Unexplained confidence loss is indistinguishable
    # from a bug -- and in the Djokovic case it masked a real one. Every
    # change to `confidence` inside this function is now recorded here with
    # its cause, and the override note prints the full itemized chain.
    # Invariant: eval_confidence minus/plus every ledger entry MUST equal
    # the final confidence. An override note with an empty ledger is a
    # HIGH-SEVERITY defect and fires an alert.
    _score_adjustments = []
    def _adj(cause, before, after):
        if round(after, 3) != round(before, 3):
            _score_adjustments.append(f"{cause}: {before:.0%}\u2192{after:.0%}")
        return after

    # ── STEP 2: HARD CAPS (decision tree, applied in order) ──────────────────

    # Data-quality ceiling — CONSOLIDATED this session (was 3 separate
    # caps: V8 tier cap, thin-data 68%, no-hold-data 68%). All three
    # answer the same underlying question -- "how much can this specific
    # evaluation's data actually be trusted" -- from three different
    # signals. They never stacked numerically (each was its own
    # min(confidence, X), and sequential min() is mathematically
    # identical to one min() across all applicable values), so THE
    # SCORE THIS PRODUCES DOES NOT CHANGE. What changes: one ledger
    # entry instead of up to three, one log line naming exactly which
    # signal(s) fired and which one actually bound the result, and one
    # place to read instead of three when auditing a pick's data
    # quality. Verified equivalent by direct test against the old
    # sequential-cap code path before this was merged.
    component_caps = []  # (label, cap_decimal) for every signal that fired

    v8_validation = rd.get("v8_validation", {})
    v8_cap = v8_validation.get("confidence_cap")
    if v8_cap and _V8_AVAILABLE:
        component_caps.append((
            f"V8 data-tier ({v8_validation.get('player_tier')}/{v8_validation.get('opponent_tier')})",
            v8_cap / 100.0))

    player_missing = player_data.get("missing_fields", [])
    opp_missing    = opp_data.get("missing_fields", [])
    thin_data_parse_failure = (
        "parse_failure" in player_missing or "parse_failure" in opp_missing)
    thin_data = (
        has_gaps or
        "all_data" in player_missing or
        "all_data" in opp_missing or
        thin_data_parse_failure or
        len(player_missing) >= 2 or
        len(opp_missing) >= 2
    )
    if thin_data:
        component_caps.append(("Thin-data", 0.68))

    player_hold = player_data.get("serve_hold_pct")
    opp_hold    = opp_data.get("serve_hold_pct")
    no_hold_data = (
        stat_type in ("total_games", "games_won", "break_points_won")
        and player_hold is None and opp_hold is None
    )
    if no_hold_data:
        component_caps.append(("No-hold-data", 0.68))

    if component_caps:
        binding_label, binding_cap = min(component_caps, key=lambda c: c[1])
        fired_labels = ", ".join(f"{lbl}({cap:.0%})" for lbl, cap in component_caps)
        if confidence > binding_cap:
            confidence = _adj(
                f"Data-quality ceiling {binding_cap:.0%} [binding: {binding_label}]",
                confidence, binding_cap)
        log("Scoring", f"DATA-QUALITY ceiling — {binding_cap:.0%} "
            f"(binding: {binding_label}; all signals fired: {fired_labels})")

        # Preserved unchanged from the old thin-data cap (this session,
        # approved): when parse_failure specifically is one of the fired
        # signals, a SYSTEM artifact (Category C) is affecting this
        # score, not just genuinely-missing data. Must never be silent,
        # regardless of which signal ended up binding the final ceiling.
        if thin_data_parse_failure:
            send_discord("alerts",
                f"⚠️ **PARSE FAILURE affected scoring** — "
                f"{evaluated.get('player')} {evaluated.get('line')} {stat_type}\n"
                f"A research-side parse failure contributed to the data-quality "
                f"ceiling. System artifact (Category C) is influencing this "
                f"confidence — review before trusting the pick."
            )

    # Cap 3: Format contamination
    if evaluated.get("format_contamination"):
        confidence = _adj("Format-contamination cap 62%", confidence, min(confidence, 0.62))
        log("Scoring", f"FORMAT CONTAMINATION cap — ceiling 62%")

    # Cap 3b (Category 9 Finding 2, approved): Gate 11 could not verify
    # this match exists (V8 unavailable at gate time). Per the standing
    # rule, unverifiable = hard YELLOW -- a pick on an unverified match
    # can never be GREEN, whatever the evidence says about the matchup.
    if evaluated.get("gate11_unverified"):
        confidence = _adj("Gate 11 unverified (V8 outage) cap 69%",
                          confidence, min(confidence, 0.69))
        log("Scoring", "GATE 11 UNVERIFIED cap — match existence not "
            "verified → YELLOW max 69%")

    # Cap 4: Compliance penalty (evaluation flagged and re-run)
    # Bug fix (this session, root cause identified from a real live
    # pick): this used to be `confidence = min(confidence, 0.66)` --
    # a flat clamp completely disconnected from how many flags were
    # actually still unresolved or how severe they were. A single
    # lingering flag and five lingering flags produced the identical
    # -to-66% result, and the size of the resulting deduction was
    # purely coincidental (however far the eval happened to sit above
    # 66%), not computed. Confirmed live: a 70% eval with 5 flags
    # showed "eval 70% -> scored 66%" with zero indication of which
    # rule cost how much -- an opaque override, not a confidence
    # model. Now computed the same way every other deduction in this
    # system works: itemized, per-cause, additive, and shown in full.
    # PROVISIONAL, not backtested (same status as other new numeric
    # constants in this system) -- calibrate once real outcome data
    # exists on re-evaluated picks specifically.
    # Bug fix (this session, confirmed live 3x independently -- Sinner
    # 12:31 AM log, Putintseva DF prop, Arseneault/Volynets prop):
    # initialized here (0.0), not inside the `if compliance_penalty`
    # block below, specifically so it's safely defined regardless of
    # whether that block runs -- STEP 3B's floor logic further down
    # needs to know whether a REAL, non-zero Category-A penalty was
    # applied, and referencing an undefined variable would crash.
    compliance_category_a_penalty = 0.0
    if compliance_penalty:
        all_flags = compliance_flags or []
        # Bug fix (this session, per real classification of tonight's
        # actual flags): only Category A (real reasoning failures --
        # ignored data, fabricated facts, broken logic) cost confidence.
        # Category B (a required sentence/template phrase wasn't
        # written even though the underlying number was already
        # correct) doesn't reflect on whether the pick is actually
        # trustworthy -- a prop isn't less likely to hit because a
        # citation phrase was missing. Confirmed from the real Kostyuk
        # case: Rule6's flag was purely "hold% was cited and used
        # correctly, but the explicit buffer-check sentence wasn't
        # written" -- that should never cost the same as an ignored
        # match_date. Unclassified flags (older auditor output without
        # the category field, or a missing/invalid value) default to A
        # -- a missed real issue is worse than an over-cautious flag.
        category_a_flags = [f for f in all_flags if f.get("category", "A") != "B"]
        category_b_flags = [f for f in all_flags if f.get("category") == "B"]
        flags = category_a_flags
        _PER_FLAG = 0.03
        _MAX_PENALTY = 0.15
        flag_count = len(flags)
        # Bug fix (this session, approved): FLAGGED with zero parseable
        # flags used to take the full -15% "manual review" penalty --
        # pricing an auditor OUTPUT MALFUNCTION (the auditor said
        # FLAGGED but produced nothing itemizable) identically to five
        # confirmed real reasoning failures. By this system's own
        # Category model that situation is Category C (system failure:
        # malformed audit output), and Category C is NEVER scored -- it
        # becomes NO PICK, same as truncation and audit parse failure
        # already do. A fabricated deduction implies a judgment that
        # was never actually made.
        if not all_flags:
            log("Scoring", "COMPLIANCE FLAGGED with NO parseable flags — "
                "Category C (malformed audit output). NO PICK produced, "
                "no fabricated penalty applied.")
            send_discord("alerts",
                f"🔴 **MALFORMED AUDIT — no pick produced** — "
                f"{evaluated.get('player')} {evaluated.get('line')} {stat_type}\n"
                f"Compliance returned FLAGGED but provided zero parseable flags. "
                f"Category C system failure — evaluation held for manual review "
                f"instead of taking a fabricated -{_MAX_PENALTY:.0%} deduction."
            )
            d1.terminate(eval_id, "AUDIT_MALFORMED",
                         "compliance FLAGGED with zero parseable flags — Category C", _logger=log)
            d1.heartbeat("scoring", "FAILED", detail="AUDIT_MALFORMED", increment_failure=True, _logger=log)
            return {**evaluated, "status": "AUDIT_MALFORMED", "confidence": None,
                    "color": "RED", "emoji": "🔴",
                    "note": "compliance FLAGGED with no parseable flags — Category C, no pick produced"}
        penalty = min(flag_count * _PER_FLAG, _MAX_PENALTY)
        compliance_category_a_penalty = penalty
        if flag_count > 0:
            # Calibration tracking (this session): _PER_FLAG (0.03) and
            # _MAX_PENALTY (0.15) are PROVISIONAL constants, same as the
            # ML signal bands — log the firing so calibration_status_
            # report() can eventually check whether penalized picks
            # actually hit at a lower rate, or whether this penalty is
            # over/under-sized. Fail-open, never affects scoring.
            record_provisional_rule_fire("Compliance_PerFlag_Penalty", eval_id,
                                          player=evaluated.get("player"))
        pre_penalty_confidence = confidence
        flag_names = [f.get("rule", "?") for f in flags] if flags else ["unspecified — manual review path"]
        confidence = _adj(
            f"Compliance {flag_count} Cat-A [{', '.join(flag_names)}] x {_PER_FLAG:.0%}",
            confidence, max(0.0, confidence - penalty))
        b_names = [f.get("rule", "?") for f in category_b_flags]
        b_note = f" | {len(category_b_flags)} Category-B (wording only, no cost) flag(s): [{', '.join(b_names)}]" if category_b_flags else ""
        log("Scoring", f"COMPLIANCE PENALTY — {flag_count} Category-A flag(s) "
            f"[{', '.join(flag_names)}] x {_PER_FLAG:.0%} = -{penalty:.0%} "
            f"(capped at -{_MAX_PENALTY:.0%}). {pre_penalty_confidence:.0%} -> {confidence:.0%}"
            f"{b_note} -- NO GREEN CEILING APPLIED (policy unconfirmed, see operator).")
        evaluated["_compliance_penalty_breakdown"] = (
            f"{flag_count} Category-A flag(s) [{', '.join(flag_names)}] "
            f"x {_PER_FLAG:.0%} = -{penalty:.0%}{b_note}"
        )

    # Cap 4B removed (this session): manual_review_cap is no longer set
    # anywhere in the pipeline. Both "compliance audit failed to parse/
    # run" and "evaluation truncated" now return None (no pick
    # generated) directly from the orchestration loop instead of
    # falling through to a capped score -- a capped number still
    # implies a judgment was made, and for these two cases none was.
    # This left the check below dead: nothing ever sets the flag it
    # reads. Removed rather than left as orphaned, unreachable logic.

    # Cap 5: Goblin conflict
    if evaluated.get("goblin_conflict"):
        confidence = _adj("Goblin-conflict cap 65%", confidence, min(confidence, 0.65))
        log("Scoring", f"GOBLIN CONFLICT cap — ceiling 65%")

    # ── PROP TYPE ENFORCEMENT ────────────────────────────────────
    # Enforces STANDARD/GOBLIN/RED DEMON rules at scoring level.
    ptype = evaluated.get("prop_type_classified", "standard")

    # GOBLIN 😈 — MORE only. If direction is LESS → flag conflict.
    if ptype == "goblin":
        if direction == "LESS":
            confidence = _adj("Goblin LESS-direction cap 55%", confidence, min(confidence, 0.55))
            log("Scoring", "GOBLIN prop — direction LESS conflicts with MORE-only rule → capped 55%")
            send_discord("alerts",
                f"⚠️ **GOBLIN DIRECTION CONFLICT** — {evaluated.get('player')} "
                f"{evaluated.get('line')} {stat_type}\n"
                f"System produced LESS on a MORE-only Goblin prop. Capped at 55%.\n"
                f"Review evaluation before placing."
            )
        else:
            log("Scoring", f"Goblin 😈 prop — MORE direction confirmed")

    # RED DEMON 🔴😈 — MORE only, higher payout, stronger justification required.
    elif ptype == "red_demon":
        if direction == "LESS":
            confidence = _adj("Red Demon LESS-direction cap 55%", confidence, min(confidence, 0.55))
            log("Scoring", "RED DEMON prop — direction LESS conflicts with MORE-only rule → capped 55%")
            send_discord("alerts",
                f"⚠️ **RED DEMON DIRECTION CONFLICT** — {evaluated.get('player')} "
                f"{evaluated.get('line')} {stat_type}\n"
                f"System produced LESS on a MORE-only Red Demon prop. Capped at 55%.\n"
                f"Review evaluation before placing."
            )
        else:
            # Red Demon requires stronger structural backing than Goblin
            # Apply -3% penalty unless path separation is strong (5-2+)
            competitive_reqs = evaluated.get("competitive_reqs", 0)
            if competitive_reqs < 4:
                # Cleanup (this session, approved): was
                # min(confidence, confidence - 0.03), which always
                # equals confidence - 0.03 — the min was decorative.
                confidence = _adj("Red Demon partial justification -3%",
                                  confidence, confidence - 0.03)
                log("Scoring", "Red Demon 🔴😈 — structural justification partial → -3% applied")
            else:
                log("Scoring", "Red Demon 🔴😈 — all 4 REQs met, strong structural backing confirmed")
    # ────────────────────────────────────────────────────────────

    # ── 5D SCORING ENFORCEMENT ───────────────────────────────────
    # These caps enforce Wimbledon Day 1 corrections at the scoring
    # layer — catches any cases where the LLM reasoned incorrectly.

    # Volatility Cap Rule — volatility cannot push direction to MORE.
    # Audit fix 6.1 (this session): the old comment said this keys off
    # "classification is VOLATILE" -- it does not, and never should:
    # v7.8 demoted classification to context-only. The implemented (and
    # correct) trigger is DOMINANCE LEVEL being UNSTABLE/UNKNOWN. The
    # dead `classification` fetch that the stale comment left behind is
    # removed; behavior unchanged.
    dominance_level = evaluated.get("dominance_level", "UNKNOWN")
    if (direction == "MORE" and
        dominance_level in ("UNSTABLE", "UNKNOWN") and
        stat_type in ("total_games", "games_won", "total_games_won", "total_sets")):
        competitive_reqs = evaluated.get("competitive_reqs", 0)
        if competitive_reqs < 3:
            confidence = _adj("5D volatility cap 67%", confidence, min(confidence, 0.67))
            log("Scoring", "5D Volatility cap — UNSTABLE dominance + MORE + <3 REQs → YELLOW max 67%")

    # Total Sets MORE Gate — enforce at scoring level
    if stat_type == "total_sets" and direction == "MORE":
        ptype_scored = evaluated.get("prop_type_classified", "standard")
        competitive_reqs = evaluated.get("competitive_reqs", 0)
        # Red Demon Total Sets MORE needs all 4 REQs
        if ptype_scored == "red_demon" and competitive_reqs < 4:
            confidence = _adj("Total Sets MORE Red Demon <4 REQs cap 65%",
                              confidence, min(confidence, 0.65))
            log("Scoring", "5D Red Demon Total Sets MORE — requires all 4 REQs → capped 65%")
        # Standard/Goblin Total Sets MORE — needs at least 3 REQs
        elif competitive_reqs < 3:
            confidence = _adj("Total Sets MORE <3 REQs cap 67%",
                              confidence, min(confidence, 0.67))
            log("Scoring", "5D Total Sets MORE — <3 proven-competitive REQs → YELLOW max 67%")
    # ────────────────────────────────────────────────────────────

    # Cap 6: Form override — recent surface title blocks blowout LESS reads
    opp_recent_title    = opp_data.get("recent_surface_title", False)
    player_recent_title = player_data.get("recent_surface_title", False)
    if direction == "LESS" and stat_type in ("total_games", "games_won", "total_games_won"):
        if opp_recent_title or player_recent_title:
            confidence = _adj("Recent-surface-title form cap 65%",
                              confidence, min(confidence, 0.65))
            log("Scoring", "Form override: recent surface title -> blowout LESS capped 65%")

    # Cap 7: Hold-present GREEN gate — no hold data = cannot reach GREEN
    # Binary hard stop per decision tree Hard Stop 4
    hold_dependent = stat_type in (
        "break_points_won", "total_games", "games_won", "total_games_won"
    )
    if hold_dependent:
        p_hold = player_data.get("serve_hold_pct", None)
        o_hold = opp_data.get("serve_hold_pct", None)
        if stat_type == "break_points_won":
            if o_hold is None:
                confidence = _adj("Missing opponent hold (BP) cap 62%",
                                  confidence, min(confidence, 0.62))
                log("Scoring", f"Hold gate: missing opponent hold (BP) -> capped 62%")
        else:
            if p_hold is None or o_hold is None:
                confidence = _adj(f"Missing hold data ({stat_type}) cap 68%",
                                  confidence, min(confidence, 0.68))
                log("Scoring", f"Hold gate: missing hold ({stat_type}) -> capped 68%")

    # ── STEP 3: DIRECTION-AWARE DOMINANCE CAPS ───────────────────────────────
    dominance_level  = evaluated.get("dominance_level", "UNKNOWN")
    # v7.9.7 fix (item 4): normalize before matching. Previously an exact
    # string match on "HARD"/"EDGE"/"SOFT" meant the model writing
    # "Strong EDGE" or "EDGE+" fell through to the UNSTABLE/UNKNOWN 62%
    # ceiling instead of the correct EDGE 69% ceiling — silently
    # under-grading the pick. Now strip/uppercase and match on
    # containment, checked in strength order (HARD before EDGE before
    # SOFT), so extra qualifier words don't break the match.
    dominance_level  = (dominance_level or "UNKNOWN").strip().upper()
    competitive_reqs = evaluated.get("competitive_reqs", 0)

    # Rebalance (this session, approved): the LESS dominance ceiling
    # used to apply to EVERY stat type -- meaning a DF or Aces LESS
    # pick could never reach GREEN without HARD *match-structure*
    # dominance, a concept that doesn't govern serve-profile props,
    # while the mirror-image DF/Aces MORE had no structural gate at
    # all. Serve-stat props (aces, double_faults) are now exempt from
    # this ceiling and instead pass through their own gate below,
    # applied in BOTH directions.
    _SERVE_STAT_PROPS = ("aces", "double_faults")
    if direction == "LESS" and stat_type not in _SERVE_STAT_PROPS:
        # LESS path: dominance determines ceiling
        # HARD (4-5/5) = GREEN eligible (no additional cap here)
        # EDGE (3/5) = YELLOW max 69%
        # SOFT (≤2/5) = YELLOW max 66%
        # UNSTABLE/UNKNOWN = YELLOW max 62%
        if "HARD" in dominance_level:
            pass  # GREEN eligible — caps already applied above
            log("Scoring", f"LESS path HARD dominance — GREEN eligible")
        elif "EDGE" in dominance_level:
            confidence = _adj("LESS-path EDGE dominance ceiling 69%",
                              confidence, min(confidence, 0.69))
            log("Scoring", f"LESS path EDGE dominance — ceiling 69%")
        elif "SOFT" in dominance_level:
            confidence = _adj("LESS-path SOFT dominance ceiling 66%",
                              confidence, min(confidence, 0.66))
            log("Scoring", f"LESS path SOFT dominance — ceiling 66%")
        else:
            confidence = _adj(f"LESS-path {dominance_level} dominance ceiling 62%",
                              confidence, min(confidence, 0.62))
            log("Scoring", f"LESS path {dominance_level} — ceiling 62%")

    elif direction == "MORE" and stat_type in ("total_games", "games_won", "total_games_won"):
        # MORE path (decision tree v7.9.4 proven-competitive framework):
        # All 4 requirements met = GREEN eligible
        # Any requirement missing = YELLOW max 69%
        # Requirements: (1) both confirmed extenders, (2) confirmed two-sided holding,
        #               (3) line beatable by demonstrated pattern, (4) not thin-data coin-flip
        if competitive_reqs >= 4:
            pass  # GREEN eligible — caps already applied above
            log("Scoring", f"MORE path — all 4 reqs met — eval confidence {eval_conf:.0%} applied")
        else:
            confidence = _adj(f"MORE path {competitive_reqs}/4 REQs ceiling 69%",
                              confidence, min(confidence, 0.69))
            log("Scoring", f"MORE path — {competitive_reqs}/4 reqs — YELLOW max 69%")

    # ── SERVE-STAT GATE (this session, approved — both directions) ──────────
    # Replaces the dominance ceiling for aces/double_faults props with a
    # gate built from what actually governs these props: the player's own
    # demonstrated serve numbers vs the line. Philosophy matches
    # ESS_FIELD_WEIGHTS (the primary average IS the prop) and the hold
    # gate (missing the governing datapoint = cannot reach GREEN).
    # The ±0.5 "coin-flip zone" threshold is NOT a new invented number --
    # it reuses the LPAB/PSAR gap band already locked June 14, 2026
    # (gap ≤0.5 = flip-eligible zone). PROVISIONAL like every other
    # uncalibrated constant here: revisit once calibration data exists.
    if stat_type in _SERVE_STAT_PROPS:
        _primary_key = "avg_aces_v8" if stat_type == "aces" else "avg_double_faults"
        _primary = player_data.get(_primary_key)
        try:
            _primary_val = float(_primary) if _primary is not None else None
        except (TypeError, ValueError):
            _primary_val = None
        try:
            _line_val = float(evaluated.get("line")) if evaluated.get("line") is not None else None
        except (TypeError, ValueError):
            _line_val = None
        if _primary_val is None:
            confidence = _adj(f"Serve-stat gate: {_primary_key} missing, cap 65%",
                              confidence, min(confidence, 0.65))
            log("Scoring", f"SERVE-STAT gate — {_primary_key} missing → "
                f"cannot reach GREEN, capped 65%")
        elif _line_val is not None and abs(_primary_val - _line_val) < 0.5:
            # Calibration tracking (this session): this IS LPAB_PSAR
            # firing -- the locked ±0.5 gap band from June 14, 2026.
            # Fired once here, at the real mechanical trigger, regardless
            # of which branch below (MORE+4reqs exemption vs. the
            # deduction) it resolves to -- both are the same rule having
            # fired, just with a different downstream outcome.
            # Deliberately NOT also tracked via text citation in
            # evaluation_agent(), even though the decision tree lists
            # "LPAB_PSAR" as a citable name -- this Python gate is the
            # authoritative, prompt-adherence-immune signal; double-
            # tracking would inflate fired_count with the same real
            # event counted twice.
            record_provisional_rule_fire("LPAB_PSAR_v1_0", eval_id, player=evaluated.get("player"))
            # Boundary fix (this session, root-caused live): was `<= 0.5`,
            # an INCLUSIVE boundary. Prop lines sit at X.5 while per-match
            # averages cluster near integers/tenths, so a gap of EXACTLY
            # 0.5 is one of the most common values this comparison can
            # produce -- the inclusive boundary swept that routine offset
            # into the coin-flip band. LPAB/PSAR's locked band is
            # "gap <=0.5 = flip-ELIGIBLE" -- an eligibility zone for a
            # directional flip CHECK, not a mandate that a 0.5 buffer
            # equals a coin flip. Strict `<` keeps the true coin-flip zone
            # and stops taxing the standard line offset.
            if direction == "MORE" and competitive_reqs >= 4:
                # Double-charge fix (confirmed live, Djokovic DF 2.5,
                # 2026-07-09: eval 72% GREEN, 4/4 proven-competitive REQs,
                # 75%-weighted path analysis -> this cap silently shaved it
                # to 69% YELLOW and the override note showed no cause).
                # REQ3 ("line beatable by demonstrated pattern") IS the
                # buffer question -- when the eval has affirmatively proven
                # all 4 REQs, the closeness of avg-to-line has already been
                # priced into its ceiling and deductions. Capping again
                # here charges the same uncertainty twice. The gate keeps
                # its error-catching role for every case where the proven
                # path was NOT established (MORE with <4 REQs, and all
                # LESS serve-stat picks).
                log("Scoring", f"SERVE-STAT gate — line {_line_val} within ±0.5 of "
                    f"avg {_primary_val}, but MORE with 4/4 proven-competitive REQs — "
                    f"closeness already priced by eval, no second charge. GREEN eligible")
            else:
                confidence = _adj(
                    f"Serve-stat coin-flip band (avg {_primary_val} vs line {_line_val})",
                    confidence, min(confidence, 0.69))
                log("Scoring", f"SERVE-STAT gate — line {_line_val} within ±0.5 of "
                    f"avg {_primary_val} (coin-flip band) → YELLOW max 69%")
        else:
            log("Scoring", f"SERVE-STAT gate — {_primary_key}={_primary_val} vs "
                f"line {_line_val}: real buffer confirmed, GREEN eligible")
    # ────────────────────────────────────────────────────────────

    # ── BP RULE v7.3 SCORING ENFORCEMENT (this session, approved) ───────────
    # BP Rule v7.3's numeric limits lived only in the master prompt --
    # instructed to the model, enforced nowhere. Same designed-vs-
    # implemented gap class as every major finding this project. These
    # numbers are NOT new: they are the locked BP Rule v7.3 constants
    # (surface ceilings, sub-2.5 MORE no-play, opponent hold-tier
    # suppression), now enforced at scoring level so a model that
    # reasons past them gets caught the way the 5D caps catch
    # volatility misuse. The underdog-LESS-below-3.5 rule is NOT
    # enforced here: is_favorite is not available in the prop dict --
    # noted, not silently skipped.
    if stat_type == "break_points_won":
        def _norm_pct(h):
            try:
                h = float(h)
            except (TypeError, ValueError):
                return None
            return h / 100.0 if h > 1 else h
        _o_hold_n = _norm_pct(opp_data.get("serve_hold_pct"))
        try:
            _bp_line = float(evaluated.get("line")) if evaluated.get("line") is not None else None
        except (TypeError, ValueError):
            _bp_line = None
        _surface_bp = str(evaluated.get("surface") or context.get("surface") or "").lower()
        if direction == "MORE":
            # BP MORE below line 2.5 = NO PLAY (locked rule, hard stop)
            if _bp_line is not None and _bp_line < 2.5:
                log("Scoring", f"BP RULE — MORE on line {_bp_line} (<2.5) is NO PLAY "
                    f"per BP Rule v7.3. No pick produced.")
                send_discord("alerts",
                    f"🛑 **BP RULE BLOCK — NO PICK** — {evaluated.get('player')} "
                    f"{_bp_line} break_points_won ⬆ MORE\n"
                    f"BP MORE below line 2.5 = NO PLAY (BP Rule v7.3, locked). "
                    f"The evaluation reasoned past a hard rule — blocked at "
                    f"scoring, not penalized."
                )
                # Using the BP_RULE_BLOCKED code (not NO_PLAY) per the approved
                # schema — these are two distinct enum values. Flagging for your
                # awareness: this is a hard rule stop, arguably closer in spirit
                # to "found no edge" than to a system failure. Left as its own
                # code rather than silently reclassifying; your call if you'd
                # rather these route to NO_PLAY in Mission Control reporting.
                d1.terminate(eval_id, "BP_RULE_BLOCKED",
                             "BP MORE below line 2.5 — BP Rule v7.3", _logger=log)
                # Heartbeat as success, not failure: the agent correctly
                # applied a locked rule and blocked a bad pick — same
                # reasoning as the BP_RULE_BLOCKED-vs-NO_PLAY flag above.
                d1.heartbeat("scoring", "IDLE", detail="BP_RULE_BLOCKED", increment_success=True, _logger=log)
                return {**evaluated, "status": "BP_RULE_BLOCKED", "confidence": None,
                        "color": "RED", "emoji": "🔴",
                        "note": "BP MORE below 2.5 — NO PLAY per BP Rule v7.3"}
            # Surface ceilings for BP MORE (locked): grass 72 / clay 78 / hard 75
            if "grass" in _surface_bp:
                confidence = _adj("BP MORE grass ceiling 72%", confidence, min(confidence, 0.72))
                log("Scoring", "BP RULE — grass BP MORE ceiling 72%")
            elif "clay" in _surface_bp:
                confidence = _adj("BP MORE clay ceiling 78%", confidence, min(confidence, 0.78))
                log("Scoring", "BP RULE — clay BP MORE ceiling 78%")
            elif "hard" in _surface_bp:
                confidence = _adj("BP MORE hard ceiling 75%", confidence, min(confidence, 0.75))
                log("Scoring", "BP RULE — hard BP MORE ceiling 75%")
            # Opponent hold-tier suppression (locked): a server this
            # hard to break suppresses BP generation regardless of the
            # backed player's return quality.
            if _o_hold_n is not None:
                if _o_hold_n >= 0.85:
                    confidence = _adj(f"BP opp hold {_o_hold_n:.0%} full suppression cap 55%",
                                      confidence, min(confidence, 0.55))
                    log("Scoring", f"BP RULE — opp hold {_o_hold_n:.0%} ≥85% → "
                        f"FULL SUPPRESSION, capped 55%")
                elif _o_hold_n >= 0.80:
                    confidence = _adj(f"BP opp hold {_o_hold_n:.0%} suppression -12%",
                                      confidence, max(0.0, confidence - 0.12))
                    log("Scoring", f"BP RULE — opp hold {_o_hold_n:.0%} (80-84%) → -12%")
                elif _o_hold_n >= 0.75:
                    confidence = _adj(f"BP opp hold {_o_hold_n:.0%} suppression -10%",
                                      confidence, max(0.0, confidence - 0.10))
                    log("Scoring", f"BP RULE — opp hold {_o_hold_n:.0%} (75-79%) → -10%")
    # ────────────────────────────────────────────────────────────

    # ── STEP 3B: FLOOR ELIGIBILITY (item 7 / master prompt Correction 5) ─────
    # Correction 5 says: never output a supported direction below 65%.
    # That floor was never actually enforced in code — only instructed to
    # the model in the master prompt — and "supported" was never defined,
    # so a genuinely thin case could still get rescued up to 65% and look
    # more confident than the evidence justified.
    # v7.9.7 fix: the floor now only applies when the case qualifies as
    # "reasonably supported" using conditions already computed above in
    # this same function — thin_data (one signal feeding the consolidated
    # data-quality ceiling above) and LESS-path UNSTABLE/UNKNOWN
    # dominance are exactly the signals this system already uses elsewhere
    # to mean "not enough here to trust." If either is true, skip the
    # floor and let the true computed confidence fall below 65% into RED —
    # an honest number instead of a floor-rescued one.
    # Bug fix (this session, confirmed live 3x independently -- Sinner
    # 12:31 AM log, Putintseva DF prop, Arseneault/Volynets prop): this
    # floor ran AFTER Cap 4's compliance penalty with no awareness that
    # the confidence value it was "rescuing" had just been deliberately
    # reduced for a REAL, confirmed reasoning failure (a genuine
    # Category-A flag), not merely a naturally weak signal. Confirmed
    # live for the Arseneault/Volynets prop: eval_confidence 65% -> a
    # real -3% Rule_TruthLock penalty computed (0.65 -> 0.62) -> this
    # floor silently pushed it right back to 65%, erasing the penalty
    # completely, with no override note logged at all. The floor's
    # entire premise ("don't under-report a well-supported pick") does
    # not apply to a pick that was JUST marked as having a real
    # reasoning failure -- rescuing it back to the same number as a
    # zero-flag pick defeats the whole point of the Category-A penalty
    # existing. A real Category-A deduction (not a zero-cost Category-B
    # flag, and not the manual-review path -- both already produce
    # compliance_category_a_penalty == 0 or a real positive value
    # respectively, see Cap 4 above) now exempts a pick from this floor.
    less_path_unstable = (direction == "LESS" and
                          ("UNSTABLE" in dominance_level or "UNKNOWN" in dominance_level))
    floor_eligible = (not thin_data
                       and not less_path_unstable
                       and compliance_category_a_penalty == 0)
    if floor_eligible and confidence < 0.65:
        # Telemetry (this session, approved audit follow-up): the floor
        # is the ONLY mechanism in this system that moves a score UP
        # from the model's own conclusion (the Masarova 60%->65% class).
        # Every downward move is itemized; the upward one must be too.
        # Recording it lets the calibration loop bucket floored picks
        # separately and answer empirically whether 60-64% evals lifted
        # to 65% actually hit at 65% rates -- if they hit at their
        # original rates instead, the floor is systematically inflating
        # marginal picks and should be narrowed or removed WITH data.
        evaluated["floor_applied"] = True
        evaluated["floor_pre_confidence"] = round(confidence, 3)
        log("Scoring", f"Correction 5 floor applied — {confidence:.0%} -> 65% (reasonably supported)")
        confidence = _adj("Correction 5 floor (upward rescue to 65%)", confidence, 0.65)
    elif confidence < 0.65:
        log("Scoring", f"Correction 5 floor WITHHELD — {confidence:.0%} left as-is "
                        f"(thin_data={thin_data}, less_path_unstable={less_path_unstable}, "
                        f"compliance_category_a_penalty={compliance_category_a_penalty:.0%})")

    # ── STEP 4: CLAMP AND COLOR ───────────────────────────────────────────────
    confidence = max(0.25, min(0.95, round(confidence, 3)))
    # v7.9.7 fix (item 6): AMBER removed. The master prompt only ever
    # defines GREEN (70%+) / YELLOW (60-69%) / RED (<60%) — AMBER existed
    # only here in scoring_agent with no counterpart in the model's own
    # rules, so a 67-69% pick's reasoning text said YELLOW while the pick
    # card silently said AMBER. 67-69% now falls into YELLOW, matching
    # the master prompt exactly. Nothing about the GREEN threshold or
    # any confidence math above this line changes.
    # Bands: 70-80=GREEN | 60-69=YELLOW | <60=RED
    # Bug fix (this session): the YELLOW threshold here was 0.55 while
    # the master prompt -- and the band comment above -- define YELLOW
    # as 60-69% and RED as below 60%. A 55-59% pick posted 🟡 when the
    # documented bands say 🔴. Code now matches the documented bands.
    if confidence >= 0.70:
        color = "GREEN"
    elif confidence >= 0.60:
        color = "YELLOW"
    else:
        color = "RED"
    emoji = "🟢" if color == "GREEN" else ("🟡" if color == "YELLOW" else "🔴")

    log("Scoring", f"{evaluated.get('player')}: {emoji} {confidence:.0%} {direction}")

    # ── GATE 11-B SCORING CORRECTION ────────────────────────────
    # Auto-corrects bo5 LESS picks without HARD dominance.
    # Fires Discord alert and attaches correction_line for output.
    scored_result = {**evaluated, "confidence": confidence, "color": color, "emoji": emoji}
    scored_result = gate_11b_scoring_correction(evaluated, scored_result)

    # Recalculate color/emoji if correction changed confidence
    if scored_result.get("format_corrected"):
        confidence = scored_result["confidence"]
        if confidence >= 0.70:
            color = "GREEN"
        elif confidence >= 0.60:
            color = "YELLOW"
        else:
            color = "RED"
        emoji = "🟢" if color == "GREEN" else ("🟡" if color == "YELLOW" else "🔴")
        scored_result["color"] = color
        scored_result["emoji"] = emoji
    # ────────────────────────────────────────────────────────────

    # v7.9.7 (item 5): if scoring_agent's caps/floor moved the final
    # confidence away from what the model itself concluded, say so
    # explicitly rather than letting the pick card and the model's own
    # reasoning text silently disagree with no explanation shown.
    final_confidence = scored_result.get("confidence")
    scored_result["score_adjustments"] = list(_score_adjustments)
    if final_confidence is not None and abs(final_confidence - _eval_baseline) >= 0.005:
        # Confidence conservation (this session, root-caused live on the
        # Djokovic DF 2.5 pick 2026-07-09): the old note printed ONLY the
        # compliance breakdown -- any cap-driven move showed a bare
        # "eval X% -> scored Y%" with no cause, indistinguishable from a
        # bug. The note is now built from the full itemized ledger. A
        # moved score with an EMPTY ledger means a confidence change
        # this function cannot account for -- that is a HIGH-SEVERITY
        # defect by definition and fires an alert instead of posting
        # silently.
        if scored_result.get("format_corrected"):
            _score_adjustments.append(
                f"Gate 11-B format correction \u2192 {final_confidence:.0%}")
            scored_result["score_adjustments"] = list(_score_adjustments)
        if _score_adjustments:
            override_note = (f"⚠️ Scoring override applied: eval {_eval_baseline:.0%} "
                              f"→ scored {final_confidence:.0%} "
                              f"({' | '.join(_score_adjustments)})")
        else:
            override_note = (f"🛑 UNEXPLAINED confidence change: eval {_eval_baseline:.0%} "
                              f"→ scored {final_confidence:.0%} with an EMPTY adjustment "
                              f"ledger — HIGH-SEVERITY defect, investigate before trusting")
            send_discord("alerts",
                f"🛑 **UNEXPLAINED CONFIDENCE CHANGE** — {evaluated.get('player')} "
                f"{evaluated.get('line')} {stat_type}\n"
                f"eval {_eval_baseline:.0%} → scored {final_confidence:.0%} but the "
                f"itemized adjustment ledger is EMPTY. Every scoring move must be "
                f"accounted for — treat as HIGH-SEVERITY defect."
            )
        scored_result["scoring_override_note"] = override_note
        log("Scoring", override_note)
    else:
        scored_result["scoring_override_note"] = None

    # Feature Echo capture (approved, this session): identical keyword
    # groups Mission Control previously re-derived on every read from
    # reasoning text -- now computed once here, against the ledger's
    # own cause strings (more reliable than free-text reasoning), and
    # persisted. "Charged 2+ times" = possible confidence double-count.
    _echo_roots = {"hold": ["hold"], "thin": ["thin"], "gap": ["gap", "class"],
                   "fatigue": ["fatigue", "rule 30", "rule30", "turnaround"],
                   "format": ["format", "bo5", "gate 11-b"],
                   "dominance": ["dominance", "edge", "soft"]}
    _echoes = [root for root, words in _echo_roots.items()
               if len([a for a in _score_adjustments if any(w in a.lower() for w in words)]) >= 2]

    d1.transition(eval_id, "SCORING_COMPLETE",
                   final_confidence=final_confidence,
                   confidence_ledger=list(_score_adjustments),
                   final_grade=scored_result.get("color"),
                   feature_echo_flags=json.dumps(_echoes),
                   # compliance_cost capture (approved, this session,
                   # corrected from the original audit's guess of
                   # COMPLIANCE_COMPLETE): compliance_category_a_penalty
                   # is only actually computed here, inside scoring_agent,
                   # not at the earlier COMPLIANCE_COMPLETE transition --
                   # that fires before this function even runs.
                   compliance_cost=compliance_category_a_penalty,
                   # empty ledger + moved score = the HIGH-SEVERITY defect
                   # signal above; recorded structurally so future queries
                   # don't have to re-parse override_note text.
                   ledger_empty_flag=int(
                       final_confidence is not None
                       and abs(final_confidence - _eval_baseline) >= 0.005
                       and not _score_adjustments
                   ),
                   _logger=log)
    # APEX-GOBLIN-FLOOR-2026-09-24: goblin is MORE-only and needs 72.1% to break
    # even at goblin payout odds. This runs LAST, after the Correction-5 floor and
    # Gate-11B format correction above (both can still move confidence), so it
    # checks the true final number rather than a value something downstream could
    # still push back above the line unnoticed.
    GOBLIN_BREAKEVEN = 0.721   # APEX-GOBLIN-FLOOR-FIX-2026-09-24: defined inline, was undefined at runtime
    if (scored_result.get("prop_type_classified") == "goblin"
            and scored_result.get("confidence") is not None
            and scored_result.get("confidence") < GOBLIN_BREAKEVEN
            and scored_result.get("status") not in ("BP_RULE_BLOCKED", "AUDIT_MALFORMED", "NO_PLAY")):
        _gb_conf = scored_result["confidence"]
        log("Scoring", f"GOBLIN BREAK-EVEN BLOCK — {_gb_conf:.1%} is below the "
            f"{GOBLIN_BREAKEVEN:.1%} goblin needs to break even. No pick produced.")
        send_discord("alerts",
            f"🛑 **GOBLIN BELOW BREAK-EVEN — NO PICK** — {evaluated.get('player')} "
            f"{evaluated.get('line')} {stat_type}\n"
            f"Final confidence {_gb_conf:.1%} is below the {GOBLIN_BREAKEVEN:.1%} "
            f"goblin payout requires to break even. MORE direction and GREEN/YELLOW "
            f"grade do not override this -- blocked at scoring, not posted."
        )
        d1.terminate(eval_id, "GOBLIN_BELOW_BREAKEVEN",
                     f"goblin confidence {_gb_conf:.1%} below {GOBLIN_BREAKEVEN:.1%} break-even",
                     _logger=log)
        d1.heartbeat("scoring", "IDLE", detail="GOBLIN_BELOW_BREAKEVEN", increment_success=True, _logger=log)
        return {**scored_result, "status": "GOBLIN_BELOW_BREAKEVEN", "confidence": None,
                "color": "RED", "emoji": "🔴",
                "note": f"goblin {_gb_conf:.1%} below {GOBLIN_BREAKEVEN:.1%} break-even -- no pick produced"}

    d1.heartbeat("scoring", "IDLE", increment_success=True, _logger=log)

    return scored_result


# ================================================================
# TGW COUNTER (deterministic — fixes 7-6 6-4 = 13 not 10 miscount)
# ================================================================

def count_tgw(scoreline, player_is_winner):
    """
    scoreline: list of (winner_games, loser_games) tuples per set.
    player_is_winner: True if the BACKED player won the match.
    Returns the backed player's TOTAL games won across ALL sets.
    Example: count_tgw([(7,6),(6,4)], True)  -> 7+6 = 13 (winner)
             count_tgw([(6,4),(6,4)], False) -> 4+4 = 8  (loser/underdog)
    """
    total = 0
    for winner_games, loser_games in scoreline:
        total += winner_games if player_is_winner else loser_games
    return total


def tgw_distribution(scorelines, player_is_winner, line):
    """
    Returns OVER/UNDER counts so the distribution decides direction.
    Never count only blowout paths for underdogs or only tight-win
    paths for favorites — competitive matches push BOTH players' TGW up.
    """
    overs, unders = [], []
    for sl in scorelines:
        g = count_tgw(sl, player_is_winner)
        (overs if g > line else unders).append((sl, g))
    return {
        "over_count":  len(overs),
        "under_count": len(unders),
        "overs":       overs,
        "unders":      unders,
        "lean": "MORE" if len(overs) > len(unders)
                else ("LESS" if len(unders) > len(overs) else "COINFLIP"),
    }


# ================================================================
# RULE 28
# ================================================================

def rule_28_agent(ranked_props, dates, dedup=True):
    """dedup=True (default, unchanged): one pick per player + one pick
    per match, exactly as before -- this is what the scheduled 5PM
    pipeline uses to build an actual bettable slip, and that behavior
    is untouched.
    dedup=False (APEX-FETCH-PUBLISHALL-2026-09-29): every ranked prop
    passes through untouched -- no player/match exclusion. Added for
    the on-demand /fetch command, whose job is full-board visibility
    ("publish every qualifying evaluated pick"), not slip construction.
    Rule 31's correlated-LESS disclosure below still runs either way."""
    selected      = []
    seen_players  = set()
    seen_matches  = set()
    excluded      = []

    if not dedup:
        selected = list(ranked_props)
    else:
        for prop in ranked_props:
            player    = prop.get("player", "").lower()
            opponent  = prop.get("opponent", "").lower()
            match_key = tuple(sorted([player, opponent]))

            if player in seen_players:
                reason = f"{prop.get('player')} already on slip"
                excluded.append({"prop": prop, "reason": reason})
                # Dataset A (Rule 28 finding, approved): a fully-scored pick —
                # possibly GREEN — is dying here today with zero record. This
                # is the exact selection-bias gap Dataset A exists to close.
                d1.terminate(prop.get("eval_id"), "RULE28_DEDUP", reason, _logger=log)
                continue
            if match_key in seen_matches:
                reason = f"Match {prop.get('player')} vs {prop.get('opponent')} already on slip"
                excluded.append({"prop": prop, "reason": reason})
                d1.terminate(prop.get("eval_id"), "RULE28_DEDUP", reason, _logger=log)
                continue

            selected.append(prop)
            seen_players.add(player)
            seen_matches.add(match_key)

    if excluded:
        lines = "\n".join([
            f"• {e['prop'].get('player')} {e['prop'].get('line')} — {e['reason']}"
            for e in excluded
        ])
        send_discord("system",
            f"📋 **Rule 28 — {len(excluded)} excluded**\n{lines}"
        )

    # Rule 31 — Common Failure Mode disclosure (this session, closes a
    # real gap: Rule 31 has existed in the master prompt this whole
    # time with ZERO enforcement anywhere -- not in code, not in the
    # compliance checklist, confirmed by searching both). Rule 31's own
    # documented purpose is disclosure only ("if courts play slow, X
    # picks bust together") -- structurally this can only be checked
    # HERE, at the batch level, once every prop for the day is scored
    # and deduped. A per-evaluation compliance check only ever sees one
    # prop in isolation -- it is the wrong place for this rule by
    # construction, not an oversight to fix there. Groups today's LESS
    # picks by (tournament, surface); 2+ in the same group share a
    # plausible common cause (slow conditions, weather, format) that
    # could bust them together. Disclosure only, per Rule 31's own
    # spec -- this never touches confidence or excludes a pick, it only
    # makes the shared risk visible before the operator plays the slip.
    _less_by_context = {}
    for p in selected:
        if (p.get("direction") or "").upper() == "LESS":
            key = (p.get("tournament") or "?", p.get("surface") or "?")
            _less_by_context.setdefault(key, []).append(p)
    for (tourn, surf), group in _less_by_context.items():
        if len(group) >= 2:
            names = ", ".join(f"{g.get('player')} {g.get('line')}" for g in group)
            send_discord("system",
                f"⚠️ **Rule 31 — correlated LESS exposure** ({tourn}, {surf})\n"
                f"{len(group)} LESS picks share this tournament/surface: {names}\n"
                f"A single shared cause (slow conditions, weather, format) could "
                f"bust these together — disclosure only, no confidence change applied."
            )
            for g in group:
                g["rule31_correlated_group"] = f"{tourn} / {surf} ({len(group)} picks)"

    return selected

# ================================================================
# RESULT CLASSIFICATION (non-completion protection)
# ================================================================

def classify_result(result_text):
    text_lower = result_text.lower()
    for kw in NON_COMPLETION_KEYWORDS:
        if kw in text_lower:
            return "NON_COMPLETION", kw
    if SCORE_PATTERN.search(result_text):
        return "COMPLETED", None
    return "UNKNOWN", None

# ================================================================
# OUTPUT AGENT
# ================================================================

def format_pick_card(pick, index):
    # APEX-ENGINE-PRIMARY-2026-08-23: engine picks carry their own rendered
    # card (tier badge, projection, probability, edge, fragility, SHAs).
    _apex_card = pick.get("_apex_engine_card")
    if _apex_card:
        return f"**{index}.** {_apex_card}"
    player       = pick.get("player", "")
    line         = pick.get("line", "")
    stat         = pick.get("stat_type", "")
    direction    = pick.get("direction", "")
    conf         = pick.get("confidence", 0)
    if conf is None:
        conf = 0.0  # defensive: no-pick dicts are filtered upstream, but a
                    # None here must never crash the whole batch post again
    emoji        = pick.get("emoji", "🟡")
    match_type   = pick.get("match_type", "")
    one_sentence = pick.get("one_sentence", "")
    rules_ok     = pick.get("rules_confirmed", [])
    goblin       = pick.get("prop_type_classified") == "goblin"
    red_demon    = pick.get("prop_type_classified") == "red_demon"
    goblin_conflict = pick.get("goblin_conflict", False)
    comp_pen     = pick.get("compliance_penalty", False)
    format_flag  = pick.get("format_contamination", False)

    rd = pick.get("research_data", {})
    p  = rd.get("player", {})
    o  = rd.get("opponent", {})

    dir_arrow = "⬆" if direction == "MORE" else "⬇"

    # Prop type tag for output
    if red_demon:
        prop_tag = " 🔴😈 RED DEMON"
    elif goblin:
        prop_tag = " 😈 GOBLIN"
    else:
        prop_tag = ""

    lines = [
        f"**{index}. {player}** vs {pick.get('opponent','')}{prop_tag}",
        f"`{line} {stat} — {dir_arrow} {direction}`",
        f"{emoji} **{conf:.0%}** | {match_type}",
        f"#{p.get('ranking') or p.get('ranking_v8') or '?'} | "
        f"{p.get('surface_record') or '?'} | "
        f"Last 3: {p.get('recent_form_last3') or '?'}",
        f"Opp #{o.get('ranking') or o.get('ranking_v8') or '?'} | "
        f"{o.get('surface_record') or '?'} | "
        f"Last 3: {o.get('recent_form_last3') or '?'}",
    ]
    if rules_ok:
        lines.append(f"✅ {' | '.join(rules_ok[:4])}")
    if one_sentence:
        lines.append(f"*{one_sentence[:180]}*")
    if comp_pen:
        # Stale-string fix (this session, root-caused live 2026-07-09):
        # this was a hardcoded "confidence -5%" left over from the old
        # flat-penalty era -- it printed "-5%" on every flagged pick
        # regardless of the actual charge (a real -6% two-flag pick and
        # a real -0% B-only pick both displayed "-5%"). The card now
        # shows the actual itemized note produced by scoring_agent's
        # confidence-conservation ledger; if for any reason no note
        # exists, it says so honestly instead of inventing a number.
        _note = pick.get("scoring_override_note")
        if _note:
            lines.append(_note)
        else:
            _bd = pick.get("_compliance_penalty_breakdown")
            lines.append(f"⚠️ Compliance flag — {_bd}" if _bd
                         else "⚠️ Compliance flag (wording-only / zero-cost — see log)")
    if format_flag:
        lines.append("⚠️ Format contamination detected — capped YELLOW")
    if goblin_conflict:
        # 2026-08-04, user-confirmed: real disagreement now visible ON
        # the posted pick itself, not just in a separate #alerts channel.
        # v7.7 stays intact -- this still posts MORE/LESS (the locked
        # direction), it's just no longer silent about the mismatch.
        _g_dir  = pick.get("genuine_direction_pre_lock")
        _g_conf = pick.get("genuine_confidence_pre_lock")
        if _g_dir:
            _g_conf_str = f"{_g_conf:.0%}" if isinstance(_g_conf, (int, float)) else "--"
            lines.append(
                f"⚠️ Model disagrees with the locked direction — real read "
                f"was {_g_dir} at {_g_conf_str} (posted as locked {direction} "
                f"per Goblin MORE-only rule)"
            )
        else:
            lines.append("⚠️ Model disagrees with the locked direction (genuine read unavailable)")
    return "\n".join(lines)

_BOARD_SUMMARY_NAMES = {"top3": "🥇 Top 3", "elite6": "🏆 Elite 6", "top10": "🔟 Top 10",
                        "bestgoblin": "🟢 Best Goblin", "bestdemon": "🔴 Best Demon",
                        "beststandard": "⚪ Best Standard"}

def _published_board_summary():
    """APEX-5PMGENERAL-2026-10-16 (owner, "Go" 2026-10-16): what the
    published boards hold right now, read from the published snapshot (no
    fetch). Returns {"counts": [(name, n)], "top": text or None} or None."""
    try:
        import apex_edge_tracker as _aet_sum
        with open(_aet_sum.PUBLISHED_BOARD_SNAPSHOT) as _fh:
            _snap_boards = (json.load(_fh).get("boards") or {})
        counts = []
        for _k, _nm in _BOARD_SUMMARY_NAMES.items():
            _ents = ((_snap_boards.get(_k) or {}).get("entries") or [])[:_aet_sum._PUBLISHED_FLAG_LIMIT.get(_k, 6)]
            counts.append((_nm, len(_ents)))
        top = None
        _top = (((_snap_boards.get("top3") or {}).get("entries") or [{}])[0]).get("p")
        if _top:
            _who = _top.get("opponent") if _top.get("player") == "match" else _top.get("player")
            top = (f"{_who} {_top.get('direction')} {_top.get('line')} "
                   f"{str(_top.get('category')).replace('_', ' ')} ({float(_top.get('prob') or 0):.0%})")
        return {"counts": counts, "top": top}
    except Exception as _e:
        log("BoardSummary", f"board summary unavailable: {type(_e).__name__}: {_e}")
        return None

def general_5pm_card(dates, summary, elapsed_min=None):
    """APEX-5PMGENERAL-2026-10-16: the short #general card the 5 PM run posts."""
    L = [f"🎾 **5 PM BOARDS READY — {dates['display_day']}**"]
    if summary:
        if summary["top"]:
            L.append(f"⭐ Top pick: **{summary['top']}**")
        L.append("📋 " + " · ".join(f"{n} **{c}**" for n, c in summary["counts"]))
    else:
        L.append("⚠️ Board summary unavailable — check #system")
    L.append("👉 Full cards: /top3 /elite6 /bestgoblin /beststandard · slips in #slips · picks in #picks"
             + (f" · ⏱ {elapsed_min:.1f} min" if elapsed_min is not None else ""))
    return "\n".join(L)

def output_agent(selected, gate_rejected, dates, mode="live", board_pool=None, board_summary=None):
    green  = [p for p in selected if p.get("color") == "GREEN"]
    yellow = [p for p in selected if p.get("color") == "YELLOW"]

    header = (f"🧪 **TEST RUN — NOT FOR LIVE USE** — {dates['ct_time']}"
              if mode == "test"
              else f"🎾 **PICKS FOR {dates['display_day'].upper()}**")

    lines = ["═"*38, header, "═"*38, ""]
    idx   = 1

    if green:
        lines.append("🟢 **HIGH CONFIDENCE (70%+)**\n")
        for p in green:
            lines.append(format_pick_card(p, idx)); lines.append(""); idx += 1

    if yellow:
        # Category 9 Finding 5 (approved): header said 55-69% -- a band
        # that has not existed since the YELLOW floor moved to 60%.
        # Same display-lie class as the old hardcoded "-5%" string.
        lines.append("🟡 **MEDIUM CONFIDENCE (60-69%) — Flex only**\n")
        for p in yellow:
            lines.append(format_pick_card(p, idx)); lines.append(""); idx += 1

    if not green and not yellow:
        # APEX-5PMGENERAL-2026-10-16: the legacy slip being empty is not "no
        # picks" -- say what the published boards hold instead.
        if board_summary:
            lines.append("📋 **PUBLISHED BOARDS:** " + " · ".join(f"{n} {c}" for n, c in board_summary["counts"]))
            if board_summary["top"]:
                lines.append(f"⭐ Top pick: {board_summary['top']}")
            lines.append("_(Legacy slip below 60% — use the boards above / #slips.)_")
        else:
            lines.append("No qualifying picks found for this date.")

    # APEX-BB-001: Best Bets below the normal picks. Never blocks the post.
    if selected or board_pool:
        try:
            import apex_edge_tracker as _aet_bb
            _bb = _aet_bb.best_bets_block([
                {"player": p.get("player", ""), "line": p.get("line", ""),
                 "direction": p.get("direction", ""),
                 "opponent": p.get("opponent", ""),
                 "category": _APEX_STAT_MAP.get(str(p.get("stat_type", "")).strip().lower().replace(" ", "_"), ""),
                 "prob": float(p.get("confidence") or 0),
                 "prop_type": p.get("prop_type_classified", "standard"),
                 "bo5": str(p.get("apex_best_of") or "") == "5"}
                for p in (board_pool if board_pool else selected)])   # APEX-AUTOFULLBOARD-2026-10-08
            if _bb:
                lines.append(_bb); lines.append("")
        except Exception as _e:
            log("ApexBB", f"best bets skipped (non-fatal): {_e}")

    lines += [
        "─"*38,
        f"Total: {len(selected)} picks (🟢{len(green)} 🟡{len(yellow)})",
        f"Gate 11 rejected: {len(gate_rejected)}",
        "✅ Rule 1: Both players — real data",
        "✅ Gate 11: All matches verified upcoming",
        "✅ Rule 28: One player + one match per slip",
        "─"*38,
    ]

    # APEX-PICKSCLEAN-2026-10-24: the legacy picks post goes to #system (POSTED marking unchanged)
    channel = "system"
    if mode != "discord":
        _post_ok = send_discord(channel, "\n".join(lines))
        # Dataset A: only fire POSTED for a real live/discord post, not a
        # test-mode dry run (mode == "test" still hits this branch today,
        # so gate explicitly rather than assume).
        # Bug fix (this session): previously marked every pick POSTED=1
        # unconditionally, regardless of whether send_discord actually
        # succeeded -- it had no way to know, since send_discord returned
        # nothing. Now gated on the real result: a failed send leaves the
        # pick at its prior lifecycle_state (still genuinely
        # SCORING_COMPLETE -- it was scored but never actually reached
        # Discord) with posted=0 explicitly recorded, and logs loudly
        # rather than silently recording a post that never happened.
        if mode != "test":
            if selected:
                d1.heartbeat("posting", "WORKING", detail=f"posting {len(selected)} picks", _logger=log)
            if _post_ok:
                for _p in selected:
                    d1.transition(_p.get("eval_id"), "POSTED", posted=1, _logger=log)
                    d1.heartbeat("posting", "WORKING", increment_success=True, _logger=log)
            else:
                for _p in selected:
                    d1.transition(_p.get("eval_id"), "SCORING_COMPLETE",
                                   detail="Discord post failed -- pick never reached Discord",
                                   posted=0, _logger=log)
                    d1.heartbeat("posting", "WORKING", increment_failure=True, _logger=log)
                log("Discord", f"⚠️ Batch post FAILED — {len(selected)} scored pick(s) never "
                                f"reached Discord. Dataset A correctly shows posted=0, not POSTED.")
            if selected:
                d1.heartbeat("posting", "IDLE", _logger=log)
        # NOTE — slip linkage intentionally NOT wired here. The codebase's
        # real slip_id concept lives in parse_slip_entry (line ~7868,
        # f"{dates['learn_on_date']}_{int(time.time())}"), created only
        # when you manually log an actual bet slip result — a different
        # event than this Discord post. Wiring d1.add_slip_member() belongs
        # in parse_slip_entry, where it can look up eval_id the same way
        # _find_archived_evaluated_line already resolves player+line+stat.
        # Flagging rather than guessing at a fake slip_id here.
    # APEX-5PMGENERAL-2026-10-16: the 5 PM run (live) posts its own
    # "Pipeline complete" with board counts -- this one was a duplicate.
    if mode != "live":
        send_discord("system",
            f"✅ **Pipeline complete** — {dates['ct_time']}\n"
            f"Picks for {dates['display_day']}: {len(selected)} (🟢{len(green)} 🟡{len(yellow)})\n"
            f"Gate 11 rejected: {len(gate_rejected)}"
        )

    # Discord mode — send full evaluation to #general
    if mode == "discord":
        for pick in selected:
            player    = pick.get("player","")
            line      = pick.get("line","")
            stat      = pick.get("stat_type","")
            direction = pick.get("direction","")
            conf      = pick.get("confidence", 0)
            emoji     = pick.get("emoji","🟡")
            dir_arrow = "⬆" if direction == "MORE" else "⬇"

            # Prop type suffix for header
            ptype = pick.get("prop_type_classified", "standard")
            if ptype == "red_demon":
                ptype_suffix = " 🔴😈"
            elif ptype == "goblin":
                ptype_suffix = " 😈"
            else:
                ptype_suffix = ""

            # Header card
            header_card = (
                f"══════════════════════════════════════\n"
                f"**{player} — {line} {stat} — {dir_arrow} {direction} — {emoji} {conf:.0%}{ptype_suffix}**\n"
                f"══════════════════════════════════════"
            )

            # Append format correction line if Gate 11-B fired
            correction_line = pick.get("correction_line")
            if correction_line:
                header_card += f"\n{correction_line}"

            # Append correlation note if Total Sets correlated from TG
            correlation_note = pick.get("correlation_note")
            if correlation_note:
                header_card += f"\n{correlation_note}"

            # Append slip correlation flag if directions agree
            slip_flag = pick.get("slip_correlation_flag")
            if slip_flag:
                header_card += f"\n{slip_flag}"

            send_discord("general", header_card)

            # Full evaluation output — print to terminal AND send to Discord
            full_eval = pick.get("evaluation_output", "")
            if full_eval:
                # Print full eval to terminal so you can read it without scrolling Discord
                print("\n" + "="*60)
                print(f"FULL EVALUATION — {player} {line} {stat} — {dir_arrow} {direction} {emoji} {conf:.0%}")
                print("="*60)
                print(full_eval)
                print("="*60 + "\n")
                # Send to Discord in chunks
                chunks = [full_eval[i:i+1800] for i in range(0, len(full_eval), 1800)]
                for chunk in chunks:
                    send_discord("general", chunk)
                    time.sleep(0.5)
            else:
                send_discord("general", format_pick_card(pick, 1))

# ================================================================
# MANUAL PROP EVALUATION (Option C — screenshot via #general)
# ================================================================

SCREENSHOT_PARSE_PROMPT = """You are reading a screenshot.
Extract ONLY what is visible. Return ONLY valid JSON.

For a PROPS screenshot (PrizePicks player card), return a JSON array:
[{"player":"exact name as shown","opponent":"exact name as shown or null",
  "stat_type":"exact stat label as shown","line":number,
  "prop_type":"standard/goblin/demon","direction_locked":"MORE/LESS or null",
  "start":"the date/time text shown under the opponent name, copied exactly, e.g. 'Tue • 9:00am' or 'Today • 12:20pm', or null"}]

For a MONEYLINE screenshot (sportsbook odds screen), return a JSON object:
{"type":"moneyline","player":"first player name","opponent":"second player name",
 "player_odds":"e.g. +109 or -145","opponent_odds":"e.g. -139 or +113"}

CRITICAL RULES:
- Extract player and opponent names ONLY. Never guess surface, tournament, tour.
- Goblin = green devil icon = prop_type goblin
- Demon = red devil icon = prop_type demon
- Standard = no icon = prop_type standard
- If you cannot read a value clearly, omit it rather than guess.
- start: copy the date/time line under the opponent name character for character (day word, then the time). Do not convert or reformat it. Use null if it is not visible.
- Return ONLY the JSON. No explanation, no markdown fences.

NAME ACCURACY CHECK (read this before finalizing any name):
Professional tennis player names are usually recognizable, real surnames.
Before finalizing a name, ask yourself: does this look like a real, familiar
tennis player name, or does it look like it could be a one- or two-letter
misread (e.g. a name that's "almost" a well-known player's name but slightly
off)? If something looks slightly unusual, look at that part of the image
again more carefully before finalizing — small character-level misreads
(like a single swapped letter) are the most common real error in this task.
This check matters most for less-common or unusual-looking surnames — take
an extra careful look at those specifically."""

# ================================================================
# SLIP CONFIRMATION SCREENSHOT — legs + stake + payout extraction
# ================================================================
# Deliberately a SEPARATE prompt from SCREENSHOT_PARSE_PROMPT above,
# not a shared one with an extra field bolted on -- that prompt is
# about a PropPicks BOARD (what's available to evaluate); this one is
# about a SLIP CONFIRMATION (what was actually staked). Different
# screen, different fields, different downstream consumer
# (parse_slip_entry, not evaluate_manual_prop/evaluate_moneyline_screenshot).
SLIP_SCREENSHOT_PARSE_PROMPT = """You are reading a screenshot of a PLACED
SLIP CONFIRMATION (e.g. a PrizePicks "My Entries" entry showing the legs
that were picked, the entry/stake amount, and the potential payout) OR
that same entry's "Details" tab, which shows the full payout tier table
under a "Minimum Guarantee" or "Payouts" header (e.g. "6 of 6 correct
pays 68x $680", "5 of 6 correct pays 1x $10", "4 of 6 correct pays 0.4x
$4"). This is NOT a board of available props to evaluate -- it is a
record of a bet that was already placed. Extract ONLY what is visible.
Return ONLY valid JSON, no markdown fences, no explanation.

APEX-SLIPBONUS-2026-10-11: a promotional entry shows "BONUS to win $X" in
the header and a "Promotion" section such as "$25 Bonus Lineup" or "Bonus
$15.00 Lineup" -- for those set is_bonus true, bonus_amount to that dollar
figure, stake to the same figure, and potential_payout to the "to win"
amount. A Details tab may show NO legs at all -- return "legs": [] then, but
still fill the header/payout fields. entry_id = the "ID: ..." number if shown.

Return this exact shape:
{"is_bonus": true/false, "bonus_amount": number or null, "entry_id": "string or null",
 "legs":[
  {"player":"exact name as shown","bet_type":"prop","direction":"MORE/LESS",
   "line":number,"stat_label":"exact stat label as shown (e.g. 'Total Games',
   'Aces', 'Double Faults', 'Break Points Won', 'Games Won', 'Total Sets', or for
   other sports e.g. 'Points', 'Pts+Rebs+Asts')",
   "prop_type":"standard/goblin/demon",
   "sport":"TENNIS, or the league shown for that leg (e.g. WNBA, NBA, NFL)",
   "discount":true/false, "original_line":number or null},
  {"player":"exact name as shown","bet_type":"moneyline","direction":"MORE/LESS"}
 ],
 "stake":number or null,
 "potential_payout":number or null,
 "payout_table":[{"hits":number,"multiplier":number,"payout":number}, ...] or null
}

CRITICAL RULES:
- One entry per leg, in the order shown.
- stake = the entry amount / amount wagered (often labeled "Entry" or
  "Wager"). potential_payout = the amount shown for a full win (often
  labeled "To Win" or "Payout"). Both are dollar amounts -- return the
  bare number, no "$" sign.
- payout_table: ONLY fill this in if a "Minimum Guarantee" or "Payouts"
  breakdown listing multiple hit-count tiers is actually visible in this
  screenshot (typically the "Details" tab, not the main entry card).
  Return null if that breakdown isn't shown -- do not infer or estimate
  tiers that aren't visibly listed. Each row is one tier: hits = how
  many picks correct, multiplier = the "Nx" shown, payout = the dollar
  amount shown for that tier. Do NOT include a separate "1st place pays"
  leaderboard-bonus row in payout_table -- that's a different, unmodeled
  bonus path, not part of the Minimum Guarantee tier structure.
- If you cannot read stake or potential_payout clearly, return null for
  that field rather than guessing -- a wrong dollar figure is worse than
  a blank one the operator can fill in. Same rule for payout_table rows:
  a partially-unreadable tier should be omitted, not guessed.
- Goblin = green devil icon = prop_type goblin. Demon = red devil icon =
  prop_type demon. Standard = no icon = prop_type standard.
- APEX-DISCOUNT-2026-10-16: discount = true when the leg is a PrizePicks
  discount / promo / flash-sale play -- e.g. a "Discount", "Promo", "Sale" or
  price-tag badge, or a crossed-out regular line next to the line played.
  original_line = that crossed-out regular number if visible, else null.
  line is always the line actually played (the discounted one). If unsure,
  discount = false.
- sport: legs are usually tennis (sport TENNIS). When the leg shows another
  league or a non-tennis stat (Points, Rebounds, Pts+Rebs+Asts, ...), set
  sport to that league as shown (e.g. WNBA), or "OTHER" if no league is
  shown.
- If you cannot read a leg clearly, omit it rather than guess -- a
  missing leg is obvious and easy to add by hand; a wrong one is not.

NAME ACCURACY CHECK (read this before finalizing any name): professional
tennis player names are usually recognizable, real surnames. Before
finalizing a name, check it against the same discipline used for board
screenshots -- if a name looks like it could be a one- or two-letter
misread of a well-known player, look again before finalizing."""

# ================================================================
# SCREENSHOT IMAGE EVALUATION — TOURNAMENT CONTEXT RESOLUTION
# ================================================================

TOURNAMENT_SURFACE_MAP = {
    "wimbledon":        "grass",
    "queens club":      "grass",
    "queen's club":    "grass",
    "halle":            "grass",
    "nottingham":       "grass",
    "s-hertogenbosch":  "grass",
    "eastbourne":       "grass",
    "bad homburg":      "grass",
    "us open":          "hard",
    "australian open":  "hard",
    "miami":            "hard",
    "indian wells":     "hard",
    "cincinnati":       "hard",
    "canada":           "hard",
    "montreal":         "hard",
    "toronto":          "hard",
    "roland garros":    "clay",
    "french open":      "clay",
    "rome":             "clay",
    "madrid":           "clay",
    "barcelona":        "clay",
    "monte carlo":      "clay",
    "hamburg":          "clay",
}

TOUR_CONTEXT_PROMPT = """You are given web search results about a tennis player's current tournament.
Extract ONLY:
1. The exact tournament name (e.g. Wimbledon, US Open, Roland Garros)
2. The tour: atp if men's, wta if women's
Return ONLY valid JSON: {"tournament": "name", "tour": "atp/wta"}
If you cannot determine either with confidence, return: {"tournament": null, "tour": null}
No explanation. No markdown."""


def _grass_surface_plausible() -> bool:
    """2026-08-14 fix (surface-poisoning bug): the real grass season runs
    June to mid-July only. A 'grass' conclusion outside June/July is
    almost always the fallback-search failure mode (query/results
    echoing 'Wimbledon', which sits first in TOURNAMENT_SURFACE_MAP),
    not a real grass event — confirmed live 2026-08-13, when every
    evaluation of the night came back tagged grass in August, including
    a matchup evaluated as hard-court with full data the day before.
    Outside Jun/Jul, a grass keyword hit is treated as unconfirmed and
    resolution continues, rather than returned as fact."""
    return datetime.now(CT).month in (6, 7)


# ── APEX-SLIP-TIME-2026-08-24: the slip's own date/time, read and used
def _apex_parse_slip_start(text, now_ct=None):
    """'Tue • 9:00am' / 'Today • 12:20pm' / 'Tomorrow • 8:30pm' -> (utc_iso, ct_iso).
    PrizePicks shows the operator's local time (Central). Weekday names resolve to
    the next such day on or after today. Returns (None, None) on anything else.
    Never raises."""
    try:
        import re as _re
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        if not text:
            return None, None
        s = str(text).strip().lower()
        m_time = _re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", s)
        if not m_time:
            return None, None
        hh = int(m_time.group(1)); mm = int(m_time.group(2) or 0); ap = m_time.group(3)
        if not (1 <= hh <= 12 and 0 <= mm <= 59):
            return None, None
        if ap == "pm" and hh != 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
        now = now_ct or _dt.now(CT)
        base = now.date()
        m_day = _re.search(r"\b(today|tomorrow|mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", s)
        day = m_day.group(1) if m_day else "today"
        if day == "today":
            d = base
        elif day == "tomorrow":
            d = base + _td(days=1)
        else:
            want = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(day)
            d = base + _td(days=(want - base.weekday()) % 7)      # same weekday -> today
        local = CT.localize(_dt(d.year, d.month, d.day, hh, mm)) if hasattr(CT, "localize") \
            else _dt(d.year, d.month, d.day, hh, mm, tzinfo=CT)
        return local.astimezone(_tz.utc).isoformat(), local.isoformat()
    except Exception:
        return None, None


def resolve_tournament_context(player_name: str, opponent_name: str) -> dict:
    """
    Resolves tournament, surface, and tour for a player pair without
    trusting the screenshot for any of this.
    1. Try V8 API recent matches to infer current tournament.
    2. Fall back to Serper web search — scans raw results for keywords
       directly rather than relying purely on model JSON extraction.
    3. If both fail, return None — bot refuses to evaluate.
    """
    log("ImageIntake", f"Resolving tournament context for {player_name}")
    player_name = _clean_player_name(player_name)
    opponent_name = _clean_player_name(opponent_name)

    # v7.9.7 fix (item 2): doubles check runs FIRST, before any V8
    # lookup or web search — same principle as the moneyline-side check
    # in _is_doubles_entry. A doubles slip has no business entering
    # singles-shaped tournament/tour resolution at all.
    if _is_doubles_entry(player_name) or _is_doubles_entry(opponent_name):
        log("ImageIntake", f"Doubles entry rejected: {player_name} vs {opponent_name} — doubles not supported")
        send_discord("alerts",
            f"🚫 **DOUBLES NOT SUPPORTED** — {player_name} vs {opponent_name}\n"
            f"This system only evaluates singles matches. Doubles slip skipped."
        )
        return None

    # ── Step 0 (APEX-CONTEXT-2026-08-24): the market's fixture IS the current
    # event. Everything below infers the tournament from the player's most recent
    # match, which on day one of a new event is LAST week's tournament (08-24: a
    # US Open qualifier resolved as clay). Ask the price feed which fixture it
    # lists for this pair and map that name through TOURNAMENT_SURFACE_MAP first.
    try:
        import apex_price_feed as _apf
        _fc = _apf.fixture_context(player_name, opponent_name)
    except Exception as _e:
        log("ImageIntake", f"fixture context unavailable (non-fatal): {_e}")
        _fc = None
    if _fc and _fc.get("tournament"):
        _fname = str(_fc["tournament"]).lower()
        _ftour = _fc.get("tour") or "atp"
        for _kw, _surf in TOURNAMENT_SURFACE_MAP.items():
            if _kw in _fname:
                if _surf == "grass" and not _grass_surface_plausible():
                    continue
                log("ImageIntake",
                    f"Fixture confirmed: '{_fc['tournament']}' -> {_surf} | {_ftour} "
                    f"(from the market's fixture list, not recent-match inference)")
                return {"tournament": _kw,
                        "surface":    _surf,
                        "tour":       _ftour,
                        "tour_confirmed": True,
                        "context_source": "fixture"}
        log("ImageIntake",
            f"Fixture found ('{_fc['tournament']}', {_ftour}) but no surface keyword in "
            f"TOURNAMENT_SURFACE_MAP -- surface falls back to recent-match inference")

    # ── Step 1: V8 recent matches — try both ATP and WTA ────────
    # v7.9.7 fix (Bug 1): the previous loop called v8_get_recent_matches
    # with the raw player_name regardless of tour_attempt, so WTA players
    # like Osaka always got the ATP endpoint (empty), never the WTA one.
    # Fix: resolve the canonical name FOR THAT TOUR first, then fetch.
    #
    # v7.9.7 fix (Fix A, this session): previously, if the canonical name
    # resolved correctly and real recent matches came back, but none of
    # those matches' tournament names matched a keyword in
    # TOURNAMENT_SURFACE_MAP (a small, incomplete list of named majors/
    # notable events), the code did `break` and threw away a WORKING
    # tour resolution entirely — falling through to a web search that
    # could then default to the wrong tour (this was the actual root
    # cause behind Gauff and Pegula both reporting MISSING data despite
    # both being correctly resolvable in V8, confirmed via direct
    # testing). Fix: v8's own match data already carries a real surface
    # field directly (get_recent_matches' "surface" key, sourced from
    # the vendor's own court.name) — use that as a reliable fallback
    # instead of discarding a confirmed tour just because our own
    # keyword list didn't recognize the tournament name.
    if _V8_AVAILABLE:
        for tour_attempt in ("atp", "wta"):
            try:
                from openclaw_v8_api import resolve_player_name as _resolve_name
                canonical = _resolve_name(player_name, tour=tour_attempt)
                if not canonical:
                    continue
                recent = v8_get_recent_matches(canonical, limit=5)
                if recent:
                    for match in recent:
                        t_name = (match.get("tournament") or
                                  match.get("tournament_name") or
                                  match.get("tournamentName") or "").lower()
                        if t_name:
                            for keyword, surface in TOURNAMENT_SURFACE_MAP.items():
                                if keyword in t_name:
                                    if surface == "grass" and not _grass_surface_plausible():
                                        log("ImageIntake",
                                            f"Grass keyword '{keyword}' matched from a recent-match "
                                            f"tournament name OUTSIDE grass season — treating as a "
                                            f"stale/implausible match, continuing resolution")
                                        continue
                                    log("ImageIntake",
                                        f"V8 confirmed: {t_name} -> "
                                        f"{surface} | {tour_attempt}")
                                    return {"tournament": keyword,
                                            "surface":    surface,
                                            "tour":       tour_attempt,
                                            "tour_confirmed": True}
                    # Fix A: no keyword matched, but we DID successfully
                    # resolve this player on this tour and DID get real
                    # recent match data — don't discard that. Use V8's
                    # own surface field from the most recent match
                    # directly, and the tournament name as-is (even if
                    # unrecognized) rather than falling through to a
                    # web search that risks defaulting the tour wrong.
                    most_recent = recent[0]
                    v8_surface = (most_recent.get("surface") or "").lower()
                    if v8_surface == "grass" and not _grass_surface_plausible():
                        log("ImageIntake",
                            f"V8 most-recent match surface is grass OUTSIDE grass "
                            f"season for {player_name} — stale match, not current "
                            f"tournament; not trusting it as the current surface")
                        v8_surface = ""
                    if v8_surface in ("hard", "clay", "grass"):
                        raw_tournament = (most_recent.get("tournament") or
                                          most_recent.get("tournament_name") or
                                          most_recent.get("tournamentName") or
                                          "Current tournament")
                        log("ImageIntake",
                            f"V8 confirmed via surface field (unrecognized "
                            f"tournament name '{raw_tournament}'): "
                            f"{v8_surface} | {tour_attempt}")
                        return {"tournament": raw_tournament,
                                "surface":    v8_surface,
                                "tour":       tour_attempt,
                                "tour_confirmed": True}
                    log("ImageIntake",
                        f"V8 {tour_attempt}: matches found but no "
                        f"tournament keyword or usable surface field "
                        f"for {player_name}")
                    break
            except Exception as e:
                log("ImageIntake", f"V8 {tour_attempt} step failed: {e}")

    # ── Step 2: Serper web search ──
    # 2026-08-14 fix (surface-poisoning bug): the old query embedded
    # "Wimbledon US Open Australian Open Roland Garros" in the search
    # itself — search results echo the query, the keyword scan below
    # then matched "wimbledon" (first entry in TOURNAMENT_SURFACE_MAP)
    # nearly every time Step 1 failed, mislabeling everything as grass.
    # Tournament names removed from the query entirely.
    names_to_try = [n for n in [player_name, opponent_name] if n]
    for name in names_to_try:
        query = f"{name} tennis player current tournament this week"
        search_results = web_search(query)
        if not search_results:
            continue

        results_lower = search_results.lower()
        for keyword, surface in TOURNAMENT_SURFACE_MAP.items():
            if keyword in results_lower:
                if surface == "grass" and not _grass_surface_plausible():
                    log("ImageIntake",
                        f"Grass keyword '{keyword}' found in search results OUTSIDE "
                        f"grass season — near-certainly query/article echo, not the "
                        f"current tournament; skipping")
                    continue
                # v7.9.7 fix (Bug 2): old check `if "wta" in tour_result`
                # missed responses like "women", "women's tour", "WTA Tour"
                # so ATP stayed the default for obvious WTA players. Fix:
                # broaden the check, and also scan the raw search text as
                # a secondary signal if the model call is ambiguous.
                #
                # v7.9.7 fix (Fix B, this session): "atp" here was a
                # silent default — if neither the model call nor the
                # secondary text scan confidently detected WTA, this
                # returned as if ATP were CONFIRMED, with nothing
                # distinguishing a real ATP confirmation from a guess.
                # This was the second half of the Gauff/Pegula root
                # cause: even when Fix A's V8 path correctly gave up
                # (genuinely no data there), this fallback path could
                # silently guess wrong instead of saying so. Now tracks
                # tour_confirmed explicitly — False means "this is an
                # unconfirmed guess," which the caller can choose to
                # surface as a flag rather than treat as fact.
                tour = "atp"
                tour_confirmed = False
                try:
                    tour_result = call_model(
                        MODEL_HAIKU,
                        "Return ONLY 'atp' or 'wta' based on whether this player "
                        "is on the men's or women's tour. No other text.",
                        f"Player name: {player_name}",
                        temperature=0.0, max_tokens=10,
                    )
                    if tour_result:
                        r = tour_result.lower().strip()
                        if any(w in r for w in ("wta", "women", "female", "woman")):
                            tour = "wta"
                            tour_confirmed = True
                        elif "atp" in r or "men" in r:
                            tour_confirmed = True
                except Exception as _e:
                    # Audit fix 2.1 (this session): was a bare pass --
                    # a failed tour search and a search that genuinely
                    # found nothing looked identical in the log.
                    log("Moneyline", f"Tour-detection search failed "
                        f"(not just inconclusive): {_e}")

                # Secondary: scan raw search text for WTA signals
                if tour == "atp" and not tour_confirmed:
                    wta_signals = ("wta", "women's singles", "women's tennis",
                                   "women's draw", "wtatennis")
                    if any(sig in results_lower for sig in wta_signals):
                        tour = "wta"
                        tour_confirmed = True
                        log("ImageIntake",
                            f"WTA detected from search text for {player_name}")

                if not tour_confirmed:
                    log("ImageIntake",
                        f"⚠️ Tour UNCONFIRMED for {player_name} — defaulting to "
                        f"'{tour}' as a guess, not a confirmed fact. Verify manually.")

                log("ImageIntake",
                    f"Serper keyword match: {keyword} -> {surface} | {tour} "
                    f"(confirmed={tour_confirmed})")
                return {"tournament": keyword, "surface": surface, "tour": tour,
                        "tour_confirmed": tour_confirmed}

        try:
            parse_result = call_model(
                MODEL_HAIKU,
                TOUR_CONTEXT_PROMPT,
                f"Search results:\n{search_results[:2000]}\n\nPlayer: {name}",
                temperature=0.1, max_tokens=200,
            )
            if parse_result:
                clean  = re.sub(r"```json|```", "", parse_result).strip()
                parsed = json.loads(clean)
                t_name = (parsed.get("tournament") or "").lower()
                # v7.9.7 fix (Fix B): tour is only "confirmed" if the
                # model actually returned a tour value — "or 'atp'" was
                # a silent guess exactly like the branch above.
                tour_raw = parsed.get("tour")
                tour = tour_raw or "atp"
                tour_confirmed = tour_raw is not None
                if not tour_confirmed:
                    log("ImageIntake",
                        f"⚠️ Tour UNCONFIRMED for {player_name} (Serper model "
                        f"extraction) — defaulting to '{tour}' as a guess.")
                if t_name:
                    for keyword, surface in TOURNAMENT_SURFACE_MAP.items():
                        if keyword in t_name:
                            if surface == "grass" and not _grass_surface_plausible():
                                log("ImageIntake",
                                    f"Serper model extracted grass tournament "
                                    f"'{keyword}' OUTSIDE grass season — implausible, "
                                    f"skipping")
                                continue
                            log("ImageIntake",
                                f"Serper model confirmed: {keyword} -> {surface} | "
                                f"{tour} (confirmed={tour_confirmed})")
                            return {"tournament": keyword,
                                    "surface": surface, "tour": tour,
                                    "tour_confirmed": tour_confirmed}
        except Exception as e:
            log("ImageIntake", f"Serper model parse failed: {e}")

    log("ImageIntake", f"Could not confirm tournament context for {player_name}")
    return None


def save_evaluation_reasoning(player: str, opponent: str,
                               eval_type: str, full_text: str, dates: dict,
                               target_dir: Path = None, pick_tag: str = ""):
    """
    Saves full evaluation reasoning to a dated file.

    v7.9.7 (moneyline folder consolidation, per operator request):
    added target_dir so moneyline reasoning can be routed into
    ML_ARCHIVE_DIR — consolidating it with the moneyline signal-
    breakdown JSON already written there this session — instead of
    being mixed into picks_archive alongside props' reasoning files.
    Defaults to ARCHIVE_DIR (the original behavior) when not specified,
    so the props call site needs no changes.
    """
    if target_dir is None:
        target_dir = ARCHIVE_DIR
    safe_p1 = re.sub(r"[^a-zA-Z0-9]", "_", player)[:20]
    safe_p2 = re.sub(r"[^a-zA-Z0-9]", "_", opponent)[:20]
    # APEX-CARDFIX-001: without a per-pick tag every pick in a match wrote to
    # the same path and truncated the previous card (open mode "w").
    safe_tag = re.sub(r"[^a-zA-Z0-9.]", "_", str(pick_tag))[:24]
    filename = (target_dir /
                (f"reasoning_{dates['archive_date']}_{eval_type}_{safe_p1}_vs_{safe_p2}"
                 + (f"_{safe_tag}" if safe_tag else "") + ".txt"))
    try:
        with open(filename, "w") as f:
            f.write(f"DEMETRIUS -- {eval_type.upper()} REASONING\n")
            f.write(f"{player} vs {opponent}\n")
            f.write(f"Generated: {dates['ct_time']} | {dates['archive_date']}\n")
            f.write("=" * 60 + "\n\n")
            f.write(full_text)
        log("ImageIntake", f"Reasoning saved: {filename.name}")
    except Exception as e:
        log("ImageIntake", f"Could not save reasoning: {e}")


def evaluate_moneyline_screenshot(player: str, opponent: str,
                                   player_odds: str, opponent_odds: str,
                                   dates: dict) -> None:
    """Called when a sportsbook moneyline screenshot is sent to #general."""
    log("ImageIntake", f"Moneyline screenshot: {player} vs {opponent}")
    ctx = resolve_tournament_context(player, opponent)
    if not ctx:
        send_discord("general",
            f"Could not confirm tournament for {player} vs {opponent}. "
            f"Please run manually: python3 openclaw_core.py --moneyline "
            f"\"{player} | {opponent} | surface | tour\"")
        return
    surface    = ctx["surface"]
    tour       = ctx["tour"]
    tournament = ctx["tournament"]

    # v7.9.7 (Fix B, this session): tour_confirmed=False means the tour
    # was a fallback guess, not a confirmed fact — surface this visibly
    # rather than silently evaluating as if it were certain.
    if not ctx.get("tour_confirmed", True):
        send_discord("alerts",
            f"⚠️ **TOUR UNCONFIRMED** — {player} vs {opponent}\n"
            f"Proceeding with tour='{tour.upper()}' as a best guess — could not "
            f"confidently confirm ATP vs WTA. Verify manually if this affects the pick."
        )

    result = moneyline_evaluation_agent(
        player, opponent, surface, tournament_id=ctx.get("tournament_id"),
        tour=tour, tournament_name=tournament)
    full_text = result.get("model_output", json.dumps(result, indent=2, default=str))
    save_evaluation_reasoning(player, opponent, "moneyline", full_text, dates, target_dir=ML_ARCHIVE_DIR)
    status = result.get("status", "NO_PLAY")
    if status == "SYSTEM_FAILURE":
        # Already alerted inside moneyline_evaluation_agent with the
        # specific cause -- just skip posting a pick here. A system
        # failure must never be presented as a NO_PLAY judgment, since
        # NO_PLAY implies the system evaluated the matchup and
        # concluded there's no edge, while SYSTEM_FAILURE means no
        # evaluation actually happened.
        return
    if status in ("GREEN", "YELLOW"):
        color  = "\U0001f7e2" if status == "GREEN" else "\U0001f7e1"
        winner = result.get("winner", player)
        conf   = result.get("confidence", "--")
        implied = format_american_odds(confidence_to_american_odds(conf))
        context_lines = moneyline_context_lines(player, opponent, result.get("research_data", {}))
        fav_line = favorite_underdog_line(player, player_odds, opponent, opponent_odds)
        one_sentence  = _extract_one_sentence(full_text)
        compact = (f"{color} **{winner} -- MONEYLINE WIN**\n"
                   f"Confidence: {conf}% -- {status}\n"
                   f"Apex implied fair odds: {implied} (derived from confidence, not a market price)\n"
                   f"Surface: {surface.title()} | {tournament.title()}\n"
                   f"Odds: {player} {player_odds} / {opponent} {opponent_odds}"
                   + (f"\n{fav_line}" if fav_line else "")
                   + (f"\n{context_lines}" if context_lines else "")
                   + (f"\n*{one_sentence}*" if one_sentence else ""))
        # 2026-08-04: the odds were already being captured automatically
        # by the screenshot vision parse (SCREENSHOT_PARSE_PROMPT) but
        # only ever used for this Discord message, then discarded --
        # save them onto the same real eval_id row so they're not lost.
        d1.transition(result.get("eval_id"), "EVALUATION_COMPLETE",
                       player_odds=player_odds, opponent_odds=opponent_odds,
                       _logger=log)
        # Fix (this session, pairs with the itemized moneyline
        # compliance penalty): a compliance-penalized pick must SAY so
        # where the pick posts -- previously the flag lived only in
        # logs/archive while the posted message showed a clean,
        # unannotated confidence.
        if result.get("compliance_note"):
            compact += f"\n{result['compliance_note']}"
        # Ordering bug fix (this session, found during a full-system
        # audit): this used to mark POSTED=1 in Dataset A BEFORE
        # send_discord() was even called (that call sat after this
        # whole if/else block, unconditionally). A Discord failure
        # meant Dataset A recorded a post that never happened, with no
        # way to tell later. send_discord() now returns a real
        # success/failure signal (see its own docstring) -- send first,
        # then gate the transition on the real result.
        _ml_post_ok = send_discord("moneyline", compact)
        d1.heartbeat("posting", "WORKING", detail=f"{player} vs {opponent}", _logger=log)
        if _ml_post_ok:
            d1.transition(result.get("eval_id"), "POSTED", posted=1, _logger=log)
            d1.heartbeat("posting", "IDLE", increment_success=True, _logger=log)
        else:
            d1.transition(result.get("eval_id"), "SCORING_COMPLETE",
                           detail="Discord post failed -- pick never reached Discord",
                           posted=0, predicted_winner=result.get("winner"), _logger=log)
            d1.heartbeat("posting", "IDLE", increment_failure=True, _logger=log)
            log("Discord", f"⚠️ Moneyline post FAILED — {player} vs {opponent} never "
                            f"reached Discord. Dataset A correctly shows posted=0.")
    else:
        reason  = result.get("reason", "Insufficient signal agreement")
        if result.get("advisory"):
            sug_conf = result.get("suggested_confidence")
            implied  = format_american_odds(confidence_to_american_odds(sug_conf))
            compact = (f"NO PLAY (SUGGESTED) -- {player} vs {opponent}\n"
                       f"Surface: {surface.title()} | {tournament.title()}\n"
                       f"Reason: {reason}\n"
                       f"Phase 1 leaned {result.get('suggested_winner')} at {sug_conf}% "
                       f"(implied {implied}) -- confirmation signals disagreed, use your own judgment")
        else:
            compact = (f"NO PLAY -- {player} vs {opponent}\n"
                       f"Surface: {surface.title()} | {tournament.title()}\n"
                       f"Reason: {reason}")
        send_discord("moneyline", compact)
    dom = result.get("dominance", {})
    breakdown = dom.get("breakdown", {})
    summary_lines = [
        f"Moneyline Summary -- {player} vs {opponent}",
        f"Tournament: {tournament.title()} | Surface: {surface.title()} | Tour: {tour.upper()}",
        f"Phase 1 score: {dom.get('dominance_score','--')} -> {dom.get('predicted_side','--')}",
    ]
    if breakdown:
        summary_lines.append(
            f"Serve: {breakdown.get('serve_edge','--')} | "
            f"Return: {breakdown.get('return_edge','--')} | "
            f"Clutch: {breakdown.get('clutch_edge','--')} | "
            f"Form: {breakdown.get('form_edge','--')}"
        )
    summary_lines.append(f"Result: {status} | Full reasoning saved to moneyline archive.")
    send_discord("system", "\n".join(summary_lines))

    # v7.9.7 fix (per operator request): moneyline previously had no
    # completion message anywhere — props already post one to #general
    # ("Evaluation complete — N graded, M skipped") when finished, but
    # moneyline only ever posted to #moneyline-channel and
    # #systems-channel, both silent from #general's perspective. This
    # was the direct cause of the "I sent this 5 minutes ago, is it
    # stuck?" confusion earlier tonight — the evaluation had actually
    # finished in under a minute, but nothing ever confirmed that in
    # the channel being watched.
    send_discord("general",
        f"✅ Moneyline evaluation complete — {player} vs {opponent}. "
        f"See #moneyline-channel and #systems-channel."
    )

def evaluate_manual_prop(user_input, dates, weights,
                         image_data=None, media_type="image/jpeg"):
    """
    v7.9.6 image intake: called when a PrizePicks screenshot or text
    prop is sent to #general.

    For screenshots: resolves tournament context via API then web search
    before evaluating. Pre-fetches V8 packet ONCE and reuses it for all
    props on the card — avoids duplicate API calls per prop.
    Routes compact results to #picks-channel, summary to #systems-channel,
    full reasoning saved to disk.

    For text input: same pipeline, routes to #picks-channel.
    APEX-DEMONPARITY-2026-09-29: Demon props are evaluated through Gate 11
    / odds / the engine exactly like Goblin and Standard props — no longer
    skipped here (see the "DEMON prop first" note below).

    media_type: real MIME type from discord attachment (image/png,
    image/jpeg etc) — passed from discord_bot.py so Claude API gets
    the correct type. Defaults to image/jpeg for backward compatibility.
    """
    log("Manual", "Processing manual prop request")

    props        = []
    first_player = None
    first_opponent = None

    # Handle screenshot image
    if image_data:
        log("Manual", f"Processing PrizePicks screenshot (media_type={media_type})")
        # v7.9.7 (this session): switched from Haiku to Sonnet for
        # screenshot vision reads specifically. Root cause of the Gauff/
        # Pegula incidents traced back to a name misread at THIS step
        # (Haiku read "Pegula" as "Peguia") — everything downstream of
        # intake was working correctly. Cost impact is small since this
        # is one short call per screenshot, not a high-volume workload;
        # confirmed with the operator this tradeoff is worth it.
        parse_result = call_model(
            MODEL_SONNET,
            SCREENSHOT_PARSE_PROMPT,
            [{"type": "image",
              "source": {"type": "base64",
                         "media_type": media_type,
                         "data": image_data}},
             {"type": "text",
              "text": "Extract all props from this PrizePicks screenshot."}],
            temperature=0.1, max_tokens=800
        )
        if not parse_result:
            send_discord("general",
                "Could not read the screenshot. "
                "Please send a clearer image or type the prop manually: "
                "Player MORE/LESS LINE STAT")
            return

        try:
            clean  = re.sub(r"```json|```", "", parse_result).strip()
            parsed = json.loads(clean)
        except Exception:
            log("Manual", "Screenshot parse failed -- not valid JSON")
            send_discord("general",
                "Screenshot could not be parsed. "
                "Please type the prop manually: Player MORE/LESS LINE STAT")
            return

        first_player   = parsed[0].get("player") if parsed else None
        first_opponent = parsed[0].get("opponent") if parsed else None

        ctx = resolve_tournament_context(first_player, first_opponent) if first_player else None
        if not ctx:
            send_discord("general",
                f"Could not confirm tournament or surface for {first_player}. "
                f"Cannot evaluate without confirmed context. "
                f"Please type the prop manually: Player MORE/LESS LINE STAT")
            return

        surface    = ctx["surface"]
        tour       = ctx["tour"]
        tournament = ctx["tournament"]
        is_gs = any(gs in tournament.lower() for gs in
                    ("wimbledon", "us open", "australian open",
                     "roland garros", "french open"))

        # v7.9.7 (Fix B, this session): same visibility as the moneyline
        # path — a guessed tour should never be silent.
        if not ctx.get("tour_confirmed", True):
            send_discord("alerts",
                f"⚠️ **TOUR UNCONFIRMED** — {first_player}\n"
                f"Proceeding with tour='{tour.upper()}' as a best guess — could not "
                f"confidently confirm ATP vs WTA. Verify manually if this affects the pick."
            )

        log("Manual", f"Context confirmed: {tournament} | {surface} | {tour.upper()}")

        for p in parsed:
            # APEX-SLIP-TIME-2026-08-24: the slip's own date/time, Central
            _s_utc, _s_ct = _apex_parse_slip_start(p.get("start"))
            if p.get("start") and not _s_ct:
                log("Manual", f"slip start text not understood: {p.get('start')!r} (ignored)")
            props.append({
                **p,
                "stat_type":                 normalize_stat_type(p.get("stat_type", "")),
                "tournament":                tournament,
                "surface":                   surface,
                "tour":                      tour,
                "tournament_stage":          3 if is_gs else 1,
                "is_grand_slam":             is_gs,
                "both_directions_available": p.get("prop_type") == "standard",
                "match_date":                (_s_ct[:10] if _s_ct else dates["match_date"]),
                "slip_start":                p.get("start"),
                "slip_start_utc":            _s_utc,
                "slip_start_ct":             _s_ct,
                "same_day_3set_count":       0,
            })

    # Handle text input
    elif isinstance(user_input, str) and user_input.strip():
        parts = user_input.strip().split()
        if len(parts) >= 3:
            first_player = " ".join(parts[:-3]) if len(parts) > 4 else parts[0]
            ctx = resolve_tournament_context(first_player, None)
            surface    = ctx["surface"]    if ctx else "hard"
            tour       = ctx["tour"]       if ctx else "atp"
            tournament = ctx["tournament"] if ctx else "Current tournament"
            is_gs      = any(gs in tournament.lower() for gs in
                             ("wimbledon", "us open", "australian open",
                              "roland garros", "french open")) if ctx else False
            first_opponent = "Research required"
            props.append({
                "player":                    first_player,
                "opponent":                  first_opponent,
                "stat_type":                 normalize_stat_type(parts[-1]),
                "line":                      float(parts[-2]) if parts[-2].replace(".", "").isdigit() else 0,
                "direction":                 parts[-3].upper() if parts[-3].upper() in ("LESS","MORE") else "LESS",
                "tournament":                tournament,
                "surface":                   surface,
                "tour":                      tour,
                "tournament_stage":          3 if is_gs else 1,
                "is_grand_slam":             is_gs,
                "both_directions_available": True,
                "prop_type_classified":      "standard",
                "match_date":                dates["match_date"],
                "same_day_3set_count":       0,
            })

    if not props:
        send_discord("general",
            "To evaluate a prop send:\n"
            "PlayerName LESS/MORE LINE STAT_TYPE\n"
            "Example: Tiafoe LESS 10.5 aces\n\n"
            "Or send a PrizePicks screenshot directly to #general."
        )
        return

    # APEX-PROPCLASS-2026-08-31: classify goblin / red demon / standard.
    # filter_props sets prop_type_classified, locks MORE-only props to
    # direction_locked="MORE" and both_directions_available=False, and drops
    # legacy demons. Without this the engine can pick LESS on a prop where
    # only MORE is offered. Fail-open: any error leaves props untouched.
    try:
        _before = len(props)
        props = filter_props(props, dates)
        log("Manual", "prop types: %s" % ", ".join(
            "%s=%s" % (p.get("player"), p.get("prop_type_classified"))
            for p in props))
        if len(props) != _before:
            log("Manual", "filter_props dropped %d prop(s)" % (_before - len(props)))
    except Exception as _e:
        log("Manual", "prop classification skipped (%s) -- props unchanged"
            % type(_e).__name__)

    # ── Pre-fetch V8 packet ONCE for all props on this card ──────
    # This is the key performance fix: instead of hitting the V8 API
    # once per prop (8x for Tiafoe's card), we fetch it once and
    # attach it to every prop so research_agent reuses it rather than
    # re-fetching. Cuts 8 sequential API round trips down to 1.
    v8_packet_cache = None
    if _V8_AVAILABLE and props:
        try:
            fp = props[0]
            _prefetch_tour = fp.get("tour", "atp")
            v8_packet_cache = build_tennis_packet(
                fp.get("player", ""),
                fp.get("opponent", ""),
                fp.get("surface", "hard"),
                _prefetch_tour
            )
            # Bug fix (this session, confirmed live on a real pick):
            # this pre-fetch runs BEFORE Gate 11 ever executes, using
            # whatever tour intake guessed (e.g. "atp" as a fallback
            # default when a tournament name like "M25 Laval" isn't
            # recognized) -- but Gate 11 runs PER-PROP, later, inside
            # process_single_prop, and can correct that guess (e.g. to
            # "wta") after this packet is already built and cached.
            # Confirmed live: Katie Volynets (a real, well-documented
            # WTA #101 player, 69 real hard-court matches on file)
            # resolved to "could not be resolved/found in V8 at all"
            # because this pre-fetch queried the ATP database before
            # Gate 11's correction to WTA ever happened -- and every
            # prop on the card then reused that stale, wrong-tour
            # packet, since nothing here re-validates it against the
            # corrected tour later. Tagging which tour this packet was
            # actually built with lets research_agent detect the
            # mismatch and re-fetch fresh instead of silently trusting
            # a packet built for the wrong tour.
            if v8_packet_cache:
                v8_packet_cache["_prefetch_tour"] = _prefetch_tour
            log("Manual",
                f"V8 packet pre-fetched -- reusing for all {len(props)} props")
        except Exception as e:
            log("Manual", f"V8 pre-fetch failed (non-fatal): {e}")

    for prop in props:
        if v8_packet_cache:
            prop["_v8_packet_prefetched"] = v8_packet_cache
    # ─────────────────────────────────────────────────────────────

    results      = []
    skipped_tags = []

    # APEX-WIRING-2026-08-27: the fantasy-score odds gate is gone. It required
    # operator-typed odds, which a Discord screenshot never carries (the
    # automatic Pinnacle price is fetched later, inside _apex_engine_pick), so
    # it blocked every fantasy screenshot. The engine gate decides now.
    #
    # APEX-DEMONPARITY-2026-09-29: this used to hard-skip every DEMON prop
    # right here, before Gate 11 / odds / the engine ever saw it -- meaning
    # every screenshot submission silently dropped 100% of its Demon props.
    # /fetch never had this block and evaluates Demon props normally, so the
    # two paths disagreed. Demon props now go through the exact same
    # Gate 11 -> odds -> engine flow as Goblin and Standard props; nothing
    # is excluded here based on prop type.
    evaluable = list(props)

    # ── Evaluate all remaining props in parallel ────────────────────
    # Instead of checking props one at a time (8 props x 10-30 sec each
    # = 5+ minutes), run up to 4 at once using ThreadPoolExecutor. This
    # cuts an 8-prop card down to roughly the time of a single prop call.
    # max_workers=4 keeps us from hammering the Claude API with too many
    # simultaneous requests at once.
    results_by_index = {}
    if evaluable:
        # Bug fix (caught during this same fix, before deployment): a
        # `with ThreadPoolExecutor(...) as executor:` block always calls
        # executor.shutdown(wait=True) on exit, unconditionally -- there
        # is no way to pass wait=False through the `with` statement's
        # implicit __exit__. That means even after catching
        # FuturesTimeoutError below and deciding to stop waiting, the
        # `with` block would still silently block at its own exit until
        # the stuck future finishes anyway, completely defeating the
        # timeout. Manual create/shutdown lets the timeout path call
        # shutdown(wait=False) specifically, so this code actually stops
        # waiting when it says it will.
        executor = ThreadPoolExecutor(max_workers=4)
        _batch_timed_out = False
        try:
            future_to_idx = {
                executor.submit(process_single_prop, prop, dates, weights): i
                for i, prop in enumerate(evaluable)
            }
            # Bug fix (this session, confirmed live): nothing posts to
            # #picks for this card until every future in this batch
            # resolves -- as_completed() blocks until ALL futures are
            # done, and the reassembly/posting code below only runs
            # after this `with` block exits. Confirmed live: 2 of 5
            # props on a real card (Putintseva vs Radivojevic, Aces +
            # Break Points Won) never completed -- root cause not yet
            # diagnosed, but call_model itself has a confirmed 240s
            # timeout, so the hang is most likely inside the V8 API
            # layer's own network calls, which have no confirmed
            # timeout of their own. This silently blocked the other 3
            # props' real, already-computed results from ever posting,
            # with zero Discord visibility -- the only way it was
            # diagnosed was manual OS-level process inspection (lsof/ps)
            # entirely outside this codebase. BATCH_TIMEOUT bounds the
            # wait so a stalled prop can no longer hold the whole card
            # hostage indefinitely -- past this point we alert loudly,
            # post whatever DID complete, and mark the rest as timed
            # out instead of leaving no signal at all that anything
            # is wrong.
            BATCH_TIMEOUT = 900  # 15 min -- comfortably above the ~16
                                  # min worst case for one prop (2 eval
                                  # attempts + 2 compliance checks, each
                                  # individually capped at 240s), far
                                  # shorter than "indefinite."
            try:
                for future in as_completed(future_to_idx, timeout=BATCH_TIMEOUT):
                    idx = future_to_idx[future]
                    try:
                        results_by_index[idx] = future.result()
                    except Exception as e:
                        # Bug fix (this session): this used to be
                        # local-log-only (log("Manual", ...)), the only
                        # failure path in this file with no Discord
                        # alert -- inconsistent with every other failure
                        # mode hardened tonight, all of which alert
                        # loudly. Now sends the same class of alert as
                        # the rest of the system.
                        p = evaluable[idx]
                        log("Manual", f"Prop evaluation threw an error: {e}")
                        send_discord("alerts",
                            f"\U0001f6d1 **PROP EVALUATION CRASHED** \u2014 "
                            f"{p.get('player','?')} {p.get('stat_type','?')} "
                            f"{p.get('line','?')}\n"
                            f"Exception: {type(e).__name__}: {e}\n"
                            f"No pick generated for this prop."
                        )
                        results_by_index[idx] = None
            except FuturesTimeoutError:
                _batch_timed_out = True
                stuck = [evaluable[i] for i in range(len(evaluable))
                         if i not in results_by_index]
                stuck_desc = "; ".join(
                    f"{p.get('player','?')} {p.get('stat_type','?')} {p.get('line','?')}"
                    for p in stuck
                ) or "unknown"
                log("Manual", f"BATCH TIMEOUT \u2014 {len(stuck)} prop(s) did not "
                    f"complete within {BATCH_TIMEOUT}s: {stuck_desc}")
                send_discord("alerts",
                    f"\U0001f6d1 **BATCH STALLED \u2014 {len(stuck)} prop(s) never "
                    f"completed** within {BATCH_TIMEOUT // 60} min\n"
                    f"Stuck: {stuck_desc}\n"
                    f"Any other props on this card that DID complete post below "
                    f"as usual. The stuck prop(s) will not resolve until the bot "
                    f"is restarted -- check for a hang with no timeout in the V8 "
                    f"API layer specifically, since call_model itself has a "
                    f"confirmed 240s timeout."
                )
                for i in range(len(evaluable)):
                    if i not in results_by_index:
                        results_by_index[i] = None
        finally:
            # Bug fix (caught during this same fix, before deployment):
            # normal path waits for clean shutdown as before (wait=True,
            # matching the old `with` block's behavior exactly). Timeout
            # path uses wait=False -- the whole point of catching
            # FuturesTimeoutError above is to stop waiting on the stuck
            # future; calling shutdown(wait=True) here would silently
            # block again and undo everything the timeout was for. Note
            # this does NOT kill the stuck thread (Python cannot force-
            # kill a running thread) -- it just stops THIS code from
            # waiting on it, so already-completed picks can post now
            # instead of never.
            executor.shutdown(wait=not _batch_timed_out)

    # ── Reassemble in original card order (workers finish out of order) ──
    for i, prop in enumerate(evaluable):
        scored = results_by_index.get(i)
        stat   = prop.get("stat_type", "")
        if scored:
            results.append(scored)
        else:
            skipped_tags.append(
                f"Gate/pipeline reject: {prop.get('player','?')}"
                f" {stat} {prop.get('line','?')}")

    # Compact results -- APEX-PICKSCLEAN-2026-10-24: a screenshot/typed prop's cards now
    # answer in #general (where it was asked); #picks only carries the plays card + results.
    if results:
        pick_lines = [f"Props -- {dates['ct_time']}\n"]
        for i, r in enumerate(results, 1):
            pick_lines.append(format_pick_card(r, i))
            pick_lines.append("")
        send_discord("general", "\n".join(pick_lines))

        # Usage/cost report (this session, approved): real measured
        # tokens + retry clear-rate after every batch, to #systems.
        try:
            post_usage_report(dates, len(results))
        except Exception as _ue:
            log("Usage", f"Usage report failed (non-fatal): {_ue}")

        for r in results:
            p_name = r.get("player", first_player or "Unknown")
            opp    = r.get("opponent", first_opponent or "opponent")
            reasoning = r.get("evaluation", json.dumps(r, indent=2, default=str))

            # v7.9.7 (this session, per operator request): the raw
            # compliance audit result (which specific rule flagged,
            # what violation was found, what was required to fix it)
            # was already correctly attached to the pick dict and
            # already reaches the JSON archive automatically -- but it
            # never appeared anywhere in the human-readable reasoning
            # file, which is what's actually reviewed after the fact.
            # Confirmed as a real gap during tonight's live trace of a
            # 79%->66% compliance-penalty case, where reconstructing
            # WHY the penalty applied required manually re-running the
            # compliance check by hand. Appending it here so future
            # review doesn't require that manual reconstruction.
            c_flags = r.get("compliance_flags", [])
            if r.get("compliance_penalty") and c_flags:
                comp_section = ["\n\n" + "=" * 60,
                                 "COMPLIANCE AUDIT RESULT (attached separately from the",
                                 "model's own evaluation -- this is what the compliance",
                                 "auditor found, not the evaluator's self-assessment)",
                                 "=" * 60]
                for flag in c_flags:
                    comp_section.append(f"\nRule: {flag.get('rule', 'unknown')}")
                    comp_section.append(f"Violation: {flag.get('violation', '')}")
                    comp_section.append(f"Required: {flag.get('required', '')}")
                reasoning = reasoning + "\n".join(comp_section)

            save_evaluation_reasoning(p_name, opp, "props", reasoning, dates,
                                      pick_tag="%s_%s" % (r.get("stat_type") or "", r.get("line") or ""))
    else:
        send_discord("general",   # APEX-PICKSCLEAN-2026-10-24: was #picks
            f"No props passed the pipeline. Check #systems-channel for details.")

    # Summary + skips to #systems-channel
    summary_lines = [
        f"Props Summary -- {dates['ct_time']}",
        f"Player: {first_player or 'Unknown'} | "
        f"Tournament: {props[0].get('tournament','?').title() if props else '?'} | "
        f"Surface: {props[0].get('surface','?').title() if props else '?'}",
        f"Evaluated: {len(results)} picks | Skipped: {len(skipped_tags)}",
        "",
    ]
    for r in results:
        stat   = r.get("stat_type", "?")
        direct = r.get("direction", "?")
        conf   = r.get("confidence", 0)
        color  = r.get("color", "?")
        reason = r.get("justification_one_sentence", "")
        summary_lines.append(
            f"{stat} {direct} -- {conf:.0%} {color}"
            + (f"\n   {reason}" if reason else "")
        )
    if skipped_tags:
        summary_lines.append("")
        summary_lines.extend(skipped_tags)
    summary_lines.append("\nFull reasoning saved to picks archive.")
    send_discord("system", "\n".join(summary_lines))

    # ── COMPLETION MESSAGE TO #general ──────────────────────────
    # Confirms back in the same channel the screenshot/prop was sent
    # to, so it's clear the run actually finished rather than leaving
    # the person watching a channel that never gets a final word.
    send_discord("general",
        f"✅ Evaluation complete — {len(results)} graded, "
        f"{len(skipped_tags)} skipped. Cards above; details in #systems-channel."
    )

# ================================================================
# GITHUB INTEGRATION
# ================================================================

GITHUB_CONFIG = {}

def load_github_config():
    global GITHUB_CONFIG
    try:
        with open("/path/to/home/openclaw/config.json") as f:
            cfg = json.load(f)
            GITHUB_CONFIG = cfg.get("github", {})
    except:
        log("GitHub", "config.json not found — GitHub disabled")

def github_api(method, endpoint, data=None):
    token    = GITHUB_CONFIG.get("token", "")
    username = GITHUB_CONFIG.get("username", "")
    repo     = GITHUB_CONFIG.get("repo", "openclaw")
    if not token or not username or "YOUR_" in token:
        return None
    url     = f"https://api.github.com/repos/{username}/{repo}{endpoint}"
    headers = {"Authorization": f"token {token}",
               "Accept":        "application/vnd.github.v3+json"}
    try:
        if   method == "GET":   resp = requests.get(url, headers=headers, timeout=15)
        elif method == "PUT":   resp = requests.put(url, headers=headers, json=data, timeout=15)
        elif method == "POST":  resp = requests.post(url, headers=headers, json=data, timeout=15)
        elif method == "PATCH": resp = requests.patch(url, headers=headers, json=data, timeout=15)
        return resp.json() if resp.status_code in (200,201,204) else None
    except Exception as e:
        log("GitHub", f"API error: {e}")
        return None

def github_read_file(filepath):
    result = github_api("GET", f"/contents/{filepath}")
    if result and "content" in result:
        return base64.b64decode(result["content"]).decode("utf-8"), result.get("sha","")
    return None, None

def github_write_file(filepath, content, commit_message, sha=None):
    encoded = base64.b64encode(content.encode()).decode()
    data    = {"message": commit_message, "content": encoded,
               "branch":  GITHUB_CONFIG.get("branch","main")}
    if sha:
        data["sha"] = sha
    return github_api("PUT", f"/contents/{filepath}", data) is not None

def propose_change(file_path, new_content, reason, proposed_by="learning_agent"):
    base_branch = GITHUB_CONFIG.get("branch","main")
    branch_name = f"propose-{file_path.replace('/','_').replace('.','')}-{int(time.time())}"
    ct_time     = datetime.now(CT).strftime("%I:%M %p CT").lstrip("0")

    # Create branch from main
    main_ref = github_api("GET", f"/git/ref/heads/{base_branch}")
    if not main_ref:
        log("GitHub", "Cannot create PR — no main ref found")
        return None
    sha = main_ref["object"]["sha"]
    github_api("POST", "/git/refs",
               {"ref": f"refs/heads/{branch_name}", "sha": sha})

    # Write file to branch
    _old_content, old_sha = github_read_file(file_path)
    encoded = base64.b64encode(new_content.encode()).decode()
    data    = {"message": f"Proposed: {reason}",
               "content": encoded, "branch": branch_name}
    if old_sha:
        data["sha"] = old_sha
    github_api("PUT", f"/contents/{file_path}", data)

    # Create PR
    pr = github_api("POST", "/pulls",
                    {"title": f"Proposed: {file_path}",
                     "body":  f"**By:** {proposed_by}\n**Reason:** {reason}",
                     "head":  branch_name,
                     "base":  base_branch})

    pr_url = pr.get("html_url","") if pr else ""

    if pr_url:
        send_discord("learning",
            f"⚙️ **CHANGE PROPOSED** — {ct_time}\n"
            f"File: `{file_path}`\n"
            f"Proposed by: {proposed_by}\n"
            f"Reason: {reason}\n\n"
            f"🔍 **Review:** {pr_url}\n\n"
            f"Reply `APPROVE` to merge\n"
            f"Reply `REJECT` to close\n"
            f"Auto-rejects in 48 hours if no response"
        )
    return pr_url

def write_session_handoff(context, dates, picks=None, open_questions=None):
    picks_section = "None — pipeline not yet run today"
    if picks:
        picks_section = "\n".join([
            f"- {p.get('player')} {p.get('direction')} "
            f"{p.get('line')} {p.get('stat_type')} "
            f"{p.get('emoji','?')} {p.get('confidence',0):.0%}"
            for p in picks
        ])

    content = f"""# SESSION HANDOFF — {dates['pipeline_run_date']} {dates['ct_time']}

## System Status
OpenClaw v7.1 — active
Pipeline: {context.get('pipeline_status','ready')}
Last pipeline run: {context.get('last_run','not yet today')}

## What Was Happening This Session
{context.get('session_summary','General operation')}

## Active Picks (for {dates['match_date']})
{picks_section}

## Open Questions
{chr(10).join(['- '+q for q in (open_questions or ['None'])]) }

## Pending GitHub PRs
{context.get('pending_prs','None')}

## Next Session Should
{context.get('next_session_todo','- Check #learning for approvals')}

## Notes
{context.get('notes','None')}
"""
    _, sha = github_read_file("SESSION_HANDOFF.md")
    success = github_write_file("SESSION_HANDOFF.md", content,
                                f"Handoff — {dates['pipeline_run_date']} {dates['ct_time']}", sha)
    if success:
        send_discord("system",
            f"📋 **Session handoff saved** — {dates['ct_time']}\n"
            f"github.com/{GITHUB_CONFIG.get('username','')}/openclaw/blob/main/SESSION_HANDOFF.md"
        )

# ================================================================
# CORE PIPELINE PROCESSOR
# ================================================================

# ================================================================
# GATE 11-B — FORMAT DETECTION (bo3 vs bo5)
# DEMETRIUS v7.9.6 — LOCKED June 29, 2026
# ================================================================

GRAND_SLAM_NAMES = {
    "australian open", "roland garros", "french open",
    "wimbledon", "us open"
}

BO5_LESS_CAP  = 0.64   # LESS on bo5 without HARD dominance → max 64%
BO5_MORE_NOTE = "Best-of-5 MORE: single dropped set clears most lines. Verify 3-0 blowout not projected."

def gate_11b_format_detection(prop: dict) -> dict:
    """
    Runs after Gate 11 pass, before research agent.
    Locks match format (bo3 or bo5) into prop.
    bo5 = ATP men's singles Grand Slam main draw ONLY.
    Everything else = bo3.
    """
    tournament = prop.get("tournament", "").lower().strip()
    tour       = prop.get("tour", "atp").lower().strip()
    rnd        = prop.get("round", "").lower().strip()
    player     = prop.get("player", "")
    opponent   = prop.get("opponent", "")

    # APEX-SLAM-001: strip periods before matching -- PrizePicks says "U.S. Open",
    # the name table says "us open"; on 08-29 that gap + an exhausted odds cap
    # locked five Bo5 main-draw matches as Bo3.
    _t_norm    = tournament.replace(".", "")
    is_slam    = any(slam in _t_norm for slam in GRAND_SLAM_NAMES)
    is_atp     = tour == "atp"
    is_qual    = any(q in rnd for q in ["qualifying", "qual", "q1", "q2", "q3"])
    is_doubles = "/" in player or "/" in opponent

    # ── APEX-CONTEXT-2026-08-24: let the market decide the format. Pinnacle's
    # main total-games line is ~18-23 for a best-of-3 and ~36-40 for a best-of-5.
    # This needs no round label, which is what the slam rule below was missing:
    # is_qual reads prop["round"], and nothing upstream sets it, so slam
    # qualifying locked Bo5 (08-24, Basavareddy/Kouame). Falls through to the
    # old rule when no line is available.
    _mt, _fh, _ft = None, None, None
    if not is_doubles:
        try:
            import apex_price_feed as _apf
            _fc = _apf.fixture_context(player, opponent)
            if _fc:
                _mt, _fh, _ft = _fc.get("main_total"), _fc.get("format_hint"), _fc.get("tournament")
                _fb = _fc.get("format_basis")   # APEX-FMT-001: the feed's REAL basis
                prop["apex_fixture_tournament"] = _ft
                prop["apex_main_total"] = _mt
        except Exception as _e:
            log("Gate11B", f"fixture context unavailable (non-fatal): {_e}")
    # APEX-FMT-002-2026-09-06: no usable fixture -> ask source 1 (The Odds API) which
    # event it prices this pair under. Its sport_key names the tournament directly
    # (tennis_atp_us_open); a slam key on an ATP pair is Bo5. This runs BEFORE the
    # recent-match inference below, which on 09-06 resolved US Open R16 pairs to
    # their last meeting (cincinnati / barcelona) and locked Bo3 against Bo5 lines.
    _fb = locals().get("_fb")
    if _fh not in ("bo3", "bo5") and not is_doubles:
        try:
            import apex_price_feed as _apf2
            _pf2 = _apf2.get_price(player, opponent) or {}
            _sk = str(_pf2.get("sport_key") or "")
            if _sk:
                _slam_key = any(s in _sk for s in ("us_open", "aus_open", "french_open", "wimbledon"))
                _title = str(_pf2.get("sport_title") or _sk)
                if _slam_key and "atp" in _sk and not is_qual:
                    _fh, _ft, _fb = "bo5", _title, f"Odds API event key {_sk} (slam, ATP main draw)"
                else:
                    _fh, _ft, _fb = "bo3", _title, f"Odds API event key {_sk} ({'WTA' if 'wta' in _sk else 'non-slam'})"
                log("Gate11B", f"format from Odds API event {_sk} -> {_fh} (fixture list had no entry)")
        except Exception as _e:
            log("Gate11B", f"Odds API event lookup unavailable (non-fatal): {_e}")
    if _fh in ("bo3", "bo5"):
        _n = 5 if _fh == "bo5" else 3
        _why = _fb or (f"Pinnacle total-games line {_mt}" if _mt is not None
                       else "no stated basis")
        # APEX-FMT-001: a bo3 verdict on an ATP slam fixture with no market line is
        # exactly the shape of the 08-29 failure -- keep the verdict (calendar owns
        # qualifying) but say it LOUDLY so a stale calendar can never hide again.
        if _fh == "bo3" and is_slam and is_atp and _mt is None:
            log("Gate11B", f"⚠️ FORMAT CONFLICT WATCH: bo3 on ATP slam fixture "
                           f"{player} vs {opponent} with NO market line (basis: {_why}) "
                           f"-- verify calendar if this is a main-draw day")
        prop["match_format"]  = _fh
        prop["format_locked"] = True
        prop["format_label"]  = f"Best-of-{_n} confirmed — {_ft or prop.get('tournament','')} ({_why})"
        prop["format_warning"] = (
            "⚠️ BEST-OF-5 MATCH. Player recent averages from Challenger/250/WTA "
            "are Best-of-3 and NOT a valid baseline for this line. "
            "All score paths must use Best-of-5 structure. "
            "TG LESS requires confirmed straight-sets (3-0) projection to hold HARD dominance."
        ) if _fh == "bo5" else None
        log("Gate11B", f"Format locked from market: BEST-OF-{_n} — {_ft} ({_why})")
        return prop

    if is_slam and is_atp and not is_qual and not is_doubles:
        prop["match_format"]  = "bo5"
        prop["format_locked"] = True
        prop["format_label"]  = (
            f"Best-of-5 confirmed — {prop.get('tournament','')} ATP Men's Singles Main Draw"
        )
        prop["format_warning"] = (
            "⚠️ BEST-OF-5 MATCH. Player recent averages from Challenger/250/WTA "
            "are Best-of-3 and NOT a valid baseline for this line. "
            "All score paths must use Best-of-5 structure. "
            "TG LESS requires confirmed straight-sets (3-0) projection to hold HARD dominance."
        )
        log("Gate11B", f"Format locked: BEST-OF-5 — {prop.get('tournament','')}")
    else:
        prop["match_format"]  = "bo3"
        prop["format_locked"] = True
        prop["format_label"]  = (
            f"Best-of-3 confirmed — {prop.get('tournament','')} "
            f"{tour.upper()} {'Qualifying' if is_qual else 'Main Draw'}"
        )
        prop["format_warning"] = None
        log("Gate11B", f"Format locked: BEST-OF-3 — {prop.get('tournament','')}")

    return prop


def gate_11b_scoring_correction(prop: dict, scored: dict) -> dict:
    """
    Runs inside scoring_agent after all caps, before final output.
    Auto-corrects confidence on bo5 LESS picks without HARD dominance.
    Attaches correction_line to scored dict for Discord output.
    """
    fmt        = prop.get("match_format", "bo3")
    stat_type  = prop.get("stat_type", "")
    direction  = scored.get("direction", "")
    confidence = scored.get("confidence", 0.0)
    # Category 1 Finding 1 fix (this session, approved): props write
    # dominance to "dominance_level" (set by evaluation_agent); the
    # key "dominance" is only ever written by the moneyline
    # evaluator's return dicts. Reading "dominance" here meant every
    # props pick fell through to the default "EDGE" -- so every bo5
    # TG/GW LESS pick was capped at BO5_LESS_CAP regardless of its
    # real dominance, including confirmed-HARD picks that are the
    # only GREEN-eligible LESS picks. Authoritative key first,
    # legacy key as fallback, default unchanged.
    dominance  = prop.get("dominance_level", prop.get("dominance", "EDGE"))

    scored["format_corrected"] = False
    scored["correction_line"]  = None

    if fmt != "bo5":
        return scored

    if stat_type not in ["total_games", "games_won"]:
        return scored

    original = confidence
    corrected = False
    reason    = ""

    if direction == "LESS" and dominance != "HARD":
        if confidence > BO5_LESS_CAP:
            confidence = BO5_LESS_CAP
            corrected  = True
            reason     = (
                f"Best-of-5 LESS requires confirmed 3-0 projection (HARD dominance). "
                f"{dominance} dominance caps at {int(BO5_LESS_CAP*100)}%."
            )

    if direction == "MORE":
        scored["format_note"] = BO5_MORE_NOTE

    if corrected:
        old_pct = int(original * 100)
        new_pct = int(confidence * 100)

        def _grade(pct):
            if pct >= 70: return f"🟢 {pct}%"
            if pct >= 60: return f"🟡 {pct}%"
            return f"🔴 {pct}%"

        old_grade = _grade(old_pct)
        new_grade = _grade(new_pct)

        correction_line = (
            f"⚠️ **Format Correction: Best-of-5** "
            f"({prop.get('tournament','Grand Slam')} ATP Men's) — "
            f"Grade revised from {old_grade} → {new_grade}. {reason}"
        )

        scored["confidence"]       = confidence
        scored["format_corrected"] = True
        scored["correction_line"]  = correction_line
        scored["old_grade"]        = old_grade
        scored["new_grade"]        = new_grade

        log("Gate11B", f"FORMAT CORRECTION: {old_pct}% → {new_pct}% | {reason}")

        try:
            send_discord("alerts",
                f"🎾 **GATE 11-B FORMAT CORRECTION**\n"
                f"Match: {prop.get('player')} vs {prop.get('opponent')}\n"
                f"Tournament: {prop.get('tournament')} (Best-of-5 ATP)\n"
                f"Prop: {prop.get('line')} {stat_type.upper()} {direction}\n"
                f"Grade revised: {old_grade} → {new_grade}\n"
                f"Reason: {reason}"
            )
        except Exception as _e:
            log("Gate11B", f"Discord alert failed (non-fatal): {_e}")

    return scored

# ================================================================
def process_single_prop(prop, dates, weights):
    pipeline_flags     = []
    compliance_penalty = False
    compliance_flags   = []
    eval_attempts      = 0

    # Dataset A (Priority 1, approved) — eval_id is minted here, upstream
    # of Gate 11, and is this evaluation's permanent identity through
    # every downstream stage. Deliberately NOT minted inside
    # gate_11_check: Gate 11 stays a pure verification function, Dataset A
    # stays a pure observation system (Decision 1, Option B).
    eval_id = d1.new_eval_id()
    prop["eval_id"] = eval_id
    d1.start(eval_id, "PROP", prop.get("player", ""), prop.get("opponent", ""),
             tour=prop.get("tour"), tournament=prop.get("tournament"),
             round_=prop.get("round"), surface=prop.get("surface"),
             match_scheduled_time=prop.get("match_date"),
             market=prop.get("stat_type"), line=prop.get("line"),
             pipeline_version=CORE_VERSION,
             _logger=log)

    # Gate 11
    gate_result = gate_11_check(prop, dates)
    if gate_result["status"] == "REJECTED":
        d1.terminate(eval_id, "GATE11_REJECTED", _logger=log)
        # APEX-DECLINETRANSPARENCY-2026-10-01: this was the one decline
        # path in the whole engine that returned None WITHOUT stamping
        # apex_decline_reason -- every Gate 11 rejection (a prop whose
        # match/player Gate 11 could not verify) fell silently into the
        # catch-all "missing data" bucket in the /fetch decline breakdown
        # with no way to tell it apart from a genuine data problem. Gate
        # 11 failing to verify a match/player is, in substance, the same
        # "unknown player" situation as the other decline path that
        # already reads that way below -- stamped the same way so the
        # breakdown counts it correctly instead of losing it.
        _apex_decline(prop, f"declined: Gate 11 rejected -- unknown player/match "
                            f"({gate_result.get('reason', 'not verified')}) -> legacy")
        return None
    if gate_result.get("flag"):
        pipeline_flags.append(gate_result["flag"])
    # Category 9 Finding 2 (approved): Gate 11 is documented as
    # MANDATORY, but on V8 outage it returned PASSED/verified:False
    # with an alert and NO downstream consequence -- the pick
    # proceeded at full confidence, GREEN-eligible, on a match whose
    # existence was never verified. Authoritative design determination:
    # fail-closed (REJECTED) already governs a FAILED check (match not
    # found); an UNAVAILABLE check falls under the permanent standing
    # rule "unavailable = state it, cap all YELLOW". This flag carries
    # that state into scoring, where a hard 69% cap enforces it.
    if gate_result.get("verified") is False:
        prop["gate11_unverified"] = True
        d1.transition(eval_id, "RESEARCH_STARTED",
                       gate11_status="gate11_unverified", _logger=log)

    # Gate 11-B — Format Detection (bo3 vs bo5)
    prop = gate_11b_format_detection(prop)
    d1.transition(eval_id, "RESEARCH_STARTED",
                   gate11b_dominance_key=prop.get("dominance_level") or prop.get("dominance"),
                   _logger=log)

    # APEX-ENGINE-PRIMARY-2026-08-23 -- frozen engine first, legacy fallback.
    # Runs AFTER Gate 11 and Gate 11-B (both preserved) and BEFORE the LLM
    # chain. On success this returns immediately: no research_agent, no
    # evaluation_agent, no compliance_agent, no scoring_agent. On ANY failure
    # it returns None and everything below runs exactly as it did before.
    _apex_pick = _apex_engine_pick(prop, dates, eval_id)
    if _apex_pick:
        return _apex_pick

    # ── APEX-ENGINE-ONLY-2026-08-24: legacy is OFF. The engine said no; say why,
    # write it down as a defect, and stop. (env APEX_ENGINE_ONLY=0 re-enables legacy.)
    if APEX_ENGINE_ONLY:
        # APEX-HOTFIX-2026-09-28 FIX #5 guardrail: apex_decline_reason
        # should always be stamped by now (every decline path in
        # _apex_engine_pick calls _apex_decline()). If it is EVER still
        # unset here, that itself is a defect worth finding immediately --
        # flag it distinctly and loudly rather than silently reusing the
        # same generic text a real "no reason recorded" case would show,
        # so a future gap like this one can't hide again.
        _decline_reason_raw = prop.get("apex_decline_reason")
        if not _decline_reason_raw:
            log("ApexEngine", "GUARDRAIL: _apex_engine_pick declined without "
                               "stamping apex_decline_reason -- a decline path is "
                               "missing its _apex_decline() call. This needs a code fix.")
        _reason = str(_decline_reason_raw or
                      "UNDECLARED DECLINE (guardrail fired — a decline path is "
                      "missing its _apex_decline() call; see log for GUARDRAIL line)")
        _p, _o = prop.get("player", ""), prop.get("opponent", "")
        _st, _ln = prop.get("stat_type", ""), prop.get("line", "")
        log("ApexEngine", f"ENGINE-ONLY: no legacy for {_p} {_st} {_ln} — {_reason}")
        _apex_record_decline(prop, _reason, eval_id)
        try:
            send_discord("alerts",
                f"⛔ **ENGINE DECLINED — no pick** — {_p} vs {_o}\n"
                f"`{_ln} {_st}`\n"
                f"Reason: {_reason}\n"
                f"Legacy is off. If Pinnacle has not priced this match yet, resend once the line is up.")
        except Exception:
            pass
        try:
            d1.terminate(eval_id, "ENGINE_DECLINED", _reason, _logger=log)
        except Exception:
            pass
        return None

    # Research
    d1.heartbeat("research", "WORKING", current_task=prop.get("player"), _logger=log)
    try:
        prop = research_agent(prop, dates)
    except Exception as _re:
        import traceback
        log("Research", f"CRASH: {_re}")
        traceback.print_exc()
        d1.terminate(eval_id, "RESEARCH_FAILURE", str(_re), _logger=log)
        d1.heartbeat("research", "FAILED", detail=str(_re), increment_failure=True, _logger=log)
        return None
    if prop["research_data"].get("has_gaps"):
        pipeline_flags.append("Research incomplete")
    # Rule 24 / Correction 24 capture (approved, this session): same
    # deterministic rank-gap check build_evaluation_content() already
    # runs to decide prompt injection (line ~3060, `_gap >= 100`),
    # recomputed independently here rather than modifying prompt-
    # construction code. Verified data source before writing this,
    # per operator's "structured truth, not approximate truth" standard.
    _rd24 = prop.get("research_data", {})
    try:
        _p24 = int(str(_rd24.get("player", {}).get("ranking", "0")).replace("#", "").strip() or "0")
        _o24 = int(str(_rd24.get("opponent", {}).get("ranking", "0")).replace("#", "").strip() or "0")
        _gap24 = abs(_p24 - _o24) if _p24 and _o24 else 0
    except Exception:
        _gap24 = 0
    d1.transition(eval_id, "RESEARCH_COMPLETE",
                   classification=prop.get("classification"),
                   rule24_triggered=int(_gap24 >= 100),
                   _logger=log)
    d1.heartbeat("research", "IDLE", increment_success=True, _logger=log)

    # Format verification for Grand Slam props
    if prop.get("is_grand_slam"):
        last5 = prop["research_data"].get("player", {}).get("recent_stat_results", [])
        if last5:
            _verified, contaminated = format_verification_check(
                prop, [{"value": v} for v in last5], dates
            )
            if contaminated:
                pipeline_flags.append("Format contamination — best-of-3 data in Grand Slam line")

    # Evaluation + Compliance loop
    evaluated = None
    for attempt in range(1, 3):
        eval_attempts += 1
        d1.heartbeat("evaluation", "WORKING", current_task=prop.get("player"), _logger=log)
        evaluated      = evaluation_agent(prop)
        if not evaluated:
            break

        # Fix (this session, operator correction): truncation is a
        # structural failure -- same prompt, same model, same
        # max_tokens budget every time -- not a transient one like a
        # network blip. Retrying doesn't address the cause, it just
        # spends a second API call to likely rediscover the same
        # problem while hiding it from view for one extra round-trip.
        # Fails immediately on ANY truncated attempt, with full
        # diagnostics from evaluation_agent (prompt size, output
        # tokens actually generated, exact stop_reason) so the real
        # cause is visible right away instead of after two attempts.
        if evaluated.get("_truncated"):
            diag = evaluated.get("_truncation_diagnostics", {})
            log("Evaluation", f"SYSTEM FAILURE — {evaluated.get('player')} "
                f"{prop.get('stat_type')} truncated on attempt {attempt}. "
                f"No evaluation was completed. No pick generated. "
                f"Diagnostics: {diag}")
            send_discord("alerts",
                f"🛑 **SYSTEM FAILURE — NO PICK GENERATED** — "
                f"{evaluated.get('player')} {evaluated.get('line')} {prop.get('stat_type')}\n"
                f"CAUSE: Evaluation hit max_tokens before reaching a conclusion "
                f"(stop_reason: {diag.get('stop_reason', '?')}).\n"
                f"DIAGNOSTICS — prompt: {diag.get('total_payload_chars', '?')} chars "
                f"({diag.get('master_prompt_chars', '?')} master + "
                f"{diag.get('content_chars', '?')} content) | "
                f"output tokens used: {diag.get('output_tokens_used', '?')} of "
                f"{diag.get('max_tokens_limit', '?')} limit.\n"
                f"This is not a low-confidence pick -- no valid evaluation was "
                f"ever produced. No retry attempted (structural failure, not "
                f"transient). Investigate whether the token budget or the "
                f"prompt's output requirements need adjustment."
            )
            d1.terminate(eval_id, "EVAL_TRUNCATED", str(diag), _logger=log)
            d1.heartbeat("evaluation", "FAILED", detail="truncated", increment_failure=True, _logger=log)
            return None

        d1.heartbeat("compliance", "WORKING", current_task=prop.get("player"), _logger=log)
        audit = compliance_agent(evaluated, attempt)
        if audit["status"] == "APPROVED":
            if attempt == 2:
                with _usage_lock:
                    _retry_stats["retries_cleared"] += 1
            break
        if audit["status"] == "ACCEPTED_WITH_PENALTY":
            compliance_penalty = True
            compliance_flags   = audit.get("flags", [])
            if attempt == 2:
                with _usage_lock:
                    _retry_stats["retries_still_flagged"] += 1
            break
        if audit["status"] == "FLAGGED_FOR_MANUAL_REVIEW":
            # Architectural fix (this session, operator correction):
            # same principle as the truncation case above -- the
            # compliance audit itself failing to run (API/parse error)
            # means no real judgment exists for this pick, same as a
            # truncated evaluation. Previously this produced a scored
            # pick capped at 62%; now it produces no pick at all, since
            # a capped number still implies an evaluation happened.
            log("Evaluation", f"SYSTEM FAILURE — {evaluated.get('player')} "
                f"{prop.get('stat_type')}: compliance audit could not be "
                f"parsed/run. No pick generated.")
            send_discord("alerts",
                f"🛑 **SYSTEM FAILURE — NO PICK GENERATED** — "
                f"{evaluated.get('player')} {evaluated.get('line')} {prop.get('stat_type')}\n"
                f"CAUSE: Compliance audit could not be parsed/run (API or parse error).\n"
                f"This is not a low-confidence pick -- no compliance judgment "
                f"was ever produced. Skipped, not graded."
            )
            d1.terminate(eval_id, "COMPLIANCE_UNPARSEABLE", _logger=log)
            d1.heartbeat("compliance", "FAILED", detail="unparseable", increment_failure=True, _logger=log)
            return None
        if audit["status"] == "FLAGGED" and attempt == 1:
            # Cost fix (this session, approved): the retry used to fire
            # on ANY flag -- including Category-B wording-only flags
            # that cost zero confidence -- spending a full second
            # Sonnet evaluation to fix a missing template sentence.
            # Per the Category model: only Category-A (real reasoning
            # failure) justifies re-running the hardest call in the
            # pipeline. B-only = accept with the note, no retry, no
            # penalty (scoring_agent computes 0 deduction from B-only
            # flag sets and keeps floor eligibility).
            _a_retry_flags = [f for f in (audit.get("flags") or [])
                              if f.get("category", "A") != "B"]
            if not _a_retry_flags:
                compliance_penalty = True
                compliance_flags   = audit.get("flags", [])
                with _usage_lock:
                    _retry_stats["b_only_no_retry"] += 1
                log("Evaluation", f"Compliance FLAGGED with Category-B flags only — "
                    f"accepted with note, NO re-evaluation (Sonnet retry saved)")
                break
            prop["compliance_flags"] = audit.get("flags", [])
            prop["is_re_evaluation"] = True
            with _usage_lock:
                _retry_stats["retries_attempted"] += 1
        else:
            compliance_penalty = True
            # Bug fix (this session, caught during this same change):
            # this branch (attempt-2 still FLAGGED) set the penalty
            # boolean WITHOUT forwarding the flags -- scoring_agent
            # then saw compliance_penalty=True with an empty flag list.
            # Under the old code that silently took the max -15%; under
            # the new Category-C reclassification it would have
            # incorrectly killed the pick as a malformed audit. The
            # audit here is NOT malformed -- real flags exist right in
            # audit["flags"] -- they just were never passed along.
            compliance_flags = audit.get("flags", [])
            with _usage_lock:
                _retry_stats["retries_still_flagged"] += 1
            break

    if not evaluated:
        d1.terminate(eval_id, "EVAL_ATTEMPTS_EXHAUSTED", _logger=log)
        d1.heartbeat("evaluation", "FAILED", detail="attempts exhausted", increment_failure=True, _logger=log)
        return None

    # Calibration tracking (this session): Rule 9, Gate 14 (props), and
    # CFEF are now real compliance checks (checklist items 15-17) --
    # this is what lets their real hit rate eventually be measured, the
    # same way RBMD/LPAB_PSAR/Gate14-ML already are. A rule "fires" here
    # if the auditor names it in EITHER checks_passed (checked, found
    # compliant) or flags (checked, violation found) -- same "was it
    # genuinely considered" philosophy as the citation-based rules
    # above, not "did it pass." Deduped per evaluation via set() so a
    # rule appearing in both lists (shouldn't happen, but not assumed)
    # still only counts once.
    _audit_rule_names = set(audit.get("checks_passed") or []) | \
                         {f.get("rule") for f in (audit.get("flags") or []) if f.get("rule")}
    _compliance_check_to_registry = {
        "Rule9": "Rule9_MarketExclusion",
        "Gate14": "Gate14_Props_Procedural",
        "CFEF": "CFEF_Trigger6",
        "Rule30_CFEF_Stacking": "Rule30_CFEF_Stacking",
    }
    for _cited_name, _registry_key in _compliance_check_to_registry.items():
        if _cited_name in _audit_rule_names:
            record_provisional_rule_fire(_registry_key, eval_id, player=evaluated.get("player"))

    # Evaluation + compliance both concluded (whichever break path was hit
    # above) — record both transitions before scoring. Structured fields
    # only; reasoning_text stays supplemental per the approved design.
    # Bug fix (this session, found during a full-system alignment audit):
    # three of these five fields were reading keys that don't exist
    # anywhere in `evaluated` -- confirmed by grepping every assignment
    # into this dict. dominance_level/req_count/reasoning_text have been
    # silently capturing None on every single evaluation since this
    # transition was added; nothing downstream ever corrected them
    # (SCORING_COMPLETE never touches these three fields). The LIVE
    # pick was never affected -- scoring_agent() reads the correct keys
    # (evaluated.get("dominance_level"), evaluated.get("competitive_reqs"))
    # everywhere it actually scores -- this only corrupted the historical
    # record Rule Command Center's hit-rate-by-dominance and
    # hit-rate-by-req-count analysis depends on.
    # Second bug found in the same audit: gate11b_dominance_key is ONLY
    # ever written once, at the earlier RESEARCH_STARTED transition --
    # before research_agent() or evaluation_agent() have even run. At
    # that point prop has no "dominance_level"/"dominance" key at all
    # (gate_11b_format_detection() only sets match_format/format_label/
    # format_warning -- confirmed by reading its full body), so that
    # earlier capture was structurally guaranteed to be None every
    # time, for every evaluation. Nothing ever corrected it afterward.
    # Rule Command Center's "Gate 11-B" row (CAPTURED_RULES, presented
    # with no caveat) has been silently reading null for this column
    # since it was added. Writing the real value here, now that
    # evaluation has actually run and dominance_level is known -- this
    # transition already UPDATEs the same eval_id row, so the earlier
    # null gets correctly overwritten rather than needing to touch the
    # premature capture point itself.
    # Fifth bug found in the same audit (systematic sweep of all 31
    # Dataset A capture call sites, this session): RESEARCH_COMPLETE's
    # `classification=prop.get("classification")` was ALSO structurally
    # guaranteed null -- both because "classification" is never the
    # real key (the LOPSIDED/COMPETITIVE/HIGH_VARIANCE read is stored as
    # evaluated["match_type"], set by _extract_match_type(), confirmed
    # by grep -- "classification" as a literal dict key is never
    # assigned anywhere in this file) AND because RESEARCH_COMPLETE
    # fires before evaluation_agent() has even run, before that read
    # could exist under ANY key. Same two-part failure as
    # gate11b_dominance_key above; same fix shape -- write the real
    # value here, where match_type is actually known, rather than at
    # the premature capture point.
    d1.transition(eval_id, "EVALUATION_COMPLETE",
                   direction=evaluated.get("direction"),
                   dominance_level=evaluated.get("dominance_level") or prop.get("dominance_level"),
                   gate11b_dominance_key=evaluated.get("dominance_level"),
                   classification=evaluated.get("match_type"),
                   eval_confidence=evaluated.get("eval_confidence"),
                   req_count=evaluated.get("competitive_reqs"),
                   reasoning_text=evaluated.get("evaluation_output"),
                   _logger=log)
    d1.heartbeat("evaluation", "IDLE", increment_success=True, _logger=log)
    # Truth Lock capture (approved, this session): compliance_agent
    # already tags violations with the exact rule name "Rule_TruthLock"
    # (confirmed at line ~3610) -- this reads that structured field
    # rather than re-deriving anything.
    _truth_lock = "FLAGGED" if any(
        "truthlock" in str(f.get("rule", "")).lower() for f in (compliance_flags or [])
    ) else "PASS"
    # Rule 30 capture (approved, after full trace report): confirmed via
    # exhaustive trace that Rule 30 is a compliance-audit flag (check #9,
    # "Rule30 rest-day awareness"), NOT the turnaround_hours/short_turnaround
    # calculation in Moneyline Signal S4 -- those are separate, unrelated
    # concepts despite sharing a 48-hour threshold coincidentally. Per the
    # operator's explicit instruction, deriving ONLY from the compliance
    # flag name, nothing else.
    _rule30_triggered = any(f.get("rule") == "Rule30" for f in (compliance_flags or []))
    d1.transition(eval_id, "COMPLIANCE_COMPLETE",
                   compliance_category=(
                       "A" if any(f.get("category", "A") == "A" for f in (compliance_flags or []))
                       else ("B" if compliance_flags else None)
                   ),
                   compliance_notes=json.dumps(compliance_flags, default=str) if compliance_flags else None,
                   truth_lock_status=_truth_lock,
                   rule30_triggered=int(_rule30_triggered),
                   _logger=log)
    d1.heartbeat("compliance", "IDLE", increment_success=True, _logger=log)

    scored = scoring_agent(evaluated, weights, compliance_penalty, compliance_flags,
                            eval_id=eval_id)
    # Bug fix (this session, caught during BP enforcement work):
    # scoring_agent's no-pick returns (BROKEN_EVAL, AUDIT_MALFORMED,
    # BP_RULE_BLOCKED) carry confidence=None -- and format_pick_card
    # renders f"{conf:.0%}", which raises TypeError on None. That
    # formatting happens in the MAIN thread's pick_lines loop, outside
    # the executor's per-prop try/except, so ONE such dict would have
    # crashed the entire batch post, taking every valid pick down with
    # it. This was latent since BROKEN_EVAL shipped (v7.9.7) and gained
    # two more trigger paths tonight. Alerts for these cases already
    # fired inside scoring_agent -- returning None here is the
    # established "no pick produced" convention, nothing is silent.
    if scored.get("confidence") is None:
        # Already terminated inside scoring_agent (BROKEN_EVAL/AUDIT_MALFORMED/
        # BP_RULE_BLOCKED all call d1.terminate() themselves) — no duplicate
        # capture call here.
        return None
    scored.update({
        "eval_id":             eval_id,
        "pipeline_flags":      pipeline_flags,
        "compliance_flags":    compliance_flags,
        "compliance_penalty":  compliance_penalty,
        "evaluation_attempts": eval_attempts,
        "match_date":          dates["match_date"],
        "pipeline_run_date":   dates["pipeline_run_date"],
    })
    return scored

# ================================================================
# SUPERVISOR
# ================================================================

def _sort_props_evaluation_order(props: list) -> list:
    """
    5B — EVALUATION ORDER LOCK
    Sorts props so Total Games is always evaluated before Total Sets
    when both come from the same match.

    Rule: Total Games is the PRIMARY evaluation.
          Total Sets is DERIVED from Total Games conclusion.
          Never evaluate Total Sets before Total Games on the same match.

    Priority order (lower = evaluated first):
    0 — total_games (primary)
    1 — games_won
    2 — break_points_won
    3 — aces
    4 — double_faults
    5 — total_sets (derived — must come after total_games)
    6 — everything else
    """
    EVAL_ORDER = {
        "total_games":      0,
        "games_won":        1,
        "total_games_won":  1,
        "break_points_won": 2,
        "aces":             3,
        "double_faults":    4,
        "total_sets":       5,
    }

    def _sort_key(prop):
        stat = prop.get("stat_type", "").lower()
        player = prop.get("player", "")
        opponent = prop.get("opponent", "")
        # Group by match (player+opponent pair) then by stat priority
        match_key = tuple(sorted([player, opponent]))
        stat_priority = EVAL_ORDER.get(stat, 6)
        return (match_key, stat_priority)

    sorted_props = sorted(props, key=_sort_key)

    # Log the evaluation order
    if len(sorted_props) > 1:
        order_str = " → ".join(
            f"{p.get('player','?')} {p.get('stat_type','?')}"
            for p in sorted_props
        )
        log("EvalOrder", f"Locked evaluation sequence: {order_str}")

    return sorted_props


def rule_31_correlation_check(scored_props: list) -> list:
    """
    Rule 31 — COMMON FAILURE MODE (master prompt: "If 2+ LESS picks
    share the same failure condition: flag 'if courts play slow today,
    X picks bust simultaneously'").

    Why this can't be a compliance-checklist item like Rule 9/Gate 14/
    CFEF above: compliance_agent() audits ONE evaluation's text in
    isolation -- it has no visibility into any other pick in the same
    batch. Rule 31 is inherently a CROSS-PICK comparison. It has to
    live here, batch-level, after every prop is scored -- the same
    pipeline stage _build_correlation_map()/_apply_correlation_gate()
    already occupies for a different cross-pick concern (TG vs Total
    Sets on the SAME match). This is the first real code for a rule
    that has existed in the master prompt with zero enforcement of any
    kind since it was written.

    Honest scope note: "shared failure condition" (e.g. "courts play
    slow today") is not itself a stored, structured fact anywhere in
    this pipeline -- there's no queryable "court speed" field to
    compare picks against. The deterministic proxy used here: LESS
    picks in the SAME tournament (same courts, same conditions that
    day) on stat types most directly sensitive to rally length/court
    speed (total_games, aces) are exactly the failure mode the rule's
    own example describes. This is a real, defensible, judgment-based
    proxy for the rule's stated intent -- not a literal reproduction
    of it, since the literal thing isn't measurable with data this
    system has. Per the roadmap's own classification ("Impact:
    disclosure only"), this NEVER touches confidence or grade -- it
    only attaches a visible flag and posts a disclosure, the same
    non-scoring role rule_28_agent's own Discord notice plays.
    """
    COURT_SPEED_SENSITIVE_STATS = {"total_games", "aces"}

    by_tournament = {}
    for prop in scored_props:
        if (prop.get("direction") or "").upper() != "LESS":
            continue
        stat = (prop.get("stat_type") or "").lower()
        if stat not in COURT_SPEED_SENSITIVE_STATS:
            continue
        tournament = prop.get("tournament") or "UNKNOWN"
        by_tournament.setdefault(tournament, []).append(prop)

    flagged_groups = []
    for tournament, props in by_tournament.items():
        if len(props) < 2:
            continue
        for p in props:
            p["rule31_flag"] = True
            p["rule31_correlated_with"] = [
                f"{other.get('player')} {other.get('stat_type')} LESS"
                for other in props if other is not p
            ]
        flagged_groups.append((tournament, props))

    if flagged_groups:
        lines = []
        for tournament, props in flagged_groups:
            names = ", ".join(f"{p.get('player')} {p.get('stat_type')} LESS" for p in props)
            lines.append(f"• {tournament}: {names}")
        send_discord("system",
            f"⚠️ **Rule 31 — correlated LESS exposure ({len(flagged_groups)} tournament(s))**\n"
            + "\n".join(lines) +
            "\nIf conditions favor longer points/games today at these events, "
            "these picks are exposed together, not independently. Disclosure "
            "only — confidence and grade are unchanged."
        )
    return scored_props


def _build_correlation_map(scored_props: list) -> dict:
    """
    Builds a correlation map after all props are evaluated.
    Links Total Games results to Total Sets for the same match.
    Feeds the correlation gate in output_agent.

    Returns dict keyed by match_key with TG and TS results.
    """
    correlation = {}
    for prop in scored_props:
        stat = prop.get("stat_type", "").lower()
        player = prop.get("player", "")
        opponent = prop.get("opponent", "")
        match_key = tuple(sorted([player, opponent]))

        if match_key not in correlation:
            correlation[match_key] = {}

        if stat == "total_games":
            correlation[match_key]["total_games"] = {
                "direction":  prop.get("direction"),
                "confidence": prop.get("confidence"),
                "color":      prop.get("color"),
                "player":     player,
                "opponent":   opponent,
            }
        elif stat == "total_sets":
            correlation[match_key]["total_sets"] = {
                "direction":  prop.get("direction"),
                "confidence": prop.get("confidence"),
                "color":      prop.get("color"),
                "player":     player,
                "opponent":   opponent,
            }

    return correlation


def _apply_correlation_gate(scored_props: list, correlation_map: dict) -> list:
    """
    5C — CORRELATION GATE
    After TG is evaluated, derives correlated Total Sets direction.

    Rules:
    - TG GREEN confirmed → Total Sets direction correlated automatically
    - TG LESS GREEN → Total Sets LESS gains correlated support
    - TG MORE GREEN → Total Sets MORE gains correlated support
    - TG YELLOW or RED → Total Sets evaluated independently (no correlation)
    - Documents the correlation explicitly in each prop's output

    Adds 'correlated_from' and 'correlation_note' fields to Total Sets props.
    """
    for prop in scored_props:
        stat = prop.get("stat_type", "").lower()
        if stat != "total_sets":
            continue

        player = prop.get("player", "")
        opponent = prop.get("opponent", "")
        match_key = tuple(sorted([player, opponent]))
        match_data = correlation_map.get(match_key, {})
        tg_data = match_data.get("total_games")

        if not tg_data:
            continue  # No TG evaluation for this match

        tg_direction  = tg_data.get("direction")
        tg_color      = tg_data.get("color")
        ts_direction  = prop.get("direction")

        # Only correlate when TG is GREEN
        if tg_color == "GREEN":
            if tg_direction == ts_direction:
                # Directions agree — correlated support
                note = (
                    f"⚠️ CORRELATED: Total Sets {ts_direction} derives "
                    f"structural support from confirmed Total Games "
                    f"{tg_direction} ({tg_data.get('confidence',0):.0%} GREEN). "
                    f"Both props share the same match thesis."
                )
                prop["correlated_from"] = "total_games"
                prop["correlation_note"] = note
                prop["correlation_agrees"] = True
                log("Correlation",
                    f"TG {tg_direction} GREEN → TS {ts_direction} correlated "
                    f"({player} vs {opponent})")

                # Flag for slip — correlated legs
                prop["slip_correlation_flag"] = (
                    f"⚠️ SLIP ALERT: This Total Sets leg is correlated with "
                    f"Total Games on the same match. Both share the "
                    f"'{ts_direction}' thesis. If the thesis breaks, both "
                    f"legs lose together. Review before combining on same slip."
                )

            else:
                # Directions conflict — flag it
                note = (
                    f"⚠️ CORRELATION CONFLICT: Total Games is {tg_direction} "
                    f"({tg_data.get('confidence',0):.0%} GREEN) but Total Sets "
                    f"is {ts_direction}. These directions contradict each other "
                    f"on the same match. Review both evaluations before placing."
                )
                prop["correlated_from"] = "total_games"
                prop["correlation_note"] = note
                prop["correlation_agrees"] = False
                log("Correlation",
                    f"CONFLICT: TG {tg_direction} GREEN vs TS {ts_direction} "
                    f"({player} vs {opponent}) — flag for review")

                # Send alert
                try:
                    send_discord("alerts",
                        f"⚠️ **CORRELATION CONFLICT**\n"
                        f"Match: {player} vs {opponent}\n"
                        f"Total Games: {tg_direction} "
                        f"({tg_data.get('confidence',0):.0%} GREEN)\n"
                        f"Total Sets: {ts_direction} "
                        f"({prop.get('confidence',0):.0%})\n"
                        f"These directions contradict. Review before placing."
                    )
                except Exception as _e:
                    # Audit fix 2.4 (this session): this alert is a
                    # money-protection feature -- if building/sending it
                    # fails, that failure must be visible, not silent.
                    log("Correlation", f"CONFLICT ALERT FAILED TO SEND "
                        f"({player} vs {opponent}): {_e}")
        else:
            # TG is not GREEN — Total Sets evaluated independently
            prop["correlated_from"] = None
            prop["correlation_note"] = (
                f"Total Games is {tg_color} {tg_direction} "
                f"({tg_data.get('confidence',0):.0%}) — "
                f"below GREEN threshold. "
                f"Total Sets evaluated independently."
            )
            prop["correlation_agrees"] = None

    return scored_props


def supervisor_agent(props, dates, weights, mode="live"):
    start_time = time.time()
    log("Supervisor", f"Pipeline starting — {len(props)} props — {mode.upper()}")
    send_discord("system",
        f"🚀 **Pipeline started** — {dates['ct_time']}\n"
        f"Fetching picks for: **{dates['display_day']}**\n"
        f"Props received: {len(props)}"
    )

    valid_props  = filter_props(props, dates)

    # ── 5B: EVALUATION ORDER LOCK ────────────────────────────────
    # Total Games must always be evaluated before Total Sets.
    # This ensures the correlation gate can fire correctly.
    valid_props = _sort_props_evaluation_order(valid_props)
    # ────────────────────────────────────────────────────────────

    scored_props  = []
    gate_rejected = []

    for prop in valid_props:
        prop.setdefault("tournament_stage",    1)
        prop.setdefault("same_day_3set_count", 0)
        scored = process_single_prop(prop, dates, weights)
        if scored:
            scored_props.append(scored)
        else:
            gate_rejected.append(prop)

    # ── 5C: CORRELATION GATE ─────────────────────────────────────
    # After all props evaluated, link TG → Total Sets conclusions.
    correlation_map = _build_correlation_map(scored_props)
    scored_props    = _apply_correlation_gate(scored_props, correlation_map)
    # Rule 31 (this session, first real enforcement of any kind --
    # see rule_31_correlation_check()'s docstring): a SEPARATE concern
    # from the TG/Total-Sets gate above -- that one is about the SAME
    # match's two markets; this one is about DIFFERENT matches sharing
    # the same-day court conditions. Runs after scoring, before
    # selection, disclosure-only (never touches confidence/grade).
    scored_props    = rule_31_correlation_check(scored_props)
    # ────────────────────────────────────────────────────────────

    ranked   = sorted(scored_props, key=lambda x: x.get("confidence",0), reverse=True)
    selected = rule_28_agent(ranked, dates)
    # APEX-AUTOFULLBOARD-2026-10-08 (owner, approved 2026-10-09): the
    # 5PM run used to build its Apex boards from `selected`, which rule_28
    # has already cut to one prop per player + one per match BEFORE Apex
    # scores anything (28-40 props on Oct 5-8 vs 100-300 on a manual /fetch).
    # The boards apply their own one-per-player/match rule, so they now get
    # the full evaluated slate -- the same pool /fetch publishes from, with
    # the same MORE-only (goblin/demon) LESS exclusion. `selected` (the 5PM
    # picks slip) is unchanged.
    board_pool = [p for p in ranked
                  if not ((p.get("prop_type_classified") or "") in ("demon", "goblin", "red_demon")
                          and (p.get("direction") or "").upper() == "LESS")]
    # APEX-5PMGENERAL-2026-10-16: publish the boards BEFORE the #picks post
    # (republish reads the pick log, not output_agent's work), so the #picks
    # post can show what the boards actually hold.
    _board_summary = None
    _cap0 = _board_status_cap()   # APEX-BOARDSTATUS-HEADER-2026-10-25
    if mode == "live":
        try:
            import apex_edge_tracker as _aet_cap
            _aet_cap.republish_boards(source="auto")   # APEX-PUBLISHPOOL-2026-10-11: one pool for every publish
        except Exception as _e:
            log("BoardCapture", f"5PM capture failed (non-fatal): {type(_e).__name__}: {_e}")
        _board_summary = _published_board_summary()
    output_agent(selected, gate_rejected, dates, mode, board_pool=board_pool, board_summary=_board_summary)
    _archive_picks(selected, dates)

    elapsed = time.time() - start_time
    # APEX-5PMDONE-2026-10-12 (owner, "go" 2026-10-12): say what the run
    # actually published -- picks per board and the top pick -- not just a count.
    _board_lines = []
    try:
        import apex_edge_tracker as _aet_sum
        with open(_aet_sum.PUBLISHED_BOARD_SNAPSHOT) as _fh:
            _snap_boards = (json.load(_fh).get("boards") or {})
        _names = {"top3": "Top 3", "elite6": "Elite 6", "top10": "Top 10", "bestgoblin": "Best Goblin",
                  "bestdemon": "Best Demon", "beststandard": "Best Standard"}
        _counts = []
        for _k, _nm in _names.items():
            _ents = ((_snap_boards.get(_k) or {}).get("entries") or [])[:_aet_sum._PUBLISHED_FLAG_LIMIT.get(_k, 6)]
            _counts.append(f"{_nm} {len(_ents)}")
        _board_lines.append("Published: " + " · ".join(_counts))
        _top = (((_snap_boards.get("top3") or {}).get("entries") or [{}])[0]).get("p")
        if _top:
            _who = _top.get("opponent") if _top.get("player") == "match" else _top.get("player")
            _board_lines.append(f"Top pick: {_who} {_top.get('direction')} {_top.get('line')} "
                                f"{str(_top.get('category')).replace('_', ' ')} ({float(_top.get('prob') or 0):.0%})")
    except Exception as _e:
        _board_lines.append(f"Board summary unavailable ({type(_e).__name__})")
    _held = _DISCORD_FETCH_SUPPRESS["suppressed_alerts"] if _DISCORD_FETCH_SUPPRESS["active"] else 0
    send_discord("system", _board_status_wrap(_board_status(_cap0) if mode == "live" else None,
        f"✅ **Pipeline complete** — {elapsed/60:.1f} min\n"
        f"Picks for {dates['display_day']}: {len(selected)} · props checked: {len(props)}\n"
        + "\n".join(_board_lines) + "\n"
        f"Gate 11 rejected: {len(gate_rejected)}"
        + (f" · {_held} single #alerts held back during the run" if _held else "")
    ))
    # APEX-5PMGENERAL-2026-10-16: the short #general card was taken out on his
    # word ("No I don't like the way it look") pending the /fetch-style post.
    return selected

def _archive_picks(picks, dates):
    """
    v7.9.7 fix (item 11): each archived pick now carries evaluated_line
    (the line this evaluation was actually run against) and
    line_evaluated_at (when). Previously the archive stored only "line"
    with no explicit record of when it was captured — meaning if the
    sportsbook's line moved between screenshot and bet placement, there
    was no way to later detect the evaluation was for a different number
    than what got bet. parse_slip_result now cross-checks this against
    the line actually logged in the slip entry and flags a mismatch
    instead of silently grading against the wrong number.
    """
    archive_file = ARCHIVE_DIR / f"picks_{dates['match_date']}.json"
    stamped = []
    for p in picks:
        entry = {k: v for k, v in p.items() if k != "evaluation_output"}
        entry["evaluated_line"]    = p.get("line")
        entry["line_evaluated_at"] = dates.get("ct_time")

        # v7.9.7 (this session): flatten SPI and ESG out of the nested
        # research_data structure into top-level archive fields. Both
        # were already being preserved in the archive (no data loss),
        # but only buried inside research_data.player/opponent/
        # match_context — making future outcome analysis awkward
        # (pick["research_data"]["player"]["spi"] vs pick["player_spi"]).
        # ESS (ess_score/ess_band/critical_data_missing) already lives
        # at the top level of the pick dict from _compute_evidence_
        # strength_score, so it needs no equivalent change here.
        rd            = p.get("research_data", {}) or {}
        player_data   = rd.get("player", {}) or {}
        opponent_data = rd.get("opponent", {}) or {}
        context       = rd.get("match_context", {}) or {}

        entry["player_spi"]   = player_data.get("spi")
        entry["opponent_spi"] = opponent_data.get("spi")

        entry["expected_service_games"] = context.get("expected_service_games")
        entry["esg_band"]               = context.get("esg_band")
        entry["esg_source"]             = context.get("esg_source")

        stamped.append(entry)
    with open(archive_file, "w") as f:
        json.dump(stamped, f, indent=2, default=str)


def _archive_ml_pick(player: str, opponent: str, final: dict, weights: dict, dates: dict) -> None:
    """
    v7.9.7 (moneyline learning system): persists the numeric signal
    breakdown and reasoning behind a moneyline pick at evaluation time
    — previously this only existed transiently inside
    moneyline_evaluation_agent's return dict and was gone once the
    request completed. Without it, nothing at result time could tell
    whether a loss traced back to bad input data, a reasoning/weighting
    problem, or plain variance — the whole point of the attribution
    system owner asked for. Stores:
      - the raw + weighted contribution of each of the four dominance
        signals (serve/return/clutch/form), so the classifier at
        result time can see exactly what drove the call and by how much
      - which signal was the largest weighted contributor
      - the Phase 2 five-signal breakdown (s1-s5)
      - the model's own reasoning text, trimmed
    One file per match_date, same pattern as props' picks_archive.
    """
    dominance = final.get("dominance", {}) or {}
    breakdown = dominance.get("breakdown", {}) or {}

    # Bug fix (review pass): if compute_ml_dominance_score returned
    # INCOMPLETE_DATA (required fields missing), breakdown is {} and
    # every .get(..., 0) below would silently default to 0 — producing
    # a fake "largest_signal" out of four zeros instead of honestly
    # recording that there was nothing real to attribute. Guard on
    # whether breakdown actually has data before computing either.
    if breakdown:
        weighted = {
            "serve_edge":  round(weights.get("ml_serve_weight",  0.35) * breakdown.get("serve_edge",  0), 3),
            "return_edge": round(weights.get("ml_return_weight", 0.25) * breakdown.get("return_edge", 0), 3),
            "clutch_edge": round(weights.get("ml_clutch_weight", 0.15) * breakdown.get("clutch_edge", 0), 3),
            "form_edge":   round(weights.get("ml_form_weight",   0.25) * breakdown.get("form_edge",   0), 3),
        }
        largest_signal = max(weighted, key=lambda k: abs(weighted[k]))
    else:
        weighted = {}
        largest_signal = None

    entry = {
        "player":            player,
        "opponent":          opponent,
        "eval_id":           final.get("eval_id"),
        "match_date":        dates.get("match_date"),
        "logged_at":         dates.get("ct_time"),
        "status":            final.get("status"),
        "winner":            final.get("winner"),
        "confidence":        final.get("confidence"),
        "signal_agreement":  final.get("signal_agreement"),
        "raw_breakdown":      breakdown,
        "weighted_breakdown": weighted,
        "largest_signal":     largest_signal,
        "phase2_signals":     final.get("signals", {}),
        # v1.4 fix (this session): these four fields were added to
        # evaluate_moneyline's return dict as part of the Moneyline
        # v1.4 aggregation rewrite, but this archive function was
        # built earlier in the same session and was never updated to
        # pull them — confirmed as the actual cause of a real review
        # finding them missing from the archived JSON despite the
        # underlying logic correctly computing them. Adding them here
        # closes that gap.
        "phase2_total":               final.get("phase2_total"),
        "phase2_direction":           final.get("phase2_direction"),
        "phase2_band":                final.get("phase2_band"),
        "phase2_confirmation_status": final.get("phase2_confirmation_status"),
        # Fix (this session, pairs with the itemized moneyline
        # compliance penalty): the archived confidence is the POST-
        # penalty number, but nothing here recorded that a penalty
        # happened or why -- at result time the attribution classifier
        # would see e.g. a 66% pick with no way to know it was born 72%
        # and compliance-penalized. Same class of gap as the Phase 2
        # fields above (return dict grew, archive didn't).
        "compliance_status":          final.get("compliance_status"),
        "compliance_flags":           final.get("compliance_flags", []),
        "compliance_penalty":         final.get("compliance_penalty", False),
        "compliance_category_a_penalty_pts": final.get("compliance_category_a_penalty_pts", 0),
        "compliance_note":            final.get("compliance_note"),
        "model_reasoning":    (final.get("model_output") or "")[:2000],
    }

    archive_file = ML_ARCHIVE_DIR / f"ml_picks_{dates['match_date']}.json"
    if archive_file.exists():
        try:
            with open(archive_file) as f:
                existing = json.load(f)
        except Exception:
            existing = []
    else:
        existing = []
    existing.append(entry)
    with open(archive_file, "w") as f:
        json.dump(existing, f, indent=2, default=str)


# ================================================================
# LEARNING AGENT
# ================================================================

# v7.9.6 (Step 5E) — expanded from the old 6-category bust_type A-F
# scheme to the full failure taxonomy from the handoff. The old scheme
# is gone entirely, not kept alongside — a loss should get exactly one
# category from the list that actually matches its bet type.

PROP_FAILURE_CATEGORIES = [
    "Research Failure",             # missing or unverified data
    "Path Failure",                 # wrong score paths counted
    "Structure Failure",            # wrong classification or dominance
    "Format Failure",               # bo3 paths used on a bo5 match (Gate 11-B)
    "Source Weight Failure",        # narrative used as a direction signal
    "Competition Context Failure",  # form stats pulled from wrong competition level
    "Volatility Misapplication",    # volatility tag used to set direction, not just cap confidence
    "Total Sets Mispricing",        # MORE on a 70%+ favorite with no model-backed flag
    "Baseline Mismatch",            # bo3 averages applied to a bo5 line
    "Variance",                     # correct read, exact line hit or minimal margin
    "External Event",               # retirement, walkover, weather, injury mid-match
    "Confidence Overcap",           # graded GREEN but data didn't actually support it
    "Gate Failure",                 # a specific gate should have blocked the pick
]

ML_FAILURE_CATEGORIES = [
    "ML Research Failure",
    "ML Signal Failure",
    "ML Classification Failure",
    "ML Form Failure",
    "ML H2H Failure",
    "ML Variance",
    "ML External Event",
]

# v7.9.7 (moneyline learning system): dedicated attribution prompt.
# owner' spec: given the combination of data + reasoning that
# produced the winner call, was a wrong (or even a right) call driven
# by (a) bad/missing DATA, (b) flawed REASONING/weighting even though
# the data was fine, or (c) plain VARIANCE — correct read, wrong day.
# Each has a different fix, so they must be told apart explicitly
# rather than collapsed into one generic "bust" tag. Only case (b)
# should ever nudge a weight — (a) means fix data sourcing, (c) means
# don't touch anything.
def _build_ml_attribution_prompt():
    cats = "\n".join(f'  "{c}"' for c in ML_FAILURE_CATEGORIES)
    return f"""Classify why a moneyline pick hit or busted, using the
recorded signal breakdown and the model's own reasoning provided below.

Return ONLY valid JSON:
{{
  "outcome": "HIT or BUST",
  "root_cause": "DATA_FAILURE, REASONING_FAILURE, or VARIANCE",
  "failure_category": "one of the categories below, or null if HIT or VARIANCE",
  "reasoning_note": "1-2 sentences on what in the data or logic led to this",
  "weight_signal_implicated": "serve_edge, return_edge, clutch_edge, form_edge, or null",
  "adjustment_direction": "increase/decrease/none"
}}

How to decide root_cause:
- DATA_FAILURE: the underlying stats fed into the dominance score were
  missing, stale, or wrong (e.g. a player's data tier was thin, a stat
  was unconfirmed, or a real update — injury, retirement, form shift —
  wasn't reflected in what the system had).
- REASONING_FAILURE: the data itself was fine and confirmed, but the
  combination of signals was weighted incorrectly for this matchup —
  e.g. a signal that should have mattered less/more for this specific
  situation was treated as equally important as usual.
- VARIANCE: the data was good and the weighting was reasonable — this
  was a legitimate close-to-even matchup or a normal upset, not a
  system problem.

Only set weight_signal_implicated (and a non-"none" adjustment_direction)
when root_cause is REASONING_FAILURE. Never set it for DATA_FAILURE or
VARIANCE — a data problem or genuine variance should NOT change a weight.

On a BUST with root_cause REASONING_FAILURE, failure_category MUST be
exactly one of these (verbatim, case-sensitive):
{cats}"""


def _build_learning_prompt(failure_categories):
    cats = "\n".join(f'  "{c}"' for c in failure_categories)
    return f"""Classify why a tennis pick hit or busted.
Return ONLY valid JSON:
{{
  "outcome": "HIT or BUST",
  "failure_category": "one of the categories below, or null if HIT",
  "rule_violated": "Rule/Gate number or null",
  "most_predictive_signal": "description",
  "weight_adjustment_candidate": "signal name or null",
  "adjustment_direction": "increase/decrease/none",
  "format_contamination": true or false,
  "young_home_player": true or false,
  "revenge_match": true or false
}}
On a BUST, failure_category MUST be exactly one of these (verbatim, case-sensitive):
{cats}
Pick the single category that best explains the miss. If more than one
plausibly applies, pick the one closest to the root cause — e.g. if a
format error caused a bad path count, that's Format Failure, not Path
Failure, because the path count was a downstream symptom."""

# Map prop stat_type strings to the field in a v8 recent-match dict
# that holds the confirmed actual value for that stat.
_STAT_TYPE_V8_FIELD = {
    "total_games":     "total_games",
    "games_won":       "player_games",
    "total_games_won": "player_games",  # APEX-STATFIX-2026-10-07: alias --
                                         # real pending data uses this exact
                                         # string (not "games_won"); was a
                                         # bare naming mismatch, confirmed
                                         # live against 35.3% of the pending
                                         # backlog being blocked by this gap.
    "total_sets":      "set_count",
}

def _sets_won_lost(score_str, player_games, opponent_games):
    """
    APEX-STATFIX-2026-10-07: parses a vendor score string
    ("7-6(1) 4-6 6-1") into per-set game counts and determines which
    position (first-listed or second-listed number in each set) is the
    subject player, by matching the summed per-set games against
    player_games/opponent_games -- which are already subject-relative,
    including on a match flipped via _flip_match_perspective(). The raw
    score string itself is NOT subject-relative (it reflects the
    vendor's fixed player1/player2 ordering for that match record), so
    this orientation check is required before counting sets.
    Live-validated 2026-10-07 against a real match (Michelsen/Alcaraz,
    2026-10-01, score "7-6(1) 4-6 6-1", player_games=13,
    opponent_games=17): sum of first-listed numbers (17) matched
    opponent_games, confirming orientation, and the resulting 1-set-won
    / 2-sets-lost count matched the real outcome.
    Returns (player_sets, opponent_sets), or (None, None) if the score
    can't be parsed or the orientation can't be confirmed.
    """
    sets = []
    for tok in (score_str or "").split():
        core = tok.split("(")[0]
        if "-" not in core:
            continue
        parts = core.split("-")
        if len(parts) != 2:
            continue
        try:
            a, b = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        sets.append((a, b))
    if not sets or player_games is None or opponent_games is None:
        return None, None
    sum_a = sum(a for a, b in sets)
    sum_b = sum(b for a, b in sets)
    if sum_a == opponent_games and sum_b == player_games:
        return (sum(1 for a, b in sets if b > a),
                sum(1 for a, b in sets if a > b))
    if sum_a == player_games and sum_b == opponent_games:
        return (sum(1 for a, b in sets if a > b),
                sum(1 for a, b in sets if b > a))
    return None, None


def _fantasy_score_from_match(match):
    """
    APEX-STATFIX-2026-10-07: fantasy_score is mathematically derivable
    from data the vendor already returns -- confirmed against the real
    formula apex_prop_sim_v1.py uses to train/calibrate on it:
        10 + (games_won - games_lost) + 3*(sets_won - sets_lost)
           + 0.5*(aces - double_faults)
    Returns None if any required component is missing/unparseable
    (never guesses a value).
    """
    player_games   = match.get("player_games")
    opponent_games = match.get("opponent_games")
    if player_games is None or opponent_games is None:
        return None
    sets_won, sets_lost = _sets_won_lost(match.get("score", ""), player_games, opponent_games)
    if sets_won is None:
        return None
    stats = match.get("player_stats", {}) or {}
    aces = stats.get("aces")
    dfs  = stats.get("doubleFaults")
    if aces is None or dfs is None:
        return None
    return (10.0 + (player_games - opponent_games)
            + 3.0 * (sets_won - sets_lost)
            + 0.5 * (aces - dfs))


def _stat_from_v8_box_score(stat_type, match):
    """
    Resolves stat types backed by the per-match "stats" block (aces,
    double faults, break points) or the scoreline (tie breaks, fantasy
    score) rather than a direct top-level match field.
    Returns None if the stat isn't in the box score or wasn't returned
    for this match.
    """
    if stat_type == "total_tie_breaks":
        # APEX-STATFIX-2026-10-07: tiebreak count is orientation-
        # independent (a tiebreak happened regardless of which side is
        # "player" vs "opponent"), so this needs no perspective check.
        return (match.get("score", "") or "").count("(")
    if stat_type == "fantasy_score":
        return _fantasy_score_from_match(match)
    stats = match.get("player_stats", {}) or {}
    key_map = {
        "aces":              "aces",
        "double_faults":     "doubleFaults",
        "break_points_won":  "breakPointsConverted",
    }
    api_key = key_map.get(stat_type)
    if api_key is None:
        return None
    return stats.get(api_key)

def verify_result_via_v8(pick, dates, tour_hint=None):
    """
    v7.9.6 (5E) — replaces the fragile web_search + first-number-regex
    result check with the now-verified V8 matches-played data (winner
    detection confirmed against two real Wimbledon 2026 results; see
    session notes). Returns:
        (status, actual_value, matched_record)
    status is one of: "COMPLETED", "NON_COMPLETION", "UNKNOWN"
    actual_value is the confirmed number for this pick's stat_type, or
    None if the match was found but that specific stat isn't available.
    matched_record is the raw v8 match dict, or None.

    Falls back to returning ("UNKNOWN", None, None) on any failure —
    callers should fall back to the web_search path when this happens,
    not treat it as a confirmed non-completion.
    """
    if not _V8_AVAILABLE:
        return "UNKNOWN", None, None

    player     = pick.get("player", "")
    opponent   = pick.get("opponent", "")
    surface    = pick.get("surface", "")
    stat_type  = pick.get("stat_type", "")
    if not player:
        return "UNKNOWN", None, None

    tours_to_try = [tour_hint] if tour_hint else _tour_order(player)
    canonical = None
    for t in tours_to_try:
        canonical = v8_resolve_player_name(player, t)
        if canonical:
            break

    if not canonical:
        return "UNKNOWN", None, None

    try:
        recent = v8_get_recent_matches(canonical, surface=surface or None, limit=8)
    except Exception:
        return "UNKNOWN", None, None

    if not recent:
        return "UNKNOWN", None, None

    # Find the match against this specific opponent, closest to the
    # date we're learning on (allow a 1-day slop for timezone/reporting lag).
    target_date = dates.get("learn_on_date", "")
    opp_lower = opponent.lower()
    candidates = [m for m in recent if opp_lower and opp_lower in m.get("opponent", "").lower()]
    if not candidates:
        return "UNKNOWN", None, None

    match = None
    for m in candidates:
        if m.get("date") == target_date:
            match = m
            break
    if match is None:
        match = candidates[0]  # closest available match against this opponent

    if match.get("retired"):
        return "NON_COMPLETION", None, match

    actual = None
    if stat_type in _STAT_TYPE_V8_FIELD:
        actual = match.get(_STAT_TYPE_V8_FIELD[stat_type])
    else:
        actual = _stat_from_v8_box_score(stat_type, match)

    if actual is None:
        # Match found and confirmed complete, but this specific stat
        # wasn't in the box score — still useful (W/L, total_games are
        # solid), just can't grade this particular prop from V8 alone.
        return "COMPLETED_NO_STAT", None, match

    return "COMPLETED", actual, match


def learning_agent(dates, weights):
    log("Learning", f"Running for: {dates['learn_on_date']}")
    d1.heartbeat("learning", "WORKING", detail=f"run for {dates['learn_on_date']}", _logger=log)
    picks_file = ARCHIVE_DIR / f"picks_{dates['learn_on_date']}.json"

    if not picks_file.exists():
        send_discord("system",
            f"📋 No picks scheduled for {dates['learn_on_date']} — nothing to evaluate"
        )
        d1.heartbeat("learning", "IDLE", detail="no picks file", _logger=log)
        return

    with open(picks_file) as f:
        today_picks = json.load(f)

    if not today_picks:
        send_discord("system",
            f"📋 No picks for {dates['learn_on_date']} — nothing to evaluate"
        )
        d1.heartbeat("learning", "IDLE", detail="no picks", _logger=log)
        return

    results        = []
    non_completions = []
    pending        = []
    duplicates_prevented = 0  # APEX-HARDEN-2026-10-07

    for pick in today_picks:
        player     = pick.get("player","")
        opponent   = pick.get("opponent","")
        tournament = pick.get("tournament","")

        actual      = None
        outcome     = None
        result_type = None
        signal      = None
        source      = None

        # APEX-LEARNV2-2026-10-24: results we already store come first -- props_db is
        # settled hourly from SportsAPI365 box scores, so most picks are graded here
        # without asking the results API again (the API missed ~2 of every 3 picks).
        v8_status, v8_actual = "UNKNOWN", None
        try:
            import apex_learning_v2 as _alv2
            _c = _alv2._conn()
            _st, _info = _alv2.lookup_result(_c, player, opponent, pick.get("stat_type"), pick.get("line"),
                                             pick.get("direction"), match_date=dates["learn_on_date"],
                                             prop_type=pick.get("prop_type_classified"))
            _c.close()
            if _st in ("HIT", "BUST"):
                v8_status, v8_actual, source = "PROPS_DB", (_info or {}).get("actual"), "props_db"
                outcome, result_type = _st, "COMPLETED"
                actual = v8_actual if v8_actual is not None else "settled"
            elif _st == "VOID":
                v8_status = "NON_COMPLETION"
        except Exception as _e:
            log("Learning", f"props_db lookup failed (non-fatal): {type(_e).__name__}: {_e}")

        # ── v7.9.6 (5E): then V8 verified data ──
        if v8_status == "UNKNOWN":
            v8_status, v8_actual, _v8_match = verify_result_via_v8(pick, dates)

        if v8_status == "NON_COMPLETION":
            result_type = "NON_COMPLETION"
            signal = "retirement (V8 confirmed)"
        elif v8_status == "COMPLETED":
            actual  = v8_actual
            outcome = _determine_outcome(actual, pick)
            result_type = "COMPLETED"
            source = "V8"
        # v8_status in ("COMPLETED_NO_STAT", "UNKNOWN") falls through to web_search below

        if result_type is None and v8_status != "PROPS_DB":
            result_text  = web_search(
                f"{player} vs {opponent} {tournament} result score {dates['learn_on_date']}"
            )
            result_type, signal = classify_result(result_text) if result_text else ("UNKNOWN", None)
            if result_type == "COMPLETED":
                actual  = _extract_stat_result(result_text, pick)
                outcome = _determine_outcome(actual, pick)
                source = "web_search"

        if result_type == "NON_COMPLETION":
            non_completions.append(pick)
            _quarantine_pick(pick, {"type": "NON_COMPLETION", "signal": signal}, dates)
            send_discord("alerts",
                f"⚠️ **NON-COMPLETION** — {dates['ct_time']}\n"
                f"Player: {player} | Pick: {pick.get('direction')} {pick.get('line')}\n"
                f"Detected: {signal}\n"
                f"**Excluded from learning — weights unchanged**"
            )
            continue

        if result_type != "COMPLETED" or actual is None:
            pending.append(pick)
            if not _add_to_pending(pick, dates):
                duplicates_prevented += 1
            continue

        classified = {}
        failure_categories = ML_FAILURE_CATEGORIES if pick.get("bet_type") == "moneyline" else PROP_FAILURE_CATEGORIES
        # APEX-LEARNV2-2026-10-24: the model used to get the raw pick JSON, whose card
        # banner text ("Total Games record 1066-650", "UNDERPERFORMING", "attention low")
        # it then quoted back as evidence. It now gets only the real facts.
        _facts = {k: pick.get(k) for k in ("player", "opponent", "stat_type", "line", "direction",
                                           "confidence", "prop_type_classified", "surface")}
        classification = call_model(
            MODEL_HAIKU, _build_learning_prompt(failure_categories),
            f"Pick: {json.dumps(_facts, default=str)}\n"
            f"Actual: {actual}\nOutcome: {outcome}\nResult source: {source}",
            temperature=0.1, max_tokens=350
        )
        if classification:
            try:
                clean = re.sub(r"```json|```","",classification).strip()
                # APEX-LEARNV2-2026-10-24: the model adds a sentence after its JSON
                # ("Extra data" errors -> every loss reason was "?"); read only the
                # first JSON object.
                _i = clean.find("{")
                classified, _ = json.JSONDecoder().raw_decode(clean[_i:] if _i >= 0 else clean)
            except Exception as _classify_e:
                # Bug fix (this session): this was a bare `except: pass`,
                # identical to the compliance-agent bug found and fixed
                # earlier tonight -- every parse failure here was silently
                # discarded with zero logging, meaning historical failure-
                # category data feeding the learning loop could be quietly
                # incomplete with no way to tell. Now logs the exception
                # type/message and the raw model output (first 500 chars)
                # that failed to parse, same pattern as the compliance fix.
                log("Learning", f"Classification parse failed for pick "
                    f"{pick.get('player', '?')} vs {pick.get('opponent', '?')}: "
                    f"{type(_classify_e).__name__}: {_classify_e} | "
                    f"raw output: {classification[:500]!r}")
        else:
            # Same fix: distinguish "call_model itself returned nothing"
            # (API call failed) from "returned something that didn't
            # parse" (logged above) -- previously both were identical,
            # silent empty {} with no trace of which happened.
            log("Learning", f"Classification call returned nothing for pick "
                f"{pick.get('player', '?')} vs {pick.get('opponent', '?')}")

        results.append({**pick, "outcome": outcome,
                        "classified": classified, "actual": actual,
                        "result_source": source})

        # Live per-pick alert (this session, requested by operator):
        # previously the ONLY place a win/loss ever appeared was the
        # single batched report at 11PM, easy to miss entirely. Now
        # fires the moment each pick's outcome is determined, during
        # the same nightly run, so results are visible incrementally
        # in #alerts-channel rather than only in one long message.
        # APEX-PICKSCLEAN-2026-10-24 (owner): per-pick wins/losses no longer post to
        # #alerts; board + backup results go to #picks (apex_picks_channel). Line kept in the log.
        _emoji = "✅ WIN" if outcome == "HIT" else "❌ LOSS"
        log("Results",
            f"{_emoji} — {pick.get('player','?')} {pick.get('direction','?')} "
            f"{pick.get('line','?')} {pick.get('stat_type','?')} "
            f"(graded {pick.get('confidence', 0):.0%} {pick.get('color','?')}) "
            f"vs {pick.get('opponent','?')} | actual: {actual} | source: {source}"
        )

    _send_learning_report(results, non_completions, pending, dates, weights,
                           duplicates_prevented=duplicates_prevented)
    _alert_pending_approvals("11PM nightly learning")
    rule_discovery_check(results, dates)
    d1.heartbeat("learning", "IDLE", increment_success=True,
                 detail=f"{len(results)} results processed", _logger=log)


# ================================================================
# MONEYLINE SETTLEMENT (2026-08-04) — the real, working equivalent of
# learning_agent() above, for moneyline. Props had a complete automated
# nightly settlement system; moneyline had none at all -- this closes
# that gap using the SAME real pattern (real match data first, web
# search fallback) rather than inventing a separate approach.
# ================================================================

def verify_moneyline_result_via_v8(player, opponent, predicted_winner,
                                    dates, tour_hint=None):
    """
    Moneyline's equivalent of verify_result_via_v8() above -- same real
    V8 matches-played lookup, but comparing WHO WON rather than a stat
    threshold. Returns (status, actual_winner, matched_record):
        status is one of: "COMPLETED", "NON_COMPLETION", "UNKNOWN"
        actual_winner is the real winning player's name, or None
        matched_record is the raw v8 match dict, or None

    Falls back to ("UNKNOWN", None, None) on any failure -- callers
    should fall back to web_search when this happens, not treat it as
    a confirmed non-completion. Deliberately mirrors verify_result_
    via_v8's exact structure so the two stay easy to compare/maintain
    side by side.
    """
    if not _V8_AVAILABLE:
        return "UNKNOWN", None, None
    if not player or not opponent:
        return "UNKNOWN", None, None

    tours_to_try = [tour_hint] if tour_hint else _tour_order(player)
    canonical = None
    for t in tours_to_try:
        canonical = v8_resolve_player_name(player, t)
        if canonical:
            break
    if not canonical:
        return "UNKNOWN", None, None

    try:
        recent = v8_get_recent_matches(canonical, limit=8)
    except Exception:
        return "UNKNOWN", None, None
    if not recent:
        return "UNKNOWN", None, None

    target_date = dates.get("learn_on_date", "")
    opp_lower = opponent.lower()
    candidates = [m for m in recent if opp_lower and opp_lower in m.get("opponent", "").lower()]
    if not candidates:
        return "UNKNOWN", None, None

    match = None
    for m in candidates:
        if m.get("date") == target_date:
            match = m
            break
    if match is None:
        match = candidates[0]

    if match.get("retired"):
        return "NON_COMPLETION", None, match

    # Real bug fix (caught before delivery, 2026-08-04): the actual
    # field get_recent_matches() returns is "won" (boolean, relative to
    # the queried player), NOT "winner" -- confirmed directly against
    # openclaw_v8_api.py's real results.append() call. The original
    # version of this function used "winner", which doesn't exist, so
    # it would have silently gotten None every time and always fallen
    # through to web_search even when V8 had a perfectly good verified
    # result. Also respecting won_verified -- a match whose score
    # couldn't be cleanly parsed has won=None/won_verified=False and
    # must not be trusted as a real result either.
    if not match.get("won_verified"):
        return "COMPLETED_NO_STAT", None, match
    won = match.get("won")
    actual_winner = canonical if won else opponent

    return "COMPLETED", actual_winner, match


def _extract_moneyline_winner_from_text(result_text, player, opponent):
    """
    Web-search fallback winner extraction, only reached when V8 has no
    match record at all. Small, targeted LLM call rather than a regex
    -- free text result descriptions are too variable to parse reliably
    with a pattern match, same reasoning classify_result() already
    applies elsewhere in this file. Returns the winner's name (player or
    opponent, verbatim as passed in) or None if genuinely unclear.
    """
    if not result_text:
        return None
    prompt = (
        f"Based ONLY on this search result text, who won the match "
        f"between {player} and {opponent}? Reply with EXACTLY ONE of: "
        f"\"{player}\", \"{opponent}\", or \"UNCLEAR\" -- nothing else.\n\n"
        f"Search result:\n{result_text[:2000]}"
    )
    try:
        raw = call_model(MODEL_HAIKU, "You extract match winners from text precisely.",
                          prompt, temperature=0.0, max_tokens=20)
    except Exception:
        return None
    if not raw:
        return None
    raw = raw.strip().strip('"')
    if raw == player:
        return player
    if raw == opponent:
        return opponent
    return None


def moneyline_settlement_agent(dates, weights=None):
    """
    The real, working moneyline equivalent of learning_agent(). Finds
    posted moneyline picks with no outcome yet, verifies the real result
    (V8 first, web search fallback, same order as props), determines
    HIT/BUST by comparing predicted_winner to the real winner, and
    records it via the already-existing d1.record_outcome() -- the
    single real source of truth Pick Results and the (now-fixed)
    tracker tiles both read from directly.

    Deliberately does NOT try to match the legacy "✅ WIN — Player MORE
    stat" Discord-log regex format some older discovery code expects --
    that regex-scraping approach is being retired in favor of both
    props and moneyline reading Dataset A directly (see the tracker
    tile fix). This posts its own clear, real moneyline-shaped alert.
    """
    if not _DAQ_AVAILABLE:
        log("MLSettlement", "dataset_a_query module unavailable — cannot run")
        return

    d1.heartbeat("ml_settlement", "WORKING",
                 detail=f"run for {dates.get('learn_on_date','?')}", _logger=log)

    pending = d1q.find_pending_moneyline_picks()
    if not pending:
        d1.heartbeat("ml_settlement", "IDLE", detail="nothing pending", _logger=log)
        return

    settled, still_pending, non_completions = [], [], []

    for pick in pending:
        player     = pick.get("player", "")
        opponent   = pick.get("opponent", "")
        tournament = pick.get("tournament", "") or ""
        tour       = pick.get("tour")
        predicted  = pick.get("predicted_winner")

        v8_status, v8_winner, _match = verify_moneyline_result_via_v8(
            player, opponent, predicted, dates, tour_hint=(tour.lower() if tour else None)
        )

        actual_winner = None
        source = None

        if v8_status == "NON_COMPLETION":
            non_completions.append(pick)
            send_discord("alerts",
                f"⚠️ **MONEYLINE NON-COMPLETION** — {dates.get('ct_time','')}\n"
                f"{player} vs {opponent} — match retired/incomplete, excluded from settlement")
            continue
        elif v8_status == "COMPLETED":
            actual_winner = v8_winner
            source = "V8"
        # COMPLETED_NO_STAT / UNKNOWN falls through to web_search below

        if actual_winner is None:
            result_text = web_search(
                f"{player} vs {opponent} {tournament} result winner {dates.get('learn_on_date','')}"
            )
            actual_winner = _extract_moneyline_winner_from_text(result_text, player, opponent) \
                            if result_text else None
            if actual_winner:
                source = "web_search"

        if actual_winner is None:
            still_pending.append(pick)
            continue

        outcome = "HIT" if actual_winner.lower() in (predicted or "").lower() \
                         or (predicted or "").lower() in actual_winner.lower() else "BUST"

        d1.record_outcome(pick["eval_id"], outcome, source, _logger=log)
        settled.append({**pick, "outcome": outcome, "actual_winner": actual_winner, "source": source})

        emoji = "✅ WIN" if outcome == "HIT" else "❌ LOSS"
        log("Results",   # APEX-PICKSCLEAN-2026-10-24: was a per-pick #alerts post
            f"{emoji} — {player} vs {opponent} (MONEYLINE) | predicted: {predicted} | "
            f"actual winner: {actual_winner} | source: {source}"
        )

    if settled:
        hits  = sum(1 for s in settled if s["outcome"] == "HIT")
        busts = len(settled) - hits
        send_discord("alerts",
            f"🧠 **MONEYLINE SETTLEMENT — {dates.get('learn_on_date','?')}**\n"
            f"{hits} hit | {busts} bust | {len(still_pending)} still pending | "
            f"{len(non_completions)} non-completions"
        )

    d1.heartbeat("ml_settlement", "IDLE", increment_success=True,
                 detail=f"{len(settled)} settled, {len(still_pending)} pending", _logger=log)


# ================================================================
# SLIP LOGGING (v7.9.6 — Step 5E)
# ================================================================
# CLI-based interim implementation. The handoff describes this as a
# Discord #slips command handler, but openclaw_core.py has no inbound
# Discord listener — send_discord() only sends, it can't receive. Live
# bot listening is Step 9 (Discord gateway bridge), not 5E. This gives
# working functionality today via CLI piping, in the exact entry/result
# text format from the handoff, so Step 9 can later just pipe Discord
# messages through these same parser functions with no rework needed.

STAT_ABBREV = {
    "tg":    "total_games",
    "sets":  "sets_won",      # APEX-WIRING-2026-08-27: "Sets Won" is the common player prop
    "ts":    "total_sets",    # APEX-WIRING-2026-08-27
    "aces":  "aces",
    "df":    "double_faults",
    "bp":    "break_points_won",
    "gw":    "games_won",
    "fs":    "fantasy_score",   # APEX-SLIPFS-2026-10-11: Fantasy Score is back on the boards
}

_SLIP_LEG_PATTERN = re.compile(
    r"^(?P<player>.+?)\s+(?P<direction>MORE|LESS)\s+"
    r"(?P<line>\d+(?:\.\d+)?)\s+(?P<stat>\S+)\s*"
    # APEX-DISCOUNT-2026-10-16: 🟢 = Goblin and 🔴 = Demon (PrizePicks' colors) are
    # accepted too; a trailing 🏷️ marks a discount play.
    r"(?P<demon>🔴😈|🔴)?\s*(?P<goblin>😈|🟢)?\s*(?P<discount>\U0001F3F7\uFE0F?)?\s*$",
    re.IGNORECASE
)

_SLIP_ML_PATTERN = re.compile(
    r"^(?P<player>.+?)\s+ML\s+(?P<direction>MORE|LESS)\s*$",
    re.IGNORECASE
)

_RESULT_MARK_TOKENS = ["✅", "❌", "PUSH", "VOID", "WIN", "LOSS"]
_RESULT_MARK_NORMALIZE = {
    "✅": "WIN", "❌": "LOSS", "WIN": "WIN", "LOSS": "LOSS",
    "PUSH": "PUSH", "VOID": "VOID",
}

def _parse_result_leg(leg_text: str):
    """
    Finds the first result marker (✅/❌/WIN/LOSS/PUSH/VOID) anywhere in
    the leg text and returns (player_name, normalized_result) or None if
    no marker is found. A single anchored regex was tried first and
    proved unreliable — the emoji markers never actually matched because
    the non-greedy player group silently absorbed them before the
    alternation got a chance to see them (verified: "Wang ✅ WIN" parsed
    as player="Wang ✅", mark="WIN" instead of player="Wang", mark="WIN").
    Searching for the earliest marker directly sidesteps that.
    """
    lower = leg_text.upper()
    earliest_pos, earliest_token = None, None
    for token in _RESULT_MARK_TOKENS:
        pos = leg_text.find(token) if token in ("✅", "❌") else lower.find(token)
        if pos == -1:
            continue
        if earliest_pos is None or pos < earliest_pos:
            earliest_pos, earliest_token = pos, token
    if earliest_pos is None:
        return None
    player = leg_text[:earliest_pos].strip()
    if not player:
        return None
    return player, _RESULT_MARK_NORMALIZE[earliest_token]

_ML_RESULT_PATTERN = re.compile(
    r"^(?P<player>.+?)\s+ML\s+(?P<result>WIN|LOSS)\s*(?:—|-|--)?\s*(?P<book>.*)$",
    re.IGNORECASE
)

def _load_slip_log():
    if SLIP_LOG_FILE.exists():
        with open(SLIP_LOG_FILE) as f:
            return json.load(f)
    return []

def _save_slip_log(entries):
    with open(SLIP_LOG_FILE, "w") as f:
        json.dump(entries, f, indent=2, default=str)

def _check_correlation(new_entry: dict, slip_id: str, log_entries: list) -> str:
    """
    v7.9.6 (Step 8, correlation gate enforcement). Blocks a moneyline
    and a prop leg for the SAME PLAYER NAME from landing on the same
    slip — the code-level backstop for the rule already written in the
    master prompt ("moneyline and prop from the same match are NEVER
    placed on the same slip").

    KNOWN LIMITATION: the slip format only captures the player being
    bet ON, never their opponent (e.g. "Sinner ML MORE" doesn't say
    who he's playing). This means same-player-name correlation is
    reliably caught, but a moneyline leg on one player and a prop leg
    on THEIR OPPONENT (same match, different names) is NOT currently
    detectable — the slip format would need an opponent field to
    catch that case. Flagged here explicitly rather than silently
    claiming full coverage.

    Returns a rejection reason string if blocked, or None if clear.
    """
    same_slip_legs = [e for e in log_entries if e["slip_id"] == slip_id]
    new_player = new_entry["player"].lower()
    new_type = new_entry["bet_type"]

    for existing in same_slip_legs:
        if existing["player"].lower() != new_player:
            continue
        if existing["bet_type"] != new_type:
            return (f"Correlation gate: {new_entry['player']} already has a "
                    f"{existing['bet_type']} leg on this slip — moneyline and "
                    f"prop for the same player are never combined.")
    return None


def _check_duplicate_leg(new_entry: dict, log_entries: list) -> bool:
    """
    SLIP-AUTOMATION-2026-09-27 (safety pass, priority 1 of 4, ahead of
    screenshot auto-confirm). Catches the same leg being logged twice on
    the same date -- a slip retyped by hand, a screenshot re-processed
    outside discord_bot.py's own image-hash dedup window, or (once
    auto-confirm ships) two auto-confirms of the same underlying
    screenshot. Compares (bet_type, player, direction, line, stat_type,
    date) against every entry already on file that isn't itself a
    rejected/duplicate row -- PENDING and already-resolved
    (WIN/LOSS/PUSH/VOID) legs both count, since a resolved leg re-typed
    by mistake is exactly as much a duplicate as a pending one.
    Returns True (duplicate) or False.
    """
    for e in log_entries:
        if e.get("status") in ("REJECTED_CORRELATION", "DUPLICATE_SKIPPED"):
            continue
        if e.get("date") != new_entry.get("date"):
            continue
        if e.get("bet_type") != new_entry.get("bet_type"):
            continue
        if (e.get("player") or "").lower() != (new_entry.get("player") or "").lower():
            continue
        if e.get("direction") != new_entry.get("direction"):
            continue
        if new_entry.get("bet_type") == "prop":
            if e.get("stat_type") != new_entry.get("stat_type"):
                continue
            if e.get("line") != new_entry.get("line"):
                continue
        return True
    return False

def _slip_signature(legs):
    return frozenset((e.get("bet_type"), str(e.get("player") or "").strip().lower(), e.get("direction"),
                      e.get("stat_type"), e.get("line")) for e in legs)


def _find_duplicate_slip(new_legs: list, new_slip_id: str, log_entries: list):
    """APEX-SLIPDUP-2026-10-13: slip_id of an earlier slip on the same date with
    the same set of picks and the same stake, else None."""
    if not new_legs:
        return None
    sig, date_, stake = _slip_signature(new_legs), new_legs[0].get("date"), new_legs[0].get("stake")
    by_slip = {}
    for e in log_entries:
        if e.get("slip_id") == new_slip_id or e.get("date") != date_:
            continue
        if e.get("status") in ("REJECTED_CORRELATION", "DUPLICATE_SKIPPED"):
            continue
        by_slip.setdefault(e.get("slip_id"), []).append(e)
    for sid, legs in by_slip.items():
        if _slip_signature(legs) == sig and legs[0].get("stake") == stake:
            return sid
    return None


def extract_slip_screenshot(image_data: str, media_type: str):
    """
    Vision extraction for a SLIP CONFIRMATION screenshot -- legs, stake,
    potential_payout, and (2026-08-04 addition) the full payout tier
    table when a Details-view screenshot is provided -- real EV math
    needs every tier (5/6, 4/6, etc.), not just the top-line payout.
    Mirrors the exact call shape discord_bot.py's #general handler
    already uses for board screenshots (MODEL_SONNET, temperature 0.1,
    JSON-only response, fence-stripped before parsing) -- same proven
    pattern, different prompt.

    Returns the parsed dict on success, or None on any failure (no
    result, unparseable JSON) -- callers should fall back to the
    existing text-entry path on None, same as the board-screenshot
    path already does. payout_table will be null within a successful
    result if the screenshot didn't show that breakdown (e.g. the main
    entry card rather than the Details tab) -- that's expected, not a
    failure.
    """
    result = call_model(
        MODEL_SONNET,
        SLIP_SCREENSHOT_PARSE_PROMPT,
        [{"type": "image",
          "source": {"type": "base64", "media_type": media_type, "data": image_data}},
         {"type": "text",
          "text": "Extract the legs, stake, potential payout, and (if visible) the full payout tier table from this slip screenshot."}],
        temperature=0.1,
        max_tokens=1500,
    )
    if not result:
        return None
    try:
        clean = re.sub(r"```json|```", "", result).strip()
        return json.loads(clean)
    except Exception:
        return None


# ================================================================
# SLIP EV MATH (2026-08-04) — real per-slip expected value.
# Real, locked design from prior scoping (see DEMETRIUS project memory):
# no shared "adjustment bar," no hardcoded payout assumptions. Takes
# Apex's own real per-leg confidence and the real payout table for the
# EXACT slip as built (read from a screenshot, never assumed), and
# computes expected value by brute-force enumeration of every real
# hit/miss combination. Standard/Goblin/Demon mixing is handled
# automatically -- the math only ever sees a confidence number and a
# payout table, never a leg "type."
# ================================================================

def compute_slip_ev(leg_confidences: list, payout_table: dict, stake: float = 1.0) -> dict:
    """
    leg_confidences: list of floats 0-1, Apex's real per-leg confidence
                      for each leg in the slip (order doesn't matter).
    payout_table: dict {hit_count: multiplier}, e.g. {6: 68, 5: 1, 4: 0.4}
                  -- read from the REAL displayed slip, never hardcoded.
                  A hit count not present in this dict pays 0 (confirmed
                  real PrizePicks rule: below the lowest listed tier
                  pays nothing).
    stake: real dollar amount wagered. Defaults to 1.0 so ev_multiple
           alone is meaningful even without a real stake.

    Enumerates all 2^n hit/miss combinations (trivial for real slip
    sizes -- even a 6-leg slip is only 64 combinations, no need for
    anything smarter). Each combination's probability is the product of
    each leg's own real confidence (hit) or 1-confidence (miss).
    Multiply by that combination's real payout multiplier and sum.

    Returns {expected_value, ev_multiple, edge_pct, n_legs,
    n_combinations, recommendation}. Never raises on empty/degenerate
    input -- returns a "not enough data" shaped result instead.
    """
    n = len(leg_confidences)
    if n == 0:
        return {"expected_value": None, "ev_multiple": None, "edge_pct": None,
                "n_legs": 0, "n_combinations": 0,
                "note": "no legs with a real confidence to compute against"}

    ev_multiple = 0.0
    for outcome in product([1, 0], repeat=n):  # 1=hit, 0=miss, per leg
        p = 1.0
        for hit, conf in zip(outcome, leg_confidences):
            p *= conf if hit else (1 - conf)
        hits = sum(outcome)
        ev_multiple += p * payout_table.get(hits, 0)

    expected_value = ev_multiple * stake
    edge_pct = (ev_multiple - 1.0) * 100  # vs breakeven (1.0x = stake back)

    return {
        "expected_value": round(expected_value, 4),
        "ev_multiple": round(ev_multiple, 4),
        "edge_pct": round(edge_pct, 2),
        "n_legs": n,
        "n_combinations": 2 ** n,
        "recommendation": "BET" if ev_multiple > 1.0 else "PASS",
    }


def evaluate_slip_ev_from_screenshot(image_data: str, media_type: str,
                                      stake_override: float = None) -> dict:
    """
    Full real pipeline: extract_slip_screenshot() -> match each leg to
    Apex's own real evaluation via dataset_a_query.find_confidence_for_leg()
    -> compute_slip_ev(). Honest at every step -- a leg with no matching
    Apex evaluation gets flagged and the whole call fails loudly rather
    than silently guessing a confidence for it; a screenshot with no
    visible payout tier table (the main entry card, not the Details tab)
    asks for the right screenshot rather than assuming a table.
    """
    extracted = extract_slip_screenshot(image_data, media_type)
    if not extracted:
        return {"error": "Could not read the screenshot -- try a clearer "
                          "image, or enter the slip manually."}

    legs = extracted.get("legs", [])
    payout_table_raw = extracted.get("payout_table")
    stake = stake_override or extracted.get("stake")

    if not payout_table_raw:
        return {"error": "No payout tier table visible in this screenshot "
                          "-- send the Details tab view (shows the full "
                          "Minimum Guarantee breakdown), not just the main "
                          "entry card.",
                "legs": legs, "stake": stake}

    payout_table = {row["hits"]: row["multiplier"] for row in payout_table_raw
                     if "hits" in row and "multiplier" in row}

    if not _DAQ_AVAILABLE:
        return {"error": "dataset_a_query module unavailable -- cannot "
                          "look up real confidence for these legs."}

    leg_confidences = []
    missing_legs = []
    for leg in legs:
        player = leg.get("player")
        if leg.get("bet_type") == "moneyline":
            match = d1q.find_confidence_for_leg(player)
        else:
            match = d1q.find_confidence_for_leg(player, leg.get("stat_label"), leg.get("line"))
        if match and match.get("final_confidence") is not None:
            leg_confidences.append(match["final_confidence"])
        else:
            missing_legs.append(player)

    if missing_legs:
        return {
            "error": f"No real Apex evaluation found for: "
                     f"{', '.join(missing_legs)} -- can't compute a real "
                     f"EV without a real confidence for every leg.",
            "legs_matched": len(leg_confidences), "legs_total": len(legs),
        }

    result = compute_slip_ev(leg_confidences, payout_table, stake or 1.0)
    result["legs"] = [l.get("player") for l in legs]
    result["payout_table_used"] = payout_table
    return result


# Best-effort display-label -> internal STAT_ABBREV key mapping, used to
# pre-fill the stat dropdown when reviewing a slip screenshot's extracted
# legs. Deliberately just a starting guess for the operator to confirm or
# correct in Mission Control, not a silent authority -- an unmapped label
# falls back to None (no default row is more discoverable-wrong than a
# blank one the operator visibly has to fill in).
SLIP_STAT_LABEL_MAP = {
    "total games": "tg", "games": "tg", "game total": "tg",
    "total sets": "sets", "sets": "sets", "set total": "sets",
    "aces": "aces",
    "double faults": "df", "double fault": "df",
    "break points won": "bp", "break points": "bp",
    "games won": "gw", "total games won": "gw",   # APEX-DISCOUNT-2026-10-16
    "fantasy score": "fs", "fantasy points": "fs", "fantasy": "fs",   # APEX-SLIPFS-2026-10-11
}


def _slip_type_from_props_db(player: str, stat_type: str, line: float):
    """APEX-TYPECHECK-2026-10-14: PrizePicks' odds type for a typed slip leg,
    from data/props_db.sqlite -- the projection for this player / stat / line
    whose match started no more than 36 hours ago. None when there is no
    match or the candidates disagree (never guesses)."""
    try:
        import sqlite3
        db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "props_db.sqlite")
        if not os.path.exists(db) or not player or not stat_type:
            return None
        since = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
        c = sqlite3.connect(db, timeout=5)
        try:
            rows = c.execute(
                "SELECT odds_type FROM projections WHERE projection_id NOT LIKE 'pl:%' AND (stat_slug=? OR category=?) "
                "AND line_last=? AND start_utc>=? AND instr(lower(player), ?)>0",
                (stat_type, stat_type, float(line), since, player.strip().lower())).fetchall()
        finally:
            c.close()
        types = {str(r[0] or "standard").lower() for r in rows}
        return types.pop() if len(types) == 1 else None
    except Exception:
        return None


LAST_SLIP_ID = None   # APEX-DISCOUNT-2026-10-16: slip_id of the newest parse_slip_entry() call

_MANUAL_LEG_KEYS = ("discount", "original_line", "sport", "stat_label")


def slip_leg_tags(e: dict) -> str:
    """APEX-DISCOUNT-2026-10-16: ' 🟢 Goblin' / ' 🔴 Demon' (PrizePicks' colors),
    ' 🏷️ Discount', and the sport for a leg the operator grades by hand."""
    t = {"goblin": " 🟢 Goblin", "red_demon": " 🔴 Demon", "demon": " 🔴 Demon"}.get(e.get("prop_type") or "", "")
    if e.get("discount"):
        t += " \U0001F3F7\uFE0F Discount" + ((" (regular %g)" % float(e["original_line"]))
                                          if e.get("original_line") not in (None, "") else "")
    if e.get("manual_grade"):
        t += " · %s, you grade it" % (e.get("sport") or "other sport")
    return t


def parse_slip_entry(raw_text: str, dates: dict, stake: float = None,
                      potential_payout: float = None, leg_extras: list = None) -> str:
    """
    v7.9.6 (5E). Parses a slip entry in the handoff's format:
        "Wang MORE 2.5 Sets 😈 | Chwalinska MORE 21.5 TG"
        "Sinner ML MORE"   (moneyline leg — no line/stat, just direction)
    Each leg separated by "|". Logs each as PENDING to slip_log.json.
    Returns a confirmation string, same shape as the Discord response
    the handoff specifies ("✅ Slip logged — X legs pending").

    v7.9.6 (Step 8): each leg is checked against the correlation gate
    before being logged — see _check_correlation(). A blocked leg is
    logged with status REJECTED_CORRELATION (visible audit trail, not
    silently dropped) and does NOT count toward "legs pending."

    stake / potential_payout (this session, optional): dollar figures
    for the whole slip, stored on every logged leg entry (denormalized,
    matching how this table already stores one flat record per leg
    rather than a separate slip-level table). None by default so every
    existing caller (the text-typed #slips-pnl path, --slip-entry CLI)
    is completely unaffected -- only the new slip-screenshot review path
    in Mission Control passes real values here.
    """
    legs_raw = [l.strip() for l in raw_text.split("|") if l.strip()]
    if not legs_raw:
        return "⚠️ No legs found — check the format and try again."
    # APEX-DISCOUNT-2026-10-16: leg_extras (optional, one dict per leg in raw_text
    # order) carries what the screenshot read beyond the text -- discount,
    # original_line, sport, stat_label.
    leg_extras = list(leg_extras or [])
    leg_extras += [{}] * (len(legs_raw) - len(leg_extras))

    global LAST_SLIP_ID
    log_entries = _load_slip_log()
    # APEX-DISCOUNT-2026-10-16: two slips confirmed in the same second used to share
    # one slip_id (and be graded as one slip) -- bump until unused.
    _t = int(time.time())
    _used = {e.get("slip_id") for e in log_entries}
    while f"{dates['learn_on_date']}_{_t}" in _used:
        _t += 1
    slip_id = f"{dates['learn_on_date']}_{_t}"
    LAST_SLIP_ID = slip_id
    logged = []
    errors = []
    blocked = []
    duplicates = []

    for _leg_i, leg_text in enumerate(legs_raw):
        _extra = {k: v for k, v in (leg_extras[_leg_i] or {}).items() if k in _MANUAL_LEG_KEYS and v not in (None, "")}
        ml_match = _SLIP_ML_PATTERN.match(leg_text)
        if ml_match:
            entry = {
                "slip_id":     slip_id,
                "raw_text":    leg_text,
                "bet_type":    "moneyline",
                "player":      ml_match.group("player").strip(),
                "direction":   ml_match.group("direction").upper(),
                "line":        None,
                "stat_type":   None,
                "prop_type":   "moneyline",
                "book":        None,
                "status":      "PENDING",
                "logged_at":   dates["ct_time"],
                "date":        dates["learn_on_date"],
                "resolved_at": None,
                "failure_category": None,
                "stake": stake,
                "potential_payout": potential_payout,
            }
            if _check_duplicate_leg(entry, logged):   # APEX-SLIPDUP-2026-10-13: within this slip only
                entry["status"] = "DUPLICATE_SKIPPED"
                log_entries.append(entry)
                duplicates.append(leg_text)
                continue
            block_reason = _check_correlation(entry, slip_id, log_entries + logged)
            if block_reason:
                entry["status"] = "REJECTED_CORRELATION"
                entry["rejection_reason"] = block_reason
                log_entries.append(entry)
                blocked.append((leg_text, block_reason))
                continue
            log_entries.append(entry)
            logged.append(entry)
            continue

        m = _SLIP_LEG_PATTERN.match(leg_text)
        if not m:
            errors.append(leg_text)
            continue

        stat_key = m.group("stat").lower()
        # APEX-DISCOUNT-2026-10-16: "@WNBA:points" = a leg from another sport (often a
        # PrizePicks discount play). It is logged and counts toward the slip, but the
        # operator grades it by hand ("reese hit" / "reese miss") -- no tennis data.
        _sport = None
        if stat_key.startswith("@") and ":" in stat_key and len(stat_key.split(":", 1)[1]) > 0:
            _sport, stat_type = stat_key[1:].split(":", 1)
            _sport = _sport.upper() or None
        else:
            stat_type = STAT_ABBREV.get(stat_key)
        if not stat_type:
            errors.append(f"{leg_text}  (unrecognized stat '{m.group('stat')}')")
            continue

        prop_type = "standard"
        _ptype_src = None
        if m.group("demon"):
            prop_type = "red_demon"
        elif m.group("goblin"):
            prop_type = "goblin"
        elif m.group("direction").upper() == "MORE" and not _sport:
            # APEX-TYPECHECK-2026-10-14: no icon typed -- take the type from the
            # props DB instead of assuming Standard. LESS legs stay Standard
            # (Goblin/Demon lines are MORE only on PrizePicks).
            _db_type = _slip_type_from_props_db(m.group("player").strip(), stat_type, float(m.group("line")))
            if _db_type:
                prop_type = "red_demon" if _db_type == "demon" else _db_type
                _ptype_src = "props_db"

        entry = {
            "slip_id":     slip_id,
            "raw_text":    leg_text,
            "bet_type":    "prop",
            "player":      m.group("player").strip(),
            "direction":   m.group("direction").upper(),
            "line":        float(m.group("line")),
            "stat_type":   stat_type,
            "prop_type":   prop_type,
            "book":        None,
            "status":      "PENDING",
            "logged_at":   dates["ct_time"],
            "date":        dates["learn_on_date"],
            "resolved_at": None,
            "failure_category": None,
            "stake": stake,
            "potential_payout": potential_payout,
        }
        if _ptype_src:
            entry["prop_type_source"] = _ptype_src
        entry.update(_extra)
        if m.group("discount"):
            entry["discount"] = True
        if _sport:
            entry["sport"] = _extra.get("sport") or _sport
            entry["manual_grade"] = True
            entry.setdefault("stat_label", stat_type.replace("_", " ").title())
        if _check_duplicate_leg(entry, logged):   # APEX-SLIPDUP-2026-10-13: within this slip only
            entry["status"] = "DUPLICATE_SKIPPED"
            log_entries.append(entry)
            duplicates.append(leg_text)
            continue
        block_reason = _check_correlation(entry, slip_id, log_entries + logged)
        if block_reason:
            entry["status"] = "REJECTED_CORRELATION"
            entry["rejection_reason"] = block_reason
            log_entries.append(entry)
            blocked.append((leg_text, block_reason))
            continue
        log_entries.append(entry)
        logged.append(entry)

    # APEX-SLIPDUP-2026-10-13 (owner): a slip is a duplicate only when
    # another slip on the same day has the SAME picks and the SAME bet amount.
    # Sharing some picks with another slip is a different slip -- every pick
    # counts toward its own slip's result. (Was: any pick already logged that
    # day on any slip was skipped, so a $[amount] 6-pick lost 2 of its legs.)
    dup_of = _find_duplicate_slip(logged, slip_id, log_entries)
    if dup_of:
        for e in logged:
            e["status"] = "DUPLICATE_SKIPPED"
            e["duplicate_of"] = dup_of
            duplicates.append(e.get("raw_text") or "")
        logged = []

    _save_slip_log(log_entries)

    lines = []
    if dup_of:
        lines.append("🔁 Same picks and same bet amount as a slip already logged today -- not counted again.")
    if logged:
        lines.append(f"✅ Slip logged — {len(logged)} leg{'s' if len(logged)!=1 else ''} pending")
        for e in logged:
            tag = slip_leg_tags(e)   # APEX-DISCOUNT-2026-10-16: 🟢 Goblin / 🔴 Demon / 🏷️ Discount
            if e["bet_type"] == "moneyline":
                lines.append(f"  • {e['player']} ML {e['direction']}")
            else:
                lines.append(f"  • {e['player']} {e['direction']} {e['line']} {e.get('stat_label') or e['stat_type']}{tag}")
        _manual = [e for e in logged if e.get("manual_grade")]
        if _manual:
            _nm = (re.sub(r"\(.*?\)", " ", _manual[0]["player"]).split() or ["name"])[-1].lower()
            lines.append(f"🏷️ {len(_manual)} leg{'s' if len(_manual) != 1 else ''} graded by you (no stats feed for "
                         f"that sport): once it's over, reply `{_nm} hit`, `{_nm} miss` or `{_nm} push`.")
    if errors:
        lines.append(f"⚠️ Could not parse {len(errors)} leg(s):")
        for e in errors:
            lines.append(f"  • {e}")
    if blocked:
        lines.append(f"🚫 Blocked {len(blocked)} leg(s) by the correlation gate:")
        for leg_text, reason in blocked:
            lines.append(f"  • {leg_text}\n    {reason}")
    if duplicates and not dup_of:
        lines.append(f"🔁 Skipped {len(duplicates)} leg(s) repeated on this slip:")
        for leg_text in duplicates:
            lines.append(f"  • {leg_text}")
    return "\n".join(lines)


def get_market_breakdown(start_date: str = None, end_date: str = None) -> dict:
    """
    Fix (this session, approved): the prop-vs-moneyline ROI gap flagged
    in prior handoffs was scoped as "add a market_type column" — but
    that field already exists (bet_type, set to "moneyline" or "prop"
    on every slip leg since v7.9.6 and already used elsewhere for the
    correlation gate and failure-category selection). Re-checking the
    real slip_log.json structure before building anything redundant.

    What's ACTUALLY missing, confirmed by direct read: no stake or
    payout amount is captured anywhere in the slip log, for ANY market
    — so true dollar ROI does not exist today even in aggregate, let
    alone split by market. That's a separate, bigger gap (it would mean
    changing the Discord slip-entry format to capture a $ amount, a
    real behavior change worth discussing before building silently).

    What's buildable RIGHT NOW with data that already exists: a settled
    win/loss/push record and win rate, split by bet_type. This is a
    genuine, real answer to "how am I doing on props vs. moneyline" —
    just not denominated in dollars yet.

    Returns:
        {
          "prop":      {"wins": int, "losses": int, "push_void": int, "settled": int, "win_rate": float or None},
          "moneyline": {"wins": int, "losses": int, "push_void": int, "settled": int, "win_rate": float or None},
          "date_range": {"start": start_date, "end": end_date},
        }
    start_date/end_date filter on the "date" field (learn_on_date at
    entry time), inclusive, "YYYY-MM-DD" strings. None means unbounded.
    """
    entries = _load_slip_log()
    breakdown = {
        "prop":      {"wins": 0, "losses": 0, "push_void": 0},
        "moneyline": {"wins": 0, "losses": 0, "push_void": 0},
    }
    for e in entries:
        bet_type = e.get("bet_type")
        if bet_type not in breakdown:
            continue  # REJECTED_CORRELATION or any future bet_type — not a settled result
        entry_date = e.get("date")
        if start_date and entry_date and entry_date < start_date:
            continue
        if end_date and entry_date and entry_date > end_date:
            continue
        status = e.get("status")
        if status == "WIN":
            breakdown[bet_type]["wins"] += 1
        elif status == "LOSS":
            breakdown[bet_type]["losses"] += 1
        elif status in ("PUSH", "VOID"):
            breakdown[bet_type]["push_void"] += 1
        # PENDING and REJECTED_CORRELATION are not settled — excluded from the record

    for bet_type, rec in breakdown.items():
        settled = rec["wins"] + rec["losses"]
        rec["settled"]  = settled + rec["push_void"]
        rec["win_rate"] = round(rec["wins"] / settled, 4) if settled > 0 else None

    breakdown["date_range"] = {"start": start_date, "end": end_date}
    return breakdown


def _find_archived_prop_eval_id(player: str, stat_type: str, match_date: str):
    """
    Calibration tracking (this session). Twin of
    _find_archived_evaluated_line, same lookup, but returns eval_id
    instead of evaluated_line — needed so parse_slip_result can call
    mark_calibration_outcome() for props the same way it already does
    for moneyline (via _find_archived_ml_pick's eval_id). Kept as its
    own small function rather than changing _find_archived_evaluated_
    line's return shape and touching its existing, working call site.
    """
    archive_file = ARCHIVE_DIR / f"picks_{match_date}.json"
    if not archive_file.exists():
        return None
    try:
        with open(archive_file) as f:
            archived = json.load(f)
    except Exception:
        return None
    for pick in archived:
        if (player.lower() in (pick.get("player") or "").lower()
                and pick.get("stat_type") == stat_type):
            return pick.get("eval_id")
    return None


def _find_archived_evaluated_line(player: str, stat_type: str, match_date: str):
    """
    v7.9.7 fix (item 11): looks up the archived pick for this player +
    stat_type on match_date and returns its evaluated_line, or None if
    no archive file exists or no matching pick is found. Used by
    parse_slip_result to catch line movement between screenshot/
    evaluation time and actual bet-placement time.
    """
    archive_file = ARCHIVE_DIR / f"picks_{match_date}.json"
    if not archive_file.exists():
        return None
    try:
        with open(archive_file) as f:
            archived = json.load(f)
    except Exception:
        return None
    for pick in archived:
        if (player.lower() in (pick.get("player") or "").lower()
                and pick.get("stat_type") == stat_type):
            return pick.get("evaluated_line")
    return None


def _find_archived_ml_pick(player: str, match_date: str):
    """
    v7.9.7 (moneyline learning system): looks up the archived moneyline
    pick for this player on match_date, returning the full entry
    (signal breakdown, model reasoning, etc.) or None if not found.
    Used by parse_slip_result to feed the attribution classifier real
    data instead of just the slip log's bare player/result.
    """
    archive_file = ML_ARCHIVE_DIR / f"ml_picks_{match_date}.json"
    if not archive_file.exists():
        return None
    try:
        with open(archive_file) as f:
            archived = json.load(f)
    except Exception:
        return None
    for pick in archived:
        if player.lower() in (pick.get("player") or "").lower():
            return pick
    return None


def parse_slip_result(raw_text: str, dates: dict, weights: dict) -> str:
    """
    v7.9.6 (5E). Parses a slip result in the handoff's format:
        "Wang ✅ WIN | Chwalinska ❌ LOSS"
        "Sinner ML WIN — Rebets"
    Matches each leg against the most recent PENDING slip_log entry for
    that player (substring match, most recent first). On a LOSS, tags a
    failure category using the same classifier prompt as learning_agent
    (PROP_FAILURE_CATEGORIES / ML_FAILURE_CATEGORIES depending on bet
    type) and posts the result to #learning-agent so it's included in
    the next report — matching the handoff's "adds to daily learning
    report queue" behavior.
    """
    log_entries = _load_slip_log()
    if not log_entries:
        return "⚠️ No slip entries on file — nothing to resolve."

    # Moneyline result — single line, has "ML WIN/LOSS — Book"
    ml_match = _ML_RESULT_PATTERN.match(raw_text.strip())
    if ml_match and "|" not in raw_text:
        legs_raw = [raw_text.strip()]
    else:
        legs_raw = [l.strip() for l in raw_text.split("|") if l.strip()]

    resolved = []
    errors = []

    for leg_text in legs_raw:
        ml_m = _ML_RESULT_PATTERN.match(leg_text)
        if ml_m and ("ML" in leg_text.upper()):
            player_hint = ml_m.group("player").strip()
            result = ml_m.group("result").upper()
            book = ml_m.group("book").strip() or None
            candidates = [e for e in log_entries
                          if e["status"] == "PENDING" and e["bet_type"] == "moneyline"
                          and player_hint.lower() in e["player"].lower()]
        else:
            parsed = _parse_result_leg(leg_text)
            if not parsed:
                errors.append(leg_text)
                continue
            player_hint, result = parsed
            book = None
            candidates = [e for e in log_entries
                          if e["status"] == "PENDING" and e["bet_type"] == "prop"
                          and player_hint.lower() in e["player"].lower()]

        if not candidates:
            errors.append(f"{leg_text}  (no matching PENDING slip entry found)")
            continue

        # Most recent pending entry for this player
        candidates.sort(key=lambda e: e["logged_at"], reverse=True)
        entry = candidates[0]
        entry["status"]      = result
        entry["book"]        = book
        entry["resolved_at"] = dates["ct_time"]

        # v7.9.7 fix (item 11): line movement detection. Compare the
        # line actually logged at slip-entry time against the line this
        # pick was evaluated against (stamped in the archive at
        # evaluation time). A mismatch means the sportsbook's line moved
        # between screenshot and bet placement — the evaluation was for
        # a different number than what got bet. Flagged, not silently
        # graded as if nothing happened.
        if entry.get("bet_type") == "prop" and entry.get("line") is not None:
            archived_line = _find_archived_evaluated_line(
                entry["player"], entry["stat_type"], entry.get("date", dates["learn_on_date"])
            )
            if archived_line is not None and abs(archived_line - entry["line"]) > 0.01:
                entry["line_mismatch"] = True
                entry["evaluated_line"] = archived_line
                log("SlipResult", f"⚠️ LINE MISMATCH — {entry['player']} "
                    f"evaluated at {archived_line}, bet logged at {entry['line']}")
            else:
                entry["line_mismatch"] = False

            # Calibration tracking (this session): fires on WIN or LOSS
            # (PUSH/VOID aren't a real hit/miss signal for any rule, same
            # principle already applied on the moneyline side). Separate
            # lookup from the evaluated_line one above -- see
            # _find_archived_prop_eval_id's docstring for why it's kept
            # as its own function rather than changing the working
            # evaluated_line lookup's return shape.
            if result in ("WIN", "LOSS"):
                _prop_eval_id = _find_archived_prop_eval_id(
                    entry["player"], entry["stat_type"], entry.get("date", dates["learn_on_date"])
                )
                if _prop_eval_id:
                    mark_calibration_outcome(_prop_eval_id, result)
                    # APEX-PROPOUTCOME-2026-10-05 (Nightly Learning Audit,
                    # Board Scorecard Settlement Audit, owner):
                    # this is calibration-only tracking -- it never wrote
                    # Dataset A's evaluations.outcome column, which is what
                    # pick_results_view()/_rows_from_dataset_a("PROP") and
                    # the Nightly Learning Audit's hit/bust/pending counts
                    # actually read. The moneyline path a few lines below
                    # calls d1.record_outcome() for exactly this reason;
                    # props never had the equivalent call, so EVERY prop
                    # pick -- going back to Dataset A logging's start on
                    # 2026-07-23 -- showed as permanently "pending" no
                    # matter how long ago the match actually settled.
                    # Confirmed live: 2069 prop rows, 0 ever settled via
                    # this path, including picks from 2026-07-24. Not a
                    # date-filter bug and not legitimately-pending data --
                    # this call was simply missing. Mirrors the moneyline
                    # HIT/BUST mapping exactly; fail-open via d1's own
                    # contract (record_outcome never raises).
                    _prop_outcome = "HIT" if result == "WIN" else "BUST"
                    d1.record_outcome(_prop_eval_id, _prop_outcome, "win_loss_alert", _logger=log)

        if entry["bet_type"] == "moneyline":
            # v7.9.7 (moneyline learning system): dedicated attribution
            # path. Pulls the real signal breakdown archived at
            # evaluation time (not just the bare slip entry) so the
            # classifier can actually distinguish data vs. reasoning
            # vs. variance, per owner' spec. Runs on WIN too
            # (not just LOSS) — a HIT still needs to log which signal
            # drove it for the weight-proposal pool, even though only
            # BUSTs need a root_cause/failure_category at all.
            archived = _find_archived_ml_pick(entry["player"], entry.get("date", dates["learn_on_date"]))
            outcome = "HIT" if result == "WIN" else ("BUST" if result == "LOSS" else None)
            if outcome and archived and archived.get("eval_id"):
                d1.record_outcome(archived["eval_id"], outcome, "win_loss_alert", _logger=log)
                # Calibration tracking (this session): result is already
                # WIN/LOSS/PUSH/VOID at this point — feed it straight to
                # every provisional rule that fired on this eval_id
                # (e.g. the ML signal bands). Fail-open by construction.
                mark_calibration_outcome(archived["eval_id"], result)
            ml_entry_for_history = None
            if outcome and archived:
                context = {
                    "player":            entry["player"],
                    "outcome":           outcome,
                    "raw_breakdown":      archived.get("raw_breakdown"),
                    "weighted_breakdown": archived.get("weighted_breakdown"),
                    "largest_signal":     archived.get("largest_signal"),
                    "phase2_signals":     archived.get("phase2_signals"),
                    "model_reasoning":    (archived.get("model_reasoning") or "")[:1200],
                }
                classification = call_model(
                    MODEL_HAIKU, _build_ml_attribution_prompt(),
                    json.dumps(context, default=str)[:3000],
                    temperature=0.1, max_tokens=350
                )
                root_cause = None
                weight_signal = None
                if classification:
                    try:
                        clean = re.sub(r"```json|```", "", classification).strip()
                        parsed = json.loads(clean)
                        root_cause = parsed.get("root_cause")
                        entry["failure_category"] = parsed.get("failure_category")
                        weight_signal = parsed.get("weight_signal_implicated")
                    except Exception as _e:
                        # Audit fix 2.2 (this session): same bug class as
                        # the compliance parse-swallow fixed earlier --
                        # log the raw output that failed so the prompt
                        # can be fixed, instead of silently dropping it.
                        log("Learning", f"Classification JSON unparseable "
                            f"({_e}). Raw output: {classification[:300]}")
                entry["ml_root_cause"] = root_cause or ("HIT" if outcome == "HIT" else "Unclassified — review manually")
                # Bug fix (review pass): the fallback "weight_signal or
                # archived.get('largest_signal')" previously applied
                # unconditionally — meaning a BUST classified as
                # DATA_FAILURE (where weight_signal is correctly None
                # per the classifier's own instructions) would still
                # fall back to largest_signal and get treated as
                # learning-pool evidence, relying on
                # _append_ml_signal_history's downstream root_cause
                # check to filter it back out. That's backwards: the
                # gate belongs here, at the source, not as an implicit
                # dependency on a second function getting it right.
                # Correct behavior: a HIT has no root_cause to fail, so
                # it always credits a signal (classifier's pick, or the
                # archived largest_signal as fallback). A BUST only
                # gets a signal_for_history when root_cause is
                # explicitly REASONING_FAILURE — DATA_FAILURE and
                # VARIANCE busts get NO signal here at all, full stop.
                if outcome == "HIT":
                    signal_for_history = weight_signal or archived.get("largest_signal")
                elif root_cause == "REASONING_FAILURE":
                    signal_for_history = weight_signal or archived.get("largest_signal")
                else:
                    signal_for_history = None
                if signal_for_history:
                    ml_entry_for_history = {
                        "signal":     signal_for_history,
                        "outcome":    outcome,
                        "color":      archived.get("status", "").upper() if archived.get("status") else None,
                        "root_cause": root_cause,
                    }
            if ml_entry_for_history:
                _build_ml_weight_proposals([ml_entry_for_history], weights, dates)

        elif result == "LOSS":
            failure_categories = PROP_FAILURE_CATEGORIES
            classification = call_model(
                MODEL_HAIKU, _build_learning_prompt(failure_categories),
                f"Pick: {json.dumps(entry, default=str)[:400]}\nOutcome: BUST",
                temperature=0.1, max_tokens=300
            )
            category = None
            if classification:
                try:
                    clean = re.sub(r"```json|```", "", classification).strip()
                    parsed = json.loads(clean)
                    category = parsed.get("failure_category")
                except Exception as _e:
                    # Audit fix 2.2 (this session): twin of the props
                    # classification parse-swallow above -- log it.
                    log("Learning", f"ML classification JSON unparseable "
                        f"({_e}). Raw output: {classification[:300]}")
            entry["failure_category"] = category or "Unclassified — review manually"

        resolved.append(entry)

    _save_slip_log(log_entries)

    lines = []
    if resolved:
        for e in resolved:
            emoji = "✅" if e["status"] == "WIN" else ("❌" if e["status"] == "LOSS" else "➖")
            cat_note = f" — tagged: {e['failure_category']}" if e.get("failure_category") else ""
            root_cause_note = f" [{e['ml_root_cause']}]" if e.get("ml_root_cause") else ""
            mismatch_note = (f" ⚠️ LINE MISMATCH (evaluated {e['evaluated_line']}, bet logged {e['line']})"
                              if e.get("line_mismatch") else "")
            lines.append(f"📊 {e['player']} — {e['status']} {emoji}{cat_note}{root_cause_note}{mismatch_note}")
            if e["status"] == "LOSS":
                send_discord("learning",
                    f"📉 **SLIP LOSS LOGGED** — {dates['ct_time']}\n"
                    f"{e['player']} {e.get('direction','')} {e.get('line','')} "
                    f"{e.get('stat_type') or 'ML'}\n"
                    f"Failure category: **{e['failure_category']}**"
                    + (f"\nRoot cause: **{e['ml_root_cause']}**" if e.get("ml_root_cause") else "")
                    + f"\nQueued for next learning report."
                )
    if errors:
        lines.append(f"⚠️ Could not resolve {len(errors)} leg(s):")
        for e in errors:
            lines.append(f"  • {e}")
    if not resolved and not errors:
        return "⚠️ No legs found in result text — check the format and try again."
    return "\n".join(lines)

# ================================================================
# PENDING PICK RETRY ENGINE (APEX-RETRYENGINE-2026-10-05)
# ================================================================
# Root cause (API-First Settlement Audit, owner, 2026-10-05):
# a pick that failed to resolve on its FIRST night was written to
# PENDING_FILE and never checked again — the only other code that
# ever touched that file was the old age-based quarantine below
# (3 days, then give up, no second look). A live test against this
# exact backlog found 34/50 (68%) of sampled pending picks already
# had a usable result sitting in the Sports Results API (V8) on a
# single retry — they were never missing data, just never asked
# twice. This block adds a real nightly retry pass instead of a
# one-shot check + timer.
# ================================================================

_RETRY_MAX_ATTEMPTS = 7   # consecutive failed nightly retries before quarantine

def _norm_name_for_match(s: str) -> str:
    """Lowercase / accent-stripped / punctuation-light form, so two
    spellings of the same name compare equal."""
    if not s:
        return ""
    try:
        s = v8_strip_accents(s)
    except Exception:
        pass
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return " ".join(s.split())

def _opponent_name_matches(target_opponent: str, api_opponent: str) -> bool:
    """Enhanced opponent matching for the retry engine — wider than the
    plain substring check verify_result_via_v8() uses (that function is
    left untouched for same-night settlement), so a name that's merely
    FORMATTED differently (reordered, accenting, a dropped middle name)
    isn't treated as 'not found'. Still requires the last name to agree
    either exactly or by a tight edit-distance, and the first name to
    agree the same way — same safety principle as resolve_player_name's
    own same-surname-stranger protection in openclaw_v8_api.py. This
    never relaxes far enough to accept a genuinely different person."""
    a = _norm_name_for_match(target_opponent)
    b = _norm_name_for_match(api_opponent)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    a_tok, b_tok = a.split(), b.split()
    if not a_tok or not b_tok:
        return False
    if a_tok[-1] != b_tok[-1]:
        return False
    a_first, b_first = a_tok[0], b_tok[0]
    if a_first == b_first:
        return True
    ratio = difflib.SequenceMatcher(None, a_first, b_first).ratio()
    approx_distance = round((1 - ratio) * max(len(a_first), len(b_first)))
    return approx_distance <= 2

def _name_collision_check(input_name: str, canonical_name: str) -> bool:
    """Name-collision protection (approved design, 2026-10-05): returns
    True if what resolve_player_name() handed back looks like a
    DIFFERENT person than the name we asked about, not just a spelling
    difference — e.g. 'Yuhan Wang' -> 'Chuhan Wang' or 'Peyton Stearns'
    -> 'Preston Stearns' (both real cases confirmed live in the audit,
    both produced by openclaw_v8_api.py's own fuzzy-match safety net,
    which logs a loud warning but nothing downstream ever checked for
    it). When this fires, the pick must NOT be auto-settled — it goes
    to the manual review queue instead.

    Deliberately strict (exact first-name match only, after accent/case
    normalization) rather than another edit-distance check: 'Yuhan' vs
    'Chuhan' and 'Peyton' vs 'Preston' both sit within the SAME small
    edit-distance budget V8's own fuzzy-match safety net already uses
    (that budget is exactly why it accepted them) — a second fuzzy check
    with a similar tolerance would just rubber-stamp the same mistake.
    A real short-form (e.g. 'Cam' for 'Cameron') will get caught by this
    too and routed to manual review — that's an intentional trade: per
    the approved design, a pick should never auto-settle on a guessed
    identity, and a human confirming a true near-miss costs far less
    than silently grading the wrong player."""
    a = _norm_name_for_match(input_name)
    b = _norm_name_for_match(canonical_name)
    if not a or not b:
        return False
    a_tok, b_tok = a.split(), b.split()
    if not a_tok or not b_tok:
        return False
    return a_tok[0] != b_tok[0]   # any first-name difference -> collision


# APEX-NAMECOLLISION-2026-10-22: the retry engine used to try the men's
# tour first for every name, so a WTA player whose name sits close to a
# man's (Ann Li -> Alan Li, Peyton Stearns -> Preston Stearns) got the
# man back from the API's fuzzy search and was flagged as a collision.
# True = names in the local WTA reference (data/known_player_names.json)
# search the women's side first, and a different-looking first name on
# one side tries the other side before flagging. False = old atp-first.
RESOLVE_TOUR_BY_REFERENCE = True
# True = one ❓ alert per match listing its picks (was one per pick).
COLLISION_ALERT_GROUPED = True


def _tour_order(name: str):
    """['wta', 'atp'] for a name only the WTA reference knows, else the
    old ['atp', 'wta']."""
    if not RESOLVE_TOUR_BY_REFERENCE or not name:
        return ["atp", "wta"]
    try:
        from openclaw_v8_api import _load_known_names
        known = _load_known_names()
    except Exception:
        return ["atp", "wta"]
    key = " ".join(name.lower().split())
    if key in known.get("wta", {}) and key not in known.get("atp", {}):
        return ["wta", "atp"]
    return ["atp", "wta"]


def _resolve_tour_checked(name: str):
    """Resolve a name for the retry engine. Returns (canonical, tour).
    With RESOLVE_TOUR_BY_REFERENCE on, if the first tour's answer has a
    different first name, the other tour is tried and a clean answer
    there wins; otherwise the first answer is returned so the
    name-collision guard still fires on it."""
    first = None
    for t in _tour_order(name):
        try:
            c = v8_resolve_player_name(name, t)
        except Exception:
            c = None
        if not c:
            continue
        if not RESOLVE_TOUR_BY_REFERENCE or not _name_collision_check(name, c):
            return c, t
        if first is None:
            first = (c, t)
    return first if first else (None, None)


def _flip_match_perspective(match: dict) -> dict:
    """
    APEX-OPPFALLBACK-2026-10-05: takes a match dict that get_recent_matches()
    returned for the OPPONENT's own profile (so every field in it is from
    the opponent's point of view) and flips it to the original player's
    point of view, so the existing stat-extraction code downstream
    (_STAT_TYPE_V8_FIELD / _stat_from_v8_box_score) can read it exactly
    like a normal result without any special-casing.

    Why this exists: live-tested against the real pending backlog
    (2026-10-05) — confirmed cases like Carlos Alcaraz vs Alex Michelsen
    (2026-10-01): Alcaraz's own matches-played profile does NOT contain
    this match (hence "opponent_not_in_recent_window"), but Michelsen's
    profile DOES contain it. Sampling 60 real stuck picks, 15 (25%) were
    recoverable this way. The vendor's per-player match coverage is
    gappy in a way that differs player-by-player for the SAME match, not
    a simple same-day lag — so checking the other side of the court is a
    real, additional source of truth, not a guess.
    """
    return {
        "date":           match.get("date"),
        "opponent":       None,  # not needed downstream; this IS the player now
        "score":          match.get("score"),
        "surface":        match.get("surface"),
        "tournament":     match.get("tournament"),
        "won":            (not match["won"]) if match.get("won") is not None else None,
        "won_verified":   match.get("won_verified"),
        "set_count":      match.get("set_count"),
        "extended":       match.get("extended"),
        "retired":        match.get("retired"),
        "total_games":    match.get("total_games"),
        "player_games":   match.get("opponent_games"),
        "opponent_games": match.get("player_games"),
        "player_stats":   match.get("opponent_stats", {}) or {},
        "opponent_stats": match.get("player_stats", {}) or {},
    }


def _recent_match_via_opponent_profile(player: str, opponent: str, target_date: str):
    """
    APEX-OPPFALLBACK-2026-10-05: fallback lookup that fetches the
    OPPONENT's own matches-played history (instead of the subject
    player's) and looks for the subject player showing up as the
    opponent on the target date. Returns a flipped match dict (see
    _flip_match_perspective) ready for normal stat extraction, or None
    if the opponent's own profile doesn't have it either.
    """
    if not opponent:
        return None
    canonical_opp, _ = _resolve_tour_checked(opponent)
    if not canonical_opp:
        return None
    try:
        opp_recent = v8_get_recent_matches(canonical_opp, surface=None, limit=25)
    except Exception:
        return None
    if not opp_recent:
        return None
    candidates = [m for m in opp_recent if _opponent_name_matches(player, m.get("opponent", ""))]
    if not candidates:
        return None
    match = None
    for m in candidates:
        if m.get("date") == target_date:
            match = m
            break
    if match is None:
        match = candidates[0]
    return _flip_match_perspective(match)


def _retry_pick_via_api(pick, dates):
    """
    Retry steps 1 + 2 (Sports Results API + enhanced match resolution).
    Returns a dict with status one of:
      "SETTLED"               — actual/outcome/source are filled in
                                 (or non_completion=True for a retirement)
      "REQUIRES_CONFIRMATION" — name-collision guard fired; do not settle
      "NOT_FOUND"             — failure_reason explains why
    """
    player    = pick.get("player", "")
    opponent  = pick.get("opponent", "")
    stat_type = pick.get("stat_type", "")
    if not player:
        return {"status": "NOT_FOUND", "failure_reason": "player_not_found"}
    if not _V8_AVAILABLE:
        return {"status": "NOT_FOUND", "failure_reason": "api_unavailable"}

    canonical, tour_used = _resolve_tour_checked(player)

    if not canonical:
        return {"status": "NOT_FOUND", "failure_reason": "player_not_found"}

    if _name_collision_check(player, canonical):
        return {"status": "REQUIRES_CONFIRMATION",
                "failure_reason": "name_collision", "canonical": canonical}

    try:
        # Wider window than verify_result_via_v8()'s limit=8, and no
        # surface filter (a surface-string mismatch between our pick
        # and the API's own tournament-court naming was silently
        # excluding real matches) — this is the "enhanced match
        # resolution" step, not just a repeat of the same-night check.
        recent = v8_get_recent_matches(canonical, surface=None, limit=25)
    except Exception:
        return {"status": "NOT_FOUND", "failure_reason": "api_timeout",
                "canonical": canonical}

    target_date = pick.get("match_date") or dates.get("learn_on_date", "")

    via_opponent_profile = False

    if not recent:
        # APEX-OPPFALLBACK-2026-10-05: before giving up, try the
        # opponent's own profile — the vendor's per-player match
        # coverage is gappy in a way that is NOT symmetric between the
        # two players in the same match (live-confirmed 2026-10-05).
        match = _recent_match_via_opponent_profile(player, opponent, target_date)
        if match is None:
            return {"status": "NOT_FOUND", "failure_reason": "match_not_yet_available",
                    "canonical": canonical}
        via_opponent_profile = True
    else:
        candidates = [m for m in recent if _opponent_name_matches(opponent, m.get("opponent", ""))]
        if not candidates:
            match = _recent_match_via_opponent_profile(player, opponent, target_date)
            if match is None:
                return {"status": "NOT_FOUND", "failure_reason": "opponent_not_in_recent_window",
                        "canonical": canonical}
            via_opponent_profile = True
        else:
            match = None
            for m in candidates:
                if m.get("date") == target_date:
                    match = m
                    break
            if match is None:
                match = candidates[0]

    if match.get("retired"):
        return {"status": "SETTLED", "actual": None, "outcome": None,
                "source": "V8", "non_completion": True, "canonical": canonical,
                "via_opponent_profile": via_opponent_profile}

    actual = None
    if stat_type in _STAT_TYPE_V8_FIELD:
        actual = match.get(_STAT_TYPE_V8_FIELD[stat_type])
    else:
        actual = _stat_from_v8_box_score(stat_type, match)

    if actual is None:
        return {"status": "NOT_FOUND",
                "failure_reason": "opponent_not_in_recent_window" if via_opponent_profile else "match_not_yet_available",
                "canonical": canonical}

    outcome = _determine_outcome(actual, pick)
    return {"status": "SETTLED", "actual": actual, "outcome": outcome,
            "source": "V8", "canonical": canonical,
            "via_opponent_profile": via_opponent_profile}


def _retry_pick_via_serper(pick, dates):
    """Retry step 3 — Serper, only reached once the API has genuinely
    returned nothing usable for this pick."""
    player     = pick.get("player", "")
    opponent   = pick.get("opponent", "")
    tournament = pick.get("tournament", "")
    match_date = pick.get("match_date") or dates.get("learn_on_date", "")
    result_text = web_search(f"{player} vs {opponent} {tournament} result score {match_date}")
    if not result_text:
        return {"status": "NOT_FOUND", "failure_reason": "serper_unavailable"}
    result_type, signal = classify_result(result_text)
    if result_type == "NON_COMPLETION":
        return {"status": "SETTLED", "actual": None, "outcome": None,
                "source": "web_search", "non_completion": True, "signal": signal}
    if result_type != "COMPLETED":
        return {"status": "NOT_FOUND", "failure_reason": "unknown"}
    actual = _extract_stat_result(result_text, pick)
    if actual is None:
        return {"status": "NOT_FOUND", "failure_reason": "unknown"}
    outcome = _determine_outcome(actual, pick)
    return {"status": "SETTLED", "actual": actual, "outcome": outcome, "source": "web_search"}


_STALE_EVENT_HOURS = 48          # APEX-HARDEN-2026-10-07
_IDENTICAL_FAILURE_QUARANTINE = 3  # APEX-HARDEN-2026-10-07

def _match_age_hours(match_date_str, today_str):
    """Approximate hours since match_date, using calendar-day distance
    from today_str (no intraday precision in either timestamp, so this
    is a conservative floor -- a match_date 2 calendar days back reads
    as >=48h even if it was actually settled late the next day)."""
    try:
        d_match = date.fromisoformat(match_date_str)
        d_today = date.fromisoformat(today_str)
    except (TypeError, ValueError):
        return 0
    return max(0, (d_today - d_match).days) * 24


def retry_pending_engine(dates, weights):
    """
    Nightly pass over the ENTIRE pending-picks backlog (not just the
    picks evaluated today — that gap is the root cause this closes).
    Retry hierarchy per pick: Sports Results API (V8, wider window +
    enhanced opponent matching) -> Serper (last resort) -> stay pending
    -> quarantine after _RETRY_MAX_ATTEMPTS consecutive failed nights,
    OR after _IDENTICAL_FAILURE_QUARANTINE consecutive nights with the
    SAME failure reason (APEX-HARDEN-2026-10-07 -- a pick failing for a
    new/changing reason each night still deserves more attempts; one
    stuck on the identical reason 3 nights running is known-dead and
    should stop being counted as pending).
    A name-collision guess is never auto-settled — it goes to the
    manual review queue instead. Sends one Settlement Health Report to
    Discord when the pass is done.
    """
    if not PENDING_FILE.exists():
        return {"pending_before": 0, "retried": 0, "resolved_api": 0,
                "resolved_serper": 0, "still_pending": 0, "manual_review": 0}

    with open(PENDING_FILE) as f:
        pending = json.load(f)

    manual_review = []
    if MANUAL_REVIEW_FILE.exists():
        with open(MANUAL_REVIEW_FILE) as f:
            manual_review = json.load(f)

    today_str = dates.get("learn_on_date", "") or str(date.today())

    pending_before   = len(pending)
    still_pending    = []
    resolved_api     = 0
    resolved_serper  = 0
    non_completions  = 0
    new_manual_review = 0
    quarantined      = 0
    quarantined_identical = 0
    reclassified_stale = 0
    collision_groups = {}   # APEX-NAMECOLLISION-2026-10-22: one ❓ per match

    for pick in pending:
        pick.setdefault("first_seen_date", pick.get("match_date", today_str))
        prev_failure_reason = pick.get("last_failure_reason")
        pick["last_retry_date"] = today_str
        pick["retry_count"] = pick.get("retry_count", 0) + 1

        # APEX-LEARNV2-2026-10-24: stored results (props_db, settled hourly from
        # SportsAPI365) first; the results API only when props_db has nothing.
        api_result = None
        try:
            import apex_learning_v2 as _alv2
            _c = _alv2._conn()
            _st, _info = _alv2.lookup_result(_c, pick.get("player"), pick.get("opponent"), pick.get("stat_type"),
                                             pick.get("line"), pick.get("direction"),
                                             match_date=pick.get("match_date"),
                                             prop_type=pick.get("prop_type_classified"))
            _c.close()
            if _st in ("HIT", "BUST"):
                api_result = {"status": "SETTLED", "actual": (_info or {}).get("actual"), "outcome": _st,
                              "source": "props_db"}
            elif _st == "VOID":
                api_result = {"status": "SETTLED", "actual": None, "outcome": None, "source": "props_db",
                              "non_completion": True, "signal": "void in props_db (retired / walkover)"}
        except Exception as _e:
            log("RetryEngine", f"props_db lookup failed (non-fatal): {type(_e).__name__}: {_e}")
        if api_result is None:
            api_result = _retry_pick_via_api(pick, dates)

        if api_result["status"] == "REQUIRES_CONFIRMATION":
            pick["last_failure_reason"] = api_result["failure_reason"]
            pick["resolution_source"] = None
            manual_review.append(pick)
            new_manual_review += 1
            if COLLISION_ALERT_GROUPED:
                _ck = (pick.get("player"), pick.get("opponent"), pick.get("match_date"))
                collision_groups.setdefault(_ck, {"canonical": api_result.get("canonical"),
                                                  "picks": []})["picks"].append(pick)
                continue
            send_discord("alerts",
                f"❓ **REQUIRES CONFIRMATION** — name-collision guard\n"
                f"Pick: {pick.get('player')} vs {pick.get('opponent')} "
                f"{pick.get('direction')} {pick.get('line')} {pick.get('stat_type')}\n"
                f"API resolved the player to a different-looking name "
                f"(**{api_result.get('canonical')}**) — not auto-settling. "
                f"Please confirm manually."
            )
            continue

        result = api_result
        source_label = "props_db" if api_result.get("source") == "props_db" else "V8"
        if result["status"] != "SETTLED":
            # APEX-SERPERDISABLE-2026-10-05: Serper step removed from the
            # retry hierarchy entirely (not just made inert) -- Serper is
            # permanently out of credits and not being renewed, so there
            # is no real "step 3" to attempt here any more. A pick the
            # API can't resolve goes straight to the retry-count check.
            # (_retry_pick_via_serper() is left in place, unused, in case
            # a funded key is ever supplied again later.)
            failure_reason = api_result.get("failure_reason")

            # APEX-HARDEN-2026-10-07 item 3: a pick whose match happened
            # >=48h ago has no business still reading as "just waiting
            # for the event to finish" -- relabel so reporting doesn't
            # keep optimistically treating it as timing, not a real
            # matching failure.
            age_hours = _match_age_hours(pick.get("match_date", ""), today_str)
            if (failure_reason in ("match_not_yet_available", "opponent_not_in_recent_window")
                    and age_hours >= _STALE_EVENT_HOURS):
                pick["stale_original_reason"] = failure_reason
                failure_reason = "stale_unresolved_48h"
                reclassified_stale += 1

            # APEX-HARDEN-2026-10-07 item 4: failure-aware streak --
            # same reason as last night extends the streak, a different
            # (or new) reason resets it.
            if failure_reason == prev_failure_reason:
                pick["consecutive_identical_failures"] = pick.get("consecutive_identical_failures", 0) + 1
            else:
                pick["consecutive_identical_failures"] = 1

            pick["last_failure_reason"] = failure_reason
            pick["resolution_source"] = None

            if pick["retry_count"] >= _RETRY_MAX_ATTEMPTS:
                _quarantine_pick(pick, {"type": "UNRESOLVABLE",
                    "signal": f"{_RETRY_MAX_ATTEMPTS} consecutive failed retries "
                              f"({pick['last_failure_reason']})"}, dates)
                quarantined += 1
            elif pick["consecutive_identical_failures"] >= _IDENTICAL_FAILURE_QUARANTINE:
                _quarantine_pick(pick, {"type": "UNRESOLVABLE",
                    "signal": f"{pick['consecutive_identical_failures']} consecutive nights with "
                              f"the identical failure reason ({pick['last_failure_reason']})"}, dates)
                quarantined += 1
                quarantined_identical += 1
            else:
                still_pending.append(pick)
            continue

        pick["resolution_source"] = source_label
        pick["consecutive_identical_failures"] = 0

        if result.get("non_completion"):
            non_completions += 1
            _quarantine_pick(pick, {"type": "NON_COMPLETION",
                "signal": result.get("signal", "retirement (confirmed on retry)")}, dates)
            send_discord("alerts",
                f"⚠️ **NON-COMPLETION** (retry engine) — {dates.get('ct_time','')}\n"
                f"Player: {pick.get('player')} | Pick: {pick.get('direction')} {pick.get('line')}\n"
                f"**Excluded from learning — weights unchanged**"
            )
            continue

        eval_id = pick.get("eval_id")
        outcome = result["outcome"]
        actual  = result["actual"]
        if eval_id:
            # Dataset A's outcome_source CHECK only allows its own names
            d1.record_outcome(eval_id, outcome,
                              "pending_results_json" if source_label == "props_db" else source_label, _logger=log)

        if source_label in ("V8", "props_db"):
            resolved_api += 1
        else:
            resolved_serper += 1

        _emoji = "✅ WIN" if outcome == "HIT" else "❌ LOSS"
        log("Results",   # APEX-PICKSCLEAN-2026-10-24: was a per-pick #alerts post
            f"{_emoji} — {pick.get('player','?')} {pick.get('direction','?')} "
            f"{pick.get('line','?')} {pick.get('stat_type','?')} "
            f"(graded {pick.get('confidence', 0):.0%} {pick.get('color','?')}) "
            f"vs {pick.get('opponent','?')} | actual: {actual} | "
            f"source: {source_label} (retry #{pick['retry_count']})"
        )

    for (_pl, _opp, _md), _g in collision_groups.items():
        _lines = [f"• {p.get('direction')} {p.get('line')} {p.get('stat_type')}"
                  for p in _g["picks"]]
        send_discord("alerts",
            f"❓ **REQUIRES CONFIRMATION** — name-collision guard\n"
            f"Match: {_pl} vs {_opp} ({_md}) · {len(_g['picks'])} pick(s)\n"
            f"The results API matched {_pl} to a different-looking name "
            f"(**{_g['canonical']}**), so these are not auto-settled:\n"
            + "\n".join(_lines[:15])
            + (f"\n…and {len(_lines) - 15} more" if len(_lines) > 15 else "")
            + "\nPlease confirm manually."
        )

    with open(PENDING_FILE, "w") as f:
        json.dump(still_pending, f, indent=2, default=str)
    with open(MANUAL_REVIEW_FILE, "w") as f:
        json.dump(manual_review, f, indent=2, default=str)

    resolved_total = resolved_api + resolved_serper
    rate = (resolved_total / pending_before * 100) if pending_before else 0.0

    # APEX-HARDEN-2026-10-07 item 6: new headline metrics.
    active_pending = len(still_pending)
    truly_resolvable = sum(1 for p in still_pending
                            if p.get("last_failure_reason") != "stale_unresolved_48h")
    quarantine_total = 0
    if QUARANTINE_FILE.exists():
        with open(QUARANTINE_FILE) as f:
            quarantine_total = len(json.load(f))

    # APEX-LEARNV2-2026-10-24: this block went to #learning, and because of
    # "="*38 + "\n" "🏁 ..." (Python glues the strings together BEFORE the *38)
    # its middle was repeated 38 times -- 9 Discord messages a night. It now goes
    # once, to data/logs/learning_health.log; the 11:30 PM card carries one
    # housekeeping line.
    _health = ("SETTLEMENT HEALTH\n"
        f"Active Pending: {active_pending}\n"
        f"Truly Resolvable: {truly_resolvable}\n"
        f"Quarantined: {quarantine_total} (+{quarantined} tonight, "
        f"{quarantined_identical} via identical-failure rule)\n"
        f"Resolution Rate: {rate:.1f}%\n"
        f"Pending Queue (pre-retry): {pending_before}\n"
        f"Resolved (props_db or API): {resolved_api}\n"
        f"Resolved Via Serper: {resolved_serper} (disabled 2026-10-05)\n"
        f"Non-completions: {non_completions}\n"
        f"Reclassified stale (>=48h): {reclassified_stale}\n"
        f"Manual Review: {len(manual_review)} (+{new_manual_review} tonight)")
    try:
        import apex_learning_v2 as _alv2
        _alv2.health_log(_health)
    except Exception as _e:
        log("RetryEngine", f"health log write failed: {_e}")

    log("RetryEngine", f"pending_before={pending_before} resolved_api={resolved_api} "
        f"resolved_serper={resolved_serper} active_pending={active_pending} "
        f"truly_resolvable={truly_resolvable} manual_review_new={new_manual_review} "
        f"quarantined={quarantined} quarantined_identical={quarantined_identical} "
        f"reclassified_stale={reclassified_stale}")

    return {"pending_before": pending_before, "retried": pending_before,
            "resolved_api": resolved_api, "resolved_serper": resolved_serper,
            "still_pending": active_pending, "active_pending": active_pending,
            "truly_resolvable": truly_resolvable, "manual_review": len(manual_review),
            "quarantined": quarantined, "quarantined_total": quarantine_total,
            "non_completions": non_completions, "reclassified_stale": reclassified_stale}


def _quarantine_pick(pick, result, dates):
    q = []
    if QUARANTINE_FILE.exists():
        with open(QUARANTINE_FILE) as f:
            q = json.load(f)
    q.append({"date": dates["learn_on_date"], "player": pick.get("player"),
              "pick": f"{pick.get('direction')} {pick.get('line')} {pick.get('stat_type')}",
              "result_type": result.get("type"), "signal": result.get("signal"),
              "status": "EXCLUDED_FROM_LEARNING"})
    with open(QUARANTINE_FILE,"w") as f:
        json.dump(q, f, indent=2)

def _pending_key(entry):
    """
    APEX-HARDEN-2026-10-07: identity of a pending pick for duplicate
    detection. Deliberately excludes eval_id -- two separate pipeline
    evaluations of the SAME real-world prop (same player/opponent/
    stat/line/direction/match_date) are duplicates even though each
    carries its own eval_id (confirmed live: 213 duplicate groups /
    426 entries in the pre-hardening backlog were exactly this shape).
    """
    return (entry.get("player"), entry.get("opponent"), entry.get("stat_type"),
            entry.get("line"), entry.get("direction"), entry.get("match_date"))


def _add_to_pending(pick, dates):
    """
    Returns True if the pick was added, False if an identical pick
    (same _pending_key) was already in the queue and this add was
    skipped (APEX-HARDEN-2026-10-07 unique-key protection).
    """
    p = []
    if PENDING_FILE.exists():
        with open(PENDING_FILE) as f:
            p = json.load(f)
    new_entry = {**pick, "match_date": dates["learn_on_date"], "added": dates["ct_time"]}
    new_key = _pending_key(new_entry)
    if any(_pending_key(existing) == new_key for existing in p):
        log("Learning",
            f"Duplicate prevented: {pick.get('player')} vs {pick.get('opponent')} "
            f"{pick.get('direction')} {pick.get('line')} {pick.get('stat_type')} "
            f"({new_entry['match_date']})")
        return False
    p.append(new_entry)
    with open(PENDING_FILE,"w") as f:
        json.dump(p, f, indent=2)
    return True

def _extract_stat_result(text, pick):
    numbers = re.findall(r'\b\d+(?:\.\d+)?\b', text or "")
    return float(numbers[0]) if numbers else None

def _determine_outcome(actual, pick):
    if actual is None:
        return "UNKNOWN"
    line      = float(pick.get("line", 0))
    direction = pick.get("direction","LESS")
    if direction == "LESS":
        return "HIT" if actual < line else "BUST"
    return "HIT" if actual > line else "BUST"

def _send_learning_report(results, non_completions, pending, dates, weights,
                           duplicates_prevented=0):
    hits  = [r for r in results if r.get("outcome") == "HIT"]
    busts = [r for r in results if r.get("outcome") == "BUST"]

    msg = [
        "═"*38,
        f"🧠 **NIGHTLY LEARNING — {dates['learn_on_date']}**",
        "═"*38,
        f"Results: {len(hits)} hits | {len(busts)} busts | "
        f"{len(non_completions)} non-completions | {len(pending)} pending",
        f"Duplicate Prevented: {duplicates_prevented}",
        "",
    ]

    if busts:
        msg.append("**BUSTS:**")
        for b in busts:
            c = b.get("classified",{})
            src = b.get("result_source", "?")
            msg.append(
                f"• {b.get('player')} {b.get('direction')} {b.get('line')} "
                f"{b.get('stat_type')} (source: {src})\n"
                f"  Category: {c.get('failure_category','?')} | Rule: {c.get('rule_violated','?')}\n"
                f"  Signal: {c.get('most_predictive_signal','?')}"
            )
        msg.append("")

    if hits:
        msg.append("**HITS:**")
        for h in hits:
            c = h.get("classified",{})
            msg.append(
                f"• {h.get('player')} {h.get('direction')} {h.get('line')} — "
                f"{c.get('most_predictive_signal','?')}"
            )
        msg.append("")

    new_proposals = _build_weight_proposals(results, weights, dates)
    all_proposals, _added = _save_proposals(new_proposals, dates)
    still_pending = [p for p in all_proposals if p["status"] == "PENDING"]
    stale = _stale_pending_proposals(all_proposals, dates)

    if still_pending:
        msg.append("**PENDING WEIGHT PROPOSALS (all awaiting your decision):**")
        for p in still_pending:
            age_note = "" if p["batch_date"] == dates["learn_on_date"] else f" — ⚠️ from {p['batch_date']}, still unresolved"
            msg.append(
                f"**Proposal {p['id']}:** `{p['signal']}`{age_note}\n"
                f"Current: {p['current']:.3f} → Proposed: {p['proposed']:.3f}\n"
                f"Advantage: {p['advantage']}\n"
                f"Risk: {p['risk']}\n"
                f"Run: `python3 openclaw_core.py --approve {p['id']}` or `--reject {p['id']}`"
            )
        if stale:
            msg.append(
                f"\n🔴 **{len(stale)} proposal(s) have been waiting more than one "
                f"day for a decision.** Nothing is lost or overwritten — they stay "
                f"pending until you act — but this is a repeat reminder every "
                f"night until resolved."
            )
    else:
        msg.append("No weight changes proposed.")

    if non_completions:
        msg.append(f"\nNon-completions (excluded from learning): {len(non_completions)}")
        for nc in non_completions:
            msg.append(f"• {nc.get('player')} — excluded, weights unchanged")

    msg.append(
        "\n**Weights will NOT update until you approve.** "
        "Replying in Discord does nothing yet (no live listener — "
        "Step 9). Run in Terminal:\n"
        "`python3 openclaw_core.py --approve N` or `--reject N`"
    )
    # APEX-LEARNV2-2026-10-24: this long report (with "Category: ? | Rule: ?" lines)
    # went to #learning every night. It now goes to data/logs/learning_health.log;
    # #learning gets one clean card at 11:30 PM (apex_learning_v2.nightly).
    try:
        import apex_learning_v2 as _alv2
        _alv2.health_log("11 PM learning report\n" + "\n".join(msg))
    except Exception as _e:
        log("Learning", f"learning report log write failed: {_e}")

def _append_signal_history(results, dates):
    """
    v7.9.6 fix: classified outcomes (HIT/BUST + which signal drove them)
    were computed fresh every learning_agent() run and never saved —
    meaning there was no way to accumulate evidence across nights at
    all. Every graded pick's signal + outcome now gets appended here,
    so weight proposals can be based on real, pooled, multi-night
    evidence instead of a single night's results.

    v7.9.6 (GREEN-only gate, operator direction): only picks that were
    graded GREEN (confidence >= 70%, per the color assignment in
    scoring_agent) are eligible to feed the weight-proposal learning
    pool — whether or not the pick was actually bet. YELLOW and RED
    picks are excluded from THIS pool, but nothing about them is
    deleted — they stay in the picks archive forever, and every single
    loss (any color) still gets its failure-category tag regardless,
    since that per-pick review is a separate system with no threshold.
    This keeps weight-change proposals grounded in the system's own
    best-quality evaluations, not its weaker or thinner-data calls.
    """
    if not SIGNAL_HISTORY_FILE.exists():
        history = []
    else:
        with open(SIGNAL_HISTORY_FILE) as f:
            history = json.load(f)

    skipped_non_green = 0
    for r in results:
        # APEX-LEARNV2-2026-10-24: picks stopped carrying a color, so this "GREEN only"
        # gate threw out every pick and the history stayed empty. GREEN meant 70%+,
        # so the gate is now the 70% Win Prob floor itself.
        if float(r.get("confidence") or 0) < 0.70:
            skipped_non_green += 1
            continue
        signal = r.get("classified", {}).get("weight_adjustment_candidate")
        if not signal:
            continue
        history.append({
            "date":   dates["learn_on_date"],
            "signal": signal,
            "outcome": r.get("outcome"),
        })

    if skipped_non_green:
        log("WeightAgent",
            f"70%+ gate: {skipped_non_green} pick(s) under 70% excluded "
            f"from the learning pool (still archived, not deleted).")

    with open(SIGNAL_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, default=str)

def _cumulative_signal_stats(last_n=SIGNAL_LOOKBACK_LAST_N):
    """Aggregates signal_history.json into {signal: (hits, total)} over
    the most recent `last_n` graded picks PER SIGNAL (replaces the old
    14-day time window -- see SIGNAL_LOOKBACK_LAST_N comment)."""
    if not SIGNAL_HISTORY_FILE.exists():
        return {}, {}

    with open(SIGNAL_HISTORY_FILE) as f:
        history = json.load(f)

    history.sort(key=lambda e: e.get("date", ""))
    per_signal = {}
    for entry in history:
        per_signal.setdefault(entry["signal"], []).append(entry)

    signal_hits, signal_total = {}, {}
    for signal, entries in per_signal.items():
        recent = entries[-last_n:]
        signal_total[signal] = len(recent)
        signal_hits[signal]  = sum(1 for e in recent if e["outcome"] == "HIT")
    return signal_hits, signal_total

# ── MONEYLINE LEARNING SYSTEM (v7.9.7) ───────────────────────────
# Mirrors the props signal_history -> cumulative_stats -> proposals
# pipeline, but with its own file, its own threshold (MIN_ML_PROPOSAL_
# SAMPLE = 10, per operator decision — moneyline volume is naturally
# lower than props), and one extra gate props doesn't need: only
# losses (and hits) the attribution classifier marked as
# REASONING_FAILURE ever reach this pool at all. A DATA_FAILURE or
# VARIANCE result never gets appended here in the first place — it's
# the wrong kind of evidence to nudge a weight from.

# Maps the attribution classifier's weight_signal_implicated value
# (matches compute_ml_dominance_score's breakdown keys) to the actual
# weights.json key it should propose adjusting.
ML_SIGNAL_TO_WEIGHT_KEY = {
    "serve_edge":  "ml_serve_weight",
    "return_edge": "ml_return_weight",
    "clutch_edge": "ml_clutch_weight",
    "form_edge":   "ml_form_weight",
}

def _append_ml_signal_history(entries, dates):
    """
    entries: list of dicts with at least {"signal": <breakdown key>,
    "outcome": "HIT"/"BUST", "color": <grade>, "root_cause": <str>}.
    Same GREEN-only gate as props, PLUS: only root_cause ==
    "REASONING_FAILURE" (or a HIT, which has no root_cause to check)
    is eligible — a BUST classified as DATA_FAILURE or VARIANCE is
    never appended here, since neither should move a weight.
    """
    if not ML_SIGNAL_HISTORY_FILE.exists():
        history = []
    else:
        with open(ML_SIGNAL_HISTORY_FILE) as f:
            history = json.load(f)

    skipped_non_green = 0
    skipped_wrong_cause = 0
    for e in entries:
        if e.get("color") != "GREEN":
            skipped_non_green += 1
            continue
        signal = e.get("signal")
        if not signal:
            continue
        if e.get("outcome") == "BUST" and e.get("root_cause") not in (None, "REASONING_FAILURE"):
            skipped_wrong_cause += 1
            continue
        history.append({
            "date":   dates["learn_on_date"],
            "signal": signal,
            "outcome": e.get("outcome"),
        })

    if skipped_non_green:
        log("MLWeightAgent",
            f"GREEN-only gate: {skipped_non_green} non-GREEN moneyline pick(s) excluded.")
    if skipped_wrong_cause:
        log("MLWeightAgent",
            f"Root-cause gate: {skipped_wrong_cause} BUST(s) excluded "
            f"(DATA_FAILURE or VARIANCE — not a weighting problem).")

    with open(ML_SIGNAL_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, default=str)

def _cumulative_ml_signal_stats(last_n=SIGNAL_LOOKBACK_LAST_N):
    """Same aggregation as _cumulative_signal_stats (most recent
    `last_n` graded picks per signal), over ML_SIGNAL_HISTORY_FILE
    instead of the props file."""
    if not ML_SIGNAL_HISTORY_FILE.exists():
        return {}, {}

    with open(ML_SIGNAL_HISTORY_FILE) as f:
        history = json.load(f)

    history.sort(key=lambda e: e.get("date", ""))
    per_signal = {}
    for entry in history:
        per_signal.setdefault(entry["signal"], []).append(entry)

    signal_hits, signal_total = {}, {}
    for signal, entries in per_signal.items():
        recent = entries[-last_n:]
        signal_total[signal] = len(recent)
        signal_hits[signal]  = sum(1 for e in recent if e["outcome"] == "HIT")
    return signal_hits, signal_total

def _build_ml_weight_proposals(entries, weights, dates=None):
    """
    Mirrors _build_weight_proposals but keyed to MIN_ML_PROPOSAL_SAMPLE
    (10) and ML_SIGNAL_TO_WEIGHT_KEY. Blocked-signal protection (never
    re-propose a rejected/reverted adjustment without new evidence)
    reuses the same approval_history.json as props — a rejection is a
    rejection regardless of which pipeline proposed it.
    """
    if dates is not None:
        _append_ml_signal_history(entries, dates)
    signal_hits, signal_total = _cumulative_ml_signal_stats()

    blocked = {}
    for h in _load_approval_history():
        if h["decision"] in ("REJECTED", "REVERTED"):
            blocked[h["signal"]] = max(blocked.get(h["signal"], 0), h["total"])

    proposals = []
    for signal, total in signal_total.items():
        if total < MIN_ML_PROPOSAL_SAMPLE:
            continue
        weight_key = ML_SIGNAL_TO_WEIGHT_KEY.get(signal)
        if not weight_key:
            continue
        if weight_key in blocked and total < blocked[weight_key] + 2:
            continue
        hit_rate = signal_hits.get(signal, 0) / total
        current  = weights.get(weight_key, DEFAULT_WEIGHTS.get(weight_key, 0))
        proposed = current
        if hit_rate > 0.80 and current > 0:
            proposed = round(min(current*1.1, current+0.02), 3)
        elif hit_rate < 0.40 and current > 0:
            proposed = round(max(current*0.9, current-0.02), 3)
        if proposed != current:
            proposals.append({
                "signal":    weight_key,
                "current":   current,
                "proposed":  proposed,
                "advantage": f"{hit_rate:.0%} hit rate on {total} samples ({signal})",
                "risk":      "Small sample — monitor after change",
                "total":     total,
            })
    return proposals[:3]
# ──────────────────────────────────────────────────────────────

def _build_weight_proposals(results, weights, dates=None):
    # Persist this run's results into the cumulative history first, then
    # build proposals off the full rolling window — not just tonight.
    if dates is not None:
        _append_signal_history(results, dates)
    signal_hits, signal_total = _cumulative_signal_stats()

    # Step 7: never suggest a rejected OR reverted adjustment twice
    # without new evidence. A REVERTED decision means the operator later
    # decided an approval was wrong — that deserves the same protection
    # as an outright rejection, or a revert would just get immediately
    # re-proposed off the same stale evidence that caused it.
    # "New evidence" = the sample size has grown by at least 2 since the
    # rejection/revert — a materially bigger sample, not the same
    # evidence resubmitted.
    blocked = {}
    for h in _load_approval_history():
        if h["decision"] in ("REJECTED", "REVERTED"):
            blocked[h["signal"]] = max(blocked.get(h["signal"], 0), h["total"])

    proposals = []
    _dead_signal_skips = 0
    for signal, total in signal_total.items():
        if total < MIN_PROPOSAL_SAMPLE:
            continue
        if signal in blocked and total < blocked[signal] + 2:
            continue
        hit_rate = signal_hits.get(signal,0) / total
        current  = weights.get(signal, DEFAULT_WEIGHTS.get(signal,0))
        # Dead-weight gate (this session, approved; hardened): prop-
        # signal weights were stripped from DEFAULT_WEIGHTS because
        # NOTHING consumes them since the v7.9.4 caps rewrite -- props
        # confidence is eval-model output + hard caps, with no numeric
        # weight layer. Gate on an explicit allowlist rather than
        # current == 0, because the on-disk weights.json can still
        # carry stale nonzero values for stripped keys (saved values
        # override defaults), which would slip a zero-check. The
        # allowlist is EMPTY by design: this builder handles PROP
        # signals only (ml_* weights have their own builder), and no
        # prop signal is consumed by scoring today. If a future scoring
        # change re-consumes a prop weight, add its key here.
        _PROP_WEIGHTS_CONSUMED_BY_SCORING = set()
        if signal not in _PROP_WEIGHTS_CONSUMED_BY_SCORING:
            _dead_signal_skips += 1
            continue
        proposed = current
        if hit_rate > 0.80 and current > 0:
            proposed = round(min(current*1.1, current+0.02), 3)
        elif hit_rate < 0.40 and current > 0:
            proposed = round(max(current*0.9, current-0.02), 3)
        if proposed != current:
            proposals.append({
                "signal":    signal,
                "current":   current,
                "proposed":  proposed,
                "advantage": f"{hit_rate:.0%} hit rate on {total} samples",
                "risk":      "Small sample — monitor after change",
                "total":     total,
            })
    if _dead_signal_skips:
        log("WeightAgent", f"Dead-weight gate: {_dead_signal_skips} signal(s) with "
            f"sufficient sample skipped — their weights are not consumed by "
            f"scoring (stripped in the dead-weight cleanup), so a proposal "
            f"would change nothing.")
    return proposals[:3]

# ================================================================
# WEIGHT ADJUSTMENT AGENT — APPROVAL FLOW (v7.9.6, Step 6/7)
# ================================================================
# Same architectural pattern as slip logging (Step 5E): Discord webhooks
# are outbound-only, so a reply typed in Discord can never reach this
# script. This is CLI-based on purpose, not as a placeholder — Step 9's
# eventual live gateway can call apply_proposal_decision() directly
# instead of needing a rewrite.

def _stale_pending_proposals(all_proposals, dates):
    """Returns PENDING proposals older than today's batch — the backlog
    that needs a persistent, repeated warning until acted on."""
    today = dates["learn_on_date"]
    return [p for p in all_proposals if p["status"] == "PENDING" and p["batch_date"] != today]

def _alert_pending_approvals(context_label):
    """Standalone, short, high-visibility reminder (this session,
    requested by operator: the only prior reminder was buried inside
    the full nightly #learning-channel report and was easy to miss).
    Fires to #alerts-channel — the channel already watched for BP-block
    and compliance alerts — at both 5PM pipeline start and 11PM
    learning run, so it repeats twice a day until resolved instead of
    once at night. Does nothing if there's nothing pending."""
    all_proposals = _load_proposals()
    still_pending = [p for p in all_proposals if p["status"] == "PENDING"]
    if not still_pending:
        return
    lines = [f"🔔 **{len(still_pending)} WEIGHT PROPOSAL(S) AWAITING YOUR APPROVAL** "
              f"({context_label})"]
    for p in still_pending:
        lines.append(f"• Proposal {p['id']}: `{p['signal']}` "
                      f"{p['current']:.3f} → {p['proposed']:.3f}")
    lines.append("Run in Terminal: `python3 openclaw_core.py --approve N` "
                  "or `--reject N`. Nothing changes until you decide.")
    send_discord("alerts", "\n".join(lines))

def _load_proposals():
    """Returns a flat list of proposals. Migrates the old single-batch
    dict format ({"batch_date":..., "proposals":[...]}) if found on disk."""
    if PROPOSALS_FILE.exists():
        with open(PROPOSALS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict) and "proposals" in data:
            return data["proposals"]
        return data
    return []

def _save_proposals(new_proposals, dates):
    """
    v7.9.6 fix: the original version overwrote the whole pending batch
    every night, silently discarding anything not yet approved/rejected.
    Proposals now ACCUMULATE — nothing is ever dropped without an
    explicit --approve/--reject decision. IDs are persistent across
    nights (never reset), and a signal that already has an undecided
    PENDING proposal doesn't get a duplicate piled on top of it.
    """
    existing = _load_proposals()
    pending_signals = {p["signal"] for p in existing if p["status"] == "PENDING"}
    next_id = max((p["id"] for p in existing), default=0) + 1

    added = []
    for p in new_proposals:
        if p["signal"] in pending_signals:
            continue  # already has an undecided proposal — don't duplicate
        entry = {
            "id":           next_id,
            "signal":       p["signal"],
            "current":      p["current"],
            "proposed":     p["proposed"],
            "advantage":    p["advantage"],
            "risk":         p["risk"],
            "total":        p.get("total", 0),
            "status":       "PENDING",
            "batch_date":   dates["learn_on_date"],
            "generated_at": dates["ct_time"],
            "decided_at":   None,
        }
        existing.append(entry)
        added.append(entry)
        next_id += 1

    with open(PROPOSALS_FILE, "w") as f:
        json.dump(existing, f, indent=2, default=str)

    return existing, added

def _load_approval_history():
    if APPROVAL_LOG_FILE.exists():
        with open(APPROVAL_LOG_FILE) as f:
            return json.load(f)
    return []

def _save_approval_history(history):
    with open(APPROVAL_LOG_FILE, "w") as f:
        json.dump(history, f, indent=2, default=str)

def apply_proposal_decision(proposal_id: int, decision: str, dates: dict) -> str:
    """
    v7.9.6 (Step 6/7). decision is "APPROVE" or "REJECT".
    On APPROVE: applies proposed weight to weights.json immediately,
    logs to approval_history.json, posts "✅ DONE" to #learning-agent
    per the handoff spec.
    On REJECT: leaves weights.json untouched, logs the rejection (with
    sample size) so _build_weight_proposals won't re-suggest the same
    signal without new evidence.
    Nothing applies without this explicit call — matches the standing
    rule that weight adjustments never auto-apply.
    """
    all_proposals = _load_proposals()
    match = next((p for p in all_proposals if p["id"] == proposal_id), None)
    if not match:
        return f"⚠️ No proposal #{proposal_id} found. Run `--proposals` to see the current pending list."
    if match["status"] != "PENDING":
        return f"⚠️ Proposal #{proposal_id} was already {match['status']} at {match['decided_at']}."

    decision = decision.upper()
    if decision not in ("APPROVE", "REJECT"):
        return f"⚠️ Unknown decision '{decision}' — use APPROVE or REJECT."

    match["status"] = "APPROVED" if decision == "APPROVE" else "REJECTED"
    match["decided_at"] = dates["ct_time"]

    history = _load_approval_history()
    history.append({
        "date":      dates["learn_on_date"],
        "signal":    match["signal"],
        "current":   match["current"],
        "proposed":  match["proposed"],
        "total":     match["total"],
        "decision":  match["status"],
        "decided_at": dates["ct_time"],
    })
    _save_approval_history(history)

    with open(PROPOSALS_FILE, "w") as f:
        json.dump(all_proposals, f, indent=2, default=str)

    if decision == "APPROVE":
        weights = load_weights()
        weights[match["signal"]] = match["proposed"]
        save_weights(weights)
        send_discord("learning",
            f"✅ **DONE** — {match['signal']} updated "
            f"{match['current']:.3f} → {match['proposed']:.3f}"
        )
        return f"✅ DONE — {match['signal']}: {match['current']:.3f} → {match['proposed']:.3f} (weights.json updated)"
    else:
        send_discord("learning",
            f"🚫 Rejected — {match['signal']} stays at {match['current']:.3f} "
            f"(will not be re-suggested without new evidence)"
        )
        return f"🚫 Rejected — {match['signal']} unchanged at {match['current']:.3f}"

def revert_weight_change(target: str, dates: dict) -> str:
    """
    v7.9.6. Undoes an approved weight change.
        target = a signal name  → reverts the most recent APPROVED
                  change for that specific signal
        target = "last"         → reverts the single most recent
                  APPROVED change across all signals
    Reverting sets weights.json back to the "current" (pre-approval)
    value stored in approval_history.json, and appends a REVERTED
    entry to that same history for a full audit trail — approvals and
    reverts are never edited or deleted, only appended to.
    """
    history = _load_approval_history()
    approved = [h for h in history if h["decision"] == "APPROVED"]
    if not approved:
        return "⚠️ No approved weight changes on record to revert."

    if target.lower() == "last":
        candidate = approved[-1]
    else:
        matches = [h for h in approved if h["signal"] == target]
        if not matches:
            return f"⚠️ No approved change found for signal '{target}'."
        candidate = matches[-1]

    # Don't revert something that's already been reverted since. Using
    # list position, not timestamp comparison — ct_time is a formatted
    # display string like "6:52 PM CT", not chronologically sortable
    # (e.g. "10:00 AM" < "9:00 AM" alphabetically). history is append-only
    # and always in real chronological order, so position is reliable.
    candidate_index = history.index(candidate)
    already_reverted = any(
        h["decision"] == "REVERTED" and h["signal"] == candidate["signal"]
        for h in history[candidate_index + 1:]
    )
    if already_reverted:
        return f"⚠️ '{candidate['signal']}' was already reverted after that approval."

    weights = load_weights()
    live_value = weights.get(candidate["signal"])
    weights[candidate["signal"]] = candidate["current"]
    save_weights(weights)

    history.append({
        "date":       dates["learn_on_date"],
        "signal":     candidate["signal"],
        "current":    live_value,          # value being reverted FROM
        "proposed":   candidate["current"], # value being reverted TO
        "total":      candidate["total"],
        "decision":   "REVERTED",
        "decided_at": dates["ct_time"],
    })
    _save_approval_history(history)

    send_discord("learning",
        f"↩️ **REVERTED** — {candidate['signal']} rolled back to "
        f"{candidate['current']:.3f} (was {live_value:.3f})"
    )
    return (f"↩️ Reverted {candidate['signal']}: {live_value:.3f} → "
            f"{candidate['current']:.3f} (weights.json updated)")


# ================================================================
# SYSTEM VERIFICATION (runs on startup)
# ================================================================

def run_system_verification(dates):
    """
    Runs when first message sent to OpenClaw after deployment.
    Checks every component. Reports to #system with pass/fail.
    Tells you exactly what is working and what needs attention.
    """
    log("Verify", "Running full system verification...")
    checks = []

    # 1 — Anthropic API
    # APEX-DROP-API-CHECK-2026-10-27 (owner, "Go drop api check"): the boards, Win
    # Probability, slips and alerts never call Anthropic, so the startup card no longer tests
    # it (two red lines on every restart). The key is still used by the slip-screenshot reader,
    # the legacy evaluation fallback and the learning write-ups; a dead key shows up there as
    # "could not read" / plain-text fallbacks. Set True to bring the two check lines back.
    SYSTEM_CHECK_ANTHROPIC = False
    if SYSTEM_CHECK_ANTHROPIC:
        test = call_model(MODEL_HAIKU, "Say OK",
                          "Reply with just the word OK", temperature=0.1, max_tokens=10)
        checks.append(("Anthropic API (Haiku)", "✅" if test and "OK" in test else "❌"))

        test2 = call_model(MODEL_SONNET, "Say OK",
                           "Reply with just the word OK", temperature=0.1, max_tokens=10)
        checks.append(("Anthropic API (Sonnet 4.6)", "✅" if test2 and "OK" in test2 else "❌"))

    # 2 — Discord channels
    for channel in ["picks","alerts","general","learning","system"]:
        webhook = DISCORD_WEBHOOKS.get(channel,"")
        if "YOUR_TOKEN" in webhook or not webhook:
            checks.append((f"Discord #{channel}", "❌ Token not configured"))
        else:
            try:
                # APEX-ODDSCARD-2026-10-24: a GET checks the webhook without posting a
                # "🔧 Verification test" line in every channel on each restart
                r = requests.get(webhook, timeout=5)
                checks.append((f"Discord #{channel}",
                               "✅" if r.status_code in (200,204) else f"❌ {r.status_code}"))
            except Exception as e:
                checks.append((f"Discord #{channel}", f"❌ {str(e)[:40]}"))

    # 3 — GitHub connection
    load_github_config()
    if GITHUB_CONFIG.get("token","") and "YOUR_" not in GITHUB_CONFIG.get("token",""):
        result = github_api("GET", "/contents/SESSION_HANDOFF.md")
        checks.append(("GitHub connection",
                       "✅" if result else "❌ Cannot read repo"))
        checks.append(("SESSION_HANDOFF.md",
                       "✅ Found" if result else "⚠️ Not found — will be created on first run"))
    else:   # APEX-QUIETVERIFY-2026-10-23 (owner, "Go for everything"): nothing uses GitHub, so no token is not a failure
        checks.append(("GitHub connection", "ℹ️ Not set up (optional, not used)"))

    # 4 — PrizePicks API
    # APEX-PPTHROTTLE2-2026-10-24 (owner, PrizePicks limits): the startup
    # check used to call api.prizepicks.com directly on every scheduler restart,
    # outside the shared gate (not logged, ignored a pause). It now reads the
    # shared pull (reused up to 10 min) like every other job.
    try:
        import apex_ops as _ppv
        _ppv.pp_partner_board(max_age=600, caller="verify")
        checks.append(("PrizePicks API", "✅ Connected (%s)" % (_ppv.PP_LAST.get("source") or "ok")))
    except Exception as e:
        checks.append(("PrizePicks API", f"❌ {str(e)[:40]}"))

    # 5 — Playwright (optional fallback for prop collection only; not used
    # by the learning system or settlement). APEX-PLAYWRIGHTCHECK-2026-10-07:
    # retired the port-9222 ping -- APEX-FETCHPIPE-2026-09-30 already moved
    # fetch_props_playwright() to launch its own Chrome against
    # CHROME_PRIZEPICKS_PROFILE, so nothing connects to that port any more.
    # The real dependency now is that persistent, logged-in profile existing.
    try:
        import playwright
        checks.append(("Playwright installed", "✅"))
        if Path(CHROME_PRIZEPICKS_PROFILE).exists():
            checks.append(("PrizePicks Chrome profile", "✅ Present (%s)" % CHROME_PRIZEPICKS_PROFILE))
        else:
            checks.append(("PrizePicks Chrome profile",
                           "⚠️ Not found — first Playwright fallback run will create and need login at %s"
                           % CHROME_PRIZEPICKS_PROFILE))
    except ImportError:
        checks.append(("Playwright",
                       "⚠️ Not installed — run: pip install playwright && playwright install chromium"))

    # 6 — Data directories
    for d in [DATA_DIR, ARCHIVE_DIR]:
        checks.append((f"Directory {d}", "✅" if d.exists() else "❌ Missing"))

    # 7 — Weights file
    checks.append(("weights.json", "✅" if WEIGHTS_FILE.exists() else "⚠️ Will be created on first run"))

    # 8 — Master prompt
    prompt_exists = Path("OPENCLAW_MASTER_PROMPT_V7.txt").exists() or Path("OPENCLAW_MASTER_PROMPT_V71.txt").exists()
    checks.append(("Master prompt file", "✅" if prompt_exists else "❌ Not found"))

    # Build report
    passed  = sum(1 for _, s in checks if s.startswith("✅"))
    warned  = sum(1 for _, s in checks if s.startswith("⚠️"))
    failed  = sum(1 for _, s in checks if s.startswith("❌"))
    total   = sum(1 for _, s in checks if not s.startswith("ℹ️"))   # optional items are not counted

    status_emoji = "✅" if failed == 0 else ("⚠️" if failed <= 2 else "❌")
    overall      = ("ALL SYSTEMS GO" if failed == 0
                    else f"{failed} ISSUE(S) NEED ATTENTION")

    report_lines = [
        "═"*38,
        f"🔧 **OPENCLAW v7.1 SYSTEM VERIFICATION**",
        f"{dates['ct_time']}",
        "═"*38,
        f"{status_emoji} **{overall}**",
        f"Passed: {passed}/{total} | Warnings: {warned} | Failed: {failed}",
        "",
        "**Component Status:**",
    ]
    for name, status in checks:
        report_lines.append(f"{status} {name}")

    if failed > 0:
        report_lines += [
            "",
            "**Action required for failed items:**",
            "1. Fill in config.json with your tokens and keys",
            "2. Run: pip install playwright && playwright install chromium",
            "3. Check SETUP_INSTRUCTIONS.txt for step-by-step guidance",
        ]

    if warned > 0:
        report_lines += [
            "",
            "**Warnings (optional but recommended):**",
            "⚠️ items work with reduced functionality",
            "Playwright is a fallback for prop collection only (not used by learning or settlement) -- partner API is primary, so a missing/stale profile just means API-only fetching until it's refreshed",
        ]

    report_lines += [
        "",
        "─"*38,
        "📋 **Schedule:**",
        "• 5:00 PM CT — Fetch props for tomorrow",
        "• 5:45 PM CT — Picks → #picks",
        "• 11:00 PM CT — Learning runs",
        "• 11:30 PM CT — Learning report → #learning",
        "• Manual — Screenshot or prop → #general anytime",
        "─"*38,
    ]

    send_discord("system", "\n".join(report_lines))

    # Also send summary to general so you see it in chat
    send_discord("general",
        f"🔧 **OpenClaw v7.1 verification complete**\n"
        f"{status_emoji} {overall} — {passed}/{total} checks passed\n"
        f"Full report in #system"
    )

    log("Verify", f"Verification complete: {passed}/{total} passed, {failed} failed")
    return failed == 0

# ================================================================
# SCHEDULER
# ================================================================

def run_pipeline():
    # APEX-OPS-2026-10-10 (owner, approved "go" 2026-10-10): every
    # 5PM run records start / finish / fail to data/logs/pipeline_runs.json
    # so the watchdog (apex_ops_watchdog.py) can alert when a run is missed.
    try:
        import apex_ops as _ops
        _ops.record_pipeline_event("start")
    except Exception as _e:
        log("Ops", f"pipeline start marker failed (non-fatal): {_e}")
    try:
        _run_pipeline_inner()
        try:
            _ops.record_pipeline_event("finish")
        except Exception:
            pass
    except Exception as _e:
        try:
            _ops.record_pipeline_event("fail", error=f"{type(_e).__name__}: {_e}")
        except Exception:
            pass
        send_discord("system", f"❌ **5 PM run failed** — {type(_e).__name__}: {str(_e)[:300]}")
        raise
    finally:
        end_fetch_discord_suppression()

def _run_pipeline_inner():
    dates  = get_system_dates()
    weights = load_weights()
    send_discord("system",
        f"⏰ **5:00 PM CT — Pipeline starting**\n"
        f"Picks for: {dates['display_day']}"
    )
    _alert_pending_approvals("5PM pipeline start")
    # APEX-5PMDONE-2026-10-12: same #alerts quiet mode /fetch uses, so the run
    # no longer fires ~1,000 single alerts (all rate-limited on 10/12).
    # run_pipeline() turns it back off in its finally.
    begin_fetch_discord_suppression()
    props, _source = fetch_props_with_fallback(dates["match_date"], dates)
    if props:
        supervisor_agent(props, dates, weights, mode="live")
    else:
        end_fetch_discord_suppression()
        send_discord("alerts",
            f"⚠️ **No props found** for {dates['display_day']}\n"
            f"Use #general to submit props or screenshots manually."
        )

def run_learning():
    dates   = get_system_dates()
    weights = load_weights()
    learning_agent(dates, weights)

def run_elo_rebuild():
    # Added 2026-08-12: closes a real gap found while wiring the new
    # confidence_engine.py into evaluate_moneyline() — elo_ops.py's
    # rebuild_elo_ratings() existed and worked, but was NEVER on this
    # schedule (confirmed: only elo_ops.get_elo_gap(), a pure read, was
    # ever called from this file). The player_elo table it reads from
    # was built once, manually, during the original signal audit
    # (2026-08-07), and had been silently going stale ever since —
    # every real match since then wasn't reflected in the ELO ratings
    # the live system reads. Placed here, between run_learning (23:00)
    # and run_moneyline_settlement (23:15), on the assumption the day's
    # collector runs are done by then — ADJUST THE SCHEDULED TIME BELOW
    # if the real tennis data collector finishes later than this.
    result = elo_ops.rebuild_elo_ratings()
    if result.get("status") != "ok":
        send_discord("alerts",
            f"⚠️ **ELO rebuild failed** — {result.get('message', result)}"
        )
    else:
        print(f"ELO rebuild OK — {result.get('players_rated')} players, "
              f"{result.get('matches_used')} real matches used")

def run_moneyline_settlement():
    # Added 2026-08-09: closes a real gap flagged since props settlement
    # was first automated — moneyline_settlement_agent() (line ~10432)
    # was a real, complete, working function, but was NEVER added to
    # the scheduler. It only ever ran when someone manually triggered
    # it. Mirrors run_learning() exactly, same real (dates, weights)
    # signature moneyline_settlement_agent() already expects.
    dates   = get_system_dates()
    weights = load_weights()
    moneyline_settlement_agent(dates, weights)

def run_pending_retry():
    # APEX-RETRYENGINE-2026-10-05: nightly retry pass over the pending
    # backlog — see retry_pending_engine()'s docstring for the root
    # cause this closes. Runs after moneyline settlement (23:15), before
    # the daily reset (23:59), same real (dates, weights) signature
    # every other nightly agent in this file already uses.
    dates   = get_system_dates()
    weights = load_weights()
    retry_pending_engine(dates, weights)

def run_reset():
    # APEX-RETRYENGINE-2026-10-05: the old age-based quarantine here
    # (3 days, no second look) is replaced by run_pending_retry()'s
    # own retry_count-based quarantine (7 consecutive failed nightly
    # retries, each one a REAL attempt against the API/Serper, not
    # just a clock running out). This function no longer touches
    # PENDING_FILE at all — retry_pending_engine() owns it exclusively
    # now, so there's one place deciding a pick's fate, not two.
    dates = get_system_dates()
    _research_cache.clear()

    send_discord("system",
        f"♻️ **Daily reset complete** — {dates['ct_time']}\n"
        f"Cache cleared. Pending results retried nightly by the retry "
        f"engine (see #learning for the Settlement Health Report). "
        f"Ready for tomorrow."
    )

def apex_reprice_active(window_min=120, min_gap_min=50):
    """APEX-REPRICE-2026-10-11 (owner, approved "go" 2026-10-11): before a
    match starts, re-check its price and re-score its props when the price moved
    2+ points since they were logged (test: Brier 0.2320 -> 0.2295 on moved
    matches). Runs every 30 min from the scheduler for active, playable props
    whose match starts within `window_min` minutes, each prop at most once per
    `min_gap_min`. Re-scored props are appended to the pick log only (see
    APEX-REPRICE in the forward-log writer), then the boards are republished.
    Posts one #alerts note only when the republish moved a board (APEX-BOARDSTATUS-
    HEADER-2026-10-25). Returns a short summary string."""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import apex_edge_tracker as _aet
    state_path = os.path.join("data", "logs", "reprice_state.json")
    try:
        with open(state_path) as fh:
            state = _json.load(fh)
    except Exception:
        state = {}
    picks, _note = _aet.load_current_board()
    now = _dt.now(_tz.utc)
    dates = get_system_dates()
    try:
        with open(_APEX_PICK_LOG) as fh:
            before = sum(1 for _ in fh)
    except Exception:
        before = 0
    # APEX-REPRICE-TOURNAMENT-2026-10-29 (owner, "Need these issue fix"): board rows carry no
    # tournament, so every re-price scored on the default Hard surface (Queretaro clay props went
    # hard at 1:02 PM 10/29). Take tournament / format from the prop's newest pick-log row.
    _rp_meta = {}
    try:
        import csv as _rp_csv
        with open(_APEX_PICK_LOG, newline="") as fh:
            for _r in _rp_csv.DictReader(fh):
                if (_r.get("match_date") or "") < dates.get("match_date", "") or not _r.get("tournament"):
                    continue
                _rp_meta["|".join(str(x) for x in _aet._apex_prop_identity(_r))] = (
                    _r.get("tournament") or "", _r.get("format_label") or "", _r.get("best_of") or "")
    except Exception as _e:
        log("Reprice", f"pick-log tournament lookup unavailable ({type(_e).__name__}); using board rows")
    checked = 0
    for r in picks or []:
        try:
            st = _aet._prop_start_utc(r)
            if not st:
                continue
            mins = (_dt.fromisoformat(st) - now).total_seconds() / 60.0
            if not (0 < mins <= window_min):
                continue
            ident = "|".join(str(x) for x in _aet._apex_prop_identity(r)) + "|" + str(r.get("prop_type"))
            last = state.get(ident)
            if last and (now - _dt.fromisoformat(last)).total_seconds() < min_gap_min * 60:
                continue
            if str(r.get("player")) == "match":
                pa, pb = [x.strip() for x in str(r.get("opponent") or "").split(" v ")][:2]
            else:
                pa, pb = r.get("player"), r.get("opponent")
            ptype = str(r.get("prop_type") or "standard")
            _m = _rp_meta.get("|".join(str(x) for x in _aet._apex_prop_identity(r)))   # APEX-REPRICE-TOURNAMENT-2026-10-29
            if _m:
                r = dict(r); r.setdefault("tournament", None); r.setdefault("format_label", None); r.setdefault("best_of", None)
                r["tournament"] = r.get("tournament") or _m[0]
                r["format_label"] = r.get("format_label") or _m[1]
                r["best_of"] = r.get("best_of") or _m[2]
            lab = (str(r.get("format_label") or "") + " " + str(r.get("tournament") or "")).lower()
            # APEX-AUTOEVAL-TOUR-2026-10-15: board rows carry no format_label or
            # tournament, so this always said "atp" and re-priced WTA props with
            # men's numbers. Ask SportsAPI365's listing, like Gate 11 does.
            _rp_tour = "wta" if ("women" in lab or "wta" in lab) else None
            if _rp_tour is None:
                try:
                    import apex_price_feed as _apf_rp
                    _rp_tour = "wta" if str((_apf_rp.fixture_context(pa, pb) or {}).get("tour") or "").lower() == "wta" else "atp"
                except Exception:
                    _rp_tour = "atp"
            # APEX-REPRICEDF-2026-10-13: the pick log stores internal names; the engine
            # wants PrizePicks slugs, so 'dfs'/'breaks' were declined as unmapped.
            _slug = r.get("stat_type") or {"dfs": "double_faults", "breaks": "break_points_won"}.get(
                r.get("category"), r.get("category"))
            prop = {"player": pa, "opponent": pb, "stat_type": _slug,
                    "line": float(r.get("line")), "tournament": r.get("tournament") or "",
                    "apex_fixture_tournament": r.get("tournament") or "",
                    "match_format": "bo5" if str(r.get("best_of") or "") in ("5", "5.0") else "bo3",
                    "tour": _rp_tour,
                    "match_date": r.get("match_date"), "prop_type_classified": ptype,
                    "is_goblin": ptype == "goblin", "is_demon": ptype == "demon",
                    "both_directions_available": ptype == "standard", "slip_start_utc": st,
                    "apex_reprice_job": True}
            _apex_engine_pick(prop, dates, None)
            state[ident] = now.isoformat(timespec="seconds")
            checked += 1
        except Exception as _e:
            log("Reprice", f"skip {r.get('player')} {r.get('category')} {r.get('line')}: {type(_e).__name__}: {_e}")
    cutoff = (now - _td(days=2)).isoformat()
    state = {k: v for k, v in state.items() if v >= cutoff}
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w") as fh:
            _json.dump(state, fh)
    except Exception:
        pass
    try:
        with open(_APEX_PICK_LOG) as fh:
            after = sum(1 for _ in fh)
    except Exception:
        after = before
    refreshed = max(0, after - before)
    if refreshed:
        _cap0 = _board_status_cap()
        try:
            _aet.republish_boards(source="reprice")
            # APEX-BOARDSTATUS-HEADER-2026-10-25: one #alerts note only when a re-price moved a board
            _st = _board_status(_cap0)
            import apex_board_status as _bs
            if _st and ((_bs.has_swaps(_st) if _bs.SWAPS_ONLY else False)   # APEX-SWAPLINES-2026-10-25
                        or (not _bs.SWAPS_ONLY and (any(e["changed"] for e in _st["main"].values())
                                                    or any(e["changed"] for e in _st["other"].values())))):
                _rp_done = False
                try:   # APEX-ALERTCHAIN-2026-10-25: one card; the odds-move lines join it at the next check
                    import apex_alert_chain as _AC
                    if _AC.CHAINS_ON:
                        _at, _w = _AC.next_board_check()
                        _AC.soft_start("reprice", _bs.card(_st, f"💲 Re-price: {refreshed} prop(s) re-scored",
                                                           ["bet365 moved its price before the match, so Apex re-scored them."]),
                                       f"Nothing to do. If a move is 3+ points on a board pick, the 📉 odds-move lines "
                                       f"get added to this card at the next check (about {_AC.hm(_at)}).")
                        _rp_done = True
                except Exception as _ce:
                    log("Reprice", f"chain card failed ({type(_ce).__name__}), posting the card")
                if not _rp_done:
                    _post_status_card(_st, f"💲 Re-price: {refreshed} prop(s) re-scored",
                                      ["bet365 moved its price before the match, so Apex re-scored them."],
                                      text=f"💲 Re-price: {refreshed} prop(s) re-scored after a bet365 price move before the match.")
        except Exception as _e:
            log("Reprice", f"republish failed (non-fatal): {_e}")
    msg = f"checked {checked} prop(s) starting within {window_min} min, re-scored {refreshed}"
    if checked:
        log("Reprice", msg)
    return msg


def _run_board_watch():
    """APEX-BOARDWATCH-2026-10-12: records when each PrizePicks tennis prop first appears."""
    try:
        import apex_board_watch as _bw
        log("BoardWatch", _bw.run(__import__(__name__)))
    except Exception as _e:
        log("BoardWatch", f"run failed (non-fatal): {type(_e).__name__}: {_e}")
        return
    _drop_alert()


# APEX-DROPALERT-2026-10-14 (owner, "Go" 2026-10-14: "I'm trying to
# figure out when the lines come out so I can be on them"). Big batches land
# around 11 AM-1 PM and 9-11 PM CT (first-seen log, 10/12-10/14). When the
# board watch sees DROP_ALERT_MIN+ new props: ping #alerts, score them right
# away (auto-eval), then ping again with the new props that made a board.
# Between 11:00-13:30 and 21:00-23:30 CT the board watch runs every 10 min.
DROP_ALERT_MIN = 25
DROP_WINDOWS = ((11 * 60, 13 * 60 + 30), (21 * 60, 23 * 60 + 30))


def _in_drop_window(now=None):
    now = now or datetime.now(CT)
    m = now.hour * 60 + now.minute
    return any(a <= m < b for a, b in DROP_WINDOWS)


def _run_drop_watch():
    """Extra 10-minute board watch, only inside the drop windows."""
    if _in_drop_window():
        _run_board_watch()


def _published_idents():
    try:
        import apex_edge_tracker as _aet
        with open(_aet.PUBLISHED_BOARD_SNAPSHOT) as fh:
            snap = json.load(fh)
    except Exception:
        return {}
    out = {}
    for b, v in (snap.get("boards") or {}).items():
        for e in (v or {}).get("entries") or []:
            p = e.get("p") or {}
            k = (str(p.get("player")), str(p.get("opponent")), str(p.get("category")), str(p.get("line")),
                 str(p.get("prop_type") or "standard"), str(p.get("direction")))
            out.setdefault(k, [p, []])[1].append(b)
    return out


_DROP_BOARD_LABEL = {"top3": "Top 3", "elite6": "Elite6", "top10": "Top10", "bestgoblin": "Best Goblin",
                     "bestdemon": "Best Demon", "beststandard": "Best Standard"}


def _board_status_cap():
    """APEX-BOARDSTATUS-HEADER-2026-10-25: the newest board capture (the 'last posted board')."""
    try:
        import apex_board_status as _bs
        return _bs.latest_capture()
    except Exception:
        return None


def _board_status(cap_before):
    """Big TOP 3 & ELITE 6 status: the newest board capture vs `cap_before`. None on failure."""
    try:
        import apex_board_status as _bs
        return _bs.from_captures(cap_before, _bs.latest_capture())
    except Exception:
        return None


def _board_status_wrap(st, text):
    try:
        import apex_board_status as _bs
        return _bs.wrap_text(st, text)
    except Exception:
        return text


def _post_card(title, color, desc, fields, text=None):
    """APEX-ALERTCARDS-2026-10-25: one #alerts card (muted during a /fetch like any alert); falls
    back to the plain text if the card can't be built."""
    try:
        import apex_alert_cards as _C
        if _C.ALERT_CARDS_ON:
            return _C.post(_C.make(title, color, desc, fields), "alerts", check_mute=True)
    except Exception as _e:
        log("AlertCards", f"card failed ({type(_e).__name__}), sending text")
    return send_discord("alerts", text or title)


def _post_status_card(st, event, event_lines=(), extra=(), text=None):
    """APEX-ALERTCARDS-2026-10-25: a board-update card (big TOP 3 & ELITE 6 status first)."""
    try:
        import apex_alert_cards as _C
        import apex_board_status as _bs
        if _C.ALERT_CARDS_ON:
            return _bs.post_alert(st, event, event_lines, [x for x in extra if x[1]], check_mute=True)
    except Exception as _e:
        log("AlertCards", f"status card failed ({type(_e).__name__}), sending text")
    return send_discord("alerts", _board_status_wrap(st, text or event))


def _card_who(p):
    if p.get("player") == "match":
        return " v ".join(x.split()[-1] for x in str(p.get("opponent") or "").split(" v ") if x.split())
    return (str(p.get("player") or "").split() or ["?"])[-1]


_CARD_CAT = {"total_games": "TG", "fantasy_score": "Fantasy", "double_faults": "DF", "aces": "Aces",
             "breaks": "Breaks", "games_won": "Games Won", "total_sets": "Sets"}


def _card_what(p):
    return (f"{_CARD_CAT.get(p.get('category'), str(p.get('category')).replace('_', ' ').title())} "
            f"{str(p.get('direction') or '').upper()} {p.get('line')} ({str(p.get('prop_type') or 'standard').title()})")


def _ampm(iso):
    try:
        return datetime.fromisoformat(str(iso)).strftime("%I:%M %p").lstrip("0")
    except Exception:
        return "earlier"


def _new_on_board_spot(p, flag):
    """'Top 10 #10' -- the spot the pick holds on the published board now."""
    try:
        import apex_edge_tracker as _aet
        with open(_aet.PUBLISHED_BOARD_SNAPSHOT) as fh:
            ents = ((json.load(fh).get("boards") or {}).get(flag) or {}).get("entries") or []
        k = _aet._apex_prop_identity(p)
        for i, e in enumerate(ents, 1):
            if _aet._apex_prop_identity(e.get("p") or {}) == k:
                return f"{_DROP_BOARD_LABEL.get(flag, flag).replace('Elite6', 'Elite 6').replace('Top10', 'Top 10')} #{i}"
    except Exception:
        pass
    return _DROP_BOARD_LABEL.get(flag, flag)


def _new_on_board_kinds(cap0, props):
    """{id(p): ('new', '') or ('up', 'why')}: a pick the last posted board already scored is not a
    new prop -- it moved up from BACKUPS (filling a pulled line when pulled_lines.csv says so)."""
    out = {}
    try:
        import apex_edge_tracker as _aet
        import apex_odds_watch as _ow
        import csv as _csv
        rows = [r for r in _ow._status_rows() if r["captured_at"] == cap0] if cap0 else []
        seen = {_aet._apex_prop_identity(r) for r in rows if r.get("board") == "all"}
        bk = {}
        for r in rows:
            if r.get("board") != "all" and r.get("status") in ("not_shown", "held_back"):
                bk.setdefault(_aet._apex_prop_identity(r), r.get("board"))
        fills = {}
        try:
            today = datetime.now().date().isoformat()
            with open(_aet.PULLED_LINES_LOG, newline="") as fh:
                for r in _csv.DictReader(fh):
                    if (r.get("logged_at") or "")[:10] == today and r.get("replacement_key"):
                        fills[r["replacement_key"]] = r
        except Exception:
            pass
        for p in props:
            k = _aet._apex_prop_identity(p)
            if not rows or k not in seen:
                out[id(p)] = ("new", "")
                continue
            rk = "|".join(str(p.get(x) or "") for x in ("player", "opponent", "category", "line", "direction"))
            f = fills.get(rk)
            if f:
                who = " v ".join(x.split()[-1] for x in str(f.get("match") or "").split(" v ") if x.split())
                out[id(p)] = ("up", f"moved up from Backup to fill the {who} {_CARD_CAT.get(f.get('market'), f.get('market'))} {f.get('line')} "
                                    f"spot PrizePicks pulled ({_DROP_BOARD_LABEL.get(f.get('board'), f.get('board'))} "
                                    f"#{f.get('rank_held')}, {_ampm(f.get('logged_at'))})".replace("Top10", "Top 10").replace("Elite6", "Elite 6"))
            else:
                out[id(p)] = ("up", "was in BACKUPS on the last board; a spot opened above it")
    except Exception as _e:
        log("AlertCards", f"new-vs-backup check skipped ({type(_e).__name__})")
    return out


def _np_items(props):
    """APEX-ALERTCHAIN-2026-10-25: board-watch props as new-props card items (same keys as the 15-min check)."""
    import apex_alert_chain as _AC
    out = []
    for p in props:
        _day = _AC.np_day(p.get("start_time_raw")) if p.get("start_time_raw") else str(p.get("match_date") or "")[:10]
        pt = p.get("odds_type") or p.get("prop_type") or ("demon" if p.get("is_demon") else "goblin" if p.get("is_goblin") else "standard")
        pair, label = _AC.np_pair(p.get("player"), p.get("opponent"))
        out.append((_AC.np_key(p.get("player"), p.get("opponent"), p.get("stat_type"), p.get("line"), pt), pair, label, _day))
    return out


def _chains_on():
    try:
        import apex_alert_chain as _AC
        return _AC.CHAINS_ON
    except Exception:
        return False


def _drop_alert():
    try:
        import apex_board_watch as _bw
        new = list(_bw.LAST_NEW)
        if _chains_on():   # APEX-ALERTCHAIN-2026-10-25: one self-editing new-props card
            import apex_alert_chain as _AC
            if not new:
                return
            _items = _np_items(new)
            _rel = [k for k, *_r in _np_items(list(_bw.LAST_RELISTED))]
            if len(new) < DROP_ALERT_MIN:
                _c, _data = _AC.np_merge(_items, _rel)
                if _c:   # the 15-min check's card is open: add these to its count
                    _AC.step(_AC.NEWPROPS_KEY, _c.get("now"), _c.get("next"), base=_AC.np_card(_data), data=_data)
                return
            _AC.np_scoring(_items, _rel)
            _run_auto_eval(from_drop=True)   # scores them and turns the card into the result
            return
        if len(new) < DROP_ALERT_MIN:
            return
        now = datetime.now(CT)
        days = sorted({str(p.get("match_date") or "") for p in new if p.get("match_date")})
        matches = {frozenset((str(p.get("player")), str(p.get("opponent")))) for p in new}
        # APEX-ALERTCARDS-2026-10-25: the three drop pings are cards; "New props on the boards" says
        # which picks are really new and which are backups that moved up (Timofeeva 17.5 at 5:44 PM
        # 10/25 was a Top 10 backup that filled the pulled Tjen line, not a new prop)
        _bycount = {}
        for p in new:
            _w = f"{str(p.get('player')).split()[-1]} v {str(p.get('opponent')).split()[-1]}"
            _bycount[_w] = _bycount.get(_w, 0) + 1
        _post_card(f"🆕 NEW PRIZEPICKS PROPS POSTED", 0x3498DB,
                   f"**{len(new)}** tennis props across **{len(matches)}** match(es)"
                   + (f" for {', '.join(days)}" if days else ""),
                   [("📥 Matches", [f"• {w} · {n} prop{'s' if n != 1 else ''}" for w, n in
                                   sorted(_bycount.items(), key=lambda x: -x[1])[:12]]
                     + ([f"+{len(_bycount) - 12} more"] if len(_bycount) > 12 else [])),
                    ("⏭️ Next", "Scoring them now — a second card shows any that make a board.")],
                   text=f"🆕 **New lines just posted** ({now.strftime('%I:%M %p').lstrip('0')} CT): {len(new)} tennis props "
                        f"across {len(matches)} match(es)" + (f" for {', '.join(days)}" if days else "")
                        + ". Scoring them now; a second ping lists any that make a board.")
        before = _published_idents()
        _cap0 = _board_status_cap()
        _run_auto_eval()
        after = _published_idents()
        _st = _board_status(_cap0)   # APEX-BOARDSTATUS-HEADER-2026-10-25
        added = [(p, boards) for k, (p, boards) in after.items() if k not in before]
        if not added:
            _post_status_card(_st, "📋 New-line check done", ["None of the new props made a board."],
                              text="📋 New-line check done: none of the new props made a board (boards unchanged).")
            return
        L = [f"🎯 **New props on the boards** ({datetime.now(CT).strftime('%I:%M %p').lstrip('0')} CT)"]
        newl, upl = [], []
        _kinds = _new_on_board_kinds(_cap0, [p for p, _b in added])
        for p, boards in sorted(added, key=lambda x: -float(x[0].get("prob") or 0)):
            who = p.get("opponent") if p.get("player") == "match" else p.get("player")
            L.append(f"• {who} -- {str(p.get('category')).replace('_', ' ').title()} {p.get('line')} "
                     f"{p.get('direction')} ({str(p.get('prop_type') or 'standard').title()}) · "
                     f"{100 * float(p.get('prob') or 0):.0f}% · {', '.join(_DROP_BOARD_LABEL.get(b, b) for b in boards)}")
            kind, note = _kinds.get(id(p), ("new", ""))
            where = ", ".join(_new_on_board_spot(p, b) for b in boards)
            ln = (f"{_card_who(p)} · {_card_what(p)} ({100 * float(p.get('prob') or 0):.0f}%) · {where}")
            if kind == "new":
                newl.append("🆕 " + ln)
            else:
                upl.append("↪️ " + ln + ("\n\u2003↳ " + note if note else ""))
        ev = []
        if newl:
            ev.append(f"{len(newl)} new prop{'s' if len(newl) != 1 else ''} made a board")
        if upl:
            ev.append(f"{len(upl)} pick{'s' if len(upl) != 1 else ''} moved up from Backups (not new props)")
        _post_status_card(_st, "🎯 New props on the boards", ev,
                          [("🆕 New props", newl), ("↪️ Moved up from Backups", upl)], text="\n".join(L))
    except Exception as _e:
        log("DropAlert", f"failed (non-fatal): {type(_e).__name__}: {_e}")


def _newprops_finish(key, before, cap0, t0, scope):
    """APEX-ALERTCHAIN-2026-10-25: the new-props card's last step -- how many got scored, which made a
    board (new, or a backup that moved up), the TOP 3 & ELITE 6 swap lines. Same card, edited."""
    import apex_alert_chain as _AC
    c = _AC.get(key) or {}
    data = c.get("data") or {}
    keys = list(data.get("keys") or [])
    n, nr = len(keys), len(set(data.get("relisted") or []) & set(keys))
    n_m = len(data.get("matches") or {})
    try:
        import apex_closing_line as _cl
        _since = (datetime.fromisoformat(c.get("started")) - timedelta(minutes=1)).isoformat() if c.get("started") \
            else (t0 - timedelta(minutes=1)).isoformat()
        n_sc = _cl._scored_since(set(keys), _since)[0]
    except Exception:
        n_sc = None
    first_set = sum(1 for k in keys if k.split("|")[2] in _APEX_FIRSTSET_SLUGS or k.split("|")[2].startswith("1st_set"))
    after = _published_idents()
    st = _board_status(cap0)
    added = [(p, boards) for k, (p, boards) in after.items() if k not in (before or {})]
    count = (f"{n} PrizePicks props ({n - nr} new, {nr} re-listed)" if nr else f"{n} new PrizePicks prop{'s' if n != 1 else ''}")
    ev = []
    if n_sc is not None:
        ev.append(f"✅ {n_sc} of {n} scored ({scope.get('t0') or _AC.hm(t0)}–{_AC.hm()}) · {n_m} match{'es' if n_m != 1 else ''}")
        left = n - n_sc
        # APEX-REALSKIP-2026-11-14 (owner, "Go real reason"): the real, checked reason per match (apex_skip_reasons)
        # instead of the old guess. Undo: apex_skip_reasons.ON = False.
        _real = []
        try:
            import apex_skip_reasons as _sr
            if _sr.ON:
                _real = _sr.lines(data, _since, {_sr._n(x) for _p, _b in added for x in ([_p.get('player')] if _p.get('player') != 'match' else str(_p.get('opponent') or '').split(' v '))})
        except Exception:
            _real = []
        if _real:
            if left > 0:
                ev.append(f"⏳ {left} not scored. Why, match by match:")
            else:
                ev.append("🔎 Match by match:")
            ev += _real
        elif left > 0:
            why = []
            if first_set:
                why.append(f"{min(first_set, left)} are 1st-set lines (handled by the first-set rule)")
            if left - first_set > 0:
                why.append(f"{left - first_set} not scored yet")
            ev.append(f"⏳ {left} not scored: " + "; ".join(why))
    newl, upl = [], []
    _kinds = _new_on_board_kinds(cap0, [p for p, _b in added]) if added else {}
    for p, boards in sorted(added, key=lambda x: -float(x[0].get("prob") or 0)):
        kind, note = _kinds.get(id(p), ("new", ""))
        ln = f"{_card_who(p)} · {_card_what(p)} ({100 * float(p.get('prob') or 0):.0f}%) · " \
             + ", ".join(_new_on_board_spot(p, b) for b in boards)
        if kind == "new":
            newl.append("🆕 " + ln)
        else:
            upl.append("↪️ " + ln + ("\n\u2003↳ " + note if note else ""))
    if newl:
        ev.append(f"🎯 {len(newl)} new prop{'s' if len(newl) != 1 else ''} made a board")
    if upl:
        ev.append(f"↪️ {len(upl)} pick{'s' if len(upl) != 1 else ''} moved up from Backups (not new props)")
    if not added:
        ev.append("None of the new props made a board.")
    for cd in scope.get("cards") or []:   # the odds watch's own result (ODDS RELEASED etc.)
        e = str(cd.get("_event") or cd.get("title") or "")
        if e and not e.startswith("\U0001F195"):
            ev.append(e)
    ev += list(scope.get("lines") or [])
    rows = sorted((data.get("matches") or {}).values(), key=lambda v: -len(v.get("keys") or []))
    ml = " · ".join(f"{v['label']} {len(v['keys'])}" for v in rows[:5]) + (f" · +{len(rows) - 5} more" if len(rows) > 5 else "")
    try:
        import apex_board_status as _bs
        card = _bs.card(st, f"🆕 {count} scored", ev,
                        [x for x in [("🆕 New props", newl), ("↪️ Moved up from Backups", upl),
                                     ("📥 Matches (props)", ml)] if x[1]])
    except Exception:
        import apex_alert_cards as _C
        card = _C.make(f"🆕 {count} scored", _C.BLUE, "\n".join(ev), [("📥 Matches (props)", ml)])
    _AC.finish(key, card, note="scored %s" % _AC.hm())


def _flush_scope(scope):
    """APEX-ALERTCHAIN-2026-10-25: no new-props card open -- the odds watch card(s) and 📅 lines this run
    held back post now, the 📅 lines on top of the first card instead of as their own message."""
    cards, lines = list(scope.get("cards") or []), list(scope.get("lines") or [])
    if not cards and not lines:
        return
    import apex_odds_watch as _ow
    if cards:
        if lines:
            cards[0] = dict(cards[0], description=("\n".join(lines) + "\n\n" + (cards[0].get("description") or ""))[:4096])
        for cd in cards:
            _ow.post_card(__import__(__name__), "alerts", cd)
    else:
        send_discord("alerts", "\n".join(lines))


_LAST_AUTOEVAL = {"ts": 0.0}


# APEX-SCORINGCATCHUP-2026-11-01 (owner, "be sure this doesn't become an issue"): a scheduler restart
# killed the 6:15 PM scoring run on 11/1 and left its card stuck. The cycle saves when it starts and finishes;
# after a restart, a run that was stopped or is overdue goes again within minutes instead of waiting 30.
_CYCLE_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs", "board_cycle_state.json")


def _cycle_mark(key):
    try:
        try:
            with open(_CYCLE_STATE) as fh:
                st = json.load(fh)
        except Exception:
            st = {}
        st[key] = time.time()
        with open(_CYCLE_STATE + ".tmp", "w") as fh:
            json.dump(st, fh)
        os.replace(_CYCLE_STATE + ".tmp", _CYCLE_STATE)
    except Exception as e:
        log("BoardCycle", f"state not saved: {type(e).__name__}")


def _cycle_catchup():
    """Runs once after startup. Never sooner than 3 minutes after the stopped run began (PrizePicks pulls)."""
    try:
        with open(_CYCLE_STATE) as fh:
            st = json.load(fh)
    except Exception:
        st = {}
    now = time.time()
    began, done = float(st.get("started", 0)), float(st.get("finished", 0))
    if now - began < 180:
        return None   # try again next minute
    if began > done or now - max(began, done) > 32 * 60:
        log("BoardCycle", "scoring catch-up after restart: the last run was stopped or is overdue")
        _run_board_cycle()
    return schedule.CancelJob


def _run_board_cycle():
    """APEX-PPTHROTTLE-2026-10-16: the 30-min PrizePicks cycle on ONE shared
    pull -- board watch (first-seen log + drop alert), auto-eval (skipped if
    the drop alert just ran it), props snapshot."""
    _cycle_mark("started")
    try:
        _run_board_cycle_body()
    finally:
        _cycle_mark("finished")


def _run_board_cycle_body():
    try:   # APEX-ALERTCHAIN-2026-10-25: the new-props card says when the next check scores them
        import apex_alert_chain as _AC
        _AC.meta("board_check", {"cycle_at": datetime.now().isoformat(timespec="seconds")})
    except Exception:
        pass
    _run_board_watch()
    if time.time() - _LAST_AUTOEVAL["ts"] > 120:
        _run_auto_eval()
    _run_props_db("snapshot")


def _run_props_db(job="snapshot"):
    """APEX-PROPSDB-2026-10-13: full props database -- board snapshot every 30
    min, result settlement every 2 hours. Background only."""
    try:
        _now = datetime.now(CT)
        if job == "settle" and (_now.hour == 16 and _now.minute >= 40 or _now.hour == 17 and _now.minute < 40):
            return
        import apex_props_db as _pdb
        log("PropsDB", _pdb.snapshot() if job == "snapshot" else _pdb.settle(limit_matches=60))
        if job == "settle":   # APEX-BET365-THREEPART-2026-10-22 (owner, "Go bet365"): hourly bet365 fill + check
            _f = _pdb.fill_bet365()
            log("PropsDB", "bet365 totals filled: %d row(s) %s" % (sum(_f.values()), dict(sorted(_f.items()))))
            import apex_data_health as _dhb
            _dhb.alert_bet365()
    except Exception as _e:
        log("PropsDB", f"{job} failed (non-fatal): {type(_e).__name__}: {_e}")


_FAST_SETTLE_BUSY = {"on": False}


# APEX-JOBCATCHUP-2026-11-01 (owner, "Fix all"): schedule.every(1).hours restarted its clock on every
# scheduler restart, so a day of restarts starved the hourly jobs (81 restarts since 10/25, a 5.6-hour gap on
# 10/25). A saved last-run time decides instead; a job due at restart runs within 5 minutes.
_HOURLY_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs", "hourly_last_run.json")


def _hourly_due(name, fn, *args):
    try:
        with open(_HOURLY_STATE) as fh:
            st = json.load(fh)
    except Exception:
        st = {}
    now = time.time()
    if now - float(st.get(name, 0)) < 3600:
        return
    st[name] = now
    try:
        with open(_HOURLY_STATE + ".tmp", "w") as fh:
            json.dump(st, fh)
        os.replace(_HOURLY_STATE + ".tmp", _HOURLY_STATE)
    except Exception as e:
        log("Hourly", f"state not saved: {type(e).__name__}")
    fn(*args)


def _run_fast_settle():
    """APEX-FASTSETTLE-2026-10-19 (owner, "Go"): hourly win/loss check.
    Same two settle steps as the 6:40 AM daily job (apex_edge_tracker_daily.sh),
    limited to the last 3 days and including today's finished matches, so the
    ledger behind board win rates updates through the day instead of once each
    morning. Runs in a background thread; skips the 5 PM fetch window and the
    6:40 AM daily job window. Background only, fail-open."""
    _now = datetime.now(CT)
    if (_now.hour == 16 and _now.minute >= 40) or (_now.hour == 17 and _now.minute < 40) \
            or (_now.hour == 6 and _now.minute >= 25) or (_now.hour == 7 and _now.minute < 15):
        return
    if _FAST_SETTLE_BUSY["on"]:
        return

    def _work():
        import subprocess as _sp
        import sys
        _FAST_SETTLE_BUSY["on"] = True
        try:
            since = (_now.date() - timedelta(days=3)).isoformat()
            here = os.path.dirname(os.path.abspath(__file__))
            out = []
            for args in (["--apply", "--since", since, "--include-today"], ["--emit-hand", "--apply"]):
                r = _sp.run([sys.executable, "apex_settle_sa365.py"] + args, cwd=here,
                            capture_output=True, text=True, timeout=900)
                tail = [ln for ln in (r.stdout or "").splitlines()
                        if ln.startswith(("would settle", "APPLIED", "nothing to write", "settled sim rows", "APPENDED", "nothing to add"))]
                out.append(" | ".join(tail[-2:]) or f"exit {r.returncode}")
            try:   # APEX-BOARDHISTORY-2026-10-25: settled results onto the board history rows (data only)
                r = _sp.run([sys.executable, "apex_board_history.py", "fill"], cwd=here,
                            capture_output=True, text=True, timeout=600)
                out.append("board history " + ((r.stdout or "").strip().splitlines() or ["exit %d" % r.returncode])[-1])
            except Exception as _bh:
                out.append(f"board history fill failed: {type(_bh).__name__}")
            log("FastSettle", " || ".join(out))
        except Exception as _e:
            log("FastSettle", f"run failed (non-fatal): {type(_e).__name__}: {_e}")
        finally:
            _FAST_SETTLE_BUSY["on"] = False

    _threading.Thread(target=_work, name="fast_settle", daemon=True).start()


def _run_auto_eval(from_drop=False):
    """APEX-AUTOEVAL-2026-10-13: score props posted since the last fetch, and
    retry ones skipped for missing odds, without waiting for /fetch."""
    _LAST_AUTOEVAL["ts"] = time.time()   # APEX-PPTHROTTLE-2026-10-16
    # APEX-ALERTCHAIN-2026-10-25: an open new-props card follows this run (scoring N of M -> result);
    # the odds watch's result card and 📅 lines go into it (or into one card) instead of extra posts
    _AC, _key, _scope = None, None, None
    try:
        import apex_alert_chain as _AC
        if _AC.CHAINS_ON:
            _key = _AC.NEWPROPS_KEY if _AC.get(_AC.NEWPROPS_KEY, max_age_min=180) else None
        else:
            _AC = None
    except Exception:
        _AC = None
    _before, _cap0, _t0 = (_published_idents(), _board_status_cap(), datetime.now()) if _key else (None, None, None)
    try:
        import apex_auto_eval as _ae
        if _AC is None:
            log("AutoEval", _ae.run(__import__(__name__)))
        else:
            with _AC.scope(_key) as _scope:
                _scope["t0"] = _AC.hm()
                if _key and not from_drop:
                    _AC.step(_key, "Scoring the new props now (started %s)" % _AC.hm(),
                             "Results in about %d min — this card shows any that make Top 3 / Elite 6 or another board."
                             % _AC.SCORE_MIN, wait_min=_AC.SCORE_MIN)
                log("AutoEval", _ae.run(__import__(__name__)))
    except Exception as _e:
        log("AutoEval", f"run failed (non-fatal): {type(_e).__name__}: {_e}")
        if _key:
            try:
                _AC.fail(_key, f"Scoring failed ({type(_e).__name__}) — the next board check (about 30 min) tries again.")
            except Exception:
                pass
            _key = None
    if _AC is not None and _scope is not None:
        try:
            if _key:
                _newprops_finish(_key, _before, _cap0, _t0, _scope)
            else:
                _flush_scope(_scope)
        except Exception as _e:
            log("AlertChain", f"finish failed (non-fatal): {type(_e).__name__}: {_e}")
    # APEX-ODDSMOVE-2026-10-15 (owner, "Go"): post when an odds move
    # shifted a board / near-board prop 3+ points. Display only.
    try:
        import apex_odds_move_alert as _oma
        log("OddsMove", _oma.run(__import__(__name__)))
    except Exception as _e:
        log("OddsMove", f"run failed (non-fatal): {type(_e).__name__}: {_e}")


def _run_category_gate():
    """APEX-CATGATE-2026-10-20: hourly -- hold a category + direction off the Best boards
    while its settled results run well under Honest Probability; let it back when it recovers."""
    try:
        import apex_category_gate as _cg
        log("CategoryGate", _cg.run())
    except Exception as _e:
        log("CategoryGate", f"run failed (non-fatal): {type(_e).__name__}: {_e}")


def _run_pinnacle():
    """APEX-PINNACLE-2026-10-22: hourly check; at most one successful OddsPapi Pinnacle pull a day (after 3 PM CT)."""
    try:
        import apex_pinnacle as _pin
        log("Pinnacle", _pin.daily())
    except Exception as _e:
        log("Pinnacle", f"run failed (non-fatal): {type(_e).__name__}: {_e}")


def _run_score_request():
    """APEX-LUMPSCORE-2026-10-30 (owner, "score a lump when it comes in"): the 15-min PrizePicks
    check left data/logs/score_request.json for a batch of DROP_ALERT_MIN+ unscored lines -> score it
    now (the open NEW PRIZEPICKS PROPS card turns into the result) instead of at the next board check."""
    try:
        import apex_ops as _ops
        p = os.path.join(_ops.LOG_DIR, "score_request.json")
        if not os.path.exists(p):
            return
        try:
            with open(p) as fh:
                req = json.load(fh) or {}
        except Exception:
            req = {}
        os.remove(p)   # one try per request, before the work, so a crash cannot loop
        try:
            age = (datetime.now(CT).replace(tzinfo=None) - datetime.fromisoformat(str(req.get("t"))[:19])).total_seconds()
        except Exception:
            age = 0
        if age > 20 * 60:
            log("LumpScore", f"stale score request from {req.get('t')} ignored ({age / 60:.0f} min old)")
            return
        log("LumpScore", f"scoring a batch of {req.get('n')} new props now (asked at {str(req.get('t'))[11:16]})")
        try:
            import apex_alert_chain as _AC
            if _AC.CHAINS_ON and _AC.get(_AC.NEWPROPS_KEY, max_age_min=180):
                _AC.np_scoring([], n_score=int(req.get("n") or 0) or None)
        except Exception as _e:
            log("LumpScore", f"card step failed (non-fatal): {type(_e).__name__}: {_e}")
        _run_auto_eval(from_drop=True)
    except Exception as _e:
        log("LumpScore", f"failed (non-fatal): {type(_e).__name__}: {_e}")


def _run_chain_stalls():
    """APEX-ALERTCHAIN-2026-10-25: an #alerts chain card whose next step is 10+ min late turns red."""
    try:
        import apex_alert_chain as _AC
        n = _AC.check_stalls()
        if n:
            log("AlertChain", f"{n} card(s) marked stalled")
    except Exception as _e:
        log("AlertChain", f"stall check failed (non-fatal): {type(_e).__name__}: {_e}")
    _run_score_request()   # APEX-LUMPSCORE-2026-10-30


def _run_reprice():
    try:
        apex_reprice_active()
    except Exception as _e:
        log("Reprice", f"run failed (non-fatal): {type(_e).__name__}: {_e}")


def _run_board_updates_test():
    """APEX-UPDATETRACK-2026-10-24: BOARD UPDATES TEST files + the automatic Tue 10/27 9 AM review
    card (#alerts + #system), then a daily 9 AM line in #system. Posts only when due."""
    try:
        import apex_board_updates_test as _but
        log("UpdatesTest", _but.run())
    except Exception as _e:
        log("UpdatesTest", f"run failed (non-fatal): {type(_e).__name__}: {_e}")


def _run_order_score():
    """APEX-ORDERGUARD-2026-10-25: 9 AM #system order check + order score lines, Monday 9:10 weekly
    order score card, the 20-pair ⚠️ ORDER SCORE warning. Check only; posts each once."""
    try:
        import apex_order_score as _aos
        _r = _aos.run()
        if "nothing due" not in str(_r):
            log("OrderScore", _r)
    except Exception as _e:
        log("OrderScore", f"run failed (non-fatal): {type(_e).__name__}: {_e}")
        try:
            send_discord("alerts", f"\U0001F6A8 ORDER CHECK could not run: the 9 AM order score job failed ({type(_e).__name__})")
        except Exception:
            pass


def _run_fair_paper():
    """APEX-FAIRPAPER-2026-10-25: fair points paper test -- grade the shadow boards, 9 AM #system line,
    the TEST FINISHED card once. Paper only; never touches a live board."""
    try:
        import apex_fair_points_paper as _fpp
        _r = _fpp.run()
        if "nothing due" not in str(_r):
            log("FairPaper", _r)
    except Exception as _e:
        log("FairPaper", f"run failed (non-fatal): {type(_e).__name__}: {_e}")


def _run_thin_tracker():
    """APEX-THINDQ-2026-11-02: thin players' lines and results, own folder; Monday 9 AM #system line."""
    try:
        import apex_thin_tracker as _tt
        _r = _tt.run()
        if not str(_r).startswith("0 new thin-player lines, 0 graded"):
            log("ThinPlayers", _r)
    except Exception as _e:
        log("ThinPlayers", f"run failed (non-fatal): {type(_e).__name__}: {_e}")


def _run_set_games():
    """APEX-SETGAMES-2026-11-01: separate first-set list, off the boards. Local files only."""
    try:
        import apex_set_games as _sg
        _r = _sg.run()
        if not str(_r).startswith("0 new"):
            log("SetGames", _r)
    except Exception as _e:
        log("SetGames", f"run failed (non-fatal): {type(_e).__name__}: {_e}")


def _run_standard_paper():
    """APEX-STANDARDPAPER-2026-10-31: paper test at first post, off the boards. Local files only."""
    try:
        import apex_standard_paper as _stdp
        _r = _stdp.run()
        if not str(_r).startswith("0 new, 0 graded"):
            log("StandardPaper", _r)
    except Exception as _e:
        log("StandardPaper", f"run failed (non-fatal): {type(_e).__name__}: {_e}")


def _run_player_history():
    """APEX-PLAYERDATA-2026-10-31: daily filled match history rebuild after the morning grading, one #system
    line, change-only #alerts. Catches up if 8:30 was missed; posts once a day."""
    try:
        import apex_player_history as _phist
        _r = _phist.run()
        if _r != "nothing due":
            log("PlayerHistory", _r)
    except Exception as _e:
        log("PlayerHistory", f"run failed (non-fatal): {type(_e).__name__}: {_e}")


def _run_learning_card():
    """APEX-LEARNV2-2026-10-24: 11:30 PM CT -- grade stuck picks from props_db, then
    post ONE plain #learning card (posts at most once a night; changes nothing live)."""
    try:
        import apex_learning_v2 as _alv2
        st, res = _alv2.nightly()
        log("LearningCard", f"catch-up {st} | card: {str(res)[:120]}")
    except Exception as _e:
        log("LearningCard", f"run failed (non-fatal): {type(_e).__name__}: {_e}")


def _run_picks_results():
    """APEX-PICKSCLEAN-2026-10-24: ~10 min -- win/loss for board + backup picks into the
    #picks results card (props_db reads only), end-of-day record line once a day is done."""
    try:
        import apex_picks_channel as _pcc
        _r = _pcc.check_results()
        if _r != "results: nothing new":
            log("PicksResults", _r)
    except Exception as _e:
        log("PicksResults", f"run failed (non-fatal): {type(_e).__name__}: {_e}")


def _ops_heartbeat(note=""):
    try:
        import apex_ops as _ops
        _ops.write_heartbeat(note)
    except Exception as _e:
        log("Ops", f"heartbeat write failed (non-fatal): {_e}")

def start_scheduler():
    load_github_config()
    dates = get_system_dates()

    send_discord("system",
        "🚀 **OpenClaw v7.1 starting...**\n"
        "Running system verification..."
    )
    run_system_verification(dates)

    # APEX-5PMFETCH-2026-10-16 (owner, option 1 "Real /fetch"): the 5 PM run is
    # now a real /fetch started by the Discord bot (discord_bot.py fivepm_fetch_loop),
    # posting the /fetch reports in #general. run_pipeline() is kept, not scheduled.
    # schedule.every().day.at("17:00").do(run_pipeline)
    schedule.every().day.at("23:00").do(run_learning)
    schedule.every().day.at("23:05").do(run_elo_rebuild)  # 2026-08-12: real gap fix, see run_elo_rebuild() docstring — ADJUST TIME if the collector finishes later
    schedule.every().day.at("23:15").do(run_moneyline_settlement)  # staggered 15min after props learning
    schedule.every().day.at("23:20").do(run_pending_retry)  # APEX-RETRYENGINE-2026-10-05: retries the whole pending backlog nightly
    schedule.every().day.at("23:30").do(_run_learning_card)  # APEX-LEARNV2-2026-10-24: one clean #learning card
    schedule.every().day.at("23:59").do(run_reset)
    # APEX-OPS-2026-10-10: heartbeat every 15 min so the watchdog can tell
    # "alive" from "stuck/dead"; one immediate beat at startup.
    schedule.every(15).minutes.do(_ops_heartbeat)
    schedule.every(30).minutes.do(_run_reprice)   # APEX-REPRICE-2026-10-11
    # APEX-PPTHROTTLE-2026-10-16 (owner, "Go"): board watch, auto-eval
    # and the props snapshot used to be three separate 30-min jobs, each
    # pulling the PrizePicks board in the same second (PrizePicks answered
    # the third with 429). They now run as one cycle on one shared pull
    # (apex_ops.pp_partner_board), every 28-32 min (random) so the timing is
    # never clockwork. The drop-window watch is 9-11 min (random).
    schedule.every(28).to(32).minutes.do(_run_board_cycle)   # board watch + auto-eval + props snapshot
    schedule.every(1).minutes.do(_cycle_catchup)             # APEX-SCORINGCATCHUP-2026-11-01: once, after a restart
    schedule.every(2).minutes.do(_run_chain_stalls)          # APEX-ALERTCHAIN-2026-10-25: late chain cards turn red
    schedule.every(9).to(11).minutes.do(_run_drop_watch)     # APEX-DROPALERT-2026-10-14: ~10-min checks in drop windows
    schedule.every(5).minutes.do(_hourly_due, "props_settle", _run_props_db, "settle")        # APEX-PROPSDB-2026-10-13; hourly since APEX-FASTSETTLE-2026-10-19
    schedule.every(5).minutes.do(_hourly_due, "fast_settle", _run_fast_settle)               # APEX-FASTSETTLE-2026-10-19: hourly ledger win/loss check
    schedule.every(5).minutes.do(_hourly_due, "category_gate", _run_category_gate)             # APEX-CATGATE-2026-10-20: Best-board category hold, change-only #alerts
    schedule.every(5).minutes.do(_hourly_due, "pinnacle", _run_pinnacle)                  # APEX-PINNACLE-2026-10-22: one Pinnacle pull a day (OddsPapi)
    schedule.every().day.at("09:00").do(_run_board_updates_test)   # APEX-UPDATETRACK-2026-10-24: 10/27 review + daily line
    schedule.every(15).minutes.do(_run_board_updates_test)         # catch-up if 9:00 was missed (posts once a day)
    schedule.every(10).minutes.do(_run_picks_results)              # APEX-PICKSCLEAN-2026-10-24: #picks win/loss card
    schedule.every().day.at("09:00").do(_run_order_score)          # APEX-ORDERGUARD-2026-10-25: 9 AM lines
    schedule.every().day.at("09:10").do(_run_order_score)          # Monday weekly card
    schedule.every(15).minutes.do(_run_order_score)                # catch-up (posts once each)
    schedule.every().day.at("09:00").do(_run_fair_paper)           # APEX-FAIRPAPER-2026-10-25: 9 AM line
    schedule.every(10).minutes.do(_run_set_games)                  # APEX-SETGAMES-2026-11-01: separate list
    schedule.every(30).minutes.do(_run_thin_tracker)               # APEX-THINDQ-2026-11-02: thin players' own file
    schedule.every(10).minutes.do(_run_standard_paper)             # APEX-STANDARDPAPER-2026-10-31: paper only
    schedule.every(15).minutes.do(_run_player_history)             # APEX-PLAYERDATA-2026-10-31: due from 8:30, runs once a day
    schedule.every(15).minutes.do(_run_fair_paper)                 # grade + catch-up + finish card (posts once each)
    _run_board_watch()   # baseline the board now so the first real tick reports only what is new
    _ops_heartbeat("startup")
    # APEX-OPS-2026-10-10: republish the boards once on every scheduler
    # start, so a deploy/restart never leaves commands reading a snapshot
    # built under older rules. Fail-open.
    try:
        import apex_edge_tracker as _aet_rp
        _rp = _aet_rp.republish_boards(source="restart")
        log("Ops", f"startup republish: {_rp}")
    except Exception as _e:
        log("Ops", f"startup republish failed (non-fatal): {type(_e).__name__}: {_e}")

    send_discord("system",
        "✅ **Scheduler active**\n"
        "• 5:00 PM CT — /fetch (run by the Discord bot)\n"
        "• 11:00 PM CT — Learning (props)\n"
        "• 11:15 PM CT — Moneyline settlement\n"
        "• 11:20 PM CT — Pending retry engine\n"
        "• 11:30 PM CT — #learning card\n"
        "• 11:59 PM CT — Reset\n"
        "• Manual — #general anytime"
    )

    while True:
        schedule.run_pending()
        time.sleep(30)

# ================================================================
# ENTRY POINT
# ================================================================

if __name__ == "__main__":
    import sys
    load_github_config()
    dates   = get_system_dates()
    weights = load_weights()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "verify":
            run_system_verification(dates)
        elif cmd == "test":
            props = [{"player":"Test Player","opponent":"Test Opponent",
                      "stat_type":"total_games","line":22.5,
                      "tournament":"Test","surface":"clay",
                      "tournament_stage":1,"is_grand_slam":False,
                      "both_directions_available":True,"prop_type":"standard",
                      "match_date":dates["match_date"],"same_day_3set_count":0}]
            supervisor_agent(props, dates, weights, mode="test")
        elif cmd == "learn":
            run_learning()
        elif cmd == "bestbets":
            # APEX-BB-002a: on-demand Best Bets from the current evaluated board.
            import apex_edge_tracker as _aet2
            _arg = (sys.argv[2] if len(sys.argv) > 2 else "").strip().lower()
            _cat = {"dfs": "dfs", "double_faults": "dfs", "fantasy": "fantasy_score",
                    "fantasy_score": "fantasy_score", "aces": "aces", "breaks": "breaks",
                    "games_won": "games_won", "gameswon": "games_won",
                    "tg": "total_games", "total_games": "total_games"}.get(_arg)
            _worst = _arg in ("worst", "avoid")
            _picks, _note = _aet2.load_current_board()
            _txt = _note if _note else _aet2.best_bets_detail(_picks, category=_cat, worst=_worst)
            print(_txt)
            send_discord("general", _txt)
        elif cmd == "reset":
            run_reset()
        elif cmd == "fetch":
            props, source = fetch_props_with_fallback(dates["match_date"], dates)
            print(f"Fetched {len(props)} props via {source}")
        elif cmd == "selftest":
            # 1) TGW counter: 7-6 6-4 WIN must be 13, not 10
            assert count_tgw([(7,6),(6,4)], True)  == 13, "TGW winner miscount"
            assert count_tgw([(6,4),(6,4)], False) == 8,  "TGW underdog miscount"
            # 2) Underdog competitive loss should NOT auto-LESS
            dist = tgw_distribution([[(6,2),(6,2)], [(6,4),(6,4)], [(7,5),(6,4)]], False, 7.5)
            print("TGW dist lean (expect not a clean LESS):", dist["lean"],
                  "| over", dist["over_count"], "under", dist["under_count"])
            # 3) Hold gate: no hold data must cap below 0.70
            fake = {"stat_type":"total_games","direction":"LESS","line":21.5,
                    "match_type":"COMPETITIVE","dominance_level":"HARD","competitive_reqs":4,
                    "eval_confidence": 0.75,
                    "research_data":{"player":{"serve_hold_pct":None,"missing_fields":[]},
                                     "opponent":{"serve_hold_pct":None,"missing_fields":[]},
                                     "match_context":{"stage":2}}}
            scored = scoring_agent(fake, load_weights())
            assert scored["confidence"] < 0.70, f"HOLD GATE FAILED — got {scored['confidence']:.0%}"
            print("PASS: hold gate caps missing-hold prop at", f"{scored['confidence']:.0%}")

            # APEX-GOBLIN-FLOOR-SELFTEST-2026-09-24: goblin below 72.1% must be blocked
            fake_gob_low = {"stat_type":"aces","direction":"MORE","line":7.5,
                    "match_type":"COMPETITIVE","dominance_level":"SOFT","competitive_reqs":2,
                    "eval_confidence": 0.60, "prop_type_classified":"goblin",
                    "research_data":{"player":{"serve_hold_pct":70,"missing_fields":[]},
                                     "opponent":{"serve_hold_pct":70,"missing_fields":[]},
                                     "match_context":{"stage":2}}}
            scored_gob_low = scoring_agent(fake_gob_low, load_weights())
            assert scored_gob_low["status"] == "GOBLIN_BELOW_BREAKEVEN", \
                f"GOBLIN FLOOR FAILED TO BLOCK — status was {scored_gob_low.get('status')}, " \
                f"confidence {scored_gob_low.get('confidence')}"
            assert scored_gob_low["confidence"] is None, "GOBLIN FLOOR should null confidence on block"
            print("PASS: goblin below break-even correctly blocked -> GOBLIN_BELOW_BREAKEVEN")

            # goblin comfortably above 72.1% must pass through untouched
            fake_gob_high = {"stat_type":"aces","direction":"MORE","line":4.5,
                    "match_type":"COMPETITIVE","dominance_level":"HARD","competitive_reqs":4,
                    "eval_confidence": 0.90, "prop_type_classified":"goblin",
                    "research_data":{"player":{"serve_hold_pct":85,"avg_aces_v8":8,"missing_fields":[]},
                                     "opponent":{"serve_hold_pct":60,"avg_aces_v8":6,"missing_fields":[]},
                                     "match_context":{"stage":2}}}
            scored_gob_high = scoring_agent(fake_gob_high, load_weights())
            assert scored_gob_high.get("status") != "GOBLIN_BELOW_BREAKEVEN", \
                f"GOBLIN FLOOR FALSE POSITIVE — blocked a pick at {scored_gob_high.get('confidence')}"
            print("PASS: goblin above break-even passes through at",
                  f"{scored_gob_high['confidence']:.0%}" if scored_gob_high.get("confidence") is not None else "N/A")

            print("ALL SELFTESTS PASSED — walls are live.")
        elif cmd == "--revert":
            if len(sys.argv) < 3:
                print("Usage: python3 openclaw_core.py --revert SIGNAL_NAME")
                print("       python3 openclaw_core.py --revert last")
                sys.exit(1)
            print(revert_weight_change(sys.argv[2], dates))
        elif cmd == "--approve":
            if len(sys.argv) < 3 or not sys.argv[2].isdigit():
                print("Usage: python3 openclaw_core.py --approve N")
                sys.exit(1)
            print(apply_proposal_decision(int(sys.argv[2]), "APPROVE", dates))
        elif cmd == "--reject":
            if len(sys.argv) < 3 or not sys.argv[2].isdigit():
                print("Usage: python3 openclaw_core.py --reject N")
                sys.exit(1)
            print(apply_proposal_decision(int(sys.argv[2]), "REJECT", dates))
        elif cmd == "--moneyline":
            if len(sys.argv) < 3:
                print('Usage: python3 openclaw_core.py --moneyline "Player1 | Player2 | surface | tour"')
                sys.exit(1)
            raw = sys.argv[2]
            parts = [p.strip() for p in raw.split("|")]
            ml_player, ml_opponent = parts[0], parts[1]
            ml_surface = parts[2] if len(parts) > 2 else "hard"
            ml_tour = parts[3] if len(parts) > 3 else "atp"
            ml_result = moneyline_evaluation_agent(ml_player, ml_opponent, ml_surface, tour=ml_tour)
            print("=" * 60)
            print(f"MONEYLINE RESULT — {ml_player} vs {ml_opponent}")
            print("=" * 60)
            print(json.dumps({k: v for k, v in ml_result.items() if k != "model_output"},
                              indent=2, default=str))
            if ml_result.get("status") in ("GREEN", "YELLOW"):
                _cli_post_ok = send_discord("moneyline",
                    f"🎾 **{ml_result.get('winner')} — MONEYLINE WIN** — "
                    f"{'🟢' if ml_result['status']=='GREEN' else '🟡'} {ml_result.get('confidence')}% "
                    f"({ml_result.get('signal_agreement')} signals)\n{ml_result.get('note') or ''}"
                )
                d1.heartbeat("posting", "WORKING", detail=f"{ml_player} vs {ml_opponent}", _logger=log)
                if _cli_post_ok:
                    d1.transition(ml_result.get("eval_id"), "POSTED", posted=1, _logger=log)
                    d1.heartbeat("posting", "IDLE", increment_success=True, _logger=log)
                else:
                    d1.transition(ml_result.get("eval_id"), "SCORING_COMPLETE",
                                   detail="Discord post failed -- pick never reached Discord",
                                   posted=0, predicted_winner=ml_result.get("winner"), _logger=log)
                    d1.heartbeat("posting", "IDLE", increment_failure=True, _logger=log)
            else:
                send_discord("moneyline",
                    f"⛔ NO PLAY — {ml_player} vs {ml_opponent}\n{ml_result.get('reason', '')}"
                )
        elif cmd == "--slip-entry":
            raw = sys.stdin.read().strip()
            if not raw:
                print("No slip entry text received.")
                sys.exit(1)
            print(parse_slip_entry(raw, dates))
        elif cmd == "--slip-result":
            raw = sys.stdin.read().strip()
            if not raw:
                print("No slip result text received.")
                sys.exit(1)
            print(parse_slip_result(raw, dates, weights))
        elif cmd == "--discord":
            raw = sys.stdin.read().strip()
            if not raw:
                print("No prop text received.")
                sys.exit(1)
            props = []
            for entry in [l.strip() for l in raw.splitlines() if l.strip()]:
                parts = [p.strip() for p in entry.split("|")]
                if len(parts) >= 4:
                    try:
                        # Parse prop type from position 8 (optional)
                        # Accepts: STANDARD, GOBLIN, RED_DEMON
                        # Defaults to STANDARD if not specified
                        raw_ptype = parts[8].lower().strip() if len(parts) > 8 else "standard"
                        ptype_map = {
                            "standard":  "standard",
                            "goblin":    "goblin",
                            "red_demon": "red_demon",
                            "red demon": "red_demon",
                            "demon":     "demon",
                        }
                        prop_type = ptype_map.get(raw_ptype, "standard")

                        props.append({
                            "player":                    parts[0],
                            "opponent":                  parts[1],
                            "stat_type":                 normalize_stat_type(parts[2]),
                            "line":                      float(parts[3]),
                            "surface":                   parts[4] if len(parts) > 4 else "hard",
                            "tournament_stage":          int(parts[5]) if len(parts) > 5 else 1,
                            "tournament":                parts[6] if len(parts) > 6 else "Unknown",
                            "is_grand_slam":             len(parts) > 7 and parts[7].lower() == "true",
                            "prop_type":                 prop_type,
                            "is_goblin":                 prop_type == "goblin",
                            "is_red_demon":              prop_type == "red_demon",
                            "is_demon":                  prop_type == "demon",
                            "both_directions_available": prop_type == "standard",
                            "match_date":                dates["match_date"],
                            "same_day_3set_count":       0,
                        })
                    except Exception as e:
                        print(f"Parse error: {entry} -- {e}")
            if props:
                supervisor_agent(props, dates, weights, mode="discord")
            else:
                print("No valid props parsed.")
                print("Format: Player | Opponent | stat_type | line | surface | stage | tournament | is_grand_slam | prop_type")
                print("prop_type options: STANDARD (default) | GOBLIN | RED_DEMON")
    else:
        start_scheduler()

# © 2026 Demetrius Reynolds — Project Apex Intelligence™. All rights reserved.
