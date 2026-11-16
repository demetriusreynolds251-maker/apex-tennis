#!/usr/bin/env python3
"""
apex_combined_model.py  --  Apex combined player-evaluation model (2026-08-17)

OBJECTIVE (governing, from the Apex Mission Statement 2026-08-17):
  Given Player A and Player B, identify who should win THIS match today based
  on tennis reality.  The match result is ground truth.  The market is NOT an
  input, NOT a training target, and NOT an objective -- it appears only in the
  after-the-fact evaluation section (readout 2).  ELO is one feature among many.

WHAT IT DOES
  1. Reads the Sackmann-format CSVs in the "ATP & WTA, 1968-2026" folder
     (YYYY.csv = ATP, YYYY_wta.csv = WTA, YYYY_challenger.csv = challengers).
  2. Walks every match in date/round order and, for each player, keeps a
     running state built ONLY from matches that already happened
     (prior-only, no peeking).  From that state it builds six feature groups
     for both players and takes A-minus-B differences:
        RATING   elo, surface elo, elo trend, rank, rank points
        SERVE    hold %, service pts won, 1st in, 1st won, 2nd won, ace rate,
                 DF rate, BP save %  (+ surface-specific versions)
        RETURN   break %, return pts won, BP conversion (+ surface-specific)
        FORM     win % last 5/10/20, sets %, games dominance, elo-adjusted
                 form, surface win %
        PHYSICAL height, age, hand, rest days, matches/minutes/sets last 7/14d,
                 retirement rate, retired recently, surface switch
        MATCHUP  H2H, surface H2H, hand matchup, height gap, serve-vs-return
                 cross terms
  3. Trains ONE model on all features together (gradient boosting; a logistic
     model runs alongside as a simpler second read).  Train <=2021, tune on
     2022-23, test on 2024+ (or --walkforward: refit for every test year).
  4. Prints the four readouts:
        (1) overall winner accuracy on held-out matches
        (2) accuracy where the model's pick differs from the market favourite
            (needs tennis-data.co.uk files in ./odds/; skipped if absent)
        (3) learned weight of each feature GROUP, as % shares
            (gain share AND permutation share -- two independent measures)
        (4) same-match comparison against the current engine
            (needs walkforward_rows.csv; skipped if absent) and against a
            plain ELO-only model.

SAFETY CHECKS (alarms print loudly)
  - scrambled-label run must land near 50 % (leak check)
  - test accuracy above 74 % is flagged as suspicious
  - features are snapshots taken BEFORE the match updates the state

USAGE (on the Mini, inside the project folder):
  python3 apex_combined_model.py --sack-dir "ATP & WTA, 1968-2026"
  python3 apex_combined_model.py --sack-dir "ATP & WTA, 1968-2026" --walkforward
  python3 apex_combined_model.py --selftest         # fake data, no files needed
"""
import argparse, glob, math, os, random, re, sys, unicodedata
from collections import defaultdict, deque
from datetime import date, timedelta

import numpy as np
import pandas as pd

MARKER = "APEX-COMBINED-2026-08-17"

# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
ROUND_RANK = {"Q1": 0, "Q2": 1, "Q3": 2, "Q4": 2, "ER": 3, "RR": 3, "R128": 4,
              "R64": 5, "R32": 6, "R16": 7, "QF": 8, "SF": 9, "BR": 9, "F": 10}
NON_MAIN_LEVELS = {"C", "S", "D", "E", "J", "15", "25", "40", "60", "80", "100", "125"}   # challenger/ITF/Davis-BJK cup/exhib/juniors


def norm_text(s):
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def name_keys(full):
    """Keys for joining 'Jannik Sinner' <-> 'Sinner J.' style names."""
    t = norm_text(full).split()
    if not t:
        return set()
    if len(t) == 1:
        return {(t[0], "")}
    keys = {(t[-1], t[0][0])}
    if len(t) >= 3:
        keys.add(("".join(t[-2:]), t[0][0]))          # 'auger aliassime' joined
        keys.add((t[-2], t[0][0]))                    # first surname only
    keys.add(("".join(t[1:]), t[0][0]))               # all-but-first joined
    return keys


def parse_ymd(v):
    s = str(v).strip()[:8]
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except Exception:
        return None


def parse_score(score):
    """Return (sets_w, sets_l, games_w, games_l, retired, walkover)."""
    if not isinstance(score, str):
        return 0, 0, 0, 0, 0, 1
    s = score.upper()
    wo = ("W/O" in s) or ("WALKOVER" in s) or (s.strip() == "")
    ret = ("RET" in s) or ("DEF" in s) or ("ABN" in s) or ("ABD" in s)
    sw = sl = gw = gl = 0
    for tok in re.findall(r"(\d+)-(\d+)", s):
        a, b = int(tok[0]), int(tok[1])
        if a > 20 or b > 20:            # tiebreak scores in brackets are stripped by regex? no: "7-6(5)" -> 7-6 only
            continue
        gw += a; gl += b
        if a > b: sw += 1
        elif b > a: sl += 1
    return sw, sl, gw, gl, int(ret), int(wo)


