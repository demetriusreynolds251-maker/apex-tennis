#!/usr/bin/env python3
"""
APEX-CLV-2026-10-10 (owner, approved "go" 2026-10-10): closing-line
capture. Runs every 15 minutes from its own LaunchAgent
(com.apex.closingline). When any prop recommended on the latest publish
hasn't been captured yet, it makes ONE call to the PrizePicks partner API
(which also supplies the start time when the publish didn't have one) and,
for each such prop starting within the next 45 minutes, records:
  - the line it was recommended at, and that market's Standard line at the
    time of the publish (from apex_board_status_log_v1.csv)
  - the Standard line now (the "closing" line), and whether the exact
    recommended line is still offered
  - clv = beat / lost / same: did the Standard line move toward our side?
    (MORE: line went up = beat; LESS: line went down = beat)
Writes apex_closing_line_log_v1.csv. Read-only otherwise; never touches
boards. The daily board report summarises it.
"""
import csv, os, sys
from datetime import datetime, timezone, timedelta
import apex_ops as ops

HERE = ops.HERE
STATUS_LOG = os.path.join(HERE, "apex_board_status_log_v1.csv")
CLV_LOG = os.path.join(HERE, "apex_closing_line_log_v1.csv")
FIELDS = ["captured_at", "match_date", "player", "opponent", "category", "direction", "rec_prop_type",
          "rec_line", "rec_standard_line", "close_standard_line", "exact_line_still_offered",
          "minutes_to_start", "start_utc", "boards", "clv"]
WINDOW_MIN = 45
# PrizePicks stat slug -> board category (mirror of openclaw_core._APEX_STAT_MAP, read live)
try:
    STAT_MAP = ops._core_const("_APEX_STAT_MAP")
except Exception:
    STAT_MAP = {}


def _n(s):
    return str(s or "").strip().lower()


def _names(player, opponent):
    if _n(player) == "match":
        return {_n(x) for x in str(opponent or "").split(" v ") if x.strip()}
    return {_n(player)}


def _write_start_index(board):
    """APEX-STARTINDEX-2026-10-10: player -> [{opponent, start_utc}] from the
    live PrizePicks board, read by apex_edge_tracker so started matches leave
    the published boards (the picks log never carried start times)."""
    idx = {}
    for b in board:
        if not b.get("start_utc") or not b.get("player"):
            continue
        e = {"opponent": ops_clean(b.get("opponent")), "start_utc": b["start_utc"]}
        lst = idx.setdefault(_n(b["player"]), [])
        if e not in lst:
            lst.append(e)
    path = os.path.join(ops.LOG_DIR, "match_start_index.json")
    old = ops._read_json(path, {}).get("players", {})
    # keep entries for matches that already started (they drop off PrizePicks)
    # for 2 days so a started prop stays recognised as started
    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    for k, lst in old.items():
        for e in lst:
            if e.get("start_utc", "") >= cutoff and e not in idx.setdefault(k, []):
                idx[k].append(e)
    ops._write_json(path, {"updated": ops.now_ct().isoformat(timespec="seconds"), "players": idx})


def ops_clean(desc):
    import re
    return re.sub(r"\s+(?:1st|2nd|3rd|first|second|third)\s+set\s*$", "", str(desc or ""), flags=re.I).strip().lower()


NEW_PROPS_MIN = 10   # APEX-NEWPROPS-2026-10-11: alert on a new match, or 10+ new lines
LUMP_MIN = 25        # APEX-LUMPSCORE-2026-10-30: a batch this big is scored right away (= openclaw_core.DROP_ALERT_MIN)
SCORE_REQUEST = os.path.join(ops.LOG_DIR, "score_request.json")


def _request_score(items, n):
    """APEX-LUMPSCORE-2026-10-30: ask the scheduler (2-min job) to score this batch now."""
    try:
        ops._write_json(SCORE_REQUEST, {"t": ops.now_ct().isoformat(timespec="seconds"), "n": int(n),
                                        "keys": [k for k, *_r in items]})
        return True
    except Exception as e:
        print("[newprops] score request not written: %s" % e)
        return False


def _hm(ts):
    try:
        return datetime.fromisoformat(str(ts)[:19]).strftime("%-I:%M %p")
    except Exception:
        return str(ts)[11:16]


PENDING = os.path.join(ops.LOG_DIR, "unscored_pending.json")   # APEX-LUMPSCORE2-2026-10-30
PENDING_MAX_MIN = 45   # a line still unscored after this long was skipped on purpose by a board check


