#!/usr/bin/env python3
"""
APEX-SURFACEMAP-2026-10-10 (owner, approved 2026-10-11: "Focus on fixing
data quality first: Surface mapping ..."). Tournament -> court surface.

Why: both prop fetch paths (PrizePicks partner API and the Playwright fallback)
hard-wired surface="clay", and the engine simulated every prop on clay. The
existing research_surface() was never called and its table knew only 30
events (no Beijing, Wuhan, Suzhou, Samsun, Chengdu, ...). Re-simulating 4,139
settled props (Oct 1-10) on the correct surface improved accuracy (Brier
0.2332 -> 0.2289); 70%+ props 67.6% -> 69.5%; Breaks 51.9% -> 57.5%.

Lookup order:
  1. the ATP/WTA/Challenger match history on disk (most recent year wins) --
     ~530 tournament names with surface and, where recorded, indoor/outdoor
  2. openclaw_core._SURFACE_HINTS (the old 30-entry table, read from source)
  3. "hard" -- and the tournament is recorded in
     data/logs/unmapped_tournaments.json so the daily report flags it.
Indoor is returned for reference only; the engine does not use it yet (the
indoor adjustment is a separate Test First item).

  python3 apex_surface_map.py "China Open - Beijing"   # one lookup
  python3 apex_surface_map.py --rebuild                # rebuild the cache
"""
import ast, csv, glob, json, os, re, sys, unicodedata
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
HIST_DIR = os.path.join(HERE, "ATP & WTA, 1968-2026")
CACHE = os.path.join(HERE, "data", "surface_map.json")
UNMAPPED = os.path.join(HERE, "data", "logs", "unmapped_tournaments.json")
CACHE_MAX_AGE_DAYS = 7
MIN_YEAR = 2023
_STOP = {"open", "tennis", "championships", "championship", "masters", "rolex", "wta", "atp",
         "challenger", "cup", "international", "internationals", "the", "de", "di", "of", "presented",
         "by", "trophy", "classic", "ladies", "mens", "womens", "tour", "series", "250", "500",
         "1000", "125", "itf", "qualifying", "quali", "q", "men", "women", "singles"}
_MAP = None


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def _tokens(s):
    return [t for t in _norm(s).split() if t not in _STOP and len(t) > 2]


def build():
    """name(normalised) -> {surface, indoor, year}; most recent year wins."""
    m = {}
    files = []
    for f in glob.glob(os.path.join(HIST_DIR, "*.csv")):
        b = os.path.basename(f)
        if b[:4].isdigit() and int(b[:4]) >= MIN_YEAR:
            files.append((int(b[:4]), f))
    for f in glob.glob(os.path.join(HERE, "[0-9][0-9][0-9][0-9].csv")):
        b = os.path.basename(f)
        if int(b[:4]) >= MIN_YEAR:
            files.append((int(b[:4]), f))
    for year, f in sorted(files):
        try:
            with open(f, newline="", encoding="utf-8", errors="ignore") as fh:
                for r in csv.DictReader(fh):
                    name, surf = _norm(r.get("tourney_name")), str(r.get("surface") or "").strip().lower()
                    if not name or surf not in ("hard", "clay", "grass", "carpet"):
                        continue
                    ind = {"I": True, "O": False, "0": False}.get(str(r.get("indoor") or "").strip())
                    old = m.get(name)
                    td = str(r.get("tourney_date") or "")
                    mon = int(td[4:6]) if len(td) >= 6 and td[4:6].isdigit() else None
                    if old is None or year >= old["year"]:
                        m[name] = {"surface": "hard" if surf == "carpet" else surf,
                                   "indoor": ind if ind is not None else (old or {}).get("indoor"),
                                   "year": year, "month": mon if mon else (old or {}).get("month")}
        except Exception as e:
            print("[surface] skipped %s: %s" % (f, e))
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE + ".tmp", "w") as fh:
        json.dump({"built": datetime.now().isoformat(timespec="seconds"), "names": m}, fh)
    os.replace(CACHE + ".tmp", CACHE)
    return m


