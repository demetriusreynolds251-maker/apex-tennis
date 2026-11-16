"""
APEX-BANLIST-2026-10-28 (owner, "Put a park on the banned list she too intimidating for
other players" + "Yes skip opponents props in her matches"): a banned-player list.

A banned player's WHOLE MATCH is off every board and slip: her own props, her opponent's props
and the match props (Total Games, tiebreaks ...). The props are still scored and saved (the data
stays), they just never take a numbered spot, a BACKUPS spot, a slip leg or a Soon spot.
Boards, Slips V2, Soon, ParlayPlay and /allprops all read the same list through
apex_edge_tracker._is_unplayable / _pre_board_reason, so one rule applies everywhere the same way.

List file: data/banned_players.json  {"players": [{"name": "Alycia Parks", "why": "...", "added": "..."}]}
Discord: /ban <name> [why], /unban <name>, /banned.
Undo: BAN_LIST_ON = False (then restart the bot + scheduler), or /unban.
"""
import json, os, re, unicodedata
from datetime import datetime

BAN_LIST_ON = True
HERE = os.path.dirname(os.path.abspath(__file__))
BAN_FILE = os.path.join(HERE, "data", "banned_players.json")
_CACHE = {"mtime": None, "entries": []}


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]+", " ", s.lower()).strip()


def _now_ct():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d %-I:%M %p CT")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M")


def load():
    """[{name, why, added}] from the list file (cached on mtime). Never raises."""
    try:
        mt = os.path.getmtime(BAN_FILE)
    except Exception:
        _CACHE.update(mtime=None, entries=[])
        return []
    if _CACHE["mtime"] != mt:
        try:
            with open(BAN_FILE) as fh:
                d = json.load(fh)
            ents = [e for e in (d.get("players") or []) if isinstance(e, dict) and e.get("name")]
        except Exception:
            ents = []
        _CACHE.update(mtime=mt, entries=ents)
    return list(_CACHE["entries"])


def _save(entries):
    os.makedirs(os.path.dirname(BAN_FILE), exist_ok=True)
    tmp = BAN_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"players": entries, "note": "whole match banned: the player, the opponent and the match props"},
                  fh, indent=1)
    os.replace(tmp, BAN_FILE)
    _CACHE["mtime"] = None


def _names_in(p):
    """The two player names of a prop row (player/opponent, or 'A v B' on match props)."""
    try:
        if str(p.get("player") or "").strip().lower() == "match":
            parts = str(p.get("opponent") or "").split(" v ")
        else:
            parts = [p.get("player"), p.get("opponent")]
        return [_norm(x) for x in parts if x]
    except Exception:
        return []


def _entry_matches(entry_name, name):
    e = _norm(entry_name)
    if not e or not name:
        return False
    if e == name:
        return True
    if " " not in e:                      # a bare last name ("parks") matches the last word only
        return name.split()[-1] == e
    return False


def banned_name(p):
    """The banned player's name if this prop's match has one on the list, else ''. Never raises."""
    if not BAN_LIST_ON:
        return ""
    try:
        ents = load()
        if not ents:
            return ""
        for n in _names_in(p):
            for e in ents:
                if _entry_matches(e.get("name"), n):
                    return e.get("name")
    except Exception:
        pass
    return ""


def reason(p):
    """BACKUPS/excluded reason text, else ''."""
    n = banned_name(p)
    return ("banned list: %s match" % n) if n else ""


def is_banned(p):
    return bool(banned_name(p))


def ban(name, why=""):
    name = " ".join(str(name or "").split())
    if not name:
        return "Give me the player's name, first and last."
    ents = load()
    for e in ents:
        if _norm(e.get("name")) == _norm(name):
            return "%s is already on the banned list (since %s)." % (e["name"], e.get("added", "?"))
    ents.append({"name": name, "why": " ".join(str(why or "").split()), "added": _now_ct()})
    _save(ents)
    return "Banned: %s. Her whole match is off every board and slip from the next board update." % name


def unban(name):
    ents = load()
    keep = [e for e in ents if _norm(e.get("name")) != _norm(name)]
    if len(keep) == len(ents):
        return "%s is not on the banned list." % name
    _save(keep)
    return "Removed %s from the banned list. Her matches can be picked again from the next board update." % name


def list_text():
    ents = load()
    if not ents:
        return "Banned list is empty."
    out = ["🚫 **BANNED LIST** (whole match off every board and slip)"]
    for i, e in enumerate(ents, 1):
        out.append("%d) %s — %s (since %s)" % (i, e["name"], e.get("why") or "no reason given", e.get("added", "?")))
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "ban":
        print(ban(sys.argv[2], " ".join(sys.argv[3:])))
    elif len(sys.argv) >= 3 and sys.argv[1] == "unban":
        print(unban(sys.argv[2]))
    else:
        print(list_text())
