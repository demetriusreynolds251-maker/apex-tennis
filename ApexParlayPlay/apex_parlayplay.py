#!/usr/bin/env python3
"""
APEX-PARLAYPLAY-2026-10-15 (owner, 2026-10-15: "keep PrizePicks
PrizePicks but make ParlayPlay ParlayPlay ... same setup", chose "own browser").

A separate ParlayPlay side. Nothing here reads or writes any PrizePicks file:
no pick log, no forward log, no board snapshot, no PrizePicks channel.

  read   every 30 min a real Chrome window (off-screen, its own profile in
         data/parlayplay/chrome_profile, no login) opens parlayplay.io and reads
         the same offering JSON the page itself loads. headless Chrome is
         stopped by Cloudflare; a real window is not. If a bot check ever shows,
         the read stops and says so -- it never tries to get past it.
  score  the same engine the PrizePicks side uses (bet365 price -> simulator)
         gives P(More) / P(Less) for every line; Honest Probability from the
         same calibration fit.
  rank   ParlayPlay pays its own multiplier on each side of each line, so the
         board ranks by value: (lower of Win Prob and Honest Prob) x multiplier
         - 1 (> 0 means the pick pays more than its risk), sides 55%+ only. One pick per match (ParlayPlay
         allows one player per tennis match in an entry).
  store  data/parlayplay/parlayplay_db.sqlite: `lines` (current line, both
         multipliers, probabilities, value) and `changes` (multiplier/line history).

CLI: python3 apex_parlayplay.py run | board | fetch
"""
import csv, gzip, json, os, sqlite3, sys, time
from datetime import datetime, timezone, timedelta

# APEX-PARLAYPLAY-HOME-2026-10-15 (owner: "complete separate from
# PrizePicks files, data, everything ... Apex ParlayPlay ... all data collected
# and saved"). Everything ParlayPlay lives in ~/openclaw/ApexParlayPlay/. The
# shared APEX engine (price feed, simulator, calibration, category records) is
# only READ from ~/openclaw; nothing here writes a PrizePicks file.
APP = os.path.dirname(os.path.abspath(__file__))          # ~/openclaw/ApexParlayPlay
HERE = os.path.dirname(APP)                                # ~/openclaw (engine, read-only)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
PP_DIR = os.path.join(APP, "data")
RAW_DIR = os.path.join(PP_DIR, "raw")                       # every read, full offering, gzipped
FIRSTSEEN_LOG = os.path.join(PP_DIR, "parlayplay_firstseen_log.csv")
DB_PATH = os.path.join(PP_DIR, "parlayplay_db.sqlite")
PROFILE = os.path.join(PP_DIR, "chrome_profile")
CONFIG = os.path.join(PP_DIR, "config.json")        # Discord webhooks for the ParlayPlay channels
STATE = os.path.join(PP_DIR, "state.json")          # last posted board, last read status
URL = "https://parlayplay.io/"
OFFERING = "crossgame/offering"
CT = timezone(timedelta(hours=-5))

STAT_MAP = {"ten_totalGames": "total_games", "ten_gamesWon": "games_won", "ten_setsWon": "sets_won"}
MATCH_LEVEL = {"total_games"}
MIN_LEAD_MIN = 5
BOARD_SIZE = 10
# The calibration fit (Honest Probability) was learned on 50-90% picks; on a
# long shot it pulls the number toward 50% and invents value (a 4% Win Prob
# Total Games 33.5 at 14.2x showed +55%). So value uses the LOWER of Win Prob
# and Honest Prob, and the board only takes sides the engine makes 55%+.
MIN_PROB = 0.55


def log(msg):
    print(f"[{datetime.now(CT).strftime('%I:%M %p').lstrip('0')} CT] [ParlayPlay] {msg}", flush=True)


def _load_json(p, default):
    try:
        with open(p) as fh:
            return json.load(fh)
    except Exception:
        return default


def _save_json(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, p)


# ------------------------------------------------------------------ read
class BotCheck(Exception):
    pass


def fetch_offering(timeout_s=60):
    """The full ParlayPlay offering JSON, read through a real Chrome window."""
    from playwright.sync_api import sync_playwright
    os.makedirs(PROFILE, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, channel="chrome", headless=False,
            args=["--window-position=-3000,-3000", "--window-size=900,700"])
        try:
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            reqs = []
            pg.on("request", lambda r: reqs.append(r) if OFFERING in r.url else None)
            pg.goto(URL, timeout=timeout_s * 1000)
            for _ in range(timeout_s):
                if reqs:
                    break
                pg.wait_for_timeout(1000)
            if not reqs:
                title = pg.title()
                if "moment" in title.lower() or "security" in pg.inner_text("body")[:400].lower():
                    raise BotCheck(f"ParlayPlay showed a bot check ({title!r})")
                raise RuntimeError(f"page loaded ({title!r}) but never asked for the offering")
            r = reqs[0]
            hdrs = r.all_headers()
            keep = {k: v for k, v in hdrs.items()
                    if k.lower() in ("x-csrftoken", "x-parlay-request", "x-requested-with", "accept",
                                     "x-parlayplay-platform", "x-parlayplay-native-platform")}
            txt = pg.evaluate("""async ([u, h]) => {
                const r = await fetch(u, {headers: h, credentials: 'include'});
                return [r.status, await r.text()]; }""", [r.url, keep])
            status, body = txt
            if status != 200:
                raise RuntimeError(f"offering returned HTTP {status}")
            return json.loads(body)
        finally:
            ctx.close()


def parse_tennis(raw, now=None):
    """One row per (match, category, player, line) with both multipliers."""
    now = now or datetime.now(timezone.utc)
    rows, seen = [], set()
    for pl in (raw or {}).get("players") or []:
        m = pl.get("match") or {}
        lg = ((m.get("league") or {}).get("leagueNameShort") or "")
        if lg not in ("ATP", "WTA"):
            continue
        try:
            st = datetime.fromisoformat(str(m.get("matchDate"))).astimezone(timezone.utc)
        except Exception:
            continue
        if m.get("matchStatus") not in (0, None) or (st - now).total_seconds() < MIN_LEAD_MIN * 60:
            continue
        home = (m.get("homeTeam") or {}).get("teamname") or ""
        away = (m.get("awayTeam") or {}).get("teamname") or ""
        who = (pl.get("player") or {}).get("fullName") or ""
        if "/" in home or "/" in away or not who:
            continue
        for s in pl.get("stats") or []:
            cat = STAT_MAP.get(s.get("challengeOption"))
            if not cat:
                continue
            subject = "match" if cat in MATCH_LEVEL else who
            for v in ((s.get("altLines") or {}).get("values") or []):
                try:
                    line = float(v.get("selectionPoints"))
                    mo = float(v.get("decimalPriceOver") or 0)
                    ml = float(v.get("decimalPriceUnder") or 0)
                except (TypeError, ValueError):
                    continue
                if mo <= 1.0 and ml <= 1.0:
                    continue
                k = (m.get("id"), cat, subject, line)
                if k in seen:
                    continue
                seen.add(k)
                opp = away if who == home else home
                rows.append({
                    "key": f"{m.get('id')}|{cat}|{subject}|{line}",
                    "match_id": str(m.get("id")), "league": lg, "home": home, "away": away,
                    "player": subject, "opponent": opp if subject != "match" else "",
                    "start_utc": st.isoformat(), "best_of": 5 if m.get("matchType") == "best_of_5" else 3,
                    "category": cat, "line": line, "mult_more": mo if mo > 1.0 else 0.0,
                    "mult_less": ml if ml > 1.0 else 0.0, "is_main": 1 if v.get("isMainLine") else 0,
                    "boosted": 1 if s.get("isBoostedPayout") else 0})
    return rows


# ------------------------------------------------------------------ score
def _honest(p):
    try:
        import apex_edge_tracker as aet
        h = aet.corrected_prob(p)
        return float(h) if h is not None else None
    except Exception:
        return None


def _same(a, b):
    a, b = str(a or "").lower().split(), str(b or "").lower().split()
    return bool(a) and bool(b) and a[-1] == b[-1]


def score(rows):
    """Adds prob_more/prob_less, honest_*, value_* to each row. Never raises."""
    import apex_price_feed, apex_prop_engine
    try:
        import apex_surface_map as asm
    except Exception:
        asm = None
    by_match = {}
    for r in rows:
        by_match.setdefault(r["match_id"], []).append(r)
    for mid, rs in by_match.items():
        r0 = rs[0]
        try:
            pf = apex_price_feed.get_price(r0["home"], r0["away"])
        except Exception as e:
            pf = None
            log(f"price failed {r0['home']} v {r0['away']}: {type(e).__name__}: {e}")
        if not pf or not pf.get("ok"):
            try:   # APEX-PPMIRROR-A-2026-10-23: the price feed's own decline reason, for DATA CHECK
                why = apex_price_feed.last_reason() or ""
            except Exception:
                why = ""
            for r in rs:
                r["score_note"] = "no bet365 price yet" + ((" [%s]" % why[:160]) if why else "")
            continue
        bl = bop = None
        try:   # APEX-PPMIRROR-B-2026-10-23: bet365's main total from the same (cached) pre-match odds
            import apex_book_lines as _abl
            ml = _abl._main_line(apex_price_feed._prematch(pf.get("event_id"), True) or {})
            if ml:
                bl, o1, o2 = ml
                bop = round((1 / o1) / ((1 / o1) + (1 / o2)), 4)
        except Exception:
            pass
        surf, indoor = "Hard", None
        if asm:
            try:
                surf, indoor, _ = asm.lookup(pf.get("sport_title") or "")
            except Exception:
                pass
        for r in rs:
            key = ("match|" if r["category"] in MATCH_LEVEL else r["player"] + "|") + r["category"]
            try:
                res = apex_prop_engine.evaluate_match(
                    pf["fav"], pf["dog"], float(pf["pinn_fav"]), surface=surf, bo5=r["best_of"] == 5,
                    wta=r["league"] == "WTA", lines={key: r["line"]}, indoor=indoor)
            except Exception as e:
                r["score_note"] = f"engine error {type(e).__name__}"
                continue
            tgt = None
            for p in (res or {}).get("props") or []:
                if p.get("category") != r["category"] or p.get("line") is None:
                    continue
                if r["category"] in MATCH_LEVEL or _same(p.get("player"), r["player"]):
                    tgt = p
                    break
            if not tgt:
                r["score_note"] = "engine has no row for this stat"
                continue
            po, pu = float(tgt.get("p_over") or 0), float(tgt.get("p_under") or 0)
            if po + pu <= 0:
                continue
            pm, pl = po / (po + pu), pu / (po + pu)
            big = max(pm, pl)
            hb = _honest(big)
            hm = (hb if pm >= pl else 1 - hb) if hb is not None else pm
            r.update({"prob_more": round(pm, 4), "prob_less": round(pl, 4),
                      "honest_more": round(hm, 4), "honest_less": round(1 - hm, 4),
                      "surface": surf, "price_source": f"{pf.get('book')}/{pf.get('match_method')}",
                      "tournament": pf.get("sport_title") or "", "score_note": "",
                      "pinn_fav": pf.get("pinn_fav"), "bet365_line": bl, "bet365_over_prob": bop,
                      "proj_mean": round(float(tgt["mean"]), 2) if tgt.get("mean") is not None else None})
            r["value_more"] = round(min(pm, hm) * r["mult_more"] - 1, 4) if r["mult_more"] else None
            r["value_less"] = round(min(pl, 1 - hm) * r["mult_less"] - 1, 4) if r["mult_less"] else None
    return rows


# ------------------------------------------------------------------ store
COLS = ["key", "match_id", "league", "home", "away", "player", "opponent", "start_utc", "best_of",
        "category", "line", "mult_more", "mult_less", "is_main", "boosted", "prob_more", "prob_less",
        "honest_more", "honest_less", "value_more", "value_less", "surface", "tournament",
        "price_source", "score_note", "first_seen", "last_seen", "scored_ts",
        "proj_mean",   # APEX-ALLBOARDPROJ-2026-10-20: engine's projected mean (display only)
        "pinn_fav", "bet365_line", "bet365_over_prob"]   # APEX-PPMIRROR-B-2026-10-23: ⚖ favorite + 📏 bet365 total
RESULT_COLS = ["actual", "score", "result", "more_won", "less_won", "settled_ts", "settle_note", "settle_tries"]


