"""Four deliberately stressful matched-input product-pair diagnostics."""
import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

from generate_integrity_operator_suite import date_key
from generate_mixed_operator_suite import CHANNELS, Parameters, render_query, sql


def render_pair_grain(p, grain, horizon, *, report_grain='lag'):
    render_query(replace(p, family='scan_control'))
    if grain not in ('day', 'receipt') or type(horizon) is not int or horizon not in (1, 7):
        raise ValueError('stress design is day/receipt crossed with1/7days')
    if p.months != p.history_months or p.months != 3:
        raise ValueError('stress design freezes a three-month window')
    c=CHANNELS[p.channel];lo=date_key(p.start_month);hi=date_key(p.start_month+3)
    receipt_select=f',s.{c.order} AS receipt_id' if grain=='receipt' else ''
    receipt_group=f',s.{c.order}' if grain=='receipt' else ''
    units = f"""SELECT s.{c.item} AS entity_id,s.{c.sold_date} AS day_id{receipt_select},
       COUNT(*) AS sales_lines,SUM(s.{c.net_paid}) AS revenue,
       COUNT(s.{c.net_paid}) AS revenue_present
FROM {c.table} s WHERE s.{c.sold_date}>={lo} AND s.{c.sold_date}<{hi}
AND s.{c.item} IS NOT NULL AND s.{c.order} IS NOT NULL
GROUP BY s.{c.item},s.{c.sold_date}{receipt_group}"""
    return render_unit_pairs(units, hi, horizon, report_grain=report_grain)


def render_unit_pairs(units, hi, horizon=7, *, report_grain='lag'):
    """Pair preaggregated entity/day units with complete seven-day anchors."""
    if type(horizon) is not int or horizon not in (1, 7):
        raise ValueError('pair horizon must be 1 or 7 days')
    if report_grain not in ('lag', 'anchor_month'):
        raise ValueError('unknown pair reporting grain')
    key = 'b.day_id-a.day_id' if report_grain == 'lag' else 'anchor_date.d_month_seq'
    alias = 'lag_days' if report_grain == 'lag' else 'report_month'
    calendar = '' if report_grain == 'lag' else '\nJOIN date_dim anchor_date ON a.day_id=anchor_date.d_date_sk'
    return sql(f"""WITH units AS (
{units})
SELECT {key} AS {alias},COUNT(*) AS matched_unit_pairs,
       COUNT(DISTINCT a.entity_id) AS paired_products,
       SUM(a.sales_lines) AS anchor_line_exposure,SUM(b.sales_lines) AS follow_line_exposure,
       SUM(a.revenue) AS anchor_revenue_exposure,SUM(b.revenue) AS follow_revenue_exposure,
       SUM(a.revenue_present) AS anchor_present_exposure,
       SUM(b.revenue_present) AS follow_present_exposure
FROM units a JOIN units b ON a.entity_id=b.entity_id
AND b.day_id>a.day_id AND b.day_id<=a.day_id+{horizon}{calendar}
WHERE a.day_id<{hi}-7
GROUP BY {key}""")


def generate_stress(output, database):
    output=Path(output);output.mkdir(parents=True,exist_ok=False);(output/'sql').mkdir()
    p=Parameters('scan_control',1,1178,3,3,'i_category',10)
    queries=[]
    for grain in ('day','receipt'):
        for horizon in (1,7):
            qid=f'pair_stress_{grain}_{horizon}d__catalog__train_00'
            body=render_pair_grain(p,grain,horizon);path=output/'sql'/f'{qid}.sql';path.write_text(body)
            queries.append({'id':qid,'query_id':qid,'split':'train','database':database,
                'template_family':f'pair_stress_{grain}_{horizon}d',
                'business_task':'Synthetic mechanism stress: product follow-up unit pairs',
                'parameters':{**asdict(p),'family':f'pair_stress_{grain}_{horizon}d','grain':grain,'horizon_days':horizon},
                'sql_file':str(path.relative_to(output)),'sql_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'control':grain=='day' and horizon==1})
    manifest={'schema_version':1,'generator':Path(__file__).name,
        'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'query_count':4,'queries':queries,
        'contract':{'primary_mode':'policy_static','primary_baselines':['native','uniform4'],
            'query_dop_domain':[1,2,4,8,16],'learned_max_dop':16,'target_latency_reduction':.2,
            'concurrency':1,'latency_based_selection':False,'controls_fraction':.25,
            'scope':'User-requested extreme mechanism tests, not a representative workload. One mechanism in a fixed2x2design; four names do not imply four semantic families.',
            'matched_inputs':'Same channel/date window and non-null product/receipt population. Both horizons use the same seven-day-complete anchor cutoff. No row padding or data mutation.',
            'grain':'Day grain collapses all receipts for product/day. Receipt grain retains product/order/day; receipt keys need not be globally unique. Source duplicate lines affect exposure but not unit-pair multiplicity.',
            'output':'Different grains answer different unit-pair questions; compare each SQL with its own identical-SQL DOP baselines. No cross-grain result-equivalence or causal business claim. Exposure repeats across matches, not additive ledger totals. Preserve NULL/negative values.',
            'stage':'Fourfixturevalidatedqueries then source/copy and24executionprepass; retain failures/timeouts, no automatic expansion or fitting.'}}
    (output/'train.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--database',required=True)
    a=p.parse_args();generate_stress(a.output_dir,a.database)