def _scored_keys(keys, since):
    """APEX-LUMPSCORE2-2026-10-30: which of these prop keys got a pick-log row after `since`."""
    want = {}
    for k in keys:
        try:
            pl, opp, slug, line = k.split("|")[:4]
            want[(pl, slug, float(line))] = k
        except (ValueError, TypeError):
            continue
    hit, last = set(), ""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "apex_engine_picks_log_v1.csv"), newline="") as fh:
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
                        last = max(last, r["ts"])
    except Exception:
        pass
    return hit, last


def _pending_update(new_props, now):
    """APEX-LUMPSCORE2-2026-10-30: running count of unscored new lines across the 15-min checks.
    -> (unscored keys incl. earlier small batches, keys of THIS batch already scored, last scored ts)."""
    pend = ops._read_json(PENDING, None) or {}
    seen = dict(pend.get("keys") or {})
    cutoff = (now - timedelta(minutes=PENDING_MAX_MIN)).isoformat(timespec="seconds")
    seen = {k: t for k, t in seen.items() if t >= cutoff}
    for k in new_props:
        seen.setdefault(k, now.isoformat(timespec="seconds"))
    since = min(seen.values()) if seen else now.isoformat(timespec="seconds")
    scored, last = _scored_keys(set(seen), (datetime.fromisoformat(since) - timedelta(minutes=1)).isoformat())
    unscored = {k: t for k, t in seen.items() if k not in scored}
    ops._write_json(PENDING, {"t": now.isoformat(timespec="seconds"), "keys": unscored})
    return unscored, set(new_props) & scored, last


def _pending_clear():
    try:
        ops._write_json(PENDING, {"t": ops.now_ct().isoformat(timespec="seconds"), "keys": {}})
    except Exception:
        pass


