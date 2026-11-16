"""APEX-PLAYTHIS-2026-11-04 (owner, "Go play this"): one 🎯 PLAY THIS card a day.

He gets a new board version every time lines move and had "no clue which one to actually play",
and he works at 5 AM so he cannot wait for the midnight board. Oct 13 to Nov 3 the board as it stood
at 8 PM CT won the same days as the midnight one (Top 3 13-8 vs 14-8), so:

- at PLAY_THIS_TIME (CT) the alerts job posts ONE card in #alerts and #general: Top 3 (Power),
  Elite 5 and Elite 6 (Flex), full names, in board order, only matches that have not started;
- every other board post that day carries an "Update only" note (apex_board_status.swap_title);
- if PrizePicks pulls a leg off that slip before it starts, a card names the BACKUP to use;
- the won-lost record is kept off this card only (data/play_this/log.json; 9 AM #system line;
  one result line per day in #alerts and #system). Void legs never lose (his rule 11/4).

Nothing here changes how boards are built or ranked. Undo: PLAY_THIS_ON = False (no restart
needed for the card; the header note needs a bot restart) or "go back to before-play-this".
"""
import os, sqlite3
from datetime import datetime, timezone, time as dtime
from zoneinfo import ZoneInfo

import apex_ops as ops

HERE = ops.HERE
CT = ZoneInfo("America/Chicago")
PLAY_THIS_ON = True
PLAY_THIS_TIME = "20:00"              # CT, the card posts on the first alerts run at or after this time
PLAY_THIS_CHANNELS = ("alerts", "general")
RESULT_CHANNELS = ("alerts", "system")
LOG = os.path.join(HERE, "data", "play_this", "log.json")
BH_DB = os.path.join(HERE, "data", "board_history.sqlite")
KINDS = (("top3", "\U0001F947", "TOP 3", "Power", 3), ("elite5", "\U0001F3C5", "ELITE 5", "Flex", 5),
         ("elite6", "\U0001F3C6", "ELITE 6", "Flex", 6))
CAT = {"total_games": "Total Games", "fantasy_score": "Fantasy Score", "aces": "Aces", "dfs": "Double Faults",
       "breaks": "Breaks", "games_won": "Games Won", "tiebreaks": "Tiebreaks", "total_sets": "Total Sets",
       "1st_set_total_games": "1st Set Games", "1st_set_total_games_won": "1st Set Games Won"}


# ------------------------------------------------------------------ helpers
def _clock_label():
    hh, mm = (int(x) for x in PLAY_THIS_TIME.split(":"))
    return datetime(2000, 1, 1, hh, mm).strftime("%-I:%M %p").replace(":00", "")


def _load():
    return ops._read_json(LOG, {"days": {}})


def _save(d):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
    except Exception:
        pass
    ops._write_json(LOG, d)


def _names(l):
    if l.get("player") == "match":
        return str(l.get("opponent") or "")
    return "%s v %s" % (l.get("player"), l.get("opponent"))


def _start_ct(l):
    try:
        return datetime.fromisoformat(l["start_utc"]).astimezone(CT).strftime("%a %-I:%M %p CT")
    except Exception:
        return "time TBA"


def _started(l, now):
    try:
        return bool(l.get("start_utc")) and datetime.fromisoformat(l["start_utc"]) <= now
    except Exception:
        return False


def _key(l):
    return "%s|%s|%s|%s|%s" % (l.get("match_id"), l.get("player"), l.get("category"), l.get("line"), l.get("direction"))


def leg_line(i, l):
    try:
        win = " · Win %d%%" % round(100 * float(l.get("win") or 0))
    except Exception:
        win = ""
    return "%d) %s · %s %s %s%s · %s" % (i, _names(l), CAT.get(l.get("category"), l.get("category")),
                                                l.get("direction"), l.get("line"), win, _start_ct(l))


def build(snap=None, now=None):
    """Legs for the three slips from the published board snapshot, started matches left out.
    Backups = next Top 10 picks (other matches) in board order."""
    import apex_slips_channel as S
    snap = snap if snap is not None else S._load(S.SNAP, {})
    now = now or datetime.now(timezone.utc)
    starts = S._starts(snap)
    boards = snap.get("boards") or {}

    def legs(flag):
        return [S._leg(e, starts, flag) for e in (boards.get(flag) or {}).get("entries") or []]
    t3, e6, t10 = legs("top3"), legs("elite6"), legs("top10")
    started = [l for l in t3 + e6 if _started(l, now)]
    t3 = [l for l in t3 if not _started(l, now)][:3]
    e6 = [l for l in e6 if not _started(l, now)][:6]

    def backups(main):
        used = {l.get("match_id") for l in main}
        out = []
        for l in t10:
            if l.get("match_id") in used or _started(l, now):
                continue
            used.add(l.get("match_id"))
            out.append(l)
        return out[:3]
    return {"top3": t3, "elite5": e6[:5], "elite6": e6, "backups": {"top3": backups(t3), "elite6": backups(e6)},
            "started": started, "captured_at": snap.get("captured_at", "")}


