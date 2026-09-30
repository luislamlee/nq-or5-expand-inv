#!/usr/bin/env python3
"""OR5_1m_EXPAND_BREAK — SHORT on 1m close < expanding range Low.

Entry (NEW vs fixed-OR BREAK_BELOW):
  1. OR5 = High/Low of 09:30–09:35 ET (first 5 one-minute bars).
  2. Walk RTH 1m bars (m1 >= 6) in order with current range (cur_H, cur_L)
     initialized to OR5 High/Low:
     - If close OUTSIDE current range:
         * close < cur_L → SHORT signal; fill at NEXT 1m OPEN (≤1 trade/day).
         * close > cur_H → breakout event: NO short that bar, NO expand.
           Range stays; continue scanning later bars.
     - If close INSIDE (cur_L ≤ close ≤ cur_H): EXPAND range:
         cur_H = max(cur_H, bar.high); cur_L = min(cur_L, bar.low).
  3. Continue until short entry or EOD. Earliest confirm = first 1m after OR5
     (09:35–09:36) if it closes < OR_L.
  4. Last-bar confirm with no next open → SKIP (no trade).

Fib targets + init stop = ORIGINAL OR5 High/Low/R (expanding range is ONLY
for the entry trigger). Stop ratchet still 5m High(n−1) with 1m close confirm.

Universes: F3_0 and ALL days with valid OR5.
TM: RATCHET_HI and HOLD_VWAP × targets 2.618 / 3.33 / 4.23
Cost 0.50 · IS≤2026-06-10

Baseline compare: fixed-OR BREAK_BELOW F3 delayed T423 + F3@0935 T423.
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

for p in (
    "/workspace/.venv_volchart/lib/python3.13/site-packages",
    "/workspace/.venv_plot/lib/python3.13/site-packages",
    "/workspace/sierra",
    "/workspace/sierra/candle_force",
):
    if Path(p).exists() and p not in sys.path:
        sys.path.insert(0, p)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import duckdb

OUT_DIR = Path("/workspace/sierra/candle_force")
PARQUET = OUT_DIR / "CANDLE_FORCE_v3_multiday_bars.parquet"
DB = Path("/workspace/sierra/ticks/in/NQ_front_volroll.duckdb")
BASE_BREAK = OUT_DIR / "OR5_1m_BREAK_BELOW_compare.csv"
BASE_CONFIRM = OUT_DIR / "OR5_F3_1m_CONFIRM_5mHI_compare.csv"
BASE_CONFIRM_TRADES = OUT_DIR / "OR5_F3_1m_CONFIRM_5mHI_trades.csv"

OUT_SPEC = OUT_DIR / "OR5_1m_EXPAND_BREAK.md"
OUT_TRADES = OUT_DIR / "OR5_1m_EXPAND_BREAK_trades.csv"
OUT_SUMMARY = OUT_DIR / "OR5_1m_EXPAND_BREAK_summary.csv"
OUT_TABLE = OUT_DIR / "OR5_1m_EXPAND_BREAK_compare.csv"
OUT_EQUITY = OUT_DIR / "OR5_1m_EXPAND_BREAK_equity.png"
OUT_ES = OUT_DIR / "OR5_1m_EXPAND_BREAK_RESUMEN_ES.md"

COST = 0.50
STOP_BUF = 2.0
EPS = 1e-9
IS_LAST = "2026-06-10"
SHORT_CUT = -5.0
FIB_LEVELS = (2.618, 3.33, 4.23)
POST_OR_M1 = 6  # first 1m after OR5 (09:35 ET open)


def _load_mod(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


laxo = _load_mod("or5_hold_laxo", OUT_DIR / "run_or5_short_hold_laxo_vs_ratchet.py")
slf = _load_mod("or5_slf", OUT_DIR / "run_or5_short_longfix.py")

metrics = slf.metrics
apply_date_split = slf.apply_date_split
as_day_str = slf.as_day_str
f3_0_candidate = laxo.f3_0_candidate
feat = laxo.feat
fib_px = laxo.fib_px
chk_vwap = laxo.chk_vwap


def fib_tag(k: float) -> str:
    return {2.618: "2618", 3.33: "333", 4.23: "423"}[k]


def m5_of(m1: int) -> int:
    return (int(m1) - 1) // 5 + 1


def load_rth_1m_with_dvwap() -> pd.DataFrame:
    con = duckdb.connect(str(DB), read_only=True)
    con.execute("SET TimeZone = 'UTC'")
    df = con.execute(
        """
        WITH rth AS (
          SELECT
            session_date,
            ts_open,
            ts_open + INTERVAL 1 MINUTE AS bar_end,
            open, high, low, close, delta, cvd, total_volume,
            row_number() OVER (PARTITION BY session_date ORDER BY ts_open) AS m1_bar
          FROM bars_t_1m
          WHERE cast(timezone('America/New_York', ts_open::TIMESTAMPTZ) AS TIME) >= TIME '09:30:00'
            AND cast(timezone('America/New_York', ts_open::TIMESTAMPTZ) AS TIME) < TIME '16:00:00'
            AND session_date BETWEEN DATE '2025-12-16' AND DATE '2026-08-25'
        )
        SELECT
          r.session_date::VARCHAR AS trading_day,
          r.m1_bar::INTEGER AS m1_bar,
          strftime(timezone('America/New_York', r.ts_open::TIMESTAMPTZ), '%H:%M') AS time_et,
          r.open::DOUBLE AS open,
          r.high::DOUBLE AS high,
          r.low::DOUBLE AS low,
          r.close::DOUBLE AS close,
          r.delta::DOUBLE AS delta,
          r.cvd::DOUBLE AS cvd,
          p.vwap::DOUBLE AS dvwap,
          p.val::DOUBLE AS dval,
          p.vah::DOUBLE AS dvah
        FROM rth r
        LEFT JOIN profile_developing p
          ON p.session_date = r.session_date AND p.ts = r.bar_end
        ORDER BY r.session_date, r.m1_bar
        """
    ).fetchdf()
    con.close()
    return df


def valid_or5(or5) -> bool:
    or_h = float(or5["high"])
    or_l = float(or5["low"])
    return (or_h - or_l) > EPS


def collect_days(bars5: pd.DataFrame, bars1: pd.DataFrame, *, universe: str):
    """Return list of (contract, day, or5, post1m) and signal-day count.

    universe: 'F3' | 'ALL'
    post1m starts at m1_bar >= POST_OR_M1.
    """
    out = []
    n_signal = 0
    or5_all = bars5[bars5["m5_bar"] == 1].copy()
    or5_all["trading_day"] = pd.to_datetime(or5_all["trading_day"]).dt.strftime("%Y-%m-%d")

    for _, or5 in or5_all.iterrows():
        if not valid_or5(or5):
            continue
        if universe == "F3" and not f3_0_candidate(or5):
            continue
        day_s = as_day_str(or5["trading_day"])
        d1 = bars1[bars1["trading_day"] == day_s].sort_values("m1_bar").reset_index(drop=True)
        if d1.empty:
            continue
        if int(d1.iloc[0]["m1_bar"]) != 1:
            continue
        post = d1[d1["m1_bar"] >= POST_OR_M1].reset_index(drop=True)
        if post.empty:
            continue
        n_signal += 1
        out.append((str(or5["contract"]), day_s, or5, post))
    return out, n_signal


def simulate_short_expand_break(
    or5,
    g1: pd.DataFrame,
    *,
    max_fib: float,
    hold_vwap: bool,
    cost: float = COST,
    variant: str,
    universe: str,
) -> dict | None:
    """Expanding-range short: inside close → expand; close < cur_L → next-open entry.

    Fib targets and init stop use ORIGINAL OR5 H/L/R. Expanding range is entry-only.
    """
    g = g1.sort_values("m1_bar").reset_index(drop=True)
    if len(g) < 2:
        return None

    # ORIGINAL OR5 — fibs + init stop (never mutate these for TM)
    or_h = float(or5["high"])
    or_l = float(or5["low"])
    r = or_h - or_l
    if r <= 0:
        return None
    or_mid = 0.5 * (or_h + or_l)
    target = fib_px(or_h, r, max_fib)
    fibs = {k: fib_px(or_h, r, k) for k in FIB_LEVELS}
    stop0 = or_h + STOP_BUF
    cur_stop = stop0
    score0 = feat(or5, "score_v2")

    # Expanding range for ENTRY trigger only
    cur_h = or_h
    cur_l = or_l
    n_expansions = 0
    n_upside_outsides = 0  # close > cur_H events (no expand, no short)

    completed_5m_hi: dict[int, float] = {}
    buck_m5: int | None = None
    buck_hi = float("-inf")

    pending_entry = False
    confirm_m1 = None
    confirm_time = None
    confirm_close = None
    confirm_cur_h = None
    confirm_cur_l = None
    confirm_n_exp = None
    in_trade = False

    entry_px = entry_m1 = entry_time = None
    exit_m1 = exit_px = exit_reason = None
    hold_fail = None
    n_hold_bars = 0
    hit_fib = {k: False for k in FIB_LEVELS}
    mfe = 0.0
    mae = 0.0

    def update_bucket_and_ratchet(n1: int, m5: int, hi: float):
        nonlocal buck_m5, buck_hi, cur_stop
        if buck_m5 is None or m5 != buck_m5:
            buck_m5 = m5
            buck_hi = hi
        else:
            buck_hi = max(buck_hi, hi)
        if n1 % 5 == 0:
            completed_5m_hi[m5] = buck_hi
            if m5 >= 3:
                prev_hi = completed_5m_hi.get(m5 - 1)
                if prev_hi is not None and prev_hi < cur_stop - EPS:
                    cur_stop = prev_hi

    for i in range(len(g)):
        row = g.iloc[i]
        n1 = int(row["m1_bar"])
        m5 = m5_of(n1)
        o = float(row["open"])
        hi = float(row["high"])
        lo = float(row["low"])
        c = float(row["close"])
        dvwap = feat(row, "dvwap")
        dval = feat(row, "dval")
        t_et = str(row["time_et"])

        if not in_trade:
            if pending_entry:
                entry_px = o
                entry_m1 = n1
                entry_time = t_et
                in_trade = True
                pending_entry = False
                # fall through to manage this bar as first held bar
            else:
                # Looking for confirm vs CURRENT expanding range
                update_bucket_and_ratchet(n1, m5, hi)

                outside_below = c < cur_l - EPS
                outside_above = c > cur_h + EPS

                if outside_below:
                    # SHORT signal — no expand on this bar
                    if i == len(g) - 1:
                        return None  # last bar, no next open → SKIP
                    pending_entry = True
                    confirm_m1 = n1
                    confirm_time = t_et
                    confirm_close = c
                    confirm_cur_h = cur_h
                    confirm_cur_l = cur_l
                    confirm_n_exp = n_expansions
                elif outside_above:
                    # Upside outside: no short, no expand; keep scanning
                    n_upside_outsides += 1
                else:
                    # INSIDE close → expand range to include this bar's H/L
                    new_h = max(cur_h, hi)
                    new_l = min(cur_l, lo)
                    if new_h > cur_h + EPS or new_l < cur_l - EPS:
                        n_expansions += 1
                    cur_h = new_h
                    cur_l = new_l
                continue

        # ---- in trade ----
        mfe = max(mfe, entry_px - lo)
        mae = max(mae, hi - entry_px)
        for k, px in fibs.items():
            if lo <= px + EPS:
                hit_fib[k] = True

        # 1) target on 1m low touch
        if lo <= target + EPS:
            exit_m1 = n1
            exit_px = float(target)
            exit_reason = f"target_{max_fib:g}"
            break

        # 2) STOP CONFIRM at 1m CLOSE
        if c >= cur_stop - EPS:
            exit_m1 = n1
            exit_px = c
            exit_reason = "ratchet_stop" if cur_stop < stop0 - EPS else "init_stop"
            break

        # 3) HOLD_VWAP early exit at 1m CLOSE
        if hold_vwap:
            ok, why = chk_vwap(c, dvwap, dval, float("nan"), float("nan"))
            if ok:
                n_hold_bars += 1
            else:
                exit_m1 = n1
                exit_px = c
                exit_reason = f"hold_exit_{why}"
                hold_fail = why
                break

        # 4) On 5m bar close: store High, ratchet
        update_bucket_and_ratchet(n1, m5, hi)

        # 5) EOD
        if i == len(g) - 1:
            exit_m1 = n1
            exit_px = c
            exit_reason = "eod"
            break

    if exit_m1 is None or entry_px is None:
        return None

    gross = float(entry_px) - float(exit_px)
    net = gross - cost
    exit_time = str(g.loc[g["m1_bar"] == exit_m1, "time_et"].iloc[0])
    bars_in_trade = int(exit_m1 - entry_m1 + 1)
    entry_delay_min = int(entry_m1 - POST_OR_M1)
    confirm_delay_min = int(confirm_m1 - POST_OR_M1) if confirm_m1 is not None else None

    # Range at confirm (before entry); width vs original R
    rng_h = float(confirm_cur_h) if confirm_cur_h is not None else cur_h
    rng_l = float(confirm_cur_l) if confirm_cur_l is not None else cur_l
    final_width = rng_h - rng_l
    width_vs_R = final_width / r if r > EPS else float("nan")

    return {
        "contract": str(or5["contract"]),
        "day": as_day_str(or5["trading_day"]),
        "side": "SHORT",
        "tf": "1m_expand_break",
        "universe": universe,
        "score_v2": round(score0, 4),
        "vbp_imbalance": round(feat(or5, "vbp_imbalance"), 6),
        "or_h": round(or_h, 4),
        "or_l": round(or_l, 4),
        "or_mid": round(or_mid, 4),
        "R": round(r, 4),
        "max_fib": max_fib,
        "target": round(target, 4),
        "stop_init": round(stop0, 4),
        "stop_final": round(cur_stop, 4),
        "confirm_m1": int(confirm_m1) if confirm_m1 is not None else None,
        "confirm_time": confirm_time,
        "confirm_close": round(float(confirm_close), 4) if confirm_close is not None else None,
        "confirm_delay_min": confirm_delay_min,
        "confirm_cur_h": round(rng_h, 4),
        "confirm_cur_l": round(rng_l, 4),
        "final_range_width": round(final_width, 4),
        "width_vs_R": round(width_vs_R, 4),
        "n_expansions": int(confirm_n_exp) if confirm_n_exp is not None else int(n_expansions),
        "n_upside_outsides": int(n_upside_outsides),
        "entry_m1": int(entry_m1),
        "entry_m5": m5_of(int(entry_m1)),
        "entry_time": entry_time,
        "entry_price": round(float(entry_px), 4),
        "entry_delay_min": entry_delay_min,
        "exit_m1": int(exit_m1),
        "exit_m5": m5_of(int(exit_m1)),
        "exit_time": exit_time,
        "exit_price": round(float(exit_px), 4),
        "exit_reason": exit_reason,
        "hold_fail": hold_fail,
        "hold_mode": "vwap" if hold_vwap else "none",
        "ratchet_mode": "5m_hi_confirm_1m_close",
        "n_hold_bars": n_hold_bars,
        "gross": round(gross, 4),
        "net": round(net, 4),
        "cost": cost,
        "bars_in_trade": bars_in_trade,
        "mfe": round(mfe, 4),
        "mae": round(mae, 4),
        "hit_2618": int(hit_fib[2.618]),
        "hit_333": int(hit_fib[3.33]),
        "hit_423": int(hit_fib[4.23]),
        "variant": variant,
    }


def run_variant(days, **kw) -> pd.DataFrame:
    rows = []
    for _c, _d, or5, g1 in days:
        t = simulate_short_expand_break(or5, g1, **kw)
        if t is not None:
            rows.append(t)
    return pd.DataFrame(rows)


def enrich_metrics(m: dict, part: pd.DataFrame) -> dict:
    extra_nan_keys = (
        "pct_win",
        "pct_loss",
        "avg_net_per_day",
        "largest_win",
        "largest_loss",
        "mean_entry_delay_min",
        "median_entry_delay_min",
        "mean_n_expansions",
        "median_n_expansions",
        "mean_width_vs_R",
        "median_width_vs_R",
        "mean_final_range_width",
        "median_final_range_width",
    )
    if part is None or len(part) == 0:
        for k in extra_nan_keys:
            m[k] = float("nan")
        m["largest_win_day"] = ""
        m["largest_loss_day"] = ""
        m["n_days"] = 0
        return m
    nets = part["net"].astype(float)
    wins = nets[nets > 0]
    losses = nets[nets < 0]
    n = len(part)
    m["pct_win"] = float(len(wins) / n)
    m["pct_loss"] = float(len(losses) / n)
    if len(wins):
        iw = wins.idxmax()
        m["largest_win"] = float(wins.loc[iw])
        m["largest_win_day"] = str(part.loc[iw, "day"])
    else:
        m["largest_win"] = float("nan")
        m["largest_win_day"] = ""
    if len(losses):
        il = losses.idxmin()
        m["largest_loss"] = float(losses.loc[il])
        m["largest_loss_day"] = str(part.loc[il, "day"])
    else:
        m["largest_loss"] = float("nan")
        m["largest_loss_day"] = ""
    n_days = int(part["day"].nunique())
    m["n_days"] = n_days
    m["avg_net_per_day"] = float(nets.sum() / n_days) if n_days else float("nan")
    if "entry_delay_min" in part.columns:
        dly = part["entry_delay_min"].astype(float)
        m["mean_entry_delay_min"] = float(dly.mean())
        m["median_entry_delay_min"] = float(dly.median())
    else:
        m["mean_entry_delay_min"] = float("nan")
        m["median_entry_delay_min"] = float("nan")
    if "n_expansions" in part.columns:
        ne = part["n_expansions"].astype(float)
        m["mean_n_expansions"] = float(ne.mean())
        m["median_n_expansions"] = float(ne.median())
    else:
        m["mean_n_expansions"] = float("nan")
        m["median_n_expansions"] = float("nan")
    if "width_vs_R" in part.columns:
        w = part["width_vs_R"].astype(float)
        m["mean_width_vs_R"] = float(w.mean())
        m["median_width_vs_R"] = float(w.median())
    else:
        m["mean_width_vs_R"] = float("nan")
        m["median_width_vs_R"] = float("nan")
    if "final_range_width" in part.columns:
        fw = part["final_range_width"].astype(float)
        m["mean_final_range_width"] = float(fw.mean())
        m["median_final_range_width"] = float(fw.median())
    else:
        m["mean_final_range_width"] = float("nan")
        m["median_final_range_width"] = float("nan")
    return m


def split_rows(variant: str, tdf: pd.DataFrame, *, universe: str, n_signal: int) -> list[dict]:
    out = []
    is_df, oos_df = apply_date_split(tdf)
    for label, part in (("IS", is_df), ("OOS", oos_df), ("ALL", tdf)):
        m = metrics(part, label)
        m["variant"] = variant
        m["universe"] = universe
        m["split"] = label
        m["tf"] = "1m_expand_break"
        m["n_signal_days"] = int(n_signal) if label == "ALL" else float("nan")
        if len(part):
            er = part["exit_reason"].astype(str)
            m["pct_hold_exit"] = float(er.str.startswith("hold_exit").mean())
            m["pct_target"] = float(er.str.startswith("target_").mean())
            m["pct_ratchet_stop"] = float((er == "ratchet_stop").mean())
            m["pct_init_stop"] = float((er == "init_stop").mean())
            m["pct_eod"] = float((er == "eod").mean())
            m["pct_hit_2618"] = float(part["hit_2618"].mean())
            m["pct_hit_333"] = float(part["hit_333"].mean())
            m["pct_hit_423"] = float(part["hit_423"].mean())
            m["avg_bars"] = float(part["bars_in_trade"].mean())
            m["avg_hold_bars"] = float(part["n_hold_bars"].mean())
            m["n_hold_exit"] = int(er.str.startswith("hold_exit").sum())
            if label == "ALL" and n_signal > 0:
                m["pct_signal_no_entry"] = float(1.0 - (len(part) / n_signal))
                m["n_signal_no_entry"] = int(n_signal - len(part))
            else:
                m["pct_signal_no_entry"] = float("nan")
                m["n_signal_no_entry"] = 0
        else:
            for k in (
                "pct_hold_exit",
                "pct_target",
                "pct_ratchet_stop",
                "pct_init_stop",
                "pct_eod",
                "pct_hit_2618",
                "pct_hit_333",
                "pct_hit_423",
                "avg_bars",
                "avg_hold_bars",
            ):
                m[k] = float("nan")
            m["n_hold_exit"] = 0
            if label == "ALL" and n_signal > 0:
                m["pct_signal_no_entry"] = 1.0
                m["n_signal_no_entry"] = int(n_signal)
            else:
                m["pct_signal_no_entry"] = float("nan")
                m["n_signal_no_entry"] = 0
        m = enrich_metrics(m, part)
        out.append(m)
    return out


def build_variants(universe: str) -> list[tuple[str, dict]]:
    specs: list[tuple[str, dict]] = []
    for fib in FIB_LEVELS:
        specs.append(
            (
                f"{universe}_EXPAND_HOLD_VWAP_T{fib_tag(fib)}",
                dict(max_fib=fib, hold_vwap=True, universe=universe),
            )
        )
    for fib in FIB_LEVELS:
        specs.append(
            (
                f"{universe}_EXPAND_RATCHET_HI_T{fib_tag(fib)}",
                dict(max_fib=fib, hold_vwap=False, universe=universe),
            )
        )
    return specs


def fmt_pf(x) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "n/a"
    return f"{x:.2f}"


def fmt_net(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.1f}"


def fmt_pct(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{100 * x:.1f}%"


def fmt_min(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.1f}"


def fmt_num(x, nd=2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.{nd}f}"


def plot_equity(trade_map: dict[str, pd.DataFrame], variants: list[str], out_path: Path):
    fig, ax = plt.subplots(figsize=(12, 6))
    for v in variants:
        tdf = trade_map.get(v)
        if tdf is None or tdf.empty:
            continue
        t = tdf.sort_values(["day", "entry_m1"]).reset_index(drop=True)
        eq = t["net"].cumsum()
        is_t423 = "T423" in v
        is_f3 = v.startswith("F3_")
        is_hold = "HOLD_VWAP" in v
        if is_f3 and is_hold and is_t423:
            color, ls, lw = "#d62728", "-", 2.0
        elif is_f3 and (not is_hold) and is_t423:
            color, ls, lw = "#ff7f0e", "--", 2.0
        elif (not is_f3) and is_hold and is_t423:
            color, ls, lw = "#1f77b4", "-", 1.8
        elif (not is_f3) and (not is_hold) and is_t423:
            color, ls, lw = "#2ca02c", "--", 1.8
        else:
            color, ls, lw = "#bbbbbb", ":", 0.9
        ax.plot(eq.index + 1, eq.values, label=v if is_t423 else None, lw=lw, color=color, ls=ls)
    ax.axhline(0, color="#888", lw=0.8)
    ax.set_title("OR5 1m EXPAND BREAK — F3 vs ALL · HOLD_VWAP / RATCHET_HI (T423 bold)")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Net pts acumulados")
    ax.legend(fontsize=8, loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def load_baseline_fixed_break() -> list[dict]:
    """Load fixed-OR BREAK_BELOW F3 delayed T423 rows from compare csv."""
    rows = []
    if not BASE_BREAK.exists():
        return rows
    df = pd.read_csv(BASE_BREAK)
    for v in ("F3_BREAK_HOLD_VWAP_T423", "F3_BREAK_RATCHET_HI_T423"):
        for sp in ("IS", "OOS", "ALL"):
            sub = df[(df["variante"] == v) & (df["split"] == sp)]
            if sub.empty:
                continue
            r = sub.iloc[0]
            rows.append(
                {
                    "variante": f"FIXED_{v}",
                    "split": sp,
                    "tf": "1m_break_below",
                    "universe": "F3",
                    "entry_mode": "1m_close_below_OR_L_fixed",
                    "n": int(r["n"]),
                    "WR": float(r["WR"]) if pd.notna(r["WR"]) else None,
                    "pct_win": float(r["pct_win"]) if "pct_win" in r and pd.notna(r["pct_win"]) else None,
                    "pct_loss": float(r["pct_loss"]) if "pct_loss" in r and pd.notna(r["pct_loss"]) else None,
                    "net": float(r["net"]) if pd.notna(r["net"]) else None,
                    "PF": float(r["PF"]) if pd.notna(r["PF"]) else None,
                    "maxDD": float(r["maxDD"]) if pd.notna(r["maxDD"]) else None,
                    "avg_net_per_day": float(r["avg_net_per_day"])
                    if "avg_net_per_day" in r and pd.notna(r["avg_net_per_day"])
                    else None,
                    "largest_win": float(r["largest_win"])
                    if "largest_win" in r and pd.notna(r["largest_win"])
                    else None,
                    "largest_loss": float(r["largest_loss"])
                    if "largest_loss" in r and pd.notna(r["largest_loss"])
                    else None,
                    "largest_win_day": str(r["largest_win_day"])
                    if "largest_win_day" in r and pd.notna(r["largest_win_day"])
                    else "",
                    "largest_loss_day": str(r["largest_loss_day"])
                    if "largest_loss_day" in r and pd.notna(r["largest_loss_day"])
                    else "",
                    "pct_hit_2618": float(r["pct_hit_2618"]) if pd.notna(r["pct_hit_2618"]) else None,
                    "pct_hit_333": float(r["pct_hit_333"]) if pd.notna(r["pct_hit_333"]) else None,
                    "pct_hit_423": float(r["pct_hit_423"]) if pd.notna(r["pct_hit_423"]) else None,
                    "pct_hold_exit": float(r["pct_hold_exit"]) if pd.notna(r["pct_hold_exit"]) else None,
                    "pct_ratchet_stop": float(r["pct_ratchet_stop"])
                    if pd.notna(r["pct_ratchet_stop"])
                    else None,
                    "pct_target": float(r["pct_target"]) if pd.notna(r["pct_target"]) else None,
                    "pct_init_stop": float(r["pct_init_stop"]) if pd.notna(r["pct_init_stop"]) else None,
                    "avg_bars": float(r["avg_bars"]) if pd.notna(r["avg_bars"]) else None,
                    "mean_entry_delay_min": float(r["mean_entry_delay_min"])
                    if "mean_entry_delay_min" in r and pd.notna(r["mean_entry_delay_min"])
                    else None,
                    "median_entry_delay_min": float(r["median_entry_delay_min"])
                    if "median_entry_delay_min" in r and pd.notna(r["median_entry_delay_min"])
                    else None,
                    "pct_signal_no_entry": float(r["pct_signal_no_entry"])
                    if "pct_signal_no_entry" in r and pd.notna(r["pct_signal_no_entry"])
                    else None,
                    "mean_n_expansions": None,
                    "median_n_expansions": None,
                    "mean_width_vs_R": None,
                    "median_width_vs_R": None,
                }
            )
    return rows


def load_baseline_0935() -> list[dict]:
    """Load F3@09:35 confirm baseline T423 rows."""
    rows = []
    extras = {}
    if BASE_CONFIRM.exists():
        df = pd.read_csv(BASE_CONFIRM)
        df = df[df["tf"].astype(str) == "1m_confirm_5mHI"].copy()
        for v in ("HOLD_VWAP_T423", "RATCHET_HI_T423"):
            for sp in ("IS", "OOS", "ALL"):
                sub = df[(df["variante"] == v) & (df["split"] == sp)]
                if sub.empty:
                    continue
                r = sub.iloc[0]
                rows.append(
                    {
                        "variante": f"F3_0935_{v}",
                        "split": sp,
                        "tf": "1m_confirm_5mHI",
                        "universe": "F3",
                        "entry_mode": "0935",
                        "n": int(r["n"]),
                        "WR": float(r["WR"]) if pd.notna(r["WR"]) else None,
                        "pct_win": float(r["pct_win"]) if "pct_win" in r and pd.notna(r["pct_win"]) else None,
                        "pct_loss": float(r["pct_loss"]) if "pct_loss" in r and pd.notna(r["pct_loss"]) else None,
                        "net": float(r["net"]) if pd.notna(r["net"]) else None,
                        "PF": float(r["PF"]) if pd.notna(r["PF"]) else None,
                        "maxDD": float(r["maxDD"]) if pd.notna(r["maxDD"]) else None,
                        "avg_net_per_day": float(r["avg_net_per_day"])
                        if "avg_net_per_day" in r and pd.notna(r["avg_net_per_day"])
                        else None,
                        "largest_win": float(r["largest_win"])
                        if "largest_win" in r and pd.notna(r["largest_win"])
                        else None,
                        "largest_loss": float(r["largest_loss"])
                        if "largest_loss" in r and pd.notna(r["largest_loss"])
                        else None,
                        "largest_win_day": "",
                        "largest_loss_day": "",
                        "pct_hit_2618": float(r["pct_hit_2618"]) if pd.notna(r["pct_hit_2618"]) else None,
                        "pct_hit_333": float(r["pct_hit_333"]) if pd.notna(r["pct_hit_333"]) else None,
                        "pct_hit_423": float(r["pct_hit_423"]) if pd.notna(r["pct_hit_423"]) else None,
                        "pct_hold_exit": float(r["pct_hold_exit"]) if pd.notna(r["pct_hold_exit"]) else None,
                        "pct_ratchet_stop": float(r["pct_ratchet_stop"])
                        if pd.notna(r["pct_ratchet_stop"])
                        else None,
                        "pct_target": float(r["pct_target"]) if pd.notna(r["pct_target"]) else None,
                        "pct_init_stop": float(r["pct_init_stop"]) if pd.notna(r["pct_init_stop"]) else None,
                        "avg_bars": float(r["avg_bars"]) if pd.notna(r["avg_bars"]) else None,
                        "mean_entry_delay_min": 0.0,
                        "median_entry_delay_min": 0.0,
                        "pct_signal_no_entry": 0.0,
                        "mean_n_expansions": None,
                        "median_n_expansions": None,
                        "mean_width_vs_R": None,
                        "median_width_vs_R": None,
                    }
                )
    if BASE_CONFIRM_TRADES.exists():
        t = pd.read_csv(BASE_CONFIRM_TRADES)
        for v in ("HOLD_VWAP_T423", "RATCHET_HI_T423"):
            p = t[t["variant"] == v]
            if p.empty:
                continue
            nets = p["net"].astype(float)
            iw = nets.idxmax()
            il = nets.idxmin()
            extras[f"F3_0935_{v}"] = {
                "largest_win": float(nets.loc[iw]),
                "largest_win_day": str(p.loc[iw, "day"]),
                "largest_loss": float(nets.loc[il]),
                "largest_loss_day": str(p.loc[il, "day"]),
            }
    for row in rows:
        if row["split"] == "ALL" and row["variante"] in extras:
            ex = extras[row["variante"]]
            row["largest_win_day"] = ex["largest_win_day"]
            row["largest_loss_day"] = ex["largest_loss_day"]
            if row.get("largest_win") is None:
                row["largest_win"] = ex["largest_win"]
            if row.get("largest_loss") is None:
                row["largest_loss"] = ex["largest_loss"]
    return rows


def _safe_round(v, nd=4):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return round(float(v), nd)


def cmp_row_from_summary(r: dict) -> dict:
    return {
        "variante": r["variant"],
        "split": r["split"],
        "tf": "1m_expand_break",
        "universe": r["universe"],
        "entry_mode": "1m_close_below_expanding_L",
        "n": int(r["n_trades"]),
        "WR": _safe_round(r["win_rate"], 4),
        "pct_win": _safe_round(r.get("pct_win"), 4),
        "pct_loss": _safe_round(r.get("pct_loss"), 4),
        "net": round(r["net_pts"], 2),
        "PF": None
        if r["pf"] is None or (isinstance(r["pf"], float) and math.isnan(r["pf"]))
        else round(float(r["pf"]), 3),
        "maxDD": round(r["max_dd"], 2),
        "avg_net_per_day": _safe_round(r.get("avg_net_per_day"), 2),
        "largest_win": _safe_round(r.get("largest_win"), 2),
        "largest_loss": _safe_round(r.get("largest_loss"), 2),
        "largest_win_day": r.get("largest_win_day", ""),
        "largest_loss_day": r.get("largest_loss_day", ""),
        "pct_hit_2618": _safe_round(r.get("pct_hit_2618"), 4),
        "pct_hit_333": _safe_round(r.get("pct_hit_333"), 4),
        "pct_hit_423": _safe_round(r.get("pct_hit_423"), 4),
        "pct_hold_exit": _safe_round(r.get("pct_hold_exit"), 4),
        "pct_ratchet_stop": _safe_round(r.get("pct_ratchet_stop"), 4),
        "pct_target": _safe_round(r.get("pct_target"), 4),
        "pct_init_stop": _safe_round(r.get("pct_init_stop"), 4),
        "avg_bars": _safe_round(r.get("avg_bars"), 2),
        "mean_entry_delay_min": _safe_round(r.get("mean_entry_delay_min"), 2),
        "median_entry_delay_min": _safe_round(r.get("median_entry_delay_min"), 2),
        "mean_n_expansions": _safe_round(r.get("mean_n_expansions"), 2),
        "median_n_expansions": _safe_round(r.get("median_n_expansions"), 2),
        "mean_width_vs_R": _safe_round(r.get("mean_width_vs_R"), 3),
        "median_width_vs_R": _safe_round(r.get("median_width_vs_R"), 3),
        "mean_final_range_width": _safe_round(r.get("mean_final_range_width"), 2),
        "median_final_range_width": _safe_round(r.get("median_final_range_width"), 2),
        "pct_signal_no_entry": _safe_round(r.get("pct_signal_no_entry"), 4),
        "n_signal_days": r.get("n_signal_days"),
        "n_signal_no_entry": r.get("n_signal_no_entry"),
    }


def main():
    print("Loading parquet (OR5) + 1m bars + dVWAP…")
    bars5 = pd.read_parquet(PARQUET)
    bars5["trading_day"] = pd.to_datetime(bars5["trading_day"]).dt.strftime("%Y-%m-%d")
    bars1 = load_rth_1m_with_dvwap()
    print(
        f"  parquet_rows={len(bars5)} days5={bars5['trading_day'].nunique()} "
        f"1m_rows={len(bars1)} days1={bars1['trading_day'].nunique()}"
    )

    days_by_u = {}
    n_signal_by_u = {}
    for uni in ("F3", "ALL"):
        days, n_sig = collect_days(bars5, bars1, universe=uni)
        days_by_u[uni] = days
        n_signal_by_u[uni] = n_sig
        print(f"  universe={uni} signal_days={n_sig}")

    trade_map: dict[str, pd.DataFrame] = {}
    summary_rows: list[dict] = []
    all_trades = []
    order: list[str] = []

    for uni in ("F3", "ALL"):
        for name, kw in build_variants(uni):
            print(f"Running {name}…")
            tdf = run_variant(days_by_u[uni], variant=name, **kw)
            trade_map[name] = tdf
            all_trades.append(tdf)
            order.append(name)
            summary_rows.extend(
                split_rows(name, tdf, universe=uni, n_signal=n_signal_by_u[uni])
            )
            m_all = next(r for r in summary_rows if r["variant"] == name and r["split"] == "ALL")
            print(
                f"  ALL n={m_all['n_trades']}/{n_signal_by_u[uni]} "
                f"no_entry={fmt_pct(m_all['pct_signal_no_entry'])} "
                f"WR={fmt_pct(m_all['win_rate'])} net={m_all['net_pts']:.1f} "
                f"PF={fmt_pf(m_all['pf'])} avg/day={fmt_net(m_all['avg_net_per_day'])} "
                f"Lwin={fmt_net(m_all['largest_win'])}@{m_all.get('largest_win_day','')} "
                f"Lloss={fmt_net(m_all['largest_loss'])}@{m_all.get('largest_loss_day','')} "
                f"delay_mean={fmt_min(m_all['mean_entry_delay_min'])} "
                f"delay_med={fmt_min(m_all['median_entry_delay_min'])} "
                f"exp_mean={fmt_num(m_all['mean_n_expansions'])} "
                f"w/R={fmt_num(m_all['mean_width_vs_R'], 3)} "
                f"hit423={fmt_pct(m_all['pct_hit_423'])} avg_bars={m_all['avg_bars']:.1f}"
            )

    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    trades_df.to_csv(OUT_TRADES, index=False)
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(OUT_SUMMARY, index=False)

    by = {(r["variant"], r["split"]): r for r in summary_rows}
    cmp_rows = [cmp_row_from_summary(r) for r in summary_rows]
    fixed_rows = load_baseline_fixed_break()
    base0935_rows = load_baseline_0935()
    cmp_rows.extend(fixed_rows)
    cmp_rows.extend(base0935_rows)
    cmp_df = pd.DataFrame(cmp_rows)
    cmp_df.to_csv(OUT_TABLE, index=False)

    plot_equity(trade_map, order, OUT_EQUITY)

    def get(v, sp="ALL"):
        return by[(v, sp)]

    key_names = {
        "F3_H": "F3_EXPAND_HOLD_VWAP_T423",
        "F3_R": "F3_EXPAND_RATCHET_HI_T423",
        "ALL_H": "ALL_EXPAND_HOLD_VWAP_T423",
        "ALL_R": "ALL_EXPAND_RATCHET_HI_T423",
    }
    K = {k: get(v) for k, v in key_names.items()}
    Kis = {k: get(v, "IS") for k, v in key_names.items()}
    Koos = {k: get(v, "OOS") for k, v in key_names.items()}

    fixed_h = next(
        (x for x in fixed_rows if x["variante"] == "FIXED_F3_BREAK_HOLD_VWAP_T423" and x["split"] == "ALL"),
        None,
    )
    fixed_r = next(
        (x for x in fixed_rows if x["variante"] == "FIXED_F3_BREAK_RATCHET_HI_T423" and x["split"] == "ALL"),
        None,
    )
    base_h = next(
        (x for x in base0935_rows if x["variante"] == "F3_0935_HOLD_VWAP_T423" and x["split"] == "ALL"),
        None,
    )
    base_r = next(
        (x for x in base0935_rows if x["variante"] == "F3_0935_RATCHET_HI_T423" and x["split"] == "ALL"),
        None,
    )

    def line_setup(label, r):
        return (
            f"| {label} | {int(r['n_trades'])} | {fmt_pct(r['win_rate'])} | {fmt_pct(r['pct_loss'])} | "
            f"{fmt_net(r['net_pts'])} | {fmt_pf(r['pf'])} | {fmt_net(r['avg_net_per_day'])} | "
            f"{fmt_net(r['largest_win'])} ({r.get('largest_win_day','')}) | "
            f"{fmt_net(r['largest_loss'])} ({r.get('largest_loss_day','')}) | "
            f"{fmt_min(r['mean_entry_delay_min'])} | {fmt_min(r['median_entry_delay_min'])} | "
            f"{fmt_num(r['mean_n_expansions'])} | {fmt_num(r['median_n_expansions'])} | "
            f"{fmt_num(r['mean_width_vs_R'], 3)} | {fmt_pct(r['pct_signal_no_entry'])} | "
            f"{fmt_pct(r['pct_hit_423'])} | {r['avg_bars']:.1f} |"
        )

    def line_base(label, b, *, delay_note="—"):
        if not b:
            return (
                f"| {label} | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |"
            )
        return (
            f"| {label} | {b['n']} | {fmt_pct(b['WR'])} | {fmt_pct(b.get('pct_loss'))} | "
            f"{fmt_net(b['net'])} | {fmt_pf(b['PF'])} | {fmt_net(b.get('avg_net_per_day'))} | "
            f"{fmt_net(b.get('largest_win'))} ({b.get('largest_win_day','')}) | "
            f"{fmt_net(b.get('largest_loss'))} ({b.get('largest_loss_day','')}) | "
            f"{fmt_min(b.get('mean_entry_delay_min'))} | {fmt_min(b.get('median_entry_delay_min'))} | "
            f"— | — | — | {fmt_pct(b.get('pct_signal_no_entry'))} | "
            f"{fmt_pct(b.get('pct_hit_423'))} | "
            f"{b['avg_bars']:.1f} |"
        )

    table_lines = "\n".join(
        [
            line_setup("F3 EXPAND HOLD_VWAP_T423", K["F3_H"]),
            line_setup("F3 EXPAND RATCHET_HI_T423", K["F3_R"]),
            line_base("F3 FIXED BREAK HOLD_VWAP_T423", fixed_h),
            line_base("F3 FIXED BREAK RATCHET_HI_T423", fixed_r),
            line_base("F3@0935 HOLD_VWAP_T423", base_h),
            line_base("F3@0935 RATCHET_HI_T423", base_r),
            line_setup("ALL EXPAND HOLD_VWAP_T423", K["ALL_H"]),
            line_setup("ALL EXPAND RATCHET_HI_T423", K["ALL_R"]),
        ]
    )

    matrix_lines = []
    for name in order:
        r = get(name)
        ri = get(name, "IS")
        ro = get(name, "OOS")
        matrix_lines.append(
            f"| {name} | {int(r['n_trades'])} | {fmt_pct(r['win_rate'])} | {fmt_net(r['net_pts'])} | "
            f"{fmt_pf(r['pf'])} | {fmt_net(r['avg_net_per_day'])} | "
            f"{fmt_net(r['largest_win'])} | {fmt_net(r['largest_loss'])} | "
            f"{fmt_net(ri['net_pts'])} | {fmt_net(ro['net_pts'])} | "
            f"{fmt_min(r['mean_entry_delay_min'])}/{fmt_min(r['median_entry_delay_min'])} | "
            f"{fmt_num(r['mean_n_expansions'])}/{fmt_num(r['median_n_expansions'])} | "
            f"{fmt_num(r['mean_width_vs_R'], 3)} | "
            f"{fmt_pct(r['pct_hit_423'])} | {r['avg_bars']:.1f} | {fmt_pct(r['pct_hold_exit'])} | "
            f"{fmt_pct(r['pct_ratchet_stop'])} |"
        )
    matrix_block = "\n".join(matrix_lines)

    # Sanity checks
    bad_0935 = 0
    bad_entry_inside = 0
    if len(trades_df):
        bad_0935 = int((trades_df["entry_time"] == "09:35").sum())
        assert bad_0935 == 0, f"BUG: {bad_0935} trades entered at 09:35"
        assert int((trades_df["entry_delay_min"] < 1).sum()) == 0, "BUG: entry_delay < 1 min"
        # confirm_close must be < confirm_cur_l
        bad_entry_inside = int(
            (trades_df["confirm_close"].astype(float) >= trades_df["confirm_cur_l"].astype(float) - EPS).sum()
        )
        assert bad_entry_inside == 0, f"BUG: {bad_entry_inside} confirms not below cur_L"
        # n_expansions >= 0 always
        assert int((trades_df["n_expansions"] < 0).sum()) == 0

    d_net_h = K["F3_H"]["net_pts"] - (fixed_h["net"] if fixed_h else 0)
    d_net_r = K["F3_R"]["net_pts"] - (fixed_r["net"] if fixed_r else 0)

    md = f"""# OR5_1m_EXPAND_BREAK — SHORT on 1m close < expanding range Low

