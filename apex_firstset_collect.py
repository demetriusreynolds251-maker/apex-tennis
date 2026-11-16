#!/usr/bin/env python3
"""APEX-FSG-COLLECT-2026-09-04 — first-set research collection.
Append-only CSV. Fail-open: never raises into the caller.
Dedupe key: (match_date, player, slug, line). Settlement by hand later."""
import csv, os, sys, datetime as _dt

BASE = os.environ.get("APEX_HOME", os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(BASE, "data", "apex_firstset_research_v1.csv")
FIELDS = ["ts","match_date","player","opponent","slug","line","prop_type",
          "tournament","best_of","wta","surface","actual","note"]
FS_SLUGS = {"1st_set_total_games","1st_set_total_games_won","1st_set_games_won",
            "1st_set_aces","1st_set_double_faults","1st_set_breaks","first_set_winner"}

def is_first_set(slug):
    return str(slug or "").lower().replace(" ","_") in FS_SLUGS

def _key(r):
    return (r.get("match_date",""), str(r.get("player","")).strip().lower(),
            str(r.get("slug","")).lower(), str(r.get("line","")))

def record(prop, match_date=None, tournament="", best_of="", wta="", surface="", note=""):
    """prop = engine prop dict. Returns 'added' | 'dup' | 'skip' | 'error:<msg>'."""
    try:
        slug = prop.get("stat_type") or prop.get("slug") or ""
        if not is_first_set(slug):
            return "skip"
        row = {"ts": _dt.datetime.utcnow().isoformat(timespec="seconds"),
               "match_date": match_date or _dt.date.today().isoformat(),
               "player": prop.get("player",""), "opponent": prop.get("opponent",""),
               "slug": slug.lower(), "line": prop.get("line",""),
               "prop_type": prop.get("prop_type") or prop.get("propclass") or "",
               "tournament": tournament, "best_of": best_of, "wta": wta,
               "surface": surface, "actual": "", "note": note}
        os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
        new = not os.path.exists(CSV_PATH)
        if not new:
            with open(CSV_PATH, newline="") as f:
                if any(_key(x) == _key(row) for x in csv.DictReader(f)):
                    return "dup"
        with open(CSV_PATH, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new: w.writeheader()
            w.writerow(row)
        return "added"
    except Exception as e:
        return "error:%s" % e

def stats():
    if not os.path.exists(CSV_PATH):
        print("no rows yet"); return
    rows = list(csv.DictReader(open(CSV_PATH, newline="")))
    settled = [r for r in rows if r["actual"].strip()]
    print("rows %d · settled %d / 200 gate" % (len(rows), len(settled)))
    by = {}
    for r in rows: by[r["slug"]] = by.get(r["slug"],0)+1
    for k,v in sorted(by.items(), key=lambda x:-x[1]): print("  %-28s %d" % (k,v))

def selftest():
    global CSV_PATH
    CSV_PATH = "/tmp/apex_fsg_selftest.csv"
    if os.path.exists(CSV_PATH): os.remove(CSV_PATH)
    p = {"player":"A Test","opponent":"B Test","stat_type":"1st_set_total_games","line":9.5}
    assert record(p, "2026-09-04") == "added"
    assert record(p, "2026-09-04") == "dup"
    assert record({"player":"A","stat_type":"aces","line":3.5}) == "skip"
    assert record(None) .startswith("error")
    print("selftest 4/4 PASS")

if __name__ == "__main__":
    {"--stats": stats, "--selftest": selftest}.get(sys.argv[1] if len(sys.argv)>1 else "", stats)()
