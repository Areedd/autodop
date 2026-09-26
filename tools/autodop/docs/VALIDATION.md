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

## FE + AutoDOP 包验证

当前包移除了 BE，FE 从精简 Java 17 镜像构建，完整项目源码仍单独保存在 GitHub。上面的源码回归结果保留作为实现依据；旧 FE/BE 全栈运行记录见 `history/FULL_FE_BE_RELEASE_VALIDATION_20260926.md`。

本轮已验证通过，隔离项目为 `autodop-check-c6815e9ea44f`：

- 精简 Java 17 镜像中的 FE 在全新元数据卷中启动成功。
- `SELECT 1` 返回 `ok=1`，`SHOW FRONTENDS` 和 `SHOW BACKENDS` 正常，注册 BE 数量为 0。
- AutoDOP `/health` 和冻结模型 HTTP 推理通过，2 个 fragment 返回合法 DOP。
- 测试容器与元数据卷清理成功，返回码 0。
- 扫描导出镜像的全部 16 层、35158 个条目，未发现 BE/CN 目录或 `starrocks_be`；检查覆盖祖先层，避免只在上层删除文件造成虚假缩小。
- FE 仍显示 `UNKNOWN-UNKNOWN` 版本字符串，以 JAR SHA-256 和源码提交标识构建。

数据表读写不属于本轮范围。详细证据见包内 `validation/`。

```bash
python tools/autodop/release/check_release.py dist/autodop-fe-20260926-linux-amd64
```

## 结果边界

本包不包含 BE，不把旧版建表/写入测试视为当前包的数据查询验收。数据查询需要另行部署和接入兼容 BE。历史性能收益不在本次功能验证范围内。
