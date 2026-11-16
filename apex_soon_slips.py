"""APEX-SOONSLIPS-2026-10-27 (owner, 12:32 AM 10/27: "I still want top 3 sooner and elite 6
sooner built top 3 80+ and top 6 90+ and when all legs has that line up for each elected board
same output design as we been doing on discord I want an alert in alert for each board when they
are ready and to tell me to make a slip in the general is where I want to slip to show").

Two "soon" boards built from the props whose match starts inside the next SOON_HOURS, with the
SAME builders, bars, holds and Ranking # as the posted boards (Top 3 = best_bets_detail limit 3,
Elite 6 = elite6_detail), on matches that have not started:
  * TOP 3 SOON   -- ready when all 3 picks have Ranking # >= TOP3_SOON_FLOOR (80)
  * ELITE 6 SOON -- ready when all 6 picks have Ranking # >= ELITE6_SOON_FLOOR (90)
When a board is ready and its lineup is new (or changed since the last alert), one short card goes
to #alerts ("ready -- make this slip, it is in #general") and the full board-style card goes to
#general. Runs after every board publish and on the 30-minute alerts check. Every alerted slip is
logged to data/soon_slips/log.jsonl (its own tracker, graded later; never the tax ledger).
Switch: SOON_SLIPS_ON = False posts nothing. Undo: git revert of this commit."""
import os, sys, json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "data", "soon_slips", "state.json")
LOG = os.path.join(HERE, "data", "soon_slips", "log.jsonl")
CT = ZoneInfo("America/Chicago")

SOON_SLIPS_ON = True
SOON_HOURS = 6                 # same window as /soon
TOP3_SOON_FLOOR = 80           # every Top 3 Soon pick needs Ranking # 80+
ELITE6_SOON_FLOOR = 90         # every Elite 6 Soon pick needs Ranking # 90+
# APEX-SOONQUIET-2026-10-27 (owner 1:59 PM: "I like that suggestion that get that done" -- 11 cards on
# the first morning, some 1 minute before the first match): no card unless the first match is at least
# MIN_LEAD_MIN away, and a CHANGED card only when at least MIN_CHANGED[board] picks differ from the last card.
MIN_LEAD_MIN = 45
MIN_CHANGED = {"top3": 2, "elite6": 3}
# APEX-SOONONDEMAND-2026-10-27 (owner 8:41 PM: "I don't want any of the soon cards showing unless I
# command them ... just give me alert ... with mini version when 2 or more new props for top and 3 for elite 6",
# 8:54 PM "only for the sooner"): the big soon card in #general is OFF (see it with /soon3 or /soon6); #alerts gets
# only the short numbered line when a soon lineup is ready or at least MIN_CHANGED picks are new.
SOON_AUTO_POST = True       # the short #alerts line
SOON_GENERAL_CARD = False   # the big card in #general (on demand only)
BOARDS = (("top3", "TOP 3 SOON", "\U0001F947", 3, TOP3_SOON_FLOOR, "Power"),
          ("elite6", "ELITE 6 SOON", "\U0001F3C6", 6, ELITE6_SOON_FLOOR, "Flex"))
DRY = "--dry-run" in sys.argv


def _read_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def _who(p):
    return str(p.get("opponent")) if p.get("player") == "match" else "%s v %s" % (p.get("player"), p.get("opponent"))


def _cat(p):
    return {"total_games": "Total Games", "fantasy_score": "Fantasy Score", "aces": "Aces", "dfs": "Double Faults",
            "breaks": "Breaks", "games_won": "Games Won"}.get(p.get("category"), p.get("category"))


def _clock(iso):
    d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(CT)
    return d, d.strftime("%a %-I:%M %p CT")


def _window_picks(t, now_utc):
    """Board props whose match starts after now and inside SOON_HOURS; (inside, next_after_window)."""
    picks, note = t.load_current_board()
    if not picks:
        return [], None
    edge = now_utc.timestamp() + SOON_HOURS * 3600
    inside, later = [], []
    for p in picks:
        s = t._prop_start_utc(p)
        if not s:
            continue
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if d <= now_utc:
            continue
        (inside if d.timestamp() <= edge else later).append((d, p))
    later.sort(key=lambda x: x[0])
    return [p for _, p in inside], (later[0] if later else None)


def build(t, flag, inside):
    """(entries, text) -- the board builder on the window's props; entries = scored dicts with 'p'."""
    # APEX-SOONFIRSTSET-2026-11-04: the same step as /soon -- builder + the posted boards' rerank (first-set picks
    # inside the window included). Undo: apex_soon.SOON_FIRST_SET = False.
    try:
        import apex_soon as _soon
        return _soon.soon_board(flag, inside, 3 if flag == "top3" else 6)
    except Exception as e:
        return [], "soon board unavailable (%s)" % type(e).__name__


