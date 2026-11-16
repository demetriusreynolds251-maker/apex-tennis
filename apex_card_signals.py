#!/usr/bin/env python3
"""
APEX-CARDSIGNALS-2026-10-21 (owner: "For the strong let's do it but I
need the signal to be self explanatory"): the five strong outside signals from
the 10/21 study, shown on every board card in plain words. DISPLAY ONLY --
nothing here ranks, qualifies, or removes a prop.

Records quoted on the lines are from the 10/21 study (70%+ props you can play,
Oct 9-21):
  1. ParlayPlay price on the same Total Games line: 1.15x or less 14-2 (88%),
     1.16-1.30x 39-16 (71%), over 1.30x 5-3 (62%)
  2. bet365 cushion on Total Games: 3+ games under bet365's line 80-18 (82%),
     under 3 games 22-17 (56%)
  3. Re-priced with fresh odds 75-17 (82%) vs still on the first price 68%
  4. Total Games by favorite strength (APEX-FAVLABEL-2026-10-25, deduped Oct 10-24):
     close match (favorite under 70%) 75%, 70-80% favorite 58%, 80%+ neutral
  5. Played yesterday 77-80% vs no match yesterday 68%

  python3 apex_card_signals.py   # print the lines for today's board props
"""
import csv, os, re, sqlite3, time, unicodedata
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PP_DB = os.path.join(HERE, "ApexParlayPlay", "data", "parlayplay_db.sqlite")
PROPS_DB = os.path.join(HERE, "data", "props_db.sqlite")
PICK_LOG = os.path.join(HERE, "apex_engine_picks_log_v1.csv")
TTL = 600
_cache = {}



def _failnote(name, err):   # APEX-FAILLOG-2026-11-01
    try:
        import apex_failopen as _fo
        _fo.note(name, err)
    except Exception:
        pass


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def _names(p):
    if p.get("player") == "match":
        return [x.strip() for x in str(p.get("opponent") or "").replace(" vs ", " v ").split(" v ") if x.strip()]
    return [x for x in (p.get("player"), p.get("opponent")) if x]


def _cached(name, build):
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    try:
        val = build()
    except Exception as _fe:
        _failnote("_cached", _fe)
        return {}   # APEX-FAVLOOKUP-2026-11-01: a failed read is not remembered for 10 minutes
    _cache[name] = (time.time(), val)
    return val


def _pp_ladder():
    out = {}
    con = sqlite3.connect(PP_DB)
    for home, away, start, line, mm in con.execute(
            "select home, away, start_utc, line, mult_more from lines where category = 'total_games' "
            "and start_utc >= ?", ((date.today() - timedelta(days=2)).isoformat(),)):
        if mm:
            out[(frozenset([_norm(home), _norm(away)]), float(line))] = float(mm)
    con.close()
    return out


def book_total(p):
    """APEX-BET365-THREEPART-2026-10-22 (owner, "Go bet365"): bet365's total for this prop's
    match ON its match date, via the shared apex_bet365_totals reader. None when unknown."""
    try:
        import apex_bet365_totals as b3
        v = b3.total(_names(p), str(p.get("match_date") or "")[:10])[0]
        if v is not None:   # APEX-BOOKCHECK-2026-11-01: too old and a fresh backup exists, use the backup
            _age = b3.log_age_hours(_names(p), str(p.get("match_date") or "")[:10])
            if _age is not None and _age > b3.BOOK_MAX_AGE_H:
                import apex_sgo_backup as _sb0
                v = _sb0.total(_names(p), str(p.get("match_date") or "")[:10])[0] or v
        if v is None and _is_pp(p) and p.get("bet365_total") not in (None, ""):   # APEX-PPMIRROR-B-2026-10-23
            v = float(p["bet365_total"])
        if v is None:   # APEX-SGOBACKUP-2026-10-31: only when bet365 has none
            import apex_sgo_backup as _sb
            v = _sb.total(_names(p), str(p.get("match_date") or "")[:10])[0]
        if v is None:   # APEX-BOOKDATE-2026-11-01: saved total from before a date move, last choice
            v = b3.total_near(_names(p), str(p.get("match_date") or "")[:10])[0]
        return v
    except Exception as _fe:
        _failnote("book_total", _fe)
        return None


# APEX-PPMIRROR-B-2026-10-23 (owner, "Go 1-8"): ParlayPlay props (prop_type "parlayplay") carry
# their own bet365 favorite chance and bet365 total (read by apex_parlayplay from the same SportsAPI365
# pre-match odds). They are used ONLY for those props and only when the shared PrizePicks sources
# have nothing, so PrizePicks cards and ranks are unchanged.
def _is_pp(p):
    return str(p.get("prop_type") or "").lower() == "parlayplay"


def _book_for(p, names):
    import apex_top3_no175_papertest as _t   # APEX-175BET365-2026-10-22: same bet365 total as the 🎯 check
    bk = _t.bet365_total(names, str(p.get("match_date") or "")[:10])[0]
    if bk is None and _is_pp(p):
        try:
            bk = float(p["bet365_total"]) if p.get("bet365_total") not in (None, "") else None
        except (TypeError, ValueError):
            bk = None
    return bk


def _book_word(p, names):
    """APEX-SGOBACKUP-2026-10-31 / APEX-BOOKCHECK-2026-11-01: who the card credits for the total, with its age. Never raises."""
    try:
        import apex_top3_no175_papertest as _t
        md = str(p.get("match_date") or "")[:10]
        if _t.bet365_total(names, md)[1] == "sgo":
            return "SportsGameOdds (bet365 has none or is old)"
        import apex_bet365_totals as b3
        if b3.book_of(names, md) == "fonbet":   # APEX-FONBET-2026-11-07
            return "FonBet (bet365 has no total)"
        age = b3.log_age_hours(names, md)
        if age is not None:
            return "bet365 (%d hours old)" % round(age)
    except Exception:
        pass
    return "bet365"


