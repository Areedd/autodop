"""Shared product/day statistics feeding temporal pairs and observed-sales changes."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from generate_mixed_operator_suite import CHANNELS, Parameters, sql
from generate_integrity_operator_suite import date_key
from generate_pair_grain_stress import render_unit_pairs
from generate_month_keyed_branch_suite import PAIR_METRICS

WINDOW_METRICS=['item_days','repeat_within_7_days','comparable_changes','net_revenue_change','revenue']


def render(p, shared=False):
    if p.family!='scan_control' or p.months!=3 or p.history_months!=3:
        raise ValueError('expected three-month product scan context')
    c=CHANNELS[p.channel];lo,hi=date_key(p.start_month),date_key(p.start_month+3)
    producer=f'''product_days AS (SELECT s.{c.item} AS entity_id,s.{c.sold_date} AS day_id,
COUNT(*) AS sales_lines,SUM(s.{c.net_paid}) AS revenue,COUNT(s.{c.net_paid}) AS revenue_present
FROM {c.table} s WHERE s.{c.sold_date}>={lo} AND s.{c.sold_date}<{hi}
AND s.{c.item} IS NOT NULL AND s.{c.order} IS NOT NULL GROUP BY s.{c.item},s.{c.sold_date})'''
    prefix='' if shared else 'WITH '+producer+'\n'
    a=render_unit_pairs(prefix+'SELECT * FROM product_days',hi,7,report_grain='anchor_month')
    b=sql('WITH '+('' if shared else producer+', ')+'''previous_sales AS (
SELECT *,LAG(day_id) OVER(PARTITION BY entity_id ORDER BY day_id) AS previous_day,
LAG(revenue) OVER(PARTITION BY entity_id ORDER BY day_id) AS previous_revenue FROM product_days)
SELECT d.d_month_seq AS report_month,COUNT(*) AS item_days,
SUM(CASE WHEN a.day_id-a.previous_day<=7 THEN 1 ELSE 0 END) AS repeat_within_7_days,
COUNT(a.revenue-a.previous_revenue) AS comparable_changes,
SUM(a.revenue-a.previous_revenue) AS net_revenue_change,SUM(a.revenue) AS revenue
FROM previous_sales a JOIN date_dim d ON a.day_id=d.d_date_sk GROUP BY d.d_month_seq''')
    fields=['a.report_month',*[f'a.{x} AS pair_{x}' for x in PAIR_METRICS],*[f'b.{x} AS window_{x}' for x in WINDOW_METRICS]]
    joined=sql('SELECT '+','.join(fields)+f" FROM ({a.strip().rstrip(';')}) a INNER JOIN ({b.strip().rstrip(';')}) b ON a.report_month=b.report_month")
    if shared:joined=sql('WITH '+producer+'\n'+joined.strip().rstrip(';'))
    return a,b,joined


def generate(output,database,p=Parameters('scan_control',1,1178,3,3,'i_category',10)):
    a,b,independent=render(p);_,_,shared=render(p,True)
    output=Path(output);output.mkdir(parents=True,exist_ok=False);(output/'sql').mkdir();queries=[]
    for name,body,control in [('product_window_pairs',a,True),('product_window_changes',b,True),('product_window_independent',independent,False),('product_window_shared',shared,False)]:
        qid=name+'__train_00';f=output/'sql'/(qid+'.sql');f.write_text(body)
        queries.append(dict(id=qid,query_id=qid,split='train',database=database,template_family=name,parameters=asdict(p),input_parameters=[asdict(p)],control=control,sql_file=str(f.relative_to(output)),sql_sha256=hashlib.sha256(f.read_bytes()).hexdigest()))
    sources=[Path(__file__),*[Path(__file__).with_name(n) for n in ['generate_mixed_operator_suite.py','generate_integrity_operator_suite.py','generate_pair_grain_stress.py','generate_month_keyed_branch_suite.py']]]
    m=dict(schema_version=1,query_count=4,queries=queries,component_source_sha256={str(f.resolve()):hashlib.sha256(f.read_bytes()).hexdigest() for f in sources},contract=dict(training_eligible=False,concurrency=1,scope='Four diagnostic SQL; pair/window standalone controls and equivalent independent/shared month INNER. Sharing and opposite DOP preference require evidence.',semantics='Known product and receipt, anonymous customers included. Pair complete-seven-day anchors. Window change compares consecutive observed product sale days, not adjacent calendar days; first in-window observation and NULL revenue have undefined change. Seven-day repeat is separate count. No missing-day zero filling, no inventory or customer-retention interpretation.',stage='Fixture/live complete equality then physical sharing gate before four-query serial timing. No automatic expansion or training.'))
    (output/'train.json').write_text(json.dumps(m,indent=2)+'\n');return m
