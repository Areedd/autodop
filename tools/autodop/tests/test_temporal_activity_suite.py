import sqlite3
from dataclasses import replace
import pytest
from generate_mixed_operator_suite import Parameters, CHANNELS
from generate_integrity_operator_suite import date_key
from generate_temporal_activity_suite import render_temporal_activity


def fixture_db():
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE date_dim(d_date_sk INTEGER,d_month_seq INTEGER)')
    for month in range(1176,1179):
        db.executemany('INSERT INTO date_dim VALUES (?,?)',[(day,month) for day in range(date_key(month),date_key(month+1))])
    for c in CHANNELS[:2]:
        db.execute(f'CREATE TABLE {c.table}({c.customer} INTEGER,{c.sold_date} INTEGER,{c.net_paid} REAL)')
    start=date_key(1176)
    # Duplicate lines and same-day channels collapse to one customer-day, retaining money.
    store=[(1,start,10),(1,start,20),(1,start+7,30),(1,start+15,None),
           (2,date_key(1177),40),(None,start,999),(3,date_key(1179),999)]
    catalog=[(1,start,5),(1,start+16,50),(2,date_key(1177),None)]
    db.executemany('INSERT INTO store_sales VALUES (?,?,?)',store)
    db.executemany('INSERT INTO catalog_sales VALUES (?,?,?)',catalog)
    return db


def rows(family):
    p=Parameters(family,0,1176,3,3,'i_category',10)
    return [dict(x) for x in fixture_db().execute(render_temporal_activity(p))]


def test_spells_gap_boundary_ties_and_conservation():
    result=rows('activity_spells')
    assert sum(x['observed_spells'] for x in result)==3
    assert sum(x['first_observed_spells'] for x in result)==2
    assert sum(x['active_days'] for x in result)==5
    assert sum(x['span_days'] for x in result)==8
    assert sum(x['sales_lines'] for x in result)==8
    assert sum(x['revenue'] or 0 for x in result)==155
    assert sum(x['revenue_present'] for x in result)==6


def test_transitions_aggregate_same_day_and_keep_first_and_null_money():
    result=rows('channel_state_transitions')
    first=[x for x in result if x['gap_band']=='first_observed']
    assert sum(x['customer_days'] for x in first)==2
    assert all(x['previous_state'] is None and x['channel_state']==3 for x in first)
    long=next(x for x in result if x['gap_band']=='over_7_days')
    assert (long['previous_state'],long['channel_state'],long['revenue'],long['revenue_present'])==(1,1,None,0)
    assert sum(x['sales_lines'] for x in result)==8
    assert sum(x['revenue'] or 0 for x in result)==155


def test_retention_zero_cells_and_right_censoring():
    result=rows('observed_cohort_retention')
    assert {(x['cohort_month'],x['month_offset']) for x in result}=={(1176,0),(1176,1),(1176,2),(1177,0),(1177,1)}
    assert all(x['cohort_customers']==1 for x in result)
    assert sum(x['active_customers'] for x in result)==2
    assert sum(x['sales_lines'] for x in result)==8
    assert sum(x['revenue'] or 0 for x in result)==155
    assert all(x['revenue'] is None and x['revenue_present']==0 for x in result if x['active_customers']==0)


def test_invalid_windows_rejected():
    p=Parameters('activity_spells',0,1176,3,6,'i_category',10)
    with pytest.raises(ValueError):render_temporal_activity(p)
    with pytest.raises(ValueError):render_temporal_activity(replace(p,family='unrecognized'))


def test_dense_retention_equivalent_on_multimonth_reactivation_and_missing_money():
    db=fixture_db()
    db.executemany('INSERT INTO store_sales VALUES (?,?,?)',[
        (1,date_key(1177)+4,12),(1,date_key(1178)+5,None),
        (4,date_key(1176)+8,None),(4,date_key(1178)+3,25),
        (5,date_key(1178),None)])
    p=Parameters('observed_cohort_retention',0,1176,3,3,'i_category',10)
    expected=sorted(tuple(x) for x in db.execute(render_temporal_activity(p)))
    actual=sorted(tuple(x) for x in db.execute(render_temporal_activity(p,'dense_expansion')))
    assert actual==expected
    assert len(actual)==6
    # Reactivation does not inflate the cohort denominator in later follow-up cells.
    assert {x[2] for x in actual if x[0]==1176}=={2}
    for family in ('activity_spells','channel_state_transitions','scan_control'):
        q=replace(p,family=family)
        assert render_temporal_activity(q)==render_temporal_activity(q,'dense_expansion')
    with pytest.raises(ValueError):render_temporal_activity(p,'unknown')
