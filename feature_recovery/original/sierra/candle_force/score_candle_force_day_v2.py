#!/usr/bin/env python3
"""Score every RTH 5m candle with Luis candle-force v2 (16 checks).

Research only. Does not touch locked play engines. Keeps v1 intact.

Checks 1–9: same semantics as score_candle_force_day.py (locked).
Checks 10–15: vs previous completed 5m on same RTH day.
Check 16: vbp_zone_weight — VbP volume-location matrix (supplements hvn_shape).
Score = sum ∈ [−16, +16]. First RTH bar: checks 10–15 all 0.

No prev-VWAP (Luis doubted it).
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date
from pathlib import Path

_VENV = Path("/workspace/sierra/.venv/lib/python3.13/site-packages")
if _VENV.exists() and str(_VENV) not in sys.path:
    sys.path.insert(0, str(_VENV))
if "/workspace/sierra" not in sys.path:
    sys.path.insert(0, "/workspace/sierra")
if "/workspace/sierra/candle_force" not in sys.path:
    sys.path.insert(0, "/workspace/sierra/candle_force")

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle
import numpy as np
import pandas as pd

from plot_daily_1h_ah_rth_dva import C_DN, C_UP, C_WICK
from plot_pmor_orhl5_day import load_rth_15s
from luis_or_127_close205 import as_naive
from luis_vbp_or import BARS_PER_5M, TICK
from plot_eval3_5m_va_pmor import chunk_ohlc_delta, vbp_va70

from candle_force_vbp_weights import (
    ZONE_COLORS,
    compute_from_grp,
    draw_matrix_legend_png,
    is_doji_body,
    matrix_summary_text,
    zone_color_for_price,
)

from score_candle_force_day import (
    AT_TICKS,
    DOJI_FRAC,
    FLATTEN_BAR,
    MARU_FRAC,
    bar_vwap,
    load_footprint,
    pick_db,
    third_loc,
    vote_body,
    vote_delta,
    vote_marubozu,
    vote_volume,
    vote_vs_level,
    vote_vs_va,
)

OUT_DIR = Path("/workspace/sierra/candle_force")
GALLERY_DIR = OUT_DIR / "score_gallery"
DEFAULT_DAY = date(2026, 8, 13)

# Luis chart style
C_BG = "#ffffff"
C_TEXT = "#000000"
C_HDR = "#e8eef2"
C_CELL = "#f7f7f7"
C_CELL_ALT = "#efefef"
C_BORDER = "#90a4ae"
C_POS = "#1b7a32"
C_NEG = "#c62828"
C_NEU = "#546e7a"
C_ZERO = "#78909c"
C_FOCUS = "#1565c0"
C_POC = "#1b7a32"
C_VA = "#6a3d9a"
C_VWAP = "#e65100"
C_VBP = "#b0bec5"
C_VBP_VA = "#5c9ead"
C_VBP_POC = "#c9a227"

VOTE9_COLS = [
    "body",
    "close_loc",
    "vs_poc",
    "vs_va",
    "vs_vwap",
    "hvn_shape",
    "volume",
    "delta_vote",
    "marubozu",
]
VOTE_PREV_COLS = [
    "prev_close",
    "prev_break",
    "prev_delta",
    "prev_volume",
    "prev_poc",
    "prev_body",
]
VOTE15_COLS = VOTE9_COLS + VOTE_PREV_COLS  # legacy name kept
VOTE16_COLS = VOTE15_COLS + ["vbp_zone_weight"]

CHECKS15 = [  # legacy alias — prefer CHECKS16
    ("body", "body", "body"),
    ("close_loc", "close_loc", "close_loc"),
    ("vs_poc", "vs_poc", "vs_poc"),
    ("vs_va", "vs_va", "vs_va"),
    ("vs_vwap", "vs_vwap", "vs_vwap"),
    ("hvn_shape", "hvn_shape", "hvn_shape"),
    ("volume", "volume", "volume"),
    ("delta", "delta_vote", "delta"),
    ("marubozu", "marubozu", "marubozu"),
    ("prev_close", "prev_close", "prev_close"),
    ("prev_break", "prev_break", "prev_break"),
    ("prev_delta", "prev_delta", "prev_delta"),
    ("prev_volume", "prev_volume", "prev_volume"),
    ("prev_poc", "prev_poc", "prev_poc"),
    ("prev_body", "prev_body", "prev_body"),
]
CHECKS16 = CHECKS15 + [("vbp_zone_weight", "vbp_zone_weight", "vbp_zone_weight")]


def _finite(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def vote_prev_close(c: float, c_prev: float) -> int:
    if abs(c - c_prev) < 1e-12:
        return 0
    return 1 if c > c_prev else -1


def vote_prev_break(c: float, h_prev: float, l_prev: float) -> int:
    if c > h_prev + 1e-12:
        return 1
    if c < l_prev - 1e-12:
        return -1
    return 0


def vote_prev_delta(delta: float, delta_prev: float) -> int:
    """Sign of (delta − delta_prev). Stronger buying than prev → +1 regardless of absolute sign."""
    if not (math.isfinite(delta) and math.isfinite(delta_prev)):
        return 0
    d = delta - delta_prev
    if abs(d) < 1e-9:
        return 0
    return 1 if d > 0 else -1


def vote_prev_volume(vol: float, vol_prev: float, body_vote: int) -> int:
    """HIGH vol vs prev confirms body."""
    if vol <= vol_prev + 1e-12:
        return 0
    if body_vote > 0:
        return 1
    if body_vote < 0:
        return -1
    return 0


def vote_prev_poc(poc: float, poc_prev: float, at_ticks: float = AT_TICKS) -> int:
    if not (math.isfinite(poc) and math.isfinite(poc_prev)):
        return 0
    if abs(poc - poc_prev) < at_ticks * TICK + 1e-12:
        return 0
    return 1 if poc > poc_prev else -1


def vote_prev_body(
    o: float, c: float, lo: float, hi: float,
    o_prev: float, c_prev: float,
) -> int:
    """body_dir * (1 if curr_body_size > prev_body_size else 0)."""
    body_dir = vote_body(o, c, lo, hi)  # +1 bull / −1 bear / 0 doji
    if body_dir == 0:
        return 0
    curr_sz = abs(c - o)
    prev_sz = abs(c_prev - o_prev)
    if curr_sz > prev_sz + 1e-12:
        return body_dir
    return 0


def score_day_v2(day: date, db: Path) -> pd.DataFrame:
    g15 = load_rth_15s(day, db)
    if g15.empty:
        raise SystemExit(f"no 15s bars for {day} in {db}")
    g15 = g15[g15["rth_bar_number"].between(1, FLATTEN_BAR)].copy()
    max_bn = int(g15["rth_bar_number"].max())
    n5 = max_bn // BARS_PER_5M
    fp = load_footprint(day, db, max_bn=n5 * BARS_PER_5M)

    rows: list[dict] = []
    prior_vols: list[float] = []
    prev: dict | None = None

    for k in range(1, n5 + 1):
        b0 = (k - 1) * BARS_PER_5M + 1
        b1 = k * BARS_PER_5M
        chunk = g15[g15["rth_bar_number"].between(b0, b1)].sort_values("rth_bar_number")
        if len(chunk) < BARS_PER_5M:
            continue

        oh = chunk_ohlc_delta(g15, b0, b1)
        o, h, lo, c = oh["open"], oh["high"], oh["low"], oh["close"]
        vol = float(oh["volume"])
        delta = float(oh["volume_delta"])
        va = vbp_va70(fp, b0, b1, c)
        poc, val, vah = va["poc"], va["val"], va["vah"]
        vwap = bar_vwap(chunk)

        v_body = vote_body(o, c, lo, h)
        v_close_loc = third_loc(c, lo, h)
        v_vs_poc = vote_vs_level(c, poc)
        v_vs_va = vote_vs_va(c, val, vah)
        v_vs_vwap = vote_vs_level(c, vwap)
        v_hvn = third_loc(poc, lo, h) if math.isfinite(poc) else 0
        med = float(np.median(prior_vols)) if prior_vols else None
        v_vol = vote_volume(vol, med, v_body)
        v_delta = vote_delta(delta)
        v_maru = vote_marubozu(o, c, lo, h)

        # VbP zone-weight matrix (check 16) — supplements hvn_shape
        fchunk = fp[(fp["rth_bar_number"] >= b0) & (fp["rth_bar_number"] <= b1)]
        grp = fchunk.groupby("price", sort=True)["total_volume"].sum()
        zw = compute_from_grp(grp, o, h, lo, c)
        v_vbp_zw = int(zw.vote)

        votes9 = {
            "body": v_body,
            "close_loc": v_close_loc,
            "vs_poc": v_vs_poc,
            "vs_va": v_vs_va,
            "vs_vwap": v_vs_vwap,
            "hvn_shape": v_hvn,
            "volume": v_vol,
            "delta_vote": v_delta,
            "marubozu": v_maru,
        }
        score9 = int(sum(votes9.values()))

        if prev is None:
            # First RTH bar of day: checks 10–15 all 0
            votes_prev = {col: 0 for col in VOTE_PREV_COLS}
        else:
            votes_prev = {
                "prev_close": vote_prev_close(c, prev["close"]),
                "prev_break": vote_prev_break(c, prev["high"], prev["low"]),
                "prev_delta": vote_prev_delta(delta, prev["delta"]),
                "prev_volume": vote_prev_volume(vol, prev["vol"], v_body),
                "prev_poc": vote_prev_poc(poc, prev["poc"]),
                "prev_body": vote_prev_body(
                    o, c, lo, h, prev["open"], prev["close"]
                ),
            }

        votes = {**votes9, **votes_prev, "vbp_zone_weight": v_vbp_zw}
        score = int(sum(votes.values()))

        t0 = as_naive(chunk.iloc[0]["t"])
        t1 = as_naive(chunk.iloc[-1]["t_end"])
        rows.append(
            {
                "trading_day": day.isoformat(),
                "m5_bar": k,
                "b0": b0,
                "b1": b1,
                "time": t0.strftime("%H:%M"),
                "t_start": t0.isoformat(sep=" "),
                "t_end": t1.isoformat(sep=" "),
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(lo, 4),
                "close": round(c, 4),
                "vol": vol,
                "delta": delta,
                "poc": round(float(poc), 4) if math.isfinite(poc) else float("nan"),
                "val": round(float(val), 4) if math.isfinite(val) else float("nan"),
                "vah": round(float(vah), 4) if math.isfinite(vah) else float("nan"),
                "vwap": round(float(vwap), 4) if math.isfinite(vwap) else float("nan"),
                **votes,
                "w_long": round(zw.w_long, 4),
                "w_short": round(zw.w_short, 4),
                "w_body_fight": round(zw.w_body_fight, 4),
                "vbp_imbalance": round(zw.imbalance, 6),
                "vbp_is_doji": int(zw.is_doji),
                "vol_lower_wick": round(zw.vol_lower_wick, 4),
                "vol_upper_wick": round(zw.vol_upper_wick, 4),
                "vol_near_close": round(zw.vol_near_close, 4),
                "vol_body_fight": round(zw.vol_body_fight, 4),
                "score9": score9,
                "score15": int(sum(votes9[c] for c in VOTE9_COLS) + sum(votes_prev.values())),
                "score": score,
            }
        )
        prior_vols.append(vol)
        prev = {
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "vol": vol,
            "delta": delta,
            "poc": float(poc) if math.isfinite(poc) else float("nan"),
        }

    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit(f"no completed 5m bars for {day}")
    df["cum_score"] = df["score"].cumsum().astype(int)
    assert (df[VOTE16_COLS].sum(axis=1) == df["score"]).all()
    assert (df[VOTE9_COLS].sum(axis=1) == df["score9"]).all()
    # first bar prev checks must be 0
    assert (df.iloc[0][VOTE_PREV_COLS] == 0).all()
    return df


def draw_chart(df: pd.DataFrame, day: date, out_png: Path) -> None:
    fig, (ax, ax2) = plt.subplots(
        2,
        1,
        figsize=(16.5, 14.0),
        gridspec_kw={"height_ratios": [2.6, 1.0], "hspace": 0.10},
        sharex=True,
    )
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#ffffff")
    ax2.set_facecolor("#ffffff")

    w = 3.6 / (24 * 60)
    xs: list[float] = []
    for _, r in df.iterrows():
        tt = pd.Timestamp(r["t_start"]).to_pydatetime()
        o, h, lo, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        up = c >= o
        body_c = C_UP if up else C_DN
        x = mdates.date2num(tt)
        xs.append(x)
        ax.plot([x, x], [lo, h], color=C_WICK, linewidth=0.9, solid_capstyle="round", zorder=3)
        y0, y1 = min(o, c), max(o, c)
        if abs(y1 - y0) < 1e-9:
            y1 = y0 + TICK * 0.15
        ax.add_patch(
            Rectangle(
                (x - w / 2, y0),
                w,
                y1 - y0,
                facecolor=body_c,
                edgecolor=body_c,
                linewidth=0.6,
                zorder=4,
                alpha=0.92,
            )
        )
        sc = int(r["score"])
        pad = (h - lo) * 0.08 + TICK * 2
        if sc > 0:
            ax.text(
                x, h + pad, f"+{sc}",
                ha="center", va="bottom", fontsize=7.0,
                color="#1b7a32", fontweight="bold", zorder=5,
            )
        elif sc < 0:
            ax.text(
                x, lo - pad, f"{sc}",
                ha="center", va="top", fontsize=7.0,
                color="#c62828", fontweight="bold", zorder=5,
            )
        else:
            ax.text(
                x, h + pad * 0.5, "0",
                ha="center", va="bottom", fontsize=6.0,
                color="#78909c", zorder=5,
            )

    hi = float(df["high"].max())
    lo_all = float(df["low"].min())
    pad_y = (hi - lo_all) * 0.06 + TICK * 4
    ax.set_ylim(lo_all - pad_y * 1.8, hi + pad_y * 1.8)
    ax.set_ylabel("NQU26", color="#000000", fontsize=11)
    ax.tick_params(colors="#000000")
    for spine in ax.spines.values():
        spine.set_color("#90a4ae")
    ax.set_title(
        f"{day.strftime('%b %d')} NQU26 · fuerza 5m v2 (−16…+16)",
        color="#000000",
        fontsize=14,
        fontweight="bold",
        pad=10,
    )
    ax.grid(True, axis="y", color="#eceff1", linewidth=0.6, zorder=0)

    scores = df["score"].to_numpy(dtype=int)
    colors = [
        "#1b7a32" if s > 0 else ("#c62828" if s < 0 else "#90a4ae") for s in scores
    ]
    ax2.bar(xs, scores, width=w * 1.15, color=colors, alpha=0.85, zorder=3, align="center")
    ax2.axhline(0, color="#546e7a", linewidth=0.8, zorder=2)
    cum = df["cum_score"].to_numpy(dtype=int)
    ax2.set_ylabel("score (−16…+16)", color="#000000", fontsize=10)
    ax2.set_ylim(-15.8, 15.8)
    ax2.tick_params(colors="#000000")
    for spine in ax2.spines.values():
        spine.set_color("#90a4ae")
    ax2.grid(True, axis="y", color="#eceff1", linewidth=0.6, zorder=0)

    ax2b = ax2.twinx()
    ax2b.plot(
        xs, cum, color="#1565c0", linewidth=1.8, marker="o", markersize=2.4,
        label="cum_score", zorder=5,
    )
    cmin, cmax = int(cum.min()), int(cum.max())
    pad_c = max(3, int(max(abs(cmin), abs(cmax)) * 0.08) + 1)
    ax2b.set_ylim(cmin - pad_c, cmax + pad_c)
    ax2b.set_ylabel("cum_score", color="#1565c0", fontsize=10)
    ax2b.tick_params(axis="y", colors="#1565c0")
    ax2b.legend(loc="upper left", fontsize=8, frameon=True, facecolor="white", edgecolor="#cfd8dc")

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax2.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0, 30]))
    ax2.set_xlabel("Chicago CT", color="#000000", fontsize=10)
    fig.autofmt_xdate(rotation=0, ha="center")

    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=140, bbox_inches="tight", facecolor="#ffffff")
    plt.close(fig)


def draw_rubric_png(out_png: Path) -> None:
    """16-check rubric +1/0/−1 (check 16 = vbp_zone_weight)."""
    factors = [
        (1, "body", "C > O (alcista)", "Doji", "C < O (bajista)", "Dirección del cuerpo OHLC."),
        (2, "close_loc", "Cierre tercio alto", "Cierre tercio medio", "Cierre tercio bajo", "Dónde cierra en [L,H]."),
        (3, "vs_poc", "C > POC", "C ≈ POC", "C < POC", "Cierre vs POC de la vela."),
        (4, "vs_va", "C > VAH", "Dentro del VA", "C < VAL", "Cierre vs Value Area."),
        (5, "vs_vwap", "C > VWAP", "C ≈ VWAP", "C < VWAP", "Cierre vs VWAP de la vela."),
        (6, "hvn_shape", "HVN tercio alto", "HVN tercio medio", "HVN tercio bajo", "POC/HVN en el rango."),
        (7, "volume", "Vol alto + cuerpo alcista", "Vol medio/bajo o doji", "Vol alto + cuerpo bajista", "Vol vs mediana previa confirma cuerpo."),
        (8, "delta", "Delta > 0", "Delta ≈ 0", "Delta < 0", "Agresores ask−bid."),
        (9, "marubozu", "Cuerpo >~70% rango alcista", "Cuerpo pequeño", "Cuerpo >~70% rango bajista", "Marubozu confirma dirección."),
        (10, "prev_close", "C > C_prev", "C = C_prev", "C < C_prev", "Cierre vs cierre prev 5m."),
        (11, "prev_break", "C > H_prev", "Dentro rango prev", "C < L_prev", "Rompe high/low de prev."),
        (12, "prev_delta", "Δ > Δ_prev", "Δ ≈ Δ_prev", "Δ < Δ_prev", "Compra más fuerte que prev."),
        (13, "prev_volume", "Vol > prev + cuerpo alcista", "Vol ≤ prev o doji", "Vol > prev + cuerpo bajista", "Vol alto vs prev confirma cuerpo."),
        (14, "prev_poc", "POC > POC_prev", "POC ≈ POC_prev", "POC < POC_prev", "POC migra vs prev."),
        (15, "prev_body", "Cuerpo expandiendo alcista", "Cuerpo ≤ prev o doji", "Cuerpo expandiendo bajista", "body_dir × (size↑)."),
        (16, "vbp_zone_weight", "imbalance > +0.15", "|imbalance| ≤ 0.15", "imbalance < −0.15", "VbP pesos por zona (mechas 3×, near-close 2×)."),
    ]

    fig, ax = plt.subplots(figsize=(14.5, 14.4), dpi=150)
    fig.patch.set_facecolor(C_BG)
    ax.set_facecolor(C_BG)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    ax.text(
        0.5, 0.985,
        "Fuerza de vela 5m  v2  (+n long / −n short)  ·  16 checks",
        ha="center", va="top", fontsize=15, fontweight="bold", color=C_TEXT,
    )
    ax.text(
        0.5, 0.958,
        "16 factores · cada uno vota +1 / 0 / −1 · Score = suma ∈ [−16, +16]  ·  10–15 vs prev · 16 = VbP zone weights",
        ha="center", va="top", fontsize=9.5, color=C_NEU,
    )
    ax.text(
        0.5, 0.938,
        "Sin prev-VWAP. Primera RTH: 10–15 = 0. Check 16 suplementa hvn_shape (no lo reemplaza).",
        ha="center", va="top", fontsize=8.5, color=C_NEU,
    )

    left = 0.02
    widths = [0.035, 0.11, 0.20, 0.17, 0.20, 0.24]
    headers = ["#", "Factor", "+1 (long)", "0 (neutro)", "−1 (short)", "Significado"]
    xs = []
    x = left
    for w in widths:
        xs.append(x)
        x += w

    top = 0.915
    bottom = 0.035
    n_rows = 1 + len(factors)
    row_h = (top - bottom) / n_rows

    y = top - row_h
    for x, w, h in zip(xs, widths, headers):
        ax.add_patch(Rectangle((x, y), w, row_h, facecolor=C_HDR, edgecolor=C_BORDER, linewidth=0.8))
        ax.text(x + w / 2, y + row_h / 2, h, ha="center", va="center", fontsize=8.5, fontweight="bold", color=C_TEXT)

    for i, (num, name, plus, zero, minus, meaning) in enumerate(factors):
        y = top - row_h * (i + 2)
        bg = C_CELL if i % 2 == 0 else C_CELL_ALT
        # highlight prev block
        if num >= 10 and num < 16:
            bg = "#e3f2fd" if i % 2 == 0 else "#bbdefb"
        elif num == 16:
            bg = "#e8f5e9" if i % 2 == 0 else "#c8e6c9"
        cells = [str(num), name, plus, zero, minus, meaning]
        colors = [C_TEXT, C_TEXT, C_POS, C_NEU, C_NEG, C_TEXT]
        for x, w, txt, col in zip(xs, widths, cells, colors):
            ax.add_patch(Rectangle((x, y), w, row_h, facecolor=bg, edgecolor=C_BORDER, linewidth=0.55))
            ax.text(
                x + (0.006 if w > 0.1 else w / 2),
                y + row_h / 2,
                txt,
                ha="left" if w > 0.1 else "center",
                va="center",
                fontsize=7.4 if num >= 10 else 7.6,
                color=col,
                fontweight="bold" if name == txt else "normal",
                wrap=True,
            )

    ax.text(
        0.5, 0.012,
        "Research only · score_candle_force_day_v2.py · locked 1–9 = v1 · +6 prev · +1 VbP zone",
        ha="center", va="bottom", fontsize=7.5, color=C_NEU,
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, facecolor=C_BG, bbox_inches="tight", pad_inches=0.22)
    plt.close(fig)


def vote_color(v: int) -> str:
    if v > 0:
        return C_POS
    if v < 0:
        return C_NEG
    return C_ZERO


def fmt_vote(v: int) -> str:
    return f"{int(v):+d}" if v != 0 else " 0"


def reason_for(name: str, r: pd.Series, prev: pd.Series | None, med: float | None) -> str:
    o, h, lo, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
    rng = h - lo
    body = abs(c - o)
    frac = (body / rng) if rng > 1e-12 else 0.0
    close_pos = ((c - lo) / rng) if rng > 1e-12 else 0.5
    poc = float(r["poc"]) if _finite(r["poc"]) else float("nan")
    val = float(r["val"]) if _finite(r["val"]) else float("nan")
    vah = float(r["vah"]) if _finite(r["vah"]) else float("nan")
    vwap = float(r["vwap"]) if _finite(r["vwap"]) else float("nan")
    hvn_pos = ((poc - lo) / rng) if rng > 1e-12 and _finite(poc) else float("nan")

    if name == "body":
        if int(r["body"]) > 0:
            return f"C>O  cuerpo {body:.2f} alcista"
        if int(r["body"]) < 0:
            return f"C<O  cuerpo {body:.2f} bajista"
        return f"doji  cuerpo {body:.2f}"
    if name == "close_loc":
        v = int(r["close_loc"])
        zone = "tercio alto" if v > 0 else ("tercio bajo" if v < 0 else "tercio medio")
        return f"cierre {zone} ({close_pos * 100:.0f}% del rango)"
    if name == "vs_poc":
        v = int(r["vs_poc"])
        if not _finite(poc):
            return "POC n/a"
        if v == 0:
            return f"C ≈ POC {poc:.2f}"
        return f"C {c:.2f} {'>' if v > 0 else '<'} POC {poc:.2f}"
    if name == "vs_va":
        v = int(r["vs_va"])
        if not _finite(val) or not _finite(vah):
            return "VA n/a"
        if v > 0:
            return f"C {c:.2f} > VAH {vah:.2f}"
        if v < 0:
            return f"C {c:.2f} < VAL {val:.2f}"
        return f"C dentro VA ({val:.2f}–{vah:.2f})"
    if name == "vs_vwap":
        v = int(r["vs_vwap"])
        if not _finite(vwap):
            return "VWAP n/a"
        if v == 0:
            return f"C ≈ VWAP {vwap:.2f}"
        return f"C {c:.2f} {'>' if v > 0 else '<'} VWAP {vwap:.2f}"
    if name == "hvn_shape":
        v = int(r["hvn_shape"])
        if not _finite(hvn_pos):
            return "HVN n/a"
        zone = "tercio alto" if v > 0 else ("tercio bajo" if v < 0 else "tercio medio")
        return f"POC/HVN {zone} ({hvn_pos * 100:.0f}% del rango)"
    if name == "volume":
        vol = float(r["vol"])
        if med is None or not math.isfinite(med):
            return f"vol {vol:.0f}  (sin mediana previa)"
        if int(r["volume"]) == 0:
            if vol > med + 1e-12:
                return f"vol {vol:.0f} > med {med:.0f} pero doji"
            return f"vol {vol:.0f} ≤ mediana previa {med:.0f}"
        side = "alcista" if int(r["volume"]) > 0 else "bajista"
        return f"vol alto {vol:.0f} > med {med:.0f} + cuerpo {side}"
    if name == "delta":
        d = float(r["delta"])
        if abs(d) < 1e-9:
            return "Δ 0  (flat)"
        return f"Δ {d:+.0f}  (ask−bid)"
    if name == "marubozu":
        if int(r["marubozu"]) == 0:
            return f"cuerpo {frac * 100:.0f}% del rango (≤{MARU_FRAC:.0%})"
        side = "alcista" if int(r["marubozu"]) > 0 else "bajista"
        return f"cuerpo {frac * 100:.0f}% del rango (>{MARU_FRAC:.0%}) {side}"
    if name == "vbp_zone_weight":
        imb = float(r["vbp_imbalance"]) if "vbp_imbalance" in r.index and _finite(r["vbp_imbalance"]) else float("nan")
        wl = float(r["w_long"]) if "w_long" in r.index else float("nan")
        ws = float(r["w_short"]) if "w_short" in r.index else float("nan")
        v = int(r["vbp_zone_weight"]) if "vbp_zone_weight" in r.index else 0
        if not math.isfinite(imb):
            return "VbP zone n/a"
        tag = "LONG" if v > 0 else ("SHORT" if v < 0 else "neutro")
        return f"imb {imb:+.3f}  wL={wl:.0f} wS={ws:.0f}  → {tag}"

    # prev checks
    if prev is None:
        return "1ª barra RTH → 0"
    c_p = float(prev["close"])
    h_p = float(prev["high"])
    l_p = float(prev["low"])
    o_p = float(prev["open"])
    d_p = float(prev["delta"])
    v_p = float(prev["vol"])
    poc_p = float(prev["poc"]) if _finite(prev["poc"]) else float("nan")

    if name == "prev_close":
        v = int(r["prev_close"])
        if v == 0:
            return f"C = C_prev {c_p:.2f}"
        return f"C {c:.2f} {'>' if v > 0 else '<'} C_prev {c_p:.2f}"
    if name == "prev_break":
        v = int(r["prev_break"])
        if v > 0:
            return f"C {c:.2f} > H_prev {h_p:.2f}"
        if v < 0:
            return f"C {c:.2f} < L_prev {l_p:.2f}"
        return f"C dentro [{l_p:.2f}–{h_p:.2f}]"
    if name == "prev_delta":
        v = int(r["prev_delta"])
        d = float(r["delta"])
        if v == 0:
            return f"Δ {d:+.0f} ≈ Δ_prev {d_p:+.0f}"
        return f"Δ {d:+.0f} {'>' if v > 0 else '<'} Δ_prev {d_p:+.0f}"
    if name == "prev_volume":
        vol = float(r["vol"])
        v = int(r["prev_volume"])
        if v == 0:
            if vol > v_p + 1e-12:
                return f"vol {vol:.0f} > prev {v_p:.0f} pero doji"
            return f"vol {vol:.0f} ≤ prev {v_p:.0f}"
        side = "alcista" if v > 0 else "bajista"
        return f"vol {vol:.0f} > prev {v_p:.0f} + cuerpo {side}"
    if name == "prev_poc":
        v = int(r["prev_poc"])
        if not _finite(poc) or not _finite(poc_p):
            return "POC n/a"
        if v == 0:
            return f"POC ≈ POC_prev {poc_p:.2f}"
        return f"POC {poc:.2f} {'>' if v > 0 else '<'} POC_prev {poc_p:.2f}"
    if name == "prev_body":
        v = int(r["prev_body"])
        prev_sz = abs(c_p - o_p)
        if int(r["body"]) == 0:
            return f"doji (curr {body:.2f} vs prev {prev_sz:.2f})"
        if v == 0:
            return f"cuerpo {body:.2f} ≤ prev {prev_sz:.2f}"
        side = "alcista" if v > 0 else "bajista"
        return f"cuerpo expande {body:.2f} > prev {prev_sz:.2f} {side}"
    return ""


def draw_candle(ax, x, o, h, lo, c, width=0.55, lw=1.15, alpha=0.94, zorder=4):
    up = c >= o
    col = C_UP if up else C_DN
    ax.plot([x, x], [lo, h], color=C_WICK, lw=lw, solid_capstyle="round", zorder=zorder)
    y0, y1 = min(o, c), max(o, c)
    if abs(y1 - y0) < 1e-9:
        y1 = y0 + TICK * 0.18
    ax.add_patch(
        Rectangle(
            (x - width / 2, y0), width, y1 - y0,
            facecolor=col, edgecolor=col, linewidth=0.7, alpha=alpha, zorder=zorder + 1,
        )
    )


def bw_label(ax, x, y, text, *, ha="left", va="center", fontsize=8.0, zorder=8):
    ax.text(
        x, y, text, color="#000000", fontsize=fontsize, va=va, ha=ha,
        fontweight="bold", zorder=zorder, clip_on=False,
        bbox=dict(boxstyle="round,pad=0.18", facecolor="#ffffff", edgecolor="#333333", lw=0.65, alpha=0.96),
    )


def _bar_vbp(day: date, db: Path, r: pd.Series):
    """Load footprint VbP for one 5m bar."""
    fp = load_footprint(day, db, max_bn=int(r["b1"]))
    va = vbp_va70(fp, int(r["b0"]), int(r["b1"]), float(r["close"]))
    fchunk = fp[(fp["rth_bar_number"] >= int(r["b0"])) & (fp["rth_bar_number"] <= int(r["b1"]))]
    grp = fchunk.groupby("price", sort=True)["total_volume"].sum()
    return va, grp


def draw_zoom_candle_panel(
    ax,
    r: pd.Series,
    va: dict,
    grp,
    *,
    panel_title: str,
    badge_score: int | None = None,
    badge_sub: str | None = None,
    compact: bool = False,
) -> None:
    """Large zoomed candle + this bar's own VbP (POC/VAL/VAH/VWAP)."""
    ax.set_facecolor("#ffffff")
    o, h, lo, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
    poc = float(va["poc"]) if _finite(va["poc"]) else float(r["poc"])
    val = float(va["val"]) if _finite(va["val"]) else float(r["val"])
    vah = float(va["vah"]) if _finite(va["vah"]) else float(r["vah"])
    vwap = float(r["vwap"])
    rng = h - lo
    cx = 0.0
    draw_candle(ax, cx, o, h, lo, c, width=0.78, lw=2.25, alpha=0.95, zorder=5)
    ax.plot([cx - 0.48, cx], [o, o], color="#000000", lw=1.3, zorder=6)
    ax.plot([cx, cx + 0.48], [c, c], color="#000000", lw=1.5, zorder=6)
    ax.scatter([cx + 0.50], [c], s=24, c="#000000", zorder=8, marker="D")
    if _finite(val) and _finite(vah):
        ax.axhspan(val, vah, facecolor="#6a3d9a", alpha=0.07, zorder=1)
    x0 = 0.95
    max_w = 2.35 if compact else 2.55
    if grp is not None and len(grp):
        prices = grp.index.to_numpy(dtype=float)
        vols = grp.to_numpy(dtype=float)
        vmax = float(vols.max()) if float(vols.max()) > 0 else 1.0
        for p, v in zip(prices, vols):
            w = (float(v) / vmax) * max_w
            if w <= 0:
                continue
            is_poc = abs(p - poc) < TICK * 0.5 + 1e-12
            in_va = _finite(val) and _finite(vah) and (val - 1e-12) <= p <= (vah + 1e-12)
            color = C_VBP_POC if is_poc else (C_VBP_VA if in_va else C_VBP)
            alpha = 0.96 if is_poc else (0.82 if in_va else 0.55)
            ax.add_patch(Rectangle(
                (x0, p - TICK / 2.0), w, TICK,
                facecolor=color, edgecolor="none", alpha=alpha, zorder=3,
            ))
        ax.text(
            x0 + max_w * 0.02, h + rng * 0.035 + TICK * 2, "VbP 5m",
            fontsize=7.6 if compact else 8.2, color="#37474f", fontweight="bold",
            va="bottom", ha="left",
        )
    x_left = -0.85
    x_right = x0 + max_w + 0.08
    levels = [
        (vah, C_VA, f"VAH  {vah:.2f}", "bottom"),
        (poc, C_POC, f"POC / HVN  {poc:.2f}", "center"),
        (vwap, C_VWAP, f"VWAP  {vwap:.2f}", "center"),
        (val, C_VA, f"VAL  {val:.2f}", "top"),
    ]
    used_y: list[float] = []
    fs_lvl = 7.4 if compact else 8.2
    for y, col, text, va_txt in levels:
        if not _finite(y):
            continue
        ax.plot(
            [x_left, x_right], [y, y], color=col,
            lw=1.45 if "POC" in text else 1.1,
            ls="-" if "POC" in text else ("-." if "VWAP" in text else "--"),
            zorder=4, solid_capstyle="butt",
        )
        yy = y
        for uy in used_y:
            if abs(yy - uy) < rng * 0.045 + TICK * 2:
                yy = uy + (rng * 0.05 + TICK * 2.2) * (1 if y >= uy else -1)
        used_y.append(yy)
        bw_label(ax, x_right + 0.04, yy, text, ha="left", va=va_txt, fontsize=fs_lvl)
    bw_label(ax, cx + 0.58, c - rng * 0.035, f"C  {c:.2f}", ha="left", va="top",
             fontsize=7.8 if compact else 8.4)
    bw_label(ax, cx - 0.50, o, f"O  {o:.2f}", ha="right", va="top", fontsize=7.2)
    bw_label(ax, cx - 0.50, lo, f"L  {lo:.2f}", ha="right", va="top", fontsize=7.0)
    if abs(h - vah) > TICK * 2:
        bw_label(ax, cx + 0.18, h, f"H  {h:.2f}", ha="left", va="bottom", fontsize=7.0)
    body = abs(c - o)
    frac = body / rng if rng > 1e-12 else 0.0
    box = (
        f"O {o:.2f}\nH {h:.2f}\nL {lo:.2f}\nC {c:.2f}\n"
        f"rango {rng:.2f}   cuerpo {frac * 100:.0f}%\n"
        f"vol {float(r['vol']):,.0f}   Δ {float(r['delta']):+.0f}"
    )
    ax.text(
        0.02, 0.02, box, transform=ax.transAxes, va="bottom", ha="left",
        fontsize=7.4 if compact else 8.2, color="#000000", family="DejaVu Sans Mono",
        bbox=dict(boxstyle="round,pad=0.32", facecolor="#ffffff", edgecolor="#90a4ae", alpha=0.95),
        zorder=9,
    )
    if badge_score is not None:
        bcol = C_POS if badge_score > 0 else (C_NEG if badge_score < 0 else C_ZERO)
        btxt = f"{badge_score:+d}" if badge_score != 0 else "0"
        ax.text(
            0.98, 0.98, btxt, transform=ax.transAxes, ha="right", va="top",
            fontsize=22 if compact else 26, fontweight="bold", color="#ffffff", zorder=12,
            bbox=dict(boxstyle="round,pad=0.28", facecolor=bcol, edgecolor="#000000", lw=0.9),
        )
        if badge_sub:
            ax.text(
                0.98, 0.86, badge_sub, transform=ax.transAxes, ha="right", va="top",
                fontsize=7.2, color="#37474f", zorder=12,
                bbox=dict(boxstyle="round,pad=0.16", facecolor="#ffffff", edgecolor="#90a4ae", lw=0.6),
            )
    pad_lo = rng * 0.12 + TICK * 8
    pad_hi = rng * 0.14 + TICK * 10
    ax.set_ylim(lo - pad_lo, h + pad_hi)
    ax.set_xlim(-1.15, x_right + (1.55 if compact else 1.70))
    ax.set_xticks([])
    ax.set_ylabel("NQU26", color="#000000", fontsize=10 if compact else 11)
    ax.tick_params(colors="#000000", labelsize=8.0)
    for sp in ax.spines.values():
        sp.set_color("#90a4ae")
    ax.grid(True, axis="y", color="#eceff1", lw=0.6, zorder=0)
    ax.set_title(panel_title, color="#000000", fontsize=10.0 if compact else 10.5, loc="left", pad=5)



