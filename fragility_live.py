#!/usr/bin/env python3
"""
fragility_live.py -- APEX-FRAGILITY-LIVE-2026-08-20

Forward test of the FROZEN Fragility V3 score. Positioning (by design and by
evidence): "Elevated upset risk among modest favorites" -- NOT a predictor of
large-favorite collapse, and not a winner model. The score shifts risk by a
few percentage points in competitive matches; the wording never claims more.

FREEZE RULES (enforced by this file):
  * loads fragility_v3_model.json exactly as written by apex_fragility_v3.py
  * contains NO fitting, tuning, thresholding, or recalibration code
  * --freeze runs ONCE: it completes the frozen artifact by storing the
    within-band percentile reference (computed from <=2023 favorites with the
    frozen coefficients -- a deterministic lookup table, not a re-fit) and
    refuses to run twice
  * every logged row records the SHA256 of the model json; --report refuses
    mixed hashes, so any tampering with the frozen model shows up

Commands:
  --freeze --rows apex_batch_a_rows.csv --prob-col pinn_a
  --spec                       print frozen feature list, coefficients, score
                               formula, wording tiers, logging schema, and a
                               worked example from a real <=2023 match
  --score "Fav Name" "Dog Name" --pinn 0.66 --surface Hard
          [--d-elo N --d-age N --d-switch N]     (manual features)
          [--tour atp --best-of 3 --tournament T --round R32 --date YYYY-MM-DD]
          If --d-elo/--d-age/--d-switch are omitted the script tries the
          apex_v2_live hook to build them from the live player state.
  --settle-sackmann --sackmann-dir "ATP & WTA, 1968-2026"
  --result MATCH_ID FAV_WON|FAV_LOST
  --report                     bucket tables on settled forward rows
  --selftest
"""
import argparse, hashlib, json, math, os, glob, re, sys
from datetime import datetime, date
import numpy as np
import pandas as pd

MARKER = "APEX-FRAGILITY-LIVE-2026-08-20"
MODEL_JSON = "fragility_v3_model.json"
REF_JSON = "fragility_v3_live_ref.json"
LOG_CSV = "fragility_forward_log.csv"
EXPECTED_MODEL_MARKER = "APEX-FRAGILITY-V3-2026-08-20"
BANDS = [(0.50, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 1.001)]
BAND_NAMES = ["50-60%", "60-65%", "65-70%", "70-75%", "75-80%", "80%+"]
BUCKETS = [(0, 20), (21, 40), (41, 60), (61, 80), (81, 100)]
LN10_400 = math.log(10) / 400.0
LOG_COLUMNS = ["match_id", "scored_at", "match_date", "tour", "tournament", "round",
               "surface", "best_of", "fav_name", "dog_name", "pinn_fav_p",
               "price_source",
               "elo_gap", "d_surface_switch", "d_age",
               "z_elo_gap", "z_d_surface_switch", "z_d_age",
               "delta", "fragility_score", "threat_score", "tier", "reasons",
               "model_sha", "result", "settled_at"]
TIERS = [  # (min_score_inclusive, label)
    (0,  "No elevated upset risk identified."),
    (41, "Some upset-risk indicators present."),
    (71, "Elevated upset risk relative to price."),
    (89, "Multiple fragility indicators present."),
]
HIGH_PRICE_NOTE = ("Favorite priced above 72%. Historical fragility evidence is "
                   "weaker in this range. Informational only.")
REASON_POS = {"elo_gap": "Market discounting favorite relative to ratings",
              "d_surface_switch": "Recent surface switch by the favorite",
              "d_age": "Older favorite versus younger opponent"}
REASON_NEG = {"elo_gap": "Market price fully backs the ratings edge",
              "d_surface_switch": "Favorite settled on this surface",
              "d_age": "Favorite is the younger player"}
DOG_POS = {"elo_gap": "Market rates the underdog better than the ratings do",
           "d_surface_switch": "Opponent (favorite) just switched surfaces",
           "d_age": "Young underdog against an older favorite"}


def sha256_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def band_of(p):
    for (lo, hi), nm in zip(BANDS, BAND_NAMES):
        if lo <= p < hi:
            return nm
    return None


