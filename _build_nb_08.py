"""Author 08_player_enrichment.ipynb."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 08 — Player enrichment

Joins our FIFA-sourced `wc26_players` (1248 rows) with FotMob and Transfermarkt by name + DOB so every player carries a single canonical ID across all four sources we use downstream.

- **FotMob team squad** (48 calls): `fotmob_player_id`, current club, FotMob position string, this-tournament rating/goals/assists/cards, `transferValue` (EUR, scisports-modelled).
- **Transfermarkt squad** (48 calls): `tm_player_id`, current club (TM id), current market value (EUR).
- **FotMob playerData per player** (1248 calls, cached): `contract_end`, `preferred_foot`, full `market_value_history`, `career_senior` and `career_national` (season-by-season), recent matches.
- **ESPN market value**: ESPN does not publish transfer values — skipped. ESPN player IDs are also not needed for value/career data; we'll add ESPN per-team rosters in a later notebook only if a Phase 3 model wants ESPN's per-match form data.

Outputs:
- `wc26_player_enrichment` — one row per `fifa_player_id`, all cross-source IDs + latest market values + WC tournament line.
- `wc26_player_market_value_history` — long format from FotMob (player × date × value × team).
- `wc26_player_career_senior` and `wc26_player_career_national` — season-by-season from FotMob.""")

code("""import sys, json, re, unicodedata
from datetime import datetime, date
from pathlib import Path
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import io, events
from lib import players as P

# Config: per-player FotMob playerData fetch is ~1248 HTTP calls. Cached on
# re-runs. Set to False if you want a quick refresh that only re-pulls team-level
# data (IDs + current value).
FETCH_PER_PLAYER_FOTMOB = True

fifa_players = io.load_table("wc26_players")
nations = io.load_table("wc26_nations")
print(f"fifa players: {len(fifa_players)}  nations: {len(nations)}")

# Event-driven scoping. We compute three sets:
#   - EVENT_PIDS:  fifa_player_ids in newly-finished matches → re-pull FotMob
#                  playerData + TM value_history for them.
#   - EVENT_NIDS:  nation_ids who played a newly-finished match → re-pull
#                  FotMob team_squad + TM squad for them.
#   - FIRST_RUN:   no state file yet → treat everything as event-A.
# Outside the tournament, with FORCE_ALL_EVENTS=1 (set by refresh.py --force-refresh)
# both sets contain everyone.
try:
    matches = io.load_table("wc26_matches")
    match_wide = io.load_table("wc26_player_match_stats_wide")
except FileNotFoundError:
    matches = match_wide = None

EVENT_PIDS: set = set()
EVENT_NIDS: set = set()
FIRST_RUN = events.is_first_run()

if matches is not None and len(matches):
    new_mids = events.newly_finished_matches(matches)
    if match_wide is not None and len(match_wide):
        EVENT_PIDS = events.players_in_matches(new_mids, match_wide)
    EVENT_NIDS = events.teams_in_matches(new_mids, matches)
    print(f"event-A: {len(new_mids)} newly-finished matches → {len(EVENT_PIDS)} players, {len(EVENT_NIDS)} teams")
else:
    print("event scoping: no matches/match_wide yet — falling back to first-run mode")
    FIRST_RUN = True

""")

md("""## 1. Name + DOB normalisation

Cross-source identity = strip-accents(last name) ∧ DOB (yyyy-mm-dd). Tokens like 'Jr.' / 'da' / 'dos' are noise that we discard before keying.""")