**Status:** [EXPLORATORY] research only · no live trading  
**Date:** 2026-09-29 (America/Bogota)  
**Data:** `CANDLE_FORCE_v3_multiday_bars.parquet` (OR5 / F3) + `bars_t_1m` / `profile_developing`  
**IS/OOS:** IS ≤ {IS_LAST} · OOS ≥ 2026-06-11 · range ~2025-12-16..2026-08-25  
**Cost:** {COST} pts RT · **STOP_BUF:** {STOP_BUF} → init stop = **original** OR_H + {STOP_BUF}

---

## Entry rule (EXPANDING RANGE)

1. Build OR5 = 09:30–09:35 ET (m5_bar=1). Init `cur_H=OR_H`, `cur_L=OR_L`.
2. Walk 1m bars after OR5 (m1_bar ≥ 6):
   - **Close outside** current range:
     - `close < cur_L` → SHORT confirm → fill at **next 1m OPEN** (≤1 trade/day). **No expand** on that bar.
     - `close > cur_H` → upside breakout event: **no short**, **no expand**; range stays; keep scanning.
   - **Close inside** (`cur_L ≤ close ≤ cur_H`): **expand**  
     `cur_H = max(cur_H, bar.high)`; `cur_L = min(cur_L, bar.low)`.
3. Continue until short entry or EOD. Earliest confirm = 09:35–09:36 bar if close < OR_L.
4. Last-bar confirm with no next open → **SKIP** (no trade).

