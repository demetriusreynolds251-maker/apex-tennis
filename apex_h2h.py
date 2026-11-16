#!/usr/bin/env python3
"""
APEX-H2H-2026-10-22 (owner: "H2h command go", "H2h card line go"): head-to-head
between two players from finished matches already stored in the warehouse
(data/warehouse/tennis_warehouse.sqlite, table matches -- SportsAPI365/V8 results plus
the historical CSV load). Display only: nothing here touches scoring, ranking or boards.

  h2h_command_text("Boulter")            -> /h2h card for her PrizePicks match today
  h2h_command_text("Boulter vs Golubic") -> /h2h card for that pair
  h2h_card_line(p)                       -> one 🤝 line for a board card (or None)

Switches: SHOW_H2H (the /h2h command), H2H_CARD_LINE (the line on board cards).
"""
import os, re, sqlite3, time, unicodedata
from datetime import datetime, timedelta, timezone

SHOW_H2H = True
H2H_CARD_LINE = True

HERE = os.path.dirname(os.path.abspath(__file__))
WAREHOUSE = os.path.join(HERE, "data", "warehouse", "tennis_warehouse.sqlite")
PROPS_DB = os.path.join(HERE, "data", "props_db.sqlite")
OVER_LINE = 17.5
CACHE_SECONDS = 30 * 60          # one lookup per pair per half hour (about one fetch)

_names = {"ts": 0.0, "by_norm": {}, "by_last": {}}
_pair_cache = {}


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z ]+", " ", s.lower().replace("-", " ").replace("'", ""))
    return " ".join(s.split())


def _clean(name):
    """PrizePicks opponent text can carry ' 1st Set' etc. -- keep just the name."""
    return re.split(r"\s+(1st|2nd|3rd)\b", str(name or "").strip())[0].strip()


def _last(name):
    return _clean(name).split()[-1] if _clean(name) else ""


def _history():
    """APEX-PLAYERDATA-2026-10-31: the filled match history when its switch is on, else the warehouse."""
    try:
        import apex_player_history as _ph
        return _ph.path(WAREHOUSE)
    except Exception:
        return WAREHOUSE


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)


def _name_index():
    if time.time() - _names["ts"] < 6 * 3600 and _names["by_norm"]:
        return _names
    by_norm, by_last = {}, {}
    for src in {WAREHOUSE, _history()}:   # APEX-PLAYERDATA-2026-10-31: older meetings stay in the warehouse
        con = _ro(src)
        try:
            for (n,) in con.execute("SELECT player_a FROM matches UNION SELECT player_b FROM matches"):
                k = _norm(n)
                if not k or n.startswith("~"):
                    continue
                by_norm.setdefault(k, set()).add(n)
                by_last.setdefault(k.split()[-1], set()).add(n)
        finally:
            con.close()
    if _history() != WAREHOUSE:   # spellings of one player answer together
        con = _ro(_history())
        try:
            groups = {}
            for n, g in con.execute("SELECT name, grp FROM variants"):
                groups.setdefault(g, set()).add(n)
        finally:
            con.close()
        for ns in groups.values():
            for n in ns:
                k = _norm(n)
                if k:
                    by_norm.setdefault(k, set()).update(ns)
    _names.update(ts=time.time(), by_norm=by_norm, by_last=by_last)
    return _names


def resolve(name):
    """Warehouse names for one person. Full name: exact (accents/case/hyphens ignored),
    else same last name + same first initial. One word: everyone with that last name."""
    idx = _name_index()
    k = _norm(_clean(name))
    if not k:
        return []
    if k in idx["by_norm"]:
        return sorted(idx["by_norm"][k])
    toks = k.split()
    if len(toks) == 1:
        hits = set(idx["by_last"].get(toks[0], set()))
        for nk, ns in idx["by_norm"].items():       # two-word last names ("de Minaur")
            if nk.endswith(" " + toks[0]) or nk == toks[0]:
                hits |= ns
        return sorted(hits)
    hits = set()
    for n in idx["by_last"].get(toks[-1], set()):
        if _norm(n)[:1] == toks[0][:1]:
            hits.add(n)
    if not hits:   # multi-word last name typed alone, e.g. "auger aliassime"
        for nk, ns in idx["by_norm"].items():
            if nk.endswith(" " + k):
                hits |= ns
    return sorted(hits)