def _db():
    os.makedirs(PP_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(f"CREATE TABLE IF NOT EXISTS lines ({', '.join(k + (' TEXT PRIMARY KEY' if k == 'key' else '') for k in COLS)})")
    c.execute("CREATE TABLE IF NOT EXISTS changes (key TEXT, ts TEXT, field TEXT, old TEXT, new TEXT)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_pp_start ON lines(start_utc)")
    have = {r[1] for r in c.execute("PRAGMA table_info(lines)")}
    for col in RESULT_COLS + ["proj_mean", "pinn_fav", "bet365_line", "bet365_over_prob"]:
        if col not in have:
            c.execute(f"ALTER TABLE lines ADD COLUMN {col}")
    c.execute("CREATE TABLE IF NOT EXISTS reads (ts TEXT, ok INTEGER, tennis_lines INTEGER, new INTEGER, "
              "changed INTEGER, raw_file TEXT, note TEXT)")
    # APEX-PP-SLIPTRACK-2026-10-15: every suggested slip, frozen as posted, then graded.
    c.execute("CREATE TABLE IF NOT EXISTS slips (sig TEXT PRIMARY KEY, posted_ts TEXT, kind TEXT, n INTEGER, "
              "tier TEXT, payout REAL, win REAL, honest REAL, last_start TEXT, result TEXT, legs_hit INTEGER, "
              "legs_void INTEGER, graded_ts TEXT, note TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS slip_legs (sig TEXT, key TEXT, direction TEXT, mult REAL, win REAL, "
              "honest REAL, start_utc TEXT, label TEXT, result TEXT)")
    return c


def store(rows):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    c = _db()
    new = changed = 0
    new_rows = []
    kept = 0
    for r in rows:
        old = c.execute("SELECT mult_more, mult_less, first_seen FROM lines WHERE key=?", (r["key"],)).fetchone()
        r["last_seen"] = ts
        r["scored_ts"] = ts if r.get("prob_more") is not None else None
        if r.get("prob_more") is None and old is not None and KEEP_LAST_GOOD_SCORE:
            kept += _keep_last_score(c, r)   # APEX-PPMIRROR-A-2026-10-23
        if old is None:
            r["first_seen"] = ts
            new += 1
            new_rows.append(r)
        else:
            r["first_seen"] = old[2]
            for f, o in (("mult_more", old[0]), ("mult_less", old[1])):
                if o is not None and abs(float(o) - float(r[f])) > 1e-9:
                    c.execute("INSERT INTO changes VALUES (?,?,?,?,?)", (r["key"], ts, f, str(o), str(r[f])))
                    changed += 1
        c.execute(f"INSERT OR REPLACE INTO lines ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})",
                  [r.get(k) for k in COLS])
    c.commit()
    c.close()
    _log_firstseen(new_rows)
    store.last_new = new_rows
    store.last_kept = kept
    return new, changed, ts


# APEX-PPMIRROR-A-2026-10-23 (owner, "Go 1-8"): on 10/22 SportsAPI365 stopped answering
# and store() wrote empty scores over 364 saved win chances, so an empty board posted. A line
# that could not be re-scored now keeps its last good score (same as PrizePicks carry-forward);
# its score note says so and the DATA CHECK counts it. Undo: KEEP_LAST_GOOD_SCORE = False.
KEEP_LAST_GOOD_SCORE = True
SCORE_COLS = ["prob_more", "prob_less", "honest_more", "honest_less", "surface", "tournament",
              "price_source", "proj_mean", "pinn_fav", "bet365_line", "bet365_over_prob", "scored_ts"]


def _keep_last_score(c, r):
    """Copies the stored score onto an unscored row. 1 if kept, else 0."""
    o = c.execute("SELECT %s FROM lines WHERE key=?" % ", ".join(SCORE_COLS), (r["key"],)).fetchone()
    if not o or o[0] is None:
        return 0
    why = r.get("score_note") or "not re-scored"
    r.update(dict(zip(SCORE_COLS, o)))
    hm = float(r["honest_more"]) if r.get("honest_more") is not None else float(r["prob_more"])
    r["value_more"] = round(min(float(r["prob_more"]), hm) * r["mult_more"] - 1, 4) if r["mult_more"] else None
    r["value_less"] = round(min(float(r["prob_less"]), 1 - hm) * r["mult_less"] - 1, 4) if r["mult_less"] else None
    r["score_note"] = ("kept last good score from %s CT (%s)" % (
        datetime.fromisoformat(o[-1]).astimezone(CT).strftime("%b %d %I:%M %p") if o[-1] else "earlier", why))[:300]
    return 1


def _log_firstseen(rows):
    if not rows:
        return
    first = not os.path.exists(FIRSTSEEN_LOG)
    with open(FIRSTSEEN_LOG, "a", newline="") as fh:
        w = csv.writer(fh)
        if first:
            w.writerow(["first_seen_ct", "start_ct", "league", "match", "player", "category", "line",
                        "mult_more", "mult_less", "key"])
        for r in rows:
            w.writerow([datetime.now(CT).strftime("%Y-%m-%dT%H:%M"),
                        datetime.fromisoformat(r["start_utc"]).astimezone(CT).strftime("%Y-%m-%dT%H:%M"),
                        r["league"], f"{r['home']} v {r['away']}", r["player"], r["category"], r["line"],
                        r["mult_more"], r["mult_less"], r["key"]])


def save_raw(raw):
    """Every read's full ParlayPlay offering (all sports), gzipped, by day."""
    now = datetime.now(CT)
    d = os.path.join(RAW_DIR, now.strftime("%Y-%m-%d"))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, now.strftime("%H%M%S") + "_offering.json.gz")
    with gzip.open(path, "wt") as fh:
        json.dump(raw, fh)
    return path


# ------------------------------------------------------------------ settle
SETTLE_AFTER_H = 3
GIVE_UP_DAYS = 4
# APEX-PP-INPLAY-2026-10-22 (owner: "Go" on the in-play grader fix): a match
# that is still in play (or not started) is NOT a void -- the line stays waiting
# and is checked again on the next pass. Only after this many hours with no final
# score is it voided (postponed / abandoned).
NOT_FINISHED_VOID_AFTER_H = 48


def settle(limit_matches=30, days=GIVE_UP_DAYS):
    """Grade finished ParlayPlay lines from SportsAPI365 scores (same source the
    PrizePicks props DB uses) and store actual / result in this DB only."""
    sys_path_ok = HERE in sys.path
    import apex_settle_sa365 as sa
    c = _db()
    now = datetime.now(timezone.utc)
    due = c.execute("""SELECT key, match_id, home, away, player, category, line, start_utc, best_of
                       FROM lines WHERE settled_ts IS NULL AND start_utc <= ? AND start_utc >= ?""",
                    ((now - timedelta(hours=SETTLE_AFTER_H)).isoformat(),
                     (now - timedelta(days=days)).isoformat())).fetchall()
    by_match = {}
    for row in due:
        by_match.setdefault(row[1], []).append(row)
    settled = held = 0
    ts = now.isoformat(timespec="seconds")
    for mid, rs in list(by_match.items())[:limit_matches]:
        _, _, home, away, _, _, _, st, bo = rs[0]
        d = datetime.fromisoformat(st).astimezone(CT).date().isoformat()
        try:
            ev, side0, note = sa.find_event(home, away, d)
        except Exception as e:
            ev, note = None, f"{type(e).__name__}: {e}"
        for key, _, _, _, player, cat, line, _, _ in rs:
            if ev is None:
                c.execute("UPDATE lines SET settle_tries=COALESCE(settle_tries,0)+1, settle_note=? WHERE key=?",
                          ("no result yet: " + str(note)[:120], key))
                held += 1
                continue
            who = home if player == "match" else player
            side = side0 if sa.same_player(who, ev.get("participant%d" % side0)) else 3 - side0
            if cat == "sets_won":
                flag = sa.match_flag(ev, str(bo or 3))
                if flag:
                    val, why = None, "VOID " + flag
                else:
                    sets = sa.parse_sets(ev.get("score"))
                    s1 = sum(a > b for a, b in sets)
                    val, why = float(s1 if side == 1 else len(sets) - s1), ""
            else:
                v, flag, note2 = sa.value_for(cat, ev, side, str(bo or 3))
                val, why = v, (("VOID " + flag) if flag else note2)
            if str(why).startswith("VOID NOT_FINISHED") and \
                    (now - datetime.fromisoformat(st)).total_seconds() < NOT_FINISHED_VOID_AFTER_H * 3600:
                c.execute("UPDATE lines SET settle_tries=COALESCE(settle_tries,0)+1, settle_note=?, score=? WHERE key=?",
                          ("still playing, check again: " + str(why)[5:], ev.get("score"), key))
                held += 1          # APEX-PP-INPLAY-2026-10-22
                continue
            if val is None and not str(why).startswith("VOID"):
                c.execute("UPDATE lines SET settle_tries=COALESCE(settle_tries,0)+1, settle_note=?, score=? WHERE key=?",
                          (str(why)[:150], ev.get("score"), key))
                held += 1
                continue
            if val is None:
                res, mw, lw = "void", None, None
            else:
                res = "push" if val == line else ("more" if val > line else "less")
                mw, lw = int(val > line), int(val < line)
            c.execute("UPDATE lines SET actual=?, score=?, result=?, more_won=?, less_won=?, settled_ts=?, "
                      "settle_note=? WHERE key=?", (val, ev.get("score"), res, mw, lw, ts, why or "", key))
            settled += 1
    c.commit()
    c.close()
    return f"graded {settled}, {held} waiting"


def current(ts=None):
    """Lines seen in the newest read whose match hasn't started."""
    c = _db()
    c.row_factory = sqlite3.Row
    ts = ts or (c.execute("SELECT MAX(last_seen) FROM lines").fetchone()[0])
    now = datetime.now(timezone.utc).isoformat()
    rs = [dict(r) for r in c.execute("SELECT * FROM lines WHERE last_seen=? AND start_utc>?", (ts, now))]
    c.close()
    return rs


# ------------------------------------------------------------------ board
# APEX-PARLAYPLAY-PROB-2026-10-15 (owner: "the same thing we do for
# PrizePicks ... get the line, compare the line and choose the correct side").
# Each prop is evaluated like a PrizePicks prop: the engine picks the side it
# makes more likely, and boards rank by Win Probability with the same 70% bar.
# The multiplier ParlayPlay pays on that side is shown on the card.
BOARD_MIN_PROB = 0.70


def evaluated(rows):
    """Every scored prop with the engine's side, Win Probability first."""
    out = []
    for r in rows:
        if r.get("prob_more") is None:
            continue
        side = "more" if r["prob_more"] >= r["prob_less"] else "less"
        m = r.get(f"mult_{side}")
        out.append(dict(r, direction=side.upper(), mult=float(m or 0),
                        prob=float(r[f"prob_{side}"]), honest=float(r[f"honest_{side}"]),
                        playable=bool(m)))
    out.sort(key=lambda x: -x["prob"])
    return out


def picks(rows):
    """Playable evaluated props (ParlayPlay offers the engine's side)."""
    return [p for p in evaluated(rows) if p["playable"]]


APEX_MIN = 67


def _pp_pick(p):
    """A ParlayPlay prop in the shape the PrizePicks board cards read."""
    match = p["player"] == "match"
    return {"player": "match" if match else p["player"],
            "opponent": f"{p['home']} v {p['away']}" if match else p["opponent"],
            "category": p["category"], "line": p["line"], "direction": p["direction"],
            "prob": p["prob"], "prob_cal": p["prob"], "bo5": p["best_of"] == 5,
            "match_date": p["start_utc"][:10], "prop_type": "parlayplay",
            # APEX-PPMIRROR-B-2026-10-23: what the shared card / rank / hold rules read
            "match_id": "pp:%s" % p["match_id"], "start_time_raw": p["start_utc"], "slip_start_utc": p["start_utc"],
            "pinn_fav": p.get("pinn_fav"), "bet365_total": _book_total(p),
            "price_source": p.get("price_source") or "", "mult": p.get("mult"),
            # APEX-ALLBOARDPROJ-2026-10-20: edge = projection - line, as on PrizePicks picks
            "edge": (float(p["proj_mean"]) - float(p["line"])) if p.get("proj_mean") is not None else None}


def scored(rows):
    """Every playable ParlayPlay prop through the same checks and scores the
    PrizePicks boards use: category evidence + probability gate, Apex Score,
    Confidence, Rank Score. Win Probability first."""
    import apex_edge_tracker as t
    rec = t.records()
    out = []
    for p in picks(rows):
        q = _pp_pick(p)
        if not t._category_has_evidence(q["category"], rec):
            continue
        if not t._category_probability_gate(q["category"], q["prob"]):
            continue
        sc, ev, lvl, up, down = t.apex_score(q["prob"], q["category"], q["bo5"], rec)
        cf, _tier, cwhy = t.apex_confidence(q["category"], q["bo5"], rec, score=sc, prob=q["prob"],
                                            evidence=ev, attention=lvl, warnings=down, card_present=False)
        out.append({"p": q, "pp": p, "sc": sc, "score": sc, "ev": ev, "lvl": lvl, "up": up, "down": down,
                    "conf": cf, "cwhy": cwhy, "rank": t.rank_score(sc, cf)})
    out.sort(key=lambda d: -d["p"]["prob"])
    return out, rec


class Board(list):
    """The numbered board picks, plus .backups (BACKUPS rows) and .held (rule holds)."""
    backups = ()
    held = ()


