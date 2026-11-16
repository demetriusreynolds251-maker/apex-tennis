#!/usr/bin/env python3
"""APEX-BOARDSTATUS-HEADER-2026-10-25 (owner, go 10/25: "It would be nice if the boards
unchanged message was bigger, or more of what I can see first, or in the header. Also when it
is a change I would like it to be updated as well").

One BIG status leads every board-update message, his betting boards first.

APEX-ALERTCARDS-2026-10-25 (owner, go 10/25 5:44 PM: "It needs to be more clear on what
moved, it looks confusing, add some emojis"): one line per pick with arrows, a plain WHY under
every pick that came in, left or dropped (from board history), and the full new lineup:

  🔁 TOP 3 CHANGED · ELITE 6 CHANGED
  🥇 TOP 3
  ⬆️ Kraus v Udvardy · TG MORE 17.5 · #2 → #1
  ⬆️ Snigur v Bondar · TG MORE 17.5 · #3 → #2
  ➕ Wang v Parks · TG MORE 17.5 (80%) · IN at #3
     ↳ was Backup 1 · took the spot Vacherot left
  ⬇️ Vacherot v Navone · TG MORE 18.5 · #1 → Backup 1
     ↳ Win Prob fell 80.9% → 79.7% at the odds update (match odds moved: favorite 64% → 66% to win)
       · now just behind Wang v Parks (score 100.0 vs 99.7)
  📋 Top 3 now: 1️⃣ Kraus v Udvardy TG 17.5 · 2️⃣ Snigur v Bondar TG 17.5 · 3️⃣ Wang v Parks TG 17.5

  Other boards: one line each (🔟 Top 10, 🟢 Best Goblin, ⚪ Best Standard, 🔴 Best Demon,
  📚 All Props with why its 70%+ count changed)

Before/after = the last posted board vs the new one (main spots), from the board status log
captures every publish writes, or two spot lists the caller passes (#picks plays card,
pulled-line fills). Display only: reads local files, never changes a pick, a board or a rule.
Never raises -- on any failure the callers get a None status and post their own message only.
"""
import contextlib, io, os, sqlite3
from datetime import datetime, timedelta, timezone

MAIN = (("top3", "TOP 3", "Top 3", "\U0001F947", 3), ("elite6", "ELITE 6", "Elite 6", "\U0001F3C5", 6))
OTHER = (("top10", "Top 10", "Top 10", 10), ("bestgoblin", "Best Goblin", "Goblin", 6),
         ("beststandard", "Best Standard", "Standard", 6), ("bestdemon", "Best Demon", "Demon", 6))
OTHER_EMOJI = {"top10": "\U0001F51F", "bestgoblin": "\U0001F7E2", "beststandard": "⚪", "bestdemon": "\U0001F534"}
LIMIT = {f: n for f, _a, _b, _c, n in MAIN}
LIMIT.update({f: n for f, _a, _b, n in OTHER})
NAME = {f: b for f, _a, b, _c, _n in MAIN}
NAME.update({f: a for f, a, _b, _n in OTHER})
HEADER_ON = True   # False = every message posts exactly as before (display only, not a board rule)
WHY_ON = True      # False = no "↳ why" lines under the moves
SWAPS_ONLY = True
# APEX-MINICHANGE-2026-10-27 (owner 8:41 PM: "just give me alert for top3 and elite6 with mini version
# when 2 or more new props for top and 3 for elite 6"): the TOP 3 / ELITE 6 status header + swap lines only go
# on a card when at least MINI_MIN_NEW[board] picks came IN on that board (first board of the day and an
# EMPTY spot always show). Below that the card is the plain event card. MINI_MIN_NEW = {} restores every change.
MINI_MIN_NEW = {}   # 8:54 PM: "I didn't want it for the regular only for the sooner" -- regular boards show every swap  # APEX-SWAPLINES-2026-10-25: #alerts board cards = swap lines only; False = the full card
MAX_LINES_PER_BOARD = 8
EMPTY_REASON = "no pick passes the board rules"
KEYCAP = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣", 10: "\U0001F51F"}
ICON = {"up": "⬆️", "down": "⬇️", "in": "➕", "out": "⬇️", "pulled": "\U0001F6AB", "started": "⏱️"}
TRIGGER_WORDS = {"autoeval": "the odds update", "reprice": "the hourly re-price", "manual": "the /fetch re-score",
                 "auto": "the 5 PM run", "5pm_fetch": "the 5 PM run", "restart": "a re-score",
                 "category_gate": "the category check"}


def _clock(ts=None):
    try:
        t = datetime.fromisoformat(ts) if isinstance(ts, str) else (ts or datetime.now())
        return t.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return datetime.now().strftime("%I:%M %p").lstrip("0")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def spot(r, prob=None):
    """One board spot from a status-log row or a pick-log / board prop."""
    import apex_edge_tracker as T
    import apex_odds_watch as W
    import apex_board_history as BH
    row = dict(r)
    if prob is not None:
        row["prob"] = prob
    who = _who(row)
    cat = W.CAT_SHORT.get(row.get("category"), str(row.get("category") or "").replace("_", " ").title())
    ln = _f(row.get("line"))
    what = "%s %s %s" % (cat, str(row.get("direction") or "").upper(), ("%g" % ln) if ln is not None else row.get("line"))
    return {"id": list(T._apex_prop_identity(row)), "label": W._pick_label(row),
            "short": W._short_label(row), "who": who, "what": what, "prob": _f(row.get("prob")),
            "pkey": BH.prop_key(row),
            "start": str(r.get("start_utc") or r.get("slip_start_utc") or "")}


def _who(r):
    import apex_odds_watch as W
    if str(r.get("player") or "").strip().lower() == "match":
        return " v ".join(W._surname(x) for x in str(r.get("opponent") or "").split(" v "))
    return W._surname(r.get("player"))


def _started(s):
    try:
        st = datetime.fromisoformat(s["start"])
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        return st <= datetime.now(timezone.utc)
    except Exception:
        return False


