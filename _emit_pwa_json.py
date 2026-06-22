"""Emit selected staging tables as compact JSON for the PWA repo to consume.

Reads parquets from data/processed/ and writes JSON to data/processed/json/.
The PWA's scripts/sync-warehouse.mjs copies those files into public/data/.

Add table names to EMIT as the PWA grows. NaN -> null, numpy scalars -> py,
list/ndarray cells -> native arrays, pd.Timestamp -> ISO 8601 string.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "processed" / "json"
OUT.mkdir(parents=True, exist_ok=True)

EMIT: list[str] = [
    # Round 1: referee profile feeds the new nested RefereeProfileSheet.
    # The staging table already joins referee_master columns (name, country,
    # confederation, flag_iso, fm_url, ...), so no separate master file.
    "wc26_stg_referee_profile",
    # Round 1.1: stg_matches gives the PWA a direct fixture -> referee_slug
    # bridge (via fifa_referee_id and ref_id_bridge, both joined in already).
    # That bypasses FotMob name-string drift (e.g. "Benitez" vs "Benitez Mareco").
    "wc26_stg_matches",
    # Round 2: small staging tables — used by the StadiumSheet (stadiums,
    # weather) and as a name/flag lookup index (nations).
    "wc26_stg_nations",
    "wc26_stg_stadiums",
    "wc26_match_weather",
    # Round 2.1: per-match power ranks; will hydrate the Player Profile rework
    # later but the index is harmless to ship now.
    "wc26_player_match_powerrank",
    # Round 2.2: Fantasy Pool tab feeds — totals + the latest live snapshot
    # (fantasy_players also has /api/fifa-fantasy live overlay).
    "wc26_stg_fantasy_player_totals",
    "fantasy_players",
]

# Slim emit: only a subset of columns for very wide tables. wc26_stg_players is
# 1248 x 190; the PWA only needs identity / face / club / nation fields today.
# Full emit can be added later when the player profile rework needs the deep
# stat columns.
SLIM_EMIT: dict[str, list[str]] = {
    "wc26_stg_players": [
        "fifa_player_id",
        "fotmob_player_id",
        "nation_id",
        "name",
        "short_name",
        "position",
        "real_position",
        "real_position_side",
        "picture_url",
        "current_club_name",
        "current_club_fotmob_id",
        "club_name",
        "club_fotmob_id",
        "club_tm_id",
    ],
}


def to_jsonable(v):
    if v is None:
        return None
    if isinstance(v, float):
        return None if math.isnan(v) else v
    if isinstance(v, np.floating):
        f = float(v)
        return None if math.isnan(f) else f
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, (np.ndarray, list, tuple)):
        return [to_jsonable(x) for x in v]
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.isoformat()
    return v


def emit(name: str, columns: list[str] | None = None) -> Path:
    src = ROOT / "data" / "processed" / f"{name}.parquet"
    df = pd.read_parquet(src)
    if columns:
        # keep only the requested columns (any missing are skipped silently
        # so a schema add doesn't break the emit; warn instead).
        present = [c for c in columns if c in df.columns]
        missing = [c for c in columns if c not in df.columns]
        if missing:
            print(f"  WARN: {name} missing columns {missing}")
        df = df[present]
        dst_name = f"{name}.json"
    else:
        dst_name = f"{name}.json"
    rows = [{k: to_jsonable(v) for k, v in r.items()} for r in df.to_dict("records")]
    dst = OUT / dst_name
    dst.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    size_kb = dst.stat().st_size / 1024
    suffix = f" [slim {len(present)}c]" if columns else ""
    print(f"wrote json/{dst_name}  ({len(rows)} rows, {size_kb:.1f} KB){suffix}")
    return dst


if __name__ == "__main__":
    for name in EMIT:
        emit(name)
    for name, cols in SLIM_EMIT.items():
        emit(name, cols)
