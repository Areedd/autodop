#!/usr/bin/env bash
set -euo pipefail

FE_HOST=${FE_HOST:-127.0.0.1}
FE_MYSQL_PORT=${FE_MYSQL_PORT:-9030}
FE_HTTP_PORT=${FE_HTTP_PORT:-8030}
STARROCKS_USER=${STARROCKS_USER:-root}
STARROCKS_PASSWORD=${STARROCKS_PASSWORD:-}
SOURCE_DB=${SOURCE_DB:-tpcds_sf10}
TARGET_DB=${TARGET_DB:-tpcds_sf10_native}
DATA_DIR=${DATA_DIR:-/home/shaokun/starrocks_deploy/tpcds_sf10}
TIMEOUT=${TIMEOUT:-7200}

mysql_cmd=(mysql -h "${FE_HOST}" -P "${FE_MYSQL_PORT}" -u "${STARROCKS_USER}")
if [[ -n "${STARROCKS_PASSWORD}" ]]; then
  mysql_cmd+=("-p${STARROCKS_PASSWORD}")
fi

database_count=$("${mysql_cmd[@]}" -N -B -e \
  "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='${TARGET_DB}'")
if [[ "${database_count}" != "0" ]]; then
  echo "Target database already exists: ${TARGET_DB}" >&2
  exit 1
fi

"${mysql_cmd[@]}" -e "
CREATE DATABASE ${TARGET_DB};
CREATE TABLE ${TARGET_DB}.date_dim LIKE ${SOURCE_DB}.date_dim;
CREATE TABLE ${TARGET_DB}.customer LIKE ${SOURCE_DB}.customer;
CREATE TABLE ${TARGET_DB}.store_sales LIKE ${SOURCE_DB}.store_sales;
CREATE TABLE ${TARGET_DB}.catalog_sales LIKE ${SOURCE_DB}.catalog_sales;
CREATE TABLE ${TARGET_DB}.web_sales LIKE ${SOURCE_DB}.web_sales;
INSERT INTO ${TARGET_DB}.date_dim SELECT * FROM ${SOURCE_DB}.date_dim;
INSERT INTO ${TARGET_DB}.customer SELECT * FROM ${SOURCE_DB}.customer;
"

load_fact() {
  local table=$1
  local file=$2
  local columns=$3
  local label="${TARGET_DB}_${table}_$(date +%s%N)"
  local response
  echo "Loading ${file} -> ${TARGET_DB}.${table}"
  response=$(curl --noproxy '*' --fail --show-error --location-trusted \
    -u "${STARROCKS_USER}:${STARROCKS_PASSWORD}" \
    -H "label:${label}" \
    -H "format:csv" \
    -H "column_separator:|" \
    -H "columns:${columns}" \
    -H "Expect: 100-continue" \
    -H "timeout:${TIMEOUT}" \
    -H "max_filter_ratio:0" \
    -T "${DATA_DIR}/${file}" \
    "http://${FE_HOST}:${FE_HTTP_PORT}/api/${TARGET_DB}/${table}/_stream_load")
  printf '%s\n' "${response}"
  grep -Eq '"Status"[[:space:]]*:[[:space:]]*"Success"' <<< "${response}"
}

load_fact store_sales store_sales.dat \
  'ss_sold_date_sk,ss_sold_time_sk,ss_item_sk,ss_customer_sk,ss_cdemo_sk,ss_hdemo_sk,ss_addr_sk,ss_store_sk,ss_promo_sk,ss_ticket_number,ss_quantity,ss_wholesale_cost,ss_list_price,ss_sales_price,ss_ext_discount_amt,ss_ext_sales_price,ss_ext_wholesale_cost,ss_ext_list_price,ss_ext_tax,ss_coupon_amt,ss_net_paid,ss_net_paid_inc_tax,ss_net_profit'

load_fact catalog_sales catalog_sales.dat \
  'cs_sold_date_sk,cs_sold_time_sk,cs_ship_date_sk,cs_bill_customer_sk,cs_bill_cdemo_sk,cs_bill_hdemo_sk,cs_bill_addr_sk,cs_ship_customer_sk,cs_ship_cdemo_sk,cs_ship_hdemo_sk,cs_ship_addr_sk,cs_call_center_sk,cs_catalog_page_sk,cs_ship_mode_sk,cs_warehouse_sk,cs_item_sk,cs_promo_sk,cs_order_number,cs_quantity,cs_wholesale_cost,cs_list_price,cs_sales_price,cs_ext_discount_amt,cs_ext_sales_price,cs_ext_wholesale_cost,cs_ext_list_price,cs_ext_tax,cs_coupon_amt,cs_ext_ship_cost,cs_net_paid,cs_net_paid_inc_tax,cs_net_paid_inc_ship,cs_net_paid_inc_ship_tax,cs_net_profit'

load_fact web_sales web_sales.dat \
  'ws_sold_date_sk,ws_sold_time_sk,ws_ship_date_sk,ws_item_sk,ws_bill_customer_sk,ws_bill_cdemo_sk,ws_bill_hdemo_sk,ws_bill_addr_sk,ws_ship_customer_sk,ws_ship_cdemo_sk,ws_ship_hdemo_sk,ws_ship_addr_sk,ws_web_page_sk,ws_web_site_sk,ws_ship_mode_sk,ws_warehouse_sk,ws_promo_sk,ws_order_number,ws_quantity,ws_wholesale_cost,ws_list_price,ws_sales_price,ws_ext_discount_amt,ws_ext_sales_price,ws_ext_wholesale_cost,ws_ext_list_price,ws_ext_tax,ws_coupon_amt,ws_ext_ship_cost,ws_net_paid,ws_net_paid_inc_tax,ws_net_paid_inc_ship,ws_net_paid_inc_ship_tax,ws_net_profit'

"${mysql_cmd[@]}" -N -B -e "
SELECT 'store_sales', COUNT(*), MIN(ss_sold_date_sk), MAX(ss_sold_date_sk)
FROM ${TARGET_DB}.store_sales
UNION ALL
SELECT 'catalog_sales', COUNT(*), MIN(cs_sold_date_sk), MAX(cs_sold_date_sk)
FROM ${TARGET_DB}.catalog_sales
UNION ALL
SELECT 'web_sales', COUNT(*), MIN(ws_sold_date_sk), MAX(ws_sold_date_sk)
FROM ${TARGET_DB}.web_sales;
"
