#!/usr/bin/env python3
"""
APEX-ALERTCHAIN-2026-10-25 (owner, go 10/25 with the 8:11 + 8:13 PM #alerts screenshots: "I got
this message, 68 new PrizePicks, but it should tell me what's about to happen, since the next
message doesn't come in until 3 mins later. Same for any alert that has delays and another message
that connects to it.")

An alert that starts a chain is ONE #alerts card that edits itself in place:

  start(key, embed, nxt, wait_min)   posts the card with a "⏭️ What happens next" section
                                     (what comes next and about when) and remembers its message id
  step(key, now, nxt, wait_min)      edits it: "⏳ Now" (e.g. scoring 40 of 73) + what is next
  finish(key, embed)                 edits it into the final card (✅ / 🔁 / fixed / back);
                                     no open card -> posts the final card as a new message
  fail(key, why)                     edits it red: what went wrong
  check_stalls()                     any open card past its deadline (expected time + 10 min)
                                     turns red "STALLED" (the scheduler runs it every 2 min)

The message ids live in data/logs/alert_chains.json (file-locked), so the bot, the scheduler and the
launchd jobs share one card per chain. Edits go straight to the webhook, so they still land while a
/fetch or the auto-eval mutes new #alerts posts. Display only: nothing here changes a pick, a board
or a rule.

scope(key) marks "a chain card is open for this run" in this process: a result card built deeper
down (the odds watch's NEW PROPS SCORED / ODDS RELEASED card, the 📅 moved-match line) is handed to
the chain (absorb_card / absorb_line) instead of posting a second message.

Undo: CHAINS_ON = False (every alert posts as its own message again, as before).
"""
import fcntl, json, os, threading, time
from contextlib import contextmanager
from datetime import datetime, timedelta

CHAINS_ON = True
STALL_MIN = 10            # a step this late past its expected time turns the card red
KEEP_H = 30               # closed chains are forgotten after this long
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "data", "logs", "alert_chains.json")
NEXT_FIELD = "⏭️ What happens next"
NOW_FIELD = "⏳ Now"
_TL = threading.local()


def _C():
    import apex_alert_cards as C
    return C


def _now():
    return datetime.now()


def _iso(t=None):
    return (t or _now()).isoformat(timespec="seconds")


def _ts(s):
    try:
        return datetime.fromisoformat(str(s))
    except Exception:
        return None


def hm(t=None):
    """'8:15 PM' (Central -- the Mac runs on CT)."""
    return (t or _now()).strftime("%I:%M %p").lstrip("0")


# ------------------------------------------------------------------ state (shared across processes)
@contextmanager
def _locked():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            with open(STATE) as fh:
                st = json.load(fh)
        except Exception:
            st = {}
        box = {"st": st, "dirty": False}
        yield box
        if box["dirty"]:
            cut = _iso(_now() - timedelta(hours=KEEP_H))
            box["st"] = {k: v for k, v in box["st"].items()
                         if k.startswith("_") or (v.get("updated") or "") >= cut}
            with open(STATE + ".tmp", "w") as fh:
                json.dump(box["st"], fh, indent=1, default=str)
            os.replace(STATE + ".tmp", STATE)


def get(key, open_only=True, max_age_min=None):
    """The chain record for key (None if none / closed / older than max_age_min)."""
    with _locked() as b:
        c = b["st"].get(key)
    if not c or key.startswith("_"):
        return None
    if open_only and c.get("done"):
        return None
    if max_age_min is not None:
        t = _ts(c.get("started"))
        if not t or (_now() - t).total_seconds() > max_age_min * 60:
            return None
    return c


def meta(name, value=None):
    """Small shared notes (e.g. when the board check last ran). meta(name) reads, meta(name, v) writes."""
    with _locked() as b:
        m = b["st"].setdefault("_meta", {})
        if value is None:
            return m.get(name)
        m[name] = value
        b["dirty"] = True
        return value


def _save(key, rec):
    with _locked() as b:
        b["st"][key] = rec
        b["dirty"] = True


# ------------------------------------------------------------------ building the card
def _strip(embed):
    return {k: v for k, v in (embed or {}).items() if not k.startswith("_")}


