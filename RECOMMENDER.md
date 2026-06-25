# Fantasy Recommender Layer ("K's 2 cents")

Read this first if you're picking up work on the recommender layer. It indexes everything that composes the 4-model fantasy intelligence system on top of the warehouse.

**Status (2026-06-25)**: live in production. Runs every hourly tick after notebook 16. Outputs 6 JSONs the PWA renders as 4 Challenge cards + per-position picks + drill-down round-tracking.

---

## 1. Where the code lives

| Path | What |
|---|---|
| `lib/recommender.py` | The core. Bracket scorer, model registry, fixture profiler, nation strength composite, archetype miner, assemblers, round-tracking. **Single source of truth for model behavior.** |
| `lib/scores365.py` | 365scores trends fetcher (used by notebook 18). |
| `17_fantasy_recommender.py` | Hourly orchestrator. Calls `refresh_live_percent_selected` → `assemble_fixture_profile` → `score_for_model` (×4) → assemblers → emit JSONs → write snapshot. |
| `17a_eda_factor_signal.py` | One-shot EDA (not on cron). 9 sections producing `data/eda/recommender_factor_signal.md`. Re-run manually when factor weights change. |
| `18_scores365_trends.ipynb` | Hourly snapshot of 365scores trend payload. Stores per-(match, trend, snapshot_ts) so trend `percentage` evolution can be tracked. |
| `_build_stg_team_match_metrics.py` | Builds `wc26_stg_team_match_metrics.parquet` (per-(match, team) aggregation of player_match_stats_wide). Feeds §I.2 nation form metrics. |
| `build_model_spec_xlsx.py` | Regenerates `K_2_Cents_Model_Spec.xlsx` from current model state. |

Refresh order (in `refresh.py:NOTEBOOK_BUCKETS["hourly"]`): … nb_16 staging → **17_fantasy_recommender (.py)** → **18_scores365_trends (.ipynb)**.

---

## 2. The 4 models

Defined in `lib/recommender.py:MODEL_REGISTRY`. To change model behavior, edit this dict — no other code change required for weight tweaks.

| id | name | weights (w_b1/b2/b3/b4) | post_boosts | sb_quota | assembler |
|---|---|---|---|---:|---|
| `m1_banker` | Banker | 0.20/0.25/0.20/0.35 | — | 9 | default |
| `m2_form_hunter` | Form Hunter | 0.10/0.20/0.40/0.30 | `recent_form_streak` (×0.30) | 6 | default |
| `m3_stat_max` | Stat Maximizer | 0.15/0.50/0.25/0.10 | `creativity_engine` (×0.20) + `powerrank_pure` (×0.20) | 3 | default |
| `m4_sb_hunter` | SB Hunter | 0.20/0.30/0.20/0.30 | `sure_shot_fwd` + `influential_mid` (both w=0, surfaced as sub-scores) | 12 | sb_hunter |

`sb_quota` is a HARD constraint with ±1 tolerance in the assembler — that's what diverges the 4 squads. Without it they'd all converge on Messi/Haaland/Kane.

---

## 3. The 5 brackets (Player Strength Score)

```
EV = (w1·B1 + w2·B2 + w3·B3 + w4·B4) × B5 × 6.5
```

Each bracket is rank-percentiled within position so B1..B4 ∈ [0, 1]. B5 ∈ [0.20, 1.80] multiplicatively.

- **B1 PlayerOverall** — nation_strength + club_senior_weighted_avg_rating + club goals/apps + national_senior_* + log(value_fotmob_latest_eur)
- **B2 WCPerfRating** — position-routed per-90 from `fifa_wc_*` + `fotmob_wc_*`, plus FDH power-rank scores, minus discipline (YC+2·RC+FoulsAgainst). Position routing in `B2_STATS` dict.
- **B3 ExternalRatings** — `fotmob_wc_fotmob_rating` + `wc_rating` + (0.5·recent5 + 0.3·recent10 + 0.2·recent15) FotMob ratings
- **B4 FantasyMeta** — form + avg_points + total_points + last_round_points + consistency + sb_total + sigmoid(5%-gate × sb_track multiplier)
- **B5 FixtureMultiplier** — position-conditional, multiplicative. FWD/MID: `(0.5+goals_index) × (1+(1-opp_strength)·0.3) × (1+heavy_hitter·0.15)`. DEF/GK: `(0.6+team_cs_index) × (1+(1-opp_strength)·0.4) × (1+heavy_hitter·0.10)`. Then × stage_flip × (1-weather_drag) × trend_mod × divergence_mod.

EV scale: real FIFA Fantasy XIs score 50-90 per round. `×6.5` puts elite tiles at ~10-12 EV and XI totals at 80-95.

---

## 4. Outputs (every tick)

PWA-bound JSONs land in `data/processed/json/` and get synced to `E:/fifawc2026/public/data/` via `scripts/sync-warehouse.mjs`:

| File | Shape | Consumer |
|---|---|---|
| `wc26_fantasy_recommendations.json` | 1248 rows (M1 scored, back-compat) | PWA + back-compat |
| `wc26_fantasy_models.json` | 4 × 1248 slim rows | PWA Player Picks tab |
| `wc26_fantasy_strategy_squads.json` | 4 Challenge squads (XI + bench + 12th) | PWA Challenges tab |
| `wc26_fantasy_joint_picks.json` | consensus + per-model surprises + per-position top | PWA Joint tab |
| `wc26_fantasy_round_tracking.json` | per-(model, round) projected vs actual | PWA Challenge drill-down |
| `wc26_fantasy_position_suggestor.json` | Top 15 + Look-out-for per position | legacy back-compat |

