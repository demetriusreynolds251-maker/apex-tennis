#!/usr/bin/env python3
"""
APEX-ODDSWATCH-2026-10-24 (owner, "Go odds" 2026-10-24): the 5:06 PM board
for 10/25 was thin because 7 morning ATP matches (71 props) had no bet365 price
yet. The 30-min auto-eval (apex_auto_eval.py) already retries props skipped for
no odds -- a skip writes no pick-log row -- but it took the first 150 props in
PrizePicks' order, so next week's ~194 unpriced props could crowd the board day
out. This module is the watch around that same retry (no second pipeline):

  start()  -- before scoring: which board-day / tonight matches are still waiting
              for bet365, what is numbered on the boards now, and which 70%+ Total
              Games picks are held for no bet365 total (APEX-NOBOOK)
  finish() -- after scoring + the normal republish: one change-only #alerts card
              "📣 ODDS RELEASED · <day> board" when a waiting match got priced (or a
              held total arrived) -- matches priced, what changed on each board, what
              is still waiting (APEX-ODDSCARD-2026-10-24) -- and the state file the
              DATA CHECK line reads
  status_line() -- "⏳ Odds watch: N matches for <date> still unpriced by bet365
              (X props), checking every 30 min" for the /fetch and 5 PM DATA CHECK

Scoring, holds and boards are untouched: props are scored by the same engine
call (_apex_engine_pick) and published by the same republish_boards path.
Never raises into the caller.
"""
import csv, json, os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "data", "logs", "odds_watch_state.json")
STATUS_MAX_AGE_H = 3
# APEX-ANYDATE-2026-10-25 (owner, "I want the top the show regardless of the date"): the
# boards rank every upcoming pick together, so the watch covers every upcoming match too (waiting
# list, no-book holds, new-props card) instead of the board day only. False = board day only.
ANY_DATE = True
# APEX-NOMOVEDLINES-2026-10-26 (owner, 10/26 5:39 PM: "take away the match move alerts, I'll
# notice that when I see the time and date of the match"): no 📅 MATCH MOVED cards in #alerts and no
# 📅 lines in the DATA CHECK. Moves are still recorded (state "moved") and props still re-scored.
MOVED_LINES_ON = False
MOVED_SHOW_H = 24   # APEX-DATEMOVE-2026-10-25: a 📅 moved-match line stays in the DATA CHECK this long
CAT_SHORT = {"total_games": "TG", "fantasy_score": "Fantasy", "double_faults": "DF", "aces": "Aces",
             "breaks": "Breaks", "games_won": "Games Won", "total_sets": "Sets", "tiebreakers": "Tiebreaks"}
BOARD_NAME = {"top3": "Top 3", "elite6": "Elite 6", "top10": "Top 10", "bestgoblin": "Best Goblin",
              "beststandard": "Best Standard", "bestdemon": "Best Demon"}


def _n(s):
    return str(s or "").strip().lower()


def pair_of(player, opponent):
    if _n(player) == "match":
        a, _, b = str(opponent or "").partition(" v ")
        return frozenset((_n(a), _n(b)))
    return frozenset((_n(player), _n(opponent)))


def _key(pair):
    return " v ".join(sorted(pair))


def _surname(name):
    parts = str(name or "").split()
    return parts[-1] if parts else "?"


def _pick_log(core):
    p = core._APEX_PICK_LOG
    return p if os.path.isabs(p) else os.path.join(HERE, p)


def priced_pairs(log_path, days=3):
    """Matches with any pick-log row in the last `days` days (= scored at least once)."""
    out = set()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        with open(log_path, newline="") as fh:
            for r in csv.DictReader(fh):
                if (r.get("ts") or "") >= cutoff:
                    out.add(pair_of(r.get("player"), r.get("opponent")))
    except Exception:
        pass
    return out


def _pkey(p):
    try:
        line = "%.1f" % float(p.get("line"))
    except (TypeError, ValueError):
        line = str(p.get("line"))
    return (_n(p.get("player")), _n(p.get("opponent")), p.get("category"), line,
            str(p.get("direction") or "").upper(), _n(p.get("prop_type") or "standard"))


def _numbered():
    """{prop key: (board, position, prop)} for every numbered spot on the published boards."""
    import apex_edge_tracker as T
    out = {}
    try:
        with open(T.PUBLISHED_BOARD_SNAPSHOT) as fh:
            boards = json.load(fh).get("boards") or {}
    except Exception:
        return out
    for flag in ("top3", "elite6", "top10", "bestgoblin", "beststandard", "bestdemon"):
        ents = ((boards.get(flag) or {}).get("entries") or [])[:T._PUBLISHED_FLAG_LIMIT.get(flag, 6)]
        for i, e in enumerate(ents):
            p = (e or {}).get("p") or {}
            out.setdefault(_pkey(p), (flag, i + 1, p))
    return out


def _nobook_held(board_day):
    """70%+ Total Games picks for the board day held to BACKUPS for no bet365 total."""
    import apex_edge_tracker as T
    picks, _ = T.load_current_board()
    out = {}
    for p in picks or []:
        if (ANY_DATE or str(p.get("match_date") or "")[:10] == board_day) and p.get("category") == "total_games" \
                and T._board_prob(p) >= 0.70 and T.no_book_total_reason(p):
            out[_pkey(p)] = p
    return out


