#!/usr/bin/env python3
"""
APEX-AUTOEVAL-2026-10-13 (owner, "Go" 2026-10-13): props PrizePicks
posts after the last /fetch or 5 PM run used to wait for the next manual
/fetch (Yastremska vs Iva Jovic went up at 7:53 AM and sat unevaluated), and a
prop skipped because the odds feed had no match yet was never tried again.

Every 30 minutes (scheduler) this:
  1. pulls the live board from the PrizePicks partner API (no browser),
  2. keeps props whose match hasn't started and that have no pick-log row yet
     -- new props AND props skipped earlier for missing odds (a skip writes no
     row, so each run retries them until the odds show up),
  3. runs each through the same engine the re-pricing job uses (no LLM path),
  4. republishes the boards when anything new was scored.
First-set markets (not modeled) and doubles are left out. Quiet: #alerts sends
are held for the run; nothing is posted unless the boards change, and then
only the normal publish happens.
"""
import csv, json, os   # json: APEX-SCORINGQUEUE-2026-11-02
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_PER_RUN = 250        # APEX-ANYDATE-2026-10-25: was 150; a full run of 150 took ~4 min
ANY_DATE = True           # APEX-ANYDATE-2026-10-25: False = old board-day-first order
MIN_LEAD_MIN = 5          # don't bother with a match starting within 5 minutes
# APEX-SCORINGQUEUE-2026-11-02 (owner, "board not filling after 11 AM" -> "go fix scoring queue"): a prop
# that was tried and produced no pick (no usable odds, unknown player, engine error) waits RETRY_MIN minutes before
# it is tried again, and props never tried go first. Before this, ~650 waiting props were taken soonest-start-first
# into the 250-a-run cap; the first 250 were low-level matches with no odds that failed every run, so Zverev v
# Musetti, Medvedev v Buse, Bonzi v Fritz and Gea v Struff (Paris, 11/3 10:00 UTC) were never scored at all.
# Undo: SCORING_QUEUE_ON = False.
SCORING_QUEUE_ON = True
RETRY_MIN = 60
RETRY_MIN_MANY = 180      # after 6 failed tries: every 3 hours
DUP_RETRY_MIN = 1440      # APEX-FIXDUP-2026-11-02: the engine already has this prop (forward log duplicate): once a day
TRIED_FILE = os.path.join(HERE, "data", "logs", "auto_eval_tried.json")


def _kstr(k):
    """Canonical text for a _key() tuple (frozenset order is not stable across runs)."""
    cat, names, line, ptype = k
    nm = sorted(names) if isinstance(names, (frozenset, set)) else list(names)
    return "%s|%s|%s|%s" % (cat, "|".join(str(x) for x in nm), line, ptype)


def _load_tried():
    try:
        with open(TRIED_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_tried(d):
    try:
        os.makedirs(os.path.dirname(TRIED_FILE), exist_ok=True)
        cutoff = (datetime.now() - timedelta(days=3)).isoformat()
        d = {k: v for k, v in d.items() if str(v.get("last") or "") >= cutoff}
        tmp = TRIED_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, TRIED_FILE)
    except Exception:
        pass


def _retry_rank(tried, ks, now_iso):
    """0 = never tried (go first); 1 = failed before, wait is over (try again); 2 = failed recently (skip this run)."""
    e = tried.get(ks)
    if not e:
        return 0
    try:
        last = datetime.fromisoformat(e["last"])
        wait = RETRY_MIN_MANY if int(e.get("n") or 0) >= 6 else RETRY_MIN
        if e.get("dup"):   # APEX-FIXDUP-2026-11-02
            wait = DUP_RETRY_MIN
        return 2 if (datetime.fromisoformat(now_iso) - last).total_seconds() < wait * 60 else 1
    except Exception:
        return 1


def _pick_log(core):
    p = core._APEX_PICK_LOG
    return p if os.path.isabs(p) else os.path.join(HERE, p)


def _norm(s):
    return str(s or "").strip().lower()


