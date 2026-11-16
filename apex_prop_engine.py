#!/usr/bin/env python3
"""
APEX-PROP-ENGINE-2026-08-23
Project Apex Intelligence / OpenClaw (DEMETRIUS)

Thin adapter between the frozen prop models and any consumer (Discord bot,
Mission Control, CLI). It owns exactly three things:

  1. loading the frozen models ONCE and caching them,
  2. the tier table (read from apex_prop_tiers.json, SHA'd),
  3. rendering the shadow card.

It contains NO modelling. Every number comes from apex_prop_sim_v1 / apex_prop_model_v1.

FAIL-OPEN CONTRACT (identical to the fragility layer):
  evaluate_match() and render_shadow_block() NEVER raise. On any error they return
  None (or an error string in the result dict) and the caller posts exactly as it
  would have without the engine. A missing model, a bad price, an unknown player,
  or a tampered file can never block or alter a legacy pick.

USAGE from the bot:
    import apex_prop_engine as ape
    block = ape.render_shadow_block("Jannik Sinner", "Alex Michelsen", 0.91,
                                    surface="Hard", bo5=True, lines={...})
    if block:
        msg += "\\n" + block

CLI:
    python3 apex_prop_engine.py --selftest
    python3 apex_prop_engine.py --status
    python3 apex_prop_engine.py --card "Fav" "Dog" --pinn 0.64 --surface Hard [--bo5] [--wta]
    python3 apex_prop_engine.py --tiers
"""
import argparse, hashlib, json, math, os, sys, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MARKER = "APEX-PROP-ENGINE-2026-08-23"
TIERS_FILE = os.path.join(HERE, "apex_prop_tiers.json")
DEFAULT_SIMS = 20000

TIER_BADGE = {"elite": "🟢 ELITE", "good": "🟡 GOOD", "caution": "🟠 CAUTION", "low": "🔴 LOW CONFIDENCE"}
TIER_ORDER = {"elite": 0, "good": 1, "caution": 2, "low": 3}
LABEL = {
    "aces": "Aces", "dfs": "Double Faults", "holds": "Holds", "breaks": "Breaks",
    "svgms": "Service Games", "games_won": "Games Won", "games_lost": "Games Lost",
    "sets_won": "Sets Won", "fantasy_score": "Fantasy Score", "total_games": "Total Games",
    "first_set_games": "First Set Games", "first_set_fav_wins": "First Set Winner",
    "total_aces": "Total Aces", "most_aces_fav": "Most Aces", "most_games_fav": "Most Games",
    "total_sets": "Total Sets", "tiebreaks": "Tiebreaks", "goes_3_sets": "Goes 3 Sets",
    "goes_5_sets": "Goes 5 Sets", "any_tiebreak": "Any Tiebreak", "fav_wins": "Match Winner",
}
BINARY = ("goes_3_sets", "goes_5_sets", "any_tiebreak", "first_set_fav_wins", "most_aces_fav", "most_games_fav", "fav_wins")

_cache = {}

# ------------------------------------------------------------------ loading
def _load():
    """Load frozen models once. Returns dict or raises (callers catch)."""
    if _cache.get("ok"):
        # APEX-FRESHSTATE-2026-10-20: the player file is rebuilt at 6:30 AM and topped up
        # hourly (apex_state_topup.py); long-running processes pick up the new file
        # instead of keeping the one they started with.
        try:
            p = os.path.join(HERE, _cache["v1"].STATE_OUT)
            mt = os.path.getmtime(p)
            if mt != _cache.get("state_mtime"):
                import pickle
                with open(p, "rb") as f:
                    _cache["state"] = pickle.load(f)
                _cache["state_mtime"] = mt
        except Exception:
            pass
        return _cache
    import apex_prop_model_v1 as v1
    import apex_prop_sim_v1 as sim
    import pickle
    m = v1.load_model()                     # refuses unfrozen or tampered
    c = sim.load_sim()                      # refuses unfrozen or tampered
    if m["_sha"] != c["v1_sha"]:
        raise RuntimeError("V1 model sha changed since the simulator was frozen")
    state_mtime = os.path.getmtime(os.path.join(HERE, v1.STATE_OUT))
    with open(os.path.join(HERE, v1.STATE_OUT), "rb") as f:
        state = pickle.load(f)
    with open(TIERS_FILE) as f:
        tiers = json.load(f)
    _cache.update({"ok": True, "v1": v1, "sim": sim, "m": m, "c": c, "state": state, "state_mtime": state_mtime, "tiers": tiers,
                   "tiers_sha": hashlib.sha256(open(TIERS_FILE, "rb").read()).hexdigest()})
    return _cache

