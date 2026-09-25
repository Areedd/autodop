# 12 条无 CRC 查询的特点与结构汇总

## 1. 结论摘要

最终认证集合包含 12 条 TPC-DS SF100 查询。它们覆盖三类典型工作：

1. 8 条数据治理与元数据质量审计；
2. 2 条销售—退货财务对账；
3. 2 条跨渠道客户差集/交集分析。

这 12 条 SQL 在业务表达上不同，但不能视为 12 种完全不同的物理
计划。规范化后共有 5 种物理拓扑：

- 8 条治理查询共享 1 种三 fragment 星型 Join/聚合拓扑；
- 两条退货对账因 Join 语义和投影不同，构成 2 种拓扑；
- `EXCEPT` 和 `INTERSECT` 各构成 1 种五 fragment 拓扑。

每条查询的平均延迟、中位延迟和 P99 都至少提升 30%。12 条查询的
中位提升均值为 37.541%，范围为 32.805%–42.817%。这些数字来自两组
独立实验：治理查询测量 5 次，对账与集合查询测量 2 次，因此不把
两组样本直接合并成一个新的总体 P99。

## 2. 查询分类

| 类别 | 数量 | 主要算子 | 中位提升均值 |
|---|---:|---|---:|
| 数据治理审计 | 8 | 大事实表扫描、小维表 Hash Join、逐行规则、无分组聚合 | 39.087% |
| 销售—退货对账 | 2 | 两侧预聚合、Hash Join、最终汇总 | 33.189% |
| 跨渠道客户分析 | 2 | 三路事实表扫描、`EXCEPT`/`INTERSECT`、最终计数 | 35.709% |

## 3. 逐条查询说明

### 3.1 销售—退货对账

#### `return_inner_reconciliation`

- 业务目标：统计确实发生退货的门店订单商品，并核对原支付金额和
  退款金额。
- 输入：`store_sales` 与 `store_returns`。
- SQL 结构：
  1. 销售侧按 `(ticket_number, item_sk)` 聚合；
  2. 退货侧按相同业务键聚合；
  3. 两个聚合结果执行 Inner Hash Join；
  4. 最终计算匹配商品数、支付总额和退款总额。
- 计划：3 个 fragment、2 个事实表 Scan、4 个 Aggregate、
  1 个 Hash Join、2 个 Exchange。
- 特点：先聚合再 Join，能减少 Join 输入规模；两个输入侧都是有实际
  计算量的 fragment，而最终单行汇总很轻。
- 中位延迟：6052.465 ms → 4066.958 ms，提升 32.805%。

#### `return_full_reconciliation`

- 业务目标：检查目录渠道中“只有退货没有销售”和“销售后未退货”
  两类不匹配记录。
- 输入：`catalog_sales` 与 `catalog_returns`。
- SQL 结构与上一条类似，但使用 Full Outer Join，并通过
  `paid IS NULL`、`refunded IS NULL` 分别计数。
- 计划：3 个 fragment、2 个 Scan、4 个 Aggregate、
  1 个 Full Outer Hash Join、2 个 Exchange。
- 特点：保留 Join 两侧未匹配数据，适合财务完整性、孤儿退款和留存
  订单检查。
- 中位延迟：2516.932 ms → 1671.923 ms，提升 33.573%。

### 3.2 跨渠道客户分析

#### `channel_three_way_exclusive`

- 业务目标：计算只在门店购买、但没有在目录或 Web 渠道购买的客户。
- 输入：`store_sales`、`catalog_sales`、`web_sales`。
- SQL 结构：门店客户集合连续减去目录客户集合和 Web 客户集合，
  最外层执行 `COUNT(*)`。
- 计划：5 个 fragment、3 个事实表 Scan、1 个 `EXCEPT`、
  2 个 Aggregate、4 个 Exchange。
- 特点：集合运算需要对三个大事实表提取客户键、去重并交换数据；
  结果只有一个标量，不受客户端结果传输影响。
- 中位延迟：2067.205 ms → 1343.493 ms，提升 35.009%。

#### `channel_customer_intersection`

- 业务目标：统计同时出现在门店、目录和 Web 三个渠道中的客户。
- 输入表与上一条相同。
- SQL 结构：三个客户集合连续执行 `INTERSECT`，最外层计数。
- 计划：5 个 fragment、3 个事实表 Scan、1 个 `INTERSECT`、
  1 个 Aggregate、4 个 Exchange。
- 特点：与差集查询共享三路数据流，但集合状态维护和输出语义不同。
- 中位延迟：2331.778 ms → 1482.820 ms，提升 36.408%。

### 3.3 数据治理与元数据质量审计

以下 8 条查询具有共同骨架：