def norm_name(s):
    return " ".join(sorted(re.sub(r"[^a-z ]", "", str(s).lower().strip()).split()))


def load_model():
    assert os.path.exists(MODEL_JSON), "missing %s -- run apex_fragility_v3.py --run first" % MODEL_JSON
    m = json.load(open(MODEL_JSON))
    assert m.get("marker") == EXPECTED_MODEL_MARKER, \
        "model marker %r != expected %r -- refusing (freeze rule)" % (m.get("marker"), EXPECTED_MODEL_MARKER)
    assert list(m["components"]) == ["elo_gap", "d_surface_switch", "d_age"], \
        "frozen component list changed -- refusing"
    return m, sha256_file(MODEL_JSON)


def load_ref():
    assert os.path.exists(REF_JSON), "missing %s -- run --freeze once" % REF_JSON
    r = json.load(open(REF_JSON))
    assert r.get("model_sha"), "ref file corrupt"
    return r


# --------------------------------------------------------------- scoring core
def compute(model, ref, pinn_fav_p, d_elo, d_age, d_switch):
    """Returns dict with z's, delta, score, tier, reasons. Pure lookup: no fit."""
    feats = {"elo_gap": d_elo * LN10_400 - logit(pinn_fav_p),
             "d_surface_switch": d_switch, "d_age": d_age}
    z = {}
    for c in model["components"]:
        x = feats[c]
        if x is None or (isinstance(x, float) and math.isnan(x)):
            x = model["med"][c]
        z[c] = (x - model["mu"][c]) / (model["sd"][c] or 1.0)
    delta = sum(model["beta"][c] * z[c] for c in model["components"])
    band = band_of(pinn_fav_p)
    ref_arr = ref["percentile_ref"].get(band) if band else None
    score = None
    if ref_arr:
        score = 100.0 * float(np.searchsorted(np.array(ref_arr), delta, side="right")) / len(ref_arr)
    tier = TIERS[0][1]
    for lo, label in TIERS:
        if score is not None and score >= lo:
            tier = label
    contrib = sorted(((c, model["beta"][c] * z[c]) for c in model["components"]),
                     key=lambda t: -t[1])
    # explanation fix: high scores ALWAYS show their actual top contributors,
    # even when each individual contribution is small
    reasons, dog_reasons = [], []
    if score is not None and score >= 61:
        for c, v in contrib[:3]:
            if v > 0:
                reasons.append(REASON_POS[c])
                dog_reasons.append(DOG_POS[c])
        if not reasons:  # defensive: cannot happen if score is high, but never blank
            c, v = contrib[0]
            reasons = [REASON_POS[c] + " (mild)"]
            dog_reasons = [DOG_POS[c] + " (mild)"]
    else:
        for c, v in contrib:
            if v < -0.03:
                reasons.append(REASON_NEG[c])
        if not reasons:
            reasons = ["No strong indicators either way"]
    return {"feats": feats, "z": z, "delta": delta, "band": band, "score": score,
            "tier": tier, "reasons": reasons, "dog_reasons": dog_reasons,
            "contrib": contrib}


def render_card(fav, dog, pinn, res):
    lines = []
    lines.append("Favorite: %s   vs   %s" % (fav, dog))
    lines.append("Favorite Probability (Pinnacle): %.0f%%" % (100 * pinn))
    if res["score"] is None:
        lines.append("Fragility Score: unavailable (price outside reference bands)")
        return "\n".join(lines)
    lines.append("Fragility Score: %d -- %s" % (round(res["score"]), res["tier"]))
    lines.append("Underdog Threat Score: %d" % round(res["score"]))
    lines.append("Factors:")
    for t in res["reasons"]:
        lines.append("  - %s" % t)
    if res["dog_reasons"]:
        lines.append("Underdog view:")
        for t in res["dog_reasons"]:
            lines.append("  - %s" % t)
    lines.append("Context: this score shifts upset risk by a few percentage points "
                 "in competitive matches; it does not predict outcomes.")
    if pinn > 0.72:
        lines.append(HIGH_PRICE_NOTE)
    return "\n".join(lines)


