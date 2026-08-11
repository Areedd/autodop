# AutoDOP 核心方法总结

> 论文：*AutoDOP: Competition-Aware Fine-Grained Parallelism Optimization for MPP Databases*
> 目标系统：StarRocks（生产级 MPP OLAP 引擎）
> 核心目标：在 MPP 引擎中为每个查询片段（fragment）优化其并行度（DOP），在感知资源竞争的同时，把离线搜索得到的高质量 DOP 配置压缩为可在毫秒级在线推理的轻量策略，并在运行时根据集群负载做有界修正。

---

## 1. 对 StarRocks 的洞察

AutoDOP 的设计建立在对 StarRocks（以及同类 MPP 引擎）执行机制与现有调度策略缺陷的几个关键观察之上。

### 1.1 Fragment 是 MPP 执行的基本调度单元

StarRocks 把编译后的物理计划在数据重分布算子（Exchange / Shuffle / DataStream 等）处切分为若干 **fragment（片段）**。每个 fragment 是一个自包含的子计划，可作为一个单元调度到 worker（BE 节点）上执行。在每个 worker 上，fragment 通过 pipeline 执行引擎实例化多个并行 **driver（驱动线程）**，driver 数量即为该 fragment 的 **DOP（degree of parallelism，并行度）**。

例如：一个三表 join 查询可能被分解为 5 个 fragment——两个 scan fragment、一个 hash-join fragment、一个 aggregation fragment、一个根 gather fragment。每个 fragment 都有独立的 DOP。

### 1.2 Query-level DOP 粒度过粗，per-fragment DOP 异质性强

StarRocks 原生策略主要基于 tablet 数量和计划形状给出一个**查询级**的统一 DOP。但论文实测（TPC-DS Q22，SF10，三节点集群）发现：同一查询内不同 fragment 对 DOP 的偏好差异巨大。

| Fragment | 最佳 DOP | 偏离最佳时的表现 |
|---|---|---|
| F1 Scan | 8 | 需要打满存储带宽 |
| F3 Hash join | 4 | DOP=8 时退化 18%（同步开销、cache 压力） |
| F5 Root gather | 1 | DOP 越高越慢（协调与 merge 主导） |

**单一全局 DOP 无法同时满足 scan、join、agg、gather 的不同需求**——这是 per-fragment DOP 优化的根本动机。

### 1.3 资源竞争是非线性耦合的，传统代价模型无法刻画

关键洞察：fragment 之间不是独立的。多个 fragment 落在同一 worker 上并发执行时，会争抢 CPU 核心、L3 cache、内存带宽、网络 buffer。这种竞争是**非线性**的——DOP 翻倍不会让吞吐减半，因为 cache thrashing、NUMA 跨 socket 流量、内存分配器竞争会带来**超线性退化**。

例如：scan fragment 在 DOP=16 且兄弟 hash-join fragment 为 DOP=2 时运行良好；但当 join 也升到 DOP=16 时，同一 worker 上的总 driver 数超出核心数，触发大量上下文切换，性能急剧下降。

经典优化器（Selinger / Volcano / Cascades）假设算子代价可加或仅弱耦合，**无法表达这种跨 fragment 的物理资源竞争**。这是 AutoDOP 必须引入"竞争边"的根本原因。

### 1.4 DOP 不是自由参数——存在执行可行性约束

StarRocks 的执行约束 $\Omega(P)$ 限制了 DOP 的取值：

- **Exchange 一致性**：上游 sender DOP=$d_s$ 经 hash-shuffle 发往下游 receiver DOP=$d_r$，路由表必须与 $d_r$ 一致；事后改 $d_r$ 会破坏数据投递。
- **Scan 分区归属**：scan fragment 的 DOP 决定数据分区如何切分到 driver。
- **Colocate join**：共享分区亲和性的 fragment 必须有匹配的 DOP。
- **Placement 规则**：worker 分配需满足数据本地性与 exchange 拓扑。

