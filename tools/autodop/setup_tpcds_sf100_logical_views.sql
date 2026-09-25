-- Repair the positional-load column permutation without copying the SF100 data.
-- The six sales/return tables were created with key columns moved to the front,
-- while their .dat files were loaded in standard TPC-DS order.  These views
-- restore the standard logical column names used by the benchmark queries.

CREATE DATABASE IF NOT EXISTS tpcds_sf100_logical;

DROP VIEW IF EXISTS tpcds_sf100_logical.store_sales;
CREATE VIEW tpcds_sf100_logical.store_sales AS
SELECT ss_item_sk AS ss_sold_date_sk,
       ss_ticket_number AS ss_sold_time_sk,
       ss_sold_date_sk AS ss_item_sk,
       ss_sold_time_sk AS ss_customer_sk,
       ss_customer_sk AS ss_cdemo_sk,
       ss_cdemo_sk AS ss_hdemo_sk,
       ss_hdemo_sk AS ss_addr_sk,
       ss_addr_sk AS ss_store_sk,
       ss_store_sk AS ss_promo_sk,
       ss_promo_sk AS ss_ticket_number,
       ss_quantity,
       ss_wholesale_cost,
       ss_list_price,
       ss_sales_price,
       ss_ext_discount_amt,
       ss_ext_sales_price,
       ss_ext_wholesale_cost,
       ss_ext_list_price,
       ss_ext_tax,
       ss_coupon_amt,
       ss_net_paid,
       ss_net_paid_inc_tax,
       ss_net_profit
FROM tpcds_sf100.store_sales;

DROP VIEW IF EXISTS tpcds_sf100_logical.store_returns;
CREATE VIEW tpcds_sf100_logical.store_returns AS
SELECT sr_item_sk AS sr_returned_date_sk,
       sr_ticket_number AS sr_return_time_sk,
       sr_returned_date_sk AS sr_item_sk,
       sr_return_time_sk AS sr_customer_sk,
       sr_customer_sk AS sr_cdemo_sk,
       sr_cdemo_sk AS sr_hdemo_sk,
       sr_hdemo_sk AS sr_addr_sk,
       sr_addr_sk AS sr_store_sk,
       sr_store_sk AS sr_reason_sk,
       sr_reason_sk AS sr_ticket_number,
       sr_return_quantity,
       sr_return_amt,
       sr_return_tax,
       sr_return_amt_inc_tax,
       sr_fee,
       sr_return_ship_cost,
       sr_refunded_cash,
       sr_reversed_charge,
       sr_store_credit,
       sr_net_loss
FROM tpcds_sf100.store_returns;

DROP VIEW IF EXISTS tpcds_sf100_logical.catalog_sales;
CREATE VIEW tpcds_sf100_logical.catalog_sales AS
SELECT cs_item_sk AS cs_sold_date_sk,
       cs_order_number AS cs_sold_time_sk,
       cs_sold_date_sk AS cs_ship_date_sk,
       cs_sold_time_sk AS cs_bill_customer_sk,
       cs_ship_date_sk AS cs_bill_cdemo_sk,
       cs_bill_customer_sk AS cs_bill_hdemo_sk,
       cs_bill_cdemo_sk AS cs_bill_addr_sk,
       cs_bill_hdemo_sk AS cs_ship_customer_sk,
       cs_bill_addr_sk AS cs_ship_cdemo_sk,
       cs_ship_customer_sk AS cs_ship_hdemo_sk,
       cs_ship_cdemo_sk AS cs_ship_addr_sk,
       cs_ship_hdemo_sk AS cs_call_center_sk,
       cs_ship_addr_sk AS cs_catalog_page_sk,
       cs_call_center_sk AS cs_ship_mode_sk,
       cs_catalog_page_sk AS cs_warehouse_sk,
       cs_ship_mode_sk AS cs_item_sk,
       cs_warehouse_sk AS cs_promo_sk,
       cs_promo_sk AS cs_order_number,
       cs_quantity,
       cs_wholesale_cost,
       cs_list_price,
       cs_sales_price,
       cs_ext_discount_amt,
       cs_ext_sales_price,
       cs_ext_wholesale_cost,
       cs_ext_list_price,
       cs_ext_tax,
       cs_coupon_amt,
       cs_ext_ship_cost,
       cs_net_paid,
       cs_net_paid_inc_tax,
       cs_net_paid_inc_ship,
       cs_net_paid_inc_ship_tax,
       cs_net_profit
