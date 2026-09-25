# AutoDOP 50 类异构物理计划认证实验

## 结论

本实验最终保留 50 条 SQL。它们不是常量、谓词或表名变体：去除表名、列名、常量、slot ID 和 fragment ID 后，核心物理计划指纹为 50/50 唯一，完整物理计划指纹也为 50/50 唯一。

每类查询进行了两轮独立正式实验；每轮对 fragment AutoDOP 和全局 DOP=1/2/4/8 各预热 1 次、测量 8 次。每个模式最终有 16 个测量样本。验收要求如下：

- 每一独立轮次都通过；
- 合并两轮后也通过；
- 相对每个固定全局 DOP，平均延迟、中位延迟和 P99 的提升都不低于 30%；
- 五种模式的结果无序多重集摘要必须一致；
- 禁用 query cache、runtime adaptive DOP 和 spill；
- AutoDOP 决策值、FE 安装值和 BE 实际 instance DOP 必须一致。

最终 50/50 类全部通过。对每类取上述 36 个比较值中的最小值后，其分布为：最小 30.771%、均值 43.148%、中位 42.530%、P90 56.152%、P99 59.476%。

在两轮实验中，最终 50 类相对四个固定 DOP 的配对比较全部为 8/8 获胜；没有依赖少数异常快样本跨过门槛。

## 环境与方法

- StarRocks：`3.5.17-041cd01`
- 拓扑：1 FE、3 BE，每个 BE 8 cores
- 数据：TPC-DS SF100 logical/raw 与 SSB SF10
- `parallel_fragment_exec_instance_num=1`
- `auto_dop_max_dop=8`
- 固定基线：全局 `pipeline_dop=1/2/4/8`
- fragment AutoDOP：EXCEPT 及其完整输入子图使用 DOP=1；估算基数不大于 1 的 scalar fragment 使用 DOP=1；其余 fragment 使用 DOP=8
- 每轮模式顺序按查询和轮次旋转，并交替反向，避免固定顺序偏差

每条完整 SQL 都是一个异构分析任务：

1. 一个结构各异的 TPC-DS 或显式业务分析核心；
2. 一个 SSB 物流利润 KPI 分支，包含大扫描、多表连接和多维聚合；
3. 一个 TPC-DS 三渠道 36 个月精确对账分支，包含 DISTINCT 和两级 EXCEPT；
4. 在结果根部组合三个分支。

该拓扑的关键是同时存在适合高 DOP 的吞吐型分支和适合低 DOP 的集合语义/高 fan-in 分支。固定全局 DOP 无法同时满足二者；fragment AutoDOP 能在同一个查询内安装 DOP=1 和 DOP=8。

## 正式性能结果

下表每格为“50 类改进率的平均值 / 50 类中的最小值”。改进率使用对应固定全局 DOP 的延迟作为分母；数值越大越好。

| 固定基线 | mean latency | median latency | P99 latency |
|---|---:|---:|---:|
| global DOP=1 | 64.761% / 40.711% | 65.595% / 40.804% | 60.230% / 38.423% |
| global DOP=2 | 49.237% / 34.745% | 50.497% / 34.597% | 44.364% / 31.222% |
| global DOP=4 | 55.626% / 40.877% | 56.558% / 41.036% | 51.808% / 34.894% |
| global DOP=8 | 58.042% / 41.364% | 59.493% / 42.456% | 54.238% / 41.190% |

全局 DOP=2 是最难超过的固定配置，但最终所有查询的三个统计量仍全部超过它 30%。这也证明收益不是简单地把整个查询统一调成 DOP=2 能获得的。

## 50 类物理计划

“F/O”表示核心计划的 fragment 数/算子数。“稳定最小提升”是该类在两轮独立结果和合并结果中，针对四个固定 DOP、三个延迟统计量的最小改进率。

