# DD01–DD04 设计文档实现完整度核对与效果评估报告

- 核对日期：2026-08-11
- 核对对象：仓库根目录四份设计文档（DD01 总体方案 / DD02 细粒度预测 / DD03 调度与资源管理 / DD04 分布式调度系统）
- 核对方式：4 个并行 research 子代理逐文档核对草稿 + 主会话逐条复核与关键代码抽查（GAT 编码器形态、负载修正常量、资源视图归一化、决策窗口插入点、轨迹三阶段字段、蒸馏策略效果 JSON 等均已人工验证）
- 状态档：✅ 已实现 / 🟡 部分实现 / ❌ 未实现 / ⚪ N/A（纯调研、元内容）
- 证据等级：L1=代码+单测（强）；L2=集成实验数据（强）；L3=实验报告/记录文档（中）；L4=仅代码存在性（弱）

---

## 1. 摘要

### 1.1 总览表

| 文档 | ✅ 已实现 | 🟡 部分实现 | ❌ 未实现 | ⚪ N/A | 总体判断 |
|---|---:|---:|---:|---:|---|
| DD01 总体方案（§1.3、§3） | 22 | 2 | 0（含 1 个 ❌ 子项） | 3 | 方案落地完整；量化验证缺口 2 处 |
| DD02 细粒度预测（§1–§6） | 23 | 9 | 6 | 0 | 核心数据管线与模型完整；**评估闭环缺口集中** |
| DD03 调度与资源管理（§1–§6） | 62 | 1 | 0 | 0 | 全链路实现最完整，常量与文档逐一一致 |
| DD04 分布式调度系统（§1–§6） | 24 | 4 | 1 | 0 | 功能链路完整；容量归一化与异构验证缺口 |

**整体结论**：四份文档的核心设计——fragment 级 DOP 决策链路（DD01）、离线排序预估管线（DD02）、配置搜索/策略蒸馏/在线决策/负载修正（DD03）、分布式资源视图/实例级调整/轨迹三阶段（DD04）——均已真实落地并经过验证（单测 60+、集成测试、真实集群 M5 实验、多份实验报告）。**未实现部分集中在"评估与验证闭环"**：DD02 的排序准确率独立评估流程、训练触发自动化、回归基线对比（P1，与既有任务 Notes 锚点一致）；DD04 的异构节点容量维度验证（P2）；OLTP 基准（P2）。**效果方面**：规则引擎/树策略路径在特定工作负载下相对最优固定全局 DOP 有 30%+ 提升（TPC-DS/SSB 正式认证），但**蒸馏策略网络在线实测为负效果**（-38.2%/-80.6%），这是效果评估中最重要的一条负面证据（详见 §6）。

### 1.2 关键数字

- 核对条目总数：157 条（含 N/A），✅ 131 / 🟡 16 / ❌ 7 / ⚪ 3
- 全链路真实执行过的离线产物：3 组 cost checkpoint、4 份 MCTS labels、多份 policy/tree-policy 产物
- 真实集群实验：M5 集成实验（AC1–AC5 全过）、51 并发异构批次实验（1122 条 trace 零错误）、三数据集 100 查询认证（TPC-DS/SSB 正式、TPC-H 预筛）、代表性查询多轮复测

---

## 2. DD01 核对矩阵（State-of-Art 调研以及总体方案）

检索范围：`fe/fe-core/src/main/java/com/starrocks/autodop/` 全部 12 个文件、`CoordinatorPreprocessor.java`、`StmtExecutor.java`、`SessionVariable.java`、`Config.java`、FE 测试 6 单测 + 3 集成、`tools/autodop/` 全部 Python 模块与脚本与测试、`be/src/exec/pipeline/pipeline_driver_queue.h`、`driver_limiter.h/.cpp`、提交 8945a68528 / 4380a14bc0 / 06a1dcaf81、13 份报告 + runs/ + journal-1.md。

