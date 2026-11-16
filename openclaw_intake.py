# -*- coding: utf-8 -*-
"""
OPENCLAW MISSION CONTROL — INTAKE (v1.1, 2026-07-23)
Turns Mission Control from a MONITOR into an OPERATOR.

Discovery: the bot already exposes evaluate_manual_prop(user_input,
dates, weights, image_data, media_type) — the exact entry point
#general uses for text props AND screenshots. Mission Control operates
OpenClaw by calling it. No bot changes.

Design: a file-backed queue (data/ops_center/intake_queue.json) plus a
single background worker thread. The web request NEVER blocks on an
evaluation — it enqueues, returns immediately, and the operator watches
the prop move through the Operations canvas (fed by the same log the
pipeline writes). Queue survives restarts; the worker is idempotent.

v1.1 — Batch API toggle (2026-07-23, approved)
Props marked batch_eligible=True bypass the synchronous evaluation call
and go through Anthropic's Message Batches API (/v1/messages/batches)
at 50% of normal token price. Research + gates still run synchronously
(they are cheap V8/web calls, not model calls); only the expensive
Sonnet evaluation call is deferred. Results come back within 2-8 hours;
a background poller checks every 10 minutes and runs compliance →
scoring → posting when results arrive — identical integrity to the live
path, just not instant.

Status flow:
  QUEUED → RUNNING → DONE              (live path)
  QUEUED → RUNNING → BATCH_PENDING     (batch path, after submit)
  BATCH_PENDING → RUNNING → DONE       (poller picks up completed batch)
  Any → FAILED                         (error, retryable)
"""
import json, os, re, csv, threading, time, base64, traceback, datetime as dt, requests
from pathlib import Path

import openclaw_ops as ops
import openclaw_core as oc

QUEUE = ops.OPS_DIR / "intake_queue.json"
_lock = threading.Lock()

def _load():
    try: return json.loads(QUEUE.read_text())
    except Exception: return []

