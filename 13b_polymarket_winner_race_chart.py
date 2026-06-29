"""Bar-chart-race GIF of the Polymarket world-cup-winner market.

Runs each hourly tick AFTER 13a_polymarket_winner_history. Reads the
long-format history parquet that 13a appends to, pivots it into a per-
team time series, and renders a matplotlib animation with nation flags
on each bar. Output:
    data/processed/static/wc26_polymarket_winner_race.gif

The companion `_emit_pwa_static.py` step copies that file alongside the
JSON emit, and the PWA's WinnerRaceChart renders it as an <img> when the
file exists.

Design notes
------------
* No external chart library — pure matplotlib FuncAnimation + PillowWriter
  (matplotlib's stdlib GIF backend). Avoids the ffmpeg / bar_chart_race
  dependency.
* Flags are fetched once from flagcdn.com (40px PNGs, ~1KB each) and
  cached under data/raw/flags/ so re-runs are offline-fast.
* Top-10 teams by the LATEST snapshot stay pinned through the animation.
  Other teams roll up into a transparent "Others" anchor — the user only
  cares about the contenders.
* Skips silently when <3 snapshots are available — bar-chart-race needs a
  trajectory, not a single point. Logs a notice so the cron output makes
  the wait state obvious.
* Failure-tolerant: any rendering error logs + exits 0 so the refresh
  pipeline isn't blocked. The PWA falls back to its live-snapshot
  placeholder when the GIF is missing.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Optional

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
FRAMES_TARGET   = 80   # ~ length of the GIF in frames; we resample history to this
FPS             = 10
TOP_N           = 10
WIDTH_PX        = 720
HEIGHT_PX       = 480
MIN_SNAPSHOTS   = 3    # need at least this many distinct ticks to animate

# ── flag fetch + cache ────────────────────────────────────────────────────

def fetch_flag(iso2: str) -> Optional[Path]:
    """Download a 40px PNG flag from flagcdn.com (cached). Returns None on failure."""
    if not iso2 or not isinstance(iso2, str):
        return None
    iso = iso2.lower()
    FLAG_CACHE.mkdir(parents=True, exist_ok=True)
    out = FLAG_CACHE / f"{iso}.png"
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        # 40px wide gives a clean rendering at our 480px chart height.
        r = polite_get(f"https://flagcdn.com/w40/{iso}.png", sleep=0.0)
        if not r.ok or not r.content:
            return None
        out.write_bytes(r.content)
        return out
    except Exception:
        return None


# ── data prep ─────────────────────────────────────────────────────────────

def load_history() -> Optional[pd.DataFrame]:
    if not HISTORY_PARQUET.exists():
        print(f"[poly-race] {HISTORY_PARQUET.name} missing — 13a hasn't produced a snapshot yet")
        return None
    df = pd.read_parquet(HISTORY_PARQUET)
    if df.empty:
        return None
    df["snapshot_ts"] = pd.to_datetime(df["snapshot_ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["snapshot_ts", "team_name", "pct"])
    return df


def build_frames(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str], dict[str, str]]:
    """Pivot history → wide frame matrix; return (wide_df, top_team_names, iso_by_team)."""
    # team_name → nation_id (mode), ignoring None entries.
    nid_by_team = (
        df.dropna(subset=["nation_id"])
        .groupby("team_name")["nation_id"]
        .agg(lambda s: s.mode().iat[0] if not s.mode().empty else None)
        .to_dict()
    )

    wide = (
        df.pivot_table(
            index="snapshot_ts",
            columns="team_name",
            values="pct",
            aggfunc="last",
        )
        .sort_index()
        .ffill()
    )
    if wide.shape[0] < MIN_SNAPSHOTS:
        return wide, [], {}

    latest = wide.iloc[-1].sort_values(ascending=False)
    top_teams = list(latest.head(TOP_N).index)
    wide_top = wide[top_teams].fillna(0.0)

    # Resample to even time intervals so the animation pace doesn't bunch
    # up around dense-snapshot hours. Pick a delta that targets ~FRAMES_TARGET
    # interpolated points across the observed span.
    span = wide.index[-1] - wide.index[0]
    if span.total_seconds() > 0:
        step = max(60, int(span.total_seconds() / FRAMES_TARGET))
        rs = wide_top.resample(f"{step}S").ffill().bfill()
        wide_top = rs

    # Map each top-team to its ISO-2 (lowercase). We need wc26_nations for
    # the iso_alpha2 ↔ nation_id link.
    iso_by_team: dict[str, str] = {}
    try:
        nations = io.load_table("wc26_nations")
        nation_iso = dict(zip(nations["nation_id"], nations["iso_alpha2"]))
        for team in top_teams:
            nid = nid_by_team.get(team)
            iso = nation_iso.get(nid) if nid else None
            if isinstance(iso, str):
                iso_by_team[team] = iso.lower()
    except Exception as exc:
        print(f"[poly-race] iso lookup failed (continuing without flags): {exc}")

    return wide_top, top_teams, iso_by_team


# ── render ────────────────────────────────────────────────────────────────

def render_gif(wide: pd.DataFrame, top_teams: list[str], iso_by_team: dict[str, str]) -> None:
    # Heavy imports kept inside the function so the script's import cost
    # is tiny when there's nothing to render.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation, PillowWriter
    from matplotlib.offsetbox import AnnotationBbox, OffsetImage
    from PIL import Image

    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    # Pre-load flag images once.
    flag_imgs: dict[str, "Image.Image"] = {}
    for team, iso in iso_by_team.items():
        path = fetch_flag(iso)
        if path is None:
            continue
        try:
            flag_imgs[team] = Image.open(path).convert("RGBA")
        except Exception:
            continue

    # Colour palette mirroring the PWA's CHART_PALETTE so the PWA and the
    # GIF feel like one product.
    PALETTE = [
        "#3B82F6", "#F59E0B", "#22C55E", "#EF4444", "#A78BFA",
        "#38BDF8", "#F97316", "#EC4899", "#14B8A6", "#EAB308",
    ]
    color_by_team = {t: PALETTE[i % len(PALETTE)] for i, t in enumerate(top_teams)}

    fig, ax = plt.subplots(figsize=(WIDTH_PX / 100, HEIGHT_PX / 100), dpi=100)
    fig.patch.set_facecolor("#111111")
    ax.set_facecolor("#111111")

    frames = wide.index

    def draw(i: int) -> None:
        ax.clear()
        ax.set_facecolor("#111111")
        ts = frames[i]
        row = wide.loc[ts].sort_values()
        teams = list(row.index)
        values = list(row.values)
        ax.barh(
            teams,
            values,
            color=[color_by_team.get(t, "#888") for t in teams],
            edgecolor="#222222",
            linewidth=0.5,
        )
        # Value labels at end of each bar.
        for y, (team, v) in enumerate(zip(teams, values)):
            ax.text(
                v + 0.4,
                y,
                f"{v:.1f}%",
                va="center",
                ha="left",
                fontsize=9,
                color="#f9f9f9",
                family="monospace",
            )
            img = flag_imgs.get(team)
            if img is not None:
                # Slightly inset from the bar end so the flag sits inside
                # the bar's coloured area at high pct, outside at low pct.
                ab = AnnotationBbox(
                    OffsetImage(img, zoom=0.5),
                    (max(v - 1.5, 0.5), y),
                    frameon=False,
                    pad=0,
                )
                ax.add_artist(ab)
        ax.set_xlim(0, max(40, float(row.max()) + 5))
        ax.set_xlabel("Implied probability (%)", color="#8a8a8a", fontsize=9)
        ax.tick_params(colors="#8a8a8a", labelsize=9)
        for spine in ax.spines.values():
            spine.set_color("#222222")
        ts_label = pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M UTC")
        ax.set_title(
            f"WC2026 Winner — {ts_label}",
            color="#f9f9f9",
            fontsize=12,
            family="monospace",
            loc="left",
        )

    # Hold the last frame for a beat so the final standings register.
    hold = max(5, FPS)
    indices = list(range(len(frames))) + [len(frames) - 1] * hold

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        anim = FuncAnimation(fig, draw, frames=indices, interval=1000 / FPS)
        writer = PillowWriter(fps=FPS)
        anim.save(str(GIF_PATH), writer=writer)
    plt.close(fig)
    size_kb = GIF_PATH.stat().st_size / 1024
    print(f"[poly-race] wrote {GIF_PATH.relative_to(ROOT)}  ({len(frames)} frames, {size_kb:.0f} KB)")


# ── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    df = load_history()
    if df is None or df.empty:
        print("[poly-race] no history yet — skipping (no race chart to render)")
        return 0

    try:
        wide, top_teams, iso_by_team = build_frames(df)
    except Exception as exc:
        print(f"[poly-race] frame-build failed: {exc}")
        return 0

    if not top_teams or wide.shape[0] < MIN_SNAPSHOTS:
        print(f"[poly-race] only {wide.shape[0]} snapshot(s); need ≥{MIN_SNAPSHOTS} — skipping")
        return 0

    try:
        render_gif(wide, top_teams, iso_by_team)
    except Exception as exc:
        # Render failure shouldn't break the refresh pipeline.
        print(f"[poly-race] render failed (non-fatal): {type(exc).__name__}: {exc}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
