#!/usr/bin/env python3
"""
APEX-LEARNV2-2026-10-24 (owner, 10/24 11:28 PM: "get the learning up to date ...
make the correct adjustments to the learning system so it can actually help ... make
sure its output is readable/reliable ... needs emojis and proper information").

What this module does (plain words -- full write-up in apex_learning_system/HOW_LEARNING_WORKS.md):

  1. GRADE FROM WHAT WE ALREADY HAVE. Every PrizePicks prop is stored in
     data/props_db.sqlite and settled hourly from SportsAPI365 box scores. The old
     nightly learning only asked the results API again, missed most matches, and
     parked them in a pending queue -> quarantine. Now a stuck pick is first looked
     up in props_db (same player, stat, line, side, date). Found = graded.
  2. HOUSEKEEPING. Rows that can never be graded (no result anywhere, or before the
     Oct 9 learning window) are marked "unresolvable -- no result source" and kept
     (never deleted). Internal counters go to data/logs/learning_health.log, not Discord.
  3. REAL NUMBERS. Hit rates are computed from props_db for playable props since
     Oct 9 (Goblin/Demon LESS excluded -- they can't be played): by stat + side, line
     band, tour, Win Prob band and board.
  4. SUGGESTIONS ONLY. It can suggest raising a floor or holding a stat+side off the
     boards, with backtest numbers and a go word. It never changes picks, weights,
     floors or rules by itself; it never suggests lowering the 70% / Apex 67 floors.
  5. ONE CLEAN #learning CARD per night (11:30 PM CT).

Run by hand:
  python3 apex_learning_v2.py catchup --dry      # what would be graded / marked
  python3 apex_learning_v2.py catchup            # do it
  python3 apex_learning_v2.py card --dry         # print tonight's card, post nothing
  python3 apex_learning_v2.py card               # post tonight's card to #learning
"""
import json, os, re, sqlite3, sys, csv, unicodedata
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "props_db.sqlite")
LEARN_DIR = os.path.join(HERE, "data", "learning")
PENDING_FILE = os.path.join(LEARN_DIR, "pending_results.json")
MANUAL_FILE = os.path.join(LEARN_DIR, "manual_review.json")
QUAR_FILE = os.path.join(LEARN_DIR, "quarantine.json")
LEDGER_FILE = os.path.join(LEARN_DIR, "graded_picks.json")        # every graded /fetch pick, once
SUGGEST_FILE = os.path.join(LEARN_DIR, "suggestions.json")        # proposals waiting for a go
CARD_STATE = os.path.join(LEARN_DIR, "learning_card_state.json")  # one card per night
HEALTH_LOG = os.path.join(HERE, "data", "logs", "learning_health.log")
PICK_LOG = os.path.join(HERE, "apex_engine_picks_log_v1.csv")

LEARN_START = "2026-10-09"      # learning only learns from Oct 9+ graded picks
FLOOR = 0.70                    # the 70% Win Prob floor (never lowered here)
UNRESOLVABLE = "UNRESOLVABLE_NO_RESULT_SOURCE"
BEFORE_WINDOW = "BEFORE_LEARNING_WINDOW"
GRADED_LATE = "GRADED_FROM_PROPS_DB"

# pick stat_type (fetch archive / pending queue) -> props_db category
STAT_TO_CAT = {
    "total_games": "total_games", "total_games_won": "games_won", "games_won": "games_won",
    "fantasy_score": "fantasy_score", "aces": "aces", "double_faults": "dfs", "dfs": "dfs",
    "break_points_won": "breaks", "breaks": "breaks", "total_tie_breaks": "tiebreaks",
    "tiebreaks": "tiebreaks", "total_sets": "total_sets",
}
CAT_NAME = {
    "total_games": "Total Games", "games_won": "Games Won", "fantasy_score": "Fantasy",
    "aces": "Aces", "dfs": "Double Faults", "breaks": "Breaks", "tiebreaks": "Tiebreaks",
    "total_sets": "Total Sets", "first_set_games": "1st Set Games",
}
CAT_EMOJI = {"total_games": "🎾", "games_won": "🏆", "fantasy_score": "🎯", "aces": "🚀",
             "dfs": "💥", "breaks": "🔓", "tiebreaks": "⚖️", "total_sets": "🧮"}
