#!/usr/bin/env python3
"""
APEX-SLIPSCHANNEL-2026-10-15 (owner, "Go" 2026-10-15 10:06 PM CT):
PrizePicks slip recommendations for #slips (the channel right before
#slips-pnl). Three slips from the published boards:

  🥇 Top 3 slip     -- the Top 3 board's picks
  🏆 Elite 6 slip   -- the Elite 6 board's picks
  📋 All Props slip -- the best /allprops picks (✅ only: 70%+ Win Probability,
                       Apex 67+, category gate), one per player and one per
                       match, size 3 or 6 -- whichever pays back the most per $[amount]
                       by Honest Probability

Cards follow the #slips-pnl layout he screenshotted, with more emojis (his
10:06 PM ask): payout, all-hit % (Win and Honest), risk tag, % the entry is
lost, $ back per $[amount] then each pick as its own block.

Every posted slip is saved in data/props_db.sqlite (rec_slips / rec_slip_legs:
type, size, tier, payout, stated %, legs) and graded from the settled forward
log: all legs hit = HIT, any leg lost = MISS, a push leg is dropped (void if
fewer than 2 remain). Finished slips get a result card in #slips. Read-only on
the boards -- never changes what a board picks.

  python3 apex_slips_channel.py            # print tonight's cards (records nothing)
  python3 apex_slips_channel.py --grade    # grade and print finished slips
"""
import csv, json, os, sqlite3, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "props_db.sqlite")
SNAP = os.path.join(HERE, "apex_published_board_snapshot.json")
FWD_LOG = os.path.join(HERE, "apex_prop_sim_forward_log_v1.csv")
HAND_LOG = os.path.join(HERE, "apex_engine_hand_settled_v1.csv")
STATE = os.path.join(HERE, "data", "logs", "slips_channel_state.json")
CT = ZoneInfo("America/Chicago")

# Payouts he confirmed from screenshots (apex_slip_planner.PAYOUTS, Power Play).
POWER = {"goblin": {3: 2.2, 6: 2.75}, "standard": {2: 3.0, 3: 6.0, 4: 10.0}}
FLEX = {("goblin", 3): {3: 1.8, 2: 0.5}, ("standard", 3): {3: 3.0, 2: 1.0}, ("standard", 4): {4: 6.0, 3: 1.5}}
TIERS = (("MEDIUM", 0.5, "\U0001F7E1"), ("HIGH", 0.3, "\U0001F7E0"),
         ("VERY HIGH", 0.15, "\U0001F534"), ("LONG SHOT", 0.0, "⚫"))   # safest -> riskiest, by Win Probability
KINDS = {"top3": ("\U0001F947", "TOP 3 SLIP"), "elite6": ("\U0001F3C6", "ELITE 6 SLIP"),
         "goblin": ("\U0001F7E2", "GOBLIN SLIP"),
         "allprops": ("\U0001F4CB", "ALL PROPS SLIP")}
STAT = {"total_games": ("\U0001F4CA", "Total Games"), "aces": ("\U0001F680", "Aces"),
        "fantasy_score": ("⭐", "Fantasy Score"), "games_won": ("\U0001F3BE", "Games Won"),
        "dfs": ("❗", "Double Faults"), "breaks": ("\U0001F4A5", "Break Points Won"),
        "total_sets": ("\U0001F522", "Total Sets"), "sets_won": ("\U0001F3C5", "Sets Won")}
PTYPE = {"goblin": ("\U0001F7E2", "Goblin"), "standard": ("\u26AA", "Standard"), "demon": ("\U0001F534", "Demon")}
NUM = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]
BLUE, GREEN, RED, GRAY = 0x3498DB, 0x2ECC71, 0xE74C3C, 0x95A5A6
STAKE = 10.0
# APEX-SAMEMATCH-2026-10-22 (owner, "1,4,5,6 go" on 10/22, item 5): a warning line on a
# slip card when two of its legs ride on the same match or the same player. Display only --
# never changes which legs go on a slip. Undo: SAME_MATCH_HINTS = False.
SAME_MATCH_HINTS = True
# APEX-SLIPSV2-2026-10-22 (owner, go on 10/22): #slips and /slips show only the Top 3 and
# Elite 6 slips, as numbered on the boards (apex_slips_v2.py, its own tracker). The old slip
# types keep recording underneath. Undo: SLIPS_V2_ONLY = False, restart the bot.
SLIPS_V2_ONLY = True


def _T():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import apex_edge_tracker as T
    return T


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


# ------------------------------------------------------------------ legs
def _starts(snap):
    """Earliest start per match across every board entry (the board can list
    one match at two times when PrizePicks staggers its props)."""
    out = {}
    for b in (snap.get("boards") or {}).values():
        for e in (b or {}).get("entries") or []:
            p = e.get("p") or {}
            s = p.get("slip_start_utc")
            if p.get("match_id") and s and (p["match_id"] not in out or s < out[p["match_id"]]):
                out[p["match_id"]] = s
    return out


