#!/usr/bin/env python3
"""
APEX-ALERTS-2026-10-11 (owner, 2026-10-11): OpenClaw's Discord
notification system. Not a dashboard -- it posts ONLY when something new needs
attention or a decision, and remembers what it already said
(data/logs/alert_state.json) so nothing repeats.

Two modes:
  --check   every 30 min (LaunchAgent com.apex.alerts) -> #alerts
            🚨 Board Integrity  (a board-worthy prop lost: not shown with no
               rule explaining it, or dropped while PrizePicks still offers it)
            ⚙️ System Health    (5 PM run failed, settlement health FAIL, a fetch
               that never got published, stale board)
            (scheduler heartbeat / missed 5 PM run stay with apex_ops_watchdog.py)
  --daily   after the morning settlements (called by the 9 AM report) -> #system
            📈/📉 New Engine, 🎯 Top 3 Goal, 🧪 Apex Score, 📊 Honest Probability,
            🏆 Category, ✅/❌ shadow tests -- each only when its status CHANGES --
            then one short OPENCLAW DAILY STATUS.
  --dry-run with either mode: print instead of posting, don't save state.

Every judgement uses LIVE SETTLED results of the new engine (boards published
from apex_engine_validation.NEW_ENGINE_START), never replays.
"""
import csv, json, os, sys, collections
from datetime import datetime, timedelta, timezone
import apex_ops as ops
import apex_engine_validation as ev

HERE = ops.HERE
STATE = os.path.join(ops.LOG_DIR, "alert_state.json")
DRY = "--dry-run" in sys.argv

# evidence thresholds (kept in one place so they're easy to read and change)
MIN_BOARD_LEGS = 30          # new-engine legs on a board before comparing engines
ENGINE_MARGIN = 0.02         # 2-point margin to call "better" / "worse"
MIN_GOAL_DAYS = 7            # complete Top 3 days before a pace call
MIN_APEX_PROPS = 100         # settled 70%+ props (blocked + passed) before an Apex Score call
APEX_MARGIN = 0.03
HONEST_MIN = 100             # settled Honest-70%+ props
HONEST_PASS = 0.72
CAT_MIN = 30                 # settled 70%+ props per category
BREAKS_MIN = 50
STANDARD_BREAK_EVEN = 0.542  # apex_edge_tracker.BREAK_EVEN
INTEGRITY_MIN_PROB = 0.70
# APEX-CHECKS5-2026-10-12 (owner, "All 5"): Best boards checked too
INTEGRITY_BOARDS = ("top3", "elite6", "top10", "bestgoblin", "beststandard", "bestdemon")
SLIP_STUCK_HOURS = 48
# APEX-TESTDONE-2026-10-13: dated reminders for studies that need more data first
REMINDERS = [
    ("percat_study", "2026-11-23", [("Study", "Per-board / per-category Win Probability cut-offs"),
                                    ("Why now", "about 60 days of settled results (the 10/13 run had 18 days; per-category cut-offs didn't hold up)"),
                                    ("Action", "ask Claude to rerun the per-board cut-off study")]),
]


def _load_state():
    return ops._read_json(STATE, {})


def _save_state(s):
    if not DRY:
        ops._write_json(STATE, s)


def _send(channel, text):
    if DRY:
        print("---- would post to #%s ----\n%s" % (channel, text))
        return True
    return ops.send_discord(channel, text)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pct(w, n):
    return ("%.1f%% (%d-%d)" % (100.0 * w / n, w, n - w)) if n else "no results"


def _who(r):
    return r["opponent"] if r.get("player") == "match" else "%s vs %s" % (r.get("player"), r.get("opponent"))


def _prop(r):
    return "%s -- %s %s %s" % (_who(r), r.get("category"), r.get("line"), r.get("direction"))


# ----------------------------------------------------------------------------- evidence
def gather():
    t, rep, S, status = ev.load()
    props = ev.new_engine_props(status, S, t)
    playable = [p for p in props if p["reason"] != "unplayable direction (MORE-only line, engine says LESS)"]
    out = {"t": t, "S": S, "status": status, "props": playable}

    # engines, per board
    boards = {}
    for b, lab in ev.BOARDS:
        new = ev.board_days(status, S, b, "new")
        old = ev.legacy_days(S, b)
        for d, v in ev.board_days(status, S, b, "old").items():
            old[d] = v
        nh = [h for items in new.values() for _, _, h in items if h is not None]
        oh = [h for items in old.values() for _, _, h in items if h is not None]
        boards[b] = {"label": lab, "new_w": sum(nh), "new_n": len(nh), "old_w": sum(oh), "old_n": len(oh),
                     "new_days": new, "old_days": old}
    out["boards"] = boards
    out["top3_new"] = ev.slip_stats(boards["top3"]["new_days"])
    out["top3_old"] = ev.slip_stats(boards["top3"]["old_days"])

    # engine verdict
    t10 = boards["top10"]
    if t10["new_n"] < MIN_BOARD_LEGS:
        eng = "insufficient"
    else:
        diffs = []
        for b in ("top3", "elite6", "top10"):
            x = boards[b]
            if x["new_n"] >= 15 and x["old_n"]:
                diffs.append(x["new_w"] / x["new_n"] - x["old_w"] / x["old_n"])
        better = sum(1 for d in diffs if d > ENGINE_MARGIN)
        worse = (t10["new_w"] / t10["new_n"]) < (t10["old_w"] / t10["old_n"]) - ENGINE_MARGIN
        eng = "worse" if worse else ("better" if better >= 2 else "even")
    out["engine"] = eng

    # Top 3 goal pace
    st = out["top3_new"]
    if st["days"] < MIN_GOAL_DAYS:
        goal = "insufficient"
    else:
        pw = st["per_week"]
        goal = "4+" if pw >= 4 else ("3-4" if pw >= 3 else "below")
    out["goal"] = goal

    # Apex Score
    blocked = [p for p in playable if p["reason"] == "Apex Score under 67" and p["hit"] is not None]
    passed = [p for p in playable if p["status"] == "eligible" and p["hit"] is not None]
    out["apex_blocked"], out["apex_passed"] = blocked, passed
    if len(blocked) + len(passed) < MIN_APEX_PROPS or len(blocked) < 20:
        apex = "insufficient"
    else:
        d = sum(p["hit"] for p in passed) / len(passed) - sum(p["hit"] for p in blocked) / len(blocked)
        apex = "helps" if d > APEX_MARGIN else ("hurts" if d < -APEX_MARGIN else "neutral")
    out["apex"] = apex

    # Honest Probability
    dec = [p for p in playable if p["hit"] is not None and _f(p.get("prob_honest")) is not None]
    h70 = [p for p in dec if _f(p["prob_honest"]) >= 0.70]
    out["honest_n"], out["honest_w"] = len(h70), sum(p["hit"] for p in h70)
    if len(h70) < HONEST_MIN:
        honest = "insufficient"
    else:
        br_raw = sum((_f(p["prob"]) - p["hit"]) ** 2 for p in dec) / len(dec)
        br_hon = sum((_f(p["prob_honest"]) - p["hit"]) ** 2 for p in dec) / len(dec)
        honest = "pass" if (out["honest_w"] / len(h70) >= HONEST_PASS and br_hon < br_raw) else "fail"
    out["honest"] = honest

    # categories (70%+, settled, new engine)
    cats = {}
    for c in ("aces", "total_games", "fantasy_score", "breaks", "dfs", "games_won"):
        xs = [p for p in playable if p["category"] == c and (_f(p["prob"]) or 0) >= 0.70 and p["hit"] is not None]
        w = sum(p["hit"] for p in xs)
        cats[c] = (w, len(xs))
    out["cats"] = cats

    def cat_state(c, ready_min=CAT_MIN):
        w, n = cats[c]
        if n < ready_min:
            return "collecting"
        r = w / n
        return "improving" if r >= 0.72 else ("declining" if r < 0.65 else "steady")
    out["cat_state"] = {"aces": cat_state("aces"), "total_games": cat_state("total_games"),
                        "fantasy_score": cat_state("fantasy_score")}
    bw, bn = cats["breaks"]
    out["cat_state"]["breaks"] = "collecting" if bn < BREAKS_MIN else ("ready" if bw / bn >= 0.70 else "not ready")

    # shadow / watch tests
    std = [p for p in playable if p.get("prop_type") == "standard" and 0.60 <= (_f(p["prob"]) or 0) < 0.70 and p["hit"] is not None]
    sw = sum(p["hit"] for p in std)
    tests = {
        "Honest Probability as qualification gate": {
            "n": len(h70), "need": HONEST_MIN, "w": out["honest_w"],
            "state": {"pass": "promote", "fail": "fail"}.get(honest, "collecting"),
            "rec": "switch the board gate to Honest Probability 70%+" if honest == "pass" else "keep raw probability as the gate"},
        "Remove Apex Score filter": {
            "n": len(blocked) + len(passed), "need": MIN_APEX_PROPS, "w": None,
            "state": {"hurts": "promote", "helps": "fail"}.get(apex, "collecting"),
            "rec": "remove the Apex 67 filter" if apex == "hurts" else "keep the Apex 67 filter"},
        "Bring Breaks back to boards": {
            "n": bn, "need": BREAKS_MIN, "w": bw,
            "state": {"ready": "promote", "not ready": "fail"}.get(out["cat_state"]["breaks"], "collecting"),
            "rec": "return Breaks to the boards" if out["cat_state"]["breaks"] == "ready" else "keep Breaks off the boards"},
        "Fantasy Score on boards (holding up?)": {
            "n": cats["fantasy_score"][1], "need": CAT_MIN, "w": cats["fantasy_score"][0],
            "state": {"declining": "fail", "improving": "promote", "steady": "promote"}.get(out["cat_state"]["fantasy_score"], "collecting"),
            "rec": "keep Fantasy Score on the boards" if out["cat_state"]["fantasy_score"] in ("improving", "steady") else "take Fantasy Score back off the boards"},
        "Standard 60-70% watch list": {
            "n": len(std), "need": 100, "w": sw,
            "state": "collecting" if len(std) < 100 else ("promote" if sw / len(std) >= 0.70 else ("fail" if sw / len(std) < STANDARD_BREAK_EVEN else "collecting")),
            "rec": "add a Standard 60%+ board" if (len(std) >= 100 and sw / len(std) >= 0.70) else "keep Standard at the 70% bar"},
    }
    out["tests"] = tests
    return out


