#!/usr/bin/env python3
"""APEX-WATCH-2026-11-07 (owner, "Go apex watch"): plain-words problem alerts.

Every #alerts check (com.apex.alerts, every 30 min) this looks at every feed Apex needs.
When something breaks or a backup takes over it posts a NEW message (never an edit):
what broke / what it does to your boards / what to do. Same problem again every 3 hours
until it is fixed, then one "✅ FIXED" post. Once a day after 9 AM: one morning line.

  python3 apex_watch.py --dry-run     print what it would post, save nothing
Switch: WATCH_ON. State: data/logs/apex_watch_state.json. Never raises into the caller.
"""
import os, sys, json, sqlite3, subprocess, importlib.util
from datetime import datetime, timedelta, date

WATCH_ON = True
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "data", "logs", "apex_watch_state.json")
REPEAT_H = 3
CALM_ON, CALM_FRESH_H, CALM_SOON_H = True, 18, 6   # APEX-CALMALERT-2026-11-12
MORNING_HOUR = 9
SGO_LOG = os.path.join(HERE, "data", "sgo_archive", "snapshots.log")
SGO_PID = os.path.join(HERE, "data", "sgo_archive", "snapshot.pid")
SA_STATE = os.path.join(HERE, "data", "sa365_alert_state.json")
PROPS_DB = os.path.join(HERE, "data", "props_db.sqlite")
BOARD_DB = os.path.join(HERE, "data", "board_history.sqlite")
SPORTS_HEALTH = os.path.expanduser("~/ApexSports/_shared/health/health.py")
SPORT_NAMES = {"nfl": "NFL", "cfb": "College Football", "nhl": "NHL", "cbb": "College Basketball",
               "wnba": "WNBA", "mlb": "MLB", "soccer": "Soccer", "afl": "AFL", "rugby": "Rugby"}
DRY = "--dry-run" in sys.argv


def _now():
    return datetime.now()


def _clock(dt):
    return dt.strftime("%-m/%-d %-I:%M %p")


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)


def _md_label(md):
    try:
        return datetime.strptime(md, "%Y-%m-%d").strftime("%-m/%-d")
    except Exception:
        return md


# ---------------------------------------------------------------- checks
# each returns None (fine) or dict(title, broke, boards, todo[, backup])

def check_totals():
    """bet365 itself (FonBet rows do NOT count), and who is covering the gap."""
    import apex_bet365_totals as b3
    try:
        import apex_sgo_backup as sgo
    except Exception:
        sgo = None
    today = date.today()
    days = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
    now_utc = datetime.utcnow().isoformat(timespec="seconds")
    con = _ro(PROPS_DB)
    rows = con.execute("select player, opponent, match_date, start_utc from projections "
                       "where category = 'total_games' and match_date in (?, ?) and player not like '%/%'",
                       days).fetchall()
    con.close()
    out = []
    first = {}   # APEX-CALMALERT-2026-11-12: earliest start per day
    for md in days:
        props = [r for r in rows if r[2] == md and (md != days[0] or (r[3] or "") > now_utc)]
        if len(props) < 5:
            continue
        first[md] = min([r[3] for r in props if r[3]] or [""])
        matches = {}
        for pl, op, d, st in props:
            nm = b3.pair_of({"player": pl, "opponent": op})
            matches.setdefault(frozenset(b3.name_key(x) for x in nm), (nm, b3.row_dates(d, st)))
        bet, fon, sg, none = 0, 0, 0, []
        for nm, ds in matches.values():
            book = None
            for x in ds:
                if b3.log_total(nm, x)[0] is not None:
                    book = b3.book_of(nm, x) or "bet365"
                    break
            if book == "bet365":
                bet += 1
            elif book == "fonbet":
                fon += 1
            else:
                got = False
                if sgo is not None:
                    for x in ds:
                        try:
                            if sgo.total(list(nm), x)[0] is not None:
                                got = True
                                break
                        except Exception:
                            pass
                if got:
                    sg += 1
                else:
                    none.append(" v ".join(nm))
        out.append((md, len(matches), bet, fon, sg, none))
    if not out:
        return None
    bad = [o for o in out if o[2] < 0.5 * o[1]]
    if not bad:
        return None
    parts, effect = [], []
    for md, n, bet, fon, sg, none in bad:
        parts.append("bet365 has a Total Games line for only %d of %d matches on %s" % (bet, n, _md_label(md)))
        cover = []
        if fon:
            cover.append("FonBet covers %d" % fon)
        if sg:
            cover.append("SportsGameOdds covers %d" % sg)
        line = "%s: %s" % (_md_label(md), ", ".join(cover) if cover else "no backup covers any")
        if none:
            line += "; %d match%s have no total at all (%s%s), so their Total Games picks sit in BACKUPS" % (
                len(none), "" if len(none) == 1 else "es", ", ".join(none[:3]), ", ..." if len(none) > 3 else "")
        effect.append(line)
    # APEX-CALMALERT-2026-11-12 (his "go calm alert"): only tomorrow is short, bet365 sent lines in the
    # last 18 hours, and tomorrow's first match is 6+ hours away -> the lines are just not posted yet.
    # Say "waiting" one time, not "broken". It turns into a real problem by itself if they never come.
    try:
        soon = (datetime.utcnow() + timedelta(hours=CALM_SOON_H)).isoformat(timespec="seconds")
        calm = (CALM_ON and all(o[0] != days[0] for o in bad)
                and all(first.get(o[0], "") > soon for o in bad)
                and b3.log_has_any(CALM_FRESH_H))
    except Exception:
        calm = False
    if calm:
        return dict(title="bet365 has not posted tomorrow's Total Games lines yet",
                    broke="; ".join(parts) + ". Today's bet365 lines came in fine.",
                    boards=" · ".join(effect) + ". Those picks move up by themselves when the lines arrive.",
                    todo="Nothing to type. This is normal the day before.",
                    backup=" · ".join(effect), wait=True, once=True)
    return dict(title="bet365 is not sending Total Games lines",
                broke="; ".join(parts) + ".",
                boards=" · ".join(effect) + ".",
                todo="Nothing to type. Apex uses FonBet and SportsGameOdds instead. If it lasts more than a day, "
                     "send SportsAPI365 the support email.",
                backup=" · ".join(effect))