def _games(score):
    s = re.sub(r"\(\d+\)", "", str(score or ""))
    sets = re.findall(r"(\d+)-(\d+)", s)
    return sum(int(a) + int(b) for a, b in sets) if sets else None


def _place(t):
    t = str(t or "").strip()
    return t.rsplit(" - ", 1)[-1].strip() if " - " in t else t


def meetings(names_a, names_b):
    """Finished meetings between two players, newest first, duplicates (same match in two
    sources, dated up to 3 weeks apart) and walkovers dropped."""
    if not names_a or not names_b:
        return []
    qa, qb = ",".join("?" * len(names_a)), ",".join("?" * len(names_b))
    sql = ("SELECT match_date, player_a, player_b, winner, tournament, score, total_games, retired "
           "FROM matches WHERE winner IS NOT NULL AND ((player_a IN (%s) AND player_b IN (%s)) "
           "OR (player_a IN (%s) AND player_b IN (%s))) ORDER BY match_date DESC" % (qa, qb, qb, qa))
    rows = []
    for src in {WAREHOUSE, _history()}:   # APEX-PLAYERDATA-2026-10-31: duplicates across the two drop below
        con = _ro(src)
        try:
            rows += con.execute(sql, list(names_a) + list(names_b) + list(names_b) + list(names_a)).fetchall()
        finally:
            con.close()
    rows.sort(key=lambda r: str(r[0]), reverse=True)
    out = []
    for d, pa, pb, w, tour, score, tg, ret in rows:
        sc = str(score or "").strip()
        if not sc or "w/o" in sc.lower() or "walkover" in sc.lower():
            continue
        retired = bool(ret) or "ret" in sc.lower() or "def" in sc.lower()
        g = _games(sc)
        if tg and not retired:
            g = int(tg)
        try:
            dt = datetime.strptime(str(d)[:10], "%Y-%m-%d")
        except Exception:
            continue
        a_won = w in names_a
        if any(abs((dt - m["date"]).days) <= 21 and m["a_won"] == a_won and
               re.sub(r"\D", "", m["score"]) == re.sub(r"\D", "", sc) for m in out):
            continue
        out.append({"date": dt, "a_won": a_won, "winner": w, "place": _place(tour), "score": sc,
                    "games": g, "retired": retired})
    return out


def _pair(a, b):
    key = tuple(sorted([_norm(_clean(a)), _norm(_clean(b))]))
    hit = _pair_cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        ms = hit[1]
        flip = key[0] != _norm(_clean(a))
        return [dict(m, a_won=not m["a_won"]) for m in ms] if flip else ms
    ms = meetings(resolve(a), resolve(b))
    flip = key[0] != _norm(_clean(a))
    _pair_cache[key] = (time.time(), [dict(m, a_won=not m["a_won"]) for m in ms] if flip else ms)
    return ms


def _summary(a, b, ms, n=5):
    """(record text, games text) -- games uses the last n finished (not retired) meetings."""
    la, lb = _last(a), _last(b)
    wa = sum(1 for m in ms if m["a_won"])
    wb = len(ms) - wa
    full = [m for m in ms if m["games"] is not None and not m["retired"]][:n]
    avg = round(sum(m["games"] for m in full) / len(full)) if full else None
    over = sum(1 for m in full if m["games"] > OVER_LINE)
    return la, lb, wa, wb, full, avg, over


