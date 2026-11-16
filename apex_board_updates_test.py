#!/usr/bin/env python3
"""
APEX-UPDATETRACK-2026-10-24 (owner, 10/24 night: "No board lock I'll just keep this for
2 day then remind me to review the results keep up with it and also the slips top 3 and elite 6
should reflect each update all this needs to be track and recorded ... be sure the the reminder
in automatic"):

BOARD UPDATES TEST -- does updating the boards after 5 PM help or hurt? From the Sun 10/25 board
on, for the Top 3 slip and the Elite 6 slip of each board day it compares:
  (a) the 5 PM board slip  (Slips v2 role "fivepm")
  (b) the FINAL board slip (last board before the first match = the official slip, unchanged)
  (c) how many times the picks changed between them, and whether the picks swapped in hit vs
      the picks they replaced (W-L each side).
Both slips are graded by Slips v2 ($[amount] a slip, same payouts). Reads data/slips/slips_v2_log.json
only; its own files, never in the P&L ledger:
  data/slips/board_updates_test.json   per day, per slip
  data/slips/board_updates_test.csv    same, one row per day and slip
  data/slips/board_updates_test_state.json   which reminder cards were posted

Reminder (automatic, run by the scheduler at 9:00 AM CT and re-checked every 15 min):
  Tue 10/27 9:00 AM -- "🧪 BOARD UPDATES TEST · 2-day review" card to #alerts and #system
  every day after   -- one running line in #system at 9 AM, until DAILY_LINE_ON = False

  python3 apex_board_updates_test.py            # print the review card (nothing posted)
  python3 apex_board_updates_test.py --line     # print the daily line
  python3 apex_board_updates_test.py --run      # what the scheduler runs (posts when due)
"""
import csv, json, os, sys
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(HERE, "data", "slips", "board_updates_test.json")
OUT_CSV = os.path.join(HERE, "data", "slips", "board_updates_test.csv")
STATE = os.path.join(HERE, "data", "slips", "board_updates_test_state.json")
CT = ZoneInfo("America/Chicago")
START_DAY = "2026-10-25"                  # first board day in the test
REVIEW_DAYS = ("2026-10-25", "2026-10-26")
REVIEW_AT = datetime(2026, 10, 27, 9, 0, tzinfo=CT)
DAILY_AT_HOUR = 9
DAILY_LINE_ON = True                      # he says stop -> False
KINDS = (("top3", "\U0001F947 TOP 3"), ("elite6", "\U0001F3C6 ELITE 6"))


def _v2():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import apex_slips_v2 as v2
    return v2


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(path + ".tmp", path)


def _money(x):
    return ("+$%.2f" % x) if x >= 0 else ("-$%.2f" % -x)


def _day(d):
    try:
        return datetime.fromisoformat(d).strftime("%a %-m/%-d")
    except Exception:
        return str(d)


def _wl(legs):
    w = sum(l.get("result") == "hit" for l in legs)
    lo = sum(l.get("result") == "miss" for l in legs)
    wait = sum(l.get("result") is None for l in legs)
    return w, lo, wait


def _slip_res(e):
    if e is None:
        return None
    if e["status"] == "pending":
        return "not graded yet"
    if e["kind"] == "top3":
        r = {"won": "✅ WON", "lost": "❌ LOST", "void": "➖ PUSH"}.get(e["status"], e["status"])
    else:
        r = "%d of %d hit" % (e["hits"], e["n"])
    return "%s %s" % (r, _money(e.get("profit") or 0))


