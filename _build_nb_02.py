"""Author 02_stadiums.ipynb."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 02 — `wc26_stadiums`

Builds the 16-row stadium table:

- Seeded from the audit app's `venues.ts` (id, name, city, country, lat/lng, IANA tz, matchKey).
- Hand-curated enrichment: capacity, roof type, surface, altitude. FIFA's WC26 venue spec values.
- Open-Meteo elevation echo as a sanity check vs hand-curated altitude.
- `espn_venue_name` captured from the cached ESPN scoreboard so the matches notebook can prefix-match without re-deriving.

Outputs `data/processed/wc26_stadiums.{parquet,csv}`.""")

code("""import sys, json
from pathlib import Path
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import seed_loader, io
from lib.http import polite_get
""")

md("## 1. Seed from `venues.ts`")

code("""venues = seed_loader.parse_venues_ts()
seed = pd.DataFrame(venues).rename(columns={
    "id": "stadium_id",
    "lat": "latitude",
    "lng": "longitude",
    "tz": "timezone",
    "matchKey": "match_key",
})
print(f"seed rows: {len(seed)} (expected 16)")
assert len(seed) == 16
seed.head()
""")

md("""## 2. Hand-curated enrichment

Source: FIFA's official WC26 venue pages, cross-checked with each stadium's own site. WC26 mandates natural grass — stadiums with permanent turf install a grass overlay for tournament matches (`surface = 'grass_overlay'`).""")

code("""ENRICH = {
    # Mexico
    "azteca":    {"capacity": 87523, "roof_type": "open",        "surface": "grass",         "altitude_m": 2200, "state_or_region": "CDMX"},
    "akron":     {"capacity": 49850, "roof_type": "open",        "surface": "grass",         "altitude_m": 1600, "state_or_region": "Jalisco"},
    "bbva":      {"capacity": 53500, "roof_type": "open",        "surface": "grass",         "altitude_m":  558, "state_or_region": "Nuevo León"},
    # Canada
    "bmo":       {"capacity": 45500, "roof_type": "open",        "surface": "grass_overlay", "altitude_m":   76, "state_or_region": "Ontario"},
    "bcplace":   {"capacity": 54500, "roof_type": "retractable", "surface": "grass_overlay", "altitude_m":    3, "state_or_region": "British Columbia"},
    # USA
    "metlife":   {"capacity": 82500, "roof_type": "open",        "surface": "grass_overlay", "altitude_m":    3, "state_or_region": "New Jersey"},
    "sofi":      {"capacity": 70240, "roof_type": "fixed",       "surface": "grass",         "altitude_m":   32, "state_or_region": "California"},
    "att":       {"capacity": 80000, "roof_type": "retractable", "surface": "grass_overlay", "altitude_m":  158, "state_or_region": "Texas"},
    "mercedes":  {"capacity": 71000, "roof_type": "retractable", "surface": "grass_overlay", "altitude_m":  316, "state_or_region": "Georgia"},
    "hardrock":  {"capacity": 65326, "roof_type": "open",        "surface": "grass",         "altitude_m":    3, "state_or_region": "Florida"},
    "lincoln":   {"capacity": 69879, "roof_type": "open",        "surface": "grass",         "altitude_m":    9, "state_or_region": "Pennsylvania"},
    "arrowhead": {"capacity": 76416, "roof_type": "open",        "surface": "grass",         "altitude_m":  273, "state_or_region": "Missouri"},
    "nrg":       {"capacity": 72220, "roof_type": "retractable", "surface": "grass_overlay", "altitude_m":   13, "state_or_region": "Texas"},
    "levis":     {"capacity": 68500, "roof_type": "open",        "surface": "grass",         "altitude_m":    3, "state_or_region": "California"},
    "lumen":     {"capacity": 68740, "roof_type": "open",        "surface": "grass_overlay", "altitude_m":    5, "state_or_region": "Washington"},
    "gillette":  {"capacity": 65878, "roof_type": "open",        "surface": "grass_overlay", "altitude_m":   24, "state_or_region": "Massachusetts"},
}
assert set(ENRICH) == set(seed["stadium_id"]), f"missing/extra in ENRICH: {set(seed['stadium_id']) ^ set(ENRICH)}"

enrich_df = pd.DataFrame.from_dict(ENRICH, orient="index").reset_index().rename(columns={"index": "stadium_id"})
df = seed.merge(enrich_df, on="stadium_id", how="left")
df.head()
""")