def start(core, todo, board_day):
    """todo = [(partner prop, prop_type, start_utc)] the auto-eval built, BEFORE its cap.
    Returns the context finish() needs."""
    ctx = {"board_day": board_day, "log": _pick_log(core), "waiting": {}, "nobook": {}, "before": {}}
    try:
        priced = priced_pairs(ctx["log"])
        for p, _pt, st in todo:
            if not ANY_DATE and st.astimezone(timezone.utc).date().isoformat() > board_day:   # same board-day test as the sort
                continue
            pr = pair_of(p.get("player"), p.get("opponent"))
            if pr in priced:
                continue
            w = ctx["waiting"].setdefault(_key(pr), {"pair": pr, "props": 0, "start": st.isoformat(),
                                                     "label": "%s v %s" % (_surname(p.get("player")), _surname(p.get("opponent")))})
            w["props"] += 1
            w["start"] = min(w["start"], st.isoformat())
        ctx["nobook"] = _nobook_held(board_day)
        ctx["before"] = _numbered()
        ctx["cap_before"] = _latest_capture(board_day)   # APEX-ODDSCARD-2026-10-24
    except Exception as e:
        core.log("OddsWatch", f"start failed (non-fatal): {type(e).__name__}: {e}")
    return ctx


def fill_nobook_totals(ctx):
    """Ask SportsAPI365 for bet365's total on the held no-book matches only (the same
    apex_book_lines blank-fill the re-price republish uses, which logs a total it finds),
    then reload the shared reader. Returns how many held picks now have a total; the
    caller republishes when that is above 0 so they come back to their spots."""
    held = list((ctx.get("nobook") or {}).values())
    if not held:
        return 0
    import apex_edge_tracker as T, apex_book_lines as _bl, apex_bet365_totals as _b3
    _bl.annotate(held, T._board_prob, T.corrected_prob, log=False, offline=True)
    _b3.refresh()
    return sum(1 for p in held if not T.no_book_total_reason(p))


def _new_rows(log_path, skip):
    rows = []
    try:
        with open(log_path, newline="") as fh:
            for i, r in enumerate(csv.DictReader(fh)):
                if i + 1 >= skip:   # skip = line count before the run (header included)
                    rows.append(r)
    except Exception:
        pass
    return rows


def _load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_state(d):
    try:
        with open(STATE + ".tmp", "w") as fh:
            json.dump(d, fh, indent=1)
        os.replace(STATE + ".tmp", STATE)
    except Exception:
        pass


def finish(core, ctx, lines_before, republished, post=True):
    """After scoring (+ republish). Posts the change-only #alerts card; returns a one-line
    summary of it for the auto-eval log ('' if none)."""
    try:
        import apex_edge_tracker as T
        board_day = ctx["board_day"]
        rows = _new_rows(ctx["log"], lines_before)
        _mark_rescored(ctx["log"], rows, lines_before)   # APEX-ALERTCARDS-2026-10-25
        scored_pairs = {}
        for r in rows:
            scored_pairs.setdefault(_key(pair_of(r.get("player"), r.get("opponent"))), []).append(r)
        state = _load_state()
        if state.get("board_day") != board_day:
            state = {"board_day": board_day, "announced": [], "moved": state.get("moved") or {}}
        announced = set(state.get("announced") or [])
        priced = [k for k in ctx["waiting"] if k in scored_pairs and k not in announced]
        after_nobook = _nobook_held(board_day) if republished and ctx["nobook"] else ctx["nobook"]
        released = {k: pair_of(p.get("player"), p.get("opponent")) for k, p in ctx["nobook"].items()
                    if k not in after_nobook}
        rel_pairs = [_key(pr) for pr in released.values() if _key(pr) not in priced]
        # what is still waiting (for the DATA CHECK line and the card)
        state["unpriced"] = {k: {"label": w["label"], "props": w["props"], "start": w["start"]}
                             for k, w in ctx["waiting"].items() if k not in scored_pairs}
        state["ts"] = datetime.now().isoformat(timespec="seconds")
        state["log"] = ctx["log"]
        # APEX-NEWPROPS-SCORED-2026-10-24: props newly scored on matches that already had prices
        # (PrizePicks adding lines to a priced match) -- the board day and tonight only
        fresh = {}
        for k, rs in scored_pairs.items():
            if k in priced or k in rel_pairs:
                continue
            rs = [r for r in rs if ANY_DATE or str(r.get("match_date") or "")[:10] <= board_day]
            if rs:
                fresh[k] = rs
        summary = ""
        cap_after = _latest_capture(board_day) if republished else ctx.get("cap_before")
        if priced or released:
            matches = [(ctx["waiting"][k]["label"], scored_pairs[k]) for k in priced]
            matches += [(" v ".join(_surname(x).title() for x in sorted(pr)), scored_pairs.get(_key(pr), []))
                        for k, pr in released.items() if _key(pr) in rel_pairs]
            embed = build_card(core, board_day, matches, ctx.get("cap_before"), cap_after,
                               state["unpriced"], republished, nobook_back=len(released), fresh=fresh)
            summary = "\U0001F4E3 ODDS RELEASED card: %d match%s, %s" % (
                len(matches), "" if len(matches) == 1 else "es", embed.get("_summary", ""))
            state["announced"] = sorted(announced | set(priced) | set(rel_pairs))
            if post:
                if not _absorb(embed):   # APEX-ALERTCHAIN-2026-10-25: an open chain card takes it
                    post_card(core, "alerts", embed)
                if embed.get("_system"):   # APEX-SWAPLINES-2026-10-25: the long lists, kept in #system
                    post_card(core, "system", embed["_system"])
        elif fresh:
            embed, line = build_newprops(core, board_day, fresh, ctx.get("cap_before"), cap_after, republished)
            if embed:
                summary = "\U0001F195 NEW PROPS SCORED card: %s" % embed.get("_summary", "")
                if post:
                    if not _absorb(embed):
                        post_card(core, "alerts", embed)
                    if embed.get("_system"):
                        post_card(core, "system", embed["_system"])
            else:
                summary = line
                if post:
                    core.send_discord("alerts", line)
        _save_state(state)
        return summary
    except Exception as e:
        core.log("OddsWatch", f"finish failed (non-fatal): {type(e).__name__}: {e}")
        return ""


