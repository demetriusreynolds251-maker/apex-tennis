#!/usr/bin/env python3
"""
apex_fragility_v3.py -- APEX-FRAGILITY-V3-2026-08-20

Answers six questions about the Fragility signal, anchored to Pinnacle price:
  1/2. Bucket tables restricted to favorites >=60%, >=65%, >=70%, >=75%
       (plus the all-favorites view), for BOTH models.
  3.   Component contributions estimated on the HOLDOUT YEARS ONLY (2024+).
  4.   Fragility V3 = only elo/market gap + surface switch + age gap.
  5.   V2 vs V3 comparison: bucket separation (top-minus-bottom excess),
       pooled trend z, holdout behavior -- at every threshold.
  6.   Favorite-loss diagnostic: actual upset rates by fragility bucket,
       band-by-bucket grid, and where the big (75%+) upsets landed.

NOT a winner model. The question is only: does the score separate favorites
into groups that under/over-perform Pinnacle's implied expectation?

HONESTY NOTE PRINTED IN THE REPORT: V3's components were chosen because they
replicated on 2024+, so 2024+ flatters V3 by construction. This comparison
can lean; only the live forward log can decide.

Run:   python3 apex_fragility_v3.py --run --rows apex_batch_a_rows.csv \
           --prob-col pinn_a --sackmann-dir "ATP & WTA, 1968-2026"
Test:  python3 apex_fragility_v3.py --selftest
"""
import argparse, json, math, os, glob, re
import numpy as np
import pandas as pd

MARKER = "APEX-FRAGILITY-V3-2026-08-20"
BANDS = [(0.50, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 1.001)]
BAND_NAMES = ["50-60%", "60-65%", "65-70%", "70-75%", "75-80%", "80%+"]
BUCKETS = [(0, 20), (21, 40), (41, 60), (61, 80), (81, 100)]
THRESHOLDS = [("all favorites", 0.50), (">=60%", 0.60), (">=65%", 0.65),
              (">=70%", 0.70), (">=75%", 0.75)]
LN10_400 = math.log(10) / 400.0
V2_COMPONENTS = ["elo_gap", "d_surface_switch", "d_age", "d_matches_14d", "d_rank",
                 "d_rank_pts", "is_slam", "is_small_event", "is_bo5", "is_qual_dog"]
V3_COMPONENTS = ["elo_gap", "d_surface_switch", "d_age"]


def logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def irls_logistic(X, y, ridge=1e-3, iters=80):
    X = np.asarray(X, float); y = np.asarray(y, float)
    beta = np.zeros(X.shape[1]); H = np.eye(X.shape[1])
    for _ in range(iters):
        eta = np.clip(X @ beta, -30, 30)
        mu = 1 / (1 + np.exp(-eta))
        w = np.maximum(mu * (1 - mu), 1e-9)
        H = (X * w[:, None]).T @ X + ridge * np.eye(X.shape[1])
        g = X.T @ (y - mu) - ridge * beta
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            return None, None
        beta = beta + step
        if np.max(np.abs(step)) < 1e-8:
            break
    try:
        se = np.sqrt(np.maximum(np.diag(np.linalg.inv(H)), 0))
    except np.linalg.LinAlgError:
        return None, None
    return beta, se


def norm_sf(z):
    from scipy.stats import norm
    return float(norm.sf(z))


def norm_name(s):
    return " ".join(sorted(re.sub(r"[^a-z ]", "", str(s).lower().strip()).split()))


def to_favorite_frame(df, pcol):
    d = df.copy()
    fav_is_a = d[pcol] >= 0.5
    d["fav_p"] = np.where(fav_is_a, d[pcol], 1 - d[pcol])
    d["fav_lost"] = np.where(fav_is_a, 1 - d["label"], d["label"]).astype(int)
    d["fav_name"] = np.where(fav_is_a, d.get("player_a", ""), d.get("player_b", ""))
    d["dog_name"] = np.where(fav_is_a, d.get("player_b", ""), d.get("player_a", ""))
    flip = ~fav_is_a
    for c in [x for x in d.columns if x.startswith("d_")]:
        if pd.api.types.is_numeric_dtype(d[c]):
            d.loc[flip, c] = -d.loc[flip, c]
    d["band"] = None
    for (lo, hi), nm in zip(BANDS, BAND_NAMES):
        d.loc[(d["fav_p"] >= lo) & (d["fav_p"] < hi), "band"] = nm
    return d


