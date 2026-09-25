CREATE DATABASE IF NOT EXISTS autodop_stage_reference;

DROP TABLE IF EXISTS autodop_stage_reference.staging_key_manifest;
CREATE TABLE autodop_stage_reference.staging_key_manifest (
    workload VARCHAR(16) NOT NULL,
    source_name VARCHAR(32) NOT NULL,
    expected_key INT NOT NULL
)
DUPLICATE KEY(workload, source_name, expected_key)
DISTRIBUTED BY HASH(source_name) BUCKETS 3
PROPERTIES ("replication_num" = "1");

INSERT INTO autodop_stage_reference.staging_key_manifest VALUES
    ('tpch', 'order_line', 1000000001),
    ('tpch', 'order_line', 1000000002),
    ('tpch', 'order_line', 1000000003),
    ('tpch', 'order_header', 1000000001),
    ('tpch', 'order_header', 1000000002),
    ('tpch', 'order_header', 1000000003),
    ('tpch', 'inventory', 1000000001),
    ('tpch', 'inventory', 1000000002),
    ('tpch', 'inventory', 1000000003),
    ('ssb', 'customer_feed', 1000000001),
    ('ssb', 'customer_feed', 1000000002),
    ('ssb', 'customer_feed', 1000000003),
    ('ssb', 'product_feed', 1000000001),
    ('ssb', 'product_feed', 1000000002),
    ('ssb', 'product_feed', 1000000003),
    ('ssb', 'supplier_feed', 1000000001),
    ('ssb', 'supplier_feed', 1000000002),
    ('ssb', 'supplier_feed', 1000000003);