Internal-only artifacts:
- `data/processed/wc26_fantasy_recommendations.parquet` — same as the JSON, parquet for re-analysis
- `data/processed/history/round_NN/snapshot_{TS}.json` — pre-round-lock freeze used by `build_round_tracking` after a round closes
- `data/eda/archetypes_*.json` — written by notebook 17a

---

## 5. Live %selected — non-negotiable rule

The model and the PWA MUST see the same ownership at scoring time, or the SB-quota math drifts from what the UI shows. Two synchronization points:

- **Warehouse**: notebook 17 calls `lib.recommender.refresh_live_percent_selected(force=True)` at the top of every run. Pulls `play.fifa.com/json/fantasy/players.json` via `cache_raw(max_age_days=0)`. Returns `{fantasy_player_id: percentSelected}`. Passed to `score_for_model(...,live_pct_selected=...)`.
- **PWA**: `src/services/fifaFantasy.ts:loadLiveFantasyPlayers()` hits the same endpoint via the `api/fifa-fantasy.ts` edge proxy. UI overrides snapshot `percent_selected` per tile and marks live values with a `●` prefix.

Don't mix snapshot ownership with live ownership at any render. Either both, or neither.

---

## 6. Round tracking + transfer rules

`build_round_tracking(squads_by_round)` in `lib/recommender.py` joins each closed round against `fantasy_player_round_stats` and applies FIFA Fantasy's actual transfer rules:

```python
FREE_TRANSFERS_BY_ROUND = {
    1: 2, 2: 2, 3: 2,      # group stage
    4: float("inf"),       # R32 — unlimited
    5: 4, 6: 4,            # R16, QF
    7: 5,                  # SF
    8: 6,                  # Final
}
EXTRA_TRANSFER_PENALTY = -3
```

Plus captain doubling and vice-captain promotion when captain plays 0 minutes. Output is per-(model, round) projected vs actual with transfers count + penalty + final pts.

Historical snapshots (`data/processed/history/round_NN/snapshot_*.json`) are pre-round-lock freezes — the newest snapshot whose `snapshot_ts` is BEFORE round start is the "committed" prediction.

---

## 7. Gotchas other agents hit

- **Python `NaN` literals crash `JSON.parse`** in the browser. ALL PWA-bound JSON must go through `dump_js_safe()` in `17_fantasy_recommender.py` (NaN/Inf → null, then `allow_nan=False` as belt-and-braces).
- **Don't pass `cache: "no-cache"`** to fetch on `/data/*.json` from the PWA — collides with the service-worker NetworkFirst handler and returns spurious misses.
- **Loader failures must surface**, not return empty defaults silently. PWA components use `Promise.allSettled` + an `ErrorBoundary` so blanks don't hide root causes.
- **`pick_target_round`** prefers the round whose `[start_date, end_date]` window contains now AND requires the round to have fixtures (skips R32 TBD until the bracket fills). Don't use `status == "playing"` alone.
- **MD1/MD2 historical snapshots don't exist** in `data/processed/history/round_NN/` — the new model architecture started writing snapshots at MD3. Round tracking shows actuals from MD3 onward only.

---

## 8. Companion docs + related memories

In this repo:
- [STAGING_CONTRACT.md](STAGING_CONTRACT.md) — the staging tables this layer reads
- [METRICS_MAP.md](METRICS_MAP.md) — source-by-source view of the underlying metrics
- [REFRESH.md](REFRESH.md) — refresh cadence per notebook
- `WC26_DATA_DICTIONARY.xlsx` — column dictionary per parquet (regen with `_build_dictionary.py`)
- `K_2_Cents_Model_Spec.xlsx` — model spec workbook (regen with `build_model_spec_xlsx.py`)
- `D-1-Doc.txt` — original optimization blueprint

In `~/.claude/projects/E--fc2026-notebooks/memory/`:
- `project_fantasy_recommender.md` — model design + architecture
- `project_pwa_data_layer.md` — how outputs reach the PWA
- `feedback_pwa_json_pitfalls.md` — the three traps above with full diagnostics
- `feedback_ui_ergonomics.md` — K's design preferences (FIFA team-page mirror, drill-down, binary filters)

---

## 9. Common tasks

**Tweak model weights** → edit `MODEL_REGISTRY` in `lib/recommender.py`, re-run `python 17_fantasy_recommender.py`, sync to PWA.

**Add a 5th model** → append to `MODEL_REGISTRY` with a new id, weights, post_boosts, sb_quota, assembler. Notebook 17 iterates the registry; PWA `MODEL_LABELS` const needs the new id + tone color.

**Re-run EDA** → `python 17a_eda_factor_signal.py`. Cached closed-round frame at `data/eda/closed_rounds_cache.parquet` skips the slow rebuild. Review `data/eda/recommender_factor_signal.md` for data-implied thresholds.

**Validate against K's instinct** → after notebook 17 finishes, eyeball top per-position picks. They should naturally distribute across the round's favored-fixture teams (MD3 produced NED/ARG/BEL/MAR/GER/CIV/JPN without any hardcoded team list). If they don't, B5 fixture-multiplier composition is the first thing to inspect.

**Backfill a missed round** → set `FORCE_REFRESH=1` and `--notebook 17_fantasy_recommender`. The orchestrator picks the current round per `pick_target_round`; for a specific past round, edit the call site temporarily.
