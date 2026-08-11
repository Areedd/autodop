# AutoDOP 阶段二实现指引：科研最小闭环

本文档承接 `AutoDOP_阶段一实现指引.md`，用于指导下一阶段代码实现。

阶段二只实现以下主链路：

```text
稳定 placement
  -> fragment 竞争图
  -> 排序代价模型
  -> 离线 MCTS
  -> 策略模型
  -> Python 在线推理
```

这是科研实验项目，不按生产系统设计。代码应当短、直接、便于修改。

## 1. 范围

### 1.1 本阶段实现

1. 将当前两次完整 assignment 改为 placement 与实例化分离的两阶段调度。
2. 将现有 trace 转换为统一训练样本。
3. 构建 fragment 依赖边和 worker 竞争边。
4. 训练 pairwise ranking 代价模型。
5. 使用代价模型执行离线 MCTS。
6. 使用 MCTS 最优配置训练 per-fragment DOP 策略模型。
7. 在 `autodop_server.py` 中直接加载策略模型。

### 1.2 本阶段不实现

- 任何 fallback、fail-open、自动恢复原始 DOP 的逻辑。
- shadow、canary、线上回滚和模型热更新。
- 正确性验证、结果校验和测试数据生成。
- BE 实时负载修正。
- 在线 MCTS、分布式训练、多模型集成。
- attention 变量排序、等价类剪枝等搜索增强。

AutoDOP 已开启时，任何异常都直接终止当前操作：

- FE 请求或响应错误：查询失败。
- placement 或实例化错误：查询失败。
- 数据字段错误：离线脚本退出。
- checkpoint 加载错误：Python 服务启动失败。
- 模型推理错误：HTTP 请求失败。

这里的“失败”只表示显式报错，不切换到另一套 DOP 决策路径。

## 2. 最终工作流

### 2.1 FE 在线路径

```text
Pass 1: assignPlacement
  -> 得到 fragment -> worker、scan range 和 bucket placement
  -> 不创建 FragmentInstance

AutoDOP hook
  -> 从 plan + placement 提取 request
  -> Python PolicyModel 返回所有 fragment 的 DOP
  -> FE 写入 PlanFragment.pipelineDop

Pass 2: materializeInstances
  -> 使用同一份 placement
  -> 按最终 DOP 创建 FragmentInstance
  -> 分配 per-driver scan ranges

finalizeDAG
  -> instance id
  -> exchange destinations
  -> sender 数量
  -> exchange DOP
```

### 2.2 离线路径

```text
trace JSONL
  -> build_dataset.py
  -> samples.jsonl + pairs.jsonl
  -> train_cost.py
  -> cost_model.pt
  -> search_mcts.py
  -> mcts_labels.jsonl
  -> train_policy.py
  -> policy_model.pt
```

在线只运行一次策略模型前向计算。MCTS 只在线下运行。

## 3. P0：稳定 placement 的两阶段调度

### 3.1 当前实现的问题

阶段一当前流程是：

```text
完整 assignment
  -> 提取特征
  -> 修改 DOP
  -> reset
  -> 再次完整 assignment
```

两次 assignment 会重复执行 worker 选择和随机调度。第二次 placement 可能与发送给模型的第一次 placement 不同，竞争图输入因此与最终执行不一致。

阶段二不继续修补 reset/reassign，而是直接拆开 assignment。

### 3.2 Placement 数据结构

在 `ExecutionFragment` 中保存一份 placement。它只描述“在哪里执行和处理哪些数据”，不包含 driver DOP：

```text
FragmentPlacement
  worker_ids                    有序，保留重复实例位置
  scan_range_assignment         worker -> scan node -> ranges
  colocate/bucket assignment    已有 bucket placement 元数据
  instance_groups               每个 worker 上的实例分组
```

placement 在一次查询内只生成一次，之后只读。

不把 `FragmentInstance` 当作 placement 容器，因为实例必须在最终 DOP 确定后创建。

### 3.3 拆分 assignment

将当前 assignment 的职责拆成两个窄接口：