# APEX-PPMIRROR-B-2026-10-23 (owner, "Go 1-8", steps 3-5): the ParlayPlay board runs the
# PrizePicks board rules by CALLING them (apex_edge_tracker / apex_blowout_signal / apex_card_signals),
# so a future PrizePicks rule change reaches ParlayPlay by itself:
#   qualify  _board_qualifies (70%+ Win Probability, Apex 67+)
#   holds    low_cushion_reason (TG MORE under 3 games of room under bet365) and board_hold_reason
#            (the 17.5 check says skip -- on ParlayPlay every TG MORE line of 17.5 or more is checked);
#            a held pick goes to BACKUPS with the reason and its match's next-best prop moves up
#            (_low_cushion_split, HOLD_SLOT_MOVES_UP)
#   rank     _rerank_after_top3 -> board_rank_score: Win Prob + 5 per net signal point, minus the
#            17.5 push-down (5 points when the check has no data)
#   one per  match AND player (_prop_names)
# Fantasy / Double Faults / Breaks holds need markets ParlayPlay doesn't offer, so they never fire.
# APEX-NOBOOK-2026-10-24: the no-book-total hold reaches ParlayPlay through board_hold_reason (a pick
# with ParlayPlay's own stored bet365 total is not held).
# Undo: PP_BOARD_RULES = False (plain Win Probability, one per match, as before 10/23).
PP_BOARD_RULES = True
# APEX-FIXCHECKER-PP-2026-11-02 (owner, "Go fix checker"; 11/2 9:58 AM the new checker caught Vandromme v Okamura
# TG 16.5 at #2 -- Jeline Vandromme has 1 match on file): lower-level players (under 5 matches on file) are off every
# board, ParlayPlay too, numbered spots and BACKUPS alike (apex_player_checks.disqualified). Undo: PP_THIN_OFF = False.
PP_THIN_OFF = True


def _hold_why(d):
    import apex_edge_tracker as t
    p = d.get("p") or {}
    try:
        import apex_blowout_signal as bs
        lc = bs.low_cushion_reason(p) if t.LOW_CUSHION_BACKUPS else None
    except Exception:
        lc = None
    try:   # APEX-BANLIST-2026-10-28
        import apex_ban_list as _abl
        _bn = _abl.reason(p)
        if _bn:
            return _bn
    except Exception:
        pass
    return lc or t.board_hold_reason(p)


def board(rows, size=BOARD_SIZE, ds=None):
    """The ParlayPlay board: same bar, holds, rank and one-per-match/player as PrizePicks."""
    import apex_edge_tracker as t
    if ds is None:
        ds, _ = scored(rows)
    if not PP_BOARD_RULES:
        out, used = Board(), set()
        for d in ds:
            if d["p"]["prob"] < BOARD_MIN_PROB or d["sc"] < APEX_MIN or d["pp"]["match_id"] in used:
                continue
            used.add(d["pp"]["match_id"])
            out.append(d)
            if len(out) >= size:
                break
        return out
    if PP_THIN_OFF:   # APEX-FIXCHECKER-PP-2026-11-02
        try:
            import apex_player_checks as _pc
            ds = [d for d in ds if not _pc.disqualified(d.get("p") or {})]
        except Exception as e:
            log(f"thin filter skipped: {type(e).__name__}: {e}")
    qual = [d for d in ds if t._board_qualifies(d)]
    # slot winners by board rank (APEX-BESTLINE-2026-10-25; Win Probability when t.BEST_RANK_PER_MATCH
    # is off), one per match/player; the rest of the slot in d["more"]
    slots = []
    for d in sorted(qual, key=t._slot_key):
        nm = t._prop_names(d["p"])
        hit = next((x for x in slots if x[0] & nm), None)
        if hit:
            hit[1]["more"].append(d)
            hit[0] |= nm
        else:
            slots.append([set(nm), dict(d, more=[])])
    win_order = [x[1] for x in slots]
    orig_rank = {id(d): i for i, d in enumerate(win_order, 1)}
    keep, held = t._low_cushion_split(win_order, orig_rank)
    pool = [d for d in qual if not _hold_why(d)]
    res = {"scored": [dict(d) for d in keep[:size]]}
    if t.RERANK_AFTER_TOP3 and res["scored"]:
        t._rerank_after_top3(res, lambda: {"scored": pool})
    out = Board(d for d in res["scored"] if not _hold_why(d))
    for d in out:
        d.pop("more", None)
    out.held = held
    out.ds = ds
    try:
        out.backups = backup_rows(ds, out)
    except Exception as e:
        log(f"backups failed: {type(e).__name__}: {e}")
        out.backups = []
    return out


def backup_rows(ds, shown):
    """APEX-PPMIRROR-B-2026-10-23: the PrizePicks BACKUPS list for ParlayPlay -- every prop that
    passed 70% / Apex 67 (plus 70%+ with Apex 60-66, marked ⚠) but has no numbered spot, with why:
    held back (rule + reason) / same match or player as #k / board full. ParlayPlay offers ~20 lines
    per match, so lines of one match with the same reason are one row ("also 16.5, 18.5").
    Returns [(prob, sub67, why, d, also_lines)] in BACKUPS order."""
    import apex_edge_tracker as t
    on = {(d["pp"]["key"], d["pp"]["direction"]) for d in shown}
    posted = [(i, t._prop_names(d["p"])) for i, d in enumerate(shown, 1)]
    groups = {}
    for d in ds:
        if (d["pp"]["key"], d["pp"]["direction"]) in on:
            continue
        pr = t._board_prob(d)
        sub67 = (pr >= t.BOARD_MIN_PROB - 1e-9 and t.BACKUP_SUB67_MIN_SCORE is not None
                 and t.BACKUP_SUB67_MIN_SCORE <= d["sc"] < t.BOARD_MIN_SCORE)
        if not t._board_qualifies(d) and not sub67:
            continue
        hw = _hold_why(d)
        hit = next((i for i, nm in posted if nm & t._prop_names(d["p"])), None)
        why = ("held back: " + hw) if hw else (("same match/player as #%d" % hit) if hit else "board full")
        k = (d["pp"]["match_id"], d["pp"]["category"], d["pp"]["player"], d["pp"]["direction"], why, sub67)
        groups.setdefault(k, []).append(d)
    rows = []
    _bo = getattr(t, "BACKUP_BOARD_ORDER", False)   # APEX-BACKUPORDER-2026-10-24: the board's own rank order
    for (_m, _c, _pl, _dr, why, sub67), g in groups.items():
        g.sort(key=(lambda x: (-t.board_rank_of("pp", x["p"]), t._bb_sortkey(x))) if _bo else t._bb_sortkey)
        rows.append((t._board_prob(g[0]), sub67, why, g[0], sorted(x["pp"]["line"] for x in g[1:])))
    if _bo:   # held for a rule / under 67 last, unnumbered
        rows.sort(key=lambda r: t.backup_sort_key("pp", r[0], r[3]["p"], r[2], r[1]))
    else:
        rows.sort(key=lambda r: (r[1], -r[0]))
    return rows


def _backup_nos(rows):
    """APEX-BACKUPORDER-2026-10-24: BACKUP number per row (None = held, listed last)."""
    import apex_edge_tracker as t
    out, n = [], 0
    for r in rows:
        if t.backup_is_held(r[2], r[1]):
            out.append(None)
        else:
            n += 1
            out.append(n)
    return out


def backup_summary(b):
    """'🔁 Backups in order: 1) X (85) · 2) ...' for the top of the ParlayPlay board, or None."""
    try:
        import apex_edge_tracker as t
        rows = list(getattr(b, "backups", None) or [])
        if not getattr(t, "BACKUP_BOARD_ORDER", False) or not rows:
            return None
        num = [r for r, k in zip(rows, _backup_nos(rows)) if k][:t.BACKUP_SUMMARY_COUNT]
        if not num:
            return None
        return "\U0001F501 Backups in order: " + " \u00B7 ".join(
            "%d) %s%s (%s%s)" % (i, t.no_book_mark(r[3]["p"]), t.backup_short_label(dict(r[3]["p"], line=r[3]["pp"]["line"])),
                                 "Ranking # " if getattr(t, "SCORE_DISPLAY", False) else "",   # APEX-SCOREDISPLAY-2026-10-25
                                 t._score_fmt(t.score_value("pp", r[3]["p"]))) for i, r in enumerate(num, 1))
    except Exception as e:
        log(f"backup summary failed: {type(e).__name__}: {e}")
        return None


def backup_lines(b, rec=None):
    """The 🔁 BACKUPS block under the ParlayPlay board (same layout as PrizePicks). Never raises."""
    try:
        import apex_edge_tracker as t
        rows = list(getattr(b, "backups", None) or [])
        if not rows or not t.BOARD_BACKUPS_LIMIT:
            return []
        rec = rec or t.records()
        n67 = sum(1 for r in rows if r[1])
        out = ["\u2500" * 30, "\U0001F501 **BACKUPS** -- passed 70%% and Apex %d but not posted here (%d)%s" % (
            t.BOARD_MIN_SCORE, len(rows) - n67,
            (" + %d at 70%%+ with Apex %d-%d, marked \u26A0" % (n67, t.BACKUP_SUB67_MIN_SCORE, t.BOARD_MIN_SCORE - 1)) if n67 else "")]
        full = rows[:t.BOARD_BACKUP_FULL_CARDS]
        # APEX-BACKUPORDER-2026-10-24: numbered in board rank order, held last; rank + gap to the last spot
        _bo = getattr(t, "BACKUP_BOARD_ORDER", False)
        nos = _backup_nos(rows) if _bo else list(range(1, len(rows) + 1))
        last = t.board_rank_of("pp", b[-1]["p"]) if (_bo and len(b)) else None

        def _tag(k, d):
            rk = t.board_rank_of("pp", d["p"])
            if not _bo:
                return "BACKUP %d" % nos[k], ""
            return ("HELD", t.score_text("pp", d["p"]) if getattr(t, "SCORE_DISPLAY", False) else "rank %d" % round(100 * rk)) \
                if nos[k] is None else ("BACKUP %d" % nos[k], t.backup_rank_text(rk, last, len(b), "pp", d["p"]))   # APEX-SCOREDISPLAY-2026-10-25
        out[1] += " -- numbered in the order this board ranks them; held picks listed last" if _bo else ""
        for k, (pr, sub67, why, d, also) in enumerate(full):
            i = nos[k] or 0
            try:
                c = card_lines(d, i, rec)
                who, _cat = _label(d["pp"])
                if len(c) > 1:
                    lab, rt = _tag(k, d)
                    c[1] = "\U0001F501 %s \u00b7 %s%s%s \u00b7 _%s_" % (lab, t.no_book_mark(d["p"]), who,   # APEX-NOBOOK-2026-10-24
                                                                       (" \u00b7 " + rt) if rt else "", why)
                    if sub67:
                        c.insert(2, "\u26A0 Apex %d, under the %d bar" % (int(d["sc"]), t.BOARD_MIN_SCORE))
                    if also:
                        c.insert(2, "\u2795 Same reason, other lines: %s" % ", ".join("%g" % x for x in also))
                out += c
            except Exception:
                out.append(_backup_one(pr, sub67, why, d, also))
        if len(rows) > len(full):
            out.append("\u2500" * 30)
        for k in range(len(full), min(len(rows), t.BOARD_BACKUPS_LIMIT)):
            ln = _backup_one(*rows[k])
            if _bo:   # '🔁 BACKUP 7 · <prop> ... · Apex 71 · rank 71, 15 points behind #6 -- board full'
                lab, rt = _tag(k, rows[k][3])
                head, sep, tail = ln[len("\U0001F501 "):].partition(" -- ")
                ln = "\U0001F501 %s \u00b7 %s \u00b7 %s%s%s" % (lab, head, rt, sep, tail)
            out.append(ln)
        if len(rows) > t.BOARD_BACKUPS_LIMIT:
            out.append("(+%d more backups -- see /pp-allprops)" % (len(rows) - t.BOARD_BACKUPS_LIMIT))
        return out
    except Exception as e:
        log(f"backup lines failed: {type(e).__name__}: {e}")
        return []


def _backup_one(pr, sub67, why, d, also):
    import apex_edge_tracker as t
    who, _ = _label(d["pp"])
    return "\U0001F501 %s%s %s %g %s (ParlayPlay %.2fx) \u00b7 Win %.1f%% \u00b7 Honest %.1f%% \u00b7 Apex %d -- %s%s%s" % (
        t.no_book_mark(d["p"]), who,   # APEX-NOBOOK-2026-10-24
        t.LABEL.get(d["pp"]["category"], d["pp"]["category"]), d["pp"]["line"], d["pp"]["direction"],
        d["pp"]["mult"], 100 * pr, 100 * d["pp"]["honest"], int(d["sc"]), why,
        (" \u00b7 also %s" % ", ".join("%g" % x for x in also)) if also else "",
        (" \u00b7 \u26A0 Apex %d, under the %d bar" % (int(d["sc"]), t.BOARD_MIN_SCORE)) if sub67 else "")


def _seen_line(pp):
    try:
        ls = datetime.fromisoformat(pp["last_seen"])
        fs = datetime.fromisoformat(pp["first_seen"])
        h = (datetime.now(timezone.utc) - ls).total_seconds() / 3600
        age = ("%.1fh ago" % h) if h >= 0.05 else "just now"
        icon = "\U0001F7E2" if h < 1 else "\U0001F7E1"
        return "%s Freshness: last confirmed on ParlayPlay %s (first seen %s CT)" % (
            icon, age, fs.astimezone(CT).strftime("%b %d %I:%M %p"))
    except Exception:
        return "\u26aa Freshness: unknown"


