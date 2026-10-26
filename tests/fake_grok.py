import json
import sys
import threading
import time
import uuid
from pathlib import Path

lock = threading.Lock()
sessions = {}
models = {}
jobs = {}


def send(message):
    with lock:
        sys.stdout.buffer.write((json.dumps(message) + "\n").encode())
        sys.stdout.buffer.flush()


def handle(m):
    p = m.get("params", {})
    method = m["method"]
    if len(sys.argv) > 1:
        with lock, open(sys.argv[1], "a", encoding="utf-8") as log:
            log.write(json.dumps({"method": method, "params": p}) + "\n")
    sid = p.get("sessionId")
    result = {}
    if method == "initialize":
        result = {"authMethods": [{"id": "cached_token"}]}
    elif method == "session/new":
        sid = str(uuid.uuid4())
        sessions[sid] = threading.Event()
        result = {"sessionId": sid}
    elif method == "session/load":
        sessions[sid] = threading.Event()
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
              "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "OLD_REPLAY_OUTPUT"}}}})
    elif method == "session/set_config_option":
        models.setdefault(sid, {})[p["configId"]] = p["value"]
        if p["value"] == "bad-model":
            send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32602, "message": "unknown model"}})
            return
        result = {"configOptions": [{"id": k, "currentValue": v} for k, v in models[sid].items()]}
    elif method == "session/prompt":
        text = p["prompt"][0]["text"]
        if "background-task" in text:
            jobs[sid] = {"task_id": "native-background-id", "completed": False, "deadline": time.monotonic() + 5}
        if "crash-with-child" in text:
            import os
            import subprocess
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
            Path(sys.argv[1]).with_suffix(".child").write_text(str(child.pid))
            os._exit(2)
        if "crash-now" in text:
            import os
            os._exit(2)
        if "slow-task" in text:
            sessions[sid].wait(1.0)
        if "artifact-task" in text:
            cwd = text.split("Working directory: ", 1)[1].splitlines()[0]
            lines = f"\nGROK_MCP_ARTIFACT: {cwd}\\煤炭\\结果.json\n`GROK_MCP_ARTIFACT: relative\\out.txt`\n"
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
                  "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": lines}}}})
        if "child-task" in text:
            child_id = str(uuid.uuid4())
            for kind, status in (("subagent_spawned", "running"), ("subagent_finished", "completed")):
                send({"jsonrpc": "2.0", "method": "_x.ai/session_notification", "params": {"sessionId": sid,
                      "update": {"sessionUpdate": kind, "subagent_id": child_id, "child_session_id": child_id,
                                 "parent_session_id": sid, "status": status}}})
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
              "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "中文结果：" + json.dumps(models[sid])}}}})
        result = {"stopReason": "cancelled" if sessions[sid].is_set() else "end_turn"}
    elif method == "session/cancel":
        sessions[sid].set()
    elif method == "_x.ai/subagent/list_running":
        result = {"result": {"subagents": []}}
    elif method == "_x.ai/task/list":
        job = jobs.get(sid)
        if job and time.monotonic() > job["deadline"]:
            job["completed"] = True
        result = {"result": {"tasks": [job] if job else []}}
    elif method == "_x.ai/task/kill":
        assert p["taskId"] == jobs[sid]["task_id"]
        jobs[sid]["completed"] = True
        result = {"result": {"taskId": p["taskId"], "outcome": "killed"}}
    elif method == "_x.ai/interject":
        result = {"result": {"status": "queued"}}
    if "id" in m:
        send({"jsonrpc": "2.0", "id": m["id"], "result": result})


for line in sys.stdin.buffer:
    threading.Thread(target=handle, args=(json.loads(line),), daemon=True).start()