def card_text(day_label, b, posted_label):
    L = ["\U0001F3AF **PLAY THIS · %s · posted %s CT**" % (day_label, posted_label),
         "The only slip to place today. Every later board post is an update only.", ""]
    if b["top3"]:
        L.append("\U0001F947 **TOP 3 · Power**" + ("" if len(b["top3"]) == 3 else " (only %d picks tonight)" % len(b["top3"])))
        L += [leg_line(i, l) for i, l in enumerate(b["top3"], 1)]
    else:
        L.append("\U0001F947 **TOP 3**: no playable picks tonight")
    L.append("")
    if b["elite6"]:
        L.append("\U0001F3C6 **ELITE 6 · Flex** (Elite 5 = picks 1 to 5)" + ("" if len(b["elite6"]) == 6 else " (only %d picks tonight)" % len(b["elite6"])))
        L += [leg_line(i, l) for i, l in enumerate(b["elite6"], 1)]
    else:
        L.append("\U0001F3C6 **ELITE 6**: no playable picks tonight")
    bk = b["backups"].get("elite6") or b["backups"].get("top3") or []
    if bk:
        L += ["", "\U0001F501 If PrizePicks pulls a leg, use the next one down: " + " · ".join(
            "%d) %s %s %s %s" % (i, _names(l), CAT.get(l.get("category"), l.get("category")), l.get("direction"), l.get("line")) for i, l in enumerate(bk, 1))]
    if b["started"]:
        L += ["", "⏱ Left out, already started: " + ", ".join(_names(l) for l in b["started"])]
    return "\n".join(L)


# ------------------------------------------------------------------ the nightly card
def due(state, now_ct=None):
    now_ct = now_ct or datetime.now(CT)
    hh, mm = (int(x) for x in PLAY_THIS_TIME.split(":"))
    return now_ct.time() >= dtime(hh, mm) and state.get("play_this_day") != now_ct.date().isoformat()


def post_card(state, post=True, now_ct=None):
    now_ct = now_ct or datetime.now(CT)
    today = now_ct.date().isoformat()
    b = build()
    posted_label = now_ct.strftime("%-I:%M %p")
    text = card_text(now_ct.strftime("%a %b %-d"), b, posted_label)
    if post:
        for ch in PLAY_THIS_CHANNELS:
            ops.send_discord(ch, text)
    d = _load()
    d["days"][today] = {"posted_at": now_ct.isoformat(timespec="minutes"), "captured_at": b["captured_at"],
                        "slips": {k: b[k] for k, _e, _n, _m, _c in KINDS},
                        "backups": b["backups"], "results": {}, "pulled": [], "result_posted": False}
    _save(d)
    state["play_this_day"] = today
    state["play_this_posted"] = posted_label
    return text


def _pulled_reason(l, since):
    """The newest board-history reason for this pick after the card; 'pulled' means PrizePicks took the line off."""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % BH_DB, uri=True, timeout=10)
        r = c.execute("select h.reason from history h join publishes p using(publish_id) where h.player=? and h.opponent=? "
                      "and h.category=? and h.line=? and p.published_at>? order by p.published_at desc limit 1",
                      (l.get("player"), l.get("opponent"), l.get("category"), float(l.get("line")), since)).fetchone()
        c.close()
        return (r[0] or "") if r else ""
    except Exception:
        return ""


def pulled_check(state, post=True):
    """Once per leg: PrizePicks pulled a PLAY THIS leg before it started -> card naming the backup."""
    out = []
    d = _load()
    today = datetime.now(CT).date().isoformat()
    e = d["days"].get(today)
    if not e:
        return out
    now = datetime.now(timezone.utc)
    changed = False
    for kind in ("top3", "elite6"):
        for i, l in enumerate(e["slips"].get(kind) or [], 1):
            k = _key(l)
            if k in e["pulled"] or _started(l, now):
                continue
            why = _pulled_reason(l, e["posted_at"])
            if "pulled" not in why.lower():
                continue
            e["pulled"].append(k)
            changed = True
            bk = next((x for x in (e["backups"].get(kind) or []) if not _started(x, now) and _key(x) not in e["pulled"]), None)
            msg = "\U0001F3AF **PLAY THIS update** · PrizePicks pulled **%s** (%s #%d)." % (
                _names(l), dict((k_, n_) for k_, _e, n_, _m, _c in KINDS)[kind].title(), i)
            msg += (" Use this instead: **%s**" % leg_line(1, bk)[3:]) if bk else " No backup left; play the slip one leg shorter."
            if post:
                for ch in PLAY_THIS_CHANNELS:
                    ops.send_discord(ch, msg)
            out.append(msg)
    if changed:
        _save(d)
    return out


