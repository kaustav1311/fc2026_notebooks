"""Author 14_staging_core.ipynb."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 14 — Staging core (4 tables)

Four downstream-friendly tables, all rebuilt from already-landed parquets — no network calls.

- `wc26_stg_nations` — pass-through of `wc26_nations` (48 rows). Stable contract for the audit-app PWA so upstream rename churn never leaks downstream.
- `wc26_stg_stadiums` — `wc26_stadiums` base + per-stadium aggregates from `wc26_matches` (match days, totals, attendance) and `wc26_match_weather` (temperature min/max + means of the other weather signals).
- `wc26_stg_referee_profile` — `referee_profile` base joined to `referee_master` so each (referee, window) row carries name / country / confederation / fm_id / slug.
- `wc26_stg_fantasy_player_totals` — `fantasy_player_round_stats` aggregated by `fantasy_player_id` (no joins) into tournament-to-date counters: appearances, minutes_played, starting_xi, total_points, total_goals_scored, total_assists, clean_sheets, saves, tackles, chances_created, shots_on_target, scouting_bonus, yellow_cards.
- `wc26_stg_player_powerrank` — `wc26_player_match_powerrank` aggregated by `(fifa_player_id, fifa_team_id)` (no joins) into mean tournament scores: attacking_score, defensive_score, creativity_score, defending_the_goal_score, plus `n_matches_ranked` + `player_kind`.

Bucket: 🟥 every 3 h (hourly bundle). All inputs read from disk; rebuild runs in seconds.""")

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

nations  = io.load_table("wc26_nations")
stadiums = io.load_table("wc26_stadiums")
matches  = io.load_table("wc26_matches")
weather  = io.load_table("wc26_match_weather")
ref_master  = io.load_table("referee_master")
ref_profile = io.load_table("referee_profile")
print(f"nations: {len(nations)}  stadiums: {len(stadiums)}  matches: {len(matches)}  weather: {len(weather)}")
print(f"ref_master: {len(ref_master)}  ref_profile: {len(ref_profile)}")
""")

md("""## 1. `wc26_stg_nations`

Pass-through copy of `wc26_nations`. One row per nation (48). Exists so downstream consumers can pin to a stable schema.""")

code("""stg_nations = nations.copy()
assert stg_nations["nation_id"].is_unique, "nation_id must be unique"
print(f"wc26_stg_nations: {len(stg_nations)} rows, {len(stg_nations.columns)} cols")
io.save_table(stg_nations, "wc26_stg_nations")
""")

md("""## 2. `wc26_stg_stadiums`

Base `wc26_stadiums` (16 rows) + two per-stadium aggregates:

- From `wc26_matches` grouped by `stadium_id`: `match_days`, `matches_total`, `matches_completed`, `total_attendance_so_far`.
- From `wc26_match_weather` grouped by `stadium_id`: temperature min/max in both raw + apparent, plus means of humidity / dew point / precipitation / rain / wind / cloud cover.

`fifa_attendance` is a comma-separated string in `wc26_matches`; we parse via `pd.to_numeric` (NaN-safe).""")

code("""# Per-stadium match aggregates from wc26_matches.
m = matches.copy()
m["fifa_attendance_num"] = pd.to_numeric(
    m["fifa_attendance"].astype(str).str.replace(",", "", regex=False),
    errors="coerce",
)
m["is_finished"] = (m["status"] == "finished").astype(int)

match_agg = (
    m.dropna(subset=["stadium_id"])
     .groupby("stadium_id", dropna=False)
     .agg(
         match_days=("date_et", "nunique"),
         matches_total=("match_number", "count"),
         matches_completed=("is_finished", "sum"),
         total_attendance_so_far=("fifa_attendance_num", "sum"),
     )
     .reset_index()
)
print(f"match_agg: {len(match_agg)} stadiums with matches")
match_agg.head()
""")

code("""# Per-stadium weather aggregates from wc26_match_weather.
w = weather.dropna(subset=["stadium_id"]).copy()
weather_agg = (
    w.groupby("stadium_id", dropna=False)
     .agg(
         max_temperature_c=("temperature_c", "max"),
         min_temperature_c=("temperature_c", "min"),
         max_apparent_temperature_c=("apparent_temperature_c", "max"),
         min_apparent_temperature_c=("apparent_temperature_c", "min"),
         avg_humidity_pct=("humidity_pct", "mean"),
         avg_dew_point_c=("dew_point_c", "mean"),
         avg_precipitation_mm=("precipitation_mm", "mean"),
         avg_rain_mm=("rain_mm", "mean"),
         avg_wind_speed_kmh=("wind_speed_kmh", "mean"),
         avg_cloud_cover_pct=("cloud_cover_pct", "mean"),
     )
     .reset_index()
)
print(f"weather_agg: {len(weather_agg)} stadiums with weather rows")
weather_agg.head()
""")

code("""# Left-join both aggregates onto the stadiums base.
stg_stadiums = (
    stadiums
        .merge(match_agg,   on="stadium_id", how="left")
        .merge(weather_agg, on="stadium_id", how="left")
)

# Stadiums without matches (none today, but tournament-window safe) → counters to 0.
for col in ["match_days", "matches_total", "matches_completed", "total_attendance_so_far"]:
    stg_stadiums[col] = stg_stadiums[col].fillna(0).astype("Int64")