def _rank(t, flag, p):
    try:
        return round(100 * float(t.board_rank_of(flag, p)), 1)
    except Exception:
        return 0.0


def _lineup_key(t, ents):
    return sorted("|".join(str(x) for x in t._apex_prop_identity(d["p"])) for d in ents)


def _general_card(flag, label, medal, text, n, floor, kind, first_clock):
    """The board's own card with a 'soon' header: same design as the posted boards."""
    lines = text.split("\n")
    head = "%s **%s -- MAKE THIS SLIP (%s)**" % (medal, label, kind)
    sub = ("_All %d picks have Ranking # %d+ and start in the next %d hours (first match %s). "
           "Play exactly these %d, in this order, one slip on %s. This is the SOON card (next %d hours only), "
           "not the regular %s board._" % (n, floor, SOON_HOURS, first_clock, n, kind, SOON_HOURS, label.replace(" SOON", "")))
    k = next((i for i, x in enumerate(lines) if ("TOP 3" in x.upper() and flag == "top3") or ("ELITE 6" in x.upper() and flag == "elite6")), None)
    if k is None:
        return "\n".join([head, sub] + lines)
    lines[k] = head
    if k + 1 < len(lines) and lines[k + 1].startswith("_"):
        lines[k + 1] = sub
    else:
        lines.insert(k + 1, sub)
    return "\n".join(lines)


def run(source="check", post=True):
    """Build both soon boards; alert + post when one is ready with a new lineup. Returns [messages]. Never raises."""
    out = []
    if not SOON_SLIPS_ON:
        return out
    try:
        import apex_edge_tracker as t
        import apex_ops as ops
        now = datetime.now(timezone.utc)
        inside, nxt = _window_picks(t, now)
        st = _read_state()
        day = now.astimezone(CT).strftime("%Y-%m-%d")
        for flag, label, medal, n, floor, kind in BOARDS:
            ents, text = build(t, flag, inside)   # APEX-SOONFIRSTSET-2026-11-04: first-set picks can fill an empty window
            ents = ents[:n]
            ranks = [_rank(t, flag, d["p"]) for d in ents]
            ready = len(ents) == n and all(r >= floor - 1e-9 for r in ranks)
            slot = st.setdefault(flag, {})
            if not ready:
                slot["last_seen_ready"] = False
                continue
            key = _lineup_key(t, ents)
            if slot.get("day") == day and slot.get("key") == key:
                continue   # same lineup already announced
            changed = slot.get("day") == day and bool(slot.get("key"))
            if changed and len(set(key) - set(slot.get("key") or [])) < MIN_CHANGED.get(flag, 2):
                continue   # APEX-SOONQUIET: only one or two picks moved -- not worth a new card
            starts = sorted(x for x in (t._prop_start_utc(d["p"]) for d in ents) if x)
            if starts:
                try:
                    _first = datetime.fromisoformat(starts[0].replace("Z", "+00:00"))
                    if _first.tzinfo is None:
                        _first = _first.replace(tzinfo=timezone.utc)
                    if (_first - now).total_seconds() < MIN_LEAD_MIN * 60:
                        continue   # APEX-SOONQUIET: first match too close to place the slip
                except Exception:
                    pass
            _, first_clock = _clock(starts[0]) if starts else (None, "soon")
            names = ["%d) %s · %s %s %s · Ranking # %g" % (i, _who(d["p"]), _cat(d["p"]), d["p"].get("direction"),
                                                           d["p"].get("line"), r) for i, (d, r) in enumerate(zip(ents, ranks), 1)]
            alert = ["%s **%s %s -- MAKE THIS SLIP**" % (medal, label, "CHANGED" if changed else "IS READY"),
                     "All %d picks are Ranking # %d+ and start in the next %d hours. First match %s." % (n, floor, SOON_HOURS, first_clock),
                     "SOON card, next %d hours only, not the regular board. Type /soon%s for the full card. Play these %d on %s%s:"
                     % (SOON_HOURS, "3" if flag == "top3" else "6", n, kind, " -- the lineup changed since the last card" if changed else "")]
            alert += names
            alert_msg = "\n".join(alert)
            general_msg = _general_card(flag, label, medal, text, n, floor, kind, first_clock)
            if DRY:
                print("---- #alerts ----\n%s\n---- #general ----\n%s" % (alert_msg, general_msg))
            elif post and SOON_AUTO_POST:
                ops.send_discord("alerts", alert_msg)
                if SOON_GENERAL_CARD:
                    ops.send_discord("general", general_msg)
            slot.update({"day": day, "key": key, "at": now.isoformat(timespec="seconds"), "first_start": starts[0] if starts else None})
            try:
                os.makedirs(os.path.dirname(LOG), exist_ok=True)
                with open(LOG, "a") as f:
                    f.write(json.dumps({"at": now.isoformat(timespec="seconds"), "day": day, "board": flag, "source": source,
                                        "changed": changed, "kind": kind, "first_start": starts[0] if starts else None,
                                        "legs": [{"player": d["p"].get("player"), "opponent": d["p"].get("opponent"),
                                                  "category": d["p"].get("category"), "direction": d["p"].get("direction"),
                                                  "line": d["p"].get("line"), "prop_type": d["p"].get("prop_type"),
                                                  "match_date": d["p"].get("match_date"), "win_prob": d["p"].get("prob_cal") or d["p"].get("prob"),
                                                  "rank": r, "start_utc": t._prop_start_utc(d["p"])} for d, r in zip(ents, ranks)]}) + "\n")
            except Exception as e:
                print("[SoonSlips] log skipped: %s" % e)
            out.append(alert_msg)
        if not DRY:
            _write_state(st)
    except Exception as e:
        print("[SoonSlips] skipped (%s): %s" % (type(e).__name__, e))
    return out


