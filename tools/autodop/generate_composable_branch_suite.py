"""Relational branch grammar with exact bag semantics and disjoint metric namespaces."""
from dataclasses import dataclass, asdict
from itertools import combinations, product
import hashlib,json,re
from pathlib import Path
from generate_three_way_keyed_suite import parts
from generate_product_window_branch_suite import render,WINDOW_METRICS
from generate_opposed_branch_stress import HIGH

OPS={'inner':'INNER JOIN','left':'LEFT OUTER JOIN','full':'FULL OUTER JOIN','cross':'CROSS JOIN','union':'UNION ALL'}

@dataclass(frozen=True)
class Node:
    op: str
    branch: int | None = None
    left: 'Node | None' = None
    right: 'Node | None' = None


def from_dict(d):
    return Node(d["op"],d.get("branch"),from_dict(d["left"]) if d.get("left") is not None else None,from_dict(d["right"]) if d.get("right") is not None else None)


def bank():
    cs=parts();_,window,_=render(HIGH)
    cs.append({'sql':window,'columns':['report_month',*WINDOW_METRICS],'parameters':asdict(HIGH)})
    return cs


def leaves(n):
    if n.op=='leaf':
        if type(n.branch)!=int or n.left is not None or n.right is not None:raise ValueError('invalid leaf')
        return {n.branch}
    if n.op not in OPS or n.branch is not None or n.left is None or n.right is None:raise ValueError('invalid binary node')
    a,b=leaves(n.left),leaves(n.right)
    if a&b:raise ValueError('branch reuse needs explicit shared-DAG semantics')
    return a|b


def schema(cs):
    out=['report_month','branch_mask']
    for i,c in enumerate(cs):
        if c['columns'][0]!='report_month' or len(c['columns'])!=len(set(c['columns'])):raise ValueError('invalid branch schema')
        for col in c['columns'][1:]:
            if not re.fullmatch('[a-z][a-z0-9_]*',col):raise ValueError('invalid metric identifier')
            out.append(f'b{i}_{col}')
    return out


def compile_sql(n,cs):
    ids=leaves(n)
    if not ids<=set(range(len(cs))):raise ValueError('unknown branch')
    columns=schema(cs)
    def emit(x):
        own=leaves(x)
        if x.op=='leaf':
            i=x.branch;fields=['q.report_month',f'{1<<i} AS branch_mask']
            for j,c in enumerate(cs):
                fields += [f"{'q.'+col if i==j else 'NULL'} AS b{j}_{col}" for col in c['columns'][1:]]
            return 'SELECT '+','.join(fields)+f" FROM ({cs[i]['sql'].strip().rstrip(';')}) q"
        a,b=emit(x.left),emit(x.right)
        if x.op=='union':return f'SELECT * FROM ({a}) l UNION ALL SELECT * FROM ({b}) r'
        leftids=leaves(x.left)
        fields=['COALESCE(l.report_month,r.report_month) AS report_month','COALESCE(l.branch_mask,0)+COALESCE(r.branch_mask,0) AS branch_mask']
        for i,c in enumerate(cs):
            side='l' if i in leftids else 'r'
            fields += [f'{side}.b{i}_{col} AS b{i}_{col}' for col in c['columns'][1:]]
        condition='' if x.op=='cross' else ' ON l.report_month=r.report_month'
        return 'SELECT '+','.join(fields)+f' FROM ({a}) l {OPS[x.op]} ({b}) r'+condition
    # Cross joins require retaining both time keys; a single coalesced month is ambiguous.
    if contains(n,'cross'):raise ValueError('cross requires a multi-key output contract; use keyed joins or union')
    return emit(n)+';\n'


def contains(n,op):return n.op==op or (n.op!='leaf' and (contains(n.left,op) or contains(n.right,op)))


