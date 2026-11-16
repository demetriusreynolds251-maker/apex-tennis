#!/usr/bin/env python3
"""
APEX-FRESHSTATE-2026-10-20 (owner, "Go all" 2026-10-20): the player file
(apex_prop_state_v1.pkl) is rebuilt every morning from the TennisMyLife match
files, which lag new tournaments by a week or more (on 10/20 the newest WTA event
was Wuhan -- no Ningbo, Osaka, Brussels, Almaty). Alycia Parks' 12-DF match on
10/18 never reached her serve history before her 10/20 DF LESS 8.5 prop.

This top-up adds the matches we already hold as SA365 box scores
(data/sa365_sheets/*.json, saved by settlement) that TennisMyLife does not have
yet, to the serve history the engine reads (aces, DFs, serve points, service
games, break points -- the same nine numbers the replay stores per match).

Rules:
  - only finished tour-level matches: the event's city must appear in a
    TennisMyLife ATP/WTA year file (2025/2026); WTA 125s, Challengers and ITFs
    stay out, same as the replay.
  - a match TennisMyLife already has (same two players, within 16 days of its
    tourney week) is skipped, so nothing is counted twice when it catches up.
  - always applied to the BASE file the morning rebuild wrote
    (apex_prop_state_v1_base.pkl), never on top of an earlier top-up, so running
    it hourly is safe.
  - Elo, match count and 'latest_date' are untouched (the health check compares
    latest_date with the data files); last match date / surface are moved forward.

Usage:
  python3 apex_state_topup.py            # apply (writes apex_prop_state_v1.pkl)
  python3 apex_state_topup.py --dry-run  # show what would be added, write nothing
  python3 apex_state_topup.py --check    # verify the box-score mapping on matches both sources have
"""
import csv, glob, json, os, pickle, sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "apex_prop_state_v1.pkl")
BASE = os.path.join(HERE, "apex_prop_state_v1_base.pkl")
SHEETS = os.path.join(HERE, "data", "sa365_sheets")
DATA_DIR = os.path.join(HERE, "ATP & WTA, 1968-2026")
LOG = os.path.join(HERE, "data", "logs", "state_topup.log")
MAX_AGE_DAYS = 60           # sheets older than this are ignored (TennisMyLife has long caught up)
DEDUPE_DAYS = 16            # a TennisMyLife tourney week covers matches up to ~2 weeks after its date


def _v1():
    sys.path.insert(0, HERE)
    import apex_prop_model_v1 as v1
    return v1


def _year_files():
    y = date.today().year
    out = []
    for yr in (y - 1, y):
        for suf in ("", "_wta"):
            p = os.path.join(DATA_DIR, "%d%s.csv" % (yr, suf))
            if os.path.exists(p):
                out.append(p)
    return out


