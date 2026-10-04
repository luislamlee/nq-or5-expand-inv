#!/usr/bin/env python3
"""DIAGNOSTIC evaluator — first 3 RTH 5m candles vs VA70/POC + Premarket OR.

Day default: 2026-08-13 America/Chicago.

For each scope (bar1, bar2, bar3, c12, c123):
  OHLC, volume, volume_delta, VbP POC + VA70 VAL/VAH,
  close_vs_va (inside | below_VAL | above_VAH),
  close_vs_poc (above | below | at).

Premarket OR: prior TD 15:00 → 08:29 CT (pm_or_for_day / Sierra 1m).
c123 range vs PM OR + bar3 close bias (ASSUMED pending Luis OK):
  bar3 close > PMH → LONG bias
  bar3 close < PML → SHORT bias
  inside PM OR → INSIDE + note close vs PM mid / PMH / PML / c123 POC

Decision matrix NOT locked — scores/table + chart only. No trade engine.
NEW script only.
"""
from __future__ import annotations

import argparse
import json
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

from plot_daily_1h_ah_rth_dva import C_DN, C_UP, C_WICK, _naive
from plot_pmor_orhl5_day import (
    aggregate_5m_from_15s,
    draw_5m_candles,
    load_rth_15s,
    load_scid_1m_range,
    pm_or_for_day,
)
from luis_or_127_close205 import as_naive
from luis_or_127_delta import value_area
from luis_or_127_swing import C_OR_GOLD
from luis_vbp_or import BARS_PER_5M, TICK, pick_poc

SCID_1M = Path("/workspace/sierra/NQU26-CME scid compare.txt")
DB_1MO = Path("/workspace/sierra/NQU26-CME-15s-1mo.duckdb")
DB_EXT = Path("/workspace/sierra/NQU26-CME-15s-ext.duckdb")
CHART_DIR = Path("/workspace/sierra/or_127_close205_charts")
DEFAULT_DAY = date(2026, 8, 13)
OUT_STEM = "EVAL3_5M_VA_PMOR_{day}"
DPI = 130
FIGSIZE = (16.5, 12.5)

# bar index i -> (start_bn, end_bn) for 5m i=1,2,3
BAR_BN = {
    1: (1, BARS_PER_5M),
    2: (BARS_PER_5M + 1, 2 * BARS_PER_5M),
    3: (2 * BARS_PER_5M + 1, 3 * BARS_PER_5M),
}
BAR_LABEL = {
    1: "08:30–08:35",
    2: "08:35–08:40",
    3: "08:40–08:45",
}


def pick_db(preferred: Path | None = None) -> Path:
    if preferred is not None and preferred.exists():
        return preferred
    for p in (DB_1MO, DB_EXT):
        if not p.exists():
            continue
        con = duckdb.connect(str(p), read_only=True)
        try:
            n = con.execute(
                "SELECT count(*) FROM footprint_15s_by_price "
                f"WHERE trading_day = DATE '{DEFAULT_DAY.isoformat()}' "
                "AND rth_bar_number BETWEEN 1 AND 60"
            ).fetchone()[0]
        finally:
            con.close()
        if n and n > 100:
            return p
    raise SystemExit("no DuckDB with dense footprint for first 3×5m")


def load_footprint(day: date, db: Path) -> pd.DataFrame:
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
        [day, 3 * BARS_PER_5M],
    ).fetchdf()
    con.close()
    if fp.empty:
        return fp
    fp["trading_day"] = pd.to_datetime(fp["trading_day"]).dt.date
    return fp.reset_index(drop=True)