code("""def _strip_accents(s):
    if not isinstance(s, str): return ""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

_STOP = {"jr", "jnr", "junior", "sr", "snr", "ii", "iii", "da", "de", "del", "do", "dos", "van", "von", "der", "den", "ten", "el", "al", "le", "la", "y"}

def _all_caps_surname(raw):
    \"\"\"FIFA writes the surname in ALL CAPS to disambiguate from the given name.
    Take the LAST all-caps token (handles surname-first like KIM Seunggyu,
    surname-last like Kylian MBAPPÉ, and middle-of-string like Cristiano
    Ronaldo dos SANTOS Aveiro). Split hyphenated surnames (ZAIRE-EMERY,
    WAN-BISSAKA) on '-' and keep the last segment so the key matches FotMob/TM
    which serialise the post-hyphen part as the displayed surname token.

    Returns None when the rule degenerates: name is all-caps (POR/BRA shout
    convention CRISTIANO RONALDO) or has no caps token — caller falls back
    to last-token of the cleaned name.\"\"\"
    if not isinstance(raw, str):
        return None
    src = _strip_accents(raw)
    raw_toks = [t for t in re.split(r"\\s+", src) if t and t.lower() not in _STOP]
    if not raw_toks:
        return None
    has_lower = any(not t.isupper() for t in raw_toks)
    if not has_lower:
        return None
    caps_toks = [t for t in raw_toks if len(t) >= 2 and t.isupper()]
    if not caps_toks:
        return None
    # Take the LAST caps token; trim punctuation; split hyphenated to last seg.
    last_caps = re.sub(r"[^A-Z\\-]", "", caps_toks[-1])
    if "-" in last_caps:
        last_caps = last_caps.rsplit("-", 1)[-1]
    return last_caps.lower() or None

def name_tokens(name):
    s = _strip_accents(name or "").lower()
    s = re.sub(r"[^a-z\\s]+", " ", s)
    return [t for t in s.split() if t and t not in _STOP and len(t) > 1]

def make_key(name, dob_iso, source="other"):
    \"\"\"Cross-source identity key = first-4-of-surname + DOB.

    source='fifa' uses the ALL-CAPS surname detection (FIFA puts surname in
    caps; position varies by culture). Other sources fall back to last token.\"\"\"
    if not isinstance(dob_iso, str) or len(dob_iso) < 10:
        return None
    surname = None
    if source == "fifa":
        surname = _all_caps_surname(name)
    if not surname:
        toks = name_tokens(name)
        if not toks:
            return None
        surname = toks[-1]
    return f"{surname[:4]}|{dob_iso[:10]}"

# Sanity checks across name conventions
for nm, dob, src, expected_first4 in [
    ("Kylian MBAPPÉ", "1998-12-20", "fifa", "mbap"),
    ("KIM Seunggyu", "1990-09-30", "fifa", "kim"),
    ("AL DAWSARI Salem", "1991-08-19", "fifa", "alda"),
    ("Seung-Gyu Kim", "1990-09-30", "fotmob", "kim"),
    ("seung-gyu-kim", "1990-09-30", "tm", "kim"),
    ("Kylian Mbappé", "1998-12-20", "fotmob", "mbap"),
]:
    k = make_key(nm, dob, src)
    ok = "✓" if k and k.startswith(expected_first4) else "✗"
    print(f"  {ok} {src:6s}  {nm!r:30s}  -> {k}")
""")

md("## 2. FotMob discovery — per nation (48 calls)")

code("""fotmob_rows = []
fmsq_fetched = fmsq_cached = 0
for r in nations.itertuples():
    fmid = r.fotmob_team_id
    if pd.isna(fmid):
        continue
    # Event-C: only re-fetch team squads for nations that played a newly-finished
    # match (catches mid-tournament injury subs). Other teams serve from cache.
    force = (FIRST_RUN or (r.nation_id in EVENT_NIDS))
    members = P.fotmob_team_squad(int(fmid), force_refresh=force if force else False)
    if force:
        fmsq_fetched += 1
        events.stamp_fetch("fotmob_team_squad", str(int(fmid)))
    else:
        fmsq_cached += 1
    for m in members:
        if m.get("fotmob_role_title") == "coach":
            continue
        m["nation_id"] = r.nation_id
        fotmob_rows.append(m)
fotmob_df = pd.DataFrame(fotmob_rows)
fotmob_df["_key"] = [make_key(r.fotmob_name, r.date_of_birth, "fotmob") for r in fotmob_df.itertuples()]
print(f"FotMob squads: fetched={fmsq_fetched} cached={fmsq_cached}; players={len(fotmob_df)} keyed={fotmob_df['_key'].notna().sum()}")
""")

md("## 3. Transfermarkt discovery — per nation (48 calls)")

