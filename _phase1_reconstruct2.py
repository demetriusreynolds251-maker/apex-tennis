import sys, os, json, csv
sys.path.insert(0, '/path/to/home/openclaw')
import apex_edge_tracker as aet
import apex_learning_engine as ale
import openclaw_core as core

HERE = '/path/to/home/openclaw'
ARCHIVE_DIR = os.path.join(HERE, 'data', 'picks_archive')
STAT_MAP = core._APEX_STAT_MAP

def load_archive(d):
    with open(os.path.join(ARCHIVE_DIR, f"picks_{d}.json")) as f:
        return json.load(f)

def adapt(p):
    out = dict(p)
    slug = str(p.get("stat_type","")).strip().lower().replace(" ","_")
    out["category"] = STAT_MAP.get(slug, "")
    out["prob"] = float(p.get("confidence") or 0)
    out["bo5"] = (str(p.get("apex_best_of") or "") == "5")
    out["prop_type"] = p.get("prop_type_classified", "standard")
    return out

days_to_reconstruct = ["2026-09-29", "2026-09-30", "2026-10-01"]

unmapped_total = 0
for d in days_to_reconstruct:
    raw = load_archive(d)
    mapped = [adapt(p) for p in raw]
    unmapped = [p for p in mapped if not p["category"]]
    unmapped_total += len(unmapped)
    if unmapped:
        print(f"{d}: {len(unmapped)} picks had no category mapping, stat_types=",
              sorted(set(str(p.get('stat_type')) for p in unmapped)))
    aet.capture_board_membership(mapped, {"match_date": d})
    print(f"{d}: replayed {len(mapped)} picks into capture_board_membership")

print("unmapped_total =", unmapped_total)
print("membership log exists now:", os.path.exists(aet.BOARD_MEMBERSHIP_LOG))
if os.path.exists(aet.BOARD_MEMBERSHIP_LOG):
    with open(aet.BOARD_MEMBERSHIP_LOG) as f:
        rows = list(csv.DictReader(f))
    print("rows written:", len(rows))
    from collections import Counter
    print("by date:", Counter(r["date"] for r in rows))
