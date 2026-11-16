#!/usr/bin/env python3
"""
APEX-PPMIRROR-C-2026-10-23 (owner, "Go 1-8", steps 6 and 8): ParlayPlay's official
daily slip and its paper tests. ParlayPlay data only -- everything here lives in
~/openclaw/ApexParlayPlay and reads the ParlayPlay database; nothing is written to a
PrizePicks file. The rules it tests are the shared PrizePicks ones (apex_card_signals,
apex_top3_no175_papertest, apex_pinnacle), called, not copied.

OFFICIAL SLIPS (Slips v2 style; APEX-PPSLIPS369-2026-10-23: Top 3, Top 6 and Top 9)
  Three slips a day: the top 3, 6 and 9 of the ParlayPlay board as All In slips (fewer legs
  when fewer picks qualify -- then marked short and left out of the Top 6 / Top 9 record). A slip belongs to the CT date of its earliest leg. The OFFICIAL slip
  of a day = the last board built before that day's first ParlayPlay match starts; the
  first board of the day is kept as a column. $[amount] a slip at ParlayPlay's real payout (the
  product of the leg multipliers when the slip was built). A void leg is dropped and the
  payout recomputed on the rest; under 2 legs left = money back. Logged in
  data/official_slips.json, apart from the old every-slip log (the `slips` table).

PAPER TESTS (background, nothing ranks on them)
  Every numbered and BACKUPS prop, first sighting per day, logged to
  learning/papertests.json and graded from the ParlayPlay results:
    17.5 check   TG MORE at 17.5+: play / skip / not enough data (shared rule175)
    Pinnacle     TG: 3+ games of room vs under 3 (shared apex_pinnacle.room)
    Signals      net signal count +2 or better vs +1 or less (shared net_score)
    Board        numbered picks vs BACKUPS
  (The Aces players-close test has no ParlayPlay market, so it is not run.)

  python3 apex_pp_tracking.py   # status lines + last 7 days
"""
import json, os, sqlite3, sys
from datetime import datetime, timezone, timedelta

APP = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(APP)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
DB_PATH = os.path.join(APP, "data", "parlayplay_db.sqlite")
LOG = os.path.join(APP, "data", "official_slips.json")
TEST_LOG = os.path.join(APP, "learning", "papertests.json")
CT = timezone(timedelta(hours=-5))
START_DAY = "2026-10-23"
START_LABEL = "since 10/23"
STAKE = 10.0
OFFICIAL_LEGS = 3
MIN_LEGS = 2


def _load(p, default):
    try:
        with open(p) as fh:
            return json.load(fh)
    except Exception:
        return default


