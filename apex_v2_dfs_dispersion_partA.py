"""
apex_v2_dfs_dispersion_partA.py -- STEPS 1-4 of the DFs investigation.

Per explicit user instruction (2026-09-26): run the identical process used
for Aces, this time on Double Faults, to determine whether DFs have the
same dispersion-calibration issue aces had before Candidate C.

  Step 1: Historical decomposition -- fitted (method-of-moments) r for DFs
          vs. production's r, pooled and tour-specific, on fit_df (<=2021).
  Step 2: Confidence buckets -- most_dfs_fav (derived the same way
          most_aces_fav is derived inside apex_prop_sim_v1.py, just done
          here in post-processing since the simulator itself doesn't
          expose a most_dfs_fav category) under CURRENT PRODUCTION
          settings (dm=1e6, i.e. near-Poisson for DFs; aces held at its
          actual live Candidate-C tour-specific settings) -- year>=2024
          holdout.
  Step 3: Variance audit -- per-row residual-based excess-variance check
          (the same quantity moment_r solves for), not the flawed pooled
          var_ratio used in the earlier full-category audit.
  Step 4: Tune window (2022-2023) grid sweep over DFs disp_mult, own
          fitted r, exactly mirroring apex_v2_dispersion_calib_tune.py's
          methodology -- aces held fixed at its live settings throughout.

Read-only against the two frozen V1 files. MD5-checked before/after.
No coefficient written back to production. Diagnostics only.
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

FIT_END, TUNE_START, TUNE_END, HOLD_START = 2021, 2022, 2023, 2024
SACKMANN_DIR = "ATP & WTA, 1968-2026"
RESULTS = {}


# ------------------------------------------------------------------
# Standard data pipeline (identical to every prior script).
# ------------------------------------------------------------------
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
print("fit_df=%d holdout=%d (ATP=%d WTA=%d)" % (len(fit_df), len(df_hold), len(df_hold_atp), len(df_hold_wta)))

# Live production Candidate-C aces settings (unchanged here -- this
# investigation only varies DFs).
R_ATP_ACES = {"fav": 8.70148205269834, "dog": 5.885645957929439}; DM_ATP_ACES = 0.6
R_WTA_ACES = {"fav": 3.9866756061964232, "dog": 2.7971092624925418}; DM_WTA_ACES = 0.4

# ------------------------------------------------------------------
# STEP 1 -- Historical decomposition: method-of-moments r for DFs,
# generalized from the exact formula in apex_v2_dispersion_calib_tune.py
# (there hard-coded to "aces"; here parameterized by category).
# ------------------------------------------------------------------
def moment_r(base_df, mask, side, cat):
    _, hf_, hd_, mu_ = sim.v1_inputs_from_rows(base_df, m)
    idx = {"aces": 0, "dfs": 1}[cat]
    lam = mu_[side][idx][mask] * base_df.loc[mask, "%s_svgms" % side].values
    y = base_df.loc[mask, "%s_%s" % (side, cat)].values
    mk = np.isfinite(lam) & np.isfinite(y) & (lam > 0)
    excess = np.mean((y[mk] - lam[mk]) ** 2) - np.mean(lam[mk])
    r = float(np.mean(lam[mk]) ** 2 / excess) if excess > 0 else float("inf")
    return r, float(excess), float(np.mean(lam[mk])), float(mk.sum())

atp_mask = (fit_df["is_wta"] == 0).values
wta_mask = (fit_df["is_wta"] == 1).values

r_pooled_dfs = {"fav": coefs["disp"]["fav"]["dfs"], "dog": coefs["disp"]["dog"]["dfs"]}
r_atp_dfs = {}; r_wta_dfs = {}
STEP1 = {"production_r_dfs": r_pooled_dfs}
for side in ("fav", "dog"):
    r, excess, lam_mean, n = moment_r(fit_df, atp_mask, side, "dfs")
    r_atp_dfs[side] = r
    STEP1["atp_%s" % side] = {"fitted_r": r, "excess_var": excess, "mean_lam": lam_mean, "n": n}
    r, excess, lam_mean, n = moment_r(fit_df, wta_mask, side, "dfs")
    r_wta_dfs[side] = r
    STEP1["wta_%s" % side] = {"fitted_r": r, "excess_var": excess, "mean_lam": lam_mean, "n": n}

print("\n=== STEP 1: HISTORICAL DECOMPOSITION (DFs, fit_df year<=2021) ===")
print("production r (pooled, currently applied at disp_mult=%g): fav=%.4f dog=%.4f" % (coefs.get("disp_mult"), r_pooled_dfs["fav"], r_pooled_dfs["dog"]))
print("own fitted r -- ATP: fav=%.4f dog=%.4f" % (r_atp_dfs["fav"], r_atp_dfs["dog"]))
print("own fitted r -- WTA: fav=%.4f dog=%.4f" % (r_wta_dfs["fav"], r_wta_dfs["dog"]))
for k in ("atp_fav", "atp_dog", "wta_fav", "wta_dog"):
    d = STEP1[k]
    print("  %-8s excess_var=%+.4f  mean_lam=%.4f  n=%d  %s" % (k, d["excess_var"], d["mean_lam"], d["n"],
          "OVERDISPERSED (excess>0)" if d["excess_var"] > 0 else "no excess found (<=Poisson)"))
RESULTS["step1_historical_decomposition"] = STEP1


# ------------------------------------------------------------------
# Custom row runner: aces fixed at live tour-specific Candidate C;
# DFs r/dm supplied by the caller (production baseline, or a grid
# candidate). Mirrors run_rows_candidate() from
# apex_full_category_calibration_audit.py, generalized for DFs.
# ------------------------------------------------------------------
import math, sys

def run_rows_dfs_candidate(df_sub, r_aces, dm_aces, r_dfs, dm_dfs, n_sims, seed=1):
    P, hf, hd, mu = sim.v1_inputs_from_rows(df_sub, m)
    pl = sim.price_level_pred(df_sub, coefs)
    rows = []; t0 = time.time()
    for i in range(len(df_sub)):
        if i and i % 1000 == 0:
            print("  ... %d/%d (%.0fs)" % (i, len(df_sub), time.time() - t0)); sys.stdout.flush()
        lv = sim.level_from(hf[i], hd[i], coefs, pl[i])
        bo = int(df_sub["best_of"].values[i])
        t, flag = sim.solve_tilt(lv, df_sub["pinn_fav"].values[i], coefs["tb_k"], bo, sigma=coefs.get("gap_sigma", 0.0))
        hA = min(max(lv + t, 0.2), 0.995); hB = min(max(lv - t, 0.2), 0.995)
        s = sim.simulate(
            hA, hB, coefs["tb_k"], bo,
            (mu["fav"][0][i], mu["fav"][1][i]), (mu["dog"][0][i], mu["dog"][1][i]),
            (r_aces["fav"] * dm_aces, r_dfs["fav"] * dm_dfs),
            (r_aces["dog"] * dm_aces, r_dfs["dog"] * dm_dfs),
            n_sims, seed=seed + i, sigma=coefs.get("gap_sigma", 0.0),
        )
        rows.append((s, hA, hB, flag))
    return rows

def aces_for_tour(is_wta):
    return (R_WTA_ACES, DM_WTA_ACES) if is_wta else (R_ATP_ACES, DM_ATP_ACES)

def most_dfs_fav_pred(sims):
    n = len(sims)
    out = np.full(n, np.nan)
    for i, (s, _, _, _) in enumerate(sims):
        fa = s["fav"]["dfs"]; da = s["dog"]["dfs"]
        pos = np.mean(fa > da); neg = np.mean(fa < da)
        d = pos + neg
        out[i] = pos / d if d > 0 else np.nan
    return out

def bucket_gaps(pred, actual):
    pbins = [0.0, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
    plabels = ["<55%", "55-60%", "60-65%", "65-70%", "70-75%", "75-80%", "80%+"]
    ok = np.isfinite(pred) & np.isfinite(actual)
    p, a = pred[ok], actual[ok]
    brier = float(np.mean((p - a) ** 2)) if len(p) else float("nan")
    idx = np.digitize(p, pbins) - 1
    out = {}
    for bi, lab in enumerate(plabels):
        mk = idx == bi; nb = int(mk.sum())
        if nb == 0:
            continue
        out[lab] = {"n": nb, "pred": float(p[mk].mean()), "act": float(a[mk].mean()), "gap": float(p[mk].mean() - a[mk].mean())}
    return brier, out, int(len(p))


# ------------------------------------------------------------------
# STEP 2 -- Confidence buckets for most_dfs_fav under CURRENT
# PRODUCTION DFs settings (own fitted-at-build-time r, dm=1e6, i.e.
# effectively Poisson -- exactly analogous to aces' pre-fix state).
# year>=2024 holdout, 1000 sims/match, aces held at live Candidate C.
# ------------------------------------------------------------------
N_SIMS = 1000
DM_PROD = coefs.get("disp_mult", 1.0)

print("\n=== STEP 2: CONFIDENCE BUCKETS, most_dfs_fav, PRODUCTION DFs settings (dm=%g) ===" % DM_PROD)
print("Running ATP holdout (n=%d)..." % len(df_hold_atp))
sims_atp_prod = run_rows_dfs_candidate(df_hold_atp, R_ATP_ACES, DM_ATP_ACES, r_pooled_dfs, DM_PROD, N_SIMS, seed=1)
print("Running WTA holdout (n=%d)..." % len(df_hold_wta))
sims_wta_prod = run_rows_dfs_candidate(df_hold_wta, R_WTA_ACES, DM_WTA_ACES, r_pooled_dfs, DM_PROD, N_SIMS, seed=1)

pred_atp = most_dfs_fav_pred(sims_atp_prod)
pred_wta = most_dfs_fav_pred(sims_wta_prod)
fa_atp, da_atp = df_hold_atp["fav_dfs"].values, df_hold_atp["dog_dfs"].values
fa_wta, da_wta = df_hold_wta["fav_dfs"].values, df_hold_wta["dog_dfs"].values
act_atp = np.where(fa_atp > da_atp, 1.0, np.where(fa_atp < da_atp, 0.0, np.nan))
act_wta = np.where(fa_wta > da_wta, 1.0, np.where(fa_wta < da_wta, 0.0, np.nan))

pred_all = np.concatenate([pred_atp, pred_wta])
act_all = np.concatenate([act_atp, act_wta])

STEP2 = {}
for label, p, a in (("pooled", pred_all, act_all), ("ATP", pred_atp, act_atp), ("WTA", pred_wta, act_wta)):
    brier, buckets, n = bucket_gaps(p, a)
    STEP2[label] = {"brier": brier, "buckets": buckets, "n": n}
    print("  %-8s brier=%.4f n=%d" % (label, brier, n))
    for lab, d in buckets.items():
        print("      %-8s n=%-5d pred=%.3f act=%.3f gap=%+.3f" % (lab, d["n"], d["pred"], d["act"], d["gap"]))
RESULTS["step2_confidence_buckets_production"] = STEP2


# ------------------------------------------------------------------
# STEP 3 -- Variance audit: does the same excess-variance (overdisper-
# sion) signal found in fit_df for DFs also show up, independently, in
# the untouched year>=2024 holdout? (Same residual-based diagnostic as
# Step 1 / moment_r -- NOT the flawed pooled var_ratio used earlier.)
# ------------------------------------------------------------------
atp_hold_mask = (df_hold["is_wta"] == 0).values
wta_hold_mask = (df_hold["is_wta"] == 1).values
STEP3 = {}
print("\n=== STEP 3: VARIANCE AUDIT (excess variance vs Poisson, DFs, HOLDOUT year>=2024) ===")
for side in ("fav", "dog"):
    r, excess, lam_mean, n = moment_r(df_hold, atp_hold_mask, side, "dfs")
    STEP3["holdout_atp_%s" % side] = {"fitted_r": r, "excess_var": excess, "mean_lam": lam_mean, "n": n}
    print("  holdout ATP %-4s fitted_r=%.4f excess_var=%+.4f mean_lam=%.4f n=%d  fit_df_r=%.4f"
          % (side, r, excess, lam_mean, n, r_atp_dfs[side]))
    r, excess, lam_mean, n = moment_r(df_hold, wta_hold_mask, side, "dfs")
    STEP3["holdout_wta_%s" % side] = {"fitted_r": r, "excess_var": excess, "mean_lam": lam_mean, "n": n}
    print("  holdout WTA %-4s fitted_r=%.4f excess_var=%+.4f mean_lam=%.4f n=%d  fit_df_r=%.4f"
          % (side, r, excess, lam_mean, n, r_wta_dfs[side]))
RESULTS["step3_variance_audit_holdout"] = STEP3
print("  (For reference, production currently applies r*dm = r*%g -- i.e. effectively Poisson/no overdispersion.)" % DM_PROD)


# ------------------------------------------------------------------
# STEP 4 -- Tune window (2022-2023) grid sweep over DFs disp_mult,
# own fitted r, mirroring apex_v2_dispersion_calib_tune.py exactly
# (same GRID, same sample cap, same tail-score metric), just applied
# to most_dfs_fav instead of most_aces_fav, and with aces held fixed
# at its actual live settings rather than swept jointly.
# ------------------------------------------------------------------
r_all_dfs = {}
all_mask = np.ones(len(fit_df), dtype=bool)
for side in ("fav", "dog"):
    r, _, _, _ = moment_r(fit_df, all_mask, side, "dfs")
    r_all_dfs[side] = r
print("\nown fitted r -- POOLED (all tours): fav=%.4f dog=%.4f" % (r_all_dfs["fav"], r_all_dfs["dog"]))

GRID = [1e6, 10, 4, 2, 1, 0.75, 0.6, 0.5, 0.4, 0.3, 0.25, 0.2, 0.15, 0.1]

tune = df[(df["year"] >= TUNE_START) & (df["year"] <= TUNE_END)]
tune = tune.sample(min(len(tune), 1000), random_state=11) if len(tune) > 1000 else tune
tune = tune.reset_index(drop=True)
tune_atp = tune[tune["is_wta"] == 0].reset_index(drop=True)
tune_wta = tune[tune["is_wta"] == 1].reset_index(drop=True)
print("tune n_pooled=%d n_atp=%d n_wta=%d" % (len(tune), len(tune_atp), len(tune_wta)))

def sweep_dfs(pop_df, r_dfs, label, n_sims=300, seed=6):
    print("\n" + "=" * 100)
    print("DFs TUNE SWEEP -- %s (n=%d, r_fav=%.3f r_dog=%.3f)" % (label, len(pop_df), r_dfs["fav"], r_dfs["dog"]))
    print("=" * 100)
    fa = pop_df["fav_dfs"].values; da = pop_df["dog_dfs"].values
    tie = fa == da; actual = (fa > da).astype(float); valid = ~tie
    is_wta_col = pop_df["is_wta"].values
    out = {}
    for dm in GRID:
        preds = np.full(len(pop_df), np.nan)
        for wta_flag in (0, 1):
            sub_idx = np.where(is_wta_col == wta_flag)[0]
            if len(sub_idx) == 0:
                continue
            sub_df = pop_df.iloc[sub_idx].reset_index(drop=True)
            r_aces, dm_aces = aces_for_tour(bool(wta_flag))
            sims = run_rows_dfs_candidate(sub_df, r_aces, dm_aces, r_dfs, dm, n_sims, seed=seed)
            p = most_dfs_fav_pred(sims)
            preds[sub_idx] = p
        brier, buckets, n = bucket_gaps(preds[valid], actual[valid])
        tail = abs(buckets.get("75-80%", {}).get("gap", 0)) + abs(buckets.get("80%+", {}).get("gap", 0))
        print("  dm=%-8g  Brier=%.4f  n=%d  tail|gap|sum=%.3f  75-80%%=%+.3f  80%%+=%+.3f"
              % (dm, brier, n, tail, buckets.get("75-80%", {}).get("gap", float("nan")),
                 buckets.get("80%+", {}).get("gap", float("nan"))))
        out[str(dm)] = {"brier": brier, "n": n, "tail_score": tail, "buckets": buckets}
    best_by_brier = min(GRID, key=lambda dm: out[str(dm)]["brier"])
    best_by_tail = min(GRID, key=lambda dm: out[str(dm)]["tail_score"])
    print("  => best by Brier: dm=%g   best by tail: dm=%g" % (best_by_brier, best_by_tail))
    out["_best_by_brier"] = best_by_brier
    out["_best_by_tail"] = best_by_tail
    return out

RESULTS["step4_tune"] = {}
RESULTS["step4_tune"]["pooled"] = sweep_dfs(tune, r_all_dfs, "POOLED")
RESULTS["step4_tune"]["atp"] = sweep_dfs(tune_atp, r_atp_dfs, "ATP-only")
RESULTS["step4_tune"]["wta"] = sweep_dfs(tune_wta, r_wta_dfs, "WTA-only")


md5_check("after")
with open("/path/to/home/openclaw/apex_v2_dfs_dispersion_partA_results.json", "w") as f:
    json.dump(RESULTS, f, indent=2, default=str)
print("\nWrote apex_v2_dfs_dispersion_partA_results.json")
print("\nDONE PART A.")
