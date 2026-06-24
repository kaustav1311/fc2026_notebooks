# Recommender Factor Signal — EDA Report

Generated: 2026-06-24T13:02:00.248403+00:00
Closed observations: **1388 (player, round)** rows across rounds [1, 2]
Position breakdown: {'MID': 524, 'DEF': 451, 'FWD': 323, 'GK': 90}

Ground truth: `points` (actual fantasy points scored). Distribution: mean=3.00, std=3.59, max=24, p95=10

> All correlations below use Pearson. 'corr' is signed (+ means more-of-this → more points). Sample sizes drop when a feature is null — those rows are excluded.

## 1. Polymarket calibration (per market type)

| Market               |   Brier |   Mean Pred |   Mean Actual |   N | Note                                                         |
|:---------------------|--------:|------------:|--------------:|----:|:-------------------------------------------------------------|
| Draw                 |   0.003 |       0.307 |         0.293 |  41 | lower Brier = better-calibrated                              |
| Moneyline (any side) | nan     |       0.347 |       nan     |  82 | post-hoc — calibration not meaningful from resolved snapshot |
| O/U 2.5              |   0.163 |       0.403 |         0.535 | 129 | lower Brier = better-calibrated                              |

**Interpretation**: lower Brier = better calibration. <0.15 is decent. Reservation: closed-market Yes prices are post-event for moneyline; real calibration needs PRE-match snapshots saved over time. Recommend extending notebook 13 to append pre-match price history (currently overwrites).

## 2. Weather clustering (16 venues × match conditions)

|   cluster |   n_matches |   n_venues |   mean_temp |   mean_humidity |   mean_wbgt | venues                                        |
|----------:|------------:|-----------:|------------:|----------------:|------------:|:----------------------------------------------|
|         0 |          42 |         13 |      23.231 |          65.143 |      20.188 | Atlanta, Boston, Dallas, Houston, Kansas City |
|         1 |          43 |         11 |      30.342 |          39.767 |      23.535 | Atlanta, Boston, Dallas, Houston, Miami       |
|         2 |           9 |          2 |      19.000 |          79.000 |      17.406 | Guadalajara, Mexico City                      |
|         3 |           2 |          2 |      19.350 |          98.500 |      19.515 | Boston, Kansas City                           |

**Cluster vs outcome (closed matches only):**
|   cluster |      n |   mean_total_goals |   draw_rate |   mean_margin |
|----------:|-------:|-------------------:|------------:|--------------:|
|     0.000 | 25.000 |              3.240 |       0.280 |         2.040 |
|     1.000 | 14.000 |              2.857 |       0.429 |         1.143 |
|     2.000 |  4.000 |              2.500 |       0.000 |         1.500 |

**Read**: clusters with high mean_total_goals favor over-bets; high draw_rate favors evenly-tied-CS strategies; high mean_margin favors lopsided plays.

## 3. Other-market depth signal (A2c)

`depth_ratio` (vol_other / vol_moneyline) vs `total_goals`: **corr = 0.093**, n=41
| depth_bucket   |   n |   mean_total_goals |   mean_margin |   draw_rate |
|:---------------|----:|-------------------:|--------------:|------------:|
| low            |  14 |              2.857 |         1.143 |       0.286 |
| mid            |  13 |              2.846 |         1.615 |       0.231 |
| high           |  14 |              3.357 |         2.357 |       0.357 |

**Read**: |corr| < 0.15 → no strong signal yet. Drop A2c or wait for more closed fixtures.

## 4. 365scores trend hit rate (per lineTypeId × percentage bucket)