def evaluate(n,cs,rows):
    compile_sql(n,cs)  # Shared validation, no database execution.
    widths=[len(c['columns'])-1 for c in cs];size=2+sum(widths)
    def ev(x):
        if x.op=='leaf':
            i=x.branch;offset=2+sum(widths[:i]);out=[]
            for row in rows[i]:
                if row[0] is None or len(row)!=widths[i]+1:raise ValueError('invalid branch result')
                r=[None]*size;r[0]=row[0];r[1]=1<<i;r[offset:offset+widths[i]]=row[1:];out.append(tuple(r))
            return out
        a,b=ev(x.left),ev(x.right)
        if x.op=='union':return a+b
        out=[];matched=set();li=leaves(x.left)
        def merge(l,r):
            result=[l[0] if l else r[0],(l[1] if l else 0)+(r[1] if r else 0)];pos=2
            for i,w in enumerate(widths):
                src=l if i in li else r;result.extend(src[pos:pos+w] if src else [None]*w);pos+=w
            return tuple(result)
        for l in a:
            hits=[(j,r) for j,r in enumerate(b) if l[0]==r[0]]
            for j,r in hits:matched.add(j);out.append(merge(l,r))
            if not hits and x.op in ('left','full'):out.append(merge(l,None))
        if x.op=='full':out += [merge(None,r) for j,r in enumerate(b) if j not in matched]
        return out
    return ev(n)


def catalog():
    ops=['inner','left','full','union'];L=lambda i:Node('leaf',i)
    trees=[L(i) for i in range(4)]
    for b in (1,2,3):trees += [Node(op,left=L(0),right=L(b)) for op in ops]
    for b,c in combinations((1,2,3),2):
        for outer,inner in product(ops,repeat=2):
            trees += [Node(outer,left=Node(inner,left=L(0),right=L(b)),right=L(c)),Node(outer,left=L(0),right=Node(inner,left=L(b),right=L(c)))]
    return trees


def generate(output,database):
    cs=bank();trees=catalog();output=Path(output);output.mkdir(parents=True,exist_ok=False);(output/'sql').mkdir();qs=[]
    for index,n in enumerate(trees):
        qid=f'grammar_{index:03d}__train_00';f=output/'sql'/(qid+'.sql');body=compile_sql(n,cs);f.write_text(body)
        qs.append(dict(id=qid,query_id=qid,split='train',database=database,template_family='composable_branch',tree=asdict(n),branch_ids=sorted(leaves(n)),parameters=cs[0]['parameters'],input_parameters=[cs[i]['parameters'] for i in sorted(leaves(n))],control=n.op=='leaf',sql_file=str(f.relative_to(output)),sql_sha256=hashlib.sha256(f.read_bytes()).hexdigest()))
    m=dict(schema_version=1,query_count=len(qs),queries=qs,output_columns=schema(cs),branch_bank=[{k:v for k,v in c.items() if k!='sql'} for c in cs],contract=dict(training_eligible=False,concurrency=1,scope='Offline combinatorial catalog. Generated count is not measured success or distinct physical-plan count. Keyed inner/left/full and bag UNION compose disjoint branches; branch_mask preserves row provenance. Four-query pilot before execution expansion.',semantics='All metrics preserved in disjoint namespaces. UNION retains multiplicity; joins after UNION may multiply matching rows. No summing unrelated metrics. No repeated identical leaf padding; cross unsupported without multi-key schema. Existing branch results and source windows preserved.'))
    sources=['generate_composable_branch_suite.py','generate_three_way_keyed_suite.py','generate_product_window_branch_suite.py','generate_month_keyed_branch_suite.py','generate_temporal_activity_suite.py','generate_pair_grain_stress.py','generate_opposed_branch_stress.py','generate_mixed_operator_suite.py','generate_integrity_operator_suite.py']
    m['component_source_sha256']={str(Path(__file__).with_name(s).resolve()):hashlib.sha256(Path(__file__).with_name(s).read_bytes()).hexdigest() for s in sources}
    (output/'all.json').write_text(json.dumps(m,indent=2)+'\n')
    pilot_ids={0,1,5,7}  # standalone A/B, LEFT(A,B), UNION(A,B); fixed before timings.
    pilot={**m,'query_count':4,'queries':[q for i,q in enumerate(qs) if i in pilot_ids]}
    (output/'train.json').write_text(json.dumps(pilot,indent=2)+'\n');return m
