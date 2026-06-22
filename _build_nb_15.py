"""Author 15_staging_matches.ipynb."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 15 — `wc26_stg_matches`

One wide row per WC26 fixture (104 expected, fewer during the bracket-fill phase). Pure pandas — no network calls. Inputs are already-landed parquets so this notebook is cheap to rerun on every 3 h tick.

Joins onto the `wc26_matches` base, in this order:

1. **Stadium fields** from `wc26_stadiums` on `stadium_id` → `stadium_capacity, roof_type, surface`.
2. **Weather fields** from `wc26_match_weather` on `espn_match_id` → `local_date, local_hour, temperature_c, apparent_temp_c, humidity_pct`.
3. **Home / away nation fields** from `wc26_nations` (prefixed `home_` / `away_`) → confederation, FIFA rank, valuation, host flag, cross-source team IDs, alias union.
4. **Referee fields** from `referee_master` via the `ref_id_bridge` parquet (built in `04_referees`).
5. **Polymarket volume** from `wc26_polymarket_match_volume` on `espn_match_id` → `volume_moneyline, volume_other`. (These map to the spec's `$Volume Moneyline` / `$Volume Exotic Bets`; column names kept aligned to the source so we don't fork the warehouse vocabulary.)

Output: `wc26_stg_matches`. Refresh bucket: 🟥 every 3 h.""")

code("""import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import io

matches    = io.load_table("wc26_matches")
stadiums   = io.load_table("wc26_stadiums")
weather    = io.load_table("wc26_match_weather")
nations    = io.load_table("wc26_nations")
ref_master = io.load_table("referee_master")
polymarket = io.load_table("wc26_polymarket_match_volume")

bridge_pq  = io.PROCESSED / "ref_id_bridge.parquet"
if bridge_pq.exists():
    ref_bridge = io.load_table("ref_id_bridge")
else:
    print("WARN: ref_id_bridge.parquet not found — referee columns will be NaN. Run 04_referees to build the bridge.")
    ref_bridge = pd.DataFrame(columns=["fifa_referee_id", "referee_id", "match_method"])

print(f"matches: {len(matches)}  stadiums: {len(stadiums)}  weather: {len(weather)}  nations: {len(nations)}")
print(f"ref_master: {len(ref_master)}  polymarket: {len(polymarket)}  ref_bridge: {len(ref_bridge)}")
""")

md("""## 1. Join stadium fields""")

code("""stadium_cols = stadiums[["stadium_id", "capacity", "roof_type", "surface"]].rename(
    columns={"capacity": "stadium_capacity"}
)
stg = matches.merge(stadium_cols, on="stadium_id", how="left")
print(f"after stadium join: {len(stg)} rows")
""")

md("""## 2. Join weather fields (per-match)""")

code("""weather_cols = weather[[
    "espn_match_id", "local_date", "local_hour",
    "temperature_c", "apparent_temperature_c", "humidity_pct",
]].rename(columns={"apparent_temperature_c": "apparent_temp_c"})

# Defensive: collapse to one row per espn_match_id (schema scan confirms it's already
# row-per-match today; guard for future schema drift).
weather_cols = weather_cols.drop_duplicates(subset=["espn_match_id"], keep="first")

stg = stg.merge(weather_cols, on="espn_match_id", how="left")
n_with_weather = stg["temperature_c"].notna().sum()
print(f"after weather join: {len(stg)} rows ({n_with_weather} with weather data)")
""")

md("""## 3. Join home + away nation fields

Spec says: replicate every nation field for the home team and for the away team. We carry the cross-source IDs + ranking + valuation + alias union but skip the FIFA Fantasy/TM/ESPN textual fields that already live elsewhere.""")

