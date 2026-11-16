#!/usr/bin/env python3
"""
APEX-PNLTAXES-2026-10-23 (owner: "Go pnl taxes"): the 2026 tax ledger -- one row per
finished real-money slip, kept as pnl/ledger_2026.csv and pnl/ledger_2026.xlsx.

Rebuilt from the sources every time, never typed by hand:
  - PrizePicks: data/slips/slip_log.json (the same finished slips the 💰 DAILY P&L card counts,
    from apex_pnl_channel.SINCE on -- 10/9 since 10/23)
  APEX-PNLREALONLY-2026-10-24 ("only the actual slips I make"): ONLY his real slips. The ParlayPlay
  Insured 3 tracker slips (Apex's own, ApexParlayPlay/data/insured_slips.json) were taken out; the
  one that was in (10/24, $[amount]) is kept in pnl/apex_slips_excluded.json, never deleted. Every row
  must pass apex_pnl_channel.refuse_reason() again here (hard guard); refusals -> pnl/refused_rows.json.
Hand-kept extras (app entry IDs, notes) live in pnl/ledger_extras.json, keyed by slip id or
real slip number, and survive every rebuild.

Never loses data: before a changed CSV is written the old one is copied to pnl/backups/
(newest 60 kept), and a rebuild that would come out with FEWER rows than the file already has
is refused (the old files stay, a warning is printed) -- a shrinking ledger means a source file
went missing, not that slips un-happened.

  rebuild(force=False) -> (rows written, note)   # skips when nothing changed unless force
  maybe_rebuild()      -> same, only when a source file changed since the last build
  summary_line()       -> one-line 2026 summary for /pnl export

  python3 apex_pnl_ledger.py          # rebuild now and print the summary
"""
import csv, json, os, shutil, sys
from collections import OrderedDict, defaultdict
from datetime import datetime

import apex_pnl_channel as pc

HERE = os.path.dirname(os.path.abspath(__file__))
YEAR = 2026
DIR = os.path.join(HERE, "pnl")
CSV = os.path.join(DIR, "ledger_%d.csv" % YEAR)
XLSX = os.path.join(DIR, "ledger_%d.xlsx" % YEAR)
BACKUPS = os.path.join(DIR, "backups")
KEEP_BACKUPS = 60
EXTRAS = os.path.join(DIR, "ledger_extras.json")
STATE = os.path.join(DIR, ".ledger_state.json")
EXCLUDED = os.path.join(DIR, "apex_slips_excluded.json")   # Apex slips moved out of the ledger (kept, not deleted)

COLS = ["settle_date", "placed_date", "platform", "app_entry_id", "slip_type", "stake", "bonus",
        "payout", "net", "result", "legs_hit", "legs_total", "legs", "source", "note", "slip_id"]

TAX_NOTE = ("PrizePicks and ParlayPlay each send a 1099-MISC when your net winnings on THEIR platform "
            "reach $600 for the year. Each platform counts only its own slips. Keep this ledger and "
            "confirm how to report it with a tax preparer.")


# ------------------------------------------------------------------ rows
def _extras():
    try:
        with open(EXTRAS) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _placed(r):
    """Placed date only when the leg was logged live (a clock time); backfills don't know it."""
    la = str(r.get("logged_at") or "")
    return str(r.get("date") or "")[:10] if la[:1].isdigit() and "backfill" not in la else ""


def _source(legs):
    la = str(legs[0].get("logged_at") or "")
    if "backfill" in la:
        return "screenshot"
    if any(l.get("hand_graded") or l.get("manual_grade") for l in legs):
        return "manual"
    return "grade"


def _leg_text(l):
    """Leg words without blanks -- screenshot backfills from Oct 9-16 often have only the player."""
    if l.get("stat_type") or l.get("stat_label") or l.get("line") is not None:
        return pc._leg_words(l)
    return str(l.get("player") or "?") + " (pick not logged)"


def _excluded_ids():
    try:
        with open(EXCLUDED) as fh:
            return {str(r.get("slip_id")) for r in json.load(fh).get("rows", [])}
    except Exception:
        return set()


