"""Author 13_polymarket.ipynb — per-fixture market data."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 13 — Polymarket per-match volumes + odds

Closes the Phase 4 dependency for `D-1-Doc §2`'s Fixture Weight term. Each WC26 match has up to two Polymarket events:

1. **Base event** — slug `fifwc-{HOME}-{AWAY}-{YYYY-MM-DD}` — three markets (`Will X win?`, `Will Y win?`, `Will X vs Y end in a draw?`). This is the 1X2 moneyline.
2. **More-markets event** — slug `…-more-markets` — typically ~38 markets per fixture (spreads at ±1.5/±2.5, over/under totals from 0.5 to 5.5, clean sheet, BTTS, sometimes goalscorer markets).

Volume and `lastTradePrice` (= implied probability) per market is the key signal. For closed (resolved) markets `outcomePrices` is binary `[1, 0]` or `[0, 1]`; for live markets it's a float pair.

Output tables
- `wc26_match_polymarket_events` — one row per (match × Polymarket event id) with event-level totals (volume, openInterest, closed flag, dates).
- `wc26_match_polymarket_markets` — one row per (event × market) with market question, outcomes, last price, volume, liquidity, spread.

Both tables share `espn_match_id` as the FK back to `wc26_matches`.""")

code("""import sys, json, re
from datetime import datetime
from pathlib import Path
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import io
from lib.http import polite_get

matches = io.load_table("wc26_matches")
nations = io.load_table("wc26_nations")
nation_name_map = nations.set_index("seed_name")["nation_id"].to_dict()
# Also build a reverse name aliases for Polymarket title matching
ALIASES = {}
for _, n in nations.iterrows():
    nid = n["nation_id"]
    for nm in (n["all_names"] if hasattr(n["all_names"], "__iter__") else []):
        if isinstance(nm, str):
            ALIASES[nm.lower()] = nid
    ALIASES[n["seed_name"].lower()] = nid
# Polymarket-specific name quirks
ALIASES.update({
    "korea republic": "KOR", "south korea": "KOR",
    "cabo verde": "CPV", "cape verde": "CPV",
    "türkiye": "TUR", "turkiye": "TUR", "turkey": "TUR",
    "côte d'ivoire": "CIV", "ivory coast": "CIV", "cote d'ivoire": "CIV",
    "bosnia-herzegovina": "BIH", "bosnia & herzegovina": "BIH",
    "united states": "USA", "usa": "USA",
    "ir iran": "IRN", "iran": "IRN",
    "dr congo": "COD", "congo dr": "COD",
})
print(f"matches: {len(matches)}  alias keys: {len(ALIASES)}")
""")

md("## 1. Walk Polymarket WC events (paginated)")

code("""def fetch_wc_events():
    events = []
    for off in range(0, 1200, 100):
        try:
            r = polite_get(
                f"https://gamma-api.polymarket.com/events?limit=100&offset={off}&tag_slug=fifa-world-cup",
                headers={"Accept": "application/json", "Referer": "https://polymarket.com/"},
            )
        except Exception:
            break
        if not r.ok:
            break
        d = r.json()
        if not d:
            break
        events.extend(d)
        if len(d) < 100:
            break
    return events

# Cache the whole list once a day — fast walk, light.
import json as _json
cached = io.latest_raw("polymarket", "wc_events_all")
if cached is None:
    events = fetch_wc_events()
    io.cache_raw(
        f"https://gamma-api.polymarket.com/events?tag_slug=fifa-world-cup&__bulk=true",
        source="polymarket", name="wc_events_all",
        # cache_raw hits the URL itself — we want our pre-collected list instead.
        # workaround: serialize and write through the same path.
    )
    # cache_raw fetched a single page; overwrite the file with our full list.
    cache_path = sorted((io.RAW / "polymarket").glob("*_wc_events_all.json"))[-1]
    cache_path.write_text(_json.dumps(events), encoding="utf-8")
else:
    events = cached

print(f"WC events pulled: {len(events)}")
print(f"  closed: {sum(1 for e in events if e.get('closed'))}")
print(f"  active: {sum(1 for e in events if e.get('active'))}")
""")

md("## 2. Parse per-fixture events — title 'X vs. Y' + a date")