def check_sgo():
    now = _now()
    alive = False
    try:
        pid = int(open(SGO_PID).read().split()[0])
        os.kill(pid, 0)
        alive = True
    except Exception:
        alive = False
    last = ""
    try:
        with open(SGO_LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4000))
            lines = [l for l in f.read().decode("utf-8", "replace").splitlines() if " ok " in l or "error" in l.lower()]
        last = lines[-1] if lines else ""
    except Exception:
        pass
    try:
        when = datetime.strptime(last[:16], "%Y-%m-%d %H:%M")
    except Exception:
        when = None
    why = None
    if not alive:
        why = "the SportsGameOdds pull is not running"
    elif when is None or now - when > timedelta(minutes=75):
        why = "no SportsGameOdds pull since %s" % (_clock(when) if when else "unknown")
    elif "HTTP 401" in last or "HTTP 403" in last:
        why = "SportsGameOdds is refusing the key"
    elif "events=0" in last:
        why = "SportsGameOdds sent back no tennis matches"
    if not why:
        return None
    return dict(title="SportsGameOdds backup is down",
                broke=why + ".",
                boards="Matches that bet365 and FonBet don't price have no book total, so their Total Games picks "
                       "go to BACKUPS and First Set LESS can't be checked.",
                todo="If it says the key is refused: check your SportsGameOdds account, then Discord /setkey. "
                     "Otherwise tell Claude.")


def check_sa365():
    try:
        st = json.load(open(SA_STATE))
    except Exception:
        return None
    if not st:
        return None
    return dict(title="SportsAPI365 is refusing the key",
                broke="SportsAPI365 (bet365 odds) refuses Apex's key.",
                boards="New PrizePicks props can't be scored, so boards stop filling.",
                todo="Make a new key in the SportsAPI365 dashboard, then Discord /setkey → SportsAPI365.")


def check_prizepicks():
    now = _now()
    if 2 <= now.hour < 8:   # PrizePicks posts little overnight
        return None
    con = _ro(PROPS_DB)
    last = con.execute("select max(last_seen) from projections").fetchone()[0]
    con.close()
    try:
        from datetime import timezone
        t = datetime.fromisoformat(last).astimezone().replace(tzinfo=None)
    except Exception:
        return None
    if now - t < timedelta(hours=4):
        return None
    return dict(title="No new PrizePicks lines",
                broke="Apex hasn't saved a PrizePicks line since %s." % _clock(t),
                boards="Boards don't see new props or line changes.",
                todo="Run /fetch. If it fails, tell Claude.")


