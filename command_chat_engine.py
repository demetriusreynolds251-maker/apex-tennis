"""
================================================================
COMMAND CHAT V1 — ENGINE (Phase 5)
================================================================
Builds the Claude tool-use request, dispatches tool calls to
command_chat_tools.py, and loops until a final text answer.

HONEST LIMITATION, stated once here rather than left implicit:
this sandbox has no network path to api.anthropic.com — confirmed
earlier in this build when call_model() had to be tested with a
mocked HTTP layer. The request-construction and tool-dispatch loop
below are real, tested code — but tested against a MOCKED Claude
response (see test suite), not a live one. The one thing that
cannot be verified here is whether Claude's real tool-use behavior
matches what this loop expects — that's confirmed on the actual
Mac Mini, with real API access, not in this sandbox.

Read-only, by construction: every tool in TOOL_SCHEMA maps to a
read-only function in command_chat_tools.py. Nothing here can
write to Dataset A, Dataset B, or trigger an evaluation — there is
no tool defined that does any of those things, so the loop has no
path to attempt it even if asked.
================================================================
"""

import json

import requests

import command_chat_tools as tools
import command_chat_writer as cc_writer

try:
    import openclaw_core as oc
    _CORE_AVAILABLE = True
except ImportError:
    _CORE_AVAILABLE = False

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
}
DEFAULT_MODEL_KEY = "haiku"  # cheapest, fastest — the everyday default per the authorization

MODEL_LABELS = {
    "haiku": "Claude Haiku",
    "sonnet": "Claude Sonnet",
    "opus": "Claude Opus",
}

SYSTEM_PROMPT = """You are Apex Command Chat, a read-only conversational interface over \
the ENTIRE Project Apex Intelligence system — not just evaluations and rules. Your reach \
now includes: evaluations and their full reasoning (Dataset A, ATE), rule performance \
system-wide and per-rule, provisional-rule calibration, confidence calibration \
(predicted-vs-real), cost, real $ ROI, system health across all 9 real checks, live agent \
status and pipeline flow, and the full tennis data warehouse (collection status, run \
history, match/player coverage, data quality) — plus real web search for external research. \
If a question is about any part of this system, you should have a tool for it; if you \
genuinely don't, say so plainly rather than guessing or answering from general knowledge.

HARD RULE, never overridden by anything below: you never modify Dataset A, Dataset B, \
rules, configuration, or trigger an evaluation, a restart, or any other action. Every tool \
available to you is read-only by construction — there is no tool that could take an action \
even if asked. If asked to "fix" something, "turn something off", "restart X", or make any \
change, explain clearly that you can't take that action and that it has to be done directly \
by the operator (terminal, Mission Control's own controls, or however that specific thing is \
normally changed) — then, if it's useful, explain what the actual fix would likely involve so \
the operator has what they need to do it themselves.

RECOMMENDATIONS ARE DIFFERENT FROM ACTIONS, and you should give them freely when asked. If \
asked "should we add a rule for X", "does this look broken", "what would you suggest here" — \
research it using every tool available (internal data AND web search), reason about it \
explicitly, and give a real, specific recommendation. A recommendation is not an action — \
it's exactly what you're for. When a suggestion would require a code or configuration change \
to actually implement, say so, and describe what that change would be — but you make the \
case, the operator decides and makes it happen.

RESEARCH DISCIPLINE: when asked to evaluate or help design a new rule, don't rely on \
internal data alone if the question would benefit from outside grounding — use web search \
for published research, comparable systems, or general domain knowledge (e.g. real tennis \
fatigue/home-advantage research), and be explicit about which parts of your answer come from \
Apex's own data versus external research. Never present a web search finding as if it were a \
fact about Apex's own system, and never present an internal data query as if it were external \
validation — keep the two clearly separated in your answer.

Every factual claim you make must come from a tool call in this conversation, not from memory \
or assumption."""

