#!/usr/bin/env python3
"""Luis volume-location scoring matrix for candle-force (VbP zone weights).

Research only. Classifies each 5m footprint VbP price level relative to the
candle's OHLC body and accumulates weighted long/short/contention mass.

Locked weights (body = [min(O,C), max(O,C)]):
  - Lower wick  (price < body_low):  weight 3× → LONG (+)
  - Upper wick  (price > body_high): weight 3× → SHORT (−)
  - Body near close (bull: top 25% of body; bear: bottom 25%): weight 2× → toward close
  - Body not near close: weight 1× → contention (w_body_fight);
      also body-below-mid → LONG 1×, body-above-mid → SHORT 1× (enabled)

Doji (body < TICK or body < DOJI_FRAC · [L,H]):
  skip near-close; use thirds of [L,H]:
    lower third 3× LONG, upper third 3× SHORT, mid third 1× fight
    (+ optional mid-split of mid third into long/short 1× below/above mid of [L,H]).

Check 16 `vbp_zone_weight` (supplements hvn_shape, does not replace it):
  imbalance = (w_long − w_short) / (w_long + w_short + eps)
  > +0.15 → +1;  < −0.15 → −1;  else 0
"""
from __future__ import annotations

import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

_VENV = Path("/workspace/sierra/.venv/lib/python3.13/site-packages")
if _VENV.exists() and str(_VENV) not in sys.path:
    sys.path.insert(0, str(_VENV))
if "/workspace/sierra" not in sys.path:
    sys.path.insert(0, "/workspace/sierra")
if "/workspace/sierra/candle_force" not in sys.path:
    sys.path.insert(0, "/workspace/sierra/candle_force")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle
import numpy as np
import pandas as pd

from luis_vbp_or import TICK
from score_candle_force_day import DOJI_FRAC

OUT_DIR = Path("/workspace/sierra/candle_force")
EPS = 1e-9
IMBALANCE_THRESH = 0.15

# Zone colors (white-bg friendly)
ZONE_COLORS = {
    "lower_wick": "#1b7a32",      # green — long
    "upper_wick": "#c62828",      # red — short
    "near_close": "#1565c0",      # blue — close-confirm
    "body_fight": "#78909c",      # gray — contention
    "body_long": "#66bb6a",       # light green — body below mid
    "body_short": "#ef5350",      # light red — body above mid
    "doji_mid": "#90a4ae",
}

ZONE_LABEL_ES = {
    "lower_wick": "Mecha inferior",
    "upper_wick": "Mecha superior",
    "near_close": "Cuerpo cerca del cierre",
    "body_fight": "Cuerpo (contienda)",
    "body_long": "Cuerpo bajo mid",
    "body_short": "Cuerpo sobre mid",
    "doji_mid": "Doji tercio medio",
}


@dataclass
class ZoneWeights:
    w_long: float
    w_short: float
    w_body_fight: float
    imbalance: float
    vote: int
    is_doji: bool
    body_low: float
    body_high: float
    body_mid: float
    near_close_lo: float
    near_close_hi: float
    vol_lower_wick: float
    vol_upper_wick: float
    vol_near_close: float
    vol_body_fight: float
    vol_body_below_mid: float
    vol_body_above_mid: float
    n_levels: int
    total_vol: float

    def as_row(self) -> dict:
        d = asdict(self)
        d["vote"] = int(self.vote)
        d["is_doji"] = bool(self.is_doji)
        return d


def is_doji_body(o: float, c: float, lo: float, hi: float) -> bool:
    rng = hi - lo
    body = abs(c - o)
    if body < TICK - 1e-12:
        return True
    if rng > 1e-12 and body < DOJI_FRAC * rng - 1e-12:
        return True
    return False


