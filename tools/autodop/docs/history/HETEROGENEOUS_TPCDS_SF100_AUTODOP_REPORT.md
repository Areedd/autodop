# TPC-DS SF100 异构高并发 Fragment AutoDOP 实验

## 结论

在已修复的 `tpcds_sf100` 上构建了一个中央批处理与单核 edge
校验并存的 51 并发请求窗口。两组独立实验各执行 1 次预热和 10
次测量。合并 20 个测量样本后，fragment AutoDOP 的批次完成延迟为：

| 模式 | 平均延迟 (ms) | 中位数 (ms) | P90 (ms) | P99 (ms) |
|---|---:|---:|---:|---:|
| 全局 DOP=1 | 13528.324 | 13528.250 | 13569.758 | 13582.057 |
| 全局 DOP=2 | 6802.589 | 6799.877 | 6828.808 | 6833.465 |
| 全局 DOP=4 | 3480.691 | 3482.803 | 3491.594 | 3494.669 |
| 全局 DOP=8 | 3702.838 | 3674.730 | 3887.359 | 3901.167 |
| Fragment AutoDOP | **2262.655** | **2261.102** | **2296.829** | **2317.068** |

相对每个固定全局 DOP，fragment AutoDOP 的提升为：

| 对照 | 平均提升 | 中位数提升 | P99 提升 |
|---|---:|---:|---:|
| 全局 DOP=1 | 83.275% | 83.286% | 82.940% |
| 全局 DOP=2 | 66.738% | 66.748% | 66.092% |
| 全局 DOP=4 | **34.994%** | **35.078%** | **33.697%** |
| 全局 DOP=8 | 38.894% | 38.469% | 40.606% |

两组独立实验各自最差的比较值分别为 34.303% 和 33.456%，不是合并
样本后才出现的优势。这里的“延迟”是同一到达屏障后 51 个请求全部完成的
批次 makespan，适用于微批、调度周期和刷新窗口 SLO；不能把它误称为单个
随机请求的延迟。

## 实验环境

- 主数据集：正确的 `tpcds_sf100.store_sales`，共 287,997,024 行。
- batch 层：三个 8 核 BE 标记为 `tier:batch`；`batch_event_fact`
  使用 192 buckets，正式 trace 中事实扫描实际分布在两个 batch BE。
- edge 层：独立的 1 核、8 GiB BE，标记为 `tier:edge`；
  `edge_hot_feed` 有 2,000,000 行和 1 个 bucket。
- edge BE 的 pipeline driver admission 上限设为 32768。这个配置只避免
  固定高 DOP 被直接拒绝；所有模式共享相同上限，并不单独优待 AutoDOP。
- 所有会话均关闭 query cache、runtime adaptive DOP 和 spill，
  `parallel_fragment_exec_instance_num=1`。
- 固定对照关闭 AutoDOP 并设置 `pipeline_dop=1/2/4/8`。fragment 模式以
  `pipeline_dop=8` 为基准，由服务为每个 fragment 决定 1 或 8。

每个并发窗口由两类工作构成：

1. 一个 batch 事件哈希任务。它读取 SF100 的 287,997,024 行销售事实，对
   customer、item、ticket 做 CRC32 摘要，代表脱敏、去重键生成、CDC
   校验和或特征编码。
2. 50 个 edge checkpoint 校验任务。每个任务包含 32 个分片集合输入：
   16 个已到达窗口实际读取数据，16 个是下一接入周期尚未到达的窗口。
   每条正式 trace 扫描 1,130,496 行，结果不是靠完全空扫得到。

## Fragment 策略

策略不识别 SQL 文件名或 query ID，仅使用计划和调度位置特征：

1. fragment 的所有 worker 都属于已声明的受限 edge worker 时，DOP=1。
2. 含 `EXCEPT` 的 fragment 及其输入闭包使用 DOP=1，避免集合协调子图内部
   出现不匹配的高并行度。
3. 其余 fragment 若 cost 至少为 10,000,000 或 scan ranges 至少为 16，
   使用 DOP=8。
4. 其余轻量 fragment 使用 DOP=1。

最终 batch 计划稳定包含两个 fragment：大表 scan/aggregate 为 DOP=8，
轻量 finalize 为 DOP=1。edge 计划稳定包含 34 个 fragment，全部为 DOP=1。
这证明结果来自同一批次内的 fragment 级差异，而不是把整批工作统一调成
另一个全局 DOP。

## 为什么固定全局 DOP 无法达到同样结果

两类工作具有方向相反的 DOP 曲线。

- batch 哈希是低状态、CPU 密集且具有 192 个 scan ranges 的工作。单独
  运行时，DOP=1/2/4/8 的中位延迟约为
  13.51/6.78/3.46/2.02 秒，DOP=8 最合适。
