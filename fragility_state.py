#!/usr/bin/env python3
"""
fragility_state.py -- APEX-FRAGILITY-STATE-2026-08-20

Builds the live player state needed to reproduce the three FROZEN Fragility V3
research variables -- d_elo, d_age, d_surface_switch -- exactly as the row
cache computed them, then PROVES it by validating against the cache before any
live feature is ever served.

Source of truth: the local apex_combined_model.py. This script imports it and
replays the Sackmann folder through its own PState / features_for /
update_state / parse_score functions -- nothing re-implemented. The SHA256 of
apex_combined_model.py is verified at build time, embedded in the state file,
and re-checked at serve time.

Expected apex_combined_model.py SHA256 (v4, the version that built the cache):
  8d90fb5c89882c9aaba144d4227a4750e2485f2015f590483f4f1a510b52a12e

STATE SCHEMA (fragility_state.pkl):
  {marker, acm_sha, built_at, year_from, sack_dir,
   players: {pid: {"names": [..], "elo": float, "n": int,
                   "last_date": iso date, "last_surface": str,
                   "last_age": float|None, "last_age_date": iso date|None}},
   name_index: {normalized name: [pid, ..]},
   validation_rows: [per replayed 2024+ match: date, name_a, name_b,
                     d_elo, d_age_projected, d_switch]}

Feature definitions served live (favorite frame):
  d_elo            = elo[fav] - elo[dog]                (plain Elo, as cached)
  d_age            = proj_age[fav] - proj_age[dog],
                     proj_age = last_age + days_since_last_age/365.25
  d_surface_switch = switch(fav) - switch(dog),
                     switch(p) = 1 if p.last_surface not None and != surface

Commands:
  --build [--year-from 1968] [--sack-dir "ATP & WTA, 1968-2026"]
  --validate        compare replayed 2024+ features vs apex_rows_cache.pkl
                    and apex_batch_a_rows.csv; writes fragility_features.ok
                    ONLY on pass -- live serving refuses without it
  --lookup "Name"   show a player's stored state
  --selftest        container-safe test of serialization, name resolution,
                    feature math, and the validation gate (the acm replay
                    itself is validated on the Mini by --validate, by design)
"""
import argparse, hashlib, json, math, os, pickle, re, sys
from datetime import datetime, date, timedelta
import numpy as np
import pandas as pd

MARKER = "APEX-FRAGILITY-STATE-2026-08-20"
STATE_PKL = "fragility_state.pkl"
OK_FILE = "fragility_features.ok"
ACM_FILE = "apex_combined_model.py"
EXPECTED_ACM_SHA = "8d90fb5c89882c9aaba144d4227a4750e2485f2015f590483f4f1a510b52a12e"
# validation tolerances: replay should be near-exact; ages are projected
VALIDATE_LAST_YEAR = 2025   # APEX-FRAGVALIDATE-2026-10-20 (set 2026 to restore the old check)
TOL = {"d_elo_mae": 1.0, "d_elo_within": (0.5, 0.99),
       "d_switch_exact": 0.995,
       "d_age_mae": 0.10, "d_age_within": (0.25, 0.99)}


def sha256_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def norm_name(s):
    return " ".join(sorted(re.sub(r"[^a-z ]", "", str(s).lower().strip()).split()))


