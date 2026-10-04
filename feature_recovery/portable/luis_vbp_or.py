#!/usr/bin/env python3
"""5-minute OR box + 5m VBP-POC hold. New script only; does not touch luis_or_system.py.

Locked rules (Luis confirmed):
  Box: 5-minute OR only, 15s bars 1..20 (08:30-08:35 Chicago). No 10/15/30 confluence,
  no 1.27, no hard stop.
  Entry: after bar 20, first 15s high>=OR High -> LONG (fill High, or open if gapped).
         first 15s low<=OR Low -> SHORT (fill Low, or open if gapped). Both same bar: skip.
  Hold: only on completed 5m bars. Long stays iff this 5m VBP POC > previous (strict).
        Short stays iff this 5m VBP POC < previous. Else exit at that 5m close (poc_stall).
  Re-entry: opposite break always; same-side only after a visit back into the box.
  Flatten leftover at rth_bar 1560. No new entry after 1480. COST 0.50 RT. 1 contract.

Default run is TWO DAYS only (2026-07-28, 2026-08-06) so the rule can be eyeballed
before a full-month pass. Do not claim edge.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

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

DB_PATH = Path("/workspace/sierra/NQU26-CME-15s-1mo.duckdb")
OUT_DIR = Path("/workspace/sierra")
CHART_DIR = OUT_DIR / "vbp_or_charts"

TICK = 0.25
COST = 0.50
BARS_PER_5M = 20
OR_LAST_BAR = 20
LAST_ENTRY_BAR = 1480
FLATTEN_BAR = 1560

# Eyeball slice only. Full month is a later pass.
RUN_DAYS = (date(2026, 7, 28), date(2026, 8, 6))


def ts(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    if isinstance(x, datetime):
        return x.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(x, pd.Timestamp):
        return x.strftime("%Y-%m-%d %H:%M:%S")
    return str(x)[:19]


def clock_hm(x) -> str:
    s = ts(x)
    return s[11:19] if len(s) >= 19 else s


def overlaps_box(low: float, high: float, or_low: float, or_high: float) -> bool:
    return (high >= or_low - 1e-12) and (low <= or_high + 1e-12)


def pick_poc(prices: np.ndarray, volumes: np.ndarray, close: float) -> tuple[float, float]:
    """Max volume. Tie: closest to 5m close; still tied: lower price."""
    if len(prices) == 0:
        return float("nan"), 0.0
    max_vol = float(volumes.max())
    tied = prices[volumes == volumes.max()]
    if len(tied) == 1:
        return float(tied[0]), max_vol
    dist = np.abs(tied - close)
    closest = tied[dist == dist.min()]
    return float(closest.min()), max_vol


def load_days(db_path: Path, days: tuple[date, ...]):
    day_list = ", ".join(f"DATE '{d.isoformat()}'" for d in days)
    con = duckdb.connect(str(db_path), read_only=True)
    bars = con.execute(
        f"""
        SELECT trading_day, rth_bar_number, bar_start_chicago, bar_end_chicago,
               open, high, low, close, total_volume
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
    return bars, fp


def build_5m_vbp(bars: pd.DataFrame, fp: pd.DataFrame) -> pd.DataFrame:
    """One row per completed 5m bar. POC from summed footprint total_volume by price.
    No peek: this row only exists once the 5m bar has closed.
    """
    rows = []
    for day, g in bars.groupby("trading_day", sort=True):
        g = g.sort_values("rth_bar_number")
        fday = fp[fp["trading_day"] == day]
        nbar = int(g["rth_bar_number"].max())
        n5 = nbar // BARS_PER_5M
        bn = g["rth_bar_number"].to_numpy()
        for k in range(1, n5 + 1):
            b0 = (k - 1) * BARS_PER_5M + 1
            b1 = k * BARS_PER_5M
            chunk = g[(g["rth_bar_number"] >= b0) & (g["rth_bar_number"] <= b1)]
            hi = float(chunk["high"].max())
            lo = float(chunk["low"].min())
            cl = float(chunk.loc[chunk["rth_bar_number"] == b1, "close"].iloc[0])
            t_start = chunk.loc[chunk["rth_bar_number"] == b0, "bar_start_chicago"].iloc[0]
            t_end = chunk.loc[chunk["rth_bar_number"] == b1, "bar_end_chicago"].iloc[0]
            vol = float(chunk["total_volume"].sum())
            fchunk = fday[(fday["rth_bar_number"] >= b0) & (fday["rth_bar_number"] <= b1)]
            if len(fchunk):
                grp = fchunk.groupby("price", sort=True)["total_volume"].sum()
                prices = grp.index.to_numpy(dtype=float)
                volumes = grp.to_numpy(dtype=float)
                poc, poc_vol = pick_poc(prices, volumes, cl)
            else:
                poc, poc_vol = float("nan"), 0.0
            rows.append({
                "trading_day": day,
                "m5_bar": k,
                "rth_bar_end": b1,
                "t_start": t_start,
                "t_end": t_end,
                "high": hi,
                "low": lo,
                "close": cl,
                "volume": vol,
                "poc": poc,
                "poc_volume": poc_vol,
            })
    return pd.DataFrame(rows)


