"""Development-selected opposing-DOP branches, explicitly a mechanism stress."""
import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
from generate_mixed_operator_suite import Parameters, render_query, sql
from generate_pair_grain_stress import render_pair_grain
from generate_temporal_activity_suite import render_temporal_activity
from generate_business_report_bundle_suite import COLUMNS

HIGH=Parameters('scan_control',1,1178,3,3,'i_category',10)
LOW=Parameters('observed_cohort_retention',2,1180,3,3,'i_class',5)
PAIR_COLUMNS=['lag_days','matched_unit_pairs','paired_products','anchor_line_exposure',
              'follow_line_exposure','anchor_revenue_exposure','follow_revenue_exposure',
              'anchor_present_exposure','follow_present_exposure']


def parts(*, high=HIGH, low=LOW, grain="day", horizon=7, retention_strategy="dense_expansion"):
    children=[]
    for family,body,columns,parameters in [
        ('product_day_pairs',render_pair_grain(high,grain,horizon),PAIR_COLUMNS,high),
        ('observed_cohort_retention',render_temporal_activity(low,retention_strategy),COLUMNS['observed_cohort_retention'],low)]:
        fields=', '.join(f"'{c}', CAST(report_input.{c} AS VARCHAR)" for c in columns)
        normalized=sql(f"SELECT '{family}' AS report_kind,JSON_OBJECT({fields}) AS payload FROM ({body.strip().rstrip(';')}) report_input")
        children.append({'family':family,'sql':body,'normalized_sql':normalized,'columns':columns,'parameters':asdict(parameters)})
    return children


def generate_opposed(output,database, *, grain="day", horizon=7, retention_strategy="dense_expansion", composition="union_all", high=HIGH, low=LOW):
    if composition not in ("union_all", "cross_join"):
        raise ValueError("unknown branch composition")
    if high.family != "scan_control" or low.family != "observed_cohort_retention":
        raise ValueError("branch parameters require scan_control and observed_cohort_retention families")
    children=parts(high=high,low=low,grain=grain,horizon=horizon,retention_strategy=retention_strategy)
    output=Path(output);output.mkdir(parents=True,exist_ok=False);(output/'sql').mkdir();(output/'constituents').mkdir()
    queries=[]
    for name,chosen in [('stress_high_alone',children[:1]),('stress_low_alone',children[1:]),
                        ('stress_opposed_union' if composition=='union_all' else 'stress_opposed_cross_join',children),('scan_control',[])]:
        qid=name+'__train_00';body=sql('\nUNION ALL\n'.join(p['normalized_sql'].strip().rstrip(';') for p in chosen)) if chosen else render_query(high)
        if len(chosen)==2 and composition=='cross_join':
            left,right=[p['normalized_sql'].strip().rstrip(';') for p in chosen]
            body=sql(f"SELECT 'paired_branch_results' AS report_kind, JSON_OBJECT('high_payload',CAST(h.payload AS VARCHAR),'low_payload',CAST(l.payload AS VARCHAR)) AS payload FROM ({left}) h CROSS JOIN ({right}) l")
        dest=output/'sql'/(qid+'.sql');dest.write_text(body);entries=[]
        for child in chosen:
            entry={k:child[k] for k in ['family','columns','parameters']}
            for field in ('sql','normalized_sql'):
                p=output/'constituents'/f"{qid}__{child['family']}__{field}.sql";p.write_text(child[field])
                entry[field+'_file']=str(p.relative_to(output));entry[field+'_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
            entries.append(entry)
        queries.append({'id':qid,'query_id':qid,'split':'train','database':database,'template_family':name,
                        'business_task':'Deliberate opposing-DOP stress; not a representative report',
                        'parameters':asdict(low if name=='stress_low_alone' else high),
                        'input_parameters':[c['parameters'] for c in chosen] or [asdict(high)],
                        'sql_file':str(dest.relative_to(output)),'sql_sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),
                        'control':not chosen,'constituents':entries})
    manifest={'schema_version':1,'generator':Path(__file__).name,'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'component_source_sha256':{name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ['generate_pair_grain_stress.py','generate_temporal_activity_suite.py','generate_business_report_bundle_suite.py']},
              'query_count':4,'queries':queries,
              'structural_parameters':{'pair_grain':grain,'horizon_days':horizon,'retention_strategy':retention_strategy,'composition':composition},
              'contract':{
                  'primary_mode':'policy_static','primary_baselines':['native','uniform4'],'target_latency_reduction':.2,
                  'query_dop_domain':[1,2,4,8,16],'learned_max_dop':16,'concurrency':1,
                  'construction':'Explicitly informed by previous development DOP curves. Reuse product-pair and retention mechanisms with independently specified date/channel contexts. Historical default DOP preferences do not establish preferences for new parameters; different windows/channels intentional, not one coherent business population.',
                  'scope':'Two existing branch mechanisms, their deliberately synthetic union, and unchanged scancontrol. Not four independent families, unseen-template evidence or production-frequency sample.',
                  'output':('Every branch column retained as JSON string/null; no dummy repeated branch, padding, or artificial data. Combined output equals multiset union of standalone normalized outputs.' if composition=='union_all' else 'Cartesian combination of complete branch report outputs, both payloads retained; row count equals product of branch cardinalities, empty branch gives empty result. Deliberate structural JOIN stress, no application relationship asserted.'),
                  'comparators':'SameSQL/fulluniformdomain; separately executed calibrated standalone branches mandatory before any combined-report benefit claim. Symmetric execute/fetch boundaries; include all child statements and fetches.',
                  'interpretation':'Previous standalone DOP preferences are hypotheses until remeasured; optimizer changes, branch overlap and shared resources may change them in the union. Explain/actual-vector/profile evidence required.',
                  'stage':'Four fixed diagnostics, fixtures and source/copy/constituent equality before prepass. Retain all outcomes; no training or representative expansion.'}}
    (output/'train.json').write_text(json.dumps(manifest,indent=2)+'\n');return manifest


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--database',required=True)
    p.add_argument('--pair-grain',choices=('day','receipt'),default='day')
    p.add_argument('--horizon-days',type=int,choices=(1,7),default=7)
    p.add_argument('--retention-strategy',choices=('reference','dense_expansion'),default='dense_expansion')
    p.add_argument('--composition',choices=('union_all','cross_join'),default='union_all')
    p.add_argument('--high-start-month',type=int,default=HIGH.start_month)
    p.add_argument('--low-start-month',type=int,default=LOW.start_month)
    p.add_argument('--high-channel',type=int,choices=(0,1,2),default=HIGH.channel)
    p.add_argument('--low-channel',type=int,choices=(0,1,2),default=LOW.channel)
    a=p.parse_args();generate_opposed(a.output_dir,a.database,grain=a.pair_grain,horizon=a.horizon_days,retention_strategy=a.retention_strategy,composition=a.composition,high=replace(HIGH,start_month=a.high_start_month,channel=a.high_channel),low=replace(LOW,start_month=a.low_start_month,channel=a.low_channel))