code("""# Pattern: title like "United States vs. Paraguay"; startDate or endDate identifies the match.
# Slug like "fifwc-usa-par-2026-06-12" or "...-more-markets".
TITLE_RE = re.compile(r"^(?P<home>[^,]+?)\\s+vs\\.?\\s+(?P<away>[^,–-]+?)(?:\\s*[–-]\\s*More Markets)?\\s*$", re.I)

def to_nid(name):
    return ALIASES.get((name or "").strip().lower())

per_match = []
unmatched = []
for e in events:
    title = (e.get("title") or "").strip()
    m = TITLE_RE.match(title)
    if not m:
        continue
    home_nid = to_nid(m.group("home"))
    away_nid = to_nid(m.group("away"))
    if not home_nid or not away_nid:
        unmatched.append((title, e.get("slug")))
        continue
    per_match.append({
        "polymarket_event_id": e["id"],
        "polymarket_slug": e["slug"],
        "title": title,
        "is_more_markets": "more-markets" in e.get("slug", "").lower(),
        "home_nation_id": home_nid,
        "away_nation_id": away_nid,
        "start_date": e.get("startDate"),
        "end_date": e.get("endDate"),
        "closed": e.get("closed"),
        "active": e.get("active"),
        "volume": e.get("volume"),
        "open_interest": e.get("openInterest"),
        "volume_1wk": e.get("volume1wk"),
        "volume_1mo": e.get("volume1mo"),
        "comment_count": e.get("commentCount"),
        "markets_raw": e.get("markets") or [],
    })

print(f"per-match events: {len(per_match)}")
print(f"unmatched name pairs (first 5): {unmatched[:5]}")
""")

md("## 3. Join per-match events to `wc26_matches` by (home, away, end_date≈kickoff_utc)")

code("""# Each Polymarket event's endDate is roughly kickoff_utc + match length (~2h),
# so we match on (home, away) within the same calendar day.
match_lookup = {}
for _, r in matches.iterrows():
    if pd.isna(r["home_nation_id"]) or pd.isna(r["away_nation_id"]) or pd.isna(r["kickoff_utc"]):
        continue
    ku = pd.Timestamp(r["kickoff_utc"])
    key = (r["home_nation_id"], r["away_nation_id"], ku.date().isoformat())
    match_lookup[key] = r["espn_match_id"]

evts_df = pd.DataFrame(per_match)
def lookup_match(row):
    if not row.get("end_date"):
        return None
    end = pd.Timestamp(row["end_date"])
    # Try the kickoff-day, the day before (UTC quirks), and day after.
    for delta in (0, -1, 1):
        d = (end + pd.Timedelta(days=delta)).date().isoformat()
        mid = match_lookup.get((row["home_nation_id"], row["away_nation_id"], d))
        if mid:
            return mid
    return None
evts_df["espn_match_id"] = evts_df.apply(lookup_match, axis=1)
matched = evts_df["espn_match_id"].notna().sum()
print(f"matched to wc26_matches: {matched}/{len(evts_df)}")
distinct = evts_df.dropna(subset=["espn_match_id"])["espn_match_id"].nunique()
print(f"distinct WC fixtures covered: {distinct}/104")
""")

md("## 4. Explode markets — one row per (event × market)")

code("""def safe_jsonloads(v):
    if isinstance(v, list): return v
    if isinstance(v, str):
        try: return json.loads(v)
        except Exception: return None
    return None

market_rows = []
for r in evts_df.itertuples():
    for m in r.markets_raw or []:
        outcomes = safe_jsonloads(m.get("outcomes")) or []
        prices = safe_jsonloads(m.get("outcomePrices")) or []
        market_rows.append({
            "polymarket_event_id": r.polymarket_event_id,
            "espn_match_id": r.espn_match_id,
            "polymarket_market_id": m.get("id"),
            "condition_id": m.get("conditionId"),
            "question": m.get("question"),
            "outcomes": outcomes,
            "outcome_prices": [float(p) if isinstance(p,str) and p else p for p in prices],
            "last_trade_price": m.get("lastTradePrice"),
            "best_bid": m.get("bestBid"),
            "best_ask": m.get("bestAsk"),
            "spread": m.get("spread"),
            "liquidity": m.get("liquidity"),
            "volume": m.get("volume"),
            "volume_24hr": m.get("volume24hr"),
            "closed": m.get("closed"),
            "active": m.get("active"),
            "market_slug": m.get("slug"),
            "market_end_date": m.get("endDate"),
        })
markets_df = pd.DataFrame(market_rows)
print(f"markets exploded: {len(markets_df)}")
print(f"  with espn_match_id linked: {markets_df['espn_match_id'].notna().sum()}")
print(f"\\nmarket-question categories (top 8 patterns):")
def category(q):
    q = (q or "").lower()
    if "draw" in q: return "draw"
    if "win" in q and " on " in q: return "moneyline"
    if "spread" in q: return "spread"
    if "o/u" in q or "over/under" in q: return "over_under"
    if "clean sheet" in q: return "clean_sheet"
    if "to score" in q or "scorer" in q: return "goalscorer"
    if "btts" in q or "both teams" in q: return "btts"
    return "other"
markets_df["category"] = markets_df["question"].map(category)
print(markets_df["category"].value_counts().to_string())
""")

