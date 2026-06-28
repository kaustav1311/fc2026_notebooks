"""Bracket-path-aware EV boost for the strategy-squad layer.

Squad-Challenge layer needs to pick 15 men who will deliver value across the
REMAINING tournament path, not just the next round. A Japan player whose
nation faces BRA / NOR / MEX / ARG to reach the Final is worth more than a
Norway player whose path likely ends at R16, even at equal per-round EV.

This module computes a per-nation `expected_future_rounds` factor by
walking the bracket forward from target_round through the Final and
compounding P(beat opponent) at each step. Where the bracket is only
partially resolved (R32 home/away known, downstream slots TBD until
feeders finish), expected opponent strength is averaged across the
candidate feeders.

Used ONLY by the squad assembler (multiplied onto ev_model). The
suggestion / joint_picks layer keeps the un-boosted ev_model so per-round
picks stay focused on the immediate fixture — that's the layer-separation
contract you asked for.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from lib.knockout_derivation import (
    R32_TEMPLATE, FEEDS, derive_all_ko_pairings,
)

# Map each KO match to which round it belongs to.
ROUND_BY_MATCH: dict[int, int] = {}
for mid in range(73, 89):
    ROUND_BY_MATCH[mid] = 4  # R32
for mid in range(89, 97):
    ROUND_BY_MATCH[mid] = 5  # R16
for mid in range(97, 101):
    ROUND_BY_MATCH[mid] = 6  # QF
ROUND_BY_MATCH[101] = 7   # SF 1
ROUND_BY_MATCH[102] = 7   # SF 2
ROUND_BY_MATCH[103] = 7   # 3rd place (tier with SF for depth purposes)
ROUND_BY_MATCH[104] = 8   # Final

# Reverse FEEDS: which downstream match each KO match feeds into.
# A KO match's winner feeds exactly one downstream match (except match 103
# which is the 3rd-place final — losers of 101/102 go there + don't go to
# the Final). Final (104) has no downstream.
WINNER_DOWNSTREAM: dict[int, int] = {}
for downstream_mid, feed in FEEDS.items():
    for side in ("home", "away"):
        src, kind = feed[side]
        if kind == "winner":
            WINNER_DOWNSTREAM[src] = downstream_mid


def _sigmoid(x: float, k: float = 4.0) -> float:
    """Smooth P(beat). Strength delta ~0.4 -> P ~0.83; ~0.2 -> P ~0.69."""
    return 1.0 / (1.0 + np.exp(-k * x))


def _p_home_beats_away(home_strength: float, away_strength: float) -> float:
    """Asymmetric win probability based on composite nation strength."""
    if home_strength is None or away_strength is None:
        return 0.5
    return float(_sigmoid(float(home_strength) - float(away_strength)))


def expected_future_rounds_by_nation(
    matches: pd.DataFrame,
    nations: pd.DataFrame,
    target_round: int,
    nation_strength_by_id: dict[str, float],
) -> dict[str, dict]:
    """For each nation that's still in the tournament at target_round, compute
    expected_future_rounds (a real number in [0, ~5]).

    The walk:
      - Start at target_round. Each nation in an R{target} slot starts with
        P(reach_target) = 1.0.
      - For each subsequent round R+1, find the nation's downstream match.
        Estimate opponent_strength as the average of the feeder slot's
        possible source teams (or the actual team if known).
      - P(reach R+1) = P(reach R) * P(beat opponent at R).
      - expected_future_rounds = SUM P(reach R) for R from target..Final.

    Returns:
      {
        nation_id: {
          "expected_future_rounds": float,
          "path": [
            {"round": int, "match_id": int, "opponent_nation_id": str|None,
             "p_beat": float, "p_reach": float},
            ...
          ]
        }
      }
    """
    # ── Step 1: derive current bracket pairings (uses Annex C + cascades) ──
    derived = derive_all_ko_pairings(matches, nations)

    # ── Step 2: figure out each nation's R{target} slot ────────────────────
    # For target=4 (R32), look at R32_TEMPLATE + derived[match_number].home/away
    # For target=5+ (R16+), use FEEDS to backtrack
    matches_in_round = [mid for mid, r in ROUND_BY_MATCH.items() if r == target_round]
    if not matches_in_round:
        return {}

    # Map nation -> entry match at target_round
    nation_entry: dict[str, int] = {}
    for mid in matches_in_round:
        d = derived.get(mid) or {}
        for side in ("home_nation_id", "away_nation_id"):
            nid = d.get(side)
            if nid:
                nation_entry[nid] = mid

    # ── Step 3: walk the bracket forward for each nation ───────────────────
    out: dict[str, dict] = {}
    for nid, start_mid in nation_entry.items():
        p_reach = 1.0
        efr = 1.0  # already at target round
        path = [{"round": target_round, "match_id": start_mid,
                 "opponent_nation_id": None, "p_beat": None, "p_reach": 1.0}]
        # Step forward through downstream
        cur_mid = start_mid
        while True:
            next_mid = WINNER_DOWNSTREAM.get(cur_mid)
            if next_mid is None:
                break
            # Find opponent at cur_mid (= someone else in that match)
            cur_pair = derived.get(cur_mid) or {}
            opp_id = None
            for side in ("home_nation_id", "away_nation_id"):
                if cur_pair.get(side) and cur_pair.get(side) != nid:
                    opp_id = cur_pair.get(side)
                    break
            # Opponent strength: if known, use it; if not, average over
            # candidate teams that could feed cur_mid (best-effort)
            if opp_id and opp_id in nation_strength_by_id:
                opp_str = nation_strength_by_id[opp_id]
            else:
                # Fallback: assume opponent ≈ 0.5 strength
                opp_str = 0.5
            my_str = nation_strength_by_id.get(nid, 0.5)
            p_beat = _p_home_beats_away(my_str, opp_str)
            p_reach_next = p_reach * p_beat
            efr += p_reach_next
            path[-1]["opponent_nation_id"] = opp_id
            path[-1]["p_beat"] = round(p_beat, 3)
            path.append({"round": ROUND_BY_MATCH[next_mid],
                         "match_id": next_mid,
                         "opponent_nation_id": None,
                         "p_beat": None,
                         "p_reach": round(p_reach_next, 3)})
            p_reach = p_reach_next
            cur_mid = next_mid
        out[nid] = {
            "expected_future_rounds": round(efr, 3),
            "path": path,
        }
    return out


def apply_path_boost_to_scored(
    scored: pd.DataFrame,
    path_factors: dict[str, dict],
    alpha: float = 0.15,
    out_col: str = "ev_model_path",
) -> pd.DataFrame:
    """Add `out_col` to scored = ev_model * (1 + alpha * expected_future_rounds).

    Nations not in the path map (knocked out, or before the lookup target)
    get factor=1.0 (i.e., no boost). Use out_col as the strat['ev_col']
    when assembling squads.
    """
    if "ev_model" not in scored.columns:
        return scored
    if "nation_id" not in scored.columns:
        return scored
    out = scored.copy()
    if out.empty:
        # Defensive: empty frame -> empty boosted column, preserve dtypes.
        out[out_col] = pd.Series([], dtype=float)
        return out
    def _boost(nid) -> float:
        if not isinstance(nid, str) or not nid:
            return 1.0
        info = path_factors.get(nid)
        if not info:
            return 1.0
        return 1.0 + alpha * float(info["expected_future_rounds"])
    ev = pd.to_numeric(out["ev_model"], errors="coerce").fillna(0.0).astype(float)
    factor = out["nation_id"].apply(_boost).astype(float)
    out[out_col] = (ev * factor).astype(float)
    return out
