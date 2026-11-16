"""
dataset_a_capture.py — Dataset A write-only capture layer (v1.1)

Design contract (non-negotiable, per operator Standing Rules):
  1. NEVER raises. Every public function is wrapped so a capture failure
     cannot block or slow a pick from posting.
  2. NEVER reads pipeline state to make a decision. Write-only tap.
  3. Lives in its own SQLite file (data/warehouse/dataset_a.sqlite) —
     isolated from data/ops_center/. An operational failure in Mission
     Control must never corrupt historical evaluation data, and vice versa.
  4. eval_id is minted ONCE, at RESEARCH_STARTED, and is the permanent
     identity for that evaluation through every downstream stage. No
     second identity scheme is ever introduced.
  5. Capture failures log to #system via the caller's logger if provided,
     but never interrupt the caller.

Import from openclaw_core.py:
    import dataset_a_capture as d1

Call sites documented in DATASET_A_INTEGRATION_PATCH.md.
"""

import sqlite3
import json
import uuid
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB_PATH = Path(__file__).parent / "data" / "warehouse" / "dataset_a.sqlite"
_lock = threading.Lock()  # ThreadPoolExecutor(max_workers=4) runs props concurrently

VALID_STATES = {
    "RESEARCH_STARTED", "RESEARCH_COMPLETE", "EVALUATION_COMPLETE",
    "COMPLIANCE_COMPLETE", "SCORING_COMPLETE", "POSTED", "REJECTED",
    "FAILED", "ENGINE_DECLINED", "NO_PLAY", "OUTCOME_RECORDED", "VALIDATED",
}

VALID_TERMINATION_REASONS = {
    "GATE11_REJECTED", "GATE11_UNVERIFIED", "RESEARCH_FAILURE",
    "EVAL_TRUNCATED", "COMPLIANCE_UNPARSEABLE", "EVAL_ATTEMPTS_EXHAUSTED",
    "BROKEN_EVAL", "AUDIT_MALFORMED", "BP_RULE_BLOCKED", "RULE28_DEDUP",
    "MANUAL_REJECT", "SYSTEM_ERROR", "ENGINE_DECLINED", "NO_PLAY",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_capture_failures_table(conn):
    """Real, permanent, queryable record of every failure this capture
    layer swallows -- not a log line that rotates away. This is the
    direct fix for the dead end hit investigating 11 orphaned
    2026-07-23 evaluations: the log line that would have proven the
    exact cause was gone by the time anyone looked. This table can't
    rotate away the same way a text log can."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS capture_failures (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at     TEXT NOT NULL,
            function_name   TEXT NOT NULL,
            eval_id         TEXT,
            error_type      TEXT,
            error_message   TEXT,
            args_summary    TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_capture_failures_time
        ON capture_failures (occurred_at)
    """)


def _safe(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            # ALWAYS record to the permanent table -- previously this
            # only logged (a rotation-vulnerable text line) and only
            # when the caller happened to pass _logger, which 43 of
            # 124 real call sites in openclaw_core.py do not. Now every
            # failure is captured regardless of whether a logger was
            # passed, and survives log rotation.
            try:
                eval_id = kwargs.get("eval_id") or (args[0] if args else None)
                with _lock, _connect() as conn:
                    _ensure_capture_failures_table(conn)
                    conn.execute(
                        "INSERT INTO capture_failures "
                        "(occurred_at, function_name, eval_id, error_type, error_message, args_summary) "
                        "VALUES (?,?,?,?,?,?)",
                        (_now(), fn.__name__, str(eval_id) if eval_id else None,
                         type(e).__name__, str(e)[:500], str(args)[:200])
                    )
            except Exception:
                pass  # even the failure-recorder must never itself break a live evaluation
            # Keep the existing optional logger path too -- immediate,
            # human-visible signal in the log stream for anyone watching
            # live, in addition to the permanent record above.
            try:
                logger = kwargs.get("_logger")
                if logger:
                    logger("DatasetA", f"CAPTURE FAILURE (non-fatal): {fn.__name__}: {e}")
            except Exception:
                pass
            return None
    return wrapper


def get_recent_capture_failures(limit=50):
    """For Mission Control (Health Center): the real, permanent failure
    record -- what actually broke, when, in which function, for which
    eval_id if known. This is the answer to 'how do we know it's an
    error, what error, and what can be done to fix it' -- the exact
    error_message here is what a real fix gets written against, not a
    guess."""
    try:
        with _connect() as conn:
            _ensure_capture_failures_table(conn)
            rows = conn.execute(
                "SELECT occurred_at, function_name, eval_id, error_type, error_message "
                "FROM capture_failures ORDER BY occurred_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(zip(
                ("occurred_at", "function_name", "eval_id", "error_type", "error_message"), r
            )) for r in rows]
    except Exception:
        return []


