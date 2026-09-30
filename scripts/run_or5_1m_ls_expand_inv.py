#!/usr/bin/env python3
"""OR5_1m_LS_EXPAND_INV — Long + Short expanding OR with pre-entry 1.618 invalidation.

Shared OR mechanics (both sides):
  1. OR5 = 09:30–09:35 ET High/Low. R = OR_H − OR_L.
  2. Expanding range after OR5: inside close → expand cur_H/cur_L to bar H/L;
     outside close → breakout (no expand that bar).
  3. Short entry: 1m close < cur_L → fill next 1m open.
  4. Long entry:  1m close > cur_H → fill next 1m open.
  5. ≤1 trade/day (first valid breakout side wins). Skip last-bar confirm.
  6. Invalidation (pre-entry, ORIGINAL OR5):
       Short dead if 1m high ≥ long fib 1.618 = OR_L + 1.618·R
       Long  dead if 1m low  ≤ short fib 1.618 = fib_px(OR_H, R, 1.618) = OR_H − 1.618·R
  7. Fib targets from ORIGINAL OR5: short fib_px(k); long OR_L + k·R. k∈{2.618,3.33,4.23}

Signal filter:
  Short F3_0: OR5 red, score_v2≤−5, vbp_imbalance≤0 (also ALL).
  Long  L3_5: OR5 green, score_v2≥+5, vbp_imbalance≥0 (symmetry with −5; note ≥+8 hist).
  Combined book = F3 shorts ∪ L3 longs (no day overlap).

Management:
  RATCHET: short=prior 5m High; long=prior 5m Low. Exit confirm 1m close beyond stop.
  HOLD_VWAP: short exit close≥dVWAP; long exit close≤dVWAP.
  Init stop: short OR_H+2; long OR_L−2. Cost 0.50.

Outputs prefix OR5_1m_LS_EXPAND_INV under /workspace/sierra/candle_force/.
Reuses helpers from run_or5_1m_inv1618 / expand_break / laxo / longfix.
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

PREFIX = "OR5_1m_LS_EXPAND_INV"
OUT_SPEC = OUT_DIR / f"{PREFIX}.md"
OUT_TRADES = OUT_DIR / f"{PREFIX}_trades.csv"
OUT_SUMMARY = OUT_DIR / f"{PREFIX}_summary.csv"
OUT_TABLE = OUT_DIR / f"{PREFIX}_compare.csv"
OUT_EQUITY = OUT_DIR / f"{PREFIX}_equity.png"
OUT_ES = OUT_DIR / f"{PREFIX}_RESUMEN_ES.md"

COST = 0.50
STOP_BUF = 2.0
EPS = 1e-9
IS_LAST = "2026-06-10"
SHORT_CUT = -5.0
LONG_CUT = 5.0  # prefer ≥+5 for symmetry with −5 (hist ≥+8 / ≥+6 noted)
LONG_CUT_ALT = 8.0
FIB_LEVELS = (2.618, 3.33, 4.23)
POST_OR_M1 = 6
INV_K = 1.618


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
fib_px = laxo.fib_px  # short: or_h - k*r
chk_vwap = laxo.chk_vwap  # short HOLD: fail if close >= dvwap
is_or_red = laxo.is_or_red
is_green = laxo.is_green


def fib_tag(k: float) -> str:
    return {2.618: "2618", 3.33: "333", 4.23: "423"}[k]


def m5_of(m1: int) -> int:
    return (int(m1) - 1) // 5 + 1


def long_fib_px(or_l: float, r: float, k: float) -> float:
    """Long fib from OR_L: OR_L + k*R."""
    return or_l + k * r


def chk_vwap_long(close, dvwap, dval, cum_d, prev_cum):
    """Long HOLD: stay above dVWAP; fail if close ≤ dVWAP."""
    if math.isnan(dvwap):
        return False, "no_profile"
    if close <= dvwap + EPS:
        return False, "below_dvwap"
    return True, ""


def l3_candidate(or5, long_cut: float = LONG_CUT) -> bool:
    """Mirror F3_0: OR5 green, score_v2 ≥ cut, vbp_imbalance ≥ 0."""
    sc = feat(or5, "score_v2")
    if math.isnan(sc) or not is_green(or5) or sc < long_cut - EPS:
        return False
    imb = feat(or5, "vbp_imbalance")
    if math.isnan(imb):
        return False
    return imb >= 0.0 - EPS


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


def collect_days(bars5: pd.DataFrame, bars1: pd.DataFrame, *, universe: str, long_cut: float = LONG_CUT):
    """universe: F3 | L3 | ALL | BOTH (F3 or L3 days, tagged)."""
    out = []
    n_signal = 0
    or5_all = bars5[bars5["m5_bar"] == 1].copy()
    or5_all["trading_day"] = pd.to_datetime(or5_all["trading_day"]).dt.strftime("%Y-%m-%d")

    for _, or5 in or5_all.iterrows():
        if not valid_or5(or5):
            continue
        side_arm = None
        if universe == "F3":
            if not f3_0_candidate(or5):
                continue
            side_arm = "SHORT"
        elif universe == "L3":
            if not l3_candidate(or5, long_cut):
                continue
            side_arm = "LONG"
        elif universe == "BOTH":
            s_ok = f3_0_candidate(or5)
            l_ok = l3_candidate(or5, long_cut)
            if not s_ok and not l_ok:
                continue
            if s_ok and l_ok:
                side_arm = "BOTH"  # shouldn't happen with F3/L3
            elif s_ok:
                side_arm = "SHORT"
            else:
                side_arm = "LONG"
        elif universe == "ALL":
            side_arm = "BOTH"
        else:
            raise ValueError(universe)

        day_s = as_day_str(or5["trading_day"])
        d1 = bars1[bars1["trading_day"] == day_s].sort_values("m1_bar").reset_index(drop=True)
        if d1.empty or int(d1.iloc[0]["m1_bar"]) != 1:
            continue
        post = d1[d1["m1_bar"] >= POST_OR_M1].reset_index(drop=True)
        if post.empty:
            continue
        n_signal += 1
        out.append((str(or5["contract"]), day_s, or5, post, side_arm))
    return out, n_signal


# ---------------------------------------------------------------------------
# Trade result + manage helpers
# ---------------------------------------------------------------------------

def _trade_result(
    *,
    or5,
    g,
    side: str,
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
    inv_on,
    inv_level,
    inv_hit_m1,
    inv_hit_time,
    inv_hit_px,
    confirm_m1=None,
    confirm_time=None,
    confirm_close=None,
    confirm_cur_h=None,
    confirm_cur_l=None,
    n_expansions=None,
    n_opp_outsides=None,
    tf: str = "1m_expand_break",
) -> dict:
    if side == "LONG":
        gross = float(exit_px) - float(entry_px)
    else:
        gross = float(entry_px) - float(exit_px)
    net = gross - cost
    exit_time = str(g.loc[g["m1_bar"] == exit_m1, "time_et"].iloc[0])
    bars_in_trade = int(exit_m1 - entry_m1 + 1)
    entry_delay_min = int(entry_m1 - POST_OR_M1)
    confirm_delay_min = int(confirm_m1 - POST_OR_M1) if confirm_m1 is not None else None

    row = {
        "contract": str(or5["contract"]),
        "day": as_day_str(or5["trading_day"]),
        "side": side,
        "tf": tf,
        "entry_mode": "expand_break",
        "universe": universe,
        "inv_1618": int(inv_on),
        "inv_level": round(inv_level, 4) if inv_level is not None else None,
        "inv_hit_m1": inv_hit_m1,
        "inv_hit_time": inv_hit_time,
        "inv_hit_px": round(float(inv_hit_px), 4) if inv_hit_px is not None else None,
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
        "ratchet_mode": "5m_lo_confirm_1m_close" if side == "LONG" else "5m_hi_confirm_1m_close",
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
        row["n_opp_outsides"] = int(n_opp_outsides) if n_opp_outsides is not None else 0
    return row


def _manage_short_bar(*, entry_px, lo, hi, c, target, max_fib, cur_stop, stop0, hold_vwap, dvwap, dval, n_hold_bars, hit_fib, fibs, mfe, mae):
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


def _manage_long_bar(*, entry_px, lo, hi, c, target, max_fib, cur_stop, stop0, hold_vwap, dvwap, dval, n_hold_bars, hit_fib, fibs, mfe, mae):
    mfe = max(mfe, hi - entry_px)
    mae = max(mae, entry_px - lo)
    for k, px in fibs.items():
        if hi >= px - EPS:
            hit_fib[k] = True
    if hi >= target - EPS:
        return True, float(target), f"target_{max_fib:g}", None, n_hold_bars, mfe, mae, hit_fib
    if c <= cur_stop + EPS:
        reason = "ratchet_stop" if cur_stop > stop0 + EPS else "init_stop"
        return True, c, reason, None, n_hold_bars, mfe, mae, hit_fib
    hold_fail = None
    if hold_vwap:
        ok, why = chk_vwap_long(c, dvwap, dval, float("nan"), float("nan"))
        if ok:
            n_hold_bars += 1
        else:
            return True, c, f"hold_exit_{why}", why, n_hold_bars, mfe, mae, hit_fib
    return False, None, None, hold_fail, n_hold_bars, mfe, mae, hit_fib


def simulate_expand_ls(
    or5,
    g1: pd.DataFrame,
    *,
    side_arm: str,
    max_fib: float,
    hold_vwap: bool,
    inv_on: bool = True,
    cost: float = COST,
    variant: str,
    universe: str,
) -> tuple[dict | None, str | None]:
    """EXPAND break ± INV for SHORT / LONG / BOTH (first valid side wins)."""
    g = g1.sort_values("m1_bar").reset_index(drop=True)
    if len(g) < 2:
        return None, "no_entry"

    or_h = float(or5["high"])
    or_l = float(or5["low"])
    r = or_h - or_l
    if r <= 0:
        return None, "no_entry"
    or_mid = 0.5 * (or_h + or_l)
    score0 = feat(or5, "score_v2")

    # INV levels from ORIGINAL OR5
    inv_short_level = long_fib_px(or_l, r, INV_K)  # short dead if hi ≥ this
    inv_long_level = fib_px(or_h, r, INV_K)  # long dead if lo ≤ this

    allow_short = side_arm in ("SHORT", "BOTH")
    allow_long = side_arm in ("LONG", "BOTH")
    short_alive = allow_short
    long_alive = allow_long

    cur_h, cur_l = or_h, or_l
    n_expansions = 0
    n_opp_outsides = 0

    # ratchet buckets
    completed_5m_hi: dict[int, float] = {}
    completed_5m_lo: dict[int, float] = {}
    buck_m5: int | None = None
    buck_hi = float("-inf")
    buck_lo = float("inf")

    pending_side = None
    confirm_m1 = confirm_time = confirm_close = None
    confirm_cur_h = confirm_cur_l = confirm_n_exp = None
    in_trade = False
    side = None
    entry_px = entry_m1 = entry_time = None
    target = stop0 = cur_stop = None
    fibs = {}
    inv_level_used = None
    inv_hit_m1 = inv_hit_time = inv_hit_px = None

    exit_m1 = exit_px = exit_reason = None
    hold_fail = None
    n_hold_bars = 0
    hit_fib = {k: False for k in FIB_LEVELS}
    mfe = mae = 0.0

    def update_buckets(n1, m5, hi, lo):
        nonlocal buck_m5, buck_hi, buck_lo, cur_stop
        if buck_m5 is None or m5 != buck_m5:
            buck_m5 = m5
            buck_hi = hi
            buck_lo = lo
        else:
            buck_hi = max(buck_hi, hi)
            buck_lo = min(buck_lo, lo)
        if n1 % 5 == 0:
            completed_5m_hi[m5] = buck_hi
            completed_5m_lo[m5] = buck_lo
            if in_trade and m5 >= 3:
                if side == "SHORT":
                    prev_hi = completed_5m_hi.get(m5 - 1)
                    if prev_hi is not None and prev_hi < cur_stop - EPS:
                        cur_stop = prev_hi
                else:
                    prev_lo = completed_5m_lo.get(m5 - 1)
                    if prev_lo is not None and prev_lo > cur_stop + EPS:
                        cur_stop = prev_lo

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
            if pending_side is not None:
                side = pending_side
                entry_px = o
                entry_m1 = n1
                entry_time = t_et
                in_trade = True
                # fall through to manage this bar
            else:
                # Pre-entry INV (side-specific)
                if inv_on:
                    if short_alive and hi >= inv_short_level - EPS:
                        inv_hit_m1, inv_hit_time, inv_hit_px = n1, t_et, hi
                        short_alive = False
                        if not long_alive:
                            return None, "inv_1618"
                    if long_alive and lo <= inv_long_level + EPS:
                        inv_hit_m1, inv_hit_time, inv_hit_px = n1, t_et, lo
                        long_alive = False
                        if not short_alive:
                            return None, "inv_1618"

                update_buckets(n1, m5, hi, lo)

                outside_below = c < cur_l - EPS
                outside_above = c > cur_h + EPS

                if outside_below and short_alive:
                    if i == len(g) - 1:
                        return None, "last_bar"
                    pending_side = "SHORT"
                    confirm_m1, confirm_time, confirm_close = n1, t_et, c
                    confirm_cur_h, confirm_cur_l = cur_h, cur_l
                    confirm_n_exp = n_expansions
                    inv_level_used = inv_short_level
                    target = fib_px(or_h, r, max_fib)
                    fibs = {k: fib_px(or_h, r, k) for k in FIB_LEVELS}
                    stop0 = or_h + STOP_BUF
                    cur_stop = stop0
                elif outside_above and long_alive:
                    if i == len(g) - 1:
                        return None, "last_bar"
                    pending_side = "LONG"
                    confirm_m1, confirm_time, confirm_close = n1, t_et, c
                    confirm_cur_h, confirm_cur_l = cur_h, cur_l
                    confirm_n_exp = n_expansions
                    inv_level_used = inv_long_level
                    target = long_fib_px(or_l, r, max_fib)
                    fibs = {k: long_fib_px(or_l, r, k) for k in FIB_LEVELS}
                    stop0 = or_l - STOP_BUF
                    cur_stop = stop0
                elif outside_below or outside_above:
                    # breakout on dead / disarmed side — count as opp, no expand
                    n_opp_outsides += 1
                else:
                    new_h = max(cur_h, hi)
                    new_l = min(cur_l, lo)
                    if new_h > cur_h + EPS or new_l < cur_l - EPS:
                        n_expansions += 1
                    cur_h, cur_l = new_h, new_l
                continue

        # in trade
        if side == "SHORT":
            done, epx, ereason, hfail, n_hold_bars, mfe, mae, hit_fib = _manage_short_bar(
                entry_px=entry_px, lo=lo, hi=hi, c=c, target=target, max_fib=max_fib,
                cur_stop=cur_stop, stop0=stop0, hold_vwap=hold_vwap, dvwap=dvwap, dval=dval,
                n_hold_bars=n_hold_bars, hit_fib=hit_fib, fibs=fibs, mfe=mfe, mae=mae,
            )
        else:
            done, epx, ereason, hfail, n_hold_bars, mfe, mae, hit_fib = _manage_long_bar(
                entry_px=entry_px, lo=lo, hi=hi, c=c, target=target, max_fib=max_fib,
                cur_stop=cur_stop, stop0=stop0, hold_vwap=hold_vwap, dvwap=dvwap, dval=dval,
                n_hold_bars=n_hold_bars, hit_fib=hit_fib, fibs=fibs, mfe=mfe, mae=mae,
            )
        if done:
            exit_m1, exit_px, exit_reason, hold_fail = n1, epx, ereason, hfail
            break

        update_buckets(n1, m5, hi, lo)

        if i == len(g) - 1:
            exit_m1, exit_px, exit_reason = n1, c, "eod"
            break

    if exit_m1 is None or entry_px is None:
        return None, "no_entry"

    return (
        _trade_result(
            or5=or5,
            g=g,
            side=side,
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
            inv_on=inv_on,
            inv_level=inv_level_used if inv_level_used is not None else (
                inv_short_level if side == "SHORT" else inv_long_level
            ),
            inv_hit_m1=inv_hit_m1,
            inv_hit_time=inv_hit_time,
            inv_hit_px=inv_hit_px,
            confirm_m1=confirm_m1,
            confirm_time=confirm_time,
            confirm_close=confirm_close,
            confirm_cur_h=confirm_cur_h,
            confirm_cur_l=confirm_cur_l,
            n_expansions=confirm_n_exp if confirm_n_exp is not None else n_expansions,
            n_opp_outsides=n_opp_outsides,
        ),
        None,
    )


def run_variant(days, **kw) -> tuple[pd.DataFrame, int, int]:
    rows = []
    n_inv = 0
    n_other = 0
    for _c, _d, or5, g1, side_arm in days:
        t, reason = simulate_expand_ls(or5, g1, side_arm=side_arm, **kw)
        if t is not None:
            rows.append(t)
        elif reason == "inv_1618":
            n_inv += 1
        else:
            n_other += 1
    return pd.DataFrame(rows), n_inv, n_other


def enrich_metrics(m: dict, part: pd.DataFrame) -> dict:
    extra_nan = (
        "pct_win", "pct_loss", "avg_net_per_day", "largest_win", "largest_loss",
        "mean_entry_delay_min", "median_entry_delay_min",
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
    side_lab: str,
    inv_on: bool,
    n_signal: int,
    n_inv: int,
    tf: str = "1m_expand_break",
) -> list[dict]:
    out = []
    # sort for stable IS/OOS / equity
    if len(tdf):
        tdf = tdf.sort_values(["day", "entry_m1"]).reset_index(drop=True)
    is_df, oos_df = apply_date_split(tdf)
    for label, part in (("IS", is_df), ("OOS", oos_df), ("ALL", tdf)):
        m = metrics(part, label)
        m["variant"] = variant
        m["universe"] = universe
        m["side_lab"] = side_lab
        m["entry_mode"] = "EXPAND"
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
                "pct_hold_exit", "pct_target", "pct_ratchet_stop", "pct_init_stop",
                "pct_eod", "pct_hit_2618", "pct_hit_333", "pct_hit_423",
                "avg_bars", "avg_hold_bars",
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
    """Primary: F3 short, L3 long (+5), Combined BOTH — EXPAND+INV × HI/HOLD × fibs.
    Also ALL short/long T423; L3≥+8 T423 sensitivity.
    """
    specs = []
    # F3 SHORT EXPAND+INV full fib × TM
    for hold in (True, False):
        tm = "HOLD_VWAP" if hold else "RATCHET_HI"
        for fib in FIB_LEVELS:
            name = f"F3_SHORT_EXPAND_INV_{tm}_T{fib_tag(fib)}"
            specs.append(dict(
                name=name, universe="F3", side_lab="SHORT", long_cut=LONG_CUT,
                inv_on=True, max_fib=fib, hold_vwap=hold,
            ))
    # L3 LONG ≥+5 EXPAND+INV
    for hold in (True, False):
        tm = "HOLD_VWAP" if hold else "RATCHET_HI"
        for fib in FIB_LEVELS:
            name = f"L3_LONG_EXPAND_INV_{tm}_T{fib_tag(fib)}"
            specs.append(dict(
                name=name, universe="L3", side_lab="LONG", long_cut=LONG_CUT,
                inv_on=True, max_fib=fib, hold_vwap=hold,
            ))
    # COMBINED F3+L3 (≥+5)
    for hold in (True, False):
        tm = "HOLD_VWAP" if hold else "RATCHET_HI"
        for fib in FIB_LEVELS:
            name = f"COMBINED_F3L5_EXPAND_INV_{tm}_T{fib_tag(fib)}"
            specs.append(dict(
                name=name, universe="BOTH", side_lab="COMBINED", long_cut=LONG_CUT,
                inv_on=True, max_fib=fib, hold_vwap=hold,
            ))
    # ALL short / long T423 only
    for side_u, side_lab in (("F3", "SHORT"), ("L3", "LONG")):
        pass
    for hold in (True, False):
        tm = "HOLD_VWAP" if hold else "RATCHET_HI"
        specs.append(dict(
            name=f"ALL_SHORT_EXPAND_INV_{tm}_T423",
            universe="ALL", side_lab="SHORT", long_cut=LONG_CUT,
            inv_on=True, max_fib=4.23, hold_vwap=hold, force_arm="SHORT",
        ))
        specs.append(dict(
            name=f"ALL_LONG_EXPAND_INV_{tm}_T423",
            universe="ALL", side_lab="LONG", long_cut=LONG_CUT,
            inv_on=True, max_fib=4.23, hold_vwap=hold, force_arm="LONG",
        ))
    # L3 ≥+8 sensitivity T423
    for hold in (True, False):
        tm = "HOLD_VWAP" if hold else "RATCHET_HI"
        specs.append(dict(
            name=f"L3p8_LONG_EXPAND_INV_{tm}_T423",
            universe="L3", side_lab="LONG", long_cut=LONG_CUT_ALT,
            inv_on=True, max_fib=4.23, hold_vwap=hold,
        ))
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
        "side_lab": r["side_lab"],
        "entry_mode": r["entry_mode"],
        "inv_1618": r["inv_1618"],
        "n": int(r["n_trades"]),
        "n_long": r.get("n_long", 0),
        "n_short": r.get("n_short", 0),
        "WR": _safe_round(r["win_rate"], 4),
        "pct_win": _safe_round(r.get("pct_win"), 4),
        "pct_loss": _safe_round(r.get("pct_loss"), 4),
        "net": round(r["net_pts"], 2),
        "PF": None if r["pf"] is None or (isinstance(r["pf"], float) and math.isnan(r["pf"])) else round(float(r["pf"]), 3),
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
        "pct_signal_no_entry": _safe_round(r.get("pct_signal_no_entry"), 4),
        "n_signal_days": r.get("n_signal_days"),
        "n_signal_no_entry": r.get("n_signal_no_entry"),
    }


def plot_equity(trade_map: dict[str, pd.DataFrame], keys: list[str], out_path: Path):
    fig, ax = plt.subplots(figsize=(13, 6.5))
    style = {
        "F3_SHORT_EXPAND_INV_RATCHET_HI_T423": ("#d62728", "-", 2.2),
        "F3_SHORT_EXPAND_INV_HOLD_VWAP_T423": ("#ff7f0e", "-", 1.8),
        "L3_LONG_EXPAND_INV_RATCHET_HI_T423": ("#2ca02c", "-", 2.2),
        "L3_LONG_EXPAND_INV_HOLD_VWAP_T423": ("#98df8a", "-", 1.8),
        "COMBINED_F3L5_EXPAND_INV_RATCHET_HI_T423": ("#1f77b4", "-", 2.4),
        "COMBINED_F3L5_EXPAND_INV_HOLD_VWAP_T423": ("#9467bd", "-", 2.0),
    }
    for v in keys:
        tdf = trade_map.get(v)
        if tdf is None or tdf.empty:
            continue
        t = tdf.sort_values(["day", "entry_m1"]).reset_index(drop=True)
        eq = t["net"].cumsum()
        color, ls, lw = style.get(v, ("#bbbbbb", ":", 0.9))
        ax.plot(eq.index + 1, eq.values, label=v.replace("_EXPAND_INV", ""), lw=lw, color=color, ls=ls)
    ax.axhline(0, color="#888", lw=0.8)
    ax.set_title("OR5 1m LS EXPAND+INV — Short F3 / Long L3≥+5 / Combined (T423)")
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

    # Cache day lists by (universe, long_cut)
    day_cache: dict[tuple, tuple] = {}

    def get_days(universe: str, long_cut: float):
        key = (universe, long_cut)
        if key not in day_cache:
            days, n_sig = collect_days(bars5, bars1, universe=universe, long_cut=long_cut)
            day_cache[key] = (days, n_sig)
            print(f"  universe={universe} cut={long_cut:+.0f} signal_days={n_sig}")
        return day_cache[key]

    specs = build_specs()
    trade_map: dict[str, pd.DataFrame] = {}
    summary_rows: list[dict] = []
    all_trades = []
    inv_counts: dict[str, int] = {}

    for sp in specs:
        name = sp["name"]
        uni = sp["universe"]
        long_cut = sp["long_cut"]
        days, n_sig = get_days(uni, long_cut)

        # ALL short/long: re-arm every day to one side
        force_arm = sp.get("force_arm")
        if force_arm:
            days_run = [(c, d, o, g, force_arm) for c, d, o, g, _a in days]
        else:
            days_run = days

        print(f"Running {name}…")
        tdf, n_inv, n_other = run_variant(
            days_run,
            max_fib=sp["max_fib"],
            hold_vwap=sp["hold_vwap"],
            inv_on=sp["inv_on"],
            variant=name,
            universe=uni if not force_arm else f"ALL_{force_arm}",
        )
        if len(tdf):
            tdf = tdf.sort_values(["day", "entry_m1"]).reset_index(drop=True)
        trade_map[name] = tdf
        all_trades.append(tdf)
        inv_counts[name] = n_inv
        summary_rows.extend(
            split_rows(
                name, tdf,
                universe=uni if not force_arm else f"ALL_{force_arm}",
                side_lab=sp["side_lab"],
                inv_on=sp["inv_on"],
                n_signal=n_sig if not force_arm else len(days_run),
                n_inv=n_inv,
            )
        )
        m_all = next(r for r in summary_rows if r["variant"] == name and r["split"] == "ALL")
        print(
            f"  ALL n={m_all['n_trades']}/{n_sig if not force_arm else len(days_run)} "
            f"L={m_all.get('n_long',0)} S={m_all.get('n_short',0)} "
            f"inv_skip={n_inv} other={n_other} "
            f"WR={fmt_pct(m_all['win_rate'])} net={m_all['net_pts']:.1f} "
            f"PF={fmt_pf(m_all['pf'])} avg/día={fmt_net(m_all['avg_net_per_day'])} "
            f"Lwin={fmt_net(m_all['largest_win'])}@{m_all.get('largest_win_day','')} "
            f"Lloss={fmt_net(m_all['largest_loss'])}@{m_all.get('largest_loss_day','')}"
        )

    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    trades_df.to_csv(OUT_TRADES, index=False)
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(OUT_SUMMARY, index=False)
    cmp_df = pd.DataFrame([cmp_row(r) for r in summary_rows])
    cmp_df.to_csv(OUT_TABLE, index=False)

    by = {(r["variant"], r["split"]): r for r in summary_rows}

    eq_keys = [
        "F3_SHORT_EXPAND_INV_RATCHET_HI_T423",
        "F3_SHORT_EXPAND_INV_HOLD_VWAP_T423",
        "L3_LONG_EXPAND_INV_RATCHET_HI_T423",
        "L3_LONG_EXPAND_INV_HOLD_VWAP_T423",
        "COMBINED_F3L5_EXPAND_INV_RATCHET_HI_T423",
        "COMBINED_F3L5_EXPAND_INV_HOLD_VWAP_T423",
    ]
    plot_equity(trade_map, eq_keys, OUT_EQUITY)

    def get(v, sp="ALL"):
        return by[(v, sp)]

    # Sanity: Combined n ≈ Short n + Long n; no shared days
    for tm in ("RATCHET_HI", "HOLD_VWAP"):
        s = trade_map[f"F3_SHORT_EXPAND_INV_{tm}_T423"]
        l = trade_map[f"L3_LONG_EXPAND_INV_{tm}_T423"]
        c = trade_map[f"COMBINED_F3L5_EXPAND_INV_{tm}_T423"]
        if len(s) and len(l):
            overlap = set(s["day"]) & set(l["day"])
            assert not overlap, f"F3/L3 day overlap: {overlap}"
        assert len(c) == len(s) + len(l), f"Combined n mismatch {tm}: {len(c)} vs {len(s)}+{len(l)}"
        # Combined net ≈ sum
        net_c = float(c["net"].sum()) if len(c) else 0.0
        net_sl = (float(s["net"].sum()) if len(s) else 0.0) + (float(l["net"].sum()) if len(l) else 0.0)
        assert abs(net_c - net_sl) < 0.01, f"Combined net mismatch {tm}"

    # Headline Spanish table rows
    headline = []
    for side_lab, key_pat in (
        ("Short F3", "F3_SHORT_EXPAND_INV_{tm}_T423"),
        ("Long L3≥+5", "L3_LONG_EXPAND_INV_{tm}_T423"),
        ("Combined F3+L5", "COMBINED_F3L5_EXPAND_INV_{tm}_T423"),
    ):
        for tm in ("RATCHET_HI", "HOLD_VWAP"):
            key = key_pat.format(tm=tm)
            r = get(key)
            r_is = get(key, "IS")
            r_oos = get(key, "OOS")
            headline.append(
                f"| {side_lab} {tm} | {int(r['n_trades'])} | {fmt_pct(r['win_rate'])} | "
                f"{fmt_net(r['net_pts'])} | {fmt_net(r['avg_net_per_day'])} | "
                f"{fmt_net(r['largest_win'])} ({r.get('largest_win_day','')}) | "
                f"{fmt_net(r['largest_loss'])} ({r.get('largest_loss_day','')}) | "
                f"{fmt_pf(r['pf'])} | "
                f"IS n={int(r_is['n_trades'])} net={fmt_net(r_is['net_pts'])} PF={fmt_pf(r_is['pf'])} · "
                f"OOS n={int(r_oos['n_trades'])} net={fmt_net(r_oos['net_pts'])} PF={fmt_pf(r_oos['pf'])} |"
            )
    headline_block = "\n".join(headline)

    # Extra rows: ALL + L3+8
    extra = []
    for key in (
        "ALL_SHORT_EXPAND_INV_RATCHET_HI_T423",
        "ALL_SHORT_EXPAND_INV_HOLD_VWAP_T423",
        "ALL_LONG_EXPAND_INV_RATCHET_HI_T423",
        "ALL_LONG_EXPAND_INV_HOLD_VWAP_T423",
        "L3p8_LONG_EXPAND_INV_RATCHET_HI_T423",
        "L3p8_LONG_EXPAND_INV_HOLD_VWAP_T423",
    ):
        r = get(key)
        lab = key.replace("_EXPAND_INV", "").replace("_T423", "")
        extra.append(
            f"| {lab} | {int(r['n_trades'])} | {fmt_pct(r['win_rate'])} | "
            f"{fmt_net(r['net_pts'])} | {fmt_pf(r['pf'])} | {fmt_net(r['avg_net_per_day'])} | "
            f"{fmt_net(r['largest_win'])} ({r.get('largest_win_day','')}) | "
            f"{fmt_net(r['largest_loss'])} ({r.get('largest_loss_day','')}) | "
            f"{fmt_n(r.get('n_inv_skipped'))} |"
        )
    extra_block = "\n".join(extra)

    n_f3 = get_days("F3", LONG_CUT)[1]
    n_l3 = get_days("L3", LONG_CUT)[1]
    n_l3p8 = get_days("L3", LONG_CUT_ALT)[1]
    n_both = get_days("BOTH", LONG_CUT)[1]
    n_all = get_days("ALL", LONG_CUT)[1]

    # Verify short F3 T423 HI matches prior INV1618 roughly
    s_hi = get("F3_SHORT_EXPAND_INV_RATCHET_HI_T423")
    print(f"\nBaseline check F3 SHORT EXPAND+INV HI T423: n={int(s_hi['n_trades'])} net={s_hi['net_pts']:.1f}")

    md = f"""# OR5_1m_LS_EXPAND_INV — Long + Short expanding OR + INV 1.618

