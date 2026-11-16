#!/usr/bin/env python3
import re
"""
APEX-BOARDHISTORY-2026-10-25 (owner: "I would like to keep track on all data what line was
where and why it change to where it change with all the stats and data information on all props
and picks so it can be studied later on."):

BOARD HISTORY -- a permanent record of every board publish. DATA ONLY: nothing here changes a
board, a rank, a card or a Discord post. Rows are only ever added (results are filled in later);
no row is ever deleted.

On every PrizePicks publish (odds watch / auto-eval, hourly re-price, 5 PM run, /fetch, manual,
restart, category gate) apex_edge_tracker.write_board_status calls log_publish(). It writes one row
per prop per board:
  boards   top3, elite6, top10, bestgoblin, bestdemon, beststandard -- numbered spots (#1..),
           BACKUP n (numbered exactly like the card), HELD (rule holds), and "not on board" for a
           prop that was on that board at the last publish and is gone now
  allprops every prop in the pull: All Props spot for 70%+ props, else "not on board" + reason
ParlayPlay writes its own rows (board "parlayplay") each time its board posts (log_pp_publish).
A PrizePicks line pulled after a fetch writes rows when the pulled pick's spot is filled
(log_pulled_fill, trigger "pulled-line fill").

Each row: publish time + trigger, board, spot, player/opponent/match start, PP type, line, MORE/LESS,
Win Prob, Apex, Honest, board rank score, every signal with its points (💵 ParlayPlay price,
📏 bet365 room, 🏟 blowouts, ⚖ favorite, 📅 played yesterday, 🎯 17.5 check, not-enough-data),
bet365 total + room, book chance, favorite %, ParlayPlay price, hold/skip reason, the previous
spot/line/Win Prob from the last publish, and a plain "why it changed". Results (games, score,
hit/miss/push/void) are written onto the rows once the prop settles (fill_results, run at every
publish and by the hourly settle job).

Saved in data/board_history.sqlite, plus a daily CSV in data/board_history/board_history_<day>.csv.

  python3 apex_board_history.py backfill        # rebuild history from the stored board logs (once)
  python3 apex_board_history.py fill            # write settled results onto history rows
  python3 apex_board_history.py export [day]    # write the daily CSV (default: today)
  python3 apex_board_history.py show <name> [day] [board]   # one prop's history in plain words
Undo: BOARD_HISTORY_ON = False (stops new rows; the file stays).
"""
import csv, json, os, sqlite3, sys, unicodedata
from datetime import datetime, date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "board_history.sqlite")
CSV_DIR = os.path.join(HERE, "data", "board_history")
PROPS_DB = os.path.join(HERE, "data", "props_db.sqlite")
PP_DB = os.path.join(HERE, "ApexParlayPlay", "data", "parlayplay_db.sqlite")
STATUS_LOG = os.path.join(HERE, "apex_board_status_log_v1.csv")
MEMBERSHIP_LOG = os.path.join(HERE, "apex_board_membership_log_v1.csv")

BOARD_HISTORY_ON = True
BOARDS = ("top3", "elite6", "top10", "bestgoblin", "bestdemon", "beststandard")
BOARD_NAMES = {"top3": "Top 3", "elite6": "Elite 6", "top10": "Top 10", "bestgoblin": "Best Goblin",
               "bestdemon": "Best Demon", "beststandard": "Best Standard", "allprops": "All Props",
               "parlayplay": "ParlayPlay"}
TRIGGERS = {"autoeval": "auto-eval / odds watch", "auto": "5 PM run", "5pm_fetch": "5 PM run",
            "manual": "/fetch", "restart": "restart", "reprice": "hourly re-price (fresh odds)",
            "category_gate": "category gate", "republish": "republish", "manual-republish": "manual republish",
            "pulled_fill": "pulled-line fill", "parlayplay": "ParlayPlay read"}
WP_MOVE = 0.01   # a Win Prob move of 1 point or more is a reason by itself

COLS = ["publish_id", "published_at", "trigger", "source", "board_day", "board", "spot", "spot_kind", "spot_no",
        "player", "opponent", "match_start", "category", "prop_type", "direction", "line",
        "win_prob", "apex_score", "honest", "rank_score", "net_signals",
        "sig_pp", "sig_room", "sig_blowout", "sig_fav", "sig_played", "line175", "line175_points", "not_enough_data",
        "bet365_total", "room", "book_chance", "favorite_pct", "pp_price",
        "reason", "prev_spot", "prev_line", "prev_win_prob", "why_changed",
        "origin", "prop_key", "market_key", "pp_key",
        "result_games", "result_score", "result", "settled_at"]
_NUM = {"publish_id", "spot_no", "line", "win_prob", "apex_score", "honest", "rank_score", "net_signals",
        "sig_pp", "sig_room", "sig_blowout", "sig_fav", "sig_played", "line175_points", "bet365_total", "room",
        "book_chance", "favorite_pct", "pp_price", "prev_line", "prev_win_prob", "result_games"}


# ------------------------------------------------------------------ storage
def _db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS publishes (publish_id INTEGER PRIMARY KEY AUTOINCREMENT, published_at TEXT, "
              "trigger TEXT, source TEXT, board_day TEXT, platform TEXT, n_rows INTEGER, origin TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, %s)"
              % ", ".join("%s %s" % (k, "REAL" if k in _NUM else "TEXT") for k in COLS))
    c.execute("CREATE INDEX IF NOT EXISTS ix_h_pub ON history(publish_id)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_h_prop ON history(prop_key, board, published_at)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_h_open ON history(result, match_start)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_h_day ON history(published_at)")
    return c


def _insert(c, pub, rows):
    cur = c.execute("INSERT INTO publishes (published_at, trigger, source, board_day, platform, n_rows, origin) "
                    "VALUES (?,?,?,?,?,?,?)", (pub["published_at"], pub["trigger"], pub["source"], pub["board_day"],
                                                pub["platform"], len(rows), pub["origin"]))
    pid = cur.lastrowid
    for r in rows:
        r.update(publish_id=pid, published_at=pub["published_at"], trigger=pub["trigger"], source=pub["source"],
                 board_day=pub["board_day"], origin=pub["origin"])
    c.executemany("INSERT INTO history (%s) VALUES (%s)" % (", ".join(COLS), ", ".join("?" * len(COLS))),
                  [tuple(r.get(k) for k in COLS) for r in rows])
    return pid


