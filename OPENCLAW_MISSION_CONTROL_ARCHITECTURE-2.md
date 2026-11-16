# OPENCLAW MISSION CONTROL — ARCHITECTURE (v1.0, DESIGN ONLY — NO CODE)
Inspiration studied: builderz-labs/mission-control (github.com/builderz-labs/
mission-control). Borrowed: single-shell layout with an always-visible live
feed, stage-flow board, "what's running / what failed / what needs attention
first" overview philosophy, local-first zero-external-dependency storage.
Rejected: agent dispatch/registration, task creation, skills hub, workspaces,
Telegram workflows — OpenClaw's agents are not dispatched; they run ONE fixed
pipeline. This is Mission Control FOR OpenClaw, not OpenClaw inside Mission
Control. Every component below answers: how does this help OpenClaw make,
validate, monitor, improve, or track picks?

Delivery vehicle: Python Textual TUI (terminal-first per the Tailscale
requirement — identical over `ssh macmini`, no browser, no ports). The web
stack of the inspiration repo is explicitly NOT adopted.

═══════════════════════════════════════════════════════════════════════
# 1. SYSTEM ARCHITECTURE DIAGRAM
═══════════════════════════════════════════════════════════════════════
                     ┌────────────────────────────────┐
                     │   OPENCLAW MISSION CONTROL     │  ← read-only observer
                     │   (openclaw_ops.py, Textual)   │    (one exception:
                     └───┬──────────┬──────────┬──────┘     approve/reject CLI)
              tails/polls│          │          │
   ┌─────────────────────▼──┐  ┌────▼──────┐  ┌▼───────────────────┐
   │ LOGS                   │  │ ARCHIVES  │  │ OPS STATE          │
   │ discord_bot.log        │  │ picks_*   │  │ data/ops_center/   │
   │ discord_bot_error.log  │  │ reasoning │  │ config, slip ledger│
   └─────────▲──────────────┘  │ ml_*      │  │ trackers, health   │
             │ writes          │ proposals │  └────────▲───────────┘
   ┌─────────┴──────────────┐  │ signal    │           │ writes (TUI-owned)
   │ OPENCLAW PIPELINE      │  │ history   │           │
   │ (openclaw_core.py,     │  │ validation│───────────┘
   │  LaunchAgent, un-      │  └───────────┘
   │  touched by the TUI)   │──► Discord (picks/alerts/learning/system/ML)
   └────────────────────────┘
Principle: the pipeline never knows Mission Control exists. Every byte the
TUI shows comes from files the pipeline ALREADY writes. Zero bot changes for
Phases 1-2. Why OpenClaw needs it: every major bug this month (silent shaves,
dropped alerts, dominance-blind gate) lived in the gap between "system did
something" and "operator could see it."

═══════════════════════════════════════════════════════════════════════
# 2. DATA FLOW DIAGRAM (one prop, end to end, with observation taps)
═══════════════════════════════════════════════════════════════════════
 PrizePicks board / screenshot / #general text
        │
        ▼
 ┌─RESEARCH────────┐ V8 packet + web fallback → research_data (merged truth)
 │ tap: log phases │ Gate 11 verify · name resolve · Gate 11 tour correction
 └───────┬─────────┘
         ▼
 ┌─EVALUATION──────┐ master prompt (HANDOFF+DT+SSC) → direction, anchor,
 │ tap: reasoning  │ REQs, dominance, eval_confidence
 │ archive file    │
 └───────┬─────────┘
         ▼
 ┌─COMPLIANCE──────┐ 13 checks vs CONFIRMED V8 GROUND TRUTH (same packet the
 │ tap: flags in   │ eval used) → flags A/B/C, retraction net, normalization
 │ archived JSON   │
 └───────┬─────────┘
         ▼
 ┌─SCORING─────────┐ caps · deductions · floor · Gate 11-B → confidence +
 │ tap: LEDGER     │ itemized score_adjustments (the conservation ledger)
 └───────┬─────────┘
         ▼
 ┌─POSTING─────────┐ pick card → #picks / #moneyline · alerts → #alerts
 │ tap: log + card │
 └───────┬─────────┘
         ▼ (23:00 nightly)
 ┌─LEARNING────────┐ HIT/BUST per pick (V8-verified) → signal histories,
 │ tap: outcomes,  │ last-10-per-signal stats → weight PROPOSALS (pending
 │ proposals file  │ operator approval, never auto-applied)
 └───────┬─────────┘
         ▼ (on demand)
 ┌─VALIDATION──────┐ green_validation_suite --run / --echo-trace →
 │ tap: run_history│ preservation, movement, calibration, echo findings
 └─────────────────┘