def _load():
    global _MAP
    if _MAP is not None:
        return _MAP
    try:
        age = (datetime.now().timestamp() - os.path.getmtime(CACHE)) / 86400.0
        if age <= CACHE_MAX_AGE_DAYS:
            with open(CACHE) as fh:
                _MAP = json.load(fh)["names"]
                return _MAP
    except Exception:
        pass
    _MAP = build()
    return _MAP


_HINTS = None

def _hints():
    global _HINTS
    if _HINTS is None:
        try:
            src = open(os.path.join(HERE, "openclaw_core.py"), encoding="utf-8").read()
            tree = ast.parse(src)
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "_SURFACE_HINTS" for t in node.targets):
                    _HINTS = ast.literal_eval(node.value)
                    break
        except Exception:
            pass
        _HINTS = _HINTS or {}
    return _HINTS


# APEX-LYONSURFACE-2026-10-22 (owner, "Go"): names the history map gets wrong
# because an older event shares the city. Normalised full tournament name -> (surface,
# indoor). Checked before anything else. Add a line when the surface check flags one.
OVERRIDES = {
    "grand prix auvergne rhone alpes lyon": ("Hard", True),   # Oct ATP Lyon, indoor hard (the May Lyon event is clay)
    # APEX-SURFACE-OVERRIDES-2026-10-27 (owner, "fix this now"): two WTA 125 events the
    # engine had no history for and was scoring on its default Hard.
    "valle d aosta ladies open courmayeur": ("Hard", True),   # Courmayeur, indoor hard (wtatennis.com / Wikipedia)
    "queretaro open queretaro": ("Clay", False),             # Club Campestre de Queretaro, OUTDOOR CLAY (wtatennis.com)
}



# APEX-SURFACE-AUTOLOOKUP-2026-10-27 (owner, "Go" after the 10/26 SURFACE warning): a
# tournament nothing on disk knows is looked up ONCE on the tour calendars, then Wikipedia (the tournament page's
# infobox "Surface" line), saved in data/surface_auto.json, and announced with one #alerts
# line. A failed lookup is retried every 6 hours; meanwhile apex_edge_tracker holds ALL of
# that tournament's props off the boards (SURFACE_HOLD_ALL_UNKNOWN). /surface in Discord
# writes the same file by hand. AUTO_LOOKUP = False turns the web lookup off (file still read).
AUTO_LOOKUP = True
AUTO_FILE = os.path.join(HERE, "data", "surface_auto.json")
AUTO_RETRY_HOURS = 6
WIKI_API = "https://en.wikipedia.org/w/api.php"
_UA = "ApexTennis/1.0 (court surface lookup; local OpenClaw bot)"
_AUTO = {"mtime": None, "d": {}}


def _auto_load():
    try:
        mt = os.path.getmtime(AUTO_FILE) if os.path.exists(AUTO_FILE) else None
        if _AUTO["mtime"] != mt:
            _AUTO["d"] = json.load(open(AUTO_FILE)) if mt else {}
            _AUTO["mtime"] = mt
    except Exception:
        _AUTO["d"] = _AUTO.get("d") or {}
    return _AUTO["d"]


def _auto_save(d):
    try:
        os.makedirs(os.path.dirname(AUTO_FILE), exist_ok=True)
        with open(AUTO_FILE + ".tmp", "w") as fh:
            json.dump(d, fh, indent=1, sort_keys=True)
        os.replace(AUTO_FILE + ".tmp", AUTO_FILE)
        _AUTO["mtime"] = None
    except Exception:
        pass


AUTO_RETRY_TRANSIENT_MIN = 15   # Wikipedia busy / no internet: try again this soon, no alert yet
_LAST_WIKI = [0.0]


class _Transient(Exception):
    """Wikipedia did not answer properly (429 / timeout / no internet) -- not a 'not found'."""