def _new_props_alert(board):
    """APEX-NEWPROPS-2026-10-11 (owner, approved "yes" 2026-10-11): post
    "🆕 New Props Released" to #alerts when PrizePicks adds new singles matches
    or a batch of 10+ new lines since the last 15-minute check, so a fresh
    /fetch can score them. One alert per batch; the first run only records."""
    path = os.path.join(ops.LOG_DIR, "pp_board_seen.json")
    singles = [b for b in board if "/" not in str(b.get("player")) and "/" not in str(b.get("opponent"))]
    cur = {"|".join([_n(b["player"]), ops_clean(b["opponent"]), b["stat_slug"], "%g" % b["line"], b["odds_type"]]) for b in singles}
    matches = {}
    for b in singles:
        k = "|".join(sorted([_n(b["player"]), ops_clean(b["opponent"])]))
        matches[k] = "%s v %s" % (b["player"], _clean_title(b["opponent"]))
    prev = ops._read_json(path, None)
    ops._write_json(path, {"t": ops.now_ct().isoformat(timespec="seconds"),
                           "props": sorted(cur | set((prev or {}).get("props", []))),
                           "matches": sorted(set(matches) | set((prev or {}).get("matches", [])))})
    if prev is None:
        return
    new_props = cur - set(prev.get("props", []))
    new_matches = [matches[k] for k in matches if k not in set(prev.get("matches", []))]
    if not new_matches and len(new_props) < NEW_PROPS_MIN:
        return
    # APEX-ALERTCHAIN-2026-10-25: one self-editing card with the board watch's sighting + the scoring
    # result (8:11 + 8:13 PM 10/25 were two cards with two counts, 68 vs 73)
    try:
        import apex_alert_chain as AC
        if AC.CHAINS_ON:
            items, done = [], set()
            for b in singles:
                k = "|".join([_n(b["player"]), ops_clean(b["opponent"]), b["stat_slug"], "%g" % b["line"], b["odds_type"]])
                if k in new_props and k not in done:
                    done.add(k)
                    pair, label = AC.np_pair(b["player"], b["opponent"])
                    items.append((k, pair, label, AC.np_day(b.get("start_utc"))))
            # APEX-LUMPSCORE-2026-10-30 (owner: score a lump right away, not 1-2 matches): lines the
            # board watch already scored count as done; LUMP_MIN+ unscored -> score request (2 min);
            # fewer -> next board check, and the card says which.
            # APEX-LUMPSCORE2-2026-10-30: the 25 is a running count -- earlier small batches still unscored
            # count too ("6 here, 10 there and 10 there").
            n_done, last_ts, unscored = 0, "", {}
            try:
                unscored, done_now, last_ts = _pending_update(new_props, ops.now_ct().replace(tzinfo=None))
                n_done = len(done_now)
            except Exception as _pe:
                print("[newprops] pending count failed (%s), counting this batch only" % _pe)
                try:
                    n_done, last_ts = _scored_since(new_props, datetime.fromisoformat(prev["t"]).replace(tzinfo=None).isoformat())
                except Exception:
                    pass
                unscored = {k: "" for k in new_props if k not in set()} if len(items) - n_done > 0 else {}
            left_now = len(items) - n_done
            left = len(unscored) if unscored else max(left_now, 0)
            earlier = max(left - left_now, 0)
            if left >= LUMP_MIN and _request_score(items, left):
                _pending_clear()
                AC.np_posted(items, nxt="%d unscored lines in all%s, so Apex scores them right away: it starts "
                             "within 2 minutes (scoring takes about %d min). This card then updates with any that "
                             "make Top 3 / Elite 6 or another board." % (left, " (%d from earlier small batches)" % earlier if earlier else "", AC.SCORE_MIN),
                             wait_min=2 + AC.SCORE_MIN,
                             now="Posted on PrizePicks — %d of %d not scored yet" % (left_now, len(items)) if n_done else None)
            elif n_done and left_now <= 0:
                e = AC.np_card(AC.np_merge(items)[1])
                e.setdefault("fields", []).append({"name": "\u2705 Already scored", "inline": False,
                    "value": "The board watch scored these at %s, before this check ran. Any that made a board "
                             "are on the boards now." % _hm(last_ts)})
                AC.finish(AC.NEWPROPS_KEY, e, note="already scored")
            else:
                AC.np_posted(items, nxt="%d unscored lines waiting in all%s. At %d Apex scores them right away; "
                             "otherwise the next board check does it, about {at} (scoring takes about %d min). This "
                             "card then updates with any that make Top 3 / Elite 6 or another board."
                             % (left, " (%d from earlier small batches)" % earlier if earlier else "", LUMP_MIN, AC.SCORE_MIN),
                             now="Posted on PrizePicks — %d of %d not scored yet" % (left_now, len(items)) if n_done else None)
            print("[newprops] chain card: %d new prop(s), %d already scored" % (len(items), n_done))
            return
    except Exception as e:
        print("[newprops] chain card failed (%s: %s), posting the old line" % (type(e).__name__, e))
    # APEX-ODDSCARD-2026-10-24: the 30-min odds watch (apex_odds_watch) scores new props on
    # its own now, so the line says when they get scored instead of "Run /fetch".
    board_day = _board_day()
    by_day = {}
    for b in singles:
        k = "|".join([_n(b["player"]), ops_clean(b["opponent"]), b["stat_slug"], "%g" % b["line"], b["odds_type"]])
        if k in new_props:
            by_day.setdefault(str(b.get("start_utc") or "")[:10] or board_day, set()).add(k)
    # APEX-NEWPROPS-SCORED-2026-10-24: this job runs every 15 min, so the odds watch may already
    # have scored them (its 🆕 NEW PROPS SCORED card / line in #alerts) -- say so instead of "within 30 min"
    soon = "the odds watch scores them within 30 min"
    try:
        n_done, last_ts = _scored_since(new_props, datetime.fromisoformat(prev["t"]).replace(tzinfo=None).isoformat())
        if n_done:
            soon = "%d already scored by the odds watch at %s (see its 🆕 card/line)" % (
                n_done, datetime.fromisoformat(last_ts).strftime("%I:%M %p").lstrip("0"))
    except Exception:
        pass
    parts = []
    for d in sorted(by_day):
        when = soon if (not board_day or d <= board_day) else "scored on their board day"
        parts.append((len(by_day[d]), _day(d), when))
    msg = "🆕 %d new PrizePicks prop%s posted" % (len(new_props), "" if len(new_props) == 1 else "s")
    if len(parts) == 1:
        msg += " (%s) · %s" % (parts[0][1], parts[0][2])
    elif parts:
        msg += " · " + " · ".join("%d for %s (%s)" % p for p in parts)
    if new_matches:
        msg += "\nNew match%s: %s%s" % ("" if len(new_matches) == 1 else "es", ", ".join(new_matches[:4]),
                                         ", ..." if len(new_matches) > 4 else "")
    ops.send_discord("alerts", msg)
    print("[newprops] " + msg)


def _scored_since(new_props, since):
    """(how many of these new prop keys got a pick-log row after `since`, newest row ts)."""
    want = {}
    for k in new_props:
        pl, opp, slug, line = k.split("|")[:4]
        want[(pl, slug, float(line))] = k
    hit, last = set(), ""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "apex_engine_picks_log_v1.csv"), newline="") as fh:
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
                    last = max(last, r["ts"])
    return len(hit), last


def _board_day():
    """The board day the boards are built for ('' if unknown): the published snapshot's match_date."""
    try:
        import apex_edge_tracker as T
        return str(ops._read_json(T.PUBLISHED_BOARD_SNAPSHOT, {}).get("match_date") or "")[:10]
    except Exception:
        return ""


