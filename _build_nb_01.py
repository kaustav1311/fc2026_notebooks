"""Author 01_nations.ipynb as a valid notebook JSON. Run once."""
import json
import uuid
from pathlib import Path

CELLS = []

def code(src: str) -> None:
    CELLS.append({
        "cell_type": "code",
        "execution_count": None,
        "id": uuid.uuid4().hex[:8],
        "metadata": {},
        "outputs": [],
        "source": [s + "\n" for s in src.rstrip("\n").split("\n")],
    })

def md(src: str) -> None:
    CELLS.append({
        "cell_type": "markdown",
        "id": uuid.uuid4().hex[:8],
        "metadata": {},
        "source": [s + "\n" for s in src.rstrip("\n").split("\n")],
    })

md("""# 01 — `wc26_nations`

Builds the canonical 48-row nations table:

- Seeded from the audit app's hand-curated `teams.ts` (FIFA 3-letter id, ISO, group, pot, confederation, FIFA rank, valuation, host flag).
- Enriched with native source IDs and source-specific name strings from ESPN, FotMob, Sofascore (where reachable), and Transfermarkt.
- Joined alias union for cross-source name lookups in later notebooks.

Outputs `data/processed/wc26_nations.{parquet,csv}`.""")

code("""import sys, json
from pathlib import Path
import pandas as pd

# Make the lib package importable when run from any cwd inside the project.
ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import seed_loader, io
from lib.http import polite_get
""")

md("## 1. Seed from the audit app")

code("""teams = seed_loader.parse_teams_ts()
espn_team_ids = seed_loader.parse_espn_team_ids()
abbr_iso = seed_loader.parse_espn_abbr_iso()

seed = pd.DataFrame(teams)
seed = seed.rename(columns={
    "name": "seed_name",
    "iso": "iso_alpha2",
    "fifaRank": "fifa_rank",
    "valuationM": "squad_valuation_m_eur",
    "isHost": "is_host",
})
seed["espn_team_id"] = seed["nation_id"].map(espn_team_ids).astype("Int64")
seed["is_host"] = seed.get("is_host", False).fillna(False).astype(bool)

print(f"seed rows: {len(seed)} (expected 48)")
assert len(seed) == 48, "teams.ts did not yield 48 rows"
assert seed["espn_team_id"].notna().all(), "missing ESPN id for at least one nation"
seed.head()
""")

md("""## 2. ESPN enrichment

One scoreboard pull (cached). We extract `(team.id, displayName, abbreviation)` for every competitor seen, then join the display name + abbreviation onto each seed row.""")

code("""scoreboard = io.cache_raw(
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates=20260611-20260720&limit=110",
    source="espn",
    name="scoreboard_wc26",
)

espn_rows = []
for ev in scoreboard.get("events", []):
    comp = (ev.get("competitions") or [{}])[0]
    for c in comp.get("competitors", []) or []:
        t = c.get("team") or {}
        if not t.get("id"):
            continue
        espn_rows.append({
            "espn_team_id": int(t["id"]),
            "espn_name": t.get("displayName") or t.get("name"),
            "espn_abbreviation": t.get("abbreviation"),
        })
espn_df = pd.DataFrame(espn_rows).drop_duplicates("espn_team_id")
print(f"distinct ESPN teams seen in scoreboard: {len(espn_df)}")
espn_df.head()
""")

md("""## 3. FotMob enrichment

`/api/data/leagues?id=77&season=2026` returns the WC-26 overview with 12 group tables. Walking `tables[].table.all[]` gives every team (id + name + shortName).""")

code("""fotmob = io.cache_raw(
    "https://www.fotmob.com/api/data/leagues?id=77&season=2026",
    source="fotmob",
    name="leagues_wc_2026",
)

fm_rows = []
for grp in (fotmob.get("table") or [{}])[0].get("data", {}).get("tables", []):
    table = grp.get("table") or {}
    for row in (table.get("all") or []):
        fm_rows.append({
            "fotmob_team_id": int(row["id"]),
            "fotmob_name": row.get("name"),
            "fotmob_short_name": row.get("shortName"),
        })
fotmob_df = pd.DataFrame(fm_rows).drop_duplicates("fotmob_team_id")
print(f"FotMob WC teams: {len(fotmob_df)} (expected 48)")
fotmob_df.head()
""")

