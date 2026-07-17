# AutoDOP 使用方法

## 1. 环境准备

AutoDOP 的执行侧改动位于 FE，需要编译并部署本仓库的 FE：

```bash
./build.sh --fe
```

准备 Python 环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r tools/autodop/requirements-autodop.txt
```

运行 AutoDOP 还需要：

- 一个使用本仓库 FE 的可运行 StarRocks 集群；
- MySQL 客户端和 curl；
- 待执行的 SELECT 查询及其数据。

AutoDOP 仅支持启用 pipeline engine 的 SELECT 查询，并要求关闭 runtime adaptive DOP。

## 2. 启动决策服务

决策服务提供固定配置、算子子图和 PolicyModel 三种互斥模式。

### 算子子图模式

下面的命令将包含 <code>INTERSECT</code> 的 fragment 及其输入 fragment 设为 DOP 4，其余 fragment 设为 DOP 1：

```bash
python3 tools/autodop/autodop_server.py \
    --operator-subgraph INTERSECT \
    --operator-dop 4 \
    --default-dop 1 \
    --verbose
```

<code>--operator-subgraph</code> 可以替换为执行计划中需要单独调节的算子名称。

### 固定配置模式

<code>*</code> 表示未单独指定的 fragment：

```bash
python3 tools/autodop/autodop_server.py \
    --fragment-dops "*=1,F02=4,F03=4" \
    --verbose
```

fragment ID 可以从 FE 生成的 AutoDOP trace 中读取。

### PolicyModel 模式

```bash
python3 tools/autodop/autodop_server.py \
    --policy-model /tmp/autodop/policy.pt \
    --verbose
```

检查服务是否正常：

```bash
curl http://127.0.0.1:18080/health
```

FE 与决策服务不在同一主机时，通过 <code>--host</code> 监听 FE 可访问的地址，并同步修改 FE 的 <code>auto_dop_endpoint</code>。

## 3. 配置 FE 并执行查询

连接 StarRocks：

```bash
mysql -h 127.0.0.1 -P 9030 -uroot
```

配置决策服务和 trace 目录：

```sql
ADMIN SET FRONTEND CONFIG ("auto_dop_endpoint" = "http://127.0.0.1:18080");
ADMIN SET FRONTEND CONFIG ("auto_dop_timeout_ms" = "500");
ADMIN SET FRONTEND CONFIG ("auto_dop_trace_dir" = "/tmp/autodop/traces");
```

在需要使用 AutoDOP 的 session 中执行：

```sql
SET enable_pipeline_engine = true;
SET enable_runtime_adaptive_dop = false;
SET enable_auto_dop = true;
SET auto_dop_max_dop = 8;
SET pipeline_dop = 1;

USE your_database;
SOURCE /absolute/path/to/query.sql;
```

<code>auto_dop_max_dop=8</code> 时，每个 fragment 的候选 DOP 为 <code>1, 2, 4, 8</code>。候选值始终为 2 的幂，最大不超过 32。

固定配置和算子子图模式只需保证返回值不超过该上限。当前 PolicyModel 的类别集合为 <code>1, 2, 4, 8, 16, 32</code>，运行 PolicyModel 时应改为：

```sql
SET auto_dop_max_dop = 32;
```

决策服务必须为每个 fragment 返回一个候选集合内的 DOP。服务不可达、超时、协议不匹配或返回非法 DOP 时，查询会明确失败。

## 4. 检查实际 DOP

FE 在 <code>auto_dop_trace_dir</code> 下生成按日期命名的文件：

```text
/tmp/autodop/traces/autodop_trace_YYYY-MM-DD.jsonl
```

每条 trace 包含决策请求、服务响应、原始 DOP、安装后的 DOP、实际 instance DOP、决策耗时和查询运行指标。

重点检查以下字段：

- <code>auto_dop.decided_dop</code>：决策服务返回的 fragment DOP；
- <code>auto_dop.installed_dop</code>：FE 安装到执行计划中的 DOP；
- <code>auto_dop.actual_instance_dop</code>：实际发送到 BE instance 的 DOP；
- <code>state</code> 和 <code>error_message</code>：查询终态。

三组 DOP 应逐个 fragment 一致。该检查可以确认修改发生在 fragment/instance 级，而不是只改变全局 <code>pipeline_dop</code>。

## 5. 与全局 DOP 对比

关闭 AutoDOP 后，<code>pipeline_dop</code> 即固定的全局 DOP：

```sql
SET enable_auto_dop = false;
SET pipeline_dop = 1;
SOURCE /absolute/path/to/query.sql;

SET pipeline_dop = 2;
SOURCE /absolute/path/to/query.sql;

