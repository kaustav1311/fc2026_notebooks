"""Refresh driver.

Run this from the project root to rebuild the warehouse on a schedule.

Usage
-----
    python refresh.py                  # current tick (hourly/daily picked from clock)
    python refresh.py --bucket hourly  # force hourly bundle
    python refresh.py --bucket daily   # force daily bundle (also runs hourly)
    python refresh.py --bucket all     # full rebuild (frozen + daily + hourly)
    python refresh.py --notebook 03    # one notebook only (idx or stem)
    python refresh.py --force-refresh  # ignore today's cached payloads, re-pull

Scheduling
----------
The intended deployment is a 3-hour cron during the tournament window
(2026-06-11 → 2026-07-19). The script picks the right bundle automatically:

    0 */3 * * *  cd /path/to/fc2026_notebooks && python refresh.py

The 07:00-UTC tick of each day additionally rolls the daily bundle. Outside the
tournament window the script exits 0 with no work.

See REFRESH.md for the bucket definitions and per-table triggers.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent

# Notebook stems (no .ipynb suffix).
NOTEBOOK_BUCKETS: dict[str, list[str]] = {
    # 🟦 frozen — rebuild only on schema change. Excluded from any default run.
    "frozen": ["01_nations", "02_stadiums"],

    # 🟨 daily — once per day during the tournament.
    "daily":  ["06_players", "08_player_enrichment", "04_referees"],

    # 🟥 every 3 hours — the dynamic data.
    "hourly": [
        "03_matches",
        "05_referee_assignments",
        "07_player_match_stats",
        "09_player_season_stats",
        "10_fifa_fantasy",
        "11_fotmob_wc_and_form",
        "12_match_weather",
        "13_polymarket",
        # Staging tables (no network calls — pure pandas over the parquets above).
        # Listed last so every upstream input is guaranteed fresh on the same tick.
        "14_staging_core",
        "15_staging_matches",
        "16_staging_players",
    ],
}

TOURNAMENT_START = datetime(2026, 6, 11, tzinfo=timezone.utc)
TOURNAMENT_END   = datetime(2026, 7, 20, tzinfo=timezone.utc)


def in_tournament_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(tz=timezone.utc)
    return TOURNAMENT_START <= now <= TOURNAMENT_END


def pick_bundle(bucket: str | None, now: datetime | None = None) -> list[str]:
    """Return the ordered list of notebook stems to run.

    - bucket=None  → tournament-aware: hourly always; +daily at 07:00 UTC.
    - bucket='hourly' / 'daily' / 'all' → explicit override.
    """
    now = now or datetime.now(tz=timezone.utc)
    if bucket == "all":
        return NOTEBOOK_BUCKETS["frozen"] + NOTEBOOK_BUCKETS["daily"] + NOTEBOOK_BUCKETS["hourly"]
    if bucket == "daily":
        return NOTEBOOK_BUCKETS["daily"] + NOTEBOOK_BUCKETS["hourly"]
    if bucket == "hourly":
        return list(NOTEBOOK_BUCKETS["hourly"])
    if bucket == "frozen":
        return list(NOTEBOOK_BUCKETS["frozen"])

    # Auto pick.
    if not in_tournament_window(now):
        return []
    out = list(NOTEBOOK_BUCKETS["hourly"])
    # Daily bundle: fire on the first tick at-or-after 07:00 UTC each day.
    # Cron runs at hours 0/3/6/9/12/15/18/21 UTC — none equal 7, so a strict
    # hour==7 check would never trigger. Use a marker file so the daily bundle
    # runs exactly once per day, on the earliest tick whose hour >= 7.
    daily_marker = ROOT / "data" / ".last_daily_run"
    if now.hour >= 7:
        last = daily_marker.read_text().strip() if daily_marker.exists() else ""
        if last != now.date().isoformat():
            out = NOTEBOOK_BUCKETS["daily"] + out
            daily_marker.parent.mkdir(parents=True, exist_ok=True)
            daily_marker.write_text(now.date().isoformat())
    return out


def resolve_notebook(needle: str) -> Path:
    """Accept '03' or '03_matches' or '03_matches.ipynb'."""
    candidates = sorted(ROOT.glob(f"{needle.removesuffix('.ipynb')}*.ipynb"))
    if not candidates:
        # try prefix match against bucket lists
        for bucket in NOTEBOOK_BUCKETS.values():
            for stem in bucket:
                if stem.startswith(needle.removesuffix('.ipynb')):
                    p = ROOT / f"{stem}.ipynb"
                    if p.exists():
                        return p
        raise FileNotFoundError(f"no notebook matched {needle!r}")
    return candidates[0]


def run_notebook(nb_path: Path, *, force_refresh: bool = False) -> None:
    import nbformat
    from nbclient import NotebookClient

    print(f"[run] {nb_path.name}{' (force_refresh)' if force_refresh else ''}")
    if force_refresh:
        os.environ["FORCE_REFRESH"] = "1"
        # FORCE_ALL_EVENTS=1 makes lib/events helpers return the full universe
        # so notebooks re-fetch every player/team/ref regardless of state file.
        os.environ["FORCE_ALL_EVENTS"] = "1"

    nb = nbformat.read(str(nb_path), as_version=4)
    NotebookClient(nb, timeout=1800, kernel_name="python3").execute()
    nbformat.write(nb, str(nb_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WC2026 warehouse refresh driver")
    parser.add_argument("--bucket", choices=["hourly", "daily", "all", "frozen"], default=None,
                        help="Override the auto-pick bundle.")
    parser.add_argument("--notebook", "-n", action="append", default=[],
                        help="Run a single notebook (idx or stem). Repeatable.")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Re-fetch ignoring today's cached payloads.")
    parser.add_argument("--no-force-refresh", action="store_true",
                        help="Opt out of the in-tournament auto-force (use cache as-is).")
    parser.add_argument("--dry-run", action="store_true", help="Print plan, don't execute.")
    args = parser.parse_args(argv)

    now = datetime.now(tz=timezone.utc)
    print(f"[refresh] {now.isoformat()} — tournament_window={in_tournament_window(now)}")

    if args.notebook:
        targets = [resolve_notebook(n) for n in args.notebook]
    else:
        stems = pick_bundle(args.bucket, now)
        if not stems:
            print("[refresh] no work for this tick (out of tournament window, no --bucket override)")
            return 0
        targets = [ROOT / f"{s}.ipynb" for s in stems]
        missing = [t for t in targets if not t.exists()]
        if missing:
            print(f"[refresh] missing notebooks: {', '.join(m.name for m in missing)}")
            return 2

    # Inside the tournament window a 3 h tick MEANS "refresh" — the cache
    # policy (max_age_days=None) accepts any cached file, so without forcing
    # we silently keep reading day-1 JSON. Auto-force unless the user opts out
    # by passing --no-force-refresh, or unless we're out of the live window.
    effective_force = args.force_refresh or (in_tournament_window(now) and not args.no_force_refresh)
    if effective_force and not args.force_refresh:
        print("[refresh] in tournament window — auto-setting force_refresh=True")

    print(f"[refresh] plan: {', '.join(t.name for t in targets)}")
    if args.dry_run:
        return 0

    failures: list[str] = []
    for t in targets:
        try:
            run_notebook(t, force_refresh=effective_force)
        except Exception as exc:  # one notebook failing shouldn't kill the bundle
            failures.append(f"{t.name}: {type(exc).__name__}: {exc}")
            print(f"[fail] {t.name}: {exc}")

    if failures:
        print(f"\n[refresh] {len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        # Don't commit event-state on a failed bundle — next tick retries the
        # same 'newly finished' delta. Idempotent.
        return 1

    # Final event-state commit: persist match/round status maps so next tick
    # sees a clean 'newly finished' delta.
    try:
        sys.path.insert(0, str(ROOT))
        from lib import events, io
        try:
            matches = io.load_table("wc26_matches")
        except FileNotFoundError:
            matches = None
        try:
            rounds = io.load_table("fantasy_rounds")
        except FileNotFoundError:
            rounds = None
        if matches is not None:
            events.commit(matches, rounds)
            s = events.summary()
            print(f"[refresh] event-state committed: matches={s['matches_tracked']} rounds={s['rounds_tracked']}")
    except Exception as exc:
        print(f"[refresh] event-state commit failed (non-fatal): {exc}")

    print(f"[refresh] {len(targets)} notebook(s) done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
