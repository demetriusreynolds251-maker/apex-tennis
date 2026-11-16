#!/usr/bin/env python3
"""
APEX-SIGNALTEST-2026-10-21 (owner: "the promising have them running in
the background but automatically update me when it's finish"): background test
of the 7 promising signals from the 10/21 sweep. Nothing is posted and nothing
on the boards or #slips changes. Every /fetch and 5 PM publish adds the day's
70%+ props you can play (no Goblin/Demon LESS, no Demons); the daily status
grades them; after FINISH_DAYS days a TEST FINISHED card posts once with each
signal's record and whether it held up.

Signals (10/21 quick-test result in brackets):
  6  Apex tier label: elite vs good vs caution      [elite 9-11, caution 75%]
  7  Projection past the line: 40%+ vs 25-40%        [68% vs 77%]
  8  Match length for Fantasy/Aces MORE (bet365 TG)  [long 76-78%, short 50-56%]
  9  PrizePicks posted the line under 8h before      [81% vs 72%]
  10 ATP vs WTA                                       [74% vs 68%]
  11 Clay vs hard                                     [68% vs 73%]
  12 Morning start (6 AM-12 PM CT)                    [69% vs 74-75%]
  13 Demon MORE at 60%+ (added 10/22)                 [14-17, 45%; a 3-Demon slip needs ~41%]

  python3 apex_signal_papertest.py           # print the running record
  python3 apex_signal_papertest.py --log-now # add today's props now
"""
import json, os, sqlite3, sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "data", "logs", "signal_papertest.json")
START = "2026-10-21"
MIN_PROB = 0.70
DEMON_MIN_PROB = 0.60   # APEX-DEMONBOARD-2026-10-22: signal 13, Demon MORE 60%+ (needs ~41% a leg)
FINISH_DAYS = 21
HOLD_GAP = 0.05       # a signal "held up" if its groups still differ by 5+ points ...
HOLD_MIN_N = 30       # ... with at least 30 graded props in each compared group


def _load():
    try:
        with open(LOG) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _pick_extra():
    import csv
    out = {}
    cut = (datetime.now().date() - timedelta(days=3)).isoformat()
    with open(os.path.join(HERE, "apex_engine_picks_log_v1.csv"), newline="") as fh:
        for r in csv.DictReader(fh):
            if (r.get("match_date") or "") < cut:
                continue
            out[(r.get("match_id"), r.get("category"), str(r.get("line")), str(r.get("prop_type") or "").lower())] = r
    return out


def log_today(picks=None, source="", today=None):
    """Adds today's 70%+ playable props (first sighting wins). Never raises."""
    try:
        import apex_edge_tracker as t
        today = today or datetime.now().date().isoformat()
        if today < START:
            return False
        if picks is None:
            picks, _ = t.load_current_board()
        data = _load()
        day = data.setdefault(today, {"props": {}})
        extra = _pick_extra()
        try:
            import apex_bet365_totals as b3   # APEX-BET365-THREEPART-2026-10-22: names + match date
        except Exception:
            b3 = None
        added = 0
        for p in picks:
            typ = str(p.get("prop_type") or "standard").lower()
            if t._is_unplayable(p) or t._pub_match_started(p):
                continue
            pr = t._board_prob(p)
            # APEX-DEMONBOARD-2026-10-22: Demon MORE at 60%+ is logged as its own group (13)
            if pr < (DEMON_MIN_PROB if typ == "demon" else MIN_PROB) - 1e-9:
                continue
            k = "|".join(str(x) for x in t._apex_prop_identity(p))
            if k in day["props"]:
                continue
            x = extra.get((p.get("match_id"), p.get("category"), str(p.get("line")), typ)) or {}
            names = [s.strip() for s in str(p.get("opponent") or "").split(" v ")] if p.get("player") == "match" \
                else [p.get("player"), p.get("opponent")]
            try:
                edge = float(p.get("edge"))
            except (TypeError, ValueError):
                edge = None
            day["props"][k] = {
                "match_date": p.get("match_date"), "player": p.get("player"), "opponent": p.get("opponent"),
                "category": p.get("category"), "line": p.get("line"), "direction": p.get("direction"),
                "prop_type": typ, "prob": round(pr, 4), "edge": edge, "tier": x.get("tier"),
                "surface": x.get("surface"), "women": "women" in str(x.get("format_label") or "").lower(),
                "start_utc": p.get("slip_start_utc"),
                "book_line": b3.total(names, p.get("match_date"))[0] if b3 else None,
                "logged_at": datetime.now().isoformat(timespec="seconds")}
            added += 1
        tmp = LOG + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1, default=str)
        os.replace(tmp, LOG)
        return added
    except Exception as e:
        print("[SignalPaperTest] log skipped (%s): %s" % (type(e).__name__, e))
        return False