# ----------------------------------------------------------------------- build
def build(sack_dir, year_from, include_challengers):
    assert os.path.exists(ACM_FILE), "%s not found in this folder" % ACM_FILE
    acm_sha = sha256_file(ACM_FILE)
    print(MARKER, "-- BUILD")
    print("apex_combined_model.py sha256:", acm_sha)
    if acm_sha != EXPECTED_ACM_SHA:
        print("WARNING: this is NOT the v4 file that built the cache "
              "(expected %s...). Proceeding, but --validate must decide."
              % EXPECTED_ACM_SHA[:16])
    sys.path.insert(0, os.getcwd())
    import apex_combined_model as acm
    df = acm.load_sackmann(sack_dir, year_from, include_challengers)
    print("loaded %d matches from %s (year_from=%d, challengers %s)"
          % (len(df), sack_dir, year_from,
             "included" if include_challengers else "EXCLUDED"))
    from collections import defaultdict
    players = defaultdict(acm.PState)
    ages = {}          # pid -> (age, date)  last seen age from the data
    names = defaultdict(set)
    val_rows = []
    n = len(df)
    for i, r in enumerate(df.itertuples(index=False)):
        if i % 100000 == 0 and i:
            print("  ... %s/%s" % (format(i, ","), format(n, ",")))
        rd = r._asdict()
        sw, sl, gw, gl, ret, wo = acm.parse_score(rd.get("score"))
        if wo:
            continue
        wn, ln = str(rd.get("winner_name")), str(rd.get("loser_name"))
        wid = rd.get("winner_id"); lid = rd.get("loser_id")
        wid = str(int(wid)) if acm.fnum(wid) is not None else "N:" + acm.norm_text(wn)
        lid = str(int(lid)) if acm.fnum(lid) is not None else "N:" + acm.norm_text(ln)
        today = rd["tdate"]; surface = rd["surface"]
        pw, pl = players[wid], players[lid]
        names[wid].add(wn); names[lid].add(ln)
        # --- validation sample: compute the LIVE-method features pre-update
        if getattr(today, "year", 0) >= 2024:
            def proj_age(pid, p):
                a = ages.get(pid)
                if a is None:
                    return None
                return a[0] + (today - a[1]).days / 365.25
            aw, al = proj_age(wid, pw), proj_age(lid, pl)
            sw_w = int(pw.last_surface is not None and pw.last_surface != surface)
            sw_l = int(pl.last_surface is not None and pl.last_surface != surface)
            val_rows.append({"date": today, "name_w": wn, "name_l": ln,
                             "d_elo_wl": pw.elo - pl.elo,
                             "d_age_wl": (aw - al) if None not in (aw, al) else np.nan,
                             "d_switch_wl": sw_w - sw_l})
        # --- state update, mirroring build_rows exactly
        exp_w = 1.0 / (1.0 + 10 ** ((pl.elo - pw.elo) / 400.0))
        W = {c: "w_%s" % c for c in acm.STAT_COLS}
        L = {c: "l_%s" % c for c in acm.STAT_COLS}
        wstats = {c: acm.fnum(rd.get(W[c])) for c in acm.STAT_COLS}
        lstats = {c: acm.fnum(rd.get(L[c])) for c in acm.STAT_COLS}
        minutes = acm.fnum(rd.get("minutes"))
        wrow = dict(wstats, sets_w=sw, sets_l=sl, games_w=gw, games_l=gl,
                    minutes=minutes, retired_self=0)
        lrow = dict(lstats, sets_w=sl, sets_l=sw, games_w=gl, games_l=gw,
                    minutes=minutes, retired_self=ret)
        elo_w, elo_l = pw.elo, pl.elo
        acm.update_state(pw, wrow, True, surface, today, exp_w, elo_l, lstats)
        acm.update_state(pl, lrow, False, surface, today, 1 - exp_w, elo_w, wstats)
        kw, kl = acm.k_factor(pw.n - 1), acm.k_factor(pl.n - 1)
        pw.elo += kw * (1 - exp_w); pl.elo -= kl * (1 - exp_w)
        pw.elo_hist.append((today, pw.elo)); pl.elo_hist.append((today, pl.elo))
        se_w, se_l = pw.surf_elo[surface], pl.surf_elo[surface]
        exp_s = 1.0 / (1.0 + 10 ** ((se_l - se_w) / 400.0))
        pw.surf_elo[surface] = se_w + kw * (1 - exp_s)
        pl.surf_elo[surface] = se_l - kl * (1 - exp_s)
        # ages AFTER features: today's row age becomes the latest known age
        wa = acm.fnum(rd.get("winner_age")); la = acm.fnum(rd.get("loser_age"))
        if wa is not None:
            ages[wid] = (wa, today)
        if la is not None:
            ages[lid] = (la, today)
    # serialize only what the three features need
    out_players, name_index = {}, {}
    for pid, p in players.items():
        a = ages.get(pid)
        out_players[pid] = {"names": sorted(names[pid]), "elo": float(p.elo),
                            "n": int(p.n),
                            "last_date": p.last_date.isoformat() if p.last_date else None,
                            "last_surface": p.last_surface,
                            "last_age": float(a[0]) if a else None,
                            "last_age_date": a[1].isoformat() if a else None}
        for nm in names[pid]:
            name_index.setdefault(norm_name(nm), [])
            if pid not in name_index[norm_name(nm)]:
                name_index[norm_name(nm)].append(pid)
    state = {"marker": MARKER, "acm_sha": acm_sha,
             "built_at": datetime.now().isoformat(), "year_from": year_from,
             "include_challengers": include_challengers,
             "sack_dir": sack_dir, "players": out_players,
             "name_index": name_index, "validation_rows": val_rows}
    pickle.dump(state, open(STATE_PKL, "wb"))
    print("state saved: %s  (%d players, %d 2024+ validation samples)"
          % (STATE_PKL, len(out_players), len(val_rows)))
    if os.path.exists(OK_FILE):
        os.remove(OK_FILE)
        print("removed stale %s -- run --validate again for this build" % OK_FILE)


