# Resumen OR5 1m INV1618 — para Luis

**Regla:** si el precio ha ido **largo por encima del fib 1.618** (OR5 original), el short OR
queda **totalmente invalidado** ese día (no short / cancelar entrada pendiente).

**Fib:** 1.618 largo = `OR_L + 1.618·R` = `OR_H + 0.618·R` (mismo helper / OR5 original, no rango expandido).

**Trigger:** pierce de **high 1m ≥ 1.618** antes del fill (pre-entrada). @0935 casi nunca aplica
(OR5 max = OR_H < 1.618; entrada inmediata) — n_inv=**0**.

**Gestión:** RATCHET_HI y HOLD_VWAP × T4.23 (matriz 2.618/3.33/4.23 en CSV). Coste 0.50.

## Tabla T423 — EXPAND±INV, FIXED±INV, @0935±INV (HI y HOLD)

| Setup | n | %W | Net | PF | pts/día | Mejor (fecha) | Peor (fecha) | n skip INV |
|---|---:|---:|---:|---:|---:|---|---|---:|
| F3 EXPAND HOLD_VWAP | 46 | 41.3% | 905.1 | 1.72 | 19.7 | 483.2 (2026-06-25) | -119.0 (2026-06-11) | 0 |
| F3 EXPAND +INV HOLD_VWAP | 42 | 42.9% | 1102.9 | 2.16 | 26.3 | 483.2 (2026-06-25) | -85.5 (2026-07-07) | 7 |
| F3 EXPAND RATCHET_HI | 46 | 47.8% | 1162.1 | 1.85 | 25.3 | 483.2 (2026-06-25) | -122.0 (2026-05-26) | 0 |
| F3 EXPAND +INV RATCHET_HI | 42 | 50.0% | 1359.9 | 2.27 | 32.4 | 483.2 (2026-06-25) | -122.0 (2026-05-26) | 7 |
| F3 FIXED HOLD_VWAP | 47 | 38.3% | 967.1 | 1.74 | 20.6 | 483.2 (2026-06-25) | -88.5 (2026-05-29) | 0 |
| F3 FIXED +INV HOLD_VWAP | 44 | 38.6% | 954.4 | 1.83 | 21.7 | 483.2 (2026-06-25) | -85.5 (2026-07-07) | 6 |
| F3 FIXED RATCHET_HI | 47 | 44.7% | 1056.9 | 1.66 | 22.5 | 483.2 (2026-06-25) | -147.8 (2026-06-11) | 0 |
| F3 FIXED +INV RATCHET_HI | 44 | 45.5% | 1044.1 | 1.73 | 23.7 | 483.2 (2026-06-25) | -147.8 (2026-06-11) | 6 |
| F3 0935 HOLD_VWAP | 50 | 34.0% | 1191.9 | 1.96 | 23.8 | 511.4 (2026-06-25) | -107.8 (2026-06-10) | 0 |
| F3 0935 +INV HOLD_VWAP | 50 | 34.0% | 1191.9 | 1.96 | 23.8 | 511.4 (2026-06-25) | -107.8 (2026-06-10) | 0 |
| F3 0935 RATCHET_HI | 50 | 44.0% | 1217.1 | 1.68 | 24.3 | 511.4 (2026-06-25) | -182.5 (2026-06-10) | 0 |
| F3 0935 +INV RATCHET_HI | 50 | 44.0% | 1217.1 | 1.68 | 24.3 | 511.4 (2026-06-25) | -182.5 (2026-06-10) | 0 |
| ALL EXPAND HOLD VWAP | 135 | 36.3% | 217.4 | 1.05 | 1.6 | 483.2 (2026-06-25) | -119.0 (2026-06-11) | 0 |
| ALL EXPAND INV1618 HOLD VWAP | 110 | 36.4% | 768.9 | 1.24 | 7.0 | 483.2 (2026-06-25) | -92.8 (2026-06-04) | 61 |
| ALL EXPAND RATCHET HI | 135 | 40.0% | -309.1 | 0.94 | -2.3 | 483.2 (2026-06-25) | -138.5 (2026-06-24) | 0 |
| ALL EXPAND INV1618 RATCHET HI | 110 | 40.9% | 242.4 | 1.06 | 2.2 | 483.2 (2026-06-25) | -138.5 (2026-06-24) | 61 |