def _wiki(params, timeout=12):
    import time, urllib.parse, urllib.request, urllib.error
    wait = 1.2 - (time.time() - _LAST_WIKI[0])   # be polite: at most ~1 call per second
    if wait > 0:
        time.sleep(wait)
    _LAST_WIKI[0] = time.time()
    params = dict(params, format="json")
    req = urllib.request.Request(WIKI_API + "?" + urllib.parse.urlencode(params), headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code in (429, 500, 502, 503, 504):
            raise _Transient("HTTP %s" % e.code)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise _Transient(str(e))


def _infobox_surface(wikitext):
    m = re.search(r"\|\s*surface\s*=\s*([^\n]*)", wikitext, re.I)
    if not m:
        return None
    raw = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", m.group(1).lower())   # [[Hardcourt|Hard]] -> hard
    raw = re.sub(r"<[^>]+>|\{\{[^}]*\}\}", " ", raw).split("|")[0]
    names = {"grass": "Grass", "clay": "Clay", "hard": "Hard", "carpet": "Hard"}
    found = [(mm.start(), names[mm.group(0)]) for mm in re.finditer(r"grass|clay|hard|carpet", raw)]
    if not found:
        return None
    surf = found[-1][1]   # a page that lists old courts writes them in order; the LAST one is today's
    for pos, v in found:  # ... unless one is marked as the current one
        tail = raw[pos:pos + 40]
        if "present" in tail or "current" in tail or str(datetime.now().year) in tail:
            surf = v
            break
    indoor = True if "indoor" in raw else (False if "outdoor" in raw else None)
    if indoor is None:
        t = re.search(r"\|\s*type\s*=\s*([^\n|]*)", wikitext, re.I)
        if t and "indoor" in t.group(1).lower():
            indoor = True
    return surf, indoor, " ".join(raw.split())


# APEX-SURFACE-TOURPAGES-2026-10-30 (owner, "Go"): the official tour calendars first.
# WTA: api.wtatennis.com (surface "Hard"/"Clay"/"Grass", inOutdoor "O"/"I"); ATP: atptour.com
# calendar JSON (Surface, IndoorOutdoor "Indoor"/"Outdoor"). Both lists are cached on disk for
# TOUR_CACHE_HOURS so a lookup costs one request per tour per day at most. Wikipedia is the
# fallback when neither tour lists the event (ITF / small events).
TOUR_WTA_API = "https://api.wtatennis.com/tennis/tournaments/?page=0&pageSize=100&from=%s&to=%s"
TOUR_ATP_API = "https://www.atptour.com/en/-/tournaments/calendar/tour"
TOUR_CACHE = os.path.join(HERE, "data", "tour_calendar_cache.json")
TOUR_CACHE_HOURS = 24
TOUR_FAIL_RETRY_MIN = 60   # a tour site that refused us is tried again after an hour, not on every lookup
TOUR_STOP = {"open", "tennis", "championships", "championship", "tournament", "international", "the", "de",
             "atp", "wta", "cup", "masters", "250", "500", "1000", "125", "challenger", "ladies", "women", "womens", "men", "mens"}


def _http_json(url, timeout=15):
    """GET -> parsed JSON. Raises _Transient on a network / server problem."""
    import urllib.request, urllib.error
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) " + _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code in (429, 500, 502, 503, 504):
            raise _Transient("HTTP %s" % e.code)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise _Transient(str(e))


def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            for x in _walk_dicts(v):
                yield x
    elif isinstance(obj, list):
        for v in obj:
            for x in _walk_dicts(v):
                yield x


def _tour_events_wta(body):
    out = []
    for e in _walk_dicts(body):
        if "surface" in e and "title" in e and isinstance(e.get("surface"), str):
            io = str(e.get("inOutdoor") or "").upper()[:1]
            out.append({"tour": "wtatennis.com", "name": str(e.get("title") or ""),
                        "city": str(e.get("city") or ""), "surface": e["surface"],
                        "indoor": True if io == "I" else (False if io == "O" else None),
                        "start": str(e.get("startDate") or ""), "end": str(e.get("endDate") or "")})
    return out


def _tour_events_atp(body):
    out = []
    for e in _walk_dicts(body):
        if "Surface" in e and "Name" in e and isinstance(e.get("Surface"), str):
            io = str(e.get("IndoorOutdoor") or "").lower()
            out.append({"tour": "atptour.com", "name": str(e.get("Name") or ""),
                        "city": str(e.get("Location") or ""), "surface": e["Surface"],
                        "indoor": True if io.startswith("in") else (False if io.startswith("out") else None),
                        "start": "", "end": "", "when": str(e.get("FormattedDate") or "")})
    return out


