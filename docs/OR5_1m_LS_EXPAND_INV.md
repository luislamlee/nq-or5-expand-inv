# OR5_1m_LS_EXPAND_INV — Long + Short expanding OR + INV 1.618

**Status:** [EXPLORATORY] research only · no live trading  
**Date:** 2026-09-29 (America/Bogota)  
**Data:** `CANDLE_FORCE_v3_multiday_bars.parquet` + `NQ_front_volroll.duckdb` 1m + dVWAP  
**IS/OOS:** IS ≤ 2026-06-10 · OOS ≥ 2026-06-11 · range ~2025-12-16..2026-08-25  
**Cost:** 0.5 pts RT · **STOP_BUF:** 2.0

---

## Shared OR mechanics

1. OR5 = 09:30–09:35 ET High/Low. R = OR_H−OR_L.
2. Expanding range: inside close → expand; outside → breakout (no expand).
3. Short: close < cur_L → next open. Long: close > cur_H → next open.
4. ≤1 trade/day (first valid side). Skip last-bar confirm.
5. **INV (pre-entry, original OR5):**
   - Short dead if 1m high ≥ `OR_L + 1.618·R`
   - Long dead if 1m low ≤ `fib_px(OR_H,R,1.618)` = `OR_H − 1.618·R`
6. Targets: short `fib_px(k)`; long `OR_L + k·R`. k ∈ {2.618, 3.33, 4.23}

## Filters

| Book | Filter | Signal days |
|---|---|---:|
| Short F3 | OR5 red · score_v2≤−5 · vbp≤0 | 50 |
| Long L3≥+5 | OR5 green · score_v2≥+5 · vbp≥0 | 59 |
| Combined | F3 ∪ L3 (0 day overlap) | 109 |
| Long L3≥+8 (sens.) | green · ≥+8 · vbp≥0 | 14 |
| ALL | no CF filter | 172 |

Note: `run_or5_cf_extremes` used ≥+6 historically; VbP long side table used ≥+8.
Primary long cut = **≥+5** for symmetry with short −5.

## Management

RATCHET: short=prior 5m High; long=prior 5m Low · exit 1m close beyond stop.  
HOLD_VWAP: short exit close≥dVWAP; long exit close≤dVWAP.  
Init stop: short OR_H+2; long OR_L−2.

## Headline — T423 HI & HOLD (ALL split)

| Setup | n | %W | Net | pts/día | Lwin (fecha) | Lloss (fecha) | PF | IS / OOS |
|---|---:|---:|---:|---:|---|---|---:|---|
| Short F3 RATCHET_HI | 42 | 50.0% | 1359.9 | 32.4 | 483.2 (2026-06-25) | -122.0 (2026-05-26) | 2.27 | IS n=26 net=637.4 PF=1.88 · OOS n=16 net=722.4 PF=3.09 |
| Short F3 HOLD_VWAP | 42 | 42.9% | 1102.9 | 26.3 | 483.2 (2026-06-25) | -85.5 (2026-07-07) | 2.16 | IS n=26 net=644.7 PF=2.13 · OOS n=16 net=458.2 PF=2.19 |
| Long L3≥+5 RATCHET_HI | 52 | 48.1% | 172.0 | 3.3 | 237.5 (2026-08-13) | -112.5 (2026-06-29) | 1.13 | IS n=36 net=-133.0 PF=0.84 · OOS n=16 net=305.0 PF=1.62 |
| Long L3≥+5 HOLD_VWAP | 52 | 44.2% | 252.5 | 4.9 | 237.5 (2026-08-13) | -112.5 (2026-06-29) | 1.21 | IS n=36 net=-59.8 PF=0.92 · OOS n=16 net=312.3 PF=1.65 |
| Combined F3+L5 RATCHET_HI | 94 | 48.9% | 1531.9 | 16.3 | 483.2 (2026-06-25) | -122.0 (2026-05-26) | 1.64 | IS n=62 net=504.4 PF=1.32 · OOS n=32 net=1027.5 PF=2.23 |
| Combined F3+L5 HOLD_VWAP | 94 | 43.6% | 1355.4 | 14.4 | 483.2 (2026-06-25) | -112.5 (2026-06-29) | 1.62 | IS n=62 net=584.9 PF=1.44 · OOS n=32 net=770.5 PF=1.89 |

## Extra — ALL sides + L3≥+8 T423

| Setup | n | %W | Net | PF | pts/día | Lwin | Lloss | n INV skip |
|---|---:|---:|---:|---:|---:|---|---|---:|
| ALL_SHORT_RATCHET_HI | 110 | 40.9% | 238.7 | 1.06 | 2.2 | 483.2 (2026-06-25) | -138.5 (2026-06-24) | 61 |
| ALL_SHORT_HOLD_VWAP | 110 | 36.4% | 765.2 | 1.24 | 7.0 | 483.2 (2026-06-25) | -92.8 (2026-06-04) | 61 |
| ALL_LONG_RATCHET_HI | 106 | 49.1% | 760.4 | 1.30 | 7.2 | 329.0 (2026-08-03) | -139.5 (2026-03-03) | 65 |
| ALL_LONG_HOLD_VWAP | 106 | 43.4% | 612.1 | 1.25 | 5.8 | 329.0 (2026-08-03) | -112.5 (2026-06-29) | 65 |
| L3p8_LONG_RATCHET_HI | 14 | 64.3% | 191.8 | 1.77 | 13.7 | 100.2 (2026-08-04) | -72.8 (2026-01-20) | 0 |
| L3p8_LONG_HOLD_VWAP | 14 | 64.3% | 214.2 | 1.95 | 15.3 | 100.2 (2026-08-04) | -56.8 (2026-01-20) | 0 |

## Outputs

- Script: `run_or5_1m_ls_expand_inv.py`
- Trades / summary / compare: `OR5_1m_LS_EXPAND_INV_*.csv`
- Equity: `OR5_1m_LS_EXPAND_INV_equity.png`
- Spanish: `OR5_1m_LS_EXPAND_INV_RESUMEN_ES.md`

*Research exploratorio — no es señal live.*