# ----------------------------------------------------------------------------- live validations
OLD_ATP_ACES_BASELINE = (0.655, "65.5%")   # playable ATP Aces picks, old engine, Oct 1-9 (n=119)
VALIDATION_MIN = 50


def _pick_index(t):
    """identity -> list of pick-log rows (oldest first) for tour, tournament and re-score history."""
    idx = collections.defaultdict(list)
    with open(t.PICK_LOG, newline="") as fh:
        for r in csv.DictReader(fh):
            idx[t._apex_prop_identity(r)].append(r)
    return idx


def live_validations(E):
    """Live checks of features that are already ON (opponent aces, indoor, re-pricing)."""
    t, props = E["t"], E["props"]
    idx = _pick_index(t)
    try:
        import apex_surface_map as sm
    except Exception:
        sm = None
    def wta(rows):
        lab = " ".join(str(rows[-1].get(k) or "") for k in ("format_label", "tournament")).lower() if rows else ""
        return "women" in lab or "wta" in lab
    atp_aces, indoor_aces, outdoor_aces = [], [], []
    for p in props:
        if p["category"] != "aces" or p["hit"] is None:
            continue
        rows = idx.get(t._apex_prop_identity(p), [])
        if not rows or wta(rows):
            continue
        atp_aces.append(p)
        ind = None
        if sm:
            try:
                ind = sm.lookup(rows[-1].get("tournament") or "")[1]
            except Exception:
                ind = None
        (indoor_aces if ind is True else outdoor_aces).append(p)
    # re-pricing: settled props whose newest row is a re-score; compare first vs latest probability
    rep = []
    for p in props:
        if p["hit"] is None:
            continue
        rows = idx.get(t._apex_prop_identity(p), [])
        if len(rows) < 2 or not str(rows[-1].get("price_source") or "").startswith(("refresh:", "reprice:")):
            continue
        def pr(r):
            v = _f(r.get("prob_cal")) or _f(r.get("prob"))
            return v if str(r.get("direction")) == str(p.get("direction")) else (1 - v if v is not None else None)
        a, b = pr(rows[0]), pr(rows[-1])
        if a is not None and b is not None:
            rep.append((a, b, 1.0 if p["hit"] else 0.0))
    V = []
    w = sum(p["hit"] for p in atp_aces); n = len(atp_aces)
    if n < VALIDATION_MIN:
        st, verdict = "collecting", ""
    else:
        r = w / n
        st = "ready" if r >= OLD_ATP_ACES_BASELINE[0] + 0.03 else ("failed" if r < OLD_ATP_ACES_BASELINE[0] - 0.03 else "warning")
        verdict = "%.1f%% vs old-engine %s" % (100 * r, OLD_ATP_ACES_BASELINE[1])
    V.append({"name": "ATP Opponent Aces", "unit": "settled ATP Aces", "n": n, "need": VALIDATION_MIN, "state": st,
              "before": OLD_ATP_ACES_BASELINE[1], "after": ("%.1f%%" % (100.0 * w / n)) if n else "--", "verdict": verdict,
              "rec": {"ready": "keep it -- confirmed improvement", "failed": "consider switching it off",
                      "warning": "keep testing -- no clear change yet"}.get(st, "")})
    wi, ni = sum(p["hit"] for p in indoor_aces), len(indoor_aces)
    wo, no = sum(p["hit"] for p in outdoor_aces), len(outdoor_aces)
    if ni < VALIDATION_MIN:
        st, verdict = "collecting", ""
    else:
        ri, ro = wi / ni, (wo / no if no else 0)
        st = "ready" if ri >= ro - 0.02 else ("failed" if ri < ro - 0.06 else "warning")
        verdict = "indoor %.1f%% vs outdoor %.1f%%" % (100 * ri, 100 * ro)
    V.append({"name": "Indoor Adjustment", "unit": "settled indoor ATP Aces", "n": ni, "need": VALIDATION_MIN, "state": st,
              "before": ("%.1f%% outdoor" % (100.0 * wo / no)) if no else "--", "after": ("%.1f%%" % (100.0 * wi / ni)) if ni else "--",
              "verdict": verdict, "rec": {"ready": "keep the indoor factor", "failed": "switch the indoor factor off",
                                         "warning": "keep testing"}.get(st, "")})
    nr = len(rep)
    if nr < VALIDATION_MIN:
        st, verdict = "collecting", ""
        b0 = b1 = None
    else:
        b0 = sum((a - h) ** 2 for a, _, h in rep) / nr
        b1 = sum((b - h) ** 2 for _, b, h in rep) / nr
        st = "ready" if b1 < b0 else ("failed" if b1 > b0 + 0.005 else "warning")
        verdict = "accuracy (Brier) first price %.4f vs re-priced %.4f" % (b0, b1)
    V.append({"name": "Repricing", "unit": "settled re-priced props", "n": nr, "need": VALIDATION_MIN, "state": st,
              "before": ("%.4f" % b0) if b0 is not None else "--", "after": ("%.4f" % b1) if b1 is not None else "--",
              "verdict": verdict, "rec": {"ready": "keep re-pricing", "failed": "switch re-pricing off",
                                         "warning": "keep testing"}.get(st, "")})
    # APEX-STANDARD6X-2026-10-11: Best Standard on its own 6x rule (break-even ~55%/leg)
    bsd = ev.board_days(E["status"], E["S"], "beststandard", "new")
    legs = [h for items in bsd.values() for _, _, h in items if h is not None]
    w = sum(1 for h in legs if h); n = len(legs)
    st = "collecting" if n < VALIDATION_MIN else ("ready" if w / n >= 0.58 else ("failed" if w / n < 0.52 else "warning"))
    V.append({"name": "Best Standard (6x rule)", "unit": "settled Best Standard legs", "n": n, "need": VALIDATION_MIN,
              "state": st, "before": "break-even 55%", "after": ("%.1f%%" % (100.0 * w / n)) if n else "--",
              "verdict": ("%.1f%% per leg vs 55%% break-even" % (100.0 * w / n)) if n else "",
              "rec": {"ready": "keep the Standard rule -- it's beating break-even", "failed": "tighten or switch off the Standard rule",
                      "warning": "keep testing"}.get(st, "")})
    # APEX-BOOKLINES-2026-10-11: live tests of bet365's view (display-only on cards today)
    bl = ev._read(os.path.join(HERE, "apex_book_lines_log_v1.csv"))
    first_bl = {}
    for r in bl:
        k = (r["match_date"], r["opponent"].lower(), r["category"], r["pp_line"], r["direction"], r.get("prop_type"))
        first_bl.setdefault(k, r)
    def _bp_for(row):
        for k, r in first_bl.items():
            if k[0] == row["match_date"] and k[2] == row["category"] and float(k[3]) == float(row["line"]) and k[4] == row.get("direction") \
               and (row["opponent"].lower() == k[1] or row["opponent"].lower() in k[1] or k[1] in row["opponent"].lower()):
                return _f(r["book_side_prob"])
        return None
    t3 = [(r, h) for items in ev.board_days(E["status"], E["S"], "top3", "new").values() for _, r, h in items if h is not None]
    hi, lo = [], []
    for r, h in t3:
        bp = _bp_for(r)
        if bp is None:
            continue
        (hi if bp >= 0.80 else lo).append(h)
    n = len(hi) + len(lo)
    if n < 60 or len(hi) < 20:
        st = "collecting"
    else:
        dh, dl = sum(hi) / len(hi), (sum(lo) / len(lo) if lo else 0)
        st = "ready" if dh >= dl + 0.05 else ("failed" if dh <= dl else "warning")
    V.append({"name": "Top 3 with bet365 80%+", "unit": "settled Top 3 legs with a bet365 line", "n": n, "need": 60, "state": st,
              "before": ("%.1f%% (bet365 under 80%%)" % (100.0 * sum(lo) / len(lo))) if lo else "--",
              "after": ("%.1f%% (bet365 80%%+)" % (100.0 * sum(hi) / len(hi))) if hi else "--",
              "verdict": "bet365 80%%+ legs %s vs others %s" % (("%.1f%%" % (100.0 * sum(hi) / len(hi))) if hi else "--", ("%.1f%%" % (100.0 * sum(lo) / len(lo))) if lo else "--"),
              "rec": {"ready": "require bet365 80%+ for Top 3 legs", "failed": "drop the bet365 filter idea", "warning": "keep testing"}.get(st, "")})
    ag, dg = [], []
    for k, r in first_bl.items():
        if r.get("prop_type") != "standard":
            continue
        m = E["S"].find(r["match_date"], r["player"], r["opponent"], r["category"], r["pp_line"], r.get("direction"))
        bp = _f(r["book_side_prob"])
        if m is None or bp is None:
            continue
        if bp >= 0.55:
            ag.append(m["hit"])
        elif bp < 0.45:
            dg.append(m["hit"])
    n = len(ag) + len(dg)
    if n < 100 or len(ag) < 15 or len(dg) < 15:
        st = "collecting"
    else:
        da, dd = sum(ag) / len(ag), sum(dg) / len(dg)
        st = "ready" if da >= dd + 0.10 else ("failed" if da <= dd else "warning")
    V.append({"name": "Standards: bet365 agree vs disagree", "unit": "settled Standards (bet365 agree or disagree)", "n": n, "need": 100,
              "state": st, "before": ("%.1f%% when bet365 disagrees" % (100.0 * sum(dg) / len(dg))) if dg else "--",
              "after": ("%.1f%% when bet365 agrees" % (100.0 * sum(ag) / len(ag))) if ag else "--",
              "verdict": "", "rec": {"ready": "skip Standards bet365 disagrees with; prefer ones it agrees with",
                                    "failed": "ignore bet365 for Standards", "warning": "keep testing"}.get(st, "")})
    return V


