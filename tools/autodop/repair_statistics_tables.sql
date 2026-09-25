CREATE TABLE IF NOT EXISTS _statistics_.table_statistic_v1 (
    table_id BIGINT NOT NULL,
    column_name VARCHAR(65530) NOT NULL,
    db_id BIGINT NOT NULL,
    table_name VARCHAR(65530),
    db_name VARCHAR(65530),
    row_count BIGINT,
    data_size BIGINT,
    distinct_count BIGINT,
    null_count BIGINT,
    `max` VARCHAR(65533),
    `min` VARCHAR(65533),
    update_time DATETIME
)
UNIQUE KEY (table_id, column_name, db_id)
DISTRIBUTED BY HASH (table_id, column_name, db_id) BUCKETS 10
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS _statistics_.column_statistics (
    table_id BIGINT NOT NULL,
    partition_id BIGINT NOT NULL,
    column_name VARCHAR(65530) NOT NULL,
    db_id BIGINT,
    table_name VARCHAR(65530),
    partition_name VARCHAR(65530),
    row_count BIGINT,
    data_size BIGINT,
    ndv HLL,
    null_count BIGINT,
    `max` VARCHAR(65533),
    `min` VARCHAR(65533),
    update_time DATETIME,
    collection_size BIGINT DEFAULT '-1'
)
PRIMARY KEY (table_id, partition_id, column_name)
DISTRIBUTED BY HASH (table_id, partition_id, column_name) BUCKETS 10
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS _statistics_.histogram_statistics (
    table_id BIGINT NOT NULL,
    column_name VARCHAR(65530) NOT NULL,
    db_id BIGINT,
    table_name VARCHAR(65530),
    buckets VARCHAR(65533),
    mcv VARCHAR(65533),
    update_time DATETIME
)
PRIMARY KEY (table_id, column_name)
DISTRIBUTED BY HASH (table_id, column_name) BUCKETS 10
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS _statistics_.external_column_statistics (
    table_uuid VARCHAR(65530) NOT NULL,
    partition_name VARCHAR(65530) NOT NULL,
    column_name VARCHAR(65530) NOT NULL,
    catalog_name VARCHAR(65530),
    db_name VARCHAR(65530),
    table_name VARCHAR(65530),
    row_count BIGINT,
    data_size BIGINT,
    ndv HLL,
    null_count BIGINT,
    `max` VARCHAR(65533),
    `min` VARCHAR(65533),
    update_time DATETIME
)
PRIMARY KEY (table_uuid, partition_name, column_name)
DISTRIBUTED BY HASH (table_uuid, partition_name, column_name) BUCKETS 10
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS _statistics_.external_histogram_statistics (
    table_uuid VARCHAR(65530) NOT NULL,
    column_name VARCHAR(65530) NOT NULL,
    catalog_name VARCHAR(65530),
    db_name VARCHAR(65530),
    table_name VARCHAR(65530),
    buckets VARCHAR(65533),
    mcv VARCHAR(65533),
    update_time DATETIME
)
PRIMARY KEY (table_uuid, column_name)
DISTRIBUTED BY HASH (table_uuid, column_name) BUCKETS 10
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS _statistics_.multi_column_statistics (
    table_id BIGINT NOT NULL,
    column_ids VARCHAR(65530) NOT NULL,
    db_id BIGINT,
    table_name VARCHAR(65530),
    column_names VARCHAR(65530),
    ndv BIGINT,
    update_time DATETIME
)
PRIMARY KEY (table_id, column_ids)
DISTRIBUTED BY HASH (table_id, column_ids) BUCKETS 10
PROPERTIES ("replication_num" = "1");
