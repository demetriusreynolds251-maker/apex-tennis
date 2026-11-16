#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OPENCLAW GREEN VALIDATION SUITE (v2.0 — 2026-07-10)

v2.0 additions (Project 1, approved): GREEN preservation tracking,
winner/loser movement tracking, fine calibration bands (70-72 / 73-75 /
76+), run-over-run trend reports (history kept in
data/validation/run_history.json — the suite's own file, not pipeline
state), and --echo-trace: a reasoning-archive scanner for one fact
charged across multiple checkpoints (Project 3 instrument).

Purpose: answer ONE question after every future fix:
    "Did this change improve GREEN picks, or make them worse?"

NOT a 500-prop research lab. A 20-30 pick benchmark built from the
picks that actually taught the system something (Djokovic, Kostyuk,
Arseneault, Curmi, Masarova, Zverev, Quevedo, ...) plus any pick you
add to the manifest.

READ-ONLY by design: never writes to weights, caches, archives, or any
pipeline state. Its only output is a report file in data/validation/.

WHAT IT DOES (Mode: scoring replay — deterministic, zero API cost):
  For every benchmark pick, re-runs TODAY'S scoring_agent on the
  archived evaluation (the archived pick dict carries everything the
  scorer needs: eval_confidence, research_data, dominance, REQs,
  compliance flags). Then compares against what was ARCHIVED at the
  time, and against the OUTCOME if known.

OUTPUTS:
  1. GREEN hit rate (archived-GREEN and replay-GREEN, vs outcomes)
  2. Confidence calibration by band (do 70s hit like 70s?)
  3. Rule/cap effectiveness on GREENs (which adjustments fire on
     winners vs losers)
  4. Confidence inflation/deflation sources (ledger cause frequency)
  5. Feature-echo heuristic (multiple ledger charges sharing one
     root fact: hold / thin / gap / fatigue / format / dominance)
  6. Per-pick delta table: archived grade vs today's-code grade