def _absorb_line(text):
    try:
        import apex_alert_chain as AC
        return AC.absorb_line(text)
    except Exception:
        return False


def _absorb(embed):
    """APEX-ALERTCHAIN-2026-10-25: True when the run's chain scope took this card (it posts once, inside
    the open new-props card or with the run's 📅 lines) instead of as a message of its own."""
    try:
        import apex_alert_chain as AC
        s = AC.active()
        if not s or not AC.CHAINS_ON:
            return False
        s["cards"].append(embed)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ APEX-ODDSCARD-2026-10-24
# owner: "The alert need to look better and have better information like top3 has
# change ____ has went to spot #___ ... just look like a jumble of information". The
# ODDS RELEASED line is now a card: which matches got priced, what changed on each board
# (who went in at which spot, who moved, who dropped to BACKUPS), what is still waiting.
# Board changes come from the board status log (apex_board_status_log_v1.csv): the last
# capture before the scoring run vs the one the republish wrote.
BOARD_BADGE = (("top3", "\U0001F947 TOP 3"), ("elite6", "\U0001F3C5 ELITE 6"), ("top10", "\U0001F51F TOP 10"),
               ("bestgoblin", "\U0001F7E2 BEST GOBLIN"), ("beststandard", "\u26AA BEST STANDARD"),
               ("bestdemon", "\U0001F534 BEST DEMON"))
CARD_GREEN, CARD_BLUE = 0x2ECC71, 0x3498DB
NEXT_CHECK_MIN = 30
STATUS_TAIL_BYTES = 4000000
MAX_MOVES_SHOWN = 2


def _status_rows():
    """The last few MB of the board status log (the newest captures), as dicts."""
    import apex_edge_tracker as T
    path = T.BOARD_STATUS_LOG
    with open(path, newline="") as fh:
        hdr = next(csv.reader([fh.readline()]))
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - STATUS_TAIL_BYTES))
        data = fh.read().decode("utf-8", "replace")
    lines = data.split("\n")
    if size > STATUS_TAIL_BYTES:
        lines = lines[1:]   # first line may be cut in half
    elif lines:
        lines = lines[1:]   # header
    return [r for r in csv.DictReader(lines, fieldnames=hdr) if r.get("captured_at")]


def _latest_capture(board_day, before=None):
    """captured_at of the newest status capture for this board day (strictly before `before`)."""
    try:
        caps = {r["captured_at"] for r in _status_rows() if ANY_DATE or r.get("match_date") == board_day}
        if before:
            caps = {c for c in caps if c < before}
        return max(caps) if caps else None
    except Exception:
        return None


def _capture(rows, cap):
    """{board: [rows on the board, by spot]}, {board: identities in BACKUPS}, 70%+ count."""
    import apex_edge_tracker as T
    on, backups, n_all = {}, {}, 0
    for r in rows:
        if r["captured_at"] != cap:
            continue
        b = r.get("board")
        if b == "all":
            n_all += r.get("status") == "eligible"
        elif r.get("status") == "recommended":
            on.setdefault(b, []).append(r)
        elif r.get("status") in ("not_shown", "held_back"):
            backups.setdefault(b, set()).add(T._apex_prop_identity(r))
    for b in on:
        on[b].sort(key=lambda r: int(r.get("position") or 99))
    return on, backups, n_all


def _pick_label(r, with_prob=True):
    if _n(r.get("player")) == "match":
        who = " v ".join(_surname(x) for x in str(r.get("opponent") or "").split(" v "))
    else:
        who = _surname(r.get("player"))
    cat = CAT_SHORT.get(r.get("category"), str(r.get("category") or "").replace("_", " ").title())
    try:
        line = "%.1f" % float(r.get("line"))
    except (TypeError, ValueError):
        line = str(r.get("line"))
    txt = "%s %s %s %s" % (who, cat, str(r.get("direction") or "").upper(), line)
    if with_prob:
        try:
            txt += " (%.0f%%)" % (100 * float(r.get("prob") or 0))
        except (TypeError, ValueError):
            pass
    return txt


def _short_label(r):
    """'Fritz v Musetti TG 19.5' / 'Machac Fantasy 4.5' for moves and drops."""
    return _pick_label(r, False).replace(" MORE ", " ").replace(" LESS ", " less ")


