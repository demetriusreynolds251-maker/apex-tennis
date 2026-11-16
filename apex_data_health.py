#!/usr/bin/env python3
"""
APEX-DATAHEALTH-2026-10-22 (owner: "we don't never need this to happen again
... consider other areas that this could happen and create safety measures", then
"Go"). Checks the inputs every score depends on, for the props scored in the last
2 days. Each check returns (name, ok, detail). Shown in the daily status as DATA
HEALTH; any check that newly fails posts one #alerts line (alert_new_failures(),
run at each manual/auto publish).

  1 Surface     every tournament's surface is confirmed by its finished matches (or an
                override) and matches what Apex scored with
  2 Tour        the ATP/WTA tour Apex used agrees with the stats warehouse for that player
  3 Names       props priced from a surname-only name match (could be the wrong match)
  4 Player data the player file was updated in the last 2 days
  5 Odds        two price sources for the same match disagree on the favorite by 15+ pts

  python3 apex_data_health.py            # print every check
"""
import csv, json, os, sqlite3, unicodedata
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PICK_LOG = os.path.join(HERE, "apex_engine_picks_log_v1.csv")
WAREHOUSE = os.path.join(HERE, "data", "warehouse", "tennis_warehouse.sqlite")
STATE_FILE = os.path.join(HERE, "apex_prop_state_v1.pkl")
ALERT_STATE = os.path.join(HERE, "data", "logs", "data_health_state.json")
DAYS = 2
TOUR_MIN = 5           # flag tour only when 5+ scored props disagree
NAME_WARN = 20         # flag surname-only matches at 20+ props
ODDS_GAP = 0.15
STATE_MAX_AGE_H = 48


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def _recent_rows():
    cut = (date.today() - timedelta(days=DAYS)).isoformat()
    out = []
    try:
        with open(PICK_LOG, newline="") as fh:
            for r in csv.DictReader(fh):
                if (r.get("match_date") or "") >= cut:
                    out.append(r)
    except Exception:
        pass
    return out


def _names(r):
    if r.get("player") == "match":
        return [x.strip() for x in str(r.get("opponent") or "").split(" v ") if x.strip()]
    return [r.get("player")]


def _live_newest(rows):
    """Newest pick-log row per prop, for matches that have not started yet."""
    now = datetime.utcnow().isoformat()
    newest = {}
    for r in rows:
        st = str(r.get("slip_start_utc") or "")[:19]
        if st and st <= now:
            continue
        newest[(r.get("match_id"), r.get("player"), r.get("category"), r.get("line"), r.get("prop_type"))] = r
    return list(newest.values())


def check_surface(rows):
    import apex_surface_map as asm
    tours = {}
    try:   # only props still on the PrizePicks board matter
        import io, contextlib, apex_edge_tracker as T
        with contextlib.redirect_stdout(io.StringIO()):
            picks, _ = T.load_current_board()
        onb = {(p.get("match_id"), p.get("category"), str(p.get("line")), str(p.get("prop_type") or "").lower()) for p in picks}
    except Exception:
        onb = None
    for r in _live_newest(rows):
        if str(r.get("prop_type") or "").lower() in ("goblin", "demon") and str(r.get("direction") or "").upper() == "LESS":
            continue   # can't be played, never reaches a board
        if onb is not None and (r.get("match_id"), r.get("category"), str(r.get("line")), str(r.get("prop_type") or "").lower()) not in onb:
            continue
        t = r.get("tournament") or ""
        if t:
            tours.setdefault(t, [0, {}])
            tours[t][0] += 1
            u = (r.get("surface") or "").lower()
            tours[t][1][u] = tours[t][1].get(u, 0) + 1
    bad = []
    for t, (n, used) in tours.items():
        st = asm.surface_status(t)
        want = asm.lookup(t)[0].lower()
        wrong = {u: c for u, c in used.items() if u and u != want}
        if st == "unknown":
            bad.append("%s: surface UNKNOWN (%d props)" % (t, n))
        elif st == "confirmed" and wrong:
            bad.append("%s: %d of %d props still scored on %s, really %s (needs a re-score)" % (
                t, sum(wrong.values()), n, "/".join(sorted(wrong)), want))
    guess = [t for t in tours if asm.surface_status(t) == "guess"]
    detail = ("; ".join(bad) if bad else "all %d tournaments confirmed" % (len(tours) - len(guess))
              + ("" if not guess else ", %d from same-season history (%s)" % (len(guess), ", ".join(sorted(guess)[:3]))))
    return "Surface", not bad, detail


