#!/usr/bin/env python3
"""Plot-only overlay: volume, cumulative delta, dPOC, dVWAP, VWAP σ bands, σ-channel.

Does NOT change the trade rule or fills. Marks come from CLOSE_2026-07-30.json.
dPOC / dVWAP come from rth_developing_value_area_15s (value_area_percent=70).
dPOC is drawn as steps-post runs colored by the volume_delta of the bar that
shifted the POC (not by up/down; not by cum_delta). Overwrites
or_127_close205_charts/CLOSE_2026-07-30.png.
The third pane is session cumulative delta (sum of bars_15s.volume_delta from
RTH bar 1 through i inclusive), same series as the 1.27 entry gate.
5m OR High/Low (bars 1-20) plus Luis fib ladder on PRICE and on CVD.
k = 1.27, 1.618, 2.05, 2.618, 3.33, 4.23, 5.33, 6.85, 8.62, 11.09
(8.62 = 5.33*phi, 11.09 = phi^5). Both sides, no clipping.
Price expansions reuse luis_or_127_delta.expansion_level (High+(k-1)*R, tick 0.25)
including new k 8.62/11.09 (same round-to-0.25). CVD uses the same (k-1)*R
formula without tick rounding. y-limits include every drawn fib plus pad.
"""
from __future__ import annotations

import json
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
from matplotlib.collections import LineCollection
import numpy as np
import pandas as pd
import duckdb

from luis_or_127_close205 import (
    C_127_L,
    C_205_L,
    C_OR_EMPH,
    C_OR_OTH,
    CHART_DIR,
    DAY,
    as_naive,
    session_ticks,
)
from luis_or_127_delta import DB_PATH, expansion_level
from luis_vbp_or import ts

OUT_PNG = CHART_DIR / f"CLOSE_{DAY.isoformat()}.png"
OUT_JSON = CHART_DIR / f"CLOSE_{DAY.isoformat()}.json"
FIGSIZE = (14.5, 12.4)
DPI = 120
C_DPOC_POS = "#2ca02c"   # volume_delta > 0 (buyers printed new POC)
C_DPOC_NEG = "#d62728"   # volume_delta < 0 (sellers printed new POC)
C_DPOC_ZERO = "#7f7f7f"  # volume_delta == 0
C_DVWAP = "#ff7f0e"
BAR_W = 15.0 / 86400.0  # 15s in matplotlib date units
VA_SOURCE = "rth_developing_value_area_15s value_area_percent=70"
TICK = 0.25
SIGMA_KS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
# oranges/browns, alpha and weight fade as |k| grows; 0.5σ stronger than 3σ
SIGMA_BAND_COLORS = ("#ff9a2e", "#f08c18", "#e07010", "#c45c14", "#a34a16", "#7a3c12")
SIGMA_BAND_ALPHAS = (0.80, 0.64, 0.50, 0.38, 0.28, 0.20)
SIGMA_BAND_LWS = (0.95, 0.80, 0.70, 0.62, 0.54, 0.46)
C_OR_GOLD = "#c9a227"
C_FIB_UP = "#5e7d32"
C_FIB_DN = "#a3542c"
OR_LAST_BAR = 20
# Full Luis ladder. 3.33 matches expansion_level helper (not 3.333).
# 8.62 = 5.33*phi, 11.09 = phi^5.
FIB_K = (1.27, 1.618, 2.05, 2.618, 3.33, 4.23, 5.33, 6.85, 8.62, 11.09)
# 1.27/2.05 stronger; outer k thinner but still visible (not near-invisible).
FIB_FADE = {
    1.618: (0.95, 0.74),
    2.618: (0.88, 0.66),
    3.33: (0.80, 0.60),
    4.23: (0.74, 0.56),
    5.33: (0.68, 0.52),
    6.85: (0.64, 0.48),
    8.62: (0.60, 0.46),
    11.09: (0.56, 0.44),
}


def expansion_cvd(cvd_high: float, cvd_low: float, k: float, side: str) -> float:
    """Same geometry as expansion_level: High+(k-1)*R / Low-(k-1)*R, no tick round."""
    r = cvd_high - cvd_low
    if side == "up":
        return cvd_high + (k - 1.0) * r
    return cvd_low - (k - 1.0) * r


def _fade_for(k: float) -> tuple[float, float]:
    for kk, va in FIB_FADE.items():
        if abs(kk - k) < 1e-12:
            return va
    return (0.50, 0.38)


def fib_style(k: float, side: str) -> dict:
    """1.27/2.05 stronger (entry/gate); outer k thinner but still visible."""
    if abs(k - 1.27) < 1e-12:
        color = C_127_L if side == "up" else C_DPOC_NEG
        return dict(color=color, lw=1.15, ls="--", alpha=0.84, zorder=3.4)
    if abs(k - 2.05) < 1e-12:
        color = C_205_L if side == "up" else "#8b1a1a"
        return dict(color=color, lw=1.20, ls=":", alpha=0.88, zorder=3.4)
    lw, alpha = _fade_for(k)
    color = C_FIB_UP if side == "up" else C_FIB_DN
    return dict(color=color, lw=lw, ls=":", alpha=alpha, zorder=2.8)


def fib_k_label(k: float) -> str:
    """Readable k text: 1.618/2.618 keep 3 dp; 3.33/8.62/11.09 keep 2 dp."""
    return f"{k:g}"


