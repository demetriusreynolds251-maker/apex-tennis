#!/usr/bin/env python3
"""
APEX-BOOKLINES-2026-10-11 (owner, approved "do both" + "go" 2026-10-11):
bet365's view of every Total Games prop at publish time.

1. annotate(picks): for each Total Games prop, works out bet365's chance for
   OUR side of the PrizePicks line from bet365's main Over/Under line and its
   no-vig odds (a different PrizePicks number is converted with a normal
   spread of BOOK_SD games). Adds p["apex_book_prob"] and a card line
   p["apex_book_note"]. DISPLAY ONLY -- no board ranks or qualifies on it.
2. Logs one row per Total Games prop per publish to apex_book_lines_log_v1.csv
   for the live tests (Top 3 with bet365 80%+, Standards agree vs disagree).

Why: backfill of bet365 closing lines for 345 settled matches (Sep 25-Oct 10):
Goblin TG MORE won 77.1% when bet365 gave our side 80-90% vs 57.6% under 70%;
Standard TG won 34.1% when bet365 gave our side under 45%, and 65.6% when the
model and bet365 both said 55%+. Closing lines know more than the line at bet
time, so the live effect is expected to be smaller -- hence a live test first.

Source: SportsAPI365 pre-match odds (Bet365 only for game totals).
"""
import csv, os
from datetime import datetime
from statistics import NormalDist

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "apex_book_lines_log_v1.csv")
FIELDS = ["captured_at", "match_date", "player", "opponent", "category", "prop_type", "pp_line", "direction",
          "prob", "prob_honest", "book", "book_line", "over_odds", "under_odds", "book_over_prob",
          "book_side_prob", "pp_vs_book"]
BOOK_SD = 4.0
AGREE, DISAGREE = 0.55, 0.45


def _pair(p):
    if p.get("player") == "match":
        xs = [x.strip() for x in str(p.get("opponent") or "").split(" v ")]
        return (xs + [""])[:2]
    return [p.get("player"), p.get("opponent")]


# APEX-FONBET-2026-11-07 (owner, "Go fonbet total"): SportsAPI365 stopped sending bet365's Total Games line
# around 11/3 (FonBet, Pinnacle, Marathonbet still there). When bet365 has no main total, FonBet's main total (the
# line closest to even odds) is used and logged with book "fonbet"; cards say FonBet. Undo: FONBET_FALLBACK = False.
FONBET_FALLBACK = True
_BOOK_OF = {}


def _main_line(pm):
    ml = _main_line_book(pm, "bet365")
    if ml is None and FONBET_FALLBACK:
        ml = _main_line_book(pm, "fonbet")
        if ml is not None:
            _BOOK_OF[ml] = "fonbet"
    return ml


def _main_line_book(pm, book):
    best = None
    for r in (pm.get("Over Under") or []):
        if str(r.get("bookmaker", "")).lower() != book:
            continue
        try:
            ln, o1, o2 = float(str(r.get("line")).split()[0]), float(r["od1"]), float(r["od2"])
        except Exception:
            continue
        if ln < 10:
            continue
        if best is None or abs(o1 - o2) < best[0]:
            best = (abs(o1 - o2), ln, o1, o2)
    return best[1:] if best else None


def side_prob(book_line, over_prob, pp_line, direction):
    """bet365's chance for our side of the PrizePicks line."""
    op = min(max(over_prob, 0.01), 0.99)
    mu = book_line + BOOK_SD * NormalDist().inv_cdf(op)
    p_over = 1 - NormalDist(mu, BOOK_SD).cdf(pp_line)
    return p_over if str(direction).upper() == "MORE" else 1 - p_over


def note_for(bp):
    if bp >= AGREE:
        return "\U0001F4D7 bet365: %d%% for this side (agrees)" % round(bp * 100)
    if bp < DISAGREE:
        return "⚠️ bet365 disagrees: only %d%% for this side" % round(bp * 100)
    return "\U0001F4D8 bet365: %d%% for this side (neutral)" % round(bp * 100)


