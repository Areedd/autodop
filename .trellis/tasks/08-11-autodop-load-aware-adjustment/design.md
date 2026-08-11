# AutoDOP 负载修正与实例级并行度调整 — 技术设计

## 1. 背景与关键研究结论

四个设计文档（DD01–DD04）要求的三项 P0 功能缺口（负载修正、实例级调整、轨迹扩展）共享同一个数据基础：节点资源视图。本次设计前完成的关键代码研究：

**R-1 资源数据通道已存在，无需 BE 侧改动。**
BE 的 `TaskWorkerPool` 每 `report_resource_usage_interval_ms`（默认 1000ms）向 FE 上报
`TResourceUsage{cpu_used_permille, mem_used_bytes, mem_limit_bytes, num_running_queries}`
（`be/src/agent/task_worker_pool.cpp:807-812`）；FE 侧 `ReportHandler.resourceUsageReport`
写入 `ComputeNode.updateResourceUsage`。`ComputeNode` 已提供：
- `getCpuUsedPermille()`（千分比，0–1000）
- `getMemUsedPct()`（`memUsedBytes / memLimitBytes`，0–1）
- `isResourceUsageFresh()`（基于 `query_queue_resource_usage_interval_ms` 判断数据新鲜度）
- `getCpuCores()` / `getMemLimitBytes()`（节点规格，供异构归一化）

DD04 §3.1"复用既有指标通道，由 FE 侧拉取"可落地为：FE 侧按周期（默认 2s）从 `ComputeNode`
采样并做 EWMA 平滑，无需新指标通道。

**R-2 实例级 DOP 通道已存在。**
`FragmentInstance.setPipelineDop()`（`FragmentInstance.java:221`）已实现并被上游 scan-range
驱动逻辑使用（`LocalFragmentAssignmentStrategy` 中 `expectedPhysicalDop = min(pipelineDop, scanRanges.size())`）。
实例级 DOP 通过 `TExecPlanFragmentParams` 下发到 BE，按实例创建 driver。DD04 的实例调整只需
在决策窗口内对可调整实例调用 `setPipelineDop` 覆盖，无执行引擎改动。

**R-3 决策窗口时序（`CoordinatorPreprocessor.computeFragmentInstances`）：**
```
assignAllPlacements() → decideAndInstall() → materializeAllFragments()
→ validateExecutionDAG() → prepareCaptureVersion() → finalizeDAG()
→ AutoDopTraceManager.captureActualDop()
```
- 实例调整插入点：`materializeAllFragments()` 之后、`finalizeDAG()` 之前。此时实例已创建、
  扫描范围已分配，但路由表尚未固化。
- 安全性论证：`finalizeDAG` 中 `connectFragmentToDestFragments` 的 exchDop 取
  `destFragment.getPipelineDop()`（目标片段级 DOP，本设计不调整）；`workerIdToNumInstances`
  只依赖实例数（不变）；扫描片段无上游输入，不存在 receiver 端路由受影响的问题。
  因此对"无上游输入 ∧ 非 colocate ∧ 非 bucket shuffle"的扫描实例调整 driver 数不破坏路由。

**R-4 可调整性判定所需数据已在决策请求中：** `edges`（source/dest）、`is_colocated`、
`is_bucket_shuffle`、`is_replicated`、`worker_ids`、`candidate_dops`、`instance_count`。
无上游输入 = 该 fragment 无 incoming edge。

**R-5 决策服务无状态约束（DD03 §1.5：请求间无共享状态）** ⇒ EWMA 平滑必须放在 FE 侧资源视图
（持有时间序列状态），死区判定与缩放作为纯函数放在 Python 侧（可单测、无状态）。

## 2. 总体架构

