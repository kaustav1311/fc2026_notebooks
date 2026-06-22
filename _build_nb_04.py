"""Author 04_referees.ipynb."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 04 — `referee_master` + `referee_profile`

Builds the referee warehouse for WC26.

- **Discovery**: `footymetrics.com/world-cup-2026/referees` — the canonical 50-ref panel page.
- **Per-ref enrichment**: `footymetrics.com/referees/{fm_id}-{slug}` — one cached fetch per ref, stats parsed from the embedded React Server Components stream.
- **Country → nation_id**: alias-joined against `wc26_nations`.
- **Optional secondary source**: WorldReferee career page if a slug guess hits.
- **Re-runs**: every fetch is routed through `cache_raw` — a fresh run with `force_refresh=True` re-pulls live data; the default uses today's cache.

Outputs:
- `data/processed/referee_master.{parquet,csv}` — one row per ref.
- `data/processed/referee_profile.{parquet,csv}` — long format, one row per (ref, source, window).""")

code("""import sys, json
from datetime import datetime
from pathlib import Path
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import io, refs, events
from lib.nation_match import match_to_canonical

nations = io.load_table("wc26_nations")
print(f"nations loaded: {len(nations)}")

# Event-D scoping: only refresh ref profiles for refs who officiated newly-finished
# matches. The bridge resolves FIFA OfficialId → slug. Cold refs serve from cache.
try:
    matches = io.load_table("wc26_matches")
    ref_bridge = io.load_table("ref_id_bridge")
except FileNotFoundError:
    matches = ref_bridge = None
EVENT_REFS: set = set()
FIRST_RUN = events.is_first_run()
if matches is not None:
    new_mids = events.newly_finished_matches(matches)
    EVENT_REFS = events.refs_in_matches(new_mids, matches, ref_bridge)
    print(f"event-D: {len(new_mids)} new matches → {len(EVENT_REFS)} refs need profile refresh")
""")

md("## 1. Discover the WC26 referee panel")

code("""panel = refs.fm_discover_wc26()
panel_df = pd.DataFrame(panel)
print(f"panel size: {len(panel_df)} (expected ~46-50)")
print(f"  with country (from listing): {panel_df['country'].notna().sum()}/{len(panel_df)}")
panel_df.head()
""")

md("""## 2. Fetch per-ref profiles

50 HTTP calls, all cached. Subsequent runs hit disk.""")

code("""profiles = []
fetched = cached = 0
for r in panel_df.itertuples():
    # Event-D: only refetch profile pages for refs who reffed a newly-finished
    # match. The pre-built referee_id slug matches refs_in_matches output.
    candidate_ref_id = f"{(r.name or r.slug or 'ref').lower().replace(' ','-').replace('.','')}-{(r.flag_iso or 'xx').lower()}"
    force = (FIRST_RUN or (candidate_ref_id in EVENT_REFS))
    try:
        p = refs.fm_fetch_profile(r.fm_id, r.slug, force_refresh=force if force else False)
        profiles.append(p)
        if force:
            fetched += 1
            events.stamp_fetch("fm_ref_profile", candidate_ref_id)
        else:
            cached += 1
    except Exception as e:
        print(f"  failed: {r.slug} ({type(e).__name__}: {e})")
        profiles.append({"fm_id": r.fm_id, "slug": r.slug, "_error": str(e)})
prof_df = pd.DataFrame(profiles)
print(f"ref profiles: fetched={fetched} cached={cached} total={len(prof_df)}")
print(f"with fixtures field: {prof_df['fixtures'].notna().sum() if 'fixtures' in prof_df else 0}")
prof_df.head(3)
""")

md("""## 3. Build `referee_master`

Merge the panel list with the per-ref profile metadata. Country comes from the listing page where rendered, else from the profile page's first flag image. Joined to `nation_id` via the alias table built in Notebook 01.""")