def check_bots(state):
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    down = []
    for label, name in (("com.apex.discordbot", "the Discord bot"), ("com.apex.apexscheduler", "the scheduler")):
        row = [l for l in out.splitlines() if l.endswith(label)]
        if not row or row[0].split()[0] == "-":
            down.append(name)
    prev = state.get("bots_down_last") or []
    state["bots_down_last"] = down
    down = [d for d in down if d in prev]   # two checks in a row (a restart takes seconds)
    if not down:
        return None
    return dict(title="Tennis Apex is not running",
                broke=" and ".join(down).capitalize() + " stopped.",
                boards="No new boards, no /fetch, no automatic jobs.",
                todo="Tell Claude, or restart the Mac.")


def check_short_boards():
    con = _ro(BOARD_DB)
    pid = con.execute("select publish_id, board_day, published_at from publishes where platform = 'prizepicks' "
                      "order by publish_id desc limit 1").fetchone()
    if not pid:
        con.close()
        return None
    rows = con.execute("select board, spot, player, opponent, prop_type, reason from history where publish_id = ? "
                       "and board in ('top3', 'elite6')", (pid[0],)).fetchall()
    con.close()
    want = {"top3": 3, "elite6": 6}
    lab = {"top3": "Top 3", "elite6": "Elite 6"}
    short, held = [], []
    for b, n in want.items():
        have = sum(1 for r in rows if r[0] == b and str(r[1]).startswith("#"))
        if have < n:
            short.append("%s has %d of %d picks" % (lab[b], have, n))
    for b, spot, pl, op, pt, why in rows:
        if b == "elite6" and "no book total" in (why or ""):
            nm = op if pl == "match" else "%s v %s" % (pl, op)
            if nm not in held:
                held.append(nm)
    if not short or not held:
        return None   # short because nothing good qualifies = by design, not a problem
    return dict(title="Boards short because of missing data",
                broke="%s on the %s board." % ("; ".join(short), _md_label(pid[1])),
                boards="%d pick%s held only because no book has a total: %s." % (
                    len(held), "" if len(held) == 1 else "s", ", ".join(held[:4])),
                todo="Nothing to type; they come back on their own when a total shows up. "
                     "If this lasts all day, tell Claude.")


def check_sports():
    try:
        spec = importlib.util.spec_from_file_location("apex_sports_health", SPORTS_HEALTH)
        H = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(H)
    except Exception:
        return None
    bad = []
    for pre, rel in H.SPORTS:
        if os.path.exists(os.path.join(H.ROOT, rel, "INACTIVE")):
            continue
        try:
            txt, _ = H.sport_block(pre, rel)
        except Exception:
            continue
        for l in txt.splitlines():
            if l.startswith(H.BAD):
                bad.append("%s: %s" % (SPORT_NAMES.get(pre, pre), l[len(H.BAD):].strip()))
    try:
        txt, _ = H.basketball_block()
        bad += ["NBA: %s" % l[len(H.BAD):].strip() for l in txt.splitlines() if l.startswith(H.BAD)]
    except Exception:
        pass
    if not bad:
        return None
    return dict(title="Problem in the other sports",
                broke="; ".join(bad[:5]) + ".",
                boards="That sport's boards may be missing or old.",
                todo="Tell Claude.")


# APEX-FEEDALERT-2026-11-10 (owner, "go feed alert", after NBA sat on 13-hour-old lines with no alert):
# a feed that stops is a problem the minute it goes stale, in every sport, not only when a bot is down.
SPORTS_ROOT = os.path.expanduser("~/ApexSports")
FEED_SPORTS = [("NFL", "football/nfl"), ("College Football", "football/ncaaf"), ("NHL", "hockey/nhl"), ("College Basketball", "basketball/ncaab"),
               ("WNBA", "basketball/wnba"), ("MLB", "baseball/mlb"), ("Soccer", "soccer"), ("AFL", "afl"), ("Rugby", "rugby")]
NBA_STALE_MIN = 100          # NBA pulls every 30 min from 8:00 AM to midnight CT


def _hm(ts):
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(ts, ZoneInfo("America/Chicago")).strftime("%-m/%-d %-I:%M %p CT")


