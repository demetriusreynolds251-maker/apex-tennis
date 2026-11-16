#!/usr/bin/env python3
"""
APEX-ALERTCARDS-2026-10-25 (owner, go 10/25 after the 5:44 PM #alerts: "It needs to be more
clear on what moved, it looks confusing, add some emojis. Order check needs to be its own alert ...
Matter of fact just change all the alerts to look better and bigger and with correct information.")

Every #alerts message is a Discord card (embed):
  bold title with an emoji
  colored side bar   green = unchanged / good · orange = changed · red = problem, empty spot,
                     order check · blue = info
  short sections with bold labels, one item per line
  footer             "Apex · 5:44 PM CT"

  make(title, color, desc, fields)  -> embed dict (Discord limits kept)
  post(embed)                       -> posts it to #alerts (plain text if the card post fails)
  from_text(text)                   -> a card from an old plain-text alert: the first line becomes
                                       the title, run-on "a · b · c" lines become one item per line,
                                       the color comes from the emoji/words
openclaw_core.send_discord / apex_ops.send_discord / send_discord_tracked turn every plain #alerts
message into a card through from_text, so no alert is left as a plain line. Display only: nothing
here changes a pick, a board or a rule.
Undo: ALERT_CARDS_ON = False (every alert posts as plain text again).
"""
import os, re, sys, time
from datetime import datetime

ALERT_CARDS_ON = True
DRY = False          # True = print / keep in PREVIEW instead of posting (tests)
PREVIEW = []         # cards built while DRY

GREEN, ORANGE, RED, BLUE = 0x2ECC71, 0xE67E22, 0xE74C3C, 0x3498DB
COLOR_EMOJI = {GREEN: "✅", ORANGE: "\U0001F501", RED: "\U0001F6A8", BLUE: "ℹ️"}
MAX_DESC, MAX_FIELD, MAX_TOTAL = 4096, 1024, 5800


def clock(ts=None):
    """'5:44 PM' in Central time."""
    try:
        from zoneinfo import ZoneInfo
        if isinstance(ts, str):
            t = datetime.fromisoformat(ts)
            t = t if t.tzinfo else t.replace(tzinfo=ZoneInfo("America/Chicago"))
        else:
            t = ts or datetime.now(ZoneInfo("America/Chicago"))
        return t.astimezone(ZoneInfo("America/Chicago")).strftime("%I:%M %p").lstrip("0")
    except Exception:
        return datetime.now().strftime("%I:%M %p").lstrip("0")


def footer(extra=None, ts=None):
    return {"text": "Apex · %s CT" % clock(ts) + (" · %s" % extra if extra else "")}


def _lines(v):
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else str(v).split("\n")


def _split_value(lines):
    """Whole lines into chunks of at most 1,024 characters."""
    out, cur = [], ""
    for ln in lines:
        ln = ln[:MAX_FIELD]
        if cur and len(cur) + 1 + len(ln) > MAX_FIELD:
            out.append(cur)
            cur = ln
        else:
            cur = (cur + "\n" + ln) if cur else ln
    if cur.strip():
        out.append(cur)
    return out


def make(title, color=BLUE, desc="", fields=(), foot=None, ts=None):
    """fields = [(name, lines or text)]; empty sections are left out. Never more than Discord takes."""
    e = {"title": str(title)[:256], "color": color, "footer": foot or footer(ts=ts)}
    if desc:
        e["description"] = str(desc)[:MAX_DESC]
    fl = []
    for name, val in fields:
        chunks = _split_value([x for x in _lines(val)])
        for i, ch in enumerate(chunks):
            fl.append({"name": (str(name) if i == 0 else "​")[:256], "value": ch, "inline": False})
    total = len(e["title"]) + len(e.get("description", "")) + len(e["footer"]["text"])
    keep = []
    for f in fl[:25]:
        n = len(f["name"]) + len(f["value"])
        if total + n > MAX_TOTAL:
            keep.append({"name": "​", "value": "… (more in the logs)", "inline": False})
            break
        keep.append(f)
        total += n
    if keep:
        e["fields"] = keep
    return e


