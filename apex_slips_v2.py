#!/usr/bin/env python3
"""
APEX-SLIPSV2-2026-10-22 (owner, go on 10/22: "The discord slips to reflect these
changes and only show slips for the top 3 and elite 6. I want to keep track of the win
rate, new tracking separate from the old so it's not mixed up, keep the old tracking
going, I want to see how these are doing after all the changes."):

SLIPS V2 -- rules from 10/22 4:08 PM CT (17.5 skip / Fantasy LESS / DF holds to BACKUPS,
DF LESS Apex 67 on Best Standard, hold slot moves up, pulled-line fill, new-line keep).

  🥇 TOP 3 SLIP (Power, 3 legs)   -- the Top 3 board exactly as numbered
  🏆 ELITE 6 SLIP (Flex, 6 legs)  -- the Elite 6 board exactly as numbered

"Exactly as numbered" = what the board posts right now (published_board_detail: after
holds, pulled-line fills and new-line keeps). No top-ups from other boards; a short board
posts a short slip with the gap stated.

Its own files, never mixed into slip_log.json, props_db rec_slips or any older tracker:
  data/slips/slips_v2_log.json   every Top 3 / Elite 6 slip, one per board build
  data/slips/slips_v2_log.csv    same, one row per slip (rebuilt on every save)
  data/slips/slips_v2_state.json which daily result cards were posted

Days are match dates. The OFFICIAL slip of a day = the last board built before the first
of that slip's own picks starts (what he plays; APEX-ANYDATE-2026-10-25, was the day's first match). The first board of the day and the 5 PM board
are kept as alternates. Each slip is graded from the same settled results the #slips
grader uses; $[amount] a slip.

  python3 apex_slips_v2.py            # summary + last 7 days
  python3 apex_slips_v2.py --log      # log the current posted board now
  python3 apex_slips_v2.py --grade    # grade open slips
"""
import csv, json, os, sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "data", "slips", "slips_v2_log.json")
CSV_OUT = os.path.join(HERE, "data", "slips", "slips_v2_log.csv")
STATE = os.path.join(HERE, "data", "slips", "slips_v2_state.json")
STATUS_LOG = os.path.join(HERE, "apex_board_status_log_v1.csv")
FIVEPM_STATE = os.path.join(HERE, "data", "logs", "fivepm_fetch_state.json")
CT = ZoneInfo("America/Chicago")
START_LABEL = "since 10/22"
STAKE = 20.0
# Top 3 on Power. Goblin 3 = 2.2x and Standard 3 = 6x are the payouts in the code
# (apex_slip_planner.PAYOUTS); mixed Goblin/Standard from his 10/11 screenshots.
POWER3 = {"GGG": 2.2, "GGS": 3.25, "GSS": 3.75, "SSS": 6.0}
POWER3_FALLBACK = 2.2   # Demon in the mix or unknown: count the lowest (Goblin) payout
# Elite 6 on Flex. The code has no 6-pick Flex table, so these come from his real
# all-Goblin 6-pick Flex slips (slip_log.json, Oct 13-22): 6 of 6 paid 3x-5x (3x used),
# 5 of 6 about 1x, 4 of 6 paid 0.4x. Goblin payouts vary -- the app has the real number.
FLEX6 = {6: 3.0, 5: 1.0, 4: 0.4}
KIND = {"top3": ("\U0001F947", "TOP 3 SLIP", "Power", 3, "Top 3"),
        "elite6": ("\U0001F3C6", "ELITE 6 SLIP", "Flex", 6, "Elite 6")}
