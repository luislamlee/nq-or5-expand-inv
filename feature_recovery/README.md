# Original Candle Force v2 recovery

## Result and important input distinction

The seven missing modules and their transitive imports were recovered from `luislamlee/nq-master-algo` at commit `56c9056fa414ebf93e3285e240368d5c4bfdb53b`.

The recovered V2 scorer is byte-identical to the scorer in reference commit `0a9d8093c12e7796a84c3fc5da5e82a5c2c2ef5f`. No scoring formula, threshold, F3 filter, or trading strategy was invented or optimized.

**All 172 historical feature rows reproduce exactly, including the exact 50 F3 dates, when the original H26 input is used. VolRoll alone differs on 17 H26 dates in 81 fields.** The difference is in the input data, not a changed formula. Those differences remain visible in their own comparison file; the baseline is never overwritten.

The full VolRoll universe contains 326 dates. The recovered original formula produces 325 valid OR5 rows and 100 F3-positive dates: the same 50 known dates plus 50 newly classified dates. `2026-04-03` has no RTH data, so it has no synthetic feature row and its manifest F3 value is null. The 154 previously missing dates reconcile as 79 red, 74 non-red, and one no-RTH date. New output is research data, not an investment recommendation or a new strategy backtest.

## Files to use

- `outputs/F3_OR5_features_volroll_regenerated.csv`: logically consistent, pure-VolRoll research output for all 325 valid sessions; this is the default research dataset
- `outputs/F3_OR5_features_legacy_preserved.csv`: an explicitly mixed-provenance alternative; retains the original 172 CSV rows byte-for-byte and adds the 153 previously missing valid-session rows from VolRoll
- `outputs/F3_OR5_historical_regenerated_172.csv`: fresh historical regression using original H26 input and VolRoll for M26/U26; this is independently generated, not copied from the fixture
- `outputs/coverage_manifest_326.csv`: all 326 dates, validity checks, row origin, original coverage, and source-version differences
- `outputs/previously_missing_dates_154.csv`: exact requested date manifest
- `outputs/regression_volroll_vs_original.csv`: all 81 field differences, unchanged and disclosed
- `outputs/regression_original_inputs_vs_original.csv`: header only, zero mismatches
- `outputs/component_votes_*.csv`: all 16 votes and intermediate original scorer outputs
- `outputs/generation_summary.json`, `generation.log`, and `output_hashes.json`: validation, inputs, package versions, and output hashes
- `fixtures/`: untouched original 172-row CSV and 50-date CSV
- `docs/ORIGINAL_DEFINITIONS.md`: exact original formulas, rounding, POC/VA ties, input schema, provenance, and limitations
- `docs/INPUT_DIFFERENCES.md`: demonstrated source-resolution change and the February 17 data gap
- `original/`: exact recovered sources, with their Git blobs and SHA-256 hashes in `source_manifest.json`
- `portable/`: the twelve scorer import dependencies with only historical machine-specific import bootstraps removed; all function/class ASTs are unchanged

Do not silently substitute either new export into an existing backtest. VolRoll-only and legacy-preserving outputs have different provenance even though their F3 membership agrees on the original 172 dates. The original 50 signal dates are not the eventual strategy trade count.

## Tested command

From the repository root, create a Python 3.12 environment and install the pinned test dependencies:

```sh
python3.12 -m venv .venv-cf-recovery
.venv-cf-recovery/bin/python -m pip install -r feature_recovery/requirements.txt
.venv-cf-recovery/bin/python feature_recovery/generate_original_features.py \
  --db /your/data/NQ_front_volroll.duckdb \
  --historical-h26-db /your/data/NQH26-CME-15s.duckdb \
  --output-dir /your/output/cf-v2-recovery
```

On Windows, use the environment's `Scripts/python.exe` and quote Windows paths as needed. Output directory must not already exist. The source databases are opened read-only and are never modified. A small OR5-only adapter database is created inside the new output directory; it is not part of the published delivery.

The exact command above was tested with Python 3.12.14 and the pinned package versions. The original historical environment was not fully pinned; its Parquet writer metadata records pandas 3.0.6/PyArrow 25.0.1 and source paths mention Python 3.13. The tested recovery environment is recorded separately rather than presented as the historical one.

