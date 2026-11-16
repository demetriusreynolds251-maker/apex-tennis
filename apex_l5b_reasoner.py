#!/usr/bin/env python3
"""APEX-L5B-001 -- LLM hypothesis proposer.
Reads settled MODEL_ERROR losses + their reasoning cards, asks the model what
assumption may be wrong. WRITES ONLY state=Proposed. Never promotes.
Dry-run by default; --commit writes to HYPOTHESIS_REGISTRY.json.
"""
import os, re, json, glob, sys, argparse
from datetime import datetime
import apex_projection_analysis as PA
import apex_root_cause as RC
import apex_reasoning_audit as RA

try:
    import openclaw_core as _core
    L5B_MODEL = _core.MODEL_SONNET      # use the system's standard model constant
except Exception:
    L5B_MODEL = "claude-sonnet-4-6"
HYP = "apex_learning_system/HYPOTHESIS_REGISTRY.json"
OUT = "apex_learning_system/L5B_PROPOSALS.md"
ALLOWED_TYPES = {"missing_variable","weighting_issue","scaling_issue",
                 "distribution_issue","calibration_issue"}

SYSTEM = """You are a forensic analyst reviewing a tennis prop projection that failed.
You receive the model's reasoning-card inputs, its projection and uncertainty band,
the actual outcome, and the error size. Nothing else exists. Nothing else may be assumed.

HARD GROUNDING RULE. You may only make claims supported by fields present in the packet.
You may NOT infer or reference: injuries, fatigue, weather, momentum, coaching, tactics,
actual service games, actual points played, opponent identity or quality, crowd, surface
speed, or any hidden variable -- unless that exact field is supplied in the packet.
If a hypothesis would require information not in the packet, do not propose it.

TESTABILITY RULE. ALIS currently stores, per settled pick: projection (mean), sd, p10, p50,
p90, line, direction, stated probability, actual outcome, category, best_of, surface, date,
and the reasoning-card bullet text. A hypothesis is only acceptable if it can be tested by
counting or comparing THOSE fields across future settlements. If your test would require any
other data, do not propose it.

Propose AT MOST 2 hypotheses. Proposing none is a valid and often correct answer.

Return ONLY valid JSON, no prose, no fences:
{"hypotheses":[{"id":"snake_case_id","statement":"one sentence about the MODEL, not the player",
"type":"missing_variable|weighting_issue|scaling_issue|distribution_issue|calibration_issue",
"fields_used":["exact packet field names you relied on"],
"evidence_used":"ONLY literal values copied from the packet, comma separated, e.g. 'p10 1.0, actual 0.0, sd 2.331'. Do NOT put derived or computed numbers here -- if you computed something, state the reasoning in 'statement' and cite only the raw inputs here.",
"test_method":"a count or comparison over the stored fields listed above"}]}"""

def find_card(player, category, date_str):
    """L5B-004: indexed lookup (player, category, date). No glob, no fallback."""
    import apex_card_index as CI
    return CI.lookup(player, category, date_str)

def build_packets():
    """One packet per MODEL_ERROR loss that has a reasoning card."""
    R = PA.rows()
    losses = [x for x in RC.classify(R) if x["cause"] == "MODEL_ERROR"]
    out = []
    for x in losses:
        card, path = find_card(str(x["player"]), x["category"], str(x["date"]))
        if card is None:
            continue     # L5B: no matching reasoning card -> do not reason blind
        out.append({"player": x["player"], "category": x["category"], "date": x["date"],
                    "line": x["line"], "direction": x["direction"], "projection": x["mean"],
                    "sd": x["sd"], "p10": x["p10"], "p90": x["p90"], "actual": x["actual"],
                    "error": round(x["err"], 2), "z": (round(x["z"], 2) if x["z"] is not None else None),
                    "root_cause": x["cause"], "why": x["why"],
                    "card_inputs": (card or {}).get("bullets", []),
                    "card_file": os.path.basename(path) if path else None})
    return out