def _logged_lines(max_age_hours=36):
    """APEX-REPRICEBOOK-2026-10-13: latest bet365 main line already logged per
    match pair -> (book_line, over_odds, under_odds). No API calls."""
    out = {}
    try:
        cutoff = datetime.now().timestamp() - max_age_hours * 3600
        with open(LOG, newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    if datetime.fromisoformat(r["captured_at"]).timestamp() < cutoff:
                        continue
                    a, b = _pair(r)
                    out[tuple(sorted([str(a).lower(), str(b).lower()]))] = (
                        float(r["book_line"]), float(r["over_odds"]), float(r["under_odds"]))
                except (KeyError, TypeError, ValueError):
                    continue
    except Exception:
        pass
    return out


def annotate(picks, prob_fn, honest_fn, max_matches=80, log=True, offline=False):
    """Returns a new list with Total Games props annotated. Fail-open.
    offline=True (re-pricing republish): reuse the bet365 line already logged
    for the match instead of calling the API -- APEX-REPRICEBOOK-2026-10-13
    (owner, "Go"): the re-price republish skipped bet365 entirely, so
    every Total Games Standard lost its "bet365 agrees" check and dropped off
    Best Standard (Xiyu Wang/Pohankova TG 20.5 on 10/13, 17 min before start)."""
    pf = None
    try:
        import apex_price_feed as pf
        pf.VERBOSE = False
    except Exception:
        if not offline:
            return picks
        pf = None
    try:   # APEX-BET365-THREEPART-2026-10-22: offline = shared reader, matched on names AND match date
        import apex_bet365_totals as b3
    except Exception:
        b3 = None
    cache, rows, out = {}, [], []
    # APEX-HOURLYTOTALS-2026-10-23 (owner, "Go for everything"): the hourly re-price
    # republish also asks SA365 for bet365's total on matches the log has NO total for yet,
    # so the 17.5 check and the room line have numbers before 5 PM. Blanks only: a match
    # with a logged total is never re-fetched or overwritten. Only those new rows are logged.
    live, live_tries = set(), 0
    now = datetime.now().isoformat(timespec="seconds")
    for p in picks:
        if p.get("category") != "total_games":
            out.append(p); continue
        a, b = _pair(p)
        key = tuple(sorted([str(a).lower(), str(b).lower()]))
        if offline:
            key = key + (str(p.get("match_date") or "")[:10],)
            if key not in cache:
                bl0, op0 = b3.total([a, b], p.get("match_date"))[:2] if b3 else (None, None)
                # (line, over odds, under odds) shape; fair odds rebuilt from the logged no-vig chance
                cache[key] = (bl0, 1 / op0, 1 / (1 - op0)) if bl0 is not None and op0 and 0 < op0 < 1 else None
                if cache[key] is None and bl0 is None and pf is not None and live_tries < 40:
                    live_tries += 1
                    try:
                        found = pf._find(a, b, True)
                        e = found[0] if isinstance(found, tuple) else found
                        pm = pf._prematch(e.get("id"), True) if e and not pf._started(e) else None
                        cache[key] = _main_line(pm) if pm else None
                        if cache[key]:
                            live.add(key)
                    except Exception:
                        cache[key] = None
        if key not in cache:
            if len(cache) >= max_matches:
                out.append(p); continue
            try:
                found = pf._find(a, b, True)
                e = found[0] if isinstance(found, tuple) else found
                pm = pf._prematch(e.get("id"), True) if e and not pf._started(e) else None
                cache[key] = _main_line(pm) if pm else None
            except Exception:
                cache[key] = None
        ml = cache[key]
        if not ml:
            out.append(p); continue
        bl, o1, o2 = ml
        over_p = (1 / o1) / ((1 / o1) + (1 / o2))
        try:
            ppl = float(p.get("line"))
        except (TypeError, ValueError):
            out.append(p); continue
        bp = side_prob(bl, over_p, ppl, p.get("direction"))
        q = dict(p, apex_book_prob=round(bp, 4), apex_book_note=note_for(bp))
        out.append(q)
        if offline and key not in live:
            continue
        pr = prob_fn(p)
        hp = honest_fn(pr)
        rows.append({"captured_at": now, "match_date": p.get("match_date"), "player": p.get("player"),
                     "opponent": p.get("opponent"), "category": p["category"],
                     "prop_type": str(p.get("prop_type") or "").lower(), "pp_line": ppl,
                     "direction": p.get("direction"), "prob": "%.4f" % pr, "prob_honest": ("%.4f" % hp) if hp else "",
                     "book": _BOOK_OF.get(ml, "bet365"), "book_line": bl, "over_odds": o1, "under_odds": o2,
                     "book_over_prob": "%.4f" % over_p, "book_side_prob": "%.4f" % bp, "pp_vs_book": "%+.1f" % (ppl - bl)})
    if (log or live) and rows:
        try:
            fresh = not os.path.exists(LOG)
            with open(LOG, "a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS)
                if fresh:
                    w.writeheader()
                w.writerows(rows)
        except Exception as e:
            print("[BookLines] log failed: %s" % e)
    return out
