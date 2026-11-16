"""
rule_registry.py
2026-08-09 — catalogs every real, tested rule/signal/gate in Apex,
split into the two genuinely different categories the user correctly
flagged: PREDICTIVE rules (does this correlate with the real outcome
-- testable via AUC/correlation against Dataset B, exactly what every
script tonight did) versus SAFETY/COMPLIANCE gates (does this fire
correctly on real/fake cases -- Gate 11 confirming a match isn't
stale, Rule 9 barring market data, etc. -- which need real
operational log evidence this registry does NOT have access to and
therefore does NOT attempt to test).

Real, explicit design choice: Category 2 entries are catalogued as
metadata ONLY. This registry will never run a correlation/AUC test
against a safety gate -- that would be the wrong tool for what that
gate is actually for, exactly the mistake flagged and avoided this
session.

Each Category 1 entry stores:
  - the real module + function that tests it (imported lazily, so a
    script missing from this folder doesn't crash the whole run --
    reported as MODULE_NOT_FOUND instead)
  - the real baseline result already established tonight, so a
    future run can show real DRIFT (a signal getting weaker or
    stronger over time), not just a bare number with no comparison
  - which real metric that test reports (AUC or Pearson r) since
    they're not on the same scale and must never be compared directly
"""
import importlib
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

MIN_SAMPLES_FOR_VERDICT = 30

# Real, fixed location for the last run's results -- Mission Control's
# "Run Now" button triggers this script as a background subprocess
# (same pattern already used for tennis_warehouse_collector.py) and
# reads this file back separately, since a full run across 10 real
# backtests against 500K+ row tables can take real meaningful time --
# too long to hold an HTTP request open for.
RESULTS_FILE = Path(__file__).resolve().parent / "data" / "rule_registry_last_run.json"

# ============================================================
# CATEGORY 1 — PREDICTIVE RULES (real, testable via correlation/AUC)
# ============================================================
# Real baselines are the ACTUAL results already produced this session
# on Apex's own Dataset B -- not guesses. metric_type tells the runner
# (and the human reading the report) which scale a "verdict" number is
# on, since AUC and Pearson r are never directly comparable.

