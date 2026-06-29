"""Bar-chart-race GIF of the Polymarket world-cup-winner market.

Runs each hourly tick AFTER the winner-history snapshot step. Reads the
long-format `wc26_polymarket_winner_history.parquet`, pivots it to a
nation x date matrix of `yes_price` (implied probability), and renders
an animated bar-chart race with nation flags drawn AS each bar's
identifier on the left, country code beside, and live percentage at
the right tip.

Output:
    data/processed/static/wc26_polymarket_winner_race.gif

The companion emit step copies that file alongside the PWA JSON. The
PWA's WinnerRaceChart HEADs the URL on mount and renders it as an
<img> when present.

Design choices
--------------
* matplotlib + Pillow only — no `bar_chart_race`/`pynimate` dependency.
  Neither library supports image-as-bar-label, which is the visual the
  product asked for. Direct matplotlib gives full control.
* Linear interpolation between daily snapshots produces a smooth race
  (the raw parquet is daily; we expand to ~15 frames per period). The
  rank can re-order mid-period because we interpolate continuously,
  which is what a viewer expects from "a race".
* Flags fetched once from flagcdn.com (60px PNGs, ~1-2 KB) and cached
  under data/raw/flags/. Re-runs are fully offline-fast.
* Schema match — the on-disk parquet has columns
  (nation_id, polymarket_market_id, market_slug, question, date_utc,
   yes_price). NOT (snapshot_ts, team_name, pct) which an earlier draft
  of this script assumed.
* Failure-tolerant: any rendering error logs + exits 0 so the refresh
  pipeline isn't blocked. The PWA falls back to its SVG line chart and
  ultimately to the live-snapshot placeholder when the GIF is missing.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
if (ROOT / "lib").is_dir():
    sys.path.insert(0, str(ROOT))

from lib import io  # noqa: E402
from lib.http import polite_get  # noqa: E402

HISTORY_PARQUET = ROOT / "data" / "processed" / "wc26_polymarket_winner_history.parquet"
STATIC_DIR      = ROOT / "data" / "processed" / "static"
GIF_PATH        = STATIC_DIR / "wc26_polymarket_winner_race.gif"
FLAG_CACHE      = ROOT / "data" / "raw" / "flags"

TOP_N             = 10
STEPS_PER_PERIOD  = 6      # interpolation density between adjacent snapshots
FPS               = 14
HOLD_FRAMES       = 24     # freeze on the last frame so the finish reads
WIDTH_PX          = 620
HEIGHT_PX         = 420
DPI               = 85
MIN_SNAPSHOTS     = 3
# Hard cap on raw snapshots fed to the interpolator. With one tick per
# day the parquet keeps growing through the tournament — past 35 daily
# rows the bar movement per frame is too small for the eye to track and
# the GIF balloons in size for no extra signal. We always include the
# very first snapshot so the opening odds anchor the race.
MAX_PERIODS       = 35

# Bar palette. Cycles when there are more visible nations than colours —
# the country-code label inside each bar disambiguates anyway.
PALETTE = [
    "#3B82F6", "#F59E0B", "#22C55E", "#EF4444", "#A78BFA",
    "#38BDF8", "#F97316", "#EC4899", "#14B8A6", "#EAB308",
    "#8B5CF6", "#10B981",
]


# ── flag fetch + cache ────────────────────────────────────────────────────

def fetch_flag(iso2: str) -> Optional[Path]:
    """Download a 60px PNG flag from flagcdn.com (cached). 60px gives a
    crisp render at our ~36-40px bar height. Returns None on failure."""
    if not iso2 or not isinstance(iso2, str):
        return None
    iso = iso2.lower()
    FLAG_CACHE.mkdir(parents=True, exist_ok=True)
    out = FLAG_CACHE / f"{iso}_60.png"
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        r = polite_get(f"https://flagcdn.com/w80/{iso}.png", sleep=0.0)
        if not r.ok or not r.content:
            return None
        out.write_bytes(r.content)
        return out
    except Exception:
        return None


# ── data prep ─────────────────────────────────────────────────────────────

def load_history() -> Optional[pd.DataFrame]:
    if not HISTORY_PARQUET.exists():
        print(f"[poly-race] {HISTORY_PARQUET.name} missing — history step hasn't produced a snapshot yet")
        return None
    df = pd.read_parquet(HISTORY_PARQUET)
    if df.empty:
        return None
    # The on-disk schema is daily snapshots keyed by `date_utc` (string)
    # plus per-team rows keyed by `nation_id` with `yes_price` (0..1).
    needed = {"date_utc", "nation_id", "yes_price"}
    if not needed.issubset(df.columns):
        print(f"[poly-race] schema mismatch — expected {needed}, got {set(df.columns)}")
        return None
    df["date_utc"] = pd.to_datetime(df["date_utc"], utc=True, errors="coerce")
    df = df.dropna(subset=["date_utc", "nation_id", "yes_price"]).copy()
    df["yes_price"] = df["yes_price"].astype(float)
    return df


def pivot_and_interpolate(df: pd.DataFrame) -> pd.DataFrame:
    """Long → wide (date x nation_id, value = pct in 0..100).

    Then dense-interpolate so the animation is smooth even though the
    raw data is one tick per day. The interpolated frame index uses
    STEPS_PER_PERIOD steps between adjacent snapshots, which works out
    to ~15x more frames than days.
    """
    wide = (
        df.pivot_table(
            index="date_utc",
            columns="nation_id",
            values="yes_price",
            aggfunc="last",
        )
        .sort_index()
        .ffill()
        .fillna(0.0)
    )
    if wide.shape[0] < MIN_SNAPSHOTS:
        return wide
    # Cap the raw period count — see MAX_PERIODS note. Keep the first
    # row (so the opening odds anchor the race) plus the most recent
    # MAX_PERIODS-1 rows.
    if wide.shape[0] > MAX_PERIODS:
        anchor = wide.iloc[[0]]
        recent = wide.iloc[-(MAX_PERIODS - 1):]
        wide = pd.concat([anchor, recent])
    # Build a fractional integer index 0, 1/N, 2/N, ... N where N =
    # STEPS_PER_PERIOD, then linearly interpolate by reindexing onto a
    # finer uniform grid. The result is len-1 * STEPS_PER_PERIOD + 1
    # frames, each containing every nation's smoothly-evolving pct.
    n_periods = wide.shape[0] - 1
    n_frames  = n_periods * STEPS_PER_PERIOD + 1
    fine_idx = np.linspace(0, n_periods, n_frames)
    # We need each row of `wide` at integer positions; values between
    # them via numpy linear interpolation per column. pandas
    # `interpolate` would do it but reindexing a DatetimeIndex onto a
    # finer step requires constructing a DatetimeRange — easier to just
    # numpy it. Build the interpolated array column by column.
    rows = []
    base_idx = np.arange(wide.shape[0])
    values = wide.values  # shape (T, K)
    for f in fine_idx:
        lo = int(np.floor(f))
        hi = min(lo + 1, len(base_idx) - 1)
        t  = f - lo
        rows.append(values[lo] * (1 - t) + values[hi] * t)
    interp = pd.DataFrame(rows, columns=wide.columns)
    # Convert to percentages once at the end so the animation deals in 0..100.
    interp = interp * 100.0
    # Also produce a parallel "label timestamp" series so the title can
    # show the real date even at sub-day fractional frames.
    timestamps = pd.Series(
        [wide.index[int(np.floor(f))] + (wide.index[min(int(np.floor(f)) + 1, len(wide.index) - 1)] - wide.index[int(np.floor(f))]) * (f - int(np.floor(f)))
         for f in fine_idx],
        index=interp.index,
    )
    interp.attrs["timestamps"] = timestamps
    return interp


# ── render ────────────────────────────────────────────────────────────────

def render_gif(wide: pd.DataFrame) -> None:
    """Render the race chart GIF. Bars are coloured rectangles; each bar
    carries a flag image + 3-letter code at its left edge (inside or
    just outside the bar depending on width) and the percentage at
    its right tip. The y-position of each bar is its current rank, so
    bars visibly swap when a nation overtakes another."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation, PillowWriter
    from matplotlib.offsetbox import AnnotationBbox, OffsetImage
    from PIL import Image

    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    # Look up iso_alpha2 + short label per nation. Used to fetch flags
    # and to render the in-bar code label.
    iso_by_nid: dict[str, str] = {}
    name_by_nid: dict[str, str] = {}
    try:
        nations = io.load_table("wc26_nations")
        for _, r in nations.iterrows():
            nid = r["nation_id"]
            if not isinstance(nid, str):
                continue
            iso = r.get("iso_alpha2")
            if isinstance(iso, str):
                iso_by_nid[nid] = iso.lower()
            short = r.get("fotmob_short_name") or r.get("seed_name") or nid
            name_by_nid[nid] = short
    except Exception as exc:
        print(f"[poly-race] nations lookup failed (continuing): {exc}")

    # Pre-fetch flags for ALL nations in the pivot (a nation may climb
    # into the top-N mid-race, so don't restrict by latest top-N).
    flag_imgs: dict[str, "Image.Image"] = {}
    for nid in wide.columns:
        iso = iso_by_nid.get(nid)
        path = fetch_flag(iso) if iso else None
        if path is None:
            continue
        try:
            flag_imgs[nid] = Image.open(path).convert("RGBA")
        except Exception:
            continue

    # Deterministic colour per nation_id by stable-sorted index, so
    # France is always blue across re-renders.
    sorted_nids = sorted(wide.columns)
    color_by_nid = {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(sorted_nids)}

    fig, ax = plt.subplots(figsize=(WIDTH_PX / DPI, HEIGHT_PX / DPI), dpi=DPI)
    fig.patch.set_facecolor("#111111")
    ax.set_facecolor("#111111")

    timestamps = wide.attrs.get("timestamps")
    n_frames = wide.shape[0]
    # Cap x-axis to the global max pct across the whole race + headroom,
    # so the scale doesn't jitter frame-to-frame. Worst case the leader's
    # bar shrinks visibly when things tighten — desirable.
    global_max = float(np.nanmax(wide.values))
    x_data_max = max(40.0, global_max * 1.15)
    # Reserve a left "label gutter" (negative x) for the flag + 3-letter
    # code so nothing overlaps the bars themselves. Gutter width is sized
    # as a fraction of the data range.
    gutter = x_data_max * 0.18
    flag_x = -gutter * 0.78
    code_x = -gutter * 0.32

    def draw(i: int) -> None:
        ax.clear()
        ax.set_facecolor("#111111")
        row = wide.iloc[i]
        # Sort ascending so the LARGEST bar is at the TOP (matplotlib's
        # barh draws bottom-up). Take top-N.
        ranked = row.sort_values(ascending=False).head(TOP_N)
        # Reverse for matplotlib's bottom-up plot.
        ranked = ranked.iloc[::-1]
        nids   = list(ranked.index)
        values = list(ranked.values)
        y_pos  = list(range(len(nids)))

        ax.barh(
            y_pos,
            values,
            color=[color_by_nid.get(n, "#888") for n in nids],
            edgecolor="#222222",
            linewidth=0.4,
            height=0.78,
        )
        # Gutter separator — thin vertical line at x=0 to anchor the bars
        # visually against the label band.
        ax.axvline(0, color="#333333", linewidth=0.6, zorder=0)
        # Decorate each row with flag (gutter, left), code label (gutter,
        # just left of x=0), and value (right tip of bar).
        for y, nid, v in zip(y_pos, nids, values):
            img = flag_imgs.get(nid)
            if img is not None:
                ab = AnnotationBbox(
                    OffsetImage(img, zoom=0.36),
                    (flag_x, y),
                    frameon=False,
                    xycoords="data",
                    boxcoords="data",
                    box_alignment=(0.5, 0.5),
                    pad=0,
                )
                ax.add_artist(ab)
            # 3-letter nation id in the gutter, right-aligned against x=0.
            ax.text(
                code_x,
                y,
                nid,
                va="center",
                ha="left",
                fontsize=10,
                color="#f9f9f9",
                family="monospace",
                fontweight="bold",
            )
            # Value at the right end of the bar.
            ax.text(
                v + x_data_max * 0.008,
                y,
                f"{v:.1f}%",
                va="center",
                ha="left",
                fontsize=10,
                color="#f9f9f9",
                family="monospace",
            )

        ax.set_xlim(-gutter, x_data_max)
        ax.set_ylim(-0.6, TOP_N - 0.4)
        ax.set_yticks([])
        # Only tick the data range (>= 0). The gutter band has no scale.
        tick_max = int(x_data_max // 5) * 5
        ax.set_xticks(list(range(0, tick_max + 1, 5)))
        ax.set_xlabel("Implied probability (%)", color="#8a8a8a", fontsize=9)
        ax.tick_params(colors="#8a8a8a", labelsize=9, length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
        # Date title — interpolated timestamp so it ticks day-by-day
        # smoothly even on sub-period frames.
        ts = timestamps.iloc[i] if timestamps is not None else None
        date_label = pd.Timestamp(ts).strftime("%Y-%m-%d") if ts is not None else f"frame {i}"
        ax.set_title(
            f"WC2026 Winner Market — Polymarket\n{date_label}",
            color="#f9f9f9",
            fontsize=12,
            family="monospace",
            loc="left",
            pad=10,
        )

    # Hold the last frame so the finish reads.
    indices = list(range(n_frames)) + [n_frames - 1] * HOLD_FRAMES

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        anim = FuncAnimation(fig, draw, frames=indices, interval=1000 / FPS)
        writer = PillowWriter(fps=FPS)
        anim.save(str(GIF_PATH), writer=writer)
    plt.close(fig)

    # Pillow re-pass with optimize=True + adaptive palette quantization
    # halves the file size compared to matplotlib's default writer. The
    # writer's frames are RGB; converting to a 128-colour palette is the
    # biggest win.
    try:
        with Image.open(GIF_PATH) as im:
            frames_iter = []
            try:
                while True:
                    frames_iter.append(
                        im.copy().convert("P", palette=Image.Palette.ADAPTIVE, colors=128)
                    )
                    im.seek(im.tell() + 1)
            except EOFError:
                pass
        if len(frames_iter) > 1:
            frames_iter[0].save(
                GIF_PATH,
                save_all=True,
                append_images=frames_iter[1:],
                duration=int(1000 / FPS),
                loop=0,
                optimize=True,
                disposal=2,
            )
    except Exception as exc:
        print(f"[poly-race] palette-optimize pass failed (keeping raw GIF): {exc}")

    size_kb = GIF_PATH.stat().st_size / 1024
    print(
        f"[poly-race] wrote {GIF_PATH.relative_to(ROOT)}  "
        f"({n_frames} interp frames + {HOLD_FRAMES} hold, {size_kb:.0f} KB)"
    )


# ── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    df = load_history()
    if df is None or df.empty:
        print("[poly-race] no history yet — skipping (no race chart to render)")
        return 0

    try:
        wide = pivot_and_interpolate(df)
    except Exception as exc:
        print(f"[poly-race] interpolation failed: {type(exc).__name__}: {exc}")
        return 0

    if wide.shape[0] < MIN_SNAPSHOTS * STEPS_PER_PERIOD:
        print(
            f"[poly-race] only {wide.shape[0]} interp frames; "
            f"need >= {MIN_SNAPSHOTS * STEPS_PER_PERIOD} — skipping"
        )
        return 0

    try:
        render_gif(wide)
    except Exception as exc:
        # Render failure shouldn't break the refresh pipeline.
        print(f"[poly-race] render failed (non-fatal): {type(exc).__name__}: {exc}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
