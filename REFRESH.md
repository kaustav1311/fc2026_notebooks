# Refresh cadence & triggers

Tournament window: **2026-06-11 → 2026-07-19**. Outside this window most tables are effectively frozen. Inside it, the warehouse must self-update on a schedule.

Every read goes through `lib/io.cache_raw`, which writes raw payloads to `data/raw/{source}/{YYYY-MM-DD}_{name}.{ext}`. Re-runs hit disk unless `force_refresh=True`. To trigger a real refresh, call the notebook with `force_refresh=True` set in cell 1, or delete the relevant cache file.

---

## Event-driven model (2026-06-22 onward)

Three notebooks (`nb_06`, `nb_08`, `nb_04`) used to blindly re-fetch all 48 teams + 1248 player profiles + 250 ref pages every daily run. They now consult `lib/events` and only re-fetch entities that an event genuinely invalidated:

- **Event-A** — a match's `status` flipped to `finished` since the last commit.
  - **nb_07** (FDH match_stats, powerranking): immutable-after-finish — sets `processed["fdh_match_stats"][mid]="done"` and skips the HTTP on subsequent ticks.
  - **nb_11** (FotMob matchDetails): same `processed["fotmob_match_details"][mid]` flag.
  - **nb_09** (FDH season aggregate): single bulk call, only refetched if at least one new match finished this tick.
  - **nb_08** (FotMob playerData + TM value_history): per-player gate — only re-fetch players who appeared in any newly-finished match.

- **Event-C** — a team played in a newly-finished match.
  - **nb_06** (FIFA squad pages): per-team gate.
  - **nb_08** (FotMob team_squad + TM team squad): per-team gate (catches mid-tournament injury subs).

- **Event-D** — a ref officiated a newly-finished match.
  - **nb_04** (FootyMetrics profile + recent fixtures): per-ref gate.

**State file**: `data/.event_state.json`. Schema:

```json
{
  "schema_version": 1,
  "last_tick_utc": "2026-06-22T10:30:00Z",
  "matches":   {"<fifa_match_id>": "finished" | "scheduled" | "live"},
  "rounds":    {"<round_id>": "complete" | "playing" | "scheduled"},
  "processed": {"<source>": {"<key>": "done"}},
  "last_fetch":{"<source>": {"<key>": "YYYY-MM-DD"}}
}
```

`refresh.py` calls `events.commit(matches, rounds)` at the END of a successful bundle to persist the status maps — a failed bundle skips the commit, so the next tick still sees the same delta (idempotent).

**Forcing a full sweep**: `python refresh.py --force-refresh` exports `FORCE_ALL_EVENTS=1`, which makes `lib/events` return the full universe for every helper (every finished match looks newly-finished, etc.). Used by the daily backstop bundle.

**Cost saving** vs the old daily blind-sweep: a typical mid-tournament 3-h tick now triggers roughly **30-150 fetches** (~22-32 players × ~4 newly-finished matches + their teams + their refs) instead of the previous ~3,000.

---

## Buckets

### 🟦 Frozen — rebuild only on schema change

| Table | Source | Re-run when |
|---|---|---|
| `wc26_nations` | hand-curated seed + TM bundle + ESPN + FotMob | nation list changes (never, mid-tournament) |
| `wc26_stadiums` | hand-curated venue spec + Open-Meteo elevation echo | venue swap (very rare; FIFA last did it in 2025) |
| `METRICS_MAP.md`, `REFRESH.md` | hand-written | when a new endpoint joins the pipeline |

Notebooks: `01_nations.ipynb`, `02_stadiums.ipynb`, `06_players.ipynb` (officials side-effect).

### 🟨 Daily — once-per-day cron

| Table | Source | Why daily |
|---|---|---|
| `referee_master`, `referee_profile` (`career` window) | FootyMetrics `/world-cup-2026/referees` + per-ref pages | panel changes are infrequent (suspensions) |
| `referee_profile` (`last_10`, `last_25`) | FootyMetrics profile `recentMatches` block | refreshed as part of the per-ref page pull |

Notebooks: `06`, `04`. Wire as a single daily script:

```bash
python -m notebook_runner 06 04 --force-refresh
```

