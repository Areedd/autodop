# AutoDOP

AutoDOP 为 StarRocks 查询的 fragment 预测并行度：离线成本学习与 MCTS 搜索生成标签，策略模型在线推理，再按当前 BE 负载修正；FE 校验响应并在异常时回退。

- [使用文档](docs/USAGE.md)：FE + AutoDOP 离线包（不含 BE）、HTTP 服务、训练、测试、停止与排障。
- [实验日志索引](docs/EXPERIMENTS.md)：104 个本地实验目录、主要报告快照与结论限制。
- [交付验证记录](docs/VALIDATION.md)：源码回归和离线运行检查。
- [清理说明](docs/CLEANUP.md)：保留与归档范围。
- [历史脚本恢复](archive/README.md)：411 个历史 report90 批次脚本，附 SHA-256 清单。

## 当前目录

| 路径 | 内容 |
| --- | --- |
| `autodop/`、`autodop_server.py` | 模型、图特征、MCTS、训练约束和 HTTP 服务 |
| `scripts/run_pipeline.py`、`scripts/auto_retrain.py` | 通用训练流水线和增量重训 |
| `scripts/verify_*.py` | 模型、数据和采集结果核验 |
| `collect_*.py`、`generate_*.py` | 数据采集与查询生成；生成器之间存在传递依赖 |
| `tests/` | 保留的有效回归测试 |
| `release/` | 镜像构建、Compose 配置与真实 HTTP/SQL 烟雾测试 |
| `docs/`、`archive/` | 当前说明、历史实验报告和归档 |
| `runs/` | 本地原始实验、模型与日志；忽略于 Git |

源码及完整交付包均需配合模型使用，快速启动见使用文档。`scripts/reproduce_positive90.py` 等实验复现入口还依赖本地 `runs/` 的原始记录；它们不是交付包的通用安装入口。

90 条优势查询是从已测 200 条查询中事后筛选的子集，不能作为独立泛化测试。冻结模型敏感性实验和有限时间窗口并发实验的适用范围见原始报告。在线缓存默认关闭，缓存命中仍应用实时负载修正。