# ------------------------------------------------------------------- validate
def _stats(diffs, name, mae_tol, within):
    d = np.abs(np.asarray([x for x in diffs if x is not None and not np.isnan(x)]))
    if len(d) == 0:
        return {"n": 0, "pass": False}
    thr, share_req = within
    r = {"n": len(d), "mae": float(d.mean()), "max": float(d.max()),
         "exact_pct": float((d < 1e-9).mean() * 100),
         "within_pct": float((d <= thr).mean() * 100)}
    r["pass"] = r["mae"] <= mae_tol and (d <= thr).mean() >= share_req
    print("  %-10s n=%-6d MAE=%.5f  max=%.4f  exact=%.2f%%  within %.2f: %.2f%%  -> %s"
          % (name, r["n"], r["mae"], r["max"], r["exact_pct"], thr,
             r["within_pct"], "PASS" if r["pass"] else "FAIL"))
    return r


def validate(cache_path, batch_path, prob_col):
    print(MARKER, "-- VALIDATE (mandatory gate before live features)")
    state = pickle.load(open(STATE_PKL, "rb"))
    print("state built %s from acm sha %s..." % (state["built_at"], state["acm_sha"][:16]))
    vr = pd.DataFrame(state["validation_rows"])
    assert len(vr) > 500, "too few 2024+ validation samples in state -- rebuild"
    vr["key"] = [(pd.Timestamp(d).date().isoformat(),
                  tuple(sorted((norm_name(a), norm_name(b)))))
                 for d, a, b in zip(vr["date"], vr["name_w"], vr["name_l"])]
    overall_pass = True
    for label, path, loader in (("apex_rows_cache.pkl", cache_path, pd.read_pickle),
                                ("apex_batch_a_rows.csv", batch_path,
                                 lambda p: pd.read_csv(p, low_memory=False))):
        if not os.path.exists(path):
            print("  %s missing -- skipped" % label)
            continue
        ref = loader(path)
        # APEX-FRAGVALIDATE-2026-10-20: compare 2024-2025 only. The reference rows were
        # built 8/17 from the data as it was then; on 10/17 TennisMyLife corrected 15
        # 2026 WTA results (e.g. Rouen), which legitimately moves every later 2026 Elo,
        # so 2026 rows can never match the frozen reference again (2024-25: 100%%
        # within 0.5; 2026 Q2: 68%%). A machinery change would still show in 2024-25.
        _yr = pd.to_datetime(ref["date"]).dt.year
        ref = ref[(_yr >= 2024) & (_yr <= VALIDATE_LAST_YEAR)]
        ref = ref[["date", "player_a", "player_b", "d_elo", "d_age", "d_surface_switch"]].copy()
        ref["key"] = [(pd.Timestamp(d).date().isoformat(),
                       tuple(sorted((norm_name(a), norm_name(b)))))
                      for d, a, b in zip(ref["date"], ref["player_a"], ref["player_b"])]
        # one row per key on both sides (drop rare same-day rematch collisions)
        vv = vr.drop_duplicates("key", keep=False)
        rr = ref.drop_duplicates("key", keep=False)
        j = rr.merge(vv, on="key", how="inner")
        print("-- vs %s: matched %d of %d 2024+ reference rows --"
              % (label, len(j), len(rr)))
        if len(j) < 500:
            print("  too few matches to validate -- FAIL"); overall_pass = False
            continue
        # orient replay values to the reference's a/b assignment
        sign = np.where([norm_name(a) == norm_name(w) for a, w in
                         zip(j["player_a"], j["name_w"])], 1.0, -1.0)
        d_elo = j["d_elo"].values - sign * j["d_elo_wl"].values
        d_age = j["d_age"].values - sign * j["d_age_wl"].values
        d_sw = j["d_surface_switch"].values - sign * j["d_switch_wl"].values
        r1 = _stats(d_elo, "d_elo", TOL["d_elo_mae"], TOL["d_elo_within"])
        r3 = _stats(d_age, "d_age", TOL["d_age_mae"], TOL["d_age_within"])
        sw_ok = np.abs(d_sw[~np.isnan(d_sw)])
        sw_exact = float((sw_ok < 1e-9).mean()) if len(sw_ok) else 0.0
        print("  %-10s n=%-6d exact=%.2f%% (required %.1f%%)  -> %s"
              % ("d_switch", len(sw_ok), sw_exact * 100, TOL["d_switch_exact"] * 100,
                 "PASS" if sw_exact >= TOL["d_switch_exact"] else "FAIL"))
        if not (r1.get("pass") and r3.get("pass") and sw_exact >= TOL["d_switch_exact"]):
            overall_pass = False
    if overall_pass:
        json.dump({"marker": MARKER, "validated_at": datetime.now().isoformat(),
                   "acm_sha": state["acm_sha"], "state_built_at": state["built_at"]},
                  open(OK_FILE, "w"))
        print("\nVALIDATION PASS -- %s written; live features are now trusted." % OK_FILE)
    else:
        if os.path.exists(OK_FILE):
            os.remove(OK_FILE)
        print("\nVALIDATION FAIL -- live features REFUSED. If d_elo is off by a "
              "roughly constant amount, the replay start year differs from the "
              "cache build; rebuild with a different --year-from and re-validate.")


