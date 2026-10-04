#!/usr/bin/env python3
"""PM Opening Range chart (Phase 1 display) — Premarket H/L ARE the OR.

Day default: 2026-08-13 America/Chicago.

Shows:
  - PM OR = prior trading day 15:00:00 CT → D 08:29:59 CT (Sierra 1m SCID)
  - Fib expansions 1.27 / 1.618 / 2.05 from PM range
  - First RTH 5m candle (08:30–08:35) with CLOSE marked vs PM H/L
  - RTH 15s + 5m context (no trade entry/exit — entry rule TBD)

NEW script only. Does not edit locked engines.
Does NOT assume ORHL5 1.27+cum_delta entry (waiting on Luis).
Not a book.
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

_VENV = Path("/workspace/sierra/.venv/lib/python3.13/site-packages")
if _VENV.exists() and str(_VENV) not in sys.path:
    sys.path.insert(0, str(_VENV))
if "/workspace/sierra" not in sys.path:
    sys.path.insert(0, "/workspace/sierra")

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd
import duckdb

from plot_daily_1h_ah_rth_dva import (
    C_DN,
    C_RTH_BG,
    C_UP,
    C_WICK,
    prev_trading_day,
    _naive,
)
from luis_or_127_r2va import expansion_level
from luis_or_127_swing import C_OR_GOLD, FIB_COL
from luis_or_127_close205 import C_127_L, as_naive, hm_s
from luis_vbp_or import BARS_PER_5M, TICK

SCID_1M = Path("/workspace/sierra/NQU26-CME scid compare.txt")
DB_PATH = Path("/workspace/sierra/NQU26-CME-15s-ext.duckdb")
CHART_DIR = Path("/workspace/sierra/or_127_close205_charts")
DEFAULT_DAY = date(2026, 8, 13)
OUT_NAME = "PMOR_ORHL5_{day}.png"
DPI = 120
FIGSIZE = (16.0, 11.5)

DISPLAY_FIBS = (1.27, 1.618, 2.05)


# ---------------------------------------------------------------------------
# Loaders (improved SCID: July+August stamps, multi-day overnight)
# ---------------------------------------------------------------------------

def load_scid_1m_range(path: Path, d0: date, d1: date) -> pd.DataFrame:
    """Sierra 1m CSV; Eastern wall → Chicago (ET−1 in Aug CDT). All days d0..d1."""
    want: set[date] = set()
    d = d0
    while d <= d1:
        want.add(d)
        d += timedelta(days=1)
    rows: list[dict] = []
    with path.open() as f:
        f.readline()
        for line in f:
            if not line.startswith("2026-"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 13:
                continue
            bits = parts[0].split("-")
            if len(bits) != 3:
                continue
            ds = f"{bits[0]}-{int(bits[1]):02d}-{int(bits[2]):02d}"
            try:
                dd = date.fromisoformat(ds)
            except ValueError:
                continue
            if dd not in want:
                continue
            t_et = datetime.fromisoformat(f"{ds}T{parts[1].split('.')[0]}")
            t_ct = t_et - timedelta(hours=1)
            try:
                o, h, lo, c = map(float, (parts[2], parts[3], parts[4], parts[5]))
                vol = int(float(parts[6]))
                bid = int(float(parts[11]))
                ask = int(float(parts[12]))
            except ValueError:
                continue
            rows.append(
                {
                    "t": t_ct,
                    "open": o,
                    "high": h,
                    "low": lo,
                    "close": c,
                    "total_volume": vol,
                    "bid_volume": bid,
                    "ask_volume": ask,
                    "volume_delta": ask - bid,
                }
            )
    if not rows:
        return pd.DataFrame()
    return (
        pd.DataFrame(rows)
        .sort_values("t")
        .drop_duplicates("t")
        .reset_index(drop=True)
    )


def load_rth_15s(day: date, db: Path = DB_PATH) -> pd.DataFrame:
    con = duckdb.connect(str(db), read_only=True)
    g = con.execute(
        """
        SELECT
            rth_bar_number, bar_start_chicago, bar_end_chicago,
            open, high, low, close,
            total_volume, volume_delta, bid_volume, ask_volume
        FROM bars_15s
        WHERE trading_day = ?
          AND rth_bar_number IS NOT NULL
        ORDER BY rth_bar_number
        """,
        [day],
    ).fetchdf()
    con.close()
    if g.empty:
        return g
    g["t"] = [_naive(x) for x in g["bar_start_chicago"]]
    g["t_end"] = [_naive(x) for x in g["bar_end_chicago"]]
    return g.reset_index(drop=True)


def pm_or_for_day(
    day: date,
    db: Path = DB_PATH,
    scid: Path = SCID_1M,
) -> dict | None:
    """Premarket OR: prior TD 15:00 CT → day 08:29:59 CT from Sierra 1m.

    Returns None if no real PM data (do not invent).
    """
    yday = prev_trading_day(day, db)
    if yday is None:
        return None
    scid_1m = load_scid_1m_range(scid, yday, day)
    if scid_1m.empty:
        return None
    ah_start = datetime.combine(yday, dtime(15, 0))
    ah_end = datetime.combine(day, dtime(8, 30))  # exclusive → through 08:29
    pm = scid_1m[(scid_1m["t"] >= ah_start) & (scid_1m["t"] < ah_end)].copy()
    if len(pm) < 60:
        return None
    pmh = float(pm["high"].max())
    pml = float(pm["low"].min())
    t0 = pm["t"].min()
    t1 = pm["t"].max()
    expected_start = ah_start
    partial = t0 > expected_start + timedelta(minutes=30)
    return {
        "day": day,
        "yday": yday,
        "pm_high": pmh,
        "pm_low": pml,
        "pm_r": pmh - pml,
        "n_1m": int(len(pm)),
        "t0": t0,
        "t1": t1,
        "window_start": ah_start,
        "window_end_excl": ah_end,
        "partial": partial,
        "src": str(scid),
    }


def first_5m_ohlc(g15: pd.DataFrame) -> dict | None:
    """RTH bars 1–20 → first 5m candle 08:30–08:35 CT."""
    chunk = g15[g15["rth_bar_number"].between(1, BARS_PER_5M)].copy()
    if chunk.empty or len(chunk) < BARS_PER_5M:
        return None
    chunk = chunk.sort_values("rth_bar_number")
    return {
        "open": float(chunk.iloc[0]["open"]),
        "high": float(chunk["high"].max()),
        "low": float(chunk["low"].min()),
        "close": float(chunk.iloc[-1]["close"]),
        "t0": as_naive(chunk.iloc[0]["t"]),
        "t1": as_naive(chunk.iloc[-1]["t_end"]),
        "n": int(len(chunk)),
    }


def close_vs_pm(close: float, pmh: float, pml: float) -> str:
    if close > pmh + 1e-12:
        return "above_PMH"
    if close < pml - 1e-12:
        return "below_PML"
    return "inside_PM_OR"


def aggregate_5m_from_15s(g15: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for end_bn in range(BARS_PER_5M, int(g15["rth_bar_number"].max()) + 1, BARS_PER_5M):
        start_bn = end_bn - BARS_PER_5M + 1
        sub = g15[g15["rth_bar_number"].between(start_bn, end_bn)]
        if sub.empty:
            continue
        sub = sub.sort_values("rth_bar_number")
        rows.append(
            {
                "t": as_naive(sub.iloc[0]["t"]),
                "t_end": as_naive(sub.iloc[-1]["t_end"]),
                "open": float(sub.iloc[0]["open"]),
                "high": float(sub["high"].max()),
                "low": float(sub["low"].min()),
                "close": float(sub.iloc[-1]["close"]),
                "total_volume": float(sub["total_volume"].sum()),
                "end_bn": end_bn,
            }
        )
    return pd.DataFrame(rows)


def draw_5m_candles(ax, five: pd.DataFrame, t0, t1, emph_first: bool = True) -> None:
    if five is None or five.empty:
        return
    w = 4.2 / (24 * 60)  # ~4.2 min in days
    for i, r in five.iterrows():
        t = r["t"]
        if t < t0 or t > t1:
            continue
        o, h, lo, c = r["open"], r["high"], r["low"], r["close"]
        up = c >= o
        body_c = C_UP if up else C_DN
        edge = body_c
        lw = 1.15
        alpha = 0.55
        if emph_first and int(r.get("end_bn", 0)) == BARS_PER_5M:
            edge = "#0f172a"
            lw = 2.0
            alpha = 0.92
        xm = mdates.date2num(t) + w / 2
        ax.plot([xm, xm], [lo, h], color=C_WICK, lw=1.0, solid_capstyle="butt", zorder=4.2)
        y0 = min(o, c)
        ht = max(abs(c - o), TICK * 0.5)
        ax.add_patch(
            Rectangle(
                (mdates.date2num(t), y0),
                w,
                ht,
                facecolor=body_c,
                edgecolor=edge,
                lw=lw,
                alpha=alpha,
                zorder=4.5,
            )
        )


def build_chart(day: date, db: Path = DB_PATH, scid: Path = SCID_1M, out: Path | None = None) -> Path:
    pm = pm_or_for_day(day, db, scid)
    if pm is None:
        raise SystemExit(f"no real PM 1m data for {day} — cannot invent PM H/L")

    g15 = load_rth_15s(day, db)
    if g15.empty:
        raise SystemExit(f"no RTH 15s for {day}")
    first5 = first_5m_ohlc(g15)
    if first5 is None:
        raise SystemExit(f"incomplete first 5m for {day}")

    pmh, pml, pmr = pm["pm_high"], pm["pm_low"], pm["pm_r"]
    f5c = first5["close"]
    loc = close_vs_pm(f5c, pmh, pml)

    long_fibs = [(k, expansion_level(pmh, pml, k, "up")) for k in DISPLAY_FIBS]
    short_fibs = [(k, expansion_level(pmh, pml, k, "down")) for k in DISPLAY_FIBS]

    five = aggregate_5m_from_15s(g15)
    t0 = datetime.combine(day, dtime(8, 30))
    # focus morning + a bit after first hour; widen if needed for levels
    t_right = datetime.combine(day, dtime(11, 0))
    vis = g15[(g15["t"] >= t0) & (g15["t"] <= t_right)].copy()

    CHART_DIR.mkdir(parents=True, exist_ok=True)
    out_path = out or (CHART_DIR / OUT_NAME.format(day=day.isoformat()))

    fig, (ax, axv) = plt.subplots(
        2,
        1,
        sharex=True,
        figsize=FIGSIZE,
        dpi=DPI,
        gridspec_kw={"height_ratios": [3.8, 0.85], "hspace": 0.05},
    )
    ax.fill_between(
        vis["t"],
        vis["low"],
        vis["high"],
        step="post",
        color="#6b6b6b",
        alpha=0.32,
        lw=0,
        zorder=1,
        label="15s Hi/Lo",
    )
    ax.step(vis["t"], vis["close"], where="post", color="0.08", lw=1.25, zorder=3.2, label="15s close")
    draw_5m_candles(ax, five, t0, t_right, emph_first=True)

    # PM OR band across pane
    ax.axhspan(pml, pmh, facecolor="#ffcc00", alpha=0.22, zorder=0.8, label="PM OR (H/L)")
    ax.axhline(pmh, color=C_OR_GOLD, lw=1.6, zorder=5)
    ax.axhline(pml, color=C_OR_GOLD, lw=1.6, zorder=5)
    # left "box" mark at open (visual OR box stand-in)
    x0 = mdates.date2num(t0)
    x1 = mdates.date2num(datetime.combine(day, dtime(8, 35)))
    ax.add_patch(
        Rectangle(
            (x0, pml),
            x1 - x0,
            pmh - pml,
            facecolor="#ffcc00",
            edgecolor="#aa8800",
            lw=1.4,
            alpha=0.35,
            zorder=2.2,
            label="PM OR @ open",
        )
    )
    ax.text(
        1.004,
        pmh,
        f"PMH  {pmh:.2f}",
        transform=ax.get_yaxis_transform(),
        color=C_OR_GOLD,
        fontsize=7.5,
        va="bottom",
        ha="left",
        clip_on=False,
        fontweight="bold",
    )
    ax.text(
        1.004,
        pml,
        f"PML  {pml:.2f}",
        transform=ax.get_yaxis_transform(),
        color=C_OR_GOLD,
        fontsize=7.5,
        va="top",
        ha="left",
        clip_on=False,
        fontweight="bold",
    )

    # fibs
    for k, px in long_fibs:
        col = FIB_COL.get(k, C_127_L if abs(k - 1.27) < 1e-9 else "#555")
        ax.axhline(px, color=col, lw=1.15, ls="--" if abs(k - 1.27) < 1e-9 else "-", zorder=3.4)
        ax.text(
            1.004,
            px,
            f"{k:g}↑  {px:.2f}",
            transform=ax.get_yaxis_transform(),
            color=col,
            fontsize=6.8,
            va="center",
            ha="left",
            clip_on=False,
            fontweight="bold",
        )
    for k, px in short_fibs:
        col = FIB_COL.get(k, "#d62728")
        ax.axhline(px, color=col, lw=1.0, ls=":", zorder=3.3, alpha=0.85)
        ax.text(
            1.004,
            px,
            f"{k:g}↓  {px:.2f}",
            transform=ax.get_yaxis_transform(),
            color=col,
            fontsize=6.5,
            va="center",
            ha="left",
            clip_on=False,
        )

    # First 5m close marker
    t_close = first5["t1"] - timedelta(seconds=1)
    mkr_col = "#16a34a" if loc == "above_PMH" else ("#dc2626" if loc == "below_PML" else "#ca8a04")
    ax.scatter(
        [t_close],
        [f5c],
        marker="D",
        s=90,
        color=mkr_col,
        edgecolors="#0f172a",
        linewidths=0.8,
        zorder=10,
        label=f"1st 5m close ({loc})",
    )
    ax.annotate(
        f"08:35 close {f5c:.2f}\n{loc}",
        xy=(t_close, f5c),
        xytext=(25, 28 if loc != "below_PML" else -36),
        textcoords="offset points",
        fontsize=8.5,
        fontweight="bold",
        color=mkr_col,
        arrowprops=dict(arrowstyle="->", color=mkr_col, lw=1.2),
        zorder=11,
    )
    ax.axvline(datetime.combine(day, dtime(8, 35)), color="#64748b", lw=0.9, ls=":", alpha=0.8, zorder=2.5)
    ax.axvline(t0, color="#334155", lw=1.0, ls="-", alpha=0.7, zorder=2.5)

    # volume pane (5m)
    if not five.empty:
        five_vis = five[(five["t"] >= t0) & (five["t"] <= t_right)]
        for _, r in five_vis.iterrows():
            up = r["close"] >= r["open"]
            axv.bar(
                r["t"],
                r["total_volume"],
                width=timedelta(minutes=4),
                color=C_UP if up else C_DN,
                alpha=0.7,
                align="edge",
            )
    axv.set_ylabel("5m vol")
    axv.grid(True, alpha=0.25)

    # ylim from morning price action + PM + nearby fibs
    ys = [float(vis["low"].min()), float(vis["high"].max()), pml, pmh, f5c]
    for _, px in long_fibs + short_fibs:
        if abs(px - f5c) < pmr * 2.5:
            ys.append(px)
    pad = max(8.0, (max(ys) - min(ys)) * 0.06)
    ax.set_ylim(min(ys) - pad, max(ys) + pad)
    ax.set_xlim(t0 - timedelta(minutes=2), t_right)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.grid(True, alpha=0.28)
    ax.legend(loc="upper left", fontsize=7.0, framealpha=0.92, ncol=2)
    ax.set_ylabel("NQ")

    partial_note = "  [PM partial start]" if pm["partial"] else ""
    ax.set_title(
        f"NQU26  {day.isoformat()}  ·  PM OR = Premarket H/L  ·  first RTH 5m close marked\n"
        f"PM: {pm['yday']} 15:00 → {day} 08:29 CT  "
        f"H={pmh:.2f}  L={pml:.2f}  R={pmr:.2f}  (n_1m={pm['n_1m']}){partial_note}\n"
        f"1st 5m 08:30–08:35  O={first5['open']:.2f} H={first5['high']:.2f} "
        f"L={first5['low']:.2f} C={f5c:.2f}  → {loc}\n"
        f"ENTRY RULE TBD (depends on 1st 5m close vs PM OR) — no trade markers",
        fontsize=10.0,
        loc="left",
    )
    fig.savefig(out_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    # stdout facts
    print(f"DAY {day.isoformat()}")
    print(f"PM_WINDOW {pm['yday']} 15:00:00 CT -> {day} 08:29:59 CT  src={pm['src']}")
    print(f"PM_H {pmh:.2f}  PM_L {pml:.2f}  PM_R {pmr:.2f}  n_1m={pm['n_1m']}  partial={pm['partial']}")
    print(
        f"FIRST5_OHLC O={first5['open']:.2f} H={first5['high']:.2f} "
        f"L={first5['low']:.2f} C={f5c:.2f}"
    )
    print(f"FIRST5_CLOSE_VS_PM {loc}")
    for k, px in long_fibs:
        print(f"FIB_UP_{k:g} {px:.2f}")
    for k, px in short_fibs:
        print(f"FIB_DN_{k:g} {px:.2f}")
    print(f"ENTRY none (rule TBD — waiting Luis)")
    print(f"EXIT none")
    print(f"NET n/a")
    print(f"CHART {out_path} bytes={out_path.stat().st_size}")
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", default=DEFAULT_DAY.isoformat())
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--scid", default=str(SCID_1M))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    day = date.fromisoformat(args.day)
    out = Path(args.out) if args.out else None
    build_chart(day, Path(args.db), Path(args.scid), out)


if __name__ == "__main__":
    main()
