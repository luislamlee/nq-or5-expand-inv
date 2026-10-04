import importlib.util, json, pathlib, shutil, sys
import duckdb
import pandas as pd
import argparse
parser=argparse.ArgumentParser(description='Missing-data and time-boundary regression tests on disposable derived data')
parser.add_argument('--db',type=pathlib.Path,required=True)
parser.add_argument('--output-dir',type=pathlib.Path,required=True)
args=parser.parse_args()
ROOT=pathlib.Path(__file__).resolve().parent
OUT=args.output_dir.resolve()
OUT.mkdir(parents=True,exist_ok=False)
spec=importlib.util.spec_from_file_location('generator',ROOT/'generate_original_features.py');gen=importlib.util.module_from_spec(spec);spec.loader.exec_module(gen)
sys.path.insert(0,str(ROOT/'portable'))
from score_candle_force_day_v2 import score_day_v2,VOTE16_COLS
source=OUT/'single_session_input.duckdb'
if source.exists():source.unlink()
c=duckdb.connect(str(source));c.execute("ATTACH " + gen.literal(args.db.resolve()) + " AS original (READ_ONLY)")
day='2025-06-17'
c.execute(f"CREATE TABLE roll_calendar AS SELECT * FROM original.roll_calendar WHERE session_date=DATE '{day}'")
for t in ['bars_t_15s','bars_t_1m']:
 c.execute(f"CREATE TABLE {t} AS SELECT * FROM original.{t} WHERE session_date=DATE '{day}' AND ts_open BETWEEN TIMESTAMP '{day} 13:29:45' AND TIMESTAMP '{day} 13:35:15'")
c.execute(f"CREATE TABLE ticks AS SELECT * FROM original.ticks WHERE ts>=TIMESTAMP '{day} 13:29:45' AND ts<TIMESTAMP '{day} 13:35:30'")
c.close()
results=[]
def run(label,mutation=None):
 db=OUT/(label+'.duckdb');adapter=OUT/(label+'_adapter.duckdb')
 for p in [db,adapter]:
  if p.exists():p.unlink()
 shutil.copyfile(source,db)
 if mutation:
  c=duckdb.connect(str(db));c.execute(mutation);c.close()
 q,_=gen.prepare_volroll(db,adapter,duckdb)
 row=q.iloc[0]
 try: reason=gen.row_problem(row)
 except BaseException as e: reason='ERROR: '+type(e).__name__+': '+str(e)
 result={'test':label,'reason':reason,'invalid_bars':int(row.invalid_bars),'invalid_slots':int(row.invalid_slots),'or5_bars':int(row.or5_15s_bars),'bad_footprint_bars':int(row.bad_footprint_bars)}
 if not reason:
  try:
   score=gen.scored_first(day,adapter,score_day_v2,VOTE16_COLS)
   result['score']=json.loads(score.to_json(orient='records'))[0]
  except BaseException as e:result['score_error']=str(e)
 results.append(result)
 return result
base=run('base')
for col in ['open','high','low','close','delta','total_volume','bid_volume','ask_volume','ts_close']:
 run('null_'+col,f"UPDATE bars_t_15s SET {col}=NULL WHERE ts_open=TIMESTAMP '{day} 13:31:00'")
run('null_tick_contract',f"UPDATE ticks SET contract=NULL WHERE ts=(SELECT min(ts) FROM ticks WHERE ts>=TIMESTAMP '{day} 13:30:00')")
run('missing_all_footprint',f"DELETE FROM ticks WHERE ts>=TIMESTAMP '{day} 13:30:00' AND ts<TIMESTAMP '{day} 13:35:00'")
run('missing_footprint',f"DELETE FROM ticks WHERE ts>=TIMESTAMP '{day} 13:31:00' AND ts<TIMESTAMP '{day} 13:31:15'")
run('missing_slot',f"DELETE FROM bars_t_15s WHERE ts_open=TIMESTAMP '{day} 13:31:00'")
run('offgrid_tick',f"UPDATE ticks SET close=close+0.01 WHERE ts= (SELECT min(ts) FROM ticks WHERE ts>=TIMESTAMP '{day} 13:30:00')")
run('no_rth',"DELETE FROM bars_t_15s")
future=run('future_poison',f"UPDATE ticks SET close=999999,total_volume=123456789 WHERE ts>=TIMESTAMP '{day} 13:35:00'; UPDATE bars_t_15s SET open=999999,high=999999,low=999999,close=999999,total_volume=123456789,delta=123456789 WHERE ts_open>=TIMESTAMP '{day} 13:35:00'")
pre=run('preopen_poison',f"UPDATE ticks SET close=999999,total_volume=123456789 WHERE ts<TIMESTAMP '{day} 13:30:00'; UPDATE bars_t_15s SET open=999999,high=999999,low=999999,close=999999,total_volume=123456789,delta=123456789 WHERE ts_open<TIMESTAMP '{day} 13:30:00'")
summary={'tests':results,'future_poison_score_identical':base.get('score')==future.get('score'),'preopen_poison_score_identical':base.get('score')==pre.get('score')}
summary['all_cases_passed']=all((not r['reason']) if r['test'] in ['base','future_poison','preopen_poison'] else (bool(r['reason']) and not r['reason'].startswith('ERROR:')) for r in results) and summary['future_poison_score_identical'] and summary['preopen_poison_score_identical']
assert summary['all_cases_passed'], 'A boundary case failed'
(OUT/'boundary_test_evidence.json').write_text(json.dumps(summary,indent=2)+'\n')
print(json.dumps({**{r['test']:{k:v for k,v in r.items() if k not in ['score']} for r in results},'future_unchanged':summary['future_poison_score_identical'],'preopen_unchanged':summary['preopen_poison_score_identical']},indent=2))
