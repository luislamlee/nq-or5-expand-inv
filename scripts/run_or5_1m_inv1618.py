#!/usr/bin/env python3
"""OR5_1m_INV1618 — pre-entry long fib 1.618 invalidation filter.

Luis rule: if price has gone long ABOVE fib 1.618 (from original OR5), the OR
short is fully invalidated that day — skip trade / cancel pending entry.

Fib (Luis language):
  OR range = 1.0; OR_H = 1.0 from OR_L.
  Long extension 1.618 = OR_L + 1.618*R = OR_H + 0.618*R.
  Same as expansion_level(or_h, or_l, 1.618, "up") / measured from ORIGINAL OR5
  (not the expanding range).

Invalidation trigger (pre-entry only):
  Prefer 1m HIGH pierce: if any 1m high ≥ inv_1618 before short fill → skip day.
  (Documented: high pierce, not close.)

Entry modes (F3; ALL expand optional):
  1) @0935 immediate (1m open after OR5) — INV almost never fires (OR5 max=OR_H
     < 1.618; entry is immediate). Still run ±INV for completeness.
  2) FIXED break: 1m close < OR_L → next open
  3) EXPAND break: inside closes expand; short close < cur_L → next open

TM: RATCHET_HI + HOLD_VWAP × T2.618/3.33/4.23. Cost 0.50.
Same data/paths as run_or5_1m_expand_break.py / run_or5_1m_break_below.py.

Outputs prefix OR5_1m_INV1618 under /workspace/sierra/candle_force/.
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

OUT_SPEC = OUT_DIR / "OR5_1m_INV1618.md"
OUT_TRADES = OUT_DIR / "OR5_1m_INV1618_trades.csv"
OUT_SUMMARY = OUT_DIR / "OR5_1m_INV1618_summary.csv"
OUT_TABLE = OUT_DIR / "OR5_1m_INV1618_compare.csv"
OUT_EQUITY = OUT_DIR / "OR5_1m_INV1618_equity.png"
OUT_ES = OUT_DIR / "OR5_1m_INV1618_RESUMEN_ES.md"

COST = 0.50
STOP_BUF = 2.0
EPS = 1e-9
IS_LAST = "2026-06-10"
SHORT_CUT = -5.0
FIB_LEVELS = (2.618, 3.33, 4.23)
POST_OR_M1 = 6
INV_K = 1.618  # long fib invalidation level


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


def long_fib_px(or_l: float, r: float, k: float) -> float:
    """Long fib from OR_L: OR_L + k*R (= OR_H + (k-1)*R). Luis / expansion_level up."""
    return or_l + k * r


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
    return (float(or5["high"]) - float(or5["low"])) > EPS


def collect_days(bars5: pd.DataFrame, bars1: pd.DataFrame, *, universe: str):
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
        if d1.empty or int(d1.iloc[0]["m1_bar"]) != 1:
            continue
        post = d1[d1["m1_bar"] >= POST_OR_M1].reset_index(drop=True)
        if post.empty:
            continue
        n_signal += 1
        out.append((str(or5["contract"]), day_s, or5, post))
    return out, n_signal


# ---------------------------------------------------------------------------
# Shared TM loop helpers
# ---------------------------------------------------------------------------

def _trade_result(
    *,
    or5,
    g,
    or_h,
    or_l,
    or_mid,
    r,
    max_fib,
    target,
    stop0,
    cur_stop,
    score0,
    entry_px,
    entry_m1,
    entry_time,
    exit_m1,
    exit_px,
    exit_reason,
    hold_fail,
    hold_vwap,
    n_hold_bars,
    hit_fib,
    mfe,
    mae,
    cost,
    variant,
    universe,
    entry_mode,
    inv_on,
    inv_level,
    inv_hit_m1,
    inv_hit_time,
    inv_hit_hi,
    confirm_m1=None,
    confirm_time=None,
    confirm_close=None,
    confirm_cur_h=None,
    confirm_cur_l=None,
    n_expansions=None,
    n_upside_outsides=None,
    tf: str,
) -> dict:
    gross = float(entry_px) - float(exit_px)
    net = gross - cost
    exit_time = str(g.loc[g["m1_bar"] == exit_m1, "time_et"].iloc[0])
    bars_in_trade = int(exit_m1 - entry_m1 + 1)
    entry_delay_min = int(entry_m1 - POST_OR_M1)
    confirm_delay_min = int(confirm_m1 - POST_OR_M1) if confirm_m1 is not None else None

    row = {
        "contract": str(or5["contract"]),
        "day": as_day_str(or5["trading_day"]),
        "side": "SHORT",
        "tf": tf,
        "entry_mode": entry_mode,
        "universe": universe,
        "inv_1618": int(inv_on),
        "inv_level": round(inv_level, 4),
        "inv_hit_m1": inv_hit_m1,
        "inv_hit_time": inv_hit_time,
        "inv_hit_hi": round(float(inv_hit_hi), 4) if inv_hit_hi is not None else None,
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
    if confirm_cur_h is not None:
        rng_h = float(confirm_cur_h)
        rng_l = float(confirm_cur_l)
        final_width = rng_h - rng_l
        row["confirm_cur_h"] = round(rng_h, 4)
        row["confirm_cur_l"] = round(rng_l, 4)
        row["final_range_width"] = round(final_width, 4)
        row["width_vs_R"] = round(final_width / r, 4) if r > EPS else float("nan")
        row["n_expansions"] = int(n_expansions) if n_expansions is not None else 0
        row["n_upside_outsides"] = int(n_upside_outsides) if n_upside_outsides is not None else 0
    return row


def _manage_bar(
    *,
    entry_px,
    lo,
    hi,
    c,
    target,
    max_fib,
    cur_stop,
    stop0,
    hold_vwap,
    dvwap,
    dval,
    n_hold_bars,
    hit_fib,
    fibs,
    mfe,
    mae,
):
    """Return (done, exit_m1_flag_via_reason, exit_px, exit_reason, hold_fail, n_hold, mfe, mae, hit_fib)."""
    mfe = max(mfe, entry_px - lo)
    mae = max(mae, hi - entry_px)
    for k, px in fibs.items():
        if lo <= px + EPS:
            hit_fib[k] = True

    if lo <= target + EPS:
        return True, float(target), f"target_{max_fib:g}", None, n_hold_bars, mfe, mae, hit_fib

    if c >= cur_stop - EPS:
        reason = "ratchet_stop" if cur_stop < stop0 - EPS else "init_stop"
        return True, c, reason, None, n_hold_bars, mfe, mae, hit_fib

    hold_fail = None
    if hold_vwap:
        ok, why = chk_vwap(c, dvwap, dval, float("nan"), float("nan"))
        if ok:
            n_hold_bars += 1
        else:
            return True, c, f"hold_exit_{why}", why, n_hold_bars, mfe, mae, hit_fib

    return False, None, None, hold_fail, n_hold_bars, mfe, mae, hit_fib


# ---------------------------------------------------------------------------
# Simulators — return (trade_dict | None, skip_reason | None)
# skip_reason: 'inv_1618' | 'no_entry' | 'last_bar' | None
# ---------------------------------------------------------------------------

def simulate_0935(
    or5,
    g1: pd.DataFrame,
    *,
    max_fib: float,
    hold_vwap: bool,
    inv_on: bool,
    cost: float = COST,
    variant: str,
    universe: str,
) -> tuple[dict | None, str | None]:
    """Immediate short at first 1m open after OR5 (09:35). INV rarely applies."""
    g = g1.sort_values("m1_bar").reset_index(drop=True)
    if len(g) < 1:
        return None, "no_entry"

    or_h = float(or5["high"])
    or_l = float(or5["low"])
    r = or_h - or_l
    if r <= 0:
        return None, "no_entry"
    or_mid = 0.5 * (or_h + or_l)
    inv_level = long_fib_px(or_l, r, INV_K)
    target = fib_px(or_h, r, max_fib)
    fibs = {k: fib_px(or_h, r, k) for k in FIB_LEVELS}
    stop0 = or_h + STOP_BUF
    cur_stop = stop0
    score0 = feat(or5, "score_v2")

    # Pre-entry INV: OR5 bars can't pierce 1.618 (max=OR_H). Still check post bars
    # before fill — but fill is bar 0 open, so nothing to check. Keep inv fields empty.
    if inv_on:
        # Theoretical: if somehow first post bar open is already after a pierce —
        # we have no prior post-OR bars. No skip.
        pass

    entry_row = g.iloc[0]
    entry_px = float(entry_row["open"])
    entry_m1 = int(entry_row["m1_bar"])
    entry_time = str(entry_row["time_et"])

    completed_5m_hi: dict[int, float] = {}
    buck_m5: int | None = None
    buck_hi = float("-inf")

    exit_m1 = exit_px = exit_reason = None
    hold_fail = None
    n_hold_bars = 0
    hit_fib = {k: False for k in FIB_LEVELS}
    mfe = mae = 0.0

    for i in range(len(g)):
        row = g.iloc[i]
        n1 = int(row["m1_bar"])
        m5 = m5_of(n1)
        hi = float(row["high"])
        lo = float(row["low"])
        c = float(row["close"])
        dvwap = feat(row, "dvwap")
        dval = feat(row, "dval")

        if buck_m5 is None or m5 != buck_m5:
            buck_m5 = m5
            buck_hi = hi
        else:
            buck_hi = max(buck_hi, hi)

        done, epx, ereason, hfail, n_hold_bars, mfe, mae, hit_fib = _manage_bar(
            entry_px=entry_px,
            lo=lo,
            hi=hi,
            c=c,
            target=target,
            max_fib=max_fib,
            cur_stop=cur_stop,
            stop0=stop0,
            hold_vwap=hold_vwap,
            dvwap=dvwap,
            dval=dval,
            n_hold_bars=n_hold_bars,
            hit_fib=hit_fib,
            fibs=fibs,
            mfe=mfe,
            mae=mae,
        )
        if done:
            exit_m1, exit_px, exit_reason, hold_fail = n1, epx, ereason, hfail
            break

        if n1 % 5 == 0:
            completed_5m_hi[m5] = buck_hi
            if m5 >= 3:
                prev_hi = completed_5m_hi.get(m5 - 1)
                if prev_hi is not None and prev_hi < cur_stop - EPS:
                    cur_stop = prev_hi

        if i == len(g) - 1:
            exit_m1, exit_px, exit_reason = n1, c, "eod"
            break

    if exit_m1 is None:
        return None, "no_entry"

    return (
        _trade_result(
            or5=or5,
            g=g,
            or_h=or_h,
            or_l=or_l,
            or_mid=or_mid,
            r=r,
            max_fib=max_fib,
            target=target,
            stop0=stop0,
            cur_stop=cur_stop,
            score0=score0,
            entry_px=entry_px,
            entry_m1=entry_m1,
            entry_time=entry_time,
            exit_m1=exit_m1,
            exit_px=exit_px,
            exit_reason=exit_reason,
            hold_fail=hold_fail,
            hold_vwap=hold_vwap,
            n_hold_bars=n_hold_bars,
            hit_fib=hit_fib,
            mfe=mfe,
            mae=mae,
            cost=cost,
            variant=variant,
            universe=universe,
            entry_mode="0935",
            inv_on=inv_on,
            inv_level=inv_level,
            inv_hit_m1=None,
            inv_hit_time=None,
            inv_hit_hi=None,
            tf="1m_confirm_5mHI",
        ),
        None,
    )


def simulate_fixed(
    or5,
    g1: pd.DataFrame,
    *,
    max_fib: float,
    hold_vwap: bool,
    inv_on: bool,
    cost: float = COST,
    variant: str,
    universe: str,
) -> tuple[dict | None, str | None]:
    """FIXED: 1m close < OR_L → next open. INV = 1m high ≥ 1.618 before fill."""
    g = g1.sort_values("m1_bar").reset_index(drop=True)
    if len(g) < 2:
        return None, "no_entry"

    or_h = float(or5["high"])
    or_l = float(or5["low"])
    r = or_h - or_l
    if r <= 0:
        return None, "no_entry"
    or_mid = 0.5 * (or_h + or_l)
    inv_level = long_fib_px(or_l, r, INV_K)
    target = fib_px(or_h, r, max_fib)
    fibs = {k: fib_px(or_h, r, k) for k in FIB_LEVELS}
    stop0 = or_h + STOP_BUF
    cur_stop = stop0
    score0 = feat(or5, "score_v2")

    completed_5m_hi: dict[int, float] = {}
    buck_m5: int | None = None
    buck_hi = float("-inf")

    pending_entry = False
    confirm_m1 = confirm_time = confirm_close = None
    in_trade = False
    entry_px = entry_m1 = entry_time = None
    exit_m1 = exit_px = exit_reason = None
    hold_fail = None
    n_hold_bars = 0
    hit_fib = {k: False for k in FIB_LEVELS}
    mfe = mae = 0.0
    inv_hit_m1 = inv_hit_time = inv_hit_hi = None

    def update_bucket_and_ratchet(n1, m5, hi):
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
                # fall through to manage
            else:
                # Pre-entry INV: 1m high pierce of long 1.618 → day invalidated
                if inv_on and hi >= inv_level - EPS:
                    inv_hit_m1, inv_hit_time, inv_hit_hi = n1, t_et, hi
                    return None, "inv_1618"

                update_bucket_and_ratchet(n1, m5, hi)
                if c < or_l - EPS:
                    if i == len(g) - 1:
                        return None, "last_bar"
                    pending_entry = True
                    confirm_m1, confirm_time, confirm_close = n1, t_et, c
                continue

        done, epx, ereason, hfail, n_hold_bars, mfe, mae, hit_fib = _manage_bar(
            entry_px=entry_px,
            lo=lo,
            hi=hi,
            c=c,
            target=target,
            max_fib=max_fib,
            cur_stop=cur_stop,
            stop0=stop0,
            hold_vwap=hold_vwap,
            dvwap=dvwap,
            dval=dval,
            n_hold_bars=n_hold_bars,
            hit_fib=hit_fib,
            fibs=fibs,
            mfe=mfe,
            mae=mae,
        )
        if done:
            exit_m1, exit_px, exit_reason, hold_fail = n1, epx, ereason, hfail
            break

        update_bucket_and_ratchet(n1, m5, hi)

        if i == len(g) - 1:
            exit_m1, exit_px, exit_reason = n1, c, "eod"
            break

    if exit_m1 is None or entry_px is None:
        return None, "no_entry"

    return (
        _trade_result(
            or5=or5,
            g=g,
            or_h=or_h,
            or_l=or_l,
            or_mid=or_mid,
            r=r,
            max_fib=max_fib,
            target=target,
            stop0=stop0,
            cur_stop=cur_stop,
            score0=score0,
            entry_px=entry_px,
            entry_m1=entry_m1,
            entry_time=entry_time,
            exit_m1=exit_m1,
            exit_px=exit_px,
            exit_reason=exit_reason,
            hold_fail=hold_fail,
            hold_vwap=hold_vwap,
            n_hold_bars=n_hold_bars,
            hit_fib=hit_fib,
            mfe=mfe,
            mae=mae,
            cost=cost,
            variant=variant,
            universe=universe,
            entry_mode="fixed_break",
            inv_on=inv_on,
            inv_level=inv_level,
            inv_hit_m1=inv_hit_m1,
            inv_hit_time=inv_hit_time,
            inv_hit_hi=inv_hit_hi,
            confirm_m1=confirm_m1,
            confirm_time=confirm_time,
            confirm_close=confirm_close,
            tf="1m_break_below",
        ),
        None,
    )


def simulate_expand(
    or5,
    g1: pd.DataFrame,
    *,
    max_fib: float,
    hold_vwap: bool,
    inv_on: bool,
    cost: float = COST,
    variant: str,
    universe: str,
) -> tuple[dict | None, str | None]:
    """EXPAND: inside→expand; close<cur_L→next open. INV uses ORIGINAL OR5 1.618."""
    g = g1.sort_values("m1_bar").reset_index(drop=True)
    if len(g) < 2:
        return None, "no_entry"

    or_h = float(or5["high"])
    or_l = float(or5["low"])
    r = or_h - or_l
    if r <= 0:
        return None, "no_entry"
    or_mid = 0.5 * (or_h + or_l)
    inv_level = long_fib_px(or_l, r, INV_K)  # ORIGINAL OR5 — not expanded
    target = fib_px(or_h, r, max_fib)
    fibs = {k: fib_px(or_h, r, k) for k in FIB_LEVELS}
    stop0 = or_h + STOP_BUF
    cur_stop = stop0
    score0 = feat(or5, "score_v2")

    cur_h, cur_l = or_h, or_l
    n_expansions = 0
    n_upside_outsides = 0

    completed_5m_hi: dict[int, float] = {}
    buck_m5: int | None = None
    buck_hi = float("-inf")

    pending_entry = False
    confirm_m1 = confirm_time = confirm_close = None
    confirm_cur_h = confirm_cur_l = confirm_n_exp = None
    in_trade = False
    entry_px = entry_m1 = entry_time = None
    exit_m1 = exit_px = exit_reason = None
    hold_fail = None
    n_hold_bars = 0
    hit_fib = {k: False for k in FIB_LEVELS}
    mfe = mae = 0.0
    inv_hit_m1 = inv_hit_time = inv_hit_hi = None

    def update_bucket_and_ratchet(n1, m5, hi):
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
            else:
                # INV first: 1m high ≥ original-OR5 long 1.618 → day dead for short
                if inv_on and hi >= inv_level - EPS:
                    inv_hit_m1, inv_hit_time, inv_hit_hi = n1, t_et, hi
                    return None, "inv_1618"

                update_bucket_and_ratchet(n1, m5, hi)
                outside_below = c < cur_l - EPS
                outside_above = c > cur_h + EPS

                if outside_below:
                    if i == len(g) - 1:
                        return None, "last_bar"
                    pending_entry = True
                    confirm_m1, confirm_time, confirm_close = n1, t_et, c
                    confirm_cur_h, confirm_cur_l = cur_h, cur_l
                    confirm_n_exp = n_expansions
                elif outside_above:
                    n_upside_outsides += 1
                else:
                    new_h = max(cur_h, hi)
                    new_l = min(cur_l, lo)
                    if new_h > cur_h + EPS or new_l < cur_l - EPS:
                        n_expansions += 1
                    cur_h, cur_l = new_h, new_l
                continue

        done, epx, ereason, hfail, n_hold_bars, mfe, mae, hit_fib = _manage_bar(
            entry_px=entry_px,
            lo=lo,
            hi=hi,
            c=c,
            target=target,
            max_fib=max_fib,
            cur_stop=cur_stop,
            stop0=stop0,
            hold_vwap=hold_vwap,
            dvwap=dvwap,
            dval=dval,
            n_hold_bars=n_hold_bars,
            hit_fib=hit_fib,
            fibs=fibs,
            mfe=mfe,
            mae=mae,
        )
        if done:
            exit_m1, exit_px, exit_reason, hold_fail = n1, epx, ereason, hfail
            break

        update_bucket_and_ratchet(n1, m5, hi)

        if i == len(g) - 1:
            exit_m1, exit_px, exit_reason = n1, c, "eod"
            break

    if exit_m1 is None or entry_px is None:
        return None, "no_entry"

    return (
        _trade_result(
            or5=or5,
            g=g,
            or_h=or_h,
            or_l=or_l,
            or_mid=or_mid,
            r=r,
            max_fib=max_fib,
            target=target,
            stop0=stop0,
            cur_stop=cur_stop,
            score0=score0,
            entry_px=entry_px,
            entry_m1=entry_m1,
            entry_time=entry_time,
            exit_m1=exit_m1,
            exit_px=exit_px,
            exit_reason=exit_reason,
            hold_fail=hold_fail,
            hold_vwap=hold_vwap,
            n_hold_bars=n_hold_bars,
            hit_fib=hit_fib,
            mfe=mfe,
            mae=mae,
            cost=cost,
            variant=variant,
            universe=universe,
            entry_mode="expand_break",
            inv_on=inv_on,
            inv_level=inv_level,
            inv_hit_m1=inv_hit_m1,
            inv_hit_time=inv_hit_time,
            inv_hit_hi=inv_hit_hi,
            confirm_m1=confirm_m1,
            confirm_time=confirm_time,
            confirm_close=confirm_close,
            confirm_cur_h=confirm_cur_h,
            confirm_cur_l=confirm_cur_l,
            n_expansions=confirm_n_exp if confirm_n_exp is not None else n_expansions,
            n_upside_outsides=n_upside_outsides,
            tf="1m_expand_break",
        ),
        None,
    )


SIMULATORS = {
    "0935": simulate_0935,
    "fixed": simulate_fixed,
    "expand": simulate_expand,
}


def run_variant(days, sim_fn, **kw) -> tuple[pd.DataFrame, int, int]:
    """Returns (trades_df, n_inv_skipped, n_other_no_trade)."""
    rows = []
    n_inv = 0
    n_other = 0
    for _c, _d, or5, g1 in days:
        t, reason = sim_fn(or5, g1, **kw)
        if t is not None:
            rows.append(t)
        elif reason == "inv_1618":
            n_inv += 1
        else:
            n_other += 1
    return pd.DataFrame(rows), n_inv, n_other


def enrich_metrics(m: dict, part: pd.DataFrame) -> dict:
    extra_nan = (
        "pct_win",
        "pct_loss",
        "avg_net_per_day",
        "largest_win",
        "largest_loss",
        "mean_entry_delay_min",
        "median_entry_delay_min",
    )
    if part is None or len(part) == 0:
        for k in extra_nan:
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
    return m


def split_rows(
    variant: str,
    tdf: pd.DataFrame,
    *,
    universe: str,
    entry_mode: str,
    inv_on: bool,
    n_signal: int,
    n_inv: int,
    tf: str,
) -> list[dict]:
    out = []
    is_df, oos_df = apply_date_split(tdf)
    for label, part in (("IS", is_df), ("OOS", oos_df), ("ALL", tdf)):
        m = metrics(part, label)
        m["variant"] = variant
        m["universe"] = universe
        m["entry_mode"] = entry_mode
        m["inv_1618"] = int(inv_on)
        m["split"] = label
        m["tf"] = tf
        m["n_signal_days"] = int(n_signal) if label == "ALL" else float("nan")
        m["n_inv_skipped"] = int(n_inv) if label == "ALL" else float("nan")
        if label == "ALL" and n_signal > 0:
            m["pct_inv_skipped"] = float(n_inv / n_signal)
        else:
            m["pct_inv_skipped"] = float("nan")
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
            if label == "ALL" and n_signal > 0:
                m["pct_signal_no_entry"] = 1.0
                m["n_signal_no_entry"] = int(n_signal)
            else:
                m["pct_signal_no_entry"] = float("nan")
                m["n_signal_no_entry"] = 0
        m = enrich_metrics(m, part)
        out.append(m)
    return out


def build_specs() -> list[dict]:
    """F3: all 3 modes × ±INV × HOLD/HI × 3 fibs.
    ALL: expand ±INV × HOLD/HI × T423 only (brief).
    """
    specs = []
    mode_meta = [
        ("0935", "0935", "1m_confirm_5mHI"),
        ("FIXED", "fixed", "1m_break_below"),
        ("EXPAND", "expand", "1m_expand_break"),
    ]
    for uni in ("F3",):
        for mode_lab, mode_key, tf in mode_meta:
            for inv in (False, True):
                inv_lab = "_INV1618" if inv else ""
                for hold in (True, False):
                    tm = "HOLD_VWAP" if hold else "RATCHET_HI"
                    for fib in FIB_LEVELS:
                        name = f"{uni}_{mode_lab}{inv_lab}_{tm}_T{fib_tag(fib)}"
                        specs.append(
                            dict(
                                name=name,
                                universe=uni,
                                mode_key=mode_key,
                                mode_lab=mode_lab,
                                tf=tf,
                                inv_on=inv,
                                max_fib=fib,
                                hold_vwap=hold,
                            )
                        )
    # ALL expand ±INV T423 only
    for inv in (False, True):
        inv_lab = "_INV1618" if inv else ""
        for hold in (True, False):
            tm = "HOLD_VWAP" if hold else "RATCHET_HI"
            name = f"ALL_EXPAND{inv_lab}_{tm}_T423"
            specs.append(
                dict(
                    name=name,
                    universe="ALL",
                    mode_key="expand",
                    mode_lab="EXPAND",
                    tf="1m_expand_break",
                    inv_on=inv,
                    max_fib=4.23,
                    hold_vwap=hold,
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


def fmt_n(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return str(int(x))


def _safe_round(v, nd=4):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return round(float(v), nd)


def cmp_row(r: dict) -> dict:
    return {
        "variante": r["variant"],
        "split": r["split"],
        "tf": r["tf"],
        "universe": r["universe"],
        "entry_mode": r["entry_mode"],
        "inv_1618": r["inv_1618"],
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
        "n_inv_skipped": r.get("n_inv_skipped"),
        "pct_inv_skipped": _safe_round(r.get("pct_inv_skipped"), 4),
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
        "pct_signal_no_entry": _safe_round(r.get("pct_signal_no_entry"), 4),
        "n_signal_days": r.get("n_signal_days"),
        "n_signal_no_entry": r.get("n_signal_no_entry"),
    }


def plot_equity(trade_map: dict[str, pd.DataFrame], keys: list[str], out_path: Path):
    fig, ax = plt.subplots(figsize=(13, 6.5))
    style = {
        "F3_EXPAND_RATCHET_HI_T423": ("#ff7f0e", "--", 1.6),
        "F3_EXPAND_INV1618_RATCHET_HI_T423": ("#d62728", "-", 2.2),
        "F3_EXPAND_HOLD_VWAP_T423": ("#1f77b4", "--", 1.4),
        "F3_EXPAND_INV1618_HOLD_VWAP_T423": ("#9467bd", "-", 2.0),
        "F3_FIXED_RATCHET_HI_T423": ("#2ca02c", "--", 1.4),
        "F3_FIXED_INV1618_RATCHET_HI_T423": ("#8c564b", "-", 2.0),
        "F3_0935_RATCHET_HI_T423": ("#7f7f7f", ":", 1.2),
        "F3_0935_INV1618_RATCHET_HI_T423": ("#17becf", "-", 1.6),
    }
    for v in keys:
        tdf = trade_map.get(v)
        if tdf is None or tdf.empty:
            continue
        t = tdf.sort_values(["day", "entry_m1"]).reset_index(drop=True)
        eq = t["net"].cumsum()
        color, ls, lw = style.get(v, ("#bbbbbb", ":", 0.9))
        ax.plot(eq.index + 1, eq.values, label=v, lw=lw, color=color, ls=ls)
    ax.axhline(0, color="#888", lw=0.8)
    ax.set_title("OR5 1m INV1618 — F3 entry modes ± long-1.618 invalidation (T423)")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Net pts acumulados")
    ax.legend(fontsize=7, loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


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

    specs = build_specs()
    trade_map: dict[str, pd.DataFrame] = {}
    summary_rows: list[dict] = []
    all_trades = []
    inv_counts: dict[str, int] = {}
    order: list[str] = []

    for sp in specs:
        name = sp["name"]
        sim_fn = SIMULATORS[sp["mode_key"]]
        print(f"Running {name}…")
        tdf, n_inv, n_other = run_variant(
            days_by_u[sp["universe"]],
            sim_fn,
            max_fib=sp["max_fib"],
            hold_vwap=sp["hold_vwap"],
            inv_on=sp["inv_on"],
            variant=name,
            universe=sp["universe"],
        )
        trade_map[name] = tdf
        all_trades.append(tdf)
        order.append(name)
        inv_counts[name] = n_inv
        summary_rows.extend(
            split_rows(
                name,
                tdf,
                universe=sp["universe"],
                entry_mode=sp["mode_lab"],
                inv_on=sp["inv_on"],
                n_signal=n_signal_by_u[sp["universe"]],
                n_inv=n_inv,
                tf=sp["tf"],
            )
        )
        m_all = next(r for r in summary_rows if r["variant"] == name and r["split"] == "ALL")
        print(
            f"  ALL n={m_all['n_trades']}/{n_signal_by_u[sp['universe']]} "
            f"inv_skip={n_inv} other_skip={n_other} "
            f"WR={fmt_pct(m_all['win_rate'])} net={m_all['net_pts']:.1f} "
            f"PF={fmt_pf(m_all['pf'])} avg/día={fmt_net(m_all['avg_net_per_day'])} "
            f"Lwin={fmt_net(m_all['largest_win'])}@{m_all.get('largest_win_day','')} "
            f"Lloss={fmt_net(m_all['largest_loss'])}@{m_all.get('largest_loss_day','')}"
        )

    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    trades_df.to_csv(OUT_TRADES, index=False)
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(OUT_SUMMARY, index=False)

    by = {(r["variant"], r["split"]): r for r in summary_rows}
    cmp_df = pd.DataFrame([cmp_row(r) for r in summary_rows])
    cmp_df.to_csv(OUT_TABLE, index=False)

    eq_keys = [
        "F3_EXPAND_RATCHET_HI_T423",
        "F3_EXPAND_INV1618_RATCHET_HI_T423",
        "F3_EXPAND_HOLD_VWAP_T423",
        "F3_EXPAND_INV1618_HOLD_VWAP_T423",
        "F3_FIXED_RATCHET_HI_T423",
        "F3_FIXED_INV1618_RATCHET_HI_T423",
        "F3_0935_RATCHET_HI_T423",
        "F3_0935_INV1618_RATCHET_HI_T423",
    ]
    plot_equity(trade_map, eq_keys, OUT_EQUITY)

    def get(v, sp="ALL"):
        return by[(v, sp)]

    # Headline T423 pairs
    pairs = [
        ("EXPAND", "HOLD_VWAP"),
        ("EXPAND", "RATCHET_HI"),
        ("FIXED", "HOLD_VWAP"),
        ("FIXED", "RATCHET_HI"),
        ("0935", "HOLD_VWAP"),
        ("0935", "RATCHET_HI"),
    ]

    def pair_names(mode, tm):
        base = f"F3_{mode}_{tm}_T423"
        inv = f"F3_{mode}_INV1618_{tm}_T423"
        return base, inv

    table_lines = []
    for mode, tm in pairs:
        base, inv = pair_names(mode, tm)
        for label, key in ((f"F3 {mode} {tm} T423", base), (f"F3 {mode}+INV1618 {tm} T423", inv)):
            r = get(key)
            table_lines.append(
                f"| {label} | {int(r['n_trades'])} | {fmt_pct(r['win_rate'])} | "
                f"{fmt_net(r['net_pts'])} | {fmt_pf(r['pf'])} | {fmt_net(r['avg_net_per_day'])} | "
                f"{fmt_net(r['largest_win'])} ({r.get('largest_win_day','')}) | "
                f"{fmt_net(r['largest_loss'])} ({r.get('largest_loss_day','')}) | "
                f"{fmt_n(r.get('n_inv_skipped'))} | {fmt_pct(r.get('pct_inv_skipped'))} |"
            )
    # ALL expand brief
    for inv_lab, key in (
        ("", "ALL_EXPAND_HOLD_VWAP_T423"),
        ("+INV1618 ", "ALL_EXPAND_INV1618_HOLD_VWAP_T423"),
        ("", "ALL_EXPAND_RATCHET_HI_T423"),
        ("+INV1618 ", "ALL_EXPAND_INV1618_RATCHET_HI_T423"),
    ):
        tm = "HOLD_VWAP" if "HOLD" in key else "RATCHET_HI"
        r = get(key)
        table_lines.append(
            f"| ALL EXPAND{inv_lab}{tm} T423 | {int(r['n_trades'])} | {fmt_pct(r['win_rate'])} | "
            f"{fmt_net(r['net_pts'])} | {fmt_pf(r['pf'])} | {fmt_net(r['avg_net_per_day'])} | "
            f"{fmt_net(r['largest_win'])} ({r.get('largest_win_day','')}) | "
            f"{fmt_net(r['largest_loss'])} ({r.get('largest_loss_day','')}) | "
            f"{fmt_n(r.get('n_inv_skipped'))} | {fmt_pct(r.get('pct_inv_skipped'))} |"
        )
    table_block = "\n".join(table_lines)

    # Delta rows for Spanish table
    delta_lines = []
    for mode, tm in pairs:
        base, inv = pair_names(mode, tm)
        b, i = get(base), get(inv)
        d_n = int(i["n_trades"]) - int(b["n_trades"])
        d_net = i["net_pts"] - b["net_pts"]
        d_wr = (i["win_rate"] - b["win_rate"]) if (
            not math.isnan(i["win_rate"]) and not math.isnan(b["win_rate"])
        ) else float("nan")
        d_day = (i["avg_net_per_day"] - b["avg_net_per_day"]) if (
            not math.isnan(i.get("avg_net_per_day", float("nan")))
            and not math.isnan(b.get("avg_net_per_day", float("nan")))
        ) else float("nan")
        delta_lines.append(
            f"| {mode} {tm} | {int(b['n_trades'])}→{int(i['n_trades'])} ({d_n:+d}) | "
            f"{fmt_pct(b['win_rate'])}→{fmt_pct(i['win_rate'])} ({fmt_pct(d_wr) if not (isinstance(d_wr, float) and math.isnan(d_wr)) else 'n/a'}) | "
            f"{fmt_net(b['net_pts'])}→{fmt_net(i['net_pts'])} ({d_net:+.1f}) | "
            f"{fmt_net(b['avg_net_per_day'])}→{fmt_net(i['avg_net_per_day'])} ({d_day:+.1f}) | "
            f"{fmt_n(i.get('n_inv_skipped'))} |"
        )
    delta_block = "\n".join(delta_lines)

    # Sanity
    n_0935_inv = inv_counts.get("F3_0935_INV1618_RATCHET_HI_T423", 0)
    assert n_0935_inv == 0, f"@0935 INV should be 0, got {n_0935_inv}"

    # Verify INV trades subset of non-INV for FIXED T423 HI
    t_fix = trade_map["F3_FIXED_RATCHET_HI_T423"]
    t_fix_inv = trade_map["F3_FIXED_INV1618_RATCHET_HI_T423"]
    if len(t_fix_inv) and len(t_fix):
        days_inv = set(t_fix_inv["day"])
        days_base = set(t_fix["day"])
        assert days_inv.issubset(days_base), "INV trades must be subset of base days"

    md = f"""# OR5_1m_INV1618 — Pre-entry long fib 1.618 invalidation