| category     | pct_bucket   |   n |   hit_rate |
|:-------------|:-------------|----:|-----------:|
| 1st-half     | 0.7-0.8      |   9 |      0.222 |
| 1st-half     | 0.8-0.9      |   5 |      0.600 |
| 1st-half     | 0.9+         |   2 |      0.500 |
| BTTS         | <0.7         |   1 |      0.000 |
| BTTS         | 0.7-0.8      |  13 |      0.615 |
| BTTS         | 0.8-0.9      |  15 |      0.667 |
| BTTS         | 0.9+         |   4 |      1.000 |
| doubleChance | <0.7         |   1 |      1.000 |
| doubleChance | 0.7-0.8      |  10 |      0.900 |
| doubleChance | 0.8-0.9      |  19 |      0.737 |
| doubleChance | 0.9+         |  21 |      0.952 |
| first-goal   | <0.7         |   1 |      0.000 |
| first-goal   | 0.7-0.8      |  19 |      0.737 |
| first-goal   | 0.8-0.9      |  21 |      0.762 |
| first-goal   | 0.9+         |   5 |      0.400 |
| result       | <0.7         |   3 |      0.333 |
| result       | 0.7-0.8      |  36 |      0.444 |
| result       | 0.8-0.9      |  27 |      0.481 |
| result       | 0.9+         |  12 |      0.750 |
| totals       | <0.7         |   2 |      1.000 |
| totals       | 0.7-0.8      |  25 |      0.600 |
| totals       | 0.8-0.9      |  24 |      0.500 |
| totals       | 0.9+         |  16 |      0.500 |

**Overall hit rate across all resolved trends**: 61.9% (n=291)

**Read**: trends with `percentage ≥ 0.9` should be more reliable. Use category-specific multipliers for A12 — e.g. doubleChance (lineTypeId=14) hit rate > result (1).

## 5. Factor signal — correlation of each catalog factor with actual fantasy points


Per-position correlation of catalog factors (§B Floor + §C Ceiling) with actual `points`. Bold-worthy factors have |corr| ≥ 0.20 in their position. Drop candidates: |corr| < 0.05 across all positions.
| factor                      |     DEF |     FWD |      GK |     MID |   max_abs |
|:----------------------------|--------:|--------:|--------:|--------:|----------:|
| B8 goals_per_app            |   0.311 |   0.879 | nan     |   0.752 |     0.879 |
| C5 form_fifa                |   0.712 |   0.799 |   0.671 |   0.773 |     0.799 |
| C5 last_round_pts           |   0.587 |   0.691 |   0.521 |   0.694 |     0.694 |
| B7 sot_per_app (FWD)        |   0.202 |   0.653 | nan     |   0.506 |     0.653 |
| B14 power_atk_score         |   0.249 |   0.631 | nan     |   0.632 |     0.632 |
| C1 fifa_wc_sot_total        |   0.202 |   0.588 | nan     |   0.440 |     0.588 |
| D1 percent_selected_inverse |   0.188 |   0.465 |   0.274 |   0.195 |     0.465 |
| C5 recent5_rating           |   0.338 |   0.462 |   0.436 |   0.393 |     0.462 |
| B14 power_gk_score          | nan     | nan     |   0.400 | nan     |     0.400 |
| C7 fotmob_touches_opp_box   |   0.203 |   0.391 | nan     |   0.294 |     0.391 |
| B6 cc_per_app (MID)         |   0.130 |   0.385 |  -0.036 |   0.286 |     0.385 |
| B14 power_cre_score         |   0.141 |   0.163 | nan     |   0.308 |     0.308 |
| C7 fotmob_big_chances       |   0.167 |   0.284 | nan     |   0.276 |     0.284 |
| B14 power_def_score         |   0.283 |  -0.063 | nan     |   0.041 |     0.283 |
| B3 saves_per_app (GK)       | nan     | nan     |   0.209 | nan     |     0.209 |
| C8 fotmob_duels_won_pct     |   0.036 |   0.030 |   0.190 |   0.039 |     0.190 |
| B1 start_pct_recent10       |  -0.013 |   0.160 |   0.119 |   0.175 |     0.175 |
| B5 tackles_per_app (MID)    |   0.052 |   0.077 |   0.054 |   0.174 |     0.174 |
| B1 start_pct_recent5        |   0.052 |   0.166 |   0.128 |   0.154 |     0.166 |
| B9 penalty_won_per_app      | nan     |   0.018 | nan     |   0.076 |     0.076 |

