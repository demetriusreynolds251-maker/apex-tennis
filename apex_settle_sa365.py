#!/usr/bin/env python3
"""APEX-SETTLE-SA365-2026-09-23 -- settle open sim-forward-log rows from SportsAPI365.
Replaces apex_settle_v8.py. Same file, same two columns ('actual', 'settled_ts'), same commands:
  --check (default) | --apply [--since YYYY-MM-DD]
  --validate [--since YYYY-MM-DD]   re-derive already-settled rows and compare; writes nothing
  --emit-hand [--apply]             settled sim rows -> hand CSV for ALIS (same as before)
Rules: games/sets/tiebreaks from the final score; aces/dfs from the stat sheet as given;
breaks only when the sheet's break counts pass the serve arithmetic. Anything missing,
unfinished, retired, walkover, or failing a check is HELD and reported -- never zero-filled."""
import csv, re, os, sys, json, time, shutil, unicodedata, collections
import datetime as dt, urllib.request, urllib.parse, urllib.error
from pathlib import Path

LOG = "apex_prop_sim_forward_log_v1.csv"
HAND = "apex_engine_hand_settled_v1.csv"
PICKS = "apex_engine_picks_log_v1.csv"
SOURCE = "settle_sa365"
FANTASY_BASE = 10.0   # 10 + (GW-GL) + 3*(SW-SL) + 0.5*(aces-DFs)  (verified PrizePicks chart)
BASE = "https://api.sportsapi365.com/v1/tennis"
KEY = (Path(__file__).resolve().parent / "sportapi365_key.txt").read_text().strip()
UA = "Mozilla/5.0 (ApexSettle)"   # Cloudflare blocks the default Python name tag (error 1010)

def api(path, tries=3):
    """GET -> (payload|None, error|None). Finite retries on 429/5xx only."""
    err = None
    for i in range(tries):
        req = urllib.request.Request(BASE + path, headers={
            "X-Gravitee-Api-Key": KEY, "Accept": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode()), None
        except urllib.error.HTTPError as e:
            err = "HTTP %s" % e.code
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, str(e)[:80])
        time.sleep(2 * (i + 1))
    return None, err

def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().replace(".", "").replace("'", "").replace("-", " ").split())

def same_player(a, b):
    A, B = _norm(a), _norm(b)
    if not A or not B:
        return False
    return A == B or A in B or B in A or (A.split()[-1] == B.split()[-1] and A[0] == B[0])

_ev_cache, _st_cache = {}, {}

_WH_NAMES = {}


def _wh_names():
    """APEX-NAMEFIX-2026-11-01 (owner, "Fix all"): {(first, last): full warehouse names with a middle name}."""
    if not _WH_NAMES:
        import sqlite3
        _WH_NAMES["_"] = 1
        try:
            path = Path(__file__).resolve().parent / "data" / "warehouse" / "tennis_warehouse.sqlite"
            con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
            for (nm,) in con.execute("select distinct player_a from matches where match_date >= '2025-01-01' "
                                     "union select distinct player_b from matches where match_date >= '2025-01-01'"):
                t = str(nm or "").split()
                if len(t) >= 3:
                    _WH_NAMES.setdefault((_norm(t[0]), _norm(t[-1])), set()).add(nm)
            con.close()
        except Exception:
            pass
    return _WH_NAMES


# APEX-TENNISSETTLE-2026-11-15: SportsAPI365 looks players up by full legal name. Short names it misses -> the names it finds.
ALIASES = {"taylor fritz": ["Taylor Harry Fritz"], "coco gauff": ["Cori Gauff"]}


def _variants(n):
    out = [n, n.replace("-", " ")] if "-" in n else [n]
    out += [a for a in ALIASES.get(_norm(n), []) if a not in out]
    t = str(n or "").split()
    if len(t) == 2:   # Leylah Fernandez is Leylah Annie Fernandez at SportsAPI365
        out += sorted(_wh_names().get((_norm(t[0]), _norm(t[1])), ()))
    return out

