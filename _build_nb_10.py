"""Author 10_fifa_fantasy.ipynb."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 10 — FIFA Fantasy

Closes the gap from `D-1-Doc.txt §4` — `selected_by_pct` is the missing input for the Scouting Premium formula. The `play.fifa.com/json/fantasy/players.json` endpoint exposes it under `percentSelected`, plus the full set of fantasy mechanics needed for daily optimization.

Endpoints
- `play.fifa.com/json/fantasy/rounds.json` — gameweeks + per-round fixtures with deadlines, venues, status.
- `play.fifa.com/json/fantasy/squads.json` — 48 fantasy squads (= the 48 WC nations), with `id` ↔ `abbr` mapping.
- `play.fifa.com/json/fantasy/players.json` — 1488 players. Every row carries `fifaId` → direct join into `wc26_players`, no fuzzy matching.
- `play.fifa.com/json/fantasy/player_stats/{fantasy_id}.json` — per-round raw stats per player (GS, AS, MP, YC, RC, CS, ST, T, CC, SB, …) and points scored.

(The `/api/en/fantasy/team/history/{id}` endpoint is auth-gated (403) — fantasy team data is per-user. Skipped here; only the team-OWNER's session can hit it.)

Output tables
- `fantasy_rounds` — one row per round (id, status, start/end UTC, fantasy windows).
- `fantasy_round_matches` — one row per fixture per round (venue, status, scores, suspended flag).
- `fantasy_squads` — 48 rows, fantasy_squad_id ↔ nation_abbr.
- `fantasy_players` — 1488 rows, with `fifa_player_id` join, price, percentSelected, totalPoints, form, lastRoundPoints, roundPoints (as JSON).
- `fantasy_player_round_stats` — long: per (fantasy_player_id, round_id) raw stats + points.""")

code("""import sys, json
from pathlib import Path
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import io
from lib.http import polite_get

HEADERS = {"Accept": "application/json", "Referer": "https://play.fifa.com/"}
""")

md("## 1. Rounds")

code("""rounds = io.cache_raw("https://play.fifa.com/json/fantasy/rounds.json",
                       source="fifa_fantasy", name="rounds")

round_rows = []
match_rows = []
for r in rounds:
    round_rows.append({
        "round_id": r.get("id"),
        "status": r.get("status"),
        "start_date": r.get("startDate"),
        "end_date": r.get("endDate"),
        "match_count": len(r.get("tournaments") or []),
    })
    for t in (r.get("tournaments") or []):
        match_rows.append({
            "round_id": r.get("id"),
            "fantasy_match_id": t.get("id"),
            "venue_id": t.get("venueId"),
            "venue_name": t.get("venueName"),
            "venue_city": t.get("venueCity"),
            "date": t.get("date"),
            "status": t.get("status"),
            "period": t.get("period"),
            "minutes": t.get("minutes"),
            "extra_minutes": t.get("extraMinutes"),
            "is_suspended": t.get("isSuspended"),
            "home_squad_id": t.get("homeSquadId"),
            "home_squad_name": t.get("homeSquadName"),
            "away_squad_id": t.get("awaySquadId"),
            "away_squad_name": t.get("awaySquadName"),
            "home_score": t.get("homeScore"),
            "away_score": t.get("awayScore"),
        })

rounds_df = pd.DataFrame(round_rows)
matches_df = pd.DataFrame(match_rows)
print(f"rounds: {len(rounds_df)}")
print(f"round-matches: {len(matches_df)}")
io.save_table(rounds_df, "fantasy_rounds")
io.save_table(matches_df, "fantasy_round_matches")
""")

md("## 2. Squads (nation mapping)")

code("""squads = io.cache_raw("https://play.fifa.com/json/fantasy/squads.json",
                       source="fifa_fantasy", name="squads")

squads_df = pd.DataFrame([{
    "fantasy_squad_id": s.get("id"),
    "name": s.get("name"),
    "group": s.get("group"),
    "abbr": s.get("abbr"),
    "is_eliminated": s.get("isEliminated"),
} for s in squads])
print(f"squads: {len(squads_df)}")

# Sanity: fantasy abbr should match our nation_id for almost all 48.
nations = io.load_table("wc26_nations")
unmatched = set(squads_df["abbr"]) - set(nations["nation_id"])
if unmatched:
    print(f"abbr mismatches vs wc26_nations: {sorted(unmatched)}")

io.save_table(squads_df, "fantasy_squads")
""")

