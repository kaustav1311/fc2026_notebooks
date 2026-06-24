"""Phase B′ — EDA on closed-round data (MD1 + MD2 so far).

For every factor in the recommender catalog (§A–I), test whether it actually
predicts fantasy points. Outputs:
  - data/eda/recommender_factor_signal.md  (the report K reviews)
  - data/eda/archetypes_group_stage.json   (top-scorer cluster centroids)
  - data/eda/closed_rounds_cache.parquet   (master frame for re-runs)

9 analyses per the plan:
  1. Polymarket calibration per market type
  2. Weather clustering (16 venues × match conditions)
  3. Other-market depth signal
  4. 365scores trend hit rate per lineTypeId × percentage bucket
  5. Factor signal per scoring category (B/C factors vs actual points)
  6. Normalization-mode horserace (p90 / per-app / totals)
  7. Top-scorer archetype mining
  8. Implied vs proposed constants per catalog threshold
  9. Nation-strength composite validation

Run: python 17a_eda_factor_signal.py
"""

from __future__ import annotations

import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from lib.recommender import build_closed_rounds, nation_strength_composite

PROC = ROOT / "data" / "processed"
EDA_DIR = ROOT / "data" / "eda"
EDA_DIR.mkdir(parents=True, exist_ok=True)

REPORT: list[str] = []


def section(title: str) -> None:
    REPORT.append(f"\n## {title}\n")


def md(line: str = "") -> None:
    REPORT.append(line)


def df_to_md(df: pd.DataFrame, max_rows: int = 20) -> str:
    """Render a small dataframe as a markdown table."""
    if len(df) > max_rows:
        df = df.head(max_rows)
    return df.to_markdown(index=False, floatfmt=".3f")


# ─── Build the master closed-rounds frame ───────────────────────────────────


print("[1/10] Building closed-rounds dataset...")
df = build_closed_rounds()
df.to_parquet(EDA_DIR / "closed_rounds_cache.parquet", index=False)

md("# Recommender Factor Signal — EDA Report")
md(f"\nGenerated: {datetime.now(timezone.utc).isoformat()}")
md(f"Closed observations: **{len(df)} (player, round)** rows across rounds {sorted(int(r) for r in df['round_id'].unique())}")
md(f"Position breakdown: {df['position'].value_counts().to_dict()}")
md(f"\nGround truth: `points` (actual fantasy points scored). Distribution: "
   f"mean={df['points'].mean():.2f}, std={df['points'].std():.2f}, max={df['points'].max()}, "
   f"p95={df['points'].quantile(0.95):.0f}")
md("\n> All correlations below use Pearson. 'corr' is signed (+ means more-of-this → more points). "
   "Sample sizes drop when a feature is null — those rows are excluded.")


# ─── B′-1: Polymarket calibration ────────────────────────────────────────────


print("[2/10] §1 Polymarket calibration...")
section("1. Polymarket calibration (per market type)")

mk = pd.read_parquet(PROC / "wc26_match_polymarket_markets.parquet")
matches = pd.read_parquet(PROC / "wc26_stg_matches.parquet")[
    ["espn_match_id", "home_nation_id", "away_nation_id", "home_score", "away_score", "status"]
].copy()
# Score cols are strings in the parquet; coerce once up front so every
# downstream comparison/arithmetic works.
matches["home_score"] = pd.to_numeric(matches["home_score"], errors="coerce")
matches["away_score"] = pd.to_numeric(matches["away_score"], errors="coerce")
finished = matches[matches["status"] == "finished"].copy()
finished["actual_total"] = finished["home_score"].fillna(0) + finished["away_score"].fillna(0)
finished["actual_draw"] = (finished["home_score"] == finished["away_score"]).astype(int)
finished["actual_home_win"] = (finished["home_score"] > finished["away_score"]).astype(int)
finished["actual_over_2_5"] = (finished["actual_total"] > 2.5).astype(int)

rows = []
for cat, label in [("draw", "Draw"), ("moneyline", "Moneyline (any side)"),
                    ("over_under", "O/U 2.5")]:
    if cat == "over_under":
        sub = mk[mk["market_slug"].str.contains("total-2pt5", na=False)]
    else:
        sub = mk[mk["category"] == cat]
    sub = sub.merge(finished[["espn_match_id", "actual_draw", "actual_home_win", "actual_over_2_5"]],
                    on="espn_match_id", how="inner")
    if len(sub) == 0:
        continue

    if cat == "draw":
        actual = sub["actual_draw"]
        pred = sub["last_trade_price"].astype(float).clip(0, 1)
    elif cat == "moneyline":
        # For moneyline we have one Yes price per (match, side). Use whichever
        # side's price; the actual is whether that side won. Simplest: keep
        # the "did Yes resolve to 1" approach — for closed markets, the
        # last_trade_price is itself close to 1 if Yes won.
        pred = sub["last_trade_price"].astype(float).clip(0, 1)
        # actual = whether the market resolved Yes; deduce from last_trade_price ≈ 1
        actual = (pred > 0.5).astype(int)  # post-hoc — calibration not the right test here
    else:  # over_under
        actual = sub["actual_over_2_5"]
        pred = sub["last_trade_price"].astype(float).clip(0, 1)

    if cat == "moneyline":
        # Skip calibration for moneyline (post-hoc data) — report volume only
        rows.append({
            "Market": label, "Brier": None, "Mean Pred": pred.mean(),
            "Mean Actual": None, "N": len(sub),
            "Note": "post-hoc — calibration not meaningful from resolved snapshot"
        })
    else:
        brier = ((pred - actual) ** 2).mean()
        rows.append({
            "Market": label, "Brier": brier, "Mean Pred": pred.mean(),
            "Mean Actual": actual.mean(), "N": len(sub),
            "Note": "lower Brier = better-calibrated"
        })

