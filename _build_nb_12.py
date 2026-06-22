"""Author 12_match_weather.ipynb."""
import json, uuid
from pathlib import Path

CELLS = []
def code(src):
    CELLS.append({"cell_type":"code","execution_count":None,"id":uuid.uuid4().hex[:8],
                  "metadata":{},"outputs":[],"source":[s+"\n" for s in src.rstrip("\n").split("\n")]})
def md(src):
    CELLS.append({"cell_type":"markdown","id":uuid.uuid4().hex[:8],"metadata":{},
                  "source":[s+"\n" for s in src.rstrip("\n").split("\n")]})

md("""# 12 — Per-match weather

`sumary_1.txt §5` flagged weather (WBGT / heat / humidity / wind / rain) as a fantasy signal — fatigue, substitution rates, late goals, keeper mistakes. Open-Meteo's CORS-free `/v1/archive` (post-match) and `/v1/forecast` (pre-match) cover every WC26 venue.

Strategy per match
- Look up `(latitude, longitude, timezone)` from `wc26_stadiums` via `stadium_id`.
- Convert `kickoff_utc` to the venue-local hour.
- Pick endpoint by horizon: archive for past matches (rich, observed), forecast for upcoming.
- Pull `temperature_2m, relative_humidity_2m, dew_point_2m, apparent_temperature, precipitation, wind_speed_10m, wind_direction_10m, cloud_cover, weather_code` for the matching hour.
- Derive WBGT proxy: `WBGT ≈ 0.7·Tw + 0.2·Tg + 0.1·T` (we use `apparent_temperature` as a Tg surrogate when globe temp isn't published; Tw from dew-point).

Output: `wc26_match_weather` — one row per match (104), pre-tournament rows have `source='forecast'`, post-match rows have `source='archive'`.

Re-fetch is idempotent — forecast rows get overwritten by archive once the match completes, captured by the `source` column.""")

code("""import sys, math
from datetime import timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import pandas as pd

ROOT = Path.cwd()
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))
elif (ROOT.parent / "lib").is_dir():
    sys.path.insert(0, str(ROOT.parent))

from lib import io

matches = io.load_table("wc26_matches")
stadiums = io.load_table("wc26_stadiums").set_index("stadium_id")
print(f"matches: {len(matches)}  stadiums: {len(stadiums)}")

now_utc = pd.Timestamp.now(tz="UTC")
print(f"now: {now_utc}")
""")

md("## 1. Build the (match, stadium, venue-local hour) plan")

code("""def venue_local_row(m):
    sid = m["stadium_id"]
    if pd.isna(sid) or sid not in stadiums.index:
        return None
    s = stadiums.loc[sid]
    ku = m["kickoff_utc"]
    if pd.isna(ku):
        return None
    ku = pd.Timestamp(ku).tz_convert("UTC")
    local = ku.tz_convert(ZoneInfo(s["timezone"]))
    return {
        "match_number": m["match_number"],
        "espn_match_id": m["espn_match_id"],
        "fifa_match_id": m.get("fifa_match_id"),
        "kickoff_utc": ku,
        "kickoff_local": local,
        "local_date": local.strftime("%Y-%m-%d"),
        "local_hour": int(local.strftime("%H")),
        "stadium_id": sid,
        "lat": round(float(s["latitude"]), 4),
        "lng": round(float(s["longitude"]), 4),
        "timezone": s["timezone"],
    }

plan_rows = [r for r in (venue_local_row(m) for _, m in matches.iterrows()) if r]
plan = pd.DataFrame(plan_rows)
plan["horizon_kind"] = (plan["kickoff_utc"] < now_utc).map({True: "archive", False: "forecast"})
print(plan["horizon_kind"].value_counts().to_dict())
plan.head()
""")

md("""## 2. Fetch hourly weather per (stadium, local_date)

Open-Meteo returns the full hourly array for a date; we cache by `(lat, lng, date, kind)` so multiple matches sharing a venue+date reuse one HTTP call. Picking the row at `local_hour` happens at parse time.""")