def h2h_card_line(p):
    """One line for a board card. None when switched off or the names are unknown."""
    if not H2H_CARD_LINE:
        return None
    try:
        if p.get("player") == "match":
            parts = re.split(r"\s+vs?\.?\s+", str(p.get("opponent") or ""), maxsplit=1)
            if len(parts) != 2:
                return None
            a, b = parts
        else:
            a, b = p.get("player"), p.get("opponent")
        a, b = _clean(a), _clean(b)
        if not a or not b:
            return None
        ms = _pair(a, b)
        if not ms:
            return "🤝 Head-to-head: never played each other (on file)"
        if len(ms) == 1:
            m = ms[0]
            g = " (%d games%s)" % (m["games"], ", retired" if m["retired"] else "") if m["games"] is not None else ""
            return "🤝 Head-to-head: one past meeting, %s won %s%s" % (_last(m["winner"]), m["score"], g)
        la, lb, wa, wb, full, avg, over = _summary(a, b, ms)
        lead = ("%s leads %d-%d" % (la, wa, wb)) if wa > wb else \
               ("%s leads %d-%d" % (lb, wb, wa)) if wb > wa else ("series tied %d-%d" % (wa, wb))
        if not full:
            return "🤝 Head-to-head: %s" % lead
        kind = "completed meetings" if any(m["retired"] for m in ms[:5]) else "meetings"
        return "🤝 Head-to-head: %s, last %d %s averaged %d games (%d of %d over %s)" % (
            lead, len(full), kind, avg, over, len(full), OVER_LINE)
    except Exception:
        return None


def _start_line(start_utc):
    try:
        from zoneinfo import ZoneInfo
        st = datetime.fromisoformat(str(start_utc).replace("Z", "+00:00"))
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        ct = st.astimezone(ZoneInfo("America/Chicago"))
        return "🕒 Starts %s %d/%d, %d:%02d %s CT" % (
            ct.strftime("%a"), ct.month, ct.day, ct.hour % 12 or 12, ct.minute, "AM" if ct.hour < 12 else "PM")
    except Exception:
        return "🕒 Start time not listed yet"


def todays_matches():
    """Distinct PrizePicks matches not finished yet: [(player, opponent, start_utc)]."""
    lo = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(timespec="seconds")
    hi = (datetime.now(timezone.utc) + timedelta(hours=36)).isoformat(timespec="seconds")
    con = _ro(PROPS_DB)
    try:
        rows = con.execute("SELECT player, opponent, MIN(start_utc) FROM projections "
                           "WHERE start_utc >= ? AND start_utc <= ? AND player IS NOT NULL "
                           "AND opponent IS NOT NULL AND player != 'match' GROUP BY player, opponent",
                           (lo, hi)).fetchall()
    finally:
        con.close()
    seen, out = set(), []
    for pl, op, st in sorted(rows, key=lambda r: r[2] or ""):
        op = _clean(op)
        k = (pl, op)
        if k in seen:
            continue
        seen.add(k)
        out.append((pl, op, st))
    return out


def _matches_name(query, full):
    q, f = _norm(query), _norm(full)
    if not q:
        return False
    return q == f or f.endswith(" " + q) or (len(q.split()) > 1 and q.split()[-1] == f.split()[-1]
                                             and q[:1] == f[:1])


def _card(a, b, start_utc=None):
    ms = _pair(a, b)
    L = ["🤝 Head-to-head: %s v %s" % (a, b)]
    if start_utc:
        L.append(_start_line(start_utc))
    if not ms:
        L.append("No past meetings on file")
        return "\n".join(L)
    L.append("Past meetings on file (newest first, up to 5):")
    for m in ms[:5]:
        g = " -- %d games" % m["games"] if m["games"] is not None else ""
        if m["retired"]:
            g += " (retired)"
        place = " %s" % m["place"] if m["place"] else ""
        L.append("• %d/%d/%s%s -- %s won %s%s" % (m["date"].month, m["date"].day, m["date"].strftime("%y"),
                                                place, _last(m["winner"]), m["score"], g))
    if len(ms) > 5:
        L.append("(%d meetings on file in all)" % len(ms))
    la, lb, wa, wb, full, avg, over = _summary(a, b, ms)
    rec = "Record: %s %d - %s %d" % (la, wa, lb, wb)
    if len(ms) >= 2 and full:
        rec += " · average %d games · %d of %d went over %s" % (avg, over, len(full), OVER_LINE)
    L.append(rec)
    return "\n".join(L)