def _tour_lists(force=False):
    """[(events...)] from the disk cache or fresh pulls. Raises _Transient only when BOTH tours fail."""
    now = datetime.now()
    cache = {}
    try:
        if os.path.exists(TOUR_CACHE):
            cache = json.load(open(TOUR_CACHE))
    except Exception:
        cache = {}
    events, errs, changed = [], [], False
    for tour, url in (("wta", TOUR_WTA_API % ((now - timedelta(days=30)).strftime("%Y-%m-%d"),
                                              (now + timedelta(days=45)).strftime("%Y-%m-%d"))),
                      ("atp", TOUR_ATP_API)):
        c = cache.get(tour) or {}
        try:
            if not force and c.get("failed") and (now - datetime.fromisoformat(c["failed"])).total_seconds() < TOUR_FAIL_RETRY_MIN * 60:
                events.extend(c.get("events") or [])
                errs.append("%s: skipped, failed %s" % (tour, c["failed"][11:16]))
                continue
        except Exception:
            pass
        fresh = False
        try:
            fresh = bool(c.get("ts")) and (now - datetime.fromisoformat(c["ts"])).total_seconds() < TOUR_CACHE_HOURS * 3600
        except Exception:
            fresh = False
        if fresh and not force:
            events.extend(c.get("events") or [])
            continue
        try:
            body = _http_json(url)
            ev = _tour_events_wta(body) if tour == "wta" else _tour_events_atp(body)
            if not ev:
                raise _Transient("%s calendar came back empty" % tour)
            cache[tour] = {"ts": now.isoformat(timespec="seconds"), "events": ev}
            events.extend(ev)
            changed = True
        except Exception as e:   # keep the old copy (even stale) when the site is down
            cache[tour] = dict(c, failed=now.isoformat(timespec="seconds"))
            changed = True
            errs.append("%s: %s" % (tour, e))
            events.extend(c.get("events") or [])
    if changed:
        try:
            os.makedirs(os.path.dirname(TOUR_CACHE), exist_ok=True)
            with open(TOUR_CACHE + ".tmp", "w") as fh:
                json.dump(cache, fh)
            os.replace(TOUR_CACHE + ".tmp", TOUR_CACHE)
        except Exception:
            pass
    if not events and errs:
        raise _Transient("; ".join(errs))
    return events


def tour_lookup(tournament, debug=False):
    """Official tour calendars -> (surface, indoor, 'wtatennis.com: <event> (hard / outdoor)') or None.
    Needs the event's city (or two name words) to match; raises _Transient when both tours are down."""
    parts = [p.strip() for p in re.split(r"\s+-\s+|,", str(tournament)) if p.strip()]
    name_t = set(_tokens(parts[0])) - TOUR_STOP if parts else set()
    city_t = set(_tokens(parts[-1])) - TOUR_STOP if len(parts) > 1 else set()
    all_t = set(_tokens(tournament)) - TOUR_STOP
    if not all_t or re.search(r"\bchallenger\b|\b[wm]\d{2,3}\b|\bitf\b", str(tournament), re.I):
        return None   # Challengers / ITF are not on these calendars; a same-city ATP event must not answer for them
    names = {"grass": "Grass", "clay": "Clay", "hard": "Hard", "carpet": "Hard"}
    best, best_sc = None, 0
    for e in _tour_lists():
        surf = names.get(str(e.get("surface") or "").strip().lower())
        if not surf:
            continue
        et = set(_tokens(e["name"])) - TOUR_STOP
        ec = set(_tokens(e.get("city") or "")) - TOUR_STOP
        sc = 2 * len(all_t & et) + (3 if (city_t and city_t & (ec | et)) else 0) + (1 if name_t & et else 0)
        if (city_t and not (city_t & (ec | et))) and len(all_t & et) < 2:
            continue   # the city must match unless two real name words do
        if sc > best_sc:
            best, best_sc = e, sc
    if debug:
        print("  tour best:", best_sc, best)
    if not best or best_sc < 5:   # city + one real name word (a Challenger in the same city must not borrow the ATP event)
        return None
    where = {True: "indoor", False: "outdoor"}.get(best.get("indoor"), "")
    return names[best["surface"].strip().lower()], best.get("indoor"), "%s: %s (%s%s)" % (
        best["tour"], best["name"], best["surface"].lower(), " / " + where if where else "")