FROM tpcds_sf100.catalog_sales;

DROP VIEW IF EXISTS tpcds_sf100_logical.catalog_returns;
CREATE VIEW tpcds_sf100_logical.catalog_returns AS
SELECT cr_item_sk AS cr_returned_date_sk,
       cr_order_number AS cr_returned_time_sk,
       cr_returned_date_sk AS cr_item_sk,
       cr_returned_time_sk AS cr_refunded_customer_sk,
       cr_refunded_customer_sk AS cr_refunded_cdemo_sk,
       cr_refunded_cdemo_sk AS cr_refunded_hdemo_sk,
       cr_refunded_hdemo_sk AS cr_refunded_addr_sk,
       cr_refunded_addr_sk AS cr_returning_customer_sk,
       cr_returning_customer_sk AS cr_returning_cdemo_sk,
       cr_returning_cdemo_sk AS cr_returning_hdemo_sk,
       cr_returning_hdemo_sk AS cr_returning_addr_sk,
       cr_returning_addr_sk AS cr_call_center_sk,
       cr_call_center_sk AS cr_catalog_page_sk,
       cr_catalog_page_sk AS cr_ship_mode_sk,
       cr_ship_mode_sk AS cr_warehouse_sk,
       cr_warehouse_sk AS cr_reason_sk,
       cr_reason_sk AS cr_order_number,
       cr_return_quantity,
       cr_return_amount,
       cr_return_tax,
       cr_return_amt_inc_tax,
       cr_fee,
       cr_return_ship_cost,
       cr_refunded_cash,
       cr_reversed_charge,
       cr_store_credit,
       cr_net_loss
FROM tpcds_sf100.catalog_returns;

DROP VIEW IF EXISTS tpcds_sf100_logical.web_sales;
CREATE VIEW tpcds_sf100_logical.web_sales AS
SELECT ws_item_sk AS ws_sold_date_sk,
       ws_order_number AS ws_sold_time_sk,
       ws_sold_date_sk AS ws_ship_date_sk,
       ws_sold_time_sk AS ws_item_sk,
       ws_ship_date_sk AS ws_bill_customer_sk,
       ws_bill_customer_sk AS ws_bill_cdemo_sk,
       ws_bill_cdemo_sk AS ws_bill_hdemo_sk,
       ws_bill_hdemo_sk AS ws_bill_addr_sk,
       ws_bill_addr_sk AS ws_ship_customer_sk,
       ws_ship_customer_sk AS ws_ship_cdemo_sk,
       ws_ship_cdemo_sk AS ws_ship_hdemo_sk,
       ws_ship_hdemo_sk AS ws_ship_addr_sk,
       ws_ship_addr_sk AS ws_web_page_sk,
       ws_web_page_sk AS ws_web_site_sk,
       ws_web_site_sk AS ws_ship_mode_sk,
       ws_ship_mode_sk AS ws_warehouse_sk,
       ws_warehouse_sk AS ws_promo_sk,
       ws_promo_sk AS ws_order_number,
       ws_quantity,
       ws_wholesale_cost,
       ws_list_price,
       ws_sales_price,
       ws_ext_discount_amt,
       ws_ext_sales_price,
       ws_ext_wholesale_cost,
       ws_ext_list_price,
       ws_ext_tax,
       ws_coupon_amt,
       ws_ext_ship_cost,
       ws_net_paid,
       ws_net_paid_inc_tax,
       ws_net_paid_inc_ship,
       ws_net_paid_inc_ship_tax,
       ws_net_profit
FROM tpcds_sf100.web_sales;

