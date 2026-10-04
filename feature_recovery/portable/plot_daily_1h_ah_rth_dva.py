#!/usr/bin/env python3
"""Display-only: one daily NQ chart — 1-hour OHLC, AH vs RTH, developing VA overlays.

Day default: 2026-08-13 America/Chicago.

Session split (Luis display):
  AH / Globex: prior trading day 15:00 CT → chart day 07:59:59 CT
  RTH (display): 08:00 → 15:00 CT  (includes 08:00–08:30; cash/fut open still 08:30)

Bars: aggregate to clock 1-hour OHLC (+ volume, volume_delta).
Overlays per period independently: dVWAP (orange), dPOC (fuchsia), dVA 70%/90%.
At period end, freeze ending VA H/L (and end POC/VWAP) and extend as dashed refs.

Data:
  - RTH 08:30–15:00 from DuckDB bars_15s + footprint_15s_by_price (1mo).
  - AH + RTH 08:00–08:30 from Sierra 1m export
    `/workspace/sierra/NQU26-CME scid compare.txt` (times are Eastern → CT = ET−1 in Aug).

NEW script only — does not edit locked engines.
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

# Historical machine-specific import bootstrap removed; see source manifest.
# Historical machine-specific import bootstrap removed; see source manifest.

# Historical machine-specific import bootstrap removed; see source manifest.


import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd

DEFAULT_DAY = date(2026, 8, 13)
DB_PATH = Path("/workspace/sierra/NQU26-CME-15s-1mo.duckdb")
SCID_1M = Path("/workspace/sierra/NQU26-CME scid compare.txt")
CHART_DIR = Path("/workspace/sierra/or_127_close205_charts")
OUT_NAME = "DAILY_1H_AH_RTH_{day}.png"
TICK = 0.25
FIGSIZE = (28.0, 17.0)  # taller vertical space for AH/RTH + overlays
DPI = 150

C_UP = "#2ca02c"
C_DN = "#d62728"
C_WICK = "#4a4a4a"
C_DPOC = "#FF00CC"
C_DVWAP = "#ff7f0e"
C_VA = "#5b8db8"
C_VA_EDGE = "#1f4e79"
C_VA90 = "#8eb8d8"
C_VA90_EDGE = "#3a6f9a"
C_AH_BG = "#eef2f6"
C_RTH_BG = "#fffaf0"
C_DIV = "#334155"
C_AH_FREEZE = "#64748b"
C_RTH_FREEZE = "#1f4e79"


def _naive(x) -> datetime:
    t = pd.Timestamp(x)
    if t.tzinfo is not None:
        t = t.tz_localize(None)
    return t.to_pydatetime().replace(microsecond=0)


def round_tick(px: float, tick: float = TICK) -> float:
    q = px / tick
    if q >= 0:
        return float(math.floor(q + 0.5) * tick)
    return float(math.ceil(q - 0.5) * tick)


def prev_trading_day(day: date, db: Path = DB_PATH) -> date | None:
    con = duckdb.connect(str(db), read_only=True)
    row = con.execute(
        "SELECT MAX(trading_day) FROM bars_15s WHERE trading_day < ?",
        [day],
    ).fetchone()
    con.close()
    if row is None or row[0] is None:
        return None
    d = row[0]
    return d if isinstance(d, date) else date.fromisoformat(str(d))


def value_area(
    prices: np.ndarray, volumes: np.ndarray, poc: float, pct: float = 0.70
) -> tuple[float, float, float]:
    """Contiguous VA from POC (greater adjacent vol first; ties expand both)."""
    nan = float("nan")
    if len(prices) == 0 or not np.isfinite(poc):
        return nan, nan, 0.0
    total = float(volumes.sum())
    if total <= 0:
        return nan, nan, 0.0
    order = np.argsort(prices)
    px = prices[order].astype(float)
    vol = volumes[order].astype(float)
    hits = np.where(np.abs(px - poc) < 1e-9)[0]
    if len(hits) == 0:
        # snap POC to nearest present tick
        i0 = int(np.argmin(np.abs(px - poc)))
    else:
        i0 = int(hits[0])
    lo = hi = i0
    va_vol = float(vol[i0])
    target = pct * total
    n = len(px)
    while va_vol + 1e-12 < target:
        up = float(vol[hi + 1]) if hi + 1 < n else None
        dn = float(vol[lo - 1]) if lo - 1 >= 0 else None
        if up is None and dn is None:
            break
        if up is None:
            lo -= 1
            va_vol += dn
        elif dn is None:
            hi += 1
            va_vol += up
        elif up > dn:
            hi += 1
            va_vol += up
        elif dn > up:
            lo -= 1
            va_vol += dn
        else:
            hi += 1
            lo -= 1
            va_vol += up + dn
    return float(px[hi]), float(px[lo]), va_vol


def poc_from_profile(agg: dict[float, float]) -> float:
    if not agg:
        return float("nan")
    max_v = max(agg.values())
    cands = sorted(px for px, v in agg.items() if abs(v - max_v) < 1e-9)
    if len(cands) == 1:
        return cands[0]
    mid = sum(px * v for px, v in agg.items()) / sum(agg.values())
    cands.sort(key=lambda p: (abs(p - mid), p))
    return cands[0]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_scid_1m_chicago(path: Path, d0: date, d1: date) -> pd.DataFrame:
    """Load Sierra 1m CSV; file timestamps are Eastern → convert to Chicago (ET−1 in Aug)."""
    want = {d0, d1}
    rows: list[dict] = []
    with path.open() as f:
        f.readline()
        for line in f:
            # Fast date gate: "2026-8-12" or "2026-08-12"
            if not (line.startswith("2026-8-") or line.startswith("2026-08-")):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 13:
                continue
            ds = parts[0].replace("2026-8-", "2026-08-")
            try:
                d = date.fromisoformat(ds)
            except ValueError:
                continue
            if d not in want:
                continue
            t_et = datetime.fromisoformat(f"{ds}T{parts[1].split('.')[0]}")
            t_ct = t_et - timedelta(hours=1)  # EDT→CDT Aug
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
                    "src": "scid_1m",
                }
            )
    if not rows:
        return pd.DataFrame()
    g = pd.DataFrame(rows).sort_values("t").drop_duplicates("t").reset_index(drop=True)
    return g


def load_rth_15s(day: date, db: Path = DB_PATH) -> pd.DataFrame:
    con = duckdb.connect(str(db), read_only=True)
    g = con.execute(
        """
        SELECT
            bar_start_chicago, bar_end_chicago,
            open, high, low, close,
            total_volume, volume_delta, bid_volume, ask_volume,
            rth_bar_number, vwap_numerator, bar_vwap
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
    g = g[(g["t"].map(lambda x: x.time() >= dtime(8, 30))) & (g["t"].map(lambda x: x.time() < dtime(15, 0)))]
    g = g.sort_values("t").reset_index(drop=True)
    g["src"] = "duckdb_15s"
    return g