**Critical:** Fib targets and init stop use the **original OR5** High/Low/R. The expanding range is **only** for the entry trigger.

**Sanity:** entry_time==09:35 = **{bad_0935}**; confirms not below cur_L = **{bad_entry_inside}** (both must be 0).

## Universes

| Universe | Filter | Signal days | Trades (T423 HI) | % signal no entry |
|---|---|---:|---:|---:|
| F3 | OR5 red + score_v2 ≤ −5 + vbp_imbalance ≤ 0 | {n_signal_by_u['F3']} | {int(K['F3_R']['n_trades'])} | {fmt_pct(K['F3_R']['pct_signal_no_entry'])} |
| ALL | Valid OR5 (R>0), no F3 | {n_signal_by_u['ALL']} | {int(K['ALL_R']['n_trades'])} | {fmt_pct(K['ALL_R']['pct_signal_no_entry'])} |

## Trade management

Same as `OR5_1m_BREAK_BELOW` / `OR5_F3_1m_CONFIRM_5mHI`:

- **RATCHET_HI:** stop LEVEL = 5m High(n−1); updates only on 5m closes; first improve at close of m5=3 → High(m5=2). Stop-out when **1m close ≥ stop**.
- **HOLD_VWAP:** early exit when 1m close ≥ dVWAP; ratchet HI still runs as secondary stop.
- Targets: Luis fibs 2.618 / 3.33 / 4.23 via `fib_px = OR_H − k·R` from **original** OR5.
- Flatten at last 1m of RTH if still open.
- **Pre-entry ratchet:** while waiting / expanding, 5m Highs still accumulate.