| 条目ID | 文档条目 | 状态 | 证据 | 缺失点/说明 |
|---|---|---|---|---|
| DD01-1.3-G1 | 目标1：竞争感知排序代价模型（输出相对性能分数，不做绝对延迟预测） | 🟡 | 模型/特征/图编码/离线代理评估全落地：`model.py` CostModel + `train_cost.py:56` softplus 排序损失；特征含 worker 放置（`AutoDopRequest.java:91-150`）；不进在线路径（server 无 cost 引擎） | **两条量化约束无验证证据**：①"OLAP 额外开销 ≤5%"——无任何开销百分比测量（全目录检索无 overhead 数值）；②"OLTP 影响≈0"——无 OLTP 基准（见 G3）。另排序准确率评估脚本缺失（P1） |
| DD01-1.3-G2 | 目标2：智能优化引擎（MCTS 搜索 + 蒸馏策略 + 在线负载修正） | ✅ | `mcts.py` + `search_mcts.py` + `train_policy.py` + `engine.py` PolicyEngine 单次前向 + `load_correction.py` 接入；防震荡三重机制（死区/EWMA/一次性施加）全落地（见 DD03 矩阵） | 效果：蒸馏策略竞争力为负/弱（见 §6 效果表），属效果问题非实现缺口 |
| DD01-1.3-G3 | 目标3：系统实现与验证（模块化解耦 + 混合负载基准） | 🟡 | StarRocks 实现（提交 8945a68528/4380a14bc0）；引擎无关解耦（Python 侧无 StarRocks 类名）；分析型基准齐全（51 并发、三数据集、代表性查询） | **❌ 子项：事务型负载基准（OLTP）未实现**（检索 tools/autodop 全部 .py/.md/.sh 无 tpcc/sysbench/OLTP，仅 TPC-DS/TPC-H/SSB 分析型；P2 缺口，与既有任务 Notes 一致） |
| DD01-3.1-P1 | 轻量：决策服务超时上限 50ms | ✅ | `Config.java:170` `auto_dop_timeout_ms=50`；`HttpAutoDopClient.java:33-37`；实测 8–37ms（M5，L2）；51 并发中位 12ms（L3） | 注意项：51 并发 P99=76ms 超 50ms（高并发 HTTP 重叠）；超时/不可达时查询显式失败无降级旁路——此行为与 DD03 §1.4"显式失败不静默回退"设计一致，但服务不可用即查询失败的鲁棒性需部署方评估（P1 候选） |
| DD01-3.1-P2 | 细粒度：候选 DOP 为 2 的幂且不超过上限 | ✅ | `AutoDopFeatureExtractor.java:197-214`（dop<<=1，上限 min(32, 会话上限)）；`data.py:10` DOP_CLASSES；校验器强制候选集（`AutoDopDecisionValidator.java:60-65`） | — |
| DD01-3.1-P3 | 自适应：采样 2s / EWMA 0.25 / 死区 CPU 0.4·内存 0.65 / 实例化前一次性施加 | ✅ | `Config.java:178-190`；`AutoDopResourceView.java:69-80,176-186`；`load_correction.py:14-27`；`CoordinatorPreprocessor.java:258-263`；单测全覆盖 | 常量与文档逐一一致（已人工比对） |
| DD01-3.1-P4 | 对 OLTP 近零影响：仅 SELECT + pipeline 进入决策路径 | ✅ | `AutoDopController.java:39-53`（非 SELECT/未启 pipeline 直接绕行）；默认关闭 `Config.java:166`；另要求 runtime adaptive DOP off（比文档更严，实现扩展） | 量化验证依赖 OLTP 基准（❌） |
| DD01-3.2-D1 | 决策层 FE 5 组件 | ✅ | Controller / FeatureExtractor / Validator / Applier / TraceManager 五个类逐一对应（提交 8945a68528） | — |
| DD01-3.2-D2 | Python 决策服务单次前向推理 | ✅ | `autodop_server.py:118-143` + `engine.py:342-360` + `HttpAutoDopClient.java:24-63`；真实产物实测（workflow_20260716 / dop_sensitive_sf100 / 树策略） | — |
| DD01-3.2-D3 | 决策结果对象：原始/决策/安装 DOP + 决策耗时 | ✅ | `AutoDopResult.java:16-40` 四字段齐全 | — |
| DD01-3.2-S1 | 两阶段调度（放置/实例化分离） | ✅ | `CoordinatorPreprocessor.java:282-292`；`FragmentAssignmentStrategy` 系拆分（提交 8945a68528） | — |
| DD01-3.2-S2 | 决策窗口（放置已知、路由未固化） | ✅ | `CoordinatorPreprocessor.java:253-263`；路由安全论证 `AutoDopInstanceAdjuster.java:14-25` | — |
| DD01-3.2-S3 | pipeline_dop 下发 BE | ✅ | `TFragmentInstanceFactory.java:205`；`FragmentInstance.java:213-217` 兜底 | — |
| DD01-3.2-E1 | 执行层复用 driver 队列/限额 | ✅ | BE 既有 `pipeline_driver_queue.h` / `driver_limiter.h/.cpp`（`fragment_executor.cpp:817` try_acquire）；本项目无 be/ 改动，复用属实 | — |
| DD01-3.2-E2 | 执行统计经轨迹落盘 | ✅（细微出入） | `AutoDopTraceManager.java:162-224`（JSONL 按日滚动，含 PQueryStatistics 聚合统计）；挂钩 `StmtExecutor.java:3247+` | 出入：落盘为聚合计数（scan_rows/bytes、cpu_cost_ns 等）非算子级 profile；核心"执行统计回流"语义满足 |
| DD01-3.2-F1/F2/F3 | 三条数据流（决策/反馈/离线训练） | ✅×3 | 决策流=窗口链路；反馈流=`data.py:266-289` build_dataset 消费 trace；训练流=build_dataset→train_cost→search_mcts→train_policy 四环节，runs/ 有端到端产物 | — |
| DD01-3.3-M1/M2 | 预估模块（离线）+ 优化引擎模块（MCTS+蒸馏+在线修正） | ✅×2 | 见 G1/G2 与 DD02/DD03 矩阵 | — |
| DD01-3.4-OFF/ON | 端到端离线/在线流程 | ✅×2 | 离线五环节逐一对应并有真实执行产物（runs/workflow_20260716）；在线流程全程接线（`CoordinatorPreprocessor.java:251-279`） | — |
| DD01-3.5-T1/T2 | 技术选型落地：两阶段改造完成、决策窗口真实存在 | ✅×2 | 提交 8945a68528；集成测试 `AutoDopInstanceAdjustIntegrationTest.java:74/153/188` | — |
| DD01-2/4/5 | §2 现状调研、§4 术语表、§5 参考文献 | ⚪×3 | 纯调研/元内容，无实现要求 | — |

---

## 3. DD02 核对矩阵（细粒度查询的预测详细设计）

检索范围：`tools/autodop/` 全部 Python 模块/脚本/测试、FE autodop 包请求与特征侧、`runs/dop_sensitive_sf100/`、`runs/tpch100_validation/`、`runs/workflow_20260716/`、提交 8945a68528；关键词 `accuracy|准确率|evaluate|eval|retrain|增量` 全目录检索。

