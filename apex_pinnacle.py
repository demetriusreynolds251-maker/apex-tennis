#!/usr/bin/env python3
"""
APEX-PINNACLE-2026-10-22 (owner, "Go" on 10/22): Pinnacle's view of each
match from OddsPapi, pulled once a day and cached on disk. DISPLAY + PAPER TEST
ONLY -- nothing here ranks, qualifies or removes a prop.

  - daily(): at most one successful pull a day (after PULL_AFTER_CT). One
    /odds-by-tournaments request returns every ATP/WTA match in this week's
    tournaments with Pinnacle's match winner, total games ladder and games-won
    ladders. The tournament list and player names come from /fixtures, which is
    called only when that cache is over FIXTURE_TTL_H old or the odds name a
    player we don't know yet (so most days use 1 request, a few use 2).
  - lookup(p) / room(p): reads the newest cached pull (no API calls, never raises).
  - card_line(p): the 🎯 Pinnacle cushion line on Total Games cards.
  - books_line(p): "📗 Books say ~71% (Apex 75%)" for Goblin cards (bet365 + Pinnacle).
  - Paper test "PINNACLE CUSHION": every playable Total Games prop on a /fetch or
    5 PM publish is logged with its Pinnacle room; the daily status grades them and
    a TEST FINISHED card posts once at FINISH_GRADED graded props.

Fail-safe: a failed pull writes {"ok": false, "error": ...} for the day; cards then
say "🎯 Pinnacle: no line today" and apex_data_health shows a Pinnacle DATA CHECK
failure in the #general fetch report. The API key is read from oddspapi_key.txt and
never printed or logged. Request budget: MONTH_LIMIT a month (free tier 250), the
pull stops itself at MONTH_STOP.

  python3 apex_pinnacle.py status      # cache + request count (no API call)
  python3 apex_pinnacle.py pull        # pull now if today's pull isn't done
  python3 apex_pinnacle.py test        # paper test record
"""
import json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from statistics import NormalDist

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "oddspapi_key.txt")
DIR = os.path.join(HERE, "data", "pinnacle")
REQ_LOG = os.path.join(DIR, "requests.log")
FIX_CACHE = os.path.join(DIR, "fixtures_cache.json")
NAMES = os.path.join(DIR, "participants.json")
TEST_LOG = os.path.join(HERE, "data", "logs", "pinnacle_cushion_papertest.json")
MARKETS_FILE = os.path.join(HERE, "oddspapi_raw", "markets_tennis.json")
BASE = "https://api.oddspapi.io/v4"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MONTH_LIMIT = 250
MONTH_STOP = 245
# requests already used this month before this module started counting (10/21 probe notes: ~193 used in Oct)
MONTH_SEED = {"2026-10": 193}
PULL_AFTER_CT = 15            # first pull attempt at/after 3 PM CT (before the 5 PM /fetch)
RETRY_EVERY_H = 2             # after a failed pull, try again at most every 2 hours ...
MAX_TRIES_PER_DAY = 3         # ... and at most 3 times a day
FIXTURE_TTL_H = 72
MAX_AGE_H = 36                # a pull older than this is "no line today"
ROOM_OK = 3.0                 # 3+ games of room = ✅ (same bar as the bet365 📏 line)
BOOK_SD = 4.0
FINISH_GRADED = 100
# APEX-ODDSPAPI-BACKUP-2026-10-22 (owner, "1,4,5,6 go" on 10/22, item 4): when OddsPapi
# can't give today's lines (the pull failed, the month is at MONTH_STOP, or the reply was
# empty) cards use (a) the last good pull if under BACKUP_CACHE_MAX_H old, else (b) bet365's
# main line from the SportsAPI365 book-line log, and DATA CHECK says BACKUP_TEXT. Never makes
# an extra OddsPapi request. Undo: ODDSPAPI_BACKUP_ON = False (old 36h cache rule, no notice).
ODDSPAPI_BACKUP_ON = True
BACKUP_CACHE_MAX_H = 24
BACKUP_TEXT = "\U0001F4CF Book lines: using backup (OddsPapi unavailable)"


def _ct_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago"))
    except Exception:
        return datetime.now()


def _load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, default=str)
    os.replace(tmp, path)


def _day_file(day):
    return os.path.join(DIR, "%s.json" % day)


# ------------------------------------------------------------------ request budget
def month_used(month=None):
    month = month or datetime.now().strftime("%Y-%m")
    n = MONTH_SEED.get(month, 0)
    try:
        with open(REQ_LOG) as fh:
            n += sum(1 for ln in fh if ln.startswith(month))
    except Exception:
        pass
    return n


