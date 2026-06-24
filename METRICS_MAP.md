# Metrics map — what's available where

Audit of every player / match / referee / fantasy metric we can pull, mapped to the source endpoint and the table that lands it. Read this when picking inputs for the scoring engine in Phase 3 (`Player EV = Base × Fixture Weight + Volume Bonus + Scouting Premium`).

Companion: **[REFRESH.md](REFRESH.md)** — refresh cadence and triggers per table.

Legend
- ✅ landed in a table today
- ⚠ available from source, not yet ingested
- 🟡 partial / approximate
- ❌ source does not publish it

---

## Table inventory + refresh cadence

> **Deprecated outputs (2026-06-20):** `wc26_players`, `wc26_player_season_stats`, and `wc26_player_season_stats_wide` are no longer contracted warehouse outputs. They'll be replaced by a forthcoming player-aggregate table. `wc26_players` continues to regenerate as an internal join target for notebooks 07/08/10 in the meantime; both season-stats tables are no longer written. Doc references below to `wc26_players.{field}` describe data lineage from FIFA — the new aggregate will land the same fields.

| Table | Rows | Notebook | Source | Refresh | Trigger |
|---|---:|---|---|---|---|
| `wc26_nations` | 48 | 01 | TM bundle + ESPN + FotMob seeds + hand | 🟦 frozen | squad/draw change |
| `wc26_stadiums` | 16 | 02 | hand-curated + Open-Meteo elevation | 🟦 frozen | venue swap |
| `wc26_matches` | 104 | 03 | ESPN-cal + FIFA-cal | 🟥 3 h | live; bracket fill |
| `wc26_player_match_stats` | 86,202 | 07 | FDH-stats (53-key allowlist) | 🟥 3 h + event | match → `finished` |
| `wc26_player_match_stats_wide` | 1,644 | 07 | FDH-stats wide pivot (14 ids + 53 stats) | 🟥 3 h + event | match → `finished` |
| `wc26_player_match_powerrank` | 546 | 07 | FDH-power | 🟥 3 h + event | match → `finished` |
| `wc26_player_enrichment` | 1,248 | 08 | FotMob-team + FotMob-player + TM-team | 🟥 3 h + event | event-A (player in newly-finished match) — promoted from daily 2026-06-24 |
| `wc26_player_market_value_history` | 64,180 | 08 | FotMob-player `marketValues.values[]` + TM `marketValueDevelopment` | 🟥 3 h | rebuilds whenever 08 ticks |
| `wc26_player_career_senior` | 21,280 | 08 | FotMob-player `careerHistory.senior` | 🟥 3 h + event | caps/goals/assists tick on every finished match |
| `wc26_player_career_national` | 11,569 | 08 | FotMob-player `careerHistory['national team']` | 🟥 3 h + event | caps/goals/assists tick on every finished match |
| `wc26_player_career_national_summary` | 1,224 | 08 | derived from `wc26_player_career_national` (youth/senior split per player) | 🟥 3 h | rebuilds with parent |
| `wc26_player_career_club_summary` | 1,224 | 08 | derived from `wc26_player_career_senior` (youth/senior split + all_clubs + current) | 🟥 3 h | rebuilds with parent |
| `wc26_player_market_value_summary` | 1,200 | 08 | derived from `wc26_player_market_value_history` (per-source latest+peak + consolidated) | 🟥 3 h | rebuilds with parent |
| `wc26_player_fotmob_wc` | 682 | 11 | parsed from `08` raw cache + per-match aggregation from FotMob `/matchDetails`. Strictly WC26 (seasonName=2026, tournamentId=24254). Carries 12 deep WC stats summed across each player's WC26 matches. | 🟥 3 h | re-parse after `08` (same tick) |
| `wc26_player_recent_matches_fotmob` | 24,193 | 11 | parsed from `08` raw cache, capped at 20 newest matches per player | 🟥 3 h | match → `finished` (lag ~15 min — bounded by the same-tick `08` playerData refetch) |
| `referee_master` | 50 | 04 | FootyMetrics WC26 panel | 🟨 daily | panel change (suspension) |
| `referee_profile` | 150 | 04 | FootyMetrics career + `recentMatches` | 🟨 daily | re-parse after refresh |
| `referee_assignments` | 58 | 05 | FIFA-cal `Officials` + FootyMetrics fixtures | 🟥 3 h + event | new appointment publishes |
| `fantasy_rounds` | 8 | 10 | Fantasy `/rounds.json` | 🟥 3 h | round status flip |
| `fantasy_round_matches` | 72 | 10 | Fantasy `/rounds.json` `tournaments[]` | 🟥 3 h | live; grows as bracket fills (104 final) |
| `fantasy_squads` | 48 | 10 | Fantasy `/squads.json` | 🟥 3 h | `isEliminated` flip post-KO |
| `fantasy_players` | 1,488 | 10 | Fantasy `/players.json` | 🟥 3 h | **`percentSelected`** moves continuously — feeds Scouting Premium |
| `fantasy_player_round_stats` | 649 | 10 | Fantasy `/player_stats/{id}.json` | 🟥 3 h + event | round → `complete`; grows ~600/round |
| `wc26_match_weather` | 92 | 12 | Open-Meteo `/v1/forecast` + `/v1/archive` | 🟥 3 h | hourly grid refresh; archive supersedes forecast post-match; coverage grows as knockout kickoffs roll inside Open-Meteo's 16-day forecast horizon |
| `wc26_match_polymarket_markets` | 3,087 | 13 | `gamma-api.polymarket.com/events?tag_slug=fifa-world-cup` markets[] | 🟥 3 h | volume + lastTradePrice (implied prob) per market |
| `wc26_polymarket_match_volume` | 72 | 13 | derived from `wc26_match_polymarket_markets` (per-match volume split: moneyline+draw vs other) | 🟥 3 h | rebuilds with parent table |
| `wc26_polymarket_winner_history` | 2,448 | 13 | Polymarket CLOB `/prices-history` (`world-cup-winner` event, 48 markets) | 🟨 daily | one daily snapshot per nation from 2026-05-01 onward |
| `ref_id_bridge` | ~50 | 04 | derived: surname+iso3 match between `wc26_matches.fifa_referee_id` (numeric) and `referee_master.referee_id` (slug); manual overrides at `data/overrides/ref_id_overrides.csv` | 🟨 daily | rebuilds with `04` |
| `wc26_stg_nations` | 48 | 14 | pass-through of `wc26_nations` | 🟦 frozen | parent edit |
| `wc26_stg_stadiums` | 16 | 14 | `wc26_stadiums` + per-stadium agg of `wc26_matches` and `wc26_match_weather` | 🟥 3 h | rebuilds with inputs |
| `wc26_stg_referee_profile` | ~250 | 14 | `referee_profile` + join `referee_master` | 🟨 daily | rebuilds with inputs |
| `wc26_stg_matches` | 104 | 15 | `wc26_matches` + joins of stadiums / weather / nations(×2) / `ref_id_bridge` → `referee_master` / `wc26_polymarket_match_volume` | 🟥 3 h | any input ticks |
| `wc26_stg_players` | ~1,248 | 16 | `wc26_player_enrichment` + 4 summary parquets + per-player agg of `wc26_player_match_stats_wide` + 4 form windows from `wc26_player_recent_matches_fotmob` | 🟥 3 h | any input ticks |
| `wc26_stg_fantasy_player_totals` | ~(growing) | 14 | groupby agg of `fantasy_player_round_stats` by `fantasy_player_id` | 🟥 3 h | rebuilds with parent |
| `wc26_stg_players_view` | 1,248 | 16 | curated subset of `wc26_stg_players` (~115 cols) + 13 derived ratios + `fifa_wc_TotalCards` | 🟥 3 h | rebuilds with parent |
| `wc26_stg_player_powerrank` | ~(grows with matches played) | 14 | mean of FDH power-ranking scores per `(fifa_player_id, fifa_team_id)` over `wc26_player_match_powerrank` | 🟥 3 h | rebuilds with parent |

