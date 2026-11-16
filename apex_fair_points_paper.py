#!/usr/bin/env python3
"""
APEX-FAIRPAPER-2026-10-25 (owner: "Track" = go fair points paper test):

FAIR POINTS PAPER TEST -- a background shadow of Top 3 and Elite 6. PAPER ONLY: it never changes a
board, a rank, a card, #slips or P&L. The shadow boards are never posted.

Fair points (apex_learning_system/reports/2026-10-25_fair_points_test.md): a non-Total-Games prop gets
the rank score a Total Games MORE pick with the same expected hit rate would carry. Fit frozen on
graded data before Oct 19 (_backups/fair_points_test_2026-10-25/fit.py):
  TG map      hit = sigmoid(A_TG + B_TG * rank)            (rank in 0-1 units)
  non-TG map  hit = sigmoid(w0 + w1*logit(Win Prob) + w2*women)   per prop type, shrunk to the pooled fit
  fair score  = the TG rank with that hit rate. Total Games MORE keeps its live rank score.

On every PrizePicks publish (apex_board_history.log_publish, after the live ORDER CHECK) the Top 3 and
Elite 6 pools that publish wrote (numbered + BACKUPS; HELD stays held) are re-sorted by fair score with
the same rules as the live board: best line per match, one per match/player, 75%+ Win Prob in the top 3
spots, same board size. Both the posted (live) board and the fair board are saved here:
  data/fair_points_paper.sqlite   pubs (one row per publish) + legs (one row per spot per arm)
Results come from board history once props settle. The official slip for a day = the last board
before that slip's own first pick (same as the slips), the day = that first pick's CT date.
Payouts are the slips' ($[amount] Top 3 Power / Elite 6 Flex, apex_slips_v2).

Posts (scheduler, run(), every 15 min, posts only when due):
  9:00 AM daily, #system   🧪 Fair points test: different picks yesterday X · fair hit A of B · live hit C of D
                           · days won fair X / live Y
  once, #system + #alerts  🧪 TEST FINISHED card at 30 graded differing legs or on 11/15, whichever first

  python3 apex_fair_points_paper.py            # running report (prints)
  python3 apex_fair_points_paper.py --line     # yesterday's 9 AM line (prints)
  python3 apex_fair_points_paper.py --card     # the finish card as it would read now (prints)
  python3 apex_fair_points_paper.py --run      # what the scheduler runs
  python3 apex_fair_points_paper.py --shadow <publish_id>   # (re)build one publish's shadow
Undo: FAIR_POINTS_PAPER_ON = False (no new shadow rows, no posts; the file stays).
"""
import json, math, os, sqlite3, sys
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
HIST_DB = os.path.join(HERE, "data", "board_history.sqlite")
DB = os.path.join(HERE, "data", "fair_points_paper.sqlite")
STATE = os.path.join(HERE, "data", "fair_points_paper_state.json")
CT = ZoneInfo("America/Chicago")
FAIR_POINTS_PAPER_ON = True
START_DAY = "2026-10-26"        # first slip day counted (first pick's CT date)
END_DAY = "2026-11-15"          # the test finishes on this day at the latest
FINISH_LEGS = 30                # ... or once this many differing legs are graded
DAILY_AT = (9, 0)
BOARDS = ("top3", "elite6")
NAMES = {"top3": "Top 3", "elite6": "Elite 6"}
TOP_BAR = 75.0                  # Win Prob the top 3 spots need (TOP_SLOTS_MIN_WP)
TOP_N = 3

# ---- the fit, frozen (pre-Oct 19 graded data) ----
A_TG, B_TG = -9.34780343230512, 12.479375560664641
POOL = [0.166911124059989, 0.5146394061313082]
FIT = {   # (category, direction): [w0, w1 (logit Win Prob), w2 (women), w3 (played yesterday)]
    ("aces", "LESS"): [0.129351, 0.497836, 0.0, 0.0],
    ("aces", "MORE"): [0.033045, 0.400877, 0.0, 0.0],
    ("breaks", "LESS"): [0.380898, 0.637989, 0.0, 0.0],
    ("breaks", "MORE"): [0.001755, 0.295851, 0.0, 0.0],
    ("dfs", "LESS"): [0.036246, 0.437795, 0.0, 0.0],
    ("dfs", "MORE"): [-0.234614, 0.30392, 0.0, 0.0],
    ("fantasy_score", "LESS"): [-0.020537, 0.34971, 0.0, 0.0],
    ("fantasy_score", "MORE"): [0.423702, 0.794932, -0.387217, 0.0],
    ("games_won", "LESS"): [0.343888, 0.591738, 0.0, 0.0],
    ("games_won", "MORE"): [0.429174, 0.567427, 0.0, 0.0],
    ("tiebreaks", "LESS"): [-0.168374, 0.332785, 0.0, 0.0],
    ("total_games", "LESS"): [-0.167748, 0.359659, 0.0, 0.0],
}