BOARD_NAME = {"top3": "Top 3", "elite6": "Elite 6", "top10": "Top 10", "bestgoblin": "Best Goblin",
              "beststandard": "Best Standard", "bestdemon": "Best Demon"}


# ─────────────────────────── helpers ───────────────────────────
def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", s.lower().replace("-", " ")).strip()


def _last(s):
    p = _norm(s).split()
    return p[-1] if p else ""


def _load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, default=str)
    os.replace(tmp, path)


def health_log(msg):
    os.makedirs(os.path.dirname(HEALTH_LOG), exist_ok=True)
    with open(HEALTH_LOG, "a") as fh:
        fh.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %I:%M %p"), msg))


def _conn():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def pick_key(p):
    """One identity per real-world prop, so each pick is counted once."""
    return "|".join(str(x) for x in (
        _norm(p.get("player")), _norm(p.get("opponent")),
        STAT_TO_CAT.get(p.get("stat_type"), p.get("stat_type")),
        float(p.get("line") or 0), (p.get("direction") or "").upper(),
        p.get("match_date") or ""))


# ─────────────────────── result lookup in props_db ───────────────────────
def lookup_result(conn, player, opponent, stat_type, line, direction, match_date=None,
                  near_date=None, prop_type=None):
    """Find the settled PrizePicks prop that matches this pick. Returns
    (status, info) where status is 'HIT' | 'BUST' | 'PUSH' | 'VOID' | 'NOT_SETTLED' | 'NO_MATCH'."""
    cat = STAT_TO_CAT.get(stat_type, stat_type)
    try:
        line = float(line)
    except (TypeError, ValueError):
        return "NO_MATCH", None
    if match_date:
        lo = hi = match_date
        rows = conn.execute(
            "select * from projections where category=? and abs(coalesce(line_last,line_first)-?)<0.01 "
            "and match_date between date(?,'-1 day') and date(?,'+1 day')", (cat, line, lo, hi)).fetchall()
    elif near_date:   # quarantine rows only know the night they were parked
        rows = conn.execute(
            "select * from projections where category=? and abs(coalesce(line_last,line_first)-?)<0.01 "
            "and match_date between date(?,'-8 day') and ?", (cat, line, near_date, near_date)).fetchall()
    else:
        return "NO_MATCH", None
    pf, of = _norm(player), _norm(opponent)
    pl, ol = _last(player), _last(opponent)
    # full names first; last name + first initial only when that can't be confused
    # (Xinyu Wang v Xiyu Wang: same last name both sides -> full names only)
    cands = [r for r in rows if _norm(r["player"]) == pf and (not of or _norm(r["opponent"]) == of)]
    if not cands and pl and pl != ol:
        def _same(a, b):
            a, b = _norm(a).split(), _norm(b).split()
            return bool(a and b) and a[-1] == b[-1] and a[0][:1] == b[0][:1]
        cands = [r for r in rows if _same(r["player"], player)
                 and (not of or _same(r["opponent"], opponent))]
    if not cands and pl and pl != ol and stat_type in ("total_games", "total_sets", "total_tie_breaks", "tiebreaks"):
        # match-level stats: the same number whichever side the prop was listed under
        cands = [r for r in rows if _norm(r["opponent"]) == pf and (not of or _norm(r["player"]) == of)]
    if not cands:
        return "NO_MATCH", None
    if prop_type:
        same = [r for r in cands if (r["odds_type"] or "standard") == prop_type]
        cands = same or cands
    if match_date:
        exact = [r for r in cands if r["match_date"] == match_date]
        cands = exact or cands
    if near_date and len({(r["match_date"], _last(r["opponent"])) for r in cands}) > 1:
        return "AMBIGUOUS", None   # same player/stat/line in two matches that week: don't guess
    settled = [r for r in cands if (r["result"] or "") in ("more", "less", "push", "void")]
    if not settled:
        return "NOT_SETTLED", dict(cands[0])
    r = sorted(settled, key=lambda x: x["match_date"] or "", reverse=True)[0]
    res = r["result"]
    if res == "void":
        return "VOID", dict(r)
    if res == "push":
        return "PUSH", dict(r)
    return ("HIT" if res == (direction or "").lower() else "BUST"), dict(r)