**Status:** [EXPLORATORY] research only · no live trading  
**Date:** 2026-09-29 (America/Bogota)  
**Data:** same as `run_or5_1m_expand_break.py` / `run_or5_1m_break_below.py`  
**IS/OOS:** IS ≤ {IS_LAST} · OOS ≥ 2026-06-11 · range ~2025-12-16..2026-08-25  
**Cost:** {COST} pts RT · **STOP_BUF:** {STOP_BUF}

---

## Luis rule

If price has gone **long above fib 1.618** (from **original OR5**), the OR short is
**fully invalidated** that day — no short / cancel pending entry.

### Fib (Luis language)
- OR range = 1.0; OR_H = 1.0 from OR_L.
- Long extension 1.618 = `OR_L + 1.618·R` = `OR_H + 0.618·R`.
- Helper: `long_fib_px(or_l, r, 1.618)` ≡ `expansion_level(..., 1.618, "up")`.
- Measured from **original OR5** (not expanding range).

### Trigger
- **Pre-entry only** (not mid-trade).
- Prefer **1m HIGH pierce**: any post-OR 1m bar with `high ≥ inv_1618` before fill → skip.
- @0935: entry is immediate at 09:35 open; OR5 max high = OR_H < 1.618, so INV never fires
  (n_inv_skipped=**{n_0935_inv}**). Documented.