```java
void assignPlacement(ExecutionFragment fragment) throws StarRocksException;

void materializeInstances(ExecutionFragment fragment) throws StarRocksException;
```

`assignPlacement()`：

- local fragment 执行 backend、scan range、colocate 和 bucket placement。
- remote fragment 根据子 fragment placement 选择 worker。
- 只写 `FragmentPlacement`。
- 不创建 `FragmentInstance`。
- 不生成 driver sequence。

`materializeInstances()`：

- 不再选择 worker。
- 不再移动 scan range。
- 从 `FragmentPlacement` 创建 `FragmentInstance`。
- 根据最终 `pipelineDop` 生成 driver sequence。
- 填充 instance 级 physical DOP 和 group execution DOP。

两个 pass 都按 fragment postorder 执行。

### 3.4 DOP 与 placement 的边界

placement pass 使用进入 AutoDOP 前的原始 DOP 完成 StarRocks 现有的 worker 数量启发式。AutoDOP 只改变每个既定实例内部的 driver 并行度，不改变：

- worker 集合。
- 每个 worker 的 scan ranges。
- bucket 到 worker 的映射。
- fragment 实例数量。

这一定义主动固定了阶段二的实验变量：只研究 fragment DOP，不同时研究 worker placement。

### 3.5 CoordinatorPreprocessor 目标流程

AutoDOP 开启时：

```java
assignAllPlacements();
AutoDopResult result = AutoDopController.decideAndInstall(
        connectContext, jobSpec, executionDAG);
materializeAllFragments();
validateExecutionDAG();
executionDAG.prepareCaptureVersion(enablePhasedSchedule);
executionDAG.finalizeDAG();
AutoDopTraceManager.register(result);
```

AutoDOP 未开启时保留 StarRocks 原生路径。它是功能开关边界，不是运行时恢复机制。

### 3.6 删除阶段一恢复路径

完成两阶段调度后删除：

- `CoordinatorPreprocessor.reassignForAutoDop()`。
- `ExecutionDAG.resetAssignmentForAutoDop()`。
- `ExecutionFragment.resetAssignmentForAutoDop()`。
- `AutoDopController.restoreOriginalDop()`。
- `AutoDopDecisionApplier.restore()`。
- `AutoDopResult.failure()`、`withFailure()`、`isFailed()` 和 `failureReason`。
- trace 中的 `fallback`、`fallback_reason`。

`PlanFragment.reset()` 当前为空，不参与 AutoDOP 流程，也不需要扩展。

### 3.7 最小硬约束

阶段二不实现复杂约束求解器，只保留输入域约束：

1. 仅处理 pipeline SELECT query。
2. `enable_runtime_adaptive_dop=false`。
3. DOP 类别固定为 `[1, 2, 4, 8, 16, 32]`。
4. 每个 fragment 的候选集由 FE 生成，并与固定类别取交集。
5. Python 必须为 request 中每个 fragment 返回且只返回一个 DOP。
6. 返回 DOP 必须属于对应 fragment 的候选集。

response 的结构和候选域检查采用 `validateOrThrow()`：合法则返回 DOP map，非法则抛异常。它不产生失败结果对象，也不选择其他 DOP。

exchange destination、sender、scan driver 和 bucket 派生结构统一在 Pass 2 与 `finalizeDAG()` 中根据最终 DOP 生成，不在 Python 中重复实现。

## 4. 代码结构

### 4.1 FE

尽量复用现有类，只增加 placement 与两阶段 assignment 所需的最小接口：

```text
fe/fe-core/src/main/java/com/starrocks/qe/scheduler/
  assignment/
    FragmentAssignmentStrategy.java
    LocalFragmentAssignmentStrategy.java
    RemoteFragmentAssignmentStrategy.java
  dag/
    FragmentPlacement.java
    ExecutionFragment.java
    ExecutionDAG.java

fe/fe-core/src/main/java/com/starrocks/autodop/
  AutoDopController.java
  AutoDopDecisionValidator.java
  AutoDopFeatureExtractor.java
  AutoDopResult.java
  AutoDopTraceManager.java
```