任何 DOP 赋值都必须满足 $\Omega(P)$，否则会导致数据错路由或结果错误。这决定了 AutoDOP 必须在**任务实例创建前**完成 DOP 决策，并进行约束校验。

### 1.5 存在一个可介入的"窗口期"

StarRocks 的两趟编译流程为 AutoDOP 提供了天然的接入点：

- **Pass 1（worker 选择）**：确定每个 fragment 的目标 worker（scan-range 分配 + placement 规则），但**不创建任务实例**。
- **AutoDOP 介入窗口**：此时 worker 放置已知、exchange 路由表与 driver 布局尚未实例化，修改 DOP 仍安全。
- **Pass 2（实例创建）**：依据最终 DOP 创建 task instance、分配 scan range、构建路由表。

这个窗口期是 AutoDOP 在线集成可行性的关键——一旦实例创建，改 DOP 就会破坏派生结构。

### 1.6 在线编译预算极紧

复杂分析查询的编译预算以毫秒计。任何在线 DOP 决策必须在此预算内完成。这排除了在线运行 MCTS（秒级）的可能，迫使 AutoDOP 采用"离线搜索 + 蒸馏 + 在线轻量推理"的架构。

---

## 2. 方法切入点

AutoDOP 的切入点可以从问题、建模、搜索、部署四个维度概括。

### 2.1 问题切入点：把 DOP 优化重构为"受约束的、计划相关的向量选择"

AutoDOP 不把 DOP 当成全局旋钮（OtterTune/QTune/CDBTune/Resune 的 flat 向量），而是形式化为：

$$C^* = \arg\min_{C \in K_1 \times \cdots \times K_N,\; C \models \Omega(P)} T(P,C,L)$$

其中：
- $P=(F,E,\Pi)$ 是执行 DAG（fragment 集、数据流边、worker 放置映射）
- $C=(d_1,\ldots,d_N)$，$d_i \in K_i$（每个 fragment 有自己的候选 DOP 集）
- $\Omega(P)$ 是引擎施加的可行性约束
- $T(P,C,L)$ 是端到端延迟，依赖于集群负载 $L$

该公式有三个关键性质：
1. **$T$ 不可分解**：由于共享 worker 上的 inter-fragment 竞争，$T$ 无法写成 per-fragment 代价之和。
2. **搜索空间指数增长**：$|K_1|\times\cdots\times|K_N|$，15 fragment × 5 候选 ≈ $3\times10^{10}$。
3. **$T$ 只能通过执行或训练好的代理观测**：解析公式无法捕捉非线性竞争。

### 2.2 建模切入点：用"竞争图 + 排序学习"替代"回归代价"

- **图编码**：在数据流依赖边之外显式加入**加权竞争边**，刻画物理资源干扰——这是经典 plan encoder 缺失的信号。
- **排序而非回归**：分布式执行中绝对延迟抖动 10–20%，但配置间的相对顺序稳定。下游 MCTS 只需比较配置优劣，不需要毫秒级预测。因此用 **pairwise ranking loss** 训练，匹配下游使用方式。

### 2.3 搜索切入点：用"结构感知 MCTS"在受限空间里高效寻优

- 把 DOP 赋值建模为**序列决策过程**，逐 fragment 固定 DOP。
- 用**注意力引导的变量排序**：把对性能敏感的 fragment 放在树浅层，让 MCTS 在重要决策上多花模拟预算。
- 用**等价类剪枝**：把结构相同的 fragment（对称 join 分支、复制维度 scan）合并为共享决策变量，把有效深度从 $N$ 降到 $N' \ll N$。

### 2.4 部署切入点：用"蒸馏 + 负载修正"把离线搜索落地为在线毫秒级决策

- 离线 MCTS 产标签，**知识蒸馏**成单次前向推理的策略网络（<5ms/50 fragment）。
- 在线用 **EWMA 平滑的实时 CPU/内存负载**对策略输出做有界缩放。
- 严格在 Pass 1 与 Pass 2 之间介入，并做约束校验，校验失败回退到原生策略——**绝不产生非法执行计划**。

