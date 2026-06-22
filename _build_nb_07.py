"""Author 07_player_match_stats.ipynb — fdh-api per match."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 07 — Per-match per-player FIFA stats

Pulls from `fdh-api.fifa.com` (FIFA's internal data hub — no auth, just a `Referer: fifa.com` header):

- `/v1/stats/match/{IdIFES}/players.json` → ~100 raw stat fields per player per match (Goals, Assists, AttemptAtGoal*, BallProgressions, DefensivePressuresApplied, Distance*, LinebreaksAttempted, etc.)
- `/v1/powerranking/match/{IdIFES}.json` → per-player attacking/defensive/creativity rank + score, within-team rank.

Outputs:
- `wc26_player_match_stats` (long format: one row per `(fifa_match_id, fifa_player_id, stat_name)` × `value`)
- `wc26_player_match_powerrank` (wide: one row per `(fifa_match_id, fifa_player_id)`)

Matches whose stats haven't been published yet (404 from fdh) are skipped. Re-run with `force_refresh=True` to pull live updates.""")

code("""import sys, json
from pathlib import Path
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import io, events
from lib.http import polite_get

matches = io.load_table("wc26_matches")
candidates = matches.dropna(subset=["fifa_id_ifes"]).copy()
candidates["fifa_id_ifes"] = candidates["fifa_id_ifes"].astype("Int64").astype(str)
print(f"matches with fifa_id_ifes: {len(candidates)}/{len(matches)}")
print(f"  finished: {(candidates['status'] == 'finished').sum()}")
""")

md("## 1. Pull `players.json` per match (raw stats)")

code("""STATS_URL = "https://fdh-api.fifa.com/v1/stats/match/{ifes}/players.json"
POWER_URL = "https://fdh-api.fifa.com/v1/powerranking/match/{ifes}.json"

stats_rows = []
power_rows = []
skipped_404 = 0

for r in candidates.itertuples():
    ifes = r.fifa_id_ifes
    fifa_match_id = r.fifa_match_id
    status = (getattr(r, "status", "") or "").lower()

    # Immutable-after-finish: FDH stats for a finished match never change.
    # If we already processed this match, force the cache path (no HTTP).
    finished_processed = (status == "finished") and events.is_processed("fdh_match_stats", str(fifa_match_id))
    fetch_kwargs = {"force_refresh": False} if finished_processed else {}

    stats_ok = power_ok = False
    # Player stats
    try:
        s = io.cache_raw(STATS_URL.format(ifes=ifes), source="fdh",
                         name=f"stats_match_{ifes}_players", sleep=0.15, **fetch_kwargs)
        stats_ok = True
        for pid, stats in s.items():
            for entry in stats:
                if not isinstance(entry, list) or len(entry) < 2:
                    continue
                name, value = entry[0], entry[1]
                stats_rows.append({
                    "fifa_match_id": fifa_match_id,
                    "fifa_id_ifes": ifes,
                    "fifa_player_id": int(pid),
                    "stat_name": name,
                    "value": value,
                })
    except Exception as e:
        if "404" in str(e):
            skipped_404 += 1
        else:
            print(f"  stats err for {ifes}: {type(e).__name__}: {e}")

    # Power ranking
    try:
        p = io.cache_raw(POWER_URL.format(ifes=ifes), source="fdh",
                         name=f"powerranking_match_{ifes}", sleep=0.15, **fetch_kwargs)
        for grp in ("outfieldPlayers", "goalkeepers"):
            for pl in p.get(grp, []):
                power_rows.append({
                    "fifa_match_id": fifa_match_id,
                    "fifa_id_ifes": ifes,
                    "fifa_player_id": pl.get("playerId"),
                    "fifa_team_id": pl.get("teamId"),
                    "player_kind": grp[:-1],  # "outfieldPlayer" / "goalkeeper"
                    "attacking_rank": pl.get("attackingRank"),
                    "defensive_rank": pl.get("defensiveRank"),
                    "creativity_rank": pl.get("creativityRank"),
                    "attacking_score": pl.get("attackingScore"),
                    "defensive_score": pl.get("defensiveScore"),
                    "creativity_score": pl.get("creativityScore"),
                    "attacking_rank_within_team": pl.get("attackingRankWithinTeam"),
                    "defensive_rank_within_team": pl.get("defensiveRankWithinTeam"),
                    "creativity_rank_within_team": pl.get("creativityRankWithinTeam"),
                    "defending_the_goal_rank": pl.get("defendingTheGoalRank"),
                    "defending_the_goal_score": pl.get("defendingTheGoalScore"),
                    "in_possession_rank": pl.get("inPossessionRank"),
                    "in_possession_score": pl.get("inPossessionScore"),
                })
        power_ok = True
    except Exception as e:
        if "404" not in str(e):
            print(f"  power err for {ifes}: {type(e).__name__}: {e}")

    # Once a finished match has both stats + powerranking landed, mark it
    # immutable so subsequent ticks skip the HTTP entirely.
    if status == "finished" and stats_ok and power_ok:
        events.mark_processed("fdh_match_stats", str(fifa_match_id))

print(f"\\nstats rows: {len(stats_rows):,}")
print(f"power rows: {len(power_rows)}")
print(f"skipped 404 (match not yet played): {skipped_404}")
""")