def _fav_for(p, fav):
    if not fav and _is_pp(p):
        return p.get("pinn_fav") or ""
    return fav


def _pair_fav_rows():
    """APEX-FAVLOOKUP-2026-11-01: {frozenset(players): favorite chance}, newest priced row of the last 3 days."""
    out = {}
    with open(PICK_LOG, newline="", errors="ignore") as fh:
        for r in csv.DictReader(fh):
            if (r.get("match_date") or "") < (date.today() - timedelta(days=3)).isoformat() or not r.get("pinn_fav"):
                continue
            nm = [x.strip() for x in str(r.get("opponent") or "").split(" v ")] if r.get("player") == "match" else [r.get("player"), r.get("opponent")]
            if len(nm) == 2:
                out[frozenset(_norm(x) for x in nm)] = r.get("pinn_fav")
    return out


def match_favorite(p):
    """The favorite's chance for this prop's match, any line. None if unknown. Never raises."""
    try:
        _src, fav = _cached("picks", _pick_rows).get(
            (p.get("match_id"), p.get("category"), str(p.get("line")), str(p.get("prop_type") or "").lower()), ("", ""))
        fav = _fav_for(p, fav) or _cached("pairfav", _pair_fav_rows).get(frozenset(_norm(x) for x in _names(p)))
        return float(fav) if fav not in (None, "") else None
    except Exception as _fe:
        _failnote("match_favorite", _fe)
        return None


def _pick_rows():
    out = {}
    with open(PICK_LOG, newline="") as fh:
        for r in csv.DictReader(fh):
            if (r.get("match_date") or "") < (date.today() - timedelta(days=2)).isoformat():
                continue
            out[(r.get("match_id"), r.get("category"), str(r.get("line")), str(r.get("prop_type") or "").lower())] = (
                r.get("price_source") or "", r.get("pinn_fav") or "")
    return out


def _played():
    out = {}
    con = sqlite3.connect(PROPS_DB)
    for pl, md, score in con.execute(
            "select player, match_date, score from projections where score like '%-%' and player != 'match' "
            "and match_date >= ?", ((date.today() - timedelta(days=4)).isoformat(),)):
        try:
            sets = [tuple(int(x) for x in s.split("-")[:2]) for s in score.split(",")]
            out[(_norm(pl), md)] = (len(sets), sum(a + b for a, b in sets))
        except Exception:
            continue
    con.close()
    return out


def _starts_played():
    """APEX-PLAYEDFIX-2026-11-01: {player: [(start, opponent, sets, games)]} from finished matches, last 5 days."""
    out = {}
    con = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True)
    seen = set()
    for pl, op, st, score in con.execute(
            "select player, opponent, start_utc, score from projections where score like '%-%' and player != 'match' "
            "and start_utc >= ?", ((datetime.utcnow() - timedelta(days=5)).isoformat(),)):
        try:
            sets = [tuple(int(x) for x in s.split("-")[:2]) for s in score.split(",")]
            k = (_norm(pl), st)
            if k in seen:
                continue
            seen.add(k)
            out.setdefault(_norm(pl), []).append((_when_ts(st), _norm(re.sub(r"\s*1st\s*set.*$", "", op or "", flags=re.I)),
                                                  len(sets), sum(a + b for a, b in sets)))
        except Exception:
            continue
    con.close()
    return out