def _last_rows(c, platform, before=None):
    """{board: {prop_key: row}} and {prop_key: row on allprops} from the newest publish of this platform."""
    q = "SELECT publish_id FROM publishes WHERE platform = ?" + (" AND published_at < ?" if before else "") + \
        " ORDER BY published_at DESC, publish_id DESC LIMIT 1"
    r = c.execute(q, (platform, before) if before else (platform,)).fetchone()
    out = {}
    if not r:
        return out
    c.row_factory = sqlite3.Row
    try:
        for x in c.execute("SELECT * FROM history WHERE publish_id = ?", (r[0],)):
            out.setdefault(x["board"], {})[x["prop_key"]] = dict(x)
    finally:
        c.row_factory = None
    return out


# ------------------------------------------------------------------ prop helpers
def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower().strip()


def _names(p):
    if p.get("player") == "match":
        return [x.strip() for x in str(p.get("opponent") or "").replace(" vs ", " v ").split(" v ") if x.strip()]
    return [x for x in (p.get("player"), p.get("opponent")) if x]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def prop_key(p):
    return "|".join([_norm(p.get("player")), _norm(p.get("opponent")), str(p.get("category") or ""),
                     str(p.get("prop_type") or "standard").lower(), str(p.get("direction") or "").upper(),
                     "%g" % _f(p.get("line")) if _f(p.get("line")) is not None else str(p.get("line"))])


def market_key(p):
    """Same match + market + type + side, ignoring the line (to spot line moves)."""
    who = ",".join(sorted(_norm(n) for n in _names(p))) if p.get("player") == "match" else \
        _norm(p.get("player")) + ">" + _norm(p.get("opponent"))
    return "|".join([who, str(p.get("category") or ""), str(p.get("prop_type") or "standard").lower(),
                     str(p.get("direction") or "").upper()])


def _who(p):
    return " v ".join(n.split()[-1] for n in _names(p)) if p.get("player") == "match" else \
        (str(p.get("player") or "").split() or [""])[-1]


def _short(p, line=True):
    return "%s %s%s" % (_who(p), {"total_games": "TG", "fantasy_score": "Fantasy", "dfs": "DF", "games_won": "GW",
                                  "aces": "Aces", "breaks": "Breaks"}.get(p.get("category"), p.get("category")),
                        (" %g" % _f(p.get("line"))) if line and _f(p.get("line")) is not None else "")


def signals(p):
    """Every card signal with its points, the same verdicts apex_card_signals.net_score adds up. Never raises."""
    out = {}
    try:
        import apex_card_signals as cs
        g, b, net, avail = cs.net_score(p)
        out["net_signals"] = net
        tg_more = p.get("category") == "total_games" and str(p.get("direction") or "").upper() == "MORE"
        names = cs._names(p)
        line = _f(p.get("line"))
        if tg_more and line is not None:
            m = cs._cached("pp", cs._pp_ladder).get((frozenset(cs._norm(x) for x in names), line))
            if cs._is_pp(p) and p.get("mult"):
                m = _f(p.get("mult"))
            if m:
                out["pp_price"] = m
                out["sig_pp"] = 1 if m <= 1.15 else (-1 if m > 1.30 else 0)
            bk = cs._book_for(p, names)
            if bk:
                out["bet365_total"], out["room"] = bk, round(bk - line, 2)
                out["sig_room"] = 1 if bk - line >= 3 else -1
            try:
                import apex_blowout_signal as bs
                rb = bs.recent_blowouts(p)
                if len(rb) == 2:
                    sh = sum(x / float(n) for _, x, n in rb) / 2.0
                    out["sig_blowout"] = -(2 if cs.BLOWOUT_DOUBLE else 1) if sh > bs.RECENT_WARN else 1
            except Exception:
                pass
            _src, fav = cs._cached("picks", cs._pick_rows).get(
                (p.get("match_id"), p.get("category"), str(p.get("line")), str(p.get("prop_type") or "").lower()), ("", ""))
            fav = _f(cs._fav_for(p, fav))
            if fav:
                out["favorite_pct"] = round(100 * fav, 1)
                out["sig_fav"] = cs.fav_verdict_for(p, fav)
        elif p.get("category") == "total_games":
            bk = cs.book_total(p)
            if bk and line is not None:
                out["bet365_total"], out["room"] = bk, round(bk - line, 2)
        out["sig_played"] = 1 if cs.played_yesterday(p) else 0
        txt = cs.line175_check(p)
        if txt:
            out["line175"] = "skip" if "❌ skip" in txt else ("no data" if "not enough data" in txt else "play")
            out["not_enough_data"] = "yes" if out["line175"] == "no data" else ""
    except Exception:
        pass
    try:
        import apex_edge_tracker as t
        pts, _k = t.line175_penalty(p)
        out["line175_points"] = -round(100 * pts) if pts else 0
    except Exception:
        pass
    if p.get("apex_book_prob") not in (None, ""):
        out["book_chance"] = round(100 * float(p["apex_book_prob"]), 1)
    if p.get("mult") and "pp_price" not in out and str(p.get("prop_type") or "").lower() == "parlayplay":
        out["pp_price"] = _f(p.get("mult"))
    return out


_PROPS = {}   # APEX-ORDERGUARD-2026-10-25: prop_key -> (prop, Apex) for the publish being written


def _order_check(platform, pid, trigger, rows):
    """APEX-ORDERGUARD-2026-10-25: the ORDER CHECK on this publish (check only, never changes a board)."""
    try:
        import apex_order_guard as og
    except Exception as ex:
        try:
            import apex_ops as ops
            ops.send_discord("alerts", "\U0001F6A8 ORDER CHECK could not run: the checker won't load (%s: %s)" % (
                type(ex).__name__, str(ex)[:100]))
        except Exception:
            pass
        return
    og.check_publish(platform, pid, trigger, rows, dict(_PROPS))