def web_lookup(tournament, debug=False):
    """Tour calendars first (tour_lookup), Wikipedia second. -> (surface, indoor, source) or None."""
    try:
        got = tour_lookup(tournament, debug=debug)
        if got:
            return got
    except _Transient as te:
        print("[surface] tour calendars unreachable (%s), trying Wikipedia" % te)
    except Exception as e:
        print("[surface] tour lookup failed for %s: %s" % (tournament, e))
    return wiki_lookup(tournament, debug=debug)


def wiki_lookup(tournament, debug=False):
    """Wikipedia tournament page -> (surface, indoor, 'wikipedia: <title> (<infobox text>)') or None
    (no page found). Raises _Transient when Wikipedia is busy / unreachable. The page must be
    about tennis and mention the city / the event's own words."""
    try:
        want = set(_tokens(tournament))
        parts = [p.strip() for p in re.split(r"\s+-\s+|,", str(tournament)) if p.strip()]
        city = _tokens(parts[-1]) if len(parts) > 1 else []
        year = datetime.now().year
        queries = [" ".join(dict.fromkeys(_tokens(tournament))) + " tennis tournament",
                   "%s tennis" % " ".join(parts), ]
        if city:
            queries.append("%s tennis tournament" % " ".join(city))
        hits, q = [], ""
        for q in queries:
            hits = _wiki({"action": "query", "list": "search", "srsearch": q, "srlimit": 6}).get("query", {}).get("search", [])
            if any(set(_tokens(h.get("title", ""))) & (want | set(city)) for h in hits):
                break
        cands = []
        for h in hits:
            title = h.get("title", "")
            tt = set(_tokens(title))
            score = len(want & tt) * 2 + (2 if city and set(city) & tt else 0)
            score += 1 if str(year) in title else (0.5 if str(year - 1) in title else 0)
            if score > 0:
                cands.append((score, title))
        if debug:
            print("  search:", q, "->", [h.get("title") for h in hits], "cands:", sorted(cands, reverse=True))
        for _sc, title in sorted(cands, reverse=True)[:3]:
            page = _wiki({"action": "parse", "page": title, "prop": "wikitext", "section": 0, "redirects": 1})
            wt = page.get("parse", {}).get("wikitext", {}).get("*", "")
            body = _norm(wt[:4000])
            got = _infobox_surface(wt)
            if debug:
                print("  page %r: tennis=%s city_ok=%s words_ok=%s infobox=%s" % (
                    title, "tennis" in wt.lower(), (not city) or any(c in body or c in _norm(title) for c in city),
                    bool(want & set(_tokens(title)) or any(w in body for w in want)), got))
            if "tennis" not in wt.lower():
                continue
            if city and not any(c in body or c in _norm(title) for c in city):
                continue
            if not (want & set(_tokens(title)) or any(w in body for w in want)):
                continue
            if got:
                return got[0], got[1], "wikipedia: %s (%s)" % (title, got[2][:40])
        return None
    except _Transient:
        raise
    except Exception as e:
        print("[surface] web lookup failed for %s: %s" % (tournament, e))
        return None


def _alert(text):
    try:
        import apex_ops
        apex_ops.send_discord("alerts", text)
    except Exception as e:
        print("[surface] alert skipped: %s" % e)