LABELS = {"healthy": "HEALTHY", "warning": "WARNING", "action": "ACTION NEEDED", "collecting": "COLLECTING DATA",
          "ready": "READY FOR REVIEW", "failed": "FAILED"}
BAR = "━━━━━━━━━━━━━━━━━━━━━━"


def _card(title, rows):
    out = [BAR, title, BAR, ""]
    for k, v in rows:
        out.append(("%s: %s" % (k, v)) if k else v)
    out.append(BAR)
    return "\n".join(out)


# ----------------------------------------------------------------------------- daily alerts
def daily(state):
    E = gather()
    B = E["boards"]
    V = live_validations(E)
    msgs = []

    def changed(key, value):
        old = state.get(key)
        state[key] = value
        return old != value

    def boards_rows():
        rows = []
        for b in ("top3", "elite6", "top10"):
            x = B[b]
            rows.append((x["label"], "%s (old engine %s)" % (_pct(x["new_w"], x["new_n"]), _pct(x["old_w"], x["old_n"]))))
        wd = E["top3_new"]["per_week"]
        rows.append(("Top 3 winning days/week", "%s (old %.1f)" % (("%.1f" % wd) if wd is not None else "--", E["top3_old"]["per_week"] or 0)))
        return rows

    if changed("engine", E["engine"]) and E["engine"] in ("better", "worse"):
        msgs.append(_card("📈 NEW ENGINE UPDATE" if E["engine"] == "better" else "📉 NEW ENGINE WARNING",
                          [("Result", "new engine is beating the old engine" if E["engine"] == "better"
                            else "new engine has dropped below the old engine")] + boards_rows()))

    if changed("goal", E["goal"]) and E["goal"] != "insufficient":
        st = E["top3_new"]
        msgs.append(_card("🎯 TOP 3 GOAL TRACKER", [
            ("Status", {"4+": "pace reached 4+ winning days/week", "3-4": "pace reached 3 winning days/week",
                        "below": "pace fell below the 3-day target"}[E["goal"]]),
            ("Pace", "%.1f winning days/week" % st["per_week"]),
            ("Record", "3-for-3 on %d of %d complete days" % (st["c"][3], st["days"]))]))

    if changed("apex", E["apex"]) and E["apex"] != "insufficient":
        bl, pa = E["apex_blocked"], E["apex_passed"]
        msgs.append(_card("🧪 APEX SCORE ALERT", [
            ("Finding", {"helps": "Apex Score is helping", "neutral": "Apex Score is neutral", "hurts": "Apex Score is hurting"}[E["apex"]]),
            ("70%+ props it blocked", _pct(sum(p["hit"] for p in bl), len(bl))),
            ("70%+ props it passed", _pct(sum(p["hit"] for p in pa), len(pa))),
            ("Recommendation", {"helps": "keep the filter", "neutral": "removing it simplifies with no loss",
                                "hurts": "remove the filter"}[E["apex"]]),
            ("Decision Required", "Keep / Remove")]))

    if changed("honest", E["honest"]) and E["honest"] != "insufficient":
        msgs.append(_card("📊 HONEST PROBABILITY ALERT", [
            ("Status", "READY FOR REVIEW -- passed; recommended as the qualification gate" if E["honest"] == "pass" else "FAILED the promotion requirements"),
            ("Sample", "%d settled Honest-70%%+ props" % E["honest_n"]),
            ("Win rate", _pct(E["honest_w"], E["honest_n"]) + " (needs 72%+ and better accuracy than raw)"),
            ("Decision Required", "Approve gate / Continue testing" if E["honest"] == "pass" else "none -- raw probability stays")]))

    names = {"aces": "Aces", "total_games": "Total Games", "fantasy_score": "Fantasy Score", "breaks": "Breaks"}
    for c, s_ in E["cat_state"].items():
        if changed("cat_" + c, s_) and s_ not in ("collecting", "steady"):
            w, n = E["cats"][c]
            msgs.append(_card("🏆 CATEGORY ALERT", [
                ("Category", names[c]),
                ("Change", {"improving": "improving", "declining": "declining", "ready": "ready for reconsideration",
                            "not ready": "not ready to return"}[s_]),
                ("70%+ props since new engine", _pct(w, n))]))

    for v in V + [dict(name=k, unit="settled", n=x["n"], need=x["need"], state={"promote": "ready", "fail": "failed"}.get(x["state"], "collecting"),
                        before="--", after=_pct(x["w"], x["n"]) if x["w"] is not None else "--", verdict="", rec=x["rec"])
                  for k, x in E["tests"].items() if not k.startswith(("Honest", "Remove Apex", "Bring Breaks"))]:
        if changed("test_" + v["name"], v["state"]) and v["state"] in ("ready", "failed"):
            if v["state"] == "ready":
                # APEX-LEARNV2-2026-10-24: same facts, plain words + emojis
                msgs.append(_card("✅ PROMOTION CANDIDATE", [("🧪 Test", v["name"]), ("📊 Checked on", "%d %s" % (v["n"], v["unit"])),
                                                            ("⬅️ Without it", v["before"]), ("➡️ With it", v["after"]),
                                                            ("💡 What it means", "it passed its test -- " + v["rec"]),
                                                            ("👉 Your call", "say go to turn it on, or keep testing")]))
            else:
                msgs.append(_card("❌ TEST FAILURE", [("Feature", v["name"]), ("Evidence", v["verdict"] or v["after"]),
                                                     ("Recommendation", v["rec"]), ("Decision Required", "Switch off / Continue testing")]))

    # Promotion Countdown: one message when any test crosses 25 / 50 / 75 / 100%
    progress = [("Honest Probability", E["honest_n"], HONEST_MIN, "settled props"),
                ("Apex Score Review", len(E["apex_blocked"]) + len(E["apex_passed"]), MIN_APEX_PROPS, "settled props")] + \
               [(v["name"], v["n"], v["need"], v["unit"]) for v in V]
    crossed = False
    for name, n, need, unit in progress:
        q = min(4, int(4 * n / max(need, 1)))   # 0..4 quarters
        if q > state.get("countdown_" + name, 0):
            crossed = crossed or q > 0
            state["countdown_" + name] = q
    if crossed:
        def phase(n, need):
            f = n / max(need, 1)
            return "Decision ready" if f >= 1 else ("Near Decision" if f >= 0.75 else ("Halfway" if f >= 0.5 else "Early Testing"))
        msgs.append(_card("🧪 TEST PROGRESS", [("", "%s\n%d / %d %s\nStatus: %s\n" % (name, n, need, unit, phase(n, need)))
                                              for name, n, need, unit in progress]))

    # APEX-TESTDONE-2026-10-13 (owner: "everything tested in the background
    # [must] let me know automatically when it's finished"): background paper
    # tests post one TEST FINISHED card with the result, and dated study reruns
    # post one reminder -- each exactly once.
    for key, mod in (("std_paper", "apex_standard_papertest"), ("gob_paper", "apex_goblin_papertest"),
                     ("slipsize_paper", "apex_slipsize_papertest"),   # APEX-SLIPSIZE-2026-10-21
                     ("signal_paper", "apex_signal_papertest"),   # APEX-SIGNALTEST-2026-10-21
                     ("rerank_paper", "apex_rerank_papertest"),   # APEX-RERANKTEST-2026-10-22
                     ("top3no175_paper", "apex_top3_no175_papertest"),   # APEX-TOP3NO175-2026-10-22
                     ("dffsholds_paper", "apex_dffs_holds_papertest"),   # APEX-DFFANTASY-2026-10-22
                     ("nobook_holds", "apex_nobook_tracker"),   # APEX-NOBOOK-2026-10-24
                     ("blowout_paper", "apex_blowout_signal"),   # APEX-BLOWOUT-2026-10-22
                     ("acesclose_paper", "apex_aces_close_papertest"),   # APEX-ACESCLOSE-2026-10-22
                     ("pinnacle_paper", "apex_pinnacle"),   # APEX-PINNACLE-2026-10-22
                     ("movedline_test", "apex_moved_line_test")):   # APEX-PULLEDNEWLINE-2026-10-22
        try:
            if state.get("finished_" + key):
                continue
            done, rows = __import__(mod).finish_check()
            if done:
                state["finished_" + key] = datetime.now().isoformat(timespec="seconds")
                msgs.append(_card("🧪 TEST FINISHED -- DECISION NEEDED", rows))
        except Exception as e:
            print("[alerts] finish check %s skipped: %s" % (key, e))
    for key, when, rows in REMINDERS:
        if ops.now_ct().date().isoformat() >= when and not state.get("reminder_" + key):
            state["reminder_" + key] = datetime.now().isoformat(timespec="seconds")
            msgs.append(_card("⏰ STUDY DUE", rows))
    for m in msgs:
        _send("system", m)
        if any(x in m for x in ("TEST FINISHED", "STUDY DUE", "PROMOTION CANDIDATE", "TEST FAILURE")):
            _send("alerts", m)   # results you must act on also land in #alerts

    # ---- one short daily status
    integrity_24h = [v for k, v in state.get("integrity_log", {}).items()
                     if v >= (datetime.now() - timedelta(hours=24)).isoformat()]
    eng, goal, apex, hon = E["engine"], E["goal"], E["apex"], E["honest"]
    st = E["top3_new"]
    lab = lambda k: LABELS[k]
    eng_lab = {"better": "healthy", "even": "warning", "worse": "action", "insufficient": "collecting"}[eng]
    goal_lab = {"4+": "healthy", "3-4": "healthy", "below": "action", "insufficient": "collecting"}[goal]
    apex_lab = {"helps": "healthy", "neutral": "ready", "hurts": "ready", "insufficient": "collecting"}[apex]
    hon_lab = {"pass": "ready", "fail": "failed", "insufficient": "collecting"}[hon]
    L = [BAR, "OPENCLAW DAILY STATUS -- %s" % ops.now_ct().strftime("%b %-d"), BAR, "",
         "ENGINE", "Status: %s" % lab(eng_lab),
         "Result: %s" % {"better": "new engine is beating the old engine", "even": "even with the old engine",
                         "worse": "behind the old engine", "insufficient": "too early to compare"}[eng],
         "Progress: %d / %d settled Top 10 legs" % (B["top10"]["new_n"], MIN_BOARD_LEGS), "",
         "TOP 3 GOAL", "Status: %s" % lab(goal_lab), "Target: 3-4 winning days/week",
         ("Pace: %.1f winning days/week" % st["per_week"]) if st["per_week"] is not None else "Pace: --",
         "Progress: %d / %d completed days" % (st["days"], MIN_GOAL_DAYS), "",
         "APEX SCORE", "Status: %s" % lab(apex_lab),
         "Finding: %s" % {"helps": "helping", "neutral": "neutral", "hurts": "hurting", "insufficient": "--"}[apex],
         "Progress: %d / %d settled props" % (len(E["apex_blocked"]) + len(E["apex_passed"]), MIN_APEX_PROPS), "",
         "HONEST PROBABILITY", "Status: %s" % lab(hon_lab),
         "Progress: %d / %d settled props" % (E["honest_n"], HONEST_MIN), "",
         "BOARD INTEGRITY", "Status: %s" % ("HEALTHY" if not integrity_24h else "ACTION NEEDED"),
         "Missed Board Props (24h): %d" % len(integrity_24h), "",
         "ACTIVE TESTS"]
    for v in V:
        L.append("• %s -- %d / %d %s -- %s" % (v["name"], v["n"], v["need"], v["unit"], lab(v["state"])))
    L.append("• Honest Probability gate -- %d / %d settled props -- %s" % (E["honest_n"], HONEST_MIN, lab(hon_lab)))
    try:   # APEX-SLIPSV2-2026-10-22: Top 3 / Elite 6 slips since the 10/22 rule changes, in slips not picks
        import apex_slips_v2 as _v2
        _v2.grade()
        L += [""] + _v2.summary_lines()
    except Exception:
        pass
    try:   # APEX-BESTTIME-2026-10-31
        import apex_best_time as _bt
        _btl = _bt.status_line()
        if _btl:
            L.append(_btl)
    except Exception as e:
        L.append("\u26A0\uFE0F Best time tracker failed: %s" % type(e).__name__)
    try:   # APEX-BOARDCOUNTERS-2026-10-31
        import apex_board_counters as _bc
        _bcl = _bc.status_line()
        if _bcl:
            L.append(_bcl)
    except Exception as e:
        L.append("\u26A0\uFE0F Board counters failed: %s" % type(e).__name__)
    try:   # APEX-SETGAMES-2026-11-01
        import apex_set_games as _sgl
        _sgs = _sgl.status_line()
        if _sgs:
            L.append(_sgs)
    except Exception as e:
        L.append("\u26A0\uFE0F First-set list failed: %s" % type(e).__name__)
    try:   # APEX-LINEVAULT-2026-11-09: every PrizePicks line, all sports, one store (reads the vault's last-run file only)
        import importlib.util as _ilu
        _lvs = _ilu.spec_from_file_location("apex_line_vault", os.path.expanduser("~/ApexSports/_shared/line_vault/vault.py"))
        _lvm = _ilu.module_from_spec(_lvs); _lvs.loader.exec_module(_lvm)
        L.append(_lvm.status_line())
    except Exception as e:
        L.append("\U0001F5C4 Line vault: \u26A0\uFE0F status failed: %s" % type(e).__name__)
    try:   # APEX-FAILLOG-2026-11-01
        import apex_failopen as _ffo
        _ffl = _ffo.status_line()
        if _ffl:
            L.append(_ffl)
    except Exception:
        pass
    try:   # APEX-CUSHIONFILL-2026-11-01
        import apex_cushion_tracker as _cft
        L.append(_cft.status_line())
    except Exception as e:
        L.append("\u26A0\uFE0F Cushion fill tracker failed: %s" % type(e).__name__)
    try:   # APEX-STANDARDPAPER-2026-10-31
        import apex_standard_paper as _stdp
        _spl = _stdp.status_line()
        if _spl:
            L.append(_spl)
    except Exception as e:
        L.append("\u26A0\uFE0F Standard line paper failed: %s" % type(e).__name__)
    try:   # APEX-PLAYERDATA-2026-10-31
        import apex_player_history as _phist
        _phl = _phist.status_line()
        if _phl:
            L.append(_phl)
    except Exception as e:
        L.append("\u26A0\uFE0F Player history check failed: %s" % type(e).__name__)
    try:   # APEX-PPMIRROR-C-2026-10-23: ParlayPlay official slip (days won + $) and ParlayPlay paper tests
        _pph = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ApexParlayPlay")
        if _pph not in sys.path:
            sys.path.insert(0, _pph)
        import apex_pp_tracking as _ppt
        L += ["", "PARLAYPLAY"] + _ppt.status_lines()
    except Exception:
        pass
    try:   # APEX-SLIPPLANNER-2026-10-11: how the recommended slip per board actually did
        import apex_slip_planner as _spl
        plans = []
        if os.path.exists(_spl.PLAN_LOG):
            with open(_spl.PLAN_LOG) as fh:
                plans = [json.loads(x) for x in fh if x.strip()]
        first = {}
        for pl in plans:
            k = (pl["board"], pl["match_date"])
            if k not in first or pl["captured_at"] < first[k]["captured_at"]:
                first[k] = pl
        lo7 = (ops.now_ct().date() - timedelta(days=6)).isoformat()
        per = collections.defaultdict(lambda: [0, 0])
        for (b, d), pl in first.items():
            if d < lo7:
                continue
            hits = []
            for lg in pl["legs"]:
                m = E["S"].find(lg.get("match_date") or d, lg.get("player"), lg.get("opponent"), lg.get("category"), lg.get("line"), lg.get("direction"))
                hits.append(None if m is None else m["hit"])
            if None in hits:
                continue
            k = sum(1 for h in hits if h)
            per[b][1] += 1
            per[b][0] += 1 if float(pl["mult"].get(str(k), 0)) > 1.0 else 0
        if per:
            names = {"top3": "Top 3", "elite6": "Elite 6", "top10": "Top 10", "bestgoblin": "Best Goblin",
                     "beststandard": "Best Standard", "bestdemon": "Best Demon"}
            L += ["", "SLIP PLANNER (recommended slips, last 7 days)"]
            for b, (w, n) in sorted(per.items()):
                L.append("%s: %d winning day%s of %d (~%.1f/week)" % (names.get(b, b), w, "" if w == 1 else "s", n, 7.0 * w / n))
    except Exception:
        pass
    try:   # APEX-SLIPGRADE-2026-10-11: your own slips, board-only vs with off-board picks
        import apex_slip_grader as _sg
        _sl = _sg.pnl_summary(7)
        if _sl:
            L += ["", "YOUR SLIPS"] + _sl
    except Exception:
        pass
    try:   # APEX-PROPSDB-2026-10-13: the full props database, one line
        import apex_props_db as _pdb
        L += ["", "PROPS DATABASE"] + _pdb.stats()
    except Exception:
        pass
    try:   # APEX-CHECKS5-2026-10-12: props the engine skipped, one line
        import apex_edge_tracker as _t5
        L += ["", "SKIPPED PROPS"] + skipped_props_line(_t5)
    except Exception:
        pass
    try:   # APEX-GOBPAPER-2026-10-13: Goblin 80% rule for Top 3 / Best Goblin, paper test only
        import apex_goblin_papertest as _gpt
        _gp = _gpt.summary()
        if _gp:
            L += ["", "GOBLIN 80% PAPER TEST"] + _gp
    except Exception:
        pass
    try:   # APEX-DATAHEALTH-2026-10-22: surface, tour, names, player data, odds
        import apex_data_health as _dhs
        L += ["", "DATA HEALTH"] + _dhs.lines()
    except Exception:
        pass
    try:   # APEX-PPTHROTTLE2-2026-10-24: PrizePicks request volume, watched daily
        import apex_ops as _ppc
        L += [_ppc.pp_calls_summary()]
    except Exception:
        pass
    try:   # APEX-BLOWOUT-2026-10-22: TG Goblins 3+ games of room, background only
        import apex_blowout_signal as _bst
        _bt = _bst.summary()
        if _bt:
            L += ["", "BLOWOUT TEST"] + _bt
    except Exception:
        pass
    try:   # APEX-PULLEDNEWLINE-2026-10-22: pulled spots -- same pick at the new line vs the backup
        import apex_moved_line_test as _mlt
        L += ["", "MOVED LINES"] + _mlt.summary()
    except Exception:
        pass
    try:   # APEX-ACESCLOSE-2026-10-22: Aces MORE players-close rule, background only
        import apex_aces_close_papertest as _acpt
        _ac = _acpt.summary()
        if _ac:
            L += ["", "ACES PLAYERS-CLOSE TEST"] + _ac
    except Exception:
        pass
    try:   # APEX-TOP3NO175-2026-10-22: Top 3 without over 17.5 Total Games, background only
        import apex_top3_no175_papertest as _t3n
        _tn = _t3n.summary()
        if _tn:
            L += ["", "TOP 3 WITHOUT 17.5 TEST"] + _tn
    except Exception:
        pass
    try:   # APEX-NOBOOK-2026-10-24: Total Games picks with no book total held to BACKUPS
        import apex_nobook_tracker as _nbk
        _nb = _nbk.summary()
        if _nb:
            L += [""] + _nb
    except Exception:
        pass
    try:   # APEX-DFFANTASY-2026-10-22: Fantasy LESS / DF holds, with vs without, slip terms
        import apex_dffs_holds_papertest as _dfh
        _dh = _dfh.summary()
        if _dh:
            L += ["", "FANTASY LESS / DF HOLDS TEST"] + _dh
    except Exception:
        pass
    try:   # APEX-RERANKTEST-2026-10-22: #4-10 re-ranked by signals, background only
        import apex_rerank_papertest as _rrpt
        _rr = _rrpt.summary()
        if _rr:
            L += ["", "RE-RANK TEST"] + _rr
    except Exception:
        pass
    try:   # APEX-PINNACLE-2026-10-22: Pinnacle cushion paper test, background only
        import apex_pinnacle as _pinpt
        _pn = _pinpt.summary()
        if _pn:
            L += ["", "PINNACLE CUSHION TEST"] + _pn
    except Exception:
        pass
    try:   # APEX-SIGNALTEST-2026-10-21: 7 promising signals, background only
        import apex_signal_papertest as _sgpt
        _sg = _sgpt.summary()
        if _sg:
            L += ["", "SIGNAL BACKGROUND TEST"] + _sg
    except Exception:
        pass
    try:   # APEX-SLIPSIZE-2026-10-21: 3-6 leg slips + prop mix, paper test only
        import apex_slipsize_papertest as _sspt
        _ss = _sspt.summary()
        if _ss:
            L += ["", "SLIP SIZE PAPER TEST"] + _ss
    except Exception:
        pass
    try:   # APEX-STDPAPER-2026-10-12: revised Best Standard rule, paper test only
        import apex_standard_papertest as _spt
        _pt = _spt.summary()
        if _pt:
            L += ["", "STANDARD PAPER TEST"] + _pt
    except Exception:
        pass
    cands = [(name, n, need, unit) for name, n, need, unit in progress if n < need]
    nxt = max(cands, key=lambda x: x[1] / max(x[2], 1), default=None)
    L += ["", "NEXT DECISION"]
    L += (["%s" % nxt[0], "Progress: %d / %d %s" % (nxt[1], nxt[2], nxt[3])] if nxt else ["none pending"])
    try:   # APEX-PLAYTHIS-2026-11-04: won-lost off the PLAY THIS card only
        import apex_play_this as _pt
        if _pt.PLAY_THIS_ON:
            L += ["", _pt.record_text()]
    except Exception as e:
        print("[alerts] play this record skipped: %s" % e)
    L.append(BAR)
    L = _trim_daily(L)   # APEX-TRIMDAILY-2026-11-09
    _send("system", "\n".join(L))
    return msgs, L




