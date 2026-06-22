"""Resolve the FotMob player_id for FIFA players the algorithmic keyer missed.

Strategy per missing FIFA player:
  1. Build search variants from the FIFA name:
       - flip surname/given (handles KIM Seunggyu → 'Seunggyu Kim')
       - given-only (Salem from AL DAWSARI Salem)
       - surname-only (last token)
  2. Hit apigw.fotmob.com/searchapi/suggest?term=<variant>
  3. Filter results by exact DOB match against FotMob's playerData
  4. Save survivors to data/seeds/player_id_overrides.csv

The override CSV is consumed by _rekey_08.py on the next re-run to lift the
match rate above the 93% the algorithmic key achieves on its own.
"""
import sys, io as sio, re, time, json, unicodedata
sys.stdout = sio.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from pathlib import Path
import pandas as pd
from lib import io
from lib.http import polite_get

ROOT = Path(__file__).resolve().parent
SEEDS = ROOT / "data" / "seeds"
SEEDS.mkdir(parents=True, exist_ok=True)
OVERRIDE_CSV = SEEDS / "player_id_overrides.csv"

SUGGEST = "https://apigw.fotmob.com/searchapi/suggest?term={term}"
HEADERS = {"Referer": "https://www.fotmob.com/", "Origin": "https://www.fotmob.com",
           "Accept": "application/json"}

_STOP = {"jr","jnr","junior","sr","snr","ii","iii","da","de","del","do","dos",
         "van","von","der","den","ten","el","al","le","la","y"}

def _sa(s): return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                            if not unicodedata.combining(c))

def fifa_name_variants(name: str) -> list[str]:
    """Generate plausible search terms from a FIFA-style name like 'KIM Seunggyu'.

    Returns ordered by likelihood, dedup'd.
    """
    if not isinstance(name, str): return []
    src = _sa(name)
    toks = [t for t in re.split(r"\s+", src) if t]
    if not toks: return []
    # Build caps vs non-caps partitions
    caps = [t for t in toks if len(t) >= 2 and t.isupper() and t.lower() not in _STOP]
    non_caps = [t for t in toks if not (len(t) >= 2 and t.isupper()) and t.lower() not in _STOP]
    out: list[str] = []
    # Variant 1: flipped — given names then last caps token (Asian/Arabic)
    if caps and non_caps:
        out.append(" ".join(non_caps + [caps[-1]]))
        if len(caps) > 1:
            out.append(" ".join(non_caps + caps))  # full caps run last
    # Variant 2: full original, lowercased (catches mixed-case sources)
    out.append(" ".join(toks))
    # Variant 3: surname only
    if caps:
        out.append(caps[-1])
    # Variant 4: first non-caps + surname (just two tokens)
    if caps and non_caps:
        out.append(f"{non_caps[0]} {caps[-1]}")
    # Variant 5: drop initials/short words from non_caps, keep last word + surname
    if caps and len(non_caps) > 1:
        out.append(f"{non_caps[-1]} {caps[-1]}")
    # Variant 6: replace dash/dot with space, original last token
    if non_caps:
        out.append(toks[-1])
    seen = set(); dedup = []
    for v in out:
        v2 = re.sub(r"\s+", " ", v).strip()
        if v2 and v2.lower() not in seen:
            seen.add(v2.lower()); dedup.append(v2)
    return dedup

def fotmob_search(term: str) -> list[dict]:
    try:
        r = polite_get(SUGGEST.format(term=term.replace(" ", "%20")),
                       headers=HEADERS, timeout=10)
    except Exception:
        return []
    if not r.ok:
        return []
    try:
        d = r.json()
    except Exception:
        return []
    sec = d.get("squadMemberSuggest") or []
    if not sec:
        return []
    return sec[0].get("options") or []

def get_player_dob(fotmob_player_id: int) -> str | None:
    """Quick playerData fetch to confirm DOB. Uses cache_raw via lib.players."""
    from lib.players import fotmob_player_data
    try:
        d = fotmob_player_data(int(fotmob_player_id))
    except Exception:
        return None
    bd = d.get("birth_date")
    if isinstance(bd, dict):
        bd = bd.get("utcTime")
    if isinstance(bd, str):
        return bd[:10]
    return None

# ─── load enrichment, pick the unmatched
e = io.load_table("wc26_player_enrichment")
unmatched = e[e["fotmob_player_id"].isna()].copy()
unmatched["birth_iso"] = unmatched["birth_date"].astype(str).str.slice(0, 10)
print(f"unmatched FIFA players: {len(unmatched)}")

# Existing overrides (don't re-search if already present)
prev = pd.DataFrame(columns=["fifa_player_id","fotmob_player_id","tm_player_id","matched_via","matched_term"])
if OVERRIDE_CSV.exists():
    try:
        prev = pd.read_csv(OVERRIDE_CSV)
        print(f"existing overrides: {len(prev)}")
    except Exception:
        pass
known = set(prev["fifa_player_id"].astype(int).tolist()) if len(prev) else set()

rows = prev.to_dict("records")
new_resolved = 0
no_match = 0

for r in unmatched.itertuples():
    if int(r.fifa_player_id) in known:
        continue
    variants = fifa_name_variants(r.name)
    pick = None
    pick_term = None
    # DOB strategy:
    #   - strict (full ISO match) if FIFA DOB looks real
    #   - year-only match when FIFA DOB is the YYYY-01-01 placeholder
    fifa_dob = r.birth_iso if isinstance(r.birth_iso, str) else ""
    fifa_year = fifa_dob[:4] if len(fifa_dob) >= 4 else ""
    is_placeholder = fifa_dob.endswith("-01-01") and len(fifa_year) == 4

    for term in variants:
        hits = fotmob_search(term)
        # First pass: strict DOB match
        for opt in hits:
            text = opt.get("text", "")
            try:
                _disp, _id = text.rsplit("|", 1)
                fmid = int(_id)
            except (ValueError, AttributeError):
                continue
            dob_remote = get_player_dob(fmid)
            if not dob_remote:
                continue
            if dob_remote == fifa_dob:
                pick = fmid; pick_term = term; break
            if is_placeholder and dob_remote[:4] == fifa_year:
                pick = fmid; pick_term = f"{term} (year-only)"; break
        if pick:
            break
        # Second pass: same hits, year-only DOB (catches placeholder dates and
        # FIFA-vs-FotMob calendar discrepancies of a few days)
        if not pick and fifa_year:
            for opt in hits:
                text = opt.get("text", "")
                try:
                    _disp, _id = text.rsplit("|", 1)
                    fmid = int(_id)
                except (ValueError, AttributeError):
                    continue
                dob_remote = get_player_dob(fmid)
                if dob_remote and dob_remote[:4] == fifa_year:
                    pick = fmid; pick_term = f"{term} (year-only)"; break
            if pick:
                break
        time.sleep(0.1)
    if pick:
        new_resolved += 1
        print(f"  ✓ {r.nation_id} {r.name!r:32s} -> fotmob_id={pick} via {pick_term!r}")
        rows.append({
            "fifa_player_id": int(r.fifa_player_id),
            "fotmob_player_id": int(pick),
            "tm_player_id": None,
            "matched_via": "fotmob_search",
            "matched_term": pick_term,
        })
    else:
        no_match += 1

df = pd.DataFrame(rows)
df = df.drop_duplicates("fifa_player_id", keep="last")
df.to_csv(OVERRIDE_CSV, index=False)
print(f"\nresolved this run: {new_resolved}")
print(f"still unmatched:  {no_match}")
print(f"override rows total: {len(df)}")
print(f"wrote {OVERRIDE_CSV}")