# ─────────────────────────── catch-up ───────────────────────────
def _record_outcome(eval_id, outcome):
    try:
        import dataset_a_capture as d1
        # Dataset A only accepts its own source names (CHECK constraint); props_db
        # results reach it through the pending queue, so that is the label.
        d1.record_outcome(eval_id, outcome, "pending_results_json")
        return True
    except Exception:
        return False


def _ledger_add(ledger, keys, p, status, info, how):
    k = pick_key(p)
    if k in keys:
        return False
    keys.add(k)
    ledger.append({
        "key": k, "player": p.get("player"), "opponent": p.get("opponent"),
        "stat_type": p.get("stat_type"), "category": STAT_TO_CAT.get(p.get("stat_type"), p.get("stat_type")),
        "line": p.get("line"), "direction": p.get("direction"), "match_date": p.get("match_date") or (info or {}).get("match_date"),
        "prop_type": p.get("prop_type_classified"), "confidence": p.get("confidence"),
        "outcome": status, "actual": (info or {}).get("actual"), "score": (info or {}).get("score"),
        "source": how, "graded_at": datetime.now().isoformat(timespec="seconds"),
    })
    return True


def catch_up(dry=True, today=None):
    today = today or date.today().isoformat()
    conn = _conn()
    pending = _load(PENDING_FILE, [])
    manual = _load(MANUAL_FILE, [])
    quar = _load(QUAR_FILE, [])
    ledger = _load(LEDGER_FILE, [])
    keys = {e["key"] for e in ledger}
    stats = {"pending_before": len(pending), "manual_before": len(manual), "quarantine_rows": len(quar)}

    def _grade_list(rows, label):
        keep, graded, voided, unres, waiting = [], 0, 0, 0, 0
        for p in rows:
            md = p.get("match_date")
            st, info = lookup_result(conn, p.get("player"), p.get("opponent"), p.get("stat_type"),
                                     p.get("line"), p.get("direction"), match_date=md,
                                     prop_type=p.get("prop_type_classified"))
            if st in ("HIT", "BUST") and not (md and md < LEARN_START):
                graded += 1
                if not dry:
                    if _ledger_add(ledger, keys, p, st, info, "props_db (%s)" % label) and p.get("eval_id"):
                        _record_outcome(p["eval_id"], st)
                continue
            if st in ("VOID", "PUSH"):
                voided += 1
                if not dry:
                    quar.append({"date": today, "player": p.get("player"), "opponent": p.get("opponent"),
                                 "match_date": md, "pick": "%s %s %s" % (p.get("direction"), p.get("line"), p.get("stat_type")),
                                 "result_type": "NON_COMPLETION" if st == "VOID" else "PUSH",
                                 "signal": "props_db says %s" % st.lower(), "status": "EXCLUDED_FROM_LEARNING"})
                continue
            age = (date.fromisoformat(today) - date.fromisoformat(md)).days if md else 99
            # a prop that props_db has never seen, or still can't settle after 3 days,
            # has no result source left -> mark it (kept, not deleted)
            if (md and md < LEARN_START) or (st == "NO_MATCH" and age >= 2) or (st == "NOT_SETTLED" and age >= 3):
                unres += 1
                if not dry:
                    quar.append({"date": today, "player": p.get("player"), "opponent": p.get("opponent"),
                                 "match_date": md, "pick": "%s %s %s" % (p.get("direction"), p.get("line"), p.get("stat_type")),
                                 "result_type": UNRESOLVABLE if not (md and md < LEARN_START) else BEFORE_WINDOW,
                                 "signal": "unresolvable — no result source (%s, %s)" % (st.lower(), label),
                                 "status": "EXCLUDED_FROM_LEARNING"})
                continue
            waiting += 1
            keep.append(p)
        return keep, {"graded": graded, "void_or_push": voided, "unresolvable": unres, "still_waiting": waiting}

    new_pending, stats["pending"] = _grade_list(pending, "pending")
    new_manual, stats["manual"] = _grade_list(manual, "manual review")

    # Quarantine: rows parked as UNRESOLVABLE. Look each one up again; Oct 9+ rows
    # with a real result are graded into the ledger, the rest are labelled honestly.
    qstats = {"graded": 0, "before_window": 0, "no_source": 0, "non_completion_kept": 0, "already_labelled": 0}
    for q in quar:
        rt = q.get("result_type")
        if rt in (UNRESOLVABLE, BEFORE_WINDOW, GRADED_LATE, "PUSH"):
            qstats["already_labelled"] += 1
            continue
        if rt == "NON_COMPLETION":
            qstats["non_completion_kept"] += 1
            continue
        m = re.match(r"(MORE|LESS)\s+([\d.]+)\s+(\S+)", q.get("pick") or "")
        if not m:
            qstats["no_source"] += 1
            if not dry:
                q["result_type"] = UNRESOLVABLE
                q["signal"] = "unresolvable — no result source (unreadable pick)"
            continue
        direction, line, stat = m.group(1), m.group(2), m.group(3)
        st, info = lookup_result(conn, q.get("player"), q.get("opponent"), stat, line, direction,
                                 match_date=q.get("match_date"), near_date=q.get("date"))
        md = (info or {}).get("match_date") or q.get("match_date") or q.get("date")
        if md and md < LEARN_START:
            qstats["before_window"] += 1
            if not dry:
                q["result_type"] = BEFORE_WINDOW
                q["old_signal"] = q.get("signal")
                q["signal"] = "before the Oct 9 learning window — not used"
            continue
        if st in ("HIT", "BUST"):
            qstats["graded"] += 1
            if not dry:
                p = {"player": q.get("player"), "opponent": (info or {}).get("opponent"), "stat_type": stat,
                     "line": float(line), "direction": direction, "match_date": md,
                     "prop_type_classified": (info or {}).get("odds_type")}
                _ledger_add(ledger, keys, p, st, info, "props_db (quarantine)")
                q["result_type"] = GRADED_LATE
                q["old_signal"] = q.get("signal")
                q["signal"] = "graded from props_db: %s" % st
            continue
        qstats["no_source"] += 1
        if not dry:
            q["result_type"] = UNRESOLVABLE
            q["old_signal"] = q.get("signal")
            q["signal"] = "unresolvable — no result source (%s)" % st.lower()
    stats["quarantine"] = qstats
    stats["ledger_after"] = len(ledger)
    if not dry:
        _save(PENDING_FILE, new_pending)
        _save(MANUAL_FILE, new_manual)
        _save(QUAR_FILE, quar)
        _save(LEDGER_FILE, ledger)
        health_log("catch-up: " + json.dumps(stats))
    conn.close()
    return stats