---

## 3. 整体方案的具体逻辑

AutoDOP 采用**离线训练—离线搜索—在线蒸馏推理—在线负载修正**的四阶段闭环，核心是把"不可分解、不可枚举、依赖实时负载"的 DOP 向量优化问题，拆解为可学习、可搜索、可部署的子任务。

### 3.1 架构分层：引擎无关核心 + 引擎相关适配层

- **引擎无关优化核心**：处理 fragment DAG 提取、竞争边增强、Graph Transformer 排序、MCTS 搜索、策略蒸馏、负载修正。学习与搜索逻辑不依赖 StarRocks 类名。
- **StarRocks 适配层**：提供 plan extractor、placement reader、candidate-DOP generator、constraint validator、DOP installer。读取 FE `PlanFragment` DAG、BE placement、scan-range 分配、bucket/colocate 元数据；生成合法 `pipeline_dop` 候选；校验 sender–receiver 与 scan-layout 不变量；在 `FragmentInstance` 创建前写入最终 DOP。

### 3.2 闭环数据流

1. **离线轨迹采集**：在历史查询上扰动 per-fragment DOP，记录 (plan, DOP 配置, 延迟) 三元组。
2. **图表示构建**：每个 plan 编码为带依赖边与竞争边的有向图。
3. **排序模型训练**：Graph Transformer 学会为同一 plan 的两个配置打分，使更快的配置得分更高。
4. **MCTS 离线搜索**：以排序模型为代理评估器，为每个训练 plan 找到高质量 DOP 标签 $C^*$。
5. **策略蒸馏**：把 MCTS 标签蒸馏成单次前向推理的策略网络。
6. **在线推理 + 负载修正**：查询到达时，策略网络预测 DOP，负载缩放器按当前 worker 状态有界调整。
7. **约束校验与安装**：校验 $\Omega(P)$，通过则写入 plan，否则回退原生策略。
8. **闭环回流**：已完成查询的 profile 加入离线语料，随工作负载演化周期性重训。

---

## 4. 方法各步骤的具体工作、作用与设计逻辑

AutoDOP 的五个核心组件对应论文 overview 的 (1)–(5)。下文逐一展开。

### 步骤 1：基于扰动的离线数据准备

**具体工作**
- 在 TPC-DS SF10 历史查询上对每个 fragment 的 DOP 做扰动，记录执行 trace，构建 (plan, DOP 配置, 延迟) 三元组语料（论文中为 2,400 条，80/20 训练-验证划分）。
- 同时为每个 plan 构建竞争感知图表示 $G(P,C)=(V, E_{\text{dep}}, E_{\text{comp}})$：
  - **节点 $v_i$**：对应 fragment $f_i$，特征向量编码四类信息——算子语义（Scan/Hash Join/Agg/Exchange…）、数据描述（优化器基数、数据量、结构特征）、放置描述（worker 分配、co-location）、DOP 描述（当前赋值 $d_i$）。
  - **依赖边 $E_{\text{dep}}$**：若 $f_i$ 产生中间结果被 $f_j$ 消费，则连边，刻画 producer–consumer 拓扑、关键路径、上下游耦合。
  - **加权竞争边 $E_{\text{comp}}$**：刻画无数据流关系但在同一 worker 上并发执行的 fragment 间的资源干扰。

**竞争边权重的计算逻辑**
- 先用轻量启发式（基于数据量与算子复杂度）估计执行窗口 $[S_i,E_i]$，计算时间重叠比：

$$\text{Overlap}_{ij} = \frac{\max(0,\min(E_i,E_j)-\max(S_i,S_j))}{\min(E_i-S_i, E_j-S_j)}$$

用较短时长做归一化，强调"一个 fragment 几乎完全落在另一个执行窗口内"的情形。

- 边权组合时间重叠、共享 worker 放置、资源压力：