## Headline compare (ALL split)

| Setup | n | %W | %L | Net | PF | avg/día | Largest W (fecha) | Largest L (fecha) | mean delay | med delay | mean exp | med exp | mean w/R | % sig no entry | %hit 4.23 | avg bars |
|---|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
{table_lines}

### Direct answers

**F3 EXPAND vs F3 FIXED BREAK (same TM T423):**
- HOLD_VWAP: expand net={fmt_net(K['F3_H']['net_pts'])} · fixed net={fmt_net(fixed_h['net'] if fixed_h else float('nan'))} · **Δnet={fmt_net(d_net_h)}**
- RATCHET_HI: expand net={fmt_net(K['F3_R']['net_pts'])} · fixed net={fmt_net(fixed_r['net'] if fixed_r else float('nan'))} · **Δnet={fmt_net(d_net_r)}**

**Entry delay (min from 09:35) & expansions before entry (F3 T423 HI):**
- delay mean={fmt_min(K['F3_R']['mean_entry_delay_min'])} · med={fmt_min(K['F3_R']['median_entry_delay_min'])}
- expansions mean={fmt_num(K['F3_R']['mean_n_expansions'])} · med={fmt_num(K['F3_R']['median_n_expansions'])}
- final range width / original R: mean={fmt_num(K['F3_R']['mean_width_vs_R'], 3)} · med={fmt_num(K['F3_R']['median_width_vs_R'], 3)}

