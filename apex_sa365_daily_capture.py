"""
apex_sa365_daily_capture.py  (APEX-SA365-PERSET-2026-09-27, Phase 6)

Daily job: capture SA365 per-set statistics for matches settled in the
previous 24h, while they are still inside the retention window found in
Phase 1 (roughly 2-3 weeks -- see SA365_PERSET_PIPELINE_2026-09-27.md
Section 3). This is data accumulation only: no features are built, no
model is fit, no ranking/probability code is touched.

SCOPE: "completed match" here means a match Apex actually produced a pick
on (apex_engine_picks_log_v1.csv), for the settled day being processed --
same universe Phase 1's backfill used, just a rolling 24h window instead
of full history. This is a deliberate, stated scope choice: it's the set
of matches Apex's own settlement quality actually depends on, and it's
the only enumeration of "yesterday's completed matches" available without
inventing a broader all-of-tennis scan this project has not built.

Usage:
    python3 apex_sa365_daily_capture.py            # captures "yesterday" (today - 1 day)
    python3 apex_sa365_daily_capture.py --date 2026-09-25   # capture a specific date (backfill-style / testing)

Logging: appends one JSON line per run to apex_sa365_daily_capture_log.jsonl
with attempted / new_captures / duplicates / empty / missing counts, plus
a per-match detail line to apex_sa365_daily_capture.log (human-readable).
"""

import argparse
import datetime as dt
import json
import time
import pandas as pd

import apex_sa365_perset_capture as cap

PICKS_LOG = "apex_engine_picks_log_v1.csv"
JSONL_LOG = "apex_sa365_daily_capture_log.jsonl"
TEXT_LOG = "apex_sa365_daily_capture.log"


def target_date(explicit):
    if explicit:
        return explicit
    return (dt.date.today() - dt.timedelta(days=1)).isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to yesterday")
    args = ap.parse_args()
    day = target_date(args.date)

    cap.init_db()
    df = pd.read_csv(PICKS_LOG)
    df = df[df["player"].astype(str) != "match"]
    df = df[df["match_date"] == day]
    uniq = df[["player", "opponent", "match_date", "surface", "best_of", "category"]].drop_duplicates(
        subset=["player", "opponent", "match_date"]
    )

    counts = {"attempted": 0, "new_captures": 0, "duplicates": 0, "empty": 0, "missing": 0, "error": 0}
    detail_lines = []
    t0 = time.time()

    if len(uniq) == 0:
        note = "No picks-log rows for match_date=%s -- nothing to capture (either no Apex picks settled that day, or the daily job ran before the picks log was updated for that date)." % day
        print(note)
        _append_jsonl(day, counts, note, 0.0)
        return

    for i, r in enumerate(uniq.itertuples(index=False), 1):
        counts["attempted"] += 1
        meta = dict(surface=r.surface, best_of=int(r.best_of) if pd.notna(r.best_of) else None,
                    tour=None, category=r.category)
        try:
            status, note, was_dup = cap.capture_one(r.player, r.opponent, r.match_date, meta)
        except Exception as e:
            status, note, was_dup = "error", "%s: %s" % (type(e).__name__, e), False

        if status == "stored":
            if was_dup:
                counts["duplicates"] += 1
            else:
                counts["new_captures"] += 1
        elif status == "no_statsheet":
            counts["empty"] += 1
        elif status == "no_event":
            counts["missing"] += 1
        elif status == "error":
            counts["error"] += 1

        line = "%3d/%d  %-12s %-22s vs %-22s %-10s -> %-13s %s%s" % (
            i, len(uniq), r.match_date, r.player[:22], r.opponent[:22],
            r.surface, status, note, "  [duplicate]" if was_dup else "")
        print(line)
        detail_lines.append(line)

    elapsed = time.time() - t0
    summary = "DONE %s in %.1fs -- %s (unique matches for the day: %d)" % (day, elapsed, counts, len(uniq))
    print(summary)
    detail_lines.append(summary)
    with open(TEXT_LOG, "a") as f:
        f.write("\n".join(detail_lines) + "\n")
    _append_jsonl(day, counts, "", elapsed)


def _append_jsonl(day, counts, note, elapsed_sec):
    entry = dict(run_at=dt.datetime.now().isoformat(timespec="seconds"),
                 target_date=day, elapsed_sec=round(elapsed_sec, 1), note=note, **counts)
    with open(JSONL_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


if __name__ == "__main__":
    main()