def ask_model(packet):
    import openclaw_core as core
    prompt = ("Failed prop:\n" + json.dumps(packet, indent=1) +
              "\n\nExisting hypotheses already on file (do not duplicate):\n" +
              json.dumps(sorted(load_hyp().keys()), indent=1))
    try:
        raw = core.call_model(L5B_MODEL, SYSTEM, prompt, temperature=0.0, max_tokens=900) \
              if hasattr(core, "call_model") else None
    except Exception as e:
        return None, "call_model failed: %s: %s" % (type(e).__name__, e)
    if raw is None:
        return None, "no call_model in core"
    txt = raw if isinstance(raw, str) else str(raw)
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    try:
        d = json.loads(txt)
    except Exception as e:
        return None, "unparseable model output (%s): %s" % (e, txt[:160])
    return d.get("hypotheses", []), None

def load_hyp():
    return json.load(open(HYP)) if os.path.exists(HYP) else {}

# FIX 2: map by observation, not by the model's chosen label.
OBSERVATION_MAP = [
    (("p10","p90","tail","tails","variance","distribution width","sd ","std","dispersion",
      "probability mass","band width","uncertainty band"), "dispersion:poisson_tails"),
    (("bo5","best-of-5","best of five","format scaling","player-level scaling"), "bo5_scaling:player_level"),
    (("return","returner","opponent hold","opponent break"), "missing_input:aces:opponent_return_strength"),
]

def _packet_numbers(packet):
    """Every number the packet actually contains, as strings, for evidence tracing."""
    vals = set()
    for k, v in packet.items():
        if isinstance(v, (int, float)) and v is not None:
            vals.add("%g" % float(v)); vals.add("%.1f" % float(v)); vals.add("%.2f" % float(v))
            vals.add("%.3f" % float(v))
        elif isinstance(v, str):
            for n in re.findall(r"\d+\.?\d*", v): vals.add(n.rstrip("."))
        elif isinstance(v, list):
            for item in v:
                for n in re.findall(r"\d+\.?\d*", str(item)): vals.add(n.rstrip("."))
    return {v.rstrip("0").rstrip(".") if "." in v else v for v in vals}

def validate(h, packet):
    """Returns (reason_rejected or None, mapped_existing_key or None).
    FIX 1: evidence whitelist -- every number cited must exist in the packet."""
    for k in ("id","statement","type","fields_used","evidence_used","test_method"):
        if not h.get(k): return "missing field %r" % k, None
    if h["type"] not in ALLOWED_TYPES: return "type %r not allowed" % h["type"], None
    if len(h["statement"]) > 300: return "statement too long", None
    blob = (h["statement"] + " " + str(h["evidence_used"]) + " " + h["test_method"]).lower()

    # FIX 1a: every numeric value in evidence_used must trace to the packet
    have = _packet_numbers(packet)
    cited = [n.rstrip(".") for n in re.findall(r"\d+\.?\d*", str(h["evidence_used"]))]
    cited_norm = [c.rstrip("0").rstrip(".") if "." in c else c for c in cited]
    if not cited_norm:
        return "evidence_used cites no packet values", None
    untraceable = [c for c in cited_norm if c not in have]
    if untraceable:
        return "evidence cites values not in the packet: %s" % untraceable, None

    # FIX 1b: the CLAIM may not depend on quantities the packet lacks.
    # Only flag "actual <NOUN>" where NOUN names a stat other than the settled one.
    UNMEASURED = ("service game", "service games played", "points played", "points won",
                  "games played", "sets played", "first serves", "return points",
                  "break points faced", "rally", "minutes", "duration")
    for phrase in re.findall(r"actual ([a-z ]{3,30})", blob):
        if any(u in phrase for u in UNMEASURED):
            return "claim depends on an unmeasured quantity: 'actual %s'" % phrase.strip(), None
    # a bare hypothetical about unmeasured play is also disallowed
    if re.search(r"\bif fewer games\b|\bfewer service games\b|\bgames actually played\b", blob):
        return "claim depends on unmeasured match volume", None

    fields = [str(x).lower().strip() for x in (h["fields_used"] if isinstance(h["fields_used"], list) else [h["fields_used"]])]
    STORED = {"projection","mean","sd","p10","p50","p90","line","direction","probability",
              "prob","actual","category","best_of","surface","date","card_inputs","error","z"}
    unknown = [f for f in fields if not any(sf in f for sf in STORED)]
    if unknown: return "fields_used contains data ALIS does not store: %s" % unknown, None

    tm = h["test_method"].lower()
    if not any(w in tm for w in ("count","compare","frequency","rate","proportion","across","per ")):
        return "test_method is not a countable comparison", None
    if "card_inputs" in tm and any(w in tm for w in ("extract","parse","mentions","stated service","from card")):
        return "test_method requires parsing free text ALIS does not index", None
    UNTESTABLE = ("service game","points played","games played","sets played","minutes",
                  "duration","first serve","return point","break point faced")
    if any(u in tm for u in UNTESTABLE):
        return "test_method requires a stat ALIS does not store", None

    # FIX 2: observation-based mapping, label-independent
    for keywords, existing in OBSERVATION_MAP:
        if any(k in blob for k in keywords):
            return None, existing
    return None, None