## Headline compare — T423 HI & HOLD (ALL split)

| Setup | n | %W | Net | PF | pts/día | Lwin (fecha) | Lloss (fecha) | n INV skip | % INV skip |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|
{table_block}

## Δ with vs without INV (F3 T423)

| Mode TM | n | %W | Net | pts/día | n INV skip |
|---|---|---|---|---|---:|
{delta_block}

## Notes
- Filter stacks on top of existing entry modes; TM unchanged (RATCHET_HI / HOLD_VWAP).
- Full matrix also includes T2.618 / T3.33 for F3 modes (see `_summary.csv` / `_compare.csv`).
- ALL-days EXPAND ±INV T423 included briefly above.

## Outputs
- Script: `run_or5_1m_inv1618.py`
- Trades / summary / compare: `OR5_1m_INV1618_*.csv`
- Equity: `OR5_1m_INV1618_equity.png`
- Spanish: `OR5_1m_INV1618_RESUMEN_ES.md`

*Research exploratorio — no es señal live.*
"""
    OUT_SPEC.write_text(md, encoding="utf-8")

    # Spanish resumen
    es_rows = []
    for mode, tm in pairs:
        base, inv = pair_names(mode, tm)
        for tag, key in (("", base), ("+INV ", inv)):
            r = get(key)
            es_rows.append(
                f"| F3 {mode} {tag}{tm} | {int(r['n_trades'])} | {fmt_pct(r['win_rate'])} | "
                f"{fmt_net(r['net_pts'])} | {fmt_pf(r['pf'])} | {fmt_net(r['avg_net_per_day'])} | "
                f"{fmt_net(r['largest_win'])} ({r.get('largest_win_day','')}) | "
                f"{fmt_net(r['largest_loss'])} ({r.get('largest_loss_day','')}) | "
                f"{fmt_n(r.get('n_inv_skipped'))} |"
            )
    for key in (
        "ALL_EXPAND_HOLD_VWAP_T423",
        "ALL_EXPAND_INV1618_HOLD_VWAP_T423",
        "ALL_EXPAND_RATCHET_HI_T423",
        "ALL_EXPAND_INV1618_RATCHET_HI_T423",
    ):
        r = get(key)
        lab = key.replace("_T423", "").replace("ALL_", "ALL ").replace("_", " ")
        es_rows.append(
            f"| {lab} | {int(r['n_trades'])} | {fmt_pct(r['win_rate'])} | "
            f"{fmt_net(r['net_pts'])} | {fmt_pf(r['pf'])} | {fmt_net(r['avg_net_per_day'])} | "
            f"{fmt_net(r['largest_win'])} ({r.get('largest_win_day','')}) | "
            f"{fmt_net(r['largest_loss'])} ({r.get('largest_loss_day','')}) | "
            f"{fmt_n(r.get('n_inv_skipped'))} |"
        )
    es_table = "\n".join(es_rows)


    # Days filtered out of trades by INV (present in base, absent in INV)
    def skipped_trade_days(mode, tm):
        base, invn = pair_names(mode, tm)
        tb = trade_map.get(base, pd.DataFrame())
        ti = trade_map.get(invn, pd.DataFrame())
        if tb is None or tb.empty:
            return [], 0.0
        bdays = set(tb["day"].astype(str))
        idays = set(ti["day"].astype(str)) if ti is not None and len(ti) else set()
        sk = sorted(bdays - idays)
        nets = tb[tb["day"].astype(str).isin(sk)]["net"].astype(float)
        return sk, float(nets.sum()) if len(nets) else 0.0

    sk_exp, sum_exp = skipped_trade_days("EXPAND", "RATCHET_HI")
    sk_fix, sum_fix = skipped_trade_days("FIXED", "RATCHET_HI")
    sk_exp_s = ", ".join(sk_exp) if sk_exp else "(ninguno)"
    sk_fix_s = ", ".join(sk_fix) if sk_fix else "(ninguno)"

    narr = []
    for mode, tm in pairs:
        base, invn = pair_names(mode, tm)
        b, i = get(base), get(invn)
        narr.append(
            f"- **{mode} {tm}:** n {int(b['n_trades'])}→{int(i['n_trades'])} · "
            f"%W {fmt_pct(b['win_rate'])}→{fmt_pct(i['win_rate'])} · "
            f"net {fmt_net(b['net_pts'])}→{fmt_net(i['net_pts'])} (Δ {i['net_pts']-b['net_pts']:+.1f}) · "
            f"pts/día {fmt_net(b['avg_net_per_day'])}→{fmt_net(i['avg_net_per_day'])} · "
            f"INV skip={fmt_n(i.get('n_inv_skipped'))} · "
            f"Lwin {fmt_net(i['largest_win'])} ({i.get('largest_win_day','')}) · "
            f"Lloss {fmt_net(i['largest_loss'])} ({i.get('largest_loss_day','')})"
        )
    narr_block = "\n".join(narr)

    es = f"""# Resumen OR5 1m INV1618 — para Luis

