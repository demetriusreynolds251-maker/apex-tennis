#!/usr/bin/env python3
"""
APEX-PROP-SIM-V1-2026-08-23
Project Apex Intelligence / OpenClaw (DEMETRIUS)

Match simulator layer on top of FROZEN Prop Model V1 (+ frozen Fragility V3 via V1).
Turns V1's averages into full distributions and probabilities for every supported prop,
from one game-by-game model of the match.

HOW IT WORKS (plain terms)
  1. V1 gives each player an expected hold rate (surface-specific, calibrated) and
     expected aces / double faults per service game.
  2. The two hold probabilities are adjusted by ONE number (the "tilt") until the
     simulated match-win probability equals the Pinnacle price. The serve LEVEL of the
     match comes from the players (V1 rates); the GAP comes from the market. Never the
     other way round.
  3. Every set is played out game by game (alternating serve, tiebreak at 6-6), every
     match set by set (best of 3 or 5). Aces and double faults are drawn per service
     game with a fitted over-dispersion.
  4. Three constants are fitted on <=2021, one knob is tuned on 2022-23, everything is
     judged on 2024+ matches the fit never saw. Then the constants are frozen.

COMMANDS (run in the project folder; needs apex_prop_model_v1.py + its frozen json/state)
  --selftest                      exact checks on the game/set/match engine + freeze guards
  --build  [--sims N]             fit/tune on apex_prop_rows_v1.csv, holdout report, write
                                  apex_prop_sim_v1.json (UNFROZEN) + apex_prop_sim_report_v1.txt
  --freeze                        stamp + SHA; refuses twice
  --spec                          frozen constants, supported props, log schema
  --project "Fav" "Dog" --pinn P --surface S [--bo5] [--wta] [--sims N]
                                  distributions + probabilities for every prop; logs them
  --line MATCH_ID PLAYER|match CATEGORY LINE BOOK
                                  attach a line -> P(over)/P(under) on the logged row
  --settle-sackmann               fill actuals from the year files (needs score)
  --report                        forward report: calibration, hit rate, Brier by category
"""
import warnings
warnings.filterwarnings("ignore")
import argparse, csv, hashlib, json, math, os, pickle, re, sys, tempfile, time
from datetime import datetime, date
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apex_prop_model_v1 as v1

MARKER = "APEX-PROP-SIM-V1-2026-08-23"
SIM_MODEL = "apex_prop_sim_v1.json"
SIM_REPORT = "apex_prop_sim_report_v1.txt"
SIM_LOG = "apex_prop_sim_forward_log_v1.csv"
FIT_END, TUNE_START, TUNE_END, HOLD_START = 2021, 2022, 2023, 2024

PLAYER_PROPS = ["aces", "dfs", "holds", "breaks", "svgms", "games_won", "games_lost", "sets_won", "fantasy_score"]
MATCH_PROPS = ["total_games", "first_set_games", "first_set_fav_wins", "total_aces", "most_aces_fav", "most_games_fav", "total_sets", "tiebreaks", "goes_3_sets", "goes_5_sets", "any_tiebreak", "fav_wins"]
LOG_FIELDS = ["ts", "match_id", "match_date", "player", "opponent", "is_fav", "pinn_fav", "surface", "best_of",
              "category", "mean", "sd", "p10", "p50", "p90", "fragility", "hold_fav", "hold_dog",
              "book", "line", "p_over", "p_under", "actual", "settled_ts", "sim_sha", "v1_sha"]

# ------------------------------------------------------------------ helpers
def sha256_text(s): return hashlib.sha256(s.encode("utf-8")).hexdigest()
def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""): h.update(c)
    return h.hexdigest()
def sigmoid(x): return 1.0 / (1.0 + math.exp(-x))