md("""## 2. Build dataframes + filter to the curated stat allowlist

FDH publishes ~116 stat keys per match. We retain a curated 55 covering
shooting / passing / movement / defence / discipline / GK / set-pieces — enough
for the scoring engine without bloating the wide pivot.""")

code("""# Curated 55 stat keys (shooting, ball progression, switches, distance/speed,
# pressures, distributions, set-pieces, GK, fouls, linebreaks, possession-sequence
# counts, offers/receptions, substitutions, discipline, basic outcomes).
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

stats_df = pd.DataFrame(stats_rows)
power_df = pd.DataFrame(power_rows)

if len(stats_df):
    raw_keys = stats_df["stat_name"].nunique()
    stats_df = stats_df[stats_df["stat_name"].isin(STAT_ALLOWLIST)].reset_index(drop=True)
    stats_df["fifa_match_id"] = stats_df["fifa_match_id"].astype(str)
    n_players = stats_df.groupby("fifa_match_id")["fifa_player_id"].nunique().mean()
    n_stat_keys = stats_df["stat_name"].nunique()
    print(f"raw stat keys: {raw_keys}  →  kept after allowlist: {n_stat_keys}")
    print(f"distinct matches: {stats_df['fifa_match_id'].nunique()}")
    print(f"avg players per match: {n_players:.1f}")
    missing_from_data = STAT_ALLOWLIST - set(stats_df['stat_name'].unique())
    if missing_from_data:
        print(f"  (allowlist keys not present in this run's data: {sorted(missing_from_data)})")
""")

md("## 3. Sanity + save")

code("""# Quick join sanity: every fifa_player_id should exist in wc26_players.
players_master = io.load_table("wc26_player_enrichment")
known_ids = set(players_master["fifa_player_id"].dropna().astype(int))
if len(stats_df):
    unknown = stats_df.loc[~stats_df["fifa_player_id"].isin(known_ids), "fifa_player_id"].unique()
    print(f"unknown player_ids in stats (not in wc26_players): {len(unknown)}")
if len(power_df):
    unknown_p = power_df.loc[~power_df["fifa_player_id"].isin(known_ids), "fifa_player_id"].unique()
    print(f"unknown player_ids in power: {len(unknown_p)}")

if len(stats_df):
    io.save_table(stats_df, "wc26_player_match_stats")
if len(power_df):
    io.save_table(power_df, "wc26_player_match_powerrank")

events.save()
print("event-state committed")
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("07_player_match_stats.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 07_player_match_stats.ipynb")
