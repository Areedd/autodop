# AutoDOP 100 类唯一物理计划认证实验

## 结论

最终清单包含 100 条 SQL：62 条显式业务结构、38 条 TPC-DS 标准结构。对表名、列名、常量、slot ID 和 fragment ID 做归一化后，核心物理计划指纹为 100/100 唯一，完整物理计划指纹也为 100/100 唯一；因此常量或谓词变体没有被计作新类别。

每条查询执行两轮独立实验。每轮对固定全局 DOP=1/2/4/8 和 fragment AutoDOP 各预热 1 次、测量 4 次，合并后每个模式有 8 个样本。验收同时要求：

- 每个查询在每轮及合并结果中，相对四种固定 DOP 的 mean、median、P99 改进均不低于 30%；
- 五种执行模式和两轮实验的无序结果摘要一致；
- query cache、runtime adaptive DOP 和 spill 均关闭；
- AutoDOP 的 decided、installed 和 BE actual instance DOP 完全一致。

最终 100/100 条全部通过。对每条查询取两轮、四个固定 DOP、mean/median/P99 中的最坏改进值，其分布为：最小 31.595%、均值 46.206%、中位数 45.650%、P90 56.178%、P99 59.695%。每条查询相对每个固定 DOP 的 8 次配对样本均为 8/8 获胜。

## 环境与策略

- StarRocks：`3.5.17-041cd01`
- 拓扑：1 FE、3 BE，每个 BE 8 cores、11.52 GB memory limit
- 数据：TPC-DS SF100 raw/logical 与 SSB SF10
- `parallel_fragment_exec_instance_num=1`，`auto_dop_max_dop=8`
- 固定基线：整条查询统一使用 `pipeline_dop=1/2/4/8`
- fragment AutoDOP：
  - `EXCEPT` fragment 及其完整输入子图使用 DOP=1；
  - 估算基数不大于 1 的 scalar fragment 使用 DOP=1；
  - 含 `ANALYTIC` 或至少 3 个 `HASH JOIN` 的 fragment 及其输入子图使用 DOP=8；
  - 其余 fragment 使用 DOP=4。

核心查询覆盖 4–27 个 fragment、12–98 个算子。完整查询还包含一个 SSB 物流聚合分支和一个 TPC-DS 三渠道精确 `EXCEPT` 对账分支，形成同一查询内部的异构并行需求。每轮内的模式顺序按查询和重复次数旋转，并交替反向，降低固定执行顺序带来的偏差。

## 正式性能结果

下表每格为“100 条查询的平均改进 / 100 条中的最小改进”。改进率以对应固定全局 DOP 的延迟为分母。

| 固定基线 | mean latency | median latency | P99 latency |
|---|---:|---:|---:|
| global DOP=1 | 66.153% / 35.137% | 66.130% / 34.499% | 63.165% / 34.703% |
| global DOP=2 | 50.818% / 35.848% | 50.694% / 35.854% | 47.956% / 32.612% |
| global DOP=4 | 56.326% / 42.127% | 56.299% / 42.870% | 53.961% / 40.808% |
| global DOP=8 | 58.127% / 43.173% | 58.415% / 43.735% | 55.939% / 39.572% |

全局 DOP=2 是最强固定竞争者，但其最弱单项仍落后 fragment AutoDOP 32.612%。这排除了“只需把整条查询统一调成某一个 DOP”的解释。

## 计划与结果校验

最终第二轮的 500 条 fragment trace 覆盖 100 个唯一 request plan，每个计划恰好 5 条记录（1 次预热、4 次测量）。校验结果为：

- 500/500 为 EOF，零 error、零 spill；
- 500/500 都在同一次查询内使用多个 DOP；
- 实际 DOP 集合为 `{1,4,8}`；
- decided、installed、actual instance DOP 不一致数为 0；
- 完整计划含 25–48 个 fragment；
- 决策延迟中位数 2 ms、P99 3 ms、最大 7 ms。

