# Prompt for OpenAI / Dot agents

Copy into ChatGPT after cloning this repo. You already have `NQ_front_volroll.duckdb` locally.

## Features (CRITICAL)

`score_v2` and `vbp_imbalance` are **NOT** inside the DuckDB. Use:

- `data/F3_OR5_features.csv` — OR5 row per day with `score_v2`, `vbp_imbalance`, `f3_0`
- `data/F3_0_days.csv` — ~50 F3_0 days
- Optional rebuild: `scripts/score_candle_force_day_v2.py` + `data/CANDLE_FORCE_v3_multiday_bars.parquet`

**F3_0:** OR5 red (`close < open`) AND `score_v2 ≤ -5` AND `vbp_imbalance ≤ 0`.

## Strategy to verify

See root `README.md`. Official stack: F3 → expand OR on inside 1m closes → short on close < cur_L → INV if high ≥ OR_L+1.618R before entry → RATCHET_HI (5m prior High) exit on 1m close ≥ stop → target 4.23 · cost 0.50.

**Benchmark:** n≈42, WR≈50%, net≈+1360 pts, ≈32.4 pts/day, Lwin +483 (2026-06-25), Lloss −122 (2026-05-26).

Point scripts’ `DB` / parquet paths to your local volroll + this repo’s `data/`. Report table vs benchmark; if n differs by >3 or net >10%, debug definitions before claiming new edge.
