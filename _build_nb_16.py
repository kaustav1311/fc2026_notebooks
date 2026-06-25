"""Author 16_staging_players.ipynb."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 16 — `wc26_stg_players`

One wide row per player (~1,248), keyed by `fifa_player_id`. Pure pandas — every input is an already-landed parquet.

Structure:

- **Base**: `wc26_player_enrichment` (1248 rows).
- **Block A — summary joins** (all one-to-one on `fifa_player_id`, pre-aggregated upstream):
  - `wc26_player_career_club_summary` → `club_*` prefix on the youth/senior/num cols; `current_club_*`, `all_clubs`, `num_total_clubs` stay bare.
  - `wc26_player_career_national_summary` → `national_*` prefix on all youth/senior/num cols.
  - `wc26_player_market_value_summary` → `value_*` prefix on every column.
  - `wc26_player_fotmob_wc` → `fotmob_wc_*` prefix on the 19 WC-tournament stats.
- **Block B — WC tournament aggregate** from `wc26_player_match_stats_wide` (1,644 rows → 1 per player):
  - 50 SUM cols, 2 Avg cols (`AvgSpeed`, `XG`), 1 MAX col (`TopSpeed`), all prefixed `fifa_wc_`.
  - 3 array cols (`stages_played`, `opponents`, `match_ids`) — sorted unique lists per player.
- **Block C — form windows** from `wc26_player_recent_matches_fotmob` (long, up to 20 newest per player):
  - For each window N ∈ (5, 10, 15, 20): 10 cols prefixed `recent{N}_` (matches_played, minutes_played, goals, assists, yellow_cards, red_cards, fotmob_rating, player_of_the_match, started_pct, has_data sentinel).

Output: `wc26_stg_players`. Refresh bucket: 🟥 every 3 h.""")

code("""import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import io

enrichment   = io.load_table("wc26_player_enrichment")
career_club  = io.load_table("wc26_player_career_club_summary")
career_natl  = io.load_table("wc26_player_career_national_summary")
market_value = io.load_table("wc26_player_market_value_summary")
fotmob_wc    = io.load_table("wc26_player_fotmob_wc")
match_wide   = io.load_table("wc26_player_match_stats_wide")
recent_form  = io.load_table("wc26_player_recent_matches_fotmob")

print(f"enrichment: {len(enrichment)}  career_club: {len(career_club)}  career_natl: {len(career_natl)}")
print(f"market_value: {len(market_value)}  fotmob_wc: {len(fotmob_wc)}")
print(f"match_wide: {len(match_wide)}  recent_form: {len(recent_form)}")
""")

md("""## Block A — summary joins (one-to-one on `fifa_player_id`)

Drop the join key from the right side before merging so we don't generate `_x`/`_y` suffixes. Apply prefix renames per the design spec.""")