🟦 frozen · 🟨 daily · 🟥 every 3 h. Full triggers and the cron sketch live in [REFRESH.md](REFRESH.md). The five `wc26_stg_*` rows are the **downstream contract**: the Phase-3 EV scorer and the audit-app PWA both read these instead of joining the raw source-sliced parquets themselves.

---

## Sources

| Tag | Endpoint | Auth | Granularity |
|-----|----------|------|-------------|
| `FIFA-cal` | `api.fifa.com/api/v3/calendar/matches?idCompetition=17&idSeason=285023&count=110` | none + `Referer:fifa.com` | one row per match (104) |
| `FIFA-squad` | `api.fifa.com/api/v3/teams/{IdTeam}/squad?idCompetition=17&idSeason=285023&language=en` | none + `Referer` | one per team (48) |
| `FDH-stats` | `fdh-api.fifa.com/v1/stats/match/{IdIFES}/players.json` | none + `Referer` | ~52 players × match |
| `FDH-power` | `fdh-api.fifa.com/v1/powerranking/match/{IdIFES}.json` | none + `Referer` | ~25 players × match |
| `FDH-season` | `fdh-api.fifa.com/v1/stats/season/285023/players.json` | none + `Referer` | one per player (1,022) — 119 stat keys, tournament-wide rollup |
| `Fantasy-rounds` | `play.fifa.com/json/fantasy/rounds.json` | none + `Referer:play.fifa.com` | 8 rounds + nested fixtures |
| `Fantasy-squads` | `play.fifa.com/json/fantasy/squads.json` | none + `Referer` | 48 nations w/ fantasy_squad_id ↔ abbr |
| `Fantasy-players` | `play.fifa.com/json/fantasy/players.json` | none + `Referer` | 1,488 players w/ **`fifaId`** join + `percentSelected` + price + form |
| `Fantasy-player-stats` | `play.fifa.com/json/fantasy/player_stats/{fantasy_id}.json` | none + `Referer` | per round (~1–8 per player) |
| `Fantasy-team-history` | `play.fifa.com/api/en/fantasy/team/history/{team_id}` | **auth required** (403) | per user — skipped |
| `FotMob-team` | `www.fotmob.com/api/data/teams?id={team_id}` | none + `Referer` | one per team |
| `FotMob-player` | `www.fotmob.com/api/data/playerData?id={player_id}` | none + `Referer` | one per player |
| `FotMob-LB` | `data.fotmob.com/stats/77/season/24254/{stat}.json` | none + `Referer` | one per stat leaderboard |
| `ESPN-cal` | `site.api.espn.com/.../fifa.world/scoreboard?dates=…` | none, CORS-friendly | one row per event |
| `TM-bundle` | `pub-…r2.dev/data/transfermarkt-datasets.zip` (`national_teams.csv.gz` only via `remotezip`) | none | one row per national team (118) |
| `TM-team` | `transfermarkt.com/{slug}/startseite/verein/{team_id}` | none, scraped HTML | one per team |
| `Polymarket` | `gamma-api.polymarket.com/events?...` | none | one per event |
| `Open-Meteo` | `api.open-meteo.com/v1/forecast` and `/v1/archive` | none | per (lat,lng,date) |
| `FootyMetrics` | `footymetrics.com/referees/{id}-{slug}` | scraped HTML | one per ref |
| `WorldReferee-upcoming` | `worldreferee.com/upcoming` | scraped HTML | site-wide |
| `Soccerway` | `int.soccerway.com/referees/{slug}/` | scraped HTML | one per ref |