# APEX-ELITE5-2026-10-31 (owner, "Go elite 5"): a 5-pick Flex from Elite 6 spots #1-5 as numbered. The Elite 6 board and
# slip are untouched. Tracked from each day's official Elite 6 slip, counted only from ELITE5_FROM, kept out of every other record.
# All 5 pays 3x (his real 5-pick Flex slips paid 2.25x-3x). A void shrinks the slip: 2.25x with one void, money back with more.
# 4 of 5 at half back is an estimate; the app has the real number. Undo: ELITE5_SLIP_ON = False.
ELITE5_SLIP_ON = True
ELITE5_FROM = "2026-10-31"
ELITE5_N = 5
ELITE5_ALL_HIT = {0: 3.0, 1: 2.25}
ELITE5_ALL_HIT_MORE_VOIDS = 1.0
ELITE5_ONE_MISS = 0.5
BUILD = {"auto": "5 PM auto", "manual": "manual /fetch", "restart": "restart rebuild",
         "reprice": "hourly", "autoeval": "odds watch", "category_gate": "hourly"}
# APEX-UPDATETRACK-2026-10-24: "autoeval" is the 30-min odds watch (apex_auto_eval), not hourly.
LOCK = LOG + ".lock"


def _sc():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import apex_slips_channel as sc
    return sc


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def _ct(ts):
    """Board captured_at is naive Central time; leg start times are UTC ISO."""
    d = datetime.fromisoformat(str(ts))
    return d.replace(tzinfo=CT) if d.tzinfo is None else d


def _money(x):
    return ("+$%.2f" % x) if x >= 0 else ("-$%.2f" % -x)


def _day(d):
    try:
        return datetime.fromisoformat(d).strftime("%b %-d")
    except Exception:
        return str(d)


# ------------------------------------------------------------------ build + log
def _same_cap(a, b):
    """APEX-UPDATETRACK-2026-10-24: the snapshot and the status log stamp one build up to a few
    seconds apart (17:53:59 vs 17:54:00 on 10/24), which left builds labelled "unknown"."""
    if a == b:
        return True
    try:
        return abs((datetime.fromisoformat(str(a)) - datetime.fromisoformat(str(b))).total_seconds()) <= 5
    except Exception:
        return False


