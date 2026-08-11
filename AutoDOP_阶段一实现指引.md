# AutoDOP 阶段一实现指引：FE 调用 Python 后端设置 fragment 级 DOP 并采集验证数据

本文档基于当前目录下的 `AutoDOP_核心方法总结.md`，只覆盖第一阶段目标：先在 StarRocks FE 中打通“fragment 级 DOP 决策服务调用、DOP 安装、执行后数据采集”的基本框架。此阶段不实现论文中的 Graph Transformer、MCTS、蒸馏模型和负载修正，只保留后续接入这些能力所需的数据结构与接入点。

## 1. 阶段目标

第一阶段要实现以下闭环：

1. 启动一个独立 Python AutoDOP 后端。
2. FE 在设置 fragment 级 DOP 的安全窗口，把决策所需的 plan、fragment、placement、候选 DOP 等信息打包成 JSON 请求发送给 Python 后端。
3. Python 后端返回一组 fragment 级 DOP 配置。阶段一可以先只调整单个目标 fragment，其他 fragment 保持原 DOP。
4. FE 校验返回值，校验通过后在实例创建和 thrift 参数生成前安装到 `PlanFragment` 与 `FragmentInstance`。
5. 查询执行结束后，记录查询执行时间、每个 fragment 的可用于 DOP 调整的特征、原始 DOP、Python 返回 DOP、FE 实际安装 DOP、实例实际执行 DOP、查询状态和基础执行统计。

阶段一的验收标准：

- Python 后端返回 `fragment_id -> dop` 后，`EXPLAIN SCHEDULER` 或 trace 文件中能看到目标 fragment 的 DOP 发生变化。
- BE 收到的 `TExecPlanFragmentParams.pipeline_dop` 与 trace 中记录的实际实例 DOP 一致。
- Python 后端超时、返回非法 DOP 或关闭时，查询能自动回退到 StarRocks 原生 DOP 策略，不影响正确性。
- 每条被 AutoDOP 处理过的查询都能落一条 JSONL 采集记录，可用于后续离线训练数据生成。

## 2. 当前代码中的关键位置

阶段一主要涉及 FE 侧这些代码路径：

| 作用 | 当前文件 | 说明 |
|---|---|---|
| fragment 级逻辑 DOP | `fe/fe-core/src/main/java/com/starrocks/planner/PlanFragment.java` | `pipelineDop` 字段和 `setPipelineDop()` 是 FE plan 级 DOP 的入口。 |
| 调度预处理 | `fe/fe-core/src/main/java/com/starrocks/qe/CoordinatorPreprocessor.java` | `computeFragmentInstances()` 负责为 fragment 选择 worker、创建 `FragmentInstance`、分配 scan ranges，最后 `finalizeDAG()`。 |
| 执行 DAG | `fe/fe-core/src/main/java/com/starrocks/qe/scheduler/dag/ExecutionDAG.java` | `finalizeDAG()` 会连接 fragment destination，并按下游 fragment 的 `pipelineDop` 设置 exchange DOP。 |
| 执行 fragment | `fe/fe-core/src/main/java/com/starrocks/qe/scheduler/dag/ExecutionFragment.java` | 保存一个 `PlanFragment` 对应的实例、scan range、destinations 等调度结果。 |
| fragment 实例实际 DOP | `fe/fe-core/src/main/java/com/starrocks/qe/scheduler/dag/FragmentInstance.java` | `setPipelineDop()` 与 `getPipelineDop()` 决定最终发给 BE 的实例 DOP。 |
| thrift 下发 | `fe/fe-core/src/main/java/com/starrocks/qe/scheduler/TFragmentInstanceFactory.java` | `toThriftForUniqueParams()` 中 `result.setPipeline_dop(instance.getPipelineDop())` 是 BE 实际收到的 DOP。 |
| 查询完成信息 | `fe/fe-core/src/main/java/com/starrocks/qe/StmtExecutor.java` | `addFinishedQueryDetail()` 已经记录 latency、状态、scan rows/bytes、CPU、内存等，可在这里补 AutoDOP trace 完成事件。 |
| profile 汇总 | `fe/fe-core/src/main/java/com/starrocks/qe/scheduler/QueryRuntimeProfile.java` | 如果后续要从 profile 解析 fragment/operator runtime 指标，可复用这里的 profile 汇总逻辑。 |

