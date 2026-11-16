#!/usr/bin/env python3
"""
APEX-PPINSURED-2026-10-23 (owner, "Go"): the ParlayPlay Insured slips and their tracker.
ParlayPlay data only -- everything here lives in ~/openclaw/ApexParlayPlay and reads the
ParlayPlay database. Nothing is written to a PrizePicks file.

THE RULE (from _backups/pp_solution_test_2026-10-23 and pp_signal_test_2026-10-23)
  Legs: ParlayPlay Total Games lines on our side paying 1.40x-5x (ParlayPlay only offers Insured
  when every pick is 1.4x-5x), our Win Prob 60-65% (no Apex 67 bar, no category gate; 65-70% at
  1.40x+ hit only 51 of 100), the 17.5 check and the low-cushion hold still apply (a held line goes
  to BACKUPS with its reason), one leg per match and player, ranked by the board rank score.
  Payout: ParlayPlay's fixed Insured table (help page "How does insurance work?", checked 10/23):
  3 legs 2.5x / 1.25x one miss, 5 legs 12x / 2.5x, 6 legs 25x / 2.5x, 9 legs 80x / 6x.
  Two or more misses lose the slip.

ARMS (all logged, graded at $[amount] official = the last board before the day's first match,
first board kept)
  ins3     PP INSURED 3 -- the live slip, the only card in #pp-slips
  ins6     PP INSURED 6 TEST -- card in #pp-systems, data only
  ins9     PP INSURED 9 TEST -- card in #pp-systems, data only
  ins5     Insured 5 -- background, no card
  ins3sc   Insured 3 "skip close matches" (favorite under 65%) -- background, no card
  A TEST FINISHED card goes to #pp-alerts per arm after 20 graded days or on 11/20.

  python3 apex_pp_insured.py   # record + last 7 days + test lines
"""
import json, os, sqlite3, sys
from datetime import datetime, timezone, timedelta