def load_footprint_rth_safe(day: date, db: Path = DB_PATH) -> pd.DataFrame:
    con = duckdb.connect(str(db), read_only=True)
    fp = con.execute(
        """
        SELECT bar_start_chicago, price, total_volume, bid_volume, ask_volume, volume_delta
        FROM footprint_15s_by_price
        WHERE trading_day = ?
          AND rth_bar_number IS NOT NULL
        ORDER BY bar_start_chicago, price
        """,
        [day],
    ).fetchdf()
    con.close()
    if fp.empty:
        return fp
    fp["t"] = [_naive(x) for x in fp["bar_start_chicago"]]
    fp = fp[(fp["t"].map(lambda x: x.time() >= dtime(8, 30))) & (fp["t"].map(lambda x: x.time() < dtime(15, 0)))]
    return fp.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Aggregate 1h + developing profiles
# ---------------------------------------------------------------------------

def hour_floor(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def aggregate_1h(bars: pd.DataFrame) -> pd.DataFrame:
    """Clock-hour OHLC from rows with columns t, open, high, low, close, total_volume, volume_delta."""
    if bars is None or bars.empty:
        return pd.DataFrame(
            columns=[
                "t", "t_end", "open", "high", "low", "close",
                "total_volume", "volume_delta", "n_src",
            ]
        )
    g = bars.copy()
    g["hour"] = g["t"].map(hour_floor)
    rows = []
    for h, sub in g.groupby("hour", sort=True):
        sub = sub.sort_values("t")
        rows.append(
            {
                "t": h,
                "t_end": h + timedelta(hours=1),
                "open": float(sub.iloc[0]["open"]),
                "high": float(sub["high"].max()),
                "low": float(sub["low"].min()),
                "close": float(sub.iloc[-1]["close"]),
                "total_volume": int(sub["total_volume"].sum()),
                "volume_delta": int(sub["volume_delta"].fillna(0).sum()),
                "n_src": int(len(sub)),
            }
        )
    return pd.DataFrame(rows)


def developing_from_close_bars(
    bars: pd.DataFrame, sample_every: int = 1
) -> pd.DataFrame:
    """Developing VWAP/POC/VA70/VA90 from 1m (or any) bars using close@tick × volume."""
    if bars is None or bars.empty:
        return pd.DataFrame()
    bars = bars.sort_values("t").reset_index(drop=True)
    agg: dict[float, float] = defaultdict(float)
    cum_pv = 0.0
    cum_v = 0.0
    out = []
    for i, r in bars.iterrows():
        px = round_tick(float(r["close"]))
        v = float(r["total_volume"])
        if v > 0 and np.isfinite(px):
            agg[px] += v
            # VWAP via typical price when available
            typ = (float(r["high"]) + float(r["low"]) + float(r["close"])) / 3.0
            cum_pv += typ * v
            cum_v += v
        if (i + 1) % sample_every != 0 and i != len(bars) - 1:
            continue
        if cum_v <= 0:
            continue
        prices = np.array(sorted(agg.keys()), dtype=float)
        vols = np.array([agg[p] for p in prices], dtype=float)
        poc = poc_from_profile(dict(zip(prices.tolist(), vols.tolist())))
        vah70, val70, _ = value_area(prices, vols, poc, 0.70)
        vah90, val90, _ = value_area(prices, vols, poc, 0.90)
        out.append(
            {
                "t": _naive(r["t"]),
                "dvwap": cum_pv / cum_v,
                "dpoc": poc,
                "dvah": vah70,
                "dval": val70,
                "dvah90": vah90,
                "dval90": val90,
                "cum_vol": cum_v,
            }
        )
    return pd.DataFrame(out)


def developing_rth_hybrid(
    pre_1m: pd.DataFrame,
    bars15: pd.DataFrame,
    fp: pd.DataFrame,
) -> pd.DataFrame:
    """RTH developing from 08:00: 1m close-vol for 08:00–08:30, then footprint 15s."""
    agg: dict[float, float] = defaultdict(float)
    cum_pv = 0.0
    cum_v = 0.0
    out = []

    def _emit(t: datetime):
        if cum_v <= 0:
            return
        prices = np.array(sorted(agg.keys()), dtype=float)
        vols = np.array([agg[p] for p in prices], dtype=float)
        poc = poc_from_profile({float(p): float(v) for p, v in zip(prices, vols)})
        vah70, val70, _ = value_area(prices, vols, poc, 0.70)
        vah90, val90, _ = value_area(prices, vols, poc, 0.90)
        out.append(
            {
                "t": t,
                "dvwap": cum_pv / cum_v,
                "dpoc": poc,
                "dvah": vah70,
                "dval": val70,
                "dvah90": vah90,
                "dval90": val90,
                "cum_vol": cum_v,
            }
        )

    # Phase A: 08:00–08:30 from 1m
    if pre_1m is not None and not pre_1m.empty:
        for _, r in pre_1m.sort_values("t").iterrows():
            px = round_tick(float(r["close"]))
            v = float(r["total_volume"])
            if v > 0 and np.isfinite(px):
                agg[px] += v
                typ = (float(r["high"]) + float(r["low"]) + float(r["close"])) / 3.0
                cum_pv += typ * v
                cum_v += v
            _emit(_naive(r["t"]))

    # Phase B: footprint by 15s bar (preferred); fallback to 15s close
    fp_by_t: dict[datetime, list[tuple[float, float]]] = defaultdict(list)
    if fp is not None and not fp.empty:
        for _, r in fp.iterrows():
            fp_by_t[_naive(r["t"])].append((float(r["price"]), float(r["total_volume"])))

    if bars15 is not None and not bars15.empty:
        for _, r in bars15.sort_values("t").iterrows():
            t = _naive(r["t"])
            contrib = fp_by_t.get(t)
            if contrib:
                for px, v in contrib:
                    if v > 0 and np.isfinite(px):
                        agg[round_tick(px)] += v
                # VWAP: prefer duckdb vwap_numerator / volume if present
                v = float(r["total_volume"])
                if v > 0:
                    if "vwap_numerator" in r.index and pd.notna(r["vwap_numerator"]):
                        cum_pv += float(r["vwap_numerator"])
                    else:
                        typ = (float(r["high"]) + float(r["low"]) + float(r["close"])) / 3.0
                        cum_pv += typ * v
                    cum_v += v
            else:
                px = round_tick(float(r["close"]))
                v = float(r["total_volume"])
                if v > 0 and np.isfinite(px):
                    agg[px] += v
                    typ = (float(r["high"]) + float(r["low"]) + float(r["close"])) / 3.0
                    cum_pv += typ * v
                    cum_v += v
            # emit every 15s is dense for 1h chart; subsample to ~1m (every 4) + last
            bn = int(r.get("rth_bar_number") or 0)
            if bn % 4 == 0 or bn == int(bars15["rth_bar_number"].iloc[-1]):
                _emit(t)

    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def draw_1h_candles(ax, df: pd.DataFrame, alpha: float = 0.95) -> None:
    if df is None or df.empty:
        return
    bar_w = (3600.0 / 86400.0) * 0.62
    for _, r in df.iterrows():
        t = _naive(r["t"])
        x = mdates.date2num(t) + (3600.0 / 86400.0) * 0.5
        o, h, lo, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        color = C_UP if c >= o else C_DN
        ax.plot([x, x], [lo, h], color=C_WICK, linewidth=1.05, solid_capstyle="round", zorder=3)
        body_lo, body_hi = min(o, c), max(o, c)
        body_h = max(body_hi - body_lo, 0.25)
        ax.add_patch(
            Rectangle(
                (x - bar_w / 2, body_lo),
                bar_w,
                body_h,
                facecolor=color,
                edgecolor=color,
                linewidth=0.4,
                alpha=alpha,
                zorder=4,
            )
        )


def plot_developing(ax, dt: pd.DataFrame, label_prefix: str) -> None:
    if dt is None or dt.empty:
        return
    ax.fill_between(
        dt["t"], dt["dval90"], dt["dvah90"],
        color=C_VA90, alpha=0.08, lw=0, zorder=1.5, label=f"{label_prefix} dVA 90%",
    )
    ax.plot(dt["t"], dt["dvah90"], color=C_VA90_EDGE, lw=1.2, ls="-", drawstyle="steps-post", zorder=4.5)
    ax.plot(dt["t"], dt["dval90"], color=C_VA90_EDGE, lw=1.2, ls="-", drawstyle="steps-post", zorder=4.5)
    ax.fill_between(
        dt["t"], dt["dval"], dt["dvah"],
        color=C_VA, alpha=0.16, lw=0, zorder=2.0, label=f"{label_prefix} dVA 70%",
    )
    ax.plot(dt["t"], dt["dvah"], color=C_VA_EDGE, lw=1.15, drawstyle="steps-post", zorder=5.0)
    ax.plot(dt["t"], dt["dval"], color=C_VA_EDGE, lw=1.15, drawstyle="steps-post", zorder=5.0)
    ax.plot(
        dt["t"], dt["dvwap"], color=C_DVWAP, lw=2.4, drawstyle="steps-post",
        zorder=6.0, label=f"{label_prefix} dVWAP",
    )
    ax.plot(
        dt["t"], dt["dpoc"], color=C_DPOC, lw=1.7, drawstyle="steps-post",
        zorder=6.1, label=f"{label_prefix} dPOC",
    )


def extend_levels(
    ax,
    levels: dict[str, float],
    x0: datetime,
    x1: datetime,
    color: str,
    label_tag: str,
    lw: float = 1.85,
) -> None:
    """Horizontal dashed freeze from x0→x1; label VA H/L thicker."""
    if x1 <= x0:
        return
    for key, ls, width, lab in (
        ("vah", "--", lw + 0.55, f"{label_tag} VA H"),
        ("val", "--", lw + 0.55, f"{label_tag} VA L"),
        ("poc", ":", lw - 0.2, f"{label_tag} POC"),
        ("vwap", ":", lw - 0.2, f"{label_tag} VWAP"),
    ):
        px = levels.get(key)
        if px is None or not np.isfinite(px):
            continue
        ax.plot(
            [x0, x1], [px, px],
            color=color, lw=width, ls=ls, zorder=5.5, alpha=0.92,
            label=lab if key in ("vah", "val") else None,
        )
        if key in ("vah", "val"):
            ax.text(
                mdates.date2num(x1),
                px,
                f" {lab} {px:.2f}",
                color=color,
                fontsize=7.2,
                fontweight="bold",
                va="center",
                ha="left",
                zorder=7,
                clip_on=False,
            )


def ending_levels(dt: pd.DataFrame) -> dict[str, float]:
    if dt is None or dt.empty:
        return {}
    r = dt.iloc[-1]
    return {
        "vah": float(r["dvah"]),
        "val": float(r["dval"]),
        "vah90": float(r["dvah90"]),
        "val90": float(r["dval90"]),
        "poc": float(r["dpoc"]),
        "vwap": float(r["dvwap"]),
    }


def draw_volume_panel(ax, ah: pd.DataFrame, rth: pd.DataFrame) -> None:
    bar_w = (3600.0 / 86400.0) * 0.62
    for df, alpha in ((ah, 0.55), (rth, 0.75)):
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            t = _naive(r["t"])
            x = mdates.date2num(t) + (3600.0 / 86400.0) * 0.5
            vol = float(r["total_volume"])
            dlt = float(r.get("volume_delta") or 0)
            color = C_UP if dlt >= 0 else C_DN
            ax.bar(
                x, vol, width=bar_w, color=color, alpha=alpha, align="center",
                edgecolor="none", zorder=2,
            )
    ax.set_ylabel("1h volume", fontsize=8)
    ax.tick_params(labelsize=7)


def build_chart(day: date, db: Path = DB_PATH, scid: Path = SCID_1M, out: Path | None = None) -> Path:
    yday = prev_trading_day(day, db)
    if yday is None:
        raise SystemExit(f"no prior trading_day before {day}")

    ah_start = datetime.combine(yday, dtime(15, 0))
    ah_end = datetime.combine(day, dtime(8, 0))  # exclusive
    rth_start = datetime.combine(day, dtime(8, 0))
    rth_end = datetime.combine(day, dtime(15, 0))  # exclusive for bars; close stamp 15:00

    scid_1m = load_scid_1m_chicago(scid, yday, day)
    if scid_1m.empty:
        raise SystemExit(f"no scid 1m rows for {yday}/{day} in {scid}")

    ah_1m = scid_1m[(scid_1m["t"] >= ah_start) & (scid_1m["t"] < ah_end)].copy()
    pre_1m = scid_1m[
        (scid_1m["t"] >= rth_start) & (scid_1m["t"] < datetime.combine(day, dtime(8, 30)))
    ].copy()

    bars15 = load_rth_15s(day, db)
    fp = load_footprint_rth_safe(day, db)

    # RTH source bars for 1h agg: 08:00–08:30 from 1m + 08:30–15:00 from 15s
    rth_src_parts = []
    if not pre_1m.empty:
        rth_src_parts.append(pre_1m[["t", "open", "high", "low", "close", "total_volume", "volume_delta"]])
    if not bars15.empty:
        rth_src_parts.append(
            bars15[["t", "open", "high", "low", "close", "total_volume", "volume_delta"]]
        )
    rth_src = pd.concat(rth_src_parts, ignore_index=True).sort_values("t") if rth_src_parts else pd.DataFrame()

    ah_1h = aggregate_1h(ah_1m)
    rth_1h = aggregate_1h(rth_src)

    # Developing overlays
    ah_dev = developing_from_close_bars(ah_1m, sample_every=1)
    rth_dev = developing_rth_hybrid(pre_1m, bars15, fp)

    ah_end_lv = ending_levels(ah_dev)
    rth_end_lv = ending_levels(rth_dev)

    # ---- figure ----
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    out_path = out or (CHART_DIR / OUT_NAME.format(day=day.isoformat()))

    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=FIGSIZE, dpi=DPI, sharex=True,
        gridspec_kw={"height_ratios": [3.4, 0.85], "hspace": 0.04},
    )

    # Backgrounds AH / RTH
    ax.axvspan(ah_start, ah_end, facecolor=C_AH_BG, alpha=0.85, zorder=0, label="AH / Globex")
    ax.axvspan(rth_start, rth_end, facecolor=C_RTH_BG, alpha=0.75, zorder=0, label="RTH (display from 08:00)")
    axv.axvspan(ah_start, ah_end, facecolor=C_AH_BG, alpha=0.85, zorder=0)
    axv.axvspan(rth_start, rth_end, facecolor=C_RTH_BG, alpha=0.75, zorder=0)

    # Divider at 08:00
    ax.axvline(rth_start, color=C_DIV, lw=1.6, ls="-", zorder=2.5)
    axv.axvline(rth_start, color=C_DIV, lw=1.4, ls="-", zorder=2.5)
    ax.text(
        rth_start, 1.01, " 08:00 CT  AH→RTH ",
        transform=ax.get_xaxis_transform(),
        color=C_DIV, fontsize=8, fontweight="bold", va="bottom", ha="left",
    )

    # Cash open marker 08:30
    cash_open = datetime.combine(day, dtime(8, 30))
    ax.axvline(cash_open, color="#94a3b8", lw=1.0, ls=":", zorder=2.4, alpha=0.9)
    ax.text(
        cash_open, 0.99, " 08:30 cash/fut open ",
        transform=ax.get_xaxis_transform(),
        color="#64748b", fontsize=7.0, va="top", ha="left",
    )

    draw_1h_candles(ax, ah_1h, alpha=0.88)
    draw_1h_candles(ax, rth_1h, alpha=0.95)

    plot_developing(ax, ah_dev, "AH")
    plot_developing(ax, rth_dev, "RTH")

    # Freeze AH ending levels across into RTH (until RTH replaces / through RTH start+)
    # Extend AH VA through RTH as dashed reference until ~ mid-RTH or full RTH
    if ah_end_lv:
        # last AH sample time ≈ last ah_1m bar
        x0 = ah_dev.iloc[-1]["t"] if not ah_dev.empty else ah_end
        extend_levels(ax, ah_end_lv, x0, rth_end, C_AH_FREEZE, "AH", lw=1.7)

    # Freeze RTH ending levels short post-close stub (15:00 → 15:20)
    if rth_end_lv:
        x0 = rth_dev.iloc[-1]["t"] if not rth_dev.empty else rth_end - timedelta(seconds=15)
        extend_levels(
            ax, rth_end_lv, x0, rth_end + timedelta(minutes=20), C_RTH_FREEZE, "RTH", lw=1.9
        )

    draw_volume_panel(axv, ah_1h, rth_1h)

    # Y lims from candles + developing
    ys: list[float] = []
    for df in (ah_1h, rth_1h):
        if df is not None and not df.empty:
            ys += list(df["low"]) + list(df["high"])
    for dt in (ah_dev, rth_dev):
        if dt is not None and not dt.empty:
            ys += list(dt["dval90"]) + list(dt["dvah90"]) + list(dt["dpoc"]) + list(dt["dvwap"])
    for lv in (ah_end_lv, rth_end_lv):
        for k in ("vah", "val", "poc", "vwap"):
            if k in lv and np.isfinite(lv[k]):
                ys.append(lv[k])
    ys = [y for y in ys if np.isfinite(y)]
    if ys:
        pad = max(10.0, (max(ys) - min(ys)) * 0.055)
        ax.set_ylim(min(ys) - pad, max(ys) + pad)

    ax.set_xlim(ah_start - timedelta(minutes=20), rth_end + timedelta(minutes=35))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M"))
    ax.grid(True, axis="both", alpha=0.22, lw=0.5)
    axv.grid(True, axis="y", alpha=0.22, lw=0.5)
    ax.set_ylabel("NQ price", fontsize=9)
    ax.tick_params(labelsize=7.5)

    # Legend (dedupe)
    handles, labels = ax.get_legend_handles_labels()
    seen = set()
    h2, l2 = [], []
    for h, lab in zip(handles, labels):
        if lab in seen or not lab:
            continue
        seen.add(lab)
        h2.append(h)
        l2.append(lab)
    ax.legend(h2, l2, loc="upper left", fontsize=7.0, ncol=3, framealpha=0.88)

    ah_n = int(len(ah_1h))
    rth_n = int(len(rth_1h))
    title = (
        f"NQU26  {day.isoformat()}  ·  1-hour bars  ·  AH vs RTH (display)\n"
        f"AH: {yday} 15:00 → {day} 08:00 CT   |   "
        f"RTH from 08:00 CT (display) → 15:00   ·   cash/fut open still 08:30\n"
        f"dVWAP orange · dPOC fuchsia · dVA 70%/90%  ·  ending VA H/L dashed freeze"
    )
    ax.set_title(title, fontsize=10.5, loc="left", pad=10)

    fig.subplots_adjust(left=0.055, right=0.955, top=0.88, bottom=0.07, hspace=0.04)
    fig.savefig(out_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    # Stdout summary
    print(f"day={day.isoformat()}  prior={yday.isoformat()}")
    print(f"AH_1h_bars={ah_n}  RTH_1h_bars={rth_n}")
    print(f"AH_1m_src={len(ah_1m)}  RTH_pre_1m={len(pre_1m)}  RTH_15s={len(bars15)}  FP_rows={len(fp)}")
    def _fmt(lv: dict, tag: str):
        if not lv:
            print(f"{tag}: (none)")
            return
        print(
            f"{tag}: VAH={lv.get('vah'):.2f}  VAL={lv.get('val'):.2f}  "
            f"POC={lv.get('poc'):.2f}  VWAP={lv.get('vwap'):.2f}  "
            f"VAH90={lv.get('vah90'):.2f}  VAL90={lv.get('val90'):.2f}"
        )
    _fmt(ah_end_lv, "ending_AH")
    _fmt(rth_end_lv, "ending_RTH")
    print(f"figsize={FIGSIZE}  dpi={DPI}")
    print(f"wrote {out_path}")
    return out_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", default=DEFAULT_DAY.isoformat(), help="chart day YYYY-MM-DD")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--scid", default=str(SCID_1M))
    p.add_argument("--out", default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> Path:
    args = parse_args(argv)
    day = date.fromisoformat(args.date)
    out = Path(args.out) if args.out else None
    return build_chart(day, Path(args.db), Path(args.scid), out)


if __name__ == "__main__":
    main()