| 条目ID | 文档条目 | 状态 | 证据 | 缺失点/说明 |
|---|---|---|---|---|
| DD02-1.3-输入 | 计划特征+DOP 配置+实测延迟三元组 | ✅ | `AutoDopTraceManager.java:253-288` 落盘 + `data.py:143-183 sample_from_trace` 解析 | — |
| DD02-1.3-输出/用途 | 相对性能分数；仅离线搜索评估、不进在线路径 | ✅×2 | `model.py:103-120` CostModel；server 无 cost 引擎，在线仅 PolicyEngine 单次前向 | — |
| DD02-1.3-训练触发 | 按语料增量周期自动重训替换 | ❌ | 无任何增量检测/自动触发机制（`retrain|增量|trigger` 全目录无匹配）；历史上 3 次手工重训（sf10/sf100/tpch100） | 机制缺位不影响已产出效果，但"自动闭环"未落地 |
| DD02-1.4-可复现性 | 固定种子+固定训练/验证划分 | 🟡 | 种子 ✅（`train_cost.py:26-27`）；**训练/验证划分 ❌**（无 split，全部 pairs 训练）；语料追溯仅有 metadata 计数无内容哈希 | 缺验证划分（与 6.1 第二层同源缺口） |
| DD02-1.4-数据校验 | 缺失/异常记录剔除 | 🟡 | 字段校验 ✅（`data.py:143-183` + 单测 `test_dataset_rejects_missing_request`）；**语义差异**：实现为校验失败即中止构建（fail-fast），非文档"剔除"；state 校验仅在采集端 `collect_dop_training.py` | 行为差异：单条坏记录会阻塞整条构建 |
| DD02-1.4-可配置性 | 轮数/批次/阈值可配；候选 DOP 上限可配 | 🟡 | 前三项 ✅（`train_cost.py:22-25`、`build_dataset.py:18-19` 默认 0.03）；**Python 侧 DOP_CLASSES 硬编码** `data.py:10` [1,2,4,8,16,32] 无配置入口（FE 侧继承会话上限 ✅）；FE 候选超出时 build_graph 直接 ValueError | 训练/搜索侧上限不可配 |
| DD02-1.4-数据安全 | 样本不含 SQL 文本 | ✅ | `AutoDopRequest.java:90-175` 全结构化，无 SQL 字段 | — |
| DD02-1.5-场景覆盖 | 固定配置模式采集、多计划形状 | ✅ | `collect_dop_training.py`（uniform + 部分 fragment 指定 + warmup/重复取中位数）；9 plans/126 配置/572 pairs（report.md:31-34） | — |
| DD02-2.1-流程 | 清洗→聚合→配对→编码→训练→**评估门控产出** | 🟡 | 前五环节 ✅（`data.py:192-227`、`graph.py:57-94`、`train_cost.py:40-62`）；**末环节 ❌**：无验证划分、无准确率门限，训练完无条件保存 | 与 DD02-6.1-模型精度验证为同一缺口，报告合并计数 |
| DD02-2.2-离线限定 | 预估离线、在线仅策略一次推理 | ✅ | CostModel 仅被离线脚本引用；`engine.py:340-357` 单次前向无逐配置打分循环 | — |
| DD02-2.3-模块链 | 四模块单向依赖链 | ✅ | data.py→graph.py→model.py→mcts.py 无循环依赖 | — |
| DD02-3.1/3.3 | 特征来源与获取位置 | ✅×2 | FE 计划对象/统计信息提取（`AutoDopFeatureExtractor.java:76-117`）；决策窗口内提取、同模块定义请求/响应 | — |
| DD02-3.2-类别特征 | 10 项类别特征+4 布尔 | ✅ | `AutoDopRequest.java:108-162` 与 `data.py:11-21` 完全对应 | — |
| DD02-3.2-数值特征 | 11 项数值特征，log 后标准化 | ✅ | `AutoDopRequest.java:132-150` + operator_counts；`data.py:23-33,230-264`（log1p + 均值/方差标准化） | — |
| DD02-3.2-图结构 | 依赖边 + 竞争边（Jaccard 权重） | ✅ | `graph.py:42-54`（竞争边 Jaccard，单测断言 0.5）、`graph.py:57-73`（依赖边） | — |
| DD02-4.1-编码器 | **图注意力网络（GAT）：沿两类边聚合邻居** | 🟡 | 实现为 `DenseAttentionBlock`（`model.py:14-48`）：**全节点稠密自注意力**，dependency/competition 仅作 logits 可学习偏置（`dependency_scale`/`competition_scale`），softmax 不按边掩码 | **机制差异（主会话已复核代码）**：非文档所述"沿两类边聚合邻居信息"的标准 GAT 消息传递；两类边调制语义已实现。影响：文档声称的图结构归纳偏置与实现不完全一致（全连接注意力在 fragment 数少时可接受，但规模增大时 O(n²) 成本与邻域聚合语义不同） |
| DD02-4.1-池化打分 | mean+max+根表示→MLP→标量 | ✅ | `model.py:122-132`（root 取 is_gather_root） | — |
| DD02-4.1-DOP编码 | 节点特征含配置 DOP 编码 | ✅ | `model.py:83` dop_embedding；`graph.py:74-84`（unknown_dop 全 0 供在线复用） | — |
| DD02-4.1-排序损失 | softplus(−(S(C⁺)−S(C⁻))) | ✅ | `train_cost.py:54-61` 与文档公式逐字一致 | — |
| DD02-4.2 | 采集/清洗聚合（中位数）/配对（<3% 剔除）/训练 | ✅×4 | `collect_dop_training.py`；`data.py:192-206`（median，单测 105.0）；`data.py:209-227`（默认 0.03 阈值）；`train_cost.py:40-62`（每 epoch 打乱+批次+Adam） | — |
| DD02-5.1/5.2/5.3 | 训练/评估两条路径、评估接口、产物格式五要素 | ✅×3 | `train_cost.py` 全流程；`mcts.py:78-83`（复用图结构只换 DOP 编码、eval+no_grad）；`model.py:149-158` save_checkpoint（权重+结构+vocab+均值方差+dop_classes） | 评估接口以函数形式存在（无独立 CLI/服务端点），语义相符 |
| DD02-6.1-指标 | 排序准确率 | 🟡 | 有一次度量：report.md:34 "CostModel pair accuracy 93.36%"（9 plans/572 pairs） | **无独立评估脚本，93.36% 不可复现**（全目录检索无 accuracy 计算代码；数值与另一文件 workload_improvement_pct 巧合相同，来源存疑）——P1 锚点复核成立 |
| DD02-6.1-数据质量验证 | 样本对数量与过滤比例检查 | 🟡 | 数量 ✅（build_dataset 打印 + metadata pair_count）；**过滤比例与计划形状覆盖度 ❌** | — |
| DD02-6.1-模型精度验证 | 独立评估流程（不参与训练）+ 验证划分 | ❌ | train_cost.py 无验证划分、无独立评估流程（`model.eval()` 仅服务搜索） | P1，与既有任务 Notes 锚点一致 |
| DD02-6.1-效果验证 | 第三层：最高/最低分配置实测对比 | ❌ | runs/ 全部报告无该实验 | 证据不足 |
| DD02-6.2 | 三模块代码结构 | ✅ | FE 决策模块 / 决策服务（server+engine）/ 离线数据与训练模块（data/graph/scripts） | — |
| DD02-6.2-配置项 | 关键配置项：训练轮数/批次/阈值 3%/候选 DOP 上限 | 🟡 | 前三项 ✅（`train_cost.py:22-25`、`build_dataset.py:18-19` 默认 0.03）；候选上限 Python 侧硬编码 `data.py:10`（同 DD02-1.4-可配置性） | 与 1.4-可配置性同一缺口，不重复计数 |
| DD02-6.3-输出形状测试 | 不同规模计划图输出标量 | 🟡 | `tests/test_autodop.py:112-118` 形状断言 ✅；**仅 2 节点单一规模**，无多规模参数化 | — |
| DD02-6.3-排序方向测试 | 快配置得分更高 | ❌ | tests/ 无任何 CostModel 排序方向断言 | P1 相关 |
| DD02-6.3-回归基线对比 | 与绝对延迟回归基准对比排序准确率 | ❌ | 无 MSE 回归基准训练代码 | 未实现 |
| DD02-6.3-可复现性测试 | 固定种子两次训练结果一致 | ❌ | train_cost.py 有 --seed，但无重复训练一致性测试 | 未实现 |