**Note (2026-06-24):** `08_player_enrichment` used to live here but was promoted to the hourly bucket. With event-gating in place a typical 08 tick is ~30 HTTP fetches, not 1248 — and keeping it daily meant a stale `wc26_player_match_stats_wide` (the daily bundle fires BEFORE the hourly notebooks at the 07:00 UTC slot) gave nb_08 an empty `EVENT_PIDS`, so the per-player FotMob playerData never refreshed and `wc26_player_career_national_summary` (caps/goals/assists) stayed stale for a full cycle. Hourly placement after nb_07 fixes the ordering.

### 🟥 Every 3 hours — during the tournament window

| Table | Source | What changes intra-day |
|---|---|---|
| `wc26_matches` | ESPN scoreboard + FIFA `/calendar/matches` | live status, scores, knockout TBD→real-team resolution, late referee appointments, attendance |
| `wc26_player_match_stats` | fdh-api `/stats/match/{IdIFES}/players.json` | a new match finishes → new 5K row block lands |
| `wc26_player_match_powerrank` | fdh-api `/powerranking/match/{IdIFES}.json` | per-match attacking/defensive/creativity ranks publish ~30 min post-final-whistle |
| `referee_assignments` | FIFA calendar Officials + FootyMetrics upcoming fixtures | FIFA publishes referee for each match 24–48 h ahead |
| `fantasy_rounds`, `fantasy_round_matches` | `play.fifa.com/json/fantasy/rounds.json` | round status, fixture period/minutes/scores |
| `fantasy_players` | `play.fifa.com/json/fantasy/players.json` | **`percentSelected` moves continuously** (this is the Scouting Premium input from `D-1-Doc §2`) — price + form + total points also tick |
| `fantasy_player_round_stats` | `play.fifa.com/json/fantasy/player_stats/{id}.json` | finalised at round end |
| `wc26_player_enrichment` | FotMob team page + TM team page + per-player FotMob playerData | per-player FotMob blob carries `careerHistory['national team']`; refetched on the same tick the player's match flips to finished |
| `wc26_player_market_value_history` | FotMob playerData `marketValues.values[]` + TM `marketValueDevelopment/graph/{id}` | re-emitted from cached + event-fresh playerData each tick |
| `wc26_player_career_senior`, `wc26_player_career_national` | FotMob playerData `careerHistory.*.seasonEntries[]` | caps/goals/assists tick up as matches finish — needs hourly refresh during the tournament |
| `wc26_player_career_national_summary`, `wc26_player_career_club_summary` | derived from the long career tables (youth/senior split + clubs array + current) | rebuilds whenever the long tables refresh |
| `wc26_player_market_value_summary` | derived from `wc26_player_market_value_history` (per-source latest+peak + consolidated) | rebuilds whenever the long table refreshes |
| `wc26_player_fotmob_wc`, `wc26_player_recent_matches_fotmob` | FotMob playerData (already cached hourly by 08; re-parse here) | new last-match rating lands ~15 min post-match |
| `fantasy_squads` | `play.fifa.com/json/fantasy/squads.json` | `isEliminated` flips after knockouts |
| `wc26_match_weather` | Open-Meteo `/v1/forecast` + `/v1/archive` | forecast updates hourly upstream; archive supersedes forecast once the match completes; knockout matches roll into coverage as they enter the 16-day forecast horizon |
| `wc26_polymarket_match_volume` | derived from `wc26_match_polymarket_markets` | rebuilds with parent table inside notebook 13 |
| `wc26_polymarket_winner_history` | Polymarket CLOB `/prices-history` per `world-cup-winner` market token | re-pulls once per day; appends new snapshots, dedupes by `(nation_id, date_utc)` |
| `wc26_stg_nations` | derived from `wc26_nations` (pass-through) | rebuilds with parent (frozen in practice) |
| `wc26_stg_stadiums` | derived from `wc26_stadiums` + per-stadium agg of `wc26_matches` + `wc26_match_weather` | totals and weather ranges drift hourly |
| `wc26_stg_referee_profile` | derived from `referee_profile` + join of `referee_master` | rebuilds with parents |
| `wc26_stg_matches` | derived from `wc26_matches` + joins of stadiums / weather / nations(×2) / `ref_id_bridge` → `referee_master` / `wc26_polymarket_match_volume` | re-fans every input on each tick |
| `wc26_stg_players` | derived from `wc26_player_enrichment` + 4 summary parquets + per-player agg of `wc26_player_match_stats_wide` + 4 form windows from `wc26_player_recent_matches_fotmob` | rebuilds whenever any input ticks |
| `wc26_stg_fantasy_player_totals` | derived from `fantasy_player_round_stats` (groupby fantasy_player_id, no joins) | rebuilds with parent |
| `wc26_stg_players_view` | derived from `wc26_stg_players` (column subset + 13 derived ratios + total cards) | rebuilds with parent |
| `wc26_stg_player_powerrank` | derived from `wc26_player_match_powerrank` (groupby on (fifa_player_id, fifa_team_id), mean of scores, no joins) | rebuilds with parent |
| `ref_id_bridge` | derived inside `04_referees` (numeric `fifa_referee_id` → slug `referee_id` by surname+iso3 + override CSV) | rebuilds with `04` daily; cheap |

