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
    rm["weather_cluster"] = rm.apply(assign_weather_cluster, axis=1)
    wm = rm["weather_cluster"].map(WEATHER_MODIFIER).apply(pd.Series)
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
    """Compute ev_strategy = α·floor + β·ceiling + γ·differential.

    Per D-1: normalization mode is the simple mean across the 3 mode scores
    (so no factor is silently skewed by minutes-handling). Strategies don't
    own a single mode.
    """
    floor_mean = scored[["floor_p90", "floor_per_app", "floor_totals"]].mean(axis=1)
    ceil_mean = scored[["ceiling_p90", "ceiling_per_app", "ceiling_totals"]].mean(axis=1)
    diff = scored["differential"].fillna(0)
    return (
        strat["alpha_floor"] * floor_mean.fillna(0)
        + strat["beta_ceiling"] * ceil_mean.fillna(0)
        + strat["gamma_differential"] * diff
    )


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
    }


# ─── Position Suggestor surface ──────────────────────────────────────────────


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