code("""tm_rows = []
tmsq_fetched = tmsq_cached = 0
for r in nations.itertuples():
    tid = r.tm_team_id
    slug = r.tm_slug if isinstance(getattr(r, 'tm_slug', None), str) else "team"
    if pd.isna(tid):
        continue
    # Event-C: refetch TM squad pages only for nations that played a newly-finished
    # match. Same rationale as FotMob squads.
    force = (FIRST_RUN or (r.nation_id in EVENT_NIDS))
    try:
        players = P.transfermarkt_squad(int(tid), slug=slug, force_refresh=force if force else False)
    except Exception as e:
        print(f"  TM {r.nation_id} (id={tid}, slug={slug}) failed: {type(e).__name__}: {e}")
        continue
    if force:
        tmsq_fetched += 1
        events.stamp_fetch("tm_team_squad", str(int(tid)))
    else:
        tmsq_cached += 1
    for p in players:
        p["nation_id"] = r.nation_id
        tm_rows.append(p)
tm_df = pd.DataFrame(tm_rows)
if len(tm_df):
    tm_df["tm_display_name"] = tm_df["tm_slug"].str.replace("-", " ").str.title()
    tm_df["_key"] = [make_key(r.tm_display_name, r.tm_dob, "tm") for r in tm_df.itertuples()]
else:
    tm_df = pd.DataFrame(columns=["nation_id","tm_player_id","tm_slug","tm_dob",
                                  "club_tm_id","club_name_tm","market_value_eur_tm",
                                  "tm_display_name","_key"])
print(f"TM players: {len(tm_df)}  with key: {tm_df['_key'].notna().sum() if len(tm_df) else 0}")
""")

md("""## 4. Join FotMob and TM onto `wc26_players`

FIFA gives us the truth set (1248 rows). FotMob and TM are LEFT-joined by `(last_name_first4 + DOB)` within the same `nation_id`. Mismatches log clearly so we can extend the alias map.""")

code("""fifa_keyed = fifa_players[["nation_id", "fifa_player_id", "name", "short_name", "birth_date",
                            "jersey_num", "height_cm", "weight_kg", "position", "real_position",
                            "real_position_side", "preferred_foot", "picture_url"]].copy()
fifa_keyed["birth_date_iso"] = fifa_keyed["birth_date"].astype(str).str.slice(0, 10)
fifa_keyed["_key"] = [make_key(r.name, r.birth_date_iso, "fifa") for r in fifa_keyed.itertuples()]
print(f"FIFA players keyed: {fifa_keyed['_key'].notna().sum()}/{len(fifa_keyed)}")

# Join with (nation_id, _key)
def left_join(target, side, source, cols):
    side_slim = side[["nation_id", "_key"] + cols].drop_duplicates(["nation_id", "_key"])
    merged = target.merge(side_slim, on=["nation_id", "_key"], how="left", suffixes=("", f"_{source}"))
    return merged

merged = left_join(fifa_keyed, fotmob_df, "fotmob", [
    "fotmob_player_id", "fotmob_name", "club_fotmob_id", "club_name",
    "transfer_value_eur_fotmob", "position_ids_desc",
    "wc_rating", "wc_goals", "wc_assists", "wc_yellow_cards", "wc_red_cards",
])
merged = left_join(merged, tm_df, "tm", [
    "tm_player_id", "tm_slug", "club_tm_id", "club_name_tm", "market_value_eur_tm",
])

print(f"\\nmatch coverage (after name+DOB keyer):")
print(f"  fotmob_player_id resolved: {merged['fotmob_player_id'].notna().sum()}/{len(merged)}")
print(f"  tm_player_id resolved:     {merged['tm_player_id'].notna().sum()}/{len(merged)}")
""")

md("""### 4b. Apply hand / search-derived overrides

`data/seeds/player_id_overrides.csv` is the override layer for IDs the algorithmic keyer can't reach (typically Arabic names where FIFA's transliteration and FotMob/TM's diverge enough to break the surname+DOB key). Built by `_resolve_fotmob_missing.py` which hits FotMob's `apigw.fotmob.com/searchapi/suggest` endpoint with multiple name variants and confirms with a DOB check.

Overrides also re-fill the squad-context columns (club, transferValue, WC line) by chasing the override's `fotmob_player_id` through the same FotMob team payloads we already cached.""")