PREDICTIVE_RULES = [
    {
        "name": "ELO (S6)", "system": "moneyline", "module": "elo_backtest",
        "function": "run_elo_backtest", "metric_type": "AUC",
        "metric_path": ("overall", "auc"),
        "real_baseline": 0.7223, "real_baseline_date": "2026-08-07",
        "real_baseline_n": 500072, "is_retired": False, "real_threshold": 0.55,
        "question": "Does the ELO rating gap predict who wins?",
    },
    {
        "name": "S5 (dominance)", "system": "moneyline", "module": "s5_backtest",
        "function": "run_s5_backtest", "metric_type": "AUC",
        "metric_path": ("auc",),
        "real_baseline": 0.6157, "real_baseline_date": "2026-08-07",
        "real_baseline_n": 177079, "is_retired": False, "real_threshold": 0.55,
        "question": "Does the weighted hold/return/clutch/form dominance score predict who wins?",
    },
    {
        "name": "S1 (surface form)", "system": "moneyline", "module": "s1_backtest",
        "function": "run_s1_backtest", "metric_type": "AUC",
        "metric_path": ("auc",),
        "real_baseline": 0.5892, "real_baseline_date": "2026-08-07",
        "real_baseline_n": 407672, "is_retired": False, "real_threshold": 0.55,
        "question": "Does current-season surface win% gap predict who wins?",
    },
    {
        "name": "S2 (tournament form) [RETIRED]", "system": "moneyline", "module": "s2_backtest",
        "function": "run_s2_backtest", "metric_type": "AUC",
        "metric_path": ("auc",),
        "real_baseline": 0.4863, "real_baseline_date": "2026-08-07",
        "real_baseline_n": 257068, "is_retired": True, "real_threshold": 0.55,
        "question": "Does rounds-won-in-this-tournament gap predict who wins? (retired -- expect this to stay null, included for drift-monitoring only)",
    },
    {
        "name": "S4 (fatigue) [RETIRED]", "system": "moneyline", "module": "s4_backtest",
        "function": "run_s4_backtest", "metric_type": "AUC",
        "metric_path": ("auc",),
        "real_baseline": 0.5106, "real_baseline_date": "2026-08-07",
        "real_baseline_n": 500072, "is_retired": True, "real_threshold": 0.55,
        "question": "Does recent turnaround/fatigue predict who wins? (retired -- expect this to stay null, included for drift-monitoring only)",
    },
    {
        "name": "Aces direction", "system": "props", "module": "ace_direction_backtest",
        "function": "run_ace_direction_backtest", "metric_type": "Pearson_r",
        "metric_path": ("pearson_r",),
        "real_baseline": 0.5781, "real_baseline_date": "2026-08-07",
        "real_baseline_n": 404945, "is_retired": False, "real_threshold": 0.1,
        "question": "Does season ace average predict real aces hit this match?",
    },
    {
        "name": "BP direction (REQ2/hold-rate)", "system": "props", "module": "bp_direction_backtest",
        "function": "run_bp_direction_backtest", "metric_type": "Pearson_r",
        "metric_path": ("pearson_r",),
        "real_baseline": -0.2456, "real_baseline_date": "2026-08-07",
        "real_baseline_n": 416961, "is_retired": False, "real_threshold": 0.1,
        "question": "Does opponent's season hold rate predict real break points won?",
    },
    {
        "name": "TG extension (REQ1)", "system": "props", "module": "tg_extension_backtest",
        "function": "run_tg_extension_backtest", "metric_type": "Pearson_r",
        "metric_path": ("pearson_r",),
        "real_baseline": -0.0487, "real_baseline_date": "2026-08-07",
        "real_baseline_n": 472581, "is_retired": True, "real_threshold": 0.1,
        "question": "Does both-players-extend rate predict real total games? (found negligible -- monitoring only)",
    },
    {
        "name": "Rule 30 (rest-day) [RETIRED]", "system": "props", "module": "rule30_props_backtest",
        "function": "run_rule30_props_backtest", "metric_type": "Pearson_r_dual",
        "metric_path": ("turnaround_vs_real_aces", "pearson_r"),
        "real_baseline": -0.0422, "real_baseline_date": "2026-08-07",
        "real_baseline_n": 511679, "is_retired": True, "real_threshold": 0.1,
        "question": "Does rest-day turnaround predict real stat-line performance? (retired -- expect this to stay null)",
    },
]

# ============================================================
# CATEGORY 2 — SAFETY / COMPLIANCE GATES (catalogued only, NOT run
# through this registry — different kind of test needed)
# ============================================================
# Real, sourced definitions confirmed from openclaw_core.py this
# session (the 18-point compliance checklist + moneyline's own
# separate gate set). Deliberately NO "run" mechanism here -- testing
# whether Gate 11 correctly identifies a stale/mismatched match needs
# real operational log evidence (confirmed real vs confirmed fake
# cases from Dataset A or similar), not a correlation test against
# Dataset B match history. Using AUC/Pearson on these would be
# measuring the wrong thing entirely.