def check_tour(rows):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % WAREHOUSE, uri=True, timeout=10)
        cnt = {}
        for a, b, t in con.execute("select player_a, player_b, tour from matches where match_date >= ?",
                                   ((date.today() - timedelta(days=120)).isoformat(),)):
            for nm in (a, b):
                c = cnt.setdefault(_norm(nm), {})
                k = str(t or "").upper()[:3]
                c[k] = c.get(k, 0) + 1
        con.close()
        # only trust the warehouse when it has 3+ matches for the player, all on one tour
        tour = {nm: list(c)[0] for nm, c in cnt.items() if len(c) == 1 and sum(c.values()) >= 3}
    except Exception as e:
        return "Tour", True, "warehouse unreadable (%s)" % e
    bad = {}
    for r in rows:
        if r.get("player") == "match":
            continue
        lab = "WTA" if "women" in (r.get("format_label") or "").lower() else ("ATP" if r.get("format_label") else None)
        t = tour.get(_norm(r.get("player")))
        if lab and t and t in ("ATP", "WTA") and t != lab:
            bad.setdefault(r.get("player"), [lab, t, 0])[2] += 1
    n = sum(v[2] for v in bad.values())
    if n < TOUR_MIN:
        return "Tour", True, "ATP/WTA agrees with the warehouse" + (" (%d small mismatches)" % n if n else "")
    top = sorted(bad.items(), key=lambda kv: -kv[1][2])[:3]
    return "Tour", False, "%d props scored on the other tour: %s" % (
        n, ", ".join("%s (scored %s, really %s)" % (k, v[0], v[1]) for k, v in top))


def check_names(rows):
    """Surname + first-initial price matches are normal (accents, short names); they are
    only a problem when two different players in the recent props share that key."""
    def key(n):
        parts = _norm(n).replace("-", " ").split()
        return (parts[-1], parts[0][:1]) if parts else None
    everyone = {}
    for r in rows:
        for n in _names(r):
            k = key(n)
            if k:
                everyone.setdefault(k, set()).add(_norm(n))
    sus = [r for r in rows if "surname_initial" in (r.get("price_source") or "")]
    amb = sorted({n for r in sus for n in _names(r) if len(everyone.get(key(n), ())) > 1})
    if amb:
        return "Names", False, "price may belong to a different player with the same surname + initial: " + ", ".join(amb[:4])
    return "Names", True, ("%d props priced on a surname + initial match, none ambiguous" % len(sus)) if sus \
        else "every price matched on the full name"


def check_player_data():
    try:
        age_h = (datetime.now().timestamp() - os.path.getmtime(STATE_FILE)) / 3600.0
    except Exception:
        return "Player data", False, "player file not found"
    return "Player data", age_h <= STATE_MAX_AGE_H, "player file updated %.0f hours ago" % age_h


def check_odds(rows):
    by = {}
    for r in rows:
        src = (r.get("price_source") or "")
        book = "pinnacle" if "pinnacle" in src else ("bet365" if "bet365" in src else None)
        try:
            fav = float(r.get("pinn_fav"))
        except (TypeError, ValueError):
            continue
        if book:
            by.setdefault(r.get("match_id"), {}).setdefault(book, []).append((r.get("ts"), fav, r))
    bad = []
    for mid, d in by.items():
        if "pinnacle" in d and "bet365" in d:
            a = max(d["pinnacle"], key=lambda x: x[0]); b = max(d["bet365"], key=lambda x: x[0])
            if abs(a[1] - b[1]) >= ODDS_GAP:
                nm = " v ".join(_names(a[2])) if a[2].get("player") == "match" else "%s v %s" % (a[2].get("player"), a[2].get("opponent"))
                bad.append("%s (Pinnacle %.0f%% vs bet365 %.0f%%)" % (nm, 100 * a[1], 100 * b[1]))
    return "Odds", not bad, ("; ".join(bad[:3]) + (" (+%d more)" % (len(bad) - 3) if len(bad) > 3 else "")) if bad \
        else "no big favorite disagreements between price sources"