def classify_price_zone(
    price: float,
    *,
    o: float,
    c: float,
    lo: float,
    hi: float,
    doji: bool | None = None,
) -> tuple[str, float, int]:
    """Return (zone_name, weight, direction) where direction ∈ {+1, 0, −1}.

    direction: +1 long-favoring, −1 short-favoring, 0 contention-only.
    For body not-near-close, direction follows side of mid (±1) while zone
    name is still body_fight / body_long / body_short for coloring.
    """
    body_low = min(o, c)
    body_high = max(o, c)
    body_rng = body_high - body_low
    body_mid = 0.5 * (body_low + body_high)
    bull = c > o + 1e-12
    bear = c < o - 1e-12
    if doji is None:
        doji = is_doji_body(o, c, lo, hi)

    if doji:
        # thirds of [L, H]; skip near-close
        rng = hi - lo
        if rng <= 1e-12:
            return "doji_mid", 1.0, 0
        pos = (price - lo) / rng
        if pos <= 1.0 / 3.0 + 1e-15:
            return "lower_wick", 3.0, +1
        if pos >= 2.0 / 3.0 - 1e-15:
            return "upper_wick", 3.0, -1
        # mid third: contention 1×; also side-of-mid of [L,H]
        mid_lh = 0.5 * (lo + hi)
        if price < mid_lh - 1e-12:
            return "body_long", 1.0, +1
        if price > mid_lh + 1e-12:
            return "body_short", 1.0, -1
        return "doji_mid", 1.0, 0

    # Non-doji: wicks vs body
    if price < body_low - 1e-12:
        return "lower_wick", 3.0, +1
    if price > body_high + 1e-12:
        return "upper_wick", 3.0, -1

    # Inside body (inclusive of body edges)
    if body_rng <= 1e-12:
        # degenerate body (shouldn't happen if not doji, but safe)
        return "body_fight", 1.0, 0

    # Near-close band
    if bull:
        nc_lo = body_high - 0.25 * body_rng
        nc_hi = body_high
        if price >= nc_lo - 1e-12:
            return "near_close", 2.0, +1
    elif bear:
        nc_lo = body_low
        nc_hi = body_low + 0.25 * body_rng
        if price <= nc_hi + 1e-12:
            return "near_close", 2.0, -1
    # flat non-doji (rare): no near-close

    # Body not near close → fight + side of mid
    if price < body_mid - 1e-12:
        return "body_long", 1.0, +1
    if price > body_mid + 1e-12:
        return "body_short", 1.0, -1
    return "body_fight", 1.0, 0


def _near_close_bounds(o: float, c: float, doji: bool) -> tuple[float, float]:
    if doji:
        return float("nan"), float("nan")
    body_low = min(o, c)
    body_high = max(o, c)
    body_rng = body_high - body_low
    if body_rng <= 1e-12:
        return float("nan"), float("nan")
    if c > o + 1e-12:
        return body_high - 0.25 * body_rng, body_high
    if c < o - 1e-12:
        return body_low, body_low + 0.25 * body_rng
    return float("nan"), float("nan")


def compute_vbp_zone_weights(
    prices: Iterable[float],
    volumes: Iterable[float],
    o: float,
    h: float,
    lo: float,
    c: float,
) -> ZoneWeights:
    """Accumulate weighted mass from a VbP (price → volume) series."""
    prices_a = np.asarray(list(prices), dtype=float)
    vols_a = np.asarray(list(volumes), dtype=float)
    doji = is_doji_body(o, c, lo, h)
    body_low = min(o, c)
    body_high = max(o, c)
    body_mid = 0.5 * (body_low + body_high)
    nc_lo, nc_hi = _near_close_bounds(o, c, doji)

    w_long = 0.0
    w_short = 0.0
    w_body_fight = 0.0
    vol_lower = vol_upper = vol_nc = vol_fight = 0.0
    vol_below = vol_above = 0.0

    for p, v in zip(prices_a, vols_a):
        if not math.isfinite(p) or not math.isfinite(v) or v <= 0:
            continue
        zone, weight, direction = classify_price_zone(
            float(p), o=o, c=c, lo=lo, hi=h, doji=doji
        )
        mass = float(v) * float(weight)
        if direction > 0:
            w_long += mass
        elif direction < 0:
            w_short += mass

        if zone == "lower_wick":
            vol_lower += float(v)
        elif zone == "upper_wick":
            vol_upper += float(v)
        elif zone == "near_close":
            vol_nc += float(v)
        elif zone in ("body_fight", "doji_mid"):
            vol_fight += float(v)
            w_body_fight += float(v) * 1.0
        elif zone == "body_long":
            vol_below += float(v)
            vol_fight += float(v)
            w_body_fight += float(v) * 1.0
        elif zone == "body_short":
            vol_above += float(v)
            vol_fight += float(v)
            w_body_fight += float(v) * 1.0

    denom = w_long + w_short + EPS
    imbalance = (w_long - w_short) / denom
    if imbalance > IMBALANCE_THRESH:
        vote = 1
    elif imbalance < -IMBALANCE_THRESH:
        vote = -1
    else:
        vote = 0

    return ZoneWeights(
        w_long=float(w_long),
        w_short=float(w_short),
        w_body_fight=float(w_body_fight),
        imbalance=float(imbalance),
        vote=int(vote),
        is_doji=bool(doji),
        body_low=float(body_low),
        body_high=float(body_high),
        body_mid=float(body_mid),
        near_close_lo=float(nc_lo) if math.isfinite(nc_lo) else float("nan"),
        near_close_hi=float(nc_hi) if math.isfinite(nc_hi) else float("nan"),
        vol_lower_wick=float(vol_lower),
        vol_upper_wick=float(vol_upper),
        vol_near_close=float(vol_nc),
        vol_body_fight=float(vol_fight),
        vol_body_below_mid=float(vol_below),
        vol_body_above_mid=float(vol_above),
        n_levels=int(np.sum(vols_a > 0)),
        total_vol=float(np.nansum(vols_a)),
    )