# ─────────────────────────── real numbers ───────────────────────────
def _tour_map():
    """player name -> 'ATP' / 'WTA' from the engine pick log's format label."""
    out = {}
    try:
        with open(PICK_LOG) as fh:
            for r in csv.DictReader(fh):
                f = (r.get("format_label") or "").lower()
                t = "WTA" if ("women" in f or "wta" in f) else ("ATP" if ("men's" in f or "atp" in f or " men " in f) else None)
                if not t:
                    continue
                names = [r.get("player"), r.get("opponent")]
                if r.get("player") == "match":
                    names = (r.get("opponent") or "").split(" v ")
                for n in names:
                    if n and n != "match":
                        out[_norm(n)] = t
    except Exception:
        pass
    return out


def settled_props(start, end, conn=None):
    """Playable, engine-scored PrizePicks props with a real result, one row per prop.
    Goblin/Demon LESS are left out (they can't be played)."""
    own = conn is None
    conn = conn or _conn()
    rows = conn.execute(
        "select * from projections where match_date between ? and ? and engine_prob is not null "
        "and engine_direction in ('MORE','LESS') and result in ('more','less')", (start, end)).fetchall()
    if own:
        conn.close()
    best = {}
    for r in rows:
        ot = (r["odds_type"] or "standard").lower()
        if ot in ("goblin", "demon") and r["engine_direction"] == "LESS":
            continue
        k = (r["match_date"], _norm(r["player"]), _norm(r["opponent"]), r["category"], ot,
             r["line_last"], r["engine_direction"])
        if k not in best or (r["last_seen"] or "") > (best[k]["last_seen"] or ""):
            best[k] = r
    out = []
    tours = None
    for r in best.values():
        d = dict(r)
        d["odds_type"] = (d["odds_type"] or "standard").lower()
        d["win"] = 1 if d["result"] == d["engine_direction"].lower() else 0
        d["boards_set"] = set((d["boards"] or "").split(",")) - {""}
        out.append(d)
    if out:
        tours = _tour_map()
        for d in out:
            d["tour"] = tours.get(_norm(d["player"])) or tours.get(_norm(d["opponent"])) or "?"
    return out


