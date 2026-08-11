# DD01 State of Art 调研以及总体方案 proposal

---

## 1. 项目概述

### 1.1 背景与动机

HTAP 系统在同一数据库内同时处理事务与分析两类负载。两类负载的资源需求差异明显：OLTP 事务短小、以延迟为约束；OLAP 查询以多表连接与聚合为主、以吞吐为指标，其 hash 表等中间结构占用大量内存。自 HyPer 提出混合事务与分析处理架构以来 [1]，TiDB、OceanBase 等系统均以行列混合存储承载两类负载 [2, 3]。

执行模型上，新一代 HTAP 引擎采用基于 Pipeline 的 Morsel Driven 执行模式。该模式下，物理计划在数据重分布算子（exchange）处切分为若干 fragment，每个 fragment 是一个可独立调度的执行单元；在节点上，fragment 通过 pipeline 引擎实例化为多个 driver 并行执行，driver 数量即该 fragment 的并行度（DOP）。DOP 由此成为引擎并行度的最小控制粒度：它决定一个 fragment 占用多少 CPU 资源与内存通道，也决定 exchange 两端的分区路由。GaussDB 新版本采用这一执行模式；StarRocks 3.5 的 pipeline 执行引擎与之同源，这是本项目选择 StarRocks 作为验证系统的直接原因。

StarRocks 原生按 tablet 数量与计划形状给整条查询一个统一 DOP（全局 `pipeline_dop`）。但同一查询内不同 fragment 对 DOP 的偏好差异很大：本项目在分析型基准查询 Q22（SF10 规模，三节点集群）上的实测显示，scan fragment 的最佳 DOP 为 8（需要打满存储带宽），hash join fragment 的最佳 DOP 为 4（DOP=8 时因同步开销与 cache 压力退化 18%），root gather fragment 的最佳 DOP 为 1（DOP 越高 merge 协调开销越大）。单一全局 DOP 无法同时满足 scan、join、agg、gather 的不同需求——这是 fragment 级 DOP 配置的直接动机。

高并发下问题进一步放大。多个查询的 fragment 在同一节点上并发执行，driver 总数超出核心数后，DOP 叠加引发 cache thrashing、上下文切换与内存分配器竞争，资源竞争呈现非线性：DOP 翻倍不会让吞吐翻倍，反而带来超线性退化。实测中，SF100 规模分析数据的 51 并发批次实验里固定全局 DOP=4 的批次完成时间为 3480.7ms，DOP=8 反而恶化到 3702.8ms；并发 40 场景下每个 worker 的 runnable driver 达 96 个（8 核机器的 12 倍），exchange wait 占比 28.6%。内存超配还会触发中间数据 spill 到磁盘，部分查询性能断崖式下跌。

传统代价模型假设各算子代价可加，无法表达跨 fragment 的物理资源竞争，既解释不了上述退化，也无法在编译期给出与负载匹配的 DOP。系统缺少"按查询实际资源需求做细粒度分配"的机制：一方面缺少对"给定计划与 DOP 配置下性能如何"的预估，另一方面缺少"每个 fragment 该取什么 DOP"的决策。这正是 1.2 要定义的问题。

### 1.2 问题定义

**核心问题**：高并发混合负载下，多查询在共享节点上竞争 CPU、内存、I/O 与网络资源，系统缺少"按查询实际资源需求做细粒度分配"的机制，导致吞吐随并发度增加而断崖式下跌。

问题边界界定如下。部署形态上，覆盖单机与分布式 MPP 两种形态，以 StarRocks 为验证系统；资源类型上，关注 CPU（核心与 cache）、内存、磁盘 I/O 与网络四类；负载类型上，覆盖决策支持类分析查询与事务型负载两类。生命周期上，问题发生在优化器阶段（编译、DOP 决策）与执行阶段（调度、内存、spill）两个环节。明确不在范围内的事项包括：存储引擎读写路径、副本与导入调度、查询改写正确性——这些环节的资源行为由 StarRocks 既有机制负责，本方案不越界干预。

### 1.3 研究目标

本项目研究目标共三条，每条带量化表述。

