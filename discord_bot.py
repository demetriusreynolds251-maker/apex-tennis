#!/usr/bin/env python3
"""
DEMETRIUS — Discord Gateway Bridge (v7.9.6, Step 9 + Image Intake)
===================================================================
Listens on four channels:

  #slips-pnl        — slip entries and results
  #learning-channel — APPROVE N / REJECT N weight proposal commands
  #general          — screenshot intake (PrizePicks props or sportsbook
                      moneyline images). Text messages in #general are
                      ignored — only image attachments are processed.
                      The bot reads the image, confirms tournament
                      context via the V8 API and web search, then
                      routes results to the correct channel:
                        props      → #picks-channel
                        moneyline  → #moneyline-channel
                        summaries  → #systems-channel
                        full text  → saved to picks archive on disk

Bot token is read from DISCORD_BOT_TOKEN environment variable only —
never hardcoded in this file.
"""
import os
import sys
import base64
import json
import re
import asyncio
import hashlib
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))

import discord
from discord.ext import tasks

import openclaw_core as core
import openclaw_intake as intake
from discord_message_classifier import (
    classify_slip_message,
    parse_approval_command,
    classify_image_content,
)

SLIPS_CHANNEL_NAME    = "slips-pnl"
LEARNING_CHANNEL_NAME = "learning-channel"
GENERAL_CHANNEL_NAME  = "general"

# v7.9.7 fix (item 12): same-screenshot deduplication guard. If the
# same image is sent to #general twice within the window below, the
# bot skips re-evaluating it instead of posting a duplicate pick,
# duplicate archive entry, and duplicate slip-log line. In-memory only
# — nothing written to disk, nothing that could corrupt long-term data.
# Short window is intentional: a genuinely new screenshot of the same
# match/line sent later (e.g. after checking a different book) should
# still be allowed through.
_RECENT_IMAGE_HASHES = {}  # md5 hash -> timestamp first seen
_DEDUP_WINDOW_SECONDS = 600  # 10 minutes

# v7.9.7 fix (item 3): task cancellation via typed "!cancel" in
# #general. IMPORTANT HONEST LIMITATION: Python's run_in_executor
# cannot forcibly kill a thread that's already running (e.g. stuck
# mid-API-call) — .cancel() on a Future only works before the task has
# actually started, which for these tasks is almost immediately. So
# this does NOT interrupt genuinely in-flight work like a hung API
# call. What it DOES do: marks the current task as abandoned, so once
# the underlying call finally does return (or times out on its own),
# the bot skips posting its result and tells you it was cancelled
# instead — useful for "I don't want to see this result anymore" even
# though the thread itself keeps running to completion in the
# background. Real interruption of a hung network call would require
# restarting the whole bot process (the existing launchctl commands),
# which stays the right tool for a truly stuck/hung process.
_current_task_label     = None   # human-readable description of the running task
_current_task_cancelled = False

# Supported image MIME types for attachment detection
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# APEX-SLASHCMDS-2026-09-29: real Discord application (slash) commands.
# Everything above this point (the on_message text-command handlers)
# stays exactly as-is -- typing the plain text still works. This adds
# the SEPARATE registration Discord needs to show a command in the "/"
# autocomplete menu, which nothing in this file has ever done before
# (confirmed by grep: zero discord.app_commands / CommandTree usage
# anywhere in the previous version of this file). /ping and /commands,
# which owner sees in the menu today, come from a different bot
# application in the same server -- neither string exists anywhere in
# this codebase.
tree = discord.app_commands.CommandTree(client)



@tasks.loop(minutes=15)
async def reaper_loop():
    """Recurring reap, every 15 minutes -- a third of the 45-minute
    threshold, so nothing sits stale for more than ~60 min worst-case
    (45 min threshold + up to 15 min until the next tick)."""
    import dataset_a_capture as d1
    reaped = await client.loop.run_in_executor(
        None,
        lambda: d1.reap_stale_evaluations(_logger=lambda ch, msg: print(f"[{ch}] {msg}"))
    )
    if reaped:
        print(f"[DiscordBot] Scheduled reap: {len(reaped)} stale evaluation(s) cleared.")


_SLIP_GRADE_LAST = {"ts": 0.0}


@tasks.loop(minutes=2)
async def slip_grader_loop():
    """APEX-SLIPGRADE-2026-10-11: grade logged slips from settled results and post
    one short line per finished slip to #slips-pnl.
    APEX-VOIDGRADE-2026-10-24: checks every 2 minutes and grades as soon as a result
    file changes (the hourly settle), else every 30 minutes as before."""
    try:
        import apex_slip_grader as _sg
        if not _sg.sources_changed() and time.time() - _SLIP_GRADE_LAST["ts"] < 1800:
            return
        _SLIP_GRADE_LAST["ts"] = time.time()
        cards = await client.loop.run_in_executor(None, lambda: _sg.run(cards=True))
        ch = next((c for c in client.get_all_channels() if getattr(c, "name", "") == SLIPS_CHANNEL_NAME), None)
        await _post_slip_grade_cards(ch, cards or [])
        # APEX-DISCOUNT-2026-10-16: once a day, picks still waiting for a hand grade
        rem = await client.loop.run_in_executor(None, _sg.manual_reminders)
        if rem and ch:
            await ch.send("\n".join(rem)[:1990])
    except Exception as e:
        print(f"[SlipGrader] failed: {type(e).__name__}: {e}")


# APEX-PNLCHANNEL-2026-10-23 (owner, "Go for everything"): #pnl gets the
# 💰 DAILY P&L card (apex_pnl_channel.py) at 9:05 AM CT, on /pnl, and again when a
# slip finishes in #slips-pnl. Off when apex_pnl_channel.LIVE is False.
PNL_CHANNEL_NAME = "pnl"


async def _ensure_pnl_channel():
    """Creates #pnl in #slips-pnl's category, just below it, if missing."""
    if not client.guilds:
        return None
    g = client.guilds[0]
    ch = next((c for c in g.text_channels if c.name == PNL_CHANNEL_NAME), None)
    if ch:
        return ch
    sp = next((c for c in g.text_channels if c.name == SLIPS_CHANNEL_NAME), None)
    try:
        ch = await g.create_text_channel(
            PNL_CHANNEL_NAME, category=sp.category if sp else None,
            topic="💰 Daily P&L from the real-money slip ledger -- every morning at 9:05 AM CT, /pnl anytime")
        if sp:
            await ch.edit(position=sp.position + 1)
        print(f"[PnL] created #{PNL_CHANNEL_NAME}")
    except Exception as e:
        print(f"[PnL] could not create #{PNL_CHANNEL_NAME}: {type(e).__name__}: {e}")
        return None
    return ch


async def _post_pnl_card(day=None, send=None, kind="daily"):
    """Posts the card to #pnl (or via `send`). Returns True when sent.
    kind: daily | weekly | monthly (APEX-PNLTAXES-2026-10-23 recaps)."""
    try:
        import apex_pnl_channel as _pc
        if send is None and not _pc.LIVE:
            return False
        fn = {"weekly": _pc.weekly_dict, "monthly": _pc.monthly_dict}.get(kind, _pc.card_dict)
        cd = await asyncio.to_thread(fn, day)
        em = discord.Embed.from_dict(cd)
        if send is not None:
            await send(embed=em)
            return True
        ch = await _ensure_pnl_channel()
        if not ch:
            return False
        await ch.send(embed=em)
        return True
    except Exception as e:
        print(f"[PnL] card failed: {type(e).__name__}: {e}")
        return False


@tasks.loop(minutes=1)
async def pnl_daily_loop():
    try:
        import apex_pnl_channel as _pc
        import apex_pnl_ledger as _lg
        # APEX-PNLTAXES-2026-10-23: tax ledger rebuilt whenever the slip log / ParlayPlay log changes
        await asyncio.to_thread(_lg.maybe_rebuild)
        if _pc.LIVE and await asyncio.to_thread(_pc.daily_due):
            await asyncio.to_thread(_lg.rebuild, True)   # and fresh every morning at 9:05
            if await _post_pnl_card():
                await asyncio.to_thread(_pc.mark_daily_posted)
        for job in await asyncio.to_thread(_pc.take_post_queue):   # one-shot posts queued from the shell
            await _post_pnl_card(job.get("day"), kind=job.get("kind", "daily"))
        if _pc.LIVE and await asyncio.to_thread(_pc.weekly_due):   # Mondays 9:10 AM CT, last week
            if await _post_pnl_card(_pc.last_week_day(), kind="weekly"):
                await asyncio.to_thread(_pc.mark_posted, "weekly_posted")
        if _pc.LIVE and await asyncio.to_thread(_pc.monthly_due):  # the 1st, 9:15 AM CT, last month
            if await _post_pnl_card(_pc.last_month_day(), kind="monthly"):
                await asyncio.to_thread(_pc.mark_posted, "monthly_posted")
    except Exception as e:
        print(f"[PnL] daily loop failed: {type(e).__name__}: {e}")


async def _post_slip_grade_cards(ch, cards):
    finals = sorted({cd.get("day") for cd in cards if cd.get("kind") == "final" and cd.get("day")})
    if finals:   # APEX-PNLCHANNEL-2026-10-23: a finished slip moves the money -- refresh #pnl
        asyncio.ensure_future(_post_pnl_card(finals[-1]))
    for cd in cards:   # APEX-SLIPCARDS-2026-10-13: one card per slip, colored by result
        if not ch:
            print("[SlipGrader] " + cd["text"])
            continue
        try:
            em = discord.Embed(title=cd["title"], description=cd["description"], color=cd["color"])
            body = "\n".join(cd["legs"])
            while body:
                cut = body[:1024].rfind("\n") if len(body) > 1024 else len(body)
                cut = cut if cut > 0 else min(len(body), 1024)
                em.add_field(name="Picks" if not em.fields else "\u200b", value=body[:cut], inline=False)
                body = body[cut:].lstrip("\n")
            em.set_footer(text=cd["footer"])
            await ch.send(embed=em)
        except Exception as e:
            print("[SlipGrader] embed failed (%s), sending text" % e)
            await ch.send(cd["text"][:1990])


# APEX-SLIPSCHANNEL-2026-10-15 (owner, "Go"): #slips, right before
# #slips-pnl -- Top 3 / Elite 6 / All Props slip recommendations
# (apex_slips_channel.py) when the published boards change, result cards
# when a recommended slip finishes, and /slips on demand.
SLIP_RECS_CHANNEL_NAME = "slips"


async def _ensure_slips_channel():
    """Creates #slips in #slips-pnl's category, just above it, if missing."""
    if not client.guilds:
        return None
    g = client.guilds[0]
    ch = next((c for c in g.text_channels if c.name == SLIP_RECS_CHANNEL_NAME), None)
    if ch:
        return ch
    pnl = next((c for c in g.text_channels if c.name == SLIPS_CHANNEL_NAME), None)
    try:
        ch = await g.create_text_channel(
            SLIP_RECS_CHANNEL_NAME, category=pnl.category if pnl else None,
            topic="PrizePicks slip recommendations: Top 3, Elite 6, Goblin, All Props -- with payout, all-hit chance and results")
        if pnl:
            await ch.edit(position=pnl.position)
        print(f"[Slips] created #{SLIP_RECS_CHANNEL_NAME}")
    except Exception as e:
        print(f"[Slips] could not create #{SLIP_RECS_CHANNEL_NAME}: {type(e).__name__}: {e}")
        return None
    return ch


def _slip_embeds(cards):
    import apex_slips_channel as _sc
    return [discord.Embed.from_dict(_sc.embed_dict(c)) for c in cards]


@tasks.loop(minutes=5)
async def slips_channel_loop():
    try:
        import apex_slips_channel as _sc
        header, recs, results = await client.loop.run_in_executor(None, _sc.auto)
        if not (recs or results):
            return
        ch = await _ensure_slips_channel()
        if not ch:
            return
        if recs:
            await ch.send(content=header, embeds=_slip_embeds(recs)[:10])
        done = []
        for sig, cd in results:
            await ch.send(embeds=_slip_embeds([cd]))
            done.append(sig)
        await client.loop.run_in_executor(None, lambda: _sc.mark_result_posted(done))
    except Exception as e:
        print(f"[Slips] loop failed: {type(e).__name__}: {e}")


async def _send_slips_on_demand(send):
    import apex_slips_channel as _sc
    try:
        cards = await asyncio.to_thread(_sc.on_demand)
    except Exception as e:
        await send(content=f"⚠️ Slips unavailable: {type(e).__name__}: {e}")
        return
    if not cards:
        await send(content="No slip right now: fewer than 2 board picks are still to play.")
        return
    await send(embeds=_slip_embeds(cards)[:10])


async def _send_slipsv2(send):
    """APEX-SLIPSV2-2026-10-22: /slipsv2 -- the Slips v2 summary + last 7 days, one line per day."""
    try:
        import apex_slips_v2 as _v2
        await asyncio.to_thread(_v2.grade)
        txt = await asyncio.to_thread(_v2.ondemand_text)
    except Exception as e:
        txt = f"⚠️ Slips v2 unavailable: {type(e).__name__}: {e}"
    await send(content=txt[:1990])


async def _send_counters(send):
    """APEX-BOARDCOUNTERS-2026-10-31: /counters -- whole-slip record per board since 10/31."""
    try:
        import apex_board_counters as _bc
        txt = await asyncio.to_thread(_bc.report_text)
    except Exception as e:
        txt = f"\u26A0\uFE0F Board counters failed: {type(e).__name__}: {e}"
    await send(content=txt[:1990])


async def _send_besttime(send):
    """APEX-BESTTIME-2026-10-31: /besttime -- when to play, real days and replay."""
    try:
        import apex_best_time as _bt
        txt = await asyncio.to_thread(_bt.report_text)
    except Exception as e:
        txt = f"\u26A0\uFE0F Best time tracker failed: {type(e).__name__}: {e}"
    await send(content=txt[:1990])


@client.event
async def on_ready():
    print(f"[DiscordBot] Logged in as {client.user} — listening on "
          f"#{SLIPS_CHANNEL_NAME}, #{LEARNING_CHANNEL_NAME}, #{GENERAL_CHANNEL_NAME}")
    await asyncio.to_thread(_fetch_marker_check_on_start)   # APEX-FETCHSTATUS-2026-10-23

    # APEX-P0-2026-09-28 Phase 2: one-time startup reap, so any evaluation
    # orphaned by whatever caused THIS restart is cleared immediately
    # rather than waiting for the first scheduled cycle below.
    import dataset_a_capture as d1
    reaped = d1.reap_stale_evaluations(_logger=lambda ch, msg: print(f"[{ch}] {msg}"))
    if reaped:
        print(f"[DiscordBot] Startup reap: {len(reaped)} stale evaluation(s) cleared.")

    reaper_loop.start()  # see scheduler hook below
    if not slip_grader_loop.is_running():   # APEX-SLIPGRADE-2026-10-11
        slip_grader_loop.start()
    await _ensure_slips_channel()            # APEX-SLIPSCHANNEL-2026-10-15
    if not slips_channel_loop.is_running():
        slips_channel_loop.start()
    if not fivepm_fetch_loop.is_running():   # APEX-5PMFETCH-2026-10-16
        fivepm_fetch_loop.start()
    await _ensure_pnl_channel()              # APEX-PNLCHANNEL-2026-10-23
    if not pnl_daily_loop.is_running():
        pnl_daily_loop.start()

    # APEX-SLASHCMDS-2026-09-29: guild-scoped sync -- shows up in the
    # "/" menu within seconds. A global sync (tree.sync() with no guild)
    # can take up to an hour to propagate to clients, which is useless
    # for same-session validation. This bot lives in exactly one server,
    # so guild-scoped is strictly better here, not a compromise.
    try:
        if client.guilds:
            guild = client.guilds[0]
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"[DiscordBot] Slash commands synced to guild "
                  f"'{guild.name}' ({guild.id}): {[c.name for c in synced]}")
        else:
            print("[DiscordBot] WARNING: bot is in zero guilds -- cannot sync slash commands.")
    except Exception as e:
        print(f"[DiscordBot] Slash command sync FAILED: {type(e).__name__}: {e}")


# ──────────────────────────────────────────────────────────────
# APEX-BB-002b: /bestbets command family (#general)
# ──────────────────────────────────────────────────────────────
_BB_COMMANDS = {
    "/bestbets":       (None, False),
    "/worstbets":      (None, True),
    "/bestdfs":        ("dfs", False),
    "/bestfantasy":    ("fantasy_score", False),
    "/bestaces":       ("aces", False),
    "/bestbreaks":     ("breaks", False),
    "/bestgameswon":   ("games_won", False),
    "/besttotalgames": ("total_games", False),
}

# APEX-TYPECMD-BOT-2026-09-09: prop-type views -- best 6 of one book type.
# APEX-FETCH-UX-2026-09-29: /bestgoblin /beststandard /bestdemon added as
# the exact names owner asks for -- same rendering as the existing
# /goblin /standard /demon aliases, nothing else changes for those.
_BB_TYPE_COMMANDS = {
    "/goblin":   ("goblin", 6),
    "/goblins":  ("goblin", 6),
    "/bestgoblin": ("goblin", 6),
    "/standard": ("standard", 6),
    "/standards": ("standard", 6),
    "/beststandard": ("standard", 6),
    "/demon":    ("demon", 6),
    "/demons":   ("demon", 6),
    "/bestdemon": ("demon", 6),
}

# APEX-FETCH-UX-2026-09-29: /top10 (top 10 across the whole board, no
# category/type filter) and /allpicks (the full current board, no
# limit). Both read-only -- load_current_board() reads the pick log
# the engine already writes on every run; neither triggers a new
# evaluation.
_BB_EXTRA_COMMANDS = {"/top10", "/elite6", "/top3", "/allprops"}   # /allpicks removed APEX-ALLPICKS-OFF-2026-10-13   # APEX-ELITE6-2026-09-29 / APEX-PERBOARD-2026-10-01

_PUBLISHED_BOARD_FLAGS = {
    "/elite6": "elite6",
    "/top3": "top3",
    "/top10": "top10",
    "/bestgoblin": "bestgoblin", "/goblin": "bestgoblin", "/goblins": "bestgoblin",
    "/bestdemon": "bestdemon", "/demon": "bestdemon", "/demons": "bestdemon",
    "/beststandard": "beststandard", "/standard": "beststandard", "/standards": "beststandard",
}


def _bb_render(cmd):
    """Runs in a worker thread: board load + ranking, no event-loop blocking."""
    import apex_edge_tracker as _aet
    # APEX-PUBLISHEDBOARD-2026-10-07 (owner, authorized): these six
    # commands (plus their plain-name aliases) now read the frozen board
    # captured at the last /fetch publish -- apex_edge_tracker.
    # published_board_detail() -- instead of rebuilding from a fresh
    # load_current_board() on every call. Published board must equal
    # command board; a live rebuild only happens through /fetch itself.
    # Checked BEFORE load_current_board() runs at all, so an empty/thin
    # live board can never block a still-valid published one from showing.
    if cmd in _PUBLISHED_BOARD_FLAGS:
        _pbd = _aet.published_board_detail(_PUBLISHED_BOARD_FLAGS[cmd])
        try:   # APEX-SETGAMES-FILL-2026-11-01: first-set picks shown under empty Top 3 / Elite spots
            import apex_set_games as _sg
            if isinstance(_pbd, str):
                _pbd += _sg.fill_text(_PUBLISHED_BOARD_FLAGS[cmd])
        except Exception:
            pass
        return _pbd
    picks, note = _aet.load_current_board()
    if note:
        return note
    if cmd == "/tieraudit":                            # APEX-TIERAUDIT-CMD-2026-09-28
        return _aet.prop_type_overlap_report(picks, limit=6)["text"]
    if cmd == "/top10":
        # APEX-FS-EXCLUSION-2026-09-30: /top10 is IMPLEMENTATION APPROVED for
        # exclusion; /allpicks below is deliberately left out -- it's the one
        # surface that must still show Fantasy Score (historical/audit view).
        # APEX-BOARDPARITY-2026-10-05 (owner, Board Consistency fix):
        # slip_preview=True turns on Held Back reporting for Top10 -- it does
        # NOT filter `scored`, so which 10 picks Top10 shows is unchanged.
        return _aet.best_bets_detail(picks, limit=10, exclude_fantasy_score=True, slip_preview=True)
    if cmd == "/allpicks":
        # APEX-ALLPICKS-REDESIGN-2026-10-05 (Phase 8): decision-making layout
        # (Evaluated Board summary -> Qualified -> Demon Eligible -> Not
        # Board Eligible -> Held Back -> Legacy Picks) instead of a raw,
        # unfiltered dump of best_bets_detail(limit=everything).
        return _aet.allpicks_detail(picks)
    if cmd == "/allprops":                           # APEX-QUALIFIED-2026-10-13
        return _aet.qualified_detail(picks)
    if cmd == "/elite6":                              # APEX-ELITE6-2026-09-29
        return _aet.elite6_detail(picks, exclude_fantasy_score=True)   # APEX-FS-EXCLUSION-2026-09-30
    if cmd == "/top3":                                # APEX-PERBOARD-2026-10-01
        return _aet.top3_detail(picks, exclude_fantasy_score=True)
    if cmd in _BB_TYPE_COMMANDS:                      # APEX-TYPECMD-BOT-2026-09-09
        # APEX-FS-EXCLUSION-2026-09-30: covers /bestdemon,/bestgoblin,/beststandard
        # AND their plain aliases /demon,/goblin,/standard -- all route through this
        # one dict/call, so excluding here closes every alias, not just the "best" name.
        ptype, lim = _BB_TYPE_COMMANDS[cmd]
        return _aet.best_bets_detail(picks, prop_type=ptype, limit=lim, slip_rules=True,
                                      exclude_fantasy_score=True)
    cat, worst = _BB_COMMANDS[cmd]
    return _aet.best_bets_detail(picks, category=cat, worst=worst)