def auto_lookup(tournament):
    """-> (surface, indoor, source) from the auto file / a fresh web lookup, or None. Posts one
    #alerts line the first time a tournament is confirmed, and one when it cannot be. Never raises."""
    try:
        key = " ".join(_norm(tournament).split())
        if not key:
            return None
        d = _auto_load()
        e = d.get(key)
        now = datetime.now()
        if e and e.get("surface"):
            return e["surface"], e.get("indoor"), "override-auto (%s)" % (e.get("source") or "saved")
        if e and e.get("tried"):
            try:
                _wait = AUTO_RETRY_TRANSIENT_MIN * 60 if e.get("transient") else AUTO_RETRY_HOURS * 3600
                if (now - datetime.fromisoformat(e["tried"])).total_seconds() < _wait:
                    return None
            except Exception:
                pass
        if not AUTO_LOOKUP:
            return None
        try:
            got = web_lookup(tournament)
        except _Transient as te:   # Wikipedia busy / offline: short retry, alert only after 3 tries in a row
            n = int((e or {}).get("transient") or 0) + 1
            print("[surface] web lookup for %s postponed (%s), try %d" % (tournament, te, n))
            d[key] = {"name": str(tournament), "surface": None, "tried": now.isoformat(timespec="seconds"),
                      "transient": n, "alerted": (e or {}).get("alerted", False)}
            if n >= 3 and not d[key]["alerted"]:
                d[key]["alerted"] = True
                _alert("\u26A0\uFE0F **NEW TOURNAMENT -- court unknown:** %s. The court lookup could not reach "
                       "the tour sites or Wikipedia (%s), so ALL of its props stay off the boards until it is confirmed (it keeps "
                       "retrying). To set it yourself, type /surface in Discord." % (tournament, te))
            _auto_save(d)
            return None
        if got:
            d[key] = {"name": str(tournament), "surface": got[0], "indoor": got[1], "source": got[2],
                      "ts": now.isoformat(timespec="seconds")}
            _auto_save(d)
            where = {True: " (indoor)", False: " (outdoor)"}.get(got[1], "")
            _alert("\U0001F3BE **NEW TOURNAMENT -- court confirmed:** %s plays on **%s**%s. Found on %s. "
                   "Its props are scored on %s from now on." % (tournament, got[0].upper(), where,
                                                                got[2].split(" (")[0], got[0].lower()))
            return got[0], got[1], "override-auto (%s)" % got[2]
        first = not (e or {}).get("alerted")
        d[key] = {"name": str(tournament), "surface": None, "tried": now.isoformat(timespec="seconds"),
                  "alerted": True}
        _auto_save(d)
        if first:
            _alert("⚠️ **NEW TOURNAMENT -- court unknown:** %s. Apex could not confirm its court, so ALL "
                   "of its props stay off the boards until it is confirmed (it retries every %d hours). "
                   "To set it yourself, type /surface in Discord." % (tournament, AUTO_RETRY_HOURS))
        return None
    except Exception as e:
        print("[surface] auto lookup skipped: %s" % e)
        return None


def set_manual(tournament, surface, indoor=None, who="Discord /surface"):
    """Save a court by hand (the /surface command). Returns the plain confirmation line."""
    surf = str(surface or "").strip().title()
    if surf not in ("Hard", "Clay", "Grass"):
        return "Court must be Hard, Clay or Grass (got '%s')." % surface
    key = " ".join(_norm(tournament).split())
    if not key:
        return "I need the tournament name exactly as the alert showed it."
    d = _auto_load()
    d[key] = {"name": str(tournament), "surface": surf, "indoor": indoor, "source": "manual (%s)" % who,
              "ts": datetime.now().isoformat(timespec="seconds")}
    _auto_save(d)
    where = {True: " (indoor)", False: " (outdoor)"}.get(indoor, "")
    return ("✅ **Court saved:** %s = **%s**%s. Its props come back on the boards at the next re-score "
            "(/fetch or the next odds update)." % (tournament, surf.upper(), where))


def unmapped_now():
    """Tournaments scored in the last 3 days whose court is still unknown (for /surface)."""
    out = []
    try:
        from datetime import date, timedelta
        since = (date.today() - timedelta(days=3)).isoformat()
        seen = set()
        with open(PICK_LOG, newline="") as fh:
            for r in csv.DictReader(fh):
                t = r.get("tournament") or ""
                if t and (r.get("match_date") or "") >= since and t not in seen:
                    seen.add(t)
                    if surface_status(t) == "unknown":
                        out.append(t)
    except Exception:
        pass
    return out