def draw_shared_axis_dual_candles(
    ax,
    prev: pd.Series,
    curr: pd.Series,
    va_p: dict,
    grp_p,
    va_c: dict,
    grp_c,
    *,
    panel_title: str,
) -> tuple[float, float, float]:
    """Two 5m candles side-by-side on ONE shared price axis (same ylim).

    ylim = min(lows) − pad … max(highs) + pad of the two bars only.
    Prev VbP muted; current VbP + POC/VAL/VAH/VWAP emphasized.
    VbP bar widths share ONE volume scale: vmax = max(max vol prev, max vol curr)
    so the same volume draws the same horizontal length on both columns.
    Returns (ylim0, ylim1, shared_vmax).
    """
    ax.set_facecolor("#ffffff")
    o_p, h_p, l_p, c_p = (
        float(prev["open"]), float(prev["high"]), float(prev["low"]), float(prev["close"])
    )
    o_c, h_c, l_c, c_c = (
        float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
    )
    lo_all = min(l_p, l_c)
    hi_all = max(h_p, h_c)
    rng = max(hi_all - lo_all, TICK * 8)
    pad = rng * 0.08 + TICK * 6
    ylim0 = lo_all - pad
    ylim1 = hi_all + pad

    poc_p = float(va_p["poc"]) if _finite(va_p["poc"]) else float(prev["poc"])
    val_p = float(va_p["val"]) if _finite(va_p["val"]) else float(prev["val"])
    vah_p = float(va_p["vah"]) if _finite(va_p["vah"]) else float(prev["vah"])
    vwap_p = float(prev["vwap"])
    poc_c = float(va_c["poc"]) if _finite(va_c["poc"]) else float(curr["poc"])
    val_c = float(va_c["val"]) if _finite(va_c["val"]) else float(curr["val"])
    vah_c = float(va_c["vah"]) if _finite(va_c["vah"]) else float(curr["vah"])
    vwap_c = float(curr["vwap"])

    # Column layout: PREV candle | muted VbP | gap | CURR candle | emph VbP | labels
    x_prev = 0.0
    x_curr = 4.55
    vbp_max_w = 1.55
    x_vbp_p = x_prev + 0.88
    x_vbp_c = x_curr + 0.88
    x_lab = x_vbp_c + vbp_max_w + 0.12

    # Current VA band across the pane (emphasized)
    if _finite(val_c) and _finite(vah_c):
        ax.axhspan(val_c, vah_c, facecolor="#6a3d9a", alpha=0.06, zorder=1)
    # Prev VA band lightly (muted)
    if _finite(val_p) and _finite(vah_p):
        ax.axhspan(val_p, vah_p, facecolor="#90a4ae", alpha=0.04, zorder=1)

    # Shared VbP volume scale across BOTH candles (same vol → same bar width)
    def _grp_max_vol(grp) -> float:
        if grp is None or not len(grp):
            return 0.0
        m = float(grp.to_numpy(dtype=float).max())
        return m if m > 0 else 0.0

    shared_vmax = max(_grp_max_vol(grp_p), _grp_max_vol(grp_c))
    if shared_vmax <= 0:
        shared_vmax = 1.0

    def _draw_vbp(x0, grp, poc, val, vah, *, ohlc, muted: bool, vmax: float) -> None:
        """VbP bars colored by Luis zone (lower wick / body / near-close / upper wick).
        Width ∝ vol with shared vmax. POC outline kept via gold edge when muted=False.
        """
        if grp is None or not len(grp):
            return
        oo, hh, ll, cc = ohlc
        doji = is_doji_body(oo, cc, ll, hh)
        prices = grp.index.to_numpy(dtype=float)
        vols = grp.to_numpy(dtype=float)
        for p, v in zip(prices, vols):
            w = (float(v) / vmax) * vbp_max_w
            if w <= 0:
                continue
            is_poc = abs(p - poc) < TICK * 0.5 + 1e-12
            zcol = zone_color_for_price(float(p), o=oo, c=cc, lo=ll, hi=hh, doji=doji)
            if muted:
                # desaturate toward gray while keeping zone hue readable
                color = zcol
                alpha = 0.38 if not is_poc else 0.55
                edge = "#c9a227" if is_poc else "none"
                elw = 0.7 if is_poc else 0.0
            else:
                color = zcol
                alpha = 0.92 if not is_poc else 0.98
                edge = "#c9a227" if is_poc else "none"
                elw = 1.0 if is_poc else 0.0
            ax.add_patch(Rectangle(
                (x0, p - TICK / 2.0), w, TICK,
                facecolor=color, edgecolor=edge, linewidth=elw, alpha=alpha, zorder=3,
            ))

    _draw_vbp(x_vbp_p, grp_p, poc_p, val_p, vah_p, ohlc=(o_p, h_p, l_p, c_p), muted=True, vmax=shared_vmax)
    _draw_vbp(x_vbp_c, grp_c, poc_c, val_c, vah_c, ohlc=(o_c, h_c, l_c, c_c), muted=False, vmax=shared_vmax)

    ax.text(
        x_vbp_p + 0.02, hi_all + pad * 0.22, "VbP prev",
        fontsize=7.0, color="#78909c", fontweight="bold", va="bottom", ha="left",
    )
    ax.text(
        x_vbp_c + 0.02, hi_all + pad * 0.22, "VbP actual",
        fontsize=7.4, color="#37474f", fontweight="bold", va="bottom", ha="left",
    )

    draw_candle(ax, x_prev, o_p, h_p, l_p, c_p, width=0.72, lw=2.0, alpha=0.92, zorder=5)
    draw_candle(ax, x_curr, o_c, h_c, l_c, c_c, width=0.78, lw=2.25, alpha=0.95, zorder=5)
    ax.plot([x_prev - 0.42, x_prev], [o_p, o_p], color="#000000", lw=1.15, zorder=6)
    ax.plot([x_prev, x_prev + 0.42], [c_p, c_p], color="#000000", lw=1.35, zorder=6)
    ax.scatter([x_prev + 0.44], [c_p], s=18, c="#000000", zorder=8, marker="D")
    ax.plot([x_curr - 0.45, x_curr], [o_c, o_c], color="#000000", lw=1.3, zorder=6)
    ax.plot([x_curr, x_curr + 0.45], [c_c, c_c], color="#000000", lw=1.5, zorder=6)
    ax.scatter([x_curr + 0.47], [c_c], s=22, c="#000000", zorder=8, marker="D")

    # Current levels emphasized (full-width dashed into label zone)
    x_left = x_prev - 0.95
    levels_c = [
        (vah_c, C_VA, f"VAH  {vah_c:.2f}", "bottom", "--"),
        (poc_c, C_POC, f"POC / HVN  {poc_c:.2f}", "center", "-"),
        (vwap_c, C_VWAP, f"VWAP  {vwap_c:.2f}", "center", "-."),
        (val_c, C_VA, f"VAL  {val_c:.2f}", "top", "--"),
    ]
    used_y: list[float] = []
    for y, col, text, va_txt, ls in levels_c:
        if not _finite(y):
            continue
        ax.plot(
            [x_left, x_lab], [y, y], color=col,
            lw=1.55 if "POC" in text else 1.15, ls=ls, zorder=4, solid_capstyle="butt",
            alpha=0.95,
        )
        yy = y
        for uy in used_y:
            if abs(yy - uy) < rng * 0.038 + TICK * 2:
                yy = uy + (rng * 0.042 + TICK * 2.0) * (1 if y >= uy else -1)
        used_y.append(yy)
        bw_label(ax, x_lab + 0.05, yy, text, ha="left", va=va_txt, fontsize=7.6)

    # Prev key levels as short muted ticks near prev column (readable but soft)
    for y, lab in ((poc_p, f"POCₚ {poc_p:.2f}"), (vwap_p, f"VWAPₚ {vwap_p:.2f}")):
        if not _finite(y):
            continue
        ax.plot(
            [x_prev - 0.55, x_vbp_p + vbp_max_w * 0.55], [y, y],
            color="#90a4ae", lw=0.9, ls=":", zorder=3, alpha=0.85,
        )
        bw_label(ax, x_prev - 0.58, y, lab, ha="right", va="center", fontsize=6.4)

    # OHLC tags (black on white)
    bw_label(ax, x_curr + 0.55, c_c - rng * 0.02, f"C  {c_c:.2f}", ha="left", va="top", fontsize=7.6)
    bw_label(ax, x_curr - 0.48, o_c, f"O  {o_c:.2f}", ha="right", va="top", fontsize=6.8)
    bw_label(ax, x_curr - 0.48, l_c, f"L  {l_c:.2f}", ha="right", va="top", fontsize=6.6)
    if abs(h_c - vah_c) > TICK * 2 or not _finite(vah_c):
        bw_label(ax, x_curr + 0.18, h_c, f"H  {h_c:.2f}", ha="left", va="bottom", fontsize=6.6)
    bw_label(ax, x_prev + 0.52, c_p, f"C  {c_p:.2f}", ha="left", va="bottom", fontsize=6.8)
    bw_label(ax, x_prev - 0.48, l_p, f"L  {l_p:.2f}", ha="right", va="top", fontsize=6.4)

    # Small score badges above each candle
    score_p = int(prev["score"])
    score_c = int(curr["score"])
    for x, sc in ((x_prev, score_p), (x_curr, score_c)):
        bcol = C_POS if sc > 0 else (C_NEG if sc < 0 else C_ZERO)
        btxt = f"{sc:+d}" if sc != 0 else "0"
        ax.text(
            x, hi_all + pad * 0.55, btxt, ha="center", va="center",
            fontsize=14, fontweight="bold", color="#ffffff", zorder=12,
            bbox=dict(boxstyle="round,pad=0.22", facecolor=bcol, edgecolor="#000000", lw=0.8),
        )

    # Column captions
    t_prev = str(prev["time"])
    t_end_p = str(prev["t_end"])[-8:-3]
    t_curr = str(curr["time"])
    t_end_c = str(curr["t_end"])[-8:-3]
    ax.text(
        x_prev, ylim0 + pad * 0.12,
        f"PREV  {t_prev}–{t_end_p}\nfp [{int(prev['b0'])}–{int(prev['b1'])}]\n"
        f"vol {float(prev['vol']):,.0f}  Δ {float(prev['delta']):+.0f}",
        ha="center", va="bottom", fontsize=7.0, color="#000000", family="DejaVu Sans Mono",
        bbox=dict(boxstyle="round,pad=0.28", facecolor="#ffffff", edgecolor="#90a4ae", alpha=0.95),
        zorder=9,
    )
    ax.text(
        x_curr, ylim0 + pad * 0.12,
        f"ACTUAL  {t_curr}–{t_end_c}\nfp [{int(curr['b0'])}–{int(curr['b1'])}]\n"
        f"vol {float(curr['vol']):,.0f}  Δ {float(curr['delta']):+.0f}",
        ha="center", va="bottom", fontsize=7.2, color="#000000", family="DejaVu Sans Mono",
        bbox=dict(boxstyle="round,pad=0.28", facecolor="#ffffff", edgecolor="#1565c0", alpha=0.95),
        zorder=9,
    )

    # Zone-weight annotations (w_long / w_short / imbalance / check 16 vote)
    def _zw_box(x, series, *, edge, title):
        if "w_long" not in series.index:
            return
        wl = float(series["w_long"])
        ws = float(series["w_short"])
        imb = float(series["vbp_imbalance"]) if "vbp_imbalance" in series.index else float("nan")
        vv = int(series["vbp_zone_weight"]) if "vbp_zone_weight" in series.index else 0
        fight = float(series["w_body_fight"]) if "w_body_fight" in series.index else float("nan")
        vcol = C_POS if vv > 0 else (C_NEG if vv < 0 else C_ZERO)
        txt = (
            f"{title}\n"
            f"w_long  {wl:,.0f}\n"
            f"w_short {ws:,.0f}\n"
            f"fight   {fight:,.0f}\n"
            f"imb {imb:+.3f}\n"
            f"check16 {vv:+d}"
        )
        ax.text(
            x, ylim1 - pad * 0.08, txt,
            ha="center", va="top", fontsize=6.6, color="#000000", family="DejaVu Sans Mono",
            bbox=dict(boxstyle="round,pad=0.30", facecolor="#ffffff", edgecolor=vcol, lw=1.2, alpha=0.96),
            zorder=11,
        )

    _zw_box(x_prev + 0.15, prev, edge="#90a4ae", title="VbP pesos PREV")
    _zw_box(x_curr + 0.15, curr, edge="#2e7d32", title="VbP pesos ACTUAL")

    # Compact zone color legend (top-left of pane)
    legend_items = [
        (ZONE_COLORS["lower_wick"], "mecha↓ 3× +"),
        (ZONE_COLORS["near_close"], "near-C 2×"),
        (ZONE_COLORS["body_long"], "body↓mid 1×"),
        (ZONE_COLORS["body_short"], "body↑mid 1×"),
        (ZONE_COLORS["upper_wick"], "mecha↑ 3× −"),
    ]
    lx, ly = x_left + 0.05, ylim1 - pad * 0.02
    for i, (col, lab) in enumerate(legend_items):
        yy = ly - i * (rng * 0.028 + TICK * 1.2)
        ax.add_patch(Rectangle((lx, yy - TICK * 0.35), 0.22, TICK * 0.7,
                               facecolor=col, edgecolor="#000000", lw=0.4, zorder=10, alpha=0.9))
        ax.text(lx + 0.28, yy, lab, fontsize=5.8, color="#000000", va="center", ha="left", zorder=10,
                bbox=dict(boxstyle="round,pad=0.08", facecolor="#ffffff", edgecolor="none", alpha=0.85))

    ax.set_ylim(ylim0, ylim1)
    ax.set_xlim(x_left - 0.15, x_lab + 2.35)
    ax.set_xticks([x_prev, x_curr])
    ax.set_xticklabels(
        [f"{t_prev} CT", f"{t_curr} CT"],
        fontsize=9.0, color="#000000", fontweight="bold",
    )
    ax.set_ylabel("NQU26  (eje compartido)", color="#000000", fontsize=10.5)
    ax.tick_params(colors="#000000", labelsize=8.2)
    for sp in ax.spines.values():
        sp.set_color("#90a4ae")
    ax.grid(True, axis="y", color="#eceff1", lw=0.6, zorder=0)
    ax.set_title(panel_title, color="#000000", fontsize=10.2, loc="left", pad=5)
    return ylim0, ylim1, shared_vmax