**目标 1：轻量级、自适应资源预估机制。** 精确预测查询对 CPU、内存、网络及 I/O 子系统的资源消耗。量化约束：对 OLTP 负载影响几乎为零，对 OLAP 负载额外开销不超过 5%。本方案将其实现为竞争感知的排序代价模型：给定查询计划特征（算子语义、基数估计、worker 放置）与 fragment 级 DOP 配置，模型输出该配置的相对性能分数，分数越高表示配置越快。模型不预测绝对运行时间，只做配置间的相对排序——这是因为分布式执行中绝对延迟抖动大，而配置间的相对顺序稳定，排序监督信号抗噪能力更强。

**目标 2：基于 Pipeline 与 Morsel 驱动的智能优化引擎。** 以预估分数为评估依据，决策每个 fragment 的最优 DOP 配置：离线用结构感知 MCTS 在指数级配置空间中搜索高质量 DOP 标签，再蒸馏为单次前向推理的在线策略网络，查询到达时直接输出完整 DOP 配置，并结合运行时负载对 DOP 配置做自适应修正。量化约束：在线决策路径开销控制在优化器阶段预算内，并发度调节不引入震荡——以死区、指数加权平滑与"实例化前一次性修正"三重机制保证。

**目标 3：系统实现与验证。** 在 StarRocks 中实现验证，模块化解耦；设计混合负载基准测试方案（分析查询吞吐测试与事务型负载基准）。模块化解耦的落点是"引擎无关优化核心 + StarRocks 适配层"两层结构，学习与搜索逻辑不依赖 StarRocks 类名，迁移至 GaussDB 时只需重写适配层。

---

## 2. 现状调研

### 2.1 查询资源与运行时间预估

#### 2.1.1 基于成本模型与基数估计的传统方法

传统方法以成本模型为核心。Selinger 等人提出的访问路径选择框架建立了"基数估计 × 算子代价累加"的基本范式 [4]，Volcano 与 Cascades 随后将其推广为可扩展的搜索框架 [5, 6, 7]。这套体系至今仍是主流优化器的骨架，适用场景是计划形状的比较——在同一查询的候选计划间排序，代价公式的相对次序在多数场景下足够可靠。

体系的问题由两项系统性实验研究暴露：Leis 等人分别在优化器行为测评与代价模型测评中指出，基数估计误差随 join 深度指数放大，代价公式与真实硬件行为脱节，优化器选出的计划与实测最优计划差距可达数个数量级 [8, 9]。基数估计本身是误差的主要来源：实验调查显示各方法误差分布差异巨大 [10]，学习型基数估计在单表场景优势明显、在 join 场景仍不稳定 [11, 12]。

对本项目的启示：传统成本模型的"代价可加"假设无法表达跨 fragment 的物理资源竞争，这正是 1.1 中实测退化现象的建模盲区；但成本模型产出的基数估计、算子参数仍是不可替代的特征来源，本项目将其作为模型输入而非决策依据。

#### 2.1.2 基于机器学习的查询级预估

机器学习的介入始于查询级性能预测，代表工作有三篇：Ganapathi 等人用核回归为查询的多个执行指标建模 [13]；Wu 等人以实验证明优化器代价模型在预测执行时间上确实不可用，并给出学习型替代方案 [15]；Bao 采用"优化器外壳"思路，用学习模型在候选计划集合上打分，把风险限制在候选集内 [20]。此外，Akdere 等人系统对比了多种学习模型并支持执行中更新 [14]，Li 等人用统计技术做资源消耗的鲁棒估计 [16]，Sun 与 Li 提出端到端学习代价估计器，用计划树编码直接回归算子代价 [17]。

学习型优化器把预测嵌入决策：Neo 与 QueryBot 5000 分别用强化学习与监督学习驱动 join order 搜索 [18, 19]，Krishnan 等人的 DQ 系统以强化学习处理同类 join 优化问题 [22]，SkinnerDB 在执行期用强化学习动态选择 join 算子 [21]。这批工作的共同范式是"查询特征 → 资源/时间"的回归建模，粒度为查询级。

对本项目的启示有三点：其一，学习替代代价模型已被反复验证可行；其二，Bao 的"在候选集内打分、不预测绝对值"思路与本项目"配置间排序"的设计一致，排序监督比回归监督更抗噪；其三，这批工作的在线推理成本普遍偏高，控制推理开销是学习型方法能否进入编译路径的共同约束，也是本项目选择"离线搜索 + 蒸馏"的直接原因。

#### 2.1.3 算子级与细粒度预估