def _save(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as fh:
        json.dump(d, fh, indent=1)
    os.replace(p + ".tmp", p)


def _ct_day(iso):
    return datetime.fromisoformat(iso).astimezone(CT).date().isoformat()


def _day(d):
    x = datetime.fromisoformat(d)
    return "%s %d/%d" % (x.strftime("%a"), x.month, x.day)


def _money(x):
    return ("+$%.2f" % x) if x >= 0 else ("-$%.2f" % -x)


def _label(pp):
    who = "%s v %s" % (pp["home"], pp["away"]) if pp["player"] == "match" else pp["player"]
    return "%s %s %s %g" % (who, pp["category"].replace("_", " ").title(), pp["direction"], pp["line"])


# ------------------------------------------------------------------ official slips
# APEX-PPSLIPS369-2026-10-23 (owner: "allow me to pick up to 9 props on one slip, so we need
# the Top 3, Top 6 and Top 9"): three official slips a day -- the board's #1-3, #1-6 and #1-9 --
# each logged when its legs change, official = last board before the day's first match (first
# board kept as a column), graded as a whole slip at $[amount] with ParlayPlay's real All In payout. A
# Top 6 / Top 9 built when fewer picks qualified is logged as "short" (n < size): shown, but not
# counted in the Top 6 / Top 9 record. "One miss" is counted for 6 and 9 (it pays nothing on All
# In; it shows how often an Insured slip would have been saved, when ParlayPlay offers one).
# Entries logged before this change have no "size" and are Top 3.
SIZES = (3, 6, 9)
# The ParlayPlay job starts fresh every run, so these changes go live on its next run with no
# restart. LIVE = False keeps everything as before 10/23 (Top 3 card set, Top 3 log, Top 3 record)
# until owner says go; LIVE = True turns on the Top 3 / 6 / 9 cards, log and record.
LIVE = False


def sizes():
    return SIZES if LIVE else SIZES[:1]


def _size(e):
    return e.get("size") or OFFICIAL_LEGS


def log_build(b, board_ts=None):
    """Logs this build's Top 3 / Top 6 / Top 9 candidates when their legs differ from the last ones
    logged for their day. Returns the new entries. Never raises."""
    out = []
    try:
        board_ts = board_ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
        L = _load(LOG, [])
        for size in sizes():
            legs = list(b[:size])
            if len(legs) < MIN_LEGS:
                continue
            day = min(_ct_day(d["pp"]["start_utc"]) for d in legs)
            sig = sorted("%s|%s" % (d["pp"]["key"], d["pp"]["direction"]) for d in legs)
            same = [e for e in L if e["day"] == day and _size(e) == size]
            if same and sorted(same[-1]["sig"]) == sig:
                continue
            pay = 1.0
            for d in legs:
                pay *= float(d["pp"]["mult"])
            e = {"id": "%s|%d|%s" % (day, size, board_ts), "day": day, "size": size, "board_ts": board_ts,
                 "n": len(legs), "short": len(legs) < size, "sig": sig, "payout": round(pay, 4),
                 "insured_ok": all(float(d["pp"]["mult"]) >= 1.40 - 1e-9 for d in legs),
                 "status": "pending", "profit": None,
                 "legs": [{"key": d["pp"]["key"], "direction": d["pp"]["direction"], "mult": float(d["pp"]["mult"]),
                           "prob": round(float(d["pp"]["prob"]), 4), "start_utc": d["pp"]["start_utc"],
                           "label": _label(d["pp"]), "result": None} for d in legs]}
            L.append(e)
            out.append(e)
        if out:
            _save(LOG, L)
    except Exception as ex:
        print("[PPTracking] official slip log failed: %s: %s" % (type(ex).__name__, ex))
    return out


def _first_starts():
    """{CT day: first ParlayPlay match start (UTC iso)} from every line ParlayPlay listed."""
    out = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=10)
        for (st,) in c.execute("SELECT DISTINCT start_utc FROM lines WHERE start_utc >= ?",
                               ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),)):
            try:
                d = _ct_day(st)
            except Exception:
                continue
            if d not in out or st < out[d]:
                out[d] = st
        c.close()
    except Exception:
        pass
    return out


def roles(L=None):
    """{entry id: {"official", "first"}} per day and size; official only once the day's first match started."""
    L = _load(LOG, []) if L is None else L
    fs = _first_starts()
    now = datetime.now(timezone.utc)
    out = {}
    by = {}
    for e in L:
        by.setdefault((e["day"], _size(e)), []).append(e)
    for (d, _sz), es in by.items():
        es.sort(key=lambda e: e["board_ts"])
        f = fs.get(d) or min(l["start_utc"] for e in es for l in e["legs"])
        fdt = datetime.fromisoformat(f)
        before = [e for e in es if datetime.fromisoformat(e["board_ts"]) < fdt]
        if not before:
            continue
        out.setdefault(before[0]["id"], set()).add("first")
        if fdt <= now:
            out.setdefault(before[-1]["id"], set()).add("official")
    return out


def pick(role, L=None, size=OFFICIAL_LEGS):
    """{day: entry} for one slip size (3, 6 or 9)."""
    L = _load(LOG, []) if L is None else L
    r = roles(L)
    return {e["day"]: e for e in L if _size(e) == size and role in r.get(e["id"], ())}


