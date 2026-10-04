# Original Candle Force v2: source contract and independent artifact audit

## Verified findings

The supplied 172-row CSV is recoverable exactly from `CANDLE_FORCE_v3_multiday_bars.parquet`, including identical CSV bytes after selecting `m5_bar=1`, retaining the original column order, and applying the stated red/F3 flags. This is re-extraction of original stored output, not regeneration from raw data.

- Original CSV SHA-256: `4f4706823ed2dcba7cc662948710b01998749d30f75664f8b2a836367865ca65`
- Small Parquet SHA-256: `7d9196858c5816f85e33419d89934ac0db401864cb2d5aa45b05bb05b577a38a`
- F3 dates CSV SHA-256: `c5cba832a01c1607315fe5ec4d8407d37c88b6fcd02b5ec6dad9c9a088d8d088`
- Multiday Parquet SHA-256: `700b284648f83bb21f24c273ff1366a4c4c9c2087ba173326d764d7dc26777aa`
- V2 scorer SHA-256: `dddbb8b86c9a01c21330a3d9f2eb133535f7c49d4698d563ff38883b273f96a5`

The small Parquet equals the CSV in all 18 columns and dtypes. The multiday file contains 13,416 rows, 103 columns, exactly the same 172 dates, and 78 bars per date. There are no additional original feature dates in it. All 16 shared OR5 fields match exactly. All 50 F3-positive dates match the original date CSV in order and values. All 172 dates and the 50 positive dates are exported separately in this audit. There are 88 red and 84 non-red OR5 candles in the original 172.

All 13,416 rows satisfy `score9=sum(first 9 votes)`, `score15=sum(first 15 votes)`, and `score=score_v2=sum(all 16 votes)`. The first bar's six previous-bar votes and volume-median vote are zero on all 172 dates. Stored imbalance equals the original formula evaluated on the stored weighted masses and rounded to 6 places, with zero mismatches.

Sample contracts: H26, 60 dates, 2025-12-16 through 2026-03-16; M26, 63 dates, 2026-03-17 through 2026-06-15; U26, 49 dates, 2026-06-16 through 2026-08-25. Every stored start/end string lies on the 08:30–15:00 Chicago five-minute grid. These strings contain no offset/timezone metadata. Explicitly interpreting them as America/Chicago converts OR5 to 09:30–09:35 America/New_York on every sample date, including both sides of the March DST change.

`existing_features_audit.json` and `regression_mismatches.json` record the checks, exact manifests, versions and hashes. The untouched CSV fixtures are in `fixtures/`. No repository code was imported or executed by `audit_existing_features.py`.

## Source provenance and score-version mapping

Recovered source repository: `luislamlee/nq-master-algo`, commit `56c9056fa414ebf93e3285e240368d5c4bfdb53b`. Public reference repository base: `0a9d8093c12e7796a84c3fc5da5e82a5c2c2ef5f`; inspected main: `7e7a409675741106d877fc0380ba615832ec7deb`. Main only adds `chat.md` to the original reference. The recovered V2 scorer is byte-identical to the public reference scorer, Git blob `2d95e496ab8b5cfc5640229df1e144302f037667`. The source retrieval manifest records other Git blobs; this audit writes an additional independent SHA-256/Git-blob check.

The exact original mapping is explicit in `sierra/candle_force/run_candle_force_v21.py:151–152`: select `VOTE16_COLS`, cast to int, sum across columns, store as `score_v2`. The original scorer stores the same sum as `score` at `score_candle_force_day_v2.py:273–308`.

The filename `CANDLE_FORCE_v3_multiday_bars.parquet` identifies an enriched research export, not a replacement of `score_v2` with V3. `run_candle_force_v3.py:40,795` loads `CANDLE_FORCE_v21_multiday_bars.parquet`; lines 983–990 save all resulting columns except `wcontrib_*`, retaining the old `score` and `score_v2` alongside V2.1/V3 results. This matches the supplied schema and row-by-row equality. `run_candle_force_v3.py:121–127` creates within-day forward returns; the supplied `fwd_ret_1` and `fwd_ret_3` exactly equal the future 1-/3-bar close minus the current close. These are outcomes and must never be signal inputs.

