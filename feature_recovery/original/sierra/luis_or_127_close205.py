#!/usr/bin/env python3
"""ONE NQ day, close-rule hold. New file only.

Does not modify locked luis_or_127_*.py / luis_vbp_*.py / luis_or_system.py.
Does not start a 23-day book. Day locked: 2026-07-30 LONG only.

ENTRY: same 1.27 + cum-delta fill as luis_or_127_delta.py (after bar 20).
HOLD (Luis confirmed): gauge EVERYTHING on the 15s CLOSE, never the wick.
  Stop: first 15s AFTER entry whose CLOSE is strictly below frozen OR VAL.
        Fill at that bar's close. Wick through VAL that closes >= VAL does NOT stop.
  2.05 is NOT a take on touch. Wick into 2.05 that closes <= 2.05 stays in.
  First CLOSE strictly above 2.05 = lift (runner). Do not exit on that bar.
  After lift: VAL stop is obsolete. Exit = first later 15s whose CLOSE is
        strictly below 2.05 (fill at that close), or flatten bar 1560.
  2.618 is a reference line only — never an auto-exit on this chart.
  Before the lift: only VAL close-stop and 15:00 flatten are exits.
COST 0.50 RT, 1 NQ, tick 0.25. Chicago RTH.
"""
from __future__ import annotations

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

from luis_or_127_delta import (
    DB_PATH,
    VA_PCT,
    _finite,
    _fmt_px,
    _naive,
    attach_bar_delta,
    load_days,
    or_va_for_day,
)
from luis_or_127_r2va import expansion_level
from luis_vbp_or import COST, FLATTEN_BAR, OR_LAST_BAR, TICK, clock_hm, ts

OUT_DIR = Path("/workspace/sierra")
CHART_DIR = OUT_DIR / "or_127_close205_charts"
DAY = date(2026, 7, 30)
FIGSIZE = (14.5, 7.2)
DPI = 120

# Published 1.27+delta book numbers for this day — verify against duckdb.
EXPECT = {
    "or_high": 27892.00,
    "or_low": 27813.25,
    "R": 78.75,
    "long_127": 27913.25,
    "long_205": 27974.75,
    "or_val": 27848.75,
    "or_vah": 27882.50,
    "entry_price": 27913.25,
    "entry_bar": 25,
    "entry_hm": "08:36:00",
}

C_127_L = "#2ca02c"
C_205_L = "#006400"
C_2618 = "#1f77b4"
C_OR_EMPH = "#e31a1c"
C_OR_OTH = "#3182bd"


def as_naive(x):
    if x is None:
        return None
    if isinstance(x, str):
        x = pd.Timestamp(x).to_pydatetime()
    if isinstance(x, pd.Timestamp):
        x = x.to_pydatetime()
    if isinstance(x, datetime) and x.tzinfo is not None:
        return x.replace(tzinfo=None)
    return x


def hm_s(x) -> str:
    t = as_naive(x)
    if t is None:
        return ""
    s = ts(t)
    return s[11:19] if len(s) >= 19 else s


