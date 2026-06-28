"""Phase C/D — Fantasy recommender (per-round, 3-model architecture).

Runs after notebook 16 on the hourly tick. Produces:
  - data/processed/wc26_fantasy_recommendations.{parquet,json}    Model 1 (banker, back-compat)
  - data/processed/wc26_fantasy_models.json                       All 3 model outputs slimmed
  - data/processed/wc26_fantasy_strategy_squads.json              3 challenge squads (one per model)
  - data/processed/wc26_fantasy_position_suggestor.json           Joint per-position-top + look out for
  - data/processed/wc26_fantasy_joint_picks.json                  Consensus + per-model surprises
  - data/processed/wc26_fantasy_round_tracking.json               Per-(model, round) projected vs actual
  - data/eda/archetypes_retrospective_v2.json / archetypes_prospective_v2.json
  - data/processed/history/round_NN/snapshot_{TS}.json            Pre-lock freeze for round tracking

The PWA consumes the JSONs via _emit_pwa_json.py.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import lib.recommender as rec_mod
from lib.recommender import (
    assemble_fixture_profile,
    score_for_model,
    build_joint_picks,
    build_round_tracking,
    mine_archetypes_v2,
    attach_archetypes,
    apply_filters,
    tag_anti_picks,
    assemble_strategy_squad,
    assemble_sb_hunter_squad,
    carry_squad_with_transfers,
    persist_squad,
    load_prior_squad,
    FREE_TRANSFERS_BY_ROUND,
    build_position_suggestor,
    refresh_live_percent_selected,
    refresh_squad_captain_and_bench,
    MODEL_REGISTRY,
)

PROC = ROOT / "data" / "processed"
PWA_JSON = PROC / "json"
PWA_JSON.mkdir(parents=True, exist_ok=True)
EDA_DIR = ROOT / "data" / "eda"

# ── Locked-snapshot directory ────────────────────────────────────────────────
# When the target round becomes R, freeze every cumulative / per-round input
# parquet at the post-R-1 state into `locked/post_round_{R-1:02d}/`. Re-runs
# for the same target round reuse the frozen snapshot, making suggestions
# deterministic across the round window and preventing R3 picks from drifting
# once R3 matches start producing data.
#
# Live FIFA %selected is intentionally NOT locked — it ticks throughout the
# round and the differential / SB-eligibility math benefits from fresh
# ownership.
LOCKED_ROOT = PROC / "locked"

# Bump whenever the lock-build semantics change (rebuilds, filters, schema).
# A reused lock with a stale schema version is force-rebuilt — protects
# against actions/cache restoring a poisoned lock from an older code revision
# (this is exactly what put R3-contaminated picks back on the PWA every cron
# tick despite a clean code path: the cache kept reviving a pre-fix lock).
LOCK_SCHEMA_VERSION = 5
# v5 (2026-06-27): seal the bulk-copy leak vectors. fantasy_players.parquet
# now PROJECTED to lock-bounded state (total_points / last_round_points /
# avg_points / round_points_json / form recomputed from round_points_json
# filtered to round_id <= lock_round). wc26_match_trends_365 filtered by
# snapshot_ts <= lock_cutoff_utc. Polymarket markets + weather forecasts
# bulk-copied (no per-snapshot key) but their mtime stamped in manifest for
# audit. lock_cutoff_utc = min(R{target}.kickoff_utc) - 1h. The lock dir is
# now a TIME CAPSULE at "the moment before R{target}'s first kickoff" —
# any rebuild reproduces the same bounded state regardless of when it runs.

# Cumulative / aggregate parquets we REBUILD from per-match sources inside
# the lock. Listed here for documentation; we don't copy them.
LOCK_REBUILT_FILES = [
    "wc26_stg_fantasy_player_totals.parquet",  # rebuilt from filtered fantasy_player_round_stats
    "wc26_stg_player_powerrank.parquet",       # rebuilt from filtered wc26_player_match_powerrank
    "wc26_stg_players_view.parquet",           # fifa_wc_* cols rebuilt from filtered wc26_player_match_stats_wide
    "wc26_stg_players.parquet",                # same — wide source for the view
]

# Per-match / per-round source parquets we FILTER and copy into the lock.
LOCK_FILTERED_PER_ROUND = "fantasy_player_round_stats.parquet"
LOCK_FILTERED_PER_MATCH = [
    "wc26_player_match_stats_wide.parquet",
    "wc26_player_match_powerrank.parquet",
    "wc26_stg_team_match_metrics.parquet",
]

# Defensive default: bulk-copy EVERY .parquet from PROC into the lock first,
# then overwrite filtered/rebuilt ones below. Beats whitelisting because the
# warehouse pipeline keeps adding parquets (volume, trends_365, nations,
# team_metrics, …) — without the bulk pass, a new dependency means another
# FileNotFoundError on next CI run.


# ── FIFA wide → fifa_wc_* aggregation spec (mirrors _build_nb_16.py Block B)
_NB16_SUM_COLS = [
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
_NB16_AVG_COLS = ["AvgSpeed", "XG"]
_NB16_MAX_COLS = ["TopSpeed"]


def _lock_cutoff_utc(target_round: int) -> "pd.Timestamp | None":
    """The 'time capsule' anchor for the lock dir.

    Defined as min(kickoff_utc) of the target round's fixtures - 1h. This is
    the moment we freeze 'the state of the world' to: every per-snapshot
    table is filtered to <= this timestamp. The 1-hour buffer absorbs the
    pre-match window where lineups + last-minute market moves shouldn't yet
    influence picks (and where a finished match's stats haven't propagated).

    Returns None if R{target} has no fixtures (e.g. R32 before group stage
    finishes) — caller then skips time-pinning and bulk-copies as fallback.
    """
    try:
        rm = pd.read_parquet(PROC / "fantasy_round_matches.parquet")
        sq = pd.read_parquet(PROC / "fantasy_squads.parquet")[["fantasy_squad_id", "abbr"]]
        sq_h = sq.rename(columns={"fantasy_squad_id": "home_squad_id", "abbr": "home_nation_id"})
        sq_a = sq.rename(columns={"fantasy_squad_id": "away_squad_id", "abbr": "away_nation_id"})
        rm2 = rm[rm["round_id"].astype(int) == target_round]
        rm2 = rm2.merge(sq_h, on="home_squad_id", how="left").merge(sq_a, on="away_squad_id", how="left")
        m = pd.read_parquet(PROC / "wc26_stg_matches.parquet")[
            ["fifa_match_id", "home_nation_id", "away_nation_id", "kickoff_utc"]
        ]
        joined = rm2.merge(m, on=["home_nation_id", "away_nation_id"], how="left")
        kos = pd.to_datetime(joined["kickoff_utc"], utc=True, errors="coerce").dropna()
        if kos.empty:
            return None
        return kos.min() - pd.Timedelta(hours=1)
    except Exception:
        return None


def _project_fantasy_players_to_lock(src: Path, dst: Path, lock_round: int) -> dict:
    """Re-derive fantasy_players.parquet's per-round aggregate columns from
    round_points_json, filtered to round_id <= lock_round.

    Closes the bulk-copy leak: total_points / last_round_points / avg_points /
    round_points_json are FIFA-side aggregates that update as new rounds play.
    If we bulk-copy them when the lock is built after R{target} has started,
    they carry R{target} contamination into b4's form/avg/last/total/consistency
    components.

    form is recomputed as the mean of the last 2 projected round_points
    (FIFA's exact form formula isn't public; this is a defensible proxy
    that mirrors how form is consumed in b4 — as a recency-weighted summary).

    Static columns (identity, position, price, percent_selected, is_active)
    are preserved as-is from live (those don't have per-round semantics).
    """
    fp = pd.read_parquet(src)

    def _parse_json(j):
        if not j:
            return {}
        try:
            return json.loads(j) if isinstance(j, str) else dict(j)
        except Exception:
            return {}

    def _project(row):
        rj = _parse_json(row.get("round_points_json"))
        # Filter to keys <= lock_round
        kept = {}
        for k, v in rj.items():
            try:
                ki = int(k)
            except (TypeError, ValueError):
                continue
            if ki <= lock_round and v is not None:
                try:
                    kept[ki] = float(v)
                except (TypeError, ValueError):
                    continue
        # New aggregates
        played = list(kept.values())
        total = float(sum(played)) if played else 0.0
        # last_round_points = round_points at lock_round, or 0 if not played
        last = float(kept.get(lock_round, 0.0))
        # avg = total / count played (0 if none)
        avg = (total / len(played)) if played else 0.0
        # form = mean of last 2 played rounds (or all if fewer)
        if played:
            ordered = [kept[k] for k in sorted(kept.keys())]
            form = float(sum(ordered[-2:]) / min(2, len(ordered)))
        else:
            form = 0.0
        # round_points_json: keep only kept keys, original key type as str
        # (mirrors FIFA endpoint's "1"/"2" string keys)
        rj_out = {str(k): kept[k] for k in sorted(kept.keys())}
        return pd.Series({
            "total_points": total,
            "last_round_points": last,
            "avg_points": avg,
            "form": form,
            "round_points_json": json.dumps(rj_out),
        })

    proj = fp.apply(_project, axis=1)
    for col in ("total_points", "last_round_points", "avg_points", "form", "round_points_json"):
        fp[col] = proj[col]

    fp.to_parquet(dst, index=False)
    # Audit summary returned for manifest stamping
    return {
        "rows": int(len(fp)),
        "max_total_points": float(fp["total_points"].max()),
        "max_last_round_points": float(fp["last_round_points"].max()),
        "max_form": float(fp["form"].max()),
        "players_with_any_played": int((fp["total_points"] != 0).sum()),
    }


def _project_trends_365_to_lock(src: Path, dst: Path, cutoff_utc) -> dict:
    """Filter 365scores trends to snapshot_ts <= cutoff_utc. Trends evolve
    hour-to-hour and reflect ALL data seen so far — leaving them unfiltered
    means R{target}-relevant trends (top trend confidence, isTop flips) feed
    B5's trend_mod even though the rest of the bracket is bounded.
    """
    df = pd.read_parquet(src)
    before = len(df)
    if "snapshot_ts" in df.columns:
        ts = pd.to_datetime(df["snapshot_ts"], utc=True, errors="coerce")
        keep_mask = ts <= cutoff_utc
        df = df[keep_mask]
    df.to_parquet(dst, index=False)
    return {"rows_kept": int(len(df)), "rows_dropped": int(before - len(df))}


def _match_ids_through_round(lock_round: int) -> tuple[set, set]:
    """Return (allowed_match_numbers, allowed_fifa_match_ids) for matches in
    fantasy rounds <= lock_round. Joins fantasy_round_matches → fantasy_squads
    → stg_matches via the home/away nation_id pair.
    """
    rm = pd.read_parquet(PROC / "fantasy_round_matches.parquet")
    sq = pd.read_parquet(PROC / "fantasy_squads.parquet")[["fantasy_squad_id", "abbr"]]
    sq_h = sq.rename(columns={"fantasy_squad_id": "home_squad_id", "abbr": "home_nation_id"})
    sq_a = sq.rename(columns={"fantasy_squad_id": "away_squad_id", "abbr": "away_nation_id"})
    rm2 = rm.merge(sq_h, on="home_squad_id", how="left").merge(sq_a, on="away_squad_id", how="left")
    matches = pd.read_parquet(PROC / "wc26_stg_matches.parquet")[
        ["match_number", "fifa_match_id", "home_nation_id", "away_nation_id"]
    ]
    joined = rm2.merge(matches, on=["home_nation_id", "away_nation_id"], how="left")
    keep = joined[joined["round_id"].astype(int) <= lock_round]
    mn = set(int(v) for v in keep["match_number"].dropna().tolist())
    fmi = set(str(v) for v in keep["fifa_match_id"].dropna().astype(str).tolist())
    return mn, fmi


def _rebuild_fantasy_totals(src_round_stats: Path, dst: Path) -> None:
    """Mirror _build_nb_14.py § 4 against an already-filtered round-stats
    parquet."""
    prs = pd.read_parquet(src_round_stats)
    out = (prs.groupby("fantasy_player_id", dropna=True)
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
    out.to_parquet(dst, index=False)
    print(f"[17]   rebuilt {dst.name}: {len(out)} players")


def _rebuild_player_powerrank(src_match_powerrank: Path, dst: Path) -> None:
    """Mirror _build_nb_14.py § 5 against filtered per-match powerrank."""
    pr = pd.read_parquet(src_match_powerrank)
    out = (pr.groupby(["fifa_player_id", "fifa_team_id"], dropna=False)
              .agg(
                  avg_attacking_score=("attacking_score", "mean"),
                  avg_defensive_score=("defensive_score", "mean"),
                  avg_creativity_score=("creativity_score", "mean"),
                  avg_defending_the_goal_score=("defending_the_goal_score", "mean"),
                  n_matches_ranked=("fifa_match_id", "count"),
                  player_kind=("player_kind", "first"),
              )
              .reset_index())
    out.to_parquet(dst, index=False)
    print(f"[17]   rebuilt {dst.name}: {len(out)} (player, team) pairs")


# Per-match FotMob columns we can rebuild directly (sum / mean per player
# within the lock window). Everything else FotMob ships (chances_created,
# big_chances_created, dribbles, touches, touches_opp_box,
# defensive_contributions, tackles, fouls_committed, xg_against_on_pitch,
# successful_dribbles_pct, duels_won, duels_won_pct) has no per-match
# analogue in wc26_player_recent_matches_fotmob — those get the scaled
# treatment below.
_FOTMOB_REBUILDABLE_COUNTERS = [
    ("goals", "fotmob_wc_goals"),
    ("assists", "fotmob_wc_assists"),
    ("minutes_played", "fotmob_wc_minutes_played"),
    ("yellow_cards", "fotmob_wc_yellow_cards"),
    ("red_cards", "fotmob_wc_red_cards"),
]
# Cumulative non-recoverable FotMob WC cols — proportionally scaled by the
# played-in-lock / played-live ratio (uniform per-match production assumption).
_FOTMOB_SCALABLE_COUNTERS = [
    "fotmob_wc_chances_created",
    "fotmob_wc_big_chances_created",
    "fotmob_wc_dribbles",
    "fotmob_wc_duels_won",
    "fotmob_wc_touches",
    "fotmob_wc_touches_opp_box",
    "fotmob_wc_defensive_contributions",
    "fotmob_wc_tackles",
    "fotmob_wc_fouls_committed",
    "fotmob_wc_xg_against_on_pitch",
]
# Rate / percentage cols — distribution-invariant under uniform-production,
# so the live values can be kept as-is even when the cumulative volume is
# scaled. The model only reads the rate.
_FOTMOB_KEEP_AS_IS_RATES = [
    "fotmob_wc_successful_dribbles_pct",
    "fotmob_wc_duels_won_pct",
    "fotmob_wc_fotmob_rating",  # mean rating — overwritten by per-match rebuild below
]


def _rebuild_fotmob_wc_aggregates(
    recent_src: Path,
    view_path: Path,
    lock_end_utc: pd.Timestamp,
    wc_start_utc: pd.Timestamp = pd.Timestamp("2026-06-11", tz="UTC"),
) -> None:
    """Rewind every fotmob_wc_* col in stg_players_view to its post-lock state.

    Two-track strategy:
      1. RECOVERABLE counters (goals, assists, minutes, yellows, reds, rating)
         — rebuilt from wc26_player_recent_matches_fotmob filtered to WC2026
         matches with match_date_utc in [wc_start_utc, lock_end_utc].
      2. NON-RECOVERABLE counters (chances_created, dribbles, touches, …)
         — scaled by (lock_apps / live_apps) per player. Falls back to 0 when
         the player has any post-lock appearances and live_apps > 0 but the
         per-match recent feed says lock_apps = 0 (i.e. all play was post-lock).

    Rates (pct cols) stay live — distribution-invariant under uniform-production.
    """
    if not recent_src.exists() or not view_path.exists():
        print(f"[17]   skip fotmob_wc rebuild (missing {recent_src.name if not recent_src.exists() else view_path.name})")
        return
    rm = pd.read_parquet(recent_src)
    view = pd.read_parquet(view_path)

    # WC2026 only — league_id 77 includes qualifiers going back to 2025-09,
    # so we additionally bound by tournament start date (2026-06-11 UTC).
    is_wc = (rm["league_id"] == 77) | (
        rm["league_name"].fillna("").str.contains("world cup", case=False, na=False)
    )
    rm["match_date_utc"] = pd.to_datetime(rm["match_date_utc"], utc=True, errors="coerce")
    in_tournament = rm["match_date_utc"].between(wc_start_utc, lock_end_utc, inclusive="both")
    locked = rm[is_wc & in_tournament].copy()
    # "Live" = same filter but bounded only by tournament window — used for
    # the apps ratio to scale non-recoverable counters.
    live = rm[is_wc & (rm["match_date_utc"] >= wc_start_utc)].copy()

    # Apps counters per player (lock vs live)
    def _apps(frame: pd.DataFrame) -> dict:
        played = frame[frame["minutes_played"].fillna(0) > 0]
        return played.groupby("fifa_player_id")["match_id_fotmob"].nunique().to_dict()

    lock_apps = _apps(locked)
    live_apps = _apps(live)

    # Build the per-player rebuilt aggregates from `locked`
    played_lock = locked[locked["minutes_played"].fillna(0) > 0].copy()
    agg_counters = played_lock.groupby("fifa_player_id").agg(
        fotmob_wc_goals=("goals", "sum"),
        fotmob_wc_assists=("assists", "sum"),
        fotmob_wc_minutes_played=("minutes_played", "sum"),
        fotmob_wc_yellow_cards=("yellow_cards", "sum"),
        fotmob_wc_red_cards=("red_cards", "sum"),
    ).reset_index()

    # Mean rating excluding zero / null (FotMob writes 0 for unrated / very-short)
    rating_rows = played_lock[played_lock["fotmob_rating"].fillna(0) > 0]
    rating_agg = rating_rows.groupby("fifa_player_id")["fotmob_rating"].mean().reset_index()
    rating_agg = rating_agg.rename(columns={"fotmob_rating": "fotmob_wc_fotmob_rating"})

    # Merge rebuilds onto the view — overwriting any existing fotmob_wc_* col.
    drop_cols = [c for c, _ in _FOTMOB_REBUILDABLE_COUNTERS] + [
        "fotmob_wc_fotmob_rating",
    ]
    drop_cols = [c for c in drop_cols if c in view.columns]
    # Also will overwrite scaled cols below.
    overwrite_cols = drop_cols + [c for c in _FOTMOB_SCALABLE_COUNTERS if c in view.columns]
    # Save originals for the scaling step
    live_view_cols = view[["fifa_player_id"] + [c for c in _FOTMOB_SCALABLE_COUNTERS if c in view.columns]].copy()

    view = view.drop(columns=overwrite_cols, errors="ignore")
    view = view.merge(agg_counters, on="fifa_player_id", how="left")
    view = view.merge(rating_agg, on="fifa_player_id", how="left")

    # Fill rebuilt counters with 0 — a player with no locked-window play
    # should read as 0 across the recoverable counters.
    for _, view_col in _FOTMOB_REBUILDABLE_COUNTERS:
        if view_col in view.columns:
            view[view_col] = view[view_col].fillna(0)
    if "fotmob_wc_fotmob_rating" in view.columns:
        # No rating signal for players with no locked play yet — leave NaN so
        # the model treats it as missing (matches the live FotMob convention).
        pass

    # Re-attach + scale the non-recoverable counters: scaled = live * (lock_apps / live_apps)
    if _FOTMOB_SCALABLE_COUNTERS:
        live_view_cols["lock_apps"] = live_view_cols["fifa_player_id"].map(lock_apps).fillna(0)
        live_view_cols["live_apps"] = live_view_cols["fifa_player_id"].map(live_apps).fillna(0)
        # Ratio: 1.0 when no live play yet (no contamination), else lock/live.
        # Players with live_apps > 0 AND lock_apps = 0 → ratio = 0 (everything
        # they "have" was earned post-lock; remove it).
        ratio = np.where(
            live_view_cols["live_apps"] > 0,
            live_view_cols["lock_apps"] / live_view_cols["live_apps"].replace(0, 1),
            1.0,
        )
        for col in _FOTMOB_SCALABLE_COUNTERS:
            if col not in live_view_cols.columns:
                continue
            live_view_cols[col] = pd.to_numeric(live_view_cols[col], errors="coerce") * ratio
        # Merge scaled values back
        view = view.merge(
            live_view_cols[["fifa_player_id"] + [c for c in _FOTMOB_SCALABLE_COUNTERS if c in live_view_cols.columns]],
            on="fifa_player_id", how="left",
        )

    view.to_parquet(view_path, index=False)
    rebuilt = [c for _, c in _FOTMOB_REBUILDABLE_COUNTERS if c in view.columns]
    scaled = [c for c in _FOTMOB_SCALABLE_COUNTERS if c in view.columns]
    print(f"[17]   rebuilt fotmob_wc_* in {view_path.name}: "
          f"{len(rebuilt)} from per-match, {len(scaled)} scaled by lock/live apps ratio")


def _rebuild_stg_players_view_fifa_wc(src_wide: Path, live_view_src: Path, dst: Path) -> None:
    """Re-aggregate the fifa_wc_* / fotmob_wc_appearances columns from the
    filtered wide table, then graft them onto the LIVE stg_players_view so
    the bio / club career / market value / recent-form columns are preserved
    unchanged. Only the WC-tournament aggregates get rewound to lock_round.
    """
    wide = pd.read_parquet(src_wide)
    view = pd.read_parquet(live_view_src)

    present = set(wide.columns)
    sum_present = [c for c in _NB16_SUM_COLS if c in present]
    avg_present = [c for c in _NB16_AVG_COLS if c in present]
    max_present = [c for c in _NB16_MAX_COLS if c in present]

    agg_spec = {**{c: "sum" for c in sum_present},
                **{c: "mean" for c in avg_present},
                **{c: "max" for c in max_present}}
    if not agg_spec:
        # No FIFA stats yet (very early in the tournament) — write the live
        # view through and zero out fifa_wc_ totals.
        view.to_parquet(dst, index=False)
        print(f"[17]   rebuilt {dst.name}: wide table empty, passthrough view")
        return

    stats_agg = wide.groupby("fifa_player_id", dropna=True).agg(agg_spec).reset_index()
    stats_agg = stats_agg.rename(columns={c: f"fifa_wc_{c}" for c in agg_spec.keys()})
    # Player-specific appearance count — only matches where the player
    # actually saw the pitch (TimePlayed > 0). FIFA's wide table includes
    # squad-presence rows for benched-but-unused players, so a naive
    # nunique(fifa_match_id) inflates "appearances" to the squad's match
    # count. This was the root cause of the Neymar-shows-3-appearances bug
    # reported on the PWA.
    played_only = wide[wide["TimePlayed"].fillna(0) > 0] if "TimePlayed" in wide.columns else wide
    n_matches = played_only.groupby("fifa_player_id").agg(
        fifa_wc_n_matches=("fifa_match_id", "nunique"),
    ).reset_index()
    stats_agg = stats_agg.merge(n_matches, on="fifa_player_id", how="left")
    # Players with squad rows but no minutes → 0 appearances, not NaN.
    stats_agg["fifa_wc_n_matches"] = stats_agg["fifa_wc_n_matches"].fillna(0).astype("Int64")

    fifa_wc_cols = [c for c in stats_agg.columns if c.startswith("fifa_wc_")]
    # Drop fifa_wc_* + fotmob_wc_appearances (we'll override) from the live
    # view to avoid duplicate columns from the merge.
    drop_cols = [c for c in view.columns if c.startswith("fifa_wc_")]
    drop_cols.append("fotmob_wc_appearances") if "fotmob_wc_appearances" in view.columns else None
    view_trimmed = view.drop(columns=[c for c in drop_cols if c in view.columns])
    out = view_trimmed.merge(stats_agg, on="fifa_player_id", how="left")
    # Authoritative WC appearance count override (mirrors _build_nb_16.py).
    if "fifa_wc_n_matches" in out.columns:
        out["fotmob_wc_appearances"] = out["fifa_wc_n_matches"]

    # Fill NaN for fifa_wc_ counters with 0 — a player with zero matches in
    # the lock window should read as 0, not None, so downstream `.fillna(0)`
    # passes elsewhere don't double-handle.
    for c in fifa_wc_cols:
        out[c] = out[c].fillna(0)

    out.to_parquet(dst, index=False)
    print(f"[17]   rebuilt {dst.name}: {len(out)} players, "
          f"{len(fifa_wc_cols)} fifa_wc_* cols re-aggregated")


def _lib_required_parquets() -> list[str]:
    """Scrape every `PROC / "<name>.parquet"` literal from lib/recommender.py.
    Used to validate an existing lock dir before reuse — if the lib has grown
    a new dependency since the lock was first created, we'll detect the gap
    and recreate. Returns an empty list if the scan fails (treat as no-check).
    """
    try:
        import re
        text = (ROOT / "lib" / "recommender.py").read_text(encoding="utf-8")
        return sorted(set(re.findall(r'PROC\s*/\s*"([^"]+\.parquet)"', text)))
    except Exception as exc:
        print(f"[17]   warn: lib parquet scan failed ({exc}); skipping lock validation")
        return []


def load_locked_percent_selected(lock_dir: Path) -> dict[int, float]:
    """Read the frozen-at-lock-creation %selected map from the lock dir.
    Returns an empty dict if the file is missing or malformed — caller falls
    back to the snapshot-baked pct in fantasy_players.parquet.
    """
    pct_path = lock_dir / "_locked_percent_selected.json"
    if not pct_path.exists():
        return {}
    try:
        payload = json.loads(pct_path.read_text())
        raw = payload.get("percent_selected", {})
        return {int(k): float(v) for k, v in raw.items()}
    except Exception as exc:  # noqa: BLE001
        print(f"[17]   warn: locked pct read failed ({exc}); using snapshot pct")
        return {}


def prepare_locked_snapshot(target_round: int, *, force_relock: bool = False) -> Path:
    """Materialise / reuse the post-Round-(target-1) snapshot directory.

    Strategy:
      1. Filter per-round / per-match source tables to data from rounds
         <= lock_round (lock_round = target - 1).
      2. REBUILD cumulative aggregates (`wc26_stg_fantasy_player_totals`,
         `wc26_stg_player_powerrank`, `wc26_stg_players_view`) from the
         filtered sources so they only reflect locked rounds — never partial
         in-flight round data.
      3. Copy non-round-dependent tables (schedule, markets, identity, squad
         rosters) as-is.

    First call creates the snapshot; subsequent calls reuse it unless
    force_relock=True (env RELOCK=1 also forces). The path is then bound to
    `lib.recommender.PROC` so every downstream loader reads the frozen copy.
    """
    lock_round = max(0, target_round - 1)
    lock_dir = LOCKED_ROOT / f"post_round_{lock_round:02d}"
    if lock_dir.exists() and not force_relock:
        # Three reuse validations — any failure forces a fresh rebuild:
        #   1. Required parquets all present (lib may have grown deps)
        #   2. Manifest schema_version matches current LOCK_SCHEMA_VERSION
        #      (GHA actions/cache can revive a lock built by older code that
        #      had different filter/rebuild semantics — was the actual cause
        #      of every cron tick re-emitting R3-contaminated picks)
        #   3. Content sanity — fifa_wc_TimePlayed.max() ≤ lock_round * 110
        #      (≈ full match + stoppage). If a player has > lock_round * 110
        #      minutes, the wide-stats filter didn't apply correctly and R3
        #      minutes leaked into the cumulative aggregate.
        reasons = []
        required = _lib_required_parquets()
        missing = [p for p in required if not (lock_dir / p).exists()]
        if missing:
            reasons.append(f"missing {len(missing)} parquet(s) (e.g. {missing[:2]})")

        manifest_path = lock_dir / "_lock_manifest.json"
        manifest_version = None
        if manifest_path.exists():
            try:
                manifest_version = int(json.loads(manifest_path.read_text()).get("schema_version", 0))
            except Exception:
                manifest_version = 0
        else:
            manifest_version = 0
        if manifest_version != LOCK_SCHEMA_VERSION:
            reasons.append(
                f"schema v{manifest_version} != current v{LOCK_SCHEMA_VERSION}"
            )

        view_path = lock_dir / "wc26_stg_players_view.parquet"
        if view_path.exists():
            try:
                _v = pd.read_parquet(view_path, columns=["fifa_wc_TimePlayed"])
                _max_t = float(_v["fifa_wc_TimePlayed"].fillna(0).max())
                _cap = lock_round * 110.0  # 90 + stoppage buffer
                if _max_t > _cap:
                    reasons.append(
                        f"fifa_wc_TimePlayed.max={_max_t:.0f} > cap {_cap:.0f} "
                        f"(R3 minutes leaked into lock view)"
                    )
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"content sanity skipped ({exc})")

        # v5+ fantasy_players sanity — catches a bulk-copy regression by
        # walking round_points_json and asserting no rounds > lock_round
        # appear. Cheap (1488 rows × small dict).
        fp_path = lock_dir / "fantasy_players.parquet"
        if fp_path.exists():
            try:
                _fp = pd.read_parquet(fp_path, columns=["round_points_json"])
                _max_round_in_lock = 0
                for j in _fp["round_points_json"].dropna():
                    try:
                        d = json.loads(j) if isinstance(j, str) else dict(j)
                        for k in d.keys():
                            ki = int(k)
                            if ki > _max_round_in_lock:
                                _max_round_in_lock = ki
                    except Exception:
                        continue
                if _max_round_in_lock > lock_round:
                    reasons.append(
                        f"fantasy_players.round_points_json max round "
                        f"{_max_round_in_lock} > lock_round {lock_round} "
                        f"(bulk-copy of fantasy_players leaked R{_max_round_in_lock} aggregates)"
                    )
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"fantasy_players sanity skipped ({exc})")

        if not reasons:
            print(f"[17] reusing locked snapshot: {lock_dir.relative_to(ROOT)} "
                  f"(schema v{LOCK_SCHEMA_VERSION})")
            return lock_dir
        print(f"[17] stale lock at {lock_dir.relative_to(ROOT)}:")
        for r in reasons:
            print(f"[17]   - {r}")
        print(f"[17] re-creating snapshot")
        shutil.rmtree(lock_dir)
    elif lock_dir.exists():
        print(f"[17] force-relock: wiping {lock_dir.relative_to(ROOT)}")
        shutil.rmtree(lock_dir)
    print(f"[17] creating locked snapshot at {lock_dir.relative_to(ROOT)} "
          f"(target R{target_round} -> freeze stats up to R{lock_round})")
    lock_dir.mkdir(parents=True, exist_ok=True)

    # Step 0 — figure out which matches belong to the locked rounds
    allowed_match_numbers, allowed_fifa_match_ids = _match_ids_through_round(lock_round)
    print(f"[17]   lock window: {len(allowed_match_numbers)} matches "
          f"(round_id <= {lock_round})")

    # Step 1 — bulk-copy every parquet from PROC. Filtered + rebuilt files
    # below will overwrite their respective copies. This makes the lock dir
    # self-sufficient even when new parquets get added to the warehouse.
    bulk_copied = 0
    for src in PROC.glob("*.parquet"):
        shutil.copy2(src, lock_dir / src.name)
        bulk_copied += 1
    print(f"[17]   bulk-copied {bulk_copied} parquets from live PROC")

    # Step 2a — filter per-round fantasy stats
    src_prs = PROC / LOCK_FILTERED_PER_ROUND
    dst_prs = lock_dir / LOCK_FILTERED_PER_ROUND
    if src_prs.exists():
        prs = pd.read_parquet(src_prs)
        before = len(prs)
        if "round_id" in prs.columns:
            prs = prs[prs["round_id"].astype(int) <= lock_round]
        prs.to_parquet(dst_prs, index=False)
        print(f"[17]   {LOCK_FILTERED_PER_ROUND}: kept {len(prs)}/{before} rows")
    else:
        print(f"[17]   skip {LOCK_FILTERED_PER_ROUND} (missing)")

    # Step 2b — filter per-match wide tables (FIFA stats + FDH powerrank)
    for fname in LOCK_FILTERED_PER_MATCH:
        src = PROC / fname
        if not src.exists():
            print(f"[17]   skip {fname} (missing)")
            continue
        df = pd.read_parquet(src)
        before = len(df)
        if "match_number" in df.columns and allowed_match_numbers:
            df = df[df["match_number"].astype("Int64").isin(allowed_match_numbers)]
        elif "fifa_match_id" in df.columns and allowed_fifa_match_ids:
            df = df[df["fifa_match_id"].astype(str).isin(allowed_fifa_match_ids)]
        df.to_parquet(lock_dir / fname, index=False)
        print(f"[17]   {fname}: kept {len(df)}/{before} rows")

    # Step 3 — rebuild aggregates from the filtered sources
    if dst_prs.exists():
        _rebuild_fantasy_totals(
            dst_prs, lock_dir / "wc26_stg_fantasy_player_totals.parquet",
        )
    src_match_pr = lock_dir / "wc26_player_match_powerrank.parquet"
    if src_match_pr.exists():
        _rebuild_player_powerrank(
            src_match_pr, lock_dir / "wc26_stg_player_powerrank.parquet",
        )
    src_wide = lock_dir / "wc26_player_match_stats_wide.parquet"
    live_view = PROC / "wc26_stg_players_view.parquet"
    if src_wide.exists() and live_view.exists():
        _rebuild_stg_players_view_fifa_wc(
            src_wide, live_view, lock_dir / "wc26_stg_players_view.parquet",
        )
        # The wide stg_players is referenced indirectly by some callers; if
        # present in live, mirror the same fifa_wc_* override pass onto it.
        live_stg = PROC / "wc26_stg_players.parquet"
        if live_stg.exists():
            _rebuild_stg_players_view_fifa_wc(
                src_wide, live_stg, lock_dir / "wc26_stg_players.parquet",
            )

    # Rebuild fotmob_wc_* on top — closes the previous partial-leak where
    # FotMob's pre-aggregated tournament rollup reflected "now" rather than
    # the lock window. Recoverable cols are summed from per-match recent
    # matches filtered to the lock date window; non-recoverable cols are
    # scaled by the played-in-lock / played-live ratio per player.
    recent_fotmob = lock_dir / "wc26_player_recent_matches_fotmob.parquet"
    if recent_fotmob.exists() and (PROC / "wc26_stg_matches.parquet").exists():
        # Lock window ends at the LATEST kickoff of any match with
        # match_number ≤ max_match_for_lock. allowed_fifa_match_ids was built
        # from those rows so we re-derive the cutoff from stg_matches.
        m_all = pd.read_parquet(PROC / "wc26_stg_matches.parquet")
        if allowed_fifa_match_ids:
            m_in_lock = m_all[m_all["fifa_match_id"].astype(str).isin(allowed_fifa_match_ids)]
            lock_end_utc = pd.to_datetime(m_in_lock["kickoff_utc"], utc=True, errors="coerce").max()
            if pd.notna(lock_end_utc):
                _rebuild_fotmob_wc_aggregates(
                    recent_fotmob,
                    lock_dir / "wc26_stg_players_view.parquet",
                    lock_end_utc,
                )
                # Mirror onto wide stg_players too if present
                if (lock_dir / "wc26_stg_players.parquet").exists():
                    _rebuild_fotmob_wc_aggregates(
                        recent_fotmob,
                        lock_dir / "wc26_stg_players.parquet",
                        lock_end_utc,
                    )

    # Step 4 — PROJECT fantasy_players.parquet (v5+). Bulk-copy left
    # total_points / last_round_points / avg_points / form / round_points_json
    # as live-FIFA-endpoint values, so a lock rebuilt after R{target} starts
    # silently leaks R{target} aggregates into b4. Re-derive from
    # round_points_json filtered to round_id <= lock_round.
    fp_src = PROC / "fantasy_players.parquet"
    fp_dst = lock_dir / "fantasy_players.parquet"
    fp_audit = {}
    if fp_src.exists():
        fp_audit = _project_fantasy_players_to_lock(fp_src, fp_dst, lock_round)
        print(f"[17]   projected fantasy_players: {fp_audit['rows']} rows; "
              f"max_total={fp_audit['max_total_points']:.0f} "
              f"max_last_round={fp_audit['max_last_round_points']:.0f} "
              f"max_form={fp_audit['max_form']:.2f} "
              f"any_played={fp_audit['players_with_any_played']}")

    # Step 5 — TIME-PIN per-snapshot tables to lock_cutoff_utc (v5+).
    # The cutoff is the moment we want the lock to represent: just before
    # the target round's first kickoff. Tables with snapshot_ts get filtered;
    # tables without snapshot_ts (Polymarket, weather) are bulk-copied but
    # their mtime stamped in the manifest for audit.
    cutoff_utc = _lock_cutoff_utc(target_round)
    trends_audit = {}
    trends_src = PROC / "wc26_match_trends_365.parquet"
    if cutoff_utc is not None and trends_src.exists():
        trends_audit = _project_trends_365_to_lock(
            trends_src, lock_dir / "wc26_match_trends_365.parquet", cutoff_utc,
        )
        print(f"[17]   filtered wc26_match_trends_365 to snapshot_ts <= "
              f"{cutoff_utc.isoformat()}: kept {trends_audit['rows_kept']}, "
              f"dropped {trends_audit['rows_dropped']}")
    elif cutoff_utc is None:
        print(f"[17]   skip trend filter: no kickoff_utc for R{target_round} "
              f"(R32 TBD before group-stage close?)")

    # Bulk-copied per-snapshot tables — stamp mtime for audit
    poly_src = PROC / "wc26_match_polymarket_markets.parquet"
    weather_src = PROC / "wc26_match_weather.parquet"
    bulk_mtimes = {}
    for nm, src in (("polymarket_markets", poly_src), ("weather", weather_src)):
        if src.exists():
            bulk_mtimes[nm] = datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc).isoformat()

    # Freeze live %selected ONCE at lock-creation time. The sigmoid in
    # b4's sb_boost (1 / (1 + exp((pct_sel - 5)/1))) is steep at the 5%
    # gate — if we inject fresh ownership at every cron tick it cascades
    # through b4 → bracket_sum_model → ev_model and shuffles both the
    # squad XI and the suggestion lists. Squad/suggestion EVs must be
    # frozen for the round; only the live captain refresh + the PWA's
    # live ownership counter should see fresh ownership.
    pct_path = lock_dir / "_locked_percent_selected.json"
    try:
        from lib.recommender import refresh_live_percent_selected as _rlps
        frozen_pct = _rlps(force=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[17]   live %sel fetch failed at lock-build ({exc}); "
              f"frozen map will be empty (scoring falls back to snapshot pct)")
        frozen_pct = {}
    pct_path.write_text(json.dumps({
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "schema_version": LOCK_SCHEMA_VERSION,
        "n_players": len(frozen_pct),
        "percent_selected": {str(k): float(v) for k, v in frozen_pct.items()},
    }, indent=2))
    print(f"[17]   froze live %selected: {len(frozen_pct)} players -> "
          f"{pct_path.name}")

    # Stamp a manifest for traceability + debugging
    (lock_dir / "_lock_manifest.json").write_text(json.dumps({
        "schema_version": LOCK_SCHEMA_VERSION,
        "target_round": target_round,
        "lock_round": lock_round,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "lock_cutoff_utc": cutoff_utc.isoformat() if cutoff_utc is not None else None,
        "locked_match_count": len(allowed_match_numbers),
        "locked_match_numbers_sample": sorted(allowed_match_numbers)[:10] + ["..."]
            if len(allowed_match_numbers) > 10 else sorted(allowed_match_numbers),
        "total_parquets_in_lock": sum(1 for _ in lock_dir.glob("*.parquet")),
        "filtered_per_round": [LOCK_FILTERED_PER_ROUND] if dst_prs.exists() else [],
        "filtered_per_match": [f for f in LOCK_FILTERED_PER_MATCH if (lock_dir / f).exists()],
        "rebuilt_aggregates": [f for f in LOCK_REBUILT_FILES if (lock_dir / f).exists()],
        "projected_fantasy_players": fp_audit,
        "filtered_trends_365": trends_audit,
        "bulk_copied_snapshot_mtimes_utc": bulk_mtimes,
    }, indent=2, default=str))
    return lock_dir


def pick_target_round() -> int:
    """Pick the round currently in [start, end] — even if the parquet's
    status hasn't flipped to 'playing' yet (FIFA's status field lags). Then
    fall back to next scheduled, then latest known. Also REQUIRES the round
    to have fixtures: scheduled-but-no-fixtures rounds (R32 TBD until the
    group stage finishes) get skipped to the prior playable round.

    2026-06-28: in the gap between rounds (R3 ended, R4 fixtures not yet
    materialized in fantasy_round_matches because upstream nb_03/group-
    standings still in progress), fall back to "highest round_id with
    fixtures" instead of dropping to most-recently-complete (which was R1
    only because R2 has status='playing', R3 'scheduled'). Without this
    fix the recommender targets R1 → empty lock → all downstream fails.
    """
    fr = pd.read_parquet(PROC / "fantasy_rounds.parquet")
    frm = pd.read_parquet(PROC / "fantasy_round_matches.parquet")
    rounds_with_matches = set(frm["round_id"].unique().tolist())
    now = pd.Timestamp.now(tz="UTC")
    fr["start"] = pd.to_datetime(fr["start_date"], utc=True, errors="coerce")
    fr["end"] = pd.to_datetime(fr["end_date"], utc=True, errors="coerce")

    # Currently active by date window
    active = fr[(fr["start"] <= now) & (fr["end"] > now)]
    active = active[active["round_id"].isin(rounds_with_matches)]
    if not active.empty:
        return int(active.iloc[0]["round_id"])
    # Otherwise next scheduled WITH fixtures
    sched = fr[(fr["start"] > now)].sort_values("start")
    sched = sched[sched["round_id"].isin(rounds_with_matches)]
    if not sched.empty:
        return int(sched.iloc[0]["round_id"])
    # Gap-between-rounds: the highest round_id that has fixtures in frm is the
    # last playable target. R3 has fixtures even after R3 ends and before R4
    # fixtures land — keep the lock + emit on R3 until R4 fixtures materialize.
    if rounds_with_matches:
        return int(max(rounds_with_matches))
    # Otherwise the most recently completed (legacy fallback for very-early
    # state where no rounds have fixtures yet)
    done = fr[fr["status"] == "complete"].sort_values("round_id")
    if not done.empty:
        return int(done.iloc[-1]["round_id"])
    return int(fr["round_id"].max())


def sanitize_for_js(obj):
    if isinstance(obj, dict):
        return {k: sanitize_for_js(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_js(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, np.floating):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, pd.Timestamp):
        return None if pd.isna(obj) else obj.isoformat()
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    return obj


def dump_js_safe(obj) -> str:
    return json.dumps(sanitize_for_js(obj), default=str, allow_nan=False)


def df_to_records(df: pd.DataFrame) -> list[dict]:
    out = []
    for _, row in df.iterrows():
        d = {}
        for col, val in row.items():
            try:
                if isinstance(val, (list, dict)):
                    d[col] = val
                elif pd.isna(val):
                    d[col] = None
                else:
                    d[col] = sanitize_for_js(val)
            except (TypeError, ValueError):
                d[col] = val
        out.append(d)
    return out


def tag_chips(row):
    chips = []
    if row.get("anti_pick"):
        chips.append("HEDGE")
    if (row.get("percent_selected") or 50) < 5:
        chips.append("DIFFERENTIAL")
    if (row.get("sb_total") or 0) >= 1:
        chips.append(f"SB_TRACK_x{int(row['sb_total'])}")
    if (row.get("differential") or 0) > 1.5:
        chips.append("SB_LIKELY")
    if (row.get("ceiling_per_app") or 0) > 7:
        chips.append("CEILING_HOT")
    if row.get("fixture_shape") == "consensus_lopsided":
        chips.append("FAVORED_FIXTURE")
    if row.get("trend_top_confidence") and row["trend_top_confidence"] > 0.7:
        chips.append(f"TREND_{row['trend_top_category'].upper()}")
    return chips


def model_to_strategy(model_id: str, cfg: dict, ev_col: str = "ev_model") -> dict:
    """Translate a MODEL_REGISTRY entry to a strategy dict the assembler accepts.

    ev_col: which column on the scored frame the assembler sorts on. Default
    `ev_model` is per-round EV (suggestion layer). For R4+ squad assembly,
    pass `ev_model_path` so the assembler considers path-to-Final value.
    """
    return {
        "id": model_id,
        "name": cfg["name"],
        "blurb": cfg["blurb"],
        "sb_quota": cfg["sb_quota"],
        "ev_col": ev_col,
        "alpha_floor": 0.0,
        "beta_ceiling": 0.0,
        "gamma_differential": 0.0,
        "fixture_filter": None,
        "anti_pick_allowed": True,
    }


def main():
    snapshot_ts = datetime.now(timezone.utc).isoformat()
    # FORCE_TARGET_ROUND=N — diagnostic / once-off run for a specific round
    # (e.g. when group stage is finished in real-world but our upstream
    # status hasn't flipped yet, set FORCE_TARGET_ROUND=4 to pre-build R32
    # squads with bracket-depth boost). Caller responsibility: don't commit
    # the emitted JSONs if the forced round doesn't have real data yet.
    forced = os.environ.get("FORCE_TARGET_ROUND")
    if forced:
        try:
            target = int(forced)
            print(f"[17] target round: {target}  (FORCED via env, snapshot {snapshot_ts})")
        except ValueError:
            target = pick_target_round()
            print(f"[17] target round: {target}  (bad FORCE_TARGET_ROUND={forced!r}, ignored)")
    else:
        target = pick_target_round()
        print(f"[17] target round: {target}  (snapshot {snapshot_ts})")

    # Freeze inputs at post-R(target-1) state. Lib functions look up `PROC`
    # at call time, so rebinding the module attribute is enough — no edits to
    # recommender.py needed. Restored before the per-round historical write
    # so artefacts still land in the LIVE processed dir.
    #
    # ENV controls:
    #   NOLOCK=1  — bypass the lock for this single run (PROC stays at LIVE,
    #               so scoring uses the freshest data including any partial
    #               in-flight round). Useful for one-off diagnostic runs;
    #               the lock dir on disk is left untouched, so the NEXT run
    #               (without NOLOCK) reverts to deterministic locked output.
    #   RELOCK=1  — wipe the lock dir and rebuild from current live PROC
    #               (use when an existing lock was created mid-round and is
    #               carrying partial-round contamination).
    nolock = os.environ.get("NOLOCK", "").lower() in ("1", "true", "yes")
    force_relock = os.environ.get("RELOCK", "").lower() in ("1", "true", "yes")
    original_proc = rec_mod.PROC
    if nolock:
        print(f"[17] NOLOCK=1 — bypassing round-lock for this run; "
              f"scoring uses LIVE PROC. Existing lock dir left untouched.")
        lock_dir = original_proc  # so the manifest line later still makes sense
    else:
        lock_dir = prepare_locked_snapshot(target, force_relock=force_relock)
        rec_mod.PROC = lock_dir
        # Sanity check — confirm the lock window matches the target round.
        # If a stale lock somehow carries post-target round stats (e.g. cache
        # corruption, manual file edit, missing filter on a future input
        # source), abort loudly rather than silently emitting leaked picks.
        try:
            _ft = pd.read_parquet(lock_dir / "wc26_stg_fantasy_player_totals.parquet")
            _max_apps = int(_ft["appearances"].max() if len(_ft) else 0)
            expected_max = target - 1
            if _max_apps > expected_max:
                print(f"[17]   LOCK SANITY FAIL: max appearances={_max_apps} "
                      f"> expected {expected_max} (target R{target}). "
                      f"Wiping lock and forcing re-create from current PROC.")
                rec_mod.PROC = original_proc
                shutil.rmtree(lock_dir, ignore_errors=True)
                lock_dir = prepare_locked_snapshot(target, force_relock=True)
                rec_mod.PROC = lock_dir
            else:
                print(f"[17]   lock sanity OK: max appearances={_max_apps} "
                      f"<= {expected_max}")
        except Exception as exc:  # noqa: BLE001
            print(f"[17]   WARN: lock sanity check skipped ({exc})")

    # 1. Archetypes (mode-agnostic — shared across models)
    print("[17] mining archetypes (retrospective + prospective)…")
    retro = mine_archetypes_v2("retrospective")
    prospective = mine_archetypes_v2("prospective")
    EDA_DIR.mkdir(parents=True, exist_ok=True)
    (EDA_DIR / "archetypes_retrospective_v2.json").write_text(
        json.dumps({k: v for k, v in retro.items() if k != "player_clusters"},
                   indent=2, default=str)
    )
    (EDA_DIR / "archetypes_prospective_v2.json").write_text(
        json.dumps({k: v for k, v in prospective.items() if k != "player_clusters"},
                   indent=2, default=str)
    )
    print(f"[17]   retro k={retro.get('k')} sil={retro.get('silhouette',0):.3f}")
    print(f"[17]   prospective k={prospective.get('k')} sil={prospective.get('silhouette',0):.3f}")

    # 1b. %selected for SQUAD + SUGGESTION scoring is read from the lock dir
    # (frozen at lock-creation time). Cron-to-cron live ownership drift moves
    # players across b4's 5%-gate sb_boost sigmoid, which cascades into
    # ev_model and reshuffles both layers — exactly what the lock is meant
    # to prevent. The live captain refresh further below fetches its own
    # fresh map; that's the only place ownership SHOULD rotate within a round.
    if nolock:
        print("[17] NOLOCK=1 — fetching live %selected for scoring (no freeze)")
        scoring_pct = refresh_live_percent_selected(force=True)
    else:
        scoring_pct = load_locked_percent_selected(lock_dir)
        if not scoring_pct:
            # Self-heal: pre-v4 lock dirs don't carry the frozen file.
            # Snapshot NOW into the existing lock and reuse henceforth so
            # subsequent cron ticks see a stable map without a full relock.
            print("[17] locked %sel map missing — snapshotting into existing lock now")
            scoring_pct = refresh_live_percent_selected(force=True)
            try:
                (lock_dir / "_locked_percent_selected.json").write_text(json.dumps({
                    "captured_utc": datetime.now(timezone.utc).isoformat(),
                    "schema_version": LOCK_SCHEMA_VERSION,
                    "n_players": len(scoring_pct),
                    "percent_selected": {str(k): float(v) for k, v in scoring_pct.items()},
                    "self_healed": True,
                }, indent=2))
            except Exception as exc:  # noqa: BLE001
                print(f"[17]   warn: could not persist self-healed pct ({exc})")
    print(f"[17]   scoring %selected map: {len(scoring_pct)} players (frozen)")

    # 2. Fixture profiles
    print("[17] assembling fixture profiles…")
    fx = assemble_fixture_profile(target)
    print(f"[17]   {len(fx)} fixtures for round {target}")

    # 3. Score under each model
    print(f"[17] scoring under {len(MODEL_REGISTRY)} models…")
    model_outputs: dict[str, pd.DataFrame] = {}
    for mid, cfg in MODEL_REGISTRY.items():
        scored = score_for_model(target, fx, mid, live_pct_selected=scoring_pct)
        scored = attach_archetypes(scored, retro, prospective)
        scored = apply_filters(scored)
        scored = tag_anti_picks(scored)
        scored["reason_chips"] = scored.apply(tag_chips, axis=1)
        scored["snapshot_ts"] = snapshot_ts
        scored["target_round_id"] = target
        scored["model_id"] = mid
        model_outputs[mid] = scored
        top5 = scored.sort_values("ev_model", ascending=False).head(5)
        print(f"[17]   {mid:20s} ({cfg['name']:14s}) — top: " +
              ", ".join(f"{r['known_name']}({r['ev_model']:.1f})"
                        for _, r in top5.iterrows()))

    # 4. Back-compat: Model 1 (Banker) is the legacy recommendation file
    banker = model_outputs["m1_banker"]
    out_parquet = PROC / "wc26_fantasy_recommendations.parquet"
    banker.to_parquet(out_parquet, index=False)
    print(f"[17]   wrote {out_parquet} ({len(banker)} rows) [m1_banker as legacy]")

    client_cols_recs = [
        "target_round_id", "model_id",
        "fantasy_player_id", "fifa_player_id", "nation_id",
        "opponent_nation_id", "is_home", "position", "price", "percent_selected",
        "known_name", "first_name", "last_name",
        "sb_total", "form", "avg_points", "total_points", "last_round_points",
        "start_prob", "differential", "anti_pick",
        "b1_overall", "b2_wc_perf", "b3_external", "b4_fantasy",
        "b5_fixture_mult", "bracket_sum", "ev_bracket",
        "bracket_sum_model", "post_boost_score", "ev_model",
        "floor_p90", "ceiling_p90", "ev_raw_p90",
        "floor_per_app", "ceiling_per_app", "ev_raw_per_app",
        "floor_totals", "ceiling_totals", "ev_raw_totals",
        "goals_index", "team_cs_index", "opp_cs_index",
        "nation_strength_delta", "moneyline_lopsidedness",
        "trend_top_category", "trend_top_pct", "trend_top_confidence",
        "fixture_shape", "mkt_composite_divergence",
        "weather_cluster", "fantasy_match_id", "espn_match_id",
        "archetype_retrospective", "archetype_retrospective_sim",
        "archetype_retrospective_examples",
        "archetype_prospective", "archetype_prospective_sim",
        "archetype_prospective_examples",
        "reason_chips", "snapshot_ts",
    ]
    cols = [c for c in client_cols_recs if c in banker.columns]
    safe_recs = dump_js_safe(df_to_records(banker[cols]))
    (PROC / "wc26_fantasy_recommendations.json").write_text(safe_recs)
    (PWA_JSON / "wc26_fantasy_recommendations.json").write_text(safe_recs)
    print(f"[17]   wrote PWA recommendations.json ({len(banker)} rows)")

    # 5. NEW: all 3 models slimmed for the UI
    print("[17] emitting per-model slimmed outputs…")
    models_payload = {}
    slim_cols = [
        "fantasy_player_id", "fifa_player_id", "nation_id", "opponent_nation_id",
        "is_home", "position", "price", "percent_selected", "known_name",
        "sb_total", "ev_model", "bracket_sum_model", "post_boost_score",
        "b1_overall", "b2_wc_perf", "b3_external", "b4_fantasy", "b5_fixture_mult",
        "fixture_shape", "trend_top_category", "trend_top_confidence",
        "fantasy_match_id", "reason_chips", "is_active",
    ]
    for mid, df in model_outputs.items():
        cfg = MODEL_REGISTRY[mid]
        slim = df[[c for c in slim_cols if c in df.columns]]
        models_payload[mid] = {
            "id": mid,
            "name": cfg["name"],
            "blurb": cfg["blurb"],
            "weights": cfg["weights"],
            "fixture_amplifier": cfg["fixture_amplifier"],
            "sb_quota": cfg["sb_quota"],
            "post_boosts": [b["name"] for b in cfg.get("post_boosts", [])],
            "rows": df_to_records(slim),
        }
    models_payload["target_round_id"] = target
    models_payload["snapshot_ts"] = snapshot_ts
    safe_models = dump_js_safe(models_payload)
    (PROC / "wc26_fantasy_models.json").write_text(safe_models)
    (PWA_JSON / "wc26_fantasy_models.json").write_text(safe_models)
    print(f"[17]   wrote models.json ({sum(len(p['rows']) for k,p in models_payload.items() if isinstance(p,dict) and 'rows' in p)} total rows)")

    # 6. Joint picks (consensus + surprises + per-position top)
    print("[17] building joint picks across models…")
    joint = build_joint_picks(model_outputs, top_n=30)
    joint["target_round_id"] = target
    joint["snapshot_ts"] = snapshot_ts
    safe_joint = dump_js_safe(joint)
    (PROC / "wc26_fantasy_joint_picks.json").write_text(safe_joint)
    (PWA_JSON / "wc26_fantasy_joint_picks.json").write_text(safe_joint)
    print(f"[17]   consensus={len(joint['consensus'])}  surprises=" +
          str({k: len(v) for k, v in joint["surprises"].items()}))

    # 7. Position suggestor — still emit one (used by current UI). Use the
    # Banker model's view as the base; joint per-position-top is also in
    # joint_picks.json for the UI to optionally overlay.
    suggestor = build_position_suggestor(banker)
    suggestor["target_round_id"] = target
    suggestor["snapshot_ts"] = snapshot_ts
    safe_sug = dump_js_safe(suggestor)
    (PROC / "wc26_fantasy_position_suggestor.json").write_text(safe_sug)
    (PWA_JSON / "wc26_fantasy_position_suggestor.json").write_text(safe_sug)

    # 8. Squads — ONE per model. M4 uses the custom SB Hunter assembler;
    # the rest use the generic assembler with ev_model sort + SB-quota cap.
    # ── Bracket-depth EV boost for the SQUAD layer (R4+ only) ────────────
    # User direction: suggestion layer (joint_picks) stays per-round and
    # upcoming-fixture-focused; squad layer must account for path-to-Final.
    # Adds an `ev_model_path` column to each model's scored frame and routes
    # the squad assembler through it. ev_model itself stays untouched so
    # build_joint_picks downstream is unaffected.
    path_factors: dict[str, dict] = {}
    if target >= 4:
        try:
            from lib.bracket_depth import (
                expected_future_rounds_by_nation, apply_path_boost_to_scored,
            )
            from lib.recommender import nation_strength_composite
            # nation_strength_composite reads PROC; PROC was restored above
            # so it sees live data (we want path estimates against current
            # bracket state, not the round-locked one).
            ns = nation_strength_composite()
            ns_lookup = ns.set_index("nation_id")["nation_total_strength"].to_dict()
            matches_for_path = pd.read_parquet(PROC / "wc26_stg_matches.parquet")
            nations_for_path = pd.read_parquet(PROC / "wc26_stg_nations.parquet")
            path_factors = expected_future_rounds_by_nation(
                matches_for_path, nations_for_path, target, ns_lookup,
            )
            print(f"[17] bracket-depth boost active: {len(path_factors)} nations "
                  f"in R{target}+ bracket; alpha=0.15")
            # Apply boost to each model's scored frame as a NEW column
            for mid in list(model_outputs.keys()):
                model_outputs[mid] = apply_path_boost_to_scored(
                    model_outputs[mid], path_factors, alpha=0.15,
                    out_col="ev_model_path",
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[17]   WARN: bracket-depth boost skipped ({exc}); "
                  f"squad assembler will sort on ev_model")

    print(f"[17] assembling {len(MODEL_REGISTRY)} challenge squads…")
    # Carry policy: R1..R3 rebuild (group stage churn). R4 R32 rebuild (FIFA
    # reset window). R5..R8 carry prior squad and apply only N free transfers.
    # The carried path uses load_prior_squad(model_id, target-1); if missing
    # (first run after deploying squad-carry), falls back to rebuild.
    use_carry = target >= 5
    if use_carry:
        print(f"[17]   target R{target} uses CARRY mode "
              f"(prior R{target-1} squad + {FREE_TRANSFERS_BY_ROUND.get(target, 2)} transfers)")
    squads = []
    # For R4+ the squad assembler sorts on `ev_model_path` (bracket-depth-
    # boosted). For R1..R3 it stays on `ev_model` (per-round only).
    squad_ev_col = "ev_model_path" if (target >= 4 and path_factors) else "ev_model"
    for mid, cfg in MODEL_REGISTRY.items():
        strat = model_to_strategy(mid, cfg, ev_col=squad_ev_col)
        # Thread target round into strat so the assembler's plan_chips() can
        # snap chip rounds forward past the current FIFA lock window.
        strat["target_round_id"] = target
        scored = model_outputs[mid]
        assembler_kind = cfg.get("assembler", "default")

        prior = load_prior_squad(mid, target - 1) if use_carry else None
        if use_carry and prior is not None:
            free_t = FREE_TRANSFERS_BY_ROUND.get(target, 2)
            sq = carry_squad_with_transfers(prior, scored, free_t,
                                            budget_m=100.0, max_per_nation=3)
            sq["assembler_path"] = "carry_with_transfers"
            sq["prior_squad_round"] = target - 1
            # Also compute the unbudgeted ideal so K can see "what if we
            # ignored carry" — useful UI for understanding which transfers
            # we're missing
            if assembler_kind == "sb_hunter":
                sq_unbud = assemble_sb_hunter_squad(scored, strat, budget_m=100.0,
                                                      max_per_nation=3, non_budget=True)
            else:
                sq_unbud = assemble_strategy_squad(scored, strat, budget_m=100.0,
                                                    max_per_nation=3, non_budget=True)
        else:
            if assembler_kind == "sb_hunter":
                sq = assemble_sb_hunter_squad(scored, strat, budget_m=100.0, max_per_nation=3)
                sq_unbud = assemble_sb_hunter_squad(scored, strat, budget_m=100.0,
                                                      max_per_nation=3, non_budget=True)
            else:
                sq = assemble_strategy_squad(scored, strat, budget_m=100.0, max_per_nation=3)
                sq_unbud = assemble_strategy_squad(scored, strat, budget_m=100.0,
                                                    max_per_nation=3, non_budget=True)
            sq["assembler_path"] = "rebuild_from_scratch"
        sq["model_id"] = mid
        sq["unbudgeted_variant"] = sq_unbud
        sq["target_round_id"] = target
        sq["snapshot_ts"] = snapshot_ts
        sq["model_id"] = mid
        sq["weights"] = cfg["weights"]
        squads.append(sq)
        print(f"[17]   {mid}: formation={sq['formation']} £{sq['budget_spent_m']:.1f}m "
              f"SB={sq['sb_band_count']}/{cfg['sb_quota']}+ proj={sq['projected_pts_with_captain']:.1f}")

    safe_sq = dump_js_safe(squads)
    (PROC / "wc26_fantasy_strategy_squads.json").write_text(safe_sq)
    (PWA_JSON / "wc26_fantasy_strategy_squads.json").write_text(safe_sq)

    # Restore live PROC for output-writing and round-status reads. Scoring,
    # archetypes, fixture profiles all consumed the locked snapshot above;
    # the historical snapshot + round_tracking blocks below want the live
    # round statuses + history directory.
    rec_mod.PROC = original_proc

    # 8b. Captain on LIVE data + MID/FWD preference + bench priority order.
    # Squad SELECTION stayed lock-bounded to kill the leak; captain CHOICE
    # benefits from fresh form (who's actually scoring right now). We run
    # ONE live-PROC scoring pass under the Banker weights and use those EVs
    # to re-pick captain + re-order bench per squad. Single pass is enough —
    # captain ranking is robust to the model weight choice.
    print("[17] refreshing captain + bench on live data…")
    try:
        live_pct_for_captain = refresh_live_percent_selected(force=True)
        print(f"[17]   live %selected for captain refresh: "
              f"{len(live_pct_for_captain)} players")
        live_fx = assemble_fixture_profile(target)
        live_scored = score_for_model(target, live_fx, "m1_banker",
                                      live_pct_selected=live_pct_for_captain)
        live_ev_by_pid = dict(
            zip(live_scored["fantasy_player_id"].astype(int),
                live_scored["ev_model"].astype(float))
        )
        # Per-model captain selector — pulls the post-boost column the
        # model's MODEL_REGISTRY entry names. Each post-boost was attached
        # to its model's scored frame by _apply_model_boosts, so we look up
        # the column from the corresponding model_outputs entry. Falls back
        # to ev_live if missing.
        for sq in squads:
            mid = sq.get("model_id") or "m1_banker"
            cfg = MODEL_REGISTRY.get(mid, {})
            selector = cfg.get("captain_selector", "ev_live")
            selector_col_by_pid = None
            if selector != "ev_live" and mid in model_outputs:
                src = model_outputs[mid]
                # Post-boost columns are added as "boost_<name>"
                col = f"boost_{selector}"
                if col in src.columns:
                    selector_col_by_pid = dict(
                        zip(src["fantasy_player_id"].astype(int),
                            src[col].astype(float))
                    )
            refresh_squad_captain_and_bench(
                sq, live_ev_by_pid,
                captain_selector=selector,
                selector_col_by_pid=selector_col_by_pid,
            )
        print(f"[17]   refreshed captain on {len(squads)} squads "
              f"(live EVs available for {len(live_ev_by_pid)} players, "
              f"per-model selectors active)")
        # Re-write strategy squads with the refreshed captain + bench order
        safe_sq = dump_js_safe(squads)
        (PROC / "wc26_fantasy_strategy_squads.json").write_text(safe_sq)
        (PWA_JSON / "wc26_fantasy_strategy_squads.json").write_text(safe_sq)
    except Exception as exc:  # noqa: BLE001
        print(f"[17]   WARN: live captain refresh failed ({exc}); "
              f"sticking with lock-bounded captain")

    # Persist FINAL post-refresh squad per model for next round's carry path.
    # Done unconditionally — even on first deploy, R{N} persists so R{N+1}
    # carry path has data. No-op if writes fail.
    for sq in squads:
        try:
            mid = sq.get("model_id")
            if mid:
                persist_squad(sq, mid, target)
        except Exception as exc:  # noqa: BLE001
            print(f"[17]   WARN: persist_squad failed for {sq.get('model_id')}: {exc}")
    print(f"[17]   persisted {len(squads)} squads to {Path('data/processed/persistent_squads')}")

    # 9. Historical snapshot (per-round-lock freeze)
    history_dir = PROC / "history" / f"round_{target:02d}"
    history_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = snapshot_ts.replace(":", "-").replace("+00:00", "Z").split(".")[0]
    snap_path = history_dir / f"snapshot_{safe_ts}.json"
    snap_path.write_text(json.dumps({
        "round_id": target,
        "snapshot_ts": snapshot_ts,
        "model_version": "models_v2",
        "models": list(MODEL_REGISTRY.keys()),
        "strategy_squads": sanitize_for_js(squads),
        "position_suggestor": sanitize_for_js(suggestor),
        "joint_picks": sanitize_for_js(joint),
    }, default=str))
    print(f"[17]   wrote {snap_path}")

    # 10. Round tracking — load committed snapshots from prior rounds + current,
    # join against fantasy_player_round_stats for closed rounds.
    print("[17] building round tracking…")
    squads_by_round: dict[int, list[dict]] = {target: squads}
    history_root = PROC / "history"
    if history_root.exists():
        for round_dir in sorted(history_root.iterdir()):
            if not round_dir.is_dir():
                continue
            try:
                rid = int(round_dir.name.replace("round_", ""))
            except ValueError:
                continue
            if rid == target:
                continue
            # Latest snapshot per round (newest mtime)
            snaps = sorted(round_dir.glob("snapshot_*.json"), key=lambda p: p.stat().st_mtime)
            if not snaps:
                continue
            try:
                snap = json.loads(snaps[-1].read_text())
            except Exception:
                continue
            prior_squads = snap.get("strategy_squads") or []
            squads_by_round[rid] = prior_squads

    tracking = build_round_tracking(squads_by_round)
    tracking["target_round_id"] = target
    tracking["snapshot_ts"] = snapshot_ts
    safe_track = dump_js_safe(tracking)
    (PROC / "wc26_fantasy_round_tracking.json").write_text(safe_track)
    (PWA_JSON / "wc26_fantasy_round_tracking.json").write_text(safe_track)
    print(f"[17]   tracking totals: {tracking['totals']}")

    print("[17] done")
    return model_outputs


if __name__ == "__main__":
    main()
