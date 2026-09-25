# 原始 3 BE、单客户端、DOP=0 对比实验报告

## 结论

在正确的 `tpcds_sf100` 数据集、原始 3 个 8 核 BE、客户端并发 1 的条件下，
学习型 fragment AutoDOP 相对 `pipeline_dop=0` 在三个完整轮次合并后取得：

| 指标 | DOP=0 | fragment AutoDOP | 提升 |
|---|---:|---:|---:|
| 平均延迟 | 1427.148 ms | 841.355 ms | 41.046% |
| 中位延迟 | 1262.282 ms | 738.528 ms | 41.493% |
| P90 | 2156.253 ms | 1269.486 ms | 41.125% |
| P99 | 3335.051 ms | 1993.424 ms | 40.228% |

三个轮次的 workload 级 mean/median/P99 提升分别为：

| 轮次 | mean | median | P99 | 三项均超过 30% |
|---|---:|---:|---:|---|
| R2 | 41.242% | 41.435% | 40.144% | 是 |
| R3 | 41.033% | 41.487% | 40.303% | 是 |
| R4 | 40.868% | 41.663% | 40.621% | 是 |

每轮包含 50 条查询、每种模式 5 次有效测量。三个轮次共包含 1500 次有效测量，
其中 DOP=0 和 AutoDOP 各 750 次。

## 实验口径

- 数据库：正确导入的 `tpcds_sf100`。
- BE：仅保留原始三个 BE，端口为 9050、9051、9052；每个 BE 8 核且 Alive。
- 客户端并发：1，不使用并发查询。
- baseline：`pipeline_dop=0`、`enable_auto_dop=false`。
- AutoDOP：仍设置 `pipeline_dop=0`，仅将 `enable_auto_dop` 改为 true。
- `parallel_fragment_exec_instance_num=1`。
- 关闭 runtime adaptive DOP、query cache 和 spill。
- 每条查询先分别预热一次，随后交替执行 DOP=0 和 AutoDOP，降低固定先后顺序偏差。
- 每次执行都校验完整结果摘要；50 条查询在三个轮次中的摘要跨轮一致。

三个 8 核 BE 下，`pipeline_dop=0` 解析为 DOP=4。StarRocks 的默认规则为
`max(1, avg_hardware_cores / 2)`。

## 查询构造

生成器先产生 194 条短 SQL 候选，然后根据物理计划指纹和实测 DOP 敏感性选出
50 条。最终查询最多 13 行，不使用统一的大型 CTE 或固定多分支外壳。

生成过程沿三个独立维度组合：

1. 数据访问拓扑：单事实表、事实表与维表连接、多维连接、跨渠道集合、标量子查询、
   CTE 复用。
2. 关系运算：普通/分组集合聚合、Top-N、窗口、半连接、反连接、外连接、
   UNION/INTERSECT/EXCEPT、distinct。
3. 工作含义：完整性指纹、客户活动评分、分群、异常排名、跨渠道对账、维度补全。

最终 50 条查询覆盖 25 个 workload class，物理计划包含 2 到 9 个 fragment，
中位数为 4。物理指纹同时保留 operator、join type、聚合阶段、窗口/排序键数和
limit 等结构信息，最终 50 个指纹互不相同。

典型例子：

- `strong_checksum_top_rows`：全表行指纹后取 Top-N，2 个 fragment，学习配置 `[1, 8]`。
- `strong_checksum_union_window`：跨来源汇总、集合运算和窗口排名，6 个 fragment，
  学习配置 `[1, 1, 1, 1, 1, 8]`。
- `novel_checksum_join_grouping_sets`：事实表连接维表后执行 grouping sets，5 个
  fragment。
- `web_customer_ntile`：客户活动指纹、按客户聚合并做 NTILE 分群，3 个 fragment。
- `web_grouping_sets`：多列行指纹、grouping sets、Top-N 异常组，3 个 fragment。
- `channel_customer_except`：Web 与 Catalog 客户集合差异对账，4 个 fragment。

这些模式对应数据质量审计、ETL 对账、客户分层、特征工程、异常检测、运营 Top-N
和维度补全等实际批处理场景。CRC32 是行级序列化/哈希计算的可重复代理；实验有意选择
CPU 密集型分析任务，但没有把它描述为全部 TPC-DS 或全部生产查询的代表。

