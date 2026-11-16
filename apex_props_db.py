#!/usr/bin/env python3
"""
APEX-PROPSDB-2026-10-13 (owner: "I want a entire database ... with all
props data, collect every detail possible"). One SQLite file holding EVERY
PrizePicks tennis prop OpenClaw sees -- evaluated or not -- with its full
PrizePicks record, its line history, what the engine and the boards said, the
match odds we have, and the final result. Background only: nothing here posts
to Discord or changes a board.

data/props_db.sqlite
  projections   one row per PrizePicks projection id: player, opponent, stat,
                odds type (standard/goblin/demon), first / last / last-before-start
                line, start time, first & last seen, promo / flash-sale flags,
                popularity (trending_count), rank, PrizePicks ids, plus at
                settlement: engine probability & direction, boards it was on,
                Pinnacle favourite price, bet365 total-games line, the actual
                stat, the score, and whether MORE / LESS won.
  changes       one row whenever a projection's line, type, status, promo,
                flash-sale or adjusted odds changes (full line history).
  raw_daily     the complete raw PrizePicks JSON for each projection, first
                time seen each day (every field PrizePicks sends).

Jobs (scheduler): snapshot() every 30 min, settle() every 2 hours.
CLI:  python3 apex_props_db.py snapshot | settle | backfill | stats | export out.csv
"""
import csv, json, os, sqlite3, sys, time
from datetime import datetime, timezone, timedelta, date

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "props_db.sqlite")
PICK_LOG = os.path.join(HERE, "apex_engine_picks_log_v1.csv")
FWD_LOG = os.path.join(HERE, "apex_prop_sim_forward_log_v1.csv")
STATUS_LOG = os.path.join(HERE, "apex_board_status_log_v1.csv")
BOOK_LOG = os.path.join(HERE, "apex_book_lines_log_v1.csv")
FIRSTSEEN_LOG = os.path.join(HERE, "apex_board_firstseen_log_v1.csv")
SETTLE_AFTER_H = 4          # try to settle once the match started this many hours ago
GIVE_UP_DAYS = 4            # stop retrying an unsettleable prop after this many days
MAX_MATCHES_PER_SETTLE = 60
# APEX-FASTSETTLE-2026-10-19 (owner, "Go"): newest props are checked first; a prop
# that has failed this many tries (no stat sheet, match not found) goes to the back of the
# queue so it can no longer fill every slot and starve newer days (it did, Oct 17-19).
BACKOFF_TRIES = 3

PROJ_COLS = [
    ("projection_id", "TEXT PRIMARY KEY"), ("source", "TEXT"), ("pp_game_id", "TEXT"), ("game_id", "TEXT"),
    ("player", "TEXT"), ("player_ppid", "TEXT"), ("player_pp_id", "TEXT"), ("opponent", "TEXT"),
    ("stat_type", "TEXT"), ("stat_slug", "TEXT"), ("category", "TEXT"), ("odds_type", "TEXT"),
    ("line_first", "REAL"), ("line_last", "REAL"), ("line_at_start", "REAL"), ("line_changes", "INTEGER"),
    ("start_utc", "TEXT"), ("match_date", "TEXT"), ("first_seen", "TEXT"), ("last_seen", "TEXT"),
    ("board_time", "TEXT"), ("pp_updated_at", "TEXT"), ("status_last", "TEXT"), ("event_type", "TEXT"),
    ("projection_type", "TEXT"), ("is_promo", "INTEGER"), ("flash_sale_line", "REAL"), ("adjusted_odds", "TEXT"),
    ("refundable", "INTEGER"), ("hr_20", "INTEGER"), ("rank", "INTEGER"), ("trending_first", "INTEGER"),
    ("trending_last", "INTEGER"), ("trending_max", "INTEGER"), ("duration", "TEXT"),
    # filled at settlement
    ("engine_prob", "REAL"), ("engine_prob_cal", "REAL"), ("engine_direction", "TEXT"), ("engine_tier", "TEXT"),
    ("engine_playable", "TEXT"), ("engine_ts", "TEXT"), ("engine_price_source", "TEXT"),
    ("boards", "TEXT"), ("board_reason", "TEXT"), ("pinn_fav", "REAL"), ("player_is_fav", "INTEGER"),
    ("bet365_line", "REAL"), ("bet365_over_prob", "REAL"), ("surface", "TEXT"), ("best_of", "TEXT"),
    ("actual", "REAL"), ("score", "TEXT"), ("result", "TEXT"), ("more_won", "INTEGER"), ("less_won", "INTEGER"),
    ("settle_note", "TEXT"), ("settled_ts", "TEXT"), ("settle_tries", "INTEGER"),
    # APEX-DISCOUNT-2026-10-16: 1 once PrizePicks ever offered this prop as a
    # discount play (is_promo, or a flash-sale line); never cleared.
    ("discount", "INTEGER"), ("discount_first_seen", "TEXT"),
]


