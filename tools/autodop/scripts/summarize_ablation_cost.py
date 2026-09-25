"""Summarize measured cost and ablation evidence without filling missing experiments."""
import csv,json,sys,hashlib,statistics,random
from collections import defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from measure_ablation_cost import stats,save
R=ROOT/'runs/ablation_cost_20260912';S=ROOT/'runs/preference_random200_20260911'
def main():
 offline=json.loads((R/'full/OFFLINE_SUMMARY.json').read_text());rows=json.loads((R/'full/OFFLINE_ROWS.json').read_text());unique={r['id']:r for r in rows};data=defaultdict(list)
 for x in csv.DictReader((S/'random200/MEASURED_OBSERVATIONS.csv').open()):data[x['id'],x['mode']].append(float(x['client_latency_ms']))
 reconstruction={}
 for name,predicate in [('all200',lambda x:True),('positive90',lambda x:x['positive']),('other110',lambda x:not x['positive'])]:
  qs=[q for q,v in unique.items() if predicate(v)];policy=[];uniform=[];query=[]
  for q in qs:
   mode='audit_dop'+str(unique[q]['uniform_dop']);a=data[q,'policy_static'];b=data[q,mode];assert len(a)==len(b)==3;policy+=a;uniform+=b;query.append(dict(id=q,dop=unique[q]['uniform_dop'],gain=1-statistics.mean(a)/statistics.mean(b)))
  reconstruction[name]=dict(queries=len(qs),policy_ms=stats(policy),policy_selected_uniform_ms=stats(uniform),gain_of_ratio_of_means=1-statistics.mean(policy)/statistics.mean(uniform),per_query=query)
 search=json.loads((R/'SEARCH_ABLATION.json').read_text());group=defaultdict(dict)
 for x in search['rows']:
  if x['seed'] is not None:group[x['plan'],x['seed']][x['mode']]=x
 comparisons={}
 for mode in ['no_equivalence','no_attention_order']:
  diffs=[v['full']['score']-v[mode]['score'] for v in group.values()];comparisons[mode]=dict(pairs=len(diffs),full_higher_score=sum(x>1e-8 for x in diffs),ties=sum(abs(x)<=1e-8 for x in diffs),full_lower_score=sum(x < -1e-8 for x in diffs),time_seconds=stats([v[mode]['seconds'] for v in group.values()]))
 search_summary=dict(full_time_seconds=stats([v['full']['seconds'] for v in group.values()]),comparisons=comparisons,uniform_time_seconds=stats([v['seconds'] for v in search['rows'] if v['mode']=='uniform_exhaustive']))
 panel={}
 for phase in sorted(d.name for d in R.iterdir() if d.is_dir() and d.name.startswith(('pilot','panel'))):
  path=R/phase/'scored/independent_verification.json'
  if not path.exists():continue
  verified=json.loads(path.read_text());panel[phase]=dict(verified=verified['all_expected_executions_verified'],expected=verified['expected_slots'])
  if not verified['all_expected_executions_verified']:continue
  obs=defaultdict(list);perquery=defaultdict(lambda:defaultdict(list))
  for line in (R/phase/'scored/events.jsonl').open():
   x=json.loads(line)
   if x.get('phase')=='measured' and x.get('status')=='verified':obs[x['mode']].append(x['client_latency_ms']);perquery[x['id']][x['mode']].append(x['client_latency_ms'])
  panel[phase]['latencies_ms']={m:stats(xs) for m,xs in obs.items()};panel[phase]['query_means']={q:{m:statistics.mean(xs) for m,xs in v.items()} for q,v in perquery.items()}
  paired=[]
  for method,baseline in [('hetero_replay','policy_uniform'),('hetero_replay','no_competition_replay'),('policy_load','policy_static'),('policy_static','uniform4')]:
   qs=list(panel[phase]['query_means'].values());pairs=[(x[method],x[baseline]) for x in qs];rng=random.Random(20260912);boot=[]
   for _ in range(2000):
    sampled=rng.choices(pairs,k=len(pairs));boot.append(1-sum(a for a,b in sampled)/sum(b for a,b in sampled))
   boot.sort();paired.append(dict(method=method,baseline=baseline,gain=1-sum(a for a,b in pairs)/sum(b for a,b in pairs),positive_queries=sum(a<b for a,b in pairs),queries=len(pairs),query_bootstrap_95pct=[boot[49],boot[1949]]))
  panel[phase]['paired_comparisons']=paired
  records=[json.loads(line) for line in (R/phase/'scored/decisions.jsonl').open()]
  load_records=[x for x in records if x['mode']=='policy_load']
  panel[phase]['load_correction_queries_including_warmup']=sum(any(':load_corrected' in d.get('reason','') for d in x.get('response',{}).get('decisions',[])) for x in load_records)
  panel[phase]['load_decision_count_including_warmup']=len(load_records)
 history=json.loads((R/'HISTORICAL_COST.json').read_text());training=json.loads((R/'TRAINING_COST.json').read_text())
 result=dict(status='complete' if any(k.startswith('panel') and v.get('verified') for k,v in panel.items()) else 'offline_complete_live_panel_pending',offline_cost=offline,historical_cost=history,training_cost=training,search_ablation=search_summary,competition_sensitivity=dict(queries=len(unique),changed_queries=sum(x['no_competition_changed']>0 for x in unique.values()),changed_fragment_decisions=sum(x['no_competition_changed'] for x in unique.values())),retrospective_uniform_comparison=reconstruction,live_sql=panel)
 load=json.loads((R/'LOAD_CORRECTION_COST.json').read_text());result['load_correction_cost']=load['summary']
 save(R/'RESULT.json',result)
 lines=['# AutoDOP 消融与成本实验补充','',f'状态：{result["status"]}。所有实验保持冻结模型；只进行串行 SQL 测试。','', '## 在线策略成本（200 条历史请求，每条预热后计时 3 次）','', '| 项目 | 均值 ms | p50 | p90 | p99 |','|---|---:|---:|---:|---:|']
 for k,v in offline['latencies'].items():lines.append(f'| {k} | {v["mean"]:.3f} | {v["p50"]:.3f} | {v["p90"]:.3f} | {v["p99"]:.3f} |')
 lines+=['',f'模型 {offline["parameters"]} 个参数，checkpoint {offline["checkpoint_bytes"]} 字节；本次热文件缓存下加载 {offline["startup_ms"]:.3f} ms，不能代表进程冷启动。分项分别计时，不能将其和当作同步端到端测量。','', '## 历史真实调用成本','']
 for k,v in history['metrics'].items():lines.append(f'- {k}: n={v["n"]}, mean={v["mean"]:.3f}, p50={v["p50"]:.3f}, p90={v["p90"]:.3f}, p99={v["p99"]:.3f}')
 lines+=['','FE 配对计时只使用成功应用的 417 次执行；回退记录的零计时排除。FE 减服务端约为 HTTP、序列化、调度等合计差值，不是纯网络延迟。尚未单独测量 FE 特征提取与 DOP 安装，不能据此断言完整优化器开销满足 5% 验收。','', '## 消融与敏感性','',f'冻结模型关闭竞争边特征后，{result["competition_sensitivity"]["changed_queries"]}/200 条查询改变 DOP，共改变 {result["competition_sensitivity"]["changed_fragment_decisions"]} 个 fragment 决策。仍保留竞争注意力通道，屏蔽的是边特征；这不是重训后的架构消融。','', '搜索实验使用原开发数据中按 fragment 数覆盖选出的 6 个计划，3 个随机种子，64 次 MCTS 迭代。完整搜索、取消等价分组、取消注意力排序采用同一成本模型。分数只代表模型预测的效用，不代表 SQL 实测延迟。','',f'搜索完整配置平均 {search_summary["full_time_seconds"]["mean"]:.3f} 秒；统一 DOP 穷举平均 {search_summary["uniform_time_seconds"]["mean"]:.3f} 秒，其候选数更少，不能按相同预算比较。']
 for mode,v in comparisons.items():lines.append(f'- 对比 {mode}：完整搜索分数更高/相同/更低 = {v["full_higher_score"]}/{v["ties"]}/{v["full_lower_score"]}，该配置平均 {v["time_seconds"]["mean"]:.3f} 秒。')
 lines+=['','## 使用已有计时的查询级约束对照','', '统一 DOP 根据冻结模型各非 gather fragment 的对数概率之和选定；gather 固定为 1，再从原五档实测记录取对应延迟。下面是回溯组合的诊断结果，并非新一轮同期实测，也不是逐查询最优统一 DOP。当前策略记录包含置信度回退。该较弱的策略统一约束基线不改变原全200相对最佳候选统一DOP平均退化约7.22%的结论。']
 for name,v in reconstruction.items():lines.append(f'- {name}: 策略平均 {v["policy_ms"]["mean"]:.2f} ms，策略选择的统一 DOP 平均 {v["policy_selected_uniform_ms"]["mean"]:.2f} ms，相对降低 {100*v["gain_of_ratio_of_means"]:.2f}%。')
 lines+=['','## 训练成本','',f'在原训练划分的 130 个计划上，以原损失函数运行 1 轮预热、3 轮正式计时；平均 {training["measured_epoch_seconds"]["mean"]:.3f} 秒/轮，数据/图/优化器准备 {training["setup_seconds"]:.3f} 秒，进程峰值 RSS {training["max_rss_KiB"]/1024:.1f} MiB。未保存或替换模型。这是训练微基准，不是原 1000 轮训练任务的实测总时长，也不包括数据采集和教师标签搜索。','', '## 新增 SQL 对照','', '固定 12 条诊断面板：从优势90和其余110中分别按固定随机种子选6条；不据此推断整个查询集平均收益。六种配置为完整策略、同策略统一 DOP、冻结异构向量、关闭竞争边后的冻结向量、负载修正策略、固定 DOP4。向量回放统一使用置信度1以隔离配置效果；与完整策略的差异同时包含回退与在线决策变化，不能单独称为推理开销。','','\n'.join(f'- {phase}: {v}' for phase,v in panel.items() if not phase.startswith('panel')),'', '首次两条流程检查暴露了自定义模式名称与采集器 uniform 数字前缀冲突，保留失败记录；修正后需小测全部核验通过才执行12条面板。忙碌时不放宽空闲阈值。']
 for phase,v in panel.items():
  if phase.startswith('panel') and v.get('verified'):
   lines += ['',f'### {phase}：全部执行已独立核验','', '| 配置 | 正式次数 | mean ms | p50 ms | p90 ms | p99 ms |','|---|---:|---:|---:|---:|---:|']
   for mode,z in v['latencies_ms'].items():lines.append(f'| {mode} | {z["n"]} | {z["mean"]:.2f} | {z["p50"]:.2f} | {z["p90"]:.2f} | {z["p99"]:.2f} |')
 for phase,v in panel.items():
  if phase.startswith('panel') and v.get('verified'):
   lines += ['',f'负载修正触发 {v["load_correction_queries_including_warmup"]}/{v["load_decision_count_including_warmup"]} 次（含预热）；若为0，则本次串行环境无法证明高负载修正的性能收益。']
   lines += ['', '配对对照：正数表示 method 更快。置信区间按查询重采样，只描述这个固定诊断面板，不代表独立泛化验证。']
   for z in v['paired_comparisons']:
    lo,hi=z['query_bootstrap_95pct'];lines.append(f'- {z["method"]} vs {z["baseline"]}: 平均延迟降低 {100*z["gain"]:.2f}%，获益 {z["positive_queries"]}/{z["queries"]}，查询 bootstrap 区间 [{100*lo:.2f}%, {100*hi:.2f}%]。')
 lines += ['', '## 负载修正计算成本', '', '以下使用合成资源负载，仅验证修正规则和耗时，不是并发性能实验。']
 for scenario,z in load['summary'].items():lines.append(f'- {scenario}: mean {z["mean"]:.4f} ms, p99 {z["p99"]:.4f} ms')
 (R/'REPORT.md').write_text('\n'.join(lines)+'\n')
if __name__=='__main__':main()