粒度向算子级细化的动机是查询级预估无法定位性能瓶颈。算子级方法以单个算子为预测单元，需要执行引擎提供算子级 profile 数据，精度高于查询级，但特征获取与建模成本随粒度细化而上升。这一方向的主要局限：多数工作假设单查询独立执行，未建模并发查询在共享节点上的资源干扰；对 MPP 引擎特有的问题——同一查询内不同 fragment 的并行度敏感性差异——缺乏建模。

对本项目的启示：细粒度的价值在于与决策粒度对齐。本项目的预估对象不是算子耗时，而是"计划 + fragment 级 DOP 配置"的相对性能分数，预估粒度与决策粒度一致，避免"预估了 A、决策用 B"的错位。

#### 2.1.4 在线修正与执行中预估

静态预估与真实执行的偏差需要反馈修正，代表工作是 LEO：在 DB2 中基于执行反馈修正基数估计，让优化器的后续决策受益 [23]。Deshpande 等人对自适应查询处理做了系统综述，覆盖执行中重优化、反馈回路与自适应算子 [24]。在线修正的价值在于用真实执行校准静态预估，代价是反馈回路延迟：修正发生在执行开始之后，对已实例化的执行结构修改成本高。

对本项目的启示：执行反馈应保留为持续修正通道而非主路径。本项目采用"执行 trace 回流重训"的慢回路实现反馈；不采用执行中的在线修正，因为 MPP 引擎中实例化后修改 DOP 会破坏 exchange 路由一致性。

### 2.2 并行度设置与运行时自适应

现有并行度控制以查询级静态设置为主，运行时自适应机制粒度粗，均不感知 fragment 间的资源竞争；学习型调参工作把 DOP 当作全局旋钮，与 per-fragment 决策存在粒度差距。

#### 2.2.1 查询级并行度设置

主流数据库以查询级 DOP 为并行度控制的基本形式：SQL Server 通过 MAXDOP 限制单语句并行度，PostgreSQL 的并行查询按表大小与 worker 数量选择并行度，StarRocks 以全局 `pipeline_dop` 控制整条查询的 driver 数量。这些设置的共同特点是静态：在编译期确定，不随节点负载变化。学习型调参系统把 DOP 纳入调优旋钮：OtterTune 对数据库系统配置做大规模机器学习推荐 [25]，QTune 以查询负载为输入做查询感知的调优 [26]——但调优对象是全局配置，粒度与单查询内部的 fragment 需求不匹配。

#### 2.2.2 运行时自适应并行度

部分系统提供运行时自适应机制。StarRocks 的 runtime adaptive DOP 在执行中根据实际数据量调整并行度，但调整限制为 2 的幂且以查询为粒度。这类机制的价值在于用执行反馈修正静态决策，局限在于：调整发生在执行开始之后，受执行结构固化约束，调整幅度有限；且以查询为粒度，无法针对单个 fragment 的竞争状态做调整。

#### 2.2.3 对本项目的启示

并行度控制的粒度应与执行结构对齐。MPP 引擎中 fragment 是基本调度单元，per-fragment DOP 决策既保留了细粒度，又能在实例化前安全实施；运行时自适应可作为决策之后的修正通道保留（本项目以负载修正形式实现）。现有工作的静态全局设置与粗粒度自适应正好留下"fragment 级、竞争感知、实例化前决策"的空白。

### 2.3 细粒度执行与调度

细粒度执行的学术脉络始于 morsel-driven 并行。Leis 等人系统阐述了 morsel 驱动执行：把关系切分为固定大小的 morsel，由工作线程池动态领取，天然支持 NUMA 感知与负载均衡 [27]（其源头是 HyPer 的混合 OLTP/OLAP 架构 [1]）。编译执行方向，Neumann 证明了对查询做底层代码生成可大幅降低每行处理开销 [28]，Umbra 把编译执行与 morsel 调度扩展到磁盘场景 [29]。并行 join 的行为研究给出了多核并行下的扩展性边界：并行度超过核心数后，同步与 cache 开销使扩展性退化 [31, 32]。列存基础方面，C-Store 确立了列式存储对分析负载的价值 [30]。