def check_pinnacle():
    """APEX-PINNACLE-2026-10-22: the daily Pinnacle pull the 🎯 card line reads."""
    import apex_pinnacle as _pin
    ok, detail = _pin.today_status()
    return ("Pinnacle", ok, detail)


# APEX-SA365CHECK-2026-10-23 (owner, "It's paid, be sure it's working"): the
# SportsAPI365 key lapsed 10/22 ~11 PM (HTTP 401) and nothing said so -- new props went
# unscored/ungraded and ParlayPlay posted an empty board. One cheap call, cached 5 min.
SA365_STATE = os.path.join(HERE, "data", "logs", "sa365_health_state.json")
SA365_PROBE_TTL = 5 * 60
_HTTP_WORDS = {"401": "401 Unauthorized", "403": "403 Forbidden", "404": "404 Not Found", "429": "429 Too Many Requests"}


def _sa365_state():
    try:
        return json.load(open(SA365_STATE))
    except Exception:
        return {}


def _sa365_save(st):
    os.makedirs(os.path.dirname(SA365_STATE), exist_ok=True)
    json.dump(st, open(SA365_STATE, "w"))


def _since_label(iso):
    try:
        t = datetime.fromisoformat(iso)
    except Exception:
        return str(iso)
    hm = t.strftime("%I:%M %p").lstrip("0")
    return hm + " CT" if t.date() == date.today() else "%d/%d %s CT" % (t.month, t.day, hm)


def check_sa365():
    """("SportsAPI365", ok, detail). Probes the live-events list at most every 5 min."""
    st = _sa365_state()
    now = datetime.now()
    try:
        fresh = (now - datetime.fromisoformat(st.get("checked_at"))).total_seconds() < SA365_PROBE_TTL
    except Exception:
        fresh = False
    if not fresh:
        try:
            import apex_price_feed as _apf
            payload, err = _apf._api("/extend/api/events/live")
        except Exception as e:
            payload, err = None, "%s: %s" % (type(e).__name__, e)
        st["checked_at"] = now.isoformat(timespec="seconds")
        if payload is not None:
            st["ok"], st["err"], st["down_since"] = True, "", None
        else:
            st["ok"], st["err"] = False, str(err or "no answer")
            st["down_since"] = st.get("down_since") or st["checked_at"]
        _sa365_save(st)
    if st.get("ok"):
        return "SportsAPI365", True, "answering"
    code = str(st.get("err") or "").replace("HTTP ", "").strip()
    why = _HTTP_WORDS.get(code, ("HTTP " + code) if code.isdigit() else code)
    return "SportsAPI365", False, ("\U0001F6D1 SportsAPI365 not answering (%s) since %s — new props can't be "
                                   "scored or graded" % (why, _since_label(st.get("down_since"))))


def _chain_post(key, msg, back, nxt):
    """APEX-ALERTCHAIN-2026-10-25: a down/low alert opens one card that the matching back/recovered
    alert edits (green). True when the chain handled it."""
    try:
        import apex_alert_chain as AC
        import apex_alert_cards as C
        if not AC.CHAINS_ON:
            return False
        e = C.from_text(msg)
        if back:
            if not AC.get(key, max_age_min=7 * 24 * 60):
                return False
            e["color"] = C.GREEN
            return AC.finish(key, e, note="back %s" % AC.hm())
        if AC.get(key):
            return AC.replace(key, e, nxt)
        return AC.soft_start(key, e, nxt)
    except Exception as ex:
        print("[DataHealth] chain card failed (%s), posting it" % type(ex).__name__)
        return False


def alert_sa365():
    """One #alerts post when SportsAPI365 stops answering, one "✅ back" post when it answers
    again. Never raises."""
    try:
        _n, ok, detail = check_sa365()
        st = _sa365_state()
        msg = None
        if not ok and not st.get("alerted_down"):
            msg, st["alerted_down"], st["alerted_since"] = detail, True, st.get("down_since")
        elif ok and st.get("alerted_down"):
            msg = "✅ SportsAPI365 back -- answering again (was down since %s)" % _since_label(st.get("alerted_since"))
            st["alerted_down"], st["alerted_since"] = False, None
        if msg:
            if not _chain_post("sa365", msg, back=ok,   # APEX-ALERTCHAIN-2026-10-25: one card, down -> back
                               nxt="Apex checks again every hour and at every board update — this card turns ✅ "
                                   "when SportsAPI365 answers again. Until then new props wait unscored."):
                import apex_ops
                apex_ops.send_discord("alerts", msg)
            _sa365_save(st)
        return msg
    except Exception as e:
        print("[DataHealth] SportsAPI365 alert skipped: %s" % e)
        return None


