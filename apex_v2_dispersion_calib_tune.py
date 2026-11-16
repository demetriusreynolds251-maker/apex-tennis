"""
apex_v2_dispersion_calib_tune.py -- PART A

Per explicit user instruction (2026-09-26 19:11 CT): build a
CALIBRATION-FOCUSED dispersion tuning pass, on the TUNE window
(2022-2023, matching original tune_alpha() methodology), replacing the
aggregate-central-80%-band-coverage objective (proven wrong last turn)
with a proper scoring rule (most_aces_fav Brier) plus explicit tail
bucket gaps. Run separately for POOLED / ATP-only / WTA-only, each using
its own fitted dispersion r, across a finer grid than the original 5
points. This is Part A (fast, tune window only, n_sims=300) -- Part B
(apex_v2_dispersion_calib_holdout.py) confirms the winning region on the
year>=2024 holdout, which this script does NOT touch or select on.

Read-only against the two frozen V1 files. MD5 checked before/after.
No coefficient is written back to disk. Evidence only -- no deployment.
"""
import copy
import hashlib
import json
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
GRID = [1e6, 10, 4, 2, 1, 0.75, 0.6, 0.5, 0.4, 0.3, 0.25, 0.2, 0.15, 0.1]
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
print("r_pooled:", r_pooled)
print("r_atp:   ", r_atp)
print("r_wta:   ", r_wta)
RESULTS["fitted_r"] = {"pooled": r_pooled, "atp": r_atp, "wta": r_wta}

tune = df[(df["year"] >= TUNE_START) & (df["year"] <= TUNE_END)]
tune = tune.sample(min(len(tune), 1000), random_state=11) if len(tune) > 1000 else tune
tune = tune.reset_index(drop=True)
tune_atp = tune[tune["is_wta"] == 0].reset_index(drop=True)
tune_wta = tune[tune["is_wta"] == 1].reset_index(drop=True)
print("tune n_pooled=%d  n_atp=%d  n_wta=%d" % (len(tune), len(tune_atp), len(tune_wta)))


pbins = [0.0, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
plabels = ["<55%", "55-60%", "60-65%", "65-70%", "70-75%", "75-80%", "80%+"]

def sweep_population(pop_df, r_fav, r_dog, label, n_sims=300, seed=6):
    print("\n" + "=" * 100)
    print("CALIBRATION-FOCUSED TUNE SWEEP -- %s (n=%d, r_fav=%.3f r_dog=%.3f)" % (label, len(pop_df), r_fav, r_dog))
    print("=" * 100)
    fav_a = pop_df["fav_aces"].values; dog_a = pop_df["dog_aces"].values
    tie = fav_a == dog_a; actual_win = (fav_a > dog_a).astype(float); valid = ~tie
    out = {}
    for dm in GRID:
        coefs_cf = copy.deepcopy(coefs)
        coefs_cf["disp"]["fav"]["aces"] = r_fav
        coefs_cf["disp"]["dog"]["aces"] = r_dog
        coefs_cf["disp_mult"] = dm
        sims = sim.run_rows(pop_df, m, coefs_cf, n_sims, seed=seed, progress=False)
        cov = []
        for (s, _, _, _), (_, row) in zip(sims, pop_df.iterrows()):
            for side in ("fav", "dog"):
                for cat in ("aces", "dfs"):
                    x = s[side][cat]; y = row["%s_%s" % (side, cat)]
                    if np.isfinite(y):
                        cov.append(np.percentile(x, 10) <= y <= np.percentile(x, 90))
        c80 = float(np.mean(cov))
        fav_samp = np.stack([s["fav"]["aces"] for s, _, _, _ in sims])
        dog_samp = np.stack([s["dog"]["aces"] for s, _, _, _ in sims])
        gt = fav_samp > dog_samp; ne = fav_samp != dog_samp
        pred_prob = gt.mean(axis=1) / np.maximum(ne.mean(axis=1), 1e-9)
        brier = float(np.mean((pred_prob[valid] - actual_win[valid]) ** 2))
        buckets = {}
        for lo, hi, lab in zip(pbins[:-1], pbins[1:], plabels):
            mask = valid & (pred_prob >= lo) & (pred_prob < hi)
            n = int(mask.sum())
            if n == 0:
                continue
            buckets[lab] = {"n": n, "gap": float(np.mean(pred_prob[mask]) - np.mean(actual_win[mask]))}
        tail_score = abs(buckets.get("75-80%", {}).get("gap", 0)) + abs(buckets.get("80%+", {}).get("gap", 0))
        print("  dm=%-8g  coverage=%.3f  Brier=%.4f  tail|gap|sum=%.3f  75-80%%=%+.3f  80%%+=%+.3f"
              % (dm, c80, brier, tail_score, buckets.get("75-80%", {}).get("gap", float("nan")),
                 buckets.get("80%+", {}).get("gap", float("nan"))))
        out[str(dm)] = {"coverage": c80, "brier": brier, "tail_score": tail_score, "buckets": buckets}
        del fav_samp, dog_samp
    best_by_coverage = min(GRID, key=lambda dm: abs(out[str(dm)]["coverage"] - 0.80))
    best_by_brier = min(GRID, key=lambda dm: out[str(dm)]["brier"])
    best_by_tail = min(GRID, key=lambda dm: out[str(dm)]["tail_score"])
    print("  => best by OLD metric (coverage->0.80): dm=%g" % best_by_coverage)
    print("  => best by Brier (proper scoring rule): dm=%g" % best_by_brier)
    print("  => best by tail |gap| sum:              dm=%g" % best_by_tail)
    out["_best_by_coverage"] = best_by_coverage
    out["_best_by_brier"] = best_by_brier
    out["_best_by_tail"] = best_by_tail
    return out

RESULTS["pooled"] = sweep_population(tune, r_pooled["fav"], r_pooled["dog"], "POOLED")
RESULTS["atp"] = sweep_population(tune_atp, r_atp["fav"], r_atp["dog"], "ATP-only")
RESULTS["wta"] = sweep_population(tune_wta, r_wta["fav"], r_wta["dog"], "WTA-only")


md5_check("after")
with open("/path/to/home/openclaw/apex_v2_dispersion_calib_tune_results.json", "w") as f:
    json.dump(RESULTS, f, indent=2, default=str)
print("\nWrote apex_v2_dispersion_calib_tune_results.json")

try:
    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    for label in ("pooled", "atp", "wta"):
        r = RESULTS[label]
        print("%-8s best_by_coverage(OLD)=%-8s best_by_brier=%-8s best_by_tail=%-8s"
              % (label, r["_best_by_coverage"], r["_best_by_brier"], r["_best_by_tail"]))
except Exception as e:
    print("Summary print failed (non-fatal, JSON already saved):", e)

print("\nDONE.")