---

## 4. DD03 核对矩阵（细粒度查询调度以及资源管理模式详细设计）

检索范围：FE autodop 包、`CoordinatorPreprocessor.java`、`Config.java:166-196`、`SessionVariable.java`、Python 全部模块/脚本/测试、提交 8945a68528/4380a14bc0、journal-1.md、13 份报告 + runs/。

| 条目ID | 文档条目 | 状态 | 证据 | 缺失点/说明 |
|---|---|---|---|---|
| DD03-1.3-离线输入/输出 | 计划样本+预估分数 → 蒸馏标签+策略网络 | ✅×2 | `search_mcts.py:62-82` + `train_policy.py:99-106` | — |
| DD03-1.3-在线输入/输出 | 计划特征（含放置）+实时负载 → 每 fragment 候选集内 DOP | ✅×2 | `AutoDopFeatureExtractor.java:52-58` + `AutoDopResourceView.java:196-212`（worker_loads）；`engine.py:345-362` + `AutoDopDecisionValidator.java:27-64` | — |
| DD03-1.3-触发时机 | 编译期、放置后实例化前；修正与决策同一次安装 | ✅ | `CoordinatorPreprocessor.java:252-263` | — |
| DD03-1.3-消费方 | 安装器写管道并行度；轨迹记录 | ✅ | `AutoDopDecisionApplier.java:28-38`；`AutoDopTraceManager.java:63-71,190-215` | — |
| DD03-1.4-稳定性 | 不可达/超时/非法值显式失败 | ✅ | `AutoDopController.java:57-66` + Validator 全抛异常 + 单测；无静默回退路径（已检索全部 catch 分支） | 与 DD01 注意项呼应：硬失败是设计使然 |
| DD03-1.4-可配置性 | 总开关全局+会话级；候选上限；采样/平滑/死区可配 | ✅ | `Config.java:166` + `SessionVariable.java:313,1333,3935`；`auto_dop_max_dop`；`Config.java:178-190`；server CLI `--load-*` | 说明：fragment 级修正死区阈值在 **server CLI**（默认与文档一致），FE 侧同名 Config 仅被 DD04 消费，需部署方对齐——已记录非缺口 |
| DD03-1.4-数据安全 | 请求不含 SQL 文本 | ✅ | `AutoDopRequest.java` 全文结构化 | — |
| DD03-2.1/2.2/2.3 | 决策流程两段（离线/在线/产物衔接）、决策窗口两性质（放置可用/修改安全）、四模块（配置搜索/策略蒸馏/在线决策/负载采集）、两条单向链 | ✅×10 | `CoordinatorPreprocessor.java:252-264`；窗口性质（放置可用+路由未固化）journal-1.md 论证；search_mcts→train_policy（离线链）、ResourceView→server（在线链） | — |
| DD03-3.1 | 配置搜索：部分赋值/重要性排序/随机补全/固定模拟次数/回报最高为标签 | ✅×5 | `mcts.py:9-23,58,68-70`；`graph.py:127-136`（cost+0.5·scan+0.2·degree 加权）；`search_mcts.py:39`（默认 500 次） | 权重系数为实现选择，文档未给具体值 |
| DD03-3.2 | 策略蒸馏：共享编码器/分类头/DOP 置零/CE+掩码/argmax | ✅×5 | `model.py:63-97` GraphEncoder 共用 + `train_policy.py:76-77` 用 cost 权重初始化；`model.py:100-143`；`train_policy.py:80` + `graph.py:88-89`（零编码单测）；`model.py:142` masked_fill；`engine.py:352-353` | 细节偏差（已人工复核，行为等价）：①实现为**权重共享单一分类头**逐 fragment 应用（文档"每 fragment 一个分类头"字面差异）；②CE 为逐计划 mean（文档"之和"，差常数，梯度方向一致） |
| DD03-3.3 | 每计划一个标签 | ✅ | `search_mcts.py:67-69` + `train_policy.py:53-56` 一一对应 | — |
| DD03-4.1 | 在线决策单次前向、无迭代无重试 | ✅ | `engine.py:350-354`；`HttpAutoDopClient.java:25-52` 单次调用 | — |
| DD03-4.2 | **负载修正全部细节**：采样 2s / EWMA 0.25 / 死区 0.4·0.65 / 线性下降触底 0.1 / 双维取小 / 多 worker 取最小 / fragment 粒度 / 候选集取整 / 一次性施加 | ✅×9 | 常量逐一比对一致：`Config.java:178-190`、`load_correction.py:19-27`（阈值下=1、线性降、floor 0.1）、`:33-37`（min 双维）、`:40-49`（min over workers，单测 weakest_worker_wins）、`:77-91`（fragment 级 apply_to_decision）、`:55-73`（corrected_dop 候选集内≥1）、`CoordinatorPreprocessor.java:256`（一次性）；`AutoDopResourceViewTest.java:56-66`（EWMA）；`tests/test_load_correction.py` 15 用例 | ①取整语义：实现为"≤目标的最大候选"（只降不升），文档字面"最接近"允许上取整——与文档"有界修正、不叠加并行度"意图一致，判 ✅ 并在文档问题清单标注字面差异；②采集机制：实现读 BE 心跳上报缓存（`AutoDopResourceView.sampleOnce`），文档"轮询指标端点"为描述差异，功能等价 |
| DD03-4.3 | 决策安装：写入管道并行度、安装后不改、轨迹记录四 DOP | ✅×3 | `AutoDopDecisionApplier.java:28-38`；窗口位置保证；`AutoDopTraceManager.java:190-215`（original/decided/installed/actual）+ M5 验证 | — |
| DD03-5.1/5.2 | 离线/在线执行流程、请求会话信息、片段字段、边列表、响应合法性 | ✅×6 | `CoordinatorPreprocessor.java:252-264` + TraceManager 落盘；SessionInfo 7 字段、FragmentInfo 20 字段、EdgeInfo 4 字段齐全（`AutoDopRequest.java:40-135`）；Validator 拒绝缺失/重复/越界（单测） | — |
| DD03-5.3/5.4 | 负载采集接口、模型产物格式与加载 | ✅×3 | `AutoDopResourceView.java:66-90,173-185,196-212`；`model.py:149-157` save_checkpoint + `engine.py:346` 加载 + 推理零编码 | — |
| DD03-6.1-搜索质量 | 搜索配置实测延迟 vs 实测最优 | 🟡 | 间接证据：CostModel accuracy 93.36%、MCTS 消融（2.726%→9.588%）、policy 复现 141/141 标签（report.md §Training/Ablation） | **直接指标缺失**：逐计划"搜索配置 vs 实测最优"的延迟差距表无任何报告给出（检索全部 13 份 md + runs + journal） |
| DD03-6.1-决策质量 | 策略输出 vs 固定全局基线、并发稳定性 | ✅ | 多报告 L3：dop_sensitive（vs DOP4 mean 15.970%）、fragment_advantage_v2（并发 2，7.793%）、三数据集（TPC-DS/SSB ≥30% 正式）、REPRESENTATIVE_SERIAL_V2（树策略 39.393%）、HETEROGENEOUS（34.994%） | 注意：以上主要来自规则/树策略路径，蒸馏策略为负效果（§6） |
| DD03-6.1-修正效果 | 高负载开/关修正对照 | ✅ | M5（journal-1.md）：低负载保持 policy、高负载 8→4（:load_corrected）、--no-load-correction 对照 decided 保持 8、开关回归 | 证据仅存于 journal（L2，无独立报告 md）——标注证据位置特殊 |
| DD03-6.1-开销验证 | 各环节耗时在预算内 | ✅ | M5 8–37ms（L2）；fragment_advantage_v2 中位 3ms/P99 6.91ms（L3）；负例：60ms 级查询 RPC+推理开销主导 -12.362%（dop_sensitive report） | — |
| DD03-6.2 | 代码结构：两阶段调度、决策服务四模式、离线训练模块、关键配置项 | ✅×4 | `CoordinatorPreprocessor.java:244-280`；`autodop_server.py:109-146`（--fragment-dops/--operator-subgraph/--policy-model/--tree-policy 互斥 + 扩展模式）；`search_mcts.py`/`train_policy.py`；配置数值全对（50/2000/0.25/0.4/0.65/0.1） | — |
| DD03-6.3 | 三类测试设计（搜索/决策服务四模式/负载修正） | ✅×3 | `tests/test_autodop.py:112-190`（候选集断言、allowed_configs、四引擎模式、零编码）；`tests/test_load_correction.py` 全用例 | — |

