import json
import os
import queue
import shutil
import subprocess
import threading
from contextlib import suppress
from pathlib import Path

from processes import ProcessTree, popen_options


def executable():
    override = os.environ.get("GROK_MCP_BIN")
    found = override or shutil.which("grok")
    if not found:
        raise RuntimeError("Grok CLI not found; install and log in to Grok first")
    return found


class Grok:
    def __init__(self, log_path, on_event, command=None):
        self.log_path = Path(log_path)
        self.on_event = on_event
        self.command = command
        self.start_lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.lock = threading.Lock()
        self.pending = {}
        self.sequence = 0
        self.proc = None
        self.reader = None
        self.tree = None
        self.log = None
        self.requests = queue.Queue()
        threading.Thread(target=self._answer_requests, daemon=True).start()

    def start(self):
        with self.start_lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            if self.proc is not None:
                self.close()
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log = self.log_path.open("ab")
            cmd = self.command or [executable(), "--sandbox", "off", "agent", "--no-leader",
                                   "--always-approve", "--model", "grok-4.7", "--reasoning-effort", "high", "stdio"]
            env = dict(os.environ, GROK_MEMORY="0", GROK_SUBAGENTS="1")
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=self.log, env=env, **popen_options())
                self.tree = ProcessTree(self.proc)
                self.reader = threading.Thread(target=self._reader, args=(self.proc,), daemon=True)
                self.reader.start()
                threading.Thread(target=self._watch, args=(self.proc,), daemon=True).start()
                init = self.request("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                                   "clientInfo": {"name": "grok-mcp", "version": "0.2.1"}})
                methods = [m["id"] for m in init.get("authMethods", [])]
                method = "cached_token" if "cached_token" in methods else "xai.api_key"
                if method not in methods:
                    raise RuntimeError("No noninteractive authentication available; run grok login")
                self.request("authenticate", {"methodId": method, "_meta": {"headless": True}})
            except Exception:
                self.close()
                raise

    def send(self, method, params, request_id=None, proc=None):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        self._write(message, proc)

    def _write(self, message, proc=None):
        with self.write_lock:
            target = proc or self.proc
            if target is None or target is not self.proc or target.poll() is not None:
                raise RuntimeError("Grok process is not running")
            target.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            target.stdin.flush()

    def request(self, method, params, timeout=45, sent=None):
        with self.lock:
            self.sequence += 1
            rid = self.sequence
            proc = self.proc
            result = queue.Queue(maxsize=1)
            self.pending[rid] = (proc, result)
        try:
            self.send(method, params, rid, proc)
            if sent is not None:
                sent.set()
            try:
                response = result.get(timeout=timeout)
            except queue.Empty as exc:
                raise TimeoutError(f"{method} timed out after {timeout}s") from exc
            if "error" in response:
                raise RuntimeError(f"{method}: {response['error']}")
            return response.get("result", {})
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def extension(self, name, params):
        envelope = self.request("_x.ai/" + name, params)
        if envelope.get("error"):
            raise RuntimeError(str(envelope["error"]))
        return envelope.get("result", envelope)

    def _reader(self, proc):
        try:
            for raw in proc.stdout:
                message = json.loads(raw)
                if "method" not in message:
                    with self.lock:
                        pending = self.pending.get(message.get("id"))
                        if pending is not None and pending[0] is proc and pending[1].empty():
                            pending[1].put_nowait(message)
                elif "id" in message:
                    self.requests.put((proc, message))
                else:
                    self.on_event(message)
        except Exception as exc:
            self._fail(proc, f"Grok output reader failed: {exc}")
            if proc.poll() is None:
                proc.kill()
        finally:
            self._fail(proc, "Grok process exited; inspect " + str(self.log_path))
            # Reap on EOF even if no task is making a request; descendants may still be alive.
            threading.Thread(target=self.close, args=(proc,), daemon=True).start()

    def _fail(self, proc, error):
        with self.lock:
            for owner, pending in self.pending.values():
                if owner is proc and pending.empty():
                    pending.put_nowait({"error": error})

    def _watch(self, proc):
        # A descendant can inherit stdout, so its parent exiting need not produce pipe EOF.
        proc.wait()
        self._fail(proc, "Grok process exited; inspect " + str(self.log_path))
        self.close(expected=proc)

    def _answer_requests(self):
        while True:
            entry = self.requests.get()
            if entry is None:
                return
            proc, message = entry
            if proc is not self.proc:
                continue
            if message["method"] == "session/request_permission":
                options = message.get("params", {}).get("options", [])
                allow = next((o for o in options if o.get("kind") == "allow_once"), None)
                outcome = {"outcome": "selected", "optionId": allow["optionId"]} if allow else {"outcome": "cancelled"}
                response = {"result": {"outcome": outcome}}
            else:
                response = {"error": {"code": -32601, "message": "No interactive input is available; report the blocker"}}
            try:
                self._write({"jsonrpc": "2.0", "id": message["id"], **response}, proc)
            except (OSError, RuntimeError):
                pass

    def configure(self, session_id, model="grok-4.7", effort="high"):
        for key, value in (("model", model), ("reasoning_effort", effort)):
            result = self.request("session/set_config_option", {"sessionId": session_id,
                                                                 "configId": key, "value": value})
            actual = {o["id"]: o.get("currentValue") for o in result.get("configOptions", [])}
            if actual.get(key) != value:
                raise RuntimeError(f"Grok did not apply {key}={value}: {actual}")

    def close(self, expected=None):
        with self.start_lock:
            if expected is not None and self.proc is not expected:
                return
            proc, tree = self.proc, self.tree
            self.proc, self.tree = None, None
            try:
                if tree:
                    tree.close()
                elif proc and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
            finally:
                if proc:
                    self._fail(proc, "Grok process exited; inspect " + str(self.log_path))
                    if self.reader and self.reader is not threading.current_thread():
                        self.reader.join(timeout=5)
                    # Closing a buffered stdin flushes it; a dead Windows pipe raises EINVAL here.
                    for stream in (proc.stdin, proc.stdout):
                        with suppress(OSError):
                            stream.close()
                if self.log:
                    self.log.close()
                    self.log = None