SET pipeline_dop = 4;
SOURCE /absolute/path/to/query.sql;

SET pipeline_dop = 8;
SOURCE /absolute/path/to/query.sql;
```

测试 fragment 级配置时，先启动对应的固定配置、算子子图或 PolicyModel 服务，然后执行：

```sql
SET pipeline_dop = 1;
SET enable_auto_dop = true;
SET auto_dop_max_dop = 8;
SOURCE /absolute/path/to/query.sql;
```

对所有模式使用相同 SQL、数据、session 配置、预热次数和重复次数，并交错执行不同模式。并发实验应固定连接数，并同时记录单查询延迟和整批查询完成时间。

## 6. 训练 PolicyModel

训练流程从 FE 生成的 AutoDOP trace 开始。

### 6.1 采集多种 DOP 配置

先使用固定配置模式运行同一查询的多个配置。例如依次启动：

```bash
python3 tools/autodop/autodop_server.py --fragment-dops "*=1"
python3 tools/autodop/autodop_server.py --fragment-dops "*=2"
python3 tools/autodop/autodop_server.py --fragment-dops "*=4"
python3 tools/autodop/autodop_server.py --fragment-dops "*=8"
```

每次只启动一个服务，并在对应配置下重复执行相同查询。获得首条 trace 后，可根据 <code>request.fragments</code> 中的 fragment ID 继续采集局部配置：

```bash
python3 tools/autodop/autodop_server.py \
    --fragment-dops "*=1,F02=4,F03=4"
```

将同一批训练查询产生的 trace 放在一个独立目录中。每个计划至少需要两个延迟存在稳定差异的 DOP 配置，才能构造排序样本。

### 6.2 构建数据集

```bash
python3 tools/autodop/scripts/build_dataset.py \
    --input /tmp/autodop/traces \
    --output /tmp/autodop/dataset \
    --pair-threshold 0.03
```

输出包括 <code>samples.jsonl</code>、<code>pairs.jsonl</code> 和 <code>metadata.json</code>。<code>pair-threshold</code> 用于忽略延迟差异过小的排序对。

### 6.3 训练 CostModel

```bash
python3 tools/autodop/scripts/train_cost.py \
    --dataset /tmp/autodop/dataset \
    --output /tmp/autodop/cost.pt \
    --epochs 50 \
    --batch-size 16
```

### 6.4 生成 DOP 标签

使用 CostModel 和 MCTS 搜索：

```bash
python3 tools/autodop/scripts/search_mcts.py \
    --dataset /tmp/autodop/dataset \
    --cost-model /tmp/autodop/cost.pt \
    --iterations 500 \
    --output /tmp/autodop/mcts_labels.jsonl
```

若只在实测配置中搜索，可增加 <code>--measured-only</code>。若直接使用每个计划的最低实测延迟作为标签，可以执行：

```bash
python3 tools/autodop/scripts/search_mcts.py \
    --dataset /tmp/autodop/dataset \
    --measured-best \
    --output /tmp/autodop/measured_labels.jsonl
```

### 6.5 训练 PolicyModel

```bash
python3 tools/autodop/scripts/train_policy.py \
    --dataset /tmp/autodop/dataset \
    --labels /tmp/autodop/mcts_labels.jsonl \
    --cost-model /tmp/autodop/cost.pt \
    --output /tmp/autodop/policy.pt \
    --epochs 50 \
    --global-context \
    --position-encoding
```

训练完成后，按“PolicyModel 模式”启动服务，并在查询 session 中设置 <code>enable_auto_dop=true</code> 和 <code>auto_dop_max_dop=32</code>。

## 7. 关键配置

| 配置 | 作用 | 默认值 |
| --- | --- | --- |
| FE <code>enable_auto_dop</code> | 为所有符合条件的查询启用 AutoDOP；实验中更推荐使用同名 session 变量 | <code>false</code> |
| FE <code>auto_dop_endpoint</code> | 决策服务地址 | <code>http://127.0.0.1:18080</code> |
| FE <code>auto_dop_timeout_ms</code> | FE 调用决策服务的超时时间 | <code>50</code> |
| FE <code>auto_dop_trace_dir</code> | FE 写入 JSONL trace 的目录 | <code>log/autodop</code> |
| Session <code>enable_auto_dop</code> | 为当前连接启用 AutoDOP | <code>false</code> |
| Session <code>auto_dop_max_dop</code> | fragment 候选 DOP 的上限 | <code>0</code>，继承其他并行度上限 |
| Session <code>pipeline_dop</code> | AutoDOP 调整前的基础 DOP；关闭 AutoDOP 时即全局 DOP | 由 StarRocks 决定 |
