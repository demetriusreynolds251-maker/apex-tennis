#!/usr/bin/env python3
"""APEX-CARDIDX-001 -- reasoning-card index keyed by (player, category, date).
Replaces glob matching in L5-B. Build once, look up instantly.
  --build   scan cards, write index
  --stats   coverage report
"""
import os, sys, json, glob, argparse
import apex_reasoning_audit as RA
IDX = "apex_learning_system/CARD_INDEX.json"

def norm(s):
    return " ".join(str(s or "").lower().replace(".", "").replace("'", "").split())

def build(verbose=True):
    files = sorted(glob.glob(os.path.join(RA.CARDS, "reasoning_*.txt")))
    idx, skipped = {}, 0
    for f in files:
        if "_moneyline_" in os.path.basename(f): skipped += 1; continue
        try: c = RA.parse_card(f)
        except Exception: skipped += 1; continue
        cat = RA.cat_key(c)
        players = norm(c.get("players")).replace(" vs ", "|").split("|")
        if not cat or not c.get("projection"): skipped += 1; continue
        for pl in players:
            pl = pl.strip()
            if not pl: continue
            for key in ((pl, cat, c.get("date") or ""), (pl.split()[-1], cat, c.get("date") or "")):
                idx.setdefault("%s::%s::%s" % key, os.path.basename(f))
    os.makedirs(os.path.dirname(IDX), exist_ok=True)
    json.dump(idx, open(IDX, "w"), indent=1)
    if verbose:
        print("indexed %d keys from %d cards (%d skipped)" % (len(idx), len(files) - skipped, skipped))
    return idx

# APEX-CARDIDX-ALIAS-2026-08-31: engine slugs vs card category names.
# cat_key() returns EXPECTED names before reaching its translation dict, so
# a "double_faults" card is never indexed under the engine slug "dfs".
# Aliases are applied here, at lookup, so cat_key() and the index are
# untouched. Reciprocal both ways; harmless when a name already agrees.
_CAT_ALIASES = {
    "dfs":              ["double_faults"],
    "double_faults":    ["dfs"],
    "breaks":           ["break_points_won"],
    "break_points_won": ["breaks"],
    "games_won":        ["total_games_won"],
    "total_games_won":  ["games_won"],
}


def _cat_candidates(category):
    """Requested category first, then any known alias. Order is stable."""
    c = str(category or "")
    out = [c]
    for a in _CAT_ALIASES.get(c, []):
        if a not in out:
            out.append(a)
    return out


def lookup_match(opponent, category, date=""):
    """APEX-MATCHCARD-2026-08-31: match-level props carry player="match" and
    the real pair in `opponent` ("A v B" / "A vs B"). The index is keyed per
    player, so resolve by either side -- a match card is ONE file indexed
    under both names, so either side returns the same card. Returns the same
    (card, path) shape as lookup(); (None, None) when nothing resolves."""
    s = str(opponent or "")
    for sep in (" vs ", " v. ", " v "):
        if sep in s:
            sides = s.split(sep, 1)
            break
    else:
        sides = [s]
    for side in sides:
        side = side.strip()
        if not side:
            continue
        card, path = lookup(side, category, date)
        if card:
            return card, path
    return None, None


def lookup(player, category, date=""):
    idx = json.load(open(IDX)) if os.path.exists(IDX) else build(verbose=False)
    p = norm(player)
    cats = _cat_candidates(category)
    for cat in cats:
        for key in ("%s::%s::%s" % (p, cat, date),
                    "%s::%s::%s" % (p.split()[-1] if p else "", cat, date),
                    "%s::%s::" % (p, cat)):
            if key in idx:
                path = os.path.join(RA.CARDS, idx[key])
                return RA.parse_card(path), path
    # date-agnostic fallback, still category-exact (aliases included)
    for k, v in idx.items():
        kp, kc, _ = k.split("::")
        if kc in cats and (kp == p or (p and kp == p.split()[-1])):
            return RA.parse_card(os.path.join(RA.CARDS, v)), os.path.join(RA.CARDS, v)
    return None, None

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true"); ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.build or a.stats:
        idx = build()
        if a.stats:
            import apex_projection_analysis as PA, apex_root_cause as RC
            losses = [x for x in RC.classify(PA.rows()) if x["cause"] == "MODEL_ERROR"]
            hit = sum(1 for x in losses if lookup(str(x["player"]), x["category"], str(x["date"]))[0])
            print("MODEL_ERROR losses: %d | cards found via index: %d" % (len(losses), hit))
    else: ap.print_help()
