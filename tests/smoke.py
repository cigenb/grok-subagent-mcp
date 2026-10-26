import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from storage import Store


class Client:
    def __init__(self, data, owner="smoke-owner", client="codex"):
        self.owner = owner
        self.seq = 0
        self.responses = {}
        self.lock = threading.Lock()
        self.p = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--client", client],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=dict(os.environ, GROK_MCP_DATA_DIR=str(data)))
        self.stderr = []
        threading.Thread(target=self.reader, daemon=True).start()
        threading.Thread(target=lambda: self.stderr.extend(self.p.stderr.read().decode("utf-8", "replace").splitlines()), daemon=True).start()
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "smoke", "version": "0"}})

    def reader(self):
        for line in self.p.stdout:
            message = json.loads(line)
            if "id" in message:
                with self.lock:
                    self.responses[message["id"]] = message

    def send(self, method, params):
        self.seq += 1
        self.p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params}) + "\n").encode())
        self.p.stdin.flush()
        return self.seq

    def get(self, rid, timeout=80):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            with self.lock:
                message = self.responses.pop(rid, None)
            if message:
                if "error" in message:
                    raise AssertionError(message)
                return message["result"]
            if self.p.poll() is not None:
                raise AssertionError(self.stderr)
            time.sleep(.05)
        raise TimeoutError(rid)

    def rpc(self, method, params):
        return self.get(self.send(method, params))

    def call(self, name, **args):
        result = self.rpc("tools/call", {"name": name, "arguments": args, "_meta": {"threadId": self.owner}})
        if result.get("isError"):
            raise AssertionError(result)
        return result["content"][0]["text"]

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=20)
        self.p.stdout.close()
        self.p.stderr.close()
        if self.p.returncode:
            raise AssertionError(self.stderr)


def main():
    root = Path(tempfile.mkdtemp(prefix="grok-mcp-smoke-"))
    data = root / "data"
    (root / "marker.txt").write_text("GROK_CHILD_7531", encoding="utf-8")
    print("Test directory:", root, flush=True)
    store = Store("codex", data)
    client = Client(data)
    scope = (f"Only read and write inside {root}. No browsing, external MCP, settings changes, commits or deletion. "
             "Subagents inherit the same scope. Verify file contents before reporting success.")
    try:
        client.call("grok_spawn", prompt="Remember TOKEN_8921. Reply with TOKEN_SAVED only.", scope=scope, cwd=str(root), wait_s=60)
        first = store.list()[0]
        assert first["status"] == "completed", first
        assert first["model"] == "grok-4.7" and first["effort"] == "high"
        sid = first["session_id"]
        print("PASS: authenticated MCP call, default model/effort, output", flush=True)
        client.close()
        client = Client(data)
        text = client.call("grok_spawn", prompt="Return the TOKEN_ number I asked you to remember, without tools.",
                           scope=scope, session_id=sid, wait_s=60)
        assert store.list()[0]["status"] == "completed", store.list()[0]
        assert "TOKEN_SAVED" not in text, text
        assert "TOKEN_8921" in text, text
        print("PASS: resume after MCP and Grok process restart", flush=True)
        text = client.call("grok_spawn", prompt=("Spawn one subagent to read marker.txt and return its contents. "
            "Collect its result. Then use a terminal command to write proof.txt containing GROK_WRITE_OK. "
            "Verify its contents and report LIVE_CHILD_OK plus the marker. Include proof.txt in the artifact list."),
            scope=scope, cwd=str(root), wait_s=60)
        task = store.list()[0]
        while task["status"] in ("starting", "running"):
            text = client.call("grok_wait", ids=[task["id"]], timeout=60)
            task = store.get(task["id"])
        assert task["status"] == "completed", task
        assert "LIVE_CHILD_OK" in text and "GROK_CHILD_7531" in text, text
        assert (root / "proof.txt").read_text(encoding="utf-8").strip() == "GROK_WRITE_OK"
        assert task["children"] and task["children"][0]["child_session_id"], task
        assert str(root / "proof.txt") in task["artifacts"], task
        print("PASS: native subagent tracking, shell execution, deliverable manifest", flush=True)
        client.call("grok_spawn", prompt="Print numbers 1 through 10000, one per line. No tools.", scope=scope, cwd=str(root))
        task = store.list()[0]
        until = time.monotonic() + 45
        while not store.output(task) and time.monotonic() < until:
            time.sleep(.2)
            task = store.get(task["id"])
        text = client.call("grok_interrupt", id=task["id"])
        assert store.get(task["id"])["status"] == "interrupted", text
        print("PASS: native cancellation", flush=True)
    finally:
        client.close()
        store.close()


if __name__ == "__main__":
    main()
