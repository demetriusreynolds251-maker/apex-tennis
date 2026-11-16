#!/usr/bin/env python3
"""
APEX-BOARDWATCH-2026-10-12 (owner, 2026-10-12: "I want this as an
addition to everything we already have").

Scrape-only board watch. Every 30 minutes the scheduler asks the PrizePicks
partner API for the whole tennis board (no Playwright, no evaluation, no
picks, nothing published) and records the first time each prop was seen.
That gives the real posting times of the board, which the picks log cannot
(it only knows when a /fetch happened to look).

Writes:
  apex_board_firstseen_log_v1.csv   one row per prop the first time it is seen
  data/logs/board_watch_state.json  the keys already seen (pruned after 3 days)
Posts one short line to #system only when new props were found; silent
otherwise. Never touches the active board, the pick log or the forward log.
"""
import csv, json, os
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "apex_board_firstseen_log_v1.csv")
STATE_PATH = os.path.join(HERE, "data", "logs", "board_watch_state.json")
LAST_NEW = []   # APEX-DROPALERT-2026-10-14: props the latest run saw for the first time
LAST_RELISTED = []   # APEX-ALERTCHAIN-2026-10-25: of those, the ones already seen at another start time
FIELDS = ["first_seen_ct", "start_time_raw", "match_date", "player", "opponent",
          "stat_type", "line", "prop_type", "game_id", "projection_id"]


def _prop_type(p):
    t = (p.get("odds_type") or p.get("prop_type") or "").strip().lower()
    if t:
        return t
    if p.get("is_demon"):
        return "demon"
    if p.get("is_goblin"):
        return "goblin"
    return "standard"


def _key(p):
    return "|".join(str(p.get(k, "")) for k in ("player", "opponent", "stat_type", "line")) + "|" + _prop_type(p) \
        + "|" + str(p.get("start_time_raw") or "")[:16]


def run(core):
    """Called by the scheduler. Returns a one-line summary string."""
    now = datetime.now(core.CT)
    LAST_NEW[:] = []
    LAST_RELISTED[:] = []
    try:
        props = core.fetch_props_partner_api(core.get_system_dates()["match_date"], caller="board_watch")
    except Exception as e:
        return f"board watch: API call failed ({type(e).__name__}: {e})"
    if not props:
        # No Playwright fallback on purpose: a watch must never fight a running
        # /fetch for the shared Chrome profile. The next tick will try again.
        return "board watch: partner API returned no props (skipped, no fallback)"
    try:
        with open(STATE_PATH) as fh:
            state = json.load(fh)
    except Exception:
        state = {}
    new, relisted = [], []
    seen_base = {k.rsplit("|", 1)[0] for k in state}   # the key without its start time
    for p in props:
        k = _key(p)
        if k in state:
            continue
        state[k] = now.isoformat(timespec="minutes")
        new.append(p)
        if k.rsplit("|", 1)[0] in seen_base:
            relisted.append(p)
    LAST_NEW[:] = new if state and len(state) > len(new) else []   # never the first baseline run
    LAST_RELISTED[:] = relisted if LAST_NEW else []
    if new:
        exists = os.path.exists(LOG_PATH)
        with open(LOG_PATH, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if not exists:
                w.writeheader()
            for p in new:
                w.writerow({"first_seen_ct": now.strftime("%Y-%m-%dT%H:%M"),
                            "start_time_raw": p.get("start_time_raw") or "",
                            "match_date": p.get("match_date") or "",
                            "player": p.get("player") or "", "opponent": p.get("opponent") or "",
                            "stat_type": p.get("stat_type") or "", "line": p.get("line"),
                            "prop_type": _prop_type(p), "game_id": p.get("game_id") or "",
                            "projection_id": p.get("projection_id") or ""})
    # prune keys older than 3 days so the state file stays small
    cutoff = (now - timedelta(days=3)).isoformat(timespec="minutes")
    state = {k: v for k, v in state.items() if v >= cutoff}
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as fh:
        json.dump(state, fh)
    summary = f"board watch {now.strftime('%I:%M %p').lstrip('0')} CT: {len(props)} props on the board, {len(new)} new"
    if new and len(state) > len(new):   # skip the very first baseline run so it does not announce the whole board
        matches = {(p.get("player"), p.get("opponent")) for p in new}
        types = {}
        for p in new:
            types[_prop_type(p)] = types.get(_prop_type(p), 0) + 1
        try:
            core.send_discord("system",
                f"🆕 **Board watch** {now.strftime('%I:%M %p').lstrip('0')} CT: {len(new)} new tennis prop(s) "
                f"across {len(matches)} match(es) ({', '.join(f'{v} {k}' for k, v in sorted(types.items()))}). "
                f"Logged to apex_board_firstseen_log_v1.csv.")
        except Exception:
            pass
    return summary