@_safe
def new_eval_id() -> str:
    return str(uuid.uuid4())


@_safe
def start(eval_id, evaluation_type, player, opponent, tour=None, tournament=None,
          round_=None, surface=None, match_scheduled_time=None,
          market=None, line=None, pipeline_version=None, _logger=None):
    """Call once, at RESEARCH_STARTED — the row's birth. Everything after
    this is a transition on the same eval_id, never a new row."""
    assert evaluation_type in ("PROP", "MONEYLINE"), \
        f"evaluation_type must be PROP or MONEYLINE, got {evaluation_type!r}"
    # Fix (2026-07-23, found live): the schema's CHECK constraint is
    # tour IN ('ATP','WTA'), but the props pipeline passes the codebase's
    # internal lowercase convention ("atp"/"wta") straight through --
    # confirmed live: every prop evaluation's birth INSERT failed with
    # "CHECK constraint failed: tour IN ('ATP','WTA')", the row never
    # existed, and every later transition() became a silent no-op UPDATE
    # on a nonexistent eval_id. Dataset A sat at 0 rows while the log
    # dutifully recorded the non-fatal capture failures. Normalize here,
    # in the capture layer, so no caller's casing can ever cost the row:
    # a recognizable tour is uppercased; anything else (None, junk,
    # "atp tour") degrades to NULL -- the schema allows NULL, and a row
    # with an unknown tour beats no row at all, per the same fail-open
    # philosophy as everything else in this file.
    if isinstance(tour, str):
        _t = tour.strip().upper()
        tour = _t if _t in ("ATP", "WTA") else None
    else:
        tour = None
    with _lock, _connect() as conn:
        conn.execute("""
            INSERT INTO evaluations
                (eval_id, created_at, evaluation_type, lifecycle_state,
                 lifecycle_updated_at, player, opponent, tour, tournament,
                 round, surface, match_scheduled_time, market, line,
                 pipeline_version)
            VALUES (?,?,?, 'RESEARCH_STARTED', ?, ?,?,?,?,?,?,?,?,?, ?)
            ON CONFLICT(eval_id) DO NOTHING
        """, (eval_id, _now(), evaluation_type, _now(), player, opponent,
              tour, tournament, round_, surface, match_scheduled_time,
              market, line, pipeline_version))
    _log_transition(eval_id, None, "RESEARCH_STARTED", "")


@_safe
def _log_transition(eval_id, from_state, to_state, detail=""):
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO lifecycle_log (eval_id, from_state, to_state, logged_at, detail) "
            "VALUES (?,?,?,?,?)",
            (eval_id, from_state, to_state, _now(), detail)
        )


_NEW_MONEYLINE_DETAIL_COLUMNS = {
    "predicted_winner": "TEXT",
    "signals": "TEXT",                    # JSON: {s1..s5} -- transition() auto-serializes dicts
    "dominance": "TEXT",                  # JSON: full breakdown from compute_ml_dominance_score
    "phase2_band": "TEXT",
    "phase2_confirmation_status": "TEXT",
    "confirmed_signal_count": "INTEGER",
    "advisory": "INTEGER",                # 0/1 -- Flow D's real suggestion flag
    "suggested_winner": "TEXT",
    "suggested_confidence": "REAL",
}


def _ensure_moneyline_detail_columns(conn, fields):
    """Self-healing schema addition, same IF-NOT-EXISTS-safe spirit as
    _ensure_primitive_estimates_table/_ensure_capture_failures_table
    elsewhere in this file.

    2026-08-04, systematic audit (not a one-field reaction): compared
    every real field evaluate_moneyline() actually produces against
    every real column moneyline ever saved to Dataset A. Found the gap
    was much bigger than the single predicted_winner fix earlier today
    -- individual signal values (S1-S5), the full dominance breakdown,
    and the Phase 2 band/confirmation math were NEVER saved for any
    moneyline pick, ever. Separately, and more urgent: no_play()'s
    fixed 4-column save meant Flow D's real advisory data (built THIS
    session) reached Discord but never reached Dataset A at all -- a
    real miss caught only by going back and checking, not by design.

    Only touches columns actually present in `fields` for this call,
    not the whole table every time -- cheap, and only runs the ALTER
    TABLE attempt for columns this specific call is about to write."""
    for col in fields:
        if col in _NEW_MONEYLINE_DETAIL_COLUMNS:
            try:
                conn.execute(
                    f"ALTER TABLE evaluations ADD COLUMN {col} "
                    f"{_NEW_MONEYLINE_DETAIL_COLUMNS[col]}"
                )
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