def _leg(d, starts, flag=None):
    T = _T()
    p = d["p"]
    win = float(T._board_prob(d))
    _sv = T.score_value(flag, p) if getattr(T, "SCORE_DISPLAY", False) else None   # APEX-SCOREDISPLAY-2026-10-25
    return {"match_id": p.get("match_id", ""), "player": p.get("player", ""), "opponent": p.get("opponent", ""),
            "category": p.get("category", ""), "line": str(p.get("line", "")), "direction": str(p.get("direction", "")).upper(),
            "prop_type": str(p.get("prop_type") or "").lower(), "win": win, "honest": win if p.get("category") == "1st_set_total_games" else float(T.corrected_prob(win)),
            "proj": T.projection_note(p),   # APEX-ALLBOARDPROJ-2026-10-20: display only
            "start_utc": starts.get(p.get("match_id")) or p.get("slip_start_utc") or "", "match_date": p.get("match_date", ""),
            "score": _sv, "score_why": T.score_why(flag, p) if _sv is not None else ""}   # APEX-SCOREDISPLAY-2026-10-25


def mark_score_decimals(legs):
    """APEX-SCOREDISPLAY-2026-10-25: one decimal on legs whose Scores are within 1 point of each other."""
    try:
        idx = [k for k, l in enumerate(legs) if l.get("score") is not None]
        for k, dec in zip(idx, _T().score_decimals([legs[k]["score"] for k in idx])):
            legs[k]["score_dec"] = dec
    except Exception:
        pass
    return legs


def _score_bit(l):
    """' · 🏆 Score 100 (+20 signals)' for a slip leg, or ''."""
    if l.get("score") is None:
        return ""
    T = _T()
    return " \u00b7 \U0001F3C6 Ranking # %s%s" % (T._score_fmt(l["score"], l.get("score_dec") or 0),
                                             (" (%s)" % l["score_why"]) if l.get("score_why") else "")


def _not_started(legs, now):
    out = []
    for l in legs:
        try:
            if l["start_utc"] and datetime.fromisoformat(l["start_utc"]) <= now:
                continue
        except Exception:
            pass
        out.append(l)
    return out


def _who(l):
    if l["player"] == "match":
        return str(l["opponent"]).split(" v ")[0]
    return l["player"]


def _sur(n):
    n = str(n or "").strip()
    return n.split()[-1] if n else "?"


def _match_label(l):
    if l["player"] == "match":
        xs = [x.strip() for x in str(l["opponent"]).replace(" vs ", " v ").split(" v ")]
    else:
        xs = [l["player"], l["opponent"]]
    return " v ".join(_sur(x) for x in xs if x)


