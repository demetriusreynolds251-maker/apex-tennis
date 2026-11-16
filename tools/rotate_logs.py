#!/usr/bin/env python3
"""APEX-LOGROTATE-2026-10-30: compress big chatter logs into data/logs/archive/<name>_<date>.log.gz,
then empty the live file in place (the running bot/scheduler keep writing to it). Nothing is deleted.
Usage: rotate_logs.py [--min-mb N]   (default: rotate a log once it is 200 MB or more)"""
import gzip, os, shutil, sys, time
ROOT = os.path.expanduser("~/openclaw")
LOGS = ["apex_scheduler.log", "discord_bot.log", "apex_scheduler_error.log", "discord_bot_error.log"]
min_mb = 200
if "--min-mb" in sys.argv:
    min_mb = float(sys.argv[sys.argv.index("--min-mb") + 1])
arch = os.path.join(ROOT, "data", "logs", "archive")
os.makedirs(arch, exist_ok=True)
for name in LOGS:
    p = os.path.join(ROOT, "data", "logs", name)
    if not os.path.exists(p):
        continue
    size = os.path.getsize(p)
    if size < min_mb * 1e6:
        print("skip %s (%.0f MB)" % (name, size / 1e6)); continue
    out = os.path.join(arch, "%s_%s.gz" % (name, time.strftime("%Y-%m-%d_%H%M")))
    free = shutil.disk_usage(arch).free
    if free < size * 0.2 + 2e9:
        print("NOT ENOUGH DISK to compress %s safely (%.1f GB free); skipped" % (name, free / 1e9)); continue
    t0 = time.time()
    with open(p, "rb") as src, gzip.open(out, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, 16 * 1024 * 1024)
    # verify the archive reads back to at least the bytes copied before emptying the live file
    with gzip.open(out, "rb") as chk:
        n = 0
        while True:
            b = chk.read(64 * 1024 * 1024)
            if not b: break
            n += len(b)
    if n < size:
        print("ARCHIVE CHECK FAILED for %s (%d < %d); live file left as is" % (name, n, size)); continue
    with open(p, "r+b") as f:
        f.truncate(0)
    print("rotated %s: %.1f GB -> %.2f GB archive in %.0f s (%s)" % (name, size / 1e9, os.path.getsize(out) / 1e9, time.time() - t0, out))