def find_event(player, opponent, match_date):
    """Names + date lookup; both name orders, +-1 and +-2 days. -> (event, side, note)."""
    key = (_norm(player), _norm(opponent), match_date)
    if key in _ev_cache:
        return _ev_cache[key]
    try:
        base = dt.date.fromisoformat(match_date[:10])
    except Exception:
        _ev_cache[key] = (None, None, "bad match_date"); return _ev_cache[key]
    pairs = [(x, y) for x in _variants(player) for y in _variants(opponent)]
    pairs += [(y, x) for x, y in pairs]
    last_err = ""
    hits, seen_ids = [], set()
    for n in (0, -1, 1, -2, 2):
        d = (base + dt.timedelta(days=n)).isoformat()
        for x, y in pairs:
            p, err = api("/extend/api/event/get/%s/%s/%s" % (urllib.parse.quote(x), urllib.parse.quote(y), d))
            time.sleep(0.25)
            if err:
                last_err = err
            ev = (p or {}).get("result") or {}
            if not ev.get("id") or ev["id"] in seen_ids:
                continue
            if same_player(player, ev.get("participant1")):
                side = 1
            elif same_player(player, ev.get("participant2")):
                side = 2
            else:
                continue
            seen_ids.add(ev["id"])
            note = "found %s%s" % (d, "" if n == 0 else " (%+dd)" % n)
            if str(ev.get("status") or "").strip().lower() == "ended":
                out = (ev, side, note); _ev_cache[key] = out; return out
            hits.append((ev, side, note))
    if hits:   # APEX-SETTLE-SA365-PREFER-ENDED: no finished record -- report a non-cancelled one first
        keep = [h for h in hits if "cancel" not in str(h[0].get("status") or "").lower()]
        out = (keep or hits)[0]; _ev_cache[key] = out; return out
    got = _from_book(player, opponent, base)   # APEX-TENNISSETTLE-2026-11-15: the match id we saw while pricing, finished score from the warehouse
    if got:
        _ev_cache[key] = got; return got
    out = (None, None, "not found by names+date (+-2 days, both orders)" + (" last error %s" % last_err if last_err else ""))
    _ev_cache[key] = out; return out


def _from_book(player, opponent, base):
    """APEX-TENNISSETTLE-2026-11-15: find the match in data/sa365_event_book.json (ids seen by the price feed) when the name lookup
    misses. Status and score come from the match warehouse final; the stat sheet is read by id. -> (event, side, note) or None."""
    try:
        book = json.load(open(Path(__file__).resolve().parent / "data" / "sa365_event_book.json"))
    except Exception:
        return None
    for e in book.values():
        try:
            d = dt.datetime.utcfromtimestamp(int(e.get("startTimestamp"))).date()
        except Exception:
            continue
        if abs((d - base).days) > 2:
            continue
        a, b = e.get("participant1"), e.get("participant2")
        if same_player(player, a) and same_player(opponent, b):
            side = 1
        elif same_player(player, b) and same_player(opponent, a):
            side = 2
        else:
            continue
        ev = dict(e)
        wh = _warehouse_final(ev)
        if wh == "RET":
            ev.update(status="Retired", score="")
        elif wh:
            ev.update(status="Ended", score=wh)
        else:
            ev.update(status="Not Started (no final score yet)", score="")
        return ev, side, "found by match id %s (%s)" % (e.get("id"), d.isoformat())
    return None

def parse_sets(score):
    return [(int(a), int(b)) for a, b in re.findall(r"(\d+)\s*-\s*(\d+)", str(score or ""))]

def set_complete(a, b):
    hi, lo = max(a, b), min(a, b)
    return (hi == 6 and lo <= 4) or (hi == 7 and lo in (5, 6))