def _base_row(p, board, kind, no, reason, apex=None, rank=None, flag_for_rank=None, sig_cache=None):
    import apex_edge_tracker as t
    pr = t._board_prob(p)
    hp = t.corrected_prob(pr)
    if rank is None:
        try:
            rank = t.board_rank_of(flag_for_rank or board, p)
        except Exception:
            rank = None
    k = prop_key(p)
    _PROPS[k] = (p, _f(apex))   # APEX-ORDERGUARD-2026-10-25: the order check re-ranks these
    sig = sig_cache.get(k) if sig_cache is not None and k in sig_cache else signals(p)
    if sig_cache is not None:
        sig_cache[k] = sig
    spot = {"main": "#%s" % no, "backup": "BACKUP %s" % no, "held": "HELD", "off": "not on board"}[kind]
    r = {"board": board, "spot": spot, "spot_kind": kind, "spot_no": no,
         "player": p.get("player"), "opponent": p.get("opponent"), "match_start": t._prop_start_utc(p) or "",
         "category": p.get("category"), "prop_type": str(p.get("prop_type") or "standard").lower(),
         "direction": str(p.get("direction") or "").upper(), "line": _f(p.get("line")),
         "win_prob": round(100 * pr, 2) if pr is not None else None,
         "apex_score": _f(apex), "honest": round(100 * hp, 2) if hp is not None else None,
         "rank_score": round(100 * rank, 2) if rank is not None else None,
         "reason": reason or "", "prop_key": k, "market_key": market_key(p)}
    r.update(sig)
    return r


# ------------------------------------------------------------------ why it changed
def _ahead(rows):
    """Order of a board's numbered + backup rows (lower = better), by prop key."""
    seq = [r for r in rows if r["spot_kind"] == "main"] + [r for r in rows if r["spot_kind"] == "backup"]
    seq.sort(key=lambda r: (0 if r["spot_kind"] == "main" else 1, r["spot_no"] or 0))
    return {r["prop_key"]: i for i, r in enumerate(seq)}


def _wp_words(a, b, source):
    after = {"reprice": "after the hourly re-price (fresh odds)", "autoeval": "after an odds update",
             "manual": "after the /fetch re-score", "restart": "after a re-score"}.get(source, "after a re-score")
    return "Win Prob changed %d→%d %s" % (round(a), round(b), after)


def explain(board, rows, prev_board, prev_all, cur_all, source):
    """Fills prev_spot / prev_line / prev_win_prob / why_changed on this board's rows and returns the
    'not on board' rows for props that left this board since the last publish."""
    prev_board = prev_board or {}
    cur_by_key = {r["prop_key"]: r for r in rows}
    now_order, old_order = _ahead(rows), _ahead(list(prev_board.values()))
    cur_mkts = {}
    for r in (cur_all or {}).values():
        cur_mkts.setdefault(r["market_key"], set()).add(r["line"])
    old_mkts = {}
    for r in (prev_all or {}).values():
        old_mkts.setdefault(r["market_key"], set()).add(r["line"])

    def _line_move(r, gone=False):
        old = old_mkts.get(r["market_key"], set()) if not gone else {r["line"]}
        new = cur_mkts.get(r["market_key"], set()) if gone else {r["line"]}
        lost = sorted(x for x in old if x not in cur_mkts.get(r["market_key"], set()) and x is not None)
        came = sorted(x for x in new if x not in old_mkts.get(r["market_key"], set()) and x is not None)
        if lost and came:
            return "line moved %g→%g" % (lost[0], came[0])
        return None

    for r in rows:
        pv = prev_board.get(r["prop_key"])
        if pv:
            r.update(prev_spot=pv["spot"], prev_line=pv["line"], prev_win_prob=pv["win_prob"])
        else:
            pa = (prev_all or {}).get(r["prop_key"])
            r.update(prev_spot="not on board" if pa or prev_all else "", prev_line=pa["line"] if pa else None,
                     prev_win_prob=pa["win_prob"] if pa else None)
        r["why_changed"] = _why_one(board, r, pv, prev_all, now_order, old_order, cur_by_key, prev_board,
                                    source, _line_move)
    gone = []
    for k, pv in prev_board.items():
        if k in cur_by_key or pv["spot_kind"] == "off":
            continue
        ca = (cur_all or {}).get(k)
        if ca is None:
            # APEX-STARTEDLABEL-2026-11-01 (owner): a started match's lines leave the feed; that isn't a pull.
            # Undo: STARTED_LABEL = False.
            _ms = pv.get("match_start") or ""
            if STARTED_LABEL and _ms and _ms <= datetime.utcnow().isoformat():
                why = "match started"
            else:
                why = _line_move(pv, gone=True) or _pulled_word(pv)   # APEX-LIVECHECK-2026-11-01
        else:
            ms = ca.get("match_start") or ""
            if ms and ms <= datetime.utcnow().isoformat():
                why = "match started"
            elif ca.get("reason"):
                why = ca["reason"]
                if ca.get("win_prob") is not None and pv.get("win_prob") is not None and \
                        abs(ca["win_prob"] - pv["win_prob"]) >= 100 * WP_MOVE:
                    why = "%s (%s)" % (why, _wp_words(pv["win_prob"], ca["win_prob"], source))
            else:
                why = "no longer qualifies for this board"
        g = dict(ca or pv)
        g.update(board=board, spot="not on board", spot_kind="off", spot_no=None, prev_spot=pv["spot"],
                 prev_line=pv["line"], prev_win_prob=pv["win_prob"], why_changed="left the board: " + why,
                 result_games=None, result_score=None, result=None, settled_at=None)
        if ca is None:
            g["reason"] = why
        gone.append(g)
    return gone