def tier_of(category):
    try:
        t = _load()["tiers"]["tiers"].get(category)
        return (t["tier"], t.get("note", "")) if t else ("low", "not in the tier table")
    except Exception:
        return ("low", "tier table unavailable")

def status():
    """Human-readable readiness check. Never raises."""
    out = [MARKER]
    try:
        z = _load()
        out.append("frozen sim   %s" % z["c"]["_sha"][:16])
        out.append("frozen V1    %s" % z["m"]["_sha"][:16])
        out.append("tier table   %s (v%d, %d categories)" % (z["tiers_sha"][:16], z["tiers"]["version"], len(z["tiers"]["tiers"])))
        out.append("player state %s (%d players)" % (z["state"]["latest_date"], len(z["state"]["players"])))
        out.append("READY")
    except Exception as e:
        out.append("NOT READY: %s" % e)
    return "\n".join(out)

# ------------------------------------------------------------------ evaluation
INDOOR_ACES_FACTOR_ATP = 1.088   # APEX-INDOOR-2026-10-11: learned on 2022-24 ATP indoor vs outdoor hard, same players


def evaluate_match(fav, dog, pinn, surface="Hard", bo5=False, wta=False, date=None, sims=DEFAULT_SIMS, lines=None, indoor=None):
    """Evaluate one match. NEVER raises.

    lines: optional dict, either {"<player>|<category>": line} or {"match|<category>": line}
    Returns dict with keys: ok, error, match, props[], or {"ok": False, "error": ...}.
    """
    try:
        z = _load()
        v1, sim = z["v1"], z["sim"]
        import pandas as pd, numpy as np
        from datetime import date as _date
        today = _date.fromisoformat(date) if date else _date.today()
        pinn = float(pinn)
        if not (0.5 <= pinn < 1.0):
            return {"ok": False, "error": "pinn must be the favorite's de-vigged probability in [0.5, 1)"}
        favn = v1.resolve_name(fav, z["state"]); dogn = v1.resolve_name(dog, z["state"])
        surface = str(surface).title(); bo = 5 if bo5 else 3
        df = sim.build_live_row(favn, dogn, pinn, surface, bo5, wta, today, z["m"], z["state"])
        P, hf, hd, mu = sim.v1_inputs_from_rows(df, z["m"])
        # APEX-OPPACES-2026-10-11 + APEX-INDOOR-2026-10-11 (owner, approved
        # "go" 2026-10-11). ATP only, hard courts only -- the scope both passed in:
        #  - opponent aces-allowed: a server's aces scale with how many aces the
        #    returner allows (settled ATP Aces Oct 1-10: Brier 0.2116 -> 0.1988)
        #  - indoor: same player hits ~8.8% more aces indoors (factor learned on
        #    2022-24, aces error -2.8% on 2025-26 ATP indoor matches)
        # Aces only; DFs, holds and breaks are untouched.
        aces_adj = {"fav": 1.0, "dog": 1.0}
        if not wta and surface == "Hard":
            try:
                import apex_opp_aces as _oa
                aces_adj["fav"] *= _oa.factor(dogn)
                aces_adj["dog"] *= _oa.factor(favn)
            except Exception:
                pass
            if indoor is True:
                aces_adj["fav"] *= INDOOR_ACES_FACTOR_ATP
                aces_adj["dog"] *= INDOOR_ACES_FACTOR_ATP
        if aces_adj["fav"] != 1.0 or aces_adj["dog"] != 1.0:
            mu = {side: (mu[side][0] * aces_adj[side], mu[side][1]) for side in mu}
        lv = sim.level_from(hf[0], hd[0], z["c"], sim.price_level_pred(df, z["c"])[0])
        t, flag = sim.solve_tilt(lv, pinn, z["c"]["tb_k"], bo, sigma=z["c"].get("gap_sigma", 0.0))
        hA = min(max(lv + t, 0.2), 0.995); hB = min(max(lv - t, 0.2), 0.995)
        # CANDIDATE-C-2026-09-26: tour-specific aces dispersion, aces-only scope.
        # Frozen, evidence-fit constants (method-of-moments, fit_df year<=2021;
        # reproduced identically in apex_v2_dispersion_calib_tune.py,
        # apex_v2_dispersion_calib_holdout.py, apex_v2_dispersion_promotion_analysis.py).
        _R_ATP = {"fav": 8.70148205269834, "dog": 5.885645957929439}; _DM_ATP = 0.6
        _R_WTA = {"fav": 3.9866756061964232, "dog": 2.7971092624925418}; _DM_WTA = 0.4
        _r, _dm = (_R_WTA, _DM_WTA) if wta else (_R_ATP, _DM_ATP)
        # CANDIDATE-C-2026-09-27: tour-specific DFs dispersion, holdout-confirmed
        # (apex_v2_dfs_dispersion_partA.py / _partB_holdout.py /
        # apex_v2_dfs_promotion_analysis.py). Same method-of-moments r fit
        # convention as aces, fit_df year<=2021.
        _RD_ATP = {"fav": 8.493509531006397, "dog": 9.163976729570685}; _DMD_ATP = 0.75
        _RD_WTA = {"fav": 8.256973781128396, "dog": 9.500414039354514}; _DMD_WTA = 0.3
        _rd, _dmd = (_RD_WTA, _DMD_WTA) if wta else (_RD_ATP, _DMD_ATP)
        s = sim.simulate(hA, hB, z["c"]["tb_k"], bo, (mu["fav"][0][0], mu["fav"][1][0]), (mu["dog"][0][0], mu["dog"][1][0]),
                         (_r["fav"] * _dm, _rd["fav"] * _dmd),
                         (_r["dog"] * _dm, _rd["dog"] * _dmd),
                         sims, sigma=z["c"].get("gap_sigma", 0.0))
        frag = float(df["fragility"][0])
        match_id = sim.sha256_text("|".join([today.isoformat(), v1.norm_name(favn), v1.norm_name(dogn)]))[:12]
        lines = lines or {}
        props = []
        rates = {}
        W, K = z["m"]["window"], z["m"]["shrink"]
        for side in ("fav", "dog"):
            tag = "%s_W%d_K%d" % (side, W, K)
            rates[side] = {k: float(df[tag + "_" + k][0]) for k in ("ace_rate", "df_rate", "hold_rate", "break_rate", "spg")}
        for side, nm, opp in (("fav", favn, dogn), ("dog", dogn, favn)):
            for cat in sim.PLAYER_PROPS:
                props.append(_prop(s[side][cat], cat, nm, opp, side == "fav", lines, s, side, rates, hA, hB, pinn, bo))
        for cat in sim.MATCH_PROPS:
            props.append(_prop(s[cat], cat, "match", favn + " v " + dogn, None, lines, s, None, rates, hA, hB, pinn, bo))
        props.sort(key=lambda p: (TIER_ORDER.get(p["tier"], 9), -abs(p["edge"]) if p["edge"] is not None else 0))
        return {"ok": True, "error": None, "match_id": match_id, "date": today.isoformat(),
                "fav": favn, "dog": dogn, "surface": surface, "best_of": bo, "pinn_fav": pinn,
                "aces_adj": aces_adj, "indoor": indoor,
                "fragility": frag, "fragility_text": _frag_text(frag, pinn),
                "hold_fav": hA, "hold_dog": hB, "solver": flag,
                "p_fav_wins": float(s["fav_wins"].mean()), "props": props, "rates": rates,
                "sim_sha": z["c"]["_sha"], "v1_sha": z["m"]["_sha"], "tiers_sha": z["tiers_sha"], "sims": sims}
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-800:]}