def _bb_is_rule_line(line):
    s = line.strip()
    return len(s) > 0 and len(set(s)) == 1 and s[0] in ("\u2501", "\u2500")


def _bb_split_blocks(txt):
    """Split rendered card text into card-atomic blocks: a new block starts
    at each horizontal-rule line, so a card's header/body/signals never get
    torn apart by chunking."""
    lines = txt.split("\n")
    blocks = []
    cur = []
    for line in lines:
        if _bb_is_rule_line(line) and cur:
            blocks.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def _bb_chunk_text(txt, limit=1900):
    """Chunk text for Discord's message-length limit without ever splitting
    inside a card block. Falls back to a line-boundary cut only if a single
    block itself exceeds the limit."""
    blocks = _bb_split_blocks(txt)
    chunks = []
    cur = ""
    for b in blocks:
        btext = "\n".join(b)
        candidate = (cur + "\n" + btext) if cur else btext
        if len(candidate) > limit and cur:
            chunks.append(cur)
            cur = btext
        else:
            cur = candidate
        while len(cur) > limit:
            cut = cur.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(cur[:cut])
            cur = cur[cut:].lstrip("\n")
    if cur:
        chunks.append(cur)
    return chunks


async def _handle_bestbets_command(message, cmd):
    import asyncio
    try:
        txt = await asyncio.to_thread(_bb_render, cmd)
    except Exception as e:
        txt = f"best bets unavailable ({type(e).__name__}: {e})"
    for chunk in _bb_chunk_text(txt):
        await message.channel.send(chunk)


# ──────────────────────────────────────────────────────────────
# APEX-DISCORD-FETCH-2026-09-28 / APEX-FETCH-WORKFLOW-2026-09-29:
# /fetch + /board command family (#general)
#
# /fetch  is now the ACTIONABLE command: full scrape -> Gate 11 ->
#         odds -> engine evaluation -> eligibility -> publishes EVERY
#         qualifying (GREEN/YELLOW) evaluated pick to the Picks
#         Channel -- not just one "best" pick per player/match. Same
#         fetch_props_playwright()+pipeline the 5PM scheduled run
#         uses, just triggered on demand and with Rule 28's
#         one-pick-per-player/match slip-dedup turned OFF (that dedup
#         is a slip-construction rule for the scheduled run's actual
#         bettable slip -- it is not an eligibility rule, so it does
#         not belong in an on-demand "show me the whole board" command).
#   /fetch evaluate  -> alias for the same behavior as bare /fetch.
#   /fetch archive   -> scrape + save a raw board snapshot (all
#                       deduped props, pre-evaluation) to the picks
#                       archive directory. Unchanged.
#
# /board  is the NEW diagnostics-only command: scrape + dedupe only.
#         No Gate 11, no odds, no engine, no picks, no Discord posts
#         beyond its own summary. This is what bare /fetch used to do
#         before this change -- kept, just renamed and separated so a
#         quick board check never accidentally posts picks.
# ──────────────────────────────────────────────────────────────
_FETCH_MODES = {"/fetch": "publish_all", "/fetch evaluate": "publish_all", "/fetch archive": "archive"}


def _board_render():
    """Scrape-only diagnostics for /board: no Gate 11, no odds, no
    engine, no picks. Runs in a worker thread — never touches the
    Discord event loop directly."""
    dates = core.get_system_dates()

    # APEX-PARTNERAPI-2026-10-01 (API-First Fetch): was a direct
    # fetch_props_playwright() call. Now goes through the same
    # partner-API-first / Playwright-fallback path /fetch uses, so /board
    # diagnostics reflect the real data-acquisition layer. No user action
    # needed either way -- the caller never has to know which path ran.
    try:
        props, _data_source = core.fetch_props_with_fallback(dates["match_date"], dates)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if not props:
        # APEX-FETCHPIPE-2026-09-30: was a hardcoded guess ("Most likely
        # Chrome isn't running...") shown regardless of the real cause --
        # confirmed live on 2026-09-30 that guess was wrong (Chrome WAS
        # running) and its screenshot was 2 days stale. Now shows the
        # actual stage/exception/this-run's-own screenshot+HTML.
        return {"ok": False, "error": core.describe_last_fetch_failure()}

    # APEX-PARTNERAPI-2026-10-01: LAST_PLAYWRIGHT_SCRAPE_STATS only gets
    # (re)populated when fetch_props_playwright() actually ran -- on a
    # partner-API success it still holds whatever a PRIOR Playwright run
    # left behind, which would mislabel stale per-tab numbers as current.
    # Only trust that global when Playwright is the source this run.
    if _data_source == "playwright":
        stats = getattr(core, "LAST_PLAYWRIGHT_SCRAPE_STATS", {}) or {}
    else:
        stats = {"raw_total": len(props), "deduped_total": len(props), "per_tab": {}}
    demon    = sum(1 for p in props if p.get("is_demon"))
    goblin   = sum(1 for p in props if p.get("is_goblin"))
    standard = len(props) - demon - goblin

    return {
        "ok":             True,
        "raw_total":      stats.get("raw_total", len(props)),
        "deduped_total":  stats.get("deduped_total", len(props)),
        "per_tab":        stats.get("per_tab", {}),
        "demon":          demon,
        "goblin":         goblin,
        "standard":       standard,
        "data_source":    _data_source,
    }


# APEX-FETCHRELIABILITY-2026-10-01: USER EXPERIENCE -- a /fetch failure
# used to show the raw Python exception verbatim (e.g. a bare
# "BrokenPipeError [Errno 32]"), which tells a non-technical reader
# nothing actionable. This translates known transient failure types to
# plain English while still logging the real exception server-side (via
# core.log, which is itself now fail-open -- see openclaw_core.log()) so
# nothing is lost for debugging. Anything not recognized falls back to a
# short generic message rather than a raw traceback fragment.
def _friendly_error(e):
    etype = type(e).__name__
    core.log("Fetch", f"/fetch failed with {etype}: {e}")
    if isinstance(e, BrokenPipeError) or "BrokenPipe" in etype:
        return ("⚠️ A connection was unexpectedly closed mid-run (not a data problem). "
                "Please run `/fetch` again.")
    if isinstance(e, (TimeoutError,)) or "Timeout" in etype:
        return "⚠️ The fetch timed out. Please try again -- this is usually temporary."
    if isinstance(e, (ConnectionError, OSError)):
        return "⚠️ A network connection issue interrupted the fetch. Please try again."
    return f"⚠️ An unexpected issue interrupted the fetch ({etype}). Please try again."


def _user_performance_report():
    """APEX-FETCHRELIABILITY-2026-10-01: thin, fail-open wrapper around
    apex_learning_engine.build_user_performance_report() for the two
    /fetch handlers below. Lazy-imports (same pattern as the board-
    membership capture hook) so a problem importing the learning engine
    can never prevent the bot itself from starting or /fetch from
    completing."""
    try:
        import apex_learning_engine as _ale
        return _ale.build_user_performance_report()
    except Exception as e:
        core.log("Fetch", f"User Performance Report unavailable: {type(e).__name__}: {e}")
        return f"🧠 **USER PERFORMANCE REPORT**\n⚠️ Unavailable this run ({type(e).__name__}: {e})"


def _fetch_archive_render():
    """/fetch archive: scrape + dump a raw pre-evaluation snapshot.
    Unchanged from the previous /fetch archive behavior."""
    import time as _time
    import json as _json

    dates = core.get_system_dates()
    # APEX-PARTNERAPI-2026-10-01 (API-First Fetch): same hybrid path as
    # /fetch and /board -- partner-API first, Playwright fallback,
    # transparent to the caller.
    try:
        props, _data_source = core.fetch_props_with_fallback(dates["match_date"], dates)
    except Exception as e:
        return {"ok": False, "error": _friendly_error(e)}
    if core.fetch_cancel_requested():   # APEX-FETCHCANCEL-2026-10-12
        return {"ok": True, "cancelled": True, "stage": "scrape", "cancel_evaluated": 0, "cancel_total": len(props)}
    if not props:
        # APEX-FETCHPIPE-2026-09-30: was a hardcoded, stale-screenshot
        # pointer regardless of what actually failed -- now shows the
        # real stage + exception + this run's own screenshot/HTML.
        return {"ok": False, "error": core.describe_last_fetch_failure()}

    # APEX-PARTNERAPI-2026-10-01: see _board_render() -- only trust the
    # Playwright-only stats global when Playwright actually ran this time.
    if _data_source == "playwright":
        stats = getattr(core, "LAST_PLAYWRIGHT_SCRAPE_STATS", {}) or {}
    else:
        stats = {"raw_total": len(props), "deduped_total": len(props), "per_tab": {}}
    result = {
        "ok": True,
        "raw_total": stats.get("raw_total", len(props)),
        "deduped_total": stats.get("deduped_total", len(props)),
        "per_tab": stats.get("per_tab", {}),
        "data_source": _data_source,
    }
    snap_path = core.ARCHIVE_DIR / f"board_snapshot_{dates['match_date']}_{int(_time.time())}.json"
    try:
        with open(snap_path, "w") as f:
            _json.dump(props, f, indent=2, default=str)
        result["archive_path"] = str(snap_path)
    except Exception as e:
        result["archive_error"] = _friendly_error(e)
    return result


_FETCH_STARTED = {"ts": None}   # APEX-PUBLISHPOOL-2026-10-11: start time of the running /fetch
_FETCH_RUNNING = {"on": False}   # APEX-FETCHCANCEL-2026-10-12: True while a /fetch is in flight


# APEX-FETCHSTATUS-2026-10-23 (owner, "Go" 2026-10-23): the 3:03 PM /fetch on 10/23
# ran 40 minutes with nothing in #general (only #system got 3 updates), so a slow run looked
# dead. Now one #general message is posted at the start and edited as the run moves:
# "N of M scored" at most every 2 minutes, then ✅ / 🛑. A 10-minute watchdog posts once to
# #general + #alerts if boards are not out yet. A marker file lets the next bot start say
# "interrupted by restart" when a restart kills a running fetch.
FETCH_RUNNING_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs", "fetch_running.json")
_FETCH_PROGRESS = {}
FETCH_STATUS_EVERY_S = 120
FETCH_WATCHDOG_S = 600
_FETCH_STEP_WORDS = {"pull": "pulling PrizePicks", "scraped": "pulling PrizePicks",
                     "evaluating": "scoring props", "progress": "scoring props",
                     "publishing": "posting boards"}


def _ct_hm(ts=None):
    t = __import__("datetime").datetime.fromtimestamp(ts or time.time(), core.CT)
    return t.strftime("%I:%M %p").lstrip("0")