@_safe
def transition(eval_id, to_state, detail="", _logger=None, **fields):
    """The one function most call sites use: advance lifecycle_state and
    optionally set any other columns in the same call (e.g. eval_confidence
    when moving to EVALUATION_COMPLETE). No-op if eval_id is None."""
    if not eval_id or to_state not in VALID_STATES:
        return
    cols, vals = ["lifecycle_state = ?", "lifecycle_updated_at = ?"], [to_state, _now()]
    for k, v in fields.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v, default=str)
        elif isinstance(v, bool):
            v = int(v)
        cols.append(f"{k} = ?")
        vals.append(v)
    vals.append(eval_id)
    with _lock, _connect() as conn:
        _ensure_moneyline_detail_columns(conn, fields)
        cur = conn.execute("SELECT lifecycle_state FROM evaluations WHERE eval_id = ?", (eval_id,))
        row = cur.fetchone()
        from_state = row[0] if row else None
        conn.execute(f"UPDATE evaluations SET {', '.join(cols)} WHERE eval_id = ?", vals)
    # Fix (2026-07-23): a transition on an eval_id with no birth row is a
    # silent no-op UPDATE -- exactly how the lowercase-tour bug hid for
    # so long (start() failed, every transition "succeeded" while
    # updating nothing). Still non-fatal, but now LOUD, so a missing row
    # can never again masquerade as a working pipeline.
    if row is None:
        try:
            logger = _logger
            if logger:
                logger("DatasetA", f"transition('{to_state}') on unknown eval_id "
                                    f"{eval_id} -- no birth row exists (start() "
                                    f"failed or was never called); UPDATE was a no-op")
        except Exception:
            pass
    _log_transition(eval_id, from_state, to_state, detail)


@_safe
def terminate(eval_id, reason, detail="", _logger=None):
    """REJECTED, FAILED, or ENGINE_DECLINED — reason must be one of
    VALID_TERMINATION_REASONS. Which lifecycle_state is inferred:
    REJECTED for gate/dedup/manual reasons, ENGINE_DECLINED for an
    intentional, expected engine decline (no odds yet, research-
    collection-only market, unusable line, match already started --
    not a system failure), FAILED for everything else (real
    system/pipeline breakage). An unrecognized reason string still
    downgrades to SYSTEM_ERROR/FAILED -- that fallback exists
    specifically to catch a caller passing a reason nobody taught this
    function about, so it must never itself be silently swallowed.

    APEX-HOTFIX-2026-09-28 FIX #1: ENGINE_DECLINED was previously not
    in VALID_TERMINATION_REASONS at all, so every engine decline
    (~990 historical rows, confirmed 100% decline-shaped on audit)
    fell through this same unrecognized-reason path and was recorded
    as SYSTEM_ERROR/FAILED -- indistinguishable from a real crash in
    Mission Control, #alerts, and every failure-rate metric."""
    if reason not in VALID_TERMINATION_REASONS:
        reason_ok = False
    else:
        reason_ok = True
    if reason in ("GATE11_REJECTED", "RULE28_DEDUP", "MANUAL_REJECT"):
        to_state = "REJECTED"
    elif reason == "ENGINE_DECLINED" and reason_ok:
        to_state = "ENGINE_DECLINED"
    else:
        to_state = "FAILED"
    transition(eval_id, to_state,
               detail=detail,
               termination_reason=(reason if reason_ok else "SYSTEM_ERROR"),
               termination_detail=detail,
               final_grade=to_state)


VALID_AGENTS = {
    "research", "evaluation", "compliance", "scoring", "posting",
    "learning", "validation", "ml_readiness",
}

VALID_HEARTBEAT_STATUS = {"IDLE", "WORKING", "BLOCKED", "FAILED", "OFFLINE"}