def _prop(x, cat, player, opponent, is_fav, lines, s, side, rates, hA, hB, pinn, bo):
    import numpy as np
    tier, note = tier_of(cat)
    key = ("match|" if player == "match" else "%s|" % player) + cat
    line = lines.get(key, lines.get(key.lower()))
    binary = cat in BINARY
    d = {"category": cat, "label": LABEL.get(cat, cat), "player": player, "opponent": opponent,
         "is_fav": is_fav, "tier": tier, "tier_badge": TIER_BADGE[tier], "tier_note": note,
         "binary": binary, "mean": float(np.mean(x)), "sd": float(np.std(x)),
         "p10": float(np.percentile(x, 10)), "p50": float(np.percentile(x, 50)), "p90": float(np.percentile(x, 90)),
         "line": None, "p_over": None, "p_under": None, "edge": None, "confidence": None,
         "reasons": _reasons(cat, side, rates, s, hA, hB, bo)}
    if binary:
        d["probability"] = float(np.mean(x))
    if line is not None:
        line = float(line)
        d["line"] = line
        d["p_over"] = float(np.mean(x > line)); d["p_under"] = float(np.mean(x < line))
        d["edge"] = d["mean"] - line
    d["confidence"] = _confidence(tier, d)
    return d

def _confidence(tier, d):
    """Tier caps confidence. A caution category can never print High."""
    if tier == "low": return "Informational"
    if d["edge"] is None: return {"elite": "Medium", "good": "Medium", "caution": "Low"}[tier]
    spread = max(d["sd"], 1e-6)
    z = abs(d["edge"]) / spread
    base = "High" if z >= 0.35 else ("Medium" if z >= 0.15 else "Low")
    if tier == "good" and base == "High": base = "Medium"
    if tier == "caution": base = "Low"
    return base

