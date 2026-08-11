# AutoDOP 负载修正与实例级并行度调整 — 执行计划

## 里程碑总览

```
M1 FE 资源视图（AutoDopResourceView + 请求扩展 + 配置）
M2 Python 负载修正（load_correction.py + 引擎接入 + 单测）
M3 FE 实例级调整（AutoDopInstanceAdjuster + 窗口接入 + 单测）
M4 轨迹三阶段快照（TraceManager 扩展 + 兼容性）
M5 集成实验验证（真实集群 + 高负载场景）
```

依赖顺序：M1 → M2/M3（M3 依赖 M1 的资源视图）；M4 与 M2/M3 并行；M5 最后。
每个里程碑结束有评审门禁；M1/M2 之间可独立验证，M3 完成后整体评审。

## M1 FE 资源视图

- [ ] `fe/fe-core/src/main/java/com/starrocks/autodop/AutoDopResourceView.java`（新增）：
  单例 + Daemon 周期任务（间隔 `auto_dop_resource_view_interval_ms`），EWMA 平滑，
  `getWorkerLoad(workerId)` 接口，数据缺失/过期 → 中性值；测试注入钩子。
- [ ] `fe/fe-core/src/main/java/com/starrocks/common/Config.java`：新增
  `auto_dop_resource_view_interval_ms`、`auto_dop_load_ewma_alpha`、
  `auto_dop_load_cpu_threshold`、`auto_dop_load_mem_threshold`、
  `auto_dop_load_factor_floor`、`auto_dop_instance_adjust_floor`、
  `auto_dop_instance_adjust_enabled`。
- [ ] `AutoDopRequest.java`：新增 `workerLoads` 顶层字段 + `WorkerLoad` 内部类。
- [ ] `AutoDopFeatureExtractor.java`：打包请求时填充 worker_loads（覆盖请求中出现的 worker）。
- [ ] 单测：`AutoDopResourceViewTest`（EWMA、新鲜度、缺失中性值、注入）。
- 门禁：FE 单测通过；`mvn -pl fe-core -DskipTests compile` 通过。

## M2 Python 负载修正

- [ ] `tools/autodop/autodop/load_correction.py`（新增）：`worker_factor` /
  `fragment_factor` / `corrected_dop` 纯函数。
- [ ] `engine.py`：`PolicyEngine.decide`、`TreePolicyEngine.decide` 输出后施加修正
  （reason 标记 `load_corrected`）；显式规则引擎不接入。
- [ ] `autodop_server.py`：新增 CLI 参数 `--load-cpu-threshold` / `--load-mem-threshold` /
  `--load-factor-floor`。
- [ ] `tools/autodop/tests/test_load_correction.py`（新增）：覆盖 DD03 §6.3 负载修正用例。
- [ ] 回归：`python3 -m pytest tools/autodop/tests/` 全绿。
- 门禁：修正函数单测全绿；PolicyEngine 带/不带负载的输出差异断言通过。

## M3 FE 实例级调整

- [ ] `AutoDopInstanceAdjuster.java`（新增）：可调整性判定（无上游输入 ∧ 非 colocate ∧
  非 bucket shuffle）+ 按 BE 资源差异化下调实例 DOP（只降不升、候选取整 ≥1、同 BE 一致、
  触底 `auto_dop_instance_adjust_floor`）；`auto_dop_instance_adjust_enabled=false` 时直接跳过。
- [ ] `CoordinatorPreprocessor.java`：`materializeAllFragments()` 后调用
  `AutoDopInstanceAdjuster.adjust(executionDAG)`（受 AutoDOP 开关控制，仅 AutoDOP 路径）。
- [ ] 单测（沿用 `AdaptiveChooseNodesTest` 框架）：高负载 BE 下调 / 低负载保持 / 路由安全
  （有上游输入、colocate、bucket shuffle 不变；实例数与片段级 DOP 不变）/ 候选集与下限。
- 门禁：FE 单测全绿；路由安全断言通过；`mvn -pl fe-core -DskipTests package` 产物可部署。

## M4 轨迹三阶段快照

- [ ] `AutoDopTraceManager.java`：`ActualFragmentDop` 增加 `baseline_instance_dops` /
  `adjusted_instance_dops`；`captureInstanceDop(executionDAG, stage)` 三时点调用
  （materialize 后 / 实例调整后 / finalizeDAG 后沿用 `captureActualDop`）。
- [ ] Python `data.py::sample_from_trace` 兼容性单测：新字段 trace 与旧字段 trace 均能解析。
- 门禁：单测通过；M5 集成中验证三份数据完整性与数值关系。

## M5 集成实验验证

- [ ] 重新构建 FE（`mvn -pl fe-core -DskipTests package` + `output/fe` 部署），重启本地 FE。
- [ ] 启动 `autodop_server.py`（18080），`SET enable_auto_dop=true` 执行 TPC-DS 查询，
  验证决策链路正常、trace 落盘、三阶段实例 DOP 完整。
- [ ] 高负载场景：对 starrocks-be2 加压（并发查询流），验证该 BE 上扫描实例 DOP 被压低、
  其余 BE 保持基准（AC5）。
- [ ] 负载修正开关对比：高负载下开/关修正，验证 DOP 收缩与延迟不劣化（AC5）。
- [ ] 关闭 `auto_dop_instance_adjust_enabled` 回归：实例 DOP 全部保持基准，链路正常。
- 门禁：AC1–AC5 全部满足；决策耗时（trace decision_latency_ms）≤ 50ms。

## 验证命令汇总

```bash
# FE 编译与单测
cd /home/shaokun/starrocks && ./build.sh --fe 2>/dev/null || mvn -pl fe/fe-core -DskipTests package
mvn -pl fe/fe-core -Dtest='AutoDop*Test,AdaptiveChooseNodesTest' test

# Python 单测
cd /home/shaokun/starrocks/tools/autodop && python3 -m pytest tests/ -q

# 集成（FE 部署与重启）
#   output/fe 替换 lib/starrocks-fe.jar 后 bash bin/start_fe.sh --daemon
# 决策服务
python3 tools/autodop/autodop_server.py --policy-model /tmp/autodop/policy.pt --load-cpu-threshold 0.4 ...
```

## 回滚点

- M1/M2 完成后：FE 与 Python 可独立回滚（请求新字段可选，服务端忽略未知字段）。
- M3 完成后：设 `auto_dop_instance_adjust_enabled=false` 即回到 M2 行为，无需重编译。
- M5 前若发现回归：git checkout 相关文件，重新部署 FE jar。
- 全程不修改：执行引擎（BE）、exchange 路由逻辑、离线训练特征。

## 评审门禁清单（最终验收对照 prd.md AC1–AC5）

- [ ] AC1 资源视图：单测 + M5 集成观察
- [ ] AC2 负载修正：Python 单测 + 开关对比实验
- [ ] AC3 实例调整：FE 单测 + 高负载实验
- [ ] AC4 轨迹：单测 + trace 检查
- [ ] AC5 端到端：高负载场景实验报告