def _fetch_status_text():
    p = _FETCH_PROGRESS
    mins = int((time.time() - p.get("t0", time.time())) // 60)
    step = p.get("step", "pull")
    if step in ("evaluating", "progress"):
        now = f"scoring props: {p.get('done', 0)} of {p.get('total', '?')} scored"
    elif step == "publishing":
        now = f"posting boards ({p.get('selected', 0)} picks)"
    else:
        now = "pulling PrizePicks"
    return (f"⏳ **Fetch started {_ct_hm(p.get('t0'))}**: pulling PrizePicks → scoring props → boards\n"
            f"Now: {now} ({mins} min so far). Updates every 2 minutes.")


def _fetch_status_edit(text=None):
    mid = _FETCH_PROGRESS.get("general_mid")
    if mid:
        core.edit_discord_message("general", mid, text or _fetch_status_text())
        _FETCH_PROGRESS["last_edit"] = time.time()
    _fetch_alert_chain(text)


def _fetch_alert_chain(final_text=None):
    """APEX-ALERTCHAIN-2026-10-25: the #alerts 'Fetch slow / stalled' card is one card that follows
    the run -- progress every 2 min, then ✅ boards posted / 🛑 failed / cancelled, edited in place."""
    try:
        import apex_alert_chain as AC
        import apex_alert_cards as C
        if not AC.CHAINS_ON or not AC.get("fetch", max_age_min=180):
            return
        if final_text is None:
            AC.step("fetch", _fetch_status_text().replace("⏳ ", ""),
                    "This card turns ✅ when the boards post (or red if the fetch fails).", wait_min=5)
            return
        e = C.from_text(final_text)
        if final_text.lstrip().startswith(("✅", "\U0001F7E2")) or "Boards posted" in final_text:
            e["color"] = C.GREEN
        AC.finish("fetch", e)
    except Exception as ex:
        print(f"[FetchStatus] alert card update failed: {type(ex).__name__}: {ex}")


def _fetch_progress_note(stage, **kw):
    """Called from the fetch thread. Records the step; edits #general at most every 2 min."""
    p = _FETCH_PROGRESS
    p["step"] = stage
    if "total" in kw:
        p["total"] = kw["total"]
    if "valid_props" in kw:
        p["total"] = kw["valid_props"]
    if "done" in kw:
        p["done"] = kw["done"]
    if "selected" in kw:
        p["selected"] = kw["selected"]
    if stage != "progress" or kw.get("done") != p.get("_last_done"):
        p["moved_at"] = time.time()
        p["_last_done"] = kw.get("done")
    if stage == "publishing" or time.time() - p.get("last_edit", 0) >= FETCH_STATUS_EVERY_S:
        _fetch_status_edit()


def _fetch_failed_text(reason):
    step = _FETCH_STEP_WORDS.get(_FETCH_PROGRESS.get("step"), _FETCH_PROGRESS.get("step") or "starting")
    return f"🛑 **Fetch failed** at step: {step} -- {str(reason)[:300]}\nRun /fetch again once that is fixed."


def _fetch_marker_write(on):
    try:
        if on:
            with open(FETCH_RUNNING_MARKER, "w") as fh:
                json.dump({"started": time.time()}, fh)
        elif os.path.exists(FETCH_RUNNING_MARKER):
            os.remove(FETCH_RUNNING_MARKER)
    except Exception as e:
        print(f"[FetchStatus] marker {'write' if on else 'clear'} failed: {type(e).__name__}: {e}")


def _fetch_marker_check_on_start():
    """Bot start: a marker left behind means a restart killed a running /fetch."""
    try:
        if not os.path.exists(FETCH_RUNNING_MARKER):
            return
        with open(FETCH_RUNNING_MARKER) as fh:
            started = json.load(fh).get("started")
        _fetch_marker_write(False)
        core.send_discord("general", f"⚠️ The /fetch started {_ct_hm(started)} was interrupted by a bot restart "
                                     "-- nothing from it was posted. Run /fetch again.")
    except Exception as e:
        print(f"[FetchStatus] restart check failed: {type(e).__name__}: {e}")


async def _fetch_watchdog():
    """Once, 10 minutes in, if boards are not posted yet: #general + #alerts."""
    try:
        await asyncio.sleep(FETCH_WATCHDOG_S)
    except asyncio.CancelledError:
        return
    p = _FETCH_PROGRESS
    if not _FETCH_RUNNING.get("on"):
        return
    step = _FETCH_STEP_WORDS.get(p.get("step"), p.get("step") or "starting")
    count = f" ({p.get('done', 0)} of {p.get('total', '?')} scored)" if p.get("total") else ""
    idle = int((time.time() - p.get("moved_at", p.get("t0", time.time()))) // 60)
    if idle >= 3:
        msg = f"⚠️ **Fetch stalled** at step: {step}{count} -- no progress for {idle} min. Started {_ct_hm(p.get('t0'))}."
    else:
        msg = f"⚠️ **Fetch slow**: 10 min in, still {step}{count}. Started {_ct_hm(p.get('t0'))}. Boards not posted yet."
    await asyncio.to_thread(core.send_discord, "general", msg)
    try:   # APEX-ALERTCHAIN-2026-10-25: one #alerts card that updates until the boards post
        import apex_alert_chain as AC
        import apex_alert_cards as C
        if AC.CHAINS_ON:
            _left = None
            try:
                _el = (time.time() - p.get("t0", time.time())) / 60.0
                if p.get("total") and p.get("done"):
                    _left = max(1.0, _el * (p["total"] - p["done"]) / max(1, p["done"]))
            except Exception:
                _left = None
            await asyncio.to_thread(AC.start, "fetch", C.from_text(msg),
                                    "Boards post when scoring finishes — this card turns ✅ then (red if the fetch fails).",
                                    _left if _left is not None else 5, None, "alerts", False)
            return
    except Exception as ex:
        print(f"[FetchStatus] alert card failed: {type(ex).__name__}: {ex}")
    await asyncio.to_thread(core.send_discord_tracked, "alerts", msg)   # tracked: not muted during a fetch


def _fetch_busy_claim():
    """APEX-PPTHROTTLE-2026-10-16: one /fetch at a time. Returns True (and
    claims the slot) when no /fetch is running; False when one already is.
    The claim covers the few seconds before _FETCH_RUNNING["on"] is set and
    expires on its own after 60 s, so a failed start can never block /fetch."""
    if _FETCH_RUNNING["on"] or time.time() - _FETCH_RUNNING.get("claimed", 0) < 60:
        return False
    _FETCH_RUNNING["claimed"] = time.time()
    return True


def _fetch_publish_all_render(progress_cb=None):
    """/fetch (and /fetch evaluate): the full actionable pipeline.
    Scrape -> filter -> Gate 11 + odds + engine eval (process_single_prop,
    unchanged) -> correlation gate / Rule 31 -> rule_28_agent(dedup=False)
    so EVERY qualifying (GREEN/YELLOW) pick publishes, not just one per
    player/match -> output_agent posts every one of them to the normal
    Picks Channel (plus its own Best Bets headline block, untouched) ->
    archive.

    Gate 11 verified/declined counts are captured with a counting
    wrapper around core.gate_11_check — observe-only, restored right
    after, never changes what Gate 11 returns or how many times it's
    called (so this adds zero extra API cost over the old /fetch
    evaluate path)."""
    def _notify(stage, **kw):   # APEX-DISCORDREPAIR-2026-09-30: best-effort only
        if progress_cb:
            try:
                progress_cb(stage, **kw)
            except Exception:
                pass

    dates = core.get_system_dates()
    _FETCH_STARTED["ts"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")   # APEX-PUBLISHPOOL-2026-10-11
    _FETCH_STARTED.pop("status", None)   # APEX-BOARDSTATUS-HEADER-2026-10-25: never an older run's status
    core.clear_fetch_cancel()   # APEX-FETCHCANCEL-2026-10-12: a fresh run never inherits an old cancel

    # APEX-DISCORDREPAIR-2026-09-30: silence individual #alerts sends for
    # the duration of this run (they're what flooded Discord and stalled
    # the pipeline past the 15-min interaction window on 2026-09-30).
    # Nothing about WHEN an alert would fire changes -- only whether it
    # reaches Discord one-by-one during a fetch. Always turned back off,
    # even on an exception, via the try/finally below.
    core.begin_fetch_discord_suppression()
    _result = {"ok": False, "error": "unexpected: render returned nothing"}
    try:
        _result = _fetch_publish_all_render_inner(dates, _notify)
    finally:
        _suppressed = core.end_fetch_discord_suppression()
        if isinstance(_result, dict):
            _result["suppressed_alerts"] = _suppressed
    return _result


def _fetch_publish_all_render_inner(dates, _notify):
    # APEX-BOARDHEALTH-2026-09-30: snapshot the active board as it stood
    # BEFORE this fetch writes anything, so the Board Health Report can
    # show a real before/after instead of just an after-the-fact count.
    # Read-only -- the same load_current_board() every board command uses.
    _previous_active_board = None
    try:
        import apex_edge_tracker as _aet_pre
        _prev_picks, _prev_note = _aet_pre.load_current_board()
        _previous_active_board = len(_prev_picks)
    except Exception:
        pass

    # APEX-PARTNERAPI-2026-10-01 (API-First Fetch -- IMPLEMENTATION
    # APPROVED): this is the real, live /fetch command path. Was a direct
    # fetch_props_playwright() call; now goes through
    # fetch_props_with_fallback(), which tries partner-api.prizepicks.com
    # first and automatically falls back to this exact same Playwright
    # path on any 403/429/timeout/schema failure/empty response. No user
    # action either way -- which path ran is only recorded for the Board
    # Health Report ("Data Source Used" line below), never surfaced as a
    # decision the user has to make.
    try:
        props, _data_source = core.fetch_props_with_fallback(dates["match_date"], dates)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if not props:
        # APEX-FETCHPIPE-2026-09-30: was a hardcoded guess ("Most likely
        # Chrome isn't running...") shown regardless of the real cause --
        # confirmed live on 2026-09-30 that guess was wrong (Chrome WAS
        # running) and its screenshot was 2 days stale. Now shows the
        # actual stage/exception/this-run's-own screenshot+HTML.
        return {"ok": False, "error": core.describe_last_fetch_failure()}

    # APEX-PARTNERAPI-2026-10-01: LAST_PLAYWRIGHT_SCRAPE_STATS only gets
    # (re)populated when fetch_props_playwright() actually ran this call --
    # on a partner-API success it would still hold whatever a PRIOR
    # Playwright run left behind, mislabeling stale per-tab numbers as
    # current. Only trust that global when Playwright is the source
    # THIS run actually used.
    if _data_source == "playwright":
        stats = getattr(core, "LAST_PLAYWRIGHT_SCRAPE_STATS", {}) or {}
    else:
        stats = {"raw_total": len(props), "deduped_total": len(props), "per_tab": {}}
    result = {
        "ok":            True,
        "raw_total":     stats.get("raw_total", len(props)),
        "deduped_total": stats.get("deduped_total", len(props)),
        "per_tab":       stats.get("per_tab", {}),
        "data_source":   _data_source,
    }
    _notify("scraped", scraped=len(props))   # APEX-DISCORDREPAIR-2026-09-30
    if core.fetch_cancel_requested():   # APEX-FETCHCANCEL-2026-10-12
        return {"ok": True, "cancelled": True, "stage": "scrape", "cancel_evaluated": 0,
                "cancel_total": len(props)}

    _orig_gate11 = core.gate_11_check
    _g11 = {"verified": 0, "rejected": 0}

    def _gate11_counting_wrapper(prop, dates_):
        r = _orig_gate11(prop, dates_)
        if r.get("status") == "REJECTED":
            _g11["rejected"] += 1
        else:
            _g11["verified"] += 1
        return r

    core.gate_11_check = _gate11_counting_wrapper
    try:
        weights      = core.load_weights()
        valid_props  = core.filter_props(props, dates)
        valid_props  = core._sort_props_evaluation_order(valid_props)
        _notify("evaluating", valid_props=len(valid_props))   # APEX-DISCORDREPAIR-2026-09-30

        # APEX-FETCHSTATUS-2026-10-23: check SportsAPI365 once before scoring. A refused key
        # stops the run here -- nothing published, active board unchanged.
        import apex_sa365_guard as _sa365g
        _sa365g.reset()
        _sa_ok, _sa_why = _sa365g.preflight()
        if not _sa_ok:
            core.log("Discord", f"/fetch stopped before scoring: SportsAPI365 {_sa_why}")
            return {"ok": True, "cancelled": True, "sa365_refused": True, "stage": "evaluate",
                    "cancel_evaluated": 0, "cancel_total": len(valid_props)}

        scored_props  = []
        gate_rejected = []
        _cancelled = False
        _sa365_dead = False
        for _i, prop in enumerate(valid_props):
            if core.fetch_cancel_requested():   # APEX-FETCHCANCEL-2026-10-12: stop between props
                _cancelled = True
                break
            if _sa365g.status().get("dead_at"):   # APEX-FETCHSTATUS-2026-10-23: key refused mid-run
                _cancelled = _sa365_dead = True
                break
            _notify("progress", done=_i, total=len(valid_props))
            prop.setdefault("tournament_stage", 1)
            prop.setdefault("same_day_3set_count", 0)
            scored = core.process_single_prop(prop, dates, weights)
            if scored:
                scored_props.append(scored)
            else:
                gate_rejected.append(prop)
        if _cancelled:   # APEX-FETCHCANCEL-2026-10-12: nothing below runs, nothing is published
            core.log("Discord", f"/fetch cancelled by request after {len(scored_props) + len(gate_rejected)} of {len(valid_props)} props")
            return {"ok": True, "cancelled": True, "stage": "evaluate", "sa365_refused": _sa365_dead,
                    "cancel_evaluated": len(scored_props) + len(gate_rejected), "cancel_total": len(valid_props)}

        # APEX-MOREFIXAUDIT-2026-10-05: the true "how many props did the
        # engine actually evaluate" count, captured HERE -- before the
        # correlation gate / rule 31 / demonlock steps below remove any of
        # them. The old "evaluated" field in the Board Summary was captured
        # AFTER all of those removals, which made it read as "Apex only
        # looked at 166 props" when the real number the engine scored was
        # much higher. This is additive only -- nothing about what gets
        # filtered or published changes.
        result["engine_evaluated_raw"] = len(scored_props)
        result["pre_engine_drops"]     = len(gate_rejected)

        # APEX-BOARDHEALTH-2026-09-30 / APEX-DECLINETRANSPARENCY-2026-10-01:
        # categorize why each declined prop was declined, for the Board
        # Health Report. Purely observational -- reads the reason
        # process_single_prop already stamped on the prop
        # (prop["apex_decline_reason"], set by openclaw_core._apex_decline);
        # changes nothing about what gets declined or why.
        #
        # AUDIT FIX (2026-10-01): the old 4-bucket version's "unknown_players"
        # matcher looked for the substring "unknown player", but the real
        # decline string for a missing name is "missing player/opponent" --
        # that bucket never matched anything, live. Gate 11 rejections also
        # returned None with NO reason stamped at all (fixed separately in
        # openclaw_core.process_single_prop). Both bugs meant most real
        # decline reasons fell into one opaque "missing_data" catch-all.
        # Rewritten to the 6 named categories owner asked for, with
        # every known apex_decline_reason string mapped explicitly below and
        # only a genuinely-unrecognized reason landing in "other".
        _decline_buckets = {"first_set_markets": 0, "unknown_players": 0, "doubles": 0,
                             "no_odds": 0, "missing_model_data": 0, "other": 0}
        for _gp in gate_rejected:
            _reason = str(_gp.get("apex_decline_reason") or "").lower()
            if "/" in str(_gp.get("player") or "") + str(_gp.get("opponent") or ""):
                # APEX-DOUBLESLINE-2026-10-23 (owner, "Go for everything"): doubles
                # teams are not unknown players -- the engine has no doubles model.
                _decline_buckets["doubles"] += 1
            elif "first-set research collection" in _reason:
                _decline_buckets["first_set_markets"] += 1
            elif "no usable odds" in _reason:
                _decline_buckets["no_odds"] += 1
            elif ("missing player/opponent" in _reason
                  or "unknown player" in _reason
                  or "gate 11 rejected" in _reason):
                _decline_buckets["unknown_players"] += 1
            elif ("unmapped stat_type" in _reason or "unusable line" in _reason
                  or "no result" in _reason
                  or re.search(r"no \w+ row with a line", _reason)):
                _decline_buckets["missing_model_data"] += 1
            elif not _reason:
                # A prop that reached gate_rejected with NO reason stamped at
                # all is itself a defect to surface, not to hide in "other"
                # silently -- but it is at least now visible and counted,
                # which is the fix this audit asked for.
                _decline_buckets["other"] += 1
            else:
                # match-already-started, goes_5_sets-on-bo3, and any other
                # genuinely unclassified decline path.
                _decline_buckets["other"] += 1
        result["decline_breakdown"] = _decline_buckets
        result["declined_exact"]    = len(gate_rejected)

        correlation_map = core._build_correlation_map(scored_props)
        scored_props    = core._apply_correlation_gate(scored_props, correlation_map)
        scored_props    = core.rule_31_correlation_check(scored_props)

        # APEX-DEMONLOCK-2026-09-29: /fetch's live posting never goes through
        # apex_edge_tracker.load_current_board(), which is the only place the
        # playable=0 (direction-locked) drop was ever enforced -- so a
        # MORE-only Demon/Goblin/Red-Demon prop the engine scored as LESS
        # could reach the Picks Channel here even after the filter_props fix.
        # The "scored" dict returned by process_single_prop never carries a
        # "playable" field (that's computed later, CSV-log-only, inside
        # _apex_forward_log_row), so this checks the platform rule directly
        # against the two fields it DOES carry: prop_type_classified + direction.
        _demonlock_dropped = [p for p in scored_props
                              if (p.get("prop_type_classified") or "") in
                                 ("demon", "goblin", "red_demon")
                              and (p.get("direction") or "").upper() == "LESS"]
        if _demonlock_dropped:
            scored_props = [p for p in scored_props if p not in _demonlock_dropped]
            core.log("Filter",
                f"/fetch: excluded {len(_demonlock_dropped)} MORE-only prop(s) "
                f"the engine scored LESS (impossible on PrizePicks) from posting")
        result["demonlock_dropped"] = len(_demonlock_dropped)   # APEX-DISCORDREPAIR-2026-09-30

        # APEX-MOREFIXAUDIT-2026-10-05: per-prop evidence for the
        # MORE-only conflict audit. Every entry in _demonlock_dropped
        # got here by the exact filter above, which ONLY matches
        # direction == "LESS" -- there is no code path for a prop with
        # Apex direction "MORE" to land in this list, so
        # apex_preferred_more below should always read 0. Recorded per
        # prop rather than assumed, so the report shows real data, not
        # an assertion about the filter's own logic.
        _demonlock_detail = [{
            "player":        p.get("player"),
            "market":        p.get("stat_type"),
            "line":          p.get("line"),
            "prop_type":     p.get("prop_type_classified"),
            "pp_direction":  "MORE only",   # platform rule for demon/goblin/red_demon — see openclaw_core.py:2659-2661
            "apex_direction": p.get("direction"),
            "confidence":    p.get("confidence"),
            "reason":        "MORE-only conflict — PrizePicks only offers MORE on this "
                              "prop type; the engine's own computed direction was LESS",
        } for p in _demonlock_dropped]
        result["demonlock_detail"]        = _demonlock_detail
        result["apex_preferred_less"]     = sum(1 for d in _demonlock_detail
                                                 if (d["apex_direction"] or "").upper() == "LESS")
        result["apex_preferred_more"]     = sum(1 for d in _demonlock_detail
                                                 if (d["apex_direction"] or "").upper() == "MORE")

        # APEX-DECLINETRANSPARENCY-2026-10-01: MORE-only conflicts
        # (demonlock_dropped) are a real decline reason too -- these are
        # scored props the engine produced, then dropped for being
        # direction-impossible on PrizePicks -- so they belong in the SAME
        # decline breakdown as the gate_rejected buckets above, not off on
        # their own where "Props Declined" silently includes them but no
        # visible count explains them. Folding it in here makes
        # decline_breakdown's 6 named counts reconcile EXACTLY to total
        # declined by construction (declined is now defined as their sum,
        # see result.update() below).
        _decline_buckets["more_only_conflicts"] = len(_demonlock_dropped)
        result["decline_breakdown"] = _decline_buckets

        ranked = sorted(scored_props, key=lambda x: x.get("confidence", 0), reverse=True)
        # dedup=False — APEX-FETCH-PUBLISHALL-2026-09-29: every scored
        # pick passes through, not just one per player/match.
        selected = core.rule_28_agent(ranked, dates, dedup=False)

        # APEX-FETCH-SAMEASMANUAL-2026-09-29: post exactly the way a
        # manual screenshot submission does (openclaw_core.evaluate_
        # manual_prop, "Compact results to #picks-channel" block) --
        # every scored prop, unconditionally, batched into one message.
        # NOT core.output_agent(): that function only cards a pick when
        # pick["color"] is exactly "GREEN"/"YELLOW", a field the frozen
        # engine (_apex_engine_pick) never sets -- so output_agent has
        # been silently posting zero individual cards in engine-only
        # mode (confirmed empirically this morning). It also always
        # appends the Best Bets headline block, which owner
        # wants ONLY on explicit /bestgoblin /beststandard /bestdemon
        # /top10 /allpicks requests, not automatically on every /fetch.
        _notify("publishing", selected=len(selected))   # APEX-DISCORDREPAIR-2026-09-30
        # APEX-PICKSCLEAN-2026-10-24 (owner, "it give too much information"): the
        # per-prop "Props --" dump no longer posts to #picks; #picks gets the one short
        # plays card from republish_boards below. Archiving/saving is unchanged.
        core._archive_picks(selected, dates)

        # APEX-BOARDCAP-2026-10-01: record which props land on Elite6 /
        # Top10 / Best Goblin / Best Demon / Best Standard today, using the
        # exact slate just archived -- the only point this is ever
        # persisted, since the board commands themselves are on-demand
        # re-rankings. Fail-open: never blocks publishing.
        try:
            import apex_edge_tracker as _aet_cap
            # APEX-PUBLISHPOOL-2026-10-11 (owner, approved "yes, keep"
            # 2026-10-11): publish from the active board (every prop this scrape
            # found still offered, with its latest good evaluation) -- the same pool
            # the 5 PM run and the restart/reprice republish use -- then post what
            # changed since the previous publish (line moves, removed lines,
            # probability moves, props kept on a non-refreshed price).
            _FETCH_STARTED["cap_before"] = core._board_status_cap()   # APEX-BOARDSTATUS-HEADER-2026-10-25
            _aet_cap.republish_boards(source="manual", fetch_started=_FETCH_STARTED.get("ts"))
            _FETCH_STARTED["status"] = core._board_status(_FETCH_STARTED["cap_before"])
            _chg = getattr(_aet_cap, "LAST_REPUBLISH_CHANGES", "")
            if _chg:   # APEX-PICKSCLEAN-2026-10-24: change report moved from #picks to #system
                core.send_discord("system", core._board_status_wrap(_FETCH_STARTED["status"], _chg))
        except Exception as _e:
            core.log("BoardCapture", f"capture failed: {type(_e).__name__}: {_e}")

        # APEX-FETCHREPORT-BOARDSTATS-2026-10-05 (Phase 9, owner): one
        # real call per board (same functions /top3 /elite6 /top10 /bestgoblin
        # /bestdemon /beststandard already use), captured right here while
        # today's `selected` slate is in hand, so the Fetch Completion Report
        # can show real Board Inventory / Prop Distribution / Board Quality /
        # Board Output / Legacy Picks Available numbers per board instead of
        # just a bare final count. Never re-ranks or re-scores anything new --
        # reuses the exact same board-detail calls, just captures their
        # return_scored=True internals too. Fail-open per board: one board's
        # failure never blocks the others or the fetch itself.
        _board_stats = {}
        try:
            import apex_edge_tracker as _aet_bs
            # APEX-BOARDSTATS-FIELDFIX-2026-10-07 (owner, All-Boards-
            # Empty fix): `selected` is shaped with stat_type / confidence /
            # prop_type_classified (the live engine's own field names) --
            # NOT the category / prob / prop_type / bo5 names best_bets_
            # detail()/elite6_detail() require. Both functions silently
            # skip any prop with no "category"
            # (`if not p.get("category"): continue`), so every single
            # `selected`-shaped prop was dropped before scoring -- the
            # exact same gap APEX-LEGACYFIX-2026-10-05 already found and
            # fixed for capture_board_membership() two days earlier. That
            # fix was never applied here, so this block (introduced the
            # same day, APEX-FETCHREPORT-BOARDSTATS-2026-10-05) kept
            # reporting a hard "0 prop(s) considered" for every board --
            # not the real "N considered, below floor" picture -- which is
            # what made the 2026-10-07 Fetch Completion Report show
            # Top3/Elite6/Top10/Goblin/Demon/Standard all at 0 even though
            # load_current_board() had 113 live, playable picks that day.
            # Same translation as APEX-LEGACYFIX-2026-10-05, applied here
            # too. A prop that already has "category" (already correctly
            # shaped) passes through untouched.
            try:
                _bs_stat_map = core._APEX_STAT_MAP
            except Exception:
                _bs_stat_map = {}

            def _boardstats_translate(p):
                if p.get("category"):
                    return p
                _slug = str(p.get("stat_type") or "").strip().lower().replace(" ", "_")
                _cat = _bs_stat_map.get(_slug, "")
                if not _cat:
                    return p
                q = dict(p)
                q["category"] = _cat
                q.setdefault("prob", float(p.get("confidence") or 0))
                q.setdefault("prop_type", p.get("prop_type_classified", "standard"))
                q.setdefault("bo5", str(p.get("apex_best_of") or "") == "5")
                return q

            _board_input = [_boardstats_translate(p) for p in selected]
            _board_calls = {
                "top3":     lambda: _aet_bs.best_bets_detail(_board_input, limit=3, return_scored=True),
                "elite6":   lambda: _aet_bs.elite6_detail(_board_input, return_scored=True),
                "top10":    lambda: _aet_bs.best_bets_detail(_board_input, limit=10, return_scored=True),
                "goblin":   lambda: _aet_bs.best_bets_detail(_board_input, prop_type="goblin", limit=6, slip_rules=True, return_scored=True),
                "demon":    lambda: _aet_bs.best_bets_detail(_board_input, prop_type="demon", limit=6, slip_rules=True, return_scored=True),
                "standard": lambda: _std_published(),
            }

            def _std_published():
                # APEX-STDREPORT-2026-10-14: Per-Board Detail for Standard reads the
                # published Best Standard board (the Standard rule), the same one
                # Board Counts and /beststandard use -- the generic Standard build
                # it used before counted Aces / Fantasy Score props the rule excludes.
                _r = _aet_bs.published_board_detail("beststandard", return_scored=True)
                if isinstance(_r, dict) and not _r.get("scored"):
                    return "\n".join(str(_r.get("text") or "").split("\n")[1:])[:600]
                return _r
            _legacy_flag_map = {"goblin": "bestgoblin", "demon": "bestdemon", "standard": "beststandard"}
            for _flag, _call in _board_calls.items():
                try:
                    _r = _call()
                    if not isinstance(_r, dict):
                        _board_stats[_flag] = {"inventory": 0, "output": 0, "quality_avg": None,
                                                "distribution": {}, "legacy_count": 0,
                                                "empty_msg": str(_r)[:300]}
                        continue
                    _scored = _r.get("scored") or []
                    _held = _r.get("held_back") or []
                    _dist = {}
                    for _d in _scored:
                        _c = _d["p"].get("category", "?")
                        _dist[_c] = _dist.get(_c, 0) + 1
                    _avg = (sum(_d["sc"] for _d in _scored) / len(_scored)) if _scored else None
                    try:
                        _lflag = _legacy_flag_map.get(_flag, _flag)
                        _legacy_n = len(_aet_bs._legacy_picks_for_board(_lflag, _scored, limit=6))
                    except Exception:
                        _legacy_n = 0
                    _board_stats[_flag] = {
                        "inventory": len(_scored) + len(_held),
                        "output": len(_scored),
                        "quality_avg": _avg,
                        "distribution": _dist,
                        "legacy_count": _legacy_n,
                    }
                except Exception as _e:
                    _board_stats[_flag] = {"error": f"{type(_e).__name__}: {_e}"}
        except Exception as _e:
            core.log("BoardStats", f"board stats failed (non-fatal): {_e}")
        result["board_stats"] = _board_stats

        published = list(selected)

        def _ptype(p):
            t = p.get("prop_type_classified") or "standard"
            return "demon" if t == "red_demon" else t

        demon_pub    = [p for p in published if _ptype(p) == "demon"]
        goblin_pub   = [p for p in published if _ptype(p) == "goblin"]
        standard_pub = [p for p in published if _ptype(p) == "standard"]

        def _best(lst):
            if not lst:
                return None
            b = max(lst, key=lambda p: p.get("confidence", 0))
            return (f"{b.get('player','')} {b.get('line','')} {b.get('stat_type','')} "
                    f"{b.get('direction','')} — {b.get('confidence',0):.0%}")

        # APEX-DECLINETRANSPARENCY-2026-10-01: "declined" used to be computed
        # independently as (gate11 verified - scored_props), which quietly
        # EXCLUDED Gate 11's own rejections (they're outside "verified" by
        # definition) while decline_breakdown's buckets (built from
        # gate_rejected, which DOES include Gate 11 rejections) covered a
        # different population -- the two numbers could disagree, which is
        # exactly the "invisible declines" gap this audit found. Declined is
        # now defined as the sum of the 6 named buckets, so it reconciles to
        # them EXACTLY, by construction, every time.
        _declined_total = sum(_decline_buckets.values())
        result.update({
            "scraped":            len(props),
            "valid_after_filter": len(valid_props),
            "gate11_verified":    _g11["verified"],
            "gate11_rejected":    _g11["rejected"],
            "evaluated":          len(scored_props),
            "passed_evaluation":  len(scored_props),   # APEX-MOREFIXAUDIT-2026-10-05: same count as "evaluated" above, exposed under the clearer name the new report uses
            "declined":           _declined_total,
            "published":          len(published),
            "demon_count":        len(demon_pub),
            "goblin_count":       len(goblin_pub),
            "standard_count":     len(standard_pub),
            "best_demon":         _best(demon_pub),
            "best_goblin":        _best(goblin_pub),
            "best_standard":      _best(standard_pub),
        })
    except Exception as e:
        result["evaluate_error"] = f"{type(e).__name__}: {e}"
    finally:
        core.gate_11_check = _orig_gate11

    # APEX-DISCORDREPAIR-2026-09-30: freshness/sync + Discord delivery
    # stats for the fetch summary -- read-only, nothing here changes the
    # freshness system or how/when it's computed.
    try:
        import apex_edge_tracker as _aet
        _sync_ts, _sync_hours = _aet.last_successful_sync()
        result["freshness_line"]    = _aet.sync_status_line()
        result["freshness_warning"] = _aet.sync_warning_banner()
        result["sync_hours_ago"]    = _sync_hours
    except Exception:
        pass
    result["discord_delivery"] = dict(getattr(core, "LAST_DISCORD_DELIVERY_STATS", {}) or {})
    # NOTE: "suppressed_alerts" is added by the outer _fetch_publish_all_render()
    # wrapper after this function returns (that's when the true count is known).

    # APEX-BOARDHEALTH-2026-09-30: Board Health Report stats. Entirely
    # observational -- reads the board through the SAME read-only functions
    # /board, /elite6, /bestgoblin, /bestdemon, /beststandard already call
    # (load_current_board, elite6_detail, best_bets_detail with
    # return_scored=True). Nothing here touches Apex Score, Confidence,
    # ALIS, ranking, selection, retirement, or identity-repair logic -- it
    # only COUNTS what those unmodified functions already decided.
    try:
        import apex_edge_tracker as _aet2
        _health_picks, _health_note = _aet2.load_current_board()
        _health_retired = dict(getattr(_aet2, "LAST_FETCH_RETIRE_DEBUG", {}) or {}).get("retired_count", 0)
        _build_dbg = dict(getattr(_aet2, "LAST_BOARD_BUILD_DEBUG", {}) or {})
        _health_rec = _aet2.records()

        _health_held_back = []   # APEX-FETCHCOMPLETION-2026-10-01: collected below

        def _scored_count(flag, cap):
            # APEX-PUBLISHEDBOARD-COUNTFIX-2026-10-07 (owner,
            # authorized): Board Counts must report the SAME number the
            # matching slash command will actually show, and since those
            # commands now read the published snapshot (published_board_
            # detail) instead of re-running elite6_detail/best_bets_detail
            # live off a freshly-reloaded load_current_board(), this count
            # has to read that same snapshot -- otherwise Board Counts and
            # Per-Board Detail tell two different stories about the same
            # fetch, which is exactly what broke: Board Counts was re-
            # deriving from a reloaded live board (which can legitimately
            # come back thin/empty right after a fetch) while Per-Board
            # Detail and the real commands were already reading the
            # just-written snapshot. capture_board_membership() (and its
            # _write_published_snapshot call) already ran earlier in this
            # same fetch, so the snapshot is current for this run.
            try:
                r = _aet2.published_board_detail(flag, rec=_health_rec, return_scored=True)
                if isinstance(r, str):
                    return 0, r
                for _h in (r.get("held_back") or []):
                    _health_held_back.append(_h)
                scored = r.get("scored") or []
                return min(len(scored), cap), r.get("text", "")
            except Exception as _e:
                return 0, f"(health check unavailable: {type(_e).__name__})"

        # Flags match _PUBLISHED_FLAG_LIMIT / _PUBLISHED_BOARD_FLAGS in
        # apex_edge_tracker.py / _bb_render() above -- kept in sync by hand.
        _e6_n, _e6_msg = _scored_count("elite6", 6)
        _gb_n, _gb_msg = _scored_count("bestgoblin", 6)
        _dm_n, _dm_msg = _scored_count("bestdemon", 6)
        _sd_n, _sd_msg = _scored_count("beststandard", 6)
        _t10_n, _t10_msg = _scored_count("top10", 10)
        _t3_n, _t3_msg = _scored_count("top3", 3)

        _current_active_board = len(_health_picks)
        result["health"] = {
            "previous_active_board": _previous_active_board,
            "active_board":     _current_active_board,
            "net_change":       (None if _previous_active_board is None
                                  else _current_active_board - _previous_active_board),
            "retired":          _health_retired,
            "matchid_retired":    _build_dbg.get("matchid_retired", 0),
            "tier_retired":       _build_dbg.get("tier_retired", 0),
            "opponent_retired":   _build_dbg.get("opponent_retired", 0),
            "fetch_retired":      _build_dbg.get("fetch_retired", 0),
            "unplayable_retired": _build_dbg.get("unplayable_retired", 0),
            "graded_retired":     _build_dbg.get("graded_retired", 0),
            "elite6_count":     _e6_n, "elite6_empty_msg":   _e6_msg if _e6_n == 0 else "",
            "goblin_count":     _gb_n, "goblin_empty_msg":   _gb_msg if _gb_n == 0 else "",
            "demon_count":      _dm_n, "demon_empty_msg":    _dm_msg if _dm_n == 0 else "",
            "standard_count":   _sd_n, "standard_empty_msg": _sd_msg if _sd_n == 0 else "",
            "top10_count":      _t10_n,
            "top3_count":       _t3_n,
        }
        # APEX-FETCHCOMPLETION-2026-10-01: dedup held-back entries across
        # the 6 views above (a prop can legitimately be held back from more
        # than one board) so the Fetch Completion Report's count isn't
        # inflated by double-counting the same prop.
        _seen_hb, _hb_dedup = set(), []
        for _h in _health_held_back:
            _p = _h.get("p") or {}
            _key = (_p.get("player"), _p.get("category"), str(_p.get("line")), _h.get("reason"))
            if _key in _seen_hb:
                continue
            _seen_hb.add(_key)
            _hb_dedup.append(_h)
        result["held_back"] = _hb_dedup
    except Exception as e:
        result["health"] = {"error": f"{type(e).__name__}: {e}"}

    # APEX-BOARDHEALTH-2026-09-30: permanent fetch-health history on disk,
    # one record per successful fetch, so a future audit never again needs
    # to be reconstructed from text logs. Never raises to the caller.
    try:
        from datetime import datetime as _dt2
        _hist_path = os.path.join("data", "logs", "fetch_health_history.json")
        _health = result.get("health") or {}
        _hist_record = {
            "timestamp":            _dt2.now().isoformat(timespec="seconds"),
            "raw_scraped":          result.get("raw_total"),
            "deduped":              result.get("deduped_total"),
            "api_additions":        max(0, (result.get("scraped") or 0) - (result.get("deduped_total") or 0)),
            "total_scraped":       result.get("scraped"),
            "evaluated":            len(scored_props),
            "declined":             result.get("declined_exact"),
            "decline_breakdown":    result.get("decline_breakdown"),
            "direction_impossible": result.get("demonlock_dropped", 0),
            "retired":              _health.get("retired", 0),
            "previous_active_board": _health.get("previous_active_board"),
            "current_active_board":  _health.get("active_board", 0),
            "net_change":            _health.get("net_change"),
            "tier_retired":         _health.get("tier_retired", 0),
            "opponent_retired":     _health.get("opponent_retired", 0),
            "matchid_retired":      _health.get("matchid_retired", 0),
            "fetch_retired":        _health.get("fetch_retired", 0),
            "unplayable_retired":   _health.get("unplayable_retired", 0),
            "active_board":         _health.get("active_board", 0),
            "elite6_count":         _health.get("elite6_count", 0),
            "top10_count":          _health.get("top10_count", 0),
            "goblin_count":         _health.get("goblin_count", 0),
            "demon_count":          _health.get("demon_count", 0),
            "standard_count":       _health.get("standard_count", 0),
        }
        _hist = []
        try:
            if os.path.exists(_hist_path):
                with open(_hist_path, "r", encoding="utf-8") as _fh:
                    _hist = json.load(_fh)
                if not isinstance(_hist, list):
                    _hist = []
        except Exception:
            _hist = []
        _hist.append(_hist_record)
        os.makedirs(os.path.dirname(_hist_path), exist_ok=True)
        with open(_hist_path, "w", encoding="utf-8") as _fh:
            json.dump(_hist, _fh, indent=2)
        result["health_history_path"]   = _hist_path
        result["health_history_record"] = _hist_record
    except Exception as e:
        result["health_history_error"] = f"{type(e).__name__}: {e}"

    return result


async def _handle_board_command(message):
    await message.channel.send(
        "📡 **/board** — scrape-only diagnostics (all 11 tabs, no Gate 11, "
        "no odds, no engine, no picks)… this can take under a minute."
    )
    try:
        result = await asyncio.to_thread(_board_render)
    except Exception as e:
        await message.channel.send(f"❌ **Board Check Failed**\nReason: `{type(e).__name__}: {e}`")
        return
    if not result.get("ok"):
        await message.channel.send(f"❌ **Board Check Failed**\nReason: {result.get('error', 'unknown error')}")
        return

    per_tab = result.get("per_tab") or {}
    per_tab_lines = "\n".join(f"  {k}: {v}" for k, v in per_tab.items()) or "  (not available)"
    lines = [
        "✅ **Board Check Complete**",
        "",
        "**Board Summary**",
        f"Total Raw Props: {result['raw_total']}",
        f"Total Deduped Props: {result['deduped_total']}",
        "",
        f"Demon: {result['demon']}",
        f"Goblin: {result['goblin']}",
        f"Standard: {result['standard']}",
        "",
        "**Props Per Tab**",
        per_tab_lines,
    ]
    for chunk in _bb_chunk_text("\n".join(lines)):
        await message.channel.send(chunk)


# APEX-PPEMOJI-2026-10-01 (PrizePicks-consistent board emojis, approved by
# owner): one shared mapping for every board-labeled line in the
# Fetch Completion Report / Board Health Report. Goblin changed from the
# old generic 👹 to PrizePicks' own 🟢; Demon/Standard/Elite6/Top10/Top3
# were already correct and are kept. Display-only -- never touches
# Elite6/Goblin/Demon/Standard selection or ranking logic.
_BOARD_EMOJI = {"top3": "\U0001F947", "elite6": "\U0001F3C6", "top10": "\U0001F51F",
                "goblin": "\U0001F7E2", "demon": "\U0001F534", "standard": "\u26AA"}


# APEX-HELDBACKBREAKDOWN-2026-10-01 (Held-Back Transparency breakdown,
# approved by owner): classifies each held_back entry's free-text
# `reason` string (stamped by apex_edge_tracker.best_bets_detail()/
# elite6_detail(), unchanged) into one of 6 named buckets for reporting.
# Read-only string matching -- never changes which props get held back
# or why; only how that reason is grouped for display.
def _held_back_bucket(reason):
    r = str(reason or "").lower()
    if "category cap" in r:
        return "Category Caps"
    if "player already has a slot" in r or ("duplicate" in r and "player" in r):
        return "Duplicate Player"
    if ("match already has a slot" in r or "same match as a pick" in r
            or ("duplicate" in r and "match" in r)):
        return "Duplicate Match"
    if "quality floor" in r:
        return "Quality Floor"
    if "break-even bar" in r or "evidence below" in r:
        return "Evidence Threshold"
    return "Other"


def _held_back_name(p):
    return p.get("opponent") if p.get("player") == "match" else p.get("player", "")


# ================================================================ APEX-FETCHCOMPLETION-2026-10-01
# FETCH COMPLETION REPORT (Board Transparency Overhaul, approved by
# owner): one short, consistent summary posted after EVERY /fetch
# (text command and slash command alike) -- Data Source, Runtime, Board
# Counts (all 6 boards, PrizePicks-consistent emoji), Declined Breakdown,
# Held Back Count. Pure formatting over numbers `result` already carries
# (decline_breakdown from Objective 1, health/held_back from the board
# health block above) plus the caller's own measured runtime -- computes
# nothing new, re-ranks nothing, never touches Apex Score, Confidence,
# ALIS, or any ranking/selection logic. Posted alongside, not instead of,
# the existing Board Health Report and User Performance Report.
def _fetch_completion_report_text(result, runtime_s=None):
    health = result.get("health") or {}
    db = result.get("decline_breakdown") or {}
    held_back = result.get("held_back") or []

    L = ["\U0001F4E1 **FETCH COMPLETION REPORT**", "=" * 30, ""]

    if result.get("data_source") == "partner_api":
        L.append("\u2705 Data Source: Partner API")
    elif result.get("data_source") == "playwright":
        L.append("\u26A0\uFE0F Data Source: Playwright Fallback")
    else:
        L.append(f"Data Source: {result.get('data_source', '?')}")

    L.append(f"\u23F1 Runtime: {runtime_s}s" if runtime_s is not None else "\u23F1 Runtime: unavailable")
    L += ["", "=" * 30, "", "\U0001F4CA **Board Counts**", ""]
    for flag, label in (("top3", "Top 3"), ("elite6", "Elite6"), ("top10", "Top10"),
                         ("goblin", "Goblin"), ("demon", "Demon"), ("standard", "Standard")):
        count = health.get(f"{flag}_count")
        L.append(f"{_BOARD_EMOJI[flag]} {label}: {count if count is not None else '?'}")

    # APEX-FETCHREPORT-BOARDSTATS-2026-10-05 (Phase 9, owner): per-board
    # breakdown -- Board Inventory (props considered for this board, before
    # its own dedup/floor), Prop Distribution (by category), Board Quality
    # (average Apex Score of what actually qualified), Board Output (final
    # published count -- same number as Board Counts above, repeated here
    # for context), Legacy Picks Available. An empty board explains itself
    # inline instead of leaving a bare 0 to interpret.
    board_stats = result.get("board_stats") or {}
    if board_stats:
        L += ["", "=" * 30, "", "\U0001F3BE **Per-Board Detail**"]
        for flag, label in (("top3", "Top 3"), ("elite6", "Elite6"), ("top10", "Top10"),
                             ("goblin", "Goblin"), ("demon", "Demon"), ("standard", "Standard")):
            bs = board_stats.get(flag) or {}
            L += ["", "-" * 30, "%s **%s**" % (_BOARD_EMOJI[flag], label)]
            if bs.get("error"):
                L.append("  Unavailable this run: %s" % bs["error"])
                continue
            if "empty_msg" in bs:
                L.append("  %s" % bs["empty_msg"])
                continue
            L.append("  \U0001F3BE Board Inventory: %s prop(s) considered" % bs.get("inventory", "?"))
            dist = bs.get("distribution") or {}
            if dist:
                _dist_str = ", ".join("%s (%d)" % (c.replace("_", " ").title(), n)
                                       for c, n in sorted(dist.items(), key=lambda x: -x[1])[:6])
                L.append("  \U0001F4CA Prop Distribution: %s" % _dist_str)
            qavg = bs.get("quality_avg")
            L.append("  \U0001F3C6 Board Quality: avg Apex Score %s" % (round(qavg) if qavg is not None else "n/a"))
            L.append("  \U0001F3AF Board Output: %d published" % bs.get("output", 0))
            L.append("  \U0001F4DA Legacy Picks Available: %d" % bs.get("legacy_count", 0))
            if bs.get("output", 0) == 0:
                L.append("  \u26A0\uFE0F Empty board: no %s prop cleared its quality floor today -- "
                         "not a bug, just no qualifying prop in today's slate (see Board Inventory above)." % label)

    L += ["", "=" * 30, ""]
    if db:   # APEX-FIRSTSETLABEL-2026-11-07 (owner, "Go fit label"): first-set props are scored by apex_set_games, not declined
        L += ["\U0001F3BE " + "First set: %d props went to the first-set rules (scored there, not declined -- they compete for board spots)" % db.get('first_set_markets', 0), ""]
    L += ["\U0001F6AB **Declined Breakdown**", ""]
    if db:
        L += [f"\u2022 MORE-only Conflicts: {db.get('more_only_conflicts', 0)}",
              f"\u2022 Unknown Players: {db.get('unknown_players', 0)}",
              f"\u2022 Doubles: {db.get('doubles', 0)}",
              f"\u2022 No Odds: {db.get('no_odds', 0)}",
              f"\u2022 Missing Model Data: {db.get('missing_model_data', 0)}",
              f"\u2022 Other: {db.get('other', 0)}",
              f"Total Declined: {sum(db.values()) - db.get('first_set_markets', 0)}"]
    else:
        L.append("Unavailable this run")

    L += ["", "=" * 30, "", f"\U0001F6A7 Held Back Count: {len(held_back)}"]
    try:   # APEX-DECLINEDDATA-2026-10-22: data problems never look like "no props"
        import apex_data_health as _dhr
        L += ["", "=" * 30, ""] + _dhr.fetch_report_lines()
    except Exception as _e:
        L += ["", "\u26A0\uFE0F DATA CHECK unavailable (%s)" % type(_e).__name__]
    return "\n".join(L)


def _board_health_report_text(result):
    """APEX-BOARDHEALTH-2026-09-30: renders the Board Health Report posted
    after every successful /fetch. Pure formatting over numbers `result`
    already carries (see _fetch_publish_all_render_inner) -- computes
    nothing new, re-ranks nothing, never touches scoring/selection/
    retirement. Never raises: falls back to '?' for any missing field."""
    from datetime import datetime as _dt3

    def g(k, d="?"):
        v = result.get(k)
        return d if v is None else v

    health = result.get("health") or {}
    db = result.get("decline_breakdown") or {}
    ts = _dt3.now().strftime("%Y-%m-%d %I:%M %p CT")

    api_additions = max(0, (result.get("scraped") or 0) - (result.get("deduped_total") or 0))

    prev_board = health.get("previous_active_board")
    cur_board  = health.get("active_board")
    net        = health.get("net_change")
    net_str    = "?" if net is None else (f"+{net}" if net >= 0 else str(net))

    _tier_r   = health.get("tier_retired", 0)
    _opp_r    = health.get("opponent_retired", 0)
    _mid_r    = health.get("matchid_retired", 0)
    _fetch_r  = health.get("fetch_retired", 0)
    _unpl_r   = health.get("unplayable_retired", 0)
    _total_retired_this_build = (_tier_r if isinstance(_tier_r, int) else 0) \
        + (_opp_r if isinstance(_opp_r, int) else 0) \
        + (_mid_r if isinstance(_mid_r, int) else 0) \
        + (_fetch_r if isinstance(_fetch_r, int) else 0) \
        + (_unpl_r if isinstance(_unpl_r, int) else 0)

    L = [
        "✅ **BOARD FETCH COMPLETE**",
        "",
        f"🕒 Fetch Completed — {ts}",
        "",
        "=" * 20,
        "",
        "**📊 BOARD HEALTH**",
        "",
        f"Raw Scraped: {g('raw_total')}",
        f"Deduped: {g('deduped_total')}",
        f"API Additions: {api_additions}",
        f"Total Scraped: {g('scraped')}",
        # APEX-PARTNERAPI-2026-10-01 (API-First Fetch): shows which
        # data-acquisition path this fetch actually used -- partner-api
        # primary or Playwright fallback -- with no action required from
        # the user either way.
        "✅ Data Source: Partner API" if result.get("data_source") == "partner_api"
        else ("⚠ Data Source: Playwright Fallback" if result.get("data_source") == "playwright"
              else f"Data Source: {g('data_source')}"),
        "",
        "=" * 20,
        "",
        f"Evaluated: {g('evaluated')}",
        f"Declined: {g('declined')}",
        "",
        "Full Decline Breakdown (reconciles exactly to Declined above):",
        f"• MORE-only Conflicts: {db.get('more_only_conflicts', 0)}",
        f"• First set (sent to the first-set rules, scored there, not a real decline): {db.get('first_set_markets', 0)}",
        f"• Unknown Players: {db.get('unknown_players', 0)}",
        f"• Doubles: {db.get('doubles', 0)}",
        f"• No Odds: {db.get('no_odds', 0)}",
        f"• Missing Model Data: {db.get('missing_model_data', 0)}",
        f"• Other: {db.get('other', 0)}",
        f"  Sum: {sum(db.values()) if db else 0} "
        f"({'✅ reconciles' if db and sum(db.values()) == g('declined') else '⚠️ MISMATCH'})",
        "",
        "=" * 20,
        "",
        "**📈 BOARD CHANGE**",
        "",
        f"Previous Active Board: {prev_board if prev_board is not None else '?'}",
        f"Current Active Board: {cur_board if cur_board is not None else '?'}",
        f"Net Change: {net_str}",
        "",
        "=" * 20,
        "",
        "**🗑 RETIREMENT SUMMARY**",
        "",
        f"Missing From Latest Fetch: {_fetch_r}",
        f"Opponent Drift: {_opp_r}",
        f"Tier Drift: {_tier_r}",
        f"Match-ID Drift: {_mid_r}",
        f"Unplayable Filter: {_unpl_r}",
        f"Total Retired: {_total_retired_this_build}",
        "",
        "=" * 20,
        "",
        "✅ **Active Board**",
        "",
        f"Active Props: {cur_board if cur_board is not None else '?'}",
        "",
        "=" * 20,
        "",
        "**🏆 BOARD CONTENT**",
        "",
        f"{_BOARD_EMOJI['top3']} Top 3: {health.get('top3_count', '?')}",
        f"{_BOARD_EMOJI['elite6']} Elite6: {health.get('elite6_count', '?')}",
        f"{_BOARD_EMOJI['top10']} Top10: {health.get('top10_count', '?')}",
        f"{_BOARD_EMOJI['goblin']} Goblin: {health.get('goblin_count', '?')}",
        f"{_BOARD_EMOJI['demon']} Demon: {health.get('demon_count', '?')}",
        f"{_BOARD_EMOJI['standard']} Standard: {health.get('standard_count', '?')}",
    ]

    if result.get("freshness_line"):
        L += ["", "=" * 20, "", "**🔄 SYNC INFORMATION**", "",
              "Last Successful PrizePicks Sync:", "", result["freshness_line"]]

    warnings = []
    for label, key in (("Elite6", "elite6"), ("Goblin", "goblin"),
                        ("Demon", "demon"), ("Standard", "standard")):
        if health.get(f"{key}_count") == 0:
            msg = health.get(f"{key}_empty_msg") or f"No {label} props cleared the quality bar today."
            warnings.append(f"⚠ {label} Board Empty\n\n{msg}")

    if isinstance(cur_board, int) and _total_retired_this_build > cur_board:
        # Name the dominant reason so "why" is never a separate lookup.
        _reasons = {"fetch": _fetch_r, "opponent drift": _opp_r, "tier drift": _tier_r,
                    "match-ID drift": _mid_r, "the unplayable filter": _unpl_r}
        _dominant = max(_reasons, key=lambda k: _reasons[k] if isinstance(_reasons[k], int) else 0)
        if _dominant == "fetch":
            _why = "Most were not confirmed by the latest fetch."
        else:
            _why = f"Most were retired by {_dominant} ({_reasons[_dominant]} props)."
        warnings.append(
            f"⚠ Large Board Reduction\n\n{_total_retired_this_build} props were retired.\n\n{_why}"
        )

    if warnings:
        L += ["", "=" * 20, "", "**⚠️ WARNINGS**", ""]
        for w in warnings:
            L += [w, ""]

    if result.get("health_history_path"):
        L += ["", f"_Logged to {result['health_history_path']}_"]
    elif result.get("health_history_error"):
        L += ["", f"_⚠️ Fetch history log failed: {result['health_history_error']}_"]

    return "\n".join(L)


async def _handle_fetch_command(message, cmd):
    if cmd not in _FETCH_MODES:
        await message.channel.send(
            "⚠️ Unknown `/fetch` option. Use `/fetch`, `/fetch evaluate`, or `/fetch archive`. "
            "For scrape-only diagnostics with no picks, use `/board`."
        )
        return

    if not _fetch_busy_claim():   # APEX-PPTHROTTLE-2026-10-16
        await message.channel.send("⏳ A /fetch is already running -- results post when it finishes. Not starting a second one.")
        return
    mode = _FETCH_MODES[cmd]
    label = {"publish_all": "full evaluation — publishes every qualifying pick", "archive": "board + archive"}
    await message.channel.send(
        f"📡 **/fetch** — launching full Tennis board scrape (all 11 tabs, "
        f"{label.get(str(mode), 'full evaluation')})… this can take several minutes."
    )

    _t0 = time.time()
    _FETCH_RUNNING["on"] = True   # APEX-FETCHCANCEL-2026-10-12 (typed /fetch path)
    core.clear_fetch_cancel()
    try:
        if mode == "archive":
            result = await asyncio.to_thread(_fetch_archive_render)
        else:
            result = await asyncio.to_thread(_fetch_publish_all_render)
    except Exception as e:
        await message.channel.send(f"❌ **Board Fetch Failed**\n{_friendly_error(e)}")
        return
    finally:
        _FETCH_RUNNING["on"] = False
        _FETCH_RUNNING["claimed"] = 0   # APEX-PPTHROTTLE-2026-10-16
    if result.get("sa365_refused"):   # APEX-FETCHSTATUS-2026-10-23: dead key -- 🛑 on top of #general + DATA CHECK
        import apex_sa365_guard as _sa365g
        _done = result.get("cancel_evaluated", 0); _tot = result.get("cancel_total", 0)
        _r_text = (_sa365g.REFUSED_TEXT + f"\nStopped after {_done} of {_tot} props ({_runtime_s:.0f}s). "
                   "Nothing was published; the boards you have are unchanged.")
        await asyncio.to_thread(_fetch_status_edit, _r_text)
        await asyncio.to_thread(core.send_discord, "general", _sa365g.REFUSED_TEXT)
        await _edit_status(_r_text)
        try:
            import apex_data_health as _adh
            await asyncio.to_thread(_adh.alert_sa365)
        except Exception as e:
            core.log("Discord", f"/fetch: SA365 alert skipped: {type(e).__name__}: {e}")
        return

    if result.get("cancelled"):   # APEX-FETCHCANCEL-2026-10-12
        await message.channel.send("🛑 **/fetch cancelled** by request. Nothing was published and the active board is unchanged.")
        return
    _runtime_s = round(time.time() - _t0, 1)

    if not result.get("ok"):
        await message.channel.send(f"❌ **Board Fetch Failed**\nReason: {result.get('error', 'unknown error')}")
        return

    per_tab = result.get("per_tab") or {}
    per_tab_lines = "\n".join(f"  {k}: {v}" for k, v in per_tab.items()) or "  (not available)"

    if mode == "archive":
        lines = [
            "✅ **Board Fetch Complete**",
            "",
            "**Board Summary**",
            f"Total Raw Props: {result['raw_total']}",
            f"Total Deduped Props: {result['deduped_total']}",
            "",
            "**Props Per Tab**",
            per_tab_lines,
        ]
        if result.get("archive_path"):
            lines += ["", f"📦 Board snapshot archived: `{result['archive_path']}`"]
        elif result.get("archive_error"):
            lines += ["", f"⚠️ Archive failed: {result['archive_error']}"]
        for chunk in _bb_chunk_text("\n".join(lines)):
            await message.channel.send(chunk)
        return

    if result.get("evaluate_error"):
        lines = [
            "⚠️ **Board Fetch — Evaluation Failed**",
            f"Reason: {result['evaluate_error']}",
            "",
            "**Board Summary**",
            f"Total Raw Props: {result['raw_total']}",
            f"Total Deduped Props: {result['deduped_total']}",
        ]
        for chunk in _bb_chunk_text("\n".join(lines)):
            await message.channel.send(chunk)
        return

    for chunk in _bb_chunk_text(_board_health_report_text(result)):
        await message.channel.send(chunk)

    # APEX-FETCHRELIABILITY-2026-10-01: Health Check Redesign -- the
    # Developer Health report above (scrape/eval mechanics) is now
    # followed by a separate User Performance Report (board records,
    # 70+ hit rate, hot/avoid categories, what Apex learned). Posted as
    # its own message so either one can be read on its own. Fail-open:
    # build_user_performance_report() never raises, so a problem here
    # can never turn a successful /fetch into a reported failure.
    for chunk in _bb_chunk_text(await asyncio.to_thread(_user_performance_report)):
        await message.channel.send(chunk)

    # APEX-UPRV3-2026-10-01 Objective #10: Fetch Completion Report --
    # posted last so it's a quick after-action summary of this one run
    # (data source, runtime, board counts, declines, held-back count).
    for chunk in _bb_chunk_text(_fetch_completion_report_text(result, _runtime_s)):
        await message.channel.send(chunk)


@client.event
async def on_message(message):
    # Never react to the bot's own messages — avoids an infinite loop
    if message.author == client.user:
        return

    channel_name = getattr(message.channel, "name", "")

    try:
        if channel_name == SLIPS_CHANNEL_NAME:
            await handle_slips_message(message, message.content)

        elif channel_name == SLIP_RECS_CHANNEL_NAME:   # APEX-SLIPSCHANNEL-2026-10-15: typed /slips
            if (message.content or "").strip().lower() == "/slips":
                await _send_slips_on_demand(message.channel.send)
            elif (message.content or "").strip().lower() == "/slipsv2":   # APEX-SLIPSV2-2026-10-22
                await _send_slipsv2(message.channel.send)
            elif (message.content or "").strip().lower() == "/besttime":   # APEX-BESTTIME-2026-10-31
                await _send_besttime(message.channel.send)
            elif (message.content or "").strip().lower() == "/counters":   # APEX-BOARDCOUNTERS-2026-10-31
                await _send_counters(message.channel.send)

        elif channel_name == LEARNING_CHANNEL_NAME:
            await handle_learning_message(message, message.content)

        elif channel_name == GENERAL_CHANNEL_NAME:
            # v7.9.7 fix (item 3): "!cancel" is the one text command
            # #general responds to — everything else stays image-only
            # per handle_general_message's existing design.
            _txt = (message.content or "").strip().lower()
            if _txt == "!cancel":
                if _FETCH_RUNNING["on"]:   # APEX-FETCHCANCEL-2026-10-12
                    _request_fetch_cancel_all()
                    await message.channel.send("🛑 Cancel requested — the running /fetch stops after the current step; nothing will be published.")
                else:
                    await handle_cancel_command(message)
            elif _txt == "/fetch" or _txt.startswith("/fetch "):
                # APEX-DISCORD-FETCH-2026-09-28: on-demand full Tennis
                # board scrape + evaluation, publishes every qualifying
                # pick. Matched before the generic _BB_COMMANDS check
                # below since "/fetch evaluate"/"/fetch archive" are
                # multi-word and wouldn't match an exact-equality lookup
                # there anyway.
                await _handle_fetch_command(message, _txt)
            elif _txt == "/slips":                       # APEX-SLIPSCHANNEL-2026-10-15
                await _send_slips_on_demand(message.channel.send)
            elif _txt == "/slipsv2":                     # APEX-SLIPSV2-2026-10-22
                await _send_slipsv2(message.channel.send)
            elif _txt == "/besttime":                    # APEX-BESTTIME-2026-10-31
                await _send_besttime(message.channel.send)
            elif _txt == "/counters":                    # APEX-BOARDCOUNTERS-2026-10-31
                await _send_counters(message.channel.send)
            elif _txt == "/board":
                # APEX-FETCH-WORKFLOW-2026-09-29: scrape-only diagnostics,
                # no Gate 11 / odds / engine / picks.
                await _handle_board_command(message)
            elif _txt in _BB_COMMANDS or _txt in _BB_TYPE_COMMANDS or _txt in _BB_EXTRA_COMMANDS or _txt == "/tieraudit":
                # APEX-BB-002b: on-demand Best Bets. Reads the latest evaluated
                # board from the pick log -- never re-evaluates, never blocks
                # the event loop (runs in a thread).
                await _handle_bestbets_command(message, _txt)
            else:
                await handle_general_message(message)

    except Exception as e:
        # Never let a single bad message crash the bot — log and reply
        print(f"[DiscordBot] ERROR handling message in #{channel_name}: {e}")
        try:
            await message.channel.send(f"⚠️ Error processing that message: {e}")
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# #slips-pnl handler
# ──────────────────────────────────────────────────────────────

async def handle_slips_message(message, text):
    # Slip-confirmation screenshot support (this session): a picture
    # of the actual placed slip, not typed text. Deliberately does NOT
    # log anything directly -- unlike #general's board screenshots
    # (safe to evaluate speculatively), this is a claim about real
    # money already staked, so it lands in Mission Control's Slip
    # Review queue for the operator to confirm or correct before
    # anything touches slip_log.json. Checked before text classification
    # so an image with a caption doesn't fall through to "casual chat."
    image_attachments = [
        a for a in message.attachments
        if a.content_type and a.content_type.split(";")[0].strip() in IMAGE_CONTENT_TYPES
    ]
    if image_attachments:
        # 2026-08-15 fix (multi-screenshot): previously only
        # image_attachments[0] was processed and the rest were silently
        # ignored. Discord allows up to 10 attachments per message —
        # process every one, sequentially.
        for _slip_attachment in image_attachments:
            await handle_slip_screenshot(message, _slip_attachment)
        return

    # APEX-SLIPCONFIRM-2026-10-11: "#3 25", "#3 bonus 25", "#3 ok", "#3 skip"
    _m = re.match(r"^\s*#(\d+)\s+(?:(bonus)\s+)?(?:\$?(\d+(?:\.\d{1,2})?)|(ok|skip|cancel))\s*$", text or "", re.IGNORECASE)
    if _m:
        await _confirm_slip_from_discord(message, int(_m.group(1)), bool(_m.group(2)),
                                         _m.group(3), (_m.group(4) or "").lower())
        return

    # APEX-DISCOUNT-2026-10-16: "reese hit" / "#7 reese miss" grades a pick that has
    # no stats feed (another sport / discount play). Only acts when it matches one.
    if await _hand_grade_from_discord(message, text):
        return

    kind = classify_slip_message(text)
    if kind is None:
        return  # casual chat — ignore

    dates = core.get_system_dates()

    if kind == "entry":
        response = core.parse_slip_entry(text, dates)
    else:
        weights  = core.load_weights()
        response = core.parse_slip_result(text, dates, weights)

    await message.channel.send(response)


async def _hand_grade_from_discord(message, text):
    import apex_slip_grader as _sg
    m = _sg.HAND_RE.match(text or "")
    if not m:
        return False
    slip_id = None
    if m.group(1):
        item = next((i for i in intake._load_slip_review() if i.get("id") == int(m.group(1))), None)
        slip_id = (item or {}).get("slip_id")
        if not slip_id:
            await message.channel.send(f"⚠️ Slip #{m.group(1)} isn't logged yet -- reply `#{m.group(1)} ok` first.")
            return True
    res = await client.loop.run_in_executor(None, lambda: _sg.hand_grade(m.group(2), m.group(3), slip_id))
    if res.get("ask"):
        await message.channel.send("⚠️ " + res["ask"])
        return True
    if not res.get("graded"):
        return False
    g = res["graded"]
    word = {"WIN": "hit ✅", "LOSS": "missed ❌", "PUSH": "push ➖", "VOID": "void ⚪"}[g[0]["status"]]
    await message.channel.send("🏷️ Graded %s %s %s %s as **%s** on %d slip%s." % (
        g[0].get("player"), g[0].get("direction"), g[0].get("line"), g[0].get("stat_label") or g[0].get("stat_type"),
        word, len({e.get("slip_id") for e in g}), "" if len({e.get("slip_id") for e in g}) == 1 else "s"))
    cards = await client.loop.run_in_executor(None, lambda: _sg.run(cards=True))
    await _post_slip_grade_cards(message.channel, cards or [])
    return True


_SLIP_DETAILS_FILE = os.path.join("data", "logs", "slip_details_pending.json")


def _pop_slip_details(payout):
    """APEX-SLIPBONUS-2026-10-11: a Details screenshot that arrived before its legs
    screenshot (kept 24h), matched by the to-win amount."""
    if payout is None:
        return None
    try:
        with open(_SLIP_DETAILS_FILE) as fh:
            items = json.load(fh)
    except Exception:
        return None
    now = time.time()
    items = [d for d in items if now - d.get("t", 0) < 86400]
    hit = next((d for d in items if abs(float(d.get("potential_payout") or -1) - float(payout)) < 0.01), None)
    if hit:
        items.remove(hit)
    try:
        with open(_SLIP_DETAILS_FILE, "w") as fh:
            json.dump(items, fh)
    except Exception:
        pass
    return hit


async def _attach_slip_details(message, extracted):
    """APEX-SLIPBONUS-2026-10-11: a Details/payout screenshot (no legs) fills in the
    entry amount of the pending slip with the same to-win amount, then logs it. If
    the legs screenshot hasn't arrived yet, it's kept for 24h and matched later."""
    payout = float(extracted["potential_payout"])
    stake = extracted.get("stake")
    bonus = bool(extracted.get("is_bonus"))
    amt = extracted.get("bonus_amount") or stake
    pend = [i for i in intake.slip_review_queue_view() if i.get("potential_payout") is not None
            and abs(float(i["potential_payout"]) - payout) < 0.01]
    if not pend:
        try:
            with open(_SLIP_DETAILS_FILE) as fh:
                items = json.load(fh)
        except Exception:
            items = []
        items.append({"t": time.time(), "potential_payout": payout, "stake": stake,
                      "is_bonus": bonus, "bonus_amount": amt})
        with open(_SLIP_DETAILS_FILE, "w") as fh:
            json.dump(items, fh)
        await message.channel.send(f"🧾 Got the details for an entry to win ${payout:.2f}"
                                   + (f" (bonus ${float(amt):.2f})" if bonus and amt else "")
                                   + " -- send the legs screenshot and I'll log them together.")
        return
    item = pend[-1]
    await _confirm_slip_from_discord(message, item["id"], bonus, amt if amt is not None else None,
                                     "" if amt is not None else "ok")


async def _confirm_slip_from_discord(message, review_id, bonus, amount, word):
    """APEX-SLIPCONFIRM-2026-10-11: confirm or discard a Slip Review item from
    #slips-pnl. Uses the same intake.confirm_slip_review() Mission Control uses
    (same parse_slip_entry validation and logging)."""
    q = intake.slip_review_queue_view()
    item = next((i for i in q if i.get("id") == review_id), None)
    if not item:
        await message.channel.send(f"⚠️ Slip #{review_id} isn't pending (already logged or discarded).")
        return
    if word in ("skip", "cancel"):
        try:
            intake.reject_slip_review(review_id)
        except Exception:
            pass
        await message.channel.send(f"🗑️ Slip #{review_id} discarded -- nothing logged.")
        return
    # re-read stat labels the slip log learned after this slip was queued (e.g. Fantasy Score)
    _fixed = False
    for l in item.get("legs", []):
        if l.get("bet_type", "prop") != "moneyline" and not l.get("stat") and l.get("stat_label_read"):
            l["stat"] = intake._guess_stat_key(l.get("stat_label_read"))
            _fixed = _fixed or bool(l["stat"])
    if _fixed:
        _q = intake._load_slip_review()
        for _i in _q:
            if _i.get("id") == review_id:
                _i["legs"] = item["legs"]
        intake._save_slip_review(_q)
    bad = [l for l in item.get("legs", []) if l.get("bet_type", "prop") != "moneyline" and not l.get("stat")
           and not intake.leg_is_manual(l)]   # APEX-DISCOUNT-2026-10-16: other-sport legs log, graded by hand
    if bad:
        await message.channel.send("⚠️ Slip #%d has a leg the slip log can't record yet (%s). Log it in Mission Control for now."
                                   % (review_id, ", ".join("%s %s" % (l.get("player"), l.get("stat_label_read")) for l in bad)))
        return
    stake = None
    if amount is not None:
        stake = 0.0 if bonus else float(amount)
    elif item.get("stake") is None:
        await message.channel.send(f"⚠️ Slip #{review_id} needs the entry amount: reply `#{review_id} 25` or `#{review_id} bonus 25`.")
        return
    res = intake.confirm_slip_review(review_id, stake=stake)
    if not res or not res.get("ok", True):
        await message.channel.send(f"⚠️ Slip #{review_id} not logged: {(res or {}).get('error', 'unknown error')}")
        return
    note = f" (bonus bet ${float(amount):.2f} -- logged as $0 of your money)" if bonus and amount else ""
    await message.channel.send(f"✅ Slip #{review_id} logged{note}.\n" + str(res.get("response") or ""))


async def handle_slip_screenshot(message, attachment):
    """
    Downloads the slip screenshot, reads it via
    core.extract_slip_screenshot(), and enqueues the result into
    Mission Control's Slip Review queue -- same dedup-hash guard
    #general already uses, same download/error handling, different
    destination (a review queue, not a live evaluation).
    """
    await message.add_reaction("👀")

    try:
        image_bytes = await attachment.read()
        image_b64   = base64.b64encode(image_bytes).decode("utf-8")
        media_type  = attachment.content_type.split(";")[0].strip()
    except Exception as e:
        await message.channel.send(f"⚠️ Could not download the image: {e}")
        return

    image_hash = hashlib.md5(image_bytes).hexdigest()
    now = time.time()
    for h in list(_RECENT_IMAGE_HASHES.keys()):
        if now - _RECENT_IMAGE_HASHES[h] > _DEDUP_WINDOW_SECONDS:
            del _RECENT_IMAGE_HASHES[h]
    if image_hash in _RECENT_IMAGE_HASHES:
        await message.channel.send("⚠️ Already read — check Mission Control → Pick Intake → Slip Review (duplicate screenshot detected).")
        return
    _RECENT_IMAGE_HASHES[image_hash] = now

    extracted = core.extract_slip_screenshot(image_b64, media_type)
    # APEX-SLIPBONUS-2026-10-11: a bonus/promo entry costs $[amount] of your own money --
    # log the stake as 0 so P&L stays honest (the bonus amount is kept in the note).
    if extracted and extracted.get("is_bonus"):
        extracted["bonus_amount"] = extracted.get("bonus_amount") or extracted.get("stake")
        extracted["stake"] = 0.0
    # A Details / payout screen with no legs: attach it to the matching slip.
    if extracted and not extracted.get("legs") and extracted.get("potential_payout") is not None:
        await _attach_slip_details(message, extracted)
        return
    if extracted and extracted.get("legs") and extracted.get("stake") is None:
        _d = _pop_slip_details(extracted.get("potential_payout"))
        if _d:
            extracted["stake"] = 0.0 if _d.get("is_bonus") else _d.get("stake")
            extracted["is_bonus"] = bool(_d.get("is_bonus"))
            extracted["bonus_amount"] = _d.get("bonus_amount")
    if not extracted or not extracted.get("legs"):
        await message.channel.send(
            "⚠️ Could not read the slip clearly. "
            "Please send a clearer screenshot, or type it manually:\n"
            "`Player MORE/LESS LINE STAT | Player2 MORE/LESS LINE STAT`"
        )
        return

    legs = extracted.get("legs", [])
    stake = extracted.get("stake")
    payout = extracted.get("potential_payout")
    stake_str = f"${stake:.2f}" if stake is not None else "not read"
    if extracted.get("is_bonus"):   # APEX-SLIPBONUS-2026-10-11
        stake_str = "bonus $%.2f ($0 of your money)" % float(extracted.get("bonus_amount") or 0)
    payout_str = f"${payout:.2f}" if payout is not None else "not read"

    # SLIP-AUTOMATION-2026-09-27, priority 4 of 4: try the auto-validation
    # layer before falling back to the manual review queue. Every leg has
    # to cross-check cleanly against apex_engine_picks_log_v1.csv (what
    # Apex itself evaluated) or this returns None and nothing changes
    # below -- same enqueue_slip_review() path as before this change.
    auto = intake.try_auto_confirm_slip(extracted, source="discord_slips_channel")
    if auto and auto.get("ok"):
        await message.channel.send(
            f"✅ Slip auto-confirmed — {len(legs)} leg{'s' if len(legs)!=1 else ''}, "
            f"entry {stake_str}, to win {payout_str}. Every leg matched an evaluated "
            f"pick, so this was logged automatically (no Slip Review needed).\n"
            + auto["response"]
        )
        return

    review_id = intake.enqueue_slip_review(extracted, source="discord_slips_channel")
    # APEX-SLIPCONFIRM-2026-10-11 (owner: "I don't want to have to confirm via
    # Mission Control"): confirm right here with a one-line reply. Mission Control's
    # Slip Review still works, it's just no longer required.
    _leg_lines = []
    _unsupported = []
    for _l in legs:
        _stat_key = intake._guess_stat_key(_l.get("stat_label"))
        if _l.get("bet_type", "prop") != "moneyline" and not _stat_key:
            _unsupported.append(_l)
        # APEX-DISCOUNT-2026-10-16: 🟢 Goblin / 🔴 Demon (PrizePicks' colors), 🏷️ Discount
        _leg_lines.append(f"• {_l.get('player')} {(_l.get('direction') or 'MORE').upper()} {_l.get('line')} {_l.get('stat_label') or ''}"
                          + {"goblin": " 🟢 Goblin", "demon": " 🔴 Demon", "red_demon": " 🔴 Demon"}.get(_l.get("prop_type") or "", "")
                          + (" 🏷️ Discount" if _l.get("discount") else "")
                          + ((" (%s)" % _l.get("sport")) if (_l.get("sport") or "TENNIS").upper() != "TENNIS" else ""))
    _ask = (f"Reply **`#{review_id} 25`** with the entry amount to log it "
            f"(use **`#{review_id} bonus 25`** for a bonus bet, so it counts as $0 of your money), "
            f"or **`#{review_id} skip`** to discard.")
    if stake is not None:
        _ask = f"Reply **`#{review_id} ok`** to log it as read, or **`#{review_id} skip`** to discard."
    _warn = ""
    _manual = [l for l in _unsupported if l.get("stat_label") and l.get("line") is not None]
    _nolabel = [l for l in _unsupported if l not in _manual]
    if _manual:   # APEX-DISCOUNT-2026-10-16: logged with the slip, graded by you
        _nm = (re.sub(r"\(.*?\)", " ", str(_manual[0].get("player") or "name")).split() or ["name"])[-1].lower()
        _warn += ("\n🏷️ " + ", ".join(f"{l.get('player')} {l.get('stat_label')}" for l in _manual) +
                  f" has no stats feed, so you grade it: after the game reply `{_nm} hit`, `{_nm} miss` or `{_nm} push`.")
    if _nolabel:
        _warn += ("\n⚠️ Can't log yet: " + ", ".join(f"{l.get('player')} (stat not readable)" for l in _nolabel) +
                  " -- type the slip by hand or log it in Mission Control.")
    await message.channel.send(
        f"📸 Slip #{review_id} read — {len(legs)} leg{'s' if len(legs)!=1 else ''}, "
        f"entry {stake_str}, to win {payout_str}.\n" + "\n".join(_leg_lines) + "\n" + _ask + _warn
    )


# ──────────────────────────────────────────────────────────────
# #learning-channel handler
# ──────────────────────────────────────────────────────────────

async def handle_learning_message(message, text):
    parsed = parse_approval_command(text)
    if parsed is None:
        return  # casual chat — ignore

    decision, proposal_id = parsed
    dates    = core.get_system_dates()
    response = core.apply_proposal_decision(proposal_id, decision, dates)
    await message.channel.send(response)


# ──────────────────────────────────────────────────────────────
# !cancel command handler
# ──────────────────────────────────────────────────────────────

async def handle_cancel_command(message):
    """
    v7.9.7 fix (item 3). See the honest limitation note above
    _current_task_label — this marks the current task abandoned so its
    result gets discarded when it eventually finishes, rather than
    truly killing an in-flight thread (Python can't do that here).
    """
    global _current_task_cancelled

    if _current_task_label is None:
        await message.channel.send("ℹ️ Nothing is currently running to cancel.")
        return

    _current_task_cancelled = True
    await message.channel.send(
        f"🛑 Cancel requested for: **{_current_task_label}**\n"
        f"Note: the background call may still finish on its own, but its "
        f"result will be discarded — no pick will post for it. ✅"
    )


# ──────────────────────────────────────────────────────────────
# #general handler — image intake only
# ──────────────────────────────────────────────────────────────

async def handle_general_message(message):
    """
    Only fires on image attachments sent to #general.
    Text messages in #general are always ignored.

    Workflow:
    1. Detect image attachment(s)
    2. Download and base64-encode EACH image (up to Discord's 10-attachment cap)
    3. Call Claude vision to extract player/opponent/lines ONLY
       (surface, tournament, tour are never read from the image)
    4. classify_image_content() decides: moneyline or props
    5. Route to evaluate_moneyline_screenshot() or evaluate_manual_prop()
       — both confirm tournament context before evaluating
    """
    # Only process messages that have image attachments
    image_attachments = [
        a for a in message.attachments
        if a.content_type and a.content_type.split(";")[0].strip() in IMAGE_CONTENT_TYPES
    ]
    if not image_attachments:
        return  # text-only message in #general — ignore

    await message.add_reaction("👀")  # acknowledge receipt visibly

    # 2026-08-15 fix (multi-screenshot): previously only the FIRST image
    # was processed and any others in the same message were silently
    # ignored — no error, no warning. Discord allows up to 10
    # attachments per message; process each one sequentially. A bad or
    # unreadable image skips to the next instead of abandoning the rest.
    # !cancel semantics unchanged: it cancels whichever image's
    # evaluation is currently running.
    total = len(image_attachments)
    for _idx, _attachment in enumerate(image_attachments, 1):
        if total > 1:
            await message.channel.send(f"🗂 Screenshot **{_idx}/{total}**…")
        await _process_general_image(message, _attachment)


async def _process_general_image(message, attachment):
    """Per-image processing for #general: download → dedup → vision
    parse → classify → route. Extracted verbatim from
    handle_general_message (2026-08-15) so multiple attachments in one
    message can each be processed; a `return` here skips only this
    image, not the whole message."""
    # Download and base64-encode this image
    try:
        image_bytes = await attachment.read()
        image_b64   = base64.b64encode(image_bytes).decode("utf-8")
        media_type  = attachment.content_type.split(";")[0].strip()
    except Exception as e:
        await message.channel.send(f"⚠️ Could not download the image: {e}")
        return

    # v7.9.7 fix (item 12): dedup check — hash the raw bytes and compare
    # against recent hashes before spending a vision-model call on it.
    # Purges expired entries opportunistically rather than needing a
    # separate cleanup task.
    image_hash = hashlib.md5(image_bytes).hexdigest()
    now = time.time()
    for h in list(_RECENT_IMAGE_HASHES.keys()):
        if now - _RECENT_IMAGE_HASHES[h] > _DEDUP_WINDOW_SECONDS:
            del _RECENT_IMAGE_HASHES[h]
    if image_hash in _RECENT_IMAGE_HASHES:
        await message.channel.send("⚠️ Already evaluated — see #picks (duplicate screenshot detected).")
        return
    _RECENT_IMAGE_HASHES[image_hash] = now

    # Ask Claude vision to extract ONLY player/opponent/lines
    # v7.9.7 (this session): switched from Haiku to Sonnet, same
    # reasoning as the props-side fix in openclaw_core.py — this is
    # where the Pegula/Peguia misread actually happened.
    # APEX-P0-2026-09-28: this vision call was previously invoked directly
    # on the Discord asyncio event loop -- a synchronous requests.post()
    # inside an async function with no executor boundary, blocking
    # heartbeats for the full HTTP call duration (confirmed root cause of
    # 7 of 9 "heartbeat blocked" freeze incidents on 2026-09-27, via
    # discord.py's own "Loop thread traceback" pointing directly at this
    # call site -- openclaw_core.py call_model(), line 737). Now wrapped
    # in run_in_executor, identical to the pattern already used a few
    # lines below for evaluate_moneyline_screenshot/evaluate_manual_prop.
    loop = asyncio.get_event_loop()
    parse_result = await loop.run_in_executor(
        None,
        lambda: core.call_model(
            core.MODEL_SONNET,
            core.SCREENSHOT_PARSE_PROMPT,
            [{"type": "image",
              "source": {"type": "base64",
                         "media_type": media_type,
                         "data": image_b64}},
             {"type": "text",
              "text": "Extract player names, opponent names, and lines from this screenshot."}],
            temperature=0.1,
            max_tokens=800,
        )
    )

    if not parse_result:
        await message.channel.send(
            "⚠️ Could not read the screenshot. "
            "Please send a clearer image or type the prop manually:\n"
            "`Player MORE/LESS LINE STAT`"
        )
        return

    # Parse the JSON the vision model returned
    try:
        clean       = re.sub(r"```json|```", "", parse_result).strip()
        parsed_data = json.loads(clean)
    except Exception:
        await message.channel.send(
            "⚠️ Screenshot could not be parsed. "
            "Please type the prop manually: `Player MORE/LESS LINE STAT`"
        )
        return

    # Classify: moneyline or props
    image_type = classify_image_content(parsed_data)

    dates   = core.get_system_dates()
    weights = core.load_weights()

    if image_type == "moneyline":
        player        = parsed_data.get("player", "")
        opponent      = parsed_data.get("opponent", "")
        player_odds   = parsed_data.get("player_odds", "")
        opponent_odds = parsed_data.get("opponent_odds", "")

        if not player or not opponent:
            await message.channel.send(
                "⚠️ Could not read both player names from the moneyline screen. "
                "Please type manually:\n"
                "`python3 openclaw_core.py --moneyline \"Player1 | Player2 | surface | tour\"`"
            )
            return

        await message.channel.send(
            f"📡 Reading moneyline: **{player} vs {opponent}**\n"
            f"Confirming tournament context...\n"
            f"(type `!cancel` to abandon this if it's taking too long)"
        )
        # v7.9.7 fix (item 3): track this task's label and reset the
        # cancel flag before starting, so !cancel has something to
        # reference and isn't left over from a previous task.
        global _current_task_label, _current_task_cancelled
        _current_task_label     = f"moneyline: {player} vs {opponent}"
        _current_task_cancelled = False

        # Run in a background thread so Discord's async event loop
        # stays free to send heartbeats during the evaluation.
        # Previously this blocked the main thread for 5+ minutes,
        # causing "heartbeat blocked" disconnects.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: core.evaluate_moneyline_screenshot(
                player, opponent, player_odds, opponent_odds, dates
            )
        )

        if _current_task_cancelled:
            await message.channel.send(
                f"🛑 Result for **{player} vs {opponent}** discarded — cancelled by request."
            )
        _current_task_label = None

    elif image_type == "props":
        first_player = parsed_data[0].get("player", "?") if parsed_data else "?"
        await message.channel.send(
            f"📡 Reading props for **{first_player}**\n"
            f"Confirming tournament context...\n"
            f"(type `!cancel` to abandon this if it's taking too long)"
        )
        _current_task_label     = f"props: {first_player}"
        _current_task_cancelled = False

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: core.evaluate_manual_prop(
                user_input=None,
                dates=dates,
                weights=weights,
                image_data=image_b64,
                media_type=media_type,
            )
        )

        if _current_task_cancelled:
            await message.channel.send(
                f"🛑 Result for **{first_player}** props discarded — cancelled by request."
            )
        _current_task_label = None

    else:
        await message.channel.send(
            "⚠️ Could not determine whether this is a moneyline or props screenshot.\n"
            "For props: send a PrizePicks player card.\n"
            "For moneyline: send a sportsbook odds screen with two players and odds."
        )


