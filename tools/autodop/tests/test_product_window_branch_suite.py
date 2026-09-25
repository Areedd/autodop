from collections import defaultdict
import pytest
from test_opposed_branch_stress import fixture
from generate_product_window_branch_suite import render
from generate_opposed_branch_stress import HIGH
from generate_pair_grain_stress import render_pair_grain
from generate_month_keyed_branch_suite import assemble
from generate_integrity_operator_suite import date_key

@pytest.mark.parametrize('empty',[False,True])
def test_full_window_reference_and_shared_composition(empty):
    db=fixture();db.execute('DELETE FROM catalog_sales');lo=date_key(1178);boundary=date_key(1179)
    if not empty:
        db.executemany('INSERT INTO catalog_sales VALUES (?,?,?,?,?,?,?)',[(None,7,1,None,lo,10,1),(None,7,2,None,lo,None,1),(9,7,3,None,lo+1,-3,1),(9,7,4,None,lo+10,0,1),(9,7,5,None,boundary,None,1),(9,8,6,None,boundary-1,2,1),(9,8,7,None,boundary,4,1),(9,8,None,None,boundary,999,1)])
    a,b,plain=render(HIGH);_,_,shared=render(HIGH,True);rows=lambda s:[tuple(r) for r in db.execute(s)]
    ar,br=rows(a),rows(b)
    assert sorted(ar)==sorted(rows(render_pair_grain(HIGH,'day',7,report_grain='anchor_month')))
    assert sorted(rows(plain))==assemble(ar,br,'inner')
    assert sorted(rows(shared))==assemble(ar,br,'inner')
    if empty:assert br==[]
    else:
        assert sorted(br)==[(1178,4,1,2,-10,9),(1179,2,1,1,2,4)]
