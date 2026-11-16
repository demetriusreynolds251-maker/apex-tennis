#!/usr/bin/env python3
"""
apex_daily_settlement_health.py

Built as Phase 6 of the APEX SETTLEMENT SYSTEM REMEDIATION (2026-10-06).

Produces a daily reconciliation report:
    Date | Props Evaluated | Props Posted | Props Settled | Props Stored |
    Missing Settlements | Duplicate Settlements | Settlement Errors |
    Header Status | System Health

Definitions used (documented so they can be adjusted later):
  - Evaluated = rows in apex_engine_picks_log_v1.csv (ts date) +
                rows in apex_engine_declines_log_v1.csv (ts date)
                i.e. every prop candidate the engine looked at that day,
                whether it got posted or declined.
  - Posted    = rows in apex_engine_picks_log_v1.csv with playable == '1'
                for that ts date (actually sent to the board/Discord).
  - Settled   = rows returned by apex_edge_tracker.settled_picks() whose
                match_date falls on that day.
  - Stored    = rows in apex_prop_sim_forward_log_v1.csv whose match_date
                falls on that day (the durable forward-tracking record
                used later for settlement/calibration).

Note: Posted and Settled will NOT match 1:1 on the same calendar day in
most cases, because a match posted today often doesn't finish (and get
settled) until a day or more later. This script does not treat that lag
as an error. It flags real problems instead:
  - "Missing Settlements": props posted 3+ days ago that still have no
    settlement on record anywhere (forward log actual, or hand-settled).
  - "Duplicate Settlements": more than one settled row for the exact same
    player/opponent/category/line/direction/prop_type/match_date.
  - "Header Status": whether apex_prop_sim_forward_log_v1.csv still has
    its header row (this was the root cause fixed on 2026-10-06).
  - "System Health" = FAIL if Header Status is broken, OR there are any
    duplicate settlements, OR missing-settlement count for old (3+ day)
    posted props is non-zero that day.
"""
import csv, sys, os, json
from collections import defaultdict, Counter
from datetime import datetime, timedelta

BASE = "/path/to/home/openclaw"
PICKS_LOG = os.path.join(BASE, "apex_engine_picks_log_v1.csv")
DECLINES_LOG = os.path.join(BASE, "apex_engine_declines_log_v1.csv")
FWD_LOG = os.path.join(BASE, "apex_prop_sim_forward_log_v1.csv")
EXPECTED_HEADER = ["ts","match_id","match_date","player","opponent","is_fav","pinn_fav",
                   "surface","best_of","category","mean","sd","p10","p50","p90","fragility",
                   "hold_fav","hold_dog","book","line","p_over","p_under","actual",
                   "settled_ts","sim_sha","v1_sha"]
OUT_JSON = os.path.join(BASE, "apex_daily_settlement_health_report.json")
OUT_LOG = os.path.join(BASE, "apex_daily_settlement_health.log")


def header_status():
    with open(FWD_LOG, newline="") as f:
        first_line = f.readline().strip().split(",")
    return "OK" if first_line == EXPECTED_HEADER else "BROKEN"


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def date_of(ts):
    if not ts:
        return None
    return ts[:10]


