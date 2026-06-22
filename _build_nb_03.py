"""Author 03_matches.ipynb."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 03 — `wc26_matches`

Builds the matches table — one row per WC26 fixture.

- Group-stage 72 matches seeded from the audit app's `fixtures.ts` (definitive date/time/venue/home/away set against the official FIFA bracket).
- ESPN scoreboard overlay adds the numeric `match_id`, live `status`, and any knockout fixtures already drawn.
- Stadium join via `espn_venue_name` (populated in Notebook 02).
- Nation join via ESPN team IDs from `wc26_nations`.
- Local kickoff derived from ET seed time + venue timezone using `zoneinfo`.

Outputs `data/processed/wc26_matches.{parquet,csv}`.""")

code("""import os, sys, json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import seed_loader, io

# Honor the FORCE_REFRESH env var set by refresh.py (auto-true during the
# tournament window). When True, every cache_raw call below re-fetches the
# live endpoint instead of returning yesterday's JSON.
FORCE_REFRESH = os.getenv("FORCE_REFRESH") == "1"
print(f"FORCE_REFRESH={FORCE_REFRESH}")

nations = io.load_table("wc26_nations")
stadiums = io.load_table("wc26_stadiums")
print(f"nations: {len(nations)}  stadiums: {len(stadiums)}")
""")

md("## 1. Group-stage seed (72 fixtures)")

code("""fixtures = seed_loader.parse_fixtures_ts()
seed = pd.DataFrame(fixtures).rename(columns={
    "id": "seed_match_id",
    "home": "home_nation_id",
    "away": "away_nation_id",
    "date": "date_et",
    "time": "kickoff_et",
    "venue": "venue_string",
})
seed["stage"] = seed["group"].map(lambda g: f"group_{g.lower()}")
print(f"seed rows: {len(seed)} (expected 72)")
assert len(seed) == 72
assert seed["home_nation_id"].isin(nations["nation_id"]).all()
assert seed["away_nation_id"].isin(nations["nation_id"]).all()
seed.head()
""")

md("## 2. ESPN scoreboard overlay")

code("""# Use cache_raw directly: the previous `latest_raw → if None → cache_raw`
# pattern silently returned ANY prior cache (no matter how stale) and never
# re-fetched. With FORCE_REFRESH=1 (set by refresh.py during the tournament
# window) this now re-pulls every tick; without it, today's cached file wins
# and yesterday's is superseded by today's write.
sb = io.cache_raw(
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates=20260611-20260720&limit=110",
    source="espn", name="scoreboard_wc26", force_refresh=FORCE_REFRESH,
)

# Reverse lookup espn_team_id -> nation_id
espn_to_nid = dict(zip(nations["espn_team_id"].astype("Int64"), nations["nation_id"]))

FINISHED = {"STATUS_FINAL", "STATUS_FULL_TIME", "STATUS_FINAL_AET", "STATUS_FINAL_PEN"}
LIVE = {"STATUS_IN_PROGRESS", "STATUS_FIRST_HALF", "STATUS_HALFTIME",
        "STATUS_SECOND_HALF", "STATUS_END_OF_REGULATION", "STATUS_OVERTIME",
        "STATUS_END_OF_EXTRATIME", "STATUS_SHOOTOUT"}

def map_status(name):
    if name in FINISHED:
        return "finished"
    if name in LIVE:
        return "live"
    return "scheduled"

espn_rows = []
for ev in sb.get("events", []):
    comp = (ev.get("competitions") or [{}])[0]
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        continue
    home_id = int((home.get("team") or {}).get("id", 0)) or None
    away_id = int((away.get("team") or {}).get("id", 0)) or None
    status_name = ((comp.get("status") or ev.get("status") or {}).get("type") or {}).get("name", "")
    notes_raw = comp.get("notes") or []
    notes = "; ".join(n.get("headline", "") for n in notes_raw if isinstance(n, dict))
    espn_rows.append({
        "espn_match_id": ev.get("id"),
        "kickoff_utc": pd.to_datetime(ev.get("date"), utc=True),
        "home_espn_id": home_id,
        "away_espn_id": away_id,
        "home_nation_id": espn_to_nid.get(home_id),
        "away_nation_id": espn_to_nid.get(away_id),
        "espn_venue_name": (comp.get("venue") or {}).get("fullName"),
        "espn_status": map_status(status_name),
        "espn_status_raw": status_name,
        "home_score": (home.get("score") or {}).get("value") if isinstance(home.get("score"), dict) else home.get("score"),
        "away_score": (away.get("score") or {}).get("value") if isinstance(away.get("score"), dict) else away.get("score"),
        "espn_notes": notes,
        "espn_season_slug": ((ev.get("season") or {}).get("slug")),
    })
espn_df = pd.DataFrame(espn_rows)
espn_df["date_et"] = espn_df["kickoff_utc"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
print(f"ESPN events: {len(espn_df)}")
espn_df.head()
""")