md("""## 3. Players (with `fifaId` join)

This is the one table the whole daily-optimizer ultimately keys on. Note `percentSelected` is the input to the Scouting Premium logarithmic boost from `D-1-Doc.txt §2`.""")

code("""import unicodedata, re

players = io.cache_raw("https://play.fifa.com/json/fantasy/players.json",
                        source="fifa_fantasy", name="players")

prows = []
for p in players:
    stats = p.get("stats") or {}
    prows.append({
        "fantasy_player_id": p.get("id"),
        "fifa_player_id": p.get("fifaId"),
        "fantasy_squad_id": p.get("squadId"),
        "first_name": p.get("firstName"),
        "last_name": p.get("lastName"),
        "known_name": p.get("knownName"),
        "position": p.get("position"),
        "price": p.get("price"),
        # is_active: false when FIFA Fantasy has marked the player 'transferred'
        # (cut from the active 26-man WC26 squad). A late call-up flips this
        # back to true at the next 3h refresh — that's the signal to watch.
        "is_active": p.get("status") != "transferred",
        "percent_selected": p.get("percentSelected"),
        "rounds_selected_json": json.dumps(p.get("roundsSelected") or {}),
        "total_points": stats.get("totalPoints"),
        "avg_points": stats.get("avgPoints"),
        "form": stats.get("form"),
        "last_round_points": stats.get("lastRoundPoints"),
        "round_points_json": json.dumps(stats.get("roundPoints") or {}),
        "qualification_round_ids_json": json.dumps(p.get("qualificationRoundIds") or []),
    })
players_df = pd.DataFrame(prows)
print(f"fantasy players: {len(players_df)} (active: {players_df['is_active'].sum()}, transferred: {(~players_df['is_active']).sum()})")
print(f"with fifa_player_id link (raw): {players_df['fifa_player_id'].notna().sum()}")

# ── known_name fallback: when FIFA Fantasy doesn't supply a knownName, fall back
#    to "first_name last_name". Trim whitespace where one side is missing.
def _knownname_fallback(row):
    kn = row["known_name"]
    if isinstance(kn, str) and kn.strip():
        return kn
    parts = [str(row[c]).strip() for c in ("first_name", "last_name") if isinstance(row[c], str) and row[c].strip()]
    return " ".join(parts) or None
players_df["known_name"] = players_df.apply(_knownname_fallback, axis=1)
print(f"known_name populated after fallback: {players_df['known_name'].notna().sum()}/{len(players_df)}")

# ── fifa_player_id backfill via name match against wc26_players (resolved by
#    nation through fantasy_squads.abbr → wc26_nations.nation_id).
wcp = io.load_table("wc26_player_enrichment")
fs = io.load_table("fantasy_squads")
nations = io.load_table("wc26_nations")
sq_to_nat = fs.set_index("fantasy_squad_id")["abbr"].to_dict()
abbr_to_nid = dict(zip(nations["espn_abbreviation"], nations["nation_id"]))
def _norm(s):
    if not isinstance(s, str): return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z\\s]+", "", s.lower()).strip()
wcp = wcp.copy()
wcp["_last"] = wcp["name"].map(_norm).str.split().str[-1]
wcp["_full"] = wcp["name"].map(_norm)

backfilled = 0
ambiguous = 0
for i in players_df.index[players_df["fifa_player_id"].isna()]:
    row = players_df.loc[i]
    nat = abbr_to_nid.get(sq_to_nat.get(row["fantasy_squad_id"]))
    if not nat:
        continue
    ln = _norm(row["last_name"])
    if not ln:
        continue
    cand = wcp[(wcp["nation_id"] == nat) & (wcp["_last"] == ln.split()[-1])]
    if len(cand) == 1:
        players_df.at[i, "fifa_player_id"] = cand.iloc[0]["fifa_player_id"]
        backfilled += 1
    elif len(cand) > 1:
        fn = _norm(row["first_name"]).split()[0] if isinstance(row["first_name"], str) else ""
        cand2 = cand[cand["_full"].str.contains(fn, na=False)] if fn else cand
        if len(cand2) == 1:
            players_df.at[i, "fifa_player_id"] = cand2.iloc[0]["fifa_player_id"]
            backfilled += 1
        else:
            ambiguous += 1
print(f"fifa_player_id backfilled: {backfilled} (ambiguous: {ambiguous})")
print(f"with fifa_player_id link (final): {players_df['fifa_player_id'].notna().sum()}")
print(f"percent_selected populated: {players_df['percent_selected'].notna().sum()}")

# FK sanity
known = set(wcp["fifa_player_id"].dropna().astype(int))
fifa_ids = players_df["fifa_player_id"].dropna().astype(int)
matched = fifa_ids.isin(known).sum()
print(f"fifa_player_id matches wc26_players: {matched}/{len(fifa_ids)}")

io.save_table(players_df, "fantasy_players")
""")