def _day(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%a %-m/%-d")
    except Exception:
        return d


def _clean_title(desc):
    import re
    return re.sub(r"\s+(?:1st|2nd|3rd|first|second|third)\s+set\s*$", "", str(desc or ""), flags=re.I).strip()


def main():
    rows = []
    if os.path.exists(STATUS_LOG):
        with open(STATUS_LOG, newline="") as f:
            rows = list(csv.DictReader(f))
    if not rows:
        try:
            _b = ops.partner_api_board(max_age=600, caller="closing_line")   # APEX-PPTHROTTLE-2026-10-16: reuse a pull up to 10 min old
            _write_start_index(_b)
            _new_props_alert(_b)
        except Exception as e:
            print("[clv] PrizePicks call failed: %s" % e)
        return
    last = max(r["captured_at"] for r in rows)
    cur = [r for r in rows if r["captured_at"] == last]
    now = datetime.now(timezone.utc)
    done = set()
    if os.path.exists(CLV_LOG):
        with open(CLV_LOG, newline="") as f:
            for r in csv.DictReader(f):
                done.add((r["match_date"], _n(r["player"]), r["category"], r["rec_line"], r["direction"]))
    targets = {}
    for r in cur:
        if r["board"] == "all" or r["status"] != "recommended":
            continue
        if r.get("start_utc"):
            try:
                if datetime.fromisoformat(r["start_utc"]) < now:
                    continue          # already started -- too late to capture
            except Exception:
                pass
        k = (r["match_date"], _n(r["player"]), r["category"], r["line"], r["direction"])
        if k in done:
            continue
        t = targets.setdefault(k, dict(r, boards=set()))
        t["boards"].add(r["board"])
    try:
        board = ops.partner_api_board(max_age=600, caller="closing_line")   # APEX-PPTHROTTLE-2026-10-16
    except Exception as e:
        print("[clv] PrizePicks call failed: %s" % e)
        return
    _write_start_index(board)   # APEX-STARTINDEX-2026-10-10: every run, targets or not
    _new_props_alert(board)     # APEX-NEWPROPS-2026-10-11
    if not targets:
        return
    # Standard line for the same market at publish time
    std_at_pub = {}
    for r in cur:
        if r["board"] == "all" and r.get("prop_type") == "standard":
            for nm in _names(r["player"], r["opponent"]):
                std_at_pub[(nm, r["category"])] = r["line"]
    out = []
    for k, r in targets.items():
        names = _names(r["player"], r["opponent"])
        lines = [b for b in board
                 if STAT_MAP.get(b["stat_slug"], b["stat_slug"]) == r["category"]
                 and (_n(b["player"]) in names or any(nm in _n(b["opponent"]) for nm in names))]
        # start time: from the publish row, else from PrizePicks itself
        start = r.get("start_utc") or next((b["start_utc"] for b in lines if b.get("start_utc")), "")
        if not start:
            continue
        try:
            mins = (datetime.fromisoformat(start) - now).total_seconds() / 60.0
        except Exception:
            continue
        if not (0 <= mins <= WINDOW_MIN):
            continue
        r["mins"], r["start_utc"] = mins, start
        std_now = next((b["line"] for b in lines if b["odds_type"] == "standard"), None)
        exact = any(abs(b["line"] - float(r["line"])) < 1e-9 and b["odds_type"] == (r.get("prop_type") or "standard")
                    for b in lines)
        std_pub = next((std_at_pub[(nm, r["category"])] for nm in names if (nm, r["category"]) in std_at_pub), "")
        clv = ""
        if std_now is not None and std_pub not in ("", None):
            mv = float(std_now) - float(std_pub)
            if abs(mv) < 1e-9:
                clv = "same"
            else:
                clv = "beat" if ((mv > 0) == (r["direction"] == "MORE")) else "lost"
        out.append({"captured_at": ops.now_ct().isoformat(timespec="seconds"), "match_date": r["match_date"],
                    "player": r["player"], "opponent": r["opponent"], "category": r["category"],
                    "direction": r["direction"], "rec_prop_type": r.get("prop_type"), "rec_line": r["line"],
                    "rec_standard_line": std_pub, "close_standard_line": "" if std_now is None else std_now,
                    "exact_line_still_offered": int(exact), "minutes_to_start": "%.0f" % r["mins"],
                    "start_utc": r["start_utc"], "boards": "|".join(sorted(r["boards"])), "clv": clv})
    if not out:
        return
    fresh = not os.path.exists(CLV_LOG)
    with open(CLV_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if fresh:
            w.writeheader()
        w.writerows(out)
    print("[clv] captured %d prop(s)" % len(out))


if __name__ == "__main__":
    main()