def _rate(rows):
    n = len(rows)
    w = sum(r["win"] for r in rows)
    exp = sum(r["engine_prob"] for r in rows) / n if n else 0
    return w, n, (w / n if n else 0), exp


def _line_band(r):
    c, ln = r["category"], r["line_last"] or 0
    if c == "total_games":
        return "under 19.5" if ln < 19.5 else ("19.5-21.5" if ln <= 21.5 else "22+")
    if c == "games_won":
        return "under 10.5" if ln < 10.5 else ("10.5-12" if ln <= 12 else "12.5+")
    if c == "fantasy_score":
        return "under 10" if ln < 10 else ("10-17.5" if ln <= 17.5 else "18+")
    return "all"


def _wp_band(p):
    return "70-75%" if p < 0.75 else ("75-80%" if p < 0.80 else "80%+")


def breakdown(rows):
    """Hit rates for 70%+ picks by stat+side, line band, side, tour, Win Prob band, board."""
    hi = [r for r in rows if r["engine_prob"] >= FLOOR]
    groups = {"stat": {}, "line": {}, "side": {}, "tour": {}, "wp": {}, "board": {}, "type": {}}
    for r in hi:
        groups["stat"].setdefault((r["category"], r["engine_direction"]), []).append(r)
        groups["line"].setdefault((r["category"], r["engine_direction"], _line_band(r)), []).append(r)
        groups["side"].setdefault(r["engine_direction"], []).append(r)
        groups["tour"].setdefault(r["tour"], []).append(r)
        groups["wp"].setdefault(_wp_band(r["engine_prob"]), []).append(r)
        groups["type"].setdefault(r["odds_type"], []).append(r)
        for b in r["boards_set"]:
            groups["board"].setdefault(b, []).append(r)
    return hi, {g: {k: _rate(v) + (v,) for k, v in d.items()} for g, d in groups.items()}


def _pct(x):
    return "%d%%" % round(100 * x)


def _stat_label(cat, side):
    return "%s %s" % (CAT_NAME.get(cat, cat), side)


# ─────────────────────────── loss reasons ───────────────────────────
def _total_games(score):
    try:
        return sum(int(a) + int(b) for a, b in (s.split("(")[0].split("-") for s in score.split(",")))
    except Exception:
        return None


def loss_reason(r):
    """One plain sentence on why a board pick lost, from the real box score."""
    ln, act, side = r["line_last"], r["actual"], r["engine_direction"]
    score = (r["score"] or "").replace(",", " ")
    cat = r["category"]
    unit = {"total_games": "games in the match", "games_won": "games won", "fantasy_score": "fantasy points",
            "aces": "aces", "dfs": "double faults", "breaks": "breaks", "tiebreaks": "tiebreaks",
            "total_sets": "sets"}.get(cat, "")
    gap = None
    if act is not None and ln is not None:
        gap = abs(act - ln)
    act_s = ("%g" % act) if act is not None else "?"
    bits = ["%s %s" % (act_s, unit)]
    if gap is not None:
        bits.append("needed %s %g" % ("over" if side == "MORE" else "under", ln))
    why = ""
    tg = _total_games(r["score"] or "")
    if side == "MORE" and cat in ("total_games", "games_won", "fantasy_score") and tg is not None and tg <= 16:
        why = " · one-sided match"
    elif side == "LESS" and cat in ("total_games", "games_won") and r["score"] and r["score"].count(",") >= 2:
        why = " · went to a 3rd set"
    elif gap is not None and gap <= 1.5:
        why = " · just missed"
    return "%s (%s)%s" % (", ".join(bits), score or "no score", why)


# ─────────────────────────── suggestions (proposals only) ───────────────────────────
def _category_gate_holds():
    """stat|SIDE pairs the hourly category gate is already holding off the Best boards."""
    st = _load(os.path.join(HERE, "data", "logs", "category_gate.json"), {})
    return {str(k) for k in (st.get("blocked") or [])} if isinstance(st, dict) else set()