def _reasons(cat, side, rates, s, hA, hB, bo):
    """Up to three lines, each a fact the engine computed. Never narrative."""
    import numpy as np
    if side is None:
        if cat == "total_games":
            return ["solved hold rates %.3f / %.3f" % (hA, hB),
                    "best of %d" % bo,
                    "the market prices this better than the model does — lean only"]
        if cat in ("goes_3_sets", "goes_5_sets", "any_tiebreak"):
            return ["hold gap %.3f drives set competitiveness" % abs(hA - hB),
                    "P(fav wins a set) implied by the solved holds"]
        if cat == "total_aces":
            return ["sum of both players' simulated aces", "bands run narrow — treat probability as slightly overconfident"]
        return ["derived from the same simulated matches as every other prop"]
    r = rates[side]; opp = rates["dog" if side == "fav" else "fav"]
    svg = float(np.mean(s[side]["svgms"]))
    if cat == "aces":
        return ["surface ace rate %.3f per service point" % r["ace_rate"],
                "%.1f expected service games x %.2f points per game" % (svg, r["spg"]),
                "bands run narrow — probability is slightly overconfident"]
    if cat == "dfs":
        return ["surface double-fault rate %.3f" % r["df_rate"],
                "%.1f expected service games" % svg,
                "noisiest category in the research — informational weight"]
    if cat == "holds":
        return ["solved hold probability %.3f" % (hA if side == "fav" else hB),
                "opponent break rate %.3f" % opp["break_rate"],
                "%.1f expected service games" % svg]
    if cat == "breaks":
        return ["surface break rate %.3f" % r["break_rate"],
                "opponent holds %.0f%% on this surface" % (100 * opp["hold_rate"]),
                "%.1f expected opponent service games" % float(np.mean(s["dog" if side == "fav" else "fav"]["svgms"]))]
    if cat in ("games_won", "games_lost"):
        return ["holds %.1f + breaks %.1f" % (float(np.mean(s[side]["holds"])), float(np.mean(s[side]["breaks"]))),
                "solved hold rates %.3f / %.3f" % (hA, hB)]
    if cat == "fantasy_score":
        gw = float(np.mean(s[side]["games_won"])); gl = float(np.mean(s[side]["games_lost"]))
        sw = float(np.mean(s[side]["sets_won"])); sl = float(np.mean(s["dog" if side == "fav" else "fav"]["sets_won"]))
        ac = float(np.mean(s[side]["aces"])); dfq = float(np.mean(s[side]["dfs"]))
        return ["games %.1f won vs %.1f lost drives %+.1f" % (gw, gl, gw - gl),
                "sets %.1f vs %.1f drives %+.1f" % (sw, sl, 3 * (sw - sl)),
                "ace/DF term only %+.1f — the noisy part is diluted" % (0.5 * (ac - dfq))]
    if cat == "sets_won":
        return ["solved hold rates %.3f / %.3f" % (hA, hB), "best of %d" % bo]
    if cat == "svgms":
        return ["expected service games from the market's match length"]
    return ["from the same simulated matches as every other prop"]