code("""# 1. Career club summary — per New-Task-Design Player sheet: keep only the
# senior_* family (drop youth_*). Identity cols stay unprefixed.
CLUB_KEEP_SENIOR = {
    "senior_appearances", "senior_goals", "senior_assists",
    "senior_weighted_avg_rating", "senior_active_seasons",
    "senior_num_seasons", "senior_num_teams",
}
club_unprefixed = {"fifa_player_id", "current_club_fotmob_id", "current_club_name", "all_clubs", "num_total_clubs"}
club_cols_in = ["fifa_player_id"] + [c for c in career_club.columns
                                      if c in CLUB_KEEP_SENIOR or c in club_unprefixed and c != "fifa_player_id"]
club = career_club[[c for c in club_cols_in if c in career_club.columns]].copy()
club_renames = {c: f"club_{c}" for c in club.columns if c not in club_unprefixed}
club = club.rename(columns=club_renames)

# 2. Career national summary — prefix all non-key cols with national_.
natl_renames = {c: f"national_{c}" for c in career_natl.columns if c != "fifa_player_id"}
natl = career_natl.rename(columns=natl_renames)

# 3. Market value summary — per design Player sheet: keep only 4 cols
# (fotmob_latest_eur, fotmob_peak_eur, fotmob_peak_date, tm_latest_eur).
VALUE_KEEP = ["fotmob_latest_eur", "fotmob_peak_eur", "fotmob_peak_date", "tm_latest_eur"]
val_cols = ["fifa_player_id"] + [c for c in VALUE_KEEP if c in market_value.columns]
val = market_value[val_cols].copy()
val = val.rename(columns={c: f"value_{c}" for c in val.columns if c != "fifa_player_id"})

# 4. FotMob WC — drop the cols that collide with enrichment (fotmob_player_id, season_name,
#    fotmob_tournament_id) before prefixing. Then prefix the 19 stat cols.
fotmob_wc_trim = fotmob_wc.drop(columns=["fotmob_player_id", "season_name", "fotmob_tournament_id"], errors="ignore")
fwc_renames = {c: f"fotmob_wc_{c}" for c in fotmob_wc_trim.columns if c != "fifa_player_id"}
fwc = fotmob_wc_trim.rename(columns=fwc_renames)

# Cascade left-joins onto enrichment.
stg = (
    enrichment
        .merge(club, on="fifa_player_id", how="left")
        .merge(natl, on="fifa_player_id", how="left")
        .merge(val,  on="fifa_player_id", how="left")
        .merge(fwc,  on="fifa_player_id", how="left")
)
print(f"after Block A: {len(stg)} rows, {len(stg.columns)} cols")
""")

md("""## Block B — WC tournament aggregate from `wc26_player_match_stats_wide`

Group by `fifa_player_id`, aggregate the 53 spec'd stat columns (50 SUM, 2 Avg, 1 MAX), then prefix with `fifa_wc_`. Also build three sorted-list array cols (`stages_played`, `opponents`, `match_ids`). The opponent of a row is the *other* team — derived from whether the player's `nation_id` matches `home_nation_id` or `away_nation_id`.

Aggregation happens BEFORE the merge so the player row count cannot inflate, even if a player has multiple rows per match (sub + start) in some edge case.""")

code("""SUM_COLS = [
    "Assists", "AttemptAtGoal", "AttemptAtGoalOnTarget",
    "AttemptedBallProgressions", "AttemptedSwitchesOfPlay",
    "CleanSheets", "CompletedBallProgressions", "CompletedSwitchesOfPlay", "Corners",
    "Crosses", "CrossesCompleted", "DefensivePressuresApplied", "DistanceHighSpeedSprinting",
    "DistanceWalking", "DistributionsCompletedUnderPressure", "DistributionsUnderPressure",
    "ForcedTurnovers", "FoulsAgainst", "FoulsFor", "FreeKicks", "GoalkeeperSaves",
    "Goals", "GoalsConceded", "GoalsOutsideThePenaltyArea", "LinebreaksAttempted",
    "LinebreaksAttemptedCompleted", "LinebreaksCompletedUnderPressure", "NumberOfInvolvements",
    "NumberOfPossessionSequences", "NumberOfShotEndingSequences", "OffersToReceiveTotal",
    "Offsides", "OwnGoals", "Passes", "PassesCompleted", "Penalties", "PenaltiesScored",
    "ReceivedOffersToReceive", "ReceptionsBetweenMidfieldAndDefensiveLine", "ReceptionsInBehind",
    "ReceptionsUnderNoPressure", "ReceptionsUnderPressure", "RedCards", "SpeedRuns", "Sprints",
    "SubstitutionsIn", "SubstitutionsOut", "TakeOnsCompleted", "TimePlayed", "TotalDistance",
    "YellowCards",
]
AVG_COLS = ["AvgSpeed", "XG"]
MAX_COLS = ["TopSpeed"]

# Only aggregate cols that actually exist in this parquet (defensive — schema may evolve).
present = set(match_wide.columns)
sum_present = [c for c in SUM_COLS if c in present]
avg_present = [c for c in AVG_COLS if c in present]
max_present = [c for c in MAX_COLS if c in present]
missing = [c for c in SUM_COLS + AVG_COLS + MAX_COLS if c not in present]
if missing:
    print(f"  WARN: spec'd stat cols not found in wc26_player_match_stats_wide: {missing}")

agg_spec = {**{c: "sum" for c in sum_present},
            **{c: "mean" for c in avg_present},
            **{c: "max"  for c in max_present}}

stats_agg = match_wide.groupby("fifa_player_id", dropna=True).agg(agg_spec).reset_index()
# Prefix every aggregated stat col with fifa_wc_.
stats_agg = stats_agg.rename(columns={c: f"fifa_wc_{c}" for c in agg_spec.keys()})

# Per design Player sheet: add fifa_wc_n_matches — count of distinct WC matches
# the player has a row for (any of fifa_match_id / fifa_id_ifes / match_number).
n_matches = match_wide.groupby("fifa_player_id").agg(
    fifa_wc_n_matches=("fifa_match_id", "nunique"),
).reset_index()
stats_agg = stats_agg.merge(n_matches, on="fifa_player_id", how="left")
print(f"stats_agg: {len(stats_agg)} players, {len(stats_agg.columns)-1} stat cols (incl. fifa_wc_n_matches)")
""")

