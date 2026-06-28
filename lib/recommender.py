"""Recommender shared logic.

Imported by BOTH:
- 17a_eda_factor_signal.py — for the closed-round EDA, validating each factor
  against actual fantasy points scored.
- 17_fantasy_recommender.ipynb — for the live per-round prediction.

Functions are organized to mirror the factor catalog in the plan:
  build_closed_rounds() → master (player, round) dataframe
  fixture_profile()     → §A factors per fixture
  player_floor()        → §B factors
  player_ceiling()      → §C
  player_differential() → §D
  player_filters()      → §E
  nation_strength()     → §I composite

Pure functions — take dataframes in, return dataframes out. No fetching.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PROC = ROOT / "data" / "processed"


# ─── Closed-round dataset ────────────────────────────────────────────────────


def build_closed_rounds(rounds: list[int] | None = None) -> pd.DataFrame:
    """One row per (fantasy_player_id, round_id) with all attributes joined.

    This is the EDA's master frame. Each row carries:
      - actual fantasy `points` for that round (ground truth)
      - player identity + position
      - %selected snapshot + price
      - opponent + fixture context
      - player attributes (form, recent windows, fifa_wc_*, fotmob_wc_*, etc.)

    Args:
      rounds: filter to specific round_ids. None = all closed/playing rounds.
    """
    # 1. Per-round actual points (ground truth)
    rs = pd.read_parquet(PROC / "fantasy_player_round_stats.parquet")
    if rounds is not None:
        rs = rs[rs["round_id"].isin(rounds)]

    # 2. Player identity + ownership snapshot (live)
    fp = pd.read_parquet(PROC / "fantasy_players.parquet")[
        ["fantasy_player_id", "fifa_player_id", "fantasy_squad_id",
         "position", "price", "percent_selected", "form",
         "total_points", "avg_points", "last_round_points",
         "first_name", "last_name", "known_name"]
    ]

    # 3. Squad → nation abbreviation
    sq = pd.read_parquet(PROC / "fantasy_squads.parquet")[
        ["fantasy_squad_id", "abbr", "name"]
    ].rename(columns={"abbr": "nation_id", "name": "nation_name"})

    # 4. Player view (the rich attributes) — load EVERY column so the EDA's
    # broad-correlation sweep can scan all 100+ FIFA / FotMob / career stats.
    # Memory cost is trivial (1248 rows × 165 cols ≈ 2MB in memory).
    pv = pd.read_parquet(PROC / "wc26_stg_players_view.parquet")
    # Drop the columns that collide with names already in the closed-rounds
    # frame from other joins to avoid _x/_y suffixes / merge-key conflicts.
    collision_cols = {"position", "fantasy_squad_id", "nation_id", "name"}
    pv = pv[[c for c in pv.columns if c not in collision_cols]]

    # 5. Per-fantasy-player totals (running counters for SB track record).
    # Renamed with _total suffix so they don't collide with the per-round
    # stats from fantasy_player_round_stats above (which has the same
    # tackles/chances/shots column names, but per-round not per-tournament).
    ft = pd.read_parquet(PROC / "wc26_stg_fantasy_player_totals.parquet")[
        ["fantasy_player_id", "scouting_bonus", "appearances", "starting_xi",
         "tackles", "chances_created", "shots_on_target"]
    ].rename(columns={
        "scouting_bonus": "sb_total",
        "tackles": "tackles_total",
        "chances_created": "chances_created_total",
        "shots_on_target": "shots_on_target_total",
    })

    # 6. Fixture for the round — join via (nation, round) → fantasy_round_matches
    rm = pd.read_parquet(PROC / "fantasy_round_matches.parquet")[
        ["round_id", "fantasy_match_id", "home_squad_id", "away_squad_id",
         "home_score", "away_score", "status", "date"]
    ]
    sq_lite = sq[["fantasy_squad_id", "nation_id"]]
    rm_h = rm.merge(sq_lite.rename(columns={"fantasy_squad_id": "home_squad_id",
                                            "nation_id": "home_nation_id"}),
                    on="home_squad_id", how="left")
    rm_h = rm_h.merge(sq_lite.rename(columns={"fantasy_squad_id": "away_squad_id",
                                              "nation_id": "away_nation_id"}),
                      on="away_squad_id", how="left")

    # Build (round_id, nation_id) -> fixture context
    fx_long = pd.concat([
        rm_h.assign(nation_id=rm_h["home_nation_id"],
                    opponent_nation_id=rm_h["away_nation_id"],
                    is_home=True,
                    team_score=rm_h["home_score"],
                    opp_score=rm_h["away_score"]),
        rm_h.assign(nation_id=rm_h["away_nation_id"],
                    opponent_nation_id=rm_h["home_nation_id"],
                    is_home=False,
                    team_score=rm_h["away_score"],
                    opp_score=rm_h["home_score"]),
    ], ignore_index=True)[
        ["round_id", "nation_id", "opponent_nation_id", "is_home",
         "team_score", "opp_score", "status", "date", "fantasy_match_id"]
    ]

    # 7. Stitch it all together
    df = rs.merge(fp, on="fantasy_player_id", how="left")
    df = df.merge(sq, on="fantasy_squad_id", how="left")
    df = df.merge(pv, on="fifa_player_id", how="left")
    df = df.merge(ft, on="fantasy_player_id", how="left")
    df = df.merge(fx_long, on=["round_id", "nation_id"], how="left")

    # Derived: goals scored in fixture, fixture margin, is_clean_sheet for team
    df["fixture_total_goals"] = df["team_score"].fillna(0) + df["opp_score"].fillna(0)
    df["fixture_margin"] = df["team_score"].fillna(0) - df["opp_score"].fillna(0)
    df["team_clean_sheet"] = (df["opp_score"] == 0).astype("Int64")

    return df


# ─── Section A: Fixture profile factors ──────────────────────────────────────


def _yes_price(row) -> float:
    """Extract the Yes/Over implied probability from a market row.

    Prefer last_trade_price; fallback to outcome_prices[0] when last_trade is
    NaN (unresolved markets often have last_trade=NaN but outcomes filled).
    """
    if pd.notna(row.get("last_trade_price")):
        return float(row["last_trade_price"])
    op = row.get("outcome_prices")
    if op is None:
        return np.nan
    try:
        return float(op[0])
    except Exception:
        return np.nan


def build_match_markets_wide() -> pd.DataFrame:
    """Pivot wc26_match_polymarket_markets to one row per espn_match_id.

    Columns emitted (any may be NaN where the market is absent):
      Moneyline: p_home_win, p_away_win, p_draw
      Match totals: p_over_{0_5, 1_5, 2_5, 3_5, 4_5, 5_5, 6_5, 7_5, 8_5, 9_5}
      First-half totals: p_h1_over_{0_5, 1_5, 2_5}
      BTTS: p_btts, p_btts_h1, p_btts_h2
      Per-side O/U: p_home_scores_{0_5,1_5,2_5}, p_away_scores_{0_5,1_5,2_5}
      Derived CS: p_home_cs (=1-p_away_scores_0_5), p_away_cs (=1-p_home_scores_0_5)
      Volume: vol_moneyline, vol_other
    """
    mk = pd.read_parquet(PROC / "wc26_match_polymarket_markets.parquet")
    matches = pd.read_parquet(PROC / "wc26_stg_matches.parquet")[
        ["espn_match_id", "home_nation_id", "away_nation_id", "fifa_match_id"]
    ]
    mk = mk.merge(matches, on="espn_match_id", how="left")
    mk["yes_price"] = mk.apply(_yes_price, axis=1)

    out = matches.copy()

    # --- Moneyline + Draw ---
    # Slug tail for moneyline is the 3-letter nation code (lowercase).
    # We match by checking whether the lowercased home/away nation_id appears
    # at the end of the slug.
    def _ml_extract(grp):
        h = grp.name[1].lower() if isinstance(grp.name, tuple) and pd.notna(grp.name[1]) else None
        a = grp.name[2].lower() if isinstance(grp.name, tuple) and pd.notna(grp.name[2]) else None
        p_h = grp[grp["market_slug"].str.endswith(h, na=False)]["yes_price"].max() if h else np.nan
        p_a = grp[grp["market_slug"].str.endswith(a, na=False)]["yes_price"].max() if a else np.nan
        return pd.Series({"p_home_win": p_h, "p_away_win": p_a})

    ml = mk[mk["category"] == "moneyline"].groupby(
        ["espn_match_id", "home_nation_id", "away_nation_id"], dropna=False
    ).apply(_ml_extract).reset_index()
    out = out.merge(ml, on=["espn_match_id", "home_nation_id", "away_nation_id"], how="left")

    draws = mk[mk["category"] == "draw"].groupby("espn_match_id")["yes_price"].max() \
        .rename("p_draw").reset_index()
    out = out.merge(draws, on="espn_match_id", how="left")

    # --- Match totals: total-Xpt5 (NOT half-, NOT team-) ---
    for thr in [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5]:
        slug_marker = f"total-{int(thr)}pt5"
        mask = (
            mk["market_slug"].str.contains(slug_marker, na=False)
            & ~mk["market_slug"].str.contains("half-total", na=False)
            & ~mk["market_slug"].str.contains("team-total", na=False)
        )
        sub = mk[mask].groupby("espn_match_id")["yes_price"].max().rename(
            f"p_over_{str(thr).replace('.', '_')}"
        ).reset_index()
        out = out.merge(sub, on="espn_match_id", how="left")

    # --- First-half totals: half-total-Xpt5 ---
    for thr in [0.5, 1.5, 2.5]:
        slug_marker = f"half-total-{int(thr)}pt5"
        mask = mk["market_slug"].str.contains(slug_marker, na=False) \
            & ~mk["market_slug"].str.contains("team-total", na=False)
        sub = mk[mask].groupby("espn_match_id")["yes_price"].max().rename(
            f"p_h1_over_{str(thr).replace('.', '_')}"
        ).reset_index()
        out = out.merge(sub, on="espn_match_id", how="left")

    # --- BTTS (full + per-half) ---
    btts_full = mk[mk["question"].str.contains("Both Teams to Score$", na=False, regex=True)] \
        .groupby("espn_match_id")["yes_price"].max().rename("p_btts").reset_index()
    out = out.merge(btts_full, on="espn_match_id", how="left")

    btts_h1 = mk[mk["market_slug"].str.contains("btts-first-half", na=False)] \
        .groupby("espn_match_id")["yes_price"].max().rename("p_btts_h1").reset_index()
    out = out.merge(btts_h1, on="espn_match_id", how="left")

    btts_h2 = mk[mk["market_slug"].str.contains("btts-second-half", na=False)] \
        .groupby("espn_match_id")["yes_price"].max().rename("p_btts_h2").reset_index()
    out = out.merge(btts_h2, on="espn_match_id", how="left")

    # --- Per-side O/U: team-total-{home|away}-Xpt5 ---
    for side in ["home", "away"]:
        for thr in [0.5, 1.5, 2.5]:
            slug_marker = f"team-total-{side}-{int(thr)}pt5"
            mask = mk["market_slug"].str.contains(slug_marker, na=False) \
                & ~mk["market_slug"].str.contains("first-half|second-half", na=False, regex=True)
            sub = mk[mask].groupby("espn_match_id")["yes_price"].max().rename(
                f"p_{side}_scores_{str(thr).replace('.', '_')}"
            ).reset_index()
            out = out.merge(sub, on="espn_match_id", how="left")

    # Derived per-side CS: opponent fails to score = 1 - p(opp scores 0.5+)
    out["p_home_cs"] = 1 - out["p_away_scores_0_5"]
    out["p_away_cs"] = 1 - out["p_home_scores_0_5"]

    # Volume
    vol = pd.read_parquet(PROC / "wc26_polymarket_match_volume.parquet")[
        ["espn_match_id", "volume_moneyline", "volume_other"]
    ].rename(columns={"volume_moneyline": "vol_moneyline", "volume_other": "vol_other"})
    out = out.merge(vol, on="espn_match_id", how="left")

    return out


# ─── Constants from EDA v2 (D-6, EDA §4 calibration) ────────────────────────

# 365scores trend calibration multipliers. Read: confidence-weight a trend's
# raw `percentage` by category × percentage-bucket hit-rate observed on MD1+MD2.
TREND_CALIBRATION = {
    # category: list of (lower_pct, upper_pct, multiplier_0_to_1)
    "doubleChance": [(0.9, 1.01, 1.00), (0.8, 0.9, 0.85), (0.7, 0.8, 0.90), (0.0, 0.7, 0.50)],
    "btts":         [(0.9, 1.01, 1.00), (0.8, 0.9, 0.80), (0.7, 0.8, 0.70), (0.0, 0.7, 0.20)],
    "first-goal":   [(0.9, 1.01, 0.50), (0.8, 0.9, 0.80), (0.7, 0.8, 0.75), (0.0, 0.7, 0.20)],
    "result":       [(0.9, 1.01, 0.85), (0.8, 0.9, 0.50), (0.7, 0.8, 0.45), (0.0, 0.7, 0.30)],
    "totals":       [(0.9, 1.01, 0.50), (0.8, 0.9, 0.55), (0.7, 0.8, 0.60), (0.0, 0.7, 0.50)],
    "1st-half":     [(0.9, 1.01, 0.50), (0.8, 0.9, 0.60), (0.7, 0.8, 0.25), (0.0, 0.7, 0.30)],
}

# Per-fixture weather/roof modifier (D-6). Multipliers applied to goals_index +
# cs_index in the fixture profile.
WEATHER_MODIFIER = {
    # cluster_id: dict of factor multipliers
    0: {"goals_index": 1.00, "cs_index": 1.00, "draw_boost": 0.00},
    1: {"goals_index": 0.92, "cs_index": 1.10, "draw_boost": 0.10},
    2: {"goals_index": 0.88, "cs_index": 1.00, "draw_boost": -0.10},
    3: {"goals_index": 1.00, "cs_index": 1.00, "draw_boost": 0.00},
}

ROOF_MODIFIER = {"retractable": 1.15, "fixed": 1.05, "open": 1.00}

# Stage-conditional §I composite weights (D-2)
STAGE_I_WEIGHTS = {
    "group":   {"i1": 0.33, "i2": 0.33, "i4": 0.34, "a14_w": 1.0},
    "r32":     {"i1": 0.20, "i2": 0.40, "i4": 0.40, "a14_w": 0.8},
    "r16":     {"i1": 0.20, "i2": 0.40, "i4": 0.40, "a14_w": 0.8},
    "qf":      {"i1": 0.10, "i2": 0.40, "i4": 0.50, "a14_w": 0.6},
    "sf":      {"i1": 0.10, "i2": 0.40, "i4": 0.50, "a14_w": 0.6},
    "final":   {"i1": 0.10, "i2": 0.40, "i4": 0.50, "a14_w": 0.6},
}

# FIFA Fantasy point values by position (master plan §scoring schema)
GOAL_PTS = {"GK": 9, "DEF": 7, "MID": 6, "FWD": 5}
CS_PTS = {"GK": 5, "DEF": 5, "MID": 1, "FWD": 0}


# ─── Section A: Fixture profile assembler ────────────────────────────────────


def assign_weather_cluster(row: pd.Series) -> int:
    """Map a match row's (venue, weather) to one of 4 EDA clusters.

    From EDA §2:
      cluster 1: hot/dry (>28°C, humidity <50%) — Atlanta/Boston/Dallas/Houston/Miami
      cluster 2: altitude — Guadalajara/Mexico City venues
      cluster 3: wet (humidity >90%) — small sample
      cluster 0: everything else (mild/humid)
    """
    altitude_venues = {"Estadio Akron", "Estadio Banorte"}
    if row.get("espn_venue_name") in altitude_venues:
        return 2
    t = row.get("temperature_c")
    h = row.get("humidity_pct")
    if pd.notna(h) and h > 90:
        return 3
    if pd.notna(t) and pd.notna(h) and t > 28 and h < 50:
        return 1
    return 0


def score_match_trend(espn_match_id: str, trends_df: pd.DataFrame) -> dict:
    """Pick the top trend per match using calibrated confidence."""
    sub = trends_df[trends_df["espn_match_id"] == str(espn_match_id)]
    if sub.empty:
        return {"trend_top_category": None, "trend_top_pct": np.nan,
                "trend_top_confidence": np.nan, "trend_top_text": None}
    sub = sub.sort_values("snapshot_ts").drop_duplicates("trend_id", keep="last")

    def cat_of_line(lt_id):
        m = {1: "result", 12: "doubleChance", 14: "doubleChance",
             5: "totals", 6: "totals", 7: "btts", 8: "first-goal",
             3: "1st-half"}
        return m.get(int(lt_id) if pd.notna(lt_id) else -1, "result")

    sub = sub.copy()
    sub["category"] = sub["lineTypeId"].apply(cat_of_line)

    def calibrate(row):
        buckets = TREND_CALIBRATION.get(row["category"], TREND_CALIBRATION["result"])
        pct = row["percentage"]
        if pd.isna(pct):
            return 0.0
        for lo, hi, mult in buckets:
            if lo <= pct < hi:
                return pct * mult
        return 0.0

    sub["confidence"] = sub.apply(calibrate, axis=1)
    top = sub.loc[sub["confidence"].idxmax()]
    return {
        "trend_top_category": top["category"],
        "trend_top_pct": float(top["percentage"]),
        "trend_top_confidence": float(top["confidence"]),
        "trend_top_text": top["text"],
    }


def assemble_fixture_profile(round_id: int) -> pd.DataFrame:
    """Per-fixture §A factor block for the given round.

    Returns one row per fantasy_match_id with the full fixture profile that
    feeds every player's score on that fixture.
    """
    rm = pd.read_parquet(PROC / "fantasy_round_matches.parquet")
    rm = rm[rm["round_id"] == round_id].copy()

    sq = pd.read_parquet(PROC / "fantasy_squads.parquet")[["fantasy_squad_id", "abbr"]] \
        .rename(columns={"abbr": "nation_id"})
    rm = rm.merge(sq.rename(columns={"fantasy_squad_id": "home_squad_id",
                                     "nation_id": "home_nation_id"}),
                  on="home_squad_id", how="left")
    rm = rm.merge(sq.rename(columns={"fantasy_squad_id": "away_squad_id",
                                     "nation_id": "away_nation_id"}),
                  on="away_squad_id", how="left")

    # Bridge to fifa_match_id + venue/weather via matches table on (date, nation pair).
    matches = pd.read_parquet(PROC / "wc26_stg_matches.parquet")
    bridge_cols = ["espn_match_id", "fifa_match_id", "home_nation_id", "away_nation_id",
                   "espn_venue_name", "roof_type", "surface", "temperature_c",
                   "humidity_pct", "stage", "fifa_referee_id"]
    rm = rm.merge(matches[bridge_cols], on=["home_nation_id", "away_nation_id"], how="left")

    # Polymarket wide pivot
    mk = build_match_markets_wide()
    rm = rm.merge(mk.drop(columns=["home_nation_id", "away_nation_id", "fifa_match_id"]),
                  on="espn_match_id", how="left")

    # Nation strength composite (§I)
    ns = nation_strength_composite()[["nation_id", "nation_total_strength",
                                       "i1_static", "i2_form", "i4_player"]]
    rm = rm.merge(ns.rename(columns={"nation_id": "home_nation_id",
                                     "nation_total_strength": "home_strength"}),
                  on="home_nation_id", how="left")
    rm = rm.merge(ns.rename(columns={"nation_id": "away_nation_id",
                                     "nation_total_strength": "away_strength",
                                     "i1_static": "i1_static_away",
                                     "i2_form": "i2_form_away",
                                     "i4_player": "i4_player_away"}),
                  on="away_nation_id", how="left")

    # A14 nation_strength_delta (home perspective). Stage-conditional weighting.
    stage_key = "group" if round_id <= 3 else "r32" if round_id == 4 else \
                "r16" if round_id == 5 else "qf" if round_id == 6 else \
                "sf" if round_id == 7 else "final"
    sw = STAGE_I_WEIGHTS[stage_key]
    rm["nation_strength_delta"] = (
        sw["a14_w"] * (rm["home_strength"].fillna(0.5) - rm["away_strength"].fillna(0.5))
    )

    # Weather cluster + modifiers
    rm["weather_cluster"] = rm.apply(assign_weather_cluster, axis=1) if len(rm) else 0
    if len(rm):
        wm_series = rm["weather_cluster"].map(WEATHER_MODIFIER)
        # apply(pd.Series) on a Series-of-dicts returns a DataFrame; on an
        # empty/all-None series it can return a Series, which breaks the
        # subsequent .rename(columns=). Guard by building the DataFrame
        # explicitly from the dict map.
        wm = pd.DataFrame(
            list(wm_series.fillna({}).values),
            index=rm.index,
            columns=["goals_index", "cs_index", "draw_boost"],
        )
    else:
        wm = pd.DataFrame(columns=["goals_index", "cs_index", "draw_boost"])
    rm = pd.concat([rm, wm.rename(columns={"goals_index": "weather_goals_mult",
                                            "cs_index": "weather_cs_mult",
                                            "draw_boost": "weather_draw_boost"})], axis=1)
    rm["roof_goals_mult"] = rm["roof_type"].map(ROOF_MODIFIER).fillna(1.0)

    # 365scores trend (top per match)
    try:
        trends = pd.read_parquet(PROC / "wc26_match_trends_365.parquet")
        trends["espn_match_id"] = trends["espn_match_id"].astype(str)
        trend_rows = rm["espn_match_id"].astype(str).apply(lambda e: score_match_trend(e, trends))
        rm = pd.concat([rm, pd.DataFrame(trend_rows.tolist(), index=rm.index)], axis=1)
    except FileNotFoundError:
        for c in ["trend_top_category", "trend_top_pct", "trend_top_confidence", "trend_top_text"]:
            rm[c] = None

    # Composite indices (combine market + weather + roof)
    rm["goals_index"] = (
        rm["p_over_2_5"].fillna(0.5)
        * rm["weather_goals_mult"]
        * rm["roof_goals_mult"]
    ).clip(0, 1)
    rm["cs_index_home"] = (rm["p_home_cs"].fillna(0.3) * rm["weather_cs_mult"]).clip(0, 1)
    rm["cs_index_away"] = (rm["p_away_cs"].fillna(0.3) * rm["weather_cs_mult"]).clip(0, 1)

    # A2b lopsidedness signed: positive = home favored
    rm["moneyline_lopsidedness"] = (
        rm["p_home_win"].fillna(0.33) - rm["p_away_win"].fillna(0.33)
    )

    # A15 market-vs-composite divergence (absolute) + signed
    rm["mkt_composite_divergence"] = (rm["moneyline_lopsidedness"] - rm["nation_strength_delta"]).abs()
    rm["mkt_composite_divergence_signed"] = rm["moneyline_lopsidedness"] - rm["nation_strength_delta"]

    # Fixture shape enum (§I.5 rule)
    def classify_shape(r):
        ml = r["moneyline_lopsidedness"]
        ns = r["nation_strength_delta"]
        if pd.isna(ml) or pd.isna(ns):
            return "unknown"
        # |ml| and |ns| both >0.4 and same sign → both agree on a favorite
        if abs(ml) > 0.4 and abs(ns) > 0.4 and (ml * ns) > 0:
            return "consensus_lopsided"
        if abs(ml) < 0.25 and abs(ns) < 0.25:
            return "consensus_tight"
        if abs(ml) > 0.4 and abs(ns) < 0.25:
            return "market_overconfident"
        if abs(ml) < 0.25 and abs(ns) > 0.4:
            return "composite_overconfident"
        return "mixed"

    rm["fixture_shape"] = rm.apply(classify_shape, axis=1)

    return rm


# ─── Section B/C/D: Per-player scoring ───────────────────────────────────────


def _safe_div(a, b):
    return np.where((b == 0) | pd.isna(b), np.nan, a / b)


def score_players_for_round(round_id: int, fixture_profiles: pd.DataFrame) -> pd.DataFrame:
    """For each (player, fixture in round_id), compute Floor/Ceiling/Differential
    in all three normalization modes (§G). Returns one row per (player, fixture).
    """
    # Player universe
    fp = pd.read_parquet(PROC / "fantasy_players.parquet")[
        ["fantasy_player_id", "fifa_player_id", "fantasy_squad_id", "position",
         "price", "percent_selected", "form", "total_points", "avg_points",
         "last_round_points", "first_name", "last_name", "known_name",
         "is_active"]
    ]
    fp["one_to_watch"] = False
    fp["injury"] = None
    sq = pd.read_parquet(PROC / "fantasy_squads.parquet")[
        ["fantasy_squad_id", "abbr", "name"]
    ].rename(columns={"abbr": "nation_id", "name": "nation_name"})
    fp = fp.merge(sq, on="fantasy_squad_id", how="left")

    # Enrichment from stg_players_view
    pv = pd.read_parquet(PROC / "wc26_stg_players_view.parquet")
    keep = ["fifa_player_id", "fifa_wc_TimePlayed", "fotmob_wc_appearances",
            "fifa_wc_Goals", "fifa_wc_Assists", "fifa_wc_AttemptAtGoalOnTarget",
            "fifa_wc_AttemptAtGoal", "fifa_wc_XG", "fifa_wc_CleanSheets",
            "fifa_wc_GoalkeeperSaves",
            "fotmob_wc_chances_created", "fotmob_wc_big_chances_created",
            "fotmob_wc_touches_opp_box", "fotmob_wc_duels_won_pct",
            "wc_rating", "recent5_fotmob_rating", "recent5_started_pct",
            "recent10_started_pct", "recent5_goals", "recent10_goals",
            "recent15_goals", "recent5_player_of_the_match",
            "avg_attacking_score", "avg_defensive_score", "avg_creativity_score",
            "avg_defending_the_goal_score"]
    pv_avail = [c for c in keep if c in pv.columns]
    fp = fp.merge(pv[pv_avail], on="fifa_player_id", how="left")

    # SB track record + per-stat totals
    ft = pd.read_parquet(PROC / "wc26_stg_fantasy_player_totals.parquet")[
        ["fantasy_player_id", "scouting_bonus", "tackles", "chances_created",
         "shots_on_target"]
    ].rename(columns={"scouting_bonus": "sb_total",
                      "tackles": "tackles_total",
                      "chances_created": "chances_created_total",
                      "shots_on_target": "shots_on_target_total"})
    fp = fp.merge(ft, on="fantasy_player_id", how="left")

    # Build per-fixture rows: cross join fixture profile with players on (nation_id)
    fx = fixture_profiles.copy()
    fx_h = fx.copy()
    fx_h["nation_id"] = fx_h["home_nation_id"]
    fx_h["is_home"] = True
    fx_h["opponent_nation_id"] = fx_h["away_nation_id"]
    fx_h["team_cs_index"] = fx_h["cs_index_home"]
    fx_h["opp_cs_index"] = fx_h["cs_index_away"]
    fx_h["team_strength"] = fx_h["home_strength"]
    fx_h["opp_strength"] = fx_h["away_strength"]

    fx_a = fx.copy()
    fx_a["nation_id"] = fx_a["away_nation_id"]
    fx_a["is_home"] = False
    fx_a["opponent_nation_id"] = fx_a["home_nation_id"]
    fx_a["team_cs_index"] = fx_a["cs_index_away"]
    fx_a["opp_cs_index"] = fx_a["cs_index_home"]
    fx_a["team_strength"] = fx_a["away_strength"]
    fx_a["opp_strength"] = fx_a["home_strength"]

    fx_long = pd.concat([fx_h, fx_a], ignore_index=True)
    fx_cols_keep = ["fantasy_match_id", "espn_match_id", "fifa_match_id",
                    "nation_id", "opponent_nation_id", "is_home",
                    "goals_index", "team_cs_index", "opp_cs_index",
                    "p_home_win", "p_away_win", "p_draw", "p_btts",
                    "nation_strength_delta", "team_strength", "opp_strength",
                    "weather_cluster", "weather_draw_boost", "fifa_referee_id",
                    "trend_top_category", "trend_top_pct", "trend_top_confidence",
                    "fixture_shape", "mkt_composite_divergence",
                    "mkt_composite_divergence_signed", "moneyline_lopsidedness",
                    "stage", "espn_venue_name", "roof_type", "surface",
                    "temperature_c", "humidity_pct"]
    fx_long = fx_long[[c for c in fx_cols_keep if c in fx_long.columns]]

    df = fp.merge(fx_long, on="nation_id", how="inner")

    # ─── Three normalization denominators ────────────────────────────────────
    mins = df.get("fifa_wc_TimePlayed", pd.Series(0, index=df.index)).fillna(0).astype(float)
    apps = df.get("fotmob_wc_appearances", pd.Series(0, index=df.index)).fillna(0).astype(float)
    df["denom_p90"] = (mins / 90).replace(0, np.nan)
    df["denom_per_app"] = apps.replace(0, np.nan)
    df["denom_totals"] = 1.0

    # Helper: emit a quantity in all three modes
    def per_mode(raw):
        return {
            "p90": raw / df["denom_p90"],
            "per_app": raw / df["denom_per_app"],
            "totals": raw.astype(float),
        }

    # ─── B Floor components ──────────────────────────────────────────────────
    start_prob = df["recent5_started_pct"].fillna(df["recent10_started_pct"]).fillna(0.5).clip(0, 1)
    appearance_pts = np.where(mins >= 60, 2, 1)

    # Position-conditional Floor contributions
    pos = df["position"].fillna("MID")
    is_gk = (pos == "GK")
    is_def = (pos == "DEF")
    is_mid = (pos == "MID")
    is_fwd = (pos == "FWD")

    # GK saves bonus (every 3 saves → +1)
    saves = df.get("fifa_wc_GoalkeeperSaves", pd.Series(0, index=df.index)).fillna(0)
    saves_mode = per_mode(saves)
    # MID tackles volume (every 3 → +1)
    tackles = df["tackles_total"].fillna(0)
    tackles_mode = per_mode(tackles)
    # MID chances created (every 2 → +1)
    cc = df["chances_created_total"].fillna(0)
    cc_mode = per_mode(cc)
    # FWD SoT (every 2 → +1)
    sot = df["shots_on_target_total"].fillna(0)
    sot_mode = per_mode(sot)
    # DEF clean-sheet prior (use CS rate × A3)
    fifa_cs = df.get("fifa_wc_CleanSheets", pd.Series(0, index=df.index)).fillna(0)
    cs_rate = _safe_div(fifa_cs, apps)
    cs_rate = pd.Series(cs_rate, index=df.index).fillna(0.3)

    # Composite Floor priors (D-1 / C.4): avg_points/total_points lag-1 prior, weight 0.3
    avg_pts = df["avg_points"].fillna(0).astype(float)

    floor_modes = {}
    for mode in ["p90", "per_app", "totals"]:
        f = start_prob * appearance_pts
        f = f + is_gk * (saves_mode[mode].fillna(0) / 3.0)
        f = f + is_gk * (df["team_cs_index"].fillna(0.3) * CS_PTS["GK"])
        f = f + is_def * (cs_rate * df["team_cs_index"].fillna(0.3) * CS_PTS["DEF"])
        f = f + is_mid * (tackles_mode[mode].fillna(0) / 3.0)
        f = f + is_mid * (cc_mode[mode].fillna(0) / 2.0)
        f = f + is_mid * (df["team_cs_index"].fillna(0.3) * CS_PTS["MID"])
        f = f + is_fwd * (sot_mode[mode].fillna(0) / 2.0)
        f = f + 0.3 * avg_pts  # lag-1 prior (dampened)
        floor_modes[mode] = f.fillna(0).clip(0, 30)

    # ─── C Ceiling components ────────────────────────────────────────────────
    fifa_goals = df.get("fifa_wc_Goals", pd.Series(0, index=df.index)).fillna(0)
    fifa_xg = df.get("fifa_wc_XG", pd.Series(0, index=df.index)).fillna(0)
    fifa_assists = df.get("fifa_wc_Assists", pd.Series(0, index=df.index)).fillna(0)
    big_chances = df.get("fotmob_wc_big_chances_created", pd.Series(0, index=df.index)).fillna(0)

    # Recent form goal blending (weights 0.5, 0.3, 0.2)
    r5g = df.get("recent5_goals", pd.Series(0, index=df.index)).fillna(0)
    r10g = df.get("recent10_goals", pd.Series(0, index=df.index)).fillna(0)
    r15g = df.get("recent15_goals", pd.Series(0, index=df.index)).fillna(0)
    recent_goal_rate = (0.5 * r5g / 5 + 0.3 * r10g / 10 + 0.2 * r15g / 15).clip(0, 2)

    # Form: prefer fotmob rating (independent of fantasy autocorr)
    form_rating = df["recent5_fotmob_rating"].fillna(6.5).astype(float)
    form_mult = ((form_rating - 6.0) / 4.0).clip(-0.15, 0.20) + 1.0  # multiplier ~0.85..1.20

    pot_m = df.get("recent5_player_of_the_match", pd.Series(0, index=df.index)).fillna(0)

    goal_pts_pos = pos.map(GOAL_PTS).fillna(0)
    fifa_xg_mode = per_mode(fifa_xg)
    assists_mode = per_mode(fifa_assists)
    big_chances_mode = per_mode(big_chances)

    ceiling_modes = {}
    for mode in ["p90", "per_app", "totals"]:
        # Goals prob blends fifa_xg per denom + recent form
        gp = (fifa_xg_mode[mode].fillna(0) * 0.6 + recent_goal_rate * 0.4) * df["goals_index"].fillna(0.5) * 2
        gp = gp.clip(0, 1.5)
        ce = gp * goal_pts_pos
        # Assist prob
        ap = (assists_mode[mode].fillna(0) * 0.5 + big_chances_mode[mode].fillna(0) * 0.5) * df["goals_index"].fillna(0.5)
        ap = ap.clip(0, 1.0)
        ce = ce + ap * 3
        # PoM bonus
        ce = ce + (pot_m * 0.5).clip(0, 2)
        # Form multiplier
        ce = ce * form_mult
        ceiling_modes[mode] = ce.fillna(0).clip(0, 30)

    # ─── D Differential (sigmoid gate at 5% × SB-track-record multiplier) ────
    pct_sel = df["percent_selected"].fillna(50.0).astype(float)
    sb_prob = 1 / (1 + np.exp((pct_sel - 5.0) / 1.0))  # sigmoid around 5%
    sb_total = df["sb_total"].fillna(0).astype(float).clip(0, 5)
    sb_mult = 1 + 0.2 * sb_total
    differential_pts = sb_prob * 2 * sb_mult
    differential_pts = differential_pts + np.where(df["one_to_watch"].fillna(False), 0.3, 0.0)

    # ─── Composite EV per mode ───────────────────────────────────────────────
    out_rows = []
    base_cols = ["fantasy_player_id", "fifa_player_id", "nation_id",
                 "opponent_nation_id", "is_home", "fantasy_match_id",
                 "espn_match_id", "position", "price", "percent_selected",
                 "first_name", "last_name", "known_name", "is_active",
                 "one_to_watch", "injury", "sb_total", "form",
                 "avg_points", "total_points", "last_round_points",
                 "goals_index", "team_cs_index", "opp_cs_index",
                 "nation_strength_delta", "moneyline_lopsidedness",
                 "trend_top_category", "trend_top_pct", "trend_top_confidence",
                 "fixture_shape", "mkt_composite_divergence",
                 "weather_cluster", "stage"]
    base = df[[c for c in base_cols if c in df.columns]].copy()

    for mode in ["p90", "per_app", "totals"]:
        base[f"floor_{mode}"] = floor_modes[mode]
        base[f"ceiling_{mode}"] = ceiling_modes[mode]
        base[f"ev_raw_{mode}"] = floor_modes[mode] + ceiling_modes[mode] + differential_pts

    base["differential"] = differential_pts
    base["start_prob"] = start_prob
    base["round_id"] = round_id

    return base


# ─── Section B-brackets: Player Strength Score (K's bracket model) ───────────
# Replaces the additive Floor/Ceiling/Differential composition. Emits 5
# brackets per player, with the fixture quality acting MULTIPLICATIVELY (B5)
# so easy-fixture players dominate naturally regardless of raw talent.
#
# EV = (w1·B1 + w2·B2 + w3·B3 + w4·B4) × B5
#
# Each bracket is a 0-1 normalized sub-score (rank-percentile within position
# so cross-position comparison is meaningful). B5 is a multiplier 0.2-1.8.

# Position-routed stat lists for B2 (WC performance rating). Each stat is
# converted to per-90 then rank-percentiled within position. Discipline
# stats are inverse-percentiled (low = good).
B2_STATS = {
    "FWD": {
        "positive": [
            "fifa_wc_AttemptAtGoalOnTarget", "fifa_wc_AttemptAtGoal", "fifa_wc_XG",
            "fifa_wc_NumberOfShotEndingSequences", "fifa_wc_TakeOnsCompleted",
            "fifa_wc_SpeedRuns", "fifa_wc_attacking_reception_pct",
            "fotmob_wc_touches_opp_box", "fifa_wc_Penalties",
            "fifa_wc_Goals",
        ],
        "fdh_score": "avg_attacking_score",
    },
    "MID": {
        "positive": [
            "fifa_wc_Assists", "fifa_wc_PassesCompleted", "fifa_wc_pass_completion_pct",
            "fifa_wc_CompletedBallProgressions", "fifa_wc_ball_progression_completion_pct",
            "fifa_wc_CompletedSwitchesOfPlay", "fifa_wc_LinebreaksAttempted",
            "fotmob_wc_chances_created", "fotmob_wc_big_chances_created",
            "fifa_wc_NumberOfInvolvements",
        ],
        "fdh_score": "avg_creativity_score",
    },
    "DEF": {
        "positive": [
            "fifa_wc_DefensivePressuresApplied", "fifa_wc_ForcedTurnovers",
            "fotmob_wc_tackles", "fotmob_wc_duels_won_pct",
            "fifa_wc_CleanSheets", "fifa_wc_CrossesCompleted",
            "fifa_wc_LinebreaksCompletedUnderPressure",
        ],
        "fdh_score": "avg_defensive_score",
    },
    "GK": {
        "positive": [
            "fifa_wc_GoalkeeperSaves", "fifa_wc_CleanSheets",
            "fifa_wc_DistributionsCompletedUnderPressure",
        ],
        "fdh_score": "avg_defending_the_goal_score",
    },
}

DISCIPLINE_STATS = ["fifa_wc_YellowCards", "fifa_wc_RedCards", "fifa_wc_FoulsAgainst"]

# Default bracket weights (sums to 1.0). Strategies override these.
DEFAULT_BRACKET_WEIGHTS = {
    "w_b1_overall": 0.20,
    "w_b2_wc_perf": 0.30,
    "w_b3_external": 0.20,
    "w_b4_fantasy": 0.30,
}

# ─── Live %selected refresh ─────────────────────────────────────────────────


def refresh_live_percent_selected(force: bool = True) -> dict[int, float]:
    """Pull the LIVE FIFA Fantasy players endpoint and return a map of
    fantasy_player_id → percent_selected. Same source the PWA edge-proxies.

    Falls back to {} if the fetch fails — the caller can then keep the
    snapshot value from fantasy_players.parquet.

    `force=True` (default) bypasses any cached copy so the model sees the
    same %selected the PWA's live overlay sees at this tick.
    """
    try:
        from lib import io as wh_io  # local import — avoids notebook-time hard-dep
        data = wh_io.cache_raw(
            "https://play.fifa.com/json/fantasy/players.json",
            source="fifa_fantasy",
            name="players_live_for_recommender",
            as_json=True,
            force_refresh=force,
            max_age_days=0,
        )
    except Exception as exc:  # network down, FIFA paywall, etc.
        print(f"[live_pct] fetch failed, falling back to snapshot: {exc}")
        return {}

    rows = data.get("value", {}).get("playerList", []) if isinstance(data, dict) else []
    if not rows:
        # Some payload shapes vary — try the bare top-level list
        rows = data if isinstance(data, list) else []
    out: dict[int, float] = {}
    for r in rows:
        pid = r.get("id") if isinstance(r, dict) else None
        pct = r.get("percentSelected") if isinstance(r, dict) else None
        if pid is not None and pct is not None:
            try:
                out[int(pid)] = float(pct)
            except (TypeError, ValueError):
                continue
    return out


# ─── MODEL REGISTRY ──────────────────────────────────────────────────────────
# Three independent scoring philosophies. Each consumes the same per-(player,
# fixture) bracket dataframe from score_players_brackets, then re-weights the
# brackets + applies model-specific post-boost components to produce ev_model.
#
# - Model 1 BANKER       — current default. Consistency + ownership + SB.
# - Model 2 FORM HUNTER  — heavy on recency: recent5 goals/started_pct/POM,
#                          last_round_points, fotmob recent rating.
# - Model 3 STAT MAXIMIZER — pure FIFA stats + powerrank. Zero ownership lean.

MODEL_REGISTRY = {
    "m1_banker": {
        "name": "Banker",
        "blurb": "Floor-heavy. Consistency, average points, SB track record. Premium picks on favored fixtures. The default top-decile path on average rounds.",
        "weights": {"w_b1_overall": 0.20, "w_b2_wc_perf": 0.25,
                    "w_b3_external": 0.20, "w_b4_fantasy": 0.35},
        "post_boosts": [],          # use brackets as-is
        "fixture_amplifier": 1.0,    # full B5 amplification
        "sb_quota": 9,               # 9 of 15 from <5% band
        # Captain selector: default — max ev_live across MID/FWD.
        "captain_selector": "ev_live",
    },
    "m2_form_hunter": {
        "name": "Form Hunter",
        "blurb": "Recency-weighted. Recent goals, started-pct, fotmob rating, last-round explosions. Punishes cold streaks even on strong fixtures.",
        # 2026-06-27: rebalanced to diversify from M1 top picks. Reduced B3
        # (which biases toward globally-top-rated megastars like Gakpo/Messi
        # equally for every model) and B4 (form is already in the post-boost
        # below — double-counting was forcing the same elite players to the
        # top). Bumped post-boost weight so recent-form-leader surfaces above
        # consistent giants.
        "weights": {"w_b1_overall": 0.10, "w_b2_wc_perf": 0.25,
                    "w_b3_external": 0.25, "w_b4_fantasy": 0.40},
        "post_boosts": [
            {"name": "recent_form_streak", "weight": 0.45,
             "components": [
                 ("recent5_goals", "rank_pct"),
                 ("recent10_goals", "rank_pct"),
                 ("recent5_player_of_the_match", "rank_pct"),
                 ("recent5_started_pct", "rank_pct"),
                 ("last_round_points", "rank_pct"),
             ]},
        ],
        "fixture_amplifier": 0.85,
        "sb_quota": 6,
        # Captain selector: pick MID/FWD with highest recent form, not raw EV.
        # Falls back to ev_live if the post-boost column isn't present.
        "captain_selector": "recent_form_streak",
    },
    "m3_stat_max": {
        "name": "Stat Maximizer",
        "blurb": "Pure FIFA-stats. Powerrank + position-routed per-90 + creativity + duels. Ignores ownership entirely — picks the best player regardless of crowd.",
        # 2026-06-27: rebalanced to surface RAW-STAT leaders above
        # globally-rated megastars. Dropped B3 (external observers' rating
        # already bakes in reputation; stat-max should ignore reputation)
        # and bumped both post-boosts so creativity + powerrank-pure leaders
        # can overtake the all-rounders.
        "weights": {"w_b1_overall": 0.15, "w_b2_wc_perf": 0.55,
                    "w_b3_external": 0.15, "w_b4_fantasy": 0.15},
        "post_boosts": [
            {"name": "creativity_engine", "weight": 0.30,
             "components": [
                 ("fotmob_wc_chances_created", "per90_rank_pct"),
                 ("fotmob_wc_big_chances_created", "per90_rank_pct"),
                 ("fotmob_wc_touches_opp_box", "per90_rank_pct"),
                 ("fifa_wc_CompletedBallProgressions", "per90_rank_pct"),
                 ("fifa_wc_LinebreaksAttempted", "per90_rank_pct"),
                 ("fifa_wc_NumberOfInvolvements", "per90_rank_pct"),
             ]},
            {"name": "powerrank_pure", "weight": 0.30,
             "components": [
                 ("avg_attacking_score", "rank_pct"),
                 ("avg_defensive_score", "rank_pct"),
                 ("avg_creativity_score", "rank_pct"),
                 ("avg_defending_the_goal_score", "rank_pct"),
             ]},
        ],
        "fixture_amplifier": 1.0,
        "sb_quota": 3,
        # Captain selector: pick MID/FWD with highest creativity post-boost.
        "captain_selector": "creativity_engine",
    },
    "m4_sb_hunter": {
        "name": "SB Hunter",
        "blurb": "K's rule. 12 sub-5% picks for the +2 Scouting Bonus event + 3 anchors: 2 sure-shot FWDs (xG × goals_index) + 1 most-influential MID (creativity + B2). Highest variance, highest rank-leap upside.",
        # Same scoring engine as Banker (we want the *strongest* differential
        # candidates by EV — not the most differential by ownership only).
        "weights": {"w_b1_overall": 0.20, "w_b2_wc_perf": 0.30,
                    "w_b3_external": 0.20, "w_b4_fantasy": 0.30},
        "post_boosts": [
            # Surface the anchors via dedicated sub-scores; the custom assembler
            # reads `boost_sure_shot_fwd` and `boost_influential_mid` directly.
            {"name": "sure_shot_fwd", "weight": 0.0,
             "components": [
                 ("fifa_wc_XG", "per90_rank_pct"),
                 ("fifa_wc_AttemptAtGoalOnTarget", "per90_rank_pct"),
                 ("recent5_goals", "rank_pct"),
                 ("recent10_goals", "rank_pct"),
             ]},
            {"name": "influential_mid", "weight": 0.0,
             "components": [
                 ("fotmob_wc_chances_created", "per90_rank_pct"),
                 ("fotmob_wc_big_chances_created", "per90_rank_pct"),
                 ("fifa_wc_CompletedBallProgressions", "per90_rank_pct"),
                 ("avg_creativity_score", "rank_pct"),
                 ("fifa_wc_NumberOfInvolvements", "per90_rank_pct"),
             ]},
        ],
        "fixture_amplifier": 1.0,
        "sb_quota": 12,
        # Custom assembler tag — orchestrator routes m4 to assemble_sb_hunter_squad
        "assembler": "sb_hunter",
        # Captain selector: pick FWD with highest sure-shot-fwd post-boost
        # (or influential MID if no FWD in XI).
        "captain_selector": "sure_shot_fwd",
    },
}


def _apply_model_boosts(brackets_df: pd.DataFrame, raw_df: pd.DataFrame,
                         model_cfg: dict) -> pd.DataFrame:
    """Apply a model's bracket re-weighting + post-boost components to produce
    ev_model. brackets_df has b1..b5 + bracket_sum + ev_bracket from
    score_players_brackets; raw_df has the underlying player attributes the
    post-boost components reference."""
    df = brackets_df.copy()
    w = model_cfg["weights"]

    # Re-mix bracket_sum with model weights
    bracket_sum_model = (
        w["w_b1_overall"] * df["b1_overall"]
        + w["w_b2_wc_perf"] * df["b2_wc_perf"]
        + w["w_b3_external"] * df["b3_external"]
        + w["w_b4_fantasy"] * df["b4_fantasy"]
    )

    # Post-boost sub-scores (computed from raw_df, joined back by player_id)
    minutes = raw_df.get("fifa_wc_TimePlayed", pd.Series(0, index=raw_df.index)).fillna(0)
    post_total = pd.Series(0.0, index=df.index)
    post_weight_sum = 0.0
    boost_breakdown = {}
    raw_lookup = raw_df.set_index("fantasy_player_id")

    for boost in model_cfg.get("post_boosts", []):
        comp_scores = []
        for col, mode in boost["components"]:
            if col not in raw_df.columns:
                continue
            raw_vals = raw_df.set_index("fantasy_player_id")[col]
            if mode == "rank_pct":
                vals = _rank_pct(raw_vals.fillna(0))
            elif mode == "per90_rank_pct":
                raw_minutes = raw_df.set_index("fantasy_player_id").get(
                    "fifa_wc_TimePlayed", pd.Series(0, index=raw_vals.index)
                ).fillna(0)
                p90 = _per90(raw_vals.fillna(0), raw_minutes)
                vals = _rank_pct(p90)
            else:
                vals = _rank_pct(raw_vals.fillna(0))
            comp_scores.append(vals)
        if not comp_scores:
            continue
        boost_score = sum(comp_scores) / len(comp_scores)
        # Map boost_score (indexed by fantasy_player_id) onto df rows
        mapped = df["fantasy_player_id"].map(boost_score.to_dict()).fillna(0.5)
        post_total = post_total + boost["weight"] * mapped
        post_weight_sum += boost["weight"]
        boost_breakdown[boost["name"]] = mapped.round(3)

    # Total score: bracket sum (normalized) + post boosts (additive)
    if post_weight_sum > 0:
        composite = (bracket_sum_model + post_total) / (1.0 + post_weight_sum)
    else:
        composite = bracket_sum_model

    # Fixture amplifier — Form Hunter dampens it slightly; others full
    fix_amp = model_cfg["fixture_amplifier"]
    effective_b5 = 1.0 + (df["b5_fixture_mult"] - 1.0) * fix_amp

    ev_model = (composite * 6.5 * effective_b5).round(2)

    df["bracket_sum_model"] = bracket_sum_model.round(3)
    df["post_boost_score"] = post_total.round(3)
    df["ev_model"] = ev_model
    for name, vals in boost_breakdown.items():
        df[f"boost_{name}"] = vals
    return df


def score_for_model(round_id: int, fixture_profiles: pd.DataFrame,
                     model_id: str,
                     live_pct_selected: dict[int, float] | None = None) -> pd.DataFrame:
    """Score players under a specific model. Returns the bracket dataframe with
    ev_model + post-boost breakdowns added.

    Pass live_pct_selected to override the snapshot ownership at scoring time.
    """
    if model_id not in MODEL_REGISTRY:
        raise ValueError(f"unknown model_id {model_id!r}; known={list(MODEL_REGISTRY)}")
    cfg = MODEL_REGISTRY[model_id]
    # Run bracket scorer with this model's bracket weights (so the existing
    # ev_bracket column also reflects the model — for back-compat).
    brackets = score_players_brackets(round_id, fixture_profiles,
                                       bracket_weights=cfg["weights"],
                                       live_pct_selected=live_pct_selected)

    # Need raw player attributes for post-boost components — load once.
    raw = pd.read_parquet(PROC / "wc26_stg_players_view.parquet")
    raw = raw[[c for c in raw.columns if c not in {"position", "nation_id", "name"}]]
    fp = pd.read_parquet(PROC / "fantasy_players.parquet")[
        ["fantasy_player_id", "fifa_player_id", "last_round_points"]
    ]
    raw = fp.merge(raw, on="fifa_player_id", how="left")
    pr = pd.read_parquet(PROC / "wc26_stg_player_powerrank.parquet")[
        ["fifa_player_id", "avg_attacking_score", "avg_defensive_score",
         "avg_creativity_score", "avg_defending_the_goal_score"]
    ]
    raw = raw.merge(pr, on="fifa_player_id", how="left")

    return _apply_model_boosts(brackets, raw, cfg)


def build_joint_picks(model_outputs: dict, top_n: int = 30) -> dict:
    """Across all 3 model outputs, build:
      - consensus: players in the top-N of ≥2 models (high-confidence picks)
      - surprises: players in the top-N of exactly 1 model (model-specific reads)
      - per-position-top: top 5 per position by max(ev_model) across models
    Returns a dict suitable for JSON emit.
    """
    if not model_outputs:
        return {"consensus": [], "surprises": {}, "per_position_top": {}}

    # Per-model top-N sets
    top_sets = {}
    for mid, df in model_outputs.items():
        top_sets[mid] = set(
            df.sort_values("ev_model", ascending=False).head(top_n)["fantasy_player_id"]
        )

    all_pids = set()
    for s in top_sets.values():
        all_pids |= s

    # Build a per-player aggregate row from any model (they share identity cols)
    sample_df = next(iter(model_outputs.values()))
    cols_id = ["fantasy_player_id", "fifa_player_id", "known_name", "nation_id",
               "opponent_nation_id", "position", "price", "percent_selected",
               "fixture_shape"]
    cols_id = [c for c in cols_id if c in sample_df.columns]

    consensus_rows = []
    surprise_rows = {mid: [] for mid in model_outputs}

    for pid in all_pids:
        in_models = [mid for mid, s in top_sets.items() if pid in s]
        # Collect ev across models
        evs = {}
        identity = None
        for mid, df in model_outputs.items():
            row = df[df["fantasy_player_id"] == pid]
            if row.empty:
                continue
            evs[mid] = float(row.iloc[0]["ev_model"])
            if identity is None:
                identity = {c: row.iloc[0][c] for c in cols_id}
        if identity is None:
            continue
        identity["evs"] = {mid: round(v, 2) for mid, v in evs.items()}
        identity["max_ev"] = round(max(evs.values()), 2) if evs else 0.0
        identity["models_in_top"] = in_models

        if len(in_models) >= 2:
            consensus_rows.append(identity)
        elif len(in_models) == 1:
            surprise_rows[in_models[0]].append(identity)

    consensus_rows.sort(key=lambda r: -r["max_ev"])
    for mid in surprise_rows:
        surprise_rows[mid].sort(key=lambda r: -r["max_ev"])

    # Per-position top — sized to match the PWA Suggested Picks cap (50 total).
    # PWA renders min(emitted, POS_CAP) where POS_CAP is {GK:5, DEF:15, MID:15,
    # FWD:15}. Crucially this list is built from the FULL scored frames, not
    # restricted to the top-30 union above — otherwise positions starved of
    # top-30 candidates (e.g. zero GKs in any model's top 30) can never fill.
    # That bug was producing "12 FWD / 5 DEF / 0 GK" on the PWA.
    #
    # Algo per position:
    #   1. Concatenate every model's rows for that position.
    #   2. For each (fantasy_player_id), keep the row with the highest
    #      ev_model — that becomes ev_max + ev_max_model.
    #   3. Sort by ev_max DESC and take top N.
    per_pos_top = {}
    pos_n = {"GK": 5, "DEF": 15, "MID": 15, "FWD": 15}
    for pos, n in pos_n.items():
        # Stack per-position slices from every model, tagged with model_id
        frames = []
        for mid, df in model_outputs.items():
            sub = df[df["position"] == pos][[
                "fantasy_player_id", "ev_model"
            ]].copy()
            sub["model_id"] = mid
            frames.append(sub)
        if not frames:
            per_pos_top[pos] = []
            continue
        stacked = pd.concat(frames, ignore_index=True)
        # Best (ev, model) per fantasy_player_id
        best = stacked.sort_values("ev_model", ascending=False).drop_duplicates(
            subset="fantasy_player_id", keep="first"
        )
        best = best.head(n)
        # Re-join the identity columns from the model that owned the best row
        rows_out = []
        for i, br in enumerate(best.itertuples(index=False)):
            pid = int(br.fantasy_player_id)
            mid = br.model_id
            ev = float(br.ev_model)
            src = model_outputs[mid]
            row = src[src["fantasy_player_id"] == pid].iloc[0]
            rows_out.append({
                "rank": i + 1,
                "fantasy_player_id": pid,
                "fifa_player_id": int(row["fifa_player_id"]) if pd.notna(row.get("fifa_player_id")) else None,
                "known_name": row.get("known_name"),
                "nation_id": row.get("nation_id"),
                "opponent_nation_id": row.get("opponent_nation_id"),
                "position": pos,
                "price": float(row.get("price") or 0),
                "percent_selected": float(row.get("percent_selected") or 0),
                "ev_max": ev,
                "ev_max_model": mid,
                "fixture_shape": row.get("fixture_shape"),
            })
        per_pos_top[pos] = rows_out

    return {
        "consensus": consensus_rows[:30],
        "surprises": surprise_rows,
        "per_position_top": per_pos_top,
        "top_n_threshold": top_n,
    }


def _rank_pct(s: pd.Series, ascending: bool = True) -> pd.Series:
    """Convert a numeric series to a 0-1 rank percentile. NaN -> 0.5 (median)."""
    return s.rank(pct=True, ascending=ascending, na_option="keep").fillna(0.5)


def _per90(stat: pd.Series, minutes: pd.Series, prior_per90: float = None) -> pd.Series:
    """Per-90 with Bayesian shrinkage for low-minute samples.
    Players with <90 min get pulled toward the position prior so they don't
    dominate via tiny-sample-rate artifacts (one shot in 30 mins != elite finisher).
    """
    minutes = minutes.fillna(0).astype(float)
    stat = stat.fillna(0).astype(float)
    if prior_per90 is None:
        # Use overall median per-90 as prior for any-data players
        with np.errstate(divide="ignore", invalid="ignore"):
            valid_per90 = stat[minutes >= 90] / (minutes[minutes >= 90] / 90)
        prior_per90 = float(valid_per90.median()) if len(valid_per90) and pd.notna(valid_per90.median()) else 0.0
    # Bayesian: (stat + prior_per90 * 90 * shrinkage_weight) / (minutes + 90 * shrinkage_weight)
    # shrinkage_weight = 0.5 means we add 45 min of prior to every player
    shrink_min = 45.0
    return (stat + prior_per90 * shrink_min / 90) / ((minutes + shrink_min) / 90)


def score_players_brackets(round_id: int, fixture_profiles: pd.DataFrame,
                            bracket_weights: dict = None,
                            live_pct_selected: dict[int, float] | None = None) -> pd.DataFrame:
    """Score players using the 5-bracket Player Strength Score model.

    Args:
      live_pct_selected: optional override map {fantasy_player_id → percent_selected}
        from the live FIFA endpoint. If provided, replaces the snapshot value
        so the differential / SB scoring sees fresh ownership.

    Returns one row per (player, fixture) with:
      b1_overall, b2_wc_perf, b3_external, b4_fantasy  → 0-1 sub-scores
      b5_fixture_mult                                  → 0.2-1.8 multiplier
      ev_bracket                                       → composite EV
      Plus all identity / fixture columns from the prior pipeline.
    """
    if bracket_weights is None:
        bracket_weights = DEFAULT_BRACKET_WEIGHTS

    # ── Load player universe with full stg_players_view ─────────────────────
    fp = pd.read_parquet(PROC / "fantasy_players.parquet")[
        ["fantasy_player_id", "fifa_player_id", "fantasy_squad_id", "position",
         "price", "percent_selected", "form", "total_points", "avg_points",
         "last_round_points", "first_name", "last_name", "known_name",
         "is_active", "round_points_json"]
    ]
    # Live %selected override — replaces snapshot value with the freshest
    # ownership from the FIFA Fantasy endpoint. Falls back to snapshot for
    # players the live payload doesn't include.
    if live_pct_selected:
        fp["percent_selected"] = fp.apply(
            lambda r: live_pct_selected.get(int(r["fantasy_player_id"]), r["percent_selected"]),
            axis=1,
        )
    fp["one_to_watch"] = False
    fp["injury"] = None
    sq = pd.read_parquet(PROC / "fantasy_squads.parquet")[
        ["fantasy_squad_id", "abbr", "name"]
    ].rename(columns={"abbr": "nation_id", "name": "nation_name"})
    fp = fp.merge(sq, on="fantasy_squad_id", how="left")

    pv = pd.read_parquet(PROC / "wc26_stg_players_view.parquet")
    # Drop collisions
    pv = pv[[c for c in pv.columns if c not in {"position", "nation_id", "name"}]]
    fp = fp.merge(pv, on="fifa_player_id", how="left")

    # Power-rank scores
    pr = pd.read_parquet(PROC / "wc26_stg_player_powerrank.parquet")[
        ["fifa_player_id", "avg_attacking_score", "avg_defensive_score",
         "avg_creativity_score", "avg_defending_the_goal_score"]
    ]
    fp = fp.merge(pr, on="fifa_player_id", how="left")

    # SB totals
    ft = pd.read_parquet(PROC / "wc26_stg_fantasy_player_totals.parquet")[
        ["fantasy_player_id", "scouting_bonus", "tackles", "chances_created",
         "shots_on_target"]
    ].rename(columns={"scouting_bonus": "sb_total"})
    fp = fp.merge(ft, on="fantasy_player_id", how="left")

    # ── Cross-join with fixture profiles (home + away rows) ─────────────────
    fx = fixture_profiles.copy()
    fx_h = fx.copy()
    fx_h["nation_id"] = fx_h["home_nation_id"]
    fx_h["is_home"] = True
    fx_h["opponent_nation_id"] = fx_h["away_nation_id"]
    fx_h["team_cs_index"] = fx_h["cs_index_home"]
    fx_h["opp_cs_index"] = fx_h["cs_index_away"]
    fx_h["team_strength"] = fx_h["home_strength"]
    fx_h["opp_strength"] = fx_h["away_strength"]
    fx_h["heavy_hitter_team"] = fx_h.get("heavy_hitter_home", 0.5)
    fx_h["heavy_hitter_opp"] = fx_h.get("heavy_hitter_away", 0.5)
    fx_a = fx.copy()
    fx_a["nation_id"] = fx_a["away_nation_id"]
    fx_a["is_home"] = False
    fx_a["opponent_nation_id"] = fx_a["home_nation_id"]
    fx_a["team_cs_index"] = fx_a["cs_index_away"]
    fx_a["opp_cs_index"] = fx_a["cs_index_home"]
    fx_a["team_strength"] = fx_a["away_strength"]
    fx_a["opp_strength"] = fx_a["home_strength"]
    fx_a["heavy_hitter_team"] = fx_a.get("heavy_hitter_away", 0.5)
    fx_a["heavy_hitter_opp"] = fx_a.get("heavy_hitter_home", 0.5)
    fx_long = pd.concat([fx_h, fx_a], ignore_index=True)
    fx_cols = ["fantasy_match_id", "espn_match_id", "fifa_match_id",
               "nation_id", "opponent_nation_id", "is_home",
               "goals_index", "team_cs_index", "opp_cs_index",
               "p_home_win", "p_away_win", "p_draw", "p_btts",
               "nation_strength_delta", "team_strength", "opp_strength",
               "heavy_hitter_team", "heavy_hitter_opp",
               "weather_cluster", "weather_draw_boost", "fifa_referee_id",
               "trend_top_category", "trend_top_pct", "trend_top_confidence",
               "fixture_shape", "mkt_composite_divergence",
               "moneyline_lopsidedness", "stage", "espn_venue_name",
               "roof_type", "surface", "temperature_c", "humidity_pct"]
    fx_long = fx_long[[c for c in fx_cols if c in fx_long.columns]]

    df = fp.merge(fx_long, on="nation_id", how="inner")

    # Drop inactive players AFTER the merge so the cross-join is full
    df = df[df["is_active"].fillna(True) == True].reset_index(drop=True)

    minutes = df["fifa_wc_TimePlayed"].fillna(0).astype(float)

    # ═══════ B1 PlayerOverall ═══════════════════════════════════════════════
    # Composed within position: nation strength + club season + national team
    # career + market value. Position-aware via club_senior_goals routing
    # (FWD/MID weighted on goals; DEF/GK weighted on appearances).
    ns = nation_strength_composite()
    ns_lookup = ns.set_index("nation_id")["nation_total_strength"].to_dict()
    nation_strength = df["nation_id"].map(ns_lookup).fillna(0.5)

    club_apps = df.get("club_senior_appearances", pd.Series(0, index=df.index)).fillna(0)
    club_rating = df.get("club_senior_weighted_avg_rating", pd.Series(6.5, index=df.index)).fillna(6.5)
    club_goals = df.get("club_senior_goals", pd.Series(0, index=df.index)).fillna(0)
    club_goals_per_app = club_goals / club_apps.replace(0, np.nan)
    nat_apps = df.get("national_senior_appearances", pd.Series(0, index=df.index)).fillna(0)
    nat_goals = df.get("national_senior_goals", pd.Series(0, index=df.index)).fillna(0)
    nat_rating = df.get("national_senior_weighted_avg_rating", pd.Series(6.5, index=df.index)).fillna(6.5)
    value = df.get("value_fotmob_latest_eur", pd.Series(1e6, index=df.index)).fillna(1e6)
    log_value = np.log1p(value)

    b1_components = pd.DataFrame({
        "nation_strength": nation_strength,
        "club_rating_pct": _rank_pct(club_rating),
        "club_goals_per_app_pct": _rank_pct(club_goals_per_app),
        "club_apps_pct": _rank_pct(club_apps),
        "national_rating_pct": _rank_pct(nat_rating),
        "national_goals_pct": _rank_pct(nat_goals / nat_apps.replace(0, np.nan)),
        "log_value_pct": _rank_pct(log_value),
    })
    # Position-conditional weighting
    pos = df["position"].fillna("MID")
    b1_w_attack = pos.isin(["FWD", "MID"]).astype(float)
    b1 = (
        0.20 * b1_components["nation_strength"]
        + 0.15 * b1_components["club_rating_pct"]
        + 0.15 * b1_components["club_goals_per_app_pct"] * b1_w_attack
        + 0.10 * b1_components["club_apps_pct"]
        + 0.10 * b1_components["national_rating_pct"]
        + 0.15 * b1_components["national_goals_pct"] * b1_w_attack
        + 0.15 * b1_components["log_value_pct"]
    )
    # Renormalize because attack-only components sometimes contribute 0 (DEF/GK)
    b1 = (b1 / b1.max()).clip(0, 1) if b1.max() > 0 else b1

    # ═══════ B2 WCPerfRating ════════════════════════════════════════════════
    # Position-routed per-90 stats from fifa_wc_* + fotmob_wc_* + FDH power-rank.
    # Bayesian shrinkage for low-minute samples.
    b2 = pd.Series(0.5, index=df.index)
    for pos_label, spec in B2_STATS.items():
        mask = (pos == pos_label)
        if mask.sum() == 0:
            continue
        sub_idx = df[mask].index
        sub_minutes = minutes.loc[sub_idx]
        pos_score = pd.Series(0.0, index=sub_idx)
        n_stats = 0
        # Positive stats: each is per-90 then rank-percentile within position
        for col in spec["positive"]:
            if col not in df.columns:
                continue
            stat_vals = df[col].loc[sub_idx].fillna(0).astype(float)
            if col.endswith("_pct"):
                # percent stats already 0-100, use rank directly
                pct = _rank_pct(stat_vals)
            else:
                p90 = _per90(stat_vals, sub_minutes)
                pct = _rank_pct(p90)
            pos_score = pos_score + pct
            n_stats += 1
        if n_stats > 0:
            pos_score = pos_score / n_stats
        # Add FDH power-rank score (already 0-1ish), weighted ~30%
        fdh_col = spec["fdh_score"]
        if fdh_col in df.columns:
            fdh_pct = _rank_pct(df[fdh_col].loc[sub_idx])
            pos_score = 0.7 * pos_score + 0.3 * fdh_pct
        b2.loc[sub_idx] = pos_score
    # Discipline penalty: subtract 10% of inverse-disciplined percentile
    disc_score = pd.Series(0.0, index=df.index)
    for col in DISCIPLINE_STATS:
        if col in df.columns:
            p90 = _per90(df[col].fillna(0), minutes)
            disc_score = disc_score + _rank_pct(p90, ascending=False)
    disc_score = disc_score / max(1, len([c for c in DISCIPLINE_STATS if c in df.columns]))
    b2 = (0.9 * b2 + 0.1 * disc_score).clip(0, 1)

    # ═══════ B3 ExternalRatings ═════════════════════════════════════════════
    fotmob_rating = df.get("fotmob_wc_fotmob_rating", pd.Series(6.5, index=df.index)).fillna(6.5)
    fifa_rating = df.get("wc_rating", pd.Series(6.5, index=df.index)).fillna(6.5)
    r5 = df.get("recent5_fotmob_rating", pd.Series(6.5, index=df.index)).fillna(6.5)
    r10 = df.get("recent10_fotmob_rating", pd.Series(6.5, index=df.index)).fillna(6.5)
    r15 = df.get("recent15_fotmob_rating", pd.Series(6.5, index=df.index)).fillna(6.5)
    blended_form = 0.5 * r5 + 0.3 * r10 + 0.2 * r15
    b3 = (
        0.35 * _rank_pct(fotmob_rating)
        + 0.25 * _rank_pct(fifa_rating)
        + 0.40 * _rank_pct(blended_form)
    ).clip(0, 1)

    # ═══════ B4 FantasyMeta ═════════════════════════════════════════════════
    form = df["form"].fillna(df["form"].median()).astype(float)
    avg_pts = df["avg_points"].fillna(0).astype(float)
    total_pts = df["total_points"].fillna(0).astype(float)
    last_round = df["last_round_points"].fillna(0).astype(float)
    sb_tot = df["sb_total"].fillna(0).astype(float)
    pct_sel = df["percent_selected"].fillna(50.0).astype(float)

    # Consistency: 1 - std/mean of round_points_json (high = more consistent)
    def _consistency(j):
        if not j: return 0.5
        try:
            vals = list(json.loads(j).values()) if isinstance(j, str) else list(j.values())
            vals = [v for v in vals if v is not None]
            if len(vals) < 2: return 0.5
            mean = sum(vals) / len(vals)
            if mean <= 0: return 0.0
            import statistics
            std = statistics.stdev(vals)
            return max(0.0, min(1.0, 1 - std / max(mean, 1)))
        except Exception:
            return 0.5
    consistency = df["round_points_json"].apply(_consistency)

    # Differential boost: sigmoid 5% gate × (1 + 0.2×sb_total)
    sb_prob = 1 / (1 + np.exp((pct_sel - 5.0) / 1.0))
    sb_boost = sb_prob * (1 + 0.2 * sb_tot.clip(0, 5))

    b4 = (
        0.25 * _rank_pct(form)
        + 0.25 * _rank_pct(avg_pts)
        + 0.20 * _rank_pct(total_pts)
        + 0.10 * _rank_pct(last_round)
        + 0.10 * _rank_pct(consistency)
        + 0.10 * _rank_pct(sb_boost)
    ).clip(0, 1)

    # ═══════ B5 FixtureMultiplier — THE KEY ════════════════════════════════
    # Position-conditional fixture quality. Composed multiplicatively from §A
    # factors. Range 0.2-1.8. FWD/MID Ceiling-oriented; DEF/GK Floor-oriented.
    goals_idx = df["goals_index"].fillna(0.5).astype(float)
    team_cs = df["team_cs_index"].fillna(0.3).astype(float)
    opp_strength = df["opp_strength"].fillna(0.5).astype(float)
    heavy_hitter = df["heavy_hitter_team"].fillna(0.5).astype(float)
    trend_conf = df["trend_top_confidence"].fillna(0.5).astype(float) if "trend_top_confidence" in df.columns else pd.Series(0.5, index=df.index)
    stage_str = df["stage"].fillna("group").str.lower()
    stage_mult = stage_str.map({
        "group_a": 1.00, "group_b": 1.00, "group_c": 1.00, "group_d": 1.00,
        "group_e": 1.00, "group_f": 1.00, "group_g": 1.00, "group_h": 1.00,
        "group_i": 1.00, "group_j": 1.00, "group_k": 1.00, "group_l": 1.00,
        "group": 1.00, "r32": 1.15, "r16": 1.25, "qf": 1.40, "sf": 1.55, "final": 1.55,
    }).fillna(1.00)

    # Position-conditional fixture quality
    fwd_mid_fix = (
        (0.5 + goals_idx)                  # 0.5-1.5: high-goal fixtures amplify
        * (1.0 + (1 - opp_strength) * 0.3) # weaker opp adds up to 30%
        * (1.0 + heavy_hitter * 0.15)      # clutch teams add 15%
    )
    def_gk_fix = (
        (0.6 + team_cs)                    # 0.6-1.6: high-CS-prob fixtures amplify
        * (1.0 + (1 - opp_strength) * 0.4) # weak opp attack adds 40%
        * (1.0 + heavy_hitter * 0.10)
    )
    base_fix = np.where(pos.isin(["FWD", "MID"]), fwd_mid_fix, def_gk_fix)
    base_fix = pd.Series(base_fix, index=df.index)

    # Modifiers
    weather_drag = pd.Series(0.0, index=df.index)
    if "weather_cluster" in df.columns:
        cluster_drag = df["weather_cluster"].map({0: 0.0, 1: 0.08, 2: 0.12, 3: 0.0}).fillna(0.0)
        weather_drag = cluster_drag
    # Trend confidence: 0.5 baseline maps to 1.0 modifier; high confidence -> 1.15
    trend_mod = 1.0 + (trend_conf - 0.5) * 0.3
    # Mkt-composite divergence: if "market_overconfident" upset opportunity, boost differentials
    mkt_div = df["mkt_composite_divergence"].fillna(0.0).astype(float) if "mkt_composite_divergence" in df.columns else pd.Series(0.0, index=df.index)
    div_mod = 1.0 + mkt_div * 0.10  # up to 10% boost when market disagrees

    b5 = (base_fix * stage_mult * (1 - weather_drag) * trend_mod * div_mod).clip(0.20, 1.80)

    # ═══════ Composite EV ════════════════════════════════════════════════════
    bracket_sum = (
        bracket_weights["w_b1_overall"] * b1
        + bracket_weights["w_b2_wc_perf"] * b2
        + bracket_weights["w_b3_external"] * b3
        + bracket_weights["w_b4_fantasy"] * b4
    )
    # Scale bracket sum to fantasy-points scale. Real per-player round score
    # averages ~3-5 pts; an elite starter on a great fixture scores ~10-15.
    # bracket_sum is 0-1, B5 is 0.20-1.80. Multiplier 6.5 → max EV ≈ 12 with
    # ev_strategy on top (×2 for captain). Tuned so XI projected total
    # lands ~50-80, matching the FIFA Fantasy distribution.
    ev_bracket = bracket_sum * 6.5 * b5

    # ═══════ Output assembly ════════════════════════════════════════════════
    base_cols = ["fantasy_player_id", "fifa_player_id", "nation_id", "nation_name",
                 "opponent_nation_id", "is_home", "fantasy_match_id",
                 "espn_match_id", "position", "price", "percent_selected",
                 "first_name", "last_name", "known_name", "is_active",
                 "one_to_watch", "injury", "sb_total", "form",
                 "avg_points", "total_points", "last_round_points",
                 "goals_index", "team_cs_index", "opp_cs_index",
                 "team_strength", "opp_strength", "heavy_hitter_team",
                 "nation_strength_delta", "moneyline_lopsidedness",
                 "trend_top_category", "trend_top_pct", "trend_top_confidence",
                 "fixture_shape", "mkt_composite_divergence",
                 "weather_cluster", "stage", "roof_type", "surface"]
    base = df[[c for c in base_cols if c in df.columns]].copy()
    base["b1_overall"] = b1.round(3)
    base["b2_wc_perf"] = b2.round(3)
    base["b3_external"] = b3.round(3)
    base["b4_fantasy"] = b4.round(3)
    base["b5_fixture_mult"] = b5.round(3)
    base["bracket_sum"] = bracket_sum.round(3)
    base["ev_bracket"] = ev_bracket.round(2)
    base["round_id"] = round_id

    # ── Backward-compat aliases so the existing assembler + suggestor + chip
    # tagger keep working without a rewrite. Floor ≈ defense-oriented bracket
    # component (B1+B4) × B5; Ceiling ≈ attack-oriented (B2+B3) × B5;
    # Differential = B4's SB-boost sub-component directly. EV mirrors ev_bracket
    # across all 3 mode-suffixed columns since modes are now subsumed by B2's
    # internal per-90 normalization.
    floor_proxy = ((b1 + b4) * 0.5 * 6.5 * b5).clip(0, 18)
    ceiling_proxy = ((b2 + b3) * 0.5 * 6.5 * b5).clip(0, 18)
    base["floor_p90"] = floor_proxy.round(2)
    base["floor_per_app"] = floor_proxy.round(2)
    base["floor_totals"] = floor_proxy.round(2)
    base["ceiling_p90"] = ceiling_proxy.round(2)
    base["ceiling_per_app"] = ceiling_proxy.round(2)
    base["ceiling_totals"] = ceiling_proxy.round(2)
    base["ev_raw_p90"] = ev_bracket.round(2)
    base["ev_raw_per_app"] = ev_bracket.round(2)
    base["ev_raw_totals"] = ev_bracket.round(2)
    # Differential alias from the SB-boost sub-component of B4
    base["differential"] = (sb_boost * 2).round(2)
    base["start_prob"] = df["recent5_started_pct"].fillna(
        df["recent10_started_pct"]).fillna(0.5).clip(0, 1).round(3)

    return base


# ─── Section C.5: Archetype mining v2 (rich FIFA stat) ───────────────────────


def compute_scoring_channels() -> pd.DataFrame:
    """Decompose actual MD1+MD2 fantasy points into channel composition per player.

    Returns one row per fantasy_player_id with pts_from_<channel>_pct columns
    summing to ≤1.0 per player (≤1 because appearance points are excluded —
    those aren't a "play-style" channel).
    """
    rs = pd.read_parquet(PROC / "fantasy_player_round_stats.parquet")
    # Use raw stat columns to estimate points from each channel
    chan_cols = ["goals", "assists", "clean_sheet", "scouting_bonus",
                 "tackles", "chances_created", "shots_on_target",
                 "saves", "yellow_card", "red_card"]
    chan_cols = [c for c in chan_cols if c in rs.columns]
    fp = pd.read_parquet(PROC / "fantasy_players.parquet")[["fantasy_player_id", "position"]]
    rs = rs.merge(fp, on="fantasy_player_id", how="left")

    # Channel point computations using FIFA schema
    rs["pts_goals"] = rs["goals_scored"].fillna(0) * rs["position"].map(GOAL_PTS).fillna(0)
    rs["pts_assists"] = rs.get("assists", 0).fillna(0) * 3
    rs["pts_cs"] = rs.get("clean_sheet", 0).fillna(0) * rs["position"].map(CS_PTS).fillna(0)
    rs["pts_sb"] = rs.get("scouting_bonus", 0).fillna(0) * 2
    rs["pts_tackles"] = np.where(rs["position"] == "MID",
                                 (rs.get("tackles", 0).fillna(0) // 3).astype(float), 0)
    rs["pts_cc"] = np.where(rs["position"] == "MID",
                            (rs.get("chances_created", 0).fillna(0) // 2).astype(float), 0)
    rs["pts_sot"] = np.where(rs["position"] == "FWD",
                             (rs.get("shots_on_target", 0).fillna(0) // 2).astype(float), 0)
    rs["pts_saves"] = np.where(rs["position"] == "GK",
                               (rs.get("saves", 0).fillna(0) // 3).astype(float), 0)

    # Aggregate per player
    chan_pts = ["pts_goals", "pts_assists", "pts_cs", "pts_sb",
                "pts_tackles", "pts_cc", "pts_sot", "pts_saves"]
    agg = rs.groupby("fantasy_player_id")[chan_pts].sum().reset_index()
    agg["total_chan_pts"] = agg[chan_pts].sum(axis=1).replace(0, np.nan)
    for c in chan_pts:
        agg[c.replace("pts_", "pts_from_") + "_pct"] = (agg[c] / agg["total_chan_pts"]).fillna(0)
    return agg[["fantasy_player_id"] + [c.replace("pts_", "pts_from_") + "_pct" for c in chan_pts]]


def mine_archetypes_v2(mode: str = "retrospective") -> dict:
    """Cluster players in the rich FIFA-stat + scoring-channel feature space.

    mode='retrospective': MD1+MD2 top-20% scorers (used for "scored like X" match)
    mode='prospective':   full 1488 pool on pre-tournament profile (used for thin samples)
    """
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import silhouette_score

    # Aggregate FIFA player-match stats per player
    pms = pd.read_parquet(PROC / "wc26_player_match_stats_wide.parquet")
    pms["TimePlayed"] = pms["TimePlayed"].fillna(0)
    pms["denom"] = (pms["TimePlayed"] / 90).replace(0, np.nan)
    p90_stats = ["AttemptAtGoal", "AttemptAtGoalOnTarget", "XG", "Assists",
                 "Crosses", "CrossesCompleted", "PassesCompleted", "Tackles",
                 "DefensivePressuresApplied", "ForcedTurnovers",
                 "Corners", "TotalDistance", "GoalkeeperSaves"]
    p90_stats = [c for c in p90_stats if c in pms.columns]
    for c in p90_stats:
        pms[f"{c}_p90"] = pms[c] / pms["denom"]
    agg_cols = {f"{c}_p90": "mean" for c in p90_stats}
    agg_cols["TimePlayed"] = "sum"
    player_agg = pms.groupby("fifa_player_id").agg(agg_cols).reset_index()

    # Players base (link fifa_player_id ↔ fantasy_player_id)
    fp = pd.read_parquet(PROC / "fantasy_players.parquet")[
        ["fantasy_player_id", "fifa_player_id", "position",
         "price", "percent_selected", "known_name"]
    ]
    pv_cols = [c for c in ["fifa_player_id", "wc_rating"]
               if c in pd.read_parquet(PROC / "wc26_stg_players_view.parquet").columns]
    pv = pd.read_parquet(PROC / "wc26_stg_players_view.parquet")[pv_cols]
    pr = pd.read_parquet(PROC / "wc26_stg_player_powerrank.parquet")[
        ["fifa_player_id", "avg_attacking_score", "avg_defensive_score",
         "avg_creativity_score", "avg_defending_the_goal_score"]
    ]
    feat = fp.merge(player_agg, on="fifa_player_id", how="left")
    feat = feat.merge(pv, on="fifa_player_id", how="left")
    feat = feat.merge(pr, on="fifa_player_id", how="left")

    # Scoring channel composition (only meaningful for retrospective)
    chan = compute_scoring_channels()
    feat = feat.merge(chan, on="fantasy_player_id", how="left")

    if mode == "retrospective":
        # Restrict to MD1+MD2 top-20% scorers
        rs = pd.read_parquet(PROC / "fantasy_player_round_stats.parquet")
        top_thr = rs["points"].quantile(0.80)
        top_ids = rs[rs["points"] >= top_thr]["fantasy_player_id"].unique()
        feat = feat[feat["fantasy_player_id"].isin(top_ids)].copy()
    # else prospective: keep everyone

    # Build feature matrix
    feature_cols = [c for c in feat.columns if c.endswith("_p90") or c.endswith("_pct")
                    or c in ("wc_rating", "avg_attacking_score",
                             "avg_defensive_score", "avg_creativity_score",
                             "price", "percent_selected")]
    X = feat[feature_cols].copy()
    # Drop columns with >40% null
    keep_features = [c for c in feature_cols if X[c].isna().mean() < 0.40]
    X = X[keep_features]
    # Impute with positional median
    for c in keep_features:
        X[c] = X[c].fillna(X[c].median())
    if len(X) < 20:
        return {"archetypes": [], "feature_cols": keep_features, "centroids": [],
                "labels": [], "mode": mode}

    Xs = StandardScaler().fit_transform(X.values)

    best_k, best_score, best_labels = None, -1, None
    for k in [6, 8, 10, 12]:
        if len(Xs) < k * 2:
            continue
        km = KMeans(n_clusters=k, n_init=10, random_state=42)
        labels = km.fit_predict(Xs)
        try:
            sc = silhouette_score(Xs, labels)
        except Exception:
            sc = -1
        if sc > best_score:
            best_k, best_score, best_labels = k, sc, labels

    if best_labels is None:
        return {"archetypes": [], "feature_cols": keep_features, "centroids": [],
                "labels": [], "mode": mode}

    feat = feat.copy()
    feat["cluster"] = best_labels

    # Name each cluster from dominant features + tier
    archetypes = []
    rs_for_pts = pd.read_parquet(PROC / "fantasy_player_round_stats.parquet")
    pts_per_player = rs_for_pts.groupby("fantasy_player_id")["points"].sum().to_dict()

    for c in range(best_k):
        sub = feat[feat["cluster"] == c]
        if sub.empty:
            continue
        # Tier prefix
        mean_price = sub["price"].mean()
        if mean_price >= 9: tier = "ELITE"
        elif mean_price >= 6: tier = "MID_TIER"
        else: tier = "BUDGET"
        # Position majority
        position_mode_val = sub["position"].mode().iloc[0] if not sub["position"].mode().empty else "MID"
        # Dominant channel
        chan_cols_in = [c for c in sub.columns if c.startswith("pts_from_") and c.endswith("_pct")]
        if chan_cols_in:
            chan_means = sub[chan_cols_in].mean()
            top_chan = chan_means.idxmax().replace("pts_from_", "").replace("_pct", "").upper()
        else:
            top_chan = "MIX"
        # Top FIFA stat
        p90_cols_in = [c for c in sub.columns if c.endswith("_p90")]
        if p90_cols_in:
            stat_means = sub[p90_cols_in].mean()
            top_stat = stat_means.idxmax().replace("_p90", "")
        else:
            top_stat = "VOLUME"
        # Differential vs popular
        own_band = "DIFF" if sub["percent_selected"].median() < 5 else "POPULAR"

        name = f"{tier}_{position_mode_val}_{top_chan}_{top_stat}_{own_band}".upper()
        # Exemplars: top-3 by total points
        sub2 = sub.copy()
        sub2["total_pts"] = sub2["fantasy_player_id"].map(pts_per_player).fillna(0)
        exemplars = sub2.nlargest(3, "total_pts")[["known_name", "nation_id", "total_pts"]] \
            if "nation_id" in sub2.columns else sub2.nlargest(3, "total_pts")[["known_name", "total_pts"]]
        exemplars_list = exemplars.to_dict("records")

        centroid = sub[keep_features].mean().to_dict()
        archetypes.append({
            "cluster_id": int(c),
            "name": name,
            "n": int(len(sub)),
            "mean_pts": float(sub2["total_pts"].mean()) if "total_pts" in sub2 else 0,
            "exemplars": exemplars_list,
            "centroid": {k: float(v) for k, v in centroid.items() if pd.notna(v)},
        })

    return {
        "mode": mode,
        "k": int(best_k),
        "silhouette": float(best_score),
        "feature_cols": keep_features,
        "archetypes": archetypes,
        "player_clusters": {int(pid): int(lbl) for pid, lbl in zip(feat["fantasy_player_id"], feat["cluster"])},
    }


def attach_archetypes(scored: pd.DataFrame, retro: dict, prospective: dict) -> pd.DataFrame:
    """Add peer_archetype + peer_similarity for both retro and prospective."""
    df = scored.copy()
    for kind, cat in [("retrospective", retro), ("prospective", prospective)]:
        if not cat or not cat.get("archetypes"):
            df[f"archetype_{kind}"] = None
            df[f"archetype_{kind}_sim"] = np.nan
            df[f"archetype_{kind}_examples"] = None
            continue
        pmap = cat["player_clusters"]
        labels = {a["cluster_id"]: a for a in cat["archetypes"]}
        df[f"archetype_{kind}"] = df["fantasy_player_id"].map(
            lambda pid: labels.get(pmap.get(int(pid))).get("name") if pmap.get(int(pid)) is not None else None
        )
        df[f"archetype_{kind}_examples"] = df["fantasy_player_id"].map(
            lambda pid: labels.get(pmap.get(int(pid))).get("exemplars") if pmap.get(int(pid)) is not None else None
        )
        df[f"archetype_{kind}_sim"] = np.where(df[f"archetype_{kind}"].notna(), 0.8, np.nan)
    return df


# ─── Section E: Filters and anti-pick ────────────────────────────────────────


def apply_filters(scored: pd.DataFrame) -> pd.DataFrame:
    """E1-E4: drop injured, inactive, or no-data players."""
    df = scored.copy()
    before = len(df)
    df = df[df["is_active"].fillna(True)]
    df = df[df["injury"].isna() | (df["injury"] == "")]
    after = len(df)
    df["dropped_by_filter_count"] = before - after
    return df


def tag_anti_picks(scored: pd.DataFrame) -> pd.DataFrame:
    """D-3: mark anti_pick=True for 2nd-tier picks on opponent side in consensus_tight fixtures."""
    df = scored.copy()
    df["anti_pick"] = False
    tight_mask = df["fixture_shape"] == "consensus_tight"
    # Within each tight fixture × position, the 2nd-3rd ranked players by ev_per_app become anti_picks
    if tight_mask.any():
        tight = df[tight_mask].copy()
        tight["pos_rank"] = tight.groupby(["fantasy_match_id", "nation_id", "position"])["ev_raw_per_app"] \
            .rank(method="dense", ascending=False)
        df.loc[tight.index, "anti_pick"] = (tight["pos_rank"].between(2, 3)).values
    return df


# ─── Section D: Strategy configs + squad assembly ───────────────────────────


# Three default strategies (K's quotas + my aggression mapping).
# sb_quota = minimum number of starting-15 picks with percent_selected < 5.
# Aggression: higher ceiling weight = more variance-tolerant.
STRATEGIES = [
    {
        "id": "s1_balanced_hunter",
        "name": "Balanced Hunter",
        "blurb": "Mid-aggression. 9 of 15 from <5% ownership band — chases SB and contrarian fixture mismatches while keeping a spine of proven scorers.",
        "alpha_floor": 0.40,
        "beta_ceiling": 0.40,
        "gamma_differential": 0.20,
        "sb_quota": 9,
        "fixture_filter": None,
        "anti_pick_allowed": True,
    },
    {
        "id": "s2_steady_banker",
        "name": "Steady Banker",
        "blurb": "Low aggression. Only 5 of 15 from <5% band — the rest are floor-maximizing premium picks on favored fixtures. Cleanest path to a top-10% finish on average rounds.",
        "alpha_floor": 0.65,
        "beta_ceiling": 0.20,
        "gamma_differential": 0.15,
        "sb_quota": 5,
        "fixture_filter": None,
        "anti_pick_allowed": False,
    },
    {
        "id": "s3_differential_max",
        "name": "Differential Maximizer",
        "blurb": "Max aggression. 12 of 15 from <5% band — built for SB +2 hunting and rank-leap rounds. Carries higher variance but every SB-firing player is a multiplier.",
        "alpha_floor": 0.20,
        "beta_ceiling": 0.45,
        "gamma_differential": 0.35,
        "sb_quota": 12,
        "fixture_filter": None,
        "anti_pick_allowed": True,
    },
]

POSITION_QUOTA = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}


def _ev_strategy(scored: pd.DataFrame, strat: dict) -> pd.Series:
    """Compute the ev_strategy column used by the squad assembler.

    Two modes:
      - If strat has `ev_col` set, use that column directly (model-based path:
        the model wrapper passes `ev_col="ev_model"` so the assembler sorts on
        the model's composite score).
      - Otherwise: α·floor + β·ceiling + γ·differential (legacy bracket+strategy
        path that's still used by the older Banker/Steady/Differential triplet).
    """
    if "ev_col" in strat and strat["ev_col"] in scored.columns:
        return scored[strat["ev_col"]].fillna(0).astype(float)
    floor_mean = scored[["floor_p90", "floor_per_app", "floor_totals"]].mean(axis=1)
    ceil_mean = scored[["ceiling_p90", "ceiling_per_app", "ceiling_totals"]].mean(axis=1)
    diff = scored["differential"].fillna(0)
    return (
        strat["alpha_floor"] * floor_mean.fillna(0)
        + strat["beta_ceiling"] * ceil_mean.fillna(0)
        + strat["gamma_differential"] * diff
    )


# ── FIFA WC2026 Fantasy boosters: forward-looking chip plan ────────────────
#
# Source of truth: play.fifa.com/fantasy/help/guidelines (snapshot
# `E:\fantasy_guidelinesfc2.txt` 2026-06-25). PWA mirror:
# src/data/fantasyRules.ts (BOOSTERS list).
#
# Eligibility constraints (from rules):
#   - wildcard: cannot be used MD1 (round 1) or R32 (round 4)
#   - qualification_booster: R32+ (round 4+)
#   - mystery_booster: revealed at R32 lock — usable in any KO round (4..8)
#   - 12th_man, max_captain: any round (1..8)
#
# Heuristic per-model plan — chosen to express each model's "best round to
# fire": Banker plays late once confidence builds; Form Hunter fires Wildcard
# at R16 once mid-tournament form has revealed itself; Stat Max stockpiles
# Max-Captain for the deepest knockouts; SB Hunter fires early because
# differentials peak when the field is widest.
#
# TODO(nb_17): replace with data-driven planning (per-model EV variance per
# round) once a multi-checkpoint history exists.

# 2026-06-27: simplified to 12th Man ONLY across KO rounds. User direction:
# Wildcard / Max-C / Qualification Booster / Mystery / Clean Sheet Shield = all
# unused. Each model fires its 12th Man on its model-specific best KO round so
# the 4 strategies stagger across R32 → R16 → QF → SF. R8 Final is chip-free
# (the squad's depth does the talking at that point).
_CHIP_PLAN_BY_MODEL = {
    "m1_banker":      {"twelfth_man": 4},   # R32: banker plays bench depth early
    "m2_form_hunter": {"twelfth_man": 5},   # R16: form crystallises by knockout 16
    "m3_stat_max":    {"twelfth_man": 6},   # QF:  stat-max sweet spot
    "m4_sb_hunter":   {"twelfth_man": 7},   # SF:  highest-leverage differential round
}
_CHIP_PLAN_DEFAULT = {"twelfth_man": 4}
# Per-chip eligibility — only 12th Man honoured now. Any other chip dropped.
_CHIP_ELIGIBLE = {
    "twelfth_man": lambda r: 1 <= r <= 8,
}


def plan_chips(model_id: str, target_round_id: int) -> dict:
    """Emit a forward-looking chip plan: {chip_id: planned_round_id}.

    Per-model heuristic mapped above. If a chip's preferred round is <
    target_round_id (i.e. the lock already passed without firing it), snap
    to the next eligible round so the plan stays usable. Returns {} if no
    chips are scheduled for or after the target round.
    """
    base = _CHIP_PLAN_BY_MODEL.get(model_id, _CHIP_PLAN_DEFAULT)
    plan = {}
    for chip, round_id in base.items():
        if chip not in _CHIP_ELIGIBLE:
            continue  # disabled chip; skip entirely
        if round_id < target_round_id:
            for r in range(target_round_id, 9):
                if _CHIP_ELIGIBLE[chip](r):
                    round_id = r
                    break
            else:
                continue  # no eligible round left; omit
        plan[chip] = int(round_id)
    return plan


def assemble_strategy_squad(scored: pd.DataFrame, strat: dict,
                             budget_m: float = 100.0,
                             max_per_nation: int = 3,
                             non_budget: bool = False) -> dict:
    """Greedy 15-man squad assembly respecting:
      - 2 GK / 5 DEF / 5 MID / 3 FWD position quota
      - max 3 per nation
      - $100m budget (skipped if non_budget=True)
      - sb_quota: at least N picks with percent_selected < 5
    """
    df = scored.copy()
    df["ev_strategy"] = _ev_strategy(df, strat)

    # Player universe: dedupe to one row per player (this round only emits one
    # fixture per nation, so each player has exactly one row already).
    df = df.sort_values("ev_strategy", ascending=False).reset_index(drop=True)

    quota = POSITION_QUOTA.copy()
    nation_count: dict[str, int] = {}
    sb_count = 0
    spent = 0.0
    picks = []
    picked_ids = set()

    target_sb = strat["sb_quota"]
    # K's intent: SB composition is a TARGET, not a floor. Allow ±1 flex but
    # treat both bands as hard caps so S1/S2/S3 squads differ meaningfully.
    sb_cap = target_sb + 1
    non_sb_cap = (15 - target_sb) + 1
    non_sb_count = 0

    def can_pick(row) -> tuple[bool, str]:
        pos = row["position"]
        if quota.get(pos, 0) <= 0:
            return False, "pos_quota_full"
        nat = row["nation_id"]
        if nation_count.get(nat, 0) >= max_per_nation:
            return False, "nation_cap"
        price = float(row.get("price") or 0)
        if not non_budget and (spent + price) > budget_m:
            return False, "budget"
        if not non_budget:
            remaining_picks = sum(quota.values()) - 1
            if remaining_picks > 0:
                min_remaining_cost = remaining_picks * 4.0
                if (spent + price + min_remaining_cost) > budget_m + 1e-6:
                    return False, "budget_projection"
        return True, "ok"

    # SINGLE PASS — best ev_strategy first, but cap non-SB picks at (15-quota).
    # This keeps premium popular picks (Messi, Haaland, Kane) when they fit
    # within the non-SB budget, while guaranteeing the SB quota.
    for _, row in df.iterrows():
        if sum(quota.values()) == 0:
            break
        if int(row["fantasy_player_id"]) in picked_ids:
            continue
        if row.get("is_active") is False:
            continue

        is_sb = (row.get("percent_selected") or 50) < 5
        if is_sb and sb_count >= sb_cap:
            continue  # SB cap reached
        if not is_sb and non_sb_count >= non_sb_cap:
            continue  # non-SB cap reached

        # Tail guarantee: with remaining slots, can we still hit the SB target?
        remaining_slots = 15 - len(picks)
        sb_still_needed = max(0, target_sb - sb_count)
        if not is_sb and remaining_slots <= sb_still_needed:
            continue  # reserve remaining slots for SB picks

        ok, _ = can_pick(row)
        if not ok:
            continue
        picks.append(row.to_dict())
        picked_ids.add(int(row["fantasy_player_id"]))
        quota[row["position"]] -= 1
        nation_count[row["nation_id"]] = nation_count.get(row["nation_id"], 0) + 1
        spent += float(row.get("price") or 0)
        if is_sb:
            sb_count += 1
        else:
            non_sb_count += 1

    # If SB quota still unmet (e.g. SB-band pool exhausted for some position),
    # report the gap rather than silently failing.
    sb_gap = max(0, target_sb - sb_count)

    # Sort picks by position then ev_strategy desc; assign starting XI vs bench
    pos_order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    picks.sort(key=lambda p: (pos_order[p["position"]], -p["ev_strategy"]))

    # Pick a valid formation: maximize EV under 1 GK + ≥3 DEF + ≥2 MID + ≥1 FWD = 11
    starting = []
    bench = []
    # 1 GK starting, 1 GK bench
    gks = [p for p in picks if p["position"] == "GK"]
    defs = [p for p in picks if p["position"] == "DEF"]
    mids = [p for p in picks if p["position"] == "MID"]
    fwds = [p for p in picks if p["position"] == "FWD"]

    starting.append(gks[0]); bench.append(gks[1] if len(gks) > 1 else None)
    # Take min counts first
    starting.extend(defs[:3])
    starting.extend(mids[:2])
    starting.extend(fwds[:1])
    # Greedy fill remaining 4 starter slots from the leftover pool by ev_strategy
    leftover = (defs[3:] + mids[2:] + fwds[1:])
    leftover.sort(key=lambda p: -p["ev_strategy"])
    # Respect formation maxima: 5 DEF / 5 MID / 3 FWD already, since quota was 5/5/3.
    # The leftover can be assigned up to (5-3=2) DEF, (5-2=3) MID, (3-1=2) FWD.
    formation_max = {"DEF": 2, "MID": 3, "FWD": 2}
    formation_used = {"DEF": 0, "MID": 0, "FWD": 0}
    for p in leftover:
        if len(starting) >= 11:
            break
        if formation_used[p["position"]] < formation_max[p["position"]]:
            starting.append(p)
            formation_used[p["position"]] += 1
    # Remaining go to bench
    for p in leftover:
        if p in starting:
            continue
        bench.append(p)
    bench = [b for b in bench if b is not None]

    # Captain = argmax(ev_strategy) within starting XI; vice = 2nd
    starting_sorted = sorted(starting, key=lambda p: -p["ev_strategy"])
    captain_id = int(starting_sorted[0]["fantasy_player_id"])
    vice_id = int(starting_sorted[1]["fantasy_player_id"])

    # 12th Man: best EV player NOT in squad (unconstrained)
    twelfth = df[~df["fantasy_player_id"].isin(picked_ids)] \
        .sort_values("ev_strategy", ascending=False).head(1)
    twelfth_pick = twelfth.iloc[0].to_dict() if not twelfth.empty else None

    # Formation string
    n_def = sum(1 for p in starting if p["position"] == "DEF")
    n_mid = sum(1 for p in starting if p["position"] == "MID")
    n_fwd = sum(1 for p in starting if p["position"] == "FWD")
    formation = f"{n_def}-{n_mid}-{n_fwd}"

    # Projected total (XI ev × 1 + captain × 1 extra)
    xi_ev_sum = sum(p["ev_strategy"] for p in starting)
    cap_bonus = max(p["ev_strategy"] for p in starting)
    projected_pts = xi_ev_sum + cap_bonus

    def slim(p):
        return {
            "fantasy_player_id": int(p["fantasy_player_id"]),
            "known_name": p.get("known_name"),
            "nation_id": p.get("nation_id"),
            "position": p["position"],
            "price": float(p.get("price") or 0),
            "percent_selected": float(p.get("percent_selected") or 0),
            "ev_strategy": float(p["ev_strategy"]),
            "is_differential": (p.get("percent_selected") or 50) < 5,
            "captain": int(p["fantasy_player_id"]) == captain_id,
            "vice_captain": int(p["fantasy_player_id"]) == vice_id,
            "reason_chips": list(p.get("reason_chips") or []),
            "archetype": p.get("archetype_retrospective") or p.get("archetype_prospective"),
        }

    return {
        "strategy_id": strat["id"],
        "name": strat["name"],
        "blurb": strat["blurb"],
        "weights": {
            "alpha_floor": strat["alpha_floor"],
            "beta_ceiling": strat["beta_ceiling"],
            "gamma_differential": strat["gamma_differential"],
        },
        "sb_quota": strat["sb_quota"],
        "sb_band_count": int(sum(1 for p in picks if (p.get("percent_selected") or 50) < 5)),
        "formation": formation,
        "budget_spent_m": round(spent, 2),
        "budget_max_m": budget_m,
        "captain_id": captain_id,
        "vice_captain_id": vice_id,
        "projected_pts_with_captain": round(projected_pts, 2),
        "starting_xi": [slim(p) for p in starting],
        "bench": [slim(p) for p in bench],
        "twelfth_man": slim(twelfth_pick) if twelfth_pick else None,
        "non_budget_mode": non_budget,
        # ── FIFA WC2026 rule-aware fields (consumed by KsTwoCentsView) ──
        # Forward-looking chip schedule per model (see plan_chips() above).
        "chip_plan": plan_chips(strat["id"], int(strat.get("target_round_id", 3) or 3)),
        # Per-round transfer accounting — populated by the multi-checkpoint
        # emitter once snapshot history exists. Stub-zero today so the PWA's
        # transfer strip can render without optional-chaining at every read.
        # TODO(nb_17): diff against the prior snapshot's squad to populate
        # planned_transfer_count + pending_in/out at each FIFA lock window.
        "planned_transfer_count": 0,
        "pending_in": [],
        "pending_out": [],
        # Checkpoint cadence stub — single entry today, becomes a list of
        # past snapshots once the warehouse archives each FIFA lock emit
        # (R3 → post-MD3 / pre-R32 → post-R32 / pre-R16 → …).
        "checkpoints": [],
    }


# ─── M4 SB Hunter — custom squad assembly ───────────────────────────────────


def assemble_sb_hunter_squad(scored: pd.DataFrame, strat: dict,
                              budget_m: float = 100.0,
                              max_per_nation: int = 3,
                              non_budget: bool = False) -> dict:
    """K's SB Hunter rule:
      - 2 anchors: top 2 FWDs by `boost_sure_shot_fwd` (any ownership)
      - 1 anchor:  top 1 MID by `boost_influential_mid` (any ownership)
      - 12 differentials: top by ev_model from <5% ownership pool, filling
        the remaining quota (2 GK / 5 DEF / 4 MID / 1 FWD)

    Same constraints as the standard assembler: position quota, nation cap,
    optional budget. If the SB-band pool runs out for some position, falls
    back to non-SB to complete the squad and reports sb_gap.
    """
    df = scored.copy()
    # Sort key for non-anchor picks
    df["ev_strategy"] = df.get("ev_model", df.get("ev_bracket", pd.Series(0, index=df.index)))

    quota = POSITION_QUOTA.copy()
    nation_count: dict[str, int] = {}
    spent = 0.0
    picks = []
    picked_ids: set[int] = set()
    anchor_meta: dict[int, str] = {}  # pid → "sure_shot_fwd" | "influential_mid"

    def can_pick(row, override_pos_check: bool = False) -> tuple[bool, str]:
        pos = row["position"]
        if not override_pos_check and quota.get(pos, 0) <= 0:
            return False, "pos_quota_full"
        nat = row["nation_id"]
        if nation_count.get(nat, 0) >= max_per_nation:
            return False, "nation_cap"
        price = float(row.get("price") or 0)
        if not non_budget and (spent + price) > budget_m:
            return False, "budget"
        return True, "ok"

    def add(row, anchor: str | None = None):
        nonlocal spent
        pid = int(row["fantasy_player_id"])
        picks.append(row.to_dict())
        picked_ids.add(pid)
        quota[row["position"]] -= 1
        nation_count[row["nation_id"]] = nation_count.get(row["nation_id"], 0) + 1
        spent += float(row.get("price") or 0)
        if anchor:
            anchor_meta[pid] = anchor

    # Step 1: 2 sure-shot FWDs
    if "boost_sure_shot_fwd" in df.columns:
        fwd_pool = df[(df["position"] == "FWD") & (df["is_active"].fillna(True) == True)].copy()
        fwd_pool = fwd_pool.sort_values("boost_sure_shot_fwd", ascending=False)
        picked_fwd = 0
        for _, row in fwd_pool.iterrows():
            if picked_fwd >= 2:
                break
            if int(row["fantasy_player_id"]) in picked_ids:
                continue
            ok, _ = can_pick(row)
            if not ok:
                continue
            add(row, anchor="sure_shot_fwd")
            picked_fwd += 1

    # Step 2: 1 most-influential MID
    if "boost_influential_mid" in df.columns:
        mid_pool = df[(df["position"] == "MID") & (df["is_active"].fillna(True) == True)].copy()
        mid_pool = mid_pool.sort_values("boost_influential_mid", ascending=False)
        for _, row in mid_pool.iterrows():
            if int(row["fantasy_player_id"]) in picked_ids:
                continue
            ok, _ = can_pick(row)
            if not ok:
                continue
            add(row, anchor="influential_mid")
            break

    # Step 3: 12 SB-band picks by ev_strategy from remaining slots.
    # Iterate descending by ev; SB-band only.
    diff_pool = df[(df["percent_selected"].fillna(50) < 5)
                   & (df["is_active"].fillna(True) == True)].sort_values(
        "ev_strategy", ascending=False)
    diff_count = 0
    target_diff = strat.get("sb_quota", 12)
    for _, row in diff_pool.iterrows():
        if sum(quota.values()) == 0:
            break
        if diff_count >= target_diff:
            break
        pid = int(row["fantasy_player_id"])
        if pid in picked_ids:
            continue
        ok, _ = can_pick(row)
        if not ok:
            continue
        add(row)
        diff_count += 1

    sb_gap = max(0, target_diff - diff_count)

    # Step 4: any leftover slots (e.g. SB-band exhausted) → fill from full pool
    if sum(quota.values()) > 0:
        full = df[df["is_active"].fillna(True) == True].sort_values(
            "ev_strategy", ascending=False)
        for _, row in full.iterrows():
            if sum(quota.values()) == 0:
                break
            pid = int(row["fantasy_player_id"])
            if pid in picked_ids:
                continue
            ok, _ = can_pick(row)
            if not ok:
                continue
            add(row)

    # ── Pick formation + captain (shared logic w/ assemble_strategy_squad) ──
    pos_order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    picks.sort(key=lambda p: (pos_order[p["position"]], -p["ev_strategy"]))

    starting = []
    bench = []
    gks = [p for p in picks if p["position"] == "GK"]
    defs = [p for p in picks if p["position"] == "DEF"]
    mids = [p for p in picks if p["position"] == "MID"]
    fwds = [p for p in picks if p["position"] == "FWD"]

    starting.append(gks[0]); bench.append(gks[1] if len(gks) > 1 else None)
    starting.extend(defs[:3])
    starting.extend(mids[:2])
    starting.extend(fwds[:1])
    leftover = (defs[3:] + mids[2:] + fwds[1:])
    leftover.sort(key=lambda p: -p["ev_strategy"])
    formation_max = {"DEF": 2, "MID": 3, "FWD": 2}
    formation_used = {"DEF": 0, "MID": 0, "FWD": 0}
    for p in leftover:
        if len(starting) >= 11: break
        if formation_used[p["position"]] < formation_max[p["position"]]:
            starting.append(p)
            formation_used[p["position"]] += 1
    for p in leftover:
        if p in starting: continue
        bench.append(p)
    bench = [b for b in bench if b is not None]

    starting_sorted = sorted(starting, key=lambda p: -p["ev_strategy"])
    captain_id = int(starting_sorted[0]["fantasy_player_id"])
    vice_id = int(starting_sorted[1]["fantasy_player_id"])

    twelfth = df[~df["fantasy_player_id"].isin(picked_ids)] \
        .sort_values("ev_strategy", ascending=False).head(1)
    twelfth_pick = twelfth.iloc[0].to_dict() if not twelfth.empty else None

    n_def = sum(1 for p in starting if p["position"] == "DEF")
    n_mid = sum(1 for p in starting if p["position"] == "MID")
    n_fwd = sum(1 for p in starting if p["position"] == "FWD")
    formation = f"{n_def}-{n_mid}-{n_fwd}"

    xi_ev_sum = sum(p["ev_strategy"] for p in starting)
    cap_bonus = max(p["ev_strategy"] for p in starting)
    projected_pts = xi_ev_sum + cap_bonus

    def slim(p):
        pid = int(p["fantasy_player_id"])
        return {
            "fantasy_player_id": pid,
            "known_name": p.get("known_name"),
            "nation_id": p.get("nation_id"),
            "position": p["position"],
            "price": float(p.get("price") or 0),
            "percent_selected": float(p.get("percent_selected") or 0),
            "ev_strategy": float(p["ev_strategy"]),
            "is_differential": (p.get("percent_selected") or 50) < 5,
            "captain": pid == captain_id,
            "vice_captain": pid == vice_id,
            "reason_chips": list(p.get("reason_chips") or []),
            "archetype": p.get("archetype_retrospective") or p.get("archetype_prospective"),
            "anchor_role": anchor_meta.get(pid),  # "sure_shot_fwd" / "influential_mid" / None
        }

    sb_band_count = sum(1 for p in picks if (p.get("percent_selected") or 50) < 5)

    return {
        "strategy_id": strat["id"],
        "model_id": strat["id"],
        "name": strat["name"],
        "blurb": strat["blurb"],
        "weights": {
            "alpha_floor": strat.get("alpha_floor", 0),
            "beta_ceiling": strat.get("beta_ceiling", 0),
            "gamma_differential": strat.get("gamma_differential", 0),
        },
        "sb_quota": strat["sb_quota"],
        "sb_band_count": sb_band_count,
        "sb_gap": sb_gap,
        "formation": formation,
        "budget_spent_m": round(spent, 2),
        "budget_max_m": budget_m,
        "captain_id": captain_id,
        "vice_captain_id": vice_id,
        "projected_pts_with_captain": round(projected_pts, 2),
        "starting_xi": [slim(p) for p in starting],
        "bench": [slim(p) for p in bench],
        "twelfth_man": slim(twelfth_pick) if twelfth_pick else None,
        "non_budget_mode": non_budget,
        "anchors": [
            {"role": "sure_shot_fwd", "count": sum(1 for r in anchor_meta.values() if r == "sure_shot_fwd")},
            {"role": "influential_mid", "count": sum(1 for r in anchor_meta.values() if r == "influential_mid")},
        ],
        # ── FIFA WC2026 rule-aware fields (see assemble_strategy_squad) ──
        "chip_plan": plan_chips(strat["id"], int(strat.get("target_round_id", 3) or 3)),
        "planned_transfer_count": 0,
        "pending_in": [],
        "pending_out": [],
        "checkpoints": [],
    }


# ─── Position Suggestor surface ──────────────────────────────────────────────


def refresh_squad_captain_and_bench(
    squad: dict,
    live_ev_by_pid: dict,
    captain_selector: str = "ev_live",
    selector_col_by_pid: dict | None = None,
) -> dict:
    """Re-pick captain (and re-order bench priority) using LIVE EV.

    The squad SELECTION respects the locked snapshot (anti-leak), but the
    captain decision benefits from live data — "who's actually scoring right
    now" — so this helper takes live ev_model values and:
      1. Picks captain from XI restricted to MID + FWD (FIFA Fantasy scoring
         heavily favours attackers for captain — 4-6 pts/goal vs 1-pt
         appearance baseline). Falls back to all XI if no MID/FWD.
      2. Vice = 2nd best by live ev within the same pool.
      3. Re-orders bench within each position by live ev (auto-sub
         priority — FIFA picks the first matching-position bench player
         when a starter misses, so highest live ev should sit first).
      4. Recomputes projected_pts_with_captain using live ev for captain
         and locked ev_strategy for everyone else.

    captain_selector:
      "ev_live"             — pick by live ev_model (M1 Banker default)
      "recent_form_streak"  — pick by recent-form post-boost (M2 Form Hunter)
      "creativity_engine"   — pick by creativity post-boost (M3 Stat Max)
      "sure_shot_fwd"       — pick FWD by sure-shot post-boost, fallback
                              influential_mid for MID (M4 SB Hunter)

    selector_col_by_pid maps the chosen selector to a per-player score; the
    caller supplies this from the scored frame (so it sees the post-boost
    columns). Falls back to ev_live if missing.

    Mutates `squad` in place AND returns it for chaining.
    """
    xi = squad.get("starting_xi") or []
    if not xi:
        return squad

    def _live_ev(p):
        return float(live_ev_by_pid.get(p["fantasy_player_id"],
                                        p.get("ev_strategy", 0)))

    def _selector_score(p):
        if selector_col_by_pid and captain_selector != "ev_live":
            pid = int(p["fantasy_player_id"])
            v = selector_col_by_pid.get(pid)
            if v is not None:
                return float(v)
        return _live_ev(p)

    if captain_selector == "sure_shot_fwd":
        # M4: prefer FWDs first, fallback to MIDs (influential_mid)
        attackers = [p for p in xi if p.get("position") == "FWD"]
        if not attackers:
            attackers = [p for p in xi if p.get("position") == "MID"]
        if not attackers:
            attackers = xi
    else:
        attackers = [p for p in xi if p.get("position") in ("MID", "FWD")]
    pool = attackers if attackers else xi
    pool_sorted = sorted(pool, key=lambda p: -_selector_score(p))
    new_captain = int(pool_sorted[0]["fantasy_player_id"])
    new_vice = int(pool_sorted[1]["fantasy_player_id"]) if len(pool_sorted) > 1 else None

    for p in xi:
        pid = int(p["fantasy_player_id"])
        p["captain"] = (pid == new_captain)
        p["vice_captain"] = (pid == new_vice)
        # Stash live ev on the slimmed row so the PWA can show "live vs
        # locked" if it wants.
        p["ev_live"] = round(_live_ev(p), 2)

    squad["captain_id"] = new_captain
    squad["vice_captain_id"] = new_vice
    squad["captain_picked_by"] = captain_selector

    # Re-order bench within position — highest live ev first so auto-sub
    # picks the best backup when FIFA Fantasy reaches into the bench.
    bench = squad.get("bench") or []
    by_pos = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for b in bench:
        by_pos.setdefault(b.get("position"), []).append(b)
    bench_sorted = []
    # FIFA's auto-sub order: GK→DEF→MID→FWD position match; within each, ev desc.
    for pos in ("GK", "DEF", "MID", "FWD"):
        for p in sorted(by_pos.get(pos, []), key=lambda x: -float(live_ev_by_pid.get(x["fantasy_player_id"], x.get("ev_strategy", 0)))):
            p["ev_live"] = round(float(live_ev_by_pid.get(p["fantasy_player_id"], p.get("ev_strategy", 0))), 2)
            bench_sorted.append(p)
    squad["bench"] = bench_sorted

    # Recompute projected = sum(locked ev for XI) + live_ev(captain)
    xi_ev_sum = sum(float(p.get("ev_strategy", 0)) for p in xi)
    cap_bonus = float(live_ev_by_pid.get(new_captain,
                                         next((p["ev_strategy"] for p in xi
                                               if p["fantasy_player_id"] == new_captain), 0)))
    squad["projected_pts_with_captain"] = round(xi_ev_sum + cap_bonus, 2)
    squad["projected_pts_breakdown"] = {
        "xi_locked_ev_sum": round(xi_ev_sum, 2),
        "captain_live_bonus": round(cap_bonus, 2),
        "captain_id": new_captain,
        "note": "XI EV from round-locked stats (anti-leak); captain bonus uses LIVE ev for next-round form sensitivity.",
    }
    return squad


def build_position_suggestor(scored: pd.DataFrame) -> dict:
    """Two surfaces:
      1. top_15_overall — top 15 ranked across all positions by ensemble ev
      2. look_out_for — under-the-radar value picks per position:
         5 DEF + 5 MID + 5 FWD + 2 GK = 17 picks

    Look-out-for rank: ev_per_app / sqrt(percent_selected+1) × (1 + sb_lift)
    where sb_lift = 0.3 if reason_chips contains 'SB_LIKELY', 0.0 otherwise.
    Captures "good projection given low ownership".
    """
    df = scored.copy()
    df["ev_ensemble"] = df[["ev_raw_p90", "ev_raw_per_app", "ev_raw_totals"]].mean(axis=1)

    # Top 15 overall by ev_ensemble
    top15 = df.sort_values("ev_ensemble", ascending=False).head(15).reset_index(drop=True)
    top15["rank"] = range(1, len(top15) + 1)

    # Look-out-for ranking
    sb_lift = df["reason_chips"].apply(
        lambda chips: 0.3 if (chips is not None and any("SB" in c for c in chips)) else 0.0
    )
    df["value_score"] = (
        df["ev_raw_per_app"].fillna(0)
        / np.sqrt(df["percent_selected"].fillna(50.0) + 1)
        * (1 + sb_lift)
    )

    per_pos = {"DEF": 5, "MID": 5, "FWD": 5, "GK": 2}
    look_out_for = []
    for pos, n in per_pos.items():
        sub = df[df["position"] == pos] \
            .sort_values("value_score", ascending=False).head(n).reset_index(drop=True)
        sub["rank"] = range(1, len(sub) + 1)
        look_out_for.append(sub)
    look_out_for_df = pd.concat(look_out_for, ignore_index=True)

    def slim(row):
        return {
            "rank": int(row["rank"]),
            "fantasy_player_id": int(row["fantasy_player_id"]),
            "known_name": row.get("known_name"),
            "nation_id": row.get("nation_id"),
            "opponent_nation_id": row.get("opponent_nation_id"),
            "position": row["position"],
            "price": float(row.get("price") or 0),
            "percent_selected": float(row.get("percent_selected") or 0),
            "ev_per_app": float(row.get("ev_raw_per_app") or 0),
            "ev_ensemble": float(row.get("ev_ensemble") or 0) if "ev_ensemble" in row else None,
            "value_score": float(row.get("value_score") or 0) if "value_score" in row else None,
            "differential": float(row.get("differential") or 0),
            "is_differential": (row.get("percent_selected") or 50) < 5,
            "fixture_shape": row.get("fixture_shape"),
            "archetype": row.get("archetype_retrospective") or row.get("archetype_prospective"),
            "reason_chips": list(row.get("reason_chips") or []),
        }

    return {
        "top_15_overall": [slim(r) for _, r in top15.iterrows()],
        "look_out_for": {
            pos: [slim(r) for _, r in look_out_for_df[look_out_for_df["position"] == pos].iterrows()]
            for pos in ["DEF", "MID", "FWD", "GK"]
        },
    }


# ─── Tracking — per-model round results ─────────────────────────────────────


# FIFA Fantasy free-transfer count by round (per master plan §F squad constraints).
FREE_TRANSFERS_BY_ROUND = {
    1: float("inf"),  # pre-MD1 setup, unlimited
    2: 2, 3: 2,        # group MD2/MD3
    4: float("inf"),  # R32 reset, unlimited
    5: 4,             # R16
    6: 4,             # QF
    7: 5,             # SF
    8: 6,             # Final
}
EXTRA_TRANSFER_COST = 3  # pts deducted per extra transfer above free count


def _squad_pick_ids(squad: dict) -> set:
    """Return the 15-man set (starting + bench) of fantasy_player_ids."""
    out = set()
    for p in squad.get("starting_xi", []):
        out.add(int(p["fantasy_player_id"]))
    for p in squad.get("bench", []):
        out.add(int(p["fantasy_player_id"]))
    return out


def compute_round_actuals(model_id: str, round_id: int,
                           squad_for_round: dict,
                           prior_squad: dict | None = None) -> dict:
    """For a closed round: join the squad against fantasy_player_round_stats
    to surface per-player actual points, the XI actual total (with captain
    doubled), transfer count + cost, and aggregate metrics. Caller passes
    the squad assembled for THIS round and the prior round's squad (for the
    transfer diff).
    """
    rs = pd.read_parquet(PROC / "fantasy_player_round_stats.parquet")
    rs_round = rs[rs["round_id"] == round_id]
    pts_by_pid = dict(zip(rs_round["fantasy_player_id"], rs_round["points"]))
    minutes_by_pid = dict(zip(rs_round["fantasy_player_id"], rs_round["minutes_played"]))

    captain_id = squad_for_round.get("captain_id")
    vice_id = squad_for_round.get("vice_captain_id")

    starting_xi_actuals = []
    xi_total = 0.0
    captain_played = False
    for p in squad_for_round.get("starting_xi", []):
        pid = int(p["fantasy_player_id"])
        pts = float(pts_by_pid.get(pid, 0) or 0)
        mins = float(minutes_by_pid.get(pid, 0) or 0)
        is_cap = pid == captain_id
        is_vc = pid == vice_id
        eff_pts = pts * (2 if is_cap and mins > 0 else 1)
        if is_cap and mins > 0:
            captain_played = True
        xi_total += eff_pts
        starting_xi_actuals.append({
            "fantasy_player_id": pid,
            "known_name": p.get("known_name"),
            "position": p["position"],
            "is_captain": is_cap,
            "is_vice_captain": is_vc,
            "minutes_played": mins,
            "raw_pts": pts,
            "effective_pts": eff_pts,
            "match_finished": mins > 0 or pts != 0,
        })

    # Captain didn't play → vice steps in
    if not captain_played and vice_id is not None:
        for entry in starting_xi_actuals:
            if entry["fantasy_player_id"] == vice_id and entry["minutes_played"] > 0:
                # Double the vice points
                xi_total += entry["raw_pts"]  # already counted once; add second copy
                entry["effective_pts"] = entry["raw_pts"] * 2
                entry["promoted_captain"] = True
                break

    # ── Auto-substitution (FIFA Fantasy WC2026 rule) ─────────────────────────
    # For each non-playing starter (minutes_played == 0), find the highest-
    # priority bench player who DID play and whose position can fill the slot
    # without breaking formation minimums (GK=1, DEF≥3, MID≥2, FWD≥1 inside
    # the final XI). Bench priority comes from the round-lock refresh —
    # ordered by live ev within position (GK→DEF→MID→FWD).
    bench_raw = squad_for_round.get("bench", []) or []
    bench_pool = [
        {
            "fantasy_player_id": int(p["fantasy_player_id"]),
            "known_name": p.get("known_name"),
            "position": p["position"],
            "raw_pts": float(pts_by_pid.get(int(p["fantasy_player_id"]), 0) or 0),
            "minutes_played": float(minutes_by_pid.get(int(p["fantasy_player_id"]), 0) or 0),
            "auto_subbed_in": False,
        }
        for p in bench_raw
    ]
    # Snapshot of current XI composition (used to gate outfield swaps).
    def _xi_pos_counts(entries):
        c = {"GK": 0, "DEF": 0, "MID": 0, "FWD": 0}
        for e in entries:
            c[e["position"]] = c.get(e["position"], 0) + 1
        return c

    auto_subs = []
    # Iterate non-playing starters in stable order so multi-sub scenarios are
    # deterministic. GK sub is independent (separate slot); outfield subs
    # must preserve DEF≥3, MID≥2, FWD≥1 in the post-sub XI.
    for starter in starting_xi_actuals:
        if starter["minutes_played"] > 0:
            continue  # played, no sub needed
        s_pos = starter["position"]
        # Find best eligible bench candidate
        for cand in bench_pool:
            if cand["auto_subbed_in"]:
                continue
            if cand["minutes_played"] <= 0:
                continue  # bench player didn't play either
            if s_pos == "GK":
                # GK ↔ GK only
                if cand["position"] != "GK":
                    continue
            else:
                # Outfield: bench candidate's position must not violate the
                # post-sub min-formation. Build a hypothetical XI without the
                # missing starter + with the candidate.
                hypo = [e for e in starting_xi_actuals if e is not starter]
                hypo_counts = _xi_pos_counts(hypo)
                hypo_counts[cand["position"]] = hypo_counts.get(cand["position"], 0) + 1
                if (hypo_counts["DEF"] < 3 or hypo_counts["MID"] < 2 or
                        hypo_counts["FWD"] < 1):
                    continue
            # Apply the sub
            sub_pts = cand["raw_pts"]
            # If the auto-subbed-in player is the vice-captain AND captain
            # didn't play, vice→captain doubling already happened above;
            # don't double-count.
            cap_double = (cand["fantasy_player_id"] == captain_id and not captain_played)
            effective = sub_pts * (2 if cap_double else 1)
            xi_total += effective
            starter["auto_subbed_out"] = True
            starter["auto_sub_replacement_id"] = cand["fantasy_player_id"]
            starter["auto_sub_replacement_name"] = cand["known_name"]
            starter["auto_sub_replacement_pts"] = effective
            cand["auto_subbed_in"] = True
            cand["replaced_player_id"] = starter["fantasy_player_id"]
            cand["replaced_player_name"] = starter["known_name"]
            cand["effective_pts"] = effective
            auto_subs.append({
                "out": starter["fantasy_player_id"],
                "in": cand["fantasy_player_id"],
                "pts_added": effective,
            })
            break

    bench_actuals = bench_pool

    # Transfers
    transfer_count = 0
    transfers_in: list = []
    transfers_out: list = []
    if prior_squad is not None:
        prev_set = _squad_pick_ids(prior_squad)
        curr_set = _squad_pick_ids(squad_for_round)
        transfers_out = sorted(prev_set - curr_set)
        transfers_in = sorted(curr_set - prev_set)
        transfer_count = len(transfers_in)
    free_t = FREE_TRANSFERS_BY_ROUND.get(round_id, 2)
    extra_t = max(0, transfer_count - (0 if free_t == float("inf") else int(free_t)))
    transfer_penalty = extra_t * EXTRA_TRANSFER_COST

    final_total = xi_total - transfer_penalty

    return {
        "model_id": model_id,
        "round_id": round_id,
        "starting_xi_actuals": starting_xi_actuals,
        "bench_actuals": bench_actuals,
        "xi_actual_total": round(xi_total, 1),
        "transfer_count": transfer_count,
        "free_transfers": (None if free_t == float("inf") else int(free_t)),
        "extra_transfers": extra_t,
        "transfer_penalty": transfer_penalty,
        "final_round_pts": round(final_total, 1),
        "transfers_in": transfers_in,
        "transfers_out": transfers_out,
        "captain_played": captain_played,
        "auto_subs": auto_subs,
    }


def build_round_tracking(squads_by_round: dict[int, list[dict]]) -> dict:
    """squads_by_round: {round_id: [squad_dict_for_each_model]}.
    For each (model, round) pair where the round is closed, compute the
    actuals. Returns a list ready for JSON emit.

    Returns:
      {
        "by_model": {model_id: [{round_id, projected, actual, transfers, ...}, ...]},
        "totals": {model_id: total_pts_so_far},
        "round_status": {round_id: "complete" | "playing" | "scheduled"}
      }
    """
    fr = pd.read_parquet(PROC / "fantasy_rounds.parquet")
    status_by_round = dict(zip(fr["round_id"].astype(int), fr["status"]))

    by_model: dict[str, list] = {}
    for round_id, squads in sorted(squads_by_round.items()):
        status = status_by_round.get(round_id, "scheduled")
        for sq in squads:
            mid = sq.get("model_id") or sq.get("strategy_id")
            row = {
                "model_id": mid,
                "round_id": round_id,
                "status": status,
                "projected_pts": sq.get("projected_pts_with_captain"),
                "snapshot_ts": sq.get("snapshot_ts"),
            }
            if status == "complete":
                prior = None
                if (round_id - 1) in squads_by_round:
                    prior_list = squads_by_round[round_id - 1]
                    prior = next((s for s in prior_list
                                  if (s.get("model_id") or s.get("strategy_id")) == mid), None)
                actuals = compute_round_actuals(mid, round_id, sq, prior)
                row.update(actuals)
            by_model.setdefault(mid, []).append(row)

    totals = {
        mid: round(sum(r.get("final_round_pts", 0) or 0 for r in rows), 1)
        for mid, rows in by_model.items()
    }
    return {
        "by_model": by_model,
        "totals": totals,
        "round_status": status_by_round,
    }


# ─── Section I: Nation strength composite ────────────────────────────────────


def nation_strength_composite() -> pd.DataFrame:
    """Build per-nation strength index from §I components."""
    nations = pd.read_parquet(PROC / "wc26_stg_nations.parquet")[
        ["nation_id", "fifa_rank", "squad_valuation_m_eur", "confederation",
         "group", "is_host"]
    ]
    trophies = pd.read_csv(ROOT / "data" / "overrides" / "wc_trophies.csv")[
        ["nation_id", "trophies_won"]
    ]
    nations = nations.merge(trophies, on="nation_id", how="left")
    nations["trophies_won"] = nations["trophies_won"].fillna(0).astype(int)

    # I.2 Tournament metrics (per-match normalized)
    tmm = pd.read_parquet(PROC / "wc26_stg_team_match_metrics.parquet")
    grp = tmm.groupby("nation_id").agg(
        matches_played=("fifa_match_id", "nunique"),
        goals_scored_total=("Goals", "sum"),
        passes_total=("Passes", "sum"),
        passes_completed_total=("PassesCompleted", "sum"),
        sot_total=("AttemptAtGoalOnTarget", "sum"),
        shots_total=("AttemptAtGoal", "sum"),
        tackles_total=("Tackles", "sum") if "Tackles" in tmm.columns else ("Goals", "sum"),
        chances_total=("ChancesCreated", "sum") if "ChancesCreated" in tmm.columns else ("Goals", "sum"),
        yellows_total=("YellowCards", "sum"),
        clean_sheets_total=("CleanSheets", "sum"),
        forced_turnovers_total=("ForcedTurnovers", "sum"),
        pressures_total=("DefensivePressuresApplied", "sum"),
        distance_total=("TotalDistance", "sum"),
        gk_saves_total=("GoalkeeperSaves", "sum"),
    ).reset_index()

    # Per-match normalization
    pm = grp.copy()
    for col in [c for c in grp.columns if c.endswith("_total")]:
        new_col = col.replace("_total", "_pm")
        pm[new_col] = grp[col] / grp["matches_played"].replace(0, np.nan)
    pm["pass_completion_pct"] = grp["passes_completed_total"] / grp["passes_total"].replace(0, np.nan)
    pm["sot_pct"] = grp["sot_total"] / grp["shots_total"].replace(0, np.nan)

    # I.4 Player cumulative strength rollup
    pr = pd.read_parquet(PROC / "wc26_stg_player_powerrank.parquet")
    pr_nations = pr.merge(
        pd.read_parquet(PROC / "wc26_stg_players_view.parquet")[
            ["fifa_player_id", "nation_id"]
        ],
        on="fifa_player_id", how="left",
    )
    rollup = pr_nations.groupby("nation_id").agg(
        nation_atk_score=("avg_attacking_score", "sum"),
        nation_def_score=("avg_defensive_score", "sum"),
        nation_cre_score=("avg_creativity_score", "sum"),
        nation_gk_score=("avg_defending_the_goal_score", "sum"),
    ).reset_index()

    # Combine
    out = nations.merge(pm, on="nation_id", how="left")
    out = out.merge(rollup, on="nation_id", how="left")

    # Composite: normalize each component to [0, 1] then weighted sum
    def norm01(s: pd.Series) -> pd.Series:
        s = s.astype(float)
        lo, hi = s.min(), s.max()
        if hi - lo == 0 or pd.isna(hi - lo):
            return pd.Series(0.5, index=s.index)
        return (s - lo) / (hi - lo)

    # Static profile composite (I.1)
    out["i1_static"] = (
        0.30 * norm01(out["trophies_won"])
        + 0.30 * (1 - norm01(out["fifa_rank"]))  # lower rank = stronger
        + 0.30 * norm01(out["squad_valuation_m_eur"])
        + 0.10 * out["is_host"].astype(float)
    )
    # Form composite (I.2) — top-4 indicators
    out["i2_form"] = (
        0.40 * norm01(out["goals_scored_pm"])
        + 0.20 * norm01(out["sot_pct"])
        + 0.20 * (1 - norm01(out["yellows_pm"]))
        + 0.20 * norm01(out["clean_sheets_pm"])
    ).fillna(0.5)
    # Player rollup (I.4)
    out["i4_player"] = (
        0.30 * norm01(out["nation_atk_score"].fillna(0))
        + 0.30 * norm01(out["nation_def_score"].fillna(0))
        + 0.20 * norm01(out["nation_cre_score"].fillna(0))
        + 0.20 * norm01(out["nation_gk_score"].fillna(0))
    )

    # Total: weighted average. Placeholder weights — EDA tunes these.
    out["nation_total_strength"] = (
        0.30 * out["i1_static"]
        + 0.40 * out["i2_form"]
        + 0.30 * out["i4_player"]
    )

    return out
