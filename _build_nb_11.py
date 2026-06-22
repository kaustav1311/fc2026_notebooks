"""Author 11_fotmob_wc_and_form.ipynb."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 11 — FotMob WC tournament stats + recent form

Cross-check against the audit-doc (`METRICS_AUDIT.md` §2 — FotMob WC2026 player tournament stats and recent-matches form). Both blobs are already cached inside each per-player FotMob `playerData` JSON from notebook 08 — this notebook only **parses** the cache, no new HTTP calls.

- **WC tournament stats** live at `careerHistory.careerItems['national team'].seasonEntries[].tournamentStats[]` (filter `leagueId == 77`). FotMob's deep stats endpoint requires the WC to be the player's default tournament, which usually only holds for full-time international forwards mid-tournament; the `tournamentStats` summary always works. Fields: apps, goals, assists, rating.
- **Recent matches** (FotMob's last-N form view): opponent, minutes, goals, assists, cards, rating, POTM flag, bench flag.

Outputs:
- `wc26_player_fotmob_wc` — one row per `fifa_player_id` with their FotMob WC line (cap-style stats + rating).
- `wc26_player_recent_matches_fotmob` — long format, one row per recent match per player.""")

code("""import sys, json
from pathlib import Path
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import io

enrich = io.load_table("wc26_player_enrichment")
keyed = enrich.dropna(subset=["fotmob_player_id"]).copy()
keyed["fotmob_player_id"] = keyed["fotmob_player_id"].astype(int)
print(f"players with cached FotMob playerData: {len(keyed)}")
""")

md("## 1. Walk cached `playerData` files")