def same_match_lines(legs):
    """APEX-SAMEMATCH-2026-10-22: ['⚠ Two legs ride on the same match (Golubic v Hontama): ...']. Never raises."""
    try:
        from collections import OrderedDict
        nums = {2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six"}
        by_m = OrderedDict()
        for l in legs:
            k = l.get("match_id") or frozenset(x.lower() for x in _match_label(l).split(" v "))
            by_m.setdefault(k, []).append(l)
        out, covered = [], set()
        for k, ls in by_m.items():
            if len(ls) >= 2:
                out.append("\u26A0 %s legs ride on the same match (%s): one bad match kills %s" % (
                    nums.get(len(ls), str(len(ls))), _match_label(ls[0]), "both" if len(ls) == 2 else "all of them"))
                covered.update(id(l) for l in ls)
        by_p = OrderedDict()
        for l in legs:
            if l["player"] != "match" and id(l) not in covered:
                by_p.setdefault(str(l["player"]).lower(), []).append(l)
        for k, ls in by_p.items():
            if len(ls) >= 2:
                out.append("\u26A0 %s legs ride on the same player (%s): one bad day kills %s" % (
                    nums.get(len(ls), str(len(ls))), ls[0]["player"], "both" if len(ls) == 2 else "all of them"))
        return out
    except Exception:
        return []


def _payout(legs):
    types = {l["prop_type"] for l in legs}
    kind = types.pop() if len(types) == 1 else "mixed"
    return kind, (POWER.get(kind) or {}).get(len(legs))


def _stats(legs):
    win = hon = 1.0
    for l in legs:
        win *= l["win"]
        hon *= min(l["win"], l["honest"])
    return win, hon


def _slip(kind, legs):
    pk, pay = _payout(legs)
    win, hon = _stats(legs)
    return {"kind": kind, "n": len(legs), "legs": legs, "ptype": pk, "payout": pay, "win": win, "honest": hon}


def _allprops_legs(starts, now):
    """✅ /allprops picks (same rows qualified_detail() lists), best first,
    one per player and one per match."""
    T = _T()
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        picks, _ = T.load_current_board()
        rec = T.records()
    rows = []
    for p in picks:
        cat = p.get("category")
        if not cat or T._is_unplayable(p):
            continue
        sc = T.apex_score(p.get("prob_cal", p.get("prob")), cat, bool(p.get("bo5")), rec)[0]
        d = {"p": p, "sc": sc, "score": sc}
        if not (T._category_has_evidence(cat, rec) and T._category_probability_gate(cat, p.get("prob_cal", p.get("prob")))
                and T._board_qualifies(d)):
            continue
        if T._board_prob(d) < T.BOARD_MIN_PROB - 1e-9:
            continue
        if T.board_hold_reason(p):   # APEX-ALLPROPS-2026-11-01: every board hold (was only Fantasy LESS / DF-after-a-match)
            continue
        rows.append(d)
    rows.sort(key=lambda d: T._bb_sortkey(d))
    out, matches, players = [], set(), set()
    for d in rows:
        l = _leg(d, starts)
        who = l["opponent"].lower() if l["player"] == "match" else l["player"].lower()
        if l["match_id"] in matches or who in players:
            continue
        if not _not_started([l], now):
            continue
        matches.add(l["match_id"]); players.add(who)
        out.append(l)
    return out


def _fill(legs, pool, n=6, mark=False):
    """Tops a slip up to n picks with the best /allprops picks still to play,
    one per match and one per player (APEX-SLIPS6-2026-10-15: his "best 6")."""
    legs = list(legs)
    used_m = {l["match_id"] for l in legs}
    used_p = {(l["opponent"] if l["player"] == "match" else l["player"]).lower() for l in legs}
    for l in pool:
        if len(legs) >= n:
            break
        who = (l["opponent"] if l["player"] == "match" else l["player"]).lower()
        if l["match_id"] in used_m or who in used_p:
            continue
        legs.append(dict(l, added=True) if mark else l)
        used_m.add(l["match_id"]); used_p.add(who)
    return legs


def _mix(boards, starts, now, pool, n=6):
    """APEX-SLIPSMIX-2026-10-15 (his ask: "mix of props if available the best
    standards goblins and demon all different props categories"): the best
    Standard (Best Standard board) and best Demon (Best Demon board) still to
    play go in first, then the best /allprops picks, taking a category not yet
    on the slip before repeating one. One per match and one per player."""
    seeds = []
    for flag in ("beststandard", "bestdemon"):
        legs = _not_started([_leg(e, starts, flag) for e in (boards.get(flag) or {}).get("entries") or []], now)
        if legs:
            seeds.append(legs[0])
    legs = _fill([], seeds, n)
    cats = {l["category"] for l in legs}
    legs = _fill(legs, [l for l in pool if l["category"] not in cats], n)
    # one per new category: _fill takes them best-first, so trim repeats and refill
    out, seen = [], set()
    for l in legs:
        if l in seeds or l["category"] not in seen:
            out.append(l); seen.add(l["category"])
    out = _fill(out, [l for l in pool if l["category"] not in seen], n)
    out = _fill(out, pool, n)
    return sorted(out, key=lambda l: -l["win"])


def build(now=None, with_allprops=True):
    """Top 3, Elite 6, Goblin and All Props slips. Legs already started are
    left out; Top 3 / Elite 6 are topped back up to 3 / 6 from the best /allprops picks,
    Goblin = best 6 Goblin picks, All Props = best 6 picks of any type."""
    now = now or datetime.now(timezone.utc)
    snap = _load(SNAP, {})
    starts = _starts(snap)
    boards = snap.get("boards") or {}
    pool = []
    if with_allprops:
        try:
            pool = _allprops_legs(starts, now)
        except Exception as e:
            print("[Slips] allprops pool failed: %s: %s" % (type(e).__name__, e))
    out = []
    top3 = mark_score_decimals(_not_started([_leg(e, starts, "top3") for e in (boards.get("top3") or {}).get("entries") or []], now)[:3])
    top3 = _fill(top3, pool, 3, mark=True)
    if len(top3) >= 2:
        out.append(_slip("top3", top3))
    e6 = mark_score_decimals(_not_started([_leg(e, starts, "elite6") for e in (boards.get("elite6") or {}).get("entries") or []], now)[:6])
    e6 = _fill(e6, pool, mark=True)
    if len(e6) >= 2:
        out.append(_slip("elite6", e6))
    gob = _fill([], [l for l in pool if l["prop_type"] == "goblin"])
    if len(gob) >= 2:
        out.append(_slip("goblin", gob))
    allp = _mix(boards, starts, now, pool)
    if len(allp) >= 2:
        out.append(_slip("allprops", allp))
    for i, s in enumerate(out):
        s["same_as"] = next((o["kind"] for o in out[:i] if _legs_sig(o) == _legs_sig(s)), None)
    return snap, out


# ------------------------------------------------------------------ cards
def tier(win):
    return next(t for t, lo, _ in TIERS if win >= lo)


def _emo(t):
    return dict((n, e) for n, _, e in TIERS)[t]


def _time(iso):
    try:
        return datetime.fromisoformat(iso).astimezone(CT).strftime("%a %I:%M %p").replace(" 0", " ")
    except Exception:
        return "time TBA"


def _leg_lines(i, l, result=None):
    se, sn = STAT.get(l["category"], ("\U0001F3BE", l["category"].replace("_", " ").title()))
    arrow = "\U0001F4C8" if l["direction"] == "MORE" else "\U0001F4C9"
    head = NUM[i] if i < len(NUM) else "▪️"
    if result:
        head = {"hit": "✅", "miss": "❌", "void": "➖"}.get(result, "⏳")
    who = "**%s**" % _who(l)
    if l["player"] == "match":
        who += " v %s" % str(l["opponent"]).split(" v ")[-1]
    if l.get("added") and not result:
        who += " \u2795 _added: a board pick already started_"
    te, tn = PTYPE.get(l.get("prop_type"), ("", ""))
    L = ["%s %s" % (head, who), " %s %s %s %s %s%s%s" % (se, sn, arrow, l["direction"], l["line"], "" if result else (l.get("proj") or ""), (" \u00b7 %s %s" % (te, tn)) if tn else "")]   # APEX-ALLBOARDPROJ-2026-10-20
    if result in ("hit", "miss", "void"):
        fin = l.get("actual")
        L.append(" %s%s" % (("\U0001F3C1 finished **%g**" % fin) if fin is not None else "\U0001F3C1 settled",
                                 " · push, dropped" if result == "void" else ""))
    elif result:
        # APEX-SLIPCARDWAIT-2026-10-19: a leg with no result yet is not settled --
        # the slip was closed early by another leg's miss.
        try:
            later = datetime.fromisoformat(l["start_utc"]) > datetime.now(timezone.utc)
        except Exception:
            later = False
        L.append(" \u23f3 not played yet \u00b7 starts %s" % _time(l["start_utc"]) if later else " \u23f3 waiting on the result")
    else:
        L.append(" \U0001F3AF Win **%d%%**%s · \U0001F9E0 Honest %d%% · ⏰ %s" % (
            round(100 * l["win"]), _score_bit(l), round(100 * l["honest"]), _time(l["start_utc"])))
    return L


def card(s):
    emo, name = KINDS[s["kind"]]
    title = "%s %s · %d PICKS" % (emo, name, s["n"])
    t = tier(s["win"])
    lose = 100 - round(100 * s["win"])
    x = 1 / s["win"] if s["win"] > 0 else 0
    one_in = ("%.1f" % x).rstrip("0").rstrip(".") if x < 2.5 else str(round(x))
    D = []
    if s["kind"] == "allprops":
        from collections import Counter
        tc = Counter(PTYPE.get(l["prop_type"], ("", l["prop_type"].title()))[1] for l in s["legs"])
        D.append("\U0001F3A8 Mix: %s \u00b7 %d stat types" % (" \u00b7 ".join(
            "%d %s" % (v, k) for k, v in tc.most_common()), len({l["category"] for l in s["legs"]})))
    if SAME_MATCH_HINTS:   # APEX-SAMEMATCH-2026-10-22: display only
        D.extend(same_match_lines(s["legs"]))
    if s.get("same_as"):
        D.append("\U0001F501 Same %d picks as the %s %s right now" % (s["n"], KINDS[s["same_as"]][0], KINDS[s["same_as"]][1].title()))
    if s["payout"]:
        back = s["payout"] * s["honest"]
        D.append("\U0001F4B0 **Pays %gx Power** · $%.0f ➜ **$%.2f**" % (s["payout"], STAKE, STAKE * s["payout"]))
    else:
        be = 1.0 / s["honest"] if s["honest"] else 0
        what = "Mixed %d-pick" % s["n"] if s["ptype"] == "mixed" else "%d-pick %s" % (s["n"], s["ptype"].title())
        D.append("\U0001F4B0 %s \u2014 check the payout in the app (pays off only at **%.1fx** or more)" % (what, be))
    D.append("\U0001F3AF All hit: **%d%%** Win Prob · \U0001F9E0 %d%% Honest" % (round(100 * s["win"]), round(100 * s["honest"])))
    D.append("%s **%s%s** · hits about 1 in %s" % (_emo(t), t, "" if t == "LONG SHOT" else " RISK", one_in))
    D.append("\U0001F4B8 You lose the entry **%d%%** of the time" % lose)
    if s["payout"]:
        D.append("\U0001F4B5 **$%.2f** back per $1 (Honest)%s" % (back, "" if back >= 1 else " — loses money on average"))
        fx = FLEX.get((s["ptype"], s["n"]))
        if fx:
            D.append("\U0001F504 Flex %s" % " / ".join("%gx" % m for _, m in sorted(fx.items(), reverse=True)))
    legs = []
    for i, l in enumerate(s["legs"]):
        legs += _leg_lines(i, l)
    return {"title": title, "description": "\n".join(D), "color": BLUE, "legs": legs,
            "footer": "✅ All picks from the boards · recommendation, not placed · Goblin payouts can vary, check the app"}


def result_card(row, legs):
    emo, name = KINDS.get(row["kind"], ("\U0001F9FE", "SLIP"))
    hit = row["result"] == "hit"
    title = "%s %s %s" % ("✅" if hit else ("➖" if row["result"] == "void" else "❌"), name,
                          "HIT" if hit else ("VOID" if row["result"] == "void" else "MISSED"))
    wait = [l for l in legs if l.get("result") not in ("hit", "miss", "void")]
    if wait:
        # APEX-SLIPCARDWAIT-2026-10-19: lost early -- name the miss; unplayed legs aren't misses
        missed = [l for l in legs if l.get("result") == "miss"]
        D = ["%s **Lost on %s** \u00b7 we said **%d%%**" % (emo, ", ".join(_who(l) for l in missed) or "a miss",
                                                          round(100 * row["win"])),
             "**%d hit \u00b7 %d missed \u00b7 %d not finished yet**" % (
                 sum(l.get("result") == "hit" for l in legs), len(missed), len(wait))]
    else:
        D = ["%s **%d of %d hit** · we said **%d%%**" % (emo, row["legs_hit"], row["n"] - (row["legs_void"] or 0),
                                                            round(100 * row["win"]))]
    if row["payout"]:
        D.append("\U0001F4B0 %gx Power · $%.0f %s" % (row["payout"], STAKE,
                 ("➜ **+$%.2f**" % (STAKE * row["payout"] - STAKE)) if hit else "➜ **-$%.2f**" % STAKE))
    D.append("%s %s · recommended %s" % (_emo(row["tier"]), row["tier"], _time(row["posted_ts"])))
    L = []
    for i, l in enumerate(legs):
        L += _leg_lines(i, l, result=l.get("result") or "wait")
    return {"title": title, "description": "\n".join(D), "color": GREEN if hit else (GRAY if row["result"] == "void" else RED),
            "legs": L, "footer": "✅ Recommended slip from #slips · graded from settled results"}


def embed_dict(c):
    fields, body = [], "\n".join(c["legs"])
    while body:
        cut = body[:1024].rfind("\n") if len(body) > 1024 else len(body)
        cut = cut if cut > 0 else min(len(body), 1024)
        fields.append({"name": "\U0001F3BE Picks" if not fields else "​", "value": body[:cut], "inline": False})
        body = body[cut:].lstrip("\n")
    if c.get("tally"):
        fields.append({"name": "\U0001F4CA Slip record so far", "value": c["tally"][:1024], "inline": False})
    return {"title": c["title"], "description": c["description"], "color": c["color"], "fields": fields,
            "footer": {"text": c["footer"]}}


# ------------------------------------------------------------------ tracking
def _db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS rec_slips (sig TEXT PRIMARY KEY, posted_ts TEXT, kind TEXT, n INTEGER, "
              "ptype TEXT, tier TEXT, payout REAL, win REAL, honest REAL, last_start TEXT, match_date TEXT, "
              "result TEXT, legs_hit INTEGER, legs_void INTEGER, graded_ts TEXT, result_posted INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS rec_slip_legs (sig TEXT, pos INTEGER, match_id TEXT, player TEXT, "
              "opponent TEXT, category TEXT, line TEXT, direction TEXT, prop_type TEXT, win REAL, honest REAL, "
              "start_utc TEXT, match_date TEXT, result TEXT, actual REAL)")
    return c


def _legs_sig(s):
    return ",".join(sorted("%s|%s|%s|%s|%s" % (l["match_id"], l["player"], l["category"], l["line"], l["direction"])
                           for l in s["legs"]))


def _sig(s):
    return s["kind"] + ":" + _legs_sig(s)


def record(slips):
    """Saves each slip as posted. Same type + same legs = same slip, counted once."""
    c = _db()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = 0
    for s in slips:
        sig = _sig(s)
        if c.execute("SELECT 1 FROM rec_slips WHERE sig=?", (sig,)).fetchone():
            continue
        c.execute("INSERT INTO rec_slips (sig, posted_ts, kind, n, ptype, tier, payout, win, honest, last_start, match_date) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (sig, ts, s["kind"], s["n"], s["ptype"], tier(s["win"]), s["payout"], s["win"], s["honest"],
                   max(l["start_utc"] for l in s["legs"]), min(l["match_date"] for l in s["legs"])))
        for i, l in enumerate(s["legs"]):
            c.execute("INSERT INTO rec_slip_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sig, i, l["match_id"], l["player"], l["opponent"], l["category"], l["line"], l["direction"],
                       l["prop_type"], l["win"], l["honest"], l["start_utc"], l["match_date"], None, None))
        new += 1
    c.commit(); c.close()
    return new


