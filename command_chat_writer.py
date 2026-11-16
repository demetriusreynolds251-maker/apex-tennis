"""
================================================================
COMMAND CHAT V1 — WRITER + SEARCH LAYER
================================================================
Phase 1: decision_records / investigation_records writers.
Phase 2: command_chat_log writer (conversation history).
Phase 3: FTS5 search over both memory tables.

Same fail-open discipline as ate_writer.py where it matters (chat
log writes must never break a conversation), but decision/
investigation writes are deliberate, operator-facing actions, not
part of a live evaluation pipeline — they're allowed to raise so a
real write failure is visible immediately to whoever is authoring
the record, rather than silently lost the way a missed trace would
be acceptable to lose.
================================================================
"""

import sqlite3
import time
from pathlib import Path

from command_chat_db import COMMAND_CHAT_DB_PATH, ensure_command_chat_db

_FAILURE_LOG = Path(__file__).parent / "command_chat_failures.log"


def _log_failure(message):
    try:
        with open(_FAILURE_LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {message}\n")
    except Exception:
        pass


def _connect(db_path=None):
    return sqlite3.connect(db_path or COMMAND_CHAT_DB_PATH, timeout=5)


# ================================================================
# PHASE 1 — decision_records / investigation_records
# ================================================================

def add_decision(date, topic, decision, reasoning, related_files=None,
                  related_rule_ids=None, db_path=None):
    """Deliberate write — raises on failure rather than fail-open,
    since this is an operator-authored record, not pipeline telemetry."""
    ensure_command_chat_db(db_path)
    conn = _connect(db_path)
    cur = conn.execute(
        "INSERT INTO decision_records "
        "(date, topic, decision, reasoning, related_files, related_rule_ids) "
        "VALUES (?,?,?,?,?,?)",
        (date, topic, decision, reasoning, related_files, related_rule_ids))
    decision_id = cur.lastrowid
    conn.execute(
        "INSERT INTO memory_search (source_type, source_id, title, body, created_at) "
        "VALUES ('decision', ?, ?, ?, datetime('now'))",
        (str(decision_id), topic, f"{decision}\n{reasoning}"))
    conn.commit()
    conn.close()
    return decision_id


def add_investigation(date, topic, question, finding, conclusion=None,
                       related_files=None, related_evaluations=None, db_path=None):
    ensure_command_chat_db(db_path)
    conn = _connect(db_path)
    cur = conn.execute(
        "INSERT INTO investigation_records "
        "(date, topic, question, finding, conclusion, related_files, related_evaluations) "
        "VALUES (?,?,?,?,?,?,?)",
        (date, topic, question, finding, conclusion, related_files, related_evaluations))
    investigation_id = cur.lastrowid
    conn.execute(
        "INSERT INTO memory_search (source_type, source_id, title, body, created_at) "
        "VALUES ('investigation', ?, ?, ?, datetime('now'))",
        (str(investigation_id), topic, f"{question}\n{finding}\n{conclusion or ''}"))
    conn.commit()
    conn.close()
    return investigation_id


# ================================================================
# PHASE 2 — command_chat_log (conversation memory, not "infinite
# context" — see the readiness review's caching/New-Chat section)
# ================================================================

def log_chat_message(session_id, role, content, sources_cited=None, db_path=None):
    """Fail-open — a lost log line should never break the chat UI
    itself, unlike a lost decision/investigation record."""
    try:
        ensure_command_chat_db(db_path)
        conn = _connect(db_path)
        import json as _json
        conn.execute(
            "INSERT INTO command_chat_log (session_id, role, content, sources_cited) "
            "VALUES (?,?,?,?)",
            (session_id, role, content,
             _json.dumps(sources_cited) if sources_cited is not None else None))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        _log_failure(f"log_chat_message failed for session={session_id}: {e}")
        return False


# ================================================================
# SESSION HISTORY — read-only, per COMMAND CHAT UX V1.1 authorization.
# No new table, no new business logic — a plain SELECT over the
# existing command_chat_log, grouped in Python (SQLite's GROUP BY
# doesn't guarantee which row's other columns come back alongside
# MIN/MAX, so the first/last message per session is computed
# explicitly here rather than relied on implicitly).
# ================================================================

def list_sessions(limit=30, db_path=None):
    """
    One row per distinct session_id: the first user message (as a
    preview) and the timestamp of the most recent message in that
    session, most-recently-active first.
    """
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT session_id, role, content, created_at FROM command_chat_log "
        "ORDER BY created_at ASC").fetchall()
    conn.close()

    by_session = {}
    for r in rows:
        s = by_session.setdefault(r["session_id"], {
            "session_id": r["session_id"], "preview": None,
            "first_at": r["created_at"], "last_at": r["created_at"],
            "message_count": 0})
        s["message_count"] += 1
        s["last_at"] = r["created_at"]
        if s["preview"] is None and r["role"] == "user":
            s["preview"] = r["content"][:80]

    sessions = list(by_session.values())
    sessions.sort(key=lambda s: s["last_at"], reverse=True)
    return sessions[:limit]


