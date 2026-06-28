"""Derive R32+ knockout pairings from finalized group standings.

Mirrors the PWA's engine/knockout.ts + engine/thirdPlace.ts + engine/standings.ts
flow in Python. Same data sources, same outputs — so when the warehouse
populates wc26_stg_matches.parquet's KO rows, both surfaces (PWA + recommender's
fixture profile assembly) see identical pairings.

Strategy:
- Parse the PWA's src/data/thirdPlaceTable.ts at runtime (single source of
  truth for the 495-row FIFA Annex C lookup). Falls back to a sensible empty
  state if the PWA repo isn't reachable.
- Hardcode R32_TEMPLATE + FEEDS here (small + stable; ports cleanly).
- Compute group standings from finished group matches in wc26_stg_matches.
- Resolve R32 home/away per slot type (W / RU / 3rd-placed).
- Cascade R16/QF/SF/3P/Final from finished KO matches.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

import pandas as pd

GROUP_IDS = list("ABCDEFGHIJKL")

# ── R32 template (matches 73-88) — hardcoded from src/data/bracket.ts ───────
# kind="W" group winner; kind="RU" runner-up; kind="3" third-placed from one
# of `allow` groups.
R32_TEMPLATE: list[dict] = [
    {"id": 73, "home": {"kind": "RU", "group": "A"}, "away": {"kind": "RU", "group": "B"}},
    {"id": 74, "home": {"kind": "W",  "group": "E"}, "away": {"kind": "3", "allow": ["A","B","C","D","F"]}},
    {"id": 75, "home": {"kind": "W",  "group": "F"}, "away": {"kind": "RU", "group": "C"}},
    {"id": 76, "home": {"kind": "W",  "group": "C"}, "away": {"kind": "RU", "group": "F"}},
    {"id": 77, "home": {"kind": "W",  "group": "I"}, "away": {"kind": "3", "allow": ["C","D","F","G","H"]}},
    {"id": 78, "home": {"kind": "RU", "group": "E"}, "away": {"kind": "RU", "group": "I"}},
    {"id": 79, "home": {"kind": "W",  "group": "A"}, "away": {"kind": "3", "allow": ["C","E","F","H","I"]}},
    {"id": 80, "home": {"kind": "W",  "group": "L"}, "away": {"kind": "3", "allow": ["E","H","I","J","K"]}},
    {"id": 81, "home": {"kind": "W",  "group": "D"}, "away": {"kind": "3", "allow": ["B","E","F","I","J"]}},
    {"id": 82, "home": {"kind": "W",  "group": "G"}, "away": {"kind": "3", "allow": ["A","E","H","I","J"]}},
    {"id": 83, "home": {"kind": "RU", "group": "K"}, "away": {"kind": "RU", "group": "L"}},
    {"id": 84, "home": {"kind": "W",  "group": "H"}, "away": {"kind": "RU", "group": "J"}},
    {"id": 85, "home": {"kind": "W",  "group": "B"}, "away": {"kind": "3", "allow": ["E","F","G","I","J"]}},
    {"id": 86, "home": {"kind": "W",  "group": "J"}, "away": {"kind": "RU", "group": "H"}},
    {"id": 87, "home": {"kind": "W",  "group": "K"}, "away": {"kind": "3", "allow": ["D","E","I","J","L"]}},
    {"id": 88, "home": {"kind": "RU", "group": "D"}, "away": {"kind": "RU", "group": "G"}},
]

# Maps match_number → which group-winner column in the Annex C table fills
# that slot's 3rd-placed away team. (Only matches with kind="3" home/away.)
THIRD_SLOT_TO_WINNER_COL: dict[int, str] = {}
for m in R32_TEMPLATE:
    if m["away"]["kind"] == "3":
        THIRD_SLOT_TO_WINNER_COL[m["id"]] = m["home"]["group"]

# ── FEEDS — which earlier match feeds each downstream KO slot ───────────────
# Verified against the official FIFA bracket; NOT a naive sequential pairing.
FEEDS: dict[int, dict] = {
    89:  {"home": (74, "winner"), "away": (77, "winner")},
    90:  {"home": (73, "winner"), "away": (75, "winner")},
    91:  {"home": (76, "winner"), "away": (78, "winner")},
    92:  {"home": (79, "winner"), "away": (80, "winner")},
    93:  {"home": (83, "winner"), "away": (84, "winner")},
    94:  {"home": (81, "winner"), "away": (82, "winner")},
    95:  {"home": (86, "winner"), "away": (88, "winner")},
    96:  {"home": (85, "winner"), "away": (87, "winner")},
    97:  {"home": (89, "winner"), "away": (90, "winner")},
    98:  {"home": (93, "winner"), "away": (94, "winner")},
    99:  {"home": (91, "winner"), "away": (92, "winner")},
    100: {"home": (95, "winner"), "away": (96, "winner")},
    101: {"home": (97, "winner"), "away": (98, "winner")},
    102: {"home": (99, "winner"), "away": (100, "winner")},
    103: {"home": (101, "loser"), "away": (102, "loser")},  # 3rd place
    104: {"home": (101, "winner"), "away": (102, "winner")},  # final
}


# ── Annex C — parse from PWA's thirdPlaceTable.ts at runtime ────────────────


def _audit_path() -> Path:
    """Resolve the PWA repo path. AUDIT_APP_PATH env var wins; otherwise
    the local default at E:/fifawc2026."""
    env = os.environ.get("AUDIT_APP_PATH")
    if env:
        return Path(env)
    return Path("E:/fifawc2026")


def load_third_place_table() -> list[dict]:
    """Parse src/data/thirdPlaceTable.ts → list of {qualified, assign} dicts.

    Each row pattern in the TS file:
      { qualified: ["E","F","G","H","I","J","K","L"], assign: {A:"E",B:"J",D:"I",...} },
    """
    pwa = _audit_path()
    ts = pwa / "src" / "data" / "thirdPlaceTable.ts"
    if not ts.exists():
        return []
    text = ts.read_text(encoding="utf-8")
    rows: list[dict] = []
    row_re = re.compile(
        r'\{\s*qualified:\s*\[([^\]]+)\]\s*,\s*assign:\s*\{([^}]+)\}\s*\}'
    )
    for m in row_re.finditer(text):
        qual_raw = m.group(1)
        assign_raw = m.group(2)
        qualified = [s.strip().strip('"') for s in qual_raw.split(",") if s.strip()]
        assign: dict[str, str] = {}
        for kv in assign_raw.split(","):
            kv = kv.strip()
            if not kv or ":" not in kv:
                continue
            k, v = kv.split(":", 1)
            assign[k.strip().strip('"')] = v.strip().strip('"')
        if len(qualified) == 8 and len(assign) == 8:
            rows.append({"qualified": qualified, "assign": assign})
    return rows


# ── Group standings — simplified vs PWA's full FIFA tiebreaker chain ────────
# Order: points DESC -> goal_difference DESC -> goals_for DESC -> fifa_rank ASC
# (FIFA rank ascending = stronger team breaks ties, mirrors the PWA's
# deterministic fallback). H2H + card-deduction tiebreakers are SKIPPED for
# this MVP — they rarely come into play and require extra inputs we'd need
# to plumb in. Add if a real-world R3 close finish bites us.


def _safe_int(v) -> int:
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return 0
        return int(v)
    except (TypeError, ValueError):
        return 0


def compute_group_standings(matches: pd.DataFrame, nations: pd.DataFrame) -> dict:
    """Build per-group standings from finished group matches.

    matches columns required: match_number, stage, home_nation_id, away_nation_id,
                              home_score, away_score, status, group (or stage like "group_a")
    nations columns required: nation_id, group, fifa_rank

    Returns: { group_id: [Standing dict in rank order] }
    """
    # Group lookup from nations table
    nation_to_group: dict[str, str] = {}
    nation_fifa_rank: dict[str, int] = {}
    if not nations.empty:
        for _, r in nations.iterrows():
            nid = r.get("nation_id")
            grp = r.get("group")
            if isinstance(grp, str) and len(grp) == 1:
                nation_to_group[nid] = grp.upper()
            elif isinstance(grp, str) and grp.lower().startswith("group_"):
                nation_to_group[nid] = grp.split("_", 1)[1].upper()
            nation_fifa_rank[nid] = _safe_int(r.get("fifa_rank")) or 999

    # Initialise stats per nation
    stats: dict[str, dict] = {}
    for nid, grp in nation_to_group.items():
        stats[nid] = {
            "nation_id": nid, "group": grp, "played": 0, "won": 0, "drawn": 0,
            "lost": 0, "gf": 0, "ga": 0, "fifa_rank": nation_fifa_rank.get(nid, 999),
        }

    # Only finished group matches contribute
    if matches.empty:
        gm = matches
    else:
        is_group = matches.get("stage", pd.Series([], dtype=str)).fillna("").str.startswith("group", na=False)
        is_done = matches.get("status", pd.Series([], dtype=str)).fillna("").str.lower().isin(
            ["finished", "ft", "full-time", "completed", "final"]
        )
        gm = matches[is_group & is_done]

    for _, m in gm.iterrows():
        h = m.get("home_nation_id"); a = m.get("away_nation_id")
        hs = _safe_int(m.get("home_score")); as_ = _safe_int(m.get("away_score"))
        if not h or not a or h not in stats or a not in stats:
            continue
        stats[h]["played"] += 1; stats[a]["played"] += 1
        stats[h]["gf"] += hs; stats[h]["ga"] += as_
        stats[a]["gf"] += as_; stats[a]["ga"] += hs
        if hs > as_:
            stats[h]["won"] += 1; stats[a]["lost"] += 1
        elif as_ > hs:
            stats[a]["won"] += 1; stats[h]["lost"] += 1
        else:
            stats[h]["drawn"] += 1; stats[a]["drawn"] += 1

    # Group + rank
    by_group: dict[str, list[dict]] = {g: [] for g in GROUP_IDS}
    for nid, s in stats.items():
        if s["group"] in by_group:
            s["pts"] = 3 * s["won"] + s["drawn"]
            s["gd"] = s["gf"] - s["ga"]
            by_group[s["group"]].append(s)
    for g in by_group:
        by_group[g].sort(
            key=lambda r: (-r["pts"], -r["gd"], -r["gf"], r["fifa_rank"])
        )
    return by_group


def rank_third_placed(by_group: dict) -> list[dict]:
    """Rank the 12 third-placed teams across groups; top 8 qualify."""
    thirds = []
    for g, rows in by_group.items():
        if len(rows) >= 3 and rows[0]["played"] == 3:
            t = rows[2]
            thirds.append({**t, "wildcard_rank": None})
    thirds.sort(key=lambda r: (-r["pts"], -r["gd"], -r["gf"], r["fifa_rank"]))
    for i, t in enumerate(thirds):
        t["wildcard_rank"] = i + 1
        t["qualified"] = i < 8
    return thirds


def assign_thirds_via_annex_c(qualified_thirds: list[dict]) -> dict[int, str]:
    """Look up Annex C row matching the 8 qualifying groups; return
    {match_number: third_placed_nation_id} for matches 73-87 with kind="3"
    away slots. Returns {} if no exact match (not all 12 groups finished)."""
    qualified_groups = sorted(t["group"] for t in qualified_thirds if t.get("qualified"))
    if len(qualified_groups) != 8:
        return {}
    table = load_third_place_table()
    if not table:
        return {}
    third_by_group = {t["group"]: t["nation_id"] for t in qualified_thirds if t.get("qualified")}
    for row in table:
        if sorted(row["qualified"]) == qualified_groups:
            # row.assign maps winner_group → source group; we want
            # match_number → nation_id of the 3rd-placed team
            out: dict[int, str] = {}
            for mid, winner_col in THIRD_SLOT_TO_WINNER_COL.items():
                src_group = row["assign"].get(winner_col)
                if src_group and src_group in third_by_group:
                    out[mid] = third_by_group[src_group]
            return out
    return {}


def resolve_r32(by_group: dict, thirds_by_match: dict[int, str]) -> dict[int, dict]:
    """Resolve each R32 match's home + away nation_id.

    Returns: { match_number: {"home_nation_id": str|None, "away_nation_id": str|None} }
    """
    out: dict[int, dict] = {}
    for tpl in R32_TEMPLATE:
        home = _resolve_slot(tpl["home"], by_group, thirds_by_match, tpl["id"])
        away = _resolve_slot(tpl["away"], by_group, thirds_by_match, tpl["id"])
        out[tpl["id"]] = {"home_nation_id": home, "away_nation_id": away}
    return out


def _resolve_slot(slot: dict, by_group: dict, thirds_by_match: dict[int, str],
                  match_id: int) -> str | None:
    if slot["kind"] == "W":
        rows = by_group.get(slot["group"], [])
        if rows and rows[0].get("played") == 3:
            return rows[0]["nation_id"]
        return None
    if slot["kind"] == "RU":
        rows = by_group.get(slot["group"], [])
        if len(rows) >= 2 and rows[1].get("played") == 3:
            return rows[1]["nation_id"]
        return None
    if slot["kind"] == "3":
        return thirds_by_match.get(match_id)
    return None


def derive_bracket(r32_resolved: dict[int, dict],
                   ko_finished_winners: dict[int, str]) -> dict[int, dict]:
    """Cascade R16/QF/SF/3P/Final from feeds. Each downstream match's home/away
    is filled iff the feeder match has a known winner (R32 winners populate
    R16 etc.). Returns {match_number: {home_nation_id, away_nation_id}}."""
    out = dict(r32_resolved)
    for mid in sorted(FEEDS.keys()):
        feed = FEEDS[mid]
        sources = {}
        for side in ("home", "away"):
            src, kind = feed[side]
            src_resolved = out.get(src) or {}
            home_id = src_resolved.get("home_nation_id")
            away_id = src_resolved.get("away_nation_id")
            winner = ko_finished_winners.get(src)
            if kind == "winner":
                sources[side] = winner
            elif kind == "loser":
                if winner and home_id and away_id:
                    sources[side] = away_id if winner == home_id else home_id
                else:
                    sources[side] = None
        out[mid] = {"home_nation_id": sources["home"], "away_nation_id": sources["away"]}
    return out


def derive_all_ko_pairings(matches: pd.DataFrame, nations: pd.DataFrame) -> dict[int, dict]:
    """End-to-end: matches + nations in → {match_number: {home, away}} out
    for every KO match (73-104). Pairs are None when their feeder chain
    isn't resolved yet."""
    by_group = compute_group_standings(matches, nations)
    thirds = rank_third_placed(by_group)
    thirds_by_match = assign_thirds_via_annex_c(thirds)
    r32 = resolve_r32(by_group, thirds_by_match)

    # KO finished winners (matches 73-104 where status finished + scores known)
    ko_winners: dict[int, str] = {}
    if not matches.empty and "status" in matches.columns:
        ko = matches[matches["match_number"].astype("Int64").between(73, 104, inclusive="both")]
        for _, m in ko.iterrows():
            status = str(m.get("status") or "").lower()
            if status not in ("finished", "ft", "full-time", "completed", "final"):
                continue
            h = m.get("home_nation_id"); a = m.get("away_nation_id")
            hs = _safe_int(m.get("home_score")); as_ = _safe_int(m.get("away_score"))
            if not h or not a:
                continue
            mn = int(m["match_number"])
            if hs > as_:
                ko_winners[mn] = h
            elif as_ > hs:
                ko_winners[mn] = a
    return derive_bracket(r32, ko_winners)


def apply_to_stg_matches(matches: pd.DataFrame, derived: dict[int, dict]) -> tuple[pd.DataFrame, int]:
    """Update stg_matches in place — fill home_nation_id / away_nation_id for
    KO rows where they're currently null AND we have a derived pair. Returns
    (updated_df, n_filled)."""
    out = matches.copy()
    if "match_number" not in out.columns:
        return out, 0
    n_filled = 0
    for idx, row in out.iterrows():
        try:
            mn = int(row["match_number"])
        except (TypeError, ValueError):
            continue
        if mn < 73 or mn > 104:
            continue
        d = derived.get(mn) or {}
        for col in ("home_nation_id", "away_nation_id"):
            cur = row.get(col)
            new = d.get(col)
            if (pd.isna(cur) or cur in (None, "")) and new:
                out.at[idx, col] = new
                n_filled += 1
    return out, n_filled