# ================================================================ APEX-TRIMDAILY-2026-11-09
# owner, "Go trim system card": the 9 AM daily status had grown to ~370
# lines and still printed paper tests that finished or failed weeks ago. This
# trims the finished sections out, keeps one board record (Slips V2 = the
# official slip for Top 3 / Elite 6 / Elite 5; the other boards from /counters),
# and splits ACTIVE TESTS into tests still collecting vs tests waiting on his
# decision. Nothing is deleted: every test module, /counters, /slipsv2 and the
# test commands keep working. Undo: TRIM_DAILY = False.
TRIM_DAILY = True
_TRIM_SECTIONS = {"SLIP PLANNER (recommended slips, last 7 days)", "GOBLIN 80% PAPER TEST", "BLOWOUT TEST",
                  "MOVED LINES", "ACES PLAYERS-CLOSE TEST", "TOP 3 WITHOUT 17.5 TEST", "FANTASY LESS / DF HOLDS TEST",
                  "RE-RANK TEST", "PINNACLE CUSHION TEST", "SIGNAL BACKGROUND TEST", "SLIP SIZE PAPER TEST",
                  "STANDARD PAPER TEST", "SKIPPED PROPS"}
_TRIM_PREFIXES = ("\U0001F3BE First-set list", "\U0001F6E1 Cushion fill", "\U0001F4DD Standard line paper",
                  "\u23F0 Best time:", "Playing off the first board instead", "\U0001F4CA NO-BOOK-TOTAL HOLDS")

