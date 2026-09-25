import sqlite3

from generate_range_branch_compositions import threshold_monthly
from generate_composable_branch_suite import Node, compile_sql, evaluate


def test_threshold_pivot_and_full_inner_unmatched_months():
    db = sqlite3.connect(':memory:')
    source = '''SELECT 1 AS report_month,100 AS threshold_amount,
3 AS qualified_customers,4 AS sales_lines,11100 AS revenue,4 AS revenue_present
UNION ALL SELECT 1,1000,2,2,11000,2
UNION ALL SELECT 3,100,1,1,100,1'''
    body, columns = threshold_monthly(source)
    rhs = db.execute(body).fetchall()
    assert sorted(rhs) == [(1, 3, 4, 11100, 4, 2, 2, 11000, 2, 0, 0, 0, 0),
                           (3, 1, 1, 100, 1, 0, 0, 0, 0, 0, 0, 0, 0)]
    cs = [dict(sql='SELECT 1 AS report_month,7 AS value UNION ALL SELECT 2,9',
               columns=['report_month', 'value']), dict(sql=body, columns=columns)]
    for op, size in [('inner', 1), ('full', 3)]:
        tree = Node(op, left=Node('leaf', 0), right=Node('leaf', 1))
        actual = db.execute(compile_sql(tree, cs)).fetchall()
        expected = evaluate(tree, cs, [[(1, 7), (2, 9)], rhs])
        assert len(actual) == size
        assert sorted(actual, key=repr) == sorted(expected, key=repr)
