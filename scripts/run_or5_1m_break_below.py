#!/usr/bin/env python3
"""OR5_1m_BREAK_BELOW — SHORT entry on 1m close < OR_L (delayed breakout), not at 09:35.

Entry (NEW):
  After OR5 (09:30–09:35 ET), do NOT enter at 09:35.
  Wait for first 1m candle with CLOSE < OR_L (m1_bar >= 6).
  Fill at NEXT 1m OPEN. If confirming close is last RTH 1m (no next open) → SKIP day.
  ≤1 trade/day. If never closes below OR_L after OR5 → no trade.

Universes:
  A) F3_0: OR5 red + score_v2 ≤ −5 + vbp_imbalance ≤ 0
  B) ALL days with valid OR5 (R > 0)

TM (same as OR5_F3_1m_CONFIRM_5mHI):
  - RATCHET_HI: stop LEVEL from 5m High(n−1); first improve at close m5=3 → High(m5=2).
    Level does NOT trail on 1m highs. Stop-out when 1m CLOSE ≥ stop.
  - HOLD_VWAP: early exit when 1m close ≥ dVWAP; ratchet HI still available as secondary.
  - Targets T2.618 / T3.33 / T4.23 via fib_px = OR_H − k*R (existing helper).
  - Cost 0.50 RT · STOP_BUF=2 → init stop = OR_H+2 · flatten last 1m RTH.
  - Pre-entry: still accumulate 5m Highs + ratchet so late entries inherit current stop level.

Baseline in compare: F3@09:35 RATCHET_HI_T423 + HOLD_VWAP_T423 from
  OR5_F3_1m_CONFIRM_5mHI_compare.csv

IS last day = 2026-06-10 · range ~2025-12-16..2026-08-25
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
BASE_CONFIRM = OUT_DIR / "OR5_F3_1m_CONFIRM_5mHI_compare.csv"
BASE_CONFIRM_TRADES = OUT_DIR / "OR5_F3_1m_CONFIRM_5mHI_trades.csv"

OUT_SPEC = OUT_DIR / "OR5_1m_BREAK_BELOW.md"
OUT_TRADES = OUT_DIR / "OR5_1m_BREAK_BELOW_trades.csv"
OUT_SUMMARY = OUT_DIR / "OR5_1m_BREAK_BELOW_summary.csv"
OUT_TABLE = OUT_DIR / "OR5_1m_BREAK_BELOW_compare.csv"
OUT_EQUITY = OUT_DIR / "OR5_1m_BREAK_BELOW_equity.png"
OUT_ES = OUT_DIR / "OR5_1m_BREAK_BELOW_RESUMEN_ES.md"

COST = 0.50
STOP_BUF = 2.0
EPS = 1e-9
IS_LAST = "2026-06-10"
SHORT_CUT = -5.0
FIB_LEVELS = (2.618, 3.33, 4.23)
# m1_bar of first 1m after OR5 (09:35 ET open)
POST_OR_M1 = 6


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
    """Return list of (contract, day, or5, post1m) and signal-day count meta.

    universe: 'F3' | 'ALL'
    post1m starts at m1_bar >= POST_OR_M1 (includes bars before entry for ratchet).
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