def fnum(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except Exception:
        return None


def rate(num, den):
    return None if not den else num / den


# ----------------------------------------------------------------------------
# player state (prior-only)
# ----------------------------------------------------------------------------
class PState:
    __slots__ = ("elo", "surf_elo", "n", "hist", "elo_hist", "surf_hist",
                 "last_date", "last_surface", "ret_n", "ret_last")

    def __init__(self):
        self.elo = 1500.0
        self.surf_elo = defaultdict(lambda: 1500.0)
        self.n = 0
        self.hist = deque(maxlen=60)          # dicts of per-match facts
        self.elo_hist = deque(maxlen=80)      # (date, elo)
        self.surf_hist = defaultdict(lambda: deque(maxlen=30))
        self.last_date = None
        self.last_surface = None
        self.ret_n = 0
        self.ret_last = None


def k_factor(n):
    return 250.0 / ((n + 5) ** 0.4)          # FiveThirtyEight-style decaying K


def _agg(rows, key_num, key_den):
    num = sum(r[key_num] for r in rows if r.get(key_num) is not None and r.get(key_den))
    den = sum(r[key_den] for r in rows if r.get(key_num) is not None and r.get(key_den))
    return rate(num, den)


def features_for(p, today, surface, opp_elo):
    """Snapshot of player p's state BEFORE today's match."""
    f = {}
    h = list(p.hist)
    hs = h[-20:]
    # RATING
    f["elo"] = p.elo
    f["surf_elo"] = p.surf_elo[surface]
    old = [e for d, e in p.elo_hist if (today - d).days >= 60]
    f["elo_trend"] = (p.elo - old[-1]) if old else 0.0
    f["n_prior"] = p.n
    # SERVE (last 20 with stats)
    f["hold_pct"] = _agg(hs, "holds", "svgms")
    f["svc_pts_won"] = _agg(hs, "svpt_won", "svpt")
    f["first_in"] = _agg(hs, "first_in", "svpt")
    f["first_won"] = _agg(hs, "first_won", "first_in")
    f["second_won"] = _agg(hs, "second_won", "second_pts")
    f["ace_rate"] = _agg(hs, "ace", "svpt")
    f["df_rate"] = _agg(hs, "df", "svpt")
    f["bp_save"] = _agg(hs, "bp_saved", "bp_faced")
    # RETURN
    f["break_pct"] = _agg(hs, "breaks", "opp_svgms")
    f["ret_pts_won"] = _agg(hs, "ret_won", "ret_pts")
    f["bp_conv"] = _agg(hs, "breaks", "opp_bp_faced")
    hb = f["hold_pct"], f["break_pct"]
    f["hold_plus_break"] = (hb[0] + hb[1]) if None not in hb else None
    # surface-specific
    ss = list(p.surf_hist[surface])[-20:]
    f["s_hold_pct"] = _agg(ss, "holds", "svgms")
    f["s_break_pct"] = _agg(ss, "breaks", "opp_svgms")
    f["s_svc_pts_won"] = _agg(ss, "svpt_won", "svpt")
    f["s_ret_pts_won"] = _agg(ss, "ret_won", "ret_pts")
    f["s_win_pct"] = rate(sum(r["win"] for r in ss), len(ss)) if ss else None
    f["s_n"] = len(ss)
    # FORM
    for k in (5, 10, 20):
        hh = h[-k:]
        f[f"win_{k}"] = rate(sum(r["win"] for r in hh), len(hh)) if hh else None
    h10 = h[-10:]
    f["sets_pct_10"] = _agg(h10, "sets_w", "sets_t")
    f["games_dom_10"] = (np.mean([r["games_w"] - r["games_l"] for r in h10 if r["games_t"]])
                        if any(r["games_t"] for r in h10) else None)
    f["adj_form_10"] = np.mean([r["win"] - r["exp"] for r in h10]) if h10 else None
    f["opp_elo_10"] = np.mean([r["opp_elo"] for r in h10]) if h10 else None
    # PHYSICAL / FATIGUE / REST
    f["days_since"] = (today - p.last_date).days if p.last_date else 400
    f["days_since"] = min(f["days_since"], 400)
    d7 = [r for r in h if (today - r["date"]).days <= 7 and (today - r["date"]).days >= 0]
    d14 = [r for r in h if (today - r["date"]).days <= 14 and (today - r["date"]).days >= 0]
    f["matches_7d"] = len(d7)
    f["matches_14d"] = len(d14)
    f["minutes_14d"] = sum(r["minutes"] or 0 for r in d14)
    f["sets_14d"] = sum(r["sets_t"] for r in d14)
    f["ret_rate"] = rate(p.ret_n, p.n) if p.n else None
    f["retired_90d"] = int(p.ret_last is not None and (today - p.ret_last).days <= 90)
    f["surface_switch"] = int(p.last_surface is not None and p.last_surface != surface)
    return f


def update_state(p, row, won, surface, today, exp, opp_elo, opp_stats):
    """Push today's match into player state AFTER features were taken."""
    st = row  # own stats dict
    svpt = st.get("svpt"); first_in = st.get("1stIn"); first_won = st.get("1stWon")
    second_won = st.get("2ndWon"); svgms = st.get("SvGms"); bpf = st.get("bpFaced"); bps = st.get("bpSaved")
    holds = (svgms - (bpf - bps)) if None not in (svgms, bpf, bps) else None
    o_svgms = opp_stats.get("SvGms"); o_bpf = opp_stats.get("bpFaced"); o_bps = opp_stats.get("bpSaved")
    breaks = (o_bpf - o_bps) if None not in (o_bpf, o_bps) else None
    o_svpt = opp_stats.get("svpt"); o_1w = opp_stats.get("1stWon"); o_2w = opp_stats.get("2ndWon")
    ret_won = (o_svpt - o_1w - o_2w) if None not in (o_svpt, o_1w, o_2w) else None
    rec = {
        "date": today, "win": int(won), "exp": exp, "opp_elo": opp_elo,
        "sets_w": row["sets_w"], "sets_t": row["sets_w"] + row["sets_l"],
        "games_w": row["games_w"], "games_l": row["games_l"], "games_t": row["games_w"] + row["games_l"],
        "minutes": row.get("minutes"),
        "svgms": svgms, "holds": holds, "svpt": svpt, "svpt_won": (first_won + second_won) if None not in (first_won, second_won) else None,
        "first_in": first_in, "first_won": first_won, "second_won": second_won,
        "second_pts": (svpt - first_in) if None not in (svpt, first_in) else None,
        "ace": st.get("ace"), "df": st.get("df"), "bp_faced": bpf, "bp_saved": bps,
        "opp_svgms": o_svgms, "breaks": breaks, "opp_bp_faced": o_bpf,
        "ret_pts": o_svpt, "ret_won": ret_won,
    }
    p.hist.append(rec)
    p.surf_hist[surface].append(rec)
    p.n += 1
    p.last_date = today
    p.last_surface = surface
    if row["retired_self"]:
        p.ret_n += 1
        p.ret_last = today


# ----------------------------------------------------------------------------
# data loading
# ----------------------------------------------------------------------------
STAT_COLS = ["ace", "df", "svpt", "1stIn", "1stWon", "2ndWon", "SvGms", "bpSaved", "bpFaced"]


def load_sackmann(sack_dir, year_from, include_challenger):
    files = []
    for fp in glob.glob(os.path.join(sack_dir, "*.csv")):
        base = os.path.basename(fp)
        m = re.match(r"^(\d{4})(_wta|_challenger)?\.csv$", base)
        if not m:
            continue
        yr = int(m.group(1)); kind = m.group(2) or "_atp"
        if yr < year_from:
            continue
        if kind == "_challenger" and not include_challenger:
            continue
        files.append((yr, kind, fp))
    if not files:
        sys.exit(f"No Sackmann-format CSVs found in {sack_dir!r} (expected YYYY.csv / YYYY_wta.csv).")
    frames = []
    for yr, kind, fp in sorted(files):
        try:
            df = pd.read_csv(fp, low_memory=False, encoding="utf-8", encoding_errors="replace")
        except Exception as e:
            print(f"  ! skip {fp}: {e}")
            continue
        df["tour"] = "WTA" if kind == "_wta" else "ATP"
        df["is_challenger"] = int(kind == "_challenger")
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df["tdate"] = df["tourney_date"].map(parse_ymd)
    df = df[df["tdate"].notna()].copy()
    df["rround"] = df["round"].map(lambda r: ROUND_RANK.get(str(r).strip(), 5))
    df["surface"] = df["surface"].fillna("Hard").astype(str).str.title()
    df.loc[~df["surface"].isin(["Hard", "Clay", "Grass", "Carpet"]), "surface"] = "Hard"
    df = df.sort_values(["tdate", "tourney_id", "rround", "match_num"], kind="mergesort").reset_index(drop=True)
    print(f"Loaded {len(df):,} matches from {len(files)} files ({df['tdate'].min()} .. {df['tdate'].max()})")
    return df


# ----------------------------------------------------------------------------
# feature building
# ----------------------------------------------------------------------------
GROUP_OF = {}


def group_for(col):
    base = col[2:] if col.startswith("d_") else col
    if base in ("elo", "surf_elo", "elo_trend", "rank", "rank_pts"):
        return "Rating"
    if base in ("hold_pct", "svc_pts_won", "first_in", "first_won", "second_won", "ace_rate", "df_rate",
                "bp_save", "s_hold_pct", "s_svc_pts_won", "hold_plus_break"):
        return "Serve"
    if base in ("break_pct", "ret_pts_won", "bp_conv", "s_break_pct", "s_ret_pts_won"):
        return "Return"
    if base in ("win_5", "win_10", "win_20", "sets_pct_10", "games_dom_10", "adj_form_10", "opp_elo_10",
                "s_win_pct", "s_n"):
        return "Form"
    if base in ("ht", "age", "left", "days_since", "matches_7d", "matches_14d", "minutes_14d", "sets_14d",
                "ret_rate", "retired_90d", "surface_switch"):
        return "Physical"
    if base.startswith("h2h") or base.startswith("mu_") or base in ("lefty_vs_righty", "ht_gap"):
        return "Matchup"
    return "Context"


def build_rows(df, min_prior, seed, main_only):
    rng = random.Random(seed)
    players = defaultdict(PState)
    h2h = defaultdict(int)          # (winner, loser) -> wins
    h2h_s = defaultdict(int)        # (winner, loser, surface) -> wins
    rows = []
    drop = defaultdict(int)
    n = len(df)
    W = {c: f"w_{c}" for c in STAT_COLS}
    L = {c: f"l_{c}" for c in STAT_COLS}
    for i, r in enumerate(df.itertuples(index=False)):
        if i % 50000 == 0 and i:
            print(f"  ... {i:,}/{n:,}")
        rd = r._asdict()
        sw, sl, gw, gl, ret, wo = parse_score(rd.get("score"))
        if wo:
            drop["walkover_or_no_score"] += 1
            continue                                   # no tennis played
        wn, ln = str(rd.get("winner_name")), str(rd.get("loser_name"))
        wid = rd.get("winner_id"); lid = rd.get("loser_id")
        wid = str(int(wid)) if fnum(wid) is not None else "N:" + norm_text(wn)
        lid = str(int(lid)) if fnum(lid) is not None else "N:" + norm_text(ln)
        today = rd["tdate"]; surface = rd["surface"]
        pw, pl = players[wid], players[lid]
        # ---- expectations BEFORE update
        exp_w = 1.0 / (1.0 + 10 ** ((pl.elo - pw.elo) / 400.0))
        # ---- eligibility of this match as a training/test row
        eligible = pw.n >= min_prior and pl.n >= min_prior
        if not eligible:
            drop["too_few_prior_matches"] += 1
        lvl = str(rd.get("tourney_level")).strip()
        if main_only and (rd.get("is_challenger") or lvl in NON_MAIN_LEVELS):
            if eligible:
                drop[f"level_{lvl}" if not rd.get("is_challenger") else "challenger_file"] += 1
            eligible = False
        if eligible:
            fw = features_for(pw, today, surface, pl.elo)
            fl = features_for(pl, today, surface, pw.elo)
            for f, side in ((fw, "winner"), (fl, "loser")):
                f["ht"] = fnum(rd.get(f"{side}_ht"))
                f["age"] = fnum(rd.get(f"{side}_age"))
                f["left"] = 1 if str(rd.get(f"{side}_hand")).upper().startswith("L") else 0
                rk = fnum(rd.get(f"{side}_rank")); f["rank"] = math.log(rk) if rk and rk > 0 else None
                rp = fnum(rd.get(f"{side}_rank_points")); f["rank_pts"] = math.log1p(rp) if rp and rp >= 0 else None
            # matchup facts (prior only)
            hw, hl = h2h[(wid, lid)], h2h[(lid, wid)]
            hsw, hsl = h2h_s[(wid, lid, surface)], h2h_s[(lid, wid, surface)]
            # random orientation: A = winner with prob .5
            a_is_winner = rng.random() < 0.5
            fa, fb = (fw, fl) if a_is_winner else (fl, fw)
            ha, hb_ = (hw, hl) if a_is_winner else (hl, hw)
            hsa, hsb = (hsw, hsl) if a_is_winner else (hsl, hsw)
            row = {"date": today, "year": today.year, "tour": rd["tour"], "surface": surface,
                   "best_of": fnum(rd.get("best_of")) or 3, "level": str(rd.get("tourney_level")),
                   "round": str(rd.get("round")), "tourney_id": str(rd.get("tourney_id")),
                   "player_a": wn if a_is_winner else ln, "player_b": ln if a_is_winner else wn,
                   "label": int(a_is_winner)}
            for k in fa:
                va, vb = fa[k], fb[k]
                row[f"d_{k}"] = (va - vb) if (va is not None and vb is not None) else np.nan
            row["d_h2h"] = ha - hb_
            row["h2h_total"] = ha + hb_
            row["d_h2h_surf"] = hsa - hsb
            row["lefty_vs_righty"] = fa["left"] - fb["left"]
            row["ht_gap"] = row["d_ht"]
            # serve-vs-return cross terms (how A's serve meets B's return and vice versa)
            def cross(x, y):
                return (x - y) if (x is not None and y is not None) else np.nan
            row["mu_a_serve_vs_b_return"] = cross(fa["svc_pts_won"], fb["ret_pts_won"])
            row["mu_b_serve_vs_a_return"] = cross(fb["svc_pts_won"], fa["ret_pts_won"])
            row["mu_serve_edge"] = row["mu_a_serve_vs_b_return"] - row["mu_b_serve_vs_a_return"] \
                if not (np.isnan(row["mu_a_serve_vs_b_return"]) or np.isnan(row["mu_b_serve_vs_a_return"])) else np.nan
            row["mu_a_break_vs_b_hold"] = cross(fa["break_pct"], 1 - fb["hold_pct"] if fb["hold_pct"] is not None else None)
            row["mu_b_break_vs_a_hold"] = cross(fb["break_pct"], 1 - fa["hold_pct"] if fa["hold_pct"] is not None else None)
            row["is_wta"] = int(rd["tour"] == "WTA")
            row["is_bo5"] = int(row["best_of"] == 5)
            rows.append(row)
        # ---- update states (AFTER snapshot)
        wstats = {c: fnum(rd.get(W[c])) for c in STAT_COLS}
        lstats = {c: fnum(rd.get(L[c])) for c in STAT_COLS}
        minutes = fnum(rd.get("minutes"))
        wrow = dict(wstats, sets_w=sw, sets_l=sl, games_w=gw, games_l=gl, minutes=minutes, retired_self=0)
        lrow = dict(lstats, sets_w=sl, sets_l=sw, games_w=gl, games_l=gw, minutes=minutes, retired_self=ret)
        elo_w, elo_l = pw.elo, pl.elo
        update_state(pw, wrow, True, surface, today, exp_w, elo_l, lstats)
        update_state(pl, lrow, False, surface, today, 1 - exp_w, elo_w, wstats)
        # elo update (margin-agnostic)
        kw, kl = k_factor(pw.n - 1), k_factor(pl.n - 1)
        pw.elo += kw * (1 - exp_w); pl.elo -= kl * (1 - exp_w)
        pw.elo_hist.append((today, pw.elo)); pl.elo_hist.append((today, pl.elo))
        se_w, se_l = pw.surf_elo[surface], pl.surf_elo[surface]
        exp_s = 1.0 / (1.0 + 10 ** ((se_l - se_w) / 400.0))
        pw.surf_elo[surface] = se_w + kw * (1 - exp_s); pl.surf_elo[surface] = se_l - kl * (1 - exp_s)
        h2h[(wid, lid)] += 1; h2h_s[(wid, lid, surface)] += 1
    out = pd.DataFrame(rows)
    print(f"Built {len(out):,} eligible rows ({out['year'].min()}..{out['year'].max()})")
    print("  matches not used as rows, by reason: " + ", ".join(f"{k}={v:,}" for k, v in sorted(drop.items(), key=lambda kv: -kv[1])))
    print("  rows per year: " + ", ".join(f"{y}={c:,}" for y, c in out.groupby("year").size().items() if y >= 2019))
    return out


# ----------------------------------------------------------------------------
# modelling
# ----------------------------------------------------------------------------
def feature_columns(df):
    skip = {"date", "year", "tour", "surface", "level", "round", "tourney_id", "player_a", "player_b",
            "label", "best_of"}
    return [c for c in df.columns if c not in skip]


def fit_gbm(Xtr, ytr, Xva, yva, seed):
    try:
        import xgboost as xgb
        m = xgb.XGBClassifier(n_estimators=1500, learning_rate=0.03, max_depth=4, subsample=0.8,
                              colsample_bytree=0.7, min_child_weight=20, reg_lambda=2.0,
                              tree_method="hist", eval_metric="logloss", early_stopping_rounds=80,
                              random_state=seed, n_jobs=4)
        m.fit(Xtr, ytr, eval_set=[(Xva, yva)], verbose=False)
        imp = m.get_booster().get_score(importance_type="total_gain")
        gain = pd.Series({c: imp.get(c, 0.0) for c in Xtr.columns})
        return m, gain, "xgboost"
    except ImportError:
        from sklearn.ensemble import HistGradientBoostingClassifier
        m = HistGradientBoostingClassifier(max_iter=800, learning_rate=0.04, max_depth=4, l2_regularization=2.0,
                                           early_stopping=True, validation_fraction=0.15, random_state=seed)
        m.fit(Xtr, ytr)
        return m, None, "sklearn-hgb"


def fit_logit(Xtr, ytr):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler
    m = make_pipeline(SimpleImputer(strategy="constant", fill_value=0.0), StandardScaler(),
                      LogisticRegression(C=0.05, max_iter=2000))
    m.fit(Xtr, ytr)
    return m


def acc(p, y):
    return float(((p >= 0.5).astype(int) == y).mean())


def logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def group_shares_gain(gain, cols):
    if gain is None:
        return None
    s = defaultdict(float)
    for c in cols:
        s[group_for(c)] += float(gain.get(c, 0.0))
    tot = sum(s.values()) or 1.0
    return {g: 100.0 * v / tot for g, v in sorted(s.items(), key=lambda kv: -kv[1])}


def group_shares_perm(model, X, y, cols, seed, reps=3):
    """Shuffle a whole group at once; accuracy drop = how much the model relied on it."""
    rng = np.random.RandomState(seed)
    base = acc(model.predict_proba(X)[:, 1], y)
    drops = {}
    groups = sorted(set(group_for(c) for c in cols))
    for g in groups:
        gc = [c for c in cols if group_for(c) == g]
        d = 0.0
        for _ in range(reps):
            Xp = X.copy()
            perm = rng.permutation(len(Xp))
            Xp[gc] = Xp[gc].values[perm]
            d += base - acc(model.predict_proba(Xp)[:, 1], y)
        drops[g] = max(d / reps, 0.0)
    tot = sum(drops.values()) or 1.0
    return {g: 100.0 * v / tot for g, v in sorted(drops.items(), key=lambda kv: -kv[1])}, drops


def group_shares_logit(m, cols):
    lr = m.named_steps["logisticregression"]
    coef = np.abs(lr.coef_[0])
    s = defaultdict(float)
    for c, w in zip(cols, coef):
        s[group_for(c)] += float(w)
    tot = sum(s.values()) or 1.0
    return {g: 100.0 * v / tot for g, v in sorted(s.items(), key=lambda kv: -kv[1])}


# ----------------------------------------------------------------------------
# readout 2: market (after the fact only)
# ----------------------------------------------------------------------------
def load_odds(odds_dir):
    files = [f for f in glob.glob(os.path.join(odds_dir, "*")) if re.search(r"\.(xls|xlsx|csv)$", f, re.I)]
    if not files:
        return None
    frames = []
    for fp in files:
        try:
            if fp.lower().endswith(".csv"):
                sheets = {"csv": pd.read_csv(fp, low_memory=False)}
            else:
                sheets = pd.read_excel(fp, sheet_name=None)
        except Exception as e:
            print(f"  ! odds skip {os.path.basename(fp)}: {e}")
            continue
        for _, s in sheets.items():
            if not {"Date", "Winner", "Loser"} <= set(s.columns):
                continue
            keep = ["Date", "Winner", "Loser"] + [c for c in ("PSW", "PSL", "B365W", "B365L", "AvgW", "AvgL", "MaxW", "MaxL") if c in s.columns]
            frames.append(s[keep].copy())
    if not frames:
        return None
    o = pd.concat(frames, ignore_index=True)
    o["Date"] = pd.to_datetime(o["Date"], errors="coerce").dt.date
    o = o[o["Date"].notna()]
    for c in ("PSW", "PSL", "AvgW", "AvgL"):
        if c in o:
            o[c] = pd.to_numeric(o[c], errors="coerce")
    if "PSW" not in o:
        o["PSW"] = np.nan; o["PSL"] = np.nan
    o["PSW"] = o["PSW"].fillna(o.get("AvgW")); o["PSL"] = o["PSL"].fillna(o.get("AvgL"))
    o = o[o["PSW"].notna() & o["PSL"].notna() & (o["PSW"] > 1.0) & (o["PSL"] > 1.0)]
    print(f"Odds rows loaded: {len(o):,} from {len(files)} files")
    return o


def td_key(name):
    t = norm_text(name).split()          # 'Sinner J.' -> ['sinner', 'j']
    if not t:
        return None
    if len(t) == 1:
        return (t[0], "")
    ini = t[-1][0]
    return frozenset({("".join(t[:-1]), ini), (t[0], ini), (t[-2], ini)})


def market_join(test, odds):
    """Attach Pinnacle fair prob for A to each test row (unique match, tournament span window)."""
    if odds is None:
        return test.assign(pinn_a=np.nan)
    idx = defaultdict(list)
    for r in odds.itertuples(index=False):
        kw, kl = td_key(r.Winner), td_key(r.Loser)
        if not kw or not kl:
            continue
        for a in kw:
            for b in kl:
                idx[frozenset({a, b})].append((r.Date, r.PSW, r.PSL, a, b))
    used = set()
    pinn = []
    for r in test.itertuples(index=False):
        ka, kb = name_keys(r.player_a), name_keys(r.player_b)
        best = None
        for a in ka:
            for b in kb:
                for cand in idx.get(frozenset({a, b}), []):
                    d, psw, psl, ka_, kb_ = cand
                    gap = (d - r.date).days
                    if -1 <= gap <= 16 and (id(cand) not in used):
                        if best is None or gap < best[0]:
                            best = (gap, cand)
        if best is None:
            pinn.append(np.nan); continue
        gap, cand = best
        used.add(id(cand))
        d, psw, psl, a_, b_ = cand
        pw, pl = 1 / psw, 1 / psl
        fair_w = pw / (pw + pl)
        # tennis-data's "Winner" is the actual winner, so A is that player iff label == 1
        a_is_w = (r.label == 1)
        pinn.append(fair_w if a_is_w else 1 - fair_w)
    return test.assign(pinn_a=pinn)


# ----------------------------------------------------------------------------
# readout 4: current engine join
# ----------------------------------------------------------------------------
def engine_join(test, wf_path):
    if not os.path.exists(wf_path):
        return test.assign(engine_a=np.nan)
    wf = pd.read_csv(wf_path, usecols=["match_date", "player_a", "player_b", "engine_prob", "label_a_won"])
    wf["d"] = pd.to_datetime(wf["match_date"], errors="coerce").dt.date
    idx = {}
    for r in wf.itertuples(index=False):
        if r.d is None or pd.isna(r.engine_prob):
            continue
        for ka in name_keys(r.player_a):
            for kb in name_keys(r.player_b):
                idx[(r.d, frozenset({ka, kb}))] = (r.player_a, r.engine_prob, ka)
    out = []
    for r in test.itertuples(index=False):
        hit = None
        for ka in name_keys(r.player_a):
            for kb in name_keys(r.player_b):
                hit = idx.get((r.date, frozenset({ka, kb})))
                if hit:
                    break
            if hit:
                break
        if not hit:
            out.append(np.nan); continue
        eng_a_name, prob, ka_ = hit
        a_same = ka_ in name_keys(r.player_a)
        out.append(prob if a_same else 1 - prob)
    return test.assign(engine_a=out)


# ----------------------------------------------------------------------------
# main run
# ----------------------------------------------------------------------------
def run(rows, args):
    cols = feature_columns(rows)
    print(f"\nFeature columns: {len(cols)}   groups: " +
          ", ".join(f"{g}={sum(group_for(c)==g for c in cols)}" for g in
                    ["Rating", "Serve", "Return", "Form", "Physical", "Matchup", "Context"]))
    test_years = sorted(y for y in rows["year"].unique() if y >= args.test_from)
    if not test_years:
        sys.exit("no test years")

    preds = []          # collected test predictions
    gains = []
    perms = []
    if args.walkforward:
        splits = [(y, rows[rows.year < y - 2], rows[(rows.year >= y - 2) & (rows.year < y)], rows[rows.year == y]) for y in test_years]
    else:
        splits = [("fixed", rows[rows.year <= args.test_from - 3],
                   rows[(rows.year > args.test_from - 3) & (rows.year < args.test_from)],
                   rows[rows.year >= args.test_from])]
    logit_model = None
    for tag, tr, va, te in splits:
        if len(tr) < 500 or len(te) == 0:
            continue
        gbm, gain, kind = fit_gbm(tr[cols], tr["label"], va[cols], va["label"], args.seed)
        lg = fit_logit(pd.concat([tr, va])[cols], pd.concat([tr, va])["label"])
        logit_model = lg
        p = gbm.predict_proba(te[cols])[:, 1]
        pl = lg.predict_proba(te[cols])[:, 1]
        # ELO-only reference (plain logistic on d_elo, d_surf_elo)
        elo_ref = fit_logit(pd.concat([tr, va])[["d_elo", "d_surf_elo"]], pd.concat([tr, va])["label"])
        pe = elo_ref.predict_proba(te[["d_elo", "d_surf_elo"]])[:, 1]
        t = te.copy(); t["p_gbm"] = p; t["p_logit"] = pl; t["p_elo"] = pe
        preds.append(t)
        if gain is not None:
            gains.append(gain)
        sh, _ = group_shares_perm(gbm, te[cols].reset_index(drop=True), te["label"].values, cols, args.seed)
        perms.append(sh)
        print(f"  split {tag}: train {len(tr):,} / tune {len(va):,} / test {len(te):,}  "
              f"[{kind}]  test acc {acc(p, te['label'].values):.4f}  logit {acc(pl, te['label'].values):.4f}  "
              f"elo-only {acc(pe, te['label'].values):.4f}")
        if tag == "fixed" and args.leakcheck:
            accs = []
            for s_ in range(3):
                rng = np.random.RandomState(args.seed + s_)
                ys = rng.permutation(tr["label"].values)
                g2, _, _ = fit_gbm(tr[cols], ys, va[cols], rng.permutation(va["label"].values), args.seed + s_)
                accs.append(acc(g2.predict_proba(te[cols])[:, 1], te["label"].values))
            a2 = float(np.mean(accs))
            flag = "OK" if abs(a2 - 0.5) < 0.03 else "!!! ALARM: scrambled labels should average ~50% -- run apex_diagnose.py investigation 8"
            print(f"  leak check (scrambled labels, 3 seeds): {', '.join(f'{a:.3f}' for a in accs)}  mean {a2:.4f}  -> {flag}")
            print("  (a single scrambled run can drift a few points by luck; the 3-seed mean is the number that matters)")
    test = pd.concat(preds, ignore_index=True)
    y = test["label"].values

    # ---------------- readout 1
    print("\n" + "=" * 78)
    print("READOUT 1 -- overall winner accuracy on held-out matches (%d+)" % args.test_from)
    print("=" * 78)
    for name, col in (("Combined model (GBM)", "p_gbm"), ("Combined model (logistic)", "p_logit"), ("ELO-only reference", "p_elo")):
        print(f"  {name:28s} n={len(test):6,}  acc {acc(test[col].values, y)*100:6.2f}%   logloss {logloss(test[col].values, y):.4f}")
    a_g = acc(test["p_gbm"].values, y)
    if a_g > 0.74:
        print("  !!! ALARM: >74% on main-tour matches is above any known ceiling -- check for leakage before believing it.")
    print("\n  by year:")
    for yv, g in test.groupby("year"):
        print(f"    {yv}: n={len(g):5,}  GBM {acc(g['p_gbm'].values, g['label'].values)*100:5.2f}%   "
              f"logit {acc(g['p_logit'].values, g['label'].values)*100:5.2f}%   ELO {acc(g['p_elo'].values, g['label'].values)*100:5.2f}%")
    print("\n  by tour:")
    for tv, g in test.groupby("tour"):
        print(f"    {tv}: n={len(g):5,}  GBM {acc(g['p_gbm'].values, g['label'].values)*100:5.2f}%   ELO {acc(g['p_elo'].values, g['label'].values)*100:5.2f}%")
    print("\n  by model confidence (GBM):")
    conf = np.abs(test["p_gbm"] - 0.5) + 0.5
    for lo, hi in ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)):
        m = (conf >= lo) & (conf < hi)
        if m.sum():
            print(f"    {lo:.2f}-{min(hi,1):.2f}: n={int(m.sum()):5,}  acc {acc(test.loc[m,'p_gbm'].values, y[m])*100:5.2f}%   avg conf {conf[m].mean()*100:5.2f}%")

    # ---------------- readout 2 (market, after the fact)
    print("\n" + "=" * 78)
    print("READOUT 2 -- when the model disagrees with the market, who was right?  (market used ONLY here)")
    print("=" * 78)
    odds = load_odds(args.odds_dir) if os.path.isdir(args.odds_dir) else None
    test = market_join(test, odds)
    mm = test[test["pinn_a"].notna()]
    if len(mm) == 0:
        print("  (no Pinnacle prices matched -- put tennis-data.co.uk files in ./odds/ to enable)")
    else:
        ym = mm["label"].values
        pick_model = (mm["p_gbm"] >= 0.5).astype(int).values
        pick_mkt = (mm["pinn_a"] >= 0.5).astype(int).values
        print(f"  matched to a Pinnacle price: {len(mm):,} of {len(test):,} test rows")
        print(f"  on those rows: model acc {acc(mm['p_gbm'].values, ym)*100:.2f}%   Pinnacle-favourite acc {acc(mm['pinn_a'].values, ym)*100:.2f}%   "
              f"model LL {logloss(mm['p_gbm'].values, ym):.4f}  Pinnacle LL {logloss(mm['pinn_a'].values, ym):.4f}")
        dis = pick_model != pick_mkt
        nd = int(dis.sum())
        if nd:
            mr = float((pick_model[dis] == ym[dis]).mean()); kr = 1 - mr
            print(f"  DISAGREEMENTS: {nd:,} ({100*nd/len(mm):.1f}% of matched)   model right {mr*100:.1f}%   market right {kr*100:.1f}%")
            # by how strongly the model disagrees
            edge = np.abs(mm["p_gbm"].values - mm["pinn_a"].values)
            for lo, hi in ((0.0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 1.0)):
                m = dis & (edge >= lo) & (edge < hi)
                if m.sum():
                    print(f"    |model - market| {lo:.2f}-{hi:.2f}: n={int(m.sum()):4,}  model right {100*(pick_model[m]==ym[m]).mean():5.1f}%")
            # model backing the market underdog
            dogpick = dis   # any disagreement = model picked the market underdog
            dp = mm[dogpick]; ydp = ym[dogpick]
            print(f"  model backed the market UNDERDOG {int(dogpick.sum()):,} times: those dogs won {100*((dp['p_gbm'].values>=0.5).astype(int)==ydp).mean():.1f}%  "
                  f"(market implied avg {100*np.where(dp['pinn_a'].values<0.5, dp['pinn_a'].values, 1-dp['pinn_a'].values).mean():.1f}%)")
        agree = ~dis
        if agree.sum():
            print(f"  agreements: {int(agree.sum()):,}  both right {100*(pick_model[agree]==ym[agree]).mean():.1f}%")

    # ---------------- readout 3 (group weights)
    print("\n" + "=" * 78)
    print("READOUT 3 -- how much each feature GROUP matters (learned, two measures)")
    print("=" * 78)
    if gains:
        g_all = pd.concat(gains, axis=1).mean(axis=1)
        gs = group_shares_gain(g_all, cols)
        print("  Gain share (what the trees split on):")
        for g, v in gs.items():
            print(f"    {g:9s} {v:5.1f}%")
    ps = {}
    for sh in perms:
        for g, v in sh.items():
            ps[g] = ps.get(g, 0) + v / len(perms)
    print("  Permutation share (accuracy lost when a group is scrambled -- what the model truly relies on):")
    for g, v in sorted(ps.items(), key=lambda kv: -kv[1]):
        print(f"    {g:9s} {v:5.1f}%")
    if logit_model is not None:
        ls = group_shares_logit(logit_model, cols)
        print("  Logistic |weight| share (second opinion, linear model):")
        for g, v in ls.items():
            print(f"    {g:9s} {v:5.1f}%")
    # headline block: the number the whole project cares about
    print("\n  ------------------------------------------------")
    print("  FINAL MODEL CONTRIBUTION (permutation share = what the model truly relies on)")
    print("  ------------------------------------------------")
    for g, v in sorted(ps.items(), key=lambda kv: -kv[1]):
        if g != "Context":
            print(f"    {g:9s} Group {v:5.1f}%")
    rating_share = ps.get("Rating", 0.0)
    if rating_share >= 70:
        print(f"  --> Rating group carries {rating_share:.0f}%: the model is STILL basically an ELO engine. Richer inputs did not take over.")
    elif rating_share >= 40:
        print(f"  --> Rating group carries {rating_share:.0f}%: ELO is the largest voice but tennis inputs share the decision.")
    else:
        print(f"  --> Rating group carries {rating_share:.0f}%: tennis-reality groups are driving the decision, ELO is one voice among many.")
    if gains:
        print("\n  top 15 individual features by gain:")
        for c, v in g_all.sort_values(ascending=False).head(15).items():
            print(f"    {c:28s} {100*v/g_all.sum():5.1f}%  [{group_for(c)}]")

    # ---------------- readout 4 (vs current engine)
    print("\n" + "=" * 78)
    print("READOUT 4 -- same-match comparison vs the current ELO-dominated engine")
    print("=" * 78)
    test = engine_join(test, args.walkforward_rows)
    ee = test[test["engine_a"].notna()]
    print(f"  ELO-only reference on all test rows: {acc(test['p_elo'].values, y)*100:.2f}%   combined GBM: {acc(test['p_gbm'].values, y)*100:.2f}%   "
          f"(+{(acc(test['p_gbm'].values, y)-acc(test['p_elo'].values, y))*100:.2f} pts)")
    if len(ee) == 0:
        print("  (walkforward_rows.csv not matched -- engine comparison skipped)")
    else:
        ye = ee["label"].values
        print(f"  matched to current engine output: {len(ee):,} of {len(test):,} test rows")
        print(f"    current engine acc {acc(ee['engine_a'].values, ye)*100:.2f}%   combined GBM {acc(ee['p_gbm'].values, ye)*100:.2f}%   "
              f"logit {acc(ee['p_logit'].values, ye)*100:.2f}%")
        both = (ee["engine_a"] >= 0.5).astype(int).values != (ee["p_gbm"] >= 0.5).astype(int).values
        if both.sum():
            print(f"    engine vs new model disagree on {int(both.sum()):,} matches: new model right {100*((ee['p_gbm'].values[both]>=0.5).astype(int)==ye[both]).mean():.1f}%")

    # save
    out = args.out
    test.to_csv(out, index=False)
    print(f"\nSaved per-match predictions -> {out}   ({len(test):,} rows)   [{MARKER}]")


