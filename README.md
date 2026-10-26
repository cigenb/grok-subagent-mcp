# grok-subagent-for-codex

English | [简体中文](README.zh-CN.md)

Use the local Grok CLI as a subagent through a standard stdio MCP server. Dispatch tasks, resume conversations, inspect progress, send follow-up instructions, and interrupt work.

The server supports Codex and Claude. This repository includes Codex integration; Claude hook integration is configured by the caller.

## Requirements

- Windows, Python 3.11+, uv, and an installed, authenticated Grok CLI.
- No third-party Python dependencies. `uv run` creates the project environment.
- Grok must support ACP session configuration and closing, and the `x.ai/task` and `x.ai/subagent` extensions.

## Install for Codex

```powershell
git clone https://github.com/cigenb/grok-subagent-for-codex.git
cd grok-subagent-for-codex
uv run python install_codex.py
```

The installer backs up existing configuration, registers the `grok` MCP server in the user-level `config.toml`, and merges three MCP tool hooks into `hooks.json`.
Review and trust these definitions through `/hooks` in the Codex CLI, then start a new chat to load the server.

`PostToolUse` and `UserPromptSubmit` collect results; `Stop` checks for unfinished tasks.
Hooks do not wake an idle chat after its turn has ended. Uncollected results remain in the task database and can be retrieved when the conversation continues.

Other MCP clients can use the same server with this stdio command:

```powershell
uv run --directory "<install-dir>" python server.py --client claude
```

Other clients can supply `--owner <source-conversation-id>` or the `source_id` tool argument to preserve ownership across server restarts.
Without either, tasks are isolated to that MCP connection. Codex uses the `threadId` supplied in call metadata.

## Tools

| Tool | Purpose |
|---|---|
| `grok_spawn` | Dispatch a task with required `prompt` and `scope`; `wait_s` waits up to 60 seconds; `session_id` resumes a conversation |
| `grok_wait` | Wait for this conversation's tasks; timing out or cancelling the wait does not stop execution |
| `grok_status` | List tasks, or inspect `ids`; `full=true` returns the complete output |
| `grok_steer` | Send instructions to a running task, applied during subsequent processing |
| `grok_interrupt` | Interrupt a task and its subagents, preserving output and files already produced |
| `grok_collect` | Client hook interface that returns event-specific hook JSON |

The default model is regular `grok-4.7` with reasoning effort `high`. Explicit `model` and `effort` arguments override these values; a resumed conversation inherits the last successfully applied settings when they are omitted.
These settings are independent of the interactive Grok defaults. The process disables cross-session memory, retains conversation resume, and allows Grok to use its own subagents.

Grok runs with its sandbox off and tool execution automatically approved. Every call's `scope` must describe allowed read/write paths, prohibited actions, and acceptance criteria.
`scope` is an instruction to the model, not filesystem access control. The caller must verify artifacts and tests. Grok is instructed to report files created or changed by terminal commands in its deliverable list.

A Grok conversation can have only one active main task. Resuming retains its working directory. Each task closes its native session after finishing; resuming loads the saved history.
The bridge does not automatically create worktrees. Paths returned for Grok subagent worktrees are retained as artifacts.
Closing the MCP connection stops its Grok process tree. After restarting the server, callers can read saved output or resume registered conversations.

## Data and cleanup

Runtime data lives under `~/.grok/cigenb-grok-mcp/codex/` or `claude/`:

- `tasks.sqlite3`: source conversations, main and child session IDs, status, delivery state, and artifact paths.
- `tasks/<task-id>/output.txt`: assistant output, excluding thinking text.
- `tasks/<task-id>/scratch/`: task temporary files.
- `logs/`: per-process Grok diagnostics; several tasks may reference the same log.
- `backups/`: Codex configuration backups made before installation.

Grok's native history remains in `~/.grok/sessions/`, associated by session ID.
Shared caches, global logs, project deliverables, and worktrees are not removed with task history.

```powershell
uv run python manage.py --client codex list
uv run python manage.py --client codex inspect <task-id>
uv run python manage.py --client codex keep <task-id>
uv run python manage.py --client codex cleanup-plan --days 7 --out cleanup-plan.json
```

Default candidates are finished tasks whose results have been collected and whose conversation has not been used for seven days. Use `--ids` to select tasks explicitly.
Tasks sharing a native conversation are checked together. Active, unread, and retained tasks cannot be deleted.
The cleanup plan lists exact files, hashes, and artifacts to preserve. Review the list and obtain deletion confirmation before applying it:

```powershell
uv run python manage.py --client codex cleanup cleanup-plan.json --confirm <confirmation-from-plan>
```

Cleanup invokes `grok sessions delete` for registered main and child sessions; that command may also delete corresponding remote history.
If tasks or files change after the preview, cleanup refuses to proceed and requires a new plan.
Process logs become eligible only after their MCP process has exited and every referencing task is already cleaned or included in the plan.
Configuration backups are retained separately. After cleanup, the task database retains ownership, session IDs, and artifact paths, while removing task and scope text.

`GROK_MCP_BIN` selects the Grok executable. `GROK_MCP_DATA_DIR` overrides the MCP data root, primarily for isolated tests.

## Tests

```powershell
uv run python -m unittest discover -s tests -v
uv run python tests/smoke.py
```

The second command calls Grok for real, creates files in a system temporary directory, and produces native test sessions.

## Uninstall Codex integration

Run `codex mcp remove grok`, then remove the three handlers whose `server` is `grok` and `tool` is `grok_collect` from `~/.codex/hooks.json`. Task data and native Grok history are retained separately.

## License

[MIT](LICENSE)
