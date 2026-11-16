#!/usr/bin/env python3
"""APEX-REALSKIP-2026-11-14 (owner, "Go real reason"): the REAL reason, match by match, for new PrizePicks tennis props that
were not scored, for the new-props card in #alerts (replaces the guessed "wait on a bet365 price or start too soon" and
"1st-set lines Apex doesn't score"). Read only: the pick log, the auto-eval retry list, the SportsGameOdds backup state, the first-set
log, props DB start times and the latest board post. Display only, never changes a pick. Never raises (returns [] on any error).
Reasons:
  - listed too close to the start (auto-eval skips a match starting within 5 minutes, or it had already started)
  - no bet365 match price yet (tried N times; Win Prob needs bet365; says when the backup total is already in)
  - bet365 still has no price after many tries (usually an event bet365 does not cover)
  - not tried yet (next automatic run within 30 minutes)
  - 1st-set lines: handled by the first-set rule, with how many passed
  - scored, but nothing made a board: the best number and why (held by rule X / under the bar)
Undo: ON = False (the card goes back to the old one-line count), restart the tennis bot."""
import csv, json, os, re, sqlite3, unicodedata
from datetime import datetime, timezone, timedelta

ON = True
HERE = os.path.dirname(os.path.abspath(__file__))
PICKS = os.path.join(HERE, "apex_engine_picks_log_v1.csv")
TRIED = os.path.join(HERE, "data", "logs", "auto_eval_tried.json")
SGO = os.path.join(HERE, "data", "logs", "sgo_backup_state.json")
SETG = os.path.join(HERE, "data", "logs", "set_games.json")
PROPS = os.path.join(HERE, "data", "props_db.sqlite")
BH = os.path.join(HERE, "data", "board_history.sqlite")
FIRSTSET = ("1st_set",)
MANY_TRIES = 6     # 6 hourly tries without a bet365 price = bet365 probably does not cover it
LEAD_MIN = 5


def _n(s):
    return " ".join(re.sub(r"[^a-z ]", "", unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower().replace("-", " ")).split())


def _j(p, d):
    try:
        with open(p) as fh:
            return json.load(fh)
    except Exception:
        return d


def _scored(keys, since):
    want = {}
    for k in keys:
        pl, opp, slug, line = k.split("|")[:4]
        want[(pl, slug, float(line))] = k
    hit = set()
    with open(PICKS, newline="") as fh:
        for r in csv.DictReader(fh):
            if (r.get("ts") or "") <= since:
                continue
            try:
                line = float(r.get("line"))
            except (TypeError, ValueError):
                continue
            names = str(r.get("opponent") or "").split(" v ") if r.get("player") == "match" else [r.get("player")]
            for nm in names:
                k = want.get((_n(nm), str(r.get("stat_type") or ""), line))
                if k:
                    hit.add(k)
    return hit


def _start(a, b):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % PROPS, uri=True)
        best = None
        for pl, op, st in con.execute("select player, opponent, start_utc from projections where start_utc >= ? and player not like '%/%'",
                                       ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),)):
            if {_n(pl), _n(str(op).replace(" 1st Set", ""))} == {a, b} and st:
                best = min(best or st, st)
        con.close()
        if best:
            t = datetime.fromisoformat(best.replace("Z", "+00:00"))
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return None


def _tries(a, b):
    n, first = 0, None
    for k, v in (_j(TRIED, {}) or {}).items():
        parts = k.split("|")
        if len(parts) >= 3 and {parts[1], parts[2]} == {a, b}:
            n = max(n, int(v.get("n") or 0))
            first = min(first or v.get("last") or "", v.get("last") or "")
    return n


def _backup_total(a, b):
    for k, v in (_j(SGO, {}) or {}).items():
        nm = k.split("|", 1)[-1].lower().split(" v ")
        if len(nm) == 2 and {_n(nm[0]), _n(nm[1])} == {a, b} and (v or {}).get("total"):
            return v["total"]
    return None


