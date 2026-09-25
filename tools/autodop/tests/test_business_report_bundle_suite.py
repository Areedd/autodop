import json
from dataclasses import replace
import pytest
from generate_mixed_operator_suite import Parameters,render_query
from generate_business_report_bundle_suite import BUNDLES,constituents,render_bundle,generate_bundles
from test_temporal_activity_suite import fixture_db
from test_customer_quality_suite import CustomerQualitySemanticsTest
from test_linked_price_suite import LinkedPriceSemanticsTest


@pytest.mark.parametrize('family',list(BUNDLES))
def test_bundle_preserves_every_constituent_value_and_multiplicity(family):
    owner=None
    if family=='engagement_retention_bundle':db=fixture_db()
    elif family=='customer_contact_quality_bundle':
        owner=CustomerQualitySemanticsTest();owner.setUp();db=owner.db
    else:
        owner=LinkedPriceSemanticsTest();owner.setUp();db=owner.db
        db.execute('INSERT INTO inventory VALUES (1,1,?,20)',(owner.day,))
        db.execute('INSERT INTO store_returns VALUES (1,?,1,5)',(owner.day,))
        db.execute('INSERT INTO date_dim VALUES (?,1176)',(owner.day+14,))
        db.executemany('INSERT INTO inventory VALUES (1,1,?,21)',[(owner.day+7,),(owner.day+14,)])
    try:
        p=Parameters(family,0,1176,3,3,'i_category',10)
        expected=[]
        for part in constituents(p):
            cur=db.execute(part['sql']);assert [x[0] for x in cur.description]==part['columns']
            for row in cur.fetchall():
                row=[round(v,6) if c=='average_observed_units' and v is not None else v for c,v in zip(part['columns'],row)]
                payload={c:None if v is None else str(v) for c,v in zip(part['columns'],row)}
                expected.append((part['family'],json.dumps(payload,sort_keys=True)))
        actual=[(row[0],json.dumps(json.loads(row[1]),sort_keys=True)) for row in db.execute(render_bundle(p))]
        assert sorted(actual)==sorted(expected)
        assert {row[0] for row in actual}==set(BUNDLES[family])
    finally:db.close()


def test_control_and_invalid_bundle():
    p=Parameters('scan_control',0,1176,3,3,'i_category',10)
    assert render_bundle(p)==render_query(p)
    with pytest.raises(ValueError):render_bundle(replace(p,family='made_up'))
    with pytest.raises(ValueError):render_bundle(replace(p,months=1))


def test_generation_exports_frozen_components_and_splits(tmp_path):
    m=generate_bundles(tmp_path/'suite','fixture_db',test_variants=1)
    assert m['query_count']==36
    assert {q['split'] for q in m['queries']}=={'train','valid','test'}
    import hashlib
    for q in m['queries']:
        for part in q.get('constituents',[]):
            for name in ('sql','normalized_sql'):
                path=tmp_path/'suite'/part[name+'_file']
                assert hashlib.sha256(path.read_bytes()).hexdigest()==part[name+'_sha256']
    with pytest.raises(ValueError):generate_bundles(tmp_path/'suite','fixture_db')