def _key(s):
    return tuple(s["id"])


def _pct(p):
    return "" if p is None else " (%.0f%%)" % (100 * p)


def _pick(s, prob=False):
    return "%s · %s%s" % (s.get("who") or s.get("short"), s.get("what") or "", _pct(s.get("prob")) if prob else "")


def _short_pick(s):
    """'Kraus v Udvardy TG 17.5' (no MORE) for lineups and one-line summaries."""
    w = (s.get("what") or "").replace(" MORE ", " ")
    return ("%s %s" % (s.get("who") or "", w)).strip() if s.get("who") else s.get("short", "")


def compare(before, after, backups=None, gone_reasons=None, when=None, first=False, whys=None, in_notes=None):
    """before/after = {flag: [spot, ...] in spot order}. backups = {flag: {identity: 'Backup 1'}}
    for the NEW board; gone_reasons = {flag: {identity: 'pulled by PrizePicks'}}; whys =
    {flag: {identity: 'plain why'}} for picks that came in/left/dropped; in_notes = {flag:
    {identity: 'moved up from Backup ...'}}. Returns the status dict the render helpers take."""
    backups = backups or {}
    gone_reasons = gone_reasons or {}
    whys = whys or {}
    in_notes = in_notes or {}
    st = {"when": _clock(when), "first": first, "main": {}, "other": {}}
    for flag in list(LIMIT):
        if flag not in after and flag not in before:
            continue
        old = list(before.get(flag) or [])
        new = list(after.get(flag) or [])
        old_pos = {_key(s): i for i, s in enumerate(old, 1)}
        new_pos = {_key(s): i for i, s in enumerate(new, 1)}
        wy = whys.get(flag) or {}
        items = []
        out_names = []
        for s in old:
            k = _key(s)
            if k in new_pos:
                continue
            bno = (backups.get(flag) or {}).get(k)
            why = (gone_reasons.get(flag) or {}).get(k)
            kind = "out"
            if why:
                to = why
                kind = "pulled" if "pulled" in why else "out"
            elif bno:
                to = bno
            elif _started(s):
                to, kind = "match started", "started"
            else:
                to = "off board"
            items.append({"kind": kind, "s": s, "frm": "#%d" % old_pos[k], "to": to, "sort": old_pos[k],
                          "why": wy.get(k) or ("" if kind == "pulled" else "")})
            out_names.append(s.get("who") or s.get("short"))
        for i, s in enumerate(new, 1):
            k = _key(s)
            if k not in old_pos:
                note = (in_notes.get(flag) or {}).get(k) or wy.get(k) or ""
                items.append({"kind": "in", "s": s, "frm": "", "to": "#%d" % i, "sort": i, "why": note})
            elif old_pos[k] != i:
                kind = "up" if i < old_pos[k] else "down"
                items.append({"kind": kind, "s": s, "frm": "#%d" % old_pos[k], "to": "#%d" % i, "sort": i,
                              "why": wy.get(k) if kind == "down" else ""})
        items.sort(key=lambda x: (0 if x["kind"] in ("up", "in") else 1, x["sort"]))
        lim = LIMIT[flag]
        empty = []
        left = [it["s"] for it in items if it["kind"] in ("out", "pulled", "started")]
        n_in = sum(1 for it in items if it["kind"] == "in")
        left = left[n_in:]   # picks that left with nothing coming in for them
        for n_e, i in enumerate(range(len(new) + 1, lim + 1)):
            why = EMPTY_REASON
            g = left[n_e] if n_e < len(left) else None
            if g and (gone_reasons.get(flag) or {}).get(_key(g)):
                why = "%s %s, no backup qualifies" % (_short_pick(g), gone_reasons[flag][_key(g)])
            elif g and _started(g):
                why = "%s started, no backup qualifies" % _short_pick(g)
            empty.append((i, why))
        ent = {"changed": bool(items) and not first, "items": items, "empty": empty, "now": new,
               "n": len(new), "limit": lim}
        ent["lines"] = item_lines(ent, why=False)
        st["main" if flag in ("top3", "elite6") else "other"][flag] = ent
    return st


def item_line(it):
    s = it["s"]
    k = it["kind"]
    if k == "in":
        return "%s %s · IN at %s" % (ICON["in"], _pick(s, prob=True), it["to"])
    if k in ("up", "down"):
        return "%s %s · %s → %s" % (ICON[k], _pick(s), it["frm"], it["to"])
    return "%s %s · %s → %s" % (ICON.get(k, ICON["out"]), _pick(s), it["frm"], it["to"])


def item_lines(ent, why=True, limit=None):
    out = []
    items = ent.get("items") or []
    limit = limit or MAX_LINES_PER_BOARD
    for it in items[:limit]:
        out.append(item_line(it))
        if why and WHY_ON and it.get("why") and it["kind"] != "up":
            out.append(" ↳ " + it["why"])
    if len(items) > limit:
        out.append("+%d more" % (len(items) - limit))
    return out


def lineup(flag, ent, compact=False):
    now = ent.get("now") or []
    if not now:
        return "\U0001F4CB %s now: empty" % NAME[flag]
    if compact:
        return "\U0001F4CB %s now: %s" % (NAME[flag], " · ".join(
            "%d) %s" % (i, s.get("who") or s.get("short")) for i, s in enumerate(now, 1)))
    return "\n".join(["\U0001F4CB **%s now:**" % NAME[flag]] + [
        "%s %s%s" % (KEYCAP.get(i, "#%d" % i), _short_pick(s), _pct(s.get("prob"))) for i, s in enumerate(now, 1)])


