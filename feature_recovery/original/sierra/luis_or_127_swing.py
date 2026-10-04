#!/usr/bin/env python3
"""Jul 30 LONG: 1.27 swing-high, swing-low, break of that high = entry.

Last higher-low hold. Take only on bid/ask absorption at 2.05, 3.33, 4.23,
5.33, 6.85. 2-bar causal swings (confirmed 2 bars later). Jul 30 only.
Does not modify locked scripts. Does not start a 23-day book.
"""
from __future__ import annotations

import json, sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

sys.path.insert(0, "/workspace/sierra/.venv/lib/python3.13/site-packages")
sys.path.insert(0, "/workspace/sierra")

import duckdb
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd

from luis_or_127_close205 import (
    C_127_L, C_205_L, C_OR_EMPH, CHART_DIR, DAY, as_naive, hm_s, session_ticks, strip_tz, _jsonable,
)
from luis_or_127_delta import DB_PATH, _finite, attach_bar_delta, load_days, or_va_for_day
from luis_or_127_r2va import expansion_level
from luis_vbp_or import COST, FLATTEN_BAR, OR_LAST_BAR, ts
from plot_close205_vol import BAR_W, load_day_series, plot_cum_delta_line

OUT_PNG = CHART_DIR / f"SWING_{DAY.isoformat()}.png"
OUT_JSON = CHART_DIR / f"SWING_{DAY.isoformat()}.json"
PIVOT = 2
GAUGE = (2.05, 3.33, 4.23, 5.33, 6.85)
C_OR_GOLD = "#c9a227"
C_HL = "#9467bd"
C_ASK = "#2ca02c"
C_BID = "#d62728"
FIB_COL = {
    1.27: C_127_L, 2.05: C_205_L, 2.618: "#1f77b4",
    3.33: "#ff7f0e", 4.23: "#1f77b4", 5.33: "#e377c2", 6.85: "#8c564b",
}


def by_bn_map(g):
    return {int(r.rth_bar_number): r for r in g.itertuples(index=False)}


def is_sh(by_bn, p: int) -> bool:
    row = by_bn.get(p)
    if row is None:
        return False
    hp = float(row.high)
    for k in range(1, PIVOT + 1):
        a = by_bn.get(p - k)
        b = by_bn.get(p + k)
        if a is None or b is None:
            return False
        if hp < float(a.high) - 1e-12:
            return False
        if hp <= float(b.high) + 1e-12:
            return False
    return True


def is_sl(by_bn, p: int) -> bool:
    row = by_bn.get(p)
    if row is None:
        return False
    lp = float(row.low)
    for k in range(1, PIVOT + 1):
        a = by_bn.get(p - k)
        b = by_bn.get(p + k)
        if a is None or b is None:
            return False
        if lp > float(a.low) + 1e-12:
            return False
        if lp >= float(b.low) - 1e-12:
            return False
    return True


def rec_swing(kind, row):
    return {
        "kind": kind,
        "bar": int(row.rth_bar_number),
        "time": ts(as_naive(row.bar_start_chicago)),
        "time_hm": hm_s(row.bar_start_chicago),
        "high": float(row.high),
        "low": float(row.low),
        "close": float(row.close),
        "open": float(row.open),
    }


def high_ab(con, day, bn: int, high_px: float) -> dict:
    df = con.execute(
        f"""
        SELECT bid_volume, ask_volume, price FROM footprint_15s_by_price
        WHERE trading_day = DATE '{day.isoformat()}'
          AND rth_bar_number = {int(bn)}
          AND abs(price - {float(high_px)}) < 0.01
        """
    ).fetchdf()
    if df.empty:
        return {"ask": None, "bid": None, "absorbed": False, "price": high_px}
    ask = int(df.ask_volume.iloc[0])
    bid = int(df.bid_volume.iloc[0])
    return {"ask": ask, "bid": bid, "absorbed": bool(bid >= ask), "price": float(df.price.iloc[0])}