md("""## 3. Open-Meteo elevation echo + grid key

One forecast call per venue (cached) just to capture Open-Meteo's `elevation` field — sanity-check our hand-curated `altitude_m`. Open-Meteo doesn't expose station IDs (it interpolates from gridded weather models); we store the lat/lng rounded to 1 decimal as a deterministic `weather_grid_key`.""")

code("""def fetch_meteo_elevation(lat, lng):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lng}&hourly=temperature_2m&forecast_days=1"
    name = f"forecast_{lat}_{lng}"
    data = io.cache_raw(url, source="open_meteo", name=name, sleep=0.2)
    return data.get("elevation")

df["open_meteo_elevation_m"] = [fetch_meteo_elevation(r.latitude, r.longitude) for r in df.itertuples()]
df["weather_grid_key"] = df.apply(lambda r: f"{round(r.latitude, 1)},{round(r.longitude, 1)}", axis=1)

# Diff vs hand-curated altitude for visibility
df["altitude_delta_m"] = (df["open_meteo_elevation_m"] - df["altitude_m"]).round(0)
print("hand-curated vs Open-Meteo elevation (large deltas = check value):")
print(df[["stadium_id", "name", "altitude_m", "open_meteo_elevation_m", "altitude_delta_m"]].to_string(index=False))
""")

md("## 4. ESPN venue-name capture")

code("""# Re-use the scoreboard JSON already cached by Notebook 01.
scoreboard = io.latest_raw("espn", "scoreboard_wc26")
if scoreboard is None:
    # First-time run with notebook 01 not yet executed — fetch fresh.
    scoreboard = io.cache_raw(
        "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates=20260611-20260720&limit=110",
        source="espn", name="scoreboard_wc26",
    )

espn_venues = set()
for ev in scoreboard.get("events", []):
    comp = (ev.get("competitions") or [{}])[0]
    v = (comp.get("venue") or {}).get("fullName")
    if v:
        espn_venues.add(v)
print(f"distinct ESPN venue strings: {len(espn_venues)}")

# Match each stadium to the ESPN venue string by prefix on its match_key.
# Plus a small alias dict for ESPN's sponsor-name variants.
ESPN_VENUE_ALIASES = {
    "azteca":    "Estadio Banorte",                  # Banorte naming rights as of 2025
    "arrowhead": "GEHA Field at Arrowhead Stadium",  # ESPN uses the full sponsored name
}

def find_espn_name(stadium_id, match_key):
    if stadium_id in ESPN_VENUE_ALIASES:
        return ESPN_VENUE_ALIASES[stadium_id]
    mk = match_key.lower()
    for v in espn_venues:
        if v.lower().startswith(mk):
            return v
    return None

df["espn_venue_name"] = [find_espn_name(r.stadium_id, r.match_key) for r in df.itertuples()]
unresolved = df.loc[df["espn_venue_name"].isna(), ["stadium_id", "match_key"]]
if len(unresolved):
    print("ESPN venue strings not yet seen (will resolve once those fixtures land):")
    print(unresolved.to_string(index=False))
else:
    print("all stadiums matched to an ESPN venue string")
""")

md("## 5. Sanity check + save")

code("""final_cols = [
    "stadium_id", "name", "city", "state_or_region", "country",
    "capacity", "roof_type", "surface", "altitude_m",
    "latitude", "longitude", "timezone",
    "weather_grid_key", "open_meteo_elevation_m", "altitude_delta_m",
    "match_key", "espn_venue_name",
]
df = df.reindex(columns=final_cols)

assert len(df) == 16
assert df["capacity"].notna().all()
assert df["timezone"].notna().all()
assert df["latitude"].notna().all() and df["longitude"].notna().all()

print(f"\\nroof breakdown:")
print(df["roof_type"].value_counts())
print(f"\\nsurface breakdown:")
print(df["surface"].value_counts())

io.save_table(df, "wc26_stadiums")
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("02_stadiums.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 02_stadiums.ipynb")