def _when_ts(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d.timestamp() if d.tzinfo else d.replace(tzinfo=timezone.utc).timestamp()


def played_before(name, p):
    """(sets, games) if this player finished a DIFFERENT match that started 10-40 hours before this prop's match,
    else None. Falls back to the old date lookup when the prop has no start time. Never raises."""
    try:
        st = p.get("slip_start_utc") or p.get("start_utc")
        if not st:
            md = str(p.get("match_date") or "")[:10]
            return _cached("played", _played).get((_norm(name), (date.fromisoformat(md) - timedelta(days=1)).isoformat())) if md else None
        t0 = _when_ts(st)
        others = {_norm(x) for x in _names(p)}
        for t, op, ns, g in _cached("startsplayed", _starts_played).get(_norm(name), []):
            if op not in others and 10 * 3600 <= t0 - t <= 40 * 3600:
                return ns, g
    except Exception as _fe:
        _failnote("played_before", _fe)
    return None


def played_yesterday(p):
    """APEX-DFFANTASY-2026-10-22: True when the prop's player (either player on a match prop)
    played the day before -- the same data as the 📅 card line. Never raises."""
    try:
        md = str(p.get("match_date") or "")[:10]
        if not md:
            return False
        names = _names(p)
        return any(played_before(n, p) for n in (names if p.get("player") == "match" else names[:1]))
    except Exception as _fe:
        _failnote("played_yesterday", _fe)
        return False


def lines(p):
    """Plain-English signal lines for one board prop. Never raises; [] if nothing known."""
    try:
        L = []
        cat = p.get("category")
        more = str(p.get("direction") or "").upper() == "MORE"
        names = _names(p)
        pair = frozenset(_norm(x) for x in names)
        try:
            line = float(p.get("line"))
        except (TypeError, ValueError):
            line = None
        src, fav = _cached("picks", _pick_rows).get(
            (p.get("match_id"), cat, str(p.get("line")), str(p.get("prop_type") or "").lower()), ("", ""))
        fav = _fav_for(p, fav)   # APEX-PPMIRROR-B-2026-10-23
        _site = "ParlayPlay" if _is_pp(p) else "PrizePicks"

        if cat == "total_games" and more and line is not None:
            m = _cached("pp", _pp_ladder).get((pair, line))
            if m:
                if m <= 1.15:
                    L.append("\U0001F4B5 ParlayPlay pays \U0001F7E2 %.2fx on this line -- the market thinks it's very likely (these hit 88%%)" % m)
                elif m <= 1.30:
                    L.append("\U0001F4B5 ParlayPlay pays \U0001F7E1 %.2fx on this line -- an average Goblin to the market (these hit 71%%)" % m)
                else:
                    L.append("\U0001F4B5 ParlayPlay pays \U0001F534 %.2fx on this line -- the market doesn't trust it (these hit 62%%)" % m)
            bk = _book_for(p, names)   # APEX-175BET365-2026-10-22 / APEX-PPMIRROR-B-2026-10-23
            if bk:
                gap = bk - line
                _bk_who = _book_word(p, names)   # APEX-SGOBACKUP-2026-10-31
                # APEX-BLOWOUT-2026-10-22: records refreshed from the 10/22 study (215 graded 70%+ TG Goblin MORE props since Oct 9)
                if gap >= 3:
                    L.append("\U0001F4CF %s expects about %g games, %s line %g -- %g games of room \u2705 (3+ games of room hit 81%%)" % (_bk_who, bk, _site, line, gap))
                else:
                    L.append("\U0001F4CF %s expects about %g games, %s line %g -- only %g game%s of room \u26a0\ufe0f blowout risk (under 3 games of room hit 57%%, mostly blowouts)" % (
                        _bk_who, bk, _site, line, gap, "" if gap == 1 else "s"))
            else:   # APEX-BET365-THREEPART-2026-10-22: say why, not silence
                L.append("\U0001F4CF bet365: no total for this match")
            try:   # APEX-PINNACLE-2026-10-22: Pinnacle's games of room, next to the bet365 line (display only)
                import apex_pinnacle as _pin
                _pl = _pin.card_line(p)
                if _pl:
                    L.append(_pl)
            except Exception:
                pass
            try:   # APEX-BLOWOUT-2026-10-22: how often each player's recent matches were blowouts (display only)
                import apex_blowout_signal as _bs
                L.extend(_bs.lines(p))
            except Exception:
                pass
            try:
                f = float(fav)
            except (TypeError, ValueError):
                f = None
            if f:   # APEX-FAVLABEL-2026-10-25: same bands as fav_verdict (net_score)
                v = fav_verdict_for(p, f)
                if v == 0 and fav_verdict(f) == -1:   # APEX-FAVROOM-2026-10-31
                    L.append("⚖️ Favorite %d%% to win, but 4+ games of room ✅ (favorites %s with 4+ room went over 17.5 %s since January)" % (
                        round(f * 100), *(("70-75", "333 of 396") if f < 0.75 else ("75-80", "172 of 194"))))
                elif v == 1:
                    L.append("⚖️ Favorite %d%% to win: close match, more games likely ✅ (these hit %d%%)" % (round(f * 100), FAV_HIT_CLOSE))
                elif v == -1:
                    L.append("⚖️ Favorite %d%% to win: strong enough to win fast ⚠️ (these hit %d%%)" % (round(f * 100), FAV_HIT_FAST))
                else:
                    L.append("⚖️ Favorite %d%% to win: big favorite, no edge either way (counted as neutral)" % round(f * 100))

        if cat == "total_games" and not more:   # APEX-PINNACLE-2026-10-22: LESS side gets the Pinnacle line too
            try:
                import apex_pinnacle as _pin
                _pl = _pin.card_line(p)
                if _pl:
                    L.append(_pl)
            except Exception:
                pass

        for _fl, _fv in fantasy_facts(p):   # APEX-FANTASYSIGNALS-2026-10-30
            L.append(_fl)
        for _fl, _fv in aces_facts(p):   # APEX-ACESSIGNALS-2026-10-30
            L.append(_fl)

        if src.startswith(("refresh", "reprice")):
            L.append("\U0001F504 Re-priced with fresh odds closer to the match ✅ (re-priced props hit 82%)")
        elif src:
            L.append("\U0001F550 Still on its first price -- Apex re-checks the odds nearer the start (first-price props hit 68%)")

        md = str(p.get("match_date") or "")[:10]
        if md:
            try:
                y = (date.fromisoformat(md) - timedelta(days=1)).isoformat()
            except ValueError:
                y = None
            if y:
                played = _cached("played", _played)
                who = [(n, played_before(n, p)) for n in (names if p.get("player") == "match" else names[:1])]
                yes = [(n, v) for n, v in who if v]
                if yes and _is_aces_more(p):   # APEX-ACESSIGNALS-2026-10-30: tired legs, fewer aces
                    L.append("\U0001F4C5 Played yesterday: %s \u26a0\ufe0f (Aces MORE after playing yesterday hit 6 of 14)" % ", ".join(
                        "%s (%d sets, %d games)" % (n.split()[-1], v[0], v[1]) for n, v in yes))
                elif yes:
                    L.append("\U0001F4C5 Played yesterday: %s -- already into this event ✅ (these hit 77-80%%)" % ", ".join(
                        "%s (%d sets, %d games)" % (n.split()[-1], v[0], v[1]) for n, v in yes))
        return L
    except Exception:
        return []


# APEX-NETSIGNAL-2026-10-22 (owner, "Go"): add the card verdicts up into one
# net score (good minus bad). Same thresholds as the lines above. Counted: 💵 price
# (<=1.15x good, >1.30x bad), 📏 room (3+ good, under 3 bad), 🏟 recent blowouts
# (<=25% good, >25% bad), ⚖ favorite (see APEX-FAVLABEL-2026-10-25 / fav_verdict),
# 📅 played yesterday (yes good, no neutral). 🔄 re-priced is left OUT (no edge in
# the backtest and the first-fetch card can't know it yet). Missing = neutral.
# Hit rates: _backups/signal_backtest_2026-10-22/report.md, re-counted with exactly
# these rules (no 🔄, close match neutral) on its rows.csv -- Total Games MORE,
# 70%+ playable props Oct 10-22: net +2 or better 126-28 (82%), +1 or less 130-72 (64%).
# (The report's 84%/61% headline counts 🔄 and close-match ✅, so it isn't used here.)
# In-sample; the rerank paper test is the real check.
NET_GOOD_AT = 2            # net +2 or better = ✅
NET_HIT_GOOD = 82          # % hit, net +2 or better (TG MORE, rows.csv re-count)
NET_HIT_REST = 64          # % hit, net +1 or less
# APEX-TOP3NET-2026-10-22 (owner, "Go"): a close match (favorite under 65%)
# counts as good, matching the ⚖ card line's ✅. Re-count on rows.csv with this
# rule (no 🔄), TG MORE: net +2 or better 151-40 (79%), +1 or less 105-60 (64%).
# Undo: False (close match neutral; the 82%/64% numbers above apply again).
CLOSE_MATCH_IS_GOOD = True
NET_HIT_GOOD_CLOSE = 79
NET_HIT_REST_CLOSE = 64
# APEX-BLOWOUTDOUBLE-2026-10-25 (owner, "Go blowout double"): on Total Games MORE a bad 🏟
# (both players' recent blowout share over RECENT_WARN, i.e. 30%+ with 10 matches each) counts as
# 2 bad signal points, so every board that ranks by board_rank_score takes 10 points off, not 5.
# Replay Oct 10-24 (apex_learning_system/reports/2026-10-25_find_a_solution.md): $[amount] vs $[amount]
# better in both Oct 10-18 and Oct 19-24. Undo: BLOWOUT_DOUBLE = False.
BLOWOUT_DOUBLE = True
# APEX-FAVLABEL-2026-10-25 (owner, "I need the label fix with a understand that more clear and
# test ran"): ⚖ favorite on Total Games MORE. Under FAV_FAST_AT = ✅ close match (+1), FAV_FAST_AT to
# FAV_BIG_AT = ⚠️ can win fast (-1), FAV_BIG_AT+ = neutral (0). Deduped Oct 10-24, TG MORE 70%+
# (apex_learning_system/reports/2026-10-25_signal_weights_backtest.md): under 70 hit 124 of 165 (75%),
# 70-80 hit 35 of 60 (58%); 80%+ hit 28 of 35 but has the least bet365 room, so it is small-sample luck
# (2026-10-25_favorite_by_line_type.md). Replaces 80%+ ✅ / 65-80 ⚠️ / under 65 ✅.
# Undo: FAV_FAST_AT = 0.65, FAV_BIG_AT_IS_GOOD = True (the old bands).
FAV_FAST_AT = 0.70
FAV_BIG_AT = 0.80
FAV_BIG_AT_IS_GOOD = False
FAV_HIT_CLOSE = 75
FAV_HIT_FAST = 58

# APEX-FANTASYSIGNALS-2026-10-30 (owner, "Go fantasy signals"): Fantasy Score MORE gets its own
# five facts, from apex_learning_system/reports/2026-10-30_signals_all_prop_types.md (286 graded
# Fantasy MORE picks at 70%+ on the All board, Oct 10-29, 78% overall):
#   📈 form: won 70%+ of last 10 = good (85%); under 50% = bad (70%); between = neutral (74%)
#   ⚖ own win chance 65-75% = bad (66%); 50-65% or underdog = good (81% / 12 of 13); 75%+ = neutral
#   🏟 lost 3+ of last 10 by blowout (17 games or fewer) = bad (48%)
#   📅 played yesterday = good (80% vs 74%), the line every prop already has
# Net: +1 or better 110 of 128 (86%), 0 75 of 95 (79%), -1 or worse 37 of 63 (59%).
# Holdout Oct 21-29: good 89%, zero 76%, bad 65%. Same +5/-5 per net point in the rank score.
# Undo: FANTASY_SIGNALS = False (Fantasy goes back to "only played yesterday").
FANTASY_SIGNALS = True
FANTASY_FORM_GOOD = 0.70
FANTASY_FORM_BAD = 0.50
FANTASY_FAV_LO = 0.65
FANTASY_FAV_HI = 0.75
FANTASY_BLOWLOSS_BAD = 3
FANTASY_GOOD_AT = 1
FANTASY_HIT_GOOD = 86      # net +1 or better
FANTASY_HIT_ZERO = 79
FANTASY_HIT_BAD = 59       # net -1 or worse
SIM_LOG = os.path.join(HERE, "apex_prop_sim_forward_log_v1.csv")


def _is_fantasy_more(p):
    return (FANTASY_SIGNALS and p.get("category") == "fantasy_score" and p.get("player") != "match"
            and str(p.get("direction") or "").upper() == "MORE")


def _sim_fav():
    """{(player, match_date): player's own win chance} from the engine's forward sim log (is_fav + pinn_fav)."""
    out = {}
    since = (date.today() - timedelta(days=3)).isoformat()
    with open(SIM_LOG, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            md = r.get("match_date") or ""
            if md < since or r.get("player") in ("", "match"):
                continue
            try:
                f = float(r.get("pinn_fav"))
                isf = int(r.get("is_fav"))
            except (TypeError, ValueError):
                continue
            if not 0 < f < 1 or isf not in (0, 1):
                continue
            out[(_norm(r.get("player")), md)] = f if isf == 1 else 1.0 - f
    return out


def own_win_chance(p):
    """The prop's player's own chance to win the match (favorite or underdog side). None if unknown."""
    try:
        md = str(p.get("match_date") or "")[:10]
        return _cached("simfav", _sim_fav).get((_norm(p.get("player")), md))
    except Exception as _fe:
        _failnote("own_win_chance", _fe)
        return None


def _form(name, before, n=10):
    """(matches, wins, blowout_losses) over the player's last n completed matches before `before`, or None."""
    try:
        import apex_blowout_signal as _bs
        con = sqlite3.connect("file:%s?mode=ro" % _bs._history(), uri=True)
        rows = con.execute(
            "select total_games, score, player_a, player_b, winner, match_date, retired from matches "
            "where (player_a = ? or player_b = ?) and match_date < ? order by match_date desc limit 60",
            (name, name, before)).fetchall()
        con.close()
        seen, h = set(), []
        for tg, score, a, b, w, md, ret in rows:
            k = (frozenset([a, b]), md)
            if k in seen or ret:
                continue
            seen.add(k)
            tg = tg or _bs._games_from_score(score)
            if not tg or not (12 <= tg <= 45):
                continue
            won = 1 if (w and _norm(w) == _norm(name)) else (0 if w else None)
            h.append((won, int(tg)))
            if len(h) >= n:
                break
        if len(h) < 3:
            return None
        return (len(h), sum(1 for w, _ in h if w), sum(1 for w, g in h if w == 0 and g <= _bs.BLOWOUT_GAMES))
    except Exception as _fe:
        _failnote("_form", _fe)
        return None


def fantasy_facts(p):
    """[(card line, verdict)] for a Fantasy Score MORE prop: 📈 form, ⚖ own win chance, 🏟 blowout losses.
    verdict +1 good / -1 bad / 0 neutral. 📅 played yesterday is added by the caller. Never raises."""
    out = []
    try:
        if not _is_fantasy_more(p):
            return out
        name = str(p.get("player") or "")
        md = str(p.get("match_date") or "")[:10] or date.today().isoformat()
        fm = _cached("form:%s:%s" % (_norm(name), md), lambda: _form(name, md))
        if fm:
            n, wins, bl = fm
            rate = wins / float(n)
            if rate >= FANTASY_FORM_GOOD:
                out.append(("\U0001F4C8 Form: won %d of last %d \u2705 (Fantasy MORE on players winning 70%%+ hit 85%%)" % (wins, n), 1))
            elif rate < FANTASY_FORM_BAD:
                out.append(("\U0001F4C9 Form: won %d of last %d \u26a0\ufe0f (under half won hit 70%%)" % (wins, n), -1))
            else:
                out.append(("\U0001F4C8 Form: won %d of last %d -- middling, counted as neutral (hit 74%%)" % (wins, n), 0))
            if bl >= FANTASY_BLOWLOSS_BAD:
                out.append(("\U0001F3DF\ufe0f Lost %d of last %d by blowout (17 games or fewer) \u26a0\ufe0f (these hit 48%%)" % (bl, n), -1))
            else:
                out.append(("\U0001F3DF\ufe0f Blowout losses: %d of last %d -- fine, counted as neutral" % (bl, n), 0))
        f = own_win_chance(p)
        if f:
            pct = round(f * 100)
            if FANTASY_FAV_LO <= f < FANTASY_FAV_HI:
                out.append(("\u2696\ufe0f %s is %d%% to win: favorite who must do more for the points \u26a0\ufe0f (65-75%% favorites hit 66%%)" % (name, pct), -1))
            elif f >= FANTASY_FAV_HI:
                out.append(("\u2696\ufe0f %s is %d%% to win: big favorite, counted as neutral" % (name, pct), 0))
            elif f >= 0.5:
                out.append(("\u2696\ufe0f %s is %d%% to win: close match, line set low \u2705 (50-65%% favorites hit 81%%)" % (name, pct), 1))
            else:
                out.append(("\u2696\ufe0f %s is %d%% to win: underdog, line set low \u2705 (underdogs hit 12 of 13)" % (name, pct), 1))
    except Exception as _fe:
        _failnote("fantasy_facts", _fe)
        pass
    return out

# APEX-ACESSIGNALS-2026-10-30 (owner, "Go aces signals live"): Aces MORE (player prop) gets its own
# facts, from _backups/signals_othertypes_2026-10-30/report.md (113 graded Aces MORE at 70%+, Oct 9-30, 70%):
#   📈 won 70%+ of last 10 = good (19 of 23); otherwise neutral
#   ⚖ underdog = bad (9 of 25); favorite = neutral (52 of 63)
#   📅 played yesterday = BAD on Aces MORE (6 of 14 vs 73 of 99) -- the opposite of other props
# Net +1 17 of 19 (89%), 0 49 of 62 (79%), -1 or worse 13 of 32 (41%). Edge was big Oct 9-20 and small
# Oct 21-30 (bad 10 of 14) -- he chose live over a paper test. Undo: ACES_SIGNALS = False.
ACES_SIGNALS = True
ACES_FORM_GOOD = 0.70
ACES_HIT_GOOD = 89
ACES_HIT_ZERO = 79
ACES_HIT_BAD = 41


def _is_aces_more(p):
    return (ACES_SIGNALS and p.get("category") == "aces" and p.get("player") != "match"
            and str(p.get("direction") or "").upper() == "MORE")


# APEX-ACESV2-2026-10-31 (owner, "Go aces signals"): more Aces MORE signals, only the favorite one is on, line 0.5 only because that
# is what was tested. Over 0.5, 2026 with odds, women under 4 aces a match: net +1 275-34, 0 1170-232, -1 544-189,
# -2 139-80, -3 36-46; both halves (_backups/aces_test_2026-10-31). Undo: ACES_SIGNALS_V2 = False.
ACES_SIGNALS_V2 = True
ACES_V2_LINE = 0.5
ACES_V2_FAV = (0.60, 0.75)
ACES_V2_LOW_AVG = 1.5
ACES_V2_ZEROS = 5
ACES_V2_N = 10
# owner, "Go remove aces penalties" 11/1: real 0.5 Goblins didn't back clay, low server or zero-ace
# (33-12 vs 76-42, 25-9 vs 84-45, _backups/bigdata_allprops_2026-10-31). The favorite +1 stays. Undo: True.
ACES_V2_PENALTIES = False


def _recent_aces(name, before):
    """Aces in the player's last ACES_V2_N matches before `before`, oldest first, one per date. Warehouse plus settled props."""
    import apex_blowout_signal as _bs
    names = [name]
    by = {}
    try:
        import apex_h2h as _h
        names = _h.resolve(name) or [name]
    except Exception:
        pass
    con = sqlite3.connect("file:%s?mode=ro" % _bs.WAREHOUSE, uri=True)
    try:
        q = ",".join("?" * len(names))
        for a, b, x, y, d, ret in con.execute(
                "select player_a,player_b,player_a_aces,player_b_aces,match_date,retired from matches where (player_a in (%s) or player_b in (%s)) "
                "and match_date<? and player_a_aces is not null and player_b_aces is not null" % (q, q), list(names) + list(names) + [before]):
            if not ret:
                by[str(d)[:10]] = x if a in names else y
    finally:
        con.close()
    con = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True)
    try:
        for d, v in con.execute("select match_date,max(actual) from projections where player in (%s) and category='aces' and actual is not null "
                                "and match_date<? group by match_date" % ",".join("?" * len(names)), list(names) + [before]):
            by.setdefault(str(d)[:10], v)
    finally:
        con.close()
    return [by[k] for k in sorted(by)][-ACES_V2_N:]


def _aces_v2_facts(p, f):
    """Extra (card line, verdict) rows for an Aces MORE at line 0.5. Never raises."""
    out = []
    try:
        if not ACES_SIGNALS_V2 or abs(float(p.get("line")) - ACES_V2_LINE) > 1e-6:
            return out
        name = str(p.get("player") or "")
        md = str(p.get("match_date") or "")[:10] or date.today().isoformat()
        if f and ACES_V2_FAV[0] <= f < ACES_V2_FAV[1]:
            out.append(("\U0001F3AF Favorite 60-75 to win \u2705 (over 0.5 with these, women at 1.5-2.5 aces: 156 of 183)", 1))
        if not ACES_V2_PENALTIES:
            return out
        if str(p.get("surface") or "").strip().lower() == "clay":
            out.append(("\U0001F7E4 Clay \u26a0\ufe0f (fewer aces: women at 1-2.5 aces went 2134 of 3153)", -1))
        h = _cached("aces10:%s:%s" % (_norm(name), md), lambda: _recent_aces(name, md))
        if len(h) >= 5:
            avg = sum(h) / float(len(h))
            zeros = sum(1 for v in h if not v)
            if avg < ACES_V2_LOW_AVG:
                out.append(("\U0001F4C9 Low server: %.1f aces a match, last %d \u26a0\ufe0f" % (avg, len(h)), -1))
            if zeros >= ACES_V2_ZEROS:
                out.append(("\U0001F6AB %d of last %d with no ace \u26a0\ufe0f" % (zeros, len(h)), -1))
    except Exception:
        pass
    return out


def aces_facts(p):
    """[(card line, verdict)] for an Aces MORE prop: 📈 form, ⚖ underdog. 📅 is handled by the caller. Never raises."""
    out = []
    try:
        if not _is_aces_more(p):
            return out
        name = str(p.get("player") or "")
        md = str(p.get("match_date") or "")[:10] or date.today().isoformat()
        fm = _cached("form:%s:%s" % (_norm(name), md), lambda: _form(name, md))
        if fm:
            n, wins, _bl = fm
            if wins / float(n) >= ACES_FORM_GOOD:
                out.append(("\U0001F4C8 Form: won %d of last %d \u2705 (Aces MORE on players winning 70%%+ hit 19 of 23)" % (wins, n), 1))
            else:
                out.append(("\U0001F4C8 Form: won %d of last %d -- counted as neutral" % (wins, n), 0))
        f = own_win_chance(p)
        if f:
            pct = round(f * 100)
            if f < 0.5:
                out.append(("\u2696\ufe0f %s is %d%% to win: underdog, fewer service games \u26a0\ufe0f (Aces MORE on underdogs hit 9 of 25)" % (name, pct), -1))
            else:
                out.append(("\u2696\ufe0f %s is %d%% to win: favorite, counted as neutral (favorites hit 52 of 63)" % (name, pct), 0))
            out += _aces_v2_facts(p, f)
    except Exception:
        pass
    return out


# APEX-FAVROOM-2026-10-31 (owner, "Go fix favorite room"): a 70-80 favorite costs no signal point on a
# Total Games MORE with 4+ games of room. 5,926 matches 1/17-10/31, over 17.5 with favorite 75-80: 4+ room 172 won,
# 22 lost; under 3 room 94 won, 45 lost (_backups/favorite_signal_test_2026-10-31). Undo: FAV_ROOM_RULE = False.
FAV_ROOM_RULE = True
FAV_ROOM_MIN = 4


def fav_verdict_for(p, f, names=None):
    """fav_verdict with the room rule, for a prop. Never raises."""
    v = fav_verdict(f)
    try:
        if v == -1 and FAV_ROOM_RULE:
            bk = _book_for(p, names if names is not None else _names(p))
            if bk and float(bk) - float(p.get("line")) >= FAV_ROOM_MIN:
                return 0
    except Exception:
        pass
    return v


def fav_verdict(f):
    """APEX-FAVLABEL-2026-10-25: +1 close match, -1 strong enough to win fast, 0 big favorite (neutral)."""
    if f >= FAV_BIG_AT:
        return 1 if FAV_BIG_AT_IS_GOOD else 0
    if f >= FAV_FAST_AT:
        return -1
    return 1 if CLOSE_MATCH_IS_GOOD else 0


def net_score(p):
    """(good, bad, net, available) from the card signal verdicts. Never raises."""
    try:
        good = bad = 0
        cat = p.get("category")
        tg_more = cat == "total_games" and str(p.get("direction") or "").upper() == "MORE"
        names = _names(p)
        pair = frozenset(_norm(x) for x in names)
        available = 1   # 📅 applies to every prop
        try:
            line = float(p.get("line"))
        except (TypeError, ValueError):
            line = None
        if tg_more and line is not None:
            available = 5
            m = _cached("pp", _pp_ladder).get((pair, line))
            if m:
                good += m <= 1.15
                bad += m > 1.30
            bk = _book_for(p, names)   # APEX-175BET365-2026-10-22 / APEX-PPMIRROR-B-2026-10-23
            if bk:
                if bk - line >= 3:
                    good += 1
                else:
                    bad += 1
            try:
                import apex_blowout_signal as _bs
                rb = _bs.recent_blowouts(p)
                if len(rb) == 2:
                    sh = sum(b / float(n) for _, b, n in rb) / 2.0
                    if sh > _bs.RECENT_WARN:
                        bad += 2 if BLOWOUT_DOUBLE else 1   # APEX-BLOWOUTDOUBLE-2026-10-25
                    else:
                        good += 1
            except Exception as _fe:
                _failnote("net_score", _fe)
                pass
            _src, fav = _cached("picks", _pick_rows).get(
                (p.get("match_id"), cat, str(p.get("line")), str(p.get("prop_type") or "").lower()), ("", ""))
            fav = _fav_for(p, fav)   # APEX-PPMIRROR-B-2026-10-23
            try:
                f = float(fav)
            except (TypeError, ValueError):
                f = None
            if f:   # APEX-FAVLABEL-2026-10-25
                v = fav_verdict_for(p, f, names)
                good += v == 1
                bad += v == -1
        if _is_aces_more(p):   # APEX-ACESSIGNALS-2026-10-30: form, underdog (+ 📅 below counts BAD)
            available = 3
            for _fl, _fv in aces_facts(p):
                good += _fv == 1
                bad += _fv == -1
        if _is_fantasy_more(p):   # APEX-FANTASYSIGNALS-2026-10-30: form, own win chance, blowout losses (+ 📅 below)
            available = 4
            for _fl, _fv in fantasy_facts(p):
                good += _fv == 1
                bad += _fv == -1
        md = str(p.get("match_date") or "")[:10]
        if md:
            try:
                y = (date.fromisoformat(md) - timedelta(days=1)).isoformat()
                played = _cached("played", _played)
                if any(played_before(n, p) for n in (names if p.get("player") == "match" else names[:1])):
                    if _is_aces_more(p):   # APEX-ACESSIGNALS-2026-10-30
                        bad += 1
                    else:
                        good += 1
            except ValueError:
                pass
        return int(good), int(bad), int(good - bad), available
    except Exception as _fe:
        _failnote("net_score", _fe)
        return 0, 0, 0, 0


def blowout_doubled(p):
    """APEX-BLOWOUTDOUBLE-2026-10-25: True when this prop's bad 🏟 counts as 2 bad signal points. Never raises."""
    try:
        if not BLOWOUT_DOUBLE or p.get("category") != "total_games" or str(p.get("direction") or "").upper() != "MORE":
            return False
        import apex_blowout_signal as _bs
        sh = _bs.blowout_share(p)
        return sh is not None and sh > _bs.RECENT_WARN
    except Exception:
        return False


def net_line(p):
    """The 🧮 Signals card line, in plain words. Never raises; '' if nothing can be counted."""
    try:
        good, bad, net, available = net_score(p)
        if not available:
            return ""
        if _is_aces_more(p):   # APEX-ACESSIGNALS-2026-10-30
            if net >= 1:
                return "\U0001F9EE Signal count: %d good, %d bad = %+d \u2705 strong (Aces MORE picks at +1 or more hit %d%%; 0 hit %d%%; -1 or less hit %d%%)" % (
                    good, bad, net, ACES_HIT_GOOD, ACES_HIT_ZERO, ACES_HIT_BAD)
            if net == 0:
                return "\U0001F9EE Signal count: %d good, %d bad = 0 (Aces MORE picks at 0 hit %d%%; +1 or more hit %d%%; -1 or less hit %d%%)" % (
                    good, bad, ACES_HIT_ZERO, ACES_HIT_GOOD, ACES_HIT_BAD)
            return "\U0001F9EE Signal count: %d good, %d bad = %+d \u26a0\ufe0f weak (Aces MORE picks at -1 or less hit %d%%; 0 hit %d%%; +1 or more hit %d%%)" % (
                good, bad, net, ACES_HIT_BAD, ACES_HIT_ZERO, ACES_HIT_GOOD)
        if _is_fantasy_more(p):   # APEX-FANTASYSIGNALS-2026-10-30
            if net >= FANTASY_GOOD_AT:
                return "\U0001F9EE Signal count: %d good, %d bad = %+d \u2705 strong (Fantasy MORE picks at +1 or more hit %d%%; 0 hit %d%%; -1 or less hit %d%%)" % (
                    good, bad, net, FANTASY_HIT_GOOD, FANTASY_HIT_ZERO, FANTASY_HIT_BAD)
            if net == 0:
                return "\U0001F9EE Signal count: %d good, %d bad = 0 (Fantasy MORE picks at 0 hit %d%%; +1 or more hit %d%%; -1 or less hit %d%%)" % (
                    good, bad, FANTASY_HIT_ZERO, FANTASY_HIT_GOOD, FANTASY_HIT_BAD)
            return "\U0001F9EE Signal count: %d good, %d bad = %+d \u26a0\ufe0f weak (Fantasy MORE picks at -1 or less hit %d%%; 0 hit %d%%; +1 or more hit %d%%)" % (
                good, bad, net, FANTASY_HIT_BAD, FANTASY_HIT_ZERO, FANTASY_HIT_GOOD)
        if available < 2:   # only 📅 can be checked outside Total Games MORE
            return ("\U0001F9EE Signal count: %d good, %d bad = %+d (only \"played yesterday\" can be "
                    "checked on this prop type)" % (good, bad, net))
        hi, lo = (NET_HIT_GOOD_CLOSE, NET_HIT_REST_CLOSE) if CLOSE_MATCH_IS_GOOD else (NET_HIT_GOOD, NET_HIT_REST)
        _dbl = " (\U0001F3DF\ufe0f blowout risk counts as 2 bad)" if blowout_doubled(p) else ""   # APEX-BLOWOUTDOUBLE-2026-10-25
        if net >= NET_GOOD_AT:
            return "\U0001F9EE Signal count: %d good, %d bad%s = %+d \u2705 strong (props at +%d or more hit %d%%; +%d or less hit %d%%)" % (
                good, bad, _dbl, net, NET_GOOD_AT, hi, NET_GOOD_AT - 1, lo)
        return "\U0001F9EE Signal count: %d good, %d bad%s = %+d \u26a0\ufe0f weak (props at +%d or less hit %d%%; +%d or more hit %d%%)" % (
            good, bad, _dbl, net, NET_GOOD_AT - 1, lo, NET_GOOD_AT, hi)
    except Exception:
        return ""


# APEX-LINE175CHECK-2026-10-22 (owner: "Go"): the 🎯 17.5 check on every over-17.5 Total Games
# card. Same rule and same data as the Top 3 tracker's rule arm (apex_top3_no175_papertest.rule175).
# Hit rates from _backups/signal_backtest_2026-10-22/line175_report.md (100 graded over-17.5
# Goblins at 70%+, Sep 30 - Oct 21): rule keeps 33-9 (79%), rule skips 35-23 (60%). DISPLAY ONLY.
LINE175_HIT_PLAY = 79
LINE175_HIT_SKIP = 60


def line175_check(p):
    """The 🎯 17.5 check card line. Never raises; '' unless Total Games MORE at line 17.5."""
    try:
        if p.get("category") != "total_games" or str(p.get("direction") or "").upper() != "MORE":
            return ""
        # APEX-PPMIRROR-B-2026-10-23: ParlayPlay offers every rung (16.5, 17.5, 18.5 ...); its picks are
        # checked on every Total Games MORE line of 17.5 or more, at their own line. PrizePicks: 17.5 only.
        _ln = float(p.get("line"))
        if (_ln < 17.5) if _is_pp(p) else (_ln != 17.5):
            return ""
        import apex_top3_no175_papertest as _t
        v, _gap, _room = _t.rule175(_names(p), str(p.get("match_date") or "")[:10], _ln,
                                    book=(_book_for(p, _names(p)) if _is_pp(p) else None))
        if v == "close":
            return "\U0001F3AF 17.5 check: players close \u2705 play it (these hit %d%%)" % LINE175_HIT_PLAY
        if v == "room":
            return "\U0001F3AF 17.5 check: %d+ games of room \u2705 play it (these hit %d%%)" % (_t.ROOM_MIN, LINE175_HIT_PLAY)
        if v == "skip":
            return "\U0001F3AF 17.5 check: mismatch and under %d games of room \u274c skip (these hit %d%%)" % (_t.ROOM_MIN, LINE175_HIT_SKIP)
        # APEX-BET365-THREEPART-2026-10-22: keep "not enough data yet" (the 17.5 rank push-down reads it),
        # and name the missing piece when it is bet365's total
        return "\U0001F3AF 17.5 check: not enough data yet" + (" (\U0001F4CF bet365: no total for this match)" if _room is None else "")
    except Exception:
        return ""


if __name__ == "__main__":
    import apex_edge_tracker as t
    picks, _ = t.load_current_board()
    for p in picks[:400]:
        if t._board_prob(p) >= 0.75 and p.get("category") == "total_games":
            print(p.get("opponent"), p.get("line"), p.get("prop_type"))
            for x in lines(p):
                print("   " + x)