def _why_one(board, r, pv, prev_all, now_order, old_order, cur_by_key, prev_board, source, line_move):
    if not prev_all and not prev_board:
        return "first publish on record"
    if r["spot_kind"] == "off" and board == "allprops":
        pa = (prev_all or {}).get(r["prop_key"])
        if pa is None:
            return line_move(r) or "new prop scored"
        if pa["spot_kind"] != "off":
            return "dropped off All Props: " + (r.get("reason") or "")
        if pa.get("win_prob") is not None and r.get("win_prob") is not None and \
                abs(r["win_prob"] - pa["win_prob"]) >= 100 * WP_MOVE:
            return _wp_words(pa["win_prob"], r["win_prob"], source)
        return "no change"
    if pv is None or (pv.get("spot_kind") == "off" and r["spot_kind"] != "off"):
        pa = pv or (prev_all or {}).get(r["prop_key"])
        if pa is None:
            return line_move(r) or "new prop scored"
        if r["spot_kind"] == "held":
            return "held: " + (r.get("reason") or "").replace("held back: ", "")
        if pa.get("win_prob") is not None and r.get("win_prob") is not None and \
                abs(r["win_prob"] - pa["win_prob"]) >= 100 * WP_MOVE:
            return "came onto this board: " + _wp_words(pa["win_prob"], r["win_prob"], source)
        if pa.get("reason") and pa.get("spot_kind") == "off":
            return "came onto this board (last time: %s)" % pa["reason"]
        return "came onto this board"
    same = pv["spot"] == r["spot"]
    wp_moved = pv.get("win_prob") is not None and r.get("win_prob") is not None and \
        abs(r["win_prob"] - pv["win_prob"]) >= 100 * WP_MOVE
    if pv.get("match_start") and r.get("match_start") and pv["match_start"][:16] != r["match_start"][:16]:
        tail = "" if same else " (now %s)" % r["spot"]
        return "match moved %s→%s%s" % (pv["match_start"][:16].replace("T", " "),
                                             r["match_start"][:16].replace("T", " "), tail)
    if same:
        return ("no change (%s)" % _wp_words(pv["win_prob"], r["win_prob"], source)) if wp_moved else "no change"
    if r["spot_kind"] == "held":
        return "held: " + (r.get("reason") or "").replace("held back: ", "")
    if pv["spot_kind"] == "held":
        return "hold lifted (was: %s)" % (pv.get("reason") or "held")
    k = r["prop_key"]
    nw, ol = now_order.get(k), old_order.get(k)
    parts = []
    if wp_moved:
        parts.append(_wp_words(pv["win_prob"], r["win_prob"], source))
    if nw is not None and ol is not None and nw > ol:   # pushed down
        jumped = [cur_by_key[x] for x, i in now_order.items() if i < nw and x != k and
                  (old_order.get(x) is None or old_order[x] > ol)]
        if jumped:
            top = max(jumped, key=lambda x: x.get("rank_score") or 0)
            src = "" if top["prop_key"] in prev_board else (" (%s)" % (top.get("why_changed") or "new here"))
            parts.append("pushed down by %s (rank %s vs %s)%s" % (
                _short({"player": top["player"], "opponent": top["opponent"], "category": top["category"],
                        "line": top["line"]}), _r(top.get("rank_score")), _r(r.get("rank_score")), src))
    elif nw is not None and ol is not None and nw < ol:   # moved up
        left = [prev_board[x] for x, i in old_order.items() if i < ol and x != k and
                (now_order.get(x) is None or now_order[x] > nw)]
        if left:
            x = left[0]
            xr = cur_by_key.get(x["prop_key"])
            where = ("left the board" if xr is None else "dropped to %s" % xr["spot"])
            parts.append("moved up: %s %s" % (_short({"player": x["player"], "opponent": x["opponent"],
                                                      "category": x["category"], "line": x["line"]}), where))
    if not parts:
        parts.append("rank order changed (rank %s, was spot %s)" % (_r(r.get("rank_score")), pv["spot"]))
    return "; ".join(parts)


def _r(x):
    return "?" if x is None else "%d" % round(x)


# ------------------------------------------------------------------ PrizePicks publish
def _allprops_rows(picks, rec, status_all, sig_cache):
    """Every prop in the pull: All Props spot (same order /allprops uses) for 70%+ props, else not on board."""
    import apex_edge_tracker as t
    rows, seen, q = [], set(), []
    for p in picks:
        k = prop_key(p)
        if k in seen or not p.get("category"):
            continue
        seen.add(k)
        st = status_all.get(t._apex_prop_identity(p)) or ("", "", None)
        if t._is_unplayable(p):
            rows.append(_base_row(p, "allprops", "off", None, st[1] or "unplayable direction", st[2],
                                  flag_for_rank="top3", sig_cache=sig_cache))
            continue
        pr = t._board_prob(p)
        if pr >= t.BOARD_MIN_PROB - 1e-9 and not t._pub_match_started(p):
            try:
                sc = t.apex_score(p.get("prob_cal", p.get("prob")), p["category"], bool(p.get("bo5")), rec)[0]
            except Exception:
                sc = st[2]
            q.append((p, sc, st))
        else:
            rows.append(_base_row(p, "allprops", "off", None, st[1] or "Win Probability under 70%", st[2],
                                  flag_for_rank="top3", sig_cache=sig_cache))
    q.sort(key=lambda x: (-t.board_rank_score(x[0], t._board_prob(x[0])), -float(x[1] or 0)))
    for i, (p, sc, st) in enumerate(q, 1):
        why = t.board_hold_reason(p) or ""
        rows.append(_base_row(p, "allprops", "main", i, ("held: " + why) if why else (st[1] or ""), sc,
                              flag_for_rank="top3", sig_cache=sig_cache))
    return rows


