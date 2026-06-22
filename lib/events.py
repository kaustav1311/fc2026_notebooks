"""Central event-state for the warehouse refresh pipeline.

This file owns `data/.event_state.json`. Every notebook that fetches from a
source consults helpers here to decide what is genuinely stale vs what can be
served from cache. The state file separates:

- `matches` / `rounds`  — last-seen status per key, used to derive what's
  newly-finished since the previous successful tick.
- `processed[source][key]` — once-and-done flags for immutable-after-finish
  payloads (FotMob matchDetails, FDH match_stats for a finished match,
  post-match weather).
- `last_fetch[source][key]` — ISO date of the last successful pull, used by
  tier-2 sources with their own cadence (daily/weekly cap).

The state file is written once at the end of `refresh.py`, after every notebook
in the bundle has succeeded — so a failed tick is idempotent and the next tick
sees the same "newly finished" set.

No HTTP / fetching here; this is pure state.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / ".event_state.json"
SCHEMA_VERSION = 1


def _empty() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_tick_utc": None,
        "matches": {},
        "rounds": {},
        "processed": {},
        "last_fetch": {},
    }


_STATE_CACHE: dict | None = None  # module-level cached state, shared across helpers


def load(reload: bool = False) -> dict:
    """Return current state (cached). Pass reload=True to re-read from disk."""
    global _STATE_CACHE
    if _STATE_CACHE is not None and not reload:
        return _STATE_CACHE
    if not STATE_PATH.exists():
        _STATE_CACHE = _empty()
        return _STATE_CACHE
    try:
        _STATE_CACHE = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        _STATE_CACHE = _empty()
    return _STATE_CACHE


def save(state: dict | None = None) -> None:
    """Persist the cached state (or the dict supplied) atomically."""
    global _STATE_CACHE
    state = state if state is not None else load()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["last_tick_utc"] = datetime.now(tz=timezone.utc).isoformat()
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)
    _STATE_CACHE = state


# ── Event detection ──────────────────────────────────────────────────────────

def _force_all() -> bool:
    """When FORCE_ALL_EVENTS=1, every event helper returns the full set as if
    nothing had been processed. `refresh.py --force-refresh` sets this to do a
    one-shot full sweep without wiping the persisted state."""
    return os.getenv("FORCE_ALL_EVENTS") == "1"


def newly_finished_matches(matches: pd.DataFrame, state: dict | None = None) -> list[str]:
    """fifa_match_ids that are now 'finished' but weren't in the last commit.

    Empty state → every currently-finished match counts (first-run backfill).
    FORCE_ALL_EVENTS=1 → every currently-finished match counts.
    """
    if _force_all():
        return all_finished_matches(matches)
    state = state if state is not None else load()
    prior = state.get("matches", {})
    out = []
    for r in matches.itertuples():
        mid = str(getattr(r, "fifa_match_id", None) or "")
        if not mid:
            continue
        cur = (getattr(r, "status", "") or "").lower()
        if cur != "finished":
            continue
        if prior.get(mid) != "finished":
            out.append(mid)
    return out


def newly_active_rounds(rounds: pd.DataFrame, state: dict | None = None) -> list[int]:
    """round_ids whose status changed since the last commit.

    Returns all currently 'playing' or 'complete' rounds that either weren't in
    the prior state or whose status moved up.
    """
    state = state if state is not None else load()
    prior = state.get("rounds", {})
    order = {"scheduled": 0, "playing": 1, "complete": 2}
    out = []
    for r in rounds.itertuples():
        rid = str(getattr(r, "round_id", ""))
        cur = (getattr(r, "status", "") or "").lower()
        if order.get(cur, 0) <= order.get(prior.get(rid, "scheduled"), 0):
            continue
        try:
            out.append(int(rid))
        except ValueError:
            continue
    return out


def all_finished_matches(matches: pd.DataFrame) -> list[str]:
    """Every currently-finished fifa_match_id (regardless of state)."""
    if "status" not in matches.columns or "fifa_match_id" not in matches.columns:
        return []
    sub = matches[matches["status"].str.lower() == "finished"]
    return [str(m) for m in sub["fifa_match_id"].dropna().tolist()]


# ── Fan-out: derive WHO is affected by each new event ────────────────────────

def players_in_matches(mids: Iterable[str], match_wide: pd.DataFrame) -> set:
    """fifa_player_ids with a row in match_stats_wide for any of mids."""
    if not mids:
        return set()
    s = set(str(m) for m in mids)
    sub = match_wide[match_wide["fifa_match_id"].astype(str).isin(s)]
    return set(sub["fifa_player_id"].dropna().tolist())


def teams_in_matches(mids: Iterable[str], matches: pd.DataFrame) -> set:
    """nation_ids (home + away) involved in mids."""
    if not mids:
        return set()
    s = set(str(m) for m in mids)
    sub = matches[matches["fifa_match_id"].astype(str).isin(s)]
    out = set()
    for col in ("home_nation_id", "away_nation_id"):
        if col in sub.columns:
            out |= set(sub[col].dropna().tolist())
    return out


def refs_in_matches(mids: Iterable[str], matches: pd.DataFrame,
                    ref_bridge: pd.DataFrame | None = None) -> set:
    """referee_ids assigned to mids. Resolves FIFA officialId → slug via bridge."""
    if not mids:
        return set()
    s = set(str(m) for m in mids)
    sub = matches[matches["fifa_match_id"].astype(str).isin(s)]
    fifa_ref_col = "fifa_referee_id"
    if fifa_ref_col not in sub.columns:
        return set()
    fifa_ids = set(sub[fifa_ref_col].dropna().astype(str).tolist())
    if ref_bridge is None or not len(ref_bridge):
        return fifa_ids
    rb = ref_bridge.copy()
    rb["fifa_referee_id"] = rb["fifa_referee_id"].astype(str)
    return set(rb[rb["fifa_referee_id"].isin(fifa_ids)]["referee_id"].dropna().tolist())


# ── Per-(source, key) processed flags ────────────────────────────────────────

def is_processed(source: str, key: str | int, state: dict | None = None) -> bool:
    if _force_all():
        return False
    state = state if state is not None else load()
    return state.get("processed", {}).get(source, {}).get(str(key)) == "done"


def mark_processed(source: str, key: str | int, state: dict | None = None) -> dict:
    """Mark a (source, key) immutable in-memory. Caller commits via save()."""
    state = state if state is not None else load()
    state.setdefault("processed", {}).setdefault(source, {})[str(key)] = "done"
    return state


def last_fetch_date(source: str, key: str | int, state: dict | None = None) -> date | None:
    state = state if state is not None else load()
    raw = state.get("last_fetch", {}).get(source, {}).get(str(key))
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def stamp_fetch(source: str, key: str | int, state: dict | None = None,
                d: date | None = None) -> dict:
    """Stamp 'last successful fetch' for (source, key). Caller commits via save()."""
    state = state if state is not None else load()
    d = d or date.today()
    state.setdefault("last_fetch", {}).setdefault(source, {})[str(key)] = d.isoformat()
    return state


def stale_keys(source: str, candidate_keys: Iterable[str | int],
               max_age_days: int, state: dict | None = None) -> list:
    """Return keys whose last_fetch is missing or older than max_age_days.

    Used by daily/weekly tier-2 sources (TM history, club career) — if the
    key isn't in any event trigger set this tick but its last fetch is older
    than the cadence cap, refetch anyway.
    """
    state = state if state is not None else load()
    today = date.today()
    out = []
    for k in candidate_keys:
        last = last_fetch_date(source, k, state)
        if last is None or (today - last).days >= max_age_days:
            out.append(k)
    return out


# ── Commit hook ──────────────────────────────────────────────────────────────

def commit(matches: pd.DataFrame, rounds: pd.DataFrame | None = None,
           state: dict | None = None) -> None:
    """Persist match/round status maps plus any in-memory state mutations.

    Call ONCE at the end of refresh.py after every notebook has succeeded.
    A failed bundle skips the commit, so the next tick still sees the same
    'newly finished' delta — refresh is idempotent.
    """
    state = state if state is not None else load()
    if "fifa_match_id" in matches.columns and "status" in matches.columns:
        new_map = {}
        for r in matches.itertuples():
            mid = str(getattr(r, "fifa_match_id", "") or "")
            if not mid:
                continue
            new_map[mid] = (getattr(r, "status", "") or "").lower()
        state["matches"] = new_map
    if rounds is not None and "round_id" in rounds.columns and "status" in rounds.columns:
        new_rounds = {}
        for r in rounds.itertuples():
            rid = str(getattr(r, "round_id", ""))
            new_rounds[rid] = (getattr(r, "status", "") or "").lower()
        state["rounds"] = new_rounds
    save(state)


# ── First-run / debugging helpers ────────────────────────────────────────────

def is_first_run() -> bool:
    """True if no state file exists yet — caller may want to do a one-shot full pull."""
    return not STATE_PATH.exists()


def reset() -> None:
    """Delete the state file. Forces a full backfill on next tick."""
    if STATE_PATH.exists():
        STATE_PATH.unlink()


def summary() -> dict:
    """Quick health snapshot — for diagnostic logging."""
    state = load()
    return {
        "schema_version": state.get("schema_version"),
        "last_tick_utc": state.get("last_tick_utc"),
        "matches_tracked": len(state.get("matches", {})),
        "rounds_tracked": len(state.get("rounds", {})),
        "processed_sources": {s: len(v) for s, v in state.get("processed", {}).items()},
        "last_fetch_sources": {s: len(v) for s, v in state.get("last_fetch", {}).items()},
    }