# ------------------------------------------------------------------ record
def _leg_result(l, fwd, hand, S):
    a = S._leg_actual(dict(l), fwd, hand)
    if a is None:
        return None, None
    try:
        line = float(l["line"])
    except (TypeError, ValueError):
        return None, None
    return ("void" if a == line else ("hit" if (a > line) == (str(l["direction"]).upper() == "MORE") else "miss")), a


def slip_result(results, n=None):
    """won = every leg hit or void (a void never loses); lost = any miss; open = something waiting;
    short = fewer legs than the slip needs (not counted either way)."""
    rs = [r.get("result") for r in results]
    if n and len(rs) < n:
        return "short"
    if any(r == "miss" for r in rs):
        return "lost"
    if any(r is None for r in rs):
        return "open"
    return "won"


def grade(post=True):
    """Grades every logged day; posts one result line per day once all three slips are decided."""
    out = []
    import apex_slips_channel as S
    d = _load()
    fwd, hand = S._actuals()
    changed = False
    for day in sorted(d["days"]):
        e = d["days"][day]
        res = e.setdefault("results", {})
        for kind, _em, _nm, _mode, _n in KINDS:
            legs = e["slips"].get(kind) or []
            rows = res.get(kind) or [{} for _ in legs]
            for l, r in zip(legs, rows):
                if r.get("result"):
                    continue
                rr, a = _leg_result(l, fwd, hand, S)
                if rr:
                    r["result"], r["actual"] = rr, a
                    changed = True
            res[kind] = rows
        if not e.get("result_posted") and e["slips"].get("top3") is not None:
            verdicts = {k: slip_result(res.get(k) or [{} for _ in e["slips"][k]], c) if e["slips"].get(k) else "none" for k, _e, _n, _m, c in KINDS}
            if all(v != "open" for v in verdicts.values()):
                bits = []
                for kind, em, nm, _mode, _n in KINDS:
                    v = verdicts[kind]
                    if v == "none":
                        continue
                    miss = [_names(l) for l, r in zip(e["slips"][kind], res.get(kind) or []) if r.get("result") == "miss"]
                    bits.append("%s %s %s%s" % (em, nm.title(), {"won": "✅ won", "lost": "❌ lost", "short": "⚠️ too few picks, not counted"}[v],
                                                (" (%s missed)" % ", ".join(miss)) if miss else ""))
                msg = "\U0001F3AF **PLAY THIS %s result** · %s" % (datetime.fromisoformat(day).strftime("%a %b %-d"), " · ".join(bits))
                msg += "\n" + record_text(d)
                if post:
                    for ch in RESULT_CHANNELS:
                        ops.send_discord(ch, msg)
                out.append(msg)
                e["result_posted"] = True
                changed = True
    if changed:
        _save(d)
    return out


def record_text(d=None):
    d = d or _load()
    days = d.get("days") or {}
    if not days:
        return "\U0001F3AF PLAY THIS: no card posted yet"
    first = datetime.fromisoformat(min(days)).strftime("%b %-d")
    bits = []
    for kind, em, nm, _mode, n in KINDS:
        w = l = o = sh = 0
        for e in days.values():
            if not e["slips"].get(kind):
                continue
            v = slip_result((e.get("results") or {}).get(kind) or [{} for _ in e["slips"][kind]], n)
            w += v == "won"; l += v == "lost"; o += v == "open"; sh += v == "short"
        bits.append("%s %s won %d lost %d%s%s" % (em, nm.title(), w, l, (" (%d waiting)" % o) if o else "",
                                                 (" (%d short)" % sh) if sh else ""))
    return "\U0001F3AF PLAY THIS record since %s (the %s card only): " % (first, _clock_label()) + " · ".join(bits)


def header_note():
    """Prefix for every board-update title while PLAY THIS is on."""
    if not PLAY_THIS_ON:
        return ""
    return "ℹ️ Update only · PLAY THIS card at %s · " % _clock_label()


def run(state, post=True):
    """Called from apex_alerts.check() every 30 min. Never raises."""
    out = []
    if not PLAY_THIS_ON:
        return out
    try:
        if due(state):
            out.append(post_card(state, post=post))
    except Exception as e:
        print("[play this] card skipped: %s: %s" % (type(e).__name__, e))
    try:
        out += pulled_check(state, post=post)
    except Exception as e:
        print("[play this] pulled check skipped: %s: %s" % (type(e).__name__, e))
    try:
        out += grade(post=post)
    except Exception as e:
        print("[play this] grading skipped: %s: %s" % (type(e).__name__, e))
    return out


if __name__ == "__main__":
    import sys
    if "--record" in sys.argv:
        print(record_text())
    else:
        now = datetime.now(CT)
        print(card_text(now.strftime("%a %b %-d"), build(), now.strftime("%-I:%M %p")))