def _n(s):
    return str(s or "").strip().lower()


def _actuals():
    """(match_id, category, player) -> actual from the forward log, and
    (category, player, opponent, match_date) -> actual from the hand log."""
    fwd, hand = {}, {}
    for path, idx, key in ((FWD_LOG, fwd, lambda r: (r.get("match_id"), r.get("category"), _n(r.get("player")))),
                           (HAND_LOG, hand, lambda r: (r.get("category"), _n(r.get("player")), _n(r.get("opponent")),
                                                       r.get("match_date")))):
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("actual") in (None, ""):
                    continue
                try:
                    idx[key(r)] = float(r["actual"])
                except ValueError:
                    pass
    return fwd, hand


_VOID_CACHE = {"mtime": None, "set": set()}


def _void_matches():
    """APEX-SLIPVOID-2026-10-27 (owner, "Find the results and just tell apex"): matches the
    settler marked void (walkover / cancelled / retired) in apex_settle_holds.json, as
    {(match_date, frozenset(names))}. A slip leg on such a match grades VOID instead of waiting
    forever (Fruhvirtova v Kawa 10/24 cancelled, Machac v Marozsan 10/25 walkover held the
    Oct 24-26 slips open). A real result in the hand/forward log always wins over a void mark."""
    path = os.path.join(HERE, "apex_settle_holds.json")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return set()
    if _VOID_CACHE["mtime"] == mt:
        return _VOID_CACHE["set"]
    out = set()
    try:
        with open(path) as fh:
            holds = json.load(fh).get("holds", [])
        for h in holds:
            if h.get("reason") != "void":
                continue
            if h.get("player") == "match":
                names = [_n(x) for x in str(h.get("opponent") or "").split(" v ")]
            else:
                names = [_n(h.get("player")), _n(h.get("opponent"))]
            out.add((str(h.get("match_date") or "")[:10], frozenset(names)))
    except Exception:
        pass
    _VOID_CACHE.update(mtime=mt, set=out)
    return out


