"""Build wc26_stg_team_match_metrics.parquet — per-team per-match aggregation
of wc26_player_match_stats_wide. One row per (fifa_match_id, nation_id),
summing the 54 player-level stat columns into team totals.

Feeds §I.2 (nation tournament metrics, per-match normalized) of the
recommender. Also useful for a future PWA Team Stats tab.

Aggregation choice: SUM for every numeric stat column. Downstream consumers
derive ratios (e.g. pass_completion_pct = sum(PassesCompleted) / sum(Passes))
— consistent with how wc26_stg_players_view already handles its derived
percentages.

A handful of stats are conceptually per-player rates (AvgSpeed, TopSpeed)
that don't compose by sum — left as nullable mean/max columns with the
`team_max_` / `team_mean_` prefix so consumers don't accidentally treat them
as totals.

Run after notebook 07. Idempotent.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "data" / "processed" / "wc26_player_match_stats_wide.parquet"
DST = ROOT / "data" / "processed" / "wc26_stg_team_match_metrics.parquet"

# Identity columns carried through from the player rows (one value per
# (match, team) — first() works since they're constant within the group).
IDENTITY_COLS = [
    "fifa_match_id",
    "fifa_id_ifes",
    "espn_match_id",
    "match_number",
    "stage",
    "kickoff_utc",
    "home_nation_id",
    "away_nation_id",
]

# Group key — nation_id is the 3-letter FIFA code, present on every player row.
GROUP_KEY = ["fifa_match_id", "nation_id"]

# Per-player rate stats that should NOT be summed. AvgSpeed -> team mean,
# TopSpeed -> team max. Renamed in output so downstream consumers don't
# mistake them for sums.
RATE_STATS_MEAN = {"AvgSpeed"}
RATE_STATS_MAX = {"TopSpeed"}


def build() -> pd.DataFrame:
    w = pd.read_parquet(SRC)
    print(f"  source: {len(w)} player-match rows, {len(w.columns)} cols")

    # Identify stat columns: numeric, not in identity, not player-identifier.
    excluded = set(IDENTITY_COLS) | {"fifa_player_id", "name", "nation_id",
                                      "position", "real_position", "jersey_num"}
    stat_cols = [
        c for c in w.columns
        if c not in excluded and pd.api.types.is_numeric_dtype(w[c])
    ]
    sum_cols = [c for c in stat_cols if c not in RATE_STATS_MEAN | RATE_STATS_MAX]
    print(f"  {len(sum_cols)} stats to sum, "
          f"{len(RATE_STATS_MEAN & set(stat_cols))} to team-mean, "
          f"{len(RATE_STATS_MAX & set(stat_cols))} to team-max")

    # Build aggregation dict
    agg_dict: dict[str, str] = {c: "sum" for c in sum_cols}
    for c in RATE_STATS_MEAN & set(stat_cols):
        agg_dict[c] = "mean"
    for c in RATE_STATS_MAX & set(stat_cols):
        agg_dict[c] = "max"
    for c in IDENTITY_COLS:
        agg_dict[c] = "first"

    out = w.groupby(GROUP_KEY, as_index=False, dropna=False).agg(agg_dict)

    # Rename rate columns so consumers can tell sum from mean/max apart
    rename_map: dict[str, str] = {}
    for c in RATE_STATS_MEAN & set(stat_cols):
        rename_map[c] = f"team_mean_{c}"
    for c in RATE_STATS_MAX & set(stat_cols):
        rename_map[c] = f"team_max_{c}"
    if rename_map:
        out = out.rename(columns=rename_map)

    # Derived: is_home + opponent_nation_id (handy for §I.5 head-to-head deltas)
    out["is_home"] = out["nation_id"] == out["home_nation_id"]
    out["opponent_nation_id"] = out.apply(
        lambda r: r["away_nation_id"] if r["is_home"] else r["home_nation_id"],
        axis=1,
    )

    # Reorder: identity → derived → stats
    front = GROUP_KEY + [c for c in IDENTITY_COLS if c not in GROUP_KEY] \
            + ["is_home", "opponent_nation_id"]
    rest = [c for c in out.columns if c not in front]
    out = out[front + rest]

    print(f"  output: {len(out)} (match, team) rows, {len(out.columns)} cols")
    return out


if __name__ == "__main__":
    df = build()
    df.to_parquet(DST, index=False)
    size_kb = DST.stat().st_size / 1024
    print(f"wrote {DST.name}  ({size_kb:.1f} KB)")