DROP VIEW IF EXISTS tpcds_sf100_logical.web_returns;
CREATE VIEW tpcds_sf100_logical.web_returns AS
SELECT wr_item_sk AS wr_returned_date_sk,
       wr_order_number AS wr_returned_time_sk,
       wr_returned_date_sk AS wr_item_sk,
       wr_returned_time_sk AS wr_refunded_customer_sk,
       wr_refunded_customer_sk AS wr_refunded_cdemo_sk,
       wr_refunded_cdemo_sk AS wr_refunded_hdemo_sk,
       wr_refunded_hdemo_sk AS wr_refunded_addr_sk,
       wr_refunded_addr_sk AS wr_returning_customer_sk,
       wr_returning_customer_sk AS wr_returning_cdemo_sk,
       wr_returning_cdemo_sk AS wr_returning_hdemo_sk,
       wr_returning_hdemo_sk AS wr_returning_addr_sk,
       wr_returning_addr_sk AS wr_web_page_sk,
       wr_web_page_sk AS wr_reason_sk,
       wr_reason_sk AS wr_order_number,
       wr_return_quantity,
       wr_return_amt,
       wr_return_tax,
       wr_return_amt_inc_tax,
       wr_fee,
       wr_return_ship_cost,
       wr_refunded_cash,
       wr_reversed_charge,
       wr_account_credit,
       wr_net_loss
FROM tpcds_sf100.web_returns;

DROP VIEW IF EXISTS tpcds_sf100_logical.call_center;
CREATE VIEW tpcds_sf100_logical.call_center AS SELECT * FROM tpcds_sf100.call_center;
DROP VIEW IF EXISTS tpcds_sf100_logical.catalog_page;
CREATE VIEW tpcds_sf100_logical.catalog_page AS SELECT * FROM tpcds_sf100.catalog_page;
DROP VIEW IF EXISTS tpcds_sf100_logical.customer;
CREATE VIEW tpcds_sf100_logical.customer AS SELECT * FROM tpcds_sf100.customer;
DROP VIEW IF EXISTS tpcds_sf100_logical.customer_address;
CREATE VIEW tpcds_sf100_logical.customer_address AS SELECT * FROM tpcds_sf100.customer_address;
DROP VIEW IF EXISTS tpcds_sf100_logical.customer_demographics;
CREATE VIEW tpcds_sf100_logical.customer_demographics AS SELECT * FROM tpcds_sf100.customer_demographics;
DROP VIEW IF EXISTS tpcds_sf100_logical.date_dim;
CREATE VIEW tpcds_sf100_logical.date_dim AS SELECT * FROM tpcds_sf100.date_dim;
DROP VIEW IF EXISTS tpcds_sf100_logical.household_demographics;
CREATE VIEW tpcds_sf100_logical.household_demographics AS SELECT * FROM tpcds_sf100.household_demographics;
DROP VIEW IF EXISTS tpcds_sf100_logical.income_band;
CREATE VIEW tpcds_sf100_logical.income_band AS SELECT * FROM tpcds_sf100.income_band;
DROP VIEW IF EXISTS tpcds_sf100_logical.inventory;
CREATE VIEW tpcds_sf100_logical.inventory AS SELECT * FROM tpcds_sf100.inventory;
DROP VIEW IF EXISTS tpcds_sf100_logical.item;
CREATE VIEW tpcds_sf100_logical.item AS SELECT * FROM tpcds_sf100.item;
DROP VIEW IF EXISTS tpcds_sf100_logical.promotion;
CREATE VIEW tpcds_sf100_logical.promotion AS SELECT * FROM tpcds_sf100.promotion;
DROP VIEW IF EXISTS tpcds_sf100_logical.reason;
CREATE VIEW tpcds_sf100_logical.reason AS SELECT * FROM tpcds_sf100.reason;
DROP VIEW IF EXISTS tpcds_sf100_logical.ship_mode;
CREATE VIEW tpcds_sf100_logical.ship_mode AS SELECT * FROM tpcds_sf100.ship_mode;
DROP VIEW IF EXISTS tpcds_sf100_logical.store;
CREATE VIEW tpcds_sf100_logical.store AS SELECT * FROM tpcds_sf100.store;
DROP VIEW IF EXISTS tpcds_sf100_logical.time_dim;
CREATE VIEW tpcds_sf100_logical.time_dim AS SELECT * FROM tpcds_sf100.time_dim;
DROP VIEW IF EXISTS tpcds_sf100_logical.warehouse;
CREATE VIEW tpcds_sf100_logical.warehouse AS SELECT * FROM tpcds_sf100.warehouse;
DROP VIEW IF EXISTS tpcds_sf100_logical.web_page;
CREATE VIEW tpcds_sf100_logical.web_page AS SELECT * FROM tpcds_sf100.web_page;
DROP VIEW IF EXISTS tpcds_sf100_logical.web_site;
CREATE VIEW tpcds_sf100_logical.web_site AS SELECT * FROM tpcds_sf100.web_site;