def _evaluated_keys(core, starts=None):
    """(category, names, line, prop_type) for every pick-log row in the last 3 days.
    APEX-DATEMOVE-2026-10-25: `starts` (a dict) gets each key's newest (ts, slip_start_utc)."""
    out = set()
    cutoff = (datetime.now() - timedelta(days=3)).isoformat()
    with open(_pick_log(core), newline="") as fh:
        for r in csv.DictReader(fh):
            if (r.get("ts") or "") < cutoff:
                continue
            if r.get("player") == "match":
                names = frozenset(_norm(x) for x in str(r.get("opponent") or "").split(" v "))
            else:
                # APEX-ALTLINE-FIX-2026-10-24: the opponent is part of the key -- with the
                # player alone, Fritz Aces 14.5 Demon v Musetti counted as done because
                # Fritz Aces 14.5 Demon v Herbert was scored 10/21 (12 of 21 new Aces
                # props at 7:43 PM 10/24 were skipped this way; /fetch has no such skip)
                names = (_norm(r.get("player")), _norm(r.get("opponent")))
            try:
                k = (r.get("category"), names, float(r.get("line")), _norm(r.get("prop_type") or "standard"))
            except (TypeError, ValueError):
                continue
            out.add(k)
            if starts is not None:
                row = (r.get("ts") or "", str(r.get("slip_start_utc") or ""), r.get("match_id") or "",
                       str(r.get("match_date") or "")[:10])
                if row[0] >= starts.get(k, ("",))[0]:
                    starts[k] = row
                pk = ("pair", frozenset(names))
                if row[0] >= starts.get(pk, ("",))[0]:
                    starts[pk] = row
    return out


# APEX-DATEMOVE-2026-10-25 (owner, "Go fix date"): a prop already scored is re-scored
# when PrizePicks has since moved its match by an hour or more (another day included). Before,
# the done-key ignored the date, so only new lines on a moved match were scored and the old
# ones were later retired as a stale matchup (Kraus v Udvardy / Mertens v Uchijima TG, 10/25).
MOVE_MIN_MIN = 60


def _moved(starts, k, st):
    """'' when the prop needs no re-score; else why: 'start' (PrizePicks' start differs from the
    prop's newest scored start by MOVE_MIN_MIN+), 'date' (the match's newer rows carry another
    match date -- Kraus v Udvardy 10/27 -> 10/26 with the start unchanged) or 'id' (same date,
    newer rows under another match_id; re-scored quietly so the prop follows its match)."""
    ts, old, mid, md = starts.get(k) or ("", "", "", "")
    try:
        o = datetime.fromisoformat(old.replace("Z", "+00:00"))
        if o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        if abs((st - o).total_seconds()) >= MOVE_MIN_MIN * 60:
            return "start"
    except Exception:
        pass
    pts, _pst, pmid, pmd = starts.get(("pair", frozenset(k[1]))) or ("", "", "", "")
    if pmid and mid and pmid != mid and pts > ts:
        return "date" if pmd != md else "id"
    return ""


def _key(core, p):
    cat = core._APEX_STAT_MAP.get(str(p.get("stat_type", "")).strip().lower().replace(" ", "_"))
    ptype = "demon" if p.get("is_demon") else ("goblin" if p.get("is_goblin") else "standard")
    names = frozenset((_norm(p.get("player")), _norm(p.get("opponent")))) if cat in core._APEX_MATCH_LEVEL \
        else (_norm(p.get("player")), _norm(p.get("opponent")))
    return (cat, names, float(p.get("line")), ptype), ptype


def _tour(core, prop):
    """APEX-AUTOEVAL-TOUR-2026-10-15 (owner, "Go" 2026-10-15): the
    partner API never says ATP or WTA, and this path skipped the Gate 11 tour
    check /fetch runs, so every prop it scored was simulated as ATP. Bouzas
    Maneiro (WTA) Break Points Won LESS 5.5 showed 75.5% as ATP; as WTA it is
    59%. Kartal Aces 0.5 MORE: 84% as ATP, 72% as WTA. Same sources /fetch and
    the re-price job use: SportsAPI365's listing, then the market's
    "(women's singles)" format label / tournament name."""
    try:
        import apex_price_feed as _apf
        fc = _apf.fixture_context(prop.get("player"), prop.get("opponent")) or {}
        if str(fc.get("tour") or "").lower() == "wta":
            return "wta"
    except Exception:
        pass
    lab = " ".join(str(prop.get(k) or "") for k in
                   ("format_label", "apex_fixture_tournament", "tournament", "league")).lower()
    return "wta" if ("women" in lab or "wta" in lab) else "atp"


