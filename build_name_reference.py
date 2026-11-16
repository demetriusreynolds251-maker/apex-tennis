#!/usr/bin/env python3
"""
build_name_reference.py

Builds a local, fast, real-name reference table from the TennisMyLife
match database CSVs (https://stats.tennismylife.org/tennis-match-database,
MIT licensed, free to use) -- gives resolve_player_name() a real, large,
pre-seeded set of confirmed official ATP/WTA player names to check before
ever hitting the live vendor API. Real names come straight from real
matches; nothing here is guessed or synthesized.

Usage (on the Mac Mini, from wherever the downloaded CSVs live):
    python3 build_name_reference.py /path/to/tml-data

Scans that folder recursively for every *.csv file, reads winner_name and
loser_name from each real row, and writes a single reference file:
    data/known_player_names.json

Tour is inferred from the filename: any file with "_wta" in its name is
WTA; everything else (plain year files, "_challenger", the atp_quali/
subfolder) is treated as ATP-level, matching how resolve_player_name()
itself already splits ATP vs WTA.

Safe to re-run any time you download fresh years -- it always rebuilds
the full reference from whatever CSVs are present, so it can't drift out
of sync or accumulate stale entries from files you've since deleted.
"""
import csv
import json
import sys
from pathlib import Path


def build_reference(root_dir: str, out_path: str = "data/known_player_names.json"):
    root = Path(root_dir)
    if not root.exists():
        print(f"ERROR: {root_dir} does not exist")
        sys.exit(1)

    csv_files = sorted(root.rglob("*.csv"))
    if not csv_files:
        print(f"ERROR: no .csv files found under {root_dir}")
        sys.exit(1)

    names = {"atp": {}, "wta": {}}
    files_processed = 0
    files_skipped = []
    rows_seen = 0

    for fp in csv_files:
        tour = "wta" if "_wta" in fp.name.lower() else "atp"
        try:
            with open(fp, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None or "winner_name" not in reader.fieldnames \
                        or "loser_name" not in reader.fieldnames:
                    files_skipped.append(fp.name)
                    continue
                for row in reader:
                    rows_seen += 1
                    for key in ("winner_name", "loser_name"):
                        real_name = (row.get(key) or "").strip()
                        if not real_name:
                            continue
                        lookup_key = real_name.lower()
                        # First real spelling seen wins -- the data is
                        # ID-based per the source's own documentation, so
                        # spelling should already be consistent within a
                        # tour; this just avoids needless overwrites.
                        names[tour].setdefault(lookup_key, real_name)
            files_processed += 1
        except Exception as e:
            print(f"WARNING: could not read {fp.name}: {e}")
            files_skipped.append(fp.name)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(names, indent=2, ensure_ascii=False))

    print(f"\nDone.")
    print(f"  Files processed: {files_processed}")
    if files_skipped:
        print(f"  Files skipped (no winner_name/loser_name columns, or unreadable): {len(files_skipped)}")
        for name in files_skipped[:10]:
            print(f"    - {name}")
    print(f"  Real match rows read: {rows_seen}")
    print(f"  Unique ATP names: {len(names['atp'])}")
    print(f"  Unique WTA names: {len(names['wta'])}")
    print(f"  Written to: {out.resolve()}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 build_name_reference.py /path/to/tml-data")
        sys.exit(1)
    build_reference(sys.argv[1])