md("""## 4. Transfermarkt enrichment

Pulls `national_teams.csv.gz` from the `dcaribou/transfermarkt-datasets` bundle on R2 — that file contains the actual national-team-as-club IDs (`national_team_id`) usable in TM's `/verein/{id}` URLs, which is what notebook 08 needs for squad scraping. We grab just that 5 KB entry from the 222 MB zip via HTTP Range using `remotezip`.

The bundle only covers ~118 of FIFA's 211 nations. Five WC26 nations (HAI, CIV, CUW, CPV, COD) aren't in it — we hand-patch their team IDs from TM quick-search.

(Sofascore enrichment was attempted but `api.sofascore.com` returns 403 to direct requests. The audit app routes through a Vercel proxy; we skip Sofascore here.)""")

code("""import gzip, io as _bytes_io
import remotezip

TM_ZIP_URL = "https://pub-e682421888d945d684bcae8890b0ec20.r2.dev/data/transfermarkt-datasets.zip"
NT_LOCAL = io.RAW / "transfermarkt" / "national_teams.csv"
if not NT_LOCAL.exists():
    with remotezip.RemoteZip(TM_ZIP_URL) as rz:
        raw = rz.read("national_teams.csv.gz")
    NT_LOCAL.write_bytes(gzip.decompress(raw))
    print(f"fetched national_teams.csv ({NT_LOCAL.stat().st_size:,} bytes)")
else:
    print(f"using cached {NT_LOCAL.name} ({NT_LOCAL.stat().st_size:,} bytes)")

tm_df = pd.read_csv(NT_LOCAL)[["national_team_id", "name", "team_code", "country_code"]].rename(
    columns={"national_team_id": "tm_team_id", "name": "tm_name",
             "team_code": "tm_slug", "country_code": "tm_code"}
)
tm_df["tm_team_id"] = tm_df["tm_team_id"].astype("Int64")

# Manual patches for the 5 nations not in dcaribou's national_teams.csv —
# values from TM quick-search (stable for years).
MANUAL_TM = {
    "HAI": {"tm_team_id": 14161, "tm_name": "Haiti",       "tm_slug": "haiti",                        "tm_code": "HAI"},
    "CIV": {"tm_team_id":  3591, "tm_name": "Ivory Coast", "tm_slug": "elfenbeinkuste",               "tm_code": "CIV"},
    "CUW": {"tm_team_id": 32364, "tm_name": "Curaçao",     "tm_slug": "curacao",                      "tm_code": "CUW"},
    "CPV": {"tm_team_id":  4311, "tm_name": "Cape Verde",  "tm_slug": "kap-verde",                    "tm_code": "CPV"},
    "COD": {"tm_team_id":  3854, "tm_name": "DR Congo",    "tm_slug": "demokratische-republik-kongo", "tm_code": "COD"},
}
print(f"Transfermarkt national teams on file: {len(tm_df)}  (+ {len(MANUAL_TM)} manual patches)")
tm_df.head()
""")

md("""## 6. Merge + alias union

Joins are by name, with a small manual alias dict for the known-painful cases from `sumary_1.txt §2`.""")