def _board_rows(flag, res, rec, sig_cache):
    import apex_edge_tracker as t
    res = res if isinstance(res, dict) else {}
    entries = list(res.get("scored") or [])
    limit = t._PUBLISHED_FLAG_LIMIT.get(flag, len(entries))
    # APEX-ALERTCARDS-2026-10-25: Best Goblin shows every 75%+ Goblin (it ran to 7 at 5:13 PM 10/25),
    # so record every spot the card shows -- recording 6 made the ORDER CHECK say "posted 6, 7 qualify"
    if flag == "bestgoblin" and getattr(t, "BEST_GOBLIN_SHOW_ALL_MIN_PROB", None):
        limit = max(limit, len(entries))   # the card's own list (the status log marks all of it on board)
    rows, done = [], set()
    for i, e in enumerate(entries[:limit], 1):
        p = e.get("p") or {}
        done.add(prop_key(p))
        rows.append(_base_row(p, flag, "main", i, e.get("rerank_note") or "on board", e.get("sc"), sig_cache=sig_cache))
    try:
        bk = t._board_backup_rows(flag, entries, entries[:limit], rec) if t.BOARD_BACKUPS_LIMIT else []
    except Exception as ex:
        print("[BoardHistory] %s backups skipped (%s)" % (flag, type(ex).__name__))
        bk = []
    n = 0
    for pr, _txt, p, why, w67 in bk:
        k = prop_key(p)
        if k in done:
            continue
        done.add(k)
        try:
            sc = t.apex_score(p.get("prob_cal", p.get("prob")), p["category"], bool(p.get("bo5")), rec)[0]
        except Exception:
            sc = None
        if t.backup_is_held(why, w67):
            rows.append(_base_row(p, flag, "held", None, (why + ("; " + w67 if w67 else "")), sc, sig_cache=sig_cache))
        else:
            n += 1
            rows.append(_base_row(p, flag, "backup", n, why, sc, sig_cache=sig_cache))
    for h in list(res.get("held_back") or []) + list(res.get("held") or []):   # type boards' own holds
        p = h.get("p") or {}
        k = prop_key(p)
        if not p or k in done:
            continue
        done.add(k)
        rows.append(_base_row(p, flag, "held", None, "held back: " + str(h.get("reason") or "held"),
                              h.get("score"), sig_cache=sig_cache))
    return rows


def log_publish(picks, results, date_str, source="unknown", rec=None, status_rows=None):
    """Called by apex_edge_tracker.write_board_status after each PrizePicks publish. Fail-open."""
    if not BOARD_HISTORY_ON:
        return 0
    try:
        import apex_edge_tracker as t
        rec = rec or t.records()
        status_all = {}
        for s in status_rows or []:
            if s.get("board") == "all":
                status_all[(str(s.get("player") or "").strip().lower(), str(s.get("opponent") or "").strip().lower(),
                            s.get("category"), str(s.get("line") or ""))] = (s.get("status"), s.get("reason"),
                                                                             _f(s.get("apex_score")))
        sig_cache = {}
        _PROPS.clear()
        pub = {"published_at": datetime.now().isoformat(timespec="seconds"), "source": source,
               "trigger": TRIGGERS.get(source, source), "board_day": date_str, "platform": "prizepicks",
               "origin": "live"}
        c = _db()
        prev = _last_rows(c, "prizepicks")
        allrows = _allprops_rows(picks, rec, status_all, sig_cache)
        cur_all = {r["prop_key"]: r for r in allrows}
        out = []
        explain("allprops", allrows, prev.get("allprops"), prev.get("allprops"), cur_all, source)
        out += allrows
        for flag in BOARDS:
            if flag not in results:
                continue
            rows = _board_rows(flag, results.get(flag), rec, sig_cache)
            out += rows + explain(flag, rows, prev.get(flag), prev.get("allprops"), cur_all, source)
        pid = _insert(c, pub, out)
        c.commit()
        _order_check("prizepicks", pid, pub["trigger"], out)   # APEX-ORDERGUARD-2026-10-25
        try:   # APEX-FAIRPAPER-2026-10-25: shadow Top 3 / Elite 6 under fair points (paper only, after the live check)
            import apex_fair_points_paper as _fpp
            _fpp.shadow_publish(pid)
        except Exception as ex:
            print("[BoardHistory] fair points shadow skipped (%s)" % type(ex).__name__)
        try:
            fill_results(c)
        except Exception as ex:
            print("[BoardHistory] results fill skipped (%s)" % type(ex).__name__)
        c.close()
        try:
            export_csv(pub["published_at"][:10])
        except Exception as ex:
            print("[BoardHistory] CSV export skipped (%s)" % type(ex).__name__)
        return len(out)
    except Exception as ex:
        print("[BoardHistory] write failed (%s): %s" % (type(ex).__name__, ex))
        return 0


def log_pulled_fill(flag, pulled, fills):
    """A numbered pick PrizePicks pulled after the fetch and the backup that took its spot. Fail-open."""
    if not BOARD_HISTORY_ON:
        return 0
    try:
        out = []
        for k, (rank, e, now_lines) in enumerate(pulled):
            p = e.get("p") or {}
            r = _base_row(p, flag, "off", None, "PP pulled line")
            r.update(prev_spot="#%s" % rank, prev_line=_f(p.get("line")), prev_win_prob=r.get("win_prob"),
                     why_changed="left the board: PP pulled line%s" % (
                         (" (now offered at %s)" % ", ".join("%g" % x for x in now_lines)) if now_lines else ""))
            out.append(r)
            f = fills[k] if k < len(fills) else None
            if f and f.get("p"):
                r2 = _base_row(f["p"], flag, "main", rank, "filled a pulled spot", f.get("sc"))
                r2.update(prev_spot="BACKUP", why_changed="moved up: filled the spot of %s (PP pulled line)" % _short(p))
                out.append(r2)
        if not out:
            return 0
        c = _db()
        _insert(c, {"published_at": datetime.now().isoformat(timespec="seconds"), "source": "pulled_fill",
                    "trigger": TRIGGERS["pulled_fill"], "board_day": date.today().isoformat(),
                    "platform": "prizepicks-fill", "origin": "live"}, out)
        c.commit(); c.close()
        return len(out)
    except Exception as ex:
        print("[BoardHistory] pulled-fill write failed (%s): %s" % (type(ex).__name__, ex))
        return 0