# ------------------------------------------------------------------ APEX-SWAPLINES-2026-10-25
# owner (go 10/25): "I'll rather it be like Josh green ______ some emoji the blank area then
# the person he been switch by and line etc ... too much unnecessary information". The #alerts
# board card shows only what he has to change on a slip:
#   🟢 Wang v Parks · TG MORE 17.5 · 80% 🔄 🔴 Vacherot v Navone · TG MORE 18.5 · 80%
#   🔴 Rune v Halys · TG MORE 19.5 🚫 pulled · ⚠️ spot EMPTY
#   🔁 Paul v Rublev · TG MORE 19.5 → 20.5
#   📋 Top 3 now: 1️⃣ Kraus 17.5 · 2️⃣ Snigur 17.5 · 3️⃣ Wang 17.5
# Spot-only shifts (#2 → #1), WHY lines and other boards stay off the card (WHY is in board_history).
def _pct0(p):
    return "" if p is None else " · %.0f%%" % (100 * p)


def _base(s):
    """'TG MORE' from 'TG MORE 17.5' (prop + side, no line)."""
    return (s.get("what") or "").rsplit(" ", 1)[0]


def _line_of(s):
    return (s.get("what") or "").rsplit(" ", 1)[-1]


def _score0(v, other=None):
    """APEX-SCOREDISPLAY-2026-10-25: ' · Ranking # 100' (one decimal when the other side of the swap is
    within 1 point: 'Score 99.7'), or '' with no board-history score."""
    try:
        import apex_edge_tracker as T
        if v is None or not T.SCORE_DISPLAY:
            return ""
        dec = 1 if (other is not None and abs(v - other) < 1) else 0
        txt = T._score_fmt(v, dec)
        return " · Ranking # %s" % (txt[:-2] if txt.endswith(".0") else txt)
    except Exception:
        return ""


def _green(s, score=None, other=None):
    return "\U0001F7E2 %s · %s%s%s" % (s.get("who") or s.get("short"), s.get("what") or "", _pct0(s.get("prob")),
                                     _score0(score, other))


def _red(it, other=None):
    s = it["s"]
    base = "\U0001F534 %s · %s" % (s.get("who") or s.get("short"), s.get("what") or "")
    if it["kind"] == "pulled":
        return base + " \U0001F6AB pulled"
    if it["kind"] == "started":
        return base + " ⏱️ started"
    p = it.get("now_prob", s.get("prob"))
    return base + _pct0(p) + _score0(it.get("score"), other)


def swap_lines(ent):
    """One line per swap on this board (see above). [] when the same picks are on it."""
    items = ent.get("items") or []
    ins = sorted([it for it in items if it["kind"] == "in"], key=lambda x: x["sort"])
    outs = sorted([it for it in items if it["kind"] in ("out", "pulled", "started")], key=lambda x: x["sort"])
    out = []
    for it in list(ins):   # same match, same prop, new line
        s = it["s"]
        m = next((o for o in outs if o["s"].get("who") == s.get("who") and _base(o["s"]) == _base(s)), None)
        if m:
            out.append("\U0001F501 %s · %s → %s" % (s.get("who") or s.get("short"), m["s"].get("what"), _line_of(s)))
            ins.remove(it)
            outs.remove(m)
    for a, b in zip(ins, outs):
        out.append("%s \U0001F504 %s" % (_green(a["s"], a.get("score"), b.get("score")), _red(b, a.get("score"))))
    n = min(len(ins), len(outs))
    n_empty = len(ent.get("empty") or [])
    for b in outs[n:]:
        out.append(_red(b) + (" · ⚠️ spot EMPTY" if n_empty > 0 else ""))
        n_empty -= 1
    for a in ins[n:]:
        out.append("%s · \U0001F195 filled #%s" % (_green(a["s"], a.get("score")), a["to"].lstrip("#")))
    if not out and ent.get("empty"):
        out.append("⚠️ #%s still EMPTY" % ", #".join(str(i) for i, _w in ent["empty"]))
    return out


def lineup_line(flag, ent):
    """'📋 Top 3 now: 1️⃣ Kraus 17.5 · 2️⃣ Snigur 17.5 · 3️⃣ Wang 17.5'."""
    now = ent.get("now") or []
    if not now:
        return "\U0001F4CB %s now: empty" % NAME[flag]
    parts = []
    for i, s in enumerate(now, 1):
        who = (s.get("who") or s.get("short") or "").split(" v ")[0]
        w = (s.get("what") or "").split(" ")
        cat, side, ln = " ".join(w[:-2]), (w[-2] if len(w) > 1 else ""), w[-1]
        bits = [who] + ([cat] if cat and cat != "TG" else []) + (["less"] if side == "LESS" else []) + [ln]
        parts.append("%s %s" % (KEYCAP.get(i, "#%d" % i), " ".join(b for b in bits if b)))
    return "\U0001F4CB %s now: %s" % (NAME[flag], " · ".join(parts))


def _swap_boards(st):
    """[(flag, name, badge, entry)] -- Top 3 / Elite 6, or the one other board a status covers."""
    if st["main"]:
        return [(f, nm, badge, st["main"][f]) for f, _u, nm, badge, _l in MAIN if f in st["main"]]
    return [(f, nm, OTHER_EMOJI.get(f, "•"), st["other"][f]) for f, nm, _s, _l in OTHER if f in st["other"]]


def big_change(st):
    """APEX-MINICHANGE-2026-10-27: True when the status is worth a TOP 3 / ELITE 6 card: first board of the
    day, an empty spot, or at least MINI_MIN_NEW[board] picks that came IN on Top 3 / Elite 6."""
    if not st:
        return False
    if st.get("first") or not MINI_MIN_NEW:
        return True
    for f, _n, _b, e in _swap_boards(st):
        if e.get("empty"):
            return True
        n_in = sum(1 for it in e.get("items") or [] if it["kind"] == "in")
        if n_in >= MINI_MIN_NEW.get(f, 1):
            return True
    return False


def has_swaps(st):
    """True when a pick came in, left, changed its line, or a spot is empty."""
    return bool(st) and any(swap_lines(e) for _f, _n, _b, e in _swap_boards(st))


