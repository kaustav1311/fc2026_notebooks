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
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

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
    build_position_suggestor,
    refresh_live_percent_selected,
    MODEL_REGISTRY,
)

PROC = ROOT / "data" / "processed"
PWA_JSON = PROC / "json"
PWA_JSON.mkdir(parents=True, exist_ok=True)
EDA_DIR = ROOT / "data" / "eda"


def pick_target_round() -> int:
    """Pick the round currently in [start, end] — even if the parquet's
    status hasn't flipped to 'playing' yet (FIFA's status field lags). Then
    fall back to next scheduled, then latest known. Also REQUIRES the round
    to have fixtures: scheduled-but-no-fixtures rounds (R32 TBD until the
    group stage finishes) get skipped to the prior playable round.
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
    # Otherwise the most recently completed
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


def model_to_strategy(model_id: str, cfg: dict) -> dict:
    """Translate a MODEL_REGISTRY entry to a strategy dict the assembler accepts."""
    return {
        "id": model_id,
        "name": cfg["name"],
        "blurb": cfg["blurb"],
        "sb_quota": cfg["sb_quota"],
        # The assembler picks up ev_col first; the α/β/γ fields are ignored
        # but kept for legacy code paths that may still read them.
        "ev_col": "ev_model",
        "alpha_floor": 0.0,
        "beta_ceiling": 0.0,
        "gamma_differential": 0.0,
        "fixture_filter": None,
        "anti_pick_allowed": True,
    }


def main():
    snapshot_ts = datetime.now(timezone.utc).isoformat()
    target = pick_target_round()
    print(f"[17] target round: {target}  (snapshot {snapshot_ts})")

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

    # 1b. Refresh live %selected — bypasses the warehouse snapshot so the
    # scoring's SB / differential math sees the same ownership the PWA does.
    print("[17] fetching live FIFA Fantasy %selected…")
    live_pct = refresh_live_percent_selected(force=True)
    print(f"[17]   live %selected map: {len(live_pct)} players")

    # 2. Fixture profiles
    print("[17] assembling fixture profiles…")
    fx = assemble_fixture_profile(target)
    print(f"[17]   {len(fx)} fixtures for round {target}")

    # 3. Score under each model
    print(f"[17] scoring under {len(MODEL_REGISTRY)} models…")
    model_outputs: dict[str, pd.DataFrame] = {}
    for mid, cfg in MODEL_REGISTRY.items():
        scored = score_for_model(target, fx, mid, live_pct_selected=live_pct)
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
    print(f"[17] assembling {len(MODEL_REGISTRY)} challenge squads…")
    squads = []
    for mid, cfg in MODEL_REGISTRY.items():
        strat = model_to_strategy(mid, cfg)
        # Thread target round into strat so the assembler's plan_chips() can
        # snap chip rounds forward past the current FIFA lock window.
        strat["target_round_id"] = target
        scored = model_outputs[mid]
        assembler_kind = cfg.get("assembler", "default")
        if assembler_kind == "sb_hunter":
            sq = assemble_sb_hunter_squad(scored, strat, budget_m=100.0, max_per_nation=3)
            sq_unbud = assemble_sb_hunter_squad(scored, strat, budget_m=100.0,
                                                  max_per_nation=3, non_budget=True)
        else:
            sq = assemble_strategy_squad(scored, strat, budget_m=100.0, max_per_nation=3)
            sq_unbud = assemble_strategy_squad(scored, strat, budget_m=100.0,
                                                max_per_nation=3, non_budget=True)
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