USAGE (from the project root, with the bot's code importable):
  python3 green_validation_suite.py --build-manifest      # seed from archives
  python3 green_validation_suite.py --run                 # replay + report
  python3 green_validation_suite.py --run --since 2026-07-01
  python3 green_validation_suite.py --set-outcome "Novak Djokovic|double_faults|2026-07-10|HIT"

MANIFEST: data/validation/benchmark_manifest.json — a list of
  {"player":..., "stat_type":..., "match_date":..., "outcome": "HIT"/"BUST"/null}
  --build-manifest seeds it with every archived pick whose confidence
  was >= 0.67 (GREEN and near-GREEN candidates). Edit freely: delete
  rows you don't want, set outcomes as results become known.
"""
import argparse, json, sys, re, uuid
from pathlib import Path
from datetime import datetime

# ── locate the pipeline ────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import openclaw_core as oc
except Exception as e:
    print(f"FATAL: cannot import openclaw_core from {HERE}: {e}")
    sys.exit(1)

# Dataset A write-back (approved, this session). Same fail-silent
# contract as every other capture call -- a write failure here can
# never break a validation run. This is the ONE deliberate write this
# file makes; everything else about its read-only design is unchanged.
import dataset_a_capture as d1

VAL_DIR  = oc.DATA_DIR / "validation"; VAL_DIR.mkdir(exist_ok=True)
MANIFEST = VAL_DIR / "benchmark_manifest.json"
HISTORY  = VAL_DIR / "run_history.json"

# Replay must be silent + side-effect free: no Discord, minimal log.
oc.send_discord = lambda *a, **k: None
_real_log = oc.log
oc.log = lambda *a, **k: None

ECHO_ROOTS = {
    "hold":      ("hold",),
    "thin_data": ("thin",),
    "rank_gap":  ("gap", "class"),
    "fatigue":   ("fatigue", "turnaround", "rest", "rule 30", "rule30"),
    "format":    ("format", "bo5", "best-of-5", "gate 11-b"),
    "dominance": ("dominance", "edge", "soft", "unstable"),
    "goblin":    ("goblin", "red demon"),
}

def _load_manifest():
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text())
    return []

def _save_manifest(m):
    MANIFEST.write_text(json.dumps(m, indent=2, default=str))

def _iter_archived_picks(since=None):
    for f in sorted(oc.ARCHIVE_DIR.glob("picks_*.json")):
        date = f.stem.replace("picks_", "")
        if since and date < since:
            continue
        try:
            for pick in json.loads(f.read_text()):
                pick["_archive_file"] = f.name
                pick["_archive_date"] = date
                yield pick
        except Exception as e:
            print(f"  (skipping unreadable archive {f.name}: {e})")

def _key(p):
    return (str(p.get("player", "")).lower().strip(),
            str(p.get("stat_type", "")).lower().strip(),
            str(p.get("match_date") or p.get("_archive_date") or ""))

def build_manifest(since=None, min_conf=0.67):
    """Seed the benchmark with every archived GREEN / near-GREEN pick."""
    manifest = _load_manifest()
    known = {(m["player"].lower(), m["stat_type"].lower(), m["match_date"]) for m in manifest}
    added = 0
    for p in _iter_archived_picks(since):
        conf = p.get("confidence")
        if conf is None or conf < min_conf:
            continue
        k = _key(p)
        if k in known:
            continue
        manifest.append({
            "player":     p.get("player"),
            "stat_type":  p.get("stat_type"),
            "match_date": p.get("match_date") or p.get("_archive_date"),
            "line":       p.get("line"),
            "direction":  p.get("direction"),
            "outcome":    None,   # fill in as results become known
            "note":       f"auto-seeded from {p['_archive_file']} (archived {conf:.0%} {p.get('color')})",
        })
        known.add(k); added += 1
    _save_manifest(manifest)
    print(f"Manifest: {len(manifest)} picks ({added} newly added) -> {MANIFEST}")
    print("Edit the file to trim to your 20-30 benchmark picks and set outcomes.")

def set_outcome(spec):
    """--set-outcome 'Player|stat_type|match_date|HIT'"""
    player, stat, date, outcome = [s.strip() for s in spec.split("|")]
    manifest = _load_manifest()
    hit = False
    for m in manifest:
        if (m["player"].lower() == player.lower()
                and m["stat_type"].lower() == stat.lower()
                and m["match_date"] == date):
            m["outcome"] = outcome.upper(); hit = True
    _save_manifest(manifest)
    print("Updated." if hit else "No matching manifest row found.")

def _echo_findings(adjustments):
    """Heuristic: 2+ ledger entries sharing one root fact = possible echo."""
    hits = {}
    for adj in adjustments or []:
        low = adj.lower()
        for root, words in ECHO_ROOTS.items():
            if any(w in low for w in words):
                hits.setdefault(root, []).append(adj)
    return {r: a for r, a in hits.items() if len(a) >= 2}

def run(since=None):
    manifest = _load_manifest()
    if not manifest:
        print("Manifest is empty — run --build-manifest first."); return
    wanted = {(m["player"].lower(), m["stat_type"].lower(), m["match_date"]): m
              for m in manifest}
    weights = oc.load_weights()

    rows, echo_report, cause_freq = [], [], {}
    for p in _iter_archived_picks(since):
        m = wanted.get(_key(p))
        if not m:
            continue
        archived_conf  = p.get("confidence")
        archived_color = p.get("color")
        # Replay through TODAY'S scoring on the archived evaluation.
        try:
            replay = oc.scoring_agent(
                p, weights,
                compliance_penalty=bool(p.get("compliance_penalty")),
                compliance_flags=p.get("compliance_flags") or [])
        except Exception as e:
            rows.append({**m, "archived": archived_conf, "replay": None,
                         "replay_color": f"REPLAY ERROR: {e}", "adjustments": []})
            continue
        r_conf, r_color = replay.get("confidence"), replay.get("color")
        adjs = replay.get("score_adjustments") or []
        for a in adjs:
            root = a.split(":")[0].strip()
            cause_freq[root] = cause_freq.get(root, 0) + 1
        echoes = _echo_findings(adjs)
        if echoes:
            echo_report.append((m["player"], m["stat_type"], m["match_date"], echoes))
        rows.append({**m, "archived": archived_conf, "archived_color": archived_color,
                     "replay": r_conf, "replay_color": r_color,
                     "adjustments": adjs, "eval_conf": p.get("eval_confidence"),
                     # Bug fix (this session): eval_id lives on the archived
                     # pick dict `p`, not the manifest dict `m` -- the row
                     # dict here only ever spread `m`, so eval_id silently
                     # never reached write-back even when the archive had
                     # it. Caught by testing before this ever shipped.
                     "eval_id": p.get("eval_id")})

    matched = [r for r in rows if r.get("replay") is not None]
    if not rows:
        print("No manifest picks were found in the archives for this range."); return

    # ── metrics ────────────────────────────────────────────────────
    def hit_rate(items):
        done = [r for r in items if r.get("outcome") in ("HIT", "BUST")]
        hits = sum(1 for r in done if r["outcome"] == "HIT")
        return (hits, len(done))

    replay_green   = [r for r in matched if r["replay_color"] == "GREEN"]
    archived_green = [r for r in matched if r.get("archived_color") == "GREEN"]
    moved          = [r for r in matched
                      if r["archived"] is not None
                      and abs(r["replay"] - r["archived"]) >= 0.005]
    grade_changed  = [r for r in matched if r["replay_color"] != r.get("archived_color")]

    # ── 2.0: GREEN preservation sets ─────────────────────────────
    def _k(r): return (r["player"], r["stat_type"], r["match_date"])
    arch_set   = {_k(r) for r in archived_green}
    rep_set    = {_k(r) for r in replay_green}
    preserved  = arch_set & rep_set
    lost       = arch_set - rep_set          # GREEN demoted by current code
    created    = rep_set - arch_set          # GREEN promoted by current code
    by_key     = {_k(r): r for r in matched}

    # ── Dataset A write-back (approved, this session) ──────────────
    # One run_id per execution, reused for every record in this run --
    # gives Mission Control a real grouping key. Skips any pick whose
    # archived dict has no eval_id (pre-Dataset-A archives, written
    # before this session's capture patches went live) -- no crash,
    # no synthetic ID, no guess, per the approved guard.
    validation_run_id = str(uuid.uuid4())
    _written, _skipped_no_id = 0, 0
    for _keys, _verdict in ((preserved, "PRESERVED"), (lost, "LOST"), (created, "CREATED")):
        for k in _keys:
            r = by_key[k]
            eid = r.get("eval_id")
            if not eid:
                _skipped_no_id += 1
                continue
            d1.record_validation(eid, validation_run_id, _verdict)
            _written += 1

    # ── 2.0: winner/loser movement ────────────────────────────────
    def _movement(keys, outcome):
        return [by_key[k] for k in keys if by_key[k].get("outcome") == outcome]
    win_promoted  = _movement(created, "HIT")
    win_demoted   = _movement(lost, "HIT")
    lose_promoted = _movement(created, "BUST")
    lose_demoted  = _movement(lost, "BUST")

    lines = ["# OPENCLAW GREEN VALIDATION REPORT (Suite 2.0)",
             f"Generated: {datetime.now().isoformat(timespec='seconds')}",
             f"Benchmark picks matched in archives: {len(matched)}/{len(manifest)}", ""]

    gh, gt = hit_rate(replay_green)
    ah, at = hit_rate(archived_green)
    lines += ["## 1. GREEN hit rate",
              f"- Replay-GREEN (today's code): {len(replay_green)} picks — "
              f"outcomes known {gt}: **{gh}/{gt} HIT"
              + (f" ({gh/gt:.0%})**" if gt else "**"),
              f"- Archived-GREEN (code at the time): {len(archived_green)} picks — "
              f"outcomes known {at}: {ah}/{at} HIT"
              + (f" ({ah/at:.0%})" if at else ""), ""]

    lines += ["## 1b. GREEN preservation",
              f"- Archived GREENs: {len(arch_set)} | Replay GREENs: {len(rep_set)}",
              f"- Preserved: {len(preserved)} | Lost (demoted): {len(lost)} | "
              f"New (promoted): {len(created)}"]
    for k in sorted(lost):
        r = by_key[k]
        lines.append(f"    - LOST: {r['player']} {r['stat_type']} ({r['match_date']}) "
                     f"{r['archived']:.0%}→{r['replay']:.0%}"
                     + (f" [outcome {r['outcome']}]" if r.get("outcome") else ""))
    for k in sorted(created):
        r = by_key[k]
        lines.append(f"    - NEW:  {r['player']} {r['stat_type']} ({r['match_date']}) "
                     f"{(r['archived'] or 0):.0%}→{r['replay']:.0%}"
                     + (f" [outcome {r['outcome']}]" if r.get("outcome") else ""))
    lines.append("")

    lines += ["## 1c. Winner/loser movement (the verdict numbers)",
              f"- Winning picks PROMOTED to GREEN: {len(win_promoted)}  (good)",
              f"- Winning picks DEMOTED from GREEN: {len(win_demoted)}  (bad — investigate every one)",
              f"- Losing picks PROMOTED to GREEN: {len(lose_promoted)}  (bad — investigate every one)",
              f"- Losing picks DEMOTED from GREEN: {len(lose_demoted)}  (good)", ""]

    lines += ["## 2. Confidence calibration (replay, outcomes known)"]
    for lo, hi, label in ((0.76, 0.81, "76%+"), (0.73, 0.76, "73-75%"),
                          (0.70, 0.73, "70-72%"), (0.65, 0.70, "65-69%"),
                          (0.60, 0.65, "60-64%")):
        band = [r for r in matched if r["replay"] is not None and lo <= r["replay"] < hi]
        h, t = hit_rate(band)
        if band:
            lines.append(f"- {label}: {len(band)} picks, "
                         f"{h}/{t} HIT" + (f" ({h/t:.0%})" if t else " (no outcomes yet)"))
    lines.append("")

    lines += ["## 3. Rule/cap effectiveness on GREEN candidates",
              "(which adjustments fired, and the record of picks they touched)"]
    for cause, n in sorted(cause_freq.items(), key=lambda x: -x[1]):
        touched = [r for r in matched if any(cause in a for a in r["adjustments"])]
        h, t = hit_rate(touched)
        rec = f"{h}-{t-h}" if t else "no outcomes yet"
        lines.append(f"- {cause}: fired on {n} picks — record where known: {rec}")
    if not cause_freq:
        lines.append("- No adjustments fired on any benchmark pick (all earned scores stood).")
    lines.append("")

    lines += ["## 4. Confidence changes vs archive (fix impact)",
              f"{len(moved)} of {len(matched)} picks score differently under today's code:"]
    for r in moved:
        lines.append(f"- {r['player']} {r['line']} {r['stat_type']} ({r['match_date']}): "
                     f"archived {r['archived']:.0%} {r.get('archived_color')} → "
                     f"replay {r['replay']:.0%} {r['replay_color']}"
                     + (f"  [outcome: {r['outcome']}]" if r.get("outcome") else ""))
    if grade_changed:
        lines.append(f"\nGrade changes (color flips): {len(grade_changed)} — review each above.")
    lines.append("")

    lines += ["## 5. Feature-echo findings (2+ charges sharing one root fact)"]
    if echo_report:
        for player, stat, date, echoes in echo_report:
            lines.append(f"- {player} {stat} ({date}):")
            for root, adjs in echoes.items():
                lines.append(f"    - `{root}` charged {len(adjs)}x: " + " | ".join(adjs))
    else:
        lines.append("- None detected in the scoring ledger. (Eval-side echo — one fact "
                     "boosting the anchor through multiple checkpoints — is not visible "
                     "from ledgers alone; it requires reading the reasoning archives.)")
    lines.append("")

    lines += ["## 6. Verdict framing",
              "A fix is GOOD for GREENs if: replay-GREEN hit rate holds or rises, "
              "no winner lost its GREEN, and every grade change in section 4 has a "
              "named ledger cause you agree with. A fix is SUSPECT if winners got "
              "demoted or losers got promoted with no new evidence."]

    lines += ["", "## 6b. Dataset A write-back",
              f"- validation_run_id: {validation_run_id}",
              f"- Verdicts written: {_written}",
              f"- Skipped (no eval_id -- pre-Dataset-A archive): {_skipped_no_id}"]

    # ── 2.0: trend vs previous runs ───────────────────────────────
    summary = {"ts": datetime.now().isoformat(timespec="seconds"),
               "validation_run_id": validation_run_id,
               "matched": len(matched),
               "replay_green": len(replay_green), "green_hits": gh, "green_known": gt,
               "preserved": len(preserved), "lost": len(lost), "created": len(created),
               "win_demoted": len(win_demoted), "lose_promoted": len(lose_promoted)}
    hist = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    lines += ["## 7. Trend vs previous runs"]
    if hist:
        prev = hist[-1]
        def _delta(field, label, good_when_up=True):
            d = summary[field] - prev.get(field, 0)
            arrow = "→" if d == 0 else ("↑" if d > 0 else "↓")
            return f"- {label}: {prev.get(field,0)} → {summary[field]} {arrow}"
        lines += [f"(previous run: {prev['ts']})",
                  _delta("replay_green", "Replay GREEN count"),
                  _delta("green_hits", "GREEN hits (outcomes known)"),
                  _delta("win_demoted", "Winning GREENs demoted"),
                  _delta("lose_promoted", "Losing picks promoted"),
                  "Improving = hits up, demoted-winners and promoted-losers at zero."]
    else:
        lines.append("First recorded run — this becomes the baseline.")
    hist.append(summary)
    HISTORY.write_text(json.dumps(hist, indent=2))

    report = "\n".join(lines)
    out = VAL_DIR / f"green_validation_{datetime.now().strftime('%Y-%m-%d_%H%M')}.md"
    out.write_text(report)
    print(report)
    print(f"\nSaved: {out}")

# ══ PROJECT 3 INSTRUMENT — FEATURE ECHO DEEP TRACE ════════════════
# Scans the reasoning archives (the eval model's full text) for one
# root fact appearing with confidence-bearing language in multiple
# checkpoint sections. Heuristic by nature: it finds CANDIDATES for
# echo, with the exact lines, for a human read — it does not claim to
# prove double-counting on its own.
_SECTION_PAT = re.compile(
    r"(?im)^\s*(?:\*\*|#+\s*)?(?:\[?CHECKPOINT\s+[A-L1-9][\]:]?|GATE\s*\d+|"
    r"HARD STOP|STEP \d|PART \d|DOMINANCE|SCORE PATHS?|CONFIDENCE|GRADE|"
    r"MATCH CONTROL|COMPETITION.CONTEXT|STRUCTURE|SURVIVAL)")
_CONF_PAT = re.compile(r"\d{2}\s*%|[+-]\s*\d+\s*%|ceiling|anchor|cap|deduct")

def echo_trace(since=None, limit=40):
    files = sorted(oc.ARCHIVE_DIR.glob("reasoning_*.txt"))
    if since:
        files = [f for f in files if f.stem.replace("reasoning_", "")[:10] >= since]
    files = files[-limit:]
    if not files:
        print(f"No reasoning archives found in {oc.ARCHIVE_DIR}."); return
    lines = ["# FEATURE ECHO DEEP TRACE",
             f"Generated: {datetime.now().isoformat(timespec='seconds')}",
             f"Files scanned: {len(files)}", ""]
    any_hit = False
    for f in files:
        text = f.read_text(errors="replace")
        # split into sections at checkpoint-style headers
        idxs = [m.start() for m in _SECTION_PAT.finditer(text)] or [0]
        idxs.append(len(text))
        sections = []
        for i in range(len(idxs) - 1):
            chunk = text[idxs[i]:idxs[i+1]]
            header = chunk.splitlines()[0][:60].strip() if chunk.strip() else f"section {i}"
            sections.append((header, chunk))
        findings = {}
        for root, words in ECHO_ROOTS.items():
            touched = []
            for header, chunk in sections:
                low = chunk.lower()
                if any(w in low for w in words):
                    # only count sections where the fact sits near confidence math
                    for ln in chunk.splitlines():
                        l = ln.lower()
                        if any(w in l for w in words) and _CONF_PAT.search(l):
                            touched.append((header, ln.strip()[:110]))
                            break
            if len(touched) >= 2:
                findings[root] = touched
        if findings:
            any_hit = True
            lines.append(f"## {f.name}")
            for root, touched in findings.items():
                sev = "HIGH" if len(touched) >= 3 else "REVIEW"
                lines.append(f"- ROOT FACT `{root}` — confidence-bearing in "
                             f"{len(touched)} sections [{sev}]:")
                for header, ln in touched:
                    lines.append(f"    - [{header}] {ln}")
            lines.append("")
    if not any_hit:
        lines.append("No multi-section confidence-bearing repetition of a single "
                     "root fact found in the scanned files.")
    report = "\n".join(lines)
    out = VAL_DIR / f"echo_trace_{datetime.now().strftime('%Y-%m-%d_%H%M')}.md"
    out.write_text(report)
    print(report)
    print(f"\nSaved: {out}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-manifest", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD archive floor")
    ap.add_argument("--set-outcome", default=None,
                    help='"Player|stat_type|match_date|HIT or BUST"')
    ap.add_argument("--echo-trace", action="store_true",
                    help="Project 3: scan reasoning archives for feature echo")
    args = ap.parse_args()
    if args.build_manifest:
        build_manifest(since=args.since)
    elif args.set_outcome:
        set_outcome(args.set_outcome)
    elif args.echo_trace:
        echo_trace(since=args.since)
    elif args.run:
        run(since=args.since)
    else:
        ap.print_help()

# © 2026 Demetrius Reynolds — Project Apex Intelligence™. All rights reserved.