---

## Player metrics

### Identity / bio

| Metric | FIFA-squad | FotMob-player | TM-team | ESPN | Landed in |
|---|:-:|:-:|:-:|:-:|---|
| Full name + locales (12) | ✅ | ✅ | ✅ | ✅ | `wc26_players.name` (FIFA) + `name` (FotMob, TM) |
| Short name | ✅ | ⚠ | ❌ | ✅ | `wc26_players.short_name` |
| Date of birth | ✅ | ✅ | ✅ | ✅ | `wc26_players.birth_date` |
| Height (cm) | ✅ | ✅ | ⚠ | ✅ | `wc26_players.height_cm` |
| Weight (kg) | ✅ | ❌ | ❌ | ⚠ | `wc26_players.weight_kg` |
| Jersey number | ✅ | ✅ | ✅ | ✅ | `wc26_players.jersey_num` |
| Preferred foot | ❌ (always null in WC squad) | ✅ | ⚠ | ⚠ | `wc26_player_enrichment.preferred_foot` |
| Position primary | ✅ | ✅ | ✅ | ✅ | `wc26_players.position`, `real_position` |
| Position side (L/R/C) | ✅ | ⚠ | ❌ | ❌ | `wc26_players.real_position_side` |
| Other positions | ❌ | ✅ (`positionIdsDesc`) | ⚠ | ❌ | `wc26_player_enrichment.position_ids_desc` |
| Photo URL | ✅ (high-res) | ✅ | ✅ | ✅ | `wc26_players.picture_url` |
| Captain flag | ⚠ | ✅ | ⚠ (icon) | ❌ | `wc26_player_enrichment` (FotMob) |
| Active status / injury | ✅ (ActiveStatus) | ✅ (injury) | ❌ | ❌ | `wc26_players.active_status`, `wc26_player_enrichment.injury` |
| Contract end date | ❌ | ✅ | ✅ | ❌ | `wc26_player_enrichment.contract_end` |