def grade_entry(e, res):
    """Sets status / mult / profit / misses on one entry from its leg results (list of hit/miss/void/None)."""
    e["misses"] = res.count("miss")
    if None not in res:
        e["final"] = True
    if "miss" in res:
        e.update(status="lost", mult=0.0, profit=-STAKE)
    elif None in res:
        return False
    else:
        live = [l for l, r in zip(e["legs"], res) if r == "hit"]
        if len(live) < MIN_LEGS:
            e.update(status="void", mult=1.0, profit=0.0)
        else:
            m = 1.0
            for l in live:
                m *= l["mult"]
            e.update(status="won", mult=round(m, 4), profit=round(STAKE * m - STAKE, 2))
    return True


def grade():
    """Grades official-slip entries from the ParlayPlay results (keeps filling legs after a slip is
    lost, so "one miss" can be counted). Returns how many changed."""
    L = _load(LOG, [])
    if not L:
        return 0
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=10)
    except Exception:
        return 0
    changed = 0
    for e in L:
        if e.get("final"):
            continue
        moved = False
        for l in e["legs"]:
            if l.get("result") is None:
                r = c.execute("SELECT result FROM lines WHERE key=?", (l["key"],)).fetchone()
                if r and r[0]:
                    l["result"] = "void" if r[0] in ("void", "push") else ("hit" if r[0] == l["direction"].lower() else "miss")
                    moved = True
        before = (e["status"], e.get("misses", 0), e.get("final"))
        grade_entry(e, [l.get("result") for l in e["legs"]])
        if moved or before != (e["status"], e.get("misses", 0), e.get("final")):
            e["graded_at"] = datetime.now(CT).isoformat(timespec="seconds")
            changed += 1
    c.close()
    if changed:
        _save(LOG, L)
    return changed


def _tally(days):
    g = [e for e in days.values() if e["status"] != "pending" and not e.get("short")]
    return sum(e["status"] == "won" for e in g), len(g), sum(e.get("profit") or 0 for e in g)


def _one_miss(days):
    """Days whose full slip finished with exactly one miss (all legs graded)."""
    return sum(1 for e in days.values() if not e.get("short") and e.get("final") and e.get("misses") == 1)


def _summary_top3(L):
    """The Top 3-only record (as before the 3/6/9 change)."""
    off = pick("official", L)
    w, n, pl = _tally(off)
    if not n:
        waiting = len({e["day"] for e in L})
        return ["\U0001F4CA PARLAYPLAY SLIPS (%s): no finished days yet%s." % (
            START_LABEL, (" (%d day%s logged so far)" % (waiting, "" if waiting == 1 else "s")) if waiting else "")]
    fw, fn, fpl = _tally({d: v for d, v in pick("first", L).items() if d in off})
    return ["\U0001F4CA PARLAYPLAY SLIPS (%s): won %d of %d day%s (%s), $%d a slip at ParlayPlay's payout." % (
                START_LABEL, w, n, "" if n == 1 else "s", _money(pl), STAKE),
            "Playing off the first board instead: won %d of %d (%s)." % (fw, fn, _money(fpl))]


def summary_lines():
    L = _load(LOG, [])
    if not LIVE:
        return _summary_top3(L)
    o3, o6, o9 = (pick("official", L, s) for s in SIZES)
    if not any(_tally(o)[1] for o in (o3, o6, o9)):
        waiting = len({e["day"] for e in L})
        return ["\U0001F4CA PARLAYPLAY SLIPS (%s): no finished days yet%s." % (
            START_LABEL, (" (%d day%s logged so far)" % (waiting, "" if waiting == 1 else "s")) if waiting else "")]
    w3, n3, p3 = _tally(o3)
    w6, n6, p6 = _tally(o6)
    w9, n9, p9 = _tally(o9)
    out = ["\U0001F4CA PARLAYPLAY SLIPS (%s): Top 3 won %d of %d day%s (%s). Top 6: all hit %d of %d, one miss %d (%s). "
           "Top 9: all hit %d of %d, one miss %d (%s). $%d a slip at ParlayPlay's All In payout." % (
               START_LABEL, w3, n3, "" if n3 == 1 else "s", _money(p3), w6, n6, _one_miss(o6), _money(p6),
               w9, n9, _one_miss(o9), _money(p9), STAKE)]
    fb = []
    for s, o in zip(SIZES, (o3, o6, o9)):
        fw, fn, fpl = _tally({d: v for d, v in pick("first", L, s).items() if d in o})
        if fn:
            fb.append("Top %d won %d of %d (%s)" % (s, fw, fn, _money(fpl)))
    if fb:
        out.append("Playing off the first board instead: " + " · ".join(fb) + ".")
    return out