def run():
    rows = _recent_rows()
    out = []
    for fn in (check_sa365, lambda: check_surface(rows), lambda: check_tour(rows), lambda: check_names(rows),
               check_player_data, lambda: check_odds(rows), check_pinnacle):
        try:
            out.append(fn())
        except Exception as e:
            out.append(("Check error", False, "%s: %s" % (type(e).__name__, e)))
    return out


def lines():
    L = [detail if detail.startswith("\U0001F6D1") else "%s %s: %s" % ("✅" if ok else "⚠️", name, detail)
         for name, ok, detail in run()]
    try:   # APEX-BET365-THREEPART-2026-10-22
        L += bet365_check_lines(all_clear_line=True)
    except Exception:
        pass
    return L


def alert_new_failures():
    """One #alerts line for each check that has just started failing. Never raises."""
    try:
        try:
            prev = json.load(open(ALERT_STATE))
        except Exception:
            prev = {}
        cur, msgs = {}, []
        for name, ok, detail in run():
            if not ok and name != "SportsAPI365":   # APEX-SA365CHECK-2026-10-23: alert_sa365() posts that one
                cur[name] = detail
                if prev.get(name) != detail:
                    msgs.append("⚠️ **Data check -- %s:** %s" % (name, detail))
        if msgs:
            import apex_ops
            apex_ops.send_discord("alerts", "\n".join(msgs))
        alert_bet365()   # APEX-BET365-THREEPART-2026-10-22: change-only
        os.makedirs(os.path.dirname(ALERT_STATE), exist_ok=True)
        json.dump(cur, open(ALERT_STATE, "w"))
        return len(msgs)
    except Exception as e:
        print("[DataHealth] alert skipped: %s" % e)
        return 0


if __name__ == "__main__":
    print("\n".join(lines()))


# APEX-DECLINEDDATA-2026-10-22 (owner: "if data check fail it should also say it
# in the general doing any fetch and those props should be in category declined and
# should tell me why", then "Go").
def held_props():
    """[(prop, reason)] for every live prop held off the boards by a data check. Never raises."""
    try:
        import io, contextlib, apex_edge_tracker as T
        with contextlib.redirect_stdout(io.StringIO()):
            picks, _ = T.load_current_board()
        out, seen = [], set()
        for p in picks:
            if T._is_unplayable(p) or T._pub_match_started(p):
                continue
            k = T._apex_prop_identity(p)
            if k in seen:
                continue
            seen.add(k)
            why = T._surface_hold(p)
            if why:
                out.append((p, why))
        return out
    except Exception:
        return []


def _prop_label(p):
    import apex_edge_tracker as T
    who = p.get("opponent") if p.get("player") == "match" else p.get("player")
    return "%s %s %s %s %s" % (who, T.LABEL.get(p.get("category"), p.get("category")), p.get("line"),
                               str(p.get("prop_type") or "standard").title(), str(p.get("direction") or "").upper())


BET365_MIN_PROPS = 5        # APEX-BET365-THREEPART-2026-10-22: only judge a day with 5+ Total Games props
BET365_MIN_COVER = 0.50     # warn when the bet365 log has a total for under half of that day's matches
BET365_ALERT_STATE = os.path.join(HERE, "data", "logs", "bet365_cover_state.json")


def _md_label(md):
    y, m, d = (int(x) for x in str(md)[:10].split("-"))
    return "%d/%d" % (m, d)