**Status:** [EXPLORATORY] research only · no live trading  
**Date:** 2026-09-29 (America/Bogota)  
**Data:** `CANDLE_FORCE_v3_multiday_bars.parquet` + `NQ_front_volroll.duckdb` 1m + dVWAP  
**IS/OOS:** IS ≤ {IS_LAST} · OOS ≥ 2026-06-11 · range ~2025-12-16..2026-08-25  
**Cost:** {COST} pts RT · **STOP_BUF:** {STOP_BUF}

---

## Shared OR mechanics

1. OR5 = 09:30–09:35 ET High/Low. R = OR_H−OR_L.
2. Expanding range: inside close → expand; outside → breakout (no expand).
3. Short: close < cur_L → next open. Long: close > cur_H → next open.
4. ≤1 trade/day (first valid side). Skip last-bar confirm.
5. **INV (pre-entry, original OR5):**
   - Short dead if 1m high ≥ `OR_L + 1.618·R`
   - Long dead if 1m low ≤ `fib_px(OR_H,R,1.618)` = `OR_H − 1.618·R`
6. Targets: short `fib_px(k)`; long `OR_L + k·R`. k ∈ {{2.618, 3.33, 4.23}}

## Filters

| Book | Filter | Signal days |
|---|---|---:|
| Short F3 | OR5 red · score_v2≤−5 · vbp≤0 | {n_f3} |
| Long L3≥+5 | OR5 green · score_v2≥+5 · vbp≥0 | {n_l3} |
| Combined | F3 ∪ L3 (0 day overlap) | {n_both} |
| Long L3≥+8 (sens.) | green · ≥+8 · vbp≥0 | {n_l3p8} |
| ALL | no CF filter | {n_all} |