def card_text(flag):
    """APEX-SOONONDEMAND-2026-10-27: /soon3 or /soon6 -- that soon board right now, with its SOON header,
    ready or not. Never posts, never logs."""
    import apex_edge_tracker as t
    now = datetime.now(timezone.utc)
    inside, nxt = _window_picks(t, now)
    after = ("\n\u23ed After the %d-hour window, next one up: %s at %s." % (SOON_HOURS, _who(nxt[1]), _clock(nxt[0].isoformat())[1])) if nxt else ""
    for f, label, medal, n, floor, kind in BOARDS:
        if f != flag:
            continue
        ents, text = build(t, flag, inside) if inside else ([], "")
        ents = ents[:n]
        ranks = [_rank(t, flag, d["p"]) for d in ents]
        starts = sorted(x for x in (t._prop_start_utc(d["p"]) for d in ents) if x)
        first_clock = _clock(starts[0])[1] if starts else "soon"
        ready = len(ents) == n and all(r >= floor - 1e-9 for r in ranks)
        if ready:
            return _general_card(flag, label, medal, text, n, floor, kind, first_clock) + after
        head = "%s **%s -- NOT READY (%s)**" % (medal, label, kind)
        sub = ("_Needs %d picks all Ranking # %d+ starting in the next %d hours. Right now %d of %d%s. "
               "This is the SOON card (next %d hours only), not the regular %s board._"
               % (n, floor, SOON_HOURS, len(ents), n, (" (Ranking # " + " / ".join("%g" % r for r in ranks) + ")") if ranks else "",
                  SOON_HOURS, label.replace(" SOON", "")))
        if not ents:
            return "\n".join([head, sub, "No board props start in the next %d hours." % SOON_HOURS]) + after
        return _general_card(flag, label, medal, text, n, floor, kind, first_clock).replace(
            "%s **%s -- MAKE THIS SLIP (%s)**" % (medal, label, kind), head, 1).split("\n_", 1)[0] + "\n" + sub + "\n" + \
            "\n".join(_general_card(flag, label, medal, text, n, floor, kind, first_clock).split("\n")[2:]) + after
    return "unknown soon board"


def status_text():
    """Plain words on what the two soon boards look like right now (for testing / a command later)."""
    import apex_edge_tracker as t
    now = datetime.now(timezone.utc)
    inside, nxt = _window_picks(t, now)
    L = ["%d board props start in the next %d hours." % (len(inside), SOON_HOURS)]
    for flag, label, medal, n, floor, kind in BOARDS:
        ents, _ = build(t, flag, inside) if inside else ([], "")
        ents = ents[:n]
        ranks = [_rank(t, flag, d["p"]) for d in ents]
        ok = len(ents) == n and all(r >= floor for r in ranks)
        L.append("%s %s: %d of %d picks, Ranking # %s -> %s" % (medal, label, len(ents), n,
                 " / ".join("%g" % r for r in ranks) or "-", "READY" if ok else "not ready (needs %d picks all %d+)" % (n, floor)))
        for i, (d, r) in enumerate(zip(ents, ranks), 1):
            L.append("   %d) %s · %s %s %s · Ranking # %g" % (i, _who(d["p"]), _cat(d["p"]), d["p"].get("direction"), d["p"].get("line"), r))
    if nxt:
        L.append("Next match after the window: %s at %s." % (_who(nxt[1]), _clock(nxt[0].isoformat())[1]))
    return "\n".join(L)


if __name__ == "__main__":
    if "--status" in sys.argv:
        print(status_text())
    else:
        msgs = run(source="cli", post=not DRY)
        print("%d alert(s)" % len(msgs))
