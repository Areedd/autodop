CREATE DATABASE IF NOT EXISTS autodop_corrected_env;

CREATE TABLE IF NOT EXISTS autodop_corrected_env.store_batch_stage (
    feed_id INT NOT NULL,
    entity_sk BIGINT NOT NULL,
    period_key INT NOT NULL,
    measure DECIMAL(18, 2) NULL
)
DUPLICATE KEY(feed_id, entity_sk)
DISTRIBUTED BY HASH(feed_id, entity_sk) BUCKETS 192
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS autodop_corrected_env.catalog_batch_stage (
    feed_id INT NOT NULL,
    entity_sk BIGINT NOT NULL,
    period_key INT NOT NULL,
    measure DECIMAL(18, 2) NULL
)
DUPLICATE KEY(feed_id, entity_sk)
DISTRIBUTED BY HASH(feed_id, entity_sk) BUCKETS 192
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS autodop_corrected_env.web_batch_stage (
    feed_id INT NOT NULL,
    entity_sk BIGINT NOT NULL,
    period_key INT NOT NULL,
    measure DECIMAL(18, 2) NULL
)
DUPLICATE KEY(feed_id, entity_sk)
DISTRIBUTED BY HASH(feed_id, entity_sk) BUCKETS 192
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS autodop_corrected_env.single_shard_feed (
    group_key INT NOT NULL,
    sequence_key BIGINT NOT NULL,
    measure BIGINT NOT NULL
)
DUPLICATE KEY(group_key, sequence_key)
DISTRIBUTED BY HASH(group_key) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE IF NOT EXISTS autodop_corrected_env.single_shard_wide_feed (
    group_key INT NOT NULL,
    sequence_key BIGINT NOT NULL,
    event_key VARCHAR(512) NOT NULL
)
DUPLICATE KEY(group_key, sequence_key)
DISTRIBUTED BY HASH(group_key) BUCKETS 1
PROPERTIES ("replication_num" = "1");
