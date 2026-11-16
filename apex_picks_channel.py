#!/usr/bin/env python3
"""APEX-PICKSCLEAN-2026-10-24 (owner, 10/24 11:49 PM: "Get picks channel up to date
now it give too much information ... the alerts should show win or loss ... please don't
put it on general ... I'm only interested in seeing the ones I actually care about which
the ones I play which is the backup, the ones on the board").

#picks now carries two things only:
  1. ONE short "TODAY'S PLAYS" card per board day -- Top 3, Elite 6, Best Goblin, Best
     Demon, Best Standard (numbered spots, then "Backups in order"), read from the same
     published board the board commands show. Posted once, then EDITED in place when the
     board changes (change-only), so it never stacks.
  2. ONE results card per board day, edited as each tracked pick settles (props_db), for
     every pick that was numbered or in BACKUPS on any posted board version that day.
     When the day is done, one end-of-day line is posted.

Display only: reads the published snapshot, the pick log via published_board_detail and
props_db. Never changes a pick, a board, a rule or any saved data; its own files live in
data/picks_channel/. Every entry point is fail-open.

  python3 apex_picks_channel.py plays [--dry]     # build / post the plays card now
  python3 apex_picks_channel.py results [--dry]   # check results now
"""
import fcntl, hashlib, json, os, sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "data", "picks_channel")
STATE_FILE = os.path.join(DIR, "state.json")       # message ids + last card signature
TRACK_FILE = os.path.join(DIR, "tracked.json")     # board day -> picks he plays (board + backups)
LOCK_FILE = os.path.join(DIR, ".lock")

BOARDS = [("top3", "\U0001F947 Top 3", 3), ("elite6", "\U0001F3C6 Elite 6", 6),
          ("bestgoblin", "\U0001F7E2 Best Goblin", 6), ("bestdemon", "\U0001F534 Best Demon", 6),
          ("beststandard", "⚪ Best Standard", 6)]
BOARD_SHORT = {"top3": "Top 3", "elite6": "Elite 6", "bestgoblin": "Best Goblin",
               "bestdemon": "Best Demon", "beststandard": "Best Standard"}
BACKUPS_SHOWN = 6
DONE_AFTER_HOURS = 36       # a pick with no result this long after its start stops holding the day open
TRACK_KEEP_DAYS = 10
UNIT = {"total_games": "games", "games_won": "games", "fantasy_score": "pts", "aces": "aces",
        "dfs": "double faults", "double_faults": "double faults", "breaks": "breaks"}
SHORT_CAT = {"total_games": "TG", "fantasy_score": "Fantasy", "games_won": "Games Won", "aces": "Aces",
             "dfs": "DF", "double_faults": "DF", "breaks": "Breaks"}

try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:   # py<3.9 fallback; the Mac runs 3.9
    CT = timezone(timedelta(hours=-5))


# ─────────────────────────── small helpers ───────────────────────────
def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