**IS / OOS (F3 EXPAND T423):**
- HOLD: IS n={int(Kis['F3_H']['n_trades'])} net={fmt_net(Kis['F3_H']['net_pts'])} PF={fmt_pf(Kis['F3_H']['pf'])} · OOS n={int(Koos['F3_H']['n_trades'])} net={fmt_net(Koos['F3_H']['net_pts'])} PF={fmt_pf(Koos['F3_H']['pf'])}
- HI: IS n={int(Kis['F3_R']['n_trades'])} net={fmt_net(Kis['F3_R']['net_pts'])} PF={fmt_pf(Kis['F3_R']['pf'])} · OOS n={int(Koos['F3_R']['n_trades'])} net={fmt_net(Koos['F3_R']['net_pts'])} PF={fmt_pf(Koos['F3_R']['pf'])}

## Full matrix (ALL split)

| variante | n | WR | net | PF | avg/día | Lwin | Lloss | IS net | OOS net | delay mean/med | exp mean/med | w/R mean | %hit4.23 | avg bars | %hold | %ratchet |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|
{matrix_block}

## Ambiguities resolved

1. **Expand only on inside closes** (cur_L ≤ close ≤ cur_H). Outside-above = no short + no expand. Outside-below = short signal + no expand.
2. **Fib/stop from original OR5** — expanding range is entry-trigger only.
3. **Last-bar confirm → SKIP** (no phantom fill).
4. **Earliest entry** = 09:36 (confirm on 09:35–09:36 close < OR_L).
5. Baseline fixed-OR from `OR5_1m_BREAK_BELOW_compare.csv`; F3@0935 from `OR5_F3_1m_CONFIRM_5mHI_compare.csv`.

