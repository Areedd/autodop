import sqlite3

from generate_composable_branch_suite import compile_sql, evaluate, leaves
from generate_multi_low_branch_suite import bank, topologies
from generate_range_branch_compositions import bank as range_bank


def test_four_branch_bags_and_every_empty_combination():
    db = sqlite3.connect(':memory:')
    names = ['a', 't', 's', 'b']
    components = []
    for name in names:
        db.execute(f'CREATE TABLE {name}(report_month INTEGER, value INTEGER)')
        components.append(dict(sql=f'SELECT * FROM {name}', columns=['report_month', 'value']))
    full = [[(1, 8), (2, None)], [(1, 3), (1, 3), (3, 9)], [(1, None), (4, 1)], [(1, 2), (2, 4)]]
    assert [len(leaves(tree)) for _, tree in topologies()] == [3, 3, 4, 4]
    assert bank()[:3] == range_bank()
    for mask in range(16):
        rows = [values if mask & (1 << i) else [] for i, values in enumerate(full)]
        for name, values in zip(names, rows):
            db.execute(f'DELETE FROM {name}')
            db.executemany(f'INSERT INTO {name} VALUES (?,?)', values)
        for _, tree in topologies():
            actual = db.execute(compile_sql(tree, components)).fetchall()
            assert sorted(actual, key=repr) == sorted(evaluate(tree, components, rows), key=repr)
    db.close()