# ------------------------------------------------------------------ ParlayPlay publish
def log_pp_publish(b, ts=None):
    """ParlayPlay board posted: numbered spots, BACKUPS and HELD rows. Fail-open."""
    if not BOARD_HISTORY_ON:
        return 0
    try:
        import apex_edge_tracker as t
        rows, done = [], set()
        _PROPS.clear()
        for i, d in enumerate(b, 1):
            r = _base_row(d["p"], "parlayplay", "main", i, d.get("rerank_note") or "on board", d.get("sc"), flag_for_rank="pp")
            r["pp_key"] = d["pp"]["key"]
            rows.append(r); done.add(r["prop_key"])
        n = 0
        for pr, sub67, why, d, also in getattr(b, "backups", None) or []:
            held = t.backup_is_held(why, sub67)
            if not held:
                n += 1
            r = _base_row(d["p"], "parlayplay", "held" if held else "backup", None if held else n,
                          why + ("; also %s" % ", ".join("%g" % x for x in also) if also else ""), d.get("sc"),
                          flag_for_rank="pp")
            r["pp_key"] = d["pp"]["key"]
            if r["prop_key"] not in done:
                rows.append(r); done.add(r["prop_key"])
        for h in getattr(b, "held", None) or []:
            p = h.get("p") or {}
            r = _base_row(p, "parlayplay", "held", None, "held back: " + str(h.get("reason") or ""), h.get("score"),
                          flag_for_rank="pp")
            if r["prop_key"] not in done:
                rows.append(r); done.add(r["prop_key"])
        c = _db()
        prev = _last_rows(c, "parlayplay")
        cur_all = {r["prop_key"]: r for r in rows}
        rows += explain("parlayplay", rows, prev.get("parlayplay"), prev.get("parlayplay"), cur_all, "parlayplay")
        pid = _insert(c, {"published_at": datetime.now().isoformat(timespec="seconds"), "source": "parlayplay",
                          "trigger": TRIGGERS["parlayplay"], "board_day": date.today().isoformat(),
                          "platform": "parlayplay", "origin": "live"}, rows)
        c.commit(); c.close()
        _order_check("parlayplay", pid, TRIGGERS["parlayplay"], rows)   # APEX-ORDERGUARD-2026-10-25
        return len(rows)
    except Exception as ex:
        print("[BoardHistory] ParlayPlay write failed (%s): %s" % (type(ex).__name__, ex))
        return 0


# ------------------------------------------------------------------ results
def _results_index():
    idx = {}
    con = sqlite3.connect(PROPS_DB, timeout=30)
    for pl, op, cat, start, md, actual, score, result, sts in con.execute(
            "select player, opponent, category, start_utc, match_date, actual, score, result, settled_ts "
            "from projections where (actual is not null or result = 'void') and match_date >= '2026-09-01' "
            "order by source = 'picklog_backfill'"):   # APEX-REGRADE-2026-11-01: settled rows first, log copies last
        op = re.sub(r"\s*1st\s*set.*$", "", op or "", flags=re.I)   # APEX-RANKFIRSTSET-2026-11-01
        for d in {str(start or "")[:10], str(md or "")[:10]}:
            if not d:
                continue
            val = (actual, score, result, sts)
            idx.setdefault(("p", _norm(pl), cat, d), val)
            idx.setdefault(("m", frozenset([_norm(pl), _norm(op)]), cat, d), val)
            idx.setdefault(("po", _norm(pl), _norm(op), cat, d), val)   # APEX-FIXGRADING-2026-10-30
    con.close()
    return idx


STARTED_LABEL = True


def _pulled_word(pv):
    """'PP pulled line' only when the latest pull really lacks it. Never raises."""
    try:
        import apex_live_check as _lc
        keys = _lc.live_keys()
        if keys and _lc.is_live(dict(player=pv["player"], opponent=pv["opponent"], category=pv["category"],
                                     line=pv["line"], prop_type=pv["prop_type"]), keys):
            return "Apex dropped it, PrizePicks still offers it"
    except Exception:
        pass
    return "PP pulled line (not in this pull)"


def fill_results(c=None):
    """Write settled results (games, score, hit/miss/push/void) onto open history rows. Returns rows filled."""
    own = c is None
    c = c or _db()
    cutoff = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    open_rows = c.execute("SELECT DISTINCT prop_key, player, opponent, category, direction, line, match_start, board_day, "
                          "pp_key FROM history WHERE result IS NULL AND match_start != '' AND match_start < ?",
                          (cutoff,)).fetchall()
    if not open_rows:
        if own:
            c.close()
        return 0
    idx = _results_index()
    pp = {}
    try:
        pc = sqlite3.connect(PP_DB, timeout=30)
        for key, actual, score, result, sts in pc.execute(
                "select key, actual, score, result, settled_ts from lines where actual is not null or result = 'void'"):
            pp[key] = (actual, score, result, sts)
        pc.close()
    except Exception:
        pass
    n = 0
    for pk, pl, op, cat, dr, line, ms, bd, ppk in open_rows:
        hit = pp.get(ppk) if ppk else None
        if hit is None:
            days = [str(ms or "")[:10], str(bd or "")[:10]]
            try:
                days.append((date.fromisoformat(str(ms)[:10]) - timedelta(days=1)).isoformat())
            except ValueError:
                pass
            if pl == "match":
                names = [x.strip() for x in str(op or "").replace(" vs ", " v ").split(" v ") if x.strip()]
                keys = [("m", frozenset(_norm(x) for x in names), cat, d) for d in days]
            else:
                # APEX-FIXGRADING-2026-10-30 (owner, "Go fix grading"): match the player AND the opponent,
                # so a player's match from the day before can't grade today's pick (Francesca Jones 10/30 got
                # her 10/29 result). Player-only fallback only on the pick's own dates, never the day before.
                keys = [("po", _norm(pl), _norm(op), cat, d) for d in days] if op else []
                # APEX-REGRADE-2026-11-01 (owner, "Fix all"): never by player alone when the opponent is known
                # (Rybakina v Fernandez 10/30 got her 10/29 Putintseva numbers).
                if not op:
                    keys += [("p", _norm(pl), cat, d) for d in days[:2]]
            hit = next((idx[k] for k in keys if k in idx), None)
        if hit is None:
            continue
        actual, score, result, sts = hit
        if result == "void" or actual is None:
            res = "void"
        elif line is None:
            continue
        elif float(actual) == float(line):
            res = "push"
        else:
            res = "hit" if (float(actual) > float(line)) == (str(dr).upper() == "MORE") else "miss"
        c.execute("UPDATE history SET result_games = ?, result_score = ?, result = ?, settled_at = ? "
                  "WHERE prop_key = ? AND result IS NULL AND match_start = ?",
                  (actual, score, res, sts or datetime.now().isoformat(timespec="seconds"), pk, ms))
        n += 1
    c.commit()
    if own:
        c.close()
    return n


