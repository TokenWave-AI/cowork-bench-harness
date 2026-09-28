# 来源与交付边界

本目录将历史 Codex campaign 的 Goal/app-server、轨迹与重试能力整理为独立入口，面向当前 release 的逐题 runtime。旧 `codex_campaign`、`remote_campaign`、`configurable_campaign` 和 `oracle_code_only` 中的作者机模板、远端 SSH/systemd 调度配置不属于本包运行前提。

配套题包为 `cowork-bench-release-101-public-20260928-64-slim`，其 manifest SHA-256：

```text
158c62dc58febc530971b901bb9efebf2cb4254de736bf15449f9ebba52d4cc6
```

该包有 101 题、47 个非 baseline 逐题 runtime、221 个不同的公开固定镜像引用。它的压缩归档为 175,547,832 字节，SHA-256 为 `132655a58603c19897eb8cd47218e2ca37a1c0e20d737c224e6abc090780df6b`，不含 Docker 镜像层。更换 release 应重建运行配置并验证 registry、工具、补丁捕获和评分，不应复用旧 hash。

## Codex 二进制来源状态

本次匹配已运行配置的两份文件：

| 程序 | 版本绑定 | SHA-256 |
| --- | --- | --- |
| codex | 0.156.1 | `0b2e9301d6100dddda3b9d5c80ebaeaa3a2f1962388f2f36f6b96a9f08b1f33f` |
| codex-code-mode-host | matching companion | `45ba654b0e145406f3316be4729b886980ce811aa2e3b6ed4e033dd41ab524f1` |

两者合计 355,083,512 字节（338.63 MiB），为 Linux x86-64 static PIE ELF。源码包不包括这两个程序，也不包括作者的 Codex home、认证、会话或模型轨迹。

我们尚未从现有材料确认这两个**精确二进制**的公开下载地址、源码构建 revision 和对应再分发条款，因此不宣称可以用任意 stock Codex 安装替代。可选的单独 tools archive 仅用于交付明确列出的文件，须另附版本、hash、来源和许可信息；当前源码交付不表示已经生成该 archive，也不表示其再分发许可已核实。

仓库未附统一 LICENSE；题包保留各上游资产许可，release README 明确不授予新的统一数据集许可。这些是当前文档与证据的缺口，不应由“文件已可下载”推导授权结论。

## 验证与实验记录

`doctor` 的职责是本地配置/文件/二进制 hash 检查，不调用 Docker 或模型 API。实际 provider、工具与环境是否工作由对应配置下的 smoke 和小批量运行确认。最终交付测试范围及结果以随包测试记录为准，历史远端 campaign 成功不能替代当前独立包的验证。

模型应只读完整 Oracle spec 或 CoWork 公开任务信息，并通过任务 MCP 修改代码。标准答案、私有测试及 evaluator 留在可信控制侧。保存 release/task/runtime/harness/binary 身份、实验条件、模型、effort、预算和结束原因；保留完整轨迹、补丁、评分与 cleanup 证据。

离线检查覆盖逐题 runtime 分派、Goal 协议及运行目录迁移；具体结果见仓库根目录 `VALIDATION.json`。
