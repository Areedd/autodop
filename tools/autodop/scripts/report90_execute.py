"""Execute frozen report90 stages with an idle gate and independent verification."""
import argparse,json,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];RUN=ROOT/'runs/report90_20260912';SOURCE=ROOT/'runs/preference_random200_20260911';sys.path.insert(0,str(SOURCE));from preflight import check

def accept_concurrent_evidence(proof):
 v=json.loads(proof.read_text())
 if v['all_expected_executions_verified']:return
 assert v['evidence_valid'] and not v['missing_slots'],'Missing or invalid evidence is not an overload result'
 failed=[]
 for line in (proof.parent/'events.jsonl').open():
  e=json.loads(line)
  if e.get('status')!='verified':
   assert e.get('status')=='failed' and 'Memory of process exceed limit' in e.get('error',''),'Unexpected failure requires investigation: '+str(e.get('error'))
   failed.append({k:e.get(k) for k in ('id','query_id','mode','phase','round','error')})
 assert len(failed)==v['failed_slots'] and failed
 (proof.parent/'OVERLOAD_OUTCOME.json').write_text(json.dumps({'status':'complete_with_observed_memory_failures','failures':failed,'note':'Every slot attempted; genuine BE process memory-limit failures retained, not retried or counted as successful queries.'},indent=2)+'\n')

def run(stage,attempt=0,clients=None):
 if stage=='serial':run('baseline_pilot')
 if stage=='concurrent' and clients=='2':
  proof=RUN/'concurrent_pilot_attempt1/scored/independent_verification.json'
  if not proof.exists():run('concurrent_pilot',attempt=1,clients='8,16')
  accept_concurrent_evidence(proof)
 protocol=json.loads((RUN/'PROTOCOL.json').read_text());out=RUN/(stage+(f'_attempt{attempt}' if attempt else ''));out.mkdir(exist_ok=False)
 for retry in range(10):
  try:check(out/f'IDLE_{retry}.json');break
  except RuntimeError:
   if retry==9:raise
   print('Busy; retry in 15 seconds',flush=True);time.sleep(15)
 pilot=stage.endswith('pilot');concurrent=stage.startswith('concurrent');manifest=RUN/('pilot.json' if pilot else 'manifest.json');modes=['native','policy_static','policy_load'] if concurrent else ['native','uniform1','uniform2','uniform4','uniform8','uniform16','policy_static','policy_before']
 if stage=='baseline_pilot':modes=['uniform1','uniform2','uniform8','uniform16']
 extra=[]
 if not concurrent and stage!='baseline_pilot':
  vectors=Path(json.loads((RUN/('CHOICES_PILOT_READY.json' if pilot else 'CHOICES_READY.json')).read_text())['directory'])/'vectors.json';modes+=[m for m in json.loads(vectors.read_text()) if not m.startswith('audit_dop')];extra=['--decision-vectors',str(vectors),'--policy-variants',str(RUN/'policy_variants.json')]
 if not pilot and not concurrent:
  assert any(json.loads(p.read_text()).get('all_expected_executions_verified') for p in RUN.glob('serial_pilot*/scored/independent_verification.json')),'Complete serial pilot first'
 if not pilot and concurrent:
  assert any(json.loads(p.read_text()).get('all_expected_executions_verified') for p in RUN.glob('concurrent_pilot*/scored/independent_verification.json')),'Complete concurrent pilot first'
 command=[sys.executable,str(ROOT/'collect_paper_evaluations.py'),'--manifest',str(manifest),'--trusted-collection',str(SOURCE/'random200/trusted_merged.json'),'--output-dir',str(out/'scored'),'--policy-model',protocol['model'],'--modes',','.join(modes),'--scenario','concurrent' if concurrent else 'serial','--query-timeout-s','180','--auto-dop-max-dop','16','--torch-threads','1','--decision-timeout-ms','1000','--warmup','1','--repetitions','1' if pilot else ('2' if concurrent else '3'),'--seed',str(protocol['seed']),*extra]
 if concurrent:command+=['--clients',clients or ('1,2,4' if pilot else '1,2,4,8,16'),'--minimum-per-client','2' if pilot else '1']
 (out/'COMMAND.json').write_text(json.dumps(command,indent=2)+'\n');subprocess.run(command,check=True)
 subprocess.run([sys.executable,str(ROOT/'scripts/verify_paper_evaluations.py'),'--collection-dir',str(out/'scored'),'--output',str(out/'scored/independent_verification.json')],check=True)
 if concurrent:accept_concurrent_evidence(out/'scored/independent_verification.json')
 else:
  v=json.loads((out/'scored/independent_verification.json').read_text());assert v['all_expected_executions_verified'],'Evidence includes failures; inspect before expanding'
 return out
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--stage',choices=['serial_pilot','serial','concurrent_pilot','concurrent'],required=True);p.add_argument('--attempt',type=int,default=0);p.add_argument('--clients');a=p.parse_args();run(a.stage,a.attempt,a.clients)