Parquet writer metadata reports PyArrow 25.0.1 and pandas 3.0.6. That is writer provenance, not a complete original Python/DuckDB/environment lock. Hard-coded Python 3.13 paths in the scripts are another clue, not a pinned environment. The source commit plus exact files can support a newly tested, pinned environment, but should not be presented as proof of the historical one.

## Exact original V2 definitions

References below are to the recovered source, relative to `sierra/`. Formulas are documented from source rather than inferred from labels.

### Constants and base votes

`luis_vbp_or.py:40–45`: tick=0.25; 20 15-second bars per 5-minute candle. `candle_force/score_candle_force_day.py:46–49`: `FLATTEN_BAR=1560`, `DOJI_FRAC=0.10`, `MARU_FRAC=0.70`, `AT_TICKS=0.5`, so the at-level tolerance is 0.125 points.

In the following, O/C/L/H are the current candle, range=H−L, body=abs(C−O), and signs are −1/0/+1. Floating-point boundary tolerances matter; source is authoritative.

1. `body` (`score_candle_force_day.py:117–126`): zero if body < 0.25−1e−12, or range>1e−12 and body < 0.10×range−1e−12; otherwise +1 for C>O+1e−12, −1 for C<O−1e−12, else 0.
2. `close_loc` (`:104–114`): flat range <=1e−12 or nonfinite price gives 0; (C−L)/range >=2/3 gives +1, <=1/3 gives −1, otherwise 0.
3. `vs_poc` (`:129–136`): nonfinite POC gives 0; abs(C−POC)<0.125+1e−12 gives 0; otherwise +1 above, −1 below.
4. `vs_va` (`:139–146`): nonfinite bounds give 0; C>VAH+1e−12 gives +1, C<VAL−1e−12 gives −1, otherwise 0.
5. `vs_vwap`: same at-level rule as POC, using this candle's VWAP.
6. `hvn_shape`: apply the same thirds rule as `close_loc` to the current candle's POC, with nonfinite POC giving 0.
7. `volume` (`:149–159`): first-bar/no-finite-prior-median gives 0; current volume must exceed median of earlier completed candles by >1e−12, then return body direction; doji remains 0. Median uses prior candles from the same scoring run/day, not later volume.
8. `delta_vote` (`:162–165`): abs(delta)<1e−9 gives 0; otherwise +1 if positive, −1 otherwise. The function does not independently reject NaN; inputs require validation.
9. `marubozu` (`:168–179`): range<=1e−12 gives 0; body/range <=0.70+1e−12 gives 0; otherwise return candle direction with ±1e−12 comparisons.

### Previous-bar votes 10–15

`candle_force/score_candle_force_day_v2.py:143–198,258–270`:

10. `prev_close`: sign(C−previous C), zero when absolute difference<1e−12.
11. `prev_break`: +1 when C>previous H+1e−12; −1 when C<previous L−1e−12; else 0.
12. `prev_delta`: nonfinite input gives 0; sign(current delta−previous delta), zero for absolute difference<1e−9.
13. `prev_volume`: 0 unless volume>previous volume+1e−12; then current body direction (0 for doji).
14. `prev_poc`: nonfinite input gives 0; zero when absolute POC change<0.125+1e−12; otherwise its sign.
15. `prev_body`: current body direction if non-doji and absolute body size exceeds previous absolute body size by >1e−12; else 0.

All six are explicitly zero when no prior scored candle exists. The original code advances `prev` only after a successfully scored chunk, so after skipped chunks it compares against the preceding scored candle, which need not be the immediately preceding clock interval. For OR5, these are always zero and cannot introduce preceding-day information.

### Check 16: VbP zone weights

`candle_force/candle_force_vbp_weights.py:55–56,105–186,204–292`:

