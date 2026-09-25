CREATE DATABASE IF NOT EXISTS autodop_corrected_env;

CREATE TABLE IF NOT EXISTS autodop_corrected_env.batch_event_fact (
    customer_sk INT NULL,
    item_sk INT NULL,
    ticket_number INT NULL
)
DUPLICATE KEY(customer_sk, item_sk)
DISTRIBUTED BY HASH(customer_sk, item_sk, ticket_number) BUCKETS 192
PROPERTIES (
    "replication_num" = "1",
    "labels.location" = "tier:batch"
);

CREATE TABLE IF NOT EXISTS autodop_corrected_env.edge_hot_feed (
    group_key INT NOT NULL,
    sequence_key BIGINT NOT NULL,
    event_key VARCHAR(512) NOT NULL
)
DUPLICATE KEY(group_key, sequence_key)
DISTRIBUTED BY HASH(group_key) BUCKETS 1
PROPERTIES (
    "replication_num" = "1",
    "labels.location" = "tier:edge"
);