$$w_{ij} = R_{\text{base}} + \alpha \cdot \text{Overlap}_{ij} \cdot \text{WorkerShare}_{ij} \cdot \text{ResourcePressure}_{ij}$$

其中 $\text{WorkerShare}_{ij} = \frac{|\pi_i \cap \pi_j|}{|\pi_i \cup \pi_j|}$，$\text{ResourcePressure}_{ij}$ 聚合两 fragment 的宽度归一化 CPU/内存/网络/exchange-buffer 需求；$R_{\text{base}}$ 是小基项，保留启发式可能漏掉的调度抖动/运行时倾斜带来的弱干扰信号。

**作用**
- 为模型提供"物理资源竞争"这一经典 plan 编码缺失的信号。
- 扰动语料让模型观察 fragment 在不同并行度与不同竞争条件下的响应。

**设计逻辑**
- **显式分离两类关系**：依赖边承载逻辑数据流，竞争边承载物理资源干扰——后者是 DOP 敏感性的核心，无法由前者推出。
- **基项 + 乘积组合**：既反映"时间重叠 × 空间共置 × 资源压力"的乘性耦合，又用 $R_{\text{base}}$ 兜底启发式误差。
- **用较短时长归一化**：避免长 fragment 的重叠被短 fragment 拖低，更符合"短 fragment 几乎被长 fragment 完全覆盖时竞争强"的直觉。

### 步骤 2：竞争感知的排序代价模型

**具体工作**
- 训练一个 **双边 Graph Transformer**，处理依赖边与竞争边。每层中每个 fragment 同时沿两类边聚合：依赖边传递关键路径延迟与数据量传播，竞争边暴露并发执行的资源耦合。
- 由于竞争边带权，模型对高时间重叠 + 共享 worker 的 fragment 对分配更强注意力——能在聚合时直接表达"提高一个 fragment 的 DOP 会挤占并发邻居资源"的权衡，而不必通过多层局部更新推断。
- 经若干层后，**全局 readout** 把 fragment embedding 池化为 plan 级向量，MLP 映射为标量分数 $S_\theta(P,C)$。
- 训练目标为**带动态间隔的 pairwise ranking loss**：

$$\mathcal{L}(\theta) = \sum_{(P,C^+,C^-)} \max\Big(0,\; m(P,C^+,C^-) - S_\theta(P,C^+) + S_\theta(P,C^-)\Big)$$

动态间隔与观测延迟差挂钩：

$$m(P,C^+,C^-) = \log\Big(1 + \frac{T(P,C^-)-T(P,C^+)}{T(P,C^+)}\Big)$$

**作用**
- 为 MCTS 提供代理评估器，替代真实执行。
- 学会"哪个配置更快"，而非"每个配置跑多少毫秒"。

**设计逻辑**
- **为什么用 Graph Transformer 而非 GNN**：fragment 的性能依赖局部上下文（父子 fragment）与非局部上下文（远处分支但在同一 worker 上的 fragment）。局部消息传递对后者低效，深层 GNN 会稀释信号。Transformer 让每个 fragment 能 attend 到任意其他 fragment，注意力由边类型与权重调制。
- **为什么排序而非回归**：绝对延迟在分布式执行中抖动 10–20%，但配置间的相对顺序稳定。MCTS 只需比较，不需毫秒值。回归目标会被测量噪声污染，而排序监督在噪声环境下仍可靠。
- **为什么动态间隔**：延迟差大的 pair，排序错误的搜索代价更高，应给更大间隔（更强监督）；延迟差小的 pair 标签在测量噪声下不可靠，应放松约束。同时过滤掉低于最小间隔阈值的 pair 以降噪。
- **实验佐证**：XGBoost 回归 RMSE 低但 pairwise accuracy 仅 43.2%；GT_reg 76.3%；加竞争边 78.2%；GT_pair 92.3%；完整 GT_pair_comp 达 93.0%——证明"竞争边 + 排序"两个设计各自有效且互补。