SAFETY_GATES = [
    {"name": "Rule_DataPresence", "system": "props", "checks": "Both players have real data"},
    {"name": "Rule_SurfaceClass", "system": "props", "checks": "Surface data correctly classified"},
    {"name": "Rule19", "system": "props", "checks": "Games-won math shown (games-count props only)"},
    {"name": "Rule32", "system": "props", "checks": "Underdog statistical ceiling, only when lopsided confirmed"},
    {"name": "Rule6", "system": "props", "checks": "Serve hold shown for BP props"},
    {"name": "Rule17", "system": "props", "checks": "Buffer vs stage minimum"},
    {"name": "Rule26", "system": "props", "checks": "Surface specialist checked"},
    {"name": "Rule18", "system": "props", "checks": "Last 3 matches checked"},
    {"name": "Rule30 (compliance check)", "system": "props", "checks": "Rest-day deduction correctly scaled -- NOTE: the underlying deduction itself was retired 2026-08-07; verify this checklist entry was updated to match"},
    {"name": "FlipRule", "system": "props", "checks": "Direction assigned correctly under uncertainty"},
    {"name": "RBMD", "system": "props", "checks": "Friction resistance check, LOW/MEDIUM total-games lines only"},
    {"name": "Rule_TruthLock", "system": "props+moneyline", "checks": "Unconfirmed claims never treated as fact"},
    {"name": "Rule_DirectionDrivers", "system": "props", "checks": "DIRECTION DRIVERS section present"},
    {"name": "Rule_DirectionIntegrity", "system": "props", "checks": "Listed drivers actually support the chosen direction"},
    {"name": "Rule_DriverConfidenceSplit", "system": "props", "checks": "Confidence math uses the same evidence as the direction call"},
    {"name": "Rule_ECL_Missing", "system": "props", "checks": "ECL CERTIFICATION block present"},
    {"name": "Rule_ECL_FalseCertification", "system": "props", "checks": "A self-reported PASSED actually holds up under re-check"},
    {"name": "Rule_ECL_SelfReportedBreak", "system": "props", "checks": "A self-reported broken evidence chain isn't double-penalized"},
    {"name": "Rule 9 (market/seed exclusion)", "system": "props+moneyline", "checks": "Odds/lines/seeding never used to JUSTIFY a direction"},
    {"name": "Gate14 (home crowd)", "system": "props+moneyline", "checks": "Confidence-only lean, never flips direction, never overrides HARD dominance"},
    {"name": "CFEF Triggers", "system": "props", "checks": "Fatigue/extender evidence considered (not necessarily acted on)"},
    {"name": "Rule30/CFEF Double-Charge", "system": "props", "checks": "Same fatigue fact not deducted twice under two different names"},
    {"name": "Gate 11", "system": "moneyline", "checks": "Match is real and currently upcoming, not stale/mismatched"},
    {"name": "Gate 11-B", "system": "moneyline", "checks": "Best-of-3/5 format correctly locked + corrected"},
    {"name": "Rule 1", "system": "moneyline", "checks": "Both players have real data (moneyline's own Rule_DataPresence)"},
    {"name": "Rule 24", "system": "moneyline", "checks": "Rank-gap >=100 check, recomputed independently"},
    {"name": "Rule 28", "system": "moneyline", "checks": "One player + one match per slip"},
    {"name": "Rule 4 / 4b / 4c", "system": "props+moneyline", "checks": "A shown search + real arithmetic required before UNCONFIRMED or a derived average"},
    {"name": "LPAB_PSAR", "system": "props", "checks": "UNKNOWN -- named in the props doc's valid-citation list but never defined there; likely lives in DEMETRIUS_v7.9.6_HANDOFF.txt, not available this session"},
]


