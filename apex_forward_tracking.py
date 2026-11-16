"""
APEX-FORWARD-TRACKING-2026-09-27
Forward-only, read-mostly tracker. Writes ONLY to its own new ledger file
(apex_forward_tracking_log_v1.json) -- never touches apex_prop_sim_v1.py,
apex_prop_model_v1.py, apex_prop_engine.py, apex_engine_picks_log_v1.csv,
apex_prop_sim_forward_log_v1.csv, slip_log.json, or any other production
data/ledger file.

Uses the exact live, unmodified apex_edge_tracker scoring/ranking/slip
functions (apex_score, apex_confidence, rank_score, _bb_sortkey,
_demote_bo5_from_top, slip_builder, records) -- same functions production
uses right now to render the real board. Nothing here is re-implemented.

START_DATE is the first calendar day this tracker will ever record. Per
user directive ("start tracking from today forward"), days before it are
permanently out of scope for this ledger -- the earlier dispersion and
actual-strategy backtests already cover history. A day's board is captured
EXACTLY ONCE, the first time this script sees rows for that match_date, at
whatever category record (et.records()) is live at that moment -- this
mirrors what the real board would have shown that day. It is never
recaptured or revised later even if more matches for that date get added
to the picks log after the fact (that would be hindsight).

Usage:
  python3 apex_forward_tracking.py --capture   # log any new days' boards
  python3 apex_forward_tracking.py --settle    # grade any now-settled legs
  python3 apex_forward_tracking.py --report    # print the running summary
  python3 apex_forward_tracking.py             # does all three, in order
"""
import os, sys, json, argparse
import pandas as pd, numpy as np
from collections import defaultdict
from datetime import date

HERE = "/path/to/home/openclaw"
sys.path.insert(0, HERE)
os.chdir(HERE)
import apex_edge_tracker as et

START_DATE = "2026-09-27"   # first day in scope -- never capture before this
LEDGER_PATH = os.path.join(HERE, "apex_forward_tracking_log_v1.json")
REPORT_PATH = os.path.join(HERE, "apex_forward_tracking_report_v1.json")
LOG_PATH = os.path.join(HERE, "apex_forward_tracking.log")
STAKE = 100.0
PAYOUT3 = {"standard": {3: 3.0, 2: 1.0, 1: 0.0, 0: 0.0},
           "goblin":   {3: 2.5, 2: 0.5, 1: 0.0, 0: 0.0}}

def log(msg):
    with open(LOG_PATH, "a") as f:
        f.write("%s %s\n" % (pd.Timestamp.now().isoformat(), msg))
    print(msg)

def load_ledger():
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH) as f:
            return json.load(f)
    return {}

def save_ledger(ledger):
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2, default=str)
    os.replace(tmp, LEDGER_PATH)   # atomic -- never leaves a half-written ledger

def load_picks():
    df = pd.read_csv(os.path.join(HERE, "apex_engine_picks_log_v1.csv"), low_memory=False)
    df = df[df["category"].notna() & (df["category"] != "category")].copy()
    df["line"] = pd.to_numeric(df["line"], errors="coerce")
    df = df.sort_values("ts").drop_duplicates(
        subset=["match_id", "player", "category", "direction", "line"], keep="last")
    return df

def load_sim():
    return pd.read_csv(os.path.join(HERE, "apex_prop_sim_forward_log_v1.csv"), low_memory=False)

def wta_flag(row):
    s = (str(row.get("format_label", "")) + " " + str(row.get("tournament", ""))).lower()
    return ("women" in s) or ("wta" in s)

def build_d(row, REC):
    prob = float(row["prob_cal"]) if pd.notna(row.get("prob_cal")) else float(row["prob"])
    cat = row["category"]
    bo5 = bool(int(row["best_of"]) == 5) if pd.notna(row.get("best_of")) else False
    sc, ev, lvl, up, down = et.apex_score(prob, cat, bo5, REC)
    conf, _ctier, cwhy = et.apex_confidence(cat, bo5, REC, warnings=down, card_present=False)
    rank = et.rank_score(sc, conf)
    p = {"player": row["player"], "opponent": row["opponent"], "category": cat,
         "direction": row["direction"], "line": row["line"], "prob": prob,
         "edge": row.get("edge"), "match_id": row["match_id"],
         "prop_type": (str(row["prop_type"]).lower() if pd.notna(row.get("prop_type")) else ""),
         "bo5": bo5}
    return {"p": p, "cat": cat, "score": sc, "conf": conf, "rank": rank, "bo5": bo5}