## Outputs

- Script: `run_or5_1m_expand_break.py`
- Trades / summary / compare: `OR5_1m_EXPAND_BREAK_*.csv`
- Equity: `OR5_1m_EXPAND_BREAK_equity.png`
- Spanish: `OR5_1m_EXPAND_BREAK_RESUMEN_ES.md`

*Research exploratorio — no es señal live.*
"""
    OUT_SPEC.write_text(md, encoding="utf-8")

    es = f"""# Resumen OR5 1m EXPAND BREAK — para Luis

**Qué hay de nuevo:** el rango de entrada **se expande** mientras las velas 1m cierren **dentro**.

1. OR5 = High/Low 09:30–09:35 ET.
2. Después, por cada 1m:
   - Cierre **dentro** → ampliar rango (nuevo Hi/Lo).
   - Cierre **por debajo** del Low actual → SHORT al **open de la siguiente** 1m.
   - Cierre **por encima** del High actual → no short, no expandir; seguir mirando.
3. Targets y stop inicial usan el **OR5 original** (la expansión solo dispara la entrada).
4. Último 1m del día sin siguiente open → **se salta** el día.

**Gestión:** RATCHET_HI y HOLD_VWAP × T2.618/3.33/4.23. Coste 0.50. IS≤{IS_LAST}.