_lg = lambda p: math.log(p / (1 - p))
_sg = lambda z: 1 / (1 + math.exp(-z))


def is_tg(cat, d):
    return cat == "total_games" and str(d or "").upper() == "MORE"


def fair_score(cat, d, wp, wta=0, yday=0):
    """Fair rank score (0-100) for a non-TG prop with Win Prob wp (0-100)."""
    w = FIT.get((cat, str(d or "").upper()), POOL + [0.0, 0.0])
    p = min(max((wp or 0) / 100.0, 0.01), 0.99)
    hit = _sg(w[0] + w[1] * _lg(p) + w[2] * wta + w[3] * yday)
    return 100.0 * (_lg(hit) - A_TG) / B_TG


_TOUR = {}


def _wta(r):
    k = (r.get("player"), r.get("opponent"))
    if k not in _TOUR:
        try:
            import apex_blowout_signal as bs
            _TOUR[k] = int((bs.tour({"player": k[0], "opponent": k[1]}) or "") == "WTA")
        except Exception:
            _TOUR[k] = 0
    return _TOUR[k]


# ------------------------------------------------------------------ storage
COLS = ["publish_id", "published_at", "board", "arm", "spot_no", "prop_key", "match_start", "player", "opponent",
        "category", "direction", "prop_type", "line", "win_prob", "live_score", "fair_score", "wta", "result"]


def _db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS pubs (publish_id INTEGER PRIMARY KEY, published_at TEXT, trigger TEXT, "
              "board_day TEXT, rebuild_ok_top3 INTEGER, rebuild_ok_elite6 INTEGER, differs_top3 INTEGER, "
              "differs_elite6 INTEGER, written_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS legs (publish_id INTEGER, published_at TEXT, board TEXT, arm TEXT, "
              "spot_no INTEGER, prop_key TEXT, match_start TEXT, player TEXT, opponent TEXT, category TEXT, "
              "direction TEXT, prop_type TEXT, line REAL, win_prob REAL, live_score REAL, fair_score REAL, wta INTEGER, "
              "result TEXT, PRIMARY KEY (publish_id, board, arm, spot_no))")
    c.execute("CREATE INDEX IF NOT EXISTS ix_legs_open ON legs(result, match_start)")
    return c


def _names(r):
    if r.get("player") == "match":
        raw = str(r.get("opponent") or "").replace(" vs ", " v ").split(" v ")
    else:
        raw = [r.get("player"), r.get("opponent")]
    return {str(x).strip().casefold() for x in raw if x}


def _pick(cands, n, score):
    """The live board rules on a pool: best line per match (by score), one per match/player, 75%+ Win
    Prob in the top 3 (short of those: highest Win Prob fills them), then by score. n spots."""
    key = lambda r: (-score(r), -(r.get("apex_score") or 0))
    groups = []
    for r in sorted(cands, key=key):
        g = next((g for g in groups if g[0] & _names(r)), None)
        if g:
            g[0] |= _names(r)
        else:
            groups.append([set(_names(r)), r])
    reps = sorted((g[1] for g in groups), key=key)
    top_n = min(TOP_N, n)
    out = [r for r in reps if (r.get("win_prob") or 0) >= TOP_BAR - 1e-9][:top_n]
    if len(out) < top_n:
        out += sorted([r for r in reps if all(r is not o for o in out)],
                      key=lambda r: (-(r.get("win_prob") or 0), -score(r)))[:top_n - len(out)]
    out += [r for r in reps if all(r is not o for o in out)]
    return out[:n]


def _live(r):
    return r["rank_score"] if r.get("rank_score") is not None else (r.get("win_prob") or 0)