code("""override_path = Path("data/seeds/player_id_overrides.csv")
if override_path.exists():
    over = pd.read_csv(override_path).dropna(subset=["fifa_player_id"])
    over["fifa_player_id"] = over["fifa_player_id"].astype(int)

    fm_lookup = fotmob_df.dropna(subset=["fotmob_player_id"]).drop_duplicates("fotmob_player_id")[
        ["fotmob_player_id", "fotmob_name", "club_fotmob_id", "club_name",
         "transfer_value_eur_fotmob", "position_ids_desc",
         "wc_rating", "wc_goals", "wc_assists", "wc_yellow_cards", "wc_red_cards"]
    ]
    over_resolved = over.merge(fm_lookup, on="fotmob_player_id", how="left")

    # When override gives us a tm_player_id, also chase the TM-side fields
    # (slug, club, market value) from the cached squad rows in tm_df. Without
    # this the override only sets the id and leaves slug/club/value null.
    if len(tm_df):
        tm_lookup = tm_df.dropna(subset=["tm_player_id"]).drop_duplicates("tm_player_id")[
            ["tm_player_id", "tm_slug", "club_tm_id", "club_name_tm", "market_value_eur_tm"]
        ]
        over_resolved = over_resolved.merge(tm_lookup, on="tm_player_id", how="left")

    # Stitch override columns into merged
    suffix_map = {c: f"{c}__over" for c in over_resolved.columns if c != "fifa_player_id"}
    merged = merged.merge(over_resolved.rename(columns=suffix_map), on="fifa_player_id", how="left")
    for col in ("fotmob_player_id", "fotmob_name", "club_fotmob_id", "club_name",
                "transfer_value_eur_fotmob", "position_ids_desc",
                "wc_rating", "wc_goals", "wc_assists", "wc_yellow_cards", "wc_red_cards",
                "tm_player_id", "tm_slug", "club_tm_id", "club_name_tm", "market_value_eur_tm"):
        ov_col = f"{col}__over"
        if ov_col in merged.columns:
            merged[col] = merged[col].fillna(merged[ov_col])
            merged = merged.drop(columns=[ov_col])

    print(f"applied {len(over)} overrides")
else:
    print("no override file at data/seeds/player_id_overrides.csv — skipped")

print(f"\\nfinal coverage:")
print(f"  fotmob_player_id resolved: {merged['fotmob_player_id'].notna().sum()}/{len(merged)}")
print(f"  tm_player_id resolved:     {merged['tm_player_id'].notna().sum()}/{len(merged)}")
""")

md("""## 5. Per-player FotMob playerData (rich profile)

Hits `/api/data/playerData?id={fotmob_player_id}` for every player whose FotMob ID we matched. Pulls `contractEnd`, `preferredFoot`, full `marketValues.values` history, plus `careerHistory.senior` and `careerHistory.national team`. ~1248 calls, cached. Cell skips gracefully if `FETCH_PER_PLAYER_FOTMOB = False`.""")