code("""# Derive opponent per row (the other team relative to the player's nation_id).
mw = match_wide.copy()
mw["_opponent"] = np.where(
    mw["nation_id"] == mw["home_nation_id"],
    mw["away_nation_id"],
    mw["home_nation_id"],
)

def _sorted_uniques(s):
    return sorted({v for v in s.dropna().tolist() if v is not None and not (isinstance(v, float) and np.isnan(v))})

arrays_agg = mw.groupby("fifa_player_id", dropna=True).agg(
    stages_played=("stage",           _sorted_uniques),
    opponents=("_opponent",           _sorted_uniques),
    match_ids=("fifa_match_id",       _sorted_uniques),
).reset_index()
print(f"arrays_agg: {len(arrays_agg)} players")

# Merge Block B onto the staging frame.
stg = stg.merge(stats_agg,  on="fifa_player_id", how="left")
stg = stg.merge(arrays_agg, on="fifa_player_id", how="left")

# Players with no WC matches yet → empty lists (parquet-safe) rather than NaN.
for col in ("stages_played", "opponents", "match_ids"):
    stg[col] = stg[col].apply(lambda v: v if isinstance(v, list) else [])

# Authoritative WC appearance count: prefer fifa_wc_n_matches (count of distinct
# fifa_match_ids the player has stats rows for, from fdh-api per-match data)
# over fotmob_wc_appearances (FotMob's tournamentStats + matchDetails roll-up
# which lags 1-2 days and silently drops ~400 players whose FotMob match block
# doesn't parse). The PWA's PlayerDetailSheet reads fotmob_wc_appearances; this
# override ensures it tracks live as matches finish. Verified 2026-06-25 — was
# undercounting 582/846 players before the fix.
if "fifa_wc_n_matches" in stg.columns:
    stg["fotmob_wc_appearances"] = stg["fifa_wc_n_matches"].fillna(stg["fotmob_wc_appearances"])

print(f"after Block B: {len(stg)} rows, {len(stg.columns)} cols")
""")

md("""## Block C — form windows from `wc26_player_recent_matches_fotmob`

For each `N ∈ (5, 10, 15, 20)`: take the player's N newest matches by `match_date_utc`, then compute 10 stats prefixed `recent{N}_`. The `fotmob_rating` aggregation filters out zero-rating rows *inside* the groupby (FotMob writes 0 for unrated/very-short subs). `recent{N}_started_pct` is guarded against zero-division. `recent{N}_has_data` is a boolean sentinel that downstream EV uses to gate the rating signal.""")