def _draw_context_strip(ax, df: pd.DataFrame, focus_bars: list[int], highlight: set[int]) -> None:
    ax.set_facecolor("#ffffff")
    lo_i = max(1, min(focus_bars) - 1)
    hi_i = max(focus_bars) + 1
    win = df[df["m5_bar"].between(lo_i, hi_i)].sort_values("m5_bar")
    # y-lim from highlighted bars
    foc = df[df["m5_bar"].isin(highlight)]
    flo, fhi = float(foc["low"].min()), float(foc["high"].max())
    frng = max(fhi - flo, TICK * 8)
    ylim0 = flo - frng * 0.28 - TICK * 8
    ylim1 = fhi + frng * 0.38 + TICK * 10
    for i, (_, row) in enumerate(win.iterrows()):
        x = float(i)
        o, h, lo, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        is_focus = int(row["m5_bar"]) in highlight
        draw_candle(
            ax, x, o, h, lo, c,
            width=0.62 if is_focus else 0.44,
            lw=1.35 if is_focus else 0.85,
            alpha=0.96 if is_focus else 0.48,
        )
        sc = int(row["score"])
        col = vote_color(sc)
        label = f"{sc:+d}" if sc != 0 else "0"
        y_sc = min(max(h, ylim0 + frng * 0.08), ylim1 - frng * 0.10) + frng * 0.045
        ax.text(
            x, y_sc, label, ha="center", va="bottom",
            fontsize=8.6 if is_focus else 7.0, color=col, fontweight="bold", zorder=6, clip_on=True,
        )
        if is_focus:
            ax.add_patch(Rectangle(
                (x - 0.42, float(row["low"]) - frng * 0.05),
                0.84, (float(row["high"]) - float(row["low"])) + frng * 0.14,
                facecolor="none", edgecolor=C_FOCUS, linewidth=1.5, zorder=7,
            ))
    ax.set_ylim(ylim0, ylim1)
    ax.set_xlim(-0.7, max(len(win) - 0.25, 1.0))
    ax.set_xticks([float(i) for i in range(len(win))])
    ax.set_xticklabels([str(t) for t in win["time"]], fontsize=8.0, color="#000000")
    ax.set_ylabel("ctx ±1", color="#546e7a", fontsize=8)
    ax.tick_params(axis="y", colors="#000000", labelsize=7.2)
    ax.tick_params(axis="x", colors="#000000", labelsize=8.0, length=0)
    for sp in ax.spines.values():
        sp.set_color("#cfd8dc")
    ax.grid(True, axis="y", color="#eceff1", lw=0.55, zorder=0)
    ax.set_title(
        "5m vecinos (foco recuadrado · scores v2)",
        color="#37474f", fontsize=9, loc="left", pad=3,
    )