md("""## 3. Merge seed + ESPN

Strategy: outer join on `(date_et, home_nation_id, away_nation_id)`. Seed-only rows mean ESPN hasn't surfaced that fixture yet (rare); ESPN-only rows are knockouts not yet in the seed (expected before draws).""")

code("""seed_keyed = seed.assign(_src_seed=True)
espn_keyed = espn_df.assign(_src_espn=True)

# Merge on the (home, away) pair only — within group stage each pair plays once,
# and matching on date_et fails when ESPN shifts kickoff across the midnight-ET
# boundary by an hour. ESPN remains the source of truth for kickoff time.
merged = seed_keyed.merge(
    espn_keyed,
    on=["home_nation_id", "away_nation_id"],
    how="outer",
    suffixes=("_seed", "_espn"),
)

# date_et column collided — pick ESPN's where available, else seed's.
merged["date_et"] = merged["date_et_espn"].fillna(merged["date_et_seed"])
merged = merged.drop(columns=["date_et_seed", "date_et_espn"])

# Stage: prefer seed group label; otherwise derive from ESPN's season.slug.
SLUG_TO_STAGE = {
    "group-stage":      None,  # seed already supplies group_a..l for these
    "round-of-32":      "r32",
    "round-of-16":      "r16",
    "quarterfinals":    "qf",
    "semifinals":       "sf",
    "3rd-place-match":  "third_place",
    "final":            "final",
}
def stage_for(row):
    if isinstance(row.get("stage"), str) and row["stage"]:
        return row["stage"]
    slug = row.get("espn_season_slug")
    return SLUG_TO_STAGE.get(slug)
merged["stage"] = merged.apply(stage_for, axis=1)

print(f"merged rows: {len(merged)}")
print(f"  seed ∩ espn: {(merged['_src_seed'].fillna(False) & merged['_src_espn'].fillna(False)).sum()}")
print(f"  seed only:   {(merged['_src_seed'].fillna(False) & ~merged['_src_espn'].fillna(False)).sum()}")
print(f"  espn only:   {(~merged['_src_seed'].fillna(False) & merged['_src_espn'].fillna(False)).sum()}")
print(f"\\nstage breakdown:")
print(merged["stage"].fillna("(unknown)").value_counts().sort_index())
""")

md("""## 4. FIFA enrichment

`api.fifa.com/api/v3/calendar/matches` carries the canonical FIFA `IdMatch`, plus `Properties.IdIFES` (the key into `fdh-api.fifa.com` for per-player stats and power rankings). Also surfaces home/away `IdTeam` (FIFA team IDs needed for the squad endpoint), `Officials[]` (centre referee inline), `Stadium.IdStadium`, formations, attendance.""")

code("""fifa = io.cache_raw(
    "https://api.fifa.com/api/v3/calendar/matches?idCompetition=17&idSeason=285023&count=110",
    source="fifa", name="calendar_matches", force_refresh=FORCE_REFRESH,
)
fifa_rows = []
for m in fifa.get("Results", []):
    home = m.get("Home") or {}
    away = m.get("Away") or {}
    stadium = m.get("Stadium") or {}
    officials = m.get("Officials") or []
    centre = next((o for o in officials if o.get("OfficialType") == 1), None) or (officials[0] if officials else {})
    fifa_rows.append({
        "fifa_match_id": m.get("IdMatch"),
        "fifa_id_ifes": (m.get("Properties") or {}).get("IdIFES"),
        "home_nation_id": home.get("IdCountry"),
        "away_nation_id": away.get("IdCountry"),
        "fifa_home_team_id": home.get("IdTeam"),
        "fifa_away_team_id": away.get("IdTeam"),
        "fifa_home_tactics": home.get("Tactics"),
        "fifa_away_tactics": away.get("Tactics"),
        "fifa_stadium_id": stadium.get("IdStadium"),
        "fifa_attendance": m.get("Attendance"),
        "fifa_referee_id": centre.get("OfficialId"),
        "fifa_referee_country": centre.get("IdCountry"),
        "fifa_referee_name": (centre.get("Name") or [{}])[0].get("Description"),
    })
fifa_df = pd.DataFrame(fifa_rows)
print(f"FIFA matches: {len(fifa_df)}")
print(f"  with IdIFES (fdh-api key): {fifa_df['fifa_id_ifes'].notna().sum()}")
print(f"  with referee assigned:     {fifa_df['fifa_referee_id'].notna().sum()}")

# Drop FIFA rows where teams aren't determined yet — otherwise the merge
# cross-joins NaN×NaN against ESPN's knockout placeholders. They'll backfill
# when ESPN+FIFA resolve the bracket post group-stage.
fifa_resolved = fifa_df.dropna(subset=["home_nation_id", "away_nation_id"])
print(f"  resolved (both nations known): {len(fifa_resolved)}/{len(fifa_df)}")

merged = merged.merge(fifa_resolved, on=["home_nation_id", "away_nation_id"], how="left")
print(f"\\nmerged after FIFA join: {len(merged)} rows")
""")