def _leg_void(l):
    try:
        if l.get("player") == "match":
            names = [_n(x) for x in str(l.get("opponent") or "").split(" v ")]
        else:
            names = [_n(l.get("player")), _n(l.get("opponent"))]
        return (str(l.get("match_date") or "")[:10], frozenset(names)) in _void_matches()
    except Exception:
        return False


def _bh_actual(l):
    """APEX-SLIPGRADE-2026-11-09 (owner, "Go fix slip grading"): last resort for a leg
    with no hand/forward result -- the board-history result for the same prop (same players,
    category and line) within a day of the leg's date. A retired/void match grades as a push
    (actual = line), a hit/miss with a saved number uses that number. Never raises."""
    try:
        import sqlite3 as _sq
        db = _sq.connect("file:%s?mode=ro" % os.path.join(HERE, "data", "board_history.sqlite"), uri=True)
        db.row_factory = _sq.Row
        cat = l["category"]; line = float(l["line"])
        if str(l.get("player", "")).lower() == "match":
            names = [x.strip() for x in str(l.get("opponent", "")).replace(" vs ", " v ").split(" v ")]
        else:
            names = [str(l.get("player", "")).strip(), str(l.get("opponent", "")).strip()]
        if len(names) < 2 or not names[0] or not names[1]:
            return None
        day = l.get("match_date") or (l.get("start_utc") or "")[:10]
        rows = db.execute(
            "select result, result_games from history where category=? and abs(line-?)<0.01 and coalesce(result,'')!='' "
            "and board_day between date(?,'-1 day') and date(?,'+1 day') "
            "and ((player='match' and opponent like ? and opponent like ?) or (player=? and opponent=?) or (player=? and opponent=?)) "
            "order by id desc limit 5",
            (cat, line, day, day, "%" + names[0] + "%", "%" + names[1] + "%", names[0], names[1], names[1], names[0])).fetchall()
        db.close()
        for r in rows:
            if r["result"] == "void":
                return line
            if r["result_games"] is not None:
                return float(r["result_games"])
            if r["result"] in ("hit", "miss"):
                up = (l.get("direction") == "MORE") == (r["result"] == "hit")
                return line + 0.5 if up else line - 0.5
    except Exception:
        pass
    return None

