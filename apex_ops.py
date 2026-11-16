#!/usr/bin/env python3
"""
APEX-OPS-2026-10-10 (owner, approved "go" 2026-10-10): small shared
helpers for the reliability jobs (scheduler watchdog, daily board report,
closing-line capture). Kept deliberately light: it never imports
openclaw_core (which loads prompts, V8, etc. at import time). The Discord
webhook table and the PrizePicks partner-API URL are read straight out of
openclaw_core.py's source text, so there is still exactly one place they are
defined.
"""
import ast, json, os, time
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(HERE, "openclaw_core.py")
LOG_DIR = os.path.join(HERE, "data", "logs")
HEARTBEAT_FILE = os.path.join(LOG_DIR, "scheduler_heartbeat.json")
PIPELINE_RUNS_FILE = os.path.join(LOG_DIR, "pipeline_runs.json")
WATCHDOG_STATE_FILE = os.path.join(LOG_DIR, "watchdog_state.json")

try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:          # py3.8 fallback -- fixed CDT offset
    CT = timezone(timedelta(hours=-5))


def now_ct():
    return datetime.now(CT)


def ct_str(dt=None):
    dt = dt or now_ct()
    return dt.strftime("%-I:%M %p CT")


_CORE_CONSTS = {}

def _core_const(name):
    """Read a literal module-level constant from openclaw_core.py without
    executing that module."""
    if name in _CORE_CONSTS:
        return _CORE_CONSTS[name]
    with open(CORE, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            _CORE_CONSTS[name] = ast.literal_eval(node.value)
            return _CORE_CONSTS[name]
    raise KeyError(name)


def send_discord(channel, message):
    """Post to one of openclaw_core's Discord channels (picks | alerts |
    general | learning | system). Splits long messages. Returns True/False."""
    import requests
    try:
        hook = _core_const("DISCORD_WEBHOOKS").get(channel)
    except Exception as e:
        print("[ops] webhook table unreadable: %s" % e)
        return False
    if not hook or "YOUR_TOKEN" in hook:
        print("[ops] #%s not configured -- logged only: %s" % (channel, message[:120]))
        return True
    if channel == "alerts":   # APEX-ALERTCARDS-2026-10-25: every #alerts message is a card
        try:
            import apex_alert_cards as _C
            if _C.ALERT_CARDS_ON:
                return bool(_C.post_text(message))
        except Exception as e:
            print("[ops] #alerts card failed (%s), sending text" % type(e).__name__)
    ok = True
    chunks, cur = [], ""
    for line in message.split("\n"):
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur); cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        for attempt in range(3):
            try:
                r = requests.post(hook, json={"content": c}, timeout=15)
                if r.status_code in (200, 204):
                    break
                if r.status_code == 429:
                    time.sleep(float((r.json() or {}).get("retry_after", 2)))
                    continue
                ok = False
                print("[ops] discord #%s status %s" % (channel, r.status_code))
                break
            except Exception as e:
                ok = False
                print("[ops] discord #%s error %s" % (channel, e))
                time.sleep(2)
    return ok


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


def write_heartbeat(note=""):
    """Called by the scheduler every 15 minutes (and at startup)."""
    _write_json(HEARTBEAT_FILE, {"ts": now_ct().isoformat(timespec="seconds"),
                                 "pid": os.getpid(), "note": note})


def record_pipeline_event(event, **extra):
    """event: 'start' | 'finish' | 'fail'. Keeps the last 60 events."""
    runs = _read_json(PIPELINE_RUNS_FILE, [])
    rec = {"ts": now_ct().isoformat(timespec="seconds"), "event": event}
    rec.update(extra)
    runs.append(rec)
    _write_json(PIPELINE_RUNS_FILE, runs[-60:])


# ----------------------------------------------------------------------------- PrizePicks gate
# APEX-PPTHROTTLE-2026-10-16 (owner, "Go" 2026-10-16): every PrizePicks
# partner-API read on this Mac (scheduler jobs, /fetch, the closing-line and
# alerts jobs, board commands) goes through pp_partner_board(). It
#   * shares one pull: a pull younger than max_age seconds is reused from
#     PP_CACHE_FILE instead of asking PrizePicks again (across processes);
#   * is single-flight: a file lock means two jobs never call at once -- the
#     second one waits and then reuses the first one's pull;
#   * backs off: a 429/403 starts a pause (15 min, or longer if PrizePicks
#     sends Retry-After) for every job, with one #alerts note. While paused no
#     request is made; callers that allow it get the last good pull instead.
PP_CACHE_FILE = os.path.join(LOG_DIR, "pp_partner_cache.json")
PP_COOLDOWN_FILE = os.path.join(LOG_DIR, "pp_cooldown.json")
PP_LOCK_FILE = os.path.join(LOG_DIR, "pp_partner.lock")
PP_REQUEST_LOG = os.path.join(LOG_DIR, "pp_requests.log")
PP_COOLDOWN_SEC = 15 * 60
PP_STALE_MAX_SEC = 30 * 60      # oldest pull served while paused
PP_LAST = {"source": None, "age": None}   # "network" | "cache" | "stale" (for log lines)


