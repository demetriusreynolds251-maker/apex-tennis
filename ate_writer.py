"""
================================================================
ATE V1 — CAPTURE LAYER (WRITER)
================================================================
NON-NEGOTIABLE PROPERTY: no function in this file may ever raise.
Every public function catches everything, logs locally, and
returns. ATE exists only as an observer — it must never influence
evaluation control flow, and a missing trace is always acceptable
where a failed evaluation is not.

Approved V1 scope: ai_traces, rule_activations, checkpoint_stamps.
confidence_ledger_events is deferred — do not add write functions
for it here without a separate, explicit approval.
================================================================
"""

import re
import sqlite3
import time
from pathlib import Path

from ate_db import ATE_DB_PATH, ensure_ate_db

_FAILURE_LOG = Path(__file__).parent / "ate_failures.log"


def _log_failure(message):
    """Last line of defense. Even this is wrapped — if local disk
    logging itself fails (full disk, permissions), there is nothing
    further to do, and this must not raise either."""
    try:
        with open(_FAILURE_LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {message}\n")
    except Exception:
        pass


def _connect(db_path=None):
    """Short timeout (2s) so lock contention fails fast into the
    caller's except block rather than hanging the pipeline. WAL
    mode (set at schema creation) makes contention rare, but the
    timeout is the actual guarantee, not WAL mode alone."""
    return sqlite3.connect(db_path or ATE_DB_PATH, timeout=2)


# ================================================================
# ai_traces
# ================================================================

def write_trace(eval_id, step_type, model, prompt, response,
                 success=True, error_message=None, stop_reason=None,
                 latency_ms=None, tokens_in=None, tokens_out=None,
                 cache_creation_tokens=None, cache_read_tokens=None,
                 cost_usd=None, db_path=None):
    """
    One row per Claude call that a caller has opted into tracing
    (step_type is required and must be one of the three approved
    values — see ate_schema_v1.sql's CHECK constraint). Call sites
    that don't pass step_type at all should never reach this
    function in the first place (see call_model() integration).

    Returns the new trace_id on success, or None on any failure —
    callers should treat None the same as "tracing didn't happen
    this time" and continue without checking further.
    """
    try:
        ensure_ate_db(db_path)
        conn = _connect(db_path)
        cur = conn.execute(
            "INSERT INTO ai_traces "
            "(eval_id, step_type, model, prompt, response, success, "
            " error_message, stop_reason, latency_ms, tokens_in, tokens_out, "
            " cache_creation_tokens, cache_read_tokens, cost_usd) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eval_id, step_type, model, prompt, response, int(bool(success)),
             error_message, stop_reason, latency_ms, tokens_in, tokens_out,
             cache_creation_tokens, cache_read_tokens, cost_usd)
        )
        trace_id = cur.lastrowid
        conn.commit()
        conn.close()
        return trace_id
    except Exception as e:
        _log_failure(f"write_trace failed for eval_id={eval_id}, "
                      f"step_type={step_type}: {e}")
        return None


# ================================================================
# rule_activations
# ================================================================

def write_rule_activation(eval_id, sequence_order, rule_name, source, status,
                           trace_id=None, category=None, confidence_delta=None,
                           reasoning_snippet=None, rule_version=None, db_path=None):
    """One row per rule mention (master-eval confirmed/challenged,
    or a compliance flag). Fail-open, same contract as write_trace."""
    try:
        ensure_ate_db(db_path)
        conn = _connect(db_path)
        conn.execute(
            "INSERT INTO rule_activations "
            "(eval_id, trace_id, sequence_order, rule_name, rule_version, "
            " source, status, category, confidence_delta, reasoning_snippet) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eval_id, trace_id, sequence_order, rule_name, rule_version,
             source, status, category, confidence_delta, reasoning_snippet)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        _log_failure(f"write_rule_activation failed for eval_id={eval_id}, "
                      f"rule={rule_name}: {e}")
        return False


def capture_rule_activations(eval_id, trace_id, rules_confirmed, rules_challenged,
                              db_path=None):
    """
    Convenience wrapper matching the shape openclaw_core.py already
    produces: evaluated["rules_confirmed"] / evaluated["rules_challenged"],
    both lists of strings like "Rule24" (from the existing
    _extract_rules() function — nothing new is parsed here, this
    just persists what openclaw_core.py already extracted).

    Wrapped in its own try/except even though write_rule_activation
    already fail-opens internally — this guards the sequence-
    numbering loop itself, in case either input isn't the list
    shape expected (e.g. None instead of []).
    """
    try:
        seq = 0
        for r in (rules_confirmed or []):
            seq += 1
            write_rule_activation(eval_id=eval_id, sequence_order=seq,
                                   rule_name=r, source="master_eval",
                                   status="CONFIRMED", trace_id=trace_id,
                                   db_path=db_path)
        for r in (rules_challenged or []):
            seq += 1
            write_rule_activation(eval_id=eval_id, sequence_order=seq,
                                   rule_name=r, source="master_eval",
                                   status="CHALLENGED", trace_id=trace_id,
                                   db_path=db_path)
    except Exception as e:
        _log_failure(f"capture_rule_activations failed for eval_id={eval_id}: {e}")


