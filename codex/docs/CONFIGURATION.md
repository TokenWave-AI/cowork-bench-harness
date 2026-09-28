# Codex 配置

以随包 `config.example.json` 为准。下面是最小字段示意；`RELEASE_ROOT`、`CODEX_BINARY_PATH`、`CODE_MODE_HOST_BINARY_PATH`、`PROVIDER_BASE_URL`、`MODEL_NAME` 都是需要替换的 JSON 字符串，不是自动展开的环境变量。

```json
{
  "harness": "codex",
  "release_root": "RELEASE_ROOT",
  "model": "MODEL_NAME",
  "effort": "max",
  "provider": {
    "base_url": "PROVIDER_BASE_URL",
    "api_key_env": "COWORK_SOLVER_API_KEY"
  },
  "colleague": {
    "inherit": true,
    "max_output_tokens": 32768
  },
  "binaries": {
    "codex": {
      "path": "CODEX_BINARY_PATH",
      "sha256": "0b2e9301d6100dddda3b9d5c80ebaeaa3a2f1962388f2f36f6b96a9f08b1f33f"
    },
    "code_mode_host": {
      "path": "CODE_MODE_HOST_BINARY_PATH",
      "sha256": "45ba654b0e145406f3316be4729b886980ce811aa2e3b6ed4e033dd41ab524f1"
    }
  },
  "conditions": ["cowork", "oracle"],
  "rows": [44],
  "concurrency": 1,
  "budget_seconds": 28800,
  "max_transport_retries": 20,
  "docker_host": "unix:///var/run/docker.sock"
}
```

| 字段 | 含义 |
| --- | --- |
| `release_root` | 不可变题包绝对路径，包含 manifest、任务资产和逐题 runtime registry |
| `model` / `effort` | solver 模型原名及 reasoning effort；provider 必须支持所选组合 |
| `provider.base_url` | Responses 协议地址，使用服务商实际给出的 API base URL |
| `provider.api_key_env` | 执行 harness 的进程中已设置的环境变量名称；JSON 不含秘密值 |
| `provider.api_key_file` | 可替代环境变量引用，指向外部 mode-0600 文件；不放到题包或模型可读目录 |
| `colleague.inherit` | CoWork 同事配置按实现继承 solver；若同事服务不同，显式填写其 URL/模型及凭证引用 |
| `colleague.max_output_tokens` | 同事单次输出 token 上限，默认 32768；不是 solver 总 token 预算 |
| `binaries` | 两个匹配的 native 程序绝对路径与 SHA-256；均需可执行 |
| `conditions` | 选择 `cowork`、`oracle`；两者都选时先 CoWork 后 Oracle |
| `rows` | 此次题号集合，使用 manifest 中真实存在的题；首次建议 `[44]` |
| `concurrency` | 模型任务并发，首次用 1，通过 smoke 后按 CPU、内存、盘和 API 进度增加 |
| `budget_seconds` | 每题模型阶段的墙钟预算，默认 28800，包含重试及等待 |
| `max_transport_retries` | 外层传输恢复默认 20；native driver 也有独立重试策略，不能按重试次数简单换算成耗时 |
| `docker_host` | Docker endpoint；配套 broker 任务通常要求本地 Unix socket |

如使用凭证文件，将 `api_key_env` 改为 `api_key_file`，例如 `/data2/cowork-secrets/solver.key`，并保证权限为 0600。不要在两处填写原始 key，也不要将认证信息拼进 URL。独立同事服务同样通过环境变量名称或外部文件配置；Oracle 不启动同事服务。

Codex 两程序的冻结版本配对为 **0.156.1**：

| 程序 | 字节数 | 建议安装路径 |
| --- | ---: | --- |
| codex | 284,479,848 | `/opt/cowork-tools/codex` |
| codex-code-mode-host | 70,603,664 | `/opt/cowork-tools/codex-code-mode-host` |

外部机器上把 `path` 改成实际安装文件。建议同目录命名为 `codex` 和 `codex-code-mode-host`，并保持这对程序匹配；不能仅修改 hash 来声明未知程序兼容。驱动使用 Goal app-server RPC，同版本号但不同分发来源的程序需要独立兼容验证。本包没有验证过的官方下载 URL，不提供推测安装命令。

运行数据与 Docker 存储位置是两项配置：`--output` 决定 harness 状态、轨迹和日志路径；镜像/容器层仍由 daemon 的 data-root 决定。普通独立 harness 无需 SSH、systemd 或旧模板，broker 题的平台条件按题包自身要求保留。

修改模型、provider、预算、release 或 binary 后使用新的输出目录，并重新做当前配置的 smoke。不要把旧 smoke 和旧补丁/评分混进新实验。
