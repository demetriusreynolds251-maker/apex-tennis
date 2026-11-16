#!/bin/bash
# APEX-DATA-REFRESH-2026-08-22
# Refreshes the match database folder "ATP & WTA, 1968-2026" from TennisMyLife
# (https://stats.tennismylife.org/tennis-match-database). Pulls the current-year
# files for ATP, WTA and Challenger plus the "ongoing" files, keeps a dated backup
# of what was there, and verifies the refresh by row count and latest tourney_date.
#
# Usage (from the project folder):
#   bash apex_data_refresh.sh            -> refresh current year only (default)
#   bash apex_data_refresh.sh 2025 2026  -> refresh the listed years (corrections land in old years too)
#
# Nothing here touches any model, state file or log. After a refresh, run:
#   python3 apex_prop_model_v1.py --refresh-state
#   python3 fragility_state.py --build --no-challengers --year-from 2005
#   python3 fragility_state.py --validate
set -u
DIR="ATP & WTA, 1968-2026"
BASE="https://stats.tennismylife.org/data"
STAMP=$(date +%Y%m%d_%H%M%S)
BACKUP="$DIR/_backup_$STAMP"
YEARS="$*"
[ -z "$YEARS" ] && YEARS=$(date +%Y)

if [ ! -d "$DIR" ]; then echo "ERROR: folder '$DIR' not found — run from the project folder"; exit 1; fi
mkdir -p "$BACKUP"
echo "APEX-DATA-REFRESH-2026-08-22  source=$BASE  years=$YEARS  backup=$BACKUP"

latest_date () {
  python3 - "$[amount]" <<'PY'
import csv, sys
m = ""; n = 0
with open(sys.argv[1], newline="", encoding="utf-8", errors="replace") as f:
    r = csv.DictReader(f)
    for row in r:
        n += 1
        d = (row.get("tourney_date") or "").strip()
        if d > m: m = d
print("%d rows, latest tourney_date %s" % (n, m or "none"))
PY
}

fetch () {
  name="$[amount]"
  url="$BASE/$name"
  before="(new file)"
  if [ -f "$DIR/$name" ]; then
    cp "$DIR/$name" "$BACKUP/$name"
    before=$(latest_date "$DIR/$name")
  fi
  tmp="$DIR/.$name.download"
  if curl -fsSL --retry 3 "$url" -o "$tmp"; then
    if head -c 200 "$tmp" | grep -q "tourney_id"; then
      mv "$tmp" "$DIR/$name"
      after=$(latest_date "$DIR/$name")
      echo "  OK   $name"
      echo "         before: $before"
      echo "         after:  $after"
    else
      rm -f "$tmp"
      echo "  SKIP $name — downloaded content is not a match CSV (header missing); old file kept"
    fi
  else
    rm -f "$tmp"
    echo "  FAIL $name — download failed; old file kept"
  fi
}

for y in $YEARS; do
  fetch "$y.csv"
  fetch "${y}_wta.csv"
  fetch "${y}_challenger.csv"
done
fetch "ongoing_tourneys.csv"
fetch "wta_ongoing_tourneys.csv"
fetch "challenger_ongoing_tourneys.csv"

echo ""
echo "Reminder: a tournament stays in the *ongoing* files until it finishes, then moves into the year file."
echo "The models only read the year files, so a tournament in progress is not in the replay yet — by design."
echo "Next: python3 apex_prop_model_v1.py --refresh-state ; fragility_state.py --build ... ; --validate"