### 步骤 3：代理引导的 MCTS 配置搜索

**具体工作**
- 把 DOP 赋值建模为序列决策：每步固定一个 fragment 的 DOP，最终对完整配置用代价模型打分。
- 用 UCB1 平衡探索与利用：$\text{UCB1}(c) = \frac{Q(c)}{N(c)} + C_p\sqrt{\frac{\ln N(\text{parent})}{N(c)}}$，$C_p=\sqrt{2}$。
- **注意力引导的变量排序**：从代价模型 global readout 层提取 fragment 重要性 $I_i = \sum_{h=1}^{H} \text{Attn}_h(\text{Global}, f_i)$，按 $I_i$ 降序排列决策变量，把高影响 fragment 放在树浅层（MCTS 在浅层分配更多模拟）。
- **等价类剪枝**：基于算子类型、exchange 类型、join 类型、输入基数范围把结构相同的 fragment（对称 join 分支、复制维度 scan、并行 agg pipeline）分组，组内共享单一决策变量，有效搜索深度从 $N$ 降到 $N'$，空间从 $|K|^N$ 降到 $|K|^{N'}$。
- **代理引导的 rollout**：rollout 时未决 fragment 从候选集均匀随机取 DOP，完整配置由代价模型 $S_\theta(P,C_{\text{sim}})$ 打分，奖励沿选择路径回传。默认 500 次迭代后，取根节点最高奖励配置作为 $C^*$。

**作用**
- 在指数级空间中找到高质量 DOP 标签，供蒸馏训练。
- 用代价模型代替真实执行，使搜索在离线可行（无需每次都跑查询）。

**设计逻辑**
- **为什么用 MCTS 而非网格/随机搜索**：空间太大无法枚举，随机搜索在 500 次评估下只能达 125.6ms 且仅 54.8% 配置合法；MCTS 通过 UCB1 聚焦有希望区域，500 次评估达 108.7ms，接近 oracle 的 107.9ms。
- **为什么注意力引导排序**：标准 MCTS 把所有决策变量同等对待，浪费模拟预算在低影响 fragment 上。把敏感 fragment 放浅层，让 MCTS 在关键决策上多探索——这是把"代价模型的结构信息"反向喂回搜索的关键设计。vanilla MCTS 需 1540ms，优化 MCTS 仅需 620ms。
- **为什么等价类剪枝**：OLAP 查询常有结构对称的 fragment（星型 join 的对称分支、复制维度表 scan）。它们在 DOP 决策上等价，合并后搜索深度骤降，对动辄几十 fragment 的大计划尤为关键。
- **为什么 rollout 用随机而非贪心**：随机 rollout 配合 UCB1 的探索项保证搜索不被早期偏见锁定；最终评估仍由代价模型完成，保证质量。

### 步骤 4：搜索到策略的蒸馏

**具体工作**
- **策略架构**：与代价模型共享 Graph Transformer 编码器（复用预训练权重作特征骨干），把评分头替换为 $N$ 个并行分类头（每个 fragment 一个），每个头输出候选 DOP 上的分布 $p_j = \text{Softmax}(\text{ClassHead}_j(h_j))$。推理时取 $\arg\max_k p_j[k]$。
- **DOP 掩码蒸馏训练**：构造输入图时**把节点特征中的 DOP 维度置零**。共享编码器以 $0.1\times$ 基础学习率微调，分类头从零开始以全速率训练。损失为所有 fragment 的交叉熵之和：$\mathcal{L}_{\text{distill}} = \sum_{j=1}^{N} \text{CE}(p_j, C^*[j])$。
- **推理成本**：单次前向 <5ms（50 fragment 以内），符合在线编译预算。

**作用**
- 把秒级的 MCTS 压缩成毫秒级的单次前向，使在线部署可行。
- 输出完整 DOP 向量，一次推理覆盖所有 fragment。