Stale-data risks the taps must respect (all found live this month): read the
ledger, never recompute it; read research_data (merged), never the raw
prefetch; treat reasoning files as eval-time truth and picks_*.json as
final truth; a moved score with an empty ledger renders RED.

═══════════════════════════════════════════════════════════════════════
# 3. AGENT COMMUNICATION DIAGRAM (current paths, gaps, audit points)
═══════════════════════════════════════════════════════════════════════
 Research ──research_data──► Evaluation ──evaluated dict──► Compliance
     │                            ▲                             │
     │ v8_packet (corrected)      │ retry w/ flags (attempt 2)  │ flags
     └──────────────┬─────────────┴─────────────────────────────┘
                    ▼
                Scoring ──scored dict (+ledger)──► Posting ──► Discord
                    │                                  │
                    └──────── picks_*.json ◄───────────┘
                                   │
                Learning ◄─────────┘──► proposals ──► OPERATOR (approve/reject)
COMMUNICATION FACTS (verified this week): eval and compliance now read the
SAME corrected packet (stale-prefetch fork closed); compliance→scoring is one
flags object, categories normalized at one choke point; direction mutates
only via the loud Goblin lock. REMAINING VISIBILITY GAPS Mission Control
exists to close: (a) mid-pipeline stage position is only inferable from log
lines — no first-class "where is this prop right now" view; (b) rule
activations (Correction 24 injected? Rule 30 scale applied?) are buried in
reasoning text; (c) validation results live in files nobody opens daily;
(d) pending proposals were invisible until this week's twice-daily alert.