_ICON = {"won": "✅", "lost": "❌", "void": "➖"}


def _cell(e, size):
    if not e:
        return "Top %d --" % size
    if e.get("short"):
        return "Top %d only %d picks" % (size, e["n"])
    if e["status"] == "pending":
        return "Top %d %.2fx playing" % (size, e["payout"])
    miss = (" (%d miss%s)" % (e["misses"], "" if e["misses"] == 1 else "es")) if e["status"] == "lost" and e.get("final") else ""
    return "Top %d %s%s %s" % (size, _ICON[e["status"]], miss, _money(e["profit"] or 0))


def _day_lines_top3(n=7):
    """Before the 3/6/9 change (LIVE = False)."""
    L = _load(LOG, [])
    off, first = pick("official", L), pick("first", L)
    out = []
    for d in sorted({e["day"] for e in L}, reverse=True)[:n]:
        e = off.get(d)
        if not e:
            nb = sum(1 for x in L if x["day"] == d)
            out.append("%s: waiting for the first match (%d board change%s logged)" % (_day(d), nb, "" if nb == 1 else "s"))
            continue
        if e["status"] == "pending":
            out.append("%s: %d-pick %.2fx, playing now, waiting on results" % (_day(d), e["n"], e["payout"]))
            continue
        f = first.get(d)
        fx = ""
        if f and f["id"] != e["id"] and f["status"] != "pending":
            fx = " (first board: %s %s)" % ({"won": "✅", "lost": "❌", "void": "➖"}[f["status"]], _money(f["profit"] or 0))
        out.append("%s: %s %d-pick %.2fx · %s%s" % (
            _day(d), {"won": "✅", "lost": "❌", "void": "➖"}[e["status"]], e["n"], e["payout"],
            _money(e["profit"] or 0), fx))
    return out


def _ondemand_top3():
    return "\n".join(summary_lines() + ["**Last 7 days** (official slip = top 3 of the last board before the first "
                                        "match, $10 a slip)"] + (_day_lines_top3(7) or ["nothing yet"]))


def day_lines(n=7):
    if not LIVE:
        return _day_lines_top3(n)
    L = _load(LOG, [])
    offs = {s: pick("official", L, s) for s in sizes()}
    out = []
    for d in sorted({e["day"] for e in L}, reverse=True)[:n]:
        if not any(d in offs[s] for s in sizes()):
            nb = sum(1 for x in L if x["day"] == d)
            out.append("%s: waiting for the first match (%d slip change%s logged)" % (_day(d), nb, "" if nb == 1 else "s"))
            continue
        out.append("%s: %s" % (_day(d), " · ".join(_cell(offs[s].get(d), s) for s in sizes())))
    return out


def ondemand_text():
    if not LIVE:
        return _ondemand_top3()
    return "\n".join(summary_lines() + ["**Last 7 days** (official = the last board before the first match, $10 a slip)"]
                     + (day_lines(7) or ["nothing yet"]))


def short_text():
    """The tally field on the slip cards (fits Discord's 1024-character field)."""
    t = ondemand_text()
    return t if len(t) <= 1024 else t[:1020] + "..."