**设计逻辑**
- **为什么共享编码器**：代价模型已学到"plan 结构 → 性能敏感度"的良好表示，复用作为特征骨干可加速收敛并保留排序知识。
- **为什么 DOP 掩码**：若输入含 DOP 特征，模型可能直接复制输入 DOP 到输出，而非学习"plan 结构 → 合适 DOP"的映射。置零强制策略仅凭算子语义、统计、放置、拓扑推断 DOP——这是在线推理时唯一的可用信息（查询到达时 DOP 尚未决定）。这是防止"捷径学习"的关键设计。
- **为什么每个 fragment 独立分类头**：DOP 是离散选择，分类头天然匹配；并行头允许一次前向同时输出所有 fragment 的 DOP，避免自回归解码的延迟。
- **为什么编码器低学习率、头高学习率**：保护预训练表示不被新任务破坏，同时让新头快速拟合。

### 步骤 5：运行时 DOP 适配与引擎集成

**具体工作**

**(a) 预实例化 DOP 修正**
- 仅在 task instance 创建前修正 DOP。此时 worker 放置已知，可推理 per-worker 竞争与当前负载；routing table、driver 布局、worker-local 分区映射尚未实例化，有界修正不会破坏执行布局。

**(b) EWMA 平滑的负载监控**
- 后台线程每 2s 轮询每个 worker 的 metrics 端点，采集 CPU 使用率与内存压力。
- 用 EWMA 平滑：$L_t = \beta \cdot L_{\text{raw}} + (1-\beta) \cdot L_{t-1}$，$\beta=0.25$，有效记忆约 4 次观测——跟踪持续负载变化，过滤 GC 暂停、网络尖峰等瞬态。

**(c) 死区缩放因子**
- 每个 worker 的缩放因子带死区，轻载下不动 DOP：

$$s_{\text{worker}} = \begin{cases} 1, & \text{CPU} \le \tau_{\text{CPU}} \;\land\; \text{MEM} \le \tau_{\text{MEM}} \\ \min(s_{\text{CPU}}, s_{\text{MEM}}), & \text{otherwise} \end{cases}$$

其中 $s_{\text{CPU}} = \max(0.1,\; 1 - 0.9 \cdot \frac{\text{CPU}_{\text{usage}}-\tau_{\text{CPU}}}{1-\tau_{\text{CPU}}})$，$s_{\text{MEM}}$ 类似。阈值处因子=1.0（不降），满载处触底 0.1。默认 $\tau_{\text{CPU}}=0.4$，$\tau_{\text{MEM}}=0.65$。

- 对每个有明确物理放置的 fragment，取所有目标 worker 缩放因子的最小值（任一目标节点受压即缩）：

$$d_{\text{adjusted}} = \max\Big(1,\; \big\lfloor d_{\text{base}} \times \min_{w \in \text{targets}} s_w \big\rfloor_{\text{round}}\Big)$$

$\lfloor\cdot\rfloor_{\text{round}}$ 舍入到 $K_i$ 中最近的合法候选。缩放在 fragment 粒度施加（同一 fragment 所有实例一致 DOP），以保持 exchange 路由一致性。

**(d) 与查询调度器的集成——两趟编译**
1. **Pass 1（worker 选择）**：调度器通过 scan-range 分配与 placement 规则确定每个 fragment 的目标 worker，不创建任务实例。
2. **AutoDOP hook**：策略网络预测 per-fragment DOP，负载缩放器用当前 worker 指标调整，最终 DOP 向量写入 plan。
3. **Pass 2（实例创建）**：创建 task instance、分配 per-driver scan range、用最终 DOP 构建 exchange routing table。sender 与 receiver 从同一 DOP 派生分区数，保证路由一致。

**(e) 约束校验与回退**
- 安装前校验 $\Omega(P)$：exchange 两端 DOP 一致性、scan-range 归属（每个 range 恰归一个 driver）、colocate 算子放置亲和性。舍入算子可引擎特定（如要求 2 的幂）。
- 校验失败或模型置信度低 → 丢弃预测，回退引擎原生 DOP 策略，**系统绝不产生非法执行计划**。

