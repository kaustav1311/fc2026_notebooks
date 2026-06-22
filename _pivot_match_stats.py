"""Pivot wc26_player_match_stats (long, 116k rows) to wide:
one row per (fifa_match_id, fifa_player_id) × 116 stat columns.

Easier to slice in Excel / SQL than the long form. Keeps a join key back to
wc26_matches + wc26_players. Re-run after each notebook 07 refresh."""
import sys, io as sio
sys.stdout = sio.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import io

long_df = io.load_table("wc26_player_match_stats")
print(f"long: {len(long_df):,} rows  |  {long_df['stat_name'].nunique()} distinct stat keys")

wide = long_df.pivot_table(
    index=["fifa_match_id", "fifa_id_ifes", "fifa_player_id"],
    columns="stat_name",
    values="value",
    aggfunc="first",
).reset_index()
wide.columns.name = None
print(f"wide: {len(wide):,} rows × {len(wide.columns)} cols")

# Re-attach a couple of useful identifiers from the match + player side for
# direct slicing without a join. Keep it surgical to not bloat the file.
matches = io.load_table("wc26_matches")[["fifa_match_id", "match_number", "stage",
                                          "kickoff_utc", "home_nation_id", "away_nation_id",
                                          "espn_match_id"]].copy()
matches["fifa_match_id"] = matches["fifa_match_id"].astype(str)
wide["fifa_match_id"] = wide["fifa_match_id"].astype(str)
wide = wide.merge(matches, on="fifa_match_id", how="left")

players = io.load_table("wc26_players")[["fifa_player_id", "nation_id", "name",
                                          "position", "real_position", "jersey_num"]].copy()
wide = wide.merge(players, on="fifa_player_id", how="left")

# Reorder: keys first, then context, then the stat columns alphabetically.
key_cols = ["fifa_match_id", "fifa_id_ifes", "espn_match_id", "match_number",
            "stage", "kickoff_utc", "home_nation_id", "away_nation_id",
            "fifa_player_id", "name", "nation_id", "position", "real_position", "jersey_num"]
stat_cols = sorted(c for c in wide.columns if c not in key_cols)
wide = wide[key_cols + stat_cols]

io.save_table(wide, "wc26_player_match_stats_wide")
print(f"\nfirst 3 cols + 5 stat cols sample:")
print(wide[key_cols[:8] + stat_cols[:5]].head(3).to_string())