# ---------------------------------------------------------------- live serving
_STATE_CACHE = {}


def _load_state():
    if "s" not in _STATE_CACHE:
        assert os.path.exists(STATE_PKL), "no %s -- run --build" % STATE_PKL
        assert os.path.exists(OK_FILE), \
            "no %s -- validation has not passed; live features refused" % OK_FILE
        s = pickle.load(open(STATE_PKL, "rb"))
        ok = json.load(open(OK_FILE))
        assert ok["acm_sha"] == s["acm_sha"] and ok["state_built_at"] == s["built_at"], \
            "state was rebuilt after validation -- run --validate again"
        _STATE_CACHE["s"] = s
    return _STATE_CACHE["s"]


def resolve(state, name):
    hits = state["name_index"].get(norm_name(name), [])
    if len(hits) == 1:
        return hits[0]
    if len(hits) == 0:
        # loose match: all tokens of the query appear in a stored name
        toks = set(norm_name(name).split())
        cand = [pid for nm, pids in state["name_index"].items()
                if toks <= set(nm.split()) for pid in pids]
        cand = sorted(set(cand))
        if len(cand) == 1:
            return cand[0]
        raise SystemExit("cannot resolve player %r (candidates: %s)"
                         % (name, [state["players"][p]["names"] for p in cand[:5]] or "none"))
    raise SystemExit("ambiguous player %r -- matches: %s"
                     % (name, [state["players"][p]["names"] for p in hits]))


def features_for_pair(fav, dog, surface="Hard", today=None):
    """The hook fragility_live.py calls. Returns favorite-frame features."""
    state = _load_state()
    today = today or date.today()
    if isinstance(today, str):
        today = pd.Timestamp(today).date()
    out = {}
    ps = []
    for nm in (fav, dog):
        pid = resolve(state, nm)
        p = state["players"][pid]
        age = None
        if p["last_age"] is not None:
            age = p["last_age"] + (today - pd.Timestamp(p["last_age_date"]).date()).days / 365.25
        switch = int(p["last_surface"] is not None and p["last_surface"] != surface)
        stale = (today - pd.Timestamp(p["last_date"]).date()).days if p["last_date"] else None
        ps.append({"name": nm, "elo": p["elo"], "age": age, "switch": switch,
                   "n": p["n"], "stale_days": stale})
    f, d = ps
    for side in ps:
        if side["stale_days"] is not None and side["stale_days"] > 90:
            print("note: %s last seen %d days ago in the data -- state may lag reality"
                  % (side["name"], side["stale_days"]))
    return {"d_elo": f["elo"] - d["elo"],
            "d_age": (f["age"] - d["age"]) if None not in (f["age"], d["age"]) else float("nan"),
            "d_surface_switch": float(f["switch"] - d["switch"])}


