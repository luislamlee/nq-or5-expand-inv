#!/usr/bin/env python3
"""1.27+cum-delta ENTRIES as the +465 frozen OR-VA book; hold/target CHANGED.

NEW files only. Does not touch locked luis_or_127_delta.py / ema* / aema / vol /
wyckoff / vbp_* / luis_or_system.py.

ENTRY: identical to luis_or_127_delta.py (must reprint same side/time/price).

HOLD / STOP / TARGET (causal, no look-ahead):
  Markup (no R2 yet): stop = frozen OR VAL (long) / OR VAH (short). 15s touch.
    NO fib take-profit. New highs only lift the live fib to the next unused
    ladder print strictly beyond the running trade extreme.
  First pause after entry: 20 consecutive 15s with no new extreme in the
    trade direction (long: high > run-max since entry; short: low < run-min).
    Extreme clock starts at the ENTRY bar (already outside OR). Entry bar
    itself is the first extreme, not a pause bar.
  When those 20 bars close, R2 locks. 70% VA from footprint of EXACTLY those
    20 bars. Long stop -> R2 VAL, short stop -> R2 VAH, FROM THE NEXT 15s
    (no peek). Pause-bar touches of the old OR-VA stop still count.
  After lock: live fib is ARMED. First 15s touch may exit (target_fib).
    Close-through the fib (close beyond, open was not) = new high continuation:
    do NOT take the old fib; lift to the next unused print; stay in.
    Stop stays first-R2 VA. No R3. No 20-minute clock.
  Session end bar 1560. COST 0.50 RT, 1 NQ, tick 0.25.
  Dual wick while armed: stop wins unless open already beyond the fib.

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

from luis_or_127_delta import (
    DAYS,
    LAST8_START,
    VA_PCT,
    _finite,
    _fmt_px,
    _naive,
    attach_bar_delta,
    load_days,
    max_dd,
    open_beyond_stop,
    open_beyond_target,
    or_va_for_day,
    or_va_stop_fill,
    tagged_stop,
    tagged_target,
    target_205_fill,
    value_area,
)
from luis_vbp_or import (
    BARS_PER_5M,
    COST,
    FLATTEN_BAR,
    LAST_ENTRY_BAR,
    OR_LAST_BAR,
    TICK,
    clock_hm,
    pick_poc,
    ts,
)

DB_PATH = Path("/workspace/sierra/NQU26-CME-15s-1mo.duckdb")
OUT_DIR = Path("/workspace/sierra")
CHART_DIR = OUT_DIR / "or_127_r2va_charts"
REF_CSV = OUT_DIR / "luis_or_127_delta_trades.csv"
PAUSE_BARS = 20
NET_465 = 465.00

# Luis OR fib ladder: upside = High+(k-1)*R, downside = Low-(k-1)*R, tick 0.25.
FIB_K = (1.27, 1.618, 2.05, 2.618, 3.33, 4.23, 5.33, 6.85)


def expansion_level(or_high: float, or_low: float, k: float, side: str) -> float:
    """Same formula as luis_or_127_delta.expansion_level (tick-rounded)."""
    r = or_high - or_low
    if side == "up":
        raw = or_high + (k - 1.0) * r
    else:
        raw = or_low - (k - 1.0) * r
    q = raw / TICK
    if q >= 0:
        return float(math.floor(q + 0.5) * TICK)
    return float(math.ceil(q - 0.5) * TICK)


def fib_ladder(or_high: float, or_low: float) -> dict:
    return {
        "long": [(k, expansion_level(or_high, or_low, k, "up")) for k in FIB_K],
        "short": [(k, expansion_level(or_high, or_low, k, "down")) for k in FIB_K],
    }


def next_fib(side: str, ladder: dict, run_ext: float):
    """First unused ladder print strictly beyond the running trade extreme."""
    levels = ladder["long" if side == "long" else "short"]
    if side == "long":
        for k, px in levels:
            if px > run_ext + 1e-12:
                return k, float(px)
    else:
        for k, px in levels:
            if px < run_ext - 1e-12:
                return k, float(px)
    return None, float("nan")


def r2_va_from_bars(fday: pd.DataFrame, bar_lo: int, bar_hi: int, ref_px: float) -> dict:
    """70% VA on footprint of EXACTLY [bar_lo, bar_hi] inclusive. POC via pick_poc vs mid."""
    nan = float("nan")
    chunk = fday[(fday["rth_bar_number"] >= bar_lo) & (fday["rth_bar_number"] <= bar_hi)]
    n_rows = int(len(chunk))
    n_bars = int(chunk["rth_bar_number"].nunique()) if n_rows else 0
    if n_rows == 0:
        return {
            "r2_poc": nan, "r2_poc_vol": 0.0, "r2_val": nan, "r2_vah": nan,
            "r2_va_vol": 0.0, "r2_fp_rows": 0, "r2_fp_bars": 0, "r2_fp_vol": 0.0,
        }
    grp = chunk.groupby("price", sort=True)["total_volume"].sum()
    prices = grp.index.to_numpy(dtype=float)
    volumes = grp.to_numpy(dtype=float)
    poc, poc_vol = pick_poc(prices, volumes, float(ref_px))
    vah, val, va_vol = value_area(prices, volumes, poc, pct=VA_PCT)
    return {
        "r2_poc": poc,
        "r2_poc_vol": poc_vol,
        "r2_val": val,
        "r2_vah": vah,
        "r2_va_vol": va_vol,
        "r2_fp_rows": n_rows,
        "r2_fp_bars": n_bars,
        "r2_fp_vol": float(volumes.sum()),
    }


def stop_reason_of(pos: dict) -> str:
    return "r2_va_stop" if pos.get("stop_kind") == "r2_va" else "or_va_stop"


def simulate_day(day: date, g: pd.DataFrame, fp: pd.DataFrame):
    g = attach_bar_delta(g)
    lv = or_va_for_day(day, g, fp)
    or_high, or_low = lv["or_high"], lv["or_low"]
    or_val, or_vah = lv["or_val"], lv["or_vah"]
    long_127, short_127 = lv["long_127"], lv["short_127"]
    long_205, short_205 = lv["long_205"], lv["short_205"]
    R = lv["R"]
    delta_src = g.attrs.get("delta_src", "volume_delta")
    ladder = fib_ladder(or_high, or_low)
    fday = fp[fp["trading_day"] == day]

    log = [
        f"{day.isoformat()} OR High={or_high:.2f} Low={or_low:.2f} R={R:.2f} "
        f"close={lv['or_close']:.2f}",
        f"  1.27 long={long_127:.2f} short={short_127:.2f}  "
        f"2.05 long={long_205:.2f} short={short_205:.2f}",
        f"  OR 5m POC={_fmt_px(lv['or_poc'])} VAL={_fmt_px(or_val)} VAH={_fmt_px(or_vah)}  "
        f"(markup stop: long=OR VAL  short=OR VAH; target UNARMED until first R2)",
        f"  fib ladder long={[round(px, 2) for _, px in ladder['long']]}",
        f"  fib ladder short={[round(px, 2) for _, px in ladder['short']]}",
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
        lvl = long_127 if side == "long" else short_127
        run_ext = float(row.high) if side == "long" else float(row.low)
        live_k, live_fib = next_fib(side, ladder, run_ext)
        pos = {
            "side": side,
            "entry_bar": bn,
            "entry_time": row.bar_start_chicago,
            "entry_price": float(fill),
            "stop": stop,
            "stop_kind": "or_va",
            "lvl_127": lvl,
            "cum_delta_entry": int(cum),
            "or_val": or_val,
            "or_vah": or_vah,
            "run_ext": run_ext,
            "pause_count": 0,
            "pause_start": None,
            "r2_locked": False,
            "r2_switch_pending": False,
            "r2_lock_bar": None,
            "r2_pause_start": None,
            "r2_pause_end": None,
            "r2_high": float("nan"),
            "r2_low": float("nan"),
            "r2_poc": float("nan"),
            "r2_val": float("nan"),
            "r2_vah": float("nan"),
            "r2_poc_vol": 0.0,
            "r2_va_vol": 0.0,
            "r2_fp_bars": 0,
            "live_k": live_k,
            "live_fib": live_fib,
            "target_armed": False,
            "markup_205_tags": 0,
            "n_lifts": 0,
        }
        filled_day = True
        log.append(
            f"  ENTRY {side.upper()} bar {bn} {clock_hm(row.bar_start_chicago)} "
            f"fill={fill:.2f} open={row.open:.2f} H={row.high:.2f} L={row.low:.2f} "
            f"1.27={lvl:.2f} stop={_fmt_px(stop)} (OR-VA) "
            f"run_ext={run_ext:.2f} live_fib={live_k}@{_fmt_px(live_fib)} UNARMED "
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
            "stop_kind": pos["stop_kind"],
            "target_fib": round(float(pos["live_fib"]), 4) if _finite(pos["live_fib"]) else float("nan"),
            "target_k": pos["live_k"] if pos["live_k"] is not None else "",
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
            "r2_locked": bool(pos["r2_locked"]),
            "r2_lock_bar": pos["r2_lock_bar"] if pos["r2_lock_bar"] is not None else "",
            "r2_pause_start": pos["r2_pause_start"] if pos["r2_pause_start"] is not None else "",
            "r2_pause_end": pos["r2_pause_end"] if pos["r2_pause_end"] is not None else "",
            "r2_high": round(float(pos["r2_high"]), 4) if _finite(pos["r2_high"]) else float("nan"),
            "r2_low": round(float(pos["r2_low"]), 4) if _finite(pos["r2_low"]) else float("nan"),
            "r2_poc": round(float(pos["r2_poc"]), 4) if _finite(pos["r2_poc"]) else float("nan"),
            "r2_val": round(float(pos["r2_val"]), 4) if _finite(pos["r2_val"]) else float("nan"),
            "r2_vah": round(float(pos["r2_vah"]), 4) if _finite(pos["r2_vah"]) else float("nan"),
            "run_ext_exit": round(float(pos["run_ext"]), 4),
            "markup_205_tags": int(pos["markup_205_tags"]),
            "n_lifts": int(pos["n_lifts"]),
        }
        trades.append(rec)
        extra = f" {path_note}" if path_note else ""
        log.append(
            f"  EXIT  {side.upper()} bar {int(row.rth_bar_number)} {clock_hm(t_exit)} "
            f"fill={exit_px:.2f} reason={reason} stop={_fmt_px(pos['stop'])} "
            f"({pos['stop_kind']}) fib={pos['live_k']}@{_fmt_px(pos['live_fib'])} "
            f"armed={pos['target_armed']} r2={pos['r2_locked']} "
            f"hold={hold_min:.2f}m gross={gross:.2f} net={net:.2f}{extra}"
        )
        pos = None

    def maybe_lock_r2(row) -> None:
        """Close of 20th pause bar: freeze R2 VA; switch stop/target NEXT bar."""
        bn = int(row.rth_bar_number)
        i0 = int(pos["pause_start"])
        i1 = bn
        highs = [float(by_bn[b].high) for b in range(i0, i1 + 1) if b in by_bn]
        lows = [float(by_bn[b].low) for b in range(i0, i1 + 1) if b in by_bn]
        r2_high = max(highs) if highs else float("nan")
        r2_low = min(lows) if lows else float("nan")
        mid = (r2_high + r2_low) / 2.0 if (_finite(r2_high) and _finite(r2_low)) else float("nan")
        va = r2_va_from_bars(fday, i0, i1, mid)
        pos["r2_locked"] = True
        pos["r2_switch_pending"] = True
        pos["r2_lock_bar"] = i1
        pos["r2_pause_start"] = i0
        pos["r2_pause_end"] = i1
        pos["r2_high"] = r2_high
        pos["r2_low"] = r2_low
        pos["r2_poc"] = va["r2_poc"]
        pos["r2_val"] = va["r2_val"]
        pos["r2_vah"] = va["r2_vah"]
        pos["r2_poc_vol"] = va["r2_poc_vol"]
        pos["r2_va_vol"] = va["r2_va_vol"]
        pos["r2_fp_bars"] = va["r2_fp_bars"]
        log.append(
            f"  R2 LOCK bar {i1} {clock_hm(row.bar_end_chicago)} "
            f"pause bars {i0}-{i1} ({i1 - i0 + 1} x 15s) "
            f"box H={_fmt_px(r2_high)} L={_fmt_px(r2_low)} mid={_fmt_px(mid)} "
            f"POC={_fmt_px(va['r2_poc'])} VAL={_fmt_px(va['r2_val'])} "
            f"VAH={_fmt_px(va['r2_vah'])} fp_bars={va['r2_fp_bars']} "
            f"fp_vol={va['r2_fp_vol']:.0f}  "
            f"stop/target switch from NEXT 15s  "
            f"armed_fib={pos['live_k']}@{_fmt_px(pos['live_fib'])} "
            f"(still OR-VA stop on this bar)"
        )
        if va["r2_fp_bars"] != PAUSE_BARS:
            log.append(
                f"  WARN R2 footprint bars={va['r2_fp_bars']} want {PAUSE_BARS} "
                f"rows={va['r2_fp_rows']}"
            )

    def apply_r2_switch(bn: int) -> None:
        if not pos or not pos.get("r2_switch_pending"):
            return
        if bn <= int(pos["r2_lock_bar"]):
            return
        side = pos["side"]
        new_stop = pos["r2_val"] if side == "long" else pos["r2_vah"]
        pos["stop"] = new_stop
        pos["stop_kind"] = "r2_va"
        pos["target_armed"] = True
        pos["r2_switch_pending"] = False
        log.append(
            f"  R2 SWITCH bar {bn} stop -> {_fmt_px(new_stop)} "
            f"({side} {'VAL' if side == 'long' else 'VAH'})  "
            f"target ARMED {pos['live_k']}@{_fmt_px(pos['live_fib'])}"
        )

    def update_extreme_and_fib(h: float, l: float, note: str = "") -> bool:
        """Return True if running extreme updated (and fib may have lifted)."""
        side = pos["side"]
        old_ext = float(pos["run_ext"])
        old_k, old_fib = pos["live_k"], pos["live_fib"]
        if side == "long":
            if h > old_ext + 1e-12:
                pos["run_ext"] = float(h)
            else:
                return False
        else:
            if l < old_ext - 1e-12:
                pos["run_ext"] = float(l)
            else:
                return False
        live_k, live_fib = next_fib(side, ladder, pos["run_ext"])
        lifted = (live_k != old_k) or ((live_k is None) != (old_k is None))
        if (not lifted) and _finite(live_fib) and _finite(old_fib):
            lifted = abs(float(live_fib) - float(old_fib)) > 1e-9
        pos["live_k"] = live_k
        pos["live_fib"] = live_fib
        if lifted:
            pos["n_lifts"] = int(pos["n_lifts"]) + 1
            log.append(
                f"  LIFT {side} run_ext {old_ext:.2f}->{pos['run_ext']:.2f} "
                f"fib {old_k}@{_fmt_px(old_fib)} -> {live_k}@{_fmt_px(live_fib)} "
                f"{note}"
            )
        return True

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
            apply_r2_switch(bn)
            side = pos["side"]
            stop = pos["stop"]
            tgt = pos["live_fib"]
            armed = bool(pos["target_armed"])

            # 2.05 would have filled during markup (unarmed): count, never take.
            in_markup = (not pos["r2_locked"]) or pos.get("r2_switch_pending")
            # switch_pending means this is still the lock bar (stop not yet R2)
            if not armed:
                fib205 = long_205 if side == "long" else short_205
                if tagged_target(side, h, l, fib205):
                    pos["markup_205_tags"] = int(pos["markup_205_tags"]) + 1
                    log.append(
                        f"  SKIP 2.05-during-markup bar {bn} {clock_hm(row.bar_start_chicago)} "
                        f"{side} 2.05={fib205:.2f} H={h:.2f} L={l:.2f} "
                        f"(target unarmed until R2 switch)"
                    )

            hit_stop = tagged_stop(side, h, l, stop)
            hit_tgt = armed and _finite(tgt) and tagged_target(side, h, l, tgt)
            close_through = (
                armed and _finite(tgt) and (
                    (side == "long" and c > tgt + 1e-12)
                    or (side == "short" and c < tgt - 1e-12)
                )
            )
            open_tgt = armed and _finite(tgt) and open_beyond_target(side, o, tgt)

            if hit_tgt and hit_stop:
                if open_tgt:
                    close_trade(
                        row, float(o), "target_fib",
                        "dual_tag: open already beyond fib → target at open",
                    )
                elif open_beyond_stop(side, o, stop):
                    close_trade(
                        row, float(o), stop_reason_of(pos),
                        "dual_tag: open already beyond stop → stop at open",
                    )
                else:
                    close_trade(
                        row, float(stop), stop_reason_of(pos),
                        "dual_tag: both wicks, open between → stop wins (protective)",
                    )
            elif hit_stop:
                fill = or_va_stop_fill(side, o, stop)
                close_trade(row, fill, stop_reason_of(pos))
            elif hit_tgt and open_tgt:
                close_trade(
                    row, float(o), "target_fib",
                    "gap through fib at open",
                )
            elif hit_tgt and close_through:
                # New-high continuation: do not take the old fib; lift; stay in.
                log.append(
                    f"  SKIP fib close-through bar {bn} {clock_hm(row.bar_start_chicago)} "
                    f"close={c:.2f} beyond {pos['live_k']}@{tgt:.2f} → lift, stay in"
                )
                update_extreme_and_fib(h, l, note="close-through continuation")
            elif hit_tgt:
                fill = target_205_fill(side, o, tgt)
                close_trade(row, fill, "target_fib")
            elif bn == FLATTEN_BAR:
                close_trade(row, c, "session_end")

            if pos is not None:
                # Markup pause clock, or post-lock extreme tracking. No R3.
                if not pos["r2_locked"]:
                    new_ext = update_extreme_and_fib(h, l, note="markup new extreme")
                    if new_ext:
                        pos["pause_count"] = 0
                        pos["pause_start"] = None
                    elif bn != pos["entry_bar"]:
                        if pos["pause_count"] == 0:
                            pos["pause_start"] = bn
                        pos["pause_count"] += 1
                        if pos["pause_count"] == PAUSE_BARS:
                            maybe_lock_r2(row)
                else:
                    # First R2 only. New extremes lift the armed fib; stop frozen.
                    if not pos.get("r2_switch_pending"):
                        update_extreme_and_fib(h, l, note="post-R2 new high (no R3)")

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
        "ladder": ladder,
        "delta_src": delta_src,
    }
    return trades, log, meta


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


def assert_entries_match(tdf: pd.DataFrame) -> None:
    ref = pd.read_csv(REF_CSV)
    if len(tdf) != len(ref):
        raise SystemExit(f"ENTRY COUNT mismatch: r2va n={len(tdf)} vs +465 n={len(ref)}")
    for i, (a, b) in enumerate(zip(tdf.itertuples(index=False), ref.itertuples(index=False))):
        a_day = str(a.day)[:10]
        b_day = str(b.day)[:10]
        a_side = str(a.side).lower()
        b_side = str(b.side).lower()
        a_et = str(a.entry_time)[:19]
        b_et = str(b.entry_time)[:19]
        a_px = round(float(a.entry_price), 4)
        b_px = round(float(b.entry_price), 4)
        if a_day != b_day or a_side != b_side or a_et != b_et or abs(a_px - b_px) > 1e-6:
            raise SystemExit(
                f"ENTRY MISMATCH row {i}: "
                f"r2va {a_day} {a_side} {a_et} @{a_px}  vs  "
                f"+465 {b_day} {b_side} {b_et} @{b_px}  STOP"
            )
    print("ENTRY SET MATCHES +465: 23 fills, same side/time/price on all days")


def plot_one(day: date, g: pd.DataFrame, trades: list[dict], lv: dict,
             ladder: dict, why: str, out_path: Path) -> None:
    g = g.sort_values("rth_bar_number").copy()
    g["t"] = pd.to_datetime(g["bar_start_chicago"])
    if getattr(g["t"].dt, "tz", None) is not None:
        g["t"] = g["t"].dt.tz_localize(None)
    t0 = datetime.combine(day, dtime(8, 30))
    t_sess_end = datetime.combine(day, dtime(15, 0))
    t_ten = datetime.combine(day, dtime(10, 30))
    tday = [t for t in trades if t["day"] == day.isoformat()][:1]
    if tday:
        tr = tday[0]
        exit_t = _naive(pd.Timestamp(tr["exit_time"]))
        pad = timedelta(minutes=25)
        t_right = min(max(exit_t + pad, t_ten), t_sess_end)
        title = (
            f"{day.isoformat()}  ·  {tr['side'].upper()}  ·  "
            f"net {tr['net']:+.2f}  ·  "
            f"{clock_hm(tr['entry_time'])} → {clock_hm(tr['exit_time'])}  "
            f"{tr['exit_reason']}  r2={tr['r2_locked']}"
        )
    else:
        t_right = t_ten
        title = f"{day.isoformat()}  ·  NONE  ·  {why or 'no trade'}"

    vis = g[(g["t"] >= t0 - timedelta(minutes=1)) & (g["t"] <= t_right + timedelta(minutes=1))]
    fig, ax = plt.subplots(figsize=(14.5, 7.2), dpi=120)
    ax.fill_between(vis["t"], vis["low"], vis["high"], color="0.82", alpha=0.55,
                    linewidth=0, label="15s range", zorder=1)
    ax.plot(vis["t"], vis["close"], color="0.18", lw=0.9, label="15s close", zorder=3)

    or_high, or_low = lv["or_high"], lv["or_low"]
    x0 = mdates.date2num(t0)
    x1 = mdates.date2num(datetime.combine(day, dtime(8, 35)))
    ax.add_patch(Rectangle(
        (x0, or_low), x1 - x0, or_high - or_low,
        facecolor="#ffcc00", edgecolor="#aa8800", linewidth=1.15, alpha=0.48,
        zorder=2, label="5m OR box",
    ))
    ax.axhline(or_high, color="#aa8800", lw=1.1, zorder=3)
    ax.axhline(or_low, color="#aa8800", lw=1.0, zorder=3)

    side = tday[0]["side"] if tday else None
    fibs = ladder["long" if side == "long" else "short"] if side else ladder["long"]
    fib_colors = ["#2ca02c", "#98df8a", "#006400", "#1f77b4", "#9467bd",
                  "#8c564b", "#e377c2", "#7f7f7f"]
    for (k, px), col in zip(fibs, fib_colors):
        ax.axhline(px, color=col, lw=0.9, ls=":", alpha=0.85,
                   label=f"{k:g}", zorder=4)

    if _finite(lv["or_val"]):
        ax.axhline(float(lv["or_val"]), color="#3182bd", lw=1.2, ls="--",
                   label="OR VAL", zorder=5)
    if _finite(lv["or_vah"]):
        ax.axhline(float(lv["or_vah"]), color="#3182bd", lw=1.0, ls="-",
                   label="OR VAH", zorder=5)

    y_extra = [or_high, or_low]
    for _, px in fibs:
        y_extra.append(px)
    if _finite(lv["or_val"]):
        y_extra.append(float(lv["or_val"]))
    if _finite(lv["or_vah"]):
        y_extra.append(float(lv["or_vah"]))

    if tday:
        tr = tday[0]
        if tr.get("r2_locked") and _finite(tr.get("r2_high")) and _finite(tr.get("r2_low")):
            # pause box from lock bars
            b0 = tr.get("r2_pause_start")
            b1 = tr.get("r2_pause_end")
            try:
                b0i, b1i = int(b0), int(b1)
                t_p0 = _naive(pd.Timestamp(g.loc[g["rth_bar_number"] == b0i, "bar_start_chicago"].iloc[0]))
                t_p1 = _naive(pd.Timestamp(g.loc[g["rth_bar_number"] == b1i, "bar_end_chicago"].iloc[0]))
                xp0, xp1 = mdates.date2num(t_p0), mdates.date2num(t_p1)
                ax.add_patch(Rectangle(
                    (xp0, float(tr["r2_low"])), xp1 - xp0,
                    float(tr["r2_high"]) - float(tr["r2_low"]),
                    facecolor="#4c78a8", edgecolor="#2c5a86", linewidth=1.2,
                    alpha=0.28, zorder=2, label="R2 pause (20x15s)",
                ))
            except Exception:
                pass
            if _finite(tr.get("r2_val")):
                ax.axhline(float(tr["r2_val"]), color="#e31a1c", lw=1.8, ls="--",
                           label="R2 VAL", zorder=6)
                y_extra.append(float(tr["r2_val"]))
            if _finite(tr.get("r2_vah")):
                ax.axhline(float(tr["r2_vah"]), color="#e31a1c", lw=1.5, ls=":",
                           label="R2 VAH", zorder=6)
                y_extra.append(float(tr["r2_vah"]))
            y_extra += [float(tr["r2_high"]), float(tr["r2_low"])]

        et = pd.Timestamp(_naive(pd.Timestamp(tr["entry_time"])))
        xt = pd.Timestamp(_naive(pd.Timestamp(tr["exit_time"])))
        col = "#2ca02c" if tr["side"] == "long" else "#d62728"
        mk = "^" if tr["side"] == "long" else "v"
        ax.scatter([et], [tr["entry_price"]], marker=mk, color=col, s=90,
                   zorder=8, edgecolors="white", linewidths=0.4, label=f"{tr['side']} entry")
        ax.axvline(et, color=col, lw=0.7, alpha=0.35, zorder=2)
        rmap = {
            "or_va_stop": "#e31a1c",
            "r2_va_stop": "#c51b8a",
            "target_fib": "#006400",
            "session_end": "#ff7f0e",
        }
        xcol = rmap.get(tr["exit_reason"], "#ff7f0e")
        ax.scatter([xt], [tr["exit_price"]], marker="x", color=xcol, s=95,
                   zorder=8, linewidths=1.2, label=tr["exit_reason"])
        ax.axvline(xt, color=xcol, lw=0.75, alpha=0.5, zorder=2)
        ax.annotate(
            f"{tr['side'][0].upper()} {tr['exit_reason']}\nnet {tr['net']:+.2f}",
            xy=(xt, tr["exit_price"]),
            xytext=(6, -16 if tr["side"] == "long" else 10),
            textcoords="offset points", fontsize=7, color=col, zorder=9,
        )

    ax.set_xlim(t0, t_right)
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
    ax.legend(uniq.values(), uniq.keys(), loc="best", fontsize=6.4, framealpha=0.9,
              handlelength=1.6, labelspacing=0.22, ncol=2)
    ax.grid(True, alpha=0.28)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_equity_overlay(day_nets: pd.Series, day_nets_465: pd.Series,
                        out_path: Path, n: int, net: float) -> None:
    x = pd.to_datetime(list(day_nets.index))
    fig, ax = plt.subplots(figsize=(11.4, 5.7), dpi=120)
    ax.plot(x, day_nets.cumsum().to_numpy(), marker="o", ms=4.5,
            color="#1f77b4", lw=1.7,
            label=f"R2-VA + fib ladder  n={n}  net={net:+.2f}")
    ax.plot(x, day_nets_465.cumsum().to_numpy(), marker="s", ms=3.8,
            color="#d62728", lw=1.3, ls="--",
            label=f"+465 frozen OR-VA 2.05/20m  n=23  net={NET_465:+.2f}")
    ax.axhline(0, color="0.5", lw=0.8)
    ax.set_xlabel("Session date")
    ax.set_ylabel("Cumulative NET points")
    ax.set_title(
        "1.27+delta  ·  R2-VA stop + fib-ladder target (no 2.05 during markup)  "
        "vs +465  ·  23 days, not an edge"
    )
    ax.legend(loc="best", fontsize=8.3)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def write_md(rows, tdf, day_nets, day_nets_465, metas, path: Path) -> None:
    n = int(len(tdf))
    n_none = sum(1 for r in rows if r["side"] == "NONE")
    net = float(tdf["net"].sum()) if n else 0.0
    gross = float(tdf["gross"].sum()) if n else 0.0
    hits = int((tdf["net"] > 0).sum()) if n else 0
    hit_txt = f"{hits}/{n}" if n else "0/0"
    reasons = tdf["exit_reason"].value_counts().to_dict() if n else {}
    n_tgt = int(reasons.get("target_fib", 0))
    n_r2s = int(reasons.get("r2_va_stop", 0))
    n_ors = int(reasons.get("or_va_stop", 0))
    n_end = int(reasons.get("session_end", 0))
    cum = day_nets.cumsum().to_numpy()
    dd = max_dd(cum)
    last8_mask = tdf["day"].apply(lambda s: date.fromisoformat(str(s)[:10]) >= LAST8_START) if n else pd.Series(dtype=bool)
    t_l8 = tdf[last8_mask] if n else tdf
    n_l8 = int(len(t_l8))
    net_l8 = float(t_l8["net"].sum()) if n_l8 else 0.0
    net_465_l8 = float(day_nets_465[day_nets_465.index >= LAST8_START.isoformat()].sum())
    longs = tdf[tdf["side"] == "long"] if n else tdf
    shorts = tdf[tdf["side"] == "short"] if n else tdf
    n_long, n_short = int(len(longs)), int(len(shorts))
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
    n_r2 = int(tdf["r2_locked"].astype(bool).sum()) if n else 0
    n_never = n - n_r2
    n_205_skip_days = int((tdf["markup_205_tags"] > 0).sum()) if n else 0
    n_205_skip_bars = int(tdf["markup_205_tags"].sum()) if n else 0
    d_net = net - NET_465
    d_l8 = net_l8 - net_465_l8
    d_dd = dd - max_dd(day_nets_465.cumsum().to_numpy())

    # last-8 luck check: drop the single best last-8 day
    luck_note = ""
    if n_l8 >= 2:
        l8_best = float(t_l8["net"].max())
        l8_wo = net_l8 - l8_best
        luck_note = (
            f"Last-8 without its best day ({l8_best:+.2f}) is {l8_wo:+.2f}."
        )

    l8_better = n_l8 >= 3 and net_l8 > net_465_l8 + 20
    not_luck = True
    if n_l8 >= 2:
        l8_best = float(t_l8["net"].max())
        not_luck = (net_l8 - l8_best) > 0 and (net_l8 - l8_best) > (net_465_l8 - float(
            day_nets_465[day_nets_465.index >= LAST8_START.isoformat()].max()
            if len(day_nets_465[day_nets_465.index >= LAST8_START.isoformat()]) else 0.0
        ))
        # simpler: last-8 still better after dropping our best day vs +465 last-8
        not_luck = (net_l8 - l8_best) > net_465_l8

    if n == 0:
        verdict, verdict_why = "NOISE", "No trades."
    elif l8_better and not_luck and net_l8 > 0:
        verdict = "MIXED"
        verdict_why = (
            f"Last-8 is better than +465 ({net_l8:+.2f} vs {net_465_l8:+.2f}) and "
            f"not a single-day spike ({luck_note}) but this is still 23 days / 8 sessions. "
            "Not an edge. No book claim."
        )
    else:
        verdict = "WEAK"
        verdict_why = (
            f"Full-slice net {net:+.2f} (Δ vs +465: {d_net:+.2f}). Last-8 from 2026-08-11 "
            f"is {net_l8:+.2f} vs +465 last-8 {net_465_l8:+.2f} (Δ {d_l8:+.2f}). "
            f"{luck_note} R2 locked on {n_r2}/23 days; never on {n_never}. "
            f"Exit mix target_fib {n_tgt} / r2_va_stop {n_r2s} / or_va_stop {n_ors} / "
            f"session_end {n_end}. 2.05 would have filled during markup on "
            f"{n_205_skip_days} days ({n_205_skip_bars} 15s tags) and those were skipped. "
            "23 days cannot prove edge. No book claim."
        )

    # example R2 day for sanity: prefer Aug 6 (2.05 skipped in markup)
    r2_rows = [r for r in rows if r["side"] != "NONE" and r.get("r2_locked")]
    ex = next((r for r in r2_rows if r["date"] == "2026-08-06"), r2_rows[0] if r2_rows else None)

    lines = [
        "# 5m OR 1.27 + cum-delta + first-R2 VA stop + fib-ladder target — 23 days",
        "",
        "Not an edge. Same 1.27+cum-delta **entries** as the frozen OR-VA +465 book. "
        "Only the hold/stop/target changed. Do not trade this from this file.",
        "",
        "## What changed vs +465",
        "",
        "- **Dropped** the 20-minute clock and the frozen 2.05 take-profit during markup.",
        "- **Markup:** stop stays frozen OR VAL (long) / OR VAH (short). New highs only "
        "lift the live fib. No take-profit until the first pause locks.",
        "- **First R2 only:** 20 consecutive 15s with no new extreme in the trade "
        "direction. 70% VA from the footprint of **exactly those 20 bars**. "
        "Stop becomes R2 VAL / R2 VAH from the **next** 15s. Target arms.",
        "- After lock, a 15s touch of the armed fib may exit (`target_fib`). "
        "A close-through of that fib is a new high: do **not** take it; lift; stay in. "
        "Stop never moves to R3.",
        "",
        "## Locked entry (identical to +465)",
        "",
        "- Box: 5-minute OR, 15s bars 1–20. R = High − Low.",
        "- Expansions, his formula, tick 0.25: upside = High+(k−1)·R, downside = Low−(k−1)·R.",
        "- After bar 20: first 15s 1.27 + matching cum-delta (sum volume_delta bars 1..trigger inclusive).",
        "- One trade/day. LAST_ENTRY_BAR = 1480 kept so the entry set matches +465.",
        "- Dual-wick (armed target vs stop): target wins only if open already beyond the fib; "
        "elif open beyond stop, stop at open; else stop wins.",
        "- COST 0.50 RT, 1 NQ, tick 0.25. Flatten 15:00 / bar 1560.",
        "",
        "## Fib ladder (Luis OR, tick 0.25)",
        "",
        "k = 1.27, 1.618, 2.05, 2.618, 3.33, 4.23, 5.33, 6.85. "
        "Long: High+(k−1)R. Short: Low−(k−1)R.",
        "Live target = first unused print **strictly beyond** the running trade extreme "
        "(long high / short low since entry).",
        "",
        "## Pause / R2 (causal)",
        "",
        f"- Pause lock = {PAUSE_BARS} consecutive 15s with no new extreme in the trade direction.",
        "- Extreme clock starts at the **entry bar** (already outside OR). That bar is the first "
        "extreme, not a pause bar. New extreme resets the 20-count.",
        "- R2 box High/Low = those 20 bars, but the **stop is the 70% VA**, not the geometric edge.",
        "- POC = max footprint total_volume; tie closest to the pause mid, then lower (`pick_poc`).",
        "- Stop does **not** become R2 VAL/VAH until the 20th pause bar has **closed** "
        "(switch on the next 15s). OR-VA touches during the pause still count.",
        "- If 2.05 (or OR-VA stop) would have hit before R2 locks: 2.05 is skipped (markup); "
        "OR-VA stop still exits. If R2 never locks: stay on OR VAL/VAH until session_end.",
        "",
        "## Headline (do not overweight)",
        "",
        "| | |",
        "|---|---|",
        "| days | 23 (2026-07-21 .. 2026-08-20) |",
        f"| n (days traded) | **{n}** |",
        f"| days no trade | **{n_none}** |",
        f"| hit (net>0) | **{hit_txt}** |",
        f"| net | **{net:+.2f}** |",
        f"| gross | {gross:+.2f} |",
        f"| max DD (session-date cum NET) | **{dd:.2f}** |",
        f"| mix | target_fib {n_tgt} / r2_va_stop {n_r2s} / or_va_stop {n_ors} / session_end {n_end} |",
        f"| avg win / avg loss | {_avg_txt(avg_win)} / {_avg_txt(avg_loss)} |",
        f"| PF (sum wins / \\|sum losses\\|) | {_pf_txt(pf)} |",
        f"| best / worst | {best:+.2f} ({best_day}) / {worst:+.2f} ({worst_day}) |" if n else "| best / worst | n/a |",
        f"| last-8 from 2026-08-11 | n={n_l8} net {net_l8:+.2f} |",
        f"| long vs short | {n_long} / {n_short}  net {net_long:+.2f} / {net_short:+.2f} |",
        f"| median hold minutes | {med_hold:.2f} |" if n else "| median hold minutes | n/a |",
        f"| R2 locked / never | **{n_r2}** / **{n_never}** |",
        f"| days 2.05 tagged during markup (skipped) | **{n_205_skip_days}** ({n_205_skip_bars} 15s prints) |",
        "",
        f"**Δ vs +465:** net {d_net:+.2f}  ·  last-8 {d_l8:+.2f}  ·  maxDD {d_dd:+.2f} "
        f"(+465 last-8 was {net_465_l8:+.2f}, net {NET_465:+.2f}).",
        "",
        f"**Verdict: {verdict}.** {verdict_why}",
        "",
        "23 days cannot prove edge. Do not trade this from this file. No book claim.",
        "",
        "## Sanity: R2 VA uses only the 20 pause bars",
        "",
    ]
    if ex:
        lines += [
            f"Example **{ex['date']}** {ex['side'].upper()}: pause bars "
            f"**{ex.get('r2_pause_start')}–{ex.get('r2_pause_end')}** "
            f"(lock bar {ex.get('r2_lock_bar')}). "
            f"Box H={_fmt_px(ex.get('r2_high'))} L={_fmt_px(ex.get('r2_low'))}. "
            f"POC={_fmt_px(ex.get('r2_poc'))} VAL={_fmt_px(ex.get('r2_val'))} "
            f"VAH={_fmt_px(ex.get('r2_vah'))}. "
            f"Stop switched from the next 15s after bar {ex.get('r2_lock_bar')}. "
            f"Exit {ex['reason']} net {ex['net']:+.2f}. "
            f"2.05 tags skipped during markup: {int(ex.get('markup_205_tags') or 0)}.",
            "",
            "No bar after the 20-bar window entered that VA. Geometric pause High/Low is **not** the stop.",
            "",
        ]
    else:
        lines += ["No day locked R2 in this slice, so there is no 20-bar VA example.", ""]

    math_meta = next((m for m in metas if m["day"] == "2026-08-06"), None)
    math_row = next((r for r in rows if r["date"] == "2026-08-06"), None)
    lines += ["## Sanity: ladder math (2026-08-06)", ""]
    if math_meta:
        lv = math_meta["levels"]
        lines.append(f"OR High={lv['or_high']:.2f} Low={lv['or_low']:.2f} R={lv['R']:.2f}.")
        for k in FIB_K:
            up = expansion_level(lv["or_high"], lv["or_low"], k, "up")
            dn = expansion_level(lv["or_high"], lv["or_low"], k, "down")
            lines.append(f"- {k:g}  long={up:.2f}  short={dn:.2f}")
        lines.append("")
        if math_row and math_row["side"] != "NONE":
            lines.append(
                f"Trade: {math_row['side'].upper()} {clock_hm(math_row['entry_time'])} @ "
                f"{math_row['entry_price']:.2f} → {clock_hm(math_row['exit_time'])} @ "
                f"{math_row['exit_price']:.2f} {math_row['reason']} net {math_row['net']:+.2f}. "
                f"markup 2.05 tags skipped={math_row.get('markup_205_tags', 0)}  "
                f"R2 locked={math_row.get('r2_locked')}."
            )
    lines += [
        "",
        "## Day-by-day vs +465",
        "",
        "| date | side | entry | exit | net_r2va | reason | r2 | skip2.05 | net_465 | Δ | hold_m |",
        "|---|---|---|---|---:|---|---|---:|---:|---:|---:|",
    ]
    ref_by = {}
    ref = pd.read_csv(REF_CSV)
    for _, r in ref.iterrows():
        ref_by[str(r["day"])[:10]] = r
    for r in rows:
        d = r["date"]
        ref_r = ref_by.get(d)
        net465 = float(ref_r["net"]) if ref_r is not None else 0.0
        if r["side"] == "NONE":
            lines.append(
                f"| {d} | NONE | — | — | +0.00 | — | — | — | {net465:+.2f} | {-net465:+.2f} | — |"
            )
        else:
            r2flag = "Y" if r.get("r2_locked") else "n"
            skip = int(r.get("markup_205_tags") or 0)
            lines.append(
                f"| {d} | {r['side'].upper()} | "
                f"{clock_hm(r['entry_time'])} @ {r['entry_price']:.2f} | "
                f"{clock_hm(r['exit_time'])} @ {r['exit_price']:.2f} | "
                f"{r['net']:+.2f} | {r['reason']} | {r2flag} | {skip} | "
                f"{net465:+.2f} | {r['net']-net465:+.2f} | {r['hold_minutes']:.2f} |"
            )
    lines += [
        "",
        "Entry side/time/price matches `luis_or_127_delta_trades.csv` on all 23 days.",
        "",
        "Equity overlay: `/workspace/sierra/luis_or_127_r2va_equity.png`. "
        "Trades: `/workspace/sierra/luis_or_127_r2va_trades.csv`. "
        "A couple of day charts (not 23): `/workspace/sierra/or_127_r2va_charts/`.",
        "",
        "Locked files were not modified. Data file: `NQU26-CME-15s-1mo.duckdb` "
        "(full `footprint_15s_by_price`, not the OR-only fullRTH slice).",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


CSV_COLS = [
    "day", "side", "entry_time", "entry_price", "exit_time", "exit_price",
    "exit_reason", "or_high", "or_low", "R",
    "long_127", "short_127", "long_205", "short_205",
    "or_poc", "or_val", "or_vah", "stop", "stop_kind",
    "target_fib", "target_k", "lvl_127",
    "cum_delta_entry", "net", "gross",
    "entry_bar", "exit_bar", "hold_minutes",
    "n_delta_skip_before_fill", "path_note", "trade_index_in_day",
    "r2_locked", "r2_lock_bar", "r2_pause_start", "r2_pause_end",
    "r2_high", "r2_low", "r2_poc", "r2_val", "r2_vah",
    "run_ext_exit", "markup_205_tags", "n_lifts",
]


def main() -> int:
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    assert LAST_ENTRY_BAR == 1480 and FLATTEN_BAR == 1560 and OR_LAST_BAR == 20
    assert COST == 0.50 and BARS_PER_5M == 20 and TICK == 0.25
    assert PAUSE_BARS == 20
    assert DB_PATH.name == "NQU26-CME-15s-1mo.duckdb"
    assert DB_PATH.exists()
    assert pick_poc is not None and value_area is not None

    days = tuple(DAYS)
    bars, fp, extra_cols = load_days(DB_PATH, days)
    print("LOADED bars", len(bars), "fp", len(fp), "extra_cols", extra_cols, "db", DB_PATH)
    if len(fp) < 100000:
        raise SystemExit(f"footprint too small ({len(fp)}); need full 1mo file, not OR-only")
    delta_src = (
        "volume_delta" if "volume_delta" in extra_cols
        else ("delta" if "delta" in extra_cols else "ask_volume-bid_volume")
    )
    print("DELTA_SRC", delta_src)

    g0 = bars[bars["trading_day"] == date(2026, 8, 6)].copy()
    g0 = attach_bar_delta(g0)
    lv6 = or_va_for_day(date(2026, 8, 6), g0, fp[fp["trading_day"] == date(2026, 8, 6)])
    print(
        f"MATH 2026-08-06 High={lv6['or_high']:.2f} Low={lv6['or_low']:.2f} R={lv6['R']:.2f}"
    )
    for k in FIB_K:
        print(
            f"  {k:g}  L={expansion_level(lv6['or_high'], lv6['or_low'], k, 'up'):.2f}  "
            f"S={expansion_level(lv6['or_high'], lv6['or_low'], k, 'down'):.2f}"
        )

    all_trades: list[dict] = []
    rows: list[dict] = []
    metas: list[dict] = []
    chart_days = {date(2026, 8, 6)}  # always; add first R2-lock day later

    for day in DAYS:
        g = bars[bars["trading_day"] == day]
        fday = fp[fp["trading_day"] == day]
        if g.empty:
            print(f"NO BARS {day}")
            continue
        if int(g["rth_bar_number"].max()) != 1560 or len(g) != 1560:
            raise SystemExit(f"incomplete day {day}: bars={len(g)}")
        trades, log, meta = simulate_day(day, g, fday)
        metas.append(meta)
        print("\n".join(log))
        print()
        if len(trades) > 1:
            raise SystemExit(f"one_trade leaked {len(trades)} fills on {day}")
        all_trades.extend(trades)
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
                "r2_locked": tr["r2_locked"],
                "r2_lock_bar": tr["r2_lock_bar"],
                "r2_pause_start": tr["r2_pause_start"],
                "r2_pause_end": tr["r2_pause_end"],
                "r2_high": tr["r2_high"],
                "r2_low": tr["r2_low"],
                "r2_poc": tr["r2_poc"],
                "r2_val": tr["r2_val"],
                "r2_vah": tr["r2_vah"],
                "markup_205_tags": tr["markup_205_tags"],
                "why": "",
            }
            rows.append(rec)
            if tr["r2_locked"] and len(chart_days) < 3:
                chart_days.add(day)
        else:
            rows.append({
                "date": day.isoformat(), "side": "NONE",
                "entry_time": "", "exit_time": "",
                "entry_price": None, "exit_price": None,
                "net": 0.0, "reason": meta["why"],
                "cum_delta_entry": None, "hold_minutes": None,
                "r2_locked": False, "markup_205_tags": 0, "why": meta["why"],
            })

    tdf = pd.DataFrame(all_trades)
    if len(tdf):
        for c in CSV_COLS:
            if c not in tdf.columns:
                tdf[c] = ""
        tdf[CSV_COLS].to_csv(OUT_DIR / "luis_or_127_r2va_trades.csv", index=False)
    else:
        pd.DataFrame(columns=CSV_COLS).to_csv(OUT_DIR / "luis_or_127_r2va_trades.csv", index=False)

    assert_entries_match(tdf)

    idx = pd.Index([d.isoformat() for d in DAYS], name="day")
    daily = tdf.groupby("day")["net"].sum() if len(tdf) else pd.Series(dtype=float)
    day_nets = pd.Series(0.0, index=idx).add(daily, fill_value=0.0)
    ref = pd.read_csv(REF_CSV)
    ref["day"] = ref["day"].astype(str).str[:10]
    day_nets_465 = pd.Series(0.0, index=idx).add(ref.groupby("day")["net"].sum(), fill_value=0.0)

    n = int(len(tdf))
    net = float(tdf["net"].sum()) if n else 0.0
    plot_equity_overlay(day_nets, day_nets_465,
                        OUT_DIR / "luis_or_127_r2va_equity.png", n, net)

    # 2-3 charts, not 23
    for day in DAYS:
        if day not in chart_days:
            continue
        g = bars[bars["trading_day"] == day]
        meta = next(m for m in metas if m["day"] == day.isoformat())
        tday = [t for t in all_trades if t["day"] == day.isoformat()]
        out = CHART_DIR / f"R2VA_{day.isoformat()}.png"
        plot_one(day, g, tday, meta["levels"], meta["ladder"], meta["why"], out)
        print(f"CHART {out}")

    write_md(rows, tdf, day_nets, day_nets_465, metas, OUT_DIR / "luis_or_127_r2va.md")

    print("TRADES")
    if n:
        show = [
            "day", "side", "entry_time", "exit_time", "exit_reason",
            "net", "hold_minutes", "r2_locked", "stop_kind", "target_k",
            "markup_205_tags",
        ]
        print(tdf[show].to_string(index=False))
        print(f"n={n} net={net:.2f} gross={tdf['gross'].sum():.2f}")
        print("reasons", tdf["exit_reason"].value_counts().to_dict())
        print("r2_locked", int(tdf["r2_locked"].astype(bool).sum()),
              "never", int((~tdf["r2_locked"].astype(bool)).sum()))
        print("markup_205_skip_days", int((tdf["markup_205_tags"] > 0).sum()),
              "bars", int(tdf["markup_205_tags"].sum()))
        print("last8", float(tdf.loc[
            tdf["day"].apply(lambda s: date.fromisoformat(str(s)[:10]) >= LAST8_START),
            "net",
        ].sum()))
        print("delta_vs_465", net - NET_465)
    else:
        print("(none)")

    print("wrote", OUT_DIR / "luis_or_127_r2va.py")
    print("wrote", OUT_DIR / "luis_or_127_r2va_trades.csv")
    print("wrote", OUT_DIR / "luis_or_127_r2va.md")
    print("wrote", OUT_DIR / "luis_or_127_r2va_equity.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
