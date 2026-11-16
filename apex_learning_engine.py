#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
APEX LEARNING INTELLIGENCE SYSTEM (ALIS) — Phase 1
Marker: APEX-ALIS-2026-08-29-v2 (result derivation + note-as-void_reason + forward-log discovery)

READ-ONLY learning layer. Sits beside apex_edge_tracker.py.

HARD RULES (user-approved 2026-08-28):
  * Reads the three logs directly. NEVER writes to them.
  * NEVER imports or modifies core prediction logic, frozen models,
    simulator logic, tiers, or outputs. (Guarded messaging-only use of
    core.send_discord for --post; fail-open to file if unavailable.)
  * Outputs ONLY: findings, recommendations, documentation, reports.
  * Retired-match rows: result VOID with REQUIRED void_reason. A VOID
    without a reason is a hard refusal (exit 2) — nothing is computed.
  * Append-only registries: lessons_learned.md, major_misses.md,
    edge_history.md, recommendations/. Nothing is ever deleted.

CLI:
  --inspect-logs        show headers found + column mapping chosen (run FIRST on Mini)
  --cycle [--post]      run a learning cycle (post = 🧠 Discord summary)
  --rec ID accepted|rejected "reason"
  --selftest            synthetic end-to-end verification
"""

import csv, json, os, re, sys, hashlib, shutil, tempfile
from datetime import datetime, date, timedelta

# ---------------------------------------------------------------- paths
# APEX-ALIS-BASEFIX-2026-08-31: the old hardcoded "~/openclaw" default
# broke when the project moved to the Desktop -- every log path resolved to
# a missing file and ALIS silently reported nothing to learn from. Falling
# back to this script's own directory keeps it correct across moves.
BASE = os.path.expanduser(os.environ["APEX_HOME"]) if os.environ.get("APEX_HOME") \
       else os.path.dirname(os.path.abspath(__file__))
ALIS = os.path.join(BASE, "apex_learning_system")
LOGS = {
    "picks":   os.path.join(BASE, "apex_engine_picks_log_v1.csv"),
    "forward": os.path.join(BASE, "apex_prop_forward_log_v1.csv"),
    "hand":    os.path.join(BASE, "apex_engine_hand_settled_v1.csv"),
}
SUBDIRS = ["reports", "findings", "category_intelligence", "change_log",
           "weekly_reviews", "monthly_reviews", "discord_summaries",
           "recommendations", "archived_versions", "dashboards", "documentation"]

# ------------------------------------------------------------- constants
ZONE = {"aces", "breaks", "double_faults", "dfs"}
BE_FLEX, BE_POWER = 0.542, 0.58
GROUPS = {
    "aces": "rate", "breaks": "rate", "double_faults": "rate", "dfs": "rate",
    "games_won": "sim", "total_games_won": "sim", "fantasy_score": "sim",
    "sets_won": "sim", "total_sets": "sim", "tiebreaks": "sim",
    "total_tie_breaks": "sim", "any_tiebreak": "sim",
    "first_set_winner": "sim", "first_set_fav_wins": "sim",
    "first_set_games": "sim", "goes_3_sets": "sim", "goes_5_sets": "sim",
    "total_games": "tg",
}
GROUP_NAMES = {"rate": "Direct Rate Model", "sim": "Simulator Derived", "tg": "Total Games"}
BINARY = {"first_set_winner", "first_set_fav_wins", "any_tiebreak",
          "goes_3_sets", "goes_5_sets"}
CANON = {"dfs": "double_faults", "total_games_won": "games_won",
         "total_tie_breaks": "tiebreaks", "first_set_fav_wins": "first_set_winner"}

# column synonym map — resolved per file; missing REQUIRED -> loud refusal
SYN = {
    "date":      ["match_date", "date", "ts", "timestamp"],
    "player":    ["player", "player_name", "name"],
    "opponent":  ["opponent", "opp", "opponent_name"],
    "surface":   ["surface"],
    "tournament":["tournament", "event"],          # optional (known log gap)
    "category":  ["category", "stat", "stat_type"],
    "line":      ["line"],
    "direction": ["direction", "dir", "side", "pick"],
    "prob":      ["prob", "probability", "p", "stated_prob"],
    "projection":["projection", "proj", "mean"],
    "actual":    ["actual", "actual_result", "result_value"],
    "result":    ["result", "outcome", "wl", "status"],
    "edge":      ["edge"],
    "fragility": ["fragility", "fragility_score"],
    "tier":      ["tier"],
    "best_of":   ["best_of", "bo", "format"],
    "void_reason": ["void_reason", "reason", "note"],
}
REQUIRED = ["player", "category", "line", "prob"]  # result OR (actual+direction) checked per-log
BO5_CUTOFF = "2026-08-29"   # BO5-FWD-001: hand rows on/after this date must carry best_of (3 or 5)

def parse_best_of(raw, row_date, label, player, category, legacy_counts):
    v = str(raw or "").strip()
    if v.endswith(".0"):
        v = v[:-2]
    if v == "3":
        return 3
    if v == "5":
        return 5
    if v == "":
        if label == "hand" and str(row_date)[:10] >= BO5_CUTOFF:
            raise RefuseError(
                "missing best_of on hand row dated %s (player=%s, category=%s); "
                "rows on/after %s require best_of 3 or 5. Nothing computed."
                % (row_date, player, category, BO5_CUTOFF))
        legacy_counts[label] = legacy_counts.get(label, 0) + 1
        return 3
    raise RefuseError(
        "invalid best_of %r in %s log (player=%s, category=%s); allowed values: 3 or 5. Nothing computed."
        % (v, label, player, category))

# ---------------------------------------------------------------- helpers
def now_iso(): return datetime.now().strftime("%Y-%m-%d %H:%M")
def today():   return date.today().isoformat()

def sha8(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:8]

def canon_cat(c):
    c = (c or "").strip().lower().replace(" ", "_")
    return CANON.get(c, c)

def derive_result(actual, line, direction, note):
    """No result column: derive from actual vs line+direction.
    Empty actual + retirement/void note => VOID; empty otherwise => unsettled."""
    if actual is None:
        if note and re.search(r"retir|void|walkover|w/?o\b", note, re.I):
            return "VOID"
        return None
    if line is None or direction not in ("MORE", "LESS"):
        return None
    if actual == line:
        return "PUSH"
    return "WIN" if ((direction == "MORE" and actual > line) or
                     (direction == "LESS" and actual < line)) else "LOSS"

def discover_forward_log():
    import glob
    cands = [LOGS["forward"]] + sorted(glob.glob(os.path.join(BASE, "*forward*log*.csv")))
    for c in cands:
        if os.path.exists(c):
            return c
    return LOGS["forward"]

def resolve_columns(header):
    found, low = {}, [h.strip().lower() for h in header]
    for key, names in SYN.items():
        for n in names:
            if n in low:
                found[key] = header[low.index(n)]
                break
    return found

def fnum(v, default=None):
    try:
        s = str(v).strip().replace("%", "")
        if s == "": return default
        x = float(s)
        return x / 100.0 if x > 1.5 and 0 else x
    except Exception:
        return default

def norm_prob(v):
    x = fnum(v)
    if x is None: return None
    return x / 100.0 if x > 1.0 else x

def attention(cat, prob, bo5=False):
    """Recomputed, not stored: deterministic from category + probability.
    BO5-FWD-001: every Bo5 pick is LOW until milestone review #1 (25 settled)."""
    if bo5:
        return "LOW"
    if canon_cat(cat) not in {canon_cat(z) for z in ZONE}:
        return "LOW"
    if prob is None: return "LOW"
    if prob >= BE_POWER: return "HIGH"
    if prob > BE_FLEX:  return "MEDIUM"
    return "LOW"

def miss_class(row):
    """Loss severity. Numeric cats: |projection-actual|. Binary/no-projection:
    by stated probability of the losing pick."""
    cat, proj, act, prob = row["category"], row.get("projection"), row.get("actual_n"), row.get("prob")
    if cat in BINARY or proj is None or act is None:
        p = prob or 0.5
        if p < 0.60: return "Near Miss"
        if p < 0.70: return "Moderate Miss"
        if p < 0.80: return "Large Miss"
        return "Extreme Miss"
    err = abs(proj - act)
    if err <= 1.0: return "Near Miss"
    if err <= 3.0: return "Moderate Miss"
    if err <= 6.0: return "Large Miss"
    return "Extreme Miss"

# ---------------------------------------------------------------- loading
class RefuseError(Exception): pass

def load_log(path, label):
    if not os.path.exists(path):
        return [], {}
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        rdr = csv.reader(f)
        try: header = next(rdr)
        except StopIteration: return [], {}
        cols = resolve_columns(header)
        rows = []
        for raw in rdr:
            if not raw or all(not c.strip() for c in raw): continue
            d = dict(zip(header, raw))
            rows.append(d)
    return rows, cols

PROJECTION_JOIN_FILE = os.path.join(BASE, "apex_projection_join_v1.csv")