def _draw_checks_table(
    ax_tb,
    r: pd.Series,
    prev: pd.Series | None,
    med: float | None,
    *,
    emphasize_prev: bool = True,
) -> None:
    ax_tb.set_facecolor("#ffffff")
    ax_tb.set_xlim(0, 1)
    ax_tb.set_ylim(0, 1)
    ax_tb.axis("off")
    score = int(r["score"])
    badge_col = C_POS if score > 0 else (C_NEG if score < 0 else C_ZERO)
    badge_txt = f"{score:+d}" if score != 0 else "0"
    ax_tb.add_patch(FancyBboxPatch(
        (0.06, 0.895), 0.88, 0.090,
        boxstyle="round,pad=0.014,rounding_size=0.03",
        facecolor=badge_col, edgecolor="#000000", linewidth=1.0, mutation_aspect=0.6, zorder=2,
    ))
    ax_tb.text(0.50, 0.955, badge_txt, ha="center", va="center",
               fontsize=32, fontweight="bold", color="#ffffff", zorder=3)
    ax_tb.text(0.50, 0.905, "fuerza v2  ·  16 checks", ha="center", va="center",
               fontsize=8.2, color="#ffffff", zorder=3)

    y0 = 0.875
    ax_tb.text(0.03, y0, "check", fontsize=7.4, fontweight="bold", color="#37474f", va="center")
    ax_tb.text(0.27, y0, "voto", fontsize=7.4, fontweight="bold", color="#37474f", va="center")
    ax_tb.text(0.38, y0, "razón", fontsize=7.4, fontweight="bold", color="#37474f", va="center")
    ax_tb.plot([0.02, 0.98], [y0 - 0.015, y0 - 0.015], color="#cfd8dc", lw=0.7)

    row_h = 0.0485
    top = y0 - 0.028
    for i, (label, col, key) in enumerate(CHECKS16):
        y = top - i * row_h
        v = int(r[col])
        colr = vote_color(v)
        is_prev = 9 <= i < 15
        is_vbp = i == 15
        if emphasize_prev and is_prev:
            bg = "#e3f2fd" if i % 2 == 0 else "#bbdefb"
        elif is_vbp:
            bg = "#e8f5e9" if i % 2 == 0 else "#c8e6c9"
        else:
            bg = "#f7f9fa" if i % 2 == 0 else "#eef2f4"
        ax_tb.add_patch(Rectangle(
            (0.015, y - row_h * 0.42), 0.97, row_h * 0.84,
            facecolor=bg, edgecolor="none", zorder=1,
        ))
        if emphasize_prev and is_prev:
            ax_tb.add_patch(Rectangle(
                (0.015, y - row_h * 0.42), 0.012, row_h * 0.84,
                facecolor=C_FOCUS, edgecolor="none", zorder=2,
            ))
        if is_vbp:
            ax_tb.add_patch(Rectangle(
                (0.015, y - row_h * 0.42), 0.012, row_h * 0.84,
                facecolor="#2e7d32", edgecolor="none", zorder=2,
            ))
        ax_tb.text(0.03, y, label, fontsize=7.5, fontweight="bold", color="#000000",
                   va="center", family="DejaVu Sans Mono", zorder=3)
        ax_tb.text(0.30, y, fmt_vote(v), fontsize=10.0, fontweight="bold", color=colr,
                   va="center", ha="center", family="DejaVu Sans Mono", zorder=3)
        ax_tb.text(0.38, y, reason_for(key, r, prev, med), fontsize=6.5, color="#212121",
                   va="center", zorder=3)

    ax_tb.plot([0.02, 0.98], [0.035, 0.035], color="#cfd8dc", lw=0.7)
    ax_tb.text(
        0.03, 0.015,
        f"suma = {score:+d}   ·   score9={int(r['score9']):+d}   ·   v2 = 9 locked + 6 prev + VbP zone",
        fontsize=7.0, color="#546e7a", va="center",
    )