def simulate(g, fp_unused, con, day=None) -> dict:
    if day is None:
        day = DAY
    g = strip_tz(attach_bar_delta(g.copy()))
    lv = or_va_for_day(day, g, fp_unused)
    or_high, or_low = float(lv["or_high"]), float(lv["or_low"])
    or_val = float(lv["or_val"])
    long_127 = float(expansion_level(or_high, or_low, 1.27, "up"))
    fibs = [(k, float(expansion_level(or_high, or_low, k, "up"))) for k in GAUGE]
    by_bn = by_bn_map(g)

    first_sh = None
    pull_sl = None
    shs, sls = [], []
    entry_row = None
    entry_px = None
    live_hl = None
    hl_steps = []
    fib_events = []
    skipped = set()
    exit_row = None
    exit_px = None
    reason = None
    run_hod = None

    def live_working():
        for k, px in fibs:
            if k in skipped:
                continue
            return k, px
        return None, None

    for bn in range(1, FLATTEN_BAR + 1):
        row = by_bn.get(bn)
        if row is None:
            continue
        o, h, l, c = float(row.open), float(row.high), float(row.low), float(row.close)

        # confirm swing at bn-PIVOT
        p = bn - PIVOT
        if p >= 1:
            if is_sh(by_bn, p):
                sh = rec_swing("SH", by_bn[p])
                shs.append(sh)
                if first_sh is None and p > OR_LAST_BAR and sh["high"] >= long_127 - 1e-12:
                    first_sh = sh
            if is_sl(by_bn, p):
                sl = rec_swing("SL", by_bn[p])
                sls.append(sl)
                if first_sh is not None and pull_sl is None and sl["bar"] > first_sh["bar"]:
                    pull_sl = sl
                if entry_row is not None and sl["bar"] > int(entry_row.rth_bar_number):
                    if sl["low"] > live_hl + 1e-12:
                        t = as_naive(row.bar_start_chicago)
                        hl_steps.append({"t": ts(t), "hl": sl["low"], "bar": sl["bar"], "why": "new_HL"})
                        live_hl = sl["low"]

        if entry_row is None:
            if first_sh is not None and pull_sl is not None and bn > pull_sl["bar"]:
                if h > first_sh["high"] + 1e-12:
                    fill = first_sh["high"] if o <= first_sh["high"] + 1e-12 else o
                    entry_row, entry_px = row, float(fill)
                    live_hl = float(pull_sl["low"])
                    run_hod = h
                    hl_steps.append({
                        "t": ts(as_naive(row.bar_start_chicago)),
                        "hl": live_hl, "bar": int(row.rth_bar_number), "why": "entry_HL",
                    })
            if bn == FLATTEN_BAR and entry_row is None:
                break
            continue

        # in trade
        run_hod = max(run_hod, h)

        if l <= live_hl + 1e-12:
            if o <= live_hl + 1e-12:
                exit_px, reason = o, "hl_open_through"
            else:
                exit_px, reason = live_hl, "hl_break"
            exit_row = row
            break

        wk, wpx = live_working()
        if wk is not None and h >= wpx - 1e-12:
            ab = high_ab(con, day, bn, h)
            ev = {
                "k": wk, "fib": wpx, "bar": bn, "time_hm": hm_s(row.bar_start_chicago),
                "high": h, "low": l, "close": c, "open": o,
                "ask": ab["ask"], "bid": ab["bid"], "absorbed": ab["absorbed"],
            }
            fib_events.append(ev)
            if ab["absorbed"]:
                # take the fib; if open already through fib on a gap, fill open
                if o >= wpx - 1e-12:
                    exit_px = o
                else:
                    exit_px = wpx
                reason = "absorbed_%.3g" % wk
                exit_row = row
                break
            if c > wpx + 1e-12:
                skipped.add(wk)

        if bn == FLATTEN_BAR:
            exit_row, exit_px, reason = row, c, "session_end"
            break

    if entry_row is None:
        facts = {
            "day": day.isoformat(), "side": "LONG", "rule": "swing-HL",
            "entry": None, "reason": "no_entry",
            "first_sh": first_sh, "pull_sl": pull_sl,
            "or_high": or_high, "or_low": or_low, "long_127": long_127,
        }
        facts["_g"] = g
        facts["_shs"] = shs
        facts["_sls"] = sls
        return facts

    if exit_row is None:
        raise SystemExit("in trade with no exit")

    xt = as_naive(exit_row.bar_end_chicago)
    et = as_naive(entry_row.bar_start_chicago)
    net = (float(exit_px) - entry_px) - COST
    facts = {
        "day": day.isoformat(),
        "side": "LONG",
        "rule": "SH near 1.27, SL, break SH; last HL hold; absorb at gauge fibs",
        "or_high": or_high, "or_low": or_low, "or_val": or_val,
        "long_127": long_127,
        "fibs": [{"k": k, "px": px} for k, px in fibs],
        "first_sh": first_sh,
        "pull_sl": pull_sl,
        "entry_time": ts(et),
        "entry_time_hm": hm_s(et),
        "entry_price": entry_px,
        "entry_bar": int(entry_row.rth_bar_number),
        "entry_ohlc": {
            "o": float(entry_row.open), "h": float(entry_row.high),
            "l": float(entry_row.low), "c": float(entry_row.close),
        },
        "live_hl_at_exit": live_hl,
        "hl_steps": hl_steps,
        "fib_events": fib_events,
        "skipped": sorted(skipped),
        "exit_time": ts(xt),
        "exit_time_hm": hm_s(xt),
        "exit_price": float(exit_px),
        "exit_reason": reason,
        "exit_bar": int(exit_row.rth_bar_number),
        "exit_ohlc": {
            "o": float(exit_row.open), "h": float(exit_row.high),
            "l": float(exit_row.low), "c": float(exit_row.close),
        },
        "gross": round(float(exit_px) - entry_px, 4),
        "cost": COST,
        "net": round(float(net), 4),
        "vs_first_tag_127": 27913.25,
        "vs_close_rule_net": 315.25,
    }
    facts["_g"] = g
    facts["_shs"] = shs
    facts["_sls"] = sls
    return facts


