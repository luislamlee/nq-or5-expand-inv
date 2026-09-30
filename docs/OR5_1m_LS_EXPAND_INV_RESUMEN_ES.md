# Resumen OR5 1m LS EXPAND+INV — para Luis

**Mecánica compartida:** OR5 expand → breakout 1m (short bajo cur_L / long sobre cur_H) → fill next open.  
**INV pre-entrada (OR5 original):** short muere si high≥OR_L+1.618·R; long muere si low≤OR_H−1.618·R (`fib_px`).  
**Filtros:** Short F3 (rojo, score≤−5, vbp≤0) · Long L3≥+5 (verde, score≥+5, vbp≥0; simétrico a −5).  
Históricamente extremes usó ≥+6 y VbP-long ≥+8 — aquí primary = **≥+5**; sensibilidad ≥+8 en CSV.  
**Gestión:** RATCHET (5m HI short / 5m LO long) y HOLD_VWAP (espejo). Coste 0.50. Foco T4.23.

Señales: F3=50 · L3≥+5=59 · Combined=109 · L3≥+8=14 · overlap F3∩L3=0.

## Tabla Long / Short / Combined — T423 HI y HOLD

| Lado | TM | n | %W | Net | pts/día | Mejor (fecha) | Peor (fecha) | PF | IS n/net/PF | OOS n/net/PF |
|---|---|---:|---:|---:|---:|---|---|---:|---|---|
| Short F3 | RATCHET_HI | 42 | 50.0% | 1359.9 | 32.4 | 483.2 (2026-06-25) | -122.0 (2026-05-26) | 2.27 | 26/637.4/1.88 | 16/722.4/3.09 |
| Short F3 | HOLD_VWAP | 42 | 42.9% | 1102.9 | 26.3 | 483.2 (2026-06-25) | -85.5 (2026-07-07) | 2.16 | 26/644.7/2.13 | 16/458.2/2.19 |
| Long L3≥+5 | RATCHET_HI | 52 | 48.1% | 172.0 | 3.3 | 237.5 (2026-08-13) | -112.5 (2026-06-29) | 1.13 | 36/-133.0/0.84 | 16/305.0/1.62 |
| Long L3≥+5 | HOLD_VWAP | 52 | 44.2% | 252.5 | 4.9 | 237.5 (2026-08-13) | -112.5 (2026-06-29) | 1.21 | 36/-59.8/0.92 | 16/312.3/1.65 |
| Combined F3+L5 | RATCHET_HI | 94 | 48.9% | 1531.9 | 16.3 | 483.2 (2026-06-25) | -122.0 (2026-05-26) | 1.64 | 62/504.4/1.32 | 32/1027.5/2.23 |
| Combined F3+L5 | HOLD_VWAP | 94 | 43.6% | 1355.4 | 14.4 | 483.2 (2026-06-25) | -112.5 (2026-06-29) | 1.62 | 62/584.9/1.44 | 32/770.5/1.89 |

## Lectura directa

- **Short F3 baseline (EXPAND+INV):** HI n=42 net=1359.9 PF=2.27 pts/día=32.4 · HOLD n=42 net=1102.9 PF=2.16
- **Long L3≥+5 (EXPAND+INV):** HI n=52 net=172.0 PF=1.13 pts/día=3.3 · Lwin 237.5 (2026-08-13) · Lloss -112.5 (2026-06-29) · HOLD net=252.5 PF=1.21
- **Combined = Short+Long:** HI n=94 net=1531.9 PF=1.64 pts/día=16.3 · HOLD n=94 net=1355.4 PF=1.62
- **Sensibilidad Long ≥+8:** HI n=14 net=191.8 PF=1.77 (pocos días=14)

Detalle EN: `OR5_1m_LS_EXPAND_INV.md` · equity: `OR5_1m_LS_EXPAND_INV_equity.png`  
CSV: `OR5_1m_LS_EXPAND_INV_trades.csv` / `_summary.csv` / `_compare.csv`

*Research exploratorio — no es señal live.*
