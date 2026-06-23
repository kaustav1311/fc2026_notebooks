"""Parse hand-curated seeds out of the sibling audit-metrics app.

We re-read the TS source files at E:/fifawc2026/src/... so any updates flow
through. Parsers are deliberately regex-based and tolerant: they reject lines
that don't match the expected shape rather than blowing up the whole load.
"""
from __future__ import annotations
import os
import re
from pathlib import Path

# Allow override via env var for CI (the GHA workflow checks the PWA repo out
# at $GITHUB_WORKSPACE/pwa-repo and exports AUDIT_APP_PATH to that path).
# Local dev keeps working without setting anything via the default below.
AUDIT_APP = Path(os.environ.get("AUDIT_APP_PATH", "E:/fifawc2026"))
TEAMS_TS = AUDIT_APP / "src" / "data" / "teams.ts"
VENUES_TS = AUDIT_APP / "src" / "data" / "venues.ts"
FIXTURES_TS = AUDIT_APP / "src" / "data" / "fixtures.ts"
ESPN_ABBR_ISO_TS = AUDIT_APP / "src" / "data" / "espnAbbrIso.ts"
ESPN_SERVICE_TS = AUDIT_APP / "src" / "services" / "espn.ts"


class SeedUnavailable(RuntimeError):
    pass


def _read(path: Path) -> str:
    if not path.exists():
        raise SeedUnavailable(
            f"seed file missing: {path} (is the audit app at {AUDIT_APP}?)"
        )
    return path.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# teams.ts → 48 nation seed rows
# ──────────────────────────────────────────────────────────────────────────────

# matches { id: "ARG", name: "Argentina", iso: "ar", group: "J", pot: 1,
#           confederation: "CONMEBOL", fifaRank: 1, valuationM: 620,
#           stars: ["…","…"], isHost: true }
_TEAM_OBJ = re.compile(r"\{\s*id:\s*\"(?P<id>[A-Z]{2,4})\",\s*(?P<rest>[^}]+)\}")
_STR_FIELD = re.compile(r"(\w+):\s*\"([^\"]*)\"")
_NUM_FIELD = re.compile(r"(\w+):\s*(-?\d+\.?\d*)")
_BOOL_FIELD = re.compile(r"(\w+):\s*(true|false)\b")
_STARS_FIELD = re.compile(r"stars:\s*\[([^\]]*)\]")


def parse_teams_ts() -> list[dict]:
    txt = _read(TEAMS_TS)
    rows: list[dict] = []
    for m in _TEAM_OBJ.finditer(txt):
        nation_id = m.group("id")
        rest = m.group("rest")
        row: dict = {"nation_id": nation_id}
        for k, v in _STR_FIELD.findall(rest):
            row[k] = v
        for k, v in _NUM_FIELD.findall(rest):
            row[k] = float(v) if "." in v else int(v)
        for k, v in _BOOL_FIELD.findall(rest):
            row[k] = v == "true"
        stars_m = _STARS_FIELD.search(rest)
        if stars_m:
            row["stars"] = [s.strip().strip("\"") for s in stars_m.group(1).split(",") if s.strip()]
        rows.append(row)
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# venues.ts → 16 stadium seed rows
# ──────────────────────────────────────────────────────────────────────────────

_VENUE_OBJ = re.compile(r"\{\s*id:\s*\"(?P<id>[a-z]+)\",\s*(?P<rest>[^}]+)\}")


def parse_venues_ts() -> list[dict]:
    txt = _read(VENUES_TS)
    rows: list[dict] = []
    # only the VENUES array body — slice from `export const VENUES` to its closing `];`
    start = txt.find("export const VENUES")
    body = txt[start:] if start >= 0 else txt
    end = body.find("];")
    body = body[:end] if end >= 0 else body
    for m in _VENUE_OBJ.finditer(body):
        venue_id = m.group("id")
        rest = m.group("rest")
        row: dict = {"id": venue_id}
        for k, v in _STR_FIELD.findall(rest):
            row[k] = v
        for k, v in _NUM_FIELD.findall(rest):
            row[k] = float(v) if "." in v else int(v)
        rows.append(row)
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# fixtures.ts → 72 group-stage match seeds
# ──────────────────────────────────────────────────────────────────────────────

_FIX_OBJ = re.compile(r"\{\s*id:\s*(?P<id>\d+),\s*(?P<rest>[^}]+)\}")


def parse_fixtures_ts() -> list[dict]:
    txt = _read(FIXTURES_TS)
    start = txt.find("const SCHEDULE")
    body = txt[start:] if start >= 0 else txt
    end = body.find("];")
    body = body[:end] if end >= 0 else body
    rows: list[dict] = []
    for m in _FIX_OBJ.finditer(body):
        rest = m.group("rest")
        row: dict = {"id": int(m.group("id"))}
        for k, v in _STR_FIELD.findall(rest):
            row[k] = v
        rows.append(row)
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# services/espn.ts → ESPN_TEAM_IDS dict
# ──────────────────────────────────────────────────────────────────────────────

_ESPN_TEAM_LINE = re.compile(r"\b([A-Z]{2,4}):\s*(\d+)\b")


def parse_espn_team_ids() -> dict[str, int]:
    txt = _read(ESPN_SERVICE_TS)
    start = txt.find("ESPN_TEAM_IDS")
    end = txt.find("};", start)
    body = txt[start:end] if start >= 0 and end >= 0 else txt
    return {code: int(eid) for code, eid in _ESPN_TEAM_LINE.findall(body)}


# ──────────────────────────────────────────────────────────────────────────────
# data/espnAbbrIso.ts → { ESPN abbr → ISO alpha-2 }
# ──────────────────────────────────────────────────────────────────────────────

_ABBR_LINE = re.compile(r"\b([A-Z][A-Z0-9_]{1,9}):\s*\"([a-z\-]+)\"")


def parse_espn_abbr_iso() -> dict[str, str]:
    txt = _read(ESPN_ABBR_ISO_TS)
    start = txt.find("ESPN_ABBR_TO_ISO")
    end = txt.find("};", start)
    body = txt[start:end] if start >= 0 and end >= 0 else txt
    return {abbr: iso for abbr, iso in _ABBR_LINE.findall(body)}
