"""Author 05_referee_assignments.ipynb."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 05 — `referee_assignments`

One row per `(match_id, role, referee_id)`. v1 captures the centre-referee role only.

Strategy:
1. Walk each referee's cached FootyMetrics profile and extract their upcoming
   WC26 fixtures (slug pattern `world-cup-{home}-{away}`).
2. Resolve the home/away slug to nation_ids via the alias table.
3. Join to `wc26_matches` by `(home_nation_id, away_nation_id)`.
4. Emit one row per (match, ref) with `role='referee'`, `source='footymetrics'`.

For matches with no centre-ref appointment surfaced yet, the row simply doesn't
exist — re-running the notebook (after re-running Notebook 04 with
`force_refresh=True` on profile fetches) refreshes the table.""")

code("""import sys, json, re
from pathlib import Path
from datetime import datetime
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import io
from lib.nation_match import match_to_canonical

matches = io.load_table("wc26_matches")
master = io.load_table("referee_master")
print(f"matches: {len(matches)}  referees: {len(master)}")
""")

md("## 1. Walk cached FM profiles for WC26 fixture slugs")

code("""cache_dir = Path("data/raw/footymetrics")
files = sorted(cache_dir.glob("*_ref_*.html"))
print(f"cached FM profiles: {len(files)}")

ref_by_fm_id = master.set_index("fm_id")["referee_id"].to_dict()

assignments = []
for f in files:
    try:
        fm_id = int(f.stem.split("ref_")[1])
    except Exception:
        continue
    referee_id = ref_by_fm_id.get(fm_id)
    if not referee_id:
        continue
    html = f.read_text(encoding="utf-8")
    chunks = re.findall(r'self\\.__next_f\\.push\\(\\[1,\\s*"(.*?)"\\]\\)', html, re.S)
    combined = "".join(json.loads('"' + c + '"') for c in chunks)
    for fix_id, slug in re.findall(r'/fixtures/(\\d+)-world-cup-([a-z][^"/\\\\<>]+?)(?=["/\\\\<>])', combined):
        assignments.append({
            "fm_id": fm_id,
            "referee_id": referee_id,
            "fm_fixture_id": int(fix_id),
            "fm_fixture_slug": slug,
        })

raw_df = pd.DataFrame(assignments).drop_duplicates(["fm_id", "fm_fixture_id"])
print(f"raw assignment rows: {len(raw_df)}")
print(f"distinct refs assigned: {raw_df['fm_id'].nunique()}")
print(f"distinct WC fixtures appointed: {raw_df['fm_fixture_id'].nunique()}")
""")

md("""## 2. Split slugs → (home, away) → nation_ids

FM slugs concatenate the home + away nation names with hyphens. Splitting them is ambiguous when one of the names itself contains a hyphen (`bosnia-herzegovina`, `cote-d-ivoire`, etc.). We try every plausible split point and accept the first that resolves both sides to canonical `nation_id`s.""")

code("""def resolve_pair(slug):
    parts = slug.split("-")
    for i in range(1, len(parts)):
        home = " ".join(parts[:i])
        away = " ".join(parts[i:])
        h = match_to_canonical(home)
        a = match_to_canonical(away)
        if h and a:
            return h, a
    return None, None

resolved = raw_df.assign(
    home_nation_id=raw_df["fm_fixture_slug"].map(lambda s: resolve_pair(s)[0]),
    away_nation_id=raw_df["fm_fixture_slug"].map(lambda s: resolve_pair(s)[1]),
)
n_ok = resolved["home_nation_id"].notna().sum()
print(f"slug → (home, away) resolved: {n_ok}/{len(resolved)}")
unresolved = resolved[resolved["home_nation_id"].isna()]
if len(unresolved):
    print("\\nslugs that failed to split — add to the alias map if any are real fixtures:")
    print(unresolved[["fm_fixture_slug"]].drop_duplicates().head(10).to_string(index=False))
""")

md("## 3. Join to `wc26_matches`")

code("""# Drop unresolved rows first — joining NaN to NaN against the 32 knockout
# placeholder rows (TBD teams in wc26_matches) would cross-join 4 × 32 = 128
# bogus rows.
resolved_ok = resolved.dropna(subset=["home_nation_id", "away_nation_id"])
m = matches[["match_number", "espn_match_id", "kickoff_utc", "stage",
              "home_nation_id", "away_nation_id"]].dropna(subset=["home_nation_id", "away_nation_id"])

joined = resolved_ok.merge(m, on=["home_nation_id", "away_nation_id"], how="left")
joined = joined.dropna(subset=["espn_match_id"]).copy()

joined["match_id"] = joined["espn_match_id"]
joined["role"] = "referee"
joined["source"] = "footymetrics"
joined["announced_at"] = pd.NaT

fm_out = joined[["match_id", "role", "referee_id", "source", "announced_at",
              "fm_fixture_id", "fm_fixture_slug", "home_nation_id", "away_nation_id",
              "match_number", "kickoff_utc", "stage"]]
print(f"FM-source assignment rows: {len(fm_out)}")
fm_out.head(5)
""")

md("""## 4. FIFA Officials as primary source

`wc26_matches` now carries `fifa_referee_id` + `fifa_referee_name` from the FIFA calendar API. That's the authoritative appointment. We join by name to `referee_master` to get our internal `referee_id`, then emit rows with `source='fifa'`. FM rows stay alongside as a verification layer.""")