def board_changes(cap_before, cap_after):
    """['🥇 TOP 3: X went to spot #2 · Y moved #2 → #3 · Z out of #3 (now in BACKUPS)', ...]
    plus the All Props 70%+ count when it changed. [] when nothing changed."""
    import apex_edge_tracker as T
    if not cap_before or not cap_after or cap_before == cap_after:
        return []
    rows = _status_rows()
    b_on, _b_bk, b_all = _capture(rows, cap_before)
    a_on, a_bk, a_all = _capture(rows, cap_after)
    out = []
    for flag, badge in BOARD_BADGE:
        old = {T._apex_prop_identity(r): (i, r) for i, r in enumerate(b_on.get(flag) or [], 1)}
        new = {T._apex_prop_identity(r): (i, r) for i, r in enumerate(a_on.get(flag) or [], 1)}
        ins, outs, moves = [], [], []
        for k, (i, r) in sorted(new.items(), key=lambda kv: kv[1][0]):
            if k not in old:
                ins.append("%s went to spot #%d" % (_pick_label(r), i))
            elif old[k][0] != i:
                moves.append("%s #%d\u2192#%d" % (_short_label(r), old[k][0], i))
        _bkn = None
        for k, (i, r) in sorted(old.items(), key=lambda kv: kv[1][0]):
            if k not in new:
                _to = "off board"
                if k in (a_bk.get(flag) or set()):
                    # APEX-BACKUPORDER-2026-10-24: name the BACKUP number the posted card shows
                    if _bkn is None:
                        try:
                            _bkn = T.backup_numbers(flag)
                        except Exception:
                            _bkn = {}
                    _b = _bkn.get(tuple(k))
                    _to = ("BACKUP %d" % _b["no"]) if _b else "BACKUPS"
                outs.append("%s #%d\u2192%s" % (_short_label(r), i, _to))
        if not (ins or outs or moves):
            continue
        items = ins + outs
        if moves:
            extra = len(moves) - MAX_MOVES_SHOWN
            items.append("moved: " + ", ".join(moves[:MAX_MOVES_SHOWN]) + (" +%d more" % extra if extra > 0 else ""))
        out.append("**%s** \u00B7 %s" % (badge, " \u00B7 ".join(items)))
    if a_all != b_all:
        out.append("**\u2705 ALL PROPS** \u00B7 %d \u2192 %d picks at 70%%+" % (b_all, a_all))
    return out


def _tour_of(rows):
    """'ATP Vienna' from the pick-log rows of one match ('' if unknown)."""
    for r in rows or []:
        t = str(r.get("tournament") or "")
        city = t.split(" - ")[-1].strip() if " - " in t else t.strip()
        fl = str(r.get("format_label") or "").lower()
        tour = "WTA" if "women's" in fl else ("ATP" if "men's" in fl else "")
        if city and city.lower() != "tennis":
            return ("%s %s" % (tour, city)).strip()
    return ""


def _day_label(board_day):
    if ANY_DATE:
        return "upcoming"
    return datetime.fromisoformat(board_day).strftime("%a %-m/%-d")


def _fresh_lines(fresh):
    """['✅ Medvedev v Basilashvili · 3 props scored (Aces)', ...] for new props on priced matches."""
    out = []
    for _k, rs in sorted(fresh.items(), key=lambda kv: -len(kv[1])):
        cats = sorted({CAT_SHORT.get(r.get("category"), str(r.get("category") or "")) for r in rs})
        # APEX-ALERTCARDS-2026-10-25: a prop scored before (match re-listed / moved) is not a new prop
        n_re = sum(1 for r in rs if r.get("_rescored"))
        n_new = len(rs) - n_re
        if not n_re:
            what = "%d new prop%s" % (n_new, "" if n_new == 1 else "s")
        elif not n_new:
            what = "\U0001F504 %d prop%s re-scored (already on file, match re-listed)" % (n_re, "" if n_re == 1 else "s")
        else:
            what = "%d new prop%s + %d re-scored" % (n_new, "" if n_new == 1 else "s", n_re)
        out.append("%s %s · %s (%s)" % ("\U0001F504" if not n_new else "✅", _match_label(rs, _k),
                                        what.replace("\U0001F504 ", ""), ", ".join(cats)))
    return out


def _mark_rescored(log_path, rows, skip):
    """Flags rows of this run whose prop was already in the pick log in the last 3 days."""
    seen = set()
    cutoff = (datetime.now() - timedelta(days=3)).isoformat()
    try:
        with open(log_path, newline="") as fh:
            for i, r in enumerate(csv.DictReader(fh)):
                if i + 1 >= skip:
                    break
                if (r.get("ts") or "") >= cutoff:
                    seen.add(_rkey(r))
    except Exception:
        return
    for r in rows:
        if _rkey(r) in seen:
            r["_rescored"] = True


def _rkey(r):
    return (_n(r.get("player")), _n(r.get("opponent")), r.get("category"), str(r.get("line")),
            _n(r.get("prop_type")), str(r.get("direction") or "").upper())


# APEX-ALERTREASON-2026-11-11 (owner, "Go fix alert's reasoning", after the card said "went to BACKUPS; stronger picks hold
# every spot" while Elite 6 had 4 open spots and the pick was a lower-level player kept off every board): the "Board changes: none"
# line now gives the REAL reason the best new pick is not on a numbered spot, and how many spots are filled. Display only; reads the
# lower-level player check and the last published board. Turn off: ALERT_REAL_REASON = False, restart.
ALERT_REAL_REASON = True