def lookup(tournament):
    """-> (surface 'Hard'|'Clay'|'Grass', indoor True/False/None, source)."""
    t = _norm(tournament)
    if not t or t == "tennis":
        return "Hard", None, "default (no tournament name)"
    _tt = " ".join(t.split())
    if _tt in OVERRIDES:
        return OVERRIDES[_tt][0], OVERRIDES[_tt][1], "override"
    _ae = _auto_load().get(_tt)   # APEX-SURFACE-AUTOLOOKUP-2026-10-27: saved by /surface or an earlier web lookup
    if _ae and _ae.get("surface"):
        return _ae["surface"], _ae.get("indoor"), "override-auto (%s)" % (_ae.get("source") or "saved")
    # APEX-SURFACESOURCE-2026-10-22 (owner, "Go"): the real court of this
    # tournament's own finished matches (stats warehouse, fed by SportsAPI365's
    # tournament.court) beats any guess from the name.
    wh = warehouse_surface(tournament)
    if wh:
        return wh[0], wh[1], "warehouse (finished matches here)"
    m = _load()
    _mon = datetime.now().month

    def _season_ok(e):
        """History only counts if that event was played within a month of now --
        a city can host a clay event in May and an indoor hard one in October."""
        mo = e.get("month")
        if not mo:
            return True
        d = abs(mo - _mon) % 12
        return min(d, 12 - d) <= 1
    if t in m and _season_ok(m[t]):
        e = m[t]; return e["surface"].title(), e.get("indoor"), "history exact"
    # PrizePicks/SportsAPI365 names look like "China Open - Beijing": try the
    # city part and the name part, then token overlap against history names.
    parts = [p.strip() for p in re.split(r"\s+-\s+|,", str(tournament)) if p.strip()]
    for p in reversed(parts):                      # city first
        n = _norm(p)
        if n in m and _season_ok(m[n]):
            e = m[n]; return e["surface"].title(), e.get("indoor"), "history (%s)" % n
    want = set(_tokens(tournament))
    best, best_score = None, 0
    for name, e in m.items():
        nt = set(_tokens(name))
        if not nt or not _season_ok(e):
            continue
        score = len(want & nt)
        if score > best_score or (score == best_score and best and e["year"] > m[best]["year"]):
            best, best_score = name, score
    if best and best_score >= 1:
        e = m[best]; return e["surface"].title(), e.get("indoor"), "history (%s)" % best
    for name, surf in _hints().items():
        if name in t:
            return str(surf).title(), None, "core table (%s)" % name
    _al = auto_lookup(tournament)   # APEX-SURFACE-AUTOLOOKUP-2026-10-27
    if _al:
        return _al
    _record_unmapped(tournament)
    return "Hard", None, "default hard (UNMAPPED -- surface UNKNOWN)"


_WH_CACHE = {"ts": 0, "map": {}}


def warehouse_surface(tournament, days=21):
    """(Surface, indoor) recorded for this exact tournament's finished matches in the
    last `days` days, or None. Cached 30 min. Never raises."""
    import sqlite3, time
    try:
        if time.time() - _WH_CACHE["ts"] > 1800:
            from datetime import date, timedelta
            mp = {}
            con = sqlite3.connect("file:%s?mode=ro" % os.path.join(HERE, "data", "warehouse", "tennis_warehouse.sqlite"),
                                  uri=True, timeout=10)
            for t, sf, n in con.execute("select tournament, surface, count(*) from matches where match_date >= ? "
                                        "and surface is not null group by 1, 2",
                                        ((date.today() - timedelta(days=days)).isoformat(),)):
                raw = str(sf or "").strip().lower()
                ind = raw.startswith("i.")
                base = raw.replace("i.", "")
                base = "hard" if base in ("hard", "carpet") else base
                if base in ("hard", "clay", "grass") and n > mp.get(t, (None, None, 0))[2]:
                    mp[t] = (base.title(), ind, n)
            con.close()
            _WH_CACHE.update(ts=time.time(), map=mp)
        e = _WH_CACHE["map"].get(str(tournament or ""))
        return (e[0], e[1]) if e else None
    except Exception:
        return None


def surface_status(tournament):
    """'confirmed' (finished matches here or override), 'guess' (same-season history /
    old table), or 'unknown' (default). Never raises."""
    try:
        src = lookup(tournament)[2]
    except Exception:
        return "unknown"
    if src.startswith(("warehouse", "override")):
        return "confirmed"
    if "UNKNOWN" in src or src.startswith("default"):
        return "unknown"
    return "guess"