code("""# FIFA-source rows from the matches table.
fifa_assigned = matches[matches["fifa_referee_id"].notna()].copy()
print(f"matches with FIFA referee_id: {len(fifa_assigned)}")

# Name match to referee_master. FIFA uses "Name" (often LAST CAPS style like
# "Wilton SAMPAIO"); FM uses full name ("Wilton Pereira Sampaio"). We normalise
# and try last-name match against referee_master.
def _norm_name(s):
    if not isinstance(s, str): return ""
    return re.sub(r"\\s+", " ", re.sub(r"[^\\w\\s]", " ", s.lower())).strip()

# Build a last-name → list[(name, referee_id, country_iso)] index for fuzzy
# resolution.
master_idx = {}
for _, r in master.iterrows():
    name_norm = _norm_name(r["name"])
    if not name_norm:
        continue
    parts = name_norm.split()
    last = parts[-1]
    iso_v = r.get("flag_iso")
    iso = iso_v.lower() if isinstance(iso_v, str) else ""
    master_idx.setdefault(last, []).append((name_norm, r["referee_id"], iso))

def match_fifa_to_master(fifa_name, fifa_country):
    n = _norm_name(fifa_name)
    if not n:
        return None
    parts = n.split()
    last = parts[-1]
    candidates = master_idx.get(last, [])
    # Prefer one whose country matches if provided.
    cc = (fifa_country or "").lower()
    for nm, rid, iso in candidates:
        if cc and iso and cc[:2] == iso[:2]:
            return rid
    # Otherwise return any single candidate.
    return candidates[0][1] if len(candidates) == 1 else (candidates[0][1] if candidates else None)

fifa_assigned["referee_id"] = fifa_assigned.apply(
    lambda r: match_fifa_to_master(r["fifa_referee_name"], r.get("fifa_referee_country")), axis=1
)
unmatched = fifa_assigned[fifa_assigned["referee_id"].isna()]
print(f"  matched to referee_master: {fifa_assigned['referee_id'].notna().sum()}/{len(fifa_assigned)}")
if len(unmatched):
    print("  unmatched (may be reserve/4th refs not in panel):")
    print(unmatched[["fifa_referee_name", "fifa_referee_country", "fifa_referee_id"]].to_string(index=False))

fifa_out = pd.DataFrame({
    "match_id": fifa_assigned["espn_match_id"],
    "role": "referee",
    "referee_id": fifa_assigned["referee_id"],
    "source": "fifa",
    "announced_at": pd.NaT,
    "fm_fixture_id": pd.NA,
    "fm_fixture_slug": None,
    "home_nation_id": fifa_assigned["home_nation_id"],
    "away_nation_id": fifa_assigned["away_nation_id"],
    "match_number": fifa_assigned["match_number"],
    "kickoff_utc": fifa_assigned["kickoff_utc"],
    "stage": fifa_assigned["stage"],
    "fifa_official_id": fifa_assigned["fifa_referee_id"],
    "fifa_official_name": fifa_assigned["fifa_referee_name"],
})
fifa_out = fifa_out.dropna(subset=["referee_id"])
print(f"\\nFIFA-source assignment rows: {len(fifa_out)}")
fifa_out.head(5)
""")

md("""## 5. Combine and save

One row per `match_id`. FIFA carries the authoritative appointment (referee_id +
fifa_official_id + name); FootyMetrics contributes `fm_fixture_id` + slug. Outer
join keeps matches that only one source has surfaced so far.""")

code("""# FIFA side carries the authoritative appointment; FM side adds the FM fixture
# identifiers. Outer join on (match_id, referee_id) keeps FM-only matches
# (FIFA hasn't published the appointment yet) and FIFA-only matches (FM hasn't
# linked the fixture yet).
fm_join = fm_out[["match_id", "referee_id", "fm_fixture_id", "fm_fixture_slug"]]
fifa_core = fifa_out[["match_id", "referee_id", "fifa_official_id", "fifa_official_name"]]

out = fifa_core.merge(fm_join, on=["match_id", "referee_id"], how="outer")

# Backfill match metadata (home/away/kickoff/stage/match_number) from
# wc26_matches by espn_match_id — this also covers FM-only rows whose FIFA-side
# columns are NaN.
meta_cols = ["home_nation_id", "away_nation_id", "match_number", "kickoff_utc", "stage"]
out = out.merge(
    matches[["espn_match_id"] + meta_cols].rename(columns={"espn_match_id": "match_id"}),
    on="match_id", how="left",
)

# Final column order.
out = out[[
    "match_id", "referee_id", "fifa_official_id", "fifa_official_name",
    "fm_fixture_id", "fm_fixture_slug",
    "home_nation_id", "away_nation_id", "match_number", "kickoff_utc", "stage",
]].sort_values(["match_number", "match_id"]).reset_index(drop=True)

assert len(out) == out["match_id"].nunique(), "duplicate match_id rows"
print(f"total assignment rows: {len(out)} (= distinct matches)")
print(f"with FIFA appointment:    {out['fifa_official_id'].notna().sum()}")
print(f"with FM fixture linkage:  {out['fm_fixture_id'].notna().sum()}")

io.save_table(out, "referee_assignments")
""")

md("""## 5. Live refresh helper

The function below re-fetches each WC26 referee's FootyMetrics profile (with
`force_refresh=True`), then re-runs the assignment extraction. Call it from
another cell once new appointments publish:

```python
from lib import refs
panel = refs.fm_discover_wc26(force_refresh=True)
for r in panel:
    refs.fm_fetch_profile(r["fm_id"], r["slug"], force_refresh=True)
# then re-run cells 1-4 above to rebuild the table.
```

For the future per-match additional officials (AR1, AR2, fourth, VAR, AVAR) we
would extend by scraping FIFA's match centre (24-48h before each kickoff) and
appending rows with the appropriate `role`.""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("05_referee_assignments.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 05_referee_assignments.ipynb")