**Strongest cross-position factors** (max |corr| ≥ 0.20):
- **B8 goals_per_app** — keep, heavy weight
- **C5 form_fifa** — keep, heavy weight
- **C5 last_round_pts** — keep, heavy weight
- **B7 sot_per_app (FWD)** — keep, heavy weight
- **B14 power_atk_score** — keep, heavy weight
- **C1 fifa_wc_sot_total** — keep, heavy weight
- **D1 percent_selected_inverse** — keep, heavy weight
- **C5 recent5_rating** — keep, heavy weight
- **B14 power_gk_score** — keep, heavy weight
- **C7 fotmob_touches_opp_box** — keep, heavy weight
- **B6 cc_per_app (MID)** — keep, heavy weight
- **B14 power_cre_score** — keep, heavy weight
- **C7 fotmob_big_chances** — keep, heavy weight
- **B14 power_def_score** — keep, heavy weight
- **B3 saves_per_app (GK)** — keep, heavy weight

**Drop candidates** (max |corr| < 0.05):

> **Caveat**: `C5 form_fifa` and `C5 last_round_pts` are partly autocorrelated with `points` (FIFA's `form` is a derived rolling avg of recent points, and `last_round_pts` is literally points scored in the prior round). They're predictive *if* we accept a lag-1 model — fine for round-N+1 prediction, but treat the correlation as an upper bound.

> `D1 percent_selected_inverse` came out POSITIVELY correlated — high-ownership players scored more, opposite to the differential thesis. Read: ownership tracks expected quality. The Scouting Bonus (D2) is a separate +2-pt event that only fires under <5%, so the differential strategy lives on that bonus, not on raw negative-ownership correlation.

## 6. Normalization-mode horserace


Three modes tested as predictors of round points. Same Floor-proxy formula per position, only the denominator changes. Per-position R² shows which mode generalizes best.
| position   | stat                  |   normalized_p90 |   per_appearance |   totals |
|:-----------|:----------------------|-----------------:|-----------------:|---------:|
| DEF        | tackles_total         |            0.052 |            0.052 |    0.044 |
| FWD        | shots_on_target_total |            0.653 |            0.653 |    0.660 |
| GK         | saves                 |            0.209 |            0.209 |    0.310 |
| MID        | chances_created_total |            0.286 |            0.286 |    0.291 |
| MID        | tackles_total         |            0.174 |            0.174 |    0.164 |

**Read**: highest-corr mode per row is the best for that (position, stat) combo. Pattern across rows suggests which mode to default each strategy to.

## 7. Top-scorer archetype mining (group stage)


Top-20% threshold: ≥5 pts. 282 top-performance rows.

8 archetypes mined. Saved to `data/eda/archetypes_group_stage.json`.
| name                           |   n |   mean_pts | exemplars                                                                                                |
|:-------------------------------|----:|-----------:|:---------------------------------------------------------------------------------------------------------|
| POPULAR_MID_CHANCE_CREATOR     |  25 |      9.400 | Isak (Sweden, R1, 16pt), Vargas (Switzerland, R2, 15pt), Gyökeres (Sweden, R1, 14pt)                     |
| DIFFERENTIAL_DEF_SET_PIECE_DEF |  83 |      8.800 | Santos Carneiro da Cunha (Brazil, R2, 15pt), Singo (Côte d'Ivoire, R1, 12pt), Dembélé (France, R2, 12pt) |
| DIFFERENTIAL_GK_SHOT_STOPPER   |  14 |      8.600 | Évora Dias (Cabo Verde, R1, 11pt), Beiranvand (IR Iran, R2, 11pt), Crépeau (Canada, R2, 9pt)             |
| POPULAR_FWD_BALANCED           |   8 |     13.800 | Messi (Argentina, R1, 19pt), Haaland (Norway, R1, 17pt), Messi (Argentina, R2, 14pt)                     |
| DIFFERENTIAL_MID_BALANCED      |  95 |      8.400 | Sarr (Senegal, R2, 15pt), Saliba (Canada, R2, 13pt), Metcalfe (Australia, R1, 11pt)                      |
| POPULAR_GK_SHOT_STOPPER        |  11 |      8.700 | Room (Curaçao, R2, 14pt), Beach (Australia, R1, 11pt), Gill (Paraguay, R2, 10pt)                         |
| POPULAR_MID_BALANCED           |  44 |     12.100 | David (Canada, R2, 24pt), Gakpo (Netherlands, R2, 19pt), Ueda (Japan, R2, 18pt)                          |
| DIFFERENTIAL_MID_BALANCED_c7   |   1 |      9.000 | Mahmic (Bosnia and Herzegovina, R2, 9pt)                                                                 |

## 8. Implied vs proposed constants (per K: top-N not top-quartile)


Elite cohort = top 50 player-rounds (min=11 pts, max=24 pts).
| threshold                                              |   proposed |   data_implied | evidence                                                               |
|:-------------------------------------------------------|-----------:|---------------:|:-----------------------------------------------------------------------|
| MID tackles_per_app floor                              |      4.500 |          0.500 | 25th pctile among top-50 MID picks (n=17)                              |
| MID tackles_per_90min floor (using fifa_wc_TimePlayed) |      4.500 |          0.520 | per-minute variant — K's preferred normalization                       |
| MID chances_created_per_app floor                      |      1.500 |          0.000 | 25th pctile among top-50 MID picks                                     |
| MID fotmob_wc_chances_created total floor              |    nan     |          1.000 | using stg_players_view total directly (K's note: refer to fotmob data) |
| SB ownership gate — MEDIAN of SB earners               |      5.000 |          0.600 | 233 SB-earning obs; 75th pctile=1.9%, max=6.8%                         |
| Form floor for elite (top-N)                           |      6.000 |          4.700 | 25th pctile among top-50 player-rounds; median=5.85                    |
| recent5_fotmob_rating floor for elite                  |      7.000 |          7.110 | 25th pctile among top-50; independent of points autocorrelation        |
| FWD avg_attacking_score floor                          |      0.700 |          6.219 | 25th pctile among top-50 FWD picks (n=21)                              |
| DEF avg_defensive_score floor                          |      0.700 |          5.766 | 25th pctile among top-50 DEF picks (n=8)                               |
| MID avg_creativity_score floor                         |      0.700 |          6.130 | 25th pctile among top-50 MID picks (n=17)                              |

_K to override the `proposed` column where data_implied differs — copy this table into the catalog._

## 2b. Surface + roof overlay correlation

**By surface type:**
| surface       |   n |   mean_total_goals |   draw_rate |   mean_margin |
|:--------------|----:|-------------------:|------------:|--------------:|
| grass         |  20 |              2.850 |       0.300 |         1.650 |
| grass_overlay |  23 |              3.217 |       0.304 |         1.739 |

**By roof type:**
| roof_type   |   n |   mean_total_goals |   draw_rate |   mean_margin |
|:------------|----:|-------------------:|------------:|--------------:|
| fixed       |   4 |              3.500 |       0.500 |         1.500 |
| open        |  27 |              2.630 |       0.259 |         1.444 |
| retractable |  12 |              3.833 |       0.333 |         2.333 |

**Read**: differences in mean_total_goals + draw_rate across surface/roof categories quantify the stadium effect. With MD1+MD2 sample sizes, treat as directional only.

## 10. Broad correlation sweep — every numeric stg_players_view column vs points


K asked: are we using the 100+ FIFA stats from stg_players_view? Now we are. df has **237 cols** total. Scanning all numeric ones for predictive signal against `points` per position. Reports the top-30 by max-|corr| across positions.

**Top 30 numeric stg_players_view factors by max |corr| across positions:**
| factor                        |   DEF |   FWD |      GK |   MID |   max_abs |
|:------------------------------|------:|------:|--------:|------:|----------:|
| B8 goals_per_app              | 0.311 | 0.879 | nan     | 0.752 |     0.879 |
| avg_points                    | 0.754 | 0.822 |   0.676 | 0.831 |     0.831 |
| total_points                  | 0.713 | 0.800 |   0.671 | 0.774 |     0.800 |
| C5 form_fifa                  | 0.712 | 0.799 |   0.671 | 0.773 |     0.799 |
| form                          | 0.712 | 0.799 |   0.671 | 0.773 |     0.799 |
| fifa_wc_Goals                 | 0.229 | 0.720 | nan     | 0.636 |     0.720 |
| last_round_points             | 0.587 | 0.691 |   0.521 | 0.694 |     0.694 |
| C5 last_round_pts             | 0.587 | 0.691 |   0.521 | 0.694 |     0.694 |
| sb_total                      | 0.614 | 0.535 |   0.410 | 0.682 |     0.682 |
| fotmob_wc_fotmob_rating       | 0.468 | 0.677 |   0.494 | 0.585 |     0.677 |
| wc_rating                     | 0.468 | 0.677 |   0.494 | 0.585 |     0.677 |
| shots_on_target_total         | 0.197 | 0.660 | nan     | 0.476 |     0.660 |
| B7 sot_per_app (FWD)          | 0.202 | 0.653 | nan     | 0.506 |     0.653 |
| avg_attacking_score           | 0.249 | 0.631 | nan     | 0.632 |     0.632 |
| B14 power_atk_score           | 0.249 | 0.631 | nan     | 0.632 |     0.632 |
| fifa_wc_XG                    | 0.165 | 0.631 | nan     | 0.361 |     0.631 |
| fifa_wc_AttemptAtGoalOnTarget | 0.202 | 0.588 | nan     | 0.440 |     0.588 |
| C1 fifa_wc_sot_total          | 0.202 | 0.588 | nan     | 0.440 |     0.588 |
| recent5_goals                 | 0.187 | 0.533 | nan     | 0.444 |     0.533 |
| fifa_wc_AttemptAtGoal         | 0.163 | 0.484 | nan     | 0.254 |     0.484 |
| price                         | 0.220 | 0.478 |   0.134 | 0.199 |     0.478 |
| D1 percent_selected_inverse   | 0.188 | 0.465 |   0.274 | 0.195 |     0.465 |
| percent_selected              | 0.188 | 0.465 |   0.274 | 0.195 |     0.465 |
| recent5_fotmob_rating         | 0.338 | 0.462 |   0.436 | 0.393 |     0.462 |
| C5 recent5_rating             | 0.338 | 0.462 |   0.436 | 0.393 |     0.462 |
| recent10_goals                | 0.142 | 0.457 | nan     | 0.361 |     0.457 |
| recent15_goals                | 0.132 | 0.456 | nan     | 0.315 |     0.456 |
| recent5_player_of_the_match   | 0.069 | 0.453 |   0.233 | 0.295 |     0.453 |
| fifa_wc_CleanSheets           | 0.382 | 0.045 |   0.440 | 0.047 |     0.440 |
| fifa_wc_Assists               | 0.209 | 0.352 | nan     | 0.424 |     0.424 |

**Surprises to add to the catalog** (factors NOT in current §B/§C that show ≥0.30 max-corr):
- **B8 goals_per_app** — max|corr|=0.88
- **avg_points** — max|corr|=0.83
- **total_points** — max|corr|=0.80
- **C5 form_fifa** — max|corr|=0.80
- **fifa_wc_Goals** — max|corr|=0.72
- **C5 last_round_pts** — max|corr|=0.69
- **sb_total** — max|corr|=0.68
- **fotmob_wc_fotmob_rating** — max|corr|=0.68
- **wc_rating** — max|corr|=0.68
- **shots_on_target_total** — max|corr|=0.66
- **B7 sot_per_app (FWD)** — max|corr|=0.65
- **B14 power_atk_score** — max|corr|=0.63
- **fifa_wc_XG** — max|corr|=0.63
- **C1 fifa_wc_sot_total** — max|corr|=0.59
- **recent5_goals** — max|corr|=0.53
- **fifa_wc_AttemptAtGoal** — max|corr|=0.48
- **price** — max|corr|=0.48
- **D1 percent_selected_inverse** — max|corr|=0.47
- **C5 recent5_rating** — max|corr|=0.46
- **recent10_goals** — max|corr|=0.46
- **recent15_goals** — max|corr|=0.46
- **recent5_player_of_the_match** — max|corr|=0.45
- **fifa_wc_CleanSheets** — max|corr|=0.44
- **fifa_wc_Assists** — max|corr|=0.42

## 11. Prospective archetypes (past-season club form + market value)


Separate from §7's retrospective archetypes (mined from MD1+MD2 top scorers), these are PROSPECTIVE — cluster the entire 1488 player pool by past-season club performance + market value + national-team profile. Useful for early-round picks where WC sample is thin.

6 prospective archetypes. Saved to `data/eda/archetypes_prospective.json`.
| name                         |   n |   mean_value_m |   mean_apps | exemplars                                                          |
|:-----------------------------|----:|---------------:|------------:|:-------------------------------------------------------------------|
| EMERGING_VETERAN_Midfielder  | 348 |          6.000 |     416.700 | Joshua KIMMICH (GER), Youri TIELEMANS (BEL), Mikel OYARZABAL (ESP) |
| EMERGING_MID_CAREER_Defender | 582 |          4.600 |     179.400 | Robin ROEFS (NED), Zion Suzuki (JPN), Carney CHUKWUEMEKA (AUT)     |
| MEGASTAR_VETERAN_Forward     |  50 |         93.700 |     314.600 | Lamine YAMAL (ESP), Michael OLISE (FRA), Erling HAALAND (NOR)      |
| EMERGING_VETERAN_Forward     |  28 |         11.500 |     621.600 | Harry KANE (ENG), BRUNO FERNANDES (POR), MOHAMED SALAH (EGY)       |
| ESTABLISHED_VETERAN_Forward  | 203 |         37.000 |     246.700 | Micky VAN DE VEN (NED), Nick WOLTEMADE (GER), Marc GUEHI (ENG)     |
| EMERGING_YOUNG_Defender      |  35 |          0.400 |      76.100 | Ermin MAHMIC (BIH), Mouhib CHAMAKH (TUN), JASSEM GABER (QAT)       |

## 9. Nation-strength composite validation (§I)


`nation_strength_delta` (home − away) vs `actual_margin` (home − away goals): **corr = 0.710** across 43 closed fixtures.

**Fixtures where the composite was wrong** (upsets the model missed):
| home_nation_id   | away_nation_id   |   home_score |   away_score |   strength_delta |
|:-----------------|:-----------------|-------------:|-------------:|-----------------:|
| KOR              | CZE              |            2 |            1 |           -0.035 |
| CAN              | BIH              |            1 |            1 |            0.255 |
| QAT              | SUI              |            1 |            1 |           -0.147 |
| BRA              | MAR              |            1 |            1 |            0.335 |
| NED              | JPN              |            2 |            2 |            0.048 |
| ESP              | CPV              |            0 |            0 |            0.204 |
| BEL              | EGY              |            1 |            1 |            0.114 |
| KSA              | URU              |            1 |            1 |           -0.143 |
| IRN              | NZL              |            2 |            2 |            0.179 |
| POR              | COD              |            1 |            1 |            0.043 |
| CZE              | RSA              |            1 |            1 |            0.139 |
| ECU              | CUW              |            0 |            0 |            0.206 |
| BEL              | IRN              |            0 |            0 |            0.018 |
| URU              | CPV              |            2 |            2 |           -0.029 |

**Top 5 nations by composite strength**:
| nation_id   |   fifa_rank |   trophies_won |   nation_total_strength |   i1_static |   i2_form |   i4_player |
|:------------|------------:|---------------:|------------------------:|------------:|----------:|------------:|
| GER         |           9 |              4 |                   0.702 |       0.742 |     0.714 |       0.645 |
| ARG         |           1 |              3 |                   0.660 |       0.621 |     0.827 |       0.475 |
| BRA         |           5 |              5 |                   0.653 |       0.828 |     0.498 |       0.685 |
| ESP         |           3 |              1 |                   0.602 |       0.606 |     0.468 |       0.777 |
| FRA         |           2 |              2 |                   0.592 |       0.689 |     0.667 |       0.397 |

**Bottom 5 nations**:
| nation_id   |   fifa_rank |   nation_total_strength |
|:------------|------------:|------------------------:|
| HAI         |          82 |                   0.134 |
| CUW         |          88 |                   0.153 |
| JOR         |          64 |                   0.166 |
| UZB         |          57 |                   0.176 |
| PAN         |          41 |                   0.178 |