code("""WC_LEAGUE_ID = 77          # FotMob's league id for "World Cup" (every edition)
WC26_SEASON_NAME = "2026"   # restrict to the current tournament
WC26_TOURNAMENT_ID = 24254  # FotMob's unique id for WC26 — past editions differ
MAX_RECENT_MATCHES = 20    # cap per player (FotMob ships everything; we want recent form)

cache_dir = Path("data/raw/fotmob")

def _latest(fpid):
    fs = sorted(cache_dir.glob(f"*_player_{fpid}.json"))
    return fs[-1] if fs else None

wc_rows = []
form_rows = []
missing = 0

for r in keyed.itertuples():
    f = _latest(r.fotmob_player_id)
    if not f:
        missing += 1
        continue
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        missing += 1
        continue

    # WC tournament line — basic appearances/goals/assists/rating from FotMob's
    # careerHistory.national team.seasonEntries.tournamentStats[leagueId=77].
    # The 12 deep stats are attached later from per-match aggregation.
    nt = ((d.get("careerHistory") or {}).get("careerItems") or {}).get("national team") or {}

    # Fallback xG-against-on-pitch from firstSeasonStats — matchDetails only
    # exposes the GK variant (`expected_goals_on_target_faced`), so for
    # outfielders we read FotMob's "xG against while on pitch" tile when its
    # match count matches the player's WC appearances.
    fss = d.get("firstSeasonStats") or {}
    tsc_items = (fss.get("topStatCard") or {}).get("items") or []
    fss_matches = None
    for it in tsc_items:
        if isinstance(it, dict) and it.get("localizedTitleId") == "matches_uppercase":
            try:
                fss_matches = int(float(it.get("statValue")))
            except (TypeError, ValueError):
                fss_matches = None
            break
    fss_xga = None
    for sec in (((fss.get("statsSection") or {}).get("items")) or []):
        if not isinstance(sec, dict):
            continue
        for it in (sec.get("items") or []):
            if isinstance(it, dict) and it.get("title") == "xG against while on pitch":
                fss_xga = pd.to_numeric(it.get("statValue"), errors="coerce")
                break
        if fss_xga is not None:
            break
    fss_xga_apply = fss_xga if fss_xga is not None and not pd.isna(fss_xga) else None

    # Use tournamentStats[WC2026] when present (gives goals/assists/rating from
    # FotMob's own tallies). The row itself is always emitted — the per-match
    # aggregation later filters to only players who actually played.
    nt_apps = nt_goals = nt_assists = nt_rating = None
    for s in (nt.get("seasonEntries") or []):
        for t in (s.get("tournamentStats") or []):
            if t.get("leagueId") != WC_LEAGUE_ID: continue
            if t.get("seasonName") != WC26_SEASON_NAME: continue
            if t.get("tournamentId") and t.get("tournamentId") != WC26_TOURNAMENT_ID: continue
            rating = t.get("rating") or {}
            nt_apps    = pd.to_numeric(t.get("appearances"), errors="coerce")
            nt_goals   = pd.to_numeric(t.get("goals"), errors="coerce")
            nt_assists = pd.to_numeric(t.get("assists"), errors="coerce")
            nt_rating  = pd.to_numeric(rating.get("rating") if isinstance(rating, dict) else rating, errors="coerce")
            break
        if nt_apps is not None:
            break

    use_fss_xga = (fss_matches is not None and nt_apps is not None and pd.notna(nt_apps) and int(nt_apps) == fss_matches)
    wc_rows.append({
        "fifa_player_id": r.fifa_player_id,
        "fotmob_player_id": r.fotmob_player_id,
        "season_name": WC26_SEASON_NAME,
        "fotmob_tournament_id": WC26_TOURNAMENT_ID,
        "appearances": nt_apps,         # may be None — backfilled from matchDetails wc_matches_md below
        "goals": nt_goals,
        "assists": nt_assists,
        "fotmob_rating": nt_rating,
        "xg_against_on_pitch_fss": fss_xga_apply if use_fss_xga else None,
    })

    # Recent matches form — cap at MAX_RECENT_MATCHES newest per player. FotMob
    # ships everything the profile page shows (avg 46, max 70); we only want
    # the most recent slice and FotMob's array is already newest-first.
    for m in (d.get("recentMatches") or [])[:MAX_RECENT_MATCHES]:
        md_ = m.get("matchDate") or {}
        rp = m.get("ratingProps") or {}
        form_rows.append({
            "fifa_player_id": r.fifa_player_id,
            "fotmob_player_id": r.fotmob_player_id,
            "match_date_utc": md_.get("utcTime") if isinstance(md_, dict) else md_,
            "match_id_fotmob": m.get("id"),
            "team_id": m.get("teamId"),
            "team_name": m.get("teamName"),
            "opponent_team_id": m.get("opponentTeamId"),
            "opponent_team_name": m.get("opponentTeamName"),
            "is_home": m.get("isHomeTeam"),
            "league_id": m.get("leagueId"),
            "league_name": m.get("leagueName"),
            "home_score": m.get("homeScore"),
            "away_score": m.get("awayScore"),
            "minutes_played": m.get("minutesPlayed"),
            "goals": m.get("goals"),
            "assists": m.get("assists"),
            "yellow_cards": m.get("yellowCards"),
            "red_cards": m.get("redCards"),
            "fotmob_rating": pd.to_numeric(rp.get("rating"), errors="coerce") if isinstance(rp, dict) else None,
            "is_top_rating": rp.get("isTopRating") if isinstance(rp, dict) else None,
            "player_of_the_match": m.get("playerOfTheMatch"),
            "on_bench": m.get("onBench"),
        })

print(f"WC stat rows: {len(wc_rows)}  recent-match rows: {len(form_rows)}  missing-cache: {missing}")
""")

md("""## 2. Per-match WC stats aggregation (FotMob `/matchDetails`)

`firstSeasonStats` only carries WC stats when WC happens to be the player's
*default* tournament — typically only top forwards mid-tournament. To get
broad coverage of the 12 deep stats (chances_created, big_chances_created,
dribbles/successful_dribbles_pct, duels_won/pct, touches, touches_opp_box,
defensive_contributions, tackles, fouls_committed, xg_against_on_pitch) we
walk each WC match's `/matchDetails` endpoint and sum the per-player blocks.

This catches mid-tier players too — Saudi defenders, Uzbek midfielders, etc.
— whose default tournament is their domestic league.""")