def card_lines(d, i, rec, score_dec=0):
    """The PrizePicks board card, with ParlayPlay's payout and freshness."""
    import apex_edge_tracker as t
    medals = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}
    L = t._render_pick_card(d, i, rec, {}, {}, medals, worst=False, show_debug=False, flag="pp", score_dec=score_dec)
    pp = d["pp"]
    st = datetime.fromisoformat(pp["start_utc"]).astimezone(CT).strftime("%a %b %d %I:%M %p")
    out = []
    extra = []
    if d.get("pulled_new_line"):   # APEX-PPMIRROR-C-2026-10-23: PrizePicks NEW LINE wording
        nl = d["pulled_new_line"]
        extra.append("\U0001F501 NEW LINE: ParlayPlay moved this from %g to %g after the last board (was %d%%, now "
                     "%d%% at the new line). Same player moved lines have hit about 7 of 10." % (
                         nl["old_line"], nl["line"], round(100 * nl["old_prob"]), round(100 * nl["prob"])))
        sw = nl.get("swap")
        extra.append(("\U0001F501 If you'd rather replace it, swap in: %s %g %s (%d%%, Apex %d, BACKUP %d). Backups "
                      "that filled a pulled spot have hit about 6 of 10." % (
                          _label(sw[1]["pp"])[0] + " " + _label(sw[1]["pp"])[1], sw[1]["pp"]["line"], sw[1]["pp"]["direction"],
                          round(100 * sw[1]["pp"]["prob"]), sw[1]["sc"], sw[0])) if sw
                     else "\U0001F501 No backup qualifies to replace it right now.")
    elif d.get("pulled_fill"):
        extra.append("\U0001F501 Filled in: ParlayPlay pulled %s after the last board, so this backup moved up." % d["pulled_fill"])
    if extra and len(L) > 2:
        L[2:2] = extra
    for ln in L:
        if "Freshness:" in ln:
            out.append(_seen_line(pp))
            if str(pp.get("score_note") or "").startswith("kept"):   # APEX-PPMIRROR-A-2026-10-23
                out.append("\u26a0\ufe0f Win chance not re-priced on this read: " + pp["score_note"][:160])
            continue
        out.append(ln)
        if ln.startswith("\U0001F4CA "):
            out.append("\U0001F7EA ParlayPlay pays %.2fx on %s \u00b7 %s \u00b7 starts %s CT" % (
                pp["mult"], pp["direction"], pp["league"], st))
            bk = _books_say(d)   # APEX-PPMIRROR-B-2026-10-23: 📗 Books say, as on PrizePicks Goblin cards
            if bk:
                out.append(bk)
    return out


def _books_say(d):
    """📗 Books say ~X% (Apex Y%) from bet365's main total (stored with the line) and Pinnacle. '' if none."""
    try:
        import apex_pinnacle as pin, apex_book_lines as abl, apex_edge_tracker as t
        p, pp = dict(d["p"]), d["pp"]
        if pp.get("bet365_line") not in (None, "") and pp.get("bet365_over_prob") not in (None, "") \
                and p.get("category") == "total_games":
            p["apex_book_prob"] = abl.side_prob(float(pp["bet365_line"]), float(pp["bet365_over_prob"]),
                                                float(p["line"]), p["direction"])
        return pin.books_line(p, t._board_prob(p))
    except Exception:
        return ""


def board_text(b, total_lines, title="BEST PARLAYPLAY -- PUBLISHED BOARD"):
    import apex_edge_tracker as t
    rec = t.records()
    now = datetime.now(CT)
    L = ["\U0001F4CC **%s -- %s**" % (title, now.strftime("%Y-%m-%d")),
         "_ParlayPlay only (not PrizePicks). Read from ParlayPlay at %s CT; %d tennis props evaluated. "
         "Same bar as PrizePicks: 70%%+ Win Probability and Apex Score 67+, one per match and player._" % (
             now.strftime("%I:%M %p").lstrip("0"), total_lines)]
    ruled = isinstance(b, Board) and PP_BOARD_RULES
    if ruled:   # APEX-PPMIRROR-B-2026-10-23: the PrizePicks order line, plus what differs on ParlayPlay
        L.append(t.order_header() + " On ParlayPlay the 17.5 check runs on every Total Games MORE line "
                 "of 17.5 or more; Total Games MORE with under 3 games of room under bet365 goes to BACKUPS.")
        for k, v in ((getattr(b, "notes", None) or {}).items()):
            L.append(v)
    if getattr(t, "SCORE_DISPLAY", False):   # APEX-SCOREDISPLAY-2026-10-25
        L.append(t.SCORE_EXPLAIN)
    if not b:
        L.append("No ParlayPlay prop clears 70%+ and Apex 67 right now.")
        L.extend(backup_lines(b, rec) if ruled else [])
        return "\n".join(L)
    _bs = backup_summary(b) if ruled else None   # APEX-BACKUPORDER-2026-10-24
    if _bs:
        L.append(_bs)
    _sdec = t.score_decimals([t.score_value("pp", d["p"]) for d in b])   # APEX-SCOREDISPLAY-2026-10-25
    for i, d in enumerate(b, 1):
        L.extend(card_lines(d, i, rec, _sdec[i - 1]))
    if ruled:
        L.extend(backup_lines(b, rec))
    L.append("\u2501" * 22)
    L.append("_Ranked by Win Probability + signal points (PrizePicks rule). Apex Score is a pass/fail check (67+); "
             "Confidence and Rank Score are shown for reference only._" if ruled else
             "_Ranked by Win Probability. Apex Score is a pass/fail check (67+); Confidence and Rank Score are shown for reference only._")
    return "\n".join(L)


def allprops_text(rows, limit=25):
    ds, rec = scored(rows)
    ds = [d for d in ds if d["p"]["prob"] >= BOARD_MIN_PROB][:limit]
    return board_text(ds, len(rows), title="PARLAYPLAY ALL PROPS (70%+)") if ds else (
        "\U0001F4CC **PARLAYPLAY ALL PROPS** -- none at 70%+ right now.")


def card(d):
    """One-line alert form."""
    who, cat = _label(d["pp"])
    return "\u2022 %s -- %s %g %s \u00b7 %d%% \u00b7 Apex %d \u00b7 pays %.2fx" % (
        who, cat, d["pp"]["line"], d["pp"]["direction"], round(100 * d["p"]["prob"]), d["sc"], d["pp"]["mult"])


def _label(p):
    cat = p["category"].replace("_", " ").title()
    who = f"{p['home']} v {p['away']}" if p["player"] == "match" else p["player"]
    return who, cat


# ------------------------------------------------------------------ slip planner
# APEX-PARLAYPLAY-SLIP-2026-10-15 (owner, "Yes let add that"):
# ParlayPlay multiplies the multipliers of every pick (6 picks at
# 1.10/1.10/1.06/1.06/1.18/1.32 -> 2.12x on his slip). Legs come only from
# props that clear the board bar (70%+ Win Probability, Apex 67+), one per
# match; for each match the planner takes the qualifying line that pays the
# most for its chance (lower of Win/Honest Probability x multiplier), so it
# can use 18.5 More at 1.30x instead of 17.5 More at 1.10x when both qualify.
SLIP_SIZES = (2, 3, 4)


def slip_legs(rows):
    ds, _ = scored(rows)
    best = {}
    for d in ds:
        p = d["p"]
        if p["prob"] < BOARD_MIN_PROB or d["sc"] < APEX_MIN:
            continue
        c = min(d["pp"]["prob"], d["pp"]["honest"])
        k = d["pp"]["match_id"]
        if k not in best or c * d["pp"]["mult"] > best[k][0]:
            best[k] = (c * d["pp"]["mult"], d)
    return [d for _, d in sorted(best.values(), key=lambda x: -x[0])]


def slips(rows, b=None):
    """APEX-PPMIRROR-A-2026-10-23 (owner, "Go 1-8"): slip legs are the ParlayPlay BOARD
    picks in board order -- the same gate as PrizePicks (70%+ Win Probability, Apex 67+), one per
    match and player, ranked by the board's rank -- never "the line that pays most for its chance"
    (those slips went 35-167; legs over 1.30x hit 60%). The 2-, 3- and 4-pick slips are the top 2,
    3 and 4 of the board. The old highest-payout slip (1-162) is kept only as a "long shot, test
    only" entry with no $ suggestion."""
    b = board(rows) if b is None else b

    def stats(legs):
        pay = win = hon = 1.0
        for d in legs:
            pay *= d["pp"]["mult"]
            win *= d["pp"]["prob"]
            hon *= min(d["pp"]["prob"], d["pp"]["honest"])
        return pay, win, hon

    out = []
    for n in SLIP_SIZES:
        if len(b) < n:
            break
        legs = list(b[:n])
        pay, win, hon = stats(legs)
        out.append({"n": n, "kind": "best", "legs": legs, "payout": pay, "win": win, "honest": hon})
    if LONG_SHOT_TEST and len(b) >= 2:
        ds, _ = scored(rows)
        by = {}
        for d in ds:
            if d["p"]["prob"] >= BOARD_MIN_PROB and d["sc"] >= APEX_MIN:
                by.setdefault(d["pp"]["match_id"], []).append(d)
        if len(by) >= 2:
            legs = [max(v, key=lambda d: d["pp"]["mult"]) for v in by.values()]
            pay, win, hon = stats(legs)
            out.append({"n": len(legs), "kind": "max", "legs": legs, "payout": pay, "win": win, "honest": hon})
    return out


# APEX-PPMIRROR-A-2026-10-23: the old highest-payout slip, logged and shown as one test-only line
# (no card, no $). Undo: False drops it entirely.
LONG_SHOT_TEST = True
# From the 10/23 audit, every graded 'best' slip leg since 10/15 (all Total Games).
MULT_NOTE = "Legs paying 1.30x or less have hit 75% (46 of 61); legs over 1.30x hit 60% (31 of 52)."
MULT_SAFE = 1.30


def _mult_dot(m):
    return "\U0001F7E2" if m <= MULT_SAFE + 1e-9 else "\U0001F7E0"


def long_shot_line(s_):
    return ("\U0001F3B2 Long shot -- test only, not a suggestion: %d legs (the highest-paying qualifying line in "
            "each match) would pay %.2fx; all-hit %d%% by Win Probability. Slips built this way went 1-162." % (
                s_["n"], s_["payout"], round(100 * s_["win"])))


TIERS = (("MEDIUM", 0.5, "\U0001F7E1"), ("HIGH", 0.3, "\U0001F7E0"),
         ("VERY HIGH", 0.15, "\U0001F534"), ("LONG SHOT", 0.0, "\u26AB"))   # safest -> riskiest, by Win Probability


def tier(win):
    return next(t for t, lo, _ in TIERS if win >= lo)


def risk_line(win, hon, pay):
    """Plain-English risk for a slip."""
    one_in = max(1, round(1 / win)) if win > 0 else 0
    t = tier(win)
    lvl = dict((n, e) for n, _, e in TIERS)[t] + " " + t + ("" if t == "LONG SHOT" else " RISK")
    lose = 100 - round(100 * win)
    back = pay * hon
    return ("%s -- hits about **1 in %d** (%d%%). You lose the whole entry about **%d%% of the time**. "
            "Long run you get back about **$%.2f per $1** (Honest)%s." % (
                lvl, one_in, round(100 * win), lose, back,
                "" if back >= 1 else ", so on average this slip loses money"))


def slip_text(rows):
    now = datetime.now(CT).strftime("%I:%M %p").lstrip("0")
    ss = slips(rows)
    L = ["\U0001F9FE **PARLAYPLAY SLIP PLANNER** (%s CT)" % now,
         "_ParlayPlay multiplies every pick's multiplier. Legs are the top of the ParlayPlay board, in board "
         "order (70%+ Win Probability, Apex 67+, one per match and player)._"]
    best = [x for x in ss if x["kind"] == "best"]
    if not best:
        L.append("Not enough board-quality ParlayPlay props for a slip right now (need 2 matches).")
        return "\n".join(L)
    for s_ in best:
        L.append("\u2501" * 22)
        L.append("**%d-pick All In -- pays %.2fx** ($10 -> $%.2f)" % (s_["n"], s_["payout"], 10 * s_["payout"]))
        for d in s_["legs"]:
            who, cat = _label(d["pp"])
            L.append("\u2022 %s -- %s %g **%s** @ %s %.2fx \u00b7 %d%%" % (
                who, cat, d["pp"]["line"], d["pp"]["direction"], _mult_dot(d["pp"]["mult"]), d["pp"]["mult"],
                round(100 * d["pp"]["prob"])))
        L.append("\U0001F3AF All hit: %d%% by Win Probability \u00b7 %d%% by Honest Probability" % (
            round(100 * s_["win"]), round(100 * s_["honest"])))
    L.append("\u2501" * 22)
    L.append("\U0001F4CF " + MULT_NOTE)
    for s_ in ss:
        if s_["kind"] == "max":
            L.append(long_shot_line(s_))
    L.append("_ParlayPlay boosts (like +25% for an extra pick) are not counted; the app shows the final payout._")
    return "\n".join(L)


SLIP_COLOR = 0x9B59B6   # ParlayPlay purple (slips-pnl uses green/red/blue for results)


def _short(name):
    parts = str(name).split()
    return " ".join(parts[1:]) if len(parts) > 1 else str(name)