def dedupe_one_per_player(scored):
    seen, prim = {}, []
    for d in scored:
        pk = (d["p"].get("player") or "") + "|" + (d["p"].get("opponent") or "")
        if pk in seen:
            continue
        seen[pk] = d; prim.append(d)
    return prim

def build_day_board(day_rows, REC):
    scored = [build_d(r, REC) for _, r in day_rows.iterrows()]
    scored.sort(key=lambda d: et._bb_sortkey(d))
    scored = dedupe_one_per_player(scored)
    scored = et._demote_bo5_from_top(scored)
    return scored

def top3(scored, kind):
    if kind == "mixed":
        return [d for d in scored if not d["bo5"]][:3]
    sm = et.slip_builder(scored, kind)
    slotted = sorted((v[0], d) for d in scored if (v := sm.get(id(d))) and v[0])
    return [d for slot, d in slotted if slot <= 3]

def leg_dict(d):
    return {"player": d["p"]["player"], "opponent": d["p"]["opponent"], "category": d["cat"],
            "direction": d["p"]["direction"], "line": d["p"]["line"],
            "prob": round(d["p"]["prob"], 4), "match_id": d["p"]["match_id"],
            "score": d["score"], "conf": d["conf"], "rank": d["rank"], "actual": None, "won": None}

def do_capture():
    ledger = load_ledger()
    picks = load_picks()
    picks = picks[picks["match_date"] >= START_DATE]
    days = sorted(picks["match_date"].unique())
    new_days = [d for d in days if d not in ledger]
    if not new_days:
        log("capture: no new days to capture (already have %s; latest picks-log day = %s)" %
            (sorted(ledger.keys()), days[-1] if days else "none"))
        return
    REC = et.records()
    for day in new_days:
        day_rows = picks[picks["match_date"] == day]
        board = build_day_board(day_rows, REC)
        entry = {"captured_ts": pd.Timestamp.now().isoformat(), "rec_overall_n": REC["overall"]["n"],
                  "n_board": len(board)}
        for kind in ("mixed", "standard", "goblin"):
            legs = top3(board, kind)
            entry[kind] = {
                "legs": [leg_dict(d) for d in legs],
                "status": "PENDING" if len(legs) == 3 else "INCOMPLETE",
                "result": None,
            }
        ledger[day] = entry
        log("captured day %s: board=%d mixed=%s(%d legs) standard=%s(%d legs) goblin=%s(%d legs)" % (
            day, len(board),
            entry["mixed"]["status"], len(entry["mixed"]["legs"]),
            entry["standard"]["status"], len(entry["standard"]["legs"]),
            entry["goblin"]["status"], len(entry["goblin"]["legs"])))
    save_ledger(ledger)

def grade_leg(leg, sim):
    m = sim[(sim["match_id"] == leg["match_id"]) & (sim["player"] == leg["player"]) &
            (sim["category"] == leg["category"])]
    if m.empty or pd.isna(m.iloc[0].get("actual")):
        return None, None   # not settled yet
    actual = float(m.iloc[0]["actual"]); line = float(leg["line"])
    direction = str(leg["direction"]).upper()
    if actual == line:
        return actual, "PUSH"   # true push -- flagged, never auto-resolved as a win/loss
    won = (actual > line) if direction == "MORE" else (actual < line)
    return actual, won

