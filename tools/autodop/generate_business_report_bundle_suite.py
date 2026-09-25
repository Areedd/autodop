#!/usr/bin/env python3
"""Coherent report bundles with exported constituents and exact text-valued payloads."""
import argparse
from dataclasses import replace
import hashlib,json
from pathlib import Path
from generate_mixed_operator_suite import generate,render_query,sql,Parameters
from generate_temporal_activity_suite import render_temporal_activity
from generate_customer_quality_suite import render_customer_quality
from generate_linked_price_suite import render_linked_price,linked_components,taxonomy_fields

BUNDLES = {
    'engagement_retention_bundle': ('activity_spells','observed_cohort_retention'),
    'customer_contact_quality_bundle': ('active_postal_consistency','contact_domain_sales'),
    'product_inventory_returns_bundle': ('price_inventory_profile','price_returns_profile'),
}
FAMILIES = {
    'engagement_retention_bundle': 'Observed shopping activity and cohort follow-up in the same two channels/window',
    'customer_contact_quality_bundle': 'Address consistency and email-domain quality for the same active-customer population',
    'product_inventory_returns_bundle': 'Price-position sales with inventory snapshots and return exposure for the same product/month population',
    'scan_control': 'Unchanged single-channel scan control',
}
COLUMNS = {
    'activity_spells': ['seen_a','seen_b','duration_band','observed_spells','first_observed_spells','active_days','span_days','sales_lines','revenue','revenue_present'],
    'observed_cohort_retention': ['cohort_month','month_offset','cohort_customers','active_customers','sales_lines','revenue','revenue_present'],
    'active_postal_consistency': ['quality','report_groups','known_customers','matched_customers','matched_customer_address_links','sales_lines','revenue','revenue_present'],
    'contact_domain_sales': ['quality','domain_size_band','domains','known_customers','matched_customers','sales_lines','revenue','revenue_present'],
}


def constituents(p):
    if p.family not in BUNDLES:
        raise ValueError('expected known report bundle')
    result=[]
    for family in BUNDLES[p.family]:
        child=replace(p,family=family)
        if family in ('activity_spells','observed_cohort_retention'):
            text=render_temporal_activity(child,'dense_expansion');columns=COLUMNS[family]
        elif family in ('active_postal_consistency','contact_domain_sales'):
            text=render_customer_quality(child,'size_bands');columns=COLUMNS[family]
        else:
            text=render_linked_price(child);_,metrics=linked_components(child)
            columns=['month_seq',*taxonomy_fields(p.group),'product_months','matched_product_months','sales_lines','quantity','revenue','profit','profit_present','lower_price_lines','upper_price_lines','upper_price_revenue',*metrics]
        # JSON string/null values avoid converting exact monetary decimals into JSON floats.
        # All source columns are retained; report_kind gives the payload its schema.
        expressions=[]
        for c in columns:
            value=f'report_input.{c}'
            if c=='average_observed_units':
                value=f'CAST(ROUND({value}, 6) AS DECIMAL(38,6))'
            expressions.append(f"'{c}', CAST({value} AS VARCHAR)")
        args=', '.join(expressions)
        normalized=sql(f"SELECT '{family}' AS report_kind, JSON_OBJECT({args}) AS payload\nFROM ({text.strip().rstrip(';')}) report_input")
        result.append({'family':family,'columns':list(columns),'sql':text,'normalized_sql':normalized})
    return result


def render_bundle(p):
    if p.family=='scan_control':
        if p.months!=p.history_months or p.months not in (3,6):raise ValueError('invalid bundle control window')
        return render_query(p)
    parts=constituents(p)
    return sql('\nUNION ALL\n'.join(x['normalized_sql'].strip().rstrip(';') for x in parts))


def generate_bundles(output,database,seed=20270222,test_variants=5):
    output=Path(output)
    generate(output,database,seed,test_variants,families=FAMILIES,renderer=render_bundle,source_path=Path(__file__),window_months=(3,6))
    component_dir=output/'constituents';component_dir.mkdir()
    for split in ('all','train','valid','test'):
        path=output/(split+'.json');m=json.loads(path.read_text())
        for q in m['queries']:
            if q['control']:continue
            parts=[]
            for part in constituents(Parameters(**q['parameters'])):
                name=q['id']+'__'+part['family']
                entry={'family':part['family'],'columns':part['columns']}
                for field in ('sql','normalized_sql'):
                    dest=component_dir/(name+'__'+field+'.sql');data=part[field].encode()
                    if dest.exists():assert dest.read_bytes()==data
                    else:dest.write_bytes(data)
                    entry[field+'_file']=str(dest.relative_to(output));entry[field+'_sha256']=hashlib.sha256(data).hexdigest()
                parts.append(entry)
            q['constituents']=parts
        m['contract'].update(
            representativeness='Three coherent reporting contexts spanning temporal windows, customer-quality grouping and product/month joins. These are compositions of six existing semantic families, not six new families or a production frequency sample.',
            output_contract='report_kind plus JSON payload with every constituent column retained as string or null; monetary decimal strings retain their precision; floating average_observed_units is explicitly rounded to six decimal places before encoding. NULL and empty string remain distinct. No cross-report summation is meaningful.',
            population='Identical channel/date/group parameters within each bundle; inherited population exclusions, current-address limitations, within-window cohort censoring and same-month return exposure semantics remain unchanged.',
            required_comparators='Same bundled SQL for all DOP modes, plus separate execution of normalized constituent reports with query-level DOP tuning; use identical execute-and-fetch timing boundaries and count all constituent statement round trips/fetches; SET costs disclosed separately for both forms, no concurrency.',
            construction='Fixed business combinations; no repeated copy of the same report, artificial row multiplication, sleep or measured-latency selection. Construction is informed by prior development findings and must be disclosed.',
            stage='Fixtures and exact bundle/constituent equality before fixed four-query serial prepass; no automatic expansion',query_dop_domain=[1,2,4,8,16],learned_max_dop=16)
        m['component_source_sha256']={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ['generate_temporal_activity_suite.py','generate_customer_quality_suite.py','generate_linked_price_suite.py','generate_integrity_operator_suite.py','generate_statistical_analytical_suite.py']}
        path.write_text(json.dumps(m,indent=2)+'\n')
    return json.loads((output/'all.json').read_text())


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True);parser.add_argument('--database',required=True)
    args=parser.parse_args();print(json.dumps({'queries':generate_bundles(args.output_dir,args.database)['query_count']}))
