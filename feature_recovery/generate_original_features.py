#!/usr/bin/env python3
"""Recover unchanged Candle Force v2 features with explicit input provenance.

Default research output is VolRoll-only. Historical H26 validation is separate.
No source DB is modified. An existing output directory is never overwritten.
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
EXPECTED_VOLROLL = '9edd9bbbc3f32fe5ef4b0fec3edf7da5be34becb71b62d6b62896d361a0efb34'
EXPECTED_H26 = 'd218ec0713d4162a4dfe179bf531008e36eae576cc6167a08106d75222e6d36a'
EXPECTED_REFERENCE = '4f4706823ed2dcba7cc662948710b01998749d30f75664f8b2a836367865ca65'


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def literal(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def verify_sources():
    manifest = json.loads((ROOT / 'source_manifest.json').read_text())
    for item in manifest['files']:
        original = ROOT / 'original' / item['source_path']
        if digest(original) != item['sha256']:
            raise ValueError(f'Original source hash mismatch: {original.name}')
        if 'portable_path' in item:
            portable = ROOT / item['portable_path']
            if digest(portable) != item['portable_sha256']:
                raise ValueError(f'Portable source hash mismatch: {portable.name}')
    return manifest


def prepare_volroll(db, adapter, duckdb):
    con = duckdb.connect(str(adapter))
    con.execute("SET TimeZone='UTC'; SET threads=4; SET memory_limit='4GB'")
    con.execute(f'ATTACH {literal(db)} AS vr (READ_ONLY)')
    schemas = {table: con.execute(f'DESCRIBE vr.{table}').fetchall()
               for table in ('ticks', 'bars_t_15s', 'bars_t_1m', 'roll_calendar')}
    duplicate_dates = con.execute('SELECT count(*)-count(DISTINCT session_date) FROM vr.roll_calendar').fetchone()[0]
    if duplicate_dates:
        raise ValueError('Roll calendar has duplicate session dates')
    con.execute('''CREATE TABLE sessions AS SELECT session_date trading_day, contract,
      (session_date+TIME '09:30:00') AT TIME ZONE 'America/New_York' AT TIME ZONE 'UTC' start_utc,
      (session_date+TIME '09:35:00') AT TIME ZONE 'America/New_York' AT TIME ZONE 'UTC' end_utc,
      (session_date+TIME '16:00:00') AT TIME ZONE 'America/New_York' AT TIME ZONE 'UTC' rth_end_utc
      FROM vr.roll_calendar ORDER BY session_date''')
    con.execute('''CREATE TABLE bars_15s AS SELECT s.trading_day,
      row_number() OVER(PARTITION BY s.trading_day ORDER BY b.ts_open)::INTEGER rth_bar_number,
      timezone('America/Chicago',b.ts_open AT TIME ZONE 'UTC') bar_start_chicago,
      timezone('America/Chicago',b.ts_close AT TIME ZONE 'UTC') bar_end_chicago,
      b.ts_open,b.ts_close,b.open,b.high,b.low,b.close,b.total_volume,
      b.delta volume_delta,b.bid_volume,b.ask_volume
      FROM vr.bars_t_15s b JOIN sessions s ON b.session_date=s.trading_day
      WHERE b.ts_open>=s.start_utc AND b.ts_open<s.end_utc ORDER BY s.trading_day,b.ts_open''')
    # Original VolRoll extension uses close, quarter-tick DuckDB round, and the
    # same positive-volume/finite-price filter. Only clock/contract scope is generalized.
    con.execute('''CREATE TABLE tick_quality AS SELECT s.trading_day,
      count(*) raw_tick_rows, count(DISTINCT t.contract) tick_contract_count,
      min(t.contract) tick_contract,
      count(*) FILTER(WHERE total_volume<=0 OR close<=1000 OR NOT isfinite(close)
        OR close IS NULL OR total_volume IS NULL OR t.contract IS NULL) invalid_ticks,
      count(*) FILTER(WHERE abs(close/0.25-round(close/0.25))>1e-9) off_tick_grid,
      min(ts) first_tick_utc, max(ts) last_tick_utc
      FROM vr.ticks t JOIN sessions s ON CAST(t.ts AS DATE)=s.trading_day
      WHERE t.ts>=s.start_utc AND t.ts<s.end_utc GROUP BY 1''')
    con.execute('''CREATE TABLE footprint_15s_by_price AS
      WITH t AS (SELECT s.trading_day,time_bucket(INTERVAL '15 seconds',ts) bar_start_utc,
        round(close/0.25)*0.25 price,total_volume FROM vr.ticks t
        JOIN sessions s ON CAST(t.ts AS DATE)=s.trading_day
        WHERE t.ts>=s.start_utc AND t.ts<s.end_utc AND total_volume>0 AND close>1000 AND isfinite(close))
      SELECT t.trading_day,b.rth_bar_number,t.price,sum(t.total_volume)::BIGINT total_volume
      FROM t JOIN bars_15s b ON t.trading_day=b.trading_day AND t.bar_start_utc=b.ts_open
      GROUP BY 1,2,3 ORDER BY 1,2,3''')
    # Later-session count is audit-only and never an eligibility/score condition.
    con.execute('''CREATE TABLE rth_coverage AS SELECT s.trading_day,count(b.bar_id) rth_15s_bars
      FROM sessions s LEFT JOIN vr.bars_t_15s b ON b.session_date=s.trading_day
      AND b.ts_open>=s.start_utc AND b.ts_open<s.rth_end_utc GROUP BY 1''')
    quality = con.execute('''WITH b AS (SELECT s.trading_day,count(b.rth_bar_number) or5_15s_bars,
      count(DISTINCT b.ts_open) distinct_slots,
      count(*) FILTER(WHERE b.rth_bar_number IS NOT NULL AND (b.ts_open IS NULL OR b.ts_close IS NULL
        OR b.ts_open<>s.start_utc+(b.rth_bar_number-1)*INTERVAL '15 seconds'
        OR b.ts_close<>s.start_utc+b.rth_bar_number*INTERVAL '15 seconds')) invalid_slots,
      count(*) FILTER(WHERE b.rth_bar_number IS NOT NULL AND (b.open IS NULL OR b.high IS NULL OR b.low IS NULL OR b.close IS NULL
        OR b.total_volume IS NULL OR b.volume_delta IS NULL OR b.bid_volume IS NULL OR b.ask_volume IS NULL
        OR b.total_volume<=0 OR NOT isfinite(b.open) OR NOT isfinite(b.high)
        OR NOT isfinite(b.low) OR NOT isfinite(b.close) OR NOT isfinite(b.volume_delta)
        OR b.high<greatest(b.open,b.close) OR b.low>least(b.open,b.close))) invalid_bars
      FROM sessions s LEFT JOIN bars_15s b USING(trading_day) GROUP BY 1),
      f AS (SELECT b.trading_day,count(*) FILTER(WHERE b.total_volume IS NULL OR f.fpvol IS NULL OR f.fpvol<>b.total_volume) bad_footprint_bars
        FROM bars_15s b LEFT JOIN (SELECT trading_day,rth_bar_number,sum(total_volume) fpvol
          FROM footprint_15s_by_price GROUP BY 1,2) f USING(trading_day,rth_bar_number) GROUP BY 1)
      SELECT s.*,b.* EXCLUDE(trading_day),q.* EXCLUDE(trading_day),
        coalesce(f.bad_footprint_bars,0) bad_footprint_bars,r.rth_15s_bars
      FROM sessions s JOIN b USING(trading_day) LEFT JOIN tick_quality q USING(trading_day)
      LEFT JOIN f USING(trading_day) JOIN rth_coverage r USING(trading_day) ORDER BY s.trading_day''').fetchdf()
    con.close()
    return quality, schemas


def row_problem(row):
    if row.rth_15s_bars == 0:
        return 'no RTH'
    if row.or5_15s_bars != 20 or row.distinct_slots != 20 or row.invalid_slots:
        return 'incomplete or misaligned OR5 15-second slots'
    if row.invalid_bars:
        return 'invalid OR5 OHLC/volume/delta'
    if row.bad_footprint_bars:
        return 'empty or volume-inconsistent OR5 footprint'
    if row.tick_contract_count != 1 or row.tick_contract != row.contract:
        return 'OR5 tick contract differs from roll calendar'
    if row.invalid_ticks or row.off_tick_grid:
        return 'invalid or off-grid OR5 tick input'
    return ''


def scored_first(day, db, scorer, votes):
    import pandas as pd
    result = scorer(pd.Timestamp(day).date(), db)
    result = result[result.m5_bar == 1].copy()
    if len(result) != 1:
        raise ValueError('Original scorer did not produce exactly one OR5 row')
    result['score_v2'] = result[votes].sum(axis=1).astype(int)
    assert (result.score_v2 == result.score).all()
    assert (result[[c for c in votes if c.startswith('prev_')]] == 0).all().all()
    result['or5_red'] = result.close < result.open
    result['f3_0'] = result.or5_red & (result.score_v2 <= -5) & (result.vbp_imbalance <= 0)
    return result


def compare(reference, generated, columns):
    import pandas as pd
    merged = reference.merge(generated, on='trading_day', suffixes=('_reference', '_generated'), validate='one_to_one', how='left', indicator=True)
    mismatches=[]
    for _, row in merged.iterrows():
        if row['_merge'] != 'both':
            mismatches.append(dict(trading_day=row.trading_day, column='__missing_row__',reference='present', generated='missing'))
            continue
        for column in columns:
            if column == 'trading_day': continue
            a,b=row[column+'_reference'],row[column+'_generated']
            if a != b and not (pd.isna(a) and pd.isna(b)):
                mismatches.append(dict(trading_day=row.trading_day,column=column,reference=str(a),generated=str(b)))
    return pd.DataFrame(mismatches,columns=['trading_day','column','reference','generated'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--historical-h26-db',type=Path,help='Original H26 input needed for exact historical regression; never changes the VolRoll-only export')
    parser.add_argument('--reference',type=Path,default=ROOT.parent/'data/F3_OR5_features.csv')
    args=parser.parse_args()
    args.db=args.db.expanduser().resolve();args.output_dir=args.output_dir.expanduser().resolve()
    if not args.db.is_file():parser.error('Database does not exist')
    if args.output_dir.exists():parser.error('Output directory already exists; choose a new directory')
    manifest=verify_sources()
    if digest(args.reference)!=EXPECTED_REFERENCE:parser.error('Reference fixture changed; refusing to validate against a modified baseline')
    if digest(args.db)!=EXPECTED_VOLROLL:parser.error('VolRoll hash differs from the audited input; no automatic data-version substitution')
    if args.historical_h26_db and digest(args.historical_h26_db)!=EXPECTED_H26:parser.error('Historical H26 hash differs from recovered original')
    args.output_dir.mkdir(parents=True)
    os.environ.setdefault('MPLCONFIGDIR',str(args.output_dir/'matplotlib_cache'))
    os.environ.setdefault('XDG_CACHE_HOME',str(args.output_dir/'cache'))
    sys.path.insert(0,str(ROOT/'portable'))
    import duckdb,numpy as np,pandas as pd
    from score_candle_force_day_v2 import score_day_v2,VOTE16_COLS
    reference=pd.read_csv(args.reference);columns=reference.columns.tolist()
    log=[]
    def report(message):
        print(message,flush=True);log.append(message)
    report('Verified source hashes and unchanged 172-row regression fixture')
    adapter=args.output_dir/'or5_research_adapter.duckdb'
    quality,schemas=prepare_volroll(args.db,adapter,duckdb)
    report(f'Prepared {len(quality)} session dates with source DB attached READ_ONLY')
    frames=[];statuses=[]
    for row in quality.itertuples(index=False):
        day=pd.Timestamp(row.trading_day).date().isoformat();reason=row_problem(row)
        if reason:
            statuses.append(dict(trading_day=day,status='no RTH' if reason=='no RTH' else 'features missing',reason=reason));continue
        try:
            f=scored_first(day,adapter,score_day_v2,VOTE16_COLS);f.insert(1,'contract',str(row.contract).removeprefix('NQ'))
            required=['score_v2','vbp_imbalance','vbp_zone_weight','poc','val','vah','vwap']
            if not np.isfinite(f[required].to_numpy(dtype=float)).all():raise ValueError('Nonfinite required feature')
            frames.append(f);statuses.append(dict(trading_day=day,status='features generated' if bool(f.or5_red.iloc[0]) else 'non-red OR5, excluded from F3',reason=''))
        except (Exception,SystemExit) as exc:
            statuses.append(dict(trading_day=day,status='features missing',reason=str(exc)))
    generated=pd.concat(frames,ignore_index=True).sort_values('trading_day')
    def export_features(frame):
        exported=frame[columns].copy()
        exported['or5_red']=exported['or5_red'].astype(int)
        exported['f3_0']=exported['f3_0'].astype(int)
        return exported
    export_features(generated).to_csv(args.output_dir/'F3_OR5_features_volroll_regenerated.csv',index=False)
    generated.to_csv(args.output_dir/'component_votes_volroll.csv',index=False)
    volroll_mismatches=compare(reference,generated,columns)
    volroll_mismatches.to_csv(args.output_dir/'regression_volroll_vs_original.csv',index=False)
    report(f'VolRoll-only: {len(generated)} generated dates, {int(generated.f3_0.sum())} F3 positives; {len(volroll_mismatches)} historical field differences')
    historical=generated[generated.trading_day.isin(reference.trading_day)].copy()
    if args.historical_h26_db:
        corrected=[]
        for day in reference.loc[reference.contract=='H26','trading_day']:
            f=scored_first(day,args.historical_h26_db,score_day_v2,VOTE16_COLS);f.insert(1,'contract','H26');corrected.append(f)
        historical=pd.concat([historical[historical.contract!='H26'],*corrected],ignore_index=True).sort_values('trading_day')
    export_features(historical).to_csv(args.output_dir/'F3_OR5_historical_regenerated_172.csv',index=False)
    exact_mismatches=compare(reference,historical,columns)
    exact_mismatches.to_csv(args.output_dir/'regression_original_inputs_vs_original.csv',index=False)
    historical.to_csv(args.output_dir/'component_votes_historical_172.csv',index=False)
    old_positive=set(reference.loc[reference.f3_0.astype(bool),'trading_day'])
    regenerated_positive=set(historical.loc[historical.f3_0,'trading_day'])
    volroll_positive=set(generated.loc[generated.trading_day.isin(reference.trading_day)&generated.f3_0,'trading_day'])
    passed=not len(exact_mismatches) and old_positive==regenerated_positive and len(historical)==len(reference)
    # A separate, explicitly named preservation export. Never overwrite the fixture
    # or call copied rows a raw-data regression. Exact original CSV row text is retained.
    reference_bytes=args.reference.read_bytes();reference_lines=reference_bytes.decode().splitlines()
    reference_rows={line.split(',')[0]:line for line in reference_lines[1:]}
    generated_lines=export_features(generated).to_csv(index=False).splitlines()
    generated_rows={line.split(',')[0]:line for line in generated_lines[1:]}
    consolidated_rows={**generated_rows,**reference_rows}
    consolidated='\n'.join([reference_lines[0]]+[consolidated_rows[d] for d in sorted(consolidated_rows)])+'\n'
    (args.output_dir/'F3_OR5_features_legacy_preserved.csv').write_text(consolidated)
    preserved='\n'.join([reference_lines[0]]+[consolidated_rows[d] for d in reference.trading_day])+'\n'
    preserved_bytes_exact=preserved.encode()==reference_bytes
    assert preserved_bytes_exact
    coverage=quality.copy();coverage['trading_day']=pd.to_datetime(coverage.trading_day).dt.strftime('%Y-%m-%d')
    coverage=coverage.merge(pd.DataFrame(statuses),on='trading_day',validate='one_to_one')
    coverage=coverage.merge(generated[['trading_day','or5_red','score_v2','vbp_imbalance','f3_0']],on='trading_day',how='left',validate='one_to_one')
    coverage['original_fixture_present']=coverage.trading_day.isin(reference.trading_day)
    coverage['volroll_feature_source']='VolRoll ticks and 15-second bars; original v2 formula'
    coverage.loc[coverage.status.isin(['no RTH','features missing']),'volroll_feature_source']='unavailable'
    coverage['legacy_preserved_row_source']=np.where(coverage.original_fixture_present,'unchanged original 172-row fixture','new VolRoll original-code generation')
    coverage.loc[coverage.status.isin(['no RTH','features missing']),'legacy_preserved_row_source']='no feature row'
    mismatch_days=set(volroll_mismatches.trading_day)
    coverage['legacy_vs_volroll_feature_mismatch']=coverage.trading_day.isin(mismatch_days)
    coverage['historical_regression_input']=np.where(coverage.original_fixture_present & (coverage.contract=='NQH26') & bool(args.historical_h26_db),'recovered original H26 15-second/footprint database',np.where(coverage.original_fixture_present,'VolRoll adapter','not in original fixture'))
    coverage.to_csv(args.output_dir/'coverage_manifest_326.csv',index=False)
    missing=coverage[~coverage.original_fixture_present].copy()
    missing.to_csv(args.output_dir/'previously_missing_dates_154.csv',index=False)
    generated.loc[generated.f3_0,['trading_day','contract','score_v2','vbp_imbalance']].to_csv(args.output_dir/'F3_positive_dates_volroll.csv',index=False)
    report(f'Original-input regression: {len(historical)} dates, {len(exact_mismatches)} feature differences; exact historical F3 dates: {old_positive==regenerated_positive}')
    report(f'Original fixture rows byte-preserved: {preserved_bytes_exact}; no-RTH dates: {coverage.loc[coverage.status=="no RTH","trading_day"].tolist()}')
    versions={name:importlib.metadata.version(name) for name in ['duckdb','numpy','pandas','pyarrow','matplotlib']}
    summary={'generated_at_utc':datetime.now(timezone.utc).isoformat(),'python':sys.version,'packages':versions,
      'source_repository':manifest['repository'],'source_commit':manifest['commit'],
      'input_hashes':{'VolRoll':EXPECTED_VOLROLL,'historical_H26':EXPECTED_H26 if args.historical_h26_db else None,'original_feature_fixture':EXPECTED_REFERENCE},
      'session_dates':len(coverage),'generated_valid_or5_dates':len(generated),'no_rth_dates':coverage.loc[coverage.status=='no RTH','trading_day'].tolist(),
      'unavailable_other_dates':coverage.loc[coverage.status=='features missing','trading_day'].tolist(),
      'volroll_f3_positive_count':int(generated.f3_0.sum()),'new_f3_positive_count':int(generated.loc[~generated.trading_day.isin(reference.trading_day),'f3_0'].sum()),
      'original_f3_positive_count':len(old_positive),'volroll_same_original_50_dates':volroll_positive==old_positive,
      'volroll_regression_mismatch_fields':len(volroll_mismatches),'volroll_regression_mismatch_dates':sorted(mismatch_days),
      'historical_regression_passed':passed,'historical_mismatch_fields':len(exact_mismatches),
      'preserved_reference_row_bytes':preserved_bytes_exact,'new_feature_dates':int((~coverage.original_fixture_present & coverage.status.ne('no RTH')).sum()),
      'manifest_status_counts':coverage.status.value_counts().to_dict(),
      'causal_scope':'Scoring adapter contains only fixed-clock OR5 data before 09:35 New York; roll_calendar is consumed without reselecting contracts; full-day RTH counts are diagnostic only',
      'limitations':['VolRoll alone does not reproduce 17 legacy H26 feature dates; original H26 input is required for exact historical regression.',
         'The additional 153 dates are new executions of the recovered original formula, not recovered historical cached labels.',
         'The no-RTH date has no synthetic feature row and has a null F3 label.',
         'Historical multiday sample inclusion used full-day completeness and was not a purely ex-ante sample selection.'],
      'schema':schemas}
    (args.output_dir/'generation_summary.json').write_text(json.dumps(summary,indent=2,default=str)+'\n')
    (args.output_dir/'generation.log').write_text('\n'.join(log)+'\n')
    outputs={p.name:{'bytes':p.stat().st_size,'sha256':digest(p)} for p in args.output_dir.iterdir() if p.is_file() and p.suffix in {'.csv','.json','.log'}}
    (args.output_dir/'output_hashes.json').write_text(json.dumps(outputs,indent=2)+'\n')
    # Strict acceptance never silently passes using preserved/cached reference rows.
    return 0 if passed and not summary['unavailable_other_dates'] else 2


if __name__=='__main__':
    raise SystemExit(main())
