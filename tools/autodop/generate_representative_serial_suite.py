#!/usr/bin/env python3

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Channel:
    name: str
    table: str
    prefix: str
    order: str
    item: str
    customer: str
    sold_date: str
    quantity: str
    sales_price: str
    ext_sales: str
    discount: str
    tax: str
    coupon: str
    net_paid: str
    profit: str
    promo: str
    returns: str
    return_order: str
    return_item: str
    return_amount: str


CHANNELS = (
    Channel("store", "store_sales", "ss", "ss_ticket_number", "ss_item_sk",
            "ss_customer_sk", "ss_sold_date_sk", "ss_quantity",
            "ss_sales_price", "ss_ext_sales_price", "ss_ext_discount_amt",
            "ss_ext_tax", "ss_coupon_amt", "ss_net_paid", "ss_net_profit",
            "ss_promo_sk", "store_returns", "sr_ticket_number", "sr_item_sk",
            "sr_return_amt"),
    Channel("catalog", "catalog_sales", "cs", "cs_order_number", "cs_item_sk",
            "cs_bill_customer_sk", "cs_sold_date_sk", "cs_quantity",
            "cs_sales_price", "cs_ext_sales_price", "cs_ext_discount_amt",
            "cs_ext_tax", "cs_coupon_amt", "cs_net_paid", "cs_net_profit",
            "cs_promo_sk", "catalog_returns", "cr_order_number", "cr_item_sk",
            "cr_return_amount"),
    Channel("web", "web_sales", "ws", "ws_order_number", "ws_item_sk",
            "ws_bill_customer_sk", "ws_sold_date_sk", "ws_quantity",
            "ws_sales_price", "ws_ext_sales_price", "ws_ext_discount_amt",
            "ws_ext_tax", "ws_coupon_amt", "ws_net_paid", "ws_net_profit",
            "ws_promo_sk", "web_returns", "wr_order_number", "wr_item_sk",
            "wr_return_amt"),
)


def sql(text):
    return "\n".join(line.rstrip() for line in text.strip().splitlines()) + ";\n"


def add(queries, query_id, task, text):
    queries.append({"query_id": query_id, "task": task, "sql": sql(text)})


def channel_queries(c):
    q = []
    t = c.table
    add(q, f"{c.name}_finance_kpis", "financial_reporting", f"""
SELECT COUNT(*) AS line_count,
       ROUND(SUM({c.ext_sales}), 2) AS gross_sales,
       ROUND(SUM({c.net_paid}), 2) AS net_revenue,
       ROUND(SUM({c.profit}), 2) AS net_profit,
       ROUND(SUM({c.tax}), 2) AS tax_collected
FROM {t}""")
    add(q, f"{c.name}_margin_bands", "margin_analysis", f"""
SELECT CASE WHEN {c.profit} < 0 THEN 'loss'
            WHEN {c.profit} < {c.net_paid} * 0.1 THEN 'low'
            WHEN {c.profit} < {c.net_paid} * 0.3 THEN 'standard'
            ELSE 'high' END AS margin_band,
       COUNT(*) AS line_count, ROUND(SUM({c.profit}), 2) AS profit
FROM {t}
GROUP BY margin_band""")
    add(q, f"{c.name}_promotion_lift", "campaign_measurement", f"""
SELECT {c.promo} IS NOT NULL AS promoted,
       COUNT(*) AS line_count,
       ROUND(AVG({c.discount}), 2) AS avg_discount,
       ROUND(AVG({c.net_paid}), 2) AS avg_paid,
       ROUND(SUM({c.profit}), 2) AS profit
FROM {t}
GROUP BY promoted""")
    add(q, f"{c.name}_quality_rules", "data_quality", f"""
SELECT SUM({c.customer} IS NULL) AS missing_customer,
       SUM({c.quantity} <= 0) AS invalid_quantity,
       SUM({c.sales_price} > {c.ext_sales}) AS price_mismatch,
       SUM({c.net_paid} < 0) AS negative_payment,
       SUM({c.profit} < -{c.net_paid}) AS extreme_loss
FROM {t}""")
    add(q, f"{c.name}_price_dispersion", "statistical_monitoring", f"""
SELECT ROUND(AVG({c.sales_price}), 2) AS mean_price,
       ROUND(STDDEV_POP({c.sales_price}), 2) AS price_stddev,
       ROUND(VARIANCE({c.quantity}), 2) AS quantity_variance,
       ROUND(STDDEV_POP({c.discount}), 2) AS discount_stddev
FROM {t}""")
    add(q, f"{c.name}_revenue_percentiles", "distribution_analysis", f"""
SELECT ROUND(PERCENTILE_APPROX(CAST({c.net_paid} AS DOUBLE), 0.50), 2) AS p50,
       ROUND(PERCENTILE_APPROX(CAST({c.net_paid} AS DOUBLE), 0.90), 2) AS p90,
       ROUND(PERCENTILE_APPROX(CAST({c.net_paid} AS DOUBLE), 0.99), 2) AS p99
FROM {t}""")
    add(q, f"{c.name}_relationship_stats", "statistical_monitoring", f"""
SELECT ROUND(CORR({c.quantity}, {c.net_paid}), 4) AS quantity_revenue_corr,
       ROUND(COVAR_POP({c.sales_price}, {c.profit}), 4) AS price_profit_covar,
       ROUND(STDDEV_SAMP({c.coupon}), 2) AS coupon_stddev
FROM {t}""")
    add(q, f"{c.name}_monthly_trend", "time_series_reporting", f"""
WITH monthly AS (
    SELECT d.d_year, d.d_moy, SUM(s.{c.net_paid}) AS revenue
    FROM {t} s JOIN date_dim d ON s.{c.sold_date} = d.d_date_sk
    GROUP BY d.d_year, d.d_moy
)
SELECT COUNT(*) AS months, ROUND(AVG(revenue), 2) AS average_month,
       ROUND(STDDEV_POP(revenue), 2) AS monthly_volatility
FROM monthly""")
    add(q, f"{c.name}_category_portfolio", "merchandising", f"""
WITH category_sales AS (
    SELECT i.i_category, SUM(s.{c.net_paid}) AS revenue,
           SUM(s.{c.quantity}) AS units
    FROM {t} s JOIN item i ON s.{c.item} = i.i_item_sk
    GROUP BY i.i_category
)
SELECT COUNT(*) AS categories, ROUND(SUM(revenue), 2) AS revenue,
       ROUND(MAX(revenue) / NULLIF(SUM(revenue), 0), 4) AS top_share
FROM category_sales""")
    add(q, f"{c.name}_customer_value", "customer_segmentation", f"""
WITH customer_value AS (
    SELECT {c.customer} AS customer_sk, COUNT(DISTINCT {c.order}) AS orders,
           SUM({c.net_paid}) AS revenue
    FROM {t}
    WHERE {c.customer} IS NOT NULL
    GROUP BY {c.customer}
)
SELECT COUNT(*) AS customers, ROUND(AVG(orders), 2) AS avg_orders,
       ROUND(AVG(revenue), 2) AS avg_revenue,
       ROUND(PERCENTILE_APPROX(CAST(revenue AS DOUBLE), 0.9), 2) AS p90_revenue
FROM customer_value""")
    add(q, f"{c.name}_top_item_rank", "product_ranking", f"""
WITH item_sales AS (
    SELECT {c.item} AS item_sk, SUM({c.net_paid}) AS revenue
    FROM {t} GROUP BY {c.item}
), ranked AS (
    SELECT revenue, DENSE_RANK() OVER (ORDER BY revenue DESC) AS position
    FROM item_sales
)
SELECT COUNT(*) AS top_items, ROUND(SUM(revenue), 2) AS top_revenue
FROM ranked WHERE position <= 100""")
    add(q, f"{c.name}_rolling_months", "time_series_reporting", f"""
WITH monthly AS (
    SELECT d.d_month_seq AS month_id, SUM(s.{c.net_paid}) AS revenue
    FROM {t} s JOIN date_dim d ON s.{c.sold_date} = d.d_date_sk
    GROUP BY d.d_month_seq
), smoothed AS (
    SELECT revenue, AVG(revenue) OVER (
        ORDER BY month_id ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS moving_avg
    FROM monthly
)
SELECT ROUND(AVG(ABS(revenue - moving_avg)), 2) AS mean_deviation
FROM smoothed""")
    add(q, f"{c.name}_calendar_rollup", "multilevel_reporting", f"""
SELECT d.d_year, d.d_qoy, ROUND(SUM(s.{c.net_paid}), 2) AS revenue
FROM {t} s JOIN date_dim d ON s.{c.sold_date} = d.d_date_sk
GROUP BY ROLLUP(d.d_year, d.d_qoy)""")
    add(q, f"{c.name}_market_reach", "cardinality_estimation", f"""
SELECT APPROX_COUNT_DISTINCT({c.customer}) AS customers,
       APPROX_COUNT_DISTINCT({c.item}) AS products,
       APPROX_COUNT_DISTINCT({c.order}) AS orders
FROM {t}""")
    add(q, f"{c.name}_basket_profile", "order_analysis", f"""
WITH baskets AS (
    SELECT {c.order} AS order_id, COUNT(*) AS lines,
           SUM({c.quantity}) AS units, SUM({c.net_paid}) AS revenue
    FROM {t} GROUP BY {c.order}
)
SELECT COUNT(*) AS orders, ROUND(AVG(lines), 2) AS avg_lines,
       ROUND(STDDEV_POP(units), 2) AS unit_variability,
       ROUND(AVG(revenue), 2) AS avg_order_value
FROM baskets""")
    add(q, f"{c.name}_discount_effectiveness", "pricing_analysis", f"""
SELECT CASE WHEN {c.discount} = 0 THEN 'none'
            WHEN {c.discount} < {c.ext_sales} * 0.1 THEN 'small'
            WHEN {c.discount} < {c.ext_sales} * 0.25 THEN 'medium'
            ELSE 'large' END AS discount_band,
       COUNT(*) AS lines, ROUND(AVG({c.quantity}), 2) AS units,
       ROUND(SUM({c.net_paid}), 2) AS revenue
FROM {t}
GROUP BY discount_band""")
    add(q, f"{c.name}_return_reconciliation", "returns_reconciliation", f"""
WITH sold AS (
    SELECT {c.order} AS order_id, {c.item} AS item_sk, SUM({c.net_paid}) AS paid
    FROM {t} GROUP BY {c.order}, {c.item}
), returned AS (
    SELECT {c.return_order} AS order_id, {c.return_item} AS item_sk,
           SUM({c.return_amount}) AS refunded
    FROM {c.returns} GROUP BY {c.return_order}, {c.return_item}
)
SELECT COUNT(*) AS sold_items, COUNT(returned.order_id) AS returned_items,
       ROUND(SUM(paid), 2) AS paid, ROUND(SUM(IFNULL(refunded, 0)), 2) AS refunded
FROM sold LEFT JOIN returned USING (order_id, item_sk)""")
    add(q, f"{c.name}_never_returned", "returns_reconciliation", f"""
SELECT COUNT(*) AS retained_lines, ROUND(SUM(s.{c.net_paid}), 2) AS retained_revenue
FROM {t} s
WHERE NOT EXISTS (
    SELECT 1 FROM {c.returns} r
    WHERE r.{c.return_order} = s.{c.order}
      AND r.{c.return_item} = s.{c.item}
)""")
    add(q, f"{c.name}_customer_name_quality", "customer_data_stewardship", f"""
SELECT LOWER(TRIM(c.c_preferred_cust_flag)) AS preference,
       SUM(c.c_email_address IS NULL OR LOCATE('@', c.c_email_address) = 0) AS bad_email,
       SUM(LENGTH(TRIM(c.c_first_name)) = 0) AS missing_first_name,
       ROUND(SUM(s.{c.net_paid}), 2) AS associated_revenue
FROM {t} s JOIN customer c ON s.{c.customer} = c.c_customer_sk
GROUP BY preference""")
    add(q, f"{c.name}_product_description_quality", "catalog_data_stewardship", f"""
SELECT i.i_category,
       SUM(i.i_item_desc IS NULL OR LENGTH(TRIM(i.i_item_desc)) < 10) AS short_descriptions,
       APPROX_COUNT_DISTINCT(LOWER(TRIM(i.i_product_name))) AS normalized_products,
       ROUND(SUM(s.{c.net_paid}), 2) AS revenue
FROM {t} s JOIN item i ON s.{c.item} = i.i_item_sk
GROUP BY i.i_category""")
    return q