def _frag_text(f, pinn):
    if pinn > 0.72:
        return "%.0f — informational only above a 72%% favorite" % f
    if f >= 89: return "%.0f — extreme; favorite far stronger on paper than the price implies" % f
    if f >= 71: return "%.0f — elevated upset risk for this price band" % f
    if f >= 41: return "%.0f — moderate" % f
    return "%.0f — low; the favorite is priced about right" % f

# ------------------------------------------------------------------ rendering
def render_shadow_block(fav, dog, pinn, surface="Hard", bo5=False, wta=False, date=None,
                        lines=None, sims=DEFAULT_SIMS, max_props=6, result=None):
    """Discord-ready shadow block. NEVER raises; returns None on any failure."""
    try:
        r = result or evaluate_match(fav, dog, pinn, surface, bo5, wta, date, sims, lines)
        if not r or not r.get("ok"):
            return None
        L = ["─── APEX PROP ENGINE — SHADOW (not used for the pick) ───",
             "%s vs %s · %s · Bo%d · Pinn %.2f · Fragility %.0f" % (r["fav"], r["dog"], r["surface"], r["best_of"], r["pinn_fav"], r["fragility"])]
        shown = [p for p in r["props"] if p["line"] is not None] or \
                [p for p in r["props"] if p["tier"] in ("elite", "good") and p["category"] != "fav_wins"]
        shown = shown[:max_props]
        for p in shown:
            L.append("")
            L.append("%s · %s · %s" % (p["tier_badge"], p["label"], p["player"] if p["player"] != "match" else "Match"))
            if p["binary"]:
                L.append("   Probability %.0f%%" % (100 * p["probability"]))
            else:
                L.append("   Projection %.1f  (10-90: %.1f–%.1f)" % (p["mean"], p["p10"], p["p90"]))
            if p["line"] is not None:
                L.append("   Line %.1f   P(over) %.0f%%   Edge %+.1f   Confidence %s"
                         % (p["line"], 100 * p["p_over"], p["edge"], p["confidence"]))
            else:
                L.append("   No line supplied   Confidence %s" % p["confidence"])
            for why in p["reasons"][:3]:
                L.append("   • %s" % why)
        best = [p for p in shown if p["tier"] == "elite"]
        weak = [p for p in shown if p["tier"] in ("caution", "low")]
        if best:
            L.append(""); L.append("⭐ BEST TIERS (historical): " + " · ".join("%s%s" % (p["label"], " (%+.1f)" % p["edge"] if p["edge"] is not None else "") for p in best[:4]))
        if weak:
            L.append("⚠️ LOWER CONFIDENCE: " + " · ".join(p["label"] for p in weak[:4]))
        L.append("")
        L.append("Fragility %s" % r["fragility_text"])
        L.append("Engine: sim %s / V1 %s / tiers %s" % (r["sim_sha"][:12], r["v1_sha"][:12], r["tiers_sha"][:8]))
        return "\n".join(L)
    except Exception:
        return None

