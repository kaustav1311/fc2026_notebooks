"""365scores trends fetcher — warehouse-side mirror of the PWA's
src/services/scores365.ts.

Each WC match has a 365scores `gameId` (independent of FotMob / FIFA IDs).
For every game we pull a `trends[]` payload — short data-driven sentences
365 surfaces in the match card. Each trend carries:

  - `text` (display sentence), `cause`, `betCTA`
  - `percentage` (0..1 ratio, e.g. 0.857 = 6/7 matches)
  - `lineTypeId` (1=result, 3=totals, 5=1st-half, 7=first-goal, 12=BTTS, 14=double-chance)
  - `isTop` (one trend per game is the headline)
  - `confidenceTrendIds` (back-up trends supporting the top one)
  - `outcome` (1=trend hit, 2=trend missed, null=pending — populated post-match)

Each snapshot gets a `snapshot_ts` so we can study how a trend's `percentage`
evolved through the days before kickoff (informs A12/A13 in the recommender
factor catalog).

Endpoints (CORS-friendly public API, no auth):
  - /web/games/fixtures/        — upcoming sliding window
  - /web/games/results/         — recent sliding window
  - /web/games/?startDate=...   — exhaustive date-range pull
  - /web/trends/?games={id}     — trends for one game
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Iterable

import pandas as pd

from lib.http import polite_get

HOST = "https://webws.365scores.com"
QS_BASE = "appTypeId=5&langId=1&timezoneName=Asia%2FCalcutta&userCountryId=80"
WC_COMPETITION_ID = 5930

# Sliding fixtures + results capture the live window; date-range pulls catch
# everything older that fell out of the slide. Mirrors the PWA layout.
WC_DATE_WINDOWS: list[tuple[str, str]] = [
    ("10/06/2026", "30/06/2026"),  # group stage
    ("26/06/2026", "20/07/2026"),  # KO (overlap by a few days)
]


def _get_json(url: str) -> dict | None:
    r = polite_get(url, sleep=0.5)  # gentle pacing — public free endpoint
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def fetch_games_index() -> list[dict]:
    """Discover all WC games + their 365 gameIds. Dedup on id."""
    seen: set[int] = set()
    out: list[dict] = []

    # 1. sliding windows
    for endpoint in ("fixtures", "results"):
        url = f"{HOST}/web/games/{endpoint}/?{QS_BASE}&competitions={WC_COMPETITION_ID}"
        data = _get_json(url) or {}
        for g in data.get("games", []) or []:
            gid = g.get("id")
            if gid is None or gid in seen:
                continue
            seen.add(gid)
            out.append(g)

    # 2. static date ranges
    for start, end in WC_DATE_WINDOWS:
        url = (f"{HOST}/web/games/?{QS_BASE}"
               f"&competitions={WC_COMPETITION_ID}"
               f"&startDate={start}&endDate={end}")
        data = _get_json(url) or {}
        for g in data.get("games", []) or []:
            gid = g.get("id")
            if gid is None or gid in seen:
                continue
            seen.add(gid)
            out.append(g)

    return out


def fetch_trends_for_game(game_id: int) -> list[dict]:
    """Pull the trends[] block for one 365scores game. Returns raw entries
    (one row per trend)."""
    url = f"{HOST}/web/trends/?{QS_BASE}&games={game_id}"
    data = _get_json(url) or {}
    return data.get("trends", []) or []


def build(matches_df: pd.DataFrame) -> pd.DataFrame:
    """Build wc26_match_trends_365 dataframe.

    Args:
      matches_df: must carry `espn_match_id` plus home/away nation names so
                  we can match 365's competitor names. Typically
                  `wc26_stg_matches` joined to `wc26_stg_nations` for names.

    Returns: long-format df, one row per (espn_match_id, trend_id, snapshot_ts).
    """
    games = fetch_games_index()
    print(f"  discovered {len(games)} 365scores games")

    # Build (normalized home, normalized away) -> 365 game_id map
    def norm(s: str) -> str:
        if not s:
            return ""
        return (s.lower()
                .replace("é", "e").replace("í", "i").replace("ó", "o")
                .replace("ú", "u").replace("ñ", "n").replace("ç", "c")
                .replace("ä", "a").replace("ö", "o").replace("ü", "u")
                .replace(" ", "").replace(".", "").replace("'", "").replace("-", ""))

    by_pair: dict[tuple[str, str], int] = {}
    for g in games:
        h = norm((g.get("homeCompetitor") or {}).get("name") or "")
        a = norm((g.get("awayCompetitor") or {}).get("name") or "")
        gid = g.get("id")
        if h and a and gid is not None:
            by_pair[(h, a)] = gid
            by_pair[(a, h)] = gid

    snapshot_ts = datetime.now(timezone.utc).isoformat()

    rows: list[dict] = []
    for _, m in matches_df.iterrows():
        h = norm(m.get("home_nation_name") or m.get("home_nation_id") or "")
        a = norm(m.get("away_nation_name") or m.get("away_nation_id") or "")
        gid = by_pair.get((h, a))
        if gid is None:
            continue
        trends = fetch_trends_for_game(int(gid))
        for t in trends:
            rows.append({
                "espn_match_id": m.get("espn_match_id"),
                "scores365_game_id": gid,
                "trend_id": t.get("id"),
                "snapshot_ts": snapshot_ts,
                "text": t.get("text"),
                "cause": t.get("cause"),
                "betCTA": t.get("betCTA"),
                "percentage": t.get("percentage"),
                "lineTypeId": t.get("lineTypeId"),
                "isTop": bool(t.get("isTop")),
                "outcome": t.get("outcome"),  # 1=hit, 2=miss, null=pending
                "competitorIds": json.dumps(t.get("competitorIds") or []),
                "confidenceTrendIds": json.dumps(t.get("confidenceTrendIds") or []),
            })
        time.sleep(0.3)  # gentle pacing between game-trend calls

    return pd.DataFrame(rows)