```text
大事实表 Scan
      │
      ├── 小维表 Scan
      │
      ▼
Hash Join → Project/逐行规则 → Local Aggregate
      │
      ▼
Final Aggregate → 单行结果
```

规范化计划均为 3 个 fragment、2 个 Scan、1 个 Hash Join、
2 个 Aggregate、2 个 Project 和 2 个 Exchange。它们的物理拓扑相同，
差异主要在业务字段和逐行计算表达式。

#### `audit_customer_identity`

- `store_sales` Join `customer`。
- 检查姓名缺失、空白姓名、出生年份越界。
- 使用 `LOWER(TRIM(last_name))` 估算规范化姓氏基数，并汇总相关收入。
- 代表客户主数据完整性和身份字段标准化。
- 中位延迟：3678.348 ms → 2180.946 ms，提升 40.709%。

#### `audit_customer_email`

- `catalog_sales` Join `customer`。
- 使用 `LOCATE` 检查 `@`、本地部分和域名结构；
  使用 `SUBSTRING`、`LOWER` 提取规范化邮件域。
- 代表 CRM 联系信息质量、营销可触达性和域分布检查。
- 中位延迟：2315.808 ms → 1422.194 ms，提升 38.588%。

#### `audit_address_completeness`

- `store_sales` Join `customer_address`。
- 对街道、城市、州和国家执行空值、`TRIM` 和长度检查。
- 代表配送地址、客户地址和地域维表的完整性监控。
- 中位延迟：4477.244 ms → 2627.484 ms，提升 41.315%。

#### `audit_address_postcode`

- `catalog_sales` Join `customer_address`。
- 检查邮编缺失及异常长度；
  使用 `LEFT(TRIM(zip), 5)` 估算邮区数量；
  使用 `LOWER(TRIM(city))` 估算规范化城市数量。
- 代表地址标准化、邮区覆盖和物流主数据检查。
- 中位延迟：1987.981 ms → 1136.782 ms，提升 42.817%。

#### `audit_item_description`

- `store_sales` Join `item`。
- 检查描述缺失、描述过短、商品名为空；
  估算规范化商品名基数。
- 代表电商目录内容质量、搜索索引输入检查和商品信息治理。
- 中位延迟：2103.445 ms → 1299.210 ms，提升 38.234%。

#### `audit_item_taxonomy`

- `catalog_sales` Join `item`。
- 检查品类、Class、品牌缺失和售价低于批发成本；
  使用规范化 `category/class` 路径估算分类层次基数。
- 代表商品分类治理、价格异常监控和目录一致性检查。
- 中位延迟：1352.401 ms → 775.436 ms，提升 42.662%。

#### `audit_store_metadata`

- `store_sales` Join `store`。
- 检查门店名、员工数、营业面积和州编码；
  估算规范化市场经理数量。
- 代表门店主数据、组织架构和运营元数据审计。
- 中位延迟：2350.767 ms → 1523.761 ms，提升 35.180%。

#### `audit_calendar_consistency`

- `store_sales` Join `date_dim`。
- 检查周末标志与星期编号冲突、相邻节假日标志、月内日期范围；
  估算活跃周数。
- 代表日历维表、财务期间和节假日规则一致性检查。
- 中位延迟：1332.075 ms → 889.952 ms，提升 33.191%。

## 4. 性能结果总表

| 查询 | DOP=0 中位延迟 | Fragment 中位延迟 | 中位提升 | mean/median/P99 最低提升 |
|---|---:|---:|---:|---:|
| `return_inner_reconciliation` | 6052.465 ms | 4066.958 ms | 32.805% | 32.805% |
| `return_full_reconciliation` | 2516.932 ms | 1671.923 ms | 33.573% | 33.573% |
| `channel_three_way_exclusive` | 2067.205 ms | 1343.493 ms | 35.009% | 33.262% |
| `channel_customer_intersection` | 2331.778 ms | 1482.820 ms | 36.408% | 36.408% |
| `audit_customer_identity` | 3678.348 ms | 2180.946 ms | 40.709% | 39.745% |
| `audit_customer_email` | 2315.808 ms | 1422.194 ms | 38.588% | 38.588% |
| `audit_address_completeness` | 4477.244 ms | 2627.484 ms | 41.315% | 41.315% |
| `audit_address_postcode` | 1987.981 ms | 1136.782 ms | 42.817% | 39.538% |
| `audit_item_description` | 2103.445 ms | 1299.210 ms | 38.234% | 36.790% |
| `audit_item_taxonomy` | 1352.401 ms | 775.436 ms | 42.662% | 39.753% |
| `audit_store_metadata` | 2350.767 ms | 1523.761 ms | 35.180% | 33.138% |
| `audit_calendar_consistency` | 1332.075 ms | 889.952 ms | 33.191% | 33.191% |