def plot_one(facts, g_full, out_path: Path) -> None:
    g = load_day_series(DAY)
    et = as_naive(facts["entry_time"])
    xt = as_naive(facts["exit_time"])
    t0 = datetime.combine(DAY, dtime(8, 30))
    t_right = max(xt + timedelta(minutes=15), datetime.combine(DAY, dtime(10, 0)))
    if facts["exit_reason"] == "session_end":
        t_right = datetime.combine(DAY, dtime(15, 0))
    vis = g[(g.t >= t0 - timedelta(seconds=15)) & (g.t <= t_right)].copy()
    or_high, or_low = facts["or_high"], facts["or_low"]
    or_val = facts["or_val"]
    long_127 = facts["long_127"]

    fig, (ax, axv, axd) = plt.subplots(
        3, 1, sharex=True, figsize=(14.5, 11.0), dpi=120,
        gridspec_kw={"height_ratios": [3.5, 0.8, 1.0], "hspace": 0.06},
    )
    ax.fill_between(vis.t, vis.low, vis.high, color="0.82", alpha=0.5, lw=0, zorder=1)
    ax.plot(vis.t, vis.close, color="0.18", lw=0.9, zorder=3, label="15s close")
    x0 = mdates.date2num(t0)
    x1 = mdates.date2num(datetime.combine(DAY, dtime(8, 35)))
    ax.add_patch(Rectangle(
        (x0, or_low), x1 - x0, or_high - or_low,
        facecolor="#ffcc00", edgecolor="#aa8800", lw=1.1, alpha=0.45, zorder=2, label="5m OR",
    ))
    ax.axhline(or_high, color=C_OR_GOLD, lw=1.2)
    ax.axhline(or_low, color=C_OR_GOLD, lw=1.2, label="OR High/Low")
    ax.axhline(or_val, color=C_OR_EMPH, lw=1.2, ls="--", label="OR VAL")
    ax.axhline(long_127, color=C_127_L, lw=1.2, ls="--")
    ax.text(1.004, long_127, f"1.27   {long_127:.2f}", transform=ax.get_yaxis_transform(),
            color=C_127_L, fontsize=7.0, va="center", ha="left", clip_on=False, fontweight="bold")
    for rec in facts["fibs"]:
        k, px = rec["k"], rec["px"]
        col = FIB_COL.get(k, "#555")
        ax.axhline(px, color=col, lw=1.25, zorder=3.4)
        ax.text(1.004, px, f"{k:g}   {px:.2f}", transform=ax.get_yaxis_transform(),
                color=col, fontsize=7.0, va="center", ha="left", clip_on=False, fontweight="bold")

    sh = facts["first_sh"]
    sl = facts["pull_sl"]
    ax.scatter([pd.Timestamp(as_naive(sh["time"]))], [sh["high"]], marker="D", color=C_ASK, s=50, zorder=8)
    ax.annotate("1st SH  %.2f" % sh["high"], xy=(pd.Timestamp(as_naive(sh["time"])), sh["high"]),
                xytext=(6, 10), textcoords="offset points", fontsize=7.2, color=C_ASK, fontweight="bold")
    ax.scatter([pd.Timestamp(as_naive(sl["time"]))], [sl["low"]], marker="v", color=C_BID, s=50, zorder=8)
    ax.annotate("SL  %.2f" % sl["low"], xy=(pd.Timestamp(as_naive(sl["time"])), sl["low"]),
                xytext=(6, -16), textcoords="offset points", fontsize=7.2, color=C_BID, fontweight="bold")

    # HL steps
    if facts["hl_steps"]:
        xs = [pd.Timestamp(as_naive(s["t"])) for s in facts["hl_steps"]]
        ys = [s["hl"] for s in facts["hl_steps"]]
        xs.append(pd.Timestamp(xt))
        ys.append(facts["live_hl_at_exit"])
        ax.step(xs, ys, where="post", color=C_HL, lw=1.8, zorder=6, label="last HL")
        ax.text(1.004, facts["live_hl_at_exit"], "HL  %.2f" % facts["live_hl_at_exit"],
                transform=ax.get_yaxis_transform(), color=C_HL, fontsize=7.0, va="center", ha="left", clip_on=False)

    et_ts = pd.Timestamp(et)
    xt_ts = pd.Timestamp(xt)
    ax.scatter([et_ts], [facts["entry_price"]], marker="^", color="#2ca02c", s=90, zorder=9,
               edgecolors="white", linewidths=0.4, label="entry (SH break)")
    ax.axvline(et_ts, color="#2ca02c", lw=0.7, alpha=0.4)
    net = facts["net"]
    xcol = "#006400" if net >= 0 else "#e31a1c"
    ax.scatter([xt_ts], [facts["exit_price"]], marker="x", color=xcol, s=100, zorder=9, linewidths=1.6)
    ax.axvline(xt_ts, color=xcol, lw=0.8, alpha=0.55)
    ax.annotate(
        "exit %s\n%s\n%.2f  net %+.2f" % (facts["exit_time_hm"], facts["exit_reason"], facts["exit_price"], net),
        xy=(xt_ts, facts["exit_price"]), xytext=(8, -28), textcoords="offset points",
        fontsize=7.4, color=xcol, fontweight="bold",
    )

    for ev in facts["fib_events"]:
        col = C_BID if ev["absorbed"] else C_ASK
        t = datetime.strptime("2026-07-30 " + ev["time_hm"], "%Y-%m-%d %H:%M:%S")
        ax.scatter([t], [ev["high"]], marker="o", color=col, s=22, zorder=8, edgecolors="white", linewidths=0.3)
        if ev["ask"] is not None:
            ax.annotate("%d/%d" % (ev["ask"], ev["bid"]), xy=(t, ev["high"]),
                        xytext=(0, 7), textcoords="offset points", ha="center", fontsize=6.0, color=col)

    ax.set_title(
        "%s LONG  SH-break entry  HL hold  absorb-take    %s  net %+.2f"
        % (DAY.isoformat(), facts["exit_reason"], net)
    )
    ax.set_ylabel("NQ")
    ax.set_xlim(t0, t_right)
    ys = [or_low, or_val, long_127, facts["entry_price"], facts["exit_price"], float(vis.low.min()), float(vis.high.max())]
    for rec in facts["fibs"]:
        if rec["px"] <= float(vis.high.max()) + 40:
            ys.append(rec["px"])
    pad = (max(ys) - min(ys)) * 0.06
    ax.set_ylim(min(ys) - pad, max(ys) + pad)
    ax.grid(True, alpha=0.28)
    ax.legend(loc="upper left", fontsize=6.8, framealpha=0.92, ncol=2)

    xnum = mdates.date2num(pd.to_datetime(vis["t"]).to_numpy())
    vol = vis["total_volume"].to_numpy(dtype=float)
    vd = vis["volume_delta"].to_numpy(dtype=float)
    axv.bar(xnum, vol, width=BAR_W, align="edge", color=np.where(vd >= 0, "#5aa05a", "#c45c5c"), linewidth=0)
    axv.axvline(et_ts, color="#2ca02c", lw=0.7, alpha=0.35)
    axv.axvline(xt_ts, color=xcol, lw=0.8, alpha=0.5)
    axv.set_ylabel("Volume")
    axv.set_ylim(0, float(np.nanmax(vol)) * 1.15)
    axv.grid(True, alpha=0.28)

    plot_cum_delta_line(axd, vis)
    axd.axhline(0, color="0.35", lw=0.8)
    axd.axvline(et_ts, color="#2ca02c", lw=0.7, alpha=0.35)
    axd.axvline(xt_ts, color=xcol, lw=0.8, alpha=0.5)
    axd.set_ylabel("Cum delta")
    axd.grid(True, alpha=0.28)
    axd.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    axd.set_xticks(session_ticks(t0, t_right))
    fig.subplots_adjust(left=0.055, right=0.84, top=0.95, bottom=0.055, hspace=0.07)
    fig.savefig(out_path)
    plt.close(fig)