工程化层面，Impala 以 fragment 为调度单位配合查询级准入，验证了"fragment 切分 + 准入控制"在 MPP 中的可行性 [33]；分布式 HTAP 系统 TiDB、OceanBase 在此基础上叠加分布式事务与行列混合存储 [2, 3]。StarRocks 的 pipeline 引擎与 Impala 同源：物理计划在 exchange 处切分为 fragment，每个 fragment 实例化多个 driver，driver 由全局队列调度；原生机制提供查询级统一 `pipeline_dop` 与运行时自适应 DOP，但自适应只做 2 的幂调整，不感知 fragment 间的资源竞争。

对本项目的启示有三点。其一，fragment/driver 是 MPP 并行度的自然单位，per-fragment DOP 是细粒度调度的正确切入点。其二，实例化前是修改 DOP 的安全窗口：StarRocks 的调度在 worker 放置确定之后、driver 实例化之前有一个间隙，此时 exchange 路由尚未固化，修改 DOP 不会破坏数据投递——本项目将决策精确卡在这个窗口。其三，driver 级并行度控制可以复用现有执行引擎机制（全局 driver 队列与限额），不需要另起一套执行引擎。

### 2.4 调研结论与研究空白

调研结论可以概括为现有工作的三点空白，逐条对应本项目的三个研究目标。

**空白一：预估粒度粗。** 查询级预估占据主流 [13-17]，系统无法区分同一查询内不同 fragment 的资源需求差异。本项目实测表明这种差异是结构性的：Q22 中 scan、hash join、root gather 三个 fragment 的最佳 DOP 分别为 8、4、1，任何单一全局 DOP 都无法同时满足。对应目标 1：需要 fragment 级的资源预估机制。

**空白二：竞争建模缺失。** 传统代价模型假设代价可加 [4-9]，无法表达共享节点上的非线性资源竞争；学习型优化器证明了学习替代代价模型的可行性，但多数聚焦 join order [18-21]，未处理"多个 fragment 共享同一节点时的资源干扰"。对应目标 2：需要以竞争感知的预估分数为依据的最优 DOP 决策机制。

**空白三：并行度决策与执行机制脱节。** 现有并行度研究以全局旋钮与独立评估为主 [25, 26]，工程系统的自适应机制粒度粗、调整幅度受限；学习型方法未在真实 MPP 引擎的调度语义（exchange 路由、实例化窗口、约束校验）下验证过 per-fragment 决策的可行性。对应目标 3：需要在真实引擎中实现 fragment 级 DOP 决策与安装，并以混合负载实验验证效果。

---

## 3. 总体方案

### 3.1 设计原则

本方案的设计原则共四条，每条都对应项目背景中的一个具体问题，并带量化约束。

**轻量**。在线编译预算以毫秒计，离线搜索（MCTS）秒级耗时不可能进入编译路径。约束：在线决策路径开销控制在优化器阶段预算内，决策服务超时上限 50ms。这决定了"离线搜索 + 在线蒸馏推理"的架构形态。

**细粒度**。Q22 实测显示同一查询内 fragment 的最佳 DOP 从 1 到 8 不等，查询级统一 DOP 必然同时过配与欠配。约束：决策以 fragment 为单位，候选 DOP 为 2 的幂集合且不超过上限。

**自适应**。节点负载随时间漂移，静态配置无法跟踪。约束：负载采样周期 2s，指数加权平滑系数 0.25，死区阈值 CPU 0.4、内存 0.65，修正只在实例化前一次性施加，不引入震荡。

**对 OLTP 近零影响**。事务型负载以延迟为约束，不能被决策路径干扰。约束：仅 SELECT 且启用 pipeline 的查询进入决策路径，其余查询完全绕行。

### 3.2 系统总体架构

**决策层**位于 FE 侧决策模块包与独立部署的 Python 决策服务。FE 侧组件负责决策链路的编排：决策控制器判断开关与查询类型，特征提取器从计划与放置信息提取特征并打包请求，决策校验器校验决策服务返回值，决策安装器把合法 DOP 写入计划，执行轨迹管理模块记录决策与执行结果。Python 决策服务加载离线训练的策略模型，接收请求后单次前向推理输出完整 DOP 配置。决策层与调度层的接口是决策结果对象，包含原始 DOP、决策 DOP、安装 DOP 与决策耗时。