def cross_channel_queries():
    q = []
    add(q, "channel_monthly_mix", "omnichannel_reporting", """
WITH sales AS (
    SELECT 'store' channel, ss_sold_date_sk date_sk, ss_net_paid revenue FROM store_sales
    UNION ALL
    SELECT 'catalog', cs_sold_date_sk, cs_net_paid FROM catalog_sales
    UNION ALL
    SELECT 'web', ws_sold_date_sk, ws_net_paid FROM web_sales
)
SELECT channel, d.d_year, ROUND(SUM(revenue), 2) AS revenue
FROM sales JOIN date_dim d ON sales.date_sk = d.d_date_sk
GROUP BY channel, d.d_year""")
    add(q, "channel_customer_overlap", "omnichannel_customers", """
WITH web_customers AS (
    SELECT DISTINCT ws_bill_customer_sk customer_sk FROM web_sales
), catalog_customers AS (
    SELECT DISTINCT cs_bill_customer_sk customer_sk FROM catalog_sales
)
SELECT COUNT(*) AS shared_customers
FROM web_customers JOIN catalog_customers USING (customer_sk)""")
    add(q, "channel_exclusive_customers", "omnichannel_customers", """
SELECT COUNT(*) AS store_only_customers
FROM (
    SELECT ss_customer_sk customer_sk FROM store_sales
    EXCEPT
    SELECT cs_bill_customer_sk FROM catalog_sales
) exclusive""")
    add(q, "channel_item_coverage", "omnichannel_merchandising", """
WITH coverage AS (
    SELECT ss_item_sk item_sk, 1 store_seen, 0 catalog_seen, 0 web_seen FROM store_sales
    UNION ALL
    SELECT cs_item_sk, 0, 1, 0 FROM catalog_sales
    UNION ALL
    SELECT ws_item_sk, 0, 0, 1 FROM web_sales
)
SELECT SUM(store_seen > 0 AND catalog_seen > 0 AND web_seen > 0) AS all_channel_items
FROM (
    SELECT item_sk, MAX(store_seen) store_seen,
           MAX(catalog_seen) catalog_seen, MAX(web_seen) web_seen
    FROM coverage GROUP BY item_sk
) item_coverage""")
    add(q, "channel_revenue_share", "omnichannel_reporting", """
WITH totals AS (
    SELECT 'store' channel, SUM(ss_net_paid) revenue FROM store_sales
    UNION ALL
    SELECT 'catalog', SUM(cs_net_paid) FROM catalog_sales
    UNION ALL
    SELECT 'web', SUM(ws_net_paid) FROM web_sales
)
SELECT channel, ROUND(revenue, 2) AS revenue,
       ROUND(revenue / SUM(revenue) OVER (), 4) AS revenue_share
FROM totals""")
    add(q, "channel_customer_value_gap", "omnichannel_customers", """
WITH store_value AS (
    SELECT ss_customer_sk customer_sk, SUM(ss_net_paid) revenue
    FROM store_sales GROUP BY ss_customer_sk
), web_value AS (
    SELECT ws_bill_customer_sk customer_sk, SUM(ws_net_paid) revenue
    FROM web_sales GROUP BY ws_bill_customer_sk
)
SELECT COUNT(*) AS customers,
       ROUND(AVG(ABS(store_value.revenue - web_value.revenue)), 2) AS avg_gap
FROM store_value JOIN web_value USING (customer_sk)""")
    add(q, "channel_return_rates", "returns_reconciliation", """
WITH sold AS (
    SELECT 'store' channel, COUNT(*) lines FROM store_sales
    UNION ALL SELECT 'catalog', COUNT(*) FROM catalog_sales
    UNION ALL SELECT 'web', COUNT(*) FROM web_sales
), returned AS (
    SELECT 'store' channel, COUNT(*) lines FROM store_returns
    UNION ALL SELECT 'catalog', COUNT(*) FROM catalog_returns
    UNION ALL SELECT 'web', COUNT(*) FROM web_returns
)
SELECT sold.channel, ROUND(returned.lines / sold.lines, 4) AS line_return_rate
FROM sold JOIN returned USING (channel)""")
    add(q, "channel_year_over_year", "time_series_reporting", """
WITH yearly AS (
    SELECT d.d_year, SUM(ss.ss_net_paid) revenue
    FROM store_sales ss JOIN date_dim d ON ss.ss_sold_date_sk = d.d_date_sk
    GROUP BY d.d_year
), compared AS (
    SELECT d_year, revenue, LAG(revenue) OVER (ORDER BY d_year) previous_revenue
    FROM yearly
)
SELECT d_year, ROUND((revenue - previous_revenue) / NULLIF(previous_revenue, 0), 4) growth
FROM compared WHERE previous_revenue IS NOT NULL""")
    return q