@_safe
def heartbeat(agent_name, status, current_task=None, detail=None,
              increment_success=False, increment_failure=False, _logger=None):
    """Priority 1 (Mission Control Phase 2, approved). One row per agent,
    continuously upserted — this is what lets Mission Control tell Idle
    apart from Working apart from Blocked apart from Offline, instead of
    inferring status purely from evaluation records. Call this at the
    start and end of each agent's work, not per-eval — it's a process
    liveness signal, not an evaluation-history signal (that's what
    lifecycle_log already is)."""
    if agent_name not in VALID_AGENTS or status not in VALID_HEARTBEAT_STATUS:
        return
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT success_count, failure_count FROM agent_heartbeat WHERE agent_name = ?",
            (agent_name,)
        ).fetchone()
        succ, fail = (row if row else (0, 0))
        if increment_success: succ += 1
        if increment_failure: fail += 1
        conn.execute("""
            INSERT INTO agent_heartbeat
                (agent_name, status, last_heartbeat, current_task, detail,
                 success_count, failure_count)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(agent_name) DO UPDATE SET
                status=excluded.status, last_heartbeat=excluded.last_heartbeat,
                current_task=excluded.current_task, detail=excluded.detail,
                success_count=excluded.success_count, failure_count=excluded.failure_count
        """, (agent_name, status, _now(), current_task, detail, succ, fail))


@_safe
def no_play(eval_id, detail="", _logger=None, **fields):
    """Deliberately separate from terminate(). A NO_PLAY is evidence
    OpenClaw completed its evaluation correctly and found no edge — it
    must never share a code path with REJECTED or FAILED, so a future
    refactor of terminate() can't accidentally collapse the distinction
    the operator specifically asked to preserve.

    **fields (2026-08-04, added): a NO_PLAY was previously a dead end
    for data -- signals, dominance breakdown, and (for moneyline Flow D)
    the real advisory suggestion all reached Discord but never reached
    Dataset A, since this function only ever saved 4 fixed columns.
    Existing callers passing no extra fields behave identically."""
    transition(eval_id, "NO_PLAY",
               detail=detail,
               termination_reason="NO_PLAY",
               termination_detail=detail,
               final_grade=None,
               **fields)


@_safe
def add_slip_member(slip_id, eval_id, _logger=None):
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO slip_members (slip_id, eval_id) VALUES (?,?)",
            (slip_id, eval_id)
        )
        conn.execute(
            "UPDATE evaluations SET slip_id = ? WHERE eval_id = ?", (slip_id, eval_id)
        )


@_safe
def record_outcome(eval_id, outcome, source, _logger=None):
    transition(eval_id, "OUTCOME_RECORDED",
               outcome=outcome, outcome_source=source,
               outcome_recorded_at=_now())


@_safe
def record_validation(eval_id, run_id, verdict, _logger=None):
    transition(eval_id, "VALIDATED",
               validation_run_id=run_id, validation_verdict=verdict)