### Cross-source player IDs

| Source | Field | Coverage gate |
|---|---|---|
| FIFA | `fifa_player_id` | 48 squads × 26 = 1248 (100%) |
| FotMob | `fotmob_player_id` | name+DOB match against FotMob team page |
| Transfermarkt | `tm_player_id` | name+DOB match against TM team page |
| ESPN | `espn_player_id` | not currently fetched — would need /teams/{id}/roster scrape |

All three land in `wc26_player_enrichment`.

### Market value

| Metric | FIFA | FotMob-team | FotMob-player | TM-team | ESPN |
|---|:-:|:-:|:-:|:-:|:-:|
| Current value (EUR) | ❌ | ✅ (`transferValue`, scisports) | ✅ (`marketValues.values[-1]`) | ✅ (`€XX.XXm`) | ❌ |
| Lower / upper bound | ❌ | ❌ | ✅ | ❌ | ❌ |
| Source label | ❌ | implicit scisports | ✅ (`scisports`) | implicit TM | ❌ |
| Historical series | ❌ | ❌ | ✅ (full history per team) | ⚠ (`marktwertverlauf` page) | ❌ |

Tables: `wc26_player_enrichment.market_value_eur_tm`, `transfer_value_eur_fotmob`, `market_value_latest_eur_fotmob` + `wc26_player_market_value_history` (long format, FotMob).

ESPN explicitly does not publish transfer values — kept out per the audit so we don't waste calls.

### WC26 tournament line (this competition only)