def _board_fill():
    """'Top 3 has 2 of 3 spots filled, Elite 6 has 2 of 6.' from the last published board; '' when it cannot be read."""
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "board_history.sqlite"), uri=True)
        pid = con.execute("select max(publish_id) from history").fetchone()[0]
        n = dict(con.execute("select board, count(*) from history where publish_id=? and spot_kind='main' and board in ('top3','elite6') group by board", (pid,)).fetchall())
        con.close()
        return "Top 3 has %d of 3 spots filled, Elite 6 has %d of 6." % (min(3, n.get("top3", 0)), min(6, n.get("elite6", 0)))
    except Exception:
        return ""


def _real_reason(best):
    """Why this scored pick holds no numbered spot, in plain words; None when it cannot be told. Never raises."""
    try:
        import apex_player_checks as pc
        pl = str(best.get("player") or "").strip()
        names = [x.strip() for x in str(best.get("opponent") or "").split(" v ")] if pl.lower() == "match" else [pl, str(best.get("opponent") or "").strip()]
        for nm in names:
            n = pc.matches_on_file(nm) if nm else None
            if pc.THIN_DISQUALIFY and n is not None and n < pc.THIN_MIN:
                return "%s is a lower-level player (%d main-tour match%s on file, needs %d), so the pick is kept off every board and is not in BACKUPS" % (nm, n, "" if n == 1 else "es", pc.THIN_MIN)
    except Exception:
        pass
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "board_history.sqlite"), uri=True)
        pid = con.execute("select max(publish_id) from history").fetchone()[0]
        rows = con.execute("select board, spot_kind, spot_no, reason from history where publish_id=? and board in ('top3','elite6') and player=? and opponent=? and category=? "
                           "and abs(line-?)<0.01 and upper(direction)=? order by case board when 'top3' then 0 else 1 end",
                           (pid, best.get("player"), best.get("opponent"), best.get("category"), float(best.get("line")), str(best.get("direction") or "").upper())).fetchall()
        con.close()
        for board, kind, no, reason in rows:
            if kind == "main":
                return "it is on the board (%s #%d)" % ("Top 3" if board == "top3" else "Elite 6", int(no or 0))
        for board, kind, no, reason in rows:
            if reason:
                return "it waits in BACKUPS: %s" % str(reason).replace("held back: ", "")
    except Exception:
        pass
    return None


def _no_change_line(best):
    """The 'Board changes: none' sentence for a new pick at 70%+ that changed no spot."""
    if not ALERT_REAL_REASON:
        return "none \u2014 the best new pick (%s) went to BACKUPS; stronger picks hold every spot" % _pick_label(best)
    why = _real_reason(best)
    fill = _board_fill()
    if why and why.startswith("it is on the board"):
        return "none \u2014 best new pick: %s. It already holds its spot: %s.%s" % (_pick_label(best), why[len("it is "):], (" " + fill) if fill else "")
    return "none \u2014 best new pick: %s. %s%s" % (_pick_label(best), ("Why it holds no spot: %s." % why) if why else "It did not take a numbered spot; its reason is listed in BACKUPS.", (" " + fill) if fill else "")


def _best_playable(rows):
    return max((r for r in rows if str(r.get("playable") or "") != "0"),
               key=lambda r: float(r.get("prob") or 0), default=None)


def _status(cap_before, cap_after, republished):
    """APEX-BOARDSTATUS-HEADER-2026-10-25: the big TOP 3 & ELITE 6 status for this update."""
    try:
        import apex_board_status as S
        return S.from_captures(cap_before, cap_after if republished else cap_before)
    except Exception:
        return None


def _with_status(embed, cap_before, cap_after, republished):
    """APEX-ALERTCARDS-2026-10-25: the status sections (one line per moved pick + why + lineup,
    other boards, All Props) replace the old run-on '📋 Board changes' field."""
    import apex_board_status as S
    st = _status(cap_before, cap_after, republished)
    if st and S.HEADER_ON:
        embed = dict(embed, description="\n".join(x for x in (embed.get("description") or "").split("\n")
                                                   if not x.startswith("boards updated")))
        flds, drop = [], False
        for f in embed.get("fields") or []:
            if f["name"] == "\U0001F4CB Board changes":
                drop = True
                continue
            if drop and f["name"] == "\u200b":
                continue
            drop = False
            flds.append(f)
        embed = dict(embed, fields=flds)
    return S.wrap_embed(st, embed)


