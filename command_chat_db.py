"""
================================================================
COMMAND CHAT V1 — DATABASE INITIALIZATION
================================================================
Same pattern as ate_db.py exactly. Idempotent.
"""

import sqlite3
from pathlib import Path

COMMAND_CHAT_DB_PATH = str(Path(__file__).parent / "command_chat.db")
_SCHEMA_FILE = Path(__file__).parent / "command_chat_schema_v1.sql"

_ensured = False


def ensure_command_chat_db(db_path=None):
    global _ensured
    path = db_path or COMMAND_CHAT_DB_PATH
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
            with open(Path(__file__).parent / "command_chat_failures.log", "a") as f:
                import time
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | ensure_command_chat_db failed: {e}\n")
        except Exception:
            pass
        return False


if __name__ == "__main__":
    ok = ensure_command_chat_db()
    print(f"command_chat.db ready at {COMMAND_CHAT_DB_PATH}" if ok else "SETUP FAILED")
    if ok:
        conn = sqlite3.connect(COMMAND_CHAT_DB_PATH)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name;")]
        print(f"tables = {tables}")
        conn.close()