def _mark_board_seen(core, props, now):
    """APEX-AUTOEVAL-SEEN-2026-10-15 (owner, "Go" 2026-10-15): this run
    pulled the whole live board, so every prop on it is confirmed still
    offered -- record that, not just the new ones it scores. Before this, a
    batch of 15+ newly scored props arriving 45+ min after the last /fetch
    became "the latest scrape" and the board hid everything else (1:39 PM
    10/15: kept 35, hid 1,569; Best Goblin down to one prop). Same sighting
    file and key format as _apex_mark_seen, written once per run."""
    import json as _js
    ts = datetime.now().isoformat(timespec="seconds")
    keys = []
    for p in props:
        try:
            st = datetime.fromisoformat(str(p.get("start_time_raw")).replace("Z", "+00:00"))
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            if st <= now:
                continue
            cat = core._APEX_STAT_MAP.get(str(p.get("stat_type", "")).strip().lower().replace(" ", "_"))
            if not cat:
                continue
            line = "%.1f" % (0.5 if cat in core._APEX_BINARY else float(p.get("line")))
            a, b = str(p.get("player") or "").strip().lower(), str(p.get("opponent") or "").strip().lower()
            if not a or not b:
                continue
            if cat in core._APEX_MATCH_LEVEL:
                keys += ["|".join(["match", f"{a} v {b}", cat, line]), "|".join(["match", f"{b} v {a}", cat, line])]
            else:
                keys.append("|".join([a, b, cat, line]))
        except Exception:
            continue
    if not keys:
        return 0
    path = core._APEX_SEEN_FILE if os.path.isabs(core._APEX_SEEN_FILE) else os.path.join(HERE, core._APEX_SEEN_FILE)
    try:
        try:
            with open(path) as fh:
                d = _js.load(fh)
        except Exception:
            d = {}
        for k in keys:
            d[k] = ts
        if len(d) > 6000:
            d = dict(sorted(d.items(), key=lambda kv: kv[1])[-4000:])
        with open(path + ".tmp", "w") as fh:
            _js.dump(d, fh)
        os.replace(path + ".tmp", path)
    except Exception as e:
        core.log("AutoEval", f"board sighting write failed (non-fatal): {e}")
        return 0
    return len(keys)


