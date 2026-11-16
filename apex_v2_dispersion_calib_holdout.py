"""
apex_v2_dispersion_calib_holdout.py -- PART B

Confirms Part A's tune-window candidate region (dm~0.4-1.0, converging to
0.4 by tail-gap and 0.6-1.0 by Brier, with clear disagreement between ATP
and WTA) on the year>=2024 HOLDOUT -- data this candidate region was NOT
selected on. Runs POOLED / ATP-only / WTA-only separately, each with its
own fitted r, across a grid spanning the region of interest. Measures MAE,
MSE, calibration (total-aces line-Brier + old aggregate coverage, for
comparison), most_aces_fav Brier, and confidence-bucket gaps.

Read-only against the two frozen V1 files. MD5 checked before/after.
Nothing written to disk except this script's own result files. No
coefficient is promoted or deployed -- evidence only.
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
GRID = [1e6, 1.0, 0.75, 0.6, 0.5, 0.4, 0.3]
N_SIMS = 1000
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

pbins = [0.0, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
plabels = ["<55%", "55-60%", "60-65%", "65-70%", "70-75%", "75-80%", "80%+"]


def sweep_holdout(pop_df, r_fav, r_dog, label):
    print("\n" + "=" * 100)
    print("HOLDOUT CONFIRMATION SWEEP -- %s (n=%d, r_fav=%.3f r_dog=%.3f)" % (label, len(pop_df), r_fav, r_dog))
    print("=" * 100)
    fav_a = pop_df["fav_aces"].values; dog_a = pop_df["dog_aces"].values
    tie = fav_a == dog_a; actual_win = (fav_a > dog_a).astype(float); valid = ~tie
    out = {}
    for dm in GRID:
        coefs_cf = copy.deepcopy(coefs)
        coefs_cf["disp"]["fav"]["aces"] = r_fav
        coefs_cf["disp"]["dog"]["aces"] = r_dog
        coefs_cf["disp_mult"] = dm
        t0 = time.time()
        sims = sim.run_rows(pop_df, m, coefs_cf, N_SIMS, seed=1, progress=False)
        dt = time.time() - t0

        fav_samp = np.stack([s["fav"]["aces"] for s, _, _, _ in sims])
        dog_samp = np.stack([s["dog"]["aces"] for s, _, _, _ in sims])
        fav_mean = fav_samp.mean(axis=1); dog_mean = dog_samp.mean(axis=1)
        aces_actual = np.concatenate([fav_a, dog_a]).astype(float)
        aces_pred = np.concatenate([fav_mean, dog_mean])
        okv = np.isfinite(aces_actual) & np.isfinite(aces_pred)
        resid = aces_actual[okv] - aces_pred[okv]
        mae = float(np.mean(np.abs(resid))); mse = float(np.mean(resid ** 2))

        total_samp = fav_samp + dog_samp
        total_actual = fav_a + dog_a
        lines_ = np.floor(total_samp.mean(axis=1)) + 0.5
        phat = np.mean(total_samp > lines_[:, None], axis=1)
        actual_over = (total_actual > lines_).astype(float)
        okc = np.isfinite(total_actual)
        calib_brier = float(np.mean((phat[okc] - actual_over[okc]) ** 2))

        # old aggregate central-80%-band coverage metric, for direct comparison
        cov = []
        for i, (s, _, _, _) in enumerate(sims):
            for side, arr in (("fav", fav_a), ("dog", dog_a)):
                x = s[side]["aces"]; y = arr[i]
                if np.isfinite(y):
                    cov.append(np.percentile(x, 10) <= y <= np.percentile(x, 90))
        c80 = float(np.mean(cov))

        gt = fav_samp > dog_samp; ne = fav_samp != dog_samp
        pred_prob = gt.mean(axis=1) / np.maximum(ne.mean(axis=1), 1e-9)
        maf_brier = float(np.mean((pred_prob[valid] - actual_win[valid]) ** 2))
        buckets = {}
        for lo, hi, lab in zip(pbins[:-1], pbins[1:], plabels):
            mask = valid & (pred_prob >= lo) & (pred_prob < hi)
            n = int(mask.sum())
            if n == 0:
                continue
            buckets[lab] = {"n": n, "gap": float(np.mean(pred_prob[mask]) - np.mean(actual_win[mask]))}
        tail_score = abs(buckets.get("75-80%", {}).get("gap", 0)) + abs(buckets.get("80%+", {}).get("gap", 0))

        print("-- dm=%g (%.1fs) -- MAE=%.4f MSE=%.4f  old_coverage=%.3f  calib_brier=%.4f  most_aces_fav_brier=%.4f  tail|gap|sum=%.3f"
              % (dm, dt, mae, mse, c80, calib_brier, maf_brier, tail_score))
        print("   75-80%%=%+.3f (n=%d)   80%%+=%+.3f (n=%d)"
              % (buckets.get("75-80%", {}).get("gap", float("nan")), buckets.get("75-80%", {}).get("n", 0),
                 buckets.get("80%+", {}).get("gap", float("nan")), buckets.get("80%+", {}).get("n", 0)))

        out[str(dm)] = {"mae": mae, "mse": mse, "old_coverage": c80, "calib_brier": calib_brier,
                        "most_aces_fav_brier": maf_brier, "tail_score": tail_score, "buckets": buckets}
        del fav_samp, dog_samp, total_samp
    return out

RESULTS = {}
RESULTS["pooled"] = sweep_holdout(df_hold, r_pooled["fav"], r_pooled["dog"], "POOLED (production r)")
RESULTS["atp"] = sweep_holdout(df_hold_atp, r_atp["fav"], r_atp["dog"], "ATP-only (ATP-specific r)")
RESULTS["wta"] = sweep_holdout(df_hold_wta, r_wta["fav"], r_wta["dog"], "WTA-only (WTA-specific r)")


md5_check("after")
with open("/path/to/home/openclaw/apex_v2_dispersion_calib_holdout_results.json", "w") as f:
    json.dump(RESULTS, f, indent=2, default=str)
print("\nWrote apex_v2_dispersion_calib_holdout_results.json")

try:
    print("\n" + "=" * 100)
    print("SUMMARY -- best dm by Brier and by tail score, per population, on HOLDOUT")
    print("=" * 100)
    for label in ("pooled", "atp", "wta"):
        r = RESULTS[label]
        best_brier = min(GRID, key=lambda dm: r[str(dm)]["most_aces_fav_brier"])
        best_tail = min(GRID, key=lambda dm: r[str(dm)]["tail_score"])
        mae_at_1e6 = r[str(1e6)]["mae"]
        mae_at_best = r[str(best_brier)]["mae"]
        print("%-8s best_by_brier=%-6s (MAE %.4f -> %.4f, delta %+.4f)   best_by_tail=%-6s"
              % (label, best_brier, mae_at_1e6, mae_at_best, mae_at_best - mae_at_1e6, best_tail))
except Exception as e:
    print("Summary print failed (non-fatal, JSON already saved):", e)

print("\nDONE.")
