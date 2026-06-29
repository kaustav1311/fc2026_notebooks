"""Polymarket tournament-winner probability snapshot.

Runs each hourly tick AFTER 13_polymarket. Fetches just the
`world-cup-winner` event from the Gamma API, parses each team's implied
probability (outcomePrices[0]), and appends one row per team to a
time-series parquet so the PWA can render a "winner probability over
time" race chart.

Design notes
------------
* Independent of 13_polymarket.ipynb. That notebook walks the full
  `tag_slug=fifa-world-cup` listing and builds per-match markets. This
  script only needs the one winner event so we hit `slug=world-cup-winner`
  directly — cheaper and avoids tangling with 13's caching path.
* Appends to `data/processed/wc26_polymarket_winner_history.parquet` with
  schema (snapshot_ts, team_name, nation_id, yes_price, pct, volume,
  market_id). Dedup key is (snapshot_ts, team_name) so re-running the
  same tick is idempotent.
* Trims snapshots older than 60 days so the parquet doesn't grow without
  bound across the tournament window.
* Failure-tolerant: any HTTP/parse error logs + exits 0 so the refresh
  pipeline isn't blocked by an upstream blip. The history file simply
  doesn't gain a new snapshot this tick.

Output is also consumed by _emit_pwa_json.py — see the EMIT list there.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))

from lib import io  # noqa: E402
from lib.http import polite_get  # noqa: E402

HISTORY_PARQUET = ROOT / "data" / "processed" / "wc26_polymarket_winner_history.parquet"
HISTORY_CSV     = ROOT / "data" / "processed" / "wc26_polymarket_winner_history.csv"
RETENTION_DAYS  = 60
GAMMA_URL       = "https://gamma-api.polymarket.com/events?slug=world-cup-winner"


# ── Polymarket team-name → warehouse nation_id ───────────────────────────
#
# Mirrors the alias resolution in 13_polymarket.ipynb. Kept independent so
# this script doesn't depend on that notebook running first — they both
# read wc26_nations from io.load_table for the canonical alias source.

POLYMARKET_NAME_QUIRKS: dict[str, str] = {
    "korea republic": "KOR",
    "south korea": "KOR",
    "cabo verde": "CPV",
    "cape verde": "CPV",
    "türkiye": "TUR",
    "turkiye": "TUR",
    "turkey": "TUR",
    "côte d'ivoire": "CIV",
    "ivory coast": "CIV",
    "cote d'ivoire": "CIV",
    "bosnia-herzegovina": "BIH",
    "bosnia & herzegovina": "BIH",
    "united states": "USA",
    "usa": "USA",
    "ir iran": "IRN",
    "iran": "IRN",
    "dr congo": "COD",
    "congo dr": "COD",
}


def build_alias_map() -> dict[str, str]:
    nations = io.load_table("wc26_nations")
    aliases: dict[str, str] = {}
    for _, n in nations.iterrows():
        nid = n["nation_id"]
        seed = n.get("seed_name")
        if isinstance(seed, str):
            aliases[seed.lower()] = nid
        all_names = n.get("all_names")
        if hasattr(all_names, "__iter__"):
            for nm in all_names:
                if isinstance(nm, str):
                    aliases[nm.lower()] = nid
    aliases.update(POLYMARKET_NAME_QUIRKS)
    return aliases


def name_to_nid(name: str | None, aliases: dict[str, str]) -> str | None:
    if not name:
        return None
    return aliases.get(name.strip().lower())


# ── Gamma fetch ───────────────────────────────────────────────────────────

def fetch_winner_event() -> dict | None:
    """Return the single world-cup-winner event payload, or None on failure."""
    try:
        r = polite_get(
            GAMMA_URL,
            headers={
                "Accept": "application/json",
                "Referer": "https://polymarket.com/",
            },
        )
    except Exception as exc:
        print(f"[poly-winner] fetch failed: {exc}")
        return None
    if not r.ok:
        print(f"[poly-winner] HTTP {r.status_code}")
        return None
    try:
        data = r.json()
    except Exception as exc:
        print(f"[poly-winner] JSON parse failed: {exc}")
        return None
    events = data if isinstance(data, list) else [data]
    if not events:
        print("[poly-winner] empty event list")
        return None
    return events[0]


def _safe_json_array(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            out = json.loads(v)
            return out if isinstance(out, list) else []
        except Exception:
            return []
    return []


def parse_snapshot(event: dict, aliases: dict[str, str]) -> pd.DataFrame:
    """Flatten the event's per-team markets into one row per team."""
    snapshot_ts = datetime.now(timezone.utc).isoformat()
    event_id = event.get("id")
    event_volume = event.get("volume")
    markets = event.get("markets") or []
    rows: list[dict] = []
    for m in markets:
        team = m.get("groupItemTitle")
        if not team or team == "Other":
            continue
        prices = _safe_json_array(m.get("outcomePrices"))
        if not prices:
            continue
        try:
            yes_price = float(prices[0])
        except (TypeError, ValueError):
            continue
        rows.append({
            "snapshot_ts": snapshot_ts,
            "polymarket_event_id": event_id,
            "polymarket_market_id": m.get("id"),
            "team_name": team,
            "nation_id": name_to_nid(team, aliases),
            "yes_price": yes_price,
            # Round-half-up at one decimal to match the PWA's display (so
            # sub-1% teams don't collapse to "0%").
            "pct": round(yes_price * 100, 1),
            "volume": float(m.get("volume") or 0) if m.get("volume") is not None else None,
            "volume_24hr": float(m.get("volume24hr") or 0) if m.get("volume24hr") is not None else None,
            "event_volume": float(event_volume) if event_volume is not None else None,
            "closed": bool(m.get("closed", False)),
            "active": bool(m.get("active", True)),
        })
    return pd.DataFrame(rows)