def change_reason(st, flags=None):
    """APEX-CHANGEWHY-HEADER-2026-10-27 (owner: "I need to know on the header change due to
    props currently drop because of live or props replaced with better props"): one short phrase
    for the header, e.g. '2 picks started' / '1 replaced by a better prop' / '1 pulled by
    PrizePicks' / '1 dropped (no longer qualifies)'. Each pick counted once across the boards."""
    try:
        started, pulled, replaced, dropped, vac = {}, {}, {}, {}, 0
        ents = [(f, e) for f, _n, _b, e in _swap_boards(st) if not flags or f in flags]
        for f, e in ents:
            items = e.get("items") or []
            n_in = sum(1 for it in items if it["kind"] == "in")
            gone = 0
            for it in items:
                k = _key(it["s"])
                if it["kind"] == "started":
                    started[k] = 1; gone += 1
                elif it["kind"] == "pulled":
                    pulled[k] = 1; gone += 1
                elif it["kind"] == "out":
                    gone += 1
                    if str(it.get("to") or "").lower().startswith("backup"):
                        replaced[k] = 1
                    else:
                        dropped[k] = 1
            vac += max(0, n_in - gone)   # came in without anyone leaving = a better prop took a spot
        parts = []
        if started:
            parts.append("%d pick%s started" % (len(started), "" if len(started) == 1 else "s"))
        n_rep = len(replaced) + vac
        if n_rep:
            parts.append("%d replaced by a better prop" % n_rep)
        if pulled:
            parts.append("%d pulled by PrizePicks" % len(pulled))
        if dropped:
            parts.append("%d dropped (no longer qualifies)" % len(dropped))
        return " · ".join(parts)
    except Exception:
        return ""


def _play_this_note():
    """APEX-PLAYTHIS-2026-11-04: 'Update only · PLAY THIS card at 8 PM · ' in front of every board-update title."""
    try:
        import apex_play_this as _pt
        return _pt.header_note()
    except Exception:
        return ""


def swap_title(st):
    if st.get("first"):
        return (_play_this_note() + title(st))[:256]
    bs = _swap_boards(st)
    ch = [nm.upper() for _f, nm, _b, e in bs if any(it["kind"] != "up" and it["kind"] != "down"
                                                   for it in e.get("items") or [])]
    t = ("\U0001F501 %s CHANGED" % " & ".join(ch)) if ch else "✅ %s UNCHANGED" % " & ".join(nm.upper() for _f, nm, _b, _e in bs)
    if ch:
        _why = change_reason(st)
        if _why:
            t += " -- " + _why
    empties = ["%s #%s" % (nm.upper(), ",".join(str(i) for i, _w in e["empty"])) for _f, nm, _b, e in bs if e["empty"]]
    if empties:
        t += " · ⚠️ %s EMPTY" % " & ".join(empties)
    return (_play_this_note() + t)[:256]


def swap_color(st):
    import apex_alert_cards as C
    bs = _swap_boards(st)
    if any(e["empty"] for _f, _n, _b, e in bs):
        return C.RED
    return C.ORANGE if has_swaps(st) else C.GREEN


def swap_body(st):
    """Per board: '🥇 **Top 3**', its swap lines; then one lineup line per changed board."""
    if st.get("first"):
        return [lineup_line(f, e) for f, _n, _b, e in _swap_boards(st)]
    out, ups = [], []
    for f, nm, badge, e in _swap_boards(st):
        ls = swap_lines(e)
        if not ls:
            continue
        out.append("%s **%s**" % (badge, nm))
        out += ls
        ups.append(lineup_line(f, e))
    if not out:
        return ["✅ %s unchanged" % " & ".join(nm for _f, nm, _b, _e in _swap_boards(st))]
    same = [nm for f, nm, _b, e in _swap_boards(st) if not swap_lines(e)]
    return out + (["✅ %s: same picks" % " & ".join(same)] if same else []) + [""] + ups