# APEX-CLOSETESTS-2026-11-09 (owner, 11/9 6:06 PM CT: "Go close the tests").
# Decided: Repricing keep on, Honest Probability gate keep, ATP Opponent Aces and
# Indoor Adjustment stay on (testing stopped), Standards bet365 agree/disagree dropped,
# Best Standard 6x closed (failed). Display only. Undo: CLOSED_TESTS = set().
CLOSED_TESTS = {"Repricing", "Honest Probability gate", "ATP Opponent Aces", "Indoor Adjustment",
                "Standards: bet365 agree vs disagree", "Best Standard (6x rule)"}

def _trim_daily(L):
    """Shorter daily status. Pure formatting over the lines already built."""
    if not TRIM_DAILY:
        return L
    out, skip = [], False
    for ln in L:
        if skip:
            if ln == "":
                skip = False
            else:
                continue
        if ln in _TRIM_SECTIONS:
            skip = True
            if out and out[-1] == "":
                out.pop()
            continue
        if ln.startswith(_TRIM_PREFIXES):
            continue
        if ln.startswith("\U0001F4CB Boards since 10/31: "):
            parts = [p for p in ln.split(": ", 1)[1].split(" \u00b7 ") if not p.startswith(("Top 3 ", "Elite 6 "))]
            ln = "\U0001F4CB Other boards since 10/31 (official board, whole slips): " + " \u00b7 ".join(parts)
        out.append(ln)
    # ACTIVE TESTS -> collecting vs waiting on a decision
    try:
        i = out.index("ACTIVE TESTS")
        j = i + 1
        while j < len(out) and out[j].startswith("\u2022 "):
            j += 1
        bullets = out[i + 1:j]
        coll = [b for b in bullets if "COLLECTING" in b]
        wait = [b for b in bullets if "COLLECTING" not in b]
        # APEX-CLOSETESTS-2026-11-09: his "Go close the tests" -- decided, off the waiting list
        wait = [b for b in wait if b[2:].partition(" -- ")[0] not in CLOSED_TESTS]
        new = ["ACTIVE TESTS (still collecting)"] + (coll or ["\u2022 none"])
        if wait:
            new += ["", "WAITING ON YOUR DECISION (type: read out the tests)"]
            for b in wait:
                name, _, rest = b[2:].partition(" -- ")
                prog, _, state = rest.rpartition(" -- ")
                new.append("\u2022 %s -- %s (%s)" % (name, state.strip().lower(), prog))
        out[i:j] = new
    except ValueError:
        pass
    # drop doubled blank lines
    final = []
    for ln in out:
        if ln == "" and final and final[-1] == "":
            continue
        final.append(ln)
    return final

# ----------------------------------------------------------------------------- APEX-PLACE-REMINDER-2026-10-26
# owner (10/26, 5:09 PM): "I'll rather have a reminder in alerts and general when it
# time to place a board for elite6". The late board (last one before the first match) made
# the most money for Elite 6, so this posts ONE reminder per slate, in #alerts and #general,
# when the first Top 3 / Elite 6 match is PLACE_REMINDER_MINUTES away or less: run /fetch,
# then play that board (Top 3 Power, Elite 6 Flex). Runs with --check (every 30 min), so the
# reminder lands 60-90 minutes before the first match. Undo: PLACE_REMINDER_ON = False.
PLACE_REMINDER_ON = True
PLACE_REMINDER_MINUTES = 90


def _ct_clock(iso):
    from zoneinfo import ZoneInfo
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(ZoneInfo("America/Chicago"))
    return d, d.strftime("%a %-I:%M %p CT")


def _name_line(p):
    who = p.get("opponent") if p.get("player") == "match" else "%s v %s" % (p.get("player"), p.get("opponent"))
    cat = {"total_games": "Total Games", "fantasy_score": "Fantasy Score", "aces": "Aces", "dfs": "Double Faults",
           "breaks": "Breaks", "games_won": "Games Won", "tiebreaks": "Tiebreaks", "total_sets": "Total Sets",
           "1st_set_total_games": "1st Set Games", "1st_set_total_games_won": "1st Set Games Won"}.get(p.get("category"), p.get("category"))
    try:
        pct = " · Win %d%%" % round(100 * float(p.get("prob_cal") or p.get("prob") or 0))
    except Exception:
        pct = ""
    return "%s · %s %s %s%s" % (who, cat, p.get("direction"), p.get("line"), pct)