def _ensure_primitive_estimates_table(conn):
    """Called lazily on first use, same non-destructive pattern as every
    other table this layer touches -- CREATE TABLE IF NOT EXISTS, never
    a migration that could touch existing data."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS primitive_estimates (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            eval_id                 TEXT,
            logged_at               TEXT NOT NULL,
            player                  TEXT,
            opponent                TEXT,
            stat_type               TEXT,
            line                    REAL,
            hold_pct                REAL,
            opp_hold_pct            REAL,
            bp_save_pct             REAL,
            break_pct               REAL,
            avg_aces                REAL,
            first_serve_pct         REAL,
            predicted_direction     TEXT,
            predicted_confidence    REAL,
            goblin_conflict         INTEGER DEFAULT 0,
            schema_version          TEXT NOT NULL,
            estimator_version       TEXT NOT NULL,
            actual_outcome          REAL,
            outcome_recorded_at     TEXT,
            data_completeness_pct   REAL,
            data_completeness_note  TEXT,
            match_model_win_prob         REAL,
            match_model_expected_games   REAL,
            match_model_expected_sets    REAL,
            shadow_recommendation   TEXT,
            shadow_reason_code      TEXT,
            shadow_edge             REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_primitive_estimates_version
        ON primitive_estimates (estimator_version, actual_outcome)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_primitive_estimates_eval_id
        ON primitive_estimates (eval_id)
    """)
    # Migration safety: if this table already exists from an earlier
    # deploy (2026-07-25, pre-shadow-fields version), CREATE TABLE IF NOT
    # EXISTS above does nothing -- these ALTERs backfill the new columns
    # on an existing table. Each wrapped individually so one already-
    # present column never blocks the rest; matches this file's fail-
    # open philosophy throughout.
    _new_cols = [
        ("data_completeness_pct", "REAL"), ("data_completeness_note", "TEXT"),
        ("match_model_win_prob", "REAL"), ("match_model_expected_games", "REAL"),
        ("match_model_expected_sets", "REAL"), ("shadow_recommendation", "TEXT"),
        ("shadow_reason_code", "TEXT"), ("shadow_edge", "REAL"),
    ]
    for col, coltype in _new_cols:
        try:
            conn.execute(f"ALTER TABLE primitive_estimates ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # column already exists -- expected on every call after the first


@_safe
def log_primitive_estimate(eval_id, player, opponent, stat_type, line,
                            primitives: dict, predicted_direction, predicted_confidence,
                            goblin_conflict, schema_version, estimator_version,
                            data_completeness_pct=None, data_completeness_note=None,
                            match_model_win_prob=None, match_model_expected_games=None,
                            match_model_expected_sets=None,
                            shadow_recommendation=None, shadow_reason_code=None,
                            shadow_edge=None, _logger=None):
    """
    Write-only, same contract as everything else in this file: never
    raises (wrapped by @_safe), never blocks a live evaluation. Called
    from openclaw_core.py's evaluation_agent() right after direction/
    confidence extraction -- captures the GENUINE prediction (see caller
    for how genuine_direction_pre_lock is threaded through when a Goblin/
    Demon lock later overrides evaluated["direction"]).

    `primitives` is a flat dict; any V8 didn't confirm for this prop comes
    through as None and is stored as NULL -- calibration scoring must
    treat NULL as "no data," never as zero.

    New optional params (2026-07-25, all default None so the original
    call site keeps working untouched): data_completeness_pct/note --
    what fraction of the 6 tracked primitives V8 actually confirmed for
    this specific pick. match_model_* -- tennis_match_model.py's exact,
    computed cross-check (win probability / expected games / expected
    sets from real hold-percentage math), stored purely as a SHADOW
    value for comparison -- does not feed back into or override the
    live evaluation. shadow_recommendation/reason_code/edge --
    recommendation_engine.py's BET/PASS read on this same pick, also
    shadow-only. None of these six new fields affect what actually gets
    posted; they exist so Mission Control can show them next to the
    real pick for comparison, and so the calibration engine can later
    ask whether these shadow signals would have helped.

    Distinct from evaluations/lifecycle_log/agent_heartbeat: this table
    tracks NUMBER accuracy (does hold_pct=0.88 match reality), not
    evaluation lifecycle. Read by estimator_calibration_ops.py on the
    Mission Control side, via calibration.py's Brier-score/reliability-
    diagram scoring -- not by anything in this file.
    """
    with _lock, _connect() as conn:
        _ensure_primitive_estimates_table(conn)
        conn.execute("""
            INSERT INTO primitive_estimates
            (eval_id, logged_at, player, opponent, stat_type, line,
             hold_pct, opp_hold_pct, bp_save_pct, break_pct, avg_aces, first_serve_pct,
             predicted_direction, predicted_confidence, goblin_conflict,
             schema_version, estimator_version,
             data_completeness_pct, data_completeness_note,
             match_model_win_prob, match_model_expected_games, match_model_expected_sets,
             shadow_recommendation, shadow_reason_code, shadow_edge)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            eval_id, _now(), player, opponent, stat_type, line,
            primitives.get("hold_pct"), primitives.get("opp_hold_pct"),
            primitives.get("bp_save_pct"), primitives.get("break_pct"),
            primitives.get("avg_aces"), primitives.get("first_serve_pct"),
            predicted_direction, predicted_confidence, int(bool(goblin_conflict)),
            schema_version, estimator_version,
            data_completeness_pct, data_completeness_note,
            match_model_win_prob, match_model_expected_games, match_model_expected_sets,
            shadow_recommendation, shadow_reason_code, shadow_edge,
        ))


@_safe
def record_settled_outcome(eval_id, actual_outcome: float, _logger=None):
    """
    Call once a match/prop settles and the real outcome is known -- from
    wherever Pick Results already gets its HIT/BUST determination. Fills
    in the two columns that start NULL at log time. Without this wired
    in somewhere real, every row from log_primitive_estimate() stays
    permanently unscoreable -- this is the second half of the loop, not
    optional. NOTE: this updates primitive_estimates only -- it does NOT
    call transition()/record_outcome() on the evaluations table; those
    are separate concerns and this function intentionally does not
    touch lifecycle state.
    """
    with _lock, _connect() as conn:
        _ensure_primitive_estimates_table(conn)
        conn.execute("""
            UPDATE primitive_estimates
            SET actual_outcome = ?, outcome_recorded_at = ?
            WHERE eval_id = ?
        """, (actual_outcome, _now(), eval_id))


# ================================================================
# STALE EVALUATION REAPER (APEX-P0-2026-09-28, Phase 2)
# ================================================================
# Root cause this exists to guard against: an evaluation entering a
# non-terminal lifecycle_state and never being force-terminated because
# the process that would have finished it froze or was interrupted
# (confirmed cause: Discord event-loop freezes, see Phase 1 fix in
# discord_bot.py). This reaper is a SAFETY NET, not the fix for that --
# it force-terminates anything abandoned past a generous threshold so
# Dataset A never carries a permanently-open evaluation, regardless of
# what interrupts a future run.
#
# Threshold justification (measured, not estimated): of 1,292 real
# evaluations that reached a genuine terminal state, the MAXIMUM
# completion time ever recorded was 788 seconds (~13.1 minutes); p99.9
# was 494.5s (~8.2 minutes); zero legitimate evaluations in this
# system's history have ever taken longer than 15 minutes. The 45-minute
# default below is a 3.4x margin over the longest evaluation ever
# observed -- it cannot mistake a real in-progress evaluation for an
# orphan against this system's own measured behavior.

STALE_NON_TERMINAL_STATES = {
    "RESEARCH_STARTED", "RESEARCH_COMPLETE", "EVALUATION_COMPLETE",
    "COMPLIANCE_COMPLETE", "SCORING_COMPLETE",
}
DEFAULT_STALE_MINUTES = 45


@_safe
def reap_stale_evaluations(stale_minutes=DEFAULT_STALE_MINUTES, _logger=None):
    """Force-terminate any evaluation that has sat in a non-terminal
    lifecycle_state for longer than stale_minutes with no further
    activity. Reuses terminate() -- the same write path every other
    caller uses -- so reaped rows go through identical validation and
    logging as any other termination; there is no parallel write path.

    reason="SYSTEM_ERROR" is deliberate: an evaluation abandoned by a
    process interruption is a genuine system failure, not an engine
    decline or a gate rejection, and should be counted as such.
    termination_detail records the prior state and how long it sat
    there, so a reaped row is distinguishable in Dataset A from any
    other SYSTEM_ERROR cause.

    Returns a list of {"eval_id", "prior_state", "detail"} for every
    row it terminated, so a caller can log/report counts. Wrapped in
    @_safe like every other public function here -- a bug in the
    reaper itself can never block or crash the caller that invoked it.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)).isoformat()
    states = tuple(STALE_NON_TERMINAL_STATES)
    placeholders = ",".join("?" * len(states))
    with _lock, _connect() as conn:
        rows = conn.execute(
            f"SELECT eval_id, lifecycle_state, lifecycle_updated_at FROM evaluations "
            f"WHERE lifecycle_state IN ({placeholders}) AND lifecycle_updated_at < ?",
            (*states, cutoff)
        ).fetchall()
    reaped = []
    for eval_id, state, updated_at in rows:
        detail = (f"reaper: abandoned in {state} since {updated_at} "
                  f"(non-terminal for over {stale_minutes} min with no further "
                  f"activity -- orphaned by a process interruption/restart "
                  f"mid-pipeline, not a live decision)")
        terminate(eval_id, "SYSTEM_ERROR", detail, _logger=_logger)
        reaped.append({"eval_id": eval_id, "prior_state": state, "detail": detail})
    if reaped and _logger:
        _logger("DatasetA", f"reap_stale_evaluations: force-terminated "
                             f"{len(reaped)} stale eval(s) (non-terminal "
                             f"for >{stale_minutes}min)")
    return reaped

# © 2026 Demetrius Reynolds — Project Apex Intelligence™. All rights reserved.