APP = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(APP)
for _p in (APP, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
DB_PATH = os.path.join(APP, "data", "parlayplay_db.sqlite")
LOG = os.path.join(APP, "data", "insured_slips.json")
STATE = os.path.join(APP, "data", "insured_state.json")
CT = timezone(timedelta(hours=-5))
START_LABEL = "since 10/23"
STAKE = 10.0

# The switch: True = Insured 3 is the #pp-slips card and the 6/9 tests post to #pp-systems.
# False = everything goes back to the Top 3/6/9 / 2/3/4 cards as before 10/23 (save point before-pp-insured).
LIVE = True

MIN_MULT, MAX_MULT = 1.40, 5.0
MIN_PROB, MAX_PROB = 0.60, 0.65
CLOSE_FAV = 0.65          # favorite under 65% to win = close match
CLOSE_HIT = 58            # close-match legs at 1.40x+ / 60-65% hit 58 of 100 (signal test 10/23)
LEG_RATE = 0.64           # measured: these legs hit 64 of 100 (129 lines, 10/16-22)
TABLE = {2: (2, 0.5), 3: (2.5, 1.25), 4: (5, 1.25), 5: (12, 2.5), 6: (25, 2.5), 7: (40, 4), 8: (50, 4), 9: (80, 6)}
ARMS = (("ins3", 3, "Insured 3"), ("ins6", 6, "Insured 6"), ("ins9", 9, "Insured 9"),
        ("ins5", 5, "Insured 5"), ("ins3sc", 3, "Insured 3 skip-close"))
ARM_NAME = {a: n for a, _, n in ARMS}
ARM_SIZE = {a: s for a, s, _ in ARMS}
FINISH_DAYS = 20
FINISH_BY = "2026-11-20"


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


def chances(n, r=LEG_RATE):
    """(all hit, at least n-1 hit) at leg rate r, legs independent."""
    a = r ** n
    return a, a + n * r ** (n - 1) * (1 - r)


# ------------------------------------------------------------------ legs
def fav(d):
    try:
        return float(d["pp"].get("pinn_fav")) if d["pp"].get("pinn_fav") not in (None, "") else None
    except (TypeError, ValueError):
        return None


def is_close(d):
    f = fav(d)
    return f is not None and f < CLOSE_FAV


def pool(rows):
    """(ok, held): every ParlayPlay Total Games line on our side at 1.40x-5x and 60-65% Win Prob.
    held = the ones a board hold (17.5 check / low cushion) sends to BACKUPS, with d["why"]."""
    import apex_edge_tracker as t
    import apex_parlayplay as pp
    rec = t.records()
    ok, held = [], []
    for p in pp.picks(rows):
        if p["category"] != "total_games":
            continue
        m = float(p.get("mult") or 0)
        if not (MIN_MULT - 1e-9 <= m <= MAX_MULT + 1e-9):
            continue
        q = pp._pp_pick(p)
        try:   # APEX-BANLIST-2026-10-28: banned player's match never makes a ParlayPlay slip
            import apex_ban_list as _abl
            if _abl.is_banned(q):
                continue
        except Exception:
            pass
        if not t._category_has_evidence(q["category"], rec):
            continue
        sc = t.apex_score(q["prob"], q["category"], q["bo5"], rec)[0]
        d = {"p": q, "pp": p, "sc": sc, "score": sc}
        pr = t._board_prob(d)
        if not (MIN_PROB - 1e-9 <= pr < MAX_PROB):
            continue
        d["win"] = pr
        d["rk"] = t.board_rank_score(q, pr)
        hw = pp._hold_why(d)
        if hw:
            d["why"] = hw
            held.append(d)
        else:
            ok.append(d)
    return ok, held


def order(cands):
    """Best-ranked line per match, then one per match/player, by rank score."""
    import apex_edge_tracker as t
    by = {}
    for d in cands:
        m = d["pp"]["match_id"]
        if m not in by or d["rk"] > by[m]["rk"]:
            by[m] = d
    out, used = [], set()
    for d in sorted(by.values(), key=lambda d: -d["rk"]):
        nm = t._prop_names(d["p"])
        if nm & used:
            continue
        used |= nm
        out.append(d)
    return out


def build(rows):
    """{arm: legs} for this read, plus "_held" (best held line per match not already used)."""
    ok, held = pool(rows)
    o = order(ok)
    out = {"ins3": o[:3], "ins5": o[:5], "ins6": o[:6], "ins9": o[:9],
           "ins3sc": order([d for d in ok if not is_close(d)])[:3]}
    used = {d["pp"]["match_id"] for d in o[:9]}
    out["_held"] = [d for d in order(held) if d["pp"]["match_id"] not in used]
    out["_n"] = len(o)
    starts = [r["start_utc"] for r in rows if r.get("start_utc")]
    out["_day"] = _ct_day(min(starts)) if starts else None   # day a 0-leg "not built" entry belongs to
    return out


# ------------------------------------------------------------------ log
def _label(pp):
    who = "%s v %s" % (pp["home"], pp["away"]) if pp["player"] == "match" else pp["player"]
    return "%s %s %s %g" % (who, pp["category"].replace("_", " ").title(), pp["direction"], pp["line"])


def log_build(arms, board_ts=None):
    """Logs each arm's slip when its legs differ from the last one logged for its day. Never raises."""
    out = []
    try:
        board_ts = board_ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
        L = _load(LOG, [])
        for arm, size, _n in ARMS:
            legs = list(arms.get(arm) or [])
            day = (min(_ct_day(d["pp"]["start_utc"]) for d in legs) if legs
                   else arms.get("_day") or datetime.now(CT).date().isoformat())
            sig = sorted("%s|%s" % (d["pp"]["key"], d["pp"]["direction"]) for d in legs)
            same = [e for e in L if e["day"] == day and e["arm"] == arm]
            if same and sorted(same[-1]["sig"]) == sig:
                continue
            e = {"id": "%s|%s|%s" % (day, arm, board_ts), "day": day, "arm": arm, "size": size, "board_ts": board_ts,
                 "n": len(legs), "short": len(legs) < size, "sig": sig, "status": "pending", "profit": None,
                 "legs": [{"key": d["pp"]["key"], "direction": d["pp"]["direction"], "mult": float(d["pp"]["mult"]),
                           "prob": round(float(d.get("win") or d["pp"]["prob"]), 4), "fav": fav(d),
                           "start_utc": d["pp"]["start_utc"], "label": _label(d["pp"]), "result": None} for d in legs]}
            L.append(e)
            out.append(e)
        if out:
            _save(LOG, L)
    except Exception as ex:
        print("[PPInsured] log failed: %s: %s" % (type(ex).__name__, ex))
    return out


def _first_starts():
    out = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=10)
        for (st,) in c.execute("SELECT DISTINCT start_utc FROM lines WHERE start_utc >= ?",
                               ((datetime.now(timezone.utc) - timedelta(days=40)).isoformat(),)):
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
    """{entry id: {"official", "first"}} per day and arm; official only once the day's first match started."""
    L = _load(LOG, []) if L is None else L
    fs = _first_starts()
    now = datetime.now(timezone.utc)
    by, out = {}, {}
    for e in L:
        by.setdefault((e["day"], e["arm"]), []).append(e)
    for (d, _a), es in by.items():
        es.sort(key=lambda e: e["board_ts"])
        f = fs.get(d) or min([l["start_utc"] for e in es for l in e["legs"]] or [None])
        if not f:
            continue
        fdt = datetime.fromisoformat(f)
        before = [e for e in es if datetime.fromisoformat(e["board_ts"]) < fdt]
        if not before:
            continue
        out.setdefault(before[0]["id"], set()).add("first")
        if fdt <= now:
            out.setdefault(before[-1]["id"], set()).add("official")
    return out