def structurally_diverse_queries():
    q = []
    add(q, "store_customer_geography", "geographic_segmentation", """
SELECT ca.ca_country, ca.ca_state, COUNT(DISTINCT ss.ss_customer_sk) AS customers,
       ROUND(SUM(ss.ss_net_paid), 2) AS revenue
FROM store_sales ss
JOIN customer c ON ss.ss_customer_sk = c.c_customer_sk
JOIN customer_address ca ON c.c_current_addr_sk = ca.ca_address_sk
GROUP BY ca.ca_country, ca.ca_state""")
    add(q, "store_education_segment", "demographic_segmentation", """
SELECT cd.cd_education_status, cd.cd_credit_rating,
       COUNT(*) AS lines, ROUND(AVG(ss.ss_net_paid), 2) AS avg_paid
FROM store_sales ss
JOIN customer_demographics cd ON ss.ss_cdemo_sk = cd.cd_demo_sk
GROUP BY cd.cd_education_status, cd.cd_credit_rating
HAVING COUNT(*) > 1000""")
    add(q, "store_household_segment", "household_segmentation", """
SELECT hd.hd_buy_potential, hd.hd_vehicle_count,
       APPROX_COUNT_DISTINCT(ss.ss_customer_sk) AS customers,
       ROUND(SUM(ss.ss_net_profit), 2) AS profit
FROM store_sales ss
JOIN household_demographics hd ON ss.ss_hdemo_sk = hd.hd_demo_sk
GROUP BY hd.hd_buy_potential, hd.hd_vehicle_count""")
    add(q, "store_persona_cube", "demographic_segmentation", """
SELECT cd.cd_gender, cd.cd_marital_status, hd.hd_buy_potential,
       ROUND(SUM(ss.ss_net_paid), 2) AS revenue
FROM store_sales ss
JOIN customer_demographics cd ON ss.ss_cdemo_sk = cd.cd_demo_sk
JOIN household_demographics hd ON ss.ss_hdemo_sk = hd.hd_demo_sk
GROUP BY CUBE(cd.cd_gender, cd.cd_marital_status, hd.hd_buy_potential)""")
    add(q, "store_birth_cohorts", "customer_segmentation", """
SELECT FLOOR(c.c_birth_year / 10) * 10 AS birth_decade,
       c.c_preferred_cust_flag, COUNT(DISTINCT c.c_customer_sk) AS customers,
       ROUND(SUM(ss.ss_net_paid), 2) AS revenue
FROM store_sales ss JOIN customer c ON ss.ss_customer_sk = c.c_customer_sk
WHERE c.c_birth_year IS NOT NULL
GROUP BY birth_decade, c.c_preferred_cust_flag""")
    add(q, "store_state_topn", "geographic_segmentation", """
SELECT ca.ca_state, ROUND(SUM(ss.ss_net_paid), 2) AS revenue
FROM store_sales ss
JOIN customer c ON ss.ss_customer_sk = c.c_customer_sk
JOIN customer_address ca ON c.c_current_addr_sk = ca.ca_address_sk
GROUP BY ca.ca_state
ORDER BY revenue DESC
LIMIT 10""")
    add(q, "store_state_rank", "geographic_segmentation", """
WITH states AS (
    SELECT ca.ca_state, SUM(ss.ss_net_profit) profit
    FROM store_sales ss JOIN customer_address ca ON ss.ss_addr_sk = ca.ca_address_sk
    GROUP BY ca.ca_state
)
SELECT ca_state, ROUND(profit, 2) profit,
       DENSE_RANK() OVER (ORDER BY profit DESC) AS profit_rank
FROM states""")
    add(q, "store_state_running_share", "geographic_segmentation", """
WITH states AS (
    SELECT ca.ca_state, SUM(ss.ss_net_paid) revenue
    FROM store_sales ss JOIN customer_address ca ON ss.ss_addr_sk = ca.ca_address_sk
    GROUP BY ca.ca_state
)
SELECT ca_state, ROUND(revenue / SUM(revenue) OVER (), 4) share,
       ROUND(SUM(revenue) OVER (ORDER BY revenue DESC), 2) running_revenue
FROM states""")
    add(q, "store_state_quarter_lag", "geographic_time_series", """
WITH state_quarters AS (
    SELECT ca.ca_state, d.d_quarter_seq, SUM(ss.ss_net_paid) revenue
    FROM store_sales ss
    JOIN customer_address ca ON ss.ss_addr_sk = ca.ca_address_sk
    JOIN date_dim d ON ss.ss_sold_date_sk = d.d_date_sk
    GROUP BY ca.ca_state, d.d_quarter_seq
)
SELECT ca_state, d_quarter_seq, ROUND(revenue, 2) revenue,
       ROUND(revenue - LAG(revenue) OVER (
           PARTITION BY ca_state ORDER BY d_quarter_seq), 2) change_amount
FROM state_quarters""")
    add(q, "store_geo_grouping_sets", "multilevel_reporting", """
SELECT ca.ca_country, ca.ca_state, ca.ca_city,
       ROUND(SUM(ss.ss_net_paid), 2) AS revenue
FROM store_sales ss JOIN customer_address ca ON ss.ss_addr_sk = ca.ca_address_sk
GROUP BY GROUPING SETS (
    (ca.ca_country), (ca.ca_country, ca.ca_state),
    (ca.ca_country, ca.ca_state, ca.ca_city)
)""")
    add(q, "store_geo_rollup", "multilevel_reporting", """
SELECT ca.ca_country, ca.ca_state,
       APPROX_COUNT_DISTINCT(ss.ss_customer_sk) AS customers,
       ROUND(SUM(ss.ss_net_profit), 2) AS profit
FROM store_sales ss JOIN customer_address ca ON ss.ss_addr_sk = ca.ca_address_sk
GROUP BY ROLLUP(ca.ca_country, ca.ca_state)""")
    add(q, "store_valid_email_sales", "customer_data_stewardship", """
SELECT COUNT(*) AS valid_email_lines, ROUND(SUM(ss.ss_net_paid), 2) AS revenue
FROM store_sales ss
WHERE ss.ss_customer_sk IN (
    SELECT c_customer_sk FROM customer
    WHERE c_email_address IS NOT NULL AND LOCATE('@', c_email_address) > 1
)""")
    add(q, "store_invalid_email_sales", "customer_data_stewardship", """
SELECT COUNT(*) AS uncontactable_lines, ROUND(SUM(ss.ss_net_paid), 2) AS revenue
FROM store_sales ss
WHERE EXISTS (
    SELECT 1 FROM customer c
    WHERE c.c_customer_sk = ss.ss_customer_sk
      AND (c.c_email_address IS NULL OR LOCATE('@', c.c_email_address) = 0)
)""")
    add(q, "store_unresolved_customer", "data_quality", """
SELECT COUNT(*) AS lines, COUNT(c.c_customer_sk) AS resolved,
       ROUND(SUM(IFNULL(ss.ss_net_paid, 0)), 2) AS revenue
FROM store_sales ss
LEFT JOIN customer c ON ss.ss_customer_sk = c.c_customer_sk""")
    add(q, "store_preaggregated_customer", "customer_segmentation", """
WITH value AS (
    SELECT ss_customer_sk customer_sk, COUNT(DISTINCT ss_ticket_number) orders,
           SUM(ss_net_paid) revenue
    FROM store_sales GROUP BY ss_customer_sk
)
SELECT c.c_preferred_cust_flag, COUNT(*) customers,
       ROUND(AVG(value.orders), 2) avg_orders, ROUND(AVG(value.revenue), 2) avg_revenue
FROM value JOIN customer c ON value.customer_sk = c.c_customer_sk
GROUP BY c.c_preferred_cust_flag""")
    add(q, "store_contactability_rollup", "customer_data_stewardship", """
SELECT c.c_preferred_cust_flag,
       CASE WHEN c.c_email_address LIKE '%@%' THEN 'contactable' ELSE 'unknown' END status,
       COUNT(DISTINCT c.c_customer_sk) customers,
       ROUND(SUM(ss.ss_net_paid), 2) revenue
FROM store_sales ss JOIN customer c ON ss.ss_customer_sk = c.c_customer_sk
GROUP BY ROLLUP(c.c_preferred_cust_flag, status)""")
    add(q, "catalog_email_domain", "customer_data_stewardship", """
SELECT LOWER(SUBSTRING(c.c_email_address, LOCATE('@', c.c_email_address) + 1)) domain,
       COUNT(DISTINCT c.c_customer_sk) customers,
       ROUND(SUM(cs.cs_net_paid), 2) revenue
FROM catalog_sales cs JOIN customer c ON cs.cs_bill_customer_sk = c.c_customer_sk
WHERE LOCATE('@', c.c_email_address) > 0
GROUP BY domain
HAVING COUNT(*) >= 10""")
    add(q, "web_customer_country", "geographic_segmentation", """
SELECT LOWER(TRIM(c.c_birth_country)) birth_country,
       c.c_preferred_cust_flag, APPROX_COUNT_DISTINCT(c.c_customer_sk) customers,
       ROUND(SUM(ws.ws_net_profit), 2) profit
FROM web_sales ws JOIN customer c ON ws.ws_bill_customer_sk = c.c_customer_sk
GROUP BY birth_country, c.c_preferred_cust_flag""")
    add(q, "web_address_normalization", "customer_data_stewardship", """
SELECT UPPER(TRIM(ca.ca_state)) state_code,
       LOWER(TRIM(ca.ca_city)) city_name,
       COUNT(*) lines, ROUND(SUM(ws.ws_net_paid), 2) revenue
FROM web_sales ws JOIN customer_address ca ON ws.ws_bill_addr_sk = ca.ca_address_sk
GROUP BY state_code, city_name""")
    add(q, "web_page_performance", "digital_analytics", """
SELECT wp.wp_type, wp.wp_autogen_flag,
       COUNT(*) lines, ROUND(AVG(ws.ws_net_paid), 2) avg_paid,
       ROUND(SUM(ws.ws_net_profit), 2) profit
FROM web_sales ws JOIN web_page wp ON ws.ws_web_page_sk = wp.wp_web_page_sk
GROUP BY wp.wp_type, wp.wp_autogen_flag""")
    add(q, "web_page_complexity_rank", "digital_analytics", """
WITH page_sales AS (
    SELECT wp.wp_web_page_sk, wp.wp_char_count, wp.wp_link_count,
           SUM(ws.ws_net_paid) revenue
    FROM web_sales ws JOIN web_page wp ON ws.ws_web_page_sk = wp.wp_web_page_sk
    GROUP BY wp.wp_web_page_sk, wp.wp_char_count, wp.wp_link_count
)
SELECT COUNT(*) pages, ROUND(SUM(revenue), 2) revenue
FROM (
    SELECT revenue, ROW_NUMBER() OVER (
        PARTITION BY FLOOR(wp_char_count / 1000) ORDER BY revenue DESC) position
    FROM page_sales
) ranked WHERE position <= 5""")
    add(q, "catalog_shipping_profile", "fulfillment_analytics", """
SELECT sm.sm_type, sm.sm_carrier,
       ROUND(AVG(cs.cs_ship_date_sk - cs.cs_sold_date_sk), 2) avg_ship_days,
       ROUND(SUM(cs.cs_ext_ship_cost), 2) ship_cost,
       COUNT(*) lines
FROM catalog_sales cs JOIN ship_mode sm ON cs.cs_ship_mode_sk = sm.sm_ship_mode_sk
GROUP BY sm.sm_type, sm.sm_carrier""")
    add(q, "web_shipping_outliers", "fulfillment_analytics", """
SELECT sm.sm_carrier,
       SUM(ws.ws_ship_date_sk - ws.ws_sold_date_sk > 7) delayed_lines,
       ROUND(AVG(ws.ws_ext_ship_cost), 2) avg_cost,
       ROUND(SUM(ws.ws_net_paid), 2) revenue
FROM web_sales ws JOIN ship_mode sm ON ws.ws_ship_mode_sk = sm.sm_ship_mode_sk
GROUP BY sm.sm_carrier
HAVING delayed_lines > 0""")
    add(q, "store_promotion_channels", "campaign_measurement", """
SELECT p.p_channel_email, p.p_channel_tv, p.p_channel_event,
       COUNT(*) lines, ROUND(SUM(ss.ss_ext_discount_amt), 2) discount,
       ROUND(SUM(ss.ss_net_profit), 2) profit
FROM store_sales ss JOIN promotion p ON ss.ss_promo_sk = p.p_promo_sk
GROUP BY p.p_channel_email, p.p_channel_tv, p.p_channel_event""")
    add(q, "store_hourly_pattern", "time_of_day_analysis", """
SELECT t.t_shift, t.t_meal_time, COUNT(*) lines,
       ROUND(SUM(ss.ss_net_paid), 2) revenue
FROM store_sales ss JOIN time_dim t ON ss.ss_sold_time_sk = t.t_time_sk
GROUP BY t.t_shift, t.t_meal_time""")
    add(q, "return_inner_reconciliation", "returns_reconciliation", """
WITH sold AS (
    SELECT ss_ticket_number order_id, ss_item_sk item_sk, SUM(ss_net_paid) paid
    FROM store_sales GROUP BY ss_ticket_number, ss_item_sk
), returned AS (
    SELECT sr_ticket_number order_id, sr_item_sk item_sk, SUM(sr_return_amt) refunded
    FROM store_returns GROUP BY sr_ticket_number, sr_item_sk
)
SELECT COUNT(*) matched_items, ROUND(SUM(paid), 2) paid,
       ROUND(SUM(refunded), 2) refunded
FROM sold JOIN returned USING (order_id, item_sk)""")
    add(q, "return_full_reconciliation", "returns_reconciliation", """
WITH sold AS (
    SELECT cs_order_number order_id, cs_item_sk item_sk, SUM(cs_net_paid) paid
    FROM catalog_sales GROUP BY cs_order_number, cs_item_sk
), returned AS (
    SELECT cr_order_number order_id, cr_item_sk item_sk, SUM(cr_return_amount) refunded
    FROM catalog_returns GROUP BY cr_order_number, cr_item_sk
)
SELECT COUNT(*) rows_checked, SUM(paid IS NULL) orphan_returns,
       SUM(refunded IS NULL) retained_items
FROM sold FULL OUTER JOIN returned USING (order_id, item_sk)""")
    add(q, "return_key_intersection", "returns_reconciliation", """
SELECT COUNT(*) AS returned_sale_keys
FROM (
    SELECT ss_ticket_number, ss_item_sk FROM store_sales
    INTERSECT
    SELECT sr_ticket_number, sr_item_sk FROM store_returns
) matched""")
    add(q, "return_key_difference", "returns_reconciliation", """
SELECT COUNT(*) AS retained_sale_keys
FROM (
    SELECT cs_order_number, cs_item_sk FROM catalog_sales
    EXCEPT
    SELECT cr_order_number, cr_item_sk FROM catalog_returns
) retained""")
    add(q, "return_semi_join", "returns_reconciliation", """
SELECT COUNT(*) returned_lines, ROUND(SUM(ws.ws_net_paid), 2) original_revenue
FROM web_sales ws
WHERE (ws.ws_order_number, ws.ws_item_sk) IN (
    SELECT wr_order_number, wr_item_sk FROM web_returns
)""")
    add(q, "return_anti_join", "returns_reconciliation", """
SELECT COUNT(*) retained_lines, ROUND(SUM(cs.cs_net_profit), 2) retained_profit
FROM catalog_sales cs
WHERE NOT EXISTS (
    SELECT 1 FROM catalog_returns cr
    WHERE cr.cr_order_number = cs.cs_order_number
      AND cr.cr_item_sk = cs.cs_item_sk
)""")
    add(q, "return_event_union", "returns_reconciliation", """
SELECT event_type, COUNT(*) events, ROUND(SUM(amount), 2) amount
FROM (
    SELECT 'sale' event_type, ss_net_paid amount FROM store_sales
    UNION ALL
    SELECT 'return', -sr_return_amt FROM store_returns
) events
GROUP BY event_type""")
    add(q, "return_category_rates", "returns_reconciliation", """
WITH sold AS (
    SELECT ss_item_sk item_sk, COUNT(*) sold_lines FROM store_sales GROUP BY ss_item_sk
), returned AS (
    SELECT sr_item_sk item_sk, COUNT(*) return_lines FROM store_returns GROUP BY sr_item_sk
)
SELECT i.i_category, SUM(sold_lines) sold_lines,
       SUM(IFNULL(return_lines, 0)) return_lines
FROM sold LEFT JOIN returned USING (item_sk)
JOIN item i ON sold.item_sk = i.i_item_sk
GROUP BY i.i_category""")
    add(q, "return_category_rank", "returns_reconciliation", """
WITH losses AS (
    SELECT i.i_category, SUM(sr.sr_net_loss) net_loss
    FROM store_returns sr JOIN item i ON sr.sr_item_sk = i.i_item_sk
    GROUP BY i.i_category
)
SELECT i_category, ROUND(net_loss, 2) net_loss,
       RANK() OVER (ORDER BY net_loss DESC) loss_rank
FROM losses""")
    add(q, "return_monthly_movement", "returns_reconciliation", """
WITH monthly AS (
    SELECT d.d_month_seq month_id, SUM(cr.cr_return_amount) refunds
    FROM catalog_returns cr JOIN date_dim d ON cr.cr_returned_date_sk = d.d_date_sk
    GROUP BY d.d_month_seq
)
SELECT month_id, ROUND(refunds, 2) refunds,
       ROUND(refunds - LAG(refunds) OVER (ORDER BY month_id), 2) change_amount
FROM monthly""")
    add(q, "return_channel_union_distinct", "omnichannel_returns", """
SELECT COUNT(*) customer_count
FROM (
    SELECT sr_customer_sk customer_sk FROM store_returns
    UNION
    SELECT cr_returning_customer_sk FROM catalog_returns
    UNION
    SELECT wr_returning_customer_sk FROM web_returns
) return_customers""")
    add(q, "channel_three_way_exclusive", "omnichannel_customers", """
SELECT COUNT(*) store_only_customers
FROM (
    SELECT ss_customer_sk customer_sk FROM store_sales
    EXCEPT
    SELECT cs_bill_customer_sk FROM catalog_sales
    EXCEPT
    SELECT ws_bill_customer_sk FROM web_sales
) customers""")
    add(q, "channel_customer_intersection", "omnichannel_customers", """
SELECT COUNT(*) all_channel_customers
FROM (
    SELECT ss_customer_sk customer_sk FROM store_sales
    INTERSECT
    SELECT cs_bill_customer_sk FROM catalog_sales
    INTERSECT
    SELECT ws_bill_customer_sk FROM web_sales
) customers""")
    add(q, "channel_customer_full_join", "omnichannel_customers", """
WITH store_customer AS (
    SELECT ss_customer_sk customer_sk, SUM(ss_net_paid) store_revenue
    FROM store_sales GROUP BY ss_customer_sk
), web_customer AS (
    SELECT ws_bill_customer_sk customer_sk, SUM(ws_net_paid) web_revenue
    FROM web_sales GROUP BY ws_bill_customer_sk
)
SELECT COUNT(*) customers, SUM(store_revenue IS NULL) web_only,
       SUM(web_revenue IS NULL) store_only
FROM store_customer FULL OUTER JOIN web_customer USING (customer_sk)""")
    add(q, "channel_customer_left_join", "omnichannel_customers", """
WITH catalog_customer AS (
    SELECT cs_bill_customer_sk customer_sk, SUM(cs_net_paid) catalog_revenue
    FROM catalog_sales GROUP BY cs_bill_customer_sk
), web_customer AS (
    SELECT ws_bill_customer_sk customer_sk, SUM(ws_net_paid) web_revenue
    FROM web_sales GROUP BY ws_bill_customer_sk
)
SELECT COUNT(*) catalog_customers, COUNT(web_customer.customer_sk) cross_shoppers,
       ROUND(SUM(catalog_revenue), 2) catalog_revenue
FROM catalog_customer LEFT JOIN web_customer USING (customer_sk)""")
    add(q, "channel_customer_rank_gap", "omnichannel_customers", """
WITH value AS (
    SELECT ss_customer_sk customer_sk, SUM(ss_net_paid) revenue
    FROM store_sales GROUP BY ss_customer_sk
), ranked AS (
    SELECT customer_sk, revenue, NTILE(10) OVER (ORDER BY revenue DESC) value_decile
    FROM value
)
SELECT value_decile, COUNT(*) customers, ROUND(AVG(revenue), 2) avg_revenue
FROM ranked GROUP BY value_decile""")
    add(q, "store_customer_right_audit", "data_quality", """
SELECT COUNT(*) customer_rows, COUNT(ss.ss_customer_sk) customers_with_sales,
       ROUND(SUM(IFNULL(ss.ss_net_paid, 0)), 2) revenue
FROM store_sales ss
RIGHT JOIN customer c ON ss.ss_customer_sk = c.c_customer_sk""")
    add(q, "store_customer_scalar_dashboard", "customer_reporting", """
SELECT
    (SELECT COUNT(DISTINCT ss_customer_sk) FROM store_sales) store_customers,
    (SELECT COUNT(*) FROM customer WHERE c_preferred_cust_flag = 'Y') preferred_customers,
    (SELECT COUNT(*) FROM customer WHERE c_email_address LIKE '%@%') contactable_customers""")
    add(q, "channel_scalar_dashboard", "omnichannel_reporting", """
SELECT
    (SELECT ROUND(SUM(ss_net_paid), 2) FROM store_sales) store_revenue,
    (SELECT ROUND(SUM(cs_net_paid), 2) FROM catalog_sales) catalog_revenue,
    (SELECT ROUND(SUM(ws_net_paid), 2) FROM web_sales) web_revenue,
    (SELECT COUNT(*) FROM store_returns) store_return_lines""")
    add(q, "customer_quality_union", "customer_data_stewardship", """
SELECT status, COUNT(*) lines, ROUND(SUM(revenue), 2) revenue
FROM (
    SELECT 'valid_email' status, ss_net_paid revenue
    FROM store_sales JOIN customer ON ss_customer_sk = c_customer_sk
    WHERE c_email_address LIKE '%@%'
    UNION ALL
    SELECT 'missing_email', cs_net_paid
    FROM catalog_sales JOIN customer ON cs_bill_customer_sk = c_customer_sk
    WHERE c_email_address IS NULL
) quality_events
GROUP BY status""")
    add(q, "customer_state_union", "geographic_segmentation", """
SELECT COUNT(*) AS active_states
FROM (
    SELECT ca.ca_state
    FROM store_sales ss JOIN customer_address ca ON ss.ss_addr_sk = ca.ca_address_sk
    UNION
    SELECT ca.ca_state
    FROM web_sales ws JOIN customer_address ca ON ws.ws_bill_addr_sk = ca.ca_address_sk
) states""")
    add(q, "store_customer_dual_rank", "customer_segmentation", """
WITH value AS (
    SELECT ss_customer_sk customer_sk, SUM(ss_net_paid) revenue,
           SUM(ss_net_profit) profit
    FROM store_sales GROUP BY ss_customer_sk
), ranked AS (
    SELECT ROW_NUMBER() OVER (ORDER BY revenue DESC) revenue_rank,
           PERCENT_RANK() OVER (ORDER BY profit) profit_percentile
    FROM value
)
SELECT COUNT(*) top_revenue_profitable
FROM ranked WHERE revenue_rank <= 1000 AND profit_percentile >= 0.5""")
    add(q, "store_customer_lead_gap", "customer_segmentation", """
WITH value AS (
    SELECT ss_customer_sk customer_sk, SUM(ss_net_paid) revenue
    FROM store_sales GROUP BY ss_customer_sk
), compared AS (
    SELECT revenue, LEAD(revenue) OVER (ORDER BY revenue DESC) next_revenue
    FROM value
)
SELECT ROUND(AVG(revenue - next_revenue), 2) average_gap
FROM compared WHERE next_revenue IS NOT NULL""")
    add(q, "store_customer_value_floor", "customer_segmentation", """
WITH value AS (
    SELECT ss_customer_sk customer_sk, ss_sold_date_sk date_sk,
           SUM(ss_net_paid) revenue
    FROM store_sales GROUP BY ss_customer_sk, ss_sold_date_sk
), measured AS (
    SELECT revenue, FIRST_VALUE(revenue) OVER (
        PARTITION BY customer_sk ORDER BY date_sk) first_day_revenue
    FROM value
)
SELECT ROUND(AVG(ABS(revenue - first_day_revenue)), 2) average_change
FROM measured""")
    add(q, "store_customer_cume_dist", "customer_segmentation", """
WITH value AS (
    SELECT ss_customer_sk customer_sk, SUM(ss_net_paid) revenue
    FROM store_sales GROUP BY ss_customer_sk
), distributed AS (
    SELECT revenue, CUME_DIST() OVER (ORDER BY revenue) revenue_position
    FROM value
)
SELECT COUNT(*) upper_quartile, ROUND(SUM(revenue), 2) upper_revenue
FROM distributed WHERE revenue_position >= 0.75""")
    add(q, "store_customer_cross_metrics", "customer_reporting", """
SELECT sales.customers, contacts.contactable, geography.states
FROM (
    SELECT COUNT(DISTINCT ss_customer_sk) customers FROM store_sales
) sales
CROSS JOIN (
    SELECT COUNT(*) contactable FROM customer WHERE c_email_address LIKE '%@%'
) contacts
CROSS JOIN (
    SELECT COUNT(DISTINCT ca_state) states FROM customer_address
) geography""")
    return q


