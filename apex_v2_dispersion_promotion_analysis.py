"""
apex_v2_dispersion_promotion_analysis.py

PROMOTION ANALYSIS for Ace dispersion calibration -- diagnostic only.
Do NOT modify production files. Do NOT deploy. Evidence only.

Compares three candidates on the untouched year>=2024 holdout:
  A) PRODUCTION      disp_mult=1,000,000, pooled fitted r (fav=7.158, dog=4.872)
  B) GLOBAL          disp_mult=0.5, pooled fitted r (unchanged r, only disp_mult lowered)
  C) SEGMENTED       ATP: its own fitted r, disp_mult=0.6
                     WTA: its own fitted r, disp_mult=0.4

For each candidate, reports (overall + by tour [ATP/WTA] + by surface
[Hard/Clay/Grass]):
  - MAE, MSE (mean-projection accuracy -- must stay flat)
  - old aggregate 80%-band coverage (for continuity with the original metric)
  - calibration Brier (total-aces line)
  - most_aces_fav Brier
  - full confidence-bucket gap table: 55-60/60-65/65-70/70-75/75-80/80%+

Only 4 simulation passes are needed total (A: 1 pooled pass, B: 1 pooled
pass, C: 1 ATP pass + 1 WTA pass) -- all downstream breakdowns (by tour,
by surface, by bucket) are computed by masking the same simulated arrays,
no extra simulation.

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

FIT_END, TUNE_START, TUNE_END, HOLD_START = 2021, 2022, 2023, 2024
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

def moment_r(mask, side):
    _, hf_, hd_, mu_ = sim.v1_inputs_from_rows(fit_df, m)
    lam = mu_[side][0][mask] * fit_df.loc[mask, "%s_svgms" % side].values
    y = fit_df.loc[mask, "%s_aces" % side].values
    mk = np.isfinite(lam) & np.isfinite(y) & (lam > 0)
    excess = np.mean((y[mk] - lam[mk]) ** 2) - np.mean(lam[mk])
    return float(np.mean(lam[mk]) ** 2 / excess) if excess > 0 else float("inf")

r_pooled = {"fav": coefs["disp"]["fav"]["aces"], "dog": coefs["disp"]["dog"]["aces"]}
r_atp = {"fav": moment_r((fit_df["is_wta"] == 0).values, "fav"), "dog": moment_r((fit_df["is_wta"] == 0).values, "dog")}
r_wta = {"fav": moment_r((fit_df["is_wta"] == 1).values, "fav"), "dog": moment_r((fit_df["is_wta"] == 1).values, "dog")}

df_hold_atp = df_hold[df_hold["is_wta"] == 0].reset_index(drop=True)
df_hold_wta = df_hold[df_hold["is_wta"] == 1].reset_index(drop=True)
print("holdout n_pooled=%d  n_atp=%d  n_wta=%d" % (len(df_hold), len(df_hold_atp), len(df_hold_wta)))
print("surface counts (pooled holdout):")
print(df_hold["surface"].value_counts().to_string())
print("r_pooled=%s  r_atp=%s  r_wta=%s" % (r_pooled, r_atp, r_wta))

pbins = [0.0, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
plabels = ["<55%", "55-60%", "60-65%", "65-70%", "70-75%", "75-80%", "80%+"]


def simulate_pop(pop_df, coefs_used, seed=SEED):
    t0 = time.time()
    sims = sim.run_rows(pop_df, m, coefs_used, N_SIMS, seed=seed, progress=False)
    dt = time.time() - t0
    fav_samp = np.stack([s["fav"]["aces"] for s, _, _, _ in sims])
    dog_samp = np.stack([s["dog"]["aces"] for s, _, _, _ in sims])
    return fav_samp, dog_samp, dt


def metrics_for_mask(fav_samp, dog_samp, fav_a, dog_a, mask, label):
    if mask.sum() == 0:
        return None
    fs = fav_samp[mask]; ds = dog_samp[mask]
    fa = fav_a[mask]; da = dog_a[mask]
    fav_mean = fs.mean(axis=1); dog_mean = ds.mean(axis=1)
    aces_actual = np.concatenate([fa, da]).astype(float)
    aces_pred = np.concatenate([fav_mean, dog_mean])
    okv = np.isfinite(aces_actual) & np.isfinite(aces_pred)
    resid = aces_actual[okv] - aces_pred[okv]
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
        "calib_brier": calib_brier, "most_aces_fav_brier": maf_brier,
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
              % (r["label"], r["n"], r["mae"], r["mse"], r["old_coverage"], r["calib_brier"], r["most_aces_fav_brier"]))
        print("      buckets: %s" % bucket_str)
    return out


# ---------------- Candidate A: PRODUCTION ----------------
fav_A, dog_A, dt_A = simulate_pop(df_hold, coefs, seed=SEED)
print("Candidate A (production) sim time: %.1fs" % dt_A)
tour_A = df_hold["is_wta"].values
surf_A = df_hold["surface"].values
RESULTS["A_production"] = full_breakdown(fav_A, dog_A, df_hold["fav_aces"].values, df_hold["dog_aces"].values,
                                          tour_A, surf_A, "A: PRODUCTION (dm=1e6)")
del fav_A, dog_A

# ---------------- Candidate B: GLOBAL dm=0.5 ----------------
coefs_B = copy.deepcopy(coefs)
coefs_B["disp_mult"] = 0.5
fav_B, dog_B, dt_B = simulate_pop(df_hold, coefs_B, seed=SEED)
print("Candidate B (global dm=0.5) sim time: %.1fs" % dt_B)
RESULTS["B_global_0.5"] = full_breakdown(fav_B, dog_B, df_hold["fav_aces"].values, df_hold["dog_aces"].values,
                                          tour_A, surf_A, "B: GLOBAL dm=0.5")
del fav_B, dog_B

# ---------------- Candidate C: SEGMENTED ATP=0.6 / WTA=0.4 ----------------
coefs_C_atp = copy.deepcopy(coefs)
coefs_C_atp["disp"]["fav"]["aces"] = r_atp["fav"]; coefs_C_atp["disp"]["dog"]["aces"] = r_atp["dog"]
coefs_C_atp["disp_mult"] = 0.6
fav_Ca, dog_Ca, dt_Ca = simulate_pop(df_hold_atp, coefs_C_atp, seed=SEED)
print("Candidate C ATP-part (dm=0.6, own r) sim time: %.1fs" % dt_Ca)

coefs_C_wta = copy.deepcopy(coefs)
coefs_C_wta["disp"]["fav"]["aces"] = r_wta["fav"]; coefs_C_wta["disp"]["dog"]["aces"] = r_wta["dog"]
coefs_C_wta["disp_mult"] = 0.4
fav_Cw, dog_Cw, dt_Cw = simulate_pop(df_hold_wta, coefs_C_wta, seed=SEED)
print("Candidate C WTA-part (dm=0.4, own r) sim time: %.1fs" % dt_Cw)

fav_C = np.concatenate([fav_Ca, fav_Cw], axis=0)
dog_C = np.concatenate([dog_Ca, dog_Cw], axis=0)
fav_a_C = np.concatenate([df_hold_atp["fav_aces"].values, df_hold_wta["fav_aces"].values])
dog_a_C = np.concatenate([df_hold_atp["dog_aces"].values, df_hold_wta["dog_aces"].values])
tour_C = np.concatenate([df_hold_atp["is_wta"].values, df_hold_wta["is_wta"].values])
surf_C = np.concatenate([df_hold_atp["surface"].values, df_hold_wta["surface"].values])
RESULTS["C_segmented"] = full_breakdown(fav_C, dog_C, fav_a_C, dog_a_C, tour_C, surf_C,
                                         "C: SEGMENTED (ATP dm=0.6 / WTA dm=0.4, own r)")
del fav_Ca, dog_Ca, fav_Cw, dog_Cw, fav_C, dog_C

md5_check("after")
with open("/path/to/home/openclaw/apex_v2_dispersion_promotion_analysis_results.json", "w") as f:
    json.dump(RESULTS, f, indent=2, default=str)
print("\nWrote apex_v2_dispersion_promotion_analysis_results.json")

try:
    print("\n" + "=" * 100)
    print("SUMMARY -- A vs B vs C, overall + by tour")
    print("=" * 100)
    for seg in ["overall", "atp", "wta"]:
        print("\n-- segment: %s --" % seg)
        for cand in ["A_production", "B_global_0.5", "C_segmented"]:
            r = RESULTS[cand][seg]
            print("  %-14s MAE=%.4f MSE=%.4f old_cov=%.3f maf_brier=%.4f  80%%+ gap=%+.3f"
                  % (cand, r["mae"], r["mse"], r["old_coverage"], r["most_aces_fav_brier"],
                     r["buckets"].get("80%+", {}).get("gap", float("nan"))))
except Exception as e:
    print("Summary print failed (non-fatal, JSON already saved):", e)

print("\nDONE.")