# ------------------------------------------------------------------ CSV export
def export_csv(day=None):
    day = day or date.today().isoformat()
    os.makedirs(CSV_DIR, exist_ok=True)
    path = os.path.join(CSV_DIR, "board_history_%s.csv" % day)
    c = _db()
    cur = c.execute("SELECT %s FROM history WHERE substr(published_at, 1, 10) = ? ORDER BY published_at, publish_id, id"
                    % ", ".join(COLS), (day,))
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        w.writerows(cur)
    os.replace(tmp, path)
    c.close()
    return path


# ------------------------------------------------------------------ backfill from stored logs
_NOT_ALLPROPS = ("unplayable direction (MORE-only line, engine says LESS)", "match already started",
                 "market not modeled", "duplicate market (same prop listed twice)")


def backfill(verbose=True):
    """Rebuild history rows from apex_board_status_log_v1.csv (every publish since Oct 10, 2:31 PM) and
    apex_board_membership_log_v1.csv (board membership per day since Sep 29, no times or spots).
    Rank scores and signals are re-computed from today's stored signal data, so BACKUP numbers of old
    publishes are close, not exact; numbered spots, Win Prob, Apex, Honest and reasons are as stored."""
    import apex_edge_tracker as t
    c = _db()
    if c.execute("SELECT count(*) FROM publishes WHERE origin LIKE 'backfill%'").fetchone()[0]:
        print("backfill already done -- nothing added")
        return 0
    first_live = (c.execute("SELECT min(published_at) FROM publishes WHERE platform = 'prizepicks'").fetchone()[0]
                  or "9999")
    pubs = {}
    with open(STATUS_LOG, newline="") as fh:
        for r in csv.DictReader(fh):
            if r["captured_at"] >= first_live:
                continue
            pubs.setdefault((r["captured_at"], r["source"], r["match_date"]), []).append(r)
    import bisect
    plog = {}   # the pick log gives each old prop its match id and own match date (signals need both)
    with open(t.PICK_LOG, newline="") as fh:
        for r in csv.DictReader(fh):
            plog.setdefault((str(r.get("player") or "").strip().lower(), str(r.get("opponent") or "").strip().lower(),
                             r.get("category"), str(r.get("line") or ""), str(r.get("prop_type") or "").lower()), []
                            ).append((r.get("ts") or "", r.get("match_id"), r.get("match_date"), r.get("slip_start_utc")))
    for v in plog.values():
        v.sort()
    sig_cache, prev, n_total = {}, {}, 0
    for (at, src, md), rs in sorted(pubs.items()):
        def P(r):
            hits = plog.get((str(r["player"] or "").strip().lower(), str(r["opponent"] or "").strip().lower(),
                             r["category"], str(r["line"] or ""), str(r["prop_type"] or "").lower())) or []
            i = bisect.bisect_right(hits, (at + "~",)) - 1
            h = hits[i] if i >= 0 else (hits[0] if hits else ("", None, r["match_date"], ""))
            return {"player": r["player"], "opponent": r["opponent"], "category": r["category"], "line": r["line"],
                    "direction": r["direction"], "prop_type": r["prop_type"], "prob": _f(r["prob"]),
                    "prob_cal": _f(r["prob"]), "match_id": h[1], "match_date": h[2] or r["match_date"],
                    "start_time_raw": r["start_utc"], "slip_start_utc": r["start_utc"] or h[3]}
        allr = [r for r in rs if r["board"] == "all"]
        out = []
        ap = []
        elig = [r for r in allr if r["status"] == "eligible" or (r["reason"] == "Apex Score under 67" and _f(r["prob"])
                and _f(r["prob"]) >= t.BOARD_MIN_PROB and _f(r["apex_score"]) and _f(r["apex_score"]) >= (t.BACKUP_SUB67_MIN_SCORE or 999))]
        for r in allr:
            p = P(r)
            if (_f(r["prob"]) or 0) >= t.BOARD_MIN_PROB - 1e-9 and r["reason"] not in _NOT_ALLPROPS:   # same list /allprops shows
                ap.append((r, p))
            else:
                x = _base_row(p, "allprops", "off", None, r["reason"], r["apex_score"], flag_for_rank="top3",
                              sig_cache=sig_cache)
                x["match_start"] = r["start_utc"] or x["match_start"]
                out.append(x)
        ap.sort(key=lambda x: (-t.board_rank_score(x[1], _f(x[0]["prob"])), -(_f(x[0]["apex_score"]) or 0)))
        for i, (r, p) in enumerate(ap, 1):
            x = _base_row(p, "allprops", "main", i, r["reason"], r["apex_score"], flag_for_rank="top3", sig_cache=sig_cache)
            x["match_start"] = r["start_utc"] or x["match_start"]
            out.append(x)
        allrows = list(out)
        cur_all = {x["prop_key"]: x for x in allrows}
        explain("allprops", allrows, prev.get("allprops"), prev.get("allprops"), cur_all, src)
        boards_now = {"allprops": {x["prop_key"]: x for x in allrows}}
        for flag in BOARDS:
            br = [r for r in rs if r["board"] == flag]
            if not br:
                continue
            want = t._BACKUP_BOARD_TYPES.get(flag)
            rows, done = [], set()
            for r in sorted([r for r in br if r["status"] == "recommended"], key=lambda r: int(r["position"] or 0)):
                if int(r["position"] or 0) > t._PUBLISHED_FLAG_LIMIT.get(flag, 99):
                    done.add(prop_key(P(r)))
                    continue
                x = _base_row(P(r), flag, "main", int(r["position"]), "on board", r["apex_score"], sig_cache=sig_cache)
                x["match_start"] = r["start_utc"] or x["match_start"]
                rows.append(x); done.add(x["prop_key"])
            held_why = {prop_key(P(r)): r["reason"] for r in br if r["status"] in ("held_back",)}
            ns_why = {prop_key(P(r)): r["reason"] for r in br if r["status"] == "not_shown"}
            cand = []
            for r in elig:
                p = P(r)
                k = prop_key(p)
                if k in done or (want and str(r["prop_type"]).lower() != want):
                    continue
                sub67 = r["status"] != "eligible"
                why = held_why.get(k)
                why = ("held back: " + why) if why else ns_why.get(k, "board full")
                try:   # the same live hold checks _board_backup_rows runs (stored bet365 / results data)
                    import apex_blowout_signal as _bs
                    if flag == "bestgoblin" and not why.startswith("held") and not t._best_eligible(p, "goblin"):
                        why = "held back: under Best Goblin's 72.1% Goblin break-even floor (%.1f%%)" % (100 * _f(r["prob"]))
                    _lc = _bs.low_cushion_reason(p) if t.LOW_CUSHION_BACKUPS else None
                    if _lc:
                        why = "held back: " + _lc
                    _hr = t.board_hold_reason(p)
                    if _hr:
                        why = "held back: " + _hr
                except Exception:
                    pass
                w67 = ("⚠ Apex %d, under the %d bar" % (int(_f(r["apex_score"])), t.BOARD_MIN_SCORE)) if sub67 else ""
                cand.append((r, p, why, w67))
                done.add(k)
            for r in br:   # held rows the eligible list does not carry
                p = P(r)
                if r["status"] == "held_back" and prop_key(p) not in done:
                    cand.append((r, p, "held back: " + r["reason"], ""))
                    done.add(prop_key(p))
            cand.sort(key=lambda x: t.backup_sort_key(flag, _f(x[0]["prob"]), x[1], x[2], x[3]))
            n = 0
            for r, p, why, w67 in cand:
                if t.backup_is_held(why, w67):
                    x = _base_row(p, flag, "held", None, why + ("; " + w67 if w67 else ""), r["apex_score"], sig_cache=sig_cache)
                else:
                    n += 1
                    x = _base_row(p, flag, "backup", n, why, r["apex_score"], sig_cache=sig_cache)
                x["match_start"] = r["start_utc"] or x["match_start"]
                rows.append(x)
            out += rows + explain(flag, rows, prev.get(flag), prev.get("allprops"), cur_all, src)
            boards_now[flag] = {x["prop_key"]: x for x in rows}
        _insert(c, {"published_at": at, "source": src, "trigger": TRIGGERS.get(src, src), "board_day": md,
                    "platform": "prizepicks", "origin": "backfill-status-log"}, out)
        prev = boards_now
        n_total += len(out)
        if verbose:
            print("%s %-12s %5d rows" % (at, src, len(out)))
        c.commit()
    # membership log: which boards each prop was on, per day, before the status log starts (Sep 29 - Oct 9)
    start = min(k[0] for k in pubs) if pubs else "9999"
    by_day = {}
    with open(MEMBERSHIP_LOG, newline="") as fh:
        for r in csv.DictReader(fh):
            if r["date"] < start[:10]:
                by_day.setdefault(r["date"], {})[(r["player"], r["opponent"], r["category"], r["line"])] = r
    for d, props in sorted(by_day.items()):
        out = []
        for (pl, op, cat, ln), r in props.items():
            for flag in BOARDS:
                if str(r.get(flag)) == "1":
                    out.append({"board": flag, "spot": "on board (no spot stored)", "spot_kind": "main",
                                "player": pl, "opponent": op, "category": cat, "line": _f(ln),
                                "prop_key": "|".join([_norm(pl), _norm(op), cat or "", "", "", "%g" % (_f(ln) or 0)]),
                                "reason": "membership log: on this board that day (time, spot and stats not stored)"})
        _insert(c, {"published_at": d + "T00:00:00", "source": "membership", "trigger": "membership log (day only)",
                    "board_day": d, "platform": "prizepicks-membership", "origin": "backfill-membership-log"}, out)
        n_total += len(out)
    c.commit()
    fill_results(c)
    days = sorted({p[0][:10] for p in pubs} | set(by_day))
    c.close()
    for d in days:
        export_csv(d)
    print("backfill: %d rows, %s to %s" % (n_total, days[0] if days else "-", days[-1] if days else "-"))
    return n_total