code("""rich_rows = []
mv_history_rows = []
career_senior_rows = []
career_national_rows = []
errors = 0

fmpd_fetched = fmpd_cached = 0
if FETCH_PER_PLAYER_FOTMOB:
    for r in merged.itertuples():
        if pd.isna(r.fotmob_player_id):
            continue
        # Event-A: refetch FotMob playerData only for players in newly-finished
        # matches. Cold players (unmatched, didn't feature) serve from cache —
        # the daily backstop run with --force-refresh covers them weekly-ish.
        force = (FIRST_RUN or (r.fifa_player_id in EVENT_PIDS))
        try:
            d = P.fotmob_player_data(int(r.fotmob_player_id), force_refresh=force if force else False)
        except Exception as e:
            errors += 1
            continue
        if force:
            fmpd_fetched += 1
            events.stamp_fetch("fotmob_player_data", str(int(r.fotmob_player_id)))
        else:
            fmpd_cached += 1

        rich_rows.append({
            "fifa_player_id": r.fifa_player_id,
            "fotmob_player_id": int(r.fotmob_player_id),
            "contract_end": d.get("contract_end"),
            "preferred_foot_fotmob": d.get("preferred_foot"),
            "market_value_latest_eur_fotmob": d.get("market_value_latest_eur"),
            "market_value_lower_eur_fotmob": d.get("market_value_lower_eur"),
            "market_value_upper_eur_fotmob": d.get("market_value_upper_eur"),
            "market_value_team_id_fotmob": d.get("market_value_team_id"),
        })

        for v in (d.get("market_value_history") or []):
            mv_history_rows.append({
                "fifa_player_id": r.fifa_player_id,
                "fotmob_player_id": int(r.fotmob_player_id),
                "date": v.get("date"),
                "value_eur": v.get("value"),
                "lower_eur": v.get("lowerBound"),
                "upper_eur": v.get("upperBound"),
                "team_id": v.get("teamId"),
                "team_name": v.get("teamName"),
                "is_period_start": v.get("isPeriodStart"),
                "source": v.get("source"),
            })

        def _flatten_team_entry(e, kind):
            return {
                "fifa_player_id": r.fifa_player_id,
                "fotmob_player_id": int(r.fotmob_player_id),
                "kind": kind,
                "fotmob_team_id": e.get("teamId"),
                "team_name": e.get("team"),
                "transfer_type": e.get("transferType"),
                "start_date": e.get("startDate"),
                "end_date": e.get("endDate"),
                "active": e.get("active"),
                "appearances": e.get("appearances"),
                "goals": e.get("goals"),
                "assists": e.get("assists"),
                "has_uncertain_data": e.get("hasUncertainData"),
            }

        def _flatten_season_entry(e, kind):
            rating = e.get("rating") or {}
            return {
                "fifa_player_id": r.fifa_player_id,
                "fotmob_player_id": int(r.fotmob_player_id),
                "kind": kind,
                "fotmob_team_id": e.get("teamId"),
                "team_name": e.get("team"),
                "transfer_type": e.get("transferType"),
                "season_name": e.get("seasonName"),
                "appearances": e.get("appearances"),
                "goals": e.get("goals"),
                "assists": e.get("assists"),
                "rating": rating.get("rating") if isinstance(rating, dict) else rating,
            }

        cs = d.get("career_senior") or {}
        for e in (cs.get("teamEntries") or []):
            career_senior_rows.append(_flatten_team_entry(e, "team"))
        for e in (cs.get("seasonEntries") or []):
            career_senior_rows.append(_flatten_season_entry(e, "season"))

        cn = d.get("career_national") or {}
        for e in (cn.get("teamEntries") or []):
            career_national_rows.append(_flatten_team_entry(e, "team"))
        for e in (cn.get("seasonEntries") or []):
            career_national_rows.append(_flatten_season_entry(e, "season"))

    print(f"FotMob playerData: fetched={fmpd_fetched} cached={fmpd_cached} succeeded={len(rich_rows)} errors={errors}")
else:
    print("FETCH_PER_PLAYER_FOTMOB = False  — skipped")
""")

md("## 6. Merge + save")

code("""rich_df = pd.DataFrame(rich_rows)
if len(rich_df):
    merged = merged.merge(rich_df, on=["fifa_player_id", "fotmob_player_id"], how="left")

# Pick canonical preferred foot where FotMob fills the gap
merged["preferred_foot"] = merged["preferred_foot"].fillna(merged.get("preferred_foot_fotmob"))

# Drop intermediate join keys, debug-leftover override metadata, and the
# market-value columns — values now live in wc26_player_market_value_summary
# (built below) which carries per-source latest+peak plus the consolidated view.
DROP_FROM_ENRICHMENT = [
    "_key", "birth_date_iso",
    "matched_via__over", "matched_term__over",
    "market_value_eur_tm", "transfer_value_eur_fotmob",
    "market_value_latest_eur_fotmob", "market_value_lower_eur_fotmob",
    "market_value_upper_eur_fotmob", "market_value_team_id_fotmob",
    "market_value_eur_fotmob_latest",
]
out = merged.drop(columns=DROP_FROM_ENRICHMENT, errors="ignore")

io.save_table(out, "wc26_player_enrichment")

if career_senior_rows:
    io.save_table(pd.DataFrame(career_senior_rows), "wc26_player_career_senior")
if career_national_rows:
    io.save_table(pd.DataFrame(career_national_rows), "wc26_player_career_national")
""")

md("""## 6b. TM market value history (one fetch per player)

FotMob ships full history through `playerData.marketValues.values` (scisports
algorithmic). TM exposes its history at `ceapi/marketValueDevelopment/graph/{tm_id}`
(human-curated). We pull each player with a known `tm_player_id`, append to the
long-format `wc26_player_market_value_history` (source tag `tm`), and use it for
the per-player summary below.""")