def match_flag(ev, best_of="3"):
    st = str(ev.get("status") or "").strip().lower()
    if "walk" in st or "w/o" in st:
        return "WO"
    if "ret" in st:
        return "RET"
    if "cancel" in st:
        return "CANCELLED"
    if st != "ended":
        # APEX-TENNISSETTLE-2026-11-15: SportsAPI365 can leave a walkover or a moved match on "Not Started" for days. 12h+ after the
        # start, the match warehouse decides: walkover/retirement -> void, a final score -> settle with it, nothing -> keep waiting.
        try:
            old = (dt.datetime.utcnow() - dt.datetime.utcfromtimestamp(int(ev.get("startTimestamp")))).total_seconds() > 12 * 3600
        except Exception:
            old = False
        if old:
            if "_wh" not in ev:
                ev["_wh"] = _warehouse_final(ev)
            wh = ev["_wh"]
            if wh == "RET":
                return "WO"
            if wh:
                ev["status"], ev["score"] = "Ended", wh
                return match_flag(ev, best_of)
        return "NOT_FINISHED(%s)" % (ev.get("status") or "?")
    sets = parse_sets(ev.get("score"))
    if not sets:
        return "NO_SCORE"
    if any(max(a, b) >= 10 for a, b in sets):
        return "MATCH_TIEBREAK"
    # APEX-VOIDFIX-2026-11-01 (owner, "Fix all"): SA365 can leave a finished match on a live-looking
    # score (Fils v Struff 4-6,7-6,6-6). An unfinished score is only a retirement if the warehouse says so;
    # a warehouse final replaces it; no warehouse row yet means hold, never void.
    s1 = sum(a > b for a, b in sets)
    if not all(set_complete(a, b) for a, b in sets) or max(s1, len(sets) - s1) < (3 if str(best_of) == "5" else 2):
        wh = _warehouse_final(ev)
        if wh is None:
            return "SCORE_UNFINISHED"
        if wh == "RET":
            return "RET"
        ev["score"] = wh
        return match_flag(ev, best_of)
    return ""


def _warehouse_final(ev):
    """Final score oriented to the event's players, 'RET' for a retirement or walkover, None if not there yet."""
    try:
        import sqlite3
        a, b = str(ev.get("participant1") or ""), str(ev.get("participant2") or "")
        d = dt.datetime.utcfromtimestamp(int(ev.get("startTimestamp"))).date()
        la, lb = _norm(a).split()[-1], _norm(b).split()[-1]
        path = Path(__file__).resolve().parent / "data" / "warehouse" / "tennis_warehouse.sqlite"
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        rows = con.execute("select player_a, player_b, score, retired from matches where match_date between ? and ? "
                           "and ((player_a like ? and player_b like ?) or (player_a like ? and player_b like ?))",
                           ((d - dt.timedelta(days=2)).isoformat(), (d + dt.timedelta(days=2)).isoformat(),
                            "%" + la + "%", "%" + lb + "%", "%" + lb + "%", "%" + la + "%")).fetchall()
        con.close()
        for pa, pb, sc, ret in rows:
            flip = same_player(pa, b) and same_player(pb, a)
            if not flip and not (same_player(pa, a) and same_player(pb, b)):
                continue
            low = str(sc or "").lower()
            if ret or not sc or "ret" in low or "w/o" in low or "walk" in low or "def" in low:
                return "RET"
            sets = re.findall(r"(\d+)-(\d+)", re.sub(r"\(\d+\)", "", sc))
            if not sets:
                return None
            return ",".join(("%s-%s" % (y, x)) if flip else ("%s-%s" % (x, y)) for x, y in sets)
    except Exception:
        pass
    return None

SHEETS = Path(__file__).resolve().parent / "data" / "sa365_sheets"   # APEX-SETTLE-SA365-ARCHIVE

def stat_sheet(ev):
    eid = ev["id"]
    if eid not in _st_cache:
        f = SHEETS / ("%s.json" % eid)
        if f.exists():
            try:
                _st_cache[eid] = (json.loads(f.read_text()), None)
                return _st_cache[eid]
            except Exception:
                pass
        p, err = api("/extend/api/event/get/statistics/%s" % eid); time.sleep(0.25)
        res = ((p or {}).get("result") or {})
        if (res.get("statistics") or {}).get("ALL"):
            try:
                SHEETS.mkdir(parents=True, exist_ok=True)
                keep = dict(res)
                keep["_event"] = {k: ev.get(k) for k in ("id", "name", "participant1", "participant2", "league", "score", "status")}
                keep["_saved"] = dt.datetime.now().isoformat(timespec="seconds")
                f.write_text(json.dumps(keep))
            except Exception:
                pass
        _st_cache[eid] = (res, err)
    return _st_cache[eid]