def build():
    """{day: {kind: row}} for every board day from START_DAY. Rewrites the JSON and CSV."""
    v2 = _v2()
    try:
        v2.grade()
    except Exception as e:
        print("[UpdatesTest] grade skipped: %s" % e)
    L = _load(v2.LOG, [])
    five, off, first = v2.pick(L, "fivepm"), v2.pick(L, "official"), v2.pick(L, "first")
    days = sorted({e["match_date"] for e in L if (e.get("match_date") or "") >= START_DAY})
    out = {}
    for d in days:
        for k, _name in KINDS:
            es = sorted([e for e in L if e["match_date"] == d and e["kind"] == k], key=lambda e: e["board_ts"])
            if not es:
                continue
            a = (five.get(d) or {}).get(k)
            a_note = ""
            if a is None:   # no 5 PM build logged that day: the first board is the stand-in
                a = (first.get(d) or {}).get(k)
                a_note = "no 5 PM board logged; first board used"
            b = (off.get(d) or {}).get(k)
            row = {"day": d, "kind": k, "fivepm_board": a and a["board_ts"], "fivepm_note": a_note,
                   "final_board": b and b["board_ts"], "final_official": b is not None,
                   "builds_logged": len(es)}
            if b is None:   # first match not started yet: the final slip isn't settled
                row["state"] = "waiting for the first match"
                b = es[-1]
                row["final_board"] = b["board_ts"]
            span = [e for e in es if a and a["board_ts"] <= e["board_ts"] <= b["board_ts"]]
            keys = [frozenset(v2.sc_key(l) for l in e["legs"]) for e in span]
            row["updates"] = sum(1 for x, y in zip(keys, keys[1:]) if x != y)
            row["update_times"] = [v2._ct(e2["board_ts"]).strftime("%-I:%M %p")
                                   for (e1, e2), x, y in zip(zip(span, span[1:]), keys, keys[1:]) if x != y]
            ak = {v2.sc_key(l) for l in (a or {}).get("legs", [])}
            bk = {v2.sc_key(l) for l in b["legs"]}
            swapped_in = [l for l in b["legs"] if v2.sc_key(l) not in ak]
            replaced = [l for l in (a or {}).get("legs", []) if v2.sc_key(l) not in bk]
            si, ri = _wl(swapped_in), _wl(replaced)
            graded = bool(a) and a["status"] != "pending" and b["status"] != "pending" and row.get("state") is None
            row.update({
                "fivepm_status": (a or {}).get("status"), "fivepm_profit": (a or {}).get("profit"),
                "fivepm_hits": (a or {}).get("hits"), "fivepm_n": (a or {}).get("n"),
                "final_status": b["status"], "final_profit": b.get("profit"), "final_hits": b["hits"], "final_n": b["n"],
                "fivepm_legs": [v2._who(l) + " %s %s %s" % (l["category"], l["direction"], l["line"]) for l in (a or {}).get("legs", [])],
                "final_legs": [v2._who(l) + " %s %s %s" % (l["category"], l["direction"], l["line"]) for l in b["legs"]],
                "swapped_in": [v2._who(l) + " %s %s %s [%s]" % (l["category"], l["direction"], l["line"], l.get("result") or "wait") for l in swapped_in],
                "replaced": [v2._who(l) + " %s %s %s [%s]" % (l["category"], l["direction"], l["line"], l.get("result") or "wait") for l in replaced],
                "swapped_in_w": si[0], "swapped_in_l": si[1], "swapped_in_wait": si[2],
                "replaced_w": ri[0], "replaced_l": ri[1], "replaced_wait": ri[2],
                "graded": graded,
                "fivepm_result": _slip_res(a), "final_result": _slip_res(b)})
            out.setdefault(d, {})[k] = row
    _save(OUT_JSON, out)
    fields = ["day", "kind", "graded", "fivepm_board", "fivepm_note", "final_board", "final_official", "builds_logged",
              "updates", "update_times", "fivepm_status", "fivepm_profit", "final_status", "final_profit",
              "swapped_in_w", "swapped_in_l", "swapped_in_wait", "replaced_w", "replaced_l", "replaced_wait",
              "fivepm_legs", "final_legs", "swapped_in", "replaced"]
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV + ".tmp", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for d in sorted(out):
            for k, _ in KINDS:
                r = out[d].get(k)
                if r:
                    w.writerow({f: (" | ".join(r[f]) if isinstance(r.get(f), list) else r.get(f)) for f in fields})
    os.replace(OUT_CSV + ".tmp", OUT_CSV)
    return out


def _day_line(r):
    if not r:
        return "no slip logged"
    if not r["graded"]:
        return "not graded yet (%s)" % (r.get("state") or "results still coming in")
    if r["updates"] == 0:
        upd = "0 updates (same slip all night)"
    else:
        upd = "%d update%s (%s) · swapped-in picks %d-%d vs replaced picks %d-%d" % (
            r["updates"], "" if r["updates"] == 1 else "s", ", ".join(r["update_times"]),
            r["swapped_in_w"], r["swapped_in_l"], r["replaced_w"], r["replaced_l"])
    return "5 PM slip %s · final slip %s · %s%s" % (r["fivepm_result"], r["final_result"], upd,
                                                   (" _(%s)_" % r["fivepm_note"]) if r.get("fivepm_note") else "")


def _totals(data, days):
    t = {"n": 0, "five": 0.0, "final": 0.0, "upd": 0, "si_w": 0, "si_l": 0, "r_w": 0, "r_l": 0}
    for d in days:
        for k, _ in KINDS:
            r = (data.get(d) or {}).get(k)
            if not r or not r["graded"]:
                continue
            t["n"] += 1
            t["five"] += r["fivepm_profit"] or 0
            t["final"] += r["final_profit"] or 0
            t["upd"] += r["updates"]
            t["si_w"] += r["swapped_in_w"]; t["si_l"] += r["swapped_in_l"]
            t["r_w"] += r["replaced_w"]; t["r_l"] += r["replaced_l"]
    return t