Notebooks: `03`, `05`, `07`, **`08`** (player enrichment — promoted 2026-06-24), `09`, `10`, `11`, `12`, `13`, **`14`** (staging core), **`15`** (staging matches), **`16`** (staging players). Order matters: `08` must follow `07` so `wc26_player_match_stats_wide` is fresh when nb_08 derives `EVENT_PIDS`.

### ⏱ Event-triggered — fire on a specific moment

| Trigger | What to refresh | Why event vs cron |
|---|---|---|
| Match status flips to `finished` | `wc26_matches` (this row only), then `07` (this match only), then `09` (season aggregate) | next 3h tick would also catch it — event-trigger trims latency from ≤3h to ≤15min |
| Round status flips to `complete` | `fantasy_player_round_stats` (this round only), `fantasy_players.total_points` | round-end is the only time the per-round stats finalise |
| Group stage ends (~2026-06-28) | full `03` re-run | knockout TBD teams resolve → `fifa_*_team_id` / `home_nation_id` / `away_nation_id` backfill on ~32 rows |
| New referee appointment lands (FIFA reveals 24–48h before kickoff) | `05` only | only the affected matches need the FIFA Officials re-pull |

---

## Suggested driver

A single Python entrypoint can pick the right notebook set per tick:

```python
# refresh.py — driver picked up by cron / GitHub Actions / Airflow
from datetime import datetime, timezone

NOTEBOOK_BUCKETS = {
    "frozen": [],  # never auto-fired
    "daily":  ["06_players", "04_referees"],
    "hourly": [
        "03_matches", "05_referee_assignments",
        "07_player_match_stats",
        # 08 must follow 07 — its event-gate reads the just-written
        # wc26_player_match_stats_wide to derive EVENT_PIDS.
        "08_player_enrichment",
        "09_player_season_stats",
        "10_fifa_fantasy", "11_fotmob_wc_and_form",
        "12_match_weather", "13_polymarket",
        # Staging notebooks always run LAST so they consume fresh inputs on the
        # same tick. They make zero HTTP calls, so the extra cost is seconds.
        "14_staging_core", "15_staging_matches", "16_staging_players",
    ],
}

def is_tournament_window(now=None):
    now = now or datetime.now(tz=timezone.utc)
    return datetime(2026, 6, 11, tzinfo=timezone.utc) <= now <= datetime(2026, 7, 20, tzinfo=timezone.utc)

def pick(now=None):
    now = now or datetime.now(tz=timezone.utc)
    if not is_tournament_window(now):
        return []
    out = list(NOTEBOOK_BUCKETS["hourly"])
    if now.hour == 7:  # daily roll, 07:00 UTC
        out = NOTEBOOK_BUCKETS["daily"] + out
    return out
```

A simple cron line `0 */3 * * *` invokes this every 3 hours; the function decides daily vs hourly bundle internally.

For event-triggered refreshes during a live match, point a small webhook handler at `fdh-api`'s status field — when `fullTime: true` flips for an `IdIFES` the warehouse hasn't seen, re-pull that single match's `/stats/match/.../players.json` + `/powerranking/...` and append to the existing parquet rather than rewriting the whole file.

---

## Auto-force during tournament window