def _firstset(a, b):
    """(lines logged by the first-set rule, how many passed it) for this match, last 2 days."""
    since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    got = passed = 0
    for v in ((_j(SETG, {}) or {}).get("picks") or {}).values():
        if {_n(v.get("a")), _n(v.get("b"))} == {a, b} and str(v.get("start") or "") >= since:
            got += 1
            passed += 1 if int(v.get("rule") or 0) >= 1 else 0
    return got, passed


def _best_on_post(a, b):
    """Best scored prop of this match in the latest board post: (win prob, words, why held/off) or None."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % BH, uri=True)
        pid = con.execute("select max(publish_id) from history where board='allprops'").fetchone()[0]
        best = None
        for pl, op, cat, line, dr, pt, wp, kind, reason in con.execute(
                "select player, opponent, category, line, direction, prop_type, win_prob, spot_kind, reason from history where publish_id=? and board='allprops'", (pid,)):
            names = {_n(x) for x in str(op).split(" v ")} if pl == "match" else {_n(pl), _n(op)}
            if names != {a, b}:
                continue
            w = float(wp or 0); w = w / 100 if w > 100 else w * 100 if w <= 1 else w
            if best is None or w > best[0]:
                who = ("match" if pl == "match" else str(pl).split()[-1])
                best = (w, "%s %s %s %g %s" % (who, str(cat).replace("_", " ").title(), (pt or "standard").title(), line, dr), str(reason or kind or ""))
        con.close()
        return best
    except Exception:
        return None


def lines(data, since, on_board_names=()):
    """Per-match reason lines for the new-props card. data = the card's data (keys + matches). Never raises."""
    if not ON:
        return []
    try:
        keys = list(data.get("keys") or [])
        hit = _scored(keys, since)
        now = datetime.now(timezone.utc)
        out = []
        for pair, m in sorted((data.get("matches") or {}).items(), key=lambda kv: kv[1].get("label") or ""):
            ks = list(m.get("keys") or [])
            ab = pair.split("|")
            if len(ab) != 2:
                continue
            a, b = _n(ab[0]), _n(ab[1])
            fs = [k for k in ks if k.split("|")[2].startswith(FIRSTSET)]
            rest = [k for k in ks if k not in fs]
            un = [k for k in rest if k not in hit]
            sc = [k for k in rest if k in hit]
            label = m.get("label") or pair
            parts = []
            if un:
                st = _start(a, b); tries = _tries(a, b); bt = _backup_total(a, b)
                if st is not None and (st - now).total_seconds() < LEAD_MIN * 60 and not tries:
                    why = "listed too close to the start (%s), never scored" % ("already started" if st <= now else "starts within 5 min")
                elif tries >= MANY_TRIES:
                    why = "bet365 still has no match price after %d tries — bet365 usually doesn't cover this event" % tries
                elif tries:
                    why = "no bet365 match price yet (tried %d time%s, tries again every hour)" % (tries, "" if tries == 1 else "s")
                else:
                    why = "not tried yet — the next automatic run (within 30 min) scores it"
                if bt and tries:
                    why += "; the backup total (%g) is in, but Win Prob needs bet365's match price" % bt
                parts.append("%d not scored: %s" % (len(un), why))
            if fs:
                got, passed = _firstset(a, b)
                parts.append("%d 1st-set line%s: handled by the first-set rule, %s" % (
                    len(fs), "" if len(fs) == 1 else "s", ("%d of %d passed" % (passed, got)) if passed else "none passed"))
            if sc and not ({a, b} & set(on_board_names or ())):
                bp = _best_on_post(a, b)
                if bp:
                    parts.append("%d scored, none on a board: best %d%% (%s)%s" % (len(sc), round(bp[0]), bp[1], (" — " + bp[2][:90]) if bp[2] else ""))
                else:
                    parts.append("%d scored" % len(sc))
            elif sc:
                parts.append("%d scored" % len(sc))
            if parts:
                out.append("• %s: %s" % (label, " · ".join(parts)))
        return out
    except Exception:
        return []


if __name__ == "__main__":
    d = (_j(os.path.join(HERE, "data", "logs", "alert_chains.json"), {}).get("newprops") or {})
    print("\n".join(lines(d.get("data") or {}, (datetime.now() - timedelta(days=2)).isoformat())))