code("""HOURLY_FIELDS = ",".join([
    "temperature_2m", "relative_humidity_2m", "dew_point_2m",
    "apparent_temperature", "precipitation", "rain", "wind_speed_10m",
    "wind_direction_10m", "cloud_cover", "weather_code",
])

def fetch_hourly(lat, lng, date_iso, kind):
    if kind == "archive":
        url = (f"https://archive-api.open-meteo.com/v1/archive"
               f"?latitude={lat}&longitude={lng}"
               f"&start_date={date_iso}&end_date={date_iso}"
               f"&hourly={HOURLY_FIELDS}&timezone=auto")
    else:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lng}"
               f"&start_date={date_iso}&end_date={date_iso}"
               f"&hourly={HOURLY_FIELDS}&timezone=auto")
    return io.cache_raw(url, source="open_meteo",
                        name=f"{kind}_{lat}_{lng}_{date_iso}",
                        sleep=0.15)

def kelvin_dew_to_wet_bulb(t_c, rh_pct):
    # Stull (2011) approximation for wet-bulb temperature from T (°C) and RH (%)
    if t_c is None or rh_pct is None:
        return None
    try:
        rh = float(rh_pct); t = float(t_c)
    except (TypeError, ValueError):
        return None
    return (t * math.atan(0.151977 * (rh + 8.313659) ** 0.5)
            + math.atan(t + rh) - math.atan(rh - 1.676331)
            + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh) - 4.686035)

out_rows = []
errors = 0
for r in plan.itertuples():
    try:
        data = fetch_hourly(r.lat, r.lng, r.local_date, r.horizon_kind)
    except Exception as e:
        errors += 1
        continue
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    target = f"{r.local_date}T{r.local_hour:02d}:00"
    try:
        idx = times.index(target)
    except ValueError:
        idx = None
    def at(k):
        v = hourly.get(k) or []
        return v[idx] if (idx is not None and idx < len(v)) else None

    t = at("temperature_2m"); rh = at("relative_humidity_2m")
    twb = kelvin_dew_to_wet_bulb(t, rh)
    appar = at("apparent_temperature")
    # WBGT proxy (no in-sun globe temp available): 0.7 Tw + 0.2 Tappar + 0.1 T
    wbgt = None
    if t is not None and twb is not None and appar is not None:
        wbgt = 0.7 * twb + 0.2 * appar + 0.1 * t

    out_rows.append({
        "espn_match_id": r.espn_match_id,
        "fifa_match_id": r.fifa_match_id,
        "match_number": r.match_number,
        "stadium_id": r.stadium_id,
        "kickoff_utc": r.kickoff_utc,
        "local_date": r.local_date,
        "local_hour": r.local_hour,
        "source": r.horizon_kind,
        "temperature_c": t,
        "apparent_temperature_c": appar,
        "humidity_pct": rh,
        "dew_point_c": at("dew_point_2m"),
        "precipitation_mm": at("precipitation"),
        "rain_mm": at("rain"),
        "wind_speed_kmh": at("wind_speed_10m"),
        "wind_direction_deg": at("wind_direction_10m"),
        "cloud_cover_pct": at("cloud_cover"),
        "wmo_weather_code": at("weather_code"),
        "wet_bulb_temp_c": round(twb, 2) if twb is not None else None,
        "wbgt_proxy_c": round(wbgt, 2) if wbgt is not None else None,
    })

print(f"weather rows: {len(out_rows)}  errors: {errors}")
""")

md("## 3. Save")

code("""weather = pd.DataFrame(out_rows)
print(f"rows: {len(weather)}")
print(f"\\nWBGT proxy distribution:")
print(weather["wbgt_proxy_c"].describe().round(1).to_string())
print(f"\\nsource split:")
print(weather["source"].value_counts().to_string())

io.save_table(weather, "wc26_match_weather")
""")

nb = {"cells": CELLS, "metadata": {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.x"}}, "nbformat": 4, "nbformat_minor": 5}
Path("12_match_weather.ipynb").write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote 12_match_weather.ipynb")
