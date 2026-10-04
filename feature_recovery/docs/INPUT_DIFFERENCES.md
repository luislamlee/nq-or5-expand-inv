# Why VolRoll alone differs from the historical H26 features

This is a verified input-data distinction. The original scorer, vote thresholds, F3 filter and reference rows were not changed to make the comparison pass.

## Sixteen dates: one-second source bars versus individual trades

Affected dates: 2026-02-23, 02-24, 02-25, 02-26, 02-27, 03-02, 03-03, 03-04, 03-05, 03-06, 03-09, 03-10, 03-11, 03-12, 03-13 and 03-16.

The original H26 companion Parquet explicitly marks all 300 OR5 source records on each of these dates as `record_type='bar'`. The original `fill_footprint_15s.py` therefore allocates each one-second bar's volume to its `(high+low+close)/3` price rounded to the 0.25 tick. VolRoll stores the individual trades and allocates their volume to trade-close prices. OHLC, volume, delta and five-minute VWAP agree, while price-volume allocation, POC/value area and some votes differ.

A diagnostic reconstructed one-second OHLC from VolRoll and applied the recovered original HLC3 transformation. All **4,257 original 15-second price-volume buckets** across these sixteen dates matched exactly, including volume, number of trades and source-record count. This proves the mechanism without inventing a formula or choosing an optimized threshold. The diagnostic was not used to guess source resolution on other dates.

## February 17: records absent from VolRoll

On 2026-02-17, original H26 15-second bar #6, 08:31:15–08:31:30 Chicago, has 1,640 volume versus VolRoll's 1,544. Original bid/ask are 739/901 versus 679/865, so delta is 162 versus 186; OHLC matches.

An exact record multiset comparison against the original companion Parquet found **95 original records missing from VolRoll**, totaling **96 volume: 60 bid plus 36 ask**. They occur from **14:31:25.992000 through 14:31:26.983001 UTC**. The reverse difference is zero. These missing records cannot be reconstructed from the supplied VolRoll alone. Neither data source is silently patched or declared universally correct.

## Source identities and regression

- Original H26 derived database: `NQH26-CME-15s.duckdb`, 40,906,752 bytes, SHA-256 `d218ec0713d4162a4dfe179bf531008e36eae576cc6167a08106d75222e6d36a`
- Its metadata names `NQH26-CME.scid`, 930,945,776 bytes, 23,273,643 records, exported 2026-09-09
- VolRoll metadata names the distinct `NQH26_FUT_CME.scid`, 1,233,711,896 bytes, 30,842,796 records
- Original companion `NQH26-CME.parquet`: 255,672,416 bytes, SHA-256 `67e0ca027f40aa195057359141c1611b15dd29bee750d8af6ae66cc74c9bd8d2`

The recovered original H26 database reproduces all 60 H26 historical rows and every one of 47 shared scorer/stored fields exactly. The other 112 historical M26/U26 rows reproduce directly from VolRoll. Together, all 172 historical feature rows and the exact 50 F3-positive dates pass. Pure VolRoll still shows all 81 field differences on the 17 dates above, while its original-sample F3 membership remains the same 50 dates.

The supplied 172-row fixture is preserved unchanged. The mixed-provenance `F3_OR5_features_legacy_preserved.csv` labels row origin in the coverage manifest; it must not be described as a single-input VolRoll regeneration. The separate `F3_OR5_features_volroll_regenerated.csv` is the consistent full-VolRoll research export.

Validation evidence is under `../validation/`. No individual market-tick rows, source Parquet, or database is included in this delivery.