def join_qualifier_dog(d, sack_dir):
    files = []
    for pat in ("[12][0-9][0-9][0-9].csv", "[12][0-9][0-9][0-9]_wta.csv"):
        files += glob.glob(os.path.join(sack_dir, pat))
    if not files or "tourney_id" not in d.columns:
        return d
    frames = []
    for f in files:
        try:
            frames.append(pd.read_csv(f, usecols=lambda c: c in
                          ("tourney_id", "winner_name", "loser_name",
                           "winner_entry", "loser_entry"), low_memory=False))
        except Exception:
            pass
    s = pd.concat(frames, ignore_index=True)
    ent = {}
    for tid, wn, ln, we, le in zip(s["tourney_id"], s["winner_name"], s["loser_name"],
                                   s.get("winner_entry"), s.get("loser_entry")):
        k = str(tid) + "|" + "|".join(sorted([norm_name(wn), norm_name(ln)]))
        ent[k] = (norm_name(wn), str(we).strip().upper(), norm_name(ln), str(le).strip().upper())
    vals, hit = [], 0
    for tid, fn, dn in zip(d["tourney_id"], d["fav_name"], d["dog_name"]):
        k = str(tid) + "|" + "|".join(sorted([norm_name(fn), norm_name(dn)]))
        rec = ent.get(k)
        if rec is None:
            vals.append(np.nan); continue
        hit += 1
        wn, we, ln, le = rec
        e = we if norm_name(dn) == wn else (le if norm_name(dn) == ln else "")
        vals.append(1.0 if e == "Q" else 0.0)
    d["is_qual_dog"] = vals
    print("  qualifier join: %.1f%% matched" % (100 * hit / max(len(d), 1)))
    return d


def build_components(d):
    comp = pd.DataFrame(index=d.index)
    comp["elo_gap"] = d["d_elo"] * LN10_400 - logit(d["fav_p"].values)
    for c in ("d_surface_switch", "d_age", "d_matches_14d", "d_rank", "d_rank_pts"):
        comp[c] = d.get(c)
    lv = d["level"].astype(str) if "level" in d.columns else pd.Series("", index=d.index)
    comp["is_slam"] = (lv == "G").astype(float)
    comp["is_small_event"] = lv.isin(["250", "I"]).astype(float)
    comp["is_bo5"] = d["is_bo5"].astype(float) if "is_bo5" in d.columns else 0.0
    comp["is_qual_dog"] = d["is_qual_dog"] if "is_qual_dog" in d.columns else np.nan
    return comp


# ------------------------------------------------------------ model machinery
def fit_score(d, comp, names, ridge=1e-3):
    """Fit weights on <=2023 only; return per-row score (within-band percentile
    of the component-only delta vs 2010-2023 favorites) and the model dict."""
    names = [c for c in names if c in comp.columns and comp[c].notna().sum() > 1000]
    fit_m = (d["year"] <= 2023).values
    med = comp.loc[fit_m, names].median()
    filled = comp[names].fillna(med)
    mu = filled[fit_m].mean(); sd = filled[fit_m].std().replace(0, 1)
    Z = (filled - mu) / sd
    lp = logit(d["fav_p"].values); y = d["fav_lost"].values
    X = np.column_stack([np.ones(fit_m.sum()), lp[fit_m], Z.values[fit_m]])
    beta, se = irls_logistic(X, y[fit_m], ridge=ridge)
    assert beta is not None
    delta = Z.values @ beta[2:]
    d = d.copy(); d["delta"] = delta
    pct = {}
    for nm in BAND_NAMES:
        ref = np.sort(d.loc[fit_m & (d["band"] == nm).values, "delta"].values)
        if len(ref) >= 200:
            pct[nm] = ref
    d["score"] = [100.0 * np.searchsorted(pct[b], x, side="right") / len(pct[b])
                  if b in pct else np.nan
                  for b, x in zip(d["band"], d["delta"])]
    model = {"names": names, "beta_fit": beta, "se_fit": se, "mu": mu, "sd": sd,
             "med": med, "Z": Z}
    return d, model