def _tml_index(v1):
    """(pairs -> [tourney dates]) and {event name: {tourney date: surface}} from the TennisMyLife year files."""
    pairs, cities = {}, {}
    for p in _year_files():
        with open(p, newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                try:
                    d = datetime.strptime((r.get("tourney_date") or "")[:8], "%Y%m%d").date()
                except ValueError:
                    continue
                k = frozenset((v1.norm_name(r.get("winner_name")), v1.norm_name(r.get("loser_name"))))
                pairs.setdefault(k, []).append(d)
                nm = (r.get("tourney_name") or "").strip().lower()
                if nm:
                    cities.setdefault(nm, {})[d] = (r.get("surface") or "Hard").strip().title()
    return pairs, cities


def _tour_level(league, cities, md):
    """Tour level = a TennisMyLife ATP/WTA year-file event in the same city at the same time of
    year (within 30 days, either year). Returns that event's surface, or None. The time-of-year
    test keeps out e.g. the October Mallorca 125 (the year file's Mallorca is the June grass 250)."""
    lg = str(league or "").lower()
    if not lg or "125" in lg or "challenger" in lg or "itf" in lg or "doubles" in lg:
        return None
    city = lg.split(" - ")[-1].strip()
    if not city:
        return None
    best = None
    for nm, by_date in cities.items():
        if city not in nm or "challenger" in nm or "road to" in nm:
            continue
        for t, surf in by_date.items():
            for yr in (md.year - 1, md.year):
                try:
                    gap = abs((t.replace(year=yr) - md).days)
                except ValueError:
                    continue
                if gap <= 30 and (best is None or gap < best[0]):
                    best = (gap, surf)
    return best[1] if best else None


def _num(s, cat, key, side, field="value"):
    v = s[cat][key][side].get(field)
    if v is None:
        raise KeyError(key)
    return float(v)


def sheet_records(d):
    """(rec_player1, rec_player2) in REC_FIELDS order, or None when the box score is incomplete.
    REC_FIELDS = ace, df, svpt, svgms, bpf, bps, osvgms, obpf, obps (same as the replay)."""
    try:
        s = d["statistics"]["ALL"]
        out = {}
        for p in ("player1", "player2"):
            out[p] = (_num(s, "service", "aces", p), _num(s, "service", "doubleFaults", p),
                      _num(s, "service", "firstServeAccuracy", p, "total"), _num(s, "service", "serviceGamesTotal", p),
                      _num(s, "service", "breakPointsSaved", p, "total"), _num(s, "service", "breakPointsSaved", p, "value"))
    except (KeyError, TypeError, AttributeError):
        return None
    a, b = out["player1"], out["player2"]
    if a[2] <= 0 or b[2] <= 0 or a[3] <= 0 or b[3] <= 0:
        return None
    return a + (b[3], b[4], b[5]), b + (a[3], a[4], a[5])


def _load_sheets():
    cutoff = (date.today() - timedelta(days=MAX_AGE_DAYS)).isoformat()
    for f in glob.glob(os.path.join(SHEETS, "*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            continue
        if not isinstance(d, dict) or not isinstance(d.get("_event"), dict):
            continue
        if not isinstance(d.get("player1"), dict) or not isinstance(d.get("player2"), dict):
            continue
        if d["_event"].get("status") != "Ended" or not d.get("dateOnly") or d["dateOnly"] < cutoff:
            continue
        yield d


def collect(state, v1):
    """Matches to add: list of dicts sorted by date. Also returns skip counts."""
    pairs, cities = _tml_index(v1)
    adds, skips = [], {"already in TennisMyLife": 0, "not tour level": 0, "box score incomplete": 0, "player not in file": 0}
    seen = set()
    for d in _load_sheets():
        n1, n2 = d["player1"]["name"], d["player2"]["name"]
        md = d["dateOnly"]
        key = frozenset((v1.norm_name(n1), v1.norm_name(n2)))
        if (md, key) in seen:
            continue
        seen.add((md, key))
        dd = date.fromisoformat(md)
        if any(t <= dd <= t + timedelta(days=DEDUPE_DAYS) for t in pairs.get(key, [])):
            skips["already in TennisMyLife"] += 1; continue
        surf = _tour_level(d["_event"].get("league"), cities, dd)
        if surf is None:
            skips["not tour level"] += 1; continue
        recs = sheet_records(d)
        if recs is None:
            skips["box score incomplete"] += 1; continue
        names = []
        for nm in (n1, n2):
            try:
                names.append(v1.resolve_name(nm, state))
            except BaseException:
                names.append(None)
        if None in names:
            skips["player not in file"] += 1; continue
        adds.append({"date": md, "players": names, "recs": recs, "surface": surf,
                     "league": d["_event"].get("league"), "score": d["_event"].get("score"), "event_id": d["_event"].get("id")})
    adds.sort(key=lambda a: (a["date"], a["event_id"] or ""))
    return adds, skips


def apply(state, adds, v1):
    maxlen = max(v1.WINDOW_GRID)
    for a in adds:
        md = date.fromisoformat(a["date"])
        for nm, rec in zip(a["players"], a["recs"]):
            p = state["players"][nm]
            for s in ("All", a["surface"]):
                lst = list(p["hist"].get(s, []))
                lst.append(tuple(rec))
                p["hist"][s] = lst[-maxlen:]
            if p["last_date"] is None or md >= p["last_date"]:
                p["last_date"] = md; p["last_surface"] = a["surface"]
    return state


# APEX-EXTRAPLAYERS-2026-10-23 (owner, "Go for everything"): singles players the
# player file has never seen (ITF / 125 / Challenger regulars: Shubladze, Tsujioka, Shek,
# Sorger on 10/23) are declined as "unknown player". `--add-player NAME --tour wta|atp`
# builds them from their last SA365 matches: box scores give the nine serve numbers, and
# the rating starts at 1500 and moves match by match like the replay does for any newcomer
# (opponents the file knows use their rating, others 1500). Kept in EXTRA and added on
# every top-up run, only while the morning rebuild still lacks that player.
EXTRA = os.path.join(HERE, "data", "state_extra_players.json")
EXTRA_MIN_MATCHES = 5       # fewer usable box scores than this -> not added


def _load_extra():
    try:
        with open(EXTRA) as f:
            return json.load(f)
    except Exception:
        return {}


def build_extra_player(name, tour="wta", limit=20, state=None):
    """-> (entry dict | None, note). Calls SA365 (recent matches + one box score per match)."""
    import openclaw_v8_api as v8
    import apex_settle_sa365 as st
    v1 = _v1()
    if state is None:
        with open(STATE, "rb") as f:
            state = pickle.load(f)
    canon = v8.resolve_player_name(name, tour)
    ms = v8.get_recent_matches(canon, limit=limit) or []
    recs = []
    for m in ms:
        md, opp = str(m.get("date") or "")[:10], m.get("opponent")
        if not md or not opp or "/" in str(opp):
            continue
        ev, side, _n = st.find_event(canon, opp, md)
        if not ev or str(ev.get("status") or "").lower() != "ended":
            continue
        sheet, _e = st.stat_sheet(ev)
        r = sheet_records(sheet) if isinstance(sheet, dict) else None
        if r is None:
            continue
        try:   # which side of the box score is our player
            side = 1 if st.same_player(canon, sheet["player1"]["name"]) else (
                2 if st.same_player(canon, sheet["player2"]["name"]) else side)
        except Exception:
            pass
        won = m.get("won")
        if won is None:
            continue
        surf = str(m.get("surface") or "Hard").strip().title()
        surf = surf if surf in v1.SURFACES else "Hard"
        recs.append((md, list(r[side - 1]), bool(won), opp, surf))
    recs.sort(key=lambda x: x[0])
    if len(recs) < EXTRA_MIN_MATCHES:
        return None, "%s: only %d SA365 box score(s) -- not added" % (canon, len(recs))
    elo, n = 1500.0, 0
    hist = {"All": []}
    for md, rec, won, opp, surf in recs:
        try:
            oe = state["players"][v1.resolve_name(opp, state)]["elo"]
        except BaseException:
            oe = 1500.0
        ew = 1.0 / (1.0 + 10 ** ((oe - elo) / 400.0))
        elo += v1.k_factor(n) * ((1.0 if won else 0.0) - ew)
        n += 1
        hist["All"].append(rec)
        hist.setdefault(surf, []).append(rec)
    return {"name": canon, "tour": tour, "elo": elo, "n": n, "last_date": recs[-1][0], "last_surface": recs[-1][4],
            "hist": hist, "built_at": datetime.now().isoformat(timespec="seconds"),
            "matches": [(x[0], x[3], x[2]) for x in recs]}, "%s: %d matches, rating %.0f" % (canon, n, elo)


def add_extra_players(names, tour="wta"):
    """Build and save each name; returns note lines. Re-run the top-up afterwards."""
    ex, notes = _load_extra(), []
    for nm in names:
        try:
            e, note = build_extra_player(nm, tour)
        except Exception as err:
            e, note = None, "%s: failed (%s)" % (nm, type(err).__name__)
        if e:
            ex[e["name"]] = e
        notes.append(note)
    os.makedirs(os.path.dirname(EXTRA), exist_ok=True)
    with open(EXTRA + ".tmp", "w") as f:
        json.dump(ex, f, indent=1)
    os.replace(EXTRA + ".tmp", EXTRA)
    return notes


def apply_extra(state, v1):
    """Adds saved extra players the file does not have. Returns the names added."""
    added = []
    maxlen = max(v1.WINDOW_GRID)
    for nm, e in sorted(_load_extra().items()):
        try:
            v1.resolve_name(nm, state)
            continue          # the rebuild has them now
        except BaseException:
            pass
        state["players"][nm] = {"elo": float(e["elo"]), "n": int(e["n"]),
                                "last_date": date.fromisoformat(e["last_date"]), "last_surface": e["last_surface"],
                                "last_age": None, "last_age_date": None,
                                "hist": {s: [tuple(r) for r in lst][-maxlen:] for s, lst in e["hist"].items()}}
        state["name_index"][v1.norm_name(nm)] = nm
        added.append(nm)
    return added


def _atomic_pickle(obj, path):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(obj, f)
    os.replace(tmp, path)


def run(dry_run=False, quiet=False):
    """Never raises. Returns a short status string."""
    try:
        v1 = _v1()
        with open(STATE, "rb") as f:
            cur = pickle.load(f)
        if not cur.get("topup"):
            # a fresh morning rebuild (or first run): this IS the base
            if not dry_run:
                _atomic_pickle(cur, BASE)
            base = cur
        else:
            if not os.path.exists(BASE):
                return "state top-up: base file missing -- left the player file as is"
            with open(BASE, "rb") as f:
                base = pickle.load(f)
            if base.get("latest_date") != cur.get("latest_date"):
                return "state top-up: base %s != current %s -- left as is" % (base.get("latest_date"), cur.get("latest_date"))
        adds, skips = collect(base, v1)
        if dry_run:
            return {"adds": adds, "skips": skips}
        sig = [(a["date"], a["players"][0], a["players"][1], a["league"]) for a in adds]
        ex_sig = sorted("%s|%s" % (k, e.get("built_at")) for k, e in _load_extra().items())
        if cur.get("topup") and cur["topup"].get("matches") == sig and cur["topup"].get("extra", []) == ex_sig:
            return "state top-up: no new matches (%d already in)" % len(adds)
        new = apply(base, adds, v1)
        extra = apply_extra(new, v1)
        new["topup"] = {"marker": "APEX-FRESHSTATE-2026-10-20", "applied_at": datetime.now().isoformat(timespec="seconds"),
                        "n_matches": len(adds), "skips": skips,
                        "matches": sig, "extra": ex_sig, "extra_added": extra}
        _atomic_pickle(new, STATE)
        msg = "state top-up: added %d SA365 matches (%d players); skipped %s" % (
            len(adds), len({n for a in adds for n in a["players"]}), ", ".join("%s %d" % kv for kv in skips.items() if kv[1]))
        if extra:
            msg += "; new players from SA365: %s" % ", ".join(extra)
        try:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, "a") as f:
                f.write("%s %s\n" % (datetime.now().isoformat(timespec="seconds"), msg))
        except Exception:
            pass
        return msg
    except Exception as e:
        return "state top-up failed (player file left as is): %s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------- stale-data flag
STALE_DAYS = 7
_recent = {"ts": 0.0, "by_player": {}, "pairs": {}}


def _recent_matches():
    """{norm name: [(date, norm opponent)]} for finished matches in the last STALE_DAYS+1 days
    that we know were played (SA365 sheets of any level + settled props in the props DB),
    plus the TennisMyLife pair index. Cached 10 min."""
    import time
    if time.time() - _recent["ts"] < 600:
        return _recent["by_player"], _recent["pairs"]
    v1 = _v1()
    out = {}
    since = (date.today() - timedelta(days=STALE_DAYS + 1)).isoformat()

    def add(a, b, md):
        a, b = v1.norm_name(a), v1.norm_name(b)
        out.setdefault(a, set()).add((md, b)); out.setdefault(b, set()).add((md, a))
    for f in glob.glob(os.path.join(SHEETS, "*.json")):
        try:
            if os.path.getmtime(f) < time.time() - 20 * 86400:
                continue
            with open(f) as fh:
                d = json.load(fh)
            if not isinstance(d, dict) or d.get("_event", {}).get("status") != "Ended" or (d.get("dateOnly") or "") < since:
                continue
            add(d["player1"]["name"], d["player2"]["name"], d["dateOnly"])
        except Exception:
            continue
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(HERE, "data", "props_db.sqlite"), uri=True, timeout=5)
        for pl, op, md in con.execute("select distinct player, opponent, match_date from projections "
                                      "where match_date >= ? and score is not null and score <> '' "
                                      "and player <> 'match' and opponent not like '%% 1st Set'", (since,)):
            if pl and op:
                add(pl, op, md)
        con.close()
    except Exception:
        pass
    pairs, _ = _tml_index(v1)
    _recent.update({"ts": time.time(), "by_player": out, "pairs": pairs})
    return out, pairs


def stale_note(player, match_date=None):
    """APEX-FRESHSTATE-2026-10-20: None when the player file is current; otherwise a short
    card line. Stale = the player's newest match in the player file is more than 7 days
    old AND they finished a match in the last 7 days that the file does not have
    (not in TennisMyLife, not added by the top-up -- e.g. a WTA 125 / Challenger match,
    or one SA365 never captured). Player props only. Never raises (returns None)."""
    try:
        if not player or player == "match":
            return None
        import apex_prop_engine as e
        z = e._load()
        st, v1 = z["state"], z["v1"]
        nm = v1.resolve_name(player, st)
        last = st["players"][nm]["last_date"]
        today = date.fromisoformat(match_date) if match_date else date.today()
        if last is not None and (today - last).days <= STALE_DAYS:
            return None
        recent, pairs = _recent_matches()
        topped = {(m[0], frozenset((v1.norm_name(m[1]), v1.norm_name(m[2])))) for m in (st.get("topup") or {}).get("matches", [])}
        missing = []
        for md, opp in recent.get(v1.norm_name(player), ()):
            dd = date.fromisoformat(md)
            if not (0 < (today - dd).days <= STALE_DAYS):
                continue
            key = frozenset((v1.norm_name(player), opp))
            if (md, key) in topped or any(t <= dd <= t + timedelta(days=DEDUPE_DAYS) for t in pairs.get(key, [])):
                continue
            missing.append(md)
        if not missing:
            return None
        return ("🕒 Player data stale: newest match in the file is %s, but %s also played on %s "
                "(not in the file yet) -- left off Best Standard" % (last.isoformat() if last else "none", player, max(missing)))
    except BaseException:
        return None


def cmd_check():
    """Matches both sources have: SA365 numbers must equal TennisMyLife's for all nine fields."""
    v1 = _v1()
    tml = {}
    for p in _year_files():
        with open(p, newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                try:
                    w = tuple(float(r[c]) for c in ("w_ace", "w_df", "w_svpt", "w_SvGms", "w_bpFaced", "w_bpSaved"))
                    l = tuple(float(r[c]) for c in ("l_ace", "l_df", "l_svpt", "l_SvGms", "l_bpFaced", "l_bpSaved"))
                except (ValueError, KeyError, TypeError):
                    continue
                try:
                    td = datetime.strptime(r["tourney_date"][:8], "%Y%m%d").date()
                except ValueError:
                    continue
                tml.setdefault((v1.norm_name(r["winner_name"]), v1.norm_name(r["loser_name"])), []).append(
                    (td, (w + (l[3], l[4], l[5]), l + (w[3], w[4], w[5]))))
    n = ok = 0
    bad = []
    for d in _load_sheets():
        recs = sheet_records(d)
        if recs is None:
            continue
        a, b = v1.norm_name(d["player1"]["name"]), v1.norm_name(d["player2"]["name"])
        dd = date.fromisoformat(d["dateOnly"])
        hit = [x for x in tml.get((a, b), []) if x[0] <= dd <= x[0] + timedelta(days=DEDUPE_DAYS)]
        flip = [x for x in tml.get((b, a), []) if x[0] <= dd <= x[0] + timedelta(days=DEDUPE_DAYS)]
        if hit:
            want = hit[0][1]
        elif flip:
            want = flip[0][1][::-1]
        else:
            continue
        n += 1
        if tuple(recs[0]) == want[0] and tuple(recs[1]) == want[1]:
            ok += 1
        else:
            bad.append((d["dateOnly"], d["player1"]["name"], d["player2"]["name"], recs, want))
    print("matches in both sources: %d | all nine numbers equal: %d" % (n, ok))
    for x in bad[:5]:
        print("  differs:", x)
    return n, ok


if __name__ == "__main__":
    if "--check" in sys.argv:
        cmd_check()
    elif "--add-player" in sys.argv:
        a = sys.argv[sys.argv.index("--add-player") + 1:]
        tour = a[a.index("--tour") + 1] if "--tour" in a else "wta"
        names = [x for x in a if not x.startswith("--") and x != tour]
        for line in add_extra_players(names, tour):
            print(line)
        print(run())
    elif "--dry-run" in sys.argv:
        r = run(dry_run=True)
        if isinstance(r, dict):
            print("would add %d matches; skipped %s" % (len(r["adds"]), r["skips"]))
            for a in r["adds"]:
                print("  %s  %-24s v %-24s %-10s %s  %s" % (a["date"], a["players"][0], a["players"][1], a["score"], a["surface"], a["league"]))
        else:
            print(r)
    else:
        print(run())