def slip_cards(rows, stake=10.0, b=None):
    """One card per suggested slip, laid out like #slips-pnl: payout, every leg's Win and Honest
    Probability, and the risk in plain words. APEX-PPMIRROR-A-2026-10-23: legs are the top of the
    board in board order; the long-shot test slip is one line on the last card, no $."""
    if _insured_on():    # APEX-PPINSURED-2026-10-23: PP INSURED 3 is the only slip card
        return insured_build(rows, stake)[1]
    if _slips369_on():   # APEX-PPSLIPS369-2026-10-23: Top 3 / Top 6 / Top 9 cards replace the 2/3/4 planner
        return slip369_cards(board(rows) if b is None else b, stake)
    now = datetime.now(CT).strftime("%I:%M %p").lstrip("0")
    cards = []
    ss = slips(rows, b)
    try:
        record_slips(ss)
    except Exception as e:
        log(f"slip record failed: {type(e).__name__}: {e}")
    for s_ in [x for x in ss if x["kind"] == "best"]:
        pay = s_["payout"]
        title = "\U0001F9FE %d-PICK SLIP \u00b7 ALL IN \u00b7 board #1-#%d" % (s_["n"], s_["n"])
        desc = ("**Pays %.2fx** \u00b7 $%.0f entry \u2192 **$%.2f** (+$%.2f if every pick hits)\n"
                "\U0001F3AF All hit: **%d%%** by Win Probability \u00b7 %d%% by Honest Probability\n"
                "%s\n_Suggested %s CT_") % (
            pay, stake, stake * pay, stake * pay - stake, round(100 * s_["win"]), round(100 * s_["honest"]),
            risk_line(s_["win"], s_["honest"], pay), now)
        legs = []
        for k, d in enumerate(s_["legs"], 1):
            pp_ = d["pp"]
            who = (f"{_short(pp_['home'])} v {_short(pp_['away'])}" if pp_["player"] == "match" else pp_["player"])
            st = _ct_hm(pp_["start_utc"])   # APEX-ANYDATE-2026-10-25
            _pj = (" \u00b7 Apex projects **%.1f**" % float(pp_["proj_mean"])) if pp_.get("proj_mean") is not None else ""
            legs.append("#%d \u23F3 **%s** \u00b7 %s %s %g%s \u00b7 %s **%.2fx**\n\u2003Win Prob **%d%%** \u00b7 Honest %d%% \u00b7 %s" % (
                k, who, pp_["category"].replace("_", " ").title(), pp_["direction"], pp_["line"], _pj,
                _mult_dot(pp_["mult"]), pp_["mult"], round(100 * pp_["prob"]), round(100 * pp_["honest"]), st))
        cards.append({"title": title, "description": desc, "color": SLIP_COLOR, "legs": legs,
                      "footer": "Top of the ParlayPlay board (70%+ / Apex 67+, board order) \u00b7 not placed \u00b7 boosts not counted"})
    if cards:
        cards[0]["description"] += "\n\U0001F4CF " + MULT_NOTE
        for s_ in ss:
            if s_["kind"] == "max":
                cards[-1]["description"] += "\n" + long_shot_line(s_)
        try:   # APEX-PPMIRROR-C-2026-10-23: the official slip record in days won and $, last 7 days
            import apex_pp_tracking as _trk
            _trk.grade()
            cards[0]["tally"] = _trk.ondemand_text()
        except Exception as e:
            log(f"slip tally failed: {type(e).__name__}: {e}")
    return cards


# ------------------------------------------------------------------ Top 3 / 6 / 9 slips
# APEX-PPSLIPS369-2026-10-23 (owner: "allow me to pick up to 9 props on one slip, so we need
# the Top 3, Top 6 and Top 9, but keep the way we choose good picks and the signals we use for
# PrizePicks"). Every board build posts three cards in #pp-slips: the board exactly as numbered
# #1-3, #1-6 and #1-9 after every PrizePicks-style rule (Win Prob + signal rank, 17.5 check,
# low-cushion hold, BACKUPS, one per match/player, pulled-line fill). Payout = ParlayPlay's real
# All In payout (the leg multipliers multiplied). ParlayPlay's Insured entry (one miss still pays)
# is only offered when EVERY pick pays 1.40x or more (ParlayPlay help center, "How does insurance
# work?"); its standard table: 3 picks 2.5x / 1.25x one miss, 6 picks 25x / 2.5x, 9 picks 80x / 6x.
# The old 2/3/4-pick planner and its `slips` table are left as they were (no new rows).
# Turned on and off by apex_pp_tracking.LIVE (one switch for the cards, the log and the record);
# while it is False the 2/3/4-pick cards post as before.
PP_SLIPS_369 = True


def _slips369_on():
    try:
        import apex_pp_tracking as _trk
        return PP_SLIPS_369 and _trk.LIVE
    except Exception:
        return False
SLIPS_369 = ((3, "\U0001F947", "PP TOP 3 SLIP", 0x9B59B6), (6, "\U0001F3C6", "PP TOP 6 SLIP", 0x8E44AD),
             (9, "\U0001F3AF", "PP TOP 9 SLIP", 0x6C3483))
INSURED_MIN_MULT = 1.40
INSURED_TABLE = {2: (2, 0.5), 3: (2.5, 1.25), 4: (5, 1.25), 5: (12, 2.5), 6: (25, 2.5), 7: (40, 4), 8: (50, 4), 9: (80, 6)}
PP_MIN_PAYOUT = 1.75   # lowest slip payout on ParlayPlay's help page "How do payouts work?"


def _net(d):
    """The 🧮 net signal count PrizePicks ranks on (shared apex_card_signals.net_score); None if unknown."""
    try:
        import apex_card_signals as cs
        return cs.net_score(d["p"])[2]
    except Exception:
        return None


def _ct_hm(iso):
    # APEX-ANYDATE-2026-10-25: boards mix every upcoming date -- a pick that isn't today
    # shows its day and date in bold ("**Mon 10/26, 4:00 AM**")
    d = datetime.fromisoformat(iso).astimezone(CT)
    if d.date() != datetime.now(CT).date():
        return "**%s %d/%d, %s**" % (d.strftime("%a"), d.month, d.day, d.strftime("%I:%M %p").lstrip("0"))
    return d.strftime("%a %I:%M %p").replace(" 0", " ")


def slip_leg_line(k, d):
    pp_ = d["pp"]
    who = (f"{_short(pp_['home'])} v {_short(pp_['away'])}" if pp_["player"] == "match"
           else "%s (v %s)" % (pp_["player"], _short(pp_.get("opponent") or "")))
    net = _net(d)
    return "#%d **%s** · %s %g **%s** · \U0001F7EA %.2fx · Win **%d%%** · \U0001F9EE %s · %s" % (
        k, who, pp_["category"].replace("_", " ").title(), pp_["line"], pp_["direction"], pp_["mult"],
        round(100 * pp_["prob"]), ("%+d" % net) if net is not None else "n/a", _ct_hm(pp_["start_utc"]))


def _same_match(legs):
    """Pairs of legs that share a match or a player (the board allows none; checked anyway)."""
    import apex_edge_tracker as t
    out = []
    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            a, b_ = legs[i], legs[j]
            if a["pp"]["match_id"] == b_["pp"]["match_id"] or (t._prop_names(a["p"]) & t._prop_names(b_["p"])):
                out.append((i + 1, j + 1))
    return out


def slip_math(legs):
    pay = win = 1.0
    for d in legs:
        pay *= float(d["pp"]["mult"])
        win *= float(d["pp"]["prob"])
    return pay, win, all(float(d["pp"]["mult"]) >= INSURED_MIN_MULT - 1e-9 for d in legs)


def how_to_play(legs, insured_ok, first=None):
    """first = the earliest start on the board (the time the slip must be built by)."""
    n = len(legs)
    first = first or min(d["pp"]["start_utc"] for d in legs)
    base = "\U0001F4DD **How to play:** build it off the LAST board posted before the first match (%s CT). " % _ct_hm(first)
    if n <= 3:
        return base + "Play it straight (All In): all %d must hit." % n
    if insured_ok:
        a, o = INSURED_TABLE.get(n, (None, None))
        return base + ("Pick the **Insured** version: one miss still pays (the app shows the amount before you submit; "
                       "ParlayPlay's standard %d-pick table is %gx all hit, %gx with one miss)." % (n, a, o))
    ms = [float(d["pp"]["mult"]) for d in legs]
    return base + ("**All must hit** -- ParlayPlay only offers Insured (one miss still pays) when every pick pays "
                   "%.2fx or more, and these pay %.2fx-%.2fx." % (INSURED_MIN_MULT, min(ms), max(ms)))


def _held_lines(b, limit=6):
    """BACKUPS that were held back by a rule (with the reason), one line each."""
    out = []
    rows = list(getattr(b, "backups", None) or [])
    held = [r for r in rows if str(r[2]).startswith("held back")]
    for pr, sub67, why, d, also in held[:limit]:
        who, cat = _label(d["pp"])
        out.append("\U0001F501 %s %s %g %s · %.2fx · Win %d%% -- %s" % (
            who, cat, d["pp"]["line"], d["pp"]["direction"], d["pp"]["mult"], round(100 * pr), why[len("held back: "):][:110]))
    if len(held) > limit:
        out.append("(+%d more held -- see /pp-best)" % (len(held) - limit))
    same = sum(1 for r in rows if str(r[2]).startswith("same match"))
    if same:
        out.append("(%d more passed but share a match or player with a pick above, so they can't go on the same slip)" % same)
    return out or ["No passing prop was held back -- nothing else cleared 70% and Apex 67."]


def slip369_cards(b, stake=10.0):
    """The three PP TOP 3 / TOP 6 / TOP 9 cards for this board. Never raises (returns what it can)."""
    now = datetime.now(CT).strftime("%I:%M %p").lstrip("0")
    cards = []
    first = min((d["pp"]["start_utc"] for d in b[:max(s[0] for s in SLIPS_369)]), default=None)
    for size, emo, name, color in SLIPS_369:
        try:
            legs = list(b[:size])
            n = len(legs)
            title = "%s %s (%d legs)" % (emo, name, size)
            if n < 2:
                cards.append({"title": title, "color": color, "legs": [],
                              "description": "Only %d pick%s qualif%s today -- no %d-leg slip.\n\n**Held back (BACKUPS):**\n%s" % (
                                  n, "" if n == 1 else "s", "ies" if n == 1 else "y", size, "\n".join(_held_lines(b))),
                              "footer": "ParlayPlay board #1-#%d · suggested %s CT · not placed" % (size, now)})
                continue
            pay, win, ins = slip_math(legs)
            D = []
            if n < size:
                D.append("⚠️ **Only %d picks qualify today**, so this is a %d-leg slip, not %d." % (n, n, size))
            D.append("\U0001F4B5 **$%.0f ➜ $%.2f if all %d hit** (ParlayPlay pays %.2fx, the %d multipliers multiplied)" % (
                stake, stake * pay, n, pay, n))
            one_in = max(1, round(1 / win)) if win > 0 else 0
            D.append("\U0001F3AF All-hit chance: **%d%%** (about 1 in %d) -- every pick's Win Prob multiplied" % (round(100 * win), one_in))
            D.append(how_to_play(legs, ins, first))
            if pay < PP_MIN_PAYOUT:
                D.append("_ParlayPlay's help page lists %.2fx as the lowest slip payout; if the app won't take this one, it will say so._" % PP_MIN_PAYOUT)
            sm = _same_match(legs)
            if sm:
                D.append("⚠️ Same match/player: " + ", ".join("#%d and #%d" % x for x in sm))
            if n < size:
                D += ["", "**Held back (BACKUPS, with why):**"] + _held_lines(b)
            cards.append({"title": title, "description": "\n".join(D), "color": color,
                          "legs": [slip_leg_line(k, d) for k, d in enumerate(legs, 1)],
                          "footer": "ParlayPlay board #1-#%d, same rules as PrizePicks · suggested %s CT · not placed" % (n, now)})
        except Exception as e:
            log(f"slip card {size} failed: {type(e).__name__}: {e}")
    if cards:
        try:
            import apex_pp_tracking as _trk
            _trk.grade()
            cards[-1]["tally"] = _trk.short_text()
        except Exception as e:
            log(f"slip tally failed: {type(e).__name__}: {e}")
    return cards



# ------------------------------------------------------------------ Insured slips
# APEX-PPINSURED-2026-10-23 (owner, "Go"): PP INSURED 3 is the ONLY card in #pp-slips --
# three Total Games legs paying 1.40x+ on ParlayPlay at our Win Prob 60-65%, one per match/player,
# ranked by rank score, 17.5 check / low-cushion holds go to BACKUPS with the reason; paid on
# ParlayPlay's Insured table (2.5x all hit, 1.25x one miss). Insured 6 and 9 are TEST cards in
# #pp-systems (data only); Insured 5 and Insured 3 "skip close matches" are logged with no card.
# Rules + tracker: apex_pp_insured.py. The Top 3/6/9 All In and 2/3/4 cards stay in the code
# behind their switches. Undo: apex_pp_insured.LIVE = False (save point before-pp-insured).
def _insured_on():
    try:
        import apex_pp_insured as _ins
        return _ins.LIVE
    except Exception:
        return False


INSURED_CARDS = {"ins3": ("\U0001F947 PP INSURED 3 SLIP (3 legs) \U0001F9EA test running", 0x9B59B6),
                 "ins6": ("\U0001F9EA PP INSURED 6 TEST (data only, don't play yet)", 0x5D6D7E),
                 "ins9": ("\U0001F9EA PP INSURED 9 TEST (data only)", 0x5D6D7E)}


