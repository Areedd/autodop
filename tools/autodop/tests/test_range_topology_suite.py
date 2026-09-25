import sqlite3

from generate_composable_branch_suite import compile_sql, evaluate
from generate_range_topology_suite import topologies


def test_all_topologies_preserve_bags_nulls_and_empty_branches():
    db = sqlite3.connect(':memory:')
    for name in ('a', 'b', 'c'):
        db.execute(f'CREATE TABLE {name}(report_month INTEGER, value INTEGER)')
    components = [dict(sql=f'SELECT * FROM {name}', columns=['report_month', 'value'])
                  for name in ('a', 'b', 'c')]
    cases = [
        [[(1, 7), (2, None)], [(1, 4), (1, 4), (3, 9)], [(1, None), (4, 2)]],
        [[], [(1, 3)], [(2, 4)]],
        [[(1, 3)], [], [(2, 4)]],
        [[(1, 3)], [(1, 4)], []],
    ]
    for rows in cases:
        for name, values in zip(('a', 'b', 'c'), rows):
            db.execute(f'DELETE FROM {name}')
            db.executemany(f'INSERT INTO {name} VALUES (?,?)', values)
        for _, tree in topologies():
            actual = db.execute(compile_sql(tree, components)).fetchall()
            assert sorted(actual, key=repr) == sorted(evaluate(tree, components, rows), key=repr)
    db.close()