def draw_one_preview(
    df: pd.DataFrame,
    r: pd.Series,
    out_png: Path,
    day: date,
    db: Path,
    *,
    with_prev: bool = False,
) -> tuple[float, float, float] | None:
    """ONE preview chart with all 16 checks listed.

    with_prev=True → ONE shared price axis (PREV + CURRENT side-by-side, same ylim)
    + shared VbP volume scale (one vmax across both profiles)
    + 16-check table (10–15 prev; 16 = vbp_zone_weight).
    Returns (ylim0, ylim1, shared_vmax) in dual/shared mode, else None.
    """
    m5 = int(r["m5_bar"])
    prev = df[df["m5_bar"] == m5 - 1].iloc[0] if m5 > 1 else None
    prior = df[df["m5_bar"] < m5]["vol"].astype(float)
    med = float(np.median(prior.to_numpy())) if len(prior) else None
    ylim_used: tuple[float, float, float] | None = None

    if with_prev and prev is not None:
        va_c, grp_c = _bar_vbp(day, db, r)
        va_p, grp_p = _bar_vbp(day, db, prev)
        score = int(r["score"])
        score_p = int(prev["score"])
        t_prev = str(prev["time"])
        t_curr = str(r["time"])

        fig = plt.figure(figsize=(20.0, 13.6), facecolor="#ffffff")
        gs = fig.add_gridspec(
            2, 2,
            height_ratios=[0.62, 4.15],
            width_ratios=[2.55, 1.15],
            hspace=0.18, wspace=0.10,
            left=0.045, right=0.985, top=0.900, bottom=0.035,
        )
        ax_ctx = fig.add_subplot(gs[0, :])
        ax_px = fig.add_subplot(gs[1, 0])
        ax_tb = fig.add_subplot(gs[1, 1])

        fig.suptitle(
            f"Fuerza v2 · prev {t_prev} vs actual {t_curr} · U26 {r['trading_day']}  ·  mismo eje de precio",
            color="#000000", fontsize=15.0, fontweight="bold", y=0.968,
        )
        fig.text(
            0.5, 0.928,
            f"dos velas 5m en UN pane (ylim + VbP vmax compartidos)  ·  VbP prev muted  ·  "
            f"16 checks en actual (azul = votos prev 10–15)  ·  prev={score_p:+d}  actual={score:+d}",
            ha="center", va="center", fontsize=9.0, color="#546e7a",
        )

        _draw_context_strip(ax_ctx, df, [m5 - 1, m5], {m5 - 1, m5})

        ylim_used = draw_shared_axis_dual_candles(
            ax_px, prev, r, va_p, grp_p, va_c, grp_c,
            panel_title=(
                f"PREV {t_prev} + ACTUAL {t_curr}  ·  shared y-scale + shared VbP vmax  "
                f"(min low − pad → max high + pad; same vol → same bar width)"
            ),
        )
        _draw_checks_table(ax_tb, r, prev, med, emphasize_prev=True)
    else:
        va, grp = _bar_vbp(day, db, r)
        score = int(r["score"])
        sign = f"+{score}" if score > 0 else str(score)
        fig = plt.figure(figsize=(16.6, 14.2), facecolor="#ffffff")
        gs = fig.add_gridspec(
            2, 2,
            height_ratios=[0.78, 4.0],
            width_ratios=[1.65, 1.15],
            hspace=0.16, wspace=0.10,
            left=0.05, right=0.975, top=0.910, bottom=0.035,
        )
        ax_ctx = fig.add_subplot(gs[0, :])
        ax_px = fig.add_subplot(gs[1, 0])
        ax_tb = fig.add_subplot(gs[1, 1])

        fig.suptitle(
            f"Fuerza v2  {sign}  ·  {r['trading_day']}  U26  {r['time']}",
            color="#000000", fontsize=16.5, fontweight="bold", y=0.975,
        )
        fig.text(
            0.5, 0.942,
            f"un candle 5m  ·  VbP de ESTA vela  ·  16 checks  (+1 / 0 / −1)  ·  score9={int(r['score9']):+d}",
            ha="center", va="center", fontsize=9.2, color="#546e7a",
        )
        # context ±2 for single mode
        lo_i = max(1, m5 - 2)
        hi_i = m5 + 2
        win = df[df["m5_bar"].between(lo_i, hi_i)].sort_values("m5_bar")
        flo, fhi = float(r["low"]), float(r["high"])
        frng = max(fhi - flo, TICK * 8)
        ylim0 = flo - frng * 0.28 - TICK * 8
        ylim1 = fhi + frng * 0.38 + TICK * 10
        ax_ctx.set_facecolor("#ffffff")
        for i, (_, row) in enumerate(win.iterrows()):
            x = float(i)
            o, h, lo, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            is_focus = int(row["m5_bar"]) == m5
            draw_candle(ax_ctx, x, o, h, lo, c, width=0.64 if is_focus else 0.46,
                        lw=1.45 if is_focus else 0.85, alpha=0.96 if is_focus else 0.50)
            sc = int(row["score"])
            col = vote_color(sc)
            label = f"{sc:+d}" if sc != 0 else "0"
            y_sc = min(max(h, ylim0 + frng * 0.08), ylim1 - frng * 0.10) + frng * 0.045
            ax_ctx.text(x, y_sc, label, ha="center", va="bottom",
                        fontsize=9.0 if is_focus else 7.4, color=col, fontweight="bold", zorder=6, clip_on=True)
            if is_focus:
                ax_ctx.add_patch(Rectangle(
                    (x - 0.44, flo - frng * 0.06), 0.88, (fhi - flo) + frng * 0.16,
                    facecolor="none", edgecolor=C_FOCUS, linewidth=1.7, zorder=7,
                ))
        ax_ctx.set_ylim(ylim0, ylim1)
        ax_ctx.set_xlim(-0.7, max(len(win) - 0.25, 1.0))
        ax_ctx.set_xticks([float(i) for i in range(len(win))])
        ax_ctx.set_xticklabels([str(t) for t in win["time"]], fontsize=8.0, color="#000000")
        ax_ctx.set_ylabel("contexto ±2", color="#546e7a", fontsize=8)
        ax_ctx.tick_params(axis="y", colors="#000000", labelsize=7.5)
        ax_ctx.tick_params(axis="x", colors="#000000", labelsize=8.0, length=0)
        for sp in ax_ctx.spines.values():
            sp.set_color("#cfd8dc")
        ax_ctx.grid(True, axis="y", color="#eceff1", lw=0.55, zorder=0)
        ax_ctx.set_title(
            "5m vecinos (foco recuadrado · scores v2)",
            color="#37474f", fontsize=9, loc="left", pad=3,
        )

        t_end = str(r["t_end"])[-8:-3]
        draw_zoom_candle_panel(
            ax_px, r, va, grp,
            panel_title=f"vela 5m  {r['time']}–{t_end}  CT   ·   footprint 15s [{int(r['b0'])}–{int(r['b1'])}]",
            badge_score=None,
            compact=False,
        )
        _draw_checks_table(ax_tb, r, prev, med, emphasize_prev=True)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=145, facecolor="#ffffff")
    plt.close(fig)
    return ylim_used