code("""WC_MATCH_DETAILS_URL = "https://www.fotmob.com/api/data/matchDetails?matchId={mid}"

# Materialise form_df early so we can discover WC match ids from it.
form_df = pd.DataFrame(form_rows)
wc_matches = (form_df[form_df["league_id"] == WC_LEAGUE_ID]
              if len(form_df) else pd.DataFrame(columns=["match_id_fotmob"]))
wc_match_ids = sorted({int(m) for m in wc_matches["match_id_fotmob"].dropna().tolist()})
print(f"distinct WC matches discovered: {len(wc_match_ids)}")

def _index_player_stats(block):
    \"\"\"Flatten per-player stats[] groups → {key: {value, total}}.\"\"\"
    out = {}
    for grp in (block.get("stats") or []):
        for entry in (grp.get("stats") or {}).values():
            if not isinstance(entry, dict):
                continue
            k = entry.get("key")
            s = entry.get("stat") or {}
            if not k:
                continue
            v = s.get("value") if isinstance(s.get("value"), (int, float)) else 0
            t = s.get("total") if isinstance(s.get("total"), (int, float)) else 0
            out[k] = {"value": v, "total": t}
    return out

# Walk + cache each WC match's playerStats.
# Immutable-after-finish: once a match's matchDetails has landed, mark it
# processed in event-state and skip the HTTP on subsequent ticks.
from lib import events as _events
match_player_stats: dict[int, dict] = {}
fetch_errors = 0
for mid in wc_match_ids:
    already = _events.is_processed("fotmob_match_details", str(mid))
    fetch_kwargs = {"force_refresh": False} if already else {}
    try:
        data = io.cache_raw(
            WC_MATCH_DETAILS_URL.format(mid=mid),
            source="fotmob", name=f"match_details_{mid}", sleep=0.2, **fetch_kwargs,
        )
    except Exception:
        fetch_errors += 1
        continue
    ps = (data.get("content") or {}).get("playerStats") or {}
    match_player_stats[mid] = ps
    if not already:
        _events.mark_processed("fotmob_match_details", str(mid))
_events.save()
print(f"matches cached: {len(match_player_stats)}  errors: {fetch_errors}")

# Per (fotmob_player_id) accumulator over their WC match blocks.
fotmob_to_fifa = (form_df[["fotmob_player_id", "fifa_player_id"]]
                  .drop_duplicates().set_index("fotmob_player_id")["fifa_player_id"].to_dict())

agg: dict[int, dict] = {}
for mid, ps in match_player_stats.items():
    for pid_str, block in ps.items():
        try:
            pid = int(pid_str)
        except (TypeError, ValueError):
            continue
        idx = _index_player_stats(block)
        mins = idx.get("minutes_played", {}).get("value", 0) or 0
        rating = idx.get("rating_title", {}).get("value", 0) or 0
        if mins <= 0 and rating <= 0:
            continue  # bench / didn't feature
        a = agg.setdefault(pid, {
            "matches": 0, "minutes": 0,
            "goals_md": 0, "assists_md": 0,
            "chances_created": 0, "big_chances_created": 0,
            "dribbles_succeeded_value": 0, "dribbles_succeeded_total": 0,
            "duel_won": 0, "duel_lost": 0,
            "touches": 0, "touches_opp_box": 0,
            "defensive_actions": 0, "tackles": 0,
            "fouls": 0, "xgot_faced": 0,
            "rating_weighted": 0.0, "rating_weight": 0.0,
        })
        a["matches"] += 1
        a["minutes"] += mins
        a["goals_md"] += idx.get("goals", {}).get("value", 0) or 0
        a["assists_md"] += idx.get("assists", {}).get("value", 0) or 0
        a["chances_created"] += idx.get("chances_created", {}).get("value", 0) or 0
        # FotMob ships the "big chance created (by the team while this player is involved)"
        # field as `big_chance_created_team_title` — the simpler `big_chances_created`
        # key does not exist in matchDetails.
        a["big_chances_created"] += idx.get("big_chance_created_team_title", {}).get("value", 0) or 0
        ds = idx.get("dribbles_succeeded", {})
        a["dribbles_succeeded_value"] += ds.get("value", 0) or 0
        a["dribbles_succeeded_total"] += ds.get("total", 0) or 0
        a["duel_won"] += idx.get("duel_won", {}).get("value", 0) or 0
        a["duel_lost"] += idx.get("duel_lost", {}).get("value", 0) or 0
        a["touches"] += idx.get("touches", {}).get("value", 0) or 0
        a["touches_opp_box"] += idx.get("touches_opp_box", {}).get("value", 0) or 0
        a["defensive_actions"] += idx.get("defensive_actions", {}).get("value", 0) or 0
        a["tackles"] += idx.get("matchstats.headers.tackles", {}).get("value", 0) or 0
        a["fouls"] += idx.get("fouls", {}).get("value", 0) or 0
        a["xgot_faced"] += idx.get("expected_goals_on_target_faced", {}).get("value", 0) or 0
        # cards aren't in matchDetails — sourced separately from recentMatches below
        if rating > 0 and mins > 0:
            a["rating_weighted"] += rating * mins
            a["rating_weight"] += mins

# Project the accumulator into the columns we publish.
deep_rows = []
for pid, a in agg.items():
    drib_pct = (100.0 * a["dribbles_succeeded_value"] / a["dribbles_succeeded_total"]
                if a["dribbles_succeeded_total"] > 0 else None)
    duels_pct = (100.0 * a["duel_won"] / (a["duel_won"] + a["duel_lost"])
                 if (a["duel_won"] + a["duel_lost"]) > 0 else None)
    deep_rows.append({
        "fotmob_player_id": pid,
        "_md_matches": a["matches"],
        "_md_minutes": a["minutes"],
        "_md_goals": a["goals_md"],
        "_md_assists": a["assists_md"],
        "_md_rating": round(a["rating_weighted"] / a["rating_weight"], 2) if a["rating_weight"] > 0 else None,
        "minutes_played": a["minutes"],
        "chances_created": a["chances_created"],
        "big_chances_created": a["big_chances_created"],
        "dribbles": a["dribbles_succeeded_total"],
        "successful_dribbles_pct": round(drib_pct, 1) if drib_pct is not None else None,
        "duels_won": a["duel_won"],
        "duels_won_pct": round(duels_pct, 1) if duels_pct is not None else None,
        "touches": a["touches"],
        "touches_opp_box": a["touches_opp_box"],
        "defensive_contributions": a["defensive_actions"],
        "tackles": a["tackles"],
        "fouls_committed": a["fouls"],
        "xg_against_on_pitch": round(a["xgot_faced"], 2) if a["xgot_faced"] else None,
    })
deep_df = pd.DataFrame(deep_rows)
print(f"per-player WC stat rows aggregated: {len(deep_df)}")
""")