md("""## 4. Per-player round stats (raw stat keys + points)

`player_stats/{fantasy_player_id}.json` returns one entry per round the player played. Stats keys: SXI (start XI flag), MP (minutes), AS (assists), YC (yellow), RC (red), OG (own goals), PW (penalty won), PC (penalty conceded), CS (clean sheets), GS (goals scored), GC (goals conceded), PS (penalty save), T (tackles), CC (chances created), ST (shots on target), FK (free kicks), S (saves), SB (Scouting Bonus eligible flag).

**Cost-aware refresh** (1488 fantasy_player_ids total, ~150 ms per call → 4 min if everyone is force-refreshed on every tick).

WC26 has 48 teams in 12 groups of 4. Each team plays 3 group matches, so the group stage runs over **3 Fantasy rounds × 24 matches per round = 72 matches**. Then knockout: R32 (16) → R16 (8) → QF (4) → SF (2) → 3rd-place + Final (2). **Total: 8 Fantasy rounds.**

Fantasy rounds are sequential — Round 1 ends, then Round 2 begins. But the 24 matches inside a single group-stage round are scheduled across several days, so within one round different squads finish their match on different days. The right unit to clock against is each squad's *most recent match end time*, not the round.

Per player: **has your squad played a match whose end time is later than the mtime of your cached stats file?** Force-refresh if ANY hold:

1. **No cached file yet** — one-time fetch.
2. **Squad has a LIVE match right now** (`status ∈ {playing, live}`) — mid-match overlay; stats tick continuously.
3. **Squad's most recent completed match ended AFTER the file's mtime** — captures the per-squad, within-round finalization moment.

Players whose squad hasn't played since their last cache → reuse cache. On a typical mid-tournament tick this drops re-fetches from 1488 → roughly the count of players whose squads played since the last cron tick.""")

