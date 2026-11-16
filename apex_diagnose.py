#!/usr/bin/env python3
"""
apex_diagnose.py -- 2026-08-17  Diagnosis phase for the Apex combined model.
No new features.  Answers the eight investigations:

  1  feature quality        mean / median / sd / missing % / correlation with outcome
  2  surface-specific       generic vs surface versions of hold %, break %, serve/return pts
  3  redundancy with Elo    correlation + R^2 of every feature (and each group) vs the Rating cols
  4  no-Rating model        Serve+Return+Form+Matchup+Physical+Context only
  5  group ablation         all groups, then drop one group at a time
  6  individual importance  top-20 by gain and by permutation, key metrics called out
  7  winner identification  combined vs Elo-only where they disagree
  8  leak check             scrambled labels x5 seeds, A/B flip symmetry, timeline audit,
                            plus an explanation of why a single scrambled run can read 54 %

Rows are built once by apex_combined_model.build_rows (prior-only) and cached in
apex_rows_cache.pkl so repeat runs are fast.

USAGE:
  python3 apex_diagnose.py --sack-dir "ATP & WTA, 1968-2026"
  python3 apex_diagnose.py --selftest
"""
import argparse, os, sys, pickle, warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apex_combined_model as acm

MARKER = "APEX-DIAG-2026-08-17"
GROUPS = ["Rating", "Serve", "Return", "Form", "Physical", "Matchup", "Context"]