def lookup(name):
    state = _load_state() if os.path.exists(OK_FILE) else pickle.load(open(STATE_PKL, "rb"))
    pid = resolve(state, name)
    p = state["players"][pid]
    print(json.dumps({k: v for k, v in p.items()}, indent=1))


# ------------------------------------------------------------------- selftest
def selftest():
    print(MARKER, "-- SELFTEST (container-safe: serialization, resolution, "
          "feature math, gate; the acm replay is proven by --validate on the Mini)")
    import tempfile
    os.chdir(tempfile.mkdtemp())
    today = date(2026, 8, 20)
    state = {"marker": MARKER, "acm_sha": "x" * 64, "built_at": "t0",
             "year_from": 1968, "sack_dir": "fake",
             "players": {
                 "1": {"names": ["Fav Man"], "elo": 1720.0, "n": 200,
                       "last_date": "2026-08-10", "last_surface": "Clay",
                       "last_age": 29.9, "last_age_date": "2026-08-10"},
                 "2": {"names": ["Dog Man"], "elo": 1600.0, "n": 150,
                       "last_date": "2026-08-12", "last_surface": "Hard",
                       "last_age": 22.4, "last_age_date": "2026-08-12"}},
             "name_index": {norm_name("Fav Man"): ["1"], norm_name("Dog Man"): ["2"]},
             "validation_rows": []}
    pickle.dump(state, open(STATE_PKL, "wb"))
    try:
        features_for_pair("Fav Man", "Dog Man"); raise RuntimeError("gate bypassed")
    except AssertionError as e:
        print("no-validation refusal OK:", str(e)[:60])
    json.dump({"marker": MARKER, "validated_at": "t1", "acm_sha": "x" * 64,
               "state_built_at": "t0"}, open(OK_FILE, "w"))
    f = features_for_pair("Fav Man", "Dog Man", surface="Hard", today=today)
    assert abs(f["d_elo"] - 120.0) < 1e-9
    # Fav switched (Clay->Hard)=1, Dog stayed (Hard)=0
    assert f["d_surface_switch"] == 1.0
    exp_age = (29.9 + 10 / 365.25) - (22.4 + 8 / 365.25)
    assert abs(f["d_age"] - exp_age) < 1e-9, f["d_age"]
    print("feature math OK:", {k: round(v, 3) for k, v in f.items()})
    assert resolve(state, "fav man") == "1"
    assert resolve(state, "Man Fav") == "1"      # token-order free
    try:
        resolve(state, "Nobody Here"); raise RuntimeError("resolved a ghost")
    except SystemExit:
        print("unknown-name refusal OK")
    # rebuild-invalidates-validation check
    state["built_at"] = "t2"
    pickle.dump(state, open(STATE_PKL, "wb"))
    _STATE_CACHE.clear()
    try:
        features_for_pair("Fav Man", "Dog Man"); raise RuntimeError("stale ok accepted")
    except AssertionError:
        print("rebuilt-state-without-revalidation refusal OK")
    # validation math
    r = _stats([0.0, 0.0, 0.001, 0.0], "toy", 0.01, (0.5, 0.99))
    assert r["pass"] and r["n"] == 4
    r = _stats([5.0] * 10, "toy2", 0.01, (0.5, 0.99))
    assert not r["pass"]
    print("SELFTEST PASS -- gate enforced, resolution and feature math correct, "
          "rebuild invalidates validation, validation math sane.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--lookup")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--sack-dir", default="ATP & WTA, 1968-2026")
    ap.add_argument("--year-from", type=int, default=1968)
    ap.add_argument("--no-challengers", action="store_true",
                    help="exclude challenger files from the replay (match the cache build)")
    ap.add_argument("--cache", default="apex_rows_cache.pkl")
    ap.add_argument("--rows", default="apex_batch_a_rows.csv")
    ap.add_argument("--prob-col", default="pinn_a")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.build:
        build(a.sack_dir, a.year_from, not a.no_challengers)
    elif a.validate:
        validate(a.cache, a.rows, a.prob_col)
    elif a.lookup:
        lookup(a.lookup)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