# ──────────────────────────────────────────────────────────────
# APEX-SLASHCMDS-2026-09-29: real Discord application (slash) commands.
# Thin wrappers around the exact same render functions the text
# commands above already use -- zero duplicated logic, so a /fetch
# typed as text and a /fetch picked from the slash menu run identically.
# Every one of these must respond within Discord's 3-second interaction
# window, so each defers immediately, does the real (slow) work in a
# thread via asyncio.to_thread, then sends the result as one or more
# followups (a deferred interaction's followup has no 3-second limit).
# ──────────────────────────────────────────────────────────────

async def _slash_send_chunks(interaction, text):
    chunks = _bb_chunk_text(text) if text else ["(no output)"]
    use_channel = False
    for chunk in chunks:
        if not use_channel:
            try:
                await interaction.followup.send(chunk)
                continue
            except Exception as e:
                # APEX-FETCHREPLY-2026-10-19 (owner, "Go"): Discord rejects a slash
                # reply after 15 minutes; a long /fetch then left the channel it was typed
                # in empty. Post the rest straight to that channel instead.
                ch = getattr(interaction, "channel", None)
                if ch is None:
                    raise
                core.log("Discord", f"interaction reply rejected ({type(e).__name__}); posting to #{getattr(ch, 'name', '?')} instead")
                use_channel = True
        await interaction.channel.send(chunk)


