"""APEX-SOON-2026-10-26 (owner: "I want a '/' for props that about to play, same rules
for the board, but it has the matches that about to play soonest when I press it").

/soon -- every pick that holds a spot on the current boards (a numbered pick or a numbered
BACKUP on Top 3, Elite 6, Best Goblin, Best Demon or Best Standard), listed by match start
time, soonest first. Same picks, same rules, same Ranking # as the boards; nothing is
re-ranked here. Picks whose match already started are left out. A pick whose line is not in
the newest PrizePicks pull gets a warning. Read-only: reads data/board_history.sqlite (the
last published PrizePicks board) and data/props_db.sqlite; writes nothing.
Switch: SOON_ON = False makes /soon say it is off."""
import os, sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
BOARD_DB = os.path.join(HERE, "data", "board_history.sqlite")
PROPS_DB = os.path.join(HERE, "data", "props_db.sqlite")
SOON_ON = True
SOON_LIMIT = 30            # most picks shown
# APEX-SOON-BEST-2026-10-26 (owner 3:22 PM: "show me the best of the best that's about to play
# soonest"): numbered board picks only, no backups. Set a board to 1+ to show its first backups again.
SOON_BACKUPS = {"top3": 0, "elite6": 0, "bestgoblin": 0, "bestdemon": 0, "beststandard": 0}
SOON_MAIN = {"top3": 3, "elite6": 6, "bestgoblin": 6, "bestdemon": 6, "beststandard": 6}   # spots that count, as posted
SOON_BOARDS = ("top3", "elite6", "bestgoblin", "bestdemon", "beststandard")
BOARD_NAMES = {"top3": "Top 3", "elite6": "Elite 6", "bestgoblin": "Best Goblin",
               "bestdemon": "Best Demon", "beststandard": "Best Standard"}
CATS = {"total_games": "Total Games", "fantasy_score": "Fantasy Score", "aces": "Aces",
        "dfs": "Double Faults", "games_won": "Games Won", "breaks": "Breaks"}
TYPE_EMOJI = {"goblin": "\U0001F7E2", "demon": "\U0001F534", "standard": "⚪"}
CT = ZoneInfo("America/Chicago")


def _ct(iso):
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso)[:25])
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(CT)
    except Exception:
        return None


