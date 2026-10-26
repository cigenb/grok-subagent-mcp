# grok-subagent-for-codex

[English](README.md) | 简体中文

通过标准 stdio MCP 调用本机 Grok CLI。支持任务派发、续问、进度查询、中途追加指令和中断。
支持 Codex 与 Claude；仓库提供 Codex 接入，Claude hook 由调用方配置。

## 环境

- Windows、Python 3.11+、uv、已安装并登录的 Grok CLI。
- 无第三方 Python 依赖，使用 `uv run` 创建项目环境。
- Grok 需支持 ACP 的会话配置、关闭会话及 `x.ai/task`、`x.ai/subagent` 扩展。

## 接入 Codex

```powershell
git clone https://github.com/cigenb/grok-subagent-for-codex.git
cd grok-subagent-for-codex
uv run python install_codex.py
```

安装程序备份已有配置后，在用户级 `config.toml` 中注册 `grok`，向 `hooks.json` 合并三条 MCP 工具 hook。
在 Codex CLI 的 `/hooks` 中检查并信任这些定义，新会话加载 MCP。

`PostToolUse` 和 `UserPromptSubmit` 收取结果；`Stop` 检查未完成任务。
hook 不会唤醒已结束的空闲回合；未领取结果保存在任务库，下一次继续聊天时可取。

其他 MCP 客户端使用相同服务：

```powershell
uv run --directory "<安装目录>" python server.py --client claude
```

其他客户端可用 `--owner <来源聊天ID>`，或在工具参数传 `source_id`，以便服务重启后仍按原聊天查询。
未指定时按本次 MCP 连接隔离。Codex 自动使用调用元数据中的 `threadId`。

## 工具

| 工具 | 用途 |
|---|---|
| `grok_spawn` | 派发任务，必填 `prompt`、`scope`；`wait_s` 等待最多 60 秒；`session_id` 续问 |
| `grok_wait` | 等待本聊天的任务，超时或取消等待不会停止执行 |
| `grok_status` | 列任务，或用 `ids` 查询详情；`full=true` 返回全文 |
| `grok_steer` | 给运行中的任务追加指令，在后续处理时生效 |
| `grok_interrupt` | 中断任务和关联子代理，保留已有输出及文件 |
| `grok_collect` | 客户端 hook 接口，输出对应事件的 hook JSON |

默认使用普通 `grok-4.7`、思考程度 `high`；可显式传 `model`、`effort`，续问未传时沿用最近一次成功应用的设置。
配置独立于 Grok 交互式会话的默认模型。进程关闭跨会话记忆，保留会话续问，允许 Grok 使用子代理。

Grok 运行时关闭沙箱并自动批准工具。每次调用的 `scope` 必须说明可读/可改路径、禁止动作及验收要求。
`scope` 是传给模型的任务指令，不是文件系统访问控制；调用方需核对产物和测试。终端命令造成的文件变化应由 Grok 在交付清单中报告。

同一 Grok 会话只能有一个执行中的主任务，续问保留原工作目录。任务结束后关闭原生会话，续问时重新加载历史。
不自动创建 worktree；Grok 子代理产生的 worktree 路径作为产物保留。
MCP 连接关闭时停止它的 Grok 进程树；重启服务后可读取已有输出或续问已登记的会话。

## 数据与清理

运行数据位于 `~/.grok/cigenb-grok-mcp/codex/` 或 `claude/`：

- `tasks.sqlite3`：来源聊天、主会话和子会话、状态、领取状态及产物路径。
- `tasks/<任务ID>/output.txt`：助手输出，不收集思考文本。
- `tasks/<任务ID>/scratch/`：任务临时文件。
- `logs/`：各 Grok 进程实例的诊断日志，多任务可以引用同一日志。
- `backups/`：接入前的 Codex 配置备份。

Grok 原生历史仍保存在 `~/.grok/sessions/`，通过会话 ID 关联。
共享缓存、全局日志、项目交付物和 worktree 不随任务历史清理。

```powershell
uv run python manage.py --client codex list
uv run python manage.py --client codex inspect <任务ID>
uv run python manage.py --client codex keep <任务ID>
uv run python manage.py --client codex cleanup-plan --days 7 --out cleanup-plan.json
```

默认候选为已结束、结果已领取、七天未续问的任务；也可通过 `--ids` 指定任务。
同一原生会话的多个任务一起检查。运行中、未领取和保留中的任务不可删除。
清理计划列出具体文件、摘要及保留的产物。检查清单并取得删除确认后执行：

```powershell
uv run python manage.py --client codex cleanup cleanup-plan.json --confirm <清单中的confirmation>
```

清理会调用 `grok sessions delete` 删除登记的原生主会话和子会话；该命令也可能删除对应远端历史。
清单生成后任务或文件有变化时拒绝执行，需要重新生成清单。
进程日志在所有引用任务均已清理或进入本次清单、且写日志的 MCP 进程已退出时进入清理候选。
配置备份独立保留。清理后任务库保留来源、会话 ID 和产物路径，移除任务正文与边界文本。

`GROK_MCP_BIN` 指定 Grok 可执行文件；`GROK_MCP_DATA_DIR` 指定 MCP 数据根目录，主要用于隔离测试。

## 验证

```powershell
uv run python -m unittest discover -s tests -v
uv run python tests/smoke.py
```

第二条会真实调用 Grok，在系统临时目录生成测试文件，并产生原生测试会话。

## 卸载 Codex 接入

运行 `codex mcp remove grok`，再从 `~/.codex/hooks.json` 移除 `server` 为 `grok`、`tool` 为 `grok_collect` 的三条 hook。任务数据和 Grok 原生历史独立保留。

## License

[MIT](LICENSE)