code("""master = panel_df.merge(
    prof_df[["fm_id", "countryApid", "source_url"]].rename(columns={"source_url": "fm_url"}),
    on="fm_id", how="left",
)

# Curaçao's flag image is null on the FM panel page — patch by referee_id slug.
master.loc[master["slug"] == "danny-desmond-makkelie", "flag_iso"] = "cw"

# Map listing-page country to canonical nation_id. WC26 participants resolve
# via the alias table; non-participants fall back to a hand-curated ISO2 → FIFA
# 3-letter code map so every panel ref still gets a nation_id.
_ISO2_TO_FIFA3 = {
    "pl": "POL", "ro": "ROU", "it": "ITA", "cn": "CHN", "ae": "UAE",
    "si": "SVN", "ve": "VEN", "pe": "PER", "cl": "CHI", "ga": "GAB",
    "mr": "MTN", "sv": "SLV", "jm": "JAM", "cr": "CRC", "hn": "HON",
}
def _resolve_nation(country, flag_iso):
    if isinstance(country, str):
        c = match_to_canonical(country)
        if c:
            return c
    if isinstance(flag_iso, str):
        return _ISO2_TO_FIFA3.get(flag_iso.lower())
    return None
master["nation_id"] = master.apply(lambda r: _resolve_nation(r["country"], r["flag_iso"]), axis=1)

# Stable referee_id: "name-iso" lowercased.
def _ref_id(row):
    name = (row.get("name") or row.get("slug") or "ref")
    if not isinstance(name, str):
        name = str(name)
    iso = row.get("flag_iso")
    if not isinstance(iso, str) or not iso:
        iso = "xx"
    return f"{name.lower().replace(' ', '-').replace('.', '')}-{iso.lower()}"
master["referee_id"] = master.apply(_ref_id, axis=1)

master["wc26_nominated"] = True
master["fm_url"] = master["fm_url"].fillna("https://footymetrics.com/referees/" + master["fm_id"].astype(str) + "-" + master["slug"])

final_cols = ["referee_id", "name", "country", "confederation", "flag_iso",
              "nation_id", "fm_id", "slug", "fm_url", "countryApid", "wc26_nominated"]
master = master.reindex(columns=final_cols)
print(f"referee_master rows: {len(master)}")
print(f"  with country: {master['country'].notna().sum()}")
print(f"  with confederation: {master['confederation'].notna().sum()}")
print(f"  resolved to a WC26 nation_id: {master['nation_id'].notna().sum()}  (rest are refs from non-participating nations)")
master.head(10)
""")

md("""## 4. Build `referee_profile`

Long format: one row per `(referee_id, source, window)`.
- `career` from FootyMetrics' top-of-page aggregates.
- `last_10` and `last_25` (best-effort) parsed from the `recentMatches` block embedded in each cached FM profile page — no extra HTTP calls.""")

code("""rows = []
now = datetime.utcnow().isoformat(timespec="seconds")
ref_id_by_fm = master.set_index("fm_id")["referee_id"].to_dict()
for p in profiles:
    fm_id = p.get("fm_id")
    rid = ref_id_by_fm.get(fm_id)
    if not rid:
        continue
    n = p.get("fixtures")
    rows.append({
        "referee_id": rid,
        "source": "footymetrics",
        "window": "career",
        "matches": n,
        "yellow_pg": p.get("avgYellowCards"),
        "red_pg": p.get("avgRedCards"),
        "penalty_pg": p.get("avgPenalties"),
        "fouls_pg": p.get("avgFouls"),
        "total_yellows": p.get("totalYellows"),
        "total_reds": p.get("totalReds"),
        "total_penalties": p.get("totalPenalties"),
        "total_fouls": p.get("totalFouls"),
        "fixtures_with_red": p.get("fixturesWithRed"),
        "fixtures_with_penalty": p.get("fixturesWithPenalty"),
        "fixtures_no_cards": p.get("fixturesNoCards"),
        "computed_at": now,
    })

# Last-10 and last-25 windows from the recentMatches block in each cached FM profile.
for r in panel_df.itertuples():
    rid = ref_id_by_fm.get(r.fm_id)
    if not rid:
        continue
    force = (FIRST_RUN or (rid in EVENT_REFS))
    try:
        recent = refs.fm_recent_fixtures(r.fm_id, r.slug, force_refresh=force if force else False)
        if force:
            events.stamp_fetch("fm_recent_fixtures", rid)
    except Exception as e:
        print(f"  recent fetch failed for {r.slug}: {e}")
        continue
    for window_label, n in (("last_5", 5), ("last_10", 10), ("last_15", 15), ("last_25", 25)):
        slice_ = recent[:n]
        if not slice_:
            continue
        m = len(slice_)
        total_yc = sum(x["yellow_cards_total"] for x in slice_)
        total_rc = sum(x["red_cards_total"] for x in slice_)
        total_pen = sum(x["penalties_total"] for x in slice_)
        total_fl = sum(x["fouls_total"] for x in slice_)
        rows.append({
            "referee_id": rid,
            "source": "footymetrics",
            "window": window_label,
            "matches": m,
            "yellow_pg": round(total_yc / m, 3),
            "red_pg": round(total_rc / m, 3),
            "penalty_pg": round(total_pen / m, 3),
            "fouls_pg": round(total_fl / m, 3),
            "total_yellows": total_yc,
            "total_reds": total_rc,
            "total_penalties": total_pen,
            "total_fouls": total_fl,
            "fixtures_with_red": sum(1 for x in slice_ if x["red_cards_total"] > 0),
            "fixtures_with_penalty": sum(1 for x in slice_ if x["penalties_total"] > 0),
            "fixtures_no_cards": sum(1 for x in slice_ if x["yellow_cards_total"] == 0 and x["red_cards_total"] == 0),
            "computed_at": now,
        })

profile = pd.DataFrame(rows)
print(f"referee_profile rows: {len(profile)} ({profile['window'].value_counts().to_dict()})")
profile.head(8)
""")