需要特别注意：当前 `CoordinatorPreprocessor.computeFragmentInstances()` 不是严格的“Pass 1 只选 worker，Pass 2 再创建实例”。它在一次 `assignFragmentToWorker()` 中同时做了 worker 选择、`FragmentInstance` 创建和 scan range 分配。对于 scan fragment，`LocalFragmentAssignmentStrategy` 还会按当时的 `pipelineDop` 分配 per-driver scan ranges，并设置实例 DOP。因此，如果在 scan range 已经按旧 DOP 分配后只改 `PlanFragment.pipelineDop`，目标 fragment 可能不会真正按新 DOP 执行。

阶段一推荐采用“probe 分配 + AutoDOP 决策 + 重新分配”的最小改造：

1. 先按原生策略执行一次 assignment，得到每个 fragment 的目标 worker、实例数量、scan range 概况。
2. 在 `finalizeDAG()` 之前调用 AutoDOP Python 后端。
3. 安装返回的 fragment DOP。
4. 清空已有 assignment 结果，按新的 DOP 重新 assignment。
5. 再执行 `validateExecutionDAG()`、`prepareCaptureVersion()` 和 `finalizeDAG()`。

这样能满足 AutoDOP 文档中“placement 已知、routing table 和 thrift instance 尚未固化”的窗口要求，也避免 scan ranges 使用旧 DOP。

## 3. 实现过程

### 3.1 新增开关与配置

先用默认关闭的方式接入，避免影响现有查询。

建议增加 FE 配置：

| 配置 | 位置 | 默认值 | 作用 |
|---|---|---|---|
| `enable_auto_dop` | `Config.java` 或 session variable | `false` | 总开关。 |
| `auto_dop_endpoint` | `Config.java` | `http://127.0.0.1:18080` | Python 后端地址。 |
| `auto_dop_timeout_ms` | `Config.java` | `20` 或 `50` | 编译路径调用后端的超时。 |
| `auto_dop_trace_dir` | `Config.java` | `log/autodop` | JSONL 采集文件目录。 |
| `auto_dop_max_dop` | `Config.java` 或 session variable | `0` | 可选，0 表示使用 session/max pipeline DOP。 |
| `auto_dop_target_fragment` | session variable | 空 | 阶段一调试用，只允许 Python 或 FE 调整该 fragment。 |

阶段一建议把开关做成 session variable 或同时支持 session variable，因为验证时可以对单个连接执行：

```sql
set enable_pipeline_engine = true;
set enable_runtime_adaptive_dop = false;
set enable_auto_dop = true;
set auto_dop_target_fragment = '1';
```

如果暂时不想改 session variable，可以先只加 `Config`，但验证灵活性会差一些。

### 3.2 启动最小 Python 后端

建议新增：

```text
tools/autodop/autodop_server.py
```

阶段一后端只需要两个接口：

| 接口 | 方法 | 用途 |
|---|---|---|
| `/health` | `GET` | FE 启动或调试时检查服务是否存活。 |
| `/v1/decide` | `POST` | 接收 FE 打包的 query/fragment 特征，返回 fragment DOP 配置。 |

请求示例：

```json
{
  "version": 1,
  "query_id": "b47c...",
  "session": {
    "pipeline_dop": 8,
    "max_pipeline_dop": 0,
    "enable_runtime_adaptive_dop": false
  },
  "fragments": [
    {
      "fragment_id": "0",
      "fragment_index": 0,
      "original_dop": 8,
      "candidate_dops": [1, 2, 4, 8],
      "root_node": "AGGREGATION",
      "leftmost_node": "EXCHANGE",
      "data_partition": "UNPARTITIONED",
      "output_partition": "UNPARTITIONED",
      "instance_count": 1,
      "worker_ids": [10002],
      "scan_range_count": 0,
      "estimated_cardinality": 1024,
      "estimated_avg_row_size": 16.0
    }
  ],
  "edges": [
    {
      "source_fragment_id": "1",
      "dest_fragment_id": "0",
      "exchange_node_id": 12,
      "partition_type": "UNPARTITIONED"
    }
  ]
}
```

响应示例：

```json
{
  "version": 1,
  "query_id": "b47c...",
  "decisions": [
    {
      "fragment_id": "1",
      "dop": 4,
      "reason": "stage1 forced target fragment"
    }
  ],
  "fallback": false,
  "message": ""
}
```

阶段一的 Python 策略可以非常简单：

- 如果传入 `--target-fragment 1 --dop 4`，则只返回 fragment 1 的 DOP 为 4。
- 如果没有指定目标 fragment，则返回空 decisions，FE 保持原生 DOP。
- 后续再把这里替换为模型推理。

