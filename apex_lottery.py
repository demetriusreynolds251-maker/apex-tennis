#!/usr/bin/env python3
"""APEX-LOTTERY-2026-11-06 (owner, "Lottery picks"): one mixed-sports slip for the biggest payout with the best
chance. Read only: reads each Apex Sports system's saved scored lines (latest_scored.json) and tennis' own picks log.
Never pulls PrizePicks, never changes a pick, never imports another sport's code. Never raises (render returns text)."""
import csv, glob, json, os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SPORTS = os.path.expanduser("~/ApexSports")
HONEST_ONLY = True       # APEX-LOTTERYHONEST-2026-11-07: every leg needs an honest number (real tracker or past seasons)
NO_PLAY_OUT = True       # APEX-LOTTERYFIX-2026-11-07: skip legs the sport marks no play (not a board stat, Questionable MORE, ...)
SIZE = 6                 # 6-pick Flex
MAX_DEMONS = 5           # payout comes from Demons; one safe Standard keeps the slip alive
SAME_GAME_OK = True      # APEX-SAMEGAME-2026-11-08 (owner, test same game: same-game LESS pairs hit like different games). Undo: False
DEMON_MIN = 70.0         # Demon MORE at Apex 70+
# APEX-LOTTERYAPEXRANK-2026-11-12 (owner: "I want my lottery picks back with the old high numbers then the actual hit rate"; on 11/12
# the top 4 soccer Shots LESS in Apex's own order all won): a pick whose shown rating was moved to its real number (soccer regular Shots LESS,
# less_real) qualifies and is ordered on the Lottery card by Apex's OWN rating again; the card shows that rating and "really hits about X"
# next to it, and the slip odds still use the real number. Boards in the soccer server are not changed. Undo: APEX_RANK = False.
APEX_RANK = True
STANDARD_MIN = 80.0      # APEX-LOTTERY80-2026-11-08 (owner, "Go lottery 80"): Standard at Apex 80+ (was 75; 75-79 really hit 72-78%). Undo: 75.0
TRAP_GAP = 0.30          # Apex projection more than 30% away from PrizePicks' own line = PrizePicks knows something
TRAP_MIN_LINE = 4.0      # LESS on small count lines (soccer shots 2.5) is a proven pattern, not a trap; MORE checked from 1.5 up
START_BUFFER_MIN = 20
STALE_HOURS = 6          # a sport whose newest PrizePicks sighting is older than this is left out
FRESH_MIN = 15           # a line must be in that sport's newest pull
# APEX-DEMONRULES-2026-11-06 (owner, "go demon rules"): Demons only one step above the regular line, and only on
# the stats whose Demons held up in both halves of history. Number = how often those Demons really hit (shown on the card,
# used for the slip odds). Set DEMON_RULES = False to go back to any Demon at P70+.
DEMON_RULES = True
DEMON_OK = {("football/nfl", "rec_yds"): 44, ("basketball/wnba", "points"): 48}
# APEX-COLLEGEOFFCARDS-2026-11-13 (owner, "Only off the all-sports cards"): no college football legs on the Lottery card; the card says
# so in one plain line. The college server is untouched. Undo: CFB_OFF_CARDS = False (or git checkout before-college-off-cards).
CFB_OFF_CARDS = False   # APEX-COLLEGETOP4-2026-11-14: college back on the cards
NO_INTL = True   # APEX-SOCCERNOINTL-2026-11-15: national-team soccer games never become Lottery legs (we can't grade them). Undo: False


def _national(team):
    try:
        import importlib.util as _iu
        sp = _iu.spec_from_file_location("apex_soccer_nations", os.path.expanduser("~/ApexSports/soccer/models/nations.py"))
        m = _iu.module_from_spec(sp); sp.loader.exec_module(m)
        return m.is_national(team)
    except Exception:
        return False


BENCH_ONLY = True   # APEX-BENCHONLY-2026-11-14: soccer Shots LESS legs only for players confirmed on the bench


def _row_honest(r, sport, kd):
    """APEX-HONEST-2026-11-07: the sport's own honest number saved on the row (real tracker, or past seasons until 20 graded)."""
    h = r.get("honest")
    if isinstance(h, dict) and h.get("pct") is not None:
        return h["pct"]
    if kd == "demon" and DEMON_RULES:
        return DEMON_OK.get((sport, str(r.get("stat"))))
    return None