class PPCooldown(Exception):
    """PrizePicks asked us to slow down; no request was made."""
    def __init__(self, until):
        self.until = until
        super().__init__("PrizePicks asked us to slow down -- paused until %s"
                         % ct_str(datetime.fromtimestamp(until, CT)))


def _pp_read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _pp_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def pp_cooldown_until():
    """Epoch seconds the PrizePicks pause ends, or 0 when not paused."""
    c = _pp_read_json(PP_COOLDOWN_FILE) or {}
    until = float(c.get("until") or 0)
    return until if until > time.time() else 0


def _pp_log_request(caller, status):
    try:
        with open(PP_REQUEST_LOG, "a") as f:
            f.write("%s\t%s\t%s\n" % (now_ct().strftime("%Y-%m-%d %H:%M:%S"), caller, status))
    except Exception:
        pass


# APEX-PPSPACING-2026-11-10 (owner, "Fix all"): every PrizePicks slow-down since 11/5 came when 2-3 Apex calls
# landed within ~20 seconds (two tennis jobs, or tennis + NBA / a sport). Tennis now waits until 20 seconds have passed
# since the last call from ANY Apex product, and stamps a shared file so the sport bots can stay clear of tennis too.
# Waits only: no call is added and none is dropped.
PP_SPACING_SEC = 20
PP_TENNIS_STAMP = os.path.expanduser("~/ApexSports/_shared/pp_lastcall_tennis.json")
PP_SPORTS_LASTCALL = os.path.expanduser("~/ApexSports/_shared/pp_lastcall.json")


def _pp_wait_clear(max_wait=30):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        last = 0.0
        for p in (PP_TENNIS_STAMP, PP_REQUEST_LOG):
            try:
                last = max(last, os.path.getmtime(p))
            except OSError:
                pass
        try:
            last = max(last, float((_pp_read_json(PP_SPORTS_LASTCALL) or {}).get("ts") or 0))
        except Exception:
            pass
        gap = time.time() - last
        if gap >= PP_SPACING_SEC or gap < 0:
            break
        time.sleep(min(2.0, max(0.2, PP_SPACING_SEC - gap)))
    try:
        with open(PP_TENNIS_STAMP, "w") as f:
            f.write('{"ts": %d, "by": "tennis"}' % time.time())
    except OSError:
        pass