## Δ con vs sin filtro INV

| Mode TM | n | %W | Net | pts/día | n INV skip |
|---|---|---|---|---|---:|
| EXPAND HOLD_VWAP | 46→42 (-4) | 41.3%→42.9% (1.6%) | 905.1→1102.9 (+197.8) | 19.7→26.3 (+6.6) | 7 |
| EXPAND RATCHET_HI | 46→42 (-4) | 47.8%→50.0% (2.2%) | 1162.1→1359.9 (+197.8) | 25.3→32.4 (+7.1) | 7 |
| FIXED HOLD_VWAP | 47→44 (-3) | 38.3%→38.6% (0.3%) | 967.1→954.4 (-12.8) | 20.6→21.7 (+1.1) | 6 |
| FIXED RATCHET_HI | 47→44 (-3) | 44.7%→45.5% (0.8%) | 1056.9→1044.1 (-12.8) | 22.5→23.7 (+1.2) | 6 |
| 0935 HOLD_VWAP | 50→50 (+0) | 34.0%→34.0% (0.0%) | 1191.9→1191.9 (+0.0) | 23.8→23.8 (+0.0) | 0 |
| 0935 RATCHET_HI | 50→50 (+0) | 44.0%→44.0% (0.0%) | 1217.1→1217.1 (+0.0) | 24.3→24.3 (+0.0) | 0 |

## Respuesta directa

- **EXPAND HOLD_VWAP:** n 46→42 · %W 41.3%→42.9% · net 905.1→1102.9 (Δ +197.8) · pts/día 19.7→26.3 · INV skip=7 · Lwin 483.2 (2026-06-25) · Lloss -85.5 (2026-07-07)
- **EXPAND RATCHET_HI:** n 46→42 · %W 47.8%→50.0% · net 1162.1→1359.9 (Δ +197.8) · pts/día 25.3→32.4 · INV skip=7 · Lwin 483.2 (2026-06-25) · Lloss -122.0 (2026-05-26)
- **FIXED HOLD_VWAP:** n 47→44 · %W 38.3%→38.6% · net 967.1→954.4 (Δ -12.8) · pts/día 20.6→21.7 · INV skip=6 · Lwin 483.2 (2026-06-25) · Lloss -85.5 (2026-07-07)
- **FIXED RATCHET_HI:** n 47→44 · %W 44.7%→45.5% · net 1056.9→1044.1 (Δ -12.8) · pts/día 22.5→23.7 · INV skip=6 · Lwin 483.2 (2026-06-25) · Lloss -147.8 (2026-06-11)
- **0935 HOLD_VWAP:** n 50→50 · %W 34.0%→34.0% · net 1191.9→1191.9 (Δ +0.0) · pts/día 23.8→23.8 · INV skip=0 · Lwin 511.4 (2026-06-25) · Lloss -107.8 (2026-06-10)
- **0935 RATCHET_HI:** n 50→50 · %W 44.0%→44.0% · net 1217.1→1217.1 (Δ +0.0) · pts/día 24.3→24.3 · INV skip=0 · Lwin 511.4 (2026-06-25) · Lloss -182.5 (2026-06-10)

### Días F3 con trade evitado por INV (T423 HI)
- **EXPAND:** 2026-05-29, 2026-06-09, 2026-06-10, 2026-06-11 (suma net evitados = -197.8) · n_inv_skip total=7
- **FIXED:** 2026-05-29, 2026-06-09, 2026-06-10 (suma net evitados = +12.8) · n_inv_skip total=6
- **@0935:** ninguno (entrada inmediata; OR5 no puede tocar 1.618)

Señales F3: **50** días · ALL: **172** días.

Detalle EN: `OR5_1m_INV1618.md` · equity: `OR5_1m_INV1618_equity.png`  
CSV: `OR5_1m_INV1618_trades.csv` / `_summary.csv` / `_compare.csv`

*Research exploratorio — no es señal live.*