def prizepicks_rows():
    out = []
    for s in pc.slips():
        if pc.refuse_reason(s["id"], s["legs"]):   # hard guard -- pc.slips() already filters, checked twice
            continue
        if not s["date"].startswith(str(YEAR)) or s["date"] < pc.SINCE:
            continue
        legs = s["legs"]
        st = [str(l.get("status") or "").upper() for l in legs]
        hit, miss = st.count("WIN"), st.count("LOSS")
        res = {"WON": "win", "LOST": "loss"}.get(s["result"], "push")
        out.append({
            "settle_date": s["date"], "placed_date": _placed(legs[0]), "platform": "PrizePicks",
            "app_entry_id": str(legs[0].get("entry_id") or ""), "slip_type": s["entry"],
            "stake": round(s["stake"], 2), "bonus": "yes" if s["bonus"] or s["stake"] < 0.005 else "no",
            "payout": round(s["back"], 2), "net": round(s["net"], 2), "result": res,
            "legs_hit": hit, "legs_total": hit + miss,
            "legs": "; ".join(_leg_text(l) + {"WIN": " ✓", "LOSS": " ✗", "UNSEEN": " (not shown)"}.get(x, " void")
                              for l, x in zip(legs, st)),
            "source": _source(legs), "note": str(legs[0].get("pp_note") or ""),
            "slip_id": str(s["id"]), "_no": s["no"]})
    return out


def rows():
    ex = _extras()
    out = prizepicks_rows()   # his real slips only -- no Apex tracker rows (APEX-PNLREALONLY-2026-10-24)
    for r in out:
        add = ex.get(r["slip_id"]) or (ex.get(str(r["_no"])) if r["_no"] else None) or {}
        if add.get("app_entry_id"):
            r["app_entry_id"] = str(add["app_entry_id"])
        if add.get("placed_date"):
            r["placed_date"] = add["placed_date"]
        if add.get("note"):
            r["note"] = (r["note"] + " | " if r["note"] else "") + add["note"]
    out.sort(key=lambda r: (r["settle_date"], r["platform"], r["_no"] or 0, r["slip_id"]))
    return out


# ------------------------------------------------------------------ totals
def _tot(rs):
    win = [r for r in rs if r["result"] == "win"]
    return {"entries": len(rs), "stakes": round(sum(r["stake"] for r in rs), 2),
            "payouts": round(sum(r["payout"] for r in rs), 2), "net": round(sum(r["net"] for r in rs), 2),
            "gross_winnings": round(sum(r["payout"] for r in win), 2), "won": len(win)}


def grouped(rs, key):
    """OrderedDict key -> {platform: totals, 'Total': totals}"""
    by = OrderedDict()
    for r in sorted(rs, key=lambda r: key(r)):
        by.setdefault(key(r), []).append(r)
    out = OrderedDict()
    for k, g in by.items():
        d = OrderedDict()
        for p in ("PrizePicks", "ParlayPlay"):
            pg = [r for r in g if r["platform"] == p]
            if pg:
                d[p] = _tot(pg)
        d["Total"] = _tot(g)
        out[k] = d
    return out


# ------------------------------------------------------------------ files
def _csv_text(rs):
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rs:
        w.writerow(r)
    return buf.getvalue()


def _old_rows():
    try:
        ex = _excluded_ids()   # rows moved out as Apex slips are not "missing" -- don't block on them
        with open(CSV, newline="") as fh:
            return sum(1 for r in csv.DictReader(fh) if r.get("slip_id") not in ex)
    except Exception:
        return 0


def _backup():
    if not os.path.exists(CSV):
        return
    os.makedirs(BACKUPS, exist_ok=True)
    shutil.copy2(CSV, os.path.join(BACKUPS, "ledger_%d_%s.csv" % (YEAR, datetime.now(pc.CT).strftime("%Y-%m-%d_%H%M%S"))))
    old = sorted(f for f in os.listdir(BACKUPS) if f.startswith("ledger_%d_" % YEAR) and f.endswith(".csv"))
    for f in old[:-KEEP_BACKUPS]:
        os.remove(os.path.join(BACKUPS, f))


