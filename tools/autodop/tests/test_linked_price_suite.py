import sqlite3
import unittest
from dataclasses import replace
from generate_integrity_operator_suite import date_key
from generate_linked_price_suite import render_linked_price
from generate_mixed_operator_suite import Parameters


class LinkedPriceSemanticsTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE store_sales(ss_item_sk,ss_sold_date_sk,ss_quantity,ss_net_paid,ss_net_profit);
CREATE TABLE catalog_sales(cs_item_sk,cs_sold_date_sk,cs_quantity,cs_net_paid);
CREATE TABLE store_returns(sr_item_sk,sr_returned_date_sk,sr_return_quantity,sr_return_amt);
CREATE TABLE inventory(inv_item_sk,inv_warehouse_sk,inv_date_sk,inv_quantity_on_hand);
CREATE TABLE date_dim(d_date_sk,d_month_seq);
CREATE TABLE item(i_item_sk,i_category,i_class,i_brand);''')
        d=date_key(1176);self.day=d
        self.db.executemany('INSERT INTO date_dim VALUES (?,1176)',[(d,),(d+7,)])
        self.db.execute("INSERT INTO item VALUES (1,'A','B','C')")
        self.db.executemany('INSERT INTO store_sales VALUES (?,?,?,?,?)',[(1,d,1,10,1),(1,d,1,20,None),(1,d,1,30,3),(2,d,1,40,4),(1,d,0,999,99)])
        self.p=Parameters('price_inventory_profile',0,1176,3,3,'i_category',10)

    def tearDown(self):
        self.db.close()

    def report(self,family):
        rows=self.db.execute(render_linked_price(replace(self.p,family=family))).fetchall()
        self.assertEqual(sum(r['sales_lines'] for r in rows),4)
        self.assertEqual(sum(r['revenue'] for r in rows),100)
        self.assertEqual(sum(r['profit_present'] for r in rows),3)
        self.assertEqual(sum(r['product_months'] for r in rows),2)
        self.assertEqual(sum(r['matched_product_months'] for r in rows),1)
        return next(r for r in rows if r['i_category']=='A'),next(r for r in rows if r['i_category'] is None)

    def test_inventory_snapshot_aggregation_null_dates_no_fanout(self):
        self.db.executemany('INSERT INTO inventory VALUES (?,?,?,?)',[(1,1,self.day,10),(1,2,self.day,20),(1,1,self.day+7,None),(3,1,self.day,999)])
        a,b=self.report('price_inventory_profile')
        self.assertEqual((a['snapshot_dates'],a['valued_snapshot_dates'],a['average_observed_units'],a['unknown_quantities']),(2,1,30,1))
        self.assertEqual(a['upper_price_revenue'],30)
        self.assertIsNone(b['average_observed_units'])

    def test_returns_missing_amounts_and_unmatched_main(self):
        self.db.executemany('INSERT INTO store_returns VALUES (?,?,?,?)',[(1,self.day,1,5),(1,self.day,2,None),(3,self.day,9,999)])
        a,b=self.report('price_returns_profile')
        self.assertEqual((a['return_lines'],a['return_quantity'],a['return_amount'],a['return_amount_present']),(2,3,5,1))
        self.assertIsNone(b['return_lines'])

    def test_other_channel_duplicate_lines_not_repeated_per_price_band(self):
        self.db.executemany('INSERT INTO catalog_sales VALUES (?,?,?,?)',[(1,self.day,2,35),(1,self.day,None,5),(3,self.day,1,999)])
        a,b=self.report('price_channel_profile')
        self.assertEqual((a['other_lines'],a['other_quantity'],a['other_quantity_present'],a['other_amount']),(2,2,1,40))
        self.assertIsNone(b['other_amount'])


if __name__=='__main__':unittest.main()
