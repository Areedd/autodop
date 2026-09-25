"""Frozen-policy sensitivity and overhead measurements; never fits evaluation queries."""
import argparse,copy,hashlib,json,random,statistics,sys,time
from collections import defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import torch
from autodop.engine import PolicyEngine
from autodop.graph import build_graph,sample_from_request

SOURCE=ROOT/'runs/preference_random200_20260911'
def save(p,v):p.write_text(json.dumps(v,indent=2,default=str)+'\n')
def stats(xs):
 xs=sorted(xs)
 def q(p):
  i=(len(xs)-1)*p;a=int(i);return xs[a]+(xs[min(a+1,len(xs)-1)]-xs[a])*(i-a)
 return dict(n=len(xs),mean=statistics.mean(xs),p50=q(.5),p90=q(.9),p99=q(.99)) if xs else dict(n=0)
def timed(fn):
 start=time.perf_counter_ns();v=fn();return v,(time.perf_counter_ns()-start)/1e6

def prepare(out,limit):
 out.mkdir(parents=True,exist_ok=False);torch.set_num_threads(1)
 p=json.loads((SOURCE/'PROTOCOL.json').read_text());manifest=json.loads((SOURCE/'random200/manifest.json').read_text());positive={q['id'] for q in json.loads((SOURCE/'random200/positive90/manifest.json').read_text())['queries']}
 model=Path(p['model']);assert hashlib.sha256(model.read_bytes()).hexdigest()==p['model_sha256']
 engine,startup=timed(lambda:PolicyEngine(model));masked=PolicyEngine(model);masked.model.encoder.config['competition_edges']=False
 vectors=json.loads((SOURCE/'random200/vectors.json').read_text())['audit_dop4'];selected=manifest['queries'][:limit] if limit else manifest['queries']
 rows=[];interventions={k:{} for k in ('policy_uniform','hetero_replay','no_competition_replay')}
 for query in selected:
  qid=query['id'];request=vectors[qid]['request'];sample=sample_from_request(request);graph=build_graph(sample,engine.metadata,unknown_dop=True)
  response=engine.decide(request);noedge=masked.decide(request)
  with torch.no_grad():logp=torch.log_softmax(engine.model(graph),dim=-1)
  indices=[i for i,f in enumerate(request['fragments']) if not f.get('is_gather_root')]
  joint=logp[indices].sum(dim=0);chosen=int(engine.metadata['dop_classes'][int(joint.argmax())])
  for mode,decisions in [('hetero_replay',response['decisions']),('no_competition_replay',noedge['decisions'])]:
   interventions[mode][qid]=dict(request=request,fragment_dops={d['fragment_id']:d['dop'] for d in decisions})
  interventions['policy_uniform'][qid]=dict(request=request,fragment_dops={str(f['fragment_id']):1 if f.get('is_gather_root') else chosen for f in request['fragments']})
  engine.decide(request) # per-query warmup
  for repeat in range(3):
   _,parse=timed(lambda:sample_from_request(request));_,build=timed(lambda:build_graph(sample,engine.metadata,unknown_dop=True))
   with torch.no_grad():_,forward=timed(lambda:torch.softmax(engine.model(graph),dim=-1).max(dim=-1))
   _,end=timed(lambda:engine.decide(request));_,masked_ms=timed(lambda:masked.decide(request))
   rows.append(dict(id=qid,positive=qid in positive,fragments=len(indices)+sum(f.get('is_gather_root',False) for f in request['fragments']),round=repeat,request_conversion_ms=parse,graph_build_ms=build,forward_softmax_ms=forward,policy_total_ms=end,no_competition_total_ms=masked_ms,uniform_dop=chosen,confidence=response['confidence'],no_competition_changed=sum(a['dop']!=b['dop'] for a,b in zip(response['decisions'],noedge['decisions']))))
  print(qid,flush=True)
 save(out/'OFFLINE_ROWS.json',rows);save(out/'vectors.json',interventions)
 rng=random.Random(20260912);panel=[]
 for positive_flag in [True,False]:
  candidates=[q for q in selected if (q['id'] in positive)==positive_flag]
  rng.shuffle(candidates);panel+=candidates[:6]
 panel.sort(key=lambda q:q['id'])
 for q in panel:q['sql_file']=str(Path(q['sql_file']).resolve())
 save(out/'manifest.json',dict(queries=panel,scope='seeded diagnostic panel; six positive and six other queries when available; not representative aggregate'))
 save(out/'pilot.json',dict(queries=panel[:2],scope='two-query harness check only'))
 fields=[k for k in rows[0] if k.endswith('_ms')]
 save(out/'OFFLINE_SUMMARY.json',dict(query_count=len(selected),startup_ms=startup,checkpoint_bytes=model.stat().st_size,parameters=sum(x.numel() for x in engine.model.parameters()),latencies={k:stats([r[k] for r in rows]) for k in fields}))
 save(out/'PROTOCOL.json',dict(model=str(model),model_sha256=p['model_sha256'],seed=20260912,modes=['policy_static','policy_uniform','hetero_replay','no_competition_replay','policy_load','uniform4'],scope='fixed checkpoint sensitivity; not retrained architecture ablation',uniform_rule='argmax sum non-gather log probabilities over common legal DOP; gather remains 1',replay_rule='frozen requests map to legal vectors; confidence=1 to isolate configuration from fallback, compare replay to replay',repetitions=3,warmup=1,concurrency=1,training=False))