def _xlsx(rs, path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E78")
    money = '"$"#,##0.00;[Red]-"$"#,##0.00'

    def sheet(ws, header, data, money_cols=(), widths=None):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill, c.alignment = head_font, head_fill, Alignment(horizontal="center")
        for d in data:
            ws.append(d)
        for col in money_cols:
            for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
                for c in row:
                    if isinstance(c.value, (int, float)):
                        c.number_format = money
        for i, h in enumerate(header, 1):
            w = (widths or {}).get(h) or max([len(str(h))] + [len(str(x[i - 1])) for x in data if i - 1 < len(x)]) + 2
            ws.column_dimensions[get_column_letter(i)].width = min(w, 60)
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "Slips"
    sheet(ws, COLS, [[r[c] for c in COLS] for r in rs], money_cols=(6, 8, 9), widths={"legs": 60, "note": 50, "slip_id": 28})
    for row in ws.iter_rows(min_row=2, min_col=13, max_col=15):
        for c in row:
            c.alignment = Alignment(wrap_text=False)

    tcols = ["entries", "won", "stakes", "payouts", "net", "gross_winnings"]

    def flat(g, label):
        return [[k, p] + [t[c] for c in tcols] for k, d in g.items() for p, t in d.items()]

    sheet(wb.create_sheet("Daily"), ["date", "platform"] + tcols,
          flat(grouped(rs, lambda r: r["settle_date"]), "date"), money_cols=(5, 6, 7, 8))
    sheet(wb.create_sheet("Monthly"), ["month", "platform"] + tcols,
          flat(grouped(rs, lambda r: r["settle_date"][:7]), "month"), money_cols=(5, 6, 7, 8))
    yr = wb.create_sheet("Year")
    sheet(yr, ["year", "platform"] + tcols, flat(grouped(rs, lambda r: r["settle_date"][:4]), "year"),
          money_cols=(5, 6, 7, 8))
    yr.append([])
    yr.append(["Note", TAX_NOTE])
    yr.append(["", "Gross winnings = everything paid back on slips the app marked a win (flex partial wins included)."])
    yr.append(["", "Net = payouts minus stakes. Bonus-funds slips have a $0 stake."])
    yr.append(["", "PrizePicks ledger starts %s (first real-money slip). Oct 9-16 slips came from app screenshots; "
                   "slips that never got a final result are left out." % pc.SINCE])
    for r in yr.iter_rows(min_row=yr.max_row - 3):
        r[0].font = Font(bold=True)
    tmp = path + ".tmp.xlsx"
    wb.save(tmp)
    os.replace(tmp, path)


def _sources_sig():
    return [os.path.getmtime(p) if os.path.exists(p) else 0 for p in (pc.LEDGER, EXTRAS)]


def _save_state(n):
    with open(STATE, "w") as fh:
        json.dump({"sig": _sources_sig(), "rows": n, "at": datetime.now(pc.CT).isoformat(timespec="seconds")}, fh)


def rebuild(force=False):
    os.makedirs(DIR, exist_ok=True)
    rs = rows()
    text = _csv_text(rs)
    old_n = _old_rows()
    if len(rs) < old_n:
        msg = "ledger NOT rebuilt: new build has %d rows, file has %d (a source file may be missing)" % (len(rs), old_n)
        print("[Ledger] " + msg)
        return old_n, msg
    try:
        with open(CSV, encoding="utf-8") as fh:
            same = fh.read() == text
    except Exception:
        same = False
    if same and os.path.exists(XLSX) and not force:
        _save_state(len(rs))
        return len(rs), "unchanged"
    if not same:
        _backup()
        with open(CSV + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(CSV + ".tmp", CSV)
    _xlsx(rs, XLSX)
    _save_state(len(rs))
    return len(rs), "rebuilt"


def maybe_rebuild():
    try:
        with open(STATE) as fh:
            if json.load(fh).get("sig") == _sources_sig():
                return None
    except Exception:
        pass
    return rebuild()


def summary_line():
    rs = rows()
    y = grouped(rs, lambda r: r["settle_date"][:4]).get(str(YEAR), {})
    parts = []
    for p in ("PrizePicks", "ParlayPlay"):
        t = y.get(p)
        if t:
            parts.append("%s %d slips, net %s" % (p, t["entries"], pc._money(t["net"])))
    t = y.get("Total") or _tot([])
    return "📒 %d ledger: %d slips · staked %s · paid back %s · net %s · gross winnings %s%s" % (
        YEAR, t["entries"], pc._plain(t["stakes"]), pc._plain(t["payouts"]), pc._money(t["net"]),
        pc._plain(t["gross_winnings"]), (" (" + "; ".join(parts) + ")") if parts else "")


if __name__ == "__main__":
    print(rebuild(force="--force" in sys.argv))
    print(summary_line())