code("""recent = recent_form.copy()
recent["_match_date"] = pd.to_datetime(recent["match_date_utc"], utc=True, errors="coerce")
# Sort newest-first per player so head(N) takes the most recent N matches.
recent = recent.sort_values(["fifa_player_id", "_match_date"], ascending=[True, False])

WINDOWS = (5, 10, 15, 20)

def build_window(df, n):
    top = df.groupby("fifa_player_id", group_keys=False, sort=False).head(n)
    g = top.groupby("fifa_player_id", sort=False)

    def _to_list(s):
        return [v for v in s.dropna().tolist()]

    block = g.agg(
        matches_played=("match_id_fotmob", "count"),
        minutes_played=("minutes_played", "sum"),
        goals=("goals", "sum"),
        assists=("assists", "sum"),
        yellow_cards=("yellow_cards", "sum"),
        red_cards=("red_cards", "sum"),
        player_of_the_match=("player_of_the_match", "sum"),
        on_bench_sum=("on_bench", "sum"),
        # Per design: per-window array cols (newest-first since df is pre-sorted).
        match_id_fotmob=("match_id_fotmob", _to_list),
        team_id=("team_id", _to_list),
        team_name=("team_name", _to_list),
        opponent_team_id=("opponent_team_id", _to_list),
        opponent_team_name=("opponent_team_name", _to_list),
    )
    # fotmob_rating: mean of rating where rating > 0 (filter inside the groupby).
    rating_means = g.apply(lambda gr: gr.loc[gr["fotmob_rating"] > 0, "fotmob_rating"].mean())
    block["fotmob_rating"] = rating_means
    # started_pct guarded against div-by-zero.
    block["started_pct"] = np.where(
        block["matches_played"] > 0,
        1 - (block["on_bench_sum"] / block["matches_played"]),
        np.nan,
    )
    block["has_data"] = (block["matches_played"] > 0)
    block = block.drop(columns=["on_bench_sum"])
    block = block.rename(columns={c: f"recent{n}_{c}" for c in block.columns})
    return block.reset_index()

form_blocks = [build_window(recent, n) for n in WINDOWS]
for n, blk in zip(WINDOWS, form_blocks):
    print(f"  recent{n}: {len(blk)} players, {len(blk.columns)-1} cols")

# Merge each form block onto the staging frame.
for blk in form_blocks:
    stg = stg.merge(blk, on="fifa_player_id", how="left")

# Players with zero rows in recent_form → has_data=False, counters=NaN; coerce to clean zeros.
for n in WINDOWS:
    stg[f"recent{n}_has_data"] = stg[f"recent{n}_has_data"].fillna(False).astype(bool)
    for c in ("matches_played", "minutes_played", "goals", "assists",
              "yellow_cards", "red_cards", "player_of_the_match"):
        col = f"recent{n}_{c}"
        stg[col] = stg[col].fillna(0)
    # Array cols default to empty list per the parquet contract.
    for c in ("match_id_fotmob", "team_id", "team_name",
              "opponent_team_id", "opponent_team_name"):
        col = f"recent{n}_{c}"
        if col in stg.columns:
            stg[col] = stg[col].apply(lambda v: v if isinstance(v, list) else [])

print(f"after Block C: {len(stg)} rows, {len(stg.columns)} cols")
""")

md("## Sanity checks + save")

