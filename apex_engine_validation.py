#!/usr/bin/env python3
"""
APEX-ENGINEVALIDATION-2026-10-11 (owner, 2026-10-11): New Engine
Validation Report -- proves (or disproves) from LIVE SETTLED results that the
2026-10-11 changes (correct surface, ATP opponent aces, indoor aces, re-pricing,
Fantasy Score back, keep-on-price-failure) beat the old engine. Read-only.

Engine split: a board publish (apex_board_status_log_v1.csv) at or after
NEW_ENGINE_START is "new engine"; earlier publishes, and the older board log
(apex_board_membership_log_v1.csv, Sep 29 - Oct 10), are "old engine". For the
old board log the first capture of each day is used; for the status log the
first publish of each day (the official measurement).

Writes data/reports/engine_validation_<date>.md and returns a short summary
for the daily Discord report.

  python3 apex_engine_validation.py          # write the report, print it
"""
import csv, os, sys, io, contextlib, collections, math
from datetime import date, timedelta
import apex_ops as ops

HERE = ops.HERE
NEW_ENGINE_START = "2026-10-11T07:50:00"   # services restarted with the new engine
BOARDS = [("top3", "Top 3"), ("elite6", "Elite 6"), ("top10", "Top 10"), ("bestgoblin", "Best Goblin")]
GOAL_LO, GOAL_HI = 3.0, 4.0
MIN_DAYS_FOR_VERDICT = 7


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _wil(w, n):
    lo, hi = ops.wilson(w, n)
    return "%.0f-%.0f%%" % (100 * lo, 100 * hi)


def _rate(w, n):
    return ("%.1f%% (%d-%d, n=%d, range %s)" % (100.0 * w / n, w, n - w, n, _wil(w, n))) if n else "no settled props"