def place_reminder(state, t):
    """One 'time to place your boards' post per slate, in #alerts and #general. Never raises."""
    out = []
    if not PLACE_REMINDER_ON:
        return out
    try:
        snap = ops._read_json(t.PUBLISHED_BOARD_SNAPSHOT, {})
        boards = snap.get("boards") or {}
        picks = {}
        for b in ("top3", "elite6"):
            picks[b] = [e.get("p") or {} for e in (boards.get(b) or {}).get("entries") or []]
        now = datetime.now(timezone.utc)
        first, first_p = None, None
        for b in ("top3", "elite6"):
            for p in picks[b]:
                s = t._prop_start_utc(p)
                if not s:
                    continue
                d, _ = _ct_clock(s)
                if d <= now:
                    continue
                if first is None or d < first:
                    first, first_p = d, p
        if first is None:
            return out
        mins = (first - now).total_seconds() / 60
        if mins > PLACE_REMINDER_MINUTES:
            return out
        key = first.isoformat(timespec="minutes")
        if state.get("place_reminder_sent") == key:
            return out
        state["place_reminder_sent"] = key
        _, clock = _ct_clock(first.isoformat())
        who = first_p.get("opponent") if first_p.get("player") == "match" else "%s v %s" % (first_p.get("player"), first_p.get("opponent"))
        lines = ["⏰ **TIME TO PLACE YOUR BOARDS**",
                 "First board match: **%s** starts %s (in %dh %02dm)." % (who, clock, int(mins // 60), int(mins % 60)),
                 "Run **/fetch** now, then play THAT board: 🥇 Top 3 on Power, 🏆 Elite 6 on Flex. One slip each, no mixing versions.",
                 ""]
        if picks["elite6"]:
            try:
                _lbl = datetime.fromisoformat(snap.get("captured_at", "")).strftime("%-I:%M %p")
            except Exception:
                _lbl = snap.get("captured_at", "")[11:16]
            lines.append("🏆 Elite 6 on the last board (%s board):" % _lbl)
            for i, p in enumerate(picks["elite6"], 1):
                lines.append("%d) %s" % (i, _name_line(p)))
        if picks["top3"]:
            lines.append("🥇 Top 3: " + " · ".join("%d) %s" % (i, _name_line(p)) for i, p in enumerate(picks["top3"], 1)))
        msg = "\n".join(lines)
        for ch in ("alerts", "general"):
            _send(ch, msg)
        out.append(msg)
    except Exception as e:
        print("[alerts] place reminder skipped: %s" % e)
    return out

# ----------------------------------------------------------------------------- integrity + health checks
def check(state):
    import apex_edge_tracker as t
    sent = []
    status = ev._read(t.BOARD_STATUS_LOG)
    if status:
        caps = sorted({r["captured_at"] for r in status})
        last = caps[-1]
        if state.get("integrity_last_capture") != last:
            state["integrity_last_capture"] = last
            prev = caps[-2] if len(caps) > 1 else None
            cur = [r for r in status if r["captured_at"] == last]
            sent += integrity(cur, [r for r in status if r["captured_at"] == prev] if prev else [], state, t)
    sent += health(state, t)
    sent += place_reminder(state, t)   # APEX-PLACE-REMINDER-2026-10-26
    try:   # APEX-PLAYTHIS-2026-11-04: the one PLAY THIS card a day, pulled-leg backups, record
        import apex_play_this as _pt
        sent += _pt.run(state, post=not DRY)
    except Exception as e:
        print("[alerts] play this skipped: %s" % e)
    try:   # APEX-SOONSLIPS-2026-10-27: Top 3 Soon (80+) / Elite 6 Soon (90+) ready cards
        import apex_soon_slips as _ss
        sent += _ss.run(source="check", post=not DRY)
    except Exception as e:
        print("[alerts] soon slips skipped: %s" % e)
    try:
        sent += pipeline_checks(state, t)   # APEX-CHECKS5-2026-10-12
    except Exception as e:
        print("[alerts] pipeline checks skipped: %s" % e)
    try:   # APEX-BEST6TRACK-2026-11-11 (owner): Best 6 of 6 tracker, with tennis and without
        import apex_best6 as _b6
        sent += _b6.run(post=not DRY)
        sent += _b6.alert(post=not DRY)   # APEX-BEST6ALERT-2026-11-11: #general message when the card changed
        import apex_best6_times as _b6t   # APEX-BEST6TIMES-2026-11-13: card saved at 9 AM / 12 PM / 4:30 PM / 7 PM with its strength
        sent += _b6t.run(post=not DRY)
    except Exception as e:
        print("[alerts] best 6 tracker skipped: %s" % e)
    try:   # APEX-BEST3-2026-11-14: Best 3 card saved at 4:30 PM and graded, #system lines
        import apex_best3 as _b3
        sent += _b3.run(post=not DRY)
    except Exception as e:
        print("[alerts] best 3 tracker skipped: %s" % e)
    try:   # APEX-DROPPEDALERT-2026-11-14 ("I said fix all"): one #alerts line when a Best 6 / Star Demon / Top 3 pick leaves before game time
        import apex_dropped_alert as _da
        sent += _da.run(post=not DRY)
    except Exception as e:
        print("[alerts] dropped alert skipped: %s" % e)
    try:   # APEX-STARDEMON-2026-11-13: grade the Star Demon board legs after the 4 AM NBA grading, one #system line
        import apex_star_demon as _sd
        sent += _sd.run(post=not DRY)
    except Exception as e:
        print("[alerts] star demon grading skipped: %s" % e)
    try:   # APEX-BREAKSAUTO-2026-11-13: Breaks LESS 4+ paper test, daily 9 AM #system line + finish card
        import apex_breaks_papertest as _bp
        sent += _bp.auto(post=not DRY)
    except Exception as e:
        print("[alerts] breaks paper skipped: %s" % e)
    try:   # APEX-EARLYDEMON-2026-11-13: 9:35 AM "board ready" line + nightly grading, #system only
        import apex_early_demon as _ed
        sent += _ed.run(post=not DRY)
    except Exception as e:
        print("[alerts] early demon skipped: %s" % e)
    try:   # APEX-WATCH-2026-11-07 (owner, "Go apex watch"): plain-words problem alerts
        import apex_watch as _aw
        sent += _aw.run(post=not DRY)
    except Exception as e:
        print("[alerts] apex watch skipped: %s" % e)
    return sent


def integrity(cur, prev, state, t):
    log = state.setdefault("integrity_log", {})
    out = []
    shown = collections.defaultdict(set)      # board -> match keys on it
    on_board = collections.defaultdict(set)
    low = {}
    capped = collections.defaultdict(set)     # board -> categories held back by a category cap
    for r in cur:
        if r["status"] == "held_back" and str(r.get("reason") or "").startswith("category cap: "):
            capped[r["board"]].add(str(r["reason"])[len("category cap: "):].split(" ")[0])
    for r in cur:
        if r["status"] == "recommended" and r["board"] != "all":
            shown[r["board"]].add(_match_names(r))
            on_board[r["board"]].add(t._apex_prop_identity(r))
            low[r["board"]] = min(low.get(r["board"], 1.0), _f(r["prob"]) or 1.0)
    # (a) a qualifier that outranks something shown, hidden with no rule explaining it
    newer = _signals_newer_than(cur[0]["captured_at"]) if cur else []
    for r, pos, low_r in hidden_qualifiers(cur, t, shown, capped):
        key = "hidden|%s|%s|%s" % (r["board"], "|".join(t._apex_prop_identity(r)), r["match_date"])
        if key in log:
            continue
        if newer and str(r.get("reason") or "").startswith("board full"):
            # APEX-NEWPROPS-SCORED-2026-10-24: the builder ranked it below the board at build time;
            # the 🧮 signal data changed after that build, so the next republish re-orders it (the
            # 7:35 PM 10/24 Medvedev Elite 6 check: ParlayPlay lines updated 7:29, board built 7:15).
            # Not logged, so a new capture that still hides it does alert.
            print("[alerts] board check skipped: %s on %s -- %s updated after the %s board" % (
                _short(r), r["board"], " and ".join(newer), cur[0]["captured_at"]))
            continue
        log[key] = datetime.now().isoformat(timespec="seconds")
        try:   # APEX-BACKUPORDER-2026-10-24: the same BACKUP number the posted board card shows
            _bk = t.backup_numbers(r["board"]).get(tuple(t._apex_prop_identity(r)))
        except Exception:
            _bk = None
        out.append(_card("\U0001F6A8 BOARD CHECK: %s should be on %s (#%d) but isn't" % (
            _short(r), BOARD_LABEL.get(r["board"], r["board"]), pos), [
            ("Pick", "%s (%.0f%% to win, rank %.0f)%s" % (_short(r), 100 * (_f(r["prob"]) or 0), 100 * _rank_of(r, t),
                                                         (" -- BACKUP %d on the posted board" % _bk["no"]) if _bk else "")),
            ("Board", "%s ranks by Win Prob + 5 points per signal; its last spot ranks %.0f" % (
                BOARD_LABEL.get(r["board"], r["board"]), 100 * low_r)),
            ("Why it matters", "no board rule (one per match/player, BACKUPS hold, category cap) explains it"),
            ("Action", "reason unknown -- review this board build")]))
    # (b) a board prop from the previous publish that vanished while PrizePicks still offers it
    gone = []
    cur_ids = {t._apex_prop_identity(r) for r in cur if r["board"] == "all"}
    for r in prev:
        if r["status"] != "recommended" or r["board"] not in INTEGRITY_BOARDS:
            continue
        ident = t._apex_prop_identity(r)
        if ident in on_board[r["board"]] or (_f(r["prob"]) or 0) < INTEGRITY_MIN_PROB:
            continue
        if t._pub_match_started(r):
            continue
        gone.append((r, ident in cur_ids))
    if gone:
        try:
            offered = {(b["stat_slug"], b["line"], b["odds_type"], ops_norm(b["player"])) for b in ops.partner_api_board(max_age=600, caller="alerts")}
            stat_map = ops._core_const("_APEX_STAT_MAP")
            rev = collections.defaultdict(set)
            for slug, cat in stat_map.items():
                rev[cat].add(slug)
        except Exception:
            offered, rev = None, {}
        for r, still_evaluated in gone:
            names = [ops_norm(x) for x in (str(r["opponent"]).split(" v ") if r["player"] == "match" else [r["player"]])]
            is_offered = offered is not None and any(
                (slug, float(r["line"]), str(r.get("prop_type") or "standard"), nm) in offered
                for slug in rev.get(r["category"], ()) for nm in names)
            if not is_offered:
                continue   # line removed by PrizePicks: expected, the refetch report covers it
            if still_evaluated:
                continue   # still on the active board, just outranked: expected
            key = "dropped|%s|%s" % (r["board"], "|".join(t._apex_prop_identity(r)))
            if key in log:
                continue
            log[key] = datetime.now().isoformat(timespec="seconds")
            out.append(_card("🚨 BOARD INTEGRITY ALERT", [
                ("Prop", _prop(r)), ("Probability", "%.1f%%" % (100 * (_f(r["prob"]) or 0))), ("Apex Score", r.get("apex_score") or "?"),
                ("Expected Board", r["board"]),
                ("Reason Missing", "dropped on the latest publish while PrizePicks still offers this exact line and the match hasn't started"),
                ("Action", "run /fetch; if it stays missing, it's a data or publish issue")]))
    cutoff = (datetime.now() - timedelta(days=3)).isoformat()
    state["integrity_log"] = {k: v for k, v in log.items() if v >= cutoff}
    for m in out:
        _send("alerts", m)
    return out



# ----------------------------------------------------------------------------- APEX-ODDSCARD-2026-10-24
# The "stronger than the lowest prop shown" check compared plain Win Probability, but
# every board ranks by board_rank_score (Win Prob + 5 points per net signal, minus the
# 17.5 push-down). On 10/24 it flagged Yastremska Fantasy MORE 3.0 (71.2%, rank 76) as
# missing from Top 3 because Fritz v Musetti TG MORE 19.5 is 71.0% -- but Fritz ranks 86
# with its signals. It now uses the real rank and the BACKUPS holds, and skips rows
# whose own reason already names the rule that kept them off.
BOARD_LABEL = {"top3": "Top 3", "elite6": "Elite 6", "top10": "Top 10", "bestgoblin": "Best Goblin",
               "beststandard": "Best Standard", "bestdemon": "Best Demon"}
RULE_REASONS = ("one per match/player", "category on hold", "outside the Best Standard rule", "under 75%",
                "category cap", "17.5", "no bet365", "BACKUPS")
_RANK_CACHE = {}


def _board_picks(t):
    if "picks" not in _RANK_CACHE:
        try:
            picks, _ = t.load_current_board()
        except Exception:
            picks = []
        _RANK_CACHE["picks"] = {t._apex_prop_identity(p): p for p in picks or []}
    return _RANK_CACHE["picks"]


def _rank_of(r, t):
    """The rank score the boards order by, for a status-log row (plain prob if the pick is gone)."""
    p = _board_picks(t).get(t._apex_prop_identity(r))
    pr = _f(r.get("prob")) or 0
    if not p:
        return pr
    try:   # APEX-BACKUPORDER-2026-10-24: the board's own rank (Best Standard by its market rank)
        return t.board_rank_of(r.get("board"), p) if hasattr(t, "board_rank_of") else t.board_rank_score(p, t._board_prob(p))
    except Exception:
        return pr


def _held(r, t):
    p = _board_picks(t).get(t._apex_prop_identity(r))
    try:
        return bool(p and t.board_hold_reason(p))
    except Exception:
        return False


def _short(r):
    if r.get("player") == "match":
        who = " v ".join(str(x).split()[-1] for x in str(r.get("opponent") or "").split(" v ") if str(x).split())
    else:
        who = (str(r.get("player") or "").split() or ["?"])[-1]
    cat = {"total_games": "TG", "fantasy_score": "Fantasy", "double_faults": "DF", "games_won": "Games Won",
           "aces": "Aces", "breaks": "Breaks"}.get(r.get("category"), str(r.get("category") or "").replace("_", " ").title())
    return "%s %s %s %s" % (who, cat, r.get("direction"), r.get("line"))


SIGNAL_SOURCES = (("ParlayPlay lines", os.path.join(HERE, "ApexParlayPlay", "data", "parlayplay_db.sqlite")),
                  ("bet365 totals", os.path.join(HERE, "apex_book_lines_log_v1.csv")))


def _signals_newer_than(captured_at):
    """Names of the 🧮 signal sources written after this board capture (the board's rank order
    used older data). [] if none or unknown."""
    try:
        cap = datetime.fromisoformat(captured_at).timestamp()
    except Exception:
        return []
    out = []
    for name, path in SIGNAL_SOURCES:
        try:
            if os.path.getmtime(path) > cap + 60:
                out.append(name)
        except OSError:
            pass
    return out


def hidden_qualifiers(cur, t, shown, capped):
    """[(row, the spot it would take, rank of the board's last spot)] for 70%+ props that
    outrank something on the board with no rule explaining why they are off it."""
    _RANK_CACHE.clear()
    low = {}
    ranks_on = collections.defaultdict(list)
    for r in cur:
        if r["status"] == "recommended" and r["board"] in INTEGRITY_BOARDS:
            ranks_on[r["board"]].append(_rank_of(r, t))
    for b, v in ranks_on.items():
        low[b] = min(v)
    out = []
    for r in cur:
        if r["status"] != "not_shown" or r["board"] not in INTEGRITY_BOARDS or r["board"] not in low:
            continue
        if (_f(r["prob"]) or 0) < INTEGRITY_MIN_PROB:
            continue
        if any(x in str(r.get("reason") or "") for x in RULE_REASONS):
            continue
        if _match_names(r) in shown[r["board"]]:
            continue   # same match already on this board: one-per-match rule, expected
        if r.get("category") in capped[r["board"]]:
            continue   # APEX-CHECKS5-2026-10-12: that category is already at its cap on this board
        rk = _rank_of(r, t)
        if rk <= low[r["board"]] + 1e-9 or _held(r, t):
            continue
        out.append((r, 1 + sum(1 for x in ranks_on[r["board"]] if x >= rk), low[r["board"]]))
    return out

# ----------------------------------------------------------------------------- APEX-CHECKS5-2026-10-12
# owner, "All 5" (2026-10-12), after a night of props going missing:
#  1. a prop hidden as "already started" while PrizePicks says its match hasn't begun
#  2. a published board showing a line PrizePicks no longer offers (moved)
#  3. Best boards in the integrity check (INTEGRITY_BOARDS) + #picks Standard Slip
#     still matching /beststandard
#  4. a slip leg still ungraded 48 h after its match started
#  5. (daily status, not an alert) live props the engine never evaluated, by reason
def _live_board():
    """[(cat, name, line, odds_type, start_utc, stat_slug, opponent)] from one partner-API call."""
    stat_map = ops._core_const("_APEX_STAT_MAP") or {}
    out = []
    for b in ops.partner_api_board(max_age=600, caller="alerts"):   # APEX-PPTHROTTLE-2026-10-16
        cat = stat_map.get(b["stat_slug"], b["stat_slug"])
        out.append((cat, ops_norm(b["player"]), float(b["line"]), b["odds_type"] or "standard", b.get("start_utc") or "", b["stat_slug"],
                    ops_norm(b.get("opponent"))))
    return out


def _names(r):
    return [ops_norm(x) for x in (str(r.get("opponent") or "").split(" v ") if r.get("player") == "match" else [r.get("player")])]


def _future(iso, now):
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d > now
    except Exception:
        return False


def pipeline_checks(state, t, live=None):
    log = state.setdefault("pipeline_log", {})
    out = []
    now = datetime.now(timezone.utc)
    live = _live_board() if live is None else live
    by_key = collections.defaultdict(list)          # (cat, name, odds_type) -> [(line, start, opponent)]
    for cat, nm, line, ot, st, _, opp in live:
        by_key[(cat, nm, ot)].append((line, st, opp))

    def _same_match(r, opp):
        names = set(_names(r)) | {ops_norm(r.get("opponent"))}
        return bool(opp) and any(opp == n or opp in n or n in opp for n in names if n)

    def once(key, card):
        if key in log:
            return
        log[key] = datetime.now().isoformat(timespec="seconds")
        out.append(card)

    # 1. hidden as started while PrizePicks says it hasn't started
    latest = {}
    with open(t.PICK_LOG, newline="") as fh:
        for r in csv.DictReader(fh):
            if (r.get("match_date") or "") < ops.now_ct().date().isoformat() or str(r.get("playable") or "") == "0":
                continue
            k = t._apex_prop_identity(r) + (str(r.get("prop_type") or "standard").lower(),)
            if k not in latest or (r.get("ts") or "") > (latest[k].get("ts") or ""):
                latest[k] = r
    for k, r in latest.items():
        if not t._pub_match_started(r):
            continue
        ot = str(r.get("prop_type") or "standard").lower()
        try:
            line = float(r.get("line"))
        except (TypeError, ValueError):
            continue
        hits = [st for nm in _names(r) for (ln, st, opp) in by_key.get((r.get("category"), nm, ot), [])
                if ln == line and _same_match(r, opp)]
        if hits and any(_future(st, now) for st in hits):
            once("started|%s|%s" % ("|".join(k), r.get("match_date")), _card("🚨 BOARD INTEGRITY ALERT", [
                ("Prop", "%s -- %s %s (%s)" % (_who(r), r.get("category"), r.get("line"), ot)),
                ("Problem", "hidden as already started, but PrizePicks says the match starts %s" % max(hits)[:16].replace("T", " ") + " UTC"),
                ("Action", "run /fetch; if it stays hidden, the start time is wrong")]))

    # 2. published board line that PrizePicks has moved (one card per prop, all its boards)
    snap = ops._read_json(t.PUBLISHED_BOARD_SNAPSHOT, {})
    moved = collections.OrderedDict()
    for b, v in (snap.get("boards") or {}).items():
        for e in ((v or {}).get("entries") or [])[:t._PUBLISHED_FLAG_LIMIT.get(b, 6)]:
            p = e.get("p") or {}
            if t._pub_match_started(p):
                continue
            ot = str(p.get("prop_type") or "standard").lower()
            try:
                line = float(p.get("line"))
            except (TypeError, ValueError):
                continue
            offered = [(ln, st) for nm in _names(p) for (ln, st, opp) in by_key.get((p.get("category"), nm, ot), [])
                       if _same_match(p, opp)]
            if not offered or any(ln == line for ln, _ in offered):
                continue                                # still offered, or pulled entirely
            k = "moved|%s|%s|%s|%s" % ("|".join(_names(p)), p.get("category"), line, ot)
            moved.setdefault(k, [p, line, sorted({ln for ln, _ in offered}), []])[3].append(b)
    for k, (p, line, now_lines, boards) in moved.items():
        once(k, _card("🚨 BOARD INTEGRITY ALERT", [
            ("Prop", "%s -- %s %g %s (%s)" % (_who(p), p.get("category"), line, p.get("direction"), str(p.get("prop_type") or "standard").lower())),
            ("Boards", ", ".join(boards)),
            ("Problem", "PrizePicks moved this line to %s; the boards still show %g" % (", ".join("%g" % x for x in now_lines), line)),
            ("Action", "run /fetch before building a slip with it")]))

    # 2b. APEX-TYPECHECK-2026-10-14: published prop whose exact line PrizePicks
    # now lists under a different type (Standard -> Goblin/Demon and back)
    live_types = collections.defaultdict(set)
    for cat, nm, line, ot, st, _, opp in live:
        live_types[(cat, nm, line)].add(ot)
    retyped = collections.OrderedDict()
    for b, v in (snap.get("boards") or {}).items():
        for e in ((v or {}).get("entries") or [])[:t._PUBLISHED_FLAG_LIMIT.get(b, 6)]:
            p = e.get("p") or {}
            if t._pub_match_started(p):
                continue
            now_type = t._retyped(p, live_types)
            if not now_type:
                continue
            ot = str(p.get("prop_type") or "standard").lower()
            k = "retyped|%s|%s|%s|%s|%s" % ("|".join(_names(p)), p.get("category"), p.get("line"), ot, now_type)
            retyped.setdefault(k, [p, ot, now_type, []])[3].append(b)
    for k, (p, ot, now_type, boards) in retyped.items():
        once(k, _card("🚨 BOARD INTEGRITY ALERT", [
            ("Prop", "%s -- %s %s %s" % (_who(p), p.get("category"), p.get("line"), p.get("direction"))),
            ("Boards", ", ".join(boards)),
            ("Problem", "PrizePicks changed this from %s to %s after it was published" % (ot.title(), now_type.title())),
            ("Action", "board commands already hide it; the next auto-eval re-scores it as %s" % now_type.title())]))

    # 3b. #picks Standard Slip == /beststandard (same picks, same order)
    try:
        import apex_book_lines as _bl
        picks, _ = t.load_current_board()
        picks = _bl.annotate(picks, t._board_prob, t.corrected_prob, log=False)
        bs = [d["p"] for d in (t.best_standard_detail(picks, return_scored=True).get("scored") or [])][:t.SLIP_SLOTS]
        lines, _ = t._slip_sections([], picks)
        block = "\n".join(lines)
        block = block[block.find("STANDARD SLIP"):block.find("GOBLIN SLIP")] if "STANDARD SLIP" in block else ""
        slot_lines = [ln for ln in block.splitlines() if " Slot " in ln]
        missing = [p for i, p in enumerate(bs, 1)
                   if not any((" Slot %d " % i) in ln and str(p.get("opponent") if p.get("player") == "match" else p.get("player")) in ln
                               and str(p.get("line")) in ln for ln in slot_lines)]
        if missing or len(slot_lines) != len(bs):
            once("stdslip|%s" % snap.get("captured_at"), _card("🚨 BOARD INTEGRITY ALERT", [
                ("Problem", "the #picks Standard Slip no longer matches /beststandard"),
                ("/beststandard", "; ".join("%s %s %s" % (_who(p), p.get("category"), p.get("line")) for p in bs) or "empty"),
                ("Standard Slip", "; ".join(ln.strip() for ln in slot_lines) or "empty"),
                ("Action", "tell Claude -- the slip code has drifted from the board")]))
    except Exception as e:
        print("[alerts] standard slip check skipped: %s" % e)

    # 4. slip legs stuck ungraded 48 h after their match started
    try:
        slips = ops._read_json(os.path.join(HERE, "data", "slips", "slip_log.json"), [])
        for leg in slips if isinstance(slips, list) else []:
            if leg.get("status") != "PENDING" or leg.get("bet_type") != "prop":
                continue
            st = t._indexed_start_utc({"player": leg.get("player"), "opponent": ""}) or ""
            try:
                started = datetime.fromisoformat(st) if st else datetime.fromisoformat(str(leg.get("date"))[:10] + "T23:59:00+00:00")
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            hrs = (now - started).total_seconds() / 3600
            if hrs < SLIP_STUCK_HOURS:
                continue
            once("slipstuck|%s|%s|%s" % (leg.get("slip_id"), leg.get("player"), leg.get("stat_type")), _card("⚙️ SYSTEM HEALTH ALERT", [
                ("Status", "ACTION NEEDED"),
                ("Problem", "slip leg still ungraded %d hours after its match: %s %s %s %s (slip %s)"
                 % (hrs, leg.get("player"), leg.get("direction"), leg.get("line"), leg.get("stat_type"), leg.get("date"))),
                ("Likely cause", "player name doesn't match the results, or the result never settled"),
                ("Action", "grade it by hand in #slips-pnl or ask Claude to check the name")]))
    except Exception as e:
        print("[alerts] stuck slip check skipped: %s" % e)

    # 6. APEX-TOTALSETS-2026-10-13 (owner, "Yes"): a STANDARD Total Sets
    # line (LESS allowed). Two sets happen ~64% of the time overall and ~66-80%
    # when the favorite is 75%+ (47,826 Bo3 matches 2010-26); PrizePicks almost
    # always posts Total Sets as a MORE-only Demon, so a Standard one is rare.
    try:
        fav = {}
        with open(os.path.join(HERE, "apex_prop_sim_forward_log_v1.csv"), newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    pf = float(r.get("pinn_fav") or "")
                except ValueError:
                    continue
                names = [ops_norm(x) for x in str(r.get("opponent") or "").split(" v ")] if r.get("player") == "match" \
                    else [ops_norm(r.get("player")), ops_norm(r.get("opponent"))]
                if len(names) == 2:
                    fav[frozenset(names)] = pf
        two_set = [(0.90, "~80%"), (0.85, "~75%"), (0.80, "~72%"), (0.75, "~67%")]
        for cat, nm, line, ot, st, slug, opp in live:
            if slug != "total_sets" or ot != "standard" or not _future(st, now):
                continue
            pf = fav.get(frozenset((nm, opp)))
            if pf is not None and pf < 0.75:
                continue
            hist = next((h for lo, h in two_set if pf is not None and round(pf, 2) >= lo), None)
            once("totalsets|%s|%s|%s" % ("|".join(sorted((nm, opp))), line, st[:10]), _card("🎾 STANDARD TOTAL SETS LINE", [
                ("Match", "%s vs %s" % (nm.title(), opp.title())),
                ("Line", "Total Sets %g -- LESS is allowed (Standard)" % line),
                ("Favorite", ("%.0f%% (Pinnacle)" % (100 * pf)) if pf is not None else "unknown -- check the odds"),
                ("History", ("two sets %s of the time at this favorite level" % hist) if hist else "two sets ~64% of the time overall"),
                ("Action", "consider LESS 2.5 as a Standard pick")]))
    except Exception as e:
        print("[alerts] total sets check skipped: %s" % e)

    cutoff = (datetime.now() - timedelta(days=3)).isoformat()
    state["pipeline_log"] = {k: v for k, v in log.items() if v >= cutoff}
    for m in out:
        _send("alerts", m)
    return out


def skipped_props_line(t, live=None):
    """5. One daily-status line: live, not-started PrizePicks props the engine never evaluated, by reason."""
    now = datetime.now(timezone.utc)
    live = _live_board() if live is None else live
    picks, _ = t.load_current_board()
    ev_exact, ev_pc = set(), set()
    for p in picks:
        for nm in _names(p):
            try:
                ev_exact.add((p.get("category"), nm, float(p.get("line")), str(p.get("prop_type") or "standard").lower()))
            except (TypeError, ValueError):
                pass
            ev_pc.add((p.get("category"), nm))
    seen, why = set(), collections.Counter()
    for cat, nm, line, ot, st, slug, _opp in live:
        if not _future(st, now) or (cat, nm, line, ot) in ev_exact:
            continue
        if (cat, nm, line, ot) in seen:
            continue
        seen.add((cat, nm, line, ot))
        if "1st_set" in slug or "first_set" in slug:
            why["first-set markets (not modeled)"] += 1
        elif "/" in nm:
            why["doubles"] += 1
        elif (cat, nm) in ev_pc:
            why["line moved since the last fetch"] += 1
        else:
            why["never evaluated (no odds, unknown player, or posted after the last fetch)"] += 1
    if not why:
        return ["Every live PrizePicks prop is on the evaluated board"]
    return ["%d live props not on the evaluated board: %s" % (sum(why.values()), "; ".join("%d %s" % (n, k) for k, n in why.most_common()))]


def ops_norm(s):
    return str(s or "").strip().lower()


def _match_names(r):
    """Both players of the prop's match, whatever the row format."""
    if r.get("player") == "match":
        return frozenset(ops_norm(x) for x in str(r.get("opponent") or "").split(" v "))
    return frozenset((ops_norm(r.get("player")), ops_norm(r.get("opponent"))))


def health(state, t):
    out = []
    today = ops.now_ct().date().isoformat()
    def once(key, msg):
        if state.get("health_" + key) == today:
            return
        state["health_" + key] = today
        out.append(_card("⚙️ SYSTEM HEALTH ALERT", [("Status", "ACTION NEEDED"), ("Problem", msg)]))
    runs = ops._read_json(ops.PIPELINE_RUNS_FILE, [])
    fails = [r for r in runs if str(r.get("ts", ""))[:10] == today and r.get("event") == "fail"]
    if fails:
        once("pipeline_fail", "today's 5 PM fetch failed: %s. Run /fetch in Discord." % fails[-1].get("error", "?"))
    sh = ops._read_json(os.path.join(HERE, "apex_daily_settlement_health_report.json"), [])
    if isinstance(sh, list) and sh and str(sh[-1].get("system_health", "OK")).upper() != "OK":
        once("settlement", "settlement health is %s for %s (missing %s, duplicates %s)."
             % (sh[-1].get("system_health"), sh[-1].get("date"), sh[-1].get("missing_settlements"), sh[-1].get("duplicate_settlements")))
    # a real fetch that never got published, or a stale board
    try:
        snap = ops._read_json(t.PUBLISHED_BOARD_SNAPSHOT, {})
        cap = snap.get("captured_at", "")
        last_fetch = ""
        with open(t.PICK_LOG, newline="") as fh:
            for r in csv.DictReader(fh):
                if not str(r.get("price_source") or "").startswith("reprice:") and (r.get("ts") or "") > last_fetch:
                    last_fetch = r["ts"]
        seen = ops._read_json(os.path.join(ops.LOG_DIR, "fetch_seen.json"), {})
        if seen:
            last_fetch = max(last_fetch, max(seen.values()))
        if cap and last_fetch and last_fetch > cap and (datetime.fromisoformat(last_fetch) - datetime.fromisoformat(cap)).total_seconds() > 1800:
            once("publish", "a fetch ran at %s but the boards weren't republished (last publish %s). Run /fetch again."
                 % (last_fetch.replace("T", " ")[:16], cap.replace("T", " ")[:16]))
        if cap and (datetime.now() - datetime.fromisoformat(cap)).total_seconds() > 20 * 3600:
            once("stale", "the published boards are %d hours old. Run /fetch." % ((datetime.now() - datetime.fromisoformat(cap)).total_seconds() // 3600))
    except Exception as e:
        print("[alerts] stale-board check skipped: %s" % e)
    for m in out:
        _send("alerts", m)
    return out


def main():
    state = _load_state()
    if "--daily" in sys.argv:
        daily(state)
    if "--check" in sys.argv:
        check(state)
    _save_state(state)


if __name__ == "__main__":
    main()
