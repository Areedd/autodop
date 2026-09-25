from collections import defaultdict
from dataclasses import replace
import pytest
from generate_integrity_operator_suite import date_key
from generate_month_keyed_branch_suite import branches, compose, assemble, generate, JOIN_KINDS
from generate_opposed_branch_stress import HIGH, LOW
from generate_pair_grain_stress import render_pair_grain
from test_opposed_branch_stress import fixture


@pytest.mark.parametrize('empty', ['none','pairs','cohorts','all'])
def test_both_keyed_joins_preserve_complete_missing_month_semantics(tmp_path, empty):
    db=fixture()
    if empty in ('pairs','all'):db.execute('DELETE FROM catalog_sales')
    if empty in ('cohorts','all'):
        db.execute('DELETE FROM web_sales');db.execute('DELETE FROM store_sales')
    children=branches();a,b=[list(db.execute(c['sql'])) for c in children]
    for kind in JOIN_KINDS:
        actual=[tuple(row) for row in db.execute(compose(children,kind))]
        assert sorted(actual)==assemble(a,b,kind)
    if empty=='none':
        assert [r[0] for r in a]==[1178] and [r[0] for r in b]==[1180]
        assert assemble(a,b,'inner')==[]
        assert len(assemble(a,b,'full_outer'))==2
        assert tuple(b[0])==(1180,1,1,1,10,1)
    m=generate(tmp_path/'out','fixture');assert m['query_count']==4
    assert [q['control'] for q in m['queries']]==[True,True,False,False]
    assert not m['contract']['training_eligible']


def test_month_pair_reference_counts_censoring_and_cross_month_exposures():
    db=fixture();boundary=date_key(1179);end=date_key(1181)
    db.executemany('INSERT INTO catalog_sales VALUES (?,?,?,?,?,?,?)',[
        (1,12,10,None,boundary-1,3,1),(1,12,11,None,boundary,-1,1),
        (1,12,12,None,boundary-1,None,1),(1,12,13,None,boundary+6,0,1),
        (1,13,14,None,end-7,20,1),(1,13,15,None,end-1,30,1)])
    raw=list(db.execute('SELECT cs_item_sk,cs_sold_date_sk,cs_net_paid FROM catalog_sales WHERE cs_order_number IS NOT NULL AND cs_item_sk IS NOT NULL'))
    units=defaultdict(list)
    for item,day,value in raw:units[item,day].append(value)
    refs=defaultdict(lambda: {'pairs':0,'products':set(),'anchor_lines':0,'follow_lines':0,'anchor_money':[], 'follow_money':[]})
    for (item,day),amounts in units.items():
        if not date_key(1178)<=day<end-7:continue
        month=next(m for m in range(1178,1181) if date_key(m)<=day<date_key(m+1))
        for (other,follow),other_amounts in units.items():
            if other!=item or not day<follow<=day+7 or follow>=end:continue
            v=refs[month];v['pairs']+=1;v['products'].add(item)
            v['anchor_lines']+=len(amounts);v['follow_lines']+=len(other_amounts)
            v['anchor_money'] += [x for x in amounts if x is not None]
            v['follow_money'] += [x for x in other_amounts if x is not None]
    actual={row[0]:tuple(row[1:]) for row in db.execute(render_pair_grain(HIGH,'day',7,report_grain='anchor_month'))}
    expected={m:(v['pairs'],len(v['products']),v['anchor_lines'],v['follow_lines'],sum(v['anchor_money']) if v['anchor_money'] else None,sum(v['follow_money']) if v['follow_money'] else None,len(v['anchor_money']),len(v['follow_money'])) for m,v in refs.items()}
    assert actual==expected
    assert 1178 in actual and 1179 in actual and 1180 not in actual


def test_month_join_overlap_and_cohort_eligible_totals():
    db=fixture();db.execute('INSERT INTO web_sales VALUES (?,?,?,?,?,?,?)',(9,11,20,None,date_key(1178),-5,1))
    children=branches();a,b=[list(db.execute(c['sql'])) for c in children]
    assert len(assemble(a,b,'inner'))==1
    assert sorted(tuple(r) for r in db.execute(compose(children,'inner')))==assemble(a,b,'inner')
    rows={r[0]:tuple(r[1:]) for r in b}
    assert rows[1179]==(1,0,0,None,0)
    assert rows[1180]==(2,1,1,10,1)


def test_invalid_month_grain_context_and_duplicate_keys_are_rejected(tmp_path):
    with pytest.raises(ValueError):render_pair_grain(HIGH,'day',7,report_grain='unknown')
    with pytest.raises(ValueError):generate(tmp_path/'invalid','fixture',low=LOW)
    assert not (tmp_path/'invalid').exists()
    with pytest.raises(ValueError):assemble([(1,),(1,)],[],'full_outer')
    with pytest.raises(ValueError):assemble([(None,)],[],'full_outer')
    with pytest.raises(ValueError):compose(branches(),'cross')
