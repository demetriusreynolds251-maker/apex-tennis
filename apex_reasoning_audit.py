#!/usr/bin/env python3
"""APEX-L5-001 System A -- reasoning-card parser + expected-input audit.
Proposes hypotheses when a card omits an input the category should carry.
PROPOSES ONLY. Never promotes. L4 owns promotion.
"""
import os, re, glob, json, sys, argparse
from datetime import datetime

CARDS = "data/picks_archive"                      # reasoning_*.txt live in the openclaw root
HYP = "apex_learning_system/HYPOTHESIS_REGISTRY.json"
OUT = "apex_learning_system/REASONING_AUDIT.md"

# What a correct card for each category should reference. Keys are the concept;
# values are the substrings that count as evidence the concept was used.
EXPECTED = {
 "aces": {"player ace rate": ["ace rate"],
          "expected service games": ["service games"],
          "opponent return strength": ["return", "opponent hold", "opponent break"]},
 "dfs":  {"player df rate": ["double-fault rate", "double fault rate"],
          "expected service games": ["service games"],
          "dispersion component": ["dispersion", "variance", "spread", "overdispers"]},
 "double_faults": {"player df rate": ["double-fault rate", "double fault rate"],
          "expected service games": ["service games"],
          "dispersion component": ["dispersion", "variance", "spread", "overdispers"]},
 "breaks": {"opponent hold rate": ["opponent holds", "opponent hold"],
          "player break rate": ["break rate"],
          "opponent service games": ["service games"]},
 "games_won": {"match length": ["expected games", "match length", "total games"],
          "player strength": ["hold", "player strength", "price", "favorite"],
          "opponent strength": ["opponent"]},
 "fantasy_score": {"component stats": ["aces", "double", "games"],
          "match length": ["games", "sets"]},
 "total_games": {"match length": ["expected games", "total games", "sets"],
          "both players hold": ["hold"]},
 "total_sets": {"win probability": ["price", "favorite", "probability", "simulated"],
          "match format": ["bo5", "bo3", "best-of", "sets"]},
}

def parse_card(path):
    raw = open(path, encoding="utf-8", errors="replace").read()
    # HARDENING 1: everything before the separator is header/context, not the pick.
    body_txt = raw.split("\u2500" * 8)[-1] if ("\u2500" * 8) in raw else raw
    g = lambda pat, src=None, d=None: (re.search(pat, src if src is not None else body_txt, re.I).group(1).strip()
                                       if re.search(pat, src if src is not None else body_txt, re.I) else d)
    # HARDENING 2: category comes from the tier line only (emoji-prefixed), never the header.
    cat = g(r"[\U0001F7E2\U0001F7E1\U0001F7E0\U0001F534]\s*(?:ELITE|GOOD|CAUTION|LOW CONFIDENCE)\s*\u00b7\s*([A-Za-z ]+)")
    bullets = [l.strip("\u2022 ").strip() for l in body_txt.splitlines() if l.strip().startswith("\u2022")]
    gen = re.search(r"Generated:\s*([\d: ]+[AP]M)[^|]*\|\s*([\d-]+)", raw)
    return {"file": os.path.basename(path),
            "players": (re.search(r"^DEMETRIUS -- PROPS REASONING\n(.+)$", raw, re.M).group(1).strip()
                        if re.search(r"^DEMETRIUS -- PROPS REASONING\n(.+)$", raw, re.M) else ""),
            "date": gen.group(2) if gen else "", "gen_time": gen.group(1).strip() if gen else "",
            "category_label": (cat or "").strip().lower(),
            "line": g(r"`([\d.]+)\s+[\w_ ]+\u2014"),
            "stat": (g(r"`[\d.]+\s+([\w_ ]+)\u2014") or "").strip(),
            "direction": "MORE" if "more \u2b06" in body_txt.lower() else ("LESS" if "less \u2b07" in body_txt.lower() else ""),
            "projection": g(r"Projection \*\*([\d.]+)\*\*"),
            "p10_p90": g(r"\(10-90:\s*([\d.]+\u2013[\d.]+)\)"),
            "probability": g(r"Probability \*\*(\d+)%"),
            "bo": "5" if "\u00b7 Bo5" in body_txt else ("3" if "\u00b7 Bo3" in body_txt else ""),
            "bullets": bullets, "text": body_txt.lower()}

MIN_DATE = "2026-08-24"          # HARDENING 3: pre-2026-08-24 cards use an older format
POISON_DATE = "2026-08-29"       # 08-29 cards at >=95% predate FMT-001/SLAM-001 fixes

def excluded(card, path):
    if "_moneyline_" in os.path.basename(path): return "moneyline (no schema family)"
    d = card.get("date") or ""
    if d and d < MIN_DATE: return "stale era (pre-%s format)" % MIN_DATE
    if not card.get("category_label") and not card.get("projection"): return "unparseable / legacy format"
    try:
        if d == POISON_DATE and float(card.get("probability") or 0) >= 95:
            return "pre-fix artifact (>=95%% on %s)" % POISON_DATE
    except ValueError: pass
    return None