def draw_or_fib_ladder(ax, high: float, low: float, *, tick_round: bool) -> list[tuple[float, str, float]]:
    """Draw full Luis OR ladder both sides. One legend entry. Returns (k, side, level)."""
    levels: list[tuple[float, str, float]] = []
    helper = expansion_level if tick_round else expansion_cvd
    first = True
    for k in FIB_K:
        for side in ("up", "down"):
            y = float(helper(high, low, k, side))
            levels.append((k, side, y))
            st = fib_style(k, side)
            lbl = "OR fibs" if first else None
            ax.axhline(y, label=lbl, **st)
            first = False
    return levels


def draw_fib_labels(ax, levels: list[tuple[float, str, float]]) -> None:
    """Right-margin label for every k, both sides. Alternate x so neighbours stay readable."""
    k_index: dict[float, int] = {}
    for i, k in enumerate(FIB_K):
        k_index[k] = i
    for k, side, y in levels:
        st = fib_style(k, side)
        is_emph = abs(k - 1.27) < 1e-12 or abs(k - 2.05) < 1e-12
        idx = k_index.get(k, 0)
        # just outside the axes, staggered so 1.27/1.618 etc. do not collide
        x = 1.004 if idx % 2 == 0 else 1.046
        ax.text(
            x, y, fib_k_label(k),
            transform=ax.get_yaxis_transform(),
            color=st["color"],
            fontsize=7.0 if is_emph else 6.2,
            va="center", ha="left", zorder=7,
            fontweight="bold" if is_emph else "normal",
            clip_on=False,
        )


def color_for_delta(delta: float) -> str:
    if delta > 0:
        return C_DPOC_POS
    if delta < 0:
        return C_DPOC_NEG
    return C_DPOC_ZERO


def dpoc_shift_runs(dpoc, volume_delta):
    """Split into equal-dPOC runs. Color = volume_delta of the first bar (the shift).

    Bar 1 (index 0) is the birth shift. A later bar is a shift iff abs(dpoc-prev) > 1e-9.
    Returns (runs, n_pos, n_neg, n_zero) where each run is (i0, i1_inclusive, color, delta).
    """
    n = len(dpoc)
    runs = []
    n_pos = n_neg = n_zero = 0
    i = 0
    while i < n:
        j = i + 1
        while j < n and abs(float(dpoc[j]) - float(dpoc[i])) <= 1e-9:
            j += 1
        d = float(volume_delta[i])
        c = color_for_delta(d)
        if d > 0:
            n_pos += 1
        elif d < 0:
            n_neg += 1
        else:
            n_zero += 1
        runs.append((i, j - 1, c, d))
        i = j
    return runs, n_pos, n_neg, n_zero


def plot_dpoc_delta_colored(ax, vis):
    """Draw dPOC as steps-post runs colored by the shift bar's volume_delta."""
    t = vis["t"]
    dpoc = vis["dpoc"].to_numpy(dtype=float)
    vd = vis["volume_delta"].to_numpy(dtype=float)
    n = len(vis)
    runs, n_pos, n_neg, n_zero = dpoc_shift_runs(dpoc, vd)

    ax.plot(
        [], [], color=C_DPOC_POS, lw=1.6, drawstyle="steps-post",
        label="dPOC (delta+)", zorder=4,
    )
    ax.plot(
        [], [], color=C_DPOC_NEG, lw=1.6, drawstyle="steps-post",
        label="dPOC (delta\u2212)", zorder=4,
    )

    for i0, i1, color, _d in runs:
        if i1 + 1 < n:
            xs = t.iloc[i0:i1 + 2]
            ys = list(dpoc[i0:i1 + 1]) + [dpoc[i1]]
        else:
            xs = t.iloc[i0:i1 + 1]
            ys = list(dpoc[i0:i1 + 1])
            if len(xs) == 1:
                xs = [t.iloc[i0], vis["t_end"].iloc[i0]]
                ys = [dpoc[i1], dpoc[i1]]
        ax.plot(xs, ys, color=color, lw=1.6, drawstyle="steps-post", zorder=4)
        if i0 > 0:
            ti = t.iloc[i0]
            ax.plot(
                [ti, ti], [dpoc[i0 - 1], dpoc[i0]],
                color=color, lw=1.6, zorder=4, solid_capstyle="round",
            )
    return len(runs), n_pos, n_neg, n_zero


def hm_s(x) -> str:
    t = as_naive(x)
    if t is None:
        return ""
    s = ts(t)
    return s[11:19] if len(s) >= 19 else s


def load_day_series(day: date) -> pd.DataFrame:
    """Canonical dPOC/dVWAP from developing VA; volume + delta from bars_15s."""
    iso = day.isoformat()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if "rth_developing_value_area_15s" not in tables:
        raise SystemExit("rth_developing_value_area_15s missing; expected canonical dPOC/dVWAP")
    g = con.execute(
        f"""
        SELECT
            b.trading_day,
            b.rth_bar_number,
            b.bar_start_chicago,
            b.bar_end_chicago,
            b.open, b.high, b.low, b.close,
            b.total_volume,
            b.volume_delta,
            b.rth_vwap AS bar_vwap,
            v.dpoc,
            v.rth_vwap AS dvwap
        FROM bars_15s b
        JOIN rth_developing_value_area_15s v
          ON b.trading_day = v.trading_day
         AND b.rth_bar_number = v.rth_bar_number
        WHERE b.trading_day = DATE '{iso}'
          AND v.value_area_percent = 70
        ORDER BY b.rth_bar_number
        """
    ).fetchdf()
    con.close()
    if g.empty:
        raise SystemExit(f"no bars+VA for {iso}")
    g["trading_day"] = pd.to_datetime(g["trading_day"]).dt.date
    g["t"] = pd.to_datetime(g["bar_start_chicago"])
    if getattr(g["t"].dt, "tz", None) is not None:
        g["t"] = g["t"].dt.tz_localize(None)
    g["t_end"] = pd.to_datetime(g["bar_end_chicago"])
    if getattr(g["t_end"].dt, "tz", None) is not None:
        g["t_end"] = g["t_end"].dt.tz_localize(None)
    g = g.sort_values("rth_bar_number").reset_index(drop=True)
    # Session-anchored at 08:30 (RTH bar 1); this file is one day so no day-reset.
    g["cum_delta"] = g["volume_delta"].astype("int64").cumsum()
    return g