不要为 AutoDOP 复制一套 scheduler；直接拆分现有 assignment 的 placement 和 materialization 阶段。

### 4.2 Python

```text
tools/autodop/
  requirements-autodop.txt
  autodop_server.py
  autodop/
    __init__.py
    data.py
    graph.py
    model.py
    mcts.py
    engine.py
  scripts/
    build_dataset.py
    train_cost.py
    search_mcts.py
    train_policy.py
```

文件职责：

| 文件 | 职责 |
|---|---|
| `data.py` | trace 读取、plan/config key、样本聚合、pair 构造。 |
| `graph.py` | 特征编码、依赖边、竞争边、dense tensor。 |
| `model.py` | `GraphEncoder`、`CostModel`、`PolicyModel`。 |
| `mcts.py` | UCT 搜索和 CostModel 评估。 |
| `engine.py` | checkpoint 加载、request 编码、decision 解码。 |
| `build_dataset.py` | 生成统一 JSONL 数据集。 |
| `train_cost.py` | 训练排序代价模型。 |
| `search_mcts.py` | 生成每个 plan 的最优 DOP 标签。 |
| `train_policy.py` | 训练在线策略模型。 |

不创建 `constraints.py`、schema adapter、模型导出器或独立评估框架。

依赖只保留：

```text
torch
```

图规模很小，直接使用 dense tensor，不依赖 PyG 或 DGL。

## 5. 数据格式

### 5.1 Trace 到样本

训练样本只使用：

- `request.fragments`：plan、静态特征和稳定 placement。
- `request.edges`：fragment 数据依赖。
- `auto_dop.installed_dop`：实际安装配置。
- `latency_ms`：端到端延迟。

不使用 `query_statistics`、SQL 文本、profile 或最终 worker 校验字段。

统一样本：

```json
{
  "plan_key": "sha256...",
  "fragments": [
    {
      "fragment_index": 0,
      "root_node": "HASH_JOIN",
      "leftmost_node": "OLAP_SCAN",
      "data_partition": "HASH_PARTITIONED",
      "output_partition": "HASH_PARTITIONED",
      "sink_type": "DataStreamSink",
      "fragment_cost": 41000000,
      "estimated_cardinality": 1200000,
      "estimated_avg_row_size": 32.0,
      "scan_range_count": 96,
      "instance_count": 3,
      "worker_ids": [10001, 10002, 10003],
      "candidate_dops": [1, 2, 4, 8, 16, 32]
    }
  ],
  "edges": [
    {"source": 1, "destination": 0, "partition_type": "HASH_PARTITIONED"}
  ],
  "dop_config": [4, 8],
  "latency_ms": 1832
}
```

fragment 按 `fragment_index` 排序，edge 中的 fragment id 在构建样本时转换为该顺序下的整数下标。模型内部不使用 `F00` 等字符串 id。

### 5.2 Key 与聚合

`plan_key`：

1. fragments 按 `fragment_index` 排序。
2. edges 按 `(source, destination, partition_type)` 排序。
3. 删除 query id、DOP、latency 和具体 worker id。
4. 对 canonical JSON 计算 SHA-256。

`config_key` 是按 fragment 顺序连接的 DOP 向量，例如：

```text
4,8,2,4
```

同一 `(plan_key, config_key)` 多次执行时，`latency_ms` 取 median。

同一 plan 内构造快慢配置 pair。延迟差小于 3% 的 pair 不进入 `pairs.jsonl`：

```text
abs(latency_a - latency_b) / min(latency_a, latency_b) < 0.03
```

数据脚本只输出：

```text
data/processed/<name>/
  samples.jsonl
  pairs.jsonl
  metadata.json
```

`metadata.json` 保存类别词表、数值特征统计量和 `dop_classes`。

本阶段直接消费现有 trace，不新增测试数据生成工具。后续扩充扰动数据时继续使用现有 fixed fragment DOP 服务。

## 6. Fragment 竞争图

### 6.1 节点特征

每个 fragment 是一个节点。

类别特征：

```text
root_node
leftmost_node
data_partition
output_partition
sink_type
is_colocated
is_replicated
is_bucket_shuffle
is_gather_root
```