def audit_queries():
    q = []
    add(q, "audit_customer_identity", "customer_data_stewardship", """
SELECT SUM(c.c_first_name IS NULL OR LENGTH(TRIM(c.c_first_name)) = 0) missing_first,
       SUM(c.c_last_name IS NULL OR LENGTH(TRIM(c.c_last_name)) = 0) missing_last,
       SUM(c.c_birth_year NOT BETWEEN 1900 AND 2000) invalid_birth_year,
       APPROX_COUNT_DISTINCT(LOWER(TRIM(c.c_last_name))) normalized_surnames,
       ROUND(SUM(ss.ss_net_paid), 2) associated_revenue
FROM store_sales ss JOIN customer c ON ss.ss_customer_sk = c.c_customer_sk""")
    add(q, "audit_customer_email", "customer_data_stewardship", """
SELECT SUM(c.c_email_address IS NULL) missing_email,
       SUM(LOCATE('@', c.c_email_address) <= 1) missing_local_part,
       SUM(LOCATE('.', c.c_email_address) < LOCATE('@', c.c_email_address)) missing_domain,
       APPROX_COUNT_DISTINCT(LOWER(SUBSTRING(
           c.c_email_address, LOCATE('@', c.c_email_address) + 1))) email_domains,
       ROUND(SUM(cs.cs_net_paid), 2) associated_revenue
FROM catalog_sales cs JOIN customer c ON cs.cs_bill_customer_sk = c.c_customer_sk""")
    add(q, "audit_customer_names", "customer_data_stewardship", """
SELECT SUM(c.c_first_name != TRIM(c.c_first_name)) padded_first_names,
       SUM(c.c_last_name != TRIM(c.c_last_name)) padded_last_names,
       SUM(LOWER(c.c_first_name) = LOWER(c.c_last_name)) repeated_names,
       APPROX_COUNT_DISTINCT(CONCAT(LOWER(TRIM(c.c_first_name)), ' ',
                                    LOWER(TRIM(c.c_last_name)))) normalized_names,
       ROUND(SUM(ws.ws_net_profit), 2) associated_profit
FROM web_sales ws JOIN customer c ON ws.ws_bill_customer_sk = c.c_customer_sk""")
    add(q, "audit_address_completeness", "address_data_stewardship", """
SELECT SUM(LENGTH(TRIM(ca.ca_street_name)) = 0) missing_street,
       SUM(LENGTH(TRIM(ca.ca_city)) = 0) missing_city,
       SUM(LENGTH(TRIM(ca.ca_state)) != 2) invalid_state,
       SUM(ca.ca_country IS NULL) missing_country,
       ROUND(SUM(ss.ss_net_paid), 2) associated_revenue
FROM store_sales ss JOIN customer_address ca ON ss.ss_addr_sk = ca.ca_address_sk""")
    add(q, "audit_address_postcode", "address_data_stewardship", """
SELECT SUM(ca.ca_zip IS NULL) missing_zip,
       SUM(LENGTH(TRIM(ca.ca_zip)) NOT IN (5, 10)) unusual_zip,
       APPROX_COUNT_DISTINCT(LEFT(TRIM(ca.ca_zip), 5)) postal_areas,
       APPROX_COUNT_DISTINCT(LOWER(TRIM(ca.ca_city))) normalized_cities,
       ROUND(SUM(cs.cs_net_paid), 2) associated_revenue
FROM catalog_sales cs JOIN customer_address ca ON cs.cs_bill_addr_sk = ca.ca_address_sk""")
    add(q, "audit_item_description", "catalog_data_stewardship", """
SELECT SUM(i.i_item_desc IS NULL) missing_description,
       SUM(LENGTH(TRIM(i.i_item_desc)) < 20) short_description,
       SUM(LENGTH(TRIM(i.i_product_name)) = 0) missing_product_name,
       APPROX_COUNT_DISTINCT(LOWER(TRIM(i.i_product_name))) normalized_products,
       ROUND(SUM(ss.ss_net_paid), 2) associated_revenue
FROM store_sales ss JOIN item i ON ss.ss_item_sk = i.i_item_sk""")
    add(q, "audit_item_taxonomy", "catalog_data_stewardship", """
SELECT SUM(i.i_category IS NULL) missing_category,
       SUM(i.i_class IS NULL) missing_class,
       SUM(i.i_brand IS NULL) missing_brand,
       SUM(i.i_current_price < i.i_wholesale_cost) below_cost_items,
       APPROX_COUNT_DISTINCT(CONCAT(LOWER(TRIM(i.i_category)), '/',
                                    LOWER(TRIM(i.i_class)))) taxonomy_paths,
       ROUND(SUM(cs.cs_net_profit), 2) associated_profit
FROM catalog_sales cs JOIN item i ON cs.cs_item_sk = i.i_item_sk""")
    add(q, "audit_promotion_metadata", "campaign_data_stewardship", """
SELECT SUM(LENGTH(TRIM(p.p_promo_name)) = 0) missing_name,
       SUM(p.p_start_date_sk > p.p_end_date_sk) invalid_period,
       SUM(p.p_cost < 0) invalid_cost,
       SUM(p.p_channel_email = 'N' AND p.p_channel_tv = 'N'
           AND p.p_channel_event = 'N') no_primary_channel,
       ROUND(SUM(ss.ss_ext_discount_amt), 2) associated_discount
FROM store_sales ss JOIN promotion p ON ss.ss_promo_sk = p.p_promo_sk""")
    add(q, "audit_store_metadata", "store_data_stewardship", """
SELECT SUM(LENGTH(TRIM(s.s_store_name)) = 0) missing_name,
       SUM(s.s_number_employees <= 0) invalid_staffing,
       SUM(s.s_floor_space <= 0) invalid_floor_space,
       SUM(LENGTH(TRIM(s.s_state)) != 2) invalid_state,
       APPROX_COUNT_DISTINCT(LOWER(TRIM(s.s_market_manager))) managers,
       ROUND(SUM(ss.ss_net_paid), 2) associated_revenue
FROM store_sales ss JOIN store s ON ss.ss_store_sk = s.s_store_sk""")
    add(q, "audit_web_page_metadata", "digital_data_stewardship", """
SELECT SUM(LENGTH(TRIM(wp.wp_url)) = 0) missing_url,
       SUM(wp.wp_char_count < 0) invalid_character_count,
       SUM(wp.wp_link_count < 0) invalid_link_count,
       SUM(wp.wp_image_count > wp.wp_link_count * 10) image_outlier,
       APPROX_COUNT_DISTINCT(LOWER(TRIM(wp.wp_type))) page_types,
       ROUND(SUM(ws.ws_net_paid), 2) associated_revenue
FROM web_sales ws JOIN web_page wp ON ws.ws_web_page_sk = wp.wp_web_page_sk""")
    add(q, "audit_shipping_codes", "fulfillment_data_stewardship", """
SELECT SUM(LENGTH(TRIM(sm.sm_code)) = 0) missing_code,
       SUM(LENGTH(TRIM(sm.sm_carrier)) = 0) missing_carrier,
       SUM(cs.cs_ship_date_sk < cs.cs_sold_date_sk) negative_lead_time,
       SUM(cs.cs_ship_date_sk - cs.cs_sold_date_sk > 30) excessive_lead_time,
       ROUND(SUM(cs.cs_ext_ship_cost), 2) associated_cost
FROM catalog_sales cs JOIN ship_mode sm ON cs.cs_ship_mode_sk = sm.sm_ship_mode_sk""")
    add(q, "audit_calendar_consistency", "calendar_data_stewardship", """
SELECT SUM(d.d_weekend = 'Y' AND d.d_dow NOT IN (1, 7)) weekend_mismatch,
       SUM(d.d_holiday = 'Y' AND d.d_following_holiday = 'Y') adjacent_holiday,
       SUM(d.d_dom > d.d_last_dom - d.d_first_dom + 1) invalid_day,
       APPROX_COUNT_DISTINCT(d.d_week_seq) active_weeks,
       ROUND(SUM(ss.ss_net_paid), 2) associated_revenue
FROM store_sales ss JOIN date_dim d ON ss.ss_sold_date_sk = d.d_date_sk""")
    add(q, "audit_store_amounts", "financial_data_stewardship", """
SELECT SUM(ABS(ss_ext_sales_price - ss_sales_price * ss_quantity) > 0.01) sales_mismatch,
       SUM(ABS(ss_net_paid_inc_tax - ss_net_paid - ss_ext_tax) > 0.01) tax_mismatch,
       SUM(ROUND(ss_net_paid - ss_coupon_amt, 2) < 0) over_couponed,
       SUM(ss_ext_discount_amt > ss_ext_list_price) excess_discount,
       ROUND(SUM(ss_net_profit), 2) reported_profit
FROM store_sales""")
    add(q, "audit_catalog_amounts", "financial_data_stewardship", """
SELECT SUM(ABS(cs_net_paid_inc_ship - cs_net_paid - cs_ext_ship_cost) > 0.01) ship_mismatch,
       SUM(ABS(cs_net_paid_inc_tax - cs_net_paid - cs_ext_tax) > 0.01) tax_mismatch,
       SUM(ROUND(cs_ext_sales_price + cs_ext_tax + cs_ext_ship_cost, 2)
           < cs_net_paid) total_mismatch,
       SUM(cs_net_profit < -cs_ext_wholesale_cost) extreme_loss,
       ROUND(SUM(cs_net_paid), 2) reported_revenue
FROM catalog_sales""")
    add(q, "audit_web_amounts", "financial_data_stewardship", """
SELECT SUM(ABS(ws_net_paid_inc_ship_tax - ws_net_paid - ws_ext_ship_cost
               - ws_ext_tax) > 0.01) total_mismatch,
       SUM(ABS(ws_ext_sales_price - ws_sales_price * ws_quantity) > 0.01) sales_mismatch,
       SUM(ROUND(ws_coupon_amt + ws_ext_discount_amt, 2)
           > ws_ext_list_price) promotion_outlier,
       SUM(ws_net_profit < -ws_ext_wholesale_cost) extreme_loss,
       ROUND(SUM(ws_net_paid), 2) reported_revenue
FROM web_sales""")
    return q