TENNIS = True            # tennis Standards only (tennis Demons hit 45% while Apex said 64%)
BB_STATS = ("points", "rebounds", "assists", "points+rebounds+assists", "points+rebounds", "points+assists",
            "rebounds+assists", "fieldGoalsMade", "pts", "reb", "ast", "pra", "pr", "pa", "ra")
BB_LABELS = ("Points", "Rebounds", "Assists", "PRA", "Pts+Rebs", "Pts+Asts", "Rebs+Asts", "FG Made", "Pts+Rebs+Asts")
NHL_LABELS = ("Points", "Assists", "Goals", "Shots On Goal")
EMO = {"football/nfl": "🏈 NFL", "football/ncaaf": "🏈 CFB", "basketball": "🏀 NBA", "basketball/ncaab": "🏀 CBB",
       "basketball/wnba": "🏀 WNBA", "hockey/nhl": "🏒 NHL", "soccer": "⚽ Soccer", "baseball/mlb": "⚾ MLB",
       "afl": "🏉 AFL", "rugby": "🏉 Rugby", "tennis": "🎾 Tennis"}
CT = timezone(timedelta(hours=-6))


def _ts(s):
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _ct(t, f="%a %-I:%M %p CT"):
    try:
        return t.astimezone(CT).strftime(f)
    except Exception:
        return "?"


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _honest(sport_dir):
    """{(kind, band): [hits, n]} from that sport's history file, read only."""
    try:
        d = json.load(open(os.path.join(sport_dir, "cache", "history_hp.json")))
        d = d.get("rates", d)
        return {(k.split("|")[0], int(k.split("|")[1])): v for k, v in d.items() if "|" in k}
    except Exception:
        return {}