md("## 3. Save")

code("""wc_df = pd.DataFrame(wc_rows)

# Merge the per-match-aggregated deep stats onto the basic WC line.
if len(wc_df) and len(deep_df):
    wc_df = wc_df.merge(deep_df, on="fotmob_player_id", how="left")

# Filter to players who actually appeared in WC26 (matchDetails confirms).
# FotMob's careerHistory.tournamentStats lags 1-2 days during the tournament;
# without this we'd either miss recent appearances or over-include benched
# squad members.
before = len(wc_df)
wc_df = wc_df[wc_df["_md_matches"].fillna(0) > 0].reset_index(drop=True)
print(f"filtered to players with WC26 appearances: {before} → {len(wc_df)}")

# Backfill appearances/goals/assists/rating from matchDetails when FotMob's
# tournamentStats hasn't rolled them up yet.
wc_df["appearances"]    = wc_df["appearances"].fillna(wc_df["_md_matches"])
wc_df["goals"]          = wc_df["goals"].fillna(wc_df["_md_goals"])
wc_df["assists"]        = wc_df["assists"].fillna(wc_df["_md_assists"])
wc_df["fotmob_rating"]  = wc_df["fotmob_rating"].fillna(wc_df["_md_rating"])

# Cards: matchDetails per-player block does NOT carry yellow/red cards.
# Source them from the recentMatches WC slice (form_df filtered to leagueId=77),
# which we already parsed above.
if len(form_df):
    cards = (form_df[form_df["league_id"] == WC_LEAGUE_ID]
             .groupby("fotmob_player_id", as_index=False)
             .agg(yellow_cards=("yellow_cards", "sum"),
                  red_cards=("red_cards", "sum")))
    wc_df = wc_df.merge(cards, on="fotmob_player_id", how="left")
else:
    wc_df["yellow_cards"] = 0
    wc_df["red_cards"] = 0
wc_df["yellow_cards"] = wc_df["yellow_cards"].fillna(0).astype(int)
wc_df["red_cards"]    = wc_df["red_cards"].fillna(0).astype(int)

# Drop the now-redundant md helper columns + wc_matches_md (per user — keep
# only `appearances`).
wc_df = wc_df.drop(columns=["_md_matches", "_md_minutes", "_md_goals",
                            "_md_assists", "_md_rating"], errors="ignore")

# xg_against_on_pitch: matchDetails only ships this for GKs (xGOT faced).
# For outfielders, fall back to firstSeasonStats' "xG against while on pitch"
# tile that we cached earlier as xg_against_on_pitch_fss.
if "xg_against_on_pitch_fss" in wc_df.columns:
    if "xg_against_on_pitch" not in wc_df.columns:
        wc_df["xg_against_on_pitch"] = None
    wc_df["xg_against_on_pitch"] = wc_df["xg_against_on_pitch"].fillna(wc_df["xg_against_on_pitch_fss"])
    wc_df = wc_df.drop(columns=["xg_against_on_pitch_fss"])

# Fill rate report for the 12 deep fields.
deep_cols = ["chances_created","big_chances_created","dribbles","successful_dribbles_pct",
             "duels_won","duels_won_pct","touches","touches_opp_box",
             "defensive_contributions","tackles","fouls_committed","xg_against_on_pitch"]
print(f"wc26_player_fotmob_wc: {len(wc_df)}")
for c in deep_cols:
    if c in wc_df.columns:
        filled = wc_df[c].notna().sum()
        print(f"  {c:28s}: {filled}/{len(wc_df)} ({100*filled/len(wc_df):.1f}%)")

# Sort recent matches newest-first.
if len(form_df):
    form_df = form_df.sort_values(["fifa_player_id", "match_date_utc"], ascending=[True, False])

print(f"wc26_player_recent_matches_fotmob: {len(form_df)}")

io.save_table(wc_df, "wc26_player_fotmob_wc")
io.save_table(form_df, "wc26_player_recent_matches_fotmob")

# Patch wc26_player_enrichment with the fresh WC line. nb_08 fills wc_rating /
# wc_goals / wc_assists / wc_yellow_cards / wc_red_cards from FotMob's team-page
# snapshot (cached daily). Our matchDetails-aggregated wc_df is fresher and
# covers more players (1200+ vs ~700). Overwrite where wc_df has a value.
try:
    enr = io.load_table("wc26_player_enrichment")
    patch = wc_df[["fifa_player_id", "fotmob_rating", "goals", "assists",
                   "yellow_cards", "red_cards"]].rename(columns={
        "fotmob_rating": "_wc_rating_new",
        "goals":         "_wc_goals_new",
        "assists":       "_wc_assists_new",
        "yellow_cards":  "_wc_yc_new",
        "red_cards":     "_wc_rc_new",
    })
    enr = enr.merge(patch, on="fifa_player_id", how="left")
    enr["wc_rating"]       = enr["_wc_rating_new"].fillna(enr["wc_rating"])
    enr["wc_goals"]        = enr["_wc_goals_new"].fillna(enr["wc_goals"])
    enr["wc_assists"]      = enr["_wc_assists_new"].fillna(enr["wc_assists"])
    enr["wc_yellow_cards"] = enr["_wc_yc_new"].fillna(enr["wc_yellow_cards"])
    enr["wc_red_cards"]    = enr["_wc_rc_new"].fillna(enr["wc_red_cards"])
    enr = enr.drop(columns=["_wc_rating_new", "_wc_goals_new", "_wc_assists_new",
                            "_wc_yc_new", "_wc_rc_new"])
    io.save_table(enr, "wc26_player_enrichment")
    print(f"patched enrichment WC fields: wc_rating now {enr['wc_rating'].notna().sum()}/{len(enr)} filled")
except FileNotFoundError:
    print("wc26_player_enrichment not yet built — skipping enrichment patch")
""")

md("""## 3. Coverage cross-check vs `METRICS_AUDIT.md`

The audit doc's FotMob "WC2026 Tournament Stats" grid lists 14 fields per player (minutes, matchesPlayed, goals, xg, goalsPlusAssists, shots, shotsOnTarget, passAccuracy, chancesCreated, touchesOppBox, defensiveContributions, cleanSheets, yellowCards, redCards). Of those:

- `minutes`, `matchesPlayed`, `goals`, `assists`, `yellowCards`, `redCards` — landed in `wc26_player_recent_matches_fotmob` per match (sum/count over leagueId=77 rows).
- The rest (xg, shots, passAccuracy, chancesCreated, etc.) are FotMob "deep" stats only exposed when WC is the player's default tournament. For the comprehensive per-player WC roll-up, **`wc26_player_season_stats` (from fdh-api) is the source-of-truth** — it covers 119 stat keys per player including all of these plus distance/sprint/pressure metrics FotMob doesn't publish.

This notebook closes the form/rating gap; the deep stats gap is covered by Notebook 09 (FIFA fdh).""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("11_fotmob_wc_and_form.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 11_fotmob_wc_and_form.ipynb")