def function_queries():
    q = []
    add(q, "normalize_email_regex", "customer_data_stewardship", """
SELECT REGEXP_REPLACE(LOWER(TRIM(c.c_email_address)), '[[:space:]]+', '') email,
       COUNT(*) sales_lines, ROUND(SUM(cs.cs_net_paid), 2) revenue
FROM catalog_sales cs JOIN customer c ON cs.cs_bill_customer_sk = c.c_customer_sk
WHERE c.c_email_address IS NOT NULL
GROUP BY email
HAVING COUNT(*) > 1""")
    add(q, "segment_email_domains", "customer_segmentation", """
SELECT SPLIT_PART(LOWER(TRIM(c.c_email_address)), '@', 2) domain,
       c.c_preferred_cust_flag, APPROX_COUNT_DISTINCT(c.c_customer_sk) customers,
       ROUND(SUM(ss.ss_net_paid), 2) revenue
FROM store_sales ss JOIN customer c ON ss.ss_customer_sk = c.c_customer_sk
WHERE LOCATE('@', c.c_email_address) > 0
GROUP BY domain, c.c_preferred_cust_flag""")
    add(q, "analyze_web_hosts", "digital_analytics", """
SELECT PARSE_URL(LOWER(TRIM(wp.wp_url)), 'HOST') host,
       wp.wp_type, COUNT(*) sales_lines,
       ROUND(AVG(ws.ws_net_paid), 2) avg_paid
FROM web_sales ws JOIN web_page wp ON ws.ws_web_page_sk = wp.wp_web_page_sk
WHERE wp.wp_url LIKE 'http%'
GROUP BY host, wp.wp_type""")
    add(q, "calendar_label_report", "time_series_reporting", """
SELECT DATE_FORMAT(d.d_date, '%Y-%m') month_label,
       d.d_weekend, COUNT(*) sales_lines,
       ROUND(SUM(ss.ss_net_paid), 2) revenue
FROM store_sales ss JOIN date_dim d ON ss.ss_sold_date_sk = d.d_date_sk
GROUP BY month_label, d.d_weekend
ORDER BY month_label, d.d_weekend""")
    add(q, "reverse_name_collision_audit", "catalog_data_stewardship", """
SELECT COUNT(*) collision_groups
FROM (
    SELECT REVERSE(LOWER(TRIM(i.i_product_name))) reverse_name,
           COUNT(DISTINCT i.i_item_sk) products, SUM(ws.ws_net_paid) revenue
    FROM web_sales ws JOIN item i ON ws.ws_item_sk = i.i_item_sk
    GROUP BY reverse_name
    HAVING products > 1
) collisions""")
    add(q, "email_suffix_distribution", "customer_segmentation", """
SELECT SUBSTRING_INDEX(LOWER(TRIM(c.c_email_address)), '.', -1) domain_suffix,
       COUNT(DISTINCT c.c_customer_sk) customers,
       ROUND(SUM(ws.ws_net_profit), 2) profit
FROM web_sales ws JOIN customer c ON ws.ws_bill_customer_sk = c.c_customer_sk
WHERE c.c_email_address LIKE '%@%.%'
GROUP BY domain_suffix""")
    add(q, "normalize_address_tokens", "address_data_stewardship", """
SELECT REGEXP_REPLACE(LOWER(TRIM(ca.ca_street_name)), '[^a-z0-9]+', ' ') street_token,
       UPPER(TRIM(ca.ca_state)) state_code,
       COUNT(*) sales_lines, ROUND(SUM(ss.ss_net_paid), 2) revenue
FROM store_sales ss JOIN customer_address ca ON ss.ss_addr_sk = ca.ca_address_sk
GROUP BY street_token, state_code
HAVING COUNT(*) >= 10""")
    add(q, "standardize_store_market", "store_data_stewardship", """
SELECT REPLACE(REPLACE(LOWER(TRIM(s.s_market_desc)), '-', ' '), '/', ' ') market_label,
       COUNT(*) sales_lines, APPROX_COUNT_DISTINCT(ss.ss_customer_sk) customers,
       ROUND(SUM(ss.ss_net_profit), 2) profit
FROM store_sales ss JOIN store s ON ss.ss_store_sk = s.s_store_sk
GROUP BY market_label""")
    add(q, "product_code_profile", "catalog_data_stewardship", """
SELECT LEFT(TRIM(i.i_item_id), 3) code_prefix,
       RIGHT(TRIM(i.i_item_id), 3) code_suffix,
       COUNT(DISTINCT i.i_item_sk) products,
       ROUND(SUM(cs.cs_net_paid), 2) revenue
FROM catalog_sales cs JOIN item i ON cs.cs_item_sk = i.i_item_sk
GROUP BY code_prefix, code_suffix""")
    add(q, "customer_login_shape", "customer_data_stewardship", """
WITH login_shapes AS (
    SELECT LENGTH(TRIM(c.c_login)) login_length,
           LOWER(LEFT(TRIM(c.c_login), 1)) initial,
           SUM(ss.ss_net_paid) revenue
    FROM store_sales ss JOIN customer c ON ss.ss_customer_sk = c.c_customer_sk
    GROUP BY login_length, initial
)
SELECT login_length, COUNT(*) shapes, ROUND(SUM(revenue), 2) revenue,
       DENSE_RANK() OVER (ORDER BY SUM(revenue) DESC) revenue_rank
FROM login_shapes GROUP BY login_length""")
    return q