## Números clave (ALL)

| Setup | n | %W | Net | PF | pts/día | Mejor (fecha) | Peor (fecha) | delay med | exp med | w/R med | % sin entrada |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|
| F3 EXPAND HOLD_VWAP_T423 | {int(K['F3_H']['n_trades'])} | {fmt_pct(K['F3_H']['win_rate'])} | {fmt_net(K['F3_H']['net_pts'])} | {fmt_pf(K['F3_H']['pf'])} | {fmt_net(K['F3_H']['avg_net_per_day'])} | {fmt_net(K['F3_H']['largest_win'])} ({K['F3_H'].get('largest_win_day','')}) | {fmt_net(K['F3_H']['largest_loss'])} ({K['F3_H'].get('largest_loss_day','')}) | {fmt_min(K['F3_H']['median_entry_delay_min'])} | {fmt_num(K['F3_H']['median_n_expansions'])} | {fmt_num(K['F3_H']['median_width_vs_R'], 3)} | {fmt_pct(K['F3_H']['pct_signal_no_entry'])} |
| F3 EXPAND RATCHET_HI_T423 | {int(K['F3_R']['n_trades'])} | {fmt_pct(K['F3_R']['win_rate'])} | {fmt_net(K['F3_R']['net_pts'])} | {fmt_pf(K['F3_R']['pf'])} | {fmt_net(K['F3_R']['avg_net_per_day'])} | {fmt_net(K['F3_R']['largest_win'])} ({K['F3_R'].get('largest_win_day','')}) | {fmt_net(K['F3_R']['largest_loss'])} ({K['F3_R'].get('largest_loss_day','')}) | {fmt_min(K['F3_R']['median_entry_delay_min'])} | {fmt_num(K['F3_R']['median_n_expansions'])} | {fmt_num(K['F3_R']['median_width_vs_R'], 3)} | {fmt_pct(K['F3_R']['pct_signal_no_entry'])} |
| F3 FIXED BREAK HOLD_VWAP_T423 | {fixed_h['n'] if fixed_h else 'n/a'} | {fmt_pct(fixed_h['WR']) if fixed_h else 'n/a'} | {fmt_net(fixed_h['net']) if fixed_h else 'n/a'} | {fmt_pf(fixed_h['PF']) if fixed_h else 'n/a'} | {fmt_net(fixed_h.get('avg_net_per_day')) if fixed_h else 'n/a'} | {fmt_net(fixed_h.get('largest_win')) if fixed_h else 'n/a'} ({fixed_h.get('largest_win_day','') if fixed_h else ''}) | {fmt_net(fixed_h.get('largest_loss')) if fixed_h else 'n/a'} ({fixed_h.get('largest_loss_day','') if fixed_h else ''}) | {fmt_min(fixed_h.get('median_entry_delay_min')) if fixed_h else 'n/a'} | — | — | {fmt_pct(fixed_h.get('pct_signal_no_entry')) if fixed_h else 'n/a'} |
| F3 FIXED BREAK RATCHET_HI_T423 | {fixed_r['n'] if fixed_r else 'n/a'} | {fmt_pct(fixed_r['WR']) if fixed_r else 'n/a'} | {fmt_net(fixed_r['net']) if fixed_r else 'n/a'} | {fmt_pf(fixed_r['PF']) if fixed_r else 'n/a'} | {fmt_net(fixed_r.get('avg_net_per_day')) if fixed_r else 'n/a'} | {fmt_net(fixed_r.get('largest_win')) if fixed_r else 'n/a'} ({fixed_r.get('largest_win_day','') if fixed_r else ''}) | {fmt_net(fixed_r.get('largest_loss')) if fixed_r else 'n/a'} ({fixed_r.get('largest_loss_day','') if fixed_r else ''}) | {fmt_min(fixed_r.get('median_entry_delay_min')) if fixed_r else 'n/a'} | — | — | {fmt_pct(fixed_r.get('pct_signal_no_entry')) if fixed_r else 'n/a'} |
| F3@0935 HOLD_VWAP_T423 | {base_h['n'] if base_h else 'n/a'} | {fmt_pct(base_h['WR']) if base_h else 'n/a'} | {fmt_net(base_h['net']) if base_h else 'n/a'} | {fmt_pf(base_h['PF']) if base_h else 'n/a'} | {fmt_net(base_h.get('avg_net_per_day')) if base_h else 'n/a'} | {fmt_net(base_h.get('largest_win')) if base_h else 'n/a'} ({base_h.get('largest_win_day','') if base_h else ''}) | {fmt_net(base_h.get('largest_loss')) if base_h else 'n/a'} ({base_h.get('largest_loss_day','') if base_h else ''}) | 0 | — | — | 0% |
| F3@0935 RATCHET_HI_T423 | {base_r['n'] if base_r else 'n/a'} | {fmt_pct(base_r['WR']) if base_r else 'n/a'} | {fmt_net(base_r['net']) if base_r else 'n/a'} | {fmt_pf(base_r['PF']) if base_r else 'n/a'} | {fmt_net(base_r.get('avg_net_per_day')) if base_r else 'n/a'} | {fmt_net(base_r.get('largest_win')) if base_r else 'n/a'} ({base_r.get('largest_win_day','') if base_r else ''}) | {fmt_net(base_r.get('largest_loss')) if base_r else 'n/a'} ({base_r.get('largest_loss_day','') if base_r else ''}) | 0 | — | — | 0% |
| ALL EXPAND HOLD_VWAP_T423 | {int(K['ALL_H']['n_trades'])} | {fmt_pct(K['ALL_H']['win_rate'])} | {fmt_net(K['ALL_H']['net_pts'])} | {fmt_pf(K['ALL_H']['pf'])} | {fmt_net(K['ALL_H']['avg_net_per_day'])} | {fmt_net(K['ALL_H']['largest_win'])} ({K['ALL_H'].get('largest_win_day','')}) | {fmt_net(K['ALL_H']['largest_loss'])} ({K['ALL_H'].get('largest_loss_day','')}) | {fmt_min(K['ALL_H']['median_entry_delay_min'])} | {fmt_num(K['ALL_H']['median_n_expansions'])} | {fmt_num(K['ALL_H']['median_width_vs_R'], 3)} | {fmt_pct(K['ALL_H']['pct_signal_no_entry'])} |
| ALL EXPAND RATCHET_HI_T423 | {int(K['ALL_R']['n_trades'])} | {fmt_pct(K['ALL_R']['win_rate'])} | {fmt_net(K['ALL_R']['net_pts'])} | {fmt_pf(K['ALL_R']['pf'])} | {fmt_net(K['ALL_R']['avg_net_per_day'])} | {fmt_net(K['ALL_R']['largest_win'])} ({K['ALL_R'].get('largest_win_day','')}) | {fmt_net(K['ALL_R']['largest_loss'])} ({K['ALL_R'].get('largest_loss_day','')}) | {fmt_min(K['ALL_R']['median_entry_delay_min'])} | {fmt_num(K['ALL_R']['median_n_expansions'])} | {fmt_num(K['ALL_R']['median_width_vs_R'], 3)} | {fmt_pct(K['ALL_R']['pct_signal_no_entry'])} |