def build_suggestions(rows14, rows7, today):
    """Proposals only. Never lowers a floor; never applied without owner's go."""
    hi14 = [r for r in rows14 if r["engine_prob"] >= FLOOR]
    out = []
    held = _category_gate_holds()
    by_stat = {}
    for r in hi14:
        by_stat.setdefault((r["category"], r["engine_direction"]), []).append(r)
    for (cat, side), rs in sorted(by_stat.items(), key=lambda kv: -len(kv[1])):
        w, n, hit, exp = _rate(rs)
        band = [r for r in rs if r["engine_prob"] < 0.75]
        top = [r for r in rs if r["engine_prob"] >= 0.75]
        wk_board = [r for r in rows7 if r["category"] == cat and r["engine_direction"] == side
                    and r["engine_prob"] >= FLOOR and r["boards_set"] & {"top3", "elite6"}]
        label = _stat_label(cat, side)
        # A) whole stat+side is far below what the engine promised -> hold it off the boards
        if n >= 25 and hit < 0.60 and hit < exp - 0.10:
            if "%s|%s" % (cat, side) in held:
                continue   # the hourly category gate already holds it
            bw = sum(r["win"] for r in wk_board)
            out.append({
                "rule": "hold:%s:%s" % (cat, side),
                "text": "Hold %s off Top 3 / Elite 6 — 70%%+ picks hit %s (%d-%d) over 14 days, engine said %s"
                        % (label, _pct(hit), w, n - w, _pct(exp)),
                "backtest": "would have taken out %d Top 3/Elite 6 picks this week (%d won, %d lost)"
                            % (len(wk_board), bw, len(wk_board) - bw),
                "n": n})
            continue
        # B) only the 70-75% slice is weak -> raise this stat's floor to 75% (never lower)
        if len(band) >= 20 and len(top) >= 15:
            bw_, bn_, bh, be = _rate(band)
            tw_, tn_, th, te = _rate(top)
            if bh < 0.60 and th >= 0.70 and bh < be - 0.08:
                wk_band = [r for r in wk_board if r["engine_prob"] < 0.75]
                kw = sum(r["win"] for r in wk_band)
                out.append({
                    "rule": "floor75:%s:%s" % (cat, side),
                    "text": "Raise the %s floor from 70%% to 75%% — its 70-75%% picks hit %s (%d-%d), its 75%%+ picks hit %s (%d-%d)"
                            % (label, _pct(bh), bw_, bn_ - bw_, _pct(th), tw_, tn_ - tw_),
                    "backtest": "would have taken out %d Top 3/Elite 6 picks this week (%d won, %d lost)"
                                % (len(wk_band), kw, len(wk_band) - kw),
                    "n": bn_})
    out = out[:3]
    # keep numbering stable across nights: same rule keeps its number until it's decided
    saved = _load(SUGGEST_FILE, {"next": 1, "items": []})
    live = {s["rule"]: s for s in saved["items"] if s.get("status") == "WAITING"}
    tonight_rules = set()
    for s in out:
        tonight_rules.add(s["rule"])
        if s["rule"] in live:
            live[s["rule"]].update(text=s["text"], backtest=s["backtest"], last_seen=today)
            s["id"] = live[s["rule"]]["id"]
        else:
            s.update(id=saved["next"], status="WAITING", created=today, last_seen=today)
            saved["next"] += 1
            saved["items"].append(dict(s))
    for s in saved["items"]:
        if s.get("status") == "WAITING" and s["rule"] not in tonight_rules:
            s["status"] = "EXPIRED (evidence went away %s)" % today
    _save(SUGGEST_FILE, saved)
    return out


# ─────────────────────────── the nightly card ───────────────────────────
def _queue_counts():
    pending = _load(PENDING_FILE, [])
    manual = _load(MANUAL_FILE, [])
    quar = _load(QUAR_FILE, [])
    no_src = sum(1 for q in quar if q.get("result_type") in (UNRESOLVABLE, "UNRESOLVABLE"))
    old = sum(1 for q in quar if q.get("result_type") == BEFORE_WINDOW)
    return len(pending) + len(manual), no_src, old, len(quar)


