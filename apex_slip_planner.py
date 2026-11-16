#!/usr/bin/env python3
"""
APEX-SLIPPLANNER-2026-10-11 (owner, approved 2026-10-11: "slip planner
for all boards"). For a board's legs, works out which PrizePicks slip gives the
most winning days while still making money, using Honest Probability (the
realistic per-leg win rate) and the real PrizePicks payouts he confirmed from
screenshots on 2026-10-11.

A "winning day" = the slip returns MORE than the entry (Flex "2 of 3 pays 1x"
only returns the entry, so it doesn't count). Legs are assumed independent;
boards already keep one prop per match.

Payouts (from his screenshots, 2026-10-11):
  Standard  3-pick Power 6x | Flex 3x / 1x
            4-pick Power 10x | Flex 6x / 1.5x
  Goblin    3-pick Power 2.2x | Flex 1.8x / 0.5x   (Goblin payouts vary with how
            6-pick Power 2.75x                       far below the line each pick is)
Mixed Standard + Goblin slips: PrizePicks prices each pick, so no payout table --
the planner shows the chance only.
"""
import json, os
from itertools import product

HERE = os.path.dirname(os.path.abspath(__file__))
PLAN_LOG = os.path.join(HERE, "data", "logs", "slip_planner_log.jsonl")
PAYOUTS = {
    "standard": {3: {"Power": {3: 6.0}, "Flex": {3: 3.0, 2: 1.0}},
                 4: {"Power": {4: 10.0}, "Flex": {4: 6.0, 3: 1.5}}},
    "goblin": {3: {"Power": {3: 2.2}, "Flex": {3: 1.8, 2: 0.5}},   # APEX-GOB22-2026-10-11: plain entry, no Bonus Lineup
               6: {"Power": {6: 2.75}}},
}
MIN_EV = 1.05   # a recommended slip must return at least +5% expected

# Real slip-builder payouts owner screenshotted 2026-10-11 (G=Goblin, S=Standard, D=Demon).
# PrizePicks prices every Goblin/Demon pick separately (the API only flags "adjusted odds"),
# so mixed slips can't be priced from a table -- the planner shows the break-even payout instead.
PAYOUT_EXAMPLES = [
    ("S S S", "Power 6x | Flex 3x / 1x"),
    ("G G G (Tien TG 19.5, Medvedev TG 17.5, Auger-Aliassime TG 19.5; plain entry)", "Power 2.2x | Flex 1.8x / 0.5x"),
    ("G G G (Kartal 17.5, Alcaraz 16.5, Lehecka FS 2.5; $25 Bonus Lineup -- lower payout)", "Power 1.8x | Flex 1.5x / 0.5x"),
    ("G S G (Kartal 17.5, Alcaraz 19.5, Tien 21.5)", "Power 3.25x | Flex 2.25x / 0.75x"),
    ("G S S (Kartal 17.5, Alcaraz 19.5, Tien 22.5)", "Power 3.75x | Flex 3x / 0.5x"),
    ("D S S (Kartal 24.5, Tien 22.5, Pohankova GW 7.5)", "Power 7x | Flex 5x / 1x"),
    ("D D D (Kartal 24.5, Alcaraz 22.5, Tien 26.5)", "Power 14x | Flex 9x / 1.5x"),
    ("S S S S", "Power 10x | Flex 6x / 1.5x"),
]
DEMON_NOTE = ("Demons are priced far below their real odds (3 Demons pay 14x when the true odds of all "
              "three hitting are about 100-to-1), so they lose money -- avoid them.")


def _dist(ps):
    d = [0.0] * (len(ps) + 1)
    for o in product((1, 0), repeat=len(ps)):
        q = 1.0
        for hit, p in zip(o, ps):
            q *= p if hit else (1 - p)
        d[sum(o)] += q
    return d


def options(probs, kind):
    """All slip options for legs (best first by probability) of one type."""
    out = []
    for n, modes in sorted((PAYOUTS.get(kind) or {}).items()):
        if len(probs) < n:
            continue
        d = _dist(probs[:n])
        for mode, mult in modes.items():
            ev = sum(d[k] * m for k, m in mult.items())
            win = sum(d[k] for k, m in mult.items() if m > 1.0)
            out.append({"n": n, "mode": mode, "mult": mult, "ev": ev, "win": win})
    return out