def run(commit=False, limit=6):
    packets = build_packets()
    L = ["## L5-B proposals (LLM reasoner)",
         "CONTAINMENT: writes state=Proposed only. Cannot promote. L4 owns all gates.", ""]
    if not packets:
        L.append("**WAITING FOR DATA** -- no MODEL_ERROR losses with reasoning cards yet.")
        print("\n".join(L)); return 0
    hyp = load_hyp(); added = 0
    L.append("MODEL_ERROR losses available: **%d** (reasoning on first %d)\n" % (len(packets), min(limit, len(packets))))
    for p in packets[:limit]:
        L.append("### %s %s %s (proj %.1f, actual %.1f, z %s)" % (
            p["player"], p["category"], p["line"], p["projection"], p["actual"], p["z"]))
        L.append("Card inputs cited: %s" % ("; ".join(p["card_inputs"]) or "card not found"))
        hs, err = ask_model(p)
        if err:
            L.append("- **SKIPPED** -- %s" % err); continue
        if not hs:
            L.append("- model proposed nothing (valid answer)"); continue
        for h in hs[:2]:
            bad, maps_to = validate(h, p)
            L += ["", "  **Hypothesis:** %s" % h.get("statement", "?"),
                  "  **Type:** %s" % h.get("type", "?"),
                  "  **Evidence used:** %s" % h.get("evidence_used", "?"),
                  "  **Fields referenced:** %s" % h.get("fields_used", "?"),
                  "  **Test method:** %s" % h.get("test_method", "?")]
            if bad:
                L += ["  **Result: ❌ REJECTED**", "  **Reason:** %s" % bad]; continue
            if maps_to:
                L += ["  **Result: ↪ MAPPED**", "  **Reason:** already captured by `%s` -- not duplicated" % maps_to]
                continue
            key = "l5b:%s:%s" % (p["category"], h["id"])
            if key in hyp:
                L += ["  **Result: ↪ DUPLICATE**", "  **Reason:** `%s` already on file" % key]; continue
            L += ["  **Result: ✅ ACCEPTED** -> `%s`" % key, "  **State:** Proposed (cannot self-promote)"]
            hyp[key] = {"statement": h["statement"], "state": "Proposed", "source": "L5B-llm",
                        "type": h["type"], "category": p["category"],
                        "evidence_used": h["evidence_used"], "fields_used": h["fields_used"], "test_method": h["test_method"],
                        "origin_pick": "%s %s %s on %s" % (p["player"], p["category"], p["line"], p["date"]),
                        "occurrences": 0, "validated": False, "retired": False,
                        "adoption_status": "not adopted", "outcome_after_adoption": "NOT AVAILABLE",
                        "first_seen": datetime.now().strftime("%Y-%m-%d")}
            added += 1
        L.append("")
    L.append("**New Proposed hypotheses: %d**" % added)
    txt = "\n".join(L); print(txt)
    if not commit:
        print("\nDRY RUN -- nothing written. Re-run with --commit."); return 0
    os.makedirs(os.path.dirname(HYP), exist_ok=True)
    json.dump(hyp, open(HYP, "w"), indent=1)
    hdr = "" if os.path.exists(OUT) else "# L5-B PROPOSALS\nAppend-only. Proposed state only.\n"
    open(OUT, "a", encoding="utf-8").write(hdr + "\n---\n# Entry %s\n%s\n" % (
        datetime.now().strftime("%Y-%m-%d %H:%M"), txt))
    print("\nwrote %d proposal(s) -> %s and %s" % (added, HYP, OUT))
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--commit", action="store_true")
    a_ = ap.parse_args()
    sys.exit(run(commit=a_.commit) if (a_.run or a_.commit) else ap.print_help())