code("""tm_history_rows = []
tm_summary: dict[int, dict] = {}  # fifa_player_id -> {current, peak, peak_date}

# merged has tm_player_id + fifa_player_id
tm_pairs = (out[["fifa_player_id", "tm_player_id"]]
            .dropna(subset=["tm_player_id"])
            .drop_duplicates("fifa_player_id"))
print(f"players with tm_player_id: {len(tm_pairs)}")

tm_errors = 0
tm_fetched = tm_cached = 0
for r in tm_pairs.itertuples():
    # Event-A: refetch TM history only for players in newly-finished matches.
    # TM market values are slow-moving so this is rarely false-negative.
    force = (FIRST_RUN or (r.fifa_player_id in EVENT_PIDS))
    try:
        h = P.transfermarkt_value_history(int(r.tm_player_id), force_refresh=force if force else False)
    except Exception:
        tm_errors += 1
        continue
    if force:
        tm_fetched += 1
        events.stamp_fetch("tm_value_history", str(int(r.tm_player_id)))
    else:
        tm_cached += 1
    for pt in h["history"]:
        if pt["date"] is None or pt["value_eur"] is None:
            continue
        tm_history_rows.append({
            "fifa_player_id": r.fifa_player_id,
            "tm_player_id": int(r.tm_player_id),
            "fotmob_player_id": None,
            "date": pt["date"],
            "value_eur": pt["value_eur"],
            "lower_eur": None,
            "upper_eur": None,
            "team_id": None,
            "team_name": pt["club_name"],
            "is_period_start": None,
            "source": "tm",
        })
    tm_summary[int(r.fifa_player_id)] = {
        "current_eur": h["current_eur"],
        "peak_eur": h["peak_eur"],
        "peak_date": h["peak_date"],
    }

print(f"TM history: fetched={tm_fetched} cached={tm_cached} rows={len(tm_history_rows)} errors={tm_errors}")

# Combine the FotMob (scisports) rows we already have with the new TM rows.
existing_rows = mv_history_rows if mv_history_rows else []
combined_history = []
# Re-emit FotMob rows with explicit `source='scisports'` tag.
for v in existing_rows:
    combined_history.append({**v, "tm_player_id": None})
combined_history.extend(tm_history_rows)
history_df = pd.DataFrame(combined_history) if combined_history else None
if history_df is not None and len(history_df):
    cols = ["fifa_player_id", "fotmob_player_id", "tm_player_id", "date",
            "value_eur", "lower_eur", "upper_eur", "team_id", "team_name",
            "is_period_start", "source"]
    history_df = history_df.reindex(columns=cols)
    io.save_table(history_df, "wc26_player_market_value_history")
    print(f"market_value_history saved: {len(history_df)} rows  by source:")
    print(history_df["source"].value_counts().to_string())
""")

md("""## 6c. Per-player market value summary

One row per `fifa_player_id`. Per-source latest + peak (FotMob/scisports + TM)
plus a consolidated view:

- `consolidated_latest_eur` = latest TM value when present, else latest FotMob value.
- `consolidated_latest_source` records which source contributed.
- `consolidated_peak_eur` = max(`fotmob_peak_eur`, `tm_peak_eur`).
- `consolidated_peak_source` + `consolidated_peak_date` from whichever source held the max.

ESPN does not publish player market values, so it doesn't contribute here.""")