def sv(sec, grp, key, who):
    try:
        return sec[grp][key]["player%d" % who]["value"]
    except Exception:
        return None

def serve_ok(g1, g2, b1, b2, tb1, tb2, allowance):
    """games won = holds + breaks + tiebreaks; serve counts can differ by at most `allowance`."""
    h1, h2 = g1 - b1 - tb1, g2 - b2 - tb2
    if h1 < 0 or h2 < 0:
        return False
    return abs((h1 + b2) - (h2 + b1)) <= allowance

def value_for(cat, ev, side, best_of="3"):
    """-> (value|None, flag, note)"""
    flag = match_flag(ev, best_of)
    if flag:
        return None, flag, "match flag %s" % flag
    sets = parse_sets(ev.get("score"))
    g1, g2 = sum(a for a, _ in sets), sum(b for _, b in sets)
    s1 = sum(a > b for a, b in sets); s2 = len(sets) - s1
    tb1 = sum((a, b) == (7, 6) for a, b in sets); tb2 = sum((a, b) == (6, 7) for a, b in sets)
    gw, gl = (g1, g2) if side == 1 else (g2, g1)
    sw, sl = (s1, s2) if side == 1 else (s2, s1)
    if cat == "games_won":    return float(gw), "", ""
    if cat == "total_games":  return float(g1 + g2), "", ""
    if cat == "total_sets":   return float(s1 + s2), "", ""
    if cat == "tiebreaks":    return float(tb1 + tb2), "", ""
    if cat == "any_tiebreak": return float(tb1 + tb2 > 0), "", ""
    if cat == "goes_3_sets":  return float(s1 + s2 == 3), "", ""
    if cat == "goes_5_sets":  return float(s1 + s2 == 5), "", ""
    if cat not in ("aces", "dfs", "double_faults", "breaks", "fantasy_score"):
        return None, "", "unsupported category"
    res, err = stat_sheet(ev)
    ALL = (res.get("statistics") or {}).get("ALL")
    if not ALL:
        return None, "", "no stat sheet" + (" (%s)" % err if err else "")
    if same_player(res.get("player1"), ev.get("participant1")) and same_player(res.get("player2"), ev.get("participant2")):
        flip = False
    elif same_player(res.get("player1"), ev.get("participant2")) and same_player(res.get("player2"), ev.get("participant1")):
        flip = True
    else:
        return None, "", "stat sheet names don't match the match record"
    me = (3 - side) if flip else side
    sw1, sw2 = sv(ALL, "games", "gamesWon", 1), sv(ALL, "games", "gamesWon", 2)
    if None not in (sw1, sw2):
        rec = (sw2, sw1) if flip else (sw1, sw2)
        if rec != (g1, g2):
            return None, "", "stat sheet games %s-%s disagree with score" % rec
    a, d = sv(ALL, "service", "aces", me), sv(ALL, "service", "doubleFaults", me)
    if cat == "aces":
        return (None, "", "aces missing") if a is None else (float(a), "", "")
    if cat in ("dfs", "double_faults"):
        return (None, "", "double faults missing") if d is None else (float(d), "", "")
    if cat == "fantasy_score":
        if a is None or d is None:
            return None, "", "aces/dfs missing for fantasy"
        return FANTASY_BASE + (gw - gl) + 3 * (sw - sl) + 0.5 * (float(a) - float(d)), "", ""
    bs = (sv(ALL, "return", "breakPointsScored", 1), sv(ALL, "return", "breakPointsScored", 2))
    if None in bs:
        return None, "", "break counts missing"
    b1, b2 = (bs[1], bs[0]) if flip else bs
    if not serve_ok(g1, g2, b1, b2, tb1, tb2, 1 + tb1 + tb2):
        return None, "", "break counts fail the serve check"
    return float(b1 if side == 1 else b2), "", ""

