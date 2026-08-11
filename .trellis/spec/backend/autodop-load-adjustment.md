# AutoDOP 负载修正与实例级并行度调整（DD03 §4.2 / DD04）

> 代码规范：AutoDOP 资源视图、负载修正、实例级调整、轨迹三阶段快照的实现契约。
> 参考设计文档：`DD03_细粒度查询调度以及资源管理模式详细设计文档.md` §4.2、`DD04_分布式环境下的资源预测以及管理的调度系统详细设计文档.md` 全文。

## 1. Scope / Trigger

- FE 侧 `com.starrocks.autodop` 包的资源视图/实例调整模块与 `CoordinatorPreprocessor` 决策窗口接入。
- Python 侧 `tools/autodop/autodop/load_correction.py` 纯函数与策略引擎接入。
- 跨层契约：决策请求 `worker_loads` 字段、trace `actual_fragments` 三阶段字段。

## 2. Signatures

```java
// FE: 资源视图（单例，包内构造，测试用 newInstanceForTest()）
AutoDopResourceView.getInstance()                       // 惰性启动 2s 采样线程（EWMA alpha=Config.auto_dop_load_ewma_alpha）
Optional<WorkerLoad> getWorkerLoad(long workerId)       // WorkerLoad{cpuUsed 0-1, memUsedPct 0-1, fresh}
AutoDopResourceView.effectiveInstance()                 // 返回 instanceForTest ?? getInstance()（测试注入钩子）
AutoDopResourceView.fillWorkerLoads(request, workerIds) // 把有数据的 worker 写入 request.workerLoads（缺失省略）

// FE: 实例级调整（materialize 后、finalizeDAG 前调用）
AutoDopInstanceAdjuster.adjust(ExecutionDAG, AutoDopResult)  // 受 Config.auto_dop_instance_adjust_enabled 控制

// Python: 负载修正（纯函数、无状态）
worker_factor(load, cpu_threshold=0.4, mem_threshold=0.65, floor=0.1) -> float
fragment_factor(fragment, worker_loads, ...) -> float
corrected_dop(base_dop, factor, candidate_dops) -> int
apply_to_decision(decision, fragment, worker_loads, ...) -> dict
```

## 3. Contracts

### 决策请求（AutoDopRequest）
- 顶层新增 `worker_loads: Map<String, WorkerLoad>`，key = worker id 字符串。
- `WorkerLoad{cpu_used: double, mem_used_pct: double, fresh: boolean}`。
- 采样来自 `ComputeNode`（BE 每 1s 上报 TResourceUsage → `backendAndComputeNodeStream()`，**不是 `getComputeNodes()`**——后者只含纯 CN 不含 Backend）。
- 数据缺失/过期的 worker 不放入 map（Python 侧按不修正处理）。

### 负载修正（Python）
- EWMA 平滑在 FE 资源视图做（决策服务无状态约束）；请求携带平滑后负载。
- 死区：cpu ≤ 0.4 且 mem ≤ 0.65 → 因子 1；超阈值线性下降到 floor（满载触底）。
- 每 worker 因子 = min(cpu 维, mem 维)；每 fragment 因子 = min(全部目标 worker)。
- 修正后 DOP：`round(base * factor)` 向下取最近候选（**只降不升**），不低于 1。
- 只接入 `PolicyEngine` / `TreePolicyEngine`（策略输出）；显式规则引擎（Fixed/CostAware/TopologyAware/Heterogeneous/OperatorSubgraph）不接入（保持确定性与基线纯净）。
- 实际修正时 reason 后缀 `:load_corrected`。

### 实例级调整（FE）
- 可调整判定（三条须同时满足）：无 upstream exchange 输入（用 `AutoDopFeatureExtractor.extractEdges` 构建 dest 集合反向判断）∧ 非 colocate（`isColocated()`）∧ 非 bucket shuffle（`isLocalBucketShuffleJoin()`）。
- 调整规则：fresh=false 或缺失 → 不调整；因子同死区公式但触底 `auto_dop_instance_adjust_floor`（默认 0.5）；只降不升；候选集与请求一致（`autoDopResult.getRequest()`）；同 BE 实例同一因子。
- **不改变**：实例数、片段级 `PlanFragment.getPipelineDop()`（exchange 路由安全的前提）。