def hr(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


def acc(p, y):
    return float(((p >= 0.5).astype(int) == y).mean())


def split(rows, test_from):
    tr = rows[rows.year <= test_from - 3]
    va = rows[(rows.year > test_from - 3) & (rows.year < test_from)]
    te = rows[rows.year >= test_from]
    return tr, va, te


def fit_eval(tr, va, te, cols, seed):
    m, gain, _ = acm.fit_gbm(tr[cols], tr["label"], va[cols], va["label"], seed)
    p = m.predict_proba(te[cols])[:, 1]
    return m, gain, p, acc(p, te["label"].values), acm.logloss(p, te["label"].values)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sack-dir", default="ATP & WTA, 1968-2026")
    ap.add_argument("--year-from", type=int, default=2005)
    ap.add_argument("--test-from", type=int, default=2024)
    ap.add_argument("--min-prior", type=int, default=10)
    ap.add_argument("--include-challenger", action="store_true")
    ap.add_argument("--all-levels", action="store_true")
    ap.add_argument("--cache", default="apex_rows_cache.pkl")
    ap.add_argument("--rebuild", action="store_true", help="ignore the cache and rebuild rows")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        d = acm.make_fake("/tmp/apex_fake")
        args.sack_dir = d; args.year_from = 2015; args.min_prior = 5; args.cache = "/tmp/apex_fake/rows.pkl"
    print(f"[{MARKER}] diagnosis phase -- no new features; understand the current model first.")

    if os.path.exists(args.cache) and not args.rebuild:
        rows = pd.read_pickle(args.cache)
        print(f"Loaded cached rows from {args.cache}: {len(rows):,}  (use --rebuild to regenerate)")
    else:
        df = acm.load_sackmann(args.sack_dir, args.year_from, args.include_challenger)
        rows = acm.build_rows(df, args.min_prior, args.seed, main_only=not args.all_levels)
        rows.to_pickle(args.cache)
        print(f"Cached rows -> {args.cache}")

    cols = acm.feature_columns(rows)
    grp = {c: acm.group_for(c) for c in cols}
    tr, va, te = split(rows, args.test_from)
    y_te = te["label"].values
    print(f"train {len(tr):,} / tune {len(va):,} / test {len(te):,}   features {len(cols)}")
    print("  test rows by level: " + ", ".join(f"{l}={n:,}" for l, n in te.groupby("level").size().sort_values(ascending=False).items()))
    print("  test rows by round: " + ", ".join(f"{r}={n:,}" for r, n in te.groupby("round").size().sort_values(ascending=False).items()))

    # ------------------------------------------------------------ 1
    hr("INVESTIGATION 1 -- feature quality (all rows; corr = correlation with 'A won')")
    y_all = rows["label"].values
    lines = []
    for c in cols:
        x = rows[c].astype(float)
        miss = x.isna().mean()
        xv = x.dropna()
        r = np.corrcoef(xv, y_all[x.notna().values])[0, 1] if len(xv) > 100 and xv.std() > 0 else np.nan
        lines.append((grp[c], c, x.mean(), x.median(), x.std(), miss, r))
    tab = pd.DataFrame(lines, columns=["group", "feature", "mean", "median", "sd", "missing", "corr_win"])
    for g in GROUPS:
        sub = tab[tab.group == g].sort_values("corr_win", key=lambda s: -s.abs())
        if len(sub) == 0:
            continue
        print(f"\n  [{g}]")
        for r in sub.itertuples(index=False):
            print(f"    {r.feature:26s} mean {r.mean:9.4f}  median {r.median:9.4f}  sd {r.sd:8.4f}  missing {r.missing*100:5.1f}%  corr {r.corr_win:+.3f}")
    print("\n  sanity on raw levels (players' hold%/break% before differencing are not stored; check the diffs):")
    print("   - a d_ feature with |corr| >= 0.10 is a meaningful single signal; 0.03-0.10 weak; <0.03 noise")
    print("   - hold_pct diff sd should be ~0.05-0.10; break_pct ~0.05-0.10; ace_rate ~0.03-0.06 (fractions, not %)")

    # ------------------------------------------------------------ 2
    hr("INVESTIGATION 2 -- generic vs surface-specific serve/return strength")
    pairs = [("d_hold_pct", "d_s_hold_pct"), ("d_break_pct", "d_s_break_pct"),
             ("d_svc_pts_won", "d_s_svc_pts_won"), ("d_ret_pts_won", "d_s_ret_pts_won")]
    trva = pd.concat([tr, va])
    for gcol, scol in pairs:
        for name, cc in (("generic", [gcol]), ("surface", [scol]), ("both", [gcol, scol])):
            m = acm.fit_logit(trva[cc], trva["label"])
            p = m.predict_proba(te[cc])[:, 1]
            cov = te[cc].notna().all(axis=1).mean()
            print(f"  {gcol[2:]:14s} {name:8s} acc {acc(p, y_te)*100:5.2f}%   coverage {cov*100:5.1f}%")
    print("  by surface of the match (single-feature logistic on generic vs surface hold+break diffs):")
    for s in ("Hard", "Clay", "Grass"):
        tes = te[te.surface == s]
        if len(tes) < 200:
            continue
        out = []
        for name, cc in (("generic", ["d_hold_pct", "d_break_pct"]), ("surface", ["d_s_hold_pct", "d_s_break_pct"])):
            m = acm.fit_logit(trva[cc], trva["label"]); p = m.predict_proba(tes[cc])[:, 1]
            out.append(f"{name} {acc(p, tes['label'].values)*100:5.2f}%")
        print(f"    {s:5s} n={len(tes):5,}  " + "   ".join(out))

    # ------------------------------------------------------------ 3
    hr("INVESTIGATION 3 -- redundancy with the Rating group (Elo, surface Elo, rank, rank pts)")
    rating_cols = [c for c in cols if grp[c] == "Rating"]
    from sklearn.linear_model import LinearRegression
    X = rows[cols].astype(float)
    print("  correlation of each feature with d_elo, and R^2 of the feature explained by ALL rating cols:")
    r2_by_group = {g: [] for g in GROUPS}
    for c in cols:
        if grp[c] == "Rating":
            continue
        m = X[[c] + rating_cols].dropna()
        if len(m) < 500 or m[c].std() == 0:
            continue
        r_elo = np.corrcoef(m[c], m["d_elo"])[0, 1]
        lr = LinearRegression().fit(m[rating_cols], m[c])
        r2 = lr.score(m[rating_cols], m[c])
        r2_by_group[grp[c]].append(r2)
        if r2 >= 0.25 or abs(r_elo) >= 0.5 or c in ("d_hold_pct", "d_break_pct", "d_hold_plus_break", "d_win_10", "d_svc_pts_won", "d_ret_pts_won"):
            print(f"    {c:26s} [{grp[c]:8s}] corr(d_elo) {r_elo:+.3f}   R^2 vs rating {r2:.3f}   independent share {1-r2:.0%}")
    print("  average R^2 (share of a group already explained by rating):")
    for g, v in r2_by_group.items():
        if v:
            print(f"    {g:9s} {np.mean(v):.3f}   ({len(v)} features)   -> {1-np.mean(v):.0%} of the group's variation is NOT in Elo/rank")
    # how much of Elo's information do the non-rating features carry?
    m = X.dropna(subset=["d_elo"])
    non = [c for c in cols if grp[c] != "Rating"]
    mm = m[non + ["d_elo"]].fillna(0)
    lr = LinearRegression().fit(mm[non], mm["d_elo"])
    print(f"  reverse: all non-rating features together explain R^2 {lr.score(mm[non], mm['d_elo']):.3f} of d_elo")

    # ------------------------------------------------------------ 4 & 5
    hr("INVESTIGATION 4 & 5 -- no-Rating model and group ablation (same GBM, same split)")
    results = {}
    _, _, p_all, a_all, ll_all = fit_eval(tr, va, te, cols, args.seed)
    results["ALL groups"] = (a_all, ll_all)
    p_by = {"ALL groups": p_all}
    for g in GROUPS:
        cc = [c for c in cols if grp[c] != g]
        if len(cc) == len(cols):
            continue
        _, _, p, a, ll = fit_eval(tr, va, te, cc, args.seed)
        results[f"ALL except {g}"] = (a, ll); p_by[f"ALL except {g}"] = p
    only_tennis = [c for c in cols if grp[c] not in ("Rating",)]
    _, _, p_nr, a_nr, ll_nr = fit_eval(tr, va, te, only_tennis, args.seed)
    results["NO RATING (tennis-performance only)"] = (a_nr, ll_nr)
    for g in ("Serve", "Return", "Form", "Physical", "Matchup"):
        cc = [c for c in cols if grp[c] == g]
        _, _, p, a, ll = fit_eval(tr, va, te, cc, args.seed)
        results[f"ONLY {g}"] = (a, ll)
    _, _, p_r, a_r, ll_r = fit_eval(tr, va, te, rating_cols, args.seed)
    results["ONLY Rating"] = (a_r, ll_r)
    elo_lr = acm.fit_logit(trva[["d_elo", "d_surf_elo"]], trva["label"]); p_elo = elo_lr.predict_proba(te[["d_elo", "d_surf_elo"]])[:, 1]
    results["Elo-only logistic (reference)"] = (acc(p_elo, y_te), acm.logloss(p_elo, y_te))
    print(f"  {'model':40s} {'acc':>7s} {'logloss':>9s} {'vs ALL':>8s}")
    for k, (a, ll) in results.items():
        print(f"  {k:40s} {a*100:6.2f}% {ll:9.4f} {(a-a_all)*100:+7.2f}")
    print("\n  reading guide: 'ALL except X' much lower than ALL = X carries unique information;")
    print("  'ONLY X' close to ALL = X alone already knows most of what the model knows.")

    # ------------------------------------------------------------ 6
    hr("INVESTIGATION 6 -- individual feature importance (top 20, gain and permutation)")
    m_all, gain, _ = acm.fit_gbm(tr[cols], tr["label"], va[cols], va["label"], args.seed)
    gtot = gain.sum() or 1
    print("  by gain:")
    for c, v in gain.sort_values(ascending=False).head(20).items():
        print(f"    {c:28s} {100*v/gtot:5.1f}%  [{grp[c]}]")
    rng = np.random.RandomState(args.seed)
    base = acc(m_all.predict_proba(te[cols])[:, 1], y_te)
    drops = {}
    Xte = te[cols].reset_index(drop=True)
    for c in cols:
        d = 0
        for _ in range(2):
            Xp = Xte.copy(); Xp[c] = Xp[c].values[rng.permutation(len(Xp))]
            d += base - acc(m_all.predict_proba(Xp)[:, 1], y_te)
        drops[c] = d / 2
    ds = pd.Series(drops).sort_values(ascending=False)
    print("  by permutation (accuracy points lost when that one column is scrambled):")
    for c, v in ds.head(20).items():
        print(f"    {c:28s} {v*100:+5.2f} pts  [{grp[c]}]")
    print("  key tennis metrics specifically:")
    for c in ("d_hold_pct", "d_break_pct", "d_hold_plus_break", "d_svc_pts_won", "d_ret_pts_won", "d_games_dom_10", "d_sets_pct_10",
              "d_first_won", "d_second_won", "d_ace_rate", "d_df_rate", "d_bp_save", "d_bp_conv"):
        if c in drops:
            print(f"    {c:28s} gain {100*gain.get(c,0)/gtot:5.2f}%   permutation {drops[c]*100:+5.2f} pts   corr(win) {tab.set_index('feature').loc[c,'corr_win']:+.3f}")

    # ------------------------------------------------------------ 7
    hr("INVESTIGATION 7 -- when combined and Elo-only disagree, who identifies the winner?")
    pk_all = (p_all >= 0.5).astype(int); pk_elo = (p_elo >= 0.5).astype(int)
    dis = pk_all != pk_elo
    print(f"  disagreements: {int(dis.sum()):,} of {len(te):,} test matches ({100*dis.mean():.1f}%)")
    if dis.sum():
        print(f"    combined right {100*(pk_all[dis]==y_te[dis]).mean():.1f}%   Elo-only right {100*(pk_elo[dis]==y_te[dis]).mean():.1f}%")
        for lo, hi in ((0.5, 0.55), (0.55, 0.6), (0.6, 0.7), (0.7, 1.01)):
            conf = np.abs(p_all - 0.5) + 0.5
            mk = dis & (conf >= lo) & (conf < hi)
            if mk.sum():
                print(f"    combined conf {lo:.2f}-{min(hi,1):.2f}: n={int(mk.sum()):4,}  combined right {100*(pk_all[mk]==y_te[mk]).mean():5.1f}%")
    pk_nr = (p_nr >= 0.5).astype(int); dis2 = pk_nr != pk_elo
    print(f"  no-Rating model vs Elo-only: disagree {int(dis2.sum()):,};  no-Rating right {100*(pk_nr[dis2]==y_te[dis2]).mean():.1f}%   Elo right {100*(pk_elo[dis2]==y_te[dis2]).mean():.1f}%")

    # ------------------------------------------------------------ 8
    hr("INVESTIGATION 8 -- leak check")
    print("  (a) scrambled labels, 5 seeds (expect ~50%; a single run can wander several points -- see note):")
    accs = []
    for s in range(5):
        r = np.random.RandomState(100 + s)
        m, _, _ = acm.fit_gbm(tr[cols], r.permutation(tr["label"].values), va[cols], r.permutation(va["label"].values), s)
        a = acc(m.predict_proba(te[cols])[:, 1], y_te); accs.append(a)
        print(f"      seed {s}: {a*100:5.2f}%   (best_iteration {getattr(m, 'best_iteration', '?')})")
    print(f"      mean {np.mean(accs)*100:.2f}%  min {min(accs)*100:.2f}%  max {max(accs)*100:.2f}%")
    print("      NOTE: a model trained on scrambled labels stops after a handful of trees whose split")
    print("      directions are random. If those few splits happen to line up with d_elo's sign, the")
    print("      model inherits some of Elo's accuracy by luck (and lands BELOW 50% just as often when")
    print("      they line up the other way). Expected value is 50%; single-run spread is wide. Only a")
    print("      mean across seeds that sits well above 50% would indicate leakage.")
    print("  (b) A/B flip symmetry: negate every diff, prediction must become 1-p (orientation leak check):")
    Xf = te[cols].copy()
    for c in cols:
        if c.startswith("d_") or c.startswith("mu_") or c in ("lefty_vs_righty", "ht_gap"):
            Xf[c] = -Xf[c]
    p_f = m_all.predict_proba(Xf)[:, 1]
    print(f"      mean |p + p_flipped - 1| = {np.mean(np.abs(p_all + p_f - 1)):.4f}   (0 = perfectly symmetric; <0.05 fine, the trees are not forced symmetric)")
    print("  (c) no-peeking proof: rebuild rows from a history TRUNCATED at a cutoff date; every row dated at or")
    print("      before the cutoff must be identical to the full build. If any feature used future information,")
    print("      the two builds would differ.")
    if 'df' not in dir():
        df = acm.load_sackmann(args.sack_dir, args.year_from, args.include_challenger)
    cutoff = pd.Timestamp(f"{args.test_from}-06-30").date()
    df_cut = df[df["tdate"] <= cutoff]
    rows_cut = acm.build_rows(df_cut, args.min_prior, args.seed, main_only=not args.all_levels)
    full_before = rows[rows["date"] <= cutoff].reset_index(drop=True)
    n = min(len(full_before), len(rows_cut))
    same_len = len(full_before) == len(rows_cut)
    num_cols = [c for c in cols]
    a = full_before[num_cols].astype(float).values[:n]; b = rows_cut[num_cols].astype(float).values[:n]
    diff = np.nansum(np.abs(a - b)) if n else 0
    nan_same = np.array_equal(np.isnan(a), np.isnan(b)) if n else True
    ok = same_len and diff < 1e-6 and nan_same
    print(f"      rows <= {cutoff}: full build {len(full_before):,}  truncated build {len(rows_cut):,}  "
          f"max abs diff {diff:.2e}  -> {'PASS: no future information in any feature' if ok else '!!! FAIL: features differ -> leakage'}")
    print("      residual caveat: Sackmann row ORDER inside a tournament. Rows are sorted by (tourney_date,")
    print("      tourney_id, round order, match_num), so earlier rounds always precede later ones.")

    print(f"\n[{MARKER}] done. Diagnosis complete -- no features were added.")


if __name__ == "__main__":
    main()
