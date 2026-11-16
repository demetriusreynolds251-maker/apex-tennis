#!/usr/bin/env python3
"""
APEX-PROJ-JOIN-2026-09-26
Read-only derived artifact: attaches each hand-settled prop to the exact
projection (mean/sd/p10/p50/p90) that generated it, using the join design
approved 2026-09-26 (Task 8):

  player-level rows: (match_date, player, category, line)
  match-level rows:  (match_date, opponent, category, line)   -- player=="match"
                                                                   is not a real
                                                                   identifier

Hand-CSV dedupe rule: when a key has more than one row, keep the row where
in_forward_log == "1" (the authoritative row; "0" rows are superseded).

NEVER writes to either source file:
  - apex_engine_hand_settled_v1.csv   (read only)
  - apex_prop_sim_forward_log_v1.csv  (read only)

Writes exactly one new file: apex_projection_join_v1.csv
Regenerable at any time by re-running this script -- it is a derived view,
not a system of record.
"""
import csv
import sys

HAND_LOG = "apex_engine_hand_settled_v1.csv"
SIM_LOG = "apex_prop_sim_forward_log_v1.csv"
OUT_FILE = "apex_projection_join_v1.csv"

MARKER = "APEX-PROJ-JOIN-2026-09-26"


def split_key(r):
    """Returns (match_date, kind, identity, category, line).
    kind is 'MATCH' for player=='match' rows (identity = opponent, which
    holds the pair string like 'A v B'), else 'PLAYER' (identity = player)."""
    is_match = (r.get("player") or "").strip().lower() == "match"
    ident = (r.get("opponent") or "").strip().lower() if is_match \
        else (r.get("player") or "").strip().lower()
    return (
        (r.get("match_date") or "").strip(),
        "MATCH" if is_match else "PLAYER",
        ident,
        (r.get("category") or "").strip(),
        str(r.get("line") or "").strip(),
    )


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_sim_lookup(sim_rows):
    """One sim row per key. Design-validated: 0 collisions under this key
    scheme as of 2026-09-26 (612 sim keys, 0 ambiguous after the match/player
    split). If a future data change reintroduces a collision, refuse loudly
    rather than guess -- silent ambiguity is exactly the risk this design
    was built to eliminate."""
    lookup = {}
    collisions = set()
    for r in sim_rows:
        k = split_key(r)
        if k in lookup:
            collisions.add(k)
        lookup[k] = r
    return lookup, collisions


def dedupe_hand_rows(hand_rows):
    """Per approved rule: when a key has multiple hand rows, keep the one
    with in_forward_log == '1' (the authoritative row). If a key has
    multiple rows and NONE carry in_forward_log == '1', keep all of them
    and flag -- this is an unanticipated case, not silently resolved."""
    by_key = {}
    unresolved = []
    for r in hand_rows:
        k = split_key(r)
        if k not in by_key:
            by_key[k] = [r]
        else:
            by_key[k].append(r)

    kept = {}
    for k, rows in by_key.items():
        if len(rows) == 1:
            kept[k] = rows[0]
            continue
        authoritative = [r for r in rows if (r.get("in_forward_log") or "").strip() == "1"]
        if len(authoritative) == 1:
            kept[k] = authoritative[0]
        elif len(authoritative) > 1:
            # more than one row claims to be authoritative -- refuse to guess
            unresolved.append((k, rows))
            kept[k] = authoritative[0]  # keep first as a fallback, but flag it
        else:
            # no row claims to be authoritative -- keep the last one seen,
            # flag it, do not silently pick a winner
            unresolved.append((k, rows))
            kept[k] = rows[-1]
    return kept, unresolved


def main():
    try:
        hand_rows = list(csv.DictReader(open(HAND_LOG)))
    except FileNotFoundError:
        print(f"REFUSED: {HAND_LOG} not found -- nothing to join", file=sys.stderr)
        sys.exit(1)
    try:
        sim_rows = list(csv.DictReader(open(SIM_LOG)))
    except FileNotFoundError:
        print(f"REFUSED: {SIM_LOG} not found -- nothing to join against", file=sys.stderr)
        sys.exit(1)

    sim_lookup, sim_collisions = build_sim_lookup(sim_rows)
    if sim_collisions:
        print(f"WARNING: {len(sim_collisions)} sim-log key collision(s) detected "
              f"-- these rows will NOT be joined (refusing to guess):", file=sys.stderr)
        for k in list(sim_collisions)[:10]:
            print(f"  {k}", file=sys.stderr)

    hand_deduped, unresolved = dedupe_hand_rows(hand_rows)
    if unresolved:
        print(f"WARNING: {len(unresolved)} hand-CSV key(s) had ambiguous "
              f"duplicates the dedupe rule couldn't cleanly resolve:", file=sys.stderr)
        for k, rows in unresolved[:10]:
            print(f"  {k} -> {len(rows)} rows, in_forward_log values: "
                  f"{[r.get('in_forward_log') for r in rows]}", file=sys.stderr)

    out_rows = []
    matched = 0
    unmatched = 0
    for k, r in hand_deduped.items():
        sim_r = None if k in sim_collisions else sim_lookup.get(k)
        actual = _f(r.get("actual"))
        mean = _f(sim_r.get("mean")) if sim_r else None

        if sim_r is not None:
            matched += 1
            status = "joined"
        else:
            unmatched += 1
            status = "no projection exists"

        proj_error = None
        if mean is not None and actual is not None:
            proj_error = round(actual - mean, 4)

        out_rows.append({
            "match_date": r.get("match_date", ""),
            "player": r.get("player", ""),
            "opponent": r.get("opponent", ""),
            "category": r.get("category", ""),
            "line": r.get("line", ""),
            "direction": r.get("direction", ""),
            "prob": r.get("prob", ""),
            "actual": r.get("actual", ""),
            "best_of": r.get("best_of", ""),
            "note": r.get("note", ""),
            "join_status": status,
            "projection_mean": sim_r.get("mean") if sim_r else "",
            "projection_sd": sim_r.get("sd") if sim_r else "",
            "projection_p10": sim_r.get("p10") if sim_r else "",
            "projection_p50": sim_r.get("p50") if sim_r else "",
            "projection_p90": sim_r.get("p90") if sim_r else "",
            "projection_error": proj_error if proj_error is not None else "",
            "sim_match_id": sim_r.get("match_id") if sim_r else "",
            "sim_sha": sim_r.get("sim_sha") if sim_r else "",
        })

    fieldnames = list(out_rows[0].keys()) if out_rows else []
    with open(OUT_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in out_rows:
            w.writerow(row)

    total = len(out_rows)
    print(f"{MARKER} -- wrote {OUT_FILE}")
    print(f"total hand rows (post-dedupe): {total}")
    print(f"joined (projection attached): {matched} ({100*matched/total:.1f}%)" if total else "no rows")
    print(f"no projection exists: {unmatched} ({100*unmatched/total:.1f}%)" if total else "")
    if sim_collisions:
        print(f"sim-log collisions excluded from joining: {len(sim_collisions)}")
    if unresolved:
        print(f"hand-CSV ambiguous dedupes (used fallback, flagged above): {len(unresolved)}")


if __name__ == "__main__":
    main()