def build_newprops(core, board_day, fresh, cap_before, cap_after, republished):
    """APEX-NEWPROPS-SCORED-2026-10-24 (owner 10/24 7:47 PM: "we should have gotten a odds
    updated"): new props on matches that already had bet365 prices. Returns (embed, None) when a
    board changed or a new pick reached 70% -- the "🆕 NEW PROPS SCORED" card, same format as
    ODDS RELEASED -- else (None, one short line) so the alert never goes silent."""
    now = datetime.now(core.CT)
    rows = [r for rs in fresh.values() for r in rs]
    n, m = len(rows), len(fresh)
    best = _best_playable(rows)
    changes = board_changes(cap_before, cap_after) if republished else []
    n_re = sum(1 for r in rows if r.get("_rescored"))   # APEX-ALERTCARDS-2026-10-25
    head = "%d new prop%s scored (%d match%s)" % (n, "" if n == 1 else "s", m, "" if m == 1 else "es") if not n_re else \
        "%d prop%s scored (%d new, %d re-scored) on %d match%s" % (n, "" if n == 1 else "s", n - n_re, n_re, m,
                                                                 "" if m == 1 else "es")
    if not changes and (best is None or float(best.get("prob") or 0) < 0.70):
        why = ("best: %s" % _pick_label(best)) if best else "all were Goblin/Demon LESS, not playable"
        import apex_board_status as S   # APEX-ALERTCARDS-2026-10-25: a card, not a plain line
        embed = S.card(_status(cap_before, cap_after, republished), "\U0001F195 %s · none reach 70%%" % head,
                       [why[0].upper() + why[1:]])
        embed["_summary"] = "%s, none reach 70%%" % head
        embed["_event"] = "\U0001F195 %s · none reach 70%%" % head
        return embed, None
    if changes:
        change_val = "\n".join(changes)
    else:
        change_val = _no_change_line(best)
    desc = "%s on matches that already had bet365 prices" % head
    if best is not None:
        desc += " · best new pick: %s" % _pick_label(best)
    if republished:
        desc += " · boards updated %s" % now.strftime("%I:%M %p").lstrip("0")
    desc = desc.replace(" \u00B7 ", "\n")   # APEX-ALERTCARDS-2026-10-25: one fact per line
    embed = {"title": "\U0001F195 NEW PROPS SCORED · %s board" % _day_label(board_day),
             "description": desc, "color": CARD_GREEN if changes else CARD_BLUE,
             "fields": [{"name": "\U0001F195 New PrizePicks props", "value": "\n".join(_fresh_lines(fresh))[:1024] or "—",
                         "inline": False}] + _change_fields(change_val),
             "footer": {"text": "Apex Odds Watch · %s CT · next check %s" % (
                 now.strftime("%I:%M %p").lstrip("0"),
                 (now + timedelta(minutes=NEXT_CHECK_MIN)).strftime("%I:%M %p").lstrip("0"))}}
    embed["_summary"] = "%s, %d board line%s changed" % (head, len(changes), "" if len(changes) == 1 else "s")
    sw = _swap_card(embed, "\U0001F195 New props scored: %s" % head, cap_before, cap_after, republished)
    return (sw or _with_status(embed, cap_before, cap_after, republished)), None


def _swap_card(embed, event, cap_before, cap_after, republished):
    """APEX-SWAPLINES-2026-10-25: the #alerts card = one short header + the swap lines; the full
    card (per-match lists, still waiting, every board's changes) rides along as _system for
    #system. None when the status can't be built (the full card posts as before)."""
    import apex_board_status as S
    if not (S.SWAPS_ONLY and S.HEADER_ON):
        return None
    st = _status(cap_before, cap_after, republished)
    if not st:
        return None
    card = S.card(st, event)
    card["_event"] = event   # APEX-ALERTCHAIN-2026-10-25: the one-line event when a chain card takes it
    card["_summary"] = embed.get("_summary", "")
    card["_system"] = {k: v for k, v in embed.items() if not k.startswith("_")}
    return card


def build_card(core, board_day, matches, cap_before, cap_after, unpriced, republished,
               nobook_back=0, sample=False, fresh=None):
    """The ODDS RELEASED embed dict. matches = [(label, pick-log rows scored for it)]."""
    now = datetime.now(core.CT)
    n_scored = sum(len(rs) for _l, rs in matches)
    priced_lines = []
    for label, rs in matches:
        tour = _tour_of(rs)
        priced_lines.append("\u2705 %s%s \u00B7 %d prop%s scored" % (
            _match_label(rs, label), (" (%s)" % tour) if tour else "", len(rs), "" if len(rs) == 1 else "s"))
    changes = board_changes(cap_before, cap_after) if republished else []
    if changes:
        change_val = "\n".join(changes)
    else:
        best = _best_playable([r for _l, rs in matches for r in rs] + [r for rs in (fresh or {}).values() for r in rs])
        if best is None:
            change_val = "none \u2014 nothing new was scored"
        elif float(best.get("prob") or 0) >= 0.70:
            change_val = _no_change_line(best)
        else:
            change_val = "none \u2014 the new props scored under 70%% (best: %s)" % _pick_label(best)
    left = sorted((unpriced or {}).values(), key=lambda w: w.get("start") or "")
    nxt = (now + timedelta(minutes=NEXT_CHECK_MIN)).strftime("%I:%M %p").lstrip("0")
    if left:
        n_left = sum(int(w.get("props") or 0) for w in left)
        wait_val = "%s (%d prop%s) \u00B7 next check %s" % (", ".join(w["label"] for w in left), n_left,
                                                         "" if n_left == 1 else "s", nxt)
    else:
        wait_val = "nothing \u2014 every match on the %s board has bet365 prices" % _day_label(board_day)
    desc = "%d match%s got bet365 prices \u00B7 %d prop%s scored" % (
        len(matches), "" if len(matches) == 1 else "es", n_scored, "" if n_scored == 1 else "s")
    if nobook_back:
        desc += " \u00B7 %d held Total Games pick%s got a bet365 total" % (nobook_back, "" if nobook_back == 1 else "s")
    if republished:
        desc += " \u00B7 boards updated %s" % (_clock(cap_after) if sample else now.strftime("%I:%M %p").lstrip("0"))
    desc = desc.replace(" \u00B7 ", "\n")   # APEX-ALERTCARDS-2026-10-25: one fact per line
    foot = "Apex Odds Watch \u00B7 %s CT" % now.strftime("%I:%M %p").lstrip("0")
    if sample:
        foot += " \u00B7 sample card (board changes from the %s update)" % _clock(cap_after)
    embed = {"title": "\U0001F4E3 ODDS RELEASED \u00B7 %s board" % _day_label(board_day),
             "description": desc, "color": CARD_GREEN if changes else CARD_BLUE,
             "fields": [{"name": "\U0001F4B5 Priced since last board", "value": "\n".join(priced_lines)[:1024] or "\u2014", "inline": False},
                        ] + ([{"name": "\U0001F195 Props scored on matches already priced",
                               "value": "\n".join(_fresh_lines(fresh))[:1024], "inline": False}] if fresh else [])
             + _change_fields(change_val) + [
                        {"name": "\u23F3 Still waiting on bet365", "value": wait_val[:1024], "inline": False}],
             "footer": {"text": foot}}
    embed["_summary"] = "%d board line%s changed" % (len(changes), "" if len(changes) == 1 else "s")
    sw = _swap_card(embed, "\U0001F4E3 Odds released: %d match%s priced, %d prop%s scored" % (
        len(matches), "" if len(matches) == 1 else "es", n_scored, "" if n_scored == 1 else "s"),
        cap_before, cap_after, republished)
    return sw or _with_status(embed, cap_before, cap_after, republished)