def _load_projection_join():
    """APEX-PROJ-JOIN-WIRE-2026-09-26: reads the read-only derived artifact
    from Task 8 (apex_projection_join.py). Never writes anything. Keyed
    identically to that script's split_key(): player-level rows key on
    (date, player, category, line); match-level rows (player=="match")
    key on (date, opponent, category, line), since player=="match" is not
    a real identifier. Fail-open: if the file is missing, ALIS behaves
    exactly as before -- rows with no projection just show "projection
    unavailable", same as always."""
    lookup = {}
    if not os.path.exists(PROJECTION_JOIN_FILE):
        return lookup
    with open(PROJECTION_JOIN_FILE, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            is_match = (r.get("player") or "").strip().lower() == "match"
            ident = (r.get("opponent") or "").strip().lower() if is_match \
                else (r.get("player") or "").strip().lower()
            key = ((r.get("match_date") or "").strip()[:10],
                   "MATCH" if is_match else "PLAYER",
                   ident,
                   (r.get("category") or "").strip(),
                   str(r.get("line") or "").strip())
            lookup[key] = r
    return lookup


def build_ledger():
    """Rebuild the full settled ledger fresh from the logs. No second database."""
    ledger, missing_reports = [], []
    legacy_counts = {}
    proj_join = _load_projection_join()   # APEX-PROJ-JOIN-WIRE-2026-09-26
    proj_join_hits = 0
    for label in ("hand", "forward", "picks"):
        path = discover_forward_log() if label == "forward" else LOGS[label]
        rows, cols = load_log(path, label)
        if not rows: continue
        miss = [r for r in REQUIRED if r not in cols]
        can_settle = ("result" in cols) or ("actual" in cols and "direction" in cols)
        if miss or not can_settle:
            if not can_settle:
                miss = miss + ["result-or-(actual+direction)"]
            missing_reports.append((label, miss, cols))
            continue
        for d in rows:
            g = lambda k, dv="": d.get(cols.get(k, ""), dv)
            note_v = str(g("void_reason")).strip()
            if "result" in cols:
                res = str(g("result")).strip().upper()
            else:
                res = derive_result(fnum(g("actual")), fnum(g("line")),
                                    str(g("direction")).strip().upper(), note_v)
            if res not in {"WIN", "LOSS", "PUSH", "VOID"}: continue
            if res == "VOID":
                reason = note_v
                if not reason:
                    raise RefuseError(
                        f"VOID row without void_reason in {label} log "
                        f"(player={g('player')}, category={g('category')}). "
                        f"Protocol requires a reason. Nothing computed.")
            cat = canon_cat(g("category"))
            row = {
                "src": label, "date": str(g("date"))[:10],
                "player": g("player"), "opponent": g("opponent"),
                "surface": (g("surface") or "unknown").lower(),
                "tournament": g("tournament") or "unknown",
                "category": cat, "group": GROUPS.get(cat, "sim"),
                "line": fnum(g("line")), "direction": str(g("direction")).upper(),
                "prob": norm_prob(g("prob")),
                "projection": fnum(g("projection")),
                "actual_n": fnum(g("actual")),
                "edge": fnum(g("edge")), "fragility": fnum(g("fragility")),
                "tier": g("tier"), "result": res,
                "prop_type": (str(d.get("prop_type") or g("prop_type") or "").strip().lower() or "untyped"),   # APEX-LEARNCARD-2026-11-09: read the CSV column directly   # APEX-TYPEREC-2026-09-09
                "void_reason": str(g("void_reason")).strip(),
            }
            # APEX-PROJ-JOIN-WIRE-2026-09-26: hand-CSV rows carry no projection
            # column of their own (by design -- see Task 8). Fill from the
            # read-only join artifact when available; never overwrites a
            # projection a log already had from its own columns.
            if label == "hand" and row["projection"] is None:
                _is_match = g("player").strip().lower() == "match"
                _ident = g("opponent").strip().lower() if _is_match else g("player").strip().lower()
                _pj_key = (row["date"], "MATCH" if _is_match else "PLAYER",
                           _ident, g("category").strip(), str(g("line")).strip())
                _pj_hit = proj_join.get(_pj_key)
                if _pj_hit and _pj_hit.get("projection_mean"):
                    row["projection"] = fnum(_pj_hit["projection_mean"])
                    row["proj_source"] = "apex_projection_join_v1.csv"
                    proj_join_hits += 1
            row["best_of"] = parse_best_of(g("best_of"), row["date"], label,
                                           row["player"], row["category"], legacy_counts)
            row["bo5"] = (row["best_of"] == 5)
            row["attention"] = attention(row["category"], row["prob"], row["bo5"])
            row["proj_err"] = (None if row["projection"] is None or row["actual_n"] is None
                               else row["actual_n"] - row["projection"])
            ledger.append(row)
    for lb, n in sorted(legacy_counts.items()):
        print("note: %d legacy row(s) in %s log without best_of assumed Bo3 (pre-%s)" % (n, lb, BO5_CUTOFF))
    if proj_join:
        print("note: %d hand row(s) enriched with a projection from %s" % (
            proj_join_hits, os.path.basename(PROJECTION_JOIN_FILE)))
    # dedupe on (date, player, category, line): hand wins over forward over picks (BO5-FWD-001)
    seen, out = set(), []
    for r in ledger:
        key = (r["date"], r["player"].lower(), r["category"], r["line"])
        if key in seen: continue
        seen.add(key); out.append(r)
    out = _sellable_filter(out)   # APEX-LEARNCARD-2026-11-09
    return out, missing_reports


# ================================================================ APEX-LEARNCARD-2026-11-09
# owner, "Go fix learning cards": the learning ledger used to grade
# every side the engine had an opinion on, including sides PrizePicks never
# sells (LESS on a Goblin or Demon line). Those are not bets anyone can
# place, so they are left out of every count. Rows logged before the
# prop_type column existed (Aug 24 - Sep 9) are typed from props_db.
SELLABLE_ONLY = True
BOARD_OFF_CATS = {"breaks"}            # categories the boards keep off (apex_edge_tracker gate)
LAST_DROPPED = {"unsellable": 0, "unknown": 0, "kept": 0}
_PP_CAT = {"double_faults": "dfs", "first_set_games": "1st_set_total_games",
           "first_set_total_games": "1st_set_total_games"}
_PP_IDX = {}

def _pp_types_index():
    """(match_date, player lower, props_db category, line) -> set of odds types."""
    if _PP_IDX:
        return _PP_IDX
    try:
        import sqlite3 as _sq
        db = _sq.connect("file:%s?mode=ro" % os.path.join(BASE, "data", "props_db.sqlite"), uri=True)
        for p, o, c, ot, l, dte in db.execute("select player, opponent, category, odds_type, line_last, match_date from projections"):
            if l is None:
                continue
            for nm in (p, o):
                _PP_IDX.setdefault((dte, (nm or "").strip().lower(), c, float(l)), set()).add((ot or "standard").lower())
        db.close()
    except Exception:
        pass
    return _PP_IDX

def _sellable(row):
    """True when PrizePicks sells this side: any side of a Standard line,
    MORE only on Goblin/Demon lines. None when it cannot be told."""
    pt = (row.get("prop_type") or "untyped").lower()
    side = (row.get("direction") or "").upper()
    if pt == "standard":
        return True
    if pt in ("goblin", "demon", "red_demon"):
        return side == "MORE"
    idx = _pp_types_index()
    cat = _PP_CAT.get(row["category"], row["category"])
    pl = (row.get("player") or "").strip().lower()
    names = [pl] if pl != "match" else [x.strip().lower() for x in (row.get("opponent") or "").replace(" vs ", " v ").split(" v ")]
    types = set()
    for n in names:
        types |= idx.get((row["date"], n, cat, row["line"]), set())
    if not types:
        return None
    return ("standard" in types) or side == "MORE"

def _sellable_filter(rows):
    if not SELLABLE_ONLY:
        return rows
    out, drop_u, drop_q = [], 0, 0
    for r in rows:
        ok = _sellable(r)
        if ok:
            out.append(r)
        elif ok is None:
            drop_q += 1
        else:
            drop_u += 1
    LAST_DROPPED.update(unsellable=drop_u, unknown=drop_q, kept=len(out))
    return out

def _cat_rec(cat, b):
    """One plain line: hit X of N (Y%) · engine said Z%."""
    n = b["w"] + b["l"]
    pct = round(100.0 * b["w"] / n) if n else 0
    eng = ""
    if b.get("avg_prob") is not None:
        ap = b["avg_prob"]; ap = ap * 100 if ap <= 1 else ap
        eng = " · engine said %d%%" % round(ap)
    return "%s — hit %d of %d (%d%%)%s" % (_cat_label(cat), b["w"], n, pct, eng)

def _sellable_note():
    return "  (counts only lines PrizePicks sells on that side; %d unsellable sides left out)" % LAST_DROPPED.get("unsellable", 0)

# ---------------------------------------------------------------- analysis
def rec_str(w, l, p=0, v=0):
    s = f"{w}-{l}"
    if p: s += f"-{p}p"
    if v: s += f" ({v} void)"
    return s

def analyze(ledger):
    bo5_all = [r for r in ledger if r.get("bo5")]      # BO5-FWD-001: separate evidence stream
    bo3_all = [r for r in ledger if not r.get("bo5")]
    dec = [r for r in bo3_all if r["result"] in ("WIN", "LOSS")]
    bo5_dec = [r for r in bo5_all if r["result"] in ("WIN", "LOSS")]
    A = {"n_dec": len(dec),
         "wins": sum(1 for r in dec if r["result"] == "WIN"),
         "push": sum(1 for r in bo3_all if r["result"] == "PUSH"),
         "void": sum(1 for r in bo3_all if r["result"] == "VOID")}
    A["losses"] = A["n_dec"] - A["wins"]
    A["acc"] = A["wins"] / A["n_dec"] if A["n_dec"] else None

    def bucket(rows):
        w = sum(1 for r in rows if r["result"] == "WIN")
        probs = [r["prob"] for r in rows if r["prob"] is not None]
        exp = sum(probs)
        return {"n": len(rows), "w": w, "l": len(rows) - w,
                "exp": round(exp, 2), "act": w,
                "cal": round(w - exp, 2),
                "avg_prob": round(sum(probs) / len(probs), 3) if probs else None}

    A["by_cat"] = {}
    for cat in sorted({r["category"] for r in dec}):
        A["by_cat"][cat] = bucket([r for r in dec if r["category"] == cat])
    A["by_group"] = {g: bucket([r for r in dec if r["group"] == g]) for g in ("rate", "sim", "tg")}
    # APEX-TYPEREC-2026-09-09: book prop type (standard / goblin / demon / red_demon).
    # Logged since 2026-09-08; everything settled before that is "untyped".
    A["by_type"] = {t: bucket([r for r in dec if (r.get("prop_type") or "untyped") == t])
                    for t in sorted({(r.get("prop_type") or "untyped") for r in dec})}
    A["by_att"] = {a: bucket([r for r in dec if r["attention"] == a]) for a in ("HIGH", "MEDIUM", "LOW")}

    edges = [(0.70, 1.01, "70%+"), (0.65, 0.70, "65-70"), (0.60, 0.65, "60-65"),
             (0.55, 0.60, "55-60"), (0.50, 0.55, "50-55"), (0.0, 0.50, "<50")]
    A["buckets"] = []
    for lo, hi, name in edges:
        rows = [r for r in dec if r["prob"] is not None and lo <= r["prob"] < hi]
        if rows:
            b = bucket(rows); b["name"] = name
            b["stated"] = b["avg_prob"]; A["buckets"].append(b)

    losses = [r for r in dec if r["result"] == "LOSS"]
    for r in losses: r["miss"] = miss_class(r)
    A["postmortems"] = losses
    A["miss_counts"] = {}
    for m in ("Near Miss", "Moderate Miss", "Large Miss", "Extreme Miss"):
        A["miss_counts"][m] = sum(1 for r in losses if r["miss"] == m)

    errs = [r for r in dec if r["proj_err"] is not None]
    A["err_overall"] = (round(sum(abs(r["proj_err"]) for r in errs) / len(errs), 2)
                        if errs else None)
    A["err_by_cat"] = {}
    for cat in sorted({r["category"] for r in errs}):
        rows = [r for r in errs if r["category"] == cat]
        A["err_by_cat"][cat] = {
            "mae": round(sum(abs(x["proj_err"]) for x in rows) / len(rows), 2),
            "bias": round(sum(x["proj_err"] for x in rows) / len(rows), 2), "n": len(rows)}
    A["err_by_surface"] = {}
    for s in sorted({r["surface"] for r in errs}):
        rows = [r for r in errs if r["surface"] == s]
        A["err_by_surface"][s] = {
            "mae": round(sum(abs(x["proj_err"]) for x in rows) / len(rows), 2), "n": len(rows)}
    A["largest_errors"] = sorted(errs, key=lambda r: -abs(r["proj_err"]))[:5]

    A["surprise_losses"] = sorted([r for r in losses if r["prob"]],
                                  key=lambda r: -r["prob"])[:5]
    A["surprise_wins"] = sorted([r for r in dec if r["result"] == "WIN" and r["prob"]],
                                key=lambda r: r["prob"])[:5]

    # gated pattern findings: n>=10 per side, >=8pt win% gap
    A["patterns"] = []
    def split(name, pred):
        a = [r for r in dec if pred(r)]; b = [r for r in dec if not pred(r)]
        if len(a) >= 10 and len(b) >= 10:
            wa = sum(1 for r in a if r["result"] == "WIN") / len(a)
            wb = sum(1 for r in b if r["result"] == "WIN") / len(b)
            if abs(wa - wb) >= 0.08:
                A["patterns"].append({"name": name,
                    "yes": f"{wa:.0%} (n={len(a)})", "no": f"{wb:.0%} (n={len(b)})",
                    "early": len(a) < 25 or len(b) < 25})
    split("edge > 1.0", lambda r: (r["edge"] or 0) > 1.0)
    split("MORE direction", lambda r: r["direction"] == "MORE")
    split("high fragility (>=50)", lambda r: (r["fragility"] or 0) >= 50)
    split("zone category", lambda r: r["group"] == "rate")

    # ---- BO5-FWD-001: separate evidence stream (never blended into the numbers above)
    B = bucket(bo5_dec)
    B["push"] = sum(1 for r in bo5_all if r["result"] == "PUSH")
    B["void"] = sum(1 for r in bo5_all if r["result"] == "VOID")
    B["by_cat"] = {c: bucket([r for r in bo5_dec if r["category"] == c])
                   for c in sorted({r["category"] for r in bo5_dec})}
    _ms = (25, 50, 100)
    B["milestone_reached"] = sum(1 for m in _ms if B["n"] >= m)
    B["next_milestone"] = next((m for m in _ms if B["n"] < m), None)
    A["bo5"] = B
    return A

def zone_verdict(A):
    out = []
    for cat in ("aces", "breaks", "double_faults"):
        b = A["by_cat"].get(cat)
        if not b or b["n"] == 0:
            out.append((cat, "KEEP", "no live evidence yet — research/holdout basis stands"))
            continue
        wr = b["w"] / b["n"]
        if b["n"] >= 10 and wr < BE_FLEX and b["cal"] < -1.0:
            out.append((cat, "REMOVE-candidate", f"hit {b['w']} of {b['n']}, engine expected about {round(b['exp'])} wins"))
        elif b["n"] >= 5 and (wr < BE_FLEX or b["cal"] < -0.5):
            out.append((cat, "WATCH", f"hit {b['w']} of {b['n']}, engine expected about {round(b['exp'])} wins"))
        else:
            out.append((cat, "KEEP", f"hit {b['w']} of {b['n']}, engine expected about {round(b['exp'])} wins"))
    return out

# ---------------------------------------------------------------- outputs
def ensure_dirs():
    for d in SUBDIRS:
        os.makedirs(os.path.join(ALIS, d), exist_ok=True)
    os.makedirs(ALIS, exist_ok=True)

def append_only(path, text, header=None):
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new and header: f.write(header)
        f.write(text)

def next_id(prefix, index_path):
    n = 0
    if os.path.exists(index_path):
        for line in open(index_path, encoding="utf-8"):
            m = re.match(rf"{prefix}-(\d+)", line.strip())
            if m: n = max(n, int(m.group(1)))
    return f"{prefix}-{n+1:03d}"

def load_prev_snapshot():
    d = os.path.join(ALIS, "category_intelligence")
    if not os.path.isdir(d): return None
    snaps = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    if not snaps: return None
    with open(os.path.join(d, snaps[-1]), encoding="utf-8") as f:
        return json.load(f)

def trend_word(cat, cur, prev):
    if not prev or cat not in prev.get("by_cat", {}): return "🚫 No prior data" if cur["n"] == 0 else "🟡 Stable (first snapshot)"
    p = prev["by_cat"][cat]
    if cur["n"] == p["n"]: return "🟡 Stable"
    dw = (cur["w"] / cur["n"] if cur["n"] else 0) - (p["w"] / p["n"] if p["n"] else 0)
    if dw > 0.02: return "🟢 Improving"
    if dw < -0.02: return "⚠️ Regressing"
    return "🟡 Stable"

def maybe_recommend(A, zone, cycle_date):
    """Conservative REC generation. Confidence by n."""
    recs = []
    for cat, verdict, why in zone:
        if verdict == "REMOVE-candidate":
            recs.append(("EDGE ZONE", f"Consider removing {cat} from the Edge Zone",
                         why, A["by_cat"][cat]["n"]))
    tg = A["by_cat"].get("total_games")
    if tg and tg["n"] >= 25 and tg["w"] / tg["n"] < BE_FLEX:
        recs.append(("ATTENTION", "Reduce operational attention to Total Games",
                     f"{rec_str(tg['w'], tg['l'])} at n>=25 below break-even, cal {tg['cal']:+}", tg["n"]))
    written = []
    idx = os.path.join(ALIS, "recommendations", "REC_INDEX.md")
    _pending = ""   # APEX-LEARNBOARDS-2026-11-09: one open recommendation per idea, not one a day
    try:
        _pending = "\n".join(l for l in open(idx, encoding="utf-8").read().splitlines() if l.rstrip().endswith("| PENDING"))
    except Exception:
        pass
    for area, title, evidence, n in recs:
        if ("| %s |" % title) in _pending:
            continue
        rid = next_id("REC", idx)
        conf = "Low" if n < 25 else ("Medium" if n < 100 else "High")
        body = (f"# {rid} — {title}\n\nCREATED: {cycle_date}\nAREA: {area}\n"
                f"EVIDENCE: {evidence}\nSAMPLE SIZE: {n}\nCONFIDENCE: {conf}\n"
                f"EXPECTED BENEFIT: focus shifts toward evidence-supported categories\n"
                f"POSSIBLE RISK: sample may still be too small; premature de-focus\n"
                f"STATUS: PENDING\n\n## Outcome log\n")
        with open(os.path.join(ALIS, "recommendations", f"{rid}.md"), "w", encoding="utf-8") as f:
            f.write(body)
        append_only(idx, f"{rid} | {cycle_date} | {title} | PENDING\n",
                    header="# RECOMMENDATION INDEX (append-only)\n")
        append_only(os.path.join(ALIS, "change_log", f"{cycle_date}_changes.md"),
                    f"\n{now_iso()} — {rid} created: {title}\nEVIDENCE: {evidence}\n"
                    f"CONFIDENCE: {conf}\nAPPLIED: no (recommendation only)\n",
                    header=f"# CHANGE LOG {cycle_date}\n")
        written.append(rid)
    return written

def seed_or_update_lessons(A, cycle_date):
    path = os.path.join(ALIS, "lessons_learned.md")
    existing = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    entries = []
    _n = [int(next_id("LESSON", path).split("-")[1])]
    def lesson(cat, obs, ev):
        tag = f"[{cat}] {obs}"
        if tag in existing: return
        lid = f"LESSON-{_n[0]:03d}"; _n[0] += 1
        entries.append(f"\n{lid} · {cycle_date} · {cat}\nOBSERVATION: {obs}\nEVIDENCE: {ev}\nSTATUS: NEW\n[{cat}] {obs}\n")
    for cat, b in A["by_cat"].items():
        if b["n"] >= 5 and abs(b["cal"]) <= 0.5:
            lesson(cat, "Probabilities running honest (record tracks stated %)",
                   f"{rec_str(b['w'], b['l'])} vs exp {b['exp']}")
        if b["n"] >= 10 and b["cal"] <= -1.0:
            lesson(cat, "Running below its own stated probabilities",
                   f"{rec_str(b['w'], b['l'])} vs exp {b['exp']}, cal {b['cal']:+}")
    h = A["by_att"]["HIGH"]
    if h["n"] >= 2:
        lesson("Attention System", f"HIGH-attention props at {rec_str(h['w'], h['l'])}",
               f"avg stated {h['avg_prob']}")
    for e in entries:
        append_only(path, e, header="# LESSONS LEARNED (append-only)\n")
    return len(entries)

def write_major_misses(A, cycle_date):
    path = os.path.join(ALIS, "major_misses.md")
    existing = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    n = 0
    for r in A["postmortems"]:
        if r["miss"] not in ("Large Miss", "Extreme Miss"): continue
        tag = f"{r['date']}|{r['player']}|{r['category']}|{r['line']}"
        if tag in existing: continue
        append_only(path,
            f"\n### {r['miss']} — {r['player']} · {r['category']}\n"
            f"TAG: {tag}\nDATE: {r['date']}\nPROJECTION: {r['projection']}  "
            f"LINE: {r['line']} {r['direction']}  ACTUAL: {r['actual_n']}  "
            f"DIFF: {r['proj_err']}\nSTATED PROB: {r['prob']}\n"
            f"WHY (evidence-level): projection missed by "
            f"{abs(r['proj_err']) if r['proj_err'] is not None else '?'} in Apex's noisiest layer; "
            f"see cycle report {cycle_date}.\n",
            header="# MAJOR MISSES (append-only — highest-value learning)\n")
        n += 1
    return n

def write_edge_history(zone, A, cycle_date):
    path = os.path.join(ALIS, "edge_history.md")
    state = {"zone": [z[0] for z in zone], "verdicts": {z[0]: z[1] for z in zone}}
    marker = json.dumps(state, sort_keys=True)
    if os.path.exists(path):
        content = open(path, encoding="utf-8").read()
        last = re.findall(r"STATE: (.*)", content)
        if last and last[-1] == marker:
            return False  # event-driven: nothing changed, write nothing
    zone_n = sum(A["by_cat"].get(c, {"n": 0})["n"] for c in ("aces", "breaks", "double_faults"))
    status = ("EARLY SIGNAL" if zone_n < 25 else
              ("EVIDENCE EMERGING" if (A["by_group"]["rate"]["w"] /
               max(1, A["by_group"]["rate"]["n"])) > BE_FLEX else "WEAKENING"))
    rb = A["by_group"]["rate"]
    entry = (f"\n{cycle_date}\nZONE: Aces · Breaks · Double Faults\n"
             f"REASON: rate model {rec_str(rb['w'], rb['l'])}, cal {rb['cal']:+}; "
             f"verdicts {', '.join(f'{c}:{v}' for c, v, _ in zone)}\n"
             f"EVIDENCE: {zone_n} settled in zone categories\n"
             f"STATUS: {status}\nCHANGE: verdict/state change recorded\n"
             f"REC: see recommendations/ if any\nSTATE: {marker}\n"
             f"-------------------\n")
    if not os.path.exists(path):
        entry = ("# EDGE HISTORY (append-only)\n\n2026-08-27\n"
                 "ZONE: Aces · Breaks · Double Faults\n"
                 "REASON: Rate model beat price-only baseline in research + holdout; "
                 "first live results (4-1) not contradicting.\nEVIDENCE: 5 settled at entry\n"
                 "STATUS: EARLY SIGNAL\nCHANGE: Initial zone definition (recorded retroactively)\n"
                 "REC: none — founding entry\n-------------------\n") + entry
    append_only(path, entry)
    return True

def fmt_proj(v):
    """Requirement 2: never show internal placeholders reader-facing."""
    return "projection unavailable" if v is None else v

def ranked_best(A):
    """Requirement 1: no BEST without n>=5. Returns (name, bucket) or (None, None)."""
    cands = [kv for kv in A["by_cat"].items() if kv[1]["n"] >= 25 and kv[0] not in BOARD_OFF_CATS]   # APEX-LEARNCARD-2026-11-09
    if not cands:
        return (None, None)
    return max(cands, key=lambda kv: (kv[1]["w"] / kv[1]["n"], kv[1]["n"]))

def findings_text(A, zone, cycle_date, prev):
    imp, weak = [], []
    if prev:
        for cat, b in A["by_cat"].items():
            t = trend_word(cat, b, prev)
            if t.startswith("🟢"): imp.append(cat)
            if t.startswith("⚠️"): weak.append(cat)
    best = ranked_best(A)
    worst = min((kv for kv in A["by_cat"].items() if kv[1]["n"] >= 5),
                key=lambda kv: kv[1]["w"] / kv[1]["n"], default=(None, None))
    honest = all(abs(b["cal"]) <= max(1.5, 0.15 * b["n"]) for b in A["buckets"]) if A["buckets"] else True
    top_miss = next((r for r in A["postmortems"] if r["miss"] in ("Extreme Miss", "Large Miss")),
                    (A["postmortems"][0] if A["postmortems"] else None))
    L = [f"# FINDINGS — {cycle_date}\n",
         f"1. LEARNED TODAY: see lessons appended this cycle; accuracy {A['acc']:.1%} on {A['n_dec']} decided." if A['acc'] is not None else "1. LEARNED TODAY: no decided props.",
         (f"2. MOST IMPORTANT MISTAKE: {top_miss['player']} {top_miss['category']} "
          + (f"proj {top_miss['projection']} → actual {top_miss['actual_n']}"
             if top_miss['projection'] is not None else
             f"lost at {top_miss['prob']:.0%} stated → actual {top_miss['actual_n']}")
          + f" ({top_miss['miss']})"
          if top_miss else "2. MOST IMPORTANT MISTAKE: none this cycle."),
         f"3. IMPROVED: {', '.join(imp) if imp else 'none vs prior snapshot'}",
         f"4. WEAKENED: {', '.join(weak) if weak else 'none vs prior snapshot'}",
         f"5. EDGE ZONE VALID? " + "; ".join(f"{c}: {v} ({w})" for c, v, w in zone),
         f"6. PROBABILITIES HONEST? {'Yes — buckets within tolerance' if honest else 'NO — see bucket table'}",
         f"7. TOMORROW: best={best[0] + ' ' + rec_str(best[1]['w'], best[1]['l']) if best[0] else '⚪ Too Small To Rank'}; "
         f"worst={worst[0] if worst[0] else 'n/a'}; HIGH cards "
         f"{rec_str(A['by_att']['HIGH']['w'], A['by_att']['HIGH']['l'])}"]
    return "\n".join(L) + "\n"

def report_text(A, zone, cycle_date, prev):
    L = [f"# ALIS CYCLE REPORT — {cycle_date}",
         f"generated {now_iso()} · PROP-BASED records · engine APEX-ALIS-2026-08-29-v2 · BO5-FWD-001\n",
         f"## Targets",
         (f"Accuracy {A['acc']:.1%} ({A['wins']}-{A['losses']}, {A['push']} push, {A['void']} void)"
          if A["acc"] is not None else "No decided props."),
         (f"vs 55%: {A['acc']-0.55:+.1%} · vs 65%: {A['acc']-0.65:+.1%} · vs 88%: {A['acc']-0.88:+.1%}"
          if A["acc"] is not None else ""),
         f"Overall projection MAE: {A['err_overall']}\n",
         "## Category intelligence"]
    for cat, b in sorted(A["by_cat"].items()):
        L.append(f"- {cat}: {rec_str(b['w'], b['l'])} · exp {b['exp']} act {b['act']} "
                 f"cal {b['cal']:+} · avg prob {b['avg_prob']} · {trend_word(cat, b, prev)}")
    L.append("\n## Prop type (standard / goblin / demon)")
    _typed = {t: b for t, b in (A.get("by_type") or {}).items() if t != "untyped"}
    if not _typed:
        L.append("- no settled picks carry a prop type yet (logging began 2026-09-08)")
    else:
        for _t in sorted(_typed):
            _b = _typed[_t]; _n = _b["w"] + _b["l"]
            _r3 = ((_b["w"] / _n) ** 3 * 100) if _n else 0
            L.append(f"- {_t}: {rec_str(_b['w'], _b['l'])}"
                     + (f" ({_b['w']/_n:.1%}) · a 3-leg of these cashes {_r3:.0f}% of the time" if _n else ""))
    _u = (A.get("by_type") or {}).get("untyped")
    if _u and (_u["w"] + _u["l"]):
        L.append(f"- untyped (settled before 09-08): {rec_str(_u['w'], _u['l'])}")
    L.append("\n## Groups")
    for g in ("rate", "sim", "tg"):
        b = A["by_group"][g]
        L.append(f"- {GROUP_NAMES[g]}: {rec_str(b['w'], b['l'])} · cal {b['cal']:+}")
    L.append("\n## Attention analysis (recomputed per prop)")
    for a in ("HIGH", "MEDIUM", "LOW"):
        b = A["by_att"][a]
        L.append(f"- {a}: {rec_str(b['w'], b['l'])} · avg stated {b['avg_prob']} · cal {b['cal']:+}")
    L.append("\n## Probability buckets")
    for b in A["buckets"]:
        L.append(f"- {b['name']}: {rec_str(b['w'], b['l'])} (stated ~{b['stated']})")
    L.append("\n## Post-mortems (losses)")
    for m, c in A["miss_counts"].items():
        L.append(f"- {m}: {c}")
    for r in A["postmortems"]:
        L.append(f"  · {r['player']} {r['category']} {r['line']} {r['direction']} "
                 f"prob {r['prob']} proj {fmt_proj(r['projection'])} actual {r['actual_n']} → {r['miss']}")
    L.append("\n## Projection error by category")
    for cat, e in A["err_by_cat"].items():
        L.append(f"- {cat}: MAE {e['mae']} bias {e['bias']:+} (n={e['n']})")
    L.append("\n## Surprises")
    L.append("Top surprising losses: " + "; ".join(
        f"{r['player']} {r['category']} @{r['prob']}" for r in A["surprise_losses"]) if A["surprise_losses"] else "none")
    L.append("Top surprising wins: " + "; ".join(
        f"{r['player']} {r['category']} @{r['prob']}" for r in A["surprise_wins"]) if A["surprise_wins"] else "none")
    L.append("\n## Pattern findings (gated n>=10/side, >=8pt gap)")
    if A["patterns"]:
        for p in A["patterns"]:
            L.append(f"- {p['name']}: yes {p['yes']} vs no {p['no']}" + (" ⚪ early" if p["early"] else ""))
    else:
        L.append("- none cleared the evidence gate (correct behavior at small n)")
    b5 = A.get("bo5")
    if b5 is not None:
        _n = b5["n"]
        ev = ("Very Low" if _n < 5 else "Low" if _n < 10 else
              "Moderate" if _n < 25 else "Strong" if _n < 100 else "Proven")
        L.append("\n## \U0001F3BE BO5 FORWARD TEST (separate evidence stream — never blended into Bo3 records above)")
        if _n == 0:
            L.append("1. Overall: no settled Bo5 props yet")
            L.append("2. Working categories: n/a")
            L.append("3. Weak categories: n/a")
            L.append("4. Calibration vs Bo3: n/a")
            L.append("5. Milestone: BO5 MILESTONE REVIEW #1 at 25 settled (25 remaining)")
        else:
            L.append(f"1. Overall: {rec_str(b5['w'], b5['l'], b5['push'], b5['void'])} · exp {b5['exp']} act {b5['act']} · cal {b5['cal']:+} (Evidence Level: {ev}, n={_n})")
            small = " \u26aa Too Small To Rank" if _n < 5 else ""
            good = "; ".join(f"{c} {rec_str(s['w'], s['l'])} cal {s['cal']:+}" for c, s in b5["by_cat"].items() if s["cal"] >= 0)
            weak = "; ".join(f"{c} {rec_str(s['w'], s['l'])} cal {s['cal']:+}" for c, s in b5["by_cat"].items() if s["cal"] < 0)
            L.append("2. Working categories: " + (good or "none yet") + small)
            L.append("3. Weak categories: " + (weak or "none yet") + small)
            bo3_cal = sum(A["by_group"][g]["cal"] for g in ("rate", "sim", "tg"))
            gap_note = "" if _n >= 25 else " (gap not meaningful below 25)"
            L.append(f"4. Calibration vs Bo3: Bo5 {b5['cal']:+} (n={_n}) · Bo3 {bo3_cal:+.2f} (n={A['n_dec']}){gap_note}")
            if b5["next_milestone"]:
                L.append(f"5. Milestone: BO5 MILESTONE REVIEW #{b5['milestone_reached'] + 1} at {b5['next_milestone']} settled ({b5['next_milestone'] - _n} remaining)")
            else:
                L.append("5. Milestone: all milestones (25/50/100) reached")
        L.append("\n## \U0001F3BE BO5 STATUS")
        L.append("- Supported categories: aces, double_faults, breaks, games_won, fantasy_score, total_games")
        L.append(f"- Evidence Level: {ev} · Settled: {_n} ({b5['push']} push, {b5['void']} void)")
        L.append("- Current policy: \u26a0\ufe0f ALL Bo5 picks LOW attention until 25 settled (BO5-FWD-001)")
        if b5["milestone_reached"] >= 1 and _n > 0:
            k = b5["milestone_reached"]
            bc = b5["by_cat"]
            best = max(bc.items(), key=lambda kv: (kv[1]["cal"], kv[1]["w"]))
            worst = min(bc.items(), key=lambda kv: (kv[1]["cal"], -kv[1]["l"]))
            rate = b5["w"] / _n
            word = ("PROMOTE" if rate > 0.55 and b5["cal"] >= 0 else
                    "RESTRICT" if rate < 0.50 or b5["cal"] <= -3 else "MAINTAIN")
            L.append(f"\n## \U0001F3BE BO5 MILESTONE REVIEW #{k}")
            L.append(f"- Overall: {rec_str(b5['w'], b5['l'], b5['push'], b5['void'])} ({rate:.1%})")
            L.append(f"- Calibration: {b5['cal']:+} over {_n} settled")
            L.append(f"- Best category: {best[0]} {rec_str(best[1]['w'], best[1]['l'])}")
            L.append(f"- Worst category: {worst[0]} {rec_str(worst[1]['w'], worst[1]['l'])}")
            L.append(f"- vs Bo3: Bo5 {rate:.1%} vs Bo3 " + (f"{A['acc']:.1%}" if A["acc"] is not None else "n/a"))
            L.append(f"- RECOMMENDATION: {word} (rule: >55% and cal>=0 -> PROMOTE; <50% or cal<=-3 -> RESTRICT; else MAINTAIN)")
            L.append("- ALIS recommends only — no automatic change is made.")
    L.append("\n## Edge Zone verdict")
    for c, v, w in zone:
        L.append(f"- {c}: {v} — {w}")
    return "\n".join(L) + "\n"

def discord_text(A, zone, cycle_date, new_lessons, new_misses, recs):
    best = ranked_best(A)
    worst = min((kv for kv in A["by_cat"].items() if kv[1]["n"] >= 5),
                key=lambda kv: kv[1]["w"] / kv[1]["n"], default=("n/a", None))
    h = A["by_att"]["HIGH"]
    top_miss = next((r for r in A["postmortems"] if r["miss"] in ("Extreme Miss", "Large Miss")), None)
    lines = [f"🧠 APEX LEARNING UPDATE — {cycle_date}",
             f"LEARNED: {new_lessons} new lesson(s); {new_misses} new major miss(es) archived",
             (f"MISTAKE OF THE DAY: {top_miss['player']} {top_miss['category']} — "
              + (f"proj {top_miss['projection']} → {top_miss['actual_n']}"
                 if top_miss['projection'] is not None else
                 f"lost at {top_miss['prob']:.0%} stated (actual {top_miss['actual_n']})")
              + f" ({top_miss['miss']})"
              if top_miss else "MISTAKE OF THE DAY: none large this cycle"),
             ("BEST: " + (f"{best[0]} {rec_str(best[1]['w'], best[1]['l'])}" if best[0] else "⚪ Too Small To Rank")
              + " · WORST: " + (f"{worst[0]} {rec_str(worst[1]['w'], worst[1]['l'])}" if worst[1] else "⚪ Too Small To Rank")),
             f"HIGH-ATTENTION: {rec_str(h['w'], h['l'])} (avg stated {h['avg_prob']})",
             "EDGE ZONE: " + ", ".join(f"{c}:{v}" for c, v, _ in zone),
             ((f"\U0001F3BE BO5 FWD: {rec_str(A['bo5']['w'], A['bo5']['l'])} · cal {A['bo5']['cal']:+} · "
               + (f"{A['bo5']['next_milestone'] - A['bo5']['n']} to review #{A['bo5']['milestone_reached'] + 1}"
                  if A['bo5']['next_milestone'] else "all milestones reached"))
              if A.get("bo5") and A["bo5"]["n"] else "\U0001F3BE BO5 FWD: 0 settled (LOW until 25)"),
             f"NEW REC: {', '.join(recs) if recs else 'none — nothing cleared the evidence bar'}"]
    if A["acc"] is not None:
        lines.append(f"ACCURACY {A['acc']:.1%} · 55% {A['acc']-0.55:+.1%} · "
                     f"65% {A['acc']-0.65:+.1%} · 88% {A['acc']-0.88:+.1%}")
    lines.append(f"Full report: apex_learning_system/reports/{cycle_date}_cycle.md")
    return "\n".join(lines)

# ================================================================ APEX-DASH-2026-09-30
# NIGHTLY LEARNING DASHBOARD -- reporting-only redesign of the Learning
# Center, approved 2026-09-30. Reads ALIS's own already-computed ledger /
# analyze() output; for score/confidence bands it RECOMPUTES via
# apex_edge_tracker's own live apex_score()/apex_confidence() functions
# (the exact functions the bot calls) against historical settled rows,
# purely to report how those scores performed. Nothing below writes to
# apex_edge_tracker and nothing changes Apex Score, Confidence, Rankings,
# or any ALIS computation above this marker. "Only redesign reporting."

# apex_edge_tracker tracks a couple of categories under a different key
# than ALIS's own canonical ledger name (ALIS's CANON dict maps the other
# direction for ingestion). This is read-only translation so the real
# settled history is found -- without it these two categories would
# silently look untested in the recomputed bands below.
_AET_CAT = {"double_faults": "dfs", "first_set_winner": "first_set_fav_wins"}

SCORE_BANDS = [("90+", 90, 101), ("80-89", 80, 90), ("70-79", 70, 80),
               ("60-69", 60, 70), ("Below 60", 0, 60)]
CONF_BANDS = [("95-100", 95, 101), ("90-94", 90, 95), ("85-89", 85, 90),
              ("80-84", 80, 85), ("Below 80", 0, 80)]

def score_confidence_bands(ledger):
    """Read-only recompute of real Apex Score / Confidence per settled
    Bo3 WIN/LOSS ledger row, using apex_edge_tracker's own live scoring
    functions and its own current category stats (records()). Returns
    (score_bands, conf_bands, error). error is set (bands stay {}) only
    if apex_edge_tracker could not be reached -- fails open, never
    crashes the cycle."""
    try:
        sys.path.insert(0, BASE)
        import apex_edge_tracker as aet
    except Exception as e:
        return {}, {}, f"apex_edge_tracker unavailable ({type(e).__name__})"
    try:
        rec = aet.records()
    except Exception as e:
        return {}, {}, f"apex_edge_tracker.records() failed ({type(e).__name__})"

    sb = {name: {"w": 0, "l": 0} for name, _, _ in SCORE_BANDS}
    cb = {name: {"w": 0, "l": 0} for name, _, _ in CONF_BANDS}
    rows = [r for r in ledger if (not r.get("bo5")) and r["result"] in ("WIN", "LOSS")
            and r.get("prob") is not None and r.get("category")]
    for r in rows:
        cat = _AET_CAT.get(r["category"], r["category"])
        try:
            sc, ev, lvl, up, down = aet.apex_score(r["prob"], cat, False, rec)
            cf, ctier, why = aet.apex_confidence(cat, False, rec, score=sc, prob=r["prob"],
                                                  evidence=ev, attention=lvl)
        except Exception:
            continue
        won = r["result"] == "WIN"
        for name, lo, hi in SCORE_BANDS:
            if lo <= sc < hi:
                sb[name]["w" if won else "l"] += 1
                break
        for name, lo, hi in CONF_BANDS:
            if lo <= cf < hi:
                cb[name]["w" if won else "l"] += 1
                break
    return sb, cb, None

def _band_line(name, b):
    n = b["w"] + b["l"]
    if n == 0:
        return f"{name}: no settled picks yet"
    return f"{name} ({b['w']}W-{b['l']}L, {b['w']/n:.1%})"

def _cat_label(cat):
    try:
        sys.path.insert(0, BASE)
        import apex_edge_tracker as aet
        return aet.LABEL.get(_AET_CAT.get(cat, cat), cat.replace("_", " ").title())
    except Exception:
        return cat.replace("_", " ").title()

def market_performance(A, min_n=5):
    """Reuses A['by_cat'] exactly as analyze() already computed it --
    zero new computation, just sorting/formatting for the dashboard."""
    cats = [(c, b) for c, b in A["by_cat"].items() if b["n"] >= min_n]
    cats.sort(key=lambda cb: -(cb[1]["w"] / cb[1]["n"]))
    return cats

def changes_from_yesterday(A, prev):
    """Per-category hit-rate delta vs the most recent prior snapshot (the
    same snapshot ALIS already loads as `prev` before writing today's).
    Read-only comparison -- does not alter either snapshot."""
    out = []
    if not prev or not prev.get("by_cat"):
        return out
    for cat, cur_b in A["by_cat"].items():
        prev_b = prev["by_cat"].get(cat)
        if not prev_b or cur_b["n"] < 5 or prev_b["n"] < 5:
            continue
        delta = (cur_b["w"] / cur_b["n"] - prev_b["w"] / prev_b["n"]) * 100.0
        if abs(delta) < 0.1:
            continue
        out.append((cat, delta))
    out.sort(key=lambda cd: -abs(cd[1]))
    return out

def _pool_changed(A, prev):
    """APEX-LEARNREPORT-2026-11-11: True when today's settled pool is SMALLER than yesterday's or grew by
    more than 10% -- that is a counting change (filter, rebuild), not one day of results."""
    try:
        cur = sum(b["n"] for b in A["by_cat"].values())
        old = sum(b["n"] for b in (prev or {}).get("by_cat", {}).values())
        return bool(old) and (cur < old or cur > old * 1.10)
    except Exception:
        return False

def _sig_n(t):
    try:
        if isinstance(t, dict):
            return int(t.get("n", 0) or 0)
        m = re.search(r"'n': (\d+)", str(t))
        return int(m.group(1)) if m else 0
    except Exception:
        return 0

def signal_performance_text():
    """Honest pass-through of openclaw_ops.signal_trend(). ALIS does not
    own signal history -- this only reports what's already there. When
    empty, says so plainly instead of inventing signal names."""
    try:
        sys.path.insert(0, BASE)
        import openclaw_ops as ops
        trend = ops.signal_trend()
    except Exception as e:
        return None, f"signal performance unavailable ({type(e).__name__})"
    return trend, None

def pending_count():
    """Current unsettled active board size -- read-only, same board-build
    path used everywhere else in the bot (apex_edge_tracker.load_current_
    board). Never affects which picks are active; purely a count here."""
    try:
        sys.path.insert(0, BASE)
        import apex_edge_tracker as aet
        picks, _note = aet.load_current_board()
        return len(picks)
    except Exception:
        return None

# ---------------------------------------------------------------- APEX-BOARDCAP-2026-10-01
# Per-board (Elite6/Top10/Best Goblin/Best Demon/Best Standard) win/loss
# records. READ-ONLY -- reads the board-membership capture log that
# apex_edge_tracker.capture_board_membership() writes at publish time;
# this file never writes to it (same hard rule as the three main logs).
# That capture log only has rows from 2026-10-01 forward -- there is no
# way to backfill which props were on which board before capture started,
# so any window reaching earlier than that will simply show "no settled
# picks yet" for those days. Not a bug; there is nothing to recover.
BOARD_TYPES = [("elite6", "Elite6"), ("top10", "Top10"), ("top3", "Top 3"),
               ("bestgoblin", "Best Goblin"),
               ("bestdemon", "Best Demon"), ("beststandard", "Best Standard")]

def load_board_membership_log():
    try:
        sys.path.insert(0, BASE)
        import apex_edge_tracker as aet
        path = aet.BOARD_MEMBERSHIP_LOG
    except Exception:
        return []
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

def board_records(ledger, cycle_date, days=1):
    """APEX-LEARNBOARDS-2026-11-09 (owner, "go fix learning board numbers"):
    whole-slip record per board from the OFFICIAL board (last board before its
    own first pick started), read from data/board_history.sqlite the same way
    /counters and PLAY THIS do. One slip a day per board: won = every pick hit
    (voids don't lose). Short boards and days still waiting on results are not
    counted. Falls back to the old membership-log join only if that read fails."""
    try:
        return _board_records_official(cycle_date, days)
    except Exception as e:
        print("[learning] official board record failed (%s: %s); using membership log" % (type(e).__name__, e))
        return _board_records_membership(ledger, cycle_date, days)

def _board_records_official(cycle_date, days=1):
    sys.path.insert(0, BASE)
    import apex_best_time as bt
    import apex_board_counters as bc
    sizes = {b: n for b, _name, n, _play in bc.BOARDS}
    end = date.fromisoformat(cycle_date) - timedelta(days=1)
    start = max(end - timedelta(days=days - 1), date(2026, 10, 10))
    out = {}
    c = bt._db()
    try:
        for flag, name in BOARD_TYPES:
            t = dict(n=0, w=0, l=0, hit=0, miss=0, short=0, wait=0, official=True)
            d = start
            while d <= end:
                rows = bt._official(c, flag, d)
                if rows:
                    legs = rows[:sizes.get(flag, 6)]
                    if len(legs) < sizes.get(flag, 6):
                        t["short"] += 1
                    else:
                        res = [bt._result(c, r) for r in legs]
                        t["hit"] += res.count("hit"); t["miss"] += res.count("miss")
                        if None in res:
                            t["wait"] += 1
                        else:
                            t["n"] += 1
                            if res.count("miss") == 0:
                                t["w"] += 1
                            else:
                                t["l"] += 1
                d += timedelta(days=1)
            out[name] = t
    finally:
        c.close()
    # APEX-SLIPSV2DASH-2026-11-09 (owner, "Go slips v2"): Top 3 / Elite 6 / Elite 5 come
    # from the Slips V2 tracker (his real slip record: the slip for a match day, graded as placed),
    # not from the board-history replay. The other boards keep the board-history official record.
    try:
        import apex_slips_v2 as V
        allv = V._load(V.LOG, [])
        off = V.pick(allv, "official")
        for flag, name, size in (("top3", "Top 3", 3), ("elite6", "Elite6", 6)):
            t = dict(n=0, w=0, l=0, hit=0, miss=0, short=0, wait=0, partial=0, official=True, source="slips v2")
            d = start
            while d <= end:
                e = (off.get(d.isoformat()) or {}).get(flag)
                if e:
                    st = e.get("status")
                    # APEX-LEARNREPORT-2026-11-11 (owner, "fix the misleading"): a short board is a real
                    # day he could play, so it counts (marked "short"); a void pick shrinks the slip, never loses it.
                    _short = e.get("n", size) < size
                    if st == "pending":
                        t["wait"] += 1
                    elif st in ("won", "lost", "partial", "void"):
                        h = e.get("hits", 0) or 0; m = e.get("misses", 0) or 0
                        if h == 0 and m == 0:
                            d += timedelta(days=1); continue    # nothing graded (all void): no day
                        t["n"] += 1; t["hit"] += h; t["miss"] += m; t["short"] += _short
                        if m == 0:
                            t["w"] += 1
                        else:
                            t["l"] += 1; t["partial"] += (m == 1 and not _short); t["partial2"] = t.get("partial2", 0) + (m == 2 and not _short)
                d += timedelta(days=1)
            out[name] = t
        t5 = dict(n=0, w=0, l=0, hit=0, miss=0, short=0, wait=0, partial=0, official=True, source="slips v2")
        d = max(start, date.fromisoformat(V.ELITE5_FROM))
        while d <= end:
            e6 = (off.get(d.isoformat()) or {}).get("elite6")
            r = V.elite5_result(e6) if e6 else None
            if r:
                st, hit, miss, _void, _pl = r
                if st == "pending":
                    t5["wait"] += 1
                else:
                    t5["n"] += 1; t5["hit"] += hit; t5["miss"] += miss
                    if st == "won":
                        t5["w"] += 1
                    else:
                        t5["l"] += 1; t5["partial"] += (st == "partial")
            elif e6 and len(e6.get("legs") or []) < V.ELITE5_N:
                res = [l.get("result") for l in (e6.get("legs") or [])]
                h, m = res.count("hit"), res.count("miss")
                if None in res and m == 0:
                    t5["wait"] += 1
                elif h or m:
                    t5["n"] += 1; t5["hit"] += h; t5["miss"] += m; t5["short"] += 1
                    if m == 0:
                        t5["w"] += 1
                    else:
                        t5["l"] += 1
            d += timedelta(days=1)
        out["Elite 5"] = t5
    except Exception as e:
        print("[learning] slips v2 board record failed (%s: %s); board-history record kept" % (type(e).__name__, e))
    return out

def _board_records_membership(ledger, cycle_date, days=1):
    """Per-board win/loss record over a trailing window of `days` calendar
    day(s) ending the day before cycle_date (days=1 -> yesterday only,
    days=7 -> the 7 settled days before today). Joins the board-membership
    log to the settled ledger on (date, player, category, line) -- the
    same identity shape build_ledger() already dedups on."""
    bm_rows = load_board_membership_log()
    empty = {name: {"n": 0, "w": 0, "l": 0} for _, name in BOARD_TYPES}
    if not bm_rows:
        return empty
    try:
        end = date.fromisoformat(cycle_date) - timedelta(days=1)
    except Exception:
        return empty
    start = end - timedelta(days=days - 1)

    ledger_by_key = {}
    for r in ledger:
        if r["result"] not in ("WIN", "LOSS"):
            continue
        ledger_by_key[(r["date"], r["player"].strip().lower(), r["category"],
                       str(r["line"]))] = r

    out = {}
    for flag, name in BOARD_TYPES:
        w = l = 0
        for row in bm_rows:
            if str(row.get(flag)) not in ("1", "True", "true"):
                continue
            try:
                d = date.fromisoformat(row["date"])
            except Exception:
                continue
            if not (start <= d <= end):
                continue
            key = (row["date"], row["player"].strip().lower(),
                   canon_cat(row["category"]), str(row["line"]))
            lr = ledger_by_key.get(key)
            if not lr:
                continue
            if lr["result"] == "WIN":
                w += 1
            else:
                l += 1
        out[name] = {"n": w + l, "w": w, "l": l}
    return out

def _board_names(recs):
    names = [name for _, name in BOARD_TYPES]
    if "Elite 5" in recs:
        names.insert(names.index("Elite6") + 1, "Elite 5")
    return names

def _board_block_lines(recs):
    lines = []
    for name in _board_names(recs):
        b = recs.get(name, {"n": 0, "w": 0, "l": 0})
        if b["n"] == 0:
            extra = ""
            if b.get("short") or b.get("wait"):
                extra = " (%s)" % ", ".join(x for x in [("%d day%s with too few picks" % (b["short"], "" if b["short"] == 1 else "s")) if b.get("short") else "",
                                                       ("%d waiting on results" % b["wait"]) if b.get("wait") else ""] if x)
            lines.append(f"  {name}: no finished days{extra}")
        else:
            emoji = "✅" if b["w"] / b["n"] >= 0.5 else "⚠️"
            part = ""
            if b.get("partial"):
                part = " · %s on %d day%s" % ("4 of 5" if name == "Elite 5" else ("5 of 6" if name == "Elite6" else "one miss"), b["partial"], "" if b["partial"] == 1 else "s")
            if name == "Elite6" and b.get("partial2"):
                part += " · 4 of 6 on %d day%s" % (b["partial2"], "" if b["partial2"] == 1 else "s")
            if b.get("short"):
                if b.get("source") == "slips v2":
                    part += " · %d of these day%s had fewer picks than normal" % (b["short"], "" if b["short"] == 1 else "s")
                else:
                    part += " · %d more day%s had too few picks (not counted)" % (b["short"], "" if b["short"] == 1 else "s")
            lines.append(f"  {name}: won {b['w']} of {b['n']} day{'' if b['n'] == 1 else 's'} {emoji}{part} · picks {b.get('hit', 0)}-{b.get('miss', 0)}")
    return lines

def _board_leaderboard(recs):
    ranked = [(name, recs[name]) for name in _board_names(recs) if recs.get(name, {"n": 0})["n"] > 0]
    ranked.sort(key=lambda nb: -(nb[1]["w"] / nb[1]["n"]))
    lines = ["🏅 BOARD PERFORMANCE (official board, whole slips, last 7 days)"]
    if not ranked:
        lines.append("  No finished board days yet")
        return lines
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for i, (name, b) in enumerate(ranked):
        tag = medals[i] if i < len(medals) else f"{i+1}."
        lines.append(f"  {tag} {name}: won {b['w']} of {b['n']} day{'' if b['n'] == 1 else 's'}")
    return lines

def _score_health_takeaway(sb):
    above_w = sb["90+"]["w"] + sb["80-89"]["w"] + sb["70-79"]["w"]
    above_l = sb["90+"]["l"] + sb["80-89"]["l"] + sb["70-79"]["l"]
    above_n = above_w + above_l
    below_w = sb["60-69"]["w"] + sb["Below 60"]["w"]
    below_l = sb["60-69"]["l"] + sb["Below 60"]["l"]
    below_n = below_w + below_l
    if above_n < 5 or below_n < 5:
        return ["⚪ Not enough settled picks yet to compare 70+ vs below-70 reliably"]
    above_pct, below_pct = above_w / above_n, below_w / below_n
    if above_pct > below_pct:
        return ["✅ Apex works best above 70",
                f"✅ 70+ picks win {above_pct:.0%}",
                f"⚠️ Below 70 only wins {below_pct:.0%}"]
    return [f"  70+ picks win {above_pct:.0%}, below-70 wins {below_pct:.0%}"]

def apex_action_items(A, zone, score_bands, deltas):
    items = []
    best = ranked_best(A)
    if best[0]:
        items.append(f"✅ Best category you can bet: {_cat_rec(best[0], best[1])}")
    for cat, status, why in zone:
        if status == "REMOVE-candidate":
            items.append(f"🚨 {_cat_label(cat)} remains below break-even — {why}")
        elif status == "WATCH":
            items.append(f"⚠ Monitor {_cat_label(cat)} — {why}")
    for cat, delta in deltas:
        if delta <= -2.0:
            items.append(f"⚠ {_cat_label(cat)} down {delta:+.1f}pt since last cycle")
    below70_w = sum(score_bands.get(n, {"w": 0})["w"] for n in ("60-69", "Below 60"))
    below70_l = sum(score_bands.get(n, {"l": 0})["l"] for n in ("60-69", "Below 60"))
    below70 = below70_w + below70_l
    if below70 >= 10 and (below70_w / below70) < 0.542:
        items.append("🚨 Apex Scores below 70 are underwater vs break-even — treat as low-trust tier")
    if not items:
        items.append("⚪ No action items this cycle — insufficient evidence to flag anything yet")
    return items

def nightly_dashboard_text(ledger, A, zone, cycle_date, prev):
    """APEX-DASH-2026-10-01: Learning Center Simplification. A user should
    understand system performance in under 30 seconds. Order: Apex Score
    Health -> Yesterday's Results (per board) -> Last 7 Days (per board)
    -> Board Performance leaderboard -> Hot Categories -> Avoid For Now
    -> What Apex Learned Today (max 5 bullets). Probability buckets,
    calibration numbers and raw confidence bands are collapsed into an
    Advanced Analytics block at the very end, not the main view. Per-board
    sections depend on apex_edge_tracker.capture_board_membership(), which
    only has data from 2026-10-01 forward -- no retroactive backfill is
    possible, so those sections read "no settled picks yet" until data
    accrues."""
    sb, cb, sc_err = score_confidence_bands(ledger)
    L = [f"🧠 NIGHTLY LEARNING REPORT — {cycle_date}", "=" * 50, ""]
    # APEX-LEARNREPORT-2026-11-11: your boards first; everything after them is every line Apex rated.
    _boards_first = []
    try:
        _r1 = board_records(ledger, cycle_date, days=1); _r7 = board_records(ledger, cycle_date, days=7)
        _boards_first += ["🎯 YOUR BOARDS YESTERDAY (whole slip: every pick must hit)"] + _board_block_lines(_r1)
        _boards_first += ["", "📈 YOUR BOARDS, LAST 7 DAYS (whole slip: every pick must hit)"] + _board_block_lines(_r7)
    except Exception as e:
        _boards_first += [f"🎯 YOUR BOARDS: unavailable ({type(e).__name__}: {e})"]
    L += _boards_first + ["", "=" * 50, "",
          "⬇️ EVERYTHING BELOW = every tennis line Apex rated this season, NOT your boards ⬇️", ""]

    # 1. APEX SCORE HEALTH
    if sc_err:
        L += [f"⚠ Apex Score Health unavailable: {sc_err}", ""]
    else:
        below70_w = sum(sb[n]["w"] for n in ("60-69", "Below 60"))
        below70_l = sum(sb[n]["l"] for n in ("60-69", "Below 60"))
        below70_n = below70_w + below70_l
        L += ["🏆 APEX SCORE HEALTH (all lines rated, not your boards)"]
        for name in ("90+", "80-89", "70-79"):
            L.append("  " + _band_line(name, sb[name]))
        L.append(f"  Below 70 ({below70_w}W-{below70_l}L, {below70_w/below70_n:.1%})"
                 if below70_n else "  Below 70: no settled picks yet")
        L += ["", *["  " + t if not t.startswith(("✅", "⚠️", "⚪")) else t
                     for t in _score_health_takeaway(sb)]]
        L += ["", "=" * 50, ""]

    # 2 & 3. Per-board Yesterday / Last 7 Days
    try:
        recs1 = board_records(ledger, cycle_date, days=1)
        recs7 = board_records(ledger, cycle_date, days=7)
        bm_err = None
    except Exception as e:
        recs1 = recs7 = {}
        bm_err = f"{type(e).__name__}: {e}"

    # (board results now print once, at the top of the report)

    # 5 & 6. Hot Categories / Avoid For Now
    mkt = market_performance(A)
    if mkt:
        top = mkt[:3]
        top_cats = {c for c, _ in top}
        bottom = [(c, b) for c, b in reversed(mkt) if c not in top_cats][:3]
        L += ["🔥 HOT CATEGORIES (all lines rated, not your boards)"]
        L += [f"  {_cat_rec(c, b)}" for c, b in top]
        L += ["", "🚫 WEAKEST CATEGORIES (all lines rated, not your boards)"]
        L += ([f"  {_cat_rec(c, b)}" for c, b in bottom]
              if bottom else ["  n/a — not enough distinct categories yet"])
    else:
        L += ["🔥 HOT CATEGORIES", "  No category has 5+ settled picks yet",
              "", "🚫 AVOID FOR NOW", "  No category has 5+ settled picks yet"]
    L += ["", "=" * 50, ""]

    # 7. What Apex Learned Today -- max 5 plain-English bullets
    deltas = changes_from_yesterday(A, prev)
    _recount = _pool_changed(A, prev)
    if _recount:
        deltas = []          # the count itself changed; a day-to-day compare would show fake moves
    L += ["🧠 WHAT APEX LEARNED TODAY"]
    L += [f"  {item}" for item in apex_action_items(A, zone, sb, deltas)[:5]]
    L.append(_sellable_note())   # APEX-LEARNCARD-2026-11-09
    L += ["", "=" * 50, ""]

    # 8. Advanced Analytics -- collapsed to the end: confidence bands,
    # signal performance, category deltas. Nothing here is new data, just
    # relocated out of the 30-second main view.
    L += ["📊 ADVANCED ANALYTICS (optional detail)", ""]
    if not sc_err:
        L += ["Confidence Performance:"]
        L += ["  " + _band_line(name, cb[name]) for name, _, _ in CONF_BANDS]
        L += [""]
    trend, sig_err = signal_performance_text()
    L += ["Signal Performance (signals with 20+ graded lines only):"]
    trend = [t for t in (trend or []) if _sig_n(t) >= 20]
    if sig_err:
        L.append(f"  Unavailable: {sig_err}")
    elif not trend:
        L.append("  No signal has 20 graded lines yet, so nothing to show.")
    elif False:
        L += ["  No signal history recorded yet (data/learning/signal_history.json is empty) —",
              "  ALIS has not logged enough signal-level outcomes to report this section honestly."]
    else:
        L += [f"  {s}" for s in trend]
    L += ["", "Changes From Yesterday (categories, all lines):"]
    if _recount:
        L.append("  The way lines are counted changed since yesterday, so no fair compare today.")
    elif deltas:
        L += [f"  {'✅' if d > 0 else '❌'} {_cat_label(c)} {d:+.1f}%" for c, d in deltas]
    else:
        L.append("  No prior snapshot with enough settled picks to compare yet")
    L += ["", "=" * 50]
    L.append(f"Full report: apex_learning_system/reports/{cycle_date}_cycle.md")
    return "\n".join(L)


# ================================================================ APEX-FETCHRELIABILITY-2026-10-01
# USER PERFORMANCE REPORT -- the half of the Health Check Redesign that
# belongs to the person reading it, not to debugging the scrape. Posted
# after every /fetch, separate from the Developer Health report (board
# counts, retirement, decline breakdown -- see discord_bot.py's
# _board_health_report_text() / slash_fetch summary, unchanged). Exact
# fields requested: Elite6 / Goblin / Demon / Standard / Top10 / Top 3
# records, 70+ Score Hit Rate, Hot Categories, Avoid Categories, What
# Apex Learned Today. Pure formatting over read-only data this module
# already computes elsewhere (board_records, market_performance,
# score_confidence_bands, apex_action_items) -- nothing new is scored or
# ranked here. Fails open: any missing piece prints "no settled picks
# yet" / "unavailable" rather than raising, so a /fetch never fails
# because this report couldn't be built.
def _band_line_v2(name, b):
    """Same W-L/hit-rate line as _band_line, plus an explicit sample size
    -- APEX-UPR-V2-2026-10-01 asked for Record + Hit Rate + Sample Size
    together on one line."""
    n = b["w"] + b["l"]
    if n == 0:
        return f"{name}: no settled picks yet"
    return f"{name}: {b['w']}-{b['l']} ({b['w']/n:.1%}, n={n})"


# APEX-UPR-V2-2026-10-01: display order for the Board Scorecard, exactly
# as owner listed it (Top 3, Elite6, Top10, Goblin, Demon, Standard)
# -- a presentation-only reordering of the existing BOARD_TYPES records,
# never a change to what board_records() computes.
SCORECARD_ORDER = [("Top 3", "\U0001F947"), ("Elite6", "\U0001F3C6"), ("Top10", "\U0001F51F"),
                    ("Best Goblin", "\U0001F7E2"), ("Best Demon", "\U0001F534"),
                    ("Best Standard", "\u26AA")]


def _scorecard_lines(ledger, cycle_date):
    """Per-board Yesterday / Last 7 Days / Last 30 Days / Lifetime, reusing
    board_records() for each window -- no new computation, same join to
    the board-membership log as the existing Yesterday/Last-7 sections.
    'Lifetime' is simply a long enough window (3650 days) that it is
    bounded only by how far back the board-membership log actually goes
    (2026-10-01 forward) -- not a separate code path."""
    try:
        windows = [("Yesterday", board_records(ledger, cycle_date, days=1)),
                   ("Last 7 Days", board_records(ledger, cycle_date, days=7)),
                   ("Last 30 Days", board_records(ledger, cycle_date, days=30)),
                   ("Lifetime", board_records(ledger, cycle_date, days=3650))]
        err = None
    except Exception as e:
        windows, err = [], f"{type(e).__name__}: {e}"
    L = ["\U0001F3AF BOARD SCORECARD"]
    if err:
        L.append(f"  Unavailable: {err}")
        return L, windows
    for name, emoji in SCORECARD_ORDER:
        L.append(f"  {emoji} {name}")
        for wname, recs in windows:
            b = recs.get(name, {"n": 0, "w": 0, "l": 0})
            if b["n"] == 0:
                L.append(f"    {wname}: no finished days")
            else:
                L.append(f"    {wname}: won {b['w']} of {b['n']} day{'' if b['n'] == 1 else 's'} · picks {b.get('hit', 0)}-{b.get('miss', 0)}")
    return L, windows


# APEX-UPR-V2-2026-10-01 (Held Back Transparency, carried into the User
# Performance Report): reads the SAME held_back lists apex_edge_tracker's
# best_bets_detail()/elite6_detail() already return per-call (built this
# morning for Held Back Transparency) -- never re-derives rank/score/
# reason itself. This is a fresh, live read of the current board through
# the normal Elite6/Goblin/Demon/Standard views, exactly like a user
# running those commands by hand; it does not change what any of those
# commands show, and it never touches scoring, confidence, or ranking.
def _collect_held_back():
    try:
        sys.path.insert(0, BASE)
        import apex_edge_tracker as aet
    except Exception as e:
        return None, f"apex_edge_tracker unavailable ({type(e).__name__})"
    try:
        board, _note = aet.load_current_board()
        rec = aet.records()
        views = []
        try:
            views.append(("Elite6", aet.elite6_detail(board, rec=rec, return_scored=True)))
        except Exception:
            pass
        for pt, label in (("goblin", "Goblin"), ("demon", "Demon"), ("standard", "Standard")):
            try:
                views.append((label, aet.best_bets_detail(board, rec=rec, prop_type=pt,
                                                            limit=50, return_scored=True)))
            except Exception:
                continue
        all_held, seen = [], set()
        for board_name, r in views:
            for h in (r.get("held_back") or []) if isinstance(r, dict) else []:
                p = h.get("p") or {}
                key = (board_name, p.get("player"), p.get("category"), str(p.get("line")), h.get("reason"))
                if key in seen:
                    continue
                seen.add(key)
                all_held.append({"board": board_name, "rank": h.get("rank"),
                                  "score": h.get("score"), "p": p, "reason": h.get("reason") or ""})
        return all_held, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _held_back_name(p):
    return p.get("opponent") if p.get("player") == "match" else p.get("player", "")


def _held_back_summary_lines(all_held, err):
    L = ["\U0001F440 HELD BACK SUMMARY"]
    if err:
        L.append(f"  Unavailable: {err}")
        return L
    all_held = all_held or []
    L.append(f"  Held Back Count: {len(all_held)}")
    if not all_held:
        L.append("  No held-back props on the current board")
        return L
    top5 = sorted(all_held, key=lambda h: -(h["score"] if h["score"] is not None else -1))[:5]
    L.append("  Top 5 held-back picks:")
    for h in top5:
        L.append("    Rank #%s \u00b7 %s \u00b7 Apex Score %s \u00b7 %s (%s)"
                 % (h["rank"] if h["rank"] is not None else "?", _held_back_name(h["p"]),
                    round(h["score"]) if h["score"] is not None else "?", h["reason"], h["board"]))
    return L


def _demon_watchlist_lines(all_held):
    """APEX-UPR-V2-2026-10-01: ALL Demon props currently below the Demon
    quality floor, highest Apex Score first, never truncated -- distinct
    from the 10-point 'near miss' watchlist best_bets_detail() shows
    inline; this is the complete below-floor Demon list for the report."""
    L = ["\U0001F47A DEMON WATCHLIST"]
    misses = [h for h in (all_held or [])
              if h["board"] == "Demon" and "quality floor" in (h["reason"] or "")]
    if not misses:
        L.append("  No Demon props currently below the floor")
        return L
    misses.sort(key=lambda h: -(h["score"] if h["score"] is not None else -1))
    for h in misses:
        L.append("  \U0001F4CD Rank #%s \u00b7 \U0001F3BE %s \u00b7 \U0001F4C8 Apex Score: %s"
                 % (h["rank"] if h["rank"] is not None else "?", _held_back_name(h["p"]),
                    round(h["score"]) if h["score"] is not None else "?"))
    return L


# ================================================================ APEX-UPR-V3-2026-10-01
# USER PERFORMANCE REPORT V3 + BOARD TRANSPARENCY OVERHAUL + PRIZEPICKS-
# CONSISTENT EMOJIS (approved by owner). Board Scorecard now leads
# the report (Objective #3); Apex Score Health and the Most Important
# Takeaway are expanded with medal-tier bands and Record/Hit%/Sample Size
# together (Objective #2); the Board Leaderboard shows a medal + board
# emoji per rank (Objective #4); Best/Worst Performing Category reverts
# to Hot/Cold Categories -- top 3 / bottom 3 (Objective #8); Held-Back
# Transparency gains a 6-bucket breakdown ahead of the ranked picks list,
# renamed Top Held-Back Picks (Objective #6); the Demon Watchlist adds
# "Missed by N" to every row and still never truncates (Objective #7).
# Goblin/Demon/Standard now read 🟢/🔴/⚪ everywhere, matching PrizePicks'
# own board colors (Objective #1). Every section here is still pure
# formatting over the same read-only data (score_confidence_bands,
# board_records, market_performance, _collect_held_back) -- nothing
# scores, ranks, or touches ALIS.

_MEDAL_BAND = [("90+", "\U0001F48E"), ("80-89", "\U0001F947"), ("70-79", "\U0001F948"),
               ("60-69", "\U0001F949"), ("Below 60", "⚠️")]


def _score_health_lines_v3(sb, sc_err):
    L = ["\U0001F3C6 APEX SCORE HEALTH"]
    if sc_err:
        L.append(f"  Unavailable: {sc_err}")
        return L
    for i, (name, medal) in enumerate(_MEDAL_BAND):
        b = sb.get(name, {"w": 0, "l": 0})
        n = b["w"] + b["l"]
        L.append(f"  {medal} {name}")
        if n == 0:
            L.append("    No settled picks yet")
        else:
            L.append(f"    Record: {b['w']}-{b['l']}")
            L.append(f"    Win %: {b['w']/n:.1%}")
            L.append(f"    Sample Size: {n}")
        if i < len(_MEDAL_BAND) - 1:
            L.append("  " + "-" * 40)
    return L


def _takeaway_lines_v3(sb, sc_err):
    """APEX-BETTINGRULE-2026-10-05 (Phase 4, owner): replaces the old
    "MOST IMPORTANT TAKEAWAY" with "APEX BETTING RULE" -- same underlying
    sb dict and bands as APEX SCORE HEALTH above it (no separate
    computation path, so the numbers always reconcile exactly), now
    showing the 90+/80-89/70-79 breakdown inside 70+ Combined AND a
    calculated Historical Edge (70+ win% minus below-70 win%)."""
    L = ["\U0001F3AF APEX BETTING RULE"]
    if sc_err:
        L.append(f"  Unavailable: {sc_err}")
        return L
    above_w = sb["90+"]["w"] + sb["80-89"]["w"] + sb["70-79"]["w"]
    above_l = sb["90+"]["l"] + sb["80-89"]["l"] + sb["70-79"]["l"]
    above_n = above_w + above_l
    below_w = sb["60-69"]["w"] + sb["Below 60"]["w"]
    below_l = sb["60-69"]["l"] + sb["Below 60"]["l"]
    below_n = below_w + below_l
    if above_n < 5 or below_n < 5:
        L.append("  ⚪ Not enough settled picks yet to compare 70+ vs below-70 reliably")
        return L
    above_pct = above_w / above_n
    below_pct = below_w / below_n
    L.append("  ✅ 70+ Combined")
    L.append(f"    Record: {above_w}-{above_l}")
    L.append(f"    Hit %: {above_pct:.1%}")
    L.append(f"    Sample Size: {above_n}")
    for name in ("90+", "80-89", "70-79"):
        b = sb.get(name, {"w": 0, "l": 0})
        n = b["w"] + b["l"]
        if n:
            L.append(f"      - {name}: {b['w']}-{b['l']} ({b['w']/n:.1%}, n={n})")
        else:
            L.append(f"      - {name}: no settled picks yet")
    L.append("  " + "-" * 40)
    L.append("  ⚠️ Below 70 Combined")
    L.append(f"    Record: {below_w}-{below_l}")
    L.append(f"    Hit %: {below_pct:.1%}")
    L.append(f"    Sample Size: {below_n}")
    L.append("  " + "-" * 40)
    edge_pt = (above_pct - below_pct) * 100
    edge_icon = "✅" if edge_pt > 0 else ("⚠️" if edge_pt < 0 else "⚪")
    L.append(f"  {edge_icon} Historical Edge: {edge_pt:+.1f}pt (70+ win% minus below-70 win%)")
    return L


def _board_leaderboard_v3(recs):
    """Same ranking as _board_leaderboard() (win rate, descending) --
    this only changes how each rank is displayed: a medal PLUS that
    board's own PrizePicks-consistent emoji (Objective #4/#1), never the
    order or the win-rate math itself."""
    emoji_map = dict(SCORECARD_ORDER)
    ranked = [(name, recs[name]) for name in _board_names(recs) if recs.get(name, {"n": 0})["n"] > 0]
    ranked.sort(key=lambda nb: -(nb[1]["w"] / nb[1]["n"]))
    L = ["\U0001F3C5 BOARD LEADERBOARD"]
    if not ranked:
        L.append("  No settled board data yet")
        return L
    medals = ["\U0001F947", "\U0001F948", "\U0001F949", "4️⃣", "5️⃣", "6️⃣"]
    for i, (name, b) in enumerate(ranked):
        tag = medals[i] if i < len(medals) else f"{i+1}."
        board_emoji = emoji_map.get(name, "")
        L.append(f"  {tag} {board_emoji} {name} — {b['w']/b['n']:.1%} ({b['w']}-{b['l']})")
        if i < len(ranked) - 1:
            L.append("  " + "-" * 40)
    return L


def _hot_cold_category_lines(mkt):
    """Objective #8: revert the single Best/Worst Performing Category
    sections back to Hot (top 3) / Cold (bottom 3) -- same market_
    performance() ranking V1 used, just more rows shown."""
    L = ["\U0001F525 HOT CATEGORIES"]
    if not mkt:
        L.append("  No category has 5+ settled picks yet")
        L += ["", "\U0001F6AB COLD CATEGORIES", "  No category has 5+ settled picks yet"]
        return L
    top = mkt[:3]
    top_cats = {c for c, _ in top}
    bottom = [(c, b) for c, b in reversed(mkt) if c not in top_cats][:3]
    L += [f"  {_cat_rec(c, b)}" for c, b in top]
    L += ["", "\U0001F6AB COLD CATEGORIES"]
    L += ([f"  {_cat_rec(c, b)}" for c, b in bottom]
          if bottom else ["  n/a — not enough distinct categories yet"])
    return L


def _held_back_bucket(reason):
    """Classifies a held_back reason string into one of the 6 buckets
    Objective #6 asks for. Mirrors discord_bot.py's _held_back_bucket()
    exactly so the two reports' counts always agree -- this is pure
    text classification over the SAME reason strings apex_edge_tracker
    already attaches to each held_back entry, never a new judgment."""
    r = str(reason or "").lower()
    if "category cap" in r:
        return "Category Caps"
    if "player already has a slot" in r or ("duplicate" in r and "player" in r):
        return "Duplicate Player"
    if ("match already has a slot" in r or "same match as a pick" in r
            or ("duplicate" in r and "match" in r)):
        return "Duplicate Match"
    if "quality floor" in r:
        return "Quality Floor"
    if "break-even bar" in r or "evidence below" in r:
        return "Evidence Threshold"
    return "Other"


def _held_back_breakdown_lines(all_held):
    buckets = ["Category Caps", "Duplicate Player", "Duplicate Match",
               "Quality Floor", "Evidence Threshold", "Other"]
    counts = {b: 0 for b in buckets}
    for h in (all_held or []):
        counts[_held_back_bucket(h.get("reason"))] += 1
    L = ["\U0001F6A7 HELD-BACK BREAKDOWN"]
    L += [f"  {b}: {counts[b]}" for b in buckets]
    return L


def _top_held_back_picks_lines(all_held):
    L = ["\U0001F3C6 TOP HELD-BACK PICKS"]
    all_held = all_held or []
    if not all_held:
        L.append("  No held-back props on the current board")
        return L
    top5 = sorted(all_held, key=lambda h: -(h["score"] if h["score"] is not None else -1))[:5]
    for i, h in enumerate(top5):
        L.append(f"  \U0001F4CD Rank #{h['rank'] if h['rank'] is not None else '?'}")
        L.append(f"  \U0001F3BE {_held_back_name(h['p'])}")
        L.append(f"  \U0001F4C8 Apex Score: {round(h['score']) if h['score'] is not None else '?'}")
        L.append(f"  \U0001F512 Held: {h['reason']} ({h['board']})")
        if i < len(top5) - 1:
            L.append("  " + "-" * 40)
    return L


def _demon_floor_value():
    try:
        sys.path.insert(0, BASE)
        import apex_edge_tracker as aet
        return aet.QUALITY_FLOOR_BY_TYPE.get("demon", 60)
    except Exception:
        return 60


def _demon_watchlist_lines(all_held):
    """APEX-UPR-V3-2026-10-01 (Objective #7): ALL Demon props currently
    below the Demon quality floor, highest Apex Score first, never
    truncated -- distinct from the 10-point 'near miss' watchlist
    best_bets_detail() shows inline; this is the complete below-floor
    Demon list for the report, now with 'Missed by N' on every row."""
    L = ["\U0001F440 DEMON WATCHLIST"]
    floor = _demon_floor_value()
    misses = [h for h in (all_held or [])
              if h["board"] == "Demon" and "quality floor" in (h["reason"] or "")]
    if not misses:
        L.append("  No Demon props currently below the floor")
        return L
    misses.sort(key=lambda h: -(h["score"] if h["score"] is not None else -1))
    for i, h in enumerate(misses):
        score = h["score"]
        missed_by = (floor - score) if score is not None else None
        L.append(f"  \U0001F4CD Rank #{h['rank'] if h['rank'] is not None else '?'}")
        L.append(f"  \U0001F3BE {_held_back_name(h['p'])}")
        L.append(f"  \U0001F4C8 Score {round(score) if score is not None else '?'}")
        L.append(f"  ⚠️ Missed by {round(missed_by) if missed_by is not None else '?'}")
        if i < len(misses) - 1:
            L.append("  " + "-" * 40)
    return L


def user_performance_report_text(ledger, A, zone, cycle_date, prev):
    sb, _cb, sc_err = score_confidence_bands(ledger)
    L = [f"\U0001F9E0 USER PERFORMANCE REPORT — {cycle_date}", "=" * 50, ""]

    # 1. BOARD SCORECARD -- Objective #3: first major section
    sc_lines, windows = _scorecard_lines(ledger, cycle_date)
    L += sc_lines
    L += ["", "=" * 50, ""]

    # 2. APEX SCORE HEALTH -- Objective #2: medal-tier bands, expanded
    L += _score_health_lines_v3(sb, sc_err)
    L += ["", "=" * 50, ""]

    # 3. APEX BETTING RULE -- Phase 4 (owner): 70+ Combined (with
    # 90+/80-89/70-79 breakdown) vs Below-70 Combined vs Historical Edge
    L += _takeaway_lines_v3(sb, sc_err)
    L += ["", "=" * 50, ""]

    # 4. BOARD LEADERBOARD -- Objective #4: medal + board emoji, ranked on
    # the steadier 7-day sample, same window V1/V2 used.
    recs7 = windows[1][1] if len(windows) > 1 else {}
    L += _board_leaderboard_v3(recs7)
    L += ["", "=" * 50, ""]

    # 5. HOT / COLD CATEGORIES -- Objective #8
    mkt = market_performance(A)
    L += _hot_cold_category_lines(mkt)
    L += ["", "=" * 50, ""]

    # 6. HELD BACK SUMMARY + BREAKDOWN + TOP HELD-BACK PICKS -- Objective #6
    all_held, hb_err = _collect_held_back()
    L.append("\U0001F440 HELD BACK SUMMARY")
    if hb_err:
        L.append(f"  Unavailable: {hb_err}")
    else:
        L.append(f"  Held Back Count: {len(all_held or [])}")
    L += ["", "=" * 50, ""]
    if not hb_err:
        L += _held_back_breakdown_lines(all_held)
        L += ["", "=" * 50, ""]
        L += _top_held_back_picks_lines(all_held)
        L += ["", "=" * 50, ""]

    # 7. DEMON WATCHLIST -- Objective #7: complete, never truncated, now
    # with "Missed by N" on every row.
    L += _demon_watchlist_lines(all_held if not hb_err else [])
    L += ["", "=" * 50, ""]

    # 8. WHAT APEX LEARNED TODAY -- unchanged content/logic (Objective #9
    # already satisfied: max 5 bullets, plain English, via
    # apex_action_items()).
    deltas = changes_from_yesterday(A, prev)
    L.append("\U0001F9E0 What Apex Learned Today:")
    L += [f"  {item}" for item in apex_action_items(A, zone, sb, deltas)[:5]]
    L.append(_sellable_note())   # APEX-LEARNCARD-2026-11-09
    return "\n".join(L)


def build_user_performance_report():
    """No-argument, fail-open entry point for discord_bot.py's /fetch
    handlers: builds the ledger/analysis this report needs on demand (the
    nightly cycle normally does this once a day; /fetch needs it
    on-demand too) and renders user_performance_report_text(). Never
    raises -- a failure here must never block or break a /fetch run."""
    try:
        ensure_dirs()
        cycle_date = today()
        ledger, _missing = build_ledger()
        if not ledger:
            return "🧠 **USER PERFORMANCE REPORT**\nNo settled picks found yet -- nothing to report."
        A = analyze(ledger)
        zone = zone_verdict(A)
        prev = load_prev_snapshot()
        return user_performance_report_text(ledger, A, zone, cycle_date, prev)
    except Exception as e:
        return f"🧠 **USER PERFORMANCE REPORT**\n⚠️ Unavailable this run ({type(e).__name__}: {e})"

# ---------------------------------------------------------------- commands
def cmd_inspect():
    for label, path in LOGS.items():
        if label == "forward":
            path = discover_forward_log()
        print(f"\n== {label}: {path}")
        if not os.path.exists(path):
            print("   MISSING"); continue
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            header = next(csv.reader(f))
        cols = resolve_columns(header)
        print(f"   header: {header}")
        print(f"   mapped: {cols}")
        miss = [r for r in REQUIRED if r not in cols]
        can_settle = ("result" in cols) or ("actual" in cols and "direction" in cols)
        if not can_settle:
            miss = miss + ["result-or-(actual+direction)"]
        tag = "none — usable" if not miss else str(miss)
        if label == "picks" and not can_settle:
            tag += " (expected — picks log is intake, not settlement; skipped by design)"
        print(f"   REQUIRED missing: {tag}")

def cmd_cycle(post=False):
    ensure_dirs()
    cycle_date = today()
    ledger, missing = build_ledger()
    for label, miss, cols in missing:
        print(f"⚠️  {label} log skipped — unmapped required columns {miss}. "
              f"Run --inspect-logs and report the header.")
    if not ledger:
        print("No settled rows found in any usable log. Nothing to learn from yet.")
        return 1
    A = analyze(ledger)
    prev = load_prev_snapshot()
    zone = zone_verdict(A)
    recs = maybe_recommend(A, zone, cycle_date)
    new_lessons = seed_or_update_lessons(A, cycle_date)
    new_misses = write_major_misses(A, cycle_date)
    write_edge_history(zone, A, cycle_date)
    rep = report_text(A, zone, cycle_date, prev)
    fin = findings_text(A, zone, cycle_date, prev)
    with open(os.path.join(ALIS, "reports", f"{cycle_date}_cycle.md"), "w", encoding="utf-8") as f:
        f.write(rep)
    with open(os.path.join(ALIS, "findings", f"{cycle_date}_findings.md"), "w", encoding="utf-8") as f:
        f.write(fin)
    # APEX-DASH-2026-09-30: redesigned nightly dashboard (reporting only --
    # see score_confidence_bands()/nightly_dashboard_text() above). `prev`
    # was already loaded above, BEFORE this cycle's snapshot is written,
    # so it is genuinely yesterday's data for the Changes-From-Yesterday
    # section.
    sb_snap, cb_snap, _sc_err = score_confidence_bands(ledger)
    dash = nightly_dashboard_text(ledger, A, zone, cycle_date, prev)
    snap = {"date": cycle_date, "by_cat": A["by_cat"], "by_group": A["by_group"],
            "by_att": A["by_att"], "acc": A["acc"],
            "score_bands": sb_snap, "conf_bands": cb_snap,
            "pending": pending_count()}
    with open(os.path.join(ALIS, "category_intelligence", f"{cycle_date}.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1)
    with open(os.path.join(ALIS, "dashboards", "latest.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1)
    msg = discord_text(A, zone, cycle_date, new_lessons, new_misses, recs)
    with open(os.path.join(ALIS, "discord_summaries", f"{cycle_date}.txt"), "w", encoding="utf-8") as f:
        f.write(msg)
    with open(os.path.join(ALIS, "discord_summaries", f"{cycle_date}_dashboard.txt"), "w", encoding="utf-8") as f:
        f.write(dash)
    print(rep)
    print("—" * 40)
    print(dash)
    if post:
        try:
            sys.path.insert(0, BASE)
            import openclaw_core as core  # messaging only — no prediction logic used
            core.send_discord("alis", dash)   # APEX-DASH-2026-09-30: dashboard replaces the old results-dump post
            print("[posted to Discord]")
        except Exception as e:
            print(f"[Discord post unavailable ({e}); summary saved to discord_summaries/]")
    return 0

def cmd_rec(rid, decision, reason):
    path = os.path.join(ALIS, "recommendations", f"{rid}.md")
    idx = os.path.join(ALIS, "recommendations", "REC_INDEX.md")
    if not os.path.exists(path):
        print(f"{rid} not found"); return 1
    if decision not in ("accepted", "rejected"):
        print("decision must be accepted|rejected"); return 1
    append_only(path, f"\n{now_iso()} — {decision.upper()}: {reason}\n")
    append_only(idx, f"{rid} | {today()} | ruling | {decision.upper()}\n")
    append_only(os.path.join(ALIS, "change_log", f"{today()}_changes.md"),
                f"\n{now_iso()} — {rid} {decision.upper()} by operator: {reason}\n",
                header=f"# CHANGE LOG {today()}\n")
    print(f"{rid} → {decision.upper()} (permanent record written)")
    return 0

# ---------------------------------------------------------------- selftest
def cmd_selftest():
    global BASE, ALIS, LOGS
    old = (BASE, ALIS, dict(LOGS))
    tmp = tempfile.mkdtemp(prefix="alis_st_")
    try:
        BASE = tmp; ALIS = os.path.join(tmp, "apex_learning_system")
        LOGS = {k: os.path.join(tmp, f"{k}.csv") for k in LOGS}
        # Hand-CSV contract BO5-FWD-001 (best_of added 2026-08-29)
        hdr = ("match_date,player,opponent,category,tier,line,direction,"
               "prob,actual,best_of,in_forward_log,source,note\n")
        rows = [
            "2026-08-24,P1,O1,double_faults,A,1.5,MORE,0.70,7,3,yes,sack,",
            "2026-08-24,P2,O2,double_faults,A,3.5,MORE,0.55,11,,yes,sack,",
            "2026-08-28,P3,O3,double_faults,A,2.5,MORE,0.57,1,3,yes,hand,",
            "2026-08-28,P4,O4,aces,A,8.5,LESS,0.61,7,3,yes,hand,",
            "2026-08-28,P5,O5,fantasy_score,B,11.0,MORE,0.55,6.0,3,yes,hand,",
            "2026-08-28,P6,O6,total_games,B,22.0,MORE,0.56,22,3,yes,hand,",
            "2026-08-28,P7,O7,games_won,B,12.5,LESS,0.53,18,3,yes,hand,",
            "2026-08-28,P8,O8,aces,A,7.5,MORE,0.86,0,3,yes,hand,",
            "2026-08-28,P9,O9,total_games,B,22.0,MORE,0.53,,3,yes,hand,retired at 1 set all",
            "2026-08-29,P10,O10,aces,A,12.5,MORE,0.60,15,5,yes,hand,",
        ]
        with open(LOGS["hand"], "w", encoding="utf-8") as f:
            f.write(hdr + "\n".join(rows) + "\n")
        ok = cmd_cycle(post=False) == 0
        assert ok, "cycle failed"
        led, _ = build_ledger()
        res = {r["player"]: r["result"] for r in led}
        assert res["P1"] == "WIN" and res["P8"] == "LOSS", "result derivation wrong"
        assert res["P6"] == "PUSH", "push derivation wrong"
        p9 = [r for r in led if r["player"] == "P9"][0]
        assert res["P9"] == "VOID" and p9["void_reason"], "note-VOID derivation wrong"
        # ---- BO5-FWD-001 checks
        p10 = [r for r in led if r["player"] == "P10"][0]
        assert p10["best_of"] == 5 and p10["bo5"], "Bo5 row not stamped bo5"
        assert p10["attention"] == "LOW", "Bo5 pick must be forced LOW"
        p2 = [r for r in led if r["player"] == "P2"][0]
        assert p2["best_of"] == 3 and not p2["bo5"], "legacy blank best_of should default to Bo3"
        A_chk = analyze(led)
        assert A_chk["bo5"]["n"] == 1 and A_chk["bo5"]["w"] == 1, "Bo5 stream stats wrong"
        assert A_chk["by_cat"]["aces"]["n"] == 2, "Bo5 row leaked into Bo3 category stats"
        _good = open(LOGS["hand"], encoding="utf-8").read()
        with open(LOGS["hand"], "w", encoding="utf-8") as f:
            f.write(hdr + "2026-08-29,PX,OX,aces,A,5.5,MORE,0.60,7,,yes,hand,\n")
        try:
            build_ledger()
            raise AssertionError("new-dated hand row without best_of was accepted")
        except RefuseError:
            pass
        with open(LOGS["hand"], "w", encoding="utf-8") as f:
            f.write(_good)
        with open(LOGS["hand"], "a", encoding="utf-8") as f:
            f.write("2026-08-27,P10,O10,aces,A,12.5,MORE,0.58,10,3,yes,hand,\n")
        led_dd, _ = build_ledger()
        assert sum(1 for r in led_dd if r["player"] == "P10") == 2, "date-aware dedupe lost a row"
        with open(LOGS["hand"], "w", encoding="utf-8") as f:
            f.write(_good)
        mm = open(os.path.join(ALIS, "major_misses.md"), encoding="utf-8").read()
        assert "Extreme Miss" in mm and "P8" in mm, "planted Extreme miss not archived"
        rep = open(os.path.join(ALIS, "reports", f"{today()}_cycle.md"), encoding="utf-8").read()
        assert "1 void" in rep or "(1 void)" in rep, "void not counted separately"
        assert "PROP-BASED" in rep, "prop-based label missing"
        assert "BO5 FORWARD TEST" in rep, "BO5 section missing from cycle report"
        assert "none cleared the evidence gate" in rep, "pattern gate leaked at small n"
        eh = open(os.path.join(ALIS, "edge_history.md"), encoding="utf-8").read()
        assert "founding entry" in eh, "edge_history founding entry missing"
        ll = open(os.path.join(ALIS, "lessons_learned.md"), encoding="utf-8").read()
        assert "LESSON-001" in ll, "no lesson seeded"
        # append-only: second cycle must not shrink registries
        n_ll, n_mm = len(ll), len(mm)
        cmd_cycle(post=False)
        assert len(open(os.path.join(ALIS, "lessons_learned.md"), encoding="utf-8").read()) >= n_ll
        assert len(open(os.path.join(ALIS, "major_misses.md"), encoding="utf-8").read()) >= n_mm
        # edge_history event-driven: unchanged state adds nothing
        eh2 = open(os.path.join(ALIS, "edge_history.md"), encoding="utf-8").read()
        assert eh2.count("STATE:") == 1, "edge_history duplicated an unchanged state"
        # VOID without reason must refuse (explicit-result log shape)
        with open(LOGS["forward"], "w", encoding="utf-8") as f:
            f.write("match_date,player,opponent,category,line,direction,prob,result,void_reason\n")
            f.write("2026-08-28,P10,O10,aces,5.5,MORE,0.60,VOID,\n")
        refused = False
        try:
            cmd_cycle(post=False)
        except RefuseError:
            refused = True
        assert refused, "VOID without reason was NOT refused"
        print("\nSELFTEST PASS — 12/12 assertions")
        return 0
    finally:
        BASE, ALIS, LOGS = old
        shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- main
def main(argv):
    if "--selftest" in argv: return cmd_selftest()
    if "--inspect-logs" in argv: cmd_inspect(); return 0
    if "--rec" in argv:
        i = argv.index("--rec")
        return cmd_rec(argv[i+1], argv[i+2], argv[i+3] if len(argv) > i+3 else "")
    if "--cycle" in argv: return cmd_cycle(post="--post" in argv)
    print(__doc__); return 0

if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except RefuseError as e:
        print(f"REFUSED: {e}"); sys.exit(2)