If `--historical-h26-db` is omitted, the generator still writes the complete VolRoll research export and comparison. It intentionally exits with status **2** because VolRoll alone cannot pass the historical input-specific regression. With both verified inputs, exact regression and data-quality checks pass and the exit status is **0**. The legacy-preserved file never makes the raw-data regression pass.

Independent checks, after generation:

```sh
python feature_recovery/validate_outputs.py /your/output/cf-v2-recovery
python feature_recovery/test_input_boundaries.py \
  --db /your/data/NQ_front_volroll.duckdb --output-dir /your/output/cf-boundary-tests
python feature_recovery/test_no_lookahead.py \
  --historical-h26-db /your/data/NQH26-CME-15s.duckdb \
  --or5-adapter-db /your/output/cf-v2-recovery/or5_research_adapter.duckdb \
  --output-dir /your/output/cf-lookahead-tests
```

Use the same pinned Python environment. These tests only mutate disposable copies in their new output directories. The delivered evidence includes 18 input-boundary cases and 15 original-scorer future-input comparisons across five standard/DST/source-resolution dates.

## Input identities

Raw inputs are deliberately excluded from this repository. Reuse the user's already available database and original H26 input; do not upload ticks, a DuckDB, or credentials here.

| Input | Bytes | SHA-256 |
|---|---:|---|
| `NQ_front_volroll.duckdb` | 3,592,433,664 | `9edd9bbbc3f32fe5ef4b0fec3edf7da5be34becb71b62d6b62896d361a0efb34` |
| `NQH26-CME-15s.duckdb` | 40,906,752 | `d218ec0713d4162a4dfe179bf531008e36eae576cc6167a08106d75222e6d36a` |
| Original 172-row fixture | 20,743 | `4f4706823ed2dcba7cc662948710b01998749d30f75664f8b2a836367865ca65` |

The CLI rejects different input hashes and a modified reference fixture. It does not silently claim validation against a new data version. The H26 file's owner can access it in the existing Drive `Converted_NQ` project files; no public copy or sharing permission change was made.

## Session, preprocessing, and no-lookahead rules

- Fixed OR5 is `[09:30,09:35)` America/New_York, exactly `[08:30,08:35)` America/Chicago. IANA timezone conversion handles both standard time and DST; UTC opens are 14:30 or 13:30 respectively
- VolRoll timestamps are UTC-naive. The adapter uses its existing `roll_calendar` and explicit UTC connections; it does not choose a new contract or back-adjust prices
- All 20 expected 15-second opens and closes must exist on the exact clock grid, with one matching tick contract, valid prices, and footprint volume equal to bar volume in each slot
- The original VolRoll footprint transformation is retained: trade price `close`, positive volume, finite price above 1000, DuckDB quarter-tick rounding, and grouped volume by 15-second slot/price
- The scorer receives only the first five minutes. No later-session profile, forward-return column, V3 weight, or subsequent volume enters the signal; previous-bar votes 10–15 are zero for OR5
- Short/holiday sessions with a valid OR5 are processed. Full-day bar counts are diagnostic only. The historical multiday sample used later-session completeness filters, which is an explicitly disclosed ex-post selection limitation of that old sample
- Empty/partial footprints, missing/misaligned slots, invalid prices, or contract inconsistencies are marked unavailable. They are never converted to a negative F3 label
- For the original H26 historical check, the original 15-second bars and footprint are passed directly to the unchanged scorer. Source resolution differs on the documented dates

## Dependency roles and licensing

The twelve portable modules are needed to import the original scorer. Several are charting/strategy modules whose helper functions or constants are imported transitively; chart/strategy functions are not run by this generator. Only the twelve necessary scorer/import modules are included. The two strategy-runner helpers, standalone preprocessing programs, historical multiday/V2.1/V3 research drivers, and diagonal-feature helper are not included in this public delivery. Their relevant schema/formula/version provenance is documented. The generator contains its tested input adapter and does not require those excluded programs. Existing strategy/backtest runners remain non-self-contained because their two helpers are not included. V3 training/reweighting is not used.

`score_v2` is the sum of the same 16 equal votes as the original `score` field. The historical `CANDLE_FORCE_v3_multiday_bars.parquet` filename denotes an enriched export retaining V2 alongside V2.1/V3 and future-return research columns; it does not change the F3 score definition.

No license file was present in the recovered source snapshot. Original authorship and source provenance are preserved; this recovery does not invent a new license or third-party reuse grant. The repository owner explicitly authorized publication of these necessary sources and derived features.