def _change_fields(text):
    """'📋 Board changes' field(s): split on whole lines so no field passes Discord's 1,024."""
    fields, cur = [], ""
    for ln in text.split("\n"):
        ln = ln[:1024]
        if cur and len(cur) + 1 + len(ln) > 1024:
            fields.append(cur)
            cur = ln
        else:
            cur = (cur + "\n" + ln) if cur else ln
    fields.append(cur)
    return [{"name": "\U0001F4CB Board changes" if i == 0 else "\u200b", "value": v, "inline": False}
            for i, v in enumerate(fields)]


def _match_label(rows, fallback):
    """'Machac v Marozsan' in PrizePicks' order, from the match's pick-log rows."""
    for r in rows or []:
        if _n(r.get("player")) == "match":
            a, _, b = str(r.get("opponent") or "").partition(" v ")
        else:
            a, b = r.get("player"), r.get("opponent")
        if a and b:
            return "%s v %s" % (_surname(a), _surname(b))
    return fallback


def _clock(cap):
    try:
        return datetime.fromisoformat(cap).strftime("%I:%M %p").lstrip("0")
    except Exception:
        return "last"


def card_text(embed):
    out = [embed["title"], embed.get("description", ""), ""]
    for f in embed.get("fields") or []:
        out += ([f["name"]] if f["name"] != "\u200b" else []) + [f["value"], ""]
    out.append(embed["footer"]["text"])
    return "\n".join(out)


def post_card(core, channel, embed):
    """Posts the embed through the channel webhook (same webhooks send_discord uses);
    falls back to the plain-text card if the embed post fails. Returns True/False."""
    body = {k: v for k, v in embed.items() if not k.startswith("_")}
    if channel == "alerts" and getattr(core, "_DISCORD_FETCH_SUPPRESS", {}).get("active"):
        return True
    hook = (getattr(core, "DISCORD_WEBHOOKS", {}) or {}).get(channel)
    if not hook or "YOUR_TOKEN" in hook:
        core.log("OddsWatch", "#%s not configured -- card logged only: %s" % (channel, embed["title"]))
        return True
    try:
        import requests, time as _t
        for _attempt in range(3):
            r = requests.post(hook, json={"embeds": [body]}, timeout=15)
            if r.status_code in (200, 204):
                return True
            if r.status_code == 429:
                _t.sleep(min(10.0, float((r.json() or {}).get("retry_after", 2))))
                continue
            core.log("OddsWatch", "card post got HTTP %s, sending as text" % r.status_code)
            break
    except Exception as e:
        core.log("OddsWatch", "card post failed (%s), sending as text" % type(e).__name__)
    return bool(core.send_discord(channel, card_text(embed)))


def _scored_between(log_path, t0, t1):
    out = {}
    try:
        with open(log_path, newline="") as fh:
            for r in csv.DictReader(fh):
                if t0 < (r.get("ts") or "") <= t1:
                    out.setdefault(_key(pair_of(r.get("player"), r.get("opponent"))), []).append(r)
    except Exception:
        pass
    return out