---

## 5. DD04 核对矩阵（分布式环境下的资源预测以及管理的调度系统详细设计）

检索范围：AutoDopResourceView / AutoDopInstanceAdjuster / AutoDopTraceManager / AutoDopRequest / CoordinatorPreprocessor / Config / ComputeNode / GlobalVariable / ExecutionFragment / ExecutionDAG / 单测+集成测试 / load_correction.py / 提交 4380a14bc0 / 13 份报告逐一 grep（实例调整/异构关键词）+ journal。

| 条目ID | 文档条目 | 状态 | 证据 | 缺失点/说明 |
|---|---|---|---|---|
| DD04-1.3-节点资源感知 | 周期采集 BE CPU/内存、按节点聚合、供两消费方 | ✅ | `AutoDopResourceView.java:88-104`（读 ComputeNode 上报的 cpu_used_permille/mem_used_pct）+ `:176-186` fillWorkerLoads；`Config.java:178`（2s） | — |
| DD04-1.3-实例并行度调整 | 紧张降、充裕保持、有界、候选集取整≥1 | ✅ | `AutoDopInstanceAdjuster.java:117-170`（仅下调、dimensionFactor 死区+线性降、correctedDop 取整≥1）；单测 + 集成 `AutoDopInstanceAdjustIntegrationTest.java:74` | 取整方向同 DD03（≤目标的最接近候选），与 §4.2"只降不升"自洽 |
| DD04-1.3-片段类型判定 | 无上游输入 ∧ 非 colocate ∧ 非 bucket shuffle | ✅ | `AutoDopInstanceAdjuster.java:43-60`；`ExecutionFragment.java:294-317`（isColocated 递归、isLocalBucketShuffleJoin 查 JoinNode.isLocalHashBucket）；带 exchange 的 bucket shuffle 被条件 1 覆盖 | — |
| DD04-1.3-轨迹记录 | 基准/调整后/最终三份实例并行度 | ✅ | `AutoDopTraceManager.java:100-151`（BASELINE/ADJUSTED 两时点）+ `:221-238`（baselineInstanceDops/adjustedInstanceDops/instanceDops 字段，已人工复核 `@SerializedName`）；接线 `CoordinatorPreprocessor.java:259-278`；集成断言 baseline≥adjusted≥final | — |
| DD04-1.4-稳定性 | 缺失/过期退化为不调整、不阻塞 | ✅ | `AutoDopInstanceAdjuster.java:113-115`（continue 保持基准）；`load_correction.py:37-42`（factor=1）；`AutoDopResourceViewTest.java:69` | — |
| DD04-1.4-可配置性 | 采集周期/死区/幅度上限可配 | ✅ | `Config.java:178/184/187/193`（幅度上限以 floor 0.5 表达，语义等价） | — |
| DD04-2.1-增量位置 | 插在两阶段之间、共用窗口 | ✅ |
| DD04-2.1-分工 | 放置沿用原生（本地性优先）；DOP 决策沿用单机（策略推理+负载修正）；本部分做实例差异化 | ✅ | `CoordinatorPreprocessor.java:252-256`（AutoDOP 路径仍走原生 `assignAllPlacements`）；负载修正在 `load_correction.py` 应用于策略输出 | — | `CoordinatorPreprocessor.java:249-278`（assignAllPlacements → decideAndInstall → materializeAllFragments → BASELINE → adjust → ADJUSTED → finalizeDAG → captureActualDop，已人工复核） | 机制细节（非缺口）：文档称"实例未建"时调整，实现为实例已 materialize（数量固定）后、finalizeDAG 前调整；路由表与 driver 扫描范围在 finalizeDAG 用调整后 DOP 生成，行为等价（`ExecutionDAG.java:300/538-540`）——记入文档问题清单 |
| DD04-2.2/2.3 | 调整流程四步、两新模块、依赖链 | ✅×4 | 接线同上；资源视图=AutoDopResourceView、实例调整=AutoDopInstanceAdjuster；依赖链 放置→资源视图→基准决策→实例调整→实例化 顺序成立 | — |
| DD04-3.1-采集 | 默认 2s、复用 BE 指标通道、同口径 | ✅ | `Config.java:178`；`ComputeNode.java:447-451` updateResourceUsage（BE 每 1s 心跳上报 TResourceUsage）；同一份数据两处消费 | — |
| DD04-3.2-归一化 | **按容量（核数、内存量）归一化，使异构节点可比** | 🟡 | 按 BE 聚合 ✅；**归一化为使用率口径**：`cpu_used_permille/1000`、`mem_used_bytes/mem_limit_bytes`（`AutoDopResourceView.java:99-101`，已人工复核）；autodop 包内无任何核数/内存量容量项参与计算（grep 确认无 getNumCores/容量消费） | **文档"按容量归一化"显式机制未落地**；行为意图（同等绝对负载在小节点紧张度更高）经 BE 上报使用率**间接**成立（permille 已含容量折算），但"使用率相同、容量不同"的节点得到完全相同的调整因子。连带影响：DD04-6.3 归一化测试缺位、6.1 异构节点验证无法严格落地 |
| DD04-3.3-时效缺失 | 滞后一周期、缺失中性值、异常节点处理 | ✅（带说明） | 滞后=周期采样天然性质；中性值=保持基准/factor=1；新鲜度窗口复用 `query_queue_resource_usage_interval_ms`（默认 5s，`ComputeNode.java:720-726`）；死亡节点由原生放置排除（isAvailable） | 说明："连续多周期未上报→退出放置候选"未作独立机制：新鲜度为单窗口判断，存活但上报过期的 BE 仍可被放置、仅调整中性化——文档自述"沿用既有放置语义"，基本一致 |
| DD04-4.1/4.2 | 调整对象三条件、调整规则四步（死区/只降不升/取整≥1/同 BE 一致） | ✅×2 | `AutoDopInstanceAdjuster.java:43-60,108-170`（因子按 worker 计算，同 BE 实例同因子；候选回退 2 的幂）；单测 `AutoDopInstanceAdjusterTest.java:52/62/74` | — |
| DD04-4.3-衔接 | 与负载修正同视图、同向 | ✅ | 同视图（实例调整直查 `effectiveInstance().getWorkerLoad`，修正消费 fillWorkerLoads 同源）；因子公式同构（floor 0.5 vs 0.1），均只收缩；M5 双开同向验证 | — |
| DD04-5.1/5.2 | 执行流程、资源接口 | ✅×2 | `CoordinatorPreprocessor.java:249-278`；`AutoDopResourceView.java:74-86` getWorkerLoad（cpu/mem/fresh）+ fillWorkerLoads | 归一化字段为使用率口径（受 3.2 影响） |
| DD04-5.3-配置项 | 采集周期/死区/幅度上限/**中性值**/**异常节点判定周期** | 🟡 | 前三项 ✅（Config.java:178/184/187/193）；**后两项 ❌**：中性值硬编码（非 fresh 置 0.0，无配置项）；异常判定周期复用既有全局变量（非 auto_dop_* 形态） | 文档 5 项配置 2 项无专属配置，影响"可配置性"声明完整性（§1.4 三项均满足） |
| DD04-6.1-按资源调整验证 | 不均衡场景：高负载 BE 实例更低且**延迟不劣于基线** | 🟡 | 行为侧 ✅：集成测试 `AutoDopInstanceAdjustIntegrationTest.java:74` + M5 真实集群（mem 79.6% BE 4→2、70.4% BE 保持 4）；**延迟对比 ❌**：M5 未测"调整 vs 不调整"延迟 | 证据缺口：文档验证方法中的延迟不劣化无实验测量 |
| DD04-6.1-路由安全验证 | 非扫描/colocate/bucket shuffle 不变、结果正确 | ✅ | 集成断言 distinct==1（`:140-143`）+ DOP≥1（`:125-126`）；M5 count 正确 | — |
| DD04-6.1-异构节点验证 | 小规格节点实例被压低、常规保持 | ❌ | 检索 13 份报告 + runs/ + journal：唯一异构报告（HETEROGENEOUS_TPCDS_SF100）为**片段级**实验（2026-07-23 早于实例调整实现），无实例级测量；现有测试均为负载注入维度（10001/10002 worker），无容量维度 | 无对应实验/测试（与 3.2 归一化 🟡 联动：无容量项时该验证本无法严格落地）——P2 |
| DD04-6.1-衔接验证 | 同视图、同向、反映到实例创建与扫描范围分配 | ✅ | 集成 `AutoDopInstanceAdjustIntegrationTest.java:188`（三阶段轨迹）；M5 --no-load-correction 对照（修正关闭实例调整独立生效 [8,8]→[4,4]） | — |
| DD04-6.2 | 两新模块代码结构 | ✅ | `AutoDopResourceView.java`（212 行）+ `AutoDopInstanceAdjuster.java`（166 行），提交 4380a14bc0 | — |
| DD04-6.3-资源视图测试 | 采集聚合/归一化/中性值/过期标记 | 🟡 | `AutoDopResourceViewTest.java:46-97`（首样本/EWMA/过期中性化/未知 worker/fillWorkerLoads）**无"规格归一化计算"用例** | 与 3.2 一致 |
| DD04-6.3-调整规则测试 | 高紧张低、充裕保持、候选集内、幅度有界、路由安全 | ✅ | `AutoDopInstanceAdjusterTest.java:52/62/74` + 集成 `:74/153`（差异化+开关回归） | — |
| DD04-6.3-衔接测试 | 同视图同向、进入实例创建、结果正确 | ✅ | 集成 `:188` + M5 + `tests/test_load_correction.py` | 无单一测试显式断言两消费方同视图（单例构造保证，可接受） |

