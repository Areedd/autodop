#!/usr/bin/env python3
"""Observed active-customer address/contact quality, without checksum padding."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from generate_integrity_operator_suite import date_key
from generate_mixed_operator_suite import CHANNELS, generate, render_query, sql

FAMILIES = {
    'active_address_sharing': 'Active-customer shared normalized address groups',
    'active_postal_consistency': 'Observed postcode disagreement within normalized address groups',
    'contact_domain_sales': 'Active-customer email syntax and domain sales coverage',
    'scan_control': 'Single-channel sales scan control',
}
ADDRESS_FIELDS = ('street_number','street_name','street_type','suite_number','city','state','zip','country')
CONTACT_OUTPUTS = ('domain', 'size_bands')
QUALITY_WINDOWS = (3, 6, 12)
EMAIL_PATTERN = '^[^@[:space:]]+@[^@[:space:]]+[.][^@[:space:]]+$'


def quality_components(p):
    if p.family not in FAMILIES or p.family == 'scan_control':
        raise ValueError('expected customer-quality family')
    render_query(replace(p,family='scan_control'))
    if p.months != p.history_months or p.months not in QUALITY_WINDOWS:
        raise ValueError('customer quality requires equal3/6/12month windows')
    c=CHANNELS[p.channel]
    low,high=date_key(p.start_month),date_key(p.start_month+p.months)
    prefix=f"""active AS (
SELECT s.{c.customer} AS customer_id, COUNT(*) AS sales_lines,
       SUM(s.{c.net_paid}) AS revenue, COUNT(s.{c.net_paid}) AS revenue_present
FROM {c.table} s WHERE s.{c.sold_date} >= {low} AND s.{c.sold_date} < {high}
GROUP BY s.{c.customer})"""
    if p.family=='contact_domain_sales':
        return prefix+f""", contacts AS (
SELECT a.*, c.c_customer_sk AS matched_customer,
       LOWER(TRIM(c.c_email_address)) AS email
FROM active a LEFT JOIN customer c ON a.customer_id=c.c_customer_sk),
classified AS (
SELECT *, CASE WHEN matched_customer IS NULL THEN 'unmatched_customer'
               WHEN email IS NULL THEN 'null_email' WHEN email='' THEN 'empty_email'
               WHEN REGEXP(email, '{EMAIL_PATTERN}') THEN 'syntax_pass' ELSE 'syntax_fail' END AS quality,
       CASE WHEN REGEXP(email, '{EMAIL_PATTERN}') THEN SPLIT_PART(email,'@',2) ELSE NULL END AS email_domain
FROM contacts)"""
    norm=',\n       '.join(f"REGEXP_REPLACE(UPPER(TRIM(ca.ca_{field})), '[ ]+', ' ') AS {field}" for field in ADDRESS_FIELDS)
    required=[f"{field} IS NOT NULL AND {field}<>''" for field in ADDRESS_FIELDS if field!='suite_number']
    complete=' AND '.join(required+['suite_number IS NOT NULL'])
    return prefix+f""", normalized AS (
SELECT a.*, c.c_customer_sk AS matched_customer, ca.ca_address_sk AS matched_address,
       {norm}
FROM active a LEFT JOIN customer c ON a.customer_id=c.c_customer_sk
LEFT JOIN customer_address ca ON c.c_current_addr_sk=ca.ca_address_sk),
classified AS (
SELECT *, CASE WHEN matched_address IS NOT NULL AND {complete} THEN 1 ELSE 0 END AS complete_address
FROM normalized)"""


def render_customer_quality(p, contact_output='domain'):
    if contact_output not in CONTACT_OUTPUTS:
        raise ValueError('unknown contact output')
    if p.family=='scan_control':
        if p.months != p.history_months or p.months not in QUALITY_WINDOWS:raise ValueError('invalid window')
        return render_query(p)
    ctes=quality_components(p)
    if p.family=='contact_domain_sales':
        domain_totals = """SELECT quality, email_domain, COUNT(customer_id) AS known_customers,
       COUNT(matched_customer) AS matched_customers, SUM(sales_lines) AS sales_lines,
       SUM(revenue) AS revenue, SUM(revenue_present) AS revenue_present
FROM classified GROUP BY quality,email_domain"""
        if contact_output == 'domain':
            return sql(f"WITH {ctes}\n{domain_totals}")
        band = """CASE WHEN quality<>'syntax_pass' THEN 'not_applicable'
                    WHEN known_customers=1 THEN 'singleton'
                    WHEN known_customers<10 THEN '2_to_9'
                    WHEN known_customers<100 THEN '10_to_99' ELSE '100_plus' END"""
        return sql(f"""WITH {ctes}, domain_totals AS ({domain_totals})
SELECT quality, {band} AS domain_size_band, COUNT(email_domain) AS domains,
       SUM(known_customers) AS known_customers, SUM(matched_customers) AS matched_customers,
       SUM(sales_lines) AS sales_lines, SUM(revenue) AS revenue,
       SUM(revenue_present) AS revenue_present