def _leg_actual(l, fwd, hand):
    if l.get("category") == "1st_set_total_games":   # APEX-RANKFIRSTSET-2026-11-01
        import apex_set_games as _sg
        return _sg.leg_actual(l.get("match_id"), l.get("line"))
    a = fwd.get((l["match_id"], l["category"], _n(l["player"])))
    if a is None:
        a = hand.get((l["category"], _n(l["player"]), _n(l["opponent"]), l["match_date"]))
    if a is None:   # APEX-SLIPGRADE-2026-11-09: the hand log can carry the match under the day before/after
        import datetime as _dt
        for dd in (-1, 1):
            try:
                alt = (_dt.date.fromisoformat(str(l["match_date"])) + _dt.timedelta(days=dd)).isoformat()
            except Exception:
                break
            a = hand.get((l["category"], _n(l["player"]), _n(l["opponent"]), alt))
            if a is not None:
                break
    if a is None:
        a = _bh_actual(l)   # APEX-SLIPGRADE-2026-11-09
    if a is None and _leg_void(l):   # APEX-SLIPVOID-2026-10-27: push on the line = the leg is dropped
        try:
            a = float(l["line"])
        except (TypeError, ValueError, KeyError):
            a = None
    return a


def grade():
    """Grades open slips. Returns result cards for slips that just finished."""
    c = _db()
    fwd, hand = _actuals()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # APEX-SLIPCARDWAIT-2026-10-19: slips already lost keep filling in their
    # unfinished legs (no new post) so the record and hit counts end up complete.
    for row in c.execute("SELECT DISTINCT s.* FROM rec_slips s JOIN rec_slip_legs l ON l.sig = s.sig "
                         "WHERE s.result IS NULL OR (s.result = 'miss' AND l.result IS NULL)").fetchall():
        legs = c.execute("SELECT rowid, * FROM rec_slip_legs WHERE sig=? ORDER BY pos", (row["sig"],)).fetchall()
        hit = lost = void = wait = 0
        for l in legs:
            a = _leg_actual(dict(l), fwd, hand)
            if a is None:
                wait += 1
                continue
            line = float(l["line"])
            r = "void" if a == line else ("hit" if (a > line) == (l["direction"] == "MORE") else "miss")
            c.execute("UPDATE rec_slip_legs SET result=?, actual=? WHERE rowid=?", (r, a, l["rowid"]))
            hit += r == "hit"; lost += r == "miss"; void += r == "void"
        if row["result"] is not None:
            c.execute("UPDATE rec_slips SET legs_hit=?, legs_void=? WHERE sig=?", (hit, void, row["sig"]))
            continue
        if lost:
            res = "miss"           # one lost leg loses a Power Play
        elif wait:
            continue
        else:
            res = "hit" if hit >= 2 else "void"
        c.execute("UPDATE rec_slips SET result=?, legs_hit=?, legs_void=?, graded_ts=? WHERE sig=?",
                  (res, hit, void, ts, row["sig"]))
    c.commit()
    cards = []
    for row in c.execute("SELECT * FROM rec_slips WHERE result IS NOT NULL AND result_posted=0 ORDER BY posted_ts").fetchall():
        legs = [dict(l) for l in c.execute("SELECT * FROM rec_slip_legs WHERE sig=? ORDER BY pos", (row["sig"],))]
        cards.append((row["sig"], result_card(dict(row), legs)))
    c.close()
    return cards