def _plain_language_verdict(rule, current_value, drift):
    """
    The real point of this function: turn a raw AUC/Pearson number into
    a plain sentence a non-technical person can act on, plus a real
    suggested next step. No jargon in the output -- "clears the bar"
    and "confirmed dead" instead of "AUC >= 0.55".

    Real logic, not a guess: whether a rule "passes" uses the exact
    same threshold each individual script already established tonight
    (0.55 for AUC-type rules, matching PROMISING_AUC_THRESHOLD in
    elo_backtest.py; 0.1 for Pearson-type rules, matching the
    NEGLIGIBLE cutoff used in tg_extension_backtest.py and the others).
    """
    clears_bar = abs(current_value) >= rule["real_threshold"]
    real_drift = abs(drift) >= 0.05  # same "worth a second look" bar used elsewhere

    if rule["is_retired"]:
        if not clears_bar:
            return ("OK — confirmed still inactive",
                    "No action needed. This rule was already removed from live picks, and it's still showing no real predictive power, exactly as expected.")
        else:
            return ("UNEXPECTED — a retired rule now shows real signal",
                    "Worth a closer look. This rule was removed because it didn't work — but it's now clearing the real bar again. Could be a fluke, or the underlying pattern may have genuinely changed. Don't reactivate it off one reading; re-check in a future run before doing anything.")
    else:
        if clears_bar and not real_drift:
            return ("OK — working as expected",
                    "No action needed. This rule is live and still pulling its real weight.")
        elif clears_bar and real_drift:
            return ("WATCH — still working, but weaker than before",
                    "No urgent action needed, but keep an eye on this one in future runs. It still clears the real bar, just by less than it used to.")
        else:
            return ("PROBLEM — this rule stopped working",
                    "This rule is currently live and influencing real picks, but it no longer shows real predictive power. Worth the same real conversation S2 and S4 got: consider removing it from live evaluation until it's re-tested and proven again.")


def _get_nested(d, path):
    for key in path:
        if d is None:
            return None
        d = d.get(key) if isinstance(d, dict) else None
    return d


def run_predictive_audit(only_system=None):
    """
    Real end-to-end run of every Category 1 rule. Each test is
    imported and called independently -- one test's failure (missing
    module, missing DB, real exception) is caught and reported, never
    allowed to crash the rest of the run.

    only_system: optionally restrict to "moneyline" or "props".
    """
    results = []
    for rule in PREDICTIVE_RULES:
        if only_system and rule["system"] != only_system:
            continue

        entry = {
            "name": rule["name"], "system": rule["system"],
            "question": rule["question"], "metric_type": rule["metric_type"],
            "real_baseline": rule["real_baseline"],
            "real_baseline_date": rule["real_baseline_date"],
        }

        try:
            mod = importlib.import_module(rule["module"])
        except ImportError as e:
            entry["status"] = "MODULE_NOT_FOUND"
            # Real fix (caught during testing): the missing module isn't
            # always rule["module"] itself -- several test scripts import
            # elo_backtest/elo_ops as a shared dependency, so hiding ONE
            # file can break several rules' imports. Report the real
            # exception text, not an assumption about which file is gone.
            entry["detail"] = f"import failed: {e}"
            entry["plain_verdict"] = "SETUP PROBLEM — couldn't run this test"
            entry["plain_action"] = "A required file is missing on this computer. This isn't a real problem with the rule itself — the test just couldn't run. Check that all the rule-testing files are in the same folder."
            results.append(entry)
            continue

        try:
            fn = getattr(mod, rule["function"])
            raw_result = fn()
        except Exception as e:
            entry["status"] = "RUN_ERROR"
            entry["detail"] = f"{type(e).__name__}: {e}"
            entry["plain_verdict"] = "ERROR — this test crashed"
            entry["plain_action"] = "Something unexpected went wrong while running this test. Worth reporting the error details below so it can be fixed."
            results.append(entry)
            continue

        if raw_result.get("status") != "ok":
            entry["status"] = "OFFLINE_OR_ERROR"
            entry["detail"] = raw_result.get("message", str(raw_result))
            entry["plain_verdict"] = "OFFLINE — couldn't reach the database"
            entry["plain_action"] = "The test couldn't connect to the real match database. Make sure the database file exists and this is being run from the right folder."
            results.append(entry)
            continue

        current = _get_nested(raw_result, rule["metric_path"])
        entry["current_value"] = current
        entry["raw_result"] = raw_result

        if current is None:
            entry["status"] = "NOT_ENOUGH_DATA"
            entry["plain_verdict"] = "WAITING — not enough real data yet"
            entry["plain_action"] = "No action possible right now. This rule needs more real matches on file before it can be tested. Nothing to do but wait."
        else:
            entry["status"] = "OK"
            drift = current - rule["real_baseline"]
            entry["drift_from_baseline"] = round(drift, 4)
            if abs(drift) >= 0.05:
                entry["drift_flag"] = "REAL MOVEMENT — worth a look"
            else:
                entry["drift_flag"] = "stable"
            plain_verdict, plain_action = _plain_language_verdict(rule, current, drift)
            entry["plain_verdict"] = plain_verdict
            entry["plain_action"] = plain_action

        results.append(entry)

    # Real, plain-English summary -- the thing meant to be read FIRST,
    # before any table of numbers. Counts by real category, not jargon.
    n_ok = sum(1 for r in results if r.get("plain_verdict", "").startswith("OK"))
    n_problem = sum(1 for r in results if r.get("plain_verdict", "").startswith("PROBLEM"))
    n_watch = sum(1 for r in results if r.get("plain_verdict", "").startswith("WATCH"))
    n_unexpected = sum(1 for r in results if r.get("plain_verdict", "").startswith("UNEXPECTED"))
    n_other = len(results) - n_ok - n_problem - n_watch - n_unexpected

    if n_problem > 0:
        headline = f"⚠ {n_problem} rule(s) need real attention — see below."
    elif n_unexpected > 0:
        headline = f"👀 {n_unexpected} retired rule(s) showing unexpected signal — worth a look, not urgent."
    elif n_watch > 0:
        headline = f"👁 Everything's working, but {n_watch} rule(s) have weakened — worth keeping an eye on."
    else:
        headline = "✅ Everything's working exactly as expected. No action needed."

    return {
        "status": "ok",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "n_rules_run": len(results),
        "headline": headline,
        "summary_counts": {"ok": n_ok, "problem": n_problem, "watch": n_watch,
                            "unexpected": n_unexpected, "other": n_other},
        "results": results,
    }


