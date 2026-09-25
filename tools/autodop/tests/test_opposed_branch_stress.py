import json
import sqlite3
from pathlib import Path
import pytest
from generate_integrity_operator_suite import date_key
from generate_mixed_operator_suite import CHANNELS
from generate_opposed_branch_stress import generate_opposed,parts,HIGH,LOW


def fixture():
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE date_dim(d_date_sk INTEGER,d_month_seq INTEGER)')
    for m in range(1178,1183):
        db.executemany('INSERT INTO date_dim VALUES (?,?)',[(d,m) for d in range(date_key(m),date_key(m+1))])
    for c in CHANNELS:
        columns=[c.customer,c.item,c.order,c.promo,c.sold_date,c.net_paid,c.profit]
        db.execute(f'CREATE TABLE {c.table}('+','.join(x+' NUMERIC' for x in columns)+')')
    lo=date_key(HIGH.start_month)
    db.executemany('INSERT INTO catalog_sales VALUES (?,?,?,?,?,?,?)',[
        (1,11,1,None,lo,10,1),(1,11,2,None,lo+1,None,1),(1,11,3,None,lo+7,-2,1)])
    db.executemany('INSERT INTO web_sales VALUES (?,?,?,?,?,?,?)',[
        (1,11,4,None,date_key(1180),10,1),(1,11,5,None,date_key(1181),None,1)])
    db.execute('INSERT INTO store_sales VALUES (?,?,?,?,?,?,?)',(2,11,6,None,date_key(1182),20,1))
    return db


def canonical(rows):
    return sorted((r[0],json.dumps(json.loads(r[1]),sort_keys=True)) for r in rows)


def test_union_exactly_preserves_both_distinct_complete_branch_outputs(tmp_path):
    m=generate_opposed(tmp_path/'sql','fixture_db');db=fixture();results={}
    for q in m['queries']:
        if q['control']:continue
        results[q['template_family']]=list(db.execute((tmp_path/'sql'/q['sql_file']).read_text()))
    assert len(results['stress_high_alone'])==3
    assert len(results['stress_low_alone'])==4
    assert canonical(results['stress_opposed_union'])==canonical(results['stress_high_alone']+results['stress_low_alone'])
    assert {json.loads(r[1])['lag_days'] for r in results['stress_high_alone']}=={'1','6','7'}


def test_normalization_retains_every_column_null_and_negative_values():
    db=fixture()
    for p in parts():
        raw=list(db.execute(p['sql']));normalized=list(db.execute(p['normalized_sql']))
        expected=sorted(json.dumps({k:None if row[k] is None else str(row[k]) for k in p['columns']},sort_keys=True) for row in raw)
        assert sorted(json.dumps(json.loads(row['payload']),sort_keys=True) for row in normalized)==expected


def test_fixed_context_and_immutable_constituent_exports(tmp_path):
    m=generate_opposed(tmp_path/'generated','fixture_db')
    assert len(m['queries'])==4
    combined=next(q for q in m['queries'] if q['template_family']=='stress_opposed_union')
    assert len(combined['constituents'])==2
    assert [p['start_month'] for p in combined['input_parameters']]==[HIGH.start_month,LOW.start_month]
    assert HIGH.start_month!=LOW.start_month
    for q in m['queries']:
        for part in q['constituents']:
            for key in ('sql','normalized_sql'):
                assert (tmp_path/'generated'/part[key+'_file']).exists()
    with pytest.raises(FileExistsError):generate_opposed(tmp_path/'generated','fixture_db')


@pytest.mark.parametrize("grain", ["day", "receipt"])
@pytest.mark.parametrize("horizon", [1, 7])
def test_structural_controls_preserve_union_and_retention_equivalence(tmp_path, grain, horizon):
    db = fixture()
    outputs = {}
    for strategy in ("reference", "dense_expansion"):
        dest = tmp_path / strategy
        manifest = generate_opposed(dest, "fixture_db", grain=grain,
                                    horizon=horizon, retention_strategy=strategy)
        rows = {q["template_family"]: list(db.execute((dest / q["sql_file"]).read_text()))
                for q in manifest["queries"] if not q["control"]}
        assert canonical(rows["stress_opposed_union"]) == canonical(
            rows["stress_high_alone"] + rows["stress_low_alone"])
        assert {int(json.loads(r[1])["lag_days"]) for r in rows["stress_high_alone"]} == (
            {1} if horizon == 1 else {1, 6, 7})
        outputs[strategy] = canonical(rows["stress_low_alone"])
    assert outputs["reference"] == outputs["dense_expansion"]


def test_cross_join_preserves_full_cartesian_branch_results_and_empty_semantics(tmp_path):
    db = fixture()
    out = tmp_path / "cross"
    manifest = generate_opposed(out, "fixture_db", composition="cross_join")
    queries = {q["template_family"]: (out / q["sql_file"]).read_text()
               for q in manifest["queries"]}
    high = [json.loads(r[1]) for r in db.execute(queries["stress_high_alone"])]
    low = [json.loads(r[1]) for r in db.execute(queries["stress_low_alone"])]
    rows = list(db.execute(queries["stress_opposed_cross_join"]))
    decoded = [(json.loads(json.loads(r[1])["high_payload"]),
                json.loads(json.loads(r[1])["low_payload"])) for r in rows]
    assert sorted(json.dumps(x, sort_keys=True) for x in decoded) == sorted(
        json.dumps((h, l), sort_keys=True) for h in high for l in low)
    assert len(rows) == 12
    db.execute("DELETE FROM catalog_sales")
    assert list(db.execute(queries["stress_opposed_cross_join"])) == []


def test_independent_branch_context_parameters_and_empty_high_output(tmp_path):
    from dataclasses import replace
    high = replace(HIGH, start_month=1179)
    out = tmp_path / "shifted"
    manifest = generate_opposed(out, "fixture_db", high=high, low=LOW)
    db = fixture()
    rows = {q["template_family"]: list(db.execute((out / q["sql_file"]).read_text()))
            for q in manifest["queries"] if not q["control"]}
    assert rows["stress_high_alone"] == []
    assert canonical(rows["stress_opposed_union"]) == canonical(rows["stress_low_alone"])
    combined = next(q for q in manifest["queries"] if q["template_family"] == "stress_opposed_union")
    assert [p["start_month"] for p in combined["input_parameters"]] == [1179, 1180]
    with pytest.raises(ValueError):
        generate_opposed(tmp_path / "invalid", "fixture_db", low=HIGH)
    assert not (tmp_path / "invalid").exists()