# --------------------------------------------------------------- freeze / spec
def freeze(rows_path, prob_col):
    model, sha = load_model()
    assert not os.path.exists(REF_JSON), \
        "%s already exists -- freeze runs ONCE; delete it only if you intend to restart the forward test" % REF_JSON
    df = pd.read_csv(rows_path, low_memory=False)
    df = df[df[prob_col].notna()].copy()
    if "year" not in df.columns:
        df["year"] = pd.to_datetime(df["date"]).dt.year
    fav_is_a = df[prob_col] >= 0.5
    fav_p = np.where(fav_is_a, df[prob_col], 1 - df[prob_col])
    sgn = np.where(fav_is_a, 1.0, -1.0)
    feats = pd.DataFrame({
        "elo_gap": df["d_elo"].values * sgn * LN10_400 - np.array([logit(p) for p in fav_p]),
        "d_surface_switch": df["d_surface_switch"].values * sgn,
        "d_age": df["d_age"].values * sgn})
    z = pd.DataFrame({c: (feats[c].fillna(model["med"][c]) - model["mu"][c]) /
                      (model["sd"][c] or 1.0) for c in model["components"]})
    delta = z.values @ np.array([model["beta"][c] for c in model["components"]])
    fit_m = (df["year"] <= 2023).values
    ref = {"marker": MARKER, "model_sha": sha, "frozen_at": datetime.now().isoformat(),
           "percentile_ref": {}}
    for (lo, hi), nm in zip(BANDS, BAND_NAMES):
        m = fit_m & (fav_p >= lo) & (fav_p < hi)
        arr = np.sort(delta[m])
        if len(arr) >= 200:
            step = max(1, len(arr) // 800)
            ref["percentile_ref"][nm] = [float(x) for x in arr[::step]]
    # store one real <=2023 worked example for --spec
    cand = np.where(fit_m & (fav_p >= 0.60) & (fav_p <= 0.70) &
                    feats.notna().all(axis=1).values)[0]
    i = int(cand[len(cand) // 2])
    ref["worked_example"] = {
        "fav_name": str(df.iloc[i]["player_a"] if fav_is_a.iloc[i] else df.iloc[i]["player_b"]),
        "dog_name": str(df.iloc[i]["player_b"] if fav_is_a.iloc[i] else df.iloc[i]["player_a"]),
        "date": str(df.iloc[i].get("date", "")), "pinn_fav_p": float(fav_p[i]),
        "d_elo": float(df.iloc[i]["d_elo"] * sgn[i]),
        "d_age": float(df.iloc[i]["d_age"] * sgn[i]),
        "d_surface_switch": float(df.iloc[i]["d_surface_switch"] * sgn[i])}
    json.dump(ref, open(REF_JSON, "w"))
    print("frozen: %s written (model sha %s...)" % (REF_JSON, sha[:16]))
    print("bands stored:", ", ".join(sorted(ref["percentile_ref"])))


def spec():
    model, sha = load_model()
    ref = load_ref()
    assert ref["model_sha"] == sha, "model json changed since freeze -- FREEZE VIOLATION"
    print(MARKER, "-- FROZEN SPECIFICATION")
    print("model file sha256: %s" % sha)
    print("frozen at: %s" % ref.get("frozen_at"))
    print("\nFEATURES (exactly three, favorite-frame):")
    print("  elo_gap          = d_elo * ln(10)/400 - logit(Pinnacle fav prob)")
    print("  d_surface_switch = favorite's surface-switch flag minus underdog's")
    print("  d_age            = favorite age minus underdog age")
    print("\nFINAL COEFFICIENTS (log-odds of favorite loss per 1 SD, price-controlled):")
    for c in model["components"]:
        print("  %-18s beta=%+0.5f   mu=%+0.5f  sd=%0.5f  median=%+0.5f"
              % (c, model["beta"][c], model["mu"][c], model["sd"][c], model["med"][c]))
    print("\nSCORE FORMULA:")
    print("  z_i   = (x_i - mu_i) / sd_i        (missing x_i -> median first)")
    print("  delta = sum(beta_i * z_i)          (price term deliberately excluded)")
    print("  score = percentile of delta among 2010-2023 favorites in the SAME")
    print("          Pinnacle price band (frozen lookup table, %d bands)"
          % len(ref["percentile_ref"]))
    print("  Underdog Threat Score = same value, underdog-side wording")
    print("\nWORDING TIERS: 0-40 '%s' | 41-70 '%s' | 71-88 '%s' | 89-100 '%s'"
          % (TIERS[0][1], TIERS[1][1], TIERS[2][1], TIERS[3][1]))
    print("  plus, whenever the favorite is priced above 72%%: '%s'" % HIGH_PRICE_NOTE)
    print("\nLOGGING SCHEMA (%s):" % LOG_CSV)
    print("  " + ", ".join(LOG_COLUMNS))
    w = ref["worked_example"]
    print("\nWORKED EXAMPLE (real <=2023 match from the frozen data):")
    res = compute(model, ref, w["pinn_fav_p"], w["d_elo"], w["d_age"], w["d_surface_switch"])
    print("  %s vs %s on %s, Pinnacle fav prob %.3f" %
          (w["fav_name"], w["dog_name"], w["date"], w["pinn_fav_p"]))
    print("  raw: d_elo=%+.1f  d_age=%+.2f  d_surface_switch=%+.1f"
          % (w["d_elo"], w["d_age"], w["d_surface_switch"]))
    for c in model["components"]:
        print("  %-18s x=%+8.4f  z=%+7.3f  beta*z=%+8.5f"
              % (c, res["feats"][c], res["z"][c], model["beta"][c] * res["z"][c]))
    print("  delta=%+0.5f  band=%s  ->  score=%d" % (res["delta"], res["band"], round(res["score"])))
    print()
    print(render_card(w["fav_name"], w["dog_name"], w["pinn_fav_p"], res))


# --------------------------------------------------------------- live scoring
def try_live_features(fav, dog, surface):
    """Feature hook. Primary: fragility_state (validated against the research
    cache; refuses unless fragility_features.ok exists). Fallback: apex_v2_live.
    Returns (d_elo, d_age, d_switch) in favorite frame, or None with a message."""
    try:
        import fragility_state as fs
        row = fs.features_for_pair(fav, dog, surface=surface)
        return (float(row["d_elo"]), float(row["d_age"]),
                float(row["d_surface_switch"])), None
    except SystemExit as e:      # unresolvable/ambiguous name -- surface it
        return None, str(e)
    except AssertionError as e:  # state missing or not validated
        return None, ("fragility_state gate: %s" % e)
    except ImportError:
        pass
    try:
        import apex_v2_live as avl
    except Exception as e:
        return None, ("no fragility_state.py and apex_v2_live not importable (%s) -- "
                      "run fragility_state.py --build then --validate, or pass "
                      "--d-elo/--d-age/--d-switch manually" % e)
    for fn in ("live_row", "build_live_row", "features_for_pair"):
        f = getattr(avl, fn, None)
        if f is None:
            continue
        try:
            row = f(fav, dog, surface=surface)
            if isinstance(row, dict) and all(k in row for k in ("d_elo", "d_age", "d_surface_switch")):
                return (float(row["d_elo"]), float(row["d_age"]),
                        float(row["d_surface_switch"])), None
        except Exception as e:
            return None, "apex_v2_live.%s failed: %s" % (fn, e)
    return None, ("no compatible feature function found -- pass "
                  "--d-elo/--d-age/--d-switch manually")


def append_log(row):
    new = not os.path.exists(LOG_CSV)
    pd.DataFrame([row], columns=LOG_COLUMNS).to_csv(LOG_CSV, mode="a", header=new, index=False)


def _match_id(mdate, fav, dog):
    return hashlib.sha256(("%s|%s|%s" % (mdate, norm_name(fav), norm_name(dog)))
                          .encode()).hexdigest()[:12]


def _already_logged(mid):
    if not os.path.exists(LOG_CSV):
        return False
    log = _read_log()
    return bool((log["match_id"] == mid).any())


def _log_score(mid, mdate, a_meta, fav, dog, pinn, feats, res, sha,
               price_source="pinnacle"):
    """Single write path with duplicate guard: one row per match_id, ever.
    price_source: 'pinnacle' = actual Pinnacle-based probability input;
    'devig_entered' = user-entered market odds after de-vigging."""
    if _already_logged(mid):
        return False
    assert price_source in ("pinnacle", "devig_entered")
    append_log({"match_id": mid, "scored_at": datetime.now().isoformat(),
                "match_date": mdate, "tour": a_meta.get("tour", ""),
                "tournament": a_meta.get("tournament", ""),
                "round": a_meta.get("round", ""), "surface": a_meta.get("surface", ""),
                "best_of": a_meta.get("best_of", 3),
                "fav_name": fav, "dog_name": dog, "pinn_fav_p": pinn,
                "price_source": price_source,
                "elo_gap": res["feats"]["elo_gap"], "d_surface_switch": feats[2],
                "d_age": feats[1], "z_elo_gap": res["z"]["elo_gap"],
                "z_d_surface_switch": res["z"]["d_surface_switch"],
                "z_d_age": res["z"]["d_age"], "delta": res["delta"],
                "fragility_score": res["score"], "threat_score": res["score"],
                "tier": res["tier"], "reasons": " | ".join(res["reasons"]),
                "model_sha": sha, "result": "", "settled_at": ""})
    return True


def format_block(res, pinn):
    """The Discord-facing informational block. Display only -- never touches
    probabilities, confidence, or pick logic."""
    lines = ["Fragility Score: %d" % round(res["score"]),
             "Underdog Threat Score: %d" % round(res["score"]),
             res["tier"]]
    real = [t for t in res["reasons"] if "No strong" not in t]
    if real:
        lines.append("Factors:")
        lines += ["- %s" % t for t in real]
    if pinn > 0.72:
        lines.append(HIGH_PRICE_NOTE)
    return "\n".join(lines)


def score_for_apex(fav_name, dog_name, pinn_fav_p, surface="Hard", tour="",
                   tournament="", round_="", best_of=3, match_date=None,
                   price_source="pinnacle"):
    """Entry point for openclaw_core / Discord integration.

    Informational layer contract:
      * FAIL-OPEN: any error returns None -- callers must treat None as
        'no fragility block' and never let this affect the pick.
      * Logs each match once (duplicate guard) with the frozen model SHA.
      * Reads only frozen artifacts; no fitting code exists in this module.
    Returns dict(score, threat_score, tier, reasons, dog_reasons, block) or None.
    """
    try:
        if not (0.5 <= float(pinn_fav_p) < 1.0):
            return None
        model, sha = load_model()
        ref = load_ref()
        if ref["model_sha"] != sha:
            return None                      # freeze violation -> stay silent, never break picks
        import fragility_state as fs
        row = fs.features_for_pair(fav_name, dog_name, surface=surface)
        feats = (float(row["d_elo"]), float(row["d_age"]), float(row["d_surface_switch"]))
        res = compute(model, ref, float(pinn_fav_p), *feats)
        if res["score"] is None:
            return None
        mdate = match_date or date.today().isoformat()
        mid = _match_id(mdate, fav_name, dog_name)
        _log_score(mid, mdate, {"tour": tour, "tournament": tournament,
                                "round": round_, "surface": surface,
                                "best_of": best_of},
                   fav_name, dog_name, float(pinn_fav_p), feats, res, sha,
                   price_source=price_source)
        return {"match_id": mid, "score": round(res["score"]),
                "threat_score": round(res["score"]), "tier": res["tier"],
                "reasons": res["reasons"], "dog_reasons": res["dog_reasons"],
                "block": format_block(res, float(pinn_fav_p))}
    except (Exception, SystemExit) as e:
        try:
            print("fragility layer unavailable (%s) -- pick flow unaffected" % e)
        except Exception:
            pass
        return None


def score_match(a):
    model, sha = load_model()
    ref = load_ref()
    assert ref["model_sha"] == sha, ("model json changed since freeze -- FREEZE VIOLATION; "
                                     "restore the original fragility_v3_model.json")
    fav, dog, pinn = a.players[0], a.players[1], a.pinn
    assert 0.5 <= pinn < 1.0, "--pinn must be the FAVORITE'S probability (0.5-1.0)"
    if a.d_elo is not None and a.d_age is not None and a.d_switch is not None:
        feats, err = (a.d_elo, a.d_age, a.d_switch), None
    else:
        feats, err = try_live_features(fav, dog, a.surface)
    if feats is None:
        raise SystemExit("cannot build features: %s" % err)
    d_elo, d_age, d_switch = feats
    res = compute(model, ref, pinn, d_elo, d_age, d_switch)
    print(render_card(fav, dog, pinn, res))
    mdate = a.date or date.today().isoformat()
    mid = _match_id(mdate, fav, dog)
    wrote = _log_score(mid, mdate, {"tour": a.tour, "tournament": a.tournament,
                                    "round": a.round, "surface": a.surface,
                                    "best_of": a.best_of},
                       fav, dog, pinn, (d_elo, d_age, d_switch), res, sha)
    if wrote:
        print("\nlogged as %s in %s" % (mid, LOG_CSV))
    else:
        print("\nalready logged as %s -- duplicate write refused" % mid)


# --------------------------------------------------------------- settle/report
def _read_log():
    log = pd.read_csv(LOG_CSV)
    for c in ("result", "settled_at"):  # empty cols load as float NaN -> make text
        log[c] = log[c].astype(object).where(log[c].notna(), "")
    return log


def settle_sackmann(sack_dir):
    if not os.path.exists(LOG_CSV):
        print("no log yet"); return
    log = _read_log()
    open_m = (log["result"] == "")
    if not open_m.any():
        print("nothing to settle"); return
    files = []
    for pat in ("[12][0-9][0-9][0-9].csv", "[12][0-9][0-9][0-9]_wta.csv"):
        files += glob.glob(os.path.join(sack_dir, pat))
    years = {int(re.findall(r"(\d{4})", os.path.basename(f))[0]) for f in files}
    use = [f for f in files if int(re.findall(r"(\d{4})", os.path.basename(f))[0]) >= max(min(years), 2026)]
    res_map = {}
    for f in use:
        try:
            t = pd.read_csv(f, usecols=lambda c: c in
                            ("winner_name", "loser_name", "tourney_date"), low_memory=False)
        except Exception:
            continue
        for wn, ln in zip(t["winner_name"], t["loser_name"]):
            res_map[frozenset([norm_name(wn), norm_name(ln)])] = norm_name(wn)
    n = 0
    for i in log.index[open_m]:
        key = frozenset([norm_name(log.at[i, "fav_name"]), norm_name(log.at[i, "dog_name"])])
        w = res_map.get(key)
        if w is None:
            continue
        log.at[i, "result"] = "FAV_WON" if w == norm_name(log.at[i, "fav_name"]) else "FAV_LOST"
        log.at[i, "settled_at"] = datetime.now().isoformat()
        n += 1
    log.to_csv(LOG_CSV, index=False)
    print("settled %d matches (name-pair matching against current-year Sackmann; "
          "same-pair rematches settle to the most recent result -- verify oddities "
          "with --result)" % n)


def set_result(match_id, result):
    assert result in ("FAV_WON", "FAV_LOST")
    log = _read_log()
    m = log["match_id"] == match_id
    assert m.any(), "match_id not found"
    log.loc[m, "result"] = result
    log.loc[m, "settled_at"] = datetime.now().isoformat()
    log.to_csv(LOG_CSV, index=False)
    print("set %s -> %s" % (match_id, result))


def report():
    assert os.path.exists(LOG_CSV), "no forward log yet"
    log = _read_log()
    print(MARKER, "-- FORWARD-TEST REPORT")
    hashes = log["model_sha"].dropna().unique()
    assert len(hashes) == 1, "FREEZE VIOLATION: log contains %d different model hashes" % len(hashes)
    print("model sha: %s...  (single frozen model: OK)" % hashes[0][:16])
    st = log[log["result"].isin(["FAV_WON", "FAV_LOST"])].copy()
    print("scored: %d   settled: %d   dates %s -> %s"
          % (len(log), len(st), log["match_date"].min(), log["match_date"].max()))
    if len(st) == 0:
        print("no settled matches yet"); return
    st["fav_lost"] = (st["result"] == "FAV_LOST").astype(int)
    if "price_source" in log.columns:
        print("rows by price_source: " + ", ".join(
            "%s=%d" % (k, v) for k, v in log["price_source"].value_counts().items()))
    subsets = [("ALL settled favorites", st),
               ("favorites priced <=72% (where the tool claims validity)",
                st[st["pinn_fav_p"] <= 0.72])]
    if "price_source" in st.columns and (st["price_source"] == "pinnacle").any():
        subsets.append(("pinnacle-priced rows only (clean validation subset)",
                        st[st["price_source"] == "pinnacle"]))
    for name, sub in subsets:
        print("\n-- %s (n=%d) --" % (name, len(sub)))
        print("   bucket   n   mean_fav_p  fav_won%  fav_lost%  implied_lost%  excess (SE)")
        for lo, hi in BUCKETS:
            b = sub[(sub["fragility_score"] >= lo) & (sub["fragility_score"] <= hi)]
            if len(b) < 10:
                print("   %2d-%-3d  n=%d -- too few" % (lo, hi, len(b))); continue
            lost = b["fav_lost"].mean(); imp = 1 - b["pinn_fav_p"].mean()
            se = math.sqrt(max(lost * (1 - lost), 1e-9) / len(b))
            print("   %2d-%-3d %5d   %5.1f%%     %5.1f     %5.1f        %5.1f       %+5.1f (%.1f)"
                  % (lo, hi, len(b), 100 * b["pinn_fav_p"].mean(), 100 * (1 - lost),
                     100 * lost, 100 * imp, 100 * (lost - imp), 100 * se))
        if len(sub) >= 300:
            X = np.column_stack([np.ones(len(sub)),
                                 [logit(p) for p in sub["pinn_fav_p"]],
                                 sub["fragility_score"].values / 100.0])
            # closed-form IRLS (no fitting of the score -- this tests it)
            beta = np.zeros(3)
            for _ in range(60):
                eta = np.clip(X @ beta, -30, 30); mu = 1 / (1 + np.exp(-eta))
                w = np.maximum(mu * (1 - mu), 1e-9)
                H = (X * w[:, None]).T @ X + 1e-3 * np.eye(3)
                beta = beta + np.linalg.solve(H, X.T @ (sub["fav_lost"].values - mu) - 1e-3 * beta)
            se = math.sqrt(np.linalg.inv(H)[2, 2])
            print("   pooled trend z=%+.2f  (validation test, not a re-fit)" % (beta[2] / se))
        else:
            print("   trend test deferred: %d settled < 300 -- too early for "
                  "significance claims" % len(sub))


# --------------------------------------------------------------- selftest
def selftest():
    print(MARKER, "-- SELFTEST")
    import tempfile
    tmp = tempfile.mkdtemp()
    os.chdir(tmp)
    model = {"marker": EXPECTED_MODEL_MARKER,
             "components": ["elo_gap", "d_surface_switch", "d_age"],
             "beta": {"elo_gap": 0.026, "d_surface_switch": 0.009, "d_age": 0.034},
             "mu": {"elo_gap": 0.0, "d_surface_switch": 0.0, "d_age": 0.0},
             "sd": {"elo_gap": 0.45, "d_surface_switch": 0.4, "d_age": 4.5},
             "med": {"elo_gap": 0.0, "d_surface_switch": 0.0, "d_age": 0.0}}
    json.dump(model, open(MODEL_JSON, "w"))
    rng = np.random.default_rng(5)
    n = 20000
    p = 0.5 + 0.45 * rng.random(n)
    rows = pd.DataFrame({"pinn_a": p, "label": 1, "player_a": "A", "player_b": "B",
                         "year": rng.integers(2015, 2024, n),
                         "date": "2020-01-01",
                         "d_elo": (np.array([logit(x) for x in p]) +
                                   rng.normal(0, 0.45, n)) / LN10_400,
                         "d_surface_switch": rng.normal(0, 0.4, n),
                         "d_age": rng.normal(0, 4.5, n)})
    rows.to_csv("apex_batch_a_rows.csv", index=False)
    freeze("apex_batch_a_rows.csv", "pinn_a")
    try:
        freeze("apex_batch_a_rows.csv", "pinn_a"); raise RuntimeError("double freeze allowed")
    except AssertionError:
        print("double-freeze correctly refused")
    spec()
    model2, sha = load_model(); ref = load_ref()
    hot = compute(model2, ref, 0.62, d_elo=260, d_age=8.0, d_switch=1.0)
    assert hot["score"] > 80, "stacked risk factors should score high, got %s" % hot["score"]
    assert len(hot["reasons"]) >= 2 and "No strong" not in hot["reasons"][0], \
        "high score must list actual contributors"
    assert hot["tier"] in (TIERS[2][1], TIERS[3][1])
    cold = compute(model2, ref, 0.62, d_elo=40, d_age=-6.0, d_switch=-1.0)
    assert cold["score"] < 25 and cold["tier"] == TIERS[0][1]
    card = render_card("F", "D", 0.80, compute(model2, ref, 0.80, 200, 5, 1))
    assert "72%" in card, "high-price note missing"
    # log + settle + report round trip
    class A: pass
    a = A(); a.players = ["Fav Man", "Dog Man"]; a.pinn = 0.63; a.surface = "Hard"
    a.d_elo, a.d_age, a.d_switch = 220.0, 7.0, 1.0
    a.tour = "atp"; a.tournament = "TestOpen"; a.round = "R32"; a.best_of = 3
    a.date = "2026-08-21"
    score_match(a)
    score_match(a)                    # duplicate: must NOT create a second row
    log = pd.read_csv(LOG_CSV)
    assert len(log) == 1, "duplicate guard failed: %d rows" % len(log)
    print("duplicate-write guard OK (second score refused)")
    set_result(log.iloc[0]["match_id"], "FAV_LOST")
    for i in range(40):  # pad settled rows so report renders buckets
        r = dict(pd.read_csv(LOG_CSV).iloc[0])
        r["match_id"] = "pad%03d" % i
        r["fragility_score"] = float(rng.random() * 100)
        r["pinn_fav_p"] = 0.62
        r["result"] = "FAV_WON" if rng.random() < 0.65 else "FAV_LOST"
        append_log(r)
    report()
    # freeze-violation detection
    model["beta"]["d_age"] = 0.99
    json.dump(model, open(MODEL_JSON, "w"))
    try:
        score_match(a); raise RuntimeError("tampered model accepted")
    except (AssertionError, SystemExit):
        print("tampered model correctly refused (freeze violation caught)")
    print("SELFTEST PASS -- freeze once, spec + worked example, high scores always "
          "explained, tiers + high-price note correct, log/settle/report round trip, "
          "tampering detected.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--spec", action="store_true")
    ap.add_argument("--score", nargs=2, dest="players", metavar=("FAV", "DOG"))
    ap.add_argument("--pinn", type=float)
    ap.add_argument("--surface", default="Hard")
    ap.add_argument("--tour", default="atp")
    ap.add_argument("--tournament", default="")
    ap.add_argument("--round", default="")
    ap.add_argument("--best-of", type=int, default=3)
    ap.add_argument("--date", default=None)
    ap.add_argument("--d-elo", type=float, default=None)
    ap.add_argument("--d-age", type=float, default=None)
    ap.add_argument("--d-switch", type=float, default=None)
    ap.add_argument("--settle-sackmann", action="store_true")
    ap.add_argument("--sackmann-dir", default="ATP & WTA, 1968-2026")
    ap.add_argument("--result", nargs=2, metavar=("MATCH_ID", "FAV_WON|FAV_LOST"))
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--rows", default="apex_batch_a_rows.csv")
    ap.add_argument("--prob-col", default="pinn_a")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.freeze:
        freeze(a.rows, a.prob_col)
    elif a.spec:
        spec()
    elif a.players:
        assert a.pinn is not None, "--pinn required with --score"
        score_match(a)
    elif a.settle_sackmann:
        settle_sackmann(a.sackmann_dir)
    elif a.result:
        set_result(a.result[0], a.result[1])
    elif a.report:
        report()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
