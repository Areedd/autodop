# 交付验证记录

验证日期：2026-09-25 至 2026-09-26（Asia/Singapore）。

## 源码验证

| 项目 | 结果 | 范围 |
| --- | --- | --- |
| Python 回归 | 211 项测试、40 个子测试通过 | 归档后全部 `tools/autodop/tests`，含完整小型训练到 HTTP 验证流水线 |
| FE 编译 | 成功 | 从当前源码编译 4438 个 Java 源文件，生成 `starrocks-fe.jar` 和运行依赖 |
| FE 定向回归 | 83 项通过，无失败、错误或跳过 | AutoDOP、调度队列、内存槽估计、Checkpoint 变量隔离、fragment 放置与 adaptive placement |
| BE CPU 计量回归 | 三组均通过，每组 2 项 | 实际 CPU recorder 源码，分别以 1、8、192 个可用核的 CpuInfo fixture 编译 |
| 历史脚本归档 | 411 个文件哈希全部一致 | 归档压缩文件及逐文件 SHA-256 |
| Python 语法 | 通过 | 当前服务、通用脚本和发布工具 |

Python 命令：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  python -m pytest tools/autodop/tests -q --disable-warnings
```

FE 构建命令（本机已有依赖缓存）：

```bash
JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
  mvn -o -f fe/pom.xml -pl fe-core -am -DskipTests -Dcheckstyle.skip package
```

构建跳过 Checkstyle：首次运行发现原项目 `checkstyle-header.txt` 资源无法定位。Java 编译和测试实际执行，不以跳过测试替代验证。

FE 定向测试使用 `org.jacoco:jacoco-maven-plugin:0.8.8:prepare-agent -Djacoco.propertyName=jacocoArgLine surefire:test`，选择 `AutoDop*Test,FifoSlotQueueTest,IntegerMemorySlotsEstimatorTest,SlotSelectionStrategyV2Test,CheckpointVariableIsolationTest,AdaptiveChooseNodesTest,VariableMgrTest`。发现并修复旧 placement 测试不足五个 fragment 的问题后，用 Maven `test -Dmaven.clean.skip=true -Dcheckstyle.skip -DfailIfNoTests=false -Dtest=AdaptiveChooseNodesTest,FragmentPlacementTest` 重新编译和验证。最终 Surefire XML 合计 83 项全部通过。

BE 本次使用已完成的完整构建，通过原始构建证明核对当前 CPU recorder 源码及镜像内 `starrocks_be` SHA-256；未重新执行全量 BE 测试。全量 StarRocks 测试不属于本次验证范围。

## 交付包验证

首次离线目录包验证通过，项目名 `autodop-check-b2cb3ff14f61`，使用随机回环端口和全新数据卷：

- `docker load -i images.tar` 成功导入两张镜像。
- AutoDOP `/health` 正常，冻结模型完成 2 个 fragment 的真实 HTTP 推理，置信度约 0.999748。
- FE/BE 首次启动、注册 BE、创建主键表、插入数据和聚合查询全部通过，结果为 `n=3,total=60`。
- 测试容器和本次新建数据卷清理成功，返回码 0。

最终重建包再次通过同样检查，项目名 `autodop-check-f01b225e4e02`，清理返回码 0。实际 BE 版本字符串为 `branch-3.5.17-665a52f`；FE 显示 `UNKNOWN-UNKNOWN`，以源码提交及 JAR SHA-256 唯一标识本次编译产物，不用兼容版本冒充产品版本。

`SELECT VERSION()` 返回的是 MySQL 兼容版本；运行软件版本以 `SHOW FRONTENDS`、`SHOW BACKENDS` 及构建哈希为准。最终包再次验证的详细日志保存在包内 `validation/`，源码校验、镜像 ID 和模型来源见 `BUILD_MANIFEST.json`。

重跑完整隔离检查：

```bash
python tools/autodop/release/check_release.py dist/autodop-20260925-linux-amd64
```

压缩包附外部 SHA-256，包内附逐文件 SHA-256。完整项目源码单独保存在 GitHub；运行包保留构建来源哈希，不附项目源码归档。本机数据库、私有配置和原始运行数据不在包内。

## 结果边界

实际 HTTP 推理使用冻结的已训练策略模型。小 SQL 可能因 fragment 门限绕过 AutoDOP，所以单独验证 HTTP 决策，并将 SQL 验证限定为 FE/BE 启动和创建、写入、聚合功能。测试不重放 39 GB 历史实验数据，不复验历史性能收益或独立泛化结论。