def simulate_day(day: date, g: pd.DataFrame, five: pd.DataFrame, *, one_trade: bool = False) -> tuple[list[dict], list[str]]:
    g = g.sort_values("rth_bar_number").reset_index(drop=True)
    five = five.sort_values("m5_bar").reset_index(drop=True)
    poc_by_k = {int(r.m5_bar): float(r.poc) for r in five.itertuples(index=False)}
    close_by_k = {int(r.m5_bar): float(r.close) for r in five.itertuples(index=False)}
    tend_by_k = {int(r.m5_bar): r.t_end for r in five.itertuples(index=False)}

    or_chunk = g[g["rth_bar_number"] <= OR_LAST_BAR]
    or_high = float(or_chunk["high"].max())
    or_low = float(or_chunk["low"].min())
    log = [
        f"{day.isoformat()} OR High={or_high:.2f} Low={or_low:.2f} width={or_high - or_low:.2f}",
        f"  5m#1 (OR) POC={poc_by_k.get(1, float('nan')):.2f} close={close_by_k.get(1, float('nan')):.2f}",
    ]

    by_bn = {int(r.rth_bar_number): r for r in g.itertuples(index=False)}

    trades: list[dict] = []
    pos = None  # dict while in a trade
    last_exit_side = None
    last_exit_bar = 0
    visited_box = False
    hold_notes: list[str] = []

    def open_trade(side: str, row, fill: float, via_visit: bool) -> None:
        nonlocal pos
        bn = int(row.rth_bar_number)
        k_entry = (bn + BARS_PER_5M - 1) // BARS_PER_5M
        k_prev = (bn - 1) // BARS_PER_5M  # last completed 5m at or before this 15s
        poc_prev = poc_by_k[k_prev]
        pos = {
            "side": side,
            "entry_bar": bn,
            "entry_time": row.bar_start_chicago,
            "entry_price": float(fill),
            "entry_open": float(row.open),
            "entry_high": float(row.high),
            "entry_low": float(row.low),
            "k_entry": k_entry,
            "poc_prev": poc_prev,
            "poc_prev_at_entry": poc_prev,
            "k_prev_at_entry": k_prev,
            "n_5m_held": 0,
            "visited_box_reentry": 1 if via_visit else 0,
            "hold_path": [],
        }
        tag = "same-side after box visit" if via_visit else (
            "first of day" if last_exit_side is None else "opposite break"
        )
        log.append(
            f"  ENTRY {side.upper()} bar {bn} {clock_hm(row.bar_start_chicago)} "
            f"fill={fill:.2f} open={row.open:.2f} H={row.high:.2f} L={row.low:.2f} "
            f"({tag}) poc_prev=k{k_prev}:{poc_prev:.2f} first_check=k{k_entry}"
        )

    def close_trade(row, exit_px: float, reason: str, poc_exit: float, poc_prev: float) -> None:
        nonlocal pos, last_exit_side, last_exit_bar, visited_box
        side = pos["side"]
        gross = (exit_px - pos["entry_price"]) if side == "long" else (pos["entry_price"] - exit_px)
        net = gross - COST
        t_exit = row.bar_end_chicago if int(row.rth_bar_number) % BARS_PER_5M == 0 else row.bar_end_chicago
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
            "poc_prev": round(float(poc_prev), 4),
            "poc_exit": round(float(poc_exit), 4),
            "n_5m_held": int(pos["n_5m_held"]),
            "net": round(float(net), 4),
            "gross": round(float(gross), 4),
            "visited_box_reentry": int(pos["visited_box_reentry"]),
            "trade_index_in_day": len(trades) + 1,
            "entry_bar": pos["entry_bar"],
            "exit_bar": int(row.rth_bar_number),
            "poc_prev_at_entry": round(float(pos["poc_prev_at_entry"]), 4),
            "hold_path": " | ".join(pos["hold_path"]),
        }
        trades.append(rec)
        first_out = pos["n_5m_held"] == 1 and reason == "poc_stall"
        log.append(
            f"  EXIT  {side.upper()} bar {int(row.rth_bar_number)} {clock_hm(t_exit)} "
            f"fill={exit_px:.2f} reason={reason} poc_prev={poc_prev:.2f} poc_now={poc_exit:.2f} "
            f"n_5m_held={pos['n_5m_held']} gross={gross:.2f} net={net:.2f}"
            f"{'  [first 5m already exits]' if first_out else ''}"
        )
        last_exit_side = side
        last_exit_bar = int(row.rth_bar_number)
        visited_box = False
        pos = None

    for bn in range(OR_LAST_BAR + 1, FLATTEN_BAR + 1):
        row = by_bn[bn]
        o, h, l, c = float(row.open), float(row.high), float(row.low), float(row.close)
        is_5m_close = (bn % BARS_PER_5M == 0)

        if pos is None:
            if one_trade and last_exit_side is not None:
                # First fill already happened and closed. Stop looking.
                continue
            just_tagged_box = False
            if last_exit_side is not None and bn > last_exit_bar:
                if overlaps_box(l, h, or_low, or_high):
                    if not visited_box:
                        log.append(
                            f"  BOX VISIT bar {bn} {clock_hm(row.bar_start_chicago)} "
                            f"L={l:.2f} H={h:.2f} overlaps [{or_low:.2f},{or_high:.2f}] "
                            f"(visit does not enter)"
                        )
                        just_tagged_box = True
                    visited_box = True

            if bn <= LAST_ENTRY_BAR:
                long_brk = h >= or_high - 1e-12
                short_brk = l <= or_low + 1e-12
                if long_brk and short_brk:
                    log.append(
                        f"  SKIP both-sides bar {bn} {clock_hm(row.bar_start_chicago)} "
                        f"H={h:.2f} L={l:.2f}"
                    )
                else:
                    want_long = False
                    want_short = False
                    via_visit = False
                    if last_exit_side is None:
                        want_long = long_brk
                        want_short = short_brk
                    else:
                        # Opposite break is always allowed (no visit required).
                        if last_exit_side == "long" and short_brk:
                            want_short = True
                        if last_exit_side == "short" and long_brk:
                            want_long = True
                        # Same-side only after a PRIOR 15s already visited the box.
                        # Visiting this bar does not itself enter.
                        if last_exit_side == "long" and long_brk and visited_box and not just_tagged_box:
                            want_long = True
                            via_visit = True
                        if last_exit_side == "short" and short_brk and visited_box and not just_tagged_box:
                            want_short = True
                            via_visit = True
                    if want_long and want_short:
                        log.append(f"  SKIP both-wanted bar {bn}")
                    elif want_long:
                        fill = or_high if o < or_high else o
                        open_trade("long", row, fill, via_visit)
                    elif want_short:
                        fill = or_low if o > or_low else o
                        open_trade("short", row, fill, via_visit)

        if pos is not None and is_5m_close:
            k = bn // BARS_PER_5M
            poc_now = poc_by_k[k]
            poc_prev = pos["poc_prev"]
            pos["n_5m_held"] += 1
            side = pos["side"]
            if side == "long":
                stay = poc_now > poc_prev
            else:
                stay = poc_now < poc_prev
            step = poc_now - poc_prev
            note = (
                f"k{k} {clock_hm(row.bar_end_chicago)} POC={poc_now:.2f} vs prev={poc_prev:.2f} "
                f"d={step:+.2f} {'STAY' if stay else 'EXIT'} close={c:.2f}"
            )
            pos["hold_path"].append(note)
            log.append("    " + note)
            if not stay:
                close_trade(row, c, "poc_stall", poc_now, poc_prev)
            else:
                pos["poc_prev"] = poc_now

        if pos is not None and bn == FLATTEN_BAR:
            k = bn // BARS_PER_5M
            close_trade(row, c, "session_end", poc_by_k[k], pos["poc_prev"])

    if not trades:
        log.append("  NO TRADE")
    return trades, log