def compute_from_grp(grp: pd.Series, o: float, h: float, lo: float, c: float) -> ZoneWeights:
    if grp is None or not len(grp):
        return compute_vbp_zone_weights([], [], o, h, lo, c)
    return compute_vbp_zone_weights(grp.index.to_numpy(dtype=float), grp.to_numpy(dtype=float), o, h, lo, c)


def zone_color_for_price(
    price: float,
    *,
    o: float,
    c: float,
    lo: float,
    hi: float,
    doji: bool | None = None,
) -> str:
    zone, _, _ = classify_price_zone(price, o=o, c=c, lo=lo, hi=hi, doji=doji)
    return ZONE_COLORS.get(zone, "#b0bec5")


def vote_vbp_zone_weight(zw: ZoneWeights) -> int:
    return int(zw.vote)


def draw_matrix_legend_png(out_png: Path) -> None:
    """Spanish table: zones × weights × direction (white bg, black labels)."""
    rows = [
        ("Mecha inferior", "price < body_low\n(body = [min(O,C), max(O,C)])", "3×", "LONG (+)", "Volumen en mecha baja\nabsorción / rechazo alcista", ZONE_COLORS["lower_wick"]),
        ("Mecha superior", "price > body_high", "3×", "SHORT (−)", "Volumen en mecha alta\nabsorción / rechazo bajista", ZONE_COLORS["upper_wick"]),
        ("Cuerpo cerca del cierre", "Alcista: top 25% del cuerpo\nBajista: bottom 25% del cuerpo", "2×", "hacia el cierre\n(+ bull / − bear)", "Confirma dirección del cierre", ZONE_COLORS["near_close"]),
        ("Cuerpo bajo mid", "body_low ≤ p < mid\n(y no near-close)", "1×", "LONG (+)", "Opcional: masa bajo mid-cuerpo\n(+ entra a contienda)", ZONE_COLORS["body_long"]),
        ("Cuerpo sobre mid", "mid < p ≤ body_high\n(y no near-close)", "1×", "SHORT (−)", "Masa sobre mid-cuerpo\n(+ entra a contienda)", ZONE_COLORS["body_short"]),
        ("Cuerpo = mid / contienda", "p ≈ mid-cuerpo\n(no near-close)", "1×", "neutro (0)", "w_body_fight — masa de contienda", ZONE_COLORS["body_fight"]),
        ("Doji (cuerpo tiny)", "body < TICK o < 10%·[L,H]\n→ tercios de [L,H]; sin near-close", "3× / 1×", "tercio bajo + / tercio alto −\ntercio medio contienda", "Casi todo como mechas vs mid", ZONE_COLORS["doji_mid"]),
    ]

    fig, ax = plt.subplots(figsize=(13.2, 9.2), dpi=150)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#ffffff")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    ax.text(
        0.5, 0.975,
        "Matriz de pesos VbP · ubicación del volumen (candle-force)",
        ha="center", va="top", fontsize=15, fontweight="bold", color="#000000",
    )
    ax.text(
        0.5, 0.942,
        "Cada nivel de precio del footprint 5m se clasifica vs el cuerpo OHLC · "
        "check 16 = vbp_zone_weight (complementa hvn_shape)",
        ha="center", va="top", fontsize=9.0, color="#546e7a",
    )

    headers = ["Zona", "Regla (precio)", "Peso", "Dirección", "Significado", ""]
    widths = [0.16, 0.26, 0.08, 0.16, 0.28, 0.04]
    left = 0.02
    xs = []
    x = left
    for w in widths:
        xs.append(x)
        x += w

    top = 0.905
    bottom = 0.22
    n_rows = 1 + len(rows)
    row_h = (top - bottom) / n_rows

    y = top - row_h
    for x, w, h in zip(xs, widths, headers):
        ax.add_patch(Rectangle((x, y), w, row_h, facecolor="#e8eef2", edgecolor="#90a4ae", lw=0.8))
        ax.text(x + w / 2, y + row_h / 2, h, ha="center", va="center",
                fontsize=8.5, fontweight="bold", color="#000000")

    for i, (zona, regla, peso, direc, meaning, col) in enumerate(rows):
        y = top - row_h * (i + 2)
        bg = "#f7f7f7" if i % 2 == 0 else "#efefef"
        cells = [zona, regla, peso, direc, meaning]
        for x, w, txt in zip(xs, widths[:-1], cells):
            ax.add_patch(Rectangle((x, y), w, row_h, facecolor=bg, edgecolor="#90a4ae", lw=0.55))
            # direction color
            tcol = "#000000"
            if "LONG" in txt or txt.strip().startswith("+") or "bajo +" in txt:
                if "LONG" in txt or "bajo +" in txt:
                    tcol = "#1b7a32"
            if "SHORT" in txt or "alto −" in txt:
                tcol = "#c62828"
            if txt in ("3×", "2×", "1×", "3× / 1×"):
                tcol = "#000000"
                ax.text(x + w / 2, y + row_h / 2, txt, ha="center", va="center",
                        fontsize=10, fontweight="bold", color=tcol)
                continue
            ax.text(
                x + 0.008, y + row_h / 2, txt,
                ha="left", va="center", fontsize=7.2, color=tcol,
                fontweight="bold" if x == xs[0] else "normal",
                linespacing=1.15,
            )
        # color swatch
        ax.add_patch(Rectangle((xs[-1], y), widths[-1], row_h, facecolor=bg, edgecolor="#90a4ae", lw=0.55))
        ax.add_patch(FancyBboxPatch(
            (xs[-1] + 0.006, y + row_h * 0.22), widths[-1] - 0.012, row_h * 0.56,
            boxstyle="round,pad=0.004,rounding_size=0.008",
            facecolor=col, edgecolor="#000000", lw=0.5,
        ))

    # Derived metrics box
    box_y = 0.02
    ax.add_patch(FancyBboxPatch(
        (0.02, box_y), 0.96, 0.185,
        boxstyle="round,pad=0.01,rounding_size=0.012",
        facecolor="#fafafa", edgecolor="#90a4ae", lw=0.9,
    ))
    metrics = (
        "Métricas derivadas por vela\n"
        "  w_long  = Σ(vol·peso) zonas LONG   ·   w_short = Σ(vol·peso) zonas SHORT\n"
        "  w_body_fight = Σ(vol·1×) en cuerpo no near-close (contienda)\n"
        "  imbalance = (w_long − w_short) / (w_long + w_short + ε)  ∈ [−1, +1]\n"
        "  check 16  vbp_zone_weight:  imbalance > +0.15 → +1   |   < −0.15 → −1   |   else 0\n"
        "  Score v2 pasa a ∈ [−16, +16]  ·  hvn_shape (check 6) se mantiene  ·  raw w_long/w_short en CSV"
    )
    ax.text(0.04, box_y + 0.175, metrics, ha="left", va="top", fontsize=8.0,
            color="#000000", family="DejaVu Sans Mono", linespacing=1.35)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, facecolor="#ffffff", bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)


def matrix_summary_text() -> str:
    lines = [
        "=== Matriz pesos VbP (Luis) ===",
        "Zona                     Peso  Dirección",
        "Mecha inferior           3×    LONG (+)",
        "Mecha superior           3×    SHORT (−)",
        "Cuerpo cerca del cierre  2×    hacia cierre (+ bull / − bear)",
        "Cuerpo bajo mid          1×    LONG (+)  [también contienda]",
        "Cuerpo sobre mid         1×    SHORT (−) [también contienda]",
        "Cuerpo = mid             1×    neutro → w_body_fight",
        "Doji: tercios [L,H]      3×/1× sin near-close",
        f"imbalance thresh ±{IMBALANCE_THRESH} → vote +1/0/−1 (check 16 vbp_zone_weight)",
        "Suplementa hvn_shape; score ∈ [−16,+16]",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    out = OUT_DIR / "CANDLE_FORCE_vbp_weight_matrix.png"
    draw_matrix_legend_png(out)
    print(matrix_summary_text())
    print(f"wrote {out}")