def history(out):
 byid={};fields=defaultdict(list);paired=defaultdict(list)
 for line in (SOURCE/'random200/scored/decisions.jsonl').open():
  r=json.loads(line)
  if r['mode']=='policy_static':byid[r['query_id']]=r['decision_ms']
 for line in (SOURCE/'random200/scored/events.jsonl').open():
  r=json.loads(line)
  if r['mode']!='policy_static' or r['phase']!='measured':continue
  tr=r['trace'];auto=tr.get('auto_dop',{});duration=auto.get('decision_latency_ms');service=byid.get(r['query_id'])
  applied=r.get('dop_validation',{}).get('applied',False)
  if service is not None:fields['service_all_ms'].append(service)
  if not applied:
   if service is not None:fields['service_fallback_ms'].append(service)
   continue
  if duration is not None:fields['fe_decision_call_ms'].append(duration)
  if service is not None:fields['service_decision_ms'].append(service)
  if duration is not None and service is not None:fields['fe_minus_service_ms'].append(duration-service)
  if duration is not None:fields['fe_decision_fraction_pct'].append(100*duration/r['client_latency_ms'])
 save(out/'HISTORICAL_COST.json',dict(metrics={k:stats(v) for k,v in fields.items()},notes='FE timer spans HTTP decision call, excludes feature extraction and installation; difference is not pure network time; FE/service paired metrics include applied executions only; service_all includes fallback. Fallback FE zero timers excluded. Millisecond timer quantization applies.'))

def load_cost(out):
 torch.set_num_threads(1)
 p=json.loads((SOURCE/'PROTOCOL.json').read_text());engine=PolicyEngine(p['model'],{'cpu_threshold':.4,'mem_threshold':.65,'floor':.1})
 vectors=json.loads((out/'full_v2/vectors.json').read_text())['hetero_replay'];rows=[]
 for qid,v in vectors.items():
  request=copy.deepcopy(v['request']);decisions=[dict(fragment_id=fid,dop=dop,reason='policy') for fid,dop in v['fragment_dops'].items()]
  workers={str(w) for f in request['fragments'] for w in f.get('worker_ids',[])}
  for label,cpu,mem in [('idle',.2,.3),('cpu_busy',.8,.3),('memory_busy',.2,.9)]:
   request['worker_loads']={w:dict(cpu_used=cpu,mem_used_pct=mem,fresh=True) for w in workers}
   engine._apply_load_correction(request,decisions,1.)
   for repeat in range(3):
    response,ms=timed(lambda:engine._apply_load_correction(request,decisions,1.))
    rows.append(dict(id=qid,scenario=label,round=repeat,correction_only_ms=ms,changed=sum(a['dop']!=b['dop'] for a,b in zip(decisions,response['decisions']))))
 save(out/'LOAD_CORRECTION_COST.json',dict(rows=rows,summary={label:stats([r['correction_only_ms'] for r in rows if r['scenario']==label]) for label in ['idle','cpu_busy','memory_busy']},notes='Synthetic resource inputs; only correction overhead and behavior. No concurrent SQL or throughput claims.'))

if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--output-dir',type=Path,required=True);a.add_argument('--limit',type=int,default=0);a.add_argument('--history',action='store_true');a.add_argument('--load',action='store_true');args=a.parse_args()
 if args.load:load_cost(args.output_dir)
 elif args.history:history(args.output_dir)
 else:prepare(args.output_dir,args.limit)