code("""# Lowercased + punctuation-stripped key for fuzzy-free name joins.
import re
def _norm(s):
    if not isinstance(s, str):
        return ""
    s = s.lower().strip()
    for a, b in (("ü","u"),("ö","o"),("ä","a"),("ç","c"),("ñ","n"),
                 ("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),
                 ("ş","s"),("ı","i"),("ğ","g"),("ô","o"),("è","e")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s

# Manual alias map (canonical_nation_id -> list of name variants seen across sources)
MANUAL_ALIASES = {
    "KOR": ["south korea", "korea republic", "republic of korea", "korea south"],
    "TUR": ["turkiye", "türkiye", "turkey"],
    "CIV": ["cote d ivoire", "côte d'ivoire", "ivory coast"],
    "COD": ["dr congo", "congo dr", "democratic republic of congo", "congo democratic republic"],
    "BIH": ["bosnia herzegovina", "bosnia and herzegovina", "bosnia & herzegovina"],
    "CZE": ["czechia", "czech republic"],
    "USA": ["united states", "usa", "united states of america"],
    "RSA": ["south africa"],
    "KSA": ["saudi arabia"],
    "CPV": ["cape verde", "cape verde islands", "cabo verde"],
    "UAE": ["united arab emirates"],
}

def norm_join(left, right, left_key, right_keys, value_cols, aliases=None):
    # left.merge(right) where right's match key is normalised against any of
    # right_keys OR against the manual alias list for left[left_key].
    aliases = aliases or {}
    right = right.copy()
    right["_norm"] = right[right_keys[0]].map(_norm)
    norm_map = {}
    for _, row in right.iterrows():
        for k in right_keys:
            v = row.get(k)
            if isinstance(v, str) and v:
                norm_map.setdefault(_norm(v), row)
    out_rows = []
    for _, lrow in left.iterrows():
        nid = lrow[left_key]
        candidates = [lrow["seed_name"]]
        candidates.extend(aliases.get(nid, []))
        match = None
        for cand in candidates:
            hit = norm_map.get(_norm(cand))
            if hit is not None:
                match = hit
                break
        out_rows.append({c: (match[c] if match is not None else None) for c in value_cols})
    return pd.concat([left.reset_index(drop=True), pd.DataFrame(out_rows)], axis=1)

df = seed
df = norm_join(df, espn_df, "nation_id", ["espn_name"], ["espn_name", "espn_abbreviation"], MANUAL_ALIASES)
df = norm_join(df, fotmob_df, "nation_id", ["fotmob_name", "fotmob_short_name"],
               ["fotmob_team_id", "fotmob_name", "fotmob_short_name"], MANUAL_ALIASES)
df = norm_join(df, tm_df, "nation_id", ["tm_name"], ["tm_team_id", "tm_name", "tm_code", "tm_slug"], MANUAL_ALIASES)

# Hand-patch the 5 nations TM doesn't cover in countries.json.
for nid, patch in MANUAL_TM.items():
    mask = df["nation_id"] == nid
    for col, val in patch.items():
        if df.loc[mask, col].isna().any():
            df.loc[mask, col] = val

# Build the union alias list for downstream nation_match.
name_cols = ["seed_name", "espn_name", "fotmob_name", "fotmob_short_name", "tm_name"]
def _aliases_for(row):
    seen = []
    for c in name_cols:
        v = row.get(c)
        if isinstance(v, str) and v and v not in seen:
            seen.append(v)
    for extra in MANUAL_ALIASES.get(row["nation_id"], []):
        if extra not in seen:
            seen.append(extra)
    return seen
df["all_names"] = df.apply(_aliases_for, axis=1)

# Final column order
final_cols = [
    "nation_id", "seed_name", "iso_alpha2", "confederation", "group", "pot",
    "fifa_rank", "squad_valuation_m_eur", "is_host",
    "espn_team_id", "espn_name", "espn_abbreviation",
    "fotmob_team_id", "fotmob_name", "fotmob_short_name",
    "tm_team_id", "tm_name", "tm_code", "tm_slug",
    "stars", "all_names",
]
df = df.reindex(columns=final_cols)
df.head()
""")

md("## 7. Sanity checks + save")

code("""assert len(df) == 48, f"expected 48 rows, got {len(df)}"
assert df["espn_team_id"].notna().all(), "missing espn_team_id"

coverage = {
    "espn_team_id": df["espn_team_id"].notna().sum(),
    "fotmob_team_id": df["fotmob_team_id"].notna().sum(),
    "tm_team_id": df["tm_team_id"].notna().sum(),
}
print("source id coverage (of 48):")
for k, v in coverage.items():
    print(f"  {k:18s} {v}/48")

missing_fotmob = df.loc[df["fotmob_team_id"].isna(), ["nation_id", "seed_name"]]
missing_tm = df.loc[df["tm_team_id"].isna(), ["nation_id", "seed_name"]]
if len(missing_fotmob):
    print(\"\\nNations missing fotmob id (check alias map):\")
    print(missing_fotmob.to_string(index=False))
if len(missing_tm):
    print(\"\\nNations missing transfermarkt id (check alias map):\")
    print(missing_tm.to_string(index=False))

io.save_table(df, "wc26_nations")
""")

nb = {
    "cells": CELLS,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.x"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}

Path("01_nations.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 01_nations.ipynb")