def write_suite(output_dir):
    queries = []
    for channel in CHANNELS:
        queries.extend(channel_queries(channel))
    queries.extend(cross_channel_queries())
    expanded = structurally_diverse_queries()
    for query in expanded:
        query["role"] = (
            "known_slow"
            if query["query_id"] == "store_contactability_rollup"
            else "expanded"
        )
    queries.extend(expanded)
    audits = audit_queries()
    for query in audits:
        query["role"] = "audit"
    queries.extend(audits)
    functions = function_queries()
    for query in functions:
        query["role"] = "function"
    queries.extend(functions)
    if len({q["query_id"] for q in queries}) != len(queries):
        raise ValueError("duplicate query id")

    output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for query in queries:
        path = output_dir / f"{query['query_id']}.sql"
        path.write_text(query["sql"], encoding="utf-8")
        records.append({
            "query_id": query["query_id"],
            "path": path.name,
            "workload_class": query["task"],
            "role": query.get("role", "base"),
            "line_count": len(query["sql"].splitlines()),
            "sql_sha256": hashlib.sha256(query["sql"].encode()).hexdigest(),
        })
    manifest = {
        "schema_version": 1,
        "database": "tpcds_sf100",
        "construction": "business-task templates without synthetic row fingerprints",
        "query_count": len(records),
        "queries": records,
    }
    manifest_path = output_dir / "candidates.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return manifest_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(write_suite(args.output_dir.resolve()))


if __name__ == "__main__":
    main()
