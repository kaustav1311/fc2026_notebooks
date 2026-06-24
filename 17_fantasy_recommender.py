"""Phase C — Fantasy recommender (per-round).

Runs after notebook 16 on the hourly tick. Produces:
  - data/processed/wc26_fantasy_recommendations.parquet
  - data/processed/wc26_fantasy_recommendations.json
  - data/eda/archetypes_retrospective_v2.json
  - data/eda/archetypes_prospective_v2.json

The PWA consumes the JSON via _emit_pwa_json.py.

Target round: the first `active` or `upcoming` round in fantasy_rounds.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from lib.recommender import (
    assemble_fixture_profile,
    score_players_brackets,
    mine_archetypes_v2,
    attach_archetypes,
    apply_filters,
    tag_anti_picks,
    assemble_strategy_squad,
    build_position_suggestor,
    STRATEGIES,
)

PROC = ROOT / "data" / "processed"
PWA_JSON = PROC / "json"
PWA_JSON.mkdir(parents=True, exist_ok=True)
EDA_DIR = ROOT / "data" / "eda"


def pick_target_round() -> int:
    """Pick the next round that hasn't started yet, OR the currently playing
    round if its end_date is still in the future."""
    fr = pd.read_parquet(PROC / "fantasy_rounds.parquet")
    now = pd.Timestamp.now(tz="UTC")
    fr["start"] = pd.to_datetime(fr["start_date"], utc=True, errors="coerce")
    fr["end"] = pd.to_datetime(fr["end_date"], utc=True, errors="coerce")
    # Playing AND not yet ended
    live = fr[(fr["status"] == "playing") & (fr["end"] > now)]
    if not live.empty:
        return int(live.iloc[0]["round_id"])
    # Otherwise next scheduled
    sched = fr[fr["start"] > now].sort_values("start")
    if not sched.empty:
        return int(sched.iloc[0]["round_id"])
    # Otherwise the latest known
    return int(fr["round_id"].max())


def to_json_safe(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if np.isnan(v) else float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if pd.isna(v) if not isinstance(v, (list, dict)) else False:
        return None
    return v


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
                    d[col] = to_json_safe(val)
            except (TypeError, ValueError):
                d[col] = val
        out.append(d)
    return out


def sanitize_for_js(obj):
    """Recursively walk a dict/list and replace NaN/Infinity floats with None.

    JavaScript's JSON.parse() throws SyntaxError on `NaN` and `Infinity`
    tokens — they're not in the JSON spec — while Python's json.dumps writes
    them by default. Anything bound for the PWA must be sanitized first.
    """
    import math
    if isinstance(obj, dict):
        return {k: sanitize_for_js(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_js(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, np.floating):
        f = float(obj)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, pd.Timestamp):
        return None if pd.isna(obj) else obj.isoformat()
    # pd.NA / np.nan that survived as scalar
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    return obj


def dump_js_safe(obj) -> str:
    """JSON dump that's guaranteed JS-parseable: no NaN, no Infinity."""
    return json.dumps(sanitize_for_js(obj), default=str, allow_nan=False)