def _first_seen(lg, con):
    nm = lg.get("player") if lg.get("player") != "match" else str(lg.get("opponent") or "").split(" v ")[0].strip()
    try:
        row = con.execute("select first_seen, start_utc from projections where player = ? and category = ? and "
                          "line_last = ? and odds_type = ? order by first_seen limit 1",
                          (nm, lg.get("category"), float(lg.get("line")), lg.get("prop_type"))).fetchone()
    except Exception:
        row = None
    return row


def _groups(lg, con):
    if str(lg.get("prop_type")) == "demon":   # APEX-DEMONBOARD-2026-10-22: only its own group
        p = float(lg.get("prob") or 0)
        return [("13 Demon MORE 60%+ (needs ~41%)", "70%+" if p >= 0.70 else "65-70%" if p >= 0.65 else "60-65%"),
                ("13 Demon MORE 60%+ (needs ~41%)", "all 60%+")]
    out = []
    t = str(lg.get("tier") or "")
    if t:
        out.append(("6 Apex tier label", t))
    try:
        rel = float(lg["edge"]) / max(float(lg["line"]), 1)
        out.append(("7 Projection past the line", "40%+" if rel >= 0.40 else "25-40%" if rel >= 0.25 else "under 25%"))
    except (TypeError, ValueError, KeyError):
        pass
    if lg.get("category") in ("fantasy_score", "aces") and str(lg.get("direction")).upper() == "MORE" and lg.get("book_line"):
        b = float(lg["book_line"])
        out.append(("8 Match length (Fantasy/Aces MORE)", "long 22.5+" if b >= 22.5 else "medium 21-22" if b >= 21 else "short under 21"))
    row = _first_seen(lg, con)
    if row and row[0] and (row[1] or lg.get("start_utc")):
        try:
            a = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).replace(tzinfo=None)
            b = datetime.fromisoformat(str(row[1] or lg.get("start_utc")).replace("Z", "+00:00")).replace(tzinfo=None)
            out.append(("9 How early PrizePicks posted", "under 8h before" if (b - a).total_seconds() < 8 * 3600 else "8h+ before"))
        except ValueError:
            pass
    out.append(("10 Tour", "WTA" if lg.get("women") else "ATP"))
    if lg.get("surface"):
        out.append(("11 Surface", str(lg["surface"])))
    try:
        h = (int(str(lg.get("start_utc"))[11:13]) - 5) % 24
        out.append(("12 Start time (CT)", "morning 6a-12p" if 6 <= h < 12 else "overnight 11p-6a" if (h >= 23 or h < 6) else "afternoon/evening"))
    except (TypeError, ValueError):
        pass
    return out


def _tally():
    data = _load()
    if not data:
        return None, 0
    import apex_engine_validation as ev
    _t, _rep, S, _st = ev.load()
    con = sqlite3.connect(os.path.join(HERE, "data", "props_db.sqlite"))
    T = {}
    for day, v in sorted(data.items()):
        for lg in (v.get("props") or {}).values():
            m = S.find(lg.get("match_date"), lg.get("player"), lg.get("opponent"), lg.get("category"),
                       lg.get("line"), lg.get("direction"))
            h = None if m is None else m.get("hit")
            if h is None:
                continue
            for g, b in _groups(lg, con):
                r = T.setdefault(g, {}).setdefault(b, [0, 0])
                r[0 if h else 1] += 1
    con.close()
    return T, len(data)


def summary():
    T, days = _tally()
    if T is None:
        return []
    if not T:
        return ["Signal background test (since %s): %d day(s) logged, nothing graded yet" % (START, days)]
    L = ["Signal background test since %s (%d days; 70%%+ props you can play; not used on boards)" % (START, days)]
    for g in sorted(T, key=lambda x: int(x.split()[0])):
        L.append("%s: %s" % (g, " · ".join("%s %d-%d (%.0f%%)" % (b, w, l, 100.0 * w / (w + l))
                                           for b, (w, l) in sorted(T[g].items()))))
    return L


def finish_check():
    """(True, card rows) once FINISH_DAYS days are logged; (False, None) before."""
    T, days = _tally()
    if not T or days < FINISH_DAYS:
        return False, None
    rows = [("Test", "7 promising signals, background test since %s (%d days)" % (START, days))]
    for g in sorted(T, key=lambda x: int(x.split()[0])):
        grp = {b: (w, l) for b, (w, l) in T[g].items()}
        big = [(b, w / float(w + l)) for b, (w, l) in grp.items() if w + l >= HOLD_MIN_N]
        held = len(big) >= 2 and max(r for _, r in big) - min(r for _, r in big) >= HOLD_GAP
        rows.append((g.split(" ", 1)[1],
                     "%s -- %s" % ("HELD UP" if held else "no real difference / too few",
                                   " · ".join("%s %d-%d" % (b, w, l) for b, (w, l) in sorted(grp.items())))))
    rows.append(("Decision", "pick which signals to show on cards, or keep testing"))
    return True, rows


if __name__ == "__main__":
    if "--log-now" in sys.argv:
        print("added %s props" % log_today(source="manual-cli"))
    print("\n".join(summary()) or "nothing logged yet")