```
                    ┌──────────────────── FE ────────────────────┐
   BE (既有)         │                                            │
  TResourceUsage ──►│  ComputeNode (既有数据)                    │
  每 1s 上报        │      │                                     │
                    │      ▼                                     │
                    │  AutoDopResourceView (新增)                │
                    │  周期采样(2s) + EWMA(0.25) + 新鲜度        │
                    │      │                                     │
                    │      ├──► 请求扩展：worker_loads 字段       │
                    │      │        │                            │
                    │      │        ▼                            │
                    │      │   AutoDopController.decideAndInstall│──► Python 决策服务
                    │      │        │                            │    策略推理 → 负载修正(纯函数)
                    │      │        ▼                            │
                    │      │   AutoDopDecisionApplier (片段级)    │
                    │      ▼        │                            │
                    │  AutoDopInstanceAdjuster (新增)            │
                    │  materialize 后、finalize 前：              │
                    │  可调整扫描实例按 BE 资源差异化 setPipelineDop│
                    │      │                                     │
                    │      ▼                                     │
                    │  AutoDopTraceManager 三阶段快照 (扩展)      │
                    └────────────────────────────────────────────┘
```

## 3. 模块设计

### 3.1 FE：AutoDopResourceView（新增，资源视图模块）

- 单例（`AutoDopResourceView.getInstance()`），内部持有一个 Daemon 线程（复用
  `ThreadPoolManager.newDaemonThread` 模式，参考 `AutoDopTraceManager.WRITER`），按
  `auto_dop_resource_view_interval_ms`（默认 2000ms）周期采样。
- 每次采样：遍历 `GlobalStateMgr.getCurrentState().getNodeMgr().getClusterInfo()`
  的存活 `ComputeNode`，读取 `getCpuUsedPermille()`/`getMemUsedPct()`/`isResourceUsageFresh()`。
- EWMA：`smoothed = alpha * raw + (1 - alpha) * smoothed`（alpha 默认 0.25，可配置）。
  首次采样直接赋值。节点无采样数据时视为中性（负载 0，fresh=false）。
- 归一化：CPU 用 `cpuUsedPermille / 1000.0`，内存用 `getMemUsedPct()`，均为 0–1，天然按
  节点容量归一化（异构节点的小规格体现在 `memLimitBytes`/`cpuCores` 上，无需额外处理）。
- 对外接口（供负载修正与实例调整共用，保证口径一致）：
  `Optional<WorkerLoad> getWorkerLoad(long workerId)`，其中
  `WorkerLoad{cpuUsed, memUsedPct, fresh}`。
- 测试钩子：`setResourceViewForTest(...)` / 可注入的负载提供器，便于单测构造高负载 BE。

### 3.2 FE：决策请求扩展（worker_loads）

`AutoDopRequest` 增加顶层字段：

```java
@SerializedName("worker_loads")
public Map<String, WorkerLoad> workerLoads = new LinkedHashMap<>();  // key = worker id
// WorkerLoad { cpu_used: double, mem_used_pct: double, fresh: boolean }
```

`AutoDopFeatureExtractor` 在打包请求时，对请求中出现的全部 `worker_ids` 从资源视图查询并填充。
数据缺失的 worker 不放入 map（Python 侧按"无负载数据 → 不修正"处理）。请求仍不携带 SQL 文本。

### 3.3 Python：负载修正（load_correction.py 新增）

纯函数模块，无状态，便于单测：

```python
def worker_factor(load, cpu_threshold=0.4, mem_threshold=0.65, floor=0.1) -> float:
    # factor = 1 若 cpu < cpu_threshold 且 mem < mem_threshold
    # 否则按超阈值比例线性下降到 floor（满载处触底 floor）
    # 取 CPU 与内存两维的较小因子

def fragment_factor(fragment, worker_loads, ...) -> float:
    # 取全部目标 worker 因子中的最小值；目标 worker 缺失负载数据时按 1（不修正）

def corrected_dop(base_dop, factor, candidate_dops) -> int:
    # round(base_dop * factor) 到候选集中最接近的值，不低于 1
```