code("""# Columns to carry from wc26_nations to BOTH home and away slots.
nation_carry = [
    "confederation", "group", "pot", "fifa_rank", "squad_valuation_m_eur",
    "is_host", "espn_team_id", "fotmob_team_id", "tm_team_id", "all_names",
]

def prefix_nation(side):
    \"\"\"Return a copy of nations with cols renamed for the home_ or away_ side.\"\"\"
    sub = nations[["nation_id"] + nation_carry].copy()
    sub = sub.rename(columns={c: f"{side}_{c}" for c in nation_carry})
    sub = sub.rename(columns={"nation_id": f"{side}_nation_id"})
    return sub

stg = stg.merge(prefix_nation("home"), on="home_nation_id", how="left")
stg = stg.merge(prefix_nation("away"), on="away_nation_id", how="left")
print(f"after nation joins: {len(stg)} rows")
n_missing_home = stg["home_fifa_rank"].isna().sum()
n_missing_away = stg["away_fifa_rank"].isna().sum()
print(f"  home_fifa_rank missing: {n_missing_home} (expected: knockout TBD placeholders)")
print(f"  away_fifa_rank missing: {n_missing_away}")
""")

md("""## 4. Join referee fields via the bridge

`wc26_matches.fifa_referee_id` (numeric FIFA OfficialId) → `ref_id_bridge.referee_id` (slug) → `referee_master` fields. Drop the bridge's intermediate `match_method` column from the output.""")

code("""ref_carry = ["referee_id", "confederation", "flag_iso", "nation_id", "fm_id", "slug", "fm_url"]
ref_pref = ref_master[ref_carry].rename(columns={c: f"referee_{c}" for c in ref_carry})

# Bridge: (fifa_referee_id) → referee_id. Then merge the prefixed master fields on referee_id.
bridge_cols = ref_bridge[["fifa_referee_id", "referee_id"]].dropna(subset=["fifa_referee_id"]).drop_duplicates(subset=["fifa_referee_id"])

# fifa_referee_id types must match for the merge to land — coerce both sides to string.
stg["_fifa_referee_id_str"]    = stg["fifa_referee_id"].astype("string")
bridge_cols["_fifa_referee_id_str"] = bridge_cols["fifa_referee_id"].astype("string")

stg = stg.merge(
    bridge_cols[["_fifa_referee_id_str", "referee_id"]].rename(columns={"referee_id": "referee_referee_id"}),
    on="_fifa_referee_id_str", how="left",
).drop(columns=["_fifa_referee_id_str"])

# Now bring in the rest of the master columns, joined on the bridge's referee_id.
stg = stg.merge(ref_pref, left_on="referee_referee_id", right_on="referee_referee_id", how="left")

n_with_ref = stg["referee_referee_id"].notna().sum()
print(f"after referee join: {len(stg)} rows ({n_with_ref} with referee_referee_id resolved)")
""")

md("""## 5. Join Polymarket volume

`wc26_polymarket_match_volume` is one row per `espn_match_id`. Keep source column names (`volume_moneyline`, `volume_other`) — the design spec's `$Volume Moneyline` / `$Volume Exotic Bets` map 1:1 onto these two.""")

code("""poly_cols = polymarket[["espn_match_id", "volume_moneyline", "volume_other"]].drop_duplicates(subset=["espn_match_id"])
stg = stg.merge(poly_cols, on="espn_match_id", how="left")
n_with_poly = stg["volume_moneyline"].notna().sum()
print(f"after polymarket join: {len(stg)} rows ({n_with_poly} with polymarket volume)")
""")

md("## 6. Sanity checks + save")

code("""assert stg["espn_match_id"].dropna().is_unique, "espn_match_id should be unique per match"
assert len(stg) == len(matches), f"row count drift: {len(stg)} vs base {len(matches)} — left-join inflated rows"

# Column health.
key_nullables = {
    "stadium_capacity": "every scheduled match has a venue",
    "home_fifa_rank":   "every group-stage match has a known home nation",
}
for col, desc in key_nullables.items():
    n_null = stg[col].isna().sum()
    print(f"  {col} NaN: {n_null}  ({desc})")

print(f"\\nwc26_stg_matches: {len(stg)} rows, {len(stg.columns)} cols")
io.save_table(stg, "wc26_stg_matches")
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("15_staging_matches.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 15_staging_matches.ipynb")
