#!/usr/bin/env python3
"""APEX-DROPPEDALERT-2026-11-14 (owner, "I said fix all"; quiet dropped alert). Every 30 minutes (apex_alerts.check) the picks
that matter most are compared with the last check:
  - the /best6of6 card,
  - the /stardemon card (NBA),
  - the Top 3 of each sport's own board (NFL, college football, NHL, NBA, tennis).
When one of them LEAVES before its game starts, one line goes to #alerts: who, stat, line, where it was, and why when we can tell
(line moved / on the injury report / no longer qualifies). Each pick once per day. Quiet 11 PM to 8 AM Central (drops found then are
posted at the first check after 8 AM if the game has not started). Nothing is posted when nothing dropped. Read only on every board.
Why it matters: picks that fell off their board before the game won about 68 of 100 since 11/10, numbered ones about 79.
State: data/dropped_alert_state.json. Undo: ON = False, restart the tennis bot."""
import json, os, subprocess, sqlite3
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ON = True
CHANNEL = "alerts"
HOURS = (8, 23)
LEAD_MIN = 5
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "data", "dropped_alert_state.json")
CT = ZoneInfo("America/Chicago")
TOP3 = [("football/nfl", "🏈 NFL"), ("football/ncaaf", "🏈 College football"), ("hockey/nhl", "🏒 NHL"), ("basketball", "🏀 NBA")]


def _ts(s):
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _watch():
    """{key: dict(who, where, start)} for everything watched right now, and {sport label: [who on its Top 10]} for the why, and an
    error flag (a source that could not be read is skipped so its picks are not reported as dropped)."""
    import apex_best6 as B
    w, top10, bad = {}, {}, set()
    try:
        picks, counts, notes = B.build(B.SIZE)
        if notes: bad.add("best6")
        for n, x in enumerate(picks, 1):
            w["b6|" + str(x.get("key"))] = dict(who=x.get("who"), where="Best 6 of 6 #%d" % n, start=str(x.get("start") or ""), sport=x.get("sport"))
    except Exception:
        bad.add("best6")
    for root, name in TOP3:
        try:
            v, err = B._sport(root, "top3", window=False)
            if err: bad.add(name); continue
            for n, x in enumerate(v, 1):
                w["t3|" + str(x.get("key"))] = dict(who=x.get("who"), where="%s Top 3 #%d" % (name, n), start=str(x.get("start") or ""), sport=name)
            t10, e2 = B._sport(root, "top10", window=False)
            top10[name] = [] if e2 else [str(x.get("who")) for x in t10]
        except Exception:
            bad.add(name)
    try:   # tennis Top 3: the latest tennis board post
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(HERE, "data", "board_history.sqlite"), uri=True)
        pid = con.execute("select max(publish_id) from history where board='top3'").fetchone()[0]
        for pl, op, cat, line, dr, pt, sno, ms in con.execute("select player, opponent, category, line, direction, prop_type, spot_no, match_start from history "
                                                              "where publish_id=? and board='top3' and spot_kind='main' order by spot_no", (pid,)):
            who = "%s %s %g %s" % (op if pl == "match" else pl, str(cat).replace("_", " ").title(), line, dr)
            w["tt|%s|%s|%g|%s" % (pl, cat, line, dr)] = dict(who=who, where="🎾 Tennis Top 3 #%d" % int(sno or 0), start=str(ms or ""), sport="🎾 Tennis")
        con.close()
    except Exception:
        bad.add("🎾 Tennis")
    hurt = []
    try:   # Star Demon card (NBA)
        import apex_star_demon as SD
        p = subprocess.run(["/usr/bin/python3", "-c", SD._SNIP, SD.NBA, SD.LIST, str(SD.MAX_ABOVE)], capture_output=True, text=True, timeout=120)
        d = json.loads(p.stdout.strip().splitlines()[-1])
        for n, x in enumerate(d.get("legs") or [], 1):
            w["sd|%s|%s|%g" % (x["player"], x["stat"], x["line"])] = dict(who="%s %s Demon %g MORE" % (x["player"], SD.STAT_NAME.get(x["stat"], x["stat"]), x["line"]),
                                                                         where="Star Demon #%d" % n, start=str(x.get("start") or ""), sport="🏀 NBA")
        hurt = list(d.get("hurt") or [])
    except Exception:
        bad.add("stardemon")
    return w, top10, hurt, bad