def strip_tz(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("rth_bar_number").copy()
    g["t"] = pd.to_datetime(g["bar_start_chicago"])
    if getattr(g["t"].dt, "tz", None) is not None:
        g["t"] = g["t"].dt.tz_localize(None)
    g["t_end"] = pd.to_datetime(g["bar_end_chicago"])
    if getattr(g["t_end"].dt, "tz", None) is not None:
        g["t_end"] = g["t_end"].dt.tz_localize(None)
    return g


def session_ticks(t0: datetime, t1: datetime) -> list[datetime]:
    ticks = []
    cur = t0.replace(second=0, microsecond=0)
    extra = cur.minute % 5
    if extra:
        cur = cur + timedelta(minutes=(5 - extra))
    span_min = (t1 - t0).total_seconds() / 60.0
    step = 5 if span_min <= 90 else (15 if span_min <= 180 else 30)
    if cur < t0:
        cur += timedelta(minutes=step)
    while cur <= t1:
        ticks.append(cur)
        cur += timedelta(minutes=step)
    if t0 not in ticks:
        ticks = [t0] + ticks
    return ticks


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
    if isinstance(obj, (datetime, pd.Timestamp)):
        return ts(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def find_long_entry(g: pd.DataFrame, long_127: float):
    """First 15s after bar 20 that tags 1.27 long with cum_delta > 0."""
    by_bn = {int(r.rth_bar_number): r for r in g.itertuples(index=False)}
    for bn in range(OR_LAST_BAR + 1, FLATTEN_BAR + 1):
        row = by_bn.get(bn)
        if row is None:
            continue
        h = float(row.high)
        o = float(row.open)
        cum = int(row.cum_delta)
        if h >= long_127 - 1e-12 and cum > 0:
            fill = long_127 if o < long_127 else o
            return row, float(fill), int(cum)
    return None, None, None


def simulate_long_close_rule(day: date, g: pd.DataFrame, fp: pd.DataFrame) -> dict:
    g = attach_bar_delta(g)
    lv = or_va_for_day(day, g, fp)
    or_high, or_low = float(lv["or_high"]), float(lv["or_low"])
    R = float(lv["R"])
    or_val, or_vah = float(lv["or_val"]), float(lv["or_vah"])
    long_127 = float(lv["long_127"])
    long_205 = float(lv["long_205"])
    fib_2618 = float(expansion_level(or_high, or_low, 2.618, "up"))

    va_diff = {}
    for k, want in (
        ("or_high", EXPECT["or_high"]),
        ("or_low", EXPECT["or_low"]),
        ("R", EXPECT["R"]),
        ("long_127", EXPECT["long_127"]),
        ("long_205", EXPECT["long_205"]),
        ("or_val", EXPECT["or_val"]),
        ("or_vah", EXPECT["or_vah"]),
    ):
        got = {"or_high": or_high, "or_low": or_low, "R": R,
               "long_127": long_127, "long_205": long_205,
               "or_val": or_val, "or_vah": or_vah}[k]
        if abs(got - want) > 1e-9:
            va_diff[k] = {"given": want, "duckdb": got}

    g = strip_tz(g)
    by_bn = {int(r.rth_bar_number): r for r in g.itertuples(index=False)}

    # 08:37:45 sanity: old book exit_time is bar_end of the 08:37:30 bar (bn 31).
    bar_083730 = by_bn.get(31)  # 08:37:30-08:37:45, old or_va_stop bar
    bar_083745 = by_bn.get(32)  # 08:37:45-08:38:00

    def bar_facts(row):
        if row is None:
            return None
        c = float(row.close)
        return {
            "bar": int(row.rth_bar_number),
            "bar_start": hm_s(row.bar_start_chicago),
            "bar_end": hm_s(row.bar_end_chicago),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": c,
            "close_below_val": bool(c < or_val),
            "wick_through_val": bool(float(row.low) <= or_val + 1e-12),
        }

    old_stop_bar = bar_facts(bar_083730)
    bar_start_083745 = bar_facts(bar_083745)

    erow, fill, cum = find_long_entry(g, long_127)
    if erow is None:
        raise SystemExit("no long 1.27+delta fill on 2026-07-30")
    entry_bar = int(erow.rth_bar_number)
    entry_px = float(fill)
    entry_time = as_naive(erow.bar_start_chicago)
    if abs(entry_px - EXPECT["entry_price"]) > 1e-9 or entry_bar != EXPECT["entry_bar"]:
        print(
            f"NOTE entry differs from known fill: got bar {entry_bar} "
            f"@ {entry_px:.2f} vs known bar {EXPECT['entry_bar']} @ {EXPECT['entry_price']:.2f}"
        )

    lift = None
    exit_row = None
    exit_px = None
    reason = None
    max_h = float(erow.high)
    min_l = float(erow.low)
    max_c = float(erow.close)
    min_c = float(erow.close)

    for bn in range(entry_bar + 1, FLATTEN_BAR + 1):
        row = by_bn.get(bn)
        if row is None:
            continue
        c = float(row.close)
        h = float(row.high)
        l = float(row.low)
        max_h = max(max_h, h)
        min_l = min(min_l, l)
        max_c = max(max_c, c)
        min_c = min(min_c, c)

        if lift is None:
            # Before lift: VAL close-stop or flatten. Do not take 2.05 as profit.
            if c < or_val:
                exit_row, exit_px, reason = row, c, "or_val_close_stop"
                break
            if c > long_205:
                lift = {
                    "bar": bn,
                    "time": as_naive(row.bar_start_chicago),
                    "close": c,
                    "open": float(row.open),
                    "high": h,
                    "low": l,
                    "bar_end": as_naive(row.bar_end_chicago),
                }
                # stay in; 2.05 is the lift, not an exit
                continue
            if bn == FLATTEN_BAR:
                exit_row, exit_px, reason = row, c, "session_end"
                break
        else:
            # After lift: VAL obsolete. Exit = close < 2.05 or flatten 15:00.
            # 2.618 is reference only.
            if c < long_205:
                exit_row, exit_px, reason = row, c, "close_below_205"
                break
            if bn == FLATTEN_BAR:
                exit_row, exit_px, reason = row, c, "session_end"
                break

    if exit_row is None:
        raise SystemExit("simulation did not exit")

    exit_bar = int(exit_row.rth_bar_number)
    exit_time = as_naive(exit_row.bar_end_chicago)
    gross = float(exit_px) - entry_px
    net = gross - COST
    et = _naive(entry_time)
    xt = _naive(exit_time)
    hold_min = (xt - et).total_seconds() / 60.0 if (et and xt) else float("nan")
    mfe = max_h - entry_px
    mae = entry_px - min_l
    mfe_close = max_c - entry_px
    mae_close = entry_px - min_c

    facts = {
        "day": day.isoformat(),
        "side": "LONG",
        "rule": "close-rule",
        "or_high": or_high,
        "or_low": or_low,
        "R": R,
        "or_val": or_val,
        "or_vah": or_vah,
        "or_poc": float(lv["or_poc"]) if _finite(lv.get("or_poc")) else None,
        "long_127": long_127,
        "long_205": long_205,
        "fib_2618": fib_2618,
        "va_match_given": len(va_diff) == 0,
        "va_diff": va_diff if va_diff else None,
        "entry_time": ts(entry_time),
        "entry_time_hm": hm_s(entry_time),
        "entry_price": entry_px,
        "entry_bar": entry_bar,
        "entry_open": float(erow.open),
        "entry_high": float(erow.high),
        "entry_low": float(erow.low),
        "entry_close": float(erow.close),
        "cum_delta_entry": int(cum),
        "known_fill_matches": (
            entry_bar == EXPECT["entry_bar"]
            and abs(entry_px - EXPECT["entry_price"]) < 1e-9
            and hm_s(entry_time) == EXPECT["entry_hm"]
        ),
        "bar_083730_old_or_va_stop": old_stop_bar,
        "bar_083745_start": bar_start_083745,
        "old_083745_close_below_val": bool(old_stop_bar["close_below_val"]) if old_stop_bar else None,
        "lift": None if lift is None else {
            "time": ts(lift["time"]),
            "time_hm": hm_s(lift["time"]),
            "close": lift["close"],
            "bar": lift["bar"],
            "open": lift["open"],
            "high": lift["high"],
            "low": lift["low"],
        },
        "exit_time": ts(exit_time),
        "exit_time_hm": hm_s(exit_time),
        "exit_price": float(exit_px),
        "exit_reason": reason,
        "exit_bar": exit_bar,
        "gross": round(gross, 4),
        "cost": COST,
        "net": round(net, 4),
        "hold_minutes": round(float(hold_min), 4) if _finite(hold_min) else None,
        "mfe": round(float(mfe), 4),
        "mae": round(float(mae), 4),
        "mfe_high": float(max_h),
        "mae_low": float(min_l),
        "mfe_close": round(float(mfe_close), 4),
        "mae_close": round(float(mae_close), 4),
        "tick": TICK,
        "flatten_bar": FLATTEN_BAR,
    }
    facts["_g"] = g
    facts["_lv"] = lv
    facts["_lift_raw"] = lift
    facts["_exit_row"] = exit_row
    facts["_entry_row"] = erow
    return facts


def plot_one(facts: dict, out_path: Path) -> None:
    """3-pane overlay (price + volume + volume delta). Fills unchanged.

    Plot-only: dPOC/dVWAP from rth_developing_value_area_15s (70%).
    Delegates to plot_close205_vol so this day's script cannot clobber the stacked PNG.
    """
    from plot_close205_vol import load_day_series, plot_stacked
    day = date.fromisoformat(facts["day"])
    g = load_day_series(day)
    plot_stacked(g, facts, out_path)


def main() -> int:
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    assert VA_PCT == 0.70
    assert OR_LAST_BAR == 20
    assert FLATTEN_BAR == 1560
    assert COST == 0.50
    assert TICK == 0.25
    assert DB_PATH.name == "NQU26-CME-15s-1mo.duckdb"
    assert DB_PATH.exists()

    bars, fp, extra_cols = load_days(DB_PATH, (DAY,))
    print("LOADED bars", len(bars), "fp", len(fp), "extra", extra_cols, "db", DB_PATH)
    if len(fp) < 10000:
        raise SystemExit(f"footprint too small ({len(fp)}); need full 1mo file")
    g = bars[bars["trading_day"] == DAY].copy()
    if g.empty:
        raise SystemExit(f"NO BARS {DAY}")

    facts = simulate_long_close_rule(DAY, g, fp)
    out = CHART_DIR / f"CLOSE_{DAY.isoformat()}.png"
    plot_one(facts, out)
    if not out.exists() or out.stat().st_size < 1000:
        raise SystemExit(f"empty/missing chart {out}")

    public = {k: v for k, v in facts.items() if not k.startswith("_")}
    public["chart"] = str(out)
    public["chart_bytes"] = int(out.stat().st_size)
    jpath = CHART_DIR / f"CLOSE_{DAY.isoformat()}.json"
    jpath.write_text(json.dumps(_jsonable(public), indent=2) + "\n")

    old = public["bar_083730_old_or_va_stop"]
    lift = public["lift"]
    print(f"\n==== {DAY.isoformat()} LONG close-rule ====")
    print(
        f"  OR High={public['or_high']:.2f} Low={public['or_low']:.2f} R={public['R']:.2f}  "
        f"VAL={_fmt_px(public['or_val'])} VAH={_fmt_px(public['or_vah'])}  "
        f"1.27={public['long_127']:.2f}  2.05={public['long_205']:.2f}  "
        f"2.618={public['fib_2618']:.2f}"
    )
    if public["va_match_given"]:
        print("  VA/OR levels MATCH given (duckdb == 27848.75 / 27882.50)")
    else:
        print("  VA/OR DIFFERS from given:", public["va_diff"])
    print(
        f"  ENTRY {public['entry_time_hm']} @ {public['entry_price']:.2f}  "
        f"bar {public['entry_bar']}  cum_delta={public['cum_delta_entry']}  "
        f"known_fill_matches={public['known_fill_matches']}"
    )
    print(
        f"  08:37:45 sanity (old or_va_stop bar_end): "
        f"bar {old['bar']} {old['bar_start']}-{old['bar_end']}  "
        f"O={old['open']:.2f} H={old['high']:.2f} L={old['low']:.2f} C={old['close']:.2f}  "
        f"close_below_VAL={old['close_below_val']}  wick_through_VAL={old['wick_through_val']}"
    )
    b32 = public["bar_083745_start"]
    print(
        f"  bar starting 08:37:45: bar {b32['bar']} C={b32['close']:.2f}  "
        f"close_below_VAL={b32['close_below_val']}"
    )
    if lift:
        print(
            f"  LIFT first close>2.05  {lift['time_hm']}  close={lift['close']:.2f}  "
            f"bar {lift['bar']}  (H={lift['high']:.2f} L={lift['low']:.2f})"
        )
    else:
        print("  LIFT none (never closed above 2.05)")
    print(
        f"  EXIT {public['exit_time_hm']} @ {public['exit_price']:.2f}  "
        f"{public['exit_reason']}  bar {public['exit_bar']}  "
        f"gross={public['gross']:+.2f} net={public['net']:+.2f}  "
        f"hold {public['hold_minutes']:.2f}m"
    )
    print(
        f"  MFE {public['mfe']:+.2f} (high {public['mfe_high']:.2f})  "
        f"MAE {public['mae']:+.2f} (low {public['mae_low']:.2f})  "
        f"close-MFE {public['mfe_close']:+.2f}  close-MAE {public['mae_close']:+.2f}"
    )
    print(f"  2.618 print used = {public['fib_2618']:.2f}")
    print(f"  CHART {out}  bytes={out.stat().st_size}")
    print(f"  JSON  {jpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