def _conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS projections (%s)" % ", ".join("%s %s" % x for x in PROJ_COLS))
    have = {r[1] for r in c.execute("PRAGMA table_info(projections)")}
    for name, typ in PROJ_COLS:
        if name not in have:
            c.execute("ALTER TABLE projections ADD COLUMN %s %s" % (name, typ.replace(" PRIMARY KEY", "")))
    c.execute("""CREATE TABLE IF NOT EXISTS changes (projection_id TEXT, ts TEXT, field TEXT,
                 old TEXT, new TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS raw_daily (projection_id TEXT, day TEXT, ts TEXT, raw TEXT, PRIMARY KEY (projection_id, day))")
    c.execute("CREATE INDEX IF NOT EXISTS ix_proj_start ON projections(start_utc)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_proj_player ON projections(player, category, line_last)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_changes ON changes(projection_id, ts)")
    return c


def _norm(s):
    return " ".join(str(s or "").strip().lower().split())


def _now():
    return datetime.now(timezone.utc)


def _iso_utc(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return ""


def _stat_map():
    try:
        import apex_ops as ops
        return ops._core_const("_APEX_STAT_MAP") or {}
    except Exception:
        return {}


# ----------------------------------------------------------------------------- snapshot
def _fetch_raw():
    """Full partner-API payload: (projection dicts with every attribute, included index)."""
    import apex_ops as ops
    # APEX-PPTHROTTLE-2026-10-16: shared PrizePicks gate -- reuses the cycle's pull (up to
    # 5 min old); never records an old pull as a new snapshot while paused.
    body = ops.pp_partner_board(max_age=300, allow_stale=False, caller="props_db")
    inc = {(x.get("type"), x.get("id")): x for x in body.get("included") or []}
    return body.get("data") or [], inc


def snapshot():
    try:
        data, inc = _fetch_raw()
    except Exception as e:
        return "props db: partner API failed (%s: %s)" % (type(e).__name__, e)
    smap = _stat_map()
    now = _now().isoformat(timespec="seconds")
    day = now[:10]
    c = _conn()
    new = changed = 0
    with c:
        for proj in data:
            a = proj.get("attributes") or {}
            rel = proj.get("relationships") or {}
            if a.get("line_score") is None:
                continue
            pid = str(proj.get("id"))
            pl = inc.get(("new_player", ((rel.get("new_player") or {}).get("data") or {}).get("id"))) or {}
            pla = pl.get("attributes") or {}
            dur = inc.get(("duration", ((rel.get("duration") or {}).get("data") or {}).get("id"))) or {}
            slug = str(a.get("stat_type") or a.get("stat_display_name") or "").strip().lower().replace(" ", "_")
            rec = {
                "pp_game_id": ((rel.get("game") or {}).get("data") or {}).get("id"),
                "game_id": a.get("game_id"), "player": pla.get("display_name") or pla.get("name") or "",
                "player_ppid": pla.get("ppid"), "player_pp_id": pl.get("id"), "opponent": a.get("description") or "",
                "stat_type": a.get("stat_type"), "stat_slug": slug, "category": smap.get(slug, slug),
                "odds_type": str(a.get("odds_type") or "standard").lower(), "line": float(a["line_score"]),
                "start_utc": _iso_utc(a.get("start_time")), "board_time": a.get("board_time"),
                "pp_updated_at": a.get("updated_at"), "status": a.get("status"), "event_type": a.get("event_type"),
                "projection_type": a.get("projection_type"), "is_promo": int(bool(a.get("is_promo"))),
                "flash_sale_line": a.get("flash_sale_line_score"),
                "adjusted_odds": None if a.get("adjusted_odds") is None else json.dumps(a.get("adjusted_odds")),
                "refundable": int(bool(a.get("refundable"))), "hr_20": int(bool(a.get("hr_20"))),
                "rank": a.get("rank"), "trending": a.get("trending_count"),
                "duration": (dur.get("attributes") or {}).get("name"),
            }
            started = rec["start_utc"] and rec["start_utc"] <= now
            row = c.execute("SELECT line_last, odds_type, status_last, is_promo, flash_sale_line, adjusted_odds, "
                            "trending_max, line_changes FROM projections WHERE projection_id=?", (pid,)).fetchone()
            if row is None:
                new += 1
                c.execute("""INSERT INTO projections (projection_id, source, pp_game_id, game_id, player, player_ppid,
                    player_pp_id, opponent, stat_type, stat_slug, category, odds_type, line_first, line_last,
                    line_at_start, line_changes, start_utc, match_date, first_seen, last_seen, board_time, pp_updated_at,
                    status_last, event_type, projection_type, is_promo, flash_sale_line, adjusted_odds, refundable,
                    hr_20, rank, trending_first, trending_last, trending_max, duration, settle_tries)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (pid, "partner_api", rec["pp_game_id"], rec["game_id"], rec["player"], rec["player_ppid"],
                     rec["player_pp_id"], rec["opponent"], rec["stat_type"], rec["stat_slug"], rec["category"],
                     rec["odds_type"], rec["line"], rec["line"], None if started else rec["line"], 0,
                     rec["start_utc"], (a.get("start_time") or "")[:10], now, now, rec["board_time"],
                     rec["pp_updated_at"], rec["status"], rec["event_type"], rec["projection_type"], rec["is_promo"],
                     rec["flash_sale_line"], rec["adjusted_odds"], rec["refundable"], rec["hr_20"], rec["rank"],
                     rec["trending"], rec["trending"], rec["trending"], rec["duration"]))
            else:
                old = dict(zip(("line", "odds_type", "status", "is_promo", "flash_sale_line", "adjusted_odds"), row[:6]))
                for f in old:
                    nv = rec[f]
                    if (old[f] if old[f] is not None else None) != (nv if nv is not None else None):
                        c.execute("INSERT INTO changes VALUES (?,?,?,?,?)", (pid, now, f, str(old[f]), str(nv)))
                        changed += 1
                lc = (row[7] or 0) + (1 if old["line"] != rec["line"] else 0)
                c.execute("""UPDATE projections SET line_last=?, odds_type=?, status_last=?, is_promo=?,
                    flash_sale_line=COALESCE(?, flash_sale_line), adjusted_odds=?, pp_updated_at=?, last_seen=?,
                    start_utc=?, rank=?, trending_last=?, trending_max=MAX(COALESCE(trending_max,0), COALESCE(?,0)),
                    line_changes=?, line_at_start=CASE WHEN ? THEN line_at_start ELSE ? END WHERE projection_id=?""",
                    (rec["line"], rec["odds_type"], rec["status"], rec["is_promo"], rec["flash_sale_line"],
                     rec["adjusted_odds"], rec["pp_updated_at"], now, rec["start_utc"], rec["rank"], rec["trending"],
                     rec["trending"], lc, 1 if started else 0, rec["line"], pid))
            if rec["is_promo"] or rec["flash_sale_line"] is not None:   # APEX-DISCOUNT-2026-10-16
                c.execute("UPDATE projections SET discount=1, discount_first_seen=COALESCE(discount_first_seen, ?) "
                          "WHERE projection_id=?", (now, pid))
            c.execute("INSERT OR IGNORE INTO raw_daily VALUES (?,?,?,?)",
                      (pid, day, now, json.dumps({"projection": proj, "player": pl, "duration": dur})))
    total = c.execute("SELECT COUNT(*) FROM projections").fetchone()[0]
    c.close()
    return "props db: %d on the board, %d new, %d change(s); %d props stored" % (len(data), new, changed, total)


