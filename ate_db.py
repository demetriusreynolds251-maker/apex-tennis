"""
================================================================
ATE V1 — DATABASE INITIALIZATION
================================================================
One function: ensure_ate_db(). Idempotent — safe to call every
time the capture layer is first used in a session, not just once.

Approved scope (see ATE_V1_SCOPE_FINALIZED.md):
    ai_traces, rule_activations, checkpoint_stamps
Deferred: confidence_ledger_events — do not add here without a
separate, explicit approval.
================================================================
"""

import sqlite3
from pathlib import Path

ATE_DB_PATH = str(Path(__file__).parent / "ate.db")
_SCHEMA_FILE = Path(__file__).parent / "ate_schema_v1.sql"

_ensured = False  # module-level guard so a hot loop calling this
                   # doesn't re-run the schema script every time


def ensure_ate_db(db_path=None):
    """
    Creates ate.db and its tables if they don't already exist.
    Safe to call repeatedly. Returns True on success, False on
    failure — callers in the capture layer should treat False the
    same as any other ATE failure: log locally, never raise.
    """
    global _ensured
    path = db_path or ATE_DB_PATH
    if _ensured and db_path is None:
        return True
    try:
        schema_sql = _SCHEMA_FILE.read_text()
        conn = sqlite3.connect(path, timeout=5)
        conn.executescript(schema_sql)
        conn.commit()
        conn.close()
        if db_path is None:
            _ensured = True
        return True
    except Exception as e:
        try:
            with open(Path(__file__).parent / "ate_failures.log", "a") as f:
                import time
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | "
                        f"ensure_ate_db failed: {e}\n")
        except Exception:
            pass
        return False


if __name__ == "__main__":
    # Manual verification entry point:
    #   python3 ate_db.py
    ok = ensure_ate_db()
    print(f"ate.db ready at {ATE_DB_PATH}" if ok else "ate.db setup FAILED — see ate_failures.log")
    if ok:
        conn = sqlite3.connect(ATE_DB_PATH)
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")]
        print(f"journal_mode = {mode}")
        print(f"tables = {tables}")
        conn.close()