def verdict(t):
    if not t["n"]:
        return "No verdict yet: no day is fully graded."
    diff = t["final"] - t["five"]
    if abs(diff) < 0.005:
        return "No difference: the final boards and the 5 PM boards made the same money."
    if diff > 0:
        return "Updates helped: the final board won %s more than the 5 PM board." % ("$%.2f" % diff)
    return "Updates hurt: the final board made %s less than the 5 PM board." % ("$%.2f" % -diff)


def review_card(data=None, days=REVIEW_DAYS):
    data = build() if data is None else data
    L = ["\U0001F9EA **BOARD UPDATES TEST · 2-day review**",
         "Days covered: %s boards · 5 PM board slip vs final board slip (the official one, last board "
         "before the first match) · $20 a slip" % " + ".join(_day(d) for d in days)]
    for k, name in KINDS:
        L.append("")
        L.append("**%s**" % name)
        for d in days:
            L.append("• %s: %s" % (_day(d), _day_line((data.get(d) or {}).get(k))))
    t = _totals(data, days)
    L.append("")
    if t["n"]:
        L.append("Total (%d graded slip%s): 5 PM boards %s · final boards %s · %d update%s · swapped-in "
                 "picks %d-%d vs replaced picks %d-%d" % (t["n"], "" if t["n"] == 1 else "s", _money(t["five"]),
                                                          _money(t["final"]), t["upd"], "" if t["upd"] == 1 else "s",
                                                          t["si_w"], t["si_l"], t["r_w"], t["r_l"]))
    L.append("⚖️ " + verdict(t))
    L.append("_Next: one running line in #system every day at 9 AM until you say stop. Files: "
             "data/slips/board_updates_test.json / .csv_")
    return "\n".join(L)


def daily_line(data=None):
    data = build() if data is None else data
    days = sorted(data)
    t = _totals(data, days)
    graded_days = sorted({d for d in days for k, _ in KINDS if ((data.get(d) or {}).get(k) or {}).get("graded")})
    last = days[-1] if days else None
    pend = [d for d in days if not all(((data.get(d) or {}).get(k) or {}).get("graded", False)
                                       for k, _ in KINDS if (data.get(d) or {}).get(k))]
    if not t["n"]:
        return "\U0001F9EA BOARD UPDATES TEST (since %s): nothing graded yet%s." % (
            _day(START_DAY), (" -- %s not graded yet" % ", ".join(_day(d) for d in pend)) if pend else "")
    by = {}
    for k, name in KINDS:
        tk = {"five": 0.0, "final": 0.0}
        for d in days:
            r = (data.get(d) or {}).get(k)
            if r and r["graded"]:
                tk["five"] += r["fivepm_profit"] or 0
                tk["final"] += r["final_profit"] or 0
        by[k] = tk
    return ("\U0001F9EA BOARD UPDATES TEST (since %s, %d day%s graded): Top 3 5 PM %s vs final %s · Elite 6 5 PM %s "
            "vs final %s · %d update%s · swapped-in %d-%d vs replaced %d-%d · %s%s" % (
                _day(START_DAY), len(graded_days), "" if len(graded_days) == 1 else "s",
                _money(by["top3"]["five"]), _money(by["top3"]["final"]),
                _money(by["elite6"]["five"]), _money(by["elite6"]["final"]),
                t["upd"], "" if t["upd"] == 1 else "s", t["si_w"], t["si_l"], t["r_w"], t["r_l"], verdict(t),
                (" (%s not graded yet)" % ", ".join(_day(d) for d in pend)) if pend else ""))


def run(now=None):
    """Scheduler entry: rebuild the files; post the 10/27 review once, then the daily line once a
    day at/after 9 AM CT. Returns what it did."""
    now = now or datetime.now(CT)
    data = build()
    st = _load(STATE, {})
    did = "files rebuilt (%d days)" % len(data)
    import apex_ops as ops
    if now >= REVIEW_AT and not st.get("review_posted"):
        card = review_card(data)
        ok = ops.send_discord("alerts", card)
        ok = ops.send_discord("system", card) and ok
        st["review_posted"] = now.isoformat(timespec="seconds")
        st.setdefault("daily", []).append(now.date().isoformat())
        _save(STATE, st)
        return did + "; 2-day review posted to #alerts and #system (%s)" % ("ok" if ok else "send problem")
    today = now.date().isoformat()
    if DAILY_LINE_ON and not st.get("stopped") and st.get("review_posted") and now.hour >= DAILY_AT_HOUR \
            and today not in (st.get("daily") or []):
        ops.send_discord("system", daily_line(data))
        st.setdefault("daily", []).append(today)
        _save(STATE, st)
        return did + "; daily line posted to #system"
    return did


if __name__ == "__main__":
    if "--run" in sys.argv:
        print(run())
    elif "--line" in sys.argv:
        print(daily_line())
    else:
        print(review_card())