def _why(o, top10, hurt):
    who = str(o.get("who") or "")
    name = " ".join(who.split()[:2])
    if any(name and name.split()[0] in h and name.split()[-1] in h for h in hurt):
        return "he is on the injury report now"
    same = [x for x in top10.get(o.get("sport")) or [] if x.startswith(name) and x != who]
    if same:
        return "his line moved (now %s)" % same[0]
    return "no longer qualifies (line pulled or changed, a hold rule, or a stronger pick took the spot)"


def _src(k):
    return {"b6": "best6", "sd": "stardemon"}.get(k.split("|")[0])


def run(post=True):
    """Called from apex_alerts.check() every 30 min. Never raises. Returns the texts sent."""
    out = []
    if not ON:
        return out
    try:
        now = datetime.now(CT); utc = datetime.now(timezone.utc); today = now.strftime("%Y-%m-%d")
        try:
            st = json.load(open(STATE))
        except Exception:
            st = {}
        if st.get("day") != today:
            st["alerted"] = []
        cur, top10, hurt, bad = _watch()
        prev = st.get("watch") or {}
        pending = [p for p in (st.get("pending") or []) if p["key"] not in (st.get("alerted") or [])]
        for k, o in prev.items():
            if k in cur or k in (st.get("alerted") or []) or any(p["key"] == k for p in pending):
                continue
            src = _src(k) or o.get("sport")
            if src in bad or o.get("sport") in bad:
                continue
            t = _ts(o.get("start"))
            if t is None or t <= utc + timedelta(minutes=LEAD_MIN):
                continue   # its game started (or about to): not a drop
            pending.append(dict(key=k, who=o.get("who"), where=o.get("where"), start=o.get("start"), why=_why(o, top10, hurt)))
        # quiet hours: keep them for the first check after 8 AM if the game has not started
        send = []
        if HOURS[0] <= now.hour < HOURS[1]:
            for p in pending:
                t = _ts(p.get("start"))
                if t is not None and t > utc:
                    send.append(p)
            pending = []
        if send:
            L = ["📤 **DROPPED BEFORE GAME TIME** · %s" % now.strftime("%-I:%M %p CT")]
            for p in send:
                t = _ts(p.get("start"))
                when = t.astimezone(CT).strftime("%a %-I:%M %p") if t else "?"
                L.append("• %s — was %s (starts %s) — %s" % (p["who"], p["where"], when, p["why"]))
            L.append("Picks that fall off their board before the game have won about 68 of 100 since 11/10 (still-numbered picks about 79). Check the app before you play it.")
            txt = "\n".join(L)
            if post:
                import apex_ops as ops
                ops.send_discord(CHANNEL, txt)
            else:
                print("---- would post to #%s ----\n%s" % (CHANNEL, txt))
            out.append(txt)
            st["alerted"] = list(st.get("alerted") or []) + [p["key"] for p in send]
        if post:
            keep = {k: v for k, v in prev.items() if (_src(k) or v.get("sport")) in bad}   # a source we could not read keeps its old list
            st.update(day=today, watch=dict(keep, **cur), pending=pending, saved=now.isoformat(timespec="seconds"))
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            with open(STATE + ".tmp", "w") as fh:
                json.dump(st, fh)
            os.replace(STATE + ".tmp", STATE)
    except Exception as e:
        print("[dropped] skipped: %s: %s" % (type(e).__name__, e))
    return out


if __name__ == "__main__":
    print(run(post=False))