def main():
    snapshot_ts = datetime.now(timezone.utc).isoformat()
    target = pick_target_round()
    print(f"[17] target round: {target}  (snapshot {snapshot_ts})")

    # 1. Archetypes
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

    # 2. Fixture profiles
    print("[17] assembling fixture profiles…")
    fx = assemble_fixture_profile(target)
    print(f"[17]   {len(fx)} fixtures for round {target}")

    # 3. Player scoring via 5-bracket Player Strength Score model
    print("[17] scoring players via bracket model (B1-B5)…")
    scored = score_players_brackets(target, fx)

    # 4. Archetype enrichment
    scored = attach_archetypes(scored, retro, prospective)

    # 5. Filters
    scored = apply_filters(scored)
    print(f"[17]   {len(scored)} player-fixture rows after filters")

    # 6. Anti-pick tagging (D-3)
    scored = tag_anti_picks(scored)

    # 7. Tagging reason chips
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
    scored["reason_chips"] = scored.apply(tag_chips, axis=1)

    # 8. Persist
    scored["snapshot_ts"] = snapshot_ts
    scored["target_round_id"] = target

    out_parquet = PROC / "wc26_fantasy_recommendations.parquet"
    scored.to_parquet(out_parquet, index=False)
    print(f"[17]   wrote {out_parquet} ({len(scored)} rows)")

    # JSON output — slim down to client-facing fields
    client_cols = [
        "target_round_id", "fantasy_player_id", "fifa_player_id", "nation_id",
        "opponent_nation_id", "is_home", "position", "price", "percent_selected",
        "known_name", "first_name", "last_name",
        "sb_total", "form", "avg_points", "total_points", "last_round_points",
        "start_prob", "differential", "anti_pick",
        # Bracket model (B1-B5 + composite)
        "b1_overall", "b2_wc_perf", "b3_external", "b4_fantasy",
        "b5_fixture_mult", "bracket_sum", "ev_bracket",
        # Back-compat aliases for the existing assembler / suggestor / UI
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
    client_cols = [c for c in client_cols if c in scored.columns]
    records = df_to_records(scored[client_cols])
    out_json = PROC / "wc26_fantasy_recommendations.json"
    safe_recs = dump_js_safe(records)
    out_json.write_text(safe_recs)
    (PWA_JSON / "wc26_fantasy_recommendations.json").write_text(safe_recs)
    print(f"[17]   wrote {out_json} + PWA copy ({len(records)} rows)")

    # ─── Phase D: position suggestor + strategy squads ──────────────────────
    print("[17] building position suggestor (Top 15 + Look out for)…")
    suggestor = build_position_suggestor(scored)
    suggestor["target_round_id"] = target
    suggestor["snapshot_ts"] = snapshot_ts
    sug_path = PROC / "wc26_fantasy_position_suggestor.json"
    safe_sug = dump_js_safe(suggestor)
    sug_path.write_text(safe_sug)
    (PWA_JSON / "wc26_fantasy_position_suggestor.json").write_text(safe_sug)
    print(f"[17]   wrote {sug_path}  top15={len(suggestor['top_15_overall'])}  "
          f"look_out_for={sum(len(v) for v in suggestor['look_out_for'].values())}")

    print("[17] assembling 3 strategy squads (S1=9 / S2=5 / S3=12 diff)…")
    squads = []
    for strat in STRATEGIES:
        sq = assemble_strategy_squad(scored, strat, budget_m=100.0, max_per_nation=3)
        sq_unbud = assemble_strategy_squad(scored, strat, budget_m=100.0,
                                            max_per_nation=3, non_budget=True)
        sq["unbudgeted_variant"] = sq_unbud
        sq["target_round_id"] = target
        sq["snapshot_ts"] = snapshot_ts
        squads.append(sq)
        print(f"[17]   {strat['id']}: formation={sq['formation']}  £{sq['budget_spent_m']:.1f}m  "
              f"SB-band={sq['sb_band_count']}/{strat['sb_quota']}+  proj={sq['projected_pts_with_captain']:.1f}")

    sqd_path = PROC / "wc26_fantasy_strategy_squads.json"
    safe_sq = dump_js_safe(squads)
    sqd_path.write_text(safe_sq)
    (PWA_JSON / "wc26_fantasy_strategy_squads.json").write_text(safe_sq)
    print(f"[17]   wrote {sqd_path}  ({len(squads)} strategies)")

    # ─── Historical snapshot (pre-round-lock freeze) ───────────────────────
    # Every tick writes a timestamped snapshot. After a round completes, the
    # final pre-lock snapshot (newest with timestamp BEFORE round start) is the
    # "committed" prediction for round-tracking. Phase F joins this against
    # fantasy_player_round_stats to produce per-strategy round actuals.
    print("[17] writing historical round-lock snapshot…")
    history_dir = PROC / "history" / f"round_{target:02d}"
    history_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = snapshot_ts.replace(":", "-").replace("+00:00", "Z").split(".")[0]
    snap_path = history_dir / f"snapshot_{safe_ts}.json"
    snap_path.write_text(json.dumps({
        "round_id": target,
        "snapshot_ts": snapshot_ts,
        "model_version": "brackets_v1",  # bumps on scoring-formula changes
        "recommendations": records,
        "position_suggestor": suggestor,
        "strategy_squads": squads,
    }, default=str))
    print(f"[17]   wrote {snap_path} ({snap_path.stat().st_size // 1024} KB)")

    return scored


if __name__ == "__main__":
    main()