def parse_score(score):
    """-> dict(sets=[(a,b,tb)], retired) in winner/loser frame; None if unparseable."""
    if not isinstance(score, str) or not score.strip(): return None
    s = score.strip(); ret = any(t in s.upper() for t in ("RET", "W/O", "DEF", "ABN", "WALKOVER"))
    sets = []
    for tok in s.split():
        m = re.match(r"^(\d+)-(\d+)(\((\d+)\))?$", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 20 or b > 20: continue              # match tiebreak written as a set (e.g. 10-8)
            sets.append((a, b, 1 if (m.group(3) or (a, b) in ((7, 6), (6, 7))) else 0))
    if not sets: return None
    return {"sets": sets, "retired": ret}

# ------------------------------------------------------------------ exact set engine
def set_table(hA, hB, pTB, first):
    """Exact distribution of one set. first=0 -> A serves game 1, 1 -> B.
    Returns list of (prob, winA, gA, gB, svA, hdA, svB, hdB, tb, next_first)."""
    out = []
    # state: (gA, gB, holdsA, holdsB) -> prob ; server of game n = first if n even else 1-first
    states = {(0, 0, 0, 0): 1.0}
    while states:
        nxt = {}
        for (gA, gB, hA_, hB_), p in states.items():
            n = gA + gB
            if gA == 6 and gB == 6:
                svA = (n + 1) // 2 if first == 0 else n // 2
                svB = n - svA
                nf = 1 - first            # TB first server = 'first'; next set starts with the other
                out.append((p * pTB, 1, 7, 6, svA, hA_, svB, hB_, 1, nf))
                out.append((p * (1 - pTB), 0, 6, 7, svA, hA_, svB, hB_, 1, nf))
                continue
            server = first if n % 2 == 0 else 1 - first
            if server == 0:
                hold, brk = (gA + 1, gB, hA_ + 1, hB_), (gA, gB + 1, hA_, hB_)
                ph = hA
            else:
                hold, brk = (gA, gB + 1, hA_, hB_ + 1), (gA + 1, gB, hA_, hB_)
                ph = hB
            for st, pp in ((hold, p * ph), (brk, p * (1 - ph))):
                if pp <= 0: continue
                a, b = st[0], st[1]
                if (a >= 6 or b >= 6) and abs(a - b) >= 2:
                    n2 = a + b; svA = (n2 + 1) // 2 if first == 0 else n2 // 2; svB = n2 - svA
                    last_server = first if (n2 - 1) % 2 == 0 else 1 - first
                    out.append((pp, 1 if a > b else 0, a, b, svA, st[2], svB, st[3], 0, 1 - last_server))
                else:
                    nxt[st] = nxt.get(st, 0.0) + pp
        states = nxt
    return out

def set_winprob(hA, hB, pTB, first):
    return set_summary(hA, hB, pTB, first)[0]

def set_summary(hA, hB, pTB, first):
    """Slim exact set DP (no hold bookkeeping): returns
    (P(A wins set), P(next first server = A | A won), P(next first = A | B won), E[games])."""
    states = {(0, 0): 1.0}; pA = 0.0; nA_w = 0.0; nA_l = 0.0; eg = 0.0
    while states:
        nxt = {}
        for (gA, gB), p in states.items():
            n = gA + gB
            if gA == 6 and gB == 6:
                # tiebreak: 13 games; TB first server = 'first'; next set starts with the other
                pA += p * pTB; eg += p * 13
                if first == 1: nA_w += p * pTB; nA_l += p * (1 - pTB)
                continue
            server = first if n % 2 == 0 else 1 - first
            ph = hA if server == 0 else hB
            for (a, b), pp in (((gA + 1, gB) if server == 0 else (gA, gB + 1), p * ph),
                               ((gA, gB + 1) if server == 0 else (gA + 1, gB), p * (1 - ph))):
                if pp <= 0: continue
                if (a >= 6 or b >= 6) and abs(a - b) >= 2:
                    n2 = a + b; eg += pp * n2
                    last_server = first if (n2 - 1) % 2 == 0 else 1 - first
                    nextA = 1 if last_server == 1 else 0
                    if a > b:
                        pA += pp
                        if nextA: nA_w += pp
                    else:
                        if nextA: nA_l += pp
                else:
                    nxt[(a, b)] = nxt.get((a, b), 0.0) + pp
        states = nxt
    return pA, nA_w / max(pA, 1e-12), nA_l / max(1 - pA, 1e-12), eg

def set_nextfirst_probs(hA, hB, pTB, first):
    """P(next set first server = A | this set first server), and E[games], by exact table."""
    tab = set_table(hA, hB, pTB, first)
    pA = sum(r[0] for r in tab if r[1] == 1)
    eg = sum(r[0] * (r[2] + r[3]) for r in tab)
    pnext0 = sum(r[0] for r in tab if r[9] == 0)
    return pA, eg, pnext0, tab

def match_winprob(hA, hB, pTB, best_of):
    """Exact P(A wins match). Set-level DP over (setsA, setsB, first_server)."""
    need = best_of // 2 + 1
    info = {f: set_summary(hA, hB, pTB, f) for f in (0, 1)}
    states = {(0, 0, 0): 1.0}; pwin = 0.0
    while states:
        nxt = {}
        for (sa, sb, f), p in states.items():
            pa, q_w, q_l, _ = info[f]
            for won, pp in ((1, p * pa), (0, p * (1 - pa))):
                sa2, sb2 = sa + won, sb + (1 - won)
                if sa2 == need: pwin += pp; continue
                if sb2 == need: continue
                q0 = q_w if won else q_l
                for f2, pf in ((0, q0), (1, 1 - q0)):
                    if pf > 0: nxt[(sa2, sb2, f2)] = nxt.get((sa2, sb2, f2), 0.0) + pp * pf
        states = nxt
    return pwin

GH_NODES = [(-2.0201828705, 0.0199532421), (-0.9585724646, 0.3936193232), (0.0, 0.9453087205), (0.9585724646, 0.3936193232), (2.0201828705, 0.0199532421)]
GH_W = [w / sum(x[1] for x in GH_NODES) for _, w in GH_NODES]
GH_X = [x * math.sqrt(2.0) for x, _ in GH_NODES]

def gap_nodes(hA, hB, sigma):
    """Per-match 'who plays above expectation' effect: the hold GAP shifts by g ~ N(0, sigma)."""
    if not sigma: return [(hA, hB, 1.0)]
    out = []
    for x, w in zip(GH_X, GH_W):
        g = sigma * x
        out.append((min(max(hA + g, 0.20), 0.995), min(max(hB - g, 0.20), 0.995), w))
    return out

def match_winprob_mix(hA, hB, pTB_k, best_of, sigma):
    return sum(w * match_winprob(a, b, sigmoid(pTB_k * (a - b)), best_of) for a, b, w in gap_nodes(hA, hB, sigma))

def solve_tilt(level, pinn, pTB_k, best_of, lo=-0.45, hi=0.45, sigma=0.0):
    """hA = level + t, hB = level - t ; find t so that P(A wins match) = pinn (averaged over the gap spread)."""
    def f(t):
        hA = min(max(level + t, 0.20), 0.995); hB = min(max(level - t, 0.20), 0.995)
        return match_winprob_mix(hA, hB, pTB_k, best_of, sigma) - pinn
    flo, fhi = f(lo), f(hi)
    if flo > 0: return lo, "clipped_low"
    if fhi < 0: return hi, "clipped_high"
    for _ in range(16):
        mid = 0.5 * (lo + hi); fm = f(mid)
        if fm > 0: hi = mid
        else: lo = mid
    return 0.5 * (lo + hi), "ok"

# ------------------------------------------------------------------ Monte Carlo
def simulate(hA, hB, pTB_k, best_of, muA, muB, rA, rB, n_sims, seed=None, sigma=0.0):
    """Vectorised Monte Carlo. muA/muB = (ace_per_svg, df_per_svg); rA/rB = (r_ace, r_df) NB dispersion.
    Returns dict of numpy arrays, one value per sim."""
    rng = np.random.default_rng(seed)
    for v in (hA, hB, muA[0], muA[1], muB[0], muB[1]):
        if not np.isfinite(v): raise ValueError("simulate: non-finite input (hold or rate) — row should have been filtered")
    nodes = gap_nodes(hA, hB, sigma)
    tabs = {}
    for j, (a, b, w) in enumerate(nodes):
        pTB = sigmoid(pTB_k * (a - b))
        for f in (0, 1):
            tab = set_table(a, b, pTB, f)
            arr = np.array([[r[0]] + list(r[1:]) for r in tab], float)
            arr[:, 0] /= arr[:, 0].sum()
            tabs[(j, f)] = arr
    need = best_of // 2 + 1
    N = n_sims
    node = rng.choice(len(nodes), size=N, p=[w for _, _, w in nodes])
    setsA = np.zeros(N, int); setsB = np.zeros(N, int); first = np.zeros(N, int)
    gA = np.zeros(N, int); gB = np.zeros(N, int); svA = np.zeros(N, int); hdA = np.zeros(N, int)
    svB = np.zeros(N, int); hdB = np.zeros(N, int); tb = np.zeros(N, int); fsg = np.zeros(N, int); fsw = np.zeros(N, int); nsets = np.zeros(N, int)
    alive = np.ones(N, bool)
    for si in range(best_of):
        idx_all = np.nonzero(alive)[0]
        if len(idx_all) == 0: break
        groups = {(j, f): idx_all[(first[idx_all] == f) & (node[idx_all] == j)] for j in range(len(nodes)) for f in (0, 1)}   # fixed BEFORE any update
        for (j, f), idx in groups.items():
            if len(idx) == 0: continue
            t = tabs[(j, f)]
            pick = rng.choice(len(t), size=len(idx), p=t[:, 0])
            o = t[pick]
            winA = o[:, 1].astype(int)
            setsA[idx] += winA; setsB[idx] += 1 - winA
            gA[idx] += o[:, 2].astype(int); gB[idx] += o[:, 3].astype(int)
            svA[idx] += o[:, 4].astype(int); hdA[idx] += o[:, 5].astype(int)
            svB[idx] += o[:, 6].astype(int); hdB[idx] += o[:, 7].astype(int)
            tb[idx] += o[:, 8].astype(int); first[idx] = o[:, 9].astype(int)
            if si == 0: fsg[idx] = (o[:, 2] + o[:, 3]).astype(int); fsw[idx] = winA; fsw[idx] = winA
            nsets[idx] += 1
        alive &= (setsA < need) & (setsB < need)
    def nb(mean_per_game, svg, r):
        lam = mean_per_game * svg
        if r is None or r <= 0 or r > 1e6:
            return rng.poisson(np.maximum(lam, 1e-9))
        g = rng.gamma(shape=r, scale=np.maximum(lam, 1e-9) / r)
        return rng.poisson(g)
    acesA = nb(muA[0], svA, rA[0]); acesB = nb(muB[0], svB, rB[0])
    dfsA = nb(muA[1], svA, rA[1]); dfsB = nb(muB[1], svB, rB[1])
    # PrizePicks tennis fantasy score (verified from the in-app scoring chart, 2026-08-23):
    #   10 (match played) + (games won - games lost) + 3*(sets won - sets lost) + 0.5*(aces - double faults)
    fsA = 10.0 + (gA - gB) + 3.0 * (setsA - setsB) + 0.5 * (acesA - dfsA)
    fsB = 10.0 + (gB - gA) + 3.0 * (setsB - setsA) + 0.5 * (acesB - dfsB)
    acesA = nb(muA[0], svA, rA[0]); acesB = nb(muB[0], svB, rB[0])
    dfsA = nb(muA[1], svA, rA[1]); dfsB = nb(muB[1], svB, rB[1])
    # PrizePicks tennis fantasy score (verified from the in-app scoring chart, 2026-08-23):
    #   10 (match played) + (games won - games lost) + 3*(sets won - sets lost) + 0.5*(aces - double faults)
    fsA = 10.0 + (gA - gB) + 3.0 * (setsA - setsB) + 0.5 * (acesA - dfsA)
    fsB = 10.0 + (gB - gA) + 3.0 * (setsB - setsA) + 0.5 * (acesB - dfsB)
    out = {
        "fav_wins": (setsA > setsB).astype(int), "first_set_fav_wins": fsw, "total_aces": acesA + acesB,
        "most_aces_fav": (acesA > acesB).astype(int) + 0.5 * (acesA == acesB),
        "first_set_fav_wins": fsw,
        "total_aces": acesA + acesB,
        "most_aces_fav": np.where(acesA > acesB, 1, np.where(acesA < acesB, 0, -1)),    # -1 = tie/push
        "most_games_fav": np.where(gA > gB, 1, np.where(gA < gB, 0, -1)),
        "total_games": gA + gB, "first_set_games": fsg, "total_sets": nsets, "tiebreaks": tb,
        "any_tiebreak": (tb > 0).astype(int),
        "goes_3_sets": (nsets == 3).astype(int) if best_of == 3 else (nsets >= 3).astype(int),
        "goes_5_sets": (nsets == 5).astype(int),
        "fav": {"aces": acesA, "dfs": dfsA, "fantasy_score": fsA, "holds": hdA,
                "breaks": svB - hdB, "svgms": svA, "games_won": gA, "games_lost": gB, "sets_won": setsA},
        "dog": {"aces": acesB, "dfs": dfsB, "fantasy_score": fsB, "holds": hdB,
                "breaks": svA - hdA, "svgms": svB, "games_won": gB, "games_lost": gA, "sets_won": setsB},
    }
    return out

# ------------------------------------------------------------------ V1 glue
def v1_inputs_from_rows(df, m):
    """From V1 rows (fav frame) + frozen V1 model -> per-row: level, mu/r inputs, price, bo."""
    P = {side: v1.project(df, m, side, "full") for side in ("fav", "dog")}
    hf = np.clip(P["fav"]["holds"] / np.maximum(P["fav"]["svgms"], 1), 0.05, 0.995)
    hd = np.clip(P["dog"]["holds"] / np.maximum(P["dog"]["svgms"], 1), 0.05, 0.995)
    mu = {side: (P[side]["aces"] / np.maximum(P[side]["svgms"], 1), P[side]["dfs"] / np.maximum(P[side]["svgms"], 1)) for side in P}
    return P, hf, hd, mu

def level_from(hf, hd, coefs, price_level):
    a = coefs.get("alpha", 1.0)
    return a * 0.5 * (hf + hd) + (1 - a) * price_level

def price_level_pred(df, coefs):
    X = v1.design_struct(df)
    return X @ np.array(coefs["level_price"])

# ------------------------------------------------------------------ build / fit
def attach_scores(df, sack_dir, year_from=2005):
    sack = v1.load_sackmann(sack_dir, year_from, challengers=False)
    key = {}
    for r in sack.itertuples(index=False):
        key[(r.tourney_date.date().isoformat(), v1.norm_name(r.winner_name), v1.norm_name(r.loser_name))] = r.score
    sc = [key.get((d, v1.norm_name(w), v1.norm_name(l))) for d, w, l in zip(df["date"], df["winner"], df["loser"])]
    df = df.copy(); df["score"] = sc
    parsed = [parse_score(s) for s in sc]
    df["n_sets"] = [len(p["sets"]) if p else np.nan for p in parsed]
    df["n_tb"] = [sum(s[2] for s in p["sets"]) if p else np.nan for p in parsed]
    df["first_set_games"] = [p["sets"][0][0] + p["sets"][0][1] if p else np.nan for p in parsed]
    # sets won in fav frame
    fav_is_w = df["fav_won"].values == 1
    sw = [sum(1 for s in p["sets"] if s[0] > s[1]) if p else np.nan for p in parsed]
    sl = [sum(1 for s in p["sets"] if s[1] > s[0]) if p else np.nan for p in parsed]
    df["fav_sets_won"] = np.where(fav_is_w, sw, sl); df["dog_sets_won"] = np.where(fav_is_w, sl, sw)
    fs_w = [1 if (p and p["sets"][0][0] > p["sets"][0][1]) else 0 if p else np.nan for p in parsed]
    df["fav_first_set"] = np.where(fav_is_w, fs_w, [1 - x if x == x else np.nan for x in fs_w])
    df["fav_games_won"] = df["fav_holds"] + df["fav_breaks"]; df["dog_games_won"] = df["dog_holds"] + df["dog_breaks"]
    df["fav_games_lost"] = df["dog_games_won"]; df["dog_games_lost"] = df["fav_games_won"]
    for side, opp in (("fav", "dog"), ("dog", "fav")):
        df["%s_fantasy_score" % side] = (10.0 + (df["%s_games_won" % side] - df["%s_games_won" % opp])
                                         + 3.0 * (df["%s_sets_won" % side] - df["%s_sets_won" % opp])
                                         + 0.5 * (df["%s_aces" % side] - df["%s_dfs" % side]))
    # tiebreak sets in fav frame: who won each TB set
    tbw = []; tbl = []
    for p, w in zip(parsed, fav_is_w):
        if not p: tbw.append(np.nan); tbl.append(np.nan); continue
        a = sum(1 for s in p["sets"] if s[2] and s[0] > s[1]); b = sum(1 for s in p["sets"] if s[2] and s[1] > s[0])
        tbw.append(a if w else b); tbl.append(b if w else a)
    df["fav_tb_won"] = tbw; df["fav_tb_lost"] = tbl
    fsw = [(p["sets"][0][0] > p["sets"][0][1]) if p else np.nan for p in parsed]
    df["fav_first_set"] = [ (x if w else (1 - x)) if x == x else np.nan for x, w in zip(fsw, fav_is_w)]
    return df

def fit_constants(df, m, L):
    coefs = {"marker": MARKER, "frozen": False, "v1_sha": m["_sha"]}
    fit = df[df["year"] <= FIT_END]
    # (1) price-only hold level regression (for the alpha blend)
    lvl = 0.5 * (fit["fav_holds"] / fit["fav_svgms"] + fit["dog_holds"] / fit["dog_svgms"])
    ok = np.isfinite(lvl)
    b, t, _ = v1.ols(v1.design_struct(fit[ok]), lvl[ok])
    coefs["level_price"] = b.tolist()
    L.append("level_price (hold level from price, fit<=%d): " % FIT_END + " + ".join("%.4f*%s" % (c, n) for c, n in zip(b, m["struct_names"])))
    # (2) NB dispersion for aces / dfs given actual service games (moment estimate)
    P, hf, hd, mu = v1_inputs_from_rows(fit, m)
    coefs["disp"] = {}
    for side in ("fav", "dog"):
        coefs["disp"][side] = {}
        for j, cat in enumerate(("aces", "dfs")):
            lam = mu[side][j] * fit["%s_svgms" % side].values; y = fit["%s_%s" % (side, cat)].values
            mk = np.isfinite(lam) & np.isfinite(y) & (lam > 0)
            excess = np.mean((y[mk] - lam[mk]) ** 2) - np.mean(lam[mk])
            r = float(np.mean(lam[mk]) ** 2 / excess) if excess > 0 else 1e6
            coefs["disp"][side][cat] = r
            L.append("  NB dispersion %s %s: r=%.2f (var/mean %.2f)" % (side, cat, r, np.mean((y[mk] - lam[mk]) ** 2) / np.mean(lam[mk])))
    # (3) tiebreak steepness k: P(fav wins TB) = sigmoid(k*(hA-hB)), fitted on TB sets <=2021 using solved holds
    tbs = fit[(fit["fav_tb_won"].fillna(0) + fit["fav_tb_lost"].fillna(0)) > 0]
    tbs = tbs.sample(min(len(tbs), 1500), random_state=7) if len(tbs) > 1500 else tbs
    Pt, hft, hdt, _ = v1_inputs_from_rows(tbs, m)
    lvl_t = 0.5 * (hft + hdt)
    best = None
    for k in (0.0, 1.5, 3.0, 4.5, 6.0):
        ll = 0.0; n = 0
        for i, (lv, pn, bo) in enumerate(zip(lvl_t, tbs["pinn_fav"].values, tbs["best_of"].values)):
            t, _ = solve_tilt(lv, pn, k, int(bo))
            p = min(max(sigmoid(k * 2 * t), 1e-4), 1 - 1e-4)
            w = tbs["fav_tb_won"].values[i]; l = tbs["fav_tb_lost"].values[i]
            ll += w * math.log(p) + l * math.log(1 - p); n += w + l
        L.append("  tiebreak k=%.1f  loglik/TB %.4f (n=%d)" % (k, ll / max(n, 1), n))
        if best is None or ll > best[0]: best = (ll, k)
    coefs["tb_k"] = best[1]
    L.append("chosen tb_k=%.1f" % best[1])
    # (4) gap spread sigma: fitted on <=2021 by matching the deciding-set rate (iid game model makes matches too close)
    samp = fit.sample(min(len(fit), 500), random_state=3)
    act_dec = float(np.mean(np.where(samp["best_of"].values == 3, samp["n_sets"].values == 3, samp["n_sets"].values == 5)))
    coefs["alpha"] = 0.75; best = None
    L.append("gap spread sigma (fit<=%d, n=%d): match deciding-set rate %.3f" % (FIT_END, len(samp), act_dec))
    for sg in (0.0, 0.03, 0.06, 0.09, 0.12, 0.15):
        coefs["gap_sigma"] = sg
        sims = run_rows(samp, m, coefs, 300, seed=9, progress=False)
        dec = float(np.mean([(s["total_sets"] == (3 if bo == 3 else 5)).mean() for (s, _, _, _), bo in zip(sims, samp["best_of"].values)]))
        tbr = float(np.mean([s["any_tiebreak"].mean() for s, _, _, _ in sims]))
        L.append("  sigma=%.2f  deciding-set %.3f (target %.3f)  any-tiebreak %.3f" % (sg, dec, act_dec, tbr))
        if best is None or abs(dec - act_dec) < best[0]: best = (abs(dec - act_dec), sg)
    coefs["gap_sigma"] = best[1]
    L.append("chosen gap_sigma=%.2f%s" % (best[1], "  ** at the edge of the grid — widen it before trusting" if best[1] in (0.0, 0.15) else ""))
    return coefs

def exact_expected_tg(hA, hB, k, best_of):
    pTB = sigmoid(k * (hA - hB)); need = best_of // 2 + 1
    info = {f: set_summary(hA, hB, pTB, f) for f in (0, 1)}
    states = {(0, 0, 0): 1.0}; etg = 0.0
    while states:
        nxt = {}
        for (sa, sb, f), p in states.items():
            pa, q_w, q_l, eg = info[f]; etg += p * eg
            for won, pp in ((1, p * pa), (0, p * (1 - pa))):
                sa2, sb2 = sa + won, sb + (1 - won)
                if sa2 == need or sb2 == need: continue
                q0 = q_w if won else q_l
                for f2, pf in ((0, q0), (1, 1 - q0)):
                    if pf > 0: nxt[(sa2, sb2, f2)] = nxt.get((sa2, sb2, f2), 0.0) + pp * pf
        states = nxt
    return etg

def tune_alpha(df, m, coefs, L):
    tune = df[(df["year"] >= TUNE_START) & (df["year"] <= TUNE_END)]
    tune = tune.sample(min(len(tune), 1000), random_state=11) if len(tune) > 1000 else tune
    best = None
    L.append("tuning alpha (hold level = alpha*rates + (1-alpha)*price) on %d-%d (n=%d): score = MAE total games + MAE fav holds + MAE dog holds" % (TUNE_START, TUNE_END, len(tune)))
    for a in (0.0, 0.25, 0.5, 0.75, 1.0):
        coefs["alpha"] = a
        sims = run_rows(tune, m, coefs, 300, seed=5, progress=False)
        tg = np.array([s["total_games"].mean() for s, _, _, _ in sims]); hf = np.array([s["fav"]["holds"].mean() for s, _, _, _ in sims]); hd = np.array([s["dog"]["holds"].mean() for s, _, _, _ in sims])
        e_tg = float(np.nanmean(np.abs(tg - tune["total_games"].values))); e_hf = float(np.nanmean(np.abs(hf - tune["fav_holds"].values))); e_hd = float(np.nanmean(np.abs(hd - tune["dog_holds"].values)))
        score = e_tg + e_hf + e_hd
        L.append("  alpha=%.2f  TG MAE %.3f  fav holds %.3f  dog holds %.3f  -> %.3f" % (a, e_tg, e_hf, e_hd, score))
        if best is None or score < best[0]: best = (score, a)
    coefs["alpha"] = best[1]; L.append("chosen alpha=%.2f" % best[1])
    # dispersion multiplier: bring the aces/dfs 80% band to 80% coverage on 2022-23 (sim adds svgms variance on top of the fitted r)
    best = None; L.append("tuning dispersion multiplier (coverage of the central 80%% band for aces+dfs, target 0.80):")
    for dm in (0.5, 1.0, 2.0, 4.0, 1e6):
        coefs["disp_mult"] = dm
        sims = run_rows(tune, m, coefs, 300, seed=6, progress=False)
        cov = []
        for (s, _, _, _), (_, r) in zip(sims, tune.iterrows()):
            for side in ("fav", "dog"):
                for cat in ("aces", "dfs"):
                    x = s[side][cat]; y = r["%s_%s" % (side, cat)]
                    if np.isfinite(y): cov.append(np.percentile(x, 10) <= y <= np.percentile(x, 90))
        c80 = float(np.mean(cov)); L.append("  disp_mult=%g  80%%-band coverage %.3f" % (dm, c80))
        if best is None or abs(c80 - 0.80) < best[0]: best = (abs(c80 - 0.80), dm)
    coefs["disp_mult"] = best[1]
    L.append("chosen disp_mult=%g%s" % (best[1], "  ** at the edge of the grid — the remaining band width is structural, not dispersion" if best[1] in (0.5, 1e6) else ""))

def run_rows(df, m, coefs, n_sims, seed=1, progress=True):
    """Simulate each row. Returns list of per-row summaries (dict of arrays kept small)."""
    P, hf, hd, mu = v1_inputs_from_rows(df, m)
    pl = price_level_pred(df, coefs)
    rows = []; t0 = time.time()
    for i in range(len(df)):
        if progress and i and i % 1000 == 0: print("  ... %d/%d (%.0fs)" % (i, len(df), time.time() - t0)); sys.stdout.flush()
        lv = level_from(hf[i], hd[i], coefs, pl[i]); bo = int(df["best_of"].values[i])
        t, flag = solve_tilt(lv, df["pinn_fav"].values[i], coefs["tb_k"], bo, sigma=coefs.get("gap_sigma", 0.0))
        hA = min(max(lv + t, 0.2), 0.995); hB = min(max(lv - t, 0.2), 0.995)
        dm = coefs.get("disp_mult", 1.0)
        s = simulate(hA, hB, coefs["tb_k"], bo, (mu["fav"][0][i], mu["fav"][1][i]), (mu["dog"][0][i], mu["dog"][1][i]),
                     (coefs["disp"]["fav"]["aces"] * dm, coefs["disp"]["fav"]["dfs"] * dm), (coefs["disp"]["dog"]["aces"] * dm, coefs["disp"]["dog"]["dfs"] * dm),
                     n_sims, seed=seed + i, sigma=coefs.get("gap_sigma", 0.0))
        rows.append((s, hA, hB, flag))
    return rows

def summarize(s, side=None, cat=None):
    x = s[cat] if side is None else s[side][cat]
    return {"mean": float(x.mean()), "sd": float(x.std()), "p10": float(np.percentile(x, 10)), "p50": float(np.percentile(x, 50)), "p90": float(np.percentile(x, 90))}

def p_over(x, line):
    return float(np.mean(x > line)), float(np.mean(x < line))

def holdout_report(df, m, coefs, n_sims, L):
    hold = df[df["year"] >= HOLD_START]
    L.append(""); L.append("== HOLDOUT %d+ (n=%d, %d sims/match) ==" % (HOLD_START, len(hold), n_sims))
    sims = run_rows(hold, m, coefs, n_sims)
    Pv1 = {side: v1.project(hold, m, side, "full") for side in ("fav", "dog")}
    summary = {}
    # --- means: sim vs V1 vs actual
    L.append("mean accuracy, MAE (sim / V1 / price-only-V1):")
    for side in ("fav", "dog"):
        Pp = v1.project(hold, m, side, "price")
        for cat in ("aces", "dfs", "holds", "breaks", "svgms", "games_won", "fantasy_score"):
            y = hold["%s_%s" % (side, cat)].values
            sm = np.array([s[side][cat].mean() for s, _, _, _ in sims])
            mk = np.isfinite(y) & np.isfinite(sm)
            v1m = Pv1[side][cat] if cat in Pv1[side] else (Pv1[side]["holds"] + Pv1[side]["breaks"])
            pm = Pp[cat] if cat in Pp else (Pp["holds"] + Pp["breaks"])
            if cat == "fantasy_score":
                v1m = np.full(len(hold), np.nan); pm = np.full(len(hold), np.nan)
            mae = (float(np.mean(np.abs(sm[mk] - y[mk]))),
                   float(np.mean(np.abs(v1m[mk] - y[mk]))) if np.isfinite(v1m).any() else float("nan"),
                   float(np.mean(np.abs(pm[mk] - y[mk]))) if np.isfinite(pm).any() else float("nan"))
            summary["%s_%s" % (side, cat)] = {"mae_sim": mae[0], "mae_v1": mae[1], "mae_price": mae[2], "n": int(mk.sum())}
            L.append("  %s %-10s %.3f / %.3f / %.3f  n=%d" % (side, cat, mae[0], mae[1], mae[2], mk.sum()))
    for cat, col in (("total_games", "total_games"), ("first_set_games", "first_set_games"), ("total_sets", "n_sets"), ("tiebreaks", "n_tb")):
        y = hold[col].values; sm = np.array([s[cat].mean() for s, _, _, _ in sims]); mk = np.isfinite(y)
        base = (v1.design_struct(hold) @ np.array(m["total_games"])) if cat == "total_games" else None
        extra = " / V1 price-only %.3f" % float(np.mean(np.abs(base[mk] - y[mk]))) if base is not None else ""
        summary[cat] = {"mae_sim": float(np.mean(np.abs(sm[mk] - y[mk]))), "n": int(mk.sum())}
        L.append("  match %-14s MAE %.3f%s  n=%d" % (cat, summary[cat]["mae_sim"], extra, mk.sum()))
    y = (hold["fav_aces"] + hold["dog_aces"]).values; sm = np.array([s["total_aces"].mean() for s, _, _, _ in sims]); mk = np.isfinite(y)
    summary["total_aces"] = {"mae_sim": float(np.mean(np.abs(sm[mk] - y[mk]))), "n": int(mk.sum())}
    L.append("  match %-14s MAE %.3f  n=%d" % ("total_aces", summary["total_aces"]["mae_sim"], mk.sum()))
    # --- market sanity: sim P(fav wins) vs price vs actual
    pf = np.array([s["fav_wins"].mean() for s, _, _, _ in sims])
    L.append("market anchor: mean sim P(fav) %.4f vs mean pinn %.4f vs actual fav win rate %.4f  (must agree; the sim never argues with the price)"
             % (pf.mean(), hold["pinn_fav"].mean(), hold["fav_won"].mean()))
    flags = [f for _, _, _, f in sims]; L.append("tilt solver: ok %d, clipped %d" % (flags.count("ok"), len(flags) - flags.count("ok")))
    # --- coverage: share of actuals inside central 50 / 80 / 90 % sim intervals
    L.append(""); L.append("coverage (share of actual outcomes inside the sim's central interval; target 50% / 80% / 90%):")
    cov = {}
    def cov_line(name, ys, xs_list):
        c = {q: [] for q in (50, 80, 90)}
        for y, x in zip(ys, xs_list):
            if not np.isfinite(y): continue
            for q in c:
                lo, hi = np.percentile(x, 50 - q / 2), np.percentile(x, 50 + q / 2)
                c[q].append(lo <= y <= hi)
        res = {q: float(np.mean(v)) for q, v in c.items() if v}
        cov[name] = res
        L.append("  %-18s %5.1f%% / %5.1f%% / %5.1f%%  n=%d" % (name, 100 * res.get(50, 0), 100 * res.get(80, 0), 100 * res.get(90, 0), len(c[50])))
    for side in ("fav", "dog"):
        for cat in ("aces", "dfs", "holds", "breaks", "games_won", "fantasy_score"):
            cov_line("%s_%s" % (side, cat), hold["%s_%s" % (side, cat)].values, [s[side][cat] for s, _, _, _ in sims])
    cov_line("total_games", hold["total_games"].values, [s["total_games"] for s, _, _, _ in sims])
    cov_line("first_set_games", hold["first_set_games"].values, [s["first_set_games"] for s, _, _, _ in sims])
    cov_line("total_aces", (hold["fav_aces"] + hold["dog_aces"]).values, [s["total_aces"] for s, _, _, _ in sims])
    # --- probability calibration at a line near the median: P(over floor(mean)+0.5)
    L.append(""); L.append("probability calibration at line = floor(sim mean)+0.5 — predicted P(over) bucket: mean predicted / actual over rate / n ; Brier")
    calib = {}
    def calib_line(name, ys, xs_list):
        preds = []; acts = []
        for y, x in zip(ys, xs_list):
            if not np.isfinite(y): continue
            line = math.floor(x.mean()) + 0.5
            preds.append(float(np.mean(x > line))); acts.append(float(y > line))
        preds = np.array(preds); acts = np.array(acts)
        if len(preds) == 0: return
        bins = [(0, .35), (.35, .45), (.45, .55), (.55, .65), (.65, 1.01)]
        parts = []
        for lo, hi in bins:
            mk = (preds >= lo) & (preds < hi)
            if mk.sum() >= 20: parts.append("%.2f/%.2f/%d" % (preds[mk].mean(), acts[mk].mean(), mk.sum()))
        brier = float(np.mean((preds - acts) ** 2))
        calib[name] = {"brier": brier, "n": int(len(preds))}
        L.append("  %-18s " % name + "  ".join(parts) + "  ; Brier %.4f" % brier)
    for side in ("fav", "dog"):
        for cat in ("aces", "dfs", "holds", "breaks", "games_won", "fantasy_score"):
            calib_line("%s_%s" % (side, cat), hold["%s_%s" % (side, cat)].values, [s[side][cat] for s, _, _, _ in sims])
    calib_line("total_games", hold["total_games"].values, [s["total_games"] for s, _, _, _ in sims])
    calib_line("first_set_games", hold["first_set_games"].values, [s["first_set_games"] for s, _, _, _ in sims])
    calib_line("total_aces", (hold["fav_aces"] + hold["dog_aces"]).values, [s["total_aces"] for s, _, _, _ in sims])
    # --- binary props
    L.append(""); L.append("binary props: mean predicted probability vs actual rate (n):")
    binp = {}
    fav_aces = hold["fav_aces"].values; dog_aces = hold["dog_aces"].values
    for cat in ("goes_3_sets", "goes_5_sets", "any_tiebreak", "first_set_fav_wins", "most_aces_fav", "most_games_fav"):
        if cat in ("most_aces_fav", "most_games_fav"): pred = np.array([np.mean(s[cat] == 1) / max(np.mean(s[cat] != -1), 1e-9) for s, _, _, _ in sims])
        else: pred = np.array([s[cat].mean() for s, _, _, _ in sims])
        if cat == "goes_3_sets": act = np.where(hold["best_of"].values == 3, hold["n_sets"].values == 3, hold["n_sets"].values >= 3).astype(float)
        elif cat == "goes_5_sets": act = (hold["n_sets"].values == 5).astype(float)
        elif cat == "any_tiebreak": act = (hold["n_tb"].values > 0).astype(float)
        elif cat == "first_set_fav_wins": act = hold["fav_first_set"].values.astype(float)
        elif cat == "most_aces_fav": act = (fav_aces > dog_aces).astype(float)
        else: act = (hold["fav_games_won"].values > hold["dog_games_won"].values).astype(float)
        mk = np.isfinite(hold["n_sets"].values)
        if cat == "most_aces_fav": mk &= fav_aces != dog_aces
        if cat == "most_games_fav": mk &= hold["fav_games_won"].values != hold["dog_games_won"].values
        if cat == "goes_5_sets": mk &= hold["best_of"].values == 5
        binp[cat] = {"pred": float(pred[mk].mean()), "actual": float(act[mk].mean()), "brier": float(np.mean((pred[mk] - act[mk]) ** 2)), "n": int(mk.sum())}
        L.append("  %-14s pred %.3f  actual %.3f  Brier %.4f  (n=%d)" % (cat, binp[cat]["pred"], binp[cat]["actual"], binp[cat]["brier"], mk.sum()))
    # --- example distributions (last 3 holdout matches)
    L.append(""); L.append("== EXAMPLE DISTRIBUTIONS (real holdout matches) ==")
    for j in range(max(0, len(hold) - 3), len(hold)):
        r = hold.iloc[j]; s, hA, hB, _ = sims[j]
        L.append("  %s %s %s vs %s pinn %.2f frag %.0f | holds solved fav %.3f dog %.3f | sim P(fav) %.3f" % (r["date"], r["tourney_name"], r["fav_name"], r["dog_name"], r["pinn_fav"], r["fragility"], hA, hB, s["fav_wins"].mean()))
        for side, nm in (("fav", r["fav_name"]), ("dog", r["dog_name"])):
            L.append("    %s: " % nm + "  ".join("%s %.1f [%.0f-%.0f] act %s" % (c, s[side][c].mean(), np.percentile(s[side][c], 10), np.percentile(s[side][c], 90), "%.0f" % r["%s_%s" % (side, c)] if pd.notna(r.get("%s_%s" % (side, c), np.nan)) else "-") for c in ("aces", "dfs", "holds", "breaks", "games_won", "fantasy_score")))
        L.append("    match: TG %.1f [%.0f-%.0f] act %s | first set %.1f act %s | P(3 sets) %.2f | P(any TB) %.2f | sets act %s"
                 % (s["total_games"].mean(), np.percentile(s["total_games"], 10), np.percentile(s["total_games"], 90), "%.0f" % r["total_games"], s["first_set_games"].mean(), "%.0f" % r["first_set_games"] if pd.notna(r["first_set_games"]) else "-", s["goes_3_sets"].mean(), s["any_tiebreak"].mean(), r["score"]))
    return {"means": summary, "coverage": cov, "calibration": calib, "binary": binp}

def cmd_build(a):
    print(MARKER, "-- BUILD"); sys.stdout.flush()
    if os.path.exists(SIM_MODEL):
        with open(SIM_MODEL) as f:
            if json.load(f).get("frozen"): raise SystemExit("%s is FROZEN — --build would overwrite it; a new simulator is a new file (V2)" % SIM_MODEL)
    m = v1.load_model()
    print("frozen V1 model sha", m["_sha"][:16])
    if not os.path.exists(v1.ROWS_OUT): raise SystemExit("%s missing — V1 --build must have run" % v1.ROWS_OUT)
    df = pd.read_csv(v1.ROWS_OUT, low_memory=False)
    df = attach_scores(df, a.sackmann_dir)
    df = df[df["score"].notna() & df["n_sets"].notna()].reset_index(drop=True)
    _, hf0, hd0, mu0 = v1_inputs_from_rows(df, m)
    okmask = np.isfinite(hf0) & np.isfinite(hd0) & np.isfinite(mu0["fav"][0]) & np.isfinite(mu0["fav"][1]) & np.isfinite(mu0["dog"][0]) & np.isfinite(mu0["dog"][1])
    print("rows dropped for incomplete V1 inputs (no serve history on that surface): %d" % int((~okmask).sum()))
    df = df[okmask].reset_index(drop=True)
    print("rows with price+stats+score: %d (fit<=%d %d, tune %d, holdout %d)" % (len(df), FIT_END, (df["year"] <= FIT_END).sum(), ((df["year"] >= TUNE_START) & (df["year"] <= TUNE_END)).sum(), (df["year"] >= HOLD_START).sum()))
    L = [MARKER + " -- BUILD REPORT " + datetime.now().isoformat(), "SIM NOT FROZEN YET — run --freeze before any live projection", "V1 model sha %s" % m["_sha"]]
    coefs = fit_constants(df, m, L)
    tune_alpha(df, m, coefs, L)
    res = holdout_report(df, m, coefs, a.sims, L)
    coefs["holdout"] = res
    with open(SIM_MODEL, "w") as f: json.dump(coefs, f, indent=1, sort_keys=True)
    with open(SIM_REPORT, "w") as f: f.write("\n".join(L) + "\n")
    print("\n".join(L)); print("\nwrote %s (unfrozen), %s" % (SIM_MODEL, SIM_REPORT))

# ------------------------------------------------------------------ freeze / spec
def load_sim(require_frozen=True):
    if not os.path.exists(SIM_MODEL): raise SystemExit("%s missing — run --build first" % SIM_MODEL)
    with open(SIM_MODEL) as f: c = json.load(f)
    if require_frozen:
        if not c.get("frozen"): raise SystemExit("simulator is NOT frozen — run --freeze first (live projections refuse unfrozen models)")
        body = dict(c); st = body.pop("freeze")
        if sha256_text(json.dumps(body, sort_keys=True)) != st["sha"]: raise SystemExit("SIM MODEL TAMPERED — refusing")
        c["_sha"] = st["sha"]
    return c

def cmd_freeze(a):
    c = load_sim(require_frozen=False)
    if c.get("frozen"): raise SystemExit("already frozen at %s — refusing to freeze twice" % c["freeze"]["at"])
    c["frozen"] = True
    sha = sha256_text(json.dumps(dict(c), sort_keys=True))
    c["freeze"] = {"at": datetime.now().isoformat(), "sha": sha, "rule": "no refit/retune during forward test; V1 sha pinned"}
    with open(SIM_MODEL, "w") as f: json.dump(c, f, indent=1, sort_keys=True)
    print("FROZEN %s  sim sha %s" % (SIM_MODEL, sha)); print("file sha256 %s" % sha256_file(SIM_MODEL))

def cmd_spec(a):
    c = load_sim(); m = v1.load_model()
    if m["_sha"] != c["v1_sha"]: print("** WARNING: V1 model sha changed since the sim was built (%s vs %s)" % (m["_sha"][:12], c["v1_sha"][:12]))
    print(MARKER, "-- FROZEN SPEC  sim sha", c["_sha"]); print("frozen at", c["freeze"]["at"], "| built on V1", c["v1_sha"][:16])
    print("\nCONSTANTS"); print("  alpha (hold level: %.2f*rates + %.2f*price)" % (c["alpha"], 1 - c["alpha"]))
    print("  tiebreak k %.1f  -> P(fav wins TB) = sigmoid(k*(hold_fav - hold_dog))" % c["tb_k"])
    print("  gap spread sigma %.2f (per-match shift of the hold gap, 5-point quadrature) ; dispersion multiplier %g" % (c.get("gap_sigma", 0), c.get("disp_mult", 1)))
    print("  NB dispersion:", {s: {k: round(v, 2) for k, v in d.items()} for s, d in c["disp"].items()})
    print("  level_price: " + " + ".join("%.4f*%s" % (x, n) for x, n in zip(c["level_price"], m["struct_names"])))
    print("\nMATCH MODEL: alternating serve, hold/break per game, tiebreak at 6-6 (all sets), best of 3 or 5; tilt solved so P(fav wins)=pinn")
    print("\nSUPPORTED PROPS per player:", PLAYER_PROPS); print("SUPPORTED PROPS per match:", MATCH_PROPS)
    print("FANTASY SCORE: 10 + (games won - games lost) + 3*(sets won - sets lost) + 0.5*(aces - double faults)")
    print("  verified from the PrizePicks in-app tennis scoring chart, 2026-08-23. ASSUMPTION: +10 applies to any player who officially starts the match (retirement/walkover handling not published; see spec).")
    print("NOT MODELLED: break points faced/saved, service/return points won, minutes, exact set score (report-only), retirement")
    print("\nHOLDOUT %d+ coverage 50/80/90:" % HOLD_START)
    for k, v in c["holdout"]["coverage"].items(): print("  %-18s %.1f / %.1f / %.1f" % (k, 100 * v.get("50", v.get(50, 0)), 100 * v.get("80", v.get(80, 0)), 100 * v.get("90", v.get(90, 0))))
    print("\nLOG SCHEMA (%s): %s" % (SIM_LOG, ", ".join(LOG_FIELDS)))

# ------------------------------------------------------------------ live projection
def build_live_row(fav, dog, pinn, surface, bo5, wta, today, m, state):
    W, K = m["window"], m["shrink"]; feats = {}
    for side, nm in (("fav", fav), ("dog", dog)):
        ps, age, sw, stale = v1.live_features(state, nm, surface, today)
        rb = v1.rate_bundle(ps, surface, W, K)
        if rb is None: raise SystemExit("no serve-stat history for %s" % nm)
        feats[side] = (rb, age, sw, stale)
        if stale and stale > 90: print("note: %s last seen %d days ago (state ends %s)" % (nm, stale, state["latest_date"]))
    row = {"lp": v1.logit(pinn), "bo5": int(bo5), "is_wta": int(wta), "pinn_fav": pinn, "best_of": 5 if bo5 else 3,
           "fav_switch": feats["fav"][2], "dog_switch": feats["dog"][2]}
    for side in ("fav", "dog"):
        tag = "%s_W%d_K%d" % (side, W, K)
        for key in ("ace_rate", "df_rate", "hold_rate", "break_rate", "spg"): row[tag + "_" + key] = feats[side][0][key]
    row["elo_gap"] = (state["players"][fav]["elo"] - state["players"][dog]["elo"]) * math.log(10) / 400 - v1.logit(pinn)
    row["d_age"] = (feats["fav"][1] - feats["dog"][1]) if (feats["fav"][1] is not None and feats["dog"][1] is not None) else np.nan
    row["d_surface_switch"] = row["fav_switch"] - row["dog_switch"]
    raw = 0.0
    for f, c in m["frag_v3"].items():
        v = row[f] if not (isinstance(row[f], float) and math.isnan(row[f])) else c["mu"]
        raw += c["beta"] * (v - c["mu"]) / c["sd"]
    fr = v1.fragility_from_ref(raw, pinn, m["frag_ref"]); row["fragility"] = fr if fr is not None else 50.0
    return pd.DataFrame([row])

def cmd_project(a):
    c = load_sim(); m = v1.load_model()
    if m["_sha"] != c["v1_sha"]: raise SystemExit("V1 model sha changed since the sim was frozen — refusing")
    with open(v1.STATE_OUT, "rb") as f: state = pickle.load(f)
    today = date.fromisoformat(a.date) if a.date else date.today()
    fav = v1.resolve_name(a.project[0], state); dog = v1.resolve_name(a.project[1], state)
    pinn = float(a.pinn)
    if not (0.5 <= pinn < 1.0): raise SystemExit("--pinn must be the FAVORITE's de-vigged probability in [0.5, 1)")
    surface = a.surface.title(); bo = 5 if a.bo5 else 3
    df = build_live_row(fav, dog, pinn, surface, a.bo5, a.wta, today, m, state)
    P, hf, hd, mu = v1_inputs_from_rows(df, m)
    lv = level_from(hf[0], hd[0], c, price_level_pred(df, c)[0])
    t, flag = solve_tilt(lv, pinn, c["tb_k"], bo, sigma=c.get("gap_sigma", 0.0))
    hA = min(max(lv + t, 0.2), 0.995); hB = min(max(lv - t, 0.2), 0.995)
    dm = c.get("disp_mult", 1.0)
    s = simulate(hA, hB, c["tb_k"], bo, (mu["fav"][0][0], mu["fav"][1][0]), (mu["dog"][0][0], mu["dog"][1][0]),
                 (c["disp"]["fav"]["aces"] * dm, c["disp"]["fav"]["dfs"] * dm), (c["disp"]["dog"]["aces"] * dm, c["disp"]["dog"]["dfs"] * dm), a.sims, sigma=c.get("gap_sigma", 0.0))
    match_id = sha256_text("|".join([today.isoformat(), v1.norm_name(fav), v1.norm_name(dog)]))[:12]
    print("%s -- PROJECTION  match_id %s  %s  %s vs %s  %s  best-of-%d  pinn_fav %.3f  fragility %.0f  (sim %s / V1 %s)"
          % (MARKER, match_id, today, fav, dog, surface, bo, pinn, df["fragility"][0], c["_sha"][:12], m["_sha"][:12]))
    print("  hold probs solved: fav %.3f  dog %.3f  (level %.3f, tilt %+.3f, %s)   sim P(fav wins) %.3f" % (hA, hB, lv, t, flag, s["fav_wins"].mean()))
    rows = []
    def add(player, opp, is_fav, cat, x):
        q = {"mean": x.mean(), "sd": x.std(), "p10": np.percentile(x, 10), "p50": np.percentile(x, 50), "p90": np.percentile(x, 90)}
        rows.append({"ts": datetime.now().isoformat(timespec="seconds"), "match_id": match_id, "match_date": today.isoformat(), "player": player, "opponent": opp,
                     "is_fav": is_fav, "pinn_fav": "%.4f" % pinn, "surface": surface, "best_of": bo, "category": cat,
                     "mean": "%.3f" % q["mean"], "sd": "%.3f" % q["sd"], "p10": ("%.1f" if cat == "fantasy_score" else "%.0f") % q["p10"], "p50": ("%.1f" if cat == "fantasy_score" else "%.0f") % q["p50"], "p90": ("%.1f" if cat == "fantasy_score" else "%.0f") % q["p90"],
                     "fragility": "%.1f" % df["fragility"][0], "hold_fav": "%.4f" % hA, "hold_dog": "%.4f" % hB,
                     "book": "", "line": "", "p_over": "", "p_under": "", "actual": "", "settled_ts": "", "sim_sha": c["_sha"], "v1_sha": m["_sha"]})
        return q
    for side, nm, opp in (("fav", fav, dog), ("dog", dog, fav)):
        print("  %s %s" % (side, nm))
        for cat in PLAYER_PROPS:
            q = add(nm, opp, int(side == "fav"), cat, s[side][cat])
            print("      %-11s mean %5.2f  sd %4.2f  10-50-90: %2.0f-%2.0f-%2.0f" % (cat, q["mean"], q["sd"], q["p10"], q["p50"], q["p90"]))
    print("  match")
    for cat in MATCH_PROPS:
        q = add("match", fav + " v " + dog, -1, cat, s[cat])
        if cat in ("most_aces_fav", "most_games_fav"):
            x = s[cat]; print("      %-16s P(fav) %.3f  P(dog) %.3f  P(tie) %.3f" % (cat, np.mean(x == 1), np.mean(x == 0), np.mean(x == -1)))
        elif cat in ("goes_3_sets", "goes_5_sets", "any_tiebreak", "fav_wins", "first_set_fav_wins"):
            print("      %-16s P = %.3f" % (cat, q["mean"]))
        else:
            print("      %-16s mean %5.2f  sd %4.2f  10-50-90: %2.0f-%2.0f-%2.0f" % (cat, q["mean"], q["sd"], q["p10"], q["p50"], q["p90"]))
    existing = set()
    if os.path.exists(SIM_LOG):
        with open(SIM_LOG) as f:
            for r in csv.DictReader(f): existing.add((r["match_id"], r["player"], r["category"]))
    new = [r for r in rows if (r["match_id"], r["player"], r["category"]) not in existing]
    if new:
        wh = not os.path.exists(SIM_LOG)
        with open(SIM_LOG, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if wh: w.writeheader()
            w.writerows(new)
        # keep the raw sims for this match so --line can compute P(over) exactly later
        os.makedirs("sim_cache", exist_ok=True)
        with open(os.path.join("sim_cache", match_id + ".pkl"), "wb") as f: pickle.dump(s, f)
        print("  logged %d rows -> %s (sims cached for --line)" % (len(new), SIM_LOG))
    else:
        print("  duplicate: already logged for this match (no rewrite)")

def read_log():
    if not os.path.exists(SIM_LOG): raise SystemExit("no sim forward log yet")
    with open(SIM_LOG) as f: return list(csv.DictReader(f))
def write_log(rows):
    with open(SIM_LOG, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS); w.writeheader(); w.writerows(rows)

def cmd_line(a):
    match_id, player, cat, line, book = a.line
    rows = read_log(); hit = 0
    cache = os.path.join("sim_cache", match_id + ".pkl")
    if not os.path.exists(cache): raise SystemExit("no cached sims for %s — was it projected by this script?" % match_id)
    with open(cache, "rb") as f: s = pickle.load(f)
    for r in rows:
        if r["match_id"] == match_id and r["category"] == cat and (player == "match" and r["player"] == "match" or v1.norm_name(r["player"]) == v1.norm_name(player)):
            if r["line"]: print("line already attached (%s %s) — one line per row" % (r["book"], r["line"])); return
            x = s[cat] if r["player"] == "match" else s["fav" if r["is_fav"] == "1" else "dog"][cat]
            po, pu = p_over(x, float(line))
            r["book"] = book; r["line"] = "%.1f" % float(line); r["p_over"] = "%.3f" % po; r["p_under"] = "%.3f" % pu; hit += 1
            print("%s %s %s: mean %s vs %s %s -> P(over) %.3f  P(under) %.3f  (break-even 54.2%% on a 6-pick flex)" % (match_id, r["player"], cat, r["mean"], book, r["line"], po, pu))
    if not hit: raise SystemExit("no logged row for %s / %s / %s" % (match_id, player, cat))
    write_log(rows)

REMATCH_GAP_DAYS = 3   # APEX-REMATCH-2026-11-01: the same match can be listed a day or two apart

def _pair_key(r):
    names = r["opponent"].split(" v ") if r["player"] == "match" else [r["player"], r["opponent"]]
    return frozenset(v1.norm_name(x) for x in names)

def cmd_settle_sackmann(a):
    rows = read_log(); open_rows = [r for r in rows if not r["actual"]]
    if not open_rows: print("nothing to settle"); return
    df = v1.load_sackmann(a.sackmann_dir, year_from=int(min(r["match_date"][:4] for r in open_rows)), challengers=False)
    n = 0
    # APEX-REMATCH-2026-11-01: the year files only date the tournament, so a rematch could be
    # stamped with the last meeting's result. Never settle before the match day, and a pair
    # cannot meet twice in one tournament: ignore any tournament that began by their last meeting.
    today = pd.Timestamp(datetime.now().date()); meets = {}
    for r in rows:
        meets.setdefault(_pair_key(r), set()).add(r["match_date"])
    for r in open_rows:
        names = [v1.norm_name(x) for x in r["opponent"].split(" v ")] if r["player"] == "match" else [v1.norm_name(r["player"]), v1.norm_name(r["opponent"])]
        md = pd.Timestamp(r["match_date"])
        if md > today: continue
        older = [pd.Timestamp(d) for d in meets[_pair_key(r)] if (md - pd.Timestamp(d)).days >= REMATCH_GAP_DAYS]
        c = df[(df["tourney_date"] >= md - pd.Timedelta(days=21)) & (df["tourney_date"] <= md)]
        c = c[c["winner_name"].map(v1.norm_name).isin(names) & c["loser_name"].map(v1.norm_name).isin(names)]
        if older: c = c[c["tourney_date"] > max(older)]
        if len(c) != 1: continue
        c = c.iloc[0]; ps = parse_score(c["score"])
        if not ps or ps["retired"]: continue
        won = v1.norm_name(c["winner_name"]) == (names[0] if r["player"] != "match" else None)
        sets = ps["sets"]; tg = sum(a_ + b_ for a_, b_, _ in sets)
        if r["player"] == "match":
            fav_name = v1.norm_name(r["opponent"].split(" v ")[0]); fav_won = v1.norm_name(c["winner_name"]) == fav_name
            val = {"total_games": tg, "first_set_games": sets[0][0] + sets[0][1], "total_sets": len(sets), "tiebreaks": sum(s_[2] for s_ in sets),
                   "any_tiebreak": int(any(s_[2] for s_ in sets)), "goes_3_sets": int(len(sets) == 3 if int(r["best_of"]) == 3 else len(sets) >= 3),
                   "goes_5_sets": int(len(sets) == 5), "fav_wins": int(fav_won),
                   "first_set_fav_wins": int((sets[0][0] > sets[0][1]) == fav_won),
                   "total_aces": (c["w_ace"] + c["l_ace"]) if pd.notna(c["w_ace"]) else None,
                   "most_aces_fav": (None if pd.isna(c["w_ace"]) or c["w_ace"] == c["l_ace"] else int((c["w_ace"] > c["l_ace"]) == fav_won)),
                   "most_games_fav": (None if sum(a_ for a_, b_, _ in sets) == sum(b_ for a_, b_, _ in sets) else int((sum(a_ for a_, b_, _ in sets) > sum(b_ for a_, b_, _ in sets)) == fav_won))}.get(r["category"])
        else:
            p = "w_" if won else "l_"; o = "l_" if won else "w_"
            if pd.isna(c[p + "SvGms"]): continue
            holds = c[p + "SvGms"] - (c[p + "bpFaced"] - c[p + "bpSaved"]); breaks = c[o + "bpFaced"] - c[o + "bpSaved"]
            gw = sum((a_ if won else b_) for a_, b_, _ in sets); gl = tg - gw
            sw = sum(1 for a_, b_, _ in sets if (a_ > b_) == won); sl = len(sets) - sw
            fs = (10.0 + (gw - gl) + 3.0 * (sw - sl) + 0.5 * (float(c[p + "ace"]) - float(c[p + "df"]))) if pd.notna(c[p + "ace"]) and pd.notna(c[p + "df"]) else None
            val = {"aces": c[p + "ace"], "dfs": c[p + "df"], "holds": holds, "breaks": breaks, "svgms": c[p + "SvGms"], "games_won": gw, "games_lost": gl,
                   "sets_won": sw, "fantasy_score": fs}.get(r["category"])
        if val is None or (isinstance(val, float) and math.isnan(val)): continue
        r["actual"] = ("%.1f" if r["category"] == "fantasy_score" else "%.0f") % float(val); r["settled_ts"] = datetime.now().isoformat(timespec="seconds"); n += 1
    write_log(rows); print("settled %d rows" % n)

def cmd_report(a):
    rows = read_log(); settled = [r for r in rows if r["actual"]]
    print(MARKER, "-- FORWARD REPORT  rows %d  settled %d  sim SHAs %s" % (len(rows), len(settled), ", ".join(sorted(set(r["sim_sha"][:12] for r in rows)))))
    if not settled: return
    print("\nper category: n, MAE of mean, coverage of 10-90 band, and (if lines) hit rate of the favoured side + Brier")
    for cat in PLAYER_PROPS + MATCH_PROPS:
        s = [r for r in settled if r["category"] == cat]
        if not s: continue
        y = np.array([float(r["actual"]) for r in s]); mu = np.array([float(r["mean"]) for r in s])
        inb = np.mean([float(r["p10"]) <= float(r["actual"]) <= float(r["p90"]) for r in s])
        lined = [r for r in s if r["line"]]
        txt = "  %-16s n=%3d  MAE %.3f  in 10-90 band %.0f%%" % (cat, len(s), np.mean(np.abs(mu - y)), 100 * inb)
        if lined:
            hits = []; br = []
            for r in lined:
                po = float(r["p_over"]); ln = float(r["line"]); act = float(r["actual"])
                if act == ln: continue
                side_over = po >= 0.5; hits.append(side_over == (act > ln)); br.append((po - float(act > ln)) ** 2)
            if hits: txt += "  | lines n=%d hit %.1f%% Brier %.3f" % (len(hits), 100 * np.mean(hits), np.mean(br))
        print(txt)
    if len(settled) < 300: print("\n(fewer than 300 settled rows: descriptive only)")

# ------------------------------------------------------------------ selftest
def cmd_selftest(a):
    print(MARKER, "-- SELFTEST (engine exactness + guards; temp dir)")
    fails = []
    # 1. set table sums to 1 and symmetric players -> 0.5
    for f in (0, 1):
        tab = set_table(0.8, 0.8, 0.5, f); tot = sum(r[0] for r in tab)
        if abs(tot - 1) > 1e-9: fails.append("set table prob sum %.6f" % tot)
        pA = sum(r[0] for r in tab if r[1] == 1)
        if abs(pA - 0.5) > 1e-9: fails.append("symmetric set P(A)=%.6f" % pA)
        if abs(set_winprob(0.8, 0.8, 0.5, f) - pA) > 1e-9: fails.append("slim/full set DP disagree")
        tab2 = set_table(0.84, 0.71, 0.6, f); sm = set_summary(0.84, 0.71, 0.6, f)
        wA = sum(r[0] for r in tab2 if r[1] == 1)
        qw = sum(r[0] for r in tab2 if r[1] == 1 and r[9] == 0) / wA; ql = sum(r[0] for r in tab2 if r[1] == 0 and r[9] == 0) / (1 - wA)
        eg = sum(r[0] * (r[2] + r[3]) for r in tab2)
        if max(abs(wA - sm[0]), abs(qw - sm[1]), abs(ql - sm[2]), abs(eg - sm[3])) > 1e-9: fails.append("slim set summary != full table (f=%d)" % f)
        # service games bookkeeping: svA+svB = games excluding tiebreak
        for r in tab:
            if r[4] + r[6] != (r[2] + r[3] - r[8]): fails.append("svgms bookkeeping %s" % (r,)); break
            if r[5] > r[4] or r[7] > r[6]: fails.append("holds > service games"); break
    # 2. always-hold players -> every set 7-6 via tiebreak
    tab = set_table(1.0, 1.0, 0.5, 0)
    if not all(r[8] == 1 and r[2] + r[3] == 13 for r in tab): fails.append("hold-everything set not 7-6")
    # 3. match winprob symmetric and monotone
    if abs(match_winprob(0.8, 0.8, 0.5, 3) - 0.5) > 1e-9: fails.append("match P(A) symmetric != 0.5")
    if not match_winprob(0.85, 0.75, 0.6, 5) > match_winprob(0.85, 0.75, 0.6, 3) > 0.5: fails.append("bo5 should amplify the favorite")
    # 4. solver recovers the price
    for pinn, bo in ((0.55, 3), (0.72, 3), (0.91, 5)):
        t, flag = solve_tilt(0.80, pinn, 3.0, bo)
        hA, hB = 0.80 + t, 0.80 - t
        got = match_winprob(hA, hB, sigmoid(3.0 * (hA - hB)), bo)
        if abs(got - pinn) > 2e-3 or flag != "ok": fails.append("solver pinn %.2f bo%d -> %.4f (%s)" % (pinn, bo, got, flag))
    # 5. Monte Carlo agrees with exact expectations
    hA, hB = 0.86, 0.74; k = 3.0
    s = simulate(hA, hB, k, 3, (0.9, 0.3), (0.5, 0.4), (8.0, 4.0), (8.0, 4.0), 40000, seed=3)
    pw = match_winprob(hA, hB, sigmoid(k * (hA - hB)), 3); etg = exact_expected_tg(hA, hB, k, 3)
    if abs(s["fav_wins"].mean() - pw) > 0.01: fails.append("MC P(win) %.3f vs exact %.3f" % (s["fav_wins"].mean(), pw))
    s2 = simulate(hA, hB, k, 3, (0.9, 0.3), (0.5, 0.4), (8.0, 4.0), (8.0, 4.0), 40000, seed=4, sigma=0.06)
    pw2 = match_winprob_mix(hA, hB, k, 3, 0.06)
    if abs(s2["fav_wins"].mean() - pw2) > 0.01: fails.append("MC with gap spread P(win) %.3f vs exact mix %.3f" % (s2["fav_wins"].mean(), pw2))
    if not s2["goes_3_sets"].mean() < s["goes_3_sets"].mean(): fails.append("gap spread should reduce deciding sets")
    t2, fl2 = solve_tilt(0.80, 0.70, 3.0, 3, sigma=0.06)
    if abs(match_winprob_mix(0.80 + t2, 0.80 - t2, 3.0, 3, 0.06) - 0.70) > 2e-3: fails.append("solver with spread off")
    if abs(s["total_games"].mean() - etg) > 0.15: fails.append("MC E[TG] %.2f vs exact %.2f" % (s["total_games"].mean(), etg))
    if abs(s["fav"]["aces"].mean() - 0.9 * s["fav"]["svgms"].mean()) > 0.2: fails.append("aces mean off per-game rate")
    if not (s["fav"]["games_won"] + s["fav"]["games_lost"] == s["total_games"]).all(): fails.append("games identity broken")
    if not (s["fav"]["holds"] + s["dog"]["breaks"] == s["fav"]["svgms"]).all(): fails.append("holds+opp breaks != svgms")
    fs_check = 10.0 + (s["fav"]["games_won"] - s["fav"]["games_lost"]) + 3.0 * (s["fav"]["sets_won"] - s["dog"]["sets_won"]) + 0.5 * (s["fav"]["aces"] - s["fav"]["dfs"])
    if not np.allclose(fs_check, s["fav"]["fantasy_score"]): fails.append("fantasy score formula mismatch")
    if not np.allclose(s["fav"]["fantasy_score"] + s["dog"]["fantasy_score"], 20.0 + 0.5 * (s["fav"]["aces"] - s["fav"]["dfs"] + s["dog"]["aces"] - s["dog"]["dfs"])):
        fails.append("fantasy score pair identity broken")
    if not (s["total_aces"] == s["fav"]["aces"] + s["dog"]["aces"]).all(): fails.append("total aces identity")
    if abs(np.mean(s["first_set_fav_wins"]) - set_winprob(hA, hB, sigmoid(k * (hA - hB)), 0) * 0.5 - set_winprob(hA, hB, sigmoid(k * (hA - hB)), 1) * 0.5) > 0.01: fails.append("first set winner off exact")
    print("  exact engine checks: %s" % ("ok" if not fails else fails))
    # 6. freeze guards in a temp dir
    cwd = os.getcwd(); tmp = tempfile.mkdtemp(prefix="apex_sim_"); os.chdir(tmp)
    try:
        with open(SIM_MODEL, "w") as f: json.dump({"marker": MARKER, "frozen": False, "alpha": 1.0, "tb_k": 3.0, "disp": {}, "level_price": [0] * 6, "v1_sha": "x", "holdout": {"coverage": {}}}, f)
        try: load_sim(); fails.append("unfrozen sim accepted")
        except SystemExit: pass
        cmd_freeze(a)
        try: cmd_freeze(a); fails.append("double freeze accepted")
        except SystemExit: pass
        with open(SIM_MODEL) as f: c = json.load(f)
        c["tb_k"] = 9.0
        with open(SIM_MODEL, "w") as f: json.dump(c, f)
        try: load_sim(); fails.append("tampered sim accepted")
        except SystemExit as e: print("  tamper check:", e)
    finally:
        os.chdir(cwd)
    print("SELFTEST", "PASS" if not fails else "FAIL: " + "; ".join(fails))
    if fails: sys.exit(1)

# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=MARKER)
    ap.add_argument("--selftest", action="store_true"); ap.add_argument("--build", action="store_true")
    ap.add_argument("--freeze", action="store_true"); ap.add_argument("--spec", action="store_true")
    ap.add_argument("--project", nargs=2, metavar=("FAV", "DOG")); ap.add_argument("--pinn", type=float)
    ap.add_argument("--surface", default="Hard"); ap.add_argument("--bo5", action="store_true"); ap.add_argument("--wta", action="store_true")
    ap.add_argument("--date", default=None); ap.add_argument("--sims", type=int, default=2000)
    ap.add_argument("--line", nargs=5, metavar=("MATCH_ID", "PLAYER_or_match", "CATEGORY", "LINE", "BOOK"))
    ap.add_argument("--settle-sackmann", action="store_true"); ap.add_argument("--report", action="store_true")
    ap.add_argument("--sackmann-dir", default="ATP & WTA, 1968-2026")
    a = ap.parse_args()
    if a.selftest: cmd_selftest(a)
    elif a.build: cmd_build(a)
    elif a.freeze: cmd_freeze(a)
    elif a.spec: cmd_spec(a)
    elif a.project:
        if a.pinn is None: raise SystemExit("--project needs --pinn")
        if a.sims < 2000: a.sims = 20000
        cmd_project(a)
    elif a.line: cmd_line(a)
    elif a.settle_sackmann: cmd_settle_sackmann(a)
    elif a.report: cmd_report(a)
    else: ap.print_help()

if __name__ == "__main__":
    main()
