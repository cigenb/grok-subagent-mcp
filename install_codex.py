import argparse
import json
import shutil
import sys
import time
import tomllib
import uuid
from pathlib import Path

from storage import data_root


def hook_config():
    result = {}
    for event in ("PostToolUse", "UserPromptSubmit", "Stop"):
        inputs = {"event": event, "source_id": "${session_id}"}
        result[event] = [{"hooks": [{"type": "mcp_tool", "server": "grok", "tool": "grok_collect",
                                     "input": inputs, "timeout": 5}]}]
    return result


def install(home):
    root = Path(__file__).resolve().parent
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.toml"
    hooks_file = home / "hooks.json"
    existing = tomllib.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    if "grok" in existing.get("mcp_servers", {}):
        settings = existing["mcp_servers"]["grok"]
        if settings.get("args") != [str(root / "server.py"), "--client", "codex"]:
            raise ValueError("An unrelated MCP server already uses the name grok")
    hooks = json.loads(hooks_file.read_text(encoding="utf-8")) if hooks_file.exists() else {"hooks": {}}
    addition = hook_config()
    for event, groups in addition.items():
        current = hooks.setdefault("hooks", {}).setdefault(event, [])
        for group in current:
            for handler in group.get("hooks", []):
                if handler.get("server") == "grok" and handler.get("tool") == "grok_collect" and group not in groups:
                    raise ValueError(f"A different Grok hook already exists for {event}; inspect it before replacing")
        for group in groups:
            if group not in current:
                current.append(group)
    for client in ("codex", "claude"):
        (data_root() / client).mkdir(parents=True, exist_ok=True)
    backup = data_root() / "codex" / "backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, exist_ok=False)
    for path in (config, hooks_file):
        if path.exists():
            shutil.copy2(path, backup / path.name)
    if "grok" not in existing.get("mcp_servers", {}):
        with config.open("a", encoding="utf-8") as stream:
            stream.write("\n[mcp_servers.grok]\n")
            stream.write("command = " + json.dumps(sys.executable) + "\n")
            stream.write("args = " + json.dumps([str(root / "server.py"), "--client", "codex"]) + "\n")
            stream.write("startup_timeout_sec = 15\ntool_timeout_sec = 75\n")
    hooks_file.write_text(json.dumps(hooks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tomllib.loads(config.read_text(encoding="utf-8"))
    json.loads(hooks_file.read_text(encoding="utf-8"))
    return backup


def main():
    parser = argparse.ArgumentParser(description="Install the Grok MCP server and Codex MCP hooks; back up existing configuration")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    args = parser.parse_args()
    print("Backup:", install(args.codex_home))
    print("Open /hooks in Codex and trust the three Grok hook definitions. New sessions load the MCP server.")


if __name__ == "__main__":
    main()
