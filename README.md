# AutoDOP FE 交付包

本分支仅存放软件交付物，不含项目源码和项目历史。

- 软件：`autodop-fe-20260926-linux-amd64.tar.gz`（991 MiB）
- 内容：独立 StarRocks FE、AutoDOP CPU 推理服务及冻结模型；不含 BE。
- [使用文档](使用文档.md)
- [验证报告](验证报告.md)
- [SHA256 校验文件](autodop-fe-20260926-linux-amd64.tar.gz.sha256)
- [源码分支](https://github.com/Areedd/autodop/tree/codex/autodop-release-20260925)

## 下载

压缩包使用 Git LFS 存储。安装 Git LFS 后执行：

```bash
git lfs install
git clone --single-branch --branch codex/autodop-package-20260926 https://github.com/Areedd/autodop.git autodop-package
cd autodop-package
git lfs pull
sha256sum -c autodop-fe-20260926-linux-amd64.tar.gz.sha256
```

也可以打开压缩包的 GitHub 文件页面，点击下载原始文件。不要将 LFS 指针文件当作压缩包；下载完成后的压缩包应为 1,039,134,400 字节，且通过上述 SHA256 校验。

启动步骤详见使用文档。FE 启动、SQL 连接和独立模型推理已验证；执行数据查询需要另行接入兼容 BE。