启动命令示例：

```bash
python3 tools/autodop/autodop_server.py --host 127.0.0.1 --port 18080 --target-fragment 1 --dop 4
```

### 3.3 新增 FE AutoDOP 包

建议新增包：

```text
fe/fe-core/src/main/java/com/starrocks/autodop/
```

阶段一建议拆成这些类：

| 类 | 职责 |
|---|---|
| `AutoDopController` | 对外入口：判断开关、提取特征、调用 client、校验响应、安装 DOP、登记 trace。 |
| `AutoDopFeatureExtractor` | 从 `JobSpec`、`ExecutionDAG`、`ExecutionFragment` 提取 query/fragment/edge 特征。 |
| `AutoDopClient` | 使用 OkHttp 或 Java HTTP client 调用 Python `/v1/decide`。 |
| `AutoDopRequest` / `AutoDopResponse` | Gson/Jackson 可序列化 DTO。 |
| `AutoDopDecisionValidator` | 校验 Python 返回值是否安全。 |
| `AutoDopDecisionApplier` | 把合法 DOP 写入 `PlanFragment`。 |
| `AutoDopTraceManager` | 查询开始时登记 request/response/安装结果，查询完成时补 latency 和执行统计并写 JSONL。 |
| `AutoDopTraceRecord` | JSONL 采集记录 DTO。 |

`AutoDopController` 的外部接口可以先设计成：

```java
public final class AutoDopController {
    public static AutoDopResult maybeDecideAndInstall(
            ConnectContext context,
            JobSpec jobSpec,
            ExecutionDAG executionDAG) {
        // 1. check switches
        // 2. extract request
        // 3. call Python backend
        // 4. validate decisions
        // 5. install PlanFragment.pipelineDop
        // 6. register trace
    }
}
```

返回的 `AutoDopResult` 至少包含：

- `boolean applied`
- `boolean fallback`
- `String fallbackReason`
- `AutoDopRequest request`
- `AutoDopResponse response`
- `Map<PlanFragmentId, Integer> originalDop`
- `Map<PlanFragmentId, Integer> decidedDop`
- `Map<PlanFragmentId, Integer> installedDop`
- `long decisionLatencyMs`

### 3.4 在调度预处理处加入 hook

推荐修改 `CoordinatorPreprocessor.computeFragmentInstances()`。目标是把当前“一次 assignment”变成“第一次 assignment 只用于 probe，AutoDOP 修改 DOP 后再重新 assignment”。

建议先抽出一个 helper：

```java
private void assignAllFragments() throws StarRocksException {
    for (ExecutionFragment execFragment : executionDAG.getFragmentsInPostorder()) {
        fragmentAssignmentStrategyFactory
                .create(execFragment, workerProvider)
                .assignFragmentToWorker(execFragment);
    }
}
```

然后把主流程改成类似：

```java
@VisibleForTesting
void computeFragmentInstances() throws StarRocksException {
    assignAllFragments();

    AutoDopResult autoDopResult =
            AutoDopController.maybeDecideAndInstall(connectContext, jobSpec, executionDAG);

    if (autoDopResult.applied()) {
        executionDAG.resetAssignmentForAutoDop();
        assignAllFragments();
    }

    validateExecutionDAG();
    executionDAG.prepareCaptureVersion(enablePhasedSchedule);
    executionDAG.finalizeDAG();

    AutoDopTraceManager.captureActualDop(jobSpec.getQueryId(), executionDAG);
}
```

需要补的 reset 能力：

```text
ExecutionDAG.resetAssignmentForAutoDop()
ExecutionFragment.resetAssignmentForAutoDop()
```

`ExecutionFragment.resetAssignmentForAutoDop()` 至少要清理：

- `instances`
- `destinations`
- `numSendersPerExchange`
- `scanRangeAssignment`
- `colocatedAssignment`
- `cachedBucketSeqAssignment`
- `bucketSeqToInstanceForFilterIsSet`
- `cachedIsColocated`
- `cachedIsReplicated`
- `cachedIsLocalBucketShuffleJoin`
- `cachedIsColocateSet`
- `isScheduled`
- `needReportFragmentFinish`

如果某些字段没有 setter 或 clear 方法，优先补窄接口，不要在 AutoDOP 代码里直接绕过封装。

为什么 hook 放在这里：

