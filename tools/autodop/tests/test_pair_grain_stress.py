import sqlite3
import pytest
from generate_integrity_operator_suite import date_key
from generate_mixed_operator_suite import Parameters
from generate_pair_grain_stress import render_pair_grain, generate_stress


def p():return Parameters('scan_control',1,1178,3,3,'i_category',10)


def fixture():
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE catalog_sales(cs_item_sk INTEGER,cs_order_number INTEGER,cs_sold_date_sk INTEGER,cs_net_paid REAL)')
    lo=date_key(1178)
    db.executemany('INSERT INTO catalog_sales VALUES (?,?,?,?)',[
        (11,101,lo,10),(11,101,lo,10),(11,102,lo,20),
        (11,201,lo+1,4),(11,202,lo+1,None),(11,203,lo+1,-1),(11,301,lo+7,5),
        (11,None,lo+1,999),(None,999,lo+1,999)])
    return db


def test_day_receipt_factor_counts_real_units_and_repeated_exposure():
    db=fixture()
    day=dict(db.execute(render_pair_grain(p(),'day',1)).fetchone())
    receipt=dict(db.execute(render_pair_grain(p(),'receipt',1)).fetchone())
    assert day['matched_unit_pairs']==1 and receipt['matched_unit_pairs']==6
    assert (day['anchor_line_exposure'],day['follow_line_exposure'])==(3,3)
    assert (receipt['anchor_line_exposure'],receipt['follow_line_exposure'])==(9,6)
    assert (receipt['anchor_revenue_exposure'],receipt['follow_revenue_exposure'])==(120,6)
    assert receipt['follow_present_exposure']==4


def test_horizon_and_anchor_population_stay_matched():
    db=fixture();hi=date_key(1181)
    db.executemany('INSERT INTO catalog_sales VALUES (?,?,?,?)',[(22,1,hi-7,10),(22,2,hi-6,20)])
    one=[dict(x) for x in db.execute(render_pair_grain(p(),'receipt',1))]
    seven=[dict(x) for x in db.execute(render_pair_grain(p(),'receipt',7))]
    assert one==[x for x in seven if x['lag_days']==1]
    assert {x['lag_days']:x['matched_unit_pairs'] for x in seven}=={1:6,6:3,7:2}


def test_all_null_money_is_not_zero():
    db=fixture();db.execute('DELETE FROM catalog_sales');lo=date_key(1178)
    db.executemany('INSERT INTO catalog_sales VALUES (?,?,?,?)',[(1,1,lo,None),(1,2,lo+1,None)])
    for grain in ('day','receipt'):
        x=db.execute(render_pair_grain(p(),grain,1)).fetchone()
        assert x['anchor_revenue_exposure'] is None and x['follow_revenue_exposure'] is None
        assert x['anchor_present_exposure']==x['follow_present_exposure']==0


def test_four_fixed_variants_and_immutable_output(tmp_path):
    m=generate_stress(tmp_path/'generated','fixture_db')
    assert m['query_count']==4 and sum(q['control'] for q in m['queries'])==1
    assert len({q['sql_sha256'] for q in m['queries']})==4
    with pytest.raises(FileExistsError):generate_stress(tmp_path/'generated','fixture_db')
    for grain,horizon in [('unknown',1),('day',2),('day',True)]:
        with pytest.raises(ValueError):render_pair_grain(p(),grain,horizon)