def to_text(embed):
    """Plain-text version of a card (fallback when the card post fails, previews)."""
    out = ["**%s**" % embed.get("title", "")]
    if embed.get("description"):
        out.append(embed["description"])
    for f in embed.get("fields") or []:
        out += ([""] + ["**%s**" % f["name"]] if f["name"] != "​" else []) + [f["value"]]
    out += ["", "-# " + ((embed.get("footer") or {}).get("text") or "")]
    return "\n".join(out)


# ------------------------------------------------------------------ posting
def _hook(channel):
    core = sys.modules.get("openclaw_core")
    hooks = getattr(core, "DISCORD_WEBHOOKS", None) if core else None
    if hooks is None:
        import apex_ops
        hooks = apex_ops._core_const("DISCORD_WEBHOOKS")
    h = (hooks or {}).get(channel)
    return None if not h or "YOUR_TOKEN" in h else h


def suppressed(channel="alerts"):
    """True (and counted) while a /fetch run mutes #alerts in this process."""
    core = sys.modules.get("openclaw_core")
    sup = getattr(core, "_DISCORD_FETCH_SUPPRESS", None) if core else None
    if channel == "alerts" and sup and sup.get("active"):
        sup["suppressed_alerts"] = sup.get("suppressed_alerts", 0) + 1
        return True
    return False


def post(embed, channel="alerts", wait=False, check_mute=False):
    """Posts one card. wait=True returns (ok, message id). Falls back to plain text."""
    body = {k: v for k, v in embed.items() if not k.startswith("_")}
    if DRY:
        PREVIEW.append(body)
        print("---- card for #%s ----\n%s" % (channel, to_text(body)))
        return (True, None) if wait else True
    if check_mute and suppressed(channel):
        return (True, None) if wait else True
    hook = _hook(channel)
    if not hook:
        print("[AlertCards] #%s not configured -- card logged only: %s" % (channel, body.get("title")))
        return (True, None) if wait else True
    import requests
    url = hook + ("?wait=true" if wait else "")
    for payload in ({"embeds": [body]}, {"content": to_text(body)[:1990]}):
        for _attempt in range(4):
            try:
                r = requests.post(url, json=payload, timeout=15)
            except Exception as ex:
                print("[AlertCards] post error (%s)" % type(ex).__name__)
                time.sleep(2)
                continue
            if r.status_code in (200, 201, 204):
                mid = None
                if wait:
                    try:
                        mid = r.json().get("id")
                    except Exception:
                        pass
                return (True, mid) if wait else True
            if r.status_code == 429:
                try:
                    time.sleep(min(10.0, float((r.json() or {}).get("retry_after", 2))))
                except Exception:
                    time.sleep(2)
                continue
            print("[AlertCards] #%s card got HTTP %s%s" % (channel, r.status_code,
                                                           ", sending as text" if "embeds" in payload else ""))
            break
    return (False, None) if wait else False


def edit(message_id, embed, channel="alerts"):
    """APEX-ALERTCHAIN-2026-10-25: edits a card this webhook posted (apex_alert_chain). Edits never
    post a new message, so they still land while #alerts is muted. Returns True/False."""
    body = {k: v for k, v in embed.items() if not k.startswith("_")}
    if DRY:
        PREVIEW.append(dict(body, _edit_of=message_id))
        print("---- edit of card %s in #%s ----\n%s" % (message_id, channel, to_text(body)))
        return True
    hook = _hook(channel)
    if not hook or not message_id:
        return False
    import requests
    for _attempt in range(4):
        try:
            r = requests.patch("%s/messages/%s" % (hook, message_id), json={"embeds": [body], "content": ""},
                               timeout=15)
        except Exception as ex:
            print("[AlertCards] edit error (%s)" % type(ex).__name__)
            time.sleep(2)
            continue
        if r.status_code in (200, 204):
            return True
        if r.status_code == 429:
            try:
                time.sleep(min(10.0, float((r.json() or {}).get("retry_after", 2))))
            except Exception:
                time.sleep(2)
            continue
        print("[AlertCards] #%s edit of %s got HTTP %s" % (channel, message_id, r.status_code))
        return False
    return False


# ------------------------------------------------------------------ old plain-text alerts -> cards
_RED_MARK = ("\U0001F6A8", "\U0001F6D1", "❌", "⛔", "\U0001F6AB")
_RED_WORDS = ("EMPTY", "could not", "can't", "cannot", "failed", "FAILED", "stalled", "error", "ERROR",
              " down", "DOWN", "not answering", "401", "403", "missing")