def bet365_coverage():
    """APEX-BET365-THREEPART-2026-10-22 (owner, "Go bet365"): for today (only matches not
    started yet) and tomorrow, [(match_date, matches with a bet365 total in the log, matches)]
    for days with 5+ Total Games props. Never raises."""
    try:
        import apex_bet365_totals as b3
        from datetime import timezone
        today = date.today()
        days = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
        now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(HERE, "data", "props_db.sqlite"), uri=True, timeout=10)
        rows = con.execute("select player, opponent, match_date, start_utc from projections "
                           "where category = 'total_games' and match_date in (?, ?) and player not like '%/%'",
                           days).fetchall()
        con.close()
        out = []
        for md in days:
            props = [(pl, op, d, st) for pl, op, d, st in rows
                     if d == md and (d != days[0] or (st or "") > now_utc)]
            if len(props) < BET365_MIN_PROPS:
                continue
            matches = {}
            for pl, op, d, st in props:
                nm = b3.pair_of({"player": pl, "opponent": op})
                matches.setdefault(frozenset(b3.name_key(x) for x in nm), (nm, b3.row_dates(d, st)))
            # APEX-WATCH-2026-11-07: count bet365's own totals only (FonBet rows made a false "recovered")
            have = sum(1 for nm, ds in matches.values()
                       if any(b3.log_total(nm, x)[0] is not None and (b3.book_of(nm, x) or "bet365") == "bet365" for x in ds))
            out.append((md, have, len(matches)))
        return out
    except Exception:
        return []


def bet365_graded_gaps(days=3):
    """(count, up to 3 example matches) of graded Total Games props from the last `days` days
    that still have no bet365 total in the props database. Never raises."""
    try:
        since = (date.today() - timedelta(days=days)).isoformat()
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(HERE, "data", "props_db.sqlite"), uri=True, timeout=10)
        rows = con.execute("select player, opponent, match_date from projections where category = 'total_games' "
                           "and settled_ts is not null and bet365_line is null and match_date >= ? "
                           "and player not like '%/%' order by match_date desc", (since,)).fetchall()
        con.close()
        try:   # APEX-NEVERPRICED-2026-10-23: matches bet365 never put a total on are not gaps
            import apex_props_db as _pdb
            np_ = _pdb.never_priced()
            def _k(r):
                return _pdb.gap_key(*((r[1].split(" v ") + [""])[:2] if r[0] == "match" else r[:2]), r[2])
            rows = [r for r in rows if _k(r) not in np_]
        except Exception:
            pass
        ex = []
        for pl, op, md in rows:
            lab = "%s (%s)" % (op if pl == "match" else "%s v %s" % (pl, op), _md_label(md))
            if lab not in ex:
                ex.append(lab)
        return len(rows), ex[:3]
    except Exception:
        return 0, []


def bet365_check_lines(all_clear_line=False):
    """DATA CHECK / DATA HEALTH lines for bet365 totals. Empty when all is well, unless
    all_clear_line (9 AM status) -- then one ✅ line."""
    L = []
    for md, have, n in bet365_coverage():
        if have < BET365_MIN_COVER * n:
            L.append("⚠️ bet365 totals cover only %d of %d matches for %s" % (have, n, _md_label(md)))
    cnt, ex = bet365_graded_gaps()
    if cnt:
        L.append("⚠️ %d graded Total Games prop%s from the last 3 days still ha%s no bet365 total (e.g. %s)" % (
            cnt, "" if cnt == 1 else "s", "s" if cnt == 1 else "ve", "; ".join(ex)))
    npl = []
    try:   # APEX-NEVERPRICED-2026-10-23: counted, not a warning
        import apex_props_db as _pdb
        since = (date.today() - timedelta(days=3)).isoformat()
        npn = sum(1 for k in _pdb.never_priced() if k[:10] >= since)
        if npn:
            npl.append("ℹ️ bet365 never priced: %d match%s" % (npn, "" if npn == 1 else "es"))
    except Exception:
        pass
    if not L and all_clear_line:
        cov = ", ".join("%s %d of %d" % (_md_label(md), have, n) for md, have, n in bet365_coverage())
        L.append("✅ bet365 totals: %s" % (("matches covered -- " + cov) if cov else "no day with 5+ Total Games props to check"))
    return L + npl