def check_feeds():
    """PrizePicks lines going old in any sport bot (paused after a slow-down, pull failing, collector stuck)."""
    import time
    from zoneinfo import ZoneInfo
    now = time.time(); ct = datetime.now(ZoneInfo("America/Chicago")); mins = ct.hour * 60 + ct.minute
    bad = []
    try:   # Apex Basketball (NBA): own collector, 8:00 AM to 11:59 PM CT
        s = json.load(open(os.path.join(SPORTS_ROOT, "basketball/logs/line_collector_state.json")))
        pause = float(s.get("pause_until") or 0)   # last_pull = last GOOD pull ("YYYY-MM-DD HH:MM" CT); last_pull_ts is only the last try
        last = datetime.strptime(s["last_pull"], "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/Chicago")).timestamp() if s.get("last_pull") else 0
        if mins >= 8 * 60 + 45 and last and now - last > NBA_STALE_MIN * 60:
            bad.append("NBA lines last saved %s%s" % (_hm(last), (", paused until %s because PrizePicks said slow down" % _hm(pause)) if pause > now else ""))
    except Exception:
        pass
    pause = {}
    try:
        pause = json.load(open(os.path.join(SPORTS_ROOT, "_shared/pp_cooldown.json")))
    except Exception:
        pass
    for name, rel in FEED_SPORTS:
        d = os.path.join(SPORTS_ROOT, rel)
        try:
            if os.path.exists(os.path.join(d, "INACTIVE")):
                continue
            cfg = json.load(open(os.path.join(d, "config/sport.json")))
            if not (cfg.get("prizepicks") or {}).get("leagues"):
                continue
            con = _ro(os.path.join(d, "database/history.db"))
            kv = dict(con.execute("select k, v from kv where k in ('pp_last_pull','pp_zero_streak')").fetchall()); con.close()
            if int(kv.get("pp_zero_streak") or 0) >= 3:
                continue                      # off season / no games: it only checks once a day
            st = json.load(open(os.path.join(d, "cache/scheduler_state.json")))
            ok = st.get("last_pp_ok") or kv.get("pp_last_pull")
            if not ok:
                continue
            last = datetime.fromisoformat(str(ok).replace("Z", "+00:00")).timestamp()
            every = int(cfg["schedule"].get("pp_every_min") or 180); w0, w1 = cfg["schedule"]["window"]
            o = int(w0[:2]) * 60 + int(w0[3:5]); c = int(w1[:2]) * 60 + int(w1[3:5])
            if not (o + 60 <= mins <= c):
                continue
            if now - last > (2 * every + 30) * 60:
                until = float(pause.get("until") or 0)
                bad.append("%s lines last saved %s%s" % (name, _hm(last), (", paused until %s because PrizePicks said slow down" % _hm(until)) if until > now else ""))
        except Exception:
            continue
    if not bad:
        return None
    return dict(title="Old PrizePicks lines",
                broke="; ".join(bad) + ".",
                boards="Those boards are built on old lines until the next good pull. Do not play them until the card shows a fresh time.",
                todo="If it says paused until a time, it restarts on its own then. If it is still old after that, tell Claude.")


def _stamp(line):
    try:
        d = datetime.strptime("%d/%s" % (datetime.now().year, line[:14]), "%Y/%m/%d %H:%M:%S"); return d.strftime("%-m/%-d %-I:%M %p CT")
    except Exception:
        return line[:11].strip()

def check_books():
    """NBA sportsbook lines stopped (The Odds API out of credits). Told once, not repeated every 3 hours."""
    try:
        if not os.path.getsize(os.path.join(SPORTS_ROOT, "basketball/secrets/odds_api_key.txt")):
            return None
        L = [l for l in open(os.path.join(SPORTS_ROOT, "basketball/logs/book_lines.log"), errors="replace").read().splitlines() if l.strip()]
    except Exception:
        return None
    if not L or "STOP: only" not in L[-1]:
        return None
    first = L[-1]
    for l in reversed(L):
        if "STOP: only" in l:
            first = l
        elif "credits left" not in l:
            break
    return dict(title="NBA sportsbook lines stopped", once=True,
                broke="The Odds API account is out of credits (%s). No sportsbook lines for NBA since %s." % (L[-1].split("STOP: ")[-1].split(",")[0], _stamp(first)),
                boards="NBA cards say 'no book line'. Picks still score from PrizePicks lines and box scores.",
                todo="Add credits or change the plan at the-odds-api.com, or cancel it. It starts again by itself when credits are back.")