def _status_rows(cap):
    """(source, earliest start of any prop on the slate) from the board status log for this build."""
    src, first = None, None
    try:
        with open(STATUS_LOG, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if str(r.get("captured_at") or "")[:10] != str(cap or "")[:10] or not _same_cap(r.get("captured_at"), cap):
                    continue
                src = src or r.get("source")
                s = r.get("start_utc") or ""
                if s and (first is None or s < first):
                    first = s
    except Exception:
        pass
    return src, first


def _build_label(src, cap):
    if src == "manual":
        st = _load(FIVEPM_STATE, {})
        try:
            if st.get("date") == cap[:10] and st.get("started") and _ct(st["started"]) <= _ct(cap) \
                    and (not st.get("finished") or _ct(cap) <= _ct(st["finished"]) + timedelta(minutes=2)):
                return "5 PM auto"
        except Exception:
            pass
    return BUILD.get(src or "", src or "unknown")


def current_slips(snap=None):
    """(snap, {"top3": [legs], "elite6": [legs]}) from the boards as posted right now."""
    sc = _sc()
    T = sc._T()
    import contextlib, io
    snap = snap or sc._load(sc.SNAP, {})
    starts = sc._starts(snap)
    out = {}
    with contextlib.redirect_stdout(io.StringIO()):
        rec = T.records()
        for k, (_, _, _, n, _) in KIND.items():
            res = T.published_board_detail(k, rec=rec, return_scored=True)
            ents = (res.get("scored") or []) if isinstance(res, dict) else []
            out[k] = sc.mark_score_decimals([sc._leg(e, starts, k) for e in ents[:n]])   # APEX-SCOREDISPLAY-2026-10-25
    return snap, out


def _power3(legs):
    code = "".join(sorted("G" if l["prop_type"] == "goblin" else ("S" if l["prop_type"] == "standard" else "D")
                          for l in legs))
    return POWER3.get(code) if len(legs) == 3 else None


class _Lock:
    """APEX-UPDATETRACK-2026-10-24: the scheduler (after each republish) and the bot's #slips loop
    both write the log; one at a time."""
    def __enter__(self):
        import fcntl
        os.makedirs(os.path.dirname(LOCK), exist_ok=True)
        self.fh = open(LOCK, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        try:
            self.fh.close()
        except Exception:
            pass


def log_build(snap=None, slips=None, source=None):
    """Logs this build's Top 3 and Elite 6 (once per board build). Returns the new entries.
    `source` = the republish trigger when the caller knows it (APEX-UPDATETRACK-2026-10-24)."""
    if slips is None:
        snap, slips = current_slips(snap)
    cap = (snap or {}).get("captured_at")
    if not cap:
        return []
    with _Lock():
        return _log_build(snap, slips, cap, source)


def _log_build(snap, slips, cap, source):
    L = _load(LOG, [])
    have = {(e["board_ts"], e["kind"]) for e in L}
    src, first = _status_rows(cap)
    src = source or src
    new = []
    for k, legs in slips.items():
        if (cap, k) in have:
            continue
        e = {"id": "%s|%s" % (cap, k), "kind": k, "board_ts": cap, "match_date": snap.get("match_date"),
             "build": _build_label(src, cap), "source": src or "", "slate_first_start": first,
             "logged_at": datetime.now(CT).isoformat(timespec="seconds"), "n": len(legs),
             "payout": _power3(legs) if k == "top3" else None, "status": "pending",
             "hits": 0, "misses": 0, "voids": 0, "mult": None, "profit": None,
             "legs": [{x: l.get(x) for x in ("match_id", "player", "opponent", "category", "line", "direction",
                                             "prop_type", "win", "honest", "start_utc", "match_date")}
                      for l in legs]}
        L.append(e)
        new.append(e)
    if new:
        _save(LOG, L)
        _write_csv(L)
    return new


def _who(l):
    return str(l["opponent"]) if l["player"] == "match" else l["player"]


def _write_csv(L):
    fields = ["match_date", "board_ts", "build", "kind", "official", "first_board", "fivepm_board", "n", "status",
              "hits", "misses", "voids", "mult", "profit", "legs"]
    off = _roles(L)
    os.makedirs(os.path.dirname(CSV_OUT), exist_ok=True)
    with open(CSV_OUT + ".tmp", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for e in L:
            r = off.get(e["id"], set())
            w.writerow({"match_date": e["match_date"], "board_ts": e["board_ts"], "build": e["build"], "kind": e["kind"],
                        "official": int("official" in r), "first_board": int("first" in r),
                        "fivepm_board": int("fivepm" in r), "n": e["n"], "status": e["status"], "hits": e["hits"],
                        "misses": e["misses"], "voids": e["voids"], "mult": e["mult"], "profit": e["profit"],
                        "legs": " | ".join("%s %s %s %s%s" % (_who(l), l["category"], l["direction"], l["line"],
                                                              (" [%s]" % l["result"]) if l.get("result") else "")
                                           for l in e["legs"])})
    os.replace(CSV_OUT + ".tmp", CSV_OUT)


# ------------------------------------------------------------------ official / first / 5 PM
def _first_start(entries):
    s = [e.get("slate_first_start") for e in entries if e.get("slate_first_start")]
    s += [l["start_utc"] for e in entries for l in e["legs"] if l.get("start_utc")]
    return min(s) if s else None


def _roles(L):
    """{entry id: {"official", "first", "fivepm"}} -- per match date and kind."""
    out = {}
    by = {}
    for e in L:
        by.setdefault(e["match_date"], []).append(e)
    now = datetime.now(timezone.utc)
    for d, es in by.items():
        fs = _first_start(es)
        try:
            fsd = datetime.fromisoformat(fs) if fs else None
        except Exception:
            fsd = None
        for k in KIND:
            ks = sorted([e for e in es if e["kind"] == k], key=lambda e: e["board_ts"])
            if OWN_FIRST_PICK and str(d) >= OWN_FIRST_PICK_FROM:
                off, lock = _own_official(ks, fsd, now)
                before = [e for e in ks if lock is None or _ct(e["board_ts"]) < lock]
                if not before:
                    continue
                out.setdefault(before[0]["id"], set()).add("first")
                five = [e for e in before if e["build"] == "5 PM auto"]
                if five:
                    out.setdefault(five[-1]["id"], set()).add("fivepm")
                if off is not None:
                    out.setdefault(off["id"], set()).add("official")
                continue
            before = [e for e in ks if fsd is None or _ct(e["board_ts"]) < fsd]
            if not before:
                continue
            out.setdefault(before[0]["id"], set()).add("first")
            five = [e for e in before if e["build"] == "5 PM auto"]
            if five:
                out.setdefault(five[-1]["id"], set()).add("fivepm")
            if fsd is not None and fsd <= now:   # official only once the first match has started
                out.setdefault(before[-1]["id"], set()).add("official")
    return out


# APEX-ANYDATE-2026-10-25 (owner, "Go fix date ... the best picks because they are the best
# regardless of the starting date"): boards now rank every upcoming pick together, so a slip's
# picks may all start after the slate's first match. The OFFICIAL slip = the last board built
# before the first of THAT slip's own picks starts (was: before the day's first match). Walking
# the builds in order, a build is locked when its own first pick starts before the next build;
# the first locked build of the day is the official one. False = the old rule.
OWN_FIRST_PICK = True
OWN_FIRST_PICK_FROM = "2026-10-26"   # days already played (10/25 and before) keep the old rule


def _own_start(e):
    s = [l["start_utc"] for l in e["legs"] if l.get("start_utc")]
    try:
        return datetime.fromisoformat(min(s)) if s else None
    except Exception:
        return None


def _own_official(ks, fsd, now):
    """(official entry or None, its lock time or None) for one day's builds of one kind."""
    for i, e in enumerate(ks):
        own = _own_start(e) or fsd
        if own is None or _ct(e["board_ts"]) >= own:
            continue
        nxt = ks[i + 1] if i + 1 < len(ks) else None
        if own <= now and (nxt is None or _ct(nxt["board_ts"]) >= own):
            return e, own
    pend = [_own_start(e) or fsd for e in ks]
    pend = [x for x in pend if x is not None and x > now]
    return None, (min(pend) if pend else fsd)


def pick(L, role):
    """{match_date: {kind: entry}} for one role."""
    r = _roles(L)
    out = {}
    for e in L:
        if role in r.get(e["id"], ()):
            out.setdefault(e["match_date"], {})[e["kind"]] = e
    return out


# ------------------------------------------------------------------ grading
def _started(l, now):
    try:
        return datetime.fromisoformat(l["start_utc"]) <= now
    except Exception:
        return True
def grade():
    """Grades every open v2 slip from the settled results the #slips grader uses."""
    with _Lock():   # APEX-UPDATETRACK-2026-10-24
        return _grade()


def _grade():
    sc = _sc()
    L = _load(LOG, [])
    if not L:
        return 0
    fwd, hand = sc._actuals()
    now = datetime.now(timezone.utc)
    changed = 0
    for e in L:
        if e["status"] != "pending":
            continue
        hit = miss = void = wait = 0   # (every logged version is graded as a whole slip)
        for l in e["legs"]:
            if l.get("result") is None and _started(l, now):   # a result logged before the match starts is a different match
                a = sc._leg_actual(l, fwd, hand)
                if a is not None:
                    line = float(l["line"])
                    l["actual"] = a
                    l["result"] = "void" if a == line else ("hit" if (a > line) == (l["direction"] == "MORE") else "miss")
                    changed += 1
            r = l.get("result")
            hit += r == "hit"; miss += r == "miss"; void += r == "void"; wait += r is None
        e.update(hits=hit, misses=miss, voids=void)
        if e["n"] == 0:
            e.update(status="void", mult=0.0, profit=0.0); changed += 1
            continue
        if e["kind"] == "top3":
            if miss:
                st, mult = "lost", 0.0
            elif wait:
                continue
            elif void:
                st, mult = "void", 1.0   # push leg: PrizePicks shrinks the slip; counted as money back
            else:
                st, mult = "won", (e.get("payout") or POWER3_FALLBACK)
        else:
            if wait and miss < 3:
                continue
            # voids drop out (PrizePicks shrinks the slip); paid by how many legs missed
            mult = 0.0 if miss >= 3 else FLEX6.get(6 - miss, 0.0)
            st = "won" if mult > 1.0 else ("lost" if mult == 0 else "partial")
        e.update(status=st, mult=mult, profit=round(STAKE * mult - STAKE, 2),
                 graded_at=datetime.now(CT).isoformat(timespec="seconds"))
        changed += 1
    if changed:
        _save(LOG, L)
    _write_csv(L)
    return changed


# ------------------------------------------------------------------ reporting
def _tally(days):
    """days = {date: {kind: entry}} -> numbers for graded days."""
    t3 = [d["top3"] for d in days.values() if d.get("top3") and d["top3"]["status"] != "pending"]
    e6 = [d["elite6"] for d in days.values() if d.get("elite6") and d["elite6"]["status"] != "pending"]
    hit6 = lambda e, k: e["hits"] == k
    return {"t3_won": sum(e["status"] == "won" for e in t3), "t3_n": len(t3), "t3_pl": sum(e["profit"] or 0 for e in t3),
            "e6_6": sum(hit6(e, 6) for e in e6), "e6_5": sum(hit6(e, 5) for e in e6), "e6_4": sum(hit6(e, 4) for e in e6),
            "e6_n": len(e6), "e6_pl": sum(e["profit"] or 0 for e in e6)}


def elite5_result(e6):
    """(status, hits, misses, voids, profit) of the Elite 5 inside one official Elite 6 slip, or None when
    the board had under 5 picks. status is won / partial / lost / pending. Never raises."""
    try:
        legs = (e6 or {}).get("legs") or []
        if len(legs) < ELITE5_N:
            return None
        res = [l.get("result") for l in legs[:ELITE5_N]]
        hit, miss, void, wait = res.count("hit"), res.count("miss"), res.count("void"), res.count(None)
        if miss >= 2:
            return "lost", hit, miss, void, -STAKE
        if wait:
            return "pending", hit, miss, void, 0.0
        if miss == 1:
            return "partial", hit, miss, void, STAKE * ELITE5_ONE_MISS - STAKE
        return "won", hit, miss, void, STAKE * ELITE5_ALL_HIT.get(void, ELITE5_ALL_HIT_MORE_VOIDS) - STAKE
    except Exception:
        return None


def elite5_line(off=None):
    """The Elite 5 record line, or None when the switch is off. Never raises."""
    try:
        if not ELITE5_SLIP_ON:
            return None
        off = pick(_load(LOG, []), "official") if off is None else off
        rs = [elite5_result(v.get("elite6")) for d, v in off.items() if d >= ELITE5_FROM]
        rs = [r for r in rs if r and r[0] != "pending"]
        return "\U0001F3C5 Elite 5 (since 10/31): won %d of %d day%s, 4 of 5 %d day%s (%s)" % (
            sum(r[0] == "won" for r in rs), len(rs), "" if len(rs) == 1 else "s", sum(r[0] == "partial" for r in rs),
            "" if sum(r[0] == "partial" for r in rs) == 1 else "s", _money(sum(r[4] for r in rs)))
    except Exception:
        return None


def summary_lines():
    L = _load(LOG, [])
    if not L:
        return ["\U0001F4CA SLIPS V2 (%s): nothing logged yet." % START_LABEL]
    off = pick(L, "official")
    t = _tally(off)
    if not (t["t3_n"] or t["e6_n"]):
        dates = sorted({e["match_date"] for e in L})
        return ["\U0001F4CA SLIPS V2 (%s): no finished days yet. First day is %s (%d board build%s logged so far)."
                % (START_LABEL, _day(dates[0]), len({e["board_ts"] for e in L}),
                   "" if len({e["board_ts"] for e in L}) == 1 else "s")]
    dd = lambda n: "%d day%s" % (n, "" if n == 1 else "s")
    out = ["\U0001F4CA SLIPS V2 (%s): Top 3 won %d of %s (%s). Elite 6: all 6 hit %d of %s, 5 of 6 %s, "
           "4 of 6 %s (%s)." % (START_LABEL, t["t3_won"], dd(t["t3_n"]), _money(t["t3_pl"]), t["e6_6"], dd(t["e6_n"]),
                               dd(t["e6_5"]), dd(t["e6_4"]), _money(t["e6_pl"]))]
    f = _tally({d: v for d, v in pick(L, "first").items() if d in off})
    out.append("Playing off the first board instead: Top 3 %d of %d, Elite 6 %d of %d (%s)."
               % (f["t3_won"], f["t3_n"], f["e6_6"], f["e6_n"], _money(f["t3_pl"] + f["e6_pl"])))
    _e5 = elite5_line(off)
    if _e5:
        out.append(_e5)
    return out


def day_lines(n=7):
    L = _load(LOG, [])
    off, first = pick(L, "official"), pick(L, "first")
    dates = sorted({e["match_date"] for e in L}, reverse=True)[:n]
    out = []
    for d in dates:
        v = off.get(d) or {}
        if not v:
            v = first.get(d) or {}
            if not v:
                continue
            nb = len({e["board_ts"] for e in L if e["match_date"] == d})
            out.append("%s: waiting for the first match (%d board build%s logged)" % (_day(d), nb, "" if nb == 1 else "s"))
            continue
        t3, e6 = v.get("top3"), v.get("elite6")
        if (t3 and t3["status"] == "pending") or (e6 and e6["status"] == "pending"):
            out.append("%s: playing now, waiting on results" % _day(d))
            continue
        pl = sum((x or {}).get("profit") or 0 for x in (t3, e6))
        out.append("%s: Top 3 %s · Elite 6 %s · %s" % (
            _day(d), {"won": "✅", "lost": "❌", "void": "➖"}.get((t3 or {}).get("status"), "--"),
            ("%d/%d" % (e6["hits"], e6["n"])) if e6 else "--", _money(pl)))
        _r5 = elite5_result(e6) if ELITE5_SLIP_ON and d >= ELITE5_FROM else None
        if _r5:
            out[-1] += " · Elite 5 %d/%d %s" % (_r5[1], ELITE5_N, _money(_r5[4]))
    return out


def ondemand_text():
    return "\n".join(summary_lines() + ["", "**Last 7 days** (official slip = last board before the slip's own first pick starts, $20 a slip)"]
                     + (day_lines(7) or ["nothing yet"]))


def daily_cards():
    """[(key, card)] -- one SLIPS V2 card per match date once its official slips are graded."""
    L = _load(LOG, [])
    st = _load(STATE, {})
    posted = set(st.get("day_cards") or [])
    out = []
    for d, v in sorted(pick(L, "official").items()):
        if d in posted or any(e["status"] == "pending" for e in v.values()):
            continue
        sc = _sc()
        legs = []
        for k in ("top3", "elite6"):
            e = v.get(k)
            if not e:
                continue
            emo, name, mode, n, _ = KIND[k]
            res = ("✅ WON" if e["status"] == "won" else ("➖ PUSH" if e["status"] == "void" else "❌ LOST")) \
                if k == "top3" else "%d of %d hit" % (e["hits"], e["n"])
            legs.append("%s **%s** (%s) · %s · %s" % (emo, name.title(), mode, res, _money(e["profit"] or 0)))
            for i, l in enumerate(e["legs"]):
                legs += sc._leg_lines(i, l, result=l.get("result") or "wait")
            legs.append("")
            _r5 = elite5_result(e) if k == "elite6" and ELITE5_SLIP_ON and d >= ELITE5_FROM else None
            if _r5:
                legs.append("\U0001F3C5 **Elite 5 Slip** (Flex, spots #1-5) · %d of %d hit%s · %s" % (
                    _r5[1], ELITE5_N, (" · %d void" % _r5[3]) if _r5[3] else "", _money(_r5[4])))
                legs.append("")
        card = {"title": "\U0001F4CA SLIPS V2 RESULT · %s" % _day(d),
                "description": "\n".join(summary_lines()), "color": sc.GREEN, "legs": legs,
                "footer": "Slips v2 · official slip = last board before its first pick starts · $20 a slip"}
        out.append(("v2day:" + d, card))
    return out


def mark_day_posted(keys):
    days = [k.split(":", 1)[1] for k in keys if k.startswith("v2day:")]
    if not days:
        return
    st = _load(STATE, {})
    st["day_cards"] = sorted(set(st.get("day_cards") or []) | set(days))
    _save(STATE, st)


# ------------------------------------------------------------------ #slips cards
def change_line(kind, cap, legs, match_date=None):
    """APEX-UPDATETRACK-2026-10-24: one line on a new #slips card saying what changed since the
    previous logged build of this slip, e.g. '🔁 Updated 6:40 PM: Hanfmann Fantasy 3.5 (86%) in at
    #2 · Fritz v Musetti TG 19.5 out to BACKUP 1'. None for the first build of the day."""
    try:
        sc = _sc()
        T = sc._T()
        prev = [e for e in _load(LOG, []) if e["kind"] == kind and e["board_ts"] < str(cap)
                and (match_date is None or e.get("match_date") == match_date)]
        if not prev:
            return None
        prev = max(prev, key=lambda e: e["board_ts"])
        when = _ct(cap).strftime("%-I:%M %p")
        pk = [sc_key(l) for l in prev["legs"]]
        nk = [sc_key(l) for l in legs]
        if pk == nk:
            return "\U0001F501 Updated %s: no change to this slip since %s" % (when, _ct(prev["board_ts"]).strftime("%-I:%M %p"))
        lab = lambda l: T.backup_short_label(l)
        items = ["%s (%d%%) in at #%d" % (lab(l), round(100 * float(l.get("win") or 0)), i)
                 for i, l in enumerate(legs, 1) if sc_key(l) not in pk]
        bk = None
        for l in prev["legs"]:
            if sc_key(l) in nk:
                continue
            if bk is None:
                import contextlib, io
                with contextlib.redirect_stdout(io.StringIO()):
                    bk = T.backup_numbers(kind)
            b = bk.get(tuple(T._apex_prop_identity(l)))
            items.append("%s out%s" % (lab(l), (" to BACKUP %d" % b["no"]) if b else " (off the board)"))
        moved = ["%s #%d\u2192#%d" % (lab(l), pk.index(sc_key(l)) + 1, i) for i, l in enumerate(legs, 1)
                 if sc_key(l) in pk and pk.index(sc_key(l)) + 1 != i]
        if moved:
            items.append("moved: " + ", ".join(moved))
        return "\U0001F501 Updated %s: %s" % (when, " \u00B7 ".join(items))
    except Exception as e:
        print("[SlipsV2] change line skipped (%s: %s)" % (type(e).__name__, e))
        return None


def cards(snap, slips):
    sc = _sc()
    out = []
    try:
        when = _ct(snap.get("captured_at")).strftime("%-I:%M %p")
    except Exception:
        when = "?"
    src, _ = _status_rows(snap.get("captured_at"))
    build = _build_label(src, snap.get("captured_at") or "")
    for k, legs in slips.items():
        emo, name, mode, n, board = KIND[k]
        D = ["\U0001F4CC %s board as posted %s CT (%s), exactly as numbered" % (board, when, build)]
        _chg = change_line(k, snap.get("captured_at"), legs, snap.get("match_date"))   # APEX-UPDATETRACK-2026-10-24
        if _chg:
            D.insert(0, _chg)
        if len(legs) < n:
            D.append("⚠ **Only %d pick%s qualif%s today**%s" % (len(legs), "" if len(legs) == 1 else "s",
                                                                   "ies" if len(legs) == 1 else "y",
                                                                   " -- no slip" if not legs else ""))
        D.extend(sc.same_match_lines(legs))
        if legs:
            win = 1.0
            for l in legs:
                win *= l["win"]
            if k == "top3":
                pay = _power3(legs)
                D.append(("\U0001F4B0 **Pays %gx Power** · $%.0f ➜ **$%.2f**" % (pay, STAKE, STAKE * pay)) if pay
                         else "\U0001F4B0 Power -- check the payout in the app")
            else:
                D.append("\U0001F4B0 **Flex** · $%.0f: 6 of 6 ➜ $%.0f · 5 of 6 ➜ $%.0f · 4 of 6 ➜ $%.0f "
                         "_(Goblin pays vary, check the app)_" % (STAKE, STAKE * FLEX6[6], STAKE * FLEX6[5], STAKE * FLEX6[4]))
            D.append("\U0001F3AF All %d hit: **%d%%** Win Prob" % (len(legs), round(100 * win)))
        L = []
        for i, l in enumerate(legs):
            L += sc._leg_lines(i, l)
        out.append({"title": "%s %s (%s, %d legs)" % (emo, name, mode, n), "description": "\n".join(D),
                    "color": sc.BLUE, "legs": L or ["No picks on this board right now."],
                    "footer": "Slips v2 tracker · $20 a slip · recommendation, not placed"})
        if k == "elite6" and ELITE5_SLIP_ON:   # APEX-ELITE5-2026-10-31
            five = legs[:ELITE5_N]
            D5 = ["\U0001F4CC Elite 6 spots #1-5 as posted %s CT (%s)" % (when, build)]
            if len(five) < ELITE5_N:
                D5.append("⚠ **Only %d pick%s qualif%s** -- no Elite 5 slip" % (len(five), "" if len(five) == 1 else "s", "ies" if len(five) == 1 else "y"))
            else:
                win5 = 1.0
                for l in five:
                    win5 *= l["win"]
                D5.append("\U0001F4B0 **Flex** · $%.0f: 5 of 5 ➜ $%.0f · 4 of 5 ➜ about $%.0f _(Goblin pays vary, check the app)_" % (
                    STAKE, STAKE * ELITE5_ALL_HIT[0], STAKE * ELITE5_ONE_MISS))
                D5.append("\U0001F3AF All 5 hit: **%d%%** Win Prob" % round(100 * win5))
            L5 = []
            for i, l in enumerate(five if len(five) >= ELITE5_N else []):
                L5 += sc._leg_lines(i, l)
            out.append({"title": "\U0001F3C5 ELITE 5 SLIP (Flex, 5 legs)", "description": "\n".join(D5), "color": sc.BLUE,
                        "legs": L5 or ["Waiting for 5 picks on the board."],
                        "footer": "Elite 5 tracker · $20 a slip · recommendation, not placed"})
    return out


def sig(slips):
    return {k: [sc_key(l) for l in v] for k, v in slips.items()}


def sc_key(l):
    return "%s|%s|%s|%s|%s" % (l["match_id"], l["player"], l["category"], l["line"], l["direction"])


if __name__ == "__main__":
    if "--log" in sys.argv:
        print("logged %d" % len(log_build()))
    elif "--grade" in sys.argv:
        print("graded %d" % grade())
    elif "--cards" in sys.argv:
        s, sl = current_slips()
        for c in cards(s, sl):
            print(_sc()._plain(c))
    else:
        print(ondemand_text())