- edge 校验在单核节点上每个输入只有一个 scan range。提高 DOP 不增加
  数据并行度，却增加 pipeline driver、exchange 和集合协调。代表性 SQL
  单独运行时，DOP=1 中位约 37.0 ms，DOP=8 约 59.5 ms。
- 在 50 路并发下，这个差异被放大。全局 DOP 低时，中央 batch 成为关键
  路径；全局 DOP 高时，edge driver 和交换协调成为关键路径。固定 DOP=4
  只能在两者之间折中，仍需约 3.48 秒。
- fragment AutoDOP 同时为 batch scan 选择 8、为 edge 子图选择 1，使两条
  关键路径都落在约 2.0–2.3 秒，因而超过所有固定选择。

## Trace 与正确性

两组正式实验共验证 1122 条 fragment trace，其中 1100 条 edge 查询、
22 条 batch 查询（包含预热）：

- 1122/1122 为 `EOF`；
- 0 error，0 spill；
- 策略决策、FE 安装值和 BE 实际 instance DOP 的不一致数为 0；
- edge 模式为 34 个 DOP=1 fragment；
- batch 模式为 1 个 DOP=1 和 1 个 DOP=8 fragment；
- benchmark 对每类查询计算结果 digest，五种模式间无结果漂移。

决策延迟中位数 12 ms，P99 76 ms。高并发使这些 HTTP 决策重叠；该开销已经
包含在 fragment makespan 中，没有从结果里扣除。

## 可迁移的构建规则

1. **先找相反的 DOP 最优点。** 至少一类任务必须随 DOP 明显加速，另一类
   在高 DOP 下因有限 scan ranges、单核资源或协调算子明显退化。若所有查询
   都偏好相同 DOP，fragment 策略不可能稳定领先最优全局 DOP 30%。
2. **让计划特征解释选择。** 高 DOP 类应具有大量 scan ranges 和足够大的
   CPU cost；低 DOP 类应具有明确的资源上限或串行/协调子图。不要按 query
   ID、SQL 文本或已知结果写规则。
3. **用资源标签表达真实异构性。** 分支机构、IoT gateway、边缘节点和中央
   数仓本来就具有不同核数。worker capacity 应进入策略，而不是假设每个
   fragment 所在节点同构。
4. **按关键路径选择并发配比。** 记 batch 和 edge 在 DOP `d` 下的窗口时间
   为 `B(d)`、`E(d,N)`，选择并发数 `N` 使
   `max(B(8), E(1,N)) <= 0.7 * min_d max(B(d), E(d,N))`。本环境用单条曲线
   校准后选择 `N=50`，而不是不断试到偶然出现一个快值。
5. **包含真实扫描和状态过渡。** edge 查询同时覆盖已到达与未来窗口，
   避免把全空表、恒假谓词或错误数据当作性能来源。
6. **把失败视为失败。** admission 上限必须允许所有固定模式完成；OOM、
   超时、driver 拒绝和错误结果都不能计作性能优势。
7. **独立重复并验证尾延迟。** 至少两组独立运行、轮换模式顺序、预热不计入
   统计，并同时报告平均、中位、P90、P99、结果 digest 和实际 DOP trace。

这套模式代表中央事实表 ETL/脱敏与边缘接入校验同时发生的混合分析系统，
也适用于零售门店上传、IoT checkpoint、区域 CDC 接入和多租户微批窗口。
它不证明标准 TPC-DS 查询在同构、低并发环境下天然有 30% 优势；之前的
标准 SF10/SF100 探索也没有得到这个结论。优势的必要条件是工作负载中确实
存在无法由一个全局 DOP 同时满足的资源与计划冲突。

## 复现

正式工作流：

```bash
tools/autodop/run_heterogeneous_tpcds_sf100_workflow.sh
```

默认执行两组各 10 次测量。可用 `EDGE_CPU` 指定宿主机上的空闲逻辑核；
缩短冒烟可设置 `REPETITIONS=1`。脚本会检查正确 SF100 行数、创建并验证
两张隔离表、启动策略服务、轮流执行五种模式、合并结果并校验全部 trace。

关键文件：

- `setup_heterogeneous_tpcds_sf100.sql`：表结构与位置约束。
- `queries/heterogeneous_cluster/querybatch_event_hash.sql`：batch SQL。
- `queries/heterogeneous_cluster/queryedge_checkpoint_validation.sql`：edge SQL。
- `queries/mixed_batch_workload/workload.json`：51 并发工作负载。
- `run_heterogeneous_tpcds_sf100_workflow.sh`：端到端工作流。
- `runs/heterogeneous_cluster_autodop_20260723/representative_combined_summary.json`：
  20 轮合并结果。
- `runs/heterogeneous_cluster_autodop_20260723/representative_trace_validation.json`：
  正式 trace 验证。
