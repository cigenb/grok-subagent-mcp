import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from install import install_claude, install_codex
from manage import apply_plan, plan
from server import MCP, Service
from storage import Store
from processes import identity


class Output:
    def __init__(self):
        self.messages = []
        self.lock = threading.Lock()

    def write(self, data):
        with self.lock:
            self.messages.append(json.loads(data))

    def flush(self):
        pass

    def response(self, rid, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                found = next((m for m in self.messages if m.get("id") == rid), None)
            if found:
                return found["result"]
            time.sleep(0.01)
        raise TimeoutError(rid)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store("codex", self.root / "state")
        self.wire = self.root / "requests.jsonl"
        self.service = Service(self.store, [sys.executable, str(Path(__file__).with_name("fake_grok.py")), str(self.wire)])
        self.output = Output()
        self.mcp = MCP(self.service, output=self.output)
        self.seq = 0

    def tearDown(self):
        self.service.close()
        time.sleep(0.1)
        self.store.close()
        self.temp.cleanup()

    def call(self, name, args=None, owner="owner-a", wait=True):
        self.seq += 1
        rid = self.seq
        self.mcp.handle({"id": rid, "method": "tools/call", "params": {"name": name,
                         "arguments": args or {}, "_meta": {"threadId": owner}}})
        return self.output.response(rid) if wait else rid

    def spawn(self, text="small-task", **kwargs):
        result = self.call("grok_spawn", {"prompt": text, "scope": "Only temporary test files", "cwd": str(self.root), **kwargs})
        self.assertFalse(result.get("isError"), result)
        return self.store.list("owner-a")[0]["id"]

    def finish(self, tid):
        result = self.call("grok_wait", {"ids": [tid], "timeout": 5})
        self.assertFalse(result.get("isError"), result)
        return self.store.get(tid)

    def test_utf8_defaults_and_session_resume(self):
        tid = self.spawn(wait_s=5)
        first = self.store.get(tid)
        self.assertEqual(first["status"], "completed")
        self.assertIn("中文结果", self.store.output(first))
        self.assertIn('"model": "grok-4.7"', self.store.output(first))
        second_id = self.spawn("resume-task", session_id=first["session_id"], wait_s=5, effort="low")
        second = self.store.get(second_id)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(second["effort"], "low")
        self.assertEqual(second["status"], "completed")
        self.assertNotIn("OLD_REPLAY_OUTPUT", self.store.output(second))
        self.store.keep(tid, True)
        third = self.spawn("resume-after-retention-change", session_id=first["session_id"], wait_s=5)
        self.assertEqual(self.store.get(third)["effort"], "low")

    def test_owner_isolation_and_spoof_rejection(self):
        tid = self.spawn(wait_s=5)
        result = self.call("grok_status", {"ids": [tid]}, owner="owner-b")
        self.assertTrue(result["isError"])
        result = self.call("grok_status", {"source_id": "owner-a"}, owner="owner-b")
        self.assertTrue(result["isError"])
        sid = self.store.get(tid)["session_id"]
        result = self.call("grok_spawn", {"prompt": "x", "scope": "x", "session_id": sid}, owner="owner-b")
        self.assertTrue(result["isError"])

    def test_concurrent_first_launch_and_resume_lock(self):
        a = self.spawn("slow-task")
        b = self.spawn("slow-task")
        self.assertEqual(self.finish(a)["status"], "completed")
        self.assertEqual(self.finish(b)["status"], "completed")
        sid = self.store.get(a)["session_id"]
        c = self.spawn("slow-task", session_id=sid)
        result = self.call("grok_spawn", {"prompt": "x", "scope": "x", "session_id": sid})
        self.assertTrue(result["isError"])
        self.finish(c)

    def test_cancel_wait_keeps_result_unread_for_hook(self):
        tid = self.spawn("slow-task")
        rid = self.call("grok_wait", {"ids": [tid], "timeout": 5}, wait=False)
        self.mcp.handle({"method": "notifications/cancelled", "params": {"requestId": rid}})
        time.sleep(1.5)
        self.assertFalse(any(m.get("id") == rid for m in self.output.messages))
        self.assertFalse(self.store.get(tid)["delivered"])
        result = self.call("grok_collect", {"event": "PostToolUse", "source_id": "owner-a"})
        payload = json.loads(result["content"][0]["text"])
        self.assertIn(tid, payload["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.store.get(tid)["delivered"])

    def test_parallel_hooks_deliver_once_and_results_remain_readable(self):
        tid = self.spawn("small-task")
        time.sleep(0.8)
        args = {"event": "PostToolUse", "source_id": "owner-a"}
        ids = [self.call("grok_collect", args, wait=False) for _ in range(4)]
        results = [self.output.response(rid) for rid in ids]
        self.assertEqual(sum(tid in r["content"][0]["text"] for r in results), 1)
        self.assertIn("中文结果", self.call("grok_status", {"ids": [tid], "full": True})["content"][0]["text"])

    def test_interrupt_stops_task(self):
        tid = self.spawn("slow-task")
        deadline = time.monotonic() + 3
        while self.store.get(tid)["status"] == "starting" and time.monotonic() < deadline:
            time.sleep(.01)
        self.call("grok_interrupt", {"id": tid})
        self.assertEqual(self.store.get(tid)["status"], "interrupted")

    def test_child_completed_before_parent_is_registered(self):
        tid = self.spawn("child-task", wait_s=5)
        children = self.store.get(tid)["children"]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["status"], "completed")
        self.assertTrue(children[0]["child_session_id"])

    def test_running_background_command_is_not_treated_as_finished(self):
        tid = self.spawn("background-task")
        until = time.monotonic() + 3
        while not self.store.get(tid).get("background_tasks") and time.monotonic() < until:
            time.sleep(.02)
        self.assertEqual(self.store.get(tid)["status"], "running")
        self.call("grok_interrupt", {"id": tid})
        self.assertEqual(self.store.get(tid)["status"], "interrupted")
        requests = [json.loads(line) for line in self.wire.read_text().splitlines()]
        kill = next(r for r in requests if r["method"] == "_x.ai/task/kill")
        self.assertEqual(kill["params"]["taskId"], "native-background-id")

    def test_process_failure_ends_inflight_task(self):
        tid = self.spawn("crash-now")
        task = self.finish(tid)
        self.assertEqual(task["status"], "failed")
        self.assertIn("exited", task["error"])

    def test_dead_parent_reaps_child_without_service_restart(self):
        tid = self.spawn("crash-with-child")
        self.assertEqual(self.finish(tid)["status"], "failed")
        pid = int(self.wire.with_suffix(".child").read_text())
        until = time.monotonic() + 5
        while identity(pid) is not None and time.monotonic() < until:
            time.sleep(.05)
        self.assertIsNone(identity(pid))

    def test_cancel_before_prompt_closes_native_session(self):
        entered, proceed = threading.Event(), threading.Event()
        configure = self.service.grok.configure
        def delayed(*args):
            entered.set()
            proceed.wait(5)
            return configure(*args)
        with patch.object(self.service.grok, "configure", side_effect=delayed):
            tid = self.spawn()
            self.assertTrue(entered.wait(5))
            rid = self.call("grok_interrupt", {"id": tid}, wait=False)
            while self.store.get(tid)["status"] != "cancelling":
                time.sleep(.01)
            proceed.set()
            self.output.response(rid)
        self.assertEqual(self.store.get(tid)["status"], "interrupted")
        methods = [json.loads(line)["method"] for line in self.wire.read_text().splitlines()]
        self.assertIn("session/close", methods)
        self.assertNotIn("session/prompt", methods)

    def test_invalid_wait_has_no_side_effect(self):
        result = self.call("grok_spawn", {"prompt": "x", "scope": "x", "wait_s": -1})
        self.assertTrue(result["isError"])
        self.assertEqual(self.store.list(), [])

    def test_task_directory_failure_rolls_back_registration(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("test directory is read-only")):
            result = self.call("grok_spawn", {"prompt": "x", "scope": "x"})
        self.assertTrue(result["isError"])
        self.assertEqual(self.store.list(), [])

    def test_unread_or_retained_tasks_cannot_be_cleaned(self):
        tid = self.spawn("small-task")
        time.sleep(.8)
        with self.assertRaises(ValueError):
            plan(self.store, [tid])
        self.finish(tid)
        task = self.store.get(tid)
        task["keep"] = True
        self.store.save(task)
        with self.assertRaises(ValueError):
            plan(self.store, [tid])

    def test_cleanup_groups_turns_and_detects_changed_files(self):
        tid = self.spawn(wait_s=5)
        sid = self.store.get(tid)["session_id"]
        other = self.spawn(session_id=sid, wait_s=5)
        saved = plan(self.store, [tid])
        self.assertEqual(set(saved["task_ids"]), {tid, other})
        (self.store.path(tid) / "scratch" / "new.txt").write_text("new")
        with self.assertRaisesRegex(ValueError, "changed"):
            apply_plan(self.store, saved, saved["confirmation"])

    def test_process_identity_recovery_and_client_partition(self):
        task = self.store.create("old", "p", "s", str(self.root))
        task["birth"] = "a previous process with the same PID"
        self.store.save(task)
        self.store.recover()
        self.assertEqual(self.store.get(task["id"])["status"], "interrupted")
        other = Store("claude", self.root / "state")
        self.assertEqual(other.list(), [])
        other.close()

    def test_cleanup_plan_selects_tasks_by_session(self):
        import subprocess
        tid = self.spawn(wait_s=5)
        sid = self.store.get(tid)["session_id"]
        second = self.spawn(session_id=sid, wait_s=5)
        out = self.root / "plan.json"
        env = dict(os.environ, GROK_MCP_DATA_DIR=str(self.root / "state"))
        manage = str(Path(__file__).resolve().parents[1] / "manage.py")
        result = subprocess.run([sys.executable, manage, "--client", "codex", "cleanup-plan", "--sessions", sid, "--out", str(out)],
                                env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(json.loads(out.read_text(encoding="utf-8"))["task_ids"]), {tid, second})
        result = subprocess.run([sys.executable, manage, "--client", "codex", "cleanup-plan", "--sessions", "unknown", "--out", str(out)],
                                env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)

    def test_cleanup_locks_all_turns_against_keep(self):
        tid = self.spawn(wait_s=5)
        sid = self.store.get(tid)["session_id"]
        second = self.spawn(session_id=sid, wait_s=5)
        saved = plan(self.store, [tid])
        other = Store("codex", self.root / "state")
        attempts = []
        def delete(*args, **kwargs):
            for task_id in (tid, second):
                with self.assertRaises(ValueError):
                    other.keep(task_id, True)
                attempts.append(task_id)
            return subprocess_result
        from types import SimpleNamespace
        subprocess_result = SimpleNamespace(returncode=0, stderr="", stdout="")
        try:
            with patch("manage.subprocess.run", side_effect=delete), patch("manage.native_dir", return_value=None):
                apply_plan(self.store, saved, saved["confirmation"])
            self.assertEqual(set(attempts), {tid, second})
            self.assertFalse(self.store.path(tid).exists())
            self.assertEqual(self.store.get(second)["status"], "cleaned")
        finally:
            other.close()

    def test_cleanup_removes_only_registered_hidden_child_files(self):
        from types import SimpleNamespace
        import uuid
        tid = self.spawn(wait_s=5)
        child = str(uuid.uuid4())
        task = self.store.get(tid)
        task["children"] = [{"child_session_id": child}]
        self.store.save(task)
        home = self.root / "native"
        directory = home / "sessions" / "workspace" / child
        directory.mkdir(parents=True)
        (directory / "summary.json").write_text(json.dumps({"info": {"id": child}, "session_kind": "subagent"}))
        (directory / "updates.jsonl").write_text("owned history")
        survivor = self.root / "deliverable.txt"
        survivor.write_text("keep")
        with patch.dict(os.environ, {"GROK_HOME": str(home)}):
            saved = plan(self.store, [tid])
            with patch("manage.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="No session found", stderr="")):
                apply_plan(self.store, saved, saved["confirmation"])
        self.assertFalse(directory.exists())
        self.assertEqual(survivor.read_text(), "keep")

    def test_log_cleanup_waits_for_all_references_and_preserves_unknown_logs(self):
        from types import SimpleNamespace
        a = self.spawn(wait_s=5)
        b = self.spawn(wait_s=5)
        self.service.close()
        for tid in (a, b):
            task = self.store.get(tid)
            task["birth"] = "previous process"
            self.store.save(task)
        foreign = self.store.root / "logs" / "unknown.log"
        foreign.write_text("not registered")
        self.assertEqual(plan(self.store, [a])["log_files"], [])
        saved = plan(self.store, [a, b])
        self.assertEqual(len(saved["log_files"]), 1)
        with patch("manage.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
            apply_plan(self.store, saved, saved["confirmation"])
        self.assertFalse(Path(saved["log_files"][0]).exists())
        self.assertTrue(foreign.exists())

    def test_abandoned_cleanup_cannot_resume_partially_deleted_history(self):
        tid = self.spawn(wait_s=5)
        task = self.store.get(tid)
        task.update(status="cleaning", cleanup_pid=os.getpid(), cleanup_birth="previous process")
        self.store.save(task)
        self.store.recover()
        self.assertEqual(self.store.get(tid)["status"], "cleanup_failed")
        with self.assertRaises(ValueError):
            self.store.session(task["session_id"], "owner-a")
        self.assertIn("中文结果", self.store.output(task))

    def test_installer_preserves_existing_settings_and_backs_up(self):
        home = self.root / "codex"
        home.mkdir()
        config = home / "config.toml"
        config.write_text('model = "existing"\n', encoding="utf-8")
        original_hook = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-command"}]}]}}
        (home / "hooks.json").write_text(json.dumps(original_hook), encoding="utf-8")
        with patch.dict(os.environ, {"GROK_MCP_DATA_DIR": str(self.root / "state")}):
            backup = install_codex(home)
        self.assertEqual((backup / "config.toml").read_text(), 'model = "existing"\n')
        hooks = json.loads((home / "hooks.json").read_text())
        self.assertEqual(hooks["hooks"]["Stop"][0], original_hook["hooks"]["Stop"][0])
        self.assertEqual(len(hooks["hooks"]["Stop"]), 2)

    def test_claude_installer_registers_through_cli_and_merges_hooks(self):
        home = self.root / "claude"
        home.mkdir()
        original = {"env": {"KEEP": "1"}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing"}]}]}}
        (home / "settings.json").write_text(json.dumps(original), encoding="utf-8")
        state = self.root / "claude.json"
        state.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        with patch.dict(os.environ, {"GROK_MCP_DATA_DIR": str(self.root / "state")}),                 patch("install.subprocess.run") as run:
            backup = install_claude(home, state, claude="claude")
            self.assertEqual(json.loads((backup / "settings.json").read_text()), original)
            command = run.call_args.args[0]
            self.assertEqual(command[:6], ["claude", "mcp", "add-json", "--scope", "user", "grok"])
            self.assertEqual(json.loads(command[6])["args"][1:], ["--client", "claude"])
            settings = json.loads((home / "settings.json").read_text())
            self.assertEqual(settings["env"], {"KEEP": "1"})
            self.assertEqual(settings["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
            handler = settings["hooks"]["Stop"][1]["hooks"][0]
            self.assertEqual(handler["input"], {"event": "Stop"})
            state.write_text(json.dumps({"mcpServers": {"grok": {"args": json.loads(command[6])["args"]}}}), encoding="utf-8")
            install_claude(home, state, claude="claude")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(json.loads((home / "settings.json").read_text())["hooks"]["Stop"]), 2)
            state.write_text(json.dumps({"mcpServers": {"grok": {"args": ["other.py"]}}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                install_claude(home, state, claude="claude")

    def test_claude_hooks_collect_for_connection_owner(self):
        store = Store("claude", self.root / "state")
        service = Service(store, [sys.executable, str(Path(__file__).with_name("fake_grok.py")), str(self.wire)])
        output = Output()
        mcp = MCP(service, owner="claude-session", output=output)
        try:
            mcp.handle({"id": 1, "method": "tools/call", "params": {"name": "grok_spawn", "arguments": {
                "prompt": "small-task", "scope": "Only temporary test files", "cwd": str(self.root)}}})
            self.assertFalse(output.response(1).get("isError"))
            tid = store.list("claude-session")[0]["id"]
            until = time.monotonic() + 5
            while store.get(tid)["status"] != "completed" and time.monotonic() < until:
                time.sleep(.05)
            mcp.handle({"id": 2, "method": "tools/call", "params": {"name": "grok_collect", "arguments": {"event": "Stop"}}})
            payload = json.loads(output.response(2)["content"][0]["text"])
            self.assertEqual(payload["decision"], "block")
            self.assertIn(tid, payload["reason"])
            self.assertTrue(store.get(tid)["delivered"])
        finally:
            service.close()
            store.close()


if __name__ == "__main__":
    unittest.main()