- 第一次 assignment 后，`ExecutionFragment` 已有 worker placement、实例数、scan range 概况，特征足够接近 AutoDOP 论文中的 Pass 1 输出。
- `finalizeDAG()` 尚未执行，exchange destination、sender 数、`DataStreamSink.exchDop` 尚未固化。
- `TFragmentInstanceFactory` 尚未生成 thrift 参数，BE 尚未收到旧 DOP。

### 3.5 特征提取的最小集合

阶段一先不要追求完整模型特征，先保证能稳定复现和后续扩展。每个 query 记录：

| 字段 | 来源 |
|---|---|
| `query_id` | `JobSpec.getQueryId()` |
| `warehouse_id` | `JobSpec.getWarehouseId()` |
| `pipeline_enabled` | `JobSpec.isEnablePipeline()` |
| `session_pipeline_dop` | `SessionVariable.getPipelineDop()` |
| `session_degree_of_parallelism` | `SessionVariable.getDegreeOfParallelism()` |
| `max_pipeline_dop` | `SessionVariable` |
| `enable_runtime_adaptive_dop` | `SessionVariable.isEnableRuntimeAdaptiveDop()` |
| `fragment_count` | `ExecutionDAG.getFragmentsInPreorder().size()` |

每个 fragment 记录：

| 字段 | 来源 |
|---|---|
| `fragment_id` | `PlanFragment.getFragmentId()` |
| `fragment_index` | `ExecutionFragment.getFragmentIndex()` |
| `original_dop` | `PlanFragment.getPipelineDop()`，调用 Python 前记录 |
| `candidate_dops` | FE 生成，例如 `[1,2,4,8]` 且不超过上限 |
| `parallel_exec_num` | `PlanFragment` getter，如没有可补 getter |
| `root_node_id` / `root_node_name` | `PlanFragment.getPlanRoot()` |
| `leftmost_node_name` | `PlanFragment.getLeftMostNode()` |
| `data_partition` | `PlanFragment.getDataPartition()`，如缺 getter 则补 |
| `output_partition` | `PlanFragment.getOutputPartition()` |
| `sink_type` | `PlanFragment.getSink().getClass().getSimpleName()` |
| `fragment_cost` | `PlanFragment.getFragmentCost()` |
| `estimated_cardinality` | `PlanNode.getCardinality()` |
| `estimated_avg_row_size` | `PlanNode.getAvgRowSize()` |
| `scan_node_count` | `ExecutionFragment.getScanNodes().size()` |
| `scan_range_count` | probe assignment 后统计 `FragmentInstance` 中 scan ranges |
| `instance_count` | `ExecutionFragment.getInstances().size()` |
| `worker_ids` | `FragmentInstance.getWorkerId()` 去重 |
| `is_colocated` / `is_replicated` / `is_bucket_shuffle` | `ExecutionFragment` 已有判断方法 |
| `is_gather_root` | root fragment 且 `DataPartition.UNPARTITIONED` |

每条边记录：

| 字段 | 来源 |
|---|---|
| `source_fragment_id` | 当前 `PlanFragment` |
| `dest_fragment_id` | `PlanFragment.getDestFragment()` |
| `exchange_node_id` | `DataSink.getExchNodeId()` |
| `partition_type` | `PlanFragment.getOutputPartition().getType()` |
| `dest_original_dop` | 下游 fragment 原始 `pipelineDop` |

阶段一先不要把完整 SQL 发给 Python，避免敏感信息泄漏。需要定位 workload 时优先发送 query id、digest 或已脱敏 SQL。

### 3.6 候选 DOP 与响应校验

FE 必须把 Python 返回当作“不可信输入”。校验失败时回退原生 DOP。

阶段一候选 DOP 生成建议：

1. 基础候选：`[1, 2, 4, 8, 16, 32]`。
2. 上限：取 `auto_dop_max_dop`、session `max_pipeline_dop`、`SessionVariable.getDegreeOfParallelism()`、集群默认 DOP 中有效值的最小正数；如果都没有，则允许到原始 DOP 或 32。
3. 如果 `enable_runtime_adaptive_dop=true`，候选必须限制为 2 的幂，或者阶段一直接跳过 AutoDOP 并提示需要关闭该功能。
4. 对 root gather、短路查询、非 pipeline 查询、load/export/table sink 等复杂场景，阶段一建议直接跳过。

响应校验规则：

