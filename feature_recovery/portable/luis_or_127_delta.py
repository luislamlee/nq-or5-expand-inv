#!/usr/bin/env python3
"""NEW 5m-OR 1.27 + cumulative-delta gate + frozen OR-VA stop + 2.05/20m.

All 23 RTH days. New files only. Does not touch luis_or_system.py,
luis_vbp_one.py, luis_vbp_or.py, luis_vbp_va_stop.py, luis_vbp_or_va_stop.py.

LOCKED:
  Box: 5-minute OR only, 15s bars 1-20, 08:30-08:35 Chicago. R = High - Low.
  Expansions (his formula, tick-rounded 0.25):
    upside = HIGH + (k-1)*R, downside = LOW - (k-1)*R
    1.27 long  = High + 0.27*R
    1.27 short = Low  - 0.27*R
    2.05 long  = High + 1.05*R
    2.05 short = Low  - 1.05*R
  NOT the DuckDB fib table (sqrt_phi 1.272020 / 2.058171).

ENTRY after bar 20, walk 15s:
  Long candidate:  high >= 1.27 long.  Fill = 1.27 if open < 1.27 else open.
  Short candidate: low  <= 1.27 short. Fill = 1.27 if open > 1.27 else open.
  BOTH same bar: skip that bar.
  CUMULATIVE DELTA from RTH open: sum(volume_delta) bars 1..trigger inclusive
    (volume_delta == ask_volume-bid_volume; no column named delta).
    Long only if cum_delta > 0. Short only if cum_delta < 0. Zero = skip.
  1.27 tags with wrong delta: skip that print, keep watching, do not consume the day.
  ONE trade per day: first valid 1.27 + matching delta. After fill ignore later.
  LAST_ENTRY: entry_bar + 80 <= 1560 so LAST_ENTRY_BAR = 1480.

STOP: frozen OR 5m VBP 70% VA (value_area copied from luis_vbp_alts.py).
  Long stop = OR VAL. Short stop = OR VAH. Never trails.
  15s touch, gap-through at open. Reason or_va_stop.

EXITS, whichever first while in the trade:
  1) or_va_stop on every 15s before targets.
  2) 2.05 target (gap-through at open). Reason target_205.
  3) 20 minutes from entry: first 15s whose bar_end >= entry_time + 20min
     (also fires at entry_bar+80). Flatten at THAT 15s CLOSE. Reason time_20m.
  4) Still in at bar 1560: session_end at close.
  Dual 2.05+stop same 15s: target wins ONLY if open already beyond 2.05;
     elif open beyond stop, stop at open; else stop wins (protective). Documented.

COST 0.50 RT, 1 NQ, tick 0.25.
23 days cannot prove edge. Do not claim one.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, time as dtime, timedelta
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

from luis_vbp_or import (
    BARS_PER_5M,
    COST,
    DB_PATH,
    FLATTEN_BAR,
    LAST_ENTRY_BAR,
    OR_LAST_BAR,
    TICK,
    clock_hm,
    pick_poc,
    ts,
)

OUT_DIR = Path("/workspace/sierra")
CHART_DIR = OUT_DIR / "or_127_delta_charts"
VA_PCT = 0.70
HOLD_BARS = 80  # 20 min = 80 x 15s
NOSTOP_NET_KNOWN = 360.75
ORVA_NET_KNOWN = 230.75

DAYS = [
    date(2026, 7, 21), date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24),
    date(2026, 7, 27), date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30),
    date(2026, 7, 31), date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5),
    date(2026, 8, 6), date(2026, 8, 7), date(2026, 8, 10), date(2026, 8, 11),
    date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 17),
    date(2026, 8, 18), date(2026, 8, 19), date(2026, 8, 20),
]
LAST8_START = date(2026, 8, 11)


def _naive(x):
    if x is None:
        return None
    if isinstance(x, pd.Timestamp):
        x = x.to_pydatetime()
    if isinstance(x, datetime) and x.tzinfo is not None:
        return x.replace(tzinfo=None)
    return x


def _finite(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _fmt_px(x) -> str:
    if not _finite(x):
        return "nan"
    return f"{float(x):.2f}"


def round_tick(px: float, tick: float = TICK) -> float:
    q = px / tick
    if q >= 0:
        return float(math.floor(q + 0.5) * tick)
    return float(math.ceil(q - 0.5) * tick)


def expansion_level(or_high: float, or_low: float, k: float, side: str) -> float:
    """Luis formula: up = High+(k-1)*R, down = Low-(k-1)*R, tick-rounded."""
    r = or_high - or_low
    if side == "up":
        return round_tick(or_high + (k - 1.0) * r)
    return round_tick(or_low - (k - 1.0) * r)


def value_area(prices: np.ndarray, volumes: np.ndarray, poc: float, pct: float = VA_PCT):
    """70% VA, contiguous from POC. Adjacent profile rows; greater vol first;
    equal includes both. Returns (vah, val, va_vol) or nans if unusable.

    Copied from luis_vbp_alts.py (do not import; that file is a different book).
    """
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
        return nan, nan, 0.0
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


def load_days(db_path: Path, days: tuple[date, ...]):
    """bars_15s including volume_delta / bid/ask, plus footprint for OR VA."""
    day_list = ", ".join(f"DATE '{d.isoformat()}'" for d in days)
    con = duckdb.connect(str(db_path), read_only=True)
    cols = [r[0] for r in con.execute("DESCRIBE bars_15s").fetchall()]
    want = [
        "trading_day", "rth_bar_number", "bar_start_chicago", "bar_end_chicago",
        "open", "high", "low", "close", "total_volume",
    ]
    extra = []
    for c in ("volume_delta", "delta", "bid_volume", "ask_volume", "rth_cumulative_delta"):
        if c in cols:
            extra.append(c)
    sel = ", ".join(want + extra)
    bars = con.execute(
        f"""
        SELECT {sel}
        FROM bars_15s
        WHERE trading_day IN ({day_list})
        ORDER BY trading_day, rth_bar_number
        """
    ).fetchdf()
    fp = con.execute(
        f"""
        SELECT trading_day, rth_bar_number, price, total_volume
        FROM footprint_15s_by_price
        WHERE trading_day IN ({day_list})
        ORDER BY trading_day, rth_bar_number, price
        """
    ).fetchdf()
    con.close()
    bars["trading_day"] = pd.to_datetime(bars["trading_day"]).dt.date
    fp["trading_day"] = pd.to_datetime(fp["trading_day"]).dt.date
    bars["bar_start_chicago"] = pd.to_datetime(bars["bar_start_chicago"])
    bars["bar_end_chicago"] = pd.to_datetime(bars["bar_end_chicago"])
    return bars, fp, extra


def attach_bar_delta(g: pd.DataFrame) -> pd.DataFrame:
    """cum_delta = sum of per-bar delta from rth_bar 1 through this bar inclusive."""
    g = g.sort_values("rth_bar_number").copy()
    if "volume_delta" in g.columns:
        g["bar_delta"] = g["volume_delta"].astype("int64")
        src = "volume_delta"
    elif "delta" in g.columns:
        g["bar_delta"] = g["delta"].astype("int64")
        src = "delta"
    elif "ask_volume" in g.columns and "bid_volume" in g.columns:
        g["bar_delta"] = g["ask_volume"].astype("int64") - g["bid_volume"].astype("int64")
        src = "ask_volume-bid_volume"
    else:
        raise SystemExit("bars_15s has no delta / volume_delta / bid+ask volume")
    g["cum_delta"] = g["bar_delta"].cumsum().astype("int64")
    g.attrs["delta_src"] = src
    return g


def or_va_for_day(day: date, g: pd.DataFrame, fp: pd.DataFrame) -> dict:
    or_chunk = g[g["rth_bar_number"] <= OR_LAST_BAR]
    or_high = float(or_chunk["high"].max())
    or_low = float(or_chunk["low"].min())
    r = or_high - or_low
    cl = float(or_chunk.loc[or_chunk["rth_bar_number"] == OR_LAST_BAR, "close"].iloc[0])
    fday = fp[fp["trading_day"] == day]
    fchunk = fday[(fday["rth_bar_number"] >= 1) & (fday["rth_bar_number"] <= OR_LAST_BAR)]
    if len(fchunk):
        grp = fchunk.groupby("price", sort=True)["total_volume"].sum()
        prices = grp.index.to_numpy(dtype=float)
        volumes = grp.to_numpy(dtype=float)
        poc, poc_vol = pick_poc(prices, volumes, cl)
        vah, val, va_vol = value_area(prices, volumes, poc)
    else:
        poc, poc_vol = float("nan"), 0.0
        vah, val, va_vol = float("nan"), float("nan"), 0.0
    return {
        "or_high": or_high,
        "or_low": or_low,
        "R": r,
        "or_close": cl,
        "or_poc": poc,
        "or_poc_vol": poc_vol,
        "or_val": val,
        "or_vah": vah,
        "or_va_vol": va_vol,
        "long_127": expansion_level(or_high, or_low, 1.27, "up"),
        "short_127": expansion_level(or_high, or_low, 1.27, "down"),
        "long_205": expansion_level(or_high, or_low, 2.05, "up"),
        "short_205": expansion_level(or_high, or_low, 2.05, "down"),
    }


def or_va_stop_fill(side: str, open_px: float, stop: float) -> float:
    if side == "long":
        return float(stop) if open_px > stop else float(open_px)
    return float(stop) if open_px < stop else float(open_px)


def target_205_fill(side: str, open_px: float, tgt: float) -> float:
    if side == "long":
        return float(tgt) if open_px < tgt else float(open_px)
    return float(tgt) if open_px > tgt else float(open_px)


def tagged_stop(side: str, high: float, low: float, stop: float) -> bool:
    if not _finite(stop):
        return False
    if side == "long":
        return low <= stop + 1e-12
    return high >= stop - 1e-12


def tagged_target(side: str, high: float, low: float, tgt: float) -> bool:
    if not _finite(tgt):
        return False
    if side == "long":
        return high >= tgt - 1e-12
    return low <= tgt + 1e-12


def open_beyond_target(side: str, open_px: float, tgt: float) -> bool:
    if side == "long":
        return open_px >= tgt - 1e-12
    return open_px <= tgt + 1e-12


def open_beyond_stop(side: str, open_px: float, stop: float) -> bool:
    if not _finite(stop):
        return False
    if side == "long":
        return open_px <= stop + 1e-12
    return open_px >= stop - 1e-12


def simulate_day(day: date, g: pd.DataFrame, fp: pd.DataFrame):
    g = attach_bar_delta(g)
    lv = or_va_for_day(day, g, fp)
    or_high, or_low = lv["or_high"], lv["or_low"]
    or_val, or_vah = lv["or_val"], lv["or_vah"]
    long_127, short_127 = lv["long_127"], lv["short_127"]
    long_205, short_205 = lv["long_205"], lv["short_205"]
    R = lv["R"]
    delta_src = g.attrs.get("delta_src", "volume_delta")

    log = [
        f"{day.isoformat()} OR High={or_high:.2f} Low={or_low:.2f} R={R:.2f} "
        f"close={lv['or_close']:.2f}",
        f"  1.27 long={long_127:.2f} short={short_127:.2f}  "
        f"2.05 long={long_205:.2f} short={short_205:.2f}",
        f"  OR 5m POC={_fmt_px(lv['or_poc'])} VAL={_fmt_px(or_val)} VAH={_fmt_px(or_vah)}  "
        f"(FROZEN long-stop=OR VAL  short-stop=OR VAH)",
        f"  delta source={delta_src}  (cum from rth_bar 1 through trigger inclusive)",
    ]

    by_bn = {int(r.rth_bar_number): r for r in g.itertuples(index=False)}
    trades: list[dict] = []
    pos = None
    filled_day = False
    n_delta_skip = 0
    n_both_skip = 0
    n_127_single = 0
    n_127_any = 0
    skip_notes: list[str] = []

    def open_trade(side: str, row, fill: float, cum: int) -> None:
        nonlocal pos, filled_day
        bn = int(row.rth_bar_number)
        stop = or_val if side == "long" else or_vah
        tgt = long_205 if side == "long" else short_205
        lvl = long_127 if side == "long" else short_127
        pos = {
            "side": side,
            "entry_bar": bn,
            "entry_time": row.bar_start_chicago,
            "entry_price": float(fill),
            "stop": stop,
            "target_205": tgt,
            "lvl_127": lvl,
            "cum_delta_entry": int(cum),
            "or_val": or_val,
            "or_vah": or_vah,
        }
        filled_day = True
        log.append(
            f"  ENTRY {side.upper()} bar {bn} {clock_hm(row.bar_start_chicago)} "
            f"fill={fill:.2f} open={row.open:.2f} H={row.high:.2f} L={row.low:.2f} "
            f"1.27={lvl:.2f} 2.05={tgt:.2f} stop={_fmt_px(stop)} "
            f"cum_delta={cum} (thru bar {bn} incl)"
        )

    def close_trade(row, exit_px: float, reason: str, path_note: str = "") -> None:
        nonlocal pos
        side = pos["side"]
        gross = (exit_px - pos["entry_price"]) if side == "long" else (pos["entry_price"] - exit_px)
        net = gross - COST
        t_exit = row.bar_end_chicago
        et = _naive(pos["entry_time"])
        xt = _naive(t_exit)
        hold_min = (xt - et).total_seconds() / 60.0 if (et and xt) else float("nan")
        rec = {
            "day": day.isoformat(),
            "side": side,
            "entry_time": ts(pos["entry_time"]),
            "entry_price": round(pos["entry_price"], 4),
            "exit_time": ts(t_exit),
            "exit_price": round(float(exit_px), 4),
            "exit_reason": reason,
            "or_high": or_high,
            "or_low": or_low,
            "R": round(float(R), 4),
            "long_127": long_127,
            "short_127": short_127,
            "long_205": long_205,
            "short_205": short_205,
            "or_val": round(float(or_val), 4) if _finite(or_val) else float("nan"),
            "or_vah": round(float(or_vah), 4) if _finite(or_vah) else float("nan"),
            "or_poc": round(float(lv["or_poc"]), 4) if _finite(lv["or_poc"]) else float("nan"),
            "stop": round(float(pos["stop"]), 4) if _finite(pos["stop"]) else float("nan"),
            "target_205": round(float(pos["target_205"]), 4),
            "lvl_127": round(float(pos["lvl_127"]), 4),
            "cum_delta_entry": int(pos["cum_delta_entry"]),
            "net": round(float(net), 4),
            "gross": round(float(gross), 4),
            "entry_bar": pos["entry_bar"],
            "exit_bar": int(row.rth_bar_number),
            "hold_minutes": round(float(hold_min), 4) if _finite(hold_min) else float("nan"),
            "n_delta_skip_before_fill": n_delta_skip,
            "path_note": path_note,
            "trade_index_in_day": 1,
        }
        trades.append(rec)
        extra = f" {path_note}" if path_note else ""
        log.append(
            f"  EXIT  {side.upper()} bar {int(row.rth_bar_number)} {clock_hm(t_exit)} "
            f"fill={exit_px:.2f} reason={reason} stop={_fmt_px(pos['stop'])} "
            f"2.05={pos['target_205']:.2f} hold={hold_min:.2f}m "
            f"gross={gross:.2f} net={net:.2f}{extra}"
        )
        pos = None

    def time_hit(row, entry_bar: int, entry_time) -> bool:
        bn = int(row.rth_bar_number)
        if bn >= entry_bar + HOLD_BARS:
            return True
        t_end = _naive(row.bar_end_chicago)
        t_ent = _naive(entry_time)
        if t_end is not None and t_ent is not None and t_end >= t_ent + timedelta(minutes=20):
            return True
        return False

    for bn in range(OR_LAST_BAR + 1, FLATTEN_BAR + 1):
        row = by_bn.get(bn)
        if row is None:
            continue
        o, h, l, c = float(row.open), float(row.high), float(row.low), float(row.close)
        cum = int(row.cum_delta)

        if pos is None and not filled_day and bn <= LAST_ENTRY_BAR:
            long_tag = h >= long_127 - 1e-12
            short_tag = l <= short_127 + 1e-12
            if long_tag or short_tag:
                n_127_any += 1
            if long_tag and short_tag:
                n_both_skip += 1
                log.append(
                    f"  SKIP both-sides bar {bn} {clock_hm(row.bar_start_chicago)} "
                    f"H={h:.2f} L={l:.2f} 1.27L={long_127:.2f} 1.27S={short_127:.2f} "
                    f"cum_delta={cum}"
                )
            elif long_tag:
                n_127_single += 1
                if cum > 0:
                    fill = long_127 if o < long_127 else o
                    open_trade("long", row, fill, cum)
                else:
                    n_delta_skip += 1
                    note = (
                        f"bar {bn} {clock_hm(row.bar_start_chicago)} LONG 1.27 "
                        f"H={h:.2f}>={long_127:.2f} but cum_delta={cum} (need >0)"
                    )
                    skip_notes.append(note)
                    log.append("  SKIP delta " + note)
            elif short_tag:
                n_127_single += 1
                if cum < 0:
                    fill = short_127 if o > short_127 else o
                    open_trade("short", row, fill, cum)
                else:
                    n_delta_skip += 1
                    note = (
                        f"bar {bn} {clock_hm(row.bar_start_chicago)} SHORT 1.27 "
                        f"L={l:.2f}<={short_127:.2f} but cum_delta={cum} (need <0)"
                    )
                    skip_notes.append(note)
                    log.append("  SKIP delta " + note)

        if pos is not None:
            side = pos["side"]
            stop = pos["stop"]
            tgt = pos["target_205"]
            hit_stop = tagged_stop(side, h, l, stop)
            hit_tgt = tagged_target(side, h, l, tgt)
            if hit_tgt and hit_stop:
                if open_beyond_target(side, o, tgt):
                    close_trade(row, float(o), "target_205",
                                "dual_tag: open already beyond 2.05 → target at open")
                elif open_beyond_stop(side, o, stop):
                    close_trade(row, float(o), "or_va_stop",
                                "dual_tag: open already beyond stop → stop at open")
                else:
                    fill = float(stop)  # open is between; stop wins, fill at stop
                    close_trade(row, fill, "or_va_stop",
                                "dual_tag: both wicks, open between → stop wins (protective)")
            elif hit_stop:
                fill = or_va_stop_fill(side, o, stop)
                close_trade(row, fill, "or_va_stop")
            elif hit_tgt:
                fill = target_205_fill(side, o, tgt)
                close_trade(row, fill, "target_205")
            elif time_hit(row, pos["entry_bar"], pos["entry_time"]):
                close_trade(row, c, "time_20m")
            elif bn == FLATTEN_BAR:
                close_trade(row, c, "session_end")

    if not trades:
        if n_127_single == 0:
            why = "no_tag"
            log.append("  NO TRADE (no_tag: no single-side 1.27 after bar 20)")
        else:
            why = "delta_block"
            log.append(
                f"  NO TRADE (delta_block: {n_127_single} single-side 1.27 tag(s), "
                f"{n_delta_skip} skipped for wrong/zero cum_delta)"
            )
    else:
        why = ""

    meta = {
        "day": day.isoformat(),
        "n_delta_skip": n_delta_skip,
        "n_both_skip": n_both_skip,
        "n_127_single": n_127_single,
        "n_127_any": n_127_any,
        "why": why,
        "skip_notes": skip_notes,
        "levels": lv,
        "delta_src": delta_src,
    }
    return trades, log, meta


def caption_sentence(day: date, tr: dict | None, lv: dict, why: str) -> str:
    if tr is None:
        if why == "delta_block":
            return (
                f"1.27 tagged but cum_delta sign never matched "
                f"(long 1.27={lv['long_127']:.2f} / short 1.27={lv['short_127']:.2f}); no trade."
            )
        return (
            f"No 15s after 08:35 tagged 1.27 long {lv['long_127']:.2f} or "
            f"short {lv['short_127']:.2f}; no trade."
        )
    side = tr["side"].upper()
    which = "OR VAL" if tr["side"] == "long" else "OR VAH"
    return (
        f"1.27 {side} {clock_hm(tr['entry_time'])} @ {tr['entry_price']:.2f} "
        f"(cum_delta={tr['cum_delta_entry']:+d}) → "
        f"{clock_hm(tr['exit_time'])} @ {tr['exit_price']:.2f} "
        f"{tr['exit_reason']} net {tr['net']:+.2f}. "
        f"frozen stop {which}={_fmt_px(tr['stop'])}; "
        f"2.05={tr['target_205']:.2f}; hold {tr['hold_minutes']:.1f}m."
    )


def draw_panel(ax, day, g, tday, t_left, t_right, lv, title: str) -> None:
    t0 = datetime.combine(day, dtime(8, 30))
    box_end = datetime.combine(day, dtime(8, 35))
    vis = g[(g["t"] >= t_left - timedelta(minutes=1)) & (g["t"] <= t_right + timedelta(minutes=1))]
    ax.fill_between(
        vis["t"], vis["low"], vis["high"], color="0.82", alpha=0.55,
        linewidth=0, label="15s range", zorder=1,
    )
    ax.plot(vis["t"], vis["close"], color="0.18", lw=0.9, label="15s close", zorder=3)

    or_high, or_low = lv["or_high"], lv["or_low"]
    x0 = mdates.date2num(t0)
    x1 = mdates.date2num(box_end)
    ax.add_patch(Rectangle(
        (x0, or_low), x1 - x0, or_high - or_low,
        facecolor="#ffcc00", edgecolor="#aa8800", linewidth=1.15, alpha=0.48,
        zorder=2, label="5m OR box",
    ))
    ax.axhline(or_high, color="#aa8800", lw=1.15, zorder=3)
    ax.axhline(or_low, color="#aa8800", lw=1.0, zorder=3)

    ax.axhline(lv["long_127"], color="#2ca02c", lw=1.25, ls="--",
               label="1.27 long", zorder=4)
    ax.axhline(lv["short_127"], color="#d62728", lw=1.25, ls="--",
               label="1.27 short", zorder=4)
    ax.axhline(lv["long_205"], color="#006400", lw=1.35, ls=":",
               label="2.05 long", zorder=4)
    ax.axhline(lv["short_205"], color="#8b0000", lw=1.35, ls=":",
               label="2.05 short", zorder=4)

    live_stop = None
    live_side = None
    if tday:
        live_stop = tday[0].get("stop")
        live_side = tday[0].get("side")

    or_val, or_vah = lv["or_val"], lv["or_vah"]
    if _finite(or_val):
        is_stop = live_side == "long"
        ax.axhline(
            float(or_val), color="#e31a1c" if is_stop else "#3182bd",
            lw=2.15 if is_stop else 1.15, ls="--",
            label="OR VAL (long stop)" if is_stop else "OR VAL",
            zorder=5,
        )
    if _finite(or_vah):
        is_stop = live_side == "short"
        ax.axhline(
            float(or_vah), color="#e31a1c" if is_stop else "#3182bd",
            lw=2.15 if is_stop else 1.15, ls=":" if is_stop else "-",
            label="OR VAH (short stop)" if is_stop else "OR VAH",
            zorder=5,
        )

    used: set[str] = set()

    def mark(xs, ys, marker, color, size, label, z=8):
        lab = label if label not in used else None
        if lab:
            used.add(label)
        kw = dict(marker=marker, color=color, s=size, zorder=z, label=lab, linewidths=1.2)
        if marker != "x":
            kw["edgecolors"] = "white"
            kw["linewidths"] = 0.4
        ax.scatter(xs, ys, **kw)

    y_extra = [or_high, or_low, lv["long_127"], lv["short_127"], lv["long_205"], lv["short_205"]]
    if _finite(or_val):
        y_extra.append(float(or_val))
    if _finite(or_vah):
        y_extra.append(float(or_vah))
    if _finite(live_stop):
        y_extra.append(float(live_stop))

    for tr in tday:
        et = pd.Timestamp(_naive(pd.Timestamp(tr["entry_time"])))
        xt = pd.Timestamp(_naive(pd.Timestamp(tr["exit_time"])))
        if xt < t_left or et > t_right:
            continue
        side = tr["side"]
        col = "#2ca02c" if side == "long" else "#d62728"
        mk = "^" if side == "long" else "v"
        mark([et], [tr["entry_price"]], mk, col, 90, f"{side} entry")
        ax.axvline(et, color=col, lw=0.7, alpha=0.35, zorder=2)
        reason = tr["exit_reason"]
        if reason == "or_va_stop":
            xcol = "#e31a1c"
        elif reason == "target_205":
            xcol = "#006400"
        elif reason == "time_20m":
            xcol = "#6a3d9a"
        else:
            xcol = "#ff7f0e"
        mark([xt], [tr["exit_price"]], "x", xcol, 95, reason)
        ax.axvline(xt, color=xcol, lw=0.75, alpha=0.5, zorder=2)
        ax.annotate(
            f"{side[0].upper()} {reason}\nnet {tr['net']:+.2f}",
            xy=(xt, tr["exit_price"]),
            xytext=(6, -16 if side == "long" else 10),
            textcoords="offset points",
            fontsize=7,
            color=col,
            zorder=9,
        )

    ax.set_xlim(t_left, t_right)
    ticks = []
    t = t_left.replace(second=0, microsecond=0)
    t = t.replace(minute=(t.minute // 5) * 5)
    if t < t_left:
        t += timedelta(minutes=5)
    span_min = (t_right - t_left).total_seconds() / 60.0
    step = 5 if span_min <= 90 else (15 if span_min <= 180 else 30)
    while t <= t_right:
        ticks.append(t)
        t += timedelta(minutes=step)
    ax.set_xticks(ticks)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_xlabel("Chicago clock")
    ax.set_ylabel("NQU26")

    y_lo = min(float(vis["low"].min()), min(y_extra))
    y_hi = max(float(vis["high"].max()), max(y_extra))
    pad_y = (y_hi - y_lo) * 0.06 if y_hi > y_lo else 8.0
    ax.set_ylim(y_lo - pad_y, y_hi + pad_y)
    ax.set_title(title, fontsize=10, pad=7)
    handles, labels = ax.get_legend_handles_labels()
    uniq = dict(zip(labels, handles))
    ax.legend(
        uniq.values(), uniq.keys(),
        loc="best", fontsize=6.6, framealpha=0.9,
        handlelength=1.7, labelspacing=0.24,
    )
    ax.grid(True, alpha=0.28)


def plot_one(day: date, g: pd.DataFrame, trades: list[dict], lv: dict,
             why: str, out_path: Path) -> None:
    g = g.sort_values("rth_bar_number").copy()
    g["t"] = pd.to_datetime(g["bar_start_chicago"])
    if getattr(g["t"].dt, "tz", None) is not None:
        g["t"] = g["t"].dt.tz_localize(None)
    t0 = datetime.combine(day, dtime(8, 30))
    t_sess_end = datetime.combine(day, dtime(15, 0))
    t_ten = datetime.combine(day, dtime(10, 0))
    tday = [t for t in trades if t["day"] == day.isoformat()][:1]

    if tday:
        tr = tday[0]
        exit_t = _naive(pd.Timestamp(tr["exit_time"]))
        pad = timedelta(minutes=20) if tr["side"] == "short" else timedelta(minutes=15)
        t_right = min(max(exit_t + pad, t_ten), t_sess_end)
        if t_right <= t0:
            t_right = min(t0 + timedelta(minutes=30), t_sess_end)
        which = "OR VAL" if tr["side"] == "long" else "OR VAH"
        title = (
            f"{day.isoformat()}  ·  {tr['side'].upper()}  ·  "
            f"net {tr['net']:+.2f}  ·  "
            f"{clock_hm(tr['entry_time'])} → {clock_hm(tr['exit_time'])}  "
            f"{tr['exit_reason']}  Δ={tr['cum_delta_entry']:+d}  "
            f"frozen {which}={_fmt_px(tr['stop'])}"
        )
    else:
        t_right = t_ten
        title = f"{day.isoformat()}  ·  NONE  ·  net +0.00  ·  {why or 'no trade'}"

    fig, ax = plt.subplots(figsize=(14.5, 7.2), dpi=120)
    draw_panel(ax, day, g, tday, t0, t_right, lv, title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_equity(day_nets: pd.Series, out_path: Path, n_trades: int, net: float) -> None:
    x = pd.to_datetime(list(day_nets.index))
    cum = day_nets.cumsum()
    fig, ax = plt.subplots(figsize=(11.2, 5.6), dpi=120)
    ax.plot(x, cum.to_numpy(), marker="o", ms=4.5, color="#1f77b4", lw=1.6,
            label=f"1.27+delta n={n_trades}  net={net:+.2f}")
    ax.axhline(0, color="0.5", lw=0.8)
    ax.set_xlabel("Session date")
    ax.set_ylabel("Cumulative NET points")
    ax.set_title(
        "5m-OR 1.27 + cum-delta gate + frozen-OR-VA stop + 2.05/20m  ·  cum NET  ·  23 days, not an edge"
    )
    ax.legend(loc="best", fontsize=8.5)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def max_dd(cum: np.ndarray) -> float:
    if len(cum) == 0:
        return 0.0
    peak = np.maximum.accumulate(cum)
    dd = cum - peak
    return float(dd.min())


def _pf_txt(pf: float) -> str:
    if not _finite(pf):
        return "n/a"
    if math.isinf(pf):
        return "inf"
    return f"{pf:.2f}"


def _avg_txt(x: float) -> str:
    if not _finite(x):
        return "n/a"
    return f"{x:+.2f}"


def write_md(rows: list[dict], tdf: pd.DataFrame, day_nets: pd.Series,
             metas: list[dict], delta_src: str, path: Path) -> None:
    n = int(len(tdf))
    n_none = sum(1 for r in rows if r["side"] == "NONE")
    n_no_tag = sum(1 for r in rows if r.get("why") == "no_tag")
    n_delta_block = sum(1 for r in rows if r.get("why") == "delta_block")
    n_delta_skip_all = int(sum(m["n_delta_skip"] for m in metas))
    net = float(tdf["net"].sum()) if n else 0.0
    gross = float(tdf["gross"].sum()) if n else 0.0
    hits = int((tdf["net"] > 0).sum()) if n else 0
    hit_txt = f"{hits}/{n}" if n else "0/0"
    reasons = tdf["exit_reason"].value_counts().to_dict() if n else {}
    n_tgt = int(reasons.get("target_205", 0))
    n_time = int(reasons.get("time_20m", 0))
    n_stop = int(reasons.get("or_va_stop", 0))
    n_end = int(reasons.get("session_end", 0))
    cum = day_nets.cumsum().to_numpy()
    dd = max_dd(cum)

    last8_mask = tdf["day"].apply(lambda s: date.fromisoformat(str(s)) >= LAST8_START) if n else pd.Series(dtype=bool)
    t_l8 = tdf[last8_mask] if n else tdf
    n_l8 = int(len(t_l8))
    net_l8 = float(t_l8["net"].sum()) if n_l8 else 0.0

    longs = tdf[tdf["side"] == "long"] if n else tdf
    shorts = tdf[tdf["side"] == "short"] if n else tdf
    n_long = int(len(longs))
    n_short = int(len(shorts))
    net_long = float(longs["net"].sum()) if n_long else 0.0
    net_short = float(shorts["net"].sum()) if n_short else 0.0
    wins = tdf.loc[tdf["net"] > 0, "net"] if n else pd.Series(dtype=float)
    losses = tdf.loc[tdf["net"] <= 0, "net"] if n else pd.Series(dtype=float)
    avg_win = float(wins.mean()) if len(wins) else float("nan")
    avg_loss = float(losses.mean()) if len(losses) else float("nan")
    win_sum = float(wins.sum()) if len(wins) else 0.0
    lose_sum = float(losses.sum()) if len(losses) else 0.0
    if lose_sum < 0:
        pf = win_sum / abs(lose_sum)
    elif win_sum > 0:
        pf = float("inf")
    else:
        pf = float("nan")
    best = float(tdf["net"].max()) if n else 0.0
    worst = float(tdf["net"].min()) if n else 0.0
    best_day = str(tdf.loc[tdf["net"].idxmax(), "day"]) if n else ""
    worst_day = str(tdf.loc[tdf["net"].idxmin(), "day"]) if n else ""
    med_hold = float(tdf["hold_minutes"].median()) if n else float("nan")

    rest_n = n
    if n == 0:
        verdict = "NOISE"
        verdict_why = "No trades in 23 days. The 1.27+delta gate never armed."
    elif n <= 4 and abs(net) < 80:
        verdict = "NOISE"
        verdict_why = (
            f"Only {n} fills in 23 days, net {net:+.2f}. Too thin to say anything. "
            "23 days cannot prove edge."
        )
    elif n_tgt >= max(1, n // 2) and net > 50 and pf >= 1.3 and n_l8 >= 3 and net_l8 > 0:
        verdict = "MIXED"
        verdict_why = (
            f"Target_205 is doing real work ({n_tgt}/{n}) and last-8 is not dead "
            f"({net_l8:+.2f}), net {net:+.2f}, PF {_pf_txt(pf)}. Still a 23-day slice "
            f"with a new entry vs the OR-break books. Not an edge."
        )
    elif net > 30 and n >= 8 and (n_stop + n_time) >= n_tgt and pf < 1.2:
        verdict = "WEAK"
        verdict_why = (
            f"Net {net:+.2f} on {n} days but mix is mostly time/stop "
            f"(target_205 {n_tgt} / time_20m {n_time} / or_va_stop {n_stop} / session_end {n_end}), "
            f"PF {_pf_txt(pf)}. 23 days cannot prove edge."
        )
    elif net < -30 or (n_l8 >= 3 and net_l8 < -20):
        verdict = "WEAK"
        tgt_net = float(tdf.loc[tdf["exit_reason"]=="target_205","net"].sum()) if n else 0.0
        time_net = float(tdf.loc[tdf["exit_reason"]=="time_20m","net"].sum()) if n else 0.0
        stop_net = float(tdf.loc[tdf["exit_reason"]=="or_va_stop","net"].sum()) if n else 0.0
        verdict_why = (
            f"Full-slice net {net:+.2f} is {n_tgt} target_205 hits ({tgt_net:+.2f}) "
            f"minus {n_time} time_20m clocks ({time_net:+.2f}) and {n_stop} frozen-OR-VA "
            f"stops ({stop_net:+.2f}). Last-8 from 2026-08-11 is {net_l8:+.2f}. "
            f"The delta gate skipped {n_delta_skip_all} 15s prints but never blocked a day "
            f"(n={n}/23, no_tag {n_no_tag}, delta_block {n_delta_block}). "
            "Front-loaded; 23 days cannot prove edge."
        )
    elif net > 0 and n_l8 >= 3 and net_l8 <= 0:
        verdict = "WEAK"
        verdict_why = (
            f"Full-slice net {net:+.2f} but last-8 from 2026-08-11 is {net_l8:+.2f}. "
            "Front-loaded. 23 days cannot prove edge."
        )
    else:
        verdict = "MIXED"
        verdict_why = (
            f"n={n} net {net:+.2f} hit {hit_txt} PF {_pf_txt(pf)} "
            f"mix target_205 {n_tgt} / time_20m {n_time} / or_va_stop {n_stop} / session_end {n_end}. "
            "Small sample, different entry than the OR-break books. Not an edge."
        )

    # sanity math day
    math_day = "2026-08-06"
    math_row = next((r for r in rows if r["date"] == math_day), None)
    math_meta = next((m for m in metas if m["day"] == math_day), None)

    lines = [
        "# 5m OR 1.27 + cum-delta gate + frozen OR-VA stop + 2.05/20m — 23 days",
        "",
        "Not an edge. New entry (first 1.27 of the 5-minute OR, cumulative-delta sign gate) "
        "plus a frozen Opening Range 5m value-area stop, a 2.05 expansion target, and a "
        "hard 20-minute clock. 23 complete RTH sessions. Do not trade this from this file.",
        "",
        f"Context (different entry, **not** an apples-to-apples edge claim): prior one-trade "
        f"POC-hold no-stop **+{NOSTOP_NET_KNOWN:.2f}**; frozen-OR-VA on that book **+{ORVA_NET_KNOWN:.2f}**.",
        "",
        "## Locked rule",
        "",
        "- Box: 5-minute OR only, 15s bars 1–20 (08:30–08:35 Chicago). R = OR High − OR Low.",
        "- Expansions, **his** formula (not the DuckDB fib table of √φ≈1.272 / φ²≈2.058): "
        "upside = High + (k−1)·R, downside = Low − (k−1)·R, then round to tick 0.25. "
        "1.27 long = High+0.27R, 1.27 short = Low−0.27R, 2.05 long = High+1.05R, 2.05 short = Low−1.05R.",
        "- After bar 20: walk 15s. Long candidate if 15s high ≥ 1.27 long; fill = 1.27 if open < 1.27 else open "
        "(gap through). Short candidate if 15s low ≤ 1.27 short; fill = 1.27 if open > 1.27 else open. "
        "BOTH same bar: skip that bar.",
        f"- Cumulative delta gate, session from RTH open. `bars_15s` has no column named `delta`; "
        f"used **`{delta_src}`** (equals ask_volume−bid_volume). "
        "cum_delta = sum from rth_bar 1 **through the trigger bar inclusive**. "
        "Long only if cum_delta > 0. Short only if cum_delta < 0. Zero = skip (strict).",
        "- If 1.27 tags but delta sign is wrong: skip that print, keep watching, do not consume the day.",
        "- ONE trade per day: first valid 1.27 tag with matching delta. After fill, ignore later breaks.",
        "- No new entry after a time that cannot complete 20 minutes before bar 1560 / 15:00 Chicago. "
        f"LAST_ENTRY_BAR = {LAST_ENTRY_BAR} (entry_bar + {HOLD_BARS} ≤ {FLATTEN_BAR}).",
        "",
        "## Stop (frozen OR 5m VA)",
        "",
        "- 70% value area of the **OR 5m only** (k=1, bars 1–20). `value_area()` copied from "
        "`luis_vbp_alts.py` (`VA_PCT=0.70`): contiguous from that 5m POC, adjacent profile rows, "
        "greater volume first, equal includes both. POC = max footprint `total_volume`; tie closest "
        "to the 5m close, then lower (`pick_poc` from `luis_vbp_or.py`).",
        "- LONG stop = OR VAL. SHORT stop = OR VAH. **FROZEN**. Never trails.",
        "- 15s touch: long low ≤ stop fill stop (or open if gapped through); short high ≥ stop fill stop "
        "(or open if gapped). Reason `or_va_stop`. Checked on every 15s **before** targets.",
        "",
        "## Exits, whichever first while in the trade",
        "",
        "1. `or_va_stop` as above.",
        "2. 2.05 target: long high ≥ 2.05 fill 2.05 (or open if gapped); short low ≤ 2.05 fill 2.05 "
        "(or open if gapped). Reason `target_205`.",
        "3. 20 minutes from entry: on the 15s bar whose `bar_end` ≥ entry_time + 20 minutes "
        f"(entry_time = that 15s `bar_start_chicago`), **or** `entry_bar + {HOLD_BARS}`. "
        "Whichever fires first. Flatten at **that 15s CLOSE**. Reason `time_20m`. "
        f"`entry_bar+{HOLD_BARS}` is one 15s later than the clock from `bar_start` "
        "(clock hits at +79 bars / 20.00 min; +80 is 20 min from the entry bar's **end**). "
        "Both are armed; the earlier one wins, so the clock from entry `bar_start` is the 20-minute hold.",
        "4. Still in at bar 1560: `session_end` at close. If the 20-minute clock also completes on 1560, "
        "`time_20m` wins (checked first).",
        "",
        "**Dual 2.05 + stop on the same 15s** (no tick path): target wins ONLY if the bar **opens already "
        "beyond 2.05** (fill at open). Elif it opens already beyond the stop, stop at open. "
        "Else both wicks with open between them → **stop wins** (protective), fill at the stop. "
        "Luis did not specify the wick-vs-wick case; this is the conservative rule, documented here.",
        "",
        "COST 0.50 RT, 1 NQ, tick 0.25.",
        "",
        "## Headline (do not overweight)",
        "",
        "| | |",
        "|---|---|",
        f"| days | 23 (2026-07-21 .. 2026-08-20) |",
        f"| n (days traded) | **{n}** |",
        f"| days no trade | **{n_none}** (no_tag {n_no_tag} / delta_block {n_delta_block}) |",
        f"| 1.27 tags skipped for wrong delta | **{n_delta_skip_all}** (15s prints, pre-fill) |",
        f"| hit (net>0) | **{hit_txt}** |",
        f"| net | **{net:+.2f}** |",
        f"| gross | {gross:+.2f} |",
        f"| max DD (session-date cum NET) | **{dd:.2f}** |",
        f"| mix | target_205 {n_tgt} / time_20m {n_time} / or_va_stop {n_stop} / session_end {n_end} |",
        f"| avg win / avg loss | {_avg_txt(avg_win)} / {_avg_txt(avg_loss)} |",
        f"| PF (sum wins / \\|sum losses\\|) | {_pf_txt(pf)} |",
        f"| best / worst | {best:+.2f} ({best_day}) / {worst:+.2f} ({worst_day}) |" if n else "| best / worst | n/a |",
        f"| last-8 from 2026-08-11 | n={n_l8} net {net_l8:+.2f} |",
        f"| long vs short | {n_long} / {n_short}  net {net_long:+.2f} / {net_short:+.2f} |",
        f"| median hold minutes | {med_hold:.2f} |" if n else "| median hold minutes | n/a |",
        "",
        f"**Verdict: {verdict}.** {verdict_why}",
        "",
        "23 days cannot prove edge. Do not trade this from this file.",
        "",
        "## Sanity: 1.27 / 2.05 math (2026-08-06)",
        "",
    ]
    if math_meta:
        lv = math_meta["levels"]
        raw_127u = lv["or_high"] + 0.27 * lv["R"]
        raw_127d = lv["or_low"] - 0.27 * lv["R"]
        raw_205u = lv["or_high"] + 1.05 * lv["R"]
        raw_205d = lv["or_low"] - 1.05 * lv["R"]
        lines += [
            f"OR High={lv['or_high']:.2f} Low={lv['or_low']:.2f} R={lv['R']:.2f}.",
            f"- 1.27 long raw High+0.27R = {raw_127u:.4f} → tick {lv['long_127']:.2f}",
            f"- 1.27 short raw Low−0.27R = {raw_127d:.4f} → tick {lv['short_127']:.2f}",
            f"- 2.05 long raw High+1.05R = {raw_205u:.4f} → tick {lv['long_205']:.2f}",
            f"- 2.05 short raw Low−1.05R = {raw_205d:.4f} → tick {lv['short_205']:.2f}",
            f"- OR 5m POC={_fmt_px(lv['or_poc'])} VAL={_fmt_px(lv['or_val'])} VAH={_fmt_px(lv['or_vah'])}.",
            "DuckDB `opening_range_5m_levels` uses √φ≈1.272020 / ≈2.058171 and is **not** this book "
            "(that table's UP 1.27 is 29409.75 vs ours 29409.50 on this day).",
            "",
        ]
        if math_row and math_row["side"] != "NONE":
            lines.append(
                f"Trade: {math_row['side'].upper()} {clock_hm(math_row['entry_time'])} @ "
                f"{math_row['entry_price']:.2f} cum_delta={math_row['cum_delta_entry']:+d} → "
                f"{clock_hm(math_row['exit_time'])} @ {math_row['exit_price']:.2f} "
                f"{math_row['reason']} net {math_row['net']:+.2f}."
            )
        elif math_row:
            lines.append(f"No trade that day ({math_row.get('why', 'none')}).")
    lines += [
        "",
        "## Sanity: cum_delta at entry (a few days)",
        "",
        "Gate is strict: long needs cum_delta>0, short needs cum_delta<0, through the trigger bar.",
        "",
    ]
    shown = 0
    for r in rows:
        if r["side"] == "NONE":
            continue
        lines.append(
            f"- **{r['date']}** {r['side'].upper()} bar {r.get('entry_bar', '?')} "
            f"{clock_hm(r['entry_time'])} fill {r['entry_price']:.2f} "
            f"cum_delta=**{int(r['cum_delta_entry']):+d}** "
            f"→ {r['reason']} net {r['net']:+.2f}"
        )
        shown += 1
        if shown >= 6:
            break
    if shown == 0:
        lines.append("No entries, so the gate never passed. See delta_block days in the table.")

    lines += [
        "",
        "## Every day",
        "",
        "| date | side | entry | exit | net | reason | cumΔ | hold_m | why if none |",
        "|---|---|---|---|---:|---|---:|---:|---|",
    ]
    for r in rows:
        if r["side"] == "NONE":
            lines.append(
                f"| {r['date']} | NONE | — | — | +0.00 | — | — | — | {r.get('why', 'no_tag')} |"
            )
        else:
            lines.append(
                f"| {r['date']} | {r['side'].upper()} | "
                f"{clock_hm(r['entry_time'])} @ {r['entry_price']:.2f} | "
                f"{clock_hm(r['exit_time'])} @ {r['exit_price']:.2f} | "
                f"{r['net']:+.2f} | {r['reason']} | {int(r['cum_delta_entry']):+d} | "
                f"{r['hold_minutes']:.2f} | — |"
            )
    lines += [
        "",
        f"1.27 single-side prints skipped for wrong/zero cum_delta (pre-fill, all 23 days): "
        f"**{n_delta_skip_all}**.",
        "",
        "Charts: `/workspace/sierra/or_127_delta_charts/D127_YYYY-MM-DD.png` (all 23, including no-trade "
        "08:30–10:00 zooms). Equity: `/workspace/sierra/luis_or_127_delta_equity.png`. "
        "Trades: `/workspace/sierra/luis_or_127_delta_trades.csv`. "
        "Captions: `/workspace/sierra/or_127_delta_charts/captions.json`.",
        "",
        "`luis_or_system.py` / `luis_vbp_one.py` / `luis_vbp_or.py` / `luis_vbp_va_stop.py` / "
        "`luis_vbp_or_va_stop.py` were not touched.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if obj is None or (isinstance(obj, float) and not math.isfinite(obj)):
        return None
    return obj


def main() -> int:
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    assert LAST_ENTRY_BAR == 1480 and FLATTEN_BAR == 1560 and OR_LAST_BAR == 20
    assert COST == 0.50 and BARS_PER_5M == 20 and TICK == 0.25
    assert HOLD_BARS == 80
    assert pick_poc is not None

    days = tuple(DAYS)
    bars, fp, extra_cols = load_days(DB_PATH, days)
    print("LOADED bars", len(bars), "fp", len(fp), "extra_cols", extra_cols)
    delta_src = (
        "volume_delta" if "volume_delta" in extra_cols
        else ("delta" if "delta" in extra_cols else "ask_volume-bid_volume")
    )
    print("DELTA_SRC", delta_src)

    # Probe: volume_delta vs ask-bid and vs rth_cumulative_delta on one day
    g0 = bars[bars["trading_day"] == date(2026, 8, 6)].copy()
    g0 = attach_bar_delta(g0)
    if "rth_cumulative_delta" in g0.columns:
        mismatch = int((g0["cum_delta"].astype("int64") != g0["rth_cumulative_delta"].astype("int64")).sum())
        print(f"CHECK cum_delta vs rth_cumulative_delta on 2026-08-06: mismatches={mismatch}")
    if "ask_volume" in g0.columns:
        askbid = g0["ask_volume"].astype("int64") - g0["bid_volume"].astype("int64")
        mm2 = int((g0["bar_delta"].astype("int64") != askbid).sum())
        print(f"CHECK bar_delta vs ask-bid on 2026-08-06: mismatches={mm2}")

    lv6 = or_va_for_day(date(2026, 8, 6), g0, fp[fp["trading_day"] == date(2026, 8, 6)])
    print(
        f"MATH 2026-08-06 High={lv6['or_high']:.2f} Low={lv6['or_low']:.2f} R={lv6['R']:.2f} "
        f"1.27L={lv6['long_127']:.2f} 1.27S={lv6['short_127']:.2f} "
        f"2.05L={lv6['long_205']:.2f} 2.05S={lv6['short_205']:.2f} "
        f"POC={_fmt_px(lv6['or_poc'])} VAL={_fmt_px(lv6['or_val'])} VAH={_fmt_px(lv6['or_vah'])}"
    )
    raw = lv6["or_high"] + 0.27 * lv6["R"]
    print(f"  raw 1.27 long={raw:.4f} rounded={round_tick(raw):.2f}")

    all_trades: list[dict] = []
    rows: list[dict] = []
    captions: list[dict] = []
    metas: list[dict] = []

    for day in DAYS:
        g = bars[bars["trading_day"] == day]
        fday = fp[fp["trading_day"] == day]
        if g.empty:
            print(f"NO BARS {day}")
            continue
        trades, log, meta = simulate_day(day, g, fday)
        metas.append(meta)
        print("\n".join(log))
        print()
        if len(trades) > 1:
            raise SystemExit(f"one_trade leaked {len(trades)} fills on {day}")
        all_trades.extend(trades)

        lv = meta["levels"]
        out = CHART_DIR / f"D127_{day.isoformat()}.png"
        plot_one(day, g, trades, lv, meta["why"], out)
        print(f"CHART {out}")
        print()

        if trades:
            tr = trades[0]
            rec = {
                "date": day.isoformat(),
                "side": tr["side"],
                "entry_time": tr["entry_time"],
                "exit_time": tr["exit_time"],
                "entry_price": tr["entry_price"],
                "exit_price": tr["exit_price"],
                "net": tr["net"],
                "reason": tr["exit_reason"],
                "cum_delta_entry": tr["cum_delta_entry"],
                "hold_minutes": tr["hold_minutes"],
                "entry_bar": tr["entry_bar"],
                "exit_bar": tr["exit_bar"],
                "stop": tr["stop"],
                "why": "",
            }
            rows.append(rec)
            captions.append({
                "date": day.isoformat(),
                "path": str(out),
                "side": tr["side"].upper(),
                "entry_time": tr["entry_time"],
                "exit_time": tr["exit_time"],
                "net": float(tr["net"]),
                "reason": tr["exit_reason"],
                "cum_delta_entry": int(tr["cum_delta_entry"]),
                "hold_minutes": float(tr["hold_minutes"]),
                "stop": tr["stop"] if _finite(tr["stop"]) else None,
                "or_val": tr["or_val"] if _finite(tr["or_val"]) else None,
                "or_vah": tr["or_vah"] if _finite(tr["or_vah"]) else None,
                "long_127": tr["long_127"],
                "short_127": tr["short_127"],
                "long_205": tr["long_205"],
                "short_205": tr["short_205"],
                "n_delta_skip_before_fill": int(tr["n_delta_skip_before_fill"]),
                "sentence": caption_sentence(day, tr, lv, ""),
            })
        else:
            rec = {
                "date": day.isoformat(),
                "side": "NONE",
                "entry_time": "",
                "exit_time": "",
                "entry_price": None,
                "exit_price": None,
                "net": 0.0,
                "reason": meta["why"],
                "cum_delta_entry": None,
                "hold_minutes": None,
                "entry_bar": None,
                "exit_bar": None,
                "stop": None,
                "why": meta["why"],
            }
            rows.append(rec)
            captions.append({
                "date": day.isoformat(),
                "path": str(out),
                "side": "NONE",
                "entry_time": "",
                "exit_time": "",
                "net": 0.0,
                "reason": meta["why"],
                "n_delta_skip": int(meta["n_delta_skip"]),
                "n_127_single": int(meta["n_127_single"]),
                "sentence": caption_sentence(day, None, lv, meta["why"]),
            })

    csv_cols = [
        "day", "side", "entry_time", "entry_price", "exit_time", "exit_price",
        "exit_reason", "or_high", "or_low", "R",
        "long_127", "short_127", "long_205", "short_205",
        "or_poc", "or_val", "or_vah", "stop", "target_205", "lvl_127",
        "cum_delta_entry", "net", "gross",
        "entry_bar", "exit_bar", "hold_minutes",
        "n_delta_skip_before_fill", "path_note", "trade_index_in_day",
    ]
    tdf = pd.DataFrame(all_trades)
    if len(tdf):
        for c in csv_cols:
            if c not in tdf.columns:
                tdf[c] = ""
        tdf[csv_cols].to_csv(OUT_DIR / "luis_or_127_delta_trades.csv", index=False)
    else:
        pd.DataFrame(columns=csv_cols).to_csv(OUT_DIR / "luis_or_127_delta_trades.csv", index=False)

    idx = pd.Index([d.isoformat() for d in DAYS], name="day")
    daily = tdf.groupby("day")["net"].sum() if len(tdf) else pd.Series(dtype=float)
    day_nets = pd.Series(0.0, index=idx).add(daily, fill_value=0.0)

    n = int(len(tdf))
    net = float(tdf["net"].sum()) if n else 0.0
    plot_equity(day_nets, OUT_DIR / "luis_or_127_delta_equity.png", n, net)

    (CHART_DIR / "captions.json").write_text(
        json.dumps(_jsonable(captions), indent=2) + "\n"
    )
    write_md(rows, tdf, day_nets, metas, delta_src, OUT_DIR / "luis_or_127_delta.md")

    print("TRADES")
    if n:
        show = [
            "day", "side", "entry_time", "exit_time", "exit_reason",
            "cum_delta_entry", "hold_minutes", "net", "stop",
        ]
        print(tdf[show].to_string(index=False))
        print(f"n={n} net={net:.2f} gross={tdf['gross'].sum():.2f}")
        print("reasons", tdf["exit_reason"].value_counts().to_dict())
        print("delta_skips_total", int(sum(m["n_delta_skip"] for m in metas)))
        print("no_trade", {
            "no_tag": sum(1 for m in metas if m["why"] == "no_tag"),
            "delta_block": sum(1 for m in metas if m["why"] == "delta_block"),
        })
        # print a couple of cum_delta entries
        for _, r in tdf.head(4).iterrows():
            print(
                f"GATE {r['day']} {r['side']} cum_delta={int(r['cum_delta_entry']):+d} "
                f"entry={r['entry_time']} fill={r['entry_price']}"
            )
    else:
        print("(none)")
        print("delta_skips_total", int(sum(m["n_delta_skip"] for m in metas)))
        print("no_trade", {
            "no_tag": sum(1 for m in metas if m["why"] == "no_tag"),
            "delta_block": sum(1 for m in metas if m["why"] == "delta_block"),
        })

    print("wrote", OUT_DIR / "luis_or_127_delta.py")
    print("wrote", OUT_DIR / "luis_or_127_delta_trades.csv")
    print("wrote", OUT_DIR / "luis_or_127_delta.md")
    print("wrote", OUT_DIR / "luis_or_127_delta_equity.png")
    print("wrote", CHART_DIR / "captions.json")
    print("charts", CHART_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