## Respuesta directa

1. **Δnet EXPAND vs FIXED BREAK (F3 T423):** HOLD **{fmt_net(d_net_h)}** · HI **{fmt_net(d_net_r)}**.
2. **Retraso entrada (desde 09:35):** F3 mediana **{fmt_min(K['F3_R']['median_entry_delay_min'])} min** (media {fmt_min(K['F3_R']['mean_entry_delay_min'])}).
3. **Expansiones antes de entrada (F3 HI):** mediana **{fmt_num(K['F3_R']['median_n_expansions'])}** (media {fmt_num(K['F3_R']['mean_n_expansions'])}).
4. **Ancho final / R original (F3 HI):** mediana **{fmt_num(K['F3_R']['median_width_vs_R'], 3)}** (media {fmt_num(K['F3_R']['mean_width_vs_R'], 3)}).
5. **% días F3 sin entrada:** {fmt_pct(K['F3_R']['pct_signal_no_entry'])} ({int(K['F3_R'].get('n_signal_no_entry') or 0)} de {n_signal_by_u['F3']}).

### IS / OOS (F3 EXPAND T423)
- HOLD: IS net={fmt_net(Kis['F3_H']['net_pts'])} PF={fmt_pf(Kis['F3_H']['pf'])} · OOS net={fmt_net(Koos['F3_H']['net_pts'])} PF={fmt_pf(Koos['F3_H']['pf'])}
- HI: IS net={fmt_net(Kis['F3_R']['net_pts'])} PF={fmt_pf(Kis['F3_R']['pf'])} · OOS net={fmt_net(Koos['F3_R']['net_pts'])} PF={fmt_pf(Koos['F3_R']['pf'])}

Detalle EN: `OR5_1m_EXPAND_BREAK.md` · equity: `OR5_1m_EXPAND_BREAK_equity.png`  
CSV: `OR5_1m_EXPAND_BREAK_trades.csv` / `_summary.csv` / `_compare.csv`

*Research exploratorio — no es señal live.*
"""
    OUT_ES.write_text(es, encoding="utf-8")

    print("\n=== DONE ===")
    print(f"trades → {OUT_TRADES}")
    print(f"summary → {OUT_SUMMARY}")
    print(f"compare → {OUT_TABLE}")
    print(f"equity → {OUT_EQUITY}")
    print(f"spec → {OUT_SPEC}")
    print(f"ES → {OUT_ES}")
    print(
        f"KEY F3 EXPAND HI_T423: n={int(K['F3_R']['n_trades'])} net={K['F3_R']['net_pts']:.1f} "
        f"PF={fmt_pf(K['F3_R']['pf'])} delay_med={fmt_min(K['F3_R']['median_entry_delay_min'])} "
        f"exp_med={fmt_num(K['F3_R']['median_n_expansions'])} "
        f"w/R_med={fmt_num(K['F3_R']['median_width_vs_R'], 3)}"
    )
    print(
        f"KEY F3 EXPAND HOLD_T423: n={int(K['F3_H']['n_trades'])} net={K['F3_H']['net_pts']:.1f} "
        f"PF={fmt_pf(K['F3_H']['pf'])}"
    )
    if fixed_r:
        print(
            f"FIXED F3 BREAK HI_T423: n={fixed_r['n']} net={fixed_r['net']:.1f} "
            f"Δnet_expand={d_net_r:.1f}"
        )
    if fixed_h:
        print(
            f"FIXED F3 BREAK HOLD_T423: n={fixed_h['n']} net={fixed_h['net']:.1f} "
            f"Δnet_expand={d_net_h:.1f}"
        )
    if base_r:
        print(f"BASE F3@0935 HI_T423: n={base_r['n']} net={base_r['net']:.1f}")
    if base_h:
        print(f"BASE F3@0935 HOLD_T423: n={base_h['n']} net={base_h['net']:.1f}")
    print(
        f"KEY ALL EXPAND HI_T423: n={int(K['ALL_R']['n_trades'])} net={K['ALL_R']['net_pts']:.1f} "
        f"PF={fmt_pf(K['ALL_R']['pf'])}"
    )


if __name__ == "__main__":
    main()
