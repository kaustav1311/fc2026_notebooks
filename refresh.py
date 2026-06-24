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
    "daily":  ["06_players", "04_referees"],

    # 🟥 every 3 hours — the dynamic data.
    "hourly": [
        "03_matches",
        "05_referee_assignments",
        "07_player_match_stats",
        # 08 must run AFTER 07 so wc26_player_match_stats_wide is fresh:
        # nb_08's event-gate reads match_wide to derive EVENT_PIDS (the
        # ~22-30 players who appeared in newly-finished matches). With a
        # stale match_wide the gate returns an empty set and the per-player
        # FotMob playerData refetch never fires — leaving caps/goals/assists
        # in wc26_player_career_national_summary stale for a full cycle.
        # Promoted from daily 2026-06-24 once event-gating made the per-tick
        # cost ~30 HTTP fetches instead of 1248.
        "08_player_enrichment",
        "09_player_season_stats",
        "10_fifa_fantasy",
        "11_fotmob_wc_and_form",
        "12_match_weather",
        "13_polymarket",
        # Staging tables (no network calls — pure pandas over the parquets above).
        # Listed before the dependents-on-staging so every upstream input is
        # guaranteed fresh on the same tick.
        "14_staging_core",
        "15_staging_matches",
        "16_staging_players",
        # 18 — 365scores trends per-fixture snapshot. Depends on
        # wc26_stg_matches + wc26_stg_nations (both built by 14/15), so runs
        # after staging. Appends to a snapshot timeline parquet so a tick's
        # trends are preserved alongside prior snapshots (lets us study how
        # a trend's `percentage` evolved through the days before kickoff).
        "18_scores365_trends",
        # 17 — fantasy recommender. Runs LAST in the hourly bundle: depends on
        # every staging table above + 18's trend snapshot. Emits
        # wc26_fantasy_recommendations.{parquet,json} which the PWA's "K's 2
        # cents" sub-tab renders. Notebook 17 is a .py script (matches
        # 17a_eda_factor_signal.py's pattern), not a .ipynb.
        "17_fantasy_recommender",
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
    """Accept '03' or '03_matches' or '03_matches.ipynb' or '17_fantasy_recommender.py'."""
    stem = needle.removesuffix('.ipynb').removesuffix('.py')
    # Prefer .ipynb, fall back to .py
    candidates = sorted(ROOT.glob(f"{stem}*.ipynb"))
    if candidates:
        return candidates[0]
    candidates = sorted(ROOT.glob(f"{stem}*.py"))
    if candidates:
        return candidates[0]
    for bucket in NOTEBOOK_BUCKETS.values():
        for s in bucket:
            if s.startswith(stem):
                for ext in (".ipynb", ".py"):
                    p = ROOT / f"{s}{ext}"
                    if p.exists():
                        return p
    raise FileNotFoundError(f"no notebook matched {needle!r}")


def run_notebook(
    nb_path: Path,
    *,
    force_refresh: bool = False,
    force_all_events: bool = False,
) -> None:
    import nbformat
    from nbclient import NotebookClient

    flags = []
    if force_refresh:
        flags.append("force_refresh")
    if force_all_events:
        flags.append("force_all_events")
    suffix = f" ({', '.join(flags)})" if flags else ""
    print(f"[run] {nb_path.name}{suffix}")

    # FORCE_REFRESH and FORCE_ALL_EVENTS are intentionally separate.
    #   FORCE_REFRESH=1     — bypass disk cache; re-fetch the URL.
    #   FORCE_ALL_EVENTS=1  — bypass event-driven gating; treat every finished
    #                         match as newly-finished. This is expensive — on
    #                         nb_08 it expands the per-player TM-history sweep
    #                         from ~30 players to ~1200, taking 30+ min.
    # Auto-force-in-tournament-window sets only FORCE_REFRESH (we need fresh
    # data but should still let event-gating trim the per-player work). The
    # user-explicit --force-refresh CLI flag sets both, because that's an
    # intentional full sweep.
    if force_refresh:
        os.environ["FORCE_REFRESH"] = "1"
    else:
        os.environ.pop("FORCE_REFRESH", None)
    if force_all_events:
        os.environ["FORCE_ALL_EVENTS"] = "1"
    else:
        os.environ.pop("FORCE_ALL_EVENTS", None)

    if nb_path.suffix == ".py":
        # Plain Python script — run via subprocess. Cleaner than nbclient for
        # non-interactive notebooks (e.g. 17_fantasy_recommender.py).
        import subprocess
        subprocess.run([sys.executable, str(nb_path)], check=True, cwd=str(ROOT))
        return

    nb = nbformat.read(str(nb_path), as_version=4)
    # 45 min per cell — covers nb_08's TM-history sweep on a cold cache when
    # event-gating legitimately needs to fetch all players (--force-refresh).
    NotebookClient(nb, timeout=2700, kernel_name="python3").execute()
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
    # FORCE_ALL_EVENTS is the "do a full sweep regardless of event state" flag.
    # Auto-force-in-tournament should NOT set this — event-gating is the whole
    # point of the 2026-06-22 refactor (a typical tick fetches ~30 players,
    # not 1200). Only the user-explicit `--force-refresh` CLI flag bypasses
    # event-gating. `--bucket all` is a cold-bootstrap path and also gets the
    # full sweep so daily notebooks populate raw cache for hourly ones.
    effective_force_all_events = args.force_refresh or (args.bucket == "all")
    if effective_force and not args.force_refresh:
        print("[refresh] in tournament window — auto-setting force_refresh=True (event-gating stays on)")
    if effective_force_all_events:
        print("[refresh] force_all_events=True — full sweep, ignoring event state")

    print(f"[refresh] plan: {', '.join(t.name for t in targets)}")
    if args.dry_run:
        return 0

    failures: list[str] = []
    for t in targets:
        try:
            run_notebook(
                t,
                force_refresh=effective_force,
                force_all_events=effective_force_all_events,
            )
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
