#!/bin/bash
# fetch_odds_history.sh — 2026-08-16
# Downloads tennis-data.co.uk yearly odds files for 2010-2021, ATP + WTA, into ./odds/
# (the 2022-2026 files are already there). Tries .xlsx first, then .xls (older years).
# Rejects anything that isn't a real spreadsheet (e.g. an HTML "not found" page).
# Usage (from the project folder):   bash fetch_odds_history.sh
#
# Older .xls files need the xlrd package to be read by pandas:
#     pip3 install --user xlrd
# (the test script will tell you if it's missing).

set -u
mkdir -p odds
cd odds || exit 1

ok=0; bad=0
for y in $(seq 2010 2021); do
  for tour in atp wta; do
    if [ "$tour" = "atp" ]; then base="http://www.tennis-data.co.uk/${y}/${y}"; else base="http://www.tennis-data.co.uk/${y}w/${y}"; fi
    got=""
    for ext in xlsx xls; do
      out="${tour}_${y}.${ext}"
      # skip if we already have a good copy
      if [ -s "$out" ] && file "$out" | grep -qiE 'excel|microsoft|zip|composite document|ole'; then got="$out (already here)"; break; fi
      curl -sSL --fail -o "$out" "${base}.${ext}" 2>/dev/null
      if [ -s "$out" ] && file "$out" | grep -qiE 'excel|microsoft|zip|composite document|ole'; then
        got="$out"; break
      else
        rm -f "$out"
      fi
    done
    if [ -n "$got" ]; then
      printf "  OK    %s  %s bytes\n" "$got" "$(wc -c < "${got%% *}" | tr -d ' ')"; ok=$((ok+1))
    else
      printf "  MISS  %s_%s  (neither .xlsx nor .xls came back as a spreadsheet)\n" "$tour" "$y"; bad=$((bad+1))
    fi
  done
done
echo
echo "Done: $ok files present, $bad missing.  Files now in ./odds/:"
ls -1 | sort