def attach_vwap_sigma(g: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Session-anchored causal VWAP σ on 15s RTH bars from 08:30.

    Classic volume-weighted identity on typical=(H+L+C)/3. Bands always
    center on table dVWAP (the orange line). If computed VWAP matches
    table dVWAP within a tick, classic σ is used as-is. If they diverge,
    still center on table dVWAP; σ stays the classic stdev of typical
    (the hug formula sqrt(max(E[typical^2]-dVWAP^2,0)) clips to 0 while
    table VWAP is not the typical-mean, which would collapse bands at
    the open including entry/lift). First bar with var==0 → NaN σ.
    """
    g = g.sort_values("rth_bar_number").reset_index(drop=True).copy()
    h = g["high"].to_numpy(dtype=float)
    lo = g["low"].to_numpy(dtype=float)
    c = g["close"].to_numpy(dtype=float)
    vol = g["total_volume"].to_numpy(dtype=float)
    table_vwap = g["dvwap"].to_numpy(dtype=float)
    typical = (h + lo + c) / 3.0
    sum_pv = np.cumsum(typical * vol)
    sum_p2v = np.cumsum(typical * typical * vol)
    sum_v = np.cumsum(vol)
    with np.errstate(divide="ignore", invalid="ignore"):
        computed_vwap = np.where(sum_v > 0, sum_pv / sum_v, np.nan)
        e_t2 = np.where(sum_v > 0, sum_p2v / sum_v, np.nan)
    var_classic = np.maximum(e_t2 - computed_vwap ** 2, 0.0)
    var_hug = np.maximum(e_t2 - table_vwap ** 2, 0.0)
    abs_diff = np.abs(computed_vwap - table_vwap)
    max_diff = float(np.nanmax(abs_diff)) if len(abs_diff) else float("nan")
    n_within_tick = int(np.nansum(abs_diff <= TICK))
    matched = bool(np.isfinite(max_diff) and max_diff <= TICK)
    # Always center on table dVWAP. Use classic σ: hug var is 0 on the
    # open when table VWAP ≠ E[typical], which is not a usable band width.
    sigma = np.sqrt(var_classic)
    sigma = np.where(var_classic > 0, sigma, np.nan)
    center = table_vwap
    z = np.zeros(len(c), dtype=float)
    ok = np.isfinite(sigma) & (sigma > 0)
    z[ok] = (c[ok] - center[ok]) / sigma[ok]
    channel = np.floor(z / 0.5).astype(np.int32)
    g["typical"] = typical
    g["computed_vwap"] = computed_vwap
    g["sigma"] = sigma
    g["vwap_center"] = center
    g["z"] = z
    g["sigma_channel"] = channel
    g["sigma_channel_y"] = channel.astype(float) * 0.5
    g["var_hug"] = var_hug
    max_abs_z = float(np.nanmax(np.abs(z))) if len(z) else float("nan")
    meta = {
        "matched_within_tick": matched,
        "max_abs_vwap_diff": max_diff,
        "n_within_tick": n_within_tick,
        "n_bars": int(len(g)),
        "max_abs_z": max_abs_z,
        "n_hug_zero": int(np.nansum(var_hug <= 0)),
    }
    return g, meta


def plot_vwap_sigma_bands(ax, vis) -> None:
    """VWAP ± kσ for k=0.5..3.0 both sides. One legend entry."""
    t = vis["t"]
    center = vis["vwap_center"].to_numpy(dtype=float)
    sig = vis["sigma"].to_numpy(dtype=float)
    lo05 = center - 0.5 * sig
    hi05 = center + 0.5 * sig
    ax.fill_between(
        t, lo05, hi05, color=C_DVWAP, alpha=0.10, linewidth=0, zorder=1.6,
    )
    ax.plot(
        [], [], color=SIGMA_BAND_COLORS[0], lw=SIGMA_BAND_LWS[0],
        alpha=SIGMA_BAND_ALPHAS[0], label="VWAP ±0.5σ steps", zorder=3,
    )
    for k, color, alpha, lw in zip(SIGMA_KS, SIGMA_BAND_COLORS, SIGMA_BAND_ALPHAS, SIGMA_BAND_LWS):
        ax.plot(t, center + k * sig, color=color, lw=lw, alpha=alpha, zorder=3)
        ax.plot(t, center - k * sig, color=color, lw=lw, alpha=alpha, zorder=3)


def series_at(g: pd.DataFrame, bar: int) -> dict:
    row = g[g["rth_bar_number"] == bar]
    if row.empty:
        raise SystemExit(f"missing bar {bar}")
    r = row.iloc[0]
    sig = r["sigma"] if "sigma" in r.index else np.nan
    z = r["z"] if "z" in r.index else np.nan
    ch = r["sigma_channel"] if "sigma_channel" in r.index else np.nan
    cv = r["computed_vwap"] if "computed_vwap" in r.index else np.nan
    return {
        "bar": int(r["rth_bar_number"]),
        "time_hm": hm_s(r["t"]),
        "close": float(r["close"]),
        "dpoc": float(r["dpoc"]),
        "dvwap": float(r["dvwap"]),
        "computed_vwap": float(cv) if pd.notna(cv) else None,
        "sigma": float(sig) if pd.notna(sig) else None,
        "z": float(z) if pd.notna(z) else None,
        "channel": int(ch) if pd.notna(ch) else None,
        "total_volume": int(r["total_volume"]),
        "volume_delta": int(r["volume_delta"]),
        "cum_delta": int(r["cum_delta"]) if "cum_delta" in r.index and pd.notna(r["cum_delta"]) else None,
    }



def plot_cum_delta_line(ax, vis) -> None:
    """Steps-post cum_delta line, green when >0, red when <0, split at 0."""
    t = pd.to_datetime(vis["t"])
    t_end = pd.to_datetime(vis["t_end"])
    y = vis["cum_delta"].to_numpy(dtype=float)
    n = len(y)
    if n == 0:
        ax.plot([], [], color=C_DPOC_POS, lw=1.45, label="Cumulative delta", zorder=4)
        return
    x = mdates.date2num(t.to_numpy())
    x_end = mdates.date2num(t_end.to_numpy())
    # Steps-post vertices: horizontal across each bar, then vertical to next value.
    vx = [x[0]]
    vy = [float(y[0])]
    for i in range(n):
        xr = x[i + 1] if i + 1 < n else x_end[i]
        vx.append(xr)
        vy.append(float(y[i]))
        if i + 1 < n:
            vx.append(x[i + 1])
            vy.append(float(y[i + 1]))
    vx = np.asarray(vx, dtype=float)
    vy = np.asarray(vy, dtype=float)

    def col(v: float) -> str | None:
        if v > 0:
            return C_DPOC_POS
        if v < 0:
            return C_DPOC_NEG
        return None

    segments: list[list[tuple[float, float]]] = []
    colors: list[str] = []
    for i in range(len(vx) - 1):
        x0, y0 = float(vx[i]), float(vy[i])
        x1, y1 = float(vx[i + 1]), float(vy[i + 1])
        if (y0 > 0 and y1 < 0) or (y0 < 0 and y1 > 0):
            frac = (0.0 - y0) / (y1 - y0)
            xz = x0 + frac * (x1 - x0)
            c0 = col(y0)
            c1 = col(y1)
            if c0 is not None:
                segments.append([(x0, y0), (xz, 0.0)])
                colors.append(c0)
            if c1 is not None:
                segments.append([(xz, 0.0), (x1, y1)])
                colors.append(c1)
            continue
        v = y0 if y0 != 0 else y1
        c = col(v)
        if c is None:
            continue
        segments.append([(x0, y0), (x1, y1)])
        colors.append(c)

    if segments:
        lc = LineCollection(
            segments, colors=colors, linewidths=1.45, zorder=4, capstyle="butt",
        )
        ax.add_collection(lc)
    ax.plot([], [], color=C_DPOC_POS, lw=1.45, label="Cumulative delta", zorder=4)


def plot_stacked(g: pd.DataFrame, facts: dict, out_path: Path) -> dict:
    day = date.fromisoformat(facts["day"])
    t0 = datetime.combine(day, dtime(8, 30))
    t_right = datetime.combine(day, dtime(15, 0))
    vis = g[(g["t"] >= t0 - timedelta(minutes=1)) & (g["t"] <= t_right + timedelta(minutes=1))].copy()
    vis = vis.sort_values("rth_bar_number")

    or_bars = g[(g["rth_bar_number"] >= 1) & (g["rth_bar_number"] <= OR_LAST_BAR)]
    if len(or_bars) != OR_LAST_BAR:
        raise SystemExit(f"expected {OR_LAST_BAR} OR bars, got {len(or_bars)}")
    or_high = float(or_bars["high"].max())
    or_low = float(or_bars["low"].min())
    or_R = or_high - or_low
    if abs(or_high - float(facts["or_high"])) > 1e-9 or abs(or_low - float(facts["or_low"])) > 1e-9:
        raise SystemExit(f"OR mismatch data={or_high}/{or_low} facts={facts['or_high']}/{facts['or_low']}")
    cvd_high = float(or_bars["cum_delta"].max())
    cvd_low = float(or_bars["cum_delta"].min())
    r_cvd = cvd_high - cvd_low

    or_val = float(facts["or_val"])
    or_vah = float(facts["or_vah"])
    long_127 = float(expansion_level(or_high, or_low, 1.27, "up"))
    long_205 = float(expansion_level(or_high, or_low, 2.05, "up"))
    short_127 = float(expansion_level(or_high, or_low, 1.27, "down"))
    short_205 = float(expansion_level(or_high, or_low, 2.05, "down"))
    if abs(long_127 - float(facts["long_127"])) > 1e-9:
        raise SystemExit(f"1.27 long mismatch {long_127} vs {facts['long_127']}")
    if abs(long_205 - float(facts["long_205"])) > 1e-9:
        raise SystemExit(f"2.05 long mismatch {long_205} vs {facts['long_205']}")
    reason = facts["exit_reason"]
    net = float(facts["net"])
    lift = facts.get("lift") or {}

    title = f"{day.isoformat()} LONG · close-rule · {reason} net {net:+.2f}"

    fig, (ax, axv, axd, axc) = plt.subplots(
        4, 1, sharex=True, figsize=FIGSIZE, dpi=DPI,
        gridspec_kw={"height_ratios": [3.4, 0.7, 0.7, 0.9], "hspace": 0.055},
    )

    # ----- PRICE -----
    ax.fill_between(
        vis["t"], vis["low"], vis["high"],
        color="0.82", alpha=0.55, linewidth=0, label="15s range", zorder=1,
    )
    plot_vwap_sigma_bands(ax, vis)
    ax.plot(vis["t"], vis["close"], color="0.18", lw=0.85, label="15s close", zorder=3)
    plot_dpoc_delta_colored(ax, vis)
    ax.plot(
        vis["t"], vis["dvwap"], color=C_DVWAP, lw=1.45,
        label="dVWAP", zorder=4,
    )

    x0 = mdates.date2num(t0)
    x1 = mdates.date2num(datetime.combine(day, dtime(8, 35)))
    ax.add_patch(Rectangle(
        (x0, or_low), x1 - x0, or_high - or_low,
        facecolor="#ffcc00", edgecolor="#aa8800", linewidth=1.15, alpha=0.48,
        zorder=2, label="5m OR box",
    ))
    ax.axhline(or_high, color=C_OR_GOLD, lw=1.55, zorder=3.5, label="OR High/Low")
    ax.axhline(or_low, color=C_OR_GOLD, lw=1.55, zorder=3.5)
    ax.text(
        datetime.combine(day, dtime(8, 30, 20)), or_high,
        " OR High", color=C_OR_GOLD, fontsize=7.0, va="bottom", ha="left", zorder=7,
        fontweight="bold",
    )
    ax.text(
        datetime.combine(day, dtime(8, 30, 20)), or_low,
        " OR Low", color=C_OR_GOLD, fontsize=7.0, va="top", ha="left", zorder=7,
        fontweight="bold",
    )
    price_fibs = draw_or_fib_ladder(ax, or_high, or_low, tick_round=True)
    draw_fib_labels(ax, price_fibs)
    ax.axhline(
        or_val, color=C_OR_EMPH, lw=2.15, ls="--",
        label="OR VAL (long stop until lift)", zorder=5,
    )
    ax.text(
        t0 + timedelta(seconds=40), or_val, " OR VAL",
        color=C_OR_EMPH, fontsize=7.2, va="bottom", ha="left", zorder=7,
        fontweight="bold",
    )
    ax.axhline(
        or_vah, color=C_OR_OTH, lw=1.15, ls="-",
        label="OR VAH", zorder=5,
    )
    ax.text(
        t0 + timedelta(seconds=40), or_vah, " OR VAH",
        color=C_OR_OTH, fontsize=7.2, va="bottom", ha="left", zorder=7,
    )

    et = as_naive(facts["entry_time"])
    xt = as_naive(facts["exit_time"])
    et_ts = pd.Timestamp(et)
    xt_ts = pd.Timestamp(xt)

    ax.scatter(
        [et_ts], [facts["entry_price"]], marker="^", color="#2ca02c", s=90,
        zorder=8, edgecolors="white", linewidths=0.4, label="long entry",
    )
    ax.axvline(et_ts, color="#2ca02c", lw=0.7, alpha=0.35, zorder=2)

    old = facts.get("bar_083730_old_or_va_stop") or {}
    if old:
        t_dip = datetime.combine(day, dtime(8, 37, 30))
        ax.scatter(
            [pd.Timestamp(t_dip)], [old["low"]],
            marker="o", facecolors="none", edgecolors=C_OR_EMPH,
            s=48, linewidths=1.05, zorder=8, label="08:37:30 wick vs VAL (held)",
        )
        ax.annotate(
            f"wick {old['low']:.2f} thru VAL\nclose {old['close']:.2f} (held)",
            xy=(pd.Timestamp(t_dip), old["low"]),
            xytext=(10, -28),
            textcoords="offset points",
            fontsize=6.6, color=C_OR_EMPH, zorder=9,
        )

    t_lift = None
    if lift:
        t_lift = pd.Timestamp(as_naive(lift["time"]))
        ax.scatter(
            [t_lift], [lift["close"]],
            marker="o", facecolors="none", edgecolors=C_205_L,
            s=90, linewidths=1.6, zorder=8, label="close > 2.05 (lift)",
        )
        ax.axvline(t_lift, color=C_205_L, lw=0.7, ls=":", alpha=0.45, zorder=2)
        ax.annotate(
            "close > 2.05",
            xy=(t_lift, lift["close"]),
            xytext=(6, 10),
            textcoords="offset points",
            fontsize=7.2, color=C_205_L, zorder=9, fontweight="bold",
        )

    xcol = "#006400" if net >= 0 else "#e31a1c"
    ax.scatter(
        [xt_ts], [facts["exit_price"]], marker="x", color=xcol, s=95,
        zorder=8, linewidths=1.4, label=reason,
    )
    ax.axvline(xt_ts, color=xcol, lw=0.75, alpha=0.5, zorder=2)
    ax.annotate(
        f"{reason}\nnet {net:+.2f}",
        xy=(xt_ts, facts["exit_price"]),
        xytext=(-72, -18),
        textcoords="offset points",
        fontsize=7, color=xcol, zorder=9,
    )

    y_extra = [
        or_high, or_low, or_val, or_vah, long_127, long_205, short_127, short_205,
        float(facts["entry_price"]), float(facts["exit_price"]),
        float(vis["dpoc"].min()), float(vis["dpoc"].max()),
        float(vis["dvwap"].min()), float(vis["dvwap"].max()),
    ]
    if lift:
        y_extra.append(float(lift["close"]))
    # Every k both sides. Highest long (11.09) and lowest short (11.09) set the
    # pane so none of the ladder is clipped. Price action will look smaller.
    for _k, _side, y in price_fibs:
        y_extra.append(y)

    vis_y = g[(g["t"] >= t0) & (g["t"] <= t_right)]
    if vis_y.empty:
        vis_y = vis
    y_lo = min(float(vis_y["low"].min()), min(y_extra))
    y_hi = max(float(vis_y["high"].max()), max(y_extra))
    pad_y = (y_hi - y_lo) * 0.08 if y_hi > y_lo else 8.0
    ax.set_ylim(y_lo - pad_y, y_hi + pad_y)
    ax.set_ylabel("NQU26")
    ax.set_title(title, fontsize=10, pad=7)
    ax.grid(True, alpha=0.28)

    handles, labels = ax.get_legend_handles_labels()
    uniq = dict(zip(labels, handles))
    ax.legend(
        uniq.values(), uniq.keys(),
        loc="upper left", fontsize=6.3, framealpha=0.9,
        handlelength=1.6, labelspacing=0.20, ncol=3,
    )

    # ----- VOLUME -----
    xnum = mdates.date2num(pd.to_datetime(vis["t"]).to_numpy())
    vol = vis["total_volume"].to_numpy(dtype=float)
    prior = vis["close"].shift(1)
    up = vis["close"].to_numpy(dtype=float) >= prior.to_numpy(dtype=float)
    up[0] = True
    vcolors = np.where(up, "#6b6b6b", "#b0b0b0")
    axv.bar(
        xnum, vol, width=BAR_W, align="edge", color=vcolors,
        linewidth=0, label="Volume", zorder=3,
    )
    axv.axvline(et_ts, color="#2ca02c", lw=0.7, alpha=0.35, zorder=2)
    if t_lift is not None:
        axv.axvline(t_lift, color=C_205_L, lw=0.7, ls=":", alpha=0.45, zorder=2)
    axv.axvline(xt_ts, color=xcol, lw=0.75, alpha=0.5, zorder=2)
    axv.set_ylabel("Volume")
    axv.set_ylim(0, float(np.nanmax(vol)) * 1.12 if len(vol) else 1)
    axv.legend(loc="upper left", fontsize=6.4, framealpha=0.9, handlelength=1.4)
    axv.grid(True, alpha=0.28)

    # ----- CUMULATIVE DELTA (session, RTH bar 1..i inclusive) -----
    cd = vis["cum_delta"].to_numpy(dtype=float)
    plot_cum_delta_line(axd, vis)
    axd.axhline(0, color="0.35", lw=0.8, zorder=5)
    axd.add_patch(Rectangle(
        (x0, cvd_low), x1 - x0, cvd_high - cvd_low,
        facecolor="#ffcc00", edgecolor="#aa8800", linewidth=1.15, alpha=0.48,
        zorder=2, label="5m OR box",
    ))
    axd.axhline(cvd_high, color=C_OR_GOLD, lw=1.35, zorder=3.5, label="OR High/Low")
    axd.axhline(cvd_low, color=C_OR_GOLD, lw=1.35, zorder=3.5)
    axd.text(
        datetime.combine(day, dtime(8, 30, 20)), cvd_high,
        " OR High", color=C_OR_GOLD, fontsize=6.4, va="bottom", ha="left", zorder=7,
        fontweight="bold",
    )
    axd.text(
        datetime.combine(day, dtime(8, 30, 20)), cvd_low,
        " OR Low", color=C_OR_GOLD, fontsize=6.4, va="top", ha="left", zorder=7,
        fontweight="bold",
    )
    cvd_fibs = draw_or_fib_ladder(axd, cvd_high, cvd_low, tick_round=False)
    draw_fib_labels(axd, cvd_fibs)
    cvd_127_up = float(expansion_cvd(cvd_high, cvd_low, 1.27, "up"))
    cvd_205_up = float(expansion_cvd(cvd_high, cvd_low, 2.05, "up"))
    cvd_127_dn = float(expansion_cvd(cvd_high, cvd_low, 1.27, "down"))
    cvd_205_dn = float(expansion_cvd(cvd_high, cvd_low, 2.05, "down"))
    axd.axvline(et_ts, color="#2ca02c", lw=0.7, alpha=0.35, zorder=2)
    if t_lift is not None:
        axd.axvline(t_lift, color=C_205_L, lw=0.7, ls=":", alpha=0.45, zorder=2)
    axd.axvline(xt_ts, color=xcol, lw=0.75, alpha=0.5, zorder=2)
    axd.set_ylabel("Cumulative delta")
    cvd_fib_ys = [y for _k, _side, y in cvd_fibs]
    if len(cd):
        ylo_d = min(0.0, float(np.nanmin(cd)), cvd_low, min(cvd_fib_ys))
        yhi_d = max(0.0, float(np.nanmax(cd)), cvd_high, max(cvd_fib_ys))
    else:
        ylo_d = min(cvd_fib_ys) if cvd_fib_ys else -1.0
        yhi_d = max(cvd_fib_ys) if cvd_fib_ys else 1.0
    # Include every CVD fib (5.33/6.85/8.62/11.09 down too). No clipping.
    pad_d = (yhi_d - ylo_d) * 0.08 if yhi_d > ylo_d else 1.0
    axd.set_ylim(ylo_d - pad_d, yhi_d + pad_d)
    cvd_ylim = axd.get_ylim()
    cvd_drawn = list(cvd_fibs)
    cvd_clipped = [
        item for item in cvd_fibs
        if not (cvd_ylim[0] - 1e-9 <= item[2] <= cvd_ylim[1] + 1e-9)
    ]
    handles_d, labels_d = axd.get_legend_handles_labels()
    uniq_d = dict(zip(labels_d, handles_d))
    axd.legend(
        uniq_d.values(), uniq_d.keys(),
        loc="upper left", fontsize=6.2, framealpha=0.9, handlelength=1.4,
        labelspacing=0.18, ncol=2,
    )
    axd.grid(True, alpha=0.28)

    # ----- σ CHANNEL -----
    ych = vis["sigma_channel_y"].to_numpy(dtype=float)
    t_ch = vis["t"]
    axc.fill_between(
        t_ch, 0, ych, where=(ych >= 0), step="post",
        color="#2ca02c", alpha=0.38, linewidth=0, interpolate=False, zorder=2,
    )
    axc.fill_between(
        t_ch, 0, ych, where=(ych < 0), step="post",
        color="#d62728", alpha=0.38, linewidth=0, interpolate=False, zorder=2,
    )
    axc.plot(
        t_ch, ych, color="0.18", lw=0.95, drawstyle="steps-post",
        label="σ channel", zorder=4,
    )
    axc.axhline(0, color="0.30", lw=0.9, zorder=5)
    axc.axvline(et_ts, color="#2ca02c", lw=0.7, alpha=0.35, zorder=2)
    if t_lift is not None:
        axc.axvline(t_lift, color=C_205_L, lw=0.7, ls=":", alpha=0.45, zorder=2)
    axc.axvline(xt_ts, color=xcol, lw=0.75, alpha=0.5, zorder=2)
    axc.set_ylabel("σ channel")
    axc.set_xlabel("Chicago clock")
    finite = ych[np.isfinite(ych)]
    if len(finite):
        ylo_c = float(np.min(finite))
        yhi_c = float(np.max(finite))
    else:
        ylo_c, yhi_c = -1.0, 1.0
    ylo_c = min(ylo_c, 0.0) - 0.5
    yhi_c = max(yhi_c, 0.0) + 0.5
    axc.set_ylim(ylo_c, yhi_c)
    tick0 = np.floor(ylo_c * 2.0) / 2.0
    tick1 = np.ceil(yhi_c * 2.0) / 2.0
    axc.set_yticks(np.arange(tick0, tick1 + 0.25, 0.5))
    axc.legend(loc="upper left", fontsize=6.4, framealpha=0.9, handlelength=1.4)
    axc.grid(True, alpha=0.28)

    ax.set_xlim(t0, t_right)
    axc.set_xticks(session_ticks(t0, t_right))
    axc.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    ylim_price = (float(ax.get_ylim()[0]), float(ax.get_ylim()[1]))
    ylim_cvd = (float(axd.get_ylim()[0]), float(axd.get_ylim()[1]))
    price_clipped = [
        item for item in price_fibs
        if not (ylim_price[0] - 1e-9 <= item[2] <= ylim_price[1] + 1e-9)
    ]
    fig.subplots_adjust(left=0.055, right=0.905, top=0.955, bottom=0.055, hspace=0.07)
    fig.savefig(out_path, dpi=DPI)
    plt.close(fig)
    return {
        "or_high": or_high,
        "or_low": or_low,
        "or_R": or_R,
        "long_127": long_127,
        "long_205": long_205,
        "short_127": short_127,
        "short_205": short_205,
        "cvd_high": cvd_high,
        "cvd_low": cvd_low,
        "r_cvd": r_cvd,
        "cvd_127_up": cvd_127_up,
        "cvd_205_up": cvd_205_up,
        "cvd_127_dn": cvd_127_dn,
        "cvd_205_dn": cvd_205_dn,
        "cvd_drawn": cvd_drawn,
        "cvd_clipped": cvd_clipped,
        "price_fibs": price_fibs,
        "price_clipped": price_clipped,
        "ylim_price": ylim_price,
        "ylim_cvd": ylim_cvd,
    }


def main() -> int:
    assert DB_PATH.name == "NQU26-CME-15s-1mo.duckdb"
    assert DB_PATH.exists()
    if not OUT_JSON.exists():
        raise SystemExit(f"missing locked fills {OUT_JSON}")
    facts = json.loads(OUT_JSON.read_text())
    # Guard: do not proceed if JSON fills drifted.
    assert facts["entry_bar"] == 25
    assert abs(float(facts["entry_price"]) - 27913.25) < 1e-9
    assert facts["entry_time_hm"] == "08:36:00"
    assert facts["lift"]["bar"] == 57
    assert abs(float(facts["lift"]["close"]) - 27990.00) < 1e-9
    assert facts["exit_bar"] == 1560
    assert abs(float(facts["exit_price"]) - 28229.00) < 1e-9
    assert facts["exit_reason"] == "session_end"
    assert abs(float(facts["net"]) - 315.25) < 1e-9

    g = load_day_series(DAY)
    g, sigma_meta = attach_vwap_sigma(g)
    at_entry = series_at(g, 25)
    at_lift = series_at(g, 57)
    at_close = series_at(g, 1560)

    CHART_DIR.mkdir(parents=True, exist_ok=True)
    or_report = plot_stacked(g, facts, OUT_PNG)
    if not OUT_PNG.exists() or OUT_PNG.stat().st_size < 1000:
        raise SystemExit(f"empty/missing chart {OUT_PNG}")

    dpoc = g["dpoc"].to_numpy(dtype=float)
    vd = g["volume_delta"].to_numpy(dtype=float)
    runs, n_pos, n_neg, n_zero = dpoc_shift_runs(dpoc, vd)
    n_shift = len(runs)
    bar_to_idx = {int(b): i for i, b in enumerate(g["rth_bar_number"].to_numpy())}

    def color_at_bar(bar: int):
        idx = bar_to_idx[bar]
        for i0, i1, c, d in runs:
            if i0 <= idx <= i1:
                shift_bar = int(g.iloc[i0]["rth_bar_number"])
                return c, d, shift_bar
        raise SystemExit(f"bar {bar} not in any dPOC run")

    c_entry, d_entry, sb_entry = color_at_bar(25)
    c_lift, d_lift, sb_lift = color_at_bar(57)
    c_close, d_close, sb_close = color_at_bar(1560)

    def sig_line(tag, at):
        sig = at["sigma"]
        z = at["z"]
        ch = at["channel"]
        sig_s = f"{sig:.6f}" if sig is not None else "nan"
        z_s = f"{z:.6f}" if z is not None else "nan"
        ch_s = f"{ch}" if ch is not None else "nan"
        y_s = f"{ch * 0.5:.1f}" if ch is not None else "nan"
        cv = at["computed_vwap"]
        cv_s = f"{cv:.4f}" if cv is not None else "nan"
        extra = (
            f"  cum_delta={at['cum_delta']:+d}"
            if at.get("cum_delta") is not None else ""
        )
        return (
            f"{tag} bar {at['bar']} {at['time_hm']}  "
            f"close={at['close']:.2f}  dVWAP={at['dvwap']:.4f}  computed_vwap={cv_s}  "
            f"sigma={sig_s}  z={z_s}  channel={ch_s} ({y_s}σ)  "
            f"dPOC={at['dpoc']:.2f}  vol={at['total_volume']}  delta={at['volume_delta']}"
            + extra
        )

    print(f"CHART {OUT_PNG} bytes={OUT_PNG.stat().st_size}")
    print(f"dPOC/dVWAP source: table {VA_SOURCE}")
    print(
        f"POC_SHIFTS n={n_shift}  delta+={n_pos}  delta-={n_neg}  delta0={n_zero}"
    )
    matched = sigma_meta["matched_within_tick"]
    print(
        f"VWAP_MATCH computed vs table dVWAP within_tick={matched}  "
        f"max_abs_diff={sigma_meta['max_abs_vwap_diff']:.6f}  "
        f"n_within_tick={sigma_meta['n_within_tick']}/{sigma_meta['n_bars']}  "
        f"center=table_dVWAP  sigma=classic_typical  "
        f"hug_var_zero_bars={sigma_meta['n_hug_zero']}"
    )
    print(sig_line("ENTRY", at_entry) + f"  dpoc_color={c_entry} shift_bar={sb_entry} shift_delta={d_entry:.0f}")
    print(sig_line("LIFT ", at_lift) + f"  dpoc_color={c_lift} shift_bar={sb_lift} shift_delta={d_lift:.0f}")
    print(sig_line("15:00", at_close) + f"  dpoc_color={c_close} shift_bar={sb_close} shift_delta={d_close:.0f}")
    print(f"CUM_DELTA entry bar 25 = {at_entry['cum_delta']:+d}")
    print(f"CUM_DELTA lift  bar 57 = {at_lift['cum_delta']:+d}")
    print(f"CUM_DELTA 15:00 bar 1560 = {at_close['cum_delta']:+d}")
    print(f"MAX_|z|={sigma_meta['max_abs_z']:.6f}")
    r = or_report
    print(
        f"PRICE_OR High={r['or_high']:.2f} Low={r['or_low']:.2f} R={r['or_R']:.2f}"
    )
    price_long = [(k, y) for k, side, y in r["price_fibs"] if side == "up"]
    price_short = [(k, y) for k, side, y in r["price_fibs"] if side == "down"]
    cvd_up = [(k, y) for k, side, y in r["cvd_drawn"] if side == "up"]
    cvd_dn = [(k, y) for k, side, y in r["cvd_drawn"] if side == "down"]
    print("PRICE_FIB_LONG  " + "  ".join(f"{k:g}={y:.2f}" for k, y in price_long))
    print("PRICE_FIB_SHORT " + "  ".join(f"{k:g}={y:.2f}" for k, y in price_short))
    print(
        f"CVD_OR bars1-20 High={r['cvd_high']:.4f} Low={r['cvd_low']:.4f} R_cvd={r['r_cvd']:.4f}"
    )
    print("CVD_FIB_UP      " + "  ".join(f"{k:g}={y:.4f}" for k, y in cvd_up))
    print("CVD_FIB_DOWN    " + "  ".join(f"{k:g}={y:.4f}" for k, y in cvd_dn))
    p_lo, p_hi = r["ylim_price"]
    d_lo, d_hi = r["ylim_cvd"]
    p_fibs = [y for _k, _s, y in r["price_fibs"]]
    d_fibs = [y for _k, _s, y in r["cvd_drawn"]]
    p_clip = r.get("price_clipped") or []
    d_clip = r.get("cvd_clipped") or []
    print(
        f"YLIM_PRICE lo={p_lo:.2f} hi={p_hi:.2f}  "
        f"fib_min={min(p_fibs):.2f} fib_max={max(p_fibs):.2f}  "
        f"clipped={len(p_clip)}"
    )
    print(
        f"YLIM_CVD   lo={d_lo:.4f} hi={d_hi:.4f}  "
        f"fib_min={min(d_fibs):.4f} fib_max={max(d_fibs):.4f}  "
        f"clipped={len(d_clip)}"
    )
    clip_s = ", ".join(
        f"{k:g}{'U' if side=='up' else 'D'}={y:.4f}" for k, side, y in d_clip
    ) or "(none)"
    print(f"CVD_FIBS_CLIPPED {clip_s}")
    print(f"CUM_DELTA_ENTRY_STILL {at_entry['cum_delta']:+d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
