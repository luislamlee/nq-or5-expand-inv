#!/usr/bin/env python3
"""Score every RTH 5m candle with Luis's locked 9-check candle force system.

Research only. Does not touch locked play engines. No backtest.

Each completed 5m bar votes 9 factors +1 / 0 / −1. Score = sum ∈ [−9,+9].
+n = LONG/up · −n = SHORT/down · 0 = neutro.

Default day: 2026-08-13 NQU26. Causal: score only after bar close; VbP from
that bar's own 20×15s footprint; volume median from prior completed 5m only.
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date
from pathlib import Path

# Historical machine-specific import bootstrap removed; see source manifest.
# Historical machine-specific import bootstrap removed; see source manifest.

# Historical machine-specific import bootstrap removed; see source manifest.


import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd
import duckdb

from plot_daily_1h_ah_rth_dva import C_DN, C_UP, C_WICK
from plot_pmor_orhl5_day import load_rth_15s
from luis_or_127_close205 import as_naive
from luis_vbp_or import BARS_PER_5M, TICK
from plot_eval3_5m_va_pmor import chunk_ohlc_delta, vbp_va70

DB_EXT = Path("/workspace/sierra/NQU26-CME-15s-ext.duckdb")
DB_1MO = Path("/workspace/sierra/NQU26-CME-15s-1mo.duckdb")
OUT_DIR = Path("/workspace/sierra/candle_force")
DEFAULT_DAY = date(2026, 8, 13)
FLATTEN_BAR = 1560  # 78 × 20
DOJI_FRAC = 0.10
MARU_FRAC = 0.70
AT_TICKS = 0.5  # ±0.5 tick = "at" POC / VWAP

VOTE_COLS = [
    "body",
    "close_loc",
    "vs_poc",
    "vs_va",
    "vs_vwap",
    "hvn_shape",
    "volume",
    "delta_vote",
    "marubozu",
]


def pick_db(preferred: Path | None = None) -> Path:
    if preferred is not None and preferred.exists():
        return preferred
    for p in (DB_EXT, DB_1MO):
        if not p.exists():
            continue
        con = duckdb.connect(str(p), read_only=True)
        try:
            n = con.execute(
                "SELECT count(*) FROM bars_15s "
                f"WHERE trading_day = DATE '{DEFAULT_DAY.isoformat()}' "
                "AND rth_bar_number BETWEEN 1 AND 1560"
            ).fetchone()[0]
        finally:
            con.close()
        if n and n >= 1560:
            return p
    raise SystemExit("no DuckDB with full RTH 15s for default day")


def load_footprint(day: date, db: Path, max_bn: int = FLATTEN_BAR) -> pd.DataFrame:
    con = duckdb.connect(str(db), read_only=True)
    fp = con.execute(
        """
        SELECT trading_day, rth_bar_number, price, total_volume
        FROM footprint_15s_by_price
        WHERE trading_day = ?
          AND rth_bar_number IS NOT NULL
          AND rth_bar_number BETWEEN 1 AND ?
        ORDER BY rth_bar_number, price
        """,
        [day, max_bn],
    ).fetchdf()
    con.close()
    if fp.empty:
        return fp
    fp["trading_day"] = pd.to_datetime(fp["trading_day"]).dt.date
    return fp.reset_index(drop=True)


def third_loc(price: float, lo: float, hi: float) -> int:
    """upper third → +1; mid → 0; lower → −1. Flat bar → 0."""
    rng = hi - lo
    if rng <= 1e-12 or not math.isfinite(price):
        return 0
    pos = (price - lo) / rng
    if pos >= 2.0 / 3.0:
        return 1
    if pos <= 1.0 / 3.0:
        return -1
    return 0


def vote_body(o: float, c: float, lo: float, hi: float) -> int:
    rng = hi - lo
    body = abs(c - o)
    if body < TICK - 1e-12 or (rng > 1e-12 and body < DOJI_FRAC * rng - 1e-12):
        return 0
    if c > o + 1e-12:
        return 1
    if c < o - 1e-12:
        return -1
    return 0


def vote_vs_level(c: float, level: float, at_ticks: float = AT_TICKS) -> int:
    if not math.isfinite(level):
        return 0
    if abs(c - level) < at_ticks * TICK + 1e-12:
        return 0
    if c > level + 1e-12:
        return 1
    return -1


def vote_vs_va(c: float, val: float, vah: float) -> int:
    if not math.isfinite(val) or not math.isfinite(vah):
        return 0
    if c > vah + 1e-12:
        return 1
    if c < val - 1e-12:
        return -1
    return 0


def vote_volume(vol: float, median_prior: float | None, body_vote: int) -> int:
    """HIGH = above median of prior completed 5m. high+bull→+1; high+bear→−1; else 0."""
    if median_prior is None or not math.isfinite(median_prior):
        return 0
    if vol <= median_prior + 1e-12:
        return 0
    if body_vote > 0:
        return 1
    if body_vote < 0:
        return -1
    return 0  # high + doji → 0


def vote_delta(delta: float) -> int:
    if abs(delta) < 1e-9:
        return 0
    return 1 if delta > 0 else -1


def vote_marubozu(o: float, c: float, lo: float, hi: float) -> int:
    rng = hi - lo
    if rng <= 1e-12:
        return 0
    body_range = abs(c - o) / rng
    if body_range <= MARU_FRAC + 1e-12:
        return 0
    if c > o + 1e-12:
        return 1
    if c < o - 1e-12:
        return -1
    return 0


def bar_vwap(chunk: pd.DataFrame) -> float:
    """VWAP of THIS 5m only = sum((H+L+C)/3 * vol) / sum(vol) over its 15s bars."""
    tp = (
        chunk["high"].astype(float)
        + chunk["low"].astype(float)
        + chunk["close"].astype(float)
    ) / 3.0
    vol = chunk["total_volume"].astype(float)
    sv = float(vol.sum())
    if sv <= 0:
        return float("nan")
    return float((tp * vol).sum() / sv)


def score_day(day: date, db: Path) -> pd.DataFrame:
    g15 = load_rth_15s(day, db)
    if g15.empty:
        raise SystemExit(f"no 15s bars for {day} in {db}")
    g15 = g15[g15["rth_bar_number"].between(1, FLATTEN_BAR)].copy()
    max_bn = int(g15["rth_bar_number"].max())
    n5 = max_bn // BARS_PER_5M
    fp = load_footprint(day, db, max_bn=n5 * BARS_PER_5M)

    rows: list[dict] = []
    prior_vols: list[float] = []

    for k in range(1, n5 + 1):
        b0 = (k - 1) * BARS_PER_5M + 1
        b1 = k * BARS_PER_5M
        chunk = g15[g15["rth_bar_number"].between(b0, b1)].sort_values("rth_bar_number")
        if len(chunk) < BARS_PER_5M:
            continue

        oh = chunk_ohlc_delta(g15, b0, b1)
        o, h, lo, c = oh["open"], oh["high"], oh["low"], oh["close"]
        vol = float(oh["volume"])
        delta = float(oh["volume_delta"])
        va = vbp_va70(fp, b0, b1, c)
        poc, val, vah = va["poc"], va["val"], va["vah"]
        vwap = bar_vwap(chunk)

        v_body = vote_body(o, c, lo, h)
        v_close_loc = third_loc(c, lo, h)
        v_vs_poc = vote_vs_level(c, poc)
        v_vs_va = vote_vs_va(c, val, vah)
        v_vs_vwap = vote_vs_level(c, vwap)
        v_hvn = third_loc(poc, lo, h) if math.isfinite(poc) else 0
        med = float(np.median(prior_vols)) if prior_vols else None
        v_vol = vote_volume(vol, med, v_body)
        v_delta = vote_delta(delta)
        v_maru = vote_marubozu(o, c, lo, h)

        votes = {
            "body": v_body,
            "close_loc": v_close_loc,
            "vs_poc": v_vs_poc,
            "vs_va": v_vs_va,
            "vs_vwap": v_vs_vwap,
            "hvn_shape": v_hvn,
            "volume": v_vol,
            "delta_vote": v_delta,
            "marubozu": v_maru,
        }
        score = int(sum(votes.values()))

        t0 = as_naive(chunk.iloc[0]["t"])
        t1 = as_naive(chunk.iloc[-1]["t_end"])
        rows.append(
            {
                "trading_day": day.isoformat(),
                "m5_bar": k,
                "b0": b0,
                "b1": b1,
                "time": t0.strftime("%H:%M"),
                "t_start": t0.isoformat(sep=" "),
                "t_end": t1.isoformat(sep=" "),
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(lo, 4),
                "close": round(c, 4),
                "vol": vol,
                "delta": delta,
                "poc": round(float(poc), 4) if math.isfinite(poc) else float("nan"),
                "val": round(float(val), 4) if math.isfinite(val) else float("nan"),
                "vah": round(float(vah), 4) if math.isfinite(vah) else float("nan"),
                "vwap": round(float(vwap), 4) if math.isfinite(vwap) else float("nan"),
                **votes,
                "score": score,
            }
        )
        prior_vols.append(vol)

    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit(f"no completed 5m bars for {day}")
    df["cum_score"] = df["score"].cumsum().astype(int)
    # sanity: score == sum of vote cols
    assert (df[VOTE_COLS].sum(axis=1) == df["score"]).all()
    return df


def draw_chart(df: pd.DataFrame, day: date, out_png: Path) -> None:
    fig, (ax, ax2) = plt.subplots(
        2,
        1,
        figsize=(16.5, 14.0),
        gridspec_kw={"height_ratios": [2.6, 1.0], "hspace": 0.10},
        sharex=True,
    )
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#ffffff")
    ax2.set_facecolor("#ffffff")

    w = 3.6 / (24 * 60)  # ~3.6 min in matplotlib date units
    xs: list[float] = []
    for _, r in df.iterrows():
        tt = pd.Timestamp(r["t_start"]).to_pydatetime()
        o, h, lo, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        up = c >= o
        body_c = C_UP if up else C_DN
        x = mdates.date2num(tt)
        xs.append(x)
        ax.plot([x, x], [lo, h], color=C_WICK, linewidth=0.9, solid_capstyle="round", zorder=3)
        y0, y1 = min(o, c), max(o, c)
        if abs(y1 - y0) < 1e-9:
            y1 = y0 + TICK * 0.15
        ax.add_patch(
            Rectangle(
                (x - w / 2, y0),
                w,
                y1 - y0,
                facecolor=body_c,
                edgecolor=body_c,
                linewidth=0.6,
                zorder=4,
                alpha=0.92,
            )
        )
        sc = int(r["score"])
        pad = (h - lo) * 0.08 + TICK * 2
        if sc > 0:
            ax.text(
                x, h + pad, f"+{sc}",
                ha="center", va="bottom", fontsize=7.5,
                color="#1b7a32", fontweight="bold", zorder=5,
            )
        elif sc < 0:
            ax.text(
                x, lo - pad, f"{sc}",
                ha="center", va="top", fontsize=7.5,
                color="#c62828", fontweight="bold", zorder=5,
            )
        else:
            ax.text(
                x, h + pad * 0.5, "0",
                ha="center", va="bottom", fontsize=6.0,
                color="#78909c", zorder=5,
            )

    hi = float(df["high"].max())
    lo_all = float(df["low"].min())
    pad_y = (hi - lo_all) * 0.06 + TICK * 4
    ax.set_ylim(lo_all - pad_y * 1.8, hi + pad_y * 1.8)
    ax.set_ylabel("NQU26", color="#000000", fontsize=11)
    ax.tick_params(colors="#000000")
    for spine in ax.spines.values():
        spine.set_color("#90a4ae")
    ax.set_title(
        f"{day.strftime('%b %d')} NQU26 · fuerza 5m (−9…+9)",
        color="#000000",
        fontsize=14,
        fontweight="bold",
        pad=10,
    )
    ax.grid(True, axis="y", color="#eceff1", linewidth=0.6, zorder=0)

    scores = df["score"].to_numpy(dtype=int)
    colors = [
        "#1b7a32" if s > 0 else ("#c62828" if s < 0 else "#90a4ae") for s in scores
    ]
    ax2.bar(xs, scores, width=w * 1.15, color=colors, alpha=0.85, zorder=3, align="center")
    ax2.axhline(0, color="#546e7a", linewidth=0.8, zorder=2)
    cum = df["cum_score"].to_numpy(dtype=int)
    ax2.set_ylabel("score (−9…+9)", color="#000000", fontsize=10)
    ax2.set_ylim(-9.8, 9.8)
    ax2.tick_params(colors="#000000")
    for spine in ax2.spines.values():
        spine.set_color("#90a4ae")
    ax2.grid(True, axis="y", color="#eceff1", linewidth=0.6, zorder=0)

    ax2b = ax2.twinx()
    ax2b.plot(
        xs, cum, color="#1565c0", linewidth=1.8, marker="o", markersize=2.4,
        label="cum_score", zorder=5,
    )
    cmin, cmax = int(cum.min()), int(cum.max())
    pad_c = max(3, int(max(abs(cmin), abs(cmax)) * 0.08) + 1)
    ax2b.set_ylim(cmin - pad_c, cmax + pad_c)
    ax2b.set_ylabel("cum_score", color="#1565c0", fontsize=10)
    ax2b.tick_params(axis="y", colors="#1565c0")
    ax2b.legend(loc="upper left", fontsize=8, frameon=True, facecolor="white", edgecolor="#cfd8dc")

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax2.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0, 30]))
    ax2.set_xlabel("Chicago CT", color="#000000", fontsize=10)
    fig.autofmt_xdate(rotation=0, ha="center")

    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=140, bbox_inches="tight", facecolor="#ffffff")
    plt.close(fig)


def print_summary(df: pd.DataFrame, day: date) -> None:
    scores = df["score"]
    print(f"=== CANDLE FORCE {day.isoformat()} NQU26 ===")
    print(f"n_bars={len(df)}  mean_score={scores.mean():.3f}  std_score={scores.std(ddof=0):.3f}")
    print(f"end_cum_score={int(df['cum_score'].iloc[-1])}")
    print("score buckets:")
    vc = scores.value_counts().sort_index()
    for s, n in vc.items():
        print(f"  {int(s):+d}: {int(n)}")
    imax = int(scores.idxmax())
    imin = int(scores.idxmin())
    rmax = df.loc[imax]
    rmin = df.loc[imin]
    print(
        f"max+: {int(rmax['score']):+d} @ {rmax['time']} "
        f"OHLC={rmax['open']:.2f}/{rmax['high']:.2f}/{rmax['low']:.2f}/{rmax['close']:.2f}"
    )
    print(
        f"max−: {int(rmin['score']):+d} @ {rmin['time']} "
        f"OHLC={rmin['open']:.2f}/{rmin['high']:.2f}/{rmin['low']:.2f}/{rmin['close']:.2f}"
    )
    print("--- first three bars (08:30 / 08:35 / 08:40) ---")
    for _, r in df.head(3).iterrows():
        print(
            f"  {r['time']}  OHLC={r['open']:.2f}/{r['high']:.2f}/{r['low']:.2f}/{r['close']:.2f}  "
            f"vol={r['vol']:.0f}  dlt={r['delta']:.0f}  "
            f"POC={r['poc']:.2f} VAL={r['val']:.2f} VAH={r['vah']:.2f} VWAP={r['vwap']:.2f}"
        )
        print(
            f"    votes body={int(r['body'])} close_loc={int(r['close_loc'])} "
            f"vs_poc={int(r['vs_poc'])} vs_va={int(r['vs_va'])} vs_vwap={int(r['vs_vwap'])} "
            f"hvn={int(r['hvn_shape'])} volume={int(r['volume'])} "
            f"delta={int(r['delta_vote'])} maru={int(r['marubozu'])}  "
            f"→ score={int(r['score'])} cum={int(r['cum_score'])}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Score RTH 5m candles with 9-check candle force")
    ap.add_argument("--day", default=DEFAULT_DAY.isoformat(), help="YYYY-MM-DD (Chicago trading day)")
    ap.add_argument("--contract", default="NQU26", help="contract label (informational)")
    ap.add_argument("--db", type=Path, default=None, help="DuckDB path")
    args = ap.parse_args()
    day = date.fromisoformat(args.day)
    db = pick_db(args.db)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = score_day(day, db)

    csv_path = OUT_DIR / f"CANDLE_FORCE_{day.isoformat()}_bars.csv"
    png_path = OUT_DIR / f"CANDLE_FORCE_{day.isoformat()}.png"
    df.to_csv(csv_path, index=False)
    draw_chart(df, day, png_path)
    print_summary(df, day)
    print(f"wrote {csv_path}")
    print(f"wrote {png_path}")
    print(f"db={db} contract={args.contract}")


if __name__ == "__main__":
    main()