def _fair(r):
    if is_tg(r.get("category"), r.get("direction")):
        return _live(r)
    return r["_fair"]


def shadow_publish(pid):
    """Build + save the shadow Top 3 / Elite 6 for one board-history publish. Fail-open. Returns a summary."""
    if not FAIR_POINTS_PAPER_ON:
        return "off"
    try:
        h = sqlite3.connect("file:%s?mode=ro" % HIST_DB, uri=True, timeout=30)
        h.row_factory = sqlite3.Row
        pub = h.execute("SELECT * FROM publishes WHERE publish_id = ?", (pid,)).fetchone()
        if not pub or pub["platform"] != "prizepicks":
            h.close()
            return "not a PrizePicks publish"
        rows = [dict(r) for r in h.execute("SELECT * FROM history WHERE publish_id = ? AND board IN ('top3', 'elite6') "
                                           "AND spot_kind IN ('main', 'backup')", (pid,))]
        h.close()
        c = _db()
        c.execute("DELETE FROM legs WHERE publish_id = ?", (pid,))
        flags, out = {}, []
        for b in BOARDS:
            rs = [r for r in rows if r["board"] == b]
            main = sorted([r for r in rs if r["spot_kind"] == "main"], key=lambda r: r["spot_no"] or 0)
            if not main:
                flags[b] = (None, None)
                continue
            for r in rs:
                r["_wta"] = 0 if is_tg(r["category"], r["direction"]) else _wta(r)
                r["_fair"] = fair_score(r["category"], r["direction"], r["win_prob"], r["_wta"])
            fair = _pick(rs, len(main), _fair)
            rebuilt = _pick(rs, len(main), _live)
            flags[b] = (int([r["prop_key"] for r in rebuilt] == [r["prop_key"] for r in main]),
                        int({r["prop_key"] for r in fair} != {r["prop_key"] for r in main}))
            for arm, legs in (("live", main), ("fair", fair)):
                for i, r in enumerate(legs, 1):
                    out.append((pid, pub["published_at"], b, arm, i, r["prop_key"], r["match_start"], r["player"],
                                r["opponent"], r["category"], r["direction"], r["prop_type"], r["line"], r["win_prob"],
                                _live(r), _fair(r), r["_wta"], r["result"]))
        c.executemany("INSERT INTO legs (%s) VALUES (%s)" % (", ".join(COLS), ", ".join("?" * len(COLS))), out)
        c.execute("INSERT OR REPLACE INTO pubs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (pid, pub["published_at"], pub["trigger"], pub["board_day"], flags.get("top3", (None,))[0],
                   flags.get("elite6", (None,))[0], flags.get("top3", (None, None))[1],
                   flags.get("elite6", (None, None))[1], datetime.now().isoformat(timespec="seconds")))
        c.commit()
        c.close()
        return "publish %s: %d shadow rows, differs top3=%s elite6=%s" % (
            pid, len(out), flags.get("top3", (None, None))[1], flags.get("elite6", (None, None))[1])
    except Exception as ex:
        print("[FairPaper] shadow failed (%s): %s" % (type(ex).__name__, ex))
        return "failed: %s" % ex


# ------------------------------------------------------------------ grading + official slips
def grade():
    """Copy settled results from board history onto open legs. Returns legs filled."""
    c = _db()
    open_ = c.execute("SELECT DISTINCT prop_key, match_start FROM legs WHERE result IS NULL").fetchall()
    if not open_:
        c.close()
        return 0
    h = sqlite3.connect("file:%s?mode=ro" % HIST_DB, uri=True, timeout=30)
    res = {}
    for k, ms, r in h.execute("SELECT prop_key, match_start, result FROM history WHERE result IS NOT NULL"):
        res[(k, ms)] = r
    h.close()
    n = 0
    for k, ms in open_:
        r = res.get((k, ms))
        if r:
            n += c.execute("UPDATE legs SET result = ? WHERE prop_key = ? AND match_start = ? AND result IS NULL",
                           (r, k, ms)).rowcount
    c.commit()
    c.close()
    return n


def _utc(local_iso):
    try:
        return datetime.fromisoformat(local_iso).replace(tzinfo=CT).astimezone(ZoneInfo("UTC")).isoformat()[:19]
    except Exception:
        return local_iso