def main():
    sys.path.insert(0, BASE)
    import apex_edge_tracker as aet

    hstatus = header_status()

    picks = load_csv(PICKS_LOG)
    declines = load_csv(DECLINES_LOG)
    fwd = load_csv(FWD_LOG)
    settled = aet.settled_picks()

    evaluated_by_day = Counter()
    posted_by_day = Counter()
    for r in picks:
        d = date_of(r.get("ts"))
        if d:
            evaluated_by_day[d] += 1
            if r.get("playable") == "1":
                posted_by_day[d] += 1
    for r in declines:
        d = date_of(r.get("ts"))
        if d:
            evaluated_by_day[d] += 1

    settled_by_day = Counter()
    for p in settled:
        d = p.get("match_date")
        if d:
            settled_by_day[d] += 1

    stored_by_day = Counter()
    for r in fwd:
        d = r.get("match_date")
        if d:
            stored_by_day[d] += 1

    # duplicate settlements per day (same key, >1 occurrence)
    key = lambda p: (p["name"], p["opponent"], p["category"], p["line"], p["direction"],
                      p["prop_type"], p["match_date"])
    cnt = Counter(key(p) for p in settled)
    dupe_keys = {k for k, v in cnt.items() if v > 1}
    dupes_by_day = Counter()
    for p in settled:
        if key(p) in dupe_keys:
            dupes_by_day[p["match_date"]] += 1

    # missing settlements: posted props 3+ days old with no settlement anywhere
    # (checked by player+opponent+category+line existing in settled set, any date)
    settled_lookup = set()
    for p in settled:
        try:
            ln = round(float(p["line"]), 2)
        except (TypeError, ValueError):
            continue
        settled_lookup.add((p["name"], p["opponent"], p["category"], ln))
    today = datetime.utcnow().date()
    missing_by_day = Counter()
    voided_by_day = Counter()
    void_lookup = set()
    try:
        _hj = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "apex_settle_holds.json")))
        for _h in _hj.get("holds", []):
            if _h.get("reason") == "void":
                try:
                    void_lookup.add((_h["player"], _h["opponent"], _h["category"], round(float(_h["line"]), 2)))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass   # no sidecar yet -> everything counts as missing, same as before
    for r in picks:
        if r.get("playable") != "1":
            continue
        md = r.get("match_date")
        if not md:
            continue
        try:
            d = datetime.strptime(md, "%Y-%m-%d").date()
        except ValueError:
            continue
        if (today - d).days < 3:
            continue  # too recent to expect settlement yet
        try:
            ln = round(float(r["line"]), 2)
        except (TypeError, ValueError):
            continue
        k = (r["player"], r["opponent"], r["category"], ln)
        if k not in settled_lookup:
            # APEX-SETTLEHOLDS-2026-10-09: retired/cancelled props are voids, not missing data
            if k in void_lookup:
                voided_by_day[md] += 1
            else:
                missing_by_day[md] += 1

    all_days = sorted(set(evaluated_by_day) | set(posted_by_day) | set(settled_by_day)
                       | set(stored_by_day) | set(missing_by_day))

    report = []
    for d in all_days:
        ev = evaluated_by_day.get(d, 0)
        po = posted_by_day.get(d, 0)
        se = settled_by_day.get(d, 0)
        st = stored_by_day.get(d, 0)
        mi = missing_by_day.get(d, 0)
        du = dupes_by_day.get(d, 0)
        health = "FAIL" if (hstatus == "BROKEN" or mi > 0 or du > 0) else "OK"
        report.append({
            "date": d, "props_evaluated": ev, "props_posted": po,
            "props_settled": se, "props_stored": st,
            "missing_settlements": mi, "voided_props": voided_by_day.get(d, 0), "duplicate_settlements": du,
            "settlement_errors": "see Phase 2/3 audit log (not re-verified daily)",
            "header_status": hstatus, "system_health": health,
        })

    with open(OUT_JSON, "w") as f:
        json.dump(report, f, indent=2)

    with open(OUT_LOG, "a") as f:
        f.write(f"\n=== Daily settlement health run {datetime.utcnow().isoformat()}Z ===\n")
        f.write(f"Header status: {hstatus}\n")
        f.write(f"Days covered: {len(report)}\n")
        fails = [r for r in report if r["system_health"] == "FAIL"]
        f.write(f"Days FAILing: {len(fails)}\n")
        for r in fails:
            f.write(f"  {r['date']}: missing={r['missing_settlements']} dupes={r['duplicate_settlements']} header={r['header_status']}\n")

    print(f"Header status: {hstatus}")
    print(f"Days covered: {len(report)}")
    fails = [r for r in report if r["system_health"] == "FAIL"]
    print(f"Days FAILing: {len(fails)}")
    for r in fails[:20]:
        print(" ", r["date"], r)
    print(f"Full report written to: {OUT_JSON}")
    print(f"Run log appended to: {OUT_LOG}")


if __name__ == "__main__":
    main()