def cat_key(card):
    lbl = card["category_label"].replace(" ", "_")
    stat = (card["stat"] or "").strip().replace(" ", "_")
    for k in EXPECTED:
        if k in (lbl, stat): return k
    return {"double_faults": "dfs", "break_points_won": "breaks",
            "total_games_won": "games_won"}.get(stat, lbl or stat)

def audit_card(card):
    """Returns list of (concept, present:boolean) for the card's category."""
    k = cat_key(card)
    spec = EXPECTED.get(k)
    if not spec: return k, None
    found = []
    for concept, needles in spec.items():
        present = any(n in card["text"] for n in needles)
        found.append((concept, present))
    return k, found

def load_hyp():
    return json.load(open(HYP)) if os.path.exists(HYP) else {}

def save_hyp(d):
    os.makedirs(os.path.dirname(HYP), exist_ok=True)
    json.dump(d, open(HYP, "w"), indent=1)

def run(write=False):
    files = sorted(glob.glob(os.path.join(CARDS, "reasoning_*.txt")))
    L = ["## Reasoning-card audit (ALIS-L5 System A)",
         "PROPOSES ONLY -- no hypothesis here is promoted, observed, supported, or validated.", ""]
    if not files:
        L.append("**WAITING FOR DATA** -- no reasoning cards found in %s" % os.path.abspath(CARDS))
        print("\n".join(L)); return 0
    hyp = load_hyp(); gaps = {}
    L += ["Cards parsed: **%d**" % len(files), "",
          "| card | cat | line | proj | prob | missing expected inputs |", "|---|---|---|---|---|---|"]
    skipped = {}
    for f in files:
        c = parse_card(f)
        why = excluded(c, f)
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        k, found = audit_card(c)
        if found is None:
            L.append("| %s | %s | %s | %s | %s | (no schema for this category) |" % (
                c["file"][:34], k, c["line"], c["projection"], c["probability"])); continue
        missing = [concept for concept, present in found if not present]
        for m in missing:
            key = "missing_input:%s:%s" % (k, m.replace(" ", "_"))
            g = gaps.setdefault(key, {"cat": k, "concept": m, "cards": []})
            g["cards"].append("%s (proj %s, %s%%)" % (c["players"] or c["file"], c["projection"], c["probability"]))
        L.append("| %s | %s | %s | %s | %s | %s |" % (c["file"][:34], k, c["line"], c["projection"],
                 c["probability"], ", ".join(missing) if missing else "none"))
    L += ["", "### Exclusions"]
    L += (["- %s: **%d** card(s)" % (w, n) for w, n in sorted(skipped.items())] or ["- none"])
    L += ["- UNSCHEMATIZED (parsed, no schema, generates nothing): counted in table above", ""]
    L += ["", "### Candidate hypotheses (state: Proposed)", ""]
    if not gaps:
        L.append("None -- every parsed card referenced its expected inputs.")
    ts = datetime.now().strftime("%Y-%m-%d")
    for key, g in sorted(gaps.items(), key=lambda kv: -len(kv[1]["cards"])):
        prev = hyp.get(key, {})
        L += ["**`%s`**" % key,
              "- Statement: %s projections may be underweighting **%s** -- the concept is absent from the reasoning card." % (g["cat"], g["concept"]),
              "- Occurrences: %d card(s)" % len(g["cards"]),
              "- Supporting examples: %s" % "; ".join(g["cards"][:4]),
              "- Counter examples: %s" % "cards in this category that DO cite it: see table above",
              "- Confidence: **Proposed** (structural observation, not outcome-tested)",
              "- Promotion: requires L4 settled evidence. Cannot self-promote.", ""]
        hyp[key] = {"statement": "%s may underweight %s (absent from reasoning card)" % (g["cat"], g["concept"]),
                    "state": "Proposed", "source": "L5-SystemA", "category": g["cat"],
                    "concept": g["concept"], "occurrences": len(g["cards"]),
                    "examples": g["cards"][:8], "counter_examples": [],
                    "confidence": "Proposed", "evidence": "structural only -- no settled outcomes yet",
                    "first_seen": prev.get("first_seen", ts), "last_seen": ts,
                    "promoted": False, "validated": False}
    txt = "\n".join(L); print(txt)
    if write:
        save_hyp(hyp)
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        hdr = "" if os.path.exists(OUT) else "# REASONING AUDIT (ALIS-L5)\nAppend-only.\n"
        open(OUT, "a", encoding="utf-8").write(hdr + "\n---\n# Entry %s\n%s\n" % (
            datetime.now().strftime("%Y-%m-%d %H:%M"), txt))
        print("\nwrote -> %s and %s" % (OUT, HYP))
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--write", action="store_true")
    a_ = ap.parse_args()
    sys.exit(run(write=a_.write) if a_.audit else ap.print_help())