def _edge_tracker_panel():
    """APEX-EDGE-ZONE-2026-08-27: POTENTIAL EDGE TRACKER text. Never raises."""
    try:
        import apex_edge_tracker as _aet
        return _aet.mission_control_panel()
    except Exception as e:
        return "POTENTIAL EDGE TRACKER — unavailable (%s)" % e

def render_mission_control(limit=12):
    """Read-only panel text from the sim forward log. NEVER raises."""
    try:
        import csv, numpy as np
        z = _load(); sim = z["sim"]
        head = ["APEX PROP ENGINE V1    sim %s · V1 %s · tiers v%d" % (z["c"]["_sha"][:8], z["m"]["_sha"][:8], z["tiers"]["version"])]
        path = os.path.join(HERE, sim.SIM_LOG)
        if not os.path.exists(path):
            head.append("FORWARD LOG   empty — no projections logged yet")
            head.append("player state  %s" % z["state"]["latest_date"])
            head.append("")
            head.append(_edge_tracker_panel())          # APEX-EDGE-ZONE-2026-08-27
            return "\n".join(head)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        settled = [r for r in rows if r["actual"]]
        head.append("FORWARD LOG   rows %d   settled %d   open %d" % (len(rows), len(settled), len(rows) - len(settled)))
        head.append("")
        head.append(_edge_tracker_panel())              # APEX-EDGE-ZONE-2026-08-27
        head.append("")
        head.append("BY TIER (settled, with a line)   — historical reliability of the CATEGORY, not this pick")
        for tier in ("elite", "good", "caution", "low"):
            sel = [r for r in settled if r["line"] and tier_of(r["category"])[0] == tier]
            hits = []
            for r in sel:
                try:
                    a, ln, po = float(r["actual"]), float(r["line"]), float(r["p_over"])
                except (ValueError, TypeError):
                    continue
                if a == ln: continue
                hits.append((po >= 0.5) == (a > ln))
            if hits:
                head.append(" %-18s n=%3d   hit %.1f%%" % (TIER_BADGE[tier], len(hits), 100 * np.mean(hits)))
            else:
                head.append(" %-18s n=  0   —" % TIER_BADGE[tier])
        head.append("")
        head.append("BREAK-EVEN REFERENCE   6-pick flex 54.2%% · power play 58%%")
        head.append("")
        head.append("RECENT")
        for r in rows[-limit:][::-1]:
            mark = ("✓ " + r["actual"]) if r["actual"] else "pending"
            ln = ("vs %s → %s %.0f%%" % (r["line"], "over" if float(r["p_over"] or 0) >= 0.5 else "under",
                                         100 * max(float(r["p_over"] or 0), float(r["p_under"] or 0)))) if r["line"] else "no line"
            head.append(" %s  %-22s %-14s %6s  %-22s %s" % (r["match_date"], (r["player"])[:22], r["category"][:14], r["mean"], ln, mark))
        head.append("")
        head.append("ALERTS")
        head.append(" • player state %s — refresh after the next tournament finishes" % z["state"]["latest_date"])
        thin = [c for c in set(r["category"] for r in rows) if len([r for r in settled if r["category"] == c]) < 30]
        if thin:
            head.append(" • %d categories below 30 settled rows — tiers not yet reviewable" % len(thin))
        return "\n".join(head)
    except Exception as e:
        return "APEX PROP ENGINE V1 — panel unavailable (%s)" % e