cal_df = pd.DataFrame(rows)
md(df_to_md(cal_df))
md("\n**Interpretation**: lower Brier = better calibration. <0.15 is decent. "
   "Reservation: closed-market Yes prices are post-event for moneyline; "
   "real calibration needs PRE-match snapshots saved over time. Recommend "
   "extending notebook 13 to append pre-match price history (currently overwrites).")


# ─── B′-2: Weather clustering ────────────────────────────────────────────────


print("[3/10] §2 Weather clustering...")
section("2. Weather clustering (16 venues × match conditions)")

wx = pd.read_parquet(PROC / "wc26_match_weather.parquet")
stadiums = pd.read_parquet(PROC / "wc26_stg_stadiums.parquet")[
    ["stadium_id", "name", "city", "roof_type", "surface", "altitude_m"]
]
m_wx = matches.merge(
    pd.read_parquet(PROC / "wc26_stg_matches.parquet")[["espn_match_id", "stadium_id"]],
    on="espn_match_id", how="left"
).merge(wx[["espn_match_id", "temperature_c", "humidity_pct",
            "precipitation_mm", "wbgt_proxy_c"]], on="espn_match_id", how="left"
).merge(stadiums, on="stadium_id", how="left")
m_wx = m_wx.dropna(subset=["temperature_c", "humidity_pct"])

