#!/usr/bin/env python3
"""APEX-RCA-001 (ALIS-L4 Phase 2) -- six-way loss classification.
INFRASTRUCTURE: built. VALIDATION: not started. EVIDENCE: gated below.
Order: DATA_ERROR -> FORMAT_ERROR -> VARIANCE -> MODEL_ERROR ->
CATEGORY_ERROR -> CONFIDENCE_ERROR (band-level, gated).
"""
import os, sys, argparse, statistics
from datetime import datetime
import apex_projection_analysis as PA
OUT = "apex_learning_system/ROOT_CAUSE.md"

def classify(R):
    cats, out = {}, []
    for r in R:
        cats.setdefault(r["category"], []).append(r)
    cbias = {c: statistics.mean(x["err"] for x in rs) for c, rs in cats.items()}
    for r in R:
        if r["hit"] is not False:
            continue
        if r["mean"] is None or r["sd"] is None:
            c, why = "DATA_ERROR", "no projection or sd stored at pick time"
        elif str(r.get("best_of")) not in ("3", "5"):
            c, why = "FORMAT_ERROR", "best_of missing/invalid (%r)" % r.get("best_of")
        elif r["in_band"]:
            c, why = "VARIANCE", "actual %.1f inside p10-p90; projection reasonable" % r["actual"]
        elif r["z"] is not None and abs(r["z"]) >= 2.0:
            c, why = "MODEL_ERROR", "actual %.1f is %+.1f sd from projection %.1f" % (r["actual"], r["z"], r["mean"])
        elif len(cats.get(r["category"], [])) >= 5 and abs(cbias[r["category"]]) >= 1.0 \
                and (r["err"] > 0) == (cbias[r["category"]] > 0):
            c, why = "CATEGORY_ERROR", "%s biased %+.2f across n=%d; this miss runs the same way" % (
                r["category"], cbias[r["category"]], len(cats[r["category"]]))
        else:
            c, why = "MODEL_ERROR", "actual outside p10-p90 (%+.1f from projection)" % r["err"]
        out.append(dict(r, cause=c, why=why))
    return out

def main(write=False):
    R = PA.rows()
    L = ["## Root-cause classification  (ALIS-L4 Phase 2)",
         "INFRASTRUCTURE: BUILT · VALIDATION: NOT STARTED"]
    if not R:
        L += ["**WAITING FOR DATA** -- no settled picks with projections yet."]
        print("\n".join(L)); return 0
    losses = classify(R)
    if not losses:
        L += ["Settled picks with projections: %d · losses: 0" % len(R),
              "**NOT AVAILABLE** -- no losses to classify yet."]
        print("\n".join(L)); return 0
    counts = {}
    for x in losses:
        counts[x["cause"]] = counts.get(x["cause"], 0) + 1
    n = len(losses)
    ev = "Very Low" if n < 5 else "Low" if n < 10 else "Moderate" if n < 25 else "Strong" if n < 100 else "Proven"
    L += ["Losses classified: **%d** · EVIDENCE: **%s**" % (n, ev), "",
          "| cause | count | share |", "|---|---|---|"]
    for c in ("MODEL_ERROR", "VARIANCE", "CATEGORY_ERROR", "CONFIDENCE_ERROR", "FORMAT_ERROR", "DATA_ERROR"):
        k = counts.get(c, 0)
        L.append("| %s | %d | %s |" % (c, k, ("%.0f%%" % (100.0 * k / n)) if k else "--"))
    L += ["", "**CONFIDENCE_ERROR:** NOT AVAILABLE -- band-level claim, needs >=10 losses in one band.",
          "", "### Per-loss detail", "| player | cat | line | proj | actual | cause | why |", "|---|---|---|---|---|---|---|"]
    for x in sorted(losses, key=lambda x: -x["abs_err"])[:15]:
        L.append("| %s | %s | %s | %.1f | %.1f | **%s** | %s |" % (
            str(x["player"])[:16], x["category"], x["line"], x["mean"], x["actual"], x["cause"], x["why"]))
    L += ["", "**Interpretation gate:** cause shares are descriptive only until n>=25 losses."]
    txt = "\n".join(L); print(txt)
    if write:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        hdr = "" if os.path.exists(OUT) else "# ROOT CAUSE ANALYSIS\nAppend-only.\n"
        open(OUT, "a", encoding="utf-8").write(hdr + "\n---\n# Entry %s\n%s\n" % (
            datetime.now().strftime("%Y-%m-%d %H:%M"), txt))
        print("\nappended -> %s" % OUT)
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--classify", action="store_true")
    ap.add_argument("--write", action="store_true")
    a_ = ap.parse_args()
    sys.exit(main(write=a_.write) if a_.classify else ap.print_help())