## 5. Fragment DOP 结构

### 5.1 治理查询

54 次治理查询跟踪全部安装相同配置：

```text
F00 = 8  大事实表 Scan + Hash Join + Project + Local Aggregate
F01 = 1  小维表 Scan
F03 = 1  Final Aggregate + Result
```

典型扫描范围为：重 fragment 192 个，小维表 fragment 5 个。模型将
计算密集的大事实表 fragment 提升到 8，同时避免给维表扫描和单行合并
分配无用 driver。

### 5.2 退货对账

两条查询均安装：

```text
销售侧聚合/Join fragment = 8
退货侧预聚合 fragment   = 8
最终单行汇总 fragment   = 1
```

与治理查询不同，退货侧虽然扫描范围少，但其预聚合成本仍较高，因此也
使用 DOP 8。

### 5.3 三渠道集合运算

- 差集查询：最大门店扫描 fragment 和 `EXCEPT` fragment 使用 8，
  其余两个渠道 Scan 及最终汇总使用 1。
- 交集查询：一个大渠道 Scan 和 `INTERSECT` fragment 使用 8，
  其余 Scan 及最终汇总使用 1。

这里的 `F00`、`F02` 等编号只在单条物理计划内有效，不能跨查询按编号
比较；应按算子、成本、扫描范围和上下游位置解释。

## 6. 为什么这些查询容易从 Fragment AutoDOP 获益

### 6.1 重计算集中在少数 fragment

治理查询将字符串清洗、条件判断、近似去重和收入聚合放在大事实表
Join 后执行。重 fragment 可以使用 8 个 driver，而维表构建和最终
合并保持 1。

### 6.2 输出基数很小

所有查询最终只返回一个或少量指标。性能差异主要来自 BE 内部执行，
不会被结果网络传输或客户端取数掩盖。

### 6.3 存在天然的轻重不均衡

- 事实表与维表大小不对称；
- 局部计算与最终合并成本不对称；
- 三渠道事实表行数和集合运算成本不对称。

全局 DOP 无法表达这些差异，而 fragment DOP 可以分别配置。

### 6.4 计算具有业务含义

CPU 工作来自 `TRIM`、`LOWER`、`LENGTH`、`LOCATE`、`SUBSTRING`、
条件规则、近似基数和财务聚合，而非统一的 CRC 行指纹。退货与集合
查询的开销来自预聚合、数据交换和集合状态维护。

## 7. 代表性与局限

### 具有代表性的方面

- 数据仓库每日全量质量检查；
- CRM、地址、商品和组织主数据治理；
- 销售与退款财务核对；
- 全渠道客户覆盖、排他客户和交叉购买分析；
- ETL 后置校验、数据可观测性和监管报表。

### 不能代表的方面

- 8/12 查询属于治理审计，工作负载分布仍偏向数据质量；
- 8 条治理 SQL 共享同一物理拓扑，不能据此宣称有 8 种不同计划；
- 缺少高基数 Group By、大结果集、复杂窗口、Top-N 和多层星型 Join；
- 实验为单客户端、三台 8 核 BE、TPC-DS SF100；
- 两条对账和两条集合查询只有 2 次测量，稳定性证据弱于 5 次测量的
  治理查询。

因此，12 条查询可以作为三类有效场景的代表样本，但不能替代一个
覆盖通用 BI/OLAP 的完整查询基准。

## 8. 可复用的构造规则

1. 从真实业务校验或对账目标出发，不先选择“重函数”。
2. 让逐行规则与大事实表 Scan/Join 位于同一可并行 fragment。
3. 使用小维表进行富化，但避免把大量 driver 浪费在维表扫描上。
4. 将最终结果限制为低基数指标，使 Final Aggregate 保持轻量。
5. 对销售和退款先按业务键预聚合，再执行 Join。
6. 对跨渠道覆盖使用原生 `EXCEPT`/`INTERSECT`，不使用哈希校验和模拟。
7. 同时保留普通聚合、高基数组和纯金额算术负对照，防止只报告有利
   查询。
8. 分开统计业务类别数、SQL 数和物理计划数，不把换字段后的同构计划
   计为新拓扑。

## 9. 关联文件

- SQL 合集：`queries/three_be_serial_representative_v2/validated_combined12/validated_queries.sql`
- 查询清单：`queries/three_be_serial_representative_v2/validated_combined12/manifest.json`
- 治理查询复测：`runs/three_be_serial_representative_v2/retrained_fragment_audit9_r1.json`
- 50 结构宽筛：`runs/three_be_serial_representative_v2/retrained_fragment_expanded50_r1.json`
- Fragment 模型：`runs/three_be_serial_representative_v2/learned_fragment_tree.json`
