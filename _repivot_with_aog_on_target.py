"""One-shot: re-parse cached FDH stat files with the expanded allow-list (adds
AttemptAtGoalOnTarget), rewrite wc26_player_match_stats + wc26_player_match_stats_wide,
then run the staging emit so all downstream parquets + sibling JSONs pick up the
new column.

This mirrors what running 07_player_match_stats.ipynb + _pivot_match_stats.py
+ _emit_staging_once.py would do in sequence — but skips the per-match HTTP cache
check (it just re-parses what's already on disk under data/raw/fdh/).
"""
import sys, warnings
sys.path.insert(0, '.')
warnings.filterwarnings('ignore')
from pathlib import Path
import pandas as pd
from lib import io

# Mirror the updated allow-list from _build_nb_07.py
STAT_ALLOWLIST = {
    "Assists", "AttemptAtGoal", "AttemptAtGoalOnTarget",
    "AttemptedBallProgressions", "AttemptedSwitchesOfPlay",
    "AvgSpeed", "CleanSheets",
    "CompletedBallProgressions", "CompletedSwitchesOfPlay",
    "Corners", "Crosses", "CrossesCompleted",
    "DefensivePressuresApplied",
    "DistanceHighSpeedSprinting", "DistanceWalking",
    "DistributionsCompletedUnderPressure", "DistributionsUnderPressure",
    "ForcedTurnovers", "FoulsAgainst", "FoulsFor", "FreeKicks",
    "GoalkeeperSaves", "Goals", "GoalsConceded", "GoalsOutsideThePenaltyArea",
    "LinebreaksAttempted", "LinebreaksAttemptedCompleted",
    "LinebreaksCompletedUnderPressure",
    "NumberOfInvolvements", "NumberOfPossessionSequences",
    "NumberOfShotEndingSequences",
    "OffersToReceiveTotal", "Offsides", "OwnGoals",
    "Passes", "PassesCompleted",
    "Penalties", "PenaltiesScored",
    "ReceivedOffersToReceive",
    "ReceptionsBetweenMidfieldAndDefensiveLine", "ReceptionsInBehind",
    "ReceptionsUnderNoPressure", "ReceptionsUnderPressure",
    "RedCards", "SpeedRuns", "Sprints",
    "SubstitutionsIn", "SubstitutionsOut",
    "TakeOnsCompleted", "TimePlayed", "TopSpeed", "TotalDistance",
    "XG", "YellowCards",
}

# Match the FIFA IdIFES → fifa_match_id mapping from wc26_matches.
matches = io.load_table("wc26_matches")
ifes_to_match = dict(zip(
    matches["fifa_id_ifes"].dropna().astype(str),
    matches["fifa_match_id"].astype(str),
))

# Walk cached FDH files; for each (date, ifes) keep only the most recent.
import json, re
fdh_dir = io.RAW / "fdh"
pat = re.compile(r"^(\d{4}-\d{2}-\d{2})_stats_match_(\d+)_players\.json$")
latest = {}
for f in fdh_dir.glob("*stats_match_*_players.json"):
    m = pat.match(f.name)
    if not m: continue
    d, ifes = m.group(1), m.group(2)
    prev = latest.get(ifes)
    if prev is None or d > prev[0]:
        latest[ifes] = (d, f)

print(f"distinct match ifes with cached FDH: {len(latest)}")

# Re-parse each into long-form rows, filtered by the new allow-list.
rows = []
n_aog_target = 0
for ifes, (d, f) in latest.items():
    payload = json.loads(f.read_text(encoding="utf-8"))
    fifa_match_id = ifes_to_match.get(ifes)
    if not fifa_match_id:
        continue
    for pid, stats in payload.items():
        if not isinstance(stats, list):
            continue
        for entry in stats:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            name, value = entry[0], entry[1]
            if name not in STAT_ALLOWLIST:
                continue
            if name == "AttemptAtGoalOnTarget":
                n_aog_target += 1
            rows.append({
                "fifa_match_id": fifa_match_id,
                "fifa_id_ifes": ifes,
                "fifa_player_id": int(pid),
                "stat_name": name,
                "value": value,
            })

long_df = pd.DataFrame(rows)
print(f"long rows: {len(long_df):,}  distinct stats: {long_df['stat_name'].nunique()}")
print(f"  AttemptAtGoalOnTarget rows: {n_aog_target}")
io.save_table(long_df, "wc26_player_match_stats")

# Pivot to wide (re-uses _pivot_match_stats.py logic).
wide = long_df.pivot_table(
    index=["fifa_match_id", "fifa_id_ifes", "fifa_player_id"],
    columns="stat_name", values="value", aggfunc="first",
).reset_index()
wide.columns.name = None

matches_ctx = matches[["fifa_match_id", "match_number", "stage", "kickoff_utc",
                       "home_nation_id", "away_nation_id", "espn_match_id"]].copy()
matches_ctx["fifa_match_id"] = matches_ctx["fifa_match_id"].astype(str)
wide["fifa_match_id"] = wide["fifa_match_id"].astype(str)
wide = wide.merge(matches_ctx, on="fifa_match_id", how="left")

players_ctx = io.load_table("wc26_players")[["fifa_player_id", "nation_id", "name",
                                              "position", "real_position", "jersey_num"]].copy()
wide = wide.merge(players_ctx, on="fifa_player_id", how="left")

key_cols = ["fifa_match_id", "fifa_id_ifes", "espn_match_id", "match_number",
            "stage", "kickoff_utc", "home_nation_id", "away_nation_id",
            "fifa_player_id", "name", "nation_id", "position", "real_position", "jersey_num"]
stat_cols = sorted(c for c in wide.columns if c not in key_cols)
wide = wide[key_cols + stat_cols]
io.save_table(wide, "wc26_player_match_stats_wide")
print(f"wide: {len(wide):,} rows × {len(wide.columns)} cols")
print(f"  AttemptAtGoalOnTarget col present: {'AttemptAtGoalOnTarget' in wide.columns}")