code("""# FotMob per-player latest + peak from the scisports history.
fm_summary: dict[int, dict] = {}
if history_df is not None and len(history_df):
    fm = history_df[history_df["source"] == "scisports"].copy()
    fm = fm.dropna(subset=["value_eur", "date"]).sort_values("date")
    for fpid, g in fm.groupby("fifa_player_id", sort=False):
        latest = g.iloc[-1]
        peak_idx = g["value_eur"].idxmax()
        peak = g.loc[peak_idx]
        fm_summary[int(fpid)] = {
            "fotmob_latest_eur": int(latest["value_eur"]),
            "fotmob_latest_date": str(latest["date"])[:10],
            "fotmob_peak_eur": int(peak["value_eur"]),
            "fotmob_peak_date": str(peak["date"])[:10],
        }

# Combine with TM summary.
all_fpids = set(fm_summary) | set(tm_summary)
# Need TM latest from history too (the `current` string is a free-form display
# field — using max-date row from the cleaned history is safer).
tm_latest: dict[int, dict] = {}
if history_df is not None and len(history_df):
    tm_h = history_df[history_df["source"] == "tm"].copy()
    tm_h = tm_h.dropna(subset=["value_eur", "date"]).sort_values("date")
    for fpid, g in tm_h.groupby("fifa_player_id", sort=False):
        latest = g.iloc[-1]
        tm_latest[int(fpid)] = {
            "tm_latest_eur": int(latest["value_eur"]),
            "tm_latest_date": str(latest["date"])[:10],
        }
all_fpids |= set(tm_latest)

summary_rows = []
for fpid in sorted(all_fpids):
    fm = fm_summary.get(fpid, {})
    tm_l = tm_latest.get(fpid, {})
    tm_s = tm_summary.get(fpid, {})
    tm_peak_eur = tm_s.get("peak_eur")
    tm_peak_date = tm_s.get("peak_date")
    # consolidated_latest: TM preferred when available
    if "tm_latest_eur" in tm_l:
        cons_latest = tm_l["tm_latest_eur"]; cons_latest_source = "tm"; cons_latest_date = tm_l["tm_latest_date"]
    elif "fotmob_latest_eur" in fm:
        cons_latest = fm["fotmob_latest_eur"]; cons_latest_source = "scisports"; cons_latest_date = fm["fotmob_latest_date"]
    else:
        cons_latest = None; cons_latest_source = None; cons_latest_date = None
    # consolidated_peak: max across sources
    candidates = []
    if "fotmob_peak_eur" in fm:
        candidates.append((fm["fotmob_peak_eur"], "scisports", fm["fotmob_peak_date"]))
    if tm_peak_eur is not None:
        candidates.append((tm_peak_eur, "tm", tm_peak_date))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        cons_peak, cons_peak_source, cons_peak_date = candidates[0]
    else:
        cons_peak = cons_peak_source = cons_peak_date = None

    summary_rows.append({
        "fifa_player_id": fpid,
        "fotmob_latest_eur": fm.get("fotmob_latest_eur"),
        "fotmob_latest_date": fm.get("fotmob_latest_date"),
        "fotmob_peak_eur": fm.get("fotmob_peak_eur"),
        "fotmob_peak_date": fm.get("fotmob_peak_date"),
        "tm_latest_eur": tm_l.get("tm_latest_eur"),
        "tm_latest_date": tm_l.get("tm_latest_date"),
        "tm_peak_eur": tm_peak_eur,
        "tm_peak_date": tm_peak_date,
        "consolidated_latest_eur": cons_latest,
        "consolidated_latest_source": cons_latest_source,
        "consolidated_latest_date": cons_latest_date,
        "consolidated_peak_eur": cons_peak,
        "consolidated_peak_source": cons_peak_source,
        "consolidated_peak_date": cons_peak_date,
    })

summary_df = pd.DataFrame(summary_rows)
print(f"market_value_summary rows: {len(summary_df)}")
print(f"  with fotmob value: {summary_df['fotmob_latest_eur'].notna().sum()}")
print(f"  with tm value:     {summary_df['tm_latest_eur'].notna().sum()}")
print(f"  with consolidated_latest: {summary_df['consolidated_latest_eur'].notna().sum()}")
print(f"  with consolidated_peak:   {summary_df['consolidated_peak_eur'].notna().sum()}")

io.save_table(summary_df, "wc26_player_market_value_summary")
""")

md("""## 7. Per-player career summaries (youth / senior split)

Two derived tables, one row per `fifa_player_id`, joining cleanly to `wc26_players`:

- `wc26_player_career_national_summary` — split into **youth** (team_name matches `U\\d+`) vs **senior**. Aggregates: appearances, goals, assists, appearances-weighted average rating (rows with rating present), active_seasons array, num_seasons, num_teams.
- `wc26_player_career_club_summary` — same youth/senior split (U-pattern on club name catches club youth like "Mexico U19" rare cases; FotMob mostly doesn't surface reserve teams), plus an `all_clubs` array, `num_total_clubs`, and `current_club_name` / `current_club_fotmob_id` (taken from the `team` row with `active=True`).

Both feed off the per-season long tables built above, so they auto-refresh whenever notebook 08 re-runs.""")