# ------------------------------------------------------------------ plain-words read
def show(name, day=None, board=None):
    c = _db()
    q = "SELECT published_at, trigger, board, spot, player, opponent, category, prop_type, direction, line, win_prob, " \
        "rank_score, net_signals, reason, prev_spot, why_changed, result, result_games FROM history " \
        "WHERE (player LIKE ? OR opponent LIKE ?)"
    a = ["%" + name + "%", "%" + name + "%"]
    if day:
        q += " AND substr(published_at, 1, 10) = ?"; a.append(day)
    if board:
        q += " AND board = ?"; a.append(board)
    for r in c.execute(q + " ORDER BY published_at, board", a):
        who = r[5] if r[4] == "match" else "%s v %s" % (r[4], r[5])
        print("%s %-22s %-13s %-12s %s %s %s %s %s | Win %s rank %s net %s | was %s | %s | %s%s" % (
            r[0][5:16].replace("T", " "), r[1][:22], BOARD_NAMES.get(r[2], r[2]), r[3], who, r[6], r[9], r[8], r[7],
            r[10], r[11], r[12], r[14] or "-", r[15] or "", r[13] or "", (" | %s (%s)" % (r[16], r[17])) if r[16] else ""))
    c.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "backfill":
        backfill()
    elif cmd == "fill":
        print("filled", fill_results())
    elif cmd == "export":
        print(export_csv(sys.argv[2] if len(sys.argv) > 2 else None))
    elif cmd == "show" and len(sys.argv) > 2:
        show(sys.argv[2], *(sys.argv[3:5]))
    else:
        print(__doc__)