def check_oddsgap():
    """APEX-PPSPACING-2026-11-10: NHL sportsbook lines (TheOddsGap, pulled hourly) stopped coming in. Told once."""
    import time
    from zoneinfo import ZoneInfo
    d = os.path.join(SPORTS_ROOT, "hockey/nhl")
    try:
        if os.path.exists(os.path.join(d, "INACTIVE")):
            return None
        cfg = json.load(open(os.path.join(d, "config/sport.json")))
        w0, w1 = cfg["schedule"]["window"]
        ct = datetime.now(ZoneInfo("America/Chicago")); mins = ct.hour * 60 + ct.minute
        o = int(w0[:2]) * 60 + int(w0[3:5]); c = int(w1[:2]) * 60 + int(w1[3:5])
        if not (o + 150 <= mins <= c):
            return None
        last = os.path.getmtime(os.path.join(d, "cache/books_room.json"))
    except Exception:
        return None
    if time.time() - last < 150 * 60:
        return None
    return dict(title="NHL sportsbook lines stopped", once=True,
                broke="TheOddsGap has not sent NHL sportsbook lines since %s (they should come every hour)." % _hm(last),
                boards="NHL picks and boards do not change. Only the 📚 note on NHL cards goes missing or shows older lines.",
                todo="Nothing right now. If it stays off, the TheOddsGap key or account needs a look.")


# APEX-SGOALERT-2026-11-14 (owner, "Go sgo alerts"; SportsGameOdds refused every sport from 5:41 AM 11/14 with no warning): reads each
# sport's sportsbook-pull log (NBA book_lines.log, NFL / college / NHL / soccer collector.log). When the latest pull was refused (HTTP 401/403/429),
# one 🛑 card names the sports, since when and what it breaks; told once per outage (not every 3 hours), and "✅ FIXED" when a pull works again.
# Logs only, never the key. Undo: SGO_BOOKS_ON = False.
SGO_BOOKS_ON = True
SGO_BOOK_LOGS = [("NBA", "basketball/logs/book_lines.log"), ("NFL", "football/nfl/logs/collector.log"), ("College football", "football/ncaaf/logs/collector.log"),
                 ("NHL", "hockey/nhl/logs/collector.log"), ("Soccer", "soccer/logs/collector.log")]


def _sgo_tail(path, n=60):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - 20000))
            L = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return []
    return [l for l in L if ("SportsGameOdds" in l and ("books:" in l or "pull failed" in l or "Book lines saved" in l))][-n:]


def check_sgo_books():
    if not SGO_BOOKS_ON:
        return None
    down, good = [], []
    for name, rel in SGO_BOOK_LOGS:
        L = _sgo_tail(os.path.join(SPORTS_ROOT, rel))
        if not L:
            continue
        refused = lambda l: any(x in l for x in ("answered 401", "answered 403", "answered 429"))
        if refused(L[-1]) or (len(L) > 1 and refused(L[-2]) and "0 book lines" in L[-1] and L[-1][:14] == L[-2][:14]):
            first = L[-1]
            for l in reversed(L):
                if refused(l) or "0 book lines" in l: first = l
                else: break
            down.append((name, first[:14]))
        else:
            last_ok = [l for l in L if not refused(l) and "0 book lines" not in l]
            if last_ok: good.append((name, last_ok[-1][:14]))
    if not down:
        return None
    since = min(d[1] for d in down)
    names = ", ".join(n for n, _ in down)
    # APEX-KEYRELOAD-2026-11-15: the key works somewhere else (another sport, or the tennis backup snapshot in the last 2 hours)
    # -> it is not the account; a program is behind on the key. Every collector now re-reads the key on each pull.
    try:
        _sn = open(os.path.join(HERE, "data", "sgo_archive", "snapshots.log")).read().splitlines()[-1]
        _snap_ok = " ok " in _sn and (datetime.now() - datetime.strptime(_sn[:16], "%Y-%m-%d %H:%M")).total_seconds() < 7200
    except Exception:
        _snap_ok = False
    if good or _snap_ok:
        works = ", ".join(n for n, _ in good) + (" and " if good and _snap_ok else "") + ("the tennis backup" if _snap_ok else "")
        return dict(title="SportsGameOdds key: a program is behind (not your account)", once=True,
                    broke="SportsGameOdds refused %s (since %s), but the same key works for %s right now. A program is still sending an old key." % (names, since, works),
                    boards="Those sports score on old sportsbook lines until it catches up (NBA numbered boards go empty).",
                    todo="Nothing for you. Every collector re-reads the saved key on its next pull. If this card is still open after the next pull, tell me and I restart that sport.")
    return dict(title="SportsGameOdds sportsbook lines DOWN", once=True,
                broke="SportsGameOdds refused the sportsbook pull for %s (first refusal %s). No new sportsbook lines." % (names, since),
                boards="NBA numbered boards go empty (no room check, everything waits in BACKUPS). NFL, college and NHL keep scoring on the last good "
                       "sportsbook lines, so the room numbers can be old. Best 6 / Best 3 get short.",
                todo="Check your SportsGameOdds account (plan, trial or limit), then save the key with /setkey in Discord. Pulls start again by themselves.")