- Group only footprint rows for the current candle by exact stored price, sorted ascending, and sum total volume (`score_candle_force_day_v2.py:239–242`). The scorer adds no further binning.
- Use the same doji threshold as body. Non-doji body is [min(O,C),max(O,C)] with midpoint=(body low+body high)/2.
- Price below body low−1e−12: 3×volume toward long. Above body high+1e−12: 3×volume toward short.
- Inside the body, bull top 25% (price>=body high−0.25×body range−1e−12) is 2×volume long; bear bottom 25% (price<=body low+0.25×body range+1e−12) is 2×volume short. Near-close classification takes precedence over remaining body classification.
- Other body prices below midpoint−1e−12 add 1×volume long; above midpoint+1e−12 add 1×volume short; midpoint is neutral. All three categories also contribute 1×volume to contention (`w_body_fight`), but contention is not separately added to the imbalance denominator.
- Doji skips near-close. Position<=1/3+1e−15 adds 3×long; position>=2/3−1e−15 adds 3×short. The middle third splits at the full range midpoint using ±1e−12 into 1×long/short, and contributes to contention; exact midpoint is neutral. A flat range<=1e−12 is neutral contention.
- Skip nonfinite prices, nonfinite volumes, and volumes<=0.
- `imbalance=(w_long−w_short)/(w_long+w_short+1e−9)`. Empty input or all-neutral mass gives 0. `vote=+1` strictly above +0.15, −1 strictly below −0.15, else 0. The vote is calculated from the unrounded imbalance. Stored `vbp_imbalance` is rounded afterward to six decimals.

### POC, value area, OHLC and VWAP

- `plot_eval3_5m_va_pmor.py:166–191`: filter the same bar-number bounds, sort, take first open, max high, min low, last close, sum total volume; sum `volume_delta` and cast to int. It only falls back to ask−bid if `volume_delta` is absent, or zero if neither source exists. The actual loader's SELECT requires all three fields.
- `plot_eval3_5m_va_pmor.py:194–218`: current-candle footprint only; group price and sum total volume. Empty footprint returns POC/VAL/VAH NaN and volumes 0.
- `luis_vbp_or.py:70–80`: maximum-volume POC; tied maximums choose the price closest to this candle's close; an equal-distance tie selects the lower price. Exact equality identifies volume/distance ties.
- `luis_or_127_delta.py:82,136–180`: VA target=70% of summed profile volume. Start at POC (first price within absolute 1e−9), expand contiguously through adjacent observed profile rows, not artificially filled zero-volume ticks. Choose the larger adjacent volume; equal adjacent volumes include both sides even if that exceeds target. Stop when accumulated volume+1e−12 >= target. Empty/nonpositive profile, invalid POC or missing POC yields NaN bounds and 0 volume.
- `score_candle_force_day.py:182–193`: VWAP is sum(((15s H+L+C)/3)×15s volume)/sum(15s volume) for this candle only. It is not exact trade-price VWAP or developing full-session VWAP. Nonpositive total volume gives NaN.
- `score_candle_force_day_v2.py:280–311`: OHLC, POC/VAL/VAH, VWAP, weighted masses and zone volume components are Python-rounded to four decimals; imbalance to six; total volume and delta remain floats. Votes are computed before output rounding. Source values and rounding must be preserved rather than adjusting the reference CSV to pass.

## Input contract and original transformations

`plot_pmor_orhl5_day.py:126–146` requires `bars_15s` with trading_day, rth_bar_number, bar_start_chicago, bar_end_chicago, open/high/low/close, total_volume, volume_delta, bid_volume, ask_volume. It filters day and non-null bar number, sorts by bar number and adds naive `t`/`t_end`. `score_candle_force_day.py:84–101` requires `footprint_15s_by_price` with trading_day, rth_bar_number, price and total_volume. No native VolRoll table is loaded directly by the scorer.

`as_naive` (`luis_or_127_close205.py:81–90`) drops timezone information; it does not convert a UTC datetime to Chicago. Therefore inputs must already be Chicago-local before this function is used.

Two original preprocessing paths differ and must not be conflated:

1. `build_bars_15s_rth_export.py` reads `scid_records_sessionized` with existing session_id, trading_day and `session_segment='RTH_CASH'`; UTC-buckets 15 seconds, aggregates first/last timestamps, and creates Chicago-local labels. For records typed `bar`, OHLC uses that bar's range; otherwise it uses coalesced trade_price/close. It numbers observed rows, not expected clock slots. `fill_footprint_15s.py:47–115` uses typical price (H+L+C)/3 for record_type='bar', otherwise coalesce(trade_price,close); rounds to 0.25 using DuckDB SQL `round`, groups by UTC bucket and price, and joins to existing bars. This requires original sessionized source/preprocessing not present in raw VolRoll's `ticks` schema.
2. `candle_force/extend_u26_15s_from_volroll.py:3–6,40–65,93–163,172–235` appends from raw VolRoll `bars_t_15s` and `ticks`; for footprint it uses tick close, total_volume>0, finite close>1000, SQL `round(close/0.25)` and 15-second UTC buckets. It hard-codes 13:30–20:00 UTC and NQU26-CME, intended only for the summer U26 extension. Non-U26 roll dates cause a warning, not rejection. Do not use this unchanged for winter, other contracts, or all 326 dates.

A portable adapter must explicitly identify the selected source transformation, treat raw naive VolRoll timestamps as UTC, compute date-specific 09:30–16:00 New York/08:30–15:00 Chicago boundaries with DST, and preserve source prices and contract membership. No price adjustment is performed by the recovered scorer/extension; whether earlier raw construction performed one remains separate input provenance. An adapter is new portability code around recovered formulas, not a recovered historical all-326 original driver.

## Missing data, short sessions and causal limits

- The scorer caps bar numbers at 1560, derives floor(max bar number/20), and skips chunks with fewer than 20 rows. It does not validate uniqueness, expected timestamps or contiguity. Row-number-based preprocessing can conceal time gaps by compressing observed bars.
- Empty/missing footprint is not automatically a missing feature: the original returns NaN POC/VA but zero weighted masses/imbalance/vote. This can silently make unavailable evidence look neutral. A validating coverage wrapper should reject absent or incomplete footprint inputs before scoring and leave unavailable features null, with explicit reason. Preserve and disclose this original behavior.
- The original full-history driver uses `complete_days`, or a fallback requiring at least 1560 rows/max bar number; its cache/merge stage also requires at least 70 five-minute bars. The observed 172 dates each have all 78 bars. This is a full-day completeness selection and excludes shortened/partial days independently of whether their OR5 was valid. Later-day completeness is not information known at OR5 close. An all-valid-OR5 coverage export should not silently claim this original universe-selection filter was causal.
- `run_candle_force_v21.py:77–91` selects contract-specific complete days against raw `roll_calendar`, but accepts a day when the calendar entry is absent. Missing roll entries should be explicit diagnostics, not guessed contract labels. The code does not establish whether roll_calendar itself was fixed as of OR5, based on prior-day volume, or revised with full-day/future volume; that construction must be audited separately before certifying no roll lookahead.
- `run_candle_force_v21.py:114–145` combines v2 contract-DB features with raw VolRoll diagonal features positionally, truncates to the shorter length, checks maximum close difference<=0.5, but does not assert matching timestamps/bar numbers. The v2 columns remain the contract-DB outputs. Exact timestamp/contract validation is needed before assuming the enriched file proves a uniform raw-data source.
- The v2 current-bar calculation is statically causal once session/contract input rows are fixed: helpers restrict OHLC/footprint to b0..b1, VWAP uses the current chunk, volume uses prior completed chunks, previous-bar votes use stored prior values. It does not read later-session profiles to calculate current votes.
- V3 fits signed weights using in-sample returns through 2026-06-10 (`run_candle_force_v3.py:403–482,809–841`). This is future-fitted for earlier in-sample dates; OOS data only flags documentation flips per the code. It does not alter the original v2 columns, and V3 should not be substituted into the fixed F3 rule.

## Required regression and no-lookahead validation