- `query_id` 必须匹配当前 query。
- `fragment_id` 必须存在于当前 `ExecutionDAG`。
- `dop` 必须在该 fragment 的 `candidate_dops` 中。
- 未返回的 fragment 保持原始 DOP。
- 同一 fragment 多次返回时视为非法。
- 如果只开启单 fragment 调试模式，则只能修改 `auto_dop_target_fragment` 指定的 fragment。
- 如果校验后所有 DOP 都等于原始 DOP，可以记录 trace，但 `applied=false`。

安装规则：

```java
for (PlanFragment fragment : jobSpec.getFragments()) {
    Integer dop = validatedDecision.get(fragment.getFragmentId());
    if (dop != null) {
        fragment.setPipelineDop(dop);
    }
}
```

安装后必须重新 assignment，确保 `FragmentInstance.setPipelineDop()`、per-driver scan range 和最终 thrift 参数都基于新 DOP 生成。

### 3.7 采集执行后的验证数据

建议新增 `AutoDopTraceManager`，用 query id 作为 key，在编译时保存 request/response/安装结果，在查询完成时补执行结果并写 JSONL。

编译时记录：

- query id
- request JSON 或结构化 request 对象
- Python response
- original DOP map
- decided DOP map
- installed DOP map
- decision latency
- fallback reason
- FE hook timestamp

`finalizeDAG()` 后记录实际执行 DOP：

- 每个 fragment 的 `PlanFragment.getPipelineDop()`
- 每个 fragment 下所有 instance 的 `FragmentInstance.getPipelineDop()`
- 每个 instance 的 worker id
- 每个 instance 的 scan range 数量
- 如果有 per-driver scan ranges，记录 driver sequence 到 scan range 数量的分布

查询完成时记录：

- `QueryDetail.getLatency()`
- `QueryDetail.getState()`
- `QueryDetail.getErrorMessage()`
- scan rows、scan bytes、CPU ns、memory bytes、spill bytes
- profile id 或 profile 内容路径，如果启用了 profile

建议在 `StmtExecutor.addFinishedQueryDetail()` 的末尾调用：

```java
AutoDopTraceManager.finishAndPersist(
        ctx.getExecutionId(),
        queryDetail,
        getQueryStatisticsForAuditLog());
```

如果启用了异步 profile，`processProfileAsync()` 的 callback 中会更晚拿到完整 profile。阶段一不强依赖 profile，可先写基础 trace；后续需要 operator 级运行指标时，再在 profile callback 中补充或另写一条 profile trace。

JSONL 单条记录示例：

```json
{
  "version": 1,
  "query_id": "b47c...",
  "state": "FINISHED",
  "latency_ms": 1832,
  "auto_dop": {
    "enabled": true,
    "applied": true,
    "fallback": false,
    "decision_latency_ms": 6,
    "original_dop": {"0": 8, "1": 8},
    "decided_dop": {"1": 4},
    "installed_dop": {"0": 8, "1": 4},
    "actual_instance_dop": {
      "0": [8],
      "1": [4, 4, 4]
    }
  },
  "fragments": [
    {
      "fragment_id": "1",
      "fragment_index": 1,
      "root_node": "HASH JOIN",
      "leftmost_node": "OLAP_SCAN",
      "worker_ids": [10001, 10002, 10003],
      "scan_range_count": 96,
      "candidate_dops": [1, 2, 4, 8],
      "original_dop": 8,
      "installed_dop": 4,
      "actual_instance_dops": [4, 4, 4]
    }
  ],
  "query_statistics": {
    "scan_rows": 1200000,
    "scan_bytes": 83000000,
    "cpu_cost_ns": 910000000,
    "mem_cost_bytes": 268435456,
    "spill_bytes": 0
  }
}
```

写文件建议：

- 路径：`${STARROCKS_HOME}/log/autodop/autodop_trace.jsonl` 或 `Config.auto_dop_trace_dir`。
- 使用后台单线程队列异步写，避免阻塞查询完成路径。
- 每条 JSON 单行，便于 `jq`、Spark、Python pandas 处理。
- 写失败只打 warn，不影响查询。
- 可按日期滚动：`autodop_trace_yyyyMMdd.jsonl`。

### 3.8 回退策略

阶段一必须保证 fail-open：

| 场景 | 行为 |
|---|---|
| 开关关闭 | 完全走原生路径。 |
| 非 pipeline 查询 | 跳过 AutoDOP。 |
| Python 后端不可达或超时 | 保持原始 DOP，记录 fallback reason。 |
| 返回非法 JSON | 保持原始 DOP。 |
| 返回非法 fragment id 或 DOP | 保持原始 DOP。 |
| reset/reassign 抛异常 | 恢复原始 DOP，清空 assignment 后重新按原生 DOP assignment；如果仍失败，再向上抛原异常。 |

