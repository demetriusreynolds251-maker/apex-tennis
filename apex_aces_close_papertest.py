#!/usr/bin/env python3
"""
APEX-ACESCLOSE-2026-10-22 (owner: "I like aces plan I want that
implement and test in the background"): background paper test of the
"players close" rule on Aces MORE props. Nothing is posted and no board,
ranking or qualification changes.

Why: the 10/22 study (_backups/line175_rank_test_2026-10-22/test2_report.md)
found Aces MORE at 70%+ hit 8-1 (89%) when the two players are close in
rating (Elo gap <= 100) and 7-7 (50%) when they are not -- too few picks to
change the boards, so it runs here first.

Read-only on the boards: every run rebuilds from what is already stored --
the props database (Win Probability = engine_prob_cal, results from the
normal hourly grader, which boards a prop made) and the board status log
(Apex Score at each board build, from 10/10). Each Aces MORE prop (Goblin /
Standard; Demons have their own board) is written once to LOG, first
sighting wins for the Elo gap, so the bucket is frozen before the match:

  main arm  = Win Prob 70%+ and Apex 67+ (Apex 67 only checkable from 10/10;
              earlier props count when Win Prob is 70%+)
  wide arm  = Win Prob 60%+
  bucket    = "close" (Elo gap <= 100) / "not close" / "no rating"

The alerts job runs log_now() every 30 min (fresh process) and the 9 AM
status shows the record. TEST FINISHED card once at FINISH_PICKS graded
main-arm picks (look back included) or FINISH_DAYS days after START.

  python3 apex_aces_close_papertest.py           # log now + print the record
  python3 apex_aces_close_papertest.py --picks   # every logged pick
"""
import csv, json, os, sqlite3, sys, time
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PROPS_DB = os.path.join(HERE, "data", "props_db.sqlite")
STATUS_LOG = os.path.join(HERE, "apex_board_status_log_v1.csv")
LOG = os.path.join(HERE, "data", "logs", "aces_close_papertest.json")
ACES_CLOSE_TEST_ON = True    # 10/22 owner: background test only
ACES_CLOSE_CARD_LINE = True  # 🤝 line on Aces MORE cards, display only
LOOKBACK = "2026-09-30"
START = "2026-10-23"
FINISH_PICKS = 60
FINISH_DAYS = 14
ELO_CLOSE = 100              # same cut as the 17.5 check (apex_top3_no175_papertest.ELO_CLOSE)
MAIN_PROB, MAIN_APEX, WIDE_PROB = 0.70, 67.0, 0.60
BUCKETS = (("close", "close players"), ("not close", "not close"), ("no rating", "no rating"))
TTL = 600
_cache = {}


def _norm(s):
    import apex_top3_no175_papertest as T
    return T._norm(s)


def _names(player, opponent):
    if player == "match":
        return [x.strip() for x in str(opponent).replace(" vs ", " v ").split(" v ")]
    return [player, opponent]


def elo_gap(names):
    """Elo gap between the two players, or None. Never raises."""
    try:
        import apex_top3_no175_papertest as T
        elo = T._rule_ctx()["elo"]
        e = [elo.get(_norm(x)) for x in names]
        return abs(e[0] - e[1]) if len(e) == 2 and None not in e else None
    except Exception:
        return None


def bucket(gap):
    return "no rating" if gap is None else ("close" if gap <= ELO_CLOSE else "not close")


def _key(md, player, line):
    return "%s|%s|%s" % (md, _norm(player), float(line))


def _load():
    try:
        with open(LOG) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _apex_scores():
    """{key: max Apex Score seen on a board build} and {key} of props a board posted, from the status log."""
    ap, onb = {}, set()
    try:
        with open(STATUS_LOG, newline="") as fh:
            for r in csv.DictReader(fh):
                if r["category"] != "aces" or r["direction"].upper() != "MORE" or r["match_date"] < LOOKBACK:
                    continue
                k = _key(r["match_date"], r["player"], r["line"])
                if r["board"] != "all":
                    onb.add(k)
                elif r["apex_score"]:
                    ap[k] = max(ap.get(k, 0.0), float(r["apex_score"]))
    except Exception:
        pass
    return ap, onb