def target(r):
    if r["player"] == "match":
        pair = re.split(r"\s+v\s+|\s+vs\s+", r["opponent"])
        return (pair[0].strip(), pair[1].strip()) if len(pair) == 2 else (None, None)
    return r["player"], r["opponent"]

def settle_one(r):
    """-> ((value, score, note), None) or (None, reason)"""
    player, opponent = target(r)
    if not player:
        return None, "bad match pair"
    ev, side, note = find_event(player, opponent, r["match_date"])
    if ev is None:
        return None, "NO MATCH: " + note
    val, flag, why = value_for(r["category"], ev, side, r.get("best_of") or "3")
    if flag in ("RET", "WO", "CANCELLED"):
        return None, "VOID needed (%s) score %s" % (flag, ev.get("score"))
    if val is None:
        return None, "HELD: " + why
    return (val, ev.get("score"), note), None

def _who(r):
    return (r["opponent"] if r["player"] == "match" else r["player"])[:30]

def main():
    mode = "--apply" if "--apply" in sys.argv else "--check"
    since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else "2026-08-01"
    today = dt.date.today().isoformat()
    rows = list(csv.DictReader(open(LOG, newline=""))); flds = list(rows[0].keys())
    open_rows = [r for r in rows if not str(r.get("actual", "")).strip()
                 and since <= r.get("match_date", "") < today
                 or (not str(r.get("actual", "")).strip() and "--include-today" in sys.argv
                     and r.get("match_date", "") == today)]   # APEX-FASTSETTLE-2026-10-19: hourly run settles today's finished matches too (unfinished ones are HELD)
    print(f"log rows {len(rows)} | open (since {since}, before {today}) {len(open_rows)}")
    settled, held = [], []
    for r in open_rows:
        got, msg = settle_one(r)
        if got is None:
            held.append((r, msg)); continue
        val, score, note = got
        r["_new_actual"] = ("%.1f" if r["category"] == "fantasy_score" else "%.0f") % val
        settled.append((r, score, note))
    # APEX-SETTLEHOLDS-2026-10-09: write what the settler could NOT settle and why, so the
    # daily health report can tell a legitimate void (retired/cancelled/walkover) from a
    # real gap. Report-only sidecar; the forward log is not touched here.
    try:
        _holds = [{"player": r.get("player"), "opponent": r.get("opponent"), "category": r.get("category"),
                   "line": r.get("line"), "match_date": r.get("match_date"),
                   "reason": ("void" if str(m).startswith("VOID") else "no_match" if str(m).startswith("NO MATCH") else "held"),
                   "detail": str(m)[:120]} for r, m in held]
        with open("apex_settle_holds.json", "w") as _hf:
            json.dump({"generated": dt.datetime.now().isoformat(timespec="seconds"), "since": since, "holds": _holds}, _hf)
    except Exception as _e:
        print("holds sidecar not written:", _e)
    kinds = collections.Counter(m.split(":")[0].split(" (")[0] for _, m in held)
    print(f"\nwould settle {len(settled)}; held {len(held)}: " + ", ".join(f"{k}={v}" for k, v in kinds.items()))
    for r, sc, note in settled[:60]:
        print(f"  {r['match_date']} {_who(r):30s} {r['category']:13s} line {r['line']:>5s} -> actual {r['_new_actual']:>5s}  [{sc}] {note}")
    for r, m in held[:40]:
        print(f"  ! {r['match_date']} {_who(r):30s} {r['category']:13s} {m}")
    if mode != "--apply" or not settled:
        print("\n(check only -- nothing written)" if mode != "--apply" else "\nnothing to write"); return
    bak = LOG + ".bak_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_presettlesa365"
    shutil.copy2(LOG, bak)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    for r, _, _ in settled:
        r["actual"] = r.pop("_new_actual"); r["settled_ts"] = ts
    for r in rows:
        r.pop("_new_actual", None)
    tmp = LOG + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=flds); w.writeheader(); w.writerows(rows)
    os.replace(tmp, LOG)
    after = sum(1 for r in csv.DictReader(open(LOG, newline="")) if r.get("actual"))
    print(f"\nAPPLIED: wrote {len(settled)} actuals. settled now {after} of {len(rows)}. backup {bak}")

