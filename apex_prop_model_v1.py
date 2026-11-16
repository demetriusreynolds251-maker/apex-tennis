#!/usr/bin/env python3
"""
APEX-PROP-MODEL-V1-2026-08-22
Project Apex Intelligence / OpenClaw (DEMETRIUS)

Prop Model V1: market foundation + surface-specific player rates + small
replicated overlays (Fragility V3, elo-gap, surface switch, age gap).
Process: build -> freeze -> forward log -> validate on unseen matches.

Python 3.9 compatible. Depends only on numpy + pandas.

COMMANDS (run in the project folder):
  --selftest                       synthetic end-to-end test (temp dir, touches nothing)
  --build  [--sackmann-dir D] [--rows apex_batch_a_rows.csv] [--prob-col pinn_a]
           [--fragility-model fragility_v3_model.json]
           replay Sackmann 2005+ (no challengers), surface-specific rates, join
           Pinnacle price, compute frozen-V3 fragility, fit/tune/holdout, write
           apex_prop_rows_v1.csv, apex_prop_model_v1.json (UNFROZEN), apex_prop_state_v1.pkl,
           apex_prop_report_v1.txt
  --refresh-state [--sackmann-dir D]   after a Sackmann refresh: rebuild player state only (frozen model untouched)
  --freeze                         one-time: stamps the model json as frozen + SHA; refuses twice
  --spec                           print frozen feature list, formulas, coefficients, schema, SHA
  --project "Fav Name" "Dog Name" --pinn 0.64 --surface Hard [--bo5] [--wta] [--tour T --round R]
                                   projections for both players; logs them (frozen model only)
  --line MATCH_ID PLAYER CATEGORY LINE BOOK [--odds-over O --odds-under U]
                                   attach a market line to a logged projection -> edge
  --settle MATCH_ID PLAYER --aces N --dfs N --holds N --breaks N --svgms N
  --settle-sackmann [--sackmann-dir D]   fill outcomes from the current-year Sackmann files
  --report                         MAE / RMSE / hit rate / calibration / edge buckets / by category
  --check-elo                      compare replay Elo to apex_batch_a_rows.csv d_elo (sanity)
  --inspect                        show columns detected in the inputs, no modelling

CATEGORY names: aces, dfs, holds, breaks, svgms
"""
import warnings
warnings.filterwarnings("ignore")
import argparse, csv, glob, hashlib, json, math, os, pickle, re, sys, tempfile, time, unicodedata
from collections import deque, defaultdict
from datetime import datetime, date

import numpy as np
import pandas as pd

MARKER = "APEX-PROP-MODEL-V1-2026-08-22"
ROWS_OUT = "apex_prop_rows_v1.csv"
MODEL_OUT = "apex_prop_model_v1.json"
STATE_OUT = "apex_prop_state_v1.pkl"
REPORT_OUT = "apex_prop_report_v1.txt"
LOG_OUT = "apex_prop_forward_log_v1.csv"
FRAG_SHA_PREFIX = "8bf6937a"   # frozen fragility_v3_model.json

# Frozen Fragility V3 (from fragility_live --spec, 2026-08-20). Never edit.
FRAG_V3 = {
    "elo_gap":          {"beta": 0.02556, "mu": -0.04835, "sd": 0.55939},
    "d_surface_switch": {"beta": 0.00859, "mu": -0.01120, "sd": 0.33921},
    "d_age":            {"beta": 0.03447, "mu": -0.17957, "sd": 5.63055},
}
PRICE_BANDS = [(0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 1.01)]

CATEGORIES = ["aces", "dfs", "holds", "breaks", "svgms"]
OVERLAY_NAMES = ["z_frag", "z_elo_gap", "switch_self", "z_age_gap"]
LOG_FIELDS = ["ts", "match_id", "match_date", "player", "opponent", "is_fav", "pinn_fav", "surface",
              "category", "projection", "fragility", "book", "line", "odds_over", "odds_under",
              "edge", "actual", "settled_ts", "model_sha"]

FIT_END, TUNE_START, TUNE_END, HOLD_START = 2021, 2022, 2023, 2024
WINDOW_GRID = [20, 40, 80]
SHRINK_GRID = [5, 15, 40]

# ------------------------------------------------------------------ helpers
def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))

def k_factor(n):
    return 250.0 / ((n + 5) ** 0.4)

def norm_name(s):
    return re.sub(r"[^a-z0-9 ]", "", str(s).lower()).strip()

def tokens(s):
    return set(norm_name(s).split())

def parse_games(score):
    """Total games from a Sackmann score string. Returns (games, retired_flag) or (None, True)."""
    if not isinstance(score, str) or not score.strip():
        return None, True
    s = score.strip()
    ret = any(t in s.upper() for t in ("RET", "W/O", "DEF", "ABN", "WALKOVER"))
    g = 0
    for tok in s.split():
        m = re.match(r"^(\d+)-(\d+)", tok)
        if m:
            g += int(m.group(1)) + int(m.group(2))
    if g == 0:
        return None, True
    return g, ret

def ols(X, y, names=None):
    """OLS with HC1 robust t-stats. X includes intercept column."""
    X = np.asarray(X, float); y = np.asarray(y, float)
    n, k = X.shape
    XtX_inv = np.linalg.pinv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    resid = y - X @ beta
    meat = (X * resid[:, None]).T @ (X * resid[:, None])
    cov = XtX_inv @ meat @ XtX_inv * n / max(n - k, 1)
    se = np.sqrt(np.maximum(np.diag(cov), 1e-12))
    return beta, beta / se, resid

def resolve_name(name, state):
    idx = state["name_index"]
    key = norm_name(name)
    if key in idx:
        return idx[key]
    toks = tokens(name)
    cands = [full for k2, full in idx.items() if toks <= set(k2.split())]
    cands = sorted(set(cands))
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        raise SystemExit("ambiguous player name %r -> %s" % (name, cands[:8]))
    raise SystemExit("unknown player %r (not in replay state)" % name)

# ------------------------------------------------------------------ Sackmann loading
def load_sackmann(d, year_from=2005, challengers=False):
    files = sorted(glob.glob(os.path.join(d, "*.csv")))
    frames = []
    for f in files:
        base = os.path.basename(f)
        m = re.match(r"^(\d{4})(_wta|_challenger)?\.csv$", base)
        if not m:
            continue
        yr = int(m.group(1)); kind = m.group(2) or ""
        if yr < year_from:
            continue
        if kind == "_challenger" and not challengers:
            continue
        df = pd.read_csv(f, low_memory=False)
        df["is_wta"] = 1 if kind == "_wta" else 0
        df["src_file"] = base
        frames.append(df)
    if not frames:
        raise SystemExit("no Sackmann files found in %r" % d)
    df = pd.concat(frames, ignore_index=True)
    df = _join_name_variants(df)   # APEX-NAMEMATCH-2026-11-02
    df["tourney_date"] = pd.to_datetime(df["tourney_date"].astype(str), format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["tourney_date", "winner_name", "loser_name"])
    if "match_num" not in df.columns:
        df["match_num"] = 0
    df["match_num"] = pd.to_numeric(df["match_num"], errors="coerce").fillna(0)
    df = df.sort_values(["tourney_date", "tourney_id", "match_num"], kind="stable").reset_index(drop=True)
    for c in ["w_ace", "w_df", "w_svpt", "w_SvGms", "w_bpSaved", "w_bpFaced",
              "l_ace", "l_df", "l_svpt", "l_SvGms", "l_bpSaved", "l_bpFaced",
              "winner_age", "loser_age", "best_of", "minutes"]:
        if c not in df.columns:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["surface"] = df["surface"].fillna("Hard").astype(str).str.strip().str.title()
    return df

# APEX-NAMEMATCH-2026-11-02 (owner, "Go both fixes correctly and properly"): one player filed under two
# spellings got two short histories (McCartney Kessler 51 + 54 matches). Spellings that differ only by capitals,
# spaces, accents, hyphens or apostrophes are joined under the most-used one; middle-name variants only when
# checked by hand (a bare subset rule would join Maria Sanchez with Maria Jose Martinez Sanchez, two players).
NAME_ALIASES = {
    "Beatriz Maia": "Beatriz Haddad Maia",
    "Tiantsoa Sarah Rakotomanga Rajaonah": "Tiantsoa Rakotomanga Rajaonah",
    "Karim Maamoun": "Karim Mohamed Maamoun",
    "Anca Todoni": "Anca Alexia Todoni",
}