def _sport_legs(now):
    legs, notes = [], []
    for f in sorted(glob.glob(SPORTS + "/**/latest_scored.json", recursive=True)):
        if "/_shared/" in f or "/templates/" in f:
            continue
        sd = os.path.dirname(os.path.dirname(f))
        sport = os.path.relpath(sd, SPORTS)
        if os.path.exists(os.path.join(sd, "INACTIVE")):
            continue
        if CFB_OFF_CARDS and sport == "football/ncaaf":
            continue
        try:
            rows = json.load(open(f)).get("rows") or []
        except Exception:
            continue
        if not rows:
            continue
        seen = [t for t in (_ts(r.get("last_seen")) for r in rows) if t]
        newest = max(seen) if seen else None
        if not newest or now - newest > timedelta(hours=STALE_HOURS):
            notes.append("%s lines are more than %d hours old, left out" % (EMO.get(sport, sport), STALE_HOURS))
            continue
        hon = _honest(sd)
        std, dem = {}, {}
        for r in rows:
            if str(r.get("kind")).lower() == "standard":
                std.setdefault((r.get("player"), r.get("stat")), []).append(_num(r.get("line")))
            if str(r.get("kind")).lower() == "demon":
                dem.setdefault((r.get("player"), r.get("stat")), []).append(_num(r.get("line")))
        for r in rows:
            kd = str(r.get("kind")).lower()
            side = str(r.get("side")).upper()
            if kd not in ("demon", "standard") or r.get("promo"):
                continue
            if NO_PLAY_OUT and r.get("no_play"):   # APEX-LOTTERYFIX-2026-11-07 (owner, "Go lottery fix"): the sport's own no-play rules apply here too
                continue
            # APEX-BENCHONLY-2026-11-14 (owner, "Go bench only"): a soccer regular Shots LESS is a Lottery leg only when the posted lineup
            # shows the player on the BENCH (same rule as the soccer boards). Undo: BENCH_ONLY = False.
            if BENCH_ONLY and sport == "soccer" and str(r.get("stat")) == "shots" and kd == "standard" and side == "LESS" \
                    and ((r.get("less_real") or {}).get("lineup") != "bench"):
                continue
            if NO_INTL and sport == "soccer" and (_national(r.get("team")) or _national(r.get("opponent"))):   # APEX-SOCCERNOINTL-2026-11-15
                continue
            if kd == "demon" and side != "MORE":
                continue
            if kd == "demon" and DEMON_RULES:   # APEX-DEMONRULES-2026-11-06: proven stat, first Demon step only
                if (sport, str(r.get("stat"))) not in DEMON_OK:
                    continue
                s0 = min([x for x in std.get((r.get("player"), r.get("stat")), []) if x] or [None])
                up = sorted(x for x in dem.get((r.get("player"), r.get("stat")), []) if x is not None and s0 is not None and x > s0)
                if s0 is None or not up or _num(r.get("line")) != up[0]:
                    continue
            st, ls = _ts(r.get("start")), _ts(r.get("last_seen"))
            if not st or st <= now + timedelta(minutes=START_BUFFER_MIN):
                continue
            if not ls or newest - ls > timedelta(minutes=FRESH_MIN):
                continue
            label = r.get("label") or r.get("stat_raw") or r.get("stat")
            if sport == "basketball" and str(r.get("stat")) not in BB_STATS and label not in BB_LABELS:
                continue
            if sport == "hockey/nhl" and label not in NHL_LABELS:
                continue
            p = _num(r.get("confidence") if r.get("confidence") is not None else r.get("win_prob"))
            lr = r.get("less_real")
            if APEX_RANK and isinstance(lr, dict) and lr.get("was") is not None:
                p = float(lr["was"])   # APEX-LOTTERYAPEXRANK-2026-11-12: Apex's own rating picks and orders the leg; the true number shows beside it
            line = _num(r.get("line"))
            if p is None or line is None:
                continue
            proj = _num(r.get("used") if r.get("used") is not None else (r.get("projected") or r.get("projection")))
            ref = min([x for x in std.get((r.get("player"), r.get("stat")), []) if x] or [line])
            trap = bool(proj is not None and ref >= (1.5 if side == "MORE" else TRAP_MIN_LINE) and abs(proj - ref) / ref > TRAP_GAP)
            band = min(int(p // 10) * 10, 90)
            h = hon.get((kd, band))
            legs.append(dict(key=str(r.get("pick_key") or r.get("projection_id") or ""), root=sd, sport=sport, player=r.get("player"), team=r.get("team") or "", opp=r.get("opponent") or "",
                             label=label, line=line, side=side, kind=kd, p=p, proj=proj, start=st, seen=ls, trap=trap, honest=_row_honest(r, sport, kd),
                             hp=(100.0 * h[0] / h[1]) if h and h[1] >= 20 else None,
                             game=(sport,) + tuple(sorted([str(r.get("team")), str(r.get("opponent"))])) + (str(r.get("game_date")),)))
    return legs, notes


def _tennis_honest(r, p):
    """APEX-HONEST-TENNIS-2026-11-07: tennis leg's honest number by pick type (apex_tennis_honest), or None."""
    try:
        import apex_tennis_honest as ath
        h = ath.honest(r.get("category"), "standard", r.get("direction"), p)
        return h["pct"] if h else None
    except Exception:
        return None


def _tennis_legs(now):
    legs = []
    if not TENNIS:
        return legs
    try:
        import apex_live_check as lc
        keys = lc.live_keys()
        if not keys:
            return legs
        best = {}
        since = (datetime.now() - timedelta(days=3)).isoformat()
        with open(os.path.join(HERE, "apex_engine_picks_log_v1.csv"), newline="", errors="ignore") as fh:
            for r in csv.DictReader(fh):
                if (r.get("ts") or "") < since or str(r.get("prop_type") or "standard").lower() != "standard":
                    continue
                best[(r.get("opponent"), r.get("player"), r.get("category"), r.get("line"), r.get("direction"))] = r
        for r in best.values():
            st = _ts(r.get("slip_start_utc"))
            p = _num(r.get("prob_cal") or r.get("prob"))
            if not st or st <= now + timedelta(minutes=START_BUFFER_MIN) or p is None:
                continue
            if str(r.get("playable") or "1") == "0" or not lc.is_live(r, keys):
                continue
            try:
                import apex_ban_list as abl
                if abl.is_banned(r):
                    continue
            except Exception:
                pass
            pl = r.get("opponent") if r.get("player") == "match" else str(r.get("player")).title()
            legs.append(dict(sport="tennis", player=pl, team="", opp=r.get("opponent") or "", label=str(r.get("category")).replace("_", " ").title(),
                             line=_num(r.get("line")), side=str(r.get("direction")).upper(), kind="standard", p=100 * p, proj=None,
                             start=st, seen=None, trap=False, hp=None, game=("tennis", r.get("opponent")),
                             tcat=r.get("category"), tpl=r.get("player"),   # APEX-LOTTERY80: for grading the saved slip
                             honest=_tennis_honest(r, p)))
    except Exception:
        pass
    return legs


# APEX-LOTTERYKEEP-2026-11-12 (owner, "Until they are graded put them back on the board as they are with a true hit rate"):
# the six soccer Shots LESS legs from the 12:06 PM board stay on the Lottery card for this one day, showing their real numbers
# (71-76, under the 80 rule). Ends by itself after KEEP_DAY. Undo now: KEEP_KEYS = ().
KEEP_DAY = "2026-11-12"
KEEP_KEYS = ("16903583|2.5|standard", "16826817|2.5|standard", "16869264|2.5|standard", "16902535|3|standard", "16912531|3|standard", "16912516|3|standard")


def _kept(x, now):
    return bool(KEEP_KEYS) and now.astimezone(CT).strftime("%Y-%m-%d") == KEEP_DAY and x.get("key") in KEEP_KEYS


def build(now=None):
    now = now or datetime.now(timezone.utc)
    legs, notes = _sport_legs(now)
    legs += _tennis_legs(now)
    ok = [x for x in legs if not x["trap"] and (x["p"] >= (DEMON_MIN if x["kind"] == "demon" else STANDARD_MIN) or _kept(x, now))]
    if any(_kept(x, now) for x in ok):
        kp = [x["p"] for x in ok if _kept(x, now)]
        notes.append("📌 The soccer Shots LESS picks are kept on this card today at your request. They show their TRUE numbers (%d-%d), which are under the usual 80 rule. All %d hitting is about 1 slip in %s" % (round(min(kp)), round(max(kp)), len(kp), _odds(kp)))
    if HONEST_ONLY:   # APEX-LOTTERYHONEST-2026-11-07 (owner, "Go lottery honest"): no leg without a checked honest number
        ok = [x for x in ok if x.get("honest") is not None]
    traps = [x for x in legs if x["trap"] and x["p"] >= (DEMON_MIN if x["kind"] == "demon" else STANDARD_MIN)]
    ok.sort(key=lambda x: -x["p"])
    used_g, used_p, demons, stds = set(), set(), [], []
    for want in ("demon", "standard"):   # Demons first (the payout), then the safest Standards; one per game and player
        for x in ok:
            if x["kind"] != want or (x["game"] in used_g and (not SAME_GAME_OK or x["sport"] == "tennis")) or x["player"] in used_p:
                continue
            if want == "demon" and len(demons) >= MAX_DEMONS:
                break
            (demons if want == "demon" else stds).append(x)
            used_g.add(x["game"]); used_p.add(x["player"])
    main = demons + stds[:SIZE - len(demons)]
    main = sorted(main, key=lambda x: x["start"])[:SIZE] if len(main) >= SIZE else main
    rest = [x for x in demons + stds if x not in main]
    return dict(main=main, backups=rest[:3], traps=sorted(traps, key=lambda x: -x["p"])[:3], notes=notes, now=now)


def _odds(ps):
    c = 1.0
    for p in ps:
        c *= p / 100.0
    return max(1, round(1 / c)) if c > 0 else None


def _leg(x, n):
    k = "🔴 Demon" if x["kind"] == "demon" else "⚪ Standard"
    L = ["🔸 **#%d · %s** %s%s" % (n, x["player"], EMO.get(x["sport"], x["sport"]),
                                   (" · %s v %s" % (x["team"], x["opp"])) if x["team"] else (" · %s" % x["opp"] if x["opp"] and x["opp"] != x["player"] else ""))]
    L.append("🕐 **Starts %s**%s" % (_ct(x["start"]), (" · 👀 Seen on PrizePicks %s" % _ct(x["seen"], "%-I:%M %p CT")) if x["seen"] else ""))
    L.append("📊 %s %g %s **%s** · %s%s" % (x["label"], x["line"], "📈" if x["side"] == "MORE" else "📉", x["side"], k,
                                          (" · Apex projects **%.1f**" % x["proj"]) if x["proj"] is not None else ""))
    L.append("🎯 **P%d**%s%s" % (round(x["p"]), (" / HP%d" % round(x["hp"])) if x["hp"] is not None else "",
                                (" · really hits about **%d%%**" % x["honest"]) if x.get("honest") else ""))
    return "\n".join(L)


# APEX-LOTTERY-FULLCARD-2026-11-06 ("it should show the same full information it would show on its own server"):
# each leg's card is made by that sport's OWN card code, run in its own process inside its own folder, so no sport's
# code is imported into tennis. If that fails, the short card below is used.
FULL_CARDS = True
_SPORT_SNIP = r"""
import sys, os, json
root = sys.argv[1]; os.chdir(root)
want = json.loads(sys.stdin.read())
if os.path.basename(root) == "basketball":
    sys.path.insert(0, os.path.join(root, "code")); sys.path.insert(0, os.path.join(root, "discord"))
    import cards as K
    rows = json.load(open(os.path.join(root, "data", "latest_scored.json")))["rows"]
    by = {str(r.get("projection_id")): r for r in rows}
    out = {k: K.pick_card(by[k], n, rows=rows) for n, k in want if k in by}
else:
    sys.path.insert(0, root)
    import common as CM
    K = CM.dmod("cards")
    rows = json.load(open(os.path.join(root, "cache", "latest_scored.json")))["rows"]
    by = {str(r.get("pick_key")): r for r in rows}
    out = {k: K.pick_card(by[k], n, rows=rows) for n, k in want if k in by}
print(json.dumps(out))
"""


def _full_cards(legs):
    """{key: card text} from each sport's own pick_card. Never raises."""
    out = {}
    if not FULL_CARDS:
        return out
    import subprocess
    by_root = {}
    for n, x in enumerate(legs, 1):
        if x.get("root") and x.get("key"):
            by_root.setdefault(x["root"], []).append([n, x["key"]])
    for root, want in by_root.items():
        try:
            p = subprocess.run(["/usr/bin/python3", "-c", _SPORT_SNIP, root], input=json.dumps(want), capture_output=True,
                               text=True, timeout=60)
            out.update(json.loads(p.stdout.strip().splitlines()[-1]))
        except Exception:
            pass
    return out


# APEX-LOTTERY80-2026-11-08 (owner, "Go lottery 80"): every full Lottery slip /lottery shows is saved once per
# day (same 6 legs = same slip) to data/lottery_slips.jsonl and graded from each sport's own results, so Lottery gets a
# real won/lost record. Read only on every other file. LOG_SLIPS = False turns it off.
LOG_SLIPS = True
SLIP_LOG = os.path.join(HERE, "data", "lottery_slips.jsonl")


def _leg_rec(x):
    return dict(sport=x["sport"], root=x.get("root"), key=x.get("key"), player=x["player"], opp=x.get("opp"), label=str(x["label"]),
                line=x["line"], side=x["side"], kind=x["kind"], p=round(x["p"], 1), honest=x.get("honest"),
                start=x["start"].isoformat(), tcat=x.get("tcat"), tpl=x.get("tpl"), result=None)


def _read_log():
    out = []
    try:
        with open(SLIP_LOG) as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    out.append(json.loads(ln))
    except Exception:
        pass
    return out


def _write_log(rows):
    tmp = SLIP_LOG + ".tmp"
    with open(tmp, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    os.replace(tmp, SLIP_LOG)


def _log_slip(m, now):
    """Save a full slip once (same legs = same slip). Never raises."""
    if not LOG_SLIPS or len(m) < SIZE:
        return
    try:
        legs = [_leg_rec(x) for x in m]
        sig = "|".join(sorted("%s %s %s %g %s" % (l["sport"], l["player"], l["label"], l["line"], l["side"]) for l in legs))
        rows = _read_log()
        if any(r.get("sig") == sig for r in rows):
            return
        rows.append(dict(sig=sig, posted=now.isoformat(), day=_ct(now, "%Y-%m-%d"), legs=legs, result=None))
        _write_log(rows)
    except Exception:
        pass


def _grade_leg(l):
    """'win' / 'loss' / 'push' / 'void' or None (not graded yet)."""
    import sqlite3, re
    try:
        if l["sport"] != "tennis":
            con = sqlite3.connect("file:%s?mode=ro" % os.path.join(l["root"], "database", "history.db"), uri=True)
            r = con.execute("select status from picks where pick_key=?", (l["key"],)).fetchone()
            con.close()
            st = (r[0] if r else None) or ""
            return st if st in ("win", "loss", "push", "void") else None
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(HERE, "data", "props_db.sqlite"), uri=True)
        day = l["start"][:10]
        who = l.get("tpl") if l.get("tpl") and l.get("tpl") != "match" else (l.get("opp") or "")
        sur = re.split(r"\s+v\s+|\s+vs\s+", who)[0].split()[-1] if who else ""
        rows = con.execute("select result from projections where category=? and odds_type='standard' and abs(coalesce(line_last,line_first)-?)<0.01 "
                           "and (player like ? or opponent like ?) and match_date between date(?, '-1 day') and date(?, '+1 day') and result is not null",
                           (l.get("tcat"), l["line"], "%" + sur + "%", "%" + sur + "%", day, day)).fetchall()
        con.close()
        for (res,) in rows:
            res = str(res).lower()
            if res in ("void", "push"):
                return res
            if res in ("more", "less"):
                return "win" if res == l["side"].lower() else "loss"
        return None
    except Exception:
        return None


def _record():
    """Grade saved slips, return one record line (or ''). Never raises."""
    if not LOG_SLIPS:
        return ""
    try:
        rows = _read_log()
        changed = False
        now = datetime.now(timezone.utc)
        for s in rows:
            if s.get("result"):
                continue
            for l in s["legs"]:
                if l.get("result") is None:
                    g = _grade_leg(l)
                    if g:
                        l["result"] = g; changed = True
            res = [l.get("result") for l in s["legs"]]
            last = max(_ts(l["start"]) for l in s["legs"])
            if all(res) or (last and now - last > timedelta(days=3)):
                live = [r for r in res if r in ("win", "loss")]
                hits = sum(1 for r in live if r == "win")
                miss = sum(1 for r in live if r == "loss")
                pend = sum(1 for r in res if not r)
                s["result"] = "unknown" if pend else ("all hit" if miss == 0 else "one miss" if miss == 1 else "lost")
                s["hits"], s["misses"] = hits, miss
                changed = True
        if changed:
            _write_log(rows)
        done = [s for s in rows if s.get("result")]
        if not rows:
            return "📒 Lottery record: starts with today's slip (every full slip is saved and graded)."
        a = sum(1 for s in done if s["result"] == "all hit")
        o = sum(1 for s in done if s["result"] == "one miss")
        lo = sum(1 for s in done if s["result"] == "lost")
        return "📒 Lottery record (real slips since 11/8): %d all hit · %d one miss (Flex still pays) · %d lost · %d waiting" % (a, o, lo, len(rows) - len(done))
    except Exception:
        return ""


def render():
    try:
        b = build()
        RULE = "━━━━━━━━━━━━━━━━"
        L = ["🎰 **LOTTERY PICKS** · %s" % _ct(b["now"], "%a %-m/%-d %-I:%M %p CT"),
             "🚦 Mixed sports, biggest payout with the best chance: up to %d 🔴 Demons at P%d+ plus ⚪ Standards at P%d+, "
             "one per player (team sports can share a game; tennis one per match), no Goblins. Lines where PrizePicks' own number says it knows something are cut as traps."
             % (MAX_DEMONS, DEMON_MIN, STANDARD_MIN)]
        if CFB_OFF_CARDS:
            L.append("🏈 College football is off this card by your choice (its own server's boards still post).")
        if DEMON_RULES:
            L.append("🔴 Demons allowed: one step above the regular line, NFL Receiving Yards or WNBA Points only (the only Demons that held up in testing).")
        m = b["main"]
        try:   # APEX-BOARDSTRENGTH-2026-11-12: weakest leg (true number) + moves since the last look; display only
            import apex_strength as _ST
            _sl, _sh = _ST.block("lottery", [dict(key=x.get("key"), who="%s %s %g %s" % (x["player"], x["label"], x["line"], x["side"]), num=(x.get("honest") or x["p"])) for x in m], SIZE, lottery=True)
        except Exception:
            _sl, _sh = [], {}
        L += _sl
        if len(m) < SIZE:
            L.append("🚫 Not enough qualified picks right now. (%d of the %d legs this slip needs.)" % (len(m), SIZE))
            fc = _full_cards(m)   # APEX-LOTTERYCARDS-2026-11-07: short slips get the same full cards
            for i, x in enumerate(m, 1):
                c = fc.get(x.get("key"))
                L += [RULE, ("%s %s · %s\n" % (EMO.get(x["sport"], x["sport"]), "🔴 Demon" if x["kind"] == "demon" else "⚪ Standard", "🎯 P%d" % round(x["p"]) + ((" · really hits about %d%%" % x["honest"]) if x.get("honest") else "") + ((" · " + _sh[x.get("key")]) if _sh.get(x.get("key")) else "")) + c) if c else _leg(x, i)]
        else:
            fc = _full_cards(m)
            for i, x in enumerate(m, 1):
                c = fc.get(x.get("key"))
                L += [RULE, ("%s %s · %s\n" % (EMO.get(x["sport"], x["sport"]), "🔴 Demon" if x["kind"] == "demon" else "⚪ Standard", "🎯 P%d" % round(x["p"]) + ((" · really hits about %d%%" % x["honest"]) if x.get("honest") else "") + ((" · " + _sh[x.get("key")]) if _sh.get(x.get("key")) else "")) + c) if c else _leg(x, i)]
            L.append(RULE)
            nd = sum(1 for x in m if x["kind"] == "demon")
            L.append("🎟 Play as a **6-pick Flex** (%d Demon%s): one miss still pays." % (nd, "" if nd == 1 else "s"))
            o = _odds([x.get("honest") or x["p"] for x in m])   # APEX-DEMONRULES-2026-11-06: Demons counted at their real hit rate
            if o:
                L.append("🎲 All 6 hit about **1 out of %d** slips using each leg's honest number%s." % (o, "" if all(x.get("honest") for x in m) else " (Apex's own number where a leg has none yet)"))   # APEX-HONEST-2026-11-07
            L.append("⏰ Lock it in before **%s** (first leg starts). Check every line is still on PrizePicks first." % _ct(m[0]["start"]))
        if b["backups"]:
            L.append("BACKUPS: " + " · ".join("%s %s %g %s P%d" % ("🔴" if x["kind"] == "demon" else "⚪", x["player"], x["line"], x["side"], round(x["p"])) for x in b["backups"]))
        if b["traps"]:
            L.append(RULE)   # APEX-TRAPWORDS-2026-11-07 ("go trap words"): its own section, clearly NOT about the picks above
            L.append("🪤 **NOT ON THIS SLIP · Traps cut** (other players, not the picks above)")
            L.append("Apex liked these, but its number is far from PrizePicks' line. A gap that big usually means PrizePicks knows something we don't (injury, bigger role, game plan), so we leave them off.")
            for x in b["traps"]:
                pj = ("%.1f" % x["proj"]) if x.get("proj") is not None else "?"
                L.append("• %s %s · %s %s %g: Apex expects about **%s**, but PrizePicks set the line at **%g**. Too far apart to trust, so it's left off." % (EMO.get(x["sport"], x["sport"]), x["player"], x["label"], x["side"], x["line"], pj, x["line"]))
        for n in b["notes"]:
            L.append("⚠️ " + n)
        _log_slip(b["main"], b["now"])   # APEX-LOTTERY80
        _rl = _record()
        if _rl:
            L.append(_rl)
        L.append("P = Apex probability · HP = how that kind of pick really hit in history (shown when 20+ graded).")
        return "\n".join(L)
    except Exception as e:
        return "🎰 Lottery Picks unavailable (%s)" % type(e).__name__


if __name__ == "__main__":
    print(render())