_DISC_CACHE = {"ts": 0.0, "idx": {}}


def discount_index(max_age=60):
    """APEX-DISCOUNT-2026-10-16: {(player, category, line): regular line} for every
    discounted prop whose match starts within the last 36 hours or later. Keyed by
    both the board line and the flash-sale line, so either finds it. Cached 60 s;
    reads the DB only (never PrizePicks)."""
    if time.time() - _DISC_CACHE["ts"] < max_age:
        return _DISC_CACHE["idx"]
    idx = {}
    try:
        since = (_now() - timedelta(hours=36)).isoformat(timespec="seconds")
        c = _conn()
        try:
            rows = c.execute("SELECT player, category, line_last, flash_sale_line FROM projections "
                             "WHERE discount=1 AND start_utc>=?", (since,)).fetchall()
        finally:
            c.close()
        for pl, cat, line, fsl in rows:
            for ln in (line, fsl):
                if ln is not None:
                    idx[(_norm(pl), cat, float(ln))] = float(line) if line is not None else None
    except Exception:
        idx = _DISC_CACHE["idx"]
    _DISC_CACHE.update(ts=time.time(), idx=idx)
    return idx


def discount_tag(player, category, line):
    """'🏷️ Discount play' (+ the regular line when it differs), or '' when not discounted."""
    try:
        key = (_norm(player), category, float(line))
    except (TypeError, ValueError):
        return ""
    idx = discount_index()
    if key not in idx:
        return ""
    reg = idx[key]
    return "\U0001F3F7\uFE0F Discount play" + ((" (regular line %g)" % reg) if reg is not None and reg != key[2] else "")


