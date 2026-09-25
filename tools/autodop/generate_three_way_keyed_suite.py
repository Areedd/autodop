"""Unequal-depth three-way keyed composition of pair/cohort/transition branches."""
from dataclasses import asdict,replace
import hashlib,json
from pathlib import Path
from generate_month_keyed_branch_suite import branches,PAIR_METRICS,COHORT_METRICS
from generate_temporal_activity_suite import activity_components
from generate_opposed_branch_stress import HIGH,LOW
from generate_mixed_operator_suite import sql

TRANSITION_METRICS=['customer_days','first_days','within_7_days','changed_state_days','revenue']


def parts():
    low=replace(LOW,start_month=HIGH.start_month)
    children=branches(HIGH,low)
    c=sql('WITH '+activity_components(low)+''', previous_activity AS (
SELECT *,LAG(day_id) OVER(PARTITION BY customer_id ORDER BY day_id) AS previous_day,
LAG(channel_state) OVER(PARTITION BY customer_id ORDER BY day_id) AS previous_state
FROM calendar_days)
SELECT month_seq AS report_month,COUNT(*) AS customer_days,
SUM(CASE WHEN previous_day IS NULL THEN 1 ELSE 0 END) AS first_days,
SUM(CASE WHEN day_id-previous_day<=7 THEN 1 ELSE 0 END) AS within_7_days,
SUM(CASE WHEN previous_state<>channel_state THEN 1 ELSE 0 END) AS changed_state_days,
SUM(revenue) AS revenue FROM previous_activity GROUP BY month_seq''')
    children.append(dict(sql=c,parameters=asdict(low),columns=['report_month',*TRANSITION_METRICS]))
    return children


def compose(children):
    if len(children)!=3:raise ValueError('three branches required')
    a,b,c=[x['sql'].strip().rstrip(';') for x in children]
    abfields=['a.report_month',*[f'a.{x} AS pair_{x}' for x in PAIR_METRICS],*[f'b.{x} AS cohort_{x}' for x in COHORT_METRICS]]
    ab='SELECT '+','.join(abfields)+f' FROM ({a}) a INNER JOIN ({b}) b ON a.report_month=b.report_month'
    fields=['COALESCE(ab.report_month,c.report_month) AS report_month',*[f'ab.pair_{x}' for x in PAIR_METRICS],*[f'ab.cohort_{x}' for x in COHORT_METRICS],*[f'c.{x} AS transition_{x}' for x in TRANSITION_METRICS]]
    return sql('SELECT '+','.join(fields)+f' FROM ({ab}) ab FULL OUTER JOIN ({c}) c ON ab.report_month=c.report_month')


def assemble(a,b,c):
    def indexed(rows):
        d={}
        for row in rows:
            if row[0] is None or row[0] in d:raise ValueError('nonnull unique month keys required')
            d[row[0]]=tuple(row[1:])
        return d
    a,b,c=map(indexed,(a,b,c));ab={k:a[k]+b[k] for k in a.keys()&b.keys()}
    return [(k,*ab.get(k,(None,)*(len(PAIR_METRICS)+len(COHORT_METRICS))),*c.get(k,(None,)*len(TRANSITION_METRICS))) for k in sorted(ab.keys()|c.keys())]


def generate(output,database):
    children=parts();cases=[(f'keyed_branch_{i}',p['sql'],True,[p]) for i,p in enumerate(children)]+[('keyed_inner_then_full',compose(children),False,children)]
    output=Path(output);output.mkdir(parents=True,exist_ok=False);(output/'sql').mkdir();qs=[]
    for name,body,control,ps in cases:
        qid=name+'__train_00';f=output/'sql'/(qid+'.sql');f.write_text(body)
        qs.append(dict(id=qid,query_id=qid,split='train',database=database,template_family=name,parameters=ps[0]['parameters'],input_parameters=[p['parameters'] for p in ps],control=control,sql_file=str(f.relative_to(output)),sql_sha256=hashlib.sha256(f.read_bytes()).hexdigest()))
    fs=[Path(__file__),*[Path(__file__).with_name(x) for x in ['generate_month_keyed_branch_suite.py','generate_temporal_activity_suite.py','generate_pair_grain_stress.py','generate_mixed_operator_suite.py','generate_integrity_operator_suite.py','generate_opposed_branch_stress.py']]]
    m=dict(schema_version=1,query_count=4,queries=qs,component_source_sha256={str(f.resolve()):hashlib.sha256(f.read_bytes()).hexdigest() for f in fs},contract=dict(training_eligible=False,concurrency=1,scope='Three standalone controls and one unequal-depth (A INNER B) FULL C keyed composition. Structural diagnostic, not three new business families or guaranteed improvement.',semantics='Complete metrics retain separate namespaces. C counts consecutive observed customer-day transitions; initial days not changes, NULL monetary values preserved. INNER intersection followed by FULL union; missing branch metrics NULL. Existing pair and cohort semantics unchanged.',stage='Four-query semantic/plan checks before serial pilot; no training or automatic expansion. Shared input optional.'))
    (output/'train.json').write_text(json.dumps(m,indent=2)+'\n');return m