md("## 5. Save base markets table")

code("""io.save_table(markets_df, "wc26_match_polymarket_markets")
""")

md("""## 6. Consolidated per-match volume

`wc26_polymarket_match_volume` — one row per `espn_match_id`. Polymarket usually
runs **two** events per fixture: the base 1X2 event (moneyline + draw markets)
and a `…-more-markets` event (over/under, spread, goalscorer, etc.). Both
event IDs are clubbed here, with their volumes split into:

- `volume_moneyline` — sum of `category in ('moneyline', 'draw')`
- `volume_other`     — sum of everything else (over_under + spread + goalscorer + …)

Status flags `closed_all` (True iff every market closed) and `any_active`
(True iff any market is still active) summarize the live state.""")

code("""mk = markets_df.copy()
mk["volume_num"] = pd.to_numeric(mk["volume"], errors="coerce").fillna(0.0)
mk["bucket"] = mk["category"].isin(["moneyline", "draw"]).map({True: "moneyline", False: "other"})

# Per-match volume + counts
vol_pivot = mk.pivot_table(index="espn_match_id", columns="bucket",
                           values="volume_num", aggfunc="sum", fill_value=0.0)
cnt_pivot = mk.pivot_table(index="espn_match_id", columns="bucket",
                           values="polymarket_market_id", aggfunc="count", fill_value=0)
for b in ("moneyline", "other"):
    if b not in vol_pivot.columns: vol_pivot[b] = 0.0
    if b not in cnt_pivot.columns: cnt_pivot[b] = 0

# Status flags
status_grp = mk.groupby("espn_match_id").agg(
    closed_all=("closed", "all"),
    any_active=("active", "any"),
)

# Club the polymarket_event_ids: base + more-markets per match.
event_pivot = (
    evts_df.assign(_kind=evts_df["is_more_markets"].map({True: "polymarket_event_id_more",
                                                         False: "polymarket_event_id_base"}))
           .pivot_table(index="espn_match_id", columns="_kind",
                        values="polymarket_event_id", aggfunc="first")
)
for c in ("polymarket_event_id_base", "polymarket_event_id_more"):
    if c not in event_pivot.columns:
        event_pivot[c] = pd.NA

# Match metadata — pick the base-event row when present for title/dates/nations.
meta = (evts_df.sort_values("is_more_markets")  # False first → base event wins
              .drop_duplicates("espn_match_id")[
                  ["espn_match_id", "title", "home_nation_id", "away_nation_id",
                   "start_date", "end_date"]])

match_vol = (
    vol_pivot.rename(columns={"moneyline": "volume_moneyline", "other": "volume_other"})
             .join(cnt_pivot.rename(columns={"moneyline": "num_moneyline_markets",
                                              "other": "num_other_markets"}))
             .join(status_grp)
             .join(event_pivot)
             .reset_index()
             .merge(meta, on="espn_match_id", how="left")
)
match_vol["total_volume"] = match_vol["volume_moneyline"] + match_vol["volume_other"]
match_vol = match_vol[[
    "espn_match_id", "title", "home_nation_id", "away_nation_id",
    "start_date", "end_date",
    "polymarket_event_id_base", "polymarket_event_id_more",
    "closed_all", "any_active",
    "volume_moneyline", "volume_other", "total_volume",
    "num_moneyline_markets", "num_other_markets",
]].sort_values("total_volume", ascending=False).reset_index(drop=True)

io.save_table(match_vol, "wc26_polymarket_match_volume")
print(f"per-match volume rows: {len(match_vol)}")
print()
print("totals (USD):")
print(f"  moneyline+draw : {match_vol['volume_moneyline'].sum():>16,.0f}")
print(f"  other          : {match_vol['volume_other'].sum():>16,.0f}")
print(f"  total          : {match_vol['total_volume'].sum():>16,.0f}")
print()
print("top 5 by total volume:")
print(match_vol.head(5)[["title", "volume_moneyline", "volume_other", "total_volume"]].to_string(index=False))
""")

md("""## 7. World Cup winner — daily implied-probability history

Pulls `Will {Nation} win the 2026 FIFA World Cup?` for each of the ~60 nations from Polymarket's `world-cup-winner` event, then hits the CLOB `prices-history` endpoint at **daily fidelity** (1440-minute buckets) from **2026-05-01** to today.

The returned `yes_price` is the implied probability of that nation winning the tournament. Feed straight into an animated bar chart (one frame per `date_utc`, bars per `nation_id`, height = `yes_price`).

Output: `wc26_polymarket_winner_history` — long format `(nation_id, date_utc, yes_price)` plus market metadata for joining back.""")