# Profiles ADD behavior on top of SYSTEM_PROMPT — they never replace it,
# so the read-only hard rule and the "cite your tools" rule apply no
# matter which profile is active. "default" adds nothing.
PROFILE_PROMPTS = {
    "default": "",
    "fable": (
        "\n\nDeep review mode: challenge assumptions rather than accepting them at face "
        "value, actively search for hidden risks and edge cases, verify conclusions "
        "against the actual tool data before stating them, and highlight architecture "
        "concerns even when not directly asked. Best suited for reviews, audits, and "
        "design validation."
    ),
    "architect": (
        "\n\nSystem design mode: think in dependencies, workflows, and infrastructure. "
        "When discussing Apex's own architecture, focus on build order and "
        "maintainability over surface-level description. Best suited for roadmap and "
        "platform-planning questions."
    ),
    "investigator": (
        "\n\nRoot-cause mode: follow evidence chains explicitly and cite the specific "
        "tool result that supports each claim. Cross-reference multiple sources before "
        "concluding. Prioritize findings and conclusions over general explanation. Best "
        "suited for bug hunts, rule reviews, and evaluation analysis."
    ),
}
DEFAULT_PROFILE_KEY = "default"

PROFILE_LABELS = {
    "default": "Default",
    "fable": "Fable",
    "architect": "Architect",
    "investigator": "Investigator",
}