# ---------------------------------------------------------------- APEX-GRADEALERTS-2026-11-15 (owner, "I should get alerts for things like this")
GRADE_SPORTS = [("NFL", "football/nfl/database/history.db", "start", "team", "opponent"),
                ("College football", "football/ncaaf/database/history.db", "start", "team", "opponent"),
                ("NHL", "hockey/nhl/database/history.db", "start", "team", "opponent"),
                ("College basketball", "basketball/ncaab/database/history.db", "start", "team", "opponent"),
                ("Soccer", "soccer/database/history.db", "start", "team", "opponent"),
                ("NBA", "basketball/data/processed/bball_tracker.sqlite", "start_utc", "team", "opponent")]
GRADE_HOUR = 4          # every sport grades at 4 AM CT
GRADE_GAME_H = 3        # a game counts as over 3 hours after its start
TENNIS_LATE_H = 15      # tennis: 3 hours of match + 12 hours to settle


def _utc(s):
    from datetime import timezone
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def check_ungraded():
    """Picks from games that should have been in the last 4 AM grading run but are still open, any sport. One card, ✅ when cleared."""
    from datetime import timezone
    now = datetime.now()
    run = now.replace(hour=GRADE_HOUR, minute=15, second=0, microsecond=0)
    if now < run:
        run -= timedelta(days=1)
    cut = (run - timedelta(hours=GRADE_GAME_H)).astimezone(timezone.utc)
    low = cut - timedelta(days=7)
    out = []
    for name, rel, sc, tc, oc in GRADE_SPORTS:
        path = os.path.join(SPORTS_ROOT, rel)
        if not os.path.exists(path):
            continue
        try:
            con = _ro(path)
            g = {}
            for st, tm, op in con.execute("select %s, %s, %s from picks where status='open'" % (sc, tc, oc)):
                t = _utc(st)
                if t is None or not (low <= t <= cut):
                    continue
                k = (str(tm or "?"), str(op or "?"), t.astimezone().strftime("%-m/%-d"))
                g[k] = g.get(k, 0) + 1
            con.close()
        except Exception:
            continue
        if g:
            n = sum(g.values())
            top = sorted(g.items(), key=lambda kv: -kv[1])[:4]
            out.append("%s: %d picks, %d game%s (%s%s)" % (name, n, len(g), "" if len(g) == 1 else "s",
                       ", ".join("%s v %s %s: %d" % (a, b, d, c) for (a, b, d), c in top), ", ..." if len(g) > 4 else ""))
    if not out:
        return None
    return dict(title="Picks not graded after the 4 AM grading run", once=True,
                broke="These games should be graded by now but their picks are still open: " + " · ".join(out) +
                      ". Usual causes: the game was moved or postponed, the box score is missing, or the team/player names don't match.",
                boards="Records, real win rates and the Best 6 / Best 3 trackers are missing these results until they grade.",
                todo="Nothing for you yet. Results are re-checked every hour from 3 PM; this card turns ✅ when they grade. If it is still open tomorrow, tell me.")