def validate():
    since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else "2026-09-01"
    rows = [r for r in csv.DictReader(open(LOG, newline=""))
            if str(r.get("actual", "")).strip() and r.get("match_date", "") >= since]
    print(f"validating {len(rows)} already-settled rows since {since} (writes nothing)")
    agree, held, differ = collections.Counter(), collections.Counter(), []
    for r in rows:
        got, msg = settle_one(r)
        if got is None:
            held[msg.split(":")[0].split(" (")[0]] += 1; continue
        try:
            old = float(r["actual"])
        except ValueError:
            held["old actual not a number"] += 1; continue
        if abs(got[0] - old) < 0.01:
            agree[r["category"]] += 1
        else:
            differ.append((r["match_date"], _who(r), r["category"], r["actual"], got[0], got[1]))
    print("AGREE:", dict(agree), "| total", sum(agree.values()))
    print("COULD NOT RE-GRADE:", dict(held))
    print(f"DIFFER ({len(differ)}):")
    for d in differ:
        print("  ", d)

def emit_hand(apply=False):
    """Join settled sim rows to picks (direction/prob/tier/best_of) and append as hand-CSV rows
    so ALIS can count them. in_forward_log=1 keeps the tracker from double-counting.
    Dedupe key: (match_date, player, category, line)."""
    sim = [r for r in csv.DictReader(open(LOG, newline="")) if str(r.get("actual", "")).strip()]
    picks = list(csv.DictReader(open(PICKS, newline="")))
    hand = list(csv.DictReader(open(HAND, newline="")))
    hflds = list(hand[0].keys())
    have = {(h["match_date"], h["player"].strip().lower(), h["category"], str(float(h["line"])) if h["line"] else "") for h in hand}
    pk = {}
    for p in picks:
        pk[(p["match_id"], p["player"].strip().lower(), p["category"], str(float(p["line"])) if p["line"] else "")] = p
    new, nopick = [], 0
    for s in sim:
        key = (s["match_id"], s["player"].strip().lower(), s["category"], str(float(s["line"])) if s["line"] else "")
        p = pk.get(key)
        if not p:
            nopick += 1; continue
        hk = (p["match_date"], p["player"].strip().lower(), p["category"], key[3])
        if hk in have:
            continue
        have.add(hk)
        new.append({"match_date": p["match_date"], "player": p["player"], "opponent": p["opponent"],
                    "category": p["category"], "tier": p.get("tier", ""), "line": p["line"],
                    "direction": p["direction"], "prob": p["prob"], "actual": s["actual"],
                    "best_of": p.get("best_of") or s.get("best_of") or "3", "in_forward_log": "1",
                    "source": SOURCE, "note": "", "prop_type": p.get("prop_type", "")})
    print(f"settled sim rows {len(sim)} | no matching pick {nopick} | already in hand {len(sim) - nopick - len(new)} | NEW hand rows {len(new)}")
    for n in new[:8]:
        print("  ", n["match_date"], n["player"][:18], n["category"], n["line"], n["direction"], "->", n["actual"])
    if not apply or not new:
        print("(check only)" if not apply else "nothing to add"); return
    shutil.copy2(HAND, HAND + ".bak_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_preemithand")
    with open(HAND, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=hflds); w.writerows(new)
    print(f"APPENDED {len(new)} rows -> hand CSV now {len(hand) + len(new)} rows")

if __name__ == "__main__":
    if "--validate" in sys.argv:
        validate()
    elif "--emit-hand" in sys.argv:
        emit_hand(apply="--apply" in sys.argv)
    else:
        main()
