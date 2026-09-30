# OR5_1m_EXPAND_BREAK — SHORT on 1m close < expanding range Low

**Status:** [EXPLORATORY] research only · no live trading  
**Date:** 2026-09-29 (America/Bogota)  
**Data:** `CANDLE_FORCE_v3_multiday_bars.parquet` (OR5 / F3) + `bars_t_1m` / `profile_developing`  
**IS/OOS:** IS ≤ 2026-06-10 · OOS ≥ 2026-06-11 · range ~2025-12-16..2026-08-25  
**Cost:** 0.5 pts RT · **STOP_BUF:** 2.0 → init stop = **original** OR_H + 2.0

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

**Sanity:** entry_time==09:35 = **0**; confirms not below cur_L = **0** (both must be 0).

## Universes

| Universe | Filter | Signal days | Trades (T423 HI) | % signal no entry |
|---|---|---:|---:|---:|
| F3 | OR5 red + score_v2 ≤ −5 + vbp_imbalance ≤ 0 | 50 | 46 | 8.0% |
| ALL | Valid OR5 (R>0), no F3 | 172 | 135 | 21.5% |

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
| F3 EXPAND HOLD_VWAP_T423 | 46 | 41.3% | 58.7% | 905.1 | 1.72 | 19.7 | 483.2 (2026-06-25) | -119.0 (2026-06-11) | 18.9 | 2.0 | 1.43 | 0.00 | 1.132 | 8.0% | 8.7% | 17.1 |
| F3 EXPAND RATCHET_HI_T423 | 46 | 47.8% | 52.2% | 1162.1 | 1.85 | 25.3 | 483.2 (2026-06-25) | -122.0 (2026-05-26) | 18.9 | 2.0 | 1.43 | 0.00 | 1.132 | 8.0% | 8.7% | 21.3 |
| F3 FIXED BREAK HOLD_VWAP_T423 | 47 | 38.3% | 61.7% | 967.1 | 1.74 | 20.6 | 483.2 (2026-06-25) | -88.5 (2026-05-29) | 12.0 | 2.0 | — | — | — | 6.0% | 8.5% | 15.9 |
| F3 FIXED BREAK RATCHET_HI_T423 | 47 | 44.7% | 55.3% | 1056.9 | 1.66 | 22.5 | 483.2 (2026-06-25) | -147.8 (2026-06-11) | 12.0 | 2.0 | — | — | — | 6.0% | 8.5% | 20.4 |
| F3@0935 HOLD_VWAP_T423 | 50 | 34.0% | 66.0% | 1191.9 | 1.96 | 23.8 | 511.4 (2026-06-25) | -107.8 (2026-06-10) | 0.0 | 0.0 | — | — | — | 0.0% | 8.0% | 13.8 |
| F3@0935 RATCHET_HI_T423 | 50 | 44.0% | 56.0% | 1217.1 | 1.68 | 24.3 | 511.4 (2026-06-25) | -182.5 (2026-06-10) | 0.0 | 0.0 | — | — | — | 0.0% | 8.0% | 21.4 |
| ALL EXPAND HOLD_VWAP_T423 | 135 | 36.3% | 63.7% | 217.4 | 1.05 | 1.6 | 483.2 (2026-06-25) | -119.0 (2026-06-11) | 27.9 | 8.0 | 2.19 | 1.00 | 1.225 | 21.5% | 4.4% | 17.3 |
| ALL EXPAND RATCHET_HI_T423 | 135 | 40.0% | 60.0% | -309.1 | 0.94 | -2.3 | 483.2 (2026-06-25) | -138.5 (2026-06-24) | 27.9 | 8.0 | 2.19 | 1.00 | 1.225 | 21.5% | 4.4% | 20.6 |

### Direct answers

**F3 EXPAND vs F3 FIXED BREAK (same TM T423):**
- HOLD_VWAP: expand net=905.1 · fixed net=967.1 · **Δnet=-62.0**
- RATCHET_HI: expand net=1162.1 · fixed net=1056.9 · **Δnet=105.3**

**Entry delay (min from 09:35) & expansions before entry (F3 T423 HI):**
- delay mean=18.9 · med=2.0
- expansions mean=1.43 · med=0.00
- final range width / original R: mean=1.132 · med=1.000