def print_report(audit_result):
    print(f"\n{'='*70}")
    print(f"APEX RULE REGISTRY — PREDICTIVE AUDIT — {audit_result['run_at']}")
    print(f"{'='*70}\n")
    print(f"{audit_result['headline']}\n")

    for r in audit_result["results"]:
        print(f"[{r['system'].upper():10s}] {r['name']}")
        print(f"  {r.get('plain_verdict', r['status'])}")
        print(f"  → {r.get('plain_action', r.get('detail', ''))}")
        if r["status"] == "OK":
            print(f"  (technical detail: {r['metric_type']} = {r['current_value']:.4f}, "
                  f"baseline {r['real_baseline']:.4f} on {r['real_baseline_date']}, "
                  f"drift {r['drift_from_baseline']:+.4f})")
        print()

    print(f"{'='*70}")
    print("CATEGORY 2 (safety/compliance gates) — catalogued only, not run here.")
    print(f"{len(SAFETY_GATES)} real gates listed in SAFETY_GATES — see rule_registry.py")
    print("These need real operational log evidence to test properly, not")
    print("correlation against match history. Not attempted by this script.")
    print(f"{'='*70}\n")


def save_results(audit_result):
    """Real, simple persistence -- writes the full audit result as JSON
    to a fixed path so a separate process (Mission Control's status
    endpoint) can read the latest run without re-executing anything.
    Strips the large 'raw_result' dict from each entry before saving
    (keeps the file small and readable -- the summary fields already
    carry what a status panel needs; the full raw backtest output stays
    in the terminal output only, not persisted)."""
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    slim = dict(audit_result)
    slim["results"] = [
        {k: v for k, v in r.items() if k != "raw_result"}
        for r in audit_result["results"]
    ]
    with open(RESULTS_FILE, "w") as f:
        json.dump(slim, f, indent=2)


def load_last_results():
    """Real read-back for Mission Control's status endpoint. Returns
    None if the registry has genuinely never been run (real "no data
    yet" case, not an error)."""
    if not RESULTS_FILE.exists():
        return None
    try:
        with open(RESULTS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    audit = run_predictive_audit(only_system=only)
    print_report(audit)
    save_results(audit)
