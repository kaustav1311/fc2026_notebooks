"""Player enrichment fetchers.

- FotMob team squad → IDs, current transfer value, WC tournament stats.
- FotMob playerData (per player) → market-value history, career history, contract end.
- Transfermarkt squad → IDs, current market value (EUR).

Every fetch routes through `cache_raw` so re-runs hit disk unless force_refresh=True.
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import Iterable

from .io import cache_raw


# ── FotMob ───────────────────────────────────────────────────────────────────

FM_TEAM_URL = "https://www.fotmob.com/api/data/teams?id={team_id}"
FM_PLAYER_URL = "https://www.fotmob.com/api/data/playerData?id={player_id}"


def fotmob_team_squad(team_id: int, force_refresh: bool | None = None) -> list[dict]:
    """Return one dict per player on the team page (incl. coach).

    Captures: id, name, shirtNumber, dateOfBirth, height, age, role, positionIds,
    positionIdsDesc, transferValue (EUR), current club (ccode = clubId, cname),
    and the WC tournament line (rating, goals, assists, ycards, rcards).
    """
    data = cache_raw(
        FM_TEAM_URL.format(team_id=team_id),
        source="fotmob",
        name=f"team_{team_id}_squad",
        force_refresh=force_refresh,
        sleep=0.2,
    )
    out: list[dict] = []
    for group in (data.get("squad") or {}).get("squad", []):
        role_title = group.get("title")  # coach/keepers/defenders/midfielders/attackers
        for m in group.get("members") or []:
            role = (m.get("role") or {})
            out.append({
                "fotmob_player_id": m.get("id"),
                "fotmob_role_title": role_title,
                "fotmob_role_key": role.get("key"),
                "fotmob_name": m.get("name"),
                "shirt_number": m.get("shirtNumber"),
                "date_of_birth": m.get("dateOfBirth"),
                "age": m.get("age"),
                "height_cm": m.get("height"),
                "position_id_primary": m.get("positionId"),
                "position_ids": m.get("positionIds"),
                "position_ids_desc": m.get("positionIdsDesc"),
                "club_fotmob_id": m.get("ccode"),
                "club_name": m.get("cname"),
                "transfer_value_eur_fotmob": m.get("transferValue"),
                "injury": m.get("injury"),
                # WC tournament line on the team page
                "wc_rating": m.get("rating"),
                "wc_goals": m.get("goals"),
                "wc_penalties": m.get("penalties"),
                "wc_assists": m.get("assists"),
                "wc_yellow_cards": m.get("ycards"),
                "wc_red_cards": m.get("rcards"),
            })
    return out


def fotmob_player_data(player_id: int, force_refresh: bool | None = None) -> dict:
    """Rich per-player profile from FotMob.

    Returns a flat dict of selected fields:
      contract_end, preferred_foot, market_value_latest_eur,
      market_value_history (list[dict]), career_senior (list), career_national (list),
      stat_seasons (list), trophies (list), recent_matches (list).
    """
    data = cache_raw(
        FM_PLAYER_URL.format(player_id=player_id),
        source="fotmob",
        name=f"player_{player_id}",
        force_refresh=force_refresh,
        sleep=0.15,
    )

    out: dict = {
        "fotmob_player_id": player_id,
        "name": data.get("name"),
        "birth_date": data.get("birthDate"),
        "is_captain": data.get("isCaptain"),
        "primary_team_id": (data.get("primaryTeam") or {}).get("teamId"),
        "primary_team_name": (data.get("primaryTeam") or {}).get("teamName"),
        "position_description": data.get("positionDescription"),
    }

    contract = data.get("contractEnd") or {}
    out["contract_end"] = contract.get("utcTime")

    # playerInformation is a list of typed cards (Height, Shirt, Age, Preferred foot, etc.)
    for card in (data.get("playerInformation") or []):
        title = (card.get("title") or "").lower().strip()
        v = card.get("value") or {}
        val = v.get("fallback") if v.get("fallback") is not None else v.get("numberValue")
        if title in ("preferred foot",):
            out["preferred_foot"] = val
        elif title == "height":
            out.setdefault("height_cm", v.get("numberValue"))

    mv = (data.get("marketValues") or {}).get("values") or []
    if mv:
        latest = mv[-1]
        out["market_value_latest_eur"] = latest.get("value")
        out["market_value_lower_eur"] = latest.get("lowerBound")
        out["market_value_upper_eur"] = latest.get("upperBound")
        out["market_value_source"] = latest.get("source")
        out["market_value_team_id"] = latest.get("teamId")
        out["market_value_team_name"] = latest.get("teamName")
        out["market_value_history"] = mv  # full list — caller can normalize to long format

    ch = (data.get("careerHistory") or {}).get("careerItems") or {}
    out["career_senior"] = ch.get("senior")
    out["career_national"] = ch.get("national team")

    out["stat_seasons"] = data.get("statSeasons")
    out["trophies"] = data.get("trophies")
    out["recent_matches"] = data.get("recentMatches")
    out["traits"] = data.get("traits")
    out["status"] = data.get("status")

    return out


# ── Transfermarkt ────────────────────────────────────────────────────────────

TM_TEAM_URL = "https://www.transfermarkt.com/{slug}/startseite/verein/{team_id}"

_TM_VALUE_RE = re.compile(r"€(\d+(?:\.\d+)?)\s*([kKmMbB])?")


def _tm_value_to_eur(s: str) -> int | None:
    """€100.00m → 100_000_000, €750k → 750_000, €5.00bn → 5_000_000_000."""
    if not isinstance(s, str):
        return None
    m = _TM_VALUE_RE.search(s)
    if not m:
        return None
    n = float(m.group(1))
    unit = (m.group(2) or "").lower()
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(unit, 1)
    return int(round(n * mult))


TM_VALUE_HISTORY_URL = "https://www.transfermarkt.com/ceapi/marketValueDevelopment/graph/{tm_id}"


def _parse_tm_value_string(s: str | None) -> int | None:
    """'€6.50m' → 6_500_000, '€300k' → 300_000. None for empty/'-'."""
    if not isinstance(s, str) or s.strip() in ("", "-"):
        return None
    return _tm_value_to_eur(s)


def _parse_tm_date_string(s: str | None) -> str | None:
    """TM uses DD/MM/YYYY in `datum_mw` and `highest_date`. Convert to ISO."""
    if not isinstance(s, str) or "/" not in s:
        return None
    try:
        return datetime.strptime(s, "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def transfermarkt_value_history(tm_player_id: int, force_refresh: bool | None = None) -> dict:
    """Fetch Transfermarkt's market value history for one player.

    TM rate-limits aggressively (~600 reqs/run). Sleep is 1.0s base; on
    failure the caller should pause and retry the missing players. Cache
    hits don't sleep, so re-runs only pay the sleep cost for new players.

    Returns dict:
      - history: list of {date, value_eur, club_name, age}
      - current_eur: latest TM-listed value
      - peak_eur, peak_date: TM-reported highest value + ISO date
    """
    data = cache_raw(
        TM_VALUE_HISTORY_URL.format(tm_id=tm_player_id),
        source="transfermarkt",
        name=f"value_history_{tm_player_id}",
        force_refresh=force_refresh,
        sleep=1.0,
    )
    history: list[dict] = []
    for pt in (data.get("list") or []):
        ts_ms = pt.get("x")
        try:
            d = datetime.utcfromtimestamp(int(ts_ms) / 1000).date().isoformat() if ts_ms else None
        except (TypeError, ValueError):
            d = _parse_tm_date_string(pt.get("datum_mw"))
        v = pt.get("y")
        history.append({
            "date": d,
            "value_eur": int(v) if isinstance(v, (int, float)) else None,
            "club_name": pt.get("verein"),
            "age": pt.get("age"),
        })
    return {
        "history": history,
        "current_eur": _parse_tm_value_string(data.get("current")),
        "peak_eur": _parse_tm_value_string(data.get("highest")),
        "peak_date": _parse_tm_date_string(data.get("highest_date")),
    }


def transfermarkt_squad(team_id: int, slug: str = "team", force_refresh: bool | None = None) -> list[dict]:
    """Parse TM team squad page for player IDs, name, DOB, club, market value.

    `slug` is decorative for TM's URL routing; using the right slug avoids the
    404 some clubs return for the generic ``-`` placeholder."""
    html = cache_raw(
        TM_TEAM_URL.format(slug=slug, team_id=team_id),
        source="transfermarkt",
        name=f"team_{team_id}_squad",
        as_json=False,
        force_refresh=force_refresh,
        sleep=0.4,
    )
    out: list[dict] = []
    # Each player anchor leads /{slug}/profil/spieler/{id}; we then look ahead
    # ~3 KB for the row's DOB + market-value cells.
    for m in re.finditer(r'href="/([a-z0-9\-]+)/profil/spieler/(\d+)"', html):
        slug, pid = m.group(1), int(m.group(2))
        ahead = html[m.end(): m.end() + 3500]
        # DOB cell — TM uses dd/mm/yyyy (age)
        dob_m = re.search(r"(\d{2}/\d{2}/\d{4})\s*\((\d+)\)", ahead)
        dob = None
        if dob_m:
            try:
                dob = datetime.strptime(dob_m.group(1), "%d/%m/%Y").date().isoformat()
            except ValueError:
                pass
        # Club anchor
        club_m = re.search(r'<a title="([^"]+)" href="/[^"]+/startseite/verein/(\d+)"', ahead)
        club_name = club_m.group(1) if club_m else None
        club_tm_id = int(club_m.group(2)) if club_m else None
        # Market value (€...m)
        mv_m = re.search(r'<a[^>]*href="/[^"]+/marktwertverlauf/spieler/\d+">€[^<]*</a>', ahead)
        mv_eur = _tm_value_to_eur(mv_m.group(0)) if mv_m else None
        if any(d.get("tm_player_id") == pid for d in out):
            continue
        out.append({
            "tm_player_id": pid,
            "tm_slug": slug,
            "tm_dob": dob,
            "club_tm_id": club_tm_id,
            "club_name_tm": club_name,
            "market_value_eur_tm": mv_eur,
        })
    return out