def contrib_table(d, model, mask, label, report):
    """Component coefficients estimated on the given rows only."""
    names = model["names"]; Z = model["Z"]
    lp = logit(d["fav_p"].values); y = d["fav_lost"].values
    m = mask.values if hasattr(mask, "values") else mask
    X = np.column_stack([np.ones(m.sum()), lp[m], Z.values[m]])
    beta, se = irls_logistic(X, y[m])
    report.append("  %-18s %10s %8s %8s   (n=%d)" % ("component", "coef", "z", "p", m.sum()))
    for i, c in enumerate(names):
        z = beta[2 + i] / max(se[2 + i], 1e-9)
        report.append("  %-18s %+10.3f %+8.2f %8.3f" % (c, beta[2 + i], z, 2 * norm_sf(abs(z))))


def bucket_table(te, report, min_n=80):
    rows = []
    for lo, hi in BUCKETS:
        b = te[(te["score"] >= lo) & (te["score"] <= hi)]
        if len(b) < min_n:
            report.append("   %2d-%-3d  n=%d -- too small, skipped" % (lo, hi, len(b)))
            continue
        lost = b["fav_lost"].mean(); imp = 1 - b["fav_p"].mean()
        se = math.sqrt(lost * (1 - lost) / len(b))
        report.append("   %2d-%-3d %6d   %5.1f%%    %5.1f     %5.1f        %5.1f       %+5.1f (%.1f)"
                      % (lo, hi, len(b), 100 * b["fav_p"].mean(), 100 * (1 - lost),
                         100 * lost, 100 * imp, 100 * (lost - imp), 100 * se))
        rows.append({"bucket": "%d-%d" % (lo, hi), "n": len(b), "excess": lost - imp})
    X = np.column_stack([np.ones(len(te)), logit(te["fav_p"].values), te["score"].values / 100.0])
    beta, se = irls_logistic(X, te["fav_lost"].values)
    z = beta[2] / se[2] if beta is not None else float("nan")
    report.append("   pooled trend z=%+.2f  p=%.4f" % (z, 2 * norm_sf(abs(z))))
    sep = (rows[-1]["excess"] - rows[0]["excess"]) if len(rows) >= 2 else float("nan")
    return sep, z


def diagnostic(d, report):
    te = d[(d["year"] >= 2024) & d["score"].notna()]
    report.append("\n=== 6. FAVORITE-LOSS DIAGNOSTIC (V3 score, 2024+ holdout) ===")
    report.append("upset rate by band x bucket (favorite loss %, n in parens):")
    hdr = "  band    " + "".join("  %8s" % ("%d-%d" % b) for b in BUCKETS)
    report.append(hdr)
    for nm in BAND_NAMES[1:]:
        cells = []
        for lo, hi in BUCKETS:
            b = te[(te["band"] == nm) & (te["score"] >= lo) & (te["score"] <= hi)]
            cells.append("  %5.1f(%4d)" % (100 * b["fav_lost"].mean(), len(b))
                         if len(b) >= 40 else "      --  ")
        report.append("  %-7s %s" % (nm, "".join(cells)))
    big = te[te["fav_p"] >= 0.75]
    report.append("big upsets (favorites priced 75%%+ who LOST): %d of %d such favorites"
                  % (int(big["fav_lost"].sum()), len(big)))
    for lo, hi in BUCKETS:
        b = big[(big["score"] >= lo) & (big["score"] <= hi)]
        if len(b) >= 40:
            report.append("   bucket %2d-%-3d: %3d upsets in %4d  (%.1f%% vs implied %.1f%%)"
                          % (lo, hi, int(b["fav_lost"].sum()), len(b),
                             100 * b["fav_lost"].mean(), 100 * (1 - b["fav_p"].mean())))


