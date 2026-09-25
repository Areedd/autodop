import re
import sqlite3
import unittest
from dataclasses import replace
from generate_customer_quality_suite import render_customer_quality
from generate_integrity_operator_suite import date_key
from generate_mixed_operator_suite import Parameters


class CustomerQualitySemanticsTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.create_function('REGEXP_REPLACE',3,lambda s,p,r:None if s is None else re.sub(p,r,s))
        self.db.create_function('REGEXP',2,lambda s,p:None if s is None else int(re.search(p.replace('[:space:]',r'\s'),s) is not None))
        self.db.create_function('SPLIT_PART',3,lambda s,sep,n:None if s is None else s.split(sep)[n-1])
        self.db.executescript('''CREATE TABLE store_sales(ss_customer_sk,ss_sold_date_sk,ss_net_paid);
CREATE TABLE customer(c_customer_sk,c_current_addr_sk,c_email_address);
CREATE TABLE customer_address(ca_address_sk,ca_street_number,ca_street_name,ca_street_type,ca_suite_number,ca_city,ca_state,ca_zip,ca_country);''')
        day=date_key(1176)
        self.db.executemany('INSERT INTO store_sales VALUES (?,?,?)',[(i,day,10) for i in range(1,9)]+[(None,day,None),(1,day,10)])
        emails=[' A@Example.COM ','b@example.com',None,'','bad','space @x.com','u@other.org']
        self.db.executemany('INSERT INTO customer VALUES (?,?,?)',[(i,[1,2,3,4,5,5,None][i-1],emails[i-1]) for i in range(1,8)])
        self.db.executemany('INSERT INTO customer_address VALUES (?,?,?,?,?,?,?,?,?)',[
            (1,'10',' Main  Road ','St','','Town','CA','12345','USA'),
            (2,'10','MAIN ROAD','ST','','TOWN','CA','12345','USA'),
            (3,'10','Main Road','St','B','Town','CA','12345','USA'),
            (4,'10','Main Road','St','','Town','CA','99999','USA'),
            (5,'10','Main Road','St',None,'Town','CA','12345','USA')])
        self.p=Parameters('active_address_sharing',0,1176,3,3,'i_category',10)

    def tearDown(self):self.db.close()

    def rows(self,family):
        rows=self.db.execute(render_customer_quality(replace(self.p,family=family))).fetchall()
        self.assertEqual(sum(r['sales_lines'] for r in rows),10)
        self.assertEqual(sum(r['revenue'] or 0 for r in rows),90)
        self.assertEqual(sum(r['revenue_present'] for r in rows),9)
        self.assertEqual(sum(r['known_customers'] for r in rows),8)
        self.assertEqual(sum(r['matched_customers'] for r in rows),7)
        return rows

    def test_address_space_normalization_suite_and_missing_isolation(self):
        rows={r['quality']:r for r in self.rows('active_address_sharing')}
        self.assertEqual(rows['shared_address']['known_customers'],2)
        self.assertEqual(rows['single_customer']['known_customers'],2)
        self.assertEqual(rows['incomplete']['report_groups'],5)

    def test_postal_disagreement_does_not_merge_suites(self):
        rows={r['quality']:r for r in self.rows('active_postal_consistency')}
        self.assertEqual(rows['postcode_disagreement']['known_customers'],3)
        self.assertEqual(rows['one_postcode']['known_customers'],1)
        self.assertEqual(rows['incomplete']['known_customers'],4)

    def test_email_domain_null_empty_invalid_and_missing_customer(self):
        rows=self.rows('contact_domain_sales')
        domain=next(r for r in rows if r['email_domain']=='example.com')
        self.assertEqual((domain['known_customers'],domain['revenue']),(2,30))
        counts={r['quality']:r['known_customers'] for r in rows if r['quality']!='syntax_pass'}
        self.assertEqual(counts,{'null_email':1,'empty_email':1,'syntax_fail':2,'unmatched_customer':1})


    def test_domain_size_bands_preserve_all_metrics(self):
        p=replace(self.p,family='contact_domain_sales')
        detailed=self.rows('contact_domain_sales')
        bands=self.db.execute(render_customer_quality(p,contact_output='size_bands')).fetchall()
        for metric in ('known_customers','matched_customers','sales_lines','revenue','revenue_present'):
            self.assertEqual(sum(r[metric] or 0 for r in detailed),sum(r[metric] or 0 for r in bands))
        valid={r['domain_size_band']:r for r in bands if r['quality']=='syntax_pass'}
        self.assertEqual((valid['2_to_9']['domains'],valid['2_to_9']['known_customers'],valid['2_to_9']['revenue']),(1,2,30))
        self.assertEqual((valid['singleton']['domains'],valid['singleton']['known_customers']),(1,1))
        self.assertTrue(all(r['domains']==0 and r['domain_size_band']=='not_applicable' for r in bands if r['quality']!='syntax_pass'))
        with self.assertRaises(ValueError):render_customer_quality(p,contact_output='invalid')


    def test_annual_window_includes_late_year_but_excludes_next_year(self):
        late=date_key(1187)
        end=date_key(1188)
        self.db.executemany('INSERT INTO store_sales VALUES (?,?,?)',[(1,late,77),(1,end,999)])
        annual=replace(self.p,months=12,history_months=12)
        rows=self.db.execute(render_customer_quality(annual)).fetchall()
        self.assertEqual(sum(r['sales_lines'] for r in rows),11)
        self.assertEqual(sum(r['revenue'] or 0 for r in rows),167)
        with self.assertRaises(ValueError):render_customer_quality(replace(self.p,months=9,history_months=9))


if __name__=='__main__':unittest.main()
