# AutoDOP 使用文档

## 交付内容与运行环境

`autodop-20260925-linux-amd64.tar.gz` 是包含 AutoDOP CPU 推理服务、冻结策略模型、StarRocks FE/BE 和离线 Docker 镜像的交付包。它采用单 FE、单 BE，适用于本地运行和功能验证。原实验使用的多 BE、TPC-DS/TPC-H 数据并未包含在包中。

需要 Linux x86_64、Docker Engine 和 Docker Compose v2，建议至少 6 个 CPU、12 GiB 可用内存及 20 GiB 可用磁盘。BE 继承已有构建的 CPU 指令集要求；本次验证机器为 Intel Xeon x86_64。无需在宿主机安装 Java、Python、PyTorch 或 CUDA。镜像导入后可以离线启动。

目录内容：

| 文件 | 用途 |
| --- | --- |
| `images.tar` | AutoDOP 和 StarRocks 两个离线镜像 |
| `compose.yaml` | 单节点运行配置及持久化数据卷 |
| `models/policy.pt` | 已有训练生成的冻结策略模型 |
| `models/request.json` | HTTP 决策接口示例请求 |
| `BUILD_MANIFEST.json` | 模型、FE/BE 二进制、源码和镜像哈希 |
| `VALIDATION.md`、`validation/` | 本次构建与运行测试结果 |
| `SHA256SUMS` | 包内文件校验清单 |
| `docs/` | 使用说明、实验索引与历史报告快照 |

## 解压、校验、启动

在压缩包所在目录执行：

```bash
sha256sum -c autodop-20260925-linux-amd64.tar.gz.sha256
tar -xzf autodop-20260925-linux-amd64.tar.gz
cd autodop-20260925-linux-amd64
sha256sum -c SHA256SUMS
docker load -i images.tar
docker compose -p autodop-demo up -d autodop fe be
docker compose -p autodop-demo --profile check run --rm check
```

最后一条命令最多等待约 4 分钟，注册包内 BE，创建 `autodop_release_smoke.items` 测试表，写入三条数据并检查聚合结果，同时验证 AutoDOP 健康检查与真实模型推理。成功时输出 `"status": "passed"`、`"n": 3`、`"total": 60`。可重复执行。

默认仅绑定宿主机回环地址：

- FE MySQL 协议：`127.0.0.1:19030`，演示用户 `root`，初始密码为空。
- FE HTTP：`http://127.0.0.1:18030`。
- AutoDOP HTTP：`http://127.0.0.1:18080`。

若端口被占用，在 `compose.yaml` 同目录创建 `.env`，例如：

```dotenv
MYSQL_PORT=29030
FE_HTTP_PORT=28030
AUTODOP_PORT=28080
```

再运行上述启动命令。容器内部服务地址保持不变。FE 元数据和 BE 数据保存在 Compose 项目专属的数据卷中。

## 调用与 SQL 验证

```bash
curl -fsS http://127.0.0.1:18080/health
curl -fsS http://127.0.0.1:18080/v1/decide \
  -H 'Content-Type: application/json' --data-binary @models/request.json
mysql -h 127.0.0.1 -P 19030 -u root \
  -e 'SELECT COUNT(*), SUM(value) FROM autodop_release_smoke.items'
```

决策响应包含 `version`、`query_id`、`confidence` 和各 fragment 的 `dop`。`/health` 正常并不等于数据库可执行 SQL，因此请执行完整的 `check` 命令。

包内 FE 已设置 `enable_auto_dop=true`、`auto_dop_endpoint=http://autodop:18080`。对新 SQL 会话可显式开启：

```sql
SET enable_pipeline_engine = true;
SET enable_runtime_adaptive_dop = false;
SET enable_auto_dop = true;
SET auto_dop_max_dop = 16;
```

FE 会按 fragment 数量、置信度和合法 DOP 等规则决定是否采用策略；小查询可能直接绕过策略，服务不可用时默认回退。交付烟雾测试验证软件能启动和执行，并不宣称每条 SQL 都采用 AutoDOP 或取得性能收益。

## 日志、停止与再次启动

```bash
docker compose -p autodop-demo ps
docker compose -p autodop-demo logs --tail 100 autodop
docker compose -p autodop-demo exec fe tail -n 100 /opt/starrocks/fe/log/fe.log
docker compose -p autodop-demo exec be bash -c 'tail -n 100 /opt/starrocks/be/log/be.INFO'
docker compose -p autodop-demo down
```