def _ct_day(start):
    try:
        return datetime.fromisoformat(str(start)[:19]).replace(tzinfo=ZoneInfo("UTC")).astimezone(CT).date().isoformat()
    except Exception:
        return ""


def official():
    """{(day, board, arm): [legs]} -- per arm, the last publish before that slip's own first pick."""
    c = _db()
    by = {}
    for r in c.execute("SELECT * FROM legs ORDER BY publish_id, board, arm, spot_no"):
        by.setdefault((r["publish_id"], r["board"], r["arm"]), []).append(dict(r))
    ok = {(r["publish_id"], "top3"): r["rebuild_ok_top3"] for r in c.execute("SELECT * FROM pubs")}
    ok.update({(r["publish_id"], "elite6"): r["rebuild_ok_elite6"] for r in c.execute("SELECT * FROM pubs")})
    c.close()
    out = {}
    for (pid, b, arm), legs in sorted(by.items()):
        for l in legs:
            l["_rebuild_ok"] = ok.get((pid, b), 1)
        starts = [l["match_start"] for l in legs if l["match_start"]]
        if not starts or _utc(legs[0]["published_at"]) >= min(starts)[:19]:
            continue
        day = _ct_day(min(starts))
        if day >= START_DAY:
            out[(day, b, arm)] = legs   # later publishes overwrite: last board before the first pick
    return out


def _payouts():
    try:
        import apex_slips_v2 as v2
        return v2.STAKE, v2.POWER3, v2.POWER3_FALLBACK, v2.FLEX6
    except Exception:
        return 20.0, {"GGG": 2.2, "GGS": 3.25, "GSS": 3.75, "SSS": 6.0}, 2.2, {6: 3.0, 5: 1.0, 4: 0.4}


def profit(board, legs):
    """$ at $[amount] on the slip, None until every leg is hit/miss (a void or open leg = not counted yet)."""
    stake, power3, fb, flex6 = _payouts()
    rs = [l["result"] for l in legs]
    if any(r not in ("hit", "miss") for r in rs):
        return None
    if board == "top3":
        if len(legs) != 3:
            return None
        code = "".join(sorted("G" if l["prop_type"] == "goblin" else ("S" if l["prop_type"] == "standard" else "D")
                              for l in legs))
        mult = (power3.get(code) or fb) if all(r == "hit" for r in rs) else 0.0
    else:
        if len(legs) != 6:
            return None
        mult = flex6.get(sum(r == "hit" for r in rs), 0.0)
    return stake * mult - stake


def days(off=None):
    """Per slip day: legs, the differing picks (fair IN / live OUT, unique by prop) and $ per arm."""
    off = off if off is not None else official()
    out = {}
    for (day, b, arm), legs in off.items():
        d = out.setdefault(day, {"legs": {}, "in": {}, "out": {}, "money": {}})
        d["legs"][(b, arm)] = legs
    for day, d in out.items():
        for b in BOARDS:
            lv, fr = d["legs"].get((b, "live")), d["legs"].get((b, "fair"))
            if lv is None or fr is None:
                continue
            kl, kf = {l["prop_key"] for l in lv}, {l["prop_key"] for l in fr}
            for l in fr:
                if l["prop_key"] not in kl:
                    d["in"].setdefault(l["prop_key"], l)
            for l in lv:
                if l["prop_key"] not in kf:
                    d["out"].setdefault(l["prop_key"], l)
            for arm, legs in (("live", lv), ("fair", fr)):
                d["money"][(b, arm)] = profit(b, legs)
            d["mismatch"] = d.get("mismatch", 0) + (not lv[0].get("_rebuild_ok", 1))
    return out


def _hits(legs):
    g = [l for l in legs if l["result"] in ("hit", "miss")]
    return sum(l["result"] == "hit" for l in g), len(g), len(legs) - len(g)