# ----------------------------------------------------------------------------- settle + enrich
def _read(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _pair_names(r):
    if r.get("player") == "match":
        return frozenset(_norm(x) for x in str(r.get("opponent") or "").split(" v "))
    return frozenset((_norm(r.get("player")), _norm(r.get("opponent"))))


def _enrich_indexes():
    picks = {}
    for r in _read(PICK_LOG):
        nm = "match" if r.get("player") == "match" else _norm(r.get("player"))
        try:
            k = (r.get("category"), _pair_names(r), nm, float(r.get("line")), _norm(r.get("prop_type") or "standard"))
        except (TypeError, ValueError):
            continue
        picks.setdefault(k, []).append(r)
    fwd = {}
    for r in _read(FWD_LOG):
        if r.get("pinn_fav"):
            fwd[_pair_names(r)] = r
    boards = {}
    for r in _read(STATUS_LOG):
        nm = "match" if r.get("player") == "match" else _norm(r.get("player"))
        try:
            k = (r.get("category"), _pair_names(r), nm, float(r.get("line")), _norm(r.get("prop_type") or "standard"))
        except (TypeError, ValueError):
            continue
        b = boards.setdefault(k, {"on": set(), "reason": ""})
        if r.get("board") == "all":
            b["reason"] = r.get("reason") or b["reason"]
        elif r.get("status") == "recommended":
            b["on"].add(r.get("board"))
    book = {}
    for r in _read(BOOK_LOG):
        book[_pair_names(r)] = r
    return picks, fwd, boards, book


MATCH_LEVEL = {"total_games", "total_sets", "tiebreaks", "first_set_games", "1st_set_total_games"}


def _value(cat, slug, ev, side, sa):
    """Settled value for any category, incl. first-set markets the engine doesn't model."""
    if slug in ("1st_set_total_games", "1st_set_total_games_won", "first_set_total_games", "first_set_games"):
        flag = sa.match_flag(ev, "3")
        sets = sa.parse_sets(ev.get("score"))
        if flag in ("WO", "CANCELLED") or not sets or not str(ev.get("status") or "").lower() == "ended":
            return None, flag or "not finished"
        a, b = sets[0]
        if not sa.set_complete(a, b):
            return None, "first set incomplete"
        if slug.endswith("_won"):
            return float(a if side == 1 else b), ""
        return float(a + b), ""
    if "cancel" in str(ev.get("status") or "").lower():
        return None, "match record cancelled (rescheduled?) -- retry"
    v, flag, why = sa.value_for({"double_faults": "dfs", "break_points_won": "breaks",
                                 "total_games_won": "games_won"}.get(cat, cat), ev, side)
    if flag in ("RET", "WO"):
        return None, "VOID (%s)" % flag
    return v, why


SETTLE_WH_FALLBACK = True   # APEX-SETTLE-FALLBACK-2026-11-04 (owner, "Go testing standard"): grade score-only props from the warehouse final when the stats site has no usable record
_SCORE_CATS = {"total_games", "total_games_won", "games_won", "total_sets", "tiebreaks", "any_tiebreak", "goes_3_sets"}
_FS_SLUGS = ("1st_set_total_games", "1st_set_total_games_won", "first_set_total_games", "first_set_games")
_WH_DBS = ("data/warehouse/tennis_warehouse.sqlite", "data/player_history_filled.sqlite")


def _wh_final(player, opponent, d):
    """Final score from the warehouses, oriented player-first. -> (sets, retired, score_text, src) or None.
    Looks +-2 days, both name orders; a retirement/walkover returns retired=True."""
    import apex_settle_sa365 as sa, re as _re
    try:
        base = date.fromisoformat(str(d)[:10])
    except Exception:
        return None
    la, lb = (_norm(player).split() or [""])[-1], (_norm(opponent).split() or [""])[-1]
    if not la or not lb:
        return None
    for rel in _WH_DBS:
        path = os.path.join(HERE, rel)
        if not os.path.exists(path):
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
            rows = con.execute("select player_a, player_b, score, retired, match_date from matches where match_date between ? and ? "
                               "and ((player_a like ? and player_b like ?) or (player_a like ? and player_b like ?))",
                               ((base - timedelta(days=2)).isoformat(), (base + timedelta(days=2)).isoformat(),
                                "%" + la + "%", "%" + lb + "%", "%" + lb + "%", "%" + la + "%")).fetchall()
            con.close()
        except Exception:
            continue
        rows.sort(key=lambda r: abs((date.fromisoformat(str(r[4])[:10]) - base).days))
        for pa, pb, sc, ret, md in rows:
            if sa.same_player(pa, player) and sa.same_player(pb, opponent):
                flip = False
            elif sa.same_player(pa, opponent) and sa.same_player(pb, player):
                flip = True
            else:
                continue
            low = str(sc or "").lower()
            sets = [(int(y), int(x)) if flip else (int(x), int(y)) for x, y in _re.findall(r"(\d+)-(\d+)", _re.sub(r"\(\d+\)", "", sc or ""))]
            if ret or not sc or "ret" in low or "w/o" in low or "walk" in low or "def" in low:
                return sets, True, sc or "", rel
            if not sets:
                continue
            return sets, False, ",".join("%d-%d" % t for t in sets), rel
    return None


def _wh_value(cat, slug, player, opponent, d, best_of="3"):
    """Warehouse fallback for score-only props. -> (value|None, why, score_text) ; why starts with VOID for retire/cancel; None value + '' = no help."""
    import apex_settle_sa365 as sa
    if not (slug in _FS_SLUGS or cat in _SCORE_CATS):
        return None, "", None
    got = _wh_final(player, opponent, d)
    if got is None:
        return None, "", None
    sets, retired, text, src = got
    if retired:
        # a first-set prop still grades when the first set was finished before the retirement
        if slug in _FS_SLUGS and sets and sa.set_complete(*sets[0]):
            a, b = sets[0]
            return (float(a) if slug.endswith("_won") else float(a + b)), "warehouse score, retired later (%s)" % src, text
        return None, "VOID (retired/walkover per warehouse)", text
    need = 3 if str(best_of) == "5" else 2
    s1 = sum(a > b for a, b in sets); s2 = len(sets) - s1
    complete = all(sa.set_complete(a, b) or max(a, b) >= 10 for a, b in sets) and max(s1, s2) >= need
    if slug in _FS_SLUGS:
        a, b = sets[0]
        if not sa.set_complete(a, b):
            return None, "", None
        return (float(a) if slug.endswith("_won") else float(a + b)), "warehouse score (%s)" % src, text
    if not complete:
        return None, "", None
    g1, g2 = sum(a for a, _ in sets), sum(b for _, b in sets)
    tb = sum(1 for a, b in sets if {a, b} == {6, 7})
    val = {"total_games": float(g1 + g2), "total_games_won": float(g1), "games_won": float(g1), "total_sets": float(s1 + s2),
           "tiebreaks": float(tb), "any_tiebreak": float(tb > 0), "goes_3_sets": float(s1 + s2 == 3)}.get(cat)
    return val, "warehouse score (%s)" % src, text


def _real_opponent(opp):
    """APEX-SETTLE-1STSET-2026-10-20: first-set props name the opponent "Jasmine Paolini 1st Set";
    the result lookup needs the real name (566 first-set props 10/13-19 never graded)."""
    import re as _re
    return _re.sub(r"\s+\d(?:st|nd|rd|th) Set$", "", str(opp or "")).strip()


def settle(limit_matches=MAX_MATCHES_PER_SETTLE):
    import apex_settle_sa365 as sa
    c = _conn()
    now = _now()
    due = c.execute("""SELECT projection_id, player, opponent, category, stat_slug, odds_type,
                              COALESCE(line_at_start, line_last), start_utc, settle_tries, match_date
                       FROM projections WHERE settled_ts IS NULL AND start_utc != '' AND start_utc <= ?
                       AND start_utc >= ? AND player NOT LIKE '%/%'
                       AND source IN ('partner_api', 'boardwatch_backfill')
                       ORDER BY COALESCE(settle_tries, 0) >= ?, start_utc DESC""",
                    ((now - timedelta(hours=SETTLE_AFTER_H)).isoformat(timespec="seconds"),
                     (now - timedelta(days=GIVE_UP_DAYS)).isoformat(timespec="seconds"), BACKOFF_TRIES)).fetchall()
    by_match = {}
    for row in due:
        by_match.setdefault((frozenset((_norm(row[1]), _norm(_real_opponent(row[2])))), row[7][:10]), []).append(row)
    picks, fwd, boards, book = _enrich_indexes()
    settled = held = 0
    for (pair, d), rows in list(by_match.items())[:limit_matches]:
        p0 = rows[0]
        ev, side0, note = sa.find_event(p0[1], _real_opponent(p0[2]), d)
        for (pid, player, opp, cat, slug, otype, line, st, tries, md) in rows:
            enrich = _enrichment(player, _real_opponent(opp), cat, line, otype, picks, fwd, boards, book, md, st)
            score_text = ev.get("score") if ev is not None else None
            if ev is None:
                val, why = None, "no result yet: " + str(note)[:120]
            else:
                side = side0 if sa.same_player(player, ev.get("participant%d" % side0)) else (3 - side0)
                val, why = _value(cat, slug, ev, side, sa)
            if val is None and not str(why).startswith("VOID") and SETTLE_WH_FALLBACK:
                # APEX-SETTLE-FALLBACK-2026-11-04: no usable stats-site record -> final score from the warehouses
                fv, fwhy, ftext = _wh_value(cat, slug, player, _real_opponent(opp), d, "3")
                if fv is not None or str(fwhy).startswith("VOID"):
                    val, why, score_text = fv, fwhy, ftext
                elif ev is not None and "cancel" in str(ev.get("status") or "").lower() and \
                        str(md or st or "")[:10] < (now - timedelta(days=3)).date().isoformat():
                    val, why = None, "VOID (cancelled, no score anywhere after 3 days)"
            if val is None and not str(why).startswith("VOID"):
                c.execute("UPDATE projections SET settle_tries=COALESCE(settle_tries,0)+1, settle_note=?, score=? WHERE projection_id=?",
                          (str(why)[:150], score_text, pid))
                held += 1
                continue
            if val is None:
                res, mw, lw = "void", None, None
            else:
                res = "push" if val == line else ("more" if val > line else "less")
                mw, lw = int(val > line), int(val < line)
            c.execute("""UPDATE projections SET actual=?, score=?, result=?, more_won=?, less_won=?, settle_note=?,
                         settled_ts=?, engine_prob=?, engine_prob_cal=?, engine_direction=?, engine_tier=?,
                         engine_playable=?, engine_ts=?, engine_price_source=?, boards=?, board_reason=?, pinn_fav=?,
                         player_is_fav=?, bet365_line=COALESCE(bet365_line, ?),
                         bet365_over_prob=COALESCE(bet365_over_prob, ?), surface=?, best_of=?
                         WHERE projection_id=?""",
                      (val, score_text, res, mw, lw, why or "", now.isoformat(timespec="seconds"),
                       *enrich, pid))
            settled += 1
    c.commit()
    try:   # APEX-TENNISCLEAN-2026-11-12: copies, late lines, retired matches and two-collector conflicts never count
        import apex_props_clean as _pc
        _pc.clean(c)
    except Exception as _e:
        print("props clean failed: %s" % _e)
    c.close()
    return "props db: settled %d prop(s), %d still waiting, %d match(es) checked" % (settled, held, min(len(by_match), limit_matches))


def _enrichment(player, opp, cat, line, otype, picks, fwd, boards, book, match_date=None, start_utc=None):
    pair = frozenset((_norm(player), _norm(opp)))
    nm = "match" if cat in MATCH_LEVEL else _norm(player)
    k = (cat, pair, nm, float(line), _norm(otype))
    pk = sorted(picks.get(k) or [], key=lambda r: r.get("ts") or "")
    pk = pk[-1] if pk else {}
    b = boards.get(k) or {}
    f = fwd.get(pair) or {}
    # APEX-BET365-THREEPART-2026-10-22 (owner, "Go bet365"): bet365 total matched on both
    # names AND the match date (was names only -- a rematch could pick up an old total).
    bk = {}
    try:
        import apex_bet365_totals as b3
        bl_ = op_ = None
        for d in b3.row_dates(match_date, start_utc):
            bl_, op_ = b3.log_total([player, opp], d)
            if bl_ is not None:
                break
        if bl_ is not None:
            bk = {"book_line": bl_, "book_over_prob": op_}
    except Exception:
        pass
    def fl(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    is_fav = None
    if f:
        fav_name = _norm(f.get("player")) if f.get("player") != "match" else None
        if f.get("is_fav") not in (None, "") and fav_name:
            is_fav = int((fav_name == _norm(player)) == (str(f.get("is_fav")) in ("1", "True", "true")))
    return (fl(pk.get("prob")), fl(pk.get("prob_cal")), pk.get("direction"), pk.get("tier"), pk.get("playable"),
            pk.get("ts"), pk.get("price_source"), ",".join(sorted(b.get("on") or [])) or None, b.get("reason"),
            fl(f.get("pinn_fav")), is_fav, fl(bk.get("book_line")), fl(bk.get("book_over_prob")),
            f.get("surface"), f.get("best_of"))


def fill_bet365(days_back=10):
    """APEX-BET365-THREEPART-2026-10-22 (owner, "Go bet365"): copy bet365's total + over
    chance from the bet365 log into projections for upcoming AND graded-but-blank rows, matched
    on both players' names AND the match date. Never overwrites a filled value. Returns
    {match_date: rows filled}. Runs every hour with the snapshot / grading loop."""
    import apex_bet365_totals as b3
    b3.refresh()
    since = (_now() - timedelta(days=days_back)).date().isoformat()
    c = _conn()
    rows = c.execute("SELECT projection_id, player, opponent, match_date, start_utc FROM projections "
                     "WHERE bet365_line IS NULL AND match_date >= ? AND player NOT LIKE '%/%'", (since,)).fetchall()
    filled, ups = {}, []
    for pid, player, opp, md, st in rows:
        names = b3.pair_of({"player": player, "opponent": _real_opponent(opp)})
        bl = op = None
        for d in b3.row_dates(md, st):   # its match_date, else the UTC date of its start (same match)
            bl, op = b3.log_total(names, d)
            if bl is not None:
                break
        if bl is None:
            continue
        ups.append((bl, op, pid))
        filled[md] = filled.get(md, 0) + 1
    if ups:
        c.executemany("UPDATE projections SET bet365_line = ?, bet365_over_prob = COALESCE(bet365_over_prob, ?) "
                      "WHERE projection_id = ? AND bet365_line IS NULL", ups)
        c.commit()
    c.close()
    b3.refresh()
    try:
        for md, n in graded_bet365_from_sa365().items():
            filled[md] = filled.get(md, 0) + n
    except Exception as e:
        print("[PropsDB] graded bet365 lookup skipped: %s" % e)
    return filled


# APEX-NEVERPRICED-2026-10-23 (owner, "Go for everything"): a graded Total Games
# prop with no bet365 total gets ONE SportsAPI365 look at its finished match. bet365 total
# found -> filled (blanks only). bet365 posted no total for that match -> remembered here,
# and the "no bet365 total" warning counts it as "bet365 never priced" instead.
NEVER_PRICED = os.path.join(HERE, "data", "logs", "bet365_never_priced.json")


def never_priced():
    """{"YYYY-MM-DD|a|b": note} -- matches whose SA365 odds had no bet365 total. Never raises."""
    try:
        with open(NEVER_PRICED) as fh:
            return json.load(fh)
    except Exception:
        return {}


def gap_key(player, opponent, match_date):
    import apex_bet365_totals as b3
    a, b = sorted(b3.name_key(n) for n in b3.pair_of({"player": player, "opponent": _real_opponent(opponent)}))
    return "%s|%s|%s" % (str(match_date)[:10], a, b)


def _sheet_for(names, match_date):
    """SA365 event id of the saved box score with both players within a day of match_date."""
    import glob
    import apex_bet365_totals as b3
    want = {b3.name_key(n) for n in names}
    d0 = datetime.strptime(str(match_date)[:10], "%Y-%m-%d").date()
    for f in glob.glob(os.path.join(HERE, "data", "sa365_sheets", "*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
            if {b3.name_key(d["player1"]["name"]), b3.name_key(d["player2"]["name"])} != want:
                continue
            if abs((datetime.strptime(d["dateOnly"], "%Y-%m-%d").date() - d0).days) <= 1:
                return str((d.get("_event") or {}).get("id") or os.path.basename(f)[:-5])
        except Exception:
            continue
    return None


def graded_bet365_from_sa365(days=3, max_lookups=40):   # APEX-NEVERPRICED-NOSHEET-2026-10-29: 10 -> 40
    """{match_date: rows filled}. Looks only at graded Total Games rows still blank."""
    import apex_bet365_totals as b3
    import apex_book_lines as bl
    import apex_price_feed as pf
    pf.VERBOSE = False
    since = (_now() - timedelta(days=days)).date().isoformat()
    c = _conn()
    rows = c.execute("SELECT DISTINCT player, opponent, match_date FROM projections WHERE category = 'total_games' "
                     "AND settled_ts IS NOT NULL AND bet365_line IS NULL AND match_date >= ? "
                     "AND player NOT LIKE '%/%'", (since,)).fetchall()
    seen, filled, tries = never_priced(), {}, 0
    done = set(seen)
    for player, opp, md in rows:
        if player == "match":
            parts = str(opp).split(" v ")
            if len(parts) != 2:
                continue
            names = parts
        else:
            names = b3.pair_of({"player": player, "opponent": _real_opponent(opp)})
        k = gap_key(names[0], names[1], md)
        if k in done or tries >= max_lookups:
            continue
        done.add(k)
        eid = _sheet_for(names, md)
        if not eid:
            # APEX-NEVERPRICED-NOSHEET-2026-10-29 (owner, "Need these issue fix"): a graded
            # match with no SportsAPI365 box score on file is an ITF-level match bet365 never priced;
            # it used to stay a ⚠️ warning for 3 days. Count it, don't warn.
            seen[k] = "no SA365 box score on file: bet365 never priced (marked %s)" % _now().isoformat(timespec="minutes")
            continue
        tries += 1
        pm = pf._prematch(eid, True)
        if not isinstance(pm, dict) or not pm:
            continue
        ml = bl._main_line(pm)
        if not ml:
            seen[k] = "SA365 event %s: no bet365 total (checked %s)" % (eid, _now().isoformat(timespec="minutes"))
            continue
        line, o1, o2 = ml
        op = (1 / o1) / ((1 / o1) + (1 / o2))
        n = 0
        for pl2, op2 in ((names[0], names[1]), (names[1], names[0])):
            n += c.execute("UPDATE projections SET bet365_line = ?, bet365_over_prob = COALESCE(bet365_over_prob, ?) "
                           "WHERE category = 'total_games' AND match_date = ? AND bet365_line IS NULL "
                           "AND ((player = ? AND opponent = ?) OR (player = 'match' AND opponent = ?))",
                           (line, round(op, 4), md, pl2, op2, "%s v %s" % (pl2, op2))).rowcount
        filled[md] = filled.get(md, 0) + n
    c.commit()
    c.close()
    if len(seen) != len(never_priced()):
        os.makedirs(os.path.dirname(NEVER_PRICED), exist_ok=True)
        with open(NEVER_PRICED + ".tmp", "w") as fh:
            json.dump(seen, fh, indent=1)
        os.replace(NEVER_PRICED + ".tmp", NEVER_PRICED)
    b3.refresh()
    return filled


# ----------------------------------------------------------------------------- backfill + reports
def backfill():
    """Load what OpenClaw already has: every engine-evaluated prop since 8/24 (with
    its result where settled) and the board-watch first-seen log since 10/12."""
    c = _conn()
    n = 0
    fwd_act = {}
    for r in _read(FWD_LOG):
        if r.get("actual") not in (None, ""):
            try:
                fwd_act[(r.get("match_id"), _norm(r.get("player")), r.get("category"), float(r.get("line")))] = (float(r["actual"]), r.get("settled_ts"))
            except (TypeError, ValueError):
                pass
    picks, fwd, boards, book = _enrich_indexes()
    with c:
        for r in _read(PICK_LOG):
            try:
                line = float(r.get("line"))
            except (TypeError, ValueError):
                continue
            if r.get("player") == "match":
                a_b = str(r.get("opponent") or "").split(" v ")
                player, opp = (a_b + [""])[:2]
            else:
                player, opp = r.get("player"), r.get("opponent")
            otype = _norm(r.get("prop_type") or "") or "untyped"
            pid = "pl:%s|%s|%s|%s|%s|%s" % (r.get("match_date"), _norm(player), _norm(opp), r.get("category"), line, otype)
            act = fwd_act.get((r.get("match_id"), _norm(r.get("player")), r.get("category"), line))
            enrich = _enrichment(player, opp, r.get("category"), line, otype, picks, fwd, boards, book, r.get("match_date"))
            res = mw = lw = None
            if act:
                res = "push" if act[0] == line else ("more" if act[0] > line else "less")
                mw, lw = int(act[0] > line), int(act[0] < line)
            c.execute("""INSERT OR REPLACE INTO projections (projection_id, source, player, opponent, category,
                odds_type, line_first, line_last, line_at_start, start_utc, match_date, first_seen, last_seen,
                engine_prob, engine_prob_cal, engine_direction, engine_tier, engine_playable, engine_ts,
                engine_price_source, boards, board_reason, pinn_fav, player_is_fav, bet365_line, bet365_over_prob,
                surface, best_of, actual, result, more_won, less_won, settled_ts, settle_tries)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (pid, "picklog_backfill", player, opp, r.get("category"), otype, line, line, line,
                 r.get("slip_start_utc") or "", r.get("match_date"), r.get("ts"), r.get("ts"), *enrich,
                 act[0] if act else None, res, mw, lw, (act[1] or "backfill") if act else None))
            n += 1
    m = 0
    smap = _stat_map()
    with c:
        for r in _read(FIRSTSEEN_LOG):   # board watch since 10/12: every prop PrizePicks posted
            pid = str(r.get("projection_id") or "")
            if not pid:
                continue
            try:
                line = float(r.get("line"))
            except (TypeError, ValueError):
                continue
            slug = str(r.get("stat_type") or "")
            cur = c.execute("INSERT OR IGNORE INTO projections (projection_id, source, game_id, player, opponent, "
                            "stat_slug, category, odds_type, line_first, line_last, line_at_start, line_changes, "
                            "start_utc, match_date, first_seen, last_seen, settle_tries) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,0)",
                            (pid, "boardwatch_backfill", r.get("game_id"), r.get("player"), r.get("opponent"), slug,
                             smap.get(slug, slug), _norm(r.get("prop_type")) or "standard", line, line, line,
                             _iso_utc(r.get("start_time_raw")), r.get("match_date"), r.get("first_seen_ct"),
                             r.get("first_seen_ct")))
            m += cur.rowcount
    total = c.execute("SELECT COUNT(*) FROM projections").fetchone()[0]
    c.close()
    return ("props db: backfilled %d engine-evaluated prop rows (latest per identity) and %d board-watch props; "
            "%d props stored" % (n, m, total))


def stats():
    c = _conn()
    q = lambda s, *a: c.execute(s, a).fetchone()[0]
    out = ["props db: %d props (%d from the live board, %d backfilled), %d settled, %d line changes"
           % (q("SELECT COUNT(*) FROM projections"), q("SELECT COUNT(*) FROM projections WHERE source='partner_api'"),
              q("SELECT COUNT(*) FROM projections WHERE source!='partner_api'"),
              q("SELECT COUNT(*) FROM projections WHERE result IS NOT NULL"), q("SELECT COUNT(*) FROM changes"))]
    c.close()
    return out


def export(path):
    c = _conn()
    cur = c.execute("SELECT * FROM projections ORDER BY start_utc, player")
    cols = [d[0] for d in cur.description]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        n = 0
        for row in cur:
            w.writerow(row)
            n += 1
    c.close()
    return "exported %d props to %s" % (n, path)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "snapshot":
        print(snapshot())
    elif cmd == "settle":
        print(settle())
    elif cmd == "fillbet365":
        f = fill_bet365(int(sys.argv[2]) if len(sys.argv) > 2 else 10)
        print("bet365 totals filled: %d rows" % sum(f.values()) + "".join("\n  %s: %d" % kv for kv in sorted(f.items())))
    elif cmd == "backfill":
        print(backfill())
    elif cmd == "export":
        print(export(sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "data", "props_db_export.csv")))
    else:
        print("\n".join(stats()))