def _record_unmapped(tournament):
    try:
        d = {}
        if os.path.exists(UNMAPPED):
            with open(UNMAPPED) as fh:
                d = json.load(fh)
        k = str(tournament)
        d[k] = {"first_seen": d.get(k, {}).get("first_seen") or datetime.now().isoformat(timespec="seconds"),
                "last_seen": datetime.now().isoformat(timespec="seconds")}
        os.makedirs(os.path.dirname(UNMAPPED), exist_ok=True)
        with open(UNMAPPED + ".tmp", "w") as fh:
            json.dump(d, fh, indent=1)
        os.replace(UNMAPPED + ".tmp", UNMAPPED)
    except Exception:
        pass


# APEX-SURFACECHECK-2026-10-22 (owner, "Go"): compares the surface Apex uses for
# each tournament it scored in the last few days with the surface the stats warehouse
# recorded for that tournament's finished matches. Returns [(tournament, apex, warehouse, n)].
WAREHOUSE = os.path.join(HERE, "data", "warehouse", "tennis_warehouse.sqlite")
PICK_LOG = os.path.join(HERE, "apex_engine_picks_log_v1.csv")


def check_mismatches(days=3):
    import sqlite3
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    tours = {}
    try:
        with open(PICK_LOG, newline="") as fh:
            for r in csv.DictReader(fh):
                if (r.get("match_date") or "") >= since and r.get("tournament"):
                    tours[r["tournament"]] = tours.get(r["tournament"], 0) + 1
    except Exception:
        return []
    out = []
    try:
        con = sqlite3.connect(WAREHOUSE)
        for t, n in tours.items():
            row = con.execute("select surface, count(*) from matches where tournament = ? and match_date >= ? "
                              "group by surface order by 2 desc limit 1",
                              (t, (date.today() - timedelta(days=14)).isoformat())).fetchone()
            if not row or not row[0]:
                continue
            wh = str(row[0]).lower().replace("i.", "")
            wh = "hard" if wh.startswith("hard") or wh == "carpet" else wh
            apex = lookup(t)[0].lower()
            if wh in ("hard", "clay", "grass") and wh != apex:
                out.append((t, apex.title(), wh.title(), n))
        con.close()
    except Exception:
        return out
    return out


def alert_new_mismatches():
    """Posts one #alerts line per newly found mismatch (each tournament once). Never raises."""
    try:
        state_path = os.path.join(HERE, "data", "logs", "surface_mismatch_state.json")
        try:
            seen = json.load(open(state_path))
        except Exception:
            seen = {}
        new = [m for m in check_mismatches() if m[0] not in seen]
        if not new:
            return 0
        import apex_ops
        for t, apex, wh, n in new:
            apex_ops.send_discord("alerts", "\u26A0\uFE0F **Surface check:** Apex is scoring **%s** as **%s**, but its finished "
                                  "matches are recorded as **%s** (%d props scored). Aces, breaks and double faults will be off "
                                  "until it's fixed -- tell Claude to add a surface override." % (t, apex, wh, n))
            seen[t] = datetime.now().isoformat(timespec="seconds")
        with open(state_path, "w") as fh:
            json.dump(seen, fh)
        return len(new)
    except Exception as e:
        print("[surface] mismatch alert skipped: %s" % e)
        return 0


if __name__ == "__main__":
    if "--tour" in sys.argv:   # python3 apex_surface_map.py --tour "Name - City"
        _nm = sys.argv[sys.argv.index("--tour") + 1]
        try:
            print(tour_lookup(_nm, debug=True))
        except _Transient as te:
            print("tour calendars unreachable:", te)
        sys.exit(0)
    if "--check" in sys.argv:
        print(check_mismatches() or "no surface mismatches")
        sys.exit(0)
    if "--web" in sys.argv:   # python3 apex_surface_map.py --web "Name - City"  (web lookup only, nothing saved)
        # APEX-SURFACE-TOURPAGES-2026-10-30: --tour "Name - City" asks only the tour calendars
        for a in sys.argv[1:]:
            if not a.startswith("--"):
                try:
                    print(a, "->", web_lookup(a, debug="--debug" in sys.argv))
                except _Transient as te:
                    print(a, "-> Wikipedia busy / unreachable (%s)" % te)
        sys.exit(0)
    if "--rebuild" in sys.argv:
        print("built %d names" % len(build()))
    for a in sys.argv[1:]:
        if not a.startswith("--"):
            print(a, "->", lookup(a))