**作用**
- 把"仅看 plan 结构"的策略输出适配到"查询到达瞬间的集群状态"，避免在高负载 worker 上叠加 DOP。
- 保证引擎安全性与正确性，不破坏 StarRocks 的路由与分区语义。

**设计逻辑**
- **为什么只在实例化前修正**：实例化后改 DOP 会破坏 routing table 与逻辑分区到物理 driver 的映射，导致 shuffle 数据错路由。Pass 1 与 Pass 2 之间的窗口是唯一既能看到放置、又能安全改 DOP 的时机。
- **为什么 EWMA 而非瞬时值**：瞬时 CPU 受 GC、网络尖峰影响大，直接用会过度反应。EWMA 的 4 次观测记忆跟踪"持续负载漂移"而非"瞬态事件"。
- **为什么死区**：轻载下策略输出已是最优，强行缩放只会引入舍入误差与抖动。死区让系统在阈值以下"什么都不做"，符合最小干预原则。
- **为什么取 min 跨 worker**：fragment 的任何目标节点受压都会成为瓶颈，按最弱节点缩放是最保守、最安全的策略。
- **为什么 fragment 粒度缩放**：exchange 路由要求 sender/receiver 并行度一致，per-instance 差异化 DOP 会破坏这一不变量。
- **为什么坚持回退**：生产系统第一原则是不出错。模型可能给出非法或低质预测，回退机制保证最坏情况退化为原生策略，不会让查询失败。
- **实验佐证**：并发 40 时，负载修正额外贡献 4.2% 平均延迟与 5.0% P90 降幅；runnable driver 从 96 降到 68，exchange wait 从 28.6% 降到 18.8%——证明负载修正机制在高并发下尤为关键。

---

## 5. 方法整体的工作流

AutoDOP 的完整工作流分为**离线 pipeline** 与**在线 pipeline** 两条路径，二者通过蒸馏产物（策略网络权重）衔接，并通过 trace 回流形成闭环。

### 5.1 离线 Pipeline（训练与搜索）

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. 轨迹采集                                                      │
│    历史查询 → 扰动 per-fragment DOP → 执行 → 记录 (plan, C, 延迟) │
│    语料：2400 条三元组（TPC-DS SF10）                            │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. 竞争图构建                                                    │
│    plan → 节点(算子/数据/放置/DOP特征)                            │
│         → 依赖边(数据流) + 加权竞争边(时间重叠×共置×资源压力)      │
│    输出：G(P,C) = (V, E_dep, E_comp)                             │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. 排序代价模型训练                                              │
│    构造 pair (C+, C-) → 动态间隔 hinge ranking loss              │
│    Graph Transformer + 全局 readout + MLP → S_θ(P,C)            │
│    产出：代价模型（代理评估器）                                   │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ 4. 结构感知 MCTS 搜索                                            │
│    注意力排序变量 + 等价类剪枝 + UCB1 + 代理 rollout              │
│    500 次迭代/查询 → 输出高质量 DOP 标签 C*                      │
│    耗时：0.62s/查询（优化 MCTS）                                  │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ 5. 策略蒸馏                                                      │
│    共享编码器(0.1×lr) + N 个分类头(全 lr)                         │
│    DOP 掩码输入 → 交叉熵损失 → 单次前向 <5ms                      │
│    产出：在线策略网络（部署产物）                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 5.2 在线 Pipeline（查询编译时）

