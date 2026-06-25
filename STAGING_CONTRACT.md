# Staging contract — for the `fifawc2026` PWA repo

This document is the handoff between the warehouse repo at `E:/fc2026_notebooks/` and the audit-app PWA at `E:/fifawc2026/`. Read this first if you're the agent maintaining the PWA — it tells you exactly which files to read, what they contain, and how fresh they are.

**Owning repo**: `E:/fc2026_notebooks/`
**Consuming repo**: `E:/fifawc2026/`
**Last contract change**: 2026-06-21 — staging tables introduced.

---

## 1. Read these five parquets. Stop reading the 28 source parquets.

| Parquet | Grain | Rows today | Key |
|---|---|---:|---|
| `data/processed/wc26_stg_nations.parquet` | 1 row / nation | 48 | `nation_id` (3-letter FIFA code) |
| `data/processed/wc26_stg_stadiums.parquet` | 1 row / stadium | 16 | `stadium_id` |
| `data/processed/wc26_stg_matches.parquet` | 1 row / fixture | 104 | `espn_match_id` (also `fifa_match_id`) |
| `data/processed/wc26_stg_referee_profile.parquet` | 1 row / (ref, window) | 250 | `(referee_id, window)` |
| `data/processed/wc26_stg_players.parquet` | 1 row / player (wide, ~190 cols) | 1,248 | `fifa_player_id` |
| `data/processed/wc26_stg_players_view.parquet` | 1 row / player (slim, ~115 picked + 14 derived) | 1,248 | `fifa_player_id` |
| `data/processed/wc26_stg_fantasy_player_totals.parquet` | 1 row / fantasy player | ~(grows with rounds played) | `fantasy_player_id` |
| `data/processed/wc26_stg_player_powerrank.parquet` | 1 row / (player, team) | ~(grows with matches) | `(fifa_player_id, fifa_team_id)` |

**The 28 source parquets (`wc26_matches`, `wc26_player_enrichment`, etc.) are now considered internal to the warehouse build.** The PWA should not read them directly — every column the PWA needs has been promoted into one of the five `wc26_stg_*` tables above, sometimes prefixed, sometimes aggregated. If something is genuinely missing, file a request to extend the staging table rather than reaching into a source parquet.

Why: the staging layer is a stable contract. Source-parquet column names can churn on upstream notebook edits (rename, dtype changes, schema additions); the staging layer absorbs that churn so the PWA doesn't have to keep up.

---

## 2. Freshness model

| Bucket | Notebooks (in order) | Cron | Latency to "truth" |
|---|---|---|---|
| Frozen | `01_nations`, `02_stadiums` | manual | seconds (only changes on schema edit) |
| Daily | `06_players`, `04_referees` | 07:00 UTC | up to 24 h on slow-moving fields |
| **Hourly (every 3 h)** | `03_matches`, `05_referee_assignments`, `07_player_match_stats`, `08_player_enrichment`, `09_player_season_stats`, `10_fifa_fantasy`, `11_fotmob_wc_and_form`, `12_match_weather`, `13_polymarket`, `14_staging_core`, `15_staging_matches`, `16_staging_players` | `0 */3 * * *` | **3 h max** |

The three staging notebooks (`14`, `15`, `16`) run last on every hourly tick, so the five `wc26_stg_*` parquets are always consistent with whatever just landed upstream — never a torn read.

**Tournament window** (2026-06-11 → 2026-07-20): `refresh.py` auto-sets `FORCE_REFRESH=1`, which makes `cache_raw` and `latest_raw` ignore any cached file that's older than today's date prefix. Pre/post-tournament: no auto-force — use `--force-refresh` explicitly if you need fresh fetches.

Special case for notebook 10 (FIFA Fantasy per-player stats — 1488 calls): refresh is *per-player mtime-vs-match-end-timestamp* so it only re-pulls players whose squad has played since their cache file was written. Typical hourly tick fetches ~50–250 players, not 1488. See [REFRESH.md](REFRESH.md) for the heuristic.

---

## 3. Reading the parquets from the PWA

```ts
// node-side; e.g. inside a Next.js loader or a build-time script
import { readParquet } from 'parquetjs-lite';  // or your library of choice
const players = await readParquet('E:/fc2026_notebooks/data/processed/wc26_stg_players.parquet');
```

For Python tooling:
```python
import pandas as pd
players = pd.read_parquet('E:/fc2026_notebooks/data/processed/wc26_stg_players.parquet')
```

