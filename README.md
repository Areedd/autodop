# AutoDOP for StarRocks

本项目在 StarRocks 中集成 AutoDOP：使用查询图特征预测 fragment 并行度，并按当前 BE 负载修正。包含成本模型训练、MCTS 搜索、策略蒸馏、HTTP 决策服务，以及 FE/BE 集成代码。

- [使用文档](tools/autodop/docs/USAGE.md)：完整离线包启动、HTTP/SQL 验证、训练、停止与排障。
- [AutoDOP 目录与入口](tools/autodop/README.md)。
- [实验日志与报告](tools/autodop/docs/EXPERIMENTS.md)。
- [项目清理说明](tools/autodop/docs/CLEANUP.md)。
- [历史实验脚本恢复](tools/autodop/archive/README.md)。

在线服务使用格式 v2 的 PolicyModel checkpoint。旧文档中的 `--operator-subgraph`、固定策略及 TreePolicy 服务参数已不适用于当前实现；旧文档留在历史目录供追溯。

交付包包含 CPU 推理服务、冻结模型和独立 FE 离线镜像（不含 BE），无需在宿主机安装 Java 或 PyTorch；数据查询需另行接入兼容 BE。原始实验数据库和约 39 GB 的历史运行数据不在运行包中，性能结论的适用范围见实验报告。

项目源自 StarRocks，许可证与声明见 [LICENSE.txt](LICENSE.txt) 和 [NOTICE.txt](NOTICE.txt)。