# ------------------------------------------------------------------ why it changed (board history)
def _hist_rows(cap_after, flags):
    """({flag: {prop_key: row}} for the board-history publish that wrote cap_after,
    the same for the publish before it, the newer publish's trigger source)."""
    import apex_board_history as BH
    c = sqlite3.connect(BH.DB, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        t0 = datetime.fromisoformat(cap_after)
        r = c.execute("SELECT publish_id, published_at, source FROM publishes WHERE platform = 'prizepicks' "
                      "AND published_at >= ? AND published_at <= ? ORDER BY published_at, publish_id LIMIT 1",
                      ((t0 - timedelta(seconds=5)).isoformat(timespec="seconds"),
                       (t0 + timedelta(minutes=4)).isoformat(timespec="seconds"))).fetchone()
        if not r:
            return {}, {}, ""
        p = c.execute("SELECT publish_id FROM publishes WHERE platform = 'prizepicks' AND published_at < ? "
                      "ORDER BY published_at DESC, publish_id DESC LIMIT 1", (r["published_at"],)).fetchone()
        want = tuple(flags) + ("allprops",)
        q = "SELECT * FROM history WHERE publish_id = ? AND board IN (%s)" % ",".join("?" * len(want))
        cur, prev = {}, {}
        for x in c.execute(q, (r["publish_id"],) + want):
            d = dict(x)
            cur.setdefault(d["board"], {})
            if d["spot_kind"] != "off" or d["prop_key"] not in cur[d["board"]]:
                cur[d["board"]][d["prop_key"]] = d
        if p:
            for x in c.execute(q, (p["publish_id"],) + want):
                d = dict(x)
                prev.setdefault(d["board"], {})
                if d["spot_kind"] != "off" or d["prop_key"] not in prev[d["board"]]:
                    prev[d["board"]][d["prop_key"]] = d
        return cur, prev, r["source"] or ""
    finally:
        c.close()


def _spot_words(row):
    if not row:
        return "off the board"
    k = row.get("spot_kind")
    if k == "main":
        return "#%d" % int(row.get("spot_no") or 0)
    if k == "backup":
        return "Backup %d" % int(row.get("spot_no") or 0)
    if k == "held":
        return "held"
    return "off the board"


def _clean_reason(txt):
    t = str(txt or "").strip()
    t = t.replace("PP pulled line (not in this pull)", "PrizePicks pulled the line").replace(
        "PP pulled line", "PrizePicks pulled the line")
    t = t.replace("Win Probability under 70%", "fell under 70% Win Prob")
    t = t.replace("held back: ", "held: ")
    return t


def _wp_change(prev, cur, source):
    a, b = _f((prev or {}).get("win_prob")), _f((cur or {}).get("win_prob"))
    if a is None or b is None or abs(a - b) < 0.1:
        return ""
    cause = []
    pb, cb = _f((prev or {}).get("bet365_total")), _f((cur or {}).get("bet365_total"))
    if pb is not None and cb is not None and pb != cb:
        cause.append("bet365 total %g → %g" % (pb, cb))
    pf, cf = _f((prev or {}).get("favorite_pct")), _f((cur or {}).get("favorite_pct"))
    if pf is not None and cf is not None and abs(pf - cf) >= 0.5:
        cause.append("match odds moved: favorite %.0f%% → %.0f%% to win" % (pf, cf) +
                     (", fewer games expected" if cf > pf and (cur or {}).get("category") == "total_games"
                      and (cur or {}).get("direction") == "MORE" else ""))
    pn, cn = _f((prev or {}).get("net_signals")), _f((cur or {}).get("net_signals"))
    if pn is not None and cn is not None and pn != cn:
        cause.append("signals %+d → %+d" % (pn, cn))
    txt = "Win Prob %s %.1f%% → %.1f%% at %s" % ("fell" if b < a else "rose", a + 1e-6, b + 1e-6,
                                                 TRIGGER_WORDS.get(source, "the re-score"))
    return txt + (" (%s)" % "; ".join(cause) if cause else "")


def _ahead_of(board_rows, row):
    """The pick sitting just above this one on the new board (main spots, then backups)."""
    seq = sorted([r for r in board_rows.values() if r.get("spot_kind") in ("main", "backup")],
                 key=lambda r: (0 if r["spot_kind"] == "main" else 1, r.get("spot_no") or 0))
    keys = [r["prop_key"] for r in seq]
    if row and row["prop_key"] in keys:
        i = keys.index(row["prop_key"])
        return seq[i - 1] if i > 0 else None
    mains = [r for r in seq if r["spot_kind"] == "main"]
    return mains[-1] if mains else None


def _rowname(r):
    import apex_board_history as BH
    return BH._short(r)


def why_for(kind, flag, s, cur, prev, source):
    """One plain sentence: why this pick came in / left / dropped. '' when board history has nothing."""
    k = s.get("pkey")
    rows = cur.get(flag) or {}
    row = rows.get(k)
    old = (prev.get(flag) or {}).get(k)
    if kind == "in":
        if not row:
            return ""
        was = row.get("prev_spot") or ""
        wc = str(row.get("why_changed") or "")
        if wc.startswith("new prop scored") or (not was and not old):
            return "new prop just scored"
        bits = []
        if was.startswith("BACKUP"):
            bits.append("was Backup %s" % was.split()[-1])
        elif was == "HELD":
            bits.append("was held (%s)" % _clean_reason((old or {}).get("reason")) if old else "was held")
        elif was:
            bits.append("was off this board")
        w = _wp_change(old or (prev.get("allprops") or {}).get(k), row, source)
        if w:
            bits.append(w)
        return " · ".join(bits)
    # out / down / pulled
    if row is None or row.get("spot_kind") == "off":
        r2 = row or (cur.get("allprops") or {}).get(k)
        wc = str((row or {}).get("why_changed") or "")
        if "pulled" in wc or (r2 is None):
            mv = (row or {}).get("why_changed") or ""
            if "line moved" in mv:
                return "PrizePicks moved the line (%s)" % mv.split("line moved", 1)[1].strip()
            return "PrizePicks pulled the line"
        bits = []
        reason = _clean_reason(r2.get("reason") or wc.replace("left the board: ", ""))
        if "started" in reason:
            return "match started"
        w = _wp_change(old, r2, source)
        if w:
            bits.append(w)
        if reason and not reason.startswith("on board"):
            bits.append(reason.split(" (Win Prob")[0])
        return " · ".join(bits) or "no longer qualifies for this board"
    bits = []
    if row.get("spot_kind") == "held":
        bits.append(_clean_reason(row.get("reason")) or "held")
    w = _wp_change(old, row, source)
    if w:
        bits.append(w)
    ah = _ahead_of(rows, row)
    if ah is not None and ah.get("rank_score") is not None and row.get("rank_score") is not None:
        gap = ah["rank_score"] - row["rank_score"]
        bits.append("now %sbehind %s (score %.1f vs %.1f)" % ("just " if 0 <= gap < 1 else "", _rowname(ah),
                                                              ah["rank_score"], row["rank_score"]))
    return " · ".join(bits)


def _fill_whys(st, cap_after):
    """Adds the board-history WHY to every in / out / down item of st (in place). Never raises."""
    if not WHY_ON:
        return
    try:
        flags = list(st["main"]) + list(st["other"])
        cur, prev, source = _hist_rows(cap_after, flags)
        if not cur:
            return
        for grp in ("main", "other"):
            for flag, ent in st[grp].items():
                for it in ent.get("items") or []:
                    if it["kind"] in ("up", "pulled") or it.get("why"):
                        continue
                    it["why"] = why_for("in" if it["kind"] == "in" else "out", flag, it["s"], cur, prev, source)
                    if it["kind"] == "out":   # name the spot it went to exactly as history recorded it
                        row = (cur.get(flag) or {}).get(it["s"].get("pkey"))
                        if it["why"].startswith("PrizePicks pulled") or it["why"].startswith("PrizePicks moved"):
                            it["kind"], it["to"] = "pulled", "pulled by PrizePicks"
                        elif row and row.get("spot_kind") in ("backup", "held"):
                            it["to"] = _spot_words(row).replace("held", "HELD")
                # an IN with no history reason: name the pick whose spot it took
                gone = [it["s"] for it in ent.get("items") or [] if it["kind"] in ("out", "pulled", "started")]
                for it in ent.get("items") or []:
                    if it["kind"] == "in" and gone:
                        g = gone.pop(0)
                        tail = "took the spot %s left" % (g.get("who") or g.get("short"))
                        it["why"] = (it["why"] + " · " + tail) if it.get("why") else tail
    except Exception as e:
        print("[BoardStatus] why lines skipped (%s: %s)" % (type(e).__name__, e))


# ------------------------------------------------------------------ All Props 70%+ count
def _allprops_change(rows, cap_before, cap_after):
    """{'before', 'after', 'parts': ['9 pulled by PrizePicks', ...]} -- why the 70%+ count moved."""
    import apex_edge_tracker as T

    def _all(cap):
        return {T._apex_prop_identity(r): r for r in rows if r["captured_at"] == cap and r.get("board") == "all"}
    b_all, a_all = _all(cap_before), _all(cap_after)
    b_el = {k for k, r in b_all.items() if r.get("status") == "eligible"}
    a_el = {k for k, r in a_all.items() if r.get("status") == "eligible"}
    if len(b_el) == len(a_el) and b_el == a_el:
        return None
    try:
        t_after = datetime.fromisoformat(cap_after).astimezone(timezone.utc)
    except Exception:
        t_after = datetime.now(timezone.utc)
    pulled = started = under = other = new = rose = 0
    for k in b_el - a_el:
        r = a_all.get(k)
        try:
            st = datetime.fromisoformat(b_all[k].get("start_utc"))
            st = st if st.tzinfo else st.replace(tzinfo=timezone.utc)
            is_started = st <= t_after
        except Exception:
            is_started = False
        if is_started:
            started += 1
        elif r is None:
            pulled += 1
        elif "under 70" in str(r.get("reason") or ""):
            under += 1
        else:
            other += 1
    for k in a_el - b_el:
        if k in b_all:
            rose += 1
        else:
            new += 1
    parts = []
    if pulled:
        parts.append("\U0001F6AB %d pulled by PrizePicks" % pulled)
    if started:
        parts.append("⏱️ %d match%s started" % (started, "" if started == 1 else "es"))
    if under:
        parts.append("⬇️ %d fell under 70%%" % under)
    if other:
        parts.append("➖ %d dropped (rule/hold)" % other)
    parts.append("\U0001F195 %d new" % new)
    if rose:
        parts.append("⬆️ %d rose to 70%%+" % rose)
    return {"before": len(b_el), "after": len(a_el), "parts": parts}


# ------------------------------------------------------------------ status log captures
def _capture_boards(rows, cap):
    import apex_odds_watch as W
    on, _bk, _n = W._capture(rows, cap)
    return {f: [spot(r) for r in on.get(f) or []] for f in LIMIT}   # a capture covers every board


def from_captures(cap_before, cap_after, rows=None):
    """Status for the publish at cap_after vs the one before it (cap_before)."""
    try:
        import apex_odds_watch as W
        import apex_edge_tracker as T
        rows = rows if rows is not None else W._status_rows()
        if not cap_after:
            caps = sorted({r["captured_at"] for r in rows})
            if not caps:
                return None
            cap_after = caps[-1]
        after = _capture_boards(rows, cap_after)
        if not cap_before or cap_before == cap_after:
            # no new publish: the board is the one already posted
            same = cap_before == cap_after
            return compare(after, after, when=None if same else cap_after, first=not same)
        before = _capture_boards(rows, cap_before)
        _o, a_bk, _n = W._capture(rows, cap_after)
        backups = {}
        gone = {f: {_key(s) for s in before.get(f) or []} - {_key(s) for s in after.get(f) or []}
                for f in before}
        newest = max({r["captured_at"] for r in rows}) == cap_after
        for f, ks in gone.items():
            if not ks:
                continue
            nums = {}
            if newest and any(k in (a_bk.get(f) or set()) for k in ks):
                with contextlib.redirect_stdout(io.StringIO()):   # the board read prints its retirements
                    nums = {k: b.get("no") for k, b in T.backup_numbers(f).items()}
            backups[f] = {k: ("Backup %s" % nums[k]) if nums.get(k) else "Backups"
                          for k in ks if k in (a_bk.get(f) or set())}
        st = compare(before, after, backups=backups, when=cap_after)
        _fill_whys(st, cap_after)
        _fill_now_probs(st, rows, cap_after)
        _fill_scores(st, cap_after)   # APEX-SCOREDISPLAY-2026-10-25
        try:
            st["allprops"] = _allprops_change(rows, cap_before, cap_after)
        except Exception:
            st["allprops"] = None
        return st
    except Exception as e:
        print("[BoardStatus] capture compare skipped (%s: %s)" % (type(e).__name__, e))
        return None


def _fill_scores(st, cap_after):
    """APEX-SCOREDISPLAY-2026-10-25: every in / out item gets the board Score board history recorded for
    it on the NEW board (rank_score: the number that board ranked it by). Never raises."""
    try:
        import apex_edge_tracker as T
        if not T.SCORE_DISPLAY:
            return
        flags = list(st["main"]) + list(st["other"])
        cur, _prev, _src = _hist_rows(cap_after, flags)
        for grp in ("main", "other"):
            for flag, ent in st[grp].items():
                for it in ent.get("items") or []:
                    if it["kind"] in ("in", "out"):
                        row = (cur.get(flag) or {}).get(it["s"].get("pkey"))
                        if row and row.get("rank_score") is not None:
                            it["score"] = _f(row["rank_score"])
    except Exception as e:
        print("[BoardStatus] scores skipped (%s: %s)" % (type(e).__name__, e))


def _fill_now_probs(st, rows, cap_after):
    """APEX-SWAPLINES-2026-10-25: a pick that left shows its Win Prob on the NEW board (the one
    that got it replaced), from the same capture. Never raises."""
    try:
        import apex_edge_tracker as T
        by = {}
        for r in rows:
            if r["captured_at"] == cap_after and _f(r.get("prob")) is not None:
                by.setdefault((r.get("board"), tuple(T._apex_prop_identity(r))), _f(r.get("prob")))
        for grp in ("main", "other"):
            for flag, ent in st[grp].items():
                for it in ent.get("items") or []:
                    if it["kind"] == "out":
                        k = _key(it["s"])
                        p = by.get((flag, k), by.get(("all", k)))
                        if p is not None:
                            it["now_prob"] = p
    except Exception as e:
        print("[BoardStatus] now probs skipped (%s: %s)" % (type(e).__name__, e))


def latest():
    """Status for the newest publish vs the one before it."""
    try:
        import apex_odds_watch as W
        rows = W._status_rows()
        caps = sorted({r["captured_at"] for r in rows})
        if not caps:
            return None
        return from_captures(caps[-2] if len(caps) > 1 else None, caps[-1], rows=rows)
    except Exception as e:
        print("[BoardStatus] latest skipped (%s: %s)" % (type(e).__name__, e))
        return None


def latest_capture():
    try:
        import apex_odds_watch as W
        return max({r["captured_at"] for r in W._status_rows()})
    except Exception:
        return None


# ------------------------------------------------------------------ rendering
def title(st):
    m = st["main"]
    present = [f for f in ("top3", "elite6") if f in m]
    if not present:   # an update about one other board only (a pulled-line fill on Best Goblin)
        ch = [NAME[f].upper() for f, _a, _b, _n in OTHER if (st["other"].get(f) or {}).get("changed")]
        t = ("\U0001F501 %s CHANGED" % " & ".join(ch)) if ch else "✅ %s UNCHANGED" % " & ".join(
            NAME[f].upper() for f in st["other"])
    elif st.get("first"):
        t = "\U0001F4CB %s POSTED" % " & ".join(NAME[f].upper() for f in present)
    else:
        ch = [f for f in present if m[f]["changed"]]
        same = [f for f in present if not m[f]["changed"]]
        if not ch:
            t = "✅ %s UNCHANGED" % " & ".join(NAME[f].upper() for f in same)
        else:
            t = "\U0001F501 " + " · ".join("%s CHANGED" % NAME[f].upper() for f in ch)
            if same:
                t += " · %s UNCHANGED" % " & ".join(NAME[f].upper() for f in same)
            _why = change_reason(st, ch)   # APEX-CHANGEWHY-HEADER-2026-10-27
            if _why:
                t += " -- " + _why
    empties = ["%s #%s" % (NAME[f].upper(), ",".join(str(i) for i, _w in m[f]["empty"]))
               for f in present if m[f]["empty"]]
    if empties:
        t += " · ⚠️ %s EMPTY" % " & ".join(empties)
    return t[:256]


def color(st):
    import apex_alert_cards as C
    if not st:
        return C.BLUE
    ents = list(st["main"].values()) or list(st["other"].values())
    if any(e["empty"] for e in ents):
        return C.RED
    if any(e["changed"] for e in ents):
        return C.ORANGE
    return C.GREEN


def _n_changes(st):
    m = st["main"]
    return sum(len(e.get("items") or []) for e in (m.values() if m else st["other"].values()))


def updated_line(st):
    if st.get("first"):
        return "Updated %s · first board posted, nothing to compare" % st["when"]
    n = _n_changes(st)
    if not n:
        return "Updated %s · same picks, same order" % st["when"]
    return "Updated %s · %d change%s" % (st["when"], n, "" if n == 1 else "s")


def _other_board_line(f, e):
    full = NAME[f]
    em = OTHER_EMOJI.get(f, "•")
    n_empty = len(e["empty"])
    if not e["changed"] and not n_empty:
        return "%s **%s** — ✅ unchanged" % (em, full)
    if not e["changed"]:
        return "%s **%s** — ⚠️ %s" % (em, full, "empty" if n_empty >= e["limit"] else
                                      "%d spot%s empty" % (n_empty, "" if n_empty == 1 else "s"))
    its = e.get("items") or []
    ins = ["%s %s" % (_short_pick(it["s"]), it["to"]) for it in its if it["kind"] == "in"]
    outs = ["%s → %s" % (_short_pick(it["s"]), it["to"]) for it in its if it["kind"] in ("out", "pulled", "started")]
    moved = sum(1 for it in its if it["kind"] in ("up", "down"))
    parts = []
    if ins:
        parts.append("➕ IN: " + ", ".join(ins[:2]) + (" +%d" % (len(ins) - 2) if len(ins) > 2 else ""))
    if outs:
        parts.append("⬇️ OUT: " + ", ".join(outs[:2]) + (" +%d" % (len(outs) - 2) if len(outs) > 2 else ""))
    if moved:
        parts.append("↕️ %d moved" % moved)
    if n_empty:
        parts.append("⚠️ %d empty" % n_empty)
    return "%s **%s** — \U0001F501 changed · %s" % (em, full, " · ".join(parts))


def other_lines(st):
    out = [_other_board_line(f, st["other"][f]) for f, _a, _b, _n in OTHER if f in st["other"]]
    ap = st.get("allprops")
    if ap:
        out.append("\U0001F4DA **All Props** — %d → %d at 70%%+ · %s" % (ap["before"], ap["after"], " · ".join(ap["parts"])))
    return out


def fields(st):
    """Card sections: Top 3 and Elite 6 first (moves + why + new lineup), then the other boards."""
    out = []
    m = st["main"]
    for f, up, _nm, badge, _l in MAIN:
        e = m.get(f)
        if e is None:
            continue
        tag = "\U0001F4CB POSTED" if st.get("first") else ("\U0001F501 CHANGED" if e["changed"] else "✅ UNCHANGED")
        if e["empty"]:
            tag += " · ⚠️ EMPTY SPOT"
        val = item_lines(e) if e["changed"] else []
        val += ["⚠️ **#%d EMPTY** — %s" % (i, why) for i, why in e["empty"]]
        val.append(lineup(f, e, compact=not e["changed"] and f == "elite6"))
        out.append(("%s %s — %s" % (badge, up, tag), val))
    if not m:   # one other board only: its own moves in full
        for f, nm, _s, _l in OTHER:
            e = st["other"].get(f)
            if e is None:
                continue
            tag = "\U0001F501 CHANGED" if e["changed"] else "✅ UNCHANGED"
            val = item_lines(e) + ["⚠️ **#%d EMPTY** — %s" % (i, why) for i, why in e["empty"]]
            val.append(lineup(f, e))
            out.append(("%s %s — %s" % (OTHER_EMOJI.get(f, ""), nm.upper(), tag), val))
        return out
    ol = other_lines(st)
    if ol:
        out.append(("\U0001F4CA Other boards", ol))
    return out


def body_lines(st):
    """Plain-text lines under the title (the #picks card and #system notes): the small 'Updated'
    line, one line per moved pick, the EMPTY warnings, then the other boards."""
    m = st["main"]
    out = [updated_line(st)]
    if _n_changes(st) and not st.get("first"):
        for f, _u, nm, badge, _l in MAIN:
            e = m.get(f) or {}
            if not e.get("items"):
                continue
            out.append("%s **%s**" % (badge, nm))
            out += item_lines(e, why=False)
    for f, _u, nm, _b, _l in MAIN:
        for i, why in (m.get(f) or {}).get("empty") or []:
            out.append("⚠️ **%s #%d EMPTY** — %s" % (nm, i, why))
    if not m:   # one other board only: its own change lines
        for f, nm, _s, _l in OTHER:
            e = st["other"].get(f) or {}
            out += item_lines(e, why=False) if e.get("items") else []
            out += ["⚠️ **%s #%d EMPTY** — %s" % (nm, i, why) for i, why in e.get("empty") or []]
        return out
    ol = other_lines(st)
    if ol:
        out.append("Other boards:")
        out += ol
    return out


def embed_top(st):
    """(embed title, description top) -- the #picks plays card puts its own text under it."""
    return title(st), "\n".join(body_lines(st))


def text_block(st):
    """Plain-message version: a big '##' heading, the small line as '-#' subtext."""
    if SWAPS_ONLY:
        return "\n".join(["## " + swap_title(st)] + swap_body(st))
    ls = body_lines(st)
    return "\n".join(["## " + title(st), "-# " + ls[0]] + ls[1:])


def wrap_text(st, text, limit=1990):
    """'<big status>\\n\\n<the old message>' -- the old message is unchanged under it."""
    if not st or not HEADER_ON or (SWAPS_ONLY and not big_change(st)):   # APEX-MINICHANGE-2026-10-27
        return text
    head = text_block(st)
    out = head + "\n\n" + text
    return out if len(out) <= limit else (head[:limit - len(text) - 2] + "\n\n" + text)[:limit]


def card(st, event, event_lines=(), extra=(), event_color=None):
    """The #alerts card for a board update: big status title, the event ('📣 Odds released ...')
    and the 'Updated' line on top, Top 3 / Elite 6 sections, other boards, then extra sections
    [(label, lines)]. Without a status (or HEADER_ON off) the event itself is the title."""
    import apex_alert_cards as C
    ev = [x for x in event_lines if x]
    if not st or not HEADER_ON or (SWAPS_ONLY and not big_change(st)):   # APEX-MINICHANGE-2026-10-27
        return C.make(event, event_color or C.BLUE, "\n".join(ev), list(extra))
    if SWAPS_ONLY:
        desc = "**%s**" % event + ("\n" + "\n".join(ev) if ev else "") + "\n\n" + "\n".join(swap_body(st))
        return C.make(swap_title(st), swap_color(st), desc, list(extra))
    desc = "**%s**" % event + ("\n" + "\n".join(ev) if ev else "") + "\n-# " + updated_line(st)
    return C.make(title(st), color(st), desc, fields(st) + list(extra))


def post_alert(st, event, event_lines=(), extra=(), event_color=None, check_mute=False):
    """Builds card(...) and posts it to #alerts. Returns True/False."""
    import apex_alert_cards as C
    return C.post(card(st, event, event_lines, extra, event_color), "alerts", check_mute=check_mute)


def wrap_embed(st, embed, desc_limit=4096):
    """An existing card with the status on top: new title = the big status; the old title (bold)
    and description go in the description, the status sections come before the old fields."""
    if not st or not HEADER_ON or (SWAPS_ONLY and not big_change(st)):   # APEX-MINICHANGE-2026-10-27
        return embed
    import apex_alert_cards as C
    e = dict(embed)
    old_t, old_d = embed.get("title") or "", embed.get("description") or ""
    if SWAPS_ONLY:
        e["title"] = swap_title(st)
        e["color"] = swap_color(st)
        e["description"] = ("**%s**" % old_t + ("\n" + old_d if old_d else "") + "\n\n"
                            + "\n".join(swap_body(st)))[:desc_limit]
        return e
    e["title"] = title(st)
    e["color"] = color(st)
    e["description"] = ("**%s**" % old_t + ("\n" + old_d if old_d else "") + "\n-# " + updated_line(st))[:desc_limit]
    sec = C.make("x", 0, "", fields(st)).get("fields") or []
    e["fields"] = (sec + list(embed.get("fields") or []))[:25]
    return e


if __name__ == "__main__":   # python3 apex_board_status.py [cap_before cap_after]  -> prints the card
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import apex_alert_cards as C
    s = from_captures(sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else latest()
    print(C.to_text(card(s, "Board update")) if s else "no status")