def capture_compliance_flag_activations(eval_id, trace_id, flags, db_path=None):
    """
    Compliance flags arrive as structured JSON already (confirmed:
    {"rule": "Rule19", "violation": "...", "category": "B"}) — no
    parsing needed, same principle as write_trace's cost fields:
    persist what already exists rather than re-derive it.
    """
    try:
        seq = 0
        for f in (flags or []):
            seq += 1
            write_rule_activation(
                eval_id=eval_id, sequence_order=seq,
                rule_name=f.get("rule", "UNKNOWN"), source="compliance",
                status="FLAGGED", trace_id=trace_id,
                category=f.get("category"),
                reasoning_snippet=f.get("violation"),
                db_path=db_path,
            )
    except Exception as e:
        _log_failure(f"capture_compliance_flag_activations failed for "
                      f"eval_id={eval_id}: {e}")


# ================================================================
# checkpoint_stamps
# ================================================================

# Copied verbatim from green_validation_suite.py's _SECTION_PAT
# (confirmed via archive verification, 2026-07-13: 32 real matches
# against a current 2026-07-09 evaluation). NOT imported directly —
# green_validation_suite.py imports openclaw_core as `oc`, so an
# import in the other direction would be circular. If the source
# regex in green_validation_suite.py ever changes, this copy needs
# to be updated manually to match.
_SECTION_PAT = re.compile(
    r"(?im)^\s*(?:\*\*|#+\s*)?(?:\[?CHECKPOINT\s+[A-L1-9][\]:]?|GATE\s*\d+|"
    r"HARD STOP|STEP \d|PART \d|DOMINANCE|SCORE PATHS?|CONFIDENCE|GRADE|"
    r"MATCH CONTROL|COMPETITION.CONTEXT|STRUCTURE|SURVIVAL)")

# Storage-cost tradeoff, flagged explicitly in the schema and here:
# full section bodies on every checkpoint of every evaluation is
# real growth. Default OFF for V1 — stamp_text (the header line)
# is captured either way; section_text is opt-in.
CAPTURE_SECTION_TEXT = False

_LABEL_TYPE_MAP = (
    (re.compile(r"CHECKPOINT", re.I), "CHECKPOINT"),
    (re.compile(r"GATE", re.I), "GATE"),
    (re.compile(r"HARD STOP", re.I), "HARD_STOP"),
    (re.compile(r"PART", re.I), "PART"),
)


def _classify_label(raw_label):
    for pat, label_type in _LABEL_TYPE_MAP:
        if pat.search(raw_label):
            return label_type
    return "OTHER"


def capture_checkpoint_stamps(eval_id, trace_id, response_text, db_path=None):
    """
    Splits a master-evaluation response into its real internal
    sections using _SECTION_PAT, and writes one row per section
    found. This is the one ATE function whose core logic (regex
    parsing of model-generated text) is new and unproven at scale
    — wrapped as a single fail-open unit, same standard as every
    other capture function, so a parsing surprise on some future
    evaluation's format can never reach the pipeline either.
    """
    try:
        if not response_text:
            return
        matches = list(_SECTION_PAT.finditer(response_text))
        if not matches:
            return
        seq = 0
        for i, m in enumerate(matches):
            seq += 1
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(response_text)
            section = response_text[start:end]
            # m.start() can point to whitespace consumed by the pattern's
            # leading \s* (e.g. a blank line before "CHECKPOINT A"), not
            # the label text itself — find the real content start rather
            # than trusting splitlines()[0] on the raw slice.
            content_start = start
            while content_start < len(response_text) and response_text[content_start] in "\n\r\t ":
                content_start += 1
            line_end = response_text.find("\n", content_start)
            if line_end == -1:
                line_end = len(response_text)
            raw_label = response_text[content_start:line_end].strip()[:80]
            stamp_text = raw_label
            label_type = _classify_label(raw_label)
            write_checkpoint_stamp(
                eval_id=eval_id, trace_id=trace_id, sequence_order=seq,
                label_type=label_type, label=raw_label, stamp_text=stamp_text,
                section_text=(section if CAPTURE_SECTION_TEXT else None),
                db_path=db_path,
            )
    except Exception as e:
        _log_failure(f"capture_checkpoint_stamps failed for eval_id={eval_id}: {e}")


def write_checkpoint_stamp(eval_id, sequence_order, label_type, label, stamp_text,
                            trace_id=None, section_text=None, db_path=None):
    try:
        ensure_ate_db(db_path)
        conn = _connect(db_path)
        conn.execute(
            "INSERT INTO checkpoint_stamps "
            "(eval_id, trace_id, sequence_order, label_type, label, "
            " stamp_text, section_text) VALUES (?,?,?,?,?,?,?)",
            (eval_id, trace_id, sequence_order, label_type, label,
             stamp_text, section_text)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        _log_failure(f"write_checkpoint_stamp failed for eval_id={eval_id}, "
                      f"label={label}: {e}")
        return False