def _with_steps(base, now=None, nxt=None, foot_note=None):
    """base card + '⏳ Now' + '⏭️ What happens next' (always the last two sections)."""
    C = _C()
    e = dict(_strip(base))
    fl = [f for f in (e.get("fields") or []) if f.get("name") not in (NOW_FIELD, NEXT_FIELD)]
    if now:
        fl.append({"name": NOW_FIELD, "value": str(now)[:1024], "inline": False})
    if nxt:
        fl.append({"name": NEXT_FIELD, "value": str(nxt)[:1024], "inline": False})
    e["fields"] = fl[-25:]
    if foot_note:
        e["footer"] = {"text": ("Apex · %s CT · %s" % (C.clock(), foot_note))[:2048]}
    return e


def eta(wait_min):
    """'about 8:15 PM (3 min)'."""
    if wait_min is None:
        return ""
    w = max(1, int(round(wait_min)))
    return "about %s (%d min)" % (hm(_now() + timedelta(minutes=w)), w)


def _deadline(wait_min):
    return _iso(_now() + timedelta(minutes=(wait_min or 0) + STALL_MIN))


# ------------------------------------------------------------------ posting / editing
def _post(embed, channel, check_mute):
    C = _C()
    if C.DRY:
        ok, mid = C.post(embed, channel, wait=True)
        return ok, mid or "dry-%d" % len(C.PREVIEW)
    if check_mute and C.suppressed(channel):
        return True, None
    return C.post(embed, channel, wait=True)


def _edit(rec, embed):
    C = _C()
    if not rec.get("mid"):
        return False
    return C.edit(rec["mid"], embed, rec.get("channel") or "alerts")


def start(key, embed, nxt, wait_min=None, now=None, channel="alerts", check_mute=True, data=None):
    """Posts the first card of a chain. nxt = what comes next (plain words); wait_min = about how
    long until it happens (adds 'about 8:15 PM (3 min)' and sets the stall deadline).
    Returns True when posted (or muted on purpose)."""
    nxt_txt = nxt + ((" — " + eta(wait_min)) if wait_min is not None and "about " not in nxt else "")
    e = _with_steps(embed, now, nxt_txt, "updates in place")
    if not CHAINS_ON:
        return _C().post(e, channel, check_mute=check_mute)
    ok, mid = _post(e, channel, check_mute)
    _save(key, {"mid": mid, "channel": channel, "started": _iso(), "updated": _iso(),
                "deadline": _deadline(wait_min), "base": _strip(embed), "now": now, "next": nxt_txt,
                "done": False, "stalled": False, "data": data or {}})
    return ok


def update_data(key, **kw):
    with _locked() as b:
        c = b["st"].get(key)
        if c:
            c.setdefault("data", {}).update(kw)
            b["dirty"] = True
            return c
    return None


def step(key, now, nxt=None, wait_min=None, base=None, data=None):
    """Edits the open card: new '⏳ Now' line, new 'what happens next' (and a new base card when
    the counts changed). Returns False when no card is open for key."""
    with _locked() as b:
        c = b["st"].get(key)
        if not c or c.get("done"):
            return False
        if base is not None:
            c["base"] = _strip(base)
        c["now"] = now
        if nxt is not None:
            c["next"] = nxt + ((" — " + eta(wait_min)) if wait_min is not None and "about " not in nxt else "")
        if wait_min is not None:   # a count-only update keeps the card's deadline
            c["deadline"] = _deadline(wait_min)
            c["stalled"] = False
        c["updated"] = _iso()
        if data:
            c.setdefault("data", {}).update(data)
        b["dirty"] = True
        rec = dict(c)
    if CHAINS_ON:
        _edit(rec, _with_steps(rec["base"], rec.get("now"), rec.get("next"), "updates in place"))
    return True


def _final_foot(rec, embed, note=None):
    C = _C()
    t0 = _ts(rec.get("started")) if rec else None
    txt = ((embed.get("footer") or {}).get("text") or "Apex · %s CT" % C.clock())
    if t0:
        txt += " · first posted %s" % hm(t0)
    if note:
        txt += " · " + note
    return {"text": txt[:2048]}


def finish(key, embed, channel="alerts", check_mute=False, note=None):
    """The last step: edits the open card into `embed` (no 'next' section). With no open card the
    final card posts as a new message. Returns True/False."""
    c = get(key)
    e = _strip(embed)
    if not CHAINS_ON or not c or not c.get("mid"):
        if c:
            _close(key)
        if c and not c.get("mid") and _C().suppressed(channel):
            return True
        return _C().post(e, channel, check_mute=check_mute)
    e["fields"] = [f for f in (e.get("fields") or []) if f.get("name") not in (NOW_FIELD, NEXT_FIELD)]
    e["footer"] = _final_foot(c, e, note)
    ok = _edit(c, e)
    if not ok:   # the old message is gone (deleted, webhook changed): post the result instead
        ok = _C().post(e, channel)
    _close(key, e)
    return ok