---

## 6. 效果汇总表

> 判定说明：效果评估基于现有证据汇总（选项 A），未补跑实验；无证据处明确标注"证据不足"。**正负证据并列呈现，不选择性引用。**

| 功能点 | 量化指标 | 证据 | 置信度 |
|---|---|---|---|
| **fragment AutoDOP vs 最优固定全局 DOP（决策质量，规则/树策略路径）** | 51 并发异构批次：2262.7ms vs DOP=4 3480.7ms（+34.994%）、vs DOP=8 3702.8ms（+38.894%） | HETEROGENEOUS_TPCDS_SF100_AUTODOP_REPORT.md（2 组×10 次，1122 条 trace 0 error） | L3（强） |
| 同上（三数据集正式认证） | TPC-DS 100/100 通过 ≥30% 硬门槛（最坏改进分布 31.6%–59.7%）；SSB 99/100（1 条尾延迟异常失败）；TPC-H 100 条仅预筛（选择偏差，未正式） | THREE_DATASET_EXPERIMENT_REPORT.md + runs/ 校验文件 | L3（TPC-H 较弱） |
| 同上（代表性查询串行） | 树策略 mean 1352ms vs 2231ms（+39.393%），8/9 查询过 30% 门槛 | REPRESENTATIVE_SERIAL_V2_REPORT.md（5 次重复轮换） | L3 |
| 同上（并发 2） | vs 最佳固定 DOP：+7.793%，49/50 轮胜 | runs/fragment_advantage_v2/report.md | L3 |
| **蒸馏策略网络（在线实测，负面）** | TPC-DS SF10 97 查询 vs 全局 DOP=4：**-38.2%**（1 快 96 慢）；repeat2 **-80.6%**（0/97）——主会话已复核 JSON 原始数据 | runs/workflow_20260716/tpcds_baseline_vs_policy_*.json | L2（负效果，重要） |
| 重训后蒸馏策略（9 敏感查询） | v1 +2.7% / v2 +9.6% / v4 +15.3%（8/9 快），远未达 30% 线 | runs/dop_sensitive_sf100/report.md | L3 |
| CostModel 排序准确率 | 93.36%（572 pairs）——**无脚本可复现，数值存疑** | runs/dop_sensitive_sf100/report.md:34 | L3（弱，证据不足） |
| MCTS 搜索质量 | 消融：unconstrained +2.726% → measured-only +9.588% → position-aware +15.314%；policy 离线复现 141/141 标签 | runs/dop_sensitive_sf100/report.md | L3（间接） |
| **负载修正通道（高/低负载对照）** | 低负载（mem 2.6%）修正不触发（reasons=policy）；高负载（mem 70–99%）DOP 8→4（reason=policy:load_corrected）；--no-load-correction 对照 decided 保持 8 | journal-1.md M5（真实 3-BE 集群） | L2（仅 journal 记录，无独立报告） |
| **实例级差异化（DD04）** | mem 79.6% BE 实例 4→2、mem 70.4% BE 保持 4；三阶段 baseline=[4,4]≥adjusted=[4,2]≥final=[4,2]；开关关闭全保持基准；查询结果正确 | journal-1.md M5 + 集成测试（数值与 dimensionFactor 公式手算一致） | L2 |
| 实例调整的延迟收益（调整 vs 不调整） | **无测量** | 13 份报告 + runs/ + journal 检索无 | 证据不足 |
| 决策开销 | M5 8–37ms ≤50ms；fragment_advantage_v2 中位 3ms/P99 6.91ms；**51 并发 P99 76ms 超 50ms**（含 HTTP 重叠，已计入 makespan 未扣除） | journal-1.md；fragment_advantage_v2；HETEROGENEOUS 报告 | L2/L3 |
| 安装正确性 | 正式 trace：FE 安装值 vs BE actual DOP **零不一致**、0 error、0 spill；结果 digest 无漂移 | fragment_advantage_v2 / THREE_DATASET 报告 trace 校验 | L3 |
| 负例边界（效果诚实性佐证） | 50 查询宽筛仅 4 条过 30% 门槛（mean +10.920%，P99 -1.147%）；原生 SSB 审计版仅 +13.2%；Q56（60ms 级）负例 -12.362% | REPRESENTATIVE_SERIAL_V2 / THREE_DATASET / dop_sensitive 报告 | L3（负例被如实保留，增加证据可信度） |
| 训练管线产物存在性 | 3 组 cost checkpoint、4 份 MCTS labels、多份 policy/tree 产物 | runs/ 目录 | L1/L4 |

