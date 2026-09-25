"""Offline search component ablation on development plans, with a frozen cost model."""
import argparse,json,sys,time,random
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import torch
from autodop import mcts
from autodop.data import load_dataset,supported_dops
from autodop.graph import build_graph
from autodop.model import load_cost_model
from measure_ablation_cost import save,stats

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--plans',type=int,default=6);p.add_argument('--iterations',type=int,default=64);p.add_argument('--seeds',type=int,default=3);a=p.parse_args();assert not a.output.exists()
 torch.set_num_threads(1);r=ROOT/'runs/structural_scale_development01_20260911';proto=json.loads((r/'coordinate5000/PROTOCOL.json').read_text());model,checkpoint=load_cost_model(proto['model']);model.eval();samples,_,_=load_dataset(proto['dataset']);unique={s['plan_key']:s for s in samples};chosen=sorted(unique.values(),key=lambda s:(len(s['fragments']),s['plan_key']));chosen=[chosen[round(i*(len(chosen)-1)/max(1,a.plans-1))] for i in range(a.plans)];rows=[]
 def natural(sample,model,metadata,candidates,device='cpu'):return mcts.equivalence_groups(sample,candidates)
 def single(sample,candidates):return [(i,) for i in range(len(candidates))]
 for sample in chosen:
  for seed in range(a.seeds):
   modes=['full','no_equivalence','no_attention_order'];random.Random(seed).shuffle(modes)
   for mode in modes:
    start=time.perf_counter();kwargs=dict(iterations=a.iterations,seed=seed)
    if mode=='no_equivalence':
     with patch.object(mcts,'equivalence_groups',single):config,score=mcts.search(sample,model,checkpoint['metadata'],**kwargs)
    elif mode=='no_attention_order':
     with patch.object(mcts,'attention_guided_groups',natural):config,score=mcts.search(sample,model,checkpoint['metadata'],**kwargs)
    else:config,score=mcts.search(sample,model,checkpoint['metadata'],**kwargs)
    rows.append(dict(plan=sample['plan_key'],fragments=len(sample['fragments']),seed=seed,mode=mode,seconds=time.perf_counter()-start,score=score,dops=config));save(a.output,dict(status='running',rows=rows));print(mode,len(rows),flush=True)
  candidates=[supported_dops(f,checkpoint['metadata']['dop_classes']) for f in sample['fragments']];legal=set.intersection(*(set(v) for f,v in zip(sample['fragments'],candidates) if not f.get('is_gather_root')))
  start=time.perf_counter();scores=[]
  for d in sorted(legal):
   config=[1 if f.get('is_gather_root') else d for f in sample['fragments']]
   with torch.no_grad():score=float(model(build_graph({**sample,'dop_config':config},checkpoint['metadata'])))
   scores.append((score,config))
  score,config=max(scores);rows.append(dict(plan=sample['plan_key'],fragments=len(sample['fragments']),seed=None,mode='uniform_exhaustive',seconds=time.perf_counter()-start,score=score,dops=config))
 save(a.output,dict(status='complete',model=proto['model'],dataset=proto['dataset'],iterations=a.iterations,rows=rows,notes='Development-only search diagnostics. Score is frozen learned utility, not measured query latency; no model fitting. Uniform enumeration has a smaller search budget.'))
if __name__=='__main__':main()