code("""import re
import numpy as np

_U_PATTERN = re.compile(r"\\bU\\d+\\b")

def _is_youth_team(name):
    return isinstance(name, str) and bool(_U_PATTERN.search(name))

def _to_num(s):
    if s is None or (isinstance(s, float) and np.isnan(s)):
        return None
    try:
        return float(str(s).rstrip("*"))
    except (TypeError, ValueError):
        return None

def _agg_seasons(rows):
    \"\"\"rows is the per-player slice already filtered to one youth/senior bucket
    AND restricted to kind=='season'. Returns the aggregate dict.\"\"\"
    if len(rows) == 0:
        return {
            "appearances": 0, "goals": 0, "assists": 0,
            "weighted_avg_rating": None,
            "active_seasons": json.dumps([]),
            "num_seasons": 0, "num_teams": 0,
        }
    apps = [(_to_num(r["appearances"]) or 0) for _, r in rows.iterrows()]
    gls  = [(_to_num(r["goals"]) or 0) for _, r in rows.iterrows()]
    asts = [(_to_num(r["assists"]) or 0) for _, r in rows.iterrows()]
    # Weighted average rating — only seasons with rating present
    weight_sum, rate_sum = 0.0, 0.0
    for _, r in rows.iterrows():
        rt = _to_num(r["rating"])
        ap = _to_num(r["appearances"]) or 0
        if rt is not None and ap > 0:
            weight_sum += ap
            rate_sum += rt * ap
    wavg = round(rate_sum / weight_sum, 3) if weight_sum > 0 else None
    seasons = sorted({s for s in rows["season_name"].dropna().tolist() if s})
    teams = sorted({t for t in rows["team_name"].dropna().tolist() if t})
    return {
        "appearances": int(sum(apps)),
        "goals": int(sum(gls)),
        "assists": int(sum(asts)),
        "weighted_avg_rating": wavg,
        "active_seasons": json.dumps(seasons),
        "num_seasons": len(seasons),
        "num_teams": len(teams),
    }

def _build_summary(long_df, include_clubs_array=False):
    out_rows = []
    for fpid, g in long_df.groupby("fifa_player_id", sort=False):
        seasons = g[g["kind"] == "season"].copy()
        seasons["_youth"] = seasons["team_name"].map(_is_youth_team)
        youth_agg = _agg_seasons(seasons[seasons["_youth"]])
        senior_agg = _agg_seasons(seasons[~seasons["_youth"]])
        row = {"fifa_player_id": fpid}
        for k, v in youth_agg.items():
            row[f"youth_{k}"] = v
        for k, v in senior_agg.items():
            row[f"senior_{k}"] = v
        if include_clubs_array:
            # current_club: from team rows where active=True (latest start)
            teams_rows = g[g["kind"] == "team"].copy()
            active = teams_rows[teams_rows["active"] == True].sort_values("start_date", ascending=False)
            if len(active):
                row["current_club_name"] = active.iloc[0]["team_name"]
                row["current_club_fotmob_id"] = active.iloc[0]["fotmob_team_id"]
            else:
                row["current_club_name"] = None
                row["current_club_fotmob_id"] = None
            all_clubs = sorted({t for t in teams_rows["team_name"].dropna().tolist() if t})
            row["all_clubs"] = json.dumps(all_clubs)
            row["num_total_clubs"] = len(all_clubs)
        out_rows.append(row)
    return pd.DataFrame(out_rows)

if career_national_rows:
    cn_long = pd.DataFrame(career_national_rows)
    nat_summary = _build_summary(cn_long, include_clubs_array=False)
    # Rename num_teams → num_nations to match the national-team terminology
    nat_summary = nat_summary.rename(columns={
        "youth_num_teams": "youth_num_nations",
        "senior_num_teams": "senior_num_nations",
    })
    print(f"national summary rows: {len(nat_summary)}")
    print(nat_summary.head(2).to_string())
    io.save_table(nat_summary, "wc26_player_career_national_summary")

if career_senior_rows:
    cs_long = pd.DataFrame(career_senior_rows)
    club_summary = _build_summary(cs_long, include_clubs_array=True)
    print(f"club summary rows: {len(club_summary)}")
    print(club_summary.head(2).to_string())
    io.save_table(club_summary, "wc26_player_career_club_summary")

events.save()
print("event-state committed")
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("08_player_enrichment.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 08_player_enrichment.ipynb")