**IS / OOS (F3 EXPAND T423):**
- HOLD: IS n=29 net=565.9 PF=1.76 · OOS n=17 net=339.2 PF=1.67
- HI: IS n=29 net=558.7 PF=1.62 · OOS n=17 net=603.4 PF=2.30

## Full matrix (ALL split)

| variante | n | WR | net | PF | avg/día | Lwin | Lloss | IS net | OOS net | delay mean/med | exp mean/med | w/R mean | %hit4.23 | avg bars | %hold | %ratchet |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|
| F3_EXPAND_HOLD_VWAP_T2618 | 46 | 43.5% | 715.8 | 1.58 | 15.6 | 236.6 | -119.0 | 463.1 | 252.6 | 18.9/2.0 | 1.43/0.00 | 1.132 | 0.0% | 12.2 | 34.8% | 32.6% |
| F3_EXPAND_HOLD_VWAP_T333 | 46 | 41.3% | 822.8 | 1.66 | 17.9 | 345.5 | -119.0 | 567.7 | 255.1 | 18.9/2.0 | 1.43/0.00 | 1.132 | 0.0% | 14.8 | 37.0% | 43.5% |
| F3_EXPAND_HOLD_VWAP_T423 | 46 | 41.3% | 905.1 | 1.72 | 19.7 | 483.2 | -119.0 | 565.9 | 339.2 | 18.9/2.0 | 1.43/0.00 | 1.132 | 8.7% | 17.1 | 41.3% | 47.8% |
| F3_EXPAND_RATCHET_HI_T2618 | 46 | 50.0% | 1014.5 | 1.75 | 22.1 | 261.6 | -122.0 | 462.0 | 552.5 | 18.9/2.0 | 1.43/0.00 | 1.132 | 0.0% | 15.5 | 0.0% | 63.0% |
| F3_EXPAND_RATCHET_HI_T333 | 46 | 47.8% | 1059.6 | 1.77 | 23.0 | 345.5 | -122.0 | 540.2 | 519.3 | 18.9/2.0 | 1.43/0.00 | 1.132 | 0.0% | 18.9 | 0.0% | 80.4% |
| F3_EXPAND_RATCHET_HI_T423 | 46 | 47.8% | 1162.1 | 1.85 | 25.3 | 483.2 | -122.0 | 558.7 | 603.4 | 18.9/2.0 | 1.43/0.00 | 1.132 | 8.7% | 21.3 | 0.0% | 89.1% |
| ALL_EXPAND_HOLD_VWAP_T2618 | 135 | 37.8% | 141.3 | 1.03 | 1.0 | 263.2 | -119.0 | -411.8 | 553.2 | 27.9/8.0 | 2.19/1.00 | 1.225 | 0.0% | 12.6 | 34.1% | 40.7% |
| ALL_EXPAND_HOLD_VWAP_T333 | 135 | 36.3% | 478.3 | 1.11 | 3.5 | 382.1 | -119.0 | -428.4 | 906.7 | 27.9/8.0 | 2.19/1.00 | 1.225 | 0.0% | 15.5 | 34.8% | 49.6% |
| ALL_EXPAND_HOLD_VWAP_T423 | 135 | 36.3% | 217.4 | 1.05 | 1.6 | 483.2 | -119.0 | -612.9 | 830.3 | 27.9/8.0 | 2.19/1.00 | 1.225 | 4.4% | 17.3 | 36.3% | 57.0% |
| ALL_EXPAND_RATCHET_HI_T2618 | 135 | 41.5% | -153.6 | 0.97 | -1.1 | 263.2 | -138.5 | -823.9 | 670.3 | 27.9/8.0 | 2.19/1.00 | 1.225 | 0.0% | 15.3 | 0.0% | 67.4% |
| ALL_EXPAND_RATCHET_HI_T333 | 135 | 40.0% | -68.4 | 0.99 | -0.5 | 382.1 | -138.5 | -1056.7 | 988.2 | 27.9/8.0 | 2.19/1.00 | 1.225 | 0.0% | 18.8 | 0.0% | 80.0% |
| ALL_EXPAND_RATCHET_HI_T423 | 135 | 40.0% | -309.1 | 0.94 | -2.3 | 483.2 | -138.5 | -1220.9 | 911.8 | 27.9/8.0 | 2.19/1.00 | 1.225 | 4.4% | 20.6 | 0.0% | 88.9% |

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