def draw_panel(ax, day, g, five, tday, t_left, t_right, or_high, or_low, *,
               label_poc: bool, title: str, annotate_trades: bool) -> None:
    t0 = datetime.combine(day, dtime(8, 30))
    box_end = datetime.combine(day, dtime(8, 35))
    vis = g[(g["t"] >= t_left - timedelta(minutes=1)) & (g["t"] <= t_right + timedelta(minutes=1))]
    ax.fill_between(vis["t"], vis["low"], vis["high"], color="0.82", alpha=0.55,
                    linewidth=0, label="15s range", zorder=1)
    ax.plot(vis["t"], vis["close"], color="0.18", lw=0.9, label="15s close", zorder=3)

    x0 = mdates.date2num(t0)
    x1 = mdates.date2num(box_end)
    ax.add_patch(Rectangle(
        (x0, or_low), x1 - x0, or_high - or_low,
        facecolor="#ffcc00", edgecolor="#aa8800", linewidth=1.15, alpha=0.48,
        zorder=2, label="5m OR box",
    ))
    ax.axhline(or_high, color="#aa8800", lw=1.15, zorder=3)
    ax.axhline(or_low, color="#aa8800", lw=1.0, zorder=3)

    fm = five[five["trading_day"] == day].sort_values("m5_bar").copy()
    fm["t"] = pd.to_datetime(fm["t_start"])
    fm_vis = fm[(fm["t"] >= t_left - timedelta(minutes=5)) & (fm["t"] <= t_right + timedelta(minutes=5))]
    if len(fm_vis):
        ax.plot(
            fm_vis["t"], fm_vis["poc"], color="#6a3d9a", lw=2.2,
            drawstyle="steps-post", label="5m VBP POC", zorder=4,
        )
        if label_poc:
            prev = None
            for _, r in fm_vis.iterrows():
                poc = float(r["poc"])
                tt = r["t"]
                if tt < t_left or tt > t_right:
                    continue
                if prev is None or abs(poc - prev) > 1e-9:
                    ax.annotate(
                        f"{poc:.2f}",
                        xy=(tt, poc),
                        xytext=(3, 7),
                        textcoords="offset points",
                        fontsize=7,
                        color="#6a3d9a",
                        zorder=5,
                    )
                    prev = poc

    used = set()

    def mark(xs, ys, marker, color, size, label, z=6):
        lab = label if label not in used else None
        if lab:
            used.add(label)
        kw = dict(marker=marker, color=color, s=size, zorder=z, label=lab, linewidths=1.2)
        if marker != "x":
            kw["edgecolors"] = "white"
            kw["linewidths"] = 0.4
        ax.scatter(xs, ys, **kw)

    for tr in tday:
        et = pd.Timestamp(tr["entry_time"])
        xt = pd.Timestamp(tr["exit_time"])
        if xt < t_left or et > t_right:
            continue
        side = tr["side"]
        col = "#2ca02c" if side == "long" else "#d62728"
        mk = "^" if side == "long" else "v"
        mark([et], [tr["entry_price"]], mk, col, 90, f"{side} entry")
        ax.axvline(et, color=col, lw=0.7, alpha=0.35, zorder=2)
        xcol = "#6a3d9a" if tr["exit_reason"] == "poc_stall" else "#ff7f0e"
        mark([xt], [tr["exit_price"]], "x", xcol, 95, tr["exit_reason"])
        ax.axvline(xt, color=xcol, lw=0.75, alpha=0.5, zorder=2)
        if annotate_trades:
            ax.annotate(
                f"T{tr['trade_index_in_day']} {side[0].upper()} {tr['exit_reason']}\n"
                f"net {tr['net']:+.2f}",
                xy=(xt, tr["exit_price"]),
                xytext=(6, -16 if side == "long" else 10),
                textcoords="offset points",
                fontsize=7,
                color=col,
                zorder=7,
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

    y_lo = min(float(vis["low"].min()), or_low)
    y_hi = max(float(vis["high"].max()), or_high)
    if len(fm_vis):
        y_lo = min(y_lo, float(fm_vis["poc"].min()))
        y_hi = max(y_hi, float(fm_vis["poc"].max()))
    pad_y = (y_hi - y_lo) * 0.05 if y_hi > y_lo else 8.0
    ax.set_ylim(y_lo - pad_y, y_hi + pad_y)
    ax.set_title(title, fontsize=10, pad=7)
    handles, labels = ax.get_legend_handles_labels()
    uniq = dict(zip(labels, handles))
    ax.legend(
        uniq.values(), uniq.keys(),
        loc="best", fontsize=7.2, framealpha=0.9,
        handlelength=1.7, labelspacing=0.28,
    )
    ax.grid(True, alpha=0.28)


def plot_day(day: date, g: pd.DataFrame, five: pd.DataFrame, trades: list[dict],
             out_path: Path) -> None:
    g = g.sort_values("rth_bar_number").copy()
    g["t"] = pd.to_datetime(g["bar_start_chicago"])
    or_chunk = g[g["rth_bar_number"] <= OR_LAST_BAR]
    or_high = float(or_chunk["high"].max())
    or_low = float(or_chunk["low"].min())
    t0 = datetime.combine(day, dtime(8, 30))
    t_sess_end = datetime.combine(day, dtime(15, 0))
    tday = [t for t in trades if t["day"] == day.isoformat()]

    if tday:
        last_exit = max(pd.Timestamp(t["exit_time"]) for t in tday)
        first_exit = min(pd.Timestamp(t["exit_time"]) for t in tday)
        pad = timedelta(minutes=20)
        morning_right = min(first_exit + pad, t_sess_end)
        if morning_right < datetime.combine(day, dtime(9, 15)):
            morning_right = datetime.combine(day, dtime(9, 15))
        full_right = min(last_exit + pad, t_sess_end)
        split = len(tday) >= 3 or (full_right - t0) > timedelta(hours=2)
    else:
        morning_right = datetime.combine(day, dtime(10, 0))
        full_right = morning_right
        split = False

    bits = []
    for tr in tday:
        bits.append(
            f"T{tr['trade_index_in_day']} {tr['side']} "
            f"{clock_hm(tr['entry_time'])}->{clock_hm(tr['exit_time'])} "
            f"{tr['exit_reason']} net={tr['net']:+.2f}"
        )
    head = f"{day.isoformat()}  5m-OR VBP-POC"
    bitstr = "  ·  ".join(bits) if bits else "no trade"

    if split:
        fig, axes = plt.subplots(2, 1, figsize=(14.5, 11.2), dpi=120)
        draw_panel(
            axes[0], day, g, five, tday, t0, morning_right, or_high, or_low,
            label_poc=True, annotate_trades=True,
            title=f"{head}  — first trade (POC steps)",
        )
        draw_panel(
            axes[1], day, g, five, tday, t0, full_right, or_high, or_low,
            label_poc=False, annotate_trades=True,
            title=f"{head}  — full window  {bitstr}",
        )
    else:
        fig, ax = plt.subplots(figsize=(14.5, 7.2), dpi=120)
        draw_panel(
            ax, day, g, five, tday, t0, morning_right, or_high, or_low,
            label_poc=True, annotate_trades=True,
            title=f"{head}   {bitstr}",
        )
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_one_trade(day: date, g: pd.DataFrame, five: pd.DataFrame, trades: list[dict],
                   out_path: Path) -> None:
    """08:30 through ~15 min after the single exit. One entry, one exit."""
    g = g.sort_values("rth_bar_number").copy()
    g["t"] = pd.to_datetime(g["bar_start_chicago"])
    or_chunk = g[g["rth_bar_number"] <= OR_LAST_BAR]
    or_high = float(or_chunk["high"].max())
    or_low = float(or_chunk["low"].min())
    t0 = datetime.combine(day, dtime(8, 30))
    t_sess_end = datetime.combine(day, dtime(15, 0))
    tday = [t for t in trades if t["day"] == day.isoformat()][:1]

    if tday:
        tr = tday[0]
        exit_t = pd.Timestamp(tr["exit_time"]).to_pydatetime()
        if exit_t.tzinfo is not None:
            exit_t = exit_t.replace(tzinfo=None)
        t_right = min(exit_t + timedelta(minutes=15), t_sess_end)
        title = (
            f"{day.isoformat()}  {tr['side'].upper()}  "
            f"{clock_hm(tr['entry_time'])} → {clock_hm(tr['exit_time'])}  "
            f"{tr['exit_reason']}  net {tr['net']:+.2f}"
        )
    else:
        t_right = datetime.combine(day, dtime(9, 15))
        title = f"{day.isoformat()}  5m-OR VBP-POC  no trade"

    fig, ax = plt.subplots(figsize=(14.5, 7.2), dpi=120)
    draw_panel(
        ax, day, g, five, tday, t0, t_right, or_high, or_low,
        label_poc=True, annotate_trades=True,
        title=title,
    )
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def run_one_trade_charts() -> list[dict]:
    """Locked play: first 5m OR break only. Two days. Charts ONE_*.png."""
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    bars, fp = load_days(DB_PATH, RUN_DAYS)
    five = build_5m_vbp(bars, fp)
    all_trades: list[dict] = []
    for day in RUN_DAYS:
        g = bars[bars["trading_day"] == day]
        fm = five[five["trading_day"] == day]
        if g.empty:
            print(f"NO BARS {day}")
            continue
        trades, log = simulate_day(day, g, fm, one_trade=True)
        all_trades.extend(trades)
        print("\n".join(log))
        print()
        out = CHART_DIR / f"ONE_{day.isoformat()}.png"
        plot_one_trade(day, g, fm, trades, out)
        print(f"CHART {out}")
        print()
    cols = [
        "day", "side", "entry_time", "entry_price", "exit_time", "exit_price",
        "exit_reason", "or_high", "or_low", "poc_prev", "poc_exit", "n_5m_held",
        "net", "gross", "visited_box_reentry", "trade_index_in_day",
    ]
    tdf = pd.DataFrame(all_trades)
    print("ONE-TRADE ROWS")
    if len(tdf):
        print(tdf[cols].to_string(index=False))
    else:
        print("(none)")
    return all_trades


def write_md(trades: list[dict], logs: dict[date, list[str]], five: pd.DataFrame,
             path: Path) -> None:
    lines = [
        "# 5m OR + 5m VBP POC — two-day eyeball",
        "",
        "Not a month backtest. Two sessions so the rule can be read on a chart. "
        "23 days cannot prove anything; two days prove less. No edge claim.",
        "",
        "Rules in force: 5-minute opening-range box only (bars 1–20). "
        "15s stop-entry at High/Low after 08:35. Hold a long only while each "
        "completed 5m VBP POC is strictly above the previous 5m POC "
        "(short: strictly below). Exit at that 5m close on a stall. "
        "Opposite break always re-enters; same-side only after price trades "
        "back through the box. No hard stop. Flatten at 15:00. COST 0.50 RT, 1 NQ.",
        "",
        "Compare later to first-touch+POC1 1.27 (n=15 net +64.75, two winners) "
        "once the full slice is run. Not compared here.",
        "",
    ]
    for day in RUN_DAYS:
        tday = [t for t in trades if t["day"] == day.isoformat()]
        lines.append(f"## {day.isoformat()}")
        lines.append("")
        fm = five[five["trading_day"] == day].sort_values("m5_bar")
        or1 = fm[fm["m5_bar"] == 1].iloc[0]
        lines.append(
            f"OR box (5m #1): High **{or1['high']:.2f}** / Low **{or1['low']:.2f}** "
            f"(width {or1['high'] - or1['low']:.2f}). "
            f"OR VBP POC **{or1['poc']:.2f}** (vol {or1['poc_volume']:.0f}), close {or1['close']:.2f}."
        )
        lines.append("")
        if not tday:
            lines.append("No trade.")
            lines.append("")
            continue
        for tr in tday:
            lines.append(
                f"**Trade {tr['trade_index_in_day']} {tr['side'].upper()}**  "
                f"entry {tr['entry_time'][11:]} @ {tr['entry_price']:.2f} → "
                f"exit {tr['exit_time'][11:]} @ {tr['exit_price']:.2f}  "
                f"`{tr['exit_reason']}`  gross {tr['gross']:+.2f} net {tr['net']:+.2f}  "
                f"n_5m_held={tr['n_5m_held']}  "
                f"poc_prev {tr['poc_prev']:.2f} vs poc_exit {tr['poc_exit']:.2f}  "
                f"visited_box_reentry={tr['visited_box_reentry']}"
            )
            if tr.get("hold_path"):
                lines.append("")
                lines.append("POC path while in:")
                for step in tr["hold_path"].split(" | "):
                    lines.append(f"- {step}")
            lines.append("")
        day_net = sum(t["net"] for t in tday)
        lines.append(f"Day net **{day_net:+.2f}** on {len(tday)} trade(s).")
        lines.append("")
        lines.append("Log:")
        lines.append("")
        lines.append("```")
        lines.extend(logs[day])
        lines.append("```")
        lines.append("")
        png = CHART_DIR / f"VBP_{day.isoformat()}.png"
        lines.append(f"Chart: `{png}`")
        lines.append("")
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    bars, fp = load_days(DB_PATH, RUN_DAYS)
    five = build_5m_vbp(bars, fp)

    all_trades: list[dict] = []
    logs: dict[date, list[str]] = {}
    for day in RUN_DAYS:
        g = bars[bars["trading_day"] == day]
        fm = five[five["trading_day"] == day]
        if g.empty:
            print(f"NO BARS {day}")
            continue
        trades, log = simulate_day(day, g, fm)
        all_trades.extend(trades)
        logs[day] = log
        print("\n".join(log))
        print()

    trade_cols = [
        "day", "side", "entry_time", "entry_price", "exit_time", "exit_price",
        "exit_reason", "or_high", "or_low", "poc_prev", "poc_exit", "n_5m_held",
        "net", "gross", "visited_box_reentry", "trade_index_in_day",
    ]
    tdf = pd.DataFrame(all_trades)
    if len(tdf):
        tdf[trade_cols].to_csv(OUT_DIR / "luis_vbp_or_trades.csv", index=False)
    else:
        pd.DataFrame(columns=trade_cols).to_csv(OUT_DIR / "luis_vbp_or_trades.csv", index=False)

    five_out = five.copy()
    five_out["day"] = five_out["trading_day"].astype(str)
    five_out[["day", "m5_bar", "t_start", "t_end", "poc", "close", "high", "low",
              "volume", "poc_volume"]].to_csv(OUT_DIR / "luis_vbp_or_5m.csv", index=False)

    for day in RUN_DAYS:
        g = bars[bars["trading_day"] == day]
        fm = five[five["trading_day"] == day]
        tday = [t for t in all_trades if t["day"] == day.isoformat()]
        out = CHART_DIR / f"VBP_{day.isoformat()}.png"
        plot_day(day, g, fm, tday, out)
        print(f"CHART {out}")

    write_md(all_trades, logs, five, OUT_DIR / "luis_vbp_or.md")

    print("TRADES")
    if len(tdf):
        print(tdf[trade_cols].to_string(index=False))
        print(f"n={len(tdf)} net={tdf['net'].sum():.2f} gross={tdf['gross'].sum():.2f}")
    else:
        print("(none)")
    print("wrote", OUT_DIR / "luis_vbp_or.py")
    print("wrote", OUT_DIR / "luis_vbp_or_trades.csv")
    print("wrote", OUT_DIR / "luis_vbp_or_5m.csv")
    print("wrote", OUT_DIR / "luis_vbp_or.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
