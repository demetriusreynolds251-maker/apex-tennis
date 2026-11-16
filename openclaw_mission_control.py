#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OPENCLAW MISSION CONTROL — WEB v2.0 (2026-07-10)
All six screens functional. Single file, stdlib only, discovery-first,
read-only against pipeline state (writes only data/ops_center/).

NEW IN v2:
- OUTCOME AUTO-DISCOVERY: parses the live "✅ WIN / ❌ LOSS" alert lines
  the bot writes to its own log (added this week) and links them to
  archived picks — GREEN/ML trackers fill themselves. Manual overrides
  still win. Zero bot changes.
- OPERATIONS: stage durations, retry counts, stall detection.
- INSPECTOR: explicit Gate 11 / Truth Lock / REQ / Dominance / Outcome
  status chips, red alerts for unexplained or compliance-altered moves.
- VALIDATION CENTER: run history table, latest report, red movement.
- RULE ROADMAP: live table joined to ledger counts + per-rule
  drill-down (affected picks + record).
- BANKROLL CENTER: slip table, daily/weekly/monthly P&L, growth chart.
Run:  python3 openclaw_mission_control.py       # port 8788
Open: http://<mac-mini-tailscale-name>:8788
"""
import json, re, sys, os, base64, hmac, datetime as dt, subprocess
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import openclaw_ops as ops
import warehouse_ops as wops
import elo_ops as eo   # 2026-08-07: ELO coverage panel, Warehouse Command screen
import rule_registry   # 2026-08-09: predictive rule audit panel, real Category 1 tests only
import estimator_calibration_ops as eco
import dataset_a_capture as d1
import openclaw_core as oc
import openclaw_intake as intake

# ATE (Apex Trace Engine) — read-only. Enhances the existing pick_detail /
# pick_from_eval / renderPickDetail workflow with observability data; never
# a second source of truth, never a separate screen. Optional — Mission
# Control must run identically with or without ate_queries present.
_ATE_AVAILABLE = False
try:
    import ate_queries as ate_q
    _ATE_AVAILABLE = True
    print("[ATE] Mission Control: trace query layer loaded successfully")
except ImportError as _ate_err:
    print(f"[ATE] Mission Control: query layer not found — running without ATE data ({_ate_err})")

_CHAT_AVAILABLE = False
try:
    import command_chat_engine as chat_engine
    import command_chat_writer as chat_writer
    _CHAT_AVAILABLE = True
    print("[CommandChat] engine loaded successfully")
except ImportError as _chat_err:
    print(f"[CommandChat] engine not found — Command Chat unavailable ({_chat_err})")

PORT = 8788

# ── OUTCOME AUTO-DISCOVERY (fixes GREEN/ML 0-of-goal) ───────────────
_WINLOSS = re.compile(r"(✅ WIN|❌ LOSS) — (.+?) (MORE|LESS|WIN) ([\d.]+)?\s*(\w+)?")

def discovered_outcomes():
    """(player, stat) -> HIT/BUST from all OpenClaw sources that carry
    per-pick identity. Primary: the bot's own WIN/LOSS alert lines
    (player+stat+outcome). Secondary: pending_results.json, which the
    learning run writes with player/stat/outcome before it posts. Signal
    history is intentionally NOT used for per-pick linking — it stores
    only date/signal/outcome with no player, so it cannot identify a
    specific pick (it feeds aggregate readiness, not tracker linking)."""
    found = {}
    # source 1: live WIN/LOSS alerts in the bot log
    f = oc.DATA_DIR / "logs" / "discord_bot.log"
    if f.exists():
        for ln in f.read_text(errors="replace").splitlines():
            m = _WINLOSS.search(ln)
            if m:
                res = "HIT" if "WIN" in m.group(1) else "BUST"
                found[(m.group(2).strip().lower(), (m.group(5) or "").lower())] = res
    # source 2: pending_results / learning outputs that carry identity
    for name in ("pending_results.json",):
        pf = oc.DATA_DIR / "learning" / name
        for r in ops._jload(pf, []):
            pl = str(r.get("player", "")).strip().lower()
            stat = str(r.get("stat_type", r.get("market", ""))).strip().lower()
            oc_out = r.get("outcome")
            if pl and oc_out in ("HIT", "BUST") and (pl, stat) not in found:
                found[(pl, stat)] = oc_out
    return found

def outcome_of(p, overrides, disc):
    k = ops._okey(p)
    if k in overrides: return overrides[k]                    # manual wins
    if p.get("outcome") in ("HIT", "BUST"): return p["outcome"]
    return disc.get((str(p.get("player", "")).lower(),
                     str(p.get("stat_type", "")).lower()))

def tracker(kind, cfg):
    """
    Real fix (2026-08-04): this used to read via ops.iter_picks(), which
    reads the picks_*.json archive files. Confirmed on-disk (same finding
    _rows_from_dataset_a() below already documented for Pick Results):
    those files are no longer written at all -- zero exist on disk,
    only reasoning_*.txt files do. This tracker was therefore silently
    stuck at 0-of-goal for BOTH props and moneyline regardless of how
    many real picks existed, not a moneyline-specific gap. Same real
    fix as Pick Results already got: read Dataset A's evaluations table
    directly -- it's the actual, live, growing source of truth.
    """
    etype = "PROP" if kind == "green" else "MONEYLINE"
    try:
        rows = ops._warehouse_query(
            "SELECT eval_id, final_grade, outcome, final_confidence, created_at "
            "FROM evaluations WHERE evaluation_type = ? AND final_grade IS NOT NULL "
            "ORDER BY created_at ASC",
            (etype,)
        )
    except Exception:
        rows = []

    if kind == "green":
        rows = [r for r in rows if r.get("final_grade") == "GREEN"]
        goal, target = cfg["green_goal"], cfg["green_target_hit_rate"]
    else:
        # Mirrors the original ML definition (GREEN or YELLOW both count
        # as a real graded moneyline pick, matching how "WIN" direction
        # was set for either grade at SCORING_COMPLETE).
        rows = [r for r in rows if r.get("final_grade") in ("GREEN", "YELLOW")]
        goal, target = cfg["ml_goal"], cfg["ml_target_hit_rate"]

    overrides = ops._jload(ops.OUTCOMES, {})
    done = []
    for r in rows:
        o = r.get("outcome") or overrides.get(r.get("eval_id"))
        if o in ("HIT", "BUST"):
            done.append((r, o))

    wins = sum(1 for _, o in done if o == "HIT")
    losses = len(done) - wins
    cur = 0; last = None; longest = 0
    for _, o in done:
        cur = cur + 1 if o == last else 1
        last = o
        longest = max(longest, cur if o == "HIT" else longest)
    hit = wins / len(done) if done else None
    if hit is None: status = "⚪"
    elif hit >= target: status = "✅"
    elif hit >= target - cfg["near_target_band"]: status = "🟡"
    else: status = "🔴"
    return {"completed": len(done), "goal": goal, "wins": wins, "losses": losses,
            "hit": hit, "status": status,
            "streak": (("W" if last == "HIT" else "L") + str(cur)) if done else "-",
            "longest_w": longest, "pending": len(rows) - len(done),
            "avg_conf": (sum(r.get("final_confidence") or 0 for r in rows) / len(rows)) if rows else 0}

# ── OPERATIONS: timestamped lanes (durations, retries, stalls) ──────
_TS = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})? [AP]M) CT\]")

def _mins(t1, t2):
    def parse(t):
        return dt.datetime.strptime(t, "%I:%M %p" if t.count(":") == 1 else "%I:%M:%S %p")
    try:
        d = (parse(t2) - parse(t1)).total_seconds() / 60
        return d + (1440 if d < -720 else 0)
    except Exception:
        return None

def operations():
    lines = ops.read_log_tail(800)
    lanes, first_seen, retries, last_ts = {}, {}, defaultdict(int), {}
    for ln in lines:
        tsm = _TS.match(ln); ts = tsm.group(1) if tsm else None
        nm = ops._NAME_PAT.search(ln)
        if not nm: continue
        name = nm.group(1)[:24]
        lane = lanes.setdefault(name, {s: "⚪" for s in ops.STAGES})
        for stage, pat in ops._STAGE_PAT.items():
            if pat.search(ln):
                for prev in ops.STAGES[:ops.STAGES.index(stage)]:
                    if lane[prev] == "⚪": lane[prev] = "🟢"
                lane[stage] = "🔴" if ops._FAIL_PAT.search(ln) else "🟡"
                if ts and (name, stage) not in first_seen:
                    first_seen[(name, stage)] = ts
                if ts: last_ts[name] = ts
        if re.search(r"attempt 2|retry", ln, re.I): retries[name] += 1
        if "posted" in ln.lower() or "✅" in ln:
            for s in ops.STAGES: lane[s] = "🟢" if lane[s] != "🔴" else "🔴"
    rows = []
    now = dt.datetime.now(oc.CT).strftime("%I:%M %p").lstrip("0")
    for name, lane in lanes.items():
        durs = {}
        for i, s in enumerate(ops.STAGES[:-1]):
            t0 = first_seen.get((name, s))
            t1 = first_seen.get((name, ops.STAGES[i + 1]))
            if t0 and t1:
                d = _mins(t0, t1)
                if d is not None: durs[s] = round(d, 1)
        running = [s for s in ops.STAGES if lane[s] == "🟡"]
        stalled = False
        if running and last_ts.get(name):
            idle = _mins(last_ts[name], now)
            stalled = idle is not None and idle > 2
        rows.append({"name": name, "lane": lane, "durs": durs,
                     "retries": retries.get(name, 0), "stalled": stalled})
    return rows[-10:]

# ── VALIDATION / ROADMAP / BANKROLL views ───────────────────────────
def validation_view():
    hist = ops._jload(ops.VALID_DIR / "run_history.json", [])
    reports = sorted(ops.VALID_DIR.glob("green_validation_*.md"))
    latest = reports[-1].read_text(errors="replace") if reports else ""
    return {"history": hist[-12:], "latest_report": latest[-12000:], "runs": len(hist)}

ROADMAP = [
 {"rule": "Gate 11", "status": "KEEP", "impact": "REJECT / 69% cap unverifiable",
  "keys": ["Gate 11"], "purpose": "Never grade a prop on an unverified match.",
  "deps": "V8 availability"},
 {"rule": "Gate 11-B", "status": "KEEP", "impact": "64% cap non-HARD bo5 LESS",
  "keys": ["Gate 11-B", "format"], "purpose": "bo3 baselines must not price bo5.",
  "deps": "dominance_level (fixed 07-10)"},
 {"rule": "Rule 24 (class gap)", "status": "REVIEW", "impact": "reasoning constraint",
  "keys": ["class-gap", "Correction 24"], "purpose": "Form vs stronger opposition can't flip direction across a class gap.",
  "deps": "rank data"},
 {"rule": "Rule 30 (rest days)", "status": "REVIEW", "impact": "-4% / CFEF by turnaround",
  "keys": ["Rule30", "Rule 30"], "purpose": "Fatigue priced by actual turnaround.",
  "deps": "match dates, CFEF"},
 {"rule": "Truth Lock", "status": "KEEP", "impact": "Cat-A when violated",
  "keys": ["TruthLock", "Truth Lock"], "purpose": "Can't call confirmed data unknown.",
  "deps": "ground-truth packet"},
 {"rule": "Conservation", "status": "KEEP", "impact": "each fact charged once",
  "keys": ["anchor"], "purpose": "Every uncertainty charged exactly once.", "deps": "everything"},
 {"rule": "Dominance (LESS)", "status": "KEEP", "impact": "69/66/62 ceilings",
  "keys": ["dominance ceiling", "LESS-path"], "purpose": "LESS picks priced by dominance quality.",
  "deps": "dominance_level"},
 {"rule": "Proven-competitive REQs", "status": "KEEP", "impact": "4/4=GREEN-eligible else 69",
  "keys": ["REQs ceiling", "MORE path"], "purpose": "MORE picks must prove the path.",
  "deps": "REQ checks"},
 {"rule": "Serve-stat gate", "status": "KEEP", "impact": "69% coin-flip band",
  "keys": ["Serve-stat", "coin-flip"], "purpose": "DF/Aces inside the buffer band need the proven path.",
  "deps": "V8 averages"},
 {"rule": "BP Rule v7.3", "status": "AUDIT", "impact": "72/75/78 ceilings + suppression",
  "keys": ["BP MORE", "BP opp hold"], "purpose": "BP direction from return dominance.",
  "deps": "hold data"},
 {"rule": "Thin-data / hold caps", "status": "KEEP", "impact": "68/62 caps",
  "keys": ["Thin-data", "hold"], "purpose": "No GREEN without load-bearing data.",
  "deps": "V8/serve data"},
 {"rule": "Correction 5 floor", "status": "REVIEW", "impact": "65% floor (only upward move)",
  "keys": ["floor"], "purpose": "Supported directions never post below 65%.",
  "deps": "eligibility"},
 {"rule": "Goblin/Red Demon", "status": "KEEP", "impact": "55% caps, MORE-only",
  "keys": ["Goblin", "Red Demon"], "purpose": "Operator-protective type locks.",
  "deps": "prop typing"},
 {"rule": "Compliance A/B/C", "status": "KEEP", "impact": "-3%/A flag, B free, C=no pick",
  "keys": ["Compliance", "Cat-A"], "purpose": "Audit costs only for real violations.",
  "deps": "auditor"},
]

def roadmap_view():
    overrides = ops._jload(ops.OUTCOMES, {}); disc = discovered_outcomes()
    allp = list(ops.iter_picks("props"))[-400:]
    out = []
    for r in ROADMAP:
        touched = [p for p in allp if any(
            any(k.lower() in a.lower() for k in r["keys"])
            for a in (p.get("score_adjustments") or []))]
        rec = [outcome_of(p, overrides, disc) for p in touched]
        out.append({**r, "fired": len(touched),
                    "record": f"{rec.count('HIT')}-{rec.count('BUST')}",
                    "picks": [{"key": ops._okey(p), "player": p.get("player"),
                               "conf": p.get("confidence"), "color": p.get("color"),
                               "outcome": outcome_of(p, overrides, disc)}
                              for p in touched[-12:]]})
    return out

def bankroll_view(cfg):
    s = ops.slips(cfg)
    daily = defaultdict(float)
    for e in s["entries"]:
        daily[e.get("date", "?")] += e.get("profit", 0)
    days = sorted(daily.items())
    def bucket(fmt):
        b = defaultdict(float)
        for d, v in days:
            try: b[dt.datetime.strptime(d, "%Y-%m-%d").strftime(fmt)] += v
            except Exception: b[d] += v
        return sorted(b.items())
    growth, run = [], cfg["starting_bankroll"]
    for d, v in days:
        run += v; growth.append([d, round(run, 2)])
    return {**{k: s[k] for k in ("n", "six", "five", "loss", "roi", "profit", "bankroll")},
            "entries": s["entries"][-40:][::-1], "daily": days[-30:],
            "weekly": bucket("%G-W%V")[-12:], "monthly": bucket("%Y-%m")[-12:],
            "growth": growth[-60:], "start": cfg["starting_bankroll"],
            "target": cfg["target_bankroll"]}

# ── canvas + inspector ──────────────────────────────────────────────
def todays_picks():
    out = []; today = ops.today_str()
    for p in ops.iter_picks("props"):
        if p.get("_date") == today or p.get("match_date") == today: out.append(p)
    if not out:
        allp = list(ops.iter_picks("props"))
        if allp:
            last = allp[-1]["_date"]; out = [p for p in allp if p["_date"] == last]
    return out

def _reasoning_for(p):
    pat = re.sub(r"[^\w]+", "_", str(p.get("player", ""))).strip("_")
    hits = sorted(oc.ARCHIVE_DIR.glob(f"reasoning_*{pat}*.txt"))
    return hits[-1].read_text(errors="replace")[-24000:] if hits else ""

def pick_detail(key):
    key_l = key.strip().lower()
    overrides = ops._jload(ops.OUTCOMES, {}); disc = discovered_outcomes()
    for p in todays_picks() + list(ops.iter_picks("props"))[-300:]:
        if ops._okey(p).lower() != key_l: continue
        txt = _reasoning_for(p)
        adjs = p.get("score_adjustments") or []
        low = (txt + json.dumps(adjs)).lower()
        flags = p.get("compliance_flags") or []
        gate11 = ("UNVERIFIED" if p.get("gate11_unverified")
                  else ("PASS" if p.get("confidence") is not None else "?"))
        tl = "FLAGGED" if any("truthlock" in str(f.get("rule", "")).lower() for f in flags) else "PASS"
        alerts = []
        if (p.get("eval_confidence") is not None and p.get("confidence") is not None
                and abs(p["eval_confidence"] - p["confidence"]) >= 0.005 and not adjs):
            alerts.append("UNEXPLAINED confidence change — empty ledger — HIGH-SEVERITY defect")
        if any("Compliance" in a for a in adjs):
            alerts.append("Compliance altered confidence — review flags")
        # Feature-echo heuristic (Priority 2): one root fact charged in
        # 2+ ledger entries = possible double-count / confidence leakage.
        echo_roots = {"hold": ["hold"], "thin": ["thin"], "gap": ["gap", "class"],
                      "fatigue": ["fatigue", "rule 30", "rule30", "turnaround"],
                      "format": ["format", "bo5", "gate 11-b"],
                      "dominance": ["dominance", "edge", "soft"]}
        echoes = []
        for root, words in echo_roots.items():
            hits = [a for a in adjs if any(w in a.lower() for w in words)]
            if len(hits) >= 2:
                echoes.append(f"{root} charged {len(hits)}× — possible confidence leakage")
        alerts += echoes
        feat_echo = "DETECTED" if echoes else "clean"
        flow = [f"eval {p.get('eval_confidence', 0):.0%}"] + adjs + \
               [f"FINAL {p.get('confidence', 0):.0%} {p.get('color', '?')}"]
        return {"pick": {k: v for k, v in p.items() if not k.startswith("research")},
                "flow": flow, "alerts": alerts, "flags": flags,
                "status": {"Gate 11": gate11, "Truth Lock": tl,
                           "Rule 24": "ACTIVE" if any(k in low for k in ("class-gap", "correction 24")) else "—",
                           "Rule 30": "ACTIVE" if any(k in low for k in ("rule30", "rule 30", "turnaround")) else "—",
                           "Dominance": p.get("dominance_level", "?"),
                           "REQs": f"{p.get('competitive_reqs', '?')}/4",
                           "Feature Echo": feat_echo,
                           "Outcome": outcome_of(p, overrides, disc) or "PENDING"},
                "reasoning": txt}
    return {"error": "pick not found"}

def ate_bundle_for(eval_id):
    """
    Read-only lookup of everything ATE captured for one evaluation.
    Returns None (not an empty dict) when there's nothing to show —
    either eval_id is missing/unknown, ATE isn't available, or this
    evaluation predates ATE's deployment — so the UI can render a
    plain "no ATE trace captured" line instead of three empty
    sections. Never raises: a problem here must never break the
    existing pick_detail / pick_from_eval response it's attached to.
    """
    if not _ATE_AVAILABLE or not eval_id:
        return None
    try:
        bundle = ate_q.get_full_trace_bundle(eval_id)
        if not bundle["traces"] and not bundle["rule_activations"] and not bundle["checkpoint_stamps"]:
            return None
        return bundle
    except Exception:
        return None

def _with_ate(d, eval_id=None):
    """
    Merges ate_bundle_for's result into an existing pick_detail /
    pick_from_eval response under a new "ate" key. Additive only —
    every existing key in d is untouched.

    eval_id is accepted explicitly rather than read from d["pick"] --
    confirmed against the real openclaw_ops.py (this session) that
    pick_from_eval()'s returned "pick" dict does NOT carry eval_id
    (it's reshaped down to player/opponent/line/stat_type/direction/
    confidence/color only, even though the Dataset A row it read has
    a real eval_id column), and the archive JSON pick_detail() reads
    predates eval_id entirely (confirmed: not present under any key
    in a real archived pick record). Callers that already know the
    eval_id (e.g. the /api/pick_eval route, which receives it as the
    "id" query param) should pass it explicitly. The d["pick"] lookup
    is kept only as a harmless fallback for a future pick shape that
    does carry it.
    """
    if isinstance(d, dict) and not d.get("error") and d.get("pick"):
        d["ate"] = ate_bundle_for(eval_id or d["pick"].get("eval_id"))
    return d

def _find_eval_id_for_pick(player, market, line):
    """
    Matches an archive pick to its Dataset A evaluation record, so Pick
    Results can link to the same reasoning detail view every other
    screen already uses. Deliberately conservative: attaches an eval_id
    ONLY when exactly one evaluations row matches player+market+line.
    Zero matches -> None (most likely predates Dataset A logging,
    2026-07-23). Multiple matches -> also None, not a best guess --
    showing the WRONG pick's reasoning as if certain would be worse
    than showing nothing, so ambiguous cases are left unclickable
    rather than resolved by a guess (same discipline as the rest of
    this project: no fabricated confidence).
    """
    if not player or not market:
        return None
    try:
        rows = ops._warehouse_query(
            "SELECT eval_id FROM evaluations WHERE player = ? AND market = ? "
            "AND (line = ? OR (line IS NULL AND ? IS NULL))",
            (player, market, line, line)
        )
    except Exception:
        return None
    return rows[0]["eval_id"] if len(rows) == 1 else None


def system_issues_view(limit=40):
    """
    Unified 'what's actually gone wrong' view (2026-07-26) -- pulls
    together every real failure signal that already exists in this
    system but was scattered across different places (or nowhere at
    all) before this:

    1. Dataset A write failures -- d1.get_recent_capture_failures().
       Real errors from the capture layer itself (a save that broke).
    2. Rejected/failed EVALUATIONS -- lifecycle_state IN ('REJECTED',
       'FAILED') in the evaluations table. This data was already being
       recorded by terminate() this whole time (termination_reason,
       termination_detail) but nothing in Mission Control ever showed
       it anywhere. Different from #1: these are evaluations that ran
       and were deliberately rejected or broke mid-pipeline, not writes
       that silently failed.
    3. Dataset B collector failures -- collector_runs where status=
       'FAILED' or rows_failed>0, with the real api_errors already
       captured by the collector. Same data Warehouse Command shows,
       surfaced here too so Health Center is a genuine single place to
       check, not one of several.

    Deliberately does NOT invent new exception-catching at every call
    site across an 11,000-line file -- that would be a much larger,
    riskier change. This aggregates real, already-recorded signals.
    Fail-open throughout: one source being unavailable never blocks
    the others.
    """
    issues = []

    # Source 1: Dataset A capture failures (already built this session)
    try:
        for f in d1.get_recent_capture_failures(limit):
            issues.append({
                "source": "dataset_a_write", "occurred_at": f.get("occurred_at"),
                "summary": f"{f.get('function_name')} failed",
                "detail": f.get("error_message") or f.get("error_type"),
                "eval_id": f.get("eval_id"),
            })
    except Exception:
        pass

    # Source 2: rejected/failed evaluations (real data, never surfaced before)
    try:
        rows = ops._warehouse_query(
            "SELECT eval_id, player, opponent, market, termination_reason, "
            "termination_detail, lifecycle_state, lifecycle_updated_at "
            "FROM evaluations WHERE lifecycle_state IN ('REJECTED','FAILED') "
            "ORDER BY lifecycle_updated_at DESC LIMIT ?", (limit,)
        )
        for r in rows:
            who = r.get("player") or "?"
            if r.get("opponent"):
                who += f" vs {r['opponent']}"
            issues.append({
                "source": "evaluation_" + r.get("lifecycle_state", "").lower(),
                "occurred_at": r.get("lifecycle_updated_at"),
                "summary": f"{who} · {r.get('market') or ''} · {r.get('termination_reason') or r.get('lifecycle_state')}",
                "detail": r.get("termination_detail"),
                "eval_id": r.get("eval_id"),
            })
    except Exception:
        pass

    # Source 3: Dataset B collector failures (already tracked via
    # wops.run_history(), just not surfaced anywhere outside Warehouse
    # Command until now)
    try:
        for r in (wops.run_history() or []):
            if r.get("status") == "FAILED" or (r.get("rows_failed") or 0) > 0:
                errs = r.get("api_errors")
                try:
                    import json as _json
                    errs = _json.loads(errs) if isinstance(errs, str) else errs
                except Exception:
                    pass
                issues.append({
                    "source": "warehouse_collector", "occurred_at": r.get("started_at"),
                    "summary": f"Collector run {r.get('run_id')} · {r.get('status')} · {r.get('rows_failed') or 0} rows failed",
                    "detail": str(errs)[:300] if errs else None,
                    "eval_id": None,
                })
    except Exception:
        pass

    issues.sort(key=lambda i: i.get("occurred_at") or "", reverse=True)
    return issues[:limit]


def classify_warehouse_warnings(lookback=14):
    """
    Answers the real question raised by staring at the Collector Run
    Feed: '48 warnings' every single day for weeks isn't 48 new
    problems -- it's very likely the same one or two warnings recurring
    identically, day after day (e.g. an injured player with no current-
    season matches to write, which is the collector's data-integrity
    safeguard working correctly, not a bug). Left undifferentiated,
    that noise buries anything that's genuinely NEW and actually worth
    looking at.

    Parses wops.run_history()'s real warning strings across the last
    `lookback` runs, groups identical warning text, and classifies each
    into two buckets:
      - new_today: warnings present in the MOST RECENT run that were
        NOT present in the run immediately before it. These are the
        ones worth actually reading.
      - recurring_known: warnings that have appeared in every run for
        several runs running -- very likely a permanent, already-
        understood condition (an inactive/injured player, a player the
        API can no longer resolve), not something to keep re-alerting
        on daily.

    This is pure triage on data the collector already produces -- it
    does not change what the collector writes or skips, only how the
    same information gets surfaced so a real new problem doesn't drown
    in 48 copies of an old, already-understood one.
    """
    runs = (wops.run_history() or [])[:lookback]
    if not runs:
        return {"new_today": [], "recurring_known": [], "runs_analyzed": 0}

    def _parse(w):
        if isinstance(w, str):
            try:
                import json as _json
                return _json.loads(w)
            except Exception:
                return [w] if w else []
        return w or []

    per_run_warnings = [set(_parse(r.get("warnings"))) for r in runs]
    # runs come back most-recent-first (per run_history()'s own ORDER BY)
    most_recent = per_run_warnings[0] if per_run_warnings else set()
    previous = per_run_warnings[1] if len(per_run_warnings) > 1 else set()

    new_today = sorted(most_recent - previous)

    # Recurring: appears in EVERY run analyzed, not just today's --
    # a genuinely intermittent warning (shows up some days, not others)
    # is neither "new" nor "known-recurring" in the sense that matters
    # here, so it's deliberately left out of both buckets rather than
    # forced into one.
    recurring_known = []
    if len(per_run_warnings) >= 3:
        common = set.intersection(*per_run_warnings) if per_run_warnings else set()
        recurring_known = sorted(common)

    return {
        "new_today": new_today,
        "recurring_known": recurring_known,
        "runs_analyzed": len(runs),
        "most_recent_run_date": runs[0].get("started_at") if runs else None,
    }


def _rows_from_dataset_a(evaluation_type):
    """
    Real fix (2026-07-25/26): the picks_*.json archive format that
    ops.iter_picks() was built to read is no longer being written --
    confirmed on-disk: data/picks_archive/ contains only reasoning_*.txt
    files, zero picks_*.json files. Dataset A's `evaluations` table is
    the real, actively growing source of truth (recording since
    2026-07-23) and already has every field Pick Results needs, INCLUDING
    a native eval_id -- no fuzzy matching required for these rows, unlike
    the legacy archive path below.

    Deliberately conservative filter: only rows with both a player AND a
    direction are included (a real evaluation that reached a conclusion,
    not one still stuck in RESEARCH_STARTED with nothing to show yet).
    """
    try:
        rows = ops._warehouse_query(
            "SELECT eval_id, player, market, line, direction, "
            "final_confidence, eval_confidence, final_grade, outcome, "
            "match_scheduled_time, created_at "
            "FROM evaluations WHERE evaluation_type = ? "
            "AND player IS NOT NULL AND direction IS NOT NULL "
            "ORDER BY created_at ASC",
            (evaluation_type,)
        )
    except Exception:
        return []
    out = []
    for r in rows:
        _date = (r.get("match_scheduled_time") or r.get("created_at") or "")[:10]
        _market = r.get("market") or ("ML" if evaluation_type == "MONEYLINE" else "?")
        _conf = r.get("final_confidence")
        if _conf is None:
            _conf = r.get("eval_confidence")
        p = {"player": r.get("player"), "stat_type": _market, "match_date": _date,
             "outcome": r.get("outcome")}
        out.append({
            "date": _date, "player": r.get("player", "?"), "market": _market,
            "line": r.get("line"), "direction": r.get("direction", ""),
            "confidence": _conf, "color": r.get("final_grade", ""),
            "outcome": r.get("outcome") if r.get("outcome") in ("HIT", "BUST", "PUSH", "VOID") else None,
            "okey": ops._okey(p), "eval_id": r.get("eval_id"),
        })
    return out


def pick_results_view():
    """PICK RESULTS (2026-07-23, approved; data source fixed 2026-07-26)
    -- the per-pick results table that never existed: every pick with
    its outcome (HIT/BUST/PUSH/VOID/pending).

    Primary source (2026-07-26): Dataset A's `evaluations` table directly
    via _rows_from_dataset_a() -- real, growing, has native eval_id.
    Secondary source: the legacy ops.iter_picks() JSON-archive path,
    kept as a harmless fallback in case older picks_*.json files exist
    outside what's currently on disk -- contributes 0 rows today
    (confirmed: data/picks_archive/ has zero files matching that
    pattern, only reasoning_*.txt), but costs nothing to leave in place
    rather than deleting a working code path.

    Same settlement contract as always: okey computed via ops._okey()
    from each row (regardless of source), so HIT/BUST buttons and
    set_outcome() never drift, and overrides apply identically to rows
    from either source."""
    overrides = ops._jload(ops.OUTCOMES, {})
    def _rows(kind):
        out = list(_rows_from_dataset_a("PROP" if kind == "props" else "MONEYLINE"))
        seen_okeys = {r["okey"] for r in out}
        for p in ops.iter_picks(kind):
            if kind == "ml" and not (p.get("color") == "GREEN"
                                      or p.get("direction") in ("WIN", "MONEYLINE")):
                # mirrors tracker()'s own ML definition -- matters when the
                # shared-naming fallback makes iter_picks("ml") read the
                # props archive; without this the ML table would silently
                # duplicate every prop row.
                continue
            okey = ops._okey(p)
            if okey in seen_okeys:
                continue  # Dataset A already has this one -- prefer it, it has eval_id
            o = ops.outcome_of(p, overrides)
            _market = p.get("stat_type") or p.get("market") or "ML"
            out.append({
                "date": p.get("match_date") or p.get("_date", ""),
                "player": p.get("player", "?"),
                "market": _market,
                "line": p.get("line"),
                "direction": p.get("direction", ""),
                "confidence": p.get("confidence"),
                "color": p.get("color", ""),
                "outcome": o if o in ("HIT", "BUST", "PUSH", "VOID") else None,
                "okey": okey,
                "eval_id": _find_eval_id_for_pick(p.get("player", "?"), _market, p.get("line")),
            })
        # Re-apply outcome overrides to the Dataset A rows too -- they
        # skip ops.outcome_of() above since they're built directly, but
        # a manual HIT/BUST override must still win the same way it
        # always has for every other row.
        for r in out:
            manual = overrides.get(r["okey"])
            if manual and not r["outcome"]:
                r["outcome"] = manual
        return out
    props, ml = _rows("props"), _rows("ml")
    def _agg(rs):
        settled = [r for r in rs if r["outcome"] in ("HIT", "BUST")]
        hits = sum(1 for r in settled if r["outcome"] == "HIT")
        return {"total": len(rs), "settled": len(settled), "hits": hits,
                "busts": len(settled) - hits,
                "pending": sum(1 for r in rs if not r["outcome"])}
    return {"props": props, "moneyline": ml,
            "props_summary": _agg(props), "ml_summary": _agg(ml)}


def cost_intelligence_view():
    """
    Cost Intelligence Center backend — read-only, ate.db (ai_traces)
    only. Returns None if ATE isn't available, so the route can send
    a clean, honest empty state rather than an error.
    Enriches top_expensive and top_slowest with player/opponent names
    from Dataset A so Notable Evaluations shows names, not raw UUIDs.
    """
    if not _ATE_AVAILABLE:
        return None
    try:
        bundle = ate_q.cost_intelligence_bundle()
        if not bundle:
            return None

        # Join player names onto notable evaluation rows — best-effort,
        # fail-open (a name lookup failure never breaks cost display).
        # Covers both the top_expensive/top_slowest LISTS and the four
        # singular evaluation_metrics fields (highest_cost, lowest_cost,
        # slowest, fastest) -- (2026-07-26) previously only the lists
        # were enriched, so Highest/Lowest/Slowest/Fastest always showed
        # a raw eval_id with no fallback even when a name was available.
        try:
            from pathlib import Path as _Path
            import sqlite3 as _sq3
            _da = _Path(__file__).resolve().parent / "data" / "warehouse" / "dataset_a.sqlite"
            if _da.exists():
                _cn = _sq3.connect(str(_da), timeout=3)
                _cn.row_factory = _sq3.Row
                _ids = set()
                for _lst in ("top_expensive", "top_slowest"):
                    for _r in (bundle.get(_lst) or []):
                        if _r.get("eval_id"):
                            _ids.add(_r["eval_id"])
                _em = bundle.get("evaluation_metrics") or {}
                for _key in ("highest_cost", "lowest_cost", "slowest", "fastest"):
                    _r = _em.get(_key)
                    if _r and _r.get("eval_id"):
                        _ids.add(_r["eval_id"])
                if _ids:
                    _ph = ",".join("?" * len(_ids))
                    _rows = _cn.execute(
                        f"SELECT eval_id, player, opponent, market, line "
                        f"FROM evaluations WHERE eval_id IN ({_ph})",
                        list(_ids)
                    ).fetchall()
                    _nm = {_r["eval_id"]: dict(_r) for _r in _rows}
                    for _lst in ("top_expensive", "top_slowest"):
                        for _r in (bundle.get(_lst) or []):
                            _info = _nm.get(_r.get("eval_id"), {})
                            _r["player"]   = _info.get("player", "")
                            _r["opponent"] = _info.get("opponent", "")
                            _r["market"]   = _info.get("market", "")
                            _r["line"]     = _info.get("line")
                    for _key in ("highest_cost", "lowest_cost", "slowest", "fastest"):
                        _r = _em.get(_key)
                        if _r and _r.get("eval_id"):
                            _info = _nm.get(_r["eval_id"], {})
                            _r["player"]   = _info.get("player", "")
                            _r["opponent"] = _info.get("opponent", "")
                            _r["market"]   = _info.get("market", "")
                            _r["line"]     = _info.get("line")
                _cn.close()
        except Exception:
            pass  # name enrichment is display-only — never block cost data

        return bundle
    except Exception:
        return None

def command_chat_turn(session_id, message, eval_id_context=None, model_key=None, profile_key=None):
    """
    Command Chat backend — one user turn. eval_id_context, when
    provided (the inspector-panel entry point), gets prepended so
    the model knows which evaluation the operator is already
    looking at without the operator having to repeat the eval_id.
    Read-only, per Command Chat's hard rule — see command_chat_engine.py.

    model_key / profile_key: resolved fresh per call from the
    frontend's current selector state, never stored against the
    session — this is what makes live switching work without a new
    chat (see command_chat_engine.run_turn's docstring).
    """
    if not _CHAT_AVAILABLE:
        return {"answer": None, "error": "Command Chat engine not available"}
    full_message = (f"[Context: currently viewing evaluation {eval_id_context}] {message}"
                     if eval_id_context else message)
    return chat_engine.run_turn(session_id, full_message, chat_engine.call_claude_with_tools,
                                 model_key=model_key, profile_key=profile_key)

def memory_graph_view():
    """Memory Center Phase 4 — everything Cytoscape.js needs, one call.
    Read-only, sourced directly from graph_edges + decision_records +
    investigation_records via command_chat_writer.get_graph_data()."""
    if not _CHAT_AVAILABLE:
        return {"nodes": [], "edges": []}
    try:
        return chat_writer.get_graph_data()
    except Exception:
        return {"nodes": [], "edges": []}

def memory_related_view(node_type, node_id):
    """Related panel query — direct neighbors of one node, both
    directions. The search-first design's primary relationship
    surface (see the finalized design doc) — most lookups should
    never need the full graph."""
    if not _CHAT_AVAILABLE or not node_type or not node_id:
        return {"outgoing": [], "incoming": []}
    try:
        return chat_writer.get_related(node_type, node_id)
    except Exception:
        return {"outgoing": [], "incoming": []}

def memory_search_view(query):
    """Search-first entry point for Memory Center's left panel —
    reuses the exact same FTS5 functions Command Chat's tool layer
    already calls, tagged with type/id so results can route straight
    into the detail+Related panel on click."""
    if not _CHAT_AVAILABLE or not query:
        return []
    try:
        decisions = chat_writer.search_decisions(query)
        investigations = chat_writer.search_investigations(query)
    except Exception:
        return []
    results = [{"type": "decision", "id": d["decision_id"], "topic": d["topic"]} for d in decisions]
    results += [{"type": "investigation", "id": i["investigation_id"], "topic": i["topic"]} for i in investigations]
    return results

def memory_record_view(node_type, node_id):
    """Full record fields for the detail panel — direct row lookup,
    not a search. system/rule nodes have no backing table row, so
    they return a minimal synthetic record instead of erroring."""
    if not _CHAT_AVAILABLE or not node_type or not node_id:
        return {"error": "not found"}
    if node_type in ("decision", "investigation"):
        try:
            import sqlite3
            import command_chat_db as ccdb
            conn = sqlite3.connect(ccdb.COMMAND_CHAT_DB_PATH)
            conn.row_factory = sqlite3.Row
            table = "decision_records" if node_type == "decision" else "investigation_records"
            id_col = "decision_id" if node_type == "decision" else "investigation_id"
            row = conn.execute(f"SELECT * FROM {table} WHERE {id_col} = ?", (node_id,)).fetchone()
            conn.close()
            return dict(row) if row else {"error": "not found"}
        except Exception:
            return {"error": "not found"}
    # rule/system — no table row, the node's own label is the whole record
    return {"type": node_type, "id": node_id, "label": node_id}

def command_chat_options():
    """Model/profile option lists for the frontend selectors — sourced
    directly from command_chat_engine's own dicts, so the dropdown
    options can never drift out of sync with what the backend actually
    supports."""
    if not _CHAT_AVAILABLE:
        return {"models": [], "profiles": [], "default_model": None, "default_profile": None}
    return {
        "models": [{"key": k, "label": v} for k, v in chat_engine.MODEL_LABELS.items()],
        "profiles": [{"key": k, "label": v} for k, v in chat_engine.PROFILE_LABELS.items()],
        "default_model": chat_engine.DEFAULT_MODEL_KEY,
        "default_profile": chat_engine.DEFAULT_PROFILE_KEY,
    }

def command_chat_sessions():
    """Session history browser backend — read-only, per the UX V1.1
    authorization. No new table, no new logic beyond command_chat_writer's
    plain SELECT-based list_sessions()."""
    if not _CHAT_AVAILABLE:
        return []
    try:
        return chat_writer.list_sessions()
    except Exception:
        return []

def command_chat_session_detail(session_id):
    if not _CHAT_AVAILABLE or not session_id:
        return []
    try:
        return chat_writer.get_session_messages(session_id)
    except Exception:
        return []

def canvas_state():
    lanes = {r["name"]: r for r in operations()}
    cols = {s: [] for s in ops.STAGES}; seen = set()
    for p in todays_picks():
        name = p.get("player", "?"); r = lanes.get(name)
        stage, state = "Post", "done"
        if r:
            for s in ops.STAGES:
                if r["lane"][s] == "🔴": stage, state = s, "failed"; break
                if r["lane"][s] == "🟡": stage, state = s, "running"; break
        cols[stage].append({"key": ops._okey(p), "player": name, "line": p.get("line"),
                            "stat": p.get("stat_type"), "dir": p.get("direction"),
                            "conf": p.get("confidence"), "color": p.get("color"),
                            "state": state, "stalled": bool(r and r["stalled"]),
                            "retries": r["retries"] if r else 0})
        seen.add(name)
    for name, r in lanes.items():
        if name in seen: continue
        stage, state = "Research", "running"
        for s in ops.STAGES:
            if r["lane"][s] == "🔴": stage, state = s, "failed"; break
            if r["lane"][s] == "🟡": stage, state = s, "running"; break
            if r["lane"][s] == "🟢": stage = s
        cols[stage].append({"key": "", "player": name, "line": "", "stat": "", "dir": "",
                            "conf": None, "color": "", "state": state,
                            "stalled": r["stalled"], "retries": r["retries"]})
    return cols

DAILY_METRICS_FILE = ops.OPS_DIR / "daily_metrics_history.json"
def snapshot_daily_metrics():
    """Writes one real row per calendar day (CT, same convention as
    ops.today_str()) recording the exact numbers Command Deck's
    executive strip already shows. Self-triggering -- called once from
    state() on whatever page load happens to be the first of the day,
    so no new LaunchAgent or cron entry is required. No-op if today's
    row already exists, so it's safe to call on every single request.
    Every value here reuses a function this file already calls
    elsewhere (memory_graph_view, command_chat_sessions, the same
    ate_q.cost_intelligence_bundle() Deck/Cost Intelligence use) --
    nothing new is computed, this only remembers what already exists.
    """
    hist = ops._jload(DAILY_METRICS_FILE, [])
    today = ops.today_str()
    if hist and hist[-1].get("date") == today:
        return  # already snapped today
    try:
        graph = memory_graph_view()
        decisions = sum(1 for n in graph.get("nodes", []) if n.get("type") == "decision")
        investigations = sum(1 for n in graph.get("nodes", []) if n.get("type") == "investigation")
        memory_nodes = len(graph.get("nodes", []))
    except Exception:
        decisions = investigations = memory_nodes = None
    try:
        sessions = len(command_chat_sessions())
    except Exception:
        sessions = None
    try:
        cost = ate_q.cost_intelligence_bundle() if _ATE_AVAILABLE else None
        evaluations = cost["evaluation_metrics"]["evaluations_traced"] if cost else None
        cost_today = cost["summary"]["today"] if cost else None
    except Exception:
        evaluations = cost_today = None
    try:
        pending_proposals = ops.learning_panel().get("pending_proposals")
    except Exception:
        pending_proposals = None
    hist.append({"date": today, "evaluations": evaluations, "decisions": decisions,
                 "investigations": investigations, "memory_nodes": memory_nodes,
                 "chat_sessions": sessions, "cost_today": cost_today,
                 "pending_proposals": pending_proposals})
    hist = hist[-90:]  # bounded history, no unbounded growth
    try:
        DAILY_METRICS_FILE.write_text(json.dumps(hist, indent=2))
    except Exception:
        pass  # snapshot failures never break the dashboard itself

def metric_history(days=14):
    return ops._jload(DAILY_METRICS_FILE, [])[-days:]

# ---------------------------------------------------------------------------
# APEX-HEALTHCHECK-MC-WIRE-2026-08-23 -- surfaces the daily healthcheck report.
# Reads apex_healthcheck_report.json (written atomically by apex_healthcheck.py).
# Runs nothing, changes nothing. Fail-open: any problem returns UNKNOWN and
# Mission Control renders exactly as it did before.
#
# Staleness is deliberately loud. A healthcheck that stopped running must never
# look like a healthy system -- that failure mode is exactly why this exists
# (bot silently dead Jul 23 -> Aug 21, nothing alerted).
# ---------------------------------------------------------------------------
_APEX_HC_REPORT = "apex_healthcheck_report.json"
_APEX_HC_STALE_HOURS = 30      # daily job + 6h grace


def _apex_healthcheck_view():
    """APEX-HEALTHCHECK-MC-WIRE-2026-08-23: the daily report, or a loud reason."""
    try:
        import json as _json, os as _os, time as _time
        path = _APEX_HC_REPORT
        if not _os.path.isabs(path):
            path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), path)
        if not _os.path.exists(path):
            return {"status": "NEVER_RUN", "overall": "UNKNOWN", "checks": [],
                    "headline": "Healthcheck has never run — no report file",
                    "age_hours": None, "stale": True}
        age_h = (_time.time() - _os.path.getmtime(path)) / 3600.0
        with open(path) as f:
            rep = _json.load(f)
        stale = age_h > _APEX_HC_STALE_HOURS
        checks = rep.get("checks", [])
        fails = rep.get("failures", [])
        warns = rep.get("warnings", [])
        if stale:
            headline = ("Healthcheck has not run in %.0f hours — treat every panel "
                        "below as unverified" % age_h)
            status = "STALE"
        elif fails:
            headline = "%d check(s) failing: %s" % (len(fails), ", ".join(fails))
            status = "FAIL"
        elif warns:
            headline = "%d warning(s): %s" % (len(warns), ", ".join(warns))
            status = "WARN"
        else:
            headline = "All %d checks passing" % len(checks)
            status = "OK"
        return {"status": status,
                "overall": rep.get("overall", "UNKNOWN"),
                "generated_at": rep.get("generated_at"),
                "age_hours": round(age_h, 1),
                "stale": stale,
                "headline": headline,
                "failures": fails,
                "warnings": warns,
                "checks": checks,
                "text": rep.get("text", "")}
    except Exception as e:
        return {"status": "UNKNOWN", "overall": "UNKNOWN", "checks": [], "stale": True,
                "headline": "Healthcheck report unreadable: %s: %s" % (type(e).__name__, e),
                "age_hours": None}


def state():
    snapshot_daily_metrics()
    cfg = ops.load_config()
    g, m = tracker("green", cfg), tracker("ml", cfg)
    s = ops.slips(cfg); hs, score = ops.health()
    hist = ops._jload(ops.VALID_DIR / "run_history.json", [])
    return {"apex_healthcheck": _apex_healthcheck_view(),   # APEX-HEALTHCHECK-MC-WIRE-2026-08-23
            "cfg": cfg, "green": g, "ml": m, "slips": s, "health": hs,
            "health_score": score, "feed": ops.live_feed(ops.read_log_tail(), 14),
            "approvals": ops.pending_proposals(), "canvas": canvas_state(),
            "ops": operations(), "validation_last": hist[-1] if hist else None,
            "intake": intake.queue_view(),
            "ml_intake": ops.ml_intake_queue(),
            "agents": ops.agent_status(),
            "live_flow": ops.live_flow(),
            "rule_overlay": ops.rule_overlay_for(),
            "learning_panel": ops.learning_panel(),
            "validation_panel": ops.validation_panel(),
            "now": ops.today_str()}

# ── HTTP ────────────────────────────────────────────────────────────
# ================================================================
# ACCESS CONTROL (this session, closes a real gap found during a
# full-system audit): Mission Control had ZERO authentication of any
# kind -- confirmed by reading every route in do_GET/do_POST, none of
# them checked anything before dispatching. Bound to 0.0.0.0 (every
# network interface, not just localhost). This was tolerable when the
# dashboard only showed picks and confidence scores; it now shows real
# financial data (stake, potential_payout, slip details from this
# session's Slip Review work) and is a materially different risk.
#
# HTTP Basic Auth, deliberately -- not a custom login page/session/
# cookie system. This is a single-operator tool, not a multi-user app:
# Basic Auth needs zero new code for session handling, works natively
# with every browser's built-in prompt (asked once, cached for the
# browser session), and works transparently for the page's own fetch()
# calls once the browser has the credentials cached -- no client-side
# JS changes needed anywhere else in this file.
#
# Credentials come from environment variables, never hardcoded here --
# set MC_AUTH_USER / MC_AUTH_PASS in the LaunchAgent plist's
# EnvironmentVariables dict before restarting. If either is unset,
# the server refuses every request with a clear message instead of
# silently falling back to no-auth -- silently reopening the exact gap
# this fix exists to close would be worse than a loud failure that
# tells you exactly what to do.
MC_AUTH_USER = os.environ.get("MC_AUTH_USER")
MC_AUTH_PASS = os.environ.get("MC_AUTH_PASS")

# Turned OFF (2026-07-21) at the operator's explicit request -- getting
# the LaunchAgent plist update to actually land on the Mac Mini turned
# into more friction than it was worth in the moment. Mission Control
# now behaves exactly as it did before this session's auth work: no
# login, open to anything that can reach the port.
# RE-ENABLING REQUIRES TWO EDITS, NOT ONE (this comment originally said
# only to flip this flag -- that became wrong when a second, stronger
# kill switch was added later the same night at the operator's request):
#   1. Delete the unconditional `return True  # AUTH DISABLED BY USER
#      REQUEST` line at the very top of _require_auth() below.
#   2. Set this flag to True.
# Then set MC_AUTH_USER / MC_AUTH_PASS in the LaunchAgent plist's
# EnvironmentVariables and restart. The rest of the implementation
# (_check_auth, credential comparison, the 401 page) is untouched and
# ready -- nothing needs to be rebuilt, but BOTH edits above are
# required or auth stays off.
MC_AUTH_ENABLED = True   # SLIP-AUTOMATION-2026-09-27 (safety pass, priority 3 of 4):
# re-enabled ahead of screenshot auto-confirm. NOT ACTIVE until MC_AUTH_USER
# / MC_AUTH_PASS are set in the LIVE LaunchAgent plist -- Desktop Commander
# is blocked by policy from writing plaintext credentials into a system
# file, so that one step is left for the operator (see
# SLIP_PNL_AUTOMATION_INVESTIGATION.md for the exact plist path and keys).
# Until that's done, _check_auth() returns False for every request and
# _require_auth() sends the clear "not configured" 401 above -- fails
# loud and locked, never silently open. Do not restart this launchd job
# until the credentials are in place, or the dashboard will 401 on
# everything until they are.


def _check_auth(handler) -> bool:
    if not MC_AUTH_USER or not MC_AUTH_PASS:
        return False
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        user, _, pw = decoded.partition(":")
    except Exception:
        return False
    # hmac.compare_digest -- timing-safe comparison, not `==`. A plain
    # string comparison leaks how many leading characters matched via
    # response timing; small enough to not matter for most tools, but
    # free to get right and this guards real financial data now.
    return hmac.compare_digest(user, MC_AUTH_USER) and hmac.compare_digest(pw, MC_AUTH_PASS)


def _require_auth(handler) -> bool:
    """Call at the top of do_GET/do_POST. Returns True if the request
    should proceed; sends 401 and returns False otherwise."""
    if not MC_AUTH_ENABLED:
        return True
    if _check_auth(handler):
        return True
    body = (b"Authentication required."
            if MC_AUTH_USER and MC_AUTH_PASS else
            b"MC_AUTH_USER / MC_AUTH_PASS are not set on this process. "
            b"Set both in the LaunchAgent plist's EnvironmentVariables and "
            b"restart -- Mission Control refuses all requests until then, "
            b"by design, rather than silently running with no authentication.")
    try:
        handler.send_response(401)
        handler.send_header("WWW-Authenticate", 'Basic realm="OpenClaw Mission Control"')
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        # The client (browser tab refreshed/navigated away/closed) hung
        # up before this finished writing -- harmless and common,
        # nothing to do about it server-side. Without this, it printed
        # a full traceback to the log on every occurrence even though
        # nothing was actually wrong; same log-noise fix already
        # applied to send_discord() and every other network call in
        # this codebase.
        pass
    return False


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if not _require_auth(self): return
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/": self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif u.path == "/api/state": self._send(200, state())
        elif u.path == "/api/apex_healthcheck":  # APEX-HEALTHCHECK-MC-WIRE-2026-08-23
            self._send(200, _apex_healthcheck_view())
        elif u.path == "/api/pick": self._send(200, pick_detail(q.get("key", [""])[0]))
        elif u.path == "/api/validation": self._send(200, validation_view())
        elif u.path == "/api/validation_workspace": self._send(200, ops.validation_workspace())
        elif u.path == "/api/health_center": self._send(200, {
            **ops.health_center(),
            "system_issues": system_issues_view(30)})
        elif u.path == "/api/ml_readiness": self._send(200, {
            **ops.ml_readiness_center(),
            "dataset_b": wops.warehouse_snapshot()})
        elif u.path == "/api/warehouse_cc": self._send(200, {
            "snapshot": wops.warehouse_snapshot(),
            "readiness": wops.readiness_card(),
            "collection": wops.collection_status(),
            "source_quality": wops.data_source_quality(),
            "feature_health": wops.feature_health(),
            "quality": wops.data_quality_center(),
            "year_coverage": wops.year_coverage(),
            "match_coverage": wops.match_coverage(),
            "trends": wops.collection_trends(),
            "health_score": wops.warehouse_health_score(),
            "elo": eo.elo_coverage_stats()})
        elif u.path == "/api/warehouse_cc_runs": self._send(200, wops.run_history())
        elif u.path == "/api/rule_registry_status":
            # 2026-08-09: reads back the last saved run rather than
            # re-executing anything — a full run takes real time
            # (10 backtests against 500K+ row tables), so this is a
            # fast read of whatever rule_registry.py's own
            # save_results() last wrote, or a real "never run" state.
            _rr_last = rule_registry.load_last_results()
            self._send(200, {"last_run": _rr_last})
        elif u.path == "/api/warehouse_warnings_triage": self._send(200, classify_warehouse_warnings())
        elif u.path == "/api/warehouse_cc_run_detail":
            self._send(200, wops.run_detail(q.get("id", [""])[0]))
        elif u.path == "/api/warehouse_cc_players": self._send(200, wops.player_coverage())
        elif u.path == "/api/warehouse_cc_player_detail":
            self._send(200, wops.player_detail(q.get("name", [""])[0]))
        elif u.path == "/api/warehouse_overview": self._send(200, {
            "sufficiency": ops.data_sufficiency(),
            "snapshot": ops.knowledge_snapshot(),
            "markets": ops.market_coverage_full(),
            "surfaces": ops.surface_coverage(),
            "tours": ops.tour_coverage(),
            "time": ops.time_distribution(),
            "outcomes": ops.outcome_coverage_full(),
            "problems": ops.top_data_problems(),
            "quality": ops.data_quality_center(),
            "forecast": ops.ml_training_forecast()})
        elif u.path == "/api/warehouse_players": self._send(200, ops.player_coverage())
        elif u.path == "/api/metric_history": self._send(200, metric_history(int(q.get("days",["14"])[0])))
        elif u.path == "/api/warehouse_player_detail":
            self._send(200, ops.player_detail(q.get("name", [""])[0]))
        elif u.path == "/api/warehouse_coverage_audit": self._send(200, ops.dataset_a_coverage_audit())
        elif u.path == "/api/warehouse_rule_audit": self._send(200, ops.rule_audit_infrastructure())
        elif u.path == "/api/roadmap": self._send(200, roadmap_view())
        elif u.path == "/api/bankroll": self._send(200, {
            **bankroll_view(ops.load_config()),
            "auto_roi": ops.slip_roi_report(),
            "manual_auto_overlaps": ops.manual_auto_overlap_report()})
        elif u.path == "/api/agents": self._send(200, ops.agent_status())
        elif u.path == "/api/confidence": self._send(200, {
            "aggregate": ops.confidence_audit_aggregate(),
            "recent": ops.recent_scored(),
            "calibration": ops.confidence_calibration_report()})
        elif u.path == "/api/pick_eval":
            _eval_id = q.get("id", [""])[0]
            self._send(200, {
                **_with_ate(ops.pick_from_eval(_eval_id), eval_id=_eval_id),
                "shadow": eco.get_pick_shadow_data(_eval_id)})
        elif u.path == "/api/rulecc": self._send(200, ops.rule_command_center())
        elif u.path == "/api/cost_cc": self._send(200, cost_intelligence_view())
        elif u.path == "/api/pick_results": self._send(200, pick_results_view())
        elif u.path == "/api/calibration": self._send(200, {
            **ops.calibration_center(),
            "estimator_calibration": eco.estimator_calibration_summary()})
        elif u.path == "/api/slip_review": self._send(200, intake.slip_review_queue_view())
        elif u.path == "/api/command_chat_options": self._send(200, command_chat_options())
        elif u.path == "/api/memory_graph": self._send(200, memory_graph_view())
        elif u.path == "/api/memory_related":
            self._send(200, memory_related_view(q.get("type", [""])[0], q.get("id", [""])[0]))
        elif u.path == "/api/memory_record":
            self._send(200, memory_record_view(q.get("type", [""])[0], q.get("id", [""])[0]))
        elif u.path == "/api/memory_search":
            self._send(200, memory_search_view(q.get("q", [""])[0]))
        elif u.path == "/api/command_chat_sessions": self._send(200, command_chat_sessions())
        elif u.path == "/api/command_chat_session_detail":
            self._send(200, command_chat_session_detail(q.get("session_id", [""])[0]))
        elif u.path == "/api/learning": self._send(200, {
            "panel": ops.learning_panel(),
            "proposals": ops.proposal_history(),
            "approvals": ops.approval_log(),
            "signals": ops.signal_trend()})
        else: self._send(404, {"error": "not found"})
    def do_POST(self):
        if not _require_auth(self): return
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/api/slip":
                ops.add_slip(ops.today_str(), float(body["stake"]), int(body["legs"]),
                             body["result"].upper(), float(body["payout"]))
            elif self.path == "/api/warehouse_run_now":
                # Run the forward collector immediately in the background.
                # Returns immediately — check Warehouse Command for results.
                try:
                    HERE = Path(__file__).resolve().parent
                    collector = str(HERE / "tennis_warehouse_collector.py")
                    subprocess.Popen(
                        [sys.executable, collector, "--forward"],
                        cwd=str(HERE),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return self._send(200, {"ok": True, "message": "Collector started — check Warehouse Command in ~30s for updated stats."})
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
            elif self.path == "/api/rule_registry_run_now":
                # 2026-08-09: real "Run Now" for the rule audit — same
                # exact background-subprocess pattern as the collector
                # above. Deliberately NOT scheduled automatically (see
                # the real Discord "Scheduler active" list) -- manual-
                # trigger only unless/until the user asks for a real
                # cadence. Real backtests against 500K+ row tables can
                # take real meaningful time combined, hence background +
                # separate status read rather than a blocking response.
                try:
                    HERE = Path(__file__).resolve().parent
                    registry_script = str(HERE / "rule_registry.py")
                    subprocess.Popen(
                        [sys.executable, registry_script],
                        cwd=str(HERE),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return self._send(200, {"ok": True, "message": "Rule audit started — check back in ~1-2 min for updated results."})
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
            elif self.path == "/api/clear_stalls":
                # Rotate the discord bot log and restart the bot so the
                # Operations canvas reads a fresh tail with no ghost stalls.
                # This is exactly what the manual Terminal sequence does:
                #   mv discord_bot.log discord_bot.log.old
                #   touch discord_bot.log
                #   launchctl bootout + bootstrap discordbot
                try:
                    import subprocess as _sp, time as _t, os as _os
                    _here = Path(__file__).resolve().parent
                    _log  = _here / "data" / "logs" / "discord_bot.log"
                    _old  = _here / "data" / "logs" / "discord_bot.log.old"
                    _plist = Path.home() / "Library" / "LaunchAgents" / "com.apex.discordbot.plist"
                    # Rotate log
                    if _log.exists():
                        _log.rename(_old)
                    _log.touch()
                    # Restart bot via launchctl
                    _uid = str(_os.getuid())
                    _sp.run(["launchctl","bootout", f"gui/{_uid}", str(_plist)],
                            capture_output=True)
                    _t.sleep(1)
                    _sp.run(["launchctl","bootstrap", f"gui/{_uid}", str(_plist)],
                            capture_output=True)
                    return self._send(200, {"ok": True, "message": "Log rotated and bot restarted — stalls will clear on next refresh."})
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
            elif self.path == "/api/bankroll_set":
                # Edit the starting bankroll amount
                try:
                    cfg = ops.load_config()
                    new_amount = float(body.get("amount", cfg.get("starting_bankroll", 500)))
                    cfg["starting_bankroll"] = new_amount
                    ops.save_config(cfg)
                    return self._send(200, {"ok": True, "amount": new_amount})
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
            elif self.path == "/api/outcome":
                ops.set_outcome(body["key"], body["outcome"].upper())
            elif self.path == "/api/intake/batch_text":
                # Batch API path (v1.1): same as /api/intake/text but
                # batch_eligible=True. Research runs synchronously; the
                # Sonnet evaluation call is deferred to Anthropic's
                # /v1/messages/batches (50% off). Result posts in ~2-8h.
                iid = intake.enqueue("text",
                                     {"text": body["text"].strip()},
                                     batch_eligible=True)
                return self._send(200, {"ok": True, "id": iid})
            elif self.path == "/api/intake/batch_image":
                # Batch API path (v1.1): same as /api/intake/image but
                # batch_eligible=True.
                # Real bug fix (2026-08-04): this never stripped the
                # data-URL prefix ("data:image/png;base64,...") the
                # browser sends, unlike /api/intake/image right below
                # it. Every batch prop screenshot got that literal
                # prefix baked into the stored b64 -- not valid base64
                # on its own, so the real vision call failed 100% of
                # the time with "invalid base64 data", deterministically,
                # regardless of the separate intake-queue race condition
                # fixed earlier this session. Confirmed against two of
                # the user's own real screenshots that still failed
                # after that first fix deployed live.
                b64 = body.get("b64", "").split(",")[-1]
                iid = intake.enqueue(
                    "image",
                    {"b64": b64,
                     "media_type": body.get("media_type", "image/png")},
                    batch_eligible=True)
                return self._send(200, {"ok": True, "id": iid})
            elif self.path == "/api/intake/batch_ml_image":
                # Real gap fix (2026-08-04): the frontend's "Moneyline
                # Screenshot -> queue for batch" button has been
                # calling this exact path since it was built, but no
                # server-side handler for it ever existed -- found
                # while tracing the batch_image bug above. ml_image
                # batch support genuinely exists on the processing
                # side (openclaw_intake.py's _process_batch_submit()
                # already branches on kind=="ml_image" via
                # prepare_batch_ml_prop()) -- this was purely a missing
                # front door, not a missing capability.
                b64 = body.get("b64", "").split(",")[-1]
                iid = intake.enqueue(
                    "ml_image",
                    {"b64": b64,
                     "media_type": body.get("media_type", "image/png")},
                    batch_eligible=True)
                return self._send(200, {"ok": True, "id": iid})
            elif self.path == "/api/intake/text":
                iid = intake.enqueue("text", {"text": body["text"].strip()})
                return self._send(200, {"ok": True, "id": iid})
            elif self.path == "/api/intake/image":
                b64 = body["b64"].split(",")[-1]        # strip data-URL prefix
                iid = intake.enqueue("image", {"b64": b64,
                                     "media_type": body.get("media_type", "image/png")})
                return self._send(200, {"ok": True, "id": iid})
            elif self.path == "/api/intake/moneyline_image":
                b64 = body["b64"].split(",")[-1]        # strip data-URL prefix
                iid = intake.enqueue("ml_image", {"b64": b64,
                                     "media_type": body.get("media_type", "image/png")})
                return self._send(200, {"ok": True, "id": iid})
            elif self.path == "/api/intake/retry":
                return self._send(200, intake.retry(int(body["id"])))
            elif self.path == "/api/intake/moneyline":
                mid = ops.submit_moneyline(body["player_a"].strip(), body["player_b"].strip(),
                                            body.get("surface", "hard").strip() or "hard",
                                            body.get("tour", "atp").strip() or "atp")
                return self._send(200, {"ok": True, "id": mid})
            elif self.path == "/api/slip_review_confirm":
                result = intake.confirm_slip_review(
                    int(body["id"]), legs=body.get("legs"),
                    stake=body.get("stake"), potential_payout=body.get("potential_payout"))
                return self._send(200, result)
            elif self.path == "/api/slip_review_reject":
                return self._send(200, intake.reject_slip_review(int(body["id"])))
            elif self.path == "/api/command_chat":
                result = command_chat_turn(body.get("session_id", "default"),
                                            body.get("message", ""),
                                            body.get("eval_id_context"),
                                            model_key=body.get("model_key"),
                                            profile_key=body.get("profile_key"))
                return self._send(200, result)
            elif self.path == "/api/decide":
                r = ops.decide_proposal(int(body["id"]), approve=bool(body["approve"]))
                return self._send(200, {"ok": r.returncode == 0, "out": r.stdout[-400:]})
            else: return self._send(404, {"error": "not found"})
            self._send(200, {"ok": True})
        except Exception as e:
            self._send(400, {"ok": False, "error": str(e)})

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>OpenClaw Mission Control</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0c11;--panel:#12141c;--card:#191b25;--edge:#242734;--txt:#e9eaf0;
--dim:#9094a3;--dim2:#5d6170;--grn:#3ecf8e;--yel:#e2b93b;--red:#f0556d;--acc:#5b64f2;
--acc-soft:rgba(91,100,242,.14);--hair:rgba(255,255,255,.06);
--ease:cubic-bezier(.2,.7,.3,1);--t-fast:120ms;--t-base:180ms;--t-slow:260ms;--radius:14px}
@media (prefers-reduced-motion: reduce){
*,*::before,*::after{animation-duration:.001ms!important;transition-duration:.001ms!important}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:13px/1.45 -apple-system,'SF Mono',Menlo,monospace}
#app{display:grid;grid-template-columns:216px 1fr;min-height:100vh}
#side{background:#0d0e15;border-right:1px solid var(--edge);padding:18px 12px;
position:sticky;top:0;height:100vh;overflow-y:auto;font-family:-apple-system,BlinkMacSystemFont,sans-serif}
#side h1{display:flex;align-items:center;gap:10px;margin:0 4px 18px}
#side h1 .mark{width:32px;height:32px;border-radius:9px;flex-shrink:0;
background:linear-gradient(150deg,var(--acc),#3a3fb5);display:flex;align-items:center;justify-content:center;
color:#fff;font-size:15px}
#side h1 .bt{display:flex;flex-direction:column;gap:1px}
#side h1 .bt b{font-size:12.5px;font-weight:700;letter-spacing:.03em;color:var(--txt)}
#side h1 .bt i{font-style:normal;font-size:8.5px;letter-spacing:.14em;color:var(--dim2)}
#side a{display:flex;align-items:center;gap:10px;color:var(--dim);text-decoration:none;font-size:12.5px;
padding:8px 10px;border-radius:9px;margin:1px 0;cursor:pointer;
transition:background var(--t-fast) var(--ease),color var(--t-fast) var(--ease)}
#side a .ic{width:15px;text-align:center;font-size:11.5px;opacity:.85;flex-shrink:0}
#side a:hover{background:#171925;color:var(--txt)}
#side a.on{background:var(--acc);color:#fff}
#side a.on .ic{color:#fff;opacity:1}
.sidebar-group-header{color:var(--dim2);font-size:9.5px;letter-spacing:.12em;
margin:16px 0 5px 10px;font-weight:600}
.sideprofile{display:flex;align-items:center;gap:10px;background:var(--panel);border:1px solid var(--edge);
border-radius:12px;padding:10px;margin-top:18px}
.sideprofile .pav{width:32px;height:32px;border-radius:50%;flex-shrink:0;
background:linear-gradient(150deg,var(--acc),#3a3fb5);display:flex;align-items:center;justify-content:center;
color:#fff;font-size:13px;font-weight:600}
.sideprofile .pt{display:flex;flex-direction:column;line-height:1.3}
.sideprofile .pt b{font-size:12px;color:var(--txt)}
.sideprofile .pt i{font-style:normal;font-size:10px;color:var(--dim2)}
.sideprofile .pt em{font-style:normal;font-size:10px;color:var(--grn)}
.sideicons{display:flex;gap:8px;margin-top:10px}
.sideicons span{width:38px;height:34px;border:1px solid var(--edge);border-radius:10px;
display:flex;align-items:center;justify-content:center;color:var(--dim);font-size:13px}
.sidebar-group-header:first-of-type{margin-top:10px}
#main{padding:24px 28px;max-width:1400px}
#main>div{animation:mcFadeIn var(--t-base) var(--ease)}
@keyframes mcFadeIn{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}
.mc-thinking-dots{display:inline-flex;gap:3px}
.mc-thinking-dots span{width:4px;height:4px;border-radius:50%;background:var(--dim);
display:inline-block;animation:mcPulse 1.1s var(--ease) infinite}
.mc-thinking-dots span:nth-child(2){animation-delay:.15s}
.mc-thinking-dots span:nth-child(3){animation-delay:.3s}
@keyframes mcPulse{0%,60%,100%{opacity:.3;transform:scale(.85)}30%{opacity:1;transform:scale(1)}}
.strip{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px}

/* ===== V6 COMPOSITION CLASSES — Command Deck, Command Chat, Memory Center, Cost Intelligence.
   These screens no longer use .tile/.panel/.strip; every other screen still does. ===== */
.v6{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;max-width:900px;padding:8px 8px 60px}
.v6 .status{display:flex;justify-content:space-between;color:var(--dim);font-size:12px;margin-bottom:48px}
.v6 .hero{margin-bottom:52px;position:relative}
.v6 .hero .m{color:var(--dim);font-size:12px;letter-spacing:.1em;text-transform:uppercase;margin-bottom:10px}
.v6 .hero .num{font-size:96px;font-weight:500;letter-spacing:-.03em;line-height:1;
 text-shadow:0 0 60px var(--acc-glow);display:inline-block}
.v6 .hero .tag{color:var(--grn);font-size:14px;margin-left:14px;font-weight:400}
.v6 .why{display:flex;gap:40px;margin-top:30px;flex-wrap:wrap}
.v6 .why .k{color:var(--dim);font-size:12px;margin-bottom:4px}
.v6 .why .v{font-size:18px;font-weight:500}
.v6 .sec{border-top:1px solid var(--hair);padding:28px 0}
.v6 .sec .h{color:var(--dim);font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;margin-bottom:16px}
.v6 .statline{display:flex;gap:48px;flex-wrap:wrap}
.v6 .statline .k{color:var(--dim2);font-size:12px}
.v6 .statline .v{font-size:19px;font-weight:500;margin-top:4px}
.v6 .rowline{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--hair);font-size:12.5px}
.v6 .rowline:last-child{border-bottom:none}
.v6 .rowline b{font-weight:400;color:var(--txt)}
.v6 .rowline .time{color:var(--dim2);font-size:11px}
.v6 .attn{display:flex;align-items:center;justify-content:space-between;padding:12px 0}
.v6 .attn .t b{font-weight:500}.v6 .attn .t span{color:var(--dim);font-size:12px;margin-left:8px}
.v6 .attn .actions a{color:var(--acc);font-size:12px;margin-left:16px;text-decoration:none;cursor:pointer}
.v6 .asktrough{border-top:1px solid var(--hair);margin-top:6px;padding-top:18px}
.v6 .asktrough input{width:100%;background:transparent;border:none;border-bottom:1px solid var(--hair);
 color:var(--txt);font-size:14px;padding:9px 0;outline:none;font-family:inherit}
.v6 .asktrough input::placeholder{color:var(--dim2)}
.v6 .asktrough input:focus{border-bottom-color:var(--acc)}

/* Chat composition */
.v6chat{display:grid;grid-template-columns:1fr;position:relative}
.v6chat .topbar{display:flex;align-items:center;gap:12px;padding:6px 0 0;color:var(--dim2);font-size:12px}
.v6chat .topbar .hist{cursor:pointer;color:var(--dim)}
.v6chat .topbar .hist:hover{color:var(--txt)}
.v6chat .thread{max-width:640px;margin:0 auto;padding:40px 0 130px}
.v6chat .msg{margin-bottom:28px}
.v6chat .msg .who{font-size:10.5px;color:var(--dim2);letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px}
.v6chat .msg.user{text-align:right}
.v6chat .msg.user .bubble{display:inline-block;background:var(--acc);color:#fff;border-radius:12px;
 padding:9px 13px;font-size:13px;max-width:80%}
.v6chat .msg.assistant .bubble{display:inline-block;background:var(--card);border:1px solid var(--edge);
 border-radius:12px;padding:10px 13px;font-size:13px;line-height:1.6;color:var(--txt);max-width:88%}
.v6chat .src{margin-top:10px;display:flex;gap:14px;flex-wrap:wrap}
.v6chat .src span{color:var(--acc);font-size:11.5px;cursor:pointer;border-bottom:1px solid transparent}
.v6chat .src span:hover{border-bottom-color:var(--acc)}
.v6chat .starters{max-width:640px;margin:0 auto 18px;display:flex;gap:10px;flex-wrap:wrap}
.v6chat .starters .chip{background:transparent;border:none;border-bottom:1px solid var(--hair);border-radius:0;
 color:var(--dim);font-size:12px;cursor:pointer;padding:0 0 2px;margin:0}
.v6chat .starters .chip:hover{color:var(--txt);border-bottom-color:var(--acc)}
.v6chat .inputdock{max-width:640px;margin:0 auto}
.v6chat .inputdock input{width:100%;background:transparent;border:none;border-bottom:1px solid var(--hair);
 color:var(--txt);font-size:14px;padding:9px 2px;outline:none;font-family:inherit}
.v6chat .inputdock input:focus{border-bottom-color:var(--acc)}
.v6chat .modelrow{display:flex;gap:14px;margin-bottom:18px;color:var(--dim2);font-size:11.5px}
.v6chat .modelrow select{background:transparent;border:none;color:var(--dim);font-size:11.5px;font-family:inherit}
.histpanel{position:fixed;top:0;left:216px;width:280px;height:100vh;background:var(--bg);
 border-right:1px solid var(--hair);padding:26px 22px;box-shadow:20px 0 40px rgba(0,0,0,.3);
 transform:translateX(-100%);transition:transform var(--t-base) var(--ease);z-index:8;overflow-y:auto}
.histpanel.open{transform:translateX(0)}
.histpanel .h{color:var(--dim);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin-bottom:16px}
.histpanel .sess{padding:10px 0;border-bottom:1px solid var(--hair);cursor:pointer}
.histpanel .sess .p{font-size:12px;color:var(--txt);margin-bottom:3px}
.histpanel .sess .m{font-size:10.5px;color:var(--dim2)}

/* Memory Center composition — graph is the environment, not a boxed canvas */
.v6mem{position:relative;height:100vh;margin:-32px -40px;overflow:hidden}
.v6mem .memcanvas{position:absolute;inset:0;background:radial-gradient(circle at 38% 30%,rgba(124,140,255,.05),transparent 55%)}
.v6mem .floatsearch{position:absolute;top:20px;left:50%;transform:translateX(-50%);width:420px;z-index:3;
 background:rgba(28,29,34,.7);backdrop-filter:blur(30px) saturate(180%);-webkit-backdrop-filter:blur(30px) saturate(180%);
 border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:10px 16px;box-shadow:0 20px 50px rgba(0,0,0,.4)}
.v6mem .floatsearch input{width:100%;background:transparent;border:none;color:var(--txt);font-size:13px;outline:none;
 font-family:-apple-system,sans-serif}
.v6mem .floatsearch .click{padding:5px 0;color:var(--txt);font-size:12px}
.v6mem .filters{position:absolute;top:26px;right:26px;display:flex;gap:14px;z-index:3}
.v6mem .filters label{color:var(--txt);font-size:11.5px;cursor:pointer;display:flex;align-items:center;gap:5px;
 font-family:-apple-system,sans-serif}
.v6mem .filters .sw{width:8px;height:8px;border-radius:50%;display:inline-block}
.v6mem .memdetail{position:absolute;top:0;right:0;width:320px;height:100%;z-index:4;
 background:rgba(19,20,23,.9);backdrop-filter:blur(24px) saturate(160%);-webkit-backdrop-filter:blur(24px) saturate(160%);
 border-left:1px solid rgba(255,255,255,.1);padding:28px 22px;overflow-y:auto;
 font-family:-apple-system,sans-serif;transform:translateX(100%);transition:transform var(--t-base) var(--ease)}
.v6mem .memdetail.open{transform:translateX(0)}
.v6mem .memdetail .h{color:var(--dim);font-size:11px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px}

/* Cost Intelligence — mockup card styling */
.v6cost .hero{background:var(--panel);border:1px solid var(--edge);border-radius:14px;padding:16px 18px;margin-bottom:12px}
.v6cost .eyebrow{color:var(--dim);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;font-weight:600;margin-bottom:10px}
.v6cost .headline{font-size:42px;font-weight:700;letter-spacing:-.02em;margin-bottom:4px}
.v6cost .subline{color:var(--dim);font-size:13px;margin-bottom:6px}
.v6cost .subline b{color:var(--grn);font-weight:600}
.v6cost .chart{margin:18px 0 4px}
.v6cost .chart svg{width:100%;display:block}
.v6cost .sec{background:var(--panel);border:1px solid var(--edge);border-radius:14px;padding:16px 18px;margin-bottom:12px}
.v6cost .sec .h{color:var(--dim);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;margin-bottom:12px;font-weight:600}
.v6cost .tally{display:flex;gap:48px;flex-wrap:wrap;border-top:1px solid var(--hair);margin-top:14px;padding-top:14px}
.v6cost .tally .k{color:var(--dim2);font-size:11px}
.v6cost .tally .v{font-size:18px;font-weight:600;margin-top:4px}
.v6cost .rowline{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--hair);font-size:12.5px}
.v6cost .rowline:last-child{border-bottom:none}
.v6cost .rowline .meta{color:var(--dim2);font-size:11.5px}
.v6cost .rowline .val{font-weight:600}
.v6cost .evalrow{cursor:pointer}
.v6cost .evalrow a{color:var(--acc);font-size:11.5px;text-decoration:none}
.tile,.panel{background:var(--panel);border:1px solid var(--edge);border-radius:14px;padding:16px 18px}
.panel{margin-bottom:12px}
.tile:hover{border-color:#333748}
.northstar{position:relative;overflow:hidden}
.northstar::before{content:'';position:absolute;top:-40%;left:-10%;width:420px;height:420px;
background:radial-gradient(circle,var(--acc-glow),transparent 68%);pointer-events:none}
.northstar .ns-score{text-shadow:0 0 30px var(--acc-glow)}
.card.glass{background:rgba(36,37,44,.62);backdrop-filter:blur(18px) saturate(160%);
-webkit-backdrop-filter:blur(18px) saturate(160%);border:1px solid rgba(255,255,255,.07);
box-shadow:0 8px 26px rgba(0,0,0,.28)}
.tile .t,h3.h{color:var(--dim);font-size:10.5px;letter-spacing:.1em;margin:0 0 10px;
font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-weight:600;text-transform:uppercase}
.tile .v{font-size:24px;margin:2px 0;font-weight:600}.m{color:var(--dim);font-size:11px}
.bar{height:4px;background:var(--card);border-radius:3px;overflow:hidden;max-width:220px}
.bar i{display:block;height:100%;background:var(--acc)}
.cols{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}
.col{background:var(--panel);border:1px solid var(--edge);border-radius:14px;padding:10px;min-height:130px}
.card{background:var(--panel);border:none;border-left:2px solid var(--edge);
border-radius:10px;padding:8px 10px;margin-bottom:8px;cursor:pointer}
.card:hover{border-left-color:var(--acc)}
.GREEN{border-left-color:var(--grn)}.YELLOW{border-left-color:var(--yel)}.RED{border-left-color:var(--red)}
.running{border-left-color:var(--yel);animation:pulse 1.4s infinite}
.failed{border-left-color:var(--red)}
@keyframes pulse{50%{opacity:.55}}
.row{display:grid;grid-template-columns:2fr 1fr;gap:36px;margin-top:8px}
#feed div{color:var(--dim);padding:7px 0;border-bottom:1px solid var(--hair);font-size:12px}
.rowline{padding:7px 0;border-bottom:1px solid var(--hair);font-size:12.5px;color:var(--txt)}
.rowline:last-child{border-bottom:none}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.g{background:var(--grn)}.y{background:var(--yel)}.r{background:var(--red)}
#insp{position:fixed;top:0;right:-580px;width:560px;height:100vh;background:rgba(19,20,23,.9);
backdrop-filter:blur(24px) saturate(160%);-webkit-backdrop-filter:blur(24px) saturate(160%);
border-left:1px solid rgba(255,255,255,.1);padding:24px;overflow:auto;transition:right .18s;z-index:9}
#insp.open{right:0}
.chip{display:inline-block;background:var(--card);border:1px solid var(--edge);
border-radius:20px;padding:2px 9px;margin:2px;font-size:11px;
transition:border-color var(--t-fast) var(--ease),transform var(--t-fast) var(--ease)}
.chip[onclick]:hover{border-color:var(--acc);transform:translateY(-1px)}
.chip[onclick]:active{transform:translateY(0) scale(.97)}
.chip .sw{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px}
.chip.ok{background:var(--acc);border-color:var(--acc);color:#fff}
.chip.A,.bad{border-color:var(--red);color:var(--red)}
.chip.B{color:var(--dim)}.chip.ok{border-color:var(--grn);color:var(--grn)}
.flow{border-left:2px solid var(--edge);margin:8px 0 8px 6px;padding-left:12px}
.flow div{margin:4px 0}
pre{white-space:pre-wrap;background:var(--panel);border-radius:8px;padding:10px;
font-size:11px;color:var(--dim);max-height:340px;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
td,th{border-bottom:1px solid var(--hair);padding:8px 10px 8px 0;text-align:left}
th{color:var(--dim);font-size:10.5px;letter-spacing:.08em;font-family:-apple-system,sans-serif;font-weight:600}
tr.click{cursor:pointer;transition:background var(--t-fast) var(--ease)}tr.click:hover{background:var(--panel)}
.click{cursor:pointer;transition:background var(--t-fast) var(--ease)}.click:hover{background:var(--panel)}
button.btn-primary{background:var(--acc);color:#fff;border:1px solid var(--acc)}
button{background:var(--card);color:var(--txt);border:1px solid var(--edge);
border-radius:10px;padding:4px 10px;cursor:pointer;
transition:border-color var(--t-fast) var(--ease),transform var(--t-fast) var(--ease)}
button:hover{border-color:var(--acc)}button:active{transform:scale(.96)}
input{background:var(--panel);border:1px solid var(--edge);color:var(--txt);
border-radius:8px;padding:4px 7px;width:72px;
transition:border-color var(--t-fast) var(--ease)}
input:focus{outline:none;border-color:var(--acc)}
.x{float:right;color:var(--dim);cursor:pointer;font-size:18px}
svg{border-radius:12px}
</style></head><body><div id="app">
<div id="side"><h1><span class="mark">&#11041;</span><span class="bt"><b>OPENCLAW</b><i>MISSION CONTROL</i></span></h1>
<div class="m" style="cursor:pointer;margin:0 0 20px;padding-right:16px;display:flex;align-items:center;gap:8px" onclick="openCommandPalette()">
 <span>&#8981;</span> Search or jump to&hellip; <kbd style="margin-left:auto;border:1px solid var(--edge);border-radius:4px;padding:0 4px;font-size:9.5px">&#8984;K</kbd></div>
<!-- ICON RULE (Mission Control UX V3, Section 10): before adding a
     new nav item, check its icon against every OTHER icon already
     used WITHIN THE SAME GROUP first (collisions inside one group
     are what actually confuse; a repeat across two different groups
     matters far less). Current icons in use, by group:
     Core Workspace: ◈ ◎ $ ▲  |  Operate: ▣ ➕ ▤ ♥  |  Evaluate: ⬡ ✦ ▧ ✓
     Learn & Improve: ◐ ◔ ↗ ⚖  |  Data: ▦ ⬢ ◆  |  Finance: ▢
     Pick something visually distinct from its own group's list. -->
<div class="sidebar-group-header">CORE WORKSPACE</div>
<a data-v="chatcc"><span class="ic">◈</span>Command Chat</a>
<a data-v="memcc"><span class="ic">◎</span>Memory Center</a>
<a data-v="costcc"><span class="ic">$</span>Cost Intelligence</a>
<a data-v="success"><span class="ic">▲</span>Apex Success</a>
<div class="sidebar-group-header">OPERATE</div>
<a data-v="deck" class="on"><span class="ic">▣</span>Command Deck</a>
<a data-v="intake"><span class="ic">➕</span>Pick Intake</a>
<a data-v="opsv"><span class="ic">▤</span>Operations</a>
<a data-v="healthcc"><span class="ic">♥</span>Health Center</a>
<div class="sidebar-group-header">EVALUATE</div>
<a data-v="canvas"><span class="ic">⬡</span>Agent Canvas</a>
<a data-v="confaudit"><span class="ic">✦</span>Confidence Audit</a>
<a data-v="rulecc"><span class="ic">▧</span>Rule Command</a>
<a data-v="valid"><span class="ic">✓</span>Validation</a>
<a data-v="results"><span class="ic">⚑</span>Pick Results</a>
<div class="sidebar-group-header">LEARN &amp; IMPROVE</div>
<a data-v="learningcc"><span class="ic">◐</span>Learning Center</a>
<a data-v="mlready"><span class="ic">◔</span>ML Readiness</a>
<a data-v="rules"><span class="ic">↗</span>Rule Roadmap</a>
<a data-v="calcc"><span class="ic">⚖</span>Calibration</a>
<div class="sidebar-group-header">DATA</div>
<a data-v="warehouse"><span class="ic">▦</span>Data Warehouse</a>
<a data-v="warehousecc"><span class="ic">⬢</span>Warehouse Command</a>
<a data-v="agents"><span class="ic">◆</span>Agent Layer</a>
<div class="sidebar-group-header">FINANCE</div>
<a data-v="bank"><span class="ic">▢</span>Bankroll</a>
<a onclick="toggleSidebarCustomize()" style="margin-top:10px" id="sidebarCustomizeToggle">⚙ Customize order</a>
<div style="margin-top:22px" class="m" id="hs"></div>
<div class="sideprofile"><span class="pav">A</span><span class="pt"><b>Ace</b><i>Administrator</i><em>&#9679; Online</em></span></div>
<div class="sideicons"><span>&#9789;</span><span>&#9881;</span><span>?</span></div></div>
<div id="main">
 <div id="v-deck"></div>
 <div id="v-intake" style="display:none"></div>
 <div id="v-canvas" style="display:none"></div>
 <div id="v-confaudit" style="display:none"></div>
 <div id="v-rulecc" style="display:none"></div>
 <div id="v-learningcc" style="display:none"></div>
 <div id="v-healthcc" style="display:none"></div>
 <div id="v-mlready" style="display:none"></div>
 <div id="v-warehouse" style="display:none"></div>
 <div id="v-warehousecc" style="display:none"></div>
 <div id="v-agents" style="display:none"></div>
 <div id="v-opsv" style="display:none"></div>
 <div id="v-valid" style="display:none"></div>
 <div id="v-rules" style="display:none"></div>
 <div id="v-bank" style="display:none"></div>
 <div id="v-costcc" style="display:none"></div>
 <div id="v-calcc" style="display:none"></div>
 <div id="v-success" style="display:none"></div>
 <div id="v-chatcc" style="display:none"></div>
 <div id="v-memcc" style="display:none"></div>
 <div id="v-results" style="display:none"></div>
</div></div>
<div id="insp"><span class="x" onclick="insp.classList.remove('open')">✕</span><div id="ibody"></div></div>
<script>
const insp=document.getElementById('insp');
const pct=x=>x==null?'–':Math.round(x*100)+'%';
const esc=s=>String(s??'').replace(/</g,'&lt;');
const gradeEmoji=g=>g=='GREEN'?'🟢':g=='YELLOW'?'🟡':g=='RED'?'🔴':'';
// Mirrors confidence_to_american_odds()/format_american_odds() in
// openclaw_core.py exactly -- same standard fair-odds conversion, no
// vig, same round-trip math. p.confidence here is already a 0-1
// fraction (see pct() above), not 0-100, unlike the Python version's
// input -- kept as its own small function rather than importing
// across languages, but the formula itself must stay identical to
// the Python side if either ever changes.
const impliedOdds=conf=>{
 if(conf==null) return null;
 const p=conf;
 if(p<=0||p>=1) return null;
 const odds = p>=0.5 ? -100*(p/(1-p)) : 100*((1-p)/p);
 return Math.round(odds);
};
const fmtOdds=o=>o==null?'--':(o>0?'+'+o:''+o);
function bar(f){return `<div class="bar"><i style="width:${Math.min(100,Math.max(0,(f||0)*100))}%"></i></div>`}
function donutChart(slices, size, centerLabel){
 // slices: [{label, value, color}]. Real values only -- every caller
 // below sources these from data already fetched for that screen.
 size = size || 140;
 const total = slices.reduce((a,s)=>a+(s.value||0),0);
 const r = size/2 - 14, cx = size/2, cy = size/2, c = 2*Math.PI*r;
 let offset = 0;
 const arcs = total > 0 ? slices.filter(s=>s.value>0).map(s => {
  const frac = s.value/total;
  const dash = frac*c;
  const el = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${s.color}" stroke-width="20"
   stroke-dasharray="${dash.toFixed(1)} ${(c-dash).toFixed(1)}" stroke-dashoffset="${(-offset).toFixed(1)}"
   transform="rotate(-90 ${cx} ${cy})"/>`;
  offset += dash;
  return el;
 }).join('') : `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="var(--edge)" stroke-width="20"/>`;
 return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
  ${arcs}
  ${centerLabel?`<text x="${cx}" y="${cy-2}" text-anchor="middle" font-size="${size*0.15}" font-weight="700" fill="var(--txt)" font-family="-apple-system,sans-serif">${esc(centerLabel)}</text>`:''}
 </svg>`;
}
function donutLegend(slices){
 return slices.map(s=>`<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:12px">
  <span style="width:9px;height:9px;border-radius:50%;background:${s.color};flex-shrink:0"></span>
  <span style="flex:1">${esc(s.label)}</span><b>${s.value}</b></div>`).join('');
}
function sparkline(hist, key, w, h, color){
 // Real history only. 0 points -> nothing. 1 point -> a dot, never a
 // fabricated flat line. 2+ points -> a real connecting line.
 w = w||64; h = h||24; color = color||'var(--acc)';
 const pts = (hist||[]).map(r=>r[key]).filter(v=>v!=null);
 if(!pts.length) return '';
 if(pts.length===1) return `<svg width="${w}" height="${h}"><circle cx="${w-4}" cy="${h/2}" r="2.5" fill="${color}"/></svg>`;
 const lo=Math.min(...pts), hi=Math.max(...pts), span=(hi-lo)||1;
 const step = w/(pts.length-1);
 const poly = pts.map((v,i)=>`${(i*step).toFixed(1)},${(h-4-((v-lo)/span)*(h-8)).toFixed(1)}`).join(' ');
 return `<svg width="${w}" height="${h}"><polyline points="${poly}" fill="none" stroke="${color}" stroke-width="1.6"/></svg>`;
}
function ringGauge(pctVal, size, color, label){
 size = size || 96;
 const r = (size/2) - 8, c = 2*Math.PI*r, cx = size/2, cy = size/2;
 const p = Math.max(0, Math.min(100, pctVal||0));
 const dash = (p/100)*c;
 color = color || (p>=80?'var(--grn)':p>=50?'var(--yel)':'var(--red)');
 return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" style="flex-shrink:0">
  <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="var(--edge)" stroke-width="7"/>
  <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}" stroke-width="7"
   stroke-linecap="round" stroke-dasharray="${dash.toFixed(1)} ${c.toFixed(1)}"
   transform="rotate(-90 ${cx} ${cy})"/>
  <text x="${cx}" y="${cy-2}" text-anchor="middle" font-size="${size*0.22}" font-weight="700" fill="var(--txt)" font-family="-apple-system,sans-serif">${Math.round(p)}%</text>
  ${label?`<text x="${cx}" y="${cy+size*0.16}" text-anchor="middle" font-size="${size*0.08}" fill="var(--dim)" font-family="-apple-system,sans-serif">${esc(label)}</text>`:''}
 </svg>`;
}
let VIEW='deck';
document.querySelectorAll('#side a').forEach(a=>{
 if(!a.dataset.v) return; // skip non-navigation links (e.g. the sidebar customize toggle)
 a.onclick=()=>{
 VIEW=a.dataset.v;document.querySelectorAll('#side a').forEach(x=>x.classList.remove('on'));
 a.classList.add('on');
 document.querySelectorAll('#main>div').forEach(d=>d.style.display='none');
 document.getElementById('v-'+VIEW).style.display='block';load()}
});

const SIDEBAR_ORDER_KEY = 'apex_sidebar_order_v2';

function applySavedSidebarOrder(){
 // v2: reorder WITHIN each group only. The v1 version moved every link
 // to just before #hs, which ripped all links out of their groups and
 // piled them below FINANCE -- the "dead tabs" bug. Old v1 saved data
 // is intentionally ignored via the key bump above.
 let saved;
 try { saved = JSON.parse(localStorage.getItem(SIDEBAR_ORDER_KEY) || 'null'); }
 catch(e) { saved = null; }
 if(!saved || !Array.isArray(saved)) return;
 const idx = k => { const i = saved.indexOf(k); return i < 0 ? 9999 : i; };
 document.querySelectorAll('#side .sidebar-group-header').forEach(h => {
  const links = [];
  let n = h.nextElementSibling;
  while(n && n.tagName === 'A' && n.dataset && n.dataset.v){ links.push(n); n = n.nextElementSibling; }
  links.sort((a,b) => idx(a.dataset.v) - idx(b.dataset.v));
  let ref = h;
  links.forEach(a => { ref.after(a); ref = a; });
 });
}

function saveSidebarOrder(){
 const side = document.getElementById('side');
 const order = Array.from(side.querySelectorAll('a[data-v]')).map(a => a.dataset.v);
 try { localStorage.setItem(SIDEBAR_ORDER_KEY, JSON.stringify(order)); } catch(e) {}
}

let SIDEBAR_CUSTOMIZE_MODE = false;
function toggleSidebarCustomize(){
 SIDEBAR_CUSTOMIZE_MODE = !SIDEBAR_CUSTOMIZE_MODE;
 const toggle = document.getElementById('sidebarCustomizeToggle');
 if(toggle) toggle.textContent = SIDEBAR_CUSTOMIZE_MODE ? '✓ Done customizing' : '⚙ Customize order';
 document.querySelectorAll('#side a[data-v]').forEach(a=>{
  let ctrl = a.querySelector('.sidebarReorderCtrl');
  if(SIDEBAR_CUSTOMIZE_MODE){
   if(!ctrl){
    ctrl = document.createElement('span');
    ctrl.className = 'sidebarReorderCtrl';
    ctrl.style.cssText = 'float:right;display:flex;gap:4px';
    ctrl.innerHTML = `<span onclick="event.stopPropagation();moveSidebarItem(this,-1)" style="cursor:pointer">▲</span><span onclick="event.stopPropagation();moveSidebarItem(this,1)" style="cursor:pointer">▼</span>`;
    a.appendChild(ctrl);
   }
  } else if(ctrl) { ctrl.remove(); }
 });
}

function moveSidebarItem(ctrlChild, direction){
 const a = ctrlChild.closest('a[data-v]');
 if(!a) return;
 const side = document.getElementById('side');
 if(direction < 0 && a.previousElementSibling && a.previousElementSibling.dataset && a.previousElementSibling.dataset.v){
  side.insertBefore(a, a.previousElementSibling);
 } else if(direction > 0){
  const next = a.nextElementSibling;
  if(next && next.dataset && next.dataset.v) side.insertBefore(next, a);
 }
 saveSidebarOrder();
}
applySavedSidebarOrder();

// ================================================================
// COMMAND PALETTE (Mission Control UX V3, item 7). Cmd+K / Ctrl+K
// opens it from anywhere. Two result sources, both already built:
// (1) navigation targets -- the sidebar's own data-v/label pairs,
//     read directly from the real DOM, not a hardcoded duplicate list
//     that could drift out of sync with the actual nav.
// (2) records -- reuses /api/memory_search as-is. No new backend
//     route, no new query.
// ================================================================

const CP_NAV_TARGETS = () => Array.from(document.querySelectorAll('#side a[data-v]'))
 .map(a => ({ key: a.dataset.v, label: a.textContent.trim() }));

function openCommandPalette(){
 if(document.getElementById('cmdPaletteOverlay')) return;
 const overlay = document.createElement('div');
 overlay.id = 'cmdPaletteOverlay';
 overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:999;'+
  'display:flex;align-items:flex-start;justify-content:center;padding-top:12vh';
 overlay.onclick = (e) => { if(e.target === overlay) closeCommandPalette(); };
 overlay.innerHTML = `<div style="background:var(--panel);border:1px solid var(--edge);border-radius:10px;
   width:520px;max-width:90vw;padding:12px" onclick="event.stopPropagation()">
  <input id="cmdPaletteInput" placeholder="Jump to a screen, or search decisions and investigations..."
   style="width:100%;padding:8px;font-size:14px" autocomplete="off">
  <div id="cmdPaletteResults" style="margin-top:8px;max-height:340px;overflow-y:auto"></div>
  <div class="m" style="margin-top:8px">↵ open · Esc close</div>
 </div>`;
 document.body.appendChild(overlay);
 document.getElementById('cmdPaletteInput').focus();
 document.getElementById('cmdPaletteInput').addEventListener('input', cpRunSearch);
 cpRunSearch(); // show nav targets immediately, before any typing
}
function closeCommandPalette(){
 const overlay = document.getElementById('cmdPaletteOverlay');
 if(overlay) overlay.remove();
}
async function cpRunSearch(){
 const q = (document.getElementById('cmdPaletteInput')?.value || '').trim().toLowerCase();
 const navMatches = CP_NAV_TARGETS().filter(t => !q || t.label.toLowerCase().includes(q));
 let recordMatches = [];
 if(q.length >= 2){
  try { recordMatches = await (await fetch('/api/memory_search?q=' + encodeURIComponent(q))).json(); }
  catch(e) { recordMatches = []; }
 }
 const box = document.getElementById('cmdPaletteResults');
 if(!box) return; // palette may have closed while the record fetch was in flight
 const navHtml = navMatches.slice(0, 8).map(t =>
  `<div class="click" style="padding:6px 4px" onclick="cpGoToNav('${t.key}')">→ ${esc(t.label)}</div>`).join('');
 const recordHtml = recordMatches.slice(0, 6).map(r =>
  `<div class="click" style="padding:6px 4px" onclick="cpGoToRecord('${r.type}','${r.id}','${esc(r.topic).replace(/'/g,"\\'")}')">
   <span class="chip" style="font-size:9px">${esc(r.type)}</span> ${esc(r.topic)}</div>`).join('');
 box.innerHTML = (navHtml || recordHtml)
  ? `${navHtml ? '<div class="m">SCREENS</div>'+navHtml : ''}${recordHtml ? '<div class="m" style="margin-top:6px">MEMORY</div>'+recordHtml : ''}`
  : '<div class="m">no matches</div>';
}
function cpGoToNav(key){
 closeCommandPalette();
 const link = document.querySelector(`#side a[data-v="${key}"]`);
 if(link) link.click();
}
function cpGoToRecord(type, id, label){
 closeCommandPalette();
 memGoToSearch(label);
}
document.addEventListener('keydown', (e) => {
 if((e.metaKey || e.ctrlKey) && e.key === 'k'){
  e.preventDefault();
  openCommandPalette();
 } else if(e.key === 'Escape'){
  closeCommandPalette();
 }
});

async function load(){
 const guard = async (fn) => {
  try { await fn(); }
  catch(e){
   console.error('[MissionControl] view "'+VIEW+'" failed:', e);
   const el = document.getElementById('v-'+VIEW);
   if(el) el.innerHTML =
    '<div class="m" style="padding:20px 0">This screen hit an error while loading \u2014 '+esc(e.message||e)+'. Other screens are unaffected.</div>';
  }
 };
 if(VIEW=='deck'||VIEW=='opsv'||VIEW=='intake'||VIEW=='agents'||VIEW=='canvas'){await guard(async()=>{
  const s=await(await fetch('/api/state')).json();
  if(VIEW=='deck')deck(s);else if(VIEW=='opsv')opsv(s);else if(VIEW=='agents')agentsView(s);else if(VIEW=='canvas')agentCanvas(s);else intakeView(s);
  hs.innerHTML=`health <b>${s.health_score}%</b><br>${s.now}`})}
 if(VIEW=='valid')await guard(async()=>{const vv=await(await fetch('/api/validation')).json();
  const vw=await(await fetch('/api/validation_workspace')).json();
  valid(vv, vw)});
 if(VIEW=='rules')await guard(async()=>rules(await(await fetch('/api/roadmap')).json()));
 if(VIEW=='bank')await guard(async()=>bank(await(await fetch('/api/bankroll')).json()));
 if(VIEW=='results')await guard(async()=>resultsCC(await(await fetch('/api/pick_results')).json()));
 if(VIEW=='costcc')await guard(async()=>costCC(await(await fetch('/api/cost_cc')).json()));
 if(VIEW=='calcc')await guard(async()=>calibrationCC(await(await fetch('/api/calibration')).json()));
 if(VIEW=='success')await guard(async()=>{
  const [st, cost, val, bankroll, wh, conf] = await Promise.all([
   fetch('/api/state').then(r=>r.json()),
   fetch('/api/cost_cc').then(r=>r.json()).catch(()=>null),
   fetch('/api/validation').then(r=>r.json()).catch(()=>null),
   fetch('/api/bankroll').then(r=>r.json()).catch(()=>null),
   fetch('/api/warehouse_overview').then(r=>r.json()).catch(()=>null),
   fetch('/api/confidence').then(r=>r.json()).catch(()=>null),
  ]);
  successCC(st, cost, val, bankroll, wh, conf)});
 if(VIEW=='chatcc')await guard(async()=>chatCC());
 if(VIEW=='memcc')await guard(async()=>memCC());
 if(VIEW=='confaudit')await guard(async()=>confAudit(await(await fetch('/api/confidence')).json()));
 if(VIEW=='rulecc')await guard(async()=>{
   const [d, rr] = await Promise.all([
     fetch('/api/rulecc').then(r=>r.json()),
     fetch('/api/rule_registry_status').then(r=>r.json())
   ]);
   ruleCC(d, rr.last_run);
 });
 if(VIEW=='learningcc')await guard(async()=>learningCC(await(await fetch('/api/learning')).json()));
 if(VIEW=='healthcc')await guard(async()=>healthCC(await(await fetch('/api/health_center')).json()));
 if(VIEW=='mlready')await guard(async()=>mlReady(await(await fetch('/api/ml_readiness')).json()));
 if(VIEW=='warehouse')await guard(async()=>loadWarehouse());
 if(VIEW=='warehousecc')await guard(async()=>loadWarehouseCC());
}
async function loadWarehouseCC(){
 const d = await(await fetch('/api/warehouse_cc')).json();
 const runs = await(await fetch('/api/warehouse_cc_runs')).json();
 const players = await(await fetch('/api/warehouse_cc_players')).json();
 const triage = await(await fetch('/api/warehouse_warnings_triage')).json();
 warehouseCCView(d, runs, players, triage);
}
async function showWarehousePlayerDetail(name){
 const d = await(await fetch('/api/warehouse_cc_player_detail?name='+encodeURIComponent(name))).json();
 const pd = document.getElementById('wcc-player-detail');
 pd.style.display='block';
 pd.innerHTML = `<h3 class="h">${esc(d.player)} <span class="m" style="float:right;cursor:pointer" onclick="document.getElementById('wcc-player-detail').style.display='none'">✕ close</span></h3>
  <div class="m">Match count: ${d.match_count}</div>
  <table><tr><th>YEAR</th><th>HOLD%</th><th>BREAK%</th><th>ACES</th><th>SOURCE</th></tr>
  ${d.years.map(y=>`<tr><td>${y.year}</td><td>${y.hold_pct??'—'}</td><td>${y.break_pct??'—'}</td>
  <td>${y.avg_aces??'—'}</td><td><span class="chip ${y.data_source=='FALLBACK_PARTIAL'?'A':'ok'}">${esc(y.data_source||'')}</span></td></tr>`).join('')}</table>
  <h3 class="h" style="margin-top:14px">Recent Matches</h3>
  ${(d.recent||[]).length===0 ? '<div class="m">No recent match records on file.</div>' : d.recent.map(m=>{
    const resultChip = m.won===true ? '<span class="chip ok">W</span>' : m.won===false ? '<span class="chip A">L</span>' : '<span class="chip">—</span>';
    const oppLine = `vs ${esc(m.opponent)} — ${esc(m.tournament||'')} (${esc(m.surface||'—')})`;
    const statLine = (m.this_winners!=null || m.this_unforced_errors!=null)
      ? `${m.this_winners??'—'} winners / ${m.this_unforced_errors??'—'} UE — opponent ${m.opponent_winners??'—'} winners / ${m.opponent_unforced_errors??'—'} UE`
      : 'No box-score detail on file for this match';
    const serveLine = (m.this_first_serve_in!=null && m.this_first_serve_of)
      ? `1st serve ${Math.round(100*m.this_first_serve_in/m.this_first_serve_of)}%${m.this_fastest_serve?` · fastest ${m.this_fastest_serve} km/h`:''}`
      : '';
    const bpLine = (m.this_bp_converted!=null && m.this_bp_converted_of)
      ? `Break points ${m.this_bp_converted}/${m.this_bp_converted_of}` : '';
    const gamesLine = (m.this_games!=null && m.opponent_games!=null)
      ? `Games ${m.this_games}-${m.opponent_games}${m.h2h_at_match?` · H2H as of this match: ${esc(m.h2h_at_match)}`:''}` : '';
    const oddsLine = (m.this_odd || m.opponent_odd)
      ? `Market odds at match time — this player: ${esc(m.this_odd||'—')} · opponent: ${esc(m.opponent_odd||'—')}` : '';
    return `<div class="panel" style="margin-top:8px;padding:8px 10px">
      <div>${resultChip} <b>${esc(m.match_date||'')}</b> · ${oppLine} · ${esc(m.score||'')}</div>
      <div class="m" style="margin-top:4px">${statLine}</div>
      ${serveLine?`<div class="m">${serveLine}</div>`:''}
      ${bpLine?`<div class="m">${bpLine}</div>`:''}
      ${gamesLine?`<div class="m">${gamesLine}</div>`:''}
      ${oddsLine?`<div class="m">${oddsLine}</div>`:''}
    </div>`;
  }).join('')}`
}
async function loadWarehouse(){
 const w = await(await fetch('/api/warehouse_overview')).json();
 const players = await(await fetch('/api/warehouse_players')).json();
 const audit = await(await fetch('/api/warehouse_coverage_audit')).json();
 const rules = await(await fetch('/api/warehouse_rule_audit')).json();
 warehouseView(w, players, audit, rules);
}
async function showPlayerDetail(name){
 const d = await(await fetch('/api/warehouse_player_detail?name='+encodeURIComponent(name))).json();
 const pd = document.getElementById('player-detail-panel');
 pd.style.display='block';
 pd.innerHTML = `<h3 class="h">${esc(d.player)} <span class="m" style="float:right;cursor:pointer" onclick="document.getElementById('player-detail-panel').style.display='none'">✕ close</span></h3>
  <div class="m">Total records: ${d.total_records} · Props: ${d.prop_rows} · Moneyline: ${d.moneyline_rows} · Outcome completion: ${pct(d.outcome_completion_pct)}</div>
  <div class="m">Date range: ${esc(d.date_range.first||'—')} to ${esc(d.date_range.last||'—')}</div>
  <div style="margin-top:8px"><b>By market:</b> ${d.by_market.map(m=>`${esc(m.market)}: ${m.n}`).join(' · ')||'none'}</div>
  <div style="margin-top:4px"><b>Outcomes:</b> ${d.outcomes.map(o=>`${esc(o.outcome)}: ${o.n}`).join(' · ')||'none'}</div>
  <div style="margin-top:4px"><b>Surfaces:</b> ${d.surfaces.map(s=>`${esc(s.surface)}: ${s.n}`).join(' · ')||'none'}</div>
  <div style="margin-top:4px"><b>Tournaments:</b> ${d.tournaments.map(t=>`${esc(t.tournament)}: ${t.n}`).join(' · ')||'none'}</div>`;
}
let INTAKE_FILTER = 'ALL';
function intakeSetFilter(f){ INTAKE_FILTER = f; if(window.LAST_INTAKE) intakeView(window.LAST_INTAKE); }
function intakeView(s){const q=s.intake||[];
 window.LAST_INTAKE = s;
 const mtr={QUEUED:0,RUNNING:0,DONE:0,FAILED:0,BATCH_PENDING:0};q.forEach(i=>mtr[i.status]=(mtr[i.status]||0)+1);
 const filterMap={ALL:null,WAITING:'QUEUED',RUNNING:'RUNNING',COMPLETED:'DONE',FAILED:'FAILED',BATCH:'BATCH_PENDING'};
 const wanted = filterMap[INTAKE_FILTER];
 const qFiltered = wanted ? q.filter(i=>i.status===wanted) : q;
 const tab=(key,label,count)=>`<span class="chip ${INTAKE_FILTER===key?'ok':''}" style="cursor:pointer" onclick="intakeSetFilter('${key}')">${label} ${count}</span>`;
 const intakeIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">Where new props and moneyline picks enter the system — by typing a prop directly, uploading a PrizePicks or moneyline screenshot, or via Slip Review. WAITING/RUNNING/COMPLETED/FAILED reflect where each submission actually is in the pipeline right now.</div>
  <div class="m" style="margin-top:6px">⏳ <b>BATCH</b> means research already ran but the evaluation call itself is deferred to Anthropic's Batch API at 50% cost, posting in ~2-8 hours instead of immediately — an intentional cost/speed tradeoff you choose per submission, not a stuck item.</div>
 </div>`;
 document.getElementById('v-intake').innerHTML= intakeIntro +
 `<div class="strip">
  <div class="tile"><div class="t">WAITING</div><div class="v">${mtr.QUEUED}</div></div>
  <div class="tile"><div class="t">RUNNING</div><div class="v">${mtr.RUNNING}</div></div>
  <div class="tile"><div class="t">COMPLETED</div><div class="v">${mtr.DONE}</div></div>
  <div class="tile"><div class="t">FAILED</div><div class="v" style="color:${mtr.FAILED?'var(--red)':'inherit'}">${mtr.FAILED}</div></div>
  <div class="tile"><div class="t">⏳ BATCH</div><div class="v" style="color:${mtr.BATCH_PENDING?'#f5c542':'inherit'}">${mtr.BATCH_PENDING}</div></div></div>
 <div style="margin-bottom:12px">${tab('ALL','All',q.length)} ${tab('WAITING','Waiting',mtr.QUEUED)} ${tab('RUNNING','Running',mtr.RUNNING)} ${tab('COMPLETED','Completed',mtr.DONE)} ${tab('FAILED','Failed',mtr.FAILED)} ${tab('BATCH','⏳ Batch',mtr.BATCH_PENDING)}</div>
 <div class="panel"><h3 class="h">➕ NEW PROP (text)</h3>
  <div class="m">format: Player MORE/LESS LINE STAT — e.g. <b>Novak Djokovic MORE 2.5 double_faults</b></div>
  <input id="tx" style="width:70%" placeholder="Player MORE/LESS LINE STAT">
  <button onclick="subText()">evaluate now</button>
  <div class="m" style="margin-top:4px">Batch: research runs now, evaluation deferred 2–8 h at 50% token cost. Result posts to #picks like a normal pick.</div></div>
 <div class="panel" style="margin-top:12px"><h3 class="h">📷 PRIZEPICKS SCREENSHOT</h3>
  <div class="m">Select a file, then choose how to submit:</div>
  <input type="file" id="img" accept="image/*" style="width:auto" onchange="window._propFile=this.files[0];document.getElementById('imgName').textContent=this.files[0]?'📎 '+this.files[0].name:'no file selected'">
  <span id="imgName" class="m">no file selected</span>
  <div style="margin-top:8px">
    <button onclick="subImg()">upload &amp; submit</button>
    <button onclick="subImgBatch()" style="margin-left:6px">⏳ queue for batch</button>
  </div>
  <span id="imgStatus" class="m"></span></div>
 <div class="panel" style="margin-top:12px"><h3 class="h">📷 MONEYLINE SCREENSHOT</h3>
  <div class="m">Select a file, then choose how to submit:</div>
  <input type="file" id="mlimg" accept="image/*" style="width:auto" onchange="window._mlFile=this.files[0];document.getElementById('mlImgName').textContent=this.files[0]?'📎 '+this.files[0].name:'no file selected'">
  <span id="mlImgName" class="m">no file selected</span>
  <div style="margin-top:8px">
    <button onclick="subMlImg()">upload &amp; submit</button>
    <button onclick="subMlImgBatch()" style="margin-left:6px">⏳ queue for batch</button>
  </div>
  <span id="mlImgStatus" class="m"></span></div>
 <div class="panel" style="margin-top:12px" id="slipReviewPanel"><h3 class="h">🎟️ SLIP REVIEW</h3><div class="m">loading&hellip;</div></div>
 <div class="panel" style="margin-top:12px"><h3 class="h">🎾 MONEYLINE</h3>
  <div class="m" style="margin-bottom:6px">Player A vs Player B — surface and tour default to hard/ATP if left blank</div>
  <input id="mlA" placeholder="Player A" style="width:22%">
  <input id="mlB" placeholder="Player B" style="width:22%">
  <input id="mlSurf" placeholder="surface" value="hard" style="width:70px">
  <input id="mlTour" placeholder="tour" value="atp" style="width:60px">
  <button onclick="subMoneyline()">submit to OpenClaw</button>
  <div class="m" style="margin-top:6px">Runs through the same Dataset A lifecycle as props — eval_id, heartbeats, confidence tracking, all included.</div>
  <table style="margin-top:8px"><tr><th>#</th><th>MATCH</th><th>STATUS</th><th>EVAL_ID</th><th>QUEUED</th></tr>
  ${(s.ml_intake||[]).map(m=>`<tr><td>${m.id}</td><td>${esc(m.player_a)} vs ${esc(m.player_b)}</td>
   <td><span class="chip ${m.display_status=='POSTED'?'ok':m.display_status=='FAILED'?'A':''}">${m.display_status}</span></td>
   <td class="m">${m.eval_id?esc(m.eval_id).slice(0,8)+'…':''}</td>
   <td class="m">${esc(m.queued_at||'')}</td></tr>`).join('')
   ||'<tr><td colspan="5" class="m">no moneyline submissions yet</td></tr>'}</table></div>
 <div class="panel" style="margin-top:12px"><h3 class="h">INTAKE QUEUE${INTAKE_FILTER!=='ALL'?' — '+INTAKE_FILTER:''}</h3>
  <table><tr><th>#</th><th>WHAT</th><th>STATUS</th><th>QUEUED</th><th>DIAGNOSTIC</th><th></th></tr>
  ${qFiltered.slice().reverse().map(i=>`<tr><td>${i.id}</td><td>${esc(i.label).slice(0,44)}</td>
   <td><span class="chip ${i.status=='DONE'?'ok':i.status=='FAILED'?'A':''}" style="${i.status=='BATCH_PENDING'?'color:#f5c542;border-color:#f5c542':''}">${i.status=='BATCH_PENDING'?'⏳ BATCH':i.status}${i.batch_eligible&&i.status!='BATCH_PENDING'?'<span style="color:#f5c542;font-size:10px;margin-left:3px">[B]</span>':''}</span></td>
   <td class="m">${i.queued_at||''}</td>
   <td class="m ${i.status=='FAILED'?'bad':''}">${i.status=='FAILED'
     ?'stage: '+esc(diagStage(i.error))+' — '+esc(i.error||'unknown')
     :esc(i.finished_at||i.started_at||'')}</td>
   <td>${i.status=='FAILED'?`<button onclick="retry(${i.id})">retry</button>`:''}</td></tr>`).join('')
   ||`<tr><td colspan="6" class="m">${INTAKE_FILTER==='ALL'?'queue empty — submit a prop above':'nothing in this filter'}</td></tr>`}</table>
  <div class="m" style="margin-top:6px">Submitted props appear in Operations as they move Research→Evaluation→Compliance→Scoring→Posted.</div></div>
 <div class="panel" style="margin-top:12px" id="intakeTopPlayers"><h3 class="h">Top players (most consistent)</h3><div class="m">loading\u2026</div></div>`+
 canvas(s);
 loadIntakeTopPlayers();
 loadSlipReview();
}
async function loadIntakeTopPlayers(){
 // Apex is tennis-only, not multi-sport -- the mockup's "Top Sports"
 // breakdown doesn't map onto how Apex actually works. The real,
 // meaningful equivalent is per-player consistency: which players
 // Apex has graded most often and hit on most reliably. Real source:
 // ops.player_coverage(), already wired at /api/warehouse_players.
 const el = document.getElementById('intakeTopPlayers');
 if(!el) return;
 try{
  const rows = await (await fetch('/api/warehouse_players')).json();
  const known = rows.map(r => ({...r, known: (r.wins||0)+(r.losses||0)}))
   .filter(r => r.known >= 3) // need a real sample before "consistent" means anything
   .map(r => ({...r, hitRate: r.wins/r.known}))
   .sort((a,b) => b.hitRate - a.hitRate || b.known - a.known)
   .slice(0, 8);
  el.innerHTML = `<h3 class="h">Top players (most consistent, min. 3 settled)</h3>` + (known.length ?
   known.map(r => `<div class="rowline" style="cursor:pointer" onclick="document.querySelector('[data-v=warehouse]').click()">
    <div style="display:flex;justify-content:space-between"><b>${esc(r.player)}</b><span>${pct(r.hitRate)}</span></div>
    <div class="m">${r.wins}W-${r.losses}L across ${r.markets} market${r.markets!=1?'s':''} \u2014 ${r.rows_n} total records</div></div>`).join('')
   : '<span class="m">not enough settled records yet for a consistency ranking</span>');
 } catch(e){ el.innerHTML = '<h3 class="h">Top players</h3><div class="m">unavailable</div>'; }
}
const SLIP_STAT_OPTIONS = [
 ['tg','Total Games'],['sets','Total Sets'],['aces','Aces'],
 ['df','Double Faults'],['bp','Break Points Won'],['gw','Games Won']];

async function loadSlipReview(){
 const el = document.getElementById('slipReviewPanel');
 if(!el) return;
 let items;
 try{ items = await (await fetch('/api/slip_review')).json(); }
 catch(e){ el.innerHTML = '<h3 class="h">🎟️ SLIP REVIEW</h3><div class="m">unavailable</div>'; return; }
 renderSlipReview(items||[]);
}

function renderSlipReview(items){
 const el = document.getElementById('slipReviewPanel');
 if(!el) return;
 window.LAST_SLIP_REVIEW = items;
 if(!items.length){
  el.innerHTML = '<h3 class="h">🎟️ SLIP REVIEW</h3><div class="m">nothing waiting — send a slip confirmation screenshot to #slips-pnl to see it here for review before it logs.</div>';
  return;
 }
 el.innerHTML = '<h3 class="h">🎟️ SLIP REVIEW<span class="chip A" style="margin-left:8px">'+items.length+' waiting</span></h3>'
  + items.map(item => `
  <div class="rowline" style="display:block;padding:12px 0">
   <div class="m" style="margin-bottom:8px">read from screenshot &middot; ${esc(item.submitted_at||'')} &middot; #${item.id}</div>
   ${item.legs.map((leg,idx)=>`
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
     <input id="sr${item.id}_leg${idx}_player" value="${esc(leg.player)}" style="width:150px" placeholder="player">
     <select id="sr${item.id}_leg${idx}_direction">
      <option value="MORE" ${leg.direction==='MORE'?'selected':''}>MORE</option>
      <option value="LESS" ${leg.direction==='LESS'?'selected':''}>LESS</option>
     </select>
     ${leg.bet_type==='moneyline' ? `<span class="chip">moneyline</span>
       <input type="hidden" id="sr${item.id}_leg${idx}_bettype" value="moneyline">
       <input type="hidden" id="sr${item.id}_leg${idx}_line" value="">
       <input type="hidden" id="sr${item.id}_leg${idx}_stat" value="">
       <input type="hidden" id="sr${item.id}_leg${idx}_proptype" value="standard">`
      : `<input type="hidden" id="sr${item.id}_leg${idx}_bettype" value="prop">
         <input id="sr${item.id}_leg${idx}_line" type="number" step="0.5" value="${leg.line??''}" style="width:60px" placeholder="line">
         <select id="sr${item.id}_leg${idx}_stat">
          <option value="">stat…</option>
          ${SLIP_STAT_OPTIONS.map(([k,label])=>`<option value="${k}" ${leg.stat===k?'selected':''}>${label}</option>`).join('')}
         </select>
         <select id="sr${item.id}_leg${idx}_proptype">
          <option value="standard" ${leg.prop_type==='standard'?'selected':''}>standard</option>
          <option value="goblin" ${leg.prop_type==='goblin'?'selected':''}>goblin 😈</option>
          <option value="red_demon" ${leg.prop_type==='red_demon'?'selected':''}>red demon 🔴😈</option>
         </select>
         ${leg.stat_label_read && !leg.stat ? `<span class="m">(read as &ldquo;${esc(leg.stat_label_read)}&rdquo; — pick the closest stat above)</span>` : ''}`}
    </div>`).join('')}
   <div style="display:flex;gap:10px;align-items:center;margin-top:6px">
    <span class="m">Entry $</span><input id="sr${item.id}_stake" type="number" step="0.01" value="${item.stake??''}" style="width:80px">
    <span class="m">To win $</span><input id="sr${item.id}_payout" type="number" step="0.01" value="${item.potential_payout??''}" style="width:80px">
    <button class="btn-primary" onclick="confirmSlipReview(${item.id})">✅ confirm</button>
    <button onclick="rejectSlipReview(${item.id})">discard</button>
   </div>
  </div>`).join('');
}

function _readSlipLeg(itemId, idx){
 const g = k => document.getElementById(`sr${itemId}_leg${idx}_${k}`);
 return {
  player: g('player').value.trim(),
  bet_type: g('bettype').value,
  direction: g('direction').value,
  line: g('line').value ? parseFloat(g('line').value) : null,
  stat: g('stat').value || null,
  prop_type: g('proptype').value,
 };
}

async function confirmSlipReview(itemId){
 const item = (window.LAST_SLIP_REVIEW||[]).find(i=>i.id===itemId);
 if(!item) return;
 const legs = item.legs.map((_, idx) => _readSlipLeg(itemId, idx))
  .filter(l => l.player); // a leg the operator blanked out is dropped, not sent empty
 const stakeVal = document.getElementById(`sr${itemId}_stake`).value;
 const payoutVal = document.getElementById(`sr${itemId}_payout`).value;
 const r = await (await fetch('/api/slip_review_confirm', {method:'POST', body: JSON.stringify({
  id: itemId, legs, stake: stakeVal?parseFloat(stakeVal):null,
  potential_payout: payoutVal?parseFloat(payoutVal):null})})).json();
 if(!r.ok){ alert(r.error||'could not confirm'); return; }
 loadSlipReview();
}

async function rejectSlipReview(itemId){
 await fetch('/api/slip_review_reject', {method:'POST', body: JSON.stringify({id: itemId})});
 loadSlipReview();
}

function diagStage(err){const e=(err||'').toLowerCase();
 if(e.includes('screenshot')||e.includes('read')||e.includes('parse')||e.includes('image'))return 'INTAKE / vision-read';
 if(e.includes('gate 11')||e.includes('verify'))return 'RESEARCH / Gate 11';
 if(e.includes('v8')||e.includes('research'))return 'RESEARCH';
 if(e.includes('compliance'))return 'COMPLIANCE';
 if(e.includes('scoring')||e.includes('confidence'))return 'SCORING';
 return 'PIPELINE'}
async function retry(id){await fetch('/api/intake/retry',{method:'POST',body:JSON.stringify({id})});load()}
function tiles(s){const g=s.green,m=s.ml,sl=s.slips,c=s.cfg;return `<div class="strip">
 <div class="tile"><div class="t">GREEN TRACKER ${g.status}</div>
  <div class="v">${g.completed}/${g.goal} · ${g.hit==null?'n/a':pct(g.hit)} (${g.wins}-${g.losses})</div>
  ${bar(g.completed/g.goal)}<div class="m">strk ${g.streak} · best W${g.longest_w} · pend ${g.pending} · avg ${pct(g.avg_conf)}</div></div>
 <div class="tile"><div class="t">SLIP TRACKER</div>
  <div class="v">ROI ${(sl.roi*100).toFixed(0)}% · net $${sl.profit.toFixed(0)}</div>
  ${bar(sl.n/sl.goal)}<div class="m">${sl.n}/${sl.goal} · 6/6:${sl.six} 5/6:${sl.five} L:${sl.loss}</div></div>
 <div class="tile"><div class="t">MONEYLINE ${m.status}</div>
  <div class="v">${m.completed}/${m.goal} · ${m.hit==null?'n/a':pct(m.hit)} (${m.wins}-${m.losses})</div>
  ${bar(m.completed/m.goal)}<div class="m">strk ${m.streak} · best W${m.longest_w}</div></div>
 <div class="tile"><div class="t">BANKROLL</div>
  <div class="v">$${sl.bankroll.toLocaleString()} / $${c.target_bankroll.toLocaleString()}</div>
  ${bar(sl.bankroll/c.target_bankroll)}<div class="m">${(100*sl.bankroll/c.target_bankroll).toFixed(2)}% of goal</div></div></div>`}
function canvas(s){return `<div class="cols">`+Object.entries(s.canvas).map(([st,cards])=>
 `<div class="col"><h3 class="h">${st.toUpperCase()} <span style="float:right">${cards.length}</span></h3>`+
 cards.map(k=>`<div class="card ${k.color||''} ${k.state}" onclick="open_('${k.key}')">
  <b>${esc(k.player)}</b>${k.stalled?' <span class="bad">⚠stall</span>':''}${k.retries?` <span class="m">retry ${k.retries}</span>`:''}
  <div class="m">${k.line??''} ${esc(k.stat)} ${k.dir??''} ${k.conf!=null?pct(k.conf):''}</div></div>`).join('')+`</div>`).join('')+`</div>`}
function agentCanvas(s){
 const FLOW=[['research','Research agent'],['evaluation','Evaluation agent'],
  ['compliance','Compliance agent'],['scoring','Scoring agent'],['posting','Posting agent']];
 const agentByName={}; (s.agents||[]).forEach(a=>agentByName[a.agent]=a);
 const lf=s.live_flow||{};
 const statusChip=st=>st=='WORKING'?'ok':st=='IDLE'?'':st=='BLOCKED'||st=='FAILED'?'A':'';
 const flowBox=([key,label])=>{const a=agentByName[key]||{status:'OFFLINE',health:'⚪'};
  const picks=lf[key]||[];
  const known=(a.success_count||0)+(a.failure_count||0);
  return `<div class="card" style="cursor:default;margin-bottom:8px">
   <div style="display:flex;justify-content:space-between;align-items:center">
    <b>${label}</b>
    <span><span class="dot ${a.health=='🟢'?'g':a.health=='🔴'?'r':'y'}"></span>
     <span class="chip ${statusChip(a.status)}">${a.status}</span></span>
   </div>
   ${known?`<div class="m" style="margin-top:4px">${a.success_count} completed \u00b7 ${a.failure_count} failed${a.current_task?' \u00b7 on: '+esc(a.current_task):''}</div>`:''}
   ${picks.length?picks.slice(0,3).map(p=>`<div class="m" style="margin-top:4px">
    → ${esc(p.player)} ${esc(p.market||'')} ${p.direction||''}</div>`).join('')
    :'<div class="m" style="margin-top:4px">idle — nothing in flight</div>'}
  </div>${key!='posting'?'<div style="text-align:center;color:var(--dim);margin:2px 0">↓</div>':''}`};
 const ro=s.rule_overlay;
 const ruleChip=(label,val,cls)=>`<span class="chip ${cls||''}">${label} ${val}</span>`;
 const rules=ro?`<div class="panel" style="margin-top:8px">
  <div class="m" style="margin-bottom:6px">rule state — most recent pick: ${esc(ro.player||'—')}</div>
  ${ruleChip('Gate11',ro.gate11,ro.gate11=='PASSED'?'ok':'')}
  ${ruleChip('Gate11-B',ro.gate11b,'')}
  ${ruleChip('Rule24',ro.rule24,ro.rule24=='ACTIVE'?'A':'')}
  ${ruleChip('Rule30',ro.rule30,ro.rule30=='ACTIVE'?'A':'')}
  ${ruleChip('TruthLock',ro.truth_lock,ro.truth_lock=='PASS'?'ok':'')}
  ${ruleChip('Dominance',ro.dominance,'')}
  ${ruleChip('REQs',(ro.reqs??'—')+'/4','')}
  ${ruleChip('FeatureEcho',ro.feature_echo,ro.feature_echo=='DETECTED'?'A':'')}
  <span class="chip">LightGBM Challenger — NOT BUILT</span>
  </div>`:`<div class="panel" style="margin-top:8px"><span class="m">no evaluation history yet</span></div>`;
 const lp=s.learning_panel||{};
 const learningCard=`<div class="panel"><h3 class="h">LEARNING AGENT</h3>
  <span class="chip ${statusChip(lp.status)}">${lp.status}</span>
  <div class="m" style="margin-top:6px">task: ${esc(lp.current_task||'none')}</div>
  <div class="m">✓ ${lp.success_count||0} &nbsp; ✗ ${lp.failure_count||0}</div>
  <div class="m">pending proposals: ${lp.pending_proposals||0}</div>
  <div class="m">last proposal: ${esc(lp.last_proposal||'unknown')}</div>
  <div class="m" style="margin-top:6px;color:var(--dim)">discoveries + rule effectiveness: ${esc(lp.discoveries_note||'')}</div>
  </div>`;
 const vp=s.validation_panel||{}; const vr=vp.latest_run;
 const validationCard=`<div class="panel" style="margin-top:10px"><h3 class="h">VALIDATION AGENT</h3>
  <span class="chip ${statusChip(vp.status)}">${vp.status}</span>
  ${vr?`<div class="m" style="margin-top:6px">latest run: ${esc(vr.ts||'')}</div>
  <div class="m">preserved ${vr.preserved??0} · lost ${vr.lost??0} · new ${vr.created??0}</div>
  <div class="m">winner-demoted ${vr.win_demoted??0} · loser-promoted ${vr.lose_promoted??0}</div>`
  :'<div class="m" style="margin-top:6px">no validation runs recorded yet</div>'}
  <div class="m" style="margin-top:6px;color:var(--dim)">${esc(vp.live_tracking_note||'')}</div>
  </div>`;
 const online = (s.agents||[]).filter(a=>a.status==='WORKING'||a.status==='IDLE').length;
 const total = (s.agents||[]).length || FLOW.length;
 const topStrip = `<div class="strip" style="grid-template-columns:repeat(3,1fr)">
  <div class="tile"><div class="t">Active agents</div><div class="v">${online}/${total}</div></div>
  <div class="tile"><div class="t">Pending proposals</div><div class="v">${(s.learning_panel||{}).pending_proposals||0}</div></div>
  <div class="tile"><div class="t">System health</div><div class="v">${s.health_score}%</div></div>
 </div>`;
 const canvasIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">A live view of the five-agent evaluation pipeline (Research → Evaluation → Compliance → Scoring → Posting) plus the Learning and Validation agents that run alongside it. Each box below shows whether that agent is currently WORKING, IDLE, BLOCKED, or FAILED, and what it's done recently.</div>
  <div class="m" style="margin-top:6px">The <b>rule state</b> panel under the flow shows the gates and rules that applied to the most recent pick (Gate 11, Rule 24, Truth Lock, etc). This is a real-time snapshot, not a history — for the full record of any single pick, use Command Chat or Pick Results.</div>
 </div>`;
 document.getElementById('v-canvas').innerHTML= canvasIntro + topStrip +
  `<div class="row">
   <div>${FLOW.map(flowBox).join('')}${rules}</div>
   <div>${learningCard}${validationCard}</div>
  </div>`;
}
function confAudit(d){
 const a=d.aggregate||{}; const rows=d.recent||[]; const cal=d.calibration||{};
 if(!a.n){document.getElementById('v-confaudit').innerHTML=
  '<div class="panel"><span class="m">no scored evaluations yet — nothing to audit</span></div>';return}
 const gradeRows=(a.by_grade||[]).map(g=>`<tr><td>${esc(g.key)}</td><td>${g.n}</td>
  <td>${pct(g.avg_eval)}</td><td>${pct(g.avg_final)}</td>
  <td class="${g.avg_delta<0?'bad':''}">${g.avg_delta>=0?'+':''}${(g.avg_delta*100).toFixed(1)}pp</td></tr>`).join('');
 const compRows=(a.by_compliance||[]).map(g=>`<tr><td>${esc(g.key)}</td><td>${g.n}</td>
  <td>${pct(g.avg_eval)}</td><td>${pct(g.avg_final)}</td>
  <td class="${g.avg_delta<0?'bad':''}">${g.avg_delta>=0?'+':''}${(g.avg_delta*100).toFixed(1)}pp</td></tr>`).join('');
 const distMax=Math.max(1,...(a.distribution||[]).map(x=>x.n));
 const distSvgW=Math.max(280,(a.distribution||[]).length*46);
 const distBars = (a.distribution||[]).length ? `<svg width="${distSvgW}" height="140">
  ${(a.distribution||[]).map((x,i)=>{
    const bw=36, gap=10, bx=10+i*(bw+gap), bh=(x.n/distMax)*100, by=120-bh;
    return `<rect x="${bx}" y="${by}" width="${bw}" height="${bh}" rx="3" fill="var(--acc)"/>
     <text x="${bx+bw/2}" y="${by-4}" font-size="10" fill="var(--txt)" text-anchor="middle">${x.n}</text>
     <text x="${bx+bw/2}" y="134" font-size="9" fill="var(--dim)" text-anchor="middle">${esc(x.label)}</text>`;
  }).join('')}
 </svg>` : '<span class="m">no scored evaluations yet</span>';
 // Calibration: predicted confidence vs. real hit rate, per bucket.
 // gap = predicted minus real. Positive = overconfident, negative =
 // underconfident. No verdict rendered on what counts as "too far off"
 // -- that's a human judgment call once there's enough n to trust,
 // same discipline as everywhere else in this app.
 const calBuckets = cal.buckets || [];
 const calRows = calBuckets.map(b=>{
  if(!b.n) return `<tr><td>${esc(b.label)}</td><td class="m">0</td><td class="m">—</td><td class="m">—</td><td class="m">—</td></tr>`;
  const gapPp = (b.gap*100);
  const gapClass = Math.abs(gapPp) < 3 ? '' : (gapPp > 0 ? 'bad' : 'bad');
  const gapLabel = gapPp>=0 ? `+${gapPp.toFixed(1)}pp over` : `${gapPp.toFixed(1)}pp under`;
  return `<tr><td>${esc(b.label)}</td><td>${b.n}</td>
   <td>${pct(b.avg_predicted)}</td><td><b>${pct(b.real_hit_rate)}</b></td>
   <td class="${gapClass}">${gapLabel}</td></tr>`;
 }).join('');
 const calPanel = cal.total_settled ? `<div class="panel"><h3 class="h">🎯 CALIBRATION — predicted vs. real (${cal.total_settled} settled)</h3>
  <div class="m" style="margin-bottom:8px">Overall real hit rate: <b>${pct(cal.overall_hit_rate)}</b>. Per bucket: does a pick scored in this range actually hit at that rate? "over" means the system is more confident than results support; "under" means the opposite. Buckets with very few settled picks (n) aren't trustworthy yet — the point of this table is to watch it fill in over time, not to read it as final on day one.</div>
  <table><tr><th>CONFIDENCE RANGE</th><th>N SETTLED</th><th>AVG PREDICTED</th><th>REAL HIT RATE</th><th>GAP</th></tr>${calRows}</table>
  </div>` : `<div class="panel"><h3 class="h">🎯 CALIBRATION — predicted vs. real</h3>
  <div class="m">No settled picks yet (need real HIT/BUST outcomes recorded). This fills in automatically as picks resolve — nothing to run manually.</div></div>`;
 document.getElementById('v-confaudit').innerHTML=`
 <div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">This answers: <b>when the system says a pick is 70% confident, does it actually hit 70% of the time?</b> That alignment is called calibration. The data builds as you settle picks (mark HIT or BUST in Pick Results). More settled picks = more meaningful numbers.</div>
  <div class="m" style="margin-top:8px">
   <b>AVG NET ADJUSTMENT</b> — how much the final posted confidence differs from the model's raw number, after compliance and scoring run. Positive = system upgrades picks on balance; negative = downgrades.<br><br>
   <b>EMPTY-LEDGER DEFECTS</b> — picks where a card posted to Discord but no score record landed in Dataset A. A data-integrity flag, not a rule violation.<br><br>
   <b>GRADE (GREEN / YELLOW)</b> — GREEN = met all high-confidence thresholds; YELLOW = met a lower bar. AVG Δ = how much scoring moved the confidence from the model's initial number.<br><br>
   <b>COMPLIANCE CATEGORY A / B</b> — A flags are rule violations that reduce the final confidence score. B flags are wording/minor issues that get noted but don't move the number.
  </div>
 </div>
 <div class="strip">
  <div class="tile"><div class="t">SCORED EVALUATIONS</div><div class="v">${a.n}</div></div>
  <div class="tile"><div class="t">AVG NET ADJUSTMENT</div><div class="v">${a.avg_delta>=0?'+':''}${(a.avg_delta*100).toFixed(1)}pp</div></div>
  <div class="tile"><div class="t">EMPTY-LEDGER DEFECTS</div><div class="v ${a.defect_count?'bad':''}">${a.defect_count} <span class="m">(${pct(a.defect_rate)})</span></div></div>
  <div class="tile"><div class="t">HIGH-SEVERITY</div><div class="v">${a.defect_count?'⚠ review needed':'✓ none detected'}</div></div>
 </div>
 ${calPanel}
 <div class="row">
  <div><div class="panel"><h3 class="h">EVAL → FINAL CONFIDENCE BY GRADE</h3>
   <div class="m" style="margin-bottom:6px">AVG EVAL = model's raw score · AVG FINAL = after all adjustments · AVG Δ = net change in percentage points</div>
   <table><tr><th>GRADE</th><th>N</th><th>AVG EVAL</th><th>AVG FINAL</th><th>AVG Δ</th></tr>${gradeRows}</table></div>
   <div class="panel" style="margin-top:10px"><h3 class="h">BY COMPLIANCE CATEGORY</h3>
   <div class="m" style="margin-bottom:6px">A = rule violation, reduces score · B = minor flag, no score impact · blank = fully approved</div>
   <table><tr><th>CATEGORY</th><th>N</th><th>AVG EVAL</th><th>AVG FINAL</th><th>AVG Δ</th></tr>${compRows}</table></div>
   <div class="panel" style="margin-top:10px"><h3 class="h">FINAL CONFIDENCE DISTRIBUTION</h3>
   <div class="m" style="margin-bottom:6px">How many picks landed in each confidence range after all adjustments. Healthy clustering is 65–80%.</div>
   ${distBars}</div></div>
  <div><div class="panel"><h3 class="h">RECENT SCORED PICKS — click for full flow</h3>
   ${rows.map(r=>`<div class="card ${r.final_grade||''} ${r.ledger_empty_flag?'failed':''}" onclick="openEval('${r.eval_id}')">
    <b>${esc(r.player)}</b> ${r.line??''} ${esc(r.market||'')}
    ${r.ledger_empty_flag?'<span class="bad">⚠ empty ledger</span>':''}
    <div class="m">${pct(r.eval_confidence)} → ${pct(r.final_confidence)} ${gradeEmoji(r.final_grade)} ${esc(r.final_grade||'')} · ${fmtOdds(impliedOdds(r.final_confidence))}</div></div>`).join('')
    ||'<span class="m">none yet</span>'}</div></div>
 </div>`}
function learningCC(d){
 const p=d.panel||{}; const props=d.proposals||{}; const appr=d.approvals||[]; const sig=d.signals||[];
 const statusChip=st=>st=='WORKING'?'ok':st=='IDLE'?'':st=='BLOCKED'||st=='FAILED'?'A':'';
 const c=props.counts||{PENDING:0,APPROVED:0,REJECTED:0};
 const propRows=(props.proposals||[]).map(x=>`<tr><td>#${x.id}</td><td>${esc(x.signal)}</td>
  <td>${x.current} → ${x.proposed}</td>
  <td><span class="chip ${x.status=='APPROVED'?'ok':x.status=='REJECTED'?'A':''}">${x.status}</span></td>
  <td class="m">${esc(x.batch_date||'')}</td></tr>`).join('');
 const apprRows=appr.map(a=>`<tr><td class="m">${esc(a.date||'')}</td><td>${esc(a.signal)}</td>
  <td>${a.current} → ${a.proposed}</td>
  <td><span class="chip ${a.decision=='APPROVED'?'ok':'A'}">${esc(a.decision)}</span></td>
  <td class="m">${esc(a.decided_at||'')}</td></tr>`).join('');
 const sigRows=sig.map(s=>`<tr><td>${esc(s.signal)}</td><td class="m">${s.kind}</td><td>${s.n}</td>
  <td>${pct(s.hit_rate)}</td>
  <td><span class="chip ${s.trend=='IMPROVING'?'ok':s.trend=='DEGRADING'?'A':''}">${s.trend}</span></td></tr>`).join('');
 document.getElementById('v-learningcc').innerHTML=`
 <div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">The Learning Center tracks how the system improves its own weights over time. When enough picks settle (HIT or BUST), the system's Learning Agent proposes adjustments to the evaluation weights. You review and approve or reject each proposal here before anything changes. <b>Nothing adjusts automatically without your approval.</b></div>
  <div class="m" style="margin-top:6px"><b>PENDING PROPOSALS</b> — weight changes the agent has flagged as worth reviewing based on real outcome patterns. <b>APPROVED</b> — changes you accepted. <b>SIGNAL PERFORMANCE</b> — whether each signal that drives weight proposals is trending better or worse over settled picks.</div>
 </div>
 <div class="strip">
  <div class="tile"><div class="t">LEARNING AGENT</div>
   <div class="v"><span class="chip ${statusChip(p.status)}">${p.status||'OFFLINE'}</span></div>
   <div class="m">✓${p.success_count||0} ✗${p.failure_count||0}</div></div>
  <div class="tile"><div class="t">PENDING PROPOSALS</div><div class="v">${c.PENDING||0}</div></div>
  <div class="tile"><div class="t">APPROVED</div><div class="v">${c.APPROVED||0}</div></div>
  <div class="tile"><div class="t">REJECTED</div><div class="v">${c.REJECTED||0}</div></div>
 </div>
 <div class="row">
  <div>
   <div class="panel"><h3 class="h">PROPOSAL ACTIVITY (${props.total||0} total)</h3>
    <table><tr><th>ID</th><th>SIGNAL</th><th>CURRENT→PROPOSED</th><th>STATUS</th><th>BATCH</th></tr>
    ${propRows||'<tr><td colspan="5" class="m">no proposals yet</td></tr>'}</table></div>
   <div class="panel" style="margin-top:10px"><h3 class="h">SIGNAL PERFORMANCE — improving/degrading</h3>
    <table><tr><th>SIGNAL</th><th>TYPE</th><th>N</th><th>HIT RATE</th><th>TREND</th></tr>
    ${sigRows||'<tr><td colspan="5" class="m">no signal history yet (GREEN-only pool)</td></tr>'}</table></div>
  </div>
  <div>
   <div class="panel"><h3 class="h">APPROVAL LOG</h3>
    <table><tr><th>DATE</th><th>SIGNAL</th><th>CHANGE</th><th>DECISION</th></tr>
    ${apprRows||'<tr><td colspan="4" class="m">no decisions logged yet</td></tr>'}</table></div>
   <div class="panel" style="margin-top:10px;opacity:0.7"><h3 class="h">RECENT DISCOVERIES
    <span class="chip A" style="float:right">NOT YET CAPTURED</span></h3>
    <div class="m">rule_discovery_check() finds real patterns but only posts them to Discord —
    zero structured storage today. Will populate once discovery capture is approved and wired.</div></div>
  </div>
 </div>`}
function warehouseCCView(d, runs, players, triage){
 const snap = d.snapshot||{}; const counts = snap.counts||{};
 const rc = d.readiness||{}; const cs = d.collection||{};
 const sq = d.source_quality||{}; const fhData = d.feature_health||{};
 const fh = fhData.features||[]; const q = d.quality||{};
 const elo = d.elo||{}; const eloByTour = elo.by_tour||[];
 const yc = (d.year_coverage||{}).years||[];
 const mc = d.match_coverage||{}; const trends = (d.trends||{}).daily||[];
 const hs = d.health_score||0; const hb = snap.heartbeat||{};

 // Format a UTC ISO timestamp to readable local time
 const fmtDt = s => {
  if(!s) return '—';
  try{ return new Date(s).toLocaleString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }
  catch(e){ return s.slice(0,16).replace('T',' '); }
 };
 const dur = s => s==null ? '—' : s < 60 ? s+'s' : Math.floor(s/60)+'m '+Math.round(s%60)+'s';

 // Collector run feed — the main feature requested
 const runFeed = runs.map(r=>{
  let errs = [];
  let warns = [];
  try{ errs = typeof r.api_errors === 'string' ? JSON.parse(r.api_errors||'[]') : (r.api_errors||[]); }catch(e){}
  try{ warns = typeof r.warnings === 'string' ? JSON.parse(r.warnings||'[]') : (r.warnings||[]); }catch(e){}
  const statusColor = r.status==='SUCCESS'?'ok':r.status==='FAILED'?'A':'';
  const errHtml = errs.length
   ? `<div class="m bad" style="margin-top:4px">⚠ ${errs.length} error(s): ${errs.slice(0,3).map(e=>esc(String(e))).join(' · ')}${errs.length>3?` … +${errs.length-3} more`:''}</div>` : '';
  const warnHtml = warns.length
   ? `<div class="m" style="margin-top:2px;color:#f5c542">⚑ ${warns.length} warning(s): ${warns.slice(0,2).map(w=>esc(String(w))).join(' · ')}${warns.length>2?` … +${warns.length-2} more`:''}</div>` : '';
  return `<div class="card" style="margin-bottom:8px;cursor:default">
   <div style="display:flex;justify-content:space-between;align-items:center">
    <span class="m" style="font-size:11px">${fmtDt(r.started_at)}</span>
    <span class="chip ${statusColor}">${esc(r.status||'?')}</span>
   </div>
   <div style="display:flex;gap:16px;margin-top:6px;flex-wrap:wrap">
    <div><div class="m">ADDED</div><div style="font-size:18px;font-weight:600;color:var(--grn)">${r.rows_added??0}</div></div>
    <div><div class="m">UPDATED</div><div style="font-size:18px;font-weight:600">${r.rows_updated??0}</div></div>
    <div><div class="m">FAILED</div><div style="font-size:18px;font-weight:600;color:${r.rows_failed?'var(--red)':'inherit'}">${r.rows_failed??0}</div></div>
    <div><div class="m">DURATION</div><div style="font-size:18px;font-weight:600">${dur(r.duration_sec)}</div></div>
    <div><div class="m">FINISHED</div><div class="m" style="padding-top:4px">${fmtDt(r.finished_at)}</div></div>
    ${r.warehouse_size_after!=null?`<div><div class="m">DB SIZE</div><div class="m" style="padding-top:4px">${r.warehouse_size_after.toLocaleString()} rows</div></div>`:''}
   </div>
   ${errHtml}${warnHtml}
  </div>`;
 }).join('') || '<div class="m">No runs recorded yet.</div>';

 // Heartbeat status
 const hbStatus = hb.status||'OFFLINE';
 const hbColor = hbStatus==='IDLE'?'ok':hbStatus==='WORKING'?'ok':hbStatus==='FAILED'?'A':'';
 const hbLastSeen = hb.last_heartbeat ? fmtDt(hb.last_heartbeat) : 'never';

 // Feature health table
 const featRows = fh.map(f=>`<tr>
  <td>${esc(f.feature_name||'')}</td>
  <td>${f.population_pct!=null?f.population_pct.toFixed(1)+'%':'—'}</td>
  <td>${f.players_covered??'—'}</td>
  <td class="m">${f.missing_pct!=null?f.missing_pct.toFixed(1)+'% missing':'—'}</td>
  <td><span class="chip ${f.ready_for_ml?'ok':'A'}">${f.ready_for_ml?'READY':'NOT YET'}</span></td></tr>`).join('');

 const whccIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen? — Dataset B (Tennis Match Warehouse)</h3>
  <div class="m">This is <b>Dataset B</b> — the historical tennis match database (<code>tennis_warehouse.sqlite</code>) built by the daily collector. It's real match results, player stats, and coverage data used as feature input for evaluations and future model training. This is not evaluation history — for that (Dataset A: your evaluation record), go to <b>Data Warehouse</b> in the nav, or see the Dataset B strip on <b>ML Readiness</b>.</div>
  <div class="m" style="margin-top:6px">The Collector Run Feed below shows every collection run with real added/updated/failed counts. Feature Health shows which computed features have enough population to be model-ready.</div>
 </div>`;
 document.getElementById('v-warehousecc').innerHTML = whccIntro + `
 <div class="strip">
  <div class="tile"><div class="t">MATCHES</div><div class="v">${(counts.matches||0).toLocaleString()}</div></div>
  <div class="tile"><div class="t">PLAYERS</div><div class="v">${(counts.players||0).toLocaleString()}</div></div>
  <div class="tile"><div class="t">STAT ROWS</div><div class="v">${(counts.player_year_stats||0).toLocaleString()}</div></div>
  <div class="tile"><div class="t">COLLECTOR RUNS</div><div class="v">${(counts.collector_runs||0).toLocaleString()}</div></div>
  <div class="tile"><div class="t">HEALTH</div><div class="v" style="color:${hs>=80?'var(--grn)':hs>=50?'#f5c542':'var(--red)'}">${hs}%</div></div>
 </div>

 <div class="panel" style="margin-top:10px">
  <h3 class="h">COLLECTOR STATUS
   <span class="chip ${hbColor}" style="margin-left:8px">${hbStatus}</span>
   <button onclick="runCollectorNow()" style="float:right;background:var(--acc)">▶ Run Now</button>
  </h3>
  <div class="m" id="runNowStatus">Runs automatically daily at 06:00 AM CT · Last heartbeat: ${hbLastSeen} · ${hb.success_count??0} successes · ${hb.failure_count??0} failures</div>
  <div class="m" style="margin-top:4px">Data range: ${esc(rc.match_date_min||'—')} → ${esc(rc.match_date_max||'—')} ·
   DB size: ${snap.size_mb||'?'} MB ·
   ${rc.runs_success||0} full / ${rc.runs_partial||0} partial / ${rc.runs_failed||0} failed runs</div>
 </div>

 <div class="panel" style="margin-top:10px">
  <h3 class="h">WARNING TRIAGE <span class="m" style="font-weight:400">(new vs. already-known)</span></h3>
  <div class="m" style="margin-bottom:8px">A repeated warning (same player, same reason, day after day) is usually a permanent, already-understood condition — most often an injured or inactive player with no current-season matches to write, which is the collector correctly refusing to write stale data, not a bug. This splits that out from anything genuinely new, so a real new problem doesn't get lost in 48 copies of an old one.</div>
  ${triage && triage.new_today && triage.new_today.length ? `
   <div class="m" style="margin-bottom:4px"><b>🆕 New today (${triage.new_today.length}) — worth reading:</b></div>
   ${triage.new_today.map(w=>`<div class="m bad" style="margin:2px 0">⚠ ${esc(w)}</div>`).join('')}
  ` : '<div class="m" style="margin-bottom:8px"><span class="chip ok">0 new warnings today</span></div>'}
  ${triage && triage.recurring_known && triage.recurring_known.length ? `
   <div class="m" style="margin:8px 0 4px"><b>🔁 Recurring across all ${triage.runs_analyzed||'—'} runs analyzed (${triage.recurring_known.length}) — likely known, not urgent:</b></div>
   ${triage.recurring_known.map(w=>`<div class="m" style="margin:2px 0;opacity:0.75">${esc(w)}</div>`).join('')}
  ` : ''}
 </div>

 <div class="panel" style="margin-top:10px">
  <h3 class="h">COLLECTOR RUN FEED (most recent first)</h3>
  ${runFeed}
 </div>

 <div class="panel" style="margin-top:10px"><h3 class="h">MATCHES BY SURFACE</h3>
  <div style="display:flex;gap:16px;flex-wrap:wrap">
  ${(mc.by_surface||[]).map(s=>`<div><div class="m">${esc(s.surface||'unknown').toUpperCase()}</div><div style="font-size:22px;font-weight:600">${s.n}</div></div>`).join('')||'<span class="m">none yet</span>'}
  </div>
 </div>

 <div class="panel" style="margin-top:10px"><h3 class="h">MATCHES BY TOUR</h3>
  <div style="display:flex;gap:24px">
  ${(mc.by_tour||[]).map(t=>`<div><div class="m">${esc(t.tour||'unknown')}</div><div style="font-size:22px;font-weight:600">${t.n}</div></div>`).join('')||'<span class="m">none yet</span>'}
  </div>
 </div>

 <div class="panel" style="margin-top:10px"><h3 class="h">FEATURE HEALTH</h3>
  ${fh.length?`<table><tr><th>FEATURE</th><th>COVERAGE</th><th>PLAYERS</th><th>MISSING</th><th>ML READY</th></tr>${featRows}</table>`:'<span class="m">computing…</span>'}
 </div>

 <div class="panel" style="margin-top:10px">
  <h3 class="h">📖 What is this? — ELO ratings</h3>
  <div class="m">A skill rating per player, computed from real historical match results (Dataset B's <code>matches</code> table, replayed chronologically). Overall + surface-specific (hard/clay/grass), kept separate rather than blended. This is <b>signal 6</b> — not yet live in evaluations. Same as every other signal, it has to clear a real predictive backtest first before it touches a pick.</div>
 </div>

 <div class="strip" style="margin-top:10px">
  <div class="tile"><div class="t">MATCH ROWS</div><div class="v">${(elo.match_rows||0).toLocaleString()}</div></div>
  <div class="tile"><div class="t">PLAYERS RATED</div><div class="v">${(elo.players_rated||0).toLocaleString()}</div></div>
  <div class="tile"><div class="t">COVERAGE</div><div class="v" style="color:${(elo.coverage_pct||0)>=80?'var(--grn)':(elo.coverage_pct||0)>=50?'#f5c542':'var(--red)'}">${(elo.coverage_pct||0).toFixed(1)}%</div></div>
  <div class="tile"><div class="t">BACKTEST AUC</div><div class="v" style="color:var(--dim2)">— <span style="font-size:11px">not run yet</span></div></div>
 </div>

 <div class="panel" style="margin-top:10px"><h3 class="h">ELO COVERAGE BY TOUR</h3>
  ${eloByTour.length?`<table><tr><th>TOUR</th><th>PLAYERS</th><th>HARD</th><th>CLAY</th><th>GRASS</th></tr>${eloByTour.map(t=>`<tr>
   <td>${esc(t.tour||'unknown')}</td>
   <td>${(t.players||0).toLocaleString()}</td>
   <td>${(t.hard_covered||0).toLocaleString()}</td>
   <td>${(t.clay_covered||0).toLocaleString()}</td>
   <td>${(t.grass_covered||0).toLocaleString()}</td>
  </tr>`).join('')}</table>`:'<span class="m">not built yet — run rebuild_elo_ratings() first</span>'}
  <div class="m" style="margin-top:6px">${elo.last_updated?`Last rebuilt: ${esc(elo.last_updated)}`:'Never rebuilt.'}</div>
 </div>

 <div class="panel" style="margin-top:10px"><h3 class="h">DATA QUALITY</h3>
  <div style="display:flex;gap:24px;flex-wrap:wrap">
   <div><div class="m">TOTAL MATCHES</div><div style="font-size:20px;font-weight:600">${(q.total_matches||0).toLocaleString()}</div></div>
   <div><div class="m">COMPLETENESS</div><div style="font-size:20px;font-weight:600;color:${(q.completeness_pct||0)>=90?'var(--grn)':'#f5c542'}">${(q.completeness_pct||0).toFixed(1)}%</div></div>
   <div><div class="m">MISSING SCORE</div><div style="font-size:20px;font-weight:600;color:${(q.missing_score||0)?'var(--red)':'inherit'}">${q.missing_score||0}</div></div>
   <div><div class="m">MISSING WINNER</div><div style="font-size:20px;font-weight:600;color:${(q.missing_winner||0)?'#f5c542':'inherit'}">${q.missing_winner||0}</div></div>
  </div>
 </div>

 <div class="panel" style="margin-top:10px"><h3 class="h">YEAR COVERAGE</h3>
  <table><tr><th>YEAR</th><th>MATCHES</th></tr>
  ${yc.map(y=>`<tr><td>${y.year}</td><td>${y.matches}</td></tr>`).join('')||'<tr><td colspan="2" class="m">none yet</td></tr>'}
  </table>
 </div>

 <div class="panel" style="margin-top:10px"><h3 class="h">COLLECTION TRENDS (last 14 days)</h3>
  <table><tr><th>DATE</th><th>ADDED</th><th>UPDATED</th><th>STATUS</th></tr>
  ${trends.map(t=>`<tr>
   <td>${esc(t.date||'')}</td>
   <td style="color:var(--grn)">${t.added||0}</td>
   <td>${t.updated||0}</td>
   <td><span class="chip ${t.status==='SUCCESS'?'ok':t.status==='FAILED'?'A':''}">${esc(t.status||'PARTIAL')}</span></td></tr>`).join('')||'<tr><td colspan="4" class="m">no data yet</td></tr>'}
  </table>
 </div>

 <div class="panel" style="margin-top:10px"><h3 class="h">DATA SOURCE QUALITY</h3>
  <div style="display:flex;gap:16px;flex-wrap:wrap">
   <div><div class="m">TOTAL PLAYERS</div><div style="font-size:20px;font-weight:600">${sq.total_players||0}</div></div>
   <div><div class="m">FULL STATS</div><div style="font-size:20px;font-weight:600;color:var(--grn)">${sq.full_stats_players||0}</div></div>
   <div><div class="m">PARTIAL ONLY</div><div style="font-size:20px;font-weight:600;color:#f5c542">${sq.partial_players||0}</div></div>
  </div>
  <div style="margin-top:8px">${(sq.source_breakdown||[]).map(b=>`<span class="chip ${b.data_source==='AGGREGATE'?'ok':'A'}">${esc(b.data_source)}: ${b.n}</span>`).join(' ')}</div>
  <div class="m" style="margin-top:6px">FALLBACK_PARTIAL rows never fabricate hold%/break%/bp_save% — those stay NULL by design when the vendor aggregate endpoint lags.</div>
 </div>

 <div class="panel" style="margin-top:10px"><h3 class="h">TOP PLAYERS BY MATCH COUNT</h3>
  <div style="display:flex;gap:8px;flex-wrap:wrap">
  ${(mc.top_players||[]).slice(0,20).map(p=>`<span class="chip" style="cursor:pointer" onclick="showWarehousePlayerDetail('${esc(p.player)}')">${esc(p.player)} ${p.n}</span>`).join('')||'<span class="m">none yet</span>'}
  </div>
 </div>

 <div class="panel" style="margin-top:10px"><h3 class="h">PLAYER DETAIL — click a name above or from the list</h3>
  <table><tr><th>PLAYER</th><th>TOUR</th><th>RANK</th><th>YEARS ON FILE</th><th>MATCHES</th></tr>
  ${players.slice(0,30).map(p=>`<tr style="cursor:pointer" onclick="showWarehousePlayerDetail('${esc(p.player)}')">
   <td>${esc(p.player)}</td><td>${esc(p.tour||'—')}</td><td>${p.current_ranking??'—'}</td>
   <td>${p.years_on_file??0}</td><td>${p.match_count??0}</td></tr>`).join('')||'<tr><td colspan="5" class="m">none yet</td></tr>'}
  </table>
 </div>
 <div id="wcc-player-detail" class="panel" style="margin-top:10px;display:none"></div>`}
function warehouseView(w, players, audit, rules){
 const suf = w.sufficiency||{}; const snap = w.snapshot||{};
 const marketRows = (w.markets||[]).map((m,i)=>{
  if(m.blocked) return `<tr><td>${i+1}</td><td>${esc(m.market)}</td><td colspan="6"><span class="chip A">BLOCKED BY DESIGN</span> <span class="m">${esc(m.blocked_reason||'')}</span></td></tr>`;
  return `<tr><td>${i+1}</td><td>${esc(m.market)}</td><td>${m.count}</td><td>${m.with_outcome}</td>
   <td>${pct(m.outcome_completion_pct)}</td><td>${m.readiness_pct}%</td><td>${esc(m.tier)}</td>
   <td class="m">${m.players_covered} players</td></tr>`;
 }).join('');
 const surfRows=(w.surfaces||[]).map(s=>`<tr><td>${esc(s.surface)}</td><td>${s.n}</td><td>${pct(s.pct)}</td></tr>`).join('');
 const tourRows=(w.tours||[]).map(t=>`<tr><td>${esc(t.tour)}</td><td>${t.n}</td><td>${pct(t.pct)}</td></tr>`).join('');
 const outcomeRows=((w.outcomes||{}).by_outcome||[]).map(o=>`<tr><td>${esc(o.bucket)}</td><td>${o.n}</td><td>${pct(o.pct)}</td></tr>`).join('');
 const problemRows=(w.problems||[]).map((p,i)=>`<tr><td>${i+1}</td><td>${esc(p.problem)}</td><td>${p.count}</td><td>${pct(p.pct)}</td></tr>`).join('');
 const q = w.quality||{};
 const forecast = w.forecast||{};
 const playerRows = players.map(p=>`<tr style="cursor:pointer" onclick="showPlayerDetail('${esc(p.player)}')">
  <td>${esc(p.player)}</td><td>${p.rows_n}</td><td>${p.markets}</td><td>${p.wins}</td><td>${p.losses}</td>
  <td>${p.pending}</td><td>${pct(p.outcome_completion_pct)}</td></tr>`).join('');
 const fieldRows = (audit.fields||[]).map(f=>`<tr class="${f.flagged_for_review?'bad':''}">
  <td>${esc(f.field)}</td><td>${f.populated}</td><td>${f.null}</td><td>${pct(f.population_pct)}</td>
  <td>${f.flagged_for_review?'⚠ review':''}</td></tr>`).join('');
 const ruleActRows = (rules.rule_activation||[]).map(r=>`<tr><td>${esc(r.rule)}</td><td>${r.activation_count}</td>
  <td>${r.known_outcomes}</td><td>${r.hit_rate==null?'n/a':pct(r.hit_rate)}</td>
  <td class="m">${esc(r.last_activation||'—')}</td></tr>`).join('');
 const cc = rules.confidence_conservation||{}; const fe = rules.feature_echo||{};

 document.getElementById('v-warehouse').innerHTML = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen? — Dataset A (Evaluation History)</h3>
  <div class="m">This shows everything OpenClaw has <b>evaluated</b> — the record of every prop and moneyline run through the full pipeline. How many picks evaluated, which markets, which surfaces, and how many have real HIT/BUST outcomes recorded. This is <b>Dataset A.</b></div>
  <div class="m" style="margin-top:6px">For the tennis match database (Dataset B — 1,812 matches, 199 players from the daily V8 collector), go to <b>Warehouse Command</b> in the nav. Dataset A = your evaluation history. Dataset B = historical tennis match data used for research.</div>
 </div>
 <div class="panel" style="border-color:var(--dim)"><h3 class="h">DATA SUFFICIENCY</h3>
  <span class="m">Evaluations: ${suf.total_evaluations} · Outcomes: ${suf.total_outcomes} ·
  Completion: ${pct(suf.outcome_completion_pct)}</span><br>
  <span class="chip A" style="margin-top:6px">Rule Audits: ${esc(suf.rule_audit_status)}</span></div>

 <div class="strip" style="margin-top:10px">
  <div class="tile"><div class="t">TOTAL EVALUATIONS</div><div class="v">${snap.total_evaluations||0}</div></div>
  <div class="tile"><div class="t">PLAYERS COVERED</div><div class="v">${snap.total_players||0}</div></div>
  <div class="tile"><div class="t">MARKETS COVERED</div><div class="v">${snap.total_markets||0}</div></div>
  <div class="tile"><div class="t">DATA QUALITY</div><div class="v">${pct(snap.data_quality_pct)}</div></div>
 </div>
 <div class="panel" style="margin-top:10px"><h3 class="h">KNOWLEDGE SNAPSHOT</h3>
  <table>
  <tr><td class="m">Props / Moneylines</td><td>${snap.total_props||0} / ${snap.total_moneylines||0}</td></tr>
  <tr><td class="m">Outcomes / Pending</td><td>${snap.total_outcomes||0} / ${snap.total_pending||0}</td></tr>
  <tr><td class="m">NO_PLAY / Failed</td><td>${snap.total_no_play||0} / ${snap.total_failed||0}</td></tr>
  <tr><td class="m">Validation / Learning records</td><td>${snap.total_validation_records||0} / ${snap.total_learning_records||0}</td></tr>
  <tr><td class="m">Growth today / week / month</td><td>${snap.growth_today||0} / ${snap.growth_week||0} / ${snap.growth_month||0}</td></tr>
  </table></div>

 <div class="panel" style="margin-top:10px"><h3 class="h">MARKET COVERAGE — ranked most→least collected</h3>
  <table><tr><th>#</th><th>MARKET</th><th>ROWS</th><th>W/ OUTCOME</th><th>COMPLETION</th><th>READINESS</th><th>TIER</th><th>COVERAGE</th></tr>
  ${marketRows}</table></div>

 <div class="row" style="margin-top:10px">
  <div class="panel"><h3 class="h">SURFACE COVERAGE</h3><table><tr><th>SURFACE</th><th>N</th><th>%</th></tr>${surfRows||'<tr><td colspan="3" class="m">none yet</td></tr>'}</table></div>
  <div class="panel"><h3 class="h">TOUR COVERAGE</h3><table><tr><th>TOUR</th><th>N</th><th>%</th></tr>${tourRows||'<tr><td colspan="3" class="m">none yet</td></tr>'}</table></div>
 </div>

 <div class="panel" style="margin-top:10px"><h3 class="h">TIME DISTRIBUTION</h3>
  <span class="m">Today: ${w.time.today} · Week: ${w.time.week} · Month: ${w.time.month} · Last 90d: ${w.time.last_90d} · Avg/day (30d): ${w.time.avg_per_day_30d}</span></div>

 <div class="panel" style="margin-top:10px"><h3 class="h">OUTCOME COVERAGE</h3>
  <table><tr><th>OUTCOME</th><th>N</th><th>%</th></tr>${outcomeRows||'<tr><td colspan="3" class="m">none yet</td></tr>'}</table></div>

 <div class="panel" style="margin-top:10px"><h3 class="h">TOP DATA PROBLEMS</h3>
  <table><tr><th>#</th><th>PROBLEM</th><th>COUNT</th><th>%</th></tr>${problemRows||'<tr><td colspan="4" class="m">none detected</td></tr>'}</table></div>

 <div class="panel" style="margin-top:10px"><h3 class="h">DATA QUALITY CENTER</h3>
  <table>
  <tr><td class="m">Missing opponent</td><td>${q.missing_opponent?.count??0} (${pct(q.missing_opponent?.pct)})</td></tr>
  <tr><td class="m">Missing surface</td><td>${q.missing_surface?.count??0} (${pct(q.missing_surface?.pct)})</td></tr>
  <tr><td class="m">Missing outcome (stale posted)</td><td>${q.missing_outcome?.count??0} (${pct(q.missing_outcome?.pct)})</td></tr>
  <tr><td class="m">Missing market</td><td>${q.missing_market?.count??0} (${pct(q.missing_market?.pct)})</td></tr>
  <tr><td class="m">Stalled evaluations</td><td>${q.stalled_evaluations?.count??0} (${pct(q.stalled_evaluations?.pct)})</td></tr>
  <tr><td class="m">Duplicate-like records</td><td>${q.duplicate_like?.count??0} (${q.duplicate_like?.groups??0} groups)</td></tr>
  </table></div>

 <div class="panel" style="margin-top:10px"><h3 class="h">ML TRAINING FORECAST</h3>
  ${forecast.available?`<table><tr><th>MARKET</th><th>CURRENT</th><th>TARGET</th><th>PER DAY</th><th>EST. DAYS</th></tr>
  ${forecast.forecasts.map(f=>`<tr><td>${esc(f.market)}</td><td>${f.current}</td><td>${f.target??'—'}</td>
  <td>${f.per_day}</td><td>${f.days_to_target??'—'}</td></tr>`).join('')}</table>`
  :`<span class="m">${esc(forecast.reason||'No meaningful forecast available yet.')}
  (${forecast.days_of_history||0}/${forecast.days_required||14} days of history)</span>`}</div>

 <div class="panel" style="margin-top:10px"><h3 class="h">PLAYER COVERAGE — click a player for detail</h3>
  <table><tr><th>PLAYER</th><th>ROWS</th><th>MARKETS</th><th>WINS</th><th>LOSSES</th><th>PENDING</th><th>COMPLETION</th></tr>
  ${playerRows||'<tr><td colspan="7" class="m">no players yet</td></tr>'}</table></div>
 <div id="player-detail-panel" class="panel" style="margin-top:10px;display:none"></div>

 <div class="panel" style="margin-top:10px"><h3 class="h">DATASET A COVERAGE AUDIT — ${audit.total} rows, ${(audit.fields||[]).length} fields</h3>
  <table><tr><th>FIELD</th><th>POPULATED</th><th>NULL</th><th>%</th><th></th></tr>${fieldRows}</table></div>

 <div class="panel" style="margin-top:10px"><h3 class="h">RULE AUDIT INFRASTRUCTURE — raw counts only, no verdicts</h3>
  <table><tr><th>RULE</th><th>ACTIVATIONS</th><th>KNOWN OUTCOMES</th><th>HIT RATE</th><th>LAST FIRED</th></tr>${ruleActRows}</table>
  <div class="m" style="margin-top:8px">Confidence conservation: ${cc.scored_evaluations||0} scored,
  ${cc.empty_ledger_defects||0} empty-ledger defects (${pct(cc.defect_rate)})</div>
  <div class="m">Feature Echo: ${fe.occurrences||0} of ${fe.scored_total||0} scored (${pct(fe.rate)})</div>
  <div class="m">Rule24 + Rule30 co-occurrence: ${rules.rule24_and_rule30_cooccurrence||0}</div>
  <div class="m" style="margin-top:6px">Classification: ${(rules.classification_distribution||[]).map(c=>`${esc(c.classification)}: ${c.n}`).join(' · ')||'none yet'}</div>
  <div class="m">Dominance: ${(rules.dominance_distribution||[]).map(d=>`${esc(d.dominance_level)}: ${d.n}`).join(' · ')||'none yet'}</div>
 </div>`;
}
function mlReady(r){
 const markets = r.markets||[];
 const card = m=>{
  if(m.blocked){
   return `<div class="card" style="cursor:default;opacity:0.7">
    <div style="display:flex;justify-content:space-between"><b>${esc(m.market)}</b>
    <span class="chip A">BLOCKED BY DESIGN</span></div>
    <div class="m" style="margin-top:6px">${esc(m.blocked_reason||'')}</div></div>`;
  }
  return `<div class="card" style="cursor:default;display:flex;gap:12px;align-items:center">
   ${m.next_threshold?ringGauge(m.readiness_pct, 64):`<div style="width:64px;text-align:center" class="m">n/a</div>`}
   <div style="flex:1">
    <div style="display:flex;justify-content:space-between"><b>${esc(m.market)}</b>
    <span class="chip">${esc(m.tier)}</span></div>
    <div class="m" style="margin-top:4px">${m.count}${m.next_threshold?' / '+m.next_threshold:''}</div>
    <div class="m" style="margin-top:4px">Model: <span class="chip">${esc(m.model_status)}</span></div>
   </div></div>`;
 };
 const mlIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">Tracks how close each market is to having enough data to train its own ML model, instead of relying purely on live LLM evaluation. Two different datasets feed this, shown separately below so neither number gets confused for the other.</div>
  <div class="m" style="margin-top:6px"><b>Dataset A (below, per-market cards)</b> — real settled evaluation history for that market. This is the labeled training data (what happened, HIT or BUST) a model would learn from. <b>Dataset B (below, warehouse strip)</b> — the historical tennis match database the collector builds daily. This is the raw feature source a model would compute inputs from. A market can be data-rich in one and data-poor in the other — both matter before training starts.</div>
 </div>`;
 const db = r.dataset_b||{}; const dbCounts = db.counts||{}; const dbHb = db.heartbeat||{};
 const dbSection = `<div class="panel" style="margin-top:12px">
  <h3 class="h">DATASET B — Tennis Match Warehouse (feature source)</h3>
  <div class="m" style="margin-bottom:8px">Real historical match data collected daily — not evaluation counts. Full detail in Warehouse Command.</div>
  <div class="strip">
   <div class="tile"><div class="t">MATCHES</div><div class="v">${(dbCounts.matches||0).toLocaleString()}</div></div>
   <div class="tile"><div class="t">PLAYERS</div><div class="v">${(dbCounts.players||0).toLocaleString()}</div></div>
   <div class="tile"><div class="t">STAT ROWS</div><div class="v">${(dbCounts.player_year_stats||0).toLocaleString()}</div></div>
   <div class="tile"><div class="t">COLLECTOR RUNS</div><div class="v">${(dbCounts.collector_runs||0).toLocaleString()}</div></div>
  </div>
  <div class="m" style="margin-top:6px">Collector status: <span class="chip ${dbHb.status==='FAILED'?'A':'ok'}">${esc(dbHb.status||'OFFLINE')}</span> · last heartbeat ${esc(dbHb.last_heartbeat||'never')}</div>
 </div>`;
 document.getElementById('v-mlready').innerHTML= mlIntro + `
 <div style="margin-bottom:12px;display:flex;align-items:center;gap:12px">
  <button onclick="runCollectorNow()" style="background:var(--acc)">▶ Run Collector Now</button>
  <span class="m" id="runNowStatus">Runs daily at 06:00 AM CT automatically</span>
 </div>
 <h3 class="h" style="margin:0 0 8px">DATASET A — Evaluation History (training labels)</h3>
 <div class="cols" style="grid-template-columns:repeat(4,1fr)">${markets.map(card).join('')}</div>
 ${dbSection}
 <div class="panel" style="margin-top:12px;opacity:0.75"><h3 class="h">INFRASTRUCTURE STATUS</h3>
 <div class="m">${esc(r.infrastructure_status||'')}</div>
 <div class="m" style="margin-top:6px">${esc(r.dataset_b_status||'')}</div></div>`}
function healthCC(h){
 const checks = h.checks||{};
 const ds = h.dataset_a||{}; const wh = h.warehouse||{}; const ag = h.agents||{};
 const dotFor = v => v=='🟢'?'g':v=='🔴'?'r':'y';
 const healthIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">A single system-wide health check — Dataset A, the warehouse, and all agents, rolled into one score. This is infrastructure health, not pick quality — a healthy score means the plumbing is working, not that picks are good.</div>
  <div class="m" style="margin-top:6px"><b>SYSTEM CHECKS</b> are individual pass/fail signals (🟢/🟡/🔴). <b>NEEDS ATTENTION</b> only shows real, currently-flagged issues — an empty list here means everything checked came back clean, not that nothing was checked. <b>SYSTEM ISSUES</b> further down goes deeper — the real, permanent record of every write failure, rejected/broken evaluation, and warehouse collector failure the pipeline has actually caught, from all three sources in one place.</div>
 </div>`;
 document.getElementById('v-healthcc').innerHTML= healthIntro + `
 <div class="panel" style="margin-bottom:12px;display:flex;gap:20px;align-items:center">
  ${ringGauge(h.score, 100)}
  <div><h3 class="h" style="margin-bottom:6px">Overall health</h3>
   <div class="m">${h.score>=80?'Healthy':h.score>=50?'Degraded':'Critical'} \u2014 ${Object.values(checks).filter(v=>v=='\ud83d\udfe2').length}/${Object.keys(checks).length} checks green</div></div>
 </div>
 <div class="strip">
  <div class="tile"><div class="t">OVERALL HEALTH</div><div class="v">${h.score}%</div></div>
  <div class="tile"><div class="t">HEALTHY AGENTS</div><div class="v">${ag.healthy??0}/8</div></div>
  <div class="tile"><div class="t">BLOCKED/FAILED</div><div class="v ${(ag.blocked||ag.failed)?'bad':''}">${(ag.blocked||0)+(ag.failed||0)}</div></div>
  <div class="tile"><div class="t">V8 MODULE</div><div class="v">${h.v8_available?'AVAILABLE AT BOOT':'MISSING AT BOOT'}</div></div>
 </div>
 <div class="panel"><h3 class="h">SYSTEM CHECKS</h3>
  ${Object.entries(checks).map(([k,v])=>`<span class="chip"><span class="dot ${dotFor(v)}"></span>${k}</span>`).join('')}
 </div>
 ${h.attention&&h.attention.length?`<div class="panel" style="margin-top:10px"><h3 class="h">NEEDS ATTENTION</h3>
  ${h.attention.map(a=>`<div class="m bad" style="margin:4px 0">⚠ ${esc(a)}</div>`).join('')}</div>`
  :'<div class="panel" style="margin-top:10px"><span class="m">Nothing flagged — all confirmed signals nominal.</span></div>'}
 <div class="row">
  <div class="panel"><h3 class="h">DATASET A HEALTH</h3>
   <table><tr><td class="m">Exists</td><td>${ds.exists?'✓':'✗'}</td></tr>
   <tr><td class="m">Last update</td><td>${esc(ds.last_update||'—')}</td></tr>
   <tr><td class="m">Evaluations</td><td>${ds.eval_count??'—'}</td></tr>
   <tr><td class="m">Lifecycle entries</td><td>${ds.lifecycle_count??'—'}</td></tr>
   <tr><td class="m">Heartbeat rows</td><td>${ds.heartbeat_count??'—'}</td></tr></table></div>
  <div class="panel"><h3 class="h">WAREHOUSE HEALTH</h3>
   <table><tr><td class="m">File exists</td><td>${wh.exists?'✓':'✗'}</td></tr>
   <tr><td class="m">Last update</td><td>${esc(wh.last_update||'—')}</td></tr>
   <tr><td class="m">Read success</td><td>${wh.read_ok?'✓':'✗'}</td></tr>
   ${Object.entries(wh.row_counts||{}).map(([t,c])=>`<tr><td class="m">${esc(t)}</td><td>${c}</td></tr>`).join('')}
   </table></div>
 </div>
 <div class="panel" style="margin-top:10px"><h3 class="h">AGENT ROLLUP</h3>
  <span class="chip ok">WORKING/IDLE ${ag.healthy??0}</span>
  <span class="chip A">BLOCKED ${ag.blocked??0}</span>
  <span class="chip A">FAILED ${ag.failed??0}</span>
  <span class="chip">OFFLINE ${ag.offline??0}</span></div>
 <div class="panel" style="margin-top:10px">
  <h3 class="h">SYSTEM ISSUES <span class="m" style="font-weight:400">(everything the pipeline has actually caught, one place)</span></h3>
  <div class="m" style="margin-bottom:8px">Pulls together every real failure signal this system already records but used to leave scattered: Dataset A write failures, evaluations that were rejected or broke mid-pipeline, and Dataset B collector failures. Empty means genuinely zero issues found across all three sources, not "nothing was checked." The <b>eval_id</b> shown on evaluation-related rows can be opened directly from Pick Results if you want the full reasoning behind it.</div>
  ${(h.system_issues||[]).length ? (h.system_issues||[]).map(i=>{
    const srcChip = i.source==='dataset_a_write' ? '<span class="chip A">WRITE FAILURE</span>'
     : i.source==='warehouse_collector' ? '<span class="chip A">COLLECTOR</span>'
     : i.source==='evaluation_rejected' ? '<span class="chip B">REJECTED</span>'
     : i.source==='evaluation_failed' ? '<span class="chip A">EVAL FAILED</span>'
     : '<span class="chip">OTHER</span>';
    return `<div class="rowline" style="cursor:default">
     <span><b>${esc(i.summary||'')}</b>
      <span class="meta"> &middot; ${esc((i.occurred_at||'').slice(0,19))}</span></span>
     <span class="val">${srcChip}</span>
    </div>
    ${i.detail?`<div class="m" style="margin:2px 0 8px 4px">${esc(i.detail)}</div>`:''}`;
  }).join('') : '<span class="chip ok">0 issues found across Dataset A, evaluations, and the warehouse collector</span>'}
 </div>
 <div class="panel" style="margin-top:10px;opacity:0.7"><h3 class="h">INTAKE WORKER <span class="chip" style="float:right">NOT SHOWN</span></h3>
  <div class="m">${esc(h.intake_worker_note||'')}</div></div>`}
function ruleCC(d, registryRun){
 const cap=d.captured||[]; const unc=d.uncaptured||[];

 // Plain-English descriptions for each rule/gate label
 const RULE_DESC = {
  'GATE 11':      'Player identity check — verifies both players are real, currently active ATP/WTA athletes before any evaluation begins. Rejects unknowns loudly rather than evaluating garbage.',
  'GATE 11-B':    'Format contamination check — detects whether the prop type (e.g. Total Games) conflicts with the match format (e.g. best-of-5 Grand Slam). Prevents applying singles logic to doubles or wrong-format matches.',
  'REQS':         'Competitive Requirements — counts how many competitive evidence signals aligned for this pick (surface specialist, form streak, H2H edge, etc.). Low REQ count = thin evidence = lower confidence ceiling.',
  'COMPLIANCE':   'Post-evaluation audit — a second Haiku model call that reads the full evaluation and checks all 18 rules were applied correctly. Catches logic gaps, missing steps, and protocol violations the model may have skipped.',
  'RULE 24':      'Rank gap check — when the ranking difference between the two players is very large, the evaluation follows a stricter path. A big rank gap means expected dominance; the system must account for that rather than treating it as a coin flip.',
  'RULE 30':      'Confidence cap — applies a ceiling to picks where evidence is present but high-risk signals fire (extreme surface mismatch, recent injury flag, format uncertainty). Keeps the system from over-committing on genuinely uncertain situations.',
  'TRUTH LOCK':   'Data freeze — locks the V8 research snapshot used in an evaluation so that compliance and scoring run against identical facts to what the model saw. Prevents drift between evaluation and audit steps.',
  'DOMINANCE':    'Surface/form dominance classification — measures how decisively one player outperforms the other on this specific surface in recent form. High dominance + high REQs = stronger GREEN signal.',
 };

 const capCard=r=>`<div class="panel" style="margin-bottom:10px">
  <h3 class="h">${esc(r.label)} <span class="chip ok" style="float:right">CAPTURED</span></h3>
  ${RULE_DESC[r.label]?`<div class="m" style="margin-bottom:6px">${esc(RULE_DESC[r.label])}</div>`:''}
  ${r.note?`<div class="m" style="color:var(--dim)">${esc(r.note)}</div>`:''}
  <table style="margin-top:6px"><tr><th>VALUE</th><th>N</th><th>HIT RATE</th><th>AVG Δ CONF</th></tr>
  ${r.breakdown.map(b=>`<tr><td>${esc(b.value)}</td><td>${b.n}</td>
   <td>${b.hit_rate==null?'n/a ('+b.known+' known)':pct(b.hit_rate)}</td>
   <td class="${b.avg_delta<0?'bad':''}">${b.avg_delta>=0?'+':''}${(b.avg_delta*100).toFixed(1)}pp</td></tr>`).join('')}
  </table></div>`;
 const uncCard=r=>`<div class="panel" style="margin-bottom:10px;opacity:0.7">
  <h3 class="h">${esc(r.label)} <span class="chip A" style="float:right">NOT YET CAPTURED</span></h3>
  ${RULE_DESC[r.label]?`<div class="m" style="margin-bottom:4px">${esc(RULE_DESC[r.label])}</div>`:''}
  <div class="m">${esc(r.note)}</div></div>`;
 // Real performance-tier donut: bucket every captured rule's real
 // per-value hit_rate rows (Dataset A) into tiers. No named-rule
 // fire-count table exists in this shape (that mockup assumed a
 // different data model) -- this is the honest equivalent using
 // what's actually captured.
 const allRows = cap.flatMap(r => r.breakdown.filter(b => b.hit_rate != null));
 const tiers = {High:0, Moderate:0, Low:0, Underperforming:0};
 allRows.forEach(b => { const h=b.hit_rate; if(h>=0.80)tiers.High++; else if(h>=0.65)tiers.Moderate++; else if(h>=0.50)tiers.Low++; else tiers.Underperforming++; });
 const tierTotal = Object.values(tiers).reduce((a,b)=>a+b,0);
 const tierDonut = tierTotal ? `<div class="panel" style="margin-bottom:12px"><h3 class="h">Rule performance distribution</h3>
  <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap">
   ${donutChart([{label:'High (\u226580%)',value:tiers.High,color:'var(--grn)'},
     {label:'Moderate (65-79%)',value:tiers.Moderate,color:'var(--acc)'},
     {label:'Low (50-64%)',value:tiers.Low,color:'var(--yel)'},
     {label:'Underperforming (<50%)',value:tiers.Underperforming,color:'var(--red)'}], 140, tierTotal+' rows')}
   <div style="flex:1;min-width:220px">${donutLegend([{label:'High (\u226580%)',value:tiers.High,color:'var(--grn)'},
     {label:'Moderate (65-79%)',value:tiers.Moderate,color:'var(--acc)'},
     {label:'Low (50-64%)',value:tiers.Low,color:'var(--yel)'},
     {label:'Underperforming (<50%)',value:tiers.Underperforming,color:'var(--red)'}])}</div>
  </div><div class="m" style="margin-top:8px">Bucketed from real Dataset A hit-rate breakdowns across all captured rules \u2014 not a per-rule fire-count table, since Apex doesn't track rules that way.</div></div>` : '';
 document.getElementById('v-rulecc').innerHTML= `
 <div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">This shows how each gate and rule in the evaluation pipeline is performing against real outcomes. <b>CAPTURED</b> means Dataset A is recording this rule's decisions and hit rates. <b>NOT YET CAPTURED</b> means the rule fires but its specific data isn't being tracked yet in a way the system can report on.</div>
  <div class="m" style="margin-top:6px"><b>HIT RATE</b> — of picks where this rule fired with a specific value, how many actually hit. "n/a (0 known)" means no settled outcomes yet — settle picks in Pick Results to fill this in. <b>AVG Δ CONF</b> — how much this rule's presence moved the final confidence score on average.</div>
 </div>` + tierDonut +
  `<div class="m" style="margin-bottom:10px">${d.n} scored evaluations analyzed. Rules below are grouped by whether Dataset A actually captures them today.</div>
  <div class="row"><div>${cap.map(capCard).join('')}</div>
  <div>${unc.map(uncCard).join('')}</div></div>` + renderRuleRegistry(registryRun)}

function renderRuleRegistry(run){
 // 2026-08-09: real, DIFFERENT data source from everything above on
 // this screen — the cards above read Dataset A (live pick outcomes,
 // accumulate slowly). This section re-runs real walk-forward
 // backtests against Dataset B's 500K+ real historical matches,
 // instantly, on demand. Deliberately labeled distinctly so the two
 // are never confused with each other.
 //
 // Real fix (2026-08-09, after the user correctly pointed out raw
 // numbers alone are useless without knowing what to DO): every rule
 // now leads with a plain-English verdict + a real suggested action,
 // computed server-side in rule_registry.py's own real logic (not
 // invented here) — the technical AUC/Pearson numbers are still
 // shown, but as supporting detail underneath, not the headline.
 const verdictColor = v => {
  if (!v) return 'var(--dim)';
  if (v.startsWith('OK')) return 'var(--grn)';
  if (v.startsWith('PROBLEM')) return 'var(--red)';
  if (v.startsWith('WATCH')) return 'var(--yel)';
  if (v.startsWith('UNEXPECTED')) return 'var(--yel)';
  if (v.startsWith('WAITING')) return 'var(--dim)';
  return 'var(--dim)';
 };
 const verdictIcon = v => {
  if (!v) return '?';
  if (v.startsWith('OK')) return '✅';
  if (v.startsWith('PROBLEM')) return '❌';
  if (v.startsWith('WATCH')) return '👁';
  if (v.startsWith('UNEXPECTED')) return '👀';
  if (v.startsWith('WAITING')) return '⏳';
  return '⚠';
 };

 const rows = (run && run.results) ? run.results : [];

 const ruleCards = rows.map(r => `
  <div class="panel" style="margin-bottom:8px;border-left:3px solid ${verdictColor(r.plain_verdict)}">
   <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
     <b>${verdictIcon(r.plain_verdict)} ${esc(r.name)}</b>
     <span class="m" style="margin-left:8px">[${esc(r.system)}]</span>
    </div>
   </div>
   <div style="color:${verdictColor(r.plain_verdict)};font-weight:600;margin-top:4px">${esc(r.plain_verdict||r.status)}</div>
   <div class="m" style="margin-top:2px">${esc(r.plain_action||r.detail||'')}</div>
   ${r.status==='OK' ? `<div class="m" style="margin-top:6px;font-size:10.5px;opacity:0.7">
     Technical detail: ${esc(r.metric_type)} = ${r.current_value.toFixed(4)}, baseline ${r.real_baseline.toFixed(4)} (${esc(r.real_baseline_date)}), drift ${r.drift_from_baseline>=0?'+':''}${r.drift_from_baseline.toFixed(4)}
    </div>` : ''}
  </div>`).join('');

 const body = rows.length ? `
  <div class="panel" style="margin-bottom:10px;background:var(--surface-1,#12141c)">
   <div style="font-size:16px;font-weight:600">${esc(run.headline||'')}</div>
   <div class="m" style="margin-top:4px">Last run: ${esc(run.run_at)} — ${run.n_rules_run} rules tested</div>
  </div>
  ${ruleCards}` : `<div class="m">Never run yet — click "▶ Run Audit Now" below to test every predictive rule against real Dataset B history for the first time.</div>`;

 return `
 <div class="panel" style="margin-top:16px">
  <h3 class="h">📖 Historical Backtest Audit — a different real data source
   <button onclick="runRuleRegistryNow()" style="float:right;background:var(--acc)">▶ Run Audit Now</button>
  </h3>
  <div class="m">Re-runs real walk-forward backtests for every predictive rule (ELO, S1, S2, S4, S5, Aces, BP, Total-Games extenders, Rule 30) against Dataset B's full 500K+ real historical match archive — instant, on demand, no waiting for live picks to accumulate.</div>
  <div class="m" style="margin-top:6px;color:var(--dim)">⚠ Does NOT run automatically on a schedule — manual trigger only, right now. Safety/compliance gates (Gate 11, Rule 9, Rule 12/Truth Lock, etc.) are intentionally NOT included here — those need real operational-log evidence to test correctly, not a correlation test against match history.</div>
  <div style="margin-top:12px">${body}</div>
 </div>`;
}

async function runRuleRegistryNow(){
 const r = await(await fetch('/api/rule_registry_run_now',{method:'POST',body:'{}'})).json();
 alert(r.message || r.error || 'Started.');
}
function agentsView(s){const agents=s.agents||[];
 const ago=sec=>{if(sec==null)return 'never';
  if(sec<60)return Math.round(sec)+'s ago';
  if(sec<3600)return Math.round(sec/60)+'m ago';
  if(sec<86400)return Math.round(sec/3600)+'h ago';
  return Math.round(sec/86400)+'d ago'};
 const statusColor=st=>st=='WORKING'?'ok':st=='IDLE'?'':st=='BLOCKED'||st=='FAILED'?'A':'';
 document.getElementById('v-agents').innerHTML=
 `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">The Agent Layer shows the real-time status of every background process running inside OpenClaw. Each agent has a specific job — Research fetches V8 data, Evaluation runs the model, Compliance audits the result, and so on. This screen tells you which agents are actively working, which are idle, and which are stuck or failed.</div>
  <div class="m" style="margin-top:6px"><b>WORKING</b> — actively processing something right now. <b>IDLE</b> — waiting for the next task, healthy. <b>BLOCKED/FAILED</b> — needs attention. <b>OFFLINE</b> — no heartbeat in 24+ hours, likely the process crashed. Last heartbeat time tells you how long ago the agent checked in.</div>
 </div>
 <div class="panel"><h3 class="h">AGENT LAYER — what OpenClaw is doing right now</h3>
 <div class="cols" style="grid-template-columns:repeat(4,1fr)">
 ${agents.map(a=>`<div class="card" style="cursor:default;min-height:118px">
  <div style="display:flex;justify-content:space-between;align-items:center">
   <b>${esc(a.label)}</b><span class="dot ${a.health=='🟢'?'g':a.health=='🔴'?'r':'y'}"></span>
  </div>
  <span class="chip ${statusColor(a.status)}" style="margin-top:4px">${a.status}</span>
  <div class="m" style="margin-top:6px">
   ${a.current_task?'task: '+esc(a.current_task):'&nbsp;'}<br>
   last heartbeat: ${ago(a.age_seconds)}<br>
   ✓ ${a.success_count}&nbsp;&nbsp;✗ ${a.failure_count}
   ${a.note?`<br><span class="${a.status=='BLOCKED'||a.status=='FAILED'?'bad':''}">${esc(a.note)}</span>`:''}
  </div></div>`).join('')}
 </div></div>
 <div class="m" style="margin-top:10px">Status derived from data/warehouse/dataset_a.sqlite — agent_heartbeat table.
 WORKING with no update in 10min displays as BLOCKED. No heartbeat in 24h displays as OFFLINE.</div>`}
async function deck(s){
 const el = document.getElementById('v-deck');
 const g=s.green, m=s.ml;
 const openEvals = (g.pending||0) + (m.pending||0);
 // Executive strip: real counts from existing endpoints, fetched in
 // parallel. Any endpoint that fails shows an em-dash (honest
 // "unavailable"), never a placeholder number.
 const [cost, graph, sessions, mhist] = await Promise.all([
  fetch('/api/cost_cc').then(r=>r.json()).catch(()=>null),
  fetch('/api/memory_graph').then(r=>r.json()).catch(()=>null),
  fetch('/api/command_chat_sessions').then(r=>r.json()).catch(()=>null),
  fetch('/api/metric_history').then(r=>r.json()).catch(()=>[]),
 ]);
 const nEvals = cost && cost.evaluation_metrics ? cost.evaluation_metrics.evaluations_traced : '\u2014';
 const nDec = graph ? graph.nodes.filter(n=>n.type==='decision').length : '\u2014';
 const nInv = graph ? graph.nodes.filter(n=>n.type==='investigation').length : '\u2014';
 const nNodes = graph ? graph.nodes.length : '\u2014';
 const nSess = sessions ? sessions.length : '\u2014';
 const costToday = cost && cost.summary ? '$'+cost.summary.today.toFixed(2) : '\u2014';
 const execStrip = `<div class="strip" style="grid-template-columns:repeat(6,1fr)">
  <div class="tile"><div class="t">Evaluations</div><div class="v">${nEvals}</div>${sparkline(mhist,'evaluations')}</div>
  <div class="tile"><div class="t">Decisions</div><div class="v">${nDec}</div>${sparkline(mhist,'decisions')}</div>
  <div class="tile"><div class="t">Investigations</div><div class="v">${nInv}</div>${sparkline(mhist,'investigations')}</div>
  <div class="tile"><div class="t">Memory nodes</div><div class="v">${nNodes}</div>${sparkline(mhist,'memory_nodes')}</div>
  <div class="tile"><div class="t">Chat sessions</div><div class="v">${nSess}</div>${sparkline(mhist,'chat_sessions')}</div>
  <div class="tile"><div class="t">Cost today</div><div class="v">${costToday}</div>${sparkline(mhist,'cost_today',64,24,'var(--grn)')}</div>
 </div>
 <div class="m" style="margin-top:-4px;margin-bottom:12px">${mhist.length<2?'Trend lines will appear once at least 2 days of history are recorded (started today).':mhist.length+' days of real history'}</div>`;
 // Pipeline strip: real per-stage counts from s.canvas -- the same
 // data the Operations kanban renders, re-presented as a flow.
 const stages = Object.entries(s.canvas||{});
 const pipeStrip = stages.length ? `<div class="panel"><h3 class="h">Pipeline</h3>
  <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
  ${stages.map(([st,cards],i)=>`${i?'<span style="color:var(--dim2)">\u2192</span>':''}
   <span style="display:flex;flex-direction:column;align-items:center;min-width:76px">
    <b style="font-size:20px">${cards.length}</b>
    <span class="m" style="text-transform:capitalize">${esc(st)}</span></span>`).join('')}
  </div></div>` : '';
 const recentDecisions = graph ? graph.nodes.filter(n=>n.type==='decision').slice(-5).reverse() : [];
 const recentInvestigations = graph ? graph.nodes.filter(n=>n.type==='investigation').slice(-5).reverse() : [];
 const recentPanel = `<div class="row" style="margin-top:12px">
  <div class="panel" style="margin:0"><h3 class="h">Recent decisions</h3>
   ${recentDecisions.length?recentDecisions.map(n=>`<div class="rowline" style="cursor:pointer" onclick="document.querySelector('[data-v=memcc]').click()">${esc(n.label)}</div>`).join(''):'<span class="m">none yet</span>'}</div>
  <div class="panel" style="margin:0"><h3 class="h">Recent investigations</h3>
   ${recentInvestigations.length?recentInvestigations.map(n=>`<div class="rowline" style="cursor:pointer" onclick="document.querySelector('[data-v=memcc]').click()">${esc(n.label)}</div>`).join(''):'<span class="m">none yet</span>'}</div>
 </div>`;
 const dotCls = v => v=='\ud83d\udfe2'?'g':v=='\ud83d\udfe1'?'y':'r';
 const deckIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">The home screen — a real-time snapshot of the whole system at a glance: how many evaluations, decisions, and investigations have happened, what's moving through the pipeline right now, and overall system health.</div>
  <div class="m" style="margin-top:6px">Trend lines under each tile need at least 2 days of history to appear — a flat or missing line on day one is expected, not broken. <b>Pipeline</b> mirrors the same live counts as Operations, just condensed into one row.</div>
 </div>`;
 el.innerHTML = deckIntro + execStrip + pipeStrip + recentPanel + `
 <div style="display:grid;grid-template-columns:1.1fr 2fr;gap:12px;margin-bottom:12px">
  <div class="panel" style="margin:0;display:flex;gap:18px;align-items:center">
   <div>${ringGauge(s.health_score, 108)}</div>
   <div style="flex:1">
    <h3 class="h" style="margin-bottom:8px">System health</h3>
    <div>${Object.entries(s.health).map(([k,v])=>`<span class="chip"><span class="dot ${dotCls(v)}"></span>${esc(k)}</span>`).join('')}</div>
   </div>
  </div>
  <div class="panel" style="margin:0">
   <h3 class="h">Today</h3>
   <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:8px">
    <div><div class="m">Green tracker</div><div style="font-size:19px;font-weight:600;margin-top:4px">${g.completed}/${g.goal} &middot; ${g.hit==null?'n/a':pct(g.hit)}</div></div>
    <div><div class="m">Moneyline</div><div style="font-size:19px;font-weight:600;margin-top:4px">${m.completed}/${m.goal} &middot; ${m.hit==null?'n/a':pct(m.hit)}</div></div>
    <div><div class="m">Open evaluations</div><div style="font-size:19px;font-weight:600;margin-top:4px">${openEvals}</div></div>
    <div><div class="m">Avg confidence</div><div style="font-size:19px;font-weight:600;margin-top:4px">${pct(g.avg_conf)}</div></div>
   </div>
  </div>
 </div>
 <div class="row" style="margin-top:0">
  <div class="panel" style="margin:0"><h3 class="h">Live feed</h3><div id="feed">
   ${s.feed.map(l=>`<div>${esc(l).slice(-150)}</div>`).join('')||'<span class="m">quiet</span>'}</div></div>
  <div>
   <div class="panel" style="margin:0 0 12px"><h3 class="h">Approvals</h3>
    ${s.approvals.length?s.approvals.map(p=>`<div class="card"><b>#${p.id}</b> ${esc(p.signal)} ${p.current??''}\u2192${p.proposed??''}
     <div style="margin-top:8px"><button class="btn-primary" onclick="decide(${p.id},true)">Approve</button> <button onclick="decide(${p.id},false)">Reject</button></div></div>`).join(''):'<span class="m">none pending</span>'}</div>
   <div class="panel" style="margin:0"><h3 class="h">Add slip</h3>
    <div style="margin-bottom:8px">stake <input id="s1" value="20"> legs <input id="s2" value="6"></div>
    <div style="margin-bottom:10px">result <input id="s3" value="6/6"> payout <input id="s4" value="0"></div>
    <button class="btn-primary" onclick="slip()">Save</button></div>
  </div>
 </div>
 <div class="panel" style="margin-top:12px">
  <input id="deckAsk" placeholder="Ask Command Chat anything about Apex\u2026" style="width:100%;padding:10px 12px"
   onkeydown="if(event.key=='Enter'){document.querySelector('[data-v=chatcc]').click();setTimeout(()=>{document.getElementById('chatInput').value=this.value;sendChatMessage()},0)}">
  <div class="m" style="margin-top:8px">Full pick-stage board lives on Operations \u2014 this screen stays focused on whether Apex is healthy right now.</div>
 </div>`
}

function timeAgo(dateStr){
 if(!dateStr) return 'unknown';
 const diffMs = Date.now() - new Date(dateStr.replace(' ','T')+'Z').getTime();
 const mins = Math.round(diffMs/60000);
 if(mins < 1) return 'just now';
 if(mins < 60) return mins+'m ago';
 const hrs = Math.round(mins/60);
 if(hrs < 24) return hrs+'h ago';
 return Math.round(hrs/24)+'d ago';
}
function opsv(s){
 const opsIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">Live view of every pick currently in flight through the pipeline, stage by stage: Research → Evaluation → Compliance → Scoring → Post. The dot in each column shows whether that stage is done (🟢), waiting (🟡), or hasn't started for that pick yet.</div>
  <div class="m" style="margin-top:6px"><b>STALLED</b> means a pick has been sitting at one stage far longer than normal — usually a sign the intake worker or an agent died mid-item. <b>Clear Stalls</b> rotates the Discord bot's log and restarts it, which resolves most stalls caused by a bot restart the Operations lane didn't know about. <b>RETRIES</b> counts real retry attempts, not estimates.</div>
 </div>`;
 // Real per-stage counts derived from the actual lane data below --
 // NOT the mockup's "queue capacity/utilization %", which has no
 // real backend concept in Apex. Counting how many in-flight picks
 // are currently sitting at each stage is honest and real.
 const stageCounts = {Research:0,Evaluation:0,Compliance:0,Scoring:0,Post:0};
 (s.ops||[]).forEach(r => { for(const st of ['Research','Evaluation','Compliance','Scoring','Post']){ if(r.lane[st]==='🟡') stageCounts[st]++; } });
 const stageStrip = `<div class="panel"><h3 class="h">Currently at each stage</h3>
  <div style="display:flex;gap:24px;flex-wrap:wrap">
  ${Object.entries(stageCounts).map(([k,v])=>`<div><div class="m">${k}</div><div style="font-size:20px;font-weight:600;margin-top:2px">${v}</div></div>`).join('')}
  </div></div>`;
 document.getElementById('v-opsv').innerHTML=opsIntro+tiles(s)+canvas(s)+stageStrip+
 `<div class="panel" style="margin-top:12px"><h3 class="h">STAGE TIMELINE (durations · retries · stalls)
  ${s.ops.some(r=>r.stalled)?`<button onclick="clearStalls()" style="float:right;font-size:11px">Clear Stalls</button>`:''}
  </h3>
 <table><tr><th>PICK</th><th>R</th><th>E</th><th>C</th><th>S</th><th>P</th><th>DUR (min)</th><th>RETRIES</th><th></th></tr>
 ${s.ops.map(r=>`<tr><td>${esc(r.name)}</td>${['Research','Evaluation','Compliance','Scoring','Post']
  .map(st=>`<td>${r.lane[st]}</td>`).join('')}
  <td class="m">${Object.entries(r.durs).map(([k,v])=>k[0]+':'+v).join(' ')}</td>
  <td>${r.retries||''}</td><td>${r.stalled?'<span class="bad">STALLED</span>':''}</td></tr>`).join('')}</table>
 <div class="m" style="margin-top:8px">RULE NODES:
 ${['Gate11','Gate11-B','Rule24','Rule30','TruthLock','Dominance','REQs','FeatureEcho','Learning','Validation']
  .map(n=>`<span class="chip">${n}</span>`).join('')}
  — click a pick card to see its activations in the Inspector</div></div>`}
function valid(v, w){
 const validIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">Validation shows whether the evaluation rules are actually working as intended based on real settled outcomes. It compares what the system predicted against what actually happened — not to second-guess individual picks, but to spot systematic patterns where a rule is consistently helping or hurting accuracy.</div>
  <div class="m" style="margin-top:6px"><b>HIT RATE</b> — percentage of picks in this category that hit. <b>TREND</b> — whether that hit rate is improving or degrading over recent picks. Needs settled picks to be meaningful — run evaluations, record outcomes in Pick Results, and this screen fills in automatically.</div>
 </div>`;
 w = w || {};
 const t = w.trend;
 const trendChip = t ? `<span class="chip ${t.direction=='IMPROVING'?'ok':t.direction=='DEGRADING'?'A':''}">${t.direction}</span>
  <span class="m">latest ${pct(t.latest_rate)} vs prior avg ${pct(t.prior_avg)}</span>` : '<span class="m">need 2+ runs for a trend</span>';
 const tot = w.totals || {};
 const dav = w.dataset_a_verdicts || {};
 document.getElementById('v-valid').innerHTML= validIntro +
 `<div class="strip">
  <div class="tile" style="display:flex;align-items:center;gap:12px">${ringGauge(v.runs?Math.round(100*(v.history&&v.history.length?((v.history[0].green_hits||0)/(v.history[0].green_known||1)):0)):0, 70)}<div><div class="t">LATEST RUN</div><div class="m">${v.runs} run${v.runs!=1?'s':''} recorded</div></div></div>
  <div class="tile"><div class="t">TREND</div><div class="v">${trendChip}</div></div>
  <div class="tile"><div class="t">PRESERVED (all runs)</div><div class="v">${tot.preserved??0}</div></div>
  <div class="tile"><div class="t">LOST (all runs)</div><div class="v ${tot.lost?'bad':''}">${tot.lost??0}</div></div>
  <div class="tile"><div class="t">CREATED (all runs)</div><div class="v">${tot.created??0}</div></div>
 </div>
 <div class="panel"><h3 class="h">VALIDATION CENTER — ${v.runs} recorded runs</h3>`+
 (v.history.length?`<table><tr><th>RUN</th><th>GREENS</th><th>HITS</th><th>PRESERVED</th><th>LOST</th><th>NEW</th><th>W-DEMOTED</th><th>L-PROMOTED</th></tr>
 ${v.history.map(h=>`<tr><td class="m">${h.ts}</td><td>${h.replay_green}</td><td>${h.green_hits}/${h.green_known}</td>
 <td>${h.preserved}</td><td class="${h.lost?'bad':''}">${h.lost}</td><td>${h.created}</td>
 <td class="${h.win_demoted?'bad':''}">${h.win_demoted}</td><td class="${h.lose_promoted?'bad':''}">${h.lose_promoted}</td></tr>`).join('')}</table>`
 :`<div class="m">No validation runs yet. On the Mac Mini run:<br>
 <b>python3 green_validation_suite.py --build-manifest</b> then <b>--run</b></div>`)+
 `</div>
 <div class="panel" style="margin-top:12px"><h3 class="h">DATASET A VALIDATION (per-pick, live) ${dav.latest_run_id?'<span class="m" style="float:right">latest run: '+dav.latest_run_id.slice(0,8)+'…</span>':''}</h3>
 ${(dav.by_verdict||[]).length?`<span class="m">${(dav.by_verdict||[]).map(x=>`${esc(x.validation_verdict)}: ${x.n}`).join(' · ')}</span>
 <div class="m" style="margin-top:6px">win-demoted (LOST+HIT, derived): ${dav.win_demoted??0} · lose-promoted (CREATED+BUST, derived): ${dav.lose_promoted??0}</div>`
 :`<span class="m">No per-pick verdicts written yet — run green_validation_suite.py --run on the Mini.</span>`}</div>
 <div class="panel" style="margin-top:12px;opacity:0.75"><h3 class="h">CALIBRATION <span class="chip A" style="float:right">NOT AVAILABLE</span></h3>
 <div class="m">${esc(w.calibration_note||'')}</div></div>
 <div class="panel" style="margin-top:10px;opacity:0.75"><h3 class="h">PROMOTIONS / DEMOTIONS <span class="chip" style="float:right">PARTIAL</span></h3>
 <div class="m">${esc(w.promotion_note||'')}</div></div>
 <div class="panel" style="margin-top:12px"><h3 class="h">LATEST REPORT</h3>
 <pre>${esc(v.latest_report)||'(none yet)'}</pre></div>`}
function rules(rows){
 const rulesIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">Every rule in the evaluation system, its current disposition, and the real picks it has touched. <b>KEEP</b> means the rule is trusted and stays active as-is. <b>AUDIT</b> means it's flagged for review — firing in a way that needs a human look before being trusted further.</div>
  <div class="m" style="margin-top:6px"><b>FIRED</b> — how many times the rule has activated. <b>RECORD</b> — its real win/loss record where known. <b>IMPACT</b> — what the rule actually does to a pick's confidence or eligibility. <b>DEPENDS</b> — other rules or gates it relies on. Click any row to expand the real picks it affected.</div>
 </div>`;
 document.getElementById('v-rules').innerHTML= rulesIntro +
 `<div class="panel"><h3 class="h">RULE ROADMAP — click a rule for affected picks</h3>
 <table><tr><th>RULE</th><th>STATUS</th><th>FIRED</th><th>RECORD</th><th>IMPACT</th><th>DEPENDS</th></tr>
 ${rows.map((r,i)=>`<tr class="click" onclick="ruled(${i})"><td><b>${r.rule}</b><div class="m">${r.purpose}</div></td>
 <td><span class="chip ${r.status=='KEEP'?'ok':r.status=='AUDIT'?'A':''}">${r.status}</span></td>
 <td>${r.fired}</td><td>${r.record}</td><td class="m">${r.impact}</td><td class="m">${r.deps}</td></tr>
 <tr id="rd${i}" style="display:none"><td colspan="6">${r.picks.length?r.picks.map(p=>
  `<span class="chip ${p.color}" style="cursor:pointer" onclick="event.stopPropagation();open_('${p.key}')">
  ${esc(p.player)} ${pct(p.conf)} ${p.outcome||'PENDING'}</span>`).join(''):'<span class="m">no ledger hits in recent archives</span>'}</td></tr>`).join('')}
 </table></div>`}
function ruled(i){const el=document.getElementById('rd'+i);el.style.display=el.style.display=='none'?'':'none'}
function barChart(pts, W, H){
 if(!pts || !pts.length) return '<span class="m">no data yet</span>';
 const vals = pts.map(p=>p[1]);
 const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals) || 1;
 const bw = (W-20)/pts.length;
 const y0 = H - ((0-lo)/(hi-lo))*(H-20) - 10;
 return `<svg width="${W}" height="${H}">
  <line x1="10" y1="${y0}" x2="${W-10}" y2="${y0}" stroke="var(--edge)" stroke-width="1"/>
  ${pts.map((p,i)=>{
    const x = 10 + i*bw + bw*0.15;
    const bh = Math.abs(p[1]-0)/(hi-lo)*(H-20);
    const y = p[1]>=0 ? y0-bh : y0;
    return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(bw*0.7).toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="${p[1]>=0?'var(--grn)':'var(--red)'}"/>
     <text x="${(x+bw*0.35).toFixed(1)}" y="${H-2}" font-size="9" fill="var(--dim)" text-anchor="middle">${esc(String(p[0]).slice(0,5))}</text>`;
  }).join('')}
 </svg>`;
}
function reliabilityDiagram(buckets, W, H){
 // Reliability diagram: x = predicted value, y = actual value, both 0-1.
 // A perfectly calibrated estimator's points fall on the diagonal.
 // Buckets marked INSUFFICIENT_DATA (below MIN_SAMPLES_PER_BUCKET) are
 // drawn as faint hollow markers -- shown so the gap in coverage is
 // visible, but never as a plotted point implying a real reading.
 if(!buckets || !buckets.length) return '<span class="m">not enough settled data yet</span>';
 const pad = 34;
 const plot = v => pad + v*(W-pad-14);           // x: value 0-1 -> pixel
 const ploty = v => (H-pad) - v*(H-pad-14);       // y: value 0-1 -> pixel (inverted)
 const real = buckets.filter(b=>b.verdict!=='INSUFFICIENT_DATA');
 const thin = buckets.filter(b=>b.verdict==='INSUFFICIENT_DATA');
 return `<svg width="${W}" height="${H}">
  <line x1="${plot(0)}" y1="${ploty(0)}" x2="${plot(1)}" y2="${ploty(1)}" stroke="var(--dim2)" stroke-width="1" stroke-dasharray="4,3"/>
  <line x1="${pad}" y1="${H-pad}" x2="${W-14}" y2="${H-pad}" stroke="var(--edge)" stroke-width="1"/>
  <line x1="${pad}" y1="14" x2="${pad}" y2="${H-pad}" stroke="var(--edge)" stroke-width="1"/>
  <text x="${(W)/2}" y="${H-6}" font-size="9" fill="var(--dim)" text-anchor="middle">predicted</text>
  <text x="10" y="${H/2}" font-size="9" fill="var(--dim)" text-anchor="middle" transform="rotate(-90,10,${H/2})">actual</text>
  ${thin.map(b=>{
    const midX = plot(0.5); // no predicted/actual mean available -- shown as a note, not a point
    return '';
  }).join('')}
  ${real.map(b=>{
    const r = Math.max(3, Math.min(11, Math.sqrt(b.n)*1.4));
    const gap = Math.abs(b.gap||0);
    const color = gap < 0.05 ? 'var(--grn)' : gap < 0.12 ? 'var(--yel)' : 'var(--red)';
    return `<circle cx="${plot(b.mean_predicted).toFixed(1)}" cy="${ploty(b.mean_actual).toFixed(1)}" r="${r.toFixed(1)}" fill="${color}" fill-opacity="0.75" stroke="${color}" stroke-width="1.5"/>`;
  }).join('')}
 </svg>
 ${thin.length ? `<div class="m" style="margin-top:4px">${thin.length} of ${buckets.length} buckets have too few settled samples to plot (need 10+ each) -- shown as gaps, not zeros.</div>` : ''}`;
}
function bank(b){
 const bankIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">The Bankroll screen tracks your real betting performance over time. It reads from slips you've submitted — either manually or via the Slip Review panel in Pick Intake. <b>All numbers here reflect real money you actually staked</b>, not theoretical picks from evaluations.</div>
  <div class="m" style="margin-top:6px"><b>NET PROFIT</b> — total won minus total staked across all slips. <b>ROI</b> — return on investment as a percentage. <b>SLIP HISTORY</b> — every slip you've logged. To add a slip: send a slip screenshot to #slips-pnl on Discord, or use the Slip Review section in Pick Intake. To edit your starting bankroll, click the ✏ Edit button below.</div>
 </div>`;
 const pts=b.growth;const W=760,H=140;
 const xs=pts.length>1?(W-20)/(pts.length-1):1;
 const vals=pts.map(p=>p[1]).concat([b.start]);const lo=Math.min(...vals),hi=Math.max(...vals)+1;
 const y=v=>H-10-((v-lo)/(hi-lo))*(H-20);
 const path=pts.map((p,i)=>(i?'L':'M')+(10+i*xs)+','+y(p[1])).join(' ');
 document.getElementById('v-bank').innerHTML= bankIntro +
 `<div class="strip"><div class="tile"><div class="t">BANKROLL</div><div class="v">$${b.bankroll.toLocaleString()}</div>
 ${bar(b.bankroll/b.target)}<div class="m">start $${b.start.toLocaleString()} → target $${b.target.toLocaleString()}</div></div>
 <div class="tile"><div class="t">NET PROFIT</div><div class="v">$${b.profit.toFixed(0)}</div><div class="m">ROI ${(b.roi*100).toFixed(0)}%</div></div>
 <div class="tile"><div class="t">SLIPS</div><div class="v">${b.n}</div><div class="m">6/6:${b.six} · 5/6:${b.five} · L:${b.loss}</div></div>
 <div class="tile"><div class="t">LATEST P&L</div><div class="m">day ${b.daily.slice(-1).map(d=>'$'+d[1].toFixed(0))} ·
  wk ${b.weekly.slice(-1).map(d=>'$'+d[1].toFixed(0))} · mo ${b.monthly.slice(-1).map(d=>'$'+d[1].toFixed(0))}</div></div></div>
 <div class="panel" style="margin-top:12px"><h3 class="h">EDIT BANKROLL</h3>
  <div class="m">Starting bankroll: <b>$${b.start.toLocaleString()}</b>
   <button onclick="document.getElementById('brEdit').style.display='block'" style="margin-left:8px;font-size:11px">✏ Edit</button></div>
  <div id="brEdit" style="display:none;margin-top:8px">
   <input id="brAmt" type="number" value="${b.start}" style="width:120px" placeholder="amount">
   <button onclick="saveBankroll()">Save</button>
   <button onclick="document.getElementById('brEdit').style.display='none'" style="margin-left:4px">Cancel</button>
   <span id="brStatus" class="m" style="margin-left:8px"></span>
  </div></div>
 ${(b.manual_auto_overlaps && b.manual_auto_overlaps.length) ? `<div class="panel" style="margin-top:12px;border-color:#c0392b">
  <h3 class="h">⚠️ POSSIBLE DOUBLE-COUNTED SLIPS</h3>
  <div class="m">${b.manual_auto_overlaps.length} manual entr${b.manual_auto_overlaps.length===1?'y':'ies'} above match an auto-captured slip on stake/payout/legs — this may be the same real slip counted in both BANKROLL and AUTO-CAPTURED ROI below. Remove the manual entry if so.</div>
  <table><tr><th>Manual #</th><th>Date</th><th>Stake</th><th>Payout</th><th>Legs</th><th>Auto slip_id</th></tr>
  ${b.manual_auto_overlaps.map(o=>`<tr><td>${o.manual_id}</td><td>${o.manual_date}</td><td>$${o.stake}</td><td>$${o.payout}</td><td>${o.legs}</td><td>${o.auto_slip_id}</td></tr>`).join('')}
  </table></div>` : ''}
 ${bankAutoRoiPanel(b.auto_roi)}
 <div class="panel"><h3 class="h">GROWTH</h3>${pts.length?`<svg width="${W}" height="${H}">
 <path d="${path}" fill="none" stroke="var(--acc)" stroke-width="2"/></svg>`:'<span class="m">no slips yet</span>'}</div>
 <div class="row">
  <div class="panel"><h3 class="h">WEEKLY PERFORMANCE</h3>${barChart(b.weekly, 380, 130)}</div>
  <div class="panel"><h3 class="h">MONTHLY PERFORMANCE</h3>${barChart(b.monthly, 380, 130)}</div>
 </div>
 <div class="row" style="margin-top:12px"><div class="panel"><h3 class="h">SLIP HISTORY</h3>
 <table><tr><th>#</th><th>DATE</th><th>STAKE</th><th>LEGS</th><th>RESULT</th><th>PAYOUT</th><th>P/L</th></tr>
 ${b.entries.map(e=>`<tr><td>${e.id}</td><td>${e.date}</td><td>$${e.stake}</td><td>${e.legs}</td>
 <td>${e.result}</td><td>$${e.payout}</td><td class="${e.profit<0?'bad':''}">$${e.profit}</td></tr>`).join('')}</table></div>
 <div class="panel"><h3 class="h">DAILY P&L</h3>
 <table><tr><th>DAY</th><th>P/L</th></tr>${b.daily.map(d=>`<tr><td>${d[0]}</td><td class="${d[1]<0?'bad':''}">$${d[1].toFixed(0)}</td></tr>`).join('')}</table></div></div>`}
function bankAutoRoiPanel(r){
 if(!r || !r.total_slips_resolved) {
  const excl = r && r.total_excluded ? `${r.total_excluded} slip(s) captured but not yet resolvable (pending results, or missing stake/payout data from before this was tracked).` : 'No auto-captured slips resolved yet.';
  return `<div class="panel"><h3 class="h">🎯 AUTO-CAPTURED ROI <span class="m">(from Slip Review screenshots, separate from manual entries above)</span></h3>
   <div class="m">${excl}</div></div>`;
 }
 const roiClass = r.total_profit < 0 ? 'bad' : '';
 const exclLine = r.total_excluded ? `<div class="m" style="margin-top:6px">${r.total_excluded} slip(s) not counted: ${Object.entries(r.excluded).filter(([k,v])=>v>0).map(([k,v])=>`${v} ${k.replace(/_/g,' ')}`).join(', ')}.</div>` : '';
 return `<div class="panel"><h3 class="h">🎯 AUTO-CAPTURED ROI <span class="m">(from Slip Review screenshots, separate from manual entries above)</span></h3>
  <div class="strip" style="margin:0">
   <div class="tile"><div class="t">RESOLVED SLIPS</div><div class="v">${r.total_slips_resolved}</div><div class="m">${r.win_count}W · ${r.loss_count}L</div></div>
   <div class="tile"><div class="t">STAKED</div><div class="v">$${r.total_staked.toFixed(2)}</div></div>
   <div class="tile"><div class="t">PROFIT</div><div class="v ${roiClass}">$${r.total_profit.toFixed(2)}</div></div>
   <div class="tile"><div class="t">ROI</div><div class="v ${roiClass}">${(r.roi_pct*100).toFixed(1)}%</div><div class="m">win rate ${(r.win_rate*100).toFixed(0)}%</div></div>
  </div>
  ${exclLine}
 </div>`;
}
function costCC(d){
 const el = document.getElementById('v-costcc');
 if(!d){ el.innerHTML = '<div class="v6cost"><span class="m">ATE not available — no cost data captured yet.</span></div>'; return }
 const s=d.summary, m=d.evaluation_metrics;
 const money = x => '$'+(x??0).toFixed(4);
 const moneyShort = x => '$'+(x??0).toFixed(2);
 // Today-vs-average delta (V3 visual design rec: a bare number is a
 // snapshot, a number with a trend is a story) -- computed entirely
 // client-side from d.daily_history, which /api/cost_cc already
 // returns in full. No new backend query for this.
 const priorDays = (d.daily_history||[]).slice(1, 8); // skip today itself, use up to the prior 7
 const priorAvg = priorDays.length ? priorDays.reduce((a,r)=>a+r.total_cost,0)/priorDays.length : null;
 const deltaPct = (priorAvg && priorAvg > 0) ? Math.round(((s.today - priorAvg)/priorAvg)*100) : null;
 const subline = deltaPct == null ? 'No prior-week average yet to compare against.'
  : `That&rsquo;s <b style="color:${deltaPct<=0?'var(--grn)':'var(--yel)'}">${Math.abs(deltaPct)}% ${deltaPct<=0?'below':'above'}</b> your 7-day average of ${moneyShort(priorAvg)}.`;
 // Sparkline: real daily_history, oldest→newest, last 14 days. Not a
 // decorative chart -- same numbers as the Daily Cost History list below.
 const hist = (d.daily_history||[]).slice(0,14).slice().reverse();
 const costs = hist.map(r=>r.total_cost||0);
 const maxC = Math.max(...costs, 0.0001), minC = Math.min(...costs, 0);
 const pts = hist.length>1 ? hist.map((r,i)=>{
   const x = (i/(hist.length-1))*680;
   const y = 78 - ((r.total_cost-minC)/(maxC-minC||1))*66;
   return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ') : '';
 const evalRow = (e,label) => {
  if(!e) return '';
  const name = e.player
   ? `<b>${esc(e.player)}</b>${e.opponent?' <span class="m">vs</span> '+esc(e.opponent):''}${e.market?' &middot; '+esc(e.market):''}${e.line!=null?' '+e.line:''}`
   : `<span class="meta">${esc(e.eval_id)}</span>`;
  const when = (e.created_at||'').slice(0,10);
  return `<div class="rowline evalrow" onclick="openEval('${esc(e.eval_id)}')">
   <span>${label} &middot; ${name}${when?' <span class="meta">&middot; '+when+'</span>':''}</span>
   <span class="val">${moneyShort(e.total_cost)} <a>view →</a></span></div>`;
 };
 const costIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">Real spend tracking from the Apex Trace Engine — every traced API call, what it cost, and how long it took. This is actual metered cost, not an estimate.</div>
  <div class="m" style="margin-top:6px"><b>Highest/Lowest/Slowest/Fastest</b> and <b>Notable evaluations</b> show the player and match when a name can be matched from Dataset A. A raw ID instead of a name is an honest gap, not a bug — it means that specific trace predates Dataset A logging (started 2026-07-23) or has no linked evaluation record, not that something's broken.</div>
 </div>`;
 el.innerHTML = costIntro + `
 <div class="v6cost">
  <div class="hero">
  <div class="eyebrow">Cost Intelligence &middot; today</div>
  <div class="headline">${moneyShort(s.today)}</div>
  <div class="subline">${subline}</div>
  ${pts ? `<div class="chart"><svg viewBox="0 0 680 90" preserveAspectRatio="none">
   <polyline points="${pts}" fill="none" stroke="var(--grn)" stroke-width="1.6"/></svg></div>` : ''}
  <div class="tally">
   <div><div class="k">Last 7 days</div><div class="v">${moneyShort(s.last_7_days)}</div></div>
   <div><div class="k">Last 30 days</div><div class="v">${moneyShort(s.last_30_days)}</div></div>
   <div><div class="k">Lifetime</div><div class="v">${moneyShort(s.lifetime)}</div></div>
  </div>
  </div>

  <div class="sec">
   <div class="h">Evaluation metrics</div>
   <div class="rowline"><span>Evaluations traced</span><span class="val">${m.evaluations_traced}</span></div>
   <div class="rowline"><span>Traces captured</span><span class="val">${m.traces_captured}</span></div>
   <div class="rowline"><span>Avg cost / evaluation</span><span class="val">${money(m.avg_cost_per_evaluation)}</span></div>
   <div class="rowline"><span>Avg latency</span><span class="val">${(m.avg_latency_ms??0).toFixed(0)}ms</span></div>
   ${evalRow(m.highest_cost,'Highest cost')}
   ${evalRow(m.lowest_cost,'Lowest cost')}
   ${evalRow(m.slowest,'Slowest')}
   ${evalRow(m.fastest,'Fastest')}
  </div>

  <div class="sec">
   <div class="h">By model</div>
   <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap">
    ${d.model_breakdown.length?donutChart(d.model_breakdown.map((r,i)=>({label:r.model,value:r.total_cost,
      color:['var(--acc)','var(--grn)','var(--yel)','var(--red)','#9b8cff'][i%5]})), 140, moneyShort(d.model_breakdown.reduce((a,r)=>a+r.total_cost,0))):'<span class="m">no data yet</span>'}
    <div style="flex:1;min-width:200px">${d.model_breakdown.length?donutLegend(d.model_breakdown.map((r,i)=>({label:r.model,value:money(r.total_cost),
      color:['var(--acc)','var(--grn)','var(--yel)','var(--red)','#9b8cff'][i%5]}))):''}</div>
   </div>
   ${d.model_breakdown.map(r=>`<div class="rowline"><span>${esc(r.model)}<span class="meta"> &middot; ${r.call_count} calls &middot; ${(r.total_tokens_in??0).toLocaleString()} in / ${(r.total_tokens_out??0).toLocaleString()} out &middot; avg ${(r.avg_latency_ms??0).toFixed(0)}ms</span></span><span class="val">${money(r.total_cost)}</span></div>`).join('')||'<div class="m">no data yet</div>'}
  </div>

  <div class="sec">
   <div class="h">By step</div>
   ${d.step_type_breakdown.map(r=>`<div class="rowline"><span>${esc(r.step_type)}<span class="meta"> &middot; ${r.call_count} calls &middot; avg ${(r.avg_latency_ms??0).toFixed(0)}ms</span></span><span class="val">${money(r.total_cost)}</span></div>`).join('')||'<div class="m">no data yet</div>'}
  </div>

  <div class="sec">
   <div class="h">Notable evaluations</div>
   ${[...d.top_expensive.map(r=>({...r,tag:'expensive'})), ...d.top_slowest.map(r=>({...r,tag:'slow'}))]
     .slice(0,8).map(r=>{
      const name = r.player
       ? `<b>${esc(r.player)}</b>${r.opponent?' <span class="m">vs</span> '+esc(r.opponent):''}${r.market?' &middot; '+esc(r.market):''}${r.line!=null?' '+r.line:''}`
       : `<span class="meta">${esc(r.eval_id)}</span>`;
      const when = (r.created_at||'').slice(0,10);
      return `<div class="rowline evalrow" onclick="openEval('${esc(r.eval_id)}')">
       <span>${name}<span class="meta"> &middot; ${when} &middot; ${r.tag}</span></span>
       <span class="val">${r.tag==='slow'?(r.total_latency_ms??0)+'ms':money(r.total_cost)} <a>view →</a></span></div>`;
     }).join('')||'<div class="m">no data yet</div>'}
  </div>

  <div class="sec" style="padding-bottom:0">
   <div class="h">Daily cost history</div>
   ${d.daily_history.map(r=>`<div class="rowline"><span>${esc(r.date)}<span class="meta"> &middot; ${r.evaluation_count} evaluations</span></span><span class="val">${money(r.total_cost)}</span></div>`).join('')||'<div class="m">no data yet</div>'}
  </div>
 </div>`
}
function calibrationCC(d){
 const el = document.getElementById('v-calcc');
 const calIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">Calibration tracks whether provisional rules in the system are firing and actually improving picks. A rule is <b>READY FOR REVIEW</b> when enough picks that triggered it have settled to form a real picture. Until then, it shows "NOT ENOUGH DATA" rather than a misleading number.</div>
  <div class="m" style="margin-top:6px"><b>FIRED COUNT</b> — how many times this rule activated during evaluation. <b>SETTLED</b> — how many of those picks have a real HIT/BUST outcome recorded. <b>HIT RATE</b> — once enough picks settle, what percentage actually hit. This fills in as you settle picks in Pick Results.</div>
 </div>`;
 if(!d || d.error){ el.innerHTML = calIntro + `<div class="v6cost"><span class="m">Calibration data unavailable${d&&d.error?': '+esc(d.error):''}.</span></div>`; return }
 const total = d.total_rules||0, ready = d.ready_for_review||0;
 const subline = total===0
  ? 'No provisional rules registered yet in PROVISIONAL_RULES.'
  : `Rules below their minimum settled sample show as <b style="color:var(--yel)">NOT ENOUGH DATA</b> rather than a hit rate that would look precise but isn&rsquo;t &mdash; same discipline as everywhere else in Apex.`;
 const statusChip = s => s==='READY_FOR_REVIEW'
  ? '<span class="chip ok">READY FOR REVIEW</span>'
  : '<span class="chip B">NOT ENOUGH DATA</span>';
 const rows = (d.rules||[]).map(r=>{
  const toward = r.min_sample ? Math.min(1, (r.settled_count||0)/r.min_sample) : 0;
  return `<div class="rowline">
   <span>${esc(r.rule_name)}<span class="meta"> &middot; introduced ${esc(r.introduced||'—')} &middot; ${r.fired_count} fired &middot; ${r.settled_count}/${r.min_sample} settled</span>
    ${bar(toward)}</span>
   <span class="val">${statusChip(r.status)}${r.hit_rate!=null?` <span style="margin-left:8px">${pct(r.hit_rate)}</span>`:''}</span>
  </div>`;
 }).join('') || '<div class="m">no provisional rules registered</div>';
 const unregisteredSec = (d.unregistered||[]).length ? `
  <div class="sec">
   <div class="h">Fired before being registered</div>
   <div class="m" style="margin-bottom:10px">These rules have real fire events logged but no entry in PROVISIONAL_RULES yet &mdash; add them there to start tracking a real hit rate.</div>
   ${d.unregistered.map(name=>`<span class="chip A">${esc(name)}</span>`).join(' ')}
  </div>` : '';

 // --- Estimator-level calibration (2026-07-25, new) -------------------
 // Distinct from the rule-level calibration above: this scores whether
 // NUMERIC PRIMITIVE ESTIMATES (hold%, avg aces, etc) match reality --
 // Brier-score-style, not hit/miss. Second WHAT IS THIS SCREEN panel is
 // deliberate: these answer genuinely different questions and shouldn't
 // share one explainer, or the distinction gets lost.
 const ec = d.estimator_calibration || {};
 const ecIntro = `<div class="panel" style="margin:20px 0 12px">
  <h3 class="h">📖 What is this screen? — Estimator Calibration (different from the rules above)</h3>
  <div class="m">The rules above answer "did this RULE fire correctly." This section answers a different question: "do our NUMBER estimates (hold%, aces, etc) actually match what happens on court." A rule can look fine while the underlying numbers feeding it are quietly off, or vice versa -- these are tracked separately on purpose.</div>
  <div class="m" style="margin-top:6px">Requires <b>${ec.min_samples_for_verdict||30}+ settled samples</b> per primitive before rendering WELL_CALIBRATED or MISCALIBRATED -- one bad match is noise, not a verdict, same discipline as everywhere else in Apex. The chart plots predicted vs. actual per bucket; points near the dashed diagonal are well-calibrated, points far from it are not.</div>
 </div>`;

 let ecBody;
 if (ec.status === 'offline' || ec.table_exists === false) {
  ecBody = `<div class="panel"><h3 class="h">Estimator Calibration</h3>
   <span class="chip B">NOT WIRED IN YET</span>
   <div class="m" style="margin-top:8px">${esc(ec.message || 'primitive_estimates table does not exist yet. Logging needs to be added to dataset_a_capture.py and deployed before this section has anything to show.')}</div>
  </div>`;
 } else {
  const totalLogged = ec.total_logged || 0, totalSettled = ec.total_settled || 0;
  const goblinLogged = ec.goblin_conflicts_logged || 0, goblinSettled = ec.goblin_conflicts_settled || 0;
  const primRows = (ec.per_primitive||[]).filter(p=>p.total_logged>0).map(p=>{
   const v = (p.versions||[])[0]; // most recent estimator_version present
   const chip = !v ? '<span class="chip B">NO DATA</span>'
    : v.verdict==='INSUFFICIENT_DATA' ? `<span class="chip B">NOT ENOUGH DATA (${v.n_samples}/${ec.min_samples_for_verdict})</span>`
    : v.verdict==='WELL_CALIBRATED' ? '<span class="chip ok">WELL CALIBRATED</span>'
    : '<span class="chip A">MISCALIBRATED</span>';
   return `<div class="rowline"><span><b>${esc(p.primitive)}</b>
    <span class="meta"> &middot; ${p.total_logged} logged &middot; ${p.total_settled} settled</span></span>
    <span class="val">${chip}</span></div>`;
  }).join('') || '<div class="m">no primitives logged yet</div>';

  const highlighted = (ec.per_primitive||[]).find(p=>p.primitive===ec.highlighted_primitive);
  const highlightedVersion = highlighted && highlighted.versions && highlighted.versions[0];
  const diagram = highlightedVersion && highlightedVersion.reliability_buckets
   ? reliabilityDiagram(highlightedVersion.reliability_buckets, 420, 260)
   : '<span class="m">not enough settled data yet to plot</span>';

  ecBody = `
  <div class="strip" style="grid-template-columns:repeat(4,1fr)">
   <div class="tile"><div class="t">Predictions logged</div><div class="v">${totalLogged}</div></div>
   <div class="tile"><div class="t">Settled</div><div class="v">${totalSettled}</div></div>
   <div class="tile"><div class="t">Goblin/demon conflicts</div><div class="v">${goblinLogged}</div>
    <div class="m">${goblinSettled} settled &middot; free calibration data</div></div>
   <div class="tile"><div class="t">Min. sample for verdict</div><div class="v">${ec.min_samples_for_verdict||30}</div></div>
  </div>
  <div class="row">
   <div class="panel"><h3 class="h">Per-primitive calibration</h3>${primRows}</div>
   <div class="panel"><h3 class="h">Reliability diagram${ec.highlighted_primitive ? ' &mdash; '+esc(ec.highlighted_primitive) : ''}</h3>${diagram}</div>
  </div>`;
 }

 el.innerHTML = calIntro + `
 <div class="v6cost">
  <div class="hero">
   <div class="eyebrow">Calibration &middot; provisional rules</div>
   <div class="headline">${ready}/${total} ready for review</div>
   <div class="subline">${subline}</div>
  </div>
  <div class="sec">
   <div class="h">Rules</div>
   ${rows}
  </div>
  ${unregisteredSec}
 </div>` + ecIntro + ecBody;
}
function successCC(s, cost, val, bankroll, wh, conf){
 const el = document.getElementById('v-success');
 const g = s.green, m = s.ml, sl = s.slips;
 // Settled = wins + losses per tracker. Real tracker fields from /api/state.
 const gSettled = (g.wins||0) + (g.losses||0);
 const mSettled = (m.wins||0) + (m.losses||0);
 const settled = gSettled + mSettled;
 const em = cost && cost.evaluation_metrics ? cost.evaluation_metrics : null;
 const costPerEval = em && em.avg_cost_per_evaluation != null ? '$'+em.avg_cost_per_evaluation.toFixed(4) : '\u2014';
 // Cost per settled pick: real ATE lifetime spend / real settled count.
 // Honest caveat rendered below: cost side counts ATE-traced spend only,
 // so picks settled before ATE existed are in the denominator but their
 // spend is not in the numerator. Real numbers, stated limitation.
 const costPerSettled = (cost && cost.summary && settled > 0)
  ? '$'+(cost.summary.lifetime/settled).toFixed(2) : (settled===0 ? 'n/a \u2014 no settled picks' : '\u2014');
 // Calibration: real run count from /api/validation. No runs = Pending, no fake %.
 const calRuns = val && typeof val.runs === 'number' ? val.runs : null;
 const latest = calRuns && val.history && val.history.length ? val.history[0] : null;
 const calBody = calRuns === null
  ? '<span class="m">Validation API unavailable</span>'
  : calRuns === 0
   ? `<span class="chip" style="color:var(--yel);border-color:var(--yel)">PENDING VALIDATION</span>
      <div class="m" style="margin-top:8px">No validation runs recorded. On the Mac Mini run:<br>
      <b>python3 green_validation_suite.py --build-manifest</b> then <b>--run</b></div>`
   : `<span class="chip" style="color:var(--grn);border-color:var(--grn)">${calRuns} RUN${calRuns>1?'S':''} RECORDED</span>
      ${latest ? `<div style="margin-top:10px;font-size:13px">Latest run: <b>${latest.green_hits}/${latest.green_known}</b> known GREENs hit
       &middot; preserved ${latest.preserved} &middot; lost ${latest.lost}</div>` : ''}
      <div class="m" style="margin-top:6px">Full history on the Validation screen.</div>`;
 const rate = (hit, n) => n === 0 ? 'n/a' : (hit == null ? 'n/a' : pct(hit));
 const marketDonut = settled>0 ? `<div class="panel"><h3 class="h">Settled picks by market</h3>
  <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap">
   ${donutChart([{label:'Props',value:gSettled,color:'var(--acc)'},{label:'Moneyline',value:mSettled,color:'var(--grn)'}], 140, settled+' total')}
   <div style="flex:1;min-width:180px">${donutLegend([{label:'Props',value:gSettled,color:'var(--acc)'},{label:'Moneyline',value:mSettled,color:'var(--grn)'}])}</div>
  </div></div>` : '';
 el.innerHTML = marketDonut + `
 <div class="strip" style="grid-template-columns:repeat(3,1fr)">
  <div class="tile"><div class="t">Settled picks</div><div class="v">${settled}</div>
   <div class="m">props ${gSettled} &middot; moneyline ${mSettled}</div></div>
  <div class="tile"><div class="t">Win rate &mdash; props</div><div class="v">${rate(g.hit, gSettled)}</div>
   <div class="m">${g.wins||0}W &middot; ${g.losses||0}L${gSettled===0?' &middot; no settled props yet':''}</div></div>
  <div class="tile"><div class="t">Win rate &mdash; moneyline</div><div class="v">${rate(m.hit, mSettled)}</div>
   <div class="m">${m.wins||0}W &middot; ${m.losses||0}L${mSettled===0?' &middot; no settled moneyline yet':''}</div></div>
 </div>
 <div class="strip" style="grid-template-columns:repeat(3,1fr)">
  <div class="tile"><div class="t">Slip ROI</div><div class="v">${sl && sl.n ? pct(sl.roi) : 'n/a'}</div>
   <div class="m">${sl && sl.n ? `${sl.n} slips &middot; profit $${sl.profit}` : 'no slips logged yet'}</div></div>
  <div class="tile"><div class="t">Cost per evaluation</div><div class="v">${costPerEval}</div>
   <div class="m">${em ? em.evaluations_traced+' evaluations traced' : 'ATE unavailable'}</div></div>
  <div class="tile"><div class="t">Cost per settled pick</div><div class="v">${costPerSettled}</div>
   <div class="m">ATE-traced spend only &mdash; pre-ATE picks lack cost capture</div></div>
 </div>
 <div class="panel"><h3 class="h">Confidence calibration</h3>${calBody}</div>
 <div class="panel"><h3 class="h">ROI over time (daily P&amp;L)</h3>${bankroll&&bankroll.daily?barChart(bankroll.daily, 900, 140):'<span class="m">no slip history yet</span>'}</div>
 <div class="panel"><h3 class="h">Performance by market</h3>
  ${wh && wh.outcomes && wh.outcomes.by_market && wh.outcomes.by_market.length ? (() => {
    const byMkt = {};
    wh.outcomes.by_market.forEach(r => { byMkt[r.market] = byMkt[r.market]||{hit:0,bust:0}; if(r.outcome==='HIT')byMkt[r.market].hit=r.n; if(r.outcome==='BUST')byMkt[r.market].bust=r.n; });
    return Object.entries(byMkt).filter(([m,v])=>v.hit+v.bust>0).sort((a,b)=>(b[1].hit+b[1].bust)-(a[1].hit+a[1].bust))
     .map(([m,v])=>`<div class="rowline"><span>${esc(m)}<span class="meta"> &middot; ${v.hit+v.bust} settled</span></span><span class="val">${pct(v.hit/(v.hit+v.bust))}</span></div>`).join('');
  })() : '<span class="m">no settled markets yet</span>'}
 </div>
 <div class="panel"><h3 class="h">📖 What is this screen?</h3>
  <div class="m">The bottom-line scoreboard for the whole system — real settled outcomes only, nothing projected. Settled means a pick has an actual HIT or BUST recorded, not just a posted confidence. Win rates and slip ROI only appear once there's a real settled sample; until then they show "n/a" rather than a number that would look precise but isn't.</div>
  <div class="m" style="margin-top:6px">Cost per evaluation/settled pick comes from the Apex Trace Engine's real per-call spend tracking — it only covers spend traced since ATE existed, so older picks can be in the settled count without matching spend in the numerator.</div>
 </div>
 <div class="panel"><h3 class="h">Recent settled picks</h3>
  ${conf && conf.recent && conf.recent.length ? conf.recent.filter(r=>r.final_grade).slice(0,8).map(r=>
   `<div class="rowline evalrow" onclick="openEval('${esc(r.eval_id)}')"><span><b>${esc(r.player)}</b> ${r.line??''} ${esc(r.market||'')} ${esc(r.direction||'')}
    <span class="meta"> &middot; ${esc(r.lifecycle_updated_at||'').slice(0,16)}</span></span><span class="val">${pct(r.final_confidence)} <a>view &rarr;</a></span></div>`).join('')
   : '<span class="m">no scored picks yet</span>'}
 </div>
 <div class="panel"><h3 class="h">Coming in Phase 2</h3>
  <div class="m">Cost per winning pick &middot; per-market performance &middot; per-model win rate &middot; per-rule W-L record &middot; success trends.
  Both stated blockers are now cleared (rule_activations.eval_id linkage confirmed wired via call_model's trace_id_out; the memory-node date field added to get_graph_data()) — the join/aggregation code for these 5 metrics itself still hasn't been built. No numbers shown until that layer exists and real settled data has accumulated.</div></div>`;
}
let CHAT_SESSION = null;
function newChatSession(){ CHAT_SESSION = 'sess-' + Date.now() + '-' + Math.random().toString(36).slice(2,8) }

const CHAT_SOURCE_LABELS = {
 get_evaluation: 'Dataset A', get_trace_bundle: 'ATE Trace',
 get_recent_activations: 'Rule Activations', get_rule_details: 'Rule Activations',
 get_cost_summary: 'Cost Intelligence', search_investigations: 'Investigation Record',
 search_decisions: 'Decision Record', search_rules: 'Rule Activations',
 get_dataset_b_player: 'Dataset B',
};
const CHAT_STARTER_PROMPTS = [
 'Why did confidence drop?', 'Show Rule 30 activations.', 'Why was this evaluation GREEN?',
 'What did Apex cost this week?', 'Show recent investigations.'
];

function chatCC(){
 if(!CHAT_SESSION) newChatSession();
 const el = document.getElementById('v-chatcc');
 el.innerHTML = `
 <div style="display:grid;grid-template-columns:1fr 300px;gap:12px">
  <div class="panel" style="margin:0;display:flex;flex-direction:column">
   <div style="display:flex;gap:10px;margin-bottom:12px;align-items:center">
    <label class="m">Model <select id="modelSelect" onchange="saveChatSelectors()"></select></label>
    <label class="m">Profile <select id="profileSelect" onchange="saveChatSelectors()"></select></label>
   </div>
   <div id="chatMessages" style="flex:1;max-height:58vh;overflow-y:auto;margin-bottom:12px"></div>
   <div id="chatStarters" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px"></div>
   <div style="display:flex;gap:8px">
    <input id="chatInput" placeholder="Ask about an evaluation, cost, a rule, a decision\u2026"
     style="flex:1;padding:10px 12px" onkeydown="if(event.key=='Enter')sendChatMessage()">
    <button class="btn-primary" onclick="sendChatMessage()">Ask</button>
   </div>
   <div class="m" style="margin-top:8px">Read-only \u2014 explains, searches, and correlates Apex's own data. Never modifies anything.</div>
  </div>
  <div>
   <div class="panel" style="margin:0 0 12px">
    <h3 class="h">What you can ask</h3>
    ${[['Evaluations','Explain an eval or score'],['Rules','Why did a rule fire or fail?'],
       ['Costs','Show cost breakdowns'],['Decisions','Why was this decision made?'],
       ['Investigations','Show or explain investigations']].map(([t,d])=>
      `<div style="padding:6px 0;border-bottom:1px solid var(--hair)"><b style="font-size:12.5px">${t}</b><div class="m">${d}</div></div>`).join('')}
   </div>
   <div class="panel" style="margin:0 0 12px">
    <h3 class="h">Recent conversations <span style="float:right;cursor:pointer;color:var(--acc);text-transform:none;letter-spacing:0" onclick="startNewChat()">+ New</span></h3>
    <div id="chatSessionList" class="m">loading\u2026</div>
   </div>
   <div class="panel" style="margin:0" id="chatSystemContext">
    <h3 class="h">System context</h3><div class="m">loading\u2026</div>
   </div>
  </div>
 </div>`;
 renderChatStarters();
 loadChatSessionList();
 loadChatOptions();
 loadChatSystemContext();
}
async function loadChatSystemContext(){
 // Real fields only: warehouse health/freshness already exist via
 // /api/warehouse_cc; no fabricated "Active Agent"/"Time Range" state.
 const el = document.getElementById('chatSystemContext');
 if(!el) return;
 try{
  const d = await (await fetch('/api/warehouse_cc')).json();
  const rc = (d && d.readiness) || {};
  const cs = (d && d.collection) || {};
  el.innerHTML = `<h3 class="h">System context</h3>
   <div class="rowline"><span class="m">Warehouse health</span> <b>${esc(rc.warehouse_health||'\u2014')}</b></div>
   <div class="rowline"><span class="m">Last ingestion</span> <b>${esc(cs.last_successful_run||'never')}</b></div>
   <div class="rowline"><span class="m">Freshness</span> <b>${esc(cs.collection_freshness||'\u2014')}</b></div>`;
 } catch(e){ el.innerHTML = '<h3 class="h">System context</h3><div class="m">unavailable</div>'; }
}



const CHAT_MODEL_KEY_STORAGE = 'apex_chat_model_key';
const CHAT_PROFILE_KEY_STORAGE = 'apex_chat_profile_key';

function saveChatSelectors(){
 try {
  localStorage.setItem(CHAT_MODEL_KEY_STORAGE, document.getElementById('modelSelect').value);
  localStorage.setItem(CHAT_PROFILE_KEY_STORAGE, document.getElementById('profileSelect').value);
 } catch(e) {}
}

async function loadChatOptions(){
 const opts = await (await fetch('/api/command_chat_options')).json();
 let savedModel, savedProfile;
 try {
  savedModel = localStorage.getItem(CHAT_MODEL_KEY_STORAGE);
  savedProfile = localStorage.getItem(CHAT_PROFILE_KEY_STORAGE);
 } catch(e) {}
 const modelSel = document.getElementById('modelSelect');
 const profileSel = document.getElementById('profileSelect');
 if(modelSel) modelSel.innerHTML = (opts.models||[]).map(m=>
  `<option value="${esc(m.key)}" ${m.key===(savedModel||opts.default_model)?'selected':''}>${esc(m.label)}</option>`).join('');
 if(profileSel) profileSel.innerHTML = (opts.profiles||[]).map(p=>
  `<option value="${esc(p.key)}" ${p.key===(savedProfile||opts.default_profile)?'selected':''}>${esc(p.label)}</option>`).join('');
}

function renderChatStarters(){
 const box = document.getElementById('chatStarters');
 if(!box) return;
 const hasMessages = document.getElementById('chatMessages').children.length > 0;
 box.style.display = hasMessages ? 'none' : 'flex';
 box.innerHTML = hasMessages ? '' : CHAT_STARTER_PROMPTS.map((p,i)=>
  `<span class="chip" style="cursor:pointer" data-prompt-i="${i}" onclick="useStarterPrompt(this.dataset.promptI)">${esc(p)}</span>`).join('');
}
function useStarterPrompt(i){
 document.getElementById('chatInput').value = CHAT_STARTER_PROMPTS[i];
 sendChatMessage();
}

async function loadChatSessionList(){
 const box = document.getElementById('chatSessionList');
 if(!box) return;
 const sessions = await (await fetch('/api/command_chat_sessions')).json();
 box.innerHTML = sessions.length ? sessions.map(s=>
  `<div class="click" style="padding:8px 6px;border-bottom:1px solid var(--hair);border-radius:8px" data-session-id="${esc(s.session_id)}" onclick="reopenChatSession(this.dataset.sessionId)">
   <div style="font-size:12px;color:var(--txt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(s.preview||'(no preview)')}</div>
   <div class="m">${s.message_count} msgs · ${esc((s.last_at||'').slice(0,16))}</div></div>`
 ).join('') : '<span class="m">no sessions yet</span>';
}

async function reopenChatSession(sessionId){
 CHAT_SESSION = sessionId;
 window._chatEvalContext = null;
 document.getElementById('chatContextChip')?.remove();
 const msgs = await (await fetch('/api/command_chat_session_detail?session_id='+encodeURIComponent(sessionId))).json();
 document.getElementById('chatMessages').innerHTML = '';
 msgs.forEach(m=>appendChatMessage(m.role, m.content, m.sources_cited));
 renderChatStarters();
}

function startNewChat(){
 newChatSession();
 window._chatEvalContext = null;
 document.getElementById('chatMessages').innerHTML = '';
 document.getElementById('chatContextChip')?.remove();
 const inputEl = document.getElementById('chatInput');
 if(inputEl) inputEl.placeholder = 'Ask about an evaluation, cost, a rule, a decision...';
 renderChatStarters();
 loadChatSessionList();
}

function appendChatMessage(role, text, sourcesCited, modelUsed, profileUsed){
 const box = document.getElementById('chatMessages');
 const isUser = role === 'user';
 const sources = (sourcesCited||[]).map(s=>{
  const label = CHAT_SOURCE_LABELS[s.tool] || s.tool;
  const detail = s.input ? (': ' + JSON.stringify(s.input)) : '';
  // Clickable only where we already have enough data in sourcesCited
  // to link somewhere real -- no new backend field added for this,
  // per the explicit "no backend work" constraint. search_decisions/
  // search_investigations only carry the query that was run (not
  // which record matched), so they re-run that search in Memory
  // Center rather than guessing at a specific record ID we don't have.
  let onclick = '';
  if((s.tool === 'search_decisions' || s.tool === 'search_investigations') && s.input && s.input.query){
   onclick = ` onclick="memGoToSearch('${esc(s.input.query).replace(/'/g,"\\'")}')"`;
  } else if((s.tool === 'get_evaluation' || s.tool === 'get_trace_bundle') && s.input && s.input.eval_id){
   onclick = ` onclick="openEval('${esc(s.input.eval_id)}')"`;
  }
  return `<span${onclick} title="${esc(s.tool + detail)}">${esc(label)}</span>`;
 }).join('');
 const modelLine = (!isUser && (modelUsed || profileUsed))
  ? `<div class="m" style="margin-top:6px;font-size:11px">Model: ${esc(modelUsed||'?')} · Profile: ${esc(profileUsed||'?')}</div>` : '';
 const row = document.createElement('div');
 row.className = 'msg ' + (isUser ? 'user' : 'assistant');
 row.style.cssText = 'animation:mcFadeIn var(--t-base) var(--ease)';
 row.innerHTML = isUser
  ? `<div class="bubble">${esc(text)}</div>`
  : `<div class="who">Apex</div><div class="bubble">${esc(text)}</div>
     ${sources ? `<div class="src">${sources}</div>` : ''}${modelLine}`;
 box.appendChild(row);
 box.scrollTop = box.scrollHeight;
}

function showThinking(){
 const box = document.getElementById('chatMessages');
 const row = document.createElement('div');
 row.id = 'chatThinking';
 row.className = 'msg assistant';
 row.style.cssText = 'animation:mcFadeIn var(--t-base) var(--ease)';
 // No fake streaming/typing simulation (standing requirement from
 // Command Chat UX V1.1) -- the pulse is an honest "waiting" signal,
 // not a simulation of the answer being generated. Per the motion
 // research: a smoothly-animated wait state measurably improves
 // perceived performance over a static one, even at identical
 // real latency.
 row.innerHTML = `<div class="who">Apex</div><div class="bubble" style="color:var(--dim);display:flex;align-items:center;gap:6px">
  <span class="mc-thinking-dots"><span></span><span></span><span></span></span>
  Apex Command Chat is thinking</div>`;
 box.appendChild(row);
 box.scrollTop = box.scrollHeight;
}
function hideThinking(){ const t = document.getElementById('chatThinking'); if(t) t.remove(); }

async function sendChatMessage(){
 const inputEl = document.getElementById('chatInput');
 const message = (inputEl.value || '').trim();
 if(!message) return;
 inputEl.value = '';
 appendChatMessage('user', message);
 renderChatStarters();
 showThinking();
 const modelKey = document.getElementById('modelSelect')?.value || null;
 const profileKey = document.getElementById('profileSelect')?.value || null;
 const body = {session_id: CHAT_SESSION, message, eval_id_context: window._chatEvalContext || null,
               model_key: modelKey, profile_key: profileKey};
 let d;
 try {
  d = await (await fetch('/api/command_chat', {method:'POST', body: JSON.stringify(body)})).json();
 } catch(e) {
  d = {error: 'request failed'};
 }
 hideThinking();
 appendChatMessage('assistant', d.answer || d.error || '(no answer)', d.sources_cited, d.model_used, d.profile_used);
 loadChatSessionList();
}

function askAboutThisEval(evalId, friendlyLabel){
 VIEW='chatcc';
 document.querySelectorAll('#side a').forEach(x=>{if(x.dataset.v=='chatcc')x.classList.add('on');else x.classList.remove('on')});
 document.querySelectorAll('#main>div').forEach(d=>d.style.display='none');
 document.getElementById('v-chatcc').style.display='block';
 chatCC();
 insp.classList.remove('open');
 window._chatEvalContext = evalId;
 const label = friendlyLabel || evalId;
 document.getElementById('chatInput').placeholder = `Ask about ${label}...`;
 const panel = document.querySelectorAll('#v-chatcc .panel')[0];
 if(panel){
  const chip = document.createElement('div');
  chip.id = 'chatContextChip';
  chip.style.cssText = 'border-bottom:1px solid var(--hair);padding:0 0 10px;margin-bottom:20px;display:flex;justify-content:space-between;align-items:center';
  chip.innerHTML = `<span class="m">Context: <b style="color:var(--txt)">${esc(label)}</b> <span style="color:var(--dim)">(${esc(evalId)})</span></span><span style="cursor:pointer;color:var(--dim)" onclick="clearChatContext()">✕</span>`;
  panel.insertBefore(chip, document.getElementById('chatMessages'));
 }
}
function clearChatContext(){
 window._chatEvalContext = null;
 const chip = document.getElementById('chatContextChip');
 if(chip) chip.remove();
 document.getElementById('chatInput').placeholder = 'Ask about an evaluation, cost, a rule, a decision...';
}

let _cytoscapeLoading = null;
function loadCytoscape(){
 if(window.cytoscape) return Promise.resolve();
 if(_cytoscapeLoading) return _cytoscapeLoading;
 _cytoscapeLoading = new Promise((resolve, reject) => {
  const s = document.createElement('script');
  s.src = 'https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js';
  s.onload = resolve;
  s.onerror = reject;
  document.head.appendChild(s);
 });
 return _cytoscapeLoading;
}

const MEMCC_NODE_COLORS = {decision:'#5b64f2', investigation:'#e2b93b', system:'#3ba3e8', rule:'#9094a3'};
// system was green (#31c48d) -- re-colored per the V3 visual design review's
// 3-color rule (red=alert, green=good/healthy, grey=neutral, never
// decorative): a green Memory Center node has nothing to do with system
// health, but green means exactly that everywhere else in this app.

let MEM_DATA = null, MEM_VIEW = 'library', MEM_TYPE_FILTER = 'all', MEM_SEARCH_Q = '';
async function memCC(){
 const el = document.getElementById('v-memcc');
 el.innerHTML = '<div class="m" style="padding:20px 0">Loading memory graph\u2026</div>';
 try { MEM_DATA = await (await fetch('/api/memory_graph')).json(); }
 catch(e){ MEM_DATA = {nodes:[],edges:[]}; }
 memRenderShell();
}
function memRenderShell(){
 // Simplified per direct request: one column, one list, no
 // KPI-tile strip, no card grid, no two-mode toggle competing for
 // attention. The interactive node graph still fully works underneath
 // (memInitGraph/memSearch/memApplyFilters untouched) -- it's just no
 // longer the default, equal-weight option. One link at the bottom
 // opens it for anyone who wants it; everyone else never sees it.
 const data = MEM_DATA || {nodes:[],edges:[]};
 const byType = {}; data.nodes.forEach(n=>byType[n.type]=(byType[n.type]||0)+1);
 const el = document.getElementById('v-memcc');
 const memIntro = `<div class="panel" style="margin-bottom:12px">
  <h3 class="h">📖 What is this screen?</h3>
  <div class="m">A searchable record of decisions and investigations Command Chat has captured — not raw picks, but the reasoning and conclusions behind system-level choices (why a rule was added, what an investigation found). Records get added when you use Command Chat, not automatically from every evaluation.</div>
  <div class="m" style="margin-top:6px">The list view is the default because it's searchable and readable. The same data is also available as an interactive node graph — link at the bottom — for anyone who wants to see how records connect to each other visually.</div>
 </div>`;
 el.innerHTML = memIntro + `
  <input id="memLibSearch" value="${esc(MEM_SEARCH_Q)}" placeholder="Search decisions, investigations, rules, systems\u2026"
   style="width:100%;padding:10px 12px;margin-bottom:10px" oninput="memSetSearch(this.value)">
  <div class="m" style="margin-bottom:14px">${data.nodes.length} records \u2014 ${byType.decision||0} decisions &middot; ${byType.investigation||0} investigations &middot;
   ${byType.system||0} systems &middot; ${byType.rule||0} rules</div>
  <div id="memTabs" style="display:flex;gap:18px;border-bottom:1px solid var(--edge);margin-bottom:4px"></div>
  <div id="memList"></div>
  <div class="m" style="margin-top:18px">Add records via Command Chat &middot; <span style="cursor:pointer;color:var(--acc)" onclick="document.querySelector('[data-v=chatcc]').click()">Open Command Chat</span>
   &middot; <span style="cursor:pointer;color:var(--acc)" onclick="memOpenGraph()">View as interactive graph &rarr;</span></div>
  <div id="memGraphWrap" style="display:none;margin-top:16px"></div>`;
 memRenderList();
}
function memSetSearch(v){ MEM_SEARCH_Q = v; memRenderList(); const i=document.getElementById('memLibSearch'); if(i){i.focus(); i.setSelectionRange(v.length,v.length);} }
function memSetTypeFilterLib(t){ MEM_TYPE_FILTER = t; memRenderList(); }
function memRenderList(){
 const data = MEM_DATA || {nodes:[],edges:[]};
 const degree = {}; data.edges.forEach(e=>{degree[e.source]=(degree[e.source]||0)+1;degree[e.target]=(degree[e.target]||0)+1;});
 const q = MEM_SEARCH_Q.trim().toLowerCase();
 const counts = {}; data.nodes.forEach(n=>counts[n.type]=(counts[n.type]||0)+1);
 const colors = {decision:'#5b64f2',investigation:'#e2b93b',system:'#3ba3e8',rule:'#9094a3'};
 const tabs = [['all','All',data.nodes.length],['decision','Decisions',counts.decision||0],
   ['investigation','Investigations',counts.investigation||0],['system','Systems',counts.system||0],['rule','Rules',counts.rule||0]];
 const tabsEl = document.getElementById('memTabs');
 if(tabsEl) tabsEl.innerHTML = tabs.map(([key,label,n])=>
   `<span style="cursor:pointer;padding:8px 0;font-size:12.5px;color:${MEM_TYPE_FILTER===key?'var(--txt)':'var(--dim)'};
    border-bottom:2px solid ${MEM_TYPE_FILTER===key?'var(--acc)':'transparent'}" onclick="memSetTypeFilterLib('${key}')">${label} (${n})</span>`).join('');
 let nodes = data.nodes.filter(n => MEM_TYPE_FILTER==='all' || n.type===MEM_TYPE_FILTER);
 if(q) nodes = nodes.filter(n => (n.label||'').toLowerCase().includes(q));
 nodes = nodes.slice().sort((a,b) => (degree[b.id]||0)-(degree[a.id]||0));
 const list = document.getElementById('memList');
 if(!list) return;
 if(!nodes.length){ list.innerHTML = `<div class="m" style="padding:20px 0;text-align:center">No records match${q?' "'+esc(q)+'"':''}.</div>`; return; }
 list.innerHTML = nodes.map((n,i) => {
   const shortId = (n.id||'').includes(':') ? n.id.split(':')[1] : n.id;
   return `<div class="rowline" style="cursor:pointer" onclick="memToggleRow(this,'${esc(n.type)}','${esc(shortId).replace(/'/g,"\\'")}','${esc(n.label).replace(/'/g,"\\'")}')">
    <div style="display:flex;justify-content:space-between;align-items:center">
     <span><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${colors[n.type]};margin-right:8px"></span>
      <b>${esc(n.label)}</b></span>
     <span class="m">${degree[n.id]||0} link${(degree[n.id]||0)!=1?'s':''} <span style="color:var(--dim2)">&#9662;</span></span>
    </div>
    <div class="memRowDetail" id="memRow${i}" style="display:none;padding:10px 0 4px 15px"></div>
   </div>`;
 }).join('');
}
async function memToggleRow(rowEl, type, id, label){
 const box = rowEl.querySelector('.memRowDetail');
 if(!box) return;
 if(box.style.display === 'block'){ box.style.display = 'none'; return; }
 // Close any other open row (one thing open at a time keeps this simple)
 document.querySelectorAll('.memRowDetail').forEach(b => { if(b !== box) b.style.display = 'none'; });
 box.style.display = 'block';
 if(box.dataset.loaded) return;  // real data only fetched once per row, cached in the DOM after that
 box.innerHTML = '<span class="m">loading\u2026</span>';
 try {
  const [record, related] = await Promise.all([
   (await fetch(`/api/memory_record?type=${encodeURIComponent(type)}&id=${encodeURIComponent(id)}`)).json(),
   (await fetch(`/api/memory_related?type=${encodeURIComponent(type)}&id=${encodeURIComponent(id)}`)).json(),
  ]);
  const fields = Object.entries(record).filter(([k]) => !['error'].includes(k))
   .map(([k,v]) => `<div class="m" style="margin-top:3px"><b style="color:var(--txt)">${esc(k)}:</b> ${esc(v)}</div>`).join('');
  const rel = [...(related.outgoing||[]).map(r => `${esc(r.edge_type)} \u2192 ${esc(r.target_type)}:${esc(r.target_id)}`),
    ...(related.incoming||[]).map(r => `${esc(r.source_type)}:${esc(r.source_id)} \u2192 ${esc(r.edge_type)}`)];
  box.innerHTML = (fields || '<div class="m">no detail record</div>') +
   (rel.length ? `<div class="m" style="margin-top:8px">Related: ${rel.map(esc).join(' &middot; ')}</div>` : '');
  box.dataset.loaded = '1';
 } catch(e){ box.innerHTML = '<span class="m">could not load detail</span>'; }
}
function memOpenGraph(){
 const wrap = document.getElementById('memGraphWrap');
 if(!wrap) return;
 wrap.style.display = 'block';
 wrap.innerHTML = `
  <div class="v6mem" style="position:relative;height:60vh;margin:0;border-radius:14px;overflow:hidden;border:1px solid var(--edge)">
   <div id="memGraphCanvas" class="memcanvas"></div>
   <div class="floatsearch">
    <input id="memSearchInput" placeholder="Find a decision or investigation\u2026" onkeydown="if(event.key=='Enter')memSearch()">
    <div id="memSearchResults" class="m" style="margin-top:6px"></div>
   </div>
   <div class="filters" id="memFilters">
    ${[['decision','#5b64f2'],['investigation','#e2b93b'],['system','#3ba3e8'],['rule','#9094a3']].map(([t,c])=>
      `<label><input type="checkbox" checked data-mem-filter="${t}" onchange="memApplyFilters()" style="width:auto">
       <span class="sw" style="background:${c}"></span>${t}</label>`).join('')}
   </div>
   <div class="memdetail" id="memDetailPanel"></div>
  </div>
  <div class="m" style="margin-top:8px;cursor:pointer;color:var(--acc)" onclick="document.getElementById('memGraphWrap').style.display='none'">&larr; Back to list</div>`;
 loadCytoscape().then(() => memInitGraph()).catch(() => {
  document.getElementById('memGraphCanvas').innerHTML =
   '<div class="m" style="padding:10px">Could not load the graph library \\u2014 check network access to cdnjs.cloudflare.com.</div>';
 });
}


let MEM_CY = null;
async function memInitGraph(){
 const data = await (await fetch('/api/memory_graph')).json();
 memFillStats(data);
 memFillInsights(data);
 const elements = [
  ...data.nodes.map(n => ({data: {id: n.id, label: n.label, type: n.type}})),
  ...data.edges.map(e => ({data: {id: e.id, source: e.source, target: e.target, label: e.label}})),
 ];
 MEM_CY = cytoscape({
  container: document.getElementById('memGraphCanvas'),
  elements,
  style: [
   {selector:'node', style:{'label':'data(label)','font-size':'10px','color':'#e8e8ec',
     'text-valign':'bottom','text-margin-y':6,'width':24,'height':24,
     'text-background-color':'#0b0c11','text-background-opacity':0.7,'text-background-padding':'2px',
     'background-color': (ele) => MEMCC_NODE_COLORS[ele.data('type')] || '#8b8d98',
     'transition-property':'width,height,border-width','transition-duration':'0.18s',
     'transition-timing-function':'ease-out'}},
   {selector:'node:selected', style:{'width':34,'height':34,'border-width':3,'border-color':'#5b64f2'}},
   {selector:'edge', style:{'width':1,'line-color':'#3a3b46','target-arrow-color':'#3a3b46',
     'target-arrow-shape':'triangle','curve-style':'bezier','font-size':'7px','label':'data(label)','color':'#8b8d98'}},
  ],
  layout: {name:'cose', animate:false, padding:60},
 });
 MEM_CY.on('tap', 'node', (evt) => memShowDetail(evt.target.data('type'), evt.target.data('id').split(':')[1] || evt.target.data('id'), evt.target.data('label')));
}

function memFillInsights(data){
 // Real, computed relationships over the actual edges already
 // fetched -- not narrative text. Degree-based and DEPENDS_ON-based
 // counts only; nothing here is invented copy.
 const el = document.getElementById('memInsights');
 if(!el) return;
 const byId = {}; data.nodes.forEach(n => byId[n.id] = n);
 const depends = {}; // node -> count of nodes that depend on it
 data.edges.forEach(e => { if((e.label||'').toUpperCase().includes('DEPEND')) depends[e.target] = (depends[e.target]||0)+1; });
 const items = [];
 const topDepend = Object.entries(depends).sort((a,b)=>b[1]-a[1])[0];
 if(topDepend && byId[topDepend[0]]) items.push(`<b>${esc(byId[topDepend[0]].label)}</b> has ${topDepend[1]} node${topDepend[1]>1?'s':''} depending on it`);
 const decisions = data.nodes.filter(n=>n.type==='decision').length;
 const investigations = data.nodes.filter(n=>n.type==='investigation').length;
 items.push(`${decisions} decision${decisions!=1?'s':''} and ${investigations} investigation${investigations!=1?'s':''} tracked in the graph`);
 const deg = {}; data.edges.forEach(e => { deg[e.source]=(deg[e.source]||0)+1; deg[e.target]=(deg[e.target]||0)+1; });
 const isolated = data.nodes.filter(n => !deg[n.id]).length;
 if(isolated) items.push(`${isolated} node${isolated>1?'s have':' has'} no recorded relationships yet`);
 el.innerHTML = items.length ? items.map(t=>`<div class="rowline">${t}</div>`).join('') : '<span class="m">not enough graph data yet for computed insights</span>';
}
function memFillStats(data){
 // Computed entirely from the real /api/memory_graph payload already
 // fetched for the graph itself -- zero extra backend calls. Growth-
 // over-time is deliberately absent: nodes carry no date field in this
 // payload (Phase 2 verification item), and we don't fake trends.
 const el = document.getElementById('memStatsPanel');
 if(!el) return;
 const byType = {};
 data.nodes.forEach(n => byType[n.type] = (byType[n.type]||0)+1);
 const degree = {};
 data.edges.forEach(e => { degree[e.source]=(degree[e.source]||0)+1; degree[e.target]=(degree[e.target]||0)+1; });
 let topId = null, topDeg = 0;
 Object.entries(degree).forEach(([id,d]) => { if(d > topDeg){ topDeg = d; topId = id; } });
 const topNode = topId ? data.nodes.find(n => n.id === topId) : null;
 const row = (k,v) => `<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:12px"><span class="m">${k}</span><b>${v}</b></div>`;
 el.innerHTML = `<h3 class="h">Graph stats</h3>
  ${row('Total nodes', data.nodes.length)}
  ${row('Total edges', data.edges.length)}
  ${row('Decisions', byType.decision||0)}
  ${row('Investigations', byType.investigation||0)}
  ${row('Systems', byType.system||0)}
  ${row('Rules', byType.rule||0)}
  ${topNode ? `<div style="margin-top:8px;border-top:1px solid var(--hair);padding-top:8px">
   <div class="m">Most connected (${topDeg} links)</div>
   <div style="font-size:12px;margin-top:3px;cursor:pointer;color:var(--acc)"
    onclick="memShowDetail('${esc(topNode.type)}','${esc((topNode.id||'').split(':')[1]||topNode.id)}','${esc(topNode.label).replace(/'/g,"\\'")}')">${esc(topNode.label)}</div></div>` : ''}`;
}
function memApplyFilters(){
 if(!MEM_CY) return;
 const active = Array.from(document.querySelectorAll('[data-mem-filter]'))
  .filter(cb => cb.checked).map(cb => cb.dataset.memFilter);
 MEM_CY.nodes().forEach(n => {
  const show = active.includes(n.data('type'));
  n.style('display', show ? 'element' : 'none');
 });
 MEM_CY.edges().forEach(e => {
  const show = e.source().style('display') !== 'none' && e.target().style('display') !== 'none';
  e.style('display', show ? 'element' : 'none');
 });
}

function memGoToSearch(query){
 // Command Chat -> Memory Center entry point (V3 workflow item 3):
 // switches to Memory Center and runs the same search a cited
 // search_decisions/search_investigations tool call used. Reuses
 // memSearch()/memCC() as-is -- no new backend, no new query logic.
 VIEW = 'memcc';
 document.querySelectorAll('#side a').forEach(x=>{if(x.dataset.v=='memcc')x.classList.add('on');else x.classList.remove('on')});
 document.querySelectorAll('#main>div').forEach(d=>d.style.display='none');
 document.getElementById('v-memcc').style.display='block';
 memCC();
 insp.classList.remove('open');
 setTimeout(() => {
  const input = document.getElementById('memSearchInput');
  if(input){ input.value = query; memSearch(); }
 }, 50); // memCC() rebuilds the DOM async (waits on loadCytoscape) --
         // give the search input a moment to exist before using it
}

async function memSearch(){
 const q = document.getElementById('memSearchInput').value.trim();
 if(!q) return;
 const results = await (await fetch('/api/memory_search?q=' + encodeURIComponent(q))).json();
 const box = document.getElementById('memSearchResults');
 box.innerHTML = results.length ? results.map(r =>
  `<div class="click" style="padding:3px 0" onclick="memShowDetail('${r.type}','${r.id}','${esc(r.topic)}')">${esc(r.topic)}</div>`
 ).join('') : 'no matches';
}

async function memShowDetail(nodeType, nodeId, label){
 const panel = document.getElementById('memDetailPanel');
 panel.classList.add('open');
 const [record, related] = await Promise.all([
  (await fetch(`/api/memory_record?type=${encodeURIComponent(nodeType)}&id=${encodeURIComponent(nodeId)}`)).json(),
  (await fetch(`/api/memory_related?type=${encodeURIComponent(nodeType)}&id=${encodeURIComponent(nodeId)}`)).json(),
 ]);
 const fields = Object.entries(record).filter(([k]) => !['error'].includes(k))
  .map(([k,v]) => {
   if(k === 'related_evaluations' && v){
    // Item 8 (V3 workflow): same clickable pattern already proven
    // by Cost Intelligence's expensive-evaluation rows -- just never
    // applied here before. related_evaluations is comma-separated
    // eval_ids already written by command_chat_writer.add_investigation().
    const chips = String(v).split(',').map(id => id.trim()).filter(Boolean)
     .map(id => `<span class="chip" style="cursor:pointer" onclick="openEval('${esc(id)}')">${esc(id)}</span>`).join(' ');
    return `<div class="m" style="margin-top:4px"><b style="color:var(--txt)">${esc(k)}:</b> ${chips}</div>`;
   }
   return `<div class="m" style="margin-top:4px"><b style="color:var(--txt)">${esc(k)}:</b> ${esc(v)}</div>`;
  }).join('');
 const relatedItems = [
  ...(related.outgoing||[]).map(r => `<div class="chip">${esc(r.edge_type)} → ${esc(r.target_type)}:${esc(r.target_id)}</div>`),
  ...(related.incoming||[]).map(r => `<div class="chip">${esc(r.source_type)}:${esc(r.source_id)} → ${esc(r.edge_type)}</div>`),
 ].join('');
 panel.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
  <h3 class="h" style="margin:0">${esc(label || nodeId)}</h3>
  <span style="cursor:pointer;color:var(--dim);font-size:16px" onclick="document.getElementById('memDetailPanel').classList.remove('open')">✕</span></div>
  ${fields || '<div class="m">'+esc(nodeType)+' node — no detail record</div>'}
  <h3 class="h" style="margin-top:12px">RELATED</h3>
  <div style="display:flex;flex-direction:column;gap:4px">${relatedItems || '<div class="m">no connections yet</div>'}</div>`;
 if(MEM_CY){
  const cyId = `${nodeType}:${nodeId}`;
  const node = MEM_CY.getElementById(cyId);
  if(node && node.length){ MEM_CY.elements().unselect(); MEM_CY.center(node); node.select(); }
 }
}

function renderPickDetail(d){
 const p=d.pick;
 const sh=d.shadow;
 const atAGlance = `
 <div class="panel" style="margin:12px 0">
  <h3 class="h">📖 AT A GLANCE</h3>
  <div class="m" style="margin-bottom:8px">A plain-language summary of what the system decided and how much it had to go on. Everything below this panel (STATUS, CONFIDENCE FLOW, COMPLIANCE, REASONING, ATE) is the same detailed record as before — this panel just puts the headline numbers in one place first.</div>
  <div class="strip" style="grid-template-columns:repeat(2,1fr)">
   <div class="tile"><div class="t">Direction</div><div class="v">${p.direction=='MORE'?'⬆':p.direction=='LESS'?'⬇':'—'} ${esc(p.direction||'—')}</div></div>
   <div class="tile"><div class="t">Confidence</div><div class="v">${pct(p.confidence)}</div><div class="m">${gradeEmoji(p.color)} ${esc(p.color||'')}</div></div>
   <div class="tile"><div class="t">Apex implied fair odds</div><div class="v">${fmtOdds(impliedOdds(p.confidence))}</div><div class="m">derived from confidence, not a market price</div></div>
  </div>
  ${sh ? `
  <div class="strip" style="grid-template-columns:repeat(2,1fr);margin-top:10px">
   <div class="tile">
    <div class="t">Data completeness</div>
    <div class="v">${sh.data_completeness_pct!=null?sh.data_completeness_pct+'%':'n/a'}</div>
    ${sh.data_completeness_pct!=null ? `<div class="m" style="margin-top:2px">
      <span class="chip ${sh.data_completeness_pct>=50?'ok':'A'}">${sh.data_completeness_pct>=50?'ENOUGH DATA':'THIN DATA'}</span>
     </div>` : '<div class="m">not logged for this pick</div>'}
    ${sh.data_completeness_note ? `<div class="m" style="margin-top:4px">${esc(sh.data_completeness_note)}</div>` : ''}
   </div>
   <div class="tile">
    <div class="t">Goblin/demon conflict</div>
    <div class="v">${sh.goblin_conflict ? '⚠️ YES' : 'No'}</div>
    ${sh.goblin_conflict ? '<div class="m">genuine read disagreed with the locked market side</div>' : ''}
   </div>
  </div>
  <div class="sec" style="margin-top:10px">
   <div class="h">Match Model cross-check <span class="m" style="font-weight:400">(exact math from hold%, independent of the reasoning above — a second opinion, not the source of the actual pick)</span></div>
   ${sh.match_model_win_prob!=null ? `
    <div class="rowline"><span>Win probability</span><span class="val">${pct(sh.match_model_win_prob)}</span></div>
    <div class="rowline"><span>Expected total games</span><span class="val">${sh.match_model_expected_games!=null?sh.match_model_expected_games.toFixed(1):'—'}</span></div>
    <div class="rowline"><span>Expected sets</span><span class="val">${sh.match_model_expected_sets!=null?sh.match_model_expected_sets.toFixed(2):'—'}</span></div>
   ` : '<div class="m">not computed for this pick — needs both players hold% confirmed by V8</div>'}
  </div>
  <div class="sec" style="margin-top:10px">
   <div class="h">Recommendation Engine read <span class="m" style="font-weight:400">(shadow only — informational, does not control what was actually posted)</span></div>
   ${sh.shadow_recommendation ? `
    <div class="rowline"><span>Read</span><span class="val"><span class="chip ${sh.shadow_recommendation=='BET'?'ok':'B'}">${esc(sh.shadow_recommendation)}</span></span></div>
    ${sh.shadow_reason_code ? `<div class="m" style="margin-top:4px">${esc(sh.shadow_reason_code)}</div>` : ''}
   ` : '<div class="m">not computed for this pick</div>'}
  </div>
  ` : `<div class="m" style="margin-top:10px">No shadow data logged for this pick — this evaluation predates the calibration-logging feature, or logging failed silently (fail-open by design, so this never blocked the pick itself).</div>`}
 </div>`;
 ibody.innerHTML=atAGlance+`<h2 style="margin:4px 0">${esc(p.player)} — ${p.line??''} ${esc(p.stat_type??'')}
 — ${p.direction=='MORE'?'⬆':p.direction=='LESS'?'⬇':'✅'} <b>${p.direction??''}</b>
 <span class="chip ${p.color}">${pct(p.confidence)} ${gradeEmoji(p.color)} ${p.color} · ${fmtOdds(impliedOdds(p.confidence))}</span></h2>
 ${d.source=='dataset_a'?'<div class="m">sourced from Dataset A directly</div>':''}
 ${d.alerts.map(a=>`<div class="chip A">🛑 ${a}</div>`).join('')}
 <h3 class="h">STATUS</h3>
 ${Object.entries(d.status).map(([k,v])=>`<span class="chip ${/PASS|HIT|4\/4|HARD/.test(v)?'ok':/FLAG|UNVER|BUST/.test(v)?'A':''}">${k}: ${v}</span>`).join('')}
 <h3 class="h">CONFIDENCE FLOW</h3>
 <div class="flow">${d.flow.map(f=>`<div class="${/UNEXPLAINED|🛑/.test(f)?'bad':''}">${esc(f)}</div>`).join('')}</div>
 <h3 class="h">COMPLIANCE</h3>
 ${(d.flags||[]).map(f=>`<span class="chip ${f.category||'A'}">${esc(f.rule)} [${f.category||'A'}]</span>`).join('')||'<span class="chip B">clean</span>'}
 <h3 class="h">REASONING</h3><pre>${esc(d.reasoning)||'(no reasoning archive found)'}</pre>
 <h3 class="h">ATE OBSERVABILITY</h3>
 ${d.ate ? `
 <div class="m" style="margin-bottom:6px">checkpoint-by-checkpoint capture from the Apex Trace Engine — independent of, and cross-checkable against, the STATUS/REASONING above</div>
 <div class="m" style="margin-top:4px">CHECKPOINTS (${d.ate.checkpoint_stamps.length})</div>
 <div class="flow">${d.ate.checkpoint_stamps.map(s=>
   `<div>${esc(s.label)}${s.stamp_text&&s.stamp_text!==s.label?` — <span class="m">${esc(s.stamp_text)}</span>`:''}</div>`
 ).join('')||'<div class="m">none captured</div>'}</div>
 <div class="m" style="margin-top:8px">RULE ACTIVATIONS (${d.ate.rule_activations.length})</div>
 ${d.ate.rule_activations.map(a=>
   `<span class="chip ${a.status=='CONFIRMED'?'ok':'A'}" title="${esc(a.reasoning_snippet||'')}">
    ${esc(a.rule_name)} [${a.source}/${a.status}${a.category?'/'+a.category:''}]</span>`
 ).join('')||'<span class="chip B">none captured</span>'}
 <div class="m" style="margin-top:8px">AI TRACES (${d.ate.traces.length})</div>
 ${d.ate.traces.map(t=>
   `<div class="m">${esc(t.step_type)} — ${t.tokens_in??'?'}in / ${t.tokens_out??'?'}out — $${(t.cost_usd||0).toFixed(4)} — ${t.latency_ms??'?'}ms
    ${t.success?'':`<span class="chip A">FAILED: ${esc(t.error_message||'unknown')}</span>`}</div>`
 ).join('')||'<div class="m">none captured</div>'}
 <div style="margin-top:10px"><span class="chip ok" style="cursor:pointer" data-evalid="${esc(d.ate.eval_id)}" data-label="${esc(p.player)} ${esc(String(p.line??''))} ${esc(p.stat_type??'')}" onclick="askAboutThisEval(this.dataset.evalid, this.dataset.label)">💬 Ask Command Chat about this evaluation</span></div>
 ` : '<div class="m">no ATE trace captured for this evaluation (predates ATE deployment, or ATE was unavailable at evaluation time) — Command Chat entry point unavailable here for the same reason (no eval_id resolvable on this path)</div>'}`;
 insp.classList.add('open')}
async function open_(key){if(!key)return;
 const d=await(await fetch('/api/pick?key='+encodeURIComponent(key))).json();if(d.error)return;
 renderPickDetail(d)}
async function openEval(id){if(!id)return;
 const d=await(await fetch('/api/pick_eval?id='+encodeURIComponent(id))).json();if(d.error)return;
 renderPickDetail(d)}
async function decide(id,ap){await fetch('/api/decide',{method:'POST',body:JSON.stringify({id,approve:ap})});load()}
async function slip(){await fetch('/api/slip',{method:'POST',body:JSON.stringify({
 stake:s1.value,legs:s2.value,result:s3.value,payout:s4.value})});load()}
async function subText(){const t=document.getElementById('tx').value.trim();if(!t)return;
 await fetch('/api/intake/text',{method:'POST',body:JSON.stringify({text:t})});
 document.getElementById('tx').value='';load()}
async function subTextBatch(){
 // Batch API path (v1.1): research runs now, evaluation call deferred
 // to Anthropic's /v1/messages/batches at 50% token cost.
 const t=document.getElementById('tx').value.trim();if(!t)return;
 await fetch('/api/intake/batch_text',{method:'POST',body:JSON.stringify({text:t})});
 document.getElementById('tx').value='';load()}
async function subImgBatch(){
 // Same three protections as subImg (auto-submit on change not used
 // here — the user must select the file then click the batch button
 // explicitly, since batch is an intentional deferral choice).
 const el=document.getElementById('imgBatch');const f=el&&el.files[0];if(!f)return;
 const st=()=>document.getElementById('imgBatchStatus');
 if(st())st().textContent='uploading '+f.name+'...';
 try{
  const b64=await new Promise((r,j)=>{const rd=new FileReader();rd.onload=()=>r(rd.result);rd.onerror=()=>j(rd.error);rd.readAsDataURL(f)});
  const resp=await fetch('/api/intake/batch_image',{method:'POST',body:JSON.stringify({b64,media_type:f.type||'image/png'})});
  if(!resp.ok)throw new Error('HTTP '+resp.status);
  const d=await resp.json();
  if(st())st().textContent='queued for batch as #'+d.id+' ⏳ — '+f.name+' (result in ~2-8h)';
 }catch(e){
  if(st())st().textContent='batch upload FAILED: '+e;
  console.error('batch image upload failed',e);
 }
 load()}
async function saveBankroll(){
 const amt=parseFloat(document.getElementById('brAmt').value);
 if(!amt||amt<=0)return;
 const st=document.getElementById('brStatus');
 if(st)st.textContent='Saving...';
 const r=await(await fetch('/api/bankroll_set',{method:'POST',body:JSON.stringify({amount:amt})})).json();
 if(r.ok){
  if(st)st.textContent='Saved ✓';
  setTimeout(()=>{document.getElementById('brEdit').style.display='none';load();},800);
 }else{
  if(st)st.textContent='Error: '+(r.error||'unknown');
 }
}
async function runCollectorNow(){
 const st=document.getElementById('runNowStatus');
 if(st)st.textContent='Starting collector...';
 try{
  const r=await(await fetch('/api/warehouse_run_now',{method:'POST',body:'{}'})).json();
  if(st)st.textContent=r.message||r.error||'Done';
 }catch(e){if(st)st.textContent='Error: '+e;}
}
async function clearStalls(){
 await fetch('/api/clear_stalls',{method:'POST',body:'{}'});
 load();
}
async function subMoneyline(){const a=document.getElementById('mlA').value.trim();
 const b=document.getElementById('mlB').value.trim();if(!a||!b)return;
 const surf=document.getElementById('mlSurf').value.trim()||'hard';
 const tour=document.getElementById('mlTour').value.trim()||'atp';
 await fetch('/api/intake/moneyline',{method:'POST',body:JSON.stringify({
  player_a:a,player_b:b,surface:surf,tour:tour})});
 document.getElementById('mlA').value='';document.getElementById('mlB').value='';load()}
async function subImg(){
 const f=window._propFile;if(!f)return;
 const st=()=>document.getElementById('imgStatus');
 if(st())st().textContent='uploading '+f.name+'...';
 try{
  const b64=await new Promise((r,j)=>{const rd=new FileReader();rd.onload=()=>r(rd.result);rd.onerror=()=>j(rd.error);rd.readAsDataURL(f)});
  const resp=await fetch('/api/intake/image',{method:'POST',body:JSON.stringify({b64,media_type:f.type||'image/png'})});
  if(!resp.ok)throw new Error('HTTP '+resp.status);
  const d=await resp.json();
  window._propFile=null;
  const nm=document.getElementById('imgName');if(nm)nm.textContent='no file selected';
  if(st())st().textContent='queued as #'+d.id+' — '+f.name;
 }catch(e){if(st())st().textContent='upload FAILED: '+e;}
 load()}
async function subImgBatch(){
 const f=window._propFile;if(!f)return;
 const st=()=>document.getElementById('imgStatus');
 if(st())st().textContent='uploading '+f.name+' for batch...';
 try{
  const b64=await new Promise((r,j)=>{const rd=new FileReader();rd.onload=()=>r(rd.result);rd.onerror=()=>j(rd.error);rd.readAsDataURL(f)});
  const resp=await fetch('/api/intake/batch_image',{method:'POST',body:JSON.stringify({b64,media_type:f.type||'image/png'})});
  if(!resp.ok)throw new Error('HTTP '+resp.status);
  const d=await resp.json();
  window._propFile=null;
  const nm=document.getElementById('imgName');if(nm)nm.textContent='no file selected';
  if(st())st().textContent='queued for batch as #'+d.id+' ⏳ (result ~2-8h)';
 }catch(e){if(st())st().textContent='batch upload FAILED: '+e;}
 load()}
async function subMlImg(){
 const f=window._mlFile;if(!f)return;
 const st=()=>document.getElementById('mlImgStatus');
 if(st())st().textContent='uploading '+f.name+'...';
 try{
  const b64=await new Promise((r,j)=>{const rd=new FileReader();rd.onload=()=>r(rd.result);rd.onerror=()=>j(rd.error);rd.readAsDataURL(f)});
  const resp=await fetch('/api/intake/moneyline_image',{method:'POST',body:JSON.stringify({b64,media_type:f.type||'image/png'})});
  if(!resp.ok)throw new Error('HTTP '+resp.status);
  const d=await resp.json();
  window._mlFile=null;
  const nm=document.getElementById('mlImgName');if(nm)nm.textContent='no file selected';
  if(st())st().textContent='queued as #'+d.id+' — '+f.name;
 }catch(e){if(st())st().textContent='upload FAILED: '+e;}
 load()}
async function subMlImgBatch(){
 const f=window._mlFile;if(!f)return;
 const st=()=>document.getElementById('mlImgStatus');
 if(st())st().textContent='uploading '+f.name+' for batch...';
 try{
  const b64=await new Promise((r,j)=>{const rd=new FileReader();rd.onload=()=>r(rd.result);rd.onerror=()=>j(rd.error);rd.readAsDataURL(f)});
  const resp=await fetch('/api/intake/batch_ml_image',{method:'POST',body:JSON.stringify({b64,media_type:f.type||'image/png'})});
  if(!resp.ok)throw new Error('HTTP '+resp.status);
  const d=await resp.json();
  window._mlFile=null;
  const nm=document.getElementById('mlImgName');if(nm)nm.textContent='no file selected';
  if(st())st().textContent='queued for batch as #'+d.id+' ⏳ (result ~2-8h)';
 }catch(e){if(st())st().textContent='batch upload FAILED: '+e;}
 load()}
function resultsBadge(o){
 if(o==='HIT')return '<span style="color:var(--grn)">✅ HIT</span>';
 if(o==='BUST')return '<span style="color:var(--red)">❌ BUST</span>';
 if(o==='PUSH'||o==='VOID')return '<span class="m">◽ '+o+'</span>';
 return '<span style="color:var(--yel)">⏳ PENDING</span>'}
let RESULTS_FILTER='ALL';let _resultsData=null;
function setResultsFilter(f){RESULTS_FILTER=f;if(_resultsData)resultsCC(_resultsData)}
async function setPickOutcome(okey,outcome){
 // front door on the EXISTING /api/outcome endpoint -- okey arrives
 // from the backend (ops._okey), never rebuilt client-side, so it can
 // never drift from what set_outcome()/tracker() resolve against.
 await fetch('/api/outcome',{method:'POST',body:JSON.stringify({key:okey,outcome:outcome})});
 resultsCC(await(await fetch('/api/pick_results')).json())}
function resultsRows(rs,section){
 const f=RESULTS_FILTER;
 const keep=rs.filter(r=>f==='ALL'||(f==='GREEN'&&r.color==='GREEN')||(f==='PENDING'&&!r.outcome)||(f==='SETTLED'&&r.outcome));
 if(!keep.length)return '<tr><td colspan="9" class="m">no '+(f==='ALL'?'':f.toLowerCase()+' ')+'picks</td></tr>';
 return keep.map(r=>{
  const conf=r.confidence!=null?Math.round(r.confidence*100)+'%':'–';
  const dot=r.color==='GREEN'?'🟢':r.color==='YELLOW'?'🟡':r.color?'🔴':'';
  // stopPropagation on both buttons -- otherwise clicking HIT/BUST would
  // also fire the row's own click-through to the reasoning view below.
  const act=!r.outcome?('<button onclick="event.stopPropagation();setPickOutcome(\''+esc(r.okey).replace(/'/g,"\\'")+'\',\'HIT\')">HIT</button> '+
   '<button onclick="event.stopPropagation();setPickOutcome(\''+esc(r.okey).replace(/'/g,"\\'")+'\',\'BUST\')">BUST</button>'):'';
  // Only rows with a confident Dataset A match get the click-through --
  // see _find_eval_id_for_pick's docstring for why an ambiguous or
  // missing match deliberately stays non-clickable rather than guessing.
  const clickable = !!r.eval_id;
  const rowAttrs = clickable ? ` class="evalrow" style="cursor:pointer" onclick="openEval('${esc(r.eval_id)}')"` : '';
  const reasonCell = clickable ? '<span class="m" title="click row for full reasoning">🔍</span>' : '<span class="m" title="no reasoning on file for this pick">—</span>';
  return '<tr'+rowAttrs+'><td class="m">'+esc(r.date)+'</td><td>'+esc(r.player)+'</td><td class="m">'+esc(r.market)+'</td>'+
   '<td>'+(r.line??'')+'</td><td>'+esc(r.direction)+'</td><td>'+dot+' '+conf+'</td>'+
   '<td>'+resultsBadge(r.outcome)+'</td><td>'+reasonCell+'</td><td>'+act+'</td></tr>'}).join('')}
function resultsCC(d){
 _resultsData=d;
 const el=document.getElementById('v-results');
 if(!d){el.innerHTML='<span class="m">no pick data available</span>';return}
 const ps=d.props_summary,ms=d.ml_summary;
 const chip=f=>'<button onclick="setResultsFilter(\''+f+'\')" style="opacity:'+(RESULTS_FILTER===f?1:.55)+'">'+f+'</button>';
 el.innerHTML=
 '<div class="panel" style="margin-bottom:12px">'+
  '<h3 class="h">📖 What is this screen?</h3>'+
  '<div class="m">The real settle record for every pick posted — props and moneyline, tracked separately. GREEN picks have crossed the confidence bar; PENDING picks haven\'t settled yet; SETTLED means a real HIT or BUST outcome is recorded. Click a row to record an outcome or open its detail.</div>'+
  '<div class="m" style="margin-top:6px">This is the source of truth for Apex Success and Validation — win rates there only exist because outcomes get settled here first.</div>'+
  '<div class="m" style="margin-top:6px">🔍 in the reason column means the full reasoning for that pick is available — click the row to open it. A dash means no confident match exists in Dataset A, most often because the pick predates Dataset A logging (started 2026-07-23) — this is a real, honest gap, not an error.</div>'+
 '</div>'+
 '<div class="strip">'+
  '<div class="tile"><div class="t">PROPS RECORD</div><div class="v">'+ps.hits+'-'+ps.busts+'</div><div class="m">'+ps.pending+' pending · '+ps.total+' total</div></div>'+
  '<div class="tile"><div class="t">MONEYLINE RECORD</div><div class="v">'+ms.hits+'-'+ms.busts+'</div><div class="m">'+ms.pending+' pending · '+ms.total+' total</div></div>'+
  '<div class="tile"><div class="t">SETTLED</div><div class="v">'+(ps.settled+ms.settled)+'</div><div class="m">across both markets</div></div>'+
 '</div>'+
 '<div style="margin:10px 0">'+chip('ALL')+' '+chip('GREEN')+' '+chip('PENDING')+' '+chip('SETTLED')+'</div>'+
 '<div class="panel"><h3 class="h">🎯 PROP RESULTS</h3><table><tr><th>date</th><th>player</th><th>market</th><th>line</th><th>dir</th><th>conf</th><th>result</th><th>reason</th><th></th></tr>'+
  resultsRows(d.props,'props')+'</table></div>'+
 '<div class="panel" style="margin-top:12px"><h3 class="h">🎾 MONEYLINE RESULTS</h3><table><tr><th>date</th><th>player</th><th>market</th><th>line</th><th>dir</th><th>conf</th><th>result</th><th>reason</th><th></th></tr>'+
  resultsRows(d.moneyline,'ml')+'</table></div>'}
load();setInterval(()=>{if(VIEW=='deck'||VIEW=='opsv'||VIEW=='intake'||VIEW=='agents'||VIEW=='canvas')load()},30000);
</script></body></html>"""

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    intake.start_worker()
    # This session: the single most reliable way to know whether
    # MC_AUTH_USER/MC_AUTH_PASS actually reached this process is to
    # have the process say so itself, on startup, in the log --
    # launchd's own `launchctl print` output doesn't reliably surface
    # plist-level EnvironmentVariables in its "environment" section,
    # which was the actual source of confusion getting this deployed.
    # Never logs the password -- only whether it's set, and which
    # username (not a secret) if so.
    if not MC_AUTH_ENABLED:
        print("[Auth] Basic Auth is OFF (MC_AUTH_ENABLED=False in code) — "
              "no login required. Turned off intentionally on 2026-07-21.")
    elif MC_AUTH_USER and MC_AUTH_PASS:
        print(f"[Auth] MC_AUTH_USER/MC_AUTH_PASS loaded — user '{MC_AUTH_USER}'. "
              f"Basic Auth is ACTIVE.")
    else:
        print("[Auth] ⚠️ MC_AUTH_USER/MC_AUTH_PASS NOT SET on this process — "
              "every request will be refused with 401 until they're set in "
              "the LaunchAgent plist's EnvironmentVariables and this process "
              "is restarted. Check with: plutil -p ~/Library/LaunchAgents/"
              "com.apex.openclawmissioncontrol.plist")
    print(f"OpenClaw Mission Control v2+ → http://0.0.0.0:{port}  (intake worker live)")
    HTTPServer(("0.0.0.0", port), H).serve_forever()

# © 2026 Demetrius Reynolds — Project Apex Intelligence™. All rights reserved.