md("""## 5. WorldReferee upcoming-panel changes check

Pull `worldreferee.com/upcoming` to spot any post-announcement changes (suspensions, withdrawals, replacements). We don't auto-apply; we surface the page snippet so the user can eyeball.""")

code("""try:
    upcoming = refs.wr_upcoming_changes()
    import re
    # Strip HTML to plain text for the report.
    text = re.sub(r"<[^>]+>", " ", upcoming)
    text = re.sub(r"\\s+", " ", text).strip()
    # Find any mention of WC26 / World Cup 26 / 2026
    snippets = []
    for m in re.finditer(r"(?:world cup|wc).{0,200}", text, re.I):
        snippets.append(m.group(0)[:200])
    print(f"upcoming page bytes: {len(upcoming)}")
    print(f"WC-related snippets found: {len(snippets)}")
    for s in snippets[:5]:
        print("  •", s[:160])
    if not snippets:
        print("  (no WC mentions in upcoming feed today)")
except Exception as e:
    print(f"worldreferee upcoming fetch failed: {type(e).__name__}: {e}")
""")

md("## 6. Save")

code("""io.save_table(master, "referee_master")
io.save_table(profile, "referee_profile")
""")

md("""## 7. Build `ref_id_bridge`

`wc26_matches.fifa_referee_id` is a numeric FIFA OfficialId string (e.g. `"361561"`);
`referee_master.referee_id` is a slug (`anthony-taylor-gb`). No direct join exists.

Bridge them on `(surname_normalised, country_iso3)` — matches the project's surname+country
identity pattern. Optional overrides at `data/overrides/ref_id_overrides.csv`
(`fifa_referee_id,referee_id,note`) are applied last so transliteration edge cases
get a one-line escape hatch.

Output: `data/processed/ref_id_bridge.parquet` (cols `fifa_referee_id, referee_id,
match_method`). Consumers: `15_staging_matches.ipynb`.""")