code("""# Locate the world-cup-winner event in the cached events list.
import time as _time
from datetime import datetime, timezone, timedelta

all_events = io.latest_raw("polymarket", "wc_events_all")
if all_events is None:
    raise RuntimeError("run cells 1-2 to populate the cached events list first")
winner_event = next((e for e in all_events if e.get("slug") == "world-cup-winner"), None)
if winner_event is None:
    raise RuntimeError("world-cup-winner event not present in cached list — re-fetch events")

# Per-market: nation match via ALIASES, plus the YES clobTokenId.
import re as _re
WINNER_QUESTION_RE = _re.compile(r"^Will (.+?) win the 2026 FIFA World Cup\\?$", _re.I)
winner_markets = []
for m in (winner_event.get("markets") or []):
    q = m.get("question") or ""
    mm = WINNER_QUESTION_RE.match(q.strip())
    if not mm:
        continue
    nation_name = mm.group(1).strip()
    nid = ALIASES.get(nation_name.lower())
    tokens = m.get("clobTokenIds")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except Exception:
            tokens = None
    yes_token = tokens[0] if isinstance(tokens, list) and tokens else None
    winner_markets.append({
        "polymarket_market_id": m.get("id"),
        "market_slug": m.get("slug"),
        "question": q,
        "nation_name_polymarket": nation_name,
        "nation_id": nid,
        "yes_token": yes_token,
        "last_trade_price": m.get("lastTradePrice"),
    })

wm_df = pd.DataFrame(winner_markets)
print(f"WC winner markets parsed: {len(wm_df)}")
print(f"  with nation_id resolved: {wm_df['nation_id'].notna().sum()}/{len(wm_df)}")
unmatched = wm_df[wm_df["nation_id"].isna()][["nation_name_polymarket"]]
if len(unmatched):
    print("  (unresolved — likely already-eliminated nations or 'another team'):")
    print(unmatched.to_string(index=False))
""")

code("""# CLOB prices-history caps any single startTs→endTs range to roughly a month;
# the simpler path is interval=max&fidelity=1440 (daily buckets, full history),
# then trim to the analysis window in pandas. Window: 2026-05-01 → today.
WINDOW_START = "2026-05-01"

PH_URL = "https://clob.polymarket.com/prices-history?market={tok}&interval=max&fidelity=1440"

hist_rows = []
errors = 0
for r in wm_df.itertuples():
    if not r.yes_token or not r.nation_id:
        continue
    url = PH_URL.format(tok=r.yes_token)
    try:
        data = io.cache_raw(
            url, source="polymarket",
            name=f"winner_hist_{r.polymarket_market_id}",
            sleep=0.25,
        )
    except Exception as e:
        errors += 1
        continue
    pts = data.get("history") or []
    for pt in pts:
        ts = pt.get("t")
        if ts is None:
            continue
        d = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
        if d < WINDOW_START:
            continue
        hist_rows.append({
            "nation_id": r.nation_id,
            "polymarket_market_id": r.polymarket_market_id,
            "market_slug": r.market_slug,
            "question": r.question,
            "date_utc": d,
            "yes_price": float(pt.get("p")) if pt.get("p") is not None else None,
        })

hist_df = pd.DataFrame(hist_rows)
# Polymarket returns several ticks per day at fidelity=1440 boundaries; keep the
# last observation per (nation_id, date_utc) so each row is one daily snapshot.
if len(hist_df):
    hist_df = (hist_df.sort_values("date_utc")
                       .drop_duplicates(["nation_id", "date_utc"], keep="last")
                       .reset_index(drop=True))

io.save_table(hist_df, "wc26_polymarket_winner_history")
print(f"winner-history rows: {len(hist_df)}  errors: {errors}")
if len(hist_df):
    print(f"  date range: {hist_df['date_utc'].min()} → {hist_df['date_utc'].max()}")
    print(f"  distinct nations: {hist_df['nation_id'].nunique()}")
    print()
    print("latest snapshot — top 10 by yes_price:")
    latest_date = hist_df['date_utc'].max()
    latest = hist_df[hist_df['date_utc'] == latest_date].sort_values('yes_price', ascending=False).head(10)
    print(latest[['date_utc', 'nation_id', 'yes_price']].to_string(index=False))
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("13_polymarket.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 13_polymarket.ipynb")
