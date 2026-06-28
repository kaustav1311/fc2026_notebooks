"""Apply lib.knockout_derivation to wc26_stg_matches.parquet in place.

Run after nb_03 finishes (which writes the matches table from ESPN + seed).
Reads matches + nations, derives every KO pairing it can from finished
group standings + Annex C lookup, writes the updated parquet back.

Idempotent: only fills NULL home_nation_id / away_nation_id. Pre-populated
host-seed slots (USA / MEX / GER) stay untouched.

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
    if n_filled == 0:
        print(f"[derive_ko] no new nation_ids to write -- no parquet update")
        return 0
    updated.to_parquet(matches_p, index=False)
    print(f"[derive_ko] wrote {matches_p.name} -- filled {n_filled} nation_id cells")
    return 0


if __name__ == "__main__":
    sys.exit(main())