code("""import re, unicodedata
from lib.nation_match import match_to_canonical

# Skip the bridge cleanly on first-time setup when wc26_matches hasn't been built.
matches_pq = io.PROCESSED / "wc26_matches.parquet"
if not matches_pq.exists():
    print("wc26_matches.parquet not found — skip ref_id_bridge (run 03 first).")
else:
    _STOP_SURNAME = {"jr", "jnr", "junior", "sr", "snr", "ii", "iii",
                     "da", "de", "del", "do", "dos", "van", "von", "der", "den", "ten",
                     "el", "al", "le", "la", "y"}

    def _strip_accents(s):
        if not isinstance(s, str): return ""
        return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

    def _surname(name):
        if not isinstance(name, str): return None
        toks = [t for t in re.split(r"\\s+", _strip_accents(name).lower()) if t and t not in _STOP_SURNAME]
        return toks[-1] if toks else None

    def _country_to_iso3(country):
        if not isinstance(country, str): return None
        iso3 = match_to_canonical(country)
        return iso3 if isinstance(iso3, str) else None

    matches_for_bridge = io.load_table("wc26_matches")
    ref_obs = (
        matches_for_bridge[["fifa_referee_id", "fifa_referee_name", "fifa_referee_country"]]
            .dropna(subset=["fifa_referee_id"])
            .drop_duplicates()
            .reset_index(drop=True)
            .copy()
    )
    ref_obs["_surname"] = ref_obs["fifa_referee_name"].map(_surname)
    ref_obs["_iso3"]    = ref_obs["fifa_referee_country"].map(_country_to_iso3)
    print(f"FIFA referee observations: {len(ref_obs)} unique fifa_referee_ids")

    master_keyed = master[["referee_id", "name", "country", "nation_id"]].copy()
    master_keyed["_surname"] = master_keyed["name"].map(_surname)
    master_keyed["_iso3"]    = master_keyed.apply(
        lambda r: r["nation_id"] if isinstance(r["nation_id"], str) else _country_to_iso3(r["country"]),
        axis=1,
    )

    # Pass 1: surname + iso3.
    primary = master_keyed[["referee_id", "_surname", "_iso3"]].dropna(subset=["_surname", "_iso3"]).drop_duplicates(["_surname", "_iso3"])
    bridge = ref_obs.merge(primary, on=["_surname", "_iso3"], how="left")
    bridge["match_method"] = bridge["referee_id"].apply(lambda x: "surname+iso3" if isinstance(x, str) else None)

    # Pass 2: surname-only fallback for unresolved rows.
    fallback_map = master_keyed[["_surname", "referee_id"]].dropna(subset=["_surname"]).drop_duplicates(subset=["_surname"]).set_index("_surname")["referee_id"].to_dict()
    unresolved_mask = bridge["referee_id"].isna()
    bridge.loc[unresolved_mask, "referee_id"]   = bridge.loc[unresolved_mask, "_surname"].map(fallback_map)
    newly_resolved = unresolved_mask & bridge["referee_id"].notna()
    bridge.loc[newly_resolved, "match_method"] = "surname_only"

    # Pass 3: manual overrides at data/overrides/ref_id_overrides.csv.
    override_path = ROOT / "data" / "overrides" / "ref_id_overrides.csv"
    if override_path.exists():
        overrides = pd.read_csv(override_path, dtype={"fifa_referee_id": str})
        if len(overrides):
            ov_map = dict(zip(overrides["fifa_referee_id"], overrides["referee_id"]))
            mask = bridge["fifa_referee_id"].astype(str).isin(ov_map)
            bridge.loc[mask, "referee_id"]   = bridge.loc[mask, "fifa_referee_id"].astype(str).map(ov_map)
            bridge.loc[mask, "match_method"] = "override"
            print(f"  applied {int(mask.sum())} override(s) from {override_path.relative_to(ROOT)}")
    else:
        print(f"  no override file at {override_path.relative_to(ROOT)} (optional)")

    bridge_final = bridge[["fifa_referee_id", "referee_id", "match_method"]].copy()
    n_total      = len(bridge_final)
    n_resolved   = int(bridge_final["referee_id"].notna().sum())
    n_unresolved = n_total - n_resolved
    print(f"ref_id_bridge: {n_total} fifa officials, {n_resolved} mapped, {n_unresolved} unmapped")
    if n_unresolved:
        miss = bridge.loc[bridge["referee_id"].isna(), ["fifa_referee_id", "fifa_referee_name", "fifa_referee_country"]]
        print("\\nunmapped FIFA officials (add a row to ref_id_overrides.csv to resolve):")
        print(miss.to_string(index=False))

    io.save_table(bridge_final, "ref_id_bridge")

events.save()
print("event-state committed")
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("04_referees.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 04_referees.ipynb")