# Matches the Anthropic tool-use schema shape directly — each entry
# here is what would go in the `tools=[...]` parameter of a real
# messages.create() call with tool use enabled.
TOOL_SCHEMA = [
    {
        "name": "get_evaluation",
        "description": "Full Dataset A detail for one evaluation by eval_id: confidence ledger, final grade, Rule 24/30 status, compliance flags, reasoning text.",
        "input_schema": {"type": "object", "properties": {
            "eval_id": {"type": "string"}}, "required": ["eval_id"]},
    },
    {
        "name": "get_trace_bundle",
        "description": "Everything ATE captured for one evaluation: AI traces (prompts/responses/cost), rule activations, checkpoint stamps.",
        "input_schema": {"type": "object", "properties": {
            "eval_id": {"type": "string"}}, "required": ["eval_id"]},
    },
    {
        "name": "get_cost_summary",
        "description": "Today / 7-day / 30-day / lifetime Claude API cost across all of Apex.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_rule_details",
        "description": "Every activation of a named rule (e.g. 'Rule 24') across all evaluations, with a status breakdown.",
        "input_schema": {"type": "object", "properties": {
            "rule_name": {"type": "string"}}, "required": ["rule_name"]},
    },
    {
        "name": "get_recent_activations",
        "description": "Recent rule activations, optionally filtered by rule name.",
        "input_schema": {"type": "object", "properties": {
            "rule_name": {"type": "string"}, "limit": {"type": "integer"}}},
    },
    {
        "name": "search_decisions",
        "description": "Full-text search over Apex's recorded architectural decisions (why something was built or chosen).",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "search_investigations",
        "description": "Full-text search over Apex's recorded technical investigations (bug hunts, root-cause findings).",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "search_rules",
        "description": "Keyword search over rule activation reasoning text. Searches activation HISTORY, not rule definitions.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "get_dataset_b_player",
        "description": "Player identity, ranking, and year-level stats from the tennis data warehouse (Dataset B).",
        "input_schema": {"type": "object", "properties": {
            "player_name": {"type": "string"}}, "required": ["player_name"]},
    },
    {
        "name": "get_dependencies",
        "description": (
            "Direct graph relationships (Memory Center) for one node — use ONLY for "
            "relational questions like 'what depends on X' or 'what would be affected "
            "if X changed'. Do NOT use this for 'why was X investigated' or 'why does "
            "X exist' questions — those are single-entity lookups and belong to "
            "search_decisions/search_investigations instead, which are faster and more "
            "direct for that shape of question."
        ),
        "input_schema": {"type": "object", "properties": {
            "node_type": {"type": "string", "description": "'decision' | 'investigation' | 'rule' | 'system'"},
            "node_id": {"type": "string", "description": "the record's ID (decision/investigation) or name (rule/system, e.g. 'ATE')"},
        }, "required": ["node_type", "node_id"]},
    },
    # ── System-wide visibility tools (this session) ──────────────
    {
        "name": "get_system_health",
        "description": "Full Health Center detail: all 9 real system checks (Bot, Log, Archives, Channels, Validation, Dataset A, Warehouse, Agents, V8 Module), the composite health score, and the real 'needs attention' list. Use for any question about overall system health, what's broken, or why the health score is what it is.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_agent_status",
        "description": "Live status of all 8 pipeline agents — online/offline, current task, heartbeat age, success/failure counts. Use for 'is anything stuck' or 'what is the X agent doing right now'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_live_pipeline_flow",
        "description": "What's actively in flight right now, grouped by pipeline stage (research/evaluation/compliance/scoring/posting). Use for live 'what's happening right now' questions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_rule_command_summary",
        "description": "System-wide rule observability: total fires, success rate, per-rule hit rate and impact, recent failures, category breakdown. Use for questions about rule performance across the WHOLE system, not one specific rule (use get_rule_details for one named rule's history).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_calibration_status",
        "description": "Real calibration data for every provisional rule (RBMD, LPAB_PSAR, Gate 14 props/moneyline, Rule 9, CFEF, Rule30/CFEF stacking): fired count, settled count, real hit rate once past minimum sample. Use for 'is Rule X actually working' or 'which provisional rules have enough data to trust'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_ml_readiness",
        "description": "Real per-market row counts against real tier thresholds for future ML training readiness.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_confidence_calibration",
        "description": "Predicted-vs-real reliability report by confidence bucket: does a 70-75% pick actually hit ~70-75% of the time? Use for 'are we overconfident/underconfident' questions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_roi_summary",
        "description": "Real auto-captured dollar ROI from Slip Review (screenshot-sourced slips): total staked, profit, ROI%, win rate, and which slips were excluded and why. Use for 'what's our real ROI' questions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_warehouse_collection_status",
        "description": "Did the daily tennis data collection actually run? Last successful/failed run, next scheduled run, real weighted success rate, freshness. THE direct answer to 'did the warehouse update today' / 'did the API run today'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_warehouse_run_history",
        "description": "History of individual collector runs with real per-run row counts, errors, and warnings.",
        "input_schema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "how many recent runs, default 30"}}},
    },
    {
        "name": "get_match_coverage",
        "description": "Real match-level data coverage: total matches, how many have a confirmed winner, feature-complete count. THE direct answer to 'how many matches do we have'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_player_coverage",
        "description": "Real player-level data coverage for ATP/WTA players in the warehouse.",
        "input_schema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "max players to return, default 200"}}},
    },
    {
        "name": "get_warehouse_health",
        "description": "Warehouse Command Center's composite health: collection success, feature completeness, coverage, data quality, outcome completeness, shown individually.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_collection_trends",
        "description": "Real row-collection volume over time (today/week/month/30-day average). Use for 'is data collection keeping pace' questions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_data_source_quality",
        "description": "Breakdown of Dataset B rows by real data source (full aggregate vs. fallback-partial) with real percentages.",
        "input_schema": {"type": "object", "properties": {}},
    },
    # Server-side tool (executed by Anthropic's API itself, not
    # dispatched through _TOOL_DISPATCH below) -- for external
    # research the internal tools above can't answer: does published
    # research support a proposed rule, what do other systems do,
    # general tennis-analytics literature. run_turn()'s loop only
    # reacts to blocks with type=='tool_use'; this tool's calls come
    # back as type=='server_tool_use'/'web_search_tool_result', which
    # that filter already correctly leaves alone -- no loop changes
    # needed for this to work.
    {"type": "web_search_20250305", "name": "web_search"},
]

