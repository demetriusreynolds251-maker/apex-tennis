# Apex testing standard (agreed Nov 4, 2026, "Go testing standard")

Why: on Nov 4 three different rules (women's 17.5 to BACKUPS, Win Prob first, 75 floor) all came out within one day of the boards as posted. One leg flips one day. Tests run on the same 12 days, one rule at a time, keep passing by luck and then fail the next week.

Every rule test, from now on:

1. **Window:** Oct 13, 2026 onward, growing every day. Never only the last 12 days.
2. **Three numbers, always:** per-pick hit record (won-lost), whole-board days won (Top 3, Elite 5, Elite 6), and both halves of the window (older half / newer half).
3. **Pass rule:** a change passes only if it wins on picks AND does not lose days in either half.
4. **Together, not one by one:** when more than one rule is on the table, test them as a set too, because they interact.
5. **No live change on a one-day difference.** It needs at least two days better over the full window.
6. **Grading comes first.** Results come from the board history and the props database, with a warehouse-score fallback. No hand grades inside a test.
7. **Replays are labeled.** "Real posted boards since the rule went live" is the gold standard. A replay of older days says so, and says which rules were live that day (the signal ranking only went live midday Oct 22, so Oct 22 is Win-Prob order).
8. **One script.** Every test runs through tools/apex_replay.py so the method is the same each time:

    python3 tools/apex_replay.py                      # boards as posted, full window
    python3 tools/apex_replay.py --rule myrule.py     # myrule.py defines keep(pick) and/or key(pick)

Win rates are whole-slip. Per-pick records are for finding ideas; days won decide.