def write_report(day, g, days):
    """Full numbers behind the card (every group, not just the top few)."""
    out = ["# Apex learning numbers — %s" % day, "",
           "70%%+ Win Prob picks, last %d days (Oct 9 or later), playable props only, from data/props_db.sqlite." % days,
           "Hit = won-lost; 'engine' = average Win Prob the engine gave those picks.", ""]
    names = {"stat": "Stat + side", "line": "Stat + side + line band", "side": "Side", "tour": "Tour",
             "wp": "Win Prob band", "board": "Board (picks that made it)", "type": "Pick type"}
    for grp, title in names.items():
        out += ["## " + title, "", "| group | record | hit | engine |", "|---|---|---|---|"]
        for k, v in sorted(g[grp].items(), key=lambda kv: -kv[1][1]):
            lbl = " ".join(str(x) for x in k) if isinstance(k, tuple) else str(k)
            lbl = BOARD_NAME.get(lbl, lbl)
            out.append("| %s | %d-%d | %s | %s |" % (lbl, v[0], v[1] - v[0], _pct(v[2]), _pct(v[3])))
        out.append("")
    path = os.path.join(HERE, "apex_learning_system", "reports", "learning_numbers_%s.md" % day)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(out))
    return path


def build_card(day=None):
    """Returns (title, description, footer, plain_text) for the night's card."""
    day = day or date.today().isoformat()
    d = date.fromisoformat(day)
    conn = _conn()
    tonight = settled_props(day, day, conn)
    yday = settled_props((d - timedelta(days=1)).isoformat(), (d - timedelta(days=1)).isoformat(), conn)
    rows14 = settled_props(max(LEARN_START, (d - timedelta(days=13)).isoformat()), day, conn)
    rows7 = settled_props((d - timedelta(days=6)).isoformat(), day, conn)
    conn.close()

    lines = []
    # ✅ graded tonight (today's matches; yesterday's late finishers counted with them)
    hi_t = [r for r in tonight if r["engine_prob"] >= FLOOR]
    w, n, hit, exp = _rate(hi_t)
    lines.append("✅ **Graded today:** %d picks at 70%%+ (%d won, %d lost · %s)" % (n, w, n - w, _pct(hit)) if n
                 else "✅ **Graded today:** no 70%+ picks settled yet")
    bl = []
    for b in ("top3", "elite6"):
        rs = [r for r in tonight if b in r["boards_set"]]
        if rs:
            bw = sum(r["win"] for r in rs)
            bl.append("%s %d-%d" % (BOARD_NAME[b], bw, len(rs) - bw))
    if bl:
        lines.append("🏅 **Boards today:** " + " · ".join(bl))

    # 📊 what's working / ⚠️ what's slipping (last 14 days, 70%+ picks)
    hi14, g = breakdown(rows14)
    days = (d - max(date.fromisoformat(LEARN_START), d - timedelta(days=13))).days + 1
    try:
        write_report(day, g, days)
    except Exception as e:
        health_log("report write failed: %s" % e)
    stat = [(k, v) for k, v in g["stat"].items() if v[1] >= 15]
    good = sorted([s for s in stat if s[1][2] >= max(0.70, s[1][3] - 0.03)], key=lambda s: -s[1][2])[:4]
    bad = sorted([s for s in stat if s[1][2] < s[1][3] - 0.08], key=lambda s: s[1][2])[:3]
    lines.append("")
    lines.append("📊 **What's working** (last %d days, 70%%+ picks):" % days)
    lines += ["%s %s **%s** (%d-%d)" % (CAT_EMOJI.get(k[0], "•"), _stat_label(*k), _pct(v[2]), v[0], v[1] - v[0])
              for k, v in good] or ["• nothing stands out yet"]
    lines.append("⚠️ **What's slipping:**")
    held = _category_gate_holds()
    lines += ["%s %s **%s** (%d-%d) · engine said %s%s" % (
        CAT_EMOJI.get(k[0], "•"), _stat_label(*k), _pct(v[2]), v[0], v[1] - v[0], _pct(v[3]),
        " · already held off the Best boards" if "%s|%s" % k in held else "") for k, v in bad] \
        or ["• nothing is falling short of what the engine promised"]

    # 📏 is the Win Prob honest?
    wp = g["wp"]
    if wp:
        lines.append("📏 **Win Prob check:** " + " · ".join(
            "%s picks hit %s" % (b, _pct(wp[b][2])) for b in ("70-75%", "75-80%", "80%+") if b in wp and wp[b][1] >= 10))
    tb = [("%s %s" % (BOARD_NAME[b], _pct(g["board"][b][2])), g["board"][b]) for b in ("top3", "elite6")
          if b in g["board"] and g["board"][b][1] >= 10]
    if tb:
        lines.append("🏆 **Board record (%d days):** " % days + " · ".join(
            "%s (%d-%d)" % (lbl, v[0], v[1] - v[0]) for lbl, v in tb))
    tours = [(t, g["tour"][t]) for t in ("ATP", "WTA") if t in g["tour"] and g["tour"][t][1] >= 15]
    if tours:
        lines.append("🌍 **By tour:** " + " · ".join("%s %s (%d-%d)" % (t, _pct(v[2]), v[0], v[1] - v[0]) for t, v in tours))

    # ❌ board losses tonight, one plain line each (Top 3 / Elite 6 only)
    losers = [r for r in tonight if (r["boards_set"] & {"top3", "elite6"}) and not r["win"]]
    if losers:
        lines.append("")
        lines.append("❌ **Board picks that lost today:**")
        for r in losers[:8]:
            lines.append("• %s %s %g %s (%s, %s) — %s" % (
                r["player"], r["engine_direction"], r["line_last"], CAT_NAME.get(r["category"], r["category"]),
                r["odds_type"].title(), _pct(r["engine_prob"]), loss_reason(r)))
        if len(losers) > 8:
            lines.append("…and %d more" % (len(losers) - 8))

    # 💡 suggestions (need his go)
    sugg = build_suggestions(rows14, rows7, day)
    lines.append("")
    lines.append("💡 **Suggestions (need your go):**")
    if sugg:
        for s in sugg:
            lines.append("%d) %s — %s. Say **go learn %d**" % (s["id"], s["text"], s["backtest"], s["id"]))
    else:
        lines.append("• none tonight — no stat is far enough off to change anything")

    # 🗂️ housekeeping (one line; internal counters go to the log file)
    waiting, no_src, old_rows, qtotal = _queue_counts()
    lines.append("")
    lines.append("🗂️ **Housekeeping:** %d picks still waiting on results · %d can't be graded (no result source)"
                 " · %d from before Oct 9 set aside" % (waiting, no_src, old_rows))

    title = "🧠 NIGHTLY LEARNING · %s" % d.strftime("%a %-m/%-d")
    footer = "Apex Learning · 11:30 PM CT · nothing changes without your go"
    desc = "\n".join(lines)
    health_log("card %s: graded_today=%d (%d-%d) 14d_70plus=%d suggestions=%d waiting=%d no_source=%d quarantine_total=%d"
               % (day, n, w, n - w, len(hi14), len(sugg), waiting, no_src, qtotal))
    return title, desc, footer, "\n".join([title, "", desc, "", footer])