_TOOL_DISPATCH = {
    "get_evaluation": lambda inp: tools.get_evaluation(inp["eval_id"]),
    "get_trace_bundle": lambda inp: tools.get_trace_bundle(inp["eval_id"]),
    "get_cost_summary": lambda inp: tools.get_cost_summary(),
    "get_rule_details": lambda inp: tools.get_rule_details(inp["rule_name"]),
    "get_recent_activations": lambda inp: tools.get_recent_activations(
        rule_name=inp.get("rule_name"), limit=inp.get("limit", 20)),
    "search_decisions": lambda inp: tools.search_decisions(inp["query"]),
    "search_investigations": lambda inp: tools.search_investigations(inp["query"]),
    "search_rules": lambda inp: tools.search_rules(inp["query"]),
    "get_dataset_b_player": lambda inp: tools.get_dataset_b_player(inp["player_name"]),
    "get_dependencies": lambda inp: tools.get_dependencies(inp["node_type"], inp["node_id"]),
    "get_system_health": lambda inp: tools.get_system_health(),
    "get_agent_status": lambda inp: tools.get_agent_status(),
    "get_live_pipeline_flow": lambda inp: tools.get_live_pipeline_flow(),
    "get_rule_command_summary": lambda inp: tools.get_rule_command_summary(),
    "get_calibration_status": lambda inp: tools.get_calibration_status(),
    "get_ml_readiness": lambda inp: tools.get_ml_readiness(),
    "get_confidence_calibration": lambda inp: tools.get_confidence_calibration(),
    "get_roi_summary": lambda inp: tools.get_roi_summary(),
    "get_warehouse_collection_status": lambda inp: tools.get_warehouse_collection_status(),
    "get_warehouse_run_history": lambda inp: tools.get_warehouse_run_history(limit=inp.get("limit", 30)),
    "get_match_coverage": lambda inp: tools.get_match_coverage(),
    "get_player_coverage": lambda inp: tools.get_player_coverage(limit=inp.get("limit", 200)),
    "get_warehouse_health": lambda inp: tools.get_warehouse_health(),
    "get_collection_trends": lambda inp: tools.get_collection_trends(),
    "get_data_source_quality": lambda inp: tools.get_data_source_quality(),
}