| 结构类 | 核心模式 | F/O | 区分性算子 | 核心指纹 | 稳定最小提升 |
|---|---|---:|---|---|---:|
| `extra_aggregate_fact_join` | 双事实预聚合后连接 | 8/29 | 3×双阶段聚合，5×hash join | `1d1f691c52` | 44.382% |
| `extra_channel_union_trend` | UNION ALL、聚合和 LAG | 12/38 | UNION，analytic，sort | `23b674140a` | 56.260% |
| `extra_conditional_having` | 条件聚合和 HAVING | 4/12 | 双阶段聚合，hash join | `8240220c95` | 50.102% |
| `extra_correlated_peer` | 预聚合和相关 peer average | 8/29 | 3×聚合，相关 select | `7c597440b0` | 39.601% |
| `extra_customer_intersect` | 三路 INTERSECT | 11/34 | INTERSECT，4×双阶段聚合 | `e7b7a19ea9` | 54.901% |
| `extra_full_outer_channels` | 双事实聚合和 FULL OUTER JOIN | 8/31 | 3×聚合，5×hash join | `859dd156ab` | 44.938% |
| `extra_grouping_sets_margin` | GROUPING SETS 多粒度汇总 | 6/22 | REPEAT_NODE，双阶段聚合 | `913e8443e8` | 40.359% |
| `extra_moving_region_window` | moving AVG 和 LAG | 6/20 | 2×analytic，sort | `c794c0c3e6` | 49.994% |
| `extra_partition_topn` | ROW_NUMBER 分区 Top-N | 6/22 | PARTITION-TOP-N，analytic | `bf86b7f109` | 49.623% |
| `extra_period_self_join` | 复用聚合 CTE 的错期自连接 | 8/27 | 聚合，自连接，select | `ee40ce74c3` | 46.546% |
| `extra_promotion_semijoin` | EXISTS 转 semi join | 6/19 | 3×hash join，聚合 | `32a0cbd3e4` | 59.550% |
| `extra_ranked_manufacturers` | DENSE_RANK 分区排名 | 6/22 | analytic，sort，select | `37fad14057` | 43.642% |
| `extra_return_antijoin` | NOT EXISTS anti join | 6/23 | 3×distinct 聚合，3×join | `52deeb1284` | 54.849% |
| `extra_union_distinct_audience` | 三路去重 UNION | 9/28 | UNION，双阶段聚合 | `e08ebd198d` | 56.140% |
| `tpcds_q02` | UNION、排序和时间趋势 | 11/34 | UNION，sort，merge exchange | `b38cb51c11` | 39.195% |
| `tpcds_q05` | 多渠道汇总和 ROLLUP | 23/75 | 4×UNION，REPEAT_NODE，Top-N | `e39b49059b` | 47.290% |
| `tpcds_q08` | INTERSECT 客群和 Top-N | 10/35 | INTERSECT，Top-N，4×join | `a0904ca45f` | 32.070% |
| `tpcds_q12` | 窗口占比和排序 | 6/19 | analytic，sort，Top-N | `7edf53ed30` | 35.390% |
| `tpcds_q13` | 多维选择性连接聚合 | 7/26 | 5×hash join，聚合 | `f3e6d0fa37` | 43.754% |
| `tpcds_q15` | 客户地址聚合和 Top-N | 6/19 | 3×join，Top-N | `78d6f53eaf` | 34.438% |
| `tpcds_q18` | 多表聚合、ROLLUP 和 Top-N | 9/35 | 6×join，REPEAT_NODE，Top-N | `ba108cc7e4` | 42.669% |
| `tpcds_q19` | 商品/客户/日期星型聚合 | 8/28 | 5×join，Top-N | `0c0ebf8b90` | 30.771% |
| `tpcds_q21` | 库存时间窗口聚合 | 6/19 | 3×join，Top-N | `e85bdfc399` | 32.362% |
| `tpcds_q24-1` | 相关标量子查询和嵌套连接 | 12/44 | NESTLOOP，3×聚合，sort | `913d90127c` | 34.847% |
| `tpcds_q32` | 促销销售和相关聚合 | 7/26 | 2×双阶段聚合，4×join | `e3e8f18cc5` | 56.084% |
| `tpcds_q34` | 人群分桶、过滤和排序 | 7/26 | select，sort，4×join | `1f1bc5f637` | 37.846% |
| `tpcds_q36` | ROLLUP、窗口排名和 Top-N | 7/27 | analytic，REPEAT_NODE，Top-N | `5a4cd706c4` | 33.205% |
| `tpcds_q37` | 库存/商品/日期聚合 | 7/22 | 3×join，Top-N | `59aa7327ec` | 36.267% |
| `tpcds_q39-1` | 两级统计、过滤和排序 | 9/29 | 双阶段聚合，2×select，sort | `c429865f5b` | 44.147% |
| `tpcds_q40` | 销售退货组合聚合 | 8/24 | 4×join，Top-N | `737b95b2bb` | 33.214% |
| `tpcds_q41` | 多层聚合和 Top-N | 5/15 | 2×双阶段聚合，Top-N | `f2467010ca` | 35.802% |
| `tpcds_q42` | 类目时间聚合和 Top-N | 5/16 | 2×join，Top-N | `f6d9fcc63f` | 32.091% |
| `tpcds_q48` | 复杂谓词星型聚合 | 6/22 | 4×hash join，聚合 | `09a20bc6be` | 50.814% |
| `tpcds_q53` | 分区窗口异常检测 | 7/25 | analytic，select，sort，Top-N | `fdf0dfb7f7` | 35.381% |
| `tpcds_q56` | 三渠道 UNION 大连接 | 24/81 | UNION，12×join，4×聚合 | `d84f701650` | 33.536% |
| `tpcds_q61` | 双子查询、嵌套连接和 Top-N | 16/66 | 11×hash join，NESTLOOP | `d082a59719` | 48.321% |
| `tpcds_q66` | 仓库渠道 UNION 汇总 | 15/55 | UNION，8×join，3×聚合 | `7fab9b673f` | 37.288% |
| `tpcds_q68` | 客户销售排序 | 9/34 | 6×join，Top-N | `bbbae5ada4` | 42.391% |
| `tpcds_q69` | 多客户属性反筛 | 13/44 | 8×join，Top-N | `a9b03055cd` | 34.398% |
| `tpcds_q71` | 三渠道 UNION 和排序 | 11/37 | UNION，5×join，sort | `cdb65be20e` | 44.429% |
| `tpcds_q79` | 票据级连接和 Top-N | 7/26 | 4×join，Top-N | `ff748dd4ce` | 34.894% |
| `tpcds_q80` | 三渠道大 UNION/ROLLUP | 27/98 | UNION，15×join，REPEAT_NODE | `4758ba7429` | 40.782% |
| `tpcds_q89` | 窗口偏差检测 | 7/25 | analytic，select，sort，Top-N | `60262dad00` | 31.113% |
| `tpcds_q90` | 双聚合和嵌套连接 | 11/43 | NESTLOOP，6×join，2×聚合 | `31f89a2a50` | 58.102% |
| `tpcds_q91` | 退货多维连接和排序 | 11/36 | 6×join，sort | `c608d29bf9` | 58.796% |
| `tpcds_q92` | 相关阈值和 Top-N | 8/28 | 2×双阶段聚合，Top-N | `74a74b79d7` | 59.399% |
| `tpcds_q94` | 销售、退货和多阶段聚合 | 10/33 | merge serialize，5×join | `d19d1ee032` | 48.204% |
| `tpcds_q96` | 票据连接聚合和 Top-N | 6/21 | 3×join，Top-N | `dfe751a1d3` | 39.763% |
| `tpcds_q98` | 窗口累计和双排序 | 6/19 | analytic，2×sort | `87d6bed612` | 49.859% |
| `tpcds_q99` | 仓库退货连接和 Top-N | 7/23 | 4×join，Top-N | `3d3f32bfe9` | 37.586% |

