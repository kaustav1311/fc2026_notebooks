"""Author 06_players.ipynb — wc26_players from FIFA squad endpoint."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 06 — `wc26_players`

Canonical player universe sourced directly from FIFA's official squad endpoint.

- Discovery: FIFA team IDs come from `wc26_matches.fifa_home_team_id` / `fifa_away_team_id` (populated in Notebook 03).
- Per-team: `api.fifa.com/api/v3/teams/{IdTeam}/squad?idCompetition=17&idSeason=285023` — 26 players each + the team's officials (coach + assistants).
- Output: `wc26_players` (~1250 rows) + `wc26_team_officials` (~150 rows, coaches/assistants).""")

code("""import sys, json
from pathlib import Path
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import io, events

matches = io.load_table("wc26_matches")
nations = io.load_table("wc26_nations")
print(f"matches: {len(matches)}  nations: {len(nations)}")

# Event-C scoping: only re-fetch FIFA squad pages for nations that played a
# newly-finished match (catches roster/jersey adjustments around games). Cold
# nations serve from cache. First run / FORCE_ALL_EVENTS=1 → all teams.
new_mids = events.newly_finished_matches(matches)
EVENT_NIDS: set = events.teams_in_matches(new_mids, matches)
FIRST_RUN = events.is_first_run()
print(f"event-C: {len(new_mids)} new matches → {len(EVENT_NIDS)} teams need squad refresh")
""")

md("## 1. Resolve FIFA team IDs for the 48 nations")

code("""home = matches.dropna(subset=["fifa_home_team_id"])[["home_nation_id", "fifa_home_team_id"]].rename(
    columns={"home_nation_id": "nation_id", "fifa_home_team_id": "fifa_team_id"}
)
away = matches.dropna(subset=["fifa_away_team_id"])[["away_nation_id", "fifa_away_team_id"]].rename(
    columns={"away_nation_id": "nation_id", "fifa_away_team_id": "fifa_team_id"}
)
team_ids = pd.concat([home, away], ignore_index=True).drop_duplicates("nation_id")
team_ids["fifa_team_id"] = team_ids["fifa_team_id"].astype("Int64")
print(f"resolved FIFA team_id for {len(team_ids)}/48 nations")
team_ids.head()
""")

md("""## 2. Pull each squad

`Players[]` and `Officials[]` per team. One cached HTTP call per nation.""")

code("""SQUAD_URL = "https://api.fifa.com/api/v3/teams/{tid}/squad?idCompetition=17&idSeason=285023"

players_rows = []
officials_rows = []

fetched = cached = 0
for r in team_ids.itertuples():
    tid = int(r.fifa_team_id)
    force = (FIRST_RUN or (r.nation_id in EVENT_NIDS))
    data = io.cache_raw(
        SQUAD_URL.format(tid=tid),
        source="fifa", name=f"squad_team_{tid}",
        sleep=0.2,
        force_refresh=force if force else False,
    )
    if force:
        fetched += 1
        events.stamp_fetch("fifa_team_squad", str(tid))
    else:
        cached += 1
    nation_id = r.nation_id
    for p in data.get("Players", []):
        name_en = (p.get("PlayerName") or [{}])[0].get("Description")
        short_en = (p.get("ShortName") or [{}])[0].get("Description")
        pos_en = (p.get("PositionLocalized") or [{}])[0].get("Description")
        real_pos_en = (p.get("RealPositionLocalized") or [{}])[0].get("Description")
        real_side_en = (p.get("RealPositionSideLocalized") or [{}])[0].get("Description")
        picture = p.get("PlayerPicture") or {}
        players_rows.append({
            "nation_id": nation_id,
            "fifa_team_id": tid,
            "fifa_player_id": int(p["IdPlayer"]) if p.get("IdPlayer") else None,
            "name": name_en,
            "short_name": short_en,
            "jersey_num": p.get("JerseyNum"),
            "birth_date": p.get("BirthDate"),
            "position": pos_en,
            "real_position": real_pos_en,
            "real_position_side": real_side_en,
            "position_code": p.get("Position"),
            "real_position_code": p.get("RealPosition"),
            "preferred_foot": p.get("PreferredFoot"),
            "height_cm": p.get("Height"),
            "weight_kg": p.get("Weight"),
            "fifa_id_country": p.get("IdCountry"),
            "picture_id": picture.get("Id"),
            "picture_url": picture.get("PictureUrl"),
            "active_status": p.get("ActiveStatus"),
            "fifa_match_appearances": p.get("MatchesPlayed"),
            "fifa_goals": p.get("Goals"),
            "fifa_yellow_cards": p.get("YellowCards"),
            "fifa_red_cards": p.get("RedCards"),
        })
    for o in data.get("Officials", []):
        name_en = (o.get("Name") or [{}])[0].get("Description")
        alias_en = (o.get("Alias") or [{}])[0].get("Description")
        type_en = (o.get("TypeLocalized") or [{}])[0].get("Description")
        officials_rows.append({
            "nation_id": nation_id,
            "fifa_team_id": tid,
            "fifa_coach_id": int(o["IdCoach"]) if o.get("IdCoach") else None,
            "name": name_en,
            "alias": alias_en,
            "role": o.get("Role"),
            "role_localized": type_en,
            "birth_date": o.get("BirthDate"),
            "fifa_id_country": o.get("IdCountry"),
        })

players = pd.DataFrame(players_rows)
officials = pd.DataFrame(officials_rows)
print(f"FIFA squads: fetched={fetched} cached={cached}; players={len(players)} across {players['nation_id'].nunique()} nations")
print(f"officials pulled: {len(officials)}")
""")

md("## 3. Sanity checks + save")

code("""# Per-nation squad size — most should be 26 (FIFA's WC roster cap).
sz = players.groupby("nation_id").size().rename("squad_size").reset_index()
print("squad size distribution:")
print(sz["squad_size"].value_counts().sort_index())
small_squads = sz[sz["squad_size"] < 20]
if len(small_squads):
    print("\\nnations with < 20 players (possibly incomplete from FIFA):")
    print(small_squads.to_string(index=False))

# Position breakdown
print("\\nposition breakdown:")
print(players["position"].value_counts())

# wc26_players is an internal artifact — nb_08 still reads it to build
# wc26_player_enrichment (the public player universe). nb_07 and nb_10 now
# load wc26_player_enrichment instead.
# wc26_team_officials dropped from the warehouse contract.
io.save_table(players, "wc26_players")

events.save()
print("event-state committed")
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("06_players.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 06_players.ipynb")
