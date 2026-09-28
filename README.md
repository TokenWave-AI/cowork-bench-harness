# CoWork-Bench evaluation harnesses

CoWork-Bench 的独立答题与评测入口，包含 **Codex** 和 **Claude Code** 两套 harness。

题包的 runtime 提供答题容器、任务工具、工作场所模拟和评分。这里的 harness 负责启动模型、连接任务 MCP、维护 Goal 与 8 小时预算、控制并发，并收集轨迹、补丁、评分及用量。

| Harness | 实现 | 源码与说明 |
| --- | --- | --- |
| Codex | app-server JSON-RPC + Goal | [codex/](codex/README.md) |
| Claude Code | 原生 CLI stream-json + Goal MCP | [claude-code/](claude-code/README.md) |

## 使用

两套代码均可单独复制使用。以 Codex 为例：

```bash
cd codex
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
# 编辑 config.json：题包路径、binary、模型、URL、凭证环境变量名称、并发等。
.venv/bin/python harness.py prepare --config config.json --output /data2/cowork-runs/campaign
.venv/bin/python harness.py doctor --root /data2/cowork-runs/campaign
.venv/bin/python harness.py smoke --root /data2/cowork-runs/campaign --condition cowork --row 44
.venv/bin/python harness.py run --root /data2/cowork-runs/campaign
.venv/bin/python harness.py status --root /data2/cowork-runs/campaign
```

Claude Code 在 `claude-code/` 下使用相同入口，另需配置现有普通用户和同事服务的 Responses 地址。完整字段见各自 `docs/CONFIGURATION.md`。

默认运行 CoWork、每题 8 小时。支持 `cowork`、`oracle` 或两者；两者都选时先 CoWork 后 Oracle。Oracle 使用完整 spec 和代码工具。模型、URL、凭证引用、并发及选题均通过配置文件指定；更改实验设置后请创建新的输出目录并重新 smoke。

## 环境与交付范围

- 配套题包：`cowork-bench-release-101-public-20260928-64-slim`，通过题包 registry 选择逐题 runtime。
- Linux/amd64、Python 3.10+、Docker，以及各 harness 文档指定的原生 CLI；部分 broker 任务还有题包 README 所列平台要求。
- Codex 配对版本为 0.156.1，含 `codex-code-mode-host`；Claude Code 为 2.1.207。程序 hash 和来源状态见各自文档。源码包不附原生程序，不保证任意 CLI 版本可替换。
- 仓库不含题包、镜像层、真实凭证、历史运行记录。任务镜像按题包 manifest 从 Harbor 拉取。
- 运行输出使用 `--output` 指定的数据盘目录；Docker 镜像和容器层由 daemon 的 data-root 决定。

## 验证

当前独立封装共通过 36 项离线测试：Codex 14 项、Claude Code 22 项（本次更新重跑 Claude，保留未改动 Codex 的既有结果），详见 [VALIDATION.json](VALIDATION.json)。覆盖 Goal 协议、逐题 runtime、路径迁移、条件绑定和工具隔离。这些测试未调用模型 API 或 Docker；接收方需对自己的实际配置完成 `doctor` 和 `smoke` 后再正式评测。

两个目录各有 `tests/README.md`。文件完整性可通过仓库根目录的 `sha256sum -c FILES.sha256` 检查。

2026-09-29 更新：兼容 Claude Code 2.1.207 对禁用 ToolSearch 返回的明确短错误，避免将已成功拒绝的调用误判为隔离失败而中止任务；同时保留首次错误原因。工具权限保持原设置。