**效果总评**：
1. **真正被认证的效果（30%+）全部来自规则引擎（operator-subgraph/topology/heterogeneous）与树策略路径**；蒸馏策略网络（DD02/DD03 文档的主推在线决策产物）在最早的真实集群对照中为**显著负效果**（-38.2%/-80.6%），经重训提升至 +9.6%~+15.3%，但仍未达到宣称线。
2. **负载修正与实例级调整（最新实现）行为正确性已验证**（低/高负载对照、差异化、开关回归、结果正确），但**"延迟不劣化"收益尚无实验测量**，且证据仅存在于 journal（无正式报告）。
3. **决策开销达标**（低并发 8–37ms ≤50ms），高并发 P99 76ms 超限且服务不可用时查询硬失败——部署前需评估鲁棒性。
4. 报告的负例被如实保留（宽筛失败、SSB 单条失败、Q56 负例），效果证据可信度较好；但 TPC-H 100 条仍缺正式认证，SSB 尚有 1 条待替换复验。

---

## 7. 缺口清单（按严重程度分级）

> 分级定义：P0=文档核心能力声明受影响或在线链路缺陷；P1=重要能力/验证缺位，影响结论严谨性；P2=扩展性/完备性缺口。

### P0 级

| 缺口 | 位置 | 影响面 | 说明 |
|---|---|---|---|
| （无） | — | — | 四个文档的核心在线链路（决策窗口、负载修正、实例调整、轨迹）均已实现且行为验证通过；未发现 P0 级未实现项 |

### P1 级