def write_preview_txt(
    out_txt: Path,
    r: pd.Series,
    prev: pd.Series | None,
    out_png: Path,
    *,
    shared_ylim: tuple[float, float] | None = None,
    shared_vmax: float | None = None,
) -> None:
    """Note both bars' scores, VbP zone weights, and the 6 prev-check votes."""
    s15 = int(r["score15"]) if "score15" in r.index else int(r["score"]) - int(r.get("vbp_zone_weight", 0))
    lines = [
        "CF score v2 ONE preview (shared ylim + shared VbP vmax + zone-colored VbP)",
        f"day,{r['trading_day']}",
        "contract,U26",
        f"current_time,{r['time']}",
        f"current_m5_bar,{int(r['m5_bar'])}",
        f"current_score16,{int(r['score']):+d}",
        f"current_score15,{s15:+d}",
        f"current_score9,{int(r['score9']):+d}",
        f"current_vbp_zw,{int(r['vbp_zone_weight']):+d}" if "vbp_zone_weight" in r.index else "current_vbp_zw,0",
        f"current_w_long,{float(r['w_long']):.4f}" if "w_long" in r.index else "current_w_long,",
        f"current_w_short,{float(r['w_short']):.4f}" if "w_short" in r.index else "current_w_short,",
        f"current_imbalance,{float(r['vbp_imbalance']):+.6f}" if "vbp_imbalance" in r.index else "current_imbalance,",
        f"current_ohlc,{float(r['open']):.2f}/{float(r['high']):.2f}/{float(r['low']):.2f}/{float(r['close']):.2f}",
        f"current_vol,{float(r['vol']):.0f}",
        f"current_delta,{float(r['delta']):+.0f}",
    ]
    if prev is not None:
        ps15 = int(prev["score15"]) if "score15" in prev.index else int(prev["score"]) - int(prev.get("vbp_zone_weight", 0))
        lines += [
            f"prev_time,{prev['time']}",
            f"prev_m5_bar,{int(prev['m5_bar'])}",
            f"prev_score16,{int(prev['score']):+d}",
            f"prev_score15,{ps15:+d}",
            f"prev_score9,{int(prev['score9']):+d}",
            f"prev_vbp_zw,{int(prev['vbp_zone_weight']):+d}" if "vbp_zone_weight" in prev.index else "prev_vbp_zw,0",
            f"prev_w_long,{float(prev['w_long']):.4f}" if "w_long" in prev.index else "prev_w_long,",
            f"prev_w_short,{float(prev['w_short']):.4f}" if "w_short" in prev.index else "prev_w_short,",
            f"prev_imbalance,{float(prev['vbp_imbalance']):+.6f}" if "vbp_imbalance" in prev.index else "prev_imbalance,",
            f"prev_ohlc,{float(prev['open']):.2f}/{float(prev['high']):.2f}/{float(prev['low']):.2f}/{float(prev['close']):.2f}",
            f"prev_vol,{float(prev['vol']):.0f}",
            f"prev_delta,{float(prev['delta']):+.0f}",
            "prev_note,first RTH bar → checks 10–15 = 0; check 16 vbp_zone_weight still applies",
        ]
    lines.append("prev_checks_on_current:")
    for name in VOTE_PREV_COLS:
        lines.append(f"  {name},{int(r[name]):+d}")
    if shared_ylim is not None:
        lines.append(f"shared_ylim,{shared_ylim[0]:.2f}..{shared_ylim[1]:.2f}")
    if shared_vmax is not None:
        lines.append(f"shared_vmax,{shared_vmax:.6g}")
        lines.append("vbp_scale,shared_max_vol_across_both_profiles")
        lines.append("vbp_color,by_zone_lower_wick_body_near_close_upper_wick")
    lines += [
        f"path,{out_png}",
        "exact,yes",
        "note,Aug 13 U26 08:35 v2+check16 with prev 08:30 · shared ylim/vmax · zone-colored VbP",
    ]
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_summary(df: pd.DataFrame, day: date) -> None:
    scores = df["score"]
    print(f"=== CANDLE FORCE v2 {day.isoformat()} NQU26 ===")
    print(f"n_bars={len(df)}  mean_score={scores.mean():.3f}  std_score={scores.std(ddof=0):.3f}")
    print(f"end_cum_score={int(df['cum_score'].iloc[-1])}")
    print("v2 score distribution:")
    vc = scores.value_counts().sort_index()
    for s, n in vc.items():
        print(f"  {int(s):+d}: {int(n)}")
    abs_max = int(scores.abs().max())
    imax = int(scores.abs().idxmax())
    rmax = df.loc[imax]
    print(
        f"max |score|={abs_max}  @ {rmax['time']}  score={int(rmax['score']):+d}  "
        f"score9={int(rmax['score9']):+d}  "
        f"OHLC={rmax['open']:.2f}/{rmax['high']:.2f}/{rmax['low']:.2f}/{rmax['close']:.2f}"
    )
    print("--- Aug13 08:30 / 08:35 / 08:40  score9 / score15 / score16 ---")
    for t in ("08:30", "08:35", "08:40"):
        hit = df[df["time"] == t]
        if hit.empty:
            print(f"  {t}  MISSING")
            continue
        r = hit.iloc[0]
        s15 = int(r["score15"]) if "score15" in r.index else int(r["score"]) - int(r.get("vbp_zone_weight", 0))
        print(
            f"  {t}  score9={int(r['score9']):+d}  score15={s15:+d}  score16={int(r['score']):+d}  "
            f"vbp_zw={int(r.get('vbp_zone_weight', 0)):+d}  "
            f"OHLC={r['open']:.2f}/{r['high']:.2f}/{r['low']:.2f}/{r['close']:.2f}  "
            f"vol={r['vol']:.0f}  dlt={r['delta']:.0f}"
        )
        if "w_long" in r.index:
            print(
                f"    vbp: w_long={float(r['w_long']):,.1f} w_short={float(r['w_short']):,.1f} "
                f"fight={float(r['w_body_fight']):,.1f} imb={float(r['vbp_imbalance']):+.4f} "
                f"lw={float(r['vol_lower_wick']):.0f} uw={float(r['vol_upper_wick']):.0f} "
                f"nc={float(r['vol_near_close']):.0f}"
            )
        print(
            f"    base: body={int(r['body'])} close_loc={int(r['close_loc'])} "
            f"vs_poc={int(r['vs_poc'])} vs_va={int(r['vs_va'])} vs_vwap={int(r['vs_vwap'])} "
            f"hvn={int(r['hvn_shape'])} volume={int(r['volume'])} "
            f"delta={int(r['delta_vote'])} maru={int(r['marubozu'])}"
        )
        print(
            f"    prev: close={int(r['prev_close'])} break={int(r['prev_break'])} "
            f"delta={int(r['prev_delta'])} volume={int(r['prev_volume'])} "
            f"poc={int(r['prev_poc'])} body={int(r['prev_body'])}"
        )