def insured_leg_line(k, d):
    import apex_pp_insured as _ins
    pp_ = d["pp"]
    who = f"{_short(pp_['home'])} v {_short(pp_['away'])}"
    line = float(pp_["line"])
    bk = _book_total(pp_)
    if bk is None:
        room = "\U0001F4CF bet365: no total for this match"
    else:
        r = (bk - line) if pp_["direction"] == "MORE" else (line - bk)
        room = "\U0001F4CF bet365 expects %g games, ParlayPlay line %g: %s" % (
            bk, line, ("%g game%s of room" % (r, "" if r == 1 else "s")) if r >= 0 else ("%g games short of our side" % -r))
    f = _ins.fav(d)
    if f is None:
        fv = "⚖ favorite chance: unknown"
    elif f < _ins.CLOSE_FAV:
        fv = "⚖ favorite %d%% to win ⚠ close match (these hit %d of 100)" % (round(100 * f), _ins.CLOSE_HIT)
    else:
        fv = "⚖ favorite %d%% to win" % round(100 * f)
    return "#%d **%s** · Total Games %g **%s** · \U0001F7EA %.2fx · Win **%d%%**\n %s\n %s · starts %s CT" % (
        k, who, line, pp_["direction"], float(pp_["mult"]), round(100 * d["win"]), room, fv, _ct_hm(pp_["start_utc"]))


def _insured_held(held, limit=5):
    out = []
    for d in held[:limit]:
        who, _c = _label(d["pp"])
        out.append("\U0001F501 %s Total Games %g %s · %.2fx · Win %d%% -- held: %s" % (
            who, d["pp"]["line"], d["pp"]["direction"], float(d["pp"]["mult"]), round(100 * d["win"]), str(d["why"])[:110]))
    if len(held) > limit:
        out.append("(+%d more held)" % (len(held) - limit))
    return out or ["No other line was held back -- nothing else is Total Games at 1.40x+ and 60-65%."]


def insured_card(arm, legs, held, stake=10.0):
    import apex_pp_insured as _ins
    size = _ins.ARM_SIZE[arm]
    title, color = INSURED_CARDS[arm]
    now = datetime.now(CT).strftime("%I:%M %p").lstrip("0")
    n = len(legs)
    D = []
    if n < size:
        D.append("⚠️ **Only %d leg%s qualif%s today** -- no %d-leg Insured slip%s." % (
            n, "" if n == 1 else "s", "ies" if n == 1 else "y", size, "" if arm != "ins3" else ", don't play one"))
    else:
        a, o = _ins.TABLE[size]
        _d = lambda x: ("%d" % x) if abs(x - round(x)) < 1e-9 else ("%.2f" % x)
        D.append("\U0001F4B5 **$%s ➜ $%s if all %d hit · $%s if one misses (Insured)**" % (
            _d(stake), _d(stake * a), size, _d(stake * o)))
        ca, c1 = _ins.chances(size)
        D.append("\U0001F3AF All %d hit about **%d of 100**; at least %d hit about **%d of 100** "
                 "(legs like these hit %d of 100)" % (size, round(100 * ca), size - 1, round(100 * c1), round(100 * _ins.LEG_RATE)))
        if arm == "ins3":
            D.append("\U0001F4DD **How to play:** Build it off the LAST board before the first match. In the app choose "
                     "**INSURED**, not All In. Check the app shows %gx / %gx before you submit; if it shows less, tell me." % (a, o))
        else:
            D.append("\U0001F9EA Test only: logged and graded every day, nothing to play.")
    if n < size:
        D += ["", "**Held back (BACKUPS, with why):**"] + _insured_held(held)
    return {"title": title, "description": "\n".join(D), "color": color,
            "legs": [insured_leg_line(k, d) for k, d in enumerate(legs, 1)],
            "footer": "ParlayPlay Total Games 1.40x+ · Win Prob 60-65%% · one per match · suggested %s CT · not placed" % now}


def insured_build(rows, stake=10.0, ts=None, log_it=False):
    """(arms, #pp-slips cards, #pp-systems cards) for this read."""
    import apex_pp_insured as _ins
    arms = _ins.build(rows)
    if log_it:
        _ins.log_build(arms, ts)
    held = arms["_held"]
    slips_c = [insured_card("ins3", arms["ins3"], held, stake)]
    sys_c = [insured_card(a, arms[a], held, stake) for a in ("ins6", "ins9")]
    try:
        _ins.grade()
        slips_c[0]["tally"] = _ins.ondemand_text()
        sys_c[-1]["tally"] = "\n".join(_ins.summary_lines())[:1024]
    except Exception as e:
        log(f"insured tally failed: {type(e).__name__}: {e}")
    return arms, slips_c, sys_c


def insured_sig(arms, which):
    return [sorted("%s|%s" % (d["pp"]["key"], d["pp"]["direction"]) for d in arms[a]) for a in which]

def _embed_len(e):
    n = len(e.get("title", "")) + len(e.get("description", "")) + len((e.get("footer") or {}).get("text", ""))
    return n + sum(len(f["name"]) + len(f["value"]) for f in e.get("fields", []))


# ------------------------------------------------------------------ slip tracking
# APEX-PP-SLIPTRACK-2026-10-15 (owner: "keep track of the win rate of
# slip from safe, to riskiest" + "win rate for 2 pick, 3 picks and so on", "Go").
# Every slip card is frozen as posted (tier, size, payout, Win/Honest all-hit %,
# each leg). Same legs again = same slip, counted once. Graded after its lines:
# all legs hit = HIT, any leg lost = MISS; a void/push leg is dropped and the
# slip graded on the rest (void if fewer than 2 legs remain).
def _slip_sig(s_):
    return s_["kind"] + ":" + ",".join(sorted(f"{d['pp']['key']}|{d['pp']['direction']}" for d in s_["legs"]))