def alert_bet365():
    """One change-only #alerts post when bet365 coverage drops under 50% for today/tomorrow, no
    repeat until the set of short days changes; one "✅ recovered" post when it is fine again.
    Never raises."""
    alert_sa365()   # APEX-SA365CHECK-2026-10-23: rides the hourly props-db run and every publish
    try:
        try:
            prev = json.load(open(BET365_ALERT_STATE))
        except Exception:
            prev = {}
        low = {md: (have, n) for md, have, n in bet365_coverage() if have < BET365_MIN_COVER * n}
        was = prev.get("low") or {}
        msg = None
        if low and sorted(low) != sorted(was):
            msg = "\n".join("⚠️ bet365 totals cover only %d of %d matches for %s" % (h, n, _md_label(md))
                            for md, (h, n) in sorted(low.items()))
        elif was and not low:
            msg = "✅ bet365 totals recovered (%s)" % ", ".join(_md_label(md) for md in sorted(was))
        if msg:
            if not _chain_post("bet365cov", msg, back=not low,   # APEX-ALERTCHAIN-2026-10-25
                               nxt="Apex re-checks every hour; FonBet and SportsGameOdds fill in where they can "
                                   "— this card turns ✅ when coverage is back over half."):
                import apex_ops
                apex_ops.send_discord("alerts", msg)
        os.makedirs(os.path.dirname(BET365_ALERT_STATE), exist_ok=True)
        json.dump({"low": {k: list(v) for k, v in low.items()}, "at": datetime.now().isoformat(timespec="seconds")},
                  open(BET365_ALERT_STATE, "w"))
        return msg
    except Exception as e:
        print("[DataHealth] bet365 alert skipped: %s" % e)
        return None


def fetch_report_lines(limit=25):
    """The DATA CHECK block + DECLINED -- data problem list for a /fetch or 5 PM report."""
    try:
        import apex_edge_tracker as T
        checks = run()
        held = held_props()
        bad = [(n, d) for n, ok, d in checks if not ok]
        L = bet365_check_lines()   # APEX-BET365-THREEPART-2026-10-22 (replaces the daily props-database note)
        try:   # APEX-ODDSWATCH-2026-10-24: a thin board explains itself (matches bet365 hasn't priced yet)
            import apex_odds_watch as _ow
            _owl = _ow.status_line()
            if _owl:
                L.append(_owl)
            L += _ow.moved_lines()   # APEX-DATEMOVE-2026-10-25
        except Exception:
            pass
        try:   # APEX-PLAYERDATA-2026-10-31
            import apex_player_history as _phist
            _phl = _phist.status_line()
            if _phl:
                L.append(_phl)
        except Exception:
            pass
        try:   # APEX-SGOBACKUP-2026-10-31
            import apex_sgo_backup as _sb
            _sbl = _sb.data_check_line()
            if _sbl:
                L.append(_sbl)
        except Exception:
            pass
        if not bad and not held:
            return ["✅ **DATA CHECK** all clear (SportsAPI365, surface, tour, names, player data, odds, Pinnacle)"] + L
        for n, d in bad:
            if d.startswith("\U0001F6D1"):   # APEX-SA365CHECK-2026-10-23
                head = "\U0001F6D1 SportsAPI365 not answering"
                L.insert(0, "\U0001F6D1 **DATA CHECK -- SportsAPI365 not answering**" + d[len(head):])
                continue
            if d.startswith("\U0001F4CF Book lines: using backup"):   # APEX-ODDSPAPI-BACKUP-2026-10-22
                L.append("⚠️ **DATA CHECK -- %s**%s" % (d.split(" -- ")[0], d[len(d.split(" -- ")[0]):]))
                continue
            L.append("⚠️ **DATA CHECK FAILED -- %s:** %s" % (n, d))
        if held:
            L += ["", "❌ **DECLINED -- data problem** (%d prop%s held off the boards until fixed; they come back by themselves "
                  "once re-scored; Apex re-scores every prop in the 2 hours before its match)" % (len(held), "" if len(held) == 1 else "s")]
            held.sort(key=lambda x: -T._board_prob(x[0]))
            for p, why in held[:limit]:
                L.append("❌ %s (%.0f%%) -- %s" % (_prop_label(p), 100 * T._board_prob(p), why))
            if len(held) > limit:
                L.append("(+%d more)" % (len(held) - limit))
        return L
    except Exception as e:
        return ["⚠️ DATA CHECK could not run (%s)" % type(e).__name__]


def board_note(prop_type=None):
    """One line for a board when props of its type are held for a data problem, else ''."""
    held = [p for p, _ in held_props() if not prop_type or str(p.get("prop_type") or "standard").lower() == prop_type]
    if not held:
        return ""
    return ("\U0001F6D1 %d prop%s held off this board for a data problem -- see DATA CHECK in the last #general fetch report"
            % (len(held), "" if len(held) == 1 else "s"))