def check_tennis_unsettled():
    """Tennis legs still unsettled 12+ hours after their match ended (singles; last 10 days). Past 4 days Apex stops trying = can't be graded."""
    from datetime import timezone
    now = datetime.now(timezone.utc)
    try:
        con = _ro(PROPS_DB)
        rows = con.execute("select player, opponent, start_utc, settle_note from projections where settled_ts is null and start_utc != '' "
                           "and start_utc <= ? and start_utc >= ? and player not like '%/%' and source in ('partner_api', 'boardwatch_backfill')",
                           ((now - timedelta(hours=TENNIS_LATE_H)).isoformat(timespec="seconds"), (now - timedelta(days=10)).isoformat(timespec="seconds"))).fetchall()
        con.close()
    except Exception:
        return None
    if not rows:
        return None
    m = {}
    for pl, op, st, note in rows:
        a, b = (op.split(" v ")[0], op.split(" v ")[-1]) if pl == "match" else (pl, op)
        k = " v ".join(sorted((str(a).replace(" 1st Set", ""), str(b).replace(" 1st Set", ""))))
        t = _utc(st)
        x = m.setdefault(k, dict(n=0, note=note or "", day=t.astimezone().strftime("%-m/%-d") if t else "?", gone=bool(t and now - t > timedelta(days=4))))
        x["n"] += 1
    def why(note):
        n = str(note or "").lower()
        if "not found" in n: return "match not found at the results site"
        if "no stat sheet" in n: return "no stat sheet (stat props)"
        if "not started" in n or "not_finished" in n: return "results site still says not finished"
        if "unfinished" in n: return "score unfinished"
        return n[:40] or "waiting"
    L = sorted(m.items(), key=lambda kv: (kv[1]["gone"], -kv[1]["n"]))   # still being tried first
    gone = [k for k, v in L if v["gone"]]
    txt = " · ".join("%s %s: %d legs, %s%s" % (k, v["day"], v["n"], why(v["note"]), " (gave up)" if v["gone"] else "") for k, v in L[:8])
    return dict(title="Tennis legs not settled", once=True,
                broke="%d legs from %d matches are still unsettled 12+ hours after the match: %s%s." % (sum(v["n"] for v in m.values()), len(m), txt, " ..." if len(L) > 8 else "") +
                      (" %d match%s past 4 days: Apex stopped trying, these can't be graded." % (len(gone), "" if len(gone) == 1 else "es") if gone else ""),
                boards="Tennis records and learning miss these legs. Boards are not affected.",
                todo="Nothing for you. Apex retries every hour; this card turns ✅ when they settle. Matches that never happened or have no stat sheet will not settle.")


def check_stale_boards():
    """A sport scoring on old sportsbook lines (no successful pull for 18+ hours while it has games coming), and NBA boards empty because of it."""
    from datetime import timezone
    now = datetime.now()
    out = []
    for name, rel in SGO_BOOK_LOGS:
        L = _sgo_tail(os.path.join(SPORTS_ROOT, rel), 400)
        ok = [l for l in L if ("book lines saved" in l.lower() or "Book lines saved" in l or "pull done" in l) and not (" 0 book lines" in l) and "answered" not in l]
        last = None
        if ok:
            try:
                last = datetime.strptime("%d/%s" % (now.year, ok[-1][:14]), "%Y/%m/%d %H:%M:%S")
            except Exception:
                last = None
        if last and now - last < timedelta(hours=18):
            continue
        tries = [l for l in L if "SportsGameOdds" in l or "pull failed" in l or "book lines saved" in l]
        if not tries or not any(x in tries[-1] for x in ("answered 4", "pull failed")):
            continue   # the last pull was not refused (no games in its leagues, e.g. soccer during an international break): not stale
        db = [g for g in GRADE_SPORTS if g[0].lower().startswith(name.lower()[:5]) or (name == "NBA" and g[0] == "NBA")]
        coming = 0
        if db:
            try:
                con = _ro(os.path.join(SPORTS_ROOT, db[0][1]))
                u = datetime.now(timezone.utc)
                coming = sum(1 for (st,) in con.execute("select %s from picks where game_date >= ?" % db[0][2], ((now - timedelta(days=1)).date().isoformat(),))
                             if (lambda t: t is not None and u <= t <= u + timedelta(hours=24))(_utc(st)))
                con.close()
            except Exception:
                coming = 0
        if coming:
            out.append("%s (last good sportsbook pull %s)" % (name, last.strftime("%-m/%-d %-I:%M %p") if last else "not found"))
    if not out:
        return None
    return dict(title="Boards running on old sportsbook lines", once=True,
                broke="No fresh sportsbook lines for 18+ hours: " + ", ".join(out) + ".",
                boards="Room/gap numbers on these boards are old. NBA numbered boards go empty without sportsbook lines.",
                todo="Nothing for you unless the SportsGameOdds card says the account is the problem. This turns ✅ after the next good pull.")


CHECKS = [("oddsgap", check_oddsgap), ("totals", check_totals), ("sgo", check_sgo), ("sa365", check_sa365), ("pp", check_prizepicks),
          ("bots", None), ("short", check_short_boards), ("sports", check_sports),
          ("feeds", check_feeds), ("books", check_books), ("sgobooks", check_sgo_books),
          ("ungraded", check_ungraded), ("tennisunsettled", check_tennis_unsettled), ("staleboards", check_stale_boards)]   # APEX-GRADEALERTS-2026-11-15   # APEX-FEEDALERT-2026-11-10 · APEX-SGOALERT-2026-11-14


