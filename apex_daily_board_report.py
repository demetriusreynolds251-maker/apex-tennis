#!/usr/bin/env python3
"""
APEX-DAILYREPORT-2026-10-10 (owner, approved "go" 2026-10-10):
the daily Board Report, posted to Discord #system at 9:00 AM CT by its own
LaunchAgent (com.apex.boardreport). Read-only except for refitting the
Honest Probability correction (apex_prob_correction_v1.json) first.

OFFICIAL WIN RATE (one definition, so the number stops changing):
  each prop on a board AS FIRST PUBLISHED for its match date (earliest
  capture in apex_board_status_log_v1.csv), recommended only, once per
  board, playable only, settled by the real result, pushes left out.
  Windows by match date: 7 / 14 / 30 days / lifetime, each with sample size
  and 95% range (Wilson). Boards before 2026-10-10 come from the older
  board log (any-time membership, old ranking) and are labelled "old system".

Sections: reliability, board performance + 3-leg hit rate, calibration
(raw vs Honest, 6 bands, trend), categories, board health funnel, closing
lines, alerts. Full text also saved to data/reports/board_report_<date>.md.

  python3 apex_daily_board_report.py            # refit, build, post
  python3 apex_daily_board_report.py --dry-run  # build and print only
"""
import csv, os, sys, io, contextlib, collections
from datetime import datetime, timedelta, date
import apex_ops as ops

HERE = ops.HERE
REPORT_DIR = os.path.join(HERE, "data", "reports")
BOARDS = [("top3", "Top 3"), ("elite6", "Elite 6"), ("top10", "Top 10"),
          ("bestgoblin", "Best Goblin"), ("beststandard", "Best Standard"), ("bestdemon", "Best Demon")]
TARGET = 0.70
ALERT_MIN_N = 20
BANDS = [(.60, .65), (.65, .70), (.70, .75), (.75, .80), (.80, .85), (.85, 1.01)]
NEW_SYSTEM_START = "2026-10-10"


def _n(s):
    return str(s or "").strip().lower()


def _rate(w, n):
    if not n:
        return "no settled props"
    lo, hi = ops.wilson(w, n)
    return "%.1f%% (%d-%d, n=%d, range %.0f-%.0f%%)" % (100.0 * w / n, w, n - w, n, 100 * lo, 100 * hi)


def _playable(p):
    return not (p.get("prop_type") in ("demon", "goblin") and p.get("direction") == "LESS")


class Settled:
    """Index of settled props for matching board rows to results."""
    def __init__(self, picks):
        self.idx = collections.defaultdict(list)
        for p in picks:
            if p.get("hit") is None:
                continue
            names = {_n(p.get("name"))} | {_n(x) for x in str(p.get("opponent") or "").split(" v ")}
            for nm in names:
                self.idx[(nm, p["category"], float(p["line"]))].append(p)

    def find(self, match_date, player, opponent, category, line, direction=None):
        try:
            line = float(line)
            d0 = date.fromisoformat(match_date)
        except Exception:
            return None
        cands = [player] if _n(player) != "match" else str(opponent or "").split(" v ")
        best = None
        for nm in cands:
            for p in self.idx.get((_n(nm), category, line), []):
                if direction and p.get("direction") != direction:
                    continue
                try:
                    dd = (date.fromisoformat(p["match_date"]) - d0).days
                except Exception:
                    continue
                if -1 <= dd <= 2 and (best is None or abs(dd) < best[0]):
                    best = (abs(dd), p)
        return best[1] if best else None


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def official_board_entries(status_rows, settled):
    """{board: [(match_date, hit)]} from the first publish per match date."""
    # APEX-OFFICIAL-2026-10-11: the official publish of a day is the first /fetch
    # or 5 PM publish (what actually gets bet); a restart/re-price republish is
    # used only on a day that had neither.
    first, first_any = {}, {}
    for r in status_rows:
        if r.get("board") in ("", "all") or r.get("status") != "recommended":
            continue
        k = (r["board"], r["match_date"])
        if k not in first_any or r["captured_at"] < first_any[k]:
            first_any[k] = r["captured_at"]
        if r.get("source") in ("manual", "auto") and (k not in first or r["captured_at"] < first[k]):
            first[k] = r["captured_at"]
    for k, v in first_any.items():
        first.setdefault(k, v)
    out = collections.defaultdict(list)
    seen = set()
    for r in status_rows:
        if r.get("status") != "recommended" or r.get("board") in ("", "all"):
            continue
        if r["captured_at"] != first.get((r["board"], r["match_date"])):
            continue
        if r.get("prop_type") in ("demon", "goblin") and r.get("direction") == "LESS":
            continue
        key = (r["board"], r["match_date"], _n(r["player"]), r["category"], r["line"])
        if key in seen:
            continue
        seen.add(key)
        m = settled.find(r["match_date"], r["player"], r["opponent"], r["category"], r["line"], r.get("direction"))
        out[r["board"]].append((r["match_date"], None if m is None else m["hit"], r.get("position")))
    return out