Note: `run_or5_cf_extremes` used ≥+6 historically; VbP long side table used ≥+8.
Primary long cut = **≥+5** for symmetry with short −5.

## Management

RATCHET: short=prior 5m High; long=prior 5m Low · exit 1m close beyond stop.  
HOLD_VWAP: short exit close≥dVWAP; long exit close≤dVWAP.  
Init stop: short OR_H+2; long OR_L−2.

## Headline — T423 HI & HOLD (ALL split)

| Setup | n | %W | Net | pts/día | Lwin (fecha) | Lloss (fecha) | PF | IS / OOS |
|---|---:|---:|---:|---:|---|---|---:|---|
{headline_block}

## Extra — ALL sides + L3≥+8 T423

| Setup | n | %W | Net | PF | pts/día | Lwin | Lloss | n INV skip |
|---|---:|---:|---:|---:|---:|---|---|---:|
{extra_block}

## Outputs

- Script: `run_or5_1m_ls_expand_inv.py`
- Trades / summary / compare: `{PREFIX}_*.csv`
- Equity: `{PREFIX}_equity.png`
- Spanish: `{PREFIX}_RESUMEN_ES.md`

*Research exploratorio — no es señal live.*
"""
    OUT_SPEC.write_text(md, encoding="utf-8")

    # Spanish resumen
    es_rows = []
    for side_lab, key_pat in (
        ("Short F3", "F3_SHORT_EXPAND_INV_{tm}_T423"),
        ("Long L3≥+5", "L3_LONG_EXPAND_INV_{tm}_T423"),
        ("Combined F3+L5", "COMBINED_F3L5_EXPAND_INV_{tm}_T423"),
    ):
        for tm in ("RATCHET_HI", "HOLD_VWAP"):
            key = key_pat.format(tm=tm)
            r = get(key)
            r_is = get(key, "IS")
            r_oos = get(key, "OOS")
            es_rows.append(
                f"| {side_lab} | {tm} | {int(r['n_trades'])} | {fmt_pct(r['win_rate'])} | "
                f"{fmt_net(r['net_pts'])} | {fmt_net(r['avg_net_per_day'])} | "
                f"{fmt_net(r['largest_win'])} ({r.get('largest_win_day','')}) | "
                f"{fmt_net(r['largest_loss'])} ({r.get('largest_loss_day','')}) | "
                f"{fmt_pf(r['pf'])} | "
                f"{int(r_is['n_trades'])}/{fmt_net(r_is['net_pts'])}/{fmt_pf(r_is['pf'])} | "
                f"{int(r_oos['n_trades'])}/{fmt_net(r_oos['net_pts'])}/{fmt_pf(r_oos['pf'])} |"
            )
    es_table = "\n".join(es_rows)

    s_hi = get("F3_SHORT_EXPAND_INV_RATCHET_HI_T423")
    s_ho = get("F3_SHORT_EXPAND_INV_HOLD_VWAP_T423")
    l_hi = get("L3_LONG_EXPAND_INV_RATCHET_HI_T423")
    l_ho = get("L3_LONG_EXPAND_INV_HOLD_VWAP_T423")
    c_hi = get("COMBINED_F3L5_EXPAND_INV_RATCHET_HI_T423")
    c_ho = get("COMBINED_F3L5_EXPAND_INV_HOLD_VWAP_T423")
    l8_hi = get("L3p8_LONG_EXPAND_INV_RATCHET_HI_T423")

    es = f"""# Resumen OR5 1m LS EXPAND+INV — para Luis