def run(core):
    now_ct = datetime.now(core.CT)
    if now_ct.hour == 16 and now_ct.minute >= 40 or now_ct.hour == 17 and now_ct.minute < 40:
        return "auto-eval: skipped around the 5 PM run"
    # APEX-FRESHSTATE-2026-10-20: add newly settled SA365 box scores to the player file
    # before scoring, so a match played earlier today counts for the next prop. Never raises.
    try:
        import apex_state_topup
        apex_state_topup.run()
    except Exception:
        pass
    dates = core.get_system_dates()
    try:
        props = core.fetch_props_partner_api(dates["match_date"], caller="auto_eval")
    except Exception as e:
        return f"auto-eval: partner API failed ({type(e).__name__}: {e})"
    if not props:
        return "auto-eval: partner API returned no props"
    starts = {}
    done = _evaluated_keys(core, starts)
    now = datetime.now(timezone.utc)
    moved = {}   # APEX-DATEMOVE-2026-10-25: "A v B" -> (new start, old start)
    seen_n = _mark_board_seen(core, props, now)
    todo = []
    _keymap, _tried, _now_iso = {}, (_load_tried() if SCORING_QUEUE_ON else {}), datetime.now().isoformat()   # APEX-SCORINGQUEUE-2026-11-02
    _dups = set()   # APEX-FIXDUP-2026-11-02: keys the engine reported as already scored this run
    for p in props:
        slug = str(p.get("stat_type", "")).strip().lower().replace(" ", "_")
        if slug in core._APEX_FIRSTSET_SLUGS or "/" in str(p.get("player") or ""):
            continue
        try:
            st = datetime.fromisoformat(str(p.get("start_time_raw")).replace("Z", "+00:00"))
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if (st - now).total_seconds() < MIN_LEAD_MIN * 60:
            continue
        try:
            k, ptype = _key(core, p)
        except (TypeError, ValueError):
            continue
        if k[0] is None:
            continue
        if k in done:
            why = _moved(starts, k, st)
            if not why:
                continue
            p = dict(p, apex_date_moved=True)
            if why != "id":
                w = moved.setdefault(" v ".join(sorted((_norm(p.get("player")), _norm(p.get("opponent"))))),
                                     {"label": "%s v %s" % (str(p.get("player")).split()[-1],
                                                            str(p.get("opponent")).split()[-1]),
                                      "start": st.isoformat(), "old_start": starts[k][1], "props": 0, "date": False})
                w["props"] += 1
                w["date"] = w["date"] or why == "date"
        todo.append((p, ptype, st))
        _keymap[id(p)] = _kstr(k)   # APEX-SCORINGQUEUE-2026-11-02
    # APEX-ODDSWATCH-2026-10-24 (owner, "Go odds"): the board day's and tonight's props
    # go first (soonest start first), so next week's unpriced matches can't fill the 150 cap
    # before the board day is tried; the watch notes which matches are still waiting for bet365.
    # Board day = UTC start date (tonight's Asia matches count, Monday's Asia matches at 8:30 PM
    # CT Sunday wait for their own board day).
    board_day = dates["match_date"]
    # APEX-ANYDATE-2026-10-25 (owner, "I want the top the show regardless of the date"):
    # every upcoming match is scored in start order, whatever its day; moved matches go first.
    if ANY_DATE:
        # APEX-SCORINGQUEUE-2026-11-02: moved matches, then never-tried props, then retries, each soonest first
        todo.sort(key=lambda t: (_retry_rank(_tried, _keymap.get(id(t[0]), ""), _now_iso), not t[0].get("apex_date_moved"), t[2]))
    else:
        todo.sort(key=lambda t: (t[2].astimezone(timezone.utc).date().isoformat() > board_day, t[2]))
    try:
        import apex_odds_watch as _ow
        ow = _ow.start(core, todo, board_day)
    except Exception as e:
        _ow, ow = None, None
        core.log("AutoEval", f"odds watch unavailable (non-fatal): {type(e).__name__}: {e}")
    if not todo:
        if ow and _nobook_ready(core, _ow, ow):
            return _odds_watch_republish(core, _ow, ow, sum(1 for _ in open(_pick_log(core))),
                                         f"auto-eval: nothing new on the board ({seen_n} sightings refreshed)")
        if ow:
            _ow.finish(core, ow, sum(1 for _ in open(_pick_log(core))), False)
        return f"auto-eval: nothing new on the board ({seen_n} sightings refreshed)"
    _waiting = 0
    if SCORING_QUEUE_ON:   # APEX-SCORINGQUEUE-2026-11-02: props that failed within RETRY_MIN sit this run out
        _keep = [t for t in todo if _retry_rank(_tried, _keymap.get(id(t[0]), ""), _now_iso) < 2]
        _waiting = len(todo) - len(_keep)
        todo = _keep
    todo = todo[:MAX_PER_RUN]
    # APEX-FETCHSTATUS-2026-10-23: one SportsAPI365 check first; a refused key skips scoring
    # (it used to spend ~5 s of failed lookups per prop, 150 props a run, scoring 0).
    try:
        import apex_sa365_guard as _sa365g
        _sa365g.reset()
        _sa_ok, _sa_why = _sa365g.preflight()
        if not _sa_ok:
            try:   # APEX-KEYALERT-2026-10-29: never silent -- one 🛑 line in #general + #alerts, reminders, then ✅
                import apex_sa365_alert as _ka
                _ka.refused(core, len(todo), _sa_why)
            except Exception as _kae:
                core.log("KeyAlert", f"alert failed (non-fatal): {type(_kae).__name__}: {_kae}")
            return f"auto-eval: SportsAPI365 key refused ({_sa_why}) -- skipped scoring {len(todo)} prop(s) ({seen_n} sightings refreshed)"
        try:
            import apex_sa365_alert as _ka
            _ka.working(core)
        except Exception:
            pass
    except Exception:
        pass
    before = sum(1 for _ in open(_pick_log(core)))
    core.begin_fetch_discord_suppression()
    tried = 0
    try:
        import apex_alert_chain as _AC   # APEX-ALERTCHAIN-2026-10-25: "scoring 40 of 73" on an open card
    except Exception:
        _AC = None
    try:
        for _i, (p, ptype, st) in enumerate(todo):
            if _AC is not None:
                try:
                    _AC.progress(_i, len(todo))
                except Exception:
                    pass
            prop = dict(p)
            prop.update({"prop_type_classified": ptype, "slip_start_utc": st.astimezone(timezone.utc).isoformat(),
                         "apex_fixture_tournament": p.get("tournament") or ""})
            # APEX-AUTOEVAL-MORELOCK-2026-10-13: /fetch locks these to MORE in
            # its filter step, which this path skips -- without the lock a LESS
            # verdict was logged playable=1 (72 Demon/Goblin LESS picks reached
            # the boards on 10/13).
            if ptype in ("goblin", "demon", "red_demon"):
                prop["direction_locked"] = "MORE"
                prop["both_directions_available"] = False
            try:
                prop = core.gate_11b_format_detection(prop)
                prop["tour"] = _tour(core, prop)
                core._apex_engine_pick(prop, dates, None)
                tried += 1
                if prop.get("apex_forward_dup") and _keymap.get(id(p)):   # APEX-FIXDUP-2026-11-02
                    _dups.add(_keymap[id(p)])
            except Exception as e:
                core.log("AutoEval", f"skip {p.get('player')} {p.get('stat_type')} {p.get('line')}: {type(e).__name__}: {e}")
    finally:
        core.end_fetch_discord_suppression()
    # APEX-DATEMOVE-2026-10-25: one 📅 line per moved match -- a new match date, or 2+ of its
    # props now start an hour+ from their scored time (one stale row alone is not a move)
    moved = {k: w for k, w in moved.items() if w["date"] or w["props"] >= 2}
    if moved and ow is not None:
        try:
            _ow.note_moved(core, moved)
        except Exception as e:
            core.log("AutoEval", f"date-move note failed (non-fatal): {type(e).__name__}: {e}")
    after = sum(1 for _ in open(_pick_log(core)))
    scored = max(0, after - before)
    if SCORING_QUEUE_ON:   # APEX-SCORINGQUEUE-2026-11-02: remember what was tried and produced nothing
        try:
            # APEX-SCORINGQUEUE-2026-11-02 (12:45 PM fix): success = a NEW pick-log row this run, not "seen in the
            # last 3 days" -- date-moved props are already in that set and were retried every run anyway
            _done2 = set()
            with open(_pick_log(core), newline="") as _fh:
                for _i2, _r in enumerate(csv.DictReader(_fh), 2):
                    if _i2 <= before:
                        continue
                    try:
                        if _r.get("player") == "match":
                            _nm = frozenset(_norm(x) for x in str(_r.get("opponent") or "").split(" v "))
                        else:
                            _nm = (_norm(_r.get("player")), _norm(_r.get("opponent")))
                        _done2.add(_kstr((_r.get("category"), _nm, float(_r.get("line")), _norm(_r.get("prop_type") or "standard"))))
                    except (TypeError, ValueError):
                        continue
            _fails = 0
            for t in todo:
                ks = _keymap.get(id(t[0]))
                if not ks:
                    continue
                if ks in _done2:
                    _tried.pop(ks, None)
                elif ks in _dups:   # APEX-FIXDUP-2026-11-02: already scored -- park it for a day, not an hour
                    e = _tried.get(ks) or {"n": 0}
                    _tried[ks] = {"n": int(e.get("n") or 0) + 1, "last": _now_iso, "dup": True}
                else:
                    e = _tried.get(ks) or {"n": 0}
                    _tried[ks] = {"n": int(e.get("n") or 0) + 1, "last": _now_iso}
                    _fails += 1
            _save_tried(_tried)
            _waiting += _fails
            if _dups:
                core.log("AutoEval", f"{len(_dups)} prop(s) were already scored (duplicates) -- parked for {DUP_RETRY_MIN // 60} h")
        except Exception as _qe:
            core.log("AutoEval", f"scoring queue bookkeeping failed (non-fatal): {type(_qe).__name__}: {_qe}")
    msg = (f"auto-eval: tried {tried} new/unpriced prop(s), scored {scored}" + (" -- boards republished" if scored else "")
           + f" ({seen_n} sightings refreshed)"
           + (f" | {_waiting} prop(s) have no usable odds yet, next try in {RETRY_MIN} min" if _waiting else ""))   # APEX-SCORINGQUEUE-2026-11-02
    nobook_back = bool(ow) and _nobook_ready(core, _ow, ow)
    if ow and (scored or nobook_back):
        return _odds_watch_republish(core, _ow, ow, before, msg)
    if scored:
        try:
            import apex_edge_tracker as _aet
            _aet.republish_boards(source="autoeval")
        except Exception as e:
            core.log("AutoEval", f"republish failed (non-fatal): {e}")
    if ow:
        _ow.finish(core, ow, before, False)
    return msg


def _nobook_ready(core, _ow, ow):
    try:
        return _ow.fill_nobook_totals(ow) > 0
    except Exception as e:
        core.log("AutoEval", f"odds watch bet365 total check failed (non-fatal): {type(e).__name__}: {e}")
        return False


def _odds_watch_republish(core, _ow, ow, before, msg):
    """APEX-ODDSWATCH-2026-10-24: the normal republish (same path as before), then the
    odds-watch #alerts line when a waiting match got priced or a held bet365 total arrived."""
    republished = False
    try:
        import apex_edge_tracker as _aet
        _aet.republish_boards(source="autoeval")
        republished = True
    except Exception as e:
        core.log("AutoEval", f"republish failed (non-fatal): {e}")
    line = _ow.finish(core, ow, before, republished)
    if republished and "republished" not in msg:
        msg += " -- boards republished (odds watch: bet365 total arrived for a held pick)"
    return msg + (f" | {line}" if line else "")