# ------------------------------------------------------------ run
def load(rows_path, prob_col):
    df = pd.read_csv(rows_path, low_memory=False)
    assert prob_col in df.columns, "--prob-col %r not in file" % prob_col
    df = df[df[prob_col].notna()].copy()
    conf = df[df[prob_col] > 0.60]
    if len(conf) > 200 and (conf["label"] == 1).mean() < 0.5:
        raise SystemExit("prob column looks inverted -- stop")
    if "year" not in df.columns and "date" in df.columns:
        df["year"] = pd.to_datetime(df["date"]).dt.year
    return df


def run(rows_path, prob_col, sack_dir):
    print(MARKER, "-- RUN")
    df = load(rows_path, prob_col)
    d0 = to_favorite_frame(df, prob_col)
    if sack_dir:
        d0 = join_qualifier_dog(d0, sack_dir)
    d0 = d0[d0["band"].notna()].copy()
    comp = build_components(d0)
    report = [MARKER,
              "Question: does Fragility separate favorites into groups that under/over-",
              "perform Pinnacle's implied expectation? Not a winner model.",
              "",
              "SELECTION-BIAS WARNING: V3's three components were kept BECAUSE they",
              "replicated on 2024+, so every 2024+ number below flatters V3 by",
              "construction. This comparison can lean; the live forward log decides."]

    dV2, mV2 = fit_score(d0, comp, V2_COMPONENTS)
    dV3, mV3 = fit_score(d0, comp, V3_COMPONENTS)

    report.append("\n=== 3. COMPONENT CONTRIBUTIONS -- HOLDOUT YEARS ONLY (fit on 2024+ alone) ===")
    report.append(" V2 (10 components):")
    contrib_table(dV2, mV2, dV2["year"] >= 2024, "V2", report)
    report.append(" V3 (3 survivors):")
    contrib_table(dV3, mV3, dV3["year"] >= 2024, "V3", report)
    report.append(" (for reference, same spec on the fit years <=2023:)")
    report.append(" V3 on <=2023:")
    contrib_table(dV3, mV3, dV3["year"] <= 2023, "V3fit", report)

    report.append("\n=== 1/2. BUCKET TABLES BY FAVORITE-PRICE THRESHOLD (2024+ holdout) ===")
    comparison = []
    for tname, thr in THRESHOLDS:
        row = {"threshold": tname}
        for label, dd in (("V2", dV2), ("V3", dV3)):
            te = dd[(dd["year"] >= 2024) & dd["score"].notna() & (dd["fav_p"] >= thr)]
            report.append("\n-- %s, %s favorites (n=%d) --" % (label, tname, len(te)))
            report.append("   bucket   n    mean_fav_p  fav_won%  fav_lost%  implied_lost%  excess (SE)")
            sep, z = bucket_table(te, report)
            row[label + "_sep"] = sep; row[label + "_z"] = z; row[label + "_n"] = len(te)
        comparison.append(row)

    report.append("\n=== 5. V2 vs V3 COMPARISON (separation = top-bucket excess minus "
                  "bottom-bucket excess, prob points) ===")
    report.append("  %-14s %8s %8s %8s   %8s %8s" %
                  ("threshold", "n", "V2_sep", "V2_z", "V3_sep", "V3_z"))
    for r in comparison:
        report.append("  %-14s %8d %+8.1f %+8.2f   %+8.1f %+8.2f"
                      % (r["threshold"], r["V2_n"], 100 * r["V2_sep"], r["V2_z"],
                         100 * r["V3_sep"], r["V3_z"]))

    diagnostic(dV3, report)
    report.append("\nREADING GUIDE: excess = actual favorite loss %% minus the loss %% "
                  "Pinnacle's own prices implied for those exact matches. Positive top "
                  "buckets + positive trend z = the score finds vulnerable favorites "
                  "beyond price. Higher thresholds have fewer matches -- expect wider "
                  "error bars; judge the pattern across thresholds, not one cell.")
    txt = "\n".join(report)
    open("fragility_v3_report.txt", "w").write(txt + "\n")
    pd.DataFrame(comparison).to_csv("fragility_v3_comparison.csv", index=False)
    json.dump({"marker": MARKER, "components": mV3["names"],
               "beta": {c: float(mV3["beta_fit"][2 + i]) for i, c in enumerate(mV3["names"])},
               "mu": {c: float(mV3["mu"][c]) for c in mV3["names"]},
               "sd": {c: float(mV3["sd"][c]) for c in mV3["names"]},
               "med": {c: float(mV3["med"][c]) for c in mV3["names"]}},
              open("fragility_v3_model.json", "w"), indent=1)
    print(txt)
    print("\nwritten: fragility_v3_report.txt, fragility_v3_comparison.csv, fragility_v3_model.json")


