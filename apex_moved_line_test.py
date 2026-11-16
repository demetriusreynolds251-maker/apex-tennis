#!/usr/bin/env python3
"""
APEX-PULLEDNEWLINE-2026-10-22 (owner, "Go new line but tell me it's the new line"):
grades the picks that took a pulled numbered spot -- the same pick kept at PrizePicks' new
line vs the backup that moved up -- from data/logs/pulled_lines.csv (written by
apex_edge_tracker._pulled_record), using the settled results in the props database.

  summary()      -> the 9 AM #system status line (apex_alerts.daily)
  finish_check() -> TEST FINISHED card once 40 moved-line picks are graded

One pick per pulled line per day (a pick on several boards counts once). Reads only.
"""
import csv, os, re, sqlite3
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PULLED_LINES_LOG = os.path.join(HERE, "data", "logs", "pulled_lines.csv")
PROPS_DB = os.path.join(HERE, "data", "props_db.sqlite")
FINISH_PICKS = 40
START = "2026-10-22"
_LABELS = {"Total Games": "total_games", "Fantasy Score": "fantasy_score", "Games Won": "games_won",
           "Aces": "aces", "Double Faults": "dfs", "Breaks": "breaks", "Games Lost": "games_lost",
           "Sets Won": "sets_won", "Total Sets": "total_sets", "Tiebreaks": "tiebreaks"}


def _rows():
    try:
        with open(PULLED_LINES_LOG, newline="") as fh:
            return list(csv.DictReader(fh))
    except Exception:
        return []


def _names(player, opponent):
    if str(player).strip().lower() == "match":
        return {x.strip().lower() for x in str(opponent or "").split(" v ") if x.strip()}, True
    return {str(player or "").strip().lower()}, False


def _parse_label(txt):
    """'Ann Li v Lilli Tagger Total Games 18.5 MORE' -> (player, opponent, category, line, side) or None."""
    m = re.match(r"^(.*?) (%s) ([\d.]+) (MORE|LESS)$" % "|".join(map(re.escape, _LABELS)), str(txt or "").strip())
    if not m:
        return None
    who, cat = m.group(1), _LABELS[m.group(2)]
    if cat == "total_games":
        return ("match", who, cat, m.group(3), m.group(4))
    a, _, b = who.partition(" v ")
    return (a, b, cat, m.group(3), m.group(4))


def picks():
    """{"kept": {key: leg}, "backup": {key: leg}} -- leg = (match_date, player, opponent, category, line, side)."""
    out = {"kept": {}, "backup": {}}
    for r in _rows():
        if (r.get("match_date") or "") < START:
            continue
        oc = r.get("outcome") or ("backup" if r.get("replacement") else "empty")
        if oc == "new_line_kept" and r.get("new_line"):
            pl, opp = r.get("player"), (r.get("match") if r.get("player") == "match" else r.get("match", "").split(" v ", 1)[-1])
            leg = (r["match_date"], pl, opp, r.get("market"), r["new_line"], r.get("side"))
            out["kept"][leg] = leg
        elif oc == "backup":
            k = r.get("replacement_key")
            parts = k.split("|") if k else _parse_label(r.get("replacement"))
            if not parts or len(parts) != 5:
                continue
            leg = (r["match_date"],) + tuple(parts)
            out["backup"][leg] = leg
    # a pulled line that was later kept at the new line on a board is not also counted as a backup there
    return out


def _actual(con, leg):
    md, pl, opp, cat, line, side = leg
    names, is_match = _names(pl, opp)
    d0 = date.fromisoformat(md)
    days = [(d0 + timedelta(days=x)).isoformat() for x in (-1, 0, 1)]
    for p_, o_, act in con.execute(
            "select player, opponent, actual from projections where category = ? and actual is not null "
            "and result != 'void' and match_date in (?, ?, ?)", (cat, days[0], days[1], days[2])):
        p_l, o_l = str(p_ or "").lower(), str(o_ or "").lower()
        if (is_match and (p_l in names or o_l in names)) or (not is_match and p_l in names):
            return float(act)
    return None


def tally():
    """{"kept": [w, l, open], "backup": [w, l, open]}. Never raises."""
    T = {"kept": [0, 0, 0], "backup": [0, 0, 0]}
    P = picks()
    try:
        con = sqlite3.connect("file:%s?mode=ro" % PROPS_DB, uri=True)
    except Exception:
        con = None
    for arm in T:
        for leg in P[arm]:
            act = _actual(con, leg) if con else None
            if act is None:
                T[arm][2] += 1
                continue
            line = float(leg[4])
            if act == line:
                continue   # push
            hit = act > line if str(leg[5]).upper() == "MORE" else act < line
            T[arm][0 if hit else 1] += 1
    if con:
        con.close()
    return T


def _rec(x):
    n = x[0] + x[1]
    return ("%d-%d (%d out of 10)" % (x[0], x[1], round(10.0 * x[0] / n))) if n >= 5 else "%d-%d" % (x[0], x[1])


def summary():
    """The 9 AM #system status line. Never raises."""
    try:
        T = tally()
        if not any(sum(v) for v in T.values()):
            return ["Moved-line picks: none yet at the new line; backups that filled: none yet"]
        return ["Moved-line picks: %s at the new line; backups that filled: %s" % (_rec(T["kept"]), _rec(T["backup"])),
                "  (pulled after the fetch, one per line per day; %d + %d still open; finishes at %d graded moved-line picks)"
                % (T["kept"][2], T["backup"][2], FINISH_PICKS)]
    except Exception as e:
        return ["Moved-line picks: unavailable (%s)" % type(e).__name__]


def finish_check():
    T = tally()
    k, b = T["kept"], T["backup"]
    if k[0] + k[1] < FINISH_PICKS:
        return False, None
    rk = k[0] / float(k[0] + k[1])
    rb = b[0] / float(b[0] + b[1]) if b[0] + b[1] else None
    if rb is None or rk >= rb - 0.02:
        verdict = ("KEEP -- picks kept at the new line hit %d out of 100%s. Recommend keeping the new-line rule"
                   % (round(100 * rk), (" vs %d out of 100 for backups" % round(100 * rb)) if rb is not None else ""))
    else:
        verdict = ("DROP -- picks kept at the new line hit %d out of 100 vs %d out of 100 for backups. "
                   "Recommend going back to the backup fill (PULLED_LINE_PREFER_NEW_LINE = False)"
                   % (round(100 * rk), round(100 * rb)))
    rows = [("Test", "Pulled numbered pick: keep the same pick at PrizePicks' new line vs fill from BACKUPS (since %s)"
             % START[5:].replace("-", "/")),
            ("Kept at the new line", _rec(k)), ("Backups that filled", _rec(b)), ("Verdict", verdict),
            ("Decision", "keep the new-line rule / drop it (back to backups only) / keep testing")]
    return True, rows


if __name__ == "__main__":
    print("\n".join(summary()))
    print(picks())
