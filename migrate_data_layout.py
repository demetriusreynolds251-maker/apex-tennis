#!/usr/bin/env python3
"""
DEMETRIUS / OpenClaw — v7.9.6 data folder reorganization
=========================================================
Moves the flat data/ folder into labeled subfolders so files are easy
to find in Finder by typing a word or date, and consolidates the
separate database/ folder into data/cache/.

SAFE TO RUN: this script only MOVES files (nothing is deleted), skips
anything already in its new location, and refuses to overwrite a file
if one already exists at the destination — it stops and tells you
exactly what to look at by hand instead of guessing.

Run this from inside the openclaw folder:
    python3 migrate_data_layout.py
"""
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
OLD_DATABASE_DIR = BASE_DIR / "database"

# (old relative path under data/, new relative path under data/)
MOVES = [
    ("weights.json",            "weights/weights.json"),
    ("pending_proposals.json",  "proposals/pending_proposals.json"),
    ("approval_history.json",   "proposals/approval_history.json"),
    ("slip_log.json",           "slips/slip_log.json"),
    ("pending_results.json",    "learning/pending_results.json"),
    ("quarantine.json",         "learning/quarantine.json"),
    ("signal_history.json",     "learning/signal_history.json"),
]

def move_file(old_path: Path, new_path: Path, report: list):
    if new_path.exists():
        report.append(f"  SKIP  {old_path.name} — already at {new_path.relative_to(DATA_DIR)}")
        return
    if not old_path.exists():
        report.append(f"  SKIP  {old_path.name} — not found, nothing to move")
        return
    new_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_path), str(new_path))
    report.append(f"  MOVED {old_path.relative_to(BASE_DIR)}  ->  {new_path.relative_to(BASE_DIR)}")

def move_dir(old_dir: Path, new_dir: Path, report: list):
    if not old_dir.exists():
        report.append(f"  SKIP  {old_dir.name}/ — not found, nothing to move")
        return
    new_dir.mkdir(parents=True, exist_ok=True)
    moved_any = False
    for item in old_dir.iterdir():
        dest = new_dir / item.name
        if dest.exists():
            report.append(f"  CONFLICT  {item} already exists at {dest} — left both in place, check by hand")
            continue
        shutil.move(str(item), str(dest))
        moved_any = True
    if moved_any:
        report.append(f"  MOVED contents of {old_dir.relative_to(BASE_DIR)}/  ->  {new_dir.relative_to(BASE_DIR)}/")
    try:
        if old_dir.exists() and not any(old_dir.iterdir()):
            old_dir.rmdir()
            report.append(f"  REMOVED now-empty {old_dir.relative_to(BASE_DIR)}/")
    except OSError:
        pass

def main():
    report = []
    DATA_DIR.mkdir(exist_ok=True)

    print("Reorganizing data/ ...")
    for old_rel, new_rel in MOVES:
        move_file(DATA_DIR / old_rel, DATA_DIR / new_rel, report)

    print("Consolidating picks archive ...")
    move_dir(DATA_DIR / "archive", DATA_DIR / "picks_archive", report)

    print("Consolidating database/ into data/cache/ ...")
    move_dir(OLD_DATABASE_DIR, DATA_DIR / "cache", report)

    print()
    print("=" * 60)
    print("MIGRATION REPORT")
    print("=" * 60)
    for line in report:
        print(line)
    print()
    print("Done. New layout:")
    for p in sorted(DATA_DIR.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(BASE_DIR)}")

if __name__ == "__main__":
    main()