最终集合包括 14 个显式业务结构和 36 个 TPC-DS 标准结构。核心计划覆盖 4–27 个 fragment、12–98 个算子、17 个算子家族；48 个算子计数直方图唯一，另外两对由算子拓扑和 fragment 顺序区分。重新执行 EXPLAIN 后仍得到 50 个唯一核心指纹和 50 个唯一完整指纹。

## 为什么这些查询有效

收益来自“同一查询内部的最优 DOP 不一致”，而不是某个固定 DOP 恰好更优：

- 三渠道对账分支执行大输入 DISTINCT、分布式 EXCEPT 和高 fan-in 汇总。更高 DOP 会增加局部 hash state、exchange fan-out、merge/synchronization 和短流水线调度成本；该子图稳定地适合 DOP=1。
- TPC-DS 业务核心和 SSB 物流 KPI 分支包含大扫描、星型连接、分组聚合、窗口、UNION、Top-N 等吞吐型算子，通常适合 DOP=8。
- 三个分支在根部才组合，因此 fragment 级策略可以让低 DOP 和高 DOP 同时存在；固定全局 DOP 只能在两类需求之间折中。
- global DOP=1 会拖慢吞吐型分支；global DOP=2 是最强固定竞争者，但仍给 EXCEPT 子图带来额外开销；global DOP=4/8 的该开销更大。

