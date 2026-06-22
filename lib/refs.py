"""Referee fetchers.

Architecture: each source exposes a small surface — `discover_*` returns the
panel list, `fetch_*` returns per-ref data. Notebook 04 orchestrates: discovery
once, profile fetch per ref, normalise to the shared schema, save.

Sources:
- FootyMetrics  → /world-cup-2026/referees for discovery + /referees/{id}-{slug} for per-ref stats.
- WorldReferee  → search for slug, scrape career page for additional career stats.
- worldreferee.com/upcoming → panel change announcements (re-run to refresh).
"""
from __future__ import annotations
import json
import re
from typing import Iterable

from .io import cache_raw, latest_raw, RAW

FM_WC26_URL = "https://footymetrics.com/world-cup-2026/referees"
FM_REF_URL = "https://footymetrics.com/referees/{fm_id}-{slug}"
WR_UPCOMING_URL = "https://worldreferee.com/upcoming"


# ── helpers ──────────────────────────────────────────────────────────────────

def _unesc(s: str) -> str:
    """Decode a JS string literal (escapes like \\n, \\", \\uXXXX) to text."""
    try:
        return json.loads('"' + s + '"')
    except Exception:
        try:
            return s.encode("utf-8", errors="ignore").decode("unicode_escape", errors="ignore")
        except Exception:
            return s


def _next_chunks(html: str) -> str:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)', html, re.S)
    return "".join(_unesc(c) for c in chunks)