assert stg_stadiums["stadium_id"].is_unique, "stadium_id must be unique"
assert len(stg_stadiums) == 16, f"expected 16 stadiums, got {len(stg_stadiums)}"
print(f"wc26_stg_stadiums: {len(stg_stadiums)} rows, {len(stg_stadiums.columns)} cols")
print(f"  total_attendance_so_far across all venues: {int(stg_stadiums['total_attendance_so_far'].sum()):,}")
io.save_table(stg_stadiums, "wc26_stg_stadiums")
""")

md("""## 3. `wc26_stg_referee_profile`

Base: `referee_profile` (long; one row per `(referee_id, window)`, ~250 rows today).
Left-join `referee_master` on `referee_id` to carry the ref's identity columns.
Drop the join key from the right side before merging so we don't get `_x`/`_y` suffixes.""")

code("""master_cols = ["referee_id", "name", "country", "confederation", "flag_iso",
                "nation_id", "fm_id", "slug", "fm_url", "countryApid"]
right = ref_master[master_cols]

stg_referee_profile = ref_profile.merge(right, on="referee_id", how="left")

# (referee_id, window) is the natural key.
assert not stg_referee_profile.duplicated(subset=["referee_id", "window"]).any(), \\
    "duplicate (referee_id, window) rows in stg_referee_profile"

n_refs = stg_referee_profile["referee_id"].nunique()
print(f"wc26_stg_referee_profile: {len(stg_referee_profile)} rows, {n_refs} refs, windows={sorted(stg_referee_profile['window'].dropna().unique().tolist())}")
io.save_table(stg_referee_profile, "wc26_stg_referee_profile")
""")

md("""## 4. `wc26_stg_fantasy_player_totals`

One row per `fantasy_player_id` — tournament-to-date totals across every round they've played. Pure aggregation of `fantasy_player_round_stats` (no joins). Lets the PWA show a single-number scoreboard per fantasy player without scanning the long table.""")

code("""prs = io.load_table("fantasy_player_round_stats")
print(f"fantasy_player_round_stats: {len(prs)} rows, {prs['fantasy_player_id'].nunique()} players")

stg_fpt = (prs.groupby("fantasy_player_id", dropna=True)
              .agg(
                  appearances=("round_id", "count"),
                  minutes_played=("minutes_played", "sum"),
                  starting_xi=("starting_xi", "sum"),
                  total_points=("points", "sum"),
                  total_goals_scored=("goals_scored", "sum"),
                  total_assists=("assists", "sum"),
                  clean_sheets=("clean_sheet", "sum"),
                  saves=("saves", "sum"),
                  tackles=("tackles", "sum"),
                  chances_created=("chances_created", "sum"),
                  shots_on_target=("shots_on_target", "sum"),
                  scouting_bonus=("scouting_bonus", "sum"),
                  yellow_cards=("yellow_cards", "sum"),
              )
              .reset_index())

assert stg_fpt["fantasy_player_id"].is_unique, "fantasy_player_id must be unique"
print(f"wc26_stg_fantasy_player_totals: {len(stg_fpt)} rows, {len(stg_fpt.columns)} cols")
io.save_table(stg_fpt, "wc26_stg_fantasy_player_totals")
""")

md("""## 5. `wc26_stg_player_powerrank`

One row per `(fifa_player_id, fifa_team_id)` — average FDH power-ranking scores across every WC match the player has been ranked in. Pure aggregation of `wc26_player_match_powerrank` (no joins).

Four per-match-average scores: `avg_attacking_score`, `avg_defensive_score`, `avg_creativity_score`, `avg_defending_the_goal_score`. The first three are universal; the fourth is goalkeeper-only (NaN for outfielders). Plus context: `n_matches_ranked` (the number of matches the player was ranked in) and `player_kind`.""")

code("""pr = io.load_table("wc26_player_match_powerrank")
print(f"wc26_player_match_powerrank: {len(pr)} rows, {pr.groupby(['fifa_player_id','fifa_team_id']).ngroups} (player, team) pairs")

stg_pr = (pr.groupby(["fifa_player_id", "fifa_team_id"], dropna=False)
            .agg(
                avg_attacking_score=("attacking_score", "mean"),
                avg_defensive_score=("defensive_score", "mean"),
                avg_creativity_score=("creativity_score", "mean"),
                avg_defending_the_goal_score=("defending_the_goal_score", "mean"),
                n_matches_ranked=("fifa_match_id", "count"),
                player_kind=("player_kind", "first"),
            )
            .reset_index())

assert not stg_pr.duplicated(subset=["fifa_player_id", "fifa_team_id"]).any(), \\
    "duplicate (fifa_player_id, fifa_team_id) rows"
print(f"wc26_stg_player_powerrank: {len(stg_pr)} rows, {len(stg_pr.columns)} cols")
io.save_table(stg_pr, "wc26_stg_player_powerrank")

# JSON emit to sibling audit-app repo (matches wc26_stg_players_view pattern).
SIBLING_JSON = Path("E:/fifawc2026/public/data/wc26_stg_player_powerrank.json")
if SIBLING_JSON.parent.exists():
    stg_pr.to_json(SIBLING_JSON, orient="records", date_format="iso", indent=None)
    print(f"wrote {SIBLING_JSON}")
else:
    print(f"WARN: sibling data dir not found ({SIBLING_JSON.parent}) — skipping JSON emit")
""")

md("## 6. Done")

code("""print("staging core: 5 tables written.")
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("14_staging_core.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 14_staging_core.ipynb")
