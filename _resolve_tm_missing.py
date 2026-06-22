"""Resolve the Transfermarkt player_id for FIFA players the algorithmic keyer
missed.

Strategy per missing FIFA player:
  1. Look up their nation's tm_team_id in wc26_nations.
  2. Walk the already-cached TM team-squad HTML for that nation
     (data/raw/transfermarkt/*team_{id}_squad*.html) — no new HTTP calls.
  3. Match by DOB (FIFA ISO yyyy-mm-dd ↔ TM dd/mm/yyyy). Single match wins.
  4. Save survivors to data/seeds/player_id_overrides.csv (merged with any
     existing FotMob overrides).

Run this when wc26_player_enrichment shows ≥1 row with tm_player_id NaN.
"""
from __future__ import annotations
import sys, io as sio, re
sys.stdout = sio.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from pathlib import Path
import pandas as pd
from lib import io

ROOT = Path(__file__).resolve().parent
SEEDS = ROOT / "data" / "seeds"
SEEDS.mkdir(parents=True, exist_ok=True)
OVERRIDE_CSV = SEEDS / "player_id_overrides.csv"
TM_CACHE = ROOT / "data" / "raw" / "transfermarkt"


def parse_tm_team_squad(tm_team_id: int) -> list[dict]:
    """Pull (tm_player_id, slug, dob_iso) tuples from the latest cached team page."""
    files = sorted(TM_CACHE.glob(f"*team_{tm_team_id}_squad*.html"))
    if not files:
        return []
    html = files[-1].read_text(encoding="utf-8")
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="/([a-z0-9\-]+)/profil/spieler/(\d+)"', html):
        slug, pid = m.group(1), m.group(2)
        if pid in seen:
            continue
        seen.add(pid)
        # TM shows DOB right after the anchor block as dd/mm/yyyy (age)
        ahead = html[m.end(): m.end() + 3000]
        dob_iso = None
        dob_m = re.search(r"(\d{2})/(\d{2})/(\d{4})", ahead)
        if dob_m:
            d, mo, y = dob_m.groups()
            dob_iso = f"{y}-{mo}-{d}"
        out.append({"tm_player_id": int(pid), "tm_slug": slug, "tm_dob": dob_iso})
    return out


def main() -> int:
    enr = io.load_table("wc26_player_enrichment")
    nations = io.load_table("wc26_nations")
    nation_tm = nations.set_index("nation_id")["tm_team_id"].to_dict()

    unmatched = enr[enr["tm_player_id"].isna()].copy()
    unmatched["birth_iso"] = unmatched["birth_date"].astype(str).str.slice(0, 10)
    print(f"FIFA players missing tm_player_id: {len(unmatched)}")

    if OVERRIDE_CSV.exists():
        prev = pd.read_csv(OVERRIDE_CSV)
    else:
        prev = pd.DataFrame(columns=["fifa_player_id", "fotmob_player_id",
                                      "tm_player_id", "matched_via", "matched_term"])

    # Build per-nation TM roster cache.
    rosters: dict[str, list[dict]] = {}
    for nid, tmid in nation_tm.items():
        if pd.isna(tmid):
            continue
        rosters[nid] = parse_tm_team_squad(int(tmid))

    rows = prev.to_dict("records")
    resolved = 0
    ambiguous = 0
    not_in_squad = 0

    for r in unmatched.itertuples():
        nid = r.nation_id
        roster = rosters.get(nid, [])
        if not roster:
            not_in_squad += 1
            continue
        cand = [p for p in roster if p["tm_dob"] and p["tm_dob"] == r.birth_iso]
        if len(cand) == 1:
            pick = cand[0]
            resolved += 1
            print(f"  ✓ {nid} {r.name!r:32s} (dob={r.birth_iso}) -> tm_id={pick['tm_player_id']} slug={pick['tm_slug']}")
            # Merge with any existing override for this fifa_player_id
            existing_idx = None
            for i, x in enumerate(rows):
                if int(x.get("fifa_player_id") or 0) == int(r.fifa_player_id):
                    existing_idx = i; break
            if existing_idx is not None:
                rows[existing_idx]["tm_player_id"] = int(pick["tm_player_id"])
                rows[existing_idx]["matched_via"] = (rows[existing_idx].get("matched_via") or "") + "+tm_dob_in_squad"
                rows[existing_idx]["matched_term"] = (rows[existing_idx].get("matched_term") or "") + f"+{pick['tm_slug']}"
            else:
                rows.append({
                    "fifa_player_id": int(r.fifa_player_id),
                    "fotmob_player_id": None,
                    "tm_player_id": int(pick["tm_player_id"]),
                    "matched_via": "tm_dob_in_squad",
                    "matched_term": pick["tm_slug"],
                })
        elif len(cand) > 1:
            ambiguous += 1
            print(f"  ? {nid} {r.name!r:32s} -> {len(cand)} candidates by DOB: {[p['tm_slug'] for p in cand]}")
        else:
            not_in_squad += 1

    df = pd.DataFrame(rows).drop_duplicates("fifa_player_id", keep="last")
    df.to_csv(OVERRIDE_CSV, index=False)
    print(f"\nresolved this run: {resolved}")
    print(f"ambiguous (multi-DOB):  {ambiguous}")
    print(f"not in TM squad page:   {not_in_squad}")
    print(f"override rows total:    {len(df)}")
    print(f"wrote {OVERRIDE_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