Or for quick eyeballing, the same five tables are also written as `.csv` alongside each `.parquet` (see [lib/io.py:save_table](lib/io.py)).

---

## 4. Schema highlights per staging table

The full per-column dictionary is in `WC26_DATA_DICTIONARY.xlsx` — open it and jump to the matching sheet (`wc26_stg_players`, etc.). Below is the **shape** so you can decide what to read.

### `wc26_stg_nations` (48 × 21)
Direct pass-through of `wc26_nations`. Carries cross-source team IDs (`espn_team_id`, `fotmob_team_id`, `tm_team_id`), `confederation`, `group`, `pot`, `fifa_rank`, `squad_valuation_m_eur`, `is_host`, `all_names` alias array.

### `wc26_stg_stadiums` (16 × 31)
Base venue fields (`capacity`, `roof_type`, `surface`, lat/lng, timezone, altitude). Plus per-stadium aggregates:
- From `wc26_matches`: `match_days`, `matches_total`, `matches_completed`, `total_attendance_so_far`.
- From `wc26_match_weather`: `max_temperature_c` / `min_temperature_c`, `max_apparent_temperature_c` / `min_apparent_temperature_c`, plus means with `avg_` prefix on humidity, dew point, precipitation, rain, wind speed, cloud cover.

### `wc26_stg_referee_profile` (250 × ~25)
Long format. Each row is one (referee, window) pair — windows are `career`, `last_5`, `last_10`, `last_15`, `last_25`. Carries discipline rates (`yellow_pg`, `red_pg`, `penalty_pg`, `fouls_pg`) and totals. Identity columns (`name`, `country`, `confederation`, `flag_iso`, `nation_id`, `fm_id`, `slug`, `fm_url`) are joined from `referee_master`.

### `wc26_stg_matches` (104 × 67)
Base fixture columns (kickoff, score, stage, status, attendance) plus joins:
- **Stadium fields**: `stadium_capacity`, `roof_type`, `surface`.
- **Weather**: `local_date`, `local_hour`, `temperature_c`, `apparent_temp_c`, `humidity_pct`.
- **Home nation fields** (prefixed `home_`): `confederation`, `group`, `pot`, `fifa_rank`, `squad_valuation_m_eur`, `is_host`, `espn_team_id`, `fotmob_team_id`, `tm_team_id`, `all_names`. Same for **away** with `away_` prefix.
- **Referee fields** (prefixed `referee_`): joined via `ref_id_bridge` (see §5). Carries `referee_id`, `confederation`, `flag_iso`, `nation_id`, `fm_id`, `slug`, `fm_url`. NaN if the FIFA OfficialId doesn't resolve to a WC26 panel ref (4th officials, VARs).
- **Polymarket volume**: `volume_moneyline` (= moneyline + draw), `volume_other` (over/under + spreads + goalscorer).

### `wc26_stg_players` (1,248 × 190)
The big one. Five column families with non-overlapping prefixes:

| Prefix | Source | Cols |
|---|---|---|
| (unprefixed) | `wc26_player_enrichment` base | ~29 (identity, bio, current club, cross-source IDs) |
| `club_*` | `wc26_player_career_club_summary` | youth + senior career totals across clubs |
| `national_*` | `wc26_player_career_national_summary` | youth + senior career totals for national team |
| `value_*` | `wc26_player_market_value_summary` | FotMob + TM latest + peak market values (EUR) |
| `fotmob_wc_*` | `wc26_player_fotmob_wc` | 19 WC tournament stats per FotMob (rating, chances_created, dribbles, duels_won, etc.) |
| `fifa_wc_*` | aggregated from `wc26_player_match_stats_wide` | 50 SUM + 2 Avg + 1 MAX = 53 stat cols across the player's WC matches |
| `recent5_*`, `recent10_*`, `recent15_*`, `recent20_*` | aggregated from `wc26_player_recent_matches_fotmob` | 4 windows × 10 cols (matches_played, minutes_played, goals, assists, yellow/red cards, fotmob_rating, player_of_the_match, started_pct, has_data sentinel) |