def legacy_board_entries(settled):
    """Old board log (Sep 29 - Oct 9): any-time membership, old ranking."""
    rows = _read_csv(os.path.join(HERE, "apex_board_membership_log_v1.csv"))
    out = collections.defaultdict(list)
    seen = set()
    for r in rows:
        if r.get("date", "") >= NEW_SYSTEM_START:
            continue
        for b, _ in BOARDS:
            if r.get(b) != "1":
                continue
            key = (b, r["date"], r["player"], r["category"], r["line"])
            if key in seen:
                continue
            seen.add(key)
            m = settled.find(r["date"], r["player"], r["opponent"], r["category"], r["line"])
            if m is not None and not _playable(m):
                continue
            out[b].append((r["date"], None if m is None else m["hit"], ""))
    return out


def window_stats(entries, today, days=None):
    lo = (today - timedelta(days=days - 1)).isoformat() if days else "0000"
    dec = [h for d, h, _ in entries if d >= lo and h is not None]
    return sum(1 for h in dec if h), len(dec)


def three_leg(entries):
    """Days where the board's first-published top 3 all settled: hit days / full days."""
    by_day = collections.defaultdict(list)
    for d, h, pos in entries:
        try:
            if int(pos) <= 3:
                by_day[d].append(h)
        except Exception:
            continue
    full = [hs for hs in by_day.values() if len(hs) >= 3 and all(h is not None for h in hs[:3])]
    return sum(1 for hs in full if all(hs[:3])), len(full)


NEW_ENGINE_START = "2026-10-11"   # surface fix + ATP opponent aces + re-pricing went live


def top3_tracker(entries, today):
    """APEX-TOP3TRACKER-2026-10-11 (owner, 2026-10-11): the Top 3 board
    as a 3-leg slip, from the official first publish of each day. Returns text
    lines: per-leg win rate, 3/2/1/0-for-3 days and projected winning days per
    week (= 7 x share of complete days that went 3-for-3), for the last 7, 14
    and 30 days and lifetime since the new engine."""
    by_day = collections.defaultdict(dict)
    for d, h, pos in entries:
        try:
            pos = int(pos)
        except Exception:
            continue
        if pos <= 3:
            by_day[d][pos] = h
    L = ["**Top 3 Tracker** (Top 3 board as a 3-leg slip, first publish of each day, settled days only)"]
    for lab, lo in (("Last 7 days", (today - timedelta(days=6)).isoformat()),
                    ("Last 14 days", (today - timedelta(days=13)).isoformat()),
                    ("Last 30 days", (today - timedelta(days=29)).isoformat()),
                    ("Lifetime since new engine (%s)" % NEW_ENGINE_START, NEW_ENGINE_START)):
        days = [v for d, v in by_day.items() if d >= max(lo, NEW_ENGINE_START if "Lifetime" in lab else lo)]
        full = [v for v in days if all(v.get(i) is not None for i in (1, 2, 3))]
        legs = [h for v in full for h in (v[1], v[2], v[3])]
        if not full:
            L.append("  %s: no complete settled days yet" % lab)
            continue
        cnt = collections.Counter(sum(1 for i in (1, 2, 3) if v[i]) for v in full)
        w = sum(1 for h in legs if h)
        L.append("  %s: per-leg %.1f%% (%d-%d) | 3-for-3 %d, 2-for-3 %d, 1-for-3 %d, 0-for-3 %d (of %d days) | projected winning days/week %.1f"
                 % (lab, 100.0 * w / len(legs), w, len(legs) - w, cnt[3], cnt[2], cnt[1], cnt[0], len(full),
                    7.0 * cnt[3] / len(full)))
    return L