def pick(role, arm, L=None):
    L = _load(LOG, []) if L is None else L
    r = roles(L)
    return {e["day"]: e for e in L if e["arm"] == arm and role in r.get(e["id"], ())}


def grade_entry(e):
    """Insured grading on the fixed table: 2+ misses = lost; a void leg drops out and the slip is paid
    on the table for the legs left (under 2 left = money back, 2 left with a miss = lost)."""
    res = [l.get("result") for l in e["legs"]]
    miss = res.count("miss")
    e["misses"] = miss
    e["final"] = None not in res
    if e.get("short"):
        e.update(status="short", profit=0.0)
        return
    if miss >= 2:
        e.update(status="lost", mult=0.0, profit=-STAKE)
        return
    if None in res:
        e.update(status="pending", profit=None)
        return
    nl = len(res) - res.count("void")
    if nl < 2 or (nl == 2 and miss):
        e.update(status="lost" if miss else "void", mult=0.0 if miss else 1.0, profit=-STAKE if miss else 0.0)
        return
    a, o = TABLE[nl]
    x = o if miss else a
    e.update(status="onemiss" if miss else "allhit", mult=x, profit=round(STAKE * x - STAKE, 2))


def grade():
    L = _load(LOG, [])
    if not L:
        return 0
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=10)
    except Exception:
        return 0
    changed = 0
    for e in L:
        if e.get("final") and e["status"] != "pending":
            continue
        before = (e["status"], e.get("misses"), e.get("final"))
        moved = False
        for l in e["legs"]:
            if l.get("result") is None:
                r = c.execute("SELECT result FROM lines WHERE key=?", (l["key"],)).fetchone()
                if r and r[0]:
                    l["result"] = "void" if r[0] in ("void", "push") else ("hit" if r[0] == l["direction"].lower() else "miss")
                    moved = True
        grade_entry(e)
        if moved or before != (e["status"], e.get("misses"), e.get("final")):
            e["graded_at"] = datetime.now(CT).isoformat(timespec="seconds")
            changed += 1
    c.close()
    if changed:
        _save(LOG, L)
    return changed


# ------------------------------------------------------------------ record
def tally(days):
    """(graded days, all-hit, one-miss, $, not-built days, leg hits, leg misses) over official entries."""
    g = [e for e in days.values() if e["status"] in ("allhit", "onemiss", "lost", "void")]
    lh = sum(l.get("result") == "hit" for e in g for l in e["legs"])
    lm = sum(l.get("result") == "miss" for e in g for l in e["legs"])
    return (len(g), sum(e["status"] == "allhit" for e in g), sum(e["status"] == "onemiss" for e in g),
            sum(e.get("profit") or 0 for e in g), sum(e.get("short") for e in days.values()), lh, lm)


def headline(L=None):
    L = _load(LOG, []) if L is None else L
    n, ah, om, pl, nb, _h, _m = tally(pick("official", "ins3", L))
    if not n:
        waiting = len({e["day"] for e in L if e["arm"] == "ins3"})
        return "\U0001F4CA PP INSURED 3 (%s): no finished days yet%s." % (
            START_LABEL, (" (%d day%s logged)" % (waiting, "" if waiting == 1 else "s")) if waiting else "")
    return "\U0001F4CA PP INSURED 3 (%s): paid %d of %d day%s, all-hit %d, one-miss %d (%s)%s" % (
        START_LABEL, ah + om, n, "" if n == 1 else "s", ah, om, _money(pl),
        (" · not built %d day%s" % (nb, "" if nb == 1 else "s")) if nb else "")


def _arm_bit(arm, L):
    n, ah, om, pl, nb, _h, _m = tally(pick("official", arm, L))
    if not n:
        return "%s: no finished days yet%s" % (ARM_NAME[arm], (" (not built %d)" % nb) if nb else "")
    return "%s: all-hit %d of %d, one-miss %d (%s)%s" % (ARM_NAME[arm], ah, n, om, _money(pl),
                                                        (", not built %d" % nb) if nb else "")


def tests_line(L=None):
    L = _load(LOG, []) if L is None else L
    return "\U0001F9EA " + " · ".join(_arm_bit(a, L) for a in ("ins6", "ins9", "ins5", "ins3sc"))