def do_settle():
    ledger = load_ledger()
    sim = load_sim()
    changed = False
    for day, entry in ledger.items():
        for kind in ("mixed", "standard", "goblin"):
            sub = entry[kind]
            if sub["status"] != "PENDING":
                continue
            actuals = [grade_leg(leg, sim) for leg in sub["legs"]]
            if any(a is None and w is None for a, w in actuals):
                continue   # at least one leg still unsettled
            if any(w == "PUSH" for _, w in actuals):
                sub["status"] = "NEEDS_REVIEW_PUSH"
                changed = True
                log("day %s %s: a leg pushed (actual == line) -- needs manual review, not auto-graded" % (day, kind))
                continue
            for leg, (actual, won) in zip(sub["legs"], actuals):
                leg["actual"] = actual; leg["won"] = bool(won)
            hits = sum(1 for _, w in actuals if w)
            mult = PAYOUT3[kind if kind != "mixed" else "standard"][hits]
            pnl = STAKE * (mult - 1.0)
            sub["result"] = {"hits": hits, "mult": mult, "pnl": pnl}
            sub["status"] = "SETTLED"
            changed = True
            log("SETTLED day %s %s: %d/3 hit, pnl=%.2f" % (day, kind, hits, pnl))
    if changed:
        save_ledger(ledger)
    else:
        log("settle: nothing newly settleable")

def iso_week(d):
    y, w, _ = pd.Timestamp(d).isocalendar()
    return "%d-W%02d" % (y, w)

def streaks(pnls):
    """Returns (longest_losing, longest_winning, current_streak_signed)."""
    longest_l = longest_w = cur_l = cur_w = 0
    for p in pnls:
        if p > 0:
            cur_w += 1; cur_l = 0; longest_w = max(longest_w, cur_w)
        else:
            cur_l += 1; cur_w = 0; longest_l = max(longest_l, cur_l)
    current = cur_w if cur_w > 0 else -cur_l
    return longest_l, longest_w, current

def drawdown(pnls):
    cum = peak = maxdd = 0.0
    for p in pnls:
        cum += p; peak = max(peak, cum); maxdd = min(maxdd, cum - peak)
    return round(maxdd, 2), round(cum, 2)

def kind_summary(ledger, kind):
    days = sorted(d for d in ledger if ledger[d][kind]["status"] == "SETTLED")
    if not days:
        return {"n_slips": 0, "days": []}
    pnls = [ledger[d][kind]["result"]["pnl"] for d in days]
    hits = [ledger[d][kind]["result"]["hits"] for d in days]
    wins = sum(1 for p in pnls if p > 0)
    longest_l, longest_w, current = streaks(pnls)
    maxdd, cum = drawdown(pnls)
    weekly = defaultdict(lambda: {"trading_days": 0, "winning_days": 0})
    for d, p in zip(days, pnls):
        wk = iso_week(d)
        weekly[wk]["trading_days"] += 1
        if p > 0:
            weekly[wk]["winning_days"] += 1
    return {"n_slips": len(days), "win_rate": round(wins / len(days), 4),
            "loss_rate": round(1 - wins / len(days), 4), "avg_legs_won": round(np.mean(hits), 3),
            "profit_loss": round(sum(pnls), 2), "roi": round(sum(pnls) / (len(days) * STAKE), 4),
            "longest_losing_streak": longest_l, "longest_winning_streak": longest_w,
            "current_streak": current, "max_drawdown": maxdd, "ending_cum_pnl": cum,
            "weekly": dict(sorted(weekly.items())), "days": days}

def do_report():
    ledger = load_ledger()
    summary = {kind: kind_summary(ledger, kind) for kind in ("mixed", "standard", "goblin")}
    pending = {d: {k: ledger[d][k]["status"] for k in ("mixed", "standard", "goblin")} for d in sorted(ledger)}
    out = {"generated": pd.Timestamp.now().isoformat(), "start_date": START_DATE,
           "n_days_captured": len(ledger), "summary": summary, "per_day_status": pending}
    with open(REPORT_PATH, "w") as f:
        json.dump(out, f, indent=2, default=str)
    for kind in ("mixed", "standard", "goblin"):
        s = summary[kind]
        log("REPORT %-8s: %s" % (kind, s))
    log("wrote %s" % REPORT_PATH)
    return out

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", action="store_true")
    ap.add_argument("--settle", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    run_all = not (args.capture or args.settle or args.report)
    log("=== forward tracking run start ===")
    if args.capture or run_all:
        do_capture()
    if args.settle or run_all:
        do_settle()
    if args.report or run_all:
        do_report()
    log("=== forward tracking run end ===")
