# AutoDOP 实现去重

本次检查运行、图特征、模型、MCTS、训练及 FE 决策链路；确认并合并以下重复实现：

- `model.py`：CostModel 和 LatencyRegressionModel 共用内部 `_GraphScoreModel`，保留各自类型和训练目标，checkpoint 参数名称不变。
- `model.py`：共用 checkpoint 读取、版本及类型校验、权重加载逻辑，保留原公开加载函数及错误消息。
- `engine.py`：负载修正开关的两条路径共用一次响应组装。
- `graph.py`：复用 `data.operator_count`，保留图特征对旧记录的算子名称回退。

没有删除仍被采集器调用的 fragment_order、固定 DOP 采集引擎或回归基线。FE 的 fragment<5 绕过、响应校验、异常回退和审计信息继续保留；没有改动 FE/BE、模型权重、90 条查询或历史源码快照。

验证：原冻结策略对 90 条查询请求的全部响应（包含 DOP 和置信度）在修改前后完全一致；固定随机种子下，成本模型和回归模型的输出及 state_dict 键完全一致。对照文件位于 `/tmp/autodop-parity-before.json` 与 `/tmp/autodop-parity-after.json`。回归测试日志位于 `/tmp/autodop-core-cleanup-tests.log`。

完整回归结果：187 项测试通过，40 个子测试通过。三个实现文件合计净减少 22 行；diff 空白检查通过。