`down` 保留数据卷；下次使用相同 `-p autodop-demo` 启动会继续使用原数据。只有明确希望删除演示数据时才使用 `down -v`。如果启动失败，先查看 `ps` 与容器日志，检查端口、剩余内存及 CPU 架构。

## 直接从源码运行 AutoDOP

项目源码单独保存在 [GitHub 发布分支](https://github.com/Areedd/autodop/tree/codex/autodop-release-20260925)，运行压缩包不附完整项目源码归档。

在项目根目录，使用 Python 3.11 或 3.12 创建独立环境。CPU 环境示例：

```bash
python3 -m venv .venv-autodop
. .venv-autodop/bin/activate
python -m pip install torch==2.9.0 --index-url https://download.pytorch.org/whl/cpu
python tools/autodop/autodop_server.py \
  --host 127.0.0.1 --port 18080 --policy-model /absolute/path/to/policy.pt
```

模型必须是本项目格式 v2 的 PolicyModel checkpoint。可使用交付包的 `models/policy.pt`；不要把 cost checkpoint 作为策略模型。`--no-load-correction` 关闭在线负载修正；`--policy-cache-capacity N` 开启容量为 N 的计划决策缓存，默认关闭，缓存命中仍使用当前负载修正。

## 训练与验证

数据目录需包含 `samples.jsonl`、`pairs.jsonl`、`metadata.json`。从项目根目录执行，使用尚未存在或为空的输出目录：

```bash
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 python tools/autodop/scripts/run_pipeline.py \
  --dataset /absolute/path/to/dataset --output-dir /tmp/autodop-new-training \
  --cost-epochs 50 --policy-epochs 150 --iterations 500 --threads 1
```

流水线包含成本模型训练、留出集评估、MCTS 标签搜索、策略蒸馏及本机 HTTP 验证。结果含成本与策略 checkpoint、数据快照、训练来源信息和 `serving.json`。需要重新验证时：

```bash
python tools/autodop/scripts/verify_policy.py \
  --policy-model /tmp/autodop-new-training/policy.pt \
  --dataset /tmp/autodop-new-training/dataset \
  --labels /tmp/autodop-new-training/labels.jsonl --output /tmp/serving-check.json
```

不要混用不同训练来源的模型、数据和标签；工具会校验指纹。可信采集、SQL 生成及回归测试的额外依赖见 `requirements-dev.txt`。涉及真实 SQL 数据采集的命令需自行提供兼容数据库和数据。

## 源码回归与重新打包

```bash
python -m pip install -r tools/autodop/requirements-dev.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  python -m pytest tools/autodop/tests -q
JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
  mvn -o -f fe/pom.xml -pl fe-core -am -DskipTests -Dcheckstyle.skip package
```

这里的 Maven `-o` 仅适用于已备齐依赖的构建环境；新机器需先配置原项目的 Maven 和第三方依赖。打包还需要已有 `starrocks-hadoop-ext.jar`、BE 镜像及匹配源码和二进制哈希的构建证明，不能只用一个任意旧镜像。

```bash
python tools/autodop/release/build_release.py \
  --output dist/my-autodop-release \
  --policy-model /absolute/path/to/policy.pt \
  --request /absolute/path/to/request.json \
  --be-image YOUR_AUDITED_BE_IMAGE \
  --be-build-proof /absolute/path/to/BUILD_COMPLETE.json
```

该命令构建镜像并导出 `images.tar`，不自动把未经测试的结果标记为可交付。可从源码运行 `python tools/autodop/release/check_release.py dist/my-autodop-release`，它会导入镜像，以随机项目名和随机回环端口创建全新测试卷，运行 HTTP/SQL 检查，并仅删除该次临时测试卷。日志保存在输出目录的 `validation/` 中。也可进入输出目录，按本文手动运行 `check`，保存测试日志后再压缩。构建时需要网络下载镜像和 Python 依赖，运行已交付的镜像无需下载。实际安装版本与镜像 ID 记录在交付清单中。

## 实验记录与清理范围

当前入口见 [项目 README](../README.md)（源码树）；实验导航见 [EXPERIMENTS.md](EXPERIMENTS.md)，历史脚本恢复说明在源码的 `tools/autodop/archive/README.md`。本地原始 `runs/` 数据约 39 GB，未放入 Git 和运行包。报告快照中保留原始结论、限制及来源路径，不代表原实验已经在此单 BE 交付环境中重新执行。
