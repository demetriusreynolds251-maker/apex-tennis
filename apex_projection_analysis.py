#!/usr/bin/env python3
"""APEX-PROJ-001 (ALIS-L4 Phase 1) -- projection storage, error, MAE, drift.
Joins settled picks to the simulator's stored projections (mean/sd/p10/p50/p90).
Read-only. Outputs WAITING FOR DATA until real joins exist.
  --analyze [--write]
"""
import csv, os, sys, argparse, statistics
from datetime import datetime
SIM = "apex_prop_sim_forward_log_v1.csv"
OUT = "apex_learning_system/PROJECTION_REGISTRY.md"

def _f(v):
    try: return float(v)
    except (TypeError, ValueError): return None

def rows():
    """Settled sim-log rows carrying a projection. This IS the backfill: the
    projections were written at pick time; nothing is recomputed."""
    if not os.path.exists(SIM): return []
    out = []
    for r in csv.DictReader(open(SIM, newline="", encoding="utf-8")):
        act, line, mean = _f(r.get("actual")), _f(r.get("line")), _f(r.get("mean"))
        if act is None or line is None or mean is None: continue
        po, pu = _f(r.get("p_over")) or 0, _f(r.get("p_under")) or 0
        d = "MORE" if po >= pu else "LESS"
        hit = None if act == line else ((act > line) if d == "MORE" else (act < line))
        out.append({"date": r.get("match_date",""), "player": r.get("player"), "category": r.get("category"),
                    "best_of": r.get("best_of"), "line": line, "actual": act, "mean": mean,
                    "sd": _f(r.get("sd")), "p10": _f(r.get("p10")), "p90": _f(r.get("p90")),
                    "err": act - mean, "abs_err": abs(act - mean),
                    "z": ((act - mean) / _f(r.get("sd"))) if _f(r.get("sd")) else None,
                    "in_band": (_f(r.get("p10")) is not None and _f(r.get("p90")) is not None
                                and _f(r.get("p10")) <= act <= _f(r.get("p90"))),
                    "hit": hit, "direction": d})
    return out

def analyze(write=False):
    R = rows()
    L = ["## Projection analysis  (scorer-independent; ALIS-L4 Phase 1)"]
    if not R:
        L += ["**WAITING FOR DATA** -- no settled picks carry a stored projection yet.",
              "(Projections are written at pick time to %s; this fills as picks settle.)" % SIM]
        print("\n".join(L)); return 0
    mae = statistics.mean(r["abs_err"] for r in R)
    bias = statistics.mean(r["err"] for r in R)
    inb = sum(1 for r in R if r["in_band"])
    L += ["Settled picks with projections: **%d**" % len(R),
          "Overall MAE: **%.2f** · bias: **%+.2f** (positive = actual exceeds projection)" % (mae, bias),
          "Inside p10-p90 band: **%d/%d (%.0f%%)** -- expected ~80%% if the distribution is honest" % (inb, len(R), 100*inb/len(R)),
          "", "### By category", "| category | n | MAE | bias | in-band% | evidence |", "|---|---|---|---|---|---|"]
    cats = {}
    for r in R: cats.setdefault(r["category"], []).append(r)
    for c, rs in sorted(cats.items()):
        n = len(rs)
        ev = "Very Low" if n < 5 else "Low" if n < 10 else "Moderate" if n < 25 else "Strong" if n < 100 else "Proven"
        L.append("| %s | %d | %.2f | %+.2f | %.0f%% | %s |" % (c, n,
                 statistics.mean(x["abs_err"] for x in rs), statistics.mean(x["err"] for x in rs),
                 100*sum(1 for x in rs if x["in_band"])/n, ev))
    L += ["", "### Drift (MAE by settlement date)", "| date | n | MAE | bias |", "|---|---|---|---|"]
    days = {}
    for r in R: days.setdefault(r["date"], []).append(r)
    for d, rs in sorted(days.items())[-14:]:
        L.append("| %s | %d | %.2f | %+.2f |" % (d, len(rs),
                 statistics.mean(x["abs_err"] for x in rs), statistics.mean(x["err"] for x in rs)))
    L.append("**Drift verdict:** " + ("INSUFFICIENT EVIDENCE -- need >=3 settlement days" if len(days) < 3 else "see trend above"))
    L += ["", "### Largest projection errors", "| player | cat | line | proj | actual | err | z |", "|---|---|---|---|---|---|---|"]
    for r in sorted(R, key=lambda x: -x["abs_err"])[:8]:
        L.append("| %s | %s | %s | %.1f | %.1f | %+.1f | %s |" % (str(r["player"])[:18], r["category"],
                 r["line"], r["mean"], r["actual"], r["err"], ("%+.1f" % r["z"]) if r["z"] is not None else "—"))
    txt = "\n".join(L)
    print(txt)
    if write:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        hdr = "" if os.path.exists(OUT) else "# PROJECTION REGISTRY\nAppend-only. Projection error, MAE, drift.\n"
        open(OUT, "a", encoding="utf-8").write(hdr + "\n---\n# Entry %s\n%s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M"), txt))
        print("\nappended -> %s" % OUT)
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--analyze", action="store_true"); ap.add_argument("--write", action="store_true")
    a_ = ap.parse_args()
    sys.exit(analyze(write=a_.write) if a_.analyze else ap.print_help())