`refresh.py` sets `FORCE_REFRESH=1` automatically when `in_tournament_window(now) == True` (2026-06-11 → 2026-07-20). `lib/io.cache_raw` and `lib/io.latest_raw` both honor this env var, so the 3 h cron actually re-fetches the live endpoints — not yesterday's cached JSON.

Semantics:
- **`cache_raw`**: when `FORCE_REFRESH=1`, the cache lookup is skipped and the URL is re-fetched.
- **`latest_raw`**: when `FORCE_REFRESH=1`, returns `None` if the most recent cached file is dated before today. Today's cache is honored — this preserves the cross-cell handoff pattern (cell A writes → cell B reads back) used in notebooks 02 and 13.

Opt-outs:
- `python refresh.py --bucket hourly --no-force-refresh` — explicitly disable the auto-force.
- Outside the tournament window the auto-force does not fire (the script exits early anyway).

Without this auto-force the cache policy (`max_age_days=None` = "accept any cached file") silently kept returning day-1 JSON across every cron tick. The auto-force is the mechanism that makes the 3 h cadence meaningful.

---

## Notebook 10 (FIFA Fantasy) per-player refresh heuristic

The bulk fantasy endpoints (`rounds.json`, `squads.json`, `players.json`) are 3 files and re-fetch cheaply. The expensive endpoint is `player_stats/{fpid}.json` — 1,488 calls × ~150 ms = ~4 minutes if every player is force-refreshed on every tick.

WC26 group stage: 48 teams in 12 groups of 4, each plays 3 matches → **3 Fantasy rounds × 24 matches per round = 72 group-stage matches**. Then knockout: R32 (16) → R16 (8) → QF (4) → SF (2) → 3rd + Final (2). **Total: 8 Fantasy rounds, 104 matches.** Rounds are sequential — Round 1 ends, then Round 2 begins. But the 24 matches *inside* a single group-stage round are scheduled across several days, so within one round different squads finish their match on different days.

The right unit to clock against is each squad's most recent match end time, not the round. Per player, a force-refresh fires iff ANY of:

1. **No cached file yet** — one-time fetch.
2. **Squad has a LIVE match right now** (`status ∈ {playing, live}`) — mid-match overlay; stats tick continuously.
3. **Squad's most recent COMPLETED match ended AFTER the cache file's mtime** — captures per-squad finalization.

We compare file `st_mtime` against match `endDate` (or `date + 2 h` as a fallback when `endDate` is missing), so a match finishing at 3 pm refreshes squads whose cache was written at 9 am the same day. On a typical mid-tournament tick this re-fetches ~50–250 players (whichever played in the last ~24 h), down from 1,488.

Implementation: `_build_nb_10.py` reads `data/raw/fifa_fantasy/*_player_stats_*.json` mtimes, walks the freshly-fetched `rounds` payload to compute `squad_recent_end_ts[squad_id]`, and sets `force_refresh=True` only for the hot subset of `fantasy_player_id`s.

---

## Cache hygiene

The raw cache (`data/raw/{source}/...`) grows ~5–20 MB per day during the tournament. Two safe rules:

1. **Never delete `data/raw/fifa/` or `data/raw/fdh/`** during the tournament — FIFA does not publish historical snapshots and we lose audit trail if the live endpoint shape changes.
2. **Safe to delete `data/raw/fifa_fantasy/player_stats_*.json` older than 7 days** — the per-round per-player data is small (~500 B) but proliferates (1488 × every refresh tick if we did force-refresh, which we don't).

A simple housekeeping cell at the top of `10` could prune outdated player_stats files; not implemented yet, low priority.

---

## Force-refresh patterns

Every cell that calls `io.cache_raw` accepts a `force_refresh` kwarg threaded through `lib/refs.py`, `lib/players.py`, and the notebooks directly. The cheap way to refresh _everything_ on a given tick:

```python
import os
os.environ["FORCE_REFRESH"] = "1"
# then run the notebook — adjust each cell to honour the env var, or set the
# config flag in cell 1 (FETCH_PER_PLAYER_FOTMOB-style).
```

The cleaner way for one notebook:

```python
# at the top of the notebook
FORCE_REFRESH = True  # was False
```

For surgical refresh (single endpoint), just delete the cached file and re-run — the `cache_raw` miss path will repopulate.