1. Generate fresh features from the verified database and recovered implementation without reading the reference CSV as a computational input. Join by trading day and contract; compare every stored field, every vote, all 172 original rows at stored precision, and the exact 50 positive dates. Report each mismatch, raw inputs and cause. Output generated and preserved-reference values separately.
2. Keep the fixed rule `close<open AND score_v2<=-5 AND vbp_imbalance<=0`. The existing strategy helper uses EPS=1e−9 comparisons; this yields the same supplied labels but should be documented instead of silently changing the explicitly requested rule.
3. For every valid OR5, assert 20 unique expected 15-second starts, first start 09:30 NY, last end 09:35 NY, finite coherent OHLC, finite positive profile totals and valid contract mapping. Check footprint volume against bars, including every 15-second slot; report differences, do not hide them with filling.
4. Run the original scorer on OR5-only input and on full-day input, then compare OR5 including component votes. Repeat after replacing/removing all rows strictly after OR5 with extreme OHLC, delta and footprint values. OR5 must remain invariant. These are test cases, not completed results in this static audit.
5. Validate DST on both standard- and daylight-time dates and around transition weekends. Verify Chicago/New York alignment, not fixed UTC clock times. Compare DB roll selection to supplied contract codes and audit how the roll calendar was produced.
6. Enumerate the 326-date universe from the database; keep no-RTH distinct from valid non-red OR5 excluded by color, generated features and missing features. Do not assign negative F3 to unknown red days. Original 172 fixture stays byte-identical; a larger generated export and coverage manifest are separate.
7. Hash raw input, original/recovered sources, new adapter, configuration and outputs; record installed package versions and actual tested command. Distinguish this materialized-output audit, a raw regeneration test, and any remaining source/roll limitations.

## Dependency roles

Feature calculations directly need the loaders, time conversion, TICK/BARS_PER_5M, OHLC aggregator, POC/VA construction, base votes, VWAP and zone-weight functions. `luis_or_127_delta.value_area` is a computational transitive dependency. Color constants, zone colors, chart rendering, legends and descriptive text are chart-only logically, but top-level imports in the original modules still require their files/packages when running the unchanged source. The two `run_or5_short_*` helpers are strategy-runner dependencies, not necessary for generating the feature table. V2.1 diagonal/V3 drivers establish export provenance; they are not required to calculate the fixed OR5 v2 signal and add full-day-data and future-return analysis that must remain separate.

## Subsequent independent database and mismatch diagnosis

After the verified raw database and original H26 source were recovered, the read-only `audit_database_coverage.py` check independently reconciled the requested universe. Exactly 326 roll-calendar dates span 2025-06-17 through 2026-09-17. All 325 dates with RTH have a complete fixed-clock OR5 (five unique one-minute starts from 09:30 through 09:34 NY, ending 09:35). The one no-RTH date is **2026-04-03**. The previously absent 154 feature dates reconcile to **79 red, 74 non-red, and one no-RTH**. Exact date manifests are `coverage_manifest_before_regeneration_326.csv` and `previously_missing_feature_dates_154.csv`; results and schema are in `database_coverage_audit.json`.

The initial recovered-scorer probe using uniform quarter-tick trade-close footprints from VolRoll produced 81 field differences on 17 known dates, while still reproducing the exact original 50 F3-positive dates. This did not satisfy feature regression. The differences have now been traced to source data, without fitting formulas to labels:

### Sixteen H26 dates use one-second source bars, not individual-trade price allocation

Dates: 2026-02-23, 02-24, 02-25, 02-26, 02-27, 03-02, 03-03, 03-04, 03-05, 03-06, 03-09, 03-10, 03-11, 03-12, 03-13 and 03-16.

Original H26 `bars_15s` and footprint metadata report exactly 300 source records per OR5 on each of these dates, even though the corresponding trade counts are 7,246–19,709. The recovered **original companion Parquet directly labels all those records `record_type='bar'`**, with 300 distinct seconds. Before this block, its records are `first_sub_trade`, `last_sub_trade` and `single_trade_bid_ask`. This activates the already-recovered `fill_footprint_15s.py` rule allocating each source bar's total volume to its HLC3 typical price rounded to 0.25.

