import sqlite3

from generate_range_expansion_suite import render_from_monthly


def test_threshold_boundaries_nulls_and_overlapping_membership():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE m(customer_id,month_seq,sales_lines,revenue,revenue_present)')
    db.executemany('INSERT INTO m VALUES (?,?,?,?,?)', [
        (1, 1, 2, 100, 2), (2, 1, 1, 1000, 1), (3, 1, 1, 10000, 1),
        (4, 1, 1, None, 0), (5, 1, 1, -20, 1), (6, 1, 1, 99, 1)])
    rows = db.execute(render_from_monthly('monthly_spend_thresholds', 'SELECT * FROM m', '')).fetchall()
    assert sorted(rows) == [(1, 100, 3, 4, 11100, 4),
                            (1, 1000, 2, 2, 11000, 2), (1, 10000, 1, 1, 10000, 1)]
    db.execute('DELETE FROM m')
    assert db.execute(render_from_monthly('monthly_spend_thresholds', 'SELECT * FROM m', '')).fetchall() == []


def test_span_includes_endpoints_and_inactive_gap_without_double_counting():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE m(customer_id,month_seq)')
    db.executemany('INSERT INTO m VALUES (?,?)', [(1, 1), (1, 3), (2, 2), (3, 3)])
    months = 'SELECT 1 AS month_seq UNION ALL SELECT 2 UNION ALL SELECT 3'
    query = render_from_monthly('observed_span_coverage', 'SELECT * FROM m', months)
    assert sorted(db.execute(query).fetchall()) == [(1, 1, 1, 0), (2, 2, 1, 1), (3, 2, 1, 2)]
    db.execute('DELETE FROM m')
    assert db.execute(query).fetchall() == []