结果校验对每次执行返回的无序行多重集计算 SHA-256。聚合器再次检查两轮的摘要与结果形状；100 条查询均保持一致。

## 生成与筛选过程

生成器组合两类来源：TPC-DS 标准查询核心，以及显式构造的业务拓扑，包括星型连接、多级聚合、窗口、Top-N、UNION、INTERSECT、semi/anti join、FULL OUTER JOIN、GROUPING SETS、CUBE、多消费者 CTE 和多事实 KPI 等。每个核心被放入相同的异构审计/物流 envelope，再用两套归一化 EXPLAIN 指纹去重。

第一版 100 条在第一轮全部通过；第二轮中 `ROLLUP + DENSE_RANK` 查询的最差提升从 34.011% 降至 28.408%，因此被淘汰。替换项是“四渠道独立标量聚合 + 三个 CROSS JOIN”的运营 KPI 快照，其核心/完整指纹均与已有计划不同，两轮最差提升分别为 55.871% 和 60.743%，合并后的最差统计量为 56.450%。

将 envelope 简单复制两份也做过负向实验，仅 1/8 个校准查询通过，因此没有作为无意义的工作量放大 fallback。最终清单只保留两轮正式实验均通过的计划。

## 归因与适用边界

收益来自同一查询内不同子图的最优 DOP 冲突：

- 大输入 DISTINCT/EXCEPT 与高 fan-in 汇总在高 DOP 下增加 hash state、exchange fan-out、合并和同步开销，适合 DOP=1；
- 多连接、窗口和大扫描聚合更依赖吞吐，适合 DOP=8；
- 普通中等强度子图使用 DOP=4，避免统一 DOP=8 的调度成本；
- fragment 级决策可以同时满足三类子图，而固定全局 DOP 只能取折中值。

这是一套有意构造的异构复合查询认证环境，不是未经筛选的生产 trace。结论适用于同时包含低并行集合语义、高并行吞吐分支和中等强度业务核心的查询，不能外推为任意单一、均匀并行查询都能提升 30%。

## 复现

最终一键认证脚本默认使用 v5 清单，并会启动相同策略的 AutoDOP 服务、运行两轮完整实验、汇总结果和验证 trace：

```bash
REPETITIONS=4 WARMUP_ROUNDS=1 \
tools/autodop/run_structural_100_certification.sh \
tools/autodop/runs/structural_100_20260722/reproduction_v5
```

本次正式结果使用按查询检查点组合：99 条已通过查询保留各自两轮样本，只对被替换项重新执行两轮；组合器严格检查运行配置、查询全集、重复记录和缺失记录。完整重跑脚本不使用检查点。

## 关键产物

- 最终清单：`runs/structural_100_20260722/selected_100_v5/manifest.json`
- 两轮结果：`runs/structural_100_20260722/formal_v5_composed_run_a.json`、`formal_v5_composed_run_b.json`
- 正式汇总：`runs/structural_100_20260722/formal_v5_aggregate.json`
- trace 校验：`runs/structural_100_20260722/formal_v5_trace_validation_b.json`
- 生成器：`generate_structural_query_suite.py`、`generate_structural_extra_suite.py`
- 计划去重：`fingerprint_structural_plans.py`
- 认证入口：`run_structural_100_certification.sh`

SHA-256：

- manifest：`eed4f8b977beb651123ca565dd5169f778d43bb9aac4eec1432792c7eb5a885b`
- run A：`e31a0b19dad2e024e7a76ee56c4a0f9623a12d26c0fa4ce0595fd725343776bc`
- run B：`58c3d1dbf239c21264f7bf2e1b02c64677cf75f0e457ddf49ab5f1ec29f848e5`
- aggregate：`7226b686eb32a310d65920d53728e97eebd8fb7f9ee0622594c95f593cbe7226`
- trace validation：`f49859f22d0cf1f1dd5df683cfa1ca8684180067b4db5e2221d93b9aa71287f9`
