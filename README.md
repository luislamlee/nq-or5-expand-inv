# NQ OR5 Expand + INV 1.618 — Short F3 (research)

Strategy pack for replicating MASTER_ALGO / Luis Lam research on NQ RTH.

**Official stack (best so far):** F3_0 filter → expanding OR after OR5 → short on 1m close below live Low → invalidate if price traded ≥ long fib 1.618 before entry → manage with RATCHET_HI (5m prior High) + 1m close confirm → target **4.23**.

Benchmark (same volroll sample ~2025-12-16…2026-08-25, cost 0.50):
| Setup | n | WR | Net pts | pts/day | Lwin | Lloss | PF |
|---|---:|---:|---:|---:|---|---|---:|
| F3 EXPAND+INV RATCHET_HI T423 | 42 | 50% | ~1360 | ~32.4 | +483 (2026-06-25) | −122 (2026-05-26) | ~2.27 |
| F3 EXPAND+INV HOLD_VWAP T423 | 42 | ~43% | ~1103 | ~26.3 | +483 (06-25) | −85.5 (07-07) | ~2.16 |

## What OpenAI / other agents need (features are NOT in volroll ticks)

Volroll DuckDB has ticks/bars only. F3 needs:

| File | Purpose |
|---|---|
| `data/F3_OR5_features.csv` | Per-day OR5 (`m5_bar=1`) with `score_v2`, `vbp_imbalance`, OHLC, `f3_0` flag |
| `data/F3_0_days.csv` | The ~50 F3_0 signal days |
| `data/CANDLE_FORCE_v3_multiday_bars.parquet` | Full 5m CF bars (all m5) if you want to rebuild scores |
| `scripts/score_candle_force_day_v2.py` | Candle Force equal-vote v2 scorer |

**F3_0 day rule:** OR5 red (`close < open`) AND `score_v2 ≤ -5` AND `vbp_imbalance ≤ 0`.

## Rules (short)

1. **OR5** = 09:30–09:35 ET. `R = OR_H - OR_L`. Fib long k = `OR_L + k*R`. Fib short k = `OR_H - k*R`.
2. **Expand:** after OR5, walk 1m bars. If close **inside** `[cur_L, cur_H]` → expand H/L to bar wick. If close **outside** → no expand that bar.
3. **Entry short:** first 1m `close < cur_L` → fill **next 1m open**. Skip if no next bar.
4. **INV 1.618:** if any 1m **high ≥ OR_L + 1.618*R** (original OR5) **before fill** → no short that day.
5. **Stop init:** `OR_H + 2`. **RATCHET_HI:** stop trails to prior **5m High** (improves only); exit when **1m close ≥ stop**.
6. **HOLD_VWAP alt:** early exit if 1m close ≥ developing RTH VWAP.
7. Targets: 2.618 / 3.33 / **4.23**. Cost **0.50**. ≤1 trade/day. Flatten EOD.

Long mirror is exploratory and weak vs short — see `docs/OR5_1m_LS_EXPAND_INV_RESUMEN_ES.md`.

## Scripts

| Script | Role |
|---|---|
| `scripts/run_or5_1m_inv1618.py` | EXPAND ± INV compare (includes FIXED / @0935 baselines) |
| `scripts/run_or5_1m_expand_break.py` | Expand without INV |
| `scripts/run_or5_1m_break_below.py` | Fixed OR_L break (no expand) |
| `scripts/run_or5_1m_ls_expand_inv.py` | Long+Short EXPAND+INV |

Paths inside scripts currently point at `/workspace/sierra/...` — point `DB` / parquet to your local volroll + this `data/` folder.

## Results snapshots

See `results/` CSVs + equity PNGs and `docs/*_RESUMEN_ES.md`.

## Data you must have locally

- `NQ_front_volroll.duckdb` (or equivalent 1m RTH bars) — **not** in this repo (too large).
- This repo’s `data/F3_*.csv` for the F3 filter.

## Research only

Not live trading signals. Verify against the benchmark table before claiming a match.
