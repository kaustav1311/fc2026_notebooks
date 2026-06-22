from __future__ import annotations
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

# pyarrow hotfix — pandas' patch_pyarrow() tries to unregister
# 'arrow.py_extension_type', which on some pyarrow versions isn't registered yet,
# raising ArrowKeyError on the first read_parquet call. Setting _hotfix_installed
# before extension_types loads short-circuits the broken patch. The pyarrow
# import is lazy inside pandas.io.parquet, so doing this here (before any
# read_parquet call) is sufficient even when the notebook imports pandas first.
try:
    import pyarrow as _pyarrow
    _pyarrow._hotfix_installed = True
except ImportError:
    pass

import pandas as pd

from .http import polite_get

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"


def _force_refresh_env() -> bool:
    """Refresh driver opt-in: refresh.py sets FORCE_REFRESH=1 to mean
    'live-window tick, ignore cache'. Honored by both cache_raw and
    latest_raw so we don't silently keep returning day-1 JSON."""
    return os.getenv("FORCE_REFRESH") == "1"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def cache_raw(
    url: str,
    source: str,
    name: str,
    *,
    as_json: bool = True,
    force_refresh: bool | None = None,
    sleep: float = 0.0,
    max_age_days: int | None = None,
) -> Any:
    """GET url, cache the payload under data/raw/{source}/{date}_{name}.{ext}.

    Cache hit policy:
      - force_refresh=True            → always re-fetch
      - force_refresh=False           → always use cache (overrides env)
      - force_refresh=None  (default) → defer to FORCE_REFRESH env var
      - max_age_days=N (N>=0)         → accept cache only if dated within N days
      - max_age_days=None (default)   → accept any cached file (most recent wins)

    The None default lets callers opt OUT of refresh selectively. notebook 10's
    per-player heuristic relies on this: it passes force_refresh=False for cold
    players so the global FORCE_REFRESH=1 (set by refresh.py during the
    tournament window) doesn't re-pull 1488 files every tick.
    """
    ext = "json" if as_json else "html"
    out_dir = RAW / source
    out_dir.mkdir(parents=True, exist_ok=True)
    name_slug = _slug(name)
    today = date.today().isoformat()
    out = out_dir / f"{today}_{name_slug}.{ext}"

    # Resolve None → env value. Explicit True/False from caller wins.
    if force_refresh is None:
        force_refresh = _force_refresh_env()

    if not force_refresh:
        matches = sorted(out_dir.glob(f"*_{name_slug}.{ext}"))
        if matches:
            chosen = matches[-1]
            if max_age_days is None:
                text = chosen.read_text(encoding="utf-8")
                return json.loads(text) if as_json else text
            # Parse the YYYY-MM-DD prefix and enforce max age.
            try:
                stem_date = chosen.name.split("_", 1)[0]
                age_days = (date.today() - date.fromisoformat(stem_date)).days
                if 0 <= age_days <= max_age_days:
                    text = chosen.read_text(encoding="utf-8")
                    return json.loads(text) if as_json else text
            except (ValueError, IndexError):
                pass

    r = polite_get(url, sleep=sleep)
    r.raise_for_status()
    text = r.text
    out.write_text(text, encoding="utf-8")
    return json.loads(text) if as_json else text


def latest_raw(source: str, name_prefix: str, *, as_json: bool = True) -> Any | None:
    """Load the most recent cached raw payload whose filename contains name_prefix.

    Under FORCE_REFRESH=1, return None ONLY when the most recent cache file is
    older than today. Today's cache is honored — this supports the cross-cell
    handoff pattern (cell A writes → cell B reads back via latest_raw) which
    several notebooks rely on. The semantic is exactly 'in the tournament
    window, ignore stale cache; reuse the file we just wrote this tick'."""
    d = RAW / source
    if not d.exists():
        return None
    matches = sorted(d.glob(f"*_{_slug(name_prefix)}.*"))
    if not matches:
        return None
    chosen = matches[-1]
    if _force_refresh_env():
        # Treat anything not stamped with today's YYYY-MM-DD prefix as stale.
        try:
            stem_date = chosen.name.split("_", 1)[0]
            if stem_date != date.today().isoformat():
                return None
        except (ValueError, IndexError):
            return None
    text = chosen.read_text(encoding="utf-8")
    return json.loads(text) if as_json else text


def save_table(df: pd.DataFrame, name: str) -> tuple[Path, Path]:
    """Write df as both Parquet (canonical) and CSV (eyeball)."""
    PROCESSED.mkdir(parents=True, exist_ok=True)
    pq = PROCESSED / f"{name}.parquet"
    csv = PROCESSED / f"{name}.csv"
    df.to_parquet(pq, index=False)
    df.to_csv(csv, index=False)
    print(f"wrote {pq.relative_to(ROOT)} ({len(df)} rows)")
    print(f"wrote {csv.relative_to(ROOT)}")
    return pq, csv


def load_table(name: str) -> pd.DataFrame:
    pq = PROCESSED / f"{name}.parquet"
    if pq.exists():
        return pd.read_parquet(pq)
    csv = PROCESSED / f"{name}.csv"
    return pd.read_csv(csv)