# ------------------------------------------------------------ selftest
def selftest():
    print(MARKER, "-- SELFTEST (synthetic)")
    rng = np.random.default_rng(23)
    n = 70000
    year = rng.integers(2015, 2026, n)
    skill = rng.normal(0, 1.1, n)
    gap = rng.normal(0, 0.5, n)
    sw_a = (rng.random(n) < 0.10).astype(float); sw_b = (rng.random(n) < 0.10).astype(float)
    age = rng.normal(0, 1, n)
    p_a = 1 / (1 + np.exp(-skill))
    sign = np.where(p_a >= 0.5, 1.0, -1.0)
    lin = skill - 0.18 * gap - 0.30 * (sw_a - sw_b) - 0.06 * age * sign
    label = (rng.random(n) < 1 / (1 + np.exp(-lin))).astype(int)
    df = pd.DataFrame({"label": label, "year": year, "player_a": "A", "player_b": "B",
                       "tourney_id": "t", "pinn_a": p_a,
                       "d_elo": (skill + gap) / LN10_400,
                       "d_surface_switch": sw_a - sw_b, "d_age": age * sign,
                       "d_matches_14d": rng.normal(0, 1, n),
                       "d_rank": rng.normal(0, 1, n), "d_rank_pts": rng.normal(0, 1, n),
                       "level": "250", "is_bo5": 0})
    d0 = to_favorite_frame(df, "pinn_a"); d0 = d0[d0["band"].notna()].copy()
    comp = build_components(d0)
    dV3, mV3 = fit_score(d0, comp, V3_COMPONENTS)
    dV2, mV2 = fit_score(d0, comp, V2_COMPONENTS)
    report = []
    contrib_table(dV3, mV3, dV3["year"] >= 2024, "V3", report)
    print("\n".join(report))
    # planted effects must show on holdout-only fit
    lines = {l.split()[0]: l for l in report if l.strip() and l.split()[0] in mV3["names"]}
    z_gap = float(lines["elo_gap"].split()[2])
    z_sw = float(lines["d_surface_switch"].split()[2])
    z_age = float(lines["d_age"].split()[2])
    assert z_gap > 2 and z_sw > 2 and z_age > 1.5, "planted effects not in holdout table"
    rep2 = []
    seps = {}
    for label, dd in (("V2", dV2), ("V3", dV3)):
        te = dd[(dd["year"] >= 2024) & dd["score"].notna() & (dd["fav_p"] >= 0.60)]
        rep2.append("-- %s >=60%% --" % label)
        sep, z = bucket_table(te, rep2)
        seps[label] = (sep, z)
    print("\n".join(rep2))
    assert seps["V3"][1] > 3, "V3 trend not recovered"
    assert seps["V3"][0] > 0.02, "V3 separation too small"
    diag = []
    diagnostic(dV3, diag)
    assert any("big upsets" in l for l in diag), "diagnostic section missing"
    print("SELFTEST PASS -- planted gap/switch/age recovered on holdout-only fit, "
          "thresholded buckets separate, diagnostic renders.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--rows", default="apex_batch_a_rows.csv")
    ap.add_argument("--prob-col", default="pinn_a")
    ap.add_argument("--sackmann-dir", default=None)
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.run:
        run(a.rows, a.prob_col, a.sackmann_dir)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