def pp_partner_board(max_age=120, allow_stale=True, caller="?"):
    """The PrizePicks tennis partner-API JSON body ({"data", "included"}).
    Reuses a pull younger than max_age seconds; makes at most one request at
    a time across every process. Raises PPCooldown while paused (unless
    allow_stale and a pull under PP_STALE_MAX_SEC old exists -- then that
    pull is returned), or the underlying error on any other failure."""
    import fcntl, requests
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(PP_LOCK_FILE, "a") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)     # released when the file closes
        cache = _pp_read_json(PP_CACHE_FILE) or {}
        age = time.time() - float(cache.get("fetched_at") or 0)
        if cache.get("body") and age <= max_age:
            PP_LAST.update(source="cache", age=int(age))
            return cache["body"]
        until = pp_cooldown_until()
        if until:
            if allow_stale and cache.get("body") and age <= PP_STALE_MAX_SEC:
                PP_LAST.update(source="stale", age=int(age))
                return cache["body"]
            raise PPCooldown(until)
        _pp_wait_clear()                      # APEX-PPSPACING-2026-11-10
        r = requests.get(_core_const("PARTNER_API"),
                         params={"league_id": 5, "per_page": 1000, "single_stat": True},
                         headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                                  "Referer": "https://app.prizepicks.com/",
                                  "Origin": "https://app.prizepicks.com"}, timeout=25)
        _pp_log_request(caller, r.status_code)
        if r.status_code in (403, 429):
            try:
                wait = max(PP_COOLDOWN_SEC, int(float(r.headers.get("Retry-After") or 0)))
            except Exception:
                wait = PP_COOLDOWN_SEC
            until = time.time() + wait
            _pp_write_json(PP_COOLDOWN_FILE, {"until": until, "status": r.status_code,
                                              "set_at": time.time(), "caller": caller})
            _pp_txt = ("⏸️ **PrizePicks asked us to slow down** (HTTP %d) -- pausing all PrizePicks reads until %s. "
                       "Boards keep the last good pull; nothing to do." % (r.status_code, ct_str(datetime.fromtimestamp(until, CT))))
            try:   # APEX-ALERTCHAIN-2026-10-25: one card, paused -> reads back on
                import apex_alert_chain as _AC, apex_alert_cards as _C
                if not _AC.CHAINS_ON:
                    raise RuntimeError("off")
                _AC.start("pp_pause", _C.from_text(_pp_txt),
                          "PrizePicks reads start again after %s — this card turns ✅ on the first good pull after that."
                          % ct_str(datetime.fromtimestamp(until, CT)), wait_min=(until - time.time()) / 60.0 + 35)
            except Exception:
                send_discord("alerts", _pp_txt)
            if allow_stale and cache.get("body") and age <= PP_STALE_MAX_SEC:
                PP_LAST.update(source="stale", age=int(age))
                return cache["body"]
            raise PPCooldown(until)
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict) or "data" not in body:
            raise ValueError("partner API invalid schema (no 'data' key)")
        _pp_write_json(PP_CACHE_FILE, {"fetched_at": time.time(), "body": body})
        PP_LAST.update(source="network", age=0)
        try:   # APEX-ALERTCHAIN-2026-10-25: a pause card still open -> it turns green now
            import apex_alert_chain as _AC, apex_alert_cards as _C
            if _AC.CHAINS_ON and _AC.get("pp_pause", max_age_min=24 * 60):
                _AC.finish("pp_pause", _C.make("✅ PRIZEPICKS READS BACK ON", _C.GREEN,
                                               "First good PrizePicks pull at %s — boards update on the normal schedule again."
                                               % _AC.hm()), note="back %s" % _AC.hm())
        except Exception:
            pass
        return body


def pp_calls_summary(day=None):
    """APEX-PPTHROTTLE2-2026-10-24: one line for the daily status -- PrizePicks
    requests on `day` (default yesterday, CT) from pp_requests.log, busiest hour,
    and how many answers were 'slow down' (429/403)."""
    day = day or (now_ct() - timedelta(days=1)).strftime("%Y-%m-%d")
    n, slow, hours = 0, 0, {}
    try:
        with open(PP_REQUEST_LOG) as f:
            for ln in f:
                p = ln.rstrip("\n").split("\t")
                if len(p) < 3 or not p[0].startswith(day):
                    continue
                n += 1
                hours[p[0][11:13]] = hours.get(p[0][11:13], 0) + 1
                slow += p[2] in ("403", "429")
    except Exception:
        pass
    return "%sPrizePicks calls yesterday: %d (max %d in one hour), slow-down answers: %d" % (
        "\u26a0\ufe0f " if slow else "", n, max(hours.values() or [0]), slow)


def partner_api_board(max_age=120, caller="ops"):
    """The PrizePicks tennis board (via pp_partner_board). Returns a list of
    dicts: player, opponent, stat_slug, line, odds_type, start_utc."""
    body = pp_partner_board(max_age=max_age, caller=caller)
    names = {}
    for inc in body.get("included") or []:
        if inc.get("type") == "new_player":
            a = inc.get("attributes") or {}
            names[inc.get("id")] = a.get("display_name") or a.get("name") or ""
    out = []
    for proj in body.get("data") or []:
        a = proj.get("attributes") or {}
        rel = proj.get("relationships") or {}
        pid = ((rel.get("new_player") or {}).get("data") or {}).get("id")
        if a.get("line_score") is None:
            continue
        out.append({"player": names.get(pid, ""),
                    "opponent": str(a.get("description") or ""),
                    "stat_slug": str(a.get("stat_type") or a.get("stat_display_name") or "").strip().lower().replace(" ", "_"),
                    "line": float(a.get("line_score")),
                    "odds_type": str(a.get("odds_type") or "standard").strip().lower(),
                    "start_utc": to_utc_iso(a.get("start_time"))})
    return out


def to_utc_iso(s):
    """Any ISO timestamp (with offset or Z) -> UTC ISO string, '' if unparseable."""
    s = str(s or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return ""


def wilson(w, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = w / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return ((c - s) / d, (c + s) / d)