**Regla:** si el precio ha ido **largo por encima del fib 1.618** (OR5 original), el short OR
queda **totalmente invalidado** ese día (no short / cancelar entrada pendiente).

**Fib:** 1.618 largo = `OR_L + 1.618·R` = `OR_H + 0.618·R` (mismo helper / OR5 original, no rango expandido).

**Trigger:** pierce de **high 1m ≥ 1.618** antes del fill (pre-entrada). @0935 casi nunca aplica
(OR5 max = OR_H < 1.618; entrada inmediata) — n_inv=**{n_0935_inv}**.

**Gestión:** RATCHET_HI y HOLD_VWAP × T4.23 (matriz 2.618/3.33/4.23 en CSV). Coste 0.50.

## Tabla T423 — EXPAND±INV, FIXED±INV, @0935±INV (HI y HOLD)

| Setup | n | %W | Net | PF | pts/día | Mejor (fecha) | Peor (fecha) | n skip INV |
|---|---:|---:|---:|---:|---:|---|---|---:|
{es_table}

## Δ con vs sin filtro INV

| Mode TM | n | %W | Net | pts/día | n INV skip |
|---|---|---|---|---|---:|
{delta_block}

## Respuesta directa

{narr_block}

### Días F3 con trade evitado por INV (T423 HI)
- **EXPAND:** {sk_exp_s} (suma net evitados = {sum_exp:+.1f}) · n_inv_skip total={fmt_n(get('F3_EXPAND_INV1618_RATCHET_HI_T423').get('n_inv_skipped'))}
- **FIXED:** {sk_fix_s} (suma net evitados = {sum_fix:+.1f}) · n_inv_skip total={fmt_n(get('F3_FIXED_INV1618_RATCHET_HI_T423').get('n_inv_skipped'))}
- **@0935:** ninguno (entrada inmediata; OR5 no puede tocar 1.618)

Señales F3: **{n_signal_by_u['F3']}** días · ALL: **{n_signal_by_u['ALL']}** días.

Detalle EN: `OR5_1m_INV1618.md` · equity: `OR5_1m_INV1618_equity.png`  
CSV: `OR5_1m_INV1618_trades.csv` / `_summary.csv` / `_compare.csv`

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
    for mode, tm in pairs:
        base, invn = pair_names(mode, tm)
        b, i = get(base), get(invn)
        print(
            f"  {mode} {tm}: base n={int(b['n_trades'])} net={b['net_pts']:.1f} | "
            f"INV n={int(i['n_trades'])} net={i['net_pts']:.1f} "
            f"Δnet={i['net_pts']-b['net_pts']:+.1f} skip={int(i.get('n_inv_skipped') or 0)}"
        )


if __name__ == "__main__":
    main()