数值特征：

```text
log1p(fragment_cost)
log1p(estimated_cardinality)
log1p(estimated_avg_row_size)
log1p(scan_range_count)
log1p(instance_count)
log1p(worker_count)
```

CostModel 额外接收当前配置的 DOP embedding。PolicyModel 使用 `UNKNOWN_DOP=0`。

### 6.2 两种边

依赖边直接来自 request：

```text
source fragment -> destination fragment
```

竞争边由稳定 placement 的 worker 集合计算。两个 fragment 有共同 worker 时添加双向边：

```text
competition(i, j) = |workers_i ∩ workers_j| / |workers_i ∪ workers_j|
```

不估计执行时间重叠，不读取 CPU/memory profile，不增加人工资源权重。

### 6.3 Tensor

```text
node_features:      [N, feature_dim]
dependency_matrix:  [N, N]
competition_matrix: [N, N]
candidate_mask:     [N, 6]
dop_ids:            [N]
```

每个 plan 单独前向计算，不实现复杂图 batching。

## 7. 排序代价模型

### 7.1 模型

共享编码器：

```text
category embeddings + numerical features + DOP embedding
  -> Linear
  -> 2 层 dense graph attention
  -> fragment embeddings
```

attention logits 只加入两项图 bias：

```text
dependency_bias
competition_weight * learnable_scale
```

CostModel readout：

```text
mean pooling + max pooling + root embedding
  -> MLP
  -> scalar score
```

score 越大表示配置越快。

### 7.2 Loss

对同一 plan 的 fast/slow 配置使用：

```text
loss = softplus(-(score_fast - score_slow))
```

不预测绝对 latency。

### 7.3 Checkpoint

单个 PyTorch checkpoint 保存：

```text
model_state
model_config
category_vocab
feature_mean
feature_std
dop_classes
```

训练脚本只记录 epoch 和 train loss。

## 8. 离线 MCTS

### 8.1 状态与动作

状态是部分 DOP 向量：

```text
[4, UNKNOWN, UNKNOWN, 8]
```

动作是为下一个 fragment 选择一个候选 DOP。

fragment 顺序使用固定启发式：

```text
importance = log1p(fragment_cost)
           + 0.5 * log1p(scan_range_count)
           + 0.2 * dependency_degree
```

按 importance 降序搜索。

### 8.2 搜索

选择使用 UCT：

```text
UCT = mean_reward
    + 1.414 * sqrt(log(parent_visits) / child_visits)
```

一次 simulation：

1. UCT 选择已展开节点。
2. 展开一个未访问候选 DOP。
3. 对剩余 fragment 随机补全配置。
4. CostModel 对完整配置输出 score。
5. score 作为 reward 回传。

默认每个 plan 运行 500 次 simulation。第一版逐次评估，不实现并行树或批量 leaf 调度。

输出：

```json
{
  "plan_key": "sha256...",
  "best_dop_config": [2, 8, 4],
  "best_score": 3.82
}
```

## 9. 策略模型

PolicyModel 复用 CostModel 的 GraphEncoder：

```text
plan graph + UNKNOWN_DOP
  -> GraphEncoder
  -> per-fragment Linear
  -> logits over [1, 2, 4, 8, 16, 32]
```

在 argmax 前应用 FE candidate mask：

```text
masked_logits = logits.masked_fill(candidate_mask == 0, -inf)
```

标签使用 MCTS 的 `best_dop_config`，Loss 使用所有 fragment 的交叉熵之和：

```text
loss = sum(CrossEntropy(masked_logits_i, label_i))
```

第一版只使用 hard label，不保存 visit distribution，不使用 KL loss。

Policy checkpoint 与 CostModel 使用相同格式。

## 10. Python 服务

### 10.1 显式启动模式

保留两个互斥的实验入口：

```bash
# 固定配置，用于主动扰动
python3 tools/autodop/autodop_server.py \
  --fragment-dops F00=1,F01=4

# 策略模型，用于在线决策
python3 tools/autodop/autodop_server.py \
  --policy-model tools/autodop/runs/policy_tpcds_sf10.pt
```