| 缺口 | 位置 | 影响面 | 证据锚点 |
|---|---|---|---|
| 决策服务不可用/超时时查询硬失败，无降级旁路；高并发 P99 76ms 超 50ms 预算 | DD01 §3.1"轻量"承诺、§3.4 在线流程 | "轻量/对 OLTP 近零影响"承诺的鲁棒性 | `AutoDopController.java:55-84`；HETEROGENEOUS 报告 §Trace |
| 蒸馏策略网络在线效果为负/弱（-38.2%/-80.6% → 重训后 +9.6%~+15.3%） | DD01 §3.3/3.4、DD03 §3 蒸馏设计 | 文档主推的"策略网络"决策路径竞争力未达成宣称效果（30% 认证全部来自规则/树策略路径） | runs/workflow_20260716/*.json；runs/dop_sensitive_sf100/report.md |
| DD02 排序准确率独立评估流程缺失（无验证划分、无评估脚本、无准确率门控；93.36% 不可复现） | DD02 §2.1 末环节、§6.1 第二层、§6.3 测试② | DD02 核心指标"排序准确率"无可靠度量 | train_cost.py 无 split/eval；report.md:34（唯一数值存疑） |
| DD02 训练触发自动化缺失（增量重训/替换） | DD02 §1.3 | "按语料积累周期触发重训"功能需求未落地 | 全目录检索无 retrain 触发机制 |
| DD02 回归基线对比缺失 | DD02 §6.3 测试③ | "与回归方案对比"的模型竞争力论证缺失 | 无 MSE 回归训练代码 |
| 负载修正/实例调整的"延迟收益"无实验测量 | DD03 §6.1 修正效果、DD04 §6.1 按资源调整 | 行为正确性已验证，但收益量化证据不足（仅 journal 记录） | journal-1.md M5（无延迟对比） |

### P2 级

| 缺口 | 位置 | 影响面 |
|---|---|---|
| 资源视图无按容量（核数/内存量）归一化，仅使用率口径 | DD04 §3.2、§6.3 | 异构节点可比性为文档显式机制；现状"使用率相同容量不同"的节点因子相同 |
| DD04 异构节点（小规格 vs 常规）实例级实验缺失 | DD04 §6.1 | 验证方法四项中一项无落地（与归一化缺口联动） |
| OLTP 事务型负载基准缺失 | DD01 §1.3 目标3、§3.1-P4 | "对 OLTP 近零影响"与"OLAP 开销 ≤5%"两条量化约束无法验证 |
| 中性值/异常节点判定周期无专属配置项 | DD04 §5.3 | 可配置性声明不完整（§1.4 三项已满足） |
| Python 侧候选 DOP 上限硬编码（DOP_CLASSES） | DD02 §1.4、§6.2 | 训练侧上限不可配，FE 超集时 ValueError |
| DD02 数据校验为 fail-fast 中止而非"剔除" | DD02 §1.4 | 单条坏记录阻塞整条构建 |
| 50ms 超时与 P99 76ms 的口径关系未澄清 | DD01 §3.1（效果注意） | 报告称 0 error，与客户端 50ms 超时的关系未说明（服务端耗时 vs 客户端超时口径） |

---

## 8. 文档问题清单（文档描述与实现/文档间的差异，非实现缺口）

| # | 问题 | 文档 | 说明 |
|---|---|---|---|
| 1 | "取候选集中最接近的值" vs 实现"≤目标的最大候选"（只降不升） | DD03 §4.2、DD04 §1.3 | 实现与"有界修正/不叠加并行度"意图一致，字面允许上取整；两处文档表述应统一为"不超过缩放值的最接近候选" |
| 2 | GAT 邻域聚合 vs 稠密注意力+边偏置 | DD02 §4.1 | 实现为 edge-biased dense self-attention（`DenseAttentionBlock`），非标准 GAT 消息传递；两类边调制语义成立 |
| 3 | "轮询各 worker 的指标端点" vs 读取 BE 心跳上报缓存 | DD03 §4.2、§5.3 | 功能等价（复用既有通道，无额外轮询开销），描述应更新 |
| 4 | "实例未建时调整" vs 实例已 materialize（数量固定）后、finalizeDAG 前调整 | DD04 §2.1 | 行为等价（路由表与扫描范围分配均在 finalizeDAG 生成），机制表述应更新 |
| 5 | "连续多周期未上报→退出放置候选" vs 单窗口新鲜度+原生放置排除 | DD04 §3.3 | 实现依赖原生放置语义（文档自述沿用），"连续多周期"表述与实现不符 |
| 6 | "按容量（核数、内存量）归一化" vs 使用率口径 | DD04 §3.2 | 见缺口清单 P2；文档应明确"使用率即相对自身容量"的语义或要求显式容量项 |
| 7 | 轨迹落盘为聚合统计 vs 文档"性能画像" | DD01 §3.2-E2 | 落盘 PQueryStatistics 聚合计数，非算子级 profile；核心回流语义满足 |
| 8 | 死区/下限阈值 FE 与 server 双处配置（默认一致但需人工对齐） | DD03 §1.4/§6.2 | FE Config 项仅被 DD04 消费，DD03 修正阈值在 server CLI；部署时需对齐 |

---

## 9. 附录：核对方法与证据等级

- 核对方式：4 个并行 trellis-research 子代理（每文档一个）产出草稿矩阵 → 主会话逐条复核 → 对 8 个关键存疑点人工验证代码（GAT 编码器 `model.py:14-48`；负载修正常量 `load_correction.py:19-27` + `Config.java:166-196`；资源视图归一化 `AutoDopResourceView.java:99-101`；决策窗口 `CoordinatorPreprocessor.java:249-278`；轨迹三阶段 `AutoDopTraceManager.java:221-238`；蒸馏策略效果 JSON `runs/workflow_20260716/tpcds_baseline_vs_policy_*.json`；93.36% 无脚本可复现确认；`AutoDopInstanceAdjustIntegrationTest` 断言）。
- 检索边界：FE autodop 包 12 类 + CoordinatorPreprocessor + Config + SessionVariable + StmtExecutor + ComputeNode + GlobalVariable + ExecutionFragment/DAG；Python 8 模块 + 5 脚本 + 3 测试文件 + server；BE 复用机制（driver queue/limiter）；提交 8945a68528 / 4380a14bc0 / 06a1dcaf81；13 份实验报告 + runs/ 全部目录 + journal-1.md。
- 状态判定规则：✅=有完整对应落地（L1/L2/L3 证据）；🟡=核心链路存在但至少一处细节缺失或与文档不符；❌=无对应落地（须写明检索范围）；⚪=纯调研/元内容。
- 本报告只核对不实现；发现的缺口未补，分级记录供用户决策（PRD Out of Scope）。
