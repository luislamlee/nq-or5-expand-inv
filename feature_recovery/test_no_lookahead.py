#!/usr/bin/env python3
"""Independent original-scorer future-input invariance tests on disposable DBs.

The source DBs are attached/read read-only. No original data are changed.
"""
from datetime import date
from pathlib import Path
import hashlib
import json
import sys
import tempfile

import duckdb
import pandas as pd

import argparse
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--historical-h26-db',type=Path,required=True)
parser.add_argument('--or5-adapter-db',type=Path,required=True)
parser.add_argument('--output-dir',type=Path,required=True)
args=parser.parse_args()
ROOT=Path(__file__).resolve().parent
OUT=args.output_dir.resolve()
OUT.mkdir(parents=True,exist_ok=False)
SOURCES=ROOT/'portable'
sys.path.insert(0,str(SOURCES))
from score_candle_force_day_v2 import score_day_v2
H26=args.historical_h26_db.resolve()
PROBE=args.or5_adapter_db.resolve()
CASES=[
    ('2025-12-17',H26,'standard time; original H26 individual trades'),
    ('2026-02-23',H26,'standard time; original H26 one-second source bars'),
    ('2026-03-06',H26,'last session before March DST change; original H26'),
    ('2026-03-09',H26,'first session after March DST change; original H26'),
    ('2026-08-13',PROBE,'daylight time; VolRoll OR5-derived probe'),
]

def compare(reference,observed):
    mismatch=[]
    for col in reference.index:
        a,b=reference[col],observed[col]
        if pd.isna(a) and pd.isna(b):
            continue
        if a!=b:
            mismatch.append({'field':col,'expected':str(a),'observed':str(b)})
    return mismatch

results=[]
for day_s,source,label in CASES:
    day=date.fromisoformat(day_s)
    source_first=score_day_v2(day,source).iloc[0]
    with tempfile.TemporaryDirectory(prefix='cf-no-lookahead-',dir=OUT) as td:
        db=Path(td)/'isolated_or5.duckdb'
        c=duckdb.connect(str(db))
        c.execute("SET TimeZone='UTC'")
        c.execute("ATTACH '"+str(source).replace("'","''")+"' AS original (READ_ONLY)")
        c.execute('''CREATE TABLE bars_15s AS
          SELECT trading_day,rth_bar_number,bar_start_chicago,bar_end_chicago,
                 open,high,low,close,total_volume,volume_delta,bid_volume,ask_volume
          FROM original.bars_15s WHERE trading_day=? AND rth_bar_number BETWEEN 1 AND 20''',[day])
        c.execute('''CREATE TABLE footprint_15s_by_price AS
          SELECT trading_day,rth_bar_number,price,total_volume
          FROM original.footprint_15s_by_price WHERE trading_day=? AND rth_bar_number BETWEEN 1 AND 20''',[day])
        c.close()
        truncated=score_day_v2(day,db).iloc[0]
        c=duckdb.connect(str(db));c.execute("SET TimeZone='UTC'")
        # Add a complete future candle with prices, volume and delta far from OR5.
        c.execute('''INSERT INTO bars_15s
          SELECT trading_day,rth_bar_number+20,
                 bar_start_chicago+INTERVAL 5 MINUTE,bar_end_chicago+INTERVAL 5 MINUTE,
                 50000.0,60000.0,40000.0,55000.0,
                 1000000000,500000000,250000000,750000000
          FROM bars_15s WHERE rth_bar_number BETWEEN 1 AND 20''')
        c.execute('''INSERT INTO footprint_15s_by_price
          SELECT trading_day,rth_bar_number,55000.0,1000000000
          FROM bars_15s WHERE rth_bar_number BETWEEN 21 AND 40''')
        c.close()
        extreme_positive=score_day_v2(day,db).iloc[0]
        c=duckdb.connect(str(db));c.execute("SET TimeZone='UTC'")
        c.execute('''UPDATE bars_15s SET open=4000,high=5000,low=2000,close=2500,
                    total_volume=2000000000,volume_delta=-1000000000,
                    bid_volume=1500000000,ask_volume=500000000 WHERE rth_bar_number>20''')
        c.execute('''UPDATE footprint_15s_by_price SET price=2500,total_volume=2000000000
                    WHERE rth_bar_number>20''')
        c.close()
        extreme_negative=score_day_v2(day,db).iloc[0]
        comparisons={
            'source_vs_truncated':compare(source_first,truncated),
            'truncated_vs_added_extreme_positive_future':compare(truncated,extreme_positive),
            'truncated_vs_replaced_extreme_negative_future':compare(truncated,extreme_negative),
        }
        results.append({'trading_day':day_s,'case':label,'source_file':source.name,
                        'columns_checked':len(truncated),'first_start':str(truncated.t_start),
                        'first_end':str(truncated.t_end),'mismatch_count':sum(map(len,comparisons.values())),
                        'comparisons':comparisons})

summary={'test':'Original-scorer future-row/footprint perturbation invariance',
         'source_commit':'56c9056fa414ebf93e3285e240368d5c4bfdb53b',
         'scorer_sha256':hashlib.sha256((ROOT/'original/sierra/candle_force/score_candle_force_day_v2.py').read_bytes()).hexdigest(),
         'case_count':len(results),'all_pass':all(r['mismatch_count']==0 for r in results),'cases':results,
         'limits':['Tests current feature computation after input session/contract selection; does not independently establish historical roll-calendar causality',
                   'Does not make original full-day completeness selection causal at OR5',
                   'Original input DBs remained read-only; disposable derived DBs were removed after each test']}
(OUT/'no_lookahead_perturbation_test.json').write_text(json.dumps(summary,indent=2)+'\n')
print(json.dumps(summary,indent=2))
if not summary['all_pass']:raise SystemExit(1)
