# Player-summary inventory — 6 layers × sources

The base player view you sketched needs 6 layers. Every input is already in the warehouse — here's exactly where, so you can author the view's columns when you're ready. This is a reference for the *materialized view* you'll define later, not the view itself.

Canonical join key for everything below: `fifa_player_id`.

---

## Layer 1 — Basic info

| Field | Table.column | Notes |
|---|---|---|
| Full name (FIFA) | `wc26_players.name` | SURNAME-style; `wc26_player_enrichment.fotmob_name` for the natural-order version |
| Short name | `wc26_players.short_name` | |
| DOB | `wc26_players.birth_date` | |
| Age (derived) | `wc26_player_enrichment` (carries `birth_date`) → `(today - birth_date).years` | |
| Height (cm) | `wc26_players.height_cm` | |
| Weight (kg) | `wc26_players.weight_kg` | |
| Preferred foot | `wc26_player_enrichment.preferred_foot` | from FotMob (FIFA doesn't publish) |
| Jersey number | `wc26_players.jersey_num` | |
| Position (coarse) | `wc26_players.position` | GK / DEF / MID / FWD |
| Position (specific) | `wc26_players.real_position` + `real_position_side` | e.g. Defender · Right |
| Other positions | `wc26_player_enrichment.position_ids_desc` | FotMob (e.g. "ST,RW,CAM") |
| Photo URL | `wc26_players.picture_url` | |
| Nation | `wc26_players.nation_id` → `wc26_nations` for name/flag/iso/confederation |
| Current club | `wc26_player_enrichment.club_name` (FotMob) or `club_name_tm` (TM) |
| Captain flag | `wc26_player_enrichment.is_captain` | from FotMob playerData |
| Injury status | `wc26_player_enrichment.injury` | FotMob (null when fit) |
| Contract end | `wc26_player_enrichment.contract_end` | FotMob |

---

## Layer 2 — Transfer-market values

Three source vintages — keep all three in the view for confidence comparison.

| Field | Table.column | Source / vintage |
|---|---|---|
| TM current value (EUR) | `wc26_player_enrichment.market_value_eur_tm` | Transfermarkt, human-curated |
| FotMob current value | `wc26_player_enrichment.transfer_value_eur_fotmob` *or* `market_value_latest_eur_fotmob` | scisports algorithmic |
| FotMob lower / upper bound | `wc26_player_enrichment.market_value_lower_eur_fotmob` / `_upper_eur_fotmob` | confidence interval |
| Historical series | `wc26_player_market_value_history` | one row per (player, valuation date) — full career |
| ESPN value | — | ESPN does not publish transfer values (intentional skip) |

**Helpful derived metrics for the view**:
- `market_value_eur_consensus = mean(market_value_eur_tm, market_value_latest_eur_fotmob)`
- `market_value_eur_gap_pct = abs(TM − FotMob) / mean(TM, FotMob)` — large gap = one source is stale
- `market_value_trend_6m = current − value 6 months ago` (from history)
- `market_value_peak_eur = max(value_eur)` (career peak)
- `market_value_peak_date` (when)

---

## Layer 3 — Club performance

The base table has the per-spell totals + per-season rows. Most-recent spell is row 0 per player.

| Field | Table | Notes |
|---|---|---|
| Career spell rows | `wc26_player_career_senior` where `kind='team'` | one per club (start_date, end_date, apps, goals, assists) |
| Per-season rows | `wc26_player_career_senior` where `kind='season'` | one per season with appearances + rating |
| Current club spell | `wc26_player_career_senior` where `kind='team' AND active=True` | the row you'd put in the player view |
| Last-season totals (club) | `wc26_player_career_senior` where `kind='season'` order by `season_name desc` LIMIT 1 | |
| All-time senior apps | `SUM(appearances) FROM career_senior WHERE kind='team'` | |
| All-time senior goals | `SUM(goals) ... kind='team'` | |
| All-time assists | `SUM(assists) ... kind='team'` | |
| Number of clubs in career | `COUNT(DISTINCT fotmob_team_id) ... kind='team'` | |

**Derived for the view**:
- `current_club_spell_years = today - start_date` for the active row
- `apps_per_season_avg = total_apps / (years_since_debut)` (rough)
- `goals_per_app = total_goals / total_apps` (career strike rate)

---

## Layer 4 — National-team performance

Same structure as club career, but for the national team. No splits other than this — FotMob doesn't separate friendlies vs competitive in this table.

| Field | Table | Notes |
|---|---|---|
| National team spell | `wc26_player_career_national` where `kind='team' AND active=True` | usually 1 row |
| Per-season national rows | `wc26_player_career_national` where `kind='season'` | |
| Total caps | `SUM(appearances) ... kind='team'` | |
| Total international goals | `SUM(goals) ... kind='team'` | |
| Total assists | `SUM(assists) ... kind='team'` | |
| Most recent season caps / goals | `kind='season'` order by `season_name desc` LIMIT 1 | |

---

## Layer 5 — WC2026 performance

This is the layer with the most depth — three independent sources to triangulate.

| Field | Table | Notes |
|---|---|---|
| WC apps / goals / assists / rating | `wc26_player_fotmob_wc` | FotMob's WC line per player |
| Tournament-wide per-stat aggregates | `wc26_player_season_stats_wide` | 1 row per player × 119 cols (Goals, Assists, AttemptAtGoal, BallProgressions, DistanceWalking, etc.) |
| Per-match per-stat values | `wc26_player_match_stats` (long) | one row per (player, match, stat_name) |
| Per-match power-ranks | `wc26_player_match_powerrank` | attacking/defensive/creativity rank per match |
| Squad-side WC totals | `wc26_player_enrichment.wc_goals` / `wc_assists` / `wc_rating` / `wc_yellow_cards` / `wc_red_cards` | FotMob team-page line — quick lookup |
| Tournament goals (FIFA-counted) | `wc26_players.fifa_goals` | populated post-match by FIFA |
| Tournament yellow / red | `wc26_players.fifa_yellow_cards` / `fifa_red_cards` | FIFA-counted |
| Fantasy points scored | `fantasy_players.total_points` + `fantasy_player_round_stats.points` per round | the EV ground-truth |
| Fantasy ownership % | `fantasy_players.percent_selected` | Scouting Premium input |

**Derived for the view**:
- `wc_goals_per_90 = season_stats_wide.Goals / (season_stats_wide.TimePlayed / 5400)` (TimePlayed is in seconds)
- `wc_top_speed_kmh = season_stats_wide.TopSpeed` (max across matches; you may want per-match max)
- `wc_avg_distance_per_match = season_stats_wide.TotalDistance / (matches_appeared)`

---

## Layer 6 — Recent form

Two parallel sources at different granularities.

| Field | Table | Notes |
|---|---|---|
| Last-N matches (any competition) | `wc26_player_recent_matches_fotmob` | newest-first; ~47 per player on avg |
| Per-match FotMob rating | same | float 0–10 |
| POTM count last N | `COUNT(*) WHERE player_of_the_match=True` | filter to recent N rows |
| Form rolling avg rating | `AVG(fotmob_rating) over last 5 matches` | window function |
| Last 10 goals / assists / minutes | `SUM(goals)`, `SUM(assists)`, `SUM(minutes_played)` over recent slice | |
| Last 5 in WC only | filter `league_id=77` then take 5 newest | |
| Fantasy form score | `fantasy_players.form` | FotMob does this internally |

**Derived for the view**:
- `form_rating_5m = AVG(fotmob_rating) over last 5 matches`
- `form_goals_5m = SUM(goals) over last 5`
- `started_last_5_pct = COUNT(*) WHERE on_bench=False AND minutes_played >= 60 / 5`

---

## Coverage summary right now

| Layer | Coverage of 1,248 FIFA players | Notes |
|---|---|---|
| 1 — Basic | 1,248 (100%) | FIFA is the source-of-truth |
| 2 — Market value (TM) | 1,140 (91%) | resolving with name-search; targeting ≥98% |
| 2 — Market value (FotMob) | 1,160 (93%) | same |
| 3 — Club career | ~1,160 | matches FotMob coverage |
| 4 — National career | ~1,160 | same |
| 5 — WC perf (FotMob WC line) | 961 (FotMob-only; players who haven't appeared yet have no row) | |
| 5 — WC perf (fdh-api season) | 1,022 | players who've appeared this tournament |
| 5 — Fantasy ownership | 1,236 with `fifa_player_id` match | |
| 6 — Recent matches | ~1,160 | |

---

## Source-of-truth picks (suggested)

When two sources disagree, my default would be:

| Metric | Primary | Fallback | Why |
|---|---|---|---|
| Player identity / DOB / nation | FIFA | — | authoritative for WC squad |
| Position | FIFA primary, FotMob secondary | — | FIFA's coarse, FotMob's other-positions adds nuance |
| Preferred foot | FotMob | — | FIFA doesn't publish |
| Market value (current) | TM | FotMob | human-curated > algorithmic |
| Market value (history) | FotMob | — | only source with timestamped series |
| WC tournament stats | FIFA fdh-api | FotMob | 119 fdh keys >> 14 FotMob keys |
| WC rating | FotMob | — | FIFA doesn't publish a per-match player rating |
| Career club / national totals | FotMob | — | only source with season-by-season |
| Recent form rating | FotMob | — | only source |
| Fantasy ownership / points | FIFA Fantasy | — | source-of-truth |

You're welcome to override any of these when picking columns for the view.