def mark_result_posted(sigs):
    if not sigs:
        return
    if any(str(x).startswith("v2day:") for x in sigs):   # APEX-SLIPSV2-2026-10-22
        import apex_slips_v2 as v2
        v2.mark_day_posted([x for x in sigs if str(x).startswith("v2day:")])
        sigs = [x for x in sigs if not str(x).startswith("v2day:")]
        if not sigs:
            return
    c = _db()
    c.executemany("UPDATE rec_slips SET result_posted=1 WHERE sig=?", [(s,) for s in sigs])
    c.commit(); c.close()


def tally_text():
    """Record by slip type and by risk tier: actual hit % vs the all-hit % the card said."""
    c = _db()
    g = c.execute("SELECT kind, tier, win, result FROM rec_slips WHERE result IN ('hit','miss')").fetchall()
    pend = c.execute("SELECT COUNT(*) FROM rec_slips WHERE result IS NULL").fetchone()[0]
    c.close()
    if not g:
        return "⏳ No graded slips yet (%d waiting on results)" % pend

    def rec(rs):
        w = sum(r["result"] == "hit" for r in rs)
        return "%d-%d %d%% (said %d%%)" % (w, len(rs) - w, round(100 * w / len(rs)), round(100 * sum(r["win"] for r in rs) / len(rs)))
    L = []
    for k in KINDS:
        rs = [r for r in g if r["kind"] == k]
        if rs:
            L.append("%s %s %s" % (KINDS[k][0], KINDS[k][1].replace(" SLIP", "").title(), rec(rs)))
    for t, _, e in TIERS:
        rs = [r for r in g if r["tier"] == t]
        if rs:
            L.append("%s %s %s" % (e, t.title(), rec(rs)))
    return "\n".join(L) + "\n⏳ %d waiting" % pend