def main():
    dry = "--dry-run" in sys.argv
    with contextlib.redirect_stderr(io.StringIO()):
        import apex_edge_tracker as t
        coef = t.fit_prob_correction(write=not dry) or t._prob_correction_coef()
        sp = t.settled_picks()
    today = ops.now_ct().date()
    settled = Settled(sp)
    status_rows = _read_csv(t.BOARD_STATUS_LOG)
    off = official_board_entries(status_rows, settled)
    leg = legacy_board_entries(settled)
    alerts = []
    L = ["\U0001F4CB **OpenClaw Daily Board Report** -- %s (%s)" % (today.isoformat(), ops.ct_str())]

    # ---- reliability
    hb = ops._read_json(ops.HEARTBEAT_FILE, {})
    runs = ops._read_json(ops.PIPELINE_RUNS_FILE, [])
    L.append("")
    L.append("**Reliability**")
    L.append("Last scheduler heartbeat: %s" % (hb.get("ts", "none on file")))
    missed = []
    for i in range(1, 8):
        d = (today - timedelta(days=i)).isoformat()
        evs = [r.get("event") for r in runs if str(r.get("ts", ""))[:10] == d]
        if d >= NEW_SYSTEM_START and "finish" not in evs:
            missed.append("%s (%s)" % (d, "failed" if "fail" in evs else "started, never finished" if "start" in evs else "never ran"))
    L.append("5 PM runs missed in the last 7 days: %s" % (", ".join(missed) if missed else "none"))
    if missed:
        alerts.append("5 PM run missed: " + ", ".join(missed))

    # ---- board performance
    L.append("")
    L.append("**Board performance -- official** (first publish of the day, playable, settled)")
    for b, label in BOARDS:
        ent = off.get(b, [])
        parts = []
        for lab, days in (("7d", 7), ("14d", 14), ("30d", 30), ("all", None)):
            w, n = window_stats(ent, today, days)
            parts.append("%s %s" % (lab, ("%.1f%% n=%d" % (100.0 * w / n, n)) if n else "--"))
        w14, n14 = window_stats(ent, today, 14)
        line = "%s: %s" % (label, " | ".join(parts))
        if b in ("top3", "elite6", "top10", "bestgoblin"):
            hit, full = three_leg(ent)
            if full:
                line += " | 3-leg all hit %d of %d days" % (hit, full)
        L.append(line)
        if n14 >= ALERT_MIN_N and w14 / n14 < TARGET:
            alerts.append("%s 14-day win rate %s is under 70%%" % (label, _rate(w14, n14)))
    L.append("_Old system, Sep 29-Oct 9 (any-time board membership, old ranking):_ " + "; ".join(
        "%s %s" % (label, ("%.1f%% n=%d" % (100.0 * window_stats(leg.get(b, []), today)[0] / window_stats(leg.get(b, []), today)[1],
                                            window_stats(leg.get(b, []), today)[1])) if window_stats(leg.get(b, []), today)[1] else "--")
        for b, label in BOARDS))

    # ---- Top 3 tracker (APEX-TOP3TRACKER-2026-10-11)
    L.append("")
    L.extend(top3_tracker(off.get("top3", []), today))
    L.append("  _Reference: replay of Oct 1-10 with the new engine -- per-leg 86.7% (26-4), 3-for-3 on 6 of 10 days, "
             "~4.2 winning days/week. Old-engine live boards: ~3 of 10 days._")

    # ---- calibration
    playable = [p for p in sp if p.get("hit") is not None and _playable(p) and p.get("category") != "fantasy_score"]
    lo30 = (today - timedelta(days=30)).isoformat()
    lo14 = (today - timedelta(days=14)).isoformat()
    lo28 = (today - timedelta(days=28)).isoformat()

    def bp(p):
        return p["prob_cal"] if p.get("prob_cal") not in (None, "") else (p.get("prob") or 0)

    L.append("")
    L.append("**Calibration** (last 30 days, playable, Fantasy Score excluded) -- stated vs won; Honest = corrected probability")
    if coef:
        L.append("Correction today: stated 70%% -> %d%%, 75%% -> %d%%, 80%% -> %d%% (fit on %d settled)"
                 % tuple([round(100 * t.corrected_prob(x, coef)) for x in (.70, .75, .80)] + [coef.get("n", 0)]))
    for a, b in BANDS:
        xs = [p for p in playable if p["match_date"] >= lo30 and a <= bp(p) < b]
        if not xs:
            L.append("  %d-%d%%: no settled props" % (a * 100, min(b, 1) * 100)); continue
        w = sum(1 for p in xs if p["hit"]); n = len(xs)
        exp = sum(bp(p) for p in xs) / n
        hon = [t.corrected_prob(bp(p), coef) for p in xs] if coef else []
        hexp = (sum(hon) / len(hon)) if hon and None not in hon else None
        gap = w / n - exp
        flag = " OVERCONFIDENT" if (gap < -0.05 and n >= 30) else " underconfident" if (gap > 0.05 and n >= 30) else ""
        recent = [p for p in xs if p["match_date"] >= lo14]
        prior = [p for p in xs if p["match_date"] < lo14]
        trend = ""
        if len(recent) >= 10 and len(prior) >= 10:
            g1 = sum(1 for p in recent if p["hit"]) / len(recent) - sum(bp(p) for p in recent) / len(recent)
            g0 = sum(1 for p in prior if p["hit"]) / len(prior) - sum(bp(p) for p in prior) / len(prior)
            trend = " | gap trend %+.1f -> %+.1f" % (100 * g0, 100 * g1)
        L.append("  %d-%d%%: won %.1f%% vs stated %.1f%% (gap %+.1f)%s, Honest said %s, n=%d%s"
                 % (a * 100, min(b, 1) * 100, 100.0 * w / n, 100 * exp, 100 * gap,
                    flag, ("%.1f%%" % (100 * hexp)) if hexp is not None else "--", n, trend))
        if flag == " OVERCONFIDENT":
            alerts.append("Calibration: %d-%d%% props won %.1f%% vs %.1f%% stated (n=%d)" % (a * 100, min(b, 1) * 100, 100.0 * w / n, 100 * exp, n))
    # Honest Probability's own check (the gate-switch test)
    if coef:
        since = [p for p in playable if p["match_date"] >= NEW_SYSTEM_START]
        hx = [p for p in since if (t.corrected_prob(bp(p), coef) or 0) >= 0.70]
        w = sum(1 for p in hx if p["hit"])
        L.append("Honest 70%%+ check since %s (switch test needs 100 props winning 72%%+): %s"
                 % (NEW_SYSTEM_START, _rate(w, len(hx))))

    # ---- categories
    L.append("")
    L.append("**Categories** (last 30 days, playable)")
    for cat, label in (("total_games", "Total Games"), ("aces", "Aces"), ("breaks", "Breaks"), ("games_won", "Games Won"),
                       ("total_sets", "Sets"), ("dfs", "Double Faults"), ("fantasy_score", "Fantasy Score")):
        xs = [p for p in sp if p.get("hit") is not None and _playable(p) and p.get("category") == cat and p["match_date"] >= lo30]
        if not xs:
            L.append("  %s: no settled props" % label); continue
        w = sum(1 for p in xs if p["hit"]); n = len(xs); exp = sum(bp(p) for p in xs) / n
        hi = [p for p in xs if bp(p) >= .70]; hw = sum(1 for p in hi if p["hit"])
        status = ("OFF boards (returns at 70%+ winning 70% on 50+)" if cat in t.BOARD_EXCLUDED_CATEGORIES
                  else "off ranked boards" if cat == "fantasy_score" else "on boards")
        L.append("  %s: won %.1f%% vs stated %.1f%% (gap %+.1f, n=%d) | 70%%+: %s | %s"
                 % (label, 100.0 * w / n, 100 * exp, 100 * (w / n - exp), n,
                    ("%.1f%% n=%d" % (100.0 * hw / len(hi), len(hi))) if hi else "--", status))
        if cat in t.BOARD_EXCLUDED_CATEGORIES and len(hi) >= 50 and hw / len(hi) >= 0.70:
            alerts.append("%s now meets its return rule (70%%+ props %.1f%% on %d) -- review bringing it back" % (label, 100.0 * hw / len(hi), len(hi)))

    # ---- board health (latest publish)
    L.append("")
    if status_rows:
        last = max(r["captured_at"] for r in status_rows)
        cur = [r for r in status_rows if r["captured_at"] == last]
        allr = [r for r in cur if r["board"] == "all"]
        L.append("**Board health** -- latest publish %s (%s), %d props" % (last.replace("T", " "), cur[0].get("source"), len(allr)))
        for why, n in collections.Counter(r["reason"] for r in allr).most_common():
            L.append("  %s: %d" % (why, n))
        for b, label in BOARDS:
            c = collections.Counter(r["status"] for r in cur if r["board"] == b)
            L.append("  %s: %d recommended, %d held back, %d watchlist, %d qualified but not shown"
                     % (label, c["recommended"], c["held_back"], c["watchlist"], c["not_shown"]))
        hb_reasons = collections.Counter(r["reason"] for r in cur if r["status"] == "held_back")
        if hb_reasons:
            L.append("  Held-back reasons: " + "; ".join("%s (%d)" % kv for kv in hb_reasons.most_common(4)))
    else:
        L.append("**Board health** -- no publish logged yet")
    fh = ops._read_json(os.path.join(ops.LOG_DIR, "fetch_health_history.json"), [])
    if isinstance(fh, list) and fh:
        x = fh[-1]
        L.append("  Last manual /fetch funnel (%s): %s available, %s declined before scoring %s, %s wrong-way on MORE-only lines"
                 % (x.get("timestamp"), x.get("raw_scraped"), x.get("declined"),
                    {k: v for k, v in (x.get("decline_breakdown") or {}).items() if v and k != "more_only_conflicts"},
                    x.get("direction_impossible")))

    # ---- surface mapping (APEX-SURFACEMAP-2026-10-10)
    um = ops._read_json(os.path.join(ops.LOG_DIR, "unmapped_tournaments.json"), {})
    if um:
        L.append("  Tournaments with no known surface (simulated as hard): " + ", ".join(sorted(um)))
        alerts.append("Unmapped tournament surface: " + ", ".join(sorted(um)))

    # ---- closing lines
    clv = _read_csv(os.path.join(HERE, "apex_closing_line_log_v1.csv"))
    L.append("")
    if clv:
        moved = [r for r in clv if r.get("clv") in ("beat", "lost")]
        beat = sum(1 for r in moved if r["clv"] == "beat")
        L.append("**Closing lines** -- %d board props captured before start; line moved on %d: our way %d (%s)"
                 % (len(clv), len(moved), beat, ("%.0f%%" % (100.0 * beat / len(moved))) if moved else "--"))
        res = collections.Counter()
        for r in moved:
            m = settled.find(r["match_date"], r["player"], r["opponent"], r["category"], r["rec_line"], r.get("direction"))
            if m is not None:
                res[(r["clv"], m["hit"])] += 1
        if res:
            L.append("  Picks when line moved our way: %d-%d; against us: %d-%d"
                     % (res[("beat", True)], res[("beat", False)], res[("lost", True)], res[("lost", False)]))
    else:
        L.append("**Closing lines** -- none captured yet")

    # ---- alerts
    L.append("")
    L.append("**Alerts**: " + ("none" if not alerts else ""))
    for a in alerts:
        L.append("⚠️ " + a)

    # ---- new engine validation (APEX-ENGINEVALIDATION-2026-10-11)
    try:
        import apex_engine_validation as _ev
        _evtext, _evsum, _evpath = _ev.build(today)
        L.append("")
        L.extend(_evsum)
    except Exception as e:
        L.append("New Engine Validation unavailable (%s)" % type(e).__name__)

    text = "\n".join(L)
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, "board_report_%s.md" % today.isoformat()), "w") as f:
        f.write(text + "\n")
    print(text)
    # APEX-ALERTS-2026-10-11 (owner: "Discord notifications, not a
    # dashboard"): the long report is saved to data/reports/ only; Discord gets
    # the change-only alerts plus one short OPENCLAW DAILY STATUS.
    if not dry:
        try:
            import apex_alerts as _al
            _st = _al._load_state()
            _al.daily(_st)
            _al._save_state(_st)
        except Exception as e:
            ops.send_discord("alerts", "⚙️ **System Health Alert** -- daily status failed to build (%s: %s)" % (type(e).__name__, e))


if __name__ == "__main__":
    main()