_GREEN_WORDS = ("recovered", "is back", "back up", "answering again", "UNCHANGED", "all in correct order",
                "fixed", "caught up")
_ORANGE_MARK = ("⚠", "\U0001F501", "\U0001F4C5", "\U0001F4B2", "\U0001F6A6", "↪")
_ORANGE_WORDS = ("moved", "CHANGED", "changed", "slow", "pulled", "held", "warning", "check")


def color_for(text):
    t = text.strip()
    head = t.split("\n", 1)[0]
    if head.startswith("✅") or any(w in head for w in _GREEN_WORDS):
        return GREEN
    if any(m in head for m in _RED_MARK) or any(w in head for w in _RED_WORDS):
        return RED
    if any(m in head for m in _ORANGE_MARK) or any(w in head for w in _ORANGE_WORDS):
        return ORANGE
    if any(m in t for m in _RED_MARK):
        return RED
    return BLUE


def _clean(s):
    s = re.sub(r"^(#{1,3}\s+|-#\s+)", "", s.strip())
    return s.replace(" -- ", " — ")


def _starts_with_emoji(s):
    return bool(s) and not s[0].isalnum() and s[0] not in "*_([#'\"•-"


_SPLITS = (": ", " — ", " · ")


def _title_split(line):
    """('⚠️ FETCH SLOW', 'rest of the line') -- the label before ':' / '—' / '·' is the title."""
    plain = line.replace("**", "").strip()
    cut = None
    for sep in _SPLITS:   # ':' first ('Data check — Pinnacle: ...'), then '—', then '·'
        i = plain.find(sep)
        if 0 < i <= 60:
            cut = (i, sep)
            break
    if cut:
        left, rest = plain[:cut[0]].strip(), plain[cut[0] + len(cut[1]):].strip()
        if len(rest) > 0:
            return left, rest[0].upper() + rest[1:]
    if len(plain) <= 90:
        return plain, ""
    sp = plain.rfind(" ", 0, 70)
    return plain[:sp if sp > 20 else 70].rstrip(" ,;") + "…", plain


def _upper_label(t):
    """Short labels read as headlines: '⚠️ Fetch slow' -> '⚠️ FETCH SLOW'."""
    m = re.match(r"^(\W*)(.*)$", t)
    pre, word = (m.group(1), m.group(2)) if m else ("", t)
    return (pre + word.upper()) if len(word) <= 38 else t


def _body(line):
    """A long run-on line becomes one item per line."""
    if len(line) <= 110:
        return [line]
    for sep in (" · ", "; ", " | "):
        if line.count(sep) >= 1:
            parts = [p.strip() for p in line.split(sep) if p.strip()]
            return [p if _starts_with_emoji(p) else "• " + p for p in parts]
    return [line]


def from_text(text, color=None):
    """A card from an old plain-text #alerts message."""
    raw = [l for l in str(text or "").split("\n")]
    while raw and not raw[0].strip():
        raw.pop(0)
    if not raw:
        return make("ℹ️ APEX ALERT", BLUE, "(empty message)")
    col = color if color is not None else color_for(text)
    t, rest = _title_split(_clean(raw[0]))
    t = _upper_label(t)
    if not _starts_with_emoji(t):
        t = COLOR_EMOJI.get(col, "ℹ️") + " " + t
    body = ([rest] if rest and rest != t else []) + [_clean(l) for l in raw[1:]]
    out = []
    for ln in body:
        out += _body(ln) if ln.strip() else [""]
    while out and not out[-1].strip():
        out.pop()
    desc = "\n".join(out)
    fields = []
    if len(desc) > 3900:   # very long reports: the rest in sections
        cut = desc.rfind("\n", 0, 3900)
        cut = cut if cut > 0 else 3900
        desc, more = desc[:cut], desc[cut:].strip("\n")
        fields = [("​", more)]
    return make(t, col, desc, fields)


def post_text(text, channel="alerts", wait=False):
    return post(from_text(text), channel, wait=wait)


if __name__ == "__main__":   # python3 apex_alert_cards.py "<alert text>"  -> prints the card
    DRY = True
    post_text(" ".join(sys.argv[1:]) or "⚠️ **Fetch slow**: 10 min in, still scoring (120 of 400 scored). Started 5:01 PM. Boards not posted yet.")