**调度层**位于 FE 的调度预处理模块，核心是两阶段调度。第一阶段的放置分配只确定每个 fragment 的目标 worker 与扫描范围归属，不创建执行实例；第二阶段的实例化依据最终 DOP 创建 FragmentInstance、分配每个 driver 的扫描范围、构建 exchange 路由表。两阶段之间是决策窗口：放置信息已知、路由尚未固化，此时修改 DOP 不会破坏数据投递。调度层与执行层的接口是下发参数，由实例参数生成器把 pipeline_dop 发给 BE。

**执行层**位于 BE，复用 StarRocks 既有机制：driver 由全局驱动执行器与查询共享驱动队列调度，驱动限额器提供 driver 总数硬限额，执行统计经性能画像汇总后由 FE 的执行轨迹管理模块落盘。

三层之间的数据流共三条。查询决策流：调度层第一阶段完成后进入决策层，决策层返回 DOP 配置，调度层第二阶段按配置实例化并下发执行层。执行反馈流：执行层统计经执行轨迹落盘，成为离线训练语料。离线训练流：语料经数据集构建、排序模型训练、MCTS 搜索、策略蒸馏四个环节，产出更新后的策略模型，回填决策层。

### 3.3 模块设计要点

**预估模块**（对应目标 1）。预测对象是"计划 + fragment 级 DOP 配置"的相对性能分数，不预测绝对运行时间。粒度为 fragment 级。该模块在离线流程中工作：以历史执行轨迹构建的训练样本（计划特征 + DOP 配置 + 实测延迟）为输入，训练竞争感知的排序代价模型，输出配置的相对性能分数。该分数不作为在线决策的直接输入，而是作为离线 MCTS 搜索的代理评估器，不进入在线编译路径。依赖：执行轨迹数据集构建、计划特征编码（依赖边 + 竞争边的图表示）。

**优化引擎模块**（对应目标 2）。职责分两步。离线：以预估模块的分数为评估依据，用 MCTS 在指数级配置空间中搜索高质量 DOP 配置，蒸馏为单次前向推理的策略网络。在线：查询编译过程中，在第一阶段放置确定之后、第二阶段实例化之前的决策窗口内，策略网络接收计划特征（含 worker 放置）直接输出完整 DOP 配置，并叠加负载修正——按目标 worker 的实时负载对 DOP 做有界缩放。输入为计划特征与节点实时负载；输出为安装后的 DOP 配置。依赖：预估模块（提供评估分数）、两阶段调度的决策窗口。

### 3.4 端到端流程

查询的完整链路分离线与在线两部分，离线部分生产决策模型，在线部分消费模型完成查询编译与执行。

**离线流程**。被处理查询的执行轨迹（请求、安装结果、实际 DOP、延迟）由执行轨迹管理模块落盘，经清洗与配对后构建排序样本——同一计划下不同 DOP 配置的延迟对比对。预估模块基于这些样本训练竞争感知的排序代价模型，输出配置的相对性能分数；优化引擎以该分数为代理评估器，用 MCTS 为每个计划搜索高质量 DOP 配置，再蒸馏为单次前向推理的策略网络，回填决策服务。离线流程各环节的实现在后续文档展开。

**在线流程**。查询经 SQL 解析与物理计划生成后（决策层尚不介入），调度层执行第一阶段放置：为每个 fragment 确定目标 worker 与扫描范围归属，写入放置结果，不创建实例。随后进入决策窗口：决策控制器判断查询类型与开关，特征提取器提取特征并发起决策请求（超时 50ms），策略网络单次前向输出 DOP，负载修正按当前 worker 负载有界缩放，决策安装器写入计划片段的管道并行度。第二阶段实例化按最终 DOP 创建片段实例、分配每个 driver 的扫描范围、构建 exchange 路由表，经下发参数送 BE，由全局驱动执行器调度执行。查询完成后执行轨迹管理模块记录决策、实际 DOP、延迟与执行统计，作为离线流程的输入，完成数据回流。

### 3.5 技术选型

选 StarRocks 作为验证系统，理由有四。其一，执行理念同源：pipeline 执行引擎、morsel 驱动、fragment 切分与目标系统（GaussDB 新版本）一致，方案迁移不需要改变模型与决策逻辑。其二，开源可修改：两阶段调度的改造已在本项目代码库中完成（调度预处理模块的放置与实例化拆分），验证成本可控。其三，具备 HTAP 能力：能承载混合负载基准测试，符合目标 3 的验证需求。其四，fragment/driver 调度语义清晰：决策窗口（放置确定后、实例化前）真实存在，per-fragment DOP 可安全安装。