def totals(D=None):
    D = D if D is not None else days()
    t = {"days": sorted(D), "in": [], "out": [], "legs": {}, "money": {"live": 0.0, "fair": 0.0},
         "won": {"live": 0, "fair": 0}, "scored_days": 0, "mismatch": 0}
    for day in sorted(D):
        d = D[day]
        t["in"] += list(d["in"].values())
        t["out"] += list(d["out"].values())
        for (b, arm), legs in d["legs"].items():
            t["legs"].setdefault((b, arm), []).extend(legs)
        # a slip's $ counts only when BOTH versions can be scored (no open/void leg on either)
        both = [b for b in BOARDS if d["money"].get((b, "live")) is not None and d["money"].get((b, "fair")) is not None]
        if both:
            t["scored_days"] += 1
            for arm in ("live", "fair"):
                m = sum(d["money"][(b, arm)] for b in both)
                t["money"][arm] += m
                t["won"][arm] += m > 0
        t["mismatch"] += d.get("mismatch", 0)
    return t


def running_rates(since=START_DAY):
    """Since Oct 26, every prop (All Props, last board before its match; Goblin/Demon LESS left out)."""
    h = sqlite3.connect("file:%s?mode=ro" % HIST_DB, uri=True, timeout=30)
    q = """with last as (select prop_key, match_start, max(published_at) pa from history
             where board='allprops' and result in ('hit','miss') and match_start is not null and match_start != ''
             and datetime(published_at, '+5 hours') < datetime(substr(match_start,1,19)) group by prop_key, match_start)
           select h.category, h.direction, h.prop_type, h.win_prob, h.rank_score, h.match_start, h.result
           from history h join last l on h.prop_key=l.prop_key and h.match_start=l.match_start
           and h.published_at=l.pa and h.board='allprops'"""
    fs, aces, tg, seen = [], [], [], set()
    for cat, d, typ, wp, rk, st, res in h.execute(q):
        if _ct_day(st) < since or (str(typ or "").lower() in ("goblin", "demon") and str(d).upper() == "LESS"):
            continue
        hit = res == "hit"
        if cat == "fantasy_score" and str(d).upper() == "MORE" and (wp or 0) >= 80:
            fs.append(hit)
        elif cat == "aces" and (wp or 0) >= 70:
            aces.append(hit)
        elif is_tg(cat, d) and rk is not None and 90 <= rk < 95:
            tg.append(hit)
    h.close()
    return {"Fantasy MORE 80%+": fs, "Aces 70%+": aces, "TG rank 90-95": tg}


def _of(h, n):
    return "%d of %d" % (h, n)


def _money(x):
    return ("+$%.2f" % x if x >= 0 else "-$%.2f" % -x).replace(".00", "")


def _label(r):
    if r.get("player") == "match":
        who = " v ".join((str(x).split() or [""])[-1] for x in str(r.get("opponent") or "").split(" v "))
    else:
        who = (str(r.get("player") or "").split() or [""])[-1]
    cat = {"total_games": "TG", "fantasy_score": "Fantasy", "dfs": "DF", "games_won": "GW", "aces": "Aces",
           "breaks": "Breaks"}.get(r.get("category"), r.get("category"))
    res = {"hit": " ✅", "miss": " ❌", "void": " void", "push": " push"}.get(r.get("result"), " (open)")
    return "%s %s %g %s%s" % (who, cat, r.get("line") or 0, str(r.get("direction") or "").upper(), res)


# ------------------------------------------------------------------ posts
def daily_line(day=None, D=None):
    day = day or (datetime.now(CT).date() - timedelta(days=1)).isoformat()
    D = D if D is not None else days()
    t = totals({k: v for k, v in D.items() if k <= day})
    won = "days won fair %d / live %d" % (t["won"]["fair"], t["won"]["live"])
    d = D.get(day)
    if not d or (not d["in"] and not d["out"]):
        return "\U0001F9EA Fair points test: no different picks yesterday · %s" % won
    a, b, ao = _hits(list(d["in"].values()))
    c_, e, co = _hits(list(d["out"].values()))
    opn = ao + co
    return ("\U0001F9EA Fair points test: different picks yesterday %d · fair hit %s · live hit %s · %s%s" % (
        max(len(d["in"]), len(d["out"])), _of(a, b), _of(c_, e), won, (" (%d not graded yet)" % opn) if opn else ""))


def graded_diff_legs(t=None):
    t = t or totals()
    return sum(1 for l in t["in"] + t["out"] if l["result"] in ("hit", "miss"))


