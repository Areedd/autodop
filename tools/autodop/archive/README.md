# 历史实验脚本归档

`report90-scripts-20260925.tar.gz` 保存 411 个历史批次脚本，原始路径和 SHA-256 见 `MANIFEST.json`。它们不属于当前通用入口和测试的依赖闭包。归档不表示旧实验结果无效。脚本中的固定机器路径、历史运行状态和部署前提需在复现前逐项检查。

在仓库根目录恢复原路径（先检查有无同名文件）：

```bash
tar -tzf tools/autodop/archive/report90-scripts-20260925.tar.gz
tar -xzkf tools/autodop/archive/report90-scripts-20260925.tar.gz -C tools/autodop
```

`-k` 禁止覆盖已有文件。原始 `runs/` 数据保留在本机，不纳入 Git。当前测试依赖的 6 个 report90 模块仍保留在 scripts 中。
