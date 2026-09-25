import sqlite3
import pytest
from generate_composable_branch_suite import Node,bank,catalog,compile_sql,evaluate,generate
from test_opposed_branch_stress import fixture


def test_all_trees_preserve_bag_duplicates_nulls_and_outer_rows():
    db=sqlite3.connect(':memory:');cs=[];rs=[]
    data=[[(1,2),(1,None),(2,-3)],[(1,4),(3,0)],[(2,None),(3,7)],[]]
    for i,rows in enumerate(data):
        db.execute(f'CREATE TABLE b{i}(report_month INTEGER,value INTEGER)');db.executemany(f'INSERT INTO b{i} VALUES (?,?)',rows)
        cs.append({'sql':f'SELECT * FROM b{i}','columns':['report_month','value']});rs.append(rows)
    assert len(catalog())==112
    for tree in catalog():
        actual=list(db.execute(compile_sql(tree,cs)))
        assert sorted(actual,key=repr)==sorted(evaluate(tree,cs,rs),key=repr)


def test_real_branch_grammar_on_fixture(tmp_path):
    db=fixture();cs=bank();rs=[[tuple(r) for r in db.execute(c['sql'])] for c in cs]
    for tree in catalog():
        actual=[tuple(r) for r in db.execute(compile_sql(tree,cs))]
        assert sorted(actual,key=repr)==sorted(evaluate(tree,cs,rs),key=repr)
    m=generate(tmp_path/'generated','fixture')
    assert m['query_count']==112
    assert len({q['sql_sha256'] for q in m['queries']})==112


def test_invalid_and_repeated_leaves_are_rejected():
    cs=bank();a=Node('leaf',0)
    for n in [Node('leaf',99),Node('inner',left=a,right=a),Node('cross',left=a,right=Node('leaf',1)),Node('bad',left=a,right=Node('leaf',1))]:
        with pytest.raises(ValueError):compile_sql(n,cs)