def _close(key, embed=None):
    with _locked() as b:
        c = b["st"].get(key)
        if c:
            c["done"] = True
            c["updated"] = c["done_at"] = _iso()
            if embed is not None:
                c["final"] = embed
            b["dirty"] = True


def fail(key, why, title=None):
    """Edits the open card red with what went wrong (a step that failed)."""
    c = get(key)
    if not c:
        return False
    C = _C()
    base = dict(c["base"])
    base["title"] = (title or ("\U0001F6A8 " + _bare(base.get("title")) + " — FAILED"))[:256]
    base["color"] = C.RED
    e = _with_steps(base, "\U0001F6A8 " + str(why), None)
    e["footer"] = _final_foot(c, e)
    ok = _edit(c, e) if CHAINS_ON else C.post(e, c.get("channel") or "alerts")
    _close(key, e)
    return ok


def _bare(t):
    t = str(t or "")
    return t.split(" ", 1)[1] if t and not t[0].isalnum() and " " in t else t


def check_stalls():
    """Open cards past their deadline turn red once ('STALLED: no update for N min'). Returns how
    many were marked. The card keeps its message id, so a late finish still turns it green."""
    if not CHAINS_ON:
        return 0
    C = _C()
    now = _now()
    hit = []
    with _locked() as b:
        for k, c in b["st"].items():
            if k.startswith("_") or c.get("done") or c.get("stalled"):
                continue
            d = _ts(c.get("deadline"))
            if d and now > d:
                c["stalled"] = True
                b["dirty"] = True
                hit.append((k, dict(c)))
    for k, c in hit:
        idle = int((now - (_ts(c.get("updated")) or now)).total_seconds() // 60)
        base = dict(c["base"])
        base["title"] = ("⚠️ STALLED — " + _bare(base.get("title")))[:256]
        base["color"] = C.RED
        exp = _ts(c.get("deadline"))
        exp = exp - timedelta(minutes=STALL_MIN) if exp else None
        e = _with_steps(base, "\U0001F6A8 No update for %d min — the next step was due by %s. Last step: %s"
                        % (idle, hm(exp) if exp else "now", c.get("now") or "posted"),
                        "Still waiting on: %s\nThis card turns green by itself if it finishes." % (c.get("next") or "—"),
                        "stalled")
        _edit(c, e)
    return len(hit)


# ------------------------------------------------------------------ one run's scope (this process)
@contextmanager
def scope(key=None):
    """While a chain card is open for this run, result cards built deeper down go to it."""
    prev = getattr(_TL, "scope", None)
    _TL.scope = {"key": key, "cards": [], "lines": []}
    try:
        yield _TL.scope
    finally:
        _TL.scope = prev


def active():
    return getattr(_TL, "scope", None)


def absorb_card(embed):
    """True = the open chain took this card (the caller does not post it)."""
    s = active()
    if not s or not s.get("key") or not CHAINS_ON:
        return False
    s["cards"].append(embed)
    return True


def absorb_line(text):
    s = active()
    if not s or not CHAINS_ON:
        return False
    s["lines"].append(str(text))
    return True


def progress(done, total, every_s=45):
    """Called per scored prop inside a scope: edits '⏳ Now: scoring 40 of 73' at most every 45 s."""
    s = active()
    if not s or not s.get("key") or not CHAINS_ON:
        return
    t = time.time()
    if t - s.get("_last_prog", 0) < every_s and done < total:
        return
    s["_last_prog"] = t
    left = max(0, total - done)
    step(s["key"], "Scoring props: **%d of %d** done (started %s)" % (done, total, s.get("t0") or hm()),
         "Results when scoring ends — about %s" % hm(_now() + timedelta(seconds=max(30, 2.5 * left))),
         wait_min=max(1, 2.5 * left / 60.0))


def soft_start(key, embed, nxt, channel="alerts", check_mute=True):
    """A card whose next step may never come (a spot that may refill, a re-price that may get odds-move
    lines): it says what would happen next but never turns red for waiting."""
    ok = start(key, embed, nxt, None, channel=channel, check_mute=check_mute)
    with _locked() as b:
        c = b["st"].get(key)
        if c:
            c["deadline"] = None
            b["dirty"] = True
    return ok


def replace(key, embed, nxt=None, now=None):
    """Swaps the open card's content (same message). False when no card is open."""
    c = get(key)
    if not c:
        return False
    return step(key, now, nxt if nxt is not None else c.get("next"), None, base=embed) and _soften(key, c)


def _soften(key, old):
    if old.get("deadline") is None:
        with _locked() as b:
            if b["st"].get(key):
                b["st"][key]["deadline"] = None
                b["dirty"] = True
    return True


def add_section(key, name, lines, max_age_min=None):
    """Adds one section to an open (or just-finished) card, e.g. 📉 odds-move lines on the re-price
    card. Returns False when there is no card to add to (the caller posts its own)."""
    with _locked() as b:
        c = b["st"].get(key)
        if not c or not c.get("mid"):
            return False
        t = _ts(c.get("started"))
        if max_age_min is not None and (not t or (_now() - t).total_seconds() > max_age_min * 60):
            return False
        tgt = "final" if c.get("done") and c.get("final") else "base"
        e = dict(c.get(tgt) or c["base"])
        e["fields"] = list(e.get("fields") or []) + [{"name": name[:256], "value": "\n".join(lines)[:1024],
                                                      "inline": False}]
        c[tgt] = e
        c["updated"] = _iso()
        b["dirty"] = True
        rec = dict(c)
    if not CHAINS_ON:
        return False
    if rec.get("done"):
        return _edit(rec, rec["final"])
    return _edit(rec, _with_steps(rec["base"], rec.get("now"), rec.get("next"), "updates in place"))


# ------------------------------------------------------------------ the new PrizePicks props card
# Two jobs see new props: the 15-min PrizePicks check (apex_closing_line) and the board watch
# (openclaw_core._drop_alert, which also scores them). Both feed this one card: one count (what it
# counts says so), one entry per match whatever order the two names come in.
NEWPROPS_KEY = "newprops"
NEWPROPS_MERGE_MIN = 45    # a second sighting this soon joins the same card
SCORE_MIN = 3              # about how long scoring a batch takes (8:13 -> 8:16 PM on 10/25)
BOARD_CHECK_MIN = 30


def np_clean(s):
    import re
    return re.sub(r"\s+(?:1st|2nd|3rd|first|second|third)\s+set\s*$", "", str(s or ""), flags=re.I).strip()


def np_key(player, opponent, slug, line, ptype):
    """Same prop key the 15-min check keeps (pp_board_seen.json): player|opponent|stat|line|type."""
    try:
        ln = "%g" % float(line)
    except (TypeError, ValueError):
        ln = str(line)
    return "|".join([str(player or "").strip().lower(), np_clean(opponent).lower(), str(slug or ""), ln,
                     str(ptype or "standard").lower()])


_PARTICLES = {"de", "van", "von", "del", "da", "di", "der", "dos", "du", "la", "le", "den"}


def _surname(n):
    """'Etcheverry' (Tomas Martin Etcheverry), 'de Minaur', 'Van Assche'."""
    w = str(n or "").split()
    if not w:
        return "?"
    out = [w[-1]]
    for x in reversed(w[1:-1]):
        if x.lower() not in _PARTICLES:
            break
        out.insert(0, x)
    return " ".join(out)


def np_pair(player, opponent):
    """('casper ruud|dominic stricker', 'Ruud v Stricker') -- one entry per match."""
    a, b = str(player or "").strip(), np_clean(opponent)
    sur = _surname
    names = sorted([a, b], key=lambda x: sur(x).lower())
    return "|".join(sorted(x.lower() for x in names)), "%s v %s" % (sur(names[0]), sur(names[1]))


def np_day(start):
    """The Central-time day a match starts on ('2026-10-26'), from any ISO start time."""
    try:
        from zoneinfo import ZoneInfo
        t = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        if t.tzinfo is None:
            return t.date().isoformat()
        return t.astimezone(ZoneInfo("America/Chicago")).date().isoformat()
    except Exception:
        return str(start or "")[:10]


def _day(d):
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").strftime("%a %-m/%-d")
    except Exception:
        return str(d or "")


def np_card(data):
    """The new-props card (posted / scoring stages): one count, one line per match."""
    C = _C()
    keys = set(data.get("keys") or [])
    rel = set(data.get("relisted") or []) & keys
    n, nr = len(keys), len(rel)
    m = data.get("matches") or {}
    days = sorted(set(data.get("days") or []))
    dl = ", ".join(_day(d) for d in days)
    if nr:
        count = "**%d props** (%d new, %d re-listed — same line back with a new start time)" % (n, n - nr, nr)
    else:
        count = "**%d new prop%s**" % (n, "" if n == 1 else "s")
    desc = "%s across **%d match%s**" % (count, len(m), "" if len(m) == 1 else "es")
    if data.get("first_seen"):
        desc += "\nOn PrizePicks by %s" % data["first_seen"]
    rows = sorted(m.values(), key=lambda v: (-len(v.get("keys") or []), v.get("label") or ""))
    ml = ["• %s · %d prop%s" % (v["label"], len(v["keys"]), "" if len(v["keys"]) == 1 else "s") for v in rows[:12]]
    if len(rows) > 12:
        ml.append("+%d more match%s" % (len(rows) - 12, "" if len(rows) - 12 == 1 else "es"))
    return C.make("\U0001F195 NEW PRIZEPICKS PROPS" + (" · " + dl if dl else ""), C.BLUE, desc,
                  [("\U0001F4E5 Matches", ml)])


def np_merge(items, relisted=()):
    """items = [(key, pair, label, day)]. Merges them into the open new-props card's counts (or a
    fresh set). Returns (open chain record or None, merged data)."""
    c = get(NEWPROPS_KEY, max_age_min=NEWPROPS_MERGE_MIN)
    data = dict((c or {}).get("data") or {})
    keys = set(data.get("keys") or [])
    rel = set(data.get("relisted") or [])
    m = {k: dict(v) for k, v in (data.get("matches") or {}).items()}
    days = set(data.get("days") or [])
    for k, pair, label, day in items:
        keys.add(k)
        e = m.setdefault(pair, {"label": label, "keys": []})
        if k not in e["keys"]:
            e["keys"].append(k)
        if day:
            days.add(str(day)[:10])
    rel |= set(relisted)
    data.update(keys=sorted(keys), relisted=sorted(rel & keys), matches=m, days=sorted(days))
    data.setdefault("first_seen", hm())
    return c, data


def next_board_check():
    """(about when the next board check scores new props, minutes from now)."""
    bc = meta("board_check") or {}
    t = _ts(bc.get("cycle_at"))
    nxt = (t + timedelta(minutes=BOARD_CHECK_MIN)) if t else None
    now = _now()
    if not nxt or nxt < now + timedelta(minutes=1):
        nxt = now + timedelta(minutes=2)
    return nxt, (nxt - now).total_seconds() / 60.0


def np_posted(items, relisted=(), nxt=None, now=None, wait_min=None):
    """The 15-min PrizePicks check saw new props: start the card (or add them to the open one).
    APEX-LUMPSCORE-2026-10-30: nxt/now/wait_min override the wording ("{at}" in nxt = the next board check)."""
    c, data = np_merge(items, relisted)
    if c:
        return step(NEWPROPS_KEY, c.get("now"), c.get("next"), base=np_card(data), data=data) or True
    at, wait = next_board_check()
    nxt = (nxt or "Apex scores them at the next board check, about {at} (scoring takes about %d min). This card "
                  "then updates with any that make Top 3 / Elite 6 or another board." % SCORE_MIN).replace("{at}", hm(at))
    return start(NEWPROPS_KEY, np_card(data), nxt, wait_min=(wait + SCORE_MIN) if wait_min is None else wait_min,
                 now=now or "Posted on PrizePicks — not scored yet", data=data)


def np_scoring(items, relisted=(), n_score=None):
    """The board watch is about to score them: start or update the card to 'scoring now'."""
    c, data = np_merge(items, relisted)
    data["scoring_at"] = hm()
    n = n_score or len(data["keys"])
    now = "Scoring **%d** prop%s now (started %s)" % (n, "" if n == 1 else "s", hm())
    nxt = "Results in about %d min — this card shows any that make Top 3 / Elite 6 or another board." % SCORE_MIN
    if c:
        return step(NEWPROPS_KEY, now, nxt, wait_min=SCORE_MIN, base=np_card(data), data=data)
    return start(NEWPROPS_KEY, np_card(data), nxt, wait_min=SCORE_MIN, now=now, data=data)


if __name__ == "__main__":   # python3 apex_alert_chain.py stalls  -> marks stalled cards
    import sys
    if sys.argv[1:] == ["stalls"]:
        print("stalled:", check_stalls())
    else:
        with _locked() as b:
            print(json.dumps({k: {x: v.get(x) for x in ("started", "updated", "done", "stalled", "now", "next")}
                              for k, v in b["st"].items() if not k.startswith("_")}, indent=1))