def _finite(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def close_vs_va(close: float, val: float, vah: float) -> str:
    if not _finite(val) or not _finite(vah):
        return "na"
    if close > vah + 1e-12:
        return "above_VAH"
    if close < val - 1e-12:
        return "below_VAL"
    return "inside"


def close_vs_poc(close: float, poc: float) -> str:
    if not _finite(poc):
        return "na"
    if abs(close - poc) < TICK * 0.5 + 1e-12:
        return "at"
    if close > poc + 1e-12:
        return "above"
    return "below"


def range_vs_pm(hi: float, lo: float, pmh: float, pml: float) -> str:
    """block entirely above PMH / below PML / overlapping / inside."""
    if lo > pmh + 1e-12:
        return "entirely_above_PMH"
    if hi < pml - 1e-12:
        return "entirely_below_PML"
    if lo >= pml - 1e-12 and hi <= pmh + 1e-12:
        return "inside"
    return "overlapping"


def close_vs_pm_levels(close: float, pmh: float, pml: float) -> str:
    if close > pmh + 1e-12:
        return "above_PMH"
    if close < pml - 1e-12:
        return "below_PML"
    return "inside_PM_OR"


def chunk_ohlc_delta(g15: pd.DataFrame, b0: int, b1: int) -> dict:
    chunk = g15[g15["rth_bar_number"].between(b0, b1)].copy()
    if chunk.empty:
        raise SystemExit(f"missing 15s bars {b0}–{b1}")
    chunk = chunk.sort_values("rth_bar_number")
    vol_col = "total_volume"
    dcol = "volume_delta" if "volume_delta" in chunk.columns else None
    if dcol is None and "bid_volume" in chunk.columns and "ask_volume" in chunk.columns:
        delta = int(chunk["ask_volume"].sum() - chunk["bid_volume"].sum())
    elif dcol is not None:
        delta = int(chunk[dcol].sum())
    else:
        delta = 0
    return {
        "open": float(chunk.iloc[0]["open"]),
        "high": float(chunk["high"].max()),
        "low": float(chunk["low"].min()),
        "close": float(chunk.iloc[-1]["close"]),
        "volume": float(chunk[vol_col].sum()),
        "volume_delta": delta,
        "t0": as_naive(chunk.iloc[0]["t"]),
        "t1": as_naive(chunk.iloc[-1]["t_end"]),
        "b0": b0,
        "b1": b1,
        "n_15s": int(len(chunk)),
    }


def vbp_va70(fp: pd.DataFrame, b0: int, b1: int, close: float) -> dict:
    fchunk = fp[(fp["rth_bar_number"] >= b0) & (fp["rth_bar_number"] <= b1)]
    if fchunk.empty:
        return {
            "poc": float("nan"),
            "poc_vol": 0.0,
            "val": float("nan"),
            "vah": float("nan"),
            "va_vol": 0.0,
            "fp_rows": 0,
        }
    grp = fchunk.groupby("price", sort=True)["total_volume"].sum()
    prices = grp.index.to_numpy(dtype=float)
    volumes = grp.to_numpy(dtype=float)
    poc, poc_vol = pick_poc(prices, volumes, close)
    vah, val, va_vol = value_area(prices, volumes, poc)
    return {
        "poc": float(poc),
        "poc_vol": float(poc_vol),
        "val": float(val),
        "vah": float(vah),
        "va_vol": float(va_vol),
        "fp_rows": int(len(fchunk)),
    }


def eval_scope(
    scope: str,
    g15: pd.DataFrame,
    fp: pd.DataFrame,
    b0: int,
    b1: int,
    clock: str,
) -> dict:
    oh = chunk_ohlc_delta(g15, b0, b1)
    va = vbp_va70(fp, b0, b1, oh["close"])
    c = oh["close"]
    row = {
        "scope": scope,
        "clock_ct": clock,
        "b0": b0,
        "b1": b1,
        "open": oh["open"],
        "high": oh["high"],
        "low": oh["low"],
        "close": c,
        "volume": oh["volume"],
        "volume_delta": oh["volume_delta"],
        "poc": va["poc"],
        "poc_vol": va["poc_vol"],
        "val": va["val"],
        "vah": va["vah"],
        "va_vol": va["va_vol"],
        "close_vs_va": close_vs_va(c, va["val"], va["vah"]),
        "close_vs_poc": close_vs_poc(c, va["poc"]),
        "n_15s": oh["n_15s"],
        "fp_rows": va["fp_rows"],
        "t0": oh["t0"].isoformat(sep=" "),
        "t1": oh["t1"].isoformat(sep=" "),
    }
    return row


def tentative_bias(bar3_close: float, pmh: float, pml: float, c123_poc: float) -> dict:
    """ASSUMED bias from bar3 close vs PM OR — pending Luis OK."""
    mid = 0.5 * (pmh + pml)
    loc = close_vs_pm_levels(bar3_close, pmh, pml)
    if bar3_close > pmh + 1e-12:
        return {
            "bias": "LONG",
            "rule": "bar3_close > PMH",
            "assumed": True,
            "close_vs_pm": loc,
            "pm_mid": mid,
            "note": f"bar3 C={bar3_close:.2f} above PMH={pmh:.2f}",
        }
    if bar3_close < pml - 1e-12:
        return {
            "bias": "SHORT",
            "rule": "bar3_close < PML",
            "assumed": True,
            "close_vs_pm": loc,
            "pm_mid": mid,
            "note": f"bar3 C={bar3_close:.2f} below PML={pml:.2f}",
        }
    # inside — fade/other side using close vs PM mid or combined POC
    vs_mid = "above_mid" if bar3_close >= mid else "below_mid"
    vs_poc = close_vs_poc(bar3_close, c123_poc) if _finite(c123_poc) else "na"
    if bar3_close >= mid:
        # close in upper half → fade short bias (ASSUMED)
        fade = "SHORT_FADE"
        why = "inside PM OR, close >= PM mid → fade short (ASSUMED)"
    else:
        fade = "LONG_FADE"
        why = "inside PM OR, close < PM mid → fade long (ASSUMED)"
    return {
        "bias": fade,
        "rule": "inside_PM_OR + close_vs_mid",
        "assumed": True,
        "close_vs_pm": loc,
        "pm_mid": mid,
        "close_vs_pm_mid": vs_mid,
        "close_vs_c123_poc": vs_poc,
        "note": (
            f"INSIDE PM OR | C={bar3_close:.2f} vs mid={mid:.2f} ({vs_mid}) "
            f"| vs c123 POC={c123_poc:.2f} ({vs_poc}) | {why}"
        ),
    }


def bw_label(ax, y: float, text: str, *, va: str = "center", fontsize: float = 7.5) -> None:
    ax.text(
        1.004,
        y,
        text,
        transform=ax.get_yaxis_transform(),
        color="#000000",
        fontsize=fontsize,
        va=va,
        ha="left",
        clip_on=False,
        fontweight="bold",
        bbox=dict(
            boxstyle="round,pad=0.15",
            facecolor="white",
            edgecolor="#333",
            lw=0.6,
            alpha=0.95,
        ),
    )


def analyze(day: date, db: Path, scid: Path) -> dict:
    pm = pm_or_for_day(day, db, scid)
    if pm is None:
        raise SystemExit(f"no real PM OR for {day}")
    g15 = load_rth_15s(day, db)
    if g15.empty:
        raise SystemExit(f"no RTH 15s for {day}")
    fp = load_footprint(day, db)
    if fp.empty:
        raise SystemExit(f"no footprint for {day}")

    rows = []
    # single bars
    for i in (1, 2, 3):
        b0, b1 = BAR_BN[i]
        rows.append(
            eval_scope(f"bar{i}", g15, fp, b0, b1, BAR_LABEL[i])
        )
    # combined
    rows.append(
        eval_scope("c12", g15, fp, 1, 2 * BARS_PER_5M, "08:30–08:40")
    )
    rows.append(
        eval_scope("c123", g15, fp, 1, 3 * BARS_PER_5M, "08:30–08:45")
    )

    by = {r["scope"]: r for r in rows}
    c123 = by["c123"]
    bar3 = by["bar3"]
    pmh, pml = float(pm["pm_high"]), float(pm["pm_low"])
    rng_vs = range_vs_pm(c123["high"], c123["low"], pmh, pml)
    c_vs_pm = close_vs_pm_levels(c123["close"], pmh, pml)
    bar3_vs_pm = close_vs_pm_levels(bar3["close"], pmh, pml)
    bias = tentative_bias(bar3["close"], pmh, pml, c123["poc"])

    return {
        "day": day,
        "db": str(db),
        "scid": str(scid),
        "pm": pm,
        "rows": rows,
        "by": by,
        "g15": g15,
        "c123_range_vs_pm": rng_vs,
        "c123_close_vs_pm": c_vs_pm,
        "bar3_close_vs_pm": bar3_vs_pm,
        "bias": bias,
    }


def write_csv_json(facts: dict, stem: Path) -> tuple[Path, Path]:
    day = facts["day"]
    pm = facts["pm"]
    pmh, pml = float(pm["pm_high"]), float(pm["pm_low"])
    bias = facts["bias"]
    out_rows = []
    for r in facts["rows"]:
        out_rows.append(
            {
                "day": day.isoformat(),
                "scope": r["scope"],
                "clock_ct": r["clock_ct"],
                "open": round(r["open"], 4),
                "high": round(r["high"], 4),
                "low": round(r["low"], 4),
                "close": round(r["close"], 4),
                "volume": round(r["volume"], 2),
                "volume_delta": int(r["volume_delta"]),
                "poc": round(r["poc"], 4) if _finite(r["poc"]) else None,
                "poc_vol": round(r["poc_vol"], 2),
                "val": round(r["val"], 4) if _finite(r["val"]) else None,
                "vah": round(r["vah"], 4) if _finite(r["vah"]) else None,
                "va_vol": round(r["va_vol"], 2),
                "close_vs_va": r["close_vs_va"],
                "close_vs_poc": r["close_vs_poc"],
                "pm_high": pmh,
                "pm_low": pml,
                "pm_r": round(pmh - pml, 4),
                "c123_range_vs_pm": facts["c123_range_vs_pm"],
                "c123_close_vs_pm": facts["c123_close_vs_pm"],
                "bar3_close_vs_pm": facts["bar3_close_vs_pm"],
                "assumed_bias": bias["bias"],
                "assumed_bias_rule": bias["rule"],
                "assumed_bias_note": bias["note"],
                "n_15s": r["n_15s"],
                "fp_rows": r["fp_rows"],
                "t0": r["t0"],
                "t1": r["t1"],
            }
        )
    csv_path = stem.with_suffix(".csv")
    json_path = stem.with_suffix(".json")
    pd.DataFrame(out_rows).to_csv(csv_path, index=False)
    payload = {
        "day": day.isoformat(),
        "title": "diagnostic evaluator — decision TBD",
        "pm_or": {
            "yday": str(pm["yday"]),
            "pm_high": pmh,
            "pm_low": pml,
            "pm_r": pmh - pml,
            "n_1m": pm["n_1m"],
            "partial": pm["partial"],
        },
        "c123_range_vs_pm": facts["c123_range_vs_pm"],
        "c123_close_vs_pm": facts["c123_close_vs_pm"],
        "bar3_close_vs_pm": facts["bar3_close_vs_pm"],
        "assumed_bias": bias,
        "scopes": out_rows,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    return csv_path, json_path


def build_chart(facts: dict, out_path: Path) -> Path:
    day = facts["day"]
    pm = facts["pm"]
    by = facts["by"]
    g15 = facts["g15"]
    bias = facts["bias"]
    pmh, pml = float(pm["pm_high"]), float(pm["pm_low"])
    pmr = pmh - pml
    mid = 0.5 * (pmh + pml)
    c123 = by["c123"]
    bar3 = by["bar3"]

    five = aggregate_5m_from_15s(g15)
    t0 = datetime.combine(day, dtime(8, 30))
    t_right = datetime.combine(day, dtime(10, 0))
    t_0845 = datetime.combine(day, dtime(8, 45))

    fig = plt.figure(figsize=FIGSIZE, dpi=DPI, facecolor="white")
    gs = fig.add_gridspec(3, 1, height_ratios=[3.6, 0.7, 1.55], hspace=0.12)
    ax = fig.add_subplot(gs[0, 0])
    axv = fig.add_subplot(gs[1, 0], sharex=ax)
    axt = fig.add_subplot(gs[2, 0])
    ax.set_facecolor("white")
    axv.set_facecolor("white")
    axt.set_facecolor("white")

    vis = g15[(g15["t"] >= t0) & (g15["t"] <= t_right)].copy()
    ax.fill_between(
        vis["t"],
        vis["low"],
        vis["high"],
        step="post",
        color="#6b6b6b",
        alpha=0.25,
        lw=0,
        zorder=1,
        label="15s Hi/Lo",
    )
    ax.step(vis["t"], vis["close"], where="post", color="0.08", lw=1.1, zorder=3.2)

    draw_5m_candles(ax, five, t0, t_right, emph_first=False)

    # Emphasize first three 5m candles + labels
    w = 4.2 / (24 * 60)
    for i, scope in enumerate((1, 2, 3), start=1):
        end_bn = i * BARS_PER_5M
        sub = five[five["end_bn"] == end_bn] if not five.empty else five
        if sub.empty:
            continue
        r = sub.iloc[0]
        o, h, lo, c = r["open"], r["high"], r["low"], r["close"]
        body_c = C_UP if c >= o else C_DN
        xm = mdates.date2num(r["t"]) + w / 2
        ax.plot([xm, xm], [lo, h], color="#0f172a", lw=1.5, zorder=5.5)
        y0 = min(o, c)
        ht = max(abs(c - o), TICK * 0.5)
        ax.add_patch(
            Rectangle(
                (mdates.date2num(r["t"]), y0),
                w,
                ht,
                facecolor=body_c,
                edgecolor="#0f172a",
                lw=1.8 if i == 3 else 1.3,
                alpha=0.95,
                zorder=5.6,
            )
        )
        row = by[f"bar{i}"]
        tag = (
            f"#{i}\nC={c:.0f}\n"
            f"VA:{row['close_vs_va']}\n"
            f"POC:{row['close_vs_poc']}"
        )
        ax.annotate(
            tag,
            xy=(xm, h),
            xytext=(0, 14),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=7.0,
            fontweight="bold",
            color="#000",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor="#333", lw=0.7, alpha=0.95),
            zorder=12,
        )

    # PM OR band
    ax.axhspan(pml, pmh, facecolor="#ffcc00", alpha=0.20, zorder=0.7, label="PM OR")
    ax.axhline(pmh, color=C_OR_GOLD, lw=1.7, zorder=5)
    ax.axhline(pml, color=C_OR_GOLD, lw=1.7, zorder=5)
    ax.axhline(mid, color="#ca8a04", lw=1.0, ls=":", zorder=4.5, alpha=0.9)
    bw_label(ax, pmh, f"PMH  {pmh:.2f}", va="bottom")
    bw_label(ax, pml, f"PML  {pml:.2f}", va="top")
    bw_label(ax, mid, f"PMmid {mid:.2f}", va="center", fontsize=6.8)

    # c123 combined VA/POC overlays (dashed purple/teal)
    if _finite(c123["val"]) and _finite(c123["vah"]):
        x0 = mdates.date2num(t0)
        x1 = mdates.date2num(t_0845)
        ax.add_patch(
            Rectangle(
                (x0, c123["val"]),
                x1 - x0,
                c123["vah"] - c123["val"],
                facecolor="#7c3aed",
                edgecolor="#5b21b6",
                lw=1.2,
                alpha=0.18,
                zorder=2.0,
                label="c123 VA70",
            )
        )
        ax.axhline(c123["val"], color="#7c3aed", lw=1.1, ls="--", zorder=4.2)
        ax.axhline(c123["vah"], color="#7c3aed", lw=1.1, ls="--", zorder=4.2)
        bw_label(ax, c123["vah"], f"c123 VAH {c123['vah']:.2f}", va="bottom", fontsize=6.8)
        bw_label(ax, c123["val"], f"c123 VAL {c123['val']:.2f}", va="top", fontsize=6.8)
    if _finite(c123["poc"]):
        ax.axhline(c123["poc"], color="#6a3d9a", lw=1.6, ls="-.", zorder=5)
        bw_label(ax, c123["poc"], f"c123 POC {c123['poc']:.2f}", va="center", fontsize=6.8)

    # bar3 close marker (bias signal)
    t_b3c = datetime.combine(day, dtime(8, 45)) - timedelta(seconds=2)
    b3c = bar3["close"]
    bias_col = {
        "LONG": "#16a34a",
        "SHORT": "#dc2626",
        "LONG_FADE": "#2563eb",
        "SHORT_FADE": "#c026d3",
    }.get(bias["bias"], "#ca8a04")
    ax.scatter(
        [t_b3c],
        [b3c],
        marker="D",
        s=110,
        color=bias_col,
        edgecolors="#0f172a",
        linewidths=0.9,
        zorder=14,
        label=f"bar3 close → {bias['bias']} (ASSUMED)",
    )
    ax.annotate(
        f"bar3 C {b3c:.2f}\n{facts['bar3_close_vs_pm']}\n"
        f"ASSUMED bias: {bias['bias']}",
        xy=(t_b3c, b3c),
        xytext=(36, 36 if bias["bias"] in ("LONG", "LONG_FADE") else -48),
        textcoords="offset points",
        fontsize=8.5,
        fontweight="bold",
        color=bias_col,
        arrowprops=dict(arrowstyle="->", color=bias_col, lw=1.3),
        bbox=dict(boxstyle="round,pad=0.25", facecolor="white", edgecolor=bias_col, lw=1.0, alpha=0.96),
        zorder=15,
    )

    # c123 range box outline
    ax.add_patch(
        Rectangle(
            (mdates.date2num(t0), c123["low"]),
            mdates.date2num(t_0845) - mdates.date2num(t0),
            c123["high"] - c123["low"],
            facecolor="none",
            edgecolor="#0f172a",
            lw=1.4,
            ls=":",
            alpha=0.7,
            zorder=3.0,
            label=f"c123 range ({facts['c123_range_vs_pm']})",
        )
    )

    ax.axvline(t0, color="#334155", lw=1.0, alpha=0.7)
    ax.axvline(t_0845, color="#64748b", lw=0.9, ls=":", alpha=0.85)

    # volume pane
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
    axv.set_ylabel("5m vol", fontsize=8)
    axv.grid(True, alpha=0.25)

    # candle ylim from morning candles + PM + c123 VA
    ys = [
        float(vis["low"].min()),
        float(vis["high"].max()),
        pml,
        pmh,
        c123["low"],
        c123["high"],
        b3c,
    ]
    for k in ("val", "vah", "poc"):
        if _finite(c123[k]):
            ys.append(float(c123[k]))
    # focus around action — clip extreme fib-less pad
    y_lo, y_hi = min(ys), max(ys)
    pad = max(6.0, (y_hi - y_lo) * 0.08)
    ax.set_ylim(y_lo - pad, y_hi + pad)
    ax.set_xlim(t0 - timedelta(minutes=2), t_right)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.grid(True, alpha=0.28)
    ax.legend(loc="upper left", fontsize=6.8, framealpha=0.92, ncol=2)
    ax.set_ylabel("NQ")

    ax.set_title(
        f"NQU26  {day.isoformat()}  ·  DIAGNOSTIC evaluator — decision TBD\n"
        f"c123 vs PM OR: range={facts['c123_range_vs_pm']}  "
        f"c123 close={facts['c123_close_vs_pm']}  |  "
        f"bar3 close bias signal={facts['bar3_close_vs_pm']}  →  "
        f"ASSUMED {bias['bias']} ({bias['rule']})\n"
        f"PM: {pm['yday']} 15:00→{day} 08:29  H={pmh:.2f} L={pml:.2f} R={pmr:.2f}  "
        f"(n_1m={pm['n_1m']})  ·  NO locked decision matrix",
        fontsize=9.5,
        loc="left",
        color="#111",
    )

    # lower text panel — score table
    axt.axis("off")
    headers = [
        "scope",
        "clock",
        "O",
        "H",
        "L",
        "C",
        "Δ",
        "vol",
        "POC",
        "VAL",
        "VAH",
        "C↔VA",
        "C↔POC",
    ]
    cell = []
    for r in facts["rows"]:
        cell.append(
            [
                r["scope"],
                r["clock_ct"],
                f"{r['open']:.2f}",
                f"{r['high']:.2f}",
                f"{r['low']:.2f}",
                f"{r['close']:.2f}",
                f"{r['volume_delta']:+d}",
                f"{r['volume']:.0f}",
                f"{r['poc']:.2f}" if _finite(r["poc"]) else "nan",
                f"{r['val']:.2f}" if _finite(r["val"]) else "nan",
                f"{r['vah']:.2f}" if _finite(r["vah"]) else "nan",
                r["close_vs_va"],
                r["close_vs_poc"],
            ]
        )
    tbl = axt.table(
        cellText=cell,
        colLabels=headers,
        loc="center",
        cellLoc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(7.2)
    tbl.scale(1.0, 1.35)
    for (row_i, col_i), cell_obj in tbl.get_celld().items():
        cell_obj.set_edgecolor("#cbd5e1")
        if row_i == 0:
            cell_obj.set_facecolor("#e2e8f0")
            cell_obj.set_text_props(fontweight="bold", color="#000")
        else:
            scope = cell[row_i - 1][0]
            if scope == "c123":
                cell_obj.set_facecolor("#fef3c7")
            elif scope == "bar3":
                cell_obj.set_facecolor("#ecfccb")
            else:
                cell_obj.set_facecolor("white")
            cell_obj.set_text_props(color="#000")

    footer = (
        f"c123 range vs PM OR = {facts['c123_range_vs_pm']}  |  "
        f"c123 close vs PM = {facts['c123_close_vs_pm']}  |  "
        f"ASSUMED bias (bar3 C): {bias['bias']} — {bias['note']}  "
        f"[pending Luis OK — not a trade]"
    )
    axt.text(
        0.0,
        -0.08,
        footer,
        transform=axt.transAxes,
        fontsize=8.0,
        fontweight="bold",
        color="#000",
        va="top",
        ha="left",
        wrap=True,
        bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor=bias_col, lw=1.2),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path


def print_scorecard(facts: dict, chart: Path, csv_path: Path, json_path: Path) -> None:
    day = facts["day"]
    pm = facts["pm"]
    bias = facts["bias"]
    pmh, pml = float(pm["pm_high"]), float(pm["pm_low"])
    print("=" * 72)
    print(f"DIAGNOSTIC evaluator — decision TBD  ·  {day.isoformat()}")
    print("=" * 72)
    print(
        f"PM OR  {pm['yday']} 15:00 → {day} 08:29 CT  "
        f"PMH={pmh:.2f}  PML={pml:.2f}  R={pmh - pml:.2f}  n_1m={pm['n_1m']}"
    )
    print(
        f"c123 range vs PM OR: {facts['c123_range_vs_pm']}  |  "
        f"c123 close vs PM: {facts['c123_close_vs_pm']}  |  "
        f"bar3 close vs PM: {facts['bar3_close_vs_pm']}"
    )
    print(
        f"ASSUMED bias (pending Luis OK): {bias['bias']}  "
        f"[{bias['rule']}]  — {bias['note']}"
    )
    print("-" * 72)
    hdr = (
        f"{'scope':6} {'clock':13} {'O':>9} {'H':>9} {'L':>9} {'C':>9} "
        f"{'Δ':>7} {'vol':>7} {'POC':>9} {'VAL':>9} {'VAH':>9} "
        f"{'C↔VA':12} {'C↔POC':7}"
    )
    print(hdr)
    for r in facts["rows"]:
        print(
            f"{r['scope']:6} {r['clock_ct']:13} "
            f"{r['open']:9.2f} {r['high']:9.2f} {r['low']:9.2f} {r['close']:9.2f} "
            f"{r['volume_delta']:+7d} {r['volume']:7.0f} "
            f"{r['poc']:9.2f} {r['val']:9.2f} {r['vah']:9.2f} "
            f"{r['close_vs_va']:12} {r['close_vs_poc']:7}"
        )
    print("-" * 72)
    print(f"CHART  {chart}  bytes={chart.stat().st_size}")
    print(f"CSV    {csv_path}")
    print(f"JSON   {json_path}")
    print("NO long/short trade engine — diagnostic scores only.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", default=DEFAULT_DAY.isoformat())
    ap.add_argument("--db", default=None)
    ap.add_argument("--scid", default=str(SCID_1M))
    ap.add_argument("--out-dir", default=str(CHART_DIR))
    args = ap.parse_args(argv)
    day = date.fromisoformat(args.day)
    db = pick_db(Path(args.db) if args.db else None)
    scid = Path(args.scid)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / OUT_STEM.format(day=day.isoformat())

    facts = analyze(day, db, scid)
    csv_path, json_path = write_csv_json(facts, stem)
    chart = build_chart(facts, stem.with_suffix(".png"))
    print_scorecard(facts, chart, csv_path, json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