def _read(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load():
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        import apex_edge_tracker as t
        import apex_daily_board_report as rep
        sp = t.settled_picks()
    S = rep.Settled(sp)
    status = _read(t.BOARD_STATUS_LOG)
    return t, rep, S, status


def _moved_lines():
    """APEX-PULLEDNEWLINE-2026-10-22: (board, match_date, player, category, old line, side) -> new line
    for numbered picks PrizePicks moved after the fetch that were kept at the new line, so the
    board record grades them at the line actually shown (data/logs/pulled_lines.csv)."""
    out = {}
    for r in _read(os.path.join(HERE, "data", "logs", "pulled_lines.csv")):
        if r.get("outcome") == "new_line_kept" and _f(r.get("new_line")) is not None and _f(r.get("line")) is not None:
            out[(r.get("board"), r.get("match_date"), str(r.get("player") or "").strip().lower(), r.get("market"),
                 _f(r.get("line")), str(r.get("side") or "").upper())] = r["new_line"]
    return out


def board_days(status, S, board, era):
    """{match_date: [ (position, row, hit) ]} from the first publish per day, for one era."""
    moved = _moved_lines()
    # official publish of the day = first /fetch or 5 PM publish (what gets bet);
    # restart / re-price republishes count only on a day that had neither
    first, first_any = {}, {}
    for r in status:
        if r.get("board") != board or r.get("status") != "recommended":
            continue
        new = r["captured_at"] >= NEW_ENGINE_START
        if (era == "new") != new:
            continue
        k = r["match_date"]
        if k not in first_any or r["captured_at"] < first_any[k]:
            first_any[k] = r["captured_at"]
        if r.get("source") in ("manual", "auto") and (k not in first or r["captured_at"] < first[k]):
            first[k] = r["captured_at"]
    for k, v in first_any.items():
        first.setdefault(k, v)
    out = collections.defaultdict(list)
    for r in status:
        if r.get("board") != board or r.get("status") != "recommended" or first.get(r["match_date"]) != r["captured_at"]:
            continue
        _nl = moved.get((board, r["match_date"], str(r.get("player") or "").strip().lower(), r["category"],
                         _f(r["line"]), str(r.get("direction") or "").upper()))
        if _nl:   # APEX-PULLEDNEWLINE-2026-10-22: graded at PrizePicks' new line
            r = dict(r, line=_nl, moved_from=r["line"])
        m = S.find(r["match_date"], r["player"], r["opponent"], r["category"], r["line"], r.get("direction"))
        out[r["match_date"]].append((int(r.get("position") or 0), r, None if m is None else m["hit"]))
    return out


def legacy_days(S, board):
    """Old board log: first capture of each day (a capture ends when a prop repeats)."""
    rows = _read(os.path.join(HERE, "apex_board_membership_log_v1.csv"))
    caps, cur, seen = [], None, set()
    for r in rows:
        k = (r["player"], r["opponent"], r["category"], r["line"])
        if cur is None or r["date"] != cur["date"] or k in seen:
            cur = {"date": r["date"], "rows": []}; caps.append(cur); seen = set()
        seen.add(k); cur["rows"].append(r)
    out, done = collections.defaultdict(list), set()
    for c in caps:
        if c["date"] in done or c["date"] >= NEW_ENGINE_START[:10]:
            continue
        done.add(c["date"])
        for r in c["rows"]:
            if r.get(board) == "1":
                m = S.find(r["date"], r["player"], r["opponent"], r["category"], r["line"])
                out[r["date"]].append((0, r, None if m is None else m["hit"]))
    return out


def slip_stats(days, lo=None, hi=None, legs=3):
    """per-leg + k-for-3 days, using the top `legs` props of each day (position order when known)."""
    full, legs_all = [], []
    for d, items in days.items():
        if (lo and d < lo) or (hi and d > hi):
            continue
        items = sorted(items, key=lambda x: (x[0] or 99))[:legs]
        hs = [h for _, _, h in items]
        legs_all += [h for h in hs if h is not None]
        if len(hs) == legs and None not in hs:
            full.append(sum(1 for h in hs if h))
    c = collections.Counter(full)
    w = sum(1 for h in legs_all if h)
    return {"legs_w": w, "legs_n": len(legs_all), "days": len(full), "c": c,
            "per_week": (7.0 * c[legs] / len(full)) if full else None}


def _slip_line(st):
    if not st["legs_n"]:
        return "no settled props"
    s = "per-leg %s · 3-for-3 %d, 2-for-3 %d, 1-for-3 %d, 0-for-3 %d (of %d complete days)" % (
        _rate(st["legs_w"], st["legs_n"]), st["c"][3], st["c"][2], st["c"][1], st["c"][0], st["days"])
    if st["per_week"] is not None:
        s += " · winning days/week %.1f" % st["per_week"]
    return s


def new_engine_props(status, S, t):
    """Every prop the new engine published (board 'all' rows), first appearance, with result."""
    seen, out = set(), []
    for r in sorted(status, key=lambda r: r["captured_at"]):
        if r.get("board") != "all" or r["captured_at"] < NEW_ENGINE_START:
            continue
        k = (r["match_date"], r["player"].lower(), r["opponent"].lower(), r["category"], r["line"], r.get("direction"))
        if k in seen:
            continue
        seen.add(k)
        m = S.find(r["match_date"], r["player"], r["opponent"], r["category"], r["line"], r.get("direction"))
        out.append(dict(r, hit=None if m is None else m["hit"]))
    return out


def build(today=None):
    t, rep, S, status = load()
    today = today or ops.now_ct().date()
    W = {"Last 7 days": (today - timedelta(days=6)).isoformat(), "Last 14 days": (today - timedelta(days=13)).isoformat(),
         "Last 30 days": (today - timedelta(days=29)).isoformat()}
    L = ["# New Engine Validation Report -- %s" % today.isoformat(), "",
         "New engine = boards published from %s CT (surface fix, ATP opponent aces, indoor aces, re-pricing, "
         "Fantasy Score back, keep-on-price-failure). Only LIVE SETTLED results are used; nothing here is a replay."
         % NEW_ENGINE_START.replace("T", " ")[:16], ""]
    summary = []

    # 1. performance, old vs new, Top 3 as a 3-leg slip
    L += ["## 1. New vs old engine -- Top 3 as a 3-leg slip", ""]
    old_days = legacy_days(S, "top3")
    for d, v in board_days(status, S, "top3", "old").items():
        old_days[d] = v
    new_days = board_days(status, S, "top3", "new")
    L.append("| Window | Old engine | New engine |")
    L.append("|---|---|---|")
    for lab, lo in list(W.items()) + [("New engine lifetime", NEW_ENGINE_START[:10])]:
        o = slip_stats(old_days, lo=lo) if lab != "New engine lifetime" else slip_stats(old_days)
        n = slip_stats(new_days, lo=lo)
        L.append("| %s | %s | %s |" % (lab if lab != "New engine lifetime" else "Lifetime (old: all; new: since start)",
                                        _slip_line(o), _slip_line(n)))
    new_life = slip_stats(new_days)
    old_life = slip_stats(old_days)
    L.append("")

    # 2. Top 3 audit
    L += ["## 2. Top 3 audit (new engine, every Top 3 prop)", ""]
    rows = [(d, pos, r, h) for d, items in sorted(new_days.items()) for pos, r, h in sorted(items, key=lambda x: x[0])]
    if rows:
        L.append("| Day | # | Prop | Category | Probability | Apex | Honest | Result |")
        L.append("|---|---|---|---|---|---|---|---|")
        for d, pos, r, h in rows:
            who = r["opponent"] if r["player"] == "match" else "%s vs %s" % (r["player"], r["opponent"])
            L.append("| %s | %d | %s %s %s | %s | %.1f%% | %s | %s | %s |" % (
                d, pos, who, r["line"], r["direction"], r["category"], 100 * (_f(r["prob"]) or 0), r.get("apex_score") or "",
                ("%.1f%%" % (100 * _f(r["prob_honest"]))) if _f(r.get("prob_honest")) else "", {True: "WON", False: "lost", None: "pending"}[h]))
        dec = [(r, h) for _, _, r, h in rows if h is not None]
        if len(dec) >= 10:
            for metric in ("prob", "apex_score", "prob_honest"):
                wv = [_f(r[metric]) for r, h in dec if h and _f(r.get(metric)) is not None]
                lv = [_f(r[metric]) for r, h in dec if not h and _f(r.get(metric)) is not None]
                if wv and lv:
                    L.append("- %s: winners average %.3f, losers %.3f (gap %+.3f)" % (metric, sum(wv) / len(wv), sum(lv) / len(lv),
                                                                                       sum(wv) / len(wv) - sum(lv) / len(lv)))
        else:
            L.append("- Which metric predicts winners: not enough settled Top 3 props yet (%d; need 10+)." % len(dec))
    else:
        L.append("No new-engine Top 3 props published yet.")
    L.append("")

    # 3. Aces validation
    props = new_engine_props(status, S, t)
    L += ["## 3. Aces validation (props evaluated since the ATP opponent-aces update)", ""]
    aces = [p for p in props if p["category"] == "aces"]
    a70 = [p for p in aces if (_f(p["prob"]) or 0) >= 0.70 and p["hit"] is not None]
    blocked = [p for p in a70 if p["reason"] == "Apex Score under 67"]
    allowed = [p for p in a70 if p["status"] == "eligible"]
    board_aces = [(r, h) for items in new_days.values() for _, r, h in items if r["category"] == "aces" and h is not None]
    L.append("- Aces at 70%%+: %s" % _rate(sum(1 for p in a70 if p["hit"]), len(a70)))
    L.append("- Blocked by Apex Score only: %s" % _rate(sum(1 for p in blocked if p["hit"]), len(blocked)))
    L.append("- Allowed by Apex Score: %s" % _rate(sum(1 for p in allowed if p["hit"]), len(allowed)))
    L.append("- Aces on the Top 3 board: %s" % _rate(sum(1 for _, h in board_aces if h), len(board_aces)))
    if len(blocked) >= 15 and len(allowed) >= 15:
        bw = sum(1 for p in blocked if p["hit"]) / len(blocked); aw = sum(1 for p in allowed if p["hit"]) / len(allowed)
        ans = "Yes -- blocked Aces are winning more than allowed ones" if bw > aw + 0.03 else (
              "No -- the score is still separating better Aces" if aw > bw + 0.03 else "No clear difference yet")
    else:
        ans = "Not enough data yet (need 15+ blocked and 15+ allowed settled Aces at 70%%+; have %d and %d)" % (len(blocked), len(allowed))
    L.append("- **Would removing Apex Score improve Aces board performance today?** %s" % ans)
    L.append("")

    # 4. Honest vs raw
    L += ["## 4. Honest Probability vs raw probability (new-engine props, playable, settled)", ""]
    dec = [p for p in props if p["hit"] is not None and _f(p.get("prob_honest")) is not None and p["reason"] != "unplayable direction (MORE-only line, engine says LESS)"]
    if dec:
        br_raw = sum((_f(p["prob"]) - p["hit"]) ** 2 for p in dec) / len(dec)
        br_hon = sum((_f(p["prob_honest"]) - p["hit"]) ** 2 for p in dec) / len(dec)
        L.append("Accuracy (Brier, lower is better): raw %.4f vs Honest %.4f on %d props" % (br_raw, br_hon, len(dec)))
        L.append("")
        L.append("| Band | Raw: won vs stated | Honest: won vs stated |")
        L.append("|---|---|---|")
        for lo_, hi_ in ((.6, .65), (.65, .7), (.7, .75), (.75, .8), (.8, .85), (.85, 1.01)):
            def band(key):
                xs = [p for p in dec if lo_ <= _f(p[key]) < hi_]
                if not xs:
                    return "--"
                w = sum(1 for p in xs if p["hit"])
                return "%.1f%% vs %.1f%% (n=%d)" % (100.0 * w / len(xs), 100 * sum(_f(p[key]) for p in xs) / len(xs), len(xs))
            L.append("| %d-%d%% | %s | %s |" % (lo_ * 100, min(hi_, 1) * 100, band("prob"), band("prob_honest")))
        h70 = [p for p in dec if _f(p["prob_honest"]) >= 0.70]
        hw = sum(1 for p in h70 if p["hit"])
        earned = (len(h70) >= 100 and hw / len(h70) >= 0.72 and br_hon < br_raw)
        L.append("")
        L.append("- Honest 70%%+ props: %s" % _rate(hw, len(h70)))
        L.append("- **Has Honest Probability earned the right to become a qualification gate?** %s" % (
            "Yes -- 100+ props, 72%+ wins, more accurate than raw" if earned else
            "Not yet -- needs 100 settled Honest-70%%+ props winning 72%%+ and a better Brier than raw (have %d)" % len(h70)))
    else:
        L.append("No settled new-engine props yet.")
    L.append("")

    # 5. Board quality
    L += ["## 5. Board quality (new engine, first publish of each day)", "", "| Board | Win rate | Trend (last 7 vs before) | Old engine |", "|---|---|---|---|"]
    for b, lab in BOARDS:
        nd = board_days(status, S, b, "new")
        allh = [h for items in nd.values() for _, _, h in items if h is not None]
        w = sum(1 for h in allh if h)
        lo7 = (today - timedelta(days=6)).isoformat()
        r7 = [h for d, items in nd.items() if d >= lo7 for _, _, h in items if h is not None]
        rb = [h for d, items in nd.items() if d < lo7 for _, _, h in items if h is not None]
        trend = "%s vs %s" % (("%.0f%%" % (100.0 * sum(r7) / len(r7))) if r7 else "--", ("%.0f%%" % (100.0 * sum(rb) / len(rb))) if rb else "--")
        od = legacy_days(S, b)
        oh = [h for items in od.values() for _, _, h in items if h is not None]
        flag = " **BELOW 70%%**" if allh and len(allh) >= 20 and w / len(allh) < 0.70 else ""
        L.append("| %s | %s%s | %s | %s |" % (lab, _rate(w, len(allh)), flag, trend, _rate(sum(1 for h in oh if h), len(oh))))
    L.append("")

    # 6. Missed opportunities
    L += ["## 6. Missed opportunities -- 70%+ props blocked only by Apex Score (new engine)", ""]
    miss = [p for p in props if p["reason"] == "Apex Score under 67"]
    md = [p for p in miss if p["hit"] is not None]
    elig = [p for p in props if p["status"] == "eligible" and p["hit"] is not None]
    L.append("- Blocked by score: %s" % _rate(sum(1 for p in md if p["hit"]), len(md)))
    L.append("- Passed the score (eligible): %s" % _rate(sum(1 for p in elig if p["hit"]), len(elig)))
    for p in sorted([p for p in md if p["hit"]], key=lambda p: -(_f(p["prob"]) or 0))[:15]:
        who = p["opponent"] if p["player"] == "match" else "%s vs %s" % (p["player"], p["opponent"])
        L.append("  - WON while blocked: %s %s %s %s -- %.0f%%, Apex %s" % (who, p["category"], p["line"], p["direction"], 100 * _f(p["prob"]), p["apex_score"]))
    if len(md) >= 30 and len(elig) >= 30:
        value = "still providing value" if sum(1 for p in elig if p["hit"]) / len(elig) > sum(1 for p in md if p["hit"]) / len(md) + 0.03 else "not providing clear value"
    else:
        value = "not enough data yet (need 30+ settled on each side; have %d blocked, %d passed)" % (len(md), len(elig))
    L.append("- **Is the score filter still providing value?** %s" % value)
    L.append("")

    # 7. Best of the best
    L += ["## 7. Best-of-the-best check", ""]
    if new_life["days"] < MIN_DAYS_FOR_VERDICT:
        verdict = "Not enough data"
        why = "%d complete new-engine Top 3 day(s); a verdict needs %d+." % (new_life["days"], MIN_DAYS_FOR_VERDICT)
    else:
        pw = new_life["per_week"]
        old_pw = old_life["per_week"] or 0
        verdict = ("Exceeding goal" if pw > GOAL_HI else "Meeting goal" if pw >= GOAL_LO else
                   "Trending toward goal" if pw > old_pw else "Below goal (not beating the old engine)")
        why = "new engine %.1f winning days/week over %d days; old engine %.1f." % (pw, new_life["days"], old_pw)
    L.append("**If I bet only the Top 3 every day, is the new engine achieving 3-4 winning days per week?** **%s** -- %s" % (verdict, why))
    L.append("")

    # 8. Recommendation
    L += ["## 8. Apex Score recommendation", ""]
    if len(md) >= 30 and len(elig) >= 30:
        bw = sum(1 for p in md if p["hit"]) / len(md); ew = sum(1 for p in elig if p["hit"]) / len(elig)
        aces_flip = len(blocked) >= 15 and len(allowed) >= 15 and (sum(1 for p in blocked if p["hit"]) / len(blocked)) > (sum(1 for p in allowed if p["hit"]) / len(allowed)) + 0.03
        rec = ("Remove Apex Score filter" if bw >= ew - 0.01 else
               "Modify Apex Score filter (exempt Aces)" if aces_flip else "Keep Apex Score filter")
        ev = "blocked props won %.1f%% vs %.1f%% for props that passed." % (100 * bw, 100 * ew)
    else:
        rec = "Keep Apex Score filter (for now)"
        ev = ("live new-engine sample too small to change it (%d blocked, %d passed settled). Prior evidence: on old-engine "
              "history the filter added ~0.1 pt once Breaks were removed, hurt Aces (blocked 69.2%% vs allowed 63.6%%), and the "
              "Sep 29-Oct 10 board replay was mixed (Top 3 better, Elite 6 worse without it)." % (len(md), len(elig)))
    L.append("**%s** -- %s" % (rec, ev))

    text = "\n".join(L) + "\n"
    os.makedirs(os.path.join(HERE, "data", "reports"), exist_ok=True)
    path = os.path.join(HERE, "data", "reports", "engine_validation_%s.md" % today.isoformat())
    with open(path, "w") as fh:
        fh.write(text)
    summary = ["**New Engine Validation** -- Top 3 goal check: **%s** (%s)" % (verdict, why),
               "  New engine Top 3: %s" % _slip_line(new_life),
               "  Apex Score: %s" % rec,
               "  Full report: %s" % path]
    return text, summary, path


if __name__ == "__main__":
    text, summary, path = build()
    print(text)