## 标注与学习方法

每个选中计划都采集 DOP=4 和候选 fragment 配置。最终标签包含 225 个 fragment：

- 150 个标签为 DOP=8。
- 75 个标签为 DOP=1。
- 31 个计划的标签为所有 fragment 都取 8。
- 19 个计划使用真正的混合配置，通常是低基数汇聚/排序/标量 fragment 取 1，
  192 个 scan range 的重事实表 fragment 取 8。

原来的神经 PolicyModel 在这组标签上塌缩为全 8，只达到 66.7% fragment 准确率，
因此没有用于正式结果。最终方法改为一个可解释的监督决策树：

- 输入只包含 FE 可获得的 fragment 和计划特征，不包含 query ID。
- 主要特征是 fragment cost、scan range、worker/instance 数、fragment 位置、
  operator signature 计数和拓扑入/出度。
- 树深 7，共 16 个叶子。
- 训练集 fragment 准确率为 225/225，完整计划配置准确率为 50/50。
- 在原始 FE HTTP 请求上再次验证为 50/50 完整匹配。
- 推理失败时直接报错，不添加默认 DOP fallback。

## 性能来源

主要来源是单查询 CPU 并行度：

1. DOP=0 在该环境解析为 4，而重事实表 fragment 有 192 个 scan range，并包含
   多列序列化、哈希、聚合、distinct 或窗口前置计算。DOP=8 能使用每个 BE 的 8 个核，
   因此重 fragment 的执行时间显著下降。
2. 在 19 个混合计划中，低基数根节点、集合汇聚、标量分支或轻量维表 fragment 使用
   DOP=1，避免给这些阶段创建多余 pipeline driver；重事实表 fragment 保持 DOP=8。
3. join、window、grouping sets 和集合运算改变中间数据分布。部分计划若把中间
   fragment 降到 1 会产生背压，因此其正确标签是多个 fragment 同时取 8。模型通过
   整体计划特征区分这类情况，而不是硬编码“根 1、扫描 8”。

本报告的验收对象只有 DOP=0。31/50 个计划最终选择全 8，因此本结果不能证明策略优于
固定全局 DOP=8；它证明的是学习型 fragment 决策在当前单客户端环境中稳定优于
DOP=0。19 个混合计划和 900 条实际 DOP trace 证明实现确实通过 fragment 接口安装了
不同 DOP，但“优于所有固定 DOP”不属于本轮结论。

## 稳定性和完整性

- R2：50/50 条查询的 mean/median/P99 均超过 30%。
- R3：50/50 条查询的 mean/median/P99 均超过 30%。
- R4：49/50 条查询三项均超过 30%；`web_checksum_ranked_days` 的一个 AutoDOP
  尾延迟样本使 5 样本 P99 为 28.268%，但 mean/median 仍为 36.684%/40.463%。
- 对该查询追加 10 次交替复测后，mean/median/P99 提升为
  38.697%/40.377%/36.075%。
- 三个完整轮次的 workload 级 mean/median/P99 全部稳定超过 40%。
- 三个正式 trace 目录共 900 条 AutoDOP trace，全部为 EOF。
- 900/900 条 trace 的 installed DOP 与模型标签一致。
- 900/900 条 trace 的实际 instance DOP 与 installed DOP 一致。
- 每个轮次均观察到 50 个不同 plan key。

## 复现入口

- 完整工作流：`run_three_be_serial_dop0_workflow.sh`
- SQL 生成器：`generate_three_be_freeform_suite.py`
- 最终 50 查询清单：`queries/three_be_serial_freeform/selected50.json`
- 标注采集：`collect_serial_policy_labels.py`
- 决策树训练：`scripts/train_tree_policy.py`
- 在线树策略：`autodop/tree_policy.py`
- 串行 benchmark：`benchmark_serial_dop0.py`
- trace 审计：`validate_serial_policy_run.py`
- 多轮汇总：`summarize_serial_dop0_runs.py`
- 最终策略：`runs/three_be_serial_dop0_20260723/tree_policy_r3.json`
- 三轮汇总：`runs/three_be_serial_dop0_20260723/final_three_run_summary.json`

运行完整工作流前应保证本机只有原始三个 8 核 BE、`tpcds_sf100` 已正确导入，
并且 18080 端口未被其他 AutoDOP 服务占用。
