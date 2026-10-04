# GrokBot request for the original volroll feature generator

Please check what is missing from [luislamlee/nq-or5-expand-inv](https://github.com/luislamlee/nq-or5-expand-inv) and return the exact original code needed to generate Candle Force v2 `score_v2` and `vbp_imbalance` from `NQ_front_volroll.duckdb`.

The checked revision is `0a9d8093c12e7796a84c3fc5da5e82a5c2c2ef5f`. I need the original definitions and outputs reproduced. Please do not invent a replacement formula, guess missing labels, optimize the strategy, or change the F3 filter.

## What is already available

- The raw `NQ_front_volroll.duckdb` is already available in my Library. There is no need for another raw-data upload. Its original Windows location is `C:\SierraChart\Data\Converted\NQ_front_volroll.duckdb`.
- Database identity from the prior audit: 3,592,433,664 bytes; SHA-256 `9edd9bbbc3f32fe5ef4b0fec3edf7da5be34becb71b62d6b62896d361a0efb34`.
- Prior audit coverage: 156,507,521 ticks, 4,456,631 bar rows, and 326 database session dates from 2025-06-17 through 2026-09-17.
- The repository has `data/F3_OR5_features.csv`, `data/F3_OR5_features.parquet`, `data/F3_0_days.csv`, `data/CANDLE_FORCE_v3_multiday_bars.parquet`, and `scripts/score_candle_force_day_v2.py`.
- The CSV was rechecked at the revision above: 172 unique feature dates from 2025-12-16 through 2026-08-25, with exactly 50 F3 flags. Those 50 dates match `F3_0_days.csv`.

The remaining 154 database session dates have no supplied OR5 feature row. The prior coverage audit found 74 non-red OR5 dates excluded from F3, one date with no RTH data, and 79 red OR5 dates still unclassifiable because the original features are missing. Please verify that reconciliation and return the exact date manifest. A missing feature must not silently become a negative F3 label.

## Missing source files verified in this revision

The scorer imports these seven project modules, none of which is included in the repository:

1. `plot_daily_1h_ah_rth_dva.py`: `C_DN`, `C_UP`, `C_WICK` (chart constants)
2. `plot_pmor_orhl5_day.py`: `load_rth_15s`
3. `luis_or_127_close205.py`: `as_naive`
4. `luis_vbp_or.py`: `BARS_PER_5M`, `TICK`
5. `plot_eval3_5m_va_pmor.py`: `chunk_ohlc_delta`, `vbp_va70`
6. `candle_force_vbp_weights.py`: `ZONE_COLORS`, `compute_from_grp`, `draw_matrix_legend_png`, `is_doji_body`, `matrix_summary_text`, `zone_color_for_price`
7. `score_candle_force_day.py`: `AT_TICKS`, `DOJI_FRAC`, `FLATTEN_BAR`, `MARU_FRAC`, `bar_vwap`, `load_footprint`, `pick_db`, `third_loc`, `vote_body`, `vote_delta`, `vote_marubozu`, `vote_volume`, `vote_vs_level`, `vote_vs_va`

These two additional helper files are dynamically loaded by the strategy runners and are also absent:

- `run_or5_short_hold_laxo_vs_ratchet.py`
- `run_or5_short_longfix.py`

Please include their transitive dependencies as needed. Distinguish dependencies required to generate the features from those needed only for charting or running the existing strategy.

Evidence: [scorer imports, lines 38–69](https://github.com/luislamlee/nq-or5-expand-inv/blob/0a9d8093c12e7796a84c3fc5da5e82a5c2c2ef5f/scripts/score_candle_force_day_v2.py#L38-L69) and [runner helper loads, lines 85–100](https://github.com/luislamlee/nq-or5-expand-inv/blob/0a9d8093c12e7796a84c3fc5da5e82a5c2c2ef5f/scripts/run_or5_1m_ls_expand_inv.py#L85-L100).

## Preferred return: the complete original generator

Please provide:

1. The original reusable source files above, all required transitive helpers, and the multiday driver/export code that produced the supplied features. The scorer currently emits a field named `score`; identify and supply the exact mapping to stored `score_v2`. Explain the provenance of the `CANDLE_FORCE_v3_multiday_bars.parquet` filename and which original score version it contains.
2. Original configuration and constants: all vote thresholds, doji/marubozu definitions, VbP zone boundaries and weights, price binning/tick rounding, POC tie-breaking, value-area construction and ties, imbalance formula and zero-denominator behavior, rounding, and missing-data handling.
3. The expected DuckDB input schema and exact mapping to this database: table/view names, required columns and types, timestamp conventions, footprint construction, and any prerequisite preprocessing. Do not assume a missing footprint or 15-second table exists. If it must be built from the available data, provide the original transformation.
4. Session and roll rules: trading-date assignment, timezone and DST handling, RTH boundaries, short/holiday sessions, incomplete or missing bars, contract selection and roll alignment, and any price adjustment. The scorer refers to Chicago trading dates and 08:30/08:35 displays, while OR5 is 09:30–09:35 America/New_York; document their exact alignment.
5. Python and package versions or a pinned environment, plus an exact tested command that processes all 326 session dates using a configurable database path and output directory. Remove machine-specific path assumptions without changing calculations.
6. Source commit/version identifiers, input-file hashes, configuration, and generation logs. Preserve the original 172 feature rows as a regression fixture.

Please return a source bundle, or commit the missing reusable source/configuration files to the repository and give the commit SHA and run command. Do not include market tick data, the DuckDB file, credentials, or unrelated private files.

## Alternative return: exact original features for the full coverage

If recovering the runnable generator is not possible, return the exact feature export generated with the original implementation for every valid OR5 session in the 326-date database universe, plus a 326-row coverage manifest.

Keep the existing CSV columns:

```text
trading_day,contract,m5_bar,open,high,low,close,vol,delta,or5_red,score_v2,vbp_imbalance,vbp_zone_weight,poc,val,vah,vwap,f3_0
```

Preserve the 172 known rows unchanged in the consolidated export. Separately provide a regenerated-versus-original comparison for those dates and an explicit list of the 154 previously missing dates. Include the same provenance, definitions, and session/roll rules requested above.

For dates where the original features cannot be produced, leave unavailable feature values null and record a reason in the coverage manifest. Distinguish “no RTH,” “non-red OR5, excluded from F3,” “features generated,” and “features missing.” Known non-red dates can be excluded by the unchanged color rule even if their scores remain unavailable; the 79 red dates cannot be classified without the missing features. Do not create synthetic OR5 rows for the no-RTH date or assign guessed scores.

## Required regression and no-lookahead checks

The fixed F3_0 rule is:

```text
OR5 = first RTH 5-minute candle, 09:30–09:35 America/New_York
f3_0 = (close < open) AND (score_v2 <= -5) AND (vbp_imbalance <= 0)
```

- Reproduce the original 172 rows' feature values at their stored precision and the exact 50 F3-positive dates, not just the count. Report every mismatch and its cause; do not overwrite the reference to make the comparison pass.
- Confirm the `score_v2` definition and all component votes. The supplied scorer uses 16 equal-vote checks, with previous-bar checks 10–15 zero for the first RTH bar.
- Verify OR5 features use only data available when OR5 completes. No later-session volume profiles, revised roll selection using future information, or other lookahead may enter the signal. If the original process has such a limitation, disclose it rather than silently changing it.
- Show consistent session dates, contracts, OHLC, volumes, and timestamps between the original sample and this database. Identify roll or source-data differences explicitly.
- Keep this feature-recovery request separate from any new experimental entry, exit, stop, target, or optimization work. The existing 50 F3 signal days are not the same as the strategy's eventual trade count.

Please start your reply with what you found, which exact files or outputs you can return, and anything still missing. If the original generator or exact labels cannot be recovered, say so explicitly.