# ------------------------------------------------------------------ entry points
def _cards(slips):
    cards = [card(s) for s in slips]
    if cards:
        try:
            cards[0]["tally"] = tally_text()
        except Exception as e:
            print("[Slips] tally failed: %s" % e)
    return cards


def on_demand():
    """/slips: current cards, recorded like an auto post. APEX-SLIPSV2-2026-10-22: shows only the
    Top 3 and Elite 6 slips (as numbered on the boards); the old four types keep recording."""
    _, slips = build()
    try:
        record(slips)
    except Exception as e:
        print("[Slips] record failed: %s: %s" % (type(e).__name__, e))
    if SLIPS_V2_ONLY:
        import apex_slips_v2 as v2
        snap, v2s = v2.current_slips()
        try:
            v2.log_build(snap, v2s)
        except Exception as e:
            print("[SlipsV2] log failed: %s: %s" % (type(e).__name__, e))
        return v2.cards(snap, v2s)
    return _cards(slips)


def auto():
    """Bot loop: (header, new recommendation cards, [(sig, result card)]).
    Recommendations only when a new board was published AND a slip's legs
    differ from the last post.

    APEX-SLIPSV2-2026-10-22 (SLIPS_V2_ONLY): #slips gets only the 🥇 Top 3 (Power) and
    🏆 Elite 6 (Flex) slips taken from the boards exactly as numbered, plus one SLIPS V2
    result card per day. Every board build is logged to the Slips v2 tracker. The old
    slip types (Top 3 / Elite 6 with top-ups, Goblin, All Props) keep recording and
    grading in props_db rec_slips underneath; their cards and result cards stop posting.
    Undo: SLIPS_V2_ONLY = False."""
    st = _load(STATE, {})
    snap = _load(SNAP, {})
    rec_cards, header = [], None
    cap = snap.get("captured_at")
    new_board = bool(cap and cap != st.get("captured_at"))
    if new_board:
        _, slips = build()
        sigs = sorted(_sig(s) for s in slips)
        if slips and sigs != st.get("sigs"):
            record(slips)
            if not SLIPS_V2_ONLY:
                rec_cards = _cards(slips)
                header = "\U0001F9FE **New slips** — the boards just updated (%s CT)" % datetime.now(CT).strftime("%I:%M %p").lstrip("0")
            st["sigs"] = sigs
    if SLIPS_V2_ONLY and cap and (new_board or "v2_sigs" not in st):
        try:
            import apex_slips_v2 as v2
            vsnap, v2s = v2.current_slips(snap)
            v2.log_build(vsnap, v2s)
            vs = v2.sig(v2s)
            if vs != st.get("v2_sigs"):
                rec_cards = v2.cards(vsnap, v2s)
                header = "\U0001F9FE **New slips** — the boards just updated (%s CT)" % datetime.now(CT).strftime("%I:%M %p").lstrip("0")
                st["v2_sigs"] = vs
        except Exception as e:
            print("[SlipsV2] build failed: %s: %s" % (type(e).__name__, e))
    if new_board:
        st["captured_at"] = cap
    _save(STATE, st)
    try:
        results = grade()
    except Exception as e:
        print("[Slips] grade failed: %s: %s" % (type(e).__name__, e))
        results = []
    if SLIPS_V2_ONLY:
        if results:   # old result cards stay in props_db only
            mark_result_posted([sig for sig, _ in results])
        results = []
        try:
            import apex_slips_v2 as v2
            v2.grade()
            results = v2.daily_cards()
        except Exception as e:
            print("[SlipsV2] grade failed: %s: %s" % (type(e).__name__, e))
    return header, rec_cards, results


def _plain(c):
    return "\n".join(["**%s**" % c["title"], c["description"], ""] + c["legs"] +
                     (["", "Slip record so far:", c["tally"]] if c.get("tally") else []) + ["_%s_" % c["footer"], "-" * 30])


if __name__ == "__main__":
    if "--grade" in sys.argv:
        for _, c in grade():
            print(_plain(c))
    else:
        _, ss = build()
        for c in _cards(ss):
            print(_plain(c))