| Metric | FIFA-squad | FotMob-team | FDH-stats | Landed |
|---|:-:|:-:|:-:|---|
| Matches played | ✅ (`MatchesPlayed`, populated post-match) | ⚠ | ✅ (count from rows) | `wc26_players.fifa_match_appearances` |
| Goals | ✅ | ✅ | ✅ | `wc26_players.fifa_goals` + per-match rows |
| Yellow / red cards | ✅ | ✅ | ✅ | `wc26_players.fifa_yellow_cards`, `_red_cards` |
| Per-match rating | ❌ | ✅ | ❌ (raw stats only) | `wc26_player_enrichment.wc_rating` |
| Assists | ❌ (FIFA squad doesn't surface) | ✅ | ✅ | `wc26_player_enrichment.wc_assists` + per-match |

### Per-match per-player stats (the goldmine — FDH only)

`wc26_player_match_stats` carries 116 distinct stat keys per (player, match), long format. The full list lives in `data/processed/wc26_player_match_stats.csv` under `stat_name`. Groups:

**Shooting** (24 keys)
AttemptAtGoal, AttemptAtGoalOnTarget, AttemptAtGoalOffTarget, AttemptAtGoalBlocked,
AttemptAtGoalInsideThePenaltyArea, AttemptAtGoalInsideThePenaltyAreaOnTarget,
AttemptAtGoalOutsideThePenaltyArea, AttemptAtGoalOutsideThePenaltyAreaOnTarget,
AttemptAtGoalFromBallProgression, AttemptAtGoalFromCorner, AttemptAtGoalFromCross,
AttemptAtGoalFromFreeKicks, AttemptAtGoalFromOther, AttemptAtGoalFromPass,
AttemptAtGoalFromPenalty, AttemptAtGoalFromRebound, HeadedAttemptAtGoal,
Goals, GoalsInsideThePenaltyArea, GoalsOutsideThePenaltyArea, GoalsFromDirectFreeKicks,
Assists.

**Passing / build-up** (~25 keys)
BallProgressions (attempted/completed), SwitchesOfPlay (attempted/completed),
LinebreaksAttempted (AllLines / AttackingAndMidfieldLine / AttackingLine / DefensiveLine),
LinebreaksCompleted (same splits),
DistributionsUnderPressure, DistributionsCompletedUnderPressure,
DirectFreeKicks, IndirectFreeKicks, Crosses, CrossesCompleted, Corners, GoalKicks.

**Defence** (~15)
DefensivePressuresApplied, DirectDefensivePressuresApplied,
ForcedTurnovers, FoulsFor, FoulsAgainst, DirectRedCards, IndirectRedCards,
GoalkeeperDefensiveActionsInsidePenaltyArea, GoalkeeperDefensiveActionsOutsidePenaltyArea,
GoalkeeperSavePercentage, GoalkeeperSaves, GoalkeeperSavesOnTarget,
CleanSheets, GoalsConceded, GoalsConcededFromAttemptAtGoalAgainst.

**Physical / load** (~10)
DistanceWalking, DistanceJogging, DistanceLowSpeedSprinting,
DistanceHighSpeedRunning, DistanceHighSpeedSprinting, TotalDistance,
AvgSpeed, TopSpeed, TimePlayed.

**Goal-against context** (defender attribution)
AttemptAtGoalAgainst, AttemptAtGoalAgainstOnTarget.

#### Comparison vs FotMob audit-doc

| Group | FotMob audit-doc count | FDH stat key count | Notes |
|---|---|---|---|
| Attacking | 8 surfaced | ~28 raw | FDH splits by zone, body part, situation |
| Defending | 4 surfaced | ~15 raw | FDH has pressure-applied breakdown FotMob lacks |
| Possession | 1 (pass acc) | ~25
 raw | Linebreaks + ball-progression + switches are FDH-only |
| Discipline | 2 | 2 | parity |
| Goalkeeping | 3 | ~6 raw | FDH gives in/out PA split |
| Physical | 0 | 9 | FDH-only — speed, distance, GPS |

**Conclusion**: every audit-doc per-match metric is in `wc26_player_match_stats`; FDH adds ~5x more granularity (especially physical + linebreaks). FotMob still wins on **season-over-season** and **multi-year career** views.

### Power ranking (FDH-power)

| Metric | Source | Landed |
|---|---|---|
| Attacking rank (tournament-wide) | FDH-power | `wc26_player_match_powerrank.attacking_rank` |
| Defensive rank | FDH-power | `defensive_rank` |
| Creativity rank | FDH-power | `creativity_rank` |
| Within-team rank for each | FDH-power | `*_rank_within_team` |
| Score (float) for each | FDH-power | `*_score` |
| GK "defending the goal" rank/score | FDH-power | `defending_the_goal_rank/score` |
| GK "in possession" rank/score | FDH-power | `in_possession_rank/score` |
| Rank change vs prior match | FDH-power | available, currently null (single-match snapshot) |

### Career / multi-season (FotMob-player only)

| Metric | FotMob field | Landed |
|---|---|---|
| Senior club seasons | `careerHistory.careerItems.senior` | `wc26_player_career_senior` (long) |
| National team seasons | `careerHistory.careerItems['national team']` | `wc26_player_career_national` |
| Senior totals (caps, goals, ratings, etc.) | derived from above | derivable per season |
| Stat seasons (per-comp per-season totals) | `statSeasons` | ⚠ currently captured into enrichment but not exploded |
| Trophies | `trophies` | ⚠ available, kept in enrichment row as JSON |
| Recent matches (last 10) | `recentMatches` | ⚠ available — for non-WC matches FotMob is the only source |

ESPN can also produce per-season totals via `/teams/{id}/seasons/{year}/statistics` if a Phase 3 model wants a redundancy check; currently not fetched.

---

## Team metrics

### Tournament context (per team across WC)

| Metric | Source | Landed |
|---|---|---|
| Tactical formation per match | FIFA-cal (`Home.Tactics`, `Away.Tactics`) | `wc26_matches.fifa_home_tactics`, `_away_tactics` |
| WC stat leaderboards (rank views) | FotMob-LB | not landed; available on demand |
| Recent form (W/D/L) | ESPN `/all/teams/{id}/schedule` | not landed — Phase 2 form table |
| Polymarket implied wins | Polymarket | Phase 4 |

### Static team profile (in `wc26_nations`)

`espn_team_id`, `fotmob_team_id`, `tm_team_id`, `iso_alpha2`, confederation, FIFA rank, squad valuation, group, pot, host flag, alias union for name resolution.

### Coaches + officials

`wc26_team_officials` (FIFA-squad): `fifa_coach_id`, name, role, DOB, country, per team.

---

## Match metrics

### Identity / scheduling

| Metric | ESPN | FIFA-cal | FDH | Landed |
|---|:-:|:-:|:-:|---|
| Kickoff UTC | ✅ | ✅ | — | `wc26_matches.kickoff_utc` |
| Kickoff local | derived | derived | — | `wc26_matches.kickoff_local` |
| Stage (group / R32 / R16 / QF / SF / 3rd / final) | ✅ | ✅ (StageName) | — | `wc26_matches.stage` |
| Status (scheduled / live / finished) | ✅ | ✅ | — | `wc26_matches.status` |
| Attendance | ⚠ | ✅ | — | `wc26_matches.fifa_attendance` |
| Final score | ✅ | ✅ | — | `wc26_matches.home_score`, `away_score` |
| Penalty score | ⚠ | ✅ (`HomeTeamPenaltyScore`) | — | not yet landed (knockouts only) |
| Aggregate (two-leg) | ❌ | ✅ | — | not applicable in WC group/knockout |

### Cross-source match IDs

`wc26_matches` carries: `espn_match_id`, `fifa_match_id`, `fifa_id_ifes`, `seed_match_id`.

### Live in-match (FDH or FotMob)

Possession %, live xG, formation changes — FDH-stats includes per-player team_id + side; FotMob-LB pre-aggregates. Not currently sampled mid-match; Phase 4 if we run a live optimizer.

---

## Stadium / weather

| Metric | Source | Landed |
|---|---|---|
| Capacity / roof / surface / altitude | hand-curated from FIFA spec | `wc26_stadiums` |
| Lat / lng / IANA tz / weather grid key | `venues.ts` seed + Open-Meteo | `wc26_stadiums` |
| Per-match weather (temp, humidity, wind, code) | Open-Meteo `/v1/forecast` and `/v1/archive` | Phase 2 — `stadium_weather_daily` |
| FIFA-published weather snapshot | FIFA-cal (`Weather.{Humidity, Temperature, WindSpeed, Type}`) | available but currently null per-match (FIFA fills post-game) — captured in raw cache |

---

## Referee metrics

### `referee_master` (50 rows, WC26 panel)

| Metric | FootyMetrics | FIFA-squad (Officials[]) | Landed |
|---|:-:|:-:|---|
| Name + country + confederation | ✅ | ⚠ (in match payload, not list) | `referee_master` |
| FIFA OfficialId | ❌ | ✅ | available via `wc26_matches.fifa_referee_id` — not yet backported to `referee_master` |
| Profile URL | ✅ | ❌ | `referee_master.fm_url` |

### `referee_profile` (50 career rows, FM source)

Current windows: `career` from FootyMetrics only. Fields:
`yellow_pg, red_pg, penalty_pg, fouls_pg, booking_points_pg, added_time_fh_avg, added_time_sh_avg, total_yellows, total_reds, total_penalties, total_fouls, fixtures_with_red, fixtures_with_penalty, fixtures_no_cards`.

#### `last_10` and `last_25` windows — secondary sources

| Source | Per-match data? | Reachable? | Notes |
|---|:-:|:-:|---|
| FootyMetrics `recentMatches` | ⚠ embedded in RSC | ✅ | Same scraper as career; parseable from cached profile pages with extra regex. **Best near-term path.** |
| Soccerway `/referees/{slug}` | ✅ table per ref | ✅ (200 OK) | Has per-match table — needs HTML parser + per-ref URL discovery |
| Transfermarkt `/profil/schiedsrichter/{id}` | per-competition totals only | ✅ | Useful for season aggregates, not single-match list |
| WorldReferee per-ref `/career` | ✅ table per ref | ✅ | URL discovery is the bottleneck (no list page links profiles) |
| FBref refs index | ❌ | 403 (Cloudflare) | not viable without cloudscraper |
| WhoScored | per-match aggregates | 403 | not viable |

**Plan to land**: extend `lib/refs.py` with `fm_recent_fixtures(fm_id, slug)` parsing the already-cached profile page for the recent-matches block, plus an optional Soccerway parser as a second source. Both write into `referee_profile` with `window IN ('last_25', 'last_10')`.

### `referee_assignments` (per match)

Sources combined: FIFA-cal Officials (primary), FootyMetrics upcoming fixtures (verification). Schema covers `role`, `source`, `announced_at`, `fifa_official_id`, `fifa_official_name`.

---

## FIFA Fantasy metrics (✅ landed via Notebook 10)

This was the missing piece flagged in `D-1-Doc.txt §2` (the Scouting Premium formula needs `selected_by_pct`). `play.fifa.com/json/fantasy/players.json` ships it as `percentSelected`, and every row carries `fifaId` for a clean join into `wc26_players`.

### Round / fixture context

| Metric | Source | Field | Landed |
|---|---|---|---|
| Round id + status | `Fantasy-rounds` | `id`, `status` (`playing`/`upcoming`/`complete`) | `fantasy_rounds` |
| Round start / end UTC | `Fantasy-rounds` | `startDate`, `endDate` | `fantasy_rounds` |
| Fixture per round (104 total) | `Fantasy-rounds` | `tournaments[]` | `fantasy_round_matches` |
| Fixture live period | `Fantasy-rounds` | `period`, `minutes`, `extraMinutes` | `fantasy_round_matches` |
| Fixture venue + city | `Fantasy-rounds` | `venueId`, `venueName`, `venueCity` | `fantasy_round_matches` |
| Fixture suspension flag | `Fantasy-rounds` | `isSuspended` | `fantasy_round_matches` |
| Fixture scores | `Fantasy-rounds` | `homeScore`, `awayScore` | `fantasy_round_matches` |

### Fantasy player universe

| Metric | Source | Field | Notes |
|---|---|---|---|
| `fifa_player_id` link | `Fantasy-players` | `fifaId` | direct integer join — no fuzzy match |
| `fantasy_player_id` | `Fantasy-players` | `id` | key for the per-round stats endpoint |
| Fantasy squad (= nation) | `Fantasy-players` | `squadId` | joins `fantasy_squads.fantasy_squad_id` ↔ `wc26_nations.nation_id` (via abbr) |
| Position (GK/DEF/MID/FWD) | `Fantasy-players` | `position` | Fantasy's own categorisation |
| Price | `Fantasy-players` | `price` | floats around the round, used for daily budget calcs |
| **`percentSelected`** | `Fantasy-players` | `percentSelected` | **the Scouting Premium input** — `< 5%` triggers the log boost |
| Selection % history | `Fantasy-players` | `roundsSelected` (dict, json'd) | per-round ownership series |
| Total / avg points | `Fantasy-players` | `stats.totalPoints`, `stats.avgPoints` | season-to-date |
| Form | `Fantasy-players` | `stats.form` | FIFA Fantasy's own form score |
| Last-round points | `Fantasy-players` | `stats.lastRoundPoints` | |
| Round-by-round points | `Fantasy-players` | `stats.roundPoints` (dict, json'd) | parallel to `roundsSelected` |
| Next fixture | `Fantasy-players` | `stats.nextFixtureFromActiveRound`, `nextFixtureFromScheduledRound` | joins `fantasy_round_matches.fantasy_match_id` |
| One-to-watch flag + text | `Fantasy-players` | `oneToWatch`, `oneToWatchText` | FIFA's editorial pick |
| Status / match-status | `Fantasy-players` | `status` (`playing`/`out`/etc), `matchStatus` (`start`/`subbed`) | |

### Per-round raw fantasy stats (per player)

`Fantasy-player-stats` exposes one entry per round-played. Stat-key vocabulary matches FIFA Fantasy's scoring system (decoded in our earlier screenshot review):

| Key | Meaning | Phase 3 use |
|---|---|---|
| `SXI` | Starting-XI flag (0/1) | starter bonus |
| `MP` | Minutes played | 60-min and 90-min tiers |
| `GS` | Goals scored | core attacking points |
| `AS` | Assists | core attacking points |
| `CS` | Clean sheets | DEF/GK floor |
| `GC` | Goals conceded | DEF/GK negative |
| `YC` | Yellow cards | discipline penalty |
| `RC` | Red cards | discipline penalty |
| `OG` | Own goals | rare negative |
| `PW` | Penalty won | bonus point |
| `PC` | Penalty conceded | negative |
| `PS` | Penalty saved | GK bonus |
| `S` | Saves (GK) | per-3-save bonus |
| `T` | Tackles | midfielder Volume Bonus trigger (`≥4.5/90` per `D-1-Doc §2`) |
| `CC` | Chances created | midfielder Volume Bonus trigger (`≥1.5/90`) |
| `ST` | Shots on target | forward attacking volume |
| `FK` | Free kicks | set-piece signal |
| `SB` | Scouting Bonus eligible flag | the `<5% ownership × >4 pts` reward described in `D-1-Doc §2` — **directly published per round** |

All 18 keys land in `fantasy_player_round_stats` as columns. `points` (total fantasy points for that round) lands alongside.

### What we *cannot* get from Fantasy

| Want | Why blocked |
|---|---|
| Other users' team picks / captain picks | `Fantasy-team-history` is auth-gated (403) — single-user only, no aggregate exposed |
| Captain % / vice-captain % | not in the public `/players.json`; would need community-aggregated data |
| Transfer trends (in/out per round) | not exposed; `roundsSelected` deltas approximate net moves |

---

## Polymarket / betting (Phase 4 — not yet ingested)

| Metric | Endpoint | Planned table |
|---|---|---|
| Match outright win prob | `gamma-api.polymarket.com/events?slug=` | `match_market_outright` |
| Clean sheet implied prob | nested in event markets | feeds GK / defender EV |
| Goal-scorer markets | event sub-markets | feeds forward EV |
| Liquidity (orderbook depth) | `last_price`, `orderbook_liquidity` | confidence weight on the implied prob |

---

## Open questions / gaps before Phase 3 scoring

1. **FotMob ↔ FIFA name+DOB match-rate** lands in `08`'s output — anything < 90% needs alias tweaking before model use.
2. **TM market value coverage** depends on TM listing the player in the national-team squad; 5 nations (HAI / CIV / CUW / CPV / COD) historically lacked TM national-team rosters in `national_teams.csv`. With the per-team manual ID patches in `01`, the TM team page itself usually still resolves them — verify in 08's output.
3. **Per-match weather** from FIFA is null today. Open-Meteo archive will backfill for finished matches; forecast covers up to 16 days ahead — handle the gap stadium-by-stadium in the Phase 2 weather notebook.
4. **Per-match Polymarket pull** is the major Phase 4 dependency for the EV scoring engine's `Fixture Weight` term.
5. ~~Per-player FIFA `selected_by_pct` for the Scouting Premium~~ — **landed in `fantasy_players.percent_selected` via notebook 10**.
6. **Tournament-stats FotMob deep fields** (xg, passAccuracy, etc.) — only exposed for players whose default tournament *is* the WC; otherwise FotMob serves their club competition's data. FotMob WC rating + apps/goals/assists lands in `wc26_player_fotmob_wc`. Per-match FDH stat granularity is in `wc26_player_match_stats` (long) / `_wide` (pivot); tournament rollups will come from the forthcoming player-aggregate table.