def log_now():
    """Adds / updates every Aces MORE prop at 60%+ since LOOKBACK. Returns (added, total). Never raises."""
    try:
        if not ACES_CLOSE_TEST_ON:
            return 0, 0
        data = _load()
        ap, onb = _apex_scores()
        con = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True)
        rows = con.execute(
            "select match_date, player, opponent, line_last, coalesce(engine_prob_cal, engine_prob), boards, result "
            "from projections where category = 'aces' and upper(engine_direction) = 'MORE' "
            "and lower(coalesce(odds_type, '')) != 'demon' and match_date >= ? "
            "and coalesce(engine_prob_cal, engine_prob) >= ? and player != 'match'", (LOOKBACK, WIDE_PROB)).fetchall()
        con.close()
        added = 0
        now = datetime.now().isoformat(timespec="seconds")
        for md, pl, op, line, prob, boards, _res in rows:
            if line is None or prob is None:
                continue
            k = _key(md, pl, line)
            e = data.get(k)
            if e is None:
                gap = elo_gap(_names(pl, op))
                e = data[k] = {"logged_at": now, "match_date": md, "player": pl, "opponent": op,
                               "line": float(line), "prob": 0.0, "apex": None,
                               "elo_gap": None if gap is None else round(gap), "bucket": bucket(gap),
                               "on_board": False, "main": False}
                added += 1
            e["prob"] = round(max(e["prob"], float(prob)), 4)
            if k in ap:
                e["apex"] = max(e["apex"] or 0.0, ap[k])
            e["on_board"] = bool(e["on_board"] or (boards or "").strip() or k in onb)
            e["main"] = bool(e["main"] or (e["prob"] >= MAIN_PROB and (e["apex"] is None or e["apex"] >= MAIN_APEX)))
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        tmp = LOG + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1, default=str)
        os.replace(tmp, LOG)
        return added, len(data)
    except Exception as e:
        print("[AcesCloseTest] log skipped (%s): %s" % (type(e).__name__, e))
        return 0, 0


def _results():
    """{key: True (more) / False (less) / "void"} from the normal grader's results in the props database."""
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True)
        for md, pl, line, res in con.execute(
                "select match_date, player, line_last, result from projections where category = 'aces' "
                "and result in ('more', 'less', 'void') and match_date >= ?", (LOOKBACK,)):
            out[_key(md, pl, line)] = "void" if res == "void" else res == "more"
        con.close()
    except Exception:
        pass
    return out


def tally():
    """{"main"|"wide": {bucket: [w, l, open]}}, graded main-arm count."""
    data = _load()
    R = _results()
    T = {a: {b: [0, 0, 0] for b, _ in BUCKETS} for a in ("main", "wide")}
    for k, e in data.items():
        h = R.get(k)
        if h == "void":   # voided props are left out, not counted as open
            continue
        for a in ("main", "wide"):
            if a == "main" and not e.get("main"):
                continue
            x = T[a][e["bucket"]]
            x[2 if h is None else (0 if h else 1)] += 1
    graded = sum(x[0] + x[1] for x in T["main"].values())
    return T, graded


def _rec(x):
    n = x[0] + x[1]
    return ("%d-%d (%.0f%%)" % (x[0], x[1], 100.0 * x[0] / n)) if n >= 5 else "%d-%d" % (x[0], x[1])


def _status(T):
    return ", ".join("%s %s" % (lbl, _rec(T[b])) for b, lbl in BUCKETS)


def summary():
    """Daily status lines (apex_alerts). Never raises."""
    try:
        if not ACES_CLOSE_TEST_ON:
            return []
        log_now()
        T, graded = tally()
        days = max(0, (date.today() - date.fromisoformat(START)).days + 1)
        opn = sum(x[2] for x in T["main"].values())
        return ["Aces test: %s" % _status(T["main"]),
                "  (Aces MORE at 70%% / Apex 67 since %s, %d graded%s, %s; not posted, boards unchanged; finishes at %d graded or %s)"
                % (LOOKBACK[5:].replace("-", "/"), graded, (", %d open" % opn) if opn else "",
                   ("day %d of %d" % (days, FINISH_DAYS)) if days else "test starts " + START[5:].replace("-", "/"),
                   FINISH_PICKS, _finish_date()[5:].replace("-", "/")),
                "  Wider check, all Aces MORE 60%%+: %s" % _status(T["wide"])]
    except Exception as e:
        return ["Aces test: unavailable (%s)" % type(e).__name__]