md("## 5. Stadium + local kickoff + final shape")

code("""# Stadium join via espn_venue_name (populated in Notebook 02).
stadium_lookup = stadiums.set_index("espn_venue_name")["stadium_id"].to_dict()
merged["stadium_id"] = merged["espn_venue_name"].map(stadium_lookup)

# Where ESPN doesn't know the venue yet (seed-only rows), fall back to the seed's
# venue_string prefix-match against stadiums.match_key.
mk_lookup = stadiums.set_index("match_key")["stadium_id"].to_dict()
def fallback_stadium(row):
    if pd.notna(row["stadium_id"]):
        return row["stadium_id"]
    vs = row.get("venue_string") or ""
    for mk, sid in mk_lookup.items():
        if vs.lower().startswith(mk.lower()):
            return sid
    return None
merged["stadium_id"] = merged.apply(fallback_stadium, axis=1)

# Local kickoff = (date_et + kickoff_et) interpreted in America/New_York,
# converted to the venue's tz.
tz_lookup = stadiums.set_index("stadium_id")["timezone"].to_dict()

def local_kickoff(row):
    if pd.notna(row.get("kickoff_utc")):
        utc = row["kickoff_utc"]
    elif row.get("date_et") and row.get("kickoff_et"):
        et = datetime.fromisoformat(f"{row['date_et']}T{row['kickoff_et']}:00").replace(
            tzinfo=ZoneInfo("America/New_York"))
        utc = et.astimezone(timezone.utc)
    else:
        return pd.NaT, pd.NaT
    tz = tz_lookup.get(row.get("stadium_id")) or "UTC"
    return pd.Timestamp(utc), pd.Timestamp(utc.astimezone(ZoneInfo(tz)))

ku, kl = zip(*merged.apply(local_kickoff, axis=1))
merged["kickoff_utc"] = list(ku)
merged["kickoff_local"] = list(kl)

# Match number — chronological order
merged = merged.sort_values("kickoff_utc", na_position="last").reset_index(drop=True)
merged.insert(0, "match_number", merged.index + 1)

# Status default
merged["status"] = merged["espn_status"].fillna("scheduled")

final_cols = [
    "match_number", "espn_match_id", "fifa_match_id", "fifa_id_ifes", "seed_match_id",
    "kickoff_utc", "kickoff_local", "date_et", "kickoff_et",
    "stage", "espn_season_slug", "home_nation_id", "away_nation_id",
    "fifa_home_team_id", "fifa_away_team_id",
    "fifa_home_tactics", "fifa_away_tactics",
    "stadium_id", "fifa_stadium_id", "espn_venue_name", "venue_string",
    "status", "home_score", "away_score", "fifa_attendance",
    "fifa_referee_id", "fifa_referee_country", "fifa_referee_name",
    "espn_status_raw", "espn_notes",
]
out = merged.reindex(columns=final_cols)
print(out.head(10).to_string(index=False))
""")

md("## 6. Sanity checks + save")

code("""# Group-stage rows must have both nations resolved.
group_rows = out[out["stage"].str.startswith("group_", na=False)]
assert group_rows["home_nation_id"].notna().all(), "group-stage row missing home nation"
assert group_rows["away_nation_id"].notna().all(), "group-stage row missing away nation"
assert group_rows["home_nation_id"].isin(nations["nation_id"]).all()
assert group_rows["away_nation_id"].isin(nations["nation_id"]).all()

# Knockout rows where ESPN ships a placeholder team id (e.g. 5926 = "Winner Group A")
# carry stadium + kickoff but no nation yet — they'll resolve on the next ESPN pull
# once the bracket fills in.
knockout_placeholders = out[out["stage"].fillna("").ne("") & ~out["stage"].str.startswith("group_", na=False) & out["home_nation_id"].isna()]
print(f"knockout rows with TBD teams (placeholders): {len(knockout_placeholders)}")

n_with_stadium = out["stadium_id"].notna().sum()
print(f"rows with stadium_id: {n_with_stadium}/{len(out)}")
unresolved_venues = out.loc[out["stadium_id"].isna(), ["match_number", "date_et", "home_nation_id", "away_nation_id", "espn_venue_name", "venue_string"]]
if len(unresolved_venues):
    print("\\nunresolved venues:")
    print(unresolved_venues.to_string(index=False))

assert len(out) >= 72, f"expected ≥ 72 matches, got {len(out)}"
print(f"\\ntotal matches: {len(out)} (group + knockout)")

io.save_table(out, "wc26_matches")
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("03_matches.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 03_matches.ipynb")