def simulate_short_break_below(
    or5,
    g1: pd.DataFrame,
    *,
    max_fib: float,
    hold_vwap: bool,
    cost: float = COST,
    variant: str,
    universe: str,
) -> dict | None:
    """Find 1m close < OR_L then enter next open; TM = 5mHI confirm + optional HOLD_VWAP."""
    g = g1.sort_values("m1_bar").reset_index(drop=True)
    if len(g) < 2:
        # need at least confirm + next open
        return None

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

    completed_5m_hi: dict[int, float] = {}
    buck_m5: int | None = None
    buck_hi = float("-inf")

    # Phase: look for confirm close < OR_L, then enter at next open
    pending_entry = False
    confirm_m1 = None
    confirm_time = None
    confirm_close = None
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
                # This bar's OPEN is the fill
                entry_px = o
                entry_m1 = n1
                entry_time = t_et
                in_trade = True
                pending_entry = False
                # fall through to manage this bar as first held bar
            else:
                # Looking for confirm: 1m close < OR_L
                # Update ratchet even while waiting so late entry inherits stop level
                update_bucket_and_ratchet(n1, m5, hi)
                if c < or_l - EPS:
                    if i == len(g) - 1:
                        # last bar of day closes outside — no next open → SKIP
                        return None
                    pending_entry = True
                    confirm_m1 = n1
                    confirm_time = t_et
                    confirm_close = c
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
    # minutes from 09:35 (m1=6) to entry open
    entry_delay_min = int(entry_m1 - POST_OR_M1)
    confirm_delay_min = int(confirm_m1 - POST_OR_M1) if confirm_m1 is not None else None

    return {
        "contract": str(or5["contract"]),
        "day": as_day_str(or5["trading_day"]),
        "side": "SHORT",
        "tf": "1m_break_below",
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
        t = simulate_short_break_below(or5, g1, **kw)
        if t is not None:
            rows.append(t)
    return pd.DataFrame(rows)


def enrich_metrics(m: dict, part: pd.DataFrame) -> dict:
    if part is None or len(part) == 0:
        for k in (
            "pct_win",
            "pct_loss",
            "avg_net_per_day",
            "largest_win",
            "largest_loss",
            "largest_win_day",
            "largest_loss_day",
            "mean_entry_delay_min",
            "median_entry_delay_min",
        ):
            m[k] = float("nan") if "day" not in k else ""
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
    return m


def split_rows(variant: str, tdf: pd.DataFrame, *, universe: str, n_signal: int) -> list[dict]:
    out = []
    is_df, oos_df = apply_date_split(tdf)
    for label, part in (("IS", is_df), ("OOS", oos_df), ("ALL", tdf)):
        m = metrics(part, label)
        m["variant"] = variant
        m["universe"] = universe
        m["split"] = label
        m["tf"] = "1m_break_below"
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
            # signal-but-no-entry only meaningful for ALL split vs full universe signal count
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
                f"{universe}_BREAK_HOLD_VWAP_T{fib_tag(fib)}",
                dict(max_fib=fib, hold_vwap=True, universe=universe),
            )
        )
    for fib in FIB_LEVELS:
        specs.append(
            (
                f"{universe}_BREAK_RATCHET_HI_T{fib_tag(fib)}",
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


def plot_equity(trade_map: dict[str, pd.DataFrame], variants: list[str], out_path: Path):
    fig, ax = plt.subplots(figsize=(12, 6))
    # highlight T423 lines
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
    ax.set_title("OR5 1m BREAK BELOW OR_L — F3 vs ALL · HOLD_VWAP / RATCHET_HI (T423 bold)")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Net pts acumulados")
    ax.legend(fontsize=8, loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def load_baseline_0935() -> tuple[list[dict], dict]:
    """Load F3@09:35 confirm baseline T423 rows + extras from trades."""
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
                "net": float(nets.sum()),
                "n": len(p),
                "WR": float((nets > 0).mean()),
            }
    # patch days into ALL rows
    for row in rows:
        if row["split"] == "ALL" and row["variante"] in extras:
            ex = extras[row["variante"]]
            row["largest_win_day"] = ex["largest_win_day"]
            row["largest_loss_day"] = ex["largest_loss_day"]
            if row.get("largest_win") is None:
                row["largest_win"] = ex["largest_win"]
            if row.get("largest_loss") is None:
                row["largest_loss"] = ex["largest_loss"]
    return rows, extras


def cmp_row_from_summary(r: dict) -> dict:
    return {
        "variante": r["variant"],
        "split": r["split"],
        "tf": "1m_break_below",
        "universe": r["universe"],
        "entry_mode": "1m_close_below_OR_L",
        "n": int(r["n_trades"]),
        "WR": round(r["win_rate"], 4) if not math.isnan(r["win_rate"]) else None,
        "pct_win": round(r["pct_win"], 4) if not math.isnan(r.get("pct_win", float("nan"))) else None,
        "pct_loss": round(r["pct_loss"], 4) if not math.isnan(r.get("pct_loss", float("nan"))) else None,
        "net": round(r["net_pts"], 2),
        "PF": None
        if r["pf"] is None or (isinstance(r["pf"], float) and math.isnan(r["pf"]))
        else round(float(r["pf"]), 3),
        "maxDD": round(r["max_dd"], 2),
        "avg_net_per_day": round(r["avg_net_per_day"], 2)
        if not math.isnan(r.get("avg_net_per_day", float("nan")))
        else None,
        "largest_win": round(r["largest_win"], 2)
        if not math.isnan(r.get("largest_win", float("nan")))
        else None,
        "largest_loss": round(r["largest_loss"], 2)
        if not math.isnan(r.get("largest_loss", float("nan")))
        else None,
        "largest_win_day": r.get("largest_win_day", ""),
        "largest_loss_day": r.get("largest_loss_day", ""),
        "pct_hit_2618": round(r["pct_hit_2618"], 4)
        if not math.isnan(r.get("pct_hit_2618", float("nan")))
        else None,
        "pct_hit_333": round(r["pct_hit_333"], 4)
        if not math.isnan(r.get("pct_hit_333", float("nan")))
        else None,
        "pct_hit_423": round(r["pct_hit_423"], 4)
        if not math.isnan(r.get("pct_hit_423", float("nan")))
        else None,
        "pct_hold_exit": round(r["pct_hold_exit"], 4)
        if not math.isnan(r.get("pct_hold_exit", float("nan")))
        else None,
        "pct_ratchet_stop": round(r["pct_ratchet_stop"], 4)
        if not math.isnan(r.get("pct_ratchet_stop", float("nan")))
        else None,
        "pct_target": round(r["pct_target"], 4)
        if not math.isnan(r.get("pct_target", float("nan")))
        else None,
        "pct_init_stop": round(r["pct_init_stop"], 4)
        if not math.isnan(r.get("pct_init_stop", float("nan")))
        else None,
        "avg_bars": round(r["avg_bars"], 2) if not math.isnan(r.get("avg_bars", float("nan"))) else None,
        "mean_entry_delay_min": round(r["mean_entry_delay_min"], 2)
        if not math.isnan(r.get("mean_entry_delay_min", float("nan")))
        else None,
        "median_entry_delay_min": round(r["median_entry_delay_min"], 2)
        if not math.isnan(r.get("median_entry_delay_min", float("nan")))
        else None,
        "pct_signal_no_entry": round(r["pct_signal_no_entry"], 4)
        if not math.isnan(r.get("pct_signal_no_entry", float("nan")))
        else None,
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
                f"hit423={fmt_pct(m_all['pct_hit_423'])} avg_bars={m_all['avg_bars']:.1f}"
            )

    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    trades_df.to_csv(OUT_TRADES, index=False)
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(OUT_SUMMARY, index=False)

    by = {(r["variant"], r["split"]): r for r in summary_rows}
    cmp_rows = [cmp_row_from_summary(r) for r in summary_rows]
    base_rows, base_ex = load_baseline_0935()
    cmp_rows.extend(base_rows)
    cmp_df = pd.DataFrame(cmp_rows)
    cmp_df.to_csv(OUT_TABLE, index=False)

    plot_equity(trade_map, order, OUT_EQUITY)

    def get(v, sp="ALL"):
        return by[(v, sp)]

    # Key setups
    key_names = {
        "F3_H": "F3_BREAK_HOLD_VWAP_T423",
        "F3_R": "F3_BREAK_RATCHET_HI_T423",
        "ALL_H": "ALL_BREAK_HOLD_VWAP_T423",
        "ALL_R": "ALL_BREAK_RATCHET_HI_T423",
    }
    K = {k: get(v) for k, v in key_names.items()}
    Kis = {k: get(v, "IS") for k, v in key_names.items()}
    Koos = {k: get(v, "OOS") for k, v in key_names.items()}

    base_h = next((x for x in base_rows if x["variante"] == "F3_0935_HOLD_VWAP_T423" and x["split"] == "ALL"), None)
    base_r = next((x for x in base_rows if x["variante"] == "F3_0935_RATCHET_HI_T423" and x["split"] == "ALL"), None)

    def line_setup(label, r):
        return (
            f"| {label} | {int(r['n_trades'])} | {fmt_pct(r['win_rate'])} | {fmt_pct(r['pct_loss'])} | "
            f"{fmt_net(r['net_pts'])} | {fmt_pf(r['pf'])} | {fmt_net(r['avg_net_per_day'])} | "
            f"{fmt_net(r['largest_win'])} ({r.get('largest_win_day','')}) | "
            f"{fmt_net(r['largest_loss'])} ({r.get('largest_loss_day','')}) | "
            f"{fmt_min(r['mean_entry_delay_min'])} | {fmt_min(r['median_entry_delay_min'])} | "
            f"{fmt_pct(r['pct_signal_no_entry'])} | {fmt_pct(r['pct_hit_2618'])} | "
            f"{fmt_pct(r['pct_hit_333'])} | {fmt_pct(r['pct_hit_423'])} | {r['avg_bars']:.1f} |"
        )

    def line_base(label, b):
        if not b:
            return f"| {label} | — | — | — | — | — | — | — | — | 0 | 0 | 0% | — | — | — | — |"
        return (
            f"| {label} | {b['n']} | {fmt_pct(b['WR'])} | {fmt_pct(b.get('pct_loss'))} | "
            f"{fmt_net(b['net'])} | {fmt_pf(b['PF'])} | {fmt_net(b.get('avg_net_per_day'))} | "
            f"{fmt_net(b.get('largest_win'))} ({b.get('largest_win_day','')}) | "
            f"{fmt_net(b.get('largest_loss'))} ({b.get('largest_loss_day','')}) | "
            f"0.0 | 0.0 | 0.0% | {fmt_pct(b.get('pct_hit_2618'))} | "
            f"{fmt_pct(b.get('pct_hit_333'))} | {fmt_pct(b.get('pct_hit_423'))} | "
            f"{b['avg_bars']:.1f} |"
        )

    table_lines = "\n".join(
        [
            line_setup("F3 delayed HOLD_VWAP_T423", K["F3_H"]),
            line_setup("F3 delayed RATCHET_HI_T423", K["F3_R"]),
            line_base("F3@0935 HOLD_VWAP_T423 (baseline)", base_h),
            line_base("F3@0935 RATCHET_HI_T423 (baseline)", base_r),
            line_setup("ALL delayed HOLD_VWAP_T423", K["ALL_H"]),
            line_setup("ALL delayed RATCHET_HI_T423", K["ALL_R"]),
        ]
    )

    # Full matrix ALL split
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
            f"{fmt_pct(r['pct_hit_423'])} | {r['avg_bars']:.1f} | {fmt_pct(r['pct_hold_exit'])} | "
            f"{fmt_pct(r['pct_ratchet_stop'])} |"
        )
    matrix_block = "\n".join(matrix_lines)

    # Sanity: no entry at 09:35
    bad_0935 = 0
    if len(trades_df):
        bad_0935 = int((trades_df["entry_time"] == "09:35").sum())
        assert bad_0935 == 0, f"BUG: {bad_0935} trades entered at 09:35"
        assert int((trades_df["entry_delay_min"] < 1).sum()) == 0, "BUG: entry_delay < 1 min"

    md = f"""# OR5_1m_BREAK_BELOW — SHORT on 1m close < OR_L (delayed breakout)

**Status:** [EXPLORATORY] research only · no live trading  
**Date:** 2026-09-29 (America/Bogota)  
**Data:** `CANDLE_FORCE_v3_multiday_bars.parquet` (OR5 / F3) + `bars_t_1m` / `profile_developing`  
**IS/OOS:** IS ≤ {IS_LAST} · OOS ≥ 2026-06-11 · range ~2025-12-16..2026-08-25  
**Cost:** {COST} pts RT · **STOP_BUF:** {STOP_BUF} → init stop = OR_H + {STOP_BUF}

---

## Entry rule (NEW)

1. Build OR5 = 09:30–09:35 ET (m5_bar=1). **Do NOT enter at 09:35.**
2. After OR5, watch 1m bars (m1_bar ≥ 6). First bar whose **close < OR_L** is the confirm.
3. **Fill = next 1m OPEN** after that confirm.
4. If the confirming close is the **last RTH 1m** (no next open) → **SKIP** (no trade that day). Documented choice: skip, not EOD flat.
5. ≤1 trade/day. If price never closes below OR_L after OR5 → no trade.

**Sanity:** trades with entry_time==09:35 = **{bad_0935}** (must be 0).

## Universes

| Universe | Filter | Signal days | Trades (T423 HI) | % signal no entry |
|---|---|---:|---:|---:|
| F3 | OR5 red + score_v2 ≤ −5 + vbp_imbalance ≤ 0 | {n_signal_by_u['F3']} | {int(K['F3_R']['n_trades'])} | {fmt_pct(K['F3_R']['pct_signal_no_entry'])} |
| ALL | Valid OR5 (R>0), no F3 | {n_signal_by_u['ALL']} | {int(K['ALL_R']['n_trades'])} | {fmt_pct(K['ALL_R']['pct_signal_no_entry'])} |

## Trade management

Same as `OR5_F3_1m_CONFIRM_5mHI`:

- **RATCHET_HI:** stop LEVEL = 5m High(n−1); updates only on 5m closes; first improve at close of m5=3 → High(m5=2). Does **not** trail on 1m highs. Stop-out when **1m close ≥ stop**.
- **HOLD_VWAP:** early exit when 1m close ≥ dVWAP; ratchet HI still runs as secondary stop.
- Targets: Luis fibs 2.618 / 3.33 / 4.23 via `fib_px = OR_H − k·R` (existing helper).
- Flatten at last 1m of RTH if still open.
- **Pre-entry ratchet:** while waiting for breakout, 5m Highs still accumulate and the stop level ratchets, so a late entry inherits the current stop (not a fresh OR_H+2 if m5≥3 already closed).

## Headline compare (ALL split) — Luis prefs first

| Setup | n | %W | %L | Net | PF | avg/día | Largest W (fecha) | Largest L (fecha) | mean delay min | med delay min | % sig no entry | %hit 2.618 | %hit 3.33 | %hit 4.23 | avg bars |
|---|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
{table_lines}

### Direct answers

**F3 delayed vs F3@0935 (same TM T423):**
- HOLD_VWAP: delayed net={fmt_net(K['F3_H']['net_pts'])} PF={fmt_pf(K['F3_H']['pf'])} n={int(K['F3_H']['n_trades'])} · @0935 net={fmt_net(base_h['net'] if base_h else float('nan'))} PF={fmt_pf(base_h['PF'] if base_h else None)} n={base_h['n'] if base_h else 'n/a'} · Δnet={fmt_net(K['F3_H']['net_pts']-(base_h['net'] if base_h else 0))}
- RATCHET_HI: delayed net={fmt_net(K['F3_R']['net_pts'])} PF={fmt_pf(K['F3_R']['pf'])} n={int(K['F3_R']['n_trades'])} · @0935 net={fmt_net(base_r['net'] if base_r else float('nan'))} PF={fmt_pf(base_r['PF'] if base_r else None)} n={base_r['n'] if base_r else 'n/a'} · Δnet={fmt_net(K['F3_R']['net_pts']-(base_r['net'] if base_r else 0))}

**F3 delayed vs ALL delayed (does F3 still help?):**
- HOLD T423: F3 net={fmt_net(K['F3_H']['net_pts'])} avg/día={fmt_net(K['F3_H']['avg_net_per_day'])} · ALL net={fmt_net(K['ALL_H']['net_pts'])} avg/día={fmt_net(K['ALL_H']['avg_net_per_day'])}
- HI T423: F3 net={fmt_net(K['F3_R']['net_pts'])} avg/día={fmt_net(K['F3_R']['avg_net_per_day'])} · ALL net={fmt_net(K['ALL_R']['net_pts'])} avg/día={fmt_net(K['ALL_R']['avg_net_per_day'])}

**Entry delay (minutes from 09:35):**
- F3: mean={fmt_min(K['F3_R']['mean_entry_delay_min'])} · median={fmt_min(K['F3_R']['median_entry_delay_min'])}
- ALL: mean={fmt_min(K['ALL_R']['mean_entry_delay_min'])} · median={fmt_min(K['ALL_R']['median_entry_delay_min'])}

**IS / OOS (F3 delayed T423):**
- HOLD: IS n={int(Kis['F3_H']['n_trades'])} net={fmt_net(Kis['F3_H']['net_pts'])} PF={fmt_pf(Kis['F3_H']['pf'])} · OOS n={int(Koos['F3_H']['n_trades'])} net={fmt_net(Koos['F3_H']['net_pts'])} PF={fmt_pf(Koos['F3_H']['pf'])}
- HI: IS n={int(Kis['F3_R']['n_trades'])} net={fmt_net(Kis['F3_R']['net_pts'])} PF={fmt_pf(Kis['F3_R']['pf'])} · OOS n={int(Koos['F3_R']['n_trades'])} net={fmt_net(Koos['F3_R']['net_pts'])} PF={fmt_pf(Koos['F3_R']['pf'])}

## Full matrix (ALL split)

| variante | n | WR | net | PF | avg/día | Lwin | Lloss | IS net | OOS net | delay mean/med | %hit4.23 | avg bars | %hold | %ratchet |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
{matrix_block}

## Ambiguities resolved

1. **Last-bar confirm with no next open → SKIP** (no trade), not EOD flat from a phantom fill.
2. **fib_px** = `OR_H − k·R` (same as existing scripts), not OR_H−(k−1)·R.
3. **Pre-entry ratchet active:** stop level updates on 5m closes while waiting for breakout.
4. **Earliest entry** = 09:36 (confirm on 09:35–09:36 bar close < OR_L → next open). Never 09:35.
5. Baseline F3@0935 pulled from `OR5_F3_1m_CONFIRM_5mHI_compare.csv` (tf=1m_confirm_5mHI).

## Outputs

- Script: `run_or5_1m_break_below.py`
- Trades / summary / compare: `OR5_1m_BREAK_BELOW_*.csv`
- Equity: `OR5_1m_BREAK_BELOW_equity.png`
- Spanish: `OR5_1m_BREAK_BELOW_RESUMEN_ES.md`

*Research exploratorio — no es señal live.*
"""
    OUT_SPEC.write_text(md, encoding="utf-8")

    # Spanish resumen for Luis
    es = f"""# Resumen OR5 1m BREAK BELOW — para Luis

**Qué hay de nuevo:** ya **no** entramos a las 09:35.  
Después del OR5 (09:30–09:35 ET) esperamos a que una vela de **1 minuto CIERRE por debajo de OR_L**.  
La entrada es el **OPEN de la siguiente vela 1m**. Máximo 1 trade/día. Si nunca cierra bajo OR_L → no hay trade.

**Si el último 1m del día cierra fuera y no hay siguiente open → se SALTA el día** (no flat EOD inventado).

**Gestión (igual que confirm 5mHI):** RATCHET_HI (nivel = High 5m n−1; salida cuando close 1m ≥ stop) y HOLD_VWAP (salida si close 1m ≥ dVWAP). Targets 2.618 / 3.33 / 4.23. Coste 0.50. IS≤{IS_LAST}.

## Números clave (ALL)

| Setup | n | %W | Net | PF | pts/día | Mejor (fecha) | Peor (fecha) | delay med (min) | % días sin breakout |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|
| F3 delayed HOLD_VWAP_T423 | {int(K['F3_H']['n_trades'])} | {fmt_pct(K['F3_H']['win_rate'])} | {fmt_net(K['F3_H']['net_pts'])} | {fmt_pf(K['F3_H']['pf'])} | {fmt_net(K['F3_H']['avg_net_per_day'])} | {fmt_net(K['F3_H']['largest_win'])} ({K['F3_H'].get('largest_win_day','')}) | {fmt_net(K['F3_H']['largest_loss'])} ({K['F3_H'].get('largest_loss_day','')}) | {fmt_min(K['F3_H']['median_entry_delay_min'])} | {fmt_pct(K['F3_H']['pct_signal_no_entry'])} |
| F3 delayed RATCHET_HI_T423 | {int(K['F3_R']['n_trades'])} | {fmt_pct(K['F3_R']['win_rate'])} | {fmt_net(K['F3_R']['net_pts'])} | {fmt_pf(K['F3_R']['pf'])} | {fmt_net(K['F3_R']['avg_net_per_day'])} | {fmt_net(K['F3_R']['largest_win'])} ({K['F3_R'].get('largest_win_day','')}) | {fmt_net(K['F3_R']['largest_loss'])} ({K['F3_R'].get('largest_loss_day','')}) | {fmt_min(K['F3_R']['median_entry_delay_min'])} | {fmt_pct(K['F3_R']['pct_signal_no_entry'])} |
| F3@0935 HOLD_VWAP_T423 | {base_h['n'] if base_h else 'n/a'} | {fmt_pct(base_h['WR']) if base_h else 'n/a'} | {fmt_net(base_h['net']) if base_h else 'n/a'} | {fmt_pf(base_h['PF']) if base_h else 'n/a'} | {fmt_net(base_h.get('avg_net_per_day')) if base_h else 'n/a'} | {fmt_net(base_h.get('largest_win')) if base_h else 'n/a'} ({base_h.get('largest_win_day','') if base_h else ''}) | {fmt_net(base_h.get('largest_loss')) if base_h else 'n/a'} ({base_h.get('largest_loss_day','') if base_h else ''}) | 0 | 0% |
| F3@0935 RATCHET_HI_T423 | {base_r['n'] if base_r else 'n/a'} | {fmt_pct(base_r['WR']) if base_r else 'n/a'} | {fmt_net(base_r['net']) if base_r else 'n/a'} | {fmt_pf(base_r['PF']) if base_r else 'n/a'} | {fmt_net(base_r.get('avg_net_per_day')) if base_r else 'n/a'} | {fmt_net(base_r.get('largest_win')) if base_r else 'n/a'} ({base_r.get('largest_win_day','') if base_r else ''}) | {fmt_net(base_r.get('largest_loss')) if base_r else 'n/a'} ({base_r.get('largest_loss_day','') if base_r else ''}) | 0 | 0% |
| ALL delayed HOLD_VWAP_T423 | {int(K['ALL_H']['n_trades'])} | {fmt_pct(K['ALL_H']['win_rate'])} | {fmt_net(K['ALL_H']['net_pts'])} | {fmt_pf(K['ALL_H']['pf'])} | {fmt_net(K['ALL_H']['avg_net_per_day'])} | {fmt_net(K['ALL_H']['largest_win'])} ({K['ALL_H'].get('largest_win_day','')}) | {fmt_net(K['ALL_H']['largest_loss'])} ({K['ALL_H'].get('largest_loss_day','')}) | {fmt_min(K['ALL_H']['median_entry_delay_min'])} | {fmt_pct(K['ALL_H']['pct_signal_no_entry'])} |
| ALL delayed RATCHET_HI_T423 | {int(K['ALL_R']['n_trades'])} | {fmt_pct(K['ALL_R']['win_rate'])} | {fmt_net(K['ALL_R']['net_pts'])} | {fmt_pf(K['ALL_R']['pf'])} | {fmt_net(K['ALL_R']['avg_net_per_day'])} | {fmt_net(K['ALL_R']['largest_win'])} ({K['ALL_R'].get('largest_win_day','')}) | {fmt_net(K['ALL_R']['largest_loss'])} ({K['ALL_R'].get('largest_loss_day','')}) | {fmt_min(K['ALL_R']['median_entry_delay_min'])} | {fmt_pct(K['ALL_R']['pct_signal_no_entry'])} |

## Respuesta directa

1. **Retraso de entrada (desde 09:35):** F3 mediana **{fmt_min(K['F3_R']['median_entry_delay_min'])} min** (media {fmt_min(K['F3_R']['mean_entry_delay_min'])}); ALL mediana **{fmt_min(K['ALL_R']['median_entry_delay_min'])}** (media {fmt_min(K['ALL_R']['mean_entry_delay_min'])}).
2. **F3 delayed vs F3@0935:** HOLD Δnet={fmt_net(K['F3_H']['net_pts']-(base_h['net'] if base_h else 0))} · HI Δnet={fmt_net(K['F3_R']['net_pts']-(base_r['net'] if base_r else 0))}.
3. **¿Sigue ayudando F3 con entrada tardía?** Compara pts/día F3 vs ALL arriba (HOLD y HI T423).
4. **% días con señal F3 sin breakout:** {fmt_pct(K['F3_R']['pct_signal_no_entry'])} ({int(K['F3_R'].get('n_signal_no_entry') or 0)} de {n_signal_by_u['F3']}).
5. **Hits fib 4.23:** F3 HOLD {fmt_pct(K['F3_H']['pct_hit_423'])} · F3 HI {fmt_pct(K['F3_R']['pct_hit_423'])} · ALL HOLD {fmt_pct(K['ALL_H']['pct_hit_423'])} · ALL HI {fmt_pct(K['ALL_R']['pct_hit_423'])}.

### IS / OOS (F3 delayed T423)
- HOLD: IS net={fmt_net(Kis['F3_H']['net_pts'])} PF={fmt_pf(Kis['F3_H']['pf'])} · OOS net={fmt_net(Koos['F3_H']['net_pts'])} PF={fmt_pf(Koos['F3_H']['pf'])}
- HI: IS net={fmt_net(Kis['F3_R']['net_pts'])} PF={fmt_pf(Kis['F3_R']['pf'])} · OOS net={fmt_net(Koos['F3_R']['net_pts'])} PF={fmt_pf(Koos['F3_R']['pf'])}

Detalle EN: `OR5_1m_BREAK_BELOW.md` · equity: `OR5_1m_BREAK_BELOW_equity.png`  
CSV: `OR5_1m_BREAK_BELOW_trades.csv` / `_summary.csv` / `_compare.csv`

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
        f"KEY F3 delayed HI_T423: n={int(K['F3_R']['n_trades'])} net={K['F3_R']['net_pts']:.1f} "
        f"PF={fmt_pf(K['F3_R']['pf'])} delay_med={fmt_min(K['F3_R']['median_entry_delay_min'])}"
    )
    print(
        f"KEY F3 delayed HOLD_T423: n={int(K['F3_H']['n_trades'])} net={K['F3_H']['net_pts']:.1f} "
        f"PF={fmt_pf(K['F3_H']['pf'])}"
    )
    if base_r:
        print(f"BASE F3@0935 HI_T423: n={base_r['n']} net={base_r['net']:.1f} PF={fmt_pf(base_r['PF'])}")
    if base_h:
        print(f"BASE F3@0935 HOLD_T423: n={base_h['n']} net={base_h['net']:.1f} PF={fmt_pf(base_h['PF'])}")
    print(
        f"KEY ALL delayed HI_T423: n={int(K['ALL_R']['n_trades'])} net={K['ALL_R']['net_pts']:.1f} "
        f"PF={fmt_pf(K['ALL_R']['pf'])} delay_med={fmt_min(K['ALL_R']['median_entry_delay_min'])}"
    )


if __name__ == "__main__":
    main()