def _save(q):
    """
    2026-08-04 — real bug fix. This used to write straight to QUEUE via
    write_text(), which is NOT atomic: it truncates the file to empty,
    then writes the new content. _load() has no lock at all, so a read
    landing in that window saw a torn/partial file -- and since the b64
    image field is by far the longest value in any item, it was the
    piece most likely to end up truncated. A cut-off base64 string is
    exactly "invalid base64 data" -- this was confirmed as the real
    cause of a live screenshot-intake failure this session (Discord
    #system log showed that exact error, matching a batch-queued
    screenshot that had been read/rewritten many times while sitting
    in the queue).

    Fix: write the full new content to a unique temp file first, then
    os.replace() it onto the real path. os.replace() is atomic on
    POSIX (macOS included) -- any reader either sees the complete old
    file or the complete new file, never something in between,
    regardless of timing. No change needed on the _load() side; this
    alone removes the torn-read window entirely.
    """
    with _lock:
        tmp = QUEUE.with_name(f"{QUEUE.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(q, indent=2, default=str))
        os.replace(tmp, QUEUE)

def enqueue(kind, payload, batch_eligible=False):
    """kind='text'     -> payload={'text': 'Player MORE/LESS LINE STAT'}
       kind='image'    -> payload={'b64':..., 'media_type':...}
       kind='ml_image' -> payload={'b64':..., 'media_type':...}

       batch_eligible=True: research runs synchronously, evaluation
       call submitted to Anthropic Batch API (50% off), results posted
       when the batch completes (2-8h). Only 'text' and 'image' kinds
       support batch; 'ml_image' is always synchronous.
    """
    # ml_image batch is supported (v1.1 update): moneyline screenshots
    # use MASTER_PROMPT + Sonnet just like props — the batch API saves
    # the same ~50% regardless of max_tokens ceiling. The parse step
    # (screenshot → player names) still runs synchronously since it's
    # a cheap vision call; only the evaluation model call is deferred.

    q = _load()
    item = {
        "id": (max([i["id"] for i in q], default=0) + 1),
        "kind": kind,
        "payload": payload,
        "status": "QUEUED",
        "label": (payload.get("text")
                  or ("moneyline screenshot" if kind == "ml_image"
                      else "screenshot")),
        "queued_at": dt.datetime.now(oc.CT).strftime("%I:%M %p").lstrip("0"),
        "error": None,
        # Batch fields (v1.1) — None until batch_eligible item processes
        "batch_eligible": batch_eligible,
        "batch_id": None,        # Anthropic batch object ID (msgbatch_...)
        "batch_custom_id": None, # per-item custom_id within the batch
        "_batch_prop": None,     # serialized prop state for poller resumption;
                                 # stripped in queue_view() — never sent to browser
    }
    q.append(item); _save(q)
    return item["id"]

def queue_view():
    """Public, image bytes and batch prop state stripped — safe to
    serialize to the browser."""
    return [{k: v for k, v in i.items()
             if k not in ("payload", "_batch_prop")} |
            {"label": i["label"]} for i in _load()][-25:]

def _set(item_id, **fields):
    q = _load()
    for i in q:
        if i["id"] == item_id:
            i.update(fields)
    _save(q)

# ================================================================
# LIVE PATH — synchronous evaluation (unchanged from v1.0)
# ================================================================

def _process(item):
    dates = oc.get_system_dates()
    weights = oc.load_weights()
    if item["kind"] == "ml_image":
        # MONEYLINE SCREENSHOT (2026-07-23, approved). Mirrors the proven
        # Discord #general flow exactly: one Sonnet vision parse with the
        # same SCREENSHOT_PARSE_PROMPT, the same classifier, then hand off
        # to the same evaluate_moneyline_screenshot() that already owns
        # tournament resolution, Dataset A capture, gated posting, and the
        # odds-are-display-only rule. Nothing here re-implements a step
        # the Discord path hasn't already proven live. Failures RAISE so
        # the worker marks the item FAILED with the real reason visible in
        # the queue (and retryable) -- never a silent wrong-path evaluation.
        b64 = item["payload"]["b64"]
        media_type = item["payload"].get("media_type", "image/png")
        parse_result = oc.call_model(
            oc.MODEL_SONNET,
            oc.SCREENSHOT_PARSE_PROMPT,
            [{"type": "image",
              "source": {"type": "base64", "media_type": media_type, "data": b64}},
             {"type": "text",
              "text": "Extract player names, opponent names, and lines from this screenshot."}],
            temperature=0.1,
            max_tokens=800,
        )
        if not parse_result:
            raise ValueError("could not read the screenshot -- send a clearer image")
        import re as _re
        try:
            parsed = json.loads(_re.sub(r"```json|```", "", parse_result).strip())
        except Exception:
            raise ValueError("screenshot could not be parsed as JSON -- send a clearer image")
        from discord_message_classifier import classify_image_content
        if classify_image_content(parsed) != "moneyline":
            raise ValueError("this looks like a props board, not a moneyline screen -- "
                             "use the PrizePicks screenshot input above instead")
        player, opponent = parsed.get("player", ""), parsed.get("opponent", "")
        if not player or not opponent:
            raise ValueError("could not read both player names from the moneyline screen")
        oc.evaluate_moneyline_screenshot(player, opponent,
                                          parsed.get("player_odds", ""),
                                          parsed.get("opponent_odds", ""), dates)
    elif item["kind"] == "image":
        b64 = item["payload"]["b64"]
        oc.evaluate_manual_prop("", dates, weights,
                                image_data=b64,
                                media_type=item["payload"].get("media_type", "image/png"))
    else:
        oc.evaluate_manual_prop(item["payload"]["text"], dates, weights)

# ================================================================
# BATCH PATH — Anthropic Message Batches API (v1.1)
# ================================================================

def _process_batch_submit(item):
    """
    Pre-evaluation phase for a batch-eligible item. Runs parsing +
    tournament context + V8 prefetch + gates + research synchronously
    (these are cheap API calls, not model calls), builds the evaluation
    content string, then submits just the Sonnet evaluation call to
    Anthropic's /v1/messages/batches endpoint at 50% of normal price.

    On success: sets item status BATCH_PENDING with batch_id stored.
    On failure: raises ValueError so the worker marks FAILED.

    Called from _worker() when item['batch_eligible'] is True.
    """
    dates   = oc.get_system_dates()
    weights = oc.load_weights()

    # ── Pre-evaluation: research + gates + content build ─────────────
    # For props (text/image): prepare_batch_prop runs research + gates.
    # For moneyline (ml_image): prepare_batch_ml_prop parses screenshot
    # + resolves context. Both return (state_dict, content_str).
    _is_ml = (item["kind"] == "ml_image")
    if _is_ml:
        prop, content = oc.prepare_batch_ml_prop(item["payload"], dates)
    else:
        kind_for_core = item["kind"]  # 'text' or 'image'
        prop, content = oc.prepare_batch_prop(kind_for_core, item["payload"],
                                               dates, weights)

    # ── Submit to Anthropic Batch API ────────────────────────────────
    # Moneyline uses max_tokens=6000 (matching moneyline_evaluation_agent);
    # props use 16000. Both use MASTER_PROMPT + cache_control.
    EVAL_MAX_TOKENS = 6000 if _is_ml else 16000
    custom_id = f"intake-{item['id']}"

    batch_payload = {
        "requests": [{
            "custom_id": custom_id,
            "params": {
                "model":       oc.MODEL_SONNET,
                "max_tokens":  EVAL_MAX_TOKENS,
                "temperature": 0.2,
                "system": [{
                    "type":          "text",
                    "text":          oc.MASTER_PROMPT,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }],
                "messages": [{"role": "user", "content": content}],
            }
        }]
    }

    resp = requests.post(
        "https://api.anthropic.com/v1/messages/batches",
        headers={
            "x-api-key":          oc.ANTHROPIC_API_KEY,
            "anthropic-version":  "2023-06-01",
            "content-type":       "application/json",
        },
        json=batch_payload,
        timeout=30,
    )
    if resp.status_code != 200:
        raise ValueError(
            f"Batch API rejected submission: HTTP {resp.status_code} — "
            f"{resp.text[:200]}"
        )

    batch_data         = resp.json()
    anthropic_batch_id = batch_data.get("id", "")
    if not anthropic_batch_id:
        raise ValueError(f"Batch API returned no id: {resp.text[:200]}")

    # Serialize prop for the poller to resume from. Strip the raw V8
    # packet (can be 50k+ chars of match data) and any b64 image — the
    # research_data extracted from it is already in prop and that's all
    # finalize_batch_evaluation needs.
    prop_snapshot = {
        k: v for k, v in prop.items()
        if k not in ("_v8_packet_prefetched",)
    }

    _set(item["id"],
         status="BATCH_PENDING",
         batch_id=anthropic_batch_id,
         batch_custom_id=custom_id,
         _batch_prop=prop_snapshot,
         _batch_ml=_is_ml,   # tells poller which finalize function to call
         submitted_at=dt.datetime.now(oc.CT).strftime("%I:%M %p").lstrip("0"))

    oc.log("Batch",
           f"#{item['id']} ({item['label']}) → submitted to Anthropic "
           f"batch {anthropic_batch_id} as {custom_id}")


def _batch_poller():
    """
    Background thread: checks every 10 minutes for BATCH_PENDING items
    whose Anthropic batch has completed, then runs the post-evaluation
    pipeline (compliance → scoring → posting) for each result.

    One Anthropic batch per intake item in the current MVP (submitted
    individually in _process_batch_submit). The poller groups by
    batch_id so it only calls the Anthropic status endpoint once per
    unique batch regardless of how many items share one.
    """
    CHECK_INTERVAL = 600  # 10 min — balances API rate limits vs latency

    while True:
        try:
            time.sleep(CHECK_INTERVAL)

            q       = _load()
            pending = [i for i in q
                       if i.get("status") == "BATCH_PENDING"
                       and i.get("batch_id")]
            if not pending:
                continue

            # Group items by their Anthropic batch_id
            batches: dict = {}
            for item in pending:
                bid = item["batch_id"]
                batches.setdefault(bid, []).append(item)

            dates   = oc.get_system_dates()
            weights = oc.load_weights()

            for batch_id, items in batches.items():
                # ── Check batch processing status ─────────────────────
                try:
                    status_resp = requests.get(
                        f"https://api.anthropic.com/v1/messages/batches/{batch_id}",
                        headers={
                            "x-api-key":         oc.ANTHROPIC_API_KEY,
                            "anthropic-version": "2023-06-01",
                        },
                        timeout=30,
                    )
                except Exception as e:
                    oc.log("Batch", f"Status check network error for {batch_id}: {e}")
                    continue

                if status_resp.status_code != 200:
                    oc.log("Batch",
                           f"Status check HTTP {status_resp.status_code} "
                           f"for {batch_id}")
                    continue

                processing_status = status_resp.json().get("processing_status", "")
                if processing_status != "ended":
                    oc.log("Batch",
                           f"{batch_id[:24]}… still {processing_status} "
                           f"({len(items)} item(s) waiting)")
                    continue

                # ── Fetch completed results (JSONL, one line per request) ─
                try:
                    results_resp = requests.get(
                        f"https://api.anthropic.com/v1/messages/batches/{batch_id}/results",
                        headers={
                            "x-api-key":         oc.ANTHROPIC_API_KEY,
                            "anthropic-version": "2023-06-01",
                        },
                        timeout=60,
                    )
                except Exception as e:
                    oc.log("Batch", f"Results fetch network error for {batch_id}: {e}")
                    continue

                if results_resp.status_code != 200:
                    oc.log("Batch",
                           f"Results HTTP {results_resp.status_code} "
                           f"for {batch_id}")
                    continue

                # Parse JSONL into a dict keyed by custom_id
                results_by_id: dict = {}
                for line in results_resp.text.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        results_by_id[row["custom_id"]] = row
                    except Exception as e:
                        oc.log("Batch", f"Result line parse error: {e}")

                # ── Finalize each completed item ──────────────────────
                for item in items:
                    custom_id = item.get("batch_custom_id")
                    row       = results_by_id.get(custom_id)

                    if not row:
                        oc.log("Batch",
                               f"No result row for custom_id {custom_id} "
                               f"in batch {batch_id}")
                        continue

                    result_block = row.get("result", {})
                    if result_block.get("type") != "succeeded":
                        err = result_block.get("error", {})
                        _set(item["id"],
                             status="FAILED",
                             error=(f"Batch API error: "
                                    f"{err.get('type','unknown')} — "
                                    f"{err.get('message','no message')[:200]}"),
                             finished_at=dt.datetime.now(oc.CT).strftime(
                                 "%I:%M %p").lstrip("0"))
                        oc.log("Batch",
                               f"#{item['id']} failed in Anthropic batch: "
                               f"{err.get('type')} {err.get('message','')[:80]}")
                        continue

                    # Extract text + stop_reason from the batch message
                    msg         = result_block.get("message", {})
                    content_blk = (msg.get("content") or [{}])[0]
                    eval_text   = content_blk.get("text", "")
                    stop_reason = msg.get("stop_reason", "end_turn")

                    if not eval_text:
                        _set(item["id"],
                             status="FAILED",
                             error="Batch result contained empty text",
                             finished_at=dt.datetime.now(oc.CT).strftime(
                                 "%I:%M %p").lstrip("0"))
                        continue

                    # Restore prop state from the snapshot saved at submit time
                    prop = item.get("_batch_prop")
                    if not prop:
                        _set(item["id"],
                             status="FAILED",
                             error="prop state not found — cannot finalize "
                                   "(queue entry predates v1.1 snapshot)")
                        continue

                    # ── Run compliance → scoring → posting ─────────────
                    _set(item["id"],
                         status="RUNNING",
                         started_at=dt.datetime.now(oc.CT).strftime(
                             "%I:%M %p").lstrip("0"))
                    try:
                        if item.get("_batch_ml"):
                            oc.finalize_batch_ml_evaluation(
                                prop, eval_text, stop_reason, dates)
                        else:
                            oc.finalize_batch_evaluation(
                                prop, eval_text, stop_reason, dates, weights)
                        _set(item["id"],
                             status="DONE",
                             finished_at=dt.datetime.now(oc.CT).strftime(
                                 "%I:%M %p").lstrip("0"))
                        oc.log("Batch",
                               f"#{item['id']} ({item['label']}) finalized "
                               f"from batch {batch_id[:24]}…")
                    except Exception as e:
                        oc.log("Batch",
                               f"finalize failed for #{item['id']}: {e}")
                        _set(item["id"],
                             status="FAILED",
                             error=str(e)[:300],
                             finished_at=dt.datetime.now(oc.CT).strftime(
                                 "%I:%M %p").lstrip("0"))

        except Exception:
            oc.log("Batch",
                   "poller loop error: " + traceback.format_exc()[:300])
            time.sleep(60)


# ================================================================
# WORKER
# ================================================================

def _worker():
    while True:
        try:
            q   = _load()
            nxt = next((i for i in q if i["status"] == "QUEUED"), None)
            if not nxt:
                time.sleep(2); continue

            _set(nxt["id"], status="RUNNING",
                 started_at=dt.datetime.now(oc.CT).strftime("%I:%M %p").lstrip("0"))
            try:
                if nxt.get("batch_eligible"):
                    # Batch path: _process_batch_submit sets status to
                    # BATCH_PENDING on success; raises on failure.
                    # DO NOT set DONE here — the poller owns final status.
                    _process_batch_submit(nxt)
                else:
                    # Live path: synchronous evaluation, set DONE on success.
                    _process(nxt)
                    _set(nxt["id"], status="DONE",
                         finished_at=dt.datetime.now(oc.CT).strftime(
                             "%I:%M %p").lstrip("0"))
            except Exception as e:
                oc.log("Intake", f"eval failed for #{nxt['id']}: {e}")
                _set(nxt["id"], status="FAILED", error=str(e)[:300])
        except Exception:
            oc.log("Intake",
                   "worker loop error: " + traceback.format_exc()[:300])
            time.sleep(3)


_started = False
def start_worker():
    """Idempotent — one worker + one batch poller per process."""
    global _started
    if _started: return
    _started = True
    threading.Thread(target=_worker,
                     daemon=True, name="intake-worker").start()
    threading.Thread(target=_batch_poller,
                     daemon=True, name="batch-poller").start()
    oc.log("Intake",
           "Mission Control intake worker + batch poller started")

def retry(item_id):
    """Re-queue a FAILED item (Priority 1: retry button). Text items
    re-run from their stored payload; image items whose bytes weren't
    persisted return a clear reason to re-upload."""
    q = _load()
    for i in q:
        if i["id"] == item_id and i["status"] == "FAILED":
            if i["kind"] in ("image", "ml_image") and not i.get("payload", {}).get("b64"):
                _set(item_id, error="screenshot bytes not retained — please re-upload the board")
                return {"ok": False, "error": "re-upload the screenshot"}
            _set(item_id, status="QUEUED", error=None, finished_at=None)
            return {"ok": True}
    return {"ok": False, "error": "not found or not in FAILED state"}


# ================================================================
# SLIP REVIEW QUEUE (this session) — read/confirm/reject a slip
# screenshot's extracted legs, stake, and payout before anything is
# logged. Deliberately a SEPARATE file and SEPARATE flow from the
# QUEUE above: that queue auto-processes (submit -> worker runs it,
# no human in the loop) because a prop evaluation is safe to run
# speculatively. A slip confirmation is different in kind -- it's a
# claim about real money already staked, read by a vision model that
# can misread a dollar figure, so nothing gets written to slip_log.json
# until the operator has seen and (if needed) corrected what was read.
# No worker thread for this queue -- there is nothing to auto-run;
# every item just sits at PENDING_REVIEW until confirmed or rejected.
# ================================================================

SLIP_REVIEW_QUEUE = ops.OPS_DIR / "slip_review_queue.json"
_slip_review_lock = threading.Lock()

# Best-effort display-label -> internal stat key, so a leg's stat
# dropdown pre-fills with a real guess instead of forcing the operator
# to pick from scratch every time. Kept here (not just in openclaw_core,
# where the canonical copy also lives for the docstring reference)
# would risk drifting out of sync -- import it directly instead.
_SLIP_STAT_LABEL_MAP = oc.SLIP_STAT_LABEL_MAP


def _load_slip_review():
    try: return json.loads(SLIP_REVIEW_QUEUE.read_text())
    except Exception: return []


def _save_slip_review(q):
    with _slip_review_lock:
        SLIP_REVIEW_QUEUE.write_text(json.dumps(q, indent=2, default=str))


def _guess_stat_key(stat_label):
    if not stat_label:
        return None
    return _SLIP_STAT_LABEL_MAP.get(stat_label.strip().lower())


def enqueue_slip_review(extracted, source="discord_slips_channel"):
    """
    extracted: the dict oc.extract_slip_screenshot() returned --
    {"legs": [...], "stake": ..., "potential_payout": ...}. Stores it
    as a reviewable item, pre-filling each leg's internal stat key via
    best-effort label matching -- the operator confirms or corrects
    every field in Mission Control, nothing here is final yet.
    """
    q = _load_slip_review()
    legs = []
    for leg in (extracted or {}).get("legs", []) or []:
        legs.append({
            "player":    leg.get("player", ""),
            "bet_type":  leg.get("bet_type", "prop"),
            "direction": (leg.get("direction") or "MORE").upper(),
            "line":      leg.get("line"),
            "stat":      _guess_stat_key(leg.get("stat_label")),
            "stat_label_read": leg.get("stat_label"),  # kept for display only
            "prop_type": leg.get("prop_type", "standard"),
            # APEX-DISCOUNT-2026-10-16
            "sport":     (leg.get("sport") or "TENNIS").upper(),
            "discount":  bool(leg.get("discount")),
            "original_line": leg.get("original_line"),
        })
    item = {
        "id": (max([i["id"] for i in q], default=0) + 1),
        "status": "PENDING_REVIEW",
        "legs": legs,
        "stake": (extracted or {}).get("stake"),
        "potential_payout": (extracted or {}).get("potential_payout"),
        "source": source,
        "submitted_at": dt.datetime.now(oc.CT).strftime("%I:%M %p").lstrip("0"),
    }
    q.append(item)
    _save_slip_review(q)
    return item["id"]


def slip_review_queue_view():
    """No image bytes ever stored here (only already-extracted fields),
    so the whole queue is safe to serialize directly -- unlike
    queue_view() above, nothing needs stripping."""
    return [i for i in _load_slip_review() if i["status"] == "PENDING_REVIEW"]


def _leg_to_raw_text(leg):
    """Builds one leg's piece of the pipe-delimited string
    oc.parse_slip_entry() already knows how to parse -- reusing that
    function's proven regex/correlation-gate logic instead of writing
    a second path that logs slips directly."""
    player = (leg.get("player") or "").strip()
    direction = (leg.get("direction") or "MORE").upper()
    if leg.get("bet_type") == "moneyline":
        return f"{player} ML {direction}"
    stat = leg.get("stat") or ""
    if not stat and leg_is_manual(leg):   # APEX-DISCOUNT-2026-10-16: graded by hand
        stat = "@%s:%s" % (re.sub(r"[^A-Z0-9]", "", (leg.get("sport") or "OTHER").upper()) or "OTHER",
                           re.sub(r"\s+", "_", (leg.get("stat_label_read") or leg.get("stat_label") or "").strip().lower()))
    line = leg.get("line")
    tag = ""
    if leg.get("prop_type") == "goblin":
        tag = " 😈"
    elif leg.get("prop_type") in ("red_demon", "demon"):
        tag = " 🔴😈"
    if leg.get("discount"):
        tag += " \U0001F3F7\uFE0F"
    return f"{player} {direction} {line} {stat}{tag}"


def leg_is_manual(leg):
    """APEX-DISCOUNT-2026-10-16: a prop leg the slip log has no stats feed for
    (another sport, or a tennis stat it doesn't track) -- it is logged anyway and
    the operator grades it by hand in #slips-pnl."""
    return (leg.get("bet_type", "prop") != "moneyline" and not leg.get("stat")
            and bool((leg.get("stat_label_read") or leg.get("stat_label") or "").strip())
            and leg.get("line") is not None)


def _leg_extras(leg):
    """APEX-DISCOUNT-2026-10-16: what parse_slip_entry() stores beyond the leg text."""
    return {"discount": bool(leg.get("discount")) or None, "original_line": leg.get("original_line"),
            "sport": (leg.get("sport") or "").upper() or None,
            "stat_label": (leg.get("stat_label_read") or leg.get("stat_label")) if leg_is_manual(leg) else None}


def confirm_slip_review(item_id, legs=None, stake=None, potential_payout=None):
    """
    Final step: the operator has seen the extracted (or corrected)
    legs/stake/payout in Mission Control and confirmed them. legs, if
    provided, REPLACES the stored legs entirely (the operator's edited
    version); otherwise the originally-extracted legs are used as-is.
    Builds the raw_text string and calls the real, already-tested
    oc.parse_slip_entry() -- same correlation gate, same logging, same
    STAT_ABBREV validation every typed-in slip already goes through.
    Removes the item from the review queue on success either way (a
    leg that fails parse_slip_entry's own validation shows up in ITS
    response as an error line, same as if it had been typed by hand --
    this function doesn't re-validate, it hands off to the function
    that already does).
    """
    q = _load_slip_review()
    item = next((i for i in q if i["id"] == item_id and i["status"] == "PENDING_REVIEW"), None)
    if not item:
        return {"ok": False, "error": "not found or already reviewed"}

    final_legs = legs if legs is not None else item["legs"]
    final_stake = stake if stake is not None else item.get("stake")
    final_payout = potential_payout if potential_payout is not None else item.get("potential_payout")

    _kept = [l for l in final_legs if (l.get("player") or "").strip()]
    raw_text = " | ".join(_leg_to_raw_text(l) for l in _kept)
    if not raw_text:
        return {"ok": False, "error": "no legs to confirm — add at least one before confirming"}

    dates = oc.get_system_dates()
    response = oc.parse_slip_entry(raw_text, dates, stake=final_stake,
                                    potential_payout=final_payout,
                                    leg_extras=[_leg_extras(l) for l in _kept])

    item["status"] = "CONFIRMED"
    item["slip_id"] = getattr(oc, "LAST_SLIP_ID", None)   # APEX-DISCOUNT-2026-10-16: "#7 reese hit"
    _save_slip_review(q)
    return {"ok": True, "response": response}


def reject_slip_review(item_id):
    """Discard a misread screenshot without logging anything --
    distinct from confirm_slip_review's REJECTED_CORRELATION (that's a
    leg the SYSTEM declined; this is the OPERATOR declining the whole
    read, e.g. wrong screenshot entirely)."""
    q = _load_slip_review()
    item = next((i for i in q if i["id"] == item_id and i["status"] == "PENDING_REVIEW"), None)
    if not item:
        return {"ok": False, "error": "not found or already reviewed"}
    item["status"] = "DISCARDED"
    _save_slip_review(q)
    return {"ok": True}


# ================================================================
# SLIP AUTO-VALIDATION (SLIP-AUTOMATION-2026-09-27, priority 4 of 4 --
# built only after priorities 1-3: duplicate-leg gate, manual/auto
# bankroll-ledger overlap check, Mission Control auth re-enable).
# Cross-checks a screenshot's extracted legs against
# apex_engine_picks_log_v1.csv -- the real record of what Apex itself
# evaluated and logged. If every leg matches, the slip is confirmed
# automatically through the exact same oc.parse_slip_entry() path the
# operator's manual confirm button already uses. If any leg doesn't
# match, NOTHING is auto-confirmed and the caller falls through to
# enqueue_slip_review() unchanged -- this is additive, not a
# replacement for the review queue.
# ================================================================

_PICKS_LOG_PATH = Path(__file__).resolve().parent / "apex_engine_picks_log_v1.csv"

# slip stat_type (oc.STAT_ABBREV values, stored on every logged leg) ->
# apex_engine_picks_log_v1.csv "category" column. Not 1:1 by name --
# double_faults/break_points_won read differently in the two places.
_STAT_TYPE_TO_PICKS_CATEGORY = {
    # BUGFIX-2026-09-27 (found during operational validation): the previous
    # version of this map used long-form keys ("double_faults",
    # "break_points_won", ...) that _guess_stat_key() / SLIP_STAT_LABEL_MAP
    # (openclaw_core.py) never actually produce -- that map returns the
    # short internal codes "tg"/"sets"/"aces"/"df"/"bp"/"gw". Only "aces"
    # happened to match by coincidence, so every other stat type silently
    # fell through to manual review instead of ever auto-confirming.
    # Keys below are exactly what _guess_stat_key() returns.
    "tg": "total_games", "sets": "total_sets", "aces": "aces",
    "df": "dfs", "bp": "breaks", "gw": "games_won",
    "fs": "fantasy_score",   # APEX-SLIPFS-2026-10-11
    # No short code exists yet for fantasy_score or sets_won specifically
    # (SLIP_STAT_LABEL_MAP has no entry for either) -- left unmapped on
    # purpose so those legs keep failing safe to manual review rather than
    # guessing a correspondence; not something to invent here.
}


def _load_picks_log_rows():
    if not _PICKS_LOG_PATH.exists():
        return []
    try:
        with open(_PICKS_LOG_PATH, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _days_between(date_a, date_b):
    try:
        a = dt.date.fromisoformat(date_a); b = dt.date.fromisoformat(date_b)
        return abs((a - b).days)
    except Exception:
        return 999


def _leg_matches_evaluated_pick(leg, rows, today_s):
    """
    True if this extracted leg (player/stat/line) matches a real row
    Apex itself evaluated and logged -- within a 3-day window (a
    screenshot is normally confirmed same-day or the next morning;
    never further back). player: case-insensitive substring match
    either direction (tolerates 'R. Sakamoto' vs 'Rei Sakamoto').
    category: derived from the leg's stat. line: EXACT match, no
    tolerance -- a misread line is exactly the failure mode this
    guards against, so no fuzziness is allowed here. Moneyline legs
    never match (the props log has no moneyline rows) -- always fails
    safe to manual review, by design.
    """
    if leg.get("bet_type") == "moneyline":
        return False
    category = _STAT_TYPE_TO_PICKS_CATEGORY.get(leg.get("stat") or "")
    if not category:
        return False
    line = leg.get("line")
    if line is None:
        return False
    try:
        line = float(line)
    except (TypeError, ValueError):
        return False
    player = (leg.get("player") or "").strip().lower()
    if not player:
        return False
    for r in rows:
        if (r.get("category") or "") != category:
            continue
        try:
            r_line = float(r.get("line") or "nan")
        except ValueError:
            continue
        if r_line != line:
            continue
        r_player = (r.get("player") or "").strip().lower()
        if r_player == "match":
            # APEX-SLIPMATCH-2026-10-11: match-level props (Total Games, Total Sets)
            # are logged as player "match", opponent "A v B" -- match either name.
            # Before this, every Total Games leg failed and went to manual review.
            names = [x.strip().lower() for x in (r.get("opponent") or "").split(" v ")]
            if not any(n and (player in n or n in player) for n in names):
                continue
        elif not r_player or (player not in r_player and r_player not in player):
            continue
        r_date = (r.get("match_date") or "")
        if r_date and today_s and _days_between(r_date, today_s) > 3:
            continue
        return True
    return False


def try_auto_confirm_slip(extracted, source="discord_slips_channel"):
    """
    SLIP-AUTOMATION-2026-09-27, priority 4 of 4: the screenshot
    auto-validation layer. Call this BEFORE enqueue_slip_review() --
    if it returns a result, the slip is already confirmed and logged;
    if it returns None, fall through to enqueue_slip_review() exactly
    as before this change.

    Fails safe in every direction: a missing/unreadable line, an
    unmapped stat, a player name that can't be matched, an unreadable
    picks log, a missing stake/payout, or a moneyline leg all route to
    manual review rather than guessing. Every leg must match or the
    WHOLE slip goes to review -- there is no partial auto-confirm.

    On a real auto-confirm, routes through the exact same
    oc.parse_slip_entry() call confirm_slip_review() uses -- same
    correlation gate, same duplicate-leg gate (SLIP-AUTOMATION
    priority 1), same STAT_ABBREV validation every typed-in slip
    already goes through. Nothing about downstream settlement
    (parse_slip_result) or ROI (ops.slip_roi_report) changes -- this
    only shortcuts the confirm step for a slip that's already provably
    correct.
    """
    try:
        legs = (extracted or {}).get("legs") or []
        if not legs:
            return None
        if extracted.get("stake") is None or extracted.get("potential_payout") is None:
            return None   # an unreadable dollar figure is exactly what the
                           # manual gate exists for -- never auto-confirm blind
        rows = _load_picks_log_rows()
        if not rows:
            return None
        today_s = dt.date.today().isoformat()
        prepped = []
        for leg in legs:
            prepped_leg = {
                "player": leg.get("player", ""), "bet_type": leg.get("bet_type", "prop"),
                "direction": (leg.get("direction") or "MORE").upper(),
                "line": leg.get("line"), "stat": _guess_stat_key(leg.get("stat_label")),
                "prop_type": leg.get("prop_type", "standard"),
                "discount": bool(leg.get("discount")), "original_line": leg.get("original_line"),
                "sport": (leg.get("sport") or "TENNIS").upper(),
            }
            if not _leg_matches_evaluated_pick(prepped_leg, rows, today_s):
                return None   # any single mismatch sends the WHOLE slip to review
            prepped.append(prepped_leg)

        prepped = [l for l in prepped if (l.get("player") or "").strip()]
        raw_text = " | ".join(_leg_to_raw_text(l) for l in prepped)
        if not raw_text:
            return None
        dates = oc.get_system_dates()
        response = oc.parse_slip_entry(raw_text, dates, stake=extracted.get("stake"),
                                        potential_payout=extracted.get("potential_payout"),
                                        leg_extras=[_leg_extras(l) for l in prepped])
        try:
            oc.log("SlipAutoConfirm",
                   f"auto-confirmed {len(prepped)}-leg slip (source={source}) -- "
                   f"every leg matched an evaluated pick in {_PICKS_LOG_PATH.name}")
        except Exception:
            pass
        return {"ok": True, "auto": True, "response": response}
    except Exception as e:
        try:
            oc.log("SlipAutoConfirm", f"auto-validate error, falling back to manual review: {e}")
        except Exception:
            pass
        return None