code("""# Stat key → human-readable column name. Order preserved in the output frame.
STAT_KEY_RENAME = {
    "SXI": "starting_xi",
    "MP":  "minutes_played",
    "GS":  "goals_scored",
    "AS":  "assists",
    "CS":  "clean_sheet",
    "GC":  "goals_conceded",
    "YC":  "yellow_cards",
    "RC":  "red_cards",
    "OG":  "own_goals",
    "PW":  "penalty_won",
    "PC":  "penalty_conceded",
    "PS":  "penalty_saved",
    "S":   "saves",
    "T":   "tackles",
    "CC":  "chances_created",
    "ST":  "shots_on_target",
    "FK":  "free_kicks",
    "SB":  "scouting_bonus",
}

# === Per-player fetch plan: refresh only when the player's data has actually moved. ===
# Per-round stats finalize when their round flips to 'complete'. Between ticks, the
# right question is per-PLAYER, not per-round: has this player's squad played MORE
# RECENTLY than the timestamp we last wrote their cache file?
#
# Signals (a player is "hot" — needs force_refresh — if ANY apply):
#   1. They have no cached file yet.                                  one-time fetch
#   2. Their squad has a LIVE match right now (status playing/live).  mid-match overlay
#   3. Their squad's most recent COMPLETED match ENDED after the      round-end / overlap
#      mtime of their last cached stats file.
#
# We compare file mtime against match end timestamp (kickoff + 2 h buffer), so a
# match finishing at 3 pm refreshes players whose cache was written at 9 am the
# same day. Scales correctly across overlapping group-stage rounds (each squad's
# clock is independent) and through knockout (per-match independent moves).
import re as _re_p
import collections as _collections
from datetime import datetime as _dt, timezone as _tz

cached_dir = io.RAW / "fifa_fantasy"
cache_mtime_by_player: dict = {}
_fname_pat = _re_p.compile(r"^\\d{4}-\\d{2}-\\d{2}_player_stats_(\\d+)\\.json$")
if cached_dir.exists():
    for f in cached_dir.glob("*_player_stats_*.json"):
        mm = _fname_pat.match(f.name)
        if not mm:
            continue
        pid = int(mm.group(1))
        mtime = f.stat().st_mtime
        prev = cache_mtime_by_player.get(pid)
        if prev is None or mtime > prev:
            cache_mtime_by_player[pid] = mtime

# Build per-squad latest-end-timestamp + live-squad set.
# FIFA Fantasy status enum observed: 'scheduled' / 'playing' / 'live' / 'complete'.
squad_recent_end_ts: dict = _collections.defaultdict(float)
live_squad_ids: set = set()
HOT_STATUSES = {"complete", "playing", "live"}
MATCH_LEN_SEC = 2 * 3600  # 90 min + stoppage + post-match data finalization

for r in rounds:
    for t in (r.get("tournaments") or []):
        status = (t.get("status") or "").lower()
        if status not in HOT_STATUSES:
            continue
        end_iso = t.get("endDate") or t.get("date")
        if not end_iso:
            continue
        try:
            end_dt = _dt.fromisoformat(end_iso.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=_tz.utc)
            end_ts = end_dt.timestamp() + (MATCH_LEN_SEC if not t.get("endDate") else 0)
        except Exception:
            continue
        for sid in (t.get("homeSquadId"), t.get("awaySquadId")):
            if sid is None:
                continue
            if end_ts > squad_recent_end_ts[sid]:
                squad_recent_end_ts[sid] = end_ts
            if status in {"playing", "live"}:
                live_squad_ids.add(sid)

squad_by_player: dict = dict(zip(
    players_df["fantasy_player_id"].tolist(),
    players_df["fantasy_squad_id"].tolist() if "fantasy_squad_id" in players_df.columns else [None]*len(players_df),
))

to_refresh: set = set()
reasons = _collections.Counter()
for fpid_raw in players_df["fantasy_player_id"].dropna().tolist():
    fpid = int(fpid_raw)
    sid = squad_by_player.get(fpid_raw) or squad_by_player.get(fpid)
    cached_mtime = cache_mtime_by_player.get(fpid)
    if cached_mtime is None:
        to_refresh.add(fpid); reasons["never_cached"] += 1; continue
    if sid in live_squad_ids:
        to_refresh.add(fpid); reasons["live_squad"] += 1; continue
    last_end = squad_recent_end_ts.get(sid)
    if last_end and last_end > cached_mtime:
        to_refresh.add(fpid); reasons["squad_played_since_cache"] += 1

total_ids = int(players_df["fantasy_player_id"].notna().sum())
print(f"per-player fetch plan: total={total_ids}  to_force_refresh={len(to_refresh)}  "
      f"({dict(reasons)})  live_squads={len(live_squad_ids)}")

round_rows = []
errors = 0
for fpid in players_df["fantasy_player_id"].tolist():
    is_hot = int(fpid) in to_refresh if pd.notna(fpid) else False
    try:
        entries = io.cache_raw(
            f"https://play.fifa.com/json/fantasy/player_stats/{fpid}.json",
            source="fifa_fantasy", name=f"player_stats_{fpid}",
            sleep=(0.05 if is_hot else 0.0),
            force_refresh=is_hot,
        )
    except Exception:
        errors += 1
        continue
    if not isinstance(entries, list):
        continue
    for e in entries:
        st = e.get("stats") or {}
        round_rows.append({
            "fantasy_player_id": fpid,
            "round_id": e.get("roundId"),
            "tournament_id": e.get("tournamentId"),
            "points": e.get("points"),
            **{out_col: st.get(src_key) for src_key, out_col in STAT_KEY_RENAME.items()},
        })

prs_df = pd.DataFrame(round_rows)
print(f"per-player round-stats rows: {len(prs_df)}  errors: {errors}")
print(f"stat columns: {[c for c in prs_df.columns if c not in ('fantasy_player_id','round_id','tournament_id','points')]}")
io.save_table(prs_df, "fantasy_player_round_stats")
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("10_fifa_fantasy.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 10_fifa_fantasy.ipynb")