def main():
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    bars, fp, extra = load_days(DB_PATH, (DAY,))
    print("LOADED", len(bars), extra)
    g = bars[bars.trading_day == DAY].copy()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    facts = simulate(g, fp, con)
    con.close()
    if facts.get("entry") is None and facts.get("entry_price") is None:
        print("NO ENTRY", facts.get("first_sh"), facts.get("pull_sl"))
        return 1
    plot_one(facts, facts["_g"], OUT_PNG)
    public = {k: v for k, v in facts.items() if not k.startswith("_")}
    public["chart"] = str(OUT_PNG)
    public["chart_bytes"] = int(OUT_PNG.stat().st_size)
    OUT_JSON.write_text(json.dumps(_jsonable(public), indent=2) + "\n")
    print("ENTRY", public["entry_time_hm"], public["entry_price"], "bar", public["entry_bar"])
    print("SH", public["first_sh"])
    print("SL", public["pull_sl"])
    print("skipped", public["skipped"])
    print("fib_events", len(public["fib_events"]))
    for ev in public["fib_events"][:12]:
        print(" ", ev["time_hm"], "k", ev["k"], "H", ev["high"], "ask/bid", ev["ask"], ev["bid"], "abs", ev["absorbed"], "C", ev["close"])
    if len(public["fib_events"]) > 12:
        print("  ...", len(public["fib_events"]) - 12, "more")
    print("HL steps", public["hl_steps"])
    print("EXIT", public["exit_time_hm"], public["exit_price"], public["exit_reason"], "net", public["net"])
    print("CHART", OUT_PNG, public["chart_bytes"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