def _finish_date():
    from datetime import timedelta
    return (date.fromisoformat(START) + timedelta(days=FINISH_DAYS - 1)).isoformat()


def finish_check():
    if not ACES_CLOSE_TEST_ON:
        return False, None
    log_now()
    T, graded = tally()
    if graded < FINISH_PICKS and date.today().isoformat() < _finish_date():
        return False, None
    c, nc = T["main"]["close"], T["main"]["not close"]
    rc = c[0] / float(c[0] + c[1]) if c[0] + c[1] else 0
    rn = nc[0] / float(nc[0] + nc[1]) if nc[0] + nc[1] else 0
    if c[0] + c[1] < 10 or nc[0] + nc[1] < 10:
        verdict = ("NOT ENOUGH PICKS -- close players %d of %d, not close %d of %d; keep testing"
                   % (c[0], c[0] + c[1], nc[0], nc[0] + nc[1]))
    elif rc - rn >= 0.10:
        verdict = ("YES -- Aces picks with close players hit more: %d out of 100 vs %d out of 100 when not close. "
                   "Recommend KEEP the rule (ask before it touches the boards)" % (round(100 * rc), round(100 * rn)))
    else:
        verdict = ("NO -- close players did not hit clearly more: %d out of 100 vs %d out of 100 when not close. "
                   "Recommend DROP the rule" % (round(100 * rc), round(100 * rn)))
    rows = [("Test", "Aces MORE, players close (rating gap 100 or less) vs not (since %s, %d graded picks at 70%% / Apex 67)"
             % (LOOKBACK[5:].replace("-", "/"), graded)),
            ("Close players", _rec(c)), ("Not close", _rec(nc)), ("No rating", _rec(T["main"]["no rating"])),
            ("Wider check, 60%+", _status(T["wide"])), ("Verdict", verdict),
            ("Decision", "keep the players-close rule for Aces (say go to use it on the boards) / drop it / keep testing")]
    return True, rows


def card_line(p):
    """🤝 line on Aces MORE cards with live numbers from the test. Display only. Never raises; '' otherwise."""
    try:
        if not ACES_CLOSE_CARD_LINE or p.get("category") != "aces" or str(p.get("direction") or "").upper() != "MORE":
            return ""
        if not _cache.get("at") or time.time() - _cache["at"] > TTL:
            _cache.update(T=tally()[0]["main"], at=time.time())
        T = _cache["T"]
        b = bucket(elo_gap(_names(p.get("player"), p.get("opponent"))))
        if b == "close":
            x = T["close"]
            return "\U0001F91D Players close ✅ (close-match Aces have hit %d of %d so far, test running)" % (x[0], x[0] + x[1])
        if b == "not close":
            x = T["not close"]
            return "\U0001F91D Players not close ⚠ (these have hit %d of %d so far, test running)" % (x[0], x[0] + x[1])
        return "\U0001F91D Rating gap unknown"
    except Exception:
        return ""


if __name__ == "__main__":
    a, n = log_now()
    print("logged %d new, %d total" % (a, n))
    print("\n".join(summary()))
    if "--picks" in sys.argv:
        R = _results()
        for k, e in sorted(_load().items(), key=lambda kv: kv[1]["match_date"]):
            h = R.get(k)
            print("%s %-24s v %-24s o%-4s %2d%% apex %-4s gap %-5s %-9s %s %s %s" % (
                e["match_date"][5:], e["player"], e["opponent"], e["line"], round(100 * e["prob"]),
                "?" if e["apex"] is None else int(e["apex"]), e["elo_gap"], e["bucket"],
                "MAIN" if e["main"] else "wide", "board" if e["on_board"] else "-",
                {True: "W", False: "L", None: "open", "void": "void"}[h]))