def pick_preview_row(df: pd.DataFrame) -> pd.Series:
    """Prefer 08:35 if |S| still interesting (≥7), else best |S| v2."""
    hit = df[df["time"] == "08:35"]
    if not hit.empty:
        r = hit.iloc[0]
        if abs(int(r["score"])) >= 7:
            return r
    return df.loc[df["score"].abs().idxmax()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Score RTH 5m candles with candle-force v2 (16 checks)")
    ap.add_argument("--day", default=DEFAULT_DAY.isoformat(), help="YYYY-MM-DD (Chicago trading day)")
    ap.add_argument("--contract", default="NQU26", help="contract label (informational)")
    ap.add_argument("--db", type=Path, default=None, help="DuckDB path")
    ap.add_argument(
        "--with-prev",
        action="store_true",
        help="ONE preview: PREV + CURRENT on shared price axis (gallery-ready)",
    )
    ap.add_argument(
        "--preview-only",
        action="store_true",
        help="Only redraw ONE preview (+ txt); skip day chart/rubric/csv rewrite",
    )
    args = ap.parse_args()
    day = date.fromisoformat(args.day)
    db = pick_db(args.db)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)

    csv_path = OUT_DIR / f"CANDLE_FORCE_v2_{day.isoformat()}_bars.csv"
    if args.preview_only and csv_path.exists():
        df = pd.read_csv(csv_path)
    else:
        df = score_day_v2(day, db)

    png_path = OUT_DIR / f"CANDLE_FORCE_v2_{day.isoformat()}.png"
    rubric_path = OUT_DIR / "CANDLE_FORCE_v2_rubric.png"
    preview_path = GALLERY_DIR / "CF_score_v2_ONE_preview.png"
    preview_txt = GALLERY_DIR / "CF_score_v2_ONE_preview.txt"

    if not args.preview_only:
        df.to_csv(csv_path, index=False)
        draw_chart(df, day, png_path)
        draw_rubric_png(rubric_path)

    prev_row = pick_preview_row(df)
    # Prefer exact 08:35 when present (locked research bar)
    hit835 = df[df["time"] == "08:35"]
    if not hit835.empty:
        prev_row = hit835.iloc[0]
    ylim_used = draw_one_preview(
        df, prev_row, preview_path, day, db, with_prev=bool(args.with_prev)
    )
    m5 = int(prev_row["m5_bar"])
    prev_bar = df[df["m5_bar"] == m5 - 1].iloc[0] if m5 > 1 else None
    shared_ylim_t = (ylim_used[0], ylim_used[1]) if ylim_used is not None else None
    shared_vmax_v = float(ylim_used[2]) if ylim_used is not None else None
    write_preview_txt(
        preview_txt, prev_row, prev_bar, preview_path,
        shared_ylim=shared_ylim_t, shared_vmax=shared_vmax_v,
    )

    # Matrix legend (always refresh)
    matrix_png = OUT_DIR / "CANDLE_FORCE_vbp_weight_matrix.png"
    draw_matrix_legend_png(matrix_png)

    # Small CSV for 08:30 + 08:35 zone totals
    focus = df[df["time"].isin(["08:30", "08:35"])].copy()
    zone_cols = [
        "trading_day", "time", "m5_bar", "open", "high", "low", "close", "vol",
        "w_long", "w_short", "w_body_fight", "vbp_imbalance", "vbp_zone_weight",
        "vol_lower_wick", "vol_upper_wick", "vol_near_close", "vol_body_fight",
        "vbp_is_doji", "hvn_shape", "score9", "score",
    ]
    zone_cols = [c for c in zone_cols if c in focus.columns]
    zone_csv = OUT_DIR / f"CANDLE_FORCE_vbp_weights_{day.isoformat()}_0830_0835.csv"
    if not focus.empty:
        focus[zone_cols].to_csv(zone_csv, index=False)

    print(matrix_summary_text())
    print(f"wrote {matrix_png}")
    if not focus.empty:
        print(f"wrote {zone_csv}")
        for _, rr in focus.iterrows():
            print(
                f"  {rr['time']}  w_long={float(rr['w_long']):,.1f}  w_short={float(rr['w_short']):,.1f}  "
                f"fight={float(rr['w_body_fight']):,.1f}  imb={float(rr['vbp_imbalance']):+.4f}  "
                f"vbp_zw={int(rr['vbp_zone_weight']):+d}  score16={int(rr['score']):+d}  "
                f"hvn={int(rr['hvn_shape']):+d}"
            )

    if not args.preview_only:
        print_summary(df, day)
    print(
        f"ONE preview → {prev_row['time']}  score16={int(prev_row['score']):+d}  "
        f"score9={int(prev_row['score9']):+d}  with_prev={bool(args.with_prev)}"
    )
    if prev_bar is not None:
        print(
            f"PREV bar → {prev_bar['time']}  score16={int(prev_bar['score']):+d}  "
            f"score9={int(prev_bar['score9']):+d}"
        )
        votes = [f"{n}={int(prev_row[n]):+d}" for n in VOTE_PREV_COLS]
        print("prev-check votes on current: " + ", ".join(votes))
    print(f"path={preview_path}")
    if ylim_used is not None:
        print(f"shared_ylim={ylim_used[0]:.2f}..{ylim_used[1]:.2f}")
        print(f"shared_vmax={ylim_used[2]:.6g}")
    print(f"wrote {preview_txt}")
    if not args.preview_only:
        print(f"wrote {csv_path}")
        print(f"wrote {png_path}")
        print(f"wrote {rubric_path}")
    print(f"db={db} contract={args.contract}")


if __name__ == "__main__":
    main()
