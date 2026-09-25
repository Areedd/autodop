from dataclasses import replace
import sqlite3

import pytest

from generate_integrity_operator_suite import date_key
from generate_mixed_operator_suite import Parameters
from generate_statistical_analytical_suite import generate_statistical, render_statistical


P = Parameters('basket_category_affinity', 0, 1176, 3, 3, 'i_category', 5)


def fixture():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE item(i_item_sk INTEGER, i_category_id INTEGER, i_category TEXT, i_class TEXT)')
    db.executemany('INSERT INTO item VALUES (?, ?, ?, ?)',
                   [(1, 10, 'A', 'shared'), (2, 10, 'A', 'shared'),
                    (3, 20, 'B', 'shared'), (4, 30, 'C', 'shared')])
    db.execute('CREATE TABLE store_sales(ss_ticket_number INTEGER, ss_item_sk INTEGER, ss_sold_date_sk INTEGER, ss_net_paid INTEGER, ss_net_profit INTEGER, ss_quantity INTEGER)')
    day = date_key(P.start_month)
    db.executemany('INSERT INTO store_sales VALUES (?, ?, ?, ?, ?, ?)', [
        (1, 1, day, 10, 1, 1), (1, 2, day, 20, 2, 2), (1, 3, day, 30, 3, 3),
        (1, 4, day, 40, 4, 4), (2, 1, day + 1, 50, 5, 5),
        (2, 3, day + 1, 60, 6, 6), (3, 1, date_key(P.start_month + 3), 999, 9, 9)])
    return db


def test_receipt_preaggregation_prevents_duplicate_pair_counts():
    with fixture() as db:
        rows = {r[:2]: r[2:] for r in db.execute(render_statistical(P))}
    assert rows == {('A', 'B'): (2, 170, 17), ('A', 'C'): (1, 70, 7), ('B', 'C'): (1, 70, 7)}


def test_same_class_name_in_different_categories_is_not_merged():
    with fixture() as db:
        rows = db.execute(render_statistical(replace(P, group='i_class'))).fetchall()
    assert len(rows) == 3
    assert sum(row[4] for row in rows) == 4
    assert all(row[0] != row[1] and row[2] == row[3] == 'shared' for row in rows)


def test_rolling_metrics_preserve_all_days_and_consume_prior_history():
    with fixture() as db:
        rows = db.execute(render_statistical(replace(P, family='product_multimetric_trend'))).fetchall()
    rows.sort()
    assert rows[0][1:] == (4, 100, 10, 10, 100, 10, 10, 0)
    assert rows[1][1:] == (2, 110, 11, 11, 150, 15, 15, 2)


def test_generation_is_deterministic_and_not_a_replacement_corpus(tmp_path):
    a = generate_statistical(tmp_path / 'a', 'test_db')
    assert a == generate_statistical(tmp_path / 'b', 'test_db')
    assert len(a['queries']) == 84 and len({q['sql_sha256'] for q in a['queries']}) == 84
    assert sum(q['control'] for q in a['queries']) == 21
    assert a['contract']['query_dop_domain'] == [1, 2, 4, 8, 16]
    with pytest.raises(ValueError, match='new/empty'):
        generate_statistical(tmp_path / 'a', 'test_db')


@pytest.mark.parametrize('change', [{'family': 'bad'}, {'channel': 3}, {'months': 1}, {'group': 'bad'}, {'history_months': 6}])
def test_invalid_parameters(change):
    with pytest.raises(ValueError):
        render_statistical(replace(P, **change))