═══════════════════════════════════════════════════════════════════════
# 4. MISSION CONTROL LAYOUT MOCKUPS (80x24 minimum; F-keys switch screens)
═══════════════════════════════════════════════════════════════════════
SCREEN 1 — COMMAND DECK (default; answers "Am I winning?" then "Is anything
broken?" in one glance)
┌ OPENCLAW MISSION CONTROL ────────────────────── 21:58 CT · SSH ─┐
│ GREENS  72/100 ✅71.4% (50-20) ██████████████▍░░░  strk W4      │
│ SLIPS   34/100 ROI +118% net $[amount] ██████▊░░░░  6/6:9 5/6:6  │
│ MONEYLN 41/100 🟡68.3% (28-13) ████████▏░░░░░░░░  strk W2      │
│ BANKROLL $[amount] / $[amount]  0.84% ▍░░░░░░░░░░░░░░░░░░░░░░░░░░  │
├─ TONIGHT'S PIPELINE ────────────────┬─ LIVE FEED ──────────────┤
│ Djokovic DF2.5   R🟢 E🟢 C🟢 S🟢 P🟢 │ 21:52 ✅ posted 72% GREEN │
│ Kostyuk TG20.5   R🟢 E🟢 C🟡2 S⚪ P⚪ │ 21:51 ⚠️ Cat-B note      │
│ Fery ML          R🟢 E🔴 C⚪  S⚪ P⚪ │ 21:49 🛑 eval unparseable│
│ GREENS TODAY: Djokovic 72 · Fritz 71│ 21:44 🔔 1 proposal pend │
├─ HEALTH ────────────────────────────┴──────────────────────────┤
│ V8🟢 Discord🟢 Compliance🟢 Scoring🟢 Archives🟢  Score 98%  q ? │
└─────────────────────────────────────────────────────────────────┘
SCREEN 2 — PICK INSPECTOR (ENTER on any prop): Confidence Flow Graph
(anchor→every ledger hop→final; unexplained hop = red), Rule Monitor
(TRUTH LOCK PASS · RULE 24 ACTIVE · RULE 30 PASS · DOMINANCE HARD · GREEN
ELIGIBLE YES), compliance flags w/ categories, reasoning pager, echo risk.
SCREEN 3 — VALIDATION CENTER (suite results; see §9).
SCREEN 4 — RULE ROADMAP (see §10).  SCREEN 5 — LEDGERS (slips/ML entry+
history).  SCREEN 6 — HEALTH & ALERTS full feed w/ 🛑 filter.

═══════════════════════════════════════════════════════════════════════
# 5. SHARED MEMORY DESIGN (classify what OpenClaw knows)
═══════════════════════════════════════════════════════════════════════
TEMPORARY (expires, never a source of truth): in-TUI render caches; the
pipeline's own per-run state; V8 API cache (cleared each deploy).
PERSISTENT (live operating state, queryable): ops_config.json ·
slip_ledger.json · green/ml trackers · pending_proposals.json ·
signal/ml histories (rolling last-10 windows) · weights.json ·
run_history.json (validation trend).
ARCHIVED (append-only, forever): picks_*.json · reasoning_*.txt ·
ml archives · validation reports · learning reports. These are the
system's memory of WHY — never pruned, they are the audit trail.
SEARCHABLE (Phase 3): a TUI-owned SQLite index (mirroring the inspiration
repo's local-first choice) over archived picks: player, stat, date, grade,
confidence, ledger causes, flags, outcome — enabling "show every bo5 LESS
GREEN that busted" in one query. The index is rebuildable from files at any
time; files remain the truth, SQLite is a view. Why OpenClaw needs it: rule
effectiveness and echo questions are queries, and today they require
grepping text files by hand.

═══════════════════════════════════════════════════════════════════════
# 6. KNOWLEDGE GRAPH DESIGN (Rules → Confidence → Performance)
═══════════════════════════════════════════════════════════════════════
Nodes: RULES (Gate 11/11-B, Rule 24/30/9/12, Conservation, dominance, REQs,
BP tiers, caps, floor) · PICKS · LEDGER CAUSES · OUTCOMES.
Edges: rule —touched→ pick (from ledger causes + reasoning markers);
pick —resulted→ outcome; rule —classified→ KEEP/REVIEW/AUDIT (roadmap).
Built FROM existing data: the conservation ledger already names every rule
that moved every score; learning already names every outcome. The graph is
a join, not new instrumentation. Operator questions it answers on demand:
which rules influence GREENs (edge counts into GREEN picks) · which improve
performance (win rate of touched vs untouched picks) · which create risk
(rules whose touched-pick record trails baseline → auto-flag REVIEW) ·
which are dormant (zero edges in 30 days). Rendering: not a graphic — a
drill-down table (rule → picks → outcomes), which is what a terminal does
honestly. Feeds the Rule Roadmap Center's Performance column (§10).

═══════════════════════════════════════════════════════════════════════
# 7. WORKFLOW VISUALIZATION DESIGN (primary goal)
═══════════════════════════════════════════════════════════════════════
One lane per active prop/ML: `R E C S P` cells, ✅complete 🟡running(spinner
+ attempt count) 🔴failed ⚪pending. Source: the log lines each agent already
emits (phase starts/ends, compliance attempts, posting confirms). A prop
whose lane hasn't advanced in >90s renders its running cell ⚠️ (stall
detection — would have exposed the silent-hang class instantly). ENTER on a
lane → Pick Inspector. Failed cell shows the exact log line that killed it.
Why OpenClaw needs it: "watch the prop move through OpenClaw" is the single
most requested capability of this entire effort — and the taps for it
already exist in the log.

═══════════════════════════════════════════════════════════════════════
# 8. PERFORMANCE TRACKER DESIGN (adopts v1.2 spec verbatim)
═══════════════════════════════════════════════════════════════════════
GREEN, SLIP, MONEYLINE, BANKROLL trackers exactly as designed in
OPENCLAW_OPS_CENTER_DESIGN.md v1.2 §Performance Expansion: config-driven
goals (100/250/500/1000/custom), auto-derived GREEN+ML records (nightly
HIT/BUST joined to archives — zero manual work, ML never merged with props),
manual slip ledger with payout-entered profit math, bankroll recomputed from
the ledger each refresh (nothing stored that can drift), all restart-safe
JSON under data/ops_center/. The Command Deck scoreboard (§4) is these four
trackers pinned as rows 1-4 of the screen, always visible.

═══════════════════════════════════════════════════════════════════════
# 9. VALIDATION CENTER DESIGN
═══════════════════════════════════════════════════════════════════════
Window onto the GREEN Validation Suite (the suite stays the engine):
last run's GREEN hit rate · preserved/lost/created (each named) · the four
verdict numbers (winner promoted/demoted, loser promoted/demoted — demoted
winners and promoted losers render RED, every one is an investigation) ·
calibration bands 76+/73-75/70-72 · rule-effectiveness records ·
run-over-run trend arrows from run_history.json · echo-trace findings with
severities. Keybind `v` runs the suite (subprocess, read-only). Why OpenClaw
needs it: the benchmark only protects you if it's in your face after every
change, not in a file you have to remember exists.

═══════════════════════════════════════════════════════════════════════
# 10. RULE ROADMAP CENTER DESIGN
═══════════════════════════════════════════════════════════════════════
OPENCLAW_RULE_ROADMAP.md rendered live and joined to the knowledge graph:
Rule · Purpose · Impact · Frequency (measured, not guessed — ledger counts)
· Dependencies · Performance (record of touched picks) · Status
KEEP/REVIEW/AUDIT/REMOVE. Sorted worst-record-first. Status edits write
back to the roadmap file (operator judgment stays the authority; the TUI
supplies the evidence). The measurement priority list (Rule 30 → BP tiers →
Correction 24 → form override → floor) renders as a standing checklist with
sample counts filling in as picks accumulate.

═══════════════════════════════════════════════════════════════════════
# 11. HEALTH & ALERT DESIGN
═══════════════════════════════════════════════════════════════════════
Composite Health Score from live probes: V8 reachability · log freshness
(silent >120s with props queued = stall ⚠️) · launchctl PID/status · last
Discord send OK (and webhook-key validity — the "systems" typo class becomes
a startup lint: every send_discord channel literal checked against the
webhook dict, mismatch = instant 🔴) · archives writable · zero unexplained-
ledger alerts today · scheduled jobs fired on time (17:00/23:00/23:59).
Alert feed mirrors #alerts/#system FROM THE LOG, so Mission Control shows
alerts even when Discord delivery itself is the failure — nothing important
happens silently, including the failure of the alerting. 🛑-only filter one
keypress away. Every panel shows data age; missing source = says so.

═══════════════════════════════════════════════════════════════════════
# 12. IMPLEMENTATION PHASES (for approval — no code until approved)
═══════════════════════════════════════════════════════════════════════
PHASE 1 — Command Deck (one session): scoreboard (4 trackers + config) ·
pipeline lanes · live feed · health score · approvals. Zero bot changes.
Exit test: identical render in a plain 80x24 `ssh macmini` session.
PHASE 2 — Inspector & Centers (one session): Pick Inspector (flow graph +
rule monitor + flags + reasoning pager) · Validation Center · Rule Roadmap
Center · slip/ML ledger screens. Zero bot changes.
PHASE 3 — Memory & Graph (optional, after 2 weeks of live use): SQLite
search index · knowledge-graph joins · echo panel wired to per-batch runs ·
(only if wanted) minimal bot-side structured-event log line for token-level
reasoning feed — the ONLY phase that touches pipeline code, and only
additively.
Risk order deliberately matches value order: Phase 1 alone would have
caught the Djokovic shave, the dropped alerts, and the dominance-blind gate
on the day they happened.


═══════════════════════════════════════════════════════════════════════
# v2.0 — WEB REDESIGN (APPROVED "BUILD IT", 2026-07-10)
═══════════════════════════════════════════════════════════════════════
## 9/10. Browser vs TUI + Technology Stack (the decision, with reasons)
BROWSER IS PRIMARY. The approved aesthetic (dark canvas, cards, side
inspector, sidebar nav) is a web design language; terminals cannot render
it. Access: Mac Mini → Tailscale → any browser at
http://<mini-tailnet-name>:8788 — no public internet exposure (Tailscale
IS the private network), no cloud, no accounts, single machine.
STACK: one Python file, stdlib http.server only — ZERO new dependencies,
no Node, no build step, no Router process, no tokens, no pm2. It imports
the PROVEN data layer from openclaw_ops.py (discovery-first: every path
from openclaw_core's own constants). Why: the reference project's entire
Router/token/npm apparatus exists to bridge remote fleets; OpenClaw is
one machine reading its own disk — adopting that apparatus would add the
manual setup the requirements ban. TUI remains the SSH fallback.

## 2/3/4/5. Mockups — Command Deck + OpenClaw Canvas + Sidebar
┌──────────┬──────────────────────────────────────────────────────────┐
│ OPENCLAW │ GREENS 72/100 ✅71% │ SLIPS ROI+118% │ ML 68% │ BANK 0.8% │
│ MISSION  ├──────────────────────────────────────────────────────────┤
│ CONTROL  │  RESEARCH   EVALUATION   COMPLIANCE   SCORING   POSTED   │
│          │  ┌────────┐ ┌─────────┐  ┌─────────┐ ┌───────┐ ┌───────┐ │
│ ▣ Deck   │  │Kostyuk │ │Fritz    │  │Djokovic⏳│ │       │ │Sinner │ │
│ ▢ Ops    │  │TG 20.5 │ │Aces 8.5 │  │DF 2.5   │ │       │ │🟢 72% │ │
│ ▢ Inspect│  │⬆ MORE  │ │⬇ LESS   │  │attempt 2│ │       │ │MORE   │ │
│ ▢ Valid  │  └────────┘ └─────────┘  └─────────┘ └───────┘ └───────┘ │
│ ▢ Rules  │  rule nodes: [Gate11✓][Rule24·][Rule30✓][TruthLock✓]...  │
│ ▢ Bank   ├───────────────────────────────┬──────────────────────────┤
│          │ LIVE FEED (alerts, ledger)    │ HEALTH  V8🟢 Bot🟢 Disc🟢 │
│ health●  │ APPROVALS 🔔1  [approve][rej] │ Valid🟢 Learn🟢  score 98%│
└──────────┴───────────────────────────────┴──────────────────────────┘
Canvas = pipeline COLUMNS (the OpenClaw workflow IS the layout); cards
are PICKS moving left→right; rule nodes light up per selected pick.
Why OpenClaw needs it: stage position, failures, rule activations, and
GREEN eligibility become one glance instead of a log grep.

## 6. Pick Inspector (right slide-in on card click)
Header: Player — LINE — ⬆/⬇ DIRECTION — grade chip.
CONFIDENCE FLOW: eval% → each ledger hop (named cause) → final; any
unexplained hop renders red. RULE MONITOR: TruthLock/R24/R30/Dominance/
REQs/GREEN-ELIGIBLE chips. COMPLIANCE: flags w/ category chips (B=grey
zero-cost, A=red -3%), attempt count. REASONING: full archived text.
Why: confidence auditing (the Djokovic class) becomes a click; every
point between eval and card is visually accounted — conservation, made
visible, permanently.

## 7. Validation Center — suite's run_history + latest report rendered:
hit-rate trend arrow, preserved/lost/created chips, winner-demoted &
loser-promoted in red, calibration bands, rule-effectiveness records.
Why: the benchmark protects GREENs only if it's in the operator's face
after every change.

## 8. Rule Roadmap — roadmap table (KEEP/REVIEW/AUDIT chips) joined to
ledger counts; worst-record-first. Why: rule pruning becomes evidence-
driven, protecting GREEN quality from over-correction stacking.

## 11. Migration Plan from Phase 1 TUI
Nothing is thrown away: openclaw_ops.py's data layer becomes the shared
module both frontends import (single source of truth for trackers,
lanes, feed, health, approvals, slip/outcome writes). Web Phase 1 =
Deck + Operations canvas + Pick Inspector + health/approvals (this
build). Web Phase 2 = Validation Center + Rule Roadmap + Bankroll
screens. TUI keeps working unchanged throughout. Same data/ops_center/
files serve both — enter a slip in either, both show it.