def append_history(snap: pd.DataFrame) -> pd.DataFrame:
    """Append today's snapshot to the long-format history; dedupe + trim."""
    HISTORY_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    if HISTORY_PARQUET.exists():
        prior = pd.read_parquet(HISTORY_PARQUET)
        combined = pd.concat([prior, snap], ignore_index=True)
    else:
        combined = snap.copy()

    # Dedup on (snapshot_ts, team_name) — re-running the same tick replaces.
    combined = combined.drop_duplicates(
        subset=["snapshot_ts", "team_name"], keep="last"
    )

    # Retention — drop rows older than RETENTION_DAYS days, preserving the
    # earliest snapshot of each calendar day so the race chart still has a
    # daily anchor over the whole tournament window.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    combined = combined[combined["snapshot_ts"] >= cutoff].copy()

    combined = combined.sort_values(["snapshot_ts", "team_name"]).reset_index(drop=True)
    return combined


# ── main ─────────────────────────────────────────────────────────────────

def main() -> int:
    print("[poly-winner] fetching world-cup-winner event ...")
    event = fetch_winner_event()
    if event is None:
        print("[poly-winner] no event — skipping snapshot (history left untouched).")
        return 0

    try:
        aliases = build_alias_map()
    except FileNotFoundError:
        print("[poly-winner] wc26_nations parquet missing — snapshot will have no nation_id mapping yet.")
        aliases = dict(POLYMARKET_NAME_QUIRKS)

    snap = parse_snapshot(event, aliases)
    if snap.empty:
        print("[poly-winner] event had no parseable team markets — skipping.")
        return 0

    snap_ts = snap.iloc[0]["snapshot_ts"]
    print(f"[poly-winner] snapshot {snap_ts} — {len(snap)} teams")
    unmatched = snap[snap["nation_id"].isna()]["team_name"].tolist()
    if unmatched:
        print(f"[poly-winner] team_name → nation_id unmapped: {unmatched}")

    combined = append_history(snap)
    combined.to_parquet(HISTORY_PARQUET, index=False)
    combined.to_csv(HISTORY_CSV, index=False)
    snapshots = combined["snapshot_ts"].nunique() if not combined.empty else 0
    print(
        f"[poly-winner] history rows={len(combined)}  distinct snapshots={snapshots}  "
        f"file={HISTORY_PARQUET.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
