import pytest
from generate_three_way_keyed_suite import parts,compose,assemble
from test_opposed_branch_stress import fixture
from generate_integrity_operator_suite import date_key

@pytest.mark.parametrize('empty',[False,True])
def test_three_way_complete_outputs_and_outer_missing_sides(empty):
    db=fixture()
    if empty:db.execute('DELETE FROM catalog_sales');db.execute('DELETE FROM web_sales');db.execute('DELETE FROM store_sales')
    else:
        d=date_key(1178)
        db.executemany('INSERT INTO web_sales VALUES (?,?,?,?,?,?,?)',[(9,1,1,None,d,2,1),(9,1,2,None,d+1,None,1)])
        db.execute('INSERT INTO store_sales VALUES (?,?,?,?,?,?,?)',(9,1,3,None,d+2,-1,1))
    cs=parts();rs=[[tuple(r) for r in db.execute(c['sql'])] for c in cs]
    assert sorted(tuple(r) for r in db.execute(compose(cs)))==assemble(*rs)
    if not empty:
        c=dict((r[0],r[1:]) for r in rs[2]);assert c[1178]==(3,1,2,1,1)
        result=assemble(*rs);assert any(r[0]==1180 and r[1:14]==(None,)*13 for r in result)
    else:assert assemble(*rs)==[]


def test_full_stage_retains_left_only_right_only_and_rejects_duplicate():
    a=[(1,*range(8))];b=[(1,*range(5))];c=[(2,*range(5))]
    out=assemble(a,b,c)
    assert out[0][-5:]==(None,)*5 and out[1][1:14]==(None,)*13
    with pytest.raises(ValueError):assemble(a+a,b,c)