# ---------------------------------------------------------------- posting

def _post(text):
    if DRY:
        print("---- would post to #alerts ----\n" + text)
        return True
    import apex_ops as ops
    return ops.send_discord("alerts", text)


def _msg(p, head):
    if p.get("wait"):   # APEX-CALMALERT
        return "🕒 WAITING, NOT BROKEN: %s\n❓ What is going on: %s\n📋 What it does to your boards: %s\n👉 What to do: %s" % (
            p["title"], p["broke"], p["boards"], p["todo"])
    return "%s: %s\n❓ What broke: %s\n📋 What it does to your boards: %s\n👉 What to do: %s" % (
        head, p["title"], p["broke"], p["boards"], p["todo"])


def run(post=True):
    """One pass. Returns the list of texts posted. Never raises."""
    if not WATCH_ON:
        return []
    sent = []
    try:
        try:
            state = json.load(open(STATE))
        except Exception:
            state = {}
        open_ = state.get("open") or {}
        now = _now()
        found = {}
        for key, fn in CHECKS:
            try:
                p = check_bots(state) if key == "bots" else fn()
            except Exception as e:
                print("[watch] %s check skipped: %s" % (key, e))
                if key in open_:
                    found[key] = open_[key]["p"]   # can't tell: keep it open, don't call it fixed
                continue
            if p:
                found[key] = p
        for key, p in found.items():
            o = open_.get(key)
            if o is None:
                t = _msg(p, "🛑 APEX PROBLEM")
                if not post or _post(t):
                    sent.append(t)
                    open_[key] = dict(p=p, since=now.isoformat(timespec="seconds"), last=now.isoformat(timespec="seconds"))
            else:
                was_wait = bool((o.get("p") or {}).get("wait"))
                o["p"] = p
                if was_wait and not p.get("wait"):   # APEX-CALMALERT: a wait that turned real is told at once
                    t = _msg(p, "🛑 APEX PROBLEM")
                    if not post or _post(t):
                        sent.append(t)
                        o["last"] = now.isoformat(timespec="seconds")
                elif not was_wait and p.get("wait"):   # was called broken, is really a wait: say so one time
                    t = _msg(p, "")
                    if not post or _post(t):
                        sent.append(t)
                        o["last"] = now.isoformat(timespec="seconds")
                elif not p.get("once") and now - datetime.fromisoformat(o["last"]) >= timedelta(hours=REPEAT_H):   # APEX-FEEDALERT: "once" problems are told one time
                    t = _msg(p, "🛑 STILL BROKEN (since %s)" % _clock(datetime.fromisoformat(o["since"])))
                    if not post or _post(t):
                        sent.append(t)
                        o["last"] = now.isoformat(timespec="seconds")
        for key in [k for k in open_ if k not in found]:
            o = open_[key]
            t = "✅ FIXED: %s (broken from %s to %s)" % (o["p"]["title"], _clock(datetime.fromisoformat(o["since"])), _clock(now))
            if o["p"].get("wait"):   # APEX-CALMALERT
                t = "✅ ARRIVED: bet365 posted tomorrow's Total Games lines (%s)" % _clock(now)
            if not post or _post(t):
                sent.append(t)
                del open_[key]
        if now.hour >= MORNING_HOUR and state.get("morning") != now.date().isoformat():
            if open_:
                L = ["🌅 APEX WATCH · morning: %d problem%s open" % (len(open_), "" if len(open_) == 1 else "s")]
                L += ["• %s (since %s)" % (o["p"]["title"], _clock(datetime.fromisoformat(o["since"]))) for o in open_.values()]
                bk = (open_.get("totals") or {}).get("p", {}).get("backup")
                if bk:
                    L.append("Running on backups: " + bk)
            else:
                L = ["🌅 APEX WATCH · morning: all feeds normal (bet365, SportsGameOdds, SportsAPI365, PrizePicks, bots, other sports)."]
            t = "\n".join(L)
            if not post or _post(t):
                sent.append(t)
                state["morning"] = now.date().isoformat()
        state["open"] = open_
        state["checked"] = now.isoformat(timespec="seconds")
        if not DRY:
            tmp = STATE + ".tmp"
            json.dump(state, open(tmp, "w"), indent=1)
            os.replace(tmp, STATE)
    except Exception as e:
        print("[watch] skipped: %s: %s" % (type(e).__name__, e))
    return sent


if __name__ == "__main__":
    run(post=True)
