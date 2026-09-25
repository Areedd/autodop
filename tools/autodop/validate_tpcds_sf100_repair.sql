SET query_timeout = 300;

SELECT 'store_sales' AS fact_table,
       COUNT(*) AS row_count,
       COUNT(ss_sold_date_sk) AS nonnull_date_keys,
       COUNT(d_date_sk) AS matched_date_keys,
       COUNT(ss_item_sk) AS nonnull_item_keys,
       COUNT(i_item_sk) AS matched_item_keys
FROM tpcds_sf100.store_sales f
LEFT JOIN tpcds_sf100.date_dim d ON f.ss_sold_date_sk = d.d_date_sk
LEFT JOIN tpcds_sf100.item i ON f.ss_item_sk = i.i_item_sk
UNION ALL
SELECT 'catalog_sales', COUNT(*), COUNT(cs_sold_date_sk), COUNT(d_date_sk),
       COUNT(cs_item_sk), COUNT(i_item_sk)
FROM tpcds_sf100.catalog_sales f
LEFT JOIN tpcds_sf100.date_dim d ON f.cs_sold_date_sk = d.d_date_sk
LEFT JOIN tpcds_sf100.item i ON f.cs_item_sk = i.i_item_sk
UNION ALL
SELECT 'web_sales', COUNT(*), COUNT(ws_sold_date_sk), COUNT(d_date_sk),
       COUNT(ws_item_sk), COUNT(i_item_sk)
FROM tpcds_sf100.web_sales f
LEFT JOIN tpcds_sf100.date_dim d ON f.ws_sold_date_sk = d.d_date_sk
LEFT JOIN tpcds_sf100.item i ON f.ws_item_sk = i.i_item_sk
UNION ALL
SELECT 'store_returns', COUNT(*), COUNT(sr_returned_date_sk), COUNT(d_date_sk),
       COUNT(sr_item_sk), COUNT(i_item_sk)
FROM tpcds_sf100.store_returns f
LEFT JOIN tpcds_sf100.date_dim d ON f.sr_returned_date_sk = d.d_date_sk
LEFT JOIN tpcds_sf100.item i ON f.sr_item_sk = i.i_item_sk
UNION ALL
SELECT 'catalog_returns', COUNT(*), COUNT(cr_returned_date_sk), COUNT(d_date_sk),
       COUNT(cr_item_sk), COUNT(i_item_sk)
FROM tpcds_sf100.catalog_returns f
LEFT JOIN tpcds_sf100.date_dim d ON f.cr_returned_date_sk = d.d_date_sk
LEFT JOIN tpcds_sf100.item i ON f.cr_item_sk = i.i_item_sk
UNION ALL
SELECT 'web_returns', COUNT(*), COUNT(wr_returned_date_sk), COUNT(d_date_sk),
       COUNT(wr_item_sk), COUNT(i_item_sk)
FROM tpcds_sf100.web_returns f
LEFT JOIN tpcds_sf100.date_dim d ON f.wr_returned_date_sk = d.d_date_sk
LEFT JOIN tpcds_sf100.item i ON f.wr_item_sk = i.i_item_sk;
