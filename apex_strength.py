"""APEX-BOARDSTRENGTH-2026-11-12 (owner, after Jack Quinn SOG 1.5 lost: "I need to know when a board is weak ... since it's on
the board it's the best of the best"; his go: "Go board strength on best6of6, bestless, goblin less all boards that has all sports").
Display only. For /best6of6, /bestless (/goblinless) and /lottery:
  * a strength line on top: 💪 STRONG (full card, every pick 85+), ✅ OK (full card, every pick 80+), ⚠️ WEAK (short card or a pick
    under 80), naming the weakest pick and how often all the picks hit together (each pick's own number multiplied);
  * each pick gets 💪 85+ / ✅ 80-84 / ⚠️ under 80;
  * moves since the last time that card was shown today: "⬆️ was #3", "⬇️ was #1", "🆕 new since 4:10 PM", and picks that dropped off.
The Lottery is a long shot by design, so it only gets the weakest leg and the moves (its own 1-in-N line stays).
No pick, rating, floor or order changes. State: data/board_strength_state.json. Turn off: ON = False, restart the tennis bot."""
import json, os
from datetime import datetime
from zoneinfo import ZoneInfo

ON = True
STRONG, SOLID = 85, 80
HONEST = True   # APEX-HONESTCARDS-2026-11-14: judge cross-sport card picks by their real win rate (apex_honest_cards)
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "board_strength_state.json")
CT = ZoneInfo("America/Chicago")


def tag(num):
    return "💪 %d" % round(num) if num >= STRONG else "✅ %d" % round(num) if num >= SOLID else "⚠️ %d weak" % round(num)


def _load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def _save(s):
    try:
        tmp = STATE + ".tmp"
        json.dump(s, open(tmp, "w"), indent=1)
        os.replace(tmp, STATE)
    except Exception:
        pass


def moves(card, items):
    """items: [dict(key, who)] in card order. Returns ({key: move text}, [dropped who], prev time or None). Saves this order."""
    now = datetime.now(CT); day = now.date().isoformat()
    s = _load(); prev = s.get(card) or {}
    out, dropped, when = {}, [], None
    if prev.get("day") == day and prev.get("order"):
        when = prev.get("time")
        old = {k: i for i, (k, w) in enumerate(prev["order"], 1)}
        for i, x in enumerate(items, 1):
            k = x.get("key")
            if k not in old: out[k] = "🆕 new since %s" % when
            elif old[k] > i: out[k] = "⬆️ was #%d" % old[k]
            elif old[k] < i: out[k] = "⬇️ was #%d" % old[k]
        now_keys = {x.get("key") for x in items}
        dropped = [w for k, w in prev["order"] if k not in now_keys]
    s[card] = dict(day=day, time=now.strftime("%-I:%M %p"), order=[[x.get("key"), x.get("who")] for x in items])
    _save(s)
    return out, dropped, when


def _chance(nums):
    p = 1.0
    for n in nums: p *= max(0.01, min(0.99, n / 100.0))
    return p


def block(card, items, size, lottery=False):
    """items: [dict(key, who, num)] in card order. Returns (lines for the top of the card, {key: text for the pick heading})."""
    if not ON: return [], {}
    try:
        mv, dropped, when = moves(card, items)
        if not items: 
            L = ["⚠️ **WEAK BOARD**: nothing qualifies right now."]
            if dropped: L.append("Dropped off since %s: %s" % (when, ", ".join(dropped)))
            return L, {}
        # APEX-HONESTCARDS-2026-11-14 (owner, "Fix all"): a pick that carries its sport is judged by its HONEST number (how
        # often that sport's picks in the same shown band really won, apex_honest_cards); strength, weakest pick and the all-hit line use
        # it, and each pick shows both. Lottery keeps its own true numbers. Undo: HONEST = False.
        shown = {id(x): x["num"] for x in items}
        if HONEST and not lottery:
            try:
                import apex_honest_cards as _HC
                items = [dict(x, num=(_HC.honest(x.get("sport"), x["num"])[0] or x["num"]), shown=x["num"]) for x in items]
            except Exception:
                pass
        low = min(items, key=lambda x: x["num"])
        p = _chance([x["num"] for x in items])
        all_hit = ("about %d out of 100" % round(100 * p)) if p >= 0.1 else ("about 1 in %d" % round(1 / p))
        if lottery:
            L = ["🎰 **LONG SHOT by design.** Weakest leg: %s (really hits about %d of 100)." % (low["who"], round(low["num"]))]
        else:
            short = len(items) < size; weak = [x for x in items if x["num"] < SOLID]
            if short or weak:
                why = []
                if short: why.append("only %d of %d spots filled" % (len(items), size))
                if weak: why.append("%d pick%s under %d" % (len(weak), "" if len(weak) == 1 else "s", SOLID))
                L = ["⚠️ **WEAK BOARD** (%s)." % ", ".join(why)]
            elif all(x["num"] >= STRONG for x in items):
                L = ["💪 **STRONG BOARD**: every pick %d or higher." % STRONG]
            else:
                L = ["✅ **OK BOARD**: every pick %d or higher, not all %d+." % (SOLID, STRONG)]
            if HONEST and any("shown" in x for x in items):
                L.append("Weakest pick: %s (Apex %d, really wins about %d of 100). All %d hit together %s slips, using each pick's REAL win rate." % (
                    low["who"], round(low.get("shown", low["num"])), round(low["num"]), len(items), all_hit))
                L.append("Real win rate = how often that sport's picks with the same Apex number really won since 11/5. Pick marks use it: 💪 85+ · ✅ 80-84 · ⚠️ under 80.")
            else:
                L.append("Weakest pick: %s (%d). All %d hit together %s slips (each pick's number multiplied)." % (low["who"], round(low["num"]), len(items), all_hit))
                L.append("Pick marks: 💪 85+ strong · ✅ 80-84 solid · ⚠️ under 80 weak.")
        if dropped: L.append("Dropped off since %s: %s" % (when, ", ".join(dropped)))
        heads = {}
        for x in items:
            t = [] if lottery else [tag(x["num"]) + ((" real (Apex %d)" % round(x["shown"])) if "shown" in x and round(x["shown"]) != round(x["num"]) else "")]
            if mv.get(x.get("key")): t.append(mv[x["key"]])
            heads[x.get("key")] = " · ".join(t)
        return L, heads
    except Exception as e:
        return ["(board strength unavailable: %s)" % type(e).__name__], {}