def _slug_to_name(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


# ── FootyMetrics ─────────────────────────────────────────────────────────────

def fm_discover_wc26(force_refresh: bool | None = None) -> list[dict]:
    """List the 50 WC26 referees according to FootyMetrics.
    Returns dicts: {fm_id, slug, name, country, flag_iso}.
    """
    html = cache_raw(FM_WC26_URL, source="footymetrics", name="wc26_referees",
                     as_json=False, force_refresh=force_refresh)
    combined = _next_chunks(html)

    # The page embeds a clean JSON record per ref:
    #   {"id":"N","slug":"N-slug","name":"...","country":{"name":"X","flag":"...|null"},"conf":"UEFA",...}
    refs: list[dict] = []
    seen_ids: set[str] = set()
    for m in re.finditer(
        r'"id":"(?P<fm_id>\d+)","slug":"\d+-(?P<slug>[a-z0-9\-]+)",'
        r'"name":"(?P<name>[^"]+)",'
        r'"country":\{"name":"(?P<country>[^"]*)","flag":(?P<flag>null|"[^"]+")\},'
        r'"conf":"(?P<conf>[^"]*)"',
        combined,
    ):
        fm_id = m.group("fm_id")
        if fm_id in seen_ids:
            continue
        seen_ids.add(fm_id)
        flag_val = m.group("flag")
        iso = None
        if flag_val.startswith('"'):
            f = flag_val.strip('"')
            mm = re.search(r"_([A-Za-z]+)\.webp$", f)
            if mm:
                iso = mm.group(1).lower()
        refs.append({
            "fm_id": int(fm_id),
            "slug": m.group("slug"),
            "name": m.group("name").strip(),
            "country": m.group("country").strip() or None,
            "confederation": m.group("conf") or None,
            "flag_iso": iso,
        })

    # Fall-back: any href the structured pattern missed (layout differs).
    for fm_id, slug in re.findall(r'/referees/(\d+)-([a-z0-9\-]+)', html):
        if fm_id in seen_ids:
            continue
        seen_ids.add(fm_id)
        refs.append({
            "fm_id": int(fm_id), "slug": slug,
            "name": _slug_to_name(slug),
            "country": None, "confederation": None, "flag_iso": None,
        })
    return refs


# Stat keys we extract from per-ref FM profile pages.
FM_PROFILE_FIELDS_INT = (
    "totalFixtures", "totalYellows", "totalReds", "totalPenalties", "totalFouls",
    "fixturesWithRed", "fixturesWith2PlusReds", "fixturesWithPenalty",
    "fixturesWith2PlusPens", "fixturesNoCards",
)
FM_PROFILE_FIELDS_FLOAT = (
    "yellowsPerFixture", "redsPerFixture", "penaltyRate", "redCardRate",
    "foulsPerFixture", "cardsPerFixture", "foulsPerCard", "cardsPerFoulRatio",
    "homeCardBias", "bookingPointsPerFixture",
)


def _scan_field(text: str, key: str):
    # Match `"key":"value"` or `"key":number` — return first match.
    m = re.search(r'"' + re.escape(key) + r'":"([^"]*)"', text)
    if m:
        return m.group(1)
    m = re.search(r'"' + re.escape(key) + r'":(-?\d+\.?\d*)', text)
    if m:
        return m.group(1)
    return None


# FootyMetrics' actual JSON keys (discovered by inspecting the streamed payload).
# We avoid the home*/away* split totals (multiple stat objects in the stream
# would race the first-match regex); derive totals from avg × fixtures instead.
FM_PROFILE_INT_KEYS = (
    "fixtures",
    "fixturesWithRed", "fixturesWith2PlusReds",
    "fixturesWithPenalty", "fixturesWith2PlusPens",
    "fixturesNoCards",
    "countryApid",
)
FM_PROFILE_FLOAT_KEYS = (
    "avgFouls", "avgYellowCards", "avgRedCards", "avgPenalties",
    "avgBookingPoints", "avgAddedFH", "avgAddedSH",
    "avgHomeYellowCards", "avgAwayYellowCards",
    "avgHomeRedCards", "avgAwayRedCards",
    "penaltyRate", "redCardRate",
    "foulsPerCard", "cardsPerFoulRatio", "homeCardBias",
)


def fm_recent_fixtures(fm_id: int, slug: str, force_refresh: bool | None = None) -> list[dict]:
    """Parse the cached FM profile page for the recentMatches[] block.

    Returns one dict per fixture (newest first):
      timestamp (ISO), league, league_apid, season_short,
      home_name, away_name, home_goals, away_goals,
      yellow_cards_total, red_cards_total, penalties_total, fouls_total.
    """
    url = FM_REF_URL.format(fm_id=fm_id, slug=slug)
    html = cache_raw(url, source="footymetrics", name=f"ref_{fm_id}",
                     as_json=False, force_refresh=force_refresh, sleep=0.3)
    body = _next_chunks(html)
    idx = body.find('recentMatches')
    if idx < 0:
        return []
    block = body[idx:]

    rows: list[dict] = []
    # Each fixture is a self-contained `{"id":"…","apid":…,…"AwayStats":{…}}` object.
    # We rely on the AwayStats closing brace as the terminator.
    pattern = re.compile(
        r'\{"id":"(?P<id>\d+)","apid":(?P<apid>\d+),"timestamp":"\$D(?P<ts>[^"]+)",'
        r'"slug":"(?P<slug>[^"]+)","homeGoals":(?P<hg>-?\d+),"awayGoals":(?P<ag>-?\d+),'
        r'.*?"HomeStats":\{(?P<hs>[^}]*)\},"AwayStats":\{(?P<as>[^}]*)\}\}',
        re.S,
    )

    def _kv(blob: str) -> dict:
        out = {}
        for m in re.finditer(r'"([a-zA-Z]+)":(-?\d+(?:\.\d+)?)', blob):
            try:
                out[m.group(1)] = float(m.group(2)) if "." in m.group(2) else int(m.group(2))
            except ValueError:
                pass
        return out

    for m in pattern.finditer(block):
        hs = _kv(m.group("hs"))
        ax = _kv(m.group("as"))
        yc = (hs.get("yellowCards", 0) or 0) + (ax.get("yellowCards", 0) or 0)
        rc = (hs.get("redCards", 0) or 0) + (ax.get("redCards", 0) or 0)
        pn = (hs.get("penalties", 0) or 0) + (ax.get("penalties", 0) or 0)
        fl = (hs.get("foulsc", 0) or 0) + (ax.get("foulsc", 0) or 0)
        rows.append({
            "fixture_id": int(m.group("id")),
            "timestamp": m.group("ts"),
            "slug": m.group("slug"),
            "home_goals": int(m.group("hg")),
            "away_goals": int(m.group("ag")),
            "yellow_cards_total": yc,
            "red_cards_total": rc,
            "penalties_total": pn,
            "fouls_total": fl,
        })
    # newest first
    rows.sort(key=lambda r: r["timestamp"], reverse=True)
    return rows


def fm_fetch_profile(fm_id: int, slug: str, force_refresh: bool | None = None) -> dict:
    url = FM_REF_URL.format(fm_id=fm_id, slug=slug)
    html = cache_raw(url, source="footymetrics", name=f"ref_{fm_id}",
                     as_json=False, force_refresh=force_refresh, sleep=0.3)
    body = _next_chunks(html)

    out: dict = {"fm_id": fm_id, "slug": slug, "source_url": url}
    for k in FM_PROFILE_INT_KEYS:
        v = _scan_field(body, k)
        if v is not None:
            try:
                out[k] = int(float(v))
            except (TypeError, ValueError):
                out[k] = None
    for k in FM_PROFILE_FLOAT_KEYS:
        v = _scan_field(body, k)
        if v is not None:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                out[k] = None

    # Country isn't reliably parseable from the profile page (league-badge flags
    # dominate). The listing page (fm_discover_wc26) already supplies it.

    # Derive totals from per-fixture averages × games (avoids the
    # multiple-stat-objects-in-stream pitfall of grabbing home*/away* fields).
    n = out.get("fixtures")
    if isinstance(n, int):
        for src, dst in (
            ("avgYellowCards", "totalYellows"),
            ("avgRedCards", "totalReds"),
            ("avgPenalties", "totalPenalties"),
            ("avgFouls", "totalFouls"),
        ):
            if isinstance(out.get(src), (int, float)):
                out[dst] = round(out[src] * n)
    return out


# ── WorldReferee ─────────────────────────────────────────────────────────────

def _slugify_for_wr(name: str) -> str:
    return re.sub(r"[^a-z0-9\-]+", "-", name.lower().replace(" ", "-")).strip("-")


def wr_career_url(name: str, ref_uid: str | None = None) -> str:
    slug = _slugify_for_wr(name)
    if ref_uid:
        return f"https://www.worldreferee.com/referee/{slug}/{ref_uid}/career"
    return f"https://www.worldreferee.com/referee/{slug}/career"


def wr_fetch_career(name: str, ref_uid: str | None = None, force_refresh: bool | None = None) -> dict | None:
    """Best-effort: try a couple of URL forms; return None if not found."""
    candidates = []
    if ref_uid:
        candidates.append(wr_career_url(name, ref_uid))
    candidates.append(wr_career_url(name))
    for url in candidates:
        try:
            html = cache_raw(url, source="worldreferee", name=f"career_{_slugify_for_wr(name)}",
                             as_json=False, force_refresh=force_refresh, sleep=0.5)
        except Exception:
            continue
        # Look for clear career-stats markers
        if "Career Statistics" not in html and "Matches Officiated" not in html:
            continue
        out: dict = {"source_url": url}
        # Pull a handful of headline numbers via labelled rows.
        for label, key in (
            ("Matches Officiated", "wr_matches"),
            ("Yellow Cards", "wr_yellows"),
            ("Red Cards", "wr_reds"),
            ("Penalties", "wr_penalties"),
            ("Fouls", "wr_fouls"),
        ):
            m = re.search(re.escape(label) + r'\s*</[a-z]+>\s*<[a-z][^>]*>\s*([\d,]+)', html)
            if m:
                out[key] = int(m.group(1).replace(",", ""))
        return out
    return None


def wr_upcoming_changes(force_refresh: bool | None = None) -> str:
    """Fetch worldreferee.com/upcoming for any announced panel changes.
    Returned verbatim HTML — Notebook 04 logs the headline lines for the user
    to skim manually. Cache the page so we can diff over time.
    """
    return cache_raw(WR_UPCOMING_URL, source="worldreferee", name="upcoming",
                     as_json=False, force_refresh=force_refresh)