Plus three list-typed columns: `stages_played`, `opponents`, `match_ids` (sorted unique values across the player's WC matches).

The `recent{N}_has_data` boolean sentinel tells you whether to trust the rating — empty windows yield 0-counters with `has_data=False`.

### `wc26_stg_players_view` (1,248 × ~129)

Slim curated view of `wc26_stg_players`. Same key (`fifa_player_id`), same row count, but ~115 hand-picked columns instead of ~190 — drops youth career fields, the obscure FIFA-WC stats, and the `recent20_*` window. Use this as the default player table; reach into `wc26_stg_players` only when you need the dropped fields.

Adds 14 **derived** columns on top of the picked subset (all `NaN`-safe; division by zero returns `NaN`):

| Column | Formula | Source ratio in screenshot |
|---|---|---|
| `fifa_wc_mid_def_reception_pct` | `ReceptionsBetweenMidfieldAndDefensiveLine / ReceivedOffersToReceive` | Mid/Def Reception % |
| `fifa_wc_attacking_reception_pct` | `ReceptionsInBehind / ReceivedOffersToReceive` | Attacking Reception % |
| `fifa_wc_under_vs_no_pressure_reception_ratio` | `ReceptionsUnderPressure / ReceptionsUnderNoPressure` | Under/No-Pressure Reception Ratio |
| `fifa_wc_reception_completion_pct` | `ReceivedOffersToReceive / OffersToReceiveTotal` | Reception Completion % |
| `fifa_wc_pass_completion_pct` | `PassesCompleted / Passes` | Pass Completion % |
| `fifa_wc_ball_progression_completion_pct` | `CompletedBallProgressions / AttemptedBallProgressions` | Ball Progression Completion % |
| `fifa_wc_switches_of_play_completion_pct` | `CompletedSwitchesOfPlay / AttemptedSwitchesOfPlay` | Switches of Play Completion % |
| `fifa_wc_cross_completion_pct` | `CrossesCompleted / Crosses` | Cross Completion % |
| `fifa_wc_distributions_under_pressure_completion_pct` | `DistributionsCompletedUnderPressure / DistributionsUnderPressure` | Distributions Under Pressure Completion % |
| `fifa_wc_linebreaks_under_pressure_proportion_pct` | `LinebreaksCompletedUnderPressure / LinebreaksAttemptedCompleted` | Proportion of Line Breaks Under Pressure % |
| `fifa_wc_linebreaks_completion_pct` | `LinebreaksAttemptedCompleted / LinebreaksAttempted` | Line Breaks Completion % |
| `fifa_wc_pct_distance_walking` | `DistanceWalking / TotalDistance` | %distance walking |
| `fifa_wc_pct_distance_high_speed_sprinting` | `DistanceHighSpeedSprinting / TotalDistance` | %distance high speed sprinting |
| `fifa_wc_TotalCards` | `YellowCards + RedCards` | combined discipline counter |

### `wc26_stg_player_powerrank` (~rows grow with matches played × 8)

One row per `(fifa_player_id, fifa_team_id)`. Pure groupby aggregation of `wc26_player_match_powerrank` — no joins. FDH power-ranking is a per-match score (higher = better), so we mean across the player's matches and carry a context counter:

| Column | Definition |
|---|---|
| `fifa_player_id` | FIFA player ID — key part 1 |
| `fifa_team_id` | FIFA team ID — key part 2 (a player on different teams would split into multiple rows; in WC26 typically one row per player) |
| `avg_attacking_score` | Mean of `attacking_score` across the player's ranked matches |
| `avg_defensive_score` | Mean of `defensive_score` |
| `avg_creativity_score` | Mean of `creativity_score` |
| `avg_defending_the_goal_score` | Mean of `defending_the_goal_score` (GK only — NaN for outfielders) |
| `n_matches_ranked` | Count of matches the player was ranked in — context for how trustworthy the means are |
| `player_kind` | `outfieldPlayer` or `goalkeeper` (copied from the first row in the group) |

### `wc26_stg_fantasy_player_totals` (~rows grow with rounds played × 14)

**Different key from the other player table.** This one is keyed by `fantasy_player_id` (FIFA Fantasy's own player ID, used by `play.fifa.com`), NOT `fifa_player_id`. Join to `wc26_stg_players` via `fantasy_players.fifa_player_id` (in the source `fantasy_players` parquet) if you need both fantasy-points and WC-stats on one row.

Pure groupby aggregation of `fantasy_player_round_stats` — tournament-to-date counters with no joins. Columns:

| Column | Definition |
|---|---|
| `fantasy_player_id` | FIFA Fantasy player ID — primary key |
| `appearances` | Count of round entries (i.e. distinct rounds this player has played) |
| `minutes_played` | Sum of `minutes_played` across all rounds |
| `starting_xi` | Sum of `starting_xi` flag (count of rounds the player started) |
| `total_points` | Sum of fantasy `points` across all rounds |
| `total_goals_scored` | Sum of `goals_scored` |
| `total_assists` | Sum of `assists` |
| `clean_sheets` | Sum of `clean_sheet` flag |
| `saves` | Sum of `saves` (GK only — 0 for outfield) |
| `tackles` | Sum of `tackles` |
| `chances_created` | Sum of `chances_created` |
| `shots_on_target` | Sum of `shots_on_target` |
| `scouting_bonus` | Sum of `scouting_bonus` (FIFA Fantasy's `<5% ownership × >4 pts` editorial bonus) |
| `yellow_cards` | Sum of `yellow_cards` |

---

## 5. The `ref_id_bridge` parquet

`wc26_matches.fifa_referee_id` is FIFA's numeric OfficialId (e.g. `"361561"`); `referee_master.referee_id` is a slug (e.g. `"anthony-taylor-gb"`). No direct join exists — the bridge resolves it.

| Parquet | Rows | Cols |
|---|---|---|
| `data/processed/ref_id_bridge.parquet` | ~40 | `fifa_referee_id`, `referee_id`, `match_method` |

`match_method ∈ {surname+iso3, surname_only, override, null}`. Roughly 90% of FIFA OfficialIds resolve to a panel ref; the remainder are 4th officials or VARs not in `referee_master`.

If a new FIFA OfficialId starts appearing in matches and isn't mapping, add a row to `data/overrides/ref_id_overrides.csv` (`fifa_referee_id,referee_id,note`) and re-run `04_referees.ipynb`. The bridge re-resolves on every daily refresh.

For the PWA you can ignore the bridge entirely — referee fields are already joined into `wc26_stg_matches` under the `referee_*` prefix.

---

## 6. Watch points

1. **Tournament window only.** Outside `2026-06-11 → 2026-07-20`, the hourly cron exits early. If you need pre-tournament/post-tournament refresh, call `python refresh.py --bucket hourly --force-refresh`.
2. **Knockout TBDs.** Until the group stage ends (~2026-06-28), knockout rows in `wc26_stg_matches` will have `home_nation_id`/`away_nation_id` NaN (and downstream prefixed nation cols too). They backfill as ESPN + FIFA resolve the bracket.
3. **Live mid-match.** `wc26_stg_matches` reflects the most-recent 3 h tick. For mid-match precision you'd need a faster cron (FDH publishes per-match stats minutes after final whistle); the current 3 h cadence is sufficient for the EV-scorer downstream.
4. **List-typed columns** (`stages_played`, `opponents`, `match_ids`, `all_names`): parquet stores these as lists. Most JS parquet readers materialize them as arrays; if yours doesn't, request the column in CSV form.
5. **Lossy joins.** When `home_nation_id` is NaN (TBD knockout slot), `home_fifa_rank` and friends are also NaN. Don't surface NaN as 0 in the PWA — fall back to "TBD" copy.

---

## 7. Companion docs in this repo

- [METRICS_MAP.md](METRICS_MAP.md) — every metric in the warehouse mapped to source endpoint + landing table. Includes the 5 new staging-table rows.
- [REFRESH.md](REFRESH.md) — refresh cadence, the auto-force model, the nb10 per-player heuristic.
- [RECOMMENDER.md](RECOMMENDER.md) — the 4-model fantasy recommender layer that sits on top of these staging tables. Notebooks 17 / 17a / 18, the `lib/recommender.py` bracket scorer, and the 6 PWA-bound JSONs that drive K's 2 cents.
- `WC26_DATA_DICTIONARY.xlsx` — sheet-per-table column dictionary; 35 sheets (Overview + 34 parquets including the 5 new staging tables + `ref_id_bridge`). Auto-regenerated by `_build_dictionary.py`.
- `K_2_Cents_Model_Spec.xlsx` — model spec workbook for the recommender layer (8 sheets: Overview / Factor catalog / Data lineage / Scoring schema / Models / Chip strategy / Anomalies / Verification). Regen with `build_model_spec_xlsx.py`.

---

## 8. Contract stability promise

The 5 staging tables and their column prefixes (`club_*`, `national_*`, `value_*`, `fotmob_wc_*`, `fifa_wc_*`, `recent{N}_*`, `home_*`, `away_*`, `referee_*`) are the stable surface. Breaking changes will land here with a version bump in this document. Additions (new columns) won't break the PWA; removals or renames will be announced.

The 28 source parquets are NOT under this contract — they can churn on upstream notebook edits.
