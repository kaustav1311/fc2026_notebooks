"""One-off: re-run notebook 08's matching + save with the improved keyer,
skipping the slow per-player FotMob fetch cell (data already cached and the
columns we'd compute from it haven't changed since the last successful run)."""
import sys, io as sio, json, re, unicodedata
sys.stdout = sio.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import io, players as P

_STOP = {"jr","jnr","junior","sr","snr","ii","iii","da","de","del","do","dos","van","von","der","den","ten","el","al","le","la","y"}

def _sa(s): return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))

def _all_caps_surname(raw):
    if not isinstance(raw, str): return None
    src = _sa(raw)
    toks = [t for t in re.split(r"\s+", src) if t and t.lower() not in _STOP]
    if not toks or not any(not t.isupper() for t in toks):
        return None
    caps_toks = [t for t in toks if len(t) >= 2 and t.isupper()]
    if not caps_toks: return None
    last_caps = re.sub(r"[^A-Z\-]", "", caps_toks[-1])
    if "-" in last_caps:
        last_caps = last_caps.rsplit("-", 1)[-1]
    return last_caps.lower() or None

def _name_tokens(name):
    s = _sa(name or "").lower()
    s = re.sub(r"[^a-z\s]+", " ", s)
    return [t for t in s.split() if t and t not in _STOP and len(t) > 1]

def make_key(name, dob, source="other"):
    if not isinstance(dob, str) or len(dob) < 10: return None
    sn = _all_caps_surname(name) if source == "fifa" else None
    if not sn:
        t = _name_tokens(name)
        if not t: return None
        sn = t[-1]
    return f"{sn[:4]}|{dob[:10]}"

# ── load FIFA truth set
fifa = io.load_table("wc26_players")
fifa["dob_iso"] = fifa["birth_date"].astype(str).str.slice(0, 10)
fifa["_key"] = [make_key(r.name, r.dob_iso, "fifa") for r in fifa.itertuples()]
print(f"FIFA players keyed: {fifa['_key'].notna().sum()}/{len(fifa)}")

# ── FotMob per-team
nations = io.load_table("wc26_nations")
fm_rows = []
for r in nations.itertuples():
    if pd.isna(r.fotmob_team_id): continue
    for m in P.fotmob_team_squad(int(r.fotmob_team_id)):
        if m.get("fotmob_role_title") == "coach": continue
        m["nation_id"] = r.nation_id; fm_rows.append(m)
fm = pd.DataFrame(fm_rows)
fm["_key"] = [make_key(r.fotmob_name, r.date_of_birth, "fotmob") for r in fm.itertuples()]

# ── TM per-team
tm_rows = []
for r in nations.itertuples():
    if pd.isna(r.tm_team_id): continue
    slug = r.tm_slug if isinstance(r.tm_slug, str) else "team"
    try: ps = P.transfermarkt_squad(int(r.tm_team_id), slug=slug)
    except Exception: continue
    for p in ps:
        p["nation_id"] = r.nation_id; tm_rows.append(p)
tm = pd.DataFrame(tm_rows)
tm["tm_display_name"] = tm["tm_slug"].str.replace("-", " ").str.title()
tm["_key"] = [make_key(r.tm_display_name, r.tm_dob, "tm") for r in tm.itertuples()]

# ── join in
fifa_slim = fifa[["nation_id","fifa_player_id","name","short_name","birth_date","jersey_num",
                  "height_cm","weight_kg","position","real_position","real_position_side",
                  "preferred_foot","picture_url","_key"]]

fm_slim = fm[["nation_id","_key","fotmob_player_id","fotmob_name","club_fotmob_id","club_name",
              "transfer_value_eur_fotmob","position_ids_desc","wc_rating","wc_goals","wc_assists",
              "wc_yellow_cards","wc_red_cards"]].drop_duplicates(["nation_id","_key"])
tm_slim = tm[["nation_id","_key","tm_player_id","tm_slug","club_tm_id","club_name_tm",
              "market_value_eur_tm"]].drop_duplicates(["nation_id","_key"])

merged = fifa_slim.merge(fm_slim, on=["nation_id","_key"], how="left")
merged = merged.merge(tm_slim, on=["nation_id","_key"], how="left")

# ── apply manual overrides (from FotMob search) BEFORE merge
override_path = Path("data/seeds/player_id_overrides.csv")
if override_path.exists():
    over = pd.read_csv(override_path)
    over = over[["fifa_player_id", "fotmob_player_id", "tm_player_id"]].dropna(subset=["fifa_player_id"])
    over["fifa_player_id"] = over["fifa_player_id"].astype(int)
    # Build small lookup tables keyed on fotmob_player_id to fetch the same
    # squad-context columns the algorithmic match would have populated.
    fm_lookup = fm.dropna(subset=["fotmob_player_id"]).drop_duplicates("fotmob_player_id")
    fm_lookup = fm_lookup[["fotmob_player_id","fotmob_name","club_fotmob_id","club_name",
                            "transfer_value_eur_fotmob","position_ids_desc","wc_rating",
                            "wc_goals","wc_assists","wc_yellow_cards","wc_red_cards"]]
    print(f"applying {len(over)} overrides…")
    # Resolve via the override → fm_lookup chain
    over_resolved = over.merge(fm_lookup, on="fotmob_player_id", how="left")
    # Now stitch back into merged: rows where the algorithmic match was null
    # but an override exists.
    merged = merged.merge(over_resolved[["fifa_player_id","fotmob_player_id","fotmob_name",
                                          "club_fotmob_id","club_name","transfer_value_eur_fotmob",
                                          "position_ids_desc","wc_rating","wc_goals","wc_assists",
                                          "wc_yellow_cards","wc_red_cards"]].rename(
        columns={c: f"{c}__over" for c in over_resolved.columns if c != "fifa_player_id"}),
        on="fifa_player_id", how="left")
    # Fill nulls in the algorithmic columns from the override columns
    for col in ("fotmob_player_id","fotmob_name","club_fotmob_id","club_name",
                "transfer_value_eur_fotmob","position_ids_desc","wc_rating","wc_goals",
                "wc_assists","wc_yellow_cards","wc_red_cards"):
        ov_col = f"{col}__over"
        if ov_col in merged.columns:
            merged[col] = merged[col].fillna(merged[ov_col])
            merged = merged.drop(columns=[ov_col])

# ── pull existing rich rows from the previous enrichment table (cached per-player FotMob data)
try:
    prev = io.load_table("wc26_player_enrichment")
    keep_cols = ["fifa_player_id"] + [c for c in prev.columns if c not in merged.columns and c != "fifa_player_id"]
    rich_carry = prev[keep_cols]
    merged = merged.merge(rich_carry, on="fifa_player_id", how="left")
except Exception as e:
    print(f"could not carry forward rich cols: {e}")

merged = merged.drop(columns=["_key"], errors="ignore")
print(f"\nFotMob coverage: {merged['fotmob_player_id'].notna().sum()}/{len(merged)}")
print(f"TM     coverage: {merged['tm_player_id'].notna().sum()}/{len(merged)}")

# Per-nation residual gaps
gap = merged[merged["fotmob_player_id"].isna()]
if len(gap):
    print(f"\nFotMob residual gaps by nation:")
    print(gap["nation_id"].value_counts().head(8).to_string())

io.save_table(merged, "wc26_player_enrichment")
