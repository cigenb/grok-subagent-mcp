import argparse
import json
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path

from storage import data_root

EVENTS = ("PostToolUse", "UserPromptSubmit", "Stop")


def server_args(client):
    return [str(Path(__file__).resolve().parent / "server.py"), "--client", client]


def hook_config(client):
    result = {}
    for event in EVENTS:
        inputs = {"event": event}
        # Claude tool calls carry no conversation ID, so hooks must resolve to the same
        # connection-level owner as the tool calls instead of passing the hook session ID.
        if client == "codex":
            inputs["source_id"] = "${session_id}"
        result[event] = [{"hooks": [{"type": "mcp_tool", "server": "grok", "tool": "grok_collect",
                                     "input": inputs, "timeout": 5}]}]
    return result


def merge_hooks(hooks, addition):
    for event, groups in addition.items():
        current = hooks.setdefault("hooks", {}).setdefault(event, [])
        for group in current:
            for handler in group.get("hooks", []):
                if handler.get("server") == "grok" and handler.get("tool") == "grok_collect" and group not in groups:
                    raise ValueError(f"A different Grok hook already exists for {event}; inspect it before replacing")
        for group in groups:
            if group not in current:
                current.append(group)
    return hooks


def backup(client, paths):
    for name in ("codex", "claude"):
        (data_root() / name).mkdir(parents=True, exist_ok=True)
    target = data_root() / client / "backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    target.mkdir(parents=True, exist_ok=False)
    for path in paths:
        if path.exists():
            shutil.copy2(path, target / path.name)
    return target


def install_codex(home):
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.toml"
    hooks_file = home / "hooks.json"
    existing = tomllib.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    if "grok" in existing.get("mcp_servers", {}):
        if existing["mcp_servers"]["grok"].get("args") != server_args("codex"):
            raise ValueError("An unrelated MCP server already uses the name grok")
    hooks = json.loads(hooks_file.read_text(encoding="utf-8")) if hooks_file.exists() else {"hooks": {}}
    merge_hooks(hooks, hook_config("codex"))
    saved = backup("codex", (config, hooks_file))
    if "grok" not in existing.get("mcp_servers", {}):
        with config.open("a", encoding="utf-8") as stream:
            stream.write("\n[mcp_servers.grok]\n")
            stream.write("command = " + json.dumps(sys.executable) + "\n")
            stream.write("args = " + json.dumps(server_args("codex")) + "\n")
            stream.write("startup_timeout_sec = 15\ntool_timeout_sec = 75\n")
    hooks_file.write_text(json.dumps(hooks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tomllib.loads(config.read_text(encoding="utf-8"))
    json.loads(hooks_file.read_text(encoding="utf-8"))
    return saved


def install_claude(home, state, claude=None):
    settings = home / "settings.json"
    registered = json.loads(state.read_text(encoding="utf-8")).get("mcpServers", {}) if state.exists() else {}
    if "grok" in registered and registered["grok"].get("args") != server_args("claude"):
        raise ValueError("An unrelated MCP server already uses the name grok")
    current = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    merge_hooks(current, hook_config("claude"))
    if "grok" not in registered:
        claude = claude or shutil.which("claude")
        if not claude:
            raise RuntimeError("Claude Code CLI not found")
    saved = backup("claude", (settings,))
    if "grok" not in registered:
        # ~/.claude.json holds live Claude state; register through the CLI instead of editing it.
        entry = {"type": "stdio", "command": sys.executable, "args": server_args("claude")}
        subprocess.run([claude, "mcp", "add-json", "--scope", "user", "grok", json.dumps(entry)],
                       stdin=subprocess.DEVNULL, check=True)
    home.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.loads(settings.read_text(encoding="utf-8"))
    return saved


def main():
    parser = argparse.ArgumentParser(description="Register the Grok MCP server and result hooks for a client; back up existing configuration")
    subs = parser.add_subparsers(dest="client", required=True)
    codex = subs.add_parser("codex")
    codex.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    claude = subs.add_parser("claude")
    claude.add_argument("--claude-home", type=Path, default=Path.home() / ".claude")
    args = parser.parse_args()
    if args.client == "codex":
        print("Backup:", install_codex(args.codex_home))
        print("Open /hooks in Codex and trust the three Grok hook definitions. New sessions load the MCP server.")
    else:
        print("Backup:", install_claude(args.claude_home, Path.home() / ".claude.json"))
        print("New Claude Code sessions load the MCP server and the three Grok hooks.")


if __name__ == "__main__":
    main()
