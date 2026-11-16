# Apex Data Pipeline — Match Database Refresh
Project Apex Intelligence / OpenClaw (DEMETRIUS) — standing maintenance procedure
Written 2026-08-22. Refresh script: `apex_data_refresh.sh` (marker APEX-DATA-REFRESH-2026-08-22)

## 1. What the folder is

The folder `ATP & WTA, 1968-2026` in the project root is **not** Jeff Sackmann's repo.
It is the TennisMyLife match database, downloaded as a single "Download All" zip from
https://stats.tennismylife.org/tennis-match-database on 2026-08-06 (the folder name
is the site's own heading). It uses Sackmann's column layout — which is why every
script calls it "Sackmann" — but with ATP player IDs, daily updates, and WTA files
added by the site from CSVs available online.

## 2. How it is updated

- **Method:** manual download. There is no project script, no scheduled job, and no
  external service feeding this folder. Until today the only refresh was the original
  download. That is why every model ended at 2026-08-03.
- **From now on:** `bash apex_data_refresh.sh` is the standing step (section 4).

## 3. Source of truth

| Folder file | What it holds | Used by the models? |
|---|---|---|
| `YYYY.csv` | ATP main tour, finished tournaments | **Yes** |
| `YYYY_wta.csv` | WTA main tour, finished tournaments | **Yes** |
| `YYYY_challenger.csv` | ATP Challenger | Loaded only with `--include-challenger`; all live models exclude it |
| `ongoing_tourneys.csv` | ATP tournament(s) in progress | No |
| `wta_ongoing_tourneys.csv` | WTA in progress | No |
| `challenger_ongoing_tourneys.csv` | Challengers in progress | No |
| `ATP_Database.csv`, `atp_matches_amateur.csv`, qualifying files | reference only | No |

Key behaviour: a tournament sits in the *ongoing* file until it finishes, then moves
into the year file. The models only read year files, so a tournament in progress is
never in the replay. "Latest tourney_date" therefore means the most recent **finished**
tournament's start week, not today's date. Example: on 2026-08-22 Cincinnati is in
progress, so the year file ends with the Toronto/Montreal week (08-02/08-03). After
Cincinnati finishes it appears, and the US Open (main draw from 08-31) only appears
after it finishes.

Upstream URLs (all under `https://stats.tennismylife.org/data/`):
`2026.csv`, `2026_wta.csv`, `2026_challenger.csv`, `ongoing_tourneys.csv`,
`wta_ongoing_tourneys.csv`, `challenger_ongoing_tourneys.csv`. Older years follow the
same pattern. Full zip: `https://stats.tennismylife.org/api/download-all`.

## 4. Refresh procedure (standard step)

Run from the project folder. Refreshes current-year ATP, WTA and Challenger files plus
the three ongoing files; keeps a dated backup under `ATP & WTA, 1968-2026/_backup_*`;
prints row count and latest tourney_date before and after each file.

```
cd "/path/to/home/openclaw/"
bash apex_data_refresh.sh
```

To also pick up corrections to a prior year: `bash apex_data_refresh.sh 2025 2026`.

**Verify:** every year file should show `OK` with an `after` row count ≥ `before`, and a
latest tourney_date ≥ the previous one. A `FAIL` or `SKIP` line means the old file was
kept — safe, but rerun later. If the site changes its URL scheme the script fails
loudly rather than writing a half file.

Note: the previous download's `wta_ongoing_tourneys.csv` had no header row. The script's
header check skips such files; the models never read them anyway.

## 5. After every refresh — mandatory rebuild chain

```
python3 apex_prop_model_v1.py --refresh-state
python3 fragility_state.py --build --no-challengers --year-from 2005
python3 fragility_state.py --validate
```

- `--refresh-state` prints the latest date loaded; it must match the refreshed file.
- The fragility `--build` deliberately removes `fragility_features.ok`; `--validate`
  re-arms it. Never run one without the other.
- Frozen models (`fragility_v3_model.json`, `apex_prop_model_v1.json`) are **not**
  touched by a refresh. `apex_prop_model_v1.py --build` refuses while frozen.
- `apex_rows_cache.pkl` / `apex_batch_a_rows.csv` are research-era caches built by
  `apex_combined_model.py` and `apex_batch_a.py`; they are NOT rebuilt by a refresh and
  do not need to be for live operation.

## 6. Cadence

- Before any tournament you will evaluate: refresh once the *previous* tournament has
  finished (so its matches have moved into the year file).
- During the US Open: refresh after each finished prior event; the Open's own matches
  arrive only after the final, so forward-log settlement (`--settle-sackmann`) for Open
  matches runs after 09-13.
- Weekly minimum, Monday morning, regardless.

## 7. Caveats worth remembering

- WTA files are built by the site from other public CSVs; the site itself does not
  guarantee them to the same standard as ATP. Treat any WTA oddity as a data question
  before a model question.
- Name spellings in this folder are what `--project` resolves against. If a player is
  "unknown", check the year file for the site's spelling before assuming a bug.