**Mecánica compartida:** OR5 expand → breakout 1m (short bajo cur_L / long sobre cur_H) → fill next open.  
**INV pre-entrada (OR5 original):** short muere si high≥OR_L+1.618·R; long muere si low≤OR_H−1.618·R (`fib_px`).  
**Filtros:** Short F3 (rojo, score≤−5, vbp≤0) · Long L3≥+5 (verde, score≥+5, vbp≥0; simétrico a −5).  
Históricamente extremes usó ≥+6 y VbP-long ≥+8 — aquí primary = **≥+5**; sensibilidad ≥+8 en CSV.  
**Gestión:** RATCHET (5m HI short / 5m LO long) y HOLD_VWAP (espejo). Coste 0.50. Foco T4.23.

Señales: F3={n_f3} · L3≥+5={n_l3} · Combined={n_both} · L3≥+8={n_l3p8} · overlap F3∩L3=0.

## Tabla Long / Short / Combined — T423 HI y HOLD

| Lado | TM | n | %W | Net | pts/día | Mejor (fecha) | Peor (fecha) | PF | IS n/net/PF | OOS n/net/PF |
|---|---|---:|---:|---:|---:|---|---|---:|---|---|
{es_table}

## Lectura directa

- **Short F3 baseline (EXPAND+INV):** HI n={int(s_hi['n_trades'])} net={fmt_net(s_hi['net_pts'])} PF={fmt_pf(s_hi['pf'])} pts/día={fmt_net(s_hi['avg_net_per_day'])} · HOLD n={int(s_ho['n_trades'])} net={fmt_net(s_ho['net_pts'])} PF={fmt_pf(s_ho['pf'])}
- **Long L3≥+5 (EXPAND+INV):** HI n={int(l_hi['n_trades'])} net={fmt_net(l_hi['net_pts'])} PF={fmt_pf(l_hi['pf'])} pts/día={fmt_net(l_hi['avg_net_per_day'])} · Lwin {fmt_net(l_hi['largest_win'])} ({l_hi.get('largest_win_day','')}) · Lloss {fmt_net(l_hi['largest_loss'])} ({l_hi.get('largest_loss_day','')}) · HOLD net={fmt_net(l_ho['net_pts'])} PF={fmt_pf(l_ho['pf'])}
- **Combined = Short+Long:** HI n={int(c_hi['n_trades'])} net={fmt_net(c_hi['net_pts'])} PF={fmt_pf(c_hi['pf'])} pts/día={fmt_net(c_hi['avg_net_per_day'])} · HOLD n={int(c_ho['n_trades'])} net={fmt_net(c_ho['net_pts'])} PF={fmt_pf(c_ho['pf'])}
- **Sensibilidad Long ≥+8:** HI n={int(l8_hi['n_trades'])} net={fmt_net(l8_hi['net_pts'])} PF={fmt_pf(l8_hi['pf'])} (pocos días={n_l3p8})

Detalle EN: `{PREFIX}.md` · equity: `{PREFIX}_equity.png`  
CSV: `{PREFIX}_trades.csv` / `_summary.csv` / `_compare.csv`

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
    for lab, key in (
        ("Short HI", "F3_SHORT_EXPAND_INV_RATCHET_HI_T423"),
        ("Short HOLD", "F3_SHORT_EXPAND_INV_HOLD_VWAP_T423"),
        ("Long HI", "L3_LONG_EXPAND_INV_RATCHET_HI_T423"),
        ("Long HOLD", "L3_LONG_EXPAND_INV_HOLD_VWAP_T423"),
        ("Comb HI", "COMBINED_F3L5_EXPAND_INV_RATCHET_HI_T423"),
        ("Comb HOLD", "COMBINED_F3L5_EXPAND_INV_HOLD_VWAP_T423"),
    ):
        r = get(key)
        print(
            f"  {lab}: n={int(r['n_trades'])} WR={fmt_pct(r['win_rate'])} "
            f"net={r['net_pts']:.1f} PF={fmt_pf(r['pf'])} pts/día={fmt_net(r['avg_net_per_day'])}"
        )


if __name__ == "__main__":
    main()