```
查询到达
   │
   ▼
┌──────────────────────────────────────────┐
│ Pass 1: worker 选择                      │
│   scan-range 分配 + placement 规则       │
│   确定 fragment → worker 映射 Π          │
│   (不创建任务实例)                        │
└──────────────────┬───────────────────────┘
                   ▼
┌──────────────────────────────────────────┐
│ AutoDOP hook (Pass 1 与 Pass 2 之间)     │
│                                          │
│ (a) 特征提取 (0.74ms)                    │
│     plan + placement → 竞争图 G(P)       │
│     (DOP 维度置零)                        │
│                                          │
│ (b) 策略推理 (2.90ms)                    │
│     G(P) → 策略网络 → per-fragment DOP   │
│     d_base = argmax p_j[k]               │
│                                          │
│ (c) 负载修正 (0.31ms)                    │
│     EWMA(CPU, MEM) → s_worker            │
│     d_adj = round(d_base × min s_w)      │
│                                          │
│ (d) 约束校验 (0.46ms)                    │
│     校验 Ω(P): exchange 一致性、         │
│     scan-range 归属、colocate 亲和       │
│     失败 → 回退原生策略                  │
│                                          │
│     总在线开销: 4.41ms (查询的 4.1%)     │
└──────────────────┬───────────────────────┘
                   ▼
┌──────────────────────────────────────────┐
│ Pass 2: 实例创建                         │
│   用最终 DOP 创建 task instance          │
│   分配 per-driver scan range             │
│   构建 exchange routing table            │
│   (sender/receiver 一致分区数)           │
└──────────────────┬───────────────────────┘
                   ▼
              查询执行
                   │
                   ▼
┌──────────────────────────────────────────┐
│ 执行完成 → profile 回流                  │
│   trace 加入离线语料 → 周期重训          │
└──────────────────────────────────────────┘
```

### 5.3 工作流的关键设计要点

1. **离线/在线解耦**：秒级的 MCTS 只在离线跑，在线只做毫秒级前向推理——这是满足编译预算的唯一方式。
2. **DOP 掩码贯穿训练与推理**：蒸馏时输入 DOP 置零，推理时本就无 DOP 可填——训练/推理分布一致，避免捷径学习。
3. **两趟编译的窗口期**：AutoDOP hook 精确卡在"放置已知、实例未建"的窗口，是同时保证"看到放置信息"与"改 DOP 安全"的唯一时机。
4. **保守兜底**：负载修正有死区、取 min、fragment 粒度；约束校验失败即回退——每一步都把安全性放在最优性之前。
5. **闭环演化**：执行 trace 回流离线语料，随工作负载/数据量/硬件变化周期重训，避免模型陈旧。
6. **引擎无关与引擎相关分离**：核心学习/搜索逻辑可移植到任何"暴露 exchange-delimited fragment + worker 放置 + 实例化前 per-fragment 并行度"的 MPP 引擎；StarRocks 适配层封装所有引擎特定细节。

---

## 6. 效果汇总

| 场景 | 指标 | 改善 |
|---|---|---|
| TPC-DS 并发 1, SF10 | 平均延迟 | -3.3% |
| TPC-DS 并发 1, SF100 | 平均延迟 | -4.5% |
| TPC-DS 并发 1, SF10 | P90 | -3.1% |
| TPC-DS 并发 1, SF100 | P90 | -3.9% |
| TPC-DS 并发 40, SF100 | 平均延迟 | -10.0% (2439.8 vs 2711.0 ms) |
| TPC-DS 并发 40, SF100 | P90 | -10.0% (5443.5 vs 6048.3 ms) |
| 并发 40 资源压力 | runnable drivers/worker | 96 → 68 |
| 并发 40 资源压力 | exchange wait | 28.6% → 18.8% |
| 在线开销 | 总在线路径 | 4.41ms (查询的 4.1%) |
| 代价模型 | pairwise accuracy | 93.0% (GT_pair_comp) |
| 搜索质量 | 500 评估 | 108.7ms (oracle 107.9ms) |

**核心结论**：AutoDOP 的收益随并发度上升而扩大——单查询时主要靠 per-fragment 异质性优化，高并发时负载修正额外发挥作用，证明"竞争感知 + 负载适配"两层设计的必要性。TPC-H 因计划结构简单、fragment 少、高竞争 fragment 少，优化空间有限，存在小幅回退，但约束校验与回退机制保证安全。