code("""# Key uniqueness.
assert stg["fifa_player_id"].is_unique, "fifa_player_id must be unique"

# Form-window monotonicity: every player's recent20_matches_played >= recent15 >= recent10 >= recent5.
for hi, lo in [(20, 15), (15, 10), (10, 5)]:
    bad = stg[stg[f"recent{hi}_matches_played"] < stg[f"recent{lo}_matches_played"]]
    assert len(bad) == 0, f"window monotonicity broken at recent{hi} < recent{lo}: {len(bad)} rows"

# Caps on window sizes.
for n in (5, 10, 15, 20):
    mx = int(stg[f"recent{n}_matches_played"].max())
    assert mx <= n, f"recent{n}_matches_played max = {mx} (> {n})"

n_with_form = int(stg["recent20_has_data"].sum())
n_with_wc_match = int(stg["fifa_wc_TimePlayed"].notna().sum()) if "fifa_wc_TimePlayed" in stg.columns else 0
print(f"\\nwc26_stg_players: {len(stg)} rows, {len(stg.columns)} cols")
print(f"  with any recent-form data:  {n_with_form}/{len(stg)}")
print(f"  with any WC match minutes:  {n_with_wc_match}/{len(stg)}")

io.save_table(stg, "wc26_stg_players")
""")

md("""## Block D — `wc26_stg_players_view` (slim curated view + derived ratios)

Hand-picked subset of the wide `wc26_stg_players` table (~115 cols). Drops the youth career blocks, obscure FIFA-WC stats, and the recent20 window. Adds 13 derived completion/ratio percentages — reception breakdown, pass completion, ball-progression completion, switches/cross/distributions completion, line-breaks proportions, %distance walking and %distance high-speed sprinting — plus a combined `fifa_wc_TotalCards` = yellow + red.

The wide `wc26_stg_players` stays as the authoritative source; this view is the consumer surface the PWA + EV scorer should read.""")

