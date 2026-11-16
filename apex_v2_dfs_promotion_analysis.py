"""
apex_v2_dfs_promotion_analysis.py

PROMOTION ANALYSIS for Double Faults dispersion calibration -- diagnostic
only. Do NOT modify production files. Do NOT deploy. Evidence only.

Mirrors apex_v2_dispersion_promotion_analysis.py (the aces promotion
analysis) exactly, retargeted to DFs. Aces is held fixed at its ACTUAL
LIVE production settings throughout (Candidate C: ATP r={fav:8.701,
dog:5.886} dm=0.6, WTA r={fav:3.987, dog:2.797} dm=0.4) -- only DFs
varies across candidates, since aces is no longer a single global
disp_mult in production.

Compares three candidates on the untouched year>=2024 holdout:
  A) PRODUCTION   DFs disp_mult=1,000,000, pooled fitted r (fav=8.322, dog=9.244)
  B) GLOBAL       DFs disp_mult=0.5, pooled fitted r (unchanged r, only disp_mult lowered)
  C) SEGMENTED    ATP: own fitted r (fav=8.494, dog=9.164), disp_mult=0.75
                  WTA: own fitted r (fav=8.257, dog=9.500), disp_mult=0.3
                  (holdout-confirmed candidate region from Part B of the
                  DFs investigation, 2026-09-26)

For each candidate: MAE, MSE, old aggregate 80%-band coverage,
calibration Brier (total-dfs line), most_dfs_fav Brier, and the full
confidence-bucket gap table -- overall + by tour (ATP/WTA) + by surface
(Hard/Clay/Grass).

MD5-checked before/after against the two frozen V1 files.
"""
import copy
import hashlib
import json
import time
import numpy as np
import pandas as pd

import apex_prop_model_v1 as v1
import apex_prop_sim_v1 as sim

FROZEN = {
    "apex_prop_sim_v1.py": "7b03e521e804862b85804f8ac4db7346",
    "apex_prop_model_v1.py": "6bf50622a1761156a77ad0d35a2007f6",
}

def md5_check(tag):
    for fn, expected in FROZEN.items():
        got = hashlib.md5(open(fn, "rb").read()).hexdigest()
        status = "PASS" if got == expected else "FAIL"
        print("MD5 %s [%s]: %s" % (tag, fn, status))
        if status == "FAIL":
            raise SystemExit("MD5 MISMATCH -- aborting.")

md5_check("before")

FIT_END, HOLD_START = 2021, 2024
SACKMANN_DIR = "ATP & WTA, 1968-2026"
N_SIMS = 1000
SEED = 1
RESULTS = {}


m = v1.load_model()
coefs = sim.load_sim()
df = pd.read_csv(v1.ROWS_OUT, low_memory=False)
df = sim.attach_scores(df, SACKMANN_DIR)
df = df[df["score"].notna() & df["n_sets"].notna()].reset_index(drop=True)
_, hf0, hd0, mu0 = sim.v1_inputs_from_rows(df, m)
okmask = (np.isfinite(hf0) & np.isfinite(hd0) & np.isfinite(mu0["fav"][0])
          & np.isfinite(mu0["fav"][1]) & np.isfinite(mu0["dog"][0]) & np.isfinite(mu0["dog"][1]))
df = df[okmask].reset_index(drop=True)
fit_df = df[df["year"] <= FIT_END].reset_index(drop=True)
df_hold = df[df["year"] >= HOLD_START].reset_index(drop=True)
df_hold_atp = df_hold[df_hold["is_wta"] == 0].reset_index(drop=True)
df_hold_wta = df_hold[df_hold["is_wta"] == 1].reset_index(drop=True)
print("holdout n_pooled=%d n_atp=%d n_wta=%d" % (len(df_hold), len(df_hold_atp), len(df_hold_wta)))
print("surface counts (pooled holdout):")
print(df_hold["surface"].value_counts().to_string())

# Live production aces settings (Candidate C, unchanged here -- fixed
# throughout this DFs-only analysis).
R_ATP_ACES = {"fav": 8.70148205269834, "dog": 5.885645957929439}; DM_ATP_ACES = 0.6
R_WTA_ACES = {"fav": 3.9866756061964232, "dog": 2.7971092624925418}; DM_WTA_ACES = 0.4

def moment_r(mask, side, cat="dfs"):
    _, hf_, hd_, mu_ = sim.v1_inputs_from_rows(fit_df, m)
    idx = {"aces": 0, "dfs": 1}[cat]
    lam = mu_[side][idx][mask] * fit_df.loc[mask, "%s_svgms" % side].values
    y = fit_df.loc[mask, "%s_%s" % (side, cat)].values
    mk = np.isfinite(lam) & np.isfinite(y) & (lam > 0)
    excess = np.mean((y[mk] - lam[mk]) ** 2) - np.mean(lam[mk])
    return float(np.mean(lam[mk]) ** 2 / excess) if excess > 0 else float("inf")