# ================================================================ APEX-FETCHCANCEL-2026-10-12
# owner, 2026-10-12: "Cancel this fetch, then create a cancel button".
# One cancel path for all three triggers (the red button under the /fetch
# launch message, the /cancel slash command, and the old typed !cancel):
# set the core stop flag; the running fetch stops at its next checkpoint
# (between PrizePicks tabs while scraping, between props while evaluating)
# and reports what it had done. Nothing is published after a cancel.
def _request_fetch_cancel_all():
    global _current_task_cancelled
    core.request_fetch_cancel()
    _current_task_cancelled = True


class _FetchCancelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=1800)   # 30 min, longer than any fetch

    @discord.ui.button(label="Cancel fetch", style=discord.ButtonStyle.danger, emoji="🛑")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _FETCH_RUNNING["on"]:
            await interaction.response.send_message("ℹ️ No /fetch is running right now.", ephemeral=True)
            return
        _request_fetch_cancel_all()
        button.disabled = True
        button.label = "Cancelling…"
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass
        try:
            await interaction.followup.send("🛑 Cancel requested — the fetch stops after the current step and nothing will be published.")
        except Exception:
            pass

    def disable(self):
        for item in self.children:
            item.disabled = True
        self.stop()


@tree.command(name="cancel", description="Stop the running /fetch after its current step — nothing gets published")
async def slash_cancel(interaction: discord.Interaction):
    if not _FETCH_RUNNING["on"]:
        await interaction.response.send_message("ℹ️ No /fetch is running right now.")
        return
    _request_fetch_cancel_all()
    await interaction.response.send_message("🛑 Cancel requested — the fetch stops after the current step (a few seconds to a minute) and nothing will be published.")