def _when(ct, now):
    s = "%s %d/%d, %d:%02d %s CT" % (ct.strftime("%a"), ct.month, ct.day, ct.hour % 12 or 12,
                                      ct.minute, "AM" if ct.hour < 12 else "PM")
    mins = int((ct - now).total_seconds() // 60)
    if mins < 60:
        return "%s (in %d min)" % (s, max(mins, 0))
    if mins < 24 * 60:
        return "%s (in %dh %02dm)" % (s, mins // 60, mins % 60)
    return s


def _latest_board():
    """(published_at CT text, [rows]) from the newest PrizePicks publish."""
    con = sqlite3.connect("file:%s?mode=ro" % BOARD_DB, uri=True, timeout=30)
    try:
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT publish_id, published_at FROM publishes WHERE platform = 'prizepicks' "
                        "ORDER BY published_at DESC, publish_id DESC LIMIT 1").fetchone()
        if not r:
            return None, []
        rows = [dict(x) for x in con.execute(
            "SELECT * FROM history WHERE publish_id = ? AND spot_kind IN ('main', 'backup')", (r["publish_id"],))]
        return r["published_at"], rows
    finally:
        con.close()


def _live_lines():
    """{(names sorted, category, odds_type): {"lines": set, "start": iso}} from the newest PrizePicks pull, or None."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True, timeout=30)
    except Exception:
        return None
    try:
        mx = con.execute("SELECT MAX(last_seen) FROM projections").fetchone()[0]
        if not mx:
            return None
        out = {"_cats": set()}
        for pl, op, cat, typ, line, st in con.execute(
                "SELECT player, opponent, category, odds_type, line_last, start_utc FROM projections WHERE last_seen = ?", (mx,)):
            out["_cats"].add(cat)
            k = (tuple(sorted((_n(pl), _n(op)))), cat, (typ or "standard").lower())
            d = out.setdefault(k, {"lines": set(), "start": None})
            d["lines"].add(float(line))
            if st and (d["start"] is None or st < d["start"]):
                d["start"] = st
        return out
    finally:
        con.close()


def _n(s):
    return " ".join(str(s or "").lower().split())


def _live_key(row):
    names = str(row["opponent"]).split(" v ", 1) if row["player"] == "match" else [row["player"], row["opponent"]]
    return (tuple(sorted(_n(x) for x in names)), row["category"], (row.get("prop_type") or "standard").lower())


def _pp_note(row, live):
    """'' when the line is in the newest pull; else a plain warning (moved to X / not offered)."""
    if live is None or row["category"] not in live.get("_cats", ()):
        return ""   # that prop type wasn't part of the newest pull at all, so no verdict
    d = live.get(_live_key(row))
    if d and float(row["line"]) in d["lines"]:
        return ""
    if d and d["lines"]:
        return "\u26A0 PrizePicks now shows %s, not %g -- check the app before you play it" % (
            " / ".join("%g" % x for x in sorted(d["lines"])), float(row["line"]))
    return "\u26A0 This line is not in the newest PrizePicks pull -- check the app, it may have been pulled"


def _pp_start(row, live):
    d = live.get(_live_key(row)) if live else None
    return d["start"] if d else None


def _same_prop(r):
    """One key per prop even when the board lists the match with the names flipped."""
    names = str(r["opponent"]).split(" v ", 1) if r["player"] == "match" else [r["player"], r["opponent"]]
    return (r["player"] == "match", tuple(sorted(_n(x) for x in names)), r["category"],
            r["direction"], float(r["line"]), (r.get("prop_type") or "").lower())


def _who(row):
    if row["player"] == "match":
        return str(row["opponent"])
    return "%s v %s" % (row["player"], row["opponent"])


SOON_HOURS = 6             # "soonest" window (owner 3:31 PM: "Let's make it 6hrs"); strict -- never widened
SOON_SHOW = 7              # the best of the best, best chance first


def soon_text(limit=SOON_SHOW):
    """APEX-SOON-BOARDSTYLE-2026-10-26 (owner 3:34 PM: "it should show just like the rest of the
    board"): /soon is the Elite 6 board builder itself (apex_edge_tracker.elite6_detail: same bars, holds,
    one-per-match, top-slot rule, Ranking #, card lines) run on only the props whose match starts inside the
    next SOON_HOURS, showing up to SOON_SHOW numbered picks. Never padded; names the next match after the
    window. Read-only, logs nothing."""
    if not SOON_ON:
        return "/soon is switched off."
    import apex_edge_tracker as _aet
    now = datetime.now(CT)
    edge = now.timestamp() + SOON_HOURS * 3600
    picks, note = _aet.load_current_board()
    if note:
        return note
    inside, later, started = [], [], 0
    for p in picks:
        ct = _ct(_aet._prop_start_utc(p))
        if ct is None:
            continue
        if ct <= now:
            started += 1
            continue
        (inside if ct.timestamp() <= edge else later).append((ct, p))
    head = ["\u23F1 **NEXT UP -- NEXT %d HOURS** -- %s" % (SOON_HOURS, now.strftime("%Y-%m-%d")),
            "_Only matches starting in the next %d hours (by %d:%02d %s CT), ranked exactly like the boards: best chance first, "
            "one pick per match, never padded._" % (
                SOON_HOURS, (now.hour + SOON_HOURS) % 12 or 12, now.minute, "AM" if ((now.hour + SOON_HOURS) % 24) < 12 else "PM")]
    # APEX-SOONFIRSTSET-2026-11-04: first-set picks inside the window count as picks too
    _fs = _fs_window_entries(datetime.now(timezone.utc), edge) if SOON_FIRST_SET else []
    if not inside and not _fs:
        L = head + ["No board pick starts in the next %d hours." % SOON_HOURS]
        if later:
            later.sort(key=lambda x: x[0])
            L.append("Next one up: %s at %s." % (_who_p(later[0][1]), _when(later[0][0], now)))
        return "\n".join(L)
    _, txt = soon_board("elite6", [p for _, p in inside], limit)
    lines = txt.split("\n")
    # swap the Elite 6 header for ours; everything under it is the board's own card style
    k = next((n for n, x in enumerate(lines) if "APEX ELITE 6" in x), None)
    if k is not None:
        lines[k] = head[0]
        if k + 1 < len(lines) and lines[k + 1].startswith("_"):
            lines[k + 1] = head[1]
        else:
            lines.insert(k + 1, head[1])
    lines = [x.replace("Elite 6 bar", "bar").replace("Elite6 Qualifies", "Next Up qualifies") for x in lines]
    if later:
        later.sort(key=lambda x: x[0])
        lines.append("\u23ED After the %d-hour window, next one up: %s at %s." % (SOON_HOURS, _who_p(later[0][1]), _when(later[0][0], now)))
    return "\n".join(lines)


# APEX-SOONFIRSTSET-2026-11-04 (owner, "Go soon first set"): /soon and the soon slips ran the board builder
# straight, so the strict first-set picks never reached them -- the posted boards merge those only in
# apex_edge_tracker._rerank_after_top3 (the /fetch publish path). soon_board() runs that SAME rerank step on the
# window's props plus the first-set picks that start inside the window, then renders the cards the board's own way.
# Read-only: the order guard's rank pool and the cushion block list are put back exactly as they were.
# Undo: SOON_FIRST_SET = False (or "go back to before-soon-first-set").
SOON_FIRST_SET = True


def _utc(iso):
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _fs_window_entries(now_utc, edge):
    """First-set board entries whose match starts after now and inside the window. Never raises."""
    try:
        import apex_set_games as _sg
        out = []
        for x in _sg.board_entries():
            d = _utc((x.get("p") or {}).get("slip_start_utc"))
            if d is not None and now_utc < d and d.timestamp() <= edge:
                out.append(x)
        return out
    except Exception:
        return []


def _fs_only(t, fs, limit):
    """Window with no regular pick: the first-set picks alone, Ranking order, one per match (same note as the boards)."""
    try:
        import apex_player_checks as _pc
        fs = [x for x in fs if not _pc.disqualified(x.get("p") or {})]
    except Exception:
        pass
    out, used = [], set()
    for d in sorted(fs, key=lambda x: -float(x.get("score") or 0)):
        nm = t._prop_names(d.get("p") or {})
        if nm & used:
            continue
        used |= nm
        d = dict(d)
        p = d.get("p") or {}
        d["rerank_note"] = "\U0001F3BE #%d by Ranking %d, from this rule's long-run first-set record (no signal points yet)" % (
            len(out) + 1, round(100 * t._board_prob(p)))
        d["rerank_note"] += (". Engine: %d for this match (breaks ties)" % round(100 * p["fs_engine"])) if p.get("fs_engine") \
            else ". Engine has no number for this match, so the closest match breaks ties"
        out.append(d)
        if len(out) >= limit:
            break
    return out


def _render_board(t, flag, scored, base):
    """Header block of the builder's own text, then each pick's card the board way, then its HELD BACK tail."""
    lines = base.split("\n") if base else []
    hb = next((i for i, x in enumerate(lines) if x.startswith("━")), None)
    ti = next((i for i, x in enumerate(lines) if x.startswith("─")), None)
    if hb is not None:
        head = lines[:hb + 1]
    else:
        day = datetime.now(CT).strftime("%Y-%m-%d")
        head = (["\U0001F3C6 **APEX ELITE 6** -- %s" % day, "_The 6 strongest plays on the entire board -- no category quota, no padding._"]
                if flag != "top3" else ["\U0001F3C6 **TOP 3 -- %s**" % day, "_The 3 strongest plays on the board._"])
        head.append("━" * 22)
    tail = lines[ti:] if ti is not None else []
    at = 2 if len(head) > 2 else len(head) - 1
    try:
        if t.SCORE_DISPLAY and not any(str(x).startswith(t.SCORE_EXPLAIN[:10]) for x in head):
            head.insert(at, t.SCORE_EXPLAIN)
        if t.RERANK_AFTER_TOP3 and t.NET_SIGNAL_POINTS and not any(str(x).startswith("\U0001F4CB Order") for x in head):
            head.insert(at, t.order_header(flag))
    except Exception:
        pass
    L = list(head)
    if not scored:
        L.append("No pick clears the bar in this window.")
    else:
        rec = t.records()
        _f1, _f2, _ = t._load_freshness_index()
        medals = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}
        sdec = t.score_decimals([t.score_value(flag, e.get("p") or {}) for e in scored])
        for i, e in enumerate(scored, 1):
            L.extend(t._render_pick_card(e, i, rec, _f1, _f2, medals, worst=False, show_debug=False,
                                         flag=flag, score_dec=sdec[i - 1]))
    return "\n".join(L + tail)


def soon_board(flag, inside, limit, now_utc=None):
    """(scored, text) for one soon board: the board builder on the window's props, the posted boards' rerank step
    (signal points, bars, cushion block, first-set picks inside the window), one pick per match, up to `limit`."""
    import apex_edge_tracker as t
    now_utc = now_utc or datetime.now(timezone.utc)
    edge = now_utc.timestamp() + SOON_HOURS * 3600
    if flag == "top3":
        res = t.best_bets_detail(inside, limit=limit, exclude_fantasy_score=True, return_scored=True)
        wide = lambda: t.best_bets_detail(inside, limit=25, exclude_fantasy_score=True, return_scored=True)
    else:
        res = t.elite6_detail(inside, limit=limit, exclude_fantasy_score=True, return_scored=True)
        wide = lambda: t.elite6_detail(inside, limit=15, exclude_fantasy_score=True, return_scored=True)
    if not isinstance(res, dict):
        res = {"scored": [], "text": str(res)}
    base = res.get("text") or ""
    if not SOON_FIRST_SET:
        return list(res.get("scored") or [])[:limit], base
    fs = _fs_window_entries(now_utc, edge)
    if not (res.get("scored") or []):
        scored = _fs_only(t, fs, limit) if fs else []
        return scored, _render_board(t, flag, scored, base)
    import apex_set_games as _sg
    orig_be = _sg.board_entries
    pools = dict(t.LAST_RANK_POOLS)
    cush = None
    try:
        with open(t.CUSHION_BLOCK_PATH, "rb") as fh:
            cush = fh.read()
    except Exception:
        pass
    lim0 = t._BOARD_LIMITS.get(flag)
    try:
        _sg.board_entries = lambda: list(fs)
        t._BOARD_LIMITS[flag] = limit
        t._rerank_after_top3(res, wide, None, flag=flag)
    finally:
        _sg.board_entries = orig_be
        if lim0 is None:
            t._BOARD_LIMITS.pop(flag, None)
        else:
            t._BOARD_LIMITS[flag] = lim0
        t.LAST_RANK_POOLS.clear()
        t.LAST_RANK_POOLS.update(pools)
        if cush is not None:
            try:
                tmp = t.CUSHION_BLOCK_PATH + ".soon.tmp"
                with open(tmp, "wb") as fh:
                    fh.write(cush)
                os.replace(tmp, t.CUSHION_BLOCK_PATH)
            except Exception:
                pass
    scored = list(res.get("scored") or [])[:limit]
    return scored, _render_board(t, flag, scored, base)


def _who_p(p):
    return str(p.get("opponent")) if p.get("player") == "match" else "%s v %s" % (p.get("player"), p.get("opponent"))


if __name__ == "__main__":
    print(soon_text())