# ------------------------------------------------------------------ selftest
def cmd_selftest():
    print(MARKER, "-- SELFTEST")
    fails = []
    # tier table integrity
    with open(TIERS_FILE) as f:
        t = json.load(f)
    for k, v in t["tiers"].items():
        if v["tier"] not in TIER_BADGE: fails.append("bad tier %r on %s" % (v["tier"], k))
    if t["tiers"]["fav_wins"]["tier"] != "low": fails.append("fav_wins must be low/never-an-edge")
    if "fav_wins" not in t["never_display_as_edge"]: fails.append("fav_wins missing from never_display_as_edge")
    # every simulator category must have a tier
    try:
        import apex_prop_sim_v1 as sim
        for c in list(sim.PLAYER_PROPS) + list(sim.MATCH_PROPS):
            if c not in t["tiers"]: fails.append("category %s has no tier" % c)
    except Exception as e:
        fails.append("cannot import simulator: %s" % e)
    # confidence capping
    d = {"edge": 5.0, "sd": 1.0, "line": 1.0}
    if _confidence("caution", d) != "Low": fails.append("caution must cap at Low")
    if _confidence("good", d) != "Medium": fails.append("good must cap at Medium")
    if _confidence("elite", d) != "High": fails.append("elite with a big edge should be High")
    if _confidence("low", d) != "Informational": fails.append("low tier must be Informational")
    # fail-open: garbage in, None/ok=False out, never an exception
    for args in ((None, None, 0.6), ("Nobody At All", "Nobody Else", 0.6), ("a", "b", 1.5), ("a", "b", "x")):
        r = evaluate_match(*args)
        if r.get("ok"): fails.append("garbage input accepted: %r" % (args,))
        if render_shadow_block(*args) is not None: fails.append("shadow block returned text for garbage: %r" % (args,))
    # panel never raises
    if not isinstance(render_mission_control(), str): fails.append("panel did not return a string")
    print("tier table sha", hashlib.sha256(open(TIERS_FILE, "rb").read()).hexdigest()[:16], "| %d categories" % len(t["tiers"]))
    print(status())
    print("SELFTEST", "PASS" if not fails else "FAIL: " + "; ".join(fails))
    if fails: sys.exit(1)

def main():
    ap = argparse.ArgumentParser(description=MARKER)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--tiers", action="store_true")
    ap.add_argument("--card", nargs=2, metavar=("FAV", "DOG"))
    ap.add_argument("--pinn", type=float); ap.add_argument("--surface", default="Hard")
    ap.add_argument("--bo5", action="store_true"); ap.add_argument("--wta", action="store_true")
    ap.add_argument("--date", default=None); ap.add_argument("--sims", type=int, default=DEFAULT_SIMS)
    ap.add_argument("--line", action="append", default=[], metavar="PLAYER|CATEGORY=LINE",
                    help="repeatable, e.g. --line 'Jannik Sinner|aces=6.5' --line 'match|total_games=22.5'")
    a = ap.parse_args()
    if a.selftest: cmd_selftest()
    elif a.status: print(status())
    elif a.tiers:
        t = _load()["tiers"]
        print(MARKER, "-- TIERS v%d  sha %s" % (t["version"], _load()["tiers_sha"][:16]))
        print("basis:", t["basis"])
        for tier in ("elite", "good", "caution", "low"):
            print("\n" + TIER_BADGE[tier])
            for k, v in sorted(t["tiers"].items()):
                if v["tier"] == tier: print("  %-20s %s" % (LABEL.get(k, k), v["note"]))
        print("\nre-tier rule:", t["rules"]["retier"])
    elif a.card:
        if a.pinn is None: raise SystemExit("--card needs --pinn")
        lines = {}
        for spec in a.line:
            k, _, v = spec.partition("="); lines[k.strip()] = float(v)
        block = render_shadow_block(a.card[0], a.card[1], a.pinn, a.surface, a.bo5, a.wta, a.date, lines, a.sims)
        if block is None:
            r = evaluate_match(a.card[0], a.card[1], a.pinn, a.surface, a.bo5, a.wta, a.date, a.sims, lines)
            print("engine returned nothing (fail-open). reason:", r.get("error"))
        else:
            print(block)
    else: ap.print_help()

if __name__ == "__main__":
    main()