def sample(core, post=True):
    """One-time sample card from the real current state: the matches announced today, the
    board changes of the newest update vs the one before it, what is still waiting."""
    state = _load_state()
    board_day = state.get("board_day")
    rows = [r for r in _status_rows() if r.get("match_date") == board_day]
    caps = sorted({r["captured_at"] for r in rows if r.get("source") == "autoeval"}) or [None]
    cap_after = caps[-1]   # the newest odds-watch (auto-eval) update, not a restart re-publish
    cap_before = _latest_capture(board_day, before=cap_after)
    scored = _scored_between(state.get("log") or _pick_log(core), cap_before or "", cap_after or "")
    labels = {}
    for k in state.get("announced") or []:
        a, _, b = k.partition(" v ")
        labels[k] = "%s v %s" % (_surname(a).title(), _surname(b).title())
    matches = [(labels[k], scored.get(k, [])) for k in sorted(labels, key=lambda k: -len(scored.get(k, [])))]
    embed = build_card(core, board_day, matches, cap_before, cap_after, state.get("unpriced"), True, sample=True)
    if post:
        post_card(core, "alerts", embed)
    return embed


def status_line():
    """'⏳ Odds watch: ...' for the DATA CHECK, or None. Reads only local files (no API calls)."""
    try:
        s = _load_state()
        ts = datetime.fromisoformat(s.get("ts"))
        if datetime.now() - ts > timedelta(hours=STATUS_MAX_AGE_H):
            return None
        priced = priced_pairs(s.get("log") or "")
        now = datetime.now().astimezone()
        left = []
        for k, w in (s.get("unpriced") or {}).items():
            st = datetime.fromisoformat(w["start"])
            if st <= now or frozenset(k.split(" v ")) in priced:
                continue
            left.append(w)
        if not left:
            return None
        d = "the upcoming boards" if ANY_DATE else datetime.fromisoformat(s["board_day"]).strftime("%a %-m/%-d")
        n = sum(w["props"] for w in left)
        return ("⏳ Odds watch: %d match%s for %s still unpriced by bet365 (%d prop%s: %s), checking every 30 min"
                % (len(left), "" if len(left) == 1 else "es", d, n, "" if n == 1 else "s",
                   ", ".join(w["label"] for w in sorted(left, key=lambda w: w["start"]))))
    except Exception:
        return None


# ------------------------------------------------------------------ APEX-DATEMOVE-2026-10-25
# owner, "Go fix date": when PrizePicks moves a match (another day or an hour+), the
# auto-eval re-scores every prop on it and the board keeps them. One #alerts line per move,
# and a DATA CHECK line for a day: "📅 Match moved: A v B now <day time>, props kept and rescored".
def _when(iso):
    try:
        import apex_edge_tracker as T
        return T.start_time_line({"slip_start_utc": iso}).replace("*", "").replace("🕒 Starts ", "").split(" · ")[0]
    except Exception:
        return iso


def moved_text(w):
    return "📅 Match moved: %s now %s, props kept and rescored" % (w["label"], _when(w["start"]))


def note_moved(core, moved):
    """moved = {pair key: {"label", "start", "old_start", "props"}} from this auto-eval run.
    Posts one #alerts line per move not announced yet (same pair + same new start)."""
    s = _load_state()
    seen = s.get("moved") or {}
    lines = []
    for k, w in moved.items():
        if (seen.get(k) or {}).get("start") == w["start"]:
            continue
        seen[k] = dict(w, noted=datetime.now().isoformat(timespec="seconds"))
        lines.append(moved_text(w))
    s["moved"] = {k: v for k, v in seen.items()
                  if datetime.now() - datetime.fromisoformat(v.get("noted")) < timedelta(days=3)}
    _save_state(s)
    if not MOVED_LINES_ON:   # APEX-NOMOVEDLINES-2026-10-26: recorded, not announced
        return []
    for k, w in moved.items():   # APEX-ALERTCARDS-2026-10-25: one orange card per moved match
        if moved_text(w) not in lines:
            continue
        try:
            import apex_alert_cards as C
            emb = C.make("\U0001F4C5 MATCH MOVED · %s" % w["label"], C.ORANGE, "", [
                ("\U0001F552 New start", _when(w["start"])),
                ("\u23EA Was", _when(w["old_start"]) if w.get("old_start") else "—"),
                ("\u2705 What Apex did", "kept all %s prop%s and re-scored them for the new time" % (
                    w.get("props") or "its", "" if w.get("props") == 1 else "s"))])
            C.post(emb, "alerts", check_mute=True)
        except Exception:
            if not _absorb_line(moved_text(w)):   # APEX-ALERTCHAIN-2026-10-25: rides the run's card
                core.send_discord("alerts", moved_text(w))
    return lines


def moved_lines():
    """📅 lines for the DATA CHECK: moves noted in the last MOVED_SHOW_H hours, match not started."""
    try:
        if not MOVED_LINES_ON:   # APEX-NOMOVEDLINES-2026-10-26
            return []
        out = []
        now = datetime.now().astimezone()
        for w in sorted((_load_state().get("moved") or {}).values(), key=lambda w: w["start"]):
            if datetime.now() - datetime.fromisoformat(w["noted"]) > timedelta(hours=MOVED_SHOW_H):
                continue
            if datetime.fromisoformat(w["start"]) <= now:
                continue
            out.append(moved_text(w))
        return out
    except Exception:
        return []


if __name__ == "__main__":   # python3 apex_odds_watch.py sample [--post]
    import sys
    sys.path.insert(0, HERE)
    import openclaw_core as _core
    if len(sys.argv) > 1 and sys.argv[1] == "sample":
        print(card_text(sample(_core, post="--post" in sys.argv)))