启动时必须且只能选择一个入口。删除现有 `--target-fragment` 和 `--dop` 参数。

固定配置和策略模型是两个主动选择的实验模式，不在请求期间互相切换。
`FixedEngine` 要求显式覆盖全部 fragment，也可以用 `*=4` 指定统一 DOP。缺少任何 fragment 都直接报错。

### 10.2 Engine

```python
class FixedEngine:
    def decide(self, request):
        ...


class PolicyEngine:
    def __init__(self, checkpoint_path):
        checkpoint = torch.load(checkpoint_path, map_location="cpu")
        self.model = build_policy_model(checkpoint)
        self.model.eval()

    def decide(self, request):
        graph = build_online_graph(request, checkpoint)
        with torch.no_grad():
            logits = self.model(graph)
        return decode_all_fragments(request, logits)
```

HTTP handler 只负责 JSON 读写，然后调用启动时选定的 engine。

PolicyEngine 返回 request 中所有 fragment：

```json
{
  "version": 1,
  "query_id": "b47c...",
  "decisions": [
    {"fragment_id": "F00", "dop": 2, "reason": "policy"},
    {"fragment_id": "F01", "dop": 8, "reason": "policy"}
  ]
}
```

response 不包含 confidence、native decision、shadow decision 或恢复状态。

## 11. 实现顺序

严格按下面顺序推进：

1. 增加 `FragmentPlacement`。
2. 将 local assignment 拆为 placement 与 materialization。
3. 将 remote assignment 拆为 placement 与 materialization。
4. 改造 `CoordinatorPreprocessor` 为 AutoDOP 两阶段路径。
5. 修改 `AutoDopFeatureExtractor` 从 placement 读取特征。
6. 删除 reset/reassign、restore 和失败结果状态。
7. 实现 `data.py` 与 `build_dataset.py`。
8. 实现 `graph.py`。
9. 实现 `GraphEncoder` 与 `CostModel`。
10. 实现 `train_cost.py`。
11. 实现 `mcts.py` 与 `search_mcts.py`。
12. 实现 `PolicyModel` 与 `train_policy.py`。
13. 实现 `FixedEngine`、`PolicyEngine` 并精简 HTTP handler。

不要同时铺开 FE 调度改造和模型代码。先固定 placement 语义，再固定数据格式，最后实现模型。

## 12. 命令链

```bash
python3 tools/autodop/scripts/build_dataset.py \
  --input log/autodop \
  --output tools/autodop/data/processed/tpcds_sf10

python3 tools/autodop/scripts/train_cost.py \
  --dataset tools/autodop/data/processed/tpcds_sf10 \
  --output tools/autodop/runs/cost_tpcds_sf10.pt

python3 tools/autodop/scripts/search_mcts.py \
  --dataset tools/autodop/data/processed/tpcds_sf10 \
  --cost-model tools/autodop/runs/cost_tpcds_sf10.pt \
  --output tools/autodop/runs/mcts_labels_tpcds_sf10.jsonl \
  --iterations 500

python3 tools/autodop/scripts/train_policy.py \
  --dataset tools/autodop/data/processed/tpcds_sf10 \
  --labels tools/autodop/runs/mcts_labels_tpcds_sf10.jsonl \
  --cost-model tools/autodop/runs/cost_tpcds_sf10.pt \
  --output tools/autodop/runs/policy_tpcds_sf10.pt

python3 tools/autodop/autodop_server.py \
  --policy-model tools/autodop/runs/policy_tpcds_sf10.pt
```

## 13. 阶段完成产物

阶段二只要求形成以下可运行产物：

1. FE 在同一份 placement 上完成 DOP 决策和实例化，不再执行第二次 worker assignment。
2. `samples.jsonl`、`pairs.jsonl` 和 `metadata.json`。
3. CostModel checkpoint。
4. 每个 plan 的 MCTS DOP 标签。
5. PolicyModel checkpoint。
6. 能加载 PolicyModel 并响应 `/v1/decide` 的 Python 服务。

正确性实验、测试数据构造和性能结果分析不属于本指引范围。