def finish_card(t=None):
    t = t or totals()
    a, b, _ = _hits(t["in"])
    c_, e, _ = _hits(t["out"])
    mf, ml = t["money"]["fair"], t["money"]["live"]
    s = (a - c_) and (1 if a > c_ else -1)
    s += (mf - ml > 0.005) - (ml - mf > 0.005)
    verdict = "Fair points helped" if s > 0 else ("Fair points hurt" if s < 0 else "No difference")
    lines = ["\U0001F9EA TEST FINISHED — Fair points paper test (%s to %s)" % (
                 START_DAY[5:].replace("-", "/"), (t["days"][-1][5:].replace("-", "/") if t["days"] else "-")),
             "**Verdict: %s** — different picks hit %s with fair points vs %s with live rules; $ at $20 %s fair vs %s live." % (
                 verdict, _of(a, b), _of(c_, e), _money(mf), _money(ml)),
             "Different picks: fair IN hit %s · live OUT hit %s" % (_of(a, b), _of(c_, e))]
    for bd in BOARDS:
        f, l = _hits(t["legs"].get((bd, "fair"), [])), _hits(t["legs"].get((bd, "live"), []))
        lines.append("%s legs: fair hit %s · live hit %s" % (NAMES[bd], _of(f[0], f[1]), _of(l[0], l[1])))
    lines.append("Days won: fair %d / live %d (of %d scored days)" % (t["won"]["fair"], t["won"]["live"], t["scored_days"]))
    try:
        rr = running_rates()
        lines.append("Since 10/26, every prop: " + " · ".join("%s hit %s" % (k, _of(sum(v), len(v))) for k, v in rr.items()))
    except Exception as ex:
        lines.append("Running hit rates could not be read (%s)" % type(ex).__name__)
    if t["mismatch"]:
        lines.append("⚠️ %d official board(s) where the shadow's copy of the live rules did not match the posted board"
                     " — those differences may not be fair points alone" % t["mismatch"])
    lines.append("Paper only: the live boards never changed.")
    return "\n".join(lines)


def report():
    D = days()
    t = totals(D)
    out = ["Fair points paper test — %d slip days scored, %d graded differing legs (finish at %d or %s)" % (
        t["scored_days"], graded_diff_legs(t), FINISH_LEGS, END_DAY)]
    for day in sorted(D):
        d = D[day]
        out.append("%s  IN: %s | OUT: %s | $ live T3 %s E6 %s, fair T3 %s E6 %s" % (
            day, "; ".join(map(_label, d["in"].values())) or "-", "; ".join(map(_label, d["out"].values())) or "-",
            d["money"].get(("top3", "live")), d["money"].get(("elite6", "live")),
            d["money"].get(("top3", "fair")), d["money"].get(("elite6", "fair"))))
    out.append(finish_card(t))
    return "\n".join(out)


def _load():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE + ".tmp", "w") as fh:
        json.dump(st, fh, indent=1)
    os.replace(STATE + ".tmp", STATE)


def run(now=None, post=True):
    """Scheduler entry (every 15 min): grade, 9 AM #system line, the finish card once."""
    if not FAIR_POINTS_PAPER_ON:
        return "off"
    now = now or datetime.now(CT)
    import apex_ops as ops
    send = ops.send_discord if post else (lambda ch, m: print("---- #%s ----\n%s" % (ch, m)) or True)
    st = _load()
    today = now.date().isoformat()
    yday = (now.date() - timedelta(days=1)).isoformat()
    n = grade()
    if st.get("finished"):
        return "finished %s; graded %d" % (st["finished"], n)
    D = days()
    t = totals(D)
    did = []
    if (now.hour, now.minute) >= DAILY_AT and st.get("daily") != today and yday >= START_DAY:
        send("system", daily_line(yday, D))
        st["daily"] = today
        did.append("daily line")
    if graded_diff_legs(t) >= FINISH_LEGS or today >= END_DAY:
        card = finish_card(t)
        send("system", card)
        send("alerts", card)
        st["finished"] = today
        did.append("finish card")
    _save(st)
    return "graded %d; %s" % (n, ", ".join(did) or "nothing due")


if __name__ == "__main__":
    if "--run" in sys.argv:
        print(run())
    elif "--line" in sys.argv:
        grade()
        print(daily_line())
    elif "--card" in sys.argv:
        grade()
        print(finish_card())
    elif "--shadow" in sys.argv:
        print(shadow_publish(int(sys.argv[sys.argv.index("--shadow") + 1])))
    else:
        grade()
        print(report())