def _last_played(names):
    con = _ro(_history())
    try:
        out = {}
        for n in names:
            r = con.execute("SELECT MAX(match_date) FROM matches WHERE player_a = ? OR player_b = ?",
                            (n, n)).fetchone()
            out[n] = str(r[0] or "")
        return out
    finally:
        con.close()


def _pick_one(query, partner=None):
    """Resolve a typed name to one warehouse name, or (None, message). With several
    candidates: the one who has played the partner, else the one still playing
    (played in the last 2 years while the others have not)."""
    c = resolve(query)
    if len(c) == 1:
        return c[0], None
    if not c:
        return None, "No player found on file for \"%s\"" % query
    exact = [n for n in c if _norm(n) == _norm(query)]
    if len(exact) == 1:
        return exact[0], None
    if partner:
        pn = resolve(partner)
        met = set()
        if pn:
            q = ",".join("?" * len(pn))
            sql = ("SELECT 1 FROM matches WHERE (player_a = ? AND player_b IN (%s)) "
                   "OR (player_b = ? AND player_a IN (%s)) LIMIT 1" % (q, q))
            con = _ro(_history())
            try:
                met = {n for n in c if con.execute(sql, [n] + pn + [n] + pn).fetchone()}
            finally:
                con.close()
        if len(met) == 1:
            return met.pop(), None
    lp = _last_played(c)
    cutoff = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
    active = [n for n in c if lp.get(n, "") >= cutoff]
    if len(active) == 1:
        return active[0], None
    pool = active or c
    return None, ("More than one player matches \"%s\": %s. Type the full name." %
                  (query, ", ".join(pool[:6]) + (" ..." if len(pool) > 6 else "")))


def h2h_command_text(arg):
    if not SHOW_H2H:
        return "/h2h is switched off"
    arg = " ".join(str(arg or "").split())
    if not arg:
        return "Type /h2h <player> or /h2h <player A> vs <player B>"
    try:
        parts = re.split(r"\s+(?:vs\.?|v\.?)\s+", arg, maxsplit=1, flags=re.I)
        if len(parts) == 2:
            a_q, b_q = parts
            # a pair on today's board keeps the PrizePicks names and start time
            for pl, op, st in todays_matches():
                if (_matches_name(a_q, pl) and _matches_name(b_q, op)) or \
                   (_matches_name(a_q, op) and _matches_name(b_q, pl)):
                    a, b = (pl, op) if _matches_name(a_q, pl) else (op, pl)
                    return _card(a, b, st)
            a, ma = _pick_one(a_q, b_q)
            b, mb = _pick_one(b_q, a or a_q)
            if ma or mb:
                return "\n".join(x for x in (ma, mb) if x)
            return _card(a, b)
        hits = [(pl, op, st) for pl, op, st in todays_matches() if _matches_name(arg, pl)]
        people = sorted({pl for pl, _, _ in hits})
        if not hits:
            return ("No PrizePicks match today for %s; type /h2h %s vs <opponent> to look up a pair"
                    % (arg, arg))
        if len(people) > 1:
            return ("More than one player today matches \"%s\": %s. Type the full name."
                    % (arg, ", ".join(people)))
        pl, op, st = hits[0]
        return _card(pl, op, st)
    except Exception as e:
        return "head-to-head unavailable (%s: %s)" % (type(e).__name__, e)


if __name__ == "__main__":
    import sys
    print(h2h_command_text(" ".join(sys.argv[1:])))