# ----------------------------------------------------------------------------
# self-test with fake Sackmann-format data
# ----------------------------------------------------------------------------
def make_fake(dirpath, seed=1):
    rng = np.random.RandomState(seed)
    os.makedirs(dirpath, exist_ok=True)
    n_players = 120
    skill = rng.normal(0, 1, n_players)          # hidden true strength
    serve = rng.normal(0, 1, n_players)          # serve talent -> shows in stats AND wins
    ht = rng.normal(185, 7, n_players).round()
    hand = rng.choice(["R", "L"], n_players, p=[0.85, 0.15])
    def sur(i):
        return "S" + "".join(chr(97 + (i // 26 ** k) % 26) for k in range(3))
    def pname(i):
        return f"John {sur(i)}"
    for year in range(2015, 2027):
        rows = []
        for t in range(18):
            tdate = date(year, 1 + t // 2, 3 + 14 * (t % 2))
            level = "A" if t % 4 else "M"
            surface = ["Hard", "Clay", "Grass"][t % 3]
            for m in range(30):
                a, b = rng.choice(n_players, 2, replace=False)
                strength_a = skill[a] + 0.8 * serve[a]; strength_b = skill[b] + 0.8 * serve[b]
                pa = 1 / (1 + math.exp(-(strength_a - strength_b)))
                w, l = (a, b) if rng.rand() < pa else (b, a)
                def stats(p):
                    svpt = int(rng.randint(60, 110)); fin = int(svpt * rng.uniform(0.55, 0.7))
                    fw = int(fin * min(0.9, max(0.5, 0.72 + 0.05 * serve[p] + rng.normal(0, 0.04))))
                    sw = int((svpt - fin) * min(0.8, max(0.3, 0.5 + 0.04 * serve[p] + rng.normal(0, 0.05))))
                    sg = int(rng.randint(8, 14)); bpf = int(rng.randint(0, 8)); bps = int(bpf * rng.uniform(0.4, 0.8))
                    return dict(ace=int(rng.poisson(4 + 3 * max(serve[p], 0))), df=int(rng.poisson(3)), svpt=svpt,
                                **{"1stIn": fin, "1stWon": fw, "2ndWon": sw, "SvGms": sg, "bpSaved": bps, "bpFaced": bpf})
                ws, ls = stats(w), stats(l)
                score = "6-4 6-3" if rng.rand() < 0.6 else "3-6 6-4 7-5"
                if rng.rand() < 0.02:
                    score += " RET"
                r = dict(tourney_id=f"{year}-{t}", tourney_name=f"T{t}", surface=surface, draw_size=32, tourney_level=level,
                         indoor=0, tourney_date=int(tdate.strftime("%Y%m%d")), match_num=m,
                         winner_id=int(w), winner_seed="", winner_entry="", winner_name=pname(w), winner_hand=hand[w],
                         winner_ht=ht[w], winner_ioc="XXX", winner_age=25.0, winner_rank=int(100 - 40 * skill[w]) if 100 - 40 * skill[w] > 1 else 1,
                         winner_rank_points=max(1, int(3000 + 800 * skill[w] + rng.normal(0, 600))), loser_id=int(l), loser_seed="", loser_entry="", loser_name=pname(l),
                         loser_hand=hand[l], loser_ht=ht[l], loser_ioc="XXX", loser_age=25.0,
                         loser_rank=int(100 - 40 * skill[l]) if 100 - 40 * skill[l] > 1 else 1, loser_rank_points=max(1, int(3000 + 800 * skill[l] + rng.normal(0, 600))),
                         score=score, best_of=3, round=["R32", "R16", "QF", "SF", "F"][m % 5], minutes=int(rng.randint(60, 180)))
                for k, v in ws.items(): r[f"w_{k}"] = v
                for k, v in ls.items(): r[f"l_{k}"] = v
                rows.append(r)
        pd.DataFrame(rows).to_csv(os.path.join(dirpath, f"{year}.csv"), index=False)
    # fake odds (tennis-data format) for 2024+: Pinnacle = truth-ish with noise
    os.makedirs(os.path.join(dirpath, "odds"), exist_ok=True)
    od = []
    for year in (2024, 2025, 2026):
        df = pd.read_csv(os.path.join(dirpath, f"{year}.csv"))
        for r in df.itertuples(index=False):
            w, l = int(r.winner_id), int(r.loser_id)
            pw = 1 / (1 + math.exp(-(skill[w] + 0.8 * serve[w] - skill[l] - 0.8 * serve[l] + rng.normal(0, 0.5))))
            d = parse_ymd(r.tourney_date) + timedelta(days=int(rng.randint(0, 6)))
            od.append(dict(Date=d.isoformat(), Winner=f"{sur(w)} J.", Loser=f"{sur(l)} J.", PSW=round(1.03 / pw, 2), PSL=round(1.03 / (1 - pw), 2)))
    pd.DataFrame(od).to_csv(os.path.join(dirpath, "odds", "fake_odds.csv"), index=False)
    # fake walkforward_rows.csv (engine = elo-ish guess)
    wf = []
    for year in (2024, 2025, 2026):
        df = pd.read_csv(os.path.join(dirpath, f"{year}.csv"))
        for r in df.itertuples(index=False):
            w, l = int(r.winner_id), int(r.loser_id)
            pw = 1 / (1 + math.exp(-(skill[w] - skill[l] + rng.normal(0, 0.8))))
            wf.append(dict(match_date=parse_ymd(r.tourney_date).isoformat(), player_a=r.winner_name, player_b=r.loser_name,
                           engine_prob=pw, label_a_won=1))
    pd.DataFrame(wf).to_csv(os.path.join(dirpath, "walkforward_rows.csv"), index=False)
    return dirpath


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sack-dir", default="ATP & WTA, 1968-2026")
    ap.add_argument("--year-from", type=int, default=2005, help="first year of history to read (state warm-up)")
    ap.add_argument("--test-from", type=int, default=2024)
    ap.add_argument("--min-prior", type=int, default=10, help="both players need this many prior matches")
    ap.add_argument("--include-challenger", action="store_true", help="also read YYYY_challenger.csv (state + rows)")
    ap.add_argument("--all-levels", action="store_true", help="evaluate on every level, not just main tour")
    ap.add_argument("--walkforward", action="store_true", help="refit for every test year instead of one fixed split")
    ap.add_argument("--odds-dir", default="odds")
    ap.add_argument("--walkforward-rows", default="walkforward_rows.csv")
    ap.add_argument("--out", default="apex_combined_predictions.csv")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-leakcheck", dest="leakcheck", action="store_false")
    ap.add_argument("--selftest", action="store_true", help="run on generated fake data")
    args = ap.parse_args()

    if args.selftest:
        d = make_fake("/tmp/apex_fake")
        args.sack_dir = d; args.odds_dir = os.path.join(d, "odds"); args.walkforward_rows = os.path.join(d, "walkforward_rows.csv")
        args.year_from = 2015; args.min_prior = 5; args.out = "/tmp/apex_fake/preds.csv"
        print("SELF-TEST on fake data: serve talent is planted to drive wins; expect Serve/Return groups to carry weight and combined > ELO-only.")

    print(f"[{MARKER}] objective: identify the actual winner from tennis reality; market is evaluation-only.")
    df = load_sackmann(args.sack_dir, args.year_from, args.include_challenger)
    rows = build_rows(df, args.min_prior, args.seed, main_only=not args.all_levels)
    run(rows, args)


if __name__ == "__main__":
    main()