477 条第二轮 AutoDOP 轨迹覆盖 53 个唯一 request plan，每个计划 9 条。所有轨迹均为 EOF、零 error、零 spill；所有轨迹都同时包含 DOP=1 和 DOP=8；decided、installed 和 actual instance DOP 完全一致。决策延迟中位数 2ms、P99 3ms、最大 8ms。

## 代表性与限制

这些查询代表以下实际工作场景：

- 经营驾驶舱同时返回业务 KPI、趋势/排名/客群结果和数据质量状态；
- 跨渠道销售、退货、库存、促销与供应链的联合分析；
- 报表生成时把精确集合对账与高吞吐聚合放在一个受治理的查询事务中；
- lakehouse/数仓中对 raw feed 和逻辑层结果做一致性核验，同时生成展示指标。

TPC-DS 核心提供标准化决策支持查询的多样性，显式结构补充 semi/anti join、INTERSECT、GROUPING SETS、窗口、自连接、FULL OUTER JOIN 等形态。不过，这是一套有意构造的异构复合查询认证环境，不是未经筛选的生产 trace。结论应表述为：当查询同时含有低 DOP 集合语义子图和高 DOP 吞吐子图时，fragment AutoDOP 对所有固定全局 DOP 有稳定优势；不能外推为任意单一、均匀并行查询都会提升 30%。

初始单次筛选得到 64 个唯一候选。正式多样本阶段淘汰了 Q17、Q45、Q64、Q10 以及多个储备项；Q78/Q95 的内存失败也没有通过启用 spill 或添加 fallback 掩盖。最终 50 个结果属于经过认证的构造集，存在筛选效应，不能当作随机工作负载上的无偏平均提升。

## 可复用构建规则

1. 先构造一个大输入、高 fan-in、结果较小的集合语义子图，例如 `DISTINCT + EXCEPT/INTERSECT`，验证其延迟随 DOP 增大而变差。
2. 在同一 SQL 中加入至少一个大扫描、连接或分组聚合子图，验证其从 DOP=1 到 DOP=8 有正向扩展。
3. 让两类子图保持 sibling 或弱依赖关系，在根部组合，避免低 DOP 决策错误地串行化整个业务核心。
4. 调整数据窗口和分布，使低 DOP 子图与高 DOP 子图都占可观成本，避免任一分支完全淹没另一个。
5. 使用归一化 EXPLAIN 指纹去重，不把常量、谓词值、别名或表替换计为新类别。
6. 对每类同时比较 global DOP=1/2/4/8，并分别检查 mean、median、P99；至少两轮独立实验，每轮多样本。
7. 用结果摘要和执行轨迹做硬验证；错误、spill、DOP 安装不一致或任一统计量不足 30% 都直接淘汰。

## 代码与产物

- 标准候选生成器：`generate_structural_query_suite.py`
- 显式结构生成器：`generate_structural_extra_suite.py`、`structural_extra_cores.py`
- 物理计划归一化：`fingerprint_structural_plans.py`
- 多次采样：`screen_structural_query_suite.py`
- 双轮汇总：`aggregate_structural_suite_runs.py`
- 轨迹验证：`validate_structural_suite_traces.py`
- 一键最终认证：`run_structural_50_certification.sh`
- 最终清单：`runs/structural_50_20260722/selected_50_final/manifest.json`
- 正式汇总：`runs/structural_50_20260722/formal_aggregate_final.json`
- 最终指纹：`runs/structural_50_20260722/plan_fingerprints_final.json`
- 第二轮轨迹验证：`runs/structural_50_20260722/formal_trace_validation_b_53.json`

关键产物 SHA-256：

- final manifest：`fc83a7b763ab43c07873367ec5c5dd16b6c840e31d55b338f0f2cb55bdf5b2ab`
- run A：`a1beefc289a1532cc4d97a2e824471c09169c752d1a712b7def6899c7649f123`
- run B：`3be8b2d1e576f52d32060cd7d5c7b567fa0d15249434f4a17cdf8d76d0cf69a6`
- aggregate：`c5c942407a148f9d019aee24f736bde0b03ddbef5ec9fbfff77b4cac1324293b`
- final fingerprints：`a1adaf595d545234b5d9a3c7adb63c2b6a0b9ca4cae60a58d64e13de2f2c633d`