接入位置：`PolicyEngine.decide` 与 `TreePolicyEngine.decide`（策略输出后、返回前）对每个
fragment 施加 `corrected_dop`，并在 decision 中标记 `reason: "policy:load_corrected"` /
保留原 reason。修正参数由 `autodop_server.py` 新增 CLI 参数注入（`--load-cpu-threshold`
`--load-mem-threshold` `--load-factor-floor`），与 FE 侧配置项保持一致（由部署方对齐）。
显式规则引擎（Fixed/CostAware/TopologyAware/Heterogeneous/OperatorSubgraph）不施加修正，
保持确定性（设计意图：修正作用于策略输出）。

### 3.4 FE：AutoDopInstanceAdjuster（新增，实例调整模块）

在 `computeFragmentInstances` 中 `materializeAllFragments()` 之后、`validateExecutionDAG()`
之前调用 `AutoDopInstanceAdjuster.adjust(executionDAG)`：

1. **可调整性判定**（对每个 `ExecutionFragment`）：
   - 无上游输入：`executionDAG` 中该 fragment 无 incoming exchange 边（按 fragment 间的
     data exchange 关系判断；决策请求的 edges 已含此信息，也可直接从 DAG 重算）。
   - 非 colocate、非 bucket shuffle：`fragment.isColocated()` / `isBucketShuffle()`（与
     `AutoDopFeatureExtractor` 使用的标记一致）。
   - 三者同时满足才可调整。
2. **逐实例调整**：对可调整片段，对每个实例按其 `getWorkerId()` 查资源视图：
   - `fresh == false` 或负载缺失 → 不调整（保持基准）。
   - 资源充裕（低于死区阈值）→ 不调整。
   - 资源紧张 → `adjusted = corrected_dop(instance.getPipelineDop(), factor, candidateDops)`，
     其中 `factor` 用与 3.3 相同的死区/线性规则但触底为
     `auto_dop_instance_adjust_floor`（默认 0.5，实例级调整更保守）；只降不升（若计算值
     ≥ 基准则不调整）；`candidateDops` 为片段候选集（与请求一致，来自
     `auto_dop_max_dop`）。
   - 同 BE 的多个实例按同一因子调整（天然一致）。
3. **不动**：非扫描片段、colocate/bucket shuffle 片段、有上游输入的片段。

调整后不改变实例数、不改变片段级 `PlanFragment.getPipelineDop()`——exchange 路由（sender
数 = 实例数）与下游 exchDop（目标片段级 DOP）均不受影响。

### 3.5 FE：轨迹三阶段快照（AutoDopTraceManager 扩展）

`ActualFragmentDop` 增加字段：

```java
@SerializedName("baseline_instance_dops")  // materialize 后、实例调整前的实例 DOP
@SerializedName("adjusted_instance_dops")  // 实例调整后的实例 DOP
@SerializedName("instance_dops")           // 既有字段 = finalizeDAG 后的最终 DOP
```

`AutoDopTraceManager` 增加 `captureInstanceDop(executionDAG, stage)`，在三个时点调用：
- `materializeAllFragments()` 后 → baseline（同时这也是"基准并行度"）；
- 实例调整后 → adjusted；
- 既有 `captureActualDop`（finalizeDAG 后）→ final（沿用现有字段，向后兼容）。

Python `data.py::sample_from_trace` 只消费 `request` / `auto_dop.installed_dop` /
`latency_ms`，新字段不破坏离线流水线（兼容性单测确认）。

### 3.6 配置项（FE Config.java + SessionVariable 不动）

| 配置 | 默认 | 说明 |
|---|---|---|
| `auto_dop_resource_view_interval_ms` | 2000 | 资源视图采样周期（DD04 §3.1 默认 2s） |
| `auto_dop_load_ewma_alpha` | 0.25 | EWMA 平滑系数（DD03 §4.2） |
| `auto_dop_load_cpu_threshold` | 0.4 | CPU 死区阈值 |
| `auto_dop_load_mem_threshold` | 0.65 | 内存死区阈值 |
| `auto_dop_load_factor_floor` | 0.1 | fragment 级修正因子下限（DD03 §4.2 满载触底） |
| `auto_dop_instance_adjust_floor` | 0.5 | 实例级调整因子下限（DD04 §1.4 调整幅度上限） |
| `auto_dop_instance_adjust_enabled` | true | 实例调整总开关（稳定性：可整体关闭） |

