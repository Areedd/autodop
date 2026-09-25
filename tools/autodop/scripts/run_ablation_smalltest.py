"""Run a serial harness check or the fixed diagnostic panel after an idle gate."""
import argparse,json,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'runs/preference_random200_20260911'
RUN=ROOT/'runs/ablation_cost_20260912'
sys.path.insert(0,str(SOURCE))
from preflight import check

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--phase',choices=['pilot','panel'],required=True)
 p.add_argument('--attempt',type=int,default=0)
 p.add_argument('--idle-attempts',type=int,default=1)
 a=p.parse_args()
 if a.attempt<0 or a.idle_attempts<1:p.error('attempt must be nonnegative and idle-attempts positive')
 if a.phase=='panel':
  proofs=list(RUN.glob('pilot*/scored/independent_verification.json'))
  assert any(json.loads(f.read_text()).get('all_expected_executions_verified') for f in proofs),'First complete a verified pilot'
 config=json.loads((RUN/'full_v2/PROTOCOL.json').read_text())
 out=RUN/(a.phase+(str(a.attempt) if a.attempt else ''))
 out.mkdir(exist_ok=False)
 for retry in range(a.idle_attempts):
  try:check(out/f'PREFLIGHT_{retry}.json');break
  except RuntimeError:
   if retry==a.idle_attempts-1:raise
   print('Busy; waiting 15 seconds before next idle check',flush=True);time.sleep(15)
 command=[sys.executable,str(ROOT/'collect_paper_evaluations.py'),'--manifest',str(RUN/'full_v2'/('pilot.json' if a.phase=='pilot' else 'manifest.json')),'--trusted-collection',str(SOURCE/'random200/trusted_merged.json'),'--output-dir',str(out/'scored'),'--policy-model',config['model'],'--decision-vectors',str(RUN/'full_v2/vectors.json'),'--modes',','.join(config['modes']),'--scenario','serial','--query-timeout-s','120','--auto-dop-max-dop','16','--torch-threads','1','--warmup','1','--repetitions','1' if a.phase=='pilot' else '3','--seed',str(config['seed'])]
 (out/'COMMAND.json').write_text(json.dumps(command,indent=2)+'\n')
 subprocess.run(command,check=True)
 subprocess.run([sys.executable,str(ROOT/'scripts/verify_paper_evaluations.py'),'--collection-dir',str(out/'scored'),'--output',str(out/'scored/independent_verification.json')],check=True)
 verification=json.loads((out/'scored/independent_verification.json').read_text())
 assert verification['all_expected_executions_verified'],'Incomplete execution evidence; do not expand'
if __name__=='__main__':main()