# ------------------------------------------------------------------ paper tests
def log_tests(b):
    """First sighting per day of every numbered + BACKUPS prop, with the shared test verdicts."""
    try:
        import apex_card_signals as cs, apex_top3_no175_papertest as t3
        try:
            import apex_pinnacle as pin
        except Exception:
            pin = None
        data = _load(TEST_LOG, {})
        today = datetime.now(CT).date().isoformat()
        day = data.setdefault(today, {})
        rows = [(i, d) for i, d in enumerate(b, 1)] + [(0, r[3]) for r in (getattr(b, "backups", None) or [])]
        added = 0
        for rank, d in rows:
            pp, p = d["pp"], d["p"]
            k = "%s|%s" % (pp["key"], pp["direction"])
            if k in day:
                continue
            v175 = None
            if pp["category"] == "total_games" and pp["direction"] == "MORE" and float(pp["line"]) >= 17.5:
                try:
                    v175 = t3.rule175(cs._names(p), str(p.get("match_date") or "")[:10], float(pp["line"]),
                                      book=p.get("bet365_total"))[0]
                except Exception:
                    v175 = None
            gap = None
            if pin and pp["category"] == "total_games":
                try:
                    r = pin.room(p)
                    gap = r[2] if r else None
                except Exception:
                    gap = None
            day[k] = {"key": pp["key"], "direction": pp["direction"], "category": pp["category"],
                      "line": pp["line"], "prob": round(float(pp["prob"]), 4), "rank": rank,
                      "net": cs.net_score(p)[2], "net_avail": cs.net_score(p)[3], "v175": v175, "pin_gap": gap,
                      "start_utc": pp["start_utc"], "result": None}
            added += 1
        if added:
            _save(TEST_LOG, data)
        return added
    except Exception as ex:
        print("[PPTracking] paper test log failed: %s: %s" % (type(ex).__name__, ex))
        return 0


def grade_tests():
    data = _load(TEST_LOG, {})
    if not data:
        return 0
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=10)
    except Exception:
        return 0
    n = 0
    for day in data.values():
        for r in day.values():
            if r.get("result") is None:
                x = c.execute("SELECT result FROM lines WHERE key=?", (r["key"],)).fetchone()
                if x and x[0]:
                    r["result"] = "void" if x[0] in ("void", "push") else ("hit" if x[0] == r["direction"].lower() else "miss")
                    n += 1
    c.close()
    if n:
        _save(TEST_LOG, data)
    return n


def _wl(rs):
    w = sum(r["result"] == "hit" for r in rs)
    l = sum(r["result"] == "miss" for r in rs)
    return ("%d of %d hit" % (w, w + l)) if w + l else "none graded yet"


def test_lines():
    data = _load(TEST_LOG, {})
    rs = [r for d in data.values() for r in d.values()]
    if not rs:
        return ["ParlayPlay paper tests: nothing logged yet."]
    g = lambda f: [r for r in rs if f(r)]
    out = ["17.5 check (TG MORE 17.5+): play ✅ %s · skip ❌ %s · no data %s" % (
               _wl(g(lambda r: r["v175"] in ("close", "room"))), _wl(g(lambda r: r["v175"] == "skip")),
               _wl(g(lambda r: r["v175"] == "nodata"))),
           "Pinnacle cushion (TG): 3+ games of room %s · under 3 %s" % (
               _wl(g(lambda r: r["pin_gap"] is not None and r["pin_gap"] >= 3)),
               _wl(g(lambda r: r["pin_gap"] is not None and r["pin_gap"] < 3))),
           "Signal count (TG MORE): +2 or better %s · +1 or less %s" % (
               _wl(g(lambda r: r["net_avail"] >= 2 and r["net"] >= 2)),
               _wl(g(lambda r: r["net_avail"] >= 2 and r["net"] < 2))),
           "Board: numbered picks %s · BACKUPS %s" % (_wl(g(lambda r: r["rank"])), _wl(g(lambda r: not r["rank"])))]
    return out


def status_lines():
    """The PARLAYPLAY block for the 9 AM #system status. Never raises."""
    try:
        grade()
        grade_tests()
    except Exception:
        pass
    try:   # APEX-PPINSURED-2026-10-23: the Insured record replaces the retired All In Top 3/6/9 record
        import apex_pp_insured as _ins
        if _ins.LIVE:
            return _ins.status_lines() + test_lines()
    except Exception:
        pass
    try:
        return summary_lines() + day_lines(3) + test_lines()
    except Exception as e:
        return ["ParlayPlay status could not run (%s)" % type(e).__name__]


if __name__ == "__main__":
    grade()
    grade_tests()
    print(ondemand_text())
    print("\n".join(test_lines()))