def _webhook(channel="learning"):
    try:
        main = sys.modules.get("__main__")   # inside the scheduler, openclaw_core is __main__
        if getattr(main, "DISCORD_WEBHOOKS", None):
            return main.DISCORD_WEBHOOKS.get(channel)
        import openclaw_core as core   # webhooks live there; never printed
        return core.DISCORD_WEBHOOKS.get(channel)
    except Exception:
        return None


def post_card(day=None, force=False, dry=False):
    day = day or date.today().isoformat()
    state = _load(CARD_STATE, {})
    if state.get("posted") == day and not force and not dry:
        return "already posted for %s" % day
    title, desc, footer, plain = build_card(day)
    if dry:
        return plain
    import requests
    hook = _webhook()
    if not hook:
        return "no #learning webhook"
    embed = {"title": title, "description": desc[:4000], "color": 0x5865F2, "footer": {"text": footer}}
    r = requests.post(hook + "?wait=true", json={"embeds": [embed]}, timeout=15)
    ok = r.status_code in (200, 201, 204)
    if ok:
        state.update(posted=day, posted_at=datetime.now().isoformat(timespec="seconds"))
        _save(CARD_STATE, state)
    health_log("card post %s -> %s" % (day, r.status_code))
    return plain if ok else "post failed %s" % r.status_code


def nightly(day=None):
    """11:30 PM CT: catch up from props_db, then post the one card."""
    try:
        st = catch_up(dry=False)
    except Exception as e:
        health_log("catch-up failed: %s: %s" % (type(e).__name__, e))
        st = None
    return st, post_card(day)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    dry = "--dry" in sys.argv
    if cmd == "catchup":
        print(json.dumps(catch_up(dry=dry), indent=2))
    elif cmd == "card":
        print(post_card(dry=dry, force="--force" in sys.argv))
    elif cmd == "nightly":
        print(nightly())
    else:
        print(__doc__)