# APEX-SETKEY-2026-10-23 (owner, "Go" 2026-10-23): server owner pastes a new
# SportsAPI365 key from his phone. The key goes in a pop-up box (never shown in the
# channel), is tested live first, and is saved only if SportsAPI365 answers 200; the old
# file is kept as sportapi365_key.txt.bak_<time>. The key is never logged or echoed.
SA365_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sportapi365_key.txt")


def _setkey_test_and_save(key):
    import urllib.request, urllib.error, shutil
    req = urllib.request.Request("https://api.sportsapi365.com/v1/tennis/extend/api/events/live", headers={
        "X-Gravitee-Api-Key": key, "Accept": "application/json", "User-Agent": "Mozilla/5.0 (ApexStats)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        return f"❌ Could not reach SportsAPI365 ({type(e).__name__}) -- nothing changed. Try again in a minute."
    if code != 200:
        return f"❌ That key was refused ({code}) -- nothing changed."
    try:
        nl = ""
        if os.path.exists(SA365_KEY_FILE):
            with open(SA365_KEY_FILE) as fh:
                nl = "\n" if fh.read().endswith("\n") else ""
            shutil.copy2(SA365_KEY_FILE, SA365_KEY_FILE + ".bak_" + time.strftime("%Y%m%d_%H%M%S"))
        with open(SA365_KEY_FILE, "w") as fh:
            fh.write(key + nl)
        os.chmod(SA365_KEY_FILE, 0o600)
    except Exception as e:
        return f"❌ Key works but saving failed ({type(e).__name__}) -- nothing changed."
    try:
        import apex_sa365_guard as _g
        _g.reset()
    except Exception:
        pass
    print("[SetKey] SportsAPI365 key replaced via /setkey (tested 200)")
    return "✅ SportsAPI365 key saved and working. Run /fetch when ready."


# APEX-SETKEY-ANTHROPIC-2026-10-27 (owner: "we need the api key, I'll send an updated one
# tomorrow"): /setkey now asks which key. Anthropic: tested with one tiny Haiku call, then saved
# to anthropic_key.txt (mode 600); openclaw_core reads that file per call, so no restart.
ANTHROPIC_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anthropic_key.txt")


def _setkey_anthropic_test_and_save(key):
    import urllib.request, urllib.error, shutil, json as _json
    if not key.startswith("sk-ant-"):
        return "❌ That does not look like an Anthropic key (they start with sk-ant-) -- nothing changed."
    body = _json.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": 5,
                        "messages": [{"role": "user", "content": "Say OK"}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            detail = _json.loads(e.read().decode("utf-8", "ignore")).get("error", {}).get("message", "")[:120]
        except Exception:
            detail = ""
        if code == 401:
            return "❌ Anthropic refused that key (401 invalid key) -- nothing changed."
        if code in (400, 402, 403, 429):
            return "❌ Key accepted but Anthropic said no (%d%s) -- nothing changed." % (code, ": " + detail if detail else "")
    except Exception as e:
        return f"❌ Could not reach Anthropic ({type(e).__name__}) -- nothing changed. Try again in a minute."
    if code != 200:
        return f"❌ Anthropic answered {code} -- nothing changed."
    try:
        if os.path.exists(ANTHROPIC_KEY_FILE):
            shutil.copy2(ANTHROPIC_KEY_FILE, ANTHROPIC_KEY_FILE + ".bak_" + time.strftime("%Y%m%d_%H%M%S"))
        with open(ANTHROPIC_KEY_FILE, "w") as fh:
            fh.write(key + "\n")
        os.chmod(ANTHROPIC_KEY_FILE, 0o600)
    except Exception as e:
        return f"❌ Key works but saving failed ({type(e).__name__}) -- nothing changed."
    print("[SetKey] Anthropic key replaced via /setkey (tested 200)")
    return "✅ Anthropic key saved and working. Slip screenshots and the learning write-ups use it from now on."


# APEX-SETKEY-SGO-2026-10-31 (owner got a SportsGameOdds trial for player aces lines;
# /setkey only had SportsAPI365 + Anthropic). Tested with one usage call, saved to
# sportsgameodds_key.txt (mode 600, gitignored by *key*.txt). Nothing reads it yet.
SGO_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sportsgameodds_key.txt")


def _setkey_sgo_test_and_save(key):
    import urllib.request, urllib.error, shutil
    code = None
    for path in ("/v2/account/usage", "/v2/sports/"):
        req = urllib.request.Request("https://api.sportsgameodds.com" + path,
                                     headers={"x-api-key": key, "accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:
            return f"❌ Could not reach SportsGameOdds ({type(e).__name__}) -- nothing changed. Try again in a minute."
        if code != 404:
            break
    if code in (401, 403):
        return "❌ SportsGameOdds refused that key (%d) -- nothing changed." % code
    if code != 200:
        return f"❌ SportsGameOdds answered {code} -- nothing changed."
    try:
        if os.path.exists(SGO_KEY_FILE):
            shutil.copy2(SGO_KEY_FILE, SGO_KEY_FILE + ".bak_" + time.strftime("%Y%m%d_%H%M%S"))
        with open(SGO_KEY_FILE, "w") as fh:
            fh.write(key + "\n")
        os.chmod(SGO_KEY_FILE, 0o600)
    except Exception as e:
        return f"❌ Key works but saving failed ({type(e).__name__}) -- nothing changed."
    print("[SetKey] SportsGameOdds key saved via /setkey (tested 200)")
    return "✅ SportsGameOdds key saved and working. Your SportsAPI365 key was not touched."


# APEX-BBALLSETKEY-2026-10-31 (owner, "Go step 4 bball bot and setkey"): the separate basketball
# bot's token. Tested with GET /users/@me against its client id, saved only under ~/ApexSports, never here.
BBALL_TOKEN_FILE = os.path.expanduser("~/ApexSports/basketball/secrets/discord_bot_token.txt")
BBALL_CLIENT_ID = int(__import__("os").environ.get("DISCORD_BBALL_CLIENT_ID", "0"))


def _setkey_bball_test_and_save(key):
    import urllib.request, urllib.error
    req = urllib.request.Request("https://discord.com/api/v10/users/@me",
                                 headers={"Authorization": "Bot " + key, "User-Agent": "ApexSetKey (apex, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            who = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return "❌ Discord refused that token (%d) -- nothing changed." % e.code
    except Exception as e:
        return f"❌ Could not reach Discord ({type(e).__name__}) -- nothing changed. Try again in a minute."
    if str(who.get("id")) != BBALL_CLIENT_ID:
        return "❌ That token works but belongs to a different bot -- nothing changed."
    try:
        os.makedirs(os.path.dirname(BBALL_TOKEN_FILE), mode=0o700, exist_ok=True)
        fd = os.open(BBALL_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key + "\n")
        os.chmod(BBALL_TOKEN_FILE, 0o600)
    except Exception as e:
        return f"❌ Token works but saving failed ({type(e).__name__}) -- nothing changed."
    print("[SetKey] Basketball bot token saved via /setkey (tested 200)")
    return "✅ Basketball bot token saved and working. Tennis was not touched."


class _SetBballKeyModal(discord.ui.Modal, title="Basketball bot token"):
    key = discord.ui.TextInput(label="Paste the Basketball bot token", min_length=40, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await asyncio.to_thread(_setkey_bball_test_and_save, str(self.key.value).strip())
        await interaction.followup.send(msg, ephemeral=True)


class _SetKeyModal(discord.ui.Modal, title="SportsAPI365 key"):
    key = discord.ui.TextInput(label="Paste the new SportsAPI365 key", min_length=20, max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await asyncio.to_thread(_setkey_test_and_save, str(self.key.value).strip())
        await interaction.followup.send(msg, ephemeral=True)


class _SetAnthropicKeyModal(discord.ui.Modal, title="Anthropic key"):
    key = discord.ui.TextInput(label="Paste the new Anthropic key (sk-ant-...)", min_length=20, max_length=250)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await asyncio.to_thread(_setkey_anthropic_test_and_save, str(self.key.value).strip())
        await interaction.followup.send(msg, ephemeral=True)


class _SetSgoKeyModal(discord.ui.Modal, title="SportsGameOdds key"):
    key = discord.ui.TextInput(label="Paste the SportsGameOdds key", min_length=16, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await asyncio.to_thread(_setkey_sgo_test_and_save, str(self.key.value).strip())
        await interaction.followup.send(msg, ephemeral=True)


@tree.command(name="setkey", description="Owner only: replace a key (SportsAPI365, Anthropic, SportsGameOdds, Basketball bot), tested first")
@discord.app_commands.describe(service="Which key to replace")
@discord.app_commands.choices(service=[discord.app_commands.Choice(name="SportsAPI365", value="sa365"),
                                       discord.app_commands.Choice(name="Anthropic", value="anthropic"),
                                       discord.app_commands.Choice(name="SportsGameOdds", value="sgo"),
                                       discord.app_commands.Choice(name="Basketball bot", value="bball")])
async def slash_setkey(interaction: discord.Interaction, service: str = "sa365"):
    if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("⛔ Only the server owner can change keys.", ephemeral=True)
        return
    await interaction.response.send_modal(_SetBballKeyModal() if service == "bball" else _SetAnthropicKeyModal() if service == "anthropic"
                                          else _SetSgoKeyModal() if service == "sgo" else _SetKeyModal())


@tree.command(name="fetch", description="Full board scrape + Gate 11 + odds + engine eval — publishes every evaluated pick")
@discord.app_commands.describe(archive_only="If true, only scrape and archive a raw snapshot — no evaluation, no posting")
async def slash_fetch(interaction: discord.Interaction, archive_only: bool = False):
    # APEX-DISCORDREPAIR-2026-09-30: completion delivery used to live
    # ENTIRELY on interaction.followup.send(), bound to Discord's ~15-min
    # interaction token. A long fetch (made longer by a #alerts 429 flood)
    # blew past that window on 2026-09-30 and the user never learned the
    # fetch had actually finished. Fix: a persistent #system message,
    # created via the regular (non-expiring) channel webhook and EDITED
    # as the run progresses, so the final status always lands somewhere
    # real whether or not the interaction token is still alive. The
    # interaction reply below is kept too, as a best-effort bonus for the
    # nicer inline UX on a fast run -- never required for delivery.
    if not _fetch_busy_claim():   # APEX-PPTHROTTLE-2026-10-16: one /fetch at a time
        await interaction.response.send_message("⏳ A /fetch is already running -- results post when it finishes. Not starting a second one.")
        return
    await interaction.response.defer(thinking=True)
    _t0 = time.time()
    _ok0, _status_mid = await asyncio.to_thread(
        core.send_discord_tracked, "system",
        "🚀 **/fetch** — starting ("
        + ("archive only" if archive_only else "full evaluation") + ")…"
    )

    async def _edit_status(text):
        if _status_mid:
            await asyncio.to_thread(core.edit_discord_message, "system", _status_mid, text)

    _FETCH_RUNNING["on"] = True   # APEX-FETCHCANCEL-2026-10-12
    core.clear_fetch_cancel()
    # APEX-FETCHSTATUS-2026-10-23: live #general status message + 10-min watchdog + restart marker
    _FETCH_PROGRESS.clear()
    _FETCH_PROGRESS.update(t0=_t0, step="pull", moved_at=_t0, last_edit=time.time())
    _ok_g, _FETCH_PROGRESS["general_mid"] = await asyncio.to_thread(
        core.send_discord_tracked, "general", _fetch_status_text())
    _fetch_marker_write(True)
    _watchdog = asyncio.create_task(_fetch_watchdog())
    _cancel_view = _FetchCancelView()
    try:
        await interaction.followup.send(
            "📡 **/fetch** — launching full Tennis board scrape (all 11 tabs, "
            + ("archive only" if archive_only else "full evaluation — publishes every evaluated pick")
            + ")… this can take several minutes. Progress + final result will also post to #system.\n"
            "Press **Cancel fetch** below (or type `/cancel`) to stop it after the current step.",
            view=_cancel_view,
        )
    except Exception as e:
        core.log("Discord", f"/fetch: initial interaction reply failed (non-fatal): {type(e).__name__}: {e}")

    def _progress_cb(stage, **kw):
        # Runs inside the background thread -- a direct (sync) edit call
        # is correct here, there is no event loop to hand it to.
        _stage_text = {
            "scraped":    f"📡 **/fetch** — board scraped ({kw.get('scraped')} props). Evaluating picks…",
            "evaluating": f"⚙️ **/fetch** — evaluating {kw.get('valid_props')} prop(s)… this is the slow part.",
            "publishing": f"📤 **/fetch** — publishing {kw.get('selected')} pick(s)…",
        }.get(stage)
        if _stage_text and _status_mid:
            core.edit_discord_message("system", _status_mid, _stage_text)
        _fetch_progress_note(stage, **kw)   # APEX-FETCHSTATUS-2026-10-23

    try:
        if archive_only:
            result = await asyncio.to_thread(_fetch_archive_render)
        else:
            result = await asyncio.to_thread(_fetch_publish_all_render, _progress_cb)
    except Exception as e:
        _fail_text = f"❌ **Fetch Failed**\n{_friendly_error(e)}"
        _FETCH_LAST["error"] = _friendly_error(e)   # APEX-5PMFETCH-2026-10-16
        await _edit_status(_fail_text)
        await asyncio.to_thread(_fetch_status_edit, _fetch_failed_text(_friendly_error(e)))   # APEX-FETCHSTATUS-2026-10-23
        try:
            await interaction.followup.send(_fail_text)
        except Exception:
            pass
        return
    finally:
        _FETCH_RUNNING["on"] = False   # APEX-FETCHCANCEL-2026-10-12
        _FETCH_RUNNING["claimed"] = 0   # APEX-PPTHROTTLE-2026-10-16
        _cancel_view.disable()
        _watchdog.cancel()   # APEX-FETCHSTATUS-2026-10-23
        _fetch_marker_write(False)

    _FETCH_LAST["result"] = result   # APEX-5PMFETCH-2026-10-16: lets the 5 PM run log finish/fail
    _runtime_s = round(time.time() - _t0, 1)

    if result.get("cancelled"):   # APEX-FETCHCANCEL-2026-10-12
        _done = result.get("cancel_evaluated", 0); _tot = result.get("cancel_total", 0)
        _stage = "during the board scrape" if result.get("stage") == "scrape" else f"after evaluating {_done} of {_tot} props"
        _c_text = (f"🛑 **/fetch cancelled** by request {_stage} (runtime: {_runtime_s}s). "
                   "Nothing was published and the active board is unchanged. Run /fetch again when ready.")
        await _edit_status(_c_text)
        await asyncio.to_thread(_fetch_status_edit, _c_text)   # APEX-FETCHSTATUS-2026-10-23
        try:
            await interaction.followup.send(_c_text)
        except Exception:
            pass
        return

    if not result.get("ok"):
        _fail_text = f"❌ **Fetch Failed**\nReason: {result.get('error', 'unknown error')}"
        await _edit_status(_fail_text)
        await asyncio.to_thread(_fetch_status_edit, _fetch_failed_text(result.get("error", "unknown error")))   # APEX-FETCHSTATUS-2026-10-23
        try:
            await interaction.followup.send(_fail_text)
        except Exception:
            pass
        return

    if archive_only:
        text = (f"✅ **Board Fetch Complete** (runtime: {_runtime_s}s)\n\nTotal Raw Props: {result['raw_total']}\n"
                f"Total Deduped Props: {result['deduped_total']}\n\n"
                + (f"📦 Board snapshot archived: `{result.get('archive_path')}`" if result.get("archive_path")
                   else f"⚠️ Archive failed: {result.get('archive_error')}"))
        await _edit_status(text)
        try:
            await _slash_send_chunks(interaction, text)
        except Exception as e:
            core.log("Discord", f"/fetch archive_only: interaction followup failed (non-fatal): {type(e).__name__}: {e}")
        return

    if result.get("evaluate_error"):
        _fail_text = f"⚠️ **Evaluation Failed**\nReason: {result['evaluate_error']}"
        await _edit_status(_fail_text)
        await asyncio.to_thread(_fetch_status_edit, _fetch_failed_text(result["evaluate_error"]))   # APEX-FETCHSTATUS-2026-10-23
        try:
            await _slash_send_chunks(interaction, _fail_text)
        except Exception:
            pass
        return

    if not archive_only:   # APEX-FETCHSTATUS-2026-10-23
        # APEX-BOARDSTATUS-HEADER-2026-10-25: the big TOP 3 & ELITE 6 status leads the final line
        await asyncio.to_thread(_fetch_status_edit, core._board_status_wrap(_FETCH_STARTED.pop("status", None),
            f"✅ **Boards posted {_ct_hm()}** -- fetch started {_ct_hm(_t0)}, took {int(_runtime_s // 60)} min. "
            f"{result.get('scored', result.get('engine_evaluated_raw', 0))} props scored, {result.get('published', 0)} picks published."))

    # APEX-DISCORDREPAIR-2026-09-30: fetch summary + warnings roll-up.
    _dd = result.get("discord_delivery") or {}
    _warnings = []
    if result.get("suppressed_alerts"):
        _warnings.append(f"{result['suppressed_alerts']} evaluation alert(s) summarized here instead of posted individually")
    if _dd.get("rate_limited_429"):
        _warnings.append(f"{_dd['rate_limited_429']} Discord 429(s) hit and retried "
                          f"({_dd.get('retries', 0)} retries, {_dd.get('final_failures', 0)} unresolved)")
    if result.get("demonlock_dropped"):
        _warnings.append(f"{result['demonlock_dropped']} MORE-only prop(s) dropped (engine scored LESS, impossible on PrizePicks)")
    if result.get("freshness_warning"):
        _warnings.append(result["freshness_warning"])

    # APEX-MOREFIXAUDIT-2026-10-05 (owner, approved design): the old
    # "Props Evaluated: 166" line read as "Apex only looked at 166 props,"
    # when the engine actually scored 306 and then lost 140 of those to a
    # MORE-only platform conflict (not a quality problem -- PrizePicks
    # simply doesn't offer the LESS side on Demon/Goblin/Red Demon lines).
    # This redesign separates "went into the engine" from "came out the
    # other end" from "got published," and breaks each loss bucket out by
    # WHERE in the funnel it happened, so none of those three numbers can
    # be mistaken for each other again.
    _db  = result.get("decline_breakdown") or {}
    _pre  = result.get("pre_engine_drops", 0)
    _raw  = result.get("engine_evaluated_raw", 0)
    _pass = result.get("passed_evaluation", result.get("evaluated", 0))
    _pub  = result.get("published", 0)
    _more = result.get("demonlock_dropped", 0)
    _less = result.get("apex_preferred_less", 0)
    _more_dir = result.get("apex_preferred_more", 0)
    _verified = result.get("gate11_verified", 0)

    def _rate(n, d):
        return f"{n} / {d} ({(n / d * 100):.1f}%)" if d else f"{n} / {d}"

    lines = [
        "✅ **Board Fetch Complete**", "",
        "🎾 **BOARD INVENTORY**",
        f"Props Scraped: {result.get('scraped')}",
        f"Props Verified: {_verified}", "",
        "🧠 **ENGINE FUNNEL**",
        f"Pre-Engine Drops: {_pre}",
        f"Engine Evaluated: {_raw}",
        f"Passed Evaluation: {_pass}",
        f"Published: {_pub}", "",
        "🚫 **PRE-ENGINE LOSSES**",
        f"First Set (scored by the first-set rules, not lost): {_db.get('first_set_markets', 0)}",
        f"Unknown Players: {_db.get('unknown_players', 0)}",
        f"Doubles: {_db.get('doubles', 0)}",
        f"No Odds: {_db.get('no_odds', 0)}", "",
        "🚫 **POST-ENGINE LOSSES**",
        f"MORE-only Conflicts: {_more}", "",
        "**MORE-ONLY DETAIL**",
        f"Apex Preferred LESS: {_less}",
        f"Apex Preferred MORE: {_more_dir}"
        + ("" if _more_dir == 0 else "  ⚠️ unexpected — review demonlock_detail"), "",
    ]
    _dlock_sample = (result.get("demonlock_detail") or [])[:10]
    if _dlock_sample:
        lines.append("**Sample MORE-only Conflicts (first 10)**")
        for _d in _dlock_sample:
            lines.append(
                f"• {_d.get('player')} — {_d.get('market')} {_d.get('line')} "
                f"[{_d.get('prop_type')}] — PrizePicks: {_d.get('pp_direction')}, "
                f"Apex: {_d.get('apex_direction')}"
            )
        lines.append("")
    lines += [
        "📊 **EVALUATION RATE**",
        _rate(_raw, _verified), "",
        "📊 **PASS RATE**",
        _rate(_pass, _raw), "",
        "📊 **PUBLISH RATE**",
        _rate(_pub, _pass), "",
        "**BOARD QUALITY INSIGHT**",
        f"Props Evaluated by Apex: {_raw}",
        f"Props Passed Evaluation: {_pass}",
        f"Props Rejected After Evaluation: {_more}", "",
        f"Demon Picks: {result.get('demon_count')}",
        f"Goblin Picks: {result.get('goblin_count')}",
        f"Standard Picks: {result.get('standard_count')}", "",
        f"Runtime: {_runtime_s}s",
    ]
    # Sum check against the OLD "Props Declined" population (pre-engine +
    # MORE-only), kept so an eyeball-auditor can still confirm nothing
    # silently vanished, same guarantee the old block gave.
    if _db:
        _db_sum = sum(_db.values())
        lines += [
            f"Sum check — Pre-Engine ({_pre}) + MORE-only ({_more}) = {_pre + _more} "
            f"vs decline buckets sum ({_db_sum}) "
            f"({'✅ reconciles' if _db_sum == _pre + _more else '⚠️ MISMATCH -- flag for review'})",
            "",
        ]
    if result.get("freshness_line"):
        lines.append(result["freshness_line"])
    lines.append("")
    if _warnings:
        lines.append("**Warnings**")
        lines.extend(f"⚠️ {w}" for w in _warnings)
        lines.append("")
    lines.append(
        "Every evaluated pick was posted to the Picks Channel — same as a "
        "manual screenshot submission. Use /bestgoblin, /beststandard, "
        "/bestdemon, /elite6, /top10, or /allprops for filtered views."
    )
    _full_text = "\n".join(lines)

    # Guaranteed delivery first (never depends on the interaction token).
    await _edit_status(_full_text)
    # Best-effort bonus: nicer inline reply when the token is still alive.
    try:
        await _slash_send_chunks(interaction, _full_text)
    except Exception as e:
        core.log("Discord", f"/fetch: interaction followup failed (non-fatal, #system already has the result): {type(e).__name__}: {e}")

    # APEX-FETCHRELIABILITY-2026-10-01: Health Check Redesign -- User
    # Performance Report, separate from the Developer Health summary
    # above. Posted best-effort to #system (guaranteed channel) and, if
    # still alive, the interaction too; never raises, so it can't turn a
    # completed /fetch into a reported failure.
    _perf_text = await asyncio.to_thread(_user_performance_report)
    try:
        await asyncio.to_thread(core.send_discord, "system", _perf_text)
    except Exception as e:
        core.log("Discord", f"/fetch: User Performance Report post failed (non-fatal): {type(e).__name__}: {e}")
    try:
        await _slash_send_chunks(interaction, _perf_text)
    except Exception:
        pass

    # APEX-UPRV3-2026-10-01 Objective #10: Fetch Completion Report --
    # same guaranteed-delivery pattern as the sections above (#system
    # first, interaction reply best-effort).
    _fcr_text = _fetch_completion_report_text(result, _runtime_s)
    try:
        await asyncio.to_thread(core.send_discord, "system", _fcr_text)
    except Exception as e:
        core.log("Discord", f"/fetch: Fetch Completion Report post failed (non-fatal): {type(e).__name__}: {e}")
    try:
        await _slash_send_chunks(interaction, _fcr_text)
    except Exception:
        pass


# ================================================================ APEX-5PMFETCH-2026-10-16
# owner, 2026-10-16 (option 1 "Real /fetch"): the 5 PM run is a real
# /fetch -- the exact slash_fetch code path, with its replies going to
# #general instead of an interaction -- so 5 PM looks the same as a /fetch he
# types. Same one-at-a-time claim and the same shared PrizePicks gate (one
# pull). If a /fetch is already running it waits and retries each minute.
# Runs once per day between 5:00 and 7:00 PM CT; the scheduler no longer runs
# the old 17:00 run_pipeline. Start/finish/fail go to pipeline_runs.json so
# the watchdog and the daily report keep working.
_FETCH_LAST = {"result": None, "error": None}
FIVEPM_FETCH_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs", "fivepm_fetch_state.json")


class _ChannelInteraction:
    """Stands in for a discord.Interaction so slash_fetch's replies post to a channel."""
    def __init__(self, channel):
        self.channel = channel
        self.response = self
        self.followup = self

    async def defer(self, **kw):
        return None

    async def send_message(self, content=None, **kw):
        return await self.channel.send(content, **kw)

    async def send(self, content=None, **kw):
        return await self.channel.send(content, **kw)


def _fivepm_state():
    try:
        with open(FIVEPM_FETCH_STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _fivepm_state_write(st):
    try:
        with open(FIVEPM_FETCH_STATE, "w") as fh:
            json.dump(st, fh, indent=1)
    except Exception as e:
        print(f"[5PMFetch] state write failed: {type(e).__name__}: {e}")


@tasks.loop(minutes=1)
async def fivepm_fetch_loop():
    try:
        import apex_ops as _ops
        now = __import__("datetime").datetime.now(core.CT)
        if not (17 <= now.hour < 19):
            return
        today = now.date().isoformat()
        st = _fivepm_state()
        if st.get("date") == today:
            return
        if _FETCH_RUNNING["on"] or time.time() - _FETCH_RUNNING.get("claimed", 0) < 60:
            if st.get("waiting") != today:
                print(f"[5PMFetch] a /fetch is already running -- waiting, retry every minute")
                _fivepm_state_write(dict(st, waiting=today))
            return
        ch = None
        if client.guilds:
            ch = next((c for c in client.guilds[0].text_channels if c.name == GENERAL_CHANNEL_NAME), None)
        if not ch:
            print("[5PMFetch] #general not found -- retry next minute")
            return
        _fivepm_state_write({"date": today, "started": now.isoformat(timespec="seconds")})
        _ops.record_pipeline_event("start", source="5pm_fetch")
        try:
            await asyncio.to_thread(core._alert_pending_approvals, "5PM fetch start")
        except Exception as e:
            print(f"[5PMFetch] pending-approval alert skipped: {type(e).__name__}: {e}")
        _FETCH_LAST["result"] = None
        _FETCH_LAST["error"] = None
        await slash_fetch.callback(_ChannelInteraction(ch))
        res, err = _FETCH_LAST["result"], _FETCH_LAST["error"]
        if res is None and err is None:
            # lost the one-at-a-time race (or never started): try again next minute
            _fivepm_state_write({"date": None, "waiting": today})
            print("[5PMFetch] fetch did not start -- retry next minute")
            return
        if err or not (res or {}).get("ok") or (res or {}).get("cancelled") or (res or {}).get("evaluate_error"):
            why = err or (res or {}).get("error") or (res or {}).get("evaluate_error") or ("cancelled" if (res or {}).get("cancelled") else "unknown")
            _ops.record_pipeline_event("fail", error=str(why)[:300], source="5pm_fetch")
            core.send_discord("system", f"❌ **5 PM fetch failed** — {str(why)[:300]}")
        else:
            _ops.record_pipeline_event("finish", source="5pm_fetch")
        _fivepm_state_write(dict(_fivepm_state(), finished=__import__("datetime").datetime.now(core.CT).isoformat(timespec="seconds")))
    except Exception as e:
        print(f"[5PMFetch] loop failed: {type(e).__name__}: {e}")
        try:
            import apex_ops as _ops
            _ops.record_pipeline_event("fail", error=f"{type(e).__name__}: {e}"[:300], source="5pm_fetch")
        except Exception:
            pass


@tree.command(name="board", description="Scrape-only board diagnostics — no Gate 11, no odds, no engine, no picks")
async def slash_board(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    await interaction.followup.send("📡 **/board** — scrape-only diagnostics (all 11 tabs)… under a minute.")
    try:
        result = await asyncio.to_thread(_board_render)
    except Exception as e:
        await interaction.followup.send(f"❌ **Board Check Failed**\nReason: `{type(e).__name__}: {e}`")
        return
    if not result.get("ok"):
        await interaction.followup.send(f"❌ **Board Check Failed**\nReason: {result.get('error', 'unknown error')}")
        return
    per_tab = result.get("per_tab") or {}
    per_tab_lines = "\n".join(f"  {k}: {v}" for k, v in per_tab.items()) or "  (not available)"
    text = (f"✅ **Board Check Complete**\n\n**Board Summary**\n"
            f"Total Raw Props: {result['raw_total']}\nTotal Deduped Props: {result['deduped_total']}\n\n"
            f"Demon: {result['demon']}\nGoblin: {result['goblin']}\nStandard: {result['standard']}\n\n"
            f"**Props Per Tab**\n{per_tab_lines}")
    await _slash_send_chunks(interaction, text)


async def _slash_bb(interaction: discord.Interaction, cmd: str):
    """Shared handler for the read-only /bestgoblin /beststandard
    /bestdemon /top10 /allpicks slash commands -- reads the most
    recently evaluated board from the pick log, never runs a new
    evaluation (same _bb_render() the text commands use)."""
    await interaction.response.defer(thinking=True)
    try:
        txt = await asyncio.to_thread(_bb_render, cmd)
    except Exception as e:
        txt = f"best bets unavailable ({type(e).__name__}: {e})"
    await _slash_send_chunks(interaction, txt)


@tree.command(name="bestgoblin", description="Best Goblin pick from the most recently evaluated board (no new evaluation)")
async def slash_bestgoblin(interaction: discord.Interaction):
    await _slash_bb(interaction, "/bestgoblin")


@tree.command(name="beststandard", description="Best Standard pick from the most recently evaluated board (no new evaluation)")
async def slash_beststandard(interaction: discord.Interaction):
    await _slash_bb(interaction, "/beststandard")


@tree.command(name="bestdemon", description="Best Demon pick from the most recently evaluated board (no new evaluation)")
async def slash_bestdemon(interaction: discord.Interaction):
    await _slash_bb(interaction, "/bestdemon")


@tree.command(name="top10", description="Top 10 picks across the whole board (no new evaluation)")
async def slash_top10(interaction: discord.Interaction):
    await _slash_bb(interaction, "/top10")


@tree.command(name="allprops", description="Every prop at 70%+ Win Probability, all types, most likely to win first (same rank as the boards)")
async def slash_allprops(interaction: discord.Interaction):
    await _slash_bb(interaction, "/allprops")


# APEX-LOTTERY-2026-11-06 (owner, "Go lottery"; tennis server by his choice): mixed-sports 6-pick Flex,
# Demons + safest Standards, no Goblins, traps cut. Read only (apex_lottery.py reads saved files, never pulls PrizePicks).
@tree.command(name="lottery", description="🎰 Lottery Picks: mixed-sports 6-pick Flex, Demons + safest Standards, no Goblins")
async def slash_lottery(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        import apex_lottery as _al
        txt = await asyncio.to_thread(_al.render)
    except Exception as e:
        txt = "🎰 Lottery Picks unavailable (%s)" % type(e).__name__
    await _slash_send_chunks(interaction, txt)


# APEX-BEST6-2026-11-11 (owner, "go best 6 ... call with tennis general but the name best6of6"; tennis server by his
# choice, like /lottery): the top 2 best-group Goblins of NFL, college football, NHL and NBA, best 6 kept. Read only
# (apex_best6.py asks each sport's own code for its current picks; never pulls PrizePicks). No tennis picks.
# APEX-BOARDREFRESH-2026-11-12 (owner: "Whenever I run a board I want everything refresh, I hate seeing picks that's not
# available"): sports whose PrizePicks lines are older than 15 minutes are read again before the card is built. Never raises.
async def _b6_fresh(interaction, _b6):
    try:
        stale = await asyncio.to_thread(_b6.stale_sports)
        if not stale: return
        await interaction.followup.send("🔄 Lines are old for %s. Reading PrizePicks again first so nothing pulled or moved shows up (about a minute per sport). The card posts right after." % ", ".join(n for _, n, _ in stale))
        did, notes = await asyncio.to_thread(_b6.refresh_sports)
        for n in notes[:4]: await interaction.followup.send("⚠️ " + n)
    except Exception as e:
        print("[best6] refresh failed: %s" % e, flush=True)


@tree.command(name="best6of6", description="⭐ Best 6 of 6: tennis top 2 + best Goblins of every sport, today or tomorrow")
async def slash_best6of6(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        import apex_best6 as _b6
        await _b6_fresh(interaction, _b6)   # APEX-BOARDREFRESH-2026-11-12
        txt = await asyncio.to_thread(_b6.render)
    except Exception as e:
        txt = "⭐ Best 6 of 6 unavailable (%s)" % type(e).__name__
    await _slash_send_chunks(interaction, txt)


# APEX-BEST3-2026-11-14 (owner, "I said fix all"): /best3, the best 3 picks from each sport's own numbered Top 3 (apex_best3.render).
@tree.command(name="best3", description="🥉 Best 3: best 3 picks from each sport's own Top 3, today or tomorrow (new, not proven yet)")
async def slash_best3(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        import apex_best6 as _b6, apex_best3 as _b3
        await _b6_fresh(interaction, _b6)
        txt = await asyncio.to_thread(_b3.render)
    except Exception as e:
        txt = "🥉 Best 3 unavailable (%s)" % type(e).__name__
    await _slash_send_chunks(interaction, txt)


# APEX-BESTLESSCARD-2026-11-12 (owner: "I want the best less command in the tennis general ... just like we did best6of6"):
# the best Goblin LESS picks of NFL, college football, NHL and NBA on one card. Read only (apex_best6.render_less). Two names, same card.
async def _bestless_send(interaction):
    await interaction.response.defer(thinking=True)
    try:
        import apex_best6 as _b6
        await _b6_fresh(interaction, _b6)   # APEX-BOARDREFRESH-2026-11-12
        txt = await asyncio.to_thread(_b6.render_less)
    except Exception as e:
        txt = "🟢 Best Goblin LESS unavailable (%s)" % type(e).__name__
    await _slash_send_chunks(interaction, txt)


# APEX-STARDEMON-2026-11-13 (owner, "Go star demon board for tennis server"): NBA STAR DEMON card, the most consistent stars'
# first Demon. Read only on NBA (apex_star_demon.render). Command only, never auto-posted. Off: apex_star_demon.ON = False, restart.
# APEX-EARLYDEMON-2026-11-13 (owner, "Go early demon board"): NBA first Demons with the sportsbooks already at the number or a key
# teammate out, to take before PrizePicks moves its line. Read only on NBA (apex_early_demon.render). Command only. Off: apex_early_demon.ON = False.
@tree.command(name="earlydemon", description="🔴 Early Demon: NBA Demons to take before PrizePicks moves the line up")
async def slash_earlydemon(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        import apex_early_demon as _ed
        txt = await asyncio.to_thread(_ed.render)
    except Exception as e:
        txt = "🔴 Early Demon unavailable (%s)" % type(e).__name__
    await _slash_send_chunks(interaction, txt)


@tree.command(name="stardemon", description="🔴 Star Demon: NBA's most consistent stars on their first Demon line")
async def slash_stardemon(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        import apex_star_demon as _sd
        txt = await asyncio.to_thread(_sd.render)
    except Exception as e:
        txt = "🔴 Star Demon unavailable (%s)" % type(e).__name__
    await _slash_send_chunks(interaction, txt)


@tree.command(name="bestless", description="🟢 Best Goblin LESS: best LESS picks of every sport, top 2 per sport, best 6 kept")
async def slash_bestless(interaction: discord.Interaction):
    await _bestless_send(interaction)


@tree.command(name="goblinless", description="🟢 Goblin LESS: same card as /bestless, best LESS picks of every sport")
async def slash_goblinless(interaction: discord.Interaction):
    await _bestless_send(interaction)


# APEX-H2H-2026-10-22 (owner, "H2h command go"): past meetings between two
# players from stored finished matches. Display only. Switch: apex_h2h.SHOW_H2H.
@tree.command(name="h2h", description="Head-to-head: past meetings for a player's match today, or 'Player A vs Player B'")
@discord.app_commands.describe(players="A player on today's board (e.g. Boulter), or two players: Boulter vs Golubic")
async def slash_h2h(interaction: discord.Interaction, players: str):
    await interaction.response.defer(thinking=True)
    try:
        import apex_h2h
        txt = await asyncio.to_thread(apex_h2h.h2h_command_text, players)
    except Exception as e:
        txt = f"head-to-head unavailable ({type(e).__name__}: {e})"
    await _slash_send_chunks(interaction, txt)


@tree.command(name="elite6", description="The 6 strongest plays on the entire board -- no category quota, no padding (no new evaluation)")
async def slash_elite6(interaction: discord.Interaction):
    await _slash_bb(interaction, "/elite6")


@tree.command(name="top3", description="Top 3 picks across the whole board, plus Next Best Options ranks 4-10 (no new evaluation)")
async def slash_top3(interaction: discord.Interaction):
    await _slash_bb(interaction, "/top3")


# APEX-BACKUPSCMD-2026-10-26 (owner: "I want the backups to have its own '/' for each board
# rather than showing up"): one read-only backups command per board.
async def _slash_backups(interaction: discord.Interaction, flag: str):
    await interaction.response.defer(thinking=True)
    try:
        import apex_edge_tracker as _etb
        txt = await asyncio.to_thread(_etb.backups_detail, flag)
    except Exception as e:
        txt = f"backups unavailable ({type(e).__name__}: {e})"
    await _slash_send_chunks(interaction, txt)


@tree.command(name="top3backups", description="Top 3 BACKUPS: next picks in line, in the order the board ranks them")
async def slash_top3backups(interaction: discord.Interaction):
    await _slash_backups(interaction, "top3")


@tree.command(name="elite6backups", description="Elite 6 BACKUPS: next picks in line, in the order the board ranks them")
async def slash_elite6backups(interaction: discord.Interaction):
    await _slash_backups(interaction, "elite6")


@tree.command(name="top10backups", description="Top 10 BACKUPS: next picks in line, in the order the board ranks them")
async def slash_top10backups(interaction: discord.Interaction):
    await _slash_backups(interaction, "top10")


@tree.command(name="goblinbackups", description="Best Goblin BACKUPS: next picks in line, in the order the board ranks them")
async def slash_goblinbackups(interaction: discord.Interaction):
    await _slash_backups(interaction, "bestgoblin")


@tree.command(name="demonbackups", description="Best Demon BACKUPS: next picks in line, in the order the board ranks them")
async def slash_demonbackups(interaction: discord.Interaction):
    await _slash_backups(interaction, "bestdemon")


@tree.command(name="standardbackups", description="Best Standard BACKUPS: next picks in line, in the order the board ranks them")
async def slash_standardbackups(interaction: discord.Interaction):
    await _slash_backups(interaction, "beststandard")


# APEX-SURFACE-AUTOLOOKUP-2026-10-27 (owner, "Go"): /surface sets a tournament's court by
# hand when the automatic lookup could not confirm it. Writes data/surface_auto.json only.
# APEX-BANLIST-2026-10-28 (owner, "Put a park on the banned list"): /ban, /unban, /banned.
@tree.command(name="ban", description="Ban a player: her whole match comes off every board and slip")
@discord.app_commands.describe(player="First and last name (e.g. Alycia Parks)", why="Plain reason (optional)")
async def slash_ban(interaction: discord.Interaction, player: str, why: str = ""):
    await interaction.response.defer(thinking=True)
    try:
        import apex_ban_list as _abl
        txt = await asyncio.to_thread(_abl.ban, player, why)
        try:
            import apex_edge_tracker as _aet_ban
            await asyncio.to_thread(_aet_ban.republish_boards, "ban")
            txt += "\nBoards republished."
        except Exception as _e:
            txt += "\n(boards will update on the next republish: %s)" % type(_e).__name__
        txt += "\n" + await asyncio.to_thread(_abl.list_text)
    except Exception as e:
        txt = f"could not ban that player ({type(e).__name__}: {e})"
    await _slash_send_chunks(interaction, txt)


@tree.command(name="unban", description="Take a player off the banned list")
@discord.app_commands.describe(player="The name as /banned shows it")
async def slash_unban(interaction: discord.Interaction, player: str):
    await interaction.response.defer(thinking=True)
    try:
        import apex_ban_list as _abl
        txt = await asyncio.to_thread(_abl.unban, player)
        try:
            import apex_edge_tracker as _aet_ban
            await asyncio.to_thread(_aet_ban.republish_boards, "unban")
            txt += "\nBoards republished."
        except Exception as _e:
            txt += "\n(boards will update on the next republish: %s)" % type(_e).__name__
        txt += "\n" + await asyncio.to_thread(_abl.list_text)
    except Exception as e:
        txt = f"could not unban that player ({type(e).__name__}: {e})"
    await _slash_send_chunks(interaction, txt)


@tree.command(name="banned", description="Show the banned list")
async def slash_banned(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        import apex_ban_list as _abl
        txt = await asyncio.to_thread(_abl.list_text)
    except Exception as e:
        txt = f"could not read the banned list ({type(e).__name__}: {e})"
    await _slash_send_chunks(interaction, txt)


@tree.command(name="surface", description="Set a tournament's court when Apex could not confirm it (clay / hard / grass)")
@discord.app_commands.describe(tournament="The tournament name as the alert showed it (e.g. Queretaro Open - Queretaro)",
                               court="Clay, Hard or Grass", indoor="Indoor or outdoor (optional)")
@discord.app_commands.choices(court=[discord.app_commands.Choice(name="Clay", value="Clay"),
                                     discord.app_commands.Choice(name="Hard", value="Hard"),
                                     discord.app_commands.Choice(name="Grass", value="Grass")],
                              indoor=[discord.app_commands.Choice(name="Indoor", value="indoor"),
                                      discord.app_commands.Choice(name="Outdoor", value="outdoor")])
async def slash_surface(interaction: discord.Interaction, tournament: str, court: str, indoor: str = ""):
    await interaction.response.defer(thinking=True)
    try:
        import apex_surface_map as _asm
        _ind = {"indoor": True, "outdoor": False}.get(indoor)
        txt = await asyncio.to_thread(_asm.set_manual, tournament, court, _ind)
        try:
            left = await asyncio.to_thread(_asm.unmapped_now)
            if left:
                txt += "\nStill unknown: " + " · ".join(left)
        except Exception:
            pass
    except Exception as e:
        txt = f"could not save that court ({type(e).__name__}: {e})"
    await _slash_send_chunks(interaction, txt)


@tree.command(name="slips", description="PrizePicks slips: Top 3 (Power) and Elite 6 (Flex) as numbered on the boards")
async def slash_slips(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)   # APEX-SLIPSCHANNEL-2026-10-15
    await _send_slips_on_demand(interaction.followup.send)


# APEX-SOON-2026-10-26 (owner: "I want a '/' for props that about to play, same rules for
# the board, but it has the matches that about to play soonest when I press it"): /soon lists the
# board picks (numbered picks + first backups) by match start time, soonest first. Read-only.
@tree.command(name="soon", description="Board picks by start time, soonest first (same picks and rules as the boards)")
async def slash_soon(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        import apex_soon as _soon
        txt = await asyncio.to_thread(_soon.soon_text)
    except Exception as e:
        txt = f"/soon unavailable ({type(e).__name__}: {e})"
    await _slash_send_chunks(interaction, txt)


# APEX-SOONONDEMAND-2026-10-27 (owner: "I don't want any of the soon cards showing unless I command
# them"): /soon3 and /soon6 show the Top 3 Soon / Elite 6 Soon card on demand (next 6 hours only).
@tree.command(name="soon3", description="TOP 3 SOON card: best 3 picks starting in the next 6 hours (all Ranking # 80+ = ready)")
async def slash_soon3(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        import apex_soon_slips as _ss
        txt = await asyncio.to_thread(_ss.card_text, "top3")
    except Exception as e:
        txt = f"/soon3 unavailable ({type(e).__name__}: {e})"
    await _slash_send_chunks(interaction, txt)


@tree.command(name="soon6", description="ELITE 6 SOON card: best 6 picks starting in the next 6 hours (all Ranking # 90+ = ready)")
async def slash_soon6(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        import apex_soon_slips as _ss
        txt = await asyncio.to_thread(_ss.card_text, "elite6")
    except Exception as e:
        txt = f"/soon6 unavailable ({type(e).__name__}: {e})"
    await _slash_send_chunks(interaction, txt)


@tree.command(name="setgames", description="First-set games list: strict rules, separate from Top 3 and Elite")
async def slash_setgames(interaction: discord.Interaction):
    await interaction.response.defer()
    import apex_set_games as _sg   # APEX-SETGAMES-2026-11-01
    for _part in _sg.chunks(await asyncio.to_thread(_sg.card_text)):
        await interaction.followup.send(content=_part)


@tree.command(name="counters", description="Whole-slip record for every board since 10/31")
async def slash_counters(interaction: discord.Interaction):
    await interaction.response.defer()
    await _send_counters(interaction.followup.send)


@tree.command(name="besttime", description="Best time to play: Top 3, Elite 5, Elite 6 at each check-in time, real days and replay")
async def slash_besttime(interaction: discord.Interaction):
    await interaction.response.defer()
    await _send_besttime(interaction.followup.send)


@tree.command(name="slipsv2", description="Slips v2 record since 10/22: Top 3 and Elite 6 by day, in dollars")
async def slash_slipsv2(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)   # APEX-SLIPSV2-2026-10-22
    await _send_slipsv2(interaction.followup.send)


@tree.command(name="pnl", description="💰 P&L card: daily (default), week, month, or export the 2026 tax ledger")
@discord.app_commands.describe(day="A day as YYYY-MM-DD (week/month: any day in it, or YYYY-MM for month)",
                               view="daily (default), week, month, or export (owner only: ledger files)")
@discord.app_commands.choices(view=[discord.app_commands.Choice(name=n, value=n)
                                    for n in ("daily", "week", "month", "export")])
async def slash_pnl(interaction: discord.Interaction, day: str = None,
                    view: discord.app_commands.Choice[str] = None):
    v = view.value if view else "daily"   # APEX-PNLTAXES-2026-10-23: week / month / export
    if v == "export" and (not interaction.guild or interaction.user.id != interaction.guild.owner_id):
        await interaction.response.send_message("⛔ Only the server owner can export the tax ledger.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)   # APEX-PNLCHANNEL-2026-10-23
    d = day.strip() if day else None
    if d and not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) or (v == "month" and re.fullmatch(r"\d{4}-\d{2}", d))):
        await interaction.followup.send(content="⚠️ Use a day like 2026-10-22 (or 2026-10 for a month), or leave it blank.")
        return
    if v == "export":
        try:
            import apex_pnl_ledger as _lg
            n, _note = await asyncio.to_thread(_lg.rebuild, True)
            line = await asyncio.to_thread(_lg.summary_line)
            await interaction.followup.send(content=line[:1990], files=[
                discord.File(_lg.XLSX, filename=os.path.basename(_lg.XLSX)),
                discord.File(_lg.CSV, filename=os.path.basename(_lg.CSV))])
        except Exception as e:
            await interaction.followup.send(content="⚠️ Ledger export failed (%s)." % type(e).__name__)
        return
    kind = {"week": "weekly", "month": "monthly"}.get(v, "daily")
    if not await _post_pnl_card(d, send=interaction.followup.send, kind=kind):
        await interaction.followup.send(content="⚠️ P&L card unavailable right now.")


# APEX-PARLAYPLAY-2026-10-15: ParlayPlay-only commands. They read
# data/parlayplay/parlayplay_db.sqlite (apex_parlayplay.py, own 30-min job) and
# never touch the PrizePicks boards.
_PP_HOME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ApexParlayPlay")
if _PP_HOME not in sys.path:
    sys.path.insert(0, _PP_HOME)


async def _slash_pp(interaction, mode):
    await interaction.response.defer(thinking=True)
    try:
        import apex_parlayplay as _pp
        cur = _pp.current()
        if mode == "best":
            text = _pp.board_text(_pp.board(cur), len(cur))
        elif mode == "slip":
            _cards = _pp.slip_cards(cur)
            if not _cards:
                await interaction.followup.send("No ParlayPlay slip right now: fewer than 2 matches have a board-quality prop.")
                return
            await interaction.followup.send(embeds=[discord.Embed.from_dict(_pp.card_embed(c)) for c in _cards[:10]])
            return
        else:
            text = _pp.allprops_text(cur)
    except Exception as e:
        text = f"⚠️ ParlayPlay data unavailable: {type(e).__name__}: {e}"
    try:
        _parts = _pp.chunks(text)
    except Exception:
        _parts = [text[i:i + 1900] for i in range(0, len(text), 1900)]
    for _part in _parts:
        await interaction.followup.send(_part)


@tree.command(name="pp-fetch", description="ParlayPlay only: read ParlayPlay now, score new props, update the board")
async def slash_pp_fetch(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    import asyncio as _aio
    try:
        proc = await _aio.create_subprocess_exec(
            sys.executable, os.path.join(_PP_HOME, "apex_parlayplay.py"), "run",
            cwd=_PP_HOME, stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.STDOUT)
        out, _ = await _aio.wait_for(proc.communicate(), timeout=300)
        last = [l for l in out.decode(errors="replace").splitlines() if l.strip() and "Warning" not in l and "warnings.warn" not in l]
        msg = last[-1] if last else "done"
    except Exception as e:
        msg = f"failed: {type(e).__name__}: {e}"
    await interaction.followup.send(f"🟪 **ParlayPlay fetch** -- {msg[:1800]}")


@tree.command(name="pp-best", description="ParlayPlay only: 70%+ Win Probability props, one per match, best first")
async def slash_pp_best(interaction: discord.Interaction):
    await _slash_pp(interaction, "best")


@tree.command(name="pp-slip", description="ParlayPlay only: the PP Insured 3 slip, its record and the last 7 days")
async def slash_pp_slip(interaction: discord.Interaction):
    await _slash_pp(interaction, "slip")


@tree.command(name="pp-tests", description="ParlayPlay only: Insured 6 / 9 / 5 / 3 skip-close test records, last 7 days")
async def slash_pp_tests(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)   # APEX-PPINSURED-2026-10-23
    try:
        import apex_pp_insured as _ins
        text = await client.loop.run_in_executor(None, _ins.tests_text)
    except Exception as e:
        text = f"⚠️ ParlayPlay tests unavailable: {type(e).__name__}: {e}"
    for i in range(0, len(text), 1900):
        await interaction.followup.send(text[i:i + 1900])


@tree.command(name="pp-allprops", description="ParlayPlay only: every prop at 70%+ Win Probability, best first")
async def slash_pp_allprops(interaction: discord.Interaction):
    await _slash_pp(interaction, "all")


# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────

def main():
    if not DISCORD_BOT_TOKEN:
        print("[DiscordBot] ERROR: DISCORD_BOT_TOKEN not found in environment. "
              "Run 'env | grep -i discord' to confirm it's set, then try again.")
        sys.exit(1)
    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