备选系统对比与取舍如下。Doris 与 StarRocks 同源（同一早期代码分叉），但 pipeline 调度接口不同，改造点不通用，且本项目团队对 StarRocks 代码更熟。ClickHouse 的并行模型是线程级自适应，缺少 fragment/instance 级的调度单元与实例化窗口，与目标系统的执行理念差异大。PostgreSQL 系是进程模型，细粒度并行（Parallel Query）的粒度粗且无 morsel 概念，迁移成本高。取舍结论：以 StarRocks 为验证系统，换取方案对目标系统的可迁移性；代价是实验环境与目标系统的具体实现细节存在差异，后期迁移时只需重写适配层（特征提取与 DOP 安装），模型与搜索逻辑保持不变。

---

## 4. 术语表

**Pipeline**（pipeline）：以算子流水线为执行单元的引擎执行模型，取代火山模型逐行拉取的执行方式。

**Morsel**（morsel）：关系数据切分出的固定大小数据块，并行执行时由工作线程动态领取的处理单位。

**Fragment**（fragment）：物理计划在数据重分布算子处切分出的调度单元，是 MPP 引擎的基本调度单位。

**Driver**（driver）：fragment 在 BE 上实例化的执行线程单元，driver 数量即该 fragment 的并行度。

**DOP**（degree of parallelism）：并行度，fragment 实例化的 driver 数量；本项目候选值为 2 的幂且不超过上限。

**FE**（frontend）：StarRocks 前端节点，负责查询解析、优化与调度，本方案的决策层与调度层所在位置。

**BE**（backend）：StarRocks 后端节点，负责数据存储与查询执行，本方案的执行层所在位置。

**Exchange**（exchange）：数据重分布算子，负责跨节点或跨 fragment 的数据传递，两端的分区路由由 DOP 决定。

**HTAP**（hybrid transactional/analytical processing）：混合事务与分析处理，在同一系统内同时承载两类负载。

**OLTP / OLAP**（online transaction/analytical processing）：事务型负载与分析型负载。

**Query profile**（query profile）：查询执行后的性能画像，包含各算子耗时、内存与 I/O 统计。

**Spill**（spill）：内存不足时中间数据溢出到磁盘的机制。

**Scale factor**（scale factor）：分析基准数据集规模的倍数标记，如 SF100 表示 100 倍规模。

**Stream**（stream）：吞吐测试中的并发查询流。

**MCTS**（Monte Carlo tree search）：蒙特卡洛树搜索，本项目用于离线搜索高质量 DOP 配置。

**EWMA**（exponentially weighted moving average）：指数加权移动平均，本项目用于平滑节点负载采样。

**Trace**（trace）：查询执行轨迹，记录决策请求、安装结果、实际 DOP 与延迟，回流离线训练。

---

## 5. 参考文献

引用格式统一为"作者，标题，会议/期刊，年份"，按正文引用顺序编号。

[1] A. Kemper, T. Neumann. HyPer: A Hybrid OLTP&OLAP Main Memory Database System Based on Virtual Memory Snapshots. ICDE, 2011.

[2] D. Huang, Q. Liu, Q. Cui, et al. TiDB: A Raft-based HTAP Database. PVLDB, 2020.

[3] G. Huang, X. Zong, S. Cai, et al. OceanBase: A 707 Million tpmC Distributed Relational Database System. PVLDB, 2022.

[4] P. G. Selinger, M. M. Astrahan, D. D. Chamberlin, R. A. Lorie, T. G. Price. Access Path Selection in a Relational Database Management System. SIGMOD, 1979.

[5] G. Graefe, W. J. McKenna. The Volcano Optimizer Generator: Extensibility and Efficient Search. ICDE, 1993.

[6] G. Graefe. The Cascades Framework for Query Optimization. IEEE Data Engineering Bulletin, 1995.

[7] G. M. Lohman. Grammar-like Functional Rules for Representing Query Optimization Alternatives. SIGMOD, 1988.

[8] V. Leis, A. Gubichev, A. Mirchev, P. Boncz, A. Kemper, T. Neumann. How Good Are Query Optimizers, Really? PVLDB, 2015.

[9] V. Leis, B. Radke, A. Gubichev, A. Kemper, T. Neumann. Query Optimization Through the Looking Glass, and What We Found There. SIGMOD, 2015.