def record_slips(ss):
    if not ss:
        return 0
    c = _db()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = 0
    for s_ in ss:
        sig = _slip_sig(s_)
        if c.execute("SELECT 1 FROM slips WHERE sig=?", (sig,)).fetchone():
            continue
        last = max(d["pp"]["start_utc"] for d in s_["legs"])
        c.execute("INSERT INTO slips (sig, posted_ts, kind, n, tier, payout, win, honest, last_start) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (sig, ts, s_["kind"], s_["n"], tier(s_["win"]), s_["payout"], s_["win"], s_["honest"], last))
        for d in s_["legs"]:
            pp_ = d["pp"]
            who, cat = _label(pp_)
            c.execute("INSERT INTO slip_legs VALUES (?,?,?,?,?,?,?,?,?)",
                      (sig, pp_["key"], pp_["direction"], pp_["mult"], pp_["prob"], pp_["honest"], pp_["start_utc"],
                       f"{who} {cat} {pp_['direction']} {pp_['line']:g}", None))
        new += 1
    c.commit()
    c.close()
    return new


def grade_slips():
    c = _db()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    done = 0
    for (sig,) in c.execute("SELECT sig FROM slips WHERE result IS NULL").fetchall():
        legs = c.execute("SELECT sl.rowid, sl.direction, l.result, l.settle_note FROM slip_legs sl "
                         "LEFT JOIN lines l ON l.key = sl.key WHERE sl.sig=?", (sig,)).fetchall()
        hit = lost = void = waiting = 0
        for rid, side, res, note in legs:
            if res is None:
                waiting += 1
                continue
            r = "void" if res in ("void", "push") else ("hit" if res == side.lower() else "miss")
            c.execute("UPDATE slip_legs SET result=? WHERE rowid=?", (r, rid))
            hit += r == "hit"; lost += r == "miss"; void += r == "void"
        if lost:
            res = "miss"            # one lost leg loses the slip, no need to wait for the rest
        elif waiting:
            continue
        elif hit < 2:
            res = "void"
        else:
            res = "hit"
        c.execute("UPDATE slips SET result=?, legs_hit=?, legs_void=?, graded_ts=?, note=? WHERE sig=?",
                  (res, hit, void, ts, ("void-adjusted: %d leg(s) dropped" % void) if void and res != "void" else "", sig))
        done += 1
    c.commit()
    c.close()
    return done


def _rec(rows):
    w = sum(r[0] == "hit" for r in rows)
    l = sum(r[0] == "miss" for r in rows)
    said = round(100 * sum(r[1] for r in rows) / len(rows)) if rows else 0
    pct = round(100 * w / (w + l)) if w + l else 0
    return w, l, pct, said


def tally_text(short=False):
    """Safest to riskiest and by slip size: record, actual hit %, and the % the cards said (Win Probability)."""
    c = _db()
    g = c.execute("SELECT kind, n, tier, win, result FROM slips WHERE result IN ('hit','miss')").fetchall()
    pend = c.execute("SELECT COUNT(*) FROM slips WHERE result IS NULL").fetchone()[0]
    voids = c.execute("SELECT COUNT(*) FROM slips WHERE result='void'").fetchone()[0]
    c.close()
    emo = dict((n, e) for n, _, e in TIERS)
    by_t, by_n = [], []
    for t, _, _ in TIERS:
        rs = [(r[4], r[3]) for r in g if r[2] == t]
        if rs:
            w, l, pct, said = _rec(rs)
            by_t.append("%s %s %d-%d %d%% (said %d%%)" % (emo[t], t, w, l, pct, said))
    for n in sorted({r[1] for r in g if r[0] == "best"}):
        rs = [(r[4], r[3]) for r in g if r[0] == "best" and r[1] == n]
        w, l, pct, said = _rec(rs)
        by_n.append("%d-pick %d-%d %d%% (said %d%%)" % (n, w, l, pct, said))
    rs = [(r[4], r[3]) for r in g if r[0] == "max"]
    if rs:
        w, l, pct, said = _rec(rs)
        by_n.append("\U0001F4B0 highest payout %d-%d %d%% (said %d%%)" % (w, l, pct, said))
    if not g:
        return "Slip record: no graded slips yet (%d waiting on results)." % pend
    tail = " \u00b7 %d waiting" % pend + (" \u00b7 %d void" % voids if voids else "")
    if short:
        return "By risk: " + " \u00b7 ".join(by_t) + "\nBy size: " + " \u00b7 ".join(by_n) + tail
    return "\n".join(["\U0001F4CA **ParlayPlay slip record** (graded slips, actual hit rate vs the all-hit % the card said)",
                      "**Safest to riskiest:**"] + ["\u2022 " + x for x in by_t] +
                     ["**By slip size:**"] + ["\u2022 " + x for x in by_n] + ["_" + tail.strip(" \u00b7") + "_"])


def card_embed(c):
    # APEX-PPSLIPS369-2026-10-23: a 9-leg card is over Discord's 1024-character field limit, so the
    # legs are split over as many "Picks" fields as they need (never cut mid-leg).
    fields, cur = [], []
    for ln in c["legs"]:
        if cur and len("\n".join(cur + [ln])) > 1024:
            fields.append(cur)
            cur = []
        cur.append(ln[:1024])
    if cur:
        fields.append(cur)
    return {"title": c["title"], "description": c["description"][:4096], "color": c["color"],
            "fields": [{"name": "Picks" if i == 0 else "Picks (cont.)", "value": "\n".join(f), "inline": False}
                       for i, f in enumerate(fields)] +
                      ([{"name": ("\U0001F4CA PP Insured record" if _insured_on() else "\U0001F4CA ParlayPlay official slip" + ("s" if _slips369_on() else "")), "value": c["tally"][:1024], "inline": False}]
                       if c.get("tally") else []),
            "footer": {"text": c["footer"]}}


def send_cards(channel, cards, header=None):
    hook = (_load_json(CONFIG, {}).get("webhooks") or {}).get(channel)
    if not hook:
        log(f"#pp-{channel} not set up yet -- {len(cards)} card(s) logged only")
        return False
    import requests
    embeds = [card_embed(c) for c in cards[:10]]
    body = {"embeds": embeds}
    if header:
        body["content"] = header
    if not cards:
        body = {"content": (header or "") + "\nNo slip today: fewer than 2 matches have a board-quality ParlayPlay prop."}
    try:
        # APEX-PPSLIPS369-2026-10-23: Discord caps one message at 6000 characters of embeds; three
        # 3/6/9 cards can pass that, so each card then goes as its own message.
        if sum(_embed_len(e) for e in embeds) > 5800:
            ok = True
            for i, e in enumerate(embeds):
                one = {"embeds": [e]}
                if i == 0 and header:
                    one["content"] = header
                ok = requests.post(hook, json=one, timeout=10).status_code < 300 and ok
            return ok
        r = requests.post(hook, json=body, timeout=10)
        return r.status_code < 300
    except Exception as e:
        log(f"send cards #pp-{channel} failed: {e}")
        return False


# ------------------------------------------------------------------ discord
def chunks(text, n=1900):
    """Discord-sized pieces, split between lines (cards stay whole when they fit)."""
    out, cur = [], ""
    for ln in text.split("\n"):
        if ln.startswith("\u2501") and cur and len(cur) + 600 > n:
            out.append(cur); cur = ""
        if len(cur) + len(ln) + 1 > n and cur:
            out.append(cur); cur = ""
        cur += ln + "\n"
    if cur.strip():
        out.append(cur)
    return out


def send(channel, text):
    """channel: picks | alerts | systems -> the ParlayPlay channels only."""
    hook = (_load_json(CONFIG, {}).get("webhooks") or {}).get(channel)
    if not hook:
        log(f"#pp-{channel} not set up yet -- logged only: {text[:120]}")
        return False
    import requests
    ok = True
    for part in chunks(text):
        try:
            r = requests.post(hook, json={"content": part}, timeout=10)
            ok = ok and r.status_code < 300
        except Exception as e:
            log(f"send #pp-{channel} failed: {e}")
            ok = False
    return ok


# ------------------------------------------------------------------ pulled lines
# APEX-PPMIRROR-C-2026-10-23 (owner, "Go 1-8", step 7): the PrizePicks pulled-line rules for
# ParlayPlay. When a numbered pick of the last posted board is no longer offered (match not started):
#   1. the same player/match, market and side at the nearest line ParlayPlay still offers keeps the
#      spot when it still passes every board rule at that line -> card says 🔁 NEW LINE, with the
#      backup he could swap in (PULLED_LINE_PREFER_NEW_LINE);
#   2. otherwise the next BACKUP moves up (the rebuild does that) and the header says so.
# #pp-alerts gets one line per pulled line per day; every pulled pick is logged to
# data/pulled_lines.csv. Undo: PP_PULLED_RULES = False.
PP_PULLED_RULES = True
PULLED_LOG = os.path.join(PP_DIR, "pulled_lines.csv")


def _detail(b):
    return [{"key": d["pp"]["key"], "direction": d["pp"]["direction"], "match_id": d["pp"]["match_id"],
             "category": d["pp"]["category"], "player": d["pp"]["player"], "line": d["pp"]["line"],
             "prob": d["pp"]["prob"], "start_utc": d["pp"]["start_utc"], "label": " ".join(_label(d["pp"])),
             "pick": _pick_label(d["pp"])}
            for d in b]


def _pick_label(pp):
    return "%s %s %g %s" % (_label(pp)[0], _label(pp)[1], pp["line"], pp["direction"])


def _swap_option(b, slot_names):
    """First BACKUP that could take a pulled spot: passes 67, not held, keeps one per match/player."""
    import apex_edge_tracker as t
    used = set()
    for d in b:
        used |= t._prop_names(d["p"])
    used -= slot_names
    for k, (pr, sub67, why, d, _also) in enumerate(getattr(b, "backups", None) or [], 1):
        if sub67 or why.startswith("held back") or t._prop_names(d["p"]) & used:
            continue
        return k, d
    return None


def apply_pulled(b, cur, prev, ds, size=BOARD_SIZE):
    """Applies the pulled-line rules to a fresh board in place. Returns (header notes, alert lines, log rows)."""
    import apex_edge_tracker as t
    if not PP_PULLED_RULES or not prev:
        return [], [], []
    now = datetime.now(timezone.utc).isoformat()
    offered = {(r["key"], d) for r in cur for d, m in (("MORE", r.get("mult_more")), ("LESS", r.get("mult_less"))) if m}
    on = {(d["pp"]["key"], d["pp"]["direction"]) for d in b}
    notes, alerts, rows = [], [], []
    for rank, e in enumerate(prev, 1):
        if (e["key"], e["direction"]) in offered or e["start_utc"] <= now:
            continue
        same = [d for d in ds if d["pp"]["match_id"] == e["match_id"] and d["pp"]["category"] == e["category"]
                and d["pp"]["player"] == e["player"] and d["pp"]["direction"] == e["direction"]
                and t._board_qualifies(d) and not _hold_why(d)]
        same.sort(key=lambda d: (abs(float(d["pp"]["line"]) - float(e["line"])), -d["pp"]["prob"]))
        now_lines = sorted({d["pp"]["line"] for d in ds if d["pp"]["match_id"] == e["match_id"]
                            and d["pp"]["category"] == e["category"] and d["pp"]["player"] == e["player"]
                            and d["pp"]["direction"] == e["direction"]})
        kept = None
        if same and t.PULLED_LINE_PREFER_NEW_LINE:
            nl = same[0]
            names = t._prop_names(nl["p"])
            # the match's current pick (or the same pick, already re-ranked elsewhere) gives way to the
            # same pick at the new line in the pulled pick's spot
            for i, d in enumerate(list(b)):
                if t._prop_names(d["p"]) & names:
                    b.pop(i)
                    break
            b.insert(min(rank - 1, len(b)), dict(nl))
            del b[size:]
            on = {(d["pp"]["key"], d["pp"]["direction"]) for d in b}
            kept = next((d for d in b if d["pp"]["key"] == nl["pp"]["key"] and d["pp"]["direction"] == nl["pp"]["direction"]), None)
        if kept is not None:
            pos = b.index(kept) + 1
            sw = _swap_option(b, t._prop_names(kept["p"]))
            kept["pulled_new_line"] = {"old_line": e["line"], "line": kept["pp"]["line"], "old_prob": e["prob"],
                                       "prob": kept["pp"]["prob"], "swap": sw}
            head = "\u26A0 ParlayPlay moved after the last board: %s %g \u2192 %g (#%d kept at the new line)%s" % (
                e["label"], e["line"], kept["pp"]["line"], pos,
                (" (swap option: %s, BACKUP %d)" % (_pick_label(sw[1]["pp"]), sw[0])) if sw
                else " (no backup qualifies to swap in)")
            notes.append(head)
            alerts.append(head + " -- now %d%%, Apex %d at the new line" % (round(100 * kept["pp"]["prob"]), kept["sc"]))
            outcome = "kept at new line %g" % kept["pp"]["line"]
        else:
            fill = next((d for d in b if (d["pp"]["key"], d["pp"]["direction"]) not in
                         {(x["key"], x["direction"]) for x in prev}), None)
            if fill is not None:
                fill["pulled_fill"] = e.get("pick") or e["label"]
            head = "\u26A0 ParlayPlay pulled: %s (was #%d)%s -- %s" % (
                e.get("pick") or e["label"], rank,
                ("; now offered at %s" % ", ".join("%g" % x for x in now_lines)) if now_lines else "",
                ("a backup moved up: %s" % _pick_label(fill["pp"])) if fill is not None else "no eligible backup, spot left empty")
            notes.append(head)
            alerts.append(head)
            outcome = ("filled by %s" % _pick_label(fill["pp"])) if fill is not None else "empty"
        rows.append([datetime.now(CT).isoformat(timespec="seconds"), e["start_utc"], e["key"], e.get("pick") or e["label"],
                     e["direction"], e["line"], rank, outcome])
    return notes, alerts, rows


def _log_pulled(rows):
    if not rows:
        return
    first = not os.path.exists(PULLED_LOG)
    with open(PULLED_LOG, "a", newline="") as fh:
        w = csv.writer(fh)
        if first:
            w.writerow(["logged_ct", "start_utc", "key", "pick", "side", "line", "was_rank", "outcome"])
        w.writerows(rows)


# ------------------------------------------------------------------ data check
# APEX-PPMIRROR-A-2026-10-23 (owner, "Go 1-8"): the PrizePicks DATA CHECK block, for
# ParlayPlay, in #pp-systems on every board build. It calls the same shared checks
# (apex_data_health: SportsAPI365, player data, Pinnacle) and adds ParlayPlay's own odds
# coverage and the DECLINED list (every match that could not be scored, with the reason).
DATA_CHECK_POST = True
ODDS_COVERAGE_WARN = 0.80     # under 80% of matches priced = warning


def _match_label(r):
    return "%s v %s" % (r.get("home"), r.get("away"))


def data_check(rows, kept=0, limit=15):
    """(all_ok, lines) for the ParlayPlay DATA CHECK block. Never raises."""
    try:
        import apex_data_health as dh
    except Exception:
        dh = None
    checks = []
    for fn in ("check_sa365", "check_player_data", "check_pinnacle"):
        try:
            checks.append(getattr(dh, fn)())
        except Exception as e:
            checks.append((fn.replace("check_", ""), False, "check could not run (%s)" % type(e).__name__))
    by = {}
    for r in rows or []:
        by.setdefault(r.get("match_id"), []).append(r)
    fresh = {m for m, rs in by.items() if any(r.get("prob_more") is not None and
                                               not str(r.get("score_note") or "").startswith("kept") for r in rs)}
    held = {m for m, rs in by.items() if m not in fresh and any(r.get("prob_more") is not None for r in rs)}
    none = [m for m in by if m not in fresh and m not in held]
    n = len(by)
    cov = len(fresh) / float(n) if n else 1.0
    checks.append(("Odds", cov >= ODDS_COVERAGE_WARN,
                   "%d of %d ParlayPlay matches priced on this read (bet365 via SportsAPI365)%s" % (
                       len(fresh), n, (", %d kept their last good score" % len(held)) if held else "")))
    try:   # APEX-PPMIRROR-B-2026-10-23: bet365 totals coverage for ParlayPlay Total Games matches
        tg = {r.get("match_id") for r in rows or [] if r.get("category") == "total_games"}
        have = {r.get("match_id") for r in rows or [] if r.get("category") == "total_games" and _book_total(r)}
        if tg:
            checks.append(("bet365 totals", len(have) >= ODDS_COVERAGE_WARN * len(tg),
                           "%d of %d Total Games matches have a bet365 total" % (len(have), len(tg))))
    except Exception:
        pass
    bad = [(nm, d) for nm, ok, d in checks if not ok]
    if not bad and not none:
        L = ["\u2705 **PARLAYPLAY DATA CHECK** all clear (%s)" % "; ".join(
            "%s: %s" % (nm, d) for nm, ok, d in checks if nm in ("Odds", "bet365 totals"))]
        if kept:
            L.append("\u2139\ufe0f %d line(s) on their last good score (not re-priced this read)" % kept)
        return True, L
    L = ["\U0001F9EA **PARLAYPLAY DATA CHECK** (%s CT)" % datetime.now(CT).strftime("%I:%M %p").lstrip("0")]
    for nm, ok, d in checks:
        if d.startswith("\U0001F6D1"):
            L.append("\U0001F6D1 **DATA CHECK -- SportsAPI365 not answering**" + d[len("\U0001F6D1 SportsAPI365 not answering"):])
        else:
            L.append(("\u2705 %s: %s" if ok else "\u26a0\ufe0f **DATA CHECK FAILED -- %s:** %s") % (nm, d))
    if held:
        L.append("\u2139\ufe0f %d match(es) kept their last good score -- the board still uses it, the card says so" % len(held))
    if none:
        L.append("\u274c **DECLINED -- no score** (%d match%s; they come back by themselves once priced)" % (
            len(none), "" if len(none) == 1 else "es"))
        for m in sorted(none, key=lambda m: by[m][0].get("start_utc") or "")[:limit]:
            r0 = by[m][0]
            st = datetime.fromisoformat(r0["start_utc"]).astimezone(CT).strftime("%a %I:%M %p").replace(" 0", " ")
            L.append("\u274c %s (%s, %s CT, %d lines) -- %s" % (
                _match_label(r0), r0.get("league"), st, len(by[m]), (r0.get("score_note") or "no score")[:200]))
        if len(none) > limit:
            L.append("(+%d more)" % (len(none) - limit))
    return False, L


def _book_total(r):
    """bet365 total for a ParlayPlay row/pick: the shared reader (names + date) first, then the
    total ParlayPlay stored from the same SportsAPI365 pre-match odds. None if unknown."""
    try:
        import apex_bet365_totals as b3
        names = [r.get("home"), r.get("away")] if r.get("home") else None
        if names:
            v = b3.total(names, str(r.get("start_utc") or "")[:10])[0]
            if v is None:
                d_ct = datetime.fromisoformat(r["start_utc"]).astimezone(CT).date().isoformat()
                v = b3.total(names, d_ct)[0]
            if v is not None:
                return float(v)
        return float(r["bet365_line"]) if r.get("bet365_line") not in (None, "") else None
    except Exception:
        return None


# ------------------------------------------------------------------ job
def run():
    """One read under a lock, so the 30-min job and /pp-fetch never open the
    Chrome profile at the same time."""
    import fcntl
    lock = open(os.path.join(PP_DIR, ".run.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return "a ParlayPlay read is already running"
    try:
        return _run()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


# APEX-PPTHROTTLE-2026-10-16 (owner, "Go"): the 30-min launchd job reads
# every 30 min inside the line-drop windows (same windows as the PrizePicks
# board watch) and hourly the rest of the day. /pp-fetch ("run") always reads.
DROP_WINDOWS = ((11 * 60, 13 * 60 + 30), (21 * 60, 23 * 60 + 30))
OFFPEAK_MIN_GAP_SEC = 55 * 60


def scheduled():
    now = datetime.now(CT)
    m = now.hour * 60 + now.minute
    if not any(a <= m < b for a, b in DROP_WINDOWS):
        last = float(_load_json(STATE, {}).get("last_ok_read") or 0)
        if time.time() - last < OFFPEAK_MIN_GAP_SEC:
            return "off-peak: last read %d min ago, skipping (hourly outside drop windows)" % ((time.time() - last) // 60)
    return run()


def _run():
    os.chdir(HERE)   # the engine loads its model files relative to ~/openclaw
    st = _load_json(STATE, {})
    try:
        raw = fetch_offering()
    except BotCheck as e:
        if st.get("read_status") != "botcheck":
            send("systems", f"⚠️ ParlayPlay read stopped: {e}. Not trying to get past it; will retry next run.")
        st["read_status"] = "botcheck"
        _save_json(STATE, st)
        return f"read stopped: {e}"
    except Exception as e:
        st["read_fail"] = st.get("read_fail", 0) + 1
        if st["read_fail"] == 3:
            send("systems", f"⚠️ ParlayPlay read failed 3 runs in a row: {type(e).__name__}: {e}")
        _save_json(STATE, st)
        return f"read failed: {type(e).__name__}: {e}"
    if st.get("read_status") == "botcheck" or st.get("read_fail", 0) >= 3:
        send("systems", "✅ ParlayPlay read is working again.")
    st["read_status"], st["read_fail"] = "ok", 0
    st["last_ok_read"] = time.time()   # APEX-PPTHROTTLE-2026-10-16: for the off-peak hourly skip
    try:
        raw_file = save_raw(raw)
    except Exception as e:
        raw_file = f"raw save failed: {e}"
    rows = parse_tennis(raw)
    score(rows)
    # APEX-SA365CHECK-2026-10-23 (owner, "It's paid, be sure it's working"): on 10/22
    # SportsAPI365 answered 401, nothing scored, and store() wrote the empty scores over 364
    # saved win chances, so an empty board posted. When nothing scored and SportsAPI365 is
    # down, store nothing and keep the last good board.
    if rows and not any(r.get("prob_more") is not None for r in rows):
        try:
            import apex_data_health as _dh
            _n, _ok, _why = _dh.check_sa365()
        except Exception:
            _ok, _why = True, ""
        if not _ok:
            if not st.get("sa365_hold"):
                send("picks", "\U0001F6D1 **held: results service down** -- %s. The last good ParlayPlay board "
                              "stands; nothing new posts until it is back." % _why.lstrip("\U0001F6D1 "))
                st["sa365_hold"] = True
            _save_json(STATE, st)
            return "held: SportsAPI365 down, %d lines read, nothing stored (last good board kept)" % len(rows)
    st.pop("sa365_hold", None)
    new, changed, ts = store(rows)
    try:
        c = _db()
        c.execute("INSERT INTO reads VALUES (?,?,?,?,?,?,?)", (ts, 1, len(rows), new, changed, raw_file, ""))
        c.commit(); c.close()
    except Exception:
        pass
    nl = getattr(store, "last_new", [])
    if nl and st.get("board_sig") is not None:
        ms = {}
        for r in nl:
            ms.setdefault(f"{r['home']} v {r['away']}", []).append(r)
        L = ["\U0001F195 **New ParlayPlay lines posted** (%s CT): %d tennis props across %d match(es)" % (
            datetime.now(CT).strftime("%I:%M %p").lstrip("0"), len(nl), len(ms))]
        for m, rr in sorted(ms.items()):
            st_ = datetime.fromisoformat(rr[0]["start_utc"]).astimezone(CT).strftime("%a %I:%M %p").replace(" 0", " ")
            cats = sorted({x["category"].replace("_", " ").title() for x in rr})
            L.append("\u2022 %s (%s, %s CT) -- %d lines: %s" % (m, rr[0]["league"], st_, len(rr), ", ".join(cats)))
        L.append("Scored already; any that make the board show in #pp-picks.")
        send("alerts", "\n".join(L))
    cur = current(ts)
    b = board(cur)
    sig = [[d["pp"]["key"], d["pp"]["direction"]] for d in b]
    # APEX-PPMIRROR-A-2026-10-23: DATA CHECK on every build; an empty board is never posted over
    # a good one when the data is failing (the last good board stands, #pp-picks says why once).
    try:
        data_ok, dc_lines = data_check(rows, getattr(store, "last_kept", 0))
    except Exception as e:
        data_ok, dc_lines = True, ["\u26a0\ufe0f PARLAYPLAY DATA CHECK could not run (%s)" % type(e).__name__]
    if DATA_CHECK_POST:
        send("systems", "\n".join(dc_lines))
    if not b and st.get("board_sig") and not data_ok:
        if not st.get("empty_hold"):
            send("picks", "\U0001F6D1 **ParlayPlay board held** -- this read has no qualifying props and the data "
                          "check failed (see #pp-systems). The last good board above stands; a new one posts when "
                          "the data is back.")
            st["empty_hold"] = True
        _save_json(STATE, st)
        return "held: empty board on a data failure, %d lines read, last good board kept" % len(rows)
    st.pop("empty_hold", None)
    try:   # APEX-PPMIRROR-C-2026-10-23: pulled lines -> same pick at the new line, or a backup moves up
        notes, p_alerts, p_rows = apply_pulled(b, cur, st.get("board_detail") or [], getattr(b, "ds", []))
        if notes:
            b.notes = dict(enumerate(notes))
            b.backups = backup_rows(b.ds, b)
            sig = [[d["pp"]["key"], d["pp"]["direction"]] for d in b]
            today = datetime.now(CT).date().isoformat()
            done = st.setdefault("pulled_alerted", {}).setdefault(today, [])
            for k in list(st["pulled_alerted"]):
                if k != today:
                    st["pulled_alerted"].pop(k)
            fresh = [a for a, r in zip(p_alerts, p_rows) if r[2] + "|" + r[4] not in done]
            if fresh:
                send("alerts", "\n".join(fresh))
                done.extend(r[2] + "|" + r[4] for r in p_rows)
            _log_pulled(p_rows)
    except Exception as e:
        log(f"pulled-line step failed: {type(e).__name__}: {e}")
    try:   # APEX-PPMIRROR-C-2026-10-23: official daily slip candidate + paper tests (ParlayPlay files only)
        import apex_pp_tracking as _trk
        if not _insured_on():   # APEX-PPINSURED-2026-10-23: the All In Top 3/6/9 log is retired
            _trk.log_build(b, ts)
        _trk.log_tests(b)
    except Exception as e:
        log(f"tracking failed: {type(e).__name__}: {e}")
    try:   # APEX-NOBOOK-2026-10-24: no-book-total holds tracker
        import apex_nobook_tracker as _nbt
        _nbt.log_pp(b)
    except Exception as e:
        log(f"no-book tracker failed: {type(e).__name__}: {e}")
    if sig != st.get("board_sig"):
        old = {tuple(x) for x in st.get("board_sig") or []}
        added = [d for d in b if (d["pp"]["key"], d["pp"]["direction"]) not in old]
        send("picks", board_text(b, len(cur)))
        try:   # APEX-BOARDHISTORY-2026-10-25: permanent board history row per prop (data only)
            import apex_board_history as _bh
            _bh.log_pp_publish(b, ts)
        except Exception as e:
            log(f"board history failed: {type(e).__name__}: {e}")
        if not _insured_on():   # APEX-PPINSURED-2026-10-23: the Insured cards post below instead
            send_cards("slips", slip_cards(cur, b=b), ("\U0001F9FE **ParlayPlay slips: Top 3 / Top 6 / Top 9** -- new board, new slips. "
                                                      "Build yours off the last board before the first match.") if _slips369_on()
                       else "\U0001F9FE **ParlayPlay slip suggestions** -- new board, new slips")
        if added and st.get("board_sig") is not None:
            send("alerts", "🎯 **New ParlayPlay props on the board**\n" + "\n".join(card(d) for d in added))
        st["board_sig"] = sig
        st["board_detail"] = _detail(b)   # APEX-PPMIRROR-C-2026-10-23: for the pulled-line check
    if _insured_on():   # APEX-PPINSURED-2026-10-23: Insured 3 to #pp-slips, Insured 6/9 tests to #pp-systems, on change
        try:
            arms, sc_, yc_ = insured_build(cur, ts=ts, log_it=True)
            s3, s69 = insured_sig(arms, ("ins3",)), insured_sig(arms, ("ins6", "ins9"))
            if s3 != st.get("insured3_sig"):
                if send_cards("slips", sc_, "\U0001F9FE **ParlayPlay Insured slip** -- new legs. Build yours off the "
                                            "last board before the first match, and choose INSURED in the app."):
                    st["insured3_sig"] = s3
            if s69 != st.get("insured69_sig"):
                if send_cards("systems", yc_, "\U0001F9EA **ParlayPlay Insured tests** -- data only, don't play these yet."):
                    st["insured69_sig"] = s69
        except Exception as e:
            log(f"insured slips failed: {type(e).__name__}: {e}")
    _save_json(STATE, st)
    scored = sum(1 for r in rows if r.get("prob_more") is not None)
    try:
        graded = settle()
    except Exception as e:
        graded = f"grading failed: {type(e).__name__}: {e}"
    try:
        graded += f", slips graded {grade_slips()}"
        try:   # APEX-PPMIRROR-C-2026-10-23
            import apex_pp_tracking as _trk
            graded += f", official slips graded {_trk.grade()}, tests graded {_trk.grade_tests()}"
        except Exception as e:
            graded += f", official grading failed: {type(e).__name__}"
        if _insured_on():   # APEX-PPINSURED-2026-10-23
            import apex_pp_insured as _ins
            graded += f", insured graded {_ins.grade()}"
            for fc in _ins.finished_cards():
                send("alerts", fc)
            if _ins.daily_post_due() and send("systems", "\n".join(["\U0001F4C5 **ParlayPlay Insured -- daily record**"]
                                                                    + _ins.summary_lines() + _ins.day_lines("ins3", 3))):
                _ins.mark_daily()
        today = datetime.now(CT)
        if today.hour >= 9 and st.get("tally_date") != today.date().isoformat():
            if send("learning", tally_text()):
                st["tally_date"] = today.date().isoformat()
                _save_json(STATE, st)
    except Exception as e:
        graded += f", slip grading failed: {type(e).__name__}: {e}"
    return f"{len(rows)} tennis lines ({new} new, {changed} multiplier change(s)), {scored} scored, board {len(b)}, {graded}"


def setup_discord():
    """One-time: ParlayPlay category + #pp-picks/#pp-alerts/#pp-systems, one
    webhook each, saved to data/parlayplay/config.json. Needs the bot role's
    Manage Channels + Manage Webhooks. Re-running reuses what already exists."""
    import re, requests
    tok = re.search(r'DISCORD_BOT_TOKEN = "([^"]+)"', open(os.path.join(HERE, "discord_bot.py")).read()).group(1)
    H = {"Authorization": "Bot " + tok}
    api = "https://discord.com/api/v10"
    guild = requests.get(f"{api}/users/@me/guilds", headers=H, timeout=15).json()[0]["id"]
    chans = requests.get(f"{api}/guilds/{guild}/channels", headers=H, timeout=15).json()
    cat = next((c for c in chans if c["type"] == 4 and c["name"].lower() == "parlayplay"), None)
    if not cat:
        cat = requests.post(f"{api}/guilds/{guild}/channels", headers=H, timeout=15,
                            json={"name": "ParlayPlay", "type": 4}).json()
        if "id" not in cat:
            raise RuntimeError(f"could not create category: {cat}")
    cfg = _load_json(CONFIG, {})
    hooks = cfg.setdefault("webhooks", {})
    for short, topic in (("picks", "ParlayPlay boards and picks only -- not PrizePicks"),
                         ("alerts", "ParlayPlay line/multiplier moves and new value picks"),
                         ("systems", "ParlayPlay read status and errors"),
                         ("slips", "ParlayPlay slip suggestions with payout and all-hit chance"),
                         # APEX-PP-LEARNING-2026-10-15: ParlayPlay's own learning channel, apart from PrizePicks
                         ("learning", "ParlayPlay learning: slip and pick results, lessons -- not PrizePicks")):
        name = f"pp-{short}"
        ch = next((c for c in chans if c["type"] == 0 and c["name"] == name), None)
        if not ch:
            ch = requests.post(f"{api}/guilds/{guild}/channels", headers=H, timeout=15,
                               json={"name": name, "type": 0, "parent_id": cat["id"], "topic": topic}).json()
            if "id" not in ch:
                raise RuntimeError(f"could not create #{name}: {ch}")
        if not hooks.get(short):
            wh = requests.post(f"{api}/channels/{ch['id']}/webhooks", headers=H, timeout=15,
                               json={"name": "ParlayPlay"}).json()
            if "id" not in wh:
                raise RuntimeError(f"could not create webhook for #{name}: {wh}")
            hooks[short] = f"https://discord.com/api/webhooks/{wh['id']}/{wh['token']}"
    _save_json(CONFIG, cfg)
    return "ParlayPlay category + #pp-picks, #pp-alerts, #pp-systems, #pp-slips, #pp-learning ready"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "setup":
        print(setup_discord())
    elif cmd == "login":
        # Opens the ParlayPlay Chrome window ON screen so he can log in himself
        # (we never type his password). Session is saved in the profile; the
        # 30-min read then sees what his logged-in account sees. Close the
        # window when done.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(PROFILE, channel="chrome", headless=False,
                                                       args=["--window-position=80,60", "--window-size=1300,900"])
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto(URL, timeout=60000)
            print("Log in to ParlayPlay in the window, then close it.")
            try:
                pg.wait_for_event("close", timeout=0)
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass
    elif cmd == "fetch":
        rows = parse_tennis(fetch_offering())
        print(len(rows), "tennis lines")
    elif cmd == "scheduled":
        print(scheduled())
    elif cmd == "board":
        cur = current()
        print(board_text(board(cur), len(cur)))
    else:
        print(run())