r_pooled_dfs = {"fav": coefs["disp"]["fav"]["dfs"], "dog": coefs["disp"]["dog"]["dfs"]}
r_atp_dfs = {s: moment_r((fit_df["is_wta"] == 0).values, s) for s in ("fav", "dog")}
r_wta_dfs = {s: moment_r((fit_df["is_wta"] == 1).values, s) for s in ("fav", "dog")}
print("r_pooled_dfs=%s  r_atp_dfs=%s  r_wta_dfs=%s" % (r_pooled_dfs, r_atp_dfs, r_wta_dfs))

pbins = [0.0, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
plabels = ["<55%", "55-60%", "60-65%", "65-70%", "70-75%", "75-80%", "80%+"]


import sys

def aces_for_tour(is_wta):
    return (R_WTA_ACES, DM_WTA_ACES) if is_wta else (R_ATP_ACES, DM_ATP_ACES)

def simulate_pop_dfs(pop_df, dfs_lookup, seed=SEED):
    """dfs_lookup(is_wta_bool) -> (r_dfs_dict, dm_dfs). Aces always
    resolved to its live per-tour Candidate C settings, regardless of
    which DFs candidate is being tested."""
    P, hf, hd, mu = sim.v1_inputs_from_rows(pop_df, m)
    pl = sim.price_level_pred(pop_df, coefs)
    is_wta_col = pop_df["is_wta"].values
    n = len(pop_df)
    fav_samp = np.empty((n, N_SIMS)); dog_samp = np.empty((n, N_SIMS))
    t0 = time.time()
    for i in range(n):
        if i and i % 2000 == 0:
            print("  ... %d/%d (%.0fs)" % (i, n, time.time() - t0)); sys.stdout.flush()
        wta_flag = bool(is_wta_col[i])
        r_aces, dm_aces = aces_for_tour(wta_flag)
        r_dfs, dm_dfs = dfs_lookup(wta_flag)
        lv = sim.level_from(hf[i], hd[i], coefs, pl[i])
        bo = int(pop_df["best_of"].values[i])
        t, flag = sim.solve_tilt(lv, pop_df["pinn_fav"].values[i], coefs["tb_k"], bo, sigma=coefs.get("gap_sigma", 0.0))
        hA = min(max(lv + t, 0.2), 0.995); hB = min(max(lv - t, 0.2), 0.995)
        s = sim.simulate(
            hA, hB, coefs["tb_k"], bo,
            (mu["fav"][0][i], mu["fav"][1][i]), (mu["dog"][0][i], mu["dog"][1][i]),
            (r_aces["fav"] * dm_aces, r_dfs["fav"] * dm_dfs),
            (r_aces["dog"] * dm_aces, r_dfs["dog"] * dm_dfs),
            N_SIMS, seed=seed + i, sigma=coefs.get("gap_sigma", 0.0),
        )
        fav_samp[i] = s["fav"]["dfs"]; dog_samp[i] = s["dog"]["dfs"]
    dt = time.time() - t0
    return fav_samp, dog_samp, dt


def metrics_for_mask(fav_samp, dog_samp, fav_a, dog_a, mask, label):
    if mask.sum() == 0:
        return None
    fs = fav_samp[mask]; ds = dog_samp[mask]
    fa = fav_a[mask]; da = dog_a[mask]
    fav_mean = fs.mean(axis=1); dog_mean = ds.mean(axis=1)
    dfs_actual = np.concatenate([fa, da]).astype(float)
    dfs_pred = np.concatenate([fav_mean, dog_mean])
    okv = np.isfinite(dfs_actual) & np.isfinite(dfs_pred)
    resid = dfs_actual[okv] - dfs_pred[okv]
    mae = float(np.mean(np.abs(resid))); mse = float(np.mean(resid ** 2))

    total_samp = fs + ds
    total_actual = fa + da
    lines_ = np.floor(total_samp.mean(axis=1)) + 0.5
    phat = np.mean(total_samp > lines_[:, None], axis=1)
    actual_over = (total_actual > lines_).astype(float)
    okc = np.isfinite(total_actual)
    calib_brier = float(np.mean((phat[okc] - actual_over[okc]) ** 2))

    cov = []
    for i in range(fs.shape[0]):
        for side_arr, act in ((fs, fa), (ds, da)):
            y = act[i]
            if np.isfinite(y):
                x = side_arr[i]
                cov.append(np.percentile(x, 10) <= y <= np.percentile(x, 90))
    c80 = float(np.mean(cov)) if cov else float("nan")

    tie = fa == da
    actual_win = (fa > da).astype(float)
    valid = ~tie
    gt = fs > ds; ne = fs != ds
    pred_prob = gt.mean(axis=1) / np.maximum(ne.mean(axis=1), 1e-9)
    maf_brier = float(np.mean((pred_prob[valid] - actual_win[valid]) ** 2)) if valid.sum() else float("nan")

    buckets = {}
    for lo, hi, lab in zip(pbins[:-1], pbins[1:], plabels):
        bmask = valid & (pred_prob >= lo) & (pred_prob < hi)
        n = int(bmask.sum())
        if n == 0:
            continue
        buckets[lab] = {"n": n, "gap": float(np.mean(pred_prob[bmask]) - np.mean(actual_win[bmask]))}

    return {
        "label": label, "n": int(mask.sum()),
        "mae": mae, "mse": mse, "old_coverage": c80,
        "calib_brier": calib_brier, "most_dfs_fav_brier": maf_brier,
        "buckets": buckets,
    }


def full_breakdown(fav_samp, dog_samp, fav_a, dog_a, tour_arr, surface_arr, cand_label):
    print("\n" + "=" * 100)
    print("CANDIDATE %s -- full breakdown" % cand_label)
    print("=" * 100)
    out = {}
    n = len(fav_a)
    allmask = np.ones(n, dtype=bool)
    out["overall"] = metrics_for_mask(fav_samp, dog_samp, fav_a, dog_a, allmask, "overall")
    out["atp"] = metrics_for_mask(fav_samp, dog_samp, fav_a, dog_a, (tour_arr == 0), "ATP")
    out["wta"] = metrics_for_mask(fav_samp, dog_samp, fav_a, dog_a, (tour_arr == 1), "WTA")
    for surf in ["Hard", "Clay", "Grass"]:
        smask = (surface_arr == surf)
        r = metrics_for_mask(fav_samp, dog_samp, fav_a, dog_a, smask, surf)
        if r is not None:
            out[surf.lower()] = r
    for key in ["overall", "atp", "wta", "hard", "clay", "grass"]:
        r = out.get(key)
        if r is None:
            continue
        b = r["buckets"]
        bucket_str = "  ".join("%s=%+.3f(n=%d)" % (lab, b[lab]["gap"], b[lab]["n"]) for lab in plabels if lab in b)
        print("-- %-8s n=%-6d MAE=%.4f MSE=%.4f old_cov=%.3f calib_brier=%.4f maf_brier=%.4f"
              % (r["label"], r["n"], r["mae"], r["mse"], r["old_coverage"], r["calib_brier"], r["most_dfs_fav_brier"]))
        print("      buckets: %s" % bucket_str)
    return out

tour_all = df_hold["is_wta"].values
surf_all = df_hold["surface"].values
fav_a_all = df_hold["fav_dfs"].values
dog_a_all = df_hold["dog_dfs"].values


# ---------------- Candidate A: PRODUCTION (DFs dm=1e6, pooled fitted r) ----------------
def lookup_A(is_wta):
    return r_pooled_dfs, coefs.get("disp_mult", 1.0)

fav_A, dog_A, dt_A = simulate_pop_dfs(df_hold, lookup_A, seed=SEED)
print("Candidate A (production) sim time: %.1fs" % dt_A)
RESULTS["A_production"] = full_breakdown(fav_A, dog_A, fav_a_all, dog_a_all, tour_all, surf_all, "A: PRODUCTION (dm=1e6)")
del fav_A, dog_A

# ---------------- Candidate B: GLOBAL dm=0.5, pooled fitted r ----------------
def lookup_B(is_wta):
    return r_pooled_dfs, 0.5

fav_B, dog_B, dt_B = simulate_pop_dfs(df_hold, lookup_B, seed=SEED)
print("Candidate B (global dm=0.5) sim time: %.1fs" % dt_B)
RESULTS["B_global_0.5"] = full_breakdown(fav_B, dog_B, fav_a_all, dog_a_all, tour_all, surf_all, "B: GLOBAL dm=0.5")
del fav_B, dog_B

# ---------------- Candidate C: SEGMENTED ATP dm=0.75 / WTA dm=0.3, own r ----------------
def lookup_C(is_wta):
    return (r_wta_dfs, 0.3) if is_wta else (r_atp_dfs, 0.75)

fav_C, dog_C, dt_C = simulate_pop_dfs(df_hold, lookup_C, seed=SEED)
print("Candidate C (segmented, own r) sim time: %.1fs" % dt_C)
RESULTS["C_segmented"] = full_breakdown(fav_C, dog_C, fav_a_all, dog_a_all, tour_all, surf_all,
                                         "C: SEGMENTED (ATP dm=0.75 / WTA dm=0.3, own r)")
del fav_C, dog_C

md5_check("after")
with open("/path/to/home/openclaw/apex_v2_dfs_promotion_analysis_results.json", "w") as f:
    json.dump(RESULTS, f, indent=2, default=str)
print("\nWrote apex_v2_dfs_promotion_analysis_results.json")

try:
    print("\n" + "=" * 100)
    print("SUMMARY -- A vs B vs C, overall + by tour")
    print("=" * 100)
    for seg in ["overall", "atp", "wta"]:
        print("\n-- segment: %s --" % seg)
        for cand in ["A_production", "B_global_0.5", "C_segmented"]:
            r = RESULTS[cand][seg]
            print("  %-14s MAE=%.4f MSE=%.4f old_cov=%.3f maf_brier=%.4f  80%%+ gap=%+.3f"
                  % (cand, r["mae"], r["mse"], r["old_coverage"], r["most_dfs_fav_brier"],
                     r["buckets"].get("80%+", {}).get("gap", float("nan"))))
except Exception as e:
    print("Summary print failed (non-fatal, JSON already saved):", e)

print("\nDONE.")