Python 侧参数与上表前三项 + factor floor 对齐（部署方配置一致即可，不强行联动）。

## 4. 数据流与时序

查询编译路径（仅 AutoDOP 开启时多出的步骤，全部在决策窗口内）：

1. `assignAllPlacements()`：放置确定（沿用）。
2. `decideAndInstall()`：资源视图提供 worker_loads → 请求携带 → Python 服务策略推理 +
   负载修正（fragment 级，作用于策略输出）→ 校验 → 安装片段级 DOP（沿用）。
3. `materializeAllFragments()`：按最终片段级 DOP 创建实例、分配扫描范围（沿用）。
4. 快照 baseline 实例 DOP。
5. `AutoDopInstanceAdjuster.adjust()`：可调整扫描实例按 BE 资源差异化下调。
6. 快照 adjusted 实例 DOP。
7. `finalizeDAG()`：路由表按调整后的实例结构生成（实例数不变，天然一致）。
8. `captureActualDop()`：记录最终实例 DOP。

离线数据流不变：trace（含三阶段实例 DOP）→ `build_dataset.py` → 训练。新字段不参与
特征编码（训练特征仍用片段级数据），仅作离线核对依据（DD04 §1.3）。

## 5. 风险与兼容性

- **路由安全**：调整限于无上游输入 ∧ 非 colocate ∧ 非 bucket shuffle 的扫描实例；
  实例数不变；片段级 DOP 不变。与 DD04 §1.2"路由安全"约束一致。
- **旧 trace 兼容**：新字段可选，`sample_from_trace` 不读取；老 trace 可直接复用。
- **决策服务兼容**：请求新增 `worker_loads` 可选字段；旧版服务忽略未知字段（JSON 解析
  宽容），新版服务对缺失字段按不修正处理 → FE 与 Python 可独立升级。
- **震荡控制**：EWMA 平滑 + 死区 + 实例化前一次性施加（DD03 §4.2 三重机制）。
- **稳定性**：资源视图数据缺失/过期 → 中性值 → 不调整；`auto_dop_instance_adjust_enabled`
  可整体关闭实例调整，不影响 fragment 级链路。

## 6. 测试策略

### 6.1 Python 单测（tests/test_load_correction.py）

- 死区：阈值以下因子恒为 1；超阈值线性下降；满载处 == floor。
- 双维取较小值；多 worker 取最小因子；缺负载数据按 1。
- 候选取整：修正后 DOP ∈ candidate_dops 且 ≥ 1；修正不升（只降或不变）。
- 引擎接入：PolicyEngine 带/不带 worker_loads 的输出差异；固定引擎不受修正影响。

### 6.2 FE 单测（fe-core test）

- 资源视图：EWMA 平滑计算、新鲜度、缺失按中性值（注入 mock 负载提供器）。
- 实例调整（沿用 `AdaptiveChooseNodesTest` 的 `CoordinatorPreprocessor` 测试框架）：
  - 构造高负载 BE → 可调整扫描实例 DOP 下调、低负载 BE 实例保持；
  - 有上游输入的片段、colocate/bucket shuffle 片段实例 DOP 不变；
  - 调整结果 ∈ 候选集且 ≥ 1；同 BE 实例一致；
  - 实例数不变；片段级 DOP 不变（路由安全断言）。
- 轨迹：三阶段快照字段齐全、数值关系正确（baseline ≥ adjusted ≥ final 且相等性成立
  当无下游钳制）。

### 6.3 集成实验（真实集群：本地 FE + starrocks-be1/2/3）

- 启动 `autodop_server.py --policy-model ...`（或 fixed 模式对照），执行 TPC-DS 查询：
  验证 trace 三阶段数据、决策耗时 ≤ 50ms。
- 高负载场景：对单 BE 加压（并发查询）→ 验证该 BE 上扫描实例 DOP 被压低、其余 BE 保持。
- 负载修正开关对比：高负载下开/关修正的 DOP 与延迟差异。