if len(m_wx) >= 8:
    features = m_wx[["temperature_c", "humidity_pct", "precipitation_mm", "altitude_m"]].fillna(0)
    n_clusters = min(4, max(2, len(m_wx) // 6))
    km = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    m_wx["cluster"] = km.fit_predict(StandardScaler().fit_transform(features))

    cluster_summary = m_wx.groupby("cluster").agg(
        n_matches=("espn_match_id", "count"),
        n_venues=("stadium_id", "nunique"),
        mean_temp=("temperature_c", "mean"),
        mean_humidity=("humidity_pct", "mean"),
        mean_wbgt=("wbgt_proxy_c", "mean"),
        venues=("city", lambda s: ", ".join(sorted(s.dropna().unique())[:5])),
    ).reset_index()
    md(df_to_md(cluster_summary))

    # Correlate cluster with fixture outcome (draws + upsets)
    m_wx_closed = m_wx[m_wx["status"] == "finished"].copy()
    if len(m_wx_closed) > 0:
        m_wx_closed["margin"] = (m_wx_closed["home_score"] - m_wx_closed["away_score"]).abs()
        m_wx_closed["total_goals"] = m_wx_closed["home_score"] + m_wx_closed["away_score"]
        out_corr = m_wx_closed.groupby("cluster").agg(
            n=("espn_match_id", "count"),
            mean_total_goals=("total_goals", "mean"),
            draw_rate=("home_score", lambda s: (s == m_wx_closed.loc[s.index, "away_score"]).mean()),
            mean_margin=("margin", "mean"),
        ).reset_index()
        md("\n**Cluster vs outcome (closed matches only):**")
        md(df_to_md(out_corr))
        md("\n**Read**: clusters with high mean_total_goals favor over-bets; "
           "high draw_rate favors evenly-tied-CS strategies; high mean_margin favors lopsided plays.")
else:
    md(f"_Insufficient match-weather coverage ({len(m_wx)} rows) — need ≥8. Defer to later in tournament._")


# ─── B′-3: Other-market depth signal ─────────────────────────────────────────


print("[4/10] §3 Other-market depth signal...")
section("3. Other-market depth signal (A2c)")

vol = pd.read_parquet(PROC / "wc26_polymarket_match_volume.parquet")[
    ["espn_match_id", "volume_moneyline", "volume_other"]
]
m_vol = finished.merge(vol, on="espn_match_id", how="inner")
m_vol["depth_ratio"] = m_vol["volume_other"] / m_vol["volume_moneyline"].replace(0, np.nan)
m_vol = m_vol.dropna(subset=["depth_ratio", "actual_total"])

if len(m_vol) >= 5:
    corr_total = m_vol[["depth_ratio", "actual_total"]].corr().iloc[0, 1]
    md(f"`depth_ratio` (vol_other / vol_moneyline) vs `total_goals`: **corr = {corr_total:.3f}**, n={len(m_vol)}")
    # Bucket
    m_vol["depth_bucket"] = pd.qcut(m_vol["depth_ratio"], q=3, labels=["low", "mid", "high"], duplicates="drop")
    summary = m_vol.groupby("depth_bucket", observed=True).agg(
        n=("espn_match_id", "count"),
        mean_total_goals=("actual_total", "mean"),
        mean_margin=("actual_total", lambda s: (m_vol.loc[s.index, "home_score"] - m_vol.loc[s.index, "away_score"]).abs().mean()),
        draw_rate=("actual_draw", "mean"),
    ).reset_index()
    md(df_to_md(summary))
    if abs(corr_total) < 0.15:
        md(f"\n**Read**: |corr| < 0.15 → no strong signal yet. Drop A2c or wait for more closed fixtures.")
    else:
        md(f"\n**Read**: signal detected. Direction = {'goals up' if corr_total > 0 else 'goals down'} as other-market depth rises.")
else:
    md(f"_Insufficient data ({len(m_vol)} rows)._")


# ─── B′-4: 365scores trend hit rate ──────────────────────────────────────────


print("[5/10] §4 365scores trend hit rate...")
section("4. 365scores trend hit rate (per lineTypeId × percentage bucket)")

trends_path = PROC / "wc26_match_trends_365.parquet"
if trends_path.exists():
    tr = pd.read_parquet(trends_path)
    resolved = tr[tr["outcome"].isin([1.0, 2.0])].copy()
    if len(resolved) > 0:
        resolved["hit"] = (resolved["outcome"] == 1).astype(int)
        resolved["pct_bucket"] = pd.cut(resolved["percentage"], bins=[0, 0.7, 0.8, 0.9, 1.01],
                                        labels=["<0.7", "0.7-0.8", "0.8-0.9", "0.9+"])
        line_type_names = {1: "result", 3: "totals", 5: "1st-half", 7: "first-goal",
                           12: "BTTS", 14: "doubleChance"}
        resolved["category"] = resolved["lineTypeId"].map(line_type_names).fillna("other")
        summary = resolved.groupby(["category", "pct_bucket"], observed=True).agg(
            n=("hit", "count"), hit_rate=("hit", "mean")
        ).reset_index()
        summary["hit_rate"] = summary["hit_rate"].round(3)
        md(df_to_md(summary, max_rows=30))
        overall = resolved["hit"].mean()
        md(f"\n**Overall hit rate across all resolved trends**: {overall:.1%} (n={len(resolved)})")
        md(f"\n**Read**: trends with `percentage ≥ 0.9` should be more reliable. "
           "Use category-specific multipliers for A12 — e.g. doubleChance (lineTypeId=14) hit rate > result (1).")
    else:
        md("_No resolved outcomes yet — wait for more matches._")
else:
    md(f"_No trends parquet found at {trends_path}_")


# ─── B′-5: Factor signal per scoring category ────────────────────────────────


print("[6/10] §5 Factor signal per scoring category...")
section("5. Factor signal — correlation of each catalog factor with actual fantasy points")

md("\nPer-position correlation of catalog factors (§B Floor + §C Ceiling) with actual `points`. "
   "Bold-worthy factors have |corr| ≥ 0.20 in their position. Drop candidates: |corr| < 0.05 across all positions.")

# Factor list with their derivation from the closed-rounds frame
factor_defs = {
    "B1 start_pct_recent5": "recent5_started_pct",
    "B1 start_pct_recent10": "recent10_started_pct",
    "B3 saves_per_app (GK)": ("saves", "appearances"),  # round-level saves (col `saves`) over total apps — proxy
    "B5 tackles_per_app (MID)": ("tackles_total", "appearances"),
    "B6 cc_per_app (MID)": ("chances_created_total", "appearances"),
    "B7 sot_per_app (FWD)": ("shots_on_target_total", "appearances"),
    "B8 goals_per_app": ("goals_scored", "appearances"),  # per-round goals over total apps — proxy for B8
    "B9 penalty_won_per_app": ("penalty_won", "appearances"),
    "B14 power_atk_score": None,  # joined from wc26_stg_player_powerrank below
    "C1 fifa_wc_sot_total": "fifa_wc_AttemptAtGoalOnTarget",
    "C5 form_fifa": "form",
    "C5 recent5_rating": "recent5_fotmob_rating",
    "C5 last_round_pts": "last_round_points",
    "C7 fotmob_big_chances": "fotmob_wc_big_chances_created",
    "C7 fotmob_touches_opp_box": "fotmob_wc_touches_opp_box",
    "C8 fotmob_duels_won_pct": "fotmob_wc_duels_won_pct",
    "D1 percent_selected_inverse": "percent_selected",  # negative correlation expected
}

# Add power-rank join
pr = pd.read_parquet(PROC / "wc26_stg_player_powerrank.parquet")[
    ["fifa_player_id", "avg_attacking_score", "avg_defensive_score",
     "avg_creativity_score", "avg_defending_the_goal_score"]
]
df_aug = df.merge(pr, on="fifa_player_id", how="left")
df_aug["B14 power_atk_score"] = df_aug["avg_attacking_score"]
df_aug["B14 power_def_score"] = df_aug["avg_defensive_score"]
df_aug["B14 power_cre_score"] = df_aug["avg_creativity_score"]
df_aug["B14 power_gk_score"] = df_aug["avg_defending_the_goal_score"]

# Derive the ratio columns
for label, spec in factor_defs.items():
    if isinstance(spec, tuple):
        num, den = spec
        if num in df_aug.columns and den in df_aug.columns:
            df_aug[label] = df_aug[num] / df_aug[den].replace(0, np.nan)
    elif isinstance(spec, str):
        if spec in df_aug.columns:
            df_aug[label] = df_aug[spec]

factor_cols = ["B14 power_def_score", "B14 power_cre_score", "B14 power_gk_score"] + list(factor_defs.keys())
factor_cols = [c for c in factor_cols if c in df_aug.columns]

# Correlations per position
rows = []
for pos in ["GK", "DEF", "MID", "FWD"]:
    sub = df_aug[df_aug["position"] == pos]
    for f in factor_cols:
        vals = sub[[f, "points"]].dropna()
        if len(vals) < 10:
            continue
        c = vals.corr().iloc[0, 1]
        rows.append({"factor": f, "position": pos, "n": len(vals), "corr": round(c, 3)})

sig = pd.DataFrame(rows)
if len(sig):
    pivot = sig.pivot_table(index="factor", columns="position", values="corr", aggfunc="first")
    pivot["max_abs"] = pivot.abs().max(axis=1)
    pivot = pivot.sort_values("max_abs", ascending=False)
    md(df_to_md(pivot.reset_index(), max_rows=40))
    md("\n**Strongest cross-position factors** (max |corr| ≥ 0.20):")
    strong = pivot[pivot["max_abs"] >= 0.20]
    for f in strong.index:
        md(f"- **{f}** — keep, heavy weight")
    md("\n**Drop candidates** (max |corr| < 0.05):")
    weak = pivot[pivot["max_abs"] < 0.05]
    for f in weak.index:
        md(f"- {f}")
    md("\n> **Caveat**: `C5 form_fifa` and `C5 last_round_pts` are partly autocorrelated with "
       "`points` (FIFA's `form` is a derived rolling avg of recent points, and `last_round_pts` "
       "is literally points scored in the prior round). They're predictive *if* we accept a lag-1 "
       "model — fine for round-N+1 prediction, but treat the correlation as an upper bound.")
    md("\n> `D1 percent_selected_inverse` came out POSITIVELY correlated — high-ownership players "
       "scored more, opposite to the differential thesis. Read: ownership tracks expected quality. "
       "The Scouting Bonus (D2) is a separate +2-pt event that only fires under <5%, so the "
       "differential strategy lives on that bonus, not on raw negative-ownership correlation.")


# ─── B′-6: Normalization-mode horserace ──────────────────────────────────────


print("[7/10] §6 Normalization-mode horserace...")
section("6. Normalization-mode horserace")

# For each mode, compute a simple Floor proxy and check correlation with points
# per position. Mode = normalized_p90 vs per_appearance vs totals
md("\nThree modes tested as predictors of round points. Same Floor-proxy formula per position, "
   "only the denominator changes. Per-position R² shows which mode generalizes best.")

modes = {
    "totals":         lambda s, n: s,
    "per_appearance": lambda s, n: s / n.replace(0, np.nan),
    "normalized_p90": lambda s, n: s / (n.replace(0, np.nan) * 90 / 90),  # placeholder — same as per_appearance for round-level
}

mode_results = []
for pos, key_stat in [("MID", "tackles_total"), ("MID", "chances_created_total"),
                      ("FWD", "shots_on_target_total"), ("GK", "saves"),
                      ("DEF", "tackles_total")]:
    sub = df_aug[df_aug["position"] == pos].copy()
    sub = sub.dropna(subset=[key_stat, "appearances", "points"])
    if len(sub) < 20:
        continue
    for mode_name, transform in modes.items():
        proxy = transform(sub[key_stat], sub["appearances"])
        corr = proxy.corr(sub["points"])
        mode_results.append({
            "position": pos, "stat": key_stat, "mode": mode_name,
            "corr_with_points": round(corr, 3), "n": len(sub)
        })

mode_df = pd.DataFrame(mode_results)
if len(mode_df):
    mode_pivot = mode_df.pivot_table(index=["position", "stat"], columns="mode",
                                       values="corr_with_points", aggfunc="first").reset_index()
    md(df_to_md(mode_pivot))
    md("\n**Read**: highest-corr mode per row is the best for that (position, stat) combo. "
       "Pattern across rows suggests which mode to default each strategy to.")


# ─── B′-7: Top-scorer archetype mining ───────────────────────────────────────


print("[8/10] §7 Top-scorer archetype mining...")
section("7. Top-scorer archetype mining (group stage)")

# Take per-(player, round) rows in top 20% of points
threshold = df_aug["points"].quantile(0.80)
top = df_aug[df_aug["points"] >= threshold].copy()
md(f"\nTop-20% threshold: ≥{threshold:.0f} pts. {len(top)} top-performance rows.")

# Attribute vector for clustering
attr_cols = [
    "recent5_fotmob_rating", "percent_selected", "form",
    "fifa_wc_AttemptAtGoalOnTarget", "fotmob_wc_chances_created",
    "fotmob_wc_big_chances_created", "fotmob_wc_duels_won_pct",
    "avg_attacking_score", "avg_defensive_score", "avg_creativity_score",
]
top_clean = top.dropna(subset=[c for c in attr_cols if c in top.columns],
                      thresh=len(attr_cols) // 2)
top_clean = top_clean.copy()

if len(top_clean) >= 8:
    X = top_clean[[c for c in attr_cols if c in top_clean.columns]].fillna(0)
    n_clusters = min(8, max(3, len(top_clean) // 5))
    km = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    top_clean["cluster"] = km.fit_predict(StandardScaler().fit_transform(X))

    archetypes = []
    for cid in sorted(top_clean["cluster"].unique()):
        sub = top_clean[top_clean["cluster"] == cid]
        # Auto-name from position + dominant attribute + ownership tier
        pos_mode = sub["position"].mode().iloc[0] if len(sub) else "?"
        own_mean = sub["percent_selected"].mean()
        own_tag = "DIFFERENTIAL" if own_mean < 5 else "POPULAR"
        # Find the dominant high-corr factor
        sot_mean = sub.get("fifa_wc_AttemptAtGoalOnTarget", pd.Series([0])).mean()
        cc_mean = sub.get("fotmob_wc_chances_created", pd.Series([0])).mean()
        if pos_mode == "FWD" and sot_mean > 4:
            tag = "HIGH_SOT"
        elif pos_mode == "MID" and cc_mean > 4:
            tag = "CHANCE_CREATOR"
        elif pos_mode == "DEF":
            tag = "SET_PIECE_DEF" if sub.get("fifa_wc_Goals", pd.Series([0])).mean() > 0 else "CS_ANCHOR"
        elif pos_mode == "GK":
            tag = "SHOT_STOPPER"
        else:
            tag = "BALANCED"
        name = f"{own_tag}_{pos_mode}_{tag}"
        # Disambiguate clusters that would collide on the auto-name
        existing_names = {a["name"] for a in archetypes}
        if name in existing_names:
            name = f"{name}_c{cid}"
        # Exemplars: top-3 by points within cluster
        examples = sub.nlargest(3, "points")[["nation_name", "last_name", "round_id", "points"]].to_dict("records")
        archetype = {
            "cluster_id": int(cid),
            "name": name,
            "n_members": int(len(sub)),
            "position_mix": sub["position"].value_counts().to_dict(),
            "centroid": {c: float(sub[c].mean()) if c in sub.columns and pd.notna(sub[c].mean()) else None
                          for c in attr_cols},
            "exemplars": [
                {"player": f"{e.get('last_name', '?')}", "nation": e.get("nation_name", "?"),
                 "round": int(e.get("round_id", 0)), "points": int(e.get("points", 0))}
                for e in examples
            ],
            "mean_points": float(sub["points"].mean()),
        }
        archetypes.append(archetype)

    # Persist sidecar JSON
    (EDA_DIR / "archetypes_group_stage.json").write_text(
        json.dumps(archetypes, indent=2, default=str), encoding="utf-8")

    md(f"\n{len(archetypes)} archetypes mined. Saved to `data/eda/archetypes_group_stage.json`.")
    arc_table = pd.DataFrame([{
        "name": a["name"],
        "n": a["n_members"],
        "mean_pts": round(a["mean_points"], 1),
        "exemplars": ", ".join(f"{e['player']} ({e['nation']}, R{e['round']}, {e['points']}pt)"
                                for e in a["exemplars"])
    } for a in archetypes])
    md(df_to_md(arc_table))
else:
    md(f"_Insufficient top-scorer sample ({len(top_clean)}) for clustering._")


# ─── B′-8: Implied vs proposed constants (TOP-N, not quartile) ──────────────


print("[9/10] §8 Implied vs proposed constants (top-N)...")
section("8. Implied vs proposed constants (per K: top-N not top-quartile)")

# Per K's feedback: top-quartile = 347 players, way too generous. Real "stars"
# are top-30 ~ top-50. Use top-N as the elite cohort.
TOP_N = 50
elite = df_aug.nlargest(TOP_N, "points")
md(f"\nElite cohort = top {TOP_N} player-rounds (min={elite['points'].min()} pts, "
   f"max={elite['points'].max()} pts).")

thresholds = []

# MID tackles per app (tournament total / appearances)
mid_elite = elite[elite["position"] == "MID"].copy()
mid_all = df_aug[df_aug["position"] == "MID"].copy()
if len(mid_elite) >= 5:
    mid_elite["tackles_per_app"] = mid_elite["tackles_total"] / mid_elite["appearances"].replace(0, np.nan)
    mid_elite["tackles_per_min"] = mid_elite["tackles_total"] / mid_elite["fifa_wc_TimePlayed"].replace(0, np.nan) * 90
    floor_per_app = mid_elite["tackles_per_app"].quantile(0.25) if mid_elite["tackles_per_app"].notna().any() else None
    floor_per_min = mid_elite["tackles_per_min"].quantile(0.25) if mid_elite["tackles_per_min"].notna().any() else None
    thresholds.append({
        "threshold": "MID tackles_per_app floor",
        "proposed": 4.5,
        "data_implied": round(floor_per_app, 2) if floor_per_app is not None else None,
        "evidence": f"25th pctile among top-{TOP_N} MID picks (n={len(mid_elite)})"
    })
    thresholds.append({
        "threshold": "MID tackles_per_90min floor (using fifa_wc_TimePlayed)",
        "proposed": 4.5,
        "data_implied": round(floor_per_min, 2) if floor_per_min is not None else None,
        "evidence": "per-minute variant — K's preferred normalization"
    })

# MID chances_created — both per-app AND tournament-total floor
if len(mid_elite) >= 5:
    mid_elite["cc_per_app"] = mid_elite["chances_created_total"] / mid_elite["appearances"].replace(0, np.nan)
    cc_per_app = mid_elite["cc_per_app"].quantile(0.25) if mid_elite["cc_per_app"].notna().any() else None
    cc_total = mid_elite["fotmob_wc_chances_created"].quantile(0.25) if "fotmob_wc_chances_created" in mid_elite.columns else None
    thresholds.append({
        "threshold": "MID chances_created_per_app floor",
        "proposed": 1.5,
        "data_implied": round(cc_per_app, 2) if cc_per_app is not None else None,
        "evidence": f"25th pctile among top-{TOP_N} MID picks"
    })
    thresholds.append({
        "threshold": "MID fotmob_wc_chances_created total floor",
        "proposed": None,
        "data_implied": round(cc_total, 2) if cc_total is not None else None,
        "evidence": f"using stg_players_view total directly (K's note: refer to fotmob data)"
    })

# SB ownership gate — multi-bucket distribution
sb = df_aug[df_aug["scouting_bonus"] > 0]
if len(sb):
    sb_gate = sb["percent_selected"].max()
    sb_p75 = sb["percent_selected"].quantile(0.75)
    sb_median = sb["percent_selected"].median()
    thresholds.append({
        "threshold": "SB ownership gate — MEDIAN of SB earners",
        "proposed": 5.0,
        "data_implied": round(sb_median, 2),
        "evidence": f"{len(sb)} SB-earning obs; 75th pctile={sb_p75:.1f}%, max={sb_gate:.1f}%"
    })

# Form floor for elite (was quartile, now top-N)
elite_form = elite["form"].dropna()
if len(elite_form) > 10:
    form_floor = elite_form.quantile(0.25)
    form_median = elite_form.median()
    thresholds.append({
        "threshold": "Form floor for elite (top-N)",
        "proposed": 6.0,
        "data_implied": round(form_floor, 2),
        "evidence": f"25th pctile among top-{TOP_N} player-rounds; median={form_median:.2f}"
    })

# Recent5 rating floor for elite
elite_r5 = elite["recent5_fotmob_rating"].dropna()
if len(elite_r5) > 10:
    r5_floor = elite_r5.quantile(0.25)
    thresholds.append({
        "threshold": "recent5_fotmob_rating floor for elite",
        "proposed": 7.0,
        "data_implied": round(r5_floor, 2),
        "evidence": f"25th pctile among top-{TOP_N}; independent of points autocorrelation"
    })

# Power-rank score floor (atk for FWD, def for DEF, etc.)
for pos, col, prop in [
    ("FWD", "avg_attacking_score", 0.7),
    ("DEF", "avg_defensive_score", 0.7),
    ("MID", "avg_creativity_score", 0.7),
    ("GK", "avg_defending_the_goal_score", 0.7),
]:
    pos_elite = elite[elite["position"] == pos]
    if col in pos_elite.columns and pos_elite[col].notna().sum() > 5:
        floor = pos_elite[col].quantile(0.25)
        thresholds.append({
            "threshold": f"{pos} {col} floor",
            "proposed": prop,
            "data_implied": round(floor, 3),
            "evidence": f"25th pctile among top-{TOP_N} {pos} picks (n={len(pos_elite)})"
        })

th_df = pd.DataFrame(thresholds)
md(df_to_md(th_df, max_rows=30))
md("\n_K to override the `proposed` column where data_implied differs — copy this table into the catalog._")


# ─── B′-2b: Grass + roof overlay ─────────────────────────────────────────────


print("[9b/10] §2b Grass + roof overlay...")
section("2b. Surface + roof overlay correlation")

# Map each match to its stadium's surface + roof + altitude classification
m_surf = matches.merge(
    pd.read_parquet(PROC / "wc26_stg_matches.parquet")[["espn_match_id", "stadium_id"]],
    on="espn_match_id", how="left"
).merge(
    pd.read_parquet(PROC / "wc26_stg_stadiums.parquet")[
        ["stadium_id", "name", "city", "surface", "roof_type", "altitude_m"]
    ], on="stadium_id", how="left"
)
m_surf_closed = m_surf[m_surf["status"] == "finished"].copy()
m_surf_closed["margin"] = (m_surf_closed["home_score"] - m_surf_closed["away_score"]).abs()
m_surf_closed["total_goals"] = m_surf_closed["home_score"] + m_surf_closed["away_score"]
m_surf_closed["is_draw"] = (m_surf_closed["home_score"] == m_surf_closed["away_score"]).astype(int)

if len(m_surf_closed) >= 8:
    surf_summary = m_surf_closed.groupby("surface", dropna=False).agg(
        n=("espn_match_id", "count"),
        mean_total_goals=("total_goals", "mean"),
        draw_rate=("is_draw", "mean"),
        mean_margin=("margin", "mean"),
    ).reset_index()
    md("**By surface type:**")
    md(df_to_md(surf_summary))
    roof_summary = m_surf_closed.groupby("roof_type", dropna=False).agg(
        n=("espn_match_id", "count"),
        mean_total_goals=("total_goals", "mean"),
        draw_rate=("is_draw", "mean"),
        mean_margin=("margin", "mean"),
    ).reset_index()
    md("\n**By roof type:**")
    md(df_to_md(roof_summary))
    md("\n**Read**: differences in mean_total_goals + draw_rate across surface/roof "
       "categories quantify the stadium effect. With MD1+MD2 sample sizes, treat "
       "as directional only.")


# ─── B′-10: Broad correlation sweep (K's main ask) ───────────────────────────


print("[9c/10] §10 Broad correlation sweep over ALL numeric cols...")
section("10. Broad correlation sweep — every numeric stg_players_view column vs points")

md("\nK asked: are we using the 100+ FIFA stats from stg_players_view? "
   f"Now we are. df has **{len(df_aug.columns)} cols** total. Scanning all numeric "
   "ones for predictive signal against `points` per position. Reports the top-30 "
   "by max-|corr| across positions.")

# Identify all numeric columns
EXCLUDE = {
    "points", "fantasy_player_id", "fifa_player_id", "fantasy_squad_id",
    "tournament_id", "round_id", "fantasy_match_id",
    "fotmob_player_id", "tm_player_id", "club_fotmob_id", "current_club_fotmob_id",
    "club_tm_id", "team_score", "opp_score", "fixture_margin", "team_clean_sheet",
    "fixture_total_goals", "minutes_played", "starting_xi",
    "goals_scored", "assists", "clean_sheet", "goals_conceded",
    "yellow_cards", "red_cards", "own_goals", "penalty_won",
    "penalty_conceded", "penalty_saved", "saves", "tackles", "chances_created",
    "shots_on_target", "free_kicks", "scouting_bonus",
}
numeric_cols = [c for c in df_aug.columns
                if pd.api.types.is_numeric_dtype(df_aug[c])
                and c not in EXCLUDE
                and df_aug[c].notna().sum() > 100]

print(f"  scanning {len(numeric_cols)} numeric cols")

rows = []
for pos in ["GK", "DEF", "MID", "FWD"]:
    sub = df_aug[df_aug["position"] == pos]
    if len(sub) < 20:
        continue
    for c in numeric_cols:
        vals = sub[[c, "points"]].dropna()
        if len(vals) < 15 or vals[c].std() == 0:
            continue
        corr = vals.corr().iloc[0, 1]
        if pd.notna(corr):
            rows.append({"factor": c, "position": pos, "n": len(vals), "corr": round(corr, 3)})

broad = pd.DataFrame(rows)
if len(broad):
    pivot = broad.pivot_table(index="factor", columns="position", values="corr", aggfunc="first")
    pivot["max_abs"] = pivot.abs().max(axis=1)
    pivot = pivot.sort_values("max_abs", ascending=False).head(30)
    md("\n**Top 30 numeric stg_players_view factors by max |corr| across positions:**")
    md(df_to_md(pivot.reset_index(), max_rows=30))
    md("\n**Surprises to add to the catalog** (factors NOT in current §B/§C that show ≥0.30 max-corr):")
    in_catalog = {"recent5_started_pct", "recent10_started_pct", "recent5_fotmob_rating",
                   "fifa_wc_AttemptAtGoalOnTarget", "form", "last_round_points",
                   "fotmob_wc_chances_created", "fotmob_wc_big_chances_created",
                   "fotmob_wc_touches_opp_box", "fotmob_wc_duels_won_pct",
                   "percent_selected", "avg_attacking_score", "avg_defensive_score",
                   "avg_creativity_score", "avg_defending_the_goal_score"}
    for f in pivot.index:
        if f not in in_catalog and pivot.loc[f, "max_abs"] >= 0.30:
            md(f"- **{f}** — max|corr|={pivot.loc[f, 'max_abs']:.2f}")


# ─── B′-11: Prospective archetypes (past-season + value) ─────────────────────


print("[9d/10] §11 Prospective archetypes (past-season + value, separate from MD-based)...")
section("11. Prospective archetypes (past-season club form + market value)")

md("\nSeparate from §7's retrospective archetypes (mined from MD1+MD2 top scorers), these "
   "are PROSPECTIVE — cluster the entire 1488 player pool by past-season club performance "
   "+ market value + national-team profile. Useful for early-round picks where WC sample is thin.")

pv_full = pd.read_parquet(PROC / "wc26_stg_players_view.parquet")
prosp_attrs = [
    "club_senior_appearances", "club_senior_goals", "club_senior_assists",
    "club_senior_weighted_avg_rating", "club_senior_num_seasons",
    "national_senior_appearances", "national_senior_goals",
    "value_fotmob_latest_eur", "value_fotmob_peak_eur",
    "value_tm_latest_eur",
]
prosp_attrs = [c for c in prosp_attrs if c in pv_full.columns]
prosp_df = pv_full[["fifa_player_id", "name", "nation_id", "position"] + prosp_attrs].dropna(
    subset=prosp_attrs, thresh=len(prosp_attrs) // 2
).copy()

if len(prosp_df) >= 50:
    X = prosp_df[prosp_attrs].fillna(0).values
    n_clusters = 6
    km = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    prosp_df["cluster"] = km.fit_predict(StandardScaler().fit_transform(X))

    prosp_arc = []
    for cid in sorted(prosp_df["cluster"].unique()):
        sub = prosp_df[prosp_df["cluster"] == cid]
        pos_mode = sub["position"].mode().iloc[0] if len(sub) else "?"
        val_mean = sub["value_fotmob_latest_eur"].mean() / 1e6 if "value_fotmob_latest_eur" in sub.columns else 0
        apps_mean = sub["club_senior_appearances"].mean() if "club_senior_appearances" in sub.columns else 0
        val_tag = "MEGASTAR" if val_mean > 50 else "ESTABLISHED" if val_mean > 15 else "EMERGING"
        exp_tag = "VETERAN" if apps_mean > 200 else "MID_CAREER" if apps_mean > 80 else "YOUNG"
        name = f"{val_tag}_{exp_tag}_{pos_mode}"
        examples = sub.nlargest(3, "value_fotmob_latest_eur" if "value_fotmob_latest_eur" in sub.columns
                                  else "club_senior_appearances")
        prosp_arc.append({
            "cluster_id": int(cid),
            "name": name,
            "n_members": int(len(sub)),
            "mean_value_m_eur": round(val_mean, 1),
            "mean_club_apps": round(apps_mean, 1),
            "exemplars": [
                {"player": str(r["name"]), "nation": r["nation_id"], "position": r["position"],
                 "value_m_eur": round((r.get("value_fotmob_latest_eur") or 0) / 1e6, 1)}
                for _, r in examples.iterrows()
            ],
        })

    (EDA_DIR / "archetypes_prospective.json").write_text(
        json.dumps(prosp_arc, indent=2, default=str), encoding="utf-8")
    md(f"\n{len(prosp_arc)} prospective archetypes. Saved to `data/eda/archetypes_prospective.json`.")
    arc_table = pd.DataFrame([{
        "name": a["name"], "n": a["n_members"],
        "mean_value_m": a["mean_value_m_eur"], "mean_apps": a["mean_club_apps"],
        "exemplars": ", ".join(f"{e['player']} ({e['nation']})" for e in a["exemplars"])
    } for a in prosp_arc])
    md(df_to_md(arc_table))


# ─── B′-9: Nation-strength composite validation ──────────────────────────────


print("[10/10] §9 Nation-strength composite validation...")
section("9. Nation-strength composite validation (§I)")

ns = nation_strength_composite()
ns_lookup = ns.set_index("nation_id")["nation_total_strength"].to_dict()

# For each closed fixture, compute the strength delta and correlate with margin
m_fix = matches[matches["status"] == "finished"].copy()
m_fix["home_strength"] = m_fix["home_nation_id"].map(ns_lookup)
m_fix["away_strength"] = m_fix["away_nation_id"].map(ns_lookup)
m_fix["strength_delta"] = m_fix["home_strength"] - m_fix["away_strength"]
m_fix["actual_margin"] = m_fix["home_score"] - m_fix["away_score"]

valid = m_fix.dropna(subset=["strength_delta", "actual_margin"])
if len(valid) >= 5:
    corr = valid[["strength_delta", "actual_margin"]].corr().iloc[0, 1]
    md(f"\n`nation_strength_delta` (home − away) vs `actual_margin` (home − away goals): "
       f"**corr = {corr:.3f}** across {len(valid)} closed fixtures.")
    # Top 5 divergences (model said one thing, reality another)
    valid = valid.copy()
    valid["divergence"] = (np.sign(valid["strength_delta"]) != np.sign(valid["actual_margin"])).astype(int)
    upsets = valid[valid["divergence"] == 1].nlargest(5, "actual_margin", keep="all")
    if len(upsets):
        md("\n**Fixtures where the composite was wrong** (upsets the model missed):")
        md(df_to_md(upsets[["home_nation_id", "away_nation_id", "home_score",
                             "away_score", "strength_delta"]]))
else:
    md(f"_Insufficient closed fixtures ({len(valid)})._")

# Also surface the composite top/bottom
md("\n**Top 5 nations by composite strength**:")
md(df_to_md(ns.nlargest(5, "nation_total_strength")[
    ["nation_id", "fifa_rank", "trophies_won", "nation_total_strength",
     "i1_static", "i2_form", "i4_player"]]))
md("\n**Bottom 5 nations**:")
md(df_to_md(ns.nsmallest(5, "nation_total_strength")[
    ["nation_id", "fifa_rank", "nation_total_strength"]]))


# ─── Write the report ────────────────────────────────────────────────────────


print("[done] Writing report...")
report_path = EDA_DIR / "recommender_factor_signal.md"
report_path.write_text("\n".join(REPORT), encoding="utf-8")
print(f"  wrote {report_path}")
print(f"  wrote {EDA_DIR / 'closed_rounds_cache.parquet'}")
print(f"  wrote {EDA_DIR / 'archetypes_group_stage.json'}")