def _get(path, params):
    if month_used() >= MONTH_STOP:
        raise RuntimeError("monthly OddsPapi budget reached (%d of %d used)" % (month_used(), MONTH_LIMIT))
    with open(KEY_FILE) as fh:
        key = fh.read().strip()
    q = dict(params, apiKey=key)
    req = urllib.request.Request(BASE + path + "?" + urllib.parse.urlencode(q),
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    status, body, err = 0, None, ""
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status = r.status
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        status, err = e.code, "HTTP %d" % e.code
    except Exception as e:
        err = type(e).__name__
    os.makedirs(DIR, exist_ok=True)
    with open(REQ_LOG, "a") as fh:   # never the key: path + status only
        fh.write("%s %s %s %s\n" % (datetime.now().isoformat(timespec="seconds"), path, status, err))
    if err:
        raise RuntimeError("OddsPapi %s: %s" % (path, err))
    return body


def _rows(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("data", "fixtures", "result"):
            if isinstance(body.get(k), list):
                return body[k]
    return []


# ------------------------------------------------------------------ markets
_MK = {}


def _markets():
    """marketId -> (kind, handicap, over/side-1 outcome id, under/side-2 outcome id)."""
    if _MK:
        return _MK
    want = {"moneyline": "ml", "totals-games": "tg", "teamtotals-games-team1": "gw1", "teamtotals-games-team2": "gw2"}
    for r in _load(MARKETS_FILE, []):
        if r.get("sportId") != 12 or r.get("marketType") not in want:
            continue
        if r["marketType"] == "moneyline" and r.get("period") != "result":
            continue
        if r["marketType"] != "moneyline" and r.get("period") not in ("result", None):
            continue
        outs = r.get("outcomes") or []
        if len(outs) != 2:
            continue
        a, b = outs[0], outs[1]
        if r["marketType"] != "moneyline" and str(a.get("outcomeName")).lower() != "over":
            a, b = b, a
        _MK[str(r["marketId"])] = (want[r["marketType"]], float(r.get("handicap") or 0), str(a["outcomeId"]), str(b["outcomeId"]))
    return _MK


def _price(market, oid):
    try:
        pl = ((market.get("outcomes") or {}).get(oid) or {}).get("players") or {}
        x = pl.get("0") or next(iter(pl.values()))
        if isinstance(x, list):
            x = x[-1]
        if x.get("active") is False:
            return None, False
        return float(x["price"]), bool(x.get("mainLine"))
    except Exception:
        return None, False


def _devig(o1, o2):
    a, b = 1.0 / o1, 1.0 / o2
    return a / (a + b)


def _parse_fixture(fx):
    """Pinnacle numbers for one fixture from an odds response."""
    pin = ((fx.get("bookmakerOdds") or {}).get("pinnacle") or {}).get("markets") or {}
    mk = _markets()
    out = {"ml1": None, "tg": {}, "gw1": {}, "gw2": {}, "tg_main": None, "gw1_main": None, "gw2_main": None}
    best = {}
    for mid, m in pin.items():
        spec = mk.get(str(mid))
        if not spec:
            continue
        kind, h, oa, ob = spec
        pa, ma = _price(m, oa)
        pb, mb = _price(m, ob)
        if not pa or not pb or pa <= 1 or pb <= 1:
            continue
        prob_a = _devig(pa, pb)
        if kind == "ml":
            out["ml1"] = round(prob_a, 4)
            continue
        out[kind][str(h)] = round(prob_a, 4)        # over probability at this handicap
        score = (0 if (ma or mb) else 1, abs(pa - pb))   # Pinnacle's main line first, else the most even price
        if kind not in best or score < best[kind][0]:
            best[kind] = (score, h)
    for kind in ("tg", "gw1", "gw2"):
        if kind in best:
            out[kind + "_main"] = best[kind][1]
    return out


# ------------------------------------------------------------------ pull
def _fixtures(force=False):
    fc = _load(FIX_CACHE, {})
    fresh = fc.get("pulled_at") and (datetime.now() - datetime.fromisoformat(fc["pulled_at"])).total_seconds() < FIXTURE_TTL_H * 3600
    if fresh and not force:
        return fc
    now = datetime.now(timezone.utc)
    body = _get("/fixtures", {"sportId": 12, "from": now.strftime("%Y-%m-%dT00:00:00Z"),
                              "to": (now + timedelta(days=3)).strftime("%Y-%m-%dT23:59:59Z")})
    names = _load(NAMES, {})
    tours = {}
    for f in _rows(body):
        slug = str(f.get("tournamentSlug") or "").lower()
        if str(f.get("categorySlug") or "").lower() not in ("atp", "wta") or "doubles" in slug or "mixed" in slug:
            continue
        tours[str(f.get("tournamentId"))] = f.get("tournamentName")
        for i in ("1", "2"):
            if f.get("participant%sId" % i) and f.get("participant%sName" % i):
                names[str(f["participant%sId" % i])] = f["participant%sName" % i]
    _save(NAMES, names)
    fc = {"pulled_at": datetime.now().isoformat(timespec="seconds"), "tournaments": tours}
    _save(FIX_CACHE, fc)
    return fc


def pull(day=None):
    """One day's pull. Returns the day's cache dict. Raises nothing."""
    day = day or _ct_now().date().isoformat()
    path = _day_file(day)
    prev = _load(path, {})
    tries = int(prev.get("tries") or 0) + 1
    res = {"day": day, "pulled_at": datetime.now().isoformat(timespec="seconds"), "tries": tries}
    try:
        fc = _fixtures()
        if not fc.get("tournaments"):
            fc = _fixtures(force=True)
        tids = sorted(fc.get("tournaments") or {})
        if not tids:
            raise RuntimeError("no ATP/WTA tournaments in the fixture list")
        body = _get("/odds-by-tournaments", {"bookmaker": "pinnacle", "tournamentIds": ",".join(tids)})
        names = _load(NAMES, {})
        rows = _rows(body)
        if any(str(f.get(k)) not in names for f in rows for k in ("participant1Id", "participant2Id")) and \
                (datetime.now() - datetime.fromisoformat(fc["pulled_at"])).total_seconds() > 3600:
            _fixtures(force=True)   # a new player we can't name yet: refresh names once (1 extra request)
            names = _load(NAMES, {})
        matches = []
        for f in rows:
            n1, n2 = names.get(str(f.get("participant1Id"))), names.get(str(f.get("participant2Id")))
            if not n1 or not n2:
                continue
            m = _parse_fixture(f)
            if m["ml1"] is None and m["tg_main"] is None:
                continue
            m.update(p1=n1, p2=n2, start=f.get("startTime"), fixture=f.get("fixtureId"))
            matches.append(m)
        if not matches:
            raise RuntimeError("Pinnacle returned no tennis prices (%d fixtures)" % len(rows))
        res.update(ok=True, matches=matches, error="")
    except Exception as e:
        res.update(ok=False, matches=prev.get("matches") or [], error=str(e)[:200])
    if prev.get("ok") and not res.get("ok"):
        return prev   # never overwrite a good pull with a failed retry
    _save(path, res)
    return res


def daily():
    """Scheduler hook (hourly): one successful pull a day after PULL_AFTER_CT. Never raises."""
    try:
        now = _ct_now()
        day = now.date().isoformat()
        st = _load(_day_file(day), {})
        if st.get("ok") or now.hour < PULL_AFTER_CT:
            return "skip (%s)" % ("done today" if st.get("ok") else "before %d:00 CT" % PULL_AFTER_CT)
        if int(st.get("tries") or 0) >= MAX_TRIES_PER_DAY:
            return "skip (gave up today after %d tries: %s)" % (st.get("tries"), st.get("error"))
        if st.get("pulled_at") and (datetime.now() - datetime.fromisoformat(st["pulled_at"])).total_seconds() < RETRY_EVERY_H * 3600:
            return "skip (last try under %dh ago)" % RETRY_EVERY_H
        r = pull(day)
        return ("ok: %d matches" % len(r.get("matches") or [])) if r.get("ok") else ("FAILED: %s" % r.get("error"))
    except Exception as e:
        return "FAILED: %s" % type(e).__name__


# ------------------------------------------------------------------ read side (no API calls)
_read = {"ts": 0, "data": None}


def unavailable():
    """APEX-ODDSPAPI-BACKUP-2026-10-22: why OddsPapi can't give today's lines, or '' when it can
    (or when today's pull simply isn't due yet). Reads files only, never raises."""
    try:
        st = _load(_day_file(_ct_now().date().isoformat()), {})
        if st.get("ok"):
            return ""
        if month_used() >= MONTH_STOP:
            return "monthly budget at its stop (%d of %d used)" % (month_used(), MONTH_LIMIT)
        return str(st.get("error") or "")
    except Exception:
        return ""


def _bet365_lines():
    """APEX-BET365-THREEPART-2026-10-22: True when the bet365 log has recent rows (backup available)."""
    try:
        import apex_bet365_totals as b3
        return b3.log_has_any(48)
    except Exception:
        return False


def backup_mode():
    """(reason, 'cache' | 'bet365' | 'none') while OddsPapi is unavailable, else ('', None)."""
    if not ODDSPAPI_BACKUP_ON:
        return "", None
    why = unavailable()
    if not why:
        return "", None
    if latest():
        return why, "cache"
    return why, ("bet365" if _bet365_lines() else "none")


def latest():
    """Newest successful pull within MAX_AGE_H, else None. Cached 5 minutes.
    APEX-ODDSPAPI-BACKUP-2026-10-22: while OddsPapi is unavailable the cache must be
    under BACKUP_CACHE_MAX_H old (else cards fall back to bet365)."""
    if _read["data"] is not None and time.time() - _read["ts"] < 300:
        return _read["data"] or None
    best = None
    max_h = BACKUP_CACHE_MAX_H if (ODDSPAPI_BACKUP_ON and unavailable()) else MAX_AGE_H
    try:
        for fn in sorted(os.listdir(DIR), reverse=True)[:6]:
            if not fn[:4].isdigit() or not fn.endswith(".json"):
                continue
            d = _load(os.path.join(DIR, fn), {})
            if d.get("ok") and d.get("pulled_at") and \
                    (datetime.now() - datetime.fromisoformat(d["pulled_at"])).total_seconds() < max_h * 3600:
                best = d
                break
    except Exception:
        best = None
    _read.update(ts=time.time(), data=best or {})
    return best


def today_status():
    """(ok, detail) for the DATA CHECK: did the pull that cards rely on work?"""
    why, mode = backup_mode()   # APEX-ODDSPAPI-BACKUP-2026-10-22
    if mode:
        d = latest()
        use = {"cache": "cards use the last good OddsPapi pull (%s, under %dh old)" % (
                   (d or {}).get("pulled_at", "?")[:16].replace("T", " "), BACKUP_CACHE_MAX_H),
               "bet365": "cards use bet365 lines from SportsAPI365",
               "none": "no backup lines either -- Total Games cards show bet365's 📏 line only where it exists"}[mode]
        return False, "%s -- %s (%s)" % (BACKUP_TEXT, use, why)
    now = _ct_now()
    st = _load(_day_file(now.date().isoformat()), {})
    if st.get("ok"):
        return True, "pulled %s (%d matches)" % (st["pulled_at"][11:16], len(st.get("matches") or []))
    if latest():
        if st.get("error"):
            return False, "today's pull failed (%s); cards use yesterday's Pinnacle prices" % st["error"]
        return True, "today's pull is due after %d:00 CT; cards use the last pull" % PULL_AFTER_CT
    if st.get("error"):
        return False, "pull failed (%s) -- cards say \"Pinnacle: no line today\"" % st["error"]
    return False, "no Pinnacle pull in the last %dh -- cards say \"Pinnacle: no line today\"" % MAX_AGE_H


def _names(p):
    if p.get("player") == "match":
        return [x.strip() for x in str(p.get("opponent") or "").replace(" vs ", " v ").split(" v ") if x.strip()]
    return [x for x in (p.get("player"), p.get("opponent")) if x]


def lookup(p):
    """(match dict, our player is p1) or (None, None). Never raises."""
    try:
        d = latest()
        if not d:
            return None, None
        import apex_price_feed as pf
        nm = _names(p)
        if len(nm) < 2:
            return None, None
        for m in d.get("matches") or []:
            if pf.match_pair(nm[0], nm[1], m["p1"], m["p2"]):
                return m, bool(pf._is(nm[0], m["p1"])) if hasattr(pf, "_is") else True
    except Exception:
        pass
    return None, None


def _ladder_prob(ladder, main, line, more):
    """Pinnacle's chance for our side of `line`: the exact ladder rung when Pinnacle
    prices it, else a normal spread around the main line."""
    if not ladder or main is None:
        return None
    k = str(float(line))
    if k in ladder:
        po = ladder[k]
    else:
        op = min(max(ladder.get(str(float(main)), 0.5), 0.01), 0.99)
        mu = float(main) + BOOK_SD * NormalDist().inv_cdf(op)
        po = 1 - NormalDist(mu, BOOK_SD).cdf(float(line))
    return po if more else 1 - po


def room(p):
    """Total Games: (pinnacle expects, line, games of room for our side) or None."""
    try:
        if p.get("category") != "total_games":
            return None
        m, _ = lookup(p)
        if not m or m.get("tg_main") is None:
            return None
        exp, line = float(m["tg_main"]), float(p.get("line"))
        more = str(p.get("direction") or "").upper() == "MORE"
        return exp, line, (exp - line) if more else (line - exp)
    except Exception:
        return None


def side_prob(p):
    """Pinnacle no-vig chance for our side of the PrizePicks line (Total Games / Games Won), or None."""
    try:
        m, p1 = lookup(p)
        if not m:
            return None
        more = str(p.get("direction") or "").upper() == "MORE"
        cat = p.get("category")
        if cat == "total_games":
            return _ladder_prob(m.get("tg"), m.get("tg_main"), p.get("line"), more)
        if cat == "games_won" and p.get("player") != "match":
            k = "gw1" if p1 else "gw2"
            return _ladder_prob(m.get(k), m.get(k + "_main"), p.get("line"), more)
    except Exception:
        pass
    return None


def card_line(p):
    """🎯 Pinnacle cushion line for Total Games cards. Never raises; '' for other markets."""
    try:
        if p.get("category") != "total_games":
            return ""
        if not latest():
            why, mode = backup_mode()   # APEX-ODDSPAPI-BACKUP-2026-10-22
            if mode == "bet365":
                try:
                    nm = _names(p)
                    import apex_bet365_totals as b3   # APEX-BET365-THREEPART-2026-10-22: names + date
                    bk = b3.total(nm[:2], str(p.get("match_date") or "")[:10])[0]
                    if bk and len(nm) >= 2:
                        exp, line = float(bk), float(p.get("line"))
                        gap = (exp - line) if str(p.get("direction") or "").upper() == "MORE" else (line - exp)
                        return "\U0001F3AF Pinnacle unavailable (OddsPapi) -- backup: bet365 expects %g, line %g -- %g game%s of room %s" % (
                            exp, line, gap, "" if gap == 1 else "s", "✅" if gap >= ROOM_OK else ("⚠️" if gap > 0 else "❌"))
                except Exception:
                    pass
            if mode:
                return "\U0001F3AF Pinnacle unavailable (OddsPapi) -- no backup line for this match"
            return "\U0001F3AF Pinnacle: no line today"
        r = room(p)
        if not r:
            return "\U0001F3AF Pinnacle: no line for this match"
        exp, line, gap = r
        if gap >= ROOM_OK:
            return "\U0001F3AF Pinnacle cushion: Pinnacle expects %g, line %g -- %g games of room ✅" % (exp, line, gap)
        if gap > 0:
            return "\U0001F3AF Pinnacle cushion: Pinnacle expects %g, line %g -- only %g game%s of room ⚠️" % (
                exp, line, gap, "" if gap == 1 else "s")
        return "\U0001F3AF Pinnacle cushion: Pinnacle expects %g, line %g -- no room, Pinnacle is on the other side ❌" % (exp, line)
    except Exception:
        return ""


def books_line(p, apex_prob):
    """APEX-BOOKSSAY-2026-10-22: '📗 Books say ~71% (Apex 75%)' from bet365 and/or Pinnacle
    for this exact line. Never raises."""
    try:
        vals, who = [], []
        bp = p.get("apex_book_prob")
        if bp not in (None, "") and p.get("category") == "total_games":
            vals.append(float(bp)); who.append("bet365")
        pp = side_prob(p)
        if pp is not None:
            vals.append(pp); who.append("Pinnacle")
        if not vals:
            return "\U0001F4D7 Books: no book prices this exact line (Apex %d%%)" % round(100 * apex_prob)
        b = sum(vals) / len(vals)
        gap = round(100 * apex_prob) - round(100 * b)
        tail = (" -- Apex is %d points higher than the books" % gap) if gap >= 5 else \
               (" -- the books like it even more" if gap <= -3 else " -- close to the books")
        return "\U0001F4D7 Books say ~%d%% (Apex %d%%)%s _(%s)_" % (round(100 * b), round(100 * apex_prob), tail, " + ".join(who))
    except Exception:
        return ""


# ------------------------------------------------------------------ paper test
def log_today(picks=None, source="", today=None):
    """PINNACLE CUSHION paper test: every playable Total Games prop (first sighting) with its
    Pinnacle room. Never raises."""
    try:
        import apex_edge_tracker as t
        today = today or datetime.now().date().isoformat()
        if picks is None:
            picks, _ = t.load_current_board()
        data = _load(TEST_LOG, {})
        day = data.setdefault(today, {})
        added = 0
        for p in picks:
            if p.get("category") != "total_games" or t._is_unplayable(p) or t._pub_match_started(p):
                continue
            r = room(p)
            if not r:
                continue
            k = "|".join(str(x) for x in t._apex_prop_identity(p))
            if k in day:
                continue
            day[k] = {"match_date": p.get("match_date"), "player": p.get("player"), "opponent": p.get("opponent"),
                      "category": "total_games", "line": p.get("line"), "direction": p.get("direction"),
                      "prop_type": str(p.get("prop_type") or "standard").lower(), "prob": round(t._board_prob(p), 4),
                      "pinnacle_expects": r[0], "room": round(r[2], 2), "pinnacle_prob": side_prob(p),
                      "logged_at": datetime.now().isoformat(timespec="seconds"), "source": source}
            added += 1
        _save(TEST_LOG, data)
        return added
    except Exception as e:
        print("[PinnacleTest] log skipped (%s): %s" % (type(e).__name__, e))
        return False


def _bucket(lg):
    r = float(lg.get("room") or 0)
    return "3+ games of room" if r >= ROOM_OK else ("0-3 games of room" if r > 0 else "no room")


def _tally():
    data = _load(TEST_LOG, {})
    if not data:
        return None
    import apex_engine_validation as ev
    _t, _rep, S, _st = ev.load()
    T = {"all": {}, "70": {}}
    n = 0
    for day, props in data.items():
        for lg in props.values():
            m = S.find(lg.get("match_date"), lg.get("player"), lg.get("opponent"), "total_games", lg.get("line"), lg.get("direction"))
            h = None if m is None else m.get("hit")
            if h is None:
                continue
            n += 1
            for g in ("all", "70") if float(lg.get("prob") or 0) >= 0.70 else ("all",):
                r = T[g].setdefault(_bucket(lg), [0, 0])
                r[0 if h else 1] += 1
    return T, n, len(data)


def summary():
    try:
        x = _tally()
        if x is None:
            return []
        T, n, days = x
        if not n:
            return ["Pinnacle cushion test (since 10/22): %d day(s) logged, nothing graded yet" % days]

        def fmt(g):
            return " · ".join("%s %d-%d (%.0f%%)" % (b, w, l, 100.0 * w / (w + l)) for b, (w, l) in sorted(g.items())) or "--"
        return ["Pinnacle cushion test: %d / %d graded Total Games props (not used on boards)" % (n, FINISH_GRADED),
                "All: " + fmt(T["all"]), "70%+ only: " + fmt(T["70"])]
    except Exception as e:
        return ["Pinnacle cushion test unavailable (%s)" % type(e).__name__]


def finish_check():
    x = _tally()
    if not x:
        return False, None
    T, n, days = x
    if n < FINISH_GRADED:
        return False, None

    def fmt(g):
        return " · ".join("%s %d-%d (%.0f%%)" % (b, w, l, 100.0 * w / (w + l)) for b, (w, l) in sorted(g.items())) or "--"
    good, rest = T["all"].get("3+ games of room", [0, 0]), [sum(v[i] for b, v in T["all"].items() if b != "3+ games of room") for i in (0, 1)]
    pg = good[0] / float(sum(good)) if sum(good) else None
    pr = rest[0] / float(sum(rest)) if sum(rest) else None
    held = pg is not None and pr is not None and pg - pr >= 0.05 and min(sum(good), sum(rest)) >= 30
    return True, [("Test", "PINNACLE CUSHION -- Total Games props by Pinnacle's games of room (%d days)" % days),
                  ("All props", fmt(T["all"])), ("70%+ props", fmt(T["70"])),
                  ("Verdict", "HELD UP -- 3+ games of room wins 5+ points more" if held else "no real difference / too few"),
                  ("Decision", "use the Pinnacle cushion on boards / keep it display only / keep testing")]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "pull":
        print(daily() if "--force" not in sys.argv else pull().get("error") or "ok")
    elif cmd == "test":
        print("\n".join(summary()) or "nothing logged yet")
    print("requests used this month: %d of %d" % (month_used(), MONTH_LIMIT))
    print("data check:", today_status())