def get_session_messages(session_id, db_path=None):
    """Full message history for one session, in order — used to
    reopen a prior conversation."""
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT role, content, sources_cited, created_at FROM command_chat_log "
        "WHERE session_id = ? ORDER BY message_id ASC", (session_id,)).fetchall()
    conn.close()
    import json as _json
    result = []
    for r in rows:
        d = dict(r)
        if d.get("sources_cited"):
            try:
                d["sources_cited"] = _json.loads(d["sources_cited"])
            except Exception:
                d["sources_cited"] = None
        result.append(d)
    return result


# ================================================================
# PHASE 3 — FTS5 search
# ================================================================

def search_decisions(query, limit=10, db_path=None):
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT d.* FROM decision_records d "
        "JOIN memory_search m ON m.source_type='decision' AND m.source_id = CAST(d.decision_id AS TEXT) "
        "WHERE memory_search MATCH ? AND m.source_type='decision' "
        "ORDER BY rank LIMIT ?",
        (query, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_investigations(query, limit=10, db_path=None):
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT i.* FROM investigation_records i "
        "JOIN memory_search m ON m.source_type='investigation' AND m.source_id = CAST(i.investigation_id AS TEXT) "
        "WHERE memory_search MATCH ? AND m.source_type='investigation' "
        "ORDER BY rank LIMIT ?",
        (query, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ================================================================
# MEMORY CENTER PHASE 1 — graph_edges. Deliberate writes, same
# raise-don't-fail-open contract as decisions/investigations (an
# edge is a curated fact, not pipeline telemetry).
# ================================================================

def add_edge(source_type, source_id, edge_type, target_type, target_id, db_path=None):
    ensure_command_chat_db(db_path)
    conn = _connect(db_path)
    cur = conn.execute(
        "INSERT INTO graph_edges (source_type, source_id, edge_type, target_type, target_id) "
        "VALUES (?,?,?,?,?)",
        (source_type, str(source_id), edge_type, target_type, str(target_id)))
    edge_id = cur.lastrowid
    conn.commit()
    conn.close()
    return edge_id


def decision_id_by_topic(topic, db_path=None):
    """Look up a decision's ID by its exact topic string — used by
    the edge backfill so it never has to assume row IDs match a
    particular insertion order (which can legitimately differ
    between two databases seeded at different times)."""
    conn = _connect(db_path)
    row = conn.execute("SELECT decision_id FROM decision_records WHERE topic = ?", (topic,)).fetchone()
    conn.close()
    return row[0] if row else None


def investigation_id_by_topic(topic, db_path=None):
    conn = _connect(db_path)
    row = conn.execute("SELECT investigation_id FROM investigation_records WHERE topic = ?", (topic,)).fetchone()
    conn.close()
    return row[0] if row else None


def get_graph_data(db_path=None):
    """
    Everything the Memory Center screen needs in one call: every
    decision/investigation as a node (rule/system nodes are implied
    by any edge referencing them, not stored as their own rows —
    see graph_edges' schema comment), plus every edge. Shaped
    directly for Cytoscape.js consumption on the frontend.
    """
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row

    nodes = []
    for r in conn.execute("SELECT decision_id AS id, topic AS label, date FROM decision_records"):
        nodes.append({"id": f"decision:{r['id']}", "label": r["label"], "type": "decision",
                      "date": r["date"]})
    for r in conn.execute("SELECT investigation_id AS id, topic AS label, date FROM investigation_records"):
        nodes.append({"id": f"investigation:{r['id']}", "label": r["label"], "type": "investigation",
                      "date": r["date"]})

    edges = conn.execute("SELECT * FROM graph_edges").fetchall()
    seen_rule_system_ids = set()
    edge_list = []
    for e in edges:
        src_node_id = f"{e['source_type']}:{e['source_id']}"
        tgt_node_id = f"{e['target_type']}:{e['target_id']}"
        # rule/system nodes aren't in their own table -- materialize
        # them here, the first time an edge references them, rather
        # than requiring a separate registration step.
        for t, i, node_id in ((e["source_type"], e["source_id"], src_node_id),
                               (e["target_type"], e["target_id"], tgt_node_id)):
            if t in ("rule", "system") and node_id not in seen_rule_system_ids:
                nodes.append({"id": node_id, "label": i, "type": t})
                seen_rule_system_ids.add(node_id)
        edge_list.append({"id": f"edge{e['edge_id']}", "source": src_node_id,
                           "target": tgt_node_id, "label": e["edge_type"]})

    conn.close()
    return {"nodes": nodes, "edges": edge_list}


def get_related(node_type, node_id, db_path=None):
    """
    Direct neighbors of one node — the Related panel query (Capacities
    pattern, Phase 3 in the finalized design, implemented here since
    it's the same graph_edges table). Returns both directions: edges
    where this node is the source, and edges where it's the target.
    """
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    outgoing = conn.execute(
        "SELECT * FROM graph_edges WHERE source_type = ? AND source_id = ?",
        (node_type, str(node_id))).fetchall()
    incoming = conn.execute(
        "SELECT * FROM graph_edges WHERE target_type = ? AND target_id = ?",
        (node_type, str(node_id))).fetchall()
    conn.close()
    return {
        "outgoing": [{"edge_type": r["edge_type"], "target_type": r["target_type"],
                       "target_id": r["target_id"]} for r in outgoing],
        "incoming": [{"edge_type": r["edge_type"], "source_type": r["source_type"],
                       "source_id": r["source_id"]} for r in incoming],
    }