FROM domain_totals GROUP BY quality, {band}""")
    keys=[f for f in ADDRESS_FIELDS if p.family!='active_postal_consistency' or f!='zip']
    # Incomplete records are isolated by customer identity; never one inferred household.
    grouping=', '.join(['complete_address','CASE WHEN complete_address=0 THEN customer_id ELSE NULL END',*keys])
    quality=("CASE WHEN complete_address=0 THEN 'incomplete' WHEN known_customers>1 THEN 'shared_address' ELSE 'single_customer' END"
             if p.family=='active_address_sharing' else
             "CASE WHEN complete_address=0 THEN 'incomplete' WHEN postcode_count>1 THEN 'postcode_disagreement' ELSE 'one_postcode' END")
    return sql(f"""WITH {ctes}, location_groups AS (
SELECT complete_address, COUNT(customer_id) AS known_customers,
       COUNT(matched_customer) AS matched_customers, COUNT(matched_address) AS matched_addresses,
       COUNT(DISTINCT zip) AS postcode_count, SUM(sales_lines) AS sales_lines,
       SUM(revenue) AS revenue, SUM(revenue_present) AS revenue_present
FROM classified GROUP BY {grouping})
SELECT {quality} AS quality, COUNT(*) AS report_groups,
       SUM(known_customers) AS known_customers, SUM(matched_customers) AS matched_customers,
       SUM(matched_addresses) AS matched_customer_address_links,
       SUM(sales_lines) AS sales_lines, SUM(revenue) AS revenue, SUM(revenue_present) AS revenue_present
FROM location_groups GROUP BY {quality}""")


def generate_customer_quality(output,database,seed=20270114,test_variants=5,contact_output='domain',window_months=(3,6)):
    if contact_output not in CONTACT_OUTPUTS:
        raise ValueError('unknown contact output')
    if not window_months or any(w not in QUALITY_WINDOWS for w in window_months):
        raise ValueError('customer-quality windows must be selected from3/6/12')
    output=Path(output)
    generate(output,database,seed,test_variants,families=FAMILIES,renderer=lambda p: render_customer_quality(p,contact_output),source_path=Path(__file__),window_months=window_months)
    for split in ('train','valid','test','all'):
        path=output/f'{split}.json';d=json.loads(path.read_text())
        d['date_helpers_source_sha256']=hashlib.sha256(Path(__file__).with_name('generate_integrity_operator_suite.py').read_bytes()).hexdigest()
        d['contract'].update(family_count=4,controls_fraction=.25,query_dop_domain=[1,2,4,8,16],learned_max_dop=16,
            design_provenance='Development-informed after numeric/ordered branch negative pilots. No latency input, repeated hashes or raw-data edits.',
            representativeness='Three customer-quality reporting questions on existing synthetic retail dimensions, not production prevalence or international address validation',
            hypothesis='Required string normalization/grouping with observed sales aggregation; no assumed heterogeneity headroom',
            population='All dated sales including anonymous/unmatched customers; aggregate by customer before current-dimension lookup, not transaction-time identity/address',
            address_semantics='Uppercase, trim and collapse ASCII-space runs; preserve NULL versus empty, street number/type, suite, city/state/country. Missing required fields or NULL suite are incomplete; empty suite is permitted. Incomplete customers isolated. Shared address is not duplicate-person identity; postcode disagreement is observed inconsistency, not correctness proof.',
            contact_semantics='Lowercase/trim for a limited email syntax/domain coverage screen, not RFC compliance or deliverability; NULL and empty separate. Original data unchanged.',
            count_semantics='Known customer IDs and matched current-customer IDs counted separately; NULL anonymous group contributes sales but zero customers. Report groups are not all real addresses; matched address links are customer links, not distinct address IDs.',
            split_semantics='Disjoint sales date windows with shared customer dimensions and grammar; no unseen-customer or unseen-template claim',
            contact_output=contact_output,
            contact_output_semantics='Domain detail or complete domain-size bands by observed known-customer count (1,2-9,10-99,100+); invalid/missing emails have not_applicable band andzero domaincount. Output change is not DOP speedup; compare identical output modes.',
            inactive_parameters=['group','top_k'],stage='Semantic fixtures, nonempty dimension matches and four fixed train prepass before grid; no automatic expansion')
        path.write_text(json.dumps(d,indent=2)+'\n')
    return d

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--database',required=True);p.add_argument('--contact-output',choices=CONTACT_OUTPUTS,default='domain');p.add_argument('--window-months',default='3,6');a=p.parse_args()
    print(json.dumps({'queries':generate_customer_quality(a.output_dir,a.database,contact_output=a.contact_output,window_months=tuple(int(x) for x in a.window_months.split(',')))['query_count']}))