def _name_key(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    s = s.replace("-", " ").replace("'", "").replace(".", " ")
    return " ".join(re.sub(r"[^a-z ]", " ", s).split())


def _join_name_variants(df):
    try:
        cnt = pd.concat([df["winner_name"], df["loser_name"]]).dropna().astype(str).value_counts()
        best = {}
        for nm, n in cnt.items():
            k = _name_key(nm)
            if k not in best or n > cnt[best[k]]:
                best[k] = nm
        canon = {nm: " ".join(best[_name_key(nm)].split()) for nm in cnt.index}
        for a, b in NAME_ALIASES.items():
            for nm in cnt.index:
                if _name_key(nm) == _name_key(a):
                    canon[nm] = b
                if _name_key(nm) == _name_key(b):
                    canon[nm] = b
        for col in ("winner_name", "loser_name"):
            df[col] = df[col].map(lambda x: canon.get(x, x) if isinstance(x, str) else x)
    except Exception as e:
        print("name join skipped (%s)" % type(e).__name__)
    return df


# ------------------------------------------------------------------ player state
SURFACES = ("Hard", "Clay", "Grass", "Carpet")
REC_FIELDS = ("ace", "df", "svpt", "svgms", "bpf", "bps", "osvgms", "obpf", "obps")

class PState:
    __slots__ = ("name", "elo", "n", "last_date", "last_surface", "last_age", "last_age_date", "hist")
    def __init__(self, name):
        self.name = name; self.elo = 1500.0; self.n = 0
        self.last_date = None; self.last_surface = None
        self.last_age = None; self.last_age_date = None
        self.hist = {s: deque(maxlen=max(WINDOW_GRID)) for s in SURFACES + ("All",)}

def rate_bundle(ps, surface, window, shrink):
    """Surface-specific rates shrunk toward all-surface rates. Returns dict or None if no data."""
    def sums(dq, w):
        out = dict((f, 0.0) for f in REC_FIELDS); cnt = 0
        for rec in list(dq)[-w:]:
            for i, f in enumerate(REC_FIELDS):
                out[f] += rec[i]
            cnt += 1
        out["n"] = cnt
        return out
    a = sums(ps.hist["All"], window)
    s = sums(ps.hist.get(surface, ps.hist["All"]), window)
    if a["n"] == 0 or a["svpt"] <= 0 or a["svgms"] <= 0:
        return None
    def rate(num_s, den_s, num_a, den_a):
        ra = num_a / den_a if den_a > 0 else 0.0
        ns = s["n"]
        rs = num_s / den_s if den_s > 0 else ra
        return (ns * rs + shrink * ra) / (ns + shrink)
    holds_s = s["svgms"] - (s["bpf"] - s["bps"]); holds_a = a["svgms"] - (a["bpf"] - a["bps"])
    brk_s = s["obpf"] - s["obps"]; brk_a = a["obpf"] - a["obps"]
    return {
        "ace_rate":   rate(s["ace"], s["svpt"], a["ace"], a["svpt"]),
        "df_rate":    rate(s["df"], s["svpt"], a["df"], a["svpt"]),
        "hold_rate":  rate(holds_s, s["svgms"], holds_a, a["svgms"]),
        "break_rate": rate(brk_s, s["osvgms"], brk_a, a["osvgms"]),
        "spg":        rate(s["svpt"], s["svgms"], a["svpt"], a["svgms"]),
        "n_surf": s["n"], "n_all": a["n"],
    }

def replay(df, grids=None, collect_rows=True, progress=True):
    """Replay main-tour matches in order. Returns (rows list, state dict)."""
    players = {}
    rows = []
    grids = grids or [(w, k) for w in WINDOW_GRID for k in SHRINK_GRID]
    t0 = time.time()
    for i, r in enumerate(df.itertuples(index=False)):
        if progress and i and i % 50000 == 0:
            print("  ... %d (%.0fs)" % (i, time.time() - t0)); sys.stdout.flush()
        wn, ln = r.winner_name, r.loser_name
        pw = players.get(wn) or players.setdefault(wn, PState(wn))
        pl = players.get(ln) or players.setdefault(ln, PState(ln))
        d = r.tourney_date.date(); surf = r.surface
        games, retired = parse_games(r.score)
        stats_ok = all(not (isinstance(v, float) and math.isnan(v)) for v in
                       (r.w_ace, r.w_df, r.w_svpt, r.w_SvGms, r.w_bpSaved, r.w_bpFaced,
                        r.l_ace, r.l_df, r.l_svpt, r.l_SvGms, r.l_bpSaved, r.l_bpFaced)) \
            and r.w_svpt > 0 and r.l_svpt > 0 and r.w_SvGms > 0 and r.l_SvGms > 0
        if collect_rows:
            row = {
                "date": d.isoformat(), "year": d.year, "tourney_id": str(r.tourney_id), "tourney_name": str(r.tourney_name),
                "round": str(r.round), "surface": surf, "is_wta": int(r.is_wta),
                "best_of": int(r.best_of) if not math.isnan(r.best_of) else (3 if r.is_wta else 3),
                "winner": wn, "loser": ln, "retired": int(retired), "total_games": games if games else np.nan,
                "w_elo": pw.elo, "l_elo": pl.elo, "w_n": pw.n, "l_n": pl.n,
                "w_age": r.winner_age, "l_age": r.loser_age,
                "w_switch": int(pw.last_surface is not None and pw.last_surface != surf),
                "l_switch": int(pl.last_surface is not None and pl.last_surface != surf),
                "w_aces": r.w_ace, "w_dfs": r.w_df, "w_svgms": r.w_SvGms, "w_svpt": r.w_svpt,
                "w_holds": (r.w_SvGms - (r.w_bpFaced - r.w_bpSaved)) if stats_ok else np.nan,
                "w_breaks": (r.l_bpFaced - r.l_bpSaved) if stats_ok else np.nan,
                "l_aces": r.l_ace, "l_dfs": r.l_df, "l_svgms": r.l_SvGms, "l_svpt": r.l_svpt,
                "l_holds": (r.l_SvGms - (r.l_bpFaced - r.l_bpSaved)) if stats_ok else np.nan,
                "l_breaks": (r.w_bpFaced - r.w_bpSaved) if stats_ok else np.nan,
                "stats_ok": int(stats_ok),
            }
            for (w, k) in grids:
                for side, ps in (("w", pw), ("l", pl)):
                    rb = rate_bundle(ps, surf, w, k)
                    tag = "%s_W%d_K%d" % (side, w, k)
                    if rb:
                        for key in ("ace_rate", "df_rate", "hold_rate", "break_rate", "spg"):
                            row["%s_%s" % (tag, key)] = rb[key]
                        row["%s_nsurf" % tag] = rb["n_surf"]; row["%s_nall" % tag] = rb["n_all"]
                    else:
                        for key in ("ace_rate", "df_rate", "hold_rate", "break_rate", "spg", "nsurf", "nall"):
                            row["%s_%s" % (tag, key)] = np.nan
            rows.append(row)
        # --- update state (mirrors acm: Elo k uses pre-match n)
        ew = 1.0 / (1.0 + 10 ** ((pl.elo - pw.elo) / 400.0))
        kw, kl = k_factor(pw.n), k_factor(pl.n)
        pw.elo += kw * (1 - ew); pl.elo += kl * (0 - (1 - ew))
        pw.n += 1; pl.n += 1
        for ps, age in ((pw, r.winner_age), (pl, r.loser_age)):
            ps.last_date = d; ps.last_surface = surf
            if not (isinstance(age, float) and math.isnan(age)):
                ps.last_age = float(age); ps.last_age_date = d
        if stats_ok:
            recw = (r.w_ace, r.w_df, r.w_svpt, r.w_SvGms, r.w_bpFaced, r.w_bpSaved, r.l_SvGms, r.l_bpFaced, r.l_bpSaved)
            recl = (r.l_ace, r.l_df, r.l_svpt, r.l_SvGms, r.l_bpFaced, r.l_bpSaved, r.w_SvGms, r.w_bpFaced, r.w_bpSaved)
            for ps, rec in ((pw, recw), (pl, recl)):
                ps.hist["All"].append(rec)
                ps.hist.setdefault(surf, deque(maxlen=max(WINDOW_GRID))).append(rec)
    state = {
        "marker": MARKER, "built_at": datetime.now().isoformat(), "n_matches": len(df),
        "latest_date": df["tourney_date"].max().date().isoformat(),
        "players": {n: {"elo": p.elo, "n": p.n, "last_date": p.last_date, "last_surface": p.last_surface,
                        "last_age": p.last_age, "last_age_date": p.last_age_date,
                        "hist": {s: list(dq) for s, dq in p.hist.items()}} for n, p in players.items()},
        "name_index": {norm_name(n): n for n in players},
    }
    return rows, state

# ------------------------------------------------------------------ price join
def join_price(rows_df, batch_path, prob_col):
    b = pd.read_csv(batch_path, low_memory=False)
    cols = {c.lower(): c for c in b.columns}
    dcol = cols.get("date") or cols.get("match_date") or cols.get("tourney_date")
    acol = cols.get("player_a") or cols.get("a") or cols.get("fav")
    bcol = cols.get("player_b") or cols.get("b") or cols.get("dog")
    if prob_col not in b.columns:
        raise SystemExit("prob column %r not in %s; columns: %s" % (prob_col, batch_path, list(b.columns)[:40]))
    if not (dcol and acol and bcol):
        raise SystemExit("could not detect date/player columns in %s; columns: %s" % (batch_path, list(b.columns)[:40]))
    b["_d"] = pd.to_datetime(b[dcol].astype(str), errors="coerce").dt.date.astype(str)
    b["_a"] = b[acol].map(norm_name); b["_b"] = b[bcol].map(norm_name)
    b["_key"] = b["_d"] + "|" + b[["_a", "_b"]].apply(lambda x: "|".join(sorted(x)), axis=1)
    price = {}
    for k, a, p in zip(b["_key"], b["_a"], b[prob_col]):
        if pd.notna(p):
            price[k] = (a, float(p))
    rows_df["_w"] = rows_df["winner"].map(norm_name); rows_df["_l"] = rows_df["loser"].map(norm_name)
    rows_df["_key"] = rows_df["date"] + "|" + rows_df[["_w", "_l"]].apply(lambda x: "|".join(sorted(x)), axis=1)
    pw = []
    for k, w in zip(rows_df["_key"], rows_df["_w"]):
        hit = price.get(k)
        if hit is None:
            pw.append(np.nan)
        else:
            a, p = hit
            pw.append(p if a == w else 1 - p)
    rows_df["p_winner"] = pw
    rows_df.drop(columns=["_w", "_l", "_key"], inplace=True)
    return rows_df, b

def fav_frame(df):
    """Re-express each row in favorite/underdog frame using the Pinnacle price."""
    df = df[df["p_winner"].notna()].copy()
    fav_is_w = df["p_winner"] >= 0.5
    df["pinn_fav"] = np.where(fav_is_w, df["p_winner"], 1 - df["p_winner"])
    df["fav_won"] = fav_is_w.astype(int)
    for col in ["elo", "age", "switch", "aces", "dfs", "svgms", "svpt", "holds", "breaks", "n"]:
        w = df["w_%s" % col]; l = df["l_%s" % col]
        df["fav_%s" % col] = np.where(fav_is_w, w, l); df["dog_%s" % col] = np.where(fav_is_w, l, w)
    df["fav_name"] = np.where(fav_is_w, df["winner"], df["loser"])
    df["dog_name"] = np.where(fav_is_w, df["loser"], df["winner"])
    rate_cols = [c for c in df.columns if re.match(r"^[wl]_W\d+_K\d+_", c)]
    for c in rate_cols:
        if c.startswith("w_"):
            lc = "l_" + c[2:]
            df["fav_" + c[2:]] = np.where(fav_is_w, df[c], df[lc]); df["dog_" + c[2:]] = np.where(fav_is_w, df[lc], df[c])
    df.drop(columns=rate_cols, inplace=True)
    df = df.copy()
    df["elo_gap"] = (df["fav_elo"] - df["dog_elo"]) * math.log(10) / 400.0 - df["pinn_fav"].map(logit)
    df["d_surface_switch"] = df["fav_switch"] - df["dog_switch"]
    df["d_age"] = df["fav_age"] - df["dog_age"]
    df["lp"] = df["pinn_fav"].map(logit)
    df["bo5"] = (df["best_of"] == 5).astype(int)
    return df

def fragility_scores(df):
    """Frozen V3 weighted sum -> within-price-band percentile vs <=2023 favorites (this script's reference)."""
    z = np.zeros(len(df))
    for f, c in FRAG_V3.items():
        z += c["beta"] * (df[f].fillna(c["mu"]).values - c["mu"]) / c["sd"]
    df["frag_raw"] = z
    ref = {}
    score = np.full(len(df), np.nan)
    ref_mask = (df["year"] <= 2023).values
    band_idx = np.full(len(df), -1)
    for bi, (lo, hi) in enumerate(PRICE_BANDS):
        m = ((df["pinn_fav"] >= lo) & (df["pinn_fav"] < hi)).values
        band_idx[m] = bi
        refv = np.sort(z[m & ref_mask])
        if len(refv) < 50:
            refv = np.sort(z[ref_mask])
        ref[bi] = refv
        score[m] = np.searchsorted(refv, z[m], side="right") / len(refv) * 100.0
    df["fragility"] = np.clip(score, 0, 100)
    return df, {str(k): [float(np.quantile(v, q)) for q in np.linspace(0, 1, 101)] for k, v in ref.items()}

def fragility_from_ref(raw, pinn, ref):
    for bi, (lo, hi) in enumerate(PRICE_BANDS):
        if lo <= pinn < hi:
            q = ref[str(bi)]
            return float(np.clip(np.searchsorted(q, raw, side="right"), 0, 100))
    return None

# ------------------------------------------------------------------ modelling
def design_struct(df):
    lp = np.asarray(df["lp"], float); bo5 = np.asarray(df["bo5"], float); wta = np.asarray(df["is_wta"], float)
    return np.column_stack([np.ones(len(df)), lp, lp ** 2, bo5, wta, bo5 * lp])

def base_projection(df, W, K, coefs, side):
    """Layer 1+2: market structure x player rates -> base expectations for one side."""
    tg = design_struct(df) @ np.array(coefs["total_games"])
    svg = np.array(coefs["svgms"][side])[0] + np.array(coefs["svgms"][side])[1] * tg
    opp = "dog" if side == "fav" else "fav"
    tag = "%s_W%d_K%d" % (side, W, K); otag = "%s_W%d_K%d" % (opp, W, K)
    V = lambda c: np.asarray(df[c], float)
    ace_r = V(tag + "_ace_rate"); df_r = V(tag + "_df_rate"); spg = V(tag + "_spg")
    hold_r = V(tag + "_hold_rate"); brk_r = V(tag + "_break_rate")
    ohold = V(otag + "_hold_rate"); obrk = V(otag + "_break_rate")
    mb = coefs["mean_break_rate"]; mh = coefs["mean_hold_rate"]
    out = {
        "svgms": svg, "tg": tg,
        "aces": svg * spg * ace_r,
        "dfs": svg * spg * df_r,
        "holds": svg * (hold_r - (obrk - mb)),
        "breaks": (np.array(coefs["svgms"][opp])[0] + np.array(coefs["svgms"][opp])[1] * tg) * (brk_r + ((1 - ohold) - (1 - mh))),
    }
    return out

def overlay_design(df, side, coefs):
    """Overlay regressors in the side's own frame. Returns (X without intercept, names)."""
    sgn = 1.0 if side == "fav" else -1.0
    zf = (df["fragility"].values - 50.0) / 28.9        # match-level score, same sign in both frames (~1 SD units)
    ze_raw = (df["elo_gap"].values - coefs["mu_elo_gap"]) / coefs["sd_elo_gap"]
    za_raw = (df["d_age"].values - coefs["mu_d_age"]) / coefs["sd_d_age"]
    # elo_gap and age gap are INSIDE the fragility score -> keep only the part not explained by it
    # (residualise in the fav frame, THEN flip to the player's own frame)
    ze = sgn * (ze_raw - coefs.get("ortho_elo_on_frag", 0.0) * zf)
    za = sgn * (za_raw - coefs.get("ortho_age_on_frag", 0.0) * zf)
    sw = df["%s_switch" % side].values.astype(float)
    return np.column_stack([zf, ze, sw, za])

def fit_model(df, verbose=True):
    """Fit structure (<=2021), tune window/shrink (2022-23), fit overlays (<=2021) with replication
    gate on 2022-23, report holdout 2024+. Returns (coefs, report_lines)."""
    L = []
    fit = df[(df["year"] <= FIT_END)]; tune = df[(df["year"] >= TUNE_START) & (df["year"] <= TUNE_END)]
    hold = df[df["year"] >= HOLD_START]
    L.append("rows priced+finished+stats: %d | fit<=%d: %d | tune %d-%d: %d | holdout %d+: %d"
             % (len(df), FIT_END, len(fit), TUNE_START, TUNE_END, len(tune), HOLD_START, len(hold)))
    coefs = {"marker": MARKER, "frozen": False}
    # ---- structure: total games & service games from price
    b, t, _ = ols(design_struct(fit), fit["total_games"])
    coefs["total_games"] = b.tolist(); coefs["total_games_t"] = t.tolist()
    coefs["struct_names"] = ["1", "logit(p)", "logit(p)^2", "bo5", "wta", "bo5*logit(p)"]
    tg_fit = design_struct(fit) @ b
    coefs["svgms"] = {}
    for side in ("fav", "dog"):
        bb, tt, _ = ols(np.column_stack([np.ones(len(fit)), tg_fit]), fit["%s_svgms" % side])
        coefs["svgms"][side] = bb.tolist()
    # league-average rates (for the price-only baseline + hold/break centring), from fit years
    allW = max(WINDOW_GRID); allK = SHRINK_GRID[0]
    coefs["mean_hold_rate"] = float(np.nanmean(np.r_[fit["fav_W%d_K%d_hold_rate" % (allW, allK)], fit["dog_W%d_K%d_hold_rate" % (allW, allK)]]))
    coefs["mean_break_rate"] = float(np.nanmean(np.r_[fit["fav_W%d_K%d_break_rate" % (allW, allK)], fit["dog_W%d_K%d_break_rate" % (allW, allK)]]))
    for key in ("ace_rate", "df_rate", "spg"):
        coefs["mean_" + key] = float(np.nanmean(np.r_[fit["fav_W%d_K%d_%s" % (allW, allK, key)], fit["dog_W%d_K%d_%s" % (allW, allK, key)]]))
    coefs["mu_elo_gap"] = float(fit["elo_gap"].mean()); coefs["sd_elo_gap"] = float(fit["elo_gap"].std())
    coefs["mu_d_age"] = float(fit["d_age"].mean()); coefs["sd_d_age"] = float(fit["d_age"].std())
    zf0 = (fit["fragility"].values - 50.0) / 28.9
    ze0 = (fit["elo_gap"].values - coefs["mu_elo_gap"]) / coefs["sd_elo_gap"]
    za0 = ((fit["d_age"].values - coefs["mu_d_age"]) / coefs["sd_d_age"])
    ok0 = np.isfinite(zf0) & np.isfinite(ze0) & np.isfinite(za0)
    coefs["ortho_elo_on_frag"] = float(np.dot(zf0[ok0], ze0[ok0]) / np.dot(zf0[ok0], zf0[ok0]))
    coefs["ortho_age_on_frag"] = float(np.dot(zf0[ok0], za0[ok0]) / np.dot(zf0[ok0], zf0[ok0]))
    L.append("overlay orthogonalisation: elo_gap z = raw - %.3f*frag z ; age z = raw - %.3f*frag z (fragility keeps the shared part)" % (coefs["ortho_elo_on_frag"], coefs["ortho_age_on_frag"]))
    L.append("structure: total_games = " + " + ".join("%.4f*%s" % (c, n) for c, n in zip(b, coefs["struct_names"])))
    L.append("           svgms_fav = %.3f + %.4f*E[TG]; svgms_dog = %.3f + %.4f*E[TG]"
             % tuple(coefs["svgms"]["fav"] + coefs["svgms"]["dog"]))
    # ---- tune window & shrink on 2022-23 (MAE of aces+dfs, both sides)
    best = None
    L.append("tuning (2022-23 MAE aces / dfs, both sides):")
    for W in WINDOW_GRID:
        for K in SHRINK_GRID:
            maes = []
            for side in ("fav", "dog"):
                bp = base_projection(tune, W, K, coefs, side)
                m = tune["%s_aces" % side].notna() & np.isfinite(bp["aces"])
                maes.append(np.nanmean(np.abs(bp["aces"][m] - tune["%s_aces" % side][m])))
                maes.append(np.nanmean(np.abs(bp["dfs"][m] - tune["%s_dfs" % side][m])))
            score = float(np.mean(maes))
            L.append("  W=%2d K=%2d  aces %.3f/%.3f  dfs %.3f/%.3f  -> %.4f" % (W, K, maes[0], maes[2], maes[1], maes[3], score))
            if best is None or score < best[0]:
                best = (score, W, K)
    coefs["window"], coefs["shrink"] = best[1], best[2]
    L.append("chosen window=%d shrink=%d" % (best[1], best[2]))
    W, K = best[1], best[2]
    # ---- calibration of base (scale/offset per category, fit years) then overlays
    coefs["calib"] = {}; coefs["overlays"] = {}
    L.append("overlay gate: fit<=%d |t|>=3 AND tune %d-%d same sign |t|>=1.5 (pre-registered); fail -> coefficient 0" % (FIT_END, TUNE_START, TUNE_END))
    for side in ("fav", "dog"):
        bp_fit = base_projection(fit, W, K, coefs, side); bp_tune = base_projection(tune, W, K, coefs, side)
        coefs["calib"][side] = {}; coefs["overlays"][side] = {}
        for cat in CATEGORIES:
            y_fit = fit["%s_%s" % (side, cat)].values; y_tune = tune["%s_%s" % (side, cat)].values
            m_f = np.isfinite(y_fit) & np.isfinite(bp_fit[cat]); m_t = np.isfinite(y_tune) & np.isfinite(bp_tune[cat])
            cb, ct, _ = ols(np.column_stack([np.ones(m_f.sum()), bp_fit[cat][m_f]]), y_fit[m_f])
            coefs["calib"][side][cat] = cb.tolist()
            base_f = cb[0] + cb[1] * bp_fit[cat][m_f]; base_t = cb[0] + cb[1] * bp_tune[cat][m_t]
            Xf = overlay_design(fit[m_f], side, coefs); Xt = overlay_design(tune[m_t], side, coefs)
            ok_f = np.isfinite(Xf).all(1); ok_t = np.isfinite(Xt).all(1)
            bf, tf, _ = ols(np.column_stack([np.ones(ok_f.sum()), Xf[ok_f]]), (y_fit[m_f] - base_f)[ok_f])
            bt, tt, _ = ols(np.column_stack([np.ones(ok_t.sum()), Xt[ok_t]]), (y_tune[m_t] - base_t)[ok_t])
            kept = {}
            for j, nm in enumerate(OVERLAY_NAMES):
                passed = abs(tf[j + 1]) >= 3.0 and abs(tt[j + 1]) >= 1.5 and np.sign(bf[j + 1]) == np.sign(bt[j + 1])
                kept[nm] = float(bf[j + 1]) if passed else 0.0
                L.append("  %s %-7s %-12s fit %+.4f (t %+5.1f) | tune %+.4f (t %+5.1f)  %s"
                         % (side, cat, nm, bf[j + 1], tf[j + 1], bt[j + 1], tt[j + 1], "KEEP" if passed else "drop"))
            coefs["overlays"][side][cat] = kept
    return coefs, L

def project(df, coefs, side, layer="full"):
    """layer: 'price' (league-average rates), 'rates' (no overlays), 'full'."""
    W, K = coefs["window"], coefs["shrink"]
    if layer == "price":
        d2 = df.copy()
        tag = "%s_W%d_K%d" % (side, W, K); otag = "%s_W%d_K%d" % ("dog" if side == "fav" else "fav", W, K)
        for t_ in (tag, otag):
            for key in ("ace_rate", "df_rate", "spg", "hold_rate", "break_rate"):
                d2[t_ + "_" + key] = coefs["mean_" + key]
        bp = base_projection(d2, W, K, coefs, side)
    else:
        bp = base_projection(df, W, K, coefs, side)
    out = {}
    X = overlay_design(df, side, coefs) if layer == "full" else None
    for cat in CATEGORIES:
        cb = coefs["calib"][side][cat]
        v = cb[0] + cb[1] * bp[cat]
        if layer == "full":
            ov = coefs["overlays"][side][cat]
            adj = np.zeros(len(df))
            for j, nm in enumerate(OVERLAY_NAMES):
                if ov.get(nm, 0.0) != 0.0:
                    adj += ov[nm] * np.nan_to_num(X[:, j])
            v = v + adj
        out[cat] = np.maximum(np.asarray(v, float), 0.0)
    return out

def holdout_report(df, coefs):
    L = ["", "== HOLDOUT %d+ (never used for fitting or tuning) ==" % HOLD_START,
         "   %-5s %-7s  %8s %8s %8s   %8s %8s %8s   (MAE | RMSE)  n" % ("side", "cat", "price", "rates", "full", "price", "rates", "full")]
    hold = df[df["year"] >= HOLD_START]
    summary = {}
    for side in ("fav", "dog"):
        P = {lay: project(hold, coefs, side, lay) for lay in ("price", "rates", "full")}
        for cat in CATEGORIES:
            y = hold["%s_%s" % (side, cat)].values
            m = np.isfinite(y)
            for lay in P:
                m = m & np.isfinite(P[lay][cat])
            maes = {lay: float(np.mean(np.abs(P[lay][cat][m] - y[m]))) for lay in P}
            rmse = {lay: float(np.sqrt(np.mean((P[lay][cat][m] - y[m]) ** 2))) for lay in P}
            summary["%s_%s" % (side, cat)] = {"mae": maes, "rmse": rmse, "n": int(m.sum())}
            L.append("   %-5s %-7s  %8.3f %8.3f %8.3f   %8.3f %8.3f %8.3f   %d"
                     % (side, cat, maes["price"], maes["rates"], maes["full"], rmse["price"], rmse["rates"], rmse["full"], m.sum()))
    # calibration (full model) by predicted-decile for aces & holds
    L.append(""); L.append("calibration, full model, holdout: predicted-decile mean prediction vs mean actual")
    for side in ("fav", "dog"):
        P = project(hold, coefs, side, "full")
        for cat in ("aces", "dfs", "holds", "breaks"):
            y = hold["%s_%s" % (side, cat)].values; p = P[cat]
            m = np.isfinite(y) & np.isfinite(p)
            if m.sum() < 100:
                continue
            q = pd.qcut(p[m], 10, labels=False, duplicates="drop")
            pairs = [(float(np.mean(p[m][q == i])), float(np.mean(y[m][q == i]))) for i in sorted(set(q))]
            L.append("   %s %-7s " % (side, cat) + " ".join("%.1f/%.1f" % pr for pr in pairs))
    return L, summary

# ------------------------------------------------------------------ example projections
def example_cards(df, coefs, n=3):
    hold = df[(df["year"] >= HOLD_START)].tail(400)
    hold = hold[hold["fav_aces"].notna()].tail(n)
    L = ["", "== EXAMPLE PROJECTIONS (real holdout matches, full model) =="]
    for _, r in hold.iterrows():
        one = hold.loc[[r.name]]
        for side in ("fav", "dog"):
            P = project(one, coefs, side, "full")
            L.append("  %s %s (%s) %s vs %s  pinn_fav=%.2f frag=%.0f | %s: " % (r["date"], r["tourney_name"], r["surface"], r["fav_name"], r["dog_name"], r["pinn_fav"], r["fragility"], side)
                     + "  ".join("%s %.1f (act %s)" % (c, P[c][0], "%.0f" % r["%s_%s" % (side, c)] if pd.notna(r["%s_%s" % (side, c)]) else "-") for c in CATEGORIES))
    return L

# ------------------------------------------------------------------ build
def cmd_build(a):
    print(MARKER, "-- BUILD"); sys.stdout.flush()
    if os.path.exists(MODEL_OUT):
        with open(MODEL_OUT) as f:
            if json.load(f).get("frozen"):
                raise SystemExit("%s is FROZEN — --build would overwrite it. Use --refresh-state after a Sackmann refresh; a new model needs a new file name (V2)." % MODEL_OUT)
    if a.fragility_model and os.path.exists(a.fragility_model):
        sh = sha256_file(a.fragility_model)
        print("fragility_v3_model.json sha %s %s" % (sh[:16], "OK (frozen V3)" if sh.startswith(FRAG_SHA_PREFIX) else "** MISMATCH — frozen V3 betas in this script still used; investigate"))
    df = load_sackmann(a.sackmann_dir, a.year_from, challengers=False)
    print("loaded %d matches (year_from=%d, challengers OUT) %s .. %s" % (len(df), a.year_from, df["tourney_date"].min().date(), df["tourney_date"].max().date()))
    rows, state = replay(df)
    rd = pd.DataFrame(rows)
    rd, _ = join_price(rd, a.rows, a.prob_col)
    print("price joined: %d / %d rows" % (int(rd["p_winner"].notna().sum()), len(rd)))
    ff = fav_frame(rd)
    ff = ff[(ff["retired"] == 0) & (ff["stats_ok"] == 1) & ff["total_games"].notna()].copy()
    ff, frag_ref = fragility_scores(ff)
    ff.to_csv(ROWS_OUT, index=False)
    print("wrote %s: %d rows" % (ROWS_OUT, len(ff)))
    coefs, L = fit_model(ff)
    coefs["frag_ref"] = frag_ref
    coefs["frag_v3"] = FRAG_V3
    coefs["price_bands"] = PRICE_BANDS
    coefs["state_latest_date"] = state["latest_date"]
    L2, summary = holdout_report(ff, coefs)
    coefs["holdout_summary"] = summary
    L3 = example_cards(ff, coefs)
    with open(MODEL_OUT, "w") as f:
        json.dump(coefs, f, indent=1, sort_keys=True)
    with open(STATE_OUT, "wb") as f:
        pickle.dump(state, f)
    rep = [MARKER + " -- BUILD REPORT " + datetime.now().isoformat(), "MODEL NOT FROZEN YET — run --freeze before any live projection"] + L + L2 + L3
    with open(REPORT_OUT, "w") as f:
        f.write("\n".join(rep) + "\n")
    print("\n".join(rep))
    print("\nwrote %s (unfrozen), %s (%d players, latest %s), %s" % (MODEL_OUT, STATE_OUT, len(state["players"]), state["latest_date"], REPORT_OUT))

def cmd_refresh_state(a):
    """After a Sackmann refresh: rebuild player state (Elo, rates, last surface) WITHOUT touching the model."""
    print(MARKER, "-- REFRESH STATE (model file untouched)")
    df = load_sackmann(a.sackmann_dir, a.year_from, challengers=False)
    print("loaded %d matches (year_from=%d, challengers OUT) %s .. %s" % (len(df), a.year_from, df["tourney_date"].min().date(), df["tourney_date"].max().date()))
    _, state = replay(df, grids=[], collect_rows=False)
    with open(STATE_OUT, "wb") as f:
        pickle.dump(state, f)
    print("wrote %s (%d players, latest %s)" % (STATE_OUT, len(state["players"]), state["latest_date"]))

# ------------------------------------------------------------------ freeze / spec
def load_model(require_frozen=True):
    if not os.path.exists(MODEL_OUT):
        raise SystemExit("%s missing — run --build first" % MODEL_OUT)
    with open(MODEL_OUT) as f:
        m = json.load(f)
    if require_frozen:
        if not m.get("frozen"):
            raise SystemExit("model is NOT frozen — run --freeze first (live projections refuse unfrozen models)")
        body = dict(m); stamp = body.pop("freeze")
        actual = sha256_text(json.dumps(body, sort_keys=True))
        if actual != stamp["sha"]:
            raise SystemExit("MODEL TAMPERED: sha %s != frozen %s — refusing" % (actual[:16], stamp["sha"][:16]))
        m["_sha"] = stamp["sha"]
    return m

def cmd_freeze(a):
    m = load_model(require_frozen=False)
    if m.get("frozen"):
        raise SystemExit("already frozen at %s (sha %s) — refusing to freeze twice" % (m["freeze"]["at"], m["freeze"]["sha"][:16]))
    m["frozen"] = True
    body = dict(m)
    sha = sha256_text(json.dumps(body, sort_keys=True))
    m["freeze"] = {"at": datetime.now().isoformat(), "sha": sha, "rule": "no refit/retune/recalibration during forward test"}
    with open(MODEL_OUT, "w") as f:
        json.dump(m, f, indent=1, sort_keys=True)
    print("FROZEN %s  model sha %s" % (MODEL_OUT, sha))
    print("file sha256 %s" % sha256_file(MODEL_OUT))

def cmd_spec(a):
    m = load_model()
    print(MARKER, "-- FROZEN SPEC   model sha", m["_sha"])
    print("frozen at", m["freeze"]["at"], "| replay state latest date", m.get("state_latest_date"))
    print("\nFEATURES")
    print("  L1 market: pinn_fav (de-vigged favorite prob), logit(p), logit(p)^2, best_of=5, is_wta")
    print("  L2 rates (surface-specific, window=%d matches, shrink k=%d toward all-surface): ace_rate, df_rate, hold_rate, break_rate, spg (points per service game) for each player" % (m["window"], m["shrink"]))
    print("  L3 overlays: fragility (frozen V3 percentile, match-level), elo_gap z and age gap z in the player's own frame (self minus opponent, residualised on fragility), own surface switch")
    print("\nFORMULAS")
    print("  E[TG]    = " + " + ".join("%.4f*%s" % (c, n) for c, n in zip(m["total_games"], m["struct_names"])))
    print("  E[svg_s] = a0 + a1*E[TG]            fav %s  dog %s" % (["%.3f" % x for x in m["svgms"]["fav"]], ["%.3f" % x for x in m["svgms"]["dog"]]))
    print("  base aces  = E[svg]*spg*ace_rate ; base dfs = E[svg]*spg*df_rate")
    print("  base holds = E[svg]*(hold_rate - (opp_break_rate - %.3f))" % m["mean_break_rate"])
    print("  base breaks= E[svg_opp]*(break_rate + (1-opp_hold_rate) - %.3f)" % (1 - m["mean_hold_rate"]))
    print("  final      = c0 + c1*base + sum(overlay_coef * overlay_z), floored at 0")
    print("\nCALIBRATION + OVERLAYS (per side/category; 0 = failed replication gate)")
    for side in ("fav", "dog"):
        for cat in CATEGORIES:
            cb = m["calib"][side][cat]; ov = m["overlays"][side][cat]
            print("  %s %-7s c0 %+.3f c1 %.3f | " % (side, cat, cb[0], cb[1]) + "  ".join("%s %+.4f" % (k, ov[k]) for k in OVERLAY_NAMES))
    print("\nHOLDOUT %d+ MAE (price-only / rates / full):" % HOLD_START)
    for k, v in m["holdout_summary"].items():
        print("  %-12s %.3f / %.3f / %.3f  n=%d" % (k, v["mae"]["price"], v["mae"]["rates"], v["mae"]["full"], v["n"]))
    print("\nLOG SCHEMA (%s): %s" % (LOG_OUT, ", ".join(LOG_FIELDS)))
    print("FREEZE RULE: %s" % m["freeze"]["rule"])

# ------------------------------------------------------------------ live projection
def live_features(state, name, surface, today):
    p = state["players"][name]
    ps = PState(name); ps.elo = p["elo"]; ps.n = p["n"]
    for s, lst in p["hist"].items():
        ps.hist[s] = deque(lst, maxlen=max(WINDOW_GRID))
    age = None
    if p["last_age"] is not None and p["last_age_date"] is not None:
        age = p["last_age"] + (today - p["last_age_date"]).days / 365.25
    switch = int(p["last_surface"] is not None and p["last_surface"] != surface)
    stale = (today - p["last_date"]).days if p["last_date"] else None
    return ps, age, switch, stale

def cmd_project(a):
    m = load_model()
    with open(STATE_OUT, "rb") as f:
        state = pickle.load(f)
    today = date.fromisoformat(a.date) if a.date else date.today()
    fav = resolve_name(a.project[0], state); dog = resolve_name(a.project[1], state)
    pinn = float(a.pinn)
    if not (0.5 <= pinn < 1.0):
        raise SystemExit("--pinn must be the FAVORITE's de-vigged probability in [0.5, 1)")
    surface = a.surface.title()
    W, K = m["window"], m["shrink"]
    feats = {}
    for side, nm in (("fav", fav), ("dog", dog)):
        ps, age, sw, stale = live_features(state, nm, surface, today)
        rb = rate_bundle(ps, surface, W, K)
        if rb is None:
            raise SystemExit("no serve-stat history for %s — cannot project" % nm)
        feats[side] = (rb, age, sw, stale)
        if stale and stale > 90:
            print("note: %s last seen %d days ago (state ends %s) — refresh Sackmann + rebuild" % (nm, stale, state["latest_date"]))
    row = {"lp": logit(pinn), "bo5": int(a.bo5), "is_wta": int(a.wta), "pinn_fav": pinn,
           "fav_switch": feats["fav"][2], "dog_switch": feats["dog"][2]}
    for side in ("fav", "dog"):
        tag = "%s_W%d_K%d" % (side, W, K)
        for key in ("ace_rate", "df_rate", "hold_rate", "break_rate", "spg"):
            row[tag + "_" + key] = feats[side][0][key]
    elo_gap = (state["players"][fav]["elo"] - state["players"][dog]["elo"]) * math.log(10) / 400 - logit(pinn)
    d_age = (feats["fav"][1] - feats["dog"][1]) if (feats["fav"][1] is not None and feats["dog"][1] is not None) else np.nan
    row["elo_gap"] = elo_gap; row["d_age"] = d_age; row["d_surface_switch"] = row["fav_switch"] - row["dog_switch"]
    raw = 0.0
    for f, c in m["frag_v3"].items():
        v = row[f] if not (isinstance(row[f], float) and math.isnan(row[f])) else c["mu"]
        raw += c["beta"] * (v - c["mu"]) / c["sd"]
    frag = fragility_from_ref(raw, pinn, m["frag_ref"])
    row["fragility"] = frag if frag is not None else 50.0
    df = pd.DataFrame([row])
    match_id = sha256_text("|".join([today.isoformat(), norm_name(fav), norm_name(dog)]))[:12]
    print("%s -- PROJECTION  match_id %s  %s  %s vs %s  %s  pinn_fav %.3f  fragility %.0f  (model %s)"
          % (MARKER, match_id, today, fav, dog, surface, pinn, row["fragility"], m["_sha"][:12]))
    print("  elo_gap %+.3f  age_gap %s  switch fav/dog %d/%d" % (elo_gap, "%+.1f" % d_age if not math.isnan(d_age) else "n/a", row["fav_switch"], row["dog_switch"]))
    existing = set()
    if os.path.exists(LOG_OUT):
        with open(LOG_OUT) as f:
            for r in csv.DictReader(f):
                existing.add((r["match_id"], r["player"], r["category"]))
    new_rows = []
    for side, nm, opp in (("fav", fav, dog), ("dog", dog, fav)):
        P = project(df, m, side, "full")
        rb = feats[side][0]
        print("  %s %-28s rates(surf n=%d/all n=%d): ace %.3f df %.3f hold %.3f break %.3f spg %.2f"
              % (side, nm, rb["n_surf"], rb["n_all"], rb["ace_rate"], rb["df_rate"], rb["hold_rate"], rb["break_rate"], rb["spg"]))
        print("      " + "  ".join("%s %.2f" % (c, P[c][0]) for c in CATEGORIES))
        for c in CATEGORIES:
            key = (match_id, nm, c)
            if key in existing:
                continue
            new_rows.append({"ts": datetime.now().isoformat(timespec="seconds"), "match_id": match_id, "match_date": today.isoformat(),
                             "player": nm, "opponent": opp, "is_fav": int(side == "fav"), "pinn_fav": "%.4f" % pinn, "surface": surface,
                             "category": c, "projection": "%.3f" % P[c][0], "fragility": "%.1f" % row["fragility"],
                             "book": "", "line": "", "odds_over": "", "odds_under": "", "edge": "", "actual": "", "settled_ts": "",
                             "model_sha": m["_sha"]})
    if new_rows:
        write_header = not os.path.exists(LOG_OUT)
        with open(LOG_OUT, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if write_header:
                w.writeheader()
            w.writerows(new_rows)
        print("  logged %d projection rows -> %s" % (len(new_rows), LOG_OUT))
    else:
        print("  duplicate: projections for this match already logged (no rewrite)")

def read_log():
    if not os.path.exists(LOG_OUT):
        raise SystemExit("no forward log yet (%s)" % LOG_OUT)
    with open(LOG_OUT) as f:
        return list(csv.DictReader(f))

def write_log(rows):
    with open(LOG_OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS); w.writeheader(); w.writerows(rows)

def cmd_line(a):
    match_id, player, cat, line, book = a.line
    if cat not in CATEGORIES:
        raise SystemExit("category must be one of %s" % CATEGORIES)
    rows = read_log(); hit = 0
    for r in rows:
        if r["match_id"] == match_id and norm_name(r["player"]) == norm_name(player) and r["category"] == cat:
            if r["line"]:
                print("line already attached (%s %s) — not overwriting; log one line per projection" % (r["book"], r["line"])); return
            r["book"] = book; r["line"] = "%.1f" % float(line)
            r["odds_over"] = a.odds_over or ""; r["odds_under"] = a.odds_under or ""
            r["edge"] = "%.3f" % (float(r["projection"]) - float(line)); hit += 1
            print("%s %s %s: projection %s vs %s line %s -> edge %+.2f (%s)" % (match_id, r["player"], cat, r["projection"], book, r["line"], float(r["edge"]), "OVER lean" if float(r["edge"]) > 0 else "UNDER lean"))
    if not hit:
        raise SystemExit("no logged projection for match %s / %s / %s — run --project first" % (match_id, player, cat))
    write_log(rows)

def cmd_settle(a):
    match_id, player = a.settle
    vals = {"aces": a.aces, "dfs": a.dfs, "holds": a.holds, "breaks": a.breaks, "svgms": a.svgms}
    rows = read_log(); n = 0
    for r in rows:
        if r["match_id"] == match_id and norm_name(r["player"]) == norm_name(player) and vals.get(r["category"]) is not None:
            r["actual"] = "%.0f" % float(vals[r["category"]]); r["settled_ts"] = datetime.now().isoformat(timespec="seconds"); n += 1
    write_log(rows); print("settled %d rows" % n)

def cmd_settle_sackmann(a):
    rows = read_log()
    open_rows = [r for r in rows if not r["actual"]]
    if not open_rows:
        print("nothing to settle"); return
    years = sorted(set(r["match_date"][:4] for r in open_rows))
    df = load_sackmann(a.sackmann_dir, year_from=int(years[0]), challengers=False)
    n = 0
    for r in open_rows:
        pn = norm_name(r["player"]); on = norm_name(r["opponent"]); md = pd.Timestamp(r["match_date"])
        cand = df[(df["tourney_date"] >= md - pd.Timedelta(days=21)) & (df["tourney_date"] <= md)]
        cand = cand[((cand["winner_name"].map(norm_name) == pn) & (cand["loser_name"].map(norm_name) == on)) |
                    ((cand["winner_name"].map(norm_name) == on) & (cand["loser_name"].map(norm_name) == pn))]
        if len(cand) != 1:
            continue
        c = cand.iloc[0]; won = norm_name(c["winner_name"]) == pn
        games, retired = parse_games(c["score"])
        if retired:
            continue
        p = "w_" if won else "l_"; o = "l_" if won else "w_"
        if pd.isna(c[p + "SvGms"]):
            continue
        val = {"aces": c[p + "ace"], "dfs": c[p + "df"], "svgms": c[p + "SvGms"],
               "holds": c[p + "SvGms"] - (c[p + "bpFaced"] - c[p + "bpSaved"]),
               "breaks": c[o + "bpFaced"] - c[o + "bpSaved"]}[r["category"]]
        if pd.isna(val):
            continue
        r["actual"] = "%.0f" % float(val); r["settled_ts"] = datetime.now().isoformat(timespec="seconds"); n += 1
    write_log(rows); print("settled %d rows from Sackmann (tourney-date window; rematches within 21 days are skipped when ambiguous)" % n)

def cmd_report(a):
    rows = read_log()
    shas = set(r["model_sha"] for r in rows)
    print(MARKER, "-- FORWARD REPORT   rows %d   model SHAs in log: %s" % (len(rows), ", ".join(s[:12] for s in shas)))
    if len(shas) > 1:
        print("** WARNING: more than one model SHA in the log — freeze was broken somewhere")
    settled = [r for r in rows if r["actual"]]
    print("settled %d / %d" % (len(settled), len(rows)))
    if not settled:
        return
    print("\nby category (settled): n, MAE, RMSE, mean proj, mean actual")
    for cat in CATEGORIES:
        s = [r for r in settled if r["category"] == cat]
        if not s:
            continue
        p = np.array([float(r["projection"]) for r in s]); y = np.array([float(r["actual"]) for r in s])
        print("  %-7s n=%3d  MAE %.3f  RMSE %.3f  proj %.2f  act %.2f" % (cat, len(s), np.mean(np.abs(p - y)), np.sqrt(np.mean((p - y) ** 2)), p.mean(), y.mean()))
    lined = [r for r in settled if r["line"]]
    print("\nedge vs lines: %d settled rows with a market line" % len(lined))
    if lined:
        hits = []
        for r in lined:
            e = float(r["edge"]); y = float(r["actual"]); ln = float(r["line"])
            if y == ln:
                continue
            hits.append((abs(e), (e > 0) == (y > ln), r["category"]))
        if hits:
            print("  hit rate (projection side of line wins): %.1f%% (n=%d, pushes excluded; break-even ~58%% at PrizePicks-style pricing)"
                  % (100 * np.mean([h[1] for h in hits]), len(hits)))
            for lo, hi in ((0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 99)):
                b = [h[1] for h in hits if lo <= h[0] < hi]
                if b:
                    print("  |edge| %.1f-%.1f: hit %.1f%% (n=%d)" % (lo, hi, 100 * np.mean(b), len(b)))
            for cat in CATEGORIES:
                b = [h[1] for h in hits if h[2] == cat]
                if b:
                    print("  %-7s hit %.1f%% (n=%d)" % (cat, 100 * np.mean(b), len(b)))
    print("\ncalibration (settled, all categories pooled by projection bucket): proj mean / actual mean / n")
    p = np.array([float(r["projection"]) for r in settled]); y = np.array([float(r["actual"]) for r in settled])
    for lo, hi in ((0, 2), (2, 4), (4, 6), (6, 9), (9, 13), (13, 99)):
        m = (p >= lo) & (p < hi)
        if m.sum():
            print("  [%2d,%2d): %.2f / %.2f / %d" % (lo, hi, p[m].mean(), y[m].mean(), m.sum()))
    compare_fragility(rows)
    if len(settled) < 300:
        print("\n(fewer than 300 settled rows: treat everything above as descriptive, not conclusive)")

def compare_fragility(rows, live_log="fragility_forward_log.csv"):
    """Side-by-side: Prop Model V1 fragility (replay Elo) vs fragility_live score on the same matches."""
    print("\nfragility drift check: Prop V1 score vs fragility_live (%s)" % live_log)
    if not os.path.exists(live_log):
        print("  %s not present — nothing to compare yet" % live_log); return
    try:
        lv = pd.read_csv(live_log)
    except Exception as e:
        print("  could not read %s: %s" % (live_log, e)); return
    cols = {c.lower(): c for c in lv.columns}
    score_col = next((cols[c] for c in cols if c in ("fragility", "score", "fragility_score", "frag_score", "fragility_v3")), None)
    name_cols = [cols[c] for c in cols if any(k in c for k in ("fav", "dog", "player", "name", "opponent")) and lv[cols[c]].dtype == object]
    date_col = next((cols[c] for c in cols if "date" in c or c in ("ts", "timestamp", "logged_at")), None)
    if score_col is None or len(name_cols) < 2:
        print("  could not detect score/name columns in %s (have: %s)" % (live_log, list(lv.columns))); return
    lv["_names"] = lv[name_cols].astype(str).apply(lambda r: frozenset(norm_name(x) for x in r if norm_name(x)), axis=1)
    lv["_d"] = pd.to_datetime(lv[date_col], errors="coerce").dt.date if date_col else None
    mine = {}
    for r in rows:
        key = frozenset([norm_name(r["player"]), norm_name(r["opponent"])])
        mine.setdefault(key, (r["match_date"], float(r["fragility"]), r["player"], r["opponent"]))
    pairs = []
    for _, l in lv.iterrows():
        for key, (d, f, p, o) in mine.items():
            if key <= l["_names"] or l["_names"] <= key:
                if date_col and l["_d"] is not None and pd.notna(l["_d"]) and abs((pd.Timestamp(d).date() - l["_d"]).days) > 5:
                    continue
                try:
                    pairs.append((d, p, o, f, float(l[score_col])))
                except (TypeError, ValueError):
                    pass
    if not pairs:
        print("  no shared matches yet"); return
    print("  %-10s %-40s %8s %8s %6s" % ("date", "match", "propV1", "live", "diff"))
    for d, p, o, f, g in pairs:
        print("  %-10s %-40s %8.1f %8.1f %+6.1f" % (d, (p + " v " + o)[:40], f, g, f - g))
    diffs = np.array([f - g for _, _, _, f, g in pairs])
    print("  n=%d  mean diff %+.2f  mean |diff| %.2f  max |diff| %.1f  (points on the 0-100 scale; expected source: replay-Elo ordering differences)"
          % (len(diffs), diffs.mean(), np.abs(diffs).mean(), np.abs(diffs).max()))

def cmd_check_elo(a):
    df = load_sackmann(a.sackmann_dir, a.year_from, challengers=False)
    rows, _ = replay(df, grids=[], collect_rows=True)
    rd = pd.DataFrame(rows)
    rd, b = join_price(rd, a.rows, a.prob_col)
    if "d_elo" not in b.columns:
        print("batch rows have no d_elo column; cannot check"); return
    cols = {c.lower(): c for c in b.columns}
    dcol = cols.get("date") or cols.get("match_date"); acol = cols.get("player_a"); bcol = cols.get("player_b")
    b["_key"] = pd.to_datetime(b[dcol].astype(str), errors="coerce").dt.date.astype(str) + "|" + b[acol].map(norm_name) + "|" + b[bcol].map(norm_name)
    my = {}
    for r in rd.itertuples(index=False):
        my[r.date + "|" + norm_name(r.winner) + "|" + norm_name(r.loser)] = r.w_elo - r.l_elo
        my[r.date + "|" + norm_name(r.loser) + "|" + norm_name(r.winner)] = r.l_elo - r.w_elo
    diffs = [float(d) - my[k] for k, d in zip(b["_key"], b["d_elo"]) if k in my and pd.notna(d)]
    diffs = np.array(diffs)
    print("Elo check vs %s d_elo: n=%d  MAE %.3f  max %.2f  exact(<=0.5) %.2f%%" % (a.rows, len(diffs), np.abs(diffs).mean(), np.abs(diffs).max(), 100 * np.mean(np.abs(diffs) <= 0.5)))
    print("(small scattered error = ordering differences inside a tournament; constant offset = wrong year_from)")

def cmd_inspect(a):
    if os.path.exists(a.rows):
        b = pd.read_csv(a.rows, nrows=5, low_memory=False); print("%s columns: %s" % (a.rows, list(b.columns)))
    files = sorted(glob.glob(os.path.join(a.sackmann_dir, "*.csv")))
    print("Sackmann dir %r: %d csv files; sample: %s" % (a.sackmann_dir, len(files), [os.path.basename(f) for f in files[-4:]]))
    if files:
        d = pd.read_csv(files[-1], nrows=3); print("columns:", list(d.columns))

# ------------------------------------------------------------------ selftest
def synth_sackmann(n=24000, seed=7):
    rng = np.random.default_rng(seed)
    players = ["%s %s" % (chr(65 + i // 26) + "ab", chr(65 + i % 26) + "xy") for i in range(160)]
    elo_true = dict(zip(players, rng.normal(1500, 120, len(players))))
    ace_true = {p: {s: float(np.clip(rng.normal(0.08 + 0.02 * (s == "Grass") - 0.02 * (s == "Clay"), 0.03), 0.01, 0.3)) for s in ("Hard", "Clay", "Grass")} for p in players}
    df_true = {p: float(np.clip(rng.normal(0.035, 0.012), 0.005, 0.1)) for p in players}
    hold_true = {p: float(np.clip(rng.normal(0.78, 0.06), 0.5, 0.95)) for p in players}
    age0 = {p: float(rng.uniform(19, 33)) for p in players}
    last_surface = {}
    recs = []
    day0 = datetime(2005, 1, 3)
    for i in range(n):
        a, b = rng.choice(players, 2, replace=False)
        d = day0 + pd.Timedelta(days=int(i * (21 * 365 / n)))
        surf = rng.choice(["Hard", "Clay", "Grass"], p=[0.55, 0.33, 0.12])
        pa = 1 / (1 + 10 ** ((elo_true[b] - elo_true[a]) / 400))
        # fragility-like latent: paper-vs-price gap. Market price = true prob + noise
        price_a = float(np.clip(pa + rng.normal(0, 0.04), 0.05, 0.95))
        fav, dog = (a, b) if price_a >= 0.5 else (b, a)
        pf = max(price_a, 1 - price_a)
        # planted: fragile favorites (fav paper strength > price) -> dog aces UP; switch -> own DFs UP
        fav_paper = 1 / (1 + 10 ** ((elo_true[dog] - elo_true[fav]) / 400))
        frag_latent = (fav_paper - pf) / 0.05
        sw = {p: int(last_surface.get(p) is not None and last_surface.get(p) != surf) for p in (a, b)}
        winner = a if rng.random() < pa else b
        loser = b if winner == a else a
        tg = int(np.clip(rng.normal(20 + 6 * (1 - abs(pf - 0.5) * 2), 4), 12, 39))
        out = {}
        for p, opp in ((a, b), (b, a)):
            svg = int(round(tg / 2 + rng.normal(0, 0.7)))
            svpt = int(svg * 6.3 + rng.normal(0, 4))
            ace_mult = 1 + (0.25 * frag_latent if p == dog else -0.15 * frag_latent)
            aces = rng.poisson(max(svpt * ace_true[p][surf] * max(ace_mult, 0.2), 0.01))
            dfs = rng.poisson(max(svpt * df_true[p] * (1.25 if sw[p] else 1.0), 0.01))
            bpf = rng.poisson(max((1 - hold_true[p]) * svg * 2.2, 0.05)); bps = rng.binomial(bpf, 0.55)
            out[p] = (aces, dfs, svpt, svg, bpf, bps)
        w, l = out[winner], out[loser]
        recs.append({"tourney_id": "T%d" % (i // 32), "tourney_name": "Synth", "surface": surf, "tourney_level": "A",
                     "tourney_date": d.strftime("%Y%m%d"), "match_num": i % 32, "winner_name": winner, "loser_name": loser,
                     "winner_age": age0[winner] + (d - day0).days / 365.25, "loser_age": age0[loser] + (d - day0).days / 365.25,
                     "score": "6-4 6-4", "best_of": 3, "round": "R32", "minutes": 100,
                     "w_ace": w[0], "w_df": w[1], "w_svpt": w[2], "w_1stIn": 0, "w_1stWon": 0, "w_2ndWon": 0, "w_SvGms": w[3], "w_bpSaved": w[5], "w_bpFaced": w[4],
                     "l_ace": l[0], "l_df": l[1], "l_svpt": l[2], "l_1stIn": 0, "l_1stWon": 0, "l_2ndWon": 0, "l_SvGms": l[3], "l_bpSaved": l[5], "l_bpFaced": l[4],
                     "_price_a": price_a, "_a": a})
        last_surface[a] = surf; last_surface[b] = surf
    df = pd.DataFrame(recs)
    return df

def cmd_selftest(a):
    print(MARKER, "-- SELFTEST (synthetic, temp dir)")
    cwd = os.getcwd(); tmp = tempfile.mkdtemp(prefix="apex_prop_v1_")
    os.chdir(tmp)
    try:
        sd = os.path.join(tmp, "sack"); os.makedirs(sd)
        df = synth_sackmann()
        for yr, g in df.groupby(df["tourney_date"].str[:4]):
            g.drop(columns=["_price_a", "_a"]).to_csv(os.path.join(sd, "%s.csv" % yr), index=False)
        batch = pd.DataFrame({"date": pd.to_datetime(df["tourney_date"]).dt.strftime("%Y-%m-%d"),
                              "player_a": df["_a"], "player_b": np.where(df["_a"] == df["winner_name"], df["loser_name"], df["winner_name"]),
                              "pinn_a": df["_price_a"]})
        batch.to_csv("apex_batch_a_rows.csv", index=False)
        ns = argparse.Namespace(sackmann_dir=sd, year_from=2005, rows="apex_batch_a_rows.csv", prob_col="pinn_a", fragility_model=None)
        cmd_build(ns)
        m = load_model(require_frozen=False)
        fails = []
        hs = m["holdout_summary"]
        for side in ("fav", "dog"):
            if not hs[side + "_aces"]["mae"]["rates"] < hs[side + "_aces"]["mae"]["price"]:
                fails.append("%s aces: rates layer did not beat price-only" % side)
        if not m["overlays"]["dog"]["aces"]["z_frag"] > 0:
            fails.append("planted fragility->dog aces not recovered (coef %.4f)" % m["overlays"]["dog"]["aces"]["z_frag"])
        if not m["overlays"]["fav"]["aces"]["z_frag"] < 0:
            fails.append("planted fragility->fav aces (negative) not recovered")
        for side in ("fav", "dog"):
            if not m["overlays"][side]["dfs"]["switch_self"] > 0:
                fails.append("planted switch->own dfs not recovered on %s" % side)
        if not hs["dog_aces"]["mae"]["full"] <= hs["dog_aces"]["mae"]["rates"] + 1e-9:
            fails.append("full model worse than rates on dog aces holdout")
        # unfrozen refusal, freeze, double-freeze refusal, tamper detection
        try:
            load_model(); fails.append("unfrozen model accepted")
        except SystemExit:
            pass
        cmd_freeze(ns)
        try:
            cmd_freeze(ns); fails.append("double freeze accepted")
        except SystemExit:
            pass
        cmd_spec(ns)
        try:
            cmd_build(ns); fails.append("--build overwrote a frozen model")
        except SystemExit as e:
            print("frozen-build guard:", e)
        cmd_refresh_state(ns)
        if not load_model().get("frozen"):
            fails.append("refresh-state disturbed the frozen model")
        # live projection round trip
        with open(STATE_OUT, "rb") as f:
            st = pickle.load(f)
        names = sorted(st["players"])[:2]
        pn = argparse.Namespace(project=names, pinn=0.64, surface="Hard", bo5=False, wta=False, date=None)
        cmd_project(pn); cmd_project(pn)   # second call = duplicate, must not add rows
        rows = read_log()
        if len(rows) != 10:
            fails.append("expected 10 log rows, got %d" % len(rows))
        mid = rows[0]["match_id"]
        cmd_line(argparse.Namespace(line=[mid, names[0], "aces", "4.5", "prizepicks"], odds_over=None, odds_under=None))
        cmd_settle(argparse.Namespace(settle=[mid, names[0]], aces=6, dfs=2, holds=9, breaks=2, svgms=10))
        cmd_report(ns)
        with open(MODEL_OUT) as f:
            mm = json.load(f)
        mm["overlays"]["dog"]["aces"]["z_frag"] += 0.5
        with open(MODEL_OUT, "w") as f:
            json.dump(mm, f, indent=1, sort_keys=True)
        try:
            load_model(); fails.append("tampered model accepted")
        except SystemExit as e:
            print("tamper check:", e)
        print("\nSELFTEST", "PASS" if not fails else "FAIL: " + "; ".join(fails))
        if fails:
            sys.exit(1)
    finally:
        os.chdir(cwd)

# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=MARKER)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--refresh-state", action="store_true", help="rebuild player state after a Sackmann refresh; never touches a frozen model")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--spec", action="store_true")
    ap.add_argument("--project", nargs=2, metavar=("FAV", "DOG"))
    ap.add_argument("--pinn", type=float)
    ap.add_argument("--surface", default="Hard")
    ap.add_argument("--bo5", action="store_true"); ap.add_argument("--wta", action="store_true")
    ap.add_argument("--date", default=None, help="match date YYYY-MM-DD (default today)")
    ap.add_argument("--tour", default=""); ap.add_argument("--round", default="")
    ap.add_argument("--line", nargs=5, metavar=("MATCH_ID", "PLAYER", "CATEGORY", "LINE", "BOOK"))
    ap.add_argument("--odds-over", default=None); ap.add_argument("--odds-under", default=None)
    ap.add_argument("--settle", nargs=2, metavar=("MATCH_ID", "PLAYER"))
    for c in CATEGORIES:
        ap.add_argument("--" + c, type=float, default=None)
    ap.add_argument("--settle-sackmann", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--check-elo", action="store_true")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--sackmann-dir", default="ATP & WTA, 1968-2026")
    ap.add_argument("--year-from", type=int, default=2005)
    ap.add_argument("--rows", default="apex_batch_a_rows.csv")
    ap.add_argument("--prob-col", default="pinn_a")
    ap.add_argument("--fragility-model", default="fragility_v3_model.json")
    a = ap.parse_args()
    if a.selftest: cmd_selftest(a)
    elif a.build: cmd_build(a)
    elif a.refresh_state: cmd_refresh_state(a)
    elif a.freeze: cmd_freeze(a)
    elif a.spec: cmd_spec(a)
    elif a.project:
        if a.pinn is None: raise SystemExit("--project needs --pinn")
        cmd_project(a)
    elif a.line: cmd_line(a)
    elif a.settle: cmd_settle(a)
    elif a.settle_sackmann: cmd_settle_sackmann(a)
    elif a.report: cmd_report(a)
    elif a.check_elo: cmd_check_elo(a)
    elif a.inspect: cmd_inspect(a)
    else: ap.print_help()

if __name__ == "__main__":
    main()
