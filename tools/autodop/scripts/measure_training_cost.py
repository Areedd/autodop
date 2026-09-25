"""Three measured epochs of the existing policy objective on its original training split."""
import json,math,random,resource,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import torch
from torch.nn import functional as F
from autodop.data import load_dataset
from autodop.graph import build_graph
from autodop.model import load_policy_model
from autodop.training import select_partition,file_sha256
from measure_ablation_cost import save,stats

def main():
 out=ROOT/'runs/ablation_cost_20260912/TRAINING_COST.json';assert not out.exists();torch.set_num_threads(1)
 p=json.loads((ROOT/'runs/structural_scale_development01_20260911/policy_preservation02/PROTOCOL.json').read_text());torch.manual_seed(p['seed']);rng=random.Random(p['seed']);start=time.perf_counter()
 incumbent,saved=load_policy_model(p['initial_policy']);incumbent.eval();model,_=load_policy_model(p['initial_policy']);model.train();samples,pairs,_=load_dataset(p['dataset']);contract=json.loads(Path(p['training_contract']).read_text());train,_=select_partition(samples,pairs,contract,'train');by={s['plan_key']:s for s in train};keys=sorted(by);assert len(keys)==130
 graphs={k:build_graph(by[k],saved['metadata'],unknown_dop=True) for k in keys};records=json.loads(Path(p['targets']).read_text())['plans'];assert {x['plan_key'] for x in records}==set(keys);classes={d:i for i,d in enumerate(saved['metadata']['dop_classes'])};targets={x['plan_key']:torch.tensor([classes[d] for d in x['target_dops']]) for x in records if x['margin_eligible']}
 with torch.no_grad():anchors={k:incumbent(g).softmax(-1) for k,g in graphs.items()}
 optimizer=torch.optim.Adam([dict(params=model.encoder.parameters(),lr=p['encoder_lr']),dict(params=model.classifier.parameters(),lr=p['classifier_lr'])]);setup=time.perf_counter()-start;rows=[]
 for epoch in range(4):
  start=time.perf_counter();order=keys.copy();rng.shuffle(order)
  for offset in range(0,len(order),p['batch_size']):
   batch=order[offset:offset+p['batch_size']];optimizer.zero_grad();outputs={k:model(graphs[k]) for k in batch}
   kl=torch.stack([F.kl_div(outputs[k].log_softmax(-1),anchors[k],reduction='batchmean') for k in batch]).mean()
   maxkl=torch.stack([F.kl_div(outputs[k].log_softmax(-1),anchors[k],reduction='none').sum(-1).max() for k in batch]).mean()
   margin=torch.stack([F.relu(math.log(p['margin_probability'])-outputs[k].log_softmax(-1).gather(1,targets[k][:,None]).squeeze(1)).max() if k in targets else torch.zeros(()) for k in batch]).mean()
   loss=p['kl_weight']*kl+p['max_kl_weight']*maxkl+p['margin_weight']*margin;assert torch.isfinite(loss);loss.backward();assert all(x.grad is None or torch.isfinite(x.grad).all() for x in model.parameters());optimizer.step()
  rows.append(dict(epoch=epoch,phase='warmup' if epoch==0 else 'measured',seconds=time.perf_counter()-start));print(rows[-1],flush=True)
 save(out,dict(status='complete',training_plans=len(keys),setup_seconds=setup,epochs=rows,measured_epoch_seconds=stats([x['seconds'] for x in rows[1:]]),max_rss_KiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,original_epochs=p['epochs'],notes='Cost microbenchmark using the original 130 training plans and original objective. One warmup, three measured epochs. No checkpoint saved or deployed; not the historical full training time.'))
if __name__=='__main__':main()