def summary_lines():
    L = _load(LOG, [])
    out = [headline(L), tests_line(L)]
    fb = []
    for arm in ("ins3", "ins6", "ins9"):
        off = pick("official", arm, L)
        n, ah, om, pl, _nb, _h, _m = tally({d: v for d, v in pick("first", arm, L).items() if d in off})
        if n:
            fb.append("%s paid %d of %d (%s)" % (ARM_NAME[arm], ah + om, n, _money(pl)))
    if fb:
        out.append("Playing off the first board instead: " + " · ".join(fb) + ".")
    return out


_ICON = {"allhit": "✅ all hit", "onemiss": "\U0001F7E1 one miss, paid", "lost": "❌ 2+ missed", "void": "➖ void"}


def _cell(e, size):
    if not e:
        return "--"
    if e.get("short"):
        return "not built (only %d qualified)" % e["n"]
    if e["status"] == "pending":
        return "playing" + (" (1 miss so far)" if e.get("misses") == 1 else "")
    return "%s %s" % (_ICON.get(e["status"], e["status"]), _money(e.get("profit") or 0))


def day_lines(arm="ins3", n=7, L=None):
    L = _load(LOG, []) if L is None else L
    off = pick("official", arm, L)
    fs = _first_starts()
    now = datetime.now(timezone.utc)
    out = []
    for d in sorted({e["day"] for e in L if e["arm"] == arm}, reverse=True)[:n]:
        e = off.get(d)
        if not e:
            started = d in fs and datetime.fromisoformat(fs[d]) <= now
            out.append("%s: %s" % (_day(d), "not counted (first slip came after the day's first match)" if started
                                   else "waiting for the first match"))
        else:
            out.append("%s: %s" % (_day(d), _cell(e, ARM_SIZE[arm])))
    return out


def ondemand_text():
    """The Insured 3 record + last 7 days (the /pp-slip field)."""
    L = _load(LOG, [])
    t = "\n".join([headline(L), "**Last 7 days** (official = last board before the first match, $10, Insured payout)"]
                  + (day_lines("ins3", 7, L) or ["nothing yet"]))
    return t if len(t) <= 1024 else t[:1020] + "..."


def tests_text():
    """/pp-tests: every test arm's record + last 7 days."""
    grade()
    L = _load(LOG, [])
    out = ["\U0001F9EA **ParlayPlay Insured tests** (data only, $10 a slip, Insured table, official = last board before "
           "the first match; finish after %d graded days or %s)" % (FINISH_DAYS, "11/20"), headline(L), tests_line(L)]
    for arm in ("ins6", "ins9", "ins5", "ins3sc"):
        out += ["", "**%s** (last 7 days)" % ARM_NAME[arm]] + (day_lines(arm, 7, L) or ["nothing yet"])
    return "\n".join(out)


def status_lines():
    """For the 9 AM #system PARLAYPLAY section. Never raises."""
    try:
        grade()
    except Exception:
        pass
    try:
        return summary_lines() + day_lines("ins3", 3)
    except Exception as e:
        return ["ParlayPlay Insured status could not run (%s)" % type(e).__name__]


def finished_cards():
    """TEST FINISHED texts for arms that reached 20 graded days (or 11/20) and haven't had one. Marks them."""
    st = _load(STATE, {})
    done = st.setdefault("finished", {})
    L = _load(LOG, [])
    today = datetime.now(CT).date().isoformat()
    out = []
    for arm, size, name in ARMS:
        if arm in done:
            continue
        n, ah, om, pl, nb, lh, lm = tally(pick("official", arm, L))
        if n < FINISH_DAYS and today < FINISH_BY:
            continue
        keep = n > 0 and pl > 0
        out.append("\U0001F3C1 **TEST FINISHED: PP %s** (%s to %s)\n"
                   "%d graded days: all %d hit on %d, one miss (paid) on %d, lost on %d%s.\n"
                   "Money at $10 a day: %s. Legs hit %d of %d.\n"
                   "**%s** -- %s" % (
                       name.upper(), "10/23", _day(today), n, size, ah, om, n - ah - om,
                       (", not built on %d" % nb) if nb else "", _money(pl), lh, lh + lm,
                       "KEEP" if keep else "DROP",
                       "it made money; say go if you want it played/kept." if keep else
                       "it did not make money, so it stops being suggested unless you say otherwise."))
        done[arm] = today
    if out:
        _save(STATE, st)
    return out


def daily_post_due():
    st = _load(STATE, {})
    now = datetime.now(CT)
    return now.hour >= 9 and st.get("daily_date") != now.date().isoformat()


def mark_daily():
    st = _load(STATE, {})
    st["daily_date"] = datetime.now(CT).date().isoformat()
    _save(STATE, st)


if __name__ == "__main__":
    grade()
    print(ondemand_text())
    print()
    print(tests_text())