class _Lock:
    def __enter__(self):
        os.makedirs(DIR, exist_ok=True)
        self.fh = open(LOCK_FILE, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def _last(name):
    return (str(name or "").split() or [""])[-1]


def _pair(p):
    """(player, opponent) full names; match props carry 'A v B' in opponent."""
    if str(p.get("player") or "") == "match":
        sides = str(p.get("opponent") or "").replace(" vs ", " v ").split(" v ")
        return (sides[0].strip(), sides[1].strip() if len(sides) > 1 else "")
    return (str(p.get("player") or ""), str(p.get("opponent") or ""))


def _key(p):
    return "|".join(str(x) for x in (str(p.get("player") or "").lower(), str(p.get("opponent") or "").lower(),
                                     p.get("category"), p.get("line"), str(p.get("direction") or "").upper()))


def _num(x):
    try:
        return "%g" % float(x)
    except (TypeError, ValueError):
        return str(x)


def _who(p):
    a, b = _pair(p)
    if str(p.get("player") or "") == "match":
        return "%s v %s" % (_last(a), _last(b))
    return "%s (v %s)" % (_last(a), _last(b))


def _market(p, short=False):
    import apex_edge_tracker as t
    cat = p.get("category")
    name = SHORT_CAT.get(cat, t.LABEL.get(cat, cat)) if short else t.LABEL.get(cat, cat)
    typ = str(p.get("prop_type") or "standard").lower()
    tag = "" if typ == "standard" else " (%s)" % ("Demon" if "demon" in typ else typ.title())
    return "%s %s %s%s" % (name, str(p.get("direction") or "").upper(), _num(p.get("line")), tag)


def _start_ct(p):
    try:
        st = datetime.fromisoformat(str(p.get("slip_start_utc")).replace("Z", "+00:00"))
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        return st.astimezone(CT)
    except Exception:
        return None


def _clock(dt):
    if not dt:
        return "time TBD"
    # APEX-ANYDATE-2026-10-25: picks from any upcoming date share the boards -- a pick that
    # isn't today shows its day and date in bold ("**Mon 10/26, 4:00 AM CT**")
    if dt.date() != datetime.now(CT).date():
        return "**%s %d/%d, %s CT**" % (dt.strftime("%a"), dt.month, dt.day, dt.strftime("%-I:%M %p"))
    return "today %s CT" % dt.strftime("%-I:%M %p")


def _day_label(md):
    try:
        d = datetime.strptime(md, "%Y-%m-%d")
        return "%s %d/%d" % (d.strftime("%a"), d.month, d.day)
    except Exception:
        return str(md)


def _webhook():
    try:
        import apex_learning_v2 as lv2   # finds core's webhooks in the scheduler or the bot; never printed
        return lv2._webhook("picks")
    except Exception:
        return None


def _post(embed, msg_id=None):
    """Edit msg_id in place, or post a new message. Returns the message id or None."""
    import requests
    hook = _webhook()
    if not hook:
        return None
    if msg_id:
        try:
            r = requests.patch("%s/messages/%s" % (hook, msg_id), json={"embeds": [embed]}, timeout=15)
            if r.status_code in (200, 204):
                return msg_id
        except Exception:
            pass   # deleted / too old: post a new one below
    r = requests.post(hook + "?wait=true", json={"embeds": [embed]}, timeout=15)
    if r.status_code in (200, 201):
        return r.json().get("id")
    return None


def _post_text(text):
    import requests
    hook = _webhook()
    if not hook:
        return False
    r = requests.post(hook, json={"content": text[:1900]}, timeout=15)
    return r.status_code in (200, 204)


# ─────────────────────────── plays card ───────────────────────────
def _bk_label(t, b):
    """'Siniakova v Krueger TG 18.5 (Score 85)' -- the backup's board Score (APEX-SCOREDISPLAY-2026-10-25)."""
    lab = b.get("label")
    if lab and getattr(t, "SCORE_DISPLAY", False) and b.get("rank") is not None:
        return "%s (Ranking # %s)" % (lab, t._score_fmt(100 * float(b["rank"])))
    return lab


def _read_boards():
    """{flag: {"spots": [p...], "backups": [label...], "backup_ids": [id...]}} from the published board."""
    import apex_edge_tracker as t
    out = {}
    for flag, _n, limit in BOARDS:
        r = t.published_board_detail(flag, return_scored=True, record=False)
        if not isinstance(r, dict):
            out[flag] = {"spots": [], "backups": [], "backup_ids": []}
            continue
        spots = [d.get("p") or {} for d in (r.get("scored") or [])][:limit]
        bks = r.get("backups") or []
        out[flag] = {"spots": spots, "backups": [_bk_label(t, b) for b in bks],
                     "backup_ids": [tuple(b.get("id") or ()) for b in bks]}
    return out


def _snapshot_meta():
    import apex_edge_tracker as t
    snap = _load(t.PUBLISHED_BOARD_SNAPSHOT, {})
    return snap.get("match_date") or "", snap.get("captured_at") or ""


def build_plays(boards, match_date, now=None):
    import apex_edge_tracker as t
    now = now or datetime.now(CT)
    lines = []
    if getattr(t, "SCORE_DISPLAY", False):   # APEX-SCOREDISPLAY-2026-10-25
        lines += [t.SCORE_EXPLAIN, ""]
    for flag, title, _limit in BOARDS:
        b = boards.get(flag) or {}
        lines.append("**%s**" % title)
        if not b.get("spots"):
            lines.append("_none on this board right now_")
        _sp = b.get("spots") or []
        _sdec = t.score_decimals([t.score_value(flag, p) for p in _sp])   # APEX-SCOREDISPLAY-2026-10-25
        for i, p in enumerate(_sp, 1):
            _stx = t.score_text(flag, p, _sdec[i - 1]) if getattr(t, "SCORE_DISPLAY", False) else ""
            lines.append("#%d %s · %s · %d%%%s · \U0001F552 %s" % (
                i, _who(p), _market(p), round(100 * t._board_prob(p)), (" · " + _stx) if _stx else "",
                _clock(_start_ct({"slip_start_utc": t._prop_start_utc(p) or p.get("slip_start_utc")}))))
        bk = [x for x in (b.get("backups") or []) if x][:BACKUPS_SHOWN]
        if bk:
            more = len([x for x in b.get("backups") or [] if x]) - len(bk)
            lines.append("\U0001F501 Backups in order: " + " ".join("%d) %s" % (i, x) for i, x in enumerate(bk, 1))
                         + (" (+%d more)" % more if more > 0 else ""))
        lines.append("")
    body = "\n".join(lines).strip()
    if len(body) > 4000:
        body = body[:3990] + "\n…"
    # APEX-ANYDATE-2026-10-25: every upcoming pick ranks together, so the card is not one day's board
    title = "\U0001F3BE TODAY'S PLAYS · all upcoming picks · updated %s" % now.strftime("%-I:%M %p")
    return title, body


def _spot_state(boards):
    """{flag: [spot, ...]} for the status header (APEX-BOARDSTATUS-HEADER-2026-10-25)."""
    try:
        import apex_board_status as S
        import apex_edge_tracker as t
        return {f: [S.spot(p, prob=t._board_prob(p)) for p in (boards.get(f) or {}).get("spots") or []]
                for f, _n, _l in BOARDS}
    except Exception:
        return None


def _status_head(before, after, boards, from_log=False):
    """'**✅ TOP 3 & ELITE 6 UNCHANGED**' + the lines under it, or '' on failure. A pick that
    left is named by the BACKUP number the new card shows. from_log: the newest board publish
    vs the one before it (when this card has no saved spots to compare)."""
    try:
        import apex_board_status as S
        if not S.HEADER_ON:
            return ""
        if from_log:
            st = S.latest()
            if not st:
                return ""
        elif after is None:
            return ""
        elif before is None:   # first card of the day: nothing to compare
            st = S.compare(after, after, first=True)
        else:
            bk = {f: {tuple(b): "Backup %d" % j for j, b in enumerate((boards.get(f) or {}).get("backup_ids") or [], 1)}
                  for f, _n, _l in BOARDS}
            st = S.compare(before, after, backups=bk)
        t, top = S.embed_top(st)
        return "**%s**\n%s" % (t, top)
    except Exception:
        return ""


def _record_tracked(boards, match_date, captured_at):
    """Every numbered + backup pick on this board version joins the day's tracked list.
    Labels come from the newest version that still lists the pick (a started match leaves
    the board, so its label freezes at the last board before it started)."""
    import apex_edge_tracker as t
    track = _load(TRACK_FILE, {})
    day = track.setdefault(match_date, {})
    by_key = {}
    for flag, _n, _l in BOARDS:
        b = boards.get(flag) or {}
        for i, p in enumerate(b.get("spots") or [], 1):
            by_key.setdefault(_key(p), [p, {}])[1][flag] = i
    # backups: published_board_detail gives identities; fetch the props from the pick log
    want = {}
    for flag, _n, _l in BOARDS:
        for j, bid in enumerate((boards.get(flag) or {}).get("backup_ids") or [], 1):
            want.setdefault(bid, []).append((flag, j))
    if want:
        picks, _ = t.load_current_board()
        for p in picks:
            ident = tuple(t._apex_prop_identity(p))
            if ident in want:
                ent = by_key.setdefault(_key(p), [p, {}])
                for flag, j in want[ident]:
                    ent[1].setdefault(flag, "B%d" % j)
    for k, (p, labels) in by_key.items():
        cur = day.get(k) or {"first_seen": captured_at}
        cur.update({"p": {f: p.get(f) for f in ("player", "opponent", "category", "line", "direction",
                                                  "prop_type", "match_date", "slip_start_utc")},
                    "labels": labels, "last_seen": captured_at})
        if any(not str(v).startswith("B") for v in labels.values()):
            cur["numbered_ever"] = True
        day[k] = cur
    cutoff = (datetime.now(CT) - timedelta(days=TRACK_KEEP_DAYS)).date().isoformat()
    for d in [d for d in track if d < cutoff]:
        track.pop(d, None)
    _save(TRACK_FILE, track)


def refresh_plays(source="", dry=False, force=False):
    """Called after every board build. Posts the plays card when the board day is new,
    edits it when its picks changed, and does nothing otherwise. Never raises."""
    try:
        with _Lock():
            match_date, captured_at = _snapshot_meta()
            if not match_date:
                return "no published board"
            boards = _read_boards()
            if not dry:
                _record_tracked(boards, match_date, captured_at)
            title, body = build_plays(boards, match_date)
            if dry:
                return title + "\n\n" + body
            sig = hashlib.sha1((match_date + body).encode()).hexdigest()
            state = _load(STATE_FILE, {})
            plays = state.get("plays") or {}
            same_day = plays.get("date") == match_date
            if same_day and plays.get("sig") == sig and not force:
                return "plays card unchanged (%s)" % source
            # APEX-BOARDSTATUS-HEADER-2026-10-25: the big TOP 3 & ELITE 6 status under the header,
            # this card's spots vs the version it replaces
            spots = _spot_state(boards)
            if same_day and plays.get("spots") is None:   # card posted before the header existed
                head = _status_head(None, None, boards, from_log=True)
            else:
                head = _status_head(plays.get("spots") if same_day else None, spots, boards)
            if head:
                body = (head + "\n\n" + body)[:4096]
            embed = {"title": title, "description": body, "color": 0x2ECC71,
                     "footer": {"text": "Full cards: board commands in #general · results post below as picks settle"}}
            mid = _post(embed, plays.get("msg_id") if same_day else None)
            if not mid:
                return "plays card post failed"
            state["plays"] = {"date": match_date, "msg_id": mid, "sig": sig, "spots": spots,
                              "updated": datetime.now(CT).isoformat(timespec="seconds"), "source": source}
            _save(STATE_FILE, state)
            return "plays card %s for %s (%s)" % ("edited" if same_day and mid == plays.get("msg_id") else "posted",
                                                  match_date, source)
    except Exception as e:
        return "plays card skipped (%s: %s)" % (type(e).__name__, e)


# ─────────────────────────── results ───────────────────────────
def _label_text(labels):
    num = [(f, n) for f, n in labels.items() if not str(n).startswith("B")]
    order = [f for f, _n, _l in BOARDS]
    if num:
        num.sort(key=lambda x: order.index(x[0]) if x[0] in order else 9)
        return ", ".join("%s #%s" % (BOARD_SHORT.get(f, f), n) for f, n in num)
    bk = sorted(labels.items(), key=lambda x: order.index(x[0]) if x[0] in order else 9)
    return ", ".join("%s backup %s" % (BOARD_SHORT.get(f, f), str(n)[1:]) for f, n in bk) or "board"


def _lookup(conn, p):
    import apex_learning_v2 as lv2
    a, b = _pair(p)
    return lv2.lookup_result(conn, a, b, p.get("category"), p.get("line"), p.get("direction"),
                             match_date=p.get("match_date"), prop_type=p.get("prop_type"))


def result_line(p, status, info, labels):
    lab = _label_text(labels)
    mk = _market(p, short=True)
    if status == "VOID":
        why = str((info or {}).get("settle_note") or "").lower()
        why = "retired" if "ret" in why else ("walkover" if "wo" in why else "void")
        return "⚪ VOID · %s · %s · %s (%s)" % (_who(p), mk, why, lab)
    act = (info or {}).get("actual")
    unit = UNIT.get(p.get("category"), "")
    got = ("%s %s" % (_num(act), unit)).strip() if act is not None else "result in"
    word = {"HIT": "WON", "BUST": "LOST", "PUSH": "PUSH"}[status]
    icon = {"HIT": "✅", "BUST": "❌", "PUSH": "➖"}[status]
    return "%s %s · %s · %s · %s (%s)" % (icon, _who(p), mk, got, word, lab)


def _record_line(match_date, settled):
    """'📋 Sun 10/25 board: Top 3 2-1 · Elite 6 5-1 · Backups 4-2' from settled (status, labels)."""
    rec = {}
    for status, labels in settled:
        if status not in ("HIT", "BUST"):
            continue
        num = [f for f, n in labels.items() if not str(n).startswith("B")]
        for f in (num or ["backups"]):
            w, l = rec.get(f, (0, 0))
            rec[f] = (w + (status == "HIT"), l + (status == "BUST"))
    order = [f for f, _n, _l in BOARDS] + ["backups"]
    parts = ["%s %d-%d" % (BOARD_SHORT.get(f, "Backups"), *rec[f]) for f in order if f in rec]
    return "\U0001F4CB %s board: %s" % (_day_label(match_date), " · ".join(parts) if parts else "no wins or losses")


def check_results(dry=False, now=None):
    """Every ~10 min: settle tracked picks from props_db; edit each day's results card when a
    new result lands; post the end-of-day line once the day is done. Never raises."""
    try:
        import apex_learning_v2 as lv2
        now = now or datetime.now(timezone.utc)
        out = []
        with _Lock():
            track = _load(TRACK_FILE, {})
            state = _load(STATE_FILE, {})
            res_state = state.setdefault("results", {})
            conn = lv2._conn()
            try:
                for md in sorted(track):
                    rs = res_state.get(md) or {}
                    if rs.get("final_posted"):
                        continue
                    rows, settled, open_n = [], [], 0
                    for k, ent in track[md].items():
                        p, labels = ent.get("p") or {}, ent.get("labels") or {}
                        status, info = _lookup(conn, p)
                        st = _start_ct(p)
                        if status in ("HIT", "BUST", "PUSH", "VOID"):
                            rows.append((st or now, result_line(p, status, info, labels)))
                            settled.append((status, labels))
                        elif st is None or now - st.astimezone(timezone.utc) < timedelta(hours=DONE_AFTER_HOURS):
                            open_n += 1
                    if not rows:
                        continue
                    rows.sort(key=lambda r: r[0])
                    rec = _record_line(md, settled)
                    done = open_n == 0
                    body = "\n".join(r[1] for r in rows)
                    body += "\n\n" + (rec if done else "%s · ⏳ %d still to play" % (rec, open_n))
                    if len(body) > 4000:
                        body = "…\n" + body[-3990:]
                    if dry:
                        out.append(body)
                        continue
                    sig = hashlib.sha1(body.encode()).hexdigest()
                    if sig != rs.get("sig"):
                        embed = {"title": "\U0001F4CA RESULTS · %s board" % _day_label(md), "description": body,
                                 "color": 0x3498DB if not done else 0x95A5A6,
                                 "footer": {"text": "Board + backup picks only · updated %s" % datetime.now(CT).strftime("%-I:%M %p")}}
                        mid = _post(embed, rs.get("msg_id"))
                        if mid:
                            rs.update(msg_id=mid, sig=sig)
                            out.append("results %s: %d settled, %d open" % (md, len(rows), open_n))
                    if done and rs.get("msg_id") and not rs.get("final_posted"):
                        if _post_text(rec):
                            rs["final_posted"] = datetime.now(CT).isoformat(timespec="seconds")
                            out.append("end-of-day line posted for %s" % md)
                    res_state[md] = rs
            finally:
                conn.close()
            if not dry:
                cutoff = (datetime.now(CT) - timedelta(days=TRACK_KEEP_DAYS)).date().isoformat()
                for d in [d for d in res_state if d < cutoff]:
                    res_state.pop(d, None)
                _save(STATE_FILE, state)
        return "\n".join(out) if out else "results: nothing new"
    except Exception as e:
        return "results skipped (%s: %s)" % (type(e).__name__, e)


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plays"
    if cmd == "plays":
        print(refresh_plays(source="cli", dry=dry, force="--force" in sys.argv))
    elif cmd == "results":
        print(check_results(dry=dry))
