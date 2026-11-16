#!/usr/bin/env python3
"""
APEX-EDGE-TRACKER-2026-08-27 (v2)
=================================

Automated category performance tracking for the forward test. Everything here is
computed fresh from the files on every call, so a newly settled row changes every
summary the next time anything renders (Mission Control panel, engine card header,
--report, --post).

Groups (config, not derived):
  🔥 rate_model   Aces / Breaks / Double Faults
  🎲 sim_derived  Games Won / Sets / Tiebreaks / Fantasy (+ the other simulator-only props)
  ⚠️ total_games  Total Games (+ first-set games)

Status per group, by rule -- nothing is ever labelled an "edge" automatically:
  ⚪ Insufficient Data     settled < 25
  🟡 Early Positive Signal settled >= 25, hit rate above break-even, groups in research order
  🟢 Evidence Emerging     settled >= 100, above break-even, hit rate >= average stated probability

Data, merged and de-duplicated:
  apex_prop_sim_forward_log_v1.csv   settled rows (actual filled)
  apex_engine_picks_log_v1.csv       direction / tier / prob for those rows
  apex_engine_hand_settled_v1.csv    hand-graded picks the log never had; skipped when the
                                     same (name, category, line) is already settled in the log

CLI:
  --report      full report: groups, categories, TOP/WORST, probability buckets, ordering, ledger
  --mc          Mission Control panel text (what render_mission_control embeds)
  --discord     engine-card header block
  --post        post the tracker summary to Discord (#systems) via openclaw_core.send_discord
  --settle-log  write hand actuals into the forward log for rows flagged in_forward_log=1
  --selftest
"""
import csv, os, sys, json
from datetime import datetime


# APEX-LOGONCE-2026-10-30 (owner, "go clean logs"): retired-record lines repeated on every board
# build filled apex_scheduler.log to 17 GB. Each distinct line is printed once per process.
# Undo: RETIRE_LOG_ONCE = False.
RETIRE_LOG_ONCE = True
_RETIRE_SEEN = set()



def _failnote(name, err):   # APEX-FAILLOG-2026-11-01
    try:
        import apex_failopen as _fo
        _fo.note(name, err)
    except Exception:
        pass


def _retire_print(msg):
    if RETIRE_LOG_ONCE:
        if msg in _RETIRE_SEEN:
            return
        if len(_RETIRE_SEEN) > 200000:
            _RETIRE_SEEN.clear()
        _RETIRE_SEEN.add(msg)
    print(msg)

MARKER = "APEX-EDGE-TRACKER-2026-08-27"
HERE = os.path.dirname(os.path.abspath(__file__))
SIM_LOG  = os.path.join(HERE, "apex_prop_sim_forward_log_v1.csv")
PICK_LOG = os.path.join(HERE, "apex_engine_picks_log_v1.csv")
HAND_LOG = os.path.join(HERE, "apex_engine_hand_settled_v1.csv")

BREAK_EVEN = 0.542
EARLY_MIN, EMERGING_MIN = 25, 100
MIN_FOR_TOP = 2                      # settled picks a category needs before TOP/WORST considers it

# APEX-BESTFLOOR70-2026-09-30 (Best List Quality Floor): a prop must score at
# least this to appear in Best Demon / Best Goblin / Best Standard. Backed by
# the 2026-09-30 audit of settled history: Apex Score 70+ picks won 84.3%
# (n=83) vs. 55.5% (n=820) below 70 -- a 28.8-point gap. Does not touch
# apex_score() itself, Elite6, Top10, or any ranking formula -- see
# best_bets_detail()'s quality-floor block for where this is applied.
#
# APEX-DEMONFLOOR60-2026-10-01 (Demon Quality Floor Split, approved by
# owner): Demon split off the shared 70+ bar. The 2026-10-01 audit
# of settled Demon props specifically found 70+ hitting 76.5% (n=17) vs.
# 60-66 hitting 84.6% (n=13) -- the shared floor was excluding Demon props
# that historically outperformed it. Demon's higher payout tier doesn't
# need the same floor as Goblin/Standard, whose own audits were never
# Demon-specific. Goblin, Standard, and Elite6 keep 70+ unchanged.
# Eligibility ONLY -- apex_score(), rank_score(), and Apex-Score-descending
# ranking order are untouched everywhere; a 60-69 Demon pick is eligible,
# it never outranks a higher-scoring pick.
# APEX-FLOORSWEEP67-2026-10-07 (owner, approved production
# configuration): Threshold Replay study (2026-10-07) re-ran Top1/Top3/
# Top6/Elite6/Top10/Best Bets against the CURRENT production stack
# (Global25 blend + Aces band cal + EV x1.25 inside apex_score(), plus
# the category probability gates) across floors 65-70. 67 won the
# weighted composite score (40% Top3 WR / 25% Elite6 WR / 20% ROI / 15%
# board availability) over every other floor tested, including the
# previous 70: Top3 WR 89.8%->90.6%, Top3 ROI +71.4%->+72.9%, Elite6 WR
# 80.2%->80.6%, Elite6 ROI +53.2%->+54.0%, with more candidates per
# slate (9.91->15.35 avg pool/day) and no worse empty-board rate.
# Demon stays at 60 -- untouched, out of scope for this study.
# APEX-PROBRANK-2026-10-10 (owner, approved "go" 2026-10-10): every
# board now qualifies a prop ONLY if Win Probability >= 70% AND Apex Score
# >= 67, and ranks qualifying props by Win Probability descending. Apex
# Score is a pass/fail qualification check, never a sort key; Confidence,
# RANK, Edge and tier labels never order a board. Replay (9/24-10/9, 778
# settled picks, no-peek rescoring): Top3 70.7%->77.1%, Elite6 68.8%->73.0%,
# Top10 69.5%->72.4%. Demon raised 60->67 so all six boards share one rule.
BOARD_MIN_PROB = 0.70
BOARD_MIN_SCORE = 67
QUALITY_FLOOR_BY_TYPE = {"demon": 67, "goblin": 67, "standard": 67}   # was demon 60 (APEX-PROBRANK-2026-10-10)
DEFAULT_QUALITY_FLOOR = 67          # fallback floor for any other/unknown prop_type
ELITE6_QUALITY_FLOOR = 67           # Elite6 is explicitly unaffected by the Demon split
# APEX-TOP3TOP10FLOOR-2026-10-05 (Phase 6, owner): Top3 and Top10 had
# NO quality floor at all -- the existing _floor_dropped (score>50) and
# _qfloor_dropped (_quality_floor()) blocks below only run "if prop_type",
# and /top10 and /top3 call best_bets_detail with prop_type=None,
# category=None, so neither gate ever touched them. Confirmed live on
# 2026-09-29 (same incident write-up as APEX-BESTFLOOR-NOPADDING-2026-09-29)
# that this let a 54%-probability, score-47 pick slip onto a Best-list via
# the same gap; Top3/Top10 were never audited at the time and share the
# identical exposure. These two constants close that gap: never padded
# to reach 3 or 10 with a sub-70 pick -- a short board (fewer than the
# floor allows) is reported honestly, same pattern as _floor_dropped.
TOP3_FLOOR = 67    # was 70 -- APEX-PROBRANK-2026-10-10 (plus BOARD_MIN_PROB)
TOP10_FLOOR = 67   # was 70 -- APEX-PROBRANK-2026-10-10 (plus BOARD_MIN_PROB)


def _board_prob(d):
    """APEX-PROBRANK-2026-10-10: the Win Probability a board ranks and
    qualifies on -- the calibrated probability when one was logged (only
    ever lower than raw, e.g. Aces LESS), else the stated probability.
    Same value the category gate and apex_score() already read. Accepts a
    scored dict ({"p": pick}) or a bare pick."""
    p = d.get("p") if isinstance(d, dict) and isinstance(d.get("p"), dict) else d
    p = p or {}
    v = p.get("prob_cal")
    if v in (None, ""):
        v = p.get("prob")
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _board_qualifies(d):
    """APEX-PROBRANK-2026-10-10: the one qualification rule every board uses."""
    sc = d.get("sc", d.get("score"))
    try:
        sc = float(sc)
    except (TypeError, ValueError):
        return False
    return _board_prob(d) >= BOARD_MIN_PROB - 1e-9 and sc >= BOARD_MIN_SCORE


def _quality_floor(prop_type):
    """APEX-DEMONFLOOR60-2026-10-01: per-type Best-list eligibility floor.
    Demon reads 60, Goblin/Standard read 70, anything else falls back to 70."""
    return QUALITY_FLOOR_BY_TYPE.get(str(prop_type or "").strip().lower(), DEFAULT_QUALITY_FLOOR)

# APEX-FETCHRETIRE-2026-09-30 (Fetch-Based Prop Retirement): a /fetch run
# on this board reliably logs a burst of rows with no gap wider than this
# between consecutive scrapes; two separate /fetch runs are reliably 45+
# minutes apart (confirmed from this log's real timestamp history, see
# load_current_board()). MIN_FETCH_RUN_SIZE is a safety valve -- a run
# that logged fewer identities than this looks like a partial/failed
# scrape, not a real full fetch, so retirement is skipped for that load
# rather than risk wiping the board over one bad run.
FETCH_RUN_GAP_MINUTES = 45
MIN_FETCH_RUN_SIZE = 15

# APEX-FETCHRETIRE-DIAG-2026-09-30: read-only diagnostic snapshot of the
# most recent load_current_board() fetch-retirement pass, for validation/
# audit scripts only -- same pattern as openclaw_core.LAST_PLAYWRIGHT_
# SCRAPE_STATS. Nothing reads this to make a filtering decision; it exists
# purely so a Fetch Retirement Validation Audit can inspect exactly which
# run window and identity set the real code used, instead of a separate
# reimplementation that could silently drift from the real logic.
LAST_FETCH_RETIRE_DEBUG = {}

# APEX-BOARDHEALTH-2026-09-30: read-only diagnostic snapshot of the FULL
# current load_current_board() run -- every removal reason, in one place,
# for the Board Health Report to read. Same pattern as
# LAST_FETCH_RETIRE_DEBUG: nothing here is read by load_current_board()
# itself to decide what to keep or drop, so it cannot change the board.
# It reflects the board's current state at the moment it was last built
# (i.e. after whatever rows are in the pick log right now), not an
# isolated delta of one specific fetch.
LAST_BOARD_BUILD_DEBUG = {}
LAST_DATE_MOVES = []   # APEX-DATEMOVE-2026-10-25: props kept by the last load_current_board() after a match moved day

GROUPS = {   # order = the ordering the research predicts, best first
    "rate_model":  {"icon": "🔥", "title": "Direct Rate Model", "short": "Aces, Breaks, Double Faults",
                    "cats": ("aces", "breaks", "dfs")},
    "sim_derived": {"icon": "🎲", "title": "Simulator Derived", "short": "Games Won, Sets, Tiebreaks, Fantasy",
                    "cats": ("games_won", "games_lost", "sets_won", "total_sets", "tiebreaks", "any_tiebreak",
                             "fantasy_score", "holds", "svgms", "goes_3_sets", "goes_5_sets", "first_set_fav_wins")},
    "total_games": {"icon": "⚠️", "title": "Total Games", "short": "Total Games",
                    "cats": ("total_games", "first_set_games")},
}
EDGE_ZONE = ("aces", "breaks", "dfs")
# Every category a Discord card can reach after APEX-WIRING-2026-08-27. Coverage is
# measured against this list: anything here with zero settled picks is UNCHECKED.
TRACKED = ("aces", "breaks", "dfs", "games_won", "total_games", "fantasy_score", "sets_won", "total_sets",
           "tiebreaks", "first_set_games", "any_tiebreak", "first_set_fav_wins", "goes_3_sets", "goes_5_sets")
THIN = 5                             # settled picks below which a category is "under-tested"
ALWAYS_SHOW = ("aces", "breaks", "dfs", "games_won", "fantasy_score", "sets_won", "tiebreaks", "total_games")
LABEL = {"1st_set_total_games": "1st Set Games", "aces": "Aces", "breaks": "Breaks", "dfs": "Double Faults", "games_won": "Games Won", "games_lost": "Games Lost",
         "sets_won": "Sets Won", "total_sets": "Total Sets", "tiebreaks": "Tiebreaks", "fantasy_score": "Fantasy Score",
         "total_games": "Total Games", "first_set_games": "First Set Games", "any_tiebreak": "Any Tiebreak",
         "goes_3_sets": "Goes 3 Sets", "goes_5_sets": "Goes 5 Sets", "first_set_fav_wins": "First Set Winner",
         "holds": "Holds", "svgms": "Service Games"}


def _market_str(p):
    """APEX-PERBOARD-2026-10-01: one-line 'Market' string for a prop --
    category label + line + direction (e.g. 'Aces 5.5 MORE') -- used by
    the per-board Held Back / Closest To Qualifying / Next Best Options
    sections so a held-back or near-miss prop is identifiable without the
    full card. Pure formatting, reads nothing new."""
    cat = p.get("category")
    return "%s %s %s" % (LABEL.get(cat, cat or "?"), p.get("line", "?"), p.get("direction", "?"))


def _previously_shown_lines(p, fs_first, fs_last):
    """APEX-HELDBACKFIRST-2026-10-01: a prop whose first-seen timestamp
    differs from its last-seen timestamp was logged on more than one
    fetch -- i.e. genuinely 'previously shown on prior fetch'. Every prop
    reaching a held_back list here came from load_current_board(), which
    already drops already-started/already-graded rows before any board
    function sees them -- so a prop that's still in play is, by
    construction, always 'match has not started yet'. Never fabricates
    either claim: both lines only appear when the freshness index actually
    shows two distinct timestamps."""
    try:
        fs, ls, _hours, _status = freshness_for(p, fs_first, fs_last)
    except Exception:
        return []
    if fs and ls and fs != ls:
        return ["\U0001F512 Previously shown on prior fetch", "\U0001F7E2 Match has not started"]
    return []


def _held_back_card_lines(h, fs_first, fs_last, board_label=None):
    """APEX-HELDBACKFIELDS-2026-10-05 (Phase 3, owner): full field
    set for one held-back prop -- Original Rank, Match, Market, Line,
    MORE/LESS, Apex Score, Confidence, Original Board, Held Reason, Match
    Status. Match Status is unconditionally "Not Started": every prop
    reaching a held_back list here came from load_current_board(), which
    already drops already-started/already-graded rows before any board
    function ever sees them (see _previously_shown_lines) -- so this is a
    guarantee of the upstream pipeline, not a freshness-index guess."""
    p = h["p"]
    nm = p.get("opponent") if p.get("player") == "match" else p.get("player", "")
    vs = "" if p.get("player") == "match" else (" vs %s" % p.get("opponent") if p.get("opponent") else "")
    conf = h.get("conf")
    dir_icon = "\U0001F4C8" if str(p.get("direction", "")).upper() == "MORE" else "\U0001F4C9"
    L = ["\U0001F4CD Original Rank: #%s" % (h["rank"] if h["rank"] is not None else "?"),
         "\U0001F3BE Match: %s%s%s" % (no_book_mark(p), nm, vs),   # APEX-NOBOOK-2026-10-24
         start_time_line(p),   # APEX-STARTTIME-2026-10-22
         "\U0001F4CA Market: %s" % LABEL.get(p.get("category"), p.get("category") or "?"),
         "\U0001F4CF Line: %s" % p.get("line", "?"),
         "%s Direction: %s" % (dir_icon, p.get("direction", "?")),
         "\U0001F4C8 Apex Score: %s" % (round(h["score"]) if h["score"] is not None else "?"),
         "\u2b50 Confidence: %s/100" % (round(conf) if conf is not None else "?"),
         "\U0001F3C5 Original Board: %s" % (board_label or "?"),
         "\U0001F512 Held Reason: %s" % h.get("reason", "?"),
         "\U0001F7E2 Match Status: Not Started"]
    if _h2h_line(p):   # APEX-H2H-2026-10-22
        L.insert(3, _h2h_line(p))
    try:   # APEX-LINE175CHECK-2026-10-22: display only
        import apex_card_signals as _csig
        _l175 = _csig.line175_check(p)
        if _l175:
            L.append(_l175)
    except Exception:
        pass
    try:   # APEX-ACESCLOSE-2026-10-22: display only
        import apex_aces_close_papertest as _acpt
        _acl = _acpt.card_line(p)
        if _acl:
            L.append(_acl)
    except Exception:
        pass
    L.extend(_previously_shown_lines(p, fs_first, fs_last))
    return L


def _held_back_oneliner(h, fs_first, fs_last, board_label=None):
    """Compact single-line form of the same expanded field set, used for
    the rest of the Held Back list beyond the Top Held-Back Options
    highlight."""
    p = h["p"]
    nm = p.get("opponent") if p.get("player") == "match" else p.get("player", "")
    vs = "" if p.get("player") == "match" else (" vs %s" % p.get("opponent") if p.get("opponent") else "")
    conf = h.get("conf")
    dir_icon = "\U0001F4C8" if str(p.get("direction", "")).upper() == "MORE" else "\U0001F4C9"
    tag = ""
    prev = _previously_shown_lines(p, fs_first, fs_last)
    if prev:
        tag = "  \U0001F512 Previously shown on prior fetch"
    return ("· Rank #%s · %s%s · %s %s %s %s · Apex Score %s · Confidence %s/100 · "
            "Board: %s · Held: %s · \U0001F7E2 Not Started%s"
            % (h["rank"] if h["rank"] is not None else "?", nm, vs,
               LABEL.get(p.get("category"), p.get("category") or "?"), p.get("line", "?"),
               dir_icon, p.get("direction", "?"),
               round(h["score"]) if h["score"] is not None else "?",
               round(conf) if conf is not None else "?",
               board_label or "?", h.get("reason", "?"), tag))


def _demon_watchlist_lines(demon_watchlist, orig_rank, this_floor):
    """APEX-HELDBACKFIRST-2026-10-01: Demon Watchlist is explicitly KEPT
    (per owner' Held-Back-First Transparency Update) as its own
    labeled section for Demon props below the Demon quality floor, ALL of
    them, highest Apex Score first -- even though every entry here is
    also present in the generic Held Back From This Board section below.
    Factored out so both the normal (non-empty) render path AND the
    empty-board fallback can show it with identical formatting."""
    L = ["\U0001F440 **DEMON WATCHLIST** (ALL Demon props below the floor, highest Apex Score first):"]
    for w in sorted(demon_watchlist, key=lambda d: -d["sc"]):
        wp = w["p"]
        wnm = wp["opponent"] if wp.get("player") == "match" else wp.get("player", "")
        miss = this_floor - w["sc"]
        L.append("\U0001F4CD Rank #%s \u00b7 \U0001F3BE %s \u00b7 \U0001F3AF %s \u00b7 \U0001F4C8 Apex Score: %d \u00b7 "
                  "\u26a0\ufe0f Missed Demon floor by %d point%s"
                  % (orig_rank.get(id(w), "?"), wnm, _market_str(wp), round(w["sc"]),
                     round(miss), "" if round(miss) == 1 else "s"))
    return L


def _held_back_section_lines(held_back, fs_first=None, fs_last=None, header=None, min_shown=6, cap=10, top_n=3, board_label=None):
    """APEX-HELDBACKFIRST-2026-10-01 (Held-Back-First Transparency Update):
    the one shared renderer for every board's Held Back section. This is
    now the ONLY empty-board fallback (the old 'Closest To Qualifying'
    near-miss view is retired) AND the always-present end-of-board section
    -- same data, same formatting, so a board never shows two different
    held-back views. Always sorted by Apex Score descending (SORT ORDER),
    shows at least `min_shown` props when that many exist (never padded),
    and highlights the top `top_n` as TOP HELD-BACK OPTIONS in full-card
    form before the rest in compact form."""
    header = header or "\U0001F6A7 HELD BACK FROM THIS BOARD"
    if not held_back:
        return [header, "  No qualified held-back props available."]
    ranked = sorted(held_back, key=lambda h: (-_board_prob(h), -(h["score"] if h["score"] is not None else -1)))   # APEX-PROBRANK-2026-10-10
    shown = ranked[:max(min_shown, 0)] if len(ranked) <= cap else ranked[:cap]
    shown = ranked[:cap] if len(ranked) >= cap else ranked[:max(min_shown, len(ranked))]
    L = ["%s (%d prop%s -- minimum 6 shown, highest Apex Score first):"
         % (header, len(held_back), "" if len(held_back) == 1 else "s")]
    L[0] = L[0].replace("highest Apex Score first", "highest Win Probability first")   # APEX-PROBRANK-2026-10-10
    top, rest = shown[:top_n], shown[top_n:]
    if top:
        L.append("")
        L.append("\U0001F3C6 **TOP HELD-BACK OPTIONS**")
        for h in top:
            L.append("")
            L.extend(_held_back_card_lines(h, fs_first, fs_last, board_label))
    if rest:
        L.append("")
        for h in rest:
            L.append(_held_back_oneliner(h, fs_first, fs_last, board_label))
    if len(held_back) > len(shown):
        L.append("(+%d more held-back prop(s) not shown)" % (len(held_back) - len(shown)))
    return L


def _legacy_picks_for_board(flag, scored_pool, limit=6):
    """APEX-LEGACYPICKS-2026-10-05 (Phase 2, owner): props that
    were recommended on an EARLIER day's SAME board, have not settled
    yet, and are STILL present on TODAY's qualifying pool for this
    exact board (same player + category + line) -- a genuinely
    re-confirmed recommendation, never a stale one carried over by
    assumption. Separate section from Held Back (which is about
    today's board only) -- never merged with it, per spec.

    Honest limitation, stated here and in the display: this system has
    no live match-clock/live-score feed, so "still available" is
    judged by (a) no settled WIN/LOSS yet for that exact prop and (b)
    still appearing in today's qualifying pool for this board -- not
    by a verified match-start timestamp. A match that is mid-play but
    genuinely unsettled could still show up here; the Match Status
    label says "Not Yet Settled", never "Not Started", for exactly
    this reason. Fail-open: any error returns [] rather than raising,
    since this is a supplementary section, not core board output."""
    try:
        import apex_learning_engine as _ale
        rows = _ale.load_board_membership_log()
    except Exception:
        return []
    if not rows:
        return []
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        settled_keys = {
            (p["match_date"], _norm(p["name"]), p["category"], str(p["line"]))
            for p in settled_picks() if p.get("hit") is not None
        }
    except Exception:
        settled_keys = set()
    cur_by_key = {}
    for d in scored_pool:
        p = d["p"]
        k = (_norm(p.get("player")), p.get("category"), str(p.get("line")))
        cur_by_key.setdefault(k, d)
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r.get("date", ""), reverse=True):
        rdate = r.get("date") or ""
        if not rdate or rdate >= today_str:
            continue
        # APEX-ALLPICKSLEGACY-2026-10-05 (Phase 8): flag=None means "appeared
        # on ANY board that day" -- used by /allpicks, which has no single
        # board-membership column of its own (it is the union of all boards).
        if flag is None:
            if not any(str(r.get(f)) in ("1", "True", "true")
                       for f in ("elite6", "top10", "top3", "bestgoblin", "bestdemon", "beststandard")):
                continue
        elif str(r.get(flag)) not in ("1", "True", "true"):
            continue
        try:
            cat = _ale.canon_cat(r.get("category"))
        except Exception:
            cat = r.get("category")
        line_str = str(r.get("line"))
        settled_key = (rdate, _norm(r.get("player")), cat, line_str)
        if settled_key in settled_keys:
            continue
        cur_key = (_norm(r.get("player")), cat, line_str)
        if cur_key in seen:
            continue
        d = cur_by_key.get(cur_key)
        if not d:
            continue
        seen.add(cur_key)
        out.append({"orig_date": rdate, "scored": d})
        if len(out) >= limit:
            break
    return out


def _legacy_picks_section_lines(entries, header="📚 PREVIOUSLY RECOMMENDED & STILL AVAILABLE"):
    """Display per Phase 2 spec: Player, Market, Line, MORE/LESS, Apex
    Score, Confidence, Original Rank (within today's re-qualification,
    since the original day's rank isn't stored), First Recommended
    date, Match Status (see honest-limitation note above)."""
    if not entries:
        return []
    L = ["", header]
    for i, e in enumerate(entries, 1):
        d = e["scored"]; p = d["p"]
        dir_icon = "📈" if str(p.get("direction", "")).upper() == "MORE" else "📉"
        name = p["opponent"] if p.get("player") == "match" else p.get("player")
        vs = "" if p.get("player") == "match" else (" vs %s" % p.get("opponent") if p.get("opponent") else "")
        L.append("  %d. 🎾 %s%s" % (i, name, vs))
        L.append("     📊 %s %s %s %s" % (LABEL.get(p.get("category"), p.get("category")),
                  p.get("line"), dir_icon, p.get("direction")))
        L.append("     📈 Apex Score %s · ⭐ Confidence %s/100 · 🏅 Current Rank #%d"
                  % (d.get("sc", "?"), d.get("conf", "?"), i))
        L.append("     ⏰ First Recommended: %s · 🟢 Match Status: Not Yet Settled "
                  "(no live match-clock feed -- judged by settlement status, not a verified start time)"
                  % e["orig_date"])
    return L


STATUS = {"none": "⚪ Insufficient Data", "early": "🟡 Early Positive Signal", "emerging": "🟢 Evidence Emerging"}
# Per-category status (user rules 2026-08-27), applied in this precedence:
#   0 settled                                  -> 🚫 NEVER TESTED
#   < CAT_MIN settled                          -> ⚪ INSUFFICIENT DATA
#   >= 25 settled and below break-even         -> ❌ NO POSITIVE SIGNAL
#   >= 10 settled and actual < expected wins   -> ⚠️ UNDERPERFORMING
#   >= 5 settled, actual >= expected, above BE -> 🔥 POTENTIAL EDGE SIGNAL  (tagged "early" below 25)
#   otherwise                                  -> ⚪ INSUFFICIENT DATA
CAT_MIN, CAT_UNDER_MIN, CAT_NEG_MIN = 5, 10, 25
CSTAT = {"never": "🚫 NEVER TESTED", "insuff": "⚪ INSUFFICIENT DATA", "neg": "❌ NO POSITIVE SIGNAL",
         "under": "⚠️ UNDERPERFORMING", "signal": "🔥 POTENTIAL EDGE SIGNAL"}
QUALIFIERS = "⚪ Early Signal Only   ⚪ Not Proven   ⚪ Forward Test Active"
TIER_ICON = {"elite": "🟢", "good": "🟡", "caution": "🟠", "low": "🔴"}
BUCKETS = (("70%+", 0.70, 1.01), ("65-70", 0.65, 0.70), ("60-65", 0.60, 0.65), ("55-60", 0.55, 0.60),
           ("50-55", 0.50, 0.55), ("<50", 0.0, 0.50))
_STATIC_TIER = {"aces": "good", "breaks": "elite", "dfs": "caution", "games_won": "elite", "total_games": "caution"}


def group_of(cat):
    for g, spec in GROUPS.items():
        if cat in spec["cats"]:
            return g
    return "sim_derived"


BO5_CUTOFF = "2026-08-29"   # BO5-FWD-001: hand rows on/after this date must carry best_of (3 or 5)


def _parse_best_of_hand(r):
    """Returns (is_bo5, is_legacy). Refuses loudly on bad input — never guesses silently."""
    v = str(r.get("best_of") or "").strip()
    if v.endswith(".0"):
        v = v[:-2]
    if v == "5":
        return True, False
    if v == "3":
        return False, False
    if v == "":
        if str(r.get("match_date") or "")[:10] >= BO5_CUTOFF:
            raise SystemExit(
                "REFUSED: hand row dated %s (player=%s, category=%s) has no best_of; "
                "rows on/after %s require best_of 3 or 5. Nothing computed."
                % (r.get("match_date"), r.get("player"), r.get("category"), BO5_CUTOFF))
        return False, True
    raise SystemExit("REFUSED: invalid best_of %r (player=%s, category=%s); allowed values: 3 or 5."
                     % (v, r.get("player"), r.get("category")))


def _norm(s):
    return " ".join(str(s or "").lower().replace("-", " ").split())


def _read(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ================================================================ APEX-FRESHNESS-2026-09-29
# PROP FRESHNESS SYSTEM. Requested after the Demon Availability Audit proved
# that /bestdemon (and every other board view) can show props that haven't
# been re-checked against PrizePicks in many hours -- the day's scheduled
# scrape can fail (confirmed: API 403 + no Chrome on :9222 for the Playwright
# fallback) and the board just keeps serving whatever it last had, silently.
#
# This is a READ-ONLY, INFORMATIONAL layer. Per the request:
#   "Do not remove props yet. Simply surface freshness information."
# Nothing here filters, drops, or reorders a single pick. It only labels.
#
# Freshness is derived from apex_engine_picks_log_v1.csv's own "ts" column --
# the timestamp the engine logs every time it evaluates a prop, which only
# happens right after a successful PrizePicks fetch (API or Playwright). A
# prop's "last confirmed" time is the newest ts ever logged for that exact
# (match_id, player, category, line, prop_type) key. If that key was only
# ever logged once, first_seen == last_seen == last_confirmed -- meaning it
# has never been re-checked since the moment it first appeared.
#
# "Last successful PrizePicks sync" (board-level) is the newest ts anywhere
# in the whole log -- i.e. the last time ANY prop was successfully evaluated,
# which only happens after a successful fetch. This needs no changes to the
# fetch pipeline in openclaw_core.py -- it's derived purely by reading data
# that already exists, so it carries zero risk to the scraper itself.

FRESHNESS_FRESH_HOURS = 3.0    # < 3h since last confirmed  -> FRESH
FRESHNESS_AGING_HOURS = 8.0    # 3-8h                        -> AGING; >8h -> STALE
SYNC_WARNING_HOURS    = 6.0    # board-level: warn if no successful sync in this long
# All three are plain module constants -- adjust here if the real refresh
# cadence (currently: once daily, ~5:00 PM CT) changes.

_FRESHNESS_CACHE = {"mtime": None, "first_seen": {}, "last_seen": {}, "max_ts": None}


def _freshness_key(r):
    """Same STABLE PROP IDENTITY load_current_board() dedups on (player,
    opponent, category, line -- APEX-PROPID-2026-09-30), so freshness
    follows the one active record for a real-world prop, not a frozen
    snapshot of whichever tier/match_id it happened to carry at some
    earlier scrape. Before this fix, a prop whose tier changed kept
    reporting the OLD tier's last-seen time forever (e.g. "STALE --
    35.9h ago" on a prop that had in fact just been rescraped under its
    new tier minutes earlier) because match_id/prop_type were part of
    the key. Fail open to the old field set if identity lookup errors."""
    try:
        return _apex_prop_identity(r)
    except Exception:
        return (r.get("match_id"), r.get("player"), r.get("category"),
                str(r.get("line")), r.get("prop_type"))


def _load_freshness_index(path=None):
    """One pass over the full pick log: first-seen and last-seen ts per exact
    pick key, plus the single newest ts anywhere in the file (the last-
    successful-sync proxy). Cached by file mtime, so rendering several board
    views back-to-back in one process doesn't re-scan a growing CSV every
    time -- only re-reads when the log has actually changed on disk."""
    path = path or PICK_LOG
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}, {}, None
    if _FRESHNESS_CACHE["mtime"] == mtime:
        return (_FRESHNESS_CACHE["first_seen"], _FRESHNESS_CACHE["last_seen"],
                _FRESHNESS_CACHE["max_ts"])
    first_seen, last_seen, max_ts = {}, {}, None
    for r in _read(path):
        ts = r.get("ts") or ""
        if not ts:
            continue
        if str(r.get("price_source") or "").startswith("reprice:"):
            continue   # APEX-REPRICE-2026-10-11: background re-score, not a PrizePicks confirmation
        k = _freshness_key(r)
        if k not in first_seen or ts < first_seen[k]:
            first_seen[k] = ts
        if k not in last_seen or ts > last_seen[k]:
            last_seen[k] = ts
        if max_ts is None or ts > max_ts:
            max_ts = ts
    _FRESHNESS_CACHE.update({"mtime": mtime, "first_seen": first_seen,
                              "last_seen": last_seen, "max_ts": max_ts})
    return first_seen, last_seen, max_ts


def freshness_for(pick, first_seen=None, last_seen=None):
    """(first_seen_ts, last_seen_ts, hours_since, status) for one pick dict
    as produced by load_current_board(). status is FRESH / AGING / STALE /
    UNVERIFIED. Never raises -- any lookup or parse failure degrades to
    UNVERIFIED rather than crashing a board render."""
    if first_seen is None or last_seen is None:
        first_seen, last_seen, _ = _load_freshness_index()
    # APEX-PROPID-2026-09-30: use the same stable identity as everywhere
    # else (was inlining the OLD match_id/prop_type key here directly,
    # which _freshness_key() itself had already been fixed to stop using
    # -- this duplicate inline copy was missed in that pass and kept every
    # lookup here landing on the empty UNVERIFIED path).
    k = _apex_prop_identity(pick)
    fs, ls = first_seen.get(k), last_seen.get(k)
    if not ls:
        return None, None, None, "UNVERIFIED"
    try:
        last_dt = datetime.fromisoformat(ls)
        hours = (datetime.now() - last_dt).total_seconds() / 3600.0
    except Exception:
        return fs, ls, None, "UNVERIFIED"
    if hours < FRESHNESS_FRESH_HOURS:
        status = "FRESH"
    elif hours < FRESHNESS_AGING_HOURS:
        status = "AGING"
    else:
        status = "STALE"
    return fs, ls, hours, status


_FRESHNESS_ICON = {"FRESH": "\U0001F7E2", "AGING": "\U0001F7E1",
                    "STALE": "\U0001F534", "UNVERIFIED": "\u26aa"}


def freshness_line(pick, first_seen=None, last_seen=None):
    """One ready-to-print line for a pick card. Never returns None."""
    fs, ls, hours, status = freshness_for(pick, first_seen, last_seen)
    icon = _FRESHNESS_ICON.get(status, "\u26aa")
    if status == "UNVERIFIED" or hours is None:
        return "%s Freshness: UNVERIFIED -- no PrizePicks confirmation timestamp on file" % icon
    age = ("%.1fh ago" % hours) if hours >= 0.05 else "just now"
    if fs == ls:
        seen_note = "seen once, never re-confirmed"
    else:
        seen_note = "first seen %s" % fs.replace("T", " ")
    return "%s Freshness: %s -- last confirmed %s (%s)" % (icon, status, age, seen_note)


def last_successful_sync(path=None):
    """(ts_str, hours_ago) for the newest ts anywhere in the pick log -- the
    best available proxy for 'last successful PrizePicks sync', since a ts
    only gets written right after a successful fetch. (None, None) if the
    log is empty or missing."""
    _, _, max_ts = _load_freshness_index(path)
    if not max_ts:
        return None, None
    try:
        hours = (datetime.now() - datetime.fromisoformat(max_ts)).total_seconds() / 3600.0
    except Exception:
        return max_ts, None
    return max_ts, hours


def sync_status_line():
    """'Last successful PrizePicks sync: ...' line for every board header."""
    ts, hours = last_successful_sync()
    if ts is None:
        return "\U0001F504 Last successful PrizePicks sync: unknown (no sync data on file)"
    disp = ts.replace("T", " ")
    if hours is None:
        return "\U0001F504 Last successful PrizePicks sync: %s" % disp
    return "\U0001F504 Last successful PrizePicks sync: %s (%.1fh ago)" % (disp, hours)


def sync_warning_banner():
    """Warning banner text, or '' when the last sync is within
    SYNC_WARNING_HOURS. Presentation only -- callers place it, this never
    filters or removes anything."""
    ts, hours = last_successful_sync()
    if ts is None:
        return ("\u26a0\ufe0f **NO SYNC DATA ON FILE** -- cannot confirm props were ever "
                "pulled from PrizePicks. Nothing removed; treat every pick below as "
                "UNVERIFIED until this is resolved.")
    if hours is None or hours < SYNC_WARNING_HOURS:
        return ""
    return ("\u26a0\ufe0f **NO SUCCESSFUL PRIZEPICKS SYNC IN %.1f HOURS** (last: %s) -- "
            "props below have not been re-checked against PrizePicks since then. "
            "Nothing has been removed; treat anything AGING/STALE with extra care."
            % (hours, ts.replace("T", " ")))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tier(cat, given=None):
    if given:
        return str(given).lower()
    try:
        import apex_prop_engine as _e
        return _e.tier_of(cat)[0]
    except Exception:
        return _STATIC_TIER.get(cat, "good")


# ------------------------------------------------------------------ ledger
def settled_picks(sim_log=SIM_LOG, pick_log=PICK_LOG, hand_log=HAND_LOG):
    """Every settled pick, oldest first. Fields: source, match_date, settled_ts, name, opponent,
    category, tier, line, direction, prob, actual, hit (True/False, None = push)."""
    out, keys, legacy_n = [], set(), 0
    # APEX-SETTLEKEY-2026-10-07: line added to the key -- without it, multiple
    # alternate lines (goblin/standard/demon tiers) on the same match/category
    # collapsed onto one pick_log row, silently misapplying one line's prob/
    # direction to every other line for that match. Root-caused via audit
    # (2026-10-06/07): corrupted total_games (-11.5pt calibration gap -> +0.1pt)
    # and fantasy_score (-8.5pt -> -1.7pt) calibration. See APEX-FULLTIER-LOGGING-
    # 2026-09-29 in openclaw_core.py, which fixed the identical bug class on the
    # write path but never got mirrored here on the read path.
    picks = {(r.get("match_id"), _norm(r.get("player")), r.get("category"), r.get("line")): r for r in _read(pick_log)}
    # APEX-WTA-AS-ATP-EXCLUDE-2026-10-15 (owner, "Go 333" 2026-10-15):
    # WTA props auto-eval / re-price scored with men's numbers (10/13-10/15)
    # are left out of every record, win rate and calibration fit. Only keys
    # whose newest pick-log row is still the ATP-scored one are listed (a later
    # /fetch or the 10/15 re-score already corrected the rest). Logs untouched.
    try:
        with open(os.path.join(HERE, "apex_wta_as_atp_exclusions.json")) as _xf:
            _wta_x = {tuple(k) for k in json.load(_xf).get("keys") or []}
    except Exception:
        _wta_x = set()
    # APEX-SURFACE-EXCLUDE-2026-10-22 (owner, "Go"): props scored on a surface that
    # disagrees with the stats warehouse (hard events scored as clay, Lyon/Mallorca/Lisbon
    # city mix-ups) are left out of records, win rates and the Honest fit. Logs untouched.
    try:
        with open(os.path.join(HERE, "apex_wrong_surface_exclusions.json")) as _sf:
            _wta_x |= {tuple(k) for k in json.load(_sf).get("keys") or []}
    except Exception:
        pass
    for r in _read(sim_log):
        if not r.get("actual") or not r.get("line"):
            continue
        if _wta_x and (r.get("match_id"), _norm(r.get("player")), r.get("category"), r.get("line")) in _wta_x:
            continue
        line, actual = _f(r["line"]), _f(r["actual"])
        po, pu = _f(r.get("p_over")) or 0.0, _f(r.get("p_under")) or 0.0
        pk = picks.get((r.get("match_id"), _norm(r.get("player")), r.get("category"), r.get("line")))
        if pk and pk.get("direction") in ("MORE", "LESS"):
            direction, prob, tier = pk["direction"], _f(pk.get("prob")), pk.get("tier")
        else:
            direction = "MORE" if po >= pu else "LESS"
            prob, tier = ((max(po, pu) / (po + pu)) if (po + pu) > 0 else None), None
        name = r.get("player") if r.get("player") != "match" else (r.get("opponent") or "").split(" v ")[0]
        d = {"source": "log", "match_date": r.get("match_date") or "", "settled_ts": r.get("settled_ts") or "",
             "name": name, "opponent": r.get("opponent"), "category": r["category"], "tier": _tier(r["category"], tier),
             "line": line, "direction": direction, "prob": prob, "actual": actual,
             "prop_type": ((pk or {}).get("prop_type") or "untyped").strip().lower(),   # APEX-TYPEREC-TRK
             "prob_cal": _f((pk or {}).get("prob_cal"))}   # APEX-OPS-2026-10-10: read-only, for the probability correction fit
        d["bo5"] = str(r.get("best_of") or "").strip() in ("5", "5.0")
        d["hit"] = None if actual == line else ((actual > line) if direction == "MORE" else (actual < line))
        out.append(d)
        keys.add((r["category"], line, _norm(name)))
        for nm in (r.get("opponent") or "").split(" v "):
            keys.add((r["category"], line, _norm(nm)))
    for r in _read(hand_log):
        line, actual = _f(r.get("line")), _f(r.get("actual"))
        if line is None or actual is None:
            continue
        key = (r["category"], line, _norm(r.get("player")))
        if key in keys:
            continue
        d = {"source": r.get("source") or "hand", "match_date": r.get("match_date") or "", "settled_ts": "",
             "name": r.get("player"), "opponent": r.get("opponent"), "category": r["category"],
             "tier": _tier(r["category"], r.get("tier")), "line": line,
             "direction": (r.get("direction") or "MORE").upper(), "prob": _f(r.get("prob")), "actual": actual,
             "prop_type": (r.get("prop_type") or "untyped").strip().lower()}   # APEX-TYPEREC-TRK
        d["bo5"], _legacy = _parse_best_of_hand(r)
        legacy_n += 1 if _legacy else 0
        d["hit"] = None if actual == line else ((actual > line) if d["direction"] == "MORE" else (actual < line))
        out.append(d); keys.add(key)
    if legacy_n:
        import sys as _sys
        print("note: %d legacy hand row(s) without best_of assumed Bo3 (pre-%s)" % (legacy_n, BO5_CUTOFF), file=_sys.stderr)
    out.sort(key=lambda p: (p["match_date"], p["settled_ts"]))
    # APEX-SETTLEDEDUPE-2026-10-08 (owner, approved 2026-10-09): the
    # forward log keeps one row per re-simulation, so a prop fetched on several
    # days is settled several times. Count each real prop once in every record
    # (65 repeat copies were inflating category n and calibration). The log
    # itself is untouched; the first (oldest) copy is kept.
    _seen_prop, _uniq = set(), []
    for _p in out:
        _k = (_p["match_date"], _norm(_p.get("name")), _norm(_p.get("opponent")), _p["category"],
              _p["line"], _p["direction"], _p.get("prop_type"))
        if _k in _seen_prop:
            continue
        _seen_prop.add(_k); _uniq.append(_p)
    return _uniq


# ------------------------------------------------------------------ statistics
def _stat(picks):
    dec = [p for p in picks if p["hit"] is not None]
    w = sum(1 for p in dec if p["hit"]); l = len(dec) - w
    probs = [p["prob"] for p in dec if p["prob"] is not None]
    exp = sum(probs)
    last10 = dec[-10:]
    return {"w": w, "l": l, "n": len(dec), "push": len(picks) - len(dec),
            "rate": (w / len(dec)) if dec else None,
            "avg_prob": (sum(probs) / len(probs)) if probs else None,
            "expected": exp if probs else None, "actual": w,
            "cal_wins": (w - exp) if probs else None,
            "cal_pts": ((w / len(dec)) - (sum(probs) / len(probs))) if (dec and probs) else None,
            "last10": (sum(1 for p in last10 if p["hit"]), sum(1 for p in last10 if not p["hit"])),
            "recent": list(reversed(dec[-5:])), "picks": dec}


def records(picks=None):
    picks = settled_picks() if picks is None else picks
    bo5_picks = [p for p in picks if p.get("bo5")]      # BO5-FWD-001: separate evidence stream
    picks = [p for p in picks if not p.get("bo5")]      # every record below is Bo3-only
    rec = {g: _stat([p for p in picks if group_of(p["category"]) == g]) for g in GROUPS}
    rec["overall"] = _stat(picks)
    cats = {}
    for c in sorted(set(p["category"] for p in picks) | set(ALWAYS_SHOW)):
        cats[c] = _stat([p for p in picks if p["category"] == c])
    rec["cats"] = cats
    order = list(GROUPS)
    for i, g in enumerate(order):
        r = rec[g]; st = "none"
        if r["n"] >= EARLY_MIN and r["rate"] is not None and r["rate"] > BREAK_EVEN:
            below = [rec[h]["rate"] for h in order[i + 1:] if rec[h]["rate"] is not None]
            if all(r["rate"] >= b for b in below):
                st = "early"
                if r["n"] >= EMERGING_MIN and r["avg_prob"] is not None and r["rate"] >= r["avg_prob"]:
                    st = "emerging"
        r["status"] = st
    rec["overall"]["status"] = None
    elig = [(c, s) for c, s in cats.items() if s["n"] >= MIN_FOR_TOP]
    rec["top"] = max(elig, key=lambda cs: (cs[1]["rate"], cs[1]["w"]))[0] if elig else None
    rec["worst"] = min(elig, key=lambda cs: (cs[1]["rate"], -cs[1]["l"]))[0] if elig else None
    dec = rec["overall"]["picks"]
    rec["buckets"] = []
    for name, lo, hi in BUCKETS:
        b = [p for p in dec if p["prob"] is not None and lo <= p["prob"] < hi]
        rec["buckets"].append((name, sum(1 for p in b if p["hit"]), sum(1 for p in b if not p["hit"]),
                               (sum(p["prob"] for p in b) / len(b)) if b else None))
    hi = [p for p in dec if p["prob"] is not None and p["prob"] >= 0.60]
    lo = [p for p in dec if p["prob"] is not None and p["prob"] < 0.60]
    rec["hi_lo"] = (_stat(hi), _stat(lo))
    rm, sd, tg = rec["rate_model"]["rate"], rec["sim_derived"]["rate"], rec["total_games"]["rate"]
    rec["ordering"] = None if None in (rm, sd, tg) else (rm >= sd and rm >= tg)
    rec["coverage"] = coverage(cats, pending_counts())
    for c, st in cats.items():
        st["status"] = cat_status(st)
    ranked = [c for c, st in cats.items() if st["n"] > 0]
    rec["leaderboard"] = sorted(ranked, key=lambda c: (-(cats[c]["cal_wins"] or 0), -(cats[c]["rate"] or 0), -cats[c]["n"], c))
    neg = [c for c in ranked if cats[c]["n"] >= MIN_FOR_TOP and (cats[c]["cal_wins"] or 0) < 0]
    rec["watchlist"] = sorted(neg, key=lambda c: (cats[c]["cal_wins"], cats[c]["rate"]))[:3]
    rec["headline"] = headline_group(rec)
    b5 = _stat(bo5_picks)
    b5["cats"] = {c: _stat([p for p in bo5_picks if p["category"] == c])
                  for c in sorted({p["category"] for p in bo5_picks})}
    _ms = (25, 50, 100)
    b5["milestone_reached"] = sum(1 for m in _ms if b5["n"] >= m)
    b5["next_milestone"] = next((m for m in _ms if b5["n"] < m), None)
    rec["bo5"] = b5
    return rec


def cat_status(st):
    n = st["n"]
    if n == 0:
        return "never"
    if n < CAT_MIN:
        return "insuff"
    above_be = st["rate"] is not None and st["rate"] > BREAK_EVEN
    if n >= CAT_NEG_MIN and not above_be:
        return "neg"
    if n >= CAT_UNDER_MIN and st["cal_wins"] is not None and st["cal_wins"] < 0:
        return "under"
    if st["cal_wins"] is not None and st["cal_wins"] >= 0 and above_be:
        return "signal"
    return "insuff"


def cstat_text(st):
    t = CSTAT[st["status"]]
    if st["status"] == "signal" and st["n"] < EARLY_MIN:
        t += " (early, n<%d)" % EARLY_MIN
    return t


def headline_group(rec):
    """Which group the evidence currently points to: best calibration, then hit rate, among groups with data."""
    have = [g for g in GROUPS if rec[g]["n"] > 0]
    if not have:
        return None
    best = max(have, key=lambda g: ((rec[g]["cal_wins"] or 0), (rec[g]["rate"] or 0), rec[g]["n"]))
    return best


def pending_counts(sim_log=SIM_LOG):
    """Open (unsettled) forward-log rows per category."""
    out = {}
    for r in _read(sim_log):
        if r.get("line") and not r.get("actual"):
            out[r["category"]] = out.get(r["category"], 0) + 1
    return out


def coverage(cats, pending):
    """unchecked = zero settled; thin = fewer than THIN settled. Each entry: (category, settled, pending)."""
    unchecked = [(c, 0, pending.get(c, 0)) for c in TRACKED if cats.get(c, {"n": 0})["n"] == 0]
    thin = [(c, cats[c]["n"], pending.get(c, 0)) for c in TRACKED if 0 < cats.get(c, {"n": 0})["n"] < THIN]
    return {"unchecked": unchecked, "thin": thin, "tracked": len(TRACKED),
            "covered": sum(1 for c in TRACKED if cats.get(c, {"n": 0})["n"] > 0)}


# ------------------------------------------------------------------ formatting helpers
def _type_lines(rec):
    """APEX-TYPEREC-TRK-2026-09-09: record by book prop type. Logged since 2026-09-08;
    anything settled before that is untyped and reported as such, never guessed."""
    try:
        picks = [p for p in rec["overall"]["picks"] if p.get("hit") is not None]
        b = {}
        for p in picks:
            t = p.get("prop_type") or "untyped"
            s = b.setdefault(t, [0, 0]); s[0 if p["hit"] else 1] += 1
        typed = {k: v for k, v in b.items() if k != "untyped"}
        if not typed:
            return ["", "🎲 PROP TYPE — no settled picks carry a type yet (logging began 09-08)"]
        out = ["", "🎲 PROP TYPE"]
        for k in sorted(typed):
            w, l = typed[k]; n = w + l
            out.append("  %-12s %d–%d (%s) · 3-leg of these cashes %d%%" % (
                k, w, l, _pct(w / n), round((w / n) ** 3 * 100)))
        if b.get("untyped"):
            out.append("  %-12s %d–%d (pre-09-08, type not logged)" % ("untyped", *b["untyped"]))
        return out
    except Exception:
        return []


def _rs(r):
    return "%d–%d" % (r["w"], r["l"]) + (" (%d push)" % r["push"] if r.get("push") else "")


def _pct(x):
    return "—" if x is None else "%.0f%%" % (100 * x)


def _num(x, fmt="%.1f"):
    return "—" if x is None else fmt % x


def _cal(r):
    if r["cal_wins"] is None:
        return "—"
    return "%+.1f wins (%+.0f pts)" % (r["cal_wins"], 100 * r["cal_pts"])


def _next(r):
    if r["status"] == "none":
        return "%d settled; %d needed for 🟡" % (r["n"], EARLY_MIN)
    if r["status"] == "early":
        return "%d settled; %d needed for 🟢" % (r["n"], EMERGING_MIN)
    return "%d settled" % r["n"]


def _recent(r, k=5):
    return "  ".join(("✅ " if p["hit"] else "❌ ") + LABEL.get(p["category"], p["category"]) for p in r["recent"][:k]) or "—"


# ------------------------------------------------------------------ renderings
def group_block(g, rec, indent=" "):
    spec, r = GROUPS[g], rec[g]
    L = ["%s %s (%s)   %s" % (spec["icon"], spec["title"], spec["short"], STATUS[r["status"]]),
         "%sRecord %s · Win %s · Expected %s · Actual %d · Calibration %s · Last 10: %d–%d"
         % (indent, _rs(r), _pct(r["rate"]), _num(r["expected"]), r["actual"], _cal(r), r["last10"][0], r["last10"][1]),
         "%sRecent: %s" % (indent, _recent(r)),
         "%s%s" % (indent, _next(r))]
    return "\n".join(L)


def side_by_side(rec):
    """The three group records beside each other. The overall record is never shown on its own."""
    parts = ["%s %s %s (%s, n=%d)" % (GROUPS[g]["icon"], GROUPS[g]["title"], _rs(rec[g]), _pct(rec[g]["rate"]), rec[g]["n"]) for g in GROUPS]
    return " | ".join(parts) + "   ·   all groups together %s (%s)" % (_rs(rec["overall"]), _pct(rec["overall"]["rate"]))


def qualifiers(r):
    return "⚪ Early Signal Only · ⚪ Sample Size: %d · ⚪ Not Proven · ⚪ Forward Test Active" % r["n"]


def headline_block(rec, indent=" ", bold=False):
    """Front and center, every render: where the evidence currently points. Computed, not hard-coded --
    if another group overtakes the rate model on calibration, this block says so."""
    g = rec["headline"]
    B = "**" if bold else ""
    if g is None:
        return "%sWHERE THE EVIDENCE CURRENTLY POINTS\n%sno settled picks yet" % (indent, indent)
    r, spec = rec[g], GROUPS[g]
    L = ["%s%s %s%s%s — %s" % (indent, spec["icon"], B, spec["title"].upper(), B, " · ".join(LABEL[c] for c in spec["cats"][:4])),
         "%s%s%s (%s)%s · %+.1f wins vs expectation · last 10: %d–%d" % (indent, B, _rs(r), _pct(r["rate"]), B, (r["cal_wins"] or 0), r["last10"][0], r["last10"][1]),
         indent + qualifiers(r)]
    if g != "rate_model":
        L.append("%sNOTE: the evidence now points here, NOT at the rate-model hypothesis" % indent)
    L.append("%s(research liked it · holdout liked it · live results not contradicting it — everything else is secondary)" % indent
             if g == "rate_model" else "")
    return "\n".join(x for x in L if x)


def leaderboard_block(rec, indent=" "):
    L = ["🏆 CATEGORY LEADERBOARD   (ranked by wins above expectation)"]
    if not rec["leaderboard"]:
        L.append(indent + "no settled picks yet")
    for i, c in enumerate(rec["leaderboard"], 1):
        st = rec["cats"][c]; cw = st["cal_wins"] or 0
        L.append("%s%d. %-16s %-7s %+.1f wins %s expectation   %s" % (indent, i, LABEL.get(c, c), _rs(st), cw,
                                                                  "above" if cw >= 0 else "below", cstat_text(st)))
    return "\n".join(L)


def watchlist_block(rec, indent=" "):
    L = ["👀 WATCHLIST   (below expectation, %d+ settled)" % MIN_FOR_TOP]
    if not rec["watchlist"]:
        L.append(indent + "nothing below expectation yet")
    for i, c in enumerate(rec["watchlist"]):
        st = rec["cats"][c]
        reason = "largest underperforming category so far" if i == 0 else "below expectation"
        L.append("%s%-16s %-7s Expected %s  Actual %d   Reason: %s   %s" % (indent, LABEL.get(c, c), _rs(st), _num(st["expected"]),
                                                                           st["actual"], reason, cstat_text(st)))
    return "\n".join(L)


def category_table(rec):
    L = [" %-16s %-7s %5s %6s %6s %7s %4s  %s" % ("Category", "Record", "Win%", "Exp", "Act", "CalDiff", "n", "Status")]
    for c, s in sorted(rec["cats"].items(), key=lambda cs: (-(cs[1]["cal_wins"] if cs[1]["cal_wins"] is not None else -99),
                                                            -(cs[1]["rate"] if cs[1]["rate"] is not None else -1), -cs[1]["n"], cs[0])):
        if s["n"] == 0 and c not in ALWAYS_SHOW:
            continue
        L.append(" %-16s %-7s %5s %6s %6d %7s %4d  %s" % (LABEL.get(c, c), _rs(s), _pct(s["rate"]), _num(s["expected"]), s["actual"],
                                                        "—" if s["cal_wins"] is None else "%+.1f" % s["cal_wins"], s["n"], cstat_text(s)))
    top = LABEL.get(rec["top"], rec["top"]) if rec["top"] else "n/a"
    worst = LABEL.get(rec["worst"], rec["worst"]) if rec["worst"] else "n/a"
    ts = rec["cats"].get(rec["top"]) if rec["top"] else None
    ws = rec["cats"].get(rec["worst"]) if rec["worst"] else None
    L.append(" TOP CATEGORY   %s%s" % (top, (" %s" % _rs(ts)) if ts else ""))
    L.append(" WORST CATEGORY %s%s   (needs %d+ settled to qualify)" % (worst, (" %s" % _rs(ws)) if ws else "", MIN_FOR_TOP))
    return "\n".join(L)


def bucket_block(rec):
    parts = ["%s %d–%d" % (n, w, l) for n, w, l, _ in rec["buckets"] if (w + l)]
    hi, lo = rec["hi_lo"]
    verdict = "—"
    if hi["n"] and lo["n"]:
        verdict = "yes" if (hi["rate"] or 0) > (lo["rate"] or 0) + 0.02 else ("no" if (hi["rate"] or 0) < (lo["rate"] or 0) - 0.02 else "no difference")
    return "\n".join([" " + " · ".join(parts) if parts else " —",
                      " 60%%+ picks %s (%s, stated %s)   vs   <60%% picks %s (%s, stated %s)   → higher-probability picks doing better? %s"
                      % (_rs(hi), _pct(hi["rate"]), _pct(hi["avg_prob"]), _rs(lo), _pct(lo["rate"]), _pct(lo["avg_prob"]), verdict)])


def missing_block(rec, indent=" "):
    cv = rec["coverage"]
    L = ["%sMISSING DATA / UNCHECKED   (%d of %d tracked categories have any settled pick)" % (indent, cv["covered"], cv["tracked"]),
         "%s  %-18s %7s %5s  %s" % (indent, "Category", "settled", "open", "Status")]
    if cv["unchecked"]:
        for c, n, pend in cv["unchecked"]:
            L.append("%s  %-18s %7d %5d  %s" % (indent, LABEL.get(c, c), 0, pend,
                                               "⏳ LOGGED, AWAITING SETTLEMENT" if pend else "🚫 NEVER TESTED — no forward-test evidence"))
    else:
        L.append("%s  none — every tracked category has at least one settled pick" % indent)
    if cv["thin"]:
        L.append("%sUNDER-TESTED   (fewer than %d settled)" % (indent, THIN))
        for c, n, pend in cv["thin"]:
            L.append("%s  %-18s %d settled%s" % (indent, LABEL.get(c, c), n, ("   (%d pending)" % pend) if pend else ""))
    return "\n".join(L)


def ordering_line(rec):
    rm, sd, tg = rec["rate_model"], rec["sim_derived"], rec["total_games"]
    if rec["ordering"] is None:
        return " Rate model vs rest: not enough settled picks in every group yet"
    return " Rate model %s (n=%d)  vs  simulator %s (n=%d)  vs  total games %s (n=%d)   → rate model still ahead? %s%s" % (
        _pct(rm["rate"]), rm["n"], _pct(sd["rate"]), sd["n"], _pct(tg["rate"]), tg["n"],
        "yes" if rec["ordering"] else "no", "" if rm["n"] >= EARLY_MIN else "  (n small — direction only)")


def standing_report(rec=None):
    """The three group records side by side, every time. Never the overall record alone."""
    rec = rec or records()
    return "\n".join([MARKER + "  " + datetime.now().strftime("%Y-%m-%d %H:%M") + "   settled %d" % rec["overall"]["n"],
                      " " + side_by_side(rec)])


# ================================================================= APEX-BB-001
# APEX-TEST7-2026-10-08 (owner, approved 2026-10-09 thread "Apex scoring
# ablation replays"): production build = Probability + live Calibration at 1x.
# Walk-forward replay (18 slate days, production board rules) ranked this #1 of
# 8 builds: 72.9% vs 71.4% board WR, +21.1 units vs the prior build. Each
# removed term is behind its own switch so any one can be restored alone.
#   APEX_T7_MANUAL_BLEND  -> prior value 0.25 (APEX-GLOBALBLEND25)
#   APEX_T7_EV_SCALE      -> prior value 1.25 (APEX-EVCONTRIB125)
#   APEX_T7_BAND_ON       -> prior value True (APEX-BB band term)
#   APEX_T7_ATTN_IN_SCORE -> prior value True (+5 HIGH / +2 MEDIUM); the
#                            Attention LEVEL is still returned for display.
#   APEX_T7_LIVE_ACES     -> prior value False (hardcoded _ACES_BAND_CAL)
APEX_T7_MANUAL_BLEND = 0.0
APEX_T7_EV_SCALE = 1.0
APEX_T7_BAND_ON = False
APEX_T7_ATTN_IN_SCORE = False
APEX_T7_LIVE_ACES = True


# APEX-CALPLAYABLE-2026-10-14 / APEX-CALSCALE-2026-10-14 (owner, "Go
# both" 2026-10-14, Aces penalty audit): 78% of settled Aces were LESS on
# Goblin/Demon lines PrizePicks never offered, and the calibration term cost
# 3 Apex points per point of miss while the probability term moves 1.46 per
# point. Each switch restores the old behaviour alone:
#   APEX_CAL_PLAYABLE_ONLY -> False measures the gap on every settled pick
#   APEX_CAL_PTS_PER_PT    -> None restores 30/20 per 10 pts, capped -45/+20
APEX_CAL_PLAYABLE_ONLY = True
APEX_CAL_PTS_PER_PT = 45.0 / (0.85 - BREAK_EVEN) / 100.0   # 1.46: same slope as the probability term
APEX_CAL_CAP = (-45.0, 20.0)


def _cal_ev(cal_pts, weight):
    """Calibration term in Apex points for a measured gap (actual - stated)."""
    if APEX_CAL_PTS_PER_PT is None:
        sc = cal_pts / 0.10
        return (30.0 if sc < 0 else 20.0) * weight * max(-1.5, min(1.0, sc))
    return max(APEX_CAL_CAP[0], min(APEX_CAL_CAP[1], APEX_CAL_PTS_PER_PT * 100.0 * cal_pts * weight))


def _is_playable_settled(q):
    return not (str(q.get("direction") or "").upper() == "LESS"
                and str(q.get("prop_type") or "").lower() in _MORE_ONLY_TYPES)


def _playable_cal(rec, cat):
    """(cal_pts, n) for a category from its playable settled picks. Cached on rec."""
    cache = rec.setdefault("_playable_cal", {})
    if cat not in cache:
        dec = [q for q in ((rec.get("cats") or {}).get(cat) or {}).get("picks") or []
               if _is_playable_settled(q) and q.get("prob") is not None]
        cache[cat] = ((sum(1 for q in dec if q["hit"]) / len(dec) - sum(float(q["prob"]) for q in dec) / len(dec))
                      if dec else None, len(dec))
    return cache[cat]


def _aces_band_live(rec):
    """APEX-TEST7-2026-10-08: per-probability-band Aces cal_pts measured from
    the settled Bo3 Aces picks already inside rec (same 4 bands as the old
    hardcoded _ACES_BAND_CAL snapshot). Cached on rec."""
    if "_aces_band_live" in rec:
        return rec["_aces_band_live"]
    dec = ((rec.get("cats") or {}).get("aces") or {}).get("picks") or []
    if APEX_CAL_PLAYABLE_ONLY:   # APEX-CALPLAYABLE-2026-10-14
        dec = [q for q in dec if _is_playable_settled(q)]
    out = {}
    for lo, hi in ((50.0, 60.0), (60.0, 70.0), (70.0, 80.0), (80.0, 101.0)):
        b = [q for q in dec if q.get("prob") is not None and lo <= float(q["prob"]) * 100.0 < hi]
        out[(lo, hi)] = ((sum(1 for q in b if q["hit"]) / len(b) - sum(float(q["prob"]) for q in b) / len(b)) if b else None, len(b))
    rec["_aces_band_live"] = out
    return out


def apex_score(prob, cat, bo5, rec):
    """Evidence-weighted score 0-100. Probability anchors; forward evidence
    (sample-weighted) adjusts; structural prior shrinks as evidence grows;
    the pick's own probability band and attention close it out. Bo5 capped."""
    prob = float(prob or 0.0)
    cs = rec["cats"].get(cat) or _stat([])
    n = cs["n"]; w = min(n / 25.0, 1.0)
    up, down = [], []
    # APEX-BB-001b: 50 = break-even; decent picks land 50s-60s, strong 70+
    score = 50.0 + 45.0 * max(-1.0, min(1.0, (prob - BREAK_EVEN) / (0.85 - BREAK_EVEN)))
    if prob >= 0.65: up.append("stated %d%%" % round(prob * 100))
    if prob <= 0.55: down.append("thin stated probability %d%%" % round(prob * 100))
    # APEX-FIXED-CAL-MANUAL-20261006-2220: fixed per-category calibration
    # adjustments supplied directly by the operator (unverified against any
    # settlement file -- applied as-is on explicit instruction). Supersedes
    # the earlier APEX-FIXED-CAL-OCT5 constants.
    # APEX-GLOBALBLEND25-2026-10-07 (owner, approved production
    # configuration): Global 25% calibration blend, applied across ALL
    # categories. Replaces the fixed-manual-only EV adjustment above with
    # 75% weight on the measured calibration gap (cal_pts-derived, same
    # category as APEX-FIXED-CAL-MANUAL before it) and 25% weight on the
    # fixed manual constants it supersedes. Approved via the Final Post-Fix
    # Championship Replay (2026-10-07) on corrected (post join-bug-fix)
    # settled data: Global 25% beat Current Production, Global25+Aces62.5%,
    # and Prop-Specific decisively on both Top 3 and Elite 6 win rate/ROI.
    _FIXED_CAL_MANUAL = {
        "total_games":   8.1,
        "games_won":     2.1,
        "dfs":          -1.0,
        "fantasy_score": -1.6,
        "breaks":       -3.6,
        "aces":         -11.2,
    }
    GLOBAL_BLEND_FRAC = APEX_T7_MANUAL_BLEND   # was 0.25 (APEX-GLOBALBLEND25); 0.0 since APEX-TEST7-2026-10-08

    # APEX-ACESBAND-2026-10-07 (owner, approved production configuration):
    # Aces' calibrated-EV term is computed per PROBABILITY BAND instead of
    # per category. Root-cause study (2026-10-07, Top-6 crossover
    # investigation) found Aces' category-wide calibration gap (n=138,
    # cal_pts=-0.1155) smeared four very different bands together: 60-69%
    # is well-calibrated (gap -1.82pts) while 70-79% and 80%+ are both
    # badly overconfident (gaps -17.70 and -19.74pts) -- the opposite of
    # the "high-confidence Aces are suppressed" hypothesis this was built
    # to test. Band cal_pts values below are a snapshot from that replay
    # (n=47-53 per band); read-only replay confirmed Top 1 and Elite 6
    # unchanged, Top 3 / Top 6 / Best Bets 15 all improved, 0/253 sampled
    # non-Aces picks changed score. Every other category's math below is
    # untouched -- this block only changes which cal_pts value ev_calibrated
    # reads for cat == "aces".
    _ACES_BAND_CAL = {
        (50.0, 60.0):  (-0.1129, 52),
        (60.0, 70.0):  (-0.0182, 49),
        (70.0, 80.0):  (-0.1770, 47),
        (80.0, 101.0): (-0.1974, 53),
    }

    if APEX_T7_LIVE_ACES:   # APEX-TEST7-2026-10-08: live-measured band table
        _ACES_BAND_CAL = _aces_band_live(rec)

    def _aces_band_cal_pts(pct):
        for (blo, bhi), (cp, bn) in _ACES_BAND_CAL.items():
            if blo <= pct < bhi:
                return cp, bn
        return None, 0

    # APEX-CALPLAYABLE-2026-10-14: the calibration gap is measured on playable
    # picks only (no LESS on Goblin/Demon lines); the W-L record is untouched.
    _cal_pts_c, _cal_n_c = (cs.get("cal_pts"), n)
    if APEX_CAL_PLAYABLE_ONLY:
        _cal_pts_c, _cal_n_c = _playable_cal(rec, cat)
    _cal_w_c = min(_cal_n_c / 25.0, 1.0)
    if cat == "aces":
        _band_cal_pts, _band_n = _aces_band_cal_pts(prob * 100.0)
        if _band_n >= 5 and _band_cal_pts is not None:
            ev_calibrated = _cal_ev(_band_cal_pts, min(_band_n / 25.0, 1.0))
        else:
            ev_calibrated = 0.0
    elif _cal_n_c >= 5 and _cal_pts_c is not None:
        ev_calibrated = _cal_ev(_cal_pts_c, _cal_w_c)
    else:
        ev_calibrated = 0.0
    ev_fixed_manual = _FIXED_CAL_MANUAL.get(cat, 0.0)
    ev = ev_calibrated + GLOBAL_BLEND_FRAC * (ev_fixed_manual - ev_calibrated)
    # APEX-EVCONTRIB125-2026-10-07 (owner, approved production configuration):
    # Elite-6 root-cause + sensitivity study (2026-10-07) found the post-blend EV
    # contribution underweighted relative to what the calibration data supports --
    # scaling it by 1.25x improved Top 3 (78.0%->80.0%), Top 6 (67.74%->72.04%,
    # passing Probability Top 6 at 70.97%), and Elite 6 (75.76%->76.47%) together,
    # with no tradeoff found in testing. Only the final blended ev is scaled here --
    # GLOBAL_BLEND_FRAC, the gates, Aces band calibration, the probability term,
    # and the attention bonus below are all untouched.
    EV_CONTRIBUTION_SCALE = APEX_T7_EV_SCALE   # was 1.25 (APEX-EVCONTRIB125); 1.0 since APEX-TEST7-2026-10-08
    ev *= EV_CONTRIBUTION_SCALE
    score += ev
    if abs(ev) >= 0.5:
        (up if ev >= 0 else down).append("%s calibration %+.2f (n=%d)"
            % (LABEL.get(cat, cat), ev, n))
    if n < 5:
        down.append("%s barely tested (n=%d)" % (LABEL.get(cat, cat), n))
    g = group_of(cat)
    prior = {"rate_model": 8.0, "sim_derived": 3.0, "total_games": -8.0}.get(g, 0.0)
    pr = prior * (1.0 - w) * (0.5 if n < 5 else 1.0)
    score += pr
    if pr >= 2: up.append("rate-model category (research prior)")
    if pr <= -2: down.append("total games market territory (research prior)")
    for name, blo, bhi in (BUCKETS if APEX_T7_BAND_ON else ()):   # APEX-TEST7-2026-10-08: Band off
        if n < 5:
            # APEX-BB-002d: band records are earned by settled picks in OTHER
            # categories; an untested category inherits none of that credit.
            break
        if blo <= prob < bhi:
            for bn, bw_, bl_, bstat in rec["buckets"]:
                if bn == name and (bw_ + bl_) >= 3 and bstat:
                    br = bw_ / (bw_ + bl_)
                    adj = 10.0 * min((bw_ + bl_) / 10.0, 1.0) * max(-1.0, min(1.0, (br - bstat) / 0.10))
                    score += adj
                    (up if adj >= 2 else down if adj <= -2 else []).append(
                        "%s band %d-%d vs ~%d%% stated" % (name, bw_, bl_, round(bstat * 100)))
            break
    lvl, _w = attention(cat, prob, rec)
    if bo5: lvl = "LOW"
    if APEX_T7_ATTN_IN_SCORE:   # APEX-TEST7-2026-10-08: Attention is display-only
        score += {"HIGH": 5.0, "MEDIUM": 2.0}.get(lvl, 0.0)
    if n == 0:
        # APEX-BB-002g: warning, not cap. Score = belief; risk lives in the flag.
        down.append("⚠️ Untested Category -- no settled evidence (n=0)")
    elif n < 5:
        down.append("⚠️ Thin Evidence -- small sample (n=%d)" % n)
    if prob >= 0.95:
        # APEX-BB-002g: true score shown; the flag carries the risk. Near-certain
        # probabilities on tennis props are almost always broken inputs (08-29).
        down.append("⚠️ LIKELY ARTIFACT -- stated %d%%; verify the line before trusting anything about this pick" % round(prob * 100))
    if bo5:
        # APEX-BB-002g: no cap. Attention=LOW carries the earned-trust verdict;
        # the tag carries the risk; the score states the belief.
        down.append("⚠️ Experimental Format -- 0 settled Bo5 results; LOW attention until 25 (BO5-FWD-001)")
    score = max(0.0, min(100.0, score))
    ev_label = ("Very Low" if n < 5 else "Low" if n < 10 else
                "Moderate" if n < 25 else "Strong" if n < 100 else "Proven")
    return round(score), ev_label, lvl, up, down


def rank_score(apex, conf):
    """APEX-BB3-2026-08-31: ordering only. RANK = APEX x (CONFIDENCE / 100), k=1.
    Not a probability. Never feeds back into APEX SCORE or CONFIDENCE."""
    try:
        return round(float(apex) * (float(conf) / 100.0), 1)
    except (TypeError, ValueError):
        return 0.0


def _bb_sortkey(d, worst=False):
    """APEX-REVERT-2026-09-26: live ordering reverted to legacy APEX score
    while RANK is audited (Task 9: RANK cashed 1/6 vs APEX 4/6 over the
    first 6 settled board-days -- too small to trust, too consistent to
    ignore). Order: APEX score, then RANK, then probability, then edge.
    RANK/rank_score/slip_slot/confidence are STILL fully computed and
    logged upstream in best_bets_block/best_bets_detail -- this function
    only decides display/slip order, not what gets calculated or logged.
    worst=True ascends. Edge is stored projection-minus-line and is NOT
    direction-normalised -- known limitation, now the fourth tiebreak."""
    p = d.get("p") or {}
    def _f(v):
        try: return float(v or 0.0)
        except (TypeError, ValueError): return 0.0
    # APEX-PROBRANK-2026-10-10: Win Probability leads; Apex Score only
    # breaks exact probability ties, then RANK, then edge.
    k = (_board_prob(d), _f(d.get("score")), _f(d.get("rank")), _f(p.get("edge")))
    return k if worst else tuple(-x for x in k)


BB_RANK_LOG = "apex_bb_rank_log_v1.csv"
_BB_RANK_FIELDS = ["ts", "board_id", "source", "match_id", "player", "category",
                   "line", "direction", "probability", "apex_score", "confidence",
                   "rank_score", "rank_position", "legacy_apex_position",
                   "prob_cal",                   # APEX-ACESCAL-TRK-2026-09-09
                   "slip_slot", "slip_reason",   # APEX-SLIP-V1-2026-09-06 (mixed)
                   "slip_std_slot", "slip_std_reason", "slip_gob_slot", "slip_gob_reason"]   # APEX-SLIP-V11


# ================================================================ APEX-SLIP-V1-2026-09-06
# Slip Builder v1 -- a CONSTRUCTION layer, not a scoring layer. Walks the RANK-ordered
# board and fills 5 slip slots under diversification rules. Never reorders, never
# re-scores, never hides: displaced picks are named with the rule that displaced them.
SLIP_SLOTS = 5
# APEX-SLIP-V11-2026-09-08: long-match risk from the match price (fit on 48,678 Bo3 / 5,509 Bo5
# Sackmann+Pinnacle matches, holdout band error 0.010). A LESS pick on a rate category is a bet
# that the match stays short; above LONG_MATCH_LESS_MAX it is not slotted.
LONG_MATCH_LESS_MAX = 0.40
_LMR_BO3 = (-0.3623, -0.0832, -0.1378, -0.1081)   # const, lp, lp^2, wta
_LMR_BO5 = (0.4645, -0.0427, -0.1593)             # const, lp, lp^2

def long_match_risk(p):
    """P(3 sets) in Bo3 / P(4+ sets) in Bo5 from pinn_fav. None if no price."""
    try:
        import math
        pf = float(p.get("pinn_fav") or 0)
        if not (0.5 <= pf < 1.0): return None
        lp = math.log(pf / (1 - pf))
        bo5 = bool(p.get("bo5")) or str(p.get("best_of", "")) == "5"
        if bo5:
            c = _LMR_BO5; z = c[0] + c[1] * lp + c[2] * lp * lp
        else:
            c = _LMR_BO3; z = c[0] + c[1] * lp + c[2] * lp * lp   # wta term omitted (not on the row; -0.11 shifts <3 pts)
        return 1.0 / (1.0 + math.exp(-z))
    except Exception:
        return None

SLIP_KINDS = (("mixed", "MIXED SLIP (all prop types)"),
              ("standard", "STANDARD SLIP (standard props only)"),
              ("goblin", "GOBLIN SLIP (goblin props only -- MORE-only lines)"))

def _slip_ident(d):
    p = d["p"]
    cat = d.get("cat") or p.get("category", "")
    name = p.get("opponent", "") if p.get("player") == "match" else p.get("player", "")
    return cat, str(p.get("direction", "")).upper(), p.get("match_id", ""), str(name).lower(), name

def slip_builder(scored, kind="mixed"):
    """Returns {id(d): (slot|"", reason, rank_pos)} for every entry in scored (RANK order).
    Top 3: max 2 per category, max 2 per category+direction, never two from one match.
    Top 5: max 2 per category. One pick per player. Fail-open (caller wraps)."""
    out, slots = {}, []
    cat_n, catdir_n, matches, players = {}, {}, {}, set()
    for pos, d in enumerate(scored, 1):
        if len(slots) >= SLIP_SLOTS:
            out[id(d)] = ("", "", pos); continue
        cat, dr, mid, pl, _ = _slip_ident(d)
        nxt = len(slots) + 1
        why = ""
        _pt = str(d["p"].get("prop_type") or "").lower()          # APEX-SLIP-V11: slip type filter
        if kind == "standard" and _pt != "standard":
            out[id(d)] = ("", "", pos); continue
        if kind == "goblin" and _pt != "goblin":
            out[id(d)] = ("", "", pos); continue
        _risk = long_match_risk(d["p"])                             # APEX-SLIP-V11: length gate on LESS
        if dr == "LESS" and cat in ("dfs", "aces", "breaks") and _risk is not None and _risk >= LONG_MATCH_LESS_MAX:
            why = "long-match risk %d%% -- LESS props need a short match" % round(_risk * 100)
        if why:
            out[id(d)] = ("", why, pos); continue
        if pl in players:
            why = "player already holds a slip slot"
        elif mid and mid in matches:
            why = "same match as slip slot #%d" % matches[mid]
        elif nxt <= 3 and cat_n.get(cat, 0) >= 2:
            why = "category cap: %s already x2 in top 3" % cat
        elif nxt <= 3 and catdir_n.get((cat, dr), 0) >= 2:
            why = "category+direction cap: %s %s already x2 in top 3" % (cat, dr)
        elif nxt > 3 and cat_n.get(cat, 0) >= 2:
            why = "category cap: %s already x2 in top 5" % cat
        if why:
            out[id(d)] = ("", why, pos); continue
        slots.append(d); players.add(pl); matches[mid] = nxt
        cat_n[cat] = cat_n.get(cat, 0) + 1; catdir_n[(cat, dr)] = catdir_n.get((cat, dr), 0) + 1
        out[id(d)] = (nxt, ("promoted from RANK #%d" % pos) if nxt < pos else "", pos)
    return out

def _slip_block(scored, slip, title="SLIP CANDIDATES", kind="mixed", seen_displaced=None):
    """Render one slip section. Complete transparency: slots, then every displaced pick with its reason.

    APEX-BESTBETSAUDIT-2026-10-05 (owner, Best Bets Today audit):
    seen_displaced, shared across the three calls in _slip_sections, lets
    a slip whose Displaced list exactly matches one already printed in
    this message say so in one line instead of repeating 15-20 lines of
    identical category-cap text (MIXED and GOBLIN slips draw from the
    same top-ranked pool and very often produce an identical list). The
    first occurrence is still printed in full -- nothing is hidden, only
    an exact repeat is collapsed.
    """
    L = ["", "\U0001F3AF **%s** (Slip Builder v1.1 -- construction layer; RANK order above is untouched)" % title]
    slotted = sorted((v[0], d) for d in scored if (v := slip.get(id(d))) and v[0])
    if not slotted:
        if kind != "mixed" and not any(str(d["p"].get("prop_type") or "") for d in scored):
            L.append("-- prop types not logged for this board yet (populates from the next evaluation)")
        else:
            L.append("-- no eligible picks for this slip type")
        return L
    for slot, d in slotted:
        cat, dr, _, _, name = _slip_ident(d); p = d["p"]
        _, why, pos = slip[id(d)]
        _slotmedal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(slot, "\U0001F539")
        _slotdir = "\U0001F4C8" if str(dr).upper() == "MORE" else "\U0001F4C9"
        L.append("%s Slot %d \u00b7 %s -- %s %s %s %s \u00b7 %d%% \u00b7 Rank #%d%s%s" % (
            _slotmedal, slot, name, LABEL.get(cat, cat), p.get("line", ""), _slotdir, dr,
            round(float(p.get("prob", 0)) * 100), pos,
            (" (%s)" % why) if why else "", " \u26a0\ufe0f BO5 SAMPLE" if p.get("bo5") else ""))
    moved = [(slip[id(d)][2], d) for d in scored if slip.get(id(d)) and not slip[id(d)][0] and slip[id(d)][1]]
    if moved:
        _dkey = tuple(sorted(id(d) for _, d in moved))
        _dup_of = seen_displaced.get(_dkey) if seen_displaced is not None else None
        if _dup_of:
            L.append("Displaced: same %d prop(s) as the %s slip above" % (len(moved), _dup_of))
            return L
        if seen_displaced is not None:
            seen_displaced[_dkey] = title.split(" (")[0]
        L.append("Displaced:")
        for pos, d in moved:
            cat, dr, _, _, name = _slip_ident(d); p = d["p"]
            L.append("\u00b7 %s %s %s %s was RANK #%d -- %s" % (name, LABEL.get(cat, cat), p.get("line", ""), dr, pos, slip[id(d)][1]))
    return L

def _slip_sections(scored, picks=None):
    """All three slips + the shared rules footer. Returns (lines, {kind: slipmap})."""
    L, maps = [], {}
    _seen_displaced = {}
    for kind, title in SLIP_KINDS:
        if kind == "standard" and picks is not None:
            # APEX-STDSLIPMATCH-2026-10-12 (owner, "Should match"): the Standard
            # slip is the Best Standard board's own picks, in its order -- it used to
            # take any Standard prop from this board's ranking, with no Standard rule.
            _bs = best_standard_detail(picks, return_scored=True)
            _bs = (_bs.get("scored") or []) if isinstance(_bs, dict) else []
            maps[kind] = {id(d): (i, "", i) if i <= SLIP_SLOTS else ("", "", i) for i, d in enumerate(_bs, 1)}
            L.extend(_slip_block(_bs, maps[kind], "STANDARD SLIP (same picks as /beststandard)", kind))
            continue
        maps[kind] = slip_builder(scored, kind)
        L.extend(_slip_block(scored, maps[kind], title, kind, seen_displaced=_seen_displaced))
    L.append("_Slip rules: top 3 max 2 per category, max 2 per category+direction, one per match; top 5 max 2 per category; "
             "LESS on dfs/aces/breaks needs long-match risk under %d%%. Logged as slip\\_slot / slip\\_std\\_slot / slip\\_gob\\_slot for ALIS._" % round(LONG_MATCH_LESS_MAX * 100))
    return L, maps


def _bb_rank_log(scored, legacy_pos, source, path=None, slip=None, slip_std=None, slip_gob=None):
    """APEX-BB3: append-only ranking history for ALIS. Fail-open by contract --
    any failure here must never stop a board posting."""
    try:
        import datetime as _dt, hashlib as _hl
        path = path or BB_RANK_LOG
        keys = sorted("%s|%s|%s" % (d["p"].get("match_id"), d["p"].get("player"),
                                    d.get("cat") or d["p"].get("category"))
                      for d in scored)
        board_id = _hl.sha256("|".join(keys).encode("utf-8")).hexdigest()[:12]
        ts = _dt.datetime.now().isoformat(timespec="seconds")
        fresh = not os.path.exists(path)
        # APEX-SLIP-V1: one-time header migration (old rows keep blank slip columns; backup kept)
        if not fresh:
            with open(path, newline="", encoding="utf-8") as fh:
                _hdr = fh.readline().strip().split(",")
            if _hdr != _BB_RANK_FIELDS:
                import shutil as _sh
                _sh.copy2(path, path + ".bak_" + _dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_preslipcols")
                _old = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    _w = csv.DictWriter(fh, fieldnames=_BB_RANK_FIELDS); _w.writeheader()
                    for _r in _old:
                        _r.setdefault("slip_slot", ""); _r.setdefault("slip_reason", ""); _w.writerow(_r)
        slip = slip or {}; slip_std = slip_std or {}; slip_gob = slip_gob or {}
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=_BB_RANK_FIELDS)
            if fresh:
                w.writeheader()
            for pos, d in enumerate(scored, 1):
                p = d["p"]
                w.writerow({"ts": ts, "board_id": board_id, "source": source,
                            "match_id": p.get("match_id", ""),
                            "player": p.get("player", ""),
                            "category": d.get("cat") or p.get("category", ""),
                            "line": p.get("line", ""),
                            "direction": p.get("direction", ""),
                            "probability": p.get("prob", ""),
                            "prob_cal": p.get("prob_cal", p.get("prob", "")),
                            "apex_score": d.get("score", d.get("sc", "")),
                            "confidence": d.get("conf", ""),
                            "rank_score": d.get("rank", ""),
                            "rank_position": pos,
                            "legacy_apex_position": legacy_pos.get(id(d), ""),
                            "slip_slot": (slip.get(id(d)) or ("", "", 0))[0],
                            "slip_reason": (slip.get(id(d)) or ("", "", 0))[1],
                            "slip_std_slot": (slip_std.get(id(d)) or ("", "", 0))[0],
                            "slip_std_reason": (slip_std.get(id(d)) or ("", "", 0))[1],
                            "slip_gob_slot": (slip_gob.get(id(d)) or ("", "", 0))[0],
                            "slip_gob_reason": (slip_gob.get(id(d)) or ("", "", 0))[1]})
    except Exception:
        pass


UNPROVEN_CATEGORY_MIN_N = 25   # same "25 settled" bar as BO5-FWD-001
UNPROVEN_CATEGORIES = {"total_sets", "tiebreaks"}  # APEX-CAT-GATE-2026-09-27


def _category_has_evidence(cat, rec):
    """APEX-CAT-GATE-2026-09-27: board-governance rule, not a scoring rule
    (same category as _demote_bo5_from_top below -- APEX score/confidence
    untouched). Found during operational validation 2026-09-27: total_sets
    and tiebreaks were rendering on live boards with 0 settled picks ever
    (n=0, status "never" in records()) -- unlike a merely thin category,
    these have literally no evidence to warn about. Excluded entirely from
    ranking/Best Bets/Slip Builder until each has >= UNPROVEN_CATEGORY_MIN_N
    settled picks, at which point apex_score's normal evidence weighting
    (n/25 ramp) takes over exactly as it does for every other category --
    remove a category from UNPROVEN_CATEGORIES once it clears the bar."""
    if cat not in UNPROVEN_CATEGORIES:
        return True
    n = (rec.get("cats", {}).get(cat) or {}).get("n") or 0
    return n >= UNPROVEN_CATEGORY_MIN_N


# APEX-CATGATE-2026-10-07 (owner, approved production configuration):
# per-category minimum stated-probability gate, applied as board-governance
# eligibility -- same category as _category_has_evidence() above. A pick
# below its category's floor never reaches apex_score()/ranking; apex_score
# itself, rank_score(), and Apex-Score-descending order are untouched.
# Approved post-calibration-bug-fix via the Final Post-Fix Championship
# Replay (2026-10-07): Global 25% blend + these floors won Top 3 and
# Elite 6 decisively over Current Production, Global25+Aces62.5%, and
# Prop-Specific on corrected data.
CATEGORY_PROBABILITY_GATE = {
    "dfs":            0.60,
    "games_won":      0.55,
    "fantasy_score":  0.65,
    "aces":           0.65,
    "breaks":         0.65,
    "total_games":    0.65,
    "total_sets":     0.50,
    "tiebreaks":      0.60,
}


# APEX-DORMANT-GATE-HARDEN-2026-10-07: these categories are wired into
# _APEX_STAT_MAP / TRACKED but have never produced a single live pick
# (0 rows in PICK_LOG, 0 in the decline log, 0 settled, as of the
# Correctness Audit this session). CATEGORY_PROBABILITY_GATE has no entry
# for any of them, which meant a future prop in one of these categories
# would auto-pass the gate with no probability floor at all. Explicit
# disable, not a guessed floor: these are not approved for live board
# inclusion. Remove a category from this set only after it has gone
# through the same evidence process as the eight approved categories.
DISABLED_CATEGORIES = frozenset({
    "sets_won",
    "first_set_games",
    "any_tiebreak",
    "first_set_fav_wins",
})


# APEX-BREAKSOFF-2026-10-10 (owner, approved "go" 2026-10-10): Breaks
# is kept OFF every recommendation board. Master audit 2026-10-10: playable
# Breaks at 70%+ stated won 57.7% (30-22) against 73.6% claimed; all playable
# Breaks 52.1% (183-168). Still fetched, scored, logged, settled and tracked
# -- this only stops it reaching a board. Return rule: bring it back once its
# 70%+ props win >= 70% on 50+ settled (the daily board report tracks this).
BOARD_EXCLUDED_CATEGORIES = frozenset({"breaks"})

# APEX-FSRETURN-2026-10-11 (owner, 2026-10-11: "make the changes where
# apex improves"): Fantasy Score returns to the ranked boards under the same
# 70% / Apex 67 rule as everything else. Evidence: settled Oct 1-9 FS props
# re-scored with the current engine (correct surface + ATP opponent aces):
# 70%+ won 71.1% (69-28, n=97) vs 67.0% under the old engine. It was excluded
# 2026-09-30 "pending recalibration"; set False to exclude it again.
BOARDS_INCLUDE_FANTASY_SCORE = True


def _category_probability_gate(cat, prob):
    """APEX-CATGATE-2026-10-07: True if `prob` clears this category's
    approved floor. A category not listed has no gate (returns True) --
    matches the approved configuration, which only specifies these eight."""
    if cat in DISABLED_CATEGORIES:   # APEX-DORMANT-GATE-HARDEN-2026-10-07
        return False
    if cat in BOARD_EXCLUDED_CATEGORIES:   # APEX-BREAKSOFF-2026-10-10
        return False
    floor = CATEGORY_PROBABILITY_GATE.get(cat)
    if floor is None:
        return True
    try:
        return float(prob or 0.0) >= floor
    except (TypeError, ValueError):
        return False


def _demote_bo5_from_top(scored):
    """APEX-BB-BO5TOP-2026-09-26: BOARD-GOVERNANCE rule, not a scoring rule.
    APEX score, CONFIDENCE, and RANK are untouched by this function -- no
    cap, no calibration penalty, nothing added to the model's math.
    Rationale (user decision, 2026-09-26): while Bo5 is formally classified
    RESTRICT and excluded from Best 3 (BO5-FWD-001, Milestone Review #3),
    it would be self-contradictory for the board to simultaneously forbid
    a pick from Best 3 eligibility while handing it the single most
    prominent slot on the board. The #1 slot is reserved for categories
    eligible for Best 3; nothing more. Test the user applied: if a pick
    couldn't enter Best 3, it shouldn't hold the headline slot either.
    Revisit/remove this function entirely if Bo5 is ever promoted from
    RESTRICT to SUPPORTED -- at that point it should compete on APEX score
    like any other category, headline slot included.
    Mechanics: if position 0 of an already fully-sorted board is Bo5,
    swap it with the first non-Bo5 pick found; every other pick, including
    the demoted one, keeps its place. No-op if the board is empty, the
    top pick is already non-Bo5, or every pick on the board is Bo5.
    APEX-BB-BO5TOP-FIX-2026-09-26: best_bets_block stores bo5 at the top
    level of each scored dict; best_bets_detail stores it nested inside
    d["p"]["bo5"] instead. _is_bo5 checks both shapes so this one helper
    works correctly for both render paths without duplicating logic."""
    def _is_bo5(d):
        if "bo5" in d:
            return bool(d.get("bo5"))
        return bool((d.get("p") or {}).get("bo5"))
    if not scored or not _is_bo5(scored[0]):
        return scored
    for i, d in enumerate(scored):
        if not _is_bo5(d):
            scored[0], scored[i] = scored[i], scored[0]
            break
    return scored



def _category_gate_blocked(p):
    """APEX-CATGATE-2026-10-20: True while this category + direction is held off the Best
    boards by apex_category_gate.py (settled hit rate clearly under Honest). Never raises."""
    try:
        import apex_category_gate as _cg
        return _cg.is_blocked(p.get("category"), p.get("direction"))
    except Exception as _fe:
        _failnote("_category_gate_blocked", _fe)
        return False


def _best_eligible(p, prop_type):
    """APEX-BEST-ELIGIBILITY-FILTER-2026-09-28: True unless this prop's own
    type has an eligibility flag written for it and that flag reads False.
    A missing/blank flag (row logged before this existed, or a type with no
    floor at all, e.g. demon) is treated as eligible -- never excludes on
    absence. Reads a plain CSV-string ("True"/"False") or a real bool."""
    t = str(prop_type or "").strip().lower()
    if t in ("goblin", "standard", "demon") and _category_gate_blocked(p):
        return False
    if t not in ("goblin", "standard"):
        return True
    v = p.get("best_goblin_eligible" if t == "goblin" else "best_standard_eligible")
    if v is None or v == "":
        return True
    return str(v).strip().lower() == "true"


def best_bets_block(picks, rec=None, prop_type=None):
    """APEX-BB-001: rendered Best Bets section. picks: dicts with player,
    category, prob, line, direction, bo5. Never raises; '' on any problem."""
    try:
        rec = rec or records()
        # APEX-TYPECMD-TRK-2026-09-09: one prop type only (goblin / standard / demon).
        # Rows evaluated before 09-08 carry no prop_type and are simply not in these views.
        if prop_type:
            picks = [p for p in picks if str(p.get("prop_type") or "").lower() == str(prop_type).lower()]
            if not picks:
                return ("No %s props on the current board yet. (Prop type has been logged since "
                        "2026-09-08 -- boards evaluated before then carry no type.)" % prop_type)
            # APEX-BEST-ELIGIBILITY-FILTER-2026-09-28: Best Goblin / Best
            # Standard specifically -- a below-floor prop stays on the
            # regular board (Picks Channel) but cannot become THIS.
            _before_elig = len(picks)
            picks = [p for p in picks if _best_eligible(p, prop_type)]
            if not picks:
                return ("No %s props currently clear the Best %s threshold (%d prop%s below floor). "
                        "Still visible in the main Picks Channel." %
                        (prop_type, str(prop_type).capitalize(), _before_elig,
                         "" if _before_elig == 1 else "s"))
        scored = []
        for p in picks:
            cat = p.get("category") or ""
            if not cat:
                continue
            if not _category_has_evidence(cat, rec):   # APEX-CAT-GATE-2026-09-27
                continue
            if not _category_probability_gate(cat, p.get("prob_cal", p.get("prob"))):   # APEX-CATGATE-2026-10-07
                continue
            sc, ev, lvl, up, down = apex_score(p.get("prob_cal", p.get("prob")), cat, bool(p.get("bo5")), rec)
            if float(p.get("prob_cal", p.get("prob")) or 0) < float(p.get("prob") or 0) - 1e-9:
                down.append("aces LESS calibrated %d%% from %d%% (measured bias)" % (
                    round(float(p["prob_cal"]) * 100), round(float(p["prob"]) * 100)))
            cs = rec["cats"].get(cat) or _stat([])
            try:
                import apex_card_index as _CI
                # APEX-MATCHCARD-2026-08-31: match rows key on the pair
                if (p.get("player") or "") == "match":
                    _card = _CI.lookup_match(str(p.get("opponent") or ""), cat,
                                             str(p.get("match_date") or ""))[0]
                else:
                    _card = _CI.lookup(str(p.get("player") or ""), cat,
                                       str(p.get("match_date") or ""))[0]
            except Exception:
                _card = None
            cf, _ctier, cwhy = apex_confidence(cat, bool(p.get("bo5")), rec,
                                               score=sc, prob=p.get("prob"), evidence=ev, attention=lvl,
                                               warnings=down, card_present=bool(_card))
            scored.append({"p": p, "cat": cat, "score": sc, "ev": ev, "lvl": lvl,
                           "conf": cf, "cwhy": cwhy, "rank": rank_score(sc, cf),
                           "up": up, "down": down, "rs": _rs(cs), "bo5": bool(p.get("bo5"))})
        if not scored:
            return ""
        # APEX-BB3: legacy APEX-only order captured for the shadow footer + log
        _legacy_all = sorted(scored, key=lambda d: -d["score"])
        _legacy_pos, _legacy_prim, _lseen = {}, [], set()
        for _ld in _legacy_all:
            _lk = (_ld["p"].get("player") or "") + "|" + (_ld["p"].get("opponent") or "")
            if _lk in _lseen:
                continue
            _lseen.add(_lk)
            _legacy_prim.append(_ld)
            _legacy_pos[id(_ld)] = len(_legacy_prim)
        scored.sort(key=lambda d: _bb_sortkey(d))   # APEX-BB3: RANK, prob, edge
        # APEX-BB-002c: one slot per player -- you can only play one prop per
        # player per slip-side. Best prop leads; the rest ride an "also:" line.
        _seen, _prim = {}, []
        for d in scored:
            _pk = (d["p"].get("player") or "") + "|" + (d["p"].get("opponent") or "")
            if _pk in _seen:
                _seen[_pk].setdefault("more", []).append(d)
            else:
                _seen[_pk] = d; _prim.append(d)
        scored = _prim
        scored = _demote_bo5_from_top(scored)   # APEX-BB-BO5TOP-2026-09-26: board-governance, not scoring
        icon = {"HIGH": "\U0001F7E2", "MEDIUM": "\U0001F7E1", "LOW": "\u26aa"}
        def line1(i, d):
            p = d["p"]
            return ("**#%d %s** -- %s `%s %s` \u00b7 %d%% \u00b7 %s %s \u00b7 cat %s \u00b7 **APEX SCORE %d** \u00b7 **CONFIDENCE %d/100** \u00b7 **RANK SCORE %.1f** (Evidence: %s)%s"
                    % (i, p.get("player", ""), LABEL.get(d["cat"], d["cat"]),
                       p.get("line", ""), p.get("direction", ""), round(float(p.get("prob") or 0) * 100),
                       icon[d["lvl"]], d["lvl"], d["rs"], d["score"],
                       d.get("conf", 0), d.get("rank", 0.0), d["ev"],
                       " \u26a0\ufe0f BO5 SAMPLE" if d["bo5"] else ""))
        L = ["\U0001F3C6 **APEX BEST BETS** -- evidence-weighted (probability \u00b7 live calibration)"]
        for i, d in enumerate(scored[:8], 1):
            L.append(line1(i, d))
        best3 = [d for d in scored if not d["bo5"]][:3]
        L.append("")
        L.append("\U0001F947 **BEST 3 PICKS TODAY**")
        if best3:
            for i, d in enumerate(best3, 1):
                why = "; ".join(d["up"][:2]) or "highest evidence-weighted score on the board"
                if d["score"] < 50:
                    why += " -- \u26a0\ufe0f below the 50 bar: best available today, not a strong bet"
                L.append("%d. %s -- %s (%d): %s" % (i, d["p"].get("player", ""),
                                                    LABEL.get(d["cat"], d["cat"]), d["score"], why))
        else:
            L.append("-- no eligible picks (Bo5 picks cannot enter Best 3 under BO5-FWD-001)")
        low = [d for d in scored if d not in best3][-3:][::-1]
        L.append("")
        L.append("\u26a0\ufe0f **LOW PRIORITY PICKS**")
        if low:
            for d in low:
                why = "; ".join(d["down"][:2]) or "no negative flags -- simply outranked today"
                L.append("\u00b7 %s -- %s (%d): %s" % (d["p"].get("player", ""),
                                                       LABEL.get(d["cat"], d["cat"]), d["score"], why))
        else:
            L.append("-- board too small to split")
        L.append("")
        L.append("_RANK SCORE = APEX \u00d7 (CONFIDENCE / 100) -- an ordering metric, not a probability._")
        L.append("_SHADOW \u2014 legacy APEX-only order: %s_"
                 % " \u00b7 ".join("%d. %s" % (_i, _d["p"].get("player", ""))
                                    for _i, _d in enumerate(_legacy_prim[:8], 1)))
        L.append("_Scores re-derive from the settled ledger at every post -- they sharpen as evidence settles._")
        try:                                   # APEX-SLIP-V11: three slips, fail-open
            _sl, _maps = _slip_sections(scored, picks); L.extend(_sl)
        except Exception:
            _maps = {}
        _bb_rank_log(scored, _legacy_pos, "block", slip=_maps.get("mixed"), slip_std=_maps.get("standard"), slip_gob=_maps.get("goblin"))
        return "\n".join(L)
    except Exception:
        return ""


# ================================================================ APEX-PROPID-2026-09-30
def _apex_prop_identity(r):
    """A real-world prop's STABLE identity: player + opponent + market + line.
    Deliberately excludes match_id, prop_type (tier), and ts -- those are
    allowed to change scrape-to-scrape without creating a second active
    record. See APEX-PROPID-2026-09-30 (Prop Identity Repair) -- this
    replaced the old (match_id, player, category, line, prop_type) key,
    which let a tier change (Standard->Goblin, etc.) or a match_id drift
    (match_date shifting a day between scrapes of the same real match)
    each spawn a permanent duplicate "ghost" row that never retired."""
    return (str(r.get("player") or "").strip().lower(),
            str(r.get("opponent") or "").strip().lower(),
            r.get("category"), str(r.get("line") or ""))


# ================================================================ APEX-BOARDCAP-2026-10-01
BOARD_MEMBERSHIP_LOG = os.path.join(HERE, "apex_board_membership_log_v1.csv")
_BOARD_MEMBERSHIP_FIELDS = ["date", "player", "opponent", "category", "line",
                            "elite6", "top10", "top3", "bestgoblin", "bestdemon", "beststandard"]

# ================================================================ APEX-PUBLISHEDBOARD-2026-10-07
# owner, authorized: commands now read this frozen snapshot of the
# last /fetch publish rather than rebuilding live on every call. See
# published_board_detail() below for the read side.
PUBLISHED_BOARD_SNAPSHOT = os.path.join(HERE, "apex_published_board_snapshot.json")
_PUBLISHED_FLAG_LIMIT = {"elite6": 6, "top10": 10, "top3": 3,
                         "bestgoblin": 6, "bestdemon": 6, "beststandard": 6}
_PUBLISHED_FLAG_LABEL = {"elite6": "Elite6", "top10": "Top10", "top3": "Top 3",
                         "bestgoblin": "Best Goblin", "bestdemon": "Best Demon",
                         "beststandard": "Best Standard"}

def _scored_identity_keys(result):
    """Pull the stable prop identity out of an elite6_detail()/best_bets_detail()
    return_scored=True result. Several early-return guard clauses in both
    functions hand back a bare string even when return_scored=True was
    passed (no qualifying props, bad category, etc.) -- those correctly
    produce zero keys here rather than raising."""
    if not isinstance(result, dict):
        return set()
    scored = result.get("scored") or []
    keys = set()
    for d in scored:
        p = d.get("p") or {}
        keys.add(_apex_prop_identity(p))
    return keys

def capture_board_membership(picks, dates, source="unknown"):
    """APEX-BOARDCAP-2026-10-01: records, once per day at publish time,
    which specific props appeared on Elite6 / Top10 / Best Goblin / Best
    Demon / Best Standard. This did not exist before this marker -- the
    five board commands are on-demand, in-memory re-rankings of
    load_current_board() and were never persisted, so there was no way to
    later compute a per-board win/loss record. No retroactive backfill is
    possible; this log only has data from the day it starts running
    forward. Runs on the SAME `picks` list the day's Picks Channel post
    was built from (not a later load_current_board() call, which could
    legitimately differ) so the capture matches what was actually shown
    that day. Fail-open: any error here must never block publishing."""
    date_str = str(dates.get("match_date") or dates.get("pipeline_run_date") or "").strip()
    if not date_str or not picks:
        return

    # APEX-LEGACYFIX-2026-10-05 (owner, Legacy Picks System Fix):
    # the live /fetch pipeline hands this function the day's `selected`
    # list, which is shaped with `stat_type` / `confidence` /
    # `prop_type_classified` (the pipeline's own field names) -- NOT the
    # `category` / `prob` / `prop_type` field names that elite6_detail()/
    # best_bets_detail() require. Those two functions silently skip any
    # prop with no `category` (`if not p.get("category"): continue`), so
    # every `selected`-shaped prop was being dropped before it could ever
    # be captured -- zero exceptions, zero log lines, just nothing
    # recorded. This translates ONLY the handful of field names those
    # two read, exactly the same mapping the one-time Oct 1 backfill
    # script (_phase1_reconstruct2.py) and the existing Best Bets block
    # in openclaw_core.py (APEX-BB-001) already use elsewhere. A prop
    # that already has `category` (e.g. a direct load_current_board()
    # call) is passed through untouched. This is the ONLY change in this
    # fix -- it does not touch board logic, Apex Score, ranking, quality
    # floors, Held Back logic, or publishing; it only repairs what this
    # recorder function sees.
    try:
        import openclaw_core as _core_legacyfix
        _STAT_MAP = _core_legacyfix._APEX_STAT_MAP
    except Exception:
        _STAT_MAP = {}

    def _legacy_translate(p):
        if p.get("category"):
            return p
        slug = str(p.get("stat_type") or "").strip().lower().replace(" ", "_")
        cat = _STAT_MAP.get(slug, "")
        if not cat:
            return p
        q = dict(p)
        q["category"] = cat
        q.setdefault("prob", float(p.get("confidence") or 0))
        q.setdefault("prop_type", p.get("prop_type_classified", "standard"))
        q.setdefault("bo5", str(p.get("apex_best_of") or "") == "5")
        return q

    picks = [_legacy_translate(p) for p in picks]
    _all_picks = picks
    try:   # APEX-SURFACEHOLD-2026-10-22: boards never see a prop held for its surface
        _held = [p for p in picks if _surface_hold(p)]
        if _held:
            print("[SurfaceHold] holding %d prop(s) off the boards until re-scored" % len(_held))
            picks = [p for p in picks if not _surface_hold(p)]
    except Exception:
        picks = _all_picks

    try:
        e6_res  = elite6_detail(picks, exclude_fantasy_score=True, return_scored=True)
        t10_res = best_bets_detail(picks, limit=10, exclude_fantasy_score=True, return_scored=True)
        # APEX-FETCHRELIABILITY-2026-10-01: "Top 3 Record" is a new field
        # in the User Performance Report -- the top 3 of the same overall
        # ranking Top10 uses, not a separate board of its own.
        t3_res  = best_bets_detail(picks, limit=3, exclude_fantasy_score=True, return_scored=True)
        bg_res  = best_bets_detail(picks, prop_type="goblin", limit=6, slip_rules=True,
                                    exclude_fantasy_score=True, return_scored=True)
        bd_res  = best_demon_detail(picks, return_scored=True)   # APEX-DEMONBOARD-2026-10-22
        bs_res  = best_standard_detail(picks, return_scored=True)   # APEX-STANDARD6X-2026-10-11
    except Exception as e:
        print("[BoardCapture] board generation failed (%s): %s" % (type(e).__name__, e))
        return
    # APEX-RERANKLIVE-2026-10-22 (owner, "Go"): Top 3 of each board stay in Win
    # Probability order; spots #4 and below are re-ordered by Win Probability plus the
    # card-signal nudges (apex_rerank_papertest.signal_adjusted), drawn from a wider
    # list of the same board's own qualifiers. Board size is unchanged. Set
    # RERANK_AFTER_TOP3 = False to turn it off.
    # APEX-TOP3NET-2026-10-22 (owner, "Go"): with TOP3_NET_SIGNAL the Top 3
    # are re-ordered by the same rule too (the /top3 board included), and this runs
    # BEFORE the membership log so every log records the published order.
    _t10_pre = list((t10_res or {}).get("scored") or []) if isinstance(t10_res, dict) else []   # APEX-175RANK-2026-10-22
    if RERANK_AFTER_TOP3:
        try:
            _wide = {
                "elite6": lambda: elite6_detail(picks, limit=15, exclude_fantasy_score=True, return_scored=True),
                "top10": lambda: best_bets_detail(picks, limit=25, exclude_fantasy_score=True, return_scored=True),
                "bestgoblin": lambda: best_bets_detail(picks, prop_type="goblin", limit=15, slip_rules=True,
                                                       exclude_fantasy_score=True, return_scored=True),
                "top3": lambda: best_bets_detail(picks, limit=25, exclude_fantasy_score=True, return_scored=True),
                "bestdemon": lambda: best_demon_detail(picks, limit=15, return_scored=True),
                "beststandard": lambda: best_standard_detail(picks, limit=15, return_scored=True),
            }
            for _flag, _res in (("elite6", e6_res), ("top10", t10_res), ("top3", t3_res), ("bestgoblin", bg_res),
                                ("bestdemon", bd_res), ("beststandard", bs_res)):
                _rerank_after_top3(_res, _wide[_flag],   # APEX-STANDARDRANK-2026-10-22
                                   _standard_rank_prob if _flag == "beststandard" else None,
                                   flag=_flag)   # APEX-DEMONSTDBARS-2026-10-25
        except Exception as e:
            print("[RerankLive] skipped (%s): %s" % (type(e).__name__, e))
        # APEX-175RANK-2026-10-22 ("still run the test"): log the Top 3 as it would be with no
        # 17.5 push-down, for the "no penalty" arm of apex_top3_no175_papertest. Background only.
        # APEX-175BACKUPS-2026-10-22: the arm ignores the push-down AND the skip-to-BACKUPS rule
        global _LINE175_RULES_OFF
        try:
            _LINE175_RULES_OFF = True
            _np0 = best_bets_detail(picks, limit=10, exclude_fantasy_score=True, return_scored=True) \
                if LINE175_SKIP_TO_BACKUPS else {"scored": _t10_pre}
            _np = {"scored": list((_np0 or {}).get("scored") or []) if isinstance(_np0, dict) else list(_t10_pre)}
            _rerank_after_top3(_np, _wide["top10"], None, penalize=False)
            _write_nopenalty_top3(date_str, source, (_np.get("scored") or [])[:3])
        except Exception as e:
            print("[175Rank] no-penalty log skipped (%s): %s" % (type(e).__name__, e))
        finally:
            _LINE175_RULES_OFF = False
        # APEX-DFFANTASY-2026-10-22: log the Top 3 and Elite 6 as they would be without the
        # Fantasy LESS / DF-after-a-match holds (17.5 rules still on), for the "without" arm
        # of apex_dffs_holds_papertest. Background only; nothing posted.
        global _DF_FS_HOLDS_OFF
        try:
            _DF_FS_HOLDS_OFF = True
            _off = {"top3": best_bets_detail(picks, limit=3, exclude_fantasy_score=True, return_scored=True),
                    "elite6": elite6_detail(picks, exclude_fantasy_score=True, return_scored=True)}
            for _flag, _res in _off.items():
                _rerank_after_top3(_res, _wide[_flag], None)
            _write_holds_off(date_str, source, {f: ((r or {}).get("scored") or []) if isinstance(r, dict) else []
                                                for f, r in _off.items()})
        except Exception as e:
            print("[DFFSHolds] without-holds log skipped (%s): %s" % (type(e).__name__, e))
        finally:
            _DF_FS_HOLDS_OFF = False
    # APEX-PUBLISHEDBOARD-2026-10-07: keys derived from the SAME result
    # dicts the snapshot below persists -- one computation, two uses.
    e6_keys, t10_keys, t3_keys = (_scored_identity_keys(e6_res), _scored_identity_keys(t10_res),
                                   _scored_identity_keys(t3_res))
    bg_keys, bd_keys, bs_keys = (_scored_identity_keys(bg_res), _scored_identity_keys(bd_res),
                                  _scored_identity_keys(bs_res))

    all_keys = e6_keys | t10_keys | t3_keys | bg_keys | bd_keys | bs_keys
    if not all_keys:
        # APEX-EMPTYBOARD-2026-11-11 (owner, "Go fix empty boards"): this returned without a word, so on a day when
        # no pick passed any board the old published board stayed up for hours (11/11: frozen at 10:17 AM). Now the empty
        # board is written like any other: snapshot, board status and board history below all run.
        if not EMPTY_BOARD_FIX:
            return
        print("[BoardCapture] no pick passed on any board -- writing the empty board (%s)" % source)

    file_exists = os.path.exists(BOARD_MEMBERSHIP_LOG)
    try:
        _seen = set()   # APEX-LEARNBOARDS-2026-11-09: one row per prop per day (was one per capture)
        if file_exists:
            with open(BOARD_MEMBERSHIP_LOG, newline="") as _rf:
                for _r in csv.DictReader(_rf):
                    if _r.get("date") == date_str:
                        _seen.add(tuple(str(_r.get(x, "")) for x in _BOARD_MEMBERSHIP_FIELDS))
        with open(BOARD_MEMBERSHIP_LOG, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_BOARD_MEMBERSHIP_FIELDS)
            if not file_exists:
                w.writeheader()
            for k in all_keys:
                player, opponent, category, line = k
                row = {
                    "date": date_str, "player": player, "opponent": opponent,
                    "category": category, "line": line,
                    "elite6": int(k in e6_keys), "top10": int(k in t10_keys),
                    "top3": int(k in t3_keys),
                    "bestgoblin": int(k in bg_keys), "bestdemon": int(k in bd_keys),
                    "beststandard": int(k in bs_keys),
                }
                sig = tuple(str(row[x]) for x in _BOARD_MEMBERSHIP_FIELDS)
                if sig in _seen:
                    continue
                _seen.add(sig)
                w.writerow(row)
    except Exception as e:
        print("[BoardCapture] write failed (%s): %s" % (type(e).__name__, e))

    # APEX-PUBLISHEDBOARD-2026-10-07 (owner, authorized): persist
    # the full rendered board data (not just identity keys) so on-demand
    # commands can read exactly what was published instead of rebuilding
    # live. Fail-open -- a snapshot-write failure must never block
    # publishing, same discipline as the CSV write above.
    try:
        _write_published_snapshot(date_str, {
            "elite6": e6_res, "top10": t10_res, "top3": t3_res,
            "bestgoblin": bg_res, "bestdemon": bd_res, "beststandard": bs_res,
        })
    except Exception as e:
        print("[BoardSnapshot] write failed (%s): %s" % (type(e).__name__, e))
    # APEX-BOARDSTATUS-2026-10-10: where every prop ended up and why, for
    # this publish (fail-open, same discipline as the two writes above).
    write_board_status(_all_picks, {
        "elite6": e6_res, "top10": t10_res, "top3": t3_res,
        "bestgoblin": bg_res, "bestdemon": bd_res, "beststandard": bs_res,
    }, date_str, source=source)


# APEX-BOOKSSAY-2026-10-22 (owner, "Go" on 10/22): every Goblin card shows what the
# books (bet365 and/or Pinnacle, no-vig, for this exact line) give our side next to Apex's
# number, e.g. "📗 Books say ~71% (Apex 75%)"; "no book line" when no book prices it.
# Display only. Undo: False.
REAL_RECORD_LINE = True   # APEX-REALRECORD-2026-10-31: '🧾 Real record' card line, display only
GOBLIN_BOOKS_LINE = True
RERANK_AFTER_TOP3 = True   # APEX-RERANKLIVE-2026-10-22; False = plain Win Probability order
# APEX-TOP3NET-2026-10-22 (owner, "Go"): True = the Top 3 of every board also
# rank by Win Prob + NET_SIGNAL_POINTS per net signal point. Undo: False (Top 3 back
# to Win Prob only; #4 and below keep the net score).
TOP3_NET_SIGNAL = True
# APEX-NETSIGNAL-2026-10-22: #4 and below rank by Win Prob + NET_SIGNAL_POINTS per net
# signal point (apex_card_signals.net_score; 0.05 = 5 Win Prob points, the value the
# 10/22 replay used). Undo: set 0 to rank #4+ by Win Prob alone (Top 3 never use it).
NET_SIGNAL_POINTS = 0.05
# APEX-DELETE75-2026-10-31 (owner, "Be sure that the 75 rule ... is deleted and removed"): the rule tested 10/22
# is the rule. Pass 70 and Apex 67, then Win Prob + signals sets the order. The 75 Win Prob bars on spots #1-3 and on
# fill-ins are gone from the code (old code: git tag before-delete-75). Only Best Demon and Best Standard keep a top bar.
TOP_SLOTS_COUNT = 3
# APEX-DEMONSTDBARS-2026-10-25 (owner: "For demon and standards that can be different set
# them at what they suppose to be"): Best Demon and Best Standard top spots use their own bar
# instead of 75 (they almost never reach 75). From graded results: Demon MORE at 62%+ Win Prob hit
# 47% (9-10) vs 33% (5-10) under it, Sep 29-Oct 24 (a 3-Demon slip needs ~41% a leg); Best Standard
# bars the score it ranks on (Win Prob x the market's own Standard record, _standard_rank_prob):
# 68%+ hit 69% (11-5) vs 52% (11-11) under it, Oct 9-24 (a 6x 3-pick needs 55% a leg). Both sit above
# the board floors (Demon 60%, Standard market rules), which are unchanged. Other boards keep 75.
# Undo: TOP_SLOTS_MIN_BY_BOARD = {} (back to 75 everywhere).
TOP_SLOTS_MIN_BY_BOARD = {"bestdemon": 62, "beststandard": 68}


def top_slots_bar(flag=None):
    """APEX-DEMONSTDBARS-2026-10-25: the top-3 bar (percent) for a board, None when off."""
    return TOP_SLOTS_MIN_BY_BOARD.get(flag)


def top_slots_words(flag=None):
    """APEX-DEMONSTDBARS-2026-10-25: what the bar measures, in plain words."""
    return "adjusted win chance" if flag == "beststandard" else "win chance"


# APEX-FANTASYPOINTS-2026-11-01 (owner, "Go ahead with the change"): Fantasy MORE signals stay on the
# card but add no rank points; those picks rank on Win Prob. 19-day replay with today's rules: Top 3 16 days
# vs 14, Elite 5 12 vs 11, Elite 6 11 vs 9, better or even in both halves. Undo: FANTASY_RANK_POINTS = True.
FANTASY_RANK_POINTS = False


def _fantasy_no_points(p):
    return (not FANTASY_RANK_POINTS and p.get("category") == "fantasy_score" and p.get("player") != "match"
            and str(p.get("direction") or "").upper() == "MORE")


def net_adjusted(p, prob):
    """APEX-NETSIGNAL-2026-10-22: Win Prob + NET_SIGNAL_POINTS * net signal score. Never raises."""
    try:
        import apex_card_signals as _csig
        if _fantasy_no_points(p):   # APEX-FANTASYPOINTS-2026-11-01
            return prob
        return prob + NET_SIGNAL_POINTS * _csig.net_score(p)[2]
    except Exception as _fe:
        _failnote("net_adjusted", _fe)
        return prob


# APEX-175RANK-2026-10-22 (owner, "Go and still run the test"): Total Games MORE
# props at line 17.5 lose rank points when their 🎯 17.5 check says skip (10 points of
# Win Prob) or "not enough data yet" (5 points). ✅ cases and every other line are
# untouched. Option C of _backups/line175_rank_test_2026-10-22/report.md.
# Undo: set both to 0.
LINE175_SKIP_PENALTY = 0.10
LINE175_NODATA_PENALTY = 0.05
# APEX-175BACKUPS-2026-10-22 (owner, "Go back up"): an over-17.5 pick whose 🎯 17.5 check
# says ❌ skip takes no numbered spot on any board; it goes to that board's BACKUPS with the
# reason, the next best pick moves up, and /allprops still lists it. "Not enough data yet"
# keeps only the push-down above. LINE175_SKIP_PENALTY stays for anything still ranking it.
# Undo: LINE175_SKIP_TO_BACKUPS = False.
LINE175_SKIP_TO_BACKUPS = True
# APEX-175CLOSEROOM-2026-10-30 (owner, "Go 17.5 close or room"): "not enough data yet" goes to
# BACKUPS too, so an over 17.5 keeps a numbered spot only when players are close or bet365 has 4+ games
# of room. Top 3 17.5 test (TEST FINISHED, Oct 23-30): this rule won 4 of 5 days ($[amount]) vs real 3 of 5
# ($[amount]). Undo: LINE175_NODATA_TO_BACKUPS = False (back to the 5-point push-down only).
LINE175_NODATA_TO_BACKUPS = True
_LINE175_RULES_OFF = False   # True only while the Top 3 tracker's no-penalty arm is built


def line175_skip_reason(p):
    """APEX-175BACKUPS-2026-10-22: the BACKUPS reason when the 17.5 check says skip, else None. Never raises."""
    try:
        if not LINE175_SKIP_TO_BACKUPS or _LINE175_RULES_OFF:
            return None
        _v = line175_penalty(p)[1]
        if _v == "nodata" and LINE175_NODATA_TO_BACKUPS:   # APEX-175CLOSEROOM-2026-10-30
            return "17.5 check can't confirm a close match or 4+ games of room (not enough data yet)"
        if _v != "skip":
            return None
        import apex_card_signals as _csig
        import apex_top3_no175_papertest as _t
        return "17.5 check says skip (mismatch and under %d games of room; these hit %d%%)" % (
            _t.ROOM_MIN, _csig.LINE175_HIT_SKIP)
    except Exception as _fe:
        _failnote("line175_skip_reason", _fe)
        return None


# APEX-DFFANTASY-2026-10-22 (owner, "1,2.3 now" on the 10/22 DF/Fantasy study, A/D1/D2):
# 1. Fantasy Score LESS never takes a numbered spot on any board or #slips card; it goes to
#    BACKUPS (FS LESS at 70%+ / Apex 67 hit 44%, 15 of 34, Sep 30 - Oct 22). /allprops still lists it.
# 3. Double Faults LESS when the player played the day before (the 📅 card data) goes to BACKUPS
#    on every board (4 of 11).
# Both use the same hold-back path as LINE175_SKIP_TO_BACKUPS. The tracker's no-penalty arm and
# the holds test's "without" arm are built with these off (_LINE175_RULES_OFF / _DF_FS_HOLDS_OFF).
# Undo: set either switch to False.
FANTASY_LESS_TO_BACKUPS = True
DF_LESS_PLAYED_YESTERDAY_HOLD = True
_DF_FS_HOLDS_OFF = False   # True only while the holds test's "without" arm is built
FANTASY_LESS_HOLD_WHY = "Fantasy LESS has hit 44% at this level (15 of 34); shown so you can see it"
DF_YESTERDAY_HOLD_WHY = "played yesterday (DF LESS after a match the day before has hit 4 of 11)"


def df_fs_hold_reason(p):
    """APEX-DFFANTASY-2026-10-22: the BACKUPS reason for the Fantasy LESS / DF-after-a-match
    holds, else None. Never raises."""
    try:
        if _DF_FS_HOLDS_OFF or _LINE175_RULES_OFF:
            return None
        d = str(p.get("direction") or "").upper()
        if d != "LESS":
            return None
        if FANTASY_LESS_TO_BACKUPS and p.get("category") == "fantasy_score":
            return FANTASY_LESS_HOLD_WHY
        if DF_LESS_PLAYED_YESTERDAY_HOLD and p.get("category") == "dfs":
            import apex_card_signals as _csig
            if _csig.played_yesterday(p):
                return DF_YESTERDAY_HOLD_WHY
    except Exception as _fe:
        _failnote("df_fs_hold_reason", _fe)
        pass
    return None


# APEX-NOBOOK-2026-10-24 (owner, "Yes let's add a flag before the name and take them off the
# boards only as a backup with a clear warning by the name ⚠️⚠️"): a Total Games pick (MORE or LESS,
# any line) with no bet365 total from the shared reader (apex_card_signals.book_total, which also
# takes ParlayPlay's own stored total) takes no numbered spot on any board. It goes to BACKUPS with
# ⚠️⚠️ before the name and the reason, the next best prop takes the slot (HOLD_SLOT_MOVES_UP), and
# /allprops shows the same flag. When the total arrives, the next hourly republish brings it back.
# Fantasy / Aces / DF / Breaks / Games Won are not touched. Tracked by apex_nobook_tracker.
# Undo: NO_BOOK_TOTAL_TO_BACKUPS = False.
NO_BOOK_TOTAL_TO_BACKUPS = True
NO_BOOK_FLAG = "\u26A0\uFE0F\u26A0\uFE0F"
NO_BOOK_WHY = "no book total (no sportsbook has priced this match), can't run the 17.5 or room check"


def no_book_total_reason(p):
    """APEX-NOBOOK-2026-10-24: the BACKUPS reason for a Total Games pick with no bet365 total, else None. Never raises."""
    try:
        if not NO_BOOK_TOTAL_TO_BACKUPS or p.get("category") != "total_games":
            return None
        import apex_card_signals as _csig
        return None if _csig.book_total(p) else NO_BOOK_WHY
    except Exception as _fe:
        _failnote("no_book_total_reason", _fe)
        return None


def no_book_mark(p):
    """APEX-NOBOOK-2026-10-24: "⚠️⚠️ " to put before the name when the no-book hold applies, else ""."""
    if p.get("category") == "1st_set_total_games":   # APEX-RANKFIRSTSET-2026-11-01
        return "\U0001F3BE "
    return (NO_BOOK_FLAG + " ") if no_book_total_reason(p) else ""


# APEX-LINE70-2026-10-31 (owner, "Go line 70 rule"): a Goblin Fantasy MORE at a line of 4+ goes to
# BACKUPS unless the player is 70%+ to win the match. A straight-set loss all but ends it (over 4.5 with no set
# won: 59 of 2,246). Replay of official slips 10/22-10/30: Top 3 stays 4 days, Elite 6 0 to 1, one short slip
# on 10/25 (_backups/line4_test_2026-10-31). No known win chance = no hold. Undo: LINE70_RULE = False.
LINE70_RULE = True
LINE70_MIN_LINE, LINE70_MIN_WIN = 4.0, 0.70


def line70_hold_reason(p):
    """The BACKUPS reason under LINE70_RULE, else None. Never raises."""
    try:
        if not LINE70_RULE or p.get("category") != "fantasy_score" or str(p.get("direction") or "").upper() != "MORE" \
                or str(p.get("prop_type") or "").lower() != "goblin" or float(p.get("line") or 0) < LINE70_MIN_LINE:
            return None
        import apex_card_signals as _csig
        f = _csig.own_win_chance(p)
        if f is None or f >= LINE70_MIN_WIN:
            return None
        return "Fantasy line %g needs a set win: player only %d%% to win the match (line 4+ needs 70%%)" % (float(p.get("line")), round(f * 100))
    except Exception as _fe:
        _failnote("line70_hold_reason", _fe)
        return None


# APEX-CUSHIONFILL-2026-11-01 (owner, "Go cushion fill"): no Total Games pick on any board when
# the favorite is 75%+ to win; a Total Games pick that moves into the spot this frees needs 4+ games of
# room under the book total, else the next one down. Other props move up as normal. 19-day replay:
# Top 3 15 days vs 14, Elite 5 12 vs 10, Elite 6 11 vs 9. Undo: CUSHION_FILL_ON = False.
CUSHION_FILL_ON = True
BIG_FAV_PCT = 0.75
CUSHION_FILL_ROOM = 4.0
BIG_FAV_WHY = "favorite 75%+ to win, can win fast"
CUSHION_ROOM_WHY = "moved into a freed spot with under 4 games of room under the book total"
ONE_ROOM_RULE = True
ONE_ROOM_WHY = "under 4 games of room under the book total"
DF_MORE_HOLD = True
DF_MORE_WHY = "Double Faults overs lost more than they won on real props (43 won, 51 lost)"
_BIG_FAV_OFF = False   # True only while working out the board as it would be with the favorites on
CUSHION_BLOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs", "cushion_block.json")


def big_fav_tg(p):
    """True for a Total Games pick whose favorite is 75%+ to win. Never raises."""
    try:
        if p.get("category") != "total_games":
            return False
        import apex_card_signals as _csig
        fav = _csig.match_favorite(p)   # APEX-FAVLOOKUP-2026-11-01: any line of the match, not this exact line only
        return fav is not None and fav >= BIG_FAV_PCT - 1e-9
    except Exception as _fe:
        _failnote("big_fav_tg", _fe)
        return False


def big_fav_reason(p):
    if not CUSHION_FILL_ON or _BIG_FAV_OFF:
        return None
    if big_fav_tg(p):
        return BIG_FAV_WHY
    # APEX-ONERULE-2026-11-01 (owner, "Go one rule and fix df"): the same test for every pick in
    # every spot, no "moved up" check. Undo: ONE_ROOM_RULE = False / DF_MORE_HOLD = False.
    if ONE_ROOM_RULE and str(p.get("direction") or "").upper() == "MORE" and _low_room_tg(p):
        try:
            import apex_card_signals as _csig
            return "only %g games of room under the book total, needs 4" % (float(_csig.book_total(p)) - float(p.get("line")))
        except Exception:
            return ONE_ROOM_WHY
    if DF_MORE_HOLD and p.get("category") == "dfs" and str(p.get("direction") or "").upper() == "MORE":
        return DF_MORE_WHY
    return None


def _low_room_tg(p):
    """True for a Total Games pick under 4 games of room under the book total. Never raises."""
    try:
        if p.get("category") != "total_games":
            return False
        import apex_card_signals as _csig
        bk = _csig.book_total(p)
        return bk is None or float(bk) - float(p.get("line")) < CUSHION_FILL_ROOM - 1e-9
    except Exception as _fe:
        _failnote("_low_room_tg", _fe)
        return False


def _cushion_blocked_load():
    try:
        with open(CUSHION_BLOCK_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def cushion_blocked_idents(flag):
    """Prop identities this board's last build kept out of a freed spot. Never raises."""
    try:
        return {tuple(x) for x in _cushion_blocked_load().get(flag or "pp", [])}
    except Exception:
        return set()


def cushion_block_reason(flag, p):
    try:
        if CUSHION_FILL_ON and tuple(_apex_prop_identity(p)) in cushion_blocked_idents(flag):
            return CUSHION_ROOM_WHY
    except Exception:
        pass
    return None


def _cushion_block_save(flag, ids):
    try:
        d = _cushion_blocked_load()
        d[flag or "pp"] = [list(x) for x in ids]
        tmp = CUSHION_BLOCK_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh, default=str)
        os.replace(tmp, CUSHION_BLOCK_PATH)
    except Exception as e:
        print("[CushionFill] block list not saved (%s)" % type(e).__name__)


def _cushion_block(pool, wide_fn, flag, n, prob_fn):
    """Identities of Total Games picks in pool that may not move into a spot freed by a big favorite.
    Works out the top n spots as they would be with the favorites on; only runs when one of them
    is a big favorite. Never raises."""
    global _BIG_FAV_OFF
    try:
        if not CUSHION_FILL_ON or _BIG_FAV_OFF or _LINE175_RULES_OFF or _DF_FS_HOLDS_OFF:
            return set()
        _BIG_FAV_OFF = True
        try:
            w0 = wide_fn()
            p0 = [d for d in ((w0.get("scored") if isinstance(w0, dict) else None) or [])
                  if not board_hold_reason(d.get("p") or {})]
        finally:
            _BIG_FAV_OFF = False
        p0 = [d for d in p0 if _board_qualifies(d)]
        rows = sorted(((board_rank_score(d.get("p") or {}, (prob_fn or _board_prob)(d), True), d) for d in p0),
                      key=lambda c: (-c[0], -float(c[1].get("score") or 0)))
        top, used = [], set()

        def _fill(seq, upto):
            for _adj, d in seq:
                if len(top) >= upto:
                    break
                nm = _prop_names(d.get("p") or {})
                if not (nm & used):
                    used.update(nm)
                    top.append(d)
        _bar = top_slots_bar(flag)
        _bp = (prob_fn or _board_prob) if flag == "beststandard" else _board_prob
        if _bar:
            _fill([c for c in rows if _bp(c[1]) >= _bar / 100.0 - 1e-9], min(TOP_SLOTS_COUNT, n))
            _fill(sorted(rows, key=lambda c: (-_bp(c[1]), -c[0])), min(TOP_SLOTS_COUNT, n))
        _fill(rows, n)
        removed = [d.get("p") or {} for d in top if big_fav_tg(d.get("p") or {})]
        if ONE_ROOM_RULE:   # APEX-ONERULE-2026-11-01: nothing is blocked by spot any more; the tracker still logs what came off
            try:
                import apex_cushion_tracker as _ct
                _ct.log_build(flag or "pp", removed, [d.get("p") or {} for d in top if big_fav_reason(d.get("p") or {})
                                                      and not big_fav_tg(d.get("p") or {})])
            except Exception as e:
                print("[CushionFill] tracker skipped (%s)" % type(e).__name__)
            return set()
        if not removed:
            return set()
        had = {tuple(_apex_prop_identity(d.get("p") or {})) for d in top}
        out = set()
        for d in pool:
            q = d.get("p") or {}
            k = tuple(_apex_prop_identity(q))
            if k not in had and _low_room_tg(q):
                out.add(k)
        try:
            import apex_cushion_tracker as _ct
            _ct.log_build(flag or "pp", removed, [d.get("p") or {} for d in pool
                                                 if tuple(_apex_prop_identity(d.get("p") or {})) in out])
        except Exception as e:
            print("[CushionFill] tracker skipped (%s)" % type(e).__name__)
        return out
    except Exception as e:
        print("[CushionFill] skipped (%s: %s)" % (type(e).__name__, e))
        return set()


def board_hold_reason(p):
    """Every rule that sends a qualified prop to BACKUPS instead of a numbered spot:
    the no-book-total hold (APEX-NOBOOK), the 17.5 skip (APEX-175BACKUPS) and the
    Fantasy LESS / DF holds (APEX-DFFANTASY). Never raises."""
    return no_book_total_reason(p) or line175_skip_reason(p) or df_fs_hold_reason(p) or line70_hold_reason(p) \
        or big_fav_reason(p) or _player_checks_reason(p)   # APEX-CUSHIONFILL-2026-11-01 / APEX-PLAYERCHECKS-2026-11-02


def _player_checks_reason(p):
    try:
        import apex_player_checks as _pc
        return _pc.hold_reason(p)
    except Exception:
        return None


def line175_penalty(p):
    """APEX-175RANK-2026-10-22: (points taken off, 'skip'/'nodata'/None). Reads the same
    🎯 card line (apex_card_signals.line175_check). Never raises."""
    try:
        if _LINE175_RULES_OFF:   # APEX-175BACKUPS-2026-10-22: no-penalty test arm
            return 0.0, None
        import apex_card_signals as _csig
        txt = _csig.line175_check(p)
        if not txt:
            return 0.0, None
        if "\u274c skip" in txt:
            return LINE175_SKIP_PENALTY, "skip"
        if "not enough data yet" in txt:
            return LINE175_NODATA_PENALTY, "nodata"
    except Exception as _fe:
        _failnote("line175_penalty", _fe)
        pass
    return 0.0, None


def board_rank_score(p, prob, penalize=True):
    """APEX-175RANK-2026-10-22: the one rank score every board orders by -- Win Prob +
    NET_SIGNAL_POINTS per net signal point, minus the 17.5 check push-down
    (penalize=False leaves the push-down out; the background test arm uses it). Never raises."""
    return net_adjusted(p, prob) - (line175_penalty(p)[0] if penalize else 0.0)


def order_header(flag=None):
    """The "📋 Order:" line on board headers (APEX-TOP3NET / APEX-175RANK-2026-10-22).
    flag names the board, for its own top-3 bar (APEX-DEMONSTDBARS-2026-10-25)."""
    h = "\U0001F4CB Order: Win Probability + %d points for each signal point on the \U0001F9EE line%s" % (
        round(NET_SIGNAL_POINTS * 100), "" if TOP3_NET_SIGNAL else " (#4 and below; Top 3 by Win Probability)")
    if LINE175_SKIP_TO_BACKUPS:   # APEX-175BACKUPS-2026-10-22
        h += "; over 17.5 picks whose 17.5 check says skip go to BACKUPS"
        if LINE175_NODATA_TO_BACKUPS:   # APEX-175CLOSEROOM-2026-10-30
            h = h[:-len(" whose 17.5 check says skip go to BACKUPS")] + " go to BACKUPS unless the players are close or bet365 has 4+ games of room"
        elif LINE175_NODATA_PENALTY:
            h += "; %d points off if not enough data yet" % round(LINE175_NODATA_PENALTY * 100)
    elif LINE175_SKIP_PENALTY or LINE175_NODATA_PENALTY:
        h += "; over 17.5 picks lose %d points if the 17.5 check says skip, %d if not enough data yet" % (
            round(LINE175_SKIP_PENALTY * 100), round(LINE175_NODATA_PENALTY * 100))
    # APEX-DFFANTASY-2026-10-22
    _held = [x for x, on in (("Fantasy LESS", FANTASY_LESS_TO_BACKUPS),
                             ("double-faults-after-a-match", DF_LESS_PLAYED_YESTERDAY_HOLD)) if on]
    if _held:
        h += "; %s picks go to BACKUPS" % " and ".join(_held)
    if NO_BOOK_TOTAL_TO_BACKUPS:   # APEX-NOBOOK-2026-10-24
        h += "; Total Games picks with no book total go to BACKUPS marked %s" % NO_BOOK_FLAG
    try:   # APEX-BLOWOUTDOUBLE-2026-10-25
        import apex_card_signals as _csig
        if _csig.BLOWOUT_DOUBLE:
            h += "; \U0001F3DF\ufe0f blowout risk counts double (%d points off)" % round(2 * NET_SIGNAL_POINTS * 100)
    except Exception:
        pass
    try:   # APEX-FANTASYSIGNALS-2026-10-30
        if _csig.FANTASY_SIGNALS:
            h += "; Fantasy Score MORE picks have their own signals (form, own win chance, blowout losses, played yesterday)"
        if getattr(_csig, "ACES_SIGNALS", False):   # APEX-ACESSIGNALS-2026-10-30
            h += "; Aces MORE picks have their own signals (form, underdog, played yesterday counts against)"
    except Exception:
        pass
    if CUSHION_FILL_ON:   # APEX-CUSHIONFILL-2026-11-01
        h += ("; no Total Games when the favorite is 75%+ to win, every Total Games over needs 4+ games of room, "
              "no Double Faults MORE" if ONE_ROOM_RULE else
              "; no Total Games when the favorite is 75%+ to win, and a Total Games pick filling that spot needs 4+ games of room")
    if top_slots_bar(flag) and TOP3_NET_SIGNAL:   # APEX-TOPSLOTS75 / APEX-DEMONSTDBARS-2026-10-25
        h += "; Top 3 spots need %d%%+ %s" % (top_slots_bar(flag), top_slots_words(flag))
        if flag == "beststandard":
            h += " (Win Probability adjusted for the market's own Standard record)"
    return h + ". A card that moved says so."


def _net_of(p):
    try:
        import apex_card_signals as _csig
        return _csig.net_score(p)[2]
    except Exception:
        return 0


NOPENALTY_TOP3_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apex_top3_nopenalty_log.csv")
_NOPENALTY_FIELDS = ["captured_at", "source", "match_date", "position", "player", "opponent",
                     "category", "line", "direction", "prop_type", "prob"]


def _write_nopenalty_top3(date_str, source, scored):
    """APEX-175RANK-2026-10-22: one row per Top 3 pick as it would be without the 17.5
    push-down (LINE175_*_PENALTY = 0). Fail-open."""
    try:
        if not scored:
            return
        new = not os.path.exists(NOPENALTY_TOP3_LOG)
        cap = datetime.now().isoformat(timespec="seconds")
        with open(NOPENALTY_TOP3_LOG, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_NOPENALTY_FIELDS)
            if new:
                w.writeheader()
            for i, d in enumerate(scored, 1):
                p = d.get("p") or {}
                w.writerow({"captured_at": cap, "source": source, "match_date": date_str, "position": i,
                            "player": p.get("player"), "opponent": p.get("opponent"),
                            "category": p.get("category"), "line": p.get("line"),
                            "direction": p.get("direction"), "prop_type": str(p.get("prop_type") or "").lower(),
                            "prob": "%.4f" % _board_prob(p)})
    except Exception as e:
        print("[175Rank] no-penalty write failed (%s): %s" % (type(e).__name__, e))


HOLDS_OFF_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apex_dffs_holds_off_log.csv")
_HOLDS_OFF_FIELDS = ["captured_at", "source", "match_date", "board"] + _NOPENALTY_FIELDS[3:]


def _write_holds_off(date_str, source, boards):
    """APEX-DFFANTASY-2026-10-22: one row per Top 3 / Elite 6 pick as it would be without the
    Fantasy LESS / DF holds. Fail-open."""
    try:
        if not any(boards.values()):
            return
        new = not os.path.exists(HOLDS_OFF_LOG)
        cap = datetime.now().isoformat(timespec="seconds")
        with open(HOLDS_OFF_LOG, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_HOLDS_OFF_FIELDS)
            if new:
                w.writeheader()
            for flag, scored in boards.items():
                for i, d in enumerate(scored[:_BOARD_LIMITS.get(flag, 6)], 1):
                    p = d.get("p") or {}
                    w.writerow({"captured_at": cap, "source": source, "match_date": date_str, "board": flag,
                                "position": i, "player": p.get("player"), "opponent": p.get("opponent"),
                                "category": p.get("category"), "line": p.get("line"),
                                "direction": p.get("direction"), "prop_type": str(p.get("prop_type") or "").lower(),
                                "prob": "%.4f" % _board_prob(p)})
    except Exception as e:
        print("[DFFSHolds] without-holds write failed (%s): %s" % (type(e).__name__, e))


# APEX-ORDERGUARD-2026-10-25: each board's wider qualifier list as _rerank_after_top3 saw it
# (flag -> (time, entries)); apex_order_guard re-ranks it on its own to check the posted order.
# Nothing reads it to build a board.
LAST_RANK_POOLS = {}


_FIRST_SET_BOARDS = ("top3", "elite6", "top10", "bestgoblin")   # Goblin lines, so not Best Demon / Best Standard
# APEX-FIXCHECKER-2026-11-02 (owner, "Go fix checker"): a first-set pick that loses its numbered spot is a
# BACKUP like any other pick (Rublev v Etcheverry 8.5 vanished 11/2 9:25 AM). Undo: FS_BACKUPS = False.
FS_BACKUPS = True
ELITE6_SIZE = 6   # APEX-FILLNEXT-2026-10-29: spots on the Elite 6 board (backup labels)


EMPTY_BOARD_FIX = True   # APEX-EMPTYBOARD-2026-11-11. Undo: False + restart (or go back to before-fix-empty-board)


def _first_set_only(flag):
    """APEX-EMPTYBOARD-2026-11-11: a board with no regular pick: the strict first-set picks alone, Ranking order, one per
    match, up to the board's size, lower-level players left out (same as apex_soon._fs_only). Never raises."""
    try:
        import apex_set_games as _sg
        fs = list(_sg.board_entries() or [])
        try:
            import apex_player_checks as _pcf
            fs = [x for x in fs if not _pcf.disqualified(x.get("p") or {})]
        except Exception:
            pass
        out, used = [], set()
        for d in sorted(fs, key=lambda x: -float(x.get("score") or 0)):
            nm = _prop_names(d.get("p") or {})
            if nm & used:
                continue
            used |= nm
            d = dict(d)
            p = d.get("p") or {}
            d["rerank_note"] = "\U0001F3BE #%d by Ranking %d, from this rule's long-run first-set record (no signal points yet)" % (
                len(out) + 1, round(100 * _board_prob(p)))
            d["rerank_note"] += (". Engine: %d for this match (breaks ties)" % round(100 * p["fs_engine"])) if p.get("fs_engine") \
                else ". Engine has no number for this match, so the closest match breaks ties"
            out.append(d)
            if len(out) >= (_BOARD_LIMITS.get(flag) or 6):
                break
        return out
    except Exception as e:
        print("[EmptyBoard] first-set only skipped (%s)" % type(e).__name__)
        return []


def _rerank_after_top3(res, wide_fn, prob_fn=None, penalize=True, flag=None):
    """APEX-RERANKLIVE-2026-10-22: in place on one board's return_scored dict. Keeps
    the first 3, then fills the board's other slots by Win Probability + signal
    nudges from the board's own wider qualifier list (one per match/player). Marks
    each moved prop with d['rerank_note']. Never raises."""
    try:
        if not isinstance(res, dict):
            return
        scored = res.get("scored") or []
        n = len(scored)
        # APEX-175BACKUPS-2026-10-22: a 17.5 skip never keeps a numbered spot (live boards only)
        # APEX-DFFANTASY-2026-10-22: the Fantasy LESS / DF holds too (board_hold_reason)
        _skip = (lambda d: bool(board_hold_reason(d.get("p") or {}))) if penalize else (lambda d: False)
        _had_skip = any(_skip(d) for d in scored)
        if _had_skip:
            scored = [d for d in scored if not _skip(d)]
            res["scored"] = scored
        keep = 0 if TOP3_NET_SIGNAL else 3   # APEX-TOP3NET-2026-10-22
        if n <= keep:
            # APEX-EMPTYBOARD-2026-11-11 (owner, "Go fix empty boards"): with no regular pick on the board this
            # returned before the first-set picks were added, so a first-set pick that passed never reached an empty board
            # (11/11: Musetti v Humbert 1st Set 8.5 MORE). Same first-set-only order /soon already uses.
            if EMPTY_BOARD_FIX and not scored and penalize and flag in _FIRST_SET_BOARDS and not _LINE175_RULES_OFF \
                    and not _DF_FS_HOLDS_OFF and not _BIG_FAV_OFF:
                _fso = _first_set_only(flag)
                if _fso:
                    res["scored"] = _fso
                    LAST_RANK_POOLS[flag] = (datetime.now(), list(_fso))
            return
        wide = wide_fn()
        if penalize and not _LINE175_RULES_OFF and not _DF_FS_HOLDS_OFF:   # APEX-ORDERGUARD-2026-10-25: read-only copy for the order check
            LAST_RANK_POOLS[flag or "pp"] = (datetime.now(), list((wide.get("scored") if isinstance(wide, dict) else None) or scored))
        pool = [d for d in ((wide.get("scored") if isinstance(wide, dict) else None) or scored) if not _skip(d)] or scored
        # APEX-RANKFIRSTSET-2026-11-01 (owner, "Go rank first set"): strict first-set picks fight for spots in
        # Ranking order like any other pick. Undo: apex_set_games.SET_GAMES_RANK = False + SET_GAMES_FILL = True.
        if penalize and flag in _FIRST_SET_BOARDS and not _LINE175_RULES_OFF and not _DF_FS_HOLDS_OFF and not _BIG_FAV_OFF:
            try:
                import apex_set_games as _sg
                _fse = _sg.board_entries()
                try:   # APEX-FIXCHECKER-2026-11-02: lower-level players are off every board, first-set picks too
                    import apex_player_checks as _pcf
                    _fse = [x for x in _fse if not _pcf.disqualified(x.get("p") or {})]
                except Exception:
                    pass
                # APEX-FSWINDOW-2026-11-01 (owner): same day window as the regular picks: a first-set pick
                # can't start later than the board's latest regular pick (Teichmann v Garland, Tue, on Monday's board).
                _rs = [_current_start_utc(x.get("p") or {}) for x in pool]
                _rs = [x for x in _rs if x]
                if _rs:
                    from zoneinfo import ZoneInfo as _ZI
                    _ctd = lambda v: datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(_ZI("America/Chicago")).date()
                    _last = max(_ctd(v) for v in _rs)   # by Central calendar day, not by the hour
                    _fst = lambda x: _current_start_utc(x["p"]) or x["p"].get("slip_start_utc")
                    _fse = [x for x in _fse if not _fst(x) or _ctd(_fst(x)) <= _last]   # no start: not one bad row wipes all
                if _fse:
                    pool = list(pool) + _fse
                    n = _BOARD_LIMITS.get(flag) or n
                    LAST_RANK_POOLS[flag] = (datetime.now(), list(LAST_RANK_POOLS.get(flag, (None, []))[1]) + _fse)
            except Exception as e:
                print("[RankFirstSet] skipped (%s)" % type(e).__name__)
        # APEX-TOP3NET-2026-10-22: the wider list can pad with props under the board bar
        # (70% Win Prob / Apex 67); never let those in. Boards with their own bar
        # (Best Demon 60%) already only list their own qualifiers.
        if all(_board_qualifies(x) for x in scored):
            pool = [x for x in pool if _board_qualifies(x)] or scored
        _cb = _cushion_block(pool, wide_fn, flag, _BOARD_LIMITS.get(flag) or n, prob_fn) if penalize else set()   # APEX-CUSHIONFILL-2026-11-01
        if penalize and not _LINE175_RULES_OFF and not _DF_FS_HOLDS_OFF:
            _cushion_block_save(flag, _cb)
        if _cb:
            pool = [x for x in pool if tuple(_apex_prop_identity(x.get("p") or {})) not in _cb]
            scored = [x for x in scored if tuple(_apex_prop_identity(x.get("p") or {})) not in _cb]
            res["scored"] = scored
        # APEX-GOBLINFIX-2026-11-01 (owner, "Go all three"): adding first-set picks capped Best Goblin at 6;
        # it shows every 75+ Goblin, one per match or player, then fills to 6. Undo: GOBLIN_SHOW_ALL_FIX = False.
        if flag == "bestgoblin" and GOBLIN_SHOW_ALL_FIX and BEST_GOBLIN_SHOW_ALL_MIN_PROB:
            _gu, _gn = set(), 0
            for d in sorted(pool, key=lambda x: -_board_prob(x.get("p") or {})):
                _nm = _prop_names(d.get("p") or {})
                if _board_prob(d.get("p") or {}) >= BEST_GOBLIN_SHOW_ALL_MIN_PROB and not (_nm & _gu):
                    _gu |= _nm
                    _gn += 1
            n = max(n, _gn)
        top = scored[:min(keep, len(scored))]
        win_rank, _wused = {}, set()   # APEX-TOP3NET-2026-10-22: one per match/player, like the board
        for d in sorted(pool, key=_bb_sortkey):
            nm = _prop_names(d.get("p") or {})
            if nm & _wused:
                continue
            _wused |= nm
            win_rank.setdefault(_apex_prop_identity(d.get("p") or {}), len(win_rank) + 1)
        used = set()
        for d in top:
            used |= _prop_names(d.get("p") or {})
        cands = []
        for d in pool:
            p = d.get("p") or {}
            k = _apex_prop_identity(p)
            if any(_apex_prop_identity(t.get("p") or {}) == k for t in top):
                continue
            cands.append((board_rank_score(p, (prob_fn or _board_prob)(d), penalize), d))   # APEX-NETSIGNAL / APEX-175RANK-2026-10-22
        cands.sort(key=lambda c: (-c[0], -float(c[1].get("score") or 0)))   # Apex Score breaks ties
        # APEX-TOPSLOTS75-2026-10-25: rank order with no top-spots rule, for the notes below
        sig_pos, _su = {}, set(used)
        for adj, d in cands:
            nm = _prop_names(d.get("p") or {})
            if nm & _su:
                continue
            _su |= nm
            sig_pos[id(d)] = len(sig_pos) + keep + 1
        rest, fallback = [], set()

        def _take(seq, upto):
            nonlocal used
            for adj, d in seq:
                if len(top) + len(rest) >= upto:
                    break
                nm = _prop_names(d.get("p") or {})
                if nm & used:
                    continue
                used |= nm
                rest.append((adj, d))

        # APEX-DEMONSTDBARS-2026-10-25: each board's own bar (Demon 62, Standard 68 on its rank
        # score, everyone else 75)
        _bar = top_slots_bar(flag)
        _bp = (prob_fn or _board_prob) if flag == "beststandard" else _board_prob
        _min = _bar / 100.0 if (_bar and not keep) else None
        if _min is not None:
            # APEX-TOPSLOTS75-2026-10-25: spots #1-3 take only bar+ picks, in rank order;
            # if a board has fewer, the empty top spots go to its highest plain Win Prob picks
            _take([c for c in cands if _bp(c[1]) >= _min - 1e-9], min(TOP_SLOTS_COUNT, n))
            _nhi = len(rest)
            _take(sorted(cands, key=lambda c: (-_bp(c[1]), -c[0])), min(TOP_SLOTS_COUNT, n))
            fallback = {id(d) for adj, d in rest[_nhi:]}
        _take(cands, n)
        # APEX-SHORTBOARD-2026-11-01 (owner, "Fix all"): a short pool still posts in Ranking order (no early return)
        out = list(top)
        for pos, (adj, d) in enumerate(rest, keep + 1):
            was = win_rank.get(_apex_prop_identity(d.get("p") or {}))
            d = dict(d)
            d.pop("rerank_note", None)
            # APEX-TOP3NET-2026-10-22: plain words (owner: "clear understanding on discord")
            _p = d.get("p") or {}
            _pen, _why = line175_penalty(_p) if penalize else (0.0, None)
            _sig = "its signals add up to %+d" % _net_of(_p)
            if _fantasy_no_points(_p):   # APEX-FANTASYPOINTS-2026-11-01
                _sig = "its Win Prob ranks it there (Fantasy signals add no points)"
            try:   # APEX-BLOWOUTDOUBLE-2026-10-25
                import apex_card_signals as _csig
                if _csig.blowout_doubled(_p):
                    _sig += " (\U0001F3DF\ufe0f blowout risk counts double)"
            except Exception:
                pass
            # APEX-175RANK-2026-10-22: say when the 17.5 check took points off
            _cut = (("its 17.5 check says skip, so it loses %d points" if _why == "skip" else
                     "its 17.5 check says not enough data yet, so it loses %d points") % round(_pen * 100)) if _pen and _why else ""
            _sp = sig_pos.get(id(rest[pos - keep - 1][1]))
            _bw = top_slots_words(flag)
            if _min is not None and id(rest[pos - keep - 1][1]) in fallback:
                d["rerank_note"] = ("\u26A0\uFE0F under %d%%: #%d holds a %.0f%% %s pick because this board has "
                                    "fewer than 3 picks at %d%%+ (top spots go to the highest %s)") % (
                    _bar, pos, 100 * _bp(_p), _bw, _bar, _bw)
            elif _min is not None and pos > TOP_SLOTS_COUNT and _sp and _sp <= TOP_SLOTS_COUNT \
                    and _bp(_p) < _min - 1e-9:
                d["rerank_note"] = ("\u2B07\uFE0F Under %d%% %s (%.0f%%), can't take a top-3 spot: #%d "
                                    "(its signals alone had it #%d)") % (_bar, _bw, 100 * _bp(_p), pos, _sp)
            elif _min is not None and pos <= TOP_SLOTS_COUNT and _sp and _sp > pos:
                d["rerank_note"] = ("\u2B06\uFE0F Up to #%d: %.0f%% %s, and top-3 spots need %d%%+ "
                                    "(its signals alone had it #%d)") % (pos, 100 * _bp(_p), _bw, _bar, _sp)
            elif was and was > pos:
                d["rerank_note"] = "\u2B06\uFE0F Moved up to #%d because %s%s (Win Probability alone had it #%d)" % (
                    pos, _sig, (", even though " + _cut) if _cut else "", was)
            elif was and was < pos:
                if _cut:
                    d["rerank_note"] = "\u2B07\uFE0F Moved down to #%d: %s (Win Probability alone had it #%d)" % (
                        pos, _cut, was)
                else:
                    d["rerank_note"] = "\u2B07\uFE0F Moved down to #%d because %s (Win Probability alone had it #%d)" % (
                        pos, _sig, was)
            elif not was:
                d["rerank_note"] = "\u2B06\uFE0F In at #%d because %s%s (Win Probability alone left it off)" % (
                    pos, _sig, (", even though " + _cut) if _cut else "")
            if _p.get("category") == "1st_set_total_games":   # APEX-RANKFIRSTSET-2026-11-01
                d["rerank_note"] = "\U0001F3BE #%d by Ranking %d, from this rule's long-run first-set record (no signal points yet)" % (
                    pos, round(100 * _board_prob(_p)))
                # APEX-FSHYBRID-2026-11-01
                d["rerank_note"] += (". Engine: %d for this match (breaks ties)" % round(100 * _p["fs_engine"])) if _p.get("fs_engine") \
                    else ". Engine has no number for this match, so the closest match breaks ties"
            out.append(d)
        res["scored"] = out
    except Exception as e:
        print("[RerankLive] board skipped (%s): %s" % (type(e).__name__, e))


def _write_published_snapshot(date_str, results):
    """APEX-PUBLISHEDBOARD-2026-10-07: writes PUBLISHED_BOARD_SNAPSHOT --
    the frozen board data published_board_detail() reads. `results` maps
    board flag -> the return_scored=True dict from that board's function
    (the SAME computation capture_board_membership() already did for the
    CSV log, reused here rather than recomputed)."""
    boards = {}
    for flag, res in results.items():
        scored = (res.get("scored") or []) if isinstance(res, dict) else []
        boards[flag] = {"entries": scored}
        if flag in ("beststandard", "bestdemon") and isinstance(res, dict) and res.get("held_back"):
            boards[flag]["held"] = [{"p": h.get("p"), "reason": h.get("reason"), "score": h.get("score")}
                                    for h in res["held_back"]]   # APEX-STANDARDHOLD-2026-10-23
        if not scored:   # APEX-OPS-2026-10-10: keep the board's own empty-board reason
            boards[flag]["empty_text"] = res if isinstance(res, str) else (res.get("text") if isinstance(res, dict) else "")
    snap = {
        "match_date": date_str,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "boards": boards,
    }
    tmp_path = PUBLISHED_BOARD_SNAPSHOT + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(snap, f, default=str)
    os.replace(tmp_path, PUBLISHED_BOARD_SNAPSHOT)   # atomic -- readers never see a half-written file


# ================================================================ APEX-HONESTPROB-2026-10-10
# owner, approved "go" 2026-10-10 (Master Audit, item R9 -- shadow
# mode). One smooth correction curve (Platt scaling) fitted on settled,
# playable, non-Fantasy-Score props: corrected = 1/(1+exp(-(a*logit(p)+b))).
# Unseen-day test (Sep 6-Oct 9, 2,332 props): best Brier score of the methods
# tried (0.2411 vs raw 0.2419); props still 70%+ after correction won 77.3%
# (n=66) vs 69.0% for raw 70%+ (n=313). DISPLAY AND LOGGING ONLY: no board
# ranks or qualifies on this value until it passes its 100-prop check.
# Coefficients are refit daily by apex_daily_board_report.py (fit uses only
# results settled before the fit date) and stored in PROB_CORRECTION_FILE.
import math as _math
PROB_CORRECTION_FILE = os.path.join(HERE, "apex_prob_correction_v1.json")
PROB_CORRECTION_MIN_N = 400
_PROB_CORR_CACHE = {"mtime": None, "coef": None}


def _logit(p):
    p = min(max(float(p), 1e-4), 1 - 1e-4)
    return _math.log(p / (1 - p))


def _correction_rows(picks=None, before=None):
    """Settled rows the correction is fitted on: playable (no Demon/Goblin
    LESS), not Fantasy Score, with a probability. Uses the calibrated
    probability where one was logged -- the same value boards read."""
    picks = settled_picks() if picks is None else picks
    out = []
    for p in picks:
        if p.get("hit") is None:
            continue
        if before and str(p.get("match_date") or "") >= before:
            continue
        if p.get("category") == "fantasy_score":
            continue
        if p.get("prop_type") in ("demon", "goblin") and p.get("direction") == "LESS":
            continue
        pr = p.get("prob_cal") if p.get("prob_cal") not in (None, "") else p.get("prob")
        if pr is None:
            continue
        out.append((float(pr), 1.0 if p["hit"] else 0.0))
    return out


def fit_prob_correction(picks=None, before=None, write=True):
    """Fit (a, b) by logistic regression on logit(prob). Returns the coef
    dict (or None when fewer than PROB_CORRECTION_MIN_N rows)."""
    from datetime import date as _dd
    before = before or _dd.today().isoformat()
    rows = _correction_rows(picks, before=before)
    if len(rows) < PROB_CORRECTION_MIN_N:
        return None
    a, b = 1.0, 0.0
    xs = [(_logit(p), y) for p, y in rows]
    for _ in range(400):
        ga = gb = 0.0
        for x, y in xs:
            e = 1.0 / (1.0 + _math.exp(-(a * x + b))) - y
            ga += e * x; gb += e
        a -= 0.5 * ga / len(xs); b -= 0.5 * gb / len(xs)
    coef = {"a": round(a, 5), "b": round(b, 5), "n": len(rows), "fit_before": before,
            "fitted_at": datetime.now().isoformat(timespec="seconds"),
            "method": "platt (logistic on logit of calibrated probability)"}
    if write:
        tmp = PROB_CORRECTION_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(coef, f, indent=1)
        os.replace(tmp, PROB_CORRECTION_FILE)
    return coef


def _prob_correction_coef():
    try:
        m = os.path.getmtime(PROB_CORRECTION_FILE)
    except Exception:
        return None
    if _PROB_CORR_CACHE["mtime"] != m:
        try:
            with open(PROB_CORRECTION_FILE) as f:
                _PROB_CORR_CACHE["coef"] = json.load(f)
        except Exception:
            _PROB_CORR_CACHE["coef"] = None
        _PROB_CORR_CACHE["mtime"] = m
    return _PROB_CORR_CACHE["coef"]


def corrected_prob(prob, coef=None):
    """Honest Probability for a stated probability, or None if no fit yet."""
    coef = coef or _prob_correction_coef()
    if not coef or prob in (None, ""):
        return None
    try:
        return 1.0 / (1.0 + _math.exp(-(coef["a"] * _logit(prob) + coef["b"])))
    except Exception:
        return None


# ================================================================ APEX-BOARDSTATUS-2026-10-10
# owner, approved "go" 2026-10-10 (Master Audit R3/R5). One row per
# prop per publish saying where it ended up and why, with the publish time
# and whether it came from the 5 PM auto run, a manual /fetch, or a
# republish. This is what the official win rate (first publish of the day,
# recommended props only), false-negative tracking, board stability and the
# board-health funnel are computed from. board="all" rows carry every prop's
# pre-board outcome; per-board rows cover every prop that passed the 70%/67
# bar (recommended / held back with its reason / watchlist / not shown).
BOARD_STATUS_LOG = os.path.join(HERE, "apex_board_status_log_v1.csv")
_BOARD_STATUS_FIELDS = ["captured_at", "source", "match_date", "board", "player", "opponent",
                        "category", "line", "direction", "prop_type", "prob", "prob_honest",
                        "apex_score", "status", "reason", "position", "start_utc"]
_STATUS_BOARD_TYPE = {"bestgoblin": "goblin", "bestdemon": "demon", "beststandard": "standard"}


def _prop_start_utc(p):
    s = _current_start_utc(p)
    if not s:
        raw = str(p.get("start_time_raw") or "").strip()
        if raw:
            try:
                from datetime import timezone as _tz
                d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=_tz.utc)
                s = d.astimezone(_tz.utc).isoformat(timespec="seconds")
            except Exception:
                s = ""
    return s


def _h2h_line(p):
    """APEX-H2H-2026-10-22 (owner, "H2h card line go"): one 🤝 head-to-head line
    under the start time on every card. Display only; switch apex_h2h.H2H_CARD_LINE."""
    try:
        import apex_h2h
        return apex_h2h.h2h_card_line(p)
    except Exception:
        return None


def start_time_line(p):
    """APEX-STARTTIME-2026-10-22 (owner): "🕒 Starts Thu 10/22, 2:30 PM CT" -- the match
    start in Central time, shown right under the matchup on every board card. Uses the
    same start time the boards use to hide started matches (PrizePicks, moved later when
    PrizePicks reschedules). Display only; says so plainly when no time is listed."""
    try:
        s = _prop_start_utc(p)
        if not s:
            return "🕒 Start time not listed yet"
        from datetime import timezone as _sttz
        from zoneinfo import ZoneInfo as _stzi
        st = datetime.fromisoformat(s)
        if st.tzinfo is None:
            st = st.replace(tzinfo=_sttz.utc)
        ct = st.astimezone(_stzi("America/Chicago"))
        when = "%s %d/%d, %d:%02d %s CT" % (
            ct.strftime("%a"), ct.month, ct.day, ct.hour % 12 or 12, ct.minute, "AM" if ct.hour < 12 else "PM")
        # APEX-ANYDATE-2026-10-25: boards mix every upcoming date, so a pick that isn't today
        # says so in bold ("🕒 **Starts Mon 10/26, 4:00 AM CT** · not today")
        if ct.date() != datetime.now(_stzi("America/Chicago")).date():
            return "🕒 **Starts %s** · not today" % when
        return "🕒 Starts %s" % when
    except Exception:
        return "🕒 Start time not listed yet"


# APEX-SURFACEHOLD-2026-10-22 (owner, "Go"): Aces, Breaks, Double Faults and
# Fantasy depend on the court. A prop in those categories is held off every board when
# its tournament's surface is UNKNOWN, or when its newest score used a surface that
# disagrees with the surface now confirmed (finished matches / override). It comes back
# by itself once it is re-scored on the right surface. False = off.
SURFACE_HOLD = True
SURFACE_HOLD_CATS = frozenset({"aces", "breaks", "dfs", "fantasy_score"})
# APEX-SURFACE-AUTOLOOKUP-2026-10-27 (owner, "Go"): while a tournament's court is still
# UNKNOWN (no history, no override, web lookup failed), EVERY one of its props is held off the
# boards, not just the court-sensitive categories. False = the Oct 22 behaviour (those four only).
SURFACE_HOLD_ALL_UNKNOWN = True
_SURF_ROWS = {"mtime": None, "rows": {}}


def _surface_hold(p):
    """Reason string if this prop must be held for its surface, else ''. Never raises."""
    try:
        if not SURFACE_HOLD:
            return ""
        _court_cat = p.get("category") in SURFACE_HOLD_CATS
        if not _court_cat and not SURFACE_HOLD_ALL_UNKNOWN:
            return ""
        mt = os.path.getmtime(PICK_LOG)
        if _SURF_ROWS["mtime"] != mt:
            rows = {}
            from datetime import date as _d, timedelta as _td
            cut = (_d.today() - _td(days=3)).isoformat()
            for r in _read(PICK_LOG):
                if (r.get("match_date") or "") >= cut:
                    rows[(r.get("match_id"), r.get("category"), str(r.get("line")), str(r.get("prop_type") or "").lower())] = (
                        r.get("tournament") or "", (r.get("surface") or "").lower())
            _SURF_ROWS.update(mtime=mt, rows=rows)
        tour, used = _SURF_ROWS["rows"].get((p.get("match_id"), p.get("category"), str(p.get("line")),
                                             str(p.get("prop_type") or "").lower()), ("", ""))
        if not tour:
            return ""
        import apex_surface_map as _asm
        st = _asm.surface_status(tour)
        if st == "unknown":
            return "surface unknown for %s" % tour
        if st == "confirmed" and _court_cat:
            want = _asm.lookup(tour)[0].lower()
            if used and used != want:
                return "scored on %s, but %s is %s -- waiting for a re-score" % (used, tour, want)
        return ""
    except Exception as _fe:
        _failnote("_surface_hold", _fe)
        return ""


def _pre_board_reason(p, rec):
    """(status, reason, apex_score) for one prop before any board-specific
    rule -- the same gates every board applies, in the same order."""
    cat = p.get("category")
    prob = _board_prob(p)
    try:   # APEX-BANLIST-2026-10-28: banned list first, same for every board
        import apex_ban_list as _abl
        _bn = _abl.reason(p)
        if _bn:
            return "excluded", _bn, None
    except Exception:
        pass
    if not cat:
        return "excluded", "market not modeled", None
    if str(p.get("playable") or "") == "0" or (
            str(p.get("prop_type") or "").lower() in ("goblin", "demon")
            and str(p.get("direction") or "").upper() == "LESS"):
        return "excluded", "unplayable direction (MORE-only line, engine says LESS)", None
    if _pub_match_started(p):
        return "excluded", "match already started", None
    _sh = _surface_hold(p)   # APEX-SURFACEHOLD-2026-10-22
    if _sh:
        return "excluded", "surface check: " + _sh, None
    if cat == "fantasy_score" and not BOARDS_INCLUDE_FANTASY_SCORE:
        return "excluded", "Fantasy Score kept off ranked boards", None
    if cat in DISABLED_CATEGORIES:
        return "excluded", "category disabled", None
    if cat in BOARD_EXCLUDED_CATEGORIES:
        return "excluded", "category kept off boards (Breaks)", None
    if not _category_has_evidence(cat, rec):
        return "excluded", "category not yet proven (<%d settled)" % UNPROVEN_CATEGORY_MIN_N, None
    if not _category_probability_gate(cat, prob):
        return "excluded", "below category minimum probability", None
    try:
        sc = apex_score(prob, cat, bool(p.get("bo5")), rec)[0]
    except Exception:
        return "excluded", "missing data (could not score)", None
    if prob < BOARD_MIN_PROB - 1e-9:
        return "excluded", "Win Probability under 70%", sc
    if sc < BOARD_MIN_SCORE:
        return "excluded", "Apex Score under 67", sc
    return "eligible", "passes 70% and 67", sc


_BOARD_LIMITS = {"top3": 3, "elite6": 6, "top10": 10, "bestgoblin": 6, "bestdemon": 6, "beststandard": 6}


def _match_names_key(p):
    return frozenset(x.strip().lower() for x in (str(p.get("opponent") or "").split(" v ")
                     if p.get("player") == "match" else [p.get("player"), p.get("opponent")]))


def _passes_standard_rule(p):
    """Same market test best_standard_detail applies (APEX-BOARDAUDIT-2026-10-19, labels only)."""
    try:
        pr = _board_prob(p)
        mkt = (p.get("category"), str(p.get("direction") or "").upper())
        if mkt in STANDARD_MARKETS:
            hp = corrected_prob(pr)
            return hp is not None and hp >= STANDARD_MIN_HONEST - 1e-9 and not _df_less_short(p, pr)
        if mkt == ("fantasy_score", "LESS"):
            return pr >= STANDARD_FS_LESS_MIN_PROB - 1e-9
        if p.get("category") == "total_games":
            bp = p.get("apex_book_prob")
            return bp is not None and pr >= STANDARD_BOOK_MIN_MODEL - 1e-9 and float(bp) >= STANDARD_BOOK_MIN_BOOK - 1e-9
    except Exception:
        return True
    return False


def write_board_status(picks, results, date_str, source="unknown", rec=None, path=None):
    """Append this publish's status rows. `results` = board flag -> the
    return_scored result each board function produced for this publish.
    Fail-open by contract: any error is printed, never raised."""
    try:
        path = path or BOARD_STATUS_LOG
        rec = rec or records()
        captured_at = datetime.now().isoformat(timespec="seconds")
        coef = _prob_correction_coef()
        rows, eligible = [], []

        def _row(p, board, status, reason, sc=None, pos=""):
            pr = _board_prob(p)
            hp = corrected_prob(pr, coef)
            return {"captured_at": captured_at, "source": source, "match_date": date_str, "board": board,
                    "player": p.get("player"), "opponent": p.get("opponent"), "category": p.get("category"),
                    "line": p.get("line"), "direction": p.get("direction"),
                    "prop_type": str(p.get("prop_type") or "").lower(),
                    "prob": "%.4f" % pr, "prob_honest": ("%.4f" % hp) if hp is not None else "",
                    "apex_score": ("%.1f" % float(sc)) if sc is not None else "",
                    "status": status, "reason": reason, "position": pos, "start_utc": _prop_start_utc(p)}

        seen = set()
        for p in picks:
            k = _apex_prop_identity(p)
            if k in seen:
                rows.append(_row(p, "all", "excluded", "duplicate market (same prop listed twice)"))
                continue
            seen.add(k)
            st, why, sc = _pre_board_reason(p, rec)
            rows.append(_row(p, "all", st, why, sc))
            if st == "eligible":
                eligible.append((k, p, sc))

        for flag, res in results.items():
            res = res if isinstance(res, dict) else {"scored": [], "held_back": [], "watchlist": []}
            done = set()
            for i, d in enumerate(res.get("scored") or [], 1):
                p = d.get("p") or {}
                done.add(_apex_prop_identity(p))
                rows.append(_row(p, flag, "recommended", "on board", d.get("sc"), i))
            for h in res.get("held_back") or []:
                p = h.get("p") or {}
                k = _apex_prop_identity(p)
                if k in done:
                    continue
                done.add(k)
                rows.append(_row(p, flag, "held_back", str(h.get("reason") or "held back"), h.get("score")))
            for d in res.get("watchlist") or []:
                p = d.get("p") or {}
                k = _apex_prop_identity(p)
                if k in done:
                    continue
                done.add(k)
                rows.append(_row(p, flag, "watchlist", "watchlist", d.get("sc")))
            want = _STATUS_BOARD_TYPE.get(flag)
            # APEX-BOARDAUDIT-2026-10-19: say which rule actually kept a qualified prop off
            # (the old catch-all "board full or one-per-player/match" was shown even when
            # the board had open slots and the real reason was the board's market rule).
            _shown = [d.get("p") or {} for d in (res.get("scored") or [])]
            _shown_m = {_match_names_key(x) for x in _shown}
            _shown_pl = {str(x.get("player") or "").strip().lower() for x in _shown if x.get("player") != "match"}
            for k, p, sc in eligible:
                if k in done:
                    continue
                if want and str(p.get("prop_type") or "").lower() != want:
                    continue
                if flag in ("bestgoblin", "beststandard", "bestdemon") and _category_gate_blocked(p):
                    why = "category on hold: settled hit rate well under Honest Probability (apex_category_gate)"
                elif flag == "beststandard" and not _passes_standard_rule(p):
                    why = "outside the Best Standard rule (DF LESS, GW MORE, FS LESS 75%+, or Total Games with bet365 agreeing)"
                elif _match_names_key(p) in _shown_m or (p.get("player") != "match"
                                                       and str(p.get("player") or "").strip().lower() in _shown_pl):
                    why = "one per match/player: another prop from this match is on the board"
                elif flag == "bestgoblin" and BEST_GOBLIN_SHOW_ALL_MIN_PROB and _board_prob(p) < BEST_GOBLIN_SHOW_ALL_MIN_PROB:
                    why = "under 75%%: Best Goblin shows every 75%%+ goblin, then fills to 6 (%d shown)" % len(_shown)
                elif _BOARD_LIMITS.get(flag) and len(_shown) >= _BOARD_LIMITS[flag]:
                    why = "board full: %d stronger props shown" % len(_shown)
                    _bar = top_slots_bar(flag)   # APEX-TOPSLOTS75 / APEX-DEMONSTDBARS-2026-10-25
                    _bv = _standard_rank_prob({"p": p}) if flag == "beststandard" else _board_prob(p)
                    if _bar and _bv < _bar / 100.0 - 1e-9:
                        why += " (under %d%% %s, can't take a top-3 spot)" % (_bar, top_slots_words(flag))
                else:
                    why = "qualified but not shown (board rule)"
                rows.append(_row(p, flag, "not_shown", why, sc))

        if path == BOARD_STATUS_LOG:   # APEX-NOBOOK-2026-10-24: separate tracker for the no-book-total hold
            try:
                import apex_nobook_tracker as _nbt
                _nbt.log_publish(results, date_str, source)
            except Exception as _e:
                print("[NoBookTracker] skipped (%s)" % type(_e).__name__)

        fresh = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_BOARD_STATUS_FIELDS)
            if fresh:
                w.writeheader()
            w.writerows(rows)
        if path == BOARD_STATUS_LOG:   # APEX-BOARDHISTORY-2026-10-25: permanent per-board history (data only)
            try:
                import apex_board_history as _bh
                _bh.log_publish(picks, results, date_str, source, rec, rows)
            except Exception as _e:
                print("[BoardHistory] skipped (%s)" % type(_e).__name__)
        return len(rows)
    except Exception as e:
        print("[BoardStatus] write failed (%s): %s" % (type(e).__name__, e))
        return 0


LAST_REPUBLISH_CHANGES = ""   # APEX-REFETCHREPORT-2026-10-11: text of the last change report


def _planner_legs(entries):
    """APEX-SLIPPLANNER-2026-10-11: board entries -> planner legs (Honest Probability)."""
    out = []
    for e in entries:
        p = e.get("p") or {}
        out.append(dict(p, prob_honest=corrected_prob(_board_prob(e))))
    return out


def _snap_board_props(snap):
    """{identity: entry p} for every prop on any board of a snapshot dict."""
    out = {}
    for flag, b in ((snap or {}).get("boards") or {}).items():
        for e in (b or {}).get("entries") or []:
            p = e.get("p") or {}
            out.setdefault(_apex_prop_identity(p), dict(p, _boards=set()))["_boards"].add(flag)
    return out


def _market_key(p):
    """Same match + category + prop type, ignoring the line (to spot line moves)."""
    if str(p.get("player") or "") == "match":
        who = tuple(sorted(x.strip().lower() for x in str(p.get("opponent") or "").split(" v ")))
    else:
        who = (str(p.get("player") or "").strip().lower(), str(p.get("opponent") or "").strip().lower())
    return (who, p.get("category"), str(p.get("prop_type") or "").lower(), str(p.get("direction") or "").upper())


def _prop_label(p):
    who = p.get("opponent") if str(p.get("player")) == "match" else "%s vs %s" % (p.get("player"), p.get("opponent"))
    return "%s -- %s %s %s (%s)" % (who, LABEL.get(p.get("category"), p.get("category")), p.get("line"),
                                     p.get("direction"), str(p.get("prop_type") or "").capitalize())


def board_change_report(old_snap, new_snap, fetch_started=None):
    """APEX-REFETCHREPORT-2026-10-11 (owner, 2026-10-11: "if the line has
    moved when I do a refetch the system should see it and let me know"). Compares
    the boards before and after a publish and lists, in plain words: line moves,
    lines no longer offered, props new to the boards, Win Probability moves of 2+
    points, and board props kept on a price this fetch could not refresh."""
    old, new = _snap_board_props(old_snap), _snap_board_props(new_snap)
    old_by_mkt = {}
    for k, p in old.items():
        old_by_mkt.setdefault(_market_key(p), []).append(p)
    L, moved_mkts = [], set()
    pct = lambda p: "%d%%" % round(_board_prob(p) * 100)
    for k, p in new.items():
        if k in old:
            d = _board_prob(p) - _board_prob(old[k])
            if abs(d) >= 0.02:
                L.append("\U0001F4C9 PROBABILITY MOVED: %s -- %s -> %s" % (_prop_label(p), pct(old[k]), pct(p)) if d < 0 else
                         "\U0001F4C8 PROBABILITY MOVED: %s -- %s -> %s" % (_prop_label(p), pct(old[k]), pct(p)))
            continue
        prev = [q for q in old_by_mkt.get(_market_key(p), []) if _apex_prop_identity(q) not in new]
        if prev:
            q = prev[0]
            moved_mkts.add(_apex_prop_identity(q))
            L.append("\u26a0\ufe0f LINE MOVED: %s -- was %s at %s, now %s at %s"
                     % (_prop_label(p).split(" -- ")[0] + " -- " + LABEL.get(p.get("category"), p.get("category")),
                        q.get("line"), pct(q), p.get("line"), pct(p)))
        else:
            L.append("\U0001F195 NEW ON BOARDS: %s -- %s" % (_prop_label(p), pct(p)))
    for k, q in old.items():
        if k in new or k in moved_mkts:
            continue
        why = "match started" if _pub_match_started(q) else "no longer on the boards (line removed or dropped below the bar)"
        L.append("\u274c REMOVED: %s -- %s" % (_prop_label(q), why))
    stale = []
    if fetch_started:
        for k, p in new.items():
            ts = str(p.get("ts") or "")
            if p.get("apex_price_note") and ts:
                stale.append("\u23f3 PRICE NOT REFRESHED: %s -- %s, last priced %s" % (_prop_label(p), pct(p), ts.replace("T", " ")[:16]))
    if not L and not stale:
        return "\U0001F504 **Board changes since last publish:** none -- every board prop is unchanged."
    head = ["\U0001F504 **Board changes since last publish** (%s -> %s)"
            % (str((old_snap or {}).get("captured_at", "?")).replace("T", " ")[:16],
               str((new_snap or {}).get("captured_at", "?")).replace("T", " ")[:16])]
    return "\n".join(head + L + stale)


# APEX-NODOUBLEPOST-2026-11-02 (owner, "Go no double post"): on 11/2 2:29 PM the scheduler's
# auto-eval republish overlapped his /fetch; the fetch changed a bet365 total mid-build and the timed
# board was wrong for 30 s (13 order-check flags). Timed republishes now wait while a /fetch is running
# (the bot's data/logs/fetch_running.json marker, ignored after NO_DOUBLE_POST_MAX_MIN); the fetch posts.
# Undo: NO_DOUBLE_POST = False + restart, or "go back to before-no-double-post".
NO_DOUBLE_POST = True
NO_DOUBLE_POST_SOURCES = ("autoeval", "reprice")
NO_DOUBLE_POST_MAX_MIN = 45
_FETCH_RUNNING_MARKER = os.path.join(HERE, "data", "logs", "fetch_running.json")


def fetch_running_since():
    """Start time (epoch) of a /fetch in flight, else None. Never raises."""
    try:
        import time as _time, json as _json
        with open(_FETCH_RUNNING_MARKER) as _fh:
            _st = float(_json.load(_fh).get("started") or 0)
        return _st if _st and _time.time() - _st < NO_DOUBLE_POST_MAX_MIN * 60 else None
    except Exception:
        return None


def republish_boards(source="republish", fetch_started=None):
    """APEX-STARTCLOCK-2026-10-27: one frozen clock for the whole publish (see _pub_now)."""
    from datetime import timezone as _tz
    if NO_DOUBLE_POST and source in NO_DOUBLE_POST_SOURCES:   # APEX-NODOUBLEPOST-2026-11-02
        _fs = fetch_running_since()
        if _fs:
            try:
                from zoneinfo import ZoneInfo as _ZI
                _hm = datetime.fromtimestamp(_fs, _ZI("America/Chicago")).strftime("%I:%M %p").lstrip("0")
            except Exception:
                _hm = "?"
            print("[Republish] skipped (%s): a /fetch started %s CT is running and will post the board" % (source, _hm))
            return "skipped: /fetch running since %s CT" % _hm
    _PUB_CLOCK["now"] = datetime.now(_tz.utc)
    try:
        return _republish_boards_run(source=source, fetch_started=fetch_started)
    finally:
        _PUB_CLOCK["now"] = None


def _republish_boards_run(source="republish", fetch_started=None):
    """APEX-REPUBLISH-2026-10-10 (owner, approved "go" 2026-10-10):
    rebuild and publish today's boards from the current evaluated board
    (load_current_board) under the CURRENT rules -- same capture path a
    /fetch uses, without fetching. Since APEX-PUBLISHPOOL-2026-10-11 this is
    the ONE publish path for /fetch, the 5 PM run, scheduler restarts and the
    re-pricing job. `fetch_started` (a /fetch's start time) marks props whose
    price this fetch could not refresh; their cards say so. The before/after
    change report is left in LAST_REPUBLISH_CHANGES."""
    global LAST_REPUBLISH_CHANGES
    from datetime import date as _dd
    picks, note = load_current_board()
    if not picks:
        return "nothing to republish (%s)" % (note or "empty board")
    if fetch_started:
        try:
            with open(os.path.join(HERE, "data", "logs", "fetch_noprice.json")) as _fh:
                _noprice = json.load(_fh)
        except Exception:
            _noprice = {}
        marked = []
        for p in picks:
            ts = str(p.get("ts") or "")
            _np = _noprice.get("|".join(str(x) for x in _apex_prop_identity(p)), "")
            if ts and ts < fetch_started and _np >= fetch_started:
                p = dict(p, apex_price_note="\u23f3 Price not refreshed on this fetch -- using the last good price from %s"
                         % ts.replace("T", " ")[:16])
            marked.append(p)
        picks = marked
    try:
        with open(PUBLISHED_BOARD_SNAPSHOT) as f:
            old_snap = json.load(f)
    except Exception:
        old_snap = {}
    dates = {"match_date": _dd.today().isoformat()}
    try:
        md = sorted({str(p.get("match_date") or "") for p in picks if p.get("match_date")})
        if md:
            dates["match_date"] = md[0]
    except Exception:
        pass
    try:   # APEX-BOOKLINES-2026-10-11: bet365's view on Total Games cards + Best Standard's agreement rule
        import apex_book_lines as _bl
        # APEX-REPRICEBOOK-2026-10-13: a re-price republish reuses the logged bet365 lines (no API calls)
        picks = _bl.annotate(picks, _board_prob, corrected_prob, log=source in ("manual", "auto"),
                             offline=(source == "reprice"))
    except Exception as e:
        print("[BookLines] skipped (%s): %s" % (type(e).__name__, e))
    capture_board_membership(picks, dates, source=source)
    if source in ("manual", "auto"):   # APEX-STDPAPER-2026-10-12: first publish of the day logs the Standard paper test
        try:
            import apex_standard_papertest as _spt
            _spt.log_today(picks, source=source)
            import apex_goblin_papertest as _gpt   # APEX-GOBPAPER-2026-10-13: background only
            _gpt.log_today(picks, source=source)
            import apex_slipsize_papertest as _sspt   # APEX-SLIPSIZE-2026-10-21: background only
            _sspt.log_today(picks, source=source)
            import apex_signal_papertest as _sgpt   # APEX-SIGNALTEST-2026-10-21: background only
            _sgpt.log_today(picks, source=source)
            import apex_rerank_papertest as _rrpt   # APEX-RERANKTEST-2026-10-22: background only
            _rrpt.log_today(picks, source=source)
            import apex_blowout_signal as _bst   # APEX-BLOWOUT-2026-10-22: background only
            _bst.log_today(picks, source=source)
            import apex_pinnacle as _pinpt   # APEX-PINNACLE-2026-10-22: PINNACLE CUSHION paper test, background only
            _pinpt.log_today(picks, source=source)
            import apex_data_health as _dh   # APEX-DATAHEALTH-2026-10-22: alert on failing input checks (incl. surface)
            _dh.alert_new_failures()
        except Exception as e:
            print("[StdPaperTest] skipped (%s): %s" % (type(e).__name__, e))
    try:
        with open(PUBLISHED_BOARD_SNAPSHOT) as f:
            new_snap = json.load(f)
        LAST_REPUBLISH_CHANGES = board_change_report(old_snap, new_snap, fetch_started)
        if source in ("manual", "auto"):   # APEX-SLIPPLANNER-2026-10-11: record the recommended slip per board
            try:
                import apex_slip_planner as _sp
                for _flag, _b in (new_snap.get("boards") or {}).items():
                    _ents = (_b or {}).get("entries") or []
                    _legs = _planner_legs(_ents[:_PUBLISHED_FLAG_LIMIT.get(_flag, 6)])
                    _pl, _rec, _k = _sp.plan(_legs)
                    _sp.log_plan(new_snap.get("captured_at"), new_snap.get("match_date"), source, _flag, _rec, _legs)
            except Exception as e:
                print("[SlipPlanner] skipped (%s): %s" % (type(e).__name__, e))
    except Exception as e:
        LAST_REPUBLISH_CHANGES = ""
        print("[Republish] change report failed (%s): %s" % (type(e).__name__, e))
    try:   # APEX-UPDATETRACK-2026-10-24: every board build goes into the Slips v2 log right away
        import apex_slips_v2 as _v2   # (the #slips loop posts the card; the 5-min loop could miss quick rebuilds)
        _v2.log_build(source=source)
    except Exception as e:
        print("[SlipsV2] log after republish skipped (%s): %s" % (type(e).__name__, e))
    try:   # APEX-PICKSCLEAN-2026-10-24: the one short #picks plays card, edited only when the board changed
        import apex_picks_channel as _pcc
        print("[PicksChannel] " + _pcc.refresh_plays(source=source))
    except Exception as e:
        print("[PicksChannel] skipped (%s): %s" % (type(e).__name__, e))
    try:   # APEX-SOONSLIPS-2026-10-27: after every publish, Top 3 Soon (80+) / Elite 6 Soon (90+) ready cards
        import apex_soon_slips as _ss
        _ss.run(source=source)
    except Exception as e:
        print("[SoonSlips] skipped (%s): %s" % (type(e).__name__, e))
    try:   # APEX-LIVECHECK-2026-11-01
        import apex_live_check as _lc2
        _lc2.run(picks)
    except Exception as e:
        print("[LiveCheck] skipped (%s)" % type(e).__name__)
    return "republished %d props for %s" % (len(picks), dates["match_date"])


# APEX-STARTINDEX-2026-10-10 (owner, approved "go" 2026-10-10): the
# picks log never recorded a start time (slip_start_utc was always blank), so
# a started match never left a published board. apex_closing_line.py now
# refreshes this index from PrizePicks every 15 minutes (player -> opponent,
# start). Used only as a fallback when a prop has no slip_start_utc.
MATCH_START_INDEX = os.path.join(HERE, "data", "logs", "match_start_index.json")
_START_IDX_CACHE = {"mtime": None, "idx": {}}


def _start_index():
    try:
        m = os.path.getmtime(MATCH_START_INDEX)
    except Exception:
        return {}
    if _START_IDX_CACHE["mtime"] != m:
        try:
            with open(MATCH_START_INDEX) as f:
                _START_IDX_CACHE["idx"] = json.load(f).get("players", {})
        except Exception:
            _START_IDX_CACHE["idx"] = {}
        _START_IDX_CACHE["mtime"] = m
    return _START_IDX_CACHE["idx"]


def _indexed_start_utc(p):
    """Start time for a prop from the PrizePicks start index, '' if unknown.
    Matches on the player (or either player for a match row) and, when the
    index entry names an opponent, requires the other player to match too."""
    idx = _start_index()
    if not idx:
        return ""
    pl = str(p.get("player") or "").strip().lower()
    opp = str(p.get("opponent") or "").strip().lower()
    if pl == "match":
        pair = [x.strip() for x in opp.split(" v ")]
    else:
        pair = [pl, opp]
    hits = []
    for i, nm in enumerate(pair):
        other = pair[1 - i] if len(pair) == 2 else ""
        for e in idx.get(nm, []):
            eo = str(e.get("opponent") or "").lower()
            if (not other or not eo or other in eo or eo in other) and e.get("start_utc"):
                hits.append(e["start_utc"])
    return min(hits) if hits else ""   # earliest listed start for that match


def _current_start_utc(p):
    """APEX-RESCHEDULE-2026-10-12 (owner, "Go"): the prop's start time,
    moved later when PrizePicks has since rescheduled the match. A re-evaluation
    with an unchanged price writes no new pick-log row, so slip_start_utc kept
    the old time and the prop was hidden as "already started" (Tagger GW 10.5,
    Yastremska TG 21.5 on 10/12). Only a later index start within 3 days of the
    stored one counts, so an old rematch can never move a start."""
    s = str(p.get("slip_start_utc") or "").strip() or _indexed_start_utc(p)
    if not s:
        return s
    try:
        from datetime import timezone as _ctz, timedelta as _ctd
        st = datetime.fromisoformat(s)
        if st.tzinfo is None:
            st = st.replace(tzinfo=_ctz.utc)
        idx = _start_index()
        pl = str(p.get("player") or "").strip().lower()
        opp = str(p.get("opponent") or "").strip().lower()
        pair = [x.strip() for x in opp.split(" v ")] if pl == "match" else [pl, opp]
        best = st
        for i, nm in enumerate(pair):
            other = pair[1 - i] if len(pair) == 2 else ""
            for e in idx.get(nm, []) if idx else []:
                eo = str(e.get("opponent") or "").lower()
                if not (other and eo and (other in eo or eo in other)) or not e.get("start_utc"):
                    continue
                t2 = datetime.fromisoformat(e["start_utc"])
                if t2.tzinfo is None:
                    t2 = t2.replace(tzinfo=_ctz.utc)
                if best < t2 <= st + _ctd(days=3):
                    best = t2
        return s if best == st else best.isoformat()
    except Exception:
        return s


# APEX-STARTCLOCK-2026-10-27 (owner, "We need this fix", 12:20 AM ORDER CHECK cards):
# one clock per publish. Janice Tjen v Polina Kudermetova started at 12:20:00 AM, the exact
# minute publish #810 was built: boards built a few seconds BEFORE kept her at #6, the Top 3
# rows and the ORDER CHECK a few seconds AFTER called the match started. Now republish_boards()
# freezes "now" once at the start and every started-check in that publish (boards, backups,
# status rows, order guard) reads the same clock, so one publish can never disagree with itself.
_PUB_CLOCK = {"now": None}


def _pub_now():
    from datetime import timezone as _tz
    return _PUB_CLOCK["now"] or datetime.now(_tz.utc)


def _pub_match_started(p):
    """True only when this prop's own match-start timestamp has passed.
    No timestamp on the prop -> fails OPEN (never hides it for this
    reason) -- APEX-PUBLISHEDBOARD-2026-10-07: "time passing alone must
    never remove a prop" means an unknown start time is never treated as
    a reason to hide something."""
    s = _current_start_utc(p)   # APEX-STARTINDEX-2026-10-10
    if not s:
        return False
    try:
        from datetime import timezone as _ptz
        st = datetime.fromisoformat(s)
        if st.tzinfo is None:
            st = st.replace(tzinfo=_ptz.utc)
        return st <= _pub_now()   # APEX-STARTCLOCK-2026-10-27
    except Exception:
        return False


_PUB_GRADED_CACHE = {"mtime": None, "set": set()}

def _pub_is_graded(p):
    """True once HAND_LOG carries a settled result for this exact prop
    identity. Cached by HAND_LOG's own mtime so repeated calls in one
    render pass don't re-read the file per prop."""
    try:
        mtime = os.path.getmtime(HAND_LOG)
    except Exception:
        mtime = None
    if _PUB_GRADED_CACHE["mtime"] != mtime:
        graded = set()
        try:
            for g in _read(HAND_LOG):
                if str(g.get("actual") or "").strip():
                    graded.add((str(g.get("match_date") or "").strip(),
                                 str(g.get("player") or "").strip().lower(),
                                 str(g.get("opponent") or "").strip().lower(),
                                 str(g.get("category") or "").strip(),
                                 str(g.get("line") or "").strip()))
        except Exception:
            graded = set()
        _PUB_GRADED_CACHE["mtime"] = mtime
        _PUB_GRADED_CACHE["set"] = graded
    key = (str(p.get("match_date") or "").strip(),
           str(p.get("player") or "").strip().lower(),
           str(p.get("opponent") or "").strip().lower(),
           str(p.get("category") or "").strip(),
           str(p.get("line") or "").strip())
    return key in _PUB_GRADED_CACHE["set"]


_MORE_ONLY_TYPES = ("goblin", "demon", "red_demon")


def _is_unplayable(r):
    """APEX-MOREONLY-BACKSTOP-2026-10-13: playable=0, OR a LESS on a
    Goblin/Demon/Red Demon line whatever the playable flag says -- PrizePicks
    offers those lines MORE only, so a missing lock upstream can never put an
    unplayable LESS on a board again."""
    if str(r.get("playable") or "") == "0":
        return True
    try:   # APEX-BANLIST-2026-10-28: a banned player's whole match never takes any spot
        import apex_ban_list as _abl
        if _abl.is_banned(r):
            return True
    except Exception:
        pass
    return (str(r.get("prop_type") or "").strip().lower() in _MORE_ONLY_TYPES
            and str(r.get("direction") or "").strip().upper() == "LESS")


# APEX-TYPECHECK-2026-10-14 (owner, "Go" 2026-10-14): PrizePicks can
# re-type a line after it is published (Krueger Games Won 5.5 went Standard ->
# Goblin at 1:39 PM CT, 4 minutes after /beststandard showed it). The board
# commands read a snapshot, so they check each prop's type against the live
# PrizePicks board first. One partner-API call, cached 3 minutes, fail-open.
_LIVE_TYPES = {"ts": 0.0, "map": None}


def _live_type_map(max_age=180):
    """(category, player name, line) -> set of odds types PrizePicks offers
    right now, or None when the partner API can't be read."""
    import time as _t
    if _LIVE_TYPES["map"] is not None and _t.time() - _LIVE_TYPES["ts"] < max_age:
        return _LIVE_TYPES["map"]
    try:
        import apex_ops as _ops
        stat_map = _ops._core_const("_APEX_STAT_MAP") or {}
        m = {}
        for b in _ops.partner_api_board(caller="typecheck"):   # APEX-PPTHROTTLE-2026-10-16: shared 2-min pull
            k = (stat_map.get(b["stat_slug"], b["stat_slug"]), str(b["player"] or "").strip().lower(), float(b["line"]))
            m.setdefault(k, set()).add(b["odds_type"] or "standard")
    except Exception as e:
        print("[TypeCheck] live board unavailable (%s: %s) -- showing the board unchecked" % (type(e).__name__, e))
        return None
    _LIVE_TYPES.update(ts=_t.time(), map=m)
    return m


def _retyped(p, live):
    """The type(s) PrizePicks now offers this exact line under, when that is
    no longer the type the prop was published with; '' otherwise (same type,
    line not on the live board, or no live board)."""
    if not live:
        return ""
    try:
        line = float(p.get("line"))
    except (TypeError, ValueError):
        return ""
    pl = str(p.get("player") or "").strip().lower()
    names = [x.strip() for x in str(p.get("opponent") or "").lower().split(" v ")] if pl == "match" else [pl]
    offered = set()
    for nm in names:
        offered |= live.get((p.get("category"), nm, line), set())
    cur = str(p.get("prop_type") or "standard").strip().lower()
    cur = "demon" if cur == "red_demon" else cur
    if not offered or cur in offered:
        return ""
    return "/".join(sorted(offered))


def _line_gone(p, live):
    """APEX-STALELINE-2026-10-14: None while PrizePicks still offers this exact
    line (any type -- a type flip is _retyped's job) or the live board can't
    be read; otherwise the sorted lines it now offers for the same player,
    category and type ([] when the line was pulled outright)."""
    if not live:
        return None
    try:
        line = float(p.get("line"))
    except (TypeError, ValueError):
        return None
    pl = str(p.get("player") or "").strip().lower()
    names = [x.strip() for x in str(p.get("opponent") or "").lower().split(" v ")] if pl == "match" else [pl]
    cat = p.get("category")
    if any(live.get((cat, nm, line)) for nm in names):
        return None
    if _two_pulls_counted(p):   # APEX-TWOPULLS-2026-11-01: a second pull 10+ minutes later must agree
        return None
    cur = str(p.get("prop_type") or "standard").strip().lower()
    cur = "demon" if cur == "red_demon" else cur
    return sorted({ln for (c, nm, ln), types in live.items() if c == cat and nm in names and cur in types})


# APEX-BOARDBACKUPS-2026-10-21 (owner: "we need to be sure that we have those
# posted at least as a backup somewhere on all boards"): every prop that passed the
# board bar (70% Win Probability and Apex 67) but did not get a slot on this board is
# listed under BACKUPS with why it missed (board full, or same match/player as a
# posted pick). Display only: backups take no numbered slot and stay out of #slips
# and board counts. Type boards list only their own type. 0 turns it off.
BOARD_BACKUPS_LIMIT = 15
# APEX-BACKUPSCMD-2026-10-26 (owner, 10/26 5:39 PM: "I want the backups to have its own '/'
# for each board rather than showing up"): board cards no longer carry the BACKUPS block or the
# "Backups in order" line; /top3backups, /elite6backups, /top10backups, /goblinbackups,
# /demonbackups, /standardbackups show them (backups_detail). Undo: BOARD_BACKUPS_ON_CARD = True.
BOARD_BACKUPS_ON_CARD = False
_BACKUPS_ONLY = False   # set by backups_detail() while it renders
BACKUPS_CMD = {"top3": "/top3backups", "elite6": "/elite6backups", "top10": "/top10backups",
               "bestgoblin": "/goblinbackups", "bestdemon": "/demonbackups", "beststandard": "/standardbackups"}
# APEX-BLOWOUT-2026-10-22 (owner: "we want the matches to be competitive", then "Go"):
# a Total Games Goblin MORE prop with under 3 games of room under bet365's main line takes
# no numbered slot on any board; it is listed under Held Back / BACKUPS with the reason and
# stays on /allprops. 10/22 study: under 3 games of room 24-18 (57%), 3+ games 106-26 (80%).
# False = those props rank as before (the card line still shows the warning).
LOW_CUSHION_BACKUPS = True


# APEX-DFFANTASY-2026-10-22: when a held pick had taken its player's/match's slot, the next
# qualifying prop from that player/match moves into the slot. Undo: False (slot goes empty).
HOLD_SLOT_MOVES_UP = True
# APEX-BESTLINE-2026-10-25 (owner, "also go with the go best line per match"): the one prop
# a match/player keeps on every board is its highest board-rank prop (board_rank_score: Win Prob +
# signal points, blowout double, 17.5 push-down), not its highest Win Prob. Held props still lose
# the slot to the next eligible one (HOLD_SLOT_MOVES_UP); the 75% top-3 and Demon/Standard bars
# apply after. Replay Oct 10-24: $[amount] vs $[amount]. Undo: BEST_RANK_PER_MATCH = False.
BEST_RANK_PER_MATCH = True


# APEX-FANTASYTG-2026-10-31 (owner, "Go fantasy tg rule"): the one exception to best rank per match.
# When a Goblin Fantasy MORE is 75%+ and that match's Goblin Total Games MORE is 70%+, Total Games takes the
# slot. Same-match Goblins since 10/10, Fantasy 75+ with Total Games 70+: Total Games 39-6, Fantasy 30-14
# (_backups/fs_vs_tg_2026-10-31). Replay on official slips 10/22-10/30: no day won or lost.
# Undo: FANTASY_TG_RULE = False.
FANTASY_TG_RULE = True
FANTASY_TG_FS_MIN, FANTASY_TG_TG_MIN = 0.75, 0.70


def _fantasy_tg_exception(tg, fs):
    """True when match prop tg takes the slot from player prop fs under FANTASY_TG_RULE. Never raises."""
    try:
        if not FANTASY_TG_RULE:
            return False
        a, b = tg.get("p") or {}, fs.get("p") or {}
        def _goblin_more(p, cat):
            return p.get("category") == cat and str(p.get("direction") or "").upper() == "MORE" \
                and str(p.get("prop_type") or "").lower() == "goblin"
        return a.get("player") == "match" and _goblin_more(a, "total_games") and _goblin_more(b, "fantasy_score") \
            and _board_qualifies(tg) and _board_qualifies(fs) \
            and _board_prob(tg) >= FANTASY_TG_TG_MIN and _board_prob(fs) >= FANTASY_TG_FS_MIN
    except Exception:
        return False


def _slot_key(d, prob_fn=None):
    """APEX-BESTLINE-2026-10-25: sort key (ascending = best first) for which prop wins a
    match's/player's slot. Rank score first when BEST_RANK_PER_MATCH, else the old order. Never raises."""
    if not BEST_RANK_PER_MATCH:
        return (-prob_fn(d),) if prob_fn else _bb_sortkey(d)
    try:
        rs = board_rank_score(d.get("p") or {}, (prob_fn or _board_prob)(d))
    except Exception:
        rs = _board_prob(d)
    return (-rs,) + _bb_sortkey(d)


def _low_cushion_split(ds, orig_rank):
    """APEX-BLOWOUT-2026-10-22: (kept, held_back rows) for one board's scored list. Never raises."""
    try:
        if not LOW_CUSHION_BACKUPS and not LINE175_SKIP_TO_BACKUPS and not FANTASY_LESS_TO_BACKUPS \
                and not DF_LESS_PLAYED_YESTERDAY_HOLD and not NO_BOOK_TOTAL_TO_BACKUPS:
            return ds, []
        import apex_blowout_signal as _bs

        def _why(d):
            if not _board_qualifies(d):   # sub-bar props fall to the floors below, as before
                return None
            return (_bs.low_cushion_reason(d.get("p") or {}) if LOW_CUSHION_BACKUPS else None) \
                or board_hold_reason(d.get("p") or {})   # APEX-175BACKUPS / APEX-DFFANTASY-2026-10-22
        keep, held, moved_up = [], [], []
        for d in ds:
            why = _why(d)
            if why:
                held.append({"rank": orig_rank.get(id(d)), "score": d["sc"], "p": d["p"], "conf": d.get("conf"), "reason": why})
                # APEX-DFFANTASY-2026-10-22: the held pick had won its player's/match's slot; the
                # best qualifying prop it beat for that slot takes the slot instead of the slot
                # going empty (10/22: Siniakova TG 17.5 held hid her match's TG 18.5).
                alt = [m for m in sorted(d.get("more") or [], key=_slot_key)   # APEX-BESTLINE-2026-10-25
                       if _board_qualifies(m) and not _why(m)] if HOLD_SLOT_MOVES_UP else []
                if alt:
                    a = dict(alt[0])
                    a["more"] = [m for m in (d.get("more") or []) if m is not alt[0]]
                    moved_up.append(a)
            else:
                keep.append(d)
        for a in moved_up:
            ka = _slot_key(a)
            i = next((j for j, x in enumerate(keep) if _slot_key(x) > ka), len(keep))
            keep.insert(i, a)
        return keep, held
    except Exception as _fe:
        _failnote("_low_cushion_split", _fe)
        return ds, []


def _type_board_cushion_hold(p):
    """APEX-STANDARDHOLD-2026-10-23: the low-cushion reason Best Standard / Best Demon hold a
    pick for (their own rules, so _low_cushion_split's 70%/67 bar does not apply), or None."""
    try:
        _nb = no_book_total_reason(p)   # APEX-NOBOOK-2026-10-24: same hold on Best Standard / Best Demon
        if _nb or not LOW_CUSHION_BACKUPS:
            return _nb
        import apex_blowout_signal as _bs
        return _bs.low_cushion_reason(p)
    except Exception as _fe:
        _failnote("_type_board_cushion_hold", _fe)
        return None


def _cushion_held_lines(held):
    """APEX-STANDARDHOLD-2026-10-23: the "⚠ held" section on Best Standard / Best Demon --
    held picks stay listed with the reason, never hidden. [] when nothing is held."""
    rows = []
    for h in held or []:
        p = h.get("p") or {}
        if _is_unplayable(p) or _pub_match_started(p) or _pub_is_graded(p):
            continue
        who = p.get("opponent") if p.get("player") == "match" else p.get("player")
        rows.append("⚠ %s%s %s %s %s (%s) · Win %.1f%% -- %s" % (
            no_book_mark(p), who, LABEL.get(p.get("category"), p.get("category")), p.get("line"),
            str(p.get("direction") or "").upper(), str(p.get("prop_type") or "standard").title(),
            100 * _board_prob(p), h.get("reason") or "held back"))
    if not rows:
        return []
    return ["─" * 30, "⚠ **HELD** -- under 3 games of bet365 room on Total Games MORE, or %s no book total, "
            "not posted here (%d):" % (NO_BOOK_FLAG, len(rows))] + rows


BOARD_BACKUP_FULL_CARDS = 6   # APEX-BACKUPCARDS-2026-10-21: top backups as full cards; 0 = one-liners only
# APEX-BACKUPSUB67-2026-10-22 (owner, "Go" on 10/22): BACKUPS also list 70%+ Win
# Probability props with Apex 60-66 (never below 60), each marked "⚠ Apex 63, under the 67
# bar", listed after the props that pass Apex 67. Backups never take numbered slots.
# Undo: None.
BACKUP_SUB67_MIN_SCORE = 60
_BACKUP_BOARD_TYPES = {"bestgoblin": "goblin", "beststandard": "standard", "bestdemon": "demon"}


# APEX-BACKUPORDER-2026-10-24 (owner, "Put the backup order in the same order the board
# uses"): BACKUPS are numbered by the same rank score the board's numbered spots use
# (board_rank_of: Win Prob + 5 points per net signal point, minus the 17.5 push-down; Best
# Standard by its market rank), not plain Win Prob, so BACKUP 1 is the next pick in line (10/24:
# Fritz, rank 86, sat at the bottom of Top 3 BACKUPS under 0-signal Fantasy picks). Picks held
# for a rule or under the 67 bar are listed after the numbered backups, unnumbered, with their
# reason. Each backup shows its rank and the gap to the board's last spot, and a short "Backups
# in order" line sits above the first pick card. Display only: spots, floors and picks unchanged.
# Undo: BACKUP_BOARD_ORDER = False (plain Win Prob order, every backup numbered, as before).
BACKUP_BOARD_ORDER = True
BACKUP_SUMMARY_COUNT = 6
_BACKUP_CAT_SHORT = {"total_games": "TG", "fantasy_score": "Fantasy", "dfs": "DF", "double_faults": "DF",
                     "games_won": "Games Won", "aces": "Aces", "breaks": "Breaks"}


def board_rank_of(flag, p):
    """APEX-BACKUPORDER-2026-10-24: the rank score `flag`'s board orders its spots by (the
    _rerank_after_top3 call in the board capture). Never raises."""
    try:
        return board_rank_score(p, _standard_rank_prob(p) if flag == "beststandard" else _board_prob(p))
    except Exception:
        return _board_prob(p)


# APEX-SCOREDISPLAY-2026-10-25 (owner, go: "Currently I only see picks as what they are, not
# with the points added for signals. We need to add that near the scores of %."): every place a pick
# shows its Win Prob also shows its board Score -- the exact number board_rank_of() ranks it by (Win
# Prob + 5 per net signal point, blowout double, 17.5 push-down; Best Standard its market-adjusted
# rank) -- as '🏆 Score 100 (+20 signals)'. Whole numbers, one decimal when two picks on the same list
# are within 1 point. Display only. Undo: SCORE_DISPLAY = False.
SCORE_DISPLAY = True
SCORE_EXPLAIN = ("\U0001F3C6 Ranking # = Win Prob + 5 for each good signal, −5 for each bad "
                 "(blowout −10). Not a win chance — board is sorted by it.")
_SCORE_CAT_WORD = {"total_games": "TG", "fantasy_score": "Fantasy", "dfs": "DF", "double_faults": "DF",
                   "games_won": "Games Won", "aces": "Aces", "breaks": "Breaks"}


def _score_round(x, dec=0):
    """Half-up rounding for display (86.5 -> 87)."""
    m = 10 ** dec
    return _math.floor(x * m + 0.5) / m


def _score_fmt(x, dec=0):
    return ("%.1f" % _score_round(x, 1)) if dec else ("%d" % _score_round(x, 0))


def _pts(n):
    """'+20' / '−10' / '0' (a real minus sign)."""
    n = int(round(n))
    return ("+%d" % n) if n > 0 else (("−%d" % -n) if n < 0 else "0")


def score_value(flag, p):
    """APEX-SCOREDISPLAY-2026-10-25: the board Score on a 0-100 scale (100 x board_rank_of). Never raises."""
    try:
        return 100.0 * board_rank_of(flag, p)
    except Exception:
        return 100.0 * _board_prob(p)


def score_why(flag, p):
    """'+20 signals' / '+5: 3 good, 🏟 blowout −10' / 'no signals on Fantasy' (+ 17.5 / market notes). Never raises."""
    try:
        import apex_card_signals as _csig
        good, bad, net, avail = _csig.net_score(p)
        pts = 100 * NET_SIGNAL_POINTS
        cat = p.get("category")
        bits = []
        if _fantasy_no_points(p):   # APEX-FANTASYPOINTS-2026-11-01
            return "Fantasy signals shown, no rank points"
        if flag == "beststandard" and BEST_STANDARD_MARKET_RANK:
            bits.append("market-adjusted %d%%" % _score_round(100 * _standard_rank_prob(p)))
        if avail < 2 and not good and not bad:
            w = _SCORE_CAT_WORD.get(cat, LABEL.get(cat, str(cat or "")))
            if cat == "total_games":
                w += " " + str(p.get("direction") or "").upper()
            bits.append("no signals on %s" % w)
        elif _csig.blowout_doubled(p) and bad >= 2:
            sub = ["%d good" % good] + (["%d bad" % (bad - 2)] if bad > 2 else [])
            bits.append("%s: %s, \U0001F3DF blowout %s" % (_pts(net * pts), ", ".join(sub), _pts(-2 * pts)))
        elif net:
            bits.append("%s signals" % _pts(net * pts))
        else:
            bits.append("signals even, %d good %d bad" % (good, bad) if (good or bad) else "no signals")
        pen, why = line175_penalty(p)
        if pen:
            bits.append("17.5 check %s%s" % (_pts(-100 * pen), " skip" if why == "skip" else " not enough data"))
        return ", ".join(bits)
    except Exception:
        return ""


def score_text(flag, p, dec=0, why=True):
    """APEX-SCOREDISPLAY-2026-10-25: '🏆 Score 100 (+20 signals)', or '' when SCORE_DISPLAY is off."""
    if not SCORE_DISPLAY:
        return ""
    w = score_why(flag, p) if why else ""
    try:   # APEX-TWOPULLS-2026-11-01
        import apex_two_pulls as _tp
        _warn = (" · " + _tp.WARNING) if _tp.missing_once(p or {}) else ""
    except Exception:
        _warn = ""
    return "\U0001F3C6 Ranking # %s%s" % (_score_fmt(score_value(flag, p), dec), (" (%s)" % w) if w else "") + _warn
    return "\U0001F3C6 Ranking # %s%s" % (_score_fmt(score_value(flag, p), dec), (" (%s)" % w) if w else "")


def score_decimals(values):
    """One decimal (1) for a pick whose Score is within 1 point of another on the same list, else 0."""
    v = [_score_round(x, 1) for x in values]
    return [1 if any(j != i and abs(v[i] - v[j]) < 1 for j in range(len(v))) else 0 for i in range(len(v))]


def backup_is_held(why, w67):
    """APEX-BACKUPORDER-2026-10-24: a backup held for a rule or under the 67 bar (listed last, unnumbered)."""
    return bool(BACKUP_BOARD_ORDER) and (bool(w67) or str(why or "").startswith("held back"))


def backup_sort_key(flag, pr, p, why, w67):
    if not BACKUP_BOARD_ORDER:
        return (bool(w67), -pr)   # APEX-BACKUPSUB67-2026-10-22 order
    return (backup_is_held(why, w67), -board_rank_of(flag, p), -pr)


def backup_rank_text(rk, last_rk, n_spots, flag=None, p=None):
    """'rank 85, 1 point behind #3' (APEX-BACKUPORDER-2026-10-24). With the prop (APEX-SCOREDISPLAY-
    2026-10-25): '🏆 Score 85 (+20 signals), 1 point behind #3'."""
    r = int(round(100 * rk))
    head = ("rank %d" % r) if (p is None or not SCORE_DISPLAY) else score_text(flag, p)
    if last_rk is None or not n_spots:
        return head
    g = int(round(100 * last_rk)) - r
    if g == 0:
        return "%s, tied with #%d" % (head, n_spots)
    return "%s, %d point%s %s #%d" % (head, abs(g), "" if abs(g) == 1 else "s", "behind" if g > 0 else "above", n_spots)


def backup_short_label(p):
    """'Siniakova v Krueger TG 18.5' / 'Kovacevic Fantasy 5.5' / '... LESS' for the summary line."""
    if p.get("player") == "match":
        who = " v ".join((str(x).split() or [""])[-1] for x in str(p.get("opponent") or "").replace(" vs ", " v ").split(" v "))
    else:
        who = (str(p.get("player") or "").split() or [""])[-1]
    cat = _BACKUP_CAT_SHORT.get(p.get("category"), LABEL.get(p.get("category"), str(p.get("category") or "")))
    try:
        ln = "%g" % float(p.get("line"))
    except (TypeError, ValueError):
        ln = str(p.get("line"))
    return "%s %s %s%s" % (who, cat, ln, " LESS" if str(p.get("direction") or "").upper() == "LESS" else "")


def backup_summary_line(flag, rows):
    """APEX-BACKUPORDER-2026-10-24: '🔁 Backups in order: 1) X (85) · 2) Y (83) ...' (numbered
    backups only, up to BACKUP_SUMMARY_COUNT), or None."""
    try:
        if not BACKUP_BOARD_ORDER or not BACKUP_SUMMARY_COUNT:
            return None
        num = [r for r in rows if not backup_is_held(r[3], r[4])][:BACKUP_SUMMARY_COUNT]
        if not num:
            return None
        return "\U0001F501 Backups in order: " + " \u00B7 ".join(
            "%d) %s%s (%s%s)" % (i, no_book_mark(r[2]), backup_short_label(r[2]), "Ranking # " if SCORE_DISPLAY else "",
                                 _score_fmt(score_value(flag, r[2])))
            for i, r in enumerate(num, 1))
    except Exception:
        return None


def _prop_names(p):
    if p.get("player") == "match":
        raw = str(p.get("opponent") or "").replace(" vs ", " v ").split(" v ")
    else:
        raw = [p.get("player"), p.get("opponent")]
    return {str(x).strip().casefold() for x in raw if x}


def _board_backup_rows(flag, shown, posted, rec, exclude=()):
    """The BACKUPS rows for one board, in BACKUPS order: (win prob, one-line text, prop,
    why, under-67 mark). `exclude` = prop identities to leave out (APEX-PULLEDFILL-2026-10-22:
    lines PrizePicks pulled after the fetch). Raises on failure; callers catch."""
    picks, _ = load_current_board()
    _fs_sc = {}
    if FS_BACKUPS and flag in _FIRST_SET_BOARDS:   # APEX-FIXCHECKER-2026-11-02: first-set picks compete for BACKUPS too
        try:
            for _x in (LAST_RANK_POOLS.get(flag, (None, []))[1] or []):
                _xp = _x.get("p") or {}
                if _xp.get("category") == "1st_set_total_games":
                    _fs_sc[_apex_prop_identity(_xp)] = (_xp, _x.get("sc"))
            picks = list(picks) + [v[0] for v in _fs_sc.values()]
        except Exception:
            _fs_sc = {}
    want = _BACKUP_BOARD_TYPES.get(flag)
    on_board = {_apex_prop_identity(e.get("p") or {}) for e in shown} | set(exclude or ())
    posted_names = [(i, _prop_names(e.get("p") or {})) for i, e in enumerate(posted, 1)]
    rows, seen = [], set()
    for p in picks:
        if want and str(p.get("prop_type") or "standard").lower() != want:
            continue
        k = _apex_prop_identity(p)
        if k in seen or k in on_board:
            continue
        seen.add(k)
        if _is_unplayable(p) or _pub_match_started(p):
            continue
        if k in _fs_sc:   # APEX-FIXCHECKER-2026-11-02: scored by the first-set rule, not the engine gates
            st, _why, sc = "eligible", "first-set rule", _fs_sc[k][1]
        else:
            st, _why, sc = _pre_board_reason(p, rec)
        sub67 = (st != "eligible" and _why == "Apex Score under 67" and BACKUP_SUB67_MIN_SCORE is not None
                 and sc is not None and sc >= BACKUP_SUB67_MIN_SCORE)   # APEX-BACKUPSUB67-2026-10-22
        if st != "eligible" and not sub67:
            continue
        names = _prop_names(p)
        hit = next((i for i, nm in posted_names if nm & names), None)
        why = ("same match/player as #%d" % hit) if hit else "board full"
        if flag == "beststandard" and not hit and not _passes_standard_rule(p):
            why = "outside the Best Standard rule"
        elif flag == "beststandard" and _df_less_apex_short(p, sc):   # APEX-DFFANTASY-2026-10-22
            why = "held back: Double Faults LESS on Best Standard needs Apex %d" % STANDARD_DF_LESS_APEX_MIN
        elif flag == "bestgoblin" and not hit and not _best_eligible(p, "goblin"):
            # APEX-BACKUPORDER-2026-10-24: Best Goblin only takes Goblins at the engine's Goblin
            # break-even (72.1%, openclaw_core _GOBLIN_BREAKEVEN_ENGINE); say so, not "board full"
            why = "held back: under Best Goblin's 72.1%% Goblin break-even floor (%.1f%%)" % (100 * _board_prob(p))
        try:   # APEX-BLOWOUT-2026-10-22: held back for blowout risk, say so
            import apex_blowout_signal as _bs
            _lc = _bs.low_cushion_reason(p) if LOW_CUSHION_BACKUPS else None
            if _lc:
                why = "held back: " + _lc
        except Exception:
            pass
        _s175 = board_hold_reason(p) or cushion_block_reason(flag, p)   # APEX-175BACKUPS / APEX-DFFANTASY-2026-10-22 / APEX-CUSHIONFILL
        if _s175:
            why = "held back: " + _s175
        pr = _board_prob(p)
        hp = corrected_prob(pr)
        who = p.get("opponent") if p.get("player") == "match" else p.get("player")
        _w67 = ("\u26A0 Apex %d, under the %d bar" % (int(sc), BOARD_MIN_SCORE)) if sub67 else ""
        rows.append((pr, "\U0001F501 %s%s %s %s %s (%s) · Win %.1f%%%s · Apex %d -- %s%s" % (
            no_book_mark(p), who, LABEL.get(p.get("category"), p.get("category")), p.get("line"),
            str(p.get("direction") or "").upper(), str(p.get("prop_type") or "standard").title(),
            100 * pr, (" · Honest %.1f%%" % (100 * hp)) if hp is not None else "",
            int(sc or 0), why, (" · " + _w67) if _w67 else ""), p, why, _w67))
    # APEX-BACKUPORDER-2026-10-24: the board's own rank order, held / under-67 last
    rows.sort(key=lambda r: backup_sort_key(flag, r[0], r[2], r[3], r[4]))
    return rows


def _board_backup_lines(flag, shown, posted, rec, exclude=(), rows=None):
    try:
        if not BOARD_BACKUPS_LIMIT:
            return []
        rows = _board_backup_rows(flag, shown, posted, rec, exclude) if rows is None else rows
        if not rows:
            return []
        _n67 = sum(1 for r in rows if r[4])
        out = ["\u2500" * 30, "\U0001F501 **BACKUPS** -- passed 70%% and Apex %d but not posted here (%d)%s%s"
               % (BOARD_MIN_SCORE, len(rows) - _n67,
                  (" + %d at 70%%+ with Apex %d-%d, marked \u26A0" % (_n67, BACKUP_SUB67_MIN_SCORE, BOARD_MIN_SCORE - 1)) if _n67 else "",
                  " -- numbered in the order this board ranks them; held picks listed last" if BACKUP_BOARD_ORDER else "")]
        # APEX-BACKUPORDER-2026-10-24: rank + gap to the board's last spot on every backup
        _last = board_rank_of(flag, (posted[-1] or {}).get("p") or {}) if posted else None
        _nos, _n = [], 0
        for r in rows:
            if backup_is_held(r[3], r[4]):
                _nos.append(None)
            else:
                _n += 1
                _nos.append(_n)

        def _tag(i, p, held):
            rk = board_rank_of(flag, p)
            return ("HELD", score_text(flag, p) if SCORE_DISPLAY else "rank %d" % round(100 * rk)) if held else \
                ("BACKUP %d" % i, backup_rank_text(rk, _last, len(posted), flag, p) if BACKUP_BOARD_ORDER else "")
        # APEX-BACKUPCARDS-2026-10-21 (owner, "Go"): the top backups render as the same
        # full card every ranked pick uses (signals, Honest, category, freshness); the rest stay
        # one line each. Display only.
        full = rows[:BOARD_BACKUP_FULL_CARDS]
        if full:
            try:
                _fs_first, _fs_last, _ = _load_freshness_index()
            except Exception:
                _fs_first, _fs_last = {}, {}
            for _k, (_pr, _line, _p, _why, _w67) in enumerate(full):
                _i = _nos[_k] if BACKUP_BOARD_ORDER else _k + 1
                _lab, _rt = _tag(_i, _p, _i is None)
                try:
                    _sc, _ev, _lvl, _up, _dn = apex_score(_p.get("prob_cal", _p.get("prob")), _p["category"], bool(_p.get("bo5")), rec)
                    _cf, _ct, _cw = apex_confidence(_p["category"], bool(_p.get("bo5")), rec, score=_sc, prob=_p.get("prob"),
                                                    evidence=_ev, attention=_lvl, warnings=_dn, card_present=False)
                    _d = {"p": _p, "sc": _sc, "score": _sc, "ev": _ev, "lvl": _lvl, "up": _up, "down": _dn,
                          "conf": _cf, "cwhy": _cw, "rank": rank_score(_sc, _cf)}
                    _card = _render_pick_card(_d, _i or 0, rec, _fs_first, _fs_last, {}, worst=False, show_debug=False,
                                              flag=flag)   # APEX-SCOREDISPLAY-2026-10-25
                    _who = _p.get("opponent") if _p.get("player") == "match" else _p.get("player")
                    _vs = "" if _p.get("player") == "match" else (" vs %s" % _p["opponent"] if _p.get("opponent") else "")
                    if len(_card) > 1:
                        _card[1] = "\U0001F501 %s · %s%s%s%s · _%s_" % (_lab, no_book_mark(_p), _who, _vs,   # APEX-NOBOOK-2026-10-24
                                                                        (" · " + _rt) if _rt else "", _why)
                        if _w67:   # APEX-BACKUPSUB67-2026-10-22
                            _card.insert(2, _w67)
                    out += _card
                except Exception:
                    out.append(_line)
            if len(rows) > len(full):
                out.append("\u2500" * 30)
        for _k in range(len(full), min(len(rows), BOARD_BACKUPS_LIMIT)):
            _line = rows[_k][1]
            if BACKUP_BOARD_ORDER:   # '🔁 BACKUP 7 · <prop> · ... · Apex 71 · rank 71, 15 points behind #3 -- board full'
                _lab, _rt = _tag(_nos[_k], rows[_k][2], _nos[_k] is None)
                _head, _sep, _tail = _line[len("\U0001F501 "):].partition(" -- ")
                _line = "\U0001F501 %s · %s · %s%s%s" % (_lab, _head, _rt, _sep, _tail)
            out.append(_line)
        if len(rows) > BOARD_BACKUPS_LIMIT:
            out.append("(+%d more backups -- see /allprops)" % (len(rows) - BOARD_BACKUPS_LIMIT))
        return out
    except Exception:
        return []


# APEX-PULLEDFILL-2026-10-22 (owner, "Go fill"): when PrizePicks pulls a numbered
# pick's line between the fetch and the board post (10/22 3:37 PM: Boulter v Ruzic TG 17.5,
# Top 3 posted 2 picks, Elite 6 posted 5), the spot is filled from that board's BACKUPS in
# order -- only a backup that is not held for a rule (17.5 skip, Fantasy LESS, DF played
# yesterday, under the 67 bar, low cushion, outside the Best Standard rule), that keeps one
# per player/match on the board, and whose line PrizePicks still offers. Later picks shift
# up. The filled card and the board header say so, #alerts gets one line per pulled line
# per day, and every pulled numbered pick is logged to PULLED_LINES_LOG for grading.
# Undo: PULLED_LINE_FILL = False (spot stays empty, as before).
PULLED_LINE_FILL = True
PULLED_LINE_ALERTS = True
PULLED_LINES_LOG = os.path.join(HERE, "data", "logs", "pulled_lines.csv")
PULLED_LINES_STATE = os.path.join(HERE, "data", "logs", "pulled_lines_state.json")
# APEX-PULLEDNEWLINE-2026-10-22 (owner, "Go new line but tell me it's the new line"):
# when PrizePicks moves a numbered pick's line after the fetch and still offers the same
# player/match, market and side at a new line, the pick keeps its spot AT THE NEW LINE --
# re-scored there (newest engine row at that line: Win Prob, Apex, signals, bet365 room,
# 17.5 check) and only if it still passes that board's own bars (70% / Apex 67, the 17.5 /
# low cushion / Fantasy LESS / DF holds, Best Standard / Best Demon rules, one per
# match/player). Nearest offered line to the old one. Otherwise the backup fill above runs,
# and the header says why the new line did not qualify. The kept card says "NEW LINE" and
# names the backup he could swap in. 10/22 study: same player at the new line 34-16 (68%)
# vs the backup that filled 25-15 (62%).
# Undo: PULLED_LINE_PREFER_NEW_LINE = False (backup fill only, as in 8bffc28).
PULLED_LINE_PREFER_NEW_LINE = True
_PULLED_FIELDS = ["logged_at", "match_date", "match", "player", "market", "line", "side", "prop_type",
                  "board", "rank_held", "win_prob", "apex", "signals", "now_offered",
                  "replacement", "replacement_win_prob", "replacement_apex",
                  "outcome", "new_line", "new_line_win_prob", "new_line_apex", "new_line_why", "swap_option",
                  "replacement_key"]   # player|opponent|category|line|side of the backup that filled, for grading
_NEWLINE_ROWS = {"mtime": None, "rows": {}}


def _newline_rows():
    """Newest pick-log row per (identity, prop_type), cached on the log's mtime."""
    try:
        m = os.path.getmtime(PICK_LOG)
    except Exception:
        return {}
    if _NEWLINE_ROWS["mtime"] != m:
        best = {}
        for r in _read(PICK_LOG):
            t = str(r.get("prop_type") or "standard").strip().lower()
            k = _apex_prop_identity(r) + ("demon" if t == "red_demon" else t,)
            if k not in best or (r.get("ts") or "") > (best[k].get("ts") or ""):
                best[k] = r
        _NEWLINE_ROWS.update(mtime=m, rows=best)
    return _NEWLINE_ROWS["rows"]


def _short_who(p):
    who = str(p.get("opponent") or "").split(" v ")[0] if p.get("player") == "match" else str(p.get("player") or "")
    return (who.split() or [""])[-1]


def _newline_bar_why(flag, p2, rec):
    """(why it fails this board's bars or None, apex score) for a prop at its new line."""
    pr = _board_prob(p2)
    if flag == "bestdemon":
        if str(p2.get("direction") or "").upper() != "MORE":
            return "Best Demon is MORE only", None
        if pr < DEMON_MIN_PROB - 1e-9:
            return "only %d%%, under Best Demon's %d%%" % (round(100 * pr), round(100 * DEMON_MIN_PROB)), None
        sc = apex_score(p2.get("prob_cal", p2.get("prob")), p2["category"], bool(p2.get("bo5")), rec)[0]
        return None, sc
    st, why, sc = _pre_board_reason(p2, rec)
    if st != "eligible":
        if why in ("Win Probability under 70%", "Apex Score under 67", "below category minimum probability"):
            try:
                sc = sc if sc is not None else apex_score(pr, p2["category"], bool(p2.get("bo5")), rec)[0]
                return "only %d%% Apex %d, under the bar" % (round(100 * pr), int(sc)), sc
            except Exception:
                return "only %d%%, under the bar" % round(100 * pr), sc
        return why, sc
    try:
        import apex_blowout_signal as _bs
        _lc = _bs.low_cushion_reason(p2) if LOW_CUSHION_BACKUPS else None
        if _lc:
            return _lc, sc
    except Exception:
        pass
    _h = board_hold_reason(p2) or cushion_block_reason(flag, p2)   # APEX-CUSHIONFILL-2026-11-01
    if _h:
        return _h, sc
    if flag in ("bestgoblin", "beststandard", "bestdemon") and _category_gate_blocked(p2):
        return "category held off the Best boards", sc
    if flag == "beststandard":
        if not _passes_standard_rule(p2):
            return "outside the Best Standard rule", sc
        if _df_less_apex_short(p2, sc) or _df_less_short(p2, pr):
            return "Double Faults LESS on Best Standard needs 70%% and Apex %d" % STANDARD_DF_LESS_APEX_MIN, sc
    return None, sc


def _pulled_new_line(flag, e, now_lines, rec, live, others):
    """APEX-PULLEDNEWLINE-2026-10-22: {"line", "entry" (scored dict or None), "why", ...} for a
    pulled numbered pick that PrizePicks now offers at another line; None when it offers none.
    Never raises."""
    try:
        p = e.get("p") or {}
        if not PULLED_LINE_PREFER_NEW_LINE or not now_lines:
            return None
        old = float(p.get("line"))
        more = str(p.get("direction") or "").upper() == "MORE"
        nl = sorted(now_lines, key=lambda x: (abs(x - old), x if more else -x))[0]
        out = {"line": nl, "entry": None, "why": None, "prob": None, "sc": None, "old_prob": _board_prob(p),
               "old_line": p.get("line")}
        t = str(p.get("prop_type") or "standard").strip().lower()
        k = (_apex_prop_identity(p)[:3] + ("%g" % nl,), _apex_prop_identity(p)[:3] + ("%.1f" % nl,))
        rows = _newline_rows()
        r = next((rows[x + ("demon" if t == "red_demon" else t,)] for x in k
                  if x + ("demon" if t == "red_demon" else t,) in rows), None)
        if r is None or (r.get("match_date") or "") != (p.get("match_date") or ""):
            out["why"] = "new line %g not priced yet" % nl
            return out
        if str(r.get("direction") or "").upper() != str(p.get("direction") or "").upper():
            out["why"] = "at %g Apex now picks %s" % (nl, str(r.get("direction") or "").upper())
            return out
        if _is_unplayable(r):
            out["why"] = "new line %g is not playable on our side" % nl
            return out
        try:
            prob = float(r.get("prob") or 0)
        except (TypeError, ValueError):
            prob = 0.0
        try:
            cal = float(r.get("prob_cal") or 0)
            cal = cal if 0.0 < cal <= prob + 1e-9 else prob
        except (TypeError, ValueError):
            cal = prob
        p2 = {k2: v for k2, v in p.items() if k2 not in ("apex_price_note", "apex_book_note")}
        p2.update({"line": r.get("line"), "prob": prob, "prob_cal": cal, "edge": r.get("edge") or "",
                   "ts": r.get("ts"), "best_goblin_eligible": r.get("best_goblin_eligible"),
                   "best_standard_eligible": r.get("best_standard_eligible"),
                   "slip_start_utc": r.get("slip_start_utc") or p.get("slip_start_utc")})
        try:   # bet365 room at the new line (logged bet365 total, no API call)
            import apex_book_lines as _bl
            p2 = _bl.annotate([p2], _board_prob, corrected_prob, log=False, offline=True)[0]
        except Exception:
            pass
        out["prob"] = _board_prob(p2)
        if _pub_is_graded(p2):
            out["why"] = "new line %g already graded" % nl
            return out
        if _retyped(p2, live):
            out["why"] = "new line %g is a different type" % nl
            return out
        used = set()
        for o in others:
            used |= _prop_names(o.get("p") or {})
        why, sc = _newline_bar_why(flag, p2, rec)
        out["sc"] = sc
        if why is None and _prop_names(p2) & used:
            why = "same match/player already on the board"
        if why:
            out["why"] = "new line %g %s" % (nl, why) if not why.startswith("new line") else why
            return out
        sc, ev, lvl, up, dn = apex_score(p2.get("prob_cal", p2.get("prob")), p2["category"], bool(p2.get("bo5")), rec)
        cf, ct, cw = apex_confidence(p2["category"], bool(p2.get("bo5")), rec, score=sc, prob=p2.get("prob"),
                                     evidence=ev, attention=lvl, warnings=dn, card_present=False)
        out["sc"] = sc
        out["entry"] = {"p": p2, "sc": sc, "score": sc, "ev": ev, "lvl": lvl, "up": up, "down": dn,
                        "conf": cf, "cwhy": cw, "rank": rank_score(sc, cf), "pulled_new_line": out}
        return out
    except Exception as ex:
        print("[PulledNewLine] %s skipped (%s: %s)" % (flag, type(ex).__name__, ex))
        return None


def _pulled_match(p):
    if p.get("player") == "match":
        return str(p.get("opponent") or "")
    return "%s v %s" % (p.get("player"), p.get("opponent")) if p.get("opponent") else str(p.get("player") or "")


def _pulled_label(p):
    return "%s %s %s" % (_pulled_match(p), LABEL.get(p.get("category"), p.get("category")), p.get("line"))


def _pulled_fill(flag, shown, gone_idents, rec, live, need):
    """Up to `need` backups that may take a pulled pick's spot, in BACKUPS order. Never raises."""
    if not PULLED_LINE_FILL or need <= 0:
        return []
    try:
        rows = _board_backup_rows(flag, shown, shown, rec, exclude=gone_idents)
        used = set()
        for e in shown:
            used |= _prop_names(e.get("p") or {})
        out = []
        for _bk, (pr, _line, p, why, w67) in enumerate(rows, 1):
            if w67 or not str(why).startswith("board full"):   # held for a rule, under 67, same match, outside the rule
                continue
            if _prop_names(p) & used:
                continue
            if flag == "bestdemon" and (str(p.get("direction") or "").upper() != "MORE"
                                         or p.get("category") in DEMON_EXCLUDED):
                continue
            if _category_gate_blocked(p) or _pub_is_graded(p):
                continue
            if live is None or _retyped(p, live) or _line_gone(p, live) is not None:
                continue   # must be confirmed still offered right now
            sc, ev, lvl, up, dn = apex_score(p.get("prob_cal", p.get("prob")), p["category"], bool(p.get("bo5")), rec)
            cf, ct, cw = apex_confidence(p["category"], bool(p.get("bo5")), rec, score=sc, prob=p.get("prob"),
                                         evidence=ev, attention=lvl, warnings=dn, card_present=False)
            out.append({"p": p, "sc": sc, "score": sc, "ev": ev, "lvl": lvl, "up": up, "down": dn,
                        "conf": cf, "cwhy": cw, "rank": rank_score(sc, cf), "bk_no": _bk})
            used |= _prop_names(p)
            if len(out) >= need:
                break
        return out
    except Exception as e:
        print("[PulledFill] %s skipped (%s: %s)" % (flag, type(e).__name__, e))
        return []


def _pulled_mkt(p):
    return "%s %s" % (_pulled_match(p), LABEL.get(p.get("category"), p.get("category")))


def _swap_short(sw):
    """'Alexandrova 18.5' for the header."""
    return "%s %s" % (_short_who(sw["p"]), sw["p"].get("line")) if sw else ""


def _pulled_header_kept(p, rank, nl):
    """APEX-PULLEDNEWLINE-2026-10-22: the header notice for a pick kept at PrizePicks' new line."""
    sw = nl.get("swap")
    return ("\u26A0 PrizePicks moved after the fetch: %s %s \u2192 %g (#%d kept at the new line)%s"
            % (_pulled_mkt(p), p.get("line"), nl["line"], rank,
               (" (swap option: %s)" % _swap_short(sw)) if sw else " (no backup qualifies to swap in)"))


def _pulled_card_lines(nl):
    """APEX-PULLEDNEWLINE-2026-10-22: the NEW LINE line + the swap-in line on a kept card."""
    out = ["\U0001F501 NEW LINE: PrizePicks moved this from %s to %g after the fetch (was %d%%, now %d%% at the new "
           "line). Same player moved lines have hit about 7 of 10."
           % (nl.get("old_line"), nl["line"], round(100 * (nl.get("old_prob") or 0)), round(100 * (nl.get("prob") or 0)))]
    sw = nl.get("swap")
    if sw:
        sp = sw["p"]
        out.append("\U0001F501 If you'd rather replace it, swap in: %s %s (%d%%, Apex %d, BACKUP %s). Backups that "
                   "filled a pulled spot have hit about 6 of 10."
                   % (_pulled_label(sp), str(sp.get("direction") or "").upper(), round(100 * _board_prob(sp)),
                      int(sw.get("sc") or 0), sw.get("bk_no") or "?"))
    else:
        out.append("\U0001F501 No backup qualifies to replace it right now.")
    return out


def _pulled_csv_upgrade():
    """Rewrite PULLED_LINES_LOG under the current header when an older header is on file."""
    import csv as _csv
    try:
        with open(PULLED_LINES_LOG, newline="") as fh:
            rows = list(_csv.DictReader(fh))
            fh.seek(0)
            hdr = next(_csv.reader(fh), [])
        if hdr == _PULLED_FIELDS:
            return
        for r in rows:
            r.setdefault("outcome", "backup" if r.get("replacement") else "empty")
        with open(PULLED_LINES_LOG + ".tmp", "w", newline="") as fh:
            w = _csv.DictWriter(fh, fieldnames=_PULLED_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        os.replace(PULLED_LINES_LOG + ".tmp", PULLED_LINES_LOG)
    except FileNotFoundError:
        pass


def _pulled_status(flag, pulled, newl, before, after):
    """APEX-BOARDSTATUS-HEADER-2026-10-25: the big status for a pulled-line fill -- this board
    as posted vs with the fill. None on failure (the alert then posts as before)."""
    try:
        import apex_board_status as S
        why = {}
        for k, (rank, e, _now) in enumerate(pulled):
            nl = newl[k] if k < len(newl) else None
            why[tuple(_apex_prop_identity(e.get("p") or {}))] = (
                "line moved to %g, kept" % nl["line"] if nl and nl.get("entry") else "pulled by PrizePicks")
        sp = lambda es: [S.spot(e.get("p") or {}, prob=_board_prob(e.get("p") or {})) for e in es]
        return S.compare({flag: sp(before)}, {flag: sp(after)}, gone_reasons={flag: why})
    except Exception as ex:
        print("[PulledFill] status skipped (%s: %s)" % (type(ex).__name__, ex))
        return None


def _pulled_record(flag, pulled, fills, newl=None, board_before=None, board_after=None):
    """CSV row per pulled numbered pick (first sighting per board per day) and one #alerts
    line per pulled line per day. `pulled` = [(rank, entry, now_lines)]; `fills` / `newl`
    line up with it (backup that filled the spot / APEX-PULLEDNEWLINE result). Never raises."""
    try:
        import csv as _csv
        newl = newl or []
        try:
            with open(PULLED_LINES_STATE) as fh:
                st = json.load(fh)
        except Exception:
            st = {}
        today = datetime.now().date().isoformat()
        if st.get("date") != today:
            st = {"date": today, "logged": [], "alerted": []}
        new_rows, alerts, cards = [], [], []   # APEX-ALERTCARDS-2026-10-25: one card per pull
        for k, (rank, e, now_lines) in enumerate(pulled):
            p = e.get("p") or {}
            ident = "|".join(str(x) for x in _apex_prop_identity(p))
            nl = newl[k] if k < len(newl) else None
            kept = bool(nl and nl.get("entry"))
            rep = None if kept else (fills[k] if k < len(fills) else None)
            rp = (rep or {}).get("p") or {}
            sw = (nl or {}).get("swap") if kept else None
            # (the board record grades a kept pick at the new line: apex_engine_validation._moved_lines)
            _lk = ident + "|" + flag + ("|kept" if kept else "")   # a later keep at the new line logs again
            if _lk not in st["logged"]:
                st["logged"].append(_lk)
                new_rows.append({"logged_at": datetime.now().isoformat(timespec="seconds"),
                                 "match_date": p.get("match_date"), "match": _pulled_match(p),
                                 "player": p.get("player"), "market": p.get("category"), "line": p.get("line"),
                                 "side": p.get("direction"), "prop_type": p.get("prop_type") or "standard",
                                 "board": flag, "rank_held": rank, "win_prob": round(_board_prob(p), 4),
                                 "apex": int(e.get("sc") or e.get("score") or 0), "signals": _net_of(p),
                                 "now_offered": ";".join("%g" % x for x in now_lines or []),
                                 "replacement": _pulled_label(rp) + " " + str(rp.get("direction") or "") if rep else "",
                                 "replacement_win_prob": round(_board_prob(rp), 4) if rep else "",
                                 "replacement_apex": int(rep.get("sc") or 0) if rep else "",
                                 "outcome": "new_line_kept" if kept else ("backup" if rep else "empty"),
                                 "new_line": ("%g" % nl["line"]) if nl else "",
                                 "new_line_win_prob": round(nl["prob"], 4) if nl and nl.get("prob") is not None else "",
                                 "new_line_apex": int(nl["sc"]) if nl and nl.get("sc") is not None else "",
                                 "new_line_why": (nl or {}).get("why") or "",
                                 "swap_option": (_pulled_label(sw["p"]) + " " + str(sw["p"].get("direction") or "").upper()
                                                 + " (BACKUP %s)" % sw.get("bk_no")) if sw else "",
                                 "replacement_key": "|".join(str(rp.get(x) or "") for x in
                                                             ("player", "opponent", "category", "line", "direction"))
                                 if rep else ""})
            _ak = ident + ("|kept" if kept else "")
            if _ak not in st["alerted"] and PULLED_LINE_ALERTS and not os.environ.get("APEX_NO_ALERTS"):
                st["alerted"].append(_ak)
                cards.append((("pulled|%s|%s|%s" % (datetime.now().date().isoformat(), flag, ident)), kept, bool(rep),
                              _pulled_card(flag, rank, p, e, now_lines, kept, nl, rep, sw)))   # APEX-ALERTCHAIN-2026-10-25
                if kept:   # APEX-PULLEDNEWLINE-2026-10-22
                    alerts.append("%s on %s -- now %d%%, Apex %d at the new line"
                                  % (_pulled_header_kept(p, rank, nl), _PUBLISHED_FLAG_LABEL.get(flag, flag),
                                     round(100 * (nl.get("prob") or 0)), int(nl.get("sc") or 0)))
                    continue
                alerts.append("\u26A0 PrizePicks pulled after the fetch: %s %s (was #%d on %s) -- %s%s"
                              % (_pulled_label(p), str(p.get("direction") or "").upper(), rank,
                                 _PUBLISHED_FLAG_LABEL.get(flag, flag),
                                 ("filled by %s %s" % (_pulled_label(rp), str(rp.get("direction") or "").upper()))
                                 if rep else "no eligible backup, spot left empty",
                                 ("; now offered at %s" % ", ".join("%g" % x for x in now_lines)) if now_lines else "")
                              + ((" (%s)" % nl["why"]) if nl and nl.get("why") else ""))
        if new_rows:
            try:   # APEX-BOARDHISTORY-2026-10-25: the pulled pick and the backup that took its spot
                import apex_board_history as _bh
                _bh.log_pulled_fill(flag, pulled, fills)
            except Exception:
                pass
            os.makedirs(os.path.dirname(PULLED_LINES_LOG), exist_ok=True)
            _pulled_csv_upgrade()
            hdr = not os.path.exists(PULLED_LINES_LOG)
            with open(PULLED_LINES_LOG, "a", newline="") as fh:
                w = _csv.DictWriter(fh, fieldnames=_PULLED_FIELDS)
                if hdr:
                    w.writeheader()
                w.writerows(new_rows)
        with open(PULLED_LINES_STATE + ".tmp", "w") as fh:
            json.dump(st, fh)
        os.replace(PULLED_LINES_STATE + ".tmp", PULLED_LINES_STATE)
        if cards:   # APEX-ALERTCARDS-2026-10-25: one card per pull, the board's new lineup on the last one
            try:
                import apex_alert_cards as _C
                if _C.ALERT_CARDS_ON:
                    _swl = None
                    if board_after is not None:
                        try:   # APEX-SWAPLINES-2026-10-25: one compact lineup line
                            import apex_board_status as _S
                            if _S.SWAPS_ONLY:
                                _swl = _S.lineup_line(flag, {"now": [_S.spot(e.get("p") or {}, prob=_board_prob(e.get("p") or {}))
                                                                     for e in board_after]})
                        except Exception:
                            _swl = None
                    _last = cards[-1][3]
                    if _swl:
                        _last["description"] = (_last.get("description") or "") + "\n\n" + _swl
                    elif board_after is not None:
                        _last["fields"] = (_last.get("fields") or []) + [
                            {"name": "\U0001F4CB %s now" % _PUBLISHED_FLAG_LABEL.get(flag, flag),
                             "value": _pulled_lineup(board_after)[:1024], "inline": False}]
                    for _ck, _kept, _filled, _cd in cards:
                        if not _pulled_chain(_ck, _kept, _filled, _cd):
                            _C.post(_cd, "alerts")
                    alerts = []
            except Exception as _ex:
                print("[PulledFill] cards failed (%s), sending text" % type(_ex).__name__)
        if alerts:
            import apex_ops as _ops
            _txt = "\n".join(alerts)
            if board_before is not None and board_after is not None:
                import apex_board_status as _bst
                _txt = _bst.wrap_text(_pulled_status(flag, pulled, newl, board_before, board_after), _txt)
            _ops.send_discord("alerts", _txt)
    except Exception as ex:
        print("[PulledFill] record skipped (%s: %s)" % (type(ex).__name__, ex))


def _pulled_chain(key, kept, filled, card):
    """APEX-ALERTCHAIN-2026-10-25: a pulled-line card says what happens next and stays one card -- a
    later 'kept at the new line' edits the same card. True when the chain posted/edited it."""
    try:
        import apex_alert_chain as AC
        if not AC.CHAINS_ON:
            return False
        if kept:
            if AC.get(key, max_age_min=24 * 60):
                return AC.finish(key, card, note="line re-posted")
            return False
        at, _w = AC.next_board_check()
        if filled:
            nxt = ("Nothing to do — the backup has the spot now. If PrizePicks re-posts this line at a new "
                   "number, Apex re-scores it at the next board update (about %s) and this card updates." % AC.hm(at))
        else:
            nxt = ("The spot stays empty until a pick passes the rules — next board update about %s. If PrizePicks "
                   "re-posts this line at a new number, this card updates." % AC.hm(at))
        return AC.soft_start(key, card, nxt, check_mute=False)
    except Exception as ex:
        print("[PulledFill] chain card failed (%s), posting it" % type(ex).__name__)
        return False


def _pulled_lineup(entries):
    """'1️⃣ Kraus v Udvardy TG 17.5 (81%)' lines for a pulled-line card."""
    keys = ["1\ufe0f\u20e3", "2\ufe0f\u20e3", "3\ufe0f\u20e3", "4\ufe0f\u20e3", "5\ufe0f\u20e3",
            "6\ufe0f\u20e3", "7\ufe0f\u20e3", "8\ufe0f\u20e3", "9\ufe0f\u20e3", "\U0001F51F"]
    out = []
    for i, e in enumerate(entries or [], 1):
        q = e.get("p") or {}
        out.append("%s %s %s (%d%%)" % (keys[i - 1] if i <= len(keys) else "#%d" % i, _pulled_label(q),
                                       str(q.get("direction") or "").upper(), round(100 * _board_prob(q))))
    return "\n".join(out) or "empty"


def _pulled_card(flag, rank, p, e, now_lines, kept, nl, rep, sw):
    """APEX-ALERTCARDS-2026-10-25: the #alerts card for one pulled line -- 🚫 Pulled, then
    ↪️ Filled by / ⚠️ Spot empty / 🔁 kept at the new line."""
    import apex_alert_cards as C
    board = _PUBLISHED_FLAG_LABEL.get(flag, flag).replace("Elite6", "Elite 6").replace("Top10", "Top 10")
    try:
        import apex_board_status as _S
        if _S.SWAPS_ONLY:
            return _pulled_swap_card(board, rank, p, kept, nl, rep, sw)
    except Exception as _ex:
        print("[PulledFill] swap card skipped (%s: %s)" % (type(_ex).__name__, _ex))
    me = "%s %s" % (_pulled_label(p), str(p.get("direction") or "").upper())
    pulled = "%s — was #%d (%d%%)" % (me, rank, round(100 * _board_prob(p)))
    offered = ("PrizePicks now offers it at %s" % ", ".join("%g" % x for x in now_lines)) if now_lines else ""
    if kept:
        f = [("\U0001F6AB Old line", pulled),
             ("\U0001F501 Kept at the new line", "%s %s %g — now %d%%, Apex %d, still #%d" % (
                 _pulled_mkt(p), str(p.get("direction") or "").upper(), nl["line"], round(100 * (nl.get("prob") or 0)),
                 int(nl.get("sc") or 0), rank)),
             ("\u21AA\ufe0f Swap option", ("%s %s (%d%%, Backup %s)" % (_pulled_label(sw["p"]), str(sw["p"].get("direction") or "").upper(),
                                                                     round(100 * _board_prob(sw["p"])), sw.get("bk_no") or "?"))
              if sw else "no backup qualifies to swap in")]
        return C.make("\U0001F501 LINE MOVED · %s #%d" % (board, rank), C.ORANGE, "PrizePicks moved this line after the board posted.", f)
    if rep:
        rp = rep.get("p") or {}
        f = [("\U0001F6AB Pulled", pulled + (("\n" + offered) if offered else "")),
             ("\u21AA\ufe0f Filled by", "%s %s (%d%%) — moved up from Backup %s into #%d" % (
                 _pulled_label(rp), str(rp.get("direction") or "").upper(), round(100 * _board_prob(rp)),
                 rep.get("bk_no") or "?", rank))]
        if nl and nl.get("why"):
            f.append(("\u2139\ufe0f New line", nl["why"]))
        return C.make("\U0001F6AB PULLED · %s #%d · filled" % (board, rank), C.ORANGE,
                      "PrizePicks pulled a pick after the board posted.", f)
    f = [("\U0001F6AB Pulled", pulled + (("\n" + offered) if offered else "")),
         ("\u26A0\ufe0f Spot empty", "#%d is empty — no backup passes the board rules" % rank)]
    if nl and nl.get("why"):
        f.append(("\u2139\ufe0f New line", nl["why"]))
    return C.make("\u26A0\ufe0f PULLED · %s #%d · SPOT EMPTY" % (board, rank), C.RED,
                  "PrizePicks pulled a pick after the board posted.", f)


def _pulled_swap_card(board, rank, p, kept, nl, rep, sw):
    """APEX-SWAPLINES-2026-10-25 (owner: green pick, the one it replaced, the line): two lines
    -- '🚫 PrizePicks pulled: 🔴 <pick> (Top 10 #8)' then '🟢 <backup> · 81% 🔄 took the spot'
    or '⚠️ spot EMPTY'; a kept line move is one '🔁 <pick> · TG MORE 19.5 → 20.5' line."""
    import apex_alert_cards as C
    import apex_board_status as S
    me = S.spot(p, prob=_board_prob(p))
    who = me.get("who") or me.get("short")
    where = "%s #%d" % (board, rank)
    if kept:
        lines = ["\U0001F501 %s · %s → %g · %d%% (%s, kept)" % (who, me.get("what"), nl["line"],
                                                               round(100 * (nl.get("prob") or 0)), where)]
        if sw:
            o = S.spot(sw["p"], prob=_board_prob(sw["p"]))
            lines.append("\u21AA\ufe0f Swap option: \U0001F7E2 %s · %s · %.0f%% (Backup %s)" % (
                o.get("who") or o.get("short"), o.get("what"), 100 * (o.get("prob") or 0), sw.get("bk_no") or "?"))
        return C.make("\U0001F501 LINE MOVED · %s" % where, C.ORANGE, "\n".join(lines))
    lines = ["\U0001F6AB PrizePicks pulled: \U0001F534 %s · %s (%s)" % (who, me.get("what"), where)]
    if rep:
        o = S.spot(rep.get("p") or {}, prob=_board_prob(rep.get("p") or {}))
        lines.append("\U0001F7E2 %s · %s · %.0f%% \U0001F504 took the spot" % (
            o.get("who") or o.get("short"), o.get("what"), 100 * (o.get("prob") or 0)))
        return C.make("\U0001F6AB PRIZEPICKS PULLED · %s" % where, C.ORANGE, "\n".join(lines))
    lines.append("\u26A0\ufe0f spot EMPTY")
    return C.make("\u26A0\ufe0f PRIZEPICKS PULLED · %s · SPOT EMPTY" % where, C.RED, "\n".join(lines))


def published_board_detail(flag, rec=None, return_scored=False, record=True):
    """APEX-PUBLISHEDBOARD-2026-10-07 (owner, authorized): /elite6,
    /top3, /top10, /bestgoblin, /bestdemon, /beststandard now call this
    instead of rebuilding from load_current_board() on every invocation.
    Reads the snapshot captured at the last /fetch publish and removes a
    prop ONLY for one of the three reasons specified: its match started,
    it was graded, or (left to the NEXT snapshot, not this read) it is no
    longer offered -- never merely because time passed since publish or a
    later fetch's retirement window moved past it without re-confirming
    it. A fresh snapshot is written only by /fetch itself -- the explicit
    live-refresh path; this function never rebuilds the board."""
    try:
        if not os.path.exists(PUBLISHED_BOARD_SNAPSHOT):
            return ("No published board snapshot yet -- run /fetch once to publish today's "
                     "board. (This command reads the published board, not a live rebuild.)")
        with open(PUBLISHED_BOARD_SNAPSHOT) as f:
            snap = json.load(f)
        board = (snap.get("boards") or {}).get(flag)
        if not board or not board.get("entries"):
            msg = "No published %s board on file yet -- run /fetch." % flag
            if board and board.get("empty_text"):   # APEX-OPS-2026-10-10
                msg = "\U0001F4CC **%s -- PUBLISHED BOARD** -- %s\n%s" % (
                    _PUBLISHED_FLAG_LABEL.get(flag, flag).upper(), snap.get("match_date", ""),
                    str(board["empty_text"]).split("\n" + "\u2500" * 30)[0])
                if EMPTY_BOARD_FIX:   # APEX-EMPTYBOARD-2026-11-11: say when it was checked and show what was held back and why
                    try:
                        _et = str(board["empty_text"])
                        for _cut in ("\n\n\U0001F3AF **MIXED SLIP", "\n_Deterministic read"):
                            _et = _et.split(_cut)[0]
                        _ck = datetime.fromisoformat(str(snap.get("captured_at"))).strftime("%I:%M %p").lstrip("0")
                        msg = ("\U0001F4CC **%s -- PUBLISHED BOARD** -- %s\n\U0001F6AB No pick passes every rule right now. "
                               "Last checked %s CT. This is the real board, not an old one.\n%s") % (
                            _PUBLISHED_FLAG_LABEL.get(flag, flag).upper(), snap.get("match_date", ""), _ck, _et)
                    except Exception:
                        pass
            if board and board.get("held"):   # APEX-STANDARDHOLD-2026-10-23
                msg = "\n".join([msg] + _cushion_held_lines(board["held"]))
            return {"text": msg, "scored": [], "held_back": []} if return_scored else msg
        rec = rec or records()
        _fs_first, _fs_last, _ = _load_freshness_index()
        entries = board.get("entries") or []
        shown, held_started, held_graded, held_retyped, held_gone = [], [], [], [], []
        _live = _live_type_map()   # APEX-TYPECHECK-2026-10-14
        _pulled = []   # APEX-PULLEDFILL-2026-10-22: (spot it held, entry, lines now offered)
        for _rank, e in enumerate(entries, 1):
            p = e.get("p") or {}
            if _is_unplayable(p):   # APEX-MOREONLY-BACKSTOP-2026-10-13
                continue
            if _pub_match_started(p):
                held_started.append(e); continue
            if _pub_is_graded(p):
                held_graded.append(e); continue
            _now_type = _retyped(p, _live)
            if _now_type:
                held_retyped.append((e, _now_type)); continue
            _gone = _line_gone(p, _live)   # APEX-STALELINE-2026-10-14
            if _gone is not None:
                held_gone.append((e, _gone))
                _pulled.append((_rank, e, _gone)); continue
            shown.append(e)
        limit = _PUBLISHED_FLAG_LIMIT.get(flag, len(shown) or 1)
        label = _PUBLISHED_FLAG_LABEL.get(flag, flag)
        # APEX-PULLEDFILL-2026-10-22: numbered picks PrizePicks pulled since the fetch
        _pulled = [x for x in _pulled if x[0] <= limit]
        _gone_ids = {_apex_prop_identity(e.get("p") or {}) for e, _ in held_gone}
        # APEX-PULLEDNEWLINE-2026-10-22: the same pick at PrizePicks' new line keeps its spot
        # when it still passes this board's bars; the rest fall to the backup fill below.
        _newl = []
        for _rk, _e, _nl in _pulled:
            _x = _pulled_new_line(flag, _e, _nl, rec, _live,
                                  shown[:limit] + [y["entry"] for y in _newl if y and y.get("entry")])
            _newl.append(_x)
        _kept = [k for k, x in enumerate(_newl) if x and x.get("entry")]
        _tofill = [k for k in range(len(_pulled)) if k not in _kept]
        _base = shown[:limit]
        for k in sorted(_kept, key=lambda k: _pulled[k][0]):   # back into the spot it held
            _base.insert(min(_pulled[k][0] - 1, len(_base)), _newl[k]["entry"])
        _nfill = max(0, min(len(_tofill), limit - len(_base)))
        _cands = _pulled_fill(flag, _base, _gone_ids, rec, _live, _nfill + len(_kept)) if _pulled else []
        _fills, _swaps = _cands[:_nfill], _cands[_nfill:]
        _fill_of = {k: _fills[j] for j, k in enumerate(_tofill[:len(_fills)])}
        _swap_of = {k: (_swaps[j] if j < len(_swaps) else None) for j, k in enumerate(_kept)}
        if _pulled:
            shown = _base + [dict(_fill_of[k], pulled_fill=_pulled[k][1].get("p") or {}) for k in _tofill if k in _fill_of] \
                + shown[limit:]
            try:   # swap options are numbered as they show under BACKUPS on this board
                _bkid = [_apex_prop_identity(r[2]) for r in
                         _board_backup_rows(flag, shown[:limit], shown[:limit], rec, exclude=_gone_ids)]
                for k, sw in _swap_of.items():
                    if sw:
                        _i = _apex_prop_identity(sw["p"])
                        sw["bk_no"] = (_bkid.index(_i) + 1) if _i in _bkid else sw.get("bk_no")
            except Exception:
                pass
            for k in _kept:
                _newl[k]["swap"] = _swap_of.get(k)
            if record:   # APEX-BACKUPORDER-2026-10-24: backup_numbers() reads without logging
                _pulled_record(flag, _pulled, [_fill_of.get(k) for k in range(len(_pulled))], _newl,
                               board_before=[e for e in entries if not _is_unplayable(e.get("p") or {})][:limit],
                               board_after=shown[:limit])
            # spots shifted, so the publish-time "Moved up/down to #N" notes no longer match
            shown = [dict(e, rerank_note=None) if e.get("rerank_note") else e for e in shown]
        L = ["\U0001F4CC **%s -- PUBLISHED BOARD** -- %s" % (label.upper(), snap.get("match_date", "")),
             "_Reads the board exactly as published at %s CT -- a prop leaves only when its "
             "match starts or it is graded, never from time passing alone. Run /fetch for a "
             "fresh live rebuild._" % snap.get("captured_at", "?")]
        if RERANK_AFTER_TOP3 and NET_SIGNAL_POINTS:   # APEX-TOP3NET-2026-10-22: say how the order is set
            L.append(order_header(flag))
        if SCORE_DISPLAY:   # APEX-SCOREDISPLAY-2026-10-25
            L.append(SCORE_EXPLAIN)
        if held_started or held_graded:
            L.append("(%d removed -- match started since publish, %d removed -- graded since publish)"
                      % (len(held_started), len(held_graded)))
        for _e, _now_type in held_retyped:   # APEX-TYPECHECK-2026-10-14
            _p = _e.get("p") or {}
            L.append("⚠️ Removed %s %s %s %s -- published as %s, PrizePicks now lists it as %s. "
                      "The next auto-eval re-scores it under the new type."
                      % (_p.get("player") if _p.get("player") != "match" else _p.get("opponent"),
                         _p.get("category"), _p.get("line"), _p.get("direction"),
                         str(_p.get("prop_type") or "standard").title(), _now_type.title()))
        for _k, (_rk, _e, _now_lines) in enumerate(_pulled):   # APEX-PULLEDFILL-2026-10-22
            _p = _e.get("p") or {}
            if _k in _kept:   # APEX-PULLEDNEWLINE-2026-10-22
                L.append(_pulled_header_kept(_p, _rk, _newl[_k]))
                continue
            _nw = (_newl[_k] or {}).get("why") if _k < len(_newl) else None
            L.append("\u26A0 PrizePicks pulled after the fetch: %s %s (was #%d)%s -- %s%s"
                     % (_pulled_label(_p), str(_p.get("direction") or "").upper(), _rk,
                        ("; now offered at %s" % ", ".join("%g" % x for x in _now_lines)) if _now_lines else "",
                        ("a backup moved up" if _k in _fill_of else "no eligible backup, spot left empty")
                        if PULLED_LINE_FILL else "spot left empty",
                        (" (%s)" % _nw) if _nw else ""))
        _pulled_ids = {id(x[1]) for x in _pulled}
        for _e, _now_lines in held_gone:   # APEX-STALELINE-2026-10-14
            if id(_e) in _pulled_ids:
                continue
            _p = _e.get("p") or {}
            L.append("⚠️ Removed %s %s %s %s (%s) -- %s"
                      % (_p.get("player") if _p.get("player") != "match" else _p.get("opponent"),
                         _p.get("category"), _p.get("line"), _p.get("direction"),
                         str(_p.get("prop_type") or "standard").title(),
                         ("PrizePicks moved it to %s." % ", ".join("%g" % x for x in _now_lines)) if _now_lines
                         else "PrizePicks no longer offers this line."))
        medals ={1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}
        try:   # APEX-BACKUPORDER-2026-10-24: one BACKUPS list for the summary line and the BACKUPS block
            _bk_rows = _board_backup_rows(flag, shown, shown[:limit], rec, exclude=_gone_ids) if BOARD_BACKUPS_LIMIT else []
        except Exception:
            _bk_rows = None
        _bk_sum = backup_summary_line(flag, _bk_rows or [])
        if _bk_sum and shown and (BOARD_BACKUPS_ON_CARD or _BACKUPS_ONLY):
            L.append(_bk_sum)
        elif shown and _bk_rows and not BOARD_BACKUPS_ON_CARD and not _BACKUPS_ONLY:   # APEX-BACKUPSCMD-2026-10-26
            L.append("\U0001F501 Backups: type %s" % BACKUPS_CMD.get(flag, "/elite6backups"))
        if not shown:
            L.append("No props remain on the published %s board right now (all started or graded "
                      "since publish) -- run /fetch for a fresh board." % label)
        _sdec = score_decimals([score_value(flag, e.get("p") or {}) for e in shown[:limit]])   # APEX-SCOREDISPLAY-2026-10-25
        for i, e in enumerate(shown[:limit], 1):
            _card = _render_pick_card(e, i, rec, _fs_first, _fs_last, medals, worst=False, show_debug=False,
                                      flag=flag, score_dec=_sdec[i - 1])
            if e.get("pulled_fill") is not None:   # APEX-PULLEDFILL-2026-10-22
                _card.insert(min(2, len(_card)), "\U0001F501 Filled in: PrizePicks pulled %s after the fetch, "
                             "so this backup moved up." % _pulled_label(e["pulled_fill"]))
            if e.get("pulled_new_line"):   # APEX-PULLEDNEWLINE-2026-10-22
                _card[min(2, len(_card)):min(2, len(_card))] = _pulled_card_lines(e["pulled_new_line"])
            L.extend(_card)
        if len(shown) > limit:
            L.append("\u2500" * 30)
            L.append("(+%d more on the published board)" % (len(shown) - limit))
        try:   # APEX-SLIPPLANNER-2026-10-11: best slip for this board's legs
            import apex_slip_planner as _sp
            _pl, _rec, _k = _sp.plan(_planner_legs(shown[:limit]))
            if _pl:
                L.append("\u2500" * 30)
                L.extend(_pl)
        except Exception:
            pass
        try:   # APEX-DECLINEDDATA-2026-10-22: held props leave a note, not silence
            import apex_data_health as _dhn
            _note = _dhn.board_note(_BACKUP_BOARD_TYPES.get(flag))
            if _note:
                L += ["\u2500" * 30, _note]
        except Exception:
            pass
        if BOARD_BACKUPS_ON_CARD or _BACKUPS_ONLY:   # APEX-BACKUPSCMD-2026-10-26
            L.extend(_board_backup_lines(flag, shown, shown[:limit], rec, exclude=_gone_ids, rows=_bk_rows))   # APEX-BOARDBACKUPS-2026-10-21
        L.extend(_cushion_held_lines(board.get("held")))   # APEX-STANDARDHOLD-2026-10-23
        text = "\n".join(L)
        if return_scored:
            _bn = [r for r in (_bk_rows or []) if not backup_is_held(r[3], r[4])]
            return {"text": text, "scored": shown[:limit],
                    "held_back": held_started + held_graded + [e for e, _ in held_retyped]
                                 + [e for e, _ in held_gone],
                    "backups": [{"no": i, "id": _apex_prop_identity(r[2]), "rank": board_rank_of(flag, r[2]),
                                 "prob": r[0], "label": backup_short_label(r[2])} for i, r in enumerate(_bn, 1)]}
        return text
    except Exception as ex:
        msg = "published board unavailable (%s: %s)" % (type(ex).__name__, ex)
        return {"text": msg, "scored": [], "held_back": []} if return_scored else msg


def backups_detail(flag):
    """APEX-BACKUPSCMD-2026-10-26: the BACKUPS block of one board on its own (same rows, same
    order, same cards as before), for the per-board backups slash commands. Read-only."""
    global _BACKUPS_ONLY
    label = {"top3": "TOP 3", "elite6": "ELITE 6", "top10": "TOP 10", "bestgoblin": "BEST GOBLIN",
             "bestdemon": "BEST DEMON", "beststandard": "BEST STANDARD"}.get(flag, str(flag).upper())
    _BACKUPS_ONLY = True
    try:
        txt = published_board_detail(flag, record=False)
    finally:
        _BACKUPS_ONLY = False
    lines = str(txt or "").split("\n")
    idx = next((i for i, l in enumerate(lines) if l.startswith("\U0001F501 **BACKUPS**")), None)
    head = "\U0001F501 **%s BACKUPS** -- next in line if a board pick vanishes (same order the board ranks them)" % label
    summ = next((l for l in lines if l.startswith("\U0001F501 Backups in order")), None)
    if idx is None:
        return head + "\nNone right now: every pick that passed 70%% and Apex %d is already on the %s board." % (BOARD_MIN_SCORE, label.title())
    return "\n".join([head] + ([summ] if summ else []) + lines[idx:])


def backup_numbers(flag):
    """APEX-BACKUPORDER-2026-10-24: {prop identity: {"no", "rank", "prob", "label"}} for the
    numbered BACKUPS on `flag`'s posted board, numbered exactly as the card shows them (the BOARD
    CHECK and the ODDS RELEASED / NEW PROPS card use it). {} on failure."""
    try:
        r = published_board_detail(flag, return_scored=True, record=False)
        return {tuple(b["id"]): b for b in ((r or {}).get("backups") or [])} if isinstance(r, dict) else {}
    except Exception:
        return {}


# ================================================================ APEX-BB-002a
MIXED_IDS_FIX = True
FEED_500_LIMIT = False   # APEX-FEEDCHECK-2026-11-01


def _two_pulls_counted(p):
    try:
        import apex_two_pulls as _tp
        return _tp.counted(p)
    except Exception:
        return False


LINE_REMOVALS_LOG = os.path.join(HERE, "data", "logs", "line_removals.csv")


def _log_line_removals(rows, why, pull_ts=None):
    """APEX-FEEDCHECK-2026-11-01: one row per recent line taken off the candidate list, with the reason, so a
    drop can be traced. Lines last scored 3+ days ago and repeats within 6 hours are skipped. Never raises."""
    try:
        from datetime import datetime as _d, timedelta as _td
        now = _d.now()
        cut = (now - _td(days=3)).isoformat()
        out = []
        try:   # shared across processes: boards are also built by short-lived jobs
            with open(LINE_REMOVALS_LOG + ".state") as fh:
                seen = {k: v for k, v in json.load(fh).items() if v >= (now - _td(hours=6)).isoformat()}
        except Exception:
            seen = {}
        for r in rows:
            if str(r.get("ts") or "") < cut:
                continue
            k = (r.get("player"), r.get("opponent"), r.get("category"), r.get("line"), r.get("prop_type"), why)
            sk = "|".join(str(x) for x in k)
            if sk in seen:
                continue
            seen[sk] = now.isoformat(timespec="seconds")
            out.append([now.isoformat(timespec="seconds"), why, pull_ts or ""] + list(k[:5]) + [r.get("match_id"), r.get("ts")])
        if out:
            with open(LINE_REMOVALS_LOG + ".state.tmp", "w") as fh:
                json.dump(seen, fh)
            os.replace(LINE_REMOVALS_LOG + ".state.tmp", LINE_REMOVALS_LOG + ".state")
            new = not os.path.exists(LINE_REMOVALS_LOG)
            with open(LINE_REMOVALS_LOG, "a", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(["logged_at", "reason", "pull_ts", "player", "opponent", "category", "line",
                                "prop_type", "match_id", "last_row_ts"])
                w.writerows(out)
    except Exception:
        pass


def load_current_board(today=None, path=None):
    """Most recent evaluated board from the pick log. Rows with match_date >=
    today, deduped on a STABLE PROP IDENTITY (player, opponent, category,
    line) -- newest ts wins. APEX-PROPID-2026-09-30 (Prop Identity Repair):
    previously deduped on (match_id, player, category, line, prop_type),
    which meant a tier change or a match_id drift (match_date shifting a
    day between scrapes of the same real match) each created a brand-new
    key instead of updating the existing one -- so the OLD tier/match_id
    never retired and kept showing up in Best Demon/Goblin/Standard
    alongside the new, correct one. Tier Integrity Audit (2026-09-30)
    found this live: Andrey Rublev Breaks 1.5 showing as both Standard
    (stale, 9/29 scrape) and Goblin (current, 9/30 scrape) at once, and
    ~60 markets duplicated under two match_ids for the same real match.
    This function is the single read path for /top10, /bestgoblin,
    /bestdemon, /beststandard, /elite6, /allpicks -- fixing it here fixes
    all of them without touching scoring, confidence, or ranking formulas.
    Returns (picks, note) -- note is non-empty when there is nothing to rank.
    Deterministic: reads the log, calls no model, creates no projections."""
    from datetime import date as _d
    today = today or _d.today().isoformat()
    path = path or PICK_LOG
    if not os.path.exists(path):
        return [], "no pick log found (%s) -- the engine has not evaluated a board yet" % path
    best = {}
    _retired = []   # APEX-PROPID-2026-09-30: old-tier / old-match_id rows a newer scrape superseded
    _scrape_ts = {}   # APEX-REPRICE-2026-10-11: newest REAL fetch ts per prop (background re-scores excluded)
    for r in _read(path):
        key = _apex_prop_identity(r)
        if not str(r.get("price_source") or "").startswith("reprice:"):
            if (r.get("ts") or "") > _scrape_ts.get(key, ""):
                _scrape_ts[key] = r.get("ts") or ""
        prev = best.get(key)
        if prev is None or (r.get("ts") or "") > (prev.get("ts") or ""):
            if prev is not None and ((prev.get("prop_type") or "") != (r.get("prop_type") or "")
                                      or (prev.get("match_id") or "") != (r.get("match_id") or "")):
                _retired.append((prev.get("player"), prev.get("opponent"), prev.get("category"),
                                  prev.get("line"), prev.get("prop_type"), prev.get("match_id"),
                                  r.get("prop_type"), r.get("match_id"), r.get("ts")))
            best[key] = r
    # APEX-BOARDHEALTH-2026-09-30: split the identity-dedup retirements into
    # match-id drift vs tier drift, for the Board Health Report. Pure
    # bookkeeping on the SAME _retired list already built above -- the
    # drop decision itself (newest ts wins) is untouched. A row whose
    # match_id changed is counted as match-id drift even if its tier also
    # changed (a different match_id means a genuinely different match,
    # which is the more fundamental reason); a tier-only change (same
    # match_id) is counted as tier drift.
    _matchid_drift_n = 0
    _tier_drift_n = 0
    for (_pl, _opp, _cat, _ln, _old_t, _old_m, _new_t, _new_m, _ts) in _retired:
        if (_old_m or "") != (_new_m or ""):
            _matchid_drift_n += 1
        elif (_old_t or "") != (_new_t or ""):
            _tier_drift_n += 1
        else:
            _matchid_drift_n += 1   # no visible diff on either field -- rare, defaults here
    if _retired:
        try:
            for (_pl, _opp, _cat, _ln, _old_t, _old_m, _new_t, _new_m, _ts) in _retired:
                _retire_print("[board] PROP IDENTITY: retired stale record -- %s vs %s | %s %s | "
                      "old_tier=%s old_match_id=%s -> new_tier=%s new_match_id=%s (as of %s)"
                      % (_pl, _opp, _cat, _ln, _old_t, _old_m, _new_t, _new_m, _ts))
        except Exception:
            pass

    # APEX-OPPDRIFT-2026-09-30 (Opponent Drift Elimination): the identity
    # fix above only collapses duplicates when the OPPONENT stays the same
    # (a tier change or a match_id drift for the same real matchup). It did
    # nothing when a player's opponent itself changes -- a new scrape that
    # moves a player onto a different opponent/match_id creates a brand-new
    # identity key (different opponent string), so the old opponent's rows
    # never got retired and stayed active side-by-side with the new ones.
    # Match Integrity Audit (2026-09-30) found this live: Daria Snigur vs
    # Katarzyna Kawa (stale, 9/29 scrape) and Daria Snigur vs Jasmine
    # Paolini (current, 9/30 scrape) both active at once -- 15 players,
    # 46 props affected board-wide. A real player can only have ONE current
    # opponent, so: for every player name, find whichever opponent/match_id
    # has the newest ts among their rows (checking both player-level rows
    # and match-level rows, where player="match" and the two names are
    # packed into opponent as "NAME v NAME"), and drop every row -- any
    # category, any line, any tier -- tied to that player's OTHER
    # opponent/match_id. This is the only thing this block changes: no
    # score, confidence, or ranking math is touched.
    def _oppdrift_participants(r):
        pl = (r.get("player") or "").strip()
        if pl.lower() == "match":
            opp = (r.get("opponent") or "")
            parts = [p.strip() for p in opp.split(" v ", 1)] if " v " in opp else \
                    ([p.strip() for p in opp.split(" vs ", 1)] if " vs " in opp else [])
            return [p for p in parts if p]
        return [pl] if pl else []

    _player_latest = {}   # name.lower() -> (ts, match_id) of that player's newest-seen matchup
    _player_latest_row = {}   # APEX-DATEMOVE-2026-10-25: name.lower() -> that newest row
    for r in best.values():
        ts = r.get("ts") or ""
        mid = r.get("match_id")
        for name in _oppdrift_participants(r):
            key = name.lower()
            prev = _player_latest.get(key)
            if prev is None or ts > prev[0]:
                _player_latest[key] = (ts, mid)
                _player_latest_row[key] = r

    # APEX-MIXEDIDS-2026-11-01 (owner, "Something still ain't right"): after a date move and a partial
    # re-score, one player's newest row can carry the new match_id and the other's the old one (Van Assche
    # v Navone, 11/2 and 11/3). Each row then failed one player's check below and was retired as a "stale
    # matchup" though PrizePicks still offered it. Same two players within 3 days: both take the newest id.
    # Undo: MIXED_IDS_FIX = False.
    if MIXED_IDS_FIX:
        for _na, _ra in list(_player_latest_row.items()):
            _pa = (_ra.get("player") or "").strip().lower()
            _names = [n.lower() for n in _oppdrift_participants(_ra)] if _pa == "match" else \
                [_pa, (_ra.get("opponent") or "").strip().lower()]
            if len(_names) != 2 or _na not in _names:
                continue
            _nb = _names[0] if _names[1] == _na else _names[1]
            _rb = _player_latest_row.get(_nb)
            if not _rb or (_rb.get("match_id") == _ra.get("match_id")
                           and str(_rb.get("match_date")) == str(_ra.get("match_date"))):
                continue   # APEX-MATCHID-2026-11-01: same id can now carry two dates after a move
            _pb = (_rb.get("player") or "").strip().lower()
            _bn = {n.lower() for n in _oppdrift_participants(_rb)} if _pb == "match" else \
                {_pb, (_rb.get("opponent") or "").strip().lower()}
            if _bn != set(_names):
                continue
            try:
                from datetime import date as _mid
                if abs((_mid.fromisoformat(str(_ra.get("match_date"))[:10])
                        - _mid.fromisoformat(str(_rb.get("match_date"))[:10])).days) > 3:
                    continue
            except Exception:
                continue
            _nw = _ra if (_ra.get("ts") or "") >= (_rb.get("ts") or "") else _rb
            for _n in _names:
                _player_latest[_n] = (_nw.get("ts") or "", _nw.get("match_id"))
                _player_latest_row[_n] = _nw

    # APEX-DATEMOVE-2026-10-25 (owner, "Go fix date"): match_id hashes the match date,
    # so when PrizePicks moves a match to another day the props re-scored after the move get a
    # new match_id while unchanged ones keep the old one -- and the drift pass below retired
    # them as a "stale matchup" (Kraus v Udvardy TG 17.5 80%, Mertens v Uchijima TG 16.5 83%
    # on 10/25 after their match moved 10/27 -> 10/26). Same two players, dates within 3 days =
    # the same match moved: the row is kept and takes the newest row's match_id, date and start.
    def _pair_of(r):
        pl = (r.get("player") or "").strip().lower()
        if pl == "match":
            return frozenset(n.lower() for n in _oppdrift_participants(r))
        return frozenset((pl, (r.get("opponent") or "").strip().lower()))

    def _date_moved_to(r):
        names = _oppdrift_participants(r)
        if not names:
            return None
        news = [_player_latest_row.get(n.lower()) for n in names]
        if any(x is None for x in news) or len({x.get("match_id") for x in news}) != 1:
            return None
        new = news[0]
        # APEX-MATCHID-2026-11-01: a moved match now keeps its id, so also compare the date.
        if (new.get("match_id") == r.get("match_id") and str(new.get("match_date")) == str(r.get("match_date"))) \
                or _pair_of(new) != _pair_of(r):
            return None
        try:
            from datetime import date as _dmd
            gap = abs((_dmd.fromisoformat(str(new.get("match_date"))[:10])
                       - _dmd.fromisoformat(str(r.get("match_date"))[:10])).days)
        except Exception:
            return None
        return new if gap <= 3 else None

    global LAST_DATE_MOVES
    LAST_DATE_MOVES = []
    for _k, _r in list(best.items()):
        _new = _date_moved_to(_r)
        if _new is not None:
            best[_k] = dict(_r, match_id=_new.get("match_id"), match_date=_new.get("match_date"),
                            slip_start_utc=_new.get("slip_start_utc") or _r.get("slip_start_utc"),
                            apex_date_moved_from=_r.get("match_date"))
            LAST_DATE_MOVES.append(best[_k])
    if LAST_DATE_MOVES:
        try:
            print("[board] DATE MOVE: kept %d prop(s) whose match PrizePicks moved to another day (%s)"
                  % (len(LAST_DATE_MOVES), ", ".join(sorted({"%s %s->%s" % (
                      " v ".join(sorted(_pair_of(x))), x["apex_date_moved_from"], x.get("match_date"))
                      for x in LAST_DATE_MOVES}))))
        except Exception:
            pass

    def _oppdrift_current(r):
        names = _oppdrift_participants(r)
        if not names:
            return True   # no identifiable participant -- fail open, leave it alone
        mid = r.get("match_id")
        return all(_player_latest.get(n.lower(), (None, mid))[1] == mid for n in names)

    # APEX-LIVECHECK-2026-11-01 (owner, "Go live check"): a line PrizePicks' latest pull still offers, for
    # the same two players as that player's newest row, is never retired as a stale matchup.
    _lc_keys = None
    try:
        import apex_live_check as _lc
        _lc_keys = _lc.live_keys()
    except Exception:
        _lc = None

    def _lc_rescue(r):
        if not (_lc and _lc_keys and _lc.is_live(r, _lc_keys)):
            return False
        _new = [_player_latest_row.get(n.lower()) for n in _oppdrift_participants(r)]
        return any(x is not None and _pair_of(x) == _pair_of(r) for x in _new)

    for _k, _r in list(best.items()):
        if not _oppdrift_current(_r) and _lc_rescue(_r):
            _nw = next(x for x in (_player_latest_row.get(n.lower()) for n in _oppdrift_participants(_r))
                       if x is not None and _pair_of(x) == _pair_of(_r))
            for _n in _oppdrift_participants(_r):
                _player_latest[_n.lower()] = (_nw.get("ts") or "", _nw.get("match_id"))
                _player_latest_row[_n.lower()] = _nw
            best[_k] = dict(_r, match_id=_nw.get("match_id"), match_date=_nw.get("match_date"),
                            slip_start_utc=_nw.get("slip_start_utc") or _r.get("slip_start_utc"))

    _oppdrift_dropped = [r for r in best.values() if not _oppdrift_current(r)]
    if _oppdrift_dropped:
        best = {k: r for k, r in best.items() if _oppdrift_current(r)}
        _log_line_removals(_oppdrift_dropped, "another match is newer for this player")
        try:
            for _r in _oppdrift_dropped:
                _retire_print("[board] OPPONENT DRIFT: retired stale matchup -- %s vs %s | %s %s | "
                      "tier=%s old_match_id=%s (superseded as of %s)"
                      % (_r.get("player"), _r.get("opponent"), _r.get("category"),
                         _r.get("line"), _r.get("prop_type"), _r.get("match_id"), _r.get("ts")))
        except Exception:
            pass

    # APEX-FETCHRETIRE-2026-09-30 (Fetch-Based Prop Retirement): every fix
    # above only retires a prop when something NEWER replaces it under a
    # related identity (a tier change, a match_id drift, an opponent
    # change). None of them catch a prop that simply stopped being offered
    # on PrizePicks with nothing replacing it -- it just sat there, still
    # "active," forever. Prop Removal Audit (2026-09-30) confirmed this
    # live: 515 of 559 active props, and 21 of 23 picks actually showing in
    # Elite6/Top10/Best Goblin/Best Standard, had not been seen in the most
    # recent /fetch -- one example (Daria Shnaider's Breaks market) was
    # independently confirmed gone from PrizePicks' live app entirely.
    # Fix: the newest scrape run is identified from this log's own
    # timestamps (a real /fetch logs a burst of rows with small gaps
    # between them; distinct runs are reliably FETCH_RUN_GAP_MINUTES+
    # apart -- confirmed from the real history on this board). Every
    # identity whose newest row falls before that run started gets
    # retired, same as the other three fixes: dropped from the ACTIVE
    # board only. Nothing is deleted from the CSV log, so historical
    # tracking, ALIS, and settlement are untouched. MIN_FETCH_RUN_SIZE is
    # the safety valve -- a suspiciously small "latest run" (a partial or
    # failed scrape) skips this pass entirely rather than risk retiring
    # the whole board over one bad fetch.
    _fetchretire_dropped = []
    global LAST_FETCH_RETIRE_DEBUG
    try:
        from datetime import datetime as _frdt
        # APEX-FETCHSEEN-2026-10-11: a fetch that found a prop unchanged records a
        # sighting instead of a new row -- count it as seen in that scrape.
        try:
            with open(os.path.join(HERE, "data", "logs", "fetch_seen.json")) as _fs:
                _seen = json.load(_fs)
            for _k in best:
                _sts = _seen.get("|".join(str(x) for x in _k))
                if _sts and _sts > _scrape_ts.get(_k, ""):
                    _scrape_ts[_k] = _sts
        except Exception:
            pass
        # APEX-REPRICE-2026-10-11: run detection uses each prop's last REAL fetch
        # time -- a background re-score is not evidence PrizePicks still offers it.
        _ts_rows = sorted(((_scrape_ts.get(_apex_prop_identity(r)), r) for r in best.values()
                           if _scrape_ts.get(_apex_prop_identity(r))),
                          key=lambda x: x[0])
        _parsed = [(_frdt.fromisoformat(t), r) for t, r in _ts_rows]
        # APEX-FETCHRETIRE-WINDOWFIX-2026-10-07-v2 (owner, authorized):
        # v1 of this fix (same date) replaced gap-scanning with a flat
        # "within FETCH_RUN_GAP_MINUTES (45) of the newest row" cutoff --
        # WRONG, confirmed live within the hour: real fetch cycles are not
        # evenly spaced. Today alone, gaps between consecutive fetch runs
        # measured 62, 72, 82, 114, 145, 159, 224, 245, 510, 664, and 731
        # minutes -- all routinely far more than 45. A flat 45-minute
        # cutoff retired props that were still perfectly current, just
        # because the NEXT fetch hadn't happened yet, making on-demand
        # board commands (/elite6, /top3, etc.) come back empty for most
        # of the gap between fetches -- a regression, not a fix.
        #
        # v2: back to gap-based clustering (find where the LATEST distinct
        # scrape burst starts, by walking backward for the first gap of
        # FETCH_RUN_GAP_MINUTES+ between consecutive rows) -- this is
        # robust to irregular real-world spacing, since it only asks "is
        # there a pause here," never "how long ago was this." The one
        # actual bug being fixed: the ORIGINAL code defaulted to
        # _run_start_idx = 0 (the entire log, since the beginning of time)
        # whenever no such gap existed anywhere -- confirmed live earlier
        # today, a 24.6h-old single-observation prop ranked #1 on Elite6
        # because of exactly this silent fallback. Correct fail-open
        # behavior when no gap is found is to treat detection as
        # unreliable and SKIP the retirement pass for this call (same as
        # the existing MIN_FETCH_RUN_SIZE safety valve already does for a
        # suspiciously small latest run) -- never to treat the whole
        # history as "current."
        _run_start_idx = None
        for i in range(len(_parsed) - 1, 0, -1):
            _gap_min = (_parsed[i][0] - _parsed[i - 1][0]).total_seconds() / 60.0
            if _gap_min >= FETCH_RUN_GAP_MINUTES:
                _run_start_idx = i
                break
        if _run_start_idx is None:
            # No gap anywhere in the log -- detection unreliable, fail open.
            _latest_run_rows = []
        else:
            _latest_run_rows = _parsed[_run_start_idx:]
        _skipped_too_small = 0 < len(_latest_run_rows) < MIN_FETCH_RUN_SIZE
        if len(_latest_run_rows) >= MIN_FETCH_RUN_SIZE:
            _latest_run_ids = set(_apex_prop_identity(r) for _, r in _latest_run_rows)
            for _key, _r in list(best.items()):
                if _key not in _latest_run_ids and not _two_pulls_counted(_r):   # APEX-TWOPULLS-2026-11-01
                    _fetchretire_dropped.append(_r)
                    del best[_key]
            # APEX-FETCHRETIRE-DIAG-2026-09-30: snapshot for audit scripts only
            LAST_FETCH_RETIRE_DEBUG = {
                "run_start_ts": _latest_run_rows[0][0].isoformat() if _latest_run_rows else None,
                "run_end_ts": _latest_run_rows[-1][0].isoformat() if _latest_run_rows else None,
                "run_row_count": len(_latest_run_rows),
                "latest_run_ids": set(_latest_run_ids),
                "retained_count": len(best),
                "retired_count": len(_fetchretire_dropped),
                "skipped_too_small": False,
            }
        else:
            LAST_FETCH_RETIRE_DEBUG = {
                "run_start_ts": None, "run_end_ts": None, "run_row_count": len(_latest_run_rows),
                "latest_run_ids": set(), "retained_count": len(best), "retired_count": 0,
                "skipped_too_small": _skipped_too_small,
            }
    except Exception:
        pass   # fail open -- a parse error here must never take the board down
    # APEX-STALELINE-2026-10-14 (owner, "Go" 2026-10-14): the gap test
    # above merges fetches under 45 min apart into one "latest run", so a line
    # PrizePicks moved or pulled between them stayed active (Sakkari Fantasy
    # Score 4.5 Goblin on four boards after it moved to 6.5). Keep a pick only
    # if the newest full partner-API pull offered its exact line and type.
    # Skipped (fail open) when that pull is missing, small, possibly cut off
    # at the 500-row page, or older than the newest fetch rows by 20+ min
    # (e.g. a Playwright-fallback fetch evaluated props it never saw).
    global LAST_LIVE_RETIRE_DEBUG
    LAST_LIVE_RETIRE_DEBUG = {"applied": False}
    try:
        from datetime import datetime as _lbdt, timedelta as _lbtd
        with open(os.path.join(HERE, "data", "logs", "fetch_live_board.json")) as _lbf:
            _lb = json.load(_lbf)
        _lb_ts = _lbdt.fromisoformat(_lb["ts"])
        _newest = max((_lbdt.fromisoformat(v) for v in _scrape_ts.values() if v), default=None)
        # APEX-FEEDCHECK-2026-11-01 (owner, "Go feed check"): the feed has no 500-row page (951 on 11/1).
        # Under that old limit this check ran only when the feed was small, and dumped lines in bursts when the
        # feed fell below 500 (120 at 2:40 AM, 229 at 7:16 AM on 11/1). Undo: FEED_500_LIMIT = True.
        _ok = (MIN_FETCH_RUN_SIZE <= int(_lb.get("count") or 0) and (not FEED_500_LIMIT or int(_lb.get("raw") or 0) < 500)
               and (_newest is None or _lb_ts >= _newest - _lbtd(minutes=20)))
        LAST_LIVE_RETIRE_DEBUG = {"applied": _ok, "board_ts": _lb["ts"], "board_count": _lb.get("count"),
                                  "newest_fetch_row": _newest.isoformat() if _newest else None, "retired": []}
        if _ok:
            _live_keys = {(c, n, float(l), t) for c, n, l, t in _lb["props"]}
            for _key, _r in list(best.items()):
                _pl = str(_r.get("player") or "").strip().lower()
                _names = ([x.strip() for x in str(_r.get("opponent") or "").lower().split(" v ")]
                          if _pl == "match" else [_pl])
                _ot = str(_r.get("prop_type") or "standard").strip().lower()
                _ot = "demon" if _ot == "red_demon" else _ot
                try:
                    _ln = float(_r.get("line"))
                except (TypeError, ValueError):
                    continue
                if not any((_r.get("category"), nm, _ln, _ot) in _live_keys for nm in _names) \
                        and not _two_pulls_counted(_r):   # APEX-TWOPULLS-2026-11-01
                    _fetchretire_dropped.append(_r)
                    LAST_LIVE_RETIRE_DEBUG["retired"].append(_r)
                    del best[_key]
    except Exception:
        pass   # fail open
    try:   # APEX-TWOPULLS-2026-11-01
        import apex_two_pulls as _tp
        _log_line_removals([x for x in best.values() if _tp.missing_once(x)], "missing from one pull, kept")
    except Exception:
        pass
    if _fetchretire_dropped:
        try:
            _lv = {id(x) for x in (LAST_LIVE_RETIRE_DEBUG.get("retired") or [])}
            _log_line_removals([x for x in _fetchretire_dropped if id(x) in _lv], "missing from two pulls",
                               LAST_LIVE_RETIRE_DEBUG.get("board_ts"))
            _log_line_removals([x for x in _fetchretire_dropped if id(x) not in _lv], "not in newest fetch run")
        except Exception:
            pass
        try:
            for _r in _fetchretire_dropped:
                _retire_print("[board] FETCH RETIREMENT: not in latest scrape -- retired -- %s vs %s | "
                      "%s %s | tier=%s last_seen=%s"
                      % (_r.get("player"), _r.get("opponent"), _r.get("category"),
                         _r.get("line"), _r.get("prop_type"), _r.get("ts")))
        except Exception:
            pass

    # APEX-DEADPICK-2026-08-31: drop picks that already carry a grade.
    # match_date is a DATE, so a finished match stayed "today" all day and
    # its picks kept entering the rank log after the outcome was known.
    # Key includes opponent so match-level rows (player="match", the pair in
    # opponent) cannot collide on a shared line.
    _graded = set()
    try:
        for _g in _read(HAND_LOG):
            if (_g.get("actual") or "").strip():
                _graded.add(((_g.get("match_date") or "").strip(),
                             (_g.get("player") or "").strip().lower(),
                             (_g.get("opponent") or "").strip().lower(),
                             (_g.get("category") or "").strip(),
                             (_g.get("line") or "").strip()))
    except Exception:
        _graded = set()          # fail open: no grades read, board unchanged

    def _is_dead(r):
        return ((r.get("match_date") or "").strip(),
                (r.get("player") or "").strip().lower(),
                (r.get("opponent") or "").strip().lower(),
                (r.get("category") or "").strip(),
                (r.get("line") or "").strip()) in _graded

    # APEX-BOARDFILTER-2026-08-31: a pick leaves the board when its match
    # STARTS (slip_start_utc past), and an unplayable pick (playable=0:
    # MORE-only prop, engine says LESS) never appears at all. Blank values
    # fall back to the date + graded filters. Fail open on parse errors.
    from datetime import datetime as _bfdt, timezone as _bftz
    _bfnow = _pub_now()   # APEX-STARTCLOCK-2026-10-27: same clock as every other started-check in this publish
    def _bf_started(r):
        s = _current_start_utc(r)   # APEX-STARTINDEX-2026-10-10, APEX-RESCHEDULE-2026-10-12
        if not s:
            return False
        try:
            st = _bfdt.fromisoformat(s)
            if st.tzinfo is None:
                st = st.replace(tzinfo=_bftz.utc)
            return st <= _bfnow
        except Exception:
            return False
    _dated = [r for r in best.values() if (r.get("match_date") or "") >= today]
    _unplayable = sum(1 for r in _dated if _is_unplayable(r))
    _started    = sum(1 for r in _dated if _bf_started(r))
    _dated = [r for r in _dated
              if not _is_unplayable(r) and not _bf_started(r)]
    try:   # APEX-THINDQ-2026-11-02: thin players' stat props are tracked apart, never on a board
        import apex_player_checks as _pcq
        _thin_n = len(_dated)
        _dated = [r for r in _dated if not _pcq.disqualified(r)]
        if len(_dated) < _thin_n:
            print("[board] thin players: %d prop(s) kept off the boards (tracked in data/thin_players)"
                  % (_thin_n - len(_dated)))
    except Exception:
        pass
    if _unplayable or _started:
        try:
            print("[board] excluded %d unplayable, %d already-started pick(s)"
                  % (_unplayable, _started))
        except Exception:
            pass
    live = [r for r in _dated if not _is_dead(r)]
    _dropped = len(_dated) - len(live)
    if _dropped:
        try:
            print("[board] excluded %d already-graded pick(s)" % _dropped)
        except Exception:
            pass

    # APEX-BOARDHEALTH-2026-09-30: consolidated removal-reason snapshot for
    # the Board Health Report's Retirement Summary. Every number here is
    # read from counters the removal passes above already computed --
    # nothing is recalculated or re-decided.
    global LAST_BOARD_BUILD_DEBUG
    try:
        LAST_BOARD_BUILD_DEBUG = {
            "matchid_retired":    _matchid_drift_n,
            "tier_retired":       _tier_drift_n,
            "opponent_retired":   len(_oppdrift_dropped),
            "fetch_retired":      len(_fetchretire_dropped),
            "unplayable_retired": _unplayable + _started,
            "graded_retired":     _dropped,
            "active_board":       len(live),
        }
    except Exception:
        pass

    if not live:
        last = max((r.get("match_date") or "" for r in best.values()), default="")
        return [], "no active board -- most recent evaluated match date is %s" % (last or "unknown")
    # APEX-ACESCAL-TRK-2026-09-09: the calibrated probability written by the engine.
    # Missing or unparsable (every row before 09-09) falls back to the raw value.
    def _cal_of(r, raw):
        try:
            v = float(r.get("prob_cal") or 0)
            return v if 0.0 < v <= raw + 1e-9 else raw
        except (TypeError, ValueError):
            return raw
    out = []
    for r in live:
        try:
            prob = float(r.get("prob") or 0)
        except (TypeError, ValueError):
            prob = 0.0
        out.append({"match_id": r.get("match_id") or "",
                    "player": r.get("player") or "", "opponent": r.get("opponent") or "",
                    "category": r.get("category") or "", "line": r.get("line") or "",
                    "direction": r.get("direction") or "", "prob": prob,
                    "prob_cal": _cal_of(r, prob),          # APEX-ACESCAL-TRK-2026-09-09
                    "prop_type": r.get("prop_type") or "", # APEX-TYPECMD-TRK-2026-09-09
                    "bo5": str(r.get("best_of") or "").strip() in ("5", "5.0"),
                    "edge": r.get("edge") or "",
                    "match_date": r.get("match_date") or "",
                    # APEX-BOARD-ELIGIBILITY-CARRY-2026-09-28: these were
                    # computed and written to the pick log by the floor-to-
                    # eligibility patch, but load_current_board() built its
                    # own reduced dict and never copied them across -- so
                    # _best_eligible() downstream always saw them as absent
                    # and fell back to its "treat as eligible" default,
                    # silently making the Best Goblin/Best Standard filter
                    # a no-op for every row. Carrying them through fixes that.
                    "best_goblin_eligible": r.get("best_goblin_eligible"),
                    "best_standard_eligible": r.get("best_standard_eligible"),
                    "ts": r.get("ts"),                       # APEX-PUBLISHPOOL-2026-10-11: when this prop was last priced
                    "slip_start_utc": r.get("slip_start_utc")})
    return out, ""


_BB_CAT_TITLE = {"dfs": "DOUBLE FAULT", "fantasy_score": "FANTASY", "aces": "ACE",
                 "breaks": "BREAK", "games_won": "GAMES WON", "total_games": "TOTAL GAMES"}

def apex_confidence(cat, bo5, rec, score=None, prob=None, evidence=None, attention=None,
                     warnings=None, card_present=None):
    """APEX-CONF-002-2026-09-29: pick-level confidence redesign, approved after
    a full audit + backtest showed the prior version (APEX-CONF-001, kept
    below in history) was a fixed per-category label -- only 4 distinct
    values ever came out of it (15/40/60/65) and it did not separate strong
    picks from weak ones (median-split gap was -1.1pt, i.e. backwards).
    0-100, independent of score. Never modifies APEX SCORE itself -- score,
    prob, evidence, attention are read in as INPUTS here, not altered.

        confidence = 50
          + (apex_score - 50) * 0.5
          + (category_actual_win_rate - 50) * 0.6      <- rec["cats"][cat]["rate"], real settled history
          + (stated_probability*100 - BREAK_EVEN*100) * 0.4
          + evidence_bonus + attention_bonus + sample_size_bonus
          - |category_calibration_gap_pts| * 0.25
          clamped to 0-100

    evidence_bonus: apex_score's real evidence labels are Very Low / Low /
    Moderate / Strong / Proven (not the Thin/Untested wording used while
    this was being designed) -- mapped onto the approved schedule in that
    same order: Proven +8, Strong +4, Moderate 0, Low -4, Very Low -8.
    attention_bonus: HIGH +3, MEDIUM +1, LOW 0 (kept small on purpose --
    attention already earns +5/+2 inside APEX SCORE itself; this is not a
    second full-strength copy of that bonus).
    sample_size_bonus: n>=50 +3, n>=25 0, n>=10 -3, else -8.

    score/prob/evidence/attention are keyword-only and optional so any
    older standalone script still calling the pre-redesign signature keeps
    working -- it falls back to a category-only estimate (no per-pick
    terms) rather than erroring.

    APEX-CONF-002 note: the old version's components 4 and 5 (active
    HYPOTHESIS_REGISTRY.json entries, and "narrow"/"overconfident" model
    warnings) are NOT part of the approved formula and are removed here.
    That also removes the only place in this codebase where anything
    apex_l5b_reasoner.py writes could reach a live score -- see the ALIS
    governance note in the redesign report for why that is disclosed as a
    side effect, not a hidden change.
    """
    cs = rec["cats"].get(cat) or _stat([])
    n = cs.get("n", 0)
    why = []
    pts = 50.0
    if score is not None:
        try:
            pts += (float(score) - 50.0) * 0.5
        except (TypeError, ValueError):
            pass
    cat_rate = cs.get("rate")
    if cat_rate is not None:
        wr_pts = cat_rate * 100.0
        pts += (wr_pts - 50.0) * 0.6
        why.append("%s category win rate %.1f%% (n=%d)" % (LABEL.get(cat, cat), wr_pts, n))
    else:
        why.append("no settled history for %s yet" % LABEL.get(cat, cat))
    if prob is not None:
        try:
            pts += (float(prob) * 100.0 - BREAK_EVEN * 100.0) * 0.4
        except (TypeError, ValueError):
            pass
    EV_BONUS = {"Proven": 8.0, "Strong": 4.0, "Moderate": 0.0, "Low": -4.0, "Very Low": -8.0}
    if evidence is not None:
        eb = EV_BONUS.get(evidence, 0.0)
        pts += eb
        if eb: why.append("%s evidence (%+.0f)" % (evidence, eb))
    ATT_BONUS = {"HIGH": 3.0, "MEDIUM": 1.0, "LOW": 0.0}
    if attention is not None:
        ab = ATT_BONUS.get(attention, 0.0)
        pts += ab
        if ab: why.append("%s attention (%+.0f)" % (attention, ab))
    if n >= 50: sb = 3.0
    elif n >= 25: sb = 0.0
    elif n >= 10: sb = -3.0
    else: sb = -8.0
    pts += sb
    if sb: why.append("category n=%d (%+.0f)" % (n, sb))
    cal = cs.get("cal_pts")
    if cal is not None:
        cal_pts = cal * 100.0
        if abs(cal_pts) >= 4:
            pen = abs(cal_pts) * 0.25
            pts -= pen
            why.append("calibration %+.1fpt gap (-%.1f)" % (cal_pts, pen))
    pts = max(0.0, min(100.0, pts))
    tier = "High" if pts >= 70 else "Moderate" if pts >= 50 else "Low" if pts >= 30 else "Untested"
    return round(pts, 1), tier, why


_ODDSMOVE_CACHE = {"mtime": None, "rows": {}}


def odds_move_note(p):
    """APEX-ODDSMOVE-CARD-2026-10-15 (owner: "this need to tell me on
    all board as well"). One card line when bet365's match odds moved since
    this prop was first scored: its Win Probability then -> now and the
    favorite's chance then -> now. Reads props_db.sqlite odds_moves (written
    by apex_odds_move_alert). Display only; None when nothing moved."""
    try:
        import sqlite3
        path = os.path.join(HERE, "data", "props_db.sqlite")
        m = os.path.getmtime(path)
        if _ODDSMOVE_CACHE["mtime"] != m:
            c = sqlite3.connect(path, timeout=5)
            rows = {}
            try:
                for r in c.execute("SELECT match_id, player, category, line, prop_type, ts, prob_prev, prob_new, "
                                   "fav_prev, fav_new, direction, prev_direction FROM odds_moves ORDER BY ts"):
                    rows.setdefault(tuple(str(x) for x in r[:5]), []).append(r[5:])
            finally:
                c.close()
            _ODDSMOVE_CACHE.update({"mtime": m, "rows": rows})
        k = tuple(str(p.get(x)) for x in ("match_id", "player", "category", "line", "prop_type"))
        mv = _ODDSMOVE_CACHE["rows"].get(k)
        if not mv:
            return None
        first, last = mv[0], mv[-1]
        d = str(p.get("direction") or "")
        p0 = first[1] if first[5] == d or first[5] in (None, "") else (1 - first[1] if first[1] is not None else None)
        p1 = _f(p.get("prob")) or last[2]
        if p0 is None or p1 is None or abs(p1 - p0) < 0.01:
            return None
        icon = "\U0001F4C9" if p1 < p0 else "\U0001F4C8"
        when = str(last[0])[11:16]
        try:
            from datetime import datetime as _omdt
            when = _omdt.fromisoformat(str(last[0])).strftime("%I:%M %p").lstrip("0")
        except Exception:
            pass
        return "%s Odds moved: %d%% \u2192 %d%% since first scored (bet365 favorite %.1f%% \u2192 %.1f%%, last move %s CT)" % (
            icon, round(100 * p0), round(100 * p1), 100 * first[3], 100 * last[4], when)
    except Exception:
        return None


def _discount_line(p):
    """APEX-DISCOUNT-2026-10-16: '🏷️ Discount play ...' when PrizePicks offered
    this prop as a discount (data/props_db.sqlite), else ''. Display only."""
    try:
        import apex_props_db as _pdb
        names = [p.get("player")] if p.get("player") != "match" else str(p.get("opponent") or "").split(" vs ")
        for n in names:
            t = _pdb.discount_tag(n, p.get("category"), p.get("line"))
            if t:
                return t
    except Exception:
        pass
    return ""


def projection_note(p):
    """APEX-ALLBOARDPROJ-2026-10-20 (owner, "go"): " · Apex projects **X**"
    for a board card's stat line -- line + edge, the same projected mean the
    All Props card shows (edge is stored projection-minus-line). Display only;
    empty when the pick has no edge."""
    try:
        return " \u00b7 Apex projects **%.1f**" % (float(p.get("line")) + float(p.get("edge")))
    except (TypeError, ValueError):
        return ""


def _render_pick_card(d, i, rec, _fs_first, _fs_last, medals, worst=False, show_debug=True, flag=None, score_dec=0):
    """APEX-BOARDPARITY-2026-10-05 (owner, Board Consistency fix): the
    one shared per-pick card renderer for every board's main ranked list.
    Extracted verbatim from best_bets_detail()'s own per-pick loop -- Top10,
    Top3, Goblin, Demon, and Standard already produced exactly this output;
    Elite6 is migrated onto this same function (see elite6_detail) so all six
    boards render one pick identically. Presentation only: takes the scored
    dict `d` it is handed and formats it -- computes no score, confidence,
    probability, or rank itself, and never touches `rec`/`scored`/selection.
    show_debug gates "Confidence factors" / "Supporting Signals" exactly as
    the old inline `not _is_top10_view` check did (Top10/Top3/Elite6 pass
    False; Goblin/Demon/Standard/category/worst pass True -- no visible
    change for any of them). Returns the list of lines for ONE pick."""
    L = []
    p, cat = d["p"], d["p"]["category"]
    cs = rec["cats"].get(cat) or _stat([])
    name = p["opponent"] if p.get("player") == "match" else p.get("player")
    vs = "" if p.get("player") == "match" else (" vs %s" % p["opponent"] if p.get("opponent") else "")
    l10 = "Last 10: %d\u2013%d" % cs["last10"] if cs["n"] else "no settled history"
    why_up = "; ".join(d.get("up", [])[:2]) or "highest evidence-weighted score available"
    why_dn = "; ".join(d.get("down", [])[:2]) or "outranked on evidence, no specific flags"
    L.append("\u2501" * 22)
    L.append("%s #%d \u00b7 %s%s%s%s" % (medals.get(i, "\U0001F538"), i, no_book_mark(p), name, vs,   # APEX-NOBOOK-2026-10-24
                                    " \u26a0\ufe0f BO5 SAMPLE" if p.get("bo5") else ""))
    L.append(start_time_line(p))   # APEX-STARTTIME-2026-10-22
    if _h2h_line(p):   # APEX-H2H-2026-10-22
        L.append(_h2h_line(p))
    _pr, _pc = float(p.get("prob") or 0), float(p.get("prob_cal", p.get("prob")) or 0)
    _dir_icon = "\U0001F4C8" if str(p.get("direction", "")).upper() == "MORE" else "\U0001F4C9"
    L.append("")
    L.append("\U0001F4CA %s %s %s %s%s" % (LABEL.get(cat, cat), p.get("line"), _dir_icon, p.get("direction"),
                                          projection_note(p)))   # APEX-ALLBOARDPROJ-2026-10-20
    if _discount_line(p):   # APEX-DISCOUNT-2026-10-16
        L.append(_discount_line(p))
    _stx = score_text(flag, p, score_dec)   # APEX-SCOREDISPLAY-2026-10-25: the board Score next to Win Prob
    # APEX-CARDWORDING-2026-11-01 (owner, "Go card wording"): lines must not read surer than the proof.
    # Picks and order don't change. Undo: CARD_WORDING = False.
    _fs_card = CARD_WORDING and cat == "1st_set_total_games"
    L.append("\U0001F3AF Win Probability: %s%s%s \u00b7 Apex Score: %d" % (
        ("%d%%" % round(_pr * 100)) if _pc >= _pr - 1e-9
        else ("%d%% (calibrated %d%%)" % (round(_pr * 100), round(_pc * 100))),
        " (same for every pick that passes this first-set rule)" if _fs_card else "",
        (" \u00b7 " + _stx) if _stx else "", d["sc"]))
    if p.get("apex_price_note"):   # APEX-PUBLISHPOOL-2026-10-11
        L.append(p["apex_price_note"])
    if p.get("apex_book_note"):    # APEX-BOOKLINES-2026-10-11: display only
        L.append(p["apex_book_note"])
    if str(p.get("prop_type") or "").lower() == "goblin" and GOBLIN_BOOKS_LINE:   # APEX-BOOKSSAY-2026-10-22: display only
        try:
            import apex_pinnacle as _pin
            _bk = _pin.books_line(p, _board_prob(p))
            if _bk:
                L.append(_bk)
        except Exception:
            pass
    if cat == "1st_set_total_games" and str(p.get("direction") or "").upper() == "LESS":   # APEX-FIRSTSETLESS-2026-11-04
        L.append("\U0001F9FE Real PrizePicks record: standard LESS, match total under 22: won 33, lost 12 (10/13-10/31, the days this rule was picked on)")
        L.append("\U0001F9FE Long-run record: won 3,128, lost 2,319 at a fixed line on older matches (weaker there)")
    elif cat == "1st_set_total_games":   # APEX-FIRSTSETSLIPS-2026-11-01
        L.append("\U0001F9FE Long-run record: " + ("over 7.5, favorite under 75: won 3,819, lost 509 in 2026 matches"
                 if float(p.get("line") or 0) < 8 else "strict over 8.5: won 1,133, lost 262 in 2026 matches"))
        if _fs_card:
            L.append("\U0001F9FE Real PrizePicks record: " + ("over 7.5: won 16, lost 0" if float(p.get("line") or 0) < 8
                     else "strict over 8.5: won 44, lost 11") + " (10/13-10/31, the days this rule was picked on)")
    elif REAL_RECORD_LINE:   # APEX-REALRECORD-2026-10-31: how this prop type did at this win chance, display only
        try:
            import apex_real_record as _rr
            _rl = _rr.line(p, _pr)
            if _rl:
                L.append(_rl)
        except Exception:
            pass
    _om = odds_move_note(p)        # APEX-ODDSMOVE-CARD-2026-10-15: display only
    if _om:
        L.append(_om)
    try:   # APEX-CARDSIGNALS-2026-10-21: the 5 strong outside signals, display only
        import apex_card_signals as _csig
        L.extend(_csig.lines(p))
        _nl = _csig.net_line(p)   # APEX-NETSIGNAL-2026-10-22
        if _nl:
            L.append(_nl)
        _l175 = _csig.line175_check(p)   # APEX-LINE175CHECK-2026-10-22: display only
        if _l175:
            L.append(_l175)
    except Exception:
        pass
    try:   # APEX-ACESCLOSE-2026-10-22: 🤝 players-close line on Aces MORE cards, display only
        import apex_aces_close_papertest as _acpt
        _acl = _acpt.card_line(p)
        if _acl:
            L.append(_acl)
    except Exception:
        pass
    if d.get("rerank_note"):   # APEX-RERANKLIVE-2026-10-22
        L.append(d["rerank_note"])
    if d.get("warn_note"):   # APEX-DEMONACESBREAKS-2026-10-22
        L.append(d["warn_note"])
    _hp = corrected_prob(_board_prob(p))   # APEX-HONESTPROB-2026-10-10: display only
    if cat == "1st_set_total_games":   # APEX-FIRSTSETSLIPS-2026-11-01: already its long-run record
        _hp = _board_prob(p)
    _thl = None
    try:   # APEX-HONEST-TENNIS-2026-11-07 (owner, "Go both"): per-type honest number, display only
        import apex_tennis_honest as _ath
        _thl = _ath.card_line(cat, p.get("prop_type"), p.get("direction"), _board_prob(p))
    except Exception:
        _thl = None
    if _fs_card:
        L.append("\U0001F9EE Honest Probability: none yet, Apex has no graded first-set picks of its own")
    elif _thl:
        L.append(_thl)
    elif _hp is not None:
        L.append("\U0001F9EE Honest Probability: %d%% _(Win Probability corrected by how past props at this level actually did -- shown for tracking, not used for ranking yet)_" % round(_hp * 100))
    L.append("")
    _att_icon = {"HIGH": "\U0001F7E2", "MEDIUM": "\U0001F7E1", "LOW": "\u26aa"}.get(d["lvl"], "\u26aa")
    if CARD_CLEANUP:   # APEX-CARDCLEANUP-2026-11-02: Attention didn't follow the ranking (LOW on the #1 pick)
        L.pop()   # its blank line goes with it
    elif CARD_WORDING:
        L.append("%s Attention: %s" % (_att_icon, d["lvl"]))
    else:
        L.append("\u2b50 Confidence: %d/100 \u00b7 Rank Score: %.1f" % (d.get("conf", 0), d.get("rank", 0.0)))
        L.append("\u2705 Evidence: %s \u00b7 %s Attention: %s" % (d["ev"], _att_icon, d["lvl"]))
    L.append("")
    L.append("\U0001F4DA Category: %s \u00b7 \U0001F553 %s" % (_rs(cs), l10))
    if show_debug and d.get("cwhy"):
        L.append("\U0001F440 Confidence factors: %s" % "; ".join(d.get("cwhy", [])[:3]))
    _frl = freshness_line(p, _fs_first, _fs_last)
    if _fs_card or CARD_CLEANUP:   # APEX-CARDCLEANUP-2026-11-02: every card reads the PrizePicks list
        try:
            import apex_two_pulls as _tp
            _frl = _tp.freshness_text(p) or _frl
        except Exception:
            pass
    L.append(_frl)
    try:   # APEX-LADDERALL-2026-11-12 (owner, "Go ladder all"): every line PrizePicks offers on this stat, on every board card
        import apex_card_extras as _ACE
        _lad = _ACE.ladder(p)
        if _lad: L.append(_lad)
    except Exception:
        pass
    L.append("")
    _bare = CARD_CLEANUP and all(x.startswith("stated ") for x in (d.get("up") or ["stated"])[:2])
    if worst or not CARD_WORDING or ("evidence-weighted" not in str(why_up) and not _bare):
        L.append(("\U0001F9E0 Why ranked low: %s" % why_dn) if worst else ("\U0001F9E0 Why Apex Likes It: %s" % why_up))
    if show_debug and d.get("more"):
        L.append("\U0001F680 Supporting Signals: " + " \u00b7 ".join("%s %s %s (%d)" % (LABEL.get(m["p"]["category"], m["p"]["category"]),
                 m["p"]["line"], m["p"]["direction"], m["sc"]) for m in d["more"][:4]))
    _lmr = long_match_risk(p)
    if str(p.get("direction", "")).upper() == "LESS" and cat in ("dfs", "aces", "breaks") and _lmr is not None:
        L.append("%s long-match risk %d%% -- a LESS prop here is a bet the match stays short%s" % (
            "\u26a0\ufe0f" if _lmr >= LONG_MATCH_LESS_MAX else "\u2139\ufe0f", round(_lmr * 100),
            " (kept off the slip)" if _lmr >= LONG_MATCH_LESS_MAX else ""))
    if not worst and d["sc"] < 50:
        L.append("\u26a0\ufe0f below the 50 bar -- best available, not a strong bet")
    return L


# APEX-BOARDAUDIT-2026-10-19 (owner, "Go"): the Best Goblin/Demon boards no
# longer cap a category at 2 picks -- the cap held back 80-83% Total Games goblins while
# 78% Fantasy Score props filled in (seen 10/13 and again 10/19). One per match stays.
# Set back to 2 to restore the old cap.
BEST_BOARD_CATEGORY_CAP = None
GOBLIN_SHOW_ALL_FIX = True
CARD_WORDING = True   # APEX-CARDWORDING-2026-11-01
CARD_CLEANUP = True   # APEX-CARDCLEANUP-2026-11-02 (owner, "Go card clean up"); undo: False
BEST_GOBLIN_SHOW_ALL_MIN_PROB = 0.75   # APEX-GOBLIN75-2026-10-19; None = back to a fixed 6


def best_bets_detail(picks, category=None, worst=False, rec=None, limit=15, prop_type=None,
                     slip_rules=False, return_scored=False, exclude_fantasy_score=False,
                     slip_preview=False):
    """APEX-BB-002a: on-demand detail view. True score order (Bo5 eligible
    anywhere per spec, always tagged). worst=True ranks ascending.

    slip_preview (APEX-BOARDPARITY-2026-10-05, owner, Board
    Consistency fix): Top10/Top3 enablement -- runs the IDENTICAL
    same-match / category-cap check that slip_rules already runs for
    Best Goblin/Demon/Standard, and reports any violations in the same
    Held Back section with the same reasons, but never removes anything
    from `scored` -- the ranked list a slip_preview caller sees is
    untouched. slip_rules (Best-Type boards) still both filters AND
    reports, exactly as before. Mutually additive, not a replacement.

    exclude_fantasy_score (APEX-FS-EXCLUSION-2026-09-30): when True, drops
    fantasy_score props from the candidate pool BEFORE scoring/ranking --
    approved pending recalibration (MAE ~9, see 2026-09-29 forensic audit).
    Data collection, logging, settlement and ALIS tracking are untouched;
    this only removes fantasy_score from THIS function's output/ranking."""
    try:
        from datetime import date as _d
        rec = rec or records()
        # APEX-HELDBACKFIRST-2026-10-01: loaded here (was loaded further
        # down, after the empty-board early returns) so the Held Back
        # section on an EMPTY board can also show "Previously shown on
        # prior fetch" -- cached by file mtime, so moving it earlier costs
        # nothing on a board that already has picks.
        _fs_first, _fs_last, _ = _load_freshness_index()
        # APEX-LEGACYPICKS-2026-10-05 (Phase 2): which board-membership
        # flag (if any) this specific call corresponds to -- only the 5
        # boards best_bets_detail serves get a Legacy Picks section;
        # /allpicks, worst-mode, and single-category views get none
        # (no matching board flag to judge "still available" against).
        _legacy_flag = None
        if not worst:
            if prop_type in ("goblin", "demon", "standard"):
                _legacy_flag = "best" + prop_type
            elif prop_type is None and category is None:
                if limit == 10:
                    _legacy_flag = "top10"
                elif limit == 3:
                    _legacy_flag = "top3"
        # APEX-HELDBACKFIELDS-2026-10-05 (Phase 3): human label for
        # "Original Board" on this call's held-back entries.
        if worst:
            _board_label = "Low Priority / Worst"
        elif category:
            _board_label = "Best %s" % LABEL.get(category, category)
        elif prop_type in ("goblin", "demon", "standard"):
            _board_label = "Best %s" % str(prop_type).capitalize()
        elif prop_type is None and category is None and limit == 10:
            _board_label = "Top10"
        elif prop_type is None and category is None and limit == 3:
            _board_label = "Top 3"
        else:
            _board_label = "All Picks"
        if category:
            picks = [p for p in picks if p.get("category") == category]
            if not picks:
                return "no active %s props on the current board" % LABEL.get(category, category)
        # APEX-TYPEDETAIL-FILTER-2026-09-28: this filter existed in
        # best_bets_block() but was missing here, so /goblin, /standard and
        # /demon (all routed through this function) ranked the same
        # unfiltered board and could return identical top picks. Same
        # pattern as best_bets_block(): prop_type has been logged per-row
        # since 2026-09-08; rows evaluated earlier carry none and are
        # simply absent from a type view.
        if prop_type:
            picks = [p for p in picks if str(p.get("prop_type") or "").lower() == str(prop_type).lower()]
            if not picks:
                return ("No %s props on the current board yet. (Prop type has been logged since "
                        "2026-09-08 -- boards evaluated before then carry no type.)" % prop_type)
            # APEX-BEST-ELIGIBILITY-FILTER-2026-09-28: Best Goblin / Best
            # Standard specifically -- a below-floor prop stays on the
            # regular board (Picks Channel) but cannot become THIS.
            _before_elig = len(picks)
            picks = [p for p in picks if _best_eligible(p, prop_type)]
            if not picks:
                return ("No %s props currently clear the Best %s threshold (%d prop%s below floor). "
                        "Still visible in the main Picks Channel." %
                        (prop_type, str(prop_type).capitalize(), _before_elig,
                         "" if _before_elig == 1 else "s"))
        # APEX-FS-EXCLUSION-2026-09-30: pull fantasy_score out of the candidate
        # pool for the callers that pass exclude_fantasy_score=True. This runs
        # on `picks` (the raw board), before ANY scoring/ranking, so an excluded
        # prop cannot influence dedup, slip rules, or the score floor either.
        # Does NOT touch PICK_LOG / the forward log / ALIS -- those are separate
        # read paths untouched by this function.
        _fs_excluded_n = 0
        if exclude_fantasy_score and not BOARDS_INCLUDE_FANTASY_SCORE:   # APEX-FSRETURN-2026-10-11
            _before_fs = len(picks)
            picks = [p for p in picks if p.get("category") != "fantasy_score"]
            _fs_excluded_n = _before_fs - len(picks)
        scored = []
        _gate_why = {"unproven": 0, "excluded": 0, "below_gate": 0}   # APEX-OPS-2026-10-10: honest empty-board reason
        for p in picks:
            if not p.get("category"):
                continue
            if not _category_has_evidence(p["category"], rec):   # APEX-CAT-GATE-2026-09-27
                _gate_why["unproven"] += 1
                continue
            if not _category_probability_gate(p["category"], p.get("prob_cal", p.get("prob"))):   # APEX-CATGATE-2026-10-07 + APEX-ACESLESS-GATEFIX-2026-10-07
                _gate_why["excluded" if (p["category"] in BOARD_EXCLUDED_CATEGORIES or p["category"] in DISABLED_CATEGORIES) else "below_gate"] += 1
                continue
            sc, ev, lvl, up, down = apex_score(p.get("prob_cal", p.get("prob")), p["category"], bool(p.get("bo5")), rec)   # APEX-ACESLESS-GATEFIX-2026-10-07
            try:
                import apex_card_index as _CI
                # APEX-MATCHCARD-2026-08-31: match rows key on the pair
                if (p.get("player") or "") == "match":
                    _card = _CI.lookup_match(str(p.get("opponent") or ""), p["category"], str(p.get("match_date") or ""))[0]
                else:
                    _card = _CI.lookup(str(p.get("player") or ""), p["category"], str(p.get("match_date") or ""))[0]
            except Exception: _card = None
            cf, ctier, cwhy = apex_confidence(p["category"], bool(p.get("bo5")), rec,
                                              score=sc, prob=p.get("prob"), evidence=ev, attention=lvl,
                                              warnings=down, card_present=bool(_card))
            scored.append({"p": p, "sc": sc, "score": sc, "ev": ev, "lvl": lvl,
                           "up": up, "down": down, "conf": cf, "cwhy": cwhy,
                           "rank": rank_score(sc, cf)})
        if not scored:
            # APEX-PERBOARD-2026-10-01: this is the true zero-candidate case --
            # no prop on this board (post prop_type/eligibility filtering) had
            # a category with enough settled evidence to even compute an Apex
            # Score, so there is no held_back data yet to pull a Closest To
            # Qualifying list from (that would mean fabricating scores). Says
            # so plainly instead of the old bare "no rankable picks" line.
            _board_name = ("%s props" % str(prop_type).capitalize()) if prop_type else (
                "%s props" % LABEL.get(category, category)) if category else "the current board"
            # APEX-OPS-2026-10-10 (owner): the old text always blamed
            # "not enough settled evidence", even when the real reason was that
            # no prop reached its category's minimum probability. Now says
            # exactly why, with counts.
            _n_in = sum(_gate_why.values())
            if _n_in == 0:
                if prop_type:
                    return ("No %s qualify today -- none reached Win Probability 70%% "
                            "(Best %s only takes props at 70%%+)." % (_board_name, str(prop_type).capitalize()))
                return "No %s on the current board today." % _board_name
            _why = []
            if _gate_why["below_gate"]:
                _why.append("%d below their category's minimum probability" % _gate_why["below_gate"])
            if _gate_why["excluded"]:
                _why.append("%d in a category kept off boards (Breaks)" % _gate_why["excluded"])
            if _gate_why["unproven"]:
                _why.append("%d in a category without enough settled results yet" % _gate_why["unproven"])
            return ("No %s qualify today -- none reached Win Probability 70%% and Apex Score 67. "
                    "Of %d candidate(s): %s." % (_board_name, _n_in, "; ".join(_why)))
        _legacy_d = sorted(scored, key=lambda d: d["sc"] if worst else -d["sc"])
        _legacy_pos = {}
        for _i, _ld in enumerate(_legacy_d, 1):
            _legacy_pos[id(_ld)] = _i
        scored.sort(key=lambda d: _bb_sortkey(d, worst))   # APEX-BB3
        # APEX-BB-002e: two-tier dedup. One prop per player (PrizePicks rule);
        # a match prop competes against BOTH its players and takes a numbered
        # slot only if it outranks each player's best prop. Skipped in worst
        # mode -- every weak pick deserves its own warning there.
        if not worst:
            import unicodedata as _ud
            def _norm(s):
                s = _ud.normalize("NFKD", str(s or ""))
                s = "".join(c for c in s if not _ud.combining(c))
                return s.casefold().replace("'", "").replace(".", "").replace("-", " ")
            # APEX-SLOTQUAL-2026-10-12 (owner, "Yes, go"): a prop that fails the
            # board bar (70% / Apex 67) no longer claims a player's or match's slot ahead
            # of one that passes -- it used to win the slot, get cut by the floor, and take
            # the qualifier with it (Yastremska Aces 0.5 at 84%, Apex 50, hid her TG 17.5
            # at 82%, Apex 85). Qualifiers go first; order within each group unchanged.
            def _slot_beats(a, b):
                if _fantasy_tg_exception(a, b):   # APEX-FANTASYTG-2026-10-31
                    return True
                qa, qb = _board_qualifies(a), _board_qualifies(b)
                return qa and not qb or (qa == qb and _slot_key(a) < _slot_key(b))   # APEX-BESTLINE-2026-10-25
            scored.sort(key=lambda d: (not _board_qualifies(d), _slot_key(d)))
            _pslots, _prim, _match_rows = {}, [], []
            for d in scored:
                if (d["p"].get("player") or "") == "match":
                    _match_rows.append(d)
                    continue
                _pk = _norm(d["p"].get("player")) + "|" + _norm(d["p"].get("opponent"))
                if _pk in _pslots:
                    _pslots[_pk].setdefault("more", []).append(d)
                else:
                    _pslots[_pk] = d; _prim.append(d)
            for md in _match_rows:
                _sides = [_norm(x) for x in
                          (md["p"].get("opponent") or "").replace(" vs ", " v ").split(" v ")]
                _hits = []
                for _pk, _slot in _pslots.items():
                    _pname = _pk.split("|")[0]
                    for _sd in _sides:
                        if _sd and all(t in _pname.split() for t in _sd.split()):
                            _hits.append(_slot); break
                if not _hits:
                    _prim.append(md)                      # standalone: no player props
                elif all(_slot_beats(md, h) for h in _hits):   # APEX-PROBRANK-2026-10-10: was RANK; APEX-SLOTQUAL-2026-10-12
                    for h in _hits:                        # match prop wins the slot
                        if h in _prim:                     # APEX-BB-002h: a prior match
                            _prim.remove(h)                # prop may already have claimed it
                            md.setdefault("more", []).append(h)
                            md["more"].extend(h.pop("more", []))
                        else:
                            for w in _prim:                # attach to whoever holds it now
                                if h in w.get("more", []):
                                    w.setdefault("more", []).append(md); break
                            else:
                                md.setdefault("more", []).append(h)
                    if md not in _prim and not any(md in w.get("more", []) for w in _prim):
                        _prim.append(md)
                else:
                    for h in _hits:                        # match prop loses
                        h.setdefault("more", []).append(md)
            # APEX-BB3: RANK SCORE order -- supersedes the CONF-001 8-point banding
            _prim.sort(key=_slot_key)   # APEX-BESTLINE-2026-10-25 (= _bb_sortkey when off)
            scored = _prim
            scored = _demote_bo5_from_top(scored)   # APEX-BB-BO5TOP-2026-09-26: board-governance, not scoring
        # APEX-HELDBACK-2026-10-01: Held Back Transparency (owner,
        # 2026-10-01 audit). Every prop that makes it this far has a real
        # rank and score -- capture both NOW, before any floor/cap below
        # removes it, so a held-back prop can be shown as "Rank #3, Score 84,
        # Held: <reason>" instead of silently vanishing. Purely observational:
        # does not change what gets filtered, only what gets reported.
        _orig_rank = {id(d): i for i, d in enumerate(scored, 1)}
        _held_back = []
        if not worst:   # APEX-BLOWOUT-2026-10-22: low-cushion TG Goblins take no numbered slot
            scored, _lc_held = _low_cushion_split(scored, _orig_rank)
            _held_back.extend(_lc_held)
        # APEX-BESTFLOOR-NOPADDING-2026-09-29: a Best-[Type] view must never pad to
        # `limit` with weak picks just to fill six slots. Strong/Proven evidence is
        # required for all three types here, AND apex_score's own break-even bar
        # (score > 50 -- the exact number this same function already labels "below
        # the 50 bar" a few lines below when NOT filtered) is required for all three
        # types too. The pre-existing _best_eligible() probability floor (Goblin
        # 72.1% / Standard 70.0%, openclaw_core.py APEX-FLOOR-TO-ELIGIBILITY-2026-09-28)
        # still runs earlier in this function, but it treats a missing/blank logged
        # flag as eligible by design (never excludes on absence) -- confirmed live on
        # 2026-09-29 that this let a 54%-probability, score-47 Standard pick (Tommy
        # Paul vs Tabilo, no logged eligibility flag) through as a #6 slot-filler.
        # The score>50 floor here closes that gap for all three types, not just
        # Demon (which never had ANY floor -- no verified Demon payout break-even
        # exists anywhere in this codebase, confirmed absent, not assumed).
        _floor_dropped = 0
        if prop_type and not worst:
            _before_floor = len(scored)
            _keep50, _drop50 = [], []
            for d in scored:
                (_keep50 if (d["ev"] in ("Strong", "Proven") and d["sc"] > 50) else _drop50).append(d)
            for d in _drop50:   # APEX-HELDBACKAUDIT-2026-10-01
                # All-Board Held-Back Classification Audit (owner,
                # 2026-10-01): a prop dropped for evidence below Strong/Proven,
                # or for scoring at/below the 50 break-even bar, FAILED
                # qualification -- it never passed the evidence/break-even
                # gate in the first place, so by definition it cannot be a
                # true held-back prop (held back = would otherwise qualify,
                # removed only for duplicate/cap/limit/slot reasons). Not
                # added to _held_back; never shown in the Held Back section.
                pass
            scored = _keep50
            _floor_dropped = _before_floor - len(scored)
        # APEX-BESTFLOOR70-2026-09-30 (Best List Quality Floor): a SEPARATE,
        # additive gate on top of the evidence/50-bar floor above. Only runs
        # when prop_type is set -- that is exactly Best Demon/Goblin/Standard
        # (/top10, /elite6, /allpicks all call this function with
        # prop_type=None and never reach this block). Removes nothing from
        # apex_score(), apex_confidence(), ranking order, Elite6, or Top10 --
        # a sub-70 pick simply isn't ELIGIBLE for a Best-[Type] list; it stays
        # fully visible on the main board / Picks Channel / Top10 / Elite6.
        _qfloor_dropped = 0
        _demon_watchlist = []   # APEX-DEMONWATCHLIST-2026-10-01: Demon-only near-misses
        _this_floor = _quality_floor(prop_type)
        if prop_type and not worst:
            _before_qfloor = len(scored)
            _keepq, _dropq = [], []
            for d in scored:
                (_keepq if (d["sc"] >= _this_floor and _board_prob(d) >= BOARD_MIN_PROB - 1e-9) else _dropq).append(d)   # APEX-PROBRANK-2026-10-10
            for d in _dropq:   # APEX-HELDBACKAUDIT-2026-10-01
                # All-Board Held-Back Classification Audit (owner,
                # 2026-10-01): failing the board's own quality floor is
                # FAILED QUALIFICATION ("board floor" is explicitly listed
                # as a failed-qualification reason, not a held-back one) --
                # not added to _held_back. Demon Watchlist (below) remains
                # its own explicitly-labeled section and is unaffected.
                # APEX-PERBOARD-2026-10-01: Demon Watchlist now shows ALL
                # Demon props below the quality floor, not just near-misses
                # within 10 points (that was the old APEX-DEMONWATCHLIST-
                # 2026-10-01 window, superseded by owner' Per-Board
                # Transparency Overhaul). The 50-point evidence floor above
                # already excludes anything below 50, so this is genuinely
                # every eligible-but-sub-floor Demon prop.
                if str(prop_type).strip().lower() == "demon":
                    _demon_watchlist.append(d)
            scored = _keepq
            _qfloor_dropped = _before_qfloor - len(scored)
            if not scored:
                # APEX-HELDBACKFIRST-2026-10-01: "Closest To Qualifying" is
                # retired as the empty-board fallback -- replaced with the
                # same Held Back From This Board section every non-empty
                # board already shows (from the SAME _held_back list this
                # function already built), so the user sees real backup
                # options instead of near-misses that never qualified.
                # Demon Watchlist is explicitly kept too, even on an empty
                # board, since it's the same qfloor-dropped Demon props.
                msg_lines = ["No new qualifying picks available."]
                if _demon_watchlist:
                    msg_lines.append("\u2500" * 30)
                    msg_lines.extend(_demon_watchlist_lines(_demon_watchlist, _orig_rank, _this_floor))
                msg_lines.append("\u2500" * 30)
                msg_lines.extend(_held_back_section_lines(_held_back, _fs_first, _fs_last, board_label=_board_label))
                msg = "\n".join(msg_lines)
                if return_scored:
                    return {"text": msg, "scored": [], "held_back": _held_back, "watchlist": _demon_watchlist}
                return msg
        # APEX-TOP3TOP10FLOOR-2026-10-05 (Phase 6): the Top3/Top10 analog of
        # the _qfloor_dropped block above -- only runs for the actual Top3
        # (limit==3) / Top10 (limit==10) calls (prop_type=None, category=
        # None, not worst; /allpicks, /elite6 and every Best-[Type]/category
        # view are untouched). Never pads back up to `limit` with a sub-70
        # pick -- a short list is reported honestly, same as every other
        # floor in this function.
        _top_floor_dropped = 0
        _top_floor = None
        if not worst and prop_type is None and category is None and limit in (3, 10):
            _top_floor = TOP3_FLOOR if limit == 3 else TOP10_FLOOR
            _before_topfloor = len(scored)
            _keept, _dropt = [], []
            for d in scored:
                (_keept if (d["sc"] >= _top_floor and _board_prob(d) >= BOARD_MIN_PROB - 1e-9) else _dropt).append(d)   # APEX-PROBRANK-2026-10-10
            for d in _dropt:   # APEX-HELDBACKAUDIT-2026-10-01 pattern: failed
                # qualification, not held-back (never passed the board's own
                # quality floor in the first place).
                pass
            scored = _keept
            _top_floor_dropped = _before_topfloor - len(scored)
            if not scored:   # APEX-TOP3TOP10FLOOR-2026-10-05: same honest-empty
                # pattern as the prop_type qfloor block above -- never pad,
                # show real backup options via Held Back instead.
                msg_lines = ["No picks clear the Top%d bar (Win Probability 70%%+ and Apex Score %d+) today -- "
                             "%d prop(s) below bar omitted, never padded to reach %d."
                             % (limit, _top_floor, _top_floor_dropped, limit)]
                msg_lines.append("─" * 30)
                msg_lines.extend(_held_back_section_lines(_held_back, _fs_first, _fs_last, board_label=_board_label))
                msg = "\n".join(msg_lines)
                if return_scored:
                    return {"text": msg, "scored": [], "held_back": _held_back, "watchlist": []}
                return msg
        if worst:
            head = "\u26a0\ufe0f **LOW PRIORITY PICKS -- %s** (weakest first)" % _d.today().isoformat()
        elif category:
            head = "\U0001F3C6 **BEST %s PROPS TODAY -- %s**" % (_BB_CAT_TITLE.get(category, category.upper()), _d.today().isoformat())
        elif prop_type:
            head = "\U0001F3C6 BEST %s PROPS TODAY -- %s\n(%d props on the board)" % (
                str(prop_type).upper(), _d.today().isoformat(), len(scored))
        else:
            # APEX-BESTBETSAUDIT-2026-10-05 (owner): this branch is
            # reached by exactly one caller -- /top10 (limit=10, no
            # prop_type/category/worst) -- but the old header called it
            # "APEX BEST BETS TODAY ... entire evaluated board, N picks",
            # which misleadingly implied it was a separate, full-board
            # developer dump (N = the whole floor-passing pool, not the 10
            # actually shown). It is /top10's real output under a wrong
            # label. Corrected to say what it actually is.
            head = "\U0001F3C6 **TOP %d -- %s** (%d cleared the quality floor, showing top %d)" % (
                limit, _d.today().isoformat(), len(scored), min(limit, len(scored)))
        # APEX-BESTBETSAUDIT-2026-10-05: true exactly for the plain Top10
        # call (no worst/category/prop_type) -- the one this audit targets.
        _is_top10_view = not worst and not category and not prop_type
        # APEX-TYPEVIEW-RULES-2026-09-18: a prop-type view is a SLIP, so it obeys the slip
        # rules -- one pick per match, max 2 per category. Dropped picks are listed below.
        # APEX-BOARDPARITY-2026-10-05: slip_preview runs the exact same check for
        # Top10/Top3 but only to REPORT into Held Back -- `scored` is rebuilt from
        # the untouched pre-check list right after, so the ranked list those two
        # boards show is byte-for-byte what it was before this flag existed.
        _held = []
        if (slip_rules or slip_preview) and not worst:
            _pre_slip_scored = scored
            def _slip_pass(pool):
                _keep, _seen_m, _catn, _hld = [], set(), {}, []
                for _d in pool:
                    _p = _d["p"]; _c = _d.get("cat") or _p.get("category", "")
                    _mid = _p.get("match_id", "")
                    if _mid and _mid in _seen_m:
                        _hld.append((_d, "same match as a pick already on this slip")); continue
                    if BEST_BOARD_CATEGORY_CAP and _catn.get(_c, 0) >= BEST_BOARD_CATEGORY_CAP:
                        _hld.append((_d, "category cap: %s already x%d on this slip" % (_c, BEST_BOARD_CATEGORY_CAP))); continue
                    _seen_m.add(_mid); _catn[_c] = _catn.get(_c, 0) + 1; _keep.append(_d)
                return _keep, _hld
            _keep, _held = _slip_pass(scored)
            # APEX-CAPALT-2026-10-12 (owner, "Yes"): a prop held back by the
            # category cap held its player's/match's slot, so its alternates (other
            # props of the same player or match, in "more") vanished with it --
            # Kostyuk FS 4.5 (75%) behind her capped Total Games. Those alternates
            # that clear the bar now compete in a second pass, in rank order.
            _alts = []
            for _d, _why in _held:
                if not _why.startswith("category cap"):
                    continue
                _stack = list(_d.get("more") or [])
                while _stack:
                    _x = _stack.pop(0); _stack.extend(_x.get("more") or [])
                    if _board_qualifies(_x) and (not prop_type or _x["sc"] >= _this_floor) and _x not in _alts:
                        _alts.append(_x)
            if _alts:
                _keep, _held = _slip_pass(sorted(list(scored) + _alts, key=lambda d: _bb_sortkey(d, worst)))
            scored = _keep if slip_rules else _pre_slip_scored
            for _d, _why in _held:   # APEX-HELDBACK-2026-10-01: fold into the unified list
                _held_back.append({"rank": _orig_rank.get(id(_d)), "score": _d["sc"], "p": _d["p"], "conf": _d.get("conf"), "reason": _why})
        # APEX-GOBLIN75-2026-10-19 (owner): Best Goblin shows every goblin at Win
        # Probability 75%+ (one per match), never fewer than its normal 6.
        if prop_type == "goblin" and slip_rules and not worst and BEST_GOBLIN_SHOW_ALL_MIN_PROB:
            limit = max(limit, sum(1 for _d in scored if _board_prob(_d["p"]) >= BEST_GOBLIN_SHOW_ALL_MIN_PROB - 1e-9))
        medals = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}
        # APEX-FRESHNESS-2026-09-29: board-level sync status (index itself
        # now loaded earlier -- see APEX-HELDBACKFIRST-2026-10-01 above).
        L = [head, sync_status_line()]
        _fs_banner = sync_warning_banner()
        if _fs_banner:
            L.append(_fs_banner)
        if _floor_dropped:   # APEX-BESTFLOOR-NOPADDING-2026-09-29
            L.append("(%d prop(s) below the Best %s quality bar omitted -- never padded to reach %d)"
                     % (_floor_dropped, str(prop_type).capitalize(), limit))
        if _top_floor is not None:   # APEX-TOP3TOP10FLOOR-2026-10-05 (Phase 6)
            L.append("\U0001F6A6 Top%d Qualifies at: Win Probability %d%%+ and Apex Score %d+ -- ranked by Win Probability" % (limit, round(BOARD_MIN_PROB * 100), _top_floor))
            if _top_floor_dropped:
                L.append("(%d prop(s) below the Top%d quality bar omitted -- never padded to reach %d)"
                         % (_top_floor_dropped, limit, limit))
        if prop_type and not worst:   # APEX-BESTFLOOR70-2026-09-30 / APEX-DEMONFLOOR60-2026-10-01
            L.append("\U0001F6A6 %s Qualifies at: Win Probability %d%%+ and Apex Score %d+ -- ranked by Win Probability" % (str(prop_type).capitalize(), round(BOARD_MIN_PROB * 100), _this_floor))
            if _qfloor_dropped:
                L.append("\U0001F6A6 %d %s prop(s) excluded for failing the %d+ floor"
                         % (_qfloor_dropped, str(prop_type).capitalize(), _this_floor))
            if _demon_watchlist:   # APEX-HELDBACKFIRST-2026-10-01 (keep Demon Watchlist)
                L.append("\u2500" * 30)
                L.extend(_demon_watchlist_lines(_demon_watchlist, _orig_rank, _this_floor))
        if _fs_excluded_n:   # APEX-FS-EXCLUSION-2026-09-30
            L.append("(%d Fantasy Score prop(s) excluded from this ranking -- pending recalibration, "
                      "still scraped/logged/settled/tracked)" % _fs_excluded_n)
        for i, d in enumerate(scored[:limit], 1):
            if i == 7 and prop_type == "goblin" and slip_rules and not worst:
                # APEX-GOBLIN75-2026-10-19 (owner, "at least as a back up"): picks past
                # the main 6 are listed as backups -- every other 75%+ goblin.
                L.append("\u2500" * 30)
                L.append("\U0001F501 **BACKUPS** -- every other Goblin at 75%+ (one per match)")
            # APEX-BOARDPARITY-2026-10-05: per-pick rendering factored out into
            # the shared _render_pick_card() helper -- Elite6 now calls the same
            # helper (see elite6_detail), so all six boards render one pick
            # identically. show_debug=not _is_top10_view reproduces the exact
            # Best-Bets-Today-audit gating that lived inline here before.
            L.extend(_render_pick_card(d, i, rec, _fs_first, _fs_last, medals,
                                        worst=worst, show_debug=not _is_top10_view))
        if len(scored) > limit:
            L.append("\u2500" * 30)
            L.append("(+%d more on the board -- %d%% probability and below)" % (len(scored) - limit, round(_board_prob(scored[limit]) * 100)))
        L.append("_Ranked by Win Probability. Apex Score is a pass/fail check (67+); Confidence and Rank Score are shown for reference only._")   # APEX-PROBRANK-2026-10-10
        if _held_back:                             # APEX-HELDBACKFIRST-2026-10-01 (was APEX-PERBOARD-2026-10-01)
            L.append("\u2500" * 30)
            L.extend(_held_back_section_lines(_held_back, _fs_first, _fs_last, board_label=_board_label))
        if _legacy_flag:   # APEX-LEGACYPICKS-2026-10-05 (Phase 2) -- own section, never merged with Held Back
            L.extend(_legacy_picks_section_lines(_legacy_picks_for_board(_legacy_flag, scored[:limit])))
        L.append("_Deterministic read of the current evaluated board -- no re-evaluation._")
        try:                                   # APEX-SLIP-V11: three slips, fail-open
            _sl, _maps = _slip_sections(scored, picks); L.extend(_sl)
        except Exception:
            _maps = {}
        _bb_rank_log(scored, _legacy_pos, "detail", slip=_maps.get("mixed"), slip_std=_maps.get("standard"), slip_gob=_maps.get("goblin"))
        if return_scored:
            # APEX-BOARDCAPLIMIT-2026-10-05: `scored` at this point is the
            # FULL post-dedup/slip-rules candidate pool, not the top
            # `limit` actually shown in the text above (that view-only
            # slice happens a few lines up, in the `for i, d in
            # enumerate(scored[:limit], 1)` loop -- it never touched the
            # `scored` variable itself). Returning the untruncated list
            # here silently broke every caller that uses return_scored to
            # learn "what's actually on this board" -- in particular
            # capture_board_membership(), which calls this for Top10 (limit
            # 10), Top3 (limit 3), Best Goblin/Demon/Standard (limit 6
            # each) and was recording the ENTIRE scored pool as board
            # membership for all five, not the real top N. elite6_detail()
            # does not have this bug (it truncates `elite` itself inside
            # the ranking loop). Confirmed live during the 2026-10-05 Board
            # Scorecard Settlement Audit: Top3 and Top10 membership keys
            # were identical in size (210 of 213 replayed rows) -- the
            # unmistakable signature of this bug, not a data issue.
            return {"text": "\n".join(L), "scored": scored[:limit], "held_back": _held_back, "watchlist": _demon_watchlist}
        return "\n".join(L)
    except Exception as e:
        if return_scored:
            return {"text": "best bets unavailable (%s)" % type(e).__name__, "scored": [], "held_back": [], "watchlist": []}
        return "best bets unavailable (%s)" % type(e).__name__



# ================================================================ APEX-ELITE6-2026-09-29
def elite6_detail(picks, rec=None, limit=6, return_scored=False, exclude_fantasy_score=False):
    """Apex's primary recommendation command: the 6 strongest plays on the
    ENTIRE evaluated board, with NO category quota -- Demon/Goblin/Standard
    compete on equal footing, exactly as strong as the board makes them.

    exclude_fantasy_score (APEX-FS-EXCLUSION-2026-09-30): when True, drops
    fantasy_score props from the candidate pool BEFORE scoring/ranking --
    approved pending recalibration (MAE ~9, see 2026-09-29 forensic audit).
    Data collection, logging, settlement and ALIS tracking are untouched;
    this only removes fantasy_score from THIS function's output/ranking.

    Candidate pool: picks the caller already loaded from load_current_board()
    (already playable-only -- direction-locked/unplayable, already-started and
    already-graded rows are dropped there, before this function ever sees
    them) + evidence-gated (_category_has_evidence) + Strong/Proven evidence
    only.

    Ranking: apex_score (already bakes in probability, category calibration/
    history, and the Attention edge-zone bonus -- HIGH/MEDIUM Attention is
    NOT excluded here, per the 2026-09-29 forensic audit that found it is a
    proven-edge bonus, not a risk flag) x confidence, via the same rank_score
    used everywhere else on the board. Sort order matches the live production
    sort (APEX-REVERT-2026-09-26: score, then rank, then probability).

    Dedup: one slot per player, one slot per match -- a second line on a
    match already holding a slot, or a second prop on a player already
    holding a slot, is skipped and counted, never silently swapped in. This
    command never pads to `limit`; if fewer than `limit` picks clear the bar,
    it says so and shows what's actually there."""
    try:
        from datetime import date as _d
        rec = rec or records()
        # APEX-LEGACYPICKS-2026-10-05 (Phase 2): Elite6 always has exactly
        # one board-membership flag -- unlike best_bets_detail, no call-site
        # branching needed.
        _legacy_flag = "elite6"
        # APEX-HELDBACKFIRST-2026-10-01: loaded here (was loaded further
        # down, after the empty-board early return) so an EMPTY Elite6
        # board's Held Back section can also show "Previously shown on
        # prior fetch" -- cached by file mtime, costs nothing extra.
        _fs_first, _fs_last, _ = _load_freshness_index()
        playable_n = len(picks)
        # APEX-FS-EXCLUSION-2026-09-30: runs on the raw pool, before scoring,
        # so an excluded prop can't affect dedup or ranking. playable_n above
        # stays the true board size; this count is reported separately.
        _fs_excluded_n = 0
        if exclude_fantasy_score and not BOARDS_INCLUDE_FANTASY_SCORE:   # APEX-FSRETURN-2026-10-11
            _before_fs = len(picks)
            picks = [p for p in picks if p.get("category") != "fantasy_score"]
            _fs_excluded_n = _before_fs - len(picks)
        scored = []
        for p in picks:
            if not p.get("category"):
                continue
            if not _category_has_evidence(p["category"], rec):
                continue
            if not _category_probability_gate(p["category"], p.get("prob_cal", p.get("prob"))):   # APEX-CATGATE-2026-10-07 + APEX-ACESLESS-GATEFIX-2026-10-07
                continue
            sc, ev, lvl, up, down = apex_score(p.get("prob_cal", p.get("prob")), p["category"], bool(p.get("bo5")), rec)   # APEX-ACESLESS-GATEFIX-2026-10-07
            try:
                import apex_card_index as _CI
                if (p.get("player") or "") == "match":
                    _card = _CI.lookup_match(str(p.get("opponent") or ""), p["category"], str(p.get("match_date") or ""))[0]
                else:
                    _card = _CI.lookup(str(p.get("player") or ""), p["category"], str(p.get("match_date") or ""))[0]
            except Exception:
                _card = None
            cf, ctier, cwhy = apex_confidence(p["category"], bool(p.get("bo5")), rec,
                                               score=sc, prob=p.get("prob"), evidence=ev, attention=lvl,
                                               warnings=down, card_present=bool(_card))
            rs = rank_score(sc, cf)
            ptype = str(p.get("prop_type") or p.get("prop_type_classified") or "untyped").lower()
            scored.append({"p": p, "cat": p["category"], "sc": sc, "score": sc, "ev": ev, "lvl": lvl,
                           "conf": cf, "cwhy": cwhy, "rank": rs, "ptype": ptype, "up": up, "down": down})
        rankable_n = len(scored)

        strong = [d for d in scored if d["ev"] in ("Strong", "Proven")]
        thin_n = rankable_n - len(strong)
        # APEX-ELITE6-FLOOR70-2026-10-01 (Elite6 Quality Floor): Board Inventory
        # Forensic Audit + Elite6 Quality Floor Audit (2026-10-01) confirmed, from
        # settled history, that Apex Score 70+ picks hit at ~84.3% while sub-70
        # picks (the only thing separating Elite6 from Best Standard/Goblin/Demon,
        # which already enforce this) hit at ~55.5% -- a ~29pt gap, consistent
        # across repeated audits. Reuses the SAME BEST_LIST_QUALITY_FLOOR constant
        # already used by Best Standard/Goblin/Demon -- never a second hardcoded
        # threshold. Additive only: removes nothing from apex_score(),
        # apex_confidence(), ranking order, or any other command -- a sub-70 pick
        # simply isn't eligible for Elite6; it stays fully visible on the main
        # board / Picks Channel / Top10. No padding: if fewer than `limit` clear
        # the floor, Elite6 shows fewer, never backfilled with a sub-70 pick.
        _strong_pre_floor_n = len(strong)
        # APEX-HELDBACK-2026-10-01: Held Back Transparency -- rank every
        # Strong/Proven candidate BEFORE the quality floor removes any of
        # them, so a floor-dropped prop can still report the rank it would
        # have held. Sort first (score desc, rank desc, prob desc -- same
        # live order used below), THEN floor-filter.
        strong.sort(key=lambda d: _bb_sortkey(d))   # APEX-PROBRANK-2026-10-10: Win Probability first
        _orig_rank = {id(d): i for i, d in enumerate(strong, 1)}
        _held_back = []
        strong, _lc_held = _low_cushion_split(strong, _orig_rank)   # APEX-BLOWOUT-2026-10-22
        _held_back.extend(_lc_held)
        # APEX-DEMONFLOOR60-2026-10-01: Elite6 deliberately keeps the fixed
        # 70+ bar regardless of a prop's own type -- the Demon split applies
        # only to Best Demon, never to Elite6's cross-type ranking.
        floor_dropped_n = len([d for d in strong if not _board_qualifies(d)])   # APEX-PROBRANK-2026-10-10
        for d in strong:
            if d["sc"] < ELITE6_QUALITY_FLOOR:
                # APEX-HELDBACKAUDIT-2026-10-01: failing Elite6's 70+ quality
                # floor is FAILED QUALIFICATION, not a true held-back prop --
                # not added to _held_back (see All-Board Held-Back
                # Classification Audit, owner, 2026-10-01).
                pass
        strong = [d for d in strong if _board_qualifies(d)]   # APEX-PROBRANK-2026-10-10: 70%+ and score 67+
        # APEX-REVERT-2026-09-26 live sort: score desc, rank desc, prob desc
        strong.sort(key=_slot_key)   # APEX-PROBRANK-2026-10-10 / APEX-BESTLINE-2026-10-25: board rank first

        import unicodedata as _ud
        def _norm(s):
            s = _ud.normalize("NFKD", str(s or ""))
            s = "".join(c for c in s if not _ud.combining(c))
            return s.casefold().replace("'", "").replace(".", "").replace("-", " ")

        elite, dropped_player, dropped_match = [], [], []
        seen_players, seen_matches = set(), set()
        for d in strong:
            p = d["p"]
            # APEX-MATCHDEDUP-FIX-2026-10-07 (owner, authorized): a
            # match-level stat (player field literally == "match") is not
            # an individual player -- it must never be keyed against other
            # matches' match-level props, or every match beyond the first
            # wrongly looks like a "duplicate player." Only a real
            # per-player row gets a pkey; match-level rows are deduped
            # solely on their own match identity (mid) below.
            pkey = "" if (p.get("player") or "") == "match" else _norm(p.get("player"))
            mid = p.get("match_id") or (_norm(p.get("player")) + "|" + _norm(p.get("opponent")))
            if pkey and pkey in seen_players:
                dropped_player.append(d)
                _held_back.append({"rank": _orig_rank.get(id(d)), "score": d["sc"], "p": d["p"], "conf": d.get("conf"),
                                    "reason": "player already has a slot (PrizePicks one-per-player rule)"})
                continue
            if mid in seen_matches:
                dropped_match.append(d)
                _held_back.append({"rank": _orig_rank.get(id(d)), "score": d["sc"], "p": d["p"], "conf": d.get("conf"),
                                    "reason": "match already has a slot"})
                continue
            elite.append(d)
            if pkey:
                seen_players.add(pkey)
            seen_matches.add(mid)
            if len(elite) >= limit:
                break

        if not elite:
            # APEX-HELDBACKFIRST-2026-10-01: "Closest To Qualifying" is
            # retired -- an empty Elite6 board now shows the same Held Back
            # From This Board section a non-empty board shows, from the
            # SAME _held_back list already built above.
            msg_lines = ["No new qualifying picks available."]
            msg_lines.append("─" * 30)
            msg_lines.extend(_held_back_section_lines(_held_back, _fs_first, _fs_last, board_label="Elite6"))
            msg = "\n".join(msg_lines)
            return {"text": msg, "scored": [], "held_back": _held_back} if return_scored else msg

        # APEX-FRESHNESS-2026-09-29: index itself now loaded earlier -- see
        # APEX-HELDBACKFIRST-2026-10-01 above.
        L = ["\U0001F3C6 **APEX ELITE 6** -- %s" % _d.today().isoformat(),
             "_The 6 strongest plays on the entire board -- no category quota, no padding._",
             sync_status_line()]
        _fs_banner = sync_warning_banner()
        if _fs_banner:
            L.append(_fs_banner)
        # APEX-ELITE6-FLOOR70-2026-10-01: transparency banner, same format as
        # Best Standard/Goblin/Demon's quality-floor banner.
        L.append("\U0001F6A6 Elite6 Qualifies at: Win Probability %d%%+ and Apex Score %d+ -- ranked by Win Probability" % (round(BOARD_MIN_PROB * 100), ELITE6_QUALITY_FLOOR))
        if floor_dropped_n:
            L.append("(%d prop(s) excluded for failing the quality floor)" % floor_dropped_n)
        if _fs_excluded_n:   # APEX-FS-EXCLUSION-2026-09-30
            L.append("(%d Fantasy Score prop(s) excluded from Elite 6 -- pending recalibration, "
                      "still scraped/logged/settled/tracked)" % _fs_excluded_n)
        # APEX-BOARDPARITY-2026-10-05 (owner, Board Consistency fix):
        # migrated onto the same shared card renderer Top3/Top10/Goblin/Demon/
        # Standard already use -- selection, ranking, scoring, confidence,
        # probability, and the floor/dedup logic above are all untouched;
        # only how each already-chosen pick is displayed changes.
        _medals = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}
        for i, d in enumerate(elite, 1):
            L.extend(_render_pick_card(d, i, rec, _fs_first, _fs_last, _medals,
                                        worst=False, show_debug=False))
        L.append("━" * 22)
        n = len(elite)
        avg_prob = sum(float(d["p"].get("prob") or 0) for d in elite) / n * 100
        avg_conf = sum(d["conf"] for d in elite) / n
        avg_score = sum(d["sc"] for d in elite) / n
        L.append("Average Probability: %.1f%%" % avg_prob)
        L.append("Average Confidence: %.1f/100" % avg_conf)
        L.append("Average Score: %.1f" % avg_score)
        L.append("━" * 22)
        L.append("**Quality checks**")
        L.append("Playable count: %d" % playable_n)
        L.append("Rankable count: %d" % rankable_n)
        L.append("Removed -- thin/untested evidence: %d" % thin_n)
        L.append("Removed -- below quality floor: %d" % floor_dropped_n)
        L.append("Removed -- duplicate player: %d" % len(dropped_player))
        L.append("Removed -- duplicate match: %d" % len(dropped_match))
        if n < limit:
            L.append("⚠️ Only %d pick(s) cleared the Elite 6 bar tonight -- shown as-is, never padded." % n)
        counts = {}
        for d in elite:
            counts[d["ptype"]] = counts.get(d["ptype"], 0) + 1
        L.append("Composition: " + ", ".join("%s %d" % (k.capitalize(), v) for k, v in sorted(counts.items())))
        if _held_back:   # APEX-HELDBACKFIRST-2026-10-01 (was APEX-PERBOARD-2026-10-01)
            L.append("─" * 30)
            L.extend(_held_back_section_lines(_held_back, _fs_first, _fs_last, board_label="Elite6"))
        if _legacy_flag:   # APEX-LEGACYPICKS-2026-10-05 (Phase 2) -- own section, never merged with Held Back
            L.extend(_legacy_picks_section_lines(_legacy_picks_for_board(_legacy_flag, elite)))
        text = "\n".join(L)
        if return_scored:
            return {"text": text, "scored": elite, "held_back": _held_back}
        return text
    except Exception as e:
        msg = "elite6 unavailable (%s)" % type(e).__name__
        return {"text": msg, "scored": [], "held_back": []} if return_scored else msg


# ================================================================ APEX-PERBOARD-2026-10-01
# ================================================================ APEX-STANDARD6X-2026-10-11
# owner, approved "do both" 2026-10-11. Best Standard used the Goblin
# bar (70% + Apex 67), which a Standard almost never reaches -- the board was
# always empty. A Standard 3-pick Power Play pays 6x, so a leg only needs ~55%
# to be profitable. New rule for THIS board only (other boards unchanged):
#   - Standard props, playable, match not started
#   - Honest Probability >= STANDARD_MIN_HONEST (0.57, about 61% stated)
#   - only markets with a winning Standard record in settled results:
#     Double Faults LESS (21-9, 70%) and Games Won MORE (57%); others are added
#     only once they win 58%+ on 50+ settled Standard props
#   - one prop per match, ranked by Win Probability, top 6
# Replay with the current engine: Standard props at 60%+ won 62.1% (82-50).
STANDARD_MIN_HONEST = 0.57
STANDARD_MARKETS = {("dfs", "LESS"), ("games_won", "MORE")}
STANDARD_BOOK_MIN_MODEL = 0.55   # APEX-STANDARDBOOK-2026-10-11: Total Games Standards need model 55%+ ...
STANDARD_BOOK_MIN_BOOK = 0.52    # ... and bet365 52%+ for the same side
# APEX-BOARDAUDIT-2026-10-19 (owner, "Go"): Fantasy Score LESS Standards qualify at
# Win Probability 75%+ (FS LESS at 75%+ went 149-52, 74%, Sep 20 - Oct 19). Brandon Nakashima
# FS 24.5 LESS at 81% was left off Best Standard while 55% Total Games filled it.
STANDARD_FS_LESS_MIN_PROB = 0.75
# APEX-DFLESS70-2026-10-22 (owner, "Go" on 10/22): Double Faults LESS Standards
# also need Win Probability 70%+ on Best Standard (on top of Honest 57%; no floor lowered).
# Undo: None.
STANDARD_DF_LESS_MIN_PROB = 0.70
# APEX-DFFANTASY-2026-10-22 (owner, "1,2.3 now", study D1): Double Faults LESS on Best
# Standard also needs Apex 67, like every other board (under 67 went 11-12 vs 12-5 for 67+).
# Held ones are listed under Best Standard BACKUPS with the "⚠ Apex 66, under the 67 bar" mark.
# Undo: None.
STANDARD_DF_LESS_APEX_MIN = 67


def _df_less_apex_short(p, sc):
    """APEX-DFFANTASY-2026-10-22: True when a DF LESS Standard is under the Apex bar."""
    try:
        return (STANDARD_DF_LESS_APEX_MIN is not None and p.get("category") == "dfs"
                and str(p.get("direction") or "").upper() == "LESS" and float(sc) < STANDARD_DF_LESS_APEX_MIN - 1e-9)
    except (TypeError, ValueError):
        return True


def _df_less_short(p, pr):
    """APEX-DFLESS70-2026-10-22: True when a DF LESS Standard is under the 70% bar."""
    return (STANDARD_DF_LESS_MIN_PROB is not None and p.get("category") == "dfs"
            and str(p.get("direction") or "").upper() == "LESS" and pr < STANDARD_DF_LESS_MIN_PROB - 1e-9)


# APEX-DEMONBOARD-2026-10-22 (owner: "Go but I do want to start seeing demon only
# on the demon board ... the highest probability demon that make sense"): Best Demon gets
# its own rule, like Best Standard. Demon MORE only, Win Probability 60%+, Aces and Breaks
# left out, one per match, highest Win Probability first. Demon audit Sep 29-Oct 21: Apex's
# 60%+ Demons hit 45% (14-17) vs ~41% a leg needed on a 3-Demon slip (14x); under 60% hit
# 37%; Aces Demons 27-29%; Breaks is kept off every board. Demons stay off all other boards.
DEMON_MIN_PROB = 0.60
DEMON_EXCLUDED = frozenset()   # APEX-DEMONACESBREAKS-2026-10-22 (his ask): Aces + Breaks Demons shown, with a warning line
DEMON_CATEGORY_WARN = {   # Demon MORE record Sep 29-Oct 21 (all lines / Apex-rated)
    "aces": "\u26A0\uFE0F Aces Demons have hit only 27% of all lines (29% of the ones Apex liked); a 3-Demon slip needs ~41% a leg",
    "breaks": "\u26A0\uFE0F Breaks is kept off every other board; Breaks Demons have hit 39% of all lines (43% of the ones Apex liked, 33-76)",
}


def best_demon_detail(picks, rec=None, limit=6, return_scored=False):
    try:
        rec = rec or records()
        _fs_first, _fs_last, _ = _load_freshness_index()
        cand, held = [], []
        for p in picks:
            if str(p.get("prop_type") or "").lower() != "demon" or not p.get("category"):
                continue
            if str(p.get("direction") or "").upper() != "MORE" or _is_unplayable(p) or _pub_match_started(p):
                continue
            if p["category"] in DEMON_EXCLUDED or p["category"] in DISABLED_CATEGORIES:
                continue
            pr = _board_prob(p)
            if pr < DEMON_MIN_PROB - 1e-9:
                continue
            try:
                sc, ev, lvl, up, down = apex_score(pr, p["category"], bool(p.get("bo5")), rec)
            except Exception:
                sc, ev, lvl, up, down = 0, "", "LOW", [], []
            _dd = {"p": p, "sc": sc, "score": sc, "ev": ev, "lvl": lvl, "up": up, "down": down,
                   "conf": 0, "cwhy": [], "rank": 0.0}
            if DEMON_CATEGORY_WARN.get(p["category"]):
                _dd["warn_note"] = DEMON_CATEGORY_WARN[p["category"]]
            _hold = _type_board_cushion_hold(p)   # APEX-STANDARDHOLD-2026-10-23
            if _hold:
                held.append({"rank": None, "score": sc, "p": p, "conf": 0, "reason": _hold})
                continue
            cand.append(_dd)
        # APEX-BESTLINE-2026-10-25: best board-rank prop per match; held props last so the next one moves up
        cand.sort(key=lambda d: (bool(board_hold_reason(d["p"])), _slot_key(d, _board_prob)))
        shown, seen = [], set()
        for d in cand:
            m = _prop_names(d["p"])
            if m & seen:
                continue
            seen |= m
            shown.append(d)
            if len(shown) >= limit:
                break
        L = ["\U0001F608 **BEST DEMON** -- %s" % _date_today_str(),
             "\U0001F6A6 Demon rule: Demon MORE at Win Probability %d%%+ (Aces and Breaks carry a warning), one per match, "
             "highest first. Demons pay big but hit less: a 3-Demon slip (about 14x) needs about 41%% a leg. "
             "Apex's 60%%+ Demons have hit 45%% (14-17) while Apex said 64%%, so read the Win Probability "
             "here as roughly 15-20 points too high." % round(DEMON_MIN_PROB * 100)]
        if top_slots_bar("bestdemon"):   # APEX-DEMONSTDBARS-2026-10-25
            L.append("\U0001F947 Top 3 spots need %d%%+ win chance (Demons at 62%%+ hit 47%%, 9 of 19, vs 33%% under it; "
                     "a 3-Demon slip needs ~41%%)" % top_slots_bar("bestdemon"))
        if not shown:
            L.append("No Demon MORE props at %d%%+ right now." % round(DEMON_MIN_PROB * 100))
        medals = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}
        for i, d in enumerate(shown, 1):
            L.extend(_render_pick_card(d, i, rec, _fs_first, _fs_last, medals, worst=False, show_debug=False))
        L.extend(_cushion_held_lines(held))   # APEX-STANDARDHOLD-2026-10-23
        text = "\n".join(L)
        return {"text": text, "scored": shown, "held_back": held, "watchlist": []} if return_scored else text
    except Exception as e:
        msg = "best demon unavailable (%s)" % type(e).__name__
        return {"text": msg, "scored": [], "held_back": []} if return_scored else msg


# APEX-STANDARDRANK-2026-10-22 (owner, "Go" on 10/22; first built 10/21 as
# APEX-STANDARDRANK-2026-10-21 and reverted for lack of a go): Win Probability is not
# comparable across Standard markets -- a 57% Total Games Standard hit 26% on the board
# while 65%+ Double Faults LESS hit ~60%. With BEST_STANDARD_MARKET_RANK = True, Best
# Standard ranks by market-adjusted probability: Win Probability times the market's own
# record (settled hit / average stated, Standards at 55%+, last 30 days), shrunk toward
# 1 by n/(n+50). Qualifying rules are unchanged. Undo: False = Win Probability order
# (the Win-Prob-only order is still logged daily in apex_standard_papertest).
BEST_STANDARD_MARKET_RANK = True
STANDARD_RANK_WINDOW_DAYS = 30
STANDARD_RANK_SHRINK_N = 50
_std_mkt_cache = {"ts": 0, "f": {}}


def _standard_market_factors(before=None):
    """{(category, DIRECTION): factor} from props_db Standards (55%+) in the 30 days
    before `before` (ISO date; None = today). Never raises."""
    f = {}
    try:
        import sqlite3 as _sq
        con = _sq.connect(os.path.join(HERE, "data", "props_db.sqlite"))
        if before:
            q, args = ("match_date >= date(?, ?) and match_date < ?",
                       (before, "-%d day" % STANDARD_RANK_WINDOW_DAYS, before))
        else:
            q, args = "match_date >= date('now', ?)", ("-%d day" % STANDARD_RANK_WINDOW_DAYS,)
        for c, d, n, ap, hit in con.execute(
                "select category, engine_direction, count(*), avg(engine_prob), "
                "avg(case when result = lower(engine_direction) then 1.0 else 0 end) "
                "from projections where odds_type = 'standard' and result in ('more','less') "
                "and engine_prob >= 0.55 and " + q + " group by 1, 2", args):
            if n and ap:
                w = n / float(n + STANDARD_RANK_SHRINK_N)
                f[(c, str(d).upper())] = (1 - w) + w * (hit / ap)
        con.close()
    except Exception:
        pass
    return f


def _standard_market_factor(cat, direction):
    import time as _t
    if not BEST_STANDARD_MARKET_RANK:
        return 1.0
    if _t.time() - _std_mkt_cache["ts"] > 3600:
        _std_mkt_cache.update(ts=_t.time(), f=_standard_market_factors())
    return _std_mkt_cache["f"].get((cat, str(direction or "").upper()), 1.0)


def _standard_rank_prob(d):
    p = d["p"] if isinstance(d, dict) and isinstance(d.get("p"), dict) else d
    return _board_prob(d) * _standard_market_factor(p.get("category"), p.get("direction"))


def best_standard_detail(picks, rec=None, limit=6, return_scored=False):
    try:
        rec = rec or records()
        _fs_first, _fs_last, _ = _load_freshness_index()
        cand, held = [], []
        for p in picks:
            if str(p.get("prop_type") or "").lower() != "standard" or not p.get("category"):
                continue
            if _pub_match_started(p):
                continue
            if _category_gate_blocked(p):   # APEX-CATGATE-2026-10-20
                continue
            pr = _board_prob(p)
            hp = corrected_prob(pr)
            _mkt = (p["category"], str(p.get("direction") or "").upper())
            if _mkt in STANDARD_MARKETS:
                if hp is None or hp < STANDARD_MIN_HONEST - 1e-9 or _df_less_short(p, pr):   # APEX-DFLESS70-2026-10-22
                    continue
            elif _mkt == ("fantasy_score", "LESS"):
                if pr < STANDARD_FS_LESS_MIN_PROB - 1e-9:
                    continue
            elif p["category"] == "total_games":
                # APEX-STANDARDBOOK-2026-10-11 (owner, approved "Go"): a Total Games
                # Standard qualifies when the model AND bet365 both favor our side --
                # bet365 backfill (345 settled matches): 65.2% (30-16) when model >= 55%
                # and bet365 >= 52%, vs ~51% for either signal alone.
                _bp = p.get("apex_book_prob")
                if _bp is None or pr < STANDARD_BOOK_MIN_MODEL - 1e-9 or float(_bp) < STANDARD_BOOK_MIN_BOOK - 1e-9:
                    continue
            else:
                continue
            try:
                sc, ev, lvl, up, down = apex_score(pr, p["category"], bool(p.get("bo5")), rec)
            except Exception:
                sc, ev, lvl, up, down = 0, "", "LOW", [], []
            if _df_less_apex_short(p, sc):   # APEX-DFFANTASY-2026-10-22
                continue
            _hold = _type_board_cushion_hold(p)   # APEX-STANDARDHOLD-2026-10-23
            if _hold:
                held.append({"rank": None, "score": sc, "p": p, "conf": 0, "reason": _hold})
                continue
            cand.append({"p": p, "sc": sc, "score": sc, "ev": ev, "lvl": lvl, "up": up, "down": down,
                         "conf": 0, "cwhy": [], "rank": 0.0})
        cand.sort(key=lambda d: (bool(board_hold_reason(d["p"])),   # APEX-BESTLINE-2026-10-25
                                 _slot_key(d, _standard_rank_prob)))   # APEX-STANDARDRANK-2026-10-22 (= Win Prob when switch off)
        shown, seen = [], set()
        for d in cand:
            p = d["p"]
            m = frozenset(x.strip().lower() for x in (str(p.get("opponent") or "").split(" v ")
                          if p.get("player") == "match" else [p.get("player"), p.get("opponent")]))
            if m in seen:
                continue
            seen.add(m)
            shown.append(d)
            if len(shown) >= limit:
                break
        L = ["\U0001F3C6 **BEST STANDARD** -- %s" % _date_today_str(),
             "\U0001F6A6 Standard rule (pays 6x on a 3-pick): Honest Probability %d%%+ in markets with a winning "
             "Standard record (Double Faults LESS at Win Probability 70%%+ and Apex 67+, Games Won MORE), Fantasy Score LESS at Win Probability %d%%+, plus Total "
             "Games where the model (55%%+) and bet365 (52%%+) agree; one per match, ranked by Win Probability%s"
             % (round(STANDARD_MIN_HONEST * 100), round(STANDARD_FS_LESS_MIN_PROB * 100),
                " adjusted for each market's own 30-day Standard record" if BEST_STANDARD_MARKET_RANK else "")]
        if top_slots_bar("beststandard"):   # APEX-DEMONSTDBARS-2026-10-25
            L.append("\U0001F947 Top 3 spots need %d%%+ adjusted win chance (Standards at 68%%+ adjusted hit 69%%, 11 of 16, "
                     "vs 52%% under it; a 6x 3-pick needs 55%%)" % top_slots_bar("beststandard"))
        if not shown:
            L.append("No Standard props meet the Standard rule right now.")
        medals = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}
        for i, d in enumerate(shown, 1):
            L.extend(_render_pick_card(d, i, rec, _fs_first, _fs_last, medals, worst=False, show_debug=False))
        L.extend(_cushion_held_lines(held))   # APEX-STANDARDHOLD-2026-10-23
        text = "\n".join(L)
        return {"text": text, "scored": shown, "held_back": held, "watchlist": []} if return_scored else text
    except Exception as e:
        msg = "best standard unavailable (%s)" % type(e).__name__
        return {"text": msg, "scored": [], "held_back": []} if return_scored else msg


def _date_today_str():
    from datetime import date as _d
    return _d.today().isoformat()


def top3_detail(picks, rec=None, return_scored=False, exclude_fantasy_score=True):
    """Per-Board Transparency Overhaul (owner, 2026-10-01): a real
    Top 3 board, distinct from Elite6 (no category quota but a fixed 70+
    floor) and Top10 (10 slots, same pool as /bestbets). Top 3 is simply
    the whole-board best_bets_detail() ranking truncated to 3 -- same
    scoring, same dedup, same Held Back Transparency -- with two things
    added on top, purely presentational:
      1. NEXT BEST OPTIONS -- ranks 4-10, a compact one-line-per-pick list
         (not full cards) so a user can see what's right behind the top 3
         without re-running /top10.
      2. The same CLOSEST TO QUALIFYING fallback every other board gets if
         it ever comes back with zero picks.
    Never re-scores, re-ranks, or filters anything -- reads the exact
    `scored` list best_bets_detail() already built and returns."""
    try:
        # APEX-BOARDPARITY-2026-10-05 (owner, Board Consistency fix):
        # slip_preview=True turns on Held Back reporting for Top3 -- it does
        # NOT filter `scored`, so which 3 picks Top3 shows is unchanged.
        res = best_bets_detail(picks, rec=rec, limit=3, exclude_fantasy_score=exclude_fantasy_score,
                                return_scored=True, slip_preview=True)
        if not isinstance(res, dict):
            # best_bets_detail can return a bare string on an early note
            # (e.g. "no active props") -- nothing to augment.
            return res if not return_scored else {"text": res, "scored": [], "held_back": []}
        text = res.get("text", "")
        scored = res.get("scored") or []
        held_back = res.get("held_back") or []
        if not scored:
            # APEX-HELDBACKFIRST-2026-10-01: best_bets_detail(prop_type=None)
            # never populates held_back on its own empty path (no quality
            # floor runs without a prop_type) -- genuinely nothing to rank
            # yet, so this stays the plain zero-evidence message
            # best_bets_detail already returned; no held-back data exists
            # to show here, and none is fabricated.
            pass
        else:
            next_best = scored[3:10]
            if next_best:
                nb_lines = ["─" * 30, "\U0001F947 **NEXT BEST OPTIONS** (ranks 4–10):"]
                for i, d in enumerate(next_best, 4):
                    p = d["p"]
                    nm = p.get("opponent") if p.get("player") == "match" else p.get("player", "")
                    nb_lines.append("  · Rank #%d · %s · %s%s · Apex Score: %d · Confidence: %d/100"
                                     % (i, nm, _market_str(p), projection_note(p), d["sc"], d.get("conf", 0)))
                nb_text = "\n".join(nb_lines)
                anchor = "_RANK SCORE = APEX"
                idx = text.find(anchor)
                if idx != -1:
                    text = text[:idx] + nb_text + "\n" + text[idx:]
                else:
                    text = text.rstrip("\n") + "\n" + nb_text
        if return_scored:
            return {"text": text, "scored": scored, "held_back": held_back}
        return text
    except Exception as e:
        msg = "top3 unavailable (%s)" % type(e).__name__
        return {"text": msg, "scored": [], "held_back": []} if return_scored else msg


# APEX-ALLPROPSSTD-2026-10-22 (owner, "Go" on 10/22): Standard props (⚪) that pass
# 70% / Apex 67 stay on /allprops (never hidden) with a warning line giving the Best
# Standard board's own record so far. Undo: False (no warning line).
ALLPROPS_STANDARD_WARN = True
# APEX-SAMEMATCH-2026-10-22 (owner, "1,4,5,6 go" on 10/22, item 5): an /allprops card gets
# "⚠ Same match as: Boulter DF LESS 6.5" when another prop on the list is from the same match.
# Display only. Undo: SAME_MATCH_HINTS = False.
SAME_MATCH_HINTS = True
ALLPROPS_EVERY_PROP = False  # APEX-ALLPROPSEVERY-2026-10-25: OFF 10/25 (APEX-ALLPROPSSIZE); under-70% rows no longer built
ALLPROPS_RANK_ORDER = True   # APEX-ALLPROPSSIZE-2026-10-25: 70%+ list in board-rank order
_bs_rec_cache = {"ts": 0, "txt": None}


def _best_standard_record_warning():
    """'⚠ Standard: Best Standard board is 50% so far (19 of 38)', cached 1 hour. Never raises."""
    import time as _t
    if _bs_rec_cache["txt"] and _t.time() - _bs_rec_cache["ts"] < 3600:
        return _bs_rec_cache["txt"]
    txt = "\u26A0 Standard: pays 6x on a 3-pick only if it hits; check the Best Standard record"
    try:
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import apex_engine_validation as _ev
            _t0, _rep, S, status = _ev.load()
            days = _ev.board_days(status, S, "beststandard", "new")
        legs = [h for items in days.values() for _, _, h in items if h is not None]
        if legs:
            w = sum(1 for h in legs if h)
            txt = "\u26A0 Standard: Best Standard board is %d%% so far (%d of %d hit; a 6x Power needs about 55%%)" % (
                round(100.0 * w / len(legs)), w, len(legs))
    except Exception:
        pass
    _bs_rec_cache.update(ts=_t.time(), txt=txt)
    return txt


_SHORT_CAT = {"dfs": "DF", "total_games": "Total Games", "games_won": "Games Won", "fantasy_score": "Fantasy",
              "aces": "Aces", "breaks": "Breaks", "sets_won": "Sets Won", "total_sets": "Total Sets", "tiebreaks": "Tiebreaks"}


def _same_match_key(p):
    """APEX-SAMEMATCH-2026-10-22: match_id, else the two players' names."""
    if p.get("match_id"):
        return p["match_id"]
    if p.get("player") == "match":
        xs = str(p.get("opponent") or "").replace(" vs ", " v ").split(" v ")
    else:
        xs = [p.get("player"), p.get("opponent")]
    return frozenset(str(x or "").strip().lower() for x in xs)


def _same_match_label(p):
    """'Boulter DF LESS 6.5' (or 'Match Total Games MORE 17.5')."""
    if p.get("player") == "match":
        return "Total Games %s %s (whole match)" % (str(p.get("direction") or "").upper(), p.get("line")) \
            if p.get("category") == "total_games" else "%s %s %s (whole match)" % (
                _SHORT_CAT.get(p.get("category"), p.get("category")), str(p.get("direction") or "").upper(), p.get("line"))
    who = (str(p.get("player") or "?").split() or ["?"])[-1]
    return "%s %s %s %s" % (who, _SHORT_CAT.get(p.get("category"), LABEL.get(p.get("category"), p.get("category"))),
                            str(p.get("direction") or "").upper(), p.get("line"))


def _allprops_order_line():
    """APEX-ALLPROPSEVERY-2026-10-25: the "📋 Order:" line on All Props. Never raises."""
    h = ("\U0001F4CB Order: most likely to win first (Win Probability + %d points per signal point, "
         "same as the boards" % round(NET_SIGNAL_POINTS * 100))
    try:
        import apex_card_signals as _csig
        if _csig.BLOWOUT_DOUBLE:
            h += "; \U0001F3DF\ufe0f blowout risk counts double"
    except Exception:
        pass
    if LINE175_SKIP_PENALTY or LINE175_NODATA_PENALTY:
        h += "; over 17.5 picks lose %d points if the 17.5 check says skip, %d if not enough data yet" % (
            round(LINE175_SKIP_PENALTY * 100), round(LINE175_NODATA_PENALTY * 100))
    h += "). Every prop type at %d%%+ Win Probability, held ones too with the reason." % round(BOARD_MIN_PROB * 100)
    return h + " One-per-match is a board rule, not used here."


def _allprops_hold_reasons(p):
    """APEX-STANDARDHOLD-2026-10-23: every board hold on this prop, in plain words. Never raises."""
    out = []
    for f in (lambda: _type_board_cushion_hold(p),   # includes APEX-NOBOOK-2026-10-24
              lambda: line175_skip_reason(p),
              lambda: _surface_hold(p),
              lambda: df_fs_hold_reason(p),
              lambda: big_fav_reason(p),   # APEX-ALLPROPS-2026-11-01
              lambda: line70_hold_reason(p),
              lambda: _category_gate_blocked(p) and "category held off the Best boards this hour (hitting well under Honest)"):
        try:
            r = f()
            if r and r not in out:
                out.append(r)
        except Exception:
            pass
    return out


def qualified_detail(picks, rec=None):
    """APEX-QUALIFIED-2026-10-13 (owner): /allprops -- every playable
    prop at Win Probability 70%+ on the latest evaluated board, every prop
    type, ranked best first (Apex tiebreak), one short card each. No
    one-per-match, one-per-player or category cap. A check mark means it
    also passes Apex 67+ and its category gate. Read-only, logs nothing."""
    try:
        rec = rec or records()
        try:
            _snap = json.load(open(PUBLISHED_BOARD_SNAPSHOT))
        except Exception:
            _snap = {}
        _on = {}
        _book = {}   # APEX-ALLPROPSFULL-2026-10-22: published bet365 line per prop, Best Goblin's first
        for _flag, _b in (_snap.get("boards") or {}).items():
            for _e in (_b or {}).get("entries") or []:
                _on.setdefault(_apex_prop_identity(_e.get("p") or {}), []).append(
                    _PUBLISHED_FLAG_LABEL.get(_flag, _flag))
        for _flag, _b in sorted((_snap.get("boards") or {}).items(), key=lambda kv: kv[0] != "bestgoblin"):
            for _e in (_b or {}).get("entries") or []:
                if (_e.get("p") or {}).get("apex_book_note"):
                    _book.setdefault(_apex_prop_identity(_e["p"]), _e["p"]["apex_book_note"])
        rows = []
        for p in picks:
            cat = p.get("category")
            if not cat or _is_unplayable(p):
                continue
            sc = apex_score(p.get("prob_cal", p.get("prob")), cat, bool(p.get("bo5")), rec)[0]
            d = {"p": p, "sc": sc, "score": sc}
            d["ok"] = (_category_has_evidence(cat, rec)
                       and _category_probability_gate(cat, p.get("prob_cal", p.get("prob")))
                       and _board_qualifies(d))
            rows.append(d)
        # APEX-ALLPROPSEVERY-2026-10-25 (owner: "all pros board should show all props but have
        # them listed in order with best to least the best most likely to succeed always go at the
        # top"): every playable prop (under 70% and held ones too, each with its hold reason), ordered
        # by the rank the boards use (board_rank_score: Win Prob + signal points, blowout double, 17.5
        # push-down; Apex Score breaks ties). The top-3 bar and one-per-match are board rules only.
        # Undo: ALLPROPS_EVERY_PROP = False (70%+ only, Win Probability order).
        # APEX-ALLPROPSSIZE-2026-10-25 (owner: only wanted the order changed, not more props):
        # same 70%+ list as before 10/25 10:38 (held ones at 70%+ still shown with the reason, as
        # before); only the order is new -- board_rank_score (Win Prob + signal points, blowout
        # double, 17.5 push-down), Apex Score breaks ties. Undo: ALLPROPS_RANK_ORDER = False.
        rows = [d for d in rows if _board_prob(d) >= BOARD_MIN_PROB - 1e-9]
        _ap_note = {}
        if ALLPROPS_RANK_ORDER:
            rows.sort(key=lambda d: (-board_rank_score(d["p"], _board_prob(d)), -float(d.get("score") or 0)))
        else:
            rows.sort(key=lambda d: _bb_sortkey(d))
        L = ["\U0001F4CB **ALL PROPS -- %d%%+ WIN PROBABILITY**" % round(BOARD_MIN_PROB * 100),
             sync_status_line(),
             _allprops_order_line() if ALLPROPS_RANK_ORDER else
             "Every prop type, best first. Nothing held back by slip rules."]
        if SCORE_DISPLAY:   # APEX-SCOREDISPLAY-2026-10-25
            L.append(SCORE_EXPLAIN)
        if not rows:
            L.append("No prop reaches %d%% Win Probability on the current board." % round(BOARD_MIN_PROB * 100))
            return "\n".join(L)
        _types = {}
        for d in rows:
            _t = str(d["p"].get("prop_type") or "?").capitalize()
            _types[_t] = _types.get(_t, 0) + 1
        L.append("**%d props** (%s) \u00b7 \u2705 %d pass Apex %d+ with no hold"
                 % (len(rows), ", ".join("%d %s" % (n, t) for t, n in sorted(_types.items())),
                    sum(1 for d in rows if d["ok"] and not _allprops_hold_reasons(d["p"])), BOARD_MIN_SCORE))
        _icon = {"goblin": "\U0001F7E2", "demon": "\U0001F534", "standard": "\u26AA"}
        # APEX-ALLPROPSFULL-2026-10-22 (owner, "Full cards"): every All Props prop
        # renders as the same full card the boards use (_render_pick_card: signals, bet365,
        # Honest, confidence, category, freshness, why), plus its prop type, the Apex bar
        # check and which boards it is on. Display only -- which props show and their order
        # are unchanged. A card that fails to render falls back to the old short card.
        try:
            _fs_first, _fs_last, _ = _load_freshness_index()
        except Exception:
            _fs_first, _fs_last = {}, {}
        _by_match = {}   # APEX-SAMEMATCH-2026-10-22
        if SAME_MATCH_HINTS:
            for _d in rows:
                _by_match.setdefault(_same_match_key(_d["p"]), []).append(_d["p"])
        _sdec = score_decimals([score_value(None, d["p"]) for d in rows])   # APEX-SCOREDISPLAY-2026-10-25
        for i, d in enumerate(rows, 1):
            p = d["p"]
            _pt = str(p.get("prop_type") or "").lower()
            boards = _on.get(_apex_prop_identity(p))
            _tag = " \u00b7 %s %s" % (_icon.get(_pt, ""), _pt.capitalize() or "?")
            # APEX-STANDARDHOLD-2026-10-23: every hold that keeps it off a numbered spot sits by its
            # mark (17.5 skip, low cushion, surface, Fantasy LESS / DF holds, hourly category hold),
            # so a \u2705 here means it would be a play on the boards
            _holds = _allprops_hold_reasons(p)
            if "arank" in d:   # APEX-ALLPROPSEVERY-2026-10-25: the number the list is sorted on
                _net = _net_of(p)
                _pen = line175_penalty(p)[0]
                _extra0 = "\U0001F4C8 Rank %.0f = Win Prob %.0f%%%s%s" % (
                    100 * d["arank"], 100 * _board_prob(d),
                    (" %+.0f for signals (%+d)" % (100 * (d["arank"] + _pen - _board_prob(d)), _net))
                    if abs(d["arank"] + _pen - _board_prob(d)) > 1e-9 else "",
                    (" \u2212%.0f 17.5 check" % (100 * _pen)) if _pen else "")
            else:
                _extra0 = None
            if _board_prob(d) < BOARD_MIN_PROB - 1e-9:   # APEX-ALLPROPSEVERY-2026-10-25
                _extra = ["\u26A0\uFE0F under %d%% Win Probability: not a board pick%s" % (
                    round(BOARD_MIN_PROB * 100), ("; also held: " + "; ".join(_holds)) if _holds else "")]
            elif d["ok"] and not _holds:
                _extra = ["\u2705 passes Apex %d, no hold -- a play on the boards" % BOARD_MIN_SCORE]
            elif d["ok"]:
                _extra = ["\u26A0\uFE0F passes Apex %d but held (BACKUPS only): %s" % (BOARD_MIN_SCORE, "; ".join(_holds))]
            else:
                _extra = ["\u26A0\uFE0F below the Apex %d bar%s" % (BOARD_MIN_SCORE,
                          ("; also held: " + "; ".join(_holds)) if _holds else "")]
            if boards:
                _extra.append("\U0001F4CC On: %s" % ", ".join(boards))
            if _extra0:
                _extra.insert(0, _extra0)
            if id(d) in _ap_note:   # APEX-ALLPROPS75-2026-10-25
                _extra.insert(0, _ap_note[id(d)])
            if _pt == "standard" and ALLPROPS_STANDARD_WARN:   # APEX-ALLPROPSSTD-2026-10-22
                _extra.append(_best_standard_record_warning())
            if SAME_MATCH_HINTS:   # APEX-SAMEMATCH-2026-10-22: display only
                _others = [o for o in _by_match.get(_same_match_key(p), []) if o is not p]
                if _others:
                    _extra.append("\u26A0 Same match as: %s%s" % (", ".join(_same_match_label(o) for o in _others[:3]),
                                                                ("" if len(_others) <= 3 else " (+%d more)" % (len(_others) - 3))))
            if "arank" in d and _board_prob(d) < BOARD_MIN_PROB - 1e-9:
                # APEX-ALLPROPSEVERY-2026-10-25: under-70% props get one short line (full cards for
                # all of them would be ~90 Discord messages); same single order as the cards
                who = p.get("opponent", "") if (p.get("player") or "") == "match" else \
                    "%s v %s" % (p.get("player", ""), p.get("opponent", ""))
                L.append("**#%d** %s \u00b7 %s %s %s \u00b7 %s%s \u00b7 Win %d%% \u00b7 Rank %.0f%s%s" % (
                    i, who, LABEL.get(p.get("category"), p.get("category")), p.get("line", ""),
                    str(p.get("direction") or "").upper(), _icon.get(_pt, ""), _pt.capitalize() or "?",
                    round(_board_prob(d) * 100), 100 * d["arank"],
                    (" \u00b7 \u26A0\uFE0F held: " + "; ".join(_holds)) if _holds else "",
                    (" \u00b7 \U0001F4CC On: " + ", ".join(boards)) if boards else ""))
                continue
            if not p.get("apex_book_note") and _book.get(_apex_prop_identity(p)):
                p = dict(p, apex_book_note=_book[_apex_prop_identity(p)])   # display only
            try:
                cat = p["category"]
                sc, ev, lvl, up, down = apex_score(p.get("prob_cal", p.get("prob")), cat, bool(p.get("bo5")), rec)
                try:
                    import apex_card_index as _CI
                    if (p.get("player") or "") == "match":
                        _card = _CI.lookup_match(str(p.get("opponent") or ""), cat, str(p.get("match_date") or ""))[0]
                    else:
                        _card = _CI.lookup(str(p.get("player") or ""), cat, str(p.get("match_date") or ""))[0]
                except Exception:
                    _card = None
                cf, _ct, cwhy = apex_confidence(cat, bool(p.get("bo5")), rec, score=sc, prob=p.get("prob"),
                                                evidence=ev, attention=lvl, warnings=down, card_present=bool(_card))
                _fd = {"p": p, "sc": sc, "score": sc, "ev": ev, "lvl": lvl, "up": up, "down": down,
                       "conf": cf, "cwhy": cwhy, "rank": rank_score(sc, cf)}
                card = _render_pick_card(_fd, i, rec, _fs_first, _fs_last, {}, worst=False, show_debug=False,
                                         score_dec=_sdec[i - 1])   # APEX-SCOREDISPLAY-2026-10-25
                # prop type goes on the 📊 line; Apex bar check and boards right after 🎯 Win Probability
                for k, x in enumerate(card):
                    if x.startswith("\U0001F4CA"):
                        card[k] = x + _tag
                        break
                _at = next((k for k, x in enumerate(card) if x.startswith("\U0001F3AF")), len(card) - 1) + 1
                L.extend(card[:_at] + _extra + card[_at:])
                continue
            except Exception:
                pass
            who = p.get("opponent", "") if (p.get("player") or "") == "match" else \
                "%s vs %s" % (p.get("player", ""), p.get("opponent", ""))
            L.append("\u2500" * 24)
            L.append("**#%d \u00b7 %s**" % (i, who))
            L.append(start_time_line(p))
            if _h2h_line(p):   # APEX-H2H-2026-10-22
                L.append(_h2h_line(p))
            L.append("\U0001F4CA %s %s %s%s" % (LABEL.get(p.get("category"), p.get("category")), p.get("line", ""), p.get("direction", ""), _tag))
            _stx = score_text(None, d["p"], _sdec[i - 1])   # APEX-SCOREDISPLAY-2026-10-25
            L.append("\U0001F3AF Win Probability **%d%%**%s \u00b7 Apex %d" % (round(_board_prob(d) * 100),
                                                                          (" \u00b7 " + _stx) if _stx else "", d["sc"]))
            L.extend(_extra)
        return "\n".join(L)
    except Exception as e:
        return "all props unavailable (%s: %s)" % (type(e).__name__, e)


def allpicks_detail(picks, rec=None):
    """APEX-ALLPICKS-REDESIGN-2026-10-05 (Phase 8, owner): /allpicks
    redesigned around decision-making, not a raw dump. Reuses the exact
    best_bets_detail(slip_rules=True) scoring/dedup/held-back machinery
    (never re-scores or re-ranks anything itself) and reorganizes the
    result into: Evaluated Board summary -> Qualified Picks -> Demon
    Eligible -> Not Board Eligible -> Held Back -> Legacy Picks.

    Qualified Picks = non-Demon props at/above the standard 70+ floor
    (DEFAULT_QUALITY_FLOOR / QUALITY_FLOOR_BY_TYPE) -- the same floor
    Top3/Top10/Best Goblin/Best Standard use. Demon Eligible is its own
    section because Demon's floor is 60+, not 70+ (APEX-DEMONFLOOR60-
    2026-10-01) -- folding it into "Qualified" would misstate which bar
    a Demon prop actually cleared. Not Board Eligible = scored, real,
    fully tracked, but below its OWN type's floor -- tagged plainly
    rather than silently omitted, since /allpicks' whole purpose is to
    show the full evaluated board, floor status included.

    APEX-SCOREVSPROB-2026-10-05: explains the "68% probability but 54
    Apex Score" question directly in the summary -- Apex Score is not a
    restatement of probability. It anchors on probability, then adjusts
    for the category's actual settled-pick calibration history, a
    structural prior, confidence-band track record, and evidence
    thinness -- so a high-probability pick in a poorly-calibrated or
    thinly-tested category can score well below its stated probability,
    and a lower-probability pick in a historically reliable category
    can score above it. The score is a trust/evidence measure, not a
    probability restatement.
    """
    try:
        from datetime import date as _d
        rec = rec or records()
        res = best_bets_detail(picks, rec=rec, limit=max(len(picks), 1),
                                slip_rules=True, return_scored=True)
        if not isinstance(res, dict):
            return res
        scored = res.get("scored") or []
        held_back = res.get("held_back") or []
        if not scored and not held_back:
            return res.get("text", "No rankable picks on the current board.")

        def _is_demon(d):
            return (d["p"].get("prop_type") or "").strip().lower() in ("demon", "red_demon")

        demon_pool  = [d for d in scored if _is_demon(d)]
        other_pool  = [d for d in scored if not _is_demon(d)]
        # APEX-PROBRANK-2026-10-10: same 70%/67 rule and Win Probability order as every board
        qualified        = sorted([d for d in other_pool if _board_qualifies(d)], key=_bb_sortkey)
        other_ineligible = sorted([d for d in other_pool if not _board_qualifies(d)], key=_bb_sortkey)
        demon_floor = QUALITY_FLOOR_BY_TYPE.get("demon", DEFAULT_QUALITY_FLOOR)
        demon_eligible   = sorted([d for d in demon_pool if _board_qualifies(d)], key=_bb_sortkey)
        demon_ineligible = sorted([d for d in demon_pool if not _board_qualifies(d)], key=_bb_sortkey)
        not_eligible = sorted(other_ineligible + demon_ineligible, key=_bb_sortkey)

        try:
            legacy = _legacy_picks_for_board(None, scored, limit=6)
        except Exception:
            legacy = []

        L = ["🏆 **ALL PICKS -- EVALUATED BOARD -- %s**" % _d.today().isoformat(),
             sync_status_line()]
        _banner = sync_warning_banner()
        if _banner:
            L.append(_banner)
        L += ["",
              "Total Evaluated: %d" % (len(scored) + len(held_back)),
              "  ✅ Qualified Picks: %d" % len(qualified),
              "  🔴 Demon Eligible: %d" % len(demon_eligible),
              "  🚫 Not Board Eligible: %d" % len(not_eligible),
              "  🔒 Held Back: %d" % len(held_back),
              "  📚 Legacy Picks Available: %d" % len(legacy),
              "",
              "ℹ️ Why a pick can show a high stated probability but a lower Apex Score: "
              "the Score anchors on probability, then adjusts for that category's real settled-pick "
              "calibration history, a structural prior, confidence-band track record, and evidence "
              "thinness -- it measures historical trust, not just the stated probability. See the "
              "category's own record below for the specific reason on any one pick.",
              "=" * 50]

        def _oneline(d, tag=""):
            p = d["p"]
            nm = p.get("opponent") if p.get("player") == "match" else p.get("player", "")
            return ("  · %s%s · %s · Apex Score: %d · Confidence: %d/100%s"
                     % (nm, (" vs %s" % p.get("opponent")) if p.get("player") != "match" and p.get("opponent") else "",
                        _market_str(p), d["sc"], d.get("conf", 0) or 0,
                        ("  %s" % tag) if tag else ""))

        L.append("✅ **QUALIFIED PICKS** (%d)" % len(qualified))
        L += ([_oneline(d) for d in qualified] if qualified else ["  none today"])
        L += ["", "=" * 50, "🔴 **DEMON ELIGIBLE** (%d, Apex Score %d+)" % (len(demon_eligible), demon_floor)]
        L += ([_oneline(d) for d in demon_eligible] if demon_eligible else ["  none today"])
        L += ["", "=" * 50,
              "🚫 **NOT BOARD ELIGIBLE** (%d -- scored and tracked, below its own type's quality floor)" % len(not_eligible)]
        L += ([_oneline(d, tag="🚫 Not Board Eligible") for d in not_eligible] if not_eligible else ["  none today"])
        L += ["", "=" * 50]
        L.extend(_held_back_section_lines(held_back, *(_load_freshness_index()[:2]), board_label="All Picks"))
        L += ["", "=" * 50]
        L.extend(_legacy_picks_section_lines(legacy))
        return "\n".join(L)
    except Exception as e:
        return "allpicks unavailable (%s: %s)" % (type(e).__name__, e)


# APEX-TYPEDETAIL-FILTER-2026-09-28: overlap diagnostic. Compares the actual
# top-N picks /goblin, /standard and /demon would each hand back post-fix, so
# a regression that reintroduces cross-type duplication is caught by running
# this rather than by eyeballing three Discord messages side by side.
_OVERLAP_TYPES = ("goblin", "standard", "demon")


def _prop_key(p):
    return (str(p.get("player") or "").strip().casefold(),
            str(p.get("opponent") or "").strip().casefold(),
            str(p.get("category") or "").strip().casefold(),
            str(p.get("line") or ""),
            str(p.get("direction") or "").strip().upper())


def prop_type_overlap_report(picks, rec=None, limit=6):
    """Runs best_bets_detail() once per real prop type (goblin/standard/demon
    -- the market-availability types PrizePicks actually offers; there is no
    platform "bot/safe" type to check because that isn't a biddable market,
    it's a confidence label -- see best_bets_detail's own Confidence/Rank
    Score fields for that) and reports whether their top-`limit` picks
    overlap. Returns a dict, never raises."""
    rec = rec or records()
    total_typed = sum(1 for p in picks if p.get("prop_type"))
    per_type, top_keys = {}, {}
    for t in _OVERLAP_TYPES:
        res = best_bets_detail(picks, prop_type=t, limit=limit, rec=rec,
                               slip_rules=True, return_scored=True)
        scored = res["scored"] if isinstance(res, dict) else []
        board_count = sum(1 for p in picks if str(p.get("prop_type") or "").lower() == t)
        top = [_prop_key(d["p"]) for d in scored[:limit]]
        per_type[t] = {"on_board": board_count, "ranked": len(scored), "top": top}
        top_keys[t] = set(top)

    overlaps = {}
    warnings = []
    type_pairs = [(a, b) for i, a in enumerate(_OVERLAP_TYPES) for b in _OVERLAP_TYPES[i + 1:]]
    for a, b in type_pairs:
        shared = top_keys[a] & top_keys[b]
        overlaps[f"{a}_vs_{b}"] = len(shared)
        denom = min(len(top_keys[a]), len(top_keys[b])) or 1
        pct = len(shared) / denom
        if pct > 0.20:
            warnings.append(
                f"{a.upper()} and {b.upper()} top-{limit} overlap {pct:.0%} "
                f"({len(shared)}/{denom}) -- above the 20% flag threshold.")
        if per_type[a]["top"] and per_type[a]["top"] == per_type[b]["top"]:
            warnings.append(
                f"{a.upper()} and {b.upper()} top selections are IDENTICAL, in the same order.")

    lines = ["🔎 **PROP TIER OVERLAP DIAGNOSTIC**",
             "Total props analyzed (typed): %d" % total_typed]
    for t in _OVERLAP_TYPES:
        lines.append("  %s: %d on board, %d ranked, top-%d shown" %
                      (t.upper(), per_type[t]["on_board"], per_type[t]["ranked"], limit))
    lines.append("Overlap counts (shared picks in each pair's top-%d):" % limit)
    for k, v in overlaps.items():
        lines.append("  %s: %d" % (k, v))
    if warnings:
        lines.append("⚠️ WARNINGS:")
        lines.extend("  - %s" % w for w in warnings)
    else:
        lines.append("No overlap warnings -- each type's top picks are distinct.")

    return {"text": "\n".join(lines), "per_type": per_type, "overlaps": overlaps,
            "warnings": warnings, "total_typed": total_typed}


def bo5_block(rec):
    """BO5-FWD-001: Bo5 forward test, rendered separately. Never blended into the records above."""
    b = rec.get("bo5")
    L = ["\U0001F3BE BO5 FORWARD TEST   (separate evidence stream — not blended into any record above)"]
    if not b or (b["n"] == 0 and b["push"] == 0):
        L.append(" no settled Bo5 props yet · all Bo5 picks \u26a0\ufe0f LOW attention · MILESTONE REVIEW #1 at 25 settled")
        return "\n".join(L)
    L.append(" Record %s · Win %s · Expected %s · Actual %d · Calibration %s"
             % (_rs(b), _pct(b["rate"]), _num(b["expected"]), b["actual"], _cal(b)))
    nm = b.get("next_milestone")
    L.append(" Settled %d · %s · all picks \u26a0\ufe0f LOW attention until review #1"
             % (b["n"], ("MILESTONE REVIEW #%d at %d settled (%d to go)" % (b["milestone_reached"] + 1, nm, nm - b["n"]))
                if nm else "all milestones (25/50/100) reached"))
    L.append(" %-16s %-7s %5s %6s %6s %7s %4s" % ("Category", "Record", "Win%", "Exp", "Act", "CalDiff", "n"))
    for c, s in sorted(b["cats"].items()):
        L.append(" %-16s %-7s %5s %6s %6d %7s %4d" % (LABEL.get(c, c), _rs(s), _pct(s["rate"]), _num(s["expected"]),
                                                      s["actual"], "—" if s["cal_wins"] is None else "%+.1f" % s["cal_wins"], s["n"]))
    for p in b["picks"][-5:][::-1]:
        L.append(" \u26a0\ufe0f BO5 SAMPLE %s %s %s %s %s %s → %s"
                 % (p["match_date"], (p["name"] or "")[:20], LABEL.get(p["category"], p["category"])[:16],
                    p["direction"], _num(p["line"]), _num(p["actual"], "%g"), "WIN" if p["hit"] else "LOSS"))
    return "\n".join(L)


def full_report(rec=None, ledger=True):
    rec = rec or records()
    L = [MARKER + "  " + datetime.now().strftime("%Y-%m-%d %H:%M") + "   settled %d" % rec["overall"]["n"], "",
         headline_block(rec), "", " " + side_by_side(rec), ""]
    L.extend(_type_lines(rec))          # APEX-TYPEREC-TRK-2026-09-09
    L.append("🔥 POTENTIAL EDGE TRACKER")
    for g in GROUPS:
        L.append(group_block(g, rec)); L.append(" " + "-" * 60)
    L.append("CATEGORY STATUS"); L.append(category_table(rec)); L.append("")
    L.append(leaderboard_block(rec)); L.append(""); L.append(watchlist_block(rec)); L.append("")
    L.append("PROBABILITY BUCKETS   (are higher-probability picks performing better?)"); L.append(bucket_block(rec)); L.append("")
    L.append("GROUP ORDERING   (is the Direct Rate Model still outperforming the rest?)"); L.append(ordering_line(rec)); L.append("")
    L.append(missing_block(rec)); L.append("")
    L.append(bo5_block(rec)); L.append("")
    L.append(" break-even 54.2% (6-pick flex) · statuses are rule-based (⚪ <25 · 🟡 25+ · 🟢 100+); none of them means proven")
    if ledger:
        L.append(""); L.append("LEDGER   (every settled pick, oldest first)")
        L.append(" %-10s %-22s %-16s %-4s %5s %-4s %6s %6s  %s" % ("date", "player", "category", "tier", "prob", "dir", "line", "actual", "result"))
        for p in rec["overall"]["picks"]:
            L.append(" %-10s %-22s %-16s %-4s %5s %-4s %6s %6s  %s" % (
                p["match_date"], (p["name"] or "")[:22], LABEL.get(p["category"], p["category"])[:16],
                TIER_ICON.get(p["tier"], "?"), _pct(p["prob"]), p["direction"], _num(p["line"]), _num(p["actual"], "%g"),
                "WIN" if p["hit"] else "LOSS"))
    return "\n".join(L)


def mission_control_panel(rec=None):
    """Permanent Mission Control section. Never raises."""
    try:
        rec = rec or records()
        L = [headline_block(rec, indent=""), "",
             "🔥 POTENTIAL EDGE TRACKER   settled %d   (zone: %s)" % (rec["overall"]["n"], " / ".join(LABEL[c] for c in EDGE_ZONE)),
             " " + side_by_side(rec), ""]
        for g in GROUPS:
            L.append(group_block(g, rec)); L.append(" " + "-" * 60)
        L.append("CATEGORY STATUS"); L.append(category_table(rec)); L.append("")
        L.append(leaderboard_block(rec)); L.append(""); L.append(watchlist_block(rec)); L.append("")
        L.append("PROBABILITY BUCKETS"); L.append(bucket_block(rec))
        L.append("GROUP ORDERING"); L.append(ordering_line(rec)); L.append("")
        L.append(missing_block(rec))
        L.append(" Rules: ⚪ <25 settled · 🟡 25+, above 54.2% and groups in research order · 🟢 100+, above break-even, calibrated")
        return "\n".join(L)
    except Exception as e:
        return "POTENTIAL EDGE TRACKER — unavailable (%s)" % e


ATTN_HIGH = 0.58     # power-play break-even; zone category at or above this = HIGH


def attention(card_category, prob, rec):
    """(level, reason) for one card: the category's standing plus this pick's own probability."""
    in_zone = card_category in EDGE_ZONE
    if prob is None:
        return ("MEDIUM" if in_zone else "LOW", "zone category" if in_zone else "outside the zone")
    if in_zone and prob >= ATTN_HIGH:
        return ("HIGH", "zone category and %.0f%% clears the power-play break-even (%.0f%%)" % (100 * prob, 100 * ATTN_HIGH))
    if in_zone and prob > BREAK_EVEN:
        return ("MEDIUM", "zone category, %.0f%% is above break-even (%.1f%%) but under %.0f%%" % (100 * prob, 100 * BREAK_EVEN, 100 * ATTN_HIGH))
    if in_zone:
        return ("LOW", "zone category, but %.0f%% is below break-even (%.1f%%) — log only" % (100 * prob, 100 * BREAK_EVEN))
    return ("LOW", "outside the zone" + (" — %.0f%%" % (100 * prob)))


def discord_header(card_category=None, rec=None, prob=None, bo5=False):
    """Two lines at the top of an engine card, about THIS card. The daily rate-model
    headline is NOT repeated here -- it lives on Mission Control and the daily post."""
    try:
        rec = rec or records()
        c = card_category
        if not c:
            return ""
        cs = rec["cats"].get(c) or _stat([])
        cs["status"] = cs.get("status") or cat_status(cs)
        rm = rec["rate_model"]
        if c in EDGE_ZONE:
            l1 = "🔥 **IN THE EDGE ZONE** — %s · rate model %s (%s) · %s so far %s · %s" % (
                LABEL.get(c, c), _rs(rm), _pct(rm["rate"]), LABEL.get(c, c), _rs(cs),
                ("⚪ early, n=%d" % rm["n"]) if rm["n"] < EARLY_MIN else STATUS[rm["status"]])
        else:
            g = group_of(c); gr = rec[g]
            tail = " · market territory" if g == "total_games" else ""
            l1 = "%s **OUTSIDE THE ZONE** — %s · %s %s (%s) · %s%s" % (
                GROUPS[g]["icon"], LABEL.get(c, c), GROUPS[g]["title"].lower() if g != "total_games" else "total games",
                _rs(gr), _pct(gr["rate"]), cstat_text(cs), tail)
        lvl, why = attention(c, prob, rec)
        if bo5:
            lvl, why = "LOW", "BO5-FWD-001 -- Bo5 experimental stream, forced LOW until 25 settled"

        icon = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "⚪"}[lvl]
        l0 = "%s **ATTENTION: %s** — %s" % (icon, lvl, why)
        return "\n".join([l0, l1, "────────────────────────"])
    except Exception:
        return ""


def discord_summary(rec=None):
    """Tracker summary for #systems, split into <=1900-char messages."""
    rec = rec or records()
    body = [headline_block(rec, indent="", bold=True), "",
            "🔥 **POTENTIAL EDGE TRACKER** — settled %d — %s" % (rec["overall"]["n"], datetime.now().strftime("%m-%d %H:%M CT")),
            side_by_side(rec), ""]
    for g in GROUPS:
        body.append(group_block(g, rec, indent="")); body.append("──────────")
    body.append("**Category status**"); body.append("```\n" + category_table(rec) + "\n```")
    body.append(leaderboard_block(rec, indent="")); body.append(""); body.append(watchlist_block(rec, indent="")); body.append("")
    body.append("**Probability buckets**"); body.append(bucket_block(rec).strip())
    body.append("**Ordering**"); body.append(ordering_line(rec).strip())
    body.append("**Missing data**"); body.append(missing_block(rec, indent="").strip())
    text = "\n".join(body)
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur); cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    return chunks


def post_to_discord(channel="system"):
    try:
        import openclaw_core as core
    except Exception as e:
        return "could not import openclaw_core (%s)" % e
    n = 0
    for chunk in discord_summary():
        try:
            core.send_discord(channel, chunk); n += 1
        except Exception as e:
            return "posted %d chunk(s) then failed: %s" % (n, e)
    return "posted %d chunk(s) to %s" % (n, channel)


# ------------------------------------------------------------------ write hand actuals into the log
def _settle_pair(player, opponent):
    """APEX-SETTLEKEY: unordered name set. Match-level rows keep the pair in
    `opponent` ("A v B"); player rows are simply {player, opponent}."""
    p = _norm(player); o = str(opponent or "")
    if p == "match" or " v " in o or " vs " in o:
        for sep in (" vs ", " v. ", " v "):
            if sep in o:
                a, b = o.split(sep, 1)
                return frozenset([_norm(a), _norm(b)])
    return frozenset([p, _norm(o)])


def _pair_eq(pa, pb):
    """Equal, or equal allowing one truncated name (token subset either way)."""
    if pa == pb:
        return True
    if len(pa) != 2 or len(pb) != 2:
        return False
    la, lb = list(pa), list(pb)
    def sub(x, y):
        sx, sy = set(x.split()), set(y.split())
        return bool(sx & sy) and (sx <= sy or sy <= sx)
    return ((sub(la[0], lb[0]) and sub(la[1], lb[1])) or
            (sub(la[0], lb[1]) and sub(la[1], lb[0])))


def settle_log(sim_log=SIM_LOG, hand_log=HAND_LOG, dry=False):
    rows = _read(sim_log)
    if not rows:
        return "forward log not found or empty"
    hand = [r for r in _read(hand_log) if str(r.get("in_forward_log", "0")).strip() == "1"]
    n = 0; amb = []
    for h in hand:
        line = _f(h.get("line")); actual = _f(h.get("actual"))
        if actual is None:
            continue   # APEX-SETTLELOG-001: VOID/no-result rows carry no actual by design
        hpair = _settle_pair(h.get("player"), h.get("opponent"))
        hdate = (h.get("match_date") or "").strip()
        cands = []
        for r in rows:
            if r.get("actual") or r.get("category") != h["category"] or _f(r.get("line")) != line:
                continue
            if (r.get("match_date") or "").strip() != hdate:
                continue
            if not _pair_eq(hpair, _settle_pair(r.get("player"), r.get("opponent"))):
                continue
            cands.append(r)
        if not cands:
            continue
        exact = [r for r in cands if _norm(r.get("player")) == _norm(h.get("player"))]
        if len(exact) == 1:
            r = exact[0]
        elif len(cands) == 1:
            r = cands[0]
        else:
            amb.append("%s %s %s line=%s -> %d candidates, REFUSED"
                       % (hdate, h.get("player"), h.get("category"), h.get("line"), len(cands)))
            continue
        r["actual"] = ("%.1f" if h["category"] == "fantasy_score" else "%.0f") % actual
        r["settled_ts"] = datetime.now().isoformat(timespec="seconds") + "-hand"
        n += 1
    if n and not dry:
        fields = list(rows[0].keys())
        with open(sim_log, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(rows)
    msg = "settled %d row(s) from hand grades%s" % (n, " (dry run)" if dry else "")
    if amb:
        msg += "\n%d AMBIGUOUS, nothing written:\n  " % len(amb) + "\n  ".join(amb)
    return msg


# ------------------------------------------------------------------ selftest
def selftest():
    fails = []
    picks = settled_picks(sim_log="/nonexistent", pick_log="/nonexistent", hand_log=HAND_LOG)
    rec = records(picks)
    for g, (w, l) in (("rate_model", (4, 1)), ("sim_derived", (2, 4)), ("total_games", (7, 8)), ("overall", (13, 13))):
        if (rec[g]["w"], rec[g]["l"]) != (w, l):
            fails.append("%s expected %d-%d got %d-%d" % (g, w, l, rec[g]["w"], rec[g]["l"]))
    if any(rec[g]["status"] != "none" for g in GROUPS):
        fails.append("all groups should be ⚪ below 25 settled")
    if rec["top"] != "dfs" or rec["worst"] != "games_won":
        fails.append("TOP/WORST expected dfs/games_won got %s/%s" % (rec["top"], rec["worst"]))
    if abs(rec["overall"]["expected"] - 14.79) > 0.05:
        fails.append("expected wins should be ~14.79, got %.2f" % rec["overall"]["expected"])
    b = {n: (w, l) for n, w, l, _ in rec["buckets"]}
    if b["70%+"] != (2, 1) or b["50-55"] != (4, 6):
        fails.append("buckets off: %s" % b)
    if rec["ordering"] is not True:
        fails.append("ordering should hold on the 08-27 tally")
    if any(p["tier"] not in TIER_ICON for p in picks):
        fails.append("every pick needs a tier")
    def synth(n, rate, prob, cat):
        return [{"category": cat, "tier": "good", "line": 1.5, "direction": "MORE", "prob": prob, "match_date": "2026-01-%02d" % (i % 28 + 1),
                 "settled_ts": "", "actual": 2 if i < int(n * rate) else 1, "hit": i < int(n * rate), "name": "x"} for i in range(n)]
    r2 = records(synth(30, 0.60, 0.58, "aces") + synth(30, 0.50, 0.55, "games_won") + synth(30, 0.45, 0.55, "total_games"))
    if r2["rate_model"]["status"] != "early": fails.append("30 @60% should be 🟡")
    if r2["sim_derived"]["status"] != "none": fails.append("50% hit should stay ⚪")
    r3 = records(synth(30, 0.60, 0.58, "aces") + synth(30, 0.70, 0.55, "games_won"))
    if r3["rate_model"]["status"] != "none": fails.append("ordering rule should block 🟡")
    r4 = records(synth(120, 0.60, 0.58, "aces"))
    if r4["rate_model"]["status"] != "emerging": fails.append("120 @60% calibrated should be 🟢")
    r5 = records(synth(120, 0.56, 0.62, "aces"))
    if r5["rate_model"]["status"] != "early": fails.append("overconfident should stay 🟡")
    import tempfile
    d = tempfile.mkdtemp(); sl = os.path.join(d, "sim.csv")
    with open(sl, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["match_id", "match_date", "player", "opponent", "category", "line", "p_over", "p_under", "actual", "settled_ts"])
        w.writeheader()
        w.writerow({"match_id": "x", "match_date": "2026-08-25", "player": "match", "opponent": "Moyuka Uchijima v Polona Hercog",
                    "category": "total_games", "line": "18.5", "p_over": "0.67", "p_under": "0.33", "actual": "18", "settled_ts": "t"})
    p2 = settled_picks(sim_log=sl, pick_log="/nonexistent", hand_log=HAND_LOG)
    if sum(1 for p in p2 if p["category"] == "total_games" and p["line"] == 18.5 and "uchijima" in _norm(p["name"])) != 1:
        fails.append("dedupe failed")
    cs = rec["cats"]
    if cs["total_games"]["status"] != "under": fails.append("total games (n=15, actual<expected) should be ⚠️, got %s" % cs["total_games"]["status"])
    if cs["dfs"]["status"] != "insuff" or cs["games_won"]["status"] != "insuff": fails.append("n<5 / n<10 categories should be ⚪")
    if cs["fantasy_score"]["status"] != "never": fails.append("fantasy should be 🚫 NEVER TESTED")
    if rec["leaderboard"][0] != "dfs" or rec["leaderboard"][-1] != "games_won": fails.append("leaderboard order wrong: %s" % rec["leaderboard"])
    if not rec["watchlist"] or rec["watchlist"][0] != "games_won": fails.append("watchlist should lead with games_won: %s" % rec["watchlist"])
    if rec["headline"] != "rate_model": fails.append("headline should be rate_model, got %s" % rec["headline"])
    synth_ok = _stat([{"category": "aces", "tier": "good", "line": 1, "direction": "MORE", "prob": 0.58, "match_date": "", "settled_ts": "",
                       "actual": 2 if i < 18 else 0, "hit": i < 18, "name": "x"} for i in range(30)])
    synth_ok["status"] = cat_status(synth_ok)
    if synth_ok["status"] != "signal": fails.append("30 settled, 60%%, stated 58%% should be 🔥, got %s" % synth_ok["status"])
    synth_neg = _stat([{"category": "aces", "tier": "good", "line": 1, "direction": "MORE", "prob": 0.58, "match_date": "", "settled_ts": "",
                        "actual": 2 if i < 13 else 0, "hit": i < 13, "name": "x"} for i in range(30)])
    if cat_status(synth_neg) != "neg": fails.append("30 settled at 43%% should be ❌")
    if "overall" in standing_report(rec).lower() and "|" not in standing_report(rec): fails.append("overall shown alone")
    cv = rec["coverage"]
    unc = [c for c, _, _ in cv["unchecked"]]
    if not all(c in unc for c in ("fantasy_score", "sets_won", "total_sets", "tiebreaks", "first_set_fav_wins")):
        fails.append("coverage should list the never-settled categories as unchecked: %s" % unc)
    if any(c in unc for c in ("aces", "breaks", "dfs", "games_won", "total_games")):
        fails.append("coverage wrongly lists a settled category as unchecked")
    if "MISSING DATA / UNCHECKED" not in mission_control_panel(rec) or "NEVER TESTED" not in full_report(rec):
        fails.append("missing-data section not rendered")
    for txt, need in ((full_report(rec), "TOP CATEGORY"), (full_report(rec), "LEDGER"), (mission_control_panel(rec), "PROBABILITY BUCKETS"),
                      (discord_header("aces", rec), "IN THE EDGE ZONE"), (discord_header("total_games", rec), "OUTSIDE THE ZONE"),
                      (discord_header("dfs", rec, prob=0.51), "⚪ **ATTENTION: LOW**"), (discord_header("dfs", rec, prob=0.61), "🟢 **ATTENTION: HIGH**"),
                      (discord_header("aces", rec, prob=0.55), "🟡 **ATTENTION: MEDIUM**"), (discord_header("games_won", rec, prob=0.79), "⚪ **ATTENTION: LOW**"),
                      (discord_header("dfs", rec, prob=0.61).splitlines()[0], "ATTENTION"),
                      (mission_control_panel(rec).splitlines()[0], "DIRECT RATE MODEL"),
                      ("\n".join(discord_summary(rec)), "Ordering")):
        if need not in txt: fails.append("rendering missing %r" % need)
    if any(len(c) > 2000 for c in discord_summary(rec)): fails.append("discord chunk over 2000 chars")
    scrub = (full_report(rec) + mission_control_panel(rec)).lower()
    for w_ in ("potential edge", "edge zone", "edge tracker", "apex-edge-tracker", "ledger", "edge signal"): scrub = scrub.replace(w_, "")
    if "DIRECT RATE MODEL" in discord_header("dfs", rec, prob=0.5): fails.append("the daily headline must not repeat on cards")
    if "edge" in scrub: fails.append("the word 'edge' appears outside the section titles")
    # ---- BO5-FWD-001 checks
    rb = records(synth(30, 0.60, 0.58, "aces") + [dict(category="aces", tier="good", line=9.5, direction="MORE",
                 prob=0.60, match_date="2026-08-29", settled_ts="", actual=12, hit=True, name="b5", bo5=True)])
    if rb["overall"]["n"] != 30: fails.append("Bo5 pick leaked into the Bo3 overall record")
    if rb["bo5"]["n"] != 1 or rb["bo5"]["w"] != 1: fails.append("Bo5 stream stats wrong: %s" % rb["bo5"]["n"])
    _fr = full_report(rb, ledger=False)
    if "BO5 FORWARD TEST" not in _fr: fails.append("BO5 section missing from full report")
    if "BO5 SAMPLE" not in _fr: fails.append("BO5 SAMPLE tag missing")
    import tempfile as _tf, csv as _csv, os as _os
    _hp = _os.path.join(_tf.mkdtemp(), "hand.csv")
    _hdr = ["match_date", "player", "opponent", "category", "tier", "line", "direction", "prob", "actual",
            "best_of", "in_forward_log", "source", "note"]
    def _wh(rows_):
        with open(_hp, "w", newline="") as fh_:
            w_ = _csv.DictWriter(fh_, fieldnames=_hdr); w_.writeheader()
            for rr in rows_: w_.writerow(rr)
    _b = {"opponent": "o", "tier": "good", "direction": "MORE", "prob": "0.6", "in_forward_log": "1", "source": "hand", "note": ""}
    _wh([dict(_b, match_date="2026-08-29", player="b5p", category="aces", line="10.5", actual="12", best_of="5"),
         dict(_b, match_date="2026-08-20", player="oldp", category="aces", line="5.5", actual="7", best_of="")])
    _pk = settled_picks(sim_log="/nonexistent", pick_log="/nonexistent", hand_log=_hp)
    if not any(p["bo5"] for p in _pk if p["name"] == "b5p"): fails.append("hand best_of=5 not read as Bo5")
    if any(p["bo5"] for p in _pk if p["name"] == "oldp"): fails.append("legacy blank best_of should default to Bo3")
    _wh([dict(_b, match_date="2026-08-29", player="newp", category="aces", line="5.5", actual="7", best_of="")])
    try:
        settled_picks(sim_log="/nonexistent", pick_log="/nonexistent", hand_log=_hp)
        fails.append("new-dated hand row without best_of was accepted")
    except SystemExit:
        pass
    # ---- APEX-BB-001 checks
    r_bb = records(synth(30, 0.62, 0.55, "dfs") + synth(30, 0.45, 0.55, "total_games"))
    if not apex_score(0.60, "dfs", False, r_bb)[0] > apex_score(0.60, "breaks", False, r_bb)[0]:
        fails.append("BB: evidenced category must outrank empty category at equal probability")
    s5 = apex_score(0.80, "aces", True, r_bb)
    if s5[0] > 49 or s5[2] != "LOW": fails.append("BB: Bo5 must cap <=49 and force LOW")
    _blk = best_bets_block([
        {"player": "AA", "category": "dfs", "prob": 0.62, "line": 2.5, "direction": "MORE", "bo5": False},
        {"player": "BB", "category": "total_games", "prob": 0.55, "line": 22.5, "direction": "MORE", "bo5": False},
        {"player": "ZZBO5", "category": "aces", "prob": 0.80, "line": 5.5, "direction": "MORE", "bo5": True}], rec=r_bb)
    for need in ("APEX BEST BETS", "BEST 3", "LOW PRIORITY", "APEX SCORE"):
        if need not in _blk: fails.append("BB render missing %r" % need)
    if "ZZBO5" in _blk.split("BEST 3")[1].split("LOW PRIORITY")[0]:
        fails.append("BB: Bo5 pick leaked into Best 3")
    if "ATTENTION: LOW" not in discord_header("aces", rec=r_bb, prob=0.75, bo5=True):
        fails.append("BB: header must force LOW on Bo5")
    if "ZZBO5" in _blk.splitlines()[1]:
        fails.append("BB-001b: Bo5 pick must never top the ranked board")
    if apex_score(0.63, "dfs", False, r_bb)[0] < 50:
        fails.append("BB-001b: a 63%% pick in an evidenced category should score >= 50")
    # ---- APEX-BB-002a checks
    import tempfile as _tf2, csv as _csv2
    _bp = os.path.join(_tf2.mkdtemp(), "picks.csv")
    _bh = ["ts", "match_id", "match_date", "player", "opponent", "category", "line", "direction", "prob", "best_of"]
    with open(_bp, "w", newline="") as fh2:
        w2 = _csv2.DictWriter(fh2, fieldnames=_bh); w2.writeheader()
        w2.writerow({"ts": "T1", "match_id": "m1", "match_date": "2026-08-29", "player": "P_old", "opponent": "O",
                     "category": "dfs", "line": "2.5", "direction": "MORE", "prob": "0.55", "best_of": "3"})
        w2.writerow({"ts": "T2", "match_id": "m1", "match_date": "2026-08-29", "player": "P_old", "opponent": "O",
                     "category": "dfs", "line": "3.5", "direction": "MORE", "prob": "0.62", "best_of": "3"})
        w2.writerow({"ts": "T1", "match_id": "m2", "match_date": "2026-08-29", "player": "P_b5", "opponent": "O2",
                     "category": "aces", "line": "9.5", "direction": "MORE", "prob": "0.70", "best_of": "5"})
        w2.writerow({"ts": "T1", "match_id": "m3", "match_date": "2026-08-01", "player": "P_stale", "opponent": "O3",
                     "category": "aces", "line": "5.5", "direction": "MORE", "prob": "0.60", "best_of": "3"})
    _bd, _bn = load_current_board(today="2026-08-29", path=_bp)
    if _bn or len(_bd) != 2: fails.append("BB2: board load/dedupe/stale-filter wrong (%s, n=%d)" % (_bn, len(_bd)))
    if not any(p["line"] == "3.5" for p in _bd): fails.append("BB2: dedupe kept the older row")
    _dt = best_bets_detail(_bd, rec=r_bb)
    for need in ("APEX BEST BETS", "🥇", "BO5 SAMPLE", "Why Apex likes it", "APEX SCORE"):
        if need not in _dt: fails.append("BB2 render missing %r" % need)
    _dc = best_bets_detail(_bd, category="dfs", rec=r_bb)
    if "P_b5" in _dc or "BEST DOUBLE FAULT" not in _dc: fails.append("BB2: category filter leaked")
    if "no active" not in best_bets_detail(_bd, category="breaks", rec=r_bb): fails.append("BB2: empty category must say so")
    _dw = best_bets_detail(_bd, worst=True, rec=r_bb)
    if "LOW PRIORITY" not in _dw or "Why ranked low" not in _dw: fails.append("BB2: worst mode wrong")
    _bd2, _bn2 = load_current_board(today="2026-09-15", path=_bp)
    if _bd2 or "no active board" not in _bn2: fails.append("BB2: stale board must return a note, not rankings")
    print(MARKER, "selftest", "PASS" if not fails else "FAIL")
    for f in fails: print("  -", f)
    return not fails


if __name__ == "__main__":
    a = sys.argv[1:] or ["--report"]
    if "--selftest" in a:
        sys.exit(0 if selftest() else 1)
    if "--settle-log" in a:
        print(settle_log(dry="--dry-run" in a))
    if "--report" in a or "--settle-log" in a:
        print(full_report(ledger="--no-ledger" not in a))
    if "--mc" in a:
        print(mission_control_panel())
    if "--discord" in a:
        print(discord_header())
    if "--post" in a:
        print(post_to_discord())
    if "--republish" in a:   # APEX-REPUBLISH-2026-10-10
        print(republish_boards(source="manual-republish"))
    if "--fit-correction" in a:   # APEX-HONESTPROB-2026-10-10
        print(fit_prob_correction())

# APEX-BB-001b applied

# APEX-BB-002c applied
