#!/usr/bin/env python3
"""Independent, exact output checks; never imports the feature generator."""
import argparse
from pathlib import Path
import json
import pandas as pd
from pandas.testing import assert_frame_equal

ROOT=Path(__file__).resolve().parent
VOTES=['body','close_loc','vs_poc','vs_va','vs_vwap','hvn_shape','volume','delta_vote','marubozu',
       'prev_close','prev_break','prev_delta','prev_volume','prev_poc','prev_body','vbp_zone_weight']

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('output_dir',type=Path)
    p.add_argument('--reference',type=Path,default=ROOT/'fixtures/F3_OR5_features.csv')
    p.add_argument('--original-multiday',type=Path,default=ROOT.parent/'data/CANDLE_FORCE_v3_multiday_bars.parquet')
    args=p.parse_args();out=args.output_dir
    reference=pd.read_csv(args.reference).sort_values('trading_day').reset_index(drop=True)
    original=pd.read_parquet(args.original_multiday).query('m5_bar==1').sort_values('trading_day').reset_index(drop=True)
    historical=pd.read_csv(out/'F3_OR5_historical_regenerated_172.csv').sort_values('trading_day').reset_index(drop=True)
    regenerated=pd.read_csv(out/'F3_OR5_features_volroll_regenerated.csv')
    preserved=pd.read_csv(out/'F3_OR5_features_legacy_preserved.csv')
    coverage=pd.read_csv(out/'coverage_manifest_326.csv')
    missing=pd.read_csv(out/'previously_missing_dates_154.csv')
    components=pd.read_csv(out/'component_votes_historical_172.csv').sort_values('trading_day').reset_index(drop=True)
    assert len(reference)==172 and len(historical)==172
    assert len(regenerated)==len(preserved)==325 and len(coverage)==326 and len(missing)==154
    for frame in (reference,historical,regenerated,preserved,coverage,missing):
        assert frame.trading_day.is_unique
    assert_frame_equal(reference,historical,check_dtype=False,check_exact=True)
    compare_columns=VOTES+['score','score9','score15','w_long','w_short','w_body_fight','vol_lower_wick','vol_upper_wick','vol_near_close','vol_body_fight']
    assert_frame_equal(original[compare_columns],components[compare_columns],check_dtype=False,check_exact=True)
    assert (components[VOTES].sum(axis=1)==components.score_v2).all()
    assert (components[[c for c in VOTES if c.startswith('prev_')]]==0).all().all()
    old=set(reference.loc[reference.f3_0.eq(1),'trading_day'])
    hist=set(historical.loc[historical.f3_0.eq(1),'trading_day'])
    raw_old=set(regenerated.loc[regenerated.trading_day.isin(reference.trading_day)&regenerated.f3_0.eq(1),'trading_day'])
    assert len(old)==50 and old==hist==raw_old
    for frame in (regenerated,preserved):
        assert list(frame.columns)==list(reference.columns)
        assert set(frame.f3_0.unique())=={0,1} and set(frame.or5_red.unique())=={0,1}
        calculated=(frame.close<frame.open)&(frame.score_v2<=-5)&(frame.vbp_imbalance<=0)
        assert (calculated==frame.f3_0.astype(bool)).all()
        assert frame.f3_0.sum()==100
    new=regenerated[~regenerated.trading_day.isin(reference.trading_day)]
    assert len(new)==153 and new.f3_0.sum()==50 and new.or5_red.sum()==79
    assert (new.or5_red==0).sum()==74
    absent=coverage[coverage.status.eq('no RTH')]
    assert absent.trading_day.tolist()==['2026-04-03']
    assert absent[['score_v2','vbp_imbalance','f3_0']].isna().all().all()
    assert not coverage.status.eq('features missing').any()
    assert '2026-04-03' not in set(regenerated.trading_day)
    original_lines=args.reference.read_bytes().decode().splitlines()
    larger_lines=(out/'F3_OR5_features_legacy_preserved.csv').read_text().splitlines()
    lookup={line.split(',')[0]:line for line in larger_lines[1:]}
    extracted='\n'.join([original_lines[0]]+[lookup[d] for d in reference.trading_day])+'\n'
    assert extracted.encode()==args.reference.read_bytes()
    assert pd.read_csv(out/'regression_original_inputs_vs_original.csv').empty
    mismatch=pd.read_csv(out/'regression_volroll_vs_original.csv')
    assert len(mismatch)==81 and mismatch.trading_day.nunique()==17
    summary=json.loads((out/'generation_summary.json').read_text())
    assert summary['historical_regression_passed'] and summary['preserved_reference_row_bytes']
    result={'all_pass':True,'historical_rows':172,'exact_historical_f3_dates':50,
            'valid_or5_rows':325,'coverage_dates':326,'new_rows':153,'new_f3_dates':50,
            'full_f3_dates':100,'volroll_input_differences':{'fields':81,'dates':17},
            'no_rth_date':'2026-04-03','original_fixture_row_bytes_preserved':True}
    print(json.dumps(result,indent=2))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
