"""Author 09_player_season_stats.ipynb."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 09 — Per-player FIFA **season** aggregates

Pulls `fdh-api.fifa.com/v1/stats/season/285023/players.json` — same 119-stat-key vocabulary as the per-match endpoint, but rolled up across every WC26 match the player has appeared in. This is the canonical "tournament-stats" view the audit-doc cited from FotMob — FIFA's version is a strict superset.

- Single call, ~4 MB JSON.
- 1022 players in this snapshot (only players who've actually appeared).
- Long format: one row per `(fifa_player_id, stat_name)` × `value`.
- Re-fetch with `force_refresh=True` after each round of matches lands.""")

code("""import sys, json
from pathlib import Path
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import io, events
""")

md("## 1. Fetch + parse")

code("""# Event-A: FDH season stats only changes when a new match finishes. If no
# match newly finished this tick, serve from cache.
try:
    matches = io.load_table("wc26_matches")
    new_mids = events.newly_finished_matches(matches)
except FileNotFoundError:
    new_mids = []
force = bool(new_mids) or events.is_first_run()
print(f"event-A: {len(new_mids)} newly-finished matches → force_refresh={force}")

data = io.cache_raw(
    "https://fdh-api.fifa.com/v1/stats/season/285023/players.json",
    source="fdh", name="stats_season_285023_players",
    force_refresh=force if force else False,
)
if force:
    events.stamp_fetch("fdh_season_stats", "285023")
    events.save()

rows = []
for pid, stats in data.items():
    if not isinstance(stats, list):
        continue
    for entry in stats:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        rows.append({
            "fifa_player_id": int(pid),
            "stat_name": entry[0],
            "value": entry[1],
        })
df = pd.DataFrame(rows)
print(f"rows: {len(df):,}  players: {df['fifa_player_id'].nunique()}  stat keys: {df['stat_name'].nunique()}")
""")

md("## 2. FK + save")

code("""# wc26_player_season_stats and wc26_player_season_stats_wide are dropped from
# the warehouse contract — to be replaced by the new player-aggregate table.
# Notebook left in place so the fdh-api season payload still goes through the
# raw cache for future use, but no processed tables are written.
print("season-stats processed tables disabled (warehouse contract change).")
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("09_player_season_stats.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 09_player_season_stats.ipynb")