As a separate diagnostic, aggregate raw VolRoll trade closes to one-second high/low/last-close and volume, then apply precisely that recovered HLC3/quarter-tick footprint transformation. **Every one of the 4,257 original 15-second price buckets across these 16 days matches exactly**, including total volume, number of trades and original-source record count. There are zero differences. This establishes the source-resolution mechanism; it is not 5-point binning, a changed scoring threshold or reverse engineering a formula from target labels. The diagnostic alone should not authorize choosing a source resolution for other unseen dates.

Evidence: `h26_source_record_types.csv`, `h26_original_footprint_characteristics.csv`, `h26_one_second_resolution_diagnostic.json`; repeatable test `diagnose_h26_resolution.py`; `h26_one_second_resolution_bucket_mismatches.csv` contains its header only. No individual market tick rows were exported.

### One H26 date contains source records absent from VolRoll

On **2026-02-17**, the discrepancy is confined to 08:31:15–08:31:30 Chicago (14:31:15–14:31:30 UTC), 15-second bar #6. OHLC agrees. The original source bar has volume 1,640 vs VolRoll's 1,544, bid 739 vs 679, ask 901 vs 865, delta 162 vs 186, and 1,564 original records vs 1,469 raw trades.

An exact normalized-record multiset comparison with the companion original Parquet confirms **95 original records absent from VolRoll**, totaling **96 volume = 60 bid + 36 ask**. They span **14:31:25.992000 through 14:31:26.983001 UTC**. The reverse multiset difference is zero: every VolRoll record in this window is in the original source. Those records cannot be recovered from the supplied VolRoll alone. This explains the date's volume, delta, VWAP and imbalance differences without manufacturing any data. See `h26_feb17_source_difference_summary.json` and `h26_feb17_bar_comparison.csv`.

### Original H26 source identity and restored regression

The recovered original H26 database SHA-256 is `d218ec0713d4162a4dfe179bf531008e36eae576cc6167a08106d75222e6d36a`. Its own build metadata identifies `NQH26-CME.scid`, 930,945,776 bytes and 23,273,643 records, exported 2026-09-09. VolRoll's `source_files` identifies a distinct `NQH26_FUT_CME.scid`, 1,233,711,896 bytes and 30,842,796 records. The source names and counts are not interchangeable. The original companion Parquet also contains 23,273,643 records, and its metadata reports writer DuckDB v1.5.5.

Running the recovered scorer on this exact original H26 database restores **zero differences across all 47 common scorer/stored columns for all 60 H26 OR5 dates**, including every component vote, weighted mass and zone-volume component. Independent comparison is in `legacy_h26_independent_all_component_comparison.json`. The fresh legacy source output is distinct from the preserved fixture and proves input-source restoration rather than copying a CSV score into the regeneration.

A reproducible all-coverage result must therefore disclose its sources: the supplied VolRoll supports a fresh raw-only run, but exact original 172-row regression requires original H26 source data for the incompatible dates. Do not claim the exact historical features were all generated solely from the supplied VolRoll or silently insert missing trade records. Keep original and raw-only comparison outputs, source selection and the identified mismatch causes explicit.

### Executed no-lookahead perturbation test

`test_original_scorer_no_lookahead.py` now executes the future-input invariance checks recommended above. It runs the actual recovered original scorer on five dates: 2025-12-17, 2026-02-23, 2026-03-06, 2026-03-09 and 2026-08-13. They cover standard time, original one-second H26 source records, both sides of the March DST change, and daylight-time VolRoll inputs.

For each date, all **46 original scorer output fields** were compared in three conditions: original source/full-day versus a disposable OR5-only database; OR5-only versus the same OR5 plus a complete extreme-positive future candle and footprint; and OR5-only versus replacement of that future candle/footprint with extreme-negative values. **All 15 comparisons passed with zero differences**. All source databases were read-only; only disposable derived copies were mutated and those copies were removed afterward. Results: `no_lookahead_perturbation_test.json`.

This establishes current-bar feature invariance to later candle/footprint input under those cases. It does not certify that the original full-day-completeness universe was available at 09:35, or independently prove how the historical roll calendar was constructed. The recovered handoff document states that volume rolls apply the next session and prices are not back-adjusted; implementation-level roll-calendar verification remains the appropriate final check for that separate claim.