为了能恢复原生 DOP，调用 Python 前必须保存：

```java
Map<PlanFragmentId, Integer> originalDopByFragment;
```

回退时逐个 `fragment.setPipelineDop(originalDop)`，并重新 assignment。

### 3.9 与 runtime adaptive DOP 的关系

当前 `PlanFragment.enableAdaptiveDop()` 会把 `pipelineDop` 约束为不大于当前值的 2 的幂，`FragmentInstance.setPipelineDop()` 在 `isUseRuntimeAdaptiveDop()` 为 true 时还会调用 `Utils.computeMinGEPower2()`。这会导致 Python 返回的非 2 的幂 DOP 被二次调整。

阶段一建议：

1. 验证时设置 `enable_runtime_adaptive_dop=false`。
2. 如果用户开启了 runtime adaptive DOP，AutoDOP 先跳过，trace 中记录 `fallback_reason=runtime_adaptive_dop_enabled`。
3. 后续阶段再统一设计两者的优先级，避免两个 DOP 机制同时修改同一 fragment。

### 3.10 验证步骤

1. 启动 Python 后端：

```bash
python3 tools/autodop/autodop_server.py --host 127.0.0.1 --port 18080 --target-fragment 1 --dop 4
```

2. 启动 FE/BE，设置会话变量：

```sql
set enable_pipeline_engine = true;
set pipeline_dop = 8;
set enable_runtime_adaptive_dop = false;
set enable_auto_dop = true;
set auto_dop_target_fragment = '1';
```

3. 运行一个包含多个 fragment 的查询，例如 TPC-DS/TPC-H join 查询。

4. 使用 `EXPLAIN SCHEDULER` 或 FE debug 日志确认目标 fragment DOP 改变。当前 `DefaultCoordinator.getSchedulerExplain()` 会从 `ExecutionFragment.getExplainString()` 输出 `DOP` 和 instances。

5. 检查 JSONL trace：

```bash
jq 'select(.query_id=="...") | .auto_dop' log/autodop/autodop_trace_*.jsonl
```

重点核对：

- `original_dop["1"] == 8`
- `decided_dop["1"] == 4`
- `installed_dop["1"] == 4`
- `actual_instance_dop["1"]` 中所有值都是 4，或清楚记录了被本地 scan range 限制后的实际值
- `latency_ms > 0`

6. 关闭 Python 后端再跑一次查询，确认查询成功且 trace 中 `fallback=true`。

### 3.11 单元测试建议

阶段一至少补这些测试：

| 测试 | 内容 |
|---|---|
| `AutoDopDecisionValidatorTest` | 非法 fragment、非法 DOP、重复 fragment、query id 不匹配都回退。 |
| `AutoDopDecisionApplierTest` | 合法 response 正确写入 `PlanFragment.pipelineDop`。 |
| `AutoDopFeatureExtractorTest` | 构造简单 fragment DAG，能提取 fragment、edge、worker、scan range 特征。 |
| `CoordinatorPreprocessorAutoDopTest` | mock Python client 返回单 fragment DOP，验证 reassignment 后 `FragmentInstance.getPipelineDop()` 生效。 |
| `AutoDopTraceManagerTest` | 编译期登记、完成期补齐、JSONL 写出字段完整。 |

为了方便测试，`AutoDopClient` 建议定义接口，生产环境用 HTTP 实现，测试环境用 fake client。

## 4. 阶段一完成后的后续扩展点

阶段一完成后，后续 AutoDOP 能自然接入：

1. Python 后端把固定规则替换为蒸馏策略网络推理。
2. `AutoDopFeatureExtractor` 增加竞争边、资源压力、执行窗口估计等特征。
3. `AutoDopDecisionValidator` 增加 colocate join、bucket shuffle、exchange 一致性等更完整的约束校验。
4. `AutoDopTraceManager` 的 JSONL 成为离线扰动搜索、排序模型训练和 MCTS 标签生成的数据源。
5. 加入 BE/worker 负载采集后，在 Python 或 FE 侧实现负载修正。

阶段一的核心原则是：先把“可控地改一个 fragment 的 DOP，并证明它真的生效”做扎实。只要这个闭环稳定，后续模型、搜索和负载修正都只是替换决策逻辑，而不是重新打通执行链路。