def call_claude_with_tools(request):
    """
    The REAL Claude API call for a tool-use request — structurally
    parallel to openclaw_core.call_model(), but a separate function
    because tool-use requests have a different shape (tools=[...],
    multi-block content, tool_use/tool_result blocks) that
    call_model() was never built to handle, and retrofitting it
    would have touched all 13 of its existing call sites for a
    feature only Command Chat needs.

    NOT independently tested end-to-end in this build — this
    sandbox has no network path to api.anthropic.com (confirmed
    earlier when call_model() itself needed HTTP mocking to test).
    What IS tested: this function is called through the exact same
    run_turn() loop that was verified against a realistic mocked
    multi-turn conversation — see the test suite. The untested part
    is specifically "does the real Anthropic API respond the way
    the mock assumed," which can only be confirmed with real network
    access, on the Mac Mini.
    """
    if not _CORE_AVAILABLE or not (oc.anthropic_key() if hasattr(oc, "anthropic_key") else oc.ANTHROPIC_API_KEY):
        return {"content": [{"type": "text",
                "text": "Command Chat is not configured — no Anthropic API key available."}],
                "stop_reason": "end_turn"}
    headers = {
        "x-api-key": (oc.anthropic_key() if hasattr(oc, "anthropic_key") else oc.ANTHROPIC_API_KEY),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    resp = requests.post("https://api.anthropic.com/v1/messages",
                          headers=headers, json=request, timeout=120)
    resp.raise_for_status()
    return resp.json()


def dispatch_tool_call(tool_name, tool_input):
    """
    Executes exactly one tool call. Read-only by construction — see
    module docstring. Never raises out to the caller: a tool failure
    becomes a tool_result the model can see and react to, not a
    crashed conversation.
    """
    if tool_name not in _TOOL_DISPATCH:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        result = _TOOL_DISPATCH[tool_name](tool_input)
        return result
    except Exception as e:
        return {"error": f"Tool '{tool_name}' failed: {e}"}


def build_request(conversation_messages, model_key=None, profile_key=None):
    """
    Builds the request shape a real client.messages.create() call
    would send. Prompt caching applied automatically, per the
    NEW_CHAT_REQUIREMENT / prompt-caching authorization — never a
    user-facing setting, same pattern already proven in
    openclaw_core.py's call_model(enable_cache=True).

    model_key / profile_key: keys into MODELS / PROFILE_PROMPTS
    (e.g. "haiku", "investigator"). Unknown or missing keys fall
    back to the defaults rather than raising — a bad value from the
    frontend should degrade gracefully, not break the conversation.

    BUG FIX (this session): this function previously referenced a
    bare `MODEL` name that no longer exists anywhere in this file —
    confirmed to raise NameError on every single call, meaning
    Command Chat could not have been sending a real request without
    this fix, model/profile selection or not.
    """
    resolved_model = MODELS.get(model_key, MODELS[DEFAULT_MODEL_KEY])
    resolved_profile_suffix = PROFILE_PROMPTS.get(profile_key, "")
    system_text = SYSTEM_PROMPT + resolved_profile_suffix
    return {
        "model": resolved_model,
        # Fix (this session): was a flat 2000 with zero headroom and no
        # truncation detection anywhere downstream — see the stop_reason
        # check in run_turn() below. 2000 is tight for an answer that
        # cites multiple tool results (get_evaluation + get_trace_bundle
        # in the same turn routinely runs long). Bumped to 4000, same
        # order of magnitude as this system's other conversational
        # (non-evaluation) calls — evaluations proper stay on their own,
        # much larger EVAL_MAX_TOKENS budget in openclaw_core.py.
        "max_tokens": 4000,
        "system": [
            {"type": "text", "text": system_text,
             "cache_control": {"type": "ephemeral"}},
        ],
        "tools": [
            *TOOL_SCHEMA[:-1],
            {**TOOL_SCHEMA[-1], "cache_control": {"type": "ephemeral"}},
        ],
        "messages": conversation_messages,
    }


def run_turn(session_id, user_message, claude_call_fn, history=None, max_tool_rounds=5,
             model_key=None, profile_key=None):
    """
    Runs one user turn to completion, including any tool-use rounds,
    and returns the final text answer plus which sources were cited.

    claude_call_fn: a function taking a request dict (from
    build_request) and returning a Claude response dict shaped like
    the real API — {"content": [...], "stop_reason": ...}. In
    production this wraps the real Anthropic client call. In this
    sandbox's test suite, it's a mock simulating real tool-use
    behavior — see the test file for exactly what's being simulated
    and what isn't verified.

    history: prior turns for this session, NOT re-fetched from
    command_chat_log automatically — per the New Chat design, a
    fresh session_id starts with empty history by default; continuity
    across sessions is a deliberate search of command_chat_log, not
    an ever-growing live context window.

    model_key / profile_key: resolved fresh on THIS call, not fixed
    at session start — this is what makes live mid-conversation
    switching work correctly: nothing about model/profile is stored
    on the session itself, so the very next message after a switch
    already uses the new selection, with no session reset needed.
    Every tool-use round within a single turn uses the same
    model/profile (switching mid-turn, between tool calls, isn't a
    real operator action — only between turns).
    """
    resolved_model_label = MODEL_LABELS.get(model_key, MODEL_LABELS[DEFAULT_MODEL_KEY])
    resolved_profile_label = PROFILE_LABELS.get(profile_key, PROFILE_LABELS[DEFAULT_PROFILE_KEY])

    messages = list(history or [])
    messages.append({"role": "user", "content": user_message})
    cc_writer.log_chat_message(session_id, "user", user_message)

    sources_cited = []
    for _ in range(max_tool_rounds):
        request = build_request(messages, model_key=model_key, profile_key=profile_key)
        response = claude_call_fn(request)
        content_blocks = response.get("content", [])

        # Fix (this session, confirmed real gap on direct code read): this
        # loop never checked stop_reason at all — a response cut off by
        # max_tokens, with no tool calls pending, used to flow straight
        # into "final answer" below and get returned to the user as if
        # complete, with zero indication anything was cut. Same principle
        # openclaw_core.py already applies to evaluations (see
        # evaluate_moneyline_screenshot's stop_reason check): a truncated
        # response is a structural failure for THIS turn, not a partial
        # success, and it must never be silent.
        #
        # Deliberately NOT a hard SYSTEM_FAILURE the way a truncated
        # evaluation is — that pattern fits a single evaluation with no
        # retry value, not an ongoing chat the operator can just re-ask
        # more narrowly. Instead: don't trust a truncated response's tool
        # calls (a cut-off tool_use block's JSON input could be malformed
        # mid-argument), log it, and tell the operator plainly so they
        # know to re-ask rather than trust a silently incomplete answer.
        if response.get("stop_reason") == "max_tokens":
            usage = response.get("usage", {})
            truncated_note = (
                "⚠️ This response was cut off (hit the output length limit) "
                "before finishing — nothing below is a complete or trustworthy "
                "answer. Try asking a narrower question, or ask me to continue."
            )
            log_line = (f"[CommandChat] TRUNCATED — session={session_id} "
                        f"output_tokens={usage.get('output_tokens', '?')} "
                        f"max_tokens={request.get('max_tokens')}")
            print(log_line)
            cc_writer.log_chat_message(session_id, "assistant", truncated_note,
                                        sources_cited=sources_cited)
            return {"answer": truncated_note, "truncated": True,
                    "sources_cited": sources_cited, "messages": messages,
                    "model_used": resolved_model_label,
                    "profile_used": resolved_profile_label}

        messages.append({"role": "assistant", "content": content_blocks})

        # Server-side web_search calls come back as type=='server_tool_use'
        # (input) paired with type=='web_search_tool_result' (the actual
        # results) -- Anthropic's API executes these itself, so they never
        # go through dispatch_tool_call below. Logged into sources_cited
        # anyway, purely for operator transparency into what was actually
        # searched -- this is display-only, it changes nothing about how
        # the loop behaves.
        for b in content_blocks:
            if b.get("type") == "server_tool_use" and b.get("name") == "web_search":
                sources_cited.append({"tool": "web_search", "input": b.get("input", {})})

        tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]
        if not tool_use_blocks:
            # Final answer — no more tool calls requested.
            final_text = "".join(
                b.get("text", "") for b in content_blocks if b.get("type") == "text")
            cc_writer.log_chat_message(session_id, "assistant", final_text,
                                        sources_cited=sources_cited)
            return {"answer": final_text, "sources_cited": sources_cited,
                    "messages": messages, "model_used": resolved_model_label,
                    "profile_used": resolved_profile_label}

        tool_results = []
        for block in tool_use_blocks:
            result = dispatch_tool_call(block["name"], block.get("input", {}))
            sources_cited.append({"tool": block["name"], "input": block.get("input", {})})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(result, default=str),
            })
        messages.append({"role": "user", "content": tool_results})

    # Exceeded max_tool_rounds without a final answer — fail visibly,
    # not silently, and still log what happened.
    cc_writer.log_chat_message(session_id, "assistant",
                                "(stopped after max tool-call rounds without a final answer)",
                                sources_cited=sources_cited)
    return {"answer": None, "error": "max_tool_rounds exceeded",
            "sources_cited": sources_cited, "messages": messages,
            "model_used": resolved_model_label, "profile_used": resolved_profile_label}