code("""SLIM_COLS = [
    # Identity / bio
    "nation_id", "fifa_player_id", "name", "short_name", "birth_date",
    "jersey_num", "height_cm", "weight_kg", "position", "preferred_foot",
    "picture_url", "fotmob_player_id", "fotmob_name", "club_fotmob_id",
    "club_name", "position_ids_desc", "wc_rating",
    "tm_player_id", "tm_slug", "club_tm_id", "club_name_tm", "contract_end",
    # Club senior career
    "club_senior_appearances", "club_senior_goals", "club_senior_assists",
    "club_senior_weighted_avg_rating", "club_senior_num_seasons",
    "current_club_name", "current_club_fotmob_id", "all_clubs", "num_total_clubs",
    # National senior career
    "national_senior_appearances", "national_senior_goals", "national_senior_assists",
    "national_senior_weighted_avg_rating", "national_senior_num_seasons",
    # Market value
    "value_fotmob_latest_eur", "value_fotmob_latest_date",
    "value_fotmob_peak_eur", "value_fotmob_peak_date",
    "value_tm_latest_eur", "value_tm_latest_date",
    "value_tm_peak_eur", "value_tm_peak_date",
    # FotMob WC tournament stats
    "fotmob_wc_appearances", "fotmob_wc_fotmob_rating",
    "fotmob_wc_chances_created", "fotmob_wc_big_chances_created",
    "fotmob_wc_dribbles", "fotmob_wc_successful_dribbles_pct",
    "fotmob_wc_duels_won", "fotmob_wc_duels_won_pct",
    "fotmob_wc_touches", "fotmob_wc_touches_opp_box",
    "fotmob_wc_defensive_contributions", "fotmob_wc_tackles",
    "fotmob_wc_xg_against_on_pitch",
    # FIFA WC tournament stats (curated subset of the 53 from the wide table)
    "fifa_wc_Goals", "fifa_wc_Assists", "fifa_wc_CleanSheets",
    "fifa_wc_GoalkeeperSaves", "fifa_wc_GoalsConceded",
    "fifa_wc_GoalsOutsideThePenaltyArea", "fifa_wc_TimePlayed",
    "fifa_wc_ReceptionsBetweenMidfieldAndDefensiveLine", "fifa_wc_ReceptionsInBehind",
    "fifa_wc_ReceptionsUnderNoPressure", "fifa_wc_ReceptionsUnderPressure",
    "fifa_wc_ReceivedOffersToReceive", "fifa_wc_OffersToReceiveTotal",
    "fifa_wc_PassesCompleted", "fifa_wc_Passes",
    "fifa_wc_CompletedBallProgressions", "fifa_wc_AttemptedBallProgressions",
    "fifa_wc_CompletedSwitchesOfPlay", "fifa_wc_AttemptedSwitchesOfPlay",
    "fifa_wc_CrossesCompleted", "fifa_wc_Crosses",
    "fifa_wc_DistributionsCompletedUnderPressure", "fifa_wc_DistributionsUnderPressure",
    "fifa_wc_LinebreaksCompletedUnderPressure", "fifa_wc_LinebreaksAttemptedCompleted",
    "fifa_wc_LinebreaksAttempted",
    "fifa_wc_DistanceWalking", "fifa_wc_DistanceHighSpeedSprinting", "fifa_wc_TotalDistance",
    "fifa_wc_AttemptAtGoal", "fifa_wc_AttemptAtGoalOnTarget",
    "fifa_wc_RedCards", "fifa_wc_YellowCards",
    "fifa_wc_AvgSpeed", "fifa_wc_TopSpeed", "fifa_wc_XG",
    "fifa_wc_Corners", "fifa_wc_FoulsAgainst", "fifa_wc_DefensivePressuresApplied",
    "fifa_wc_SpeedRuns", "fifa_wc_Sprints", "fifa_wc_FoulsFor", "fifa_wc_OwnGoals",
    "fifa_wc_NumberOfInvolvements", "fifa_wc_ForcedTurnovers", "fifa_wc_Offsides",
    "fifa_wc_FreeKicks",
    "fifa_wc_NumberOfPossessionSequences", "fifa_wc_NumberOfShotEndingSequences",
    "fifa_wc_Penalties", "fifa_wc_PenaltiesScored", "fifa_wc_TakeOnsCompleted",
    "fifa_wc_n_matches",
    # Arrays
    "stages_played", "opponents", "match_ids",
    # Recent windows 5/10/15 (drop 20)
    "recent5_minutes_played", "recent5_goals", "recent5_assists",
    "recent5_yellow_cards", "recent5_red_cards", "recent5_player_of_the_match",
    "recent5_fotmob_rating", "recent5_has_data", "recent5_started_pct",
    "recent5_match_id_fotmob", "recent5_team_id", "recent5_team_name",
    "recent5_opponent_team_id", "recent5_opponent_team_name",
    "recent10_minutes_played", "recent10_goals", "recent10_assists",
    "recent10_yellow_cards", "recent10_red_cards", "recent10_player_of_the_match",
    "recent10_fotmob_rating", "recent10_has_data", "recent10_started_pct",
    "recent10_match_id_fotmob", "recent10_team_id", "recent10_team_name",
    "recent10_opponent_team_id", "recent10_opponent_team_name",
    "recent15_minutes_played", "recent15_goals", "recent15_assists",
    "recent15_yellow_cards", "recent15_red_cards", "recent15_player_of_the_match",
    "recent15_fotmob_rating", "recent15_started_pct", "recent15_has_data",
    "recent15_match_id_fotmob", "recent15_team_id", "recent15_team_name",
    "recent15_opponent_team_id", "recent15_opponent_team_name",
]

present = [c for c in SLIM_COLS if c in stg.columns]
missing = [c for c in SLIM_COLS if c not in stg.columns]
if missing:
    print(f"WARN: spec'd cols missing from wc26_stg_players ({len(missing)}): {missing[:8]}{'...' if len(missing)>8 else ''}")

slim = stg[present].copy()

def _div(num_col, den_col):
    \"\"\"Safe divide: NaN where denominator is 0 or missing.\"\"\"
    if num_col not in slim.columns or den_col not in slim.columns:
        return pd.Series([np.nan]*len(slim), index=slim.index)
    n = slim[num_col]; d = slim[den_col]
    return np.where(pd.notna(d) & (d != 0) & pd.notna(n), n / d, np.nan)

# === Derived ratios (per screenshot) ===
slim["fifa_wc_mid_def_reception_pct"]                       = _div("fifa_wc_ReceptionsBetweenMidfieldAndDefensiveLine", "fifa_wc_ReceivedOffersToReceive")
slim["fifa_wc_attacking_reception_pct"]                     = _div("fifa_wc_ReceptionsInBehind", "fifa_wc_ReceivedOffersToReceive")
slim["fifa_wc_under_vs_no_pressure_reception_ratio"]        = _div("fifa_wc_ReceptionsUnderPressure", "fifa_wc_ReceptionsUnderNoPressure")
slim["fifa_wc_reception_completion_pct"]                    = _div("fifa_wc_ReceivedOffersToReceive", "fifa_wc_OffersToReceiveTotal")
slim["fifa_wc_pass_completion_pct"]                         = _div("fifa_wc_PassesCompleted", "fifa_wc_Passes")
slim["fifa_wc_ball_progression_completion_pct"]             = _div("fifa_wc_CompletedBallProgressions", "fifa_wc_AttemptedBallProgressions")
slim["fifa_wc_switches_of_play_completion_pct"]             = _div("fifa_wc_CompletedSwitchesOfPlay", "fifa_wc_AttemptedSwitchesOfPlay")
slim["fifa_wc_cross_completion_pct"]                        = _div("fifa_wc_CrossesCompleted", "fifa_wc_Crosses")
slim["fifa_wc_distributions_under_pressure_completion_pct"] = _div("fifa_wc_DistributionsCompletedUnderPressure", "fifa_wc_DistributionsUnderPressure")
slim["fifa_wc_linebreaks_under_pressure_proportion_pct"]    = _div("fifa_wc_LinebreaksCompletedUnderPressure", "fifa_wc_LinebreaksAttemptedCompleted")
slim["fifa_wc_linebreaks_completion_pct"]                   = _div("fifa_wc_LinebreaksAttemptedCompleted", "fifa_wc_LinebreaksAttempted")
slim["fifa_wc_pct_distance_walking"]                        = _div("fifa_wc_DistanceWalking", "fifa_wc_TotalDistance")
slim["fifa_wc_pct_distance_high_speed_sprinting"]           = _div("fifa_wc_DistanceHighSpeedSprinting", "fifa_wc_TotalDistance")

# Combined cards
yc = slim["fifa_wc_YellowCards"].fillna(0) if "fifa_wc_YellowCards" in slim.columns else 0
rc = slim["fifa_wc_RedCards"].fillna(0)    if "fifa_wc_RedCards"    in slim.columns else 0
slim["fifa_wc_TotalCards"] = yc + rc

assert slim["fifa_player_id"].is_unique, "fifa_player_id must be unique in slim view"
print(f"wc26_stg_players_view: {len(slim)} rows, {len(slim.columns)} cols  (picked {len(present)}/{len(SLIM_COLS)} spec'd + {len(slim.columns)-len(present)} derived)")
io.save_table(slim, "wc26_stg_players_view")

# Also emit JSON to the sibling audit-app repo (E:/fifawc2026/public/data/).
# That repo's loaders consume orient='records' list-of-dicts (same as the other
# wc26_stg_*.json files already in that directory).
SIBLING_JSON = Path("E:/fifawc2026/public/data/wc26_stg_players_view.json")
if SIBLING_JSON.parent.exists():
    slim.to_json(SIBLING_JSON, orient="records", date_format="iso", indent=None)
    print(f"wrote {SIBLING_JSON}")
else:
    print(f"WARN: sibling data dir not found ({SIBLING_JSON.parent}) — skipping JSON emit")
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("16_staging_players.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 16_staging_players.ipynb")