### 轨迹（TraceRecord.actual_fragments[]）
- 每个 fragment：`baseline_instance_dops`（materialize 后）→ `adjusted_instance_dops`（实例调整后）→ `instance_dops`（finalizeDAG 后，既有字段）。
- `captureInstanceDop(queryId, dag, stage)` 负责前两阶段；`captureActualDop` 只追加 final（复用已有 actualFragments 条目，按 fragmentId 匹配）。
- **`AutoDopTraceManager.register` 必须在 materialize 之前调用**（baseline 快照需要 record 已存在）。

## 4. Validation & Error Matrix

| 条件 | 行为 |
|---|---|
| worker 无负载数据 / fresh=false | 不修正、不调整（保持基准） |
| 负载低于死区阈值 | 因子 = 1，输出不变（低负载不扰动） |
| 决策服务不可达/超时/非法响应 | 查询显式失败（沿用 `AutoDopController` 语义，不静默回退） |
| `auto_dop_instance_adjust_enabled=false` | 实例调整整体跳过，fragment 级链路不受影响 |
| 修正后 DOP 向下取整后无候选 | 取最小候选（≥1） |

## 5. Good / Base / Bad Cases

- **Good**：mem 79.6% 的 BE 上扫描实例 DOP 4→2，mem 70.4% 的 BE 保持 4；`baseline=[4,4] ≥ adjusted=[4,2] ≥ final=[4,2]`。
- **Base**：低负载（mem 2.6%）时 reasons 保持 `policy`，无 `load_corrected`，实例 DOP 全等于基准。
- **Bad**：对 `FixedEngine` 输出施加修正（破坏显式意图）；用 `getComputeNodes()` 采样（漏掉 Backend 导致 worker_loads 恒为空）。

## 6. Tests Required

- Python `tests/test_load_correction.py`：死区恒 1、线性下降、满载==floor、双维取 min、多 worker 取 min、缺数据=1、候选取整只降不升、引擎接入差异、FixedEngine 不受影响（16 例）。
- FE `AutoDopResourceViewTest`：首次采样直赋、EWMA、过期中性化、未知 worker empty、fillWorkerLoads 省略缺失（6 例）。
- FE `AutoDopInstanceAdjusterTest`（autodop 包，纯单元）+ `AutoDopInstanceAdjustIntegrationTest`（qe 包，集成）：高负载下调/低负载保持/路由安全（上游输入与 colocate/bucket shuffle 片段实例 DOP 一致）/禁用开关保持基准/轨迹三阶段关系（baseline ≥ adjusted ≥ final）。
- 集成实验（真实集群）：高负载场景 trace 检查（三份数据、decision_latency_ms ≤ 50ms、结果正确性）。

## 7. Wrong vs Correct

#### Wrong
```java
// 只采样 CN：worker_loads 永远为空（真实集群 BE 是 Backend 不是 ComputeNode）
getClusterInfo().getComputeNodes()
```
#### Correct
```java
// Backend + CN 一起采样
getClusterInfo().backendAndComputeNodeStream()
```

#### Wrong
```java
// register 在 materialize 之后：baseline 快照丢失（TRACES 里还没有 record）
decideAndInstall(...); materializeAllFragments(); captureInstanceDop(BASELINE); ...
```
#### Correct
```java
decideAndInstall(...); AutoDopTraceManager.register(result); materializeAllFragments();
captureInstanceDop(..., BASELINE); AutoDopInstanceAdjuster.adjust(...); captureInstanceDop(..., ADJUSTED);
```

## 设计决策

### 负载修正只作用于策略输出
**Context**：DD03 §4.2 修正目标是"策略网络的输出"。
**Options**：全部引擎统一修正 vs 仅策略类引擎。
**Decision**：仅 `PolicyEngine`/`TreePolicyEngine`。显式规则引擎是实验基线，需保持确定性（输入相同输出相同）与用户显式意图。
**Extensibility**：如需对规则引擎也修正，在 `autodop_server.py` 加 `--apply-load-correction` 开关即可，纯函数已就绪。

### EWMA 在 FE、死区缩放纯函数在 Python
**Context**：决策服务"请求间无共享状态"（DD03 §1.5），EWMA 需要时间序列状态。
**Decision**：FE 资源视图持有平滑状态，请求携带平滑后负载；Python 侧纯函数做死区/缩放（可单测、无状态）。