[10] H. Harmouch, F. Naumann. Cardinality Estimation: An Experimental Survey. PVLDB, 2017.

[11] Z. Yang, A. Kamsetty, S. Luan, E. Liang, Y. Duan, X. Chen, I. Stoica. Learned Cardinality Estimation: An In-depth Study. SIGMOD, 2022.

[12] B. Hilprecht, A. Schmidt, M. Kulessa, A. Molina, K. Kersting, C. Binnig. DeepDB: Learn from Data, not from Queries! PVLDB, 2020.

[13] A. Ganapathi, H. Kuno, U. Dayal, J. L. Wiener, A. Fox, M. Jordan, D. Patterson. Predicting Multiple Metrics for Queries: Better Decisions Enabled by Machine Learning. ICDE, 2009.

[14] M. Akdere, U. Çetintemel, M. Riondato, E. Upfal, S. B. Zdonik. Learning-Based Query Performance Modeling and Prediction. ICDE, 2012.

[15] W. Wu, Y. Chi, S. Zhu, J. Tatemura, H. Hacıgümüş, J. F. Naughton. Predicting Query Execution Time: Are Optimizer Cost Models Really Unusable? ICDE, 2013.

[16] J. Li, A. C. König, V. Narasayya, S. Chaudhuri. Robust Estimation of Resource Consumption for SQL Queries Using Statistical Techniques. PVLDB, 2012.

[17] J. Sun, G. Li. An End-to-End Learning-based Cost Estimator. PVLDB, 2019.

[18] R. Marcus, O. Papaemmanouil. Towards a Hands-Free Query Optimizer through Deep Learning. CIDR, 2018.

[19] R. Marcus, P. Negi, H. Mao, C. Zhang, M. Alizadeh, T. Kraska, O. Papaemmanouil, O. Kennedy. Neo: A Learned Query Optimizer. PVLDB, 2019.

[20] R. Marcus, P. Negi, H. Mao, N. Tatbul, M. Alizadeh, T. Kraska. Bao: Making Learned Query Optimization Practical. SIGMOD, 2021.

[21] I. Trummer, J. Wang, D. Maram, S. Moseley, S. Jo, J. Antonakakis. SkinnerDB: Regret-Bounded Query Evaluation via Reinforcement Learning. SIGMOD, 2019.

[22] S. Krishnan, Z. Yang, K. Goldberg, J. Hellerstein, I. Stoica. Learning to Optimize Join Queries With Deep Reinforcement Learning. arXiv:1808.03196, 2018.

[23] M. Stillger, G. M. Lohman, V. Markl, M. Kandil. LEO — DB2's LEarning Optimizer. VLDB, 2001.

[24] A. Deshpande, Z. Ives, V. Raman. Adaptive Query Processing. Foundations and Trends in Databases, 2007.

[25] D. Van Aken, A. Pavlo, G. J. Gordon, B. Zhang. Automatic Database Management System Tuning Through Large-scale Machine Learning. SIGMOD, 2017.

[26] G. Li, X. Zhou, S. Li, B. Gao. QTune: A Query-Aware Database Tuning System with Deep Reinforcement Learning. PVLDB, 2019.

[27] V. Leis, P. Boncz, A. Kemper, T. Neumann. Morsel-Driven Parallelism: A NUMA-Aware Query Evaluation Framework for the Many-Core Age. SIGMOD, 2014.

[28] T. Neumann. Efficiently Compiling Efficient Query Plans for Modern Hardware. PVLDB, 2011.

[29] T. Neumann, M. Freitag. Umbra: A Disk-Based System with In-Memory Performance. CIDR, 2020.

[30] M. Stonebraker, D. J. Abadi, A. Batkin, et al. C-Store: A Column-Oriented DBMS. VLDB, 2005.

[31] S. Blanas, Y. Li, J. M. Patel. Design and Evaluation of Main Memory Hash Join Algorithms for Multi-Core CPUs. SIGMOD, 2011.

[32] C. Balkesen, J. Teubner, G. Alonso, M. T. Özsu. Main-Memory Hash Joins on Multi-Core CPUs: Tuning to the Underlying Hardware. PVLDB, 2013.

[33] M. Kornacker, A. Behm, V. Bittorf, et al. Impala: A Modern, Open-Source SQL Engine for Hadoop. CIDR, 2015.
