"""Apply lib.knockout_derivation to wc26_stg_matches.parquet in place AND
bootstrap fantasy_round_matches.parquet R4+ entries from derived pairings.

Run after nb_03 finishes (which writes the matches table from ESPN + seed).
Reads matches + nations, derives every KO pairing it can from finished
group standings + Annex C lookup, writes the updated parquet back.

Idempotent: only fills NULL home_nation_id / away_nation_id. Pre-populated
host-seed slots (USA / MEX / GER) stay untouched.

Also bootstraps fantasy_round_matches with R4 rows the moment R32 pairings
are derived — so the recommender can target R4 without waiting on FIFA
Fantasy's own R4 round publication (which can lag by 12-24h after group
stage closes). Same pattern extends to R5+ as each KO round resolves.

Cron-friendly: silently no-ops if PWA repo (for Annex C lookup) isn't
reachable — without the lookup we can only resolve W/RU slots, not the
8 third-placed assignments.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
PROC = ROOT / "data" / "processed"

sys.path.insert(0, str(ROOT))
from lib.knockout_derivation import derive_all_ko_pairings, apply_to_stg_matches


def main() -> int:
    matches_p = PROC / "wc26_stg_matches.parquet"
    nations_p = PROC / "wc26_stg_nations.parquet"
    if not matches_p.exists():
        print("[derive_ko] wc26_stg_matches.parquet missing -- skip")
        return 0
    if not nations_p.exists():
        print("[derive_ko] wc26_stg_nations.parquet missing -- skip")
        return 0
    matches = pd.read_parquet(matches_p)
    nations = pd.read_parquet(nations_p)

    # Pre-derivation: how many KO rows are currently empty?
    ko_rows = matches[matches["match_number"].astype("Int64").between(73, 104, inclusive="both")]
    before_empty = int(((ko_rows["home_nation_id"].isna() | (ko_rows["home_nation_id"] == "")) |
                        (ko_rows["away_nation_id"].isna() | (ko_rows["away_nation_id"] == ""))).sum())
    print(f"[derive_ko] before: {before_empty}/32 KO rows have missing nation_id")

    derived = derive_all_ko_pairings(matches, nations)
    n_pairs = sum(
        1 for d in derived.values()
        if d.get("home_nation_id") and d.get("away_nation_id")
    )
    print(f"[derive_ko] derived complete pairs: {n_pairs}/32")

    updated, n_filled = apply_to_stg_matches(matches, derived)
    if n_filled > 0:
        updated.to_parquet(matches_p, index=False)
        print(f"[derive_ko] wrote {matches_p.name} -- filled {n_filled} nation_id cells")
    else:
        print(f"[derive_ko] no new nation_ids to write -- stg_matches unchanged")

    # ── Bootstrap fantasy_round_matches R4+ entries from derived KO pairs ──
    # FIFA Fantasy publishes round_matches per round; their R4 publication
    # often lags 12-24h after group stage. The recommender's
    # assemble_fixture_profile reads from fantasy_round_matches, so without
    # R4 rows there's nothing to score against. Once stg_matches has KO
    # pairings (this script just filled them), build the rows ourselves so
    # target_round=4 scoring can start immediately.
    frm_p = PROC / "fantasy_round_matches.parquet"
    fsq_p = PROC / "fantasy_squads.parquet"
    if frm_p.exists() and fsq_p.exists():
        try:
            n_bootstrapped = _bootstrap_fantasy_round_matches(
                updated if n_filled > 0 else matches, derived, frm_p, fsq_p,
            )
            if n_bootstrapped > 0:
                print(f"[derive_ko] bootstrapped {n_bootstrapped} R4+ fantasy_round_matches rows")
        except Exception as exc:  # noqa: BLE001
            print(f"[derive_ko] bootstrap step skipped ({exc})")
    return 0


def _bootstrap_fantasy_round_matches(matches: pd.DataFrame, derived: dict[int, dict],
                                       frm_p: Path, fsq_p: Path) -> int:
    """For each KO round with at least one fully-resolved match, append rows
    to fantasy_round_matches.parquet if not already present.

    fantasy_round_matches schema (minimum): round_id, fantasy_match_id,
    home_squad_id, away_squad_id, date, status, period, minutes, ...
    We only set the FK-shaped columns the recommender actually reads;
    everything else stays NaN / default.
    """
    frm = pd.read_parquet(frm_p)
    fsq = pd.read_parquet(fsq_p)[["fantasy_squad_id", "abbr"]]
    nation_to_squad = dict(zip(fsq["abbr"], fsq["fantasy_squad_id"]))

    # Existing keys to avoid dupes
    existing_keys: set[tuple] = set()
    if "round_id" in frm.columns and "fantasy_match_id" in frm.columns:
        for _, r in frm.iterrows():
            existing_keys.add((int(r["round_id"]), int(r["fantasy_match_id"])))

    # KO round mapping (stg_matches.match_number -> fantasy round_id)
    # Per FIFA: 73-88 = R4 (R32), 89-96 = R5 (R16), 97-100 = R6 (QF),
    # 101-102 = R7 (SF), 103 = R7 (3rd place), 104 = R8 (Final).
    def _ko_round(mn: int) -> int | None:
        if 73 <= mn <= 88: return 4
        if 89 <= mn <= 96: return 5
        if 97 <= mn <= 100: return 6
        if 101 <= mn <= 103: return 7
        if mn == 104: return 8
        return None

    new_rows = []
    matches_idx = matches.set_index("match_number") if "match_number" in matches.columns else matches
    for mn, pair in derived.items():
        rid = _ko_round(int(mn))
        if rid is None:
            continue
        home_nid = pair.get("home_nation_id")
        away_nid = pair.get("away_nation_id")
        if not home_nid or not away_nid:
            continue
        if home_nid not in nation_to_squad or away_nid not in nation_to_squad:
            continue
        if (rid, int(mn)) in existing_keys:
            continue
        # Pull date/venue from stg_matches if present
        date = None
        try:
            row = matches_idx.loc[int(mn)]
            ko_utc = row.get("kickoff_utc")
            if ko_utc is not None and not pd.isna(ko_utc):
                date = pd.to_datetime(ko_utc, utc=True, errors="coerce")
        except Exception:
            pass
        new_rows.append({
            "round_id": int(rid),
            "fantasy_match_id": int(mn),
            "home_squad_id": int(nation_to_squad[home_nid]),
            "away_squad_id": int(nation_to_squad[away_nid]),
            "date": date,
            "status": "scheduled",
        })

    if not new_rows:
        return 0
    new_df = pd.DataFrame(new_rows)
    # Align schemas — fill any frm columns we didn't set with NaN.
    for col in frm.columns:
        if col not in new_df.columns:
            new_df[col] = pd.NA
    new_df = new_df[frm.columns] if all(c in new_df.columns for c in frm.columns) else new_df
    out = pd.concat([frm, new_df], ignore_index=True)
    out.to_parquet(frm_p, index=False)
    return len(new_rows)


if __name__ == "__main__":
    sys.exit(main())