def plan(legs):
    """legs: list of dicts with prob_honest (0-1) and prop_type, in board order.
    Returns (lines, recommended option or None, kind)."""
    legs = [l for l in legs if l.get("prob_honest")]
    if not legs:
        return [], None, None
    types = {str(l.get("prop_type") or "").lower() for l in legs}
    kind = types.pop() if len(types) == 1 else "mixed"
    probs = [float(l["prob_honest"]) for l in legs]
    head = "\U0001F9FE **SLIP PLANNER** (Honest Probability x today's PrizePicks payouts)"
    # break-even line for ANY mix: worth playing only if the app's Power payout is at least this
    be_lines = []
    for n in (2, 3):
        if len(probs) >= n:
            pa = 1.0
            for x in probs[:n]:
                pa *= x
            be_lines.append("%d-pick Power: all hit %.0f%% of days -- worth it only if PrizePicks pays %.1fx or more"
                            % (n, 100 * pa, MIN_EV / pa))
    if kind not in PAYOUTS:
        if len(probs) < 2:
            return [], None, kind
        out = [head, "Mixed or Demon picks -- PrizePicks prices each one, so compare the app's payout to these:"] + ["   " + x for x in be_lines]
        if kind == "demon" or "demon" in {str(l.get("prop_type") or "").lower() for l in legs}:
            out.append("\u26a0\ufe0f " + DEMON_NOTE)
        return out, None, kind
    opts = options(probs, kind)
    if not opts:
        return [head, "Not enough legs on this board for a slip today."], None, kind
    good = [o for o in opts if o["ev"] >= MIN_EV]
    rec = max(good, key=lambda o: (o["win"], o["ev"])) if good else None
    def fmt(o):
        full = _dist(probs[:o["n"]])[o["n"]]
        txt = "%d-pick %s (%s) -- profit day %.0f%% (~%.1f/week)" % (
            o["n"], o["mode"], " / ".join("%gx" % m for k, m in sorted(o["mult"].items(), reverse=True)),
            100 * o["win"], 7 * o["win"])
        if o["mode"] == "Flex":
            txt += " incl. partial wins; full sweep %.0f%% (~%.1f/week)" % (100 * full, 7 * full)
        return txt + ", expected %s%.0f%%" % ("+" if o["ev"] >= 1 else "-", abs(100 * (o["ev"] - 1)))
    lines = [head]
    if kind == "goblin":
        lines.append("   (Goblin payouts shrink with how deep each line is -- check the app; break-even shown below.)")
    if rec:
        lines.append("✅ Best: " + fmt(rec))
        for o in sorted([o for o in opts if o is not rec], key=lambda o: -o["ev"])[:2]:
            lines.append("   Also: " + fmt(o))
    else:
        best = max(opts, key=lambda o: o["ev"])
        lines.append("⚠️ No profitable slip from this board today -- best is " + fmt(best))
        if kind == "goblin":
            lines.append("   (Goblin payouts are too low for these probabilities -- see Best Standard.)")
    lines += ["   " + x for x in be_lines]
    return lines, rec, kind


def log_plan(captured_at, match_date, source, board, rec, legs):
    if not rec:
        return
    try:
        os.makedirs(os.path.dirname(PLAN_LOG), exist_ok=True)
        with open(PLAN_LOG, "a") as fh:
            fh.write(json.dumps({"captured_at": captured_at, "match_date": match_date, "source": source, "board": board,
                                 "n": rec["n"], "mode": rec["mode"], "mult": {str(k): v for k, v in rec["mult"].items()},
                                 "ev": round(rec["ev"], 4), "win": round(rec["win"], 4),
                                 "legs": [{k: l.get(k) for k in ("player", "opponent", "category", "line", "direction", "match_date")}
                                          for l in legs[:rec["n"]]]}) + "\n")
    except Exception as e:
        print("[SlipPlanner] log failed: %s" % e)
