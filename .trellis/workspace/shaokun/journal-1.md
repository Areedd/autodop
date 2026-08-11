# Journal - shaokun (Part 1)

> AI development session journal
> Started: 2026-08-11

---

## 2026-08-11 — AutoDOP P0 任务启动与环境恢复

### 环境恢复（重要，供后续会话复用）

实验集群拓扑：
- FE：本地构建产物 `output/fe`（branch-3.5.17 含 AutoDOP 代码），172.21.0.1:9030/8030/9020/9010
- BE：docker 容器 starrocks-be1/2/3（host 网络，127.0.0.1:9050/9051/9052，3.5.17，cluster_id 1895689352-3.5.17）
- 另一套无关集群：wu-starrocks-fe/be（172.23.x，4.1.1，端口映射 19030 等）；vlake-case-doris（Doris 4.1.3）

FE 启动故障修复链条（本次踩坑）：
1. `start_fe.sh` 必须用 `bash` 执行（默认 sh 是 dash，脚本语法不兼容）
2. FE 卡在 waitForReady：meta/image/ROLE 记录的 IP 172.21.0.1 已不存在 → 重建 docker 网络 `docker network create --subnet=172.21.0.0/16 starrocks-net` 恢复该 IP（网桥网关）
3. bdb 与 image/ROLE 身份不一致（bdb 是 192.168.10.110，image 是 172.21.0.1）→ BDB 卡 queryGroupForMembership；删除 bdb 后需 `start_with_incomplete_meta = true`（fe.conf）从 image 重建（重建后应移除此配置）
4. BE 拒绝心跳 "Out-dated epoch"（BE 缓存 epoch=40 > 重建后 FE epoch=1）→ `docker restart starrocks-be1/2/3` 重置内存态 master_info

关键代码结论（供 design.md 使用）：
- 资源数据通道已存在：BE 每 1s 上报 TResourceUsage（cpu_used_permille/mem_used_bytes/mem_limit_bytes）→ FE ComputeNode.updateResourceUsage → getCpuUsedPermille()/getMemUsedPct()/isResourceUsageFresh()
- 实例级 DOP 通道已存在：FragmentInstance.setPipelineDop()（上游 scan-range 逻辑已在用）
- 决策窗口：assignAllPlacements → decideAndInstall → materializeAllFragments → finalizeDAG；实例调整插入 materialize 之后、finalizeDAG 之前（finalize 的 exchDop 只依赖目标片段级 DOP，路由安全）

### 任务

创建任务 08-11-autodop-load-aware-adjustment（P0），完成 prd/design/implement 三文档，待 review 后 start。

## 2026-08-11 实现完成（M1–M5 全部通过）

### 交付内容

- M1 FE 资源视图：`AutoDopResourceView`（单例+2s 采样+EWMA 0.25+新鲜度）、`AutoDopRequest.workerLoads`、FeatureExtractor 填充、7 个 Config 项
- M2 Python 负载修正：`load_correction.py` 纯函数（死区/线性下降/双维取 min/最弱 worker/只降不升候选取整），接入 PolicyEngine/TreePolicyEngine（reason 后缀 :load_corrected），server 新增 --load-* 参数与 --no-load-correction
- M3 FE 实例调整：`AutoDopInstanceAdjuster`（无上游输入 ∧ 非 colocate ∧ 非 bucket shuffle 判定 + 按 BE 差异化下调），插入 materialize 后、finalizeDAG 前
- M4 轨迹三阶段：`baseline_instance_dops`/`adjusted_instance_dops`/`instance_dops`；**register 提前到 materialize 前**（baseline 快照需要 record 已存在）
- M5 集成实验（真实集群）全部验证通过

### 踩坑（重要）

1. **`getComputeNodes()` 不含 Backend**（只含纯 CN）→ worker_loads 恒空；必须用 `backendAndComputeNodeStream()`
2. **重启 FE 时旧进程可能杀不干净**：ps grep "output/fe" 匹配不到新进程 cmdline（java 进程 cmdline 是绝对路径）；用 `ps aux | grep StarRocksFE` 精确匹配；否则旧 jar 一直跑、改动看似无效
3. **mock 集群放置不确定**：集成测试断言不能依赖特定 worker 上有实例；高负载注入到全部 worker 保证必下调
4. 集成测试需在 com.starrocks.qe 包（CoordinatorPreprocessor 包可见），单元测试在 com.starrocks.autodop 包
5. `start_fe.sh` 必须用 bash；BE 重启解决 epoch 不匹配（见上文）

### 实验数据（AC1–AC5 全满足）

- 低负载（mem 2.6%）：reasons 保持 policy，实例 DOP=基准，decision_latency 8-37ms ≤ 50ms
- 高负载（mem 70-99%）：policy:load_corrected（8→4）；实例差异化（F00 adjusted=[4,2]：mem 79.6% 的 11698 降 2、mem 70.4% 的 11697 保持 4）；三阶段 baseline=[4,4]≥adjusted=[4,2]≥final=[4,2]
- --no-load-correction 对照：decided 保持 8（无收缩），实例调整仍生效（[8,8]→[4,4]）
- auto_dop_instance_adjust_enabled=false：实例 DOP 全保持基准，链路正常
- 查询结果正确性不受影响（count 正确